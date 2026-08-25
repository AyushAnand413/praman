"""The orchestrated checkout flow — the paths that actually move money.

The unit suites cover the bounds, the gate, the ledger and the payments client in
isolation. This file covers what happens when they are wired together, which is
where the interesting failures live: a gate that decides a mandate is required but
nothing that makes it binding, a stock hold that is taken and never committed, a
budget reserved for a payment that never arrives.

Every test here goes through `kernel.checkout` rather than reaching into the
pieces, because the ordering of the steps *is* the security property and only the
orchestrator has an ordering. A FakeRazorpay stands in for the gateway and its
`calls` list is the assertion surface — "how many times was the gateway contacted"
is the question idempotency is really asking.
"""

from __future__ import annotations

import pytest

from kernel import budgets, checkout as checkout_kernel, idempotency, stock
from kernel.gates import TIER_AUTO, TIER_HUMAN, TIER_MANDATE
from store import ledger, orders
from store.timestamps import plus_seconds, utc_now

AGENT = "agent-test"


def _checkout(offer, *, key, client, agent_id=AGENT, **kwargs):
    """Run a checkout against a seeded offer summary."""
    return checkout_kernel.checkout(
        offer_id=offer["offer_id"],
        option_id=offer["option_id"],
        idempotency_key=key,
        agent_id=agent_id,
        client_factory=lambda: client,
        **kwargs,
    )


def _events(conn) -> list[str]:
    return [row["event"] for row in conn.execute("SELECT event FROM ledger").fetchall()]


# ── the mandate is binding, not merely verified ───────────────────────────────
#
# The gate reports `requires_mandate`; these tests are about whether anything acts
# on it. Verifying a mandate that happens to be attached is a different and much
# weaker guarantee than requiring one, and the difference is invisible until a
# request simply omits the token.


def test_a_tier_1_cart_is_refused_when_no_mandate_is_presented(
    db, make_offer, fake_razorpay, live_mode, trusted_issuer
):
    offer = make_offer("tier1")
    assert offer["gate_tier"] == TIER_MANDATE, "scenario must be in the mandate band"

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        _checkout(offer, key="key-no-mandate", client=fake_razorpay)

    assert caught.value.code == "mandate_required"
    assert caught.value.http_status == 403
    # Refused before anything external, so the gateway was never contacted.
    assert fake_razorpay.calls == []


def test_a_refused_tier_1_cart_reserves_nothing(
    db, make_offer, fake_razorpay, live_mode, trusted_issuer
):
    """The refusal must cost nothing: no order, no hold, no budget.

    A refusal that left a hold behind would take the units off the shelf for the
    length of the TTL every time an agent forgot its mandate.
    """
    offer = make_offer("tier1")
    before = budgets.spent()

    with pytest.raises(checkout_kernel.CheckoutError):
        _checkout(offer, key="key-no-mandate", client=fake_razorpay)

    assert orders.for_session(offer["session_id"]) == []
    assert stock.live_holds() == []
    assert budgets.spent() == before


def test_the_refusal_leaves_the_idempotency_key_usable(
    db, make_offer, fake_razorpay, live_mode, mandate_for
):
    """The corrected retry is the whole point of refusing this early.

    An agent told "you need a mandate" is expected to attach one and send the
    identical request. If the first attempt burned the key, that retry would come
    back as `request_in_flight` forever and the agent could never recover.
    """
    offer = make_offer("tier1")

    with pytest.raises(checkout_kernel.CheckoutError):
        _checkout(offer, key="key-retried", client=fake_razorpay)

    result = _checkout(
        offer, key="key-retried", client=fake_razorpay, mandate_token=mandate_for()
    )

    assert result.status != checkout_kernel.STATUS_REPLAYED
    assert result.gate_tier == TIER_MANDATE


def test_a_tier_1_cart_proceeds_with_a_valid_mandate(
    db, make_offer, fake_razorpay, live_mode, mandate_for
):
    offer = make_offer("tier1")

    result = _checkout(
        offer, key="key-with-mandate", client=fake_razorpay, mandate_token=mandate_for()
    )

    assert result.status == checkout_kernel.STATUS_AWAITING_PAYMENT
    assert result.gate_tier == TIER_MANDATE
    assert "create_order" in fake_razorpay.names()


