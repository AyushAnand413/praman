"""The ledger's two hard guarantees.

First: a hand-edited historic row makes /audit/verify report
`{intact: false, broken_at: N}`.
Second: a money event with an empty reason is rejected at write time.
"""

from __future__ import annotations

import sqlite3

import pytest

try:
    import psycopg2  # type: ignore
except ImportError:
    psycopg2 = None  # type: ignore

from settings import LEDGER_GENESIS_PREV_HASH
from store import ledger
from store.canonical import NonCanonicalValue, canonical_json

DROP_GUARDS = (
    "DROP TRIGGER IF EXISTS ledger_no_update;"
    "DROP TRIGGER IF EXISTS ledger_no_delete;"
)


def _fill(conn, count: int = 4) -> None:
    for i in range(count):
        ledger.append(
            "policy_kernel",
            "policy.approved",
            {"step": i, "sku": "AT-PRO-BLK"},
            conn=conn,
        )


# ── chain structure ────────────────────────────────────────────────────────


def test_genesis_entry_links_to_zeroes(db):
    entry = ledger.append("system", "ledger.genesis", {"boot": True}, conn=db)
    assert entry.seq == 1
    assert entry.prev_hash == LEDGER_GENESIS_PREV_HASH
    assert LEDGER_GENESIS_PREV_HASH == "0" * 64
    assert len(entry.entry_hash) == 64


def test_each_entry_links_to_its_predecessor(db):
    first = ledger.append("system", "a", {"n": 1}, conn=db)
    second = ledger.append("vyapaari", "b", {"n": 2}, conn=db)
    assert second.prev_hash == first.entry_hash
    assert second.seq == first.seq + 1


def test_verify_reports_intact_chain(db):
    _fill(db, 5)
    report = ledger.verify_chain(db)
    assert report["intact"] is True
    assert report["broken_at"] is None
    assert report["entries_checked"] == 5


# ── the mandatory-reason rule ──────────────────────────────────────────────


def test_money_event_without_reason_is_rejected_at_write_time(db):
    """A money event with an empty reason never reaches the table."""
    with pytest.raises(ledger.MandatoryReasonMissing):
        ledger.append("razorpay", "payment.captured", {"amount_inr": 5598},
                      money_delta_inr=5598, conn=db)
    assert ledger.tip(db)[0] == 0, "a rejected write must not advance the chain"


def test_whitespace_reason_does_not_satisfy_the_rule(db):
    with pytest.raises(ledger.MandatoryReasonMissing):
        ledger.append("razorpay", "refund.processed", {}, money_delta_inr=-5598,
                      reason="   \n\t ", conn=db)


def test_negative_money_delta_also_requires_a_reason(db):
    with pytest.raises(ledger.MandatoryReasonMissing):
        ledger.append("razorpay", "refund.processed", {}, money_delta_inr=-100, conn=db)


def test_money_event_with_reason_is_accepted(db):
    entry = ledger.append(
        "razorpay", "payment.captured", {"razorpay_payment_id": "pay_test"},
        money_delta_inr=5598,
        reason="Captured Rs 5598 for order ORD-1 (AT-PRO-BLK + AT-CASE-01).",
        conn=db,
    )
    assert entry.money_delta_inr == 5598
    assert ledger.verify_chain(db)["intact"] is True


def test_non_money_event_needs_no_reason(db):
    entry = ledger.append("vyapaari", "proposal.emitted", {"upsells": 2}, conn=db)
    assert entry.money_delta_inr == 0
    assert entry.reason == ""


def test_sql_check_backstops_the_python_guard(db):
    """Even a direct INSERT that skips the writer cannot log unexplained money."""
    with pytest.raises((sqlite3.IntegrityError, psycopg2.IntegrityError) if psycopg2 else (sqlite3.IntegrityError,)):  # type: ignore
        db.execute(
            """INSERT INTO ledger (seq, ts, actor, event, payload, money_delta_inr,
                                   reason, policy_mode, prev_hash, entry_hash)
               VALUES (1, '2026-01-01T00:00:00.000000Z', 'razorpay', 'payment.captured',
                       '{}', 5598, '', 'live', ?, 'deadbeef')""",
            (LEDGER_GENESIS_PREV_HASH,),
        )
    try:
        db._pg.rollback()  # type: ignore[attr-defined]
    except Exception:
        pass


def test_unknown_actor_is_rejected(db):
    with pytest.raises(ledger.UnknownActor):
        ledger.append("marketing_bot", "offer.request", {}, conn=db)


# ── append-only enforcement ────────────────────────────────────────────────


def test_update_on_ledger_is_refused_by_the_database(db):
    _fill(db, 2)
    with pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE ledger SET reason = 'rewritten' WHERE seq = 1")


