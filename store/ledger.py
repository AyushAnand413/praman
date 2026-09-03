"""The append-only, hash-chained ledger.

Three properties, each enforced structurally rather than by convention:

1. **Append-only.** No UPDATE, no DELETE — guaranteed by SQL triggers in
   `store.db`. Corrections are new compensating entries that reference the
   original.

2. **Hash-chained.** `entry_hash = SHA256(prev_hash + canonical_json(core))`,
   genesis `prev_hash = "0" * 64`. Altering a historic entry breaks every
   downstream hash, and `/audit/verify` reports the first break.

   The hash core is the WHOLE entry, not just the payload. Hashing only the
   payload would leave `money_delta_inr`, `reason`, `actor`, `event`, and `ts`
   unprotected — an editor could change Rs 5,598 to Rs 0, or blank a reason,
   and the chain would still verify. Since the guarantee on offer is "hand-edit
   a historic ledger row and verify reports the break", the hash must cover
   every semantic field. The formula's shape is unchanged; its input is wider.

3. **Mandatory reason.** An entry with `money_delta_inr != 0` and an empty
   reason is rejected at write time, here and again by a CHECK constraint in
   SQL.

Honesty note, worth repeating wherever this is described: this is
tamper-EVIDENCE, not tamper-proofing. Someone with database write access can
rewrite the entire chain. Claiming immutability would be false.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import settings
from settings import LEDGER_ACTORS, LEDGER_GENESIS_PREV_HASH
from store.canonical import canonical_json, entry_hash
from store.db import get_connection, transaction, write_lock


class LedgerError(RuntimeError):
    """Base class for refusals at write time."""


class MandatoryReasonMissing(LedgerError):
    """A money event with no reason."""


class UnknownActor(LedgerError):
    """An actor outside the closed set."""


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    ts: str
    actor: str
    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    money_delta_inr: int = 0
    reason: str = ""
    policy_mode: str = "shadow"
    prev_hash: str = LEDGER_GENESIS_PREV_HASH
    entry_hash: str = ""

    def as_public(self) -> dict[str, Any]:
        """Serialized form for /audit/{id}. The trail is public by design."""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "event": self.event,
            "payload": self.payload,
            "money_delta_inr": self.money_delta_inr,
            "reason": self.reason,
            "policy_mode": self.policy_mode,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash_core(
    *,
    seq: int,
    ts: str,
    actor: str,
    event: str,
    payload_json: str,
    money_delta_inr: int,
    reason: str,
    policy_mode: str,
) -> dict[str, Any]:
    """The material that gets hashed.

    `payload_json` is the stored canonical string, not a re-parsed object, so
    verification hashes exactly the bytes on disk and cannot drift from the
    writer.
    """
    return {
        "seq": seq,
        "ts": ts,
        "actor": actor,
        "event": event,
        "payload": payload_json,
        "money_delta_inr": money_delta_inr,
        "reason": reason,
        "policy_mode": policy_mode,
    }


def tip(conn=None) -> tuple[int, str]:
    """(seq, entry_hash) of the newest entry, or (0, genesis) when empty."""
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return 0, LEDGER_GENESIS_PREV_HASH
    return int(row["seq"]), str(row["entry_hash"])


def append(
    actor: str,
    event: str,
    payload: dict[str, Any] | None = None,
    *,
    money_delta_inr: int = 0,
    reason: str = "",
    policy_mode: str | None = None,
    conn=None,
) -> LedgerEntry:
    """Append one entry. The only writer.

    Raises before touching the database if the entry is inadmissible, so a
    refusal never leaves a partial row or advances the chain.
    """
    if actor not in LEDGER_ACTORS:
        raise UnknownActor(
            f"actor {actor!r} is outside the closed set of ledger actors. "
            "A new actor is a design decision, not a string."
        )

    money_delta_inr = int(money_delta_inr)

    # The mandatory-reason rule.
    if money_delta_inr != 0 and not reason.strip():
        raise MandatoryReasonMissing(
            f"event {event!r} moves money (money_delta_inr={money_delta_inr}) "
            "with an empty reason. Every money action must be explainable."
        )

    payload_json = canonical_json(payload or {})
    ts = _utc_now()
    # Read the mode through the module rather than a name bound at import time,
    # so an entry is always tagged with the mode actually in force when it was
    # written.
    mode = policy_mode or settings.POLICY_MODE.value

    conn = conn or get_connection()
    # The lock covers read-tip -> compute-hash -> insert as one unit; without
    # it two threads could chain off the same tip and produce a fork.
    # On Vercel multi-process, threading.Lock is insufficient -> also take
    # a Postgres advisory xact lock (no-op on SQLite/test).
    with write_lock:
        with transaction(conn):
            try:
                conn.execute("SELECT pg_advisory_xact_lock(424242)")
            except Exception:
                pass
            prev_seq, prev_hash = tip(conn)
            seq = prev_seq + 1
            digest = entry_hash(
                prev_hash,
                _hash_core(
                    seq=seq,
                    ts=ts,
                    actor=actor,
                    event=event,
                    payload_json=payload_json,
                    money_delta_inr=money_delta_inr,
                    reason=reason,
                    policy_mode=mode,
                ),
            )
            conn.execute(
                """INSERT INTO ledger
                       (seq, ts, actor, event, payload, money_delta_inr, reason,
                        policy_mode, prev_hash, entry_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (seq, ts, actor, event, payload_json, money_delta_inr, reason,
                 mode, prev_hash, digest),
            )

    return LedgerEntry(
        seq=seq,
        ts=ts,
        actor=actor,
        event=event,
        payload=payload or {},
        money_delta_inr=money_delta_inr,
        reason=reason,
        policy_mode=mode,
        prev_hash=prev_hash,
        entry_hash=digest,
    )