def test_a_tier_1_cart_is_refused_when_the_mandate_is_invalid(
    db, make_offer, fake_razorpay, live_mode, mandate_for
):
    """An expired mandate is a rejection on its own terms, not a missing one.

    Reaching the checkout refusal at all matters: the mandate branch rejects first
    with its specific code, which is what makes the audit trail say *why*.
    """
    stale = mandate_for(now=plus_seconds(utc_now(), -1_200))
    offer = make_offer("tier1")

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        _checkout(offer, key="key-stale", client=fake_razorpay, mandate_token=stale)

    assert caught.value.code == "mandate_expired"
    assert fake_razorpay.calls == []


def test_the_refusal_names_the_missing_mandate_on_the_ledger(
    db, make_offer, fake_razorpay, live_mode, trusted_issuer
):
    """A refusal nobody can see later is a refusal that did not happen."""
    offer = make_offer("tier1")

    with pytest.raises(checkout_kernel.CheckoutError):
        _checkout(offer, key="key-audited", client=fake_razorpay)

    rejections = [
        entry
        for entry in ledger.recent(50)
        if entry.event == "checkout.rejected"
        and entry.payload.get("reason_code") == "mandate_required"
    ]
    assert len(rejections) == 1
    assert rejections[0].payload["mandate_presented"] is False
    assert "signed mandate" in rejections[0].reason


def test_a_tier_2_cart_is_held_rather_than_refused_for_a_missing_mandate(
    db, make_offer, fake_razorpay, live_mode, trusted_issuer
):
    """Tier 2 also reports `requires_mandate`, and must not be refused here.

    A Tier 2 cart is meant to reach a person. Refusing it for a missing mandate
    would replace human review with a 403 and quietly delete the approvals path.
    """
    offer = make_offer("tier2")
    assert offer["gate_tier"] == TIER_HUMAN
    assert offer["requires_mandate"] is True

    result = _checkout(offer, key="key-tier2", client=fake_razorpay)

    assert result.status == checkout_kernel.STATUS_PENDING_APPROVAL
    assert result.state == orders.HELD
    assert result.approval_id is not None
    assert fake_razorpay.calls == []


# ── the first-order rule ──────────────────────────────────────────────────────


def test_a_small_first_order_captures_without_a_human(
    db, make_offer, fake_razorpay, live_mode
):
    """A Rs 399 cable is Tier 0 even though the agent has never bought before.

    "Never seen this agent" is a real risk signal, but the only agent identity in
    the request is a string the caller chose. Deriving the signal from it sends
    every honest first purchase to a person while stopping no adversary, who simply
    reuses an id. The authority is the amount rule, the bounds and the mandate.
    """
    offer = make_offer("tier0")
    assert offer["gate_tier"] == TIER_AUTO

    result = _checkout(offer, key="key-first-order", client=fake_razorpay)

    assert result.gate_tier == TIER_AUTO
    assert result.state != orders.HELD
    assert result.status == checkout_kernel.STATUS_AWAITING_PAYMENT


def test_a_fresh_agent_id_does_not_change_the_tier(
    db, make_offer, fake_razorpay, live_mode
):
    """Two unrelated agent ids, same cart, same tier — the signal is not consulted."""
    first = _checkout(
        make_offer("tier0"), key="key-a", client=fake_razorpay, agent_id="agent-one"
    )
    second = _checkout(
        make_offer("tier0"), key="key-b", client=fake_razorpay, agent_id="agent-two"
    )

    assert first.gate_tier == second.gate_tier == TIER_AUTO


# ── the two-step path commits stock ───────────────────────────────────────────
#
# The normal path returns a gateway order and ends. The capture arrives later as a
# separate request that knows an order id and nothing else, so what was reserved
# has to survive on the order row. Stock that never decrements is the kind of bug
# that only surfaces as an oversell weeks later.


def test_the_two_step_path_records_what_it_reserved(
    db, make_offer, fake_razorpay, live_mode
):
    offer = make_offer("tier0")

    result = _checkout(offer, key="key-recorded", client=fake_razorpay)

    hold_ids, budget_reserved = orders.reservation(result.order_id)
    assert len(hold_ids) == 1
    assert budget_reserved == 0  # tier0 is sold at list price
    assert stock.get(hold_ids[0])["state"] == stock.ACTIVE