def test_delete_on_ledger_is_refused_by_the_database(db):
    _fill(db, 2)
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM ledger WHERE seq = 1")


# ── tamper detection ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "column, value",
    [
        ("payload", '{"step":0,"sku":"AT-AIR-BLK"}'),  # swap the product
        ("money_delta_inr", 0),                        # hide the money
        ("reason", "routine adjustment"),              # rewrite the explanation
        ("actor", "merchant"),                         # reassign blame
        ("ts", "2020-01-01T00:00:00.000000Z"),         # backdate
    ],
)
def test_editing_any_semantic_field_breaks_the_chain(db, column, value):
    """Why the hash core covers the whole entry, not just the payload.

    If only `payload` were hashed — the narrowest possible reading of the rule
    — four of these five edits would go undetected, including zeroing out the
    money.
    """
    ledger.append("system", "start", {"n": 0}, conn=db)
    ledger.append("razorpay", "payment.captured", {"n": 1}, money_delta_inr=5598,
                  reason="captured for ORD-1", conn=db)
    _fill(db, 2)
    assert ledger.verify_chain(db)["intact"] is True

    db.executescript(DROP_GUARDS)  # what DB write access buys an attacker
    db.execute(f"UPDATE ledger SET {column} = ? WHERE seq = 2", (value,))

    report = ledger.verify_chain(db)
    assert report["intact"] is False
    assert report["broken_at"] == 2
    assert report["detail"]


def test_blanking_a_reason_is_refused_even_with_the_triggers_gone(db):
    """The SQL CHECK outlives the append-only triggers.

    Dropping a trigger is one statement; removing a CHECK constraint requires
    rebuilding the table. So an attacker who wants to erase why money moved has
    to reconstruct `ledger` wholesale — and the chain still catches that.
    """
    ledger.append("system", "start", {"n": 0}, conn=db)
    ledger.append("razorpay", "payment.captured", {"n": 1}, money_delta_inr=5598,
                  reason="captured for ORD-1", conn=db)

    db.executescript(DROP_GUARDS)
    with pytest.raises((sqlite3.IntegrityError, psycopg2.IntegrityError) if psycopg2 else (sqlite3.IntegrityError,), match="money_delta_inr"):  # type: ignore
        db.execute("UPDATE ledger SET reason = '' WHERE seq = 2")

    # The failed UPDATE leaves the Postgres transaction aborted; clear it
    # so the read that follows does not see "current transaction is aborted"
    try:
        db._pg.rollback()  # type: ignore[attr-defined]
    except Exception:
        pass
    assert ledger.verify_chain(db)["intact"] is True


def test_removing_an_entry_is_detected_as_a_sequence_gap(db):
    _fill(db, 4)
    db.executescript(DROP_GUARDS)
    db.execute("DELETE FROM ledger WHERE seq = 2")

    report = ledger.verify_chain(db)
    assert report["intact"] is False
    assert report["broken_at"] == 3
    assert "gap" in report["detail"]


def test_verify_endpoint_reports_the_break(client, db):
    """Tamper detection through the HTTP surface the demo actually uses."""
    _fill(db, 3)
    assert client.get("/audit/verify").json()["intact"] is True

    db.executescript(DROP_GUARDS)
    db.execute("UPDATE ledger SET payload = '{\"tampered\":true}' WHERE seq = 2")

    body = client.get("/audit/verify").json()
    assert body["intact"] is False
    assert body["broken_at"] == 2


def test_audit_entry_endpoint(client, db):
    entry = ledger.append("vyapaari", "proposal.emitted", {"upsells": 2}, conn=db)
    body = client.get(f"/audit/{entry.seq}").json()
    assert body["event"] == "proposal.emitted"
    assert body["entry_hash"] == entry.entry_hash
    assert client.get("/audit/424242").status_code == 404


# ── canonical JSON ─────────────────────────────────────────────────────────


def test_key_order_does_not_change_the_hash(db):
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_is_stable_and_compact():
    assert canonical_json({"z": [3, 1], "a": "x"}) == '{"a":"x","z":[3,1]}'


def test_equal_numbers_hash_identically():
    assert canonical_json({"n": 3.0}) == canonical_json({"n": 3})
    assert canonical_json({"n": -0.0}) == canonical_json({"n": 0})


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_representable_floats_are_refused(value):
    with pytest.raises(NonCanonicalValue):
        canonical_json({"n": value})


def test_sets_and_decimals_are_refused():
    from decimal import Decimal

    with pytest.raises(NonCanonicalValue):
        canonical_json({"s": {1, 2}})
    with pytest.raises(NonCanonicalValue):
        canonical_json({"d": Decimal("1.10")})