def get(seq: int, conn=None) -> LedgerEntry | None:
    conn = conn or get_connection()
    row = conn.execute("SELECT * FROM ledger WHERE seq = ?", (seq,)).fetchone()
    return _row_to_entry(row) if row else None


def recent(
    limit: int = 50,
    conn=None,
    before_seq: int | None = None,
) -> list[LedgerEntry]:
    """Newest entries first — the dashboard's live feed.

    `before_seq` pages backwards, returning only entries strictly older than
    that seq. It is a keyset cursor rather than a numeric offset because entries
    arrive while a merchant is reading: with OFFSET, a new row at the head
    shifts every later page down one and the reader sees a row twice or not at
    all. `seq` is monotonic, so a cursor cannot drift.
    """
    conn = conn or get_connection()
    if before_seq is None:
        rows = conn.execute(
            "SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (int(limit),)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE seq < ? ORDER BY seq DESC LIMIT ?",
            (int(before_seq), int(limit)),
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


#: The payload keys that identify a thing a trail can be requested for. Kept as a
#: named tuple of columns so the query below and the audit endpoint agree on what
#: counts as an identifier.
TRAIL_KEYS = ("order_id", "offer_id", "session_id", "approval_id")

_TRAIL_SQL = "SELECT * FROM ledger WHERE " + " OR ".join(
    f"json_extract(payload, '$.{key}') = ?" for key in TRAIL_KEYS
) + " ORDER BY seq"


def trail(entity_id: str, conn=None) -> list[LedgerEntry]:
    """Every entry mentioning one order, offer, session, or approval.

    This is what `/audit/{order_id}` serves: the complete story of one
    transaction rather than a single row. It scans the table, which is the honest
    trade at this scale — an index per identifier would speed up a query nobody
    runs at volume, and a scan cannot go stale the way a partial index can.
    """
    conn = conn or get_connection()
    rows = conn.execute(_TRAIL_SQL, (entity_id,) * len(TRAIL_KEYS)).fetchall()
    return [_row_to_entry(row) for row in rows]


def find_by_payload(
    key: str, value: str, conn=None
) -> list[LedgerEntry]:
    """Entries whose payload has `key` equal to `value`, oldest first.

    The webhook handler uses this to recognise a redelivered event: the ledger is
    the record of what has already been processed, so it can also be the authority
    on what has not. `key` is interpolated into the JSON path, so it must be a
    literal from the calling module and never request-supplied.
    """
    if not key.isidentifier():
        raise ValueError(f"payload key {key!r} must be a plain identifier")
    conn = conn or get_connection()
    rows = conn.execute(
        f"SELECT * FROM ledger WHERE json_extract(payload, '$.{key}') = ? ORDER BY seq",
        (value,),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def _row_to_entry(row) -> LedgerEntry:
    import json

    return LedgerEntry(
        seq=int(row["seq"]),
        ts=row["ts"],
        actor=row["actor"],
        event=row["event"],
        payload=json.loads(row["payload"]),
        money_delta_inr=int(row["money_delta_inr"]),
        reason=row["reason"],
        policy_mode=row["policy_mode"],
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
    )


def verify_chain(conn=None, limit: int | None = None) -> dict[str, Any]:
    """Recompute the whole chain and report the FIRST break.

    Returns `{intact, entries_checked, broken_at, ...}`. `broken_at` is the seq
    of the first entry that fails — which is the edited row itself, since its
    own hash no longer matches its contents.
    If limit is set, only the last `limit` entries are checked (faster, partial).
    """
    conn = conn or get_connection()
    if limit is not None:
        rows = conn.execute("SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (int(limit),)).fetchall()
        rows = list(reversed(rows))
    else:
        rows = conn.execute("SELECT * FROM ledger ORDER BY seq ASC").fetchall()

    if limit is not None and rows:
        expected_prev = str(rows[0]["prev_hash"])
        expected_seq = int(rows[0]["seq"])
    else:
        expected_prev = LEDGER_GENESIS_PREV_HASH
        expected_seq = 1
    checked = 0

    for row in rows:
        seq = int(row["seq"])

        # A missing seq means a row was removed — detectable even though the
        # triggers forbid DELETE, because a privileged rewrite can drop them.
        if seq != expected_seq:
            return _broken(
                seq,
                checked,
                f"sequence gap: expected seq {expected_seq}, found {seq} "
                "(entries were removed)",
                len(rows),
            )

        if row["prev_hash"] != expected_prev:
            return _broken(
                seq,
                checked,
                "prev_hash does not match the previous entry's entry_hash "
                "(chain re-linked or an entry was inserted)",
                len(rows),
            )

        recomputed = entry_hash(
            row["prev_hash"],
            _hash_core(
                seq=seq,
                ts=row["ts"],
                actor=row["actor"],
                event=row["event"],
                payload_json=row["payload"],
                money_delta_inr=int(row["money_delta_inr"]),
                reason=row["reason"],
                policy_mode=row["policy_mode"],
            ),
        )
        if recomputed != row["entry_hash"]:
            return _broken(
                seq,
                checked,
                "entry_hash does not match the entry's contents (row was edited)",
                len(rows),
            )

        # The mandatory-reason rule holds retroactively too: a money row whose
        # reason was blanked is already caught by the hash above, but report it
        # precisely.
        if int(row["money_delta_inr"]) != 0 and not str(row["reason"]).strip():
            return _broken(
                seq, checked, "money event with an empty reason", len(rows)
            )

        expected_prev = str(row["entry_hash"])
        expected_seq = seq + 1
        checked += 1

    head_seq, head_hash = (
        (int(rows[-1]["seq"]), str(rows[-1]["entry_hash"]))
        if rows
        else (0, LEDGER_GENESIS_PREV_HASH)
    )
    return {
        "intact": True,
        "entries_checked": checked,
        "broken_at": None,
        "detail": None,
        "head_seq": head_seq,
        "head_hash": head_hash,
        "genesis_prev_hash": LEDGER_GENESIS_PREV_HASH,
        "note": "Tamper-evidence, not tamper-proofing: write access to the "
                "database allows rewriting the whole chain.",
    }


def _broken(seq: int, checked: int, detail: str, total: int) -> dict[str, Any]:
    return {
        "intact": False,
        "entries_checked": checked,
        "broken_at": seq,
        "detail": detail,
        "entries_total": total,
        "note": "Tamper-evidence, not tamper-proofing.",
    }