def test_settling_a_two_step_checkout_decrements_stock(
    db, make_offer, fake_razorpay, live_mode
):
    """The bug this file exists for: settle is given only an order id.

    `settle` recovers the holds from the order row. Without that it commits nothing
    and the shelf never moves, while the order reports CAPTURED.
    """
    offer = make_offer("tier0")
    sku = offer["offer_id"] and "AT-CBL-USBC"
    before = stock.on_hand(sku)

    result = _checkout(offer, key="key-settle", client=fake_razorpay)
    assert result.status == checkout_kernel.STATUS_AWAITING_PAYMENT

    # Exactly what the HTTP endpoint passes: an order id and a payment id.
    settled = checkout_kernel.settle(
        result.order_id, payment_id="pay_test_0001", client=fake_razorpay
    )

    assert settled["state"] == orders.CAPTURED
    assert stock.on_hand(sku) == before - 1


def test_settling_clears_the_reservation_so_a_repeat_cannot_double_decrement(
    db, make_offer, fake_razorpay, live_mode
):
    """A retried settle must not sell the unit twice.

    Two things stop it, and both are asserted: the reservation is cleared, so a
    second call finds no holds to commit, and the hold itself is COMMITTED, so
    committing it again is a no-op rather than a second decrement.
    """
    offer = make_offer("tier0")
    before = stock.on_hand("AT-CBL-USBC")

    result = _checkout(offer, key="key-twice", client=fake_razorpay)
    hold_ids, _ = orders.reservation(result.order_id)

    checkout_kernel.settle(
        result.order_id, payment_id="pay_test_0002", client=fake_razorpay
    )
    assert orders.reservation(result.order_id) == ([], 0)

    # Committing the original holds again — the worst case, bypassing the cleared
    # reservation entirely — still moves nothing.
    report = stock.commit_settled(hold_ids)

    assert report["committed"] == hold_ids
    assert stock.on_hand("AT-CBL-USBC") == before - 1


def test_a_hold_that_lapsed_during_the_card_form_still_decrements(
    db, make_offer, fake_razorpay, live_mode
):
    """The normal case, not an edge one: the hold TTL is 120 seconds.

    Buyers spend longer than that filling in a card form, so by the time settle
    runs the hold has routinely expired. Refusing to decrement would not undo the
    captured sale — it would lose track of it, turning this order's problem into
    the next order's oversell.
    """
    offer = make_offer("tier0")
    before = stock.on_hand("AT-CBL-USBC")

    result = _checkout(offer, key="key-lapsed", client=fake_razorpay)
    hold_ids, _ = orders.reservation(result.order_id)

    # Age the hold past its TTL, exactly as the clock would.
    db.execute(
        "UPDATE stock_holds SET expires_at = ? WHERE hold_id = ?",
        ("2000-01-01T00:00:00.000000Z", hold_ids[0]),
    )

    checkout_kernel.settle(
        result.order_id, payment_id="pay_test_0003", client=fake_razorpay
    )

    assert stock.on_hand("AT-CBL-USBC") == before - 1
    anomalies = [e for e in ledger.recent(50) if e.event == "stock.commit_anomaly"]
    assert len(anomalies) == 1
    assert len(anomalies[0].payload["recovered"]) == 1


def test_a_genuine_oversell_is_reported_rather_than_swallowed(
    db, make_offer, fake_razorpay, live_mode
):
    """The one case the recovery cannot fix, and must not hide.

    If the shelf no longer holds the units — something else sold them while the
    payment was in flight — the decrement cannot happen. Stock is not silently
    driven negative and the discrepancy is named on the ledger.
    """
    offer = make_offer("tier0")
    result = _checkout(offer, key="key-oversold", client=fake_razorpay)

    db.execute("UPDATE products SET stock_qty = 0 WHERE sku = 'AT-CBL-USBC'")

    checkout_kernel.settle(
        result.order_id, payment_id="pay_test_0004", client=fake_razorpay
    )

    assert stock.on_hand("AT-CBL-USBC") == 0  # never negative
    anomalies = [e for e in ledger.recent(50) if e.event == "stock.commit_anomaly"]
    assert len(anomalies[0].payload["oversold"]) == 1


