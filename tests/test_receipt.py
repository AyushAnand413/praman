"""The policy receipt — signature, coverage, and tamper detection.

A receipt's value is entirely in what its MAC covers. These tests attack that
surface directly: every signed field is mutated in turn and the signature must
break, because a field outside the MAC is a field an attacker can rewrite after
the fact.

The strongest test in the file is `test_every_signed_field_is_covered`, which
enumerates the body rather than naming fields. Adding a field to the receipt
without adding it to the signing body fails there automatically — which is the
only way a coverage claim survives future edits.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from kernel import mode, receipt as receipts
from kernel.bounds import (
    LineItem,
    ROLE_BASE,
    ROLE_UPSELL,
    check_idempotency_key,
    evaluate_offer,
)
from kernel.gates import assign_tier
from kernel.receipt import (
    RECEIPT_VERSION,
    SIGNATURE_ALGORITHM,
    PolicyReceipt,
    ReceiptError,
    _signing_body,
)
from store.canonical import canonical_json

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)

PRIVATE = {
    "AT-BASE": {"cost_inr": 3000, "floor_price_inr": 3600, "max_discount_pct": 12},
    "AT-UP": {"cost_inr": 200, "floor_price_inr": 250, "max_discount_pct": 12},
}
AVAILABLE = {"AT-BASE": 50, "AT-UP": 50}

KEY = b"a-fixed-key-for-these-tests"


def _evaluation(base_offered: int = 4600, upsell_offered: int | None = None):
    items = [LineItem("AT-BASE", 1, 5000, base_offered, role=ROLE_BASE)]
    if upsell_offered is not None:
        items.append(LineItem("AT-UP", 1, 500, upsell_offered, role=ROLE_UPSELL))
    return evaluate_offer(
        items,
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )


def _issue(offer_id: str = "OF_test", **kwargs) -> PolicyReceipt:
    evaluation = kwargs.pop("evaluation", None) or _evaluation()
    gate = kwargs.pop("gate", None) or assign_tier(
        total_inr=evaluation.total_inr,
        discount_pct=evaluation.discount_pct,
        tripped_bounds=evaluation.tripped_bounds,
    )
    kwargs.setdefault("key", KEY)
    return receipts.issue(offer_id=offer_id, evaluation=evaluation, gate=gate, **kwargs)


# ── issuing ────────────────────────────────────────────────────────────────────


def test_an_issued_receipt_verifies():
    signed = _issue()
    assert receipts.verify(signed.as_payload(), key=KEY) is True


def test_the_receipt_names_its_offer_and_carries_an_id_and_a_timestamp():
    signed = _issue(offer_id="OF_abc123")
    assert signed.offer_id == "OF_abc123"
    assert signed.receipt_id.startswith("PR")
    # An ISO-8601 UTC timestamp, parseable rather than merely present.
    assert datetime.fromisoformat(signed.issued_at).tzinfo is not None


def test_reasons_are_recorded_verbatim():
    """If the prose shown to a human could differ from the prose in the receipt,
    the receipt would not be evidence of anything."""
    shown = ("Rs 400 off, inside the 12% per-SKU cap", "no mandate required")
    signed = _issue(reasons=shown)
    assert signed.reasons == shown
    assert signed.as_payload()["reasons"] == list(shown)


def test_the_receipt_carries_a_verdict_per_item():
    signed = _issue(evaluation=_evaluation(4600, upsell_offered=480))
    assert len(signed.verdicts) == 2
    assert {v["sku"] for v in signed.verdicts} == {"AT-BASE", "AT-UP"}


def test_extra_bounds_are_covered_by_the_signature():
    """Bounds evaluated outside the cart — mandate checks, say — still get signed.

    Otherwise a receipt would attest to the cart's bounds while staying silent on
    the ones that actually decided the transaction.
    """
    extra = check_idempotency_key(key="k-1")
    signed = _issue(extra_bounds=(extra,))
    numbers = {b["bound"] for b in signed.gate["cart_bounds"]}
    assert 9 in numbers

    payload = signed.as_payload()
    payload["gate"]["cart_bounds"][-1]["passed"] = False
    assert receipts.verify(payload, key=KEY) is False


def test_the_totals_block_records_the_money_that_was_authorised():
    signed = _issue(evaluation=_evaluation(4600))
    assert signed.totals["total_inr"] == 4600
    assert signed.totals["list_total_inr"] == 5000
    assert signed.totals["discount_inr"] == 400
    assert signed.totals["discount_pct"] == "8.00"
    assert signed.totals["offer_failed"] is False


def test_a_refused_offer_still_gets_a_signed_receipt():
    """A refusal is a decision, and the buyer is owed the same evidence for it."""
    evaluation = _evaluation(1000)  # under floor, over the per-SKU cap
    assert evaluation.offer_failed is True
    signed = _issue(evaluation=evaluation)
    assert signed.totals["offer_failed"] is True
    assert signed.totals["failure_detail"]
    assert receipts.verify(signed.as_payload(), key=KEY) is True


# ── the signature covers the whole body ────────────────────────────────────────


def test_every_signed_field_is_covered():
    """Mutate each signed field in turn; every mutation must break the MAC.

    Enumerated from the body rather than hand-listed, so a field added to the
    receipt without being added to the signing material fails here rather than
    shipping as a silently unprotected field.
    """
    signed = _issue(reasons=("a reason",))
    payload = signed.as_payload()
    body = _signing_body(signed)
    assert set(payload) == set(body) | {"signature"}

    for field in body:
        tampered = json.loads(json.dumps(payload))  # deep copy
        original = tampered[field]
        if isinstance(original, str):
            tampered[field] = original + "x"
        elif isinstance(original, bool):
            tampered[field] = not original
        elif isinstance(original, int):
            tampered[field] = original + 1
        elif isinstance(original, list):
            tampered[field] = original + ["injected"]
        elif isinstance(original, dict):
            tampered[field] = {**original, "injected": True}
        else:
            pytest.fail(f"{field} has unexpected type {type(original)}")

        assert receipts.verify(tampered, key=KEY) is False, (
            f"the receipt still verified after {field} was changed, so {field} is "
            "outside the signature"
        )


def test_changing_the_amount_breaks_the_signature():
    """The single most important tamper case, called out on its own."""
    payload = _issue().as_payload()
    assert receipts.verify(payload, key=KEY) is True
    payload["totals"]["total_inr"] = 1
    assert receipts.verify(payload, key=KEY) is False


def test_downgrading_the_gate_tier_breaks_the_signature():
    """Rewriting a held transaction into an auto-approved one must not verify."""
    payload = _issue(
        evaluation=_evaluation(4600),
        gate=assign_tier(total_inr=14997, discount_pct=Decimal("0.00"), tripped_bounds=(6,)),
    ).as_payload()
    payload["gate_tier"] = 0
    assert receipts.verify(payload, key=KEY) is False


def test_rewriting_a_reason_breaks_the_signature():
    payload = _issue(reasons=("the real reason",)).as_payload()
    payload["reasons"] = ["a reason invented afterwards"]
    assert receipts.verify(payload, key=KEY) is False


def test_relabelling_a_shadow_receipt_as_live_breaks_the_signature():
    payload = _issue().as_payload()
    payload["policy_mode"] = "shadow" if payload["policy_mode"] == "live" else "live"
    assert receipts.verify(payload, key=KEY) is False


def test_the_signature_does_not_cover_itself():
    """A MAC over its own field would be impossible to compute or to check."""
    signed = _issue()
    assert "signature" not in _signing_body(signed)
    assert "signature" not in _signing_body(signed.as_payload())


def test_the_version_and_algorithm_are_signed():
    """Both are inside the MAC, so an old receipt reads as old rather than forged."""
    body = _signing_body(_issue())
    assert body["version"] == RECEIPT_VERSION
    assert body["algorithm"] == SIGNATURE_ALGORITHM


# ── verification ───────────────────────────────────────────────────────────────


def test_a_receipt_signed_with_another_key_does_not_verify():
    payload = _issue().as_payload()
    assert receipts.verify(payload, key=b"a-different-key") is False


@pytest.mark.parametrize("bad", [None, "", 0, [], {}, 12345])
def test_a_missing_or_non_string_signature_is_a_failure_not_an_exception(bad):
    """A forged receipt and a malformed one are handled identically by callers."""
    payload = _issue().as_payload()
    payload["signature"] = bad
    assert receipts.verify(payload, key=KEY) is False


def test_a_signature_of_the_wrong_length_does_not_raise():
    """`compare_digest` on unequal lengths returns False; it must not throw."""
    payload = _issue().as_payload()
    payload["signature"] = "ab"
    assert receipts.verify(payload, key=KEY) is False


def test_verifying_a_partial_receipt_is_refused_rather_than_failed():
    """Dropping a field must not be a way to make verification easier.

    Were a missing field merely omitted from the material, an attacker could strip
    `totals` and re-sign a receipt that says nothing about the amount.
    """
    payload = _issue().as_payload()
    del payload["totals"]
    with pytest.raises(ReceiptError) as exc:
        receipts.verify(payload, key=KEY)
    assert "totals" in str(exc.value)


def test_an_unexpected_extra_field_breaks_verification():
    """Anything not in the signed set changes the material, so the MAC misses."""
    payload = _issue().as_payload()
    payload["approved_by"] = "me"
    assert receipts.verify(payload, key=KEY) is False


def test_require_valid_passes_a_good_receipt_and_raises_on_a_bad_one():
    payload = _issue().as_payload()
    receipts.require_valid(payload, key=KEY)  # no exception

    payload["totals"]["total_inr"] = 999_999
    with pytest.raises(ReceiptError) as exc:
        receipts.require_valid(payload, key=KEY)
    assert "do not match the decision that was signed" in str(exc.value)


def test_the_error_names_the_receipt_it_rejected():
    payload = _issue().as_payload()
    receipt_id = payload["receipt_id"]
    payload["gate_tier"] = 0
    with pytest.raises(ReceiptError) as exc:
        receipts.require_valid(payload, key=KEY)
    assert receipt_id in str(exc.value)


# ── determinism and serialisation ──────────────────────────────────────────────


def test_the_same_body_signs_to_the_same_mac():
    """Key order and whitespace cannot change the MAC, or nothing would verify
    after a round trip through JSON."""
    signed = _issue()
    payload = signed.as_payload()
    reordered = dict(reversed(list(payload.items())))
    assert receipts.verify(reordered, key=KEY) is True

    round_tripped = json.loads(json.dumps(payload, indent=4, sort_keys=True))
    assert receipts.verify(round_tripped, key=KEY) is True


def test_two_receipts_for_the_same_decision_differ_only_in_id_and_time():
    """Each issuance is its own event, so ids and timestamps are not reused."""
    first, second = _issue(), _issue()
    assert first.receipt_id != second.receipt_id
    assert first.totals == second.totals
    assert first.signature != second.signature  # different ids, different MAC


def test_the_payload_canonicalises():
    """The receipt is stored as JSON and hashed into the ledger, so it must have
    a canonical form — which means no Decimal and no float anywhere inside it."""
    payload = _issue().as_payload()
    canonical_json(payload)  # raises on Decimal, set, or NaN

    def assert_no_floats(node, path="receipt"):
        if isinstance(node, float) or isinstance(node, Decimal):
            pytest.fail(f"{path} is a {type(node).__name__}")
        if isinstance(node, dict):
            for key, value in node.items():
                assert_no_floats(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                assert_no_floats(value, f"{path}[{index}]")

    assert_no_floats(payload)


def test_the_receipt_object_is_frozen():
    """A signed receipt whose fields could change would not be a receipt."""
    signed = _issue()
    with pytest.raises(Exception):
        signed.signature = "rewritten"  # type: ignore[misc]


# ── mode ───────────────────────────────────────────────────────────────────────


def test_the_receipt_records_the_mode_it_was_issued_in():
    signed = _issue()
    assert signed.policy_mode == mode.mode_value()
    assert signed.as_payload()["policy_mode"] == mode.mode_value()


def test_a_shadow_receipt_is_signed_like_any_other(monkeypatch):
    """Shadow mode is a rehearsal, not a different code path.

    Same evaluation, same signing material, same verification — with the mode
    recorded inside the MAC so a shadow receipt cannot later be read as live.
    """
    monkeypatch.setattr(mode, "mode_value", lambda: "shadow")
    signed = _issue()
    assert signed.policy_mode == "shadow"
    assert receipts.verify(signed.as_payload(), key=KEY) is True


# ── the ambient key ────────────────────────────────────────────────────────────


def test_the_default_key_comes_from_the_environment(test_secrets):
    """With no explicit key, the secret is read from the environment — never from
    a file in the repository and never a hardcoded default."""
    signed = _issue(key=None)
    assert receipts.verify(signed.as_payload()) is True
    assert receipts.verify(signed.as_payload(), key=KEY) is False


def test_issuing_without_a_configured_secret_refuses_rather_than_signs(monkeypatch):
    """An unsigned receipt that looked signed would be worse than no receipt."""
    from settings import MissingSecret

    monkeypatch.delenv("POLICY_RECEIPT_HMAC_SECRET", raising=False)
    with pytest.raises(MissingSecret):
        _issue(key=None)