def test_the_single_call_path_also_commits_stock(
    db, make_offer, fake_razorpay, live_mode
):
    """The shortcut path passes its holds explicitly; it must reach the same place."""
    offer = make_offer("tier0")
    before = stock.on_hand("AT-CBL-USBC")

    result = _checkout(
        offer, key="key-one-call", client=fake_razorpay, payment_id="pay_test_0005"
    )

    assert result.status == checkout_kernel.STATUS_CONFIRMED
    assert result.state == orders.CAPTURED
    assert stock.on_hand("AT-CBL-USBC") == before - 1


# ── reserved discount budget comes back ───────────────────────────────────────


def test_an_abandoned_checkout_releases_its_reserved_budget(
    db, make_offer, fake_razorpay, live_mode, mandate_for
):
    """A buyer who closes the tab must not keep the day's discount budget.

    Stock self-heals — availability is computed against live holds, so the units
    come back on their own. Reserved budget does not: it is a row that only a
    release reduces, so every abandoned cart would permanently shrink the day and
    the shortfall would read as real spend.
    """
    # The `upsell` scenario proceeds to payment AND carries a real discount, which
    # is what makes it the right cart for a budget-reservation test. Its Rs 5,697
    # total sits in the mandate band, so it needs a mandate to reach payment at all.
    offer = make_offer("upsell")
    result = _checkout(
        offer, key="key-abandoned", client=fake_razorpay, mandate_token=mandate_for()
    )

    _, reserved = orders.reservation(result.order_id)
    assert reserved > 0
    assert budgets.spent() == reserved

    # Past the grace window, with the hold long gone.
    swept = checkout_kernel.expire_abandoned(
        now=plus_seconds(utc_now(), 3_600)
    )

    assert [entry["order_id"] for entry in swept] == [result.order_id]
    assert budgets.spent() == 0
    assert orders.require(result.order_id)["state"] == orders.FAILED


def test_the_sweep_leaves_a_buyer_who_is_still_paying_alone(
    db, make_offer, fake_razorpay, live_mode, mandate_for
):
    """The timing trap. A lapsed hold is NOT evidence of abandonment.

    The hold TTL is 120 seconds and card forms take longer, so sweeping on hold
    expiry alone would mark orders FAILED underneath buyers mid-payment — and
    settle would then refuse a captured payment, because FAILED is terminal.
    """
    offer = make_offer("upsell")
    result = _checkout(
        offer, key="key-still-paying", client=fake_razorpay, mandate_token=mandate_for()
    )
    hold_ids, reserved = orders.reservation(result.order_id)

    # Every hold has lapsed, but the order was touched moments ago.
    for hold_id in hold_ids:
        db.execute(
            "UPDATE stock_holds SET expires_at = ? WHERE hold_id = ?",
            ("2000-01-01T00:00:00.000000Z", hold_id),
        )

    assert checkout_kernel.expire_abandoned() == []
    assert orders.require(result.order_id)["state"] == orders.PENDING
    assert budgets.spent() == reserved

    # And the payment they were completing still settles.
    settled = checkout_kernel.settle(
        result.order_id, payment_id="pay_test_0006", client=fake_razorpay
    )
    assert settled["state"] == orders.CAPTURED


def test_the_sweep_is_idempotent(db, make_offer, fake_razorpay, live_mode, mandate_for):
    """Runs at boot and at the top of every checkout, so a second pass must be free."""
    offer = make_offer("upsell")
    result = _checkout(
        offer, key="key-swept-twice", client=fake_razorpay, mandate_token=mandate_for()
    )
    later = plus_seconds(utc_now(), 3_600)

    assert len(checkout_kernel.expire_abandoned(now=later)) == 1
    assert checkout_kernel.expire_abandoned(now=later) == []
    assert budgets.spent() == 0


def test_a_settled_order_is_never_swept(
    db, make_offer, fake_razorpay, live_mode, mandate_for
):
    """Releasing the budget of a payment that succeeded would understate real spend."""
    offer = make_offer("upsell")
    result = _checkout(
        offer,
        key="key-settled-not-swept",
        client=fake_razorpay,
        mandate_token=mandate_for(),
    )
    _, reserved = orders.reservation(result.order_id)

    checkout_kernel.settle(
        result.order_id, payment_id="pay_test_0007", client=fake_razorpay
    )

    assert checkout_kernel.expire_abandoned(now=plus_seconds(utc_now(), 3_600)) == []
    assert budgets.spent() == reserved


def test_a_failed_gateway_call_returns_the_budget_immediately(
    db, make_offer, live_mode, mandate_for
):
    """No waiting for a sweep when the failure is known at once."""

    class BrokenRazorpay:
        key_id = "rzp_test_broken"

        def create_order(self, *args, **kwargs):
            raise RuntimeError("gateway unavailable")

    offer = make_offer("upsell")

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        _checkout(
            offer,
            key="key-broken",
            client=BrokenRazorpay(),
            mandate_token=mandate_for(),
        )

    assert caught.value.code == "gateway_error"
    assert budgets.spent() == 0
    assert stock.live_holds() == []


# ── idempotency across the whole flow ─────────────────────────────────────────


def test_the_same_key_twice_contacts_the_gateway_once(
    db, make_offer, fake_razorpay, live_mode
):
    """The double-charge test. Asserted on gateway calls, not on a return value."""
    offer = make_offer("tier0")

    first = _checkout(offer, key="key-once", client=fake_razorpay)
    second = _checkout(offer, key="key-once", client=fake_razorpay)

    assert fake_razorpay.names().count("create_order") == 1
    assert second.status == checkout_kernel.STATUS_REPLAYED
    assert second.order_id == first.order_id


def test_a_reused_key_with_a_different_cart_is_refused(
    db, make_offer, fake_razorpay, live_mode
):
    """Same key, different request: the stored outcome would be the wrong answer."""
    first_offer = make_offer("tier0")
    other_offer = make_offer("tier0")

    _checkout(first_offer, key="key-shared", client=fake_razorpay)

    with pytest.raises(idempotency.FingerprintMismatch):
        _checkout(other_offer, key="key-shared", client=fake_razorpay)

    assert fake_razorpay.names().count("create_order") == 1


def test_a_replay_does_not_decrement_stock_twice(
    db, make_offer, fake_razorpay, live_mode
):
    offer = make_offer("tier0")
    before = stock.on_hand("AT-CBL-USBC")

    _checkout(offer, key="key-replay-stock", client=fake_razorpay, payment_id="pay_r1")
    _checkout(offer, key="key-replay-stock", client=fake_razorpay, payment_id="pay_r1")

    assert stock.on_hand("AT-CBL-USBC") == before - 1


def test_a_missing_idempotency_key_is_refused_before_any_lookup(
    db, make_offer, fake_razorpay, live_mode
):
    offer = make_offer("tier0")

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        _checkout(offer, key=None, client=fake_razorpay)

    assert caught.value.code == "idempotency_key_required"
    assert fake_razorpay.calls == []


def test_an_unknown_offer_leaves_the_key_reusable(db, fake_razorpay, live_mode):
    """A 404 is a pre-external refusal too: nothing happened worth replaying."""
    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        checkout_kernel.checkout(
            offer_id="offer_does_not_exist",
            option_id="primary",
            idempotency_key="key-404",
            agent_id=AGENT,
            client_factory=lambda: fake_razorpay,
        )

    assert caught.value.http_status == 404
    assert idempotency.get("key-404") is None


# ── shadow mode decides everything and calls nothing ──────────────────────────


def test_shadow_mode_completes_without_touching_the_gateway(
    db, make_offer, forbidden_razorpay
):
    """No `live_mode` fixture here — shadow is the default, as it is in a deploy.

    ForbiddenRazorpay raises on any call rather than merely recording none, so this
    proves the path ran and still called nothing. An empty call list would also be
    satisfied by code that never got that far.
    """
    offer = make_offer("tier0")

    result = _checkout(offer, key="key-shadow", client=forbidden_razorpay)

    assert result.status == checkout_kernel.STATUS_SHADOW
    assert result.would_have_charged is True
    assert result.policy_mode == "shadow"


def test_shadow_mode_still_signs_a_policy_receipt(
    db, make_offer, forbidden_razorpay
):
    """The verdict is identical to live; only the money is absent."""
    offer = make_offer("tier0")

    result = _checkout(offer, key="key-shadow-receipt", client=forbidden_razorpay)

    assert result.policy_receipt["signature"]
    # `gate_tier` is the signed field name; the nested `gate` object carries the
    # same value alongside the triggers that produced it.
    assert result.policy_receipt["gate_tier"] == TIER_AUTO
    assert result.policy_receipt["gate"]["gate_tier"] == TIER_AUTO
    assert result.reasons


def test_shadow_mode_commits_no_stock_and_holds_no_budget(
    db, make_offer, forbidden_razorpay, mandate_for
):
    offer = make_offer("upsell")
    before = stock.on_hand("AT-PRO-BLK")

    result = _checkout(
        offer,
        key="key-shadow-stock",
        client=forbidden_razorpay,
        mandate_token=mandate_for(),
    )

    assert stock.on_hand("AT-PRO-BLK") == before
    assert stock.live_holds() == []
    assert budgets.spent() == 0
    assert orders.reservation(result.order_id) == ([], 0)


def test_shadow_mode_still_enforces_the_mandate_requirement(
    db, make_offer, forbidden_razorpay, trusted_issuer
):
    """A policy that only holds in live mode is not a policy.

    Shadow mode exists to rehearse the real decision. If it approved carts live
    mode would refuse, the rehearsal would be worthless.
    """
    offer = make_offer("tier1")

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        _checkout(offer, key="key-shadow-mandate", client=forbidden_razorpay)

    assert caught.value.code == "mandate_required"


def test_shadow_mode_records_what_would_have_moved(
    db, make_offer, forbidden_razorpay
):
    offer = make_offer("tier0")

    _checkout(offer, key="key-shadow-ledger", client=forbidden_razorpay)

    skipped = [e for e in ledger.recent(50) if e.event == "payment.shadow_skipped"]
    assert len(skipped) == 1
    assert skipped[0].payload["would_have_charged"] is True
    assert skipped[0].payload["razorpay_called"] is False
    assert skipped[0].money_delta_inr == offer["total_inr"]


def test_settle_refuses_outright_in_shadow_mode(db, make_offer, fake_razorpay):
    """The kernel-level guard, not an API check: there is no endpoint to bypass."""
    from kernel.mode import ShadowModeViolation

    with pytest.raises(ShadowModeViolation):
        checkout_kernel.settle(
            "order_anything", payment_id="pay_x", client=fake_razorpay
        )


# ── the intent reaches the ledger before the gateway does ─────────────────────


def test_the_intent_is_recorded_before_the_gateway_is_called(
    db, make_offer, live_mode
):
    """If the process dies mid-payment, the ledger must already know what was tried.

    Enforced by a client that appends to the ledger the moment it is called: the
    intent entry has to already be there, so its sequence number is lower.
    """
    entry_seqs: dict[str, int] = {}

    class RecordingRazorpay:
        key_id = "rzp_test_recording"

        def create_order(self, amount_inr, *, receipt, notes=None, currency="INR"):
            entry_seqs["at_call"], _ = ledger.tip()
            return {
                "id": "order_recorded",
                "amount_inr": amount_inr,
                "currency": currency,
                "receipt": receipt,
                "status": "created",
            }

    offer = make_offer("tier0")
    _checkout(offer, key="key-ordering", client=RecordingRazorpay())

    intents = [e for e in ledger.recent(50) if e.event == "payment.intent"]
    assert len(intents) == 1
    assert intents[0].seq <= entry_seqs["at_call"]


def test_the_intent_names_the_holds_it_took(db, make_offer, fake_razorpay, live_mode):
    offer = make_offer("tier0")

    result = _checkout(offer, key="key-intent-holds", client=fake_razorpay)

    intent = next(e for e in ledger.recent(50) if e.event == "payment.intent")
    hold_ids, _ = orders.reservation(result.order_id)
    assert intent.payload["holds"] == sorted(hold_ids)


# ── the amount comes from storage, never from the caller ──────────────────────


def test_the_charged_amount_is_the_stored_offer_total(
    db, make_offer, fake_razorpay, live_mode, mandate_for
):
    offer = make_offer("tier1")

    result = _checkout(
        offer, key="key-amount", client=fake_razorpay, mandate_token=mandate_for()
    )

    assert result.amount_inr == offer["total_inr"] == 4_599
    create = next(c for c in fake_razorpay.calls if c[0] == "create_order")
    assert create[1][0] == 4_599


def test_a_captured_amount_that_disagrees_is_refused(
    db, make_offer, live_mode
):
    """Unreachable by design, and still refused rather than papered over."""
    from tests.conftest import FakeRazorpay

    class WrongAmountRazorpay(FakeRazorpay):
        def capture_payment(self, payment_id, amount_inr, *, currency="INR"):
            self._record("capture_payment", payment_id, amount_inr)
            return {
                "id": payment_id,
                "amount_inr": amount_inr + 1,
                "currency": currency,
                "status": "captured",
                "captured": True,
            }

    offer = make_offer("tier0")
    client = WrongAmountRazorpay()
    result = _checkout(offer, key="key-mismatch", client=client)

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        checkout_kernel.settle(
            result.order_id, payment_id="pay_bad", client=client
        )

    assert caught.value.code == "amount_mismatch"
    mismatches = [e for e in ledger.recent(50) if e.event == "payment.amount_mismatch"]
    assert len(mismatches) == 1


# ── the payment must belong to this order ─────────────────────────────────────


def test_a_payment_made_against_another_order_is_refused(
    db, make_offer, live_mode
):
    """Equal amounts are not the same order, and settle must not treat them as one.

    Without this check two carts for the same total are each settleable with the
    other's payment — and worse, one payment settles both, shipping twice for a
    single sum of money. The amount check cannot catch it precisely because the
    amounts agree.
    """
    from tests.conftest import FakeRazorpay

    offer = make_offer("tier0")
    client = FakeRazorpay(payment_order_id="order_someoneElsesOrder")
    result = _checkout(offer, key="key-foreign-payment", client=client)

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        checkout_kernel.settle(
            result.order_id, payment_id="pay_belongsElsewhere", client=client
        )

    assert caught.value.code == "payment_order_mismatch"
    assert caught.value.http_status == 409

    recorded = [e for e in ledger.recent(50) if e.event == "payment.order_mismatch"]
    assert len(recorded) == 1
    assert recorded[0].payload["presented_razorpay_order_id"] == (
        "order_someoneElsesOrder"
    )
    assert recorded[0].payload["expected_razorpay_order_id"] == (
        result.razorpay["order_id"]
    )
    assert recorded[0].reason

    # No capture was attempted: the refusal happens before the money call.
    assert "capture_payment" not in client.names()


def test_a_refused_foreign_payment_leaves_the_order_settleable(
    db, make_offer, live_mode
):
    """A wrong payment id is a caller mistake, not a failed payment.

    So unlike a capture failure this must not unwind: the order is still waiting
    for its own payment, and releasing its stock hold would penalise it for
    someone else's error. Asserted because the two paths sit next to each other
    and sharing the unwind would be the easy mistake.
    """
    from tests.conftest import FakeRazorpay

    offer = make_offer("tier0")
    client = FakeRazorpay(payment_order_id="order_notThisOne")
    result = _checkout(offer, key="key-foreign-then-real", client=client)

    holds_before, budget_before = orders.reservation(result.order_id)

    with pytest.raises(checkout_kernel.CheckoutError):
        checkout_kernel.settle(
            result.order_id, payment_id="pay_wrong", client=client
        )

    # The reservation survived, and so did the hold behind it.
    assert orders.reservation(result.order_id) == (holds_before, budget_before)
    assert stock.get(holds_before[0])["state"] == stock.ACTIVE
    assert orders.require(result.order_id)["state"] == orders.PENDING

    # And the correct payment still settles it.
    client.payment_order_id = None  # a payment against this order's own gateway order
    settled = checkout_kernel.settle(
        result.order_id, payment_id="pay_right", client=client
    )
    assert settled["state"] == orders.CAPTURED
