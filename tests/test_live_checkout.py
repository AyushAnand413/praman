"""The checkout orchestrator driven against the real Razorpay test-mode API.

    python -m pytest tests/test_live_checkout.py --live-api -v

`test_live_razorpay.py` proves the transport. This proves the money path: a
seeded offer goes through the whole of the orchestrator — bounds, gate, mandate,
stock hold, budget reservation, idempotency, ledger — and the gateway order at
the end of it is a genuine `order_...` in the Razorpay dashboard, created with
an amount this system chose and the caller never supplied.

What stops short of a capture, and why: creating a payment needs a card, and the
server-to-server payment API is not activated on this account. So these tests
end at `awaiting_payment` with a real gateway order. The capture itself is
`scripts/razorpay_smoke.py` plus one browser click, covered in the runbook.

Everything here runs in live POLICY_MODE against a throwaway database, so stock
commits and budget accrues exactly as it would in production.
"""

from __future__ import annotations

import pytest

from kernel import budgets, checkout as checkout_kernel, stock
from kernel.gates import TIER_AUTO, TIER_HUMAN, TIER_MANDATE
from store import ledger, orders

pytestmark = pytest.mark.live_api

AGENT = "agent-live-test"


def _checkout(offer, *, key, client, **kwargs):
    return checkout_kernel.checkout(
        offer_id=offer["offer_id"],
        option_id=offer["option_id"],
        idempotency_key=key,
        agent_id=AGENT,
        client_factory=lambda: client,
        **kwargs,
    )


# ── the happy path, end to end, with a real gateway order ──────────────────────


def test_a_tier_0_checkout_creates_a_real_gateway_order(
    db, make_offer, live_razorpay, live_mode
):
    """The full orchestrator, no mandate needed, against the real API.

    Tier 0 is the autonomous band: under Rs 2,000 at no discount, so no mandate
    and no human. The order id this returns is real and visible in the dashboard.
    """
    offer = make_offer("tier0")
    assert offer["gate_tier"] == TIER_AUTO

    result = _checkout(offer, key="live-tier0-001", client=live_razorpay)

    assert result.status == checkout_kernel.STATUS_AWAITING_PAYMENT
    assert result.state == orders.PENDING
    assert result.razorpay["order_id"].startswith("order_")
    assert result.razorpay["amount_inr"] == offer["total_inr"]
    assert result.policy_receipt["signature"]

    # Razorpay agrees about the amount, which is the point of asking it.
    gateway = live_razorpay.fetch_order(result.razorpay["order_id"])
    assert gateway["amount_inr"] == offer["total_inr"]
    assert gateway["status"] == "created"
    # The receipt field carries our order id, so the dashboard row is traceable.
    assert gateway["receipt"] == result.order_id


def test_a_tier_1_checkout_needs_a_mandate_and_then_reaches_the_gateway(
    db, make_offer, live_razorpay, live_mode, mandate_for
):
    """Rs 2,000-6,000 requires a signed mandate — refused without, real order with."""
    offer = make_offer("tier1")
    assert offer["gate_tier"] == TIER_MANDATE

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        _checkout(offer, key="live-tier1-nomandate", client=live_razorpay)
    assert caught.value.code == "mandate_required"

    result = _checkout(
        offer,
        key="live-tier1-001",
        client=live_razorpay,
        mandate_token=mandate_for(agent_id=AGENT),
    )
    assert result.status == checkout_kernel.STATUS_AWAITING_PAYMENT
    assert live_razorpay.fetch_order(result.razorpay["order_id"])["amount_inr"] == (
        offer["total_inr"]
    )


def test_the_amount_comes_from_storage_and_matches_the_gateway(
    db, make_offer, live_razorpay, live_mode, mandate_for
):
    """No amount field exists on the request, so the gateway figure is server-chosen.

    The cart carries a real discount, so list total and charged total differ — and
    the number that reached Razorpay must be the discounted one.
    """
    offer = make_offer("upsell")
    assert offer["total_inr"] < offer["list_total_inr"]

    result = _checkout(
        offer, key="live-amount-001", client=live_razorpay, mandate_token=mandate_for(agent_id=AGENT)
    )

    gateway = live_razorpay.fetch_order(result.razorpay["order_id"])
    assert gateway["amount_inr"] == offer["total_inr"]
    assert gateway["amount_inr"] != offer["list_total_inr"]


# ── Phase 2 deliverable 5: one key, one gateway call ───────────────────────────


def test_the_same_idempotency_key_creates_exactly_one_razorpay_order(
    db, make_offer, live_razorpay, live_mode
):
    """The double-charge guarantee, measured against the real gateway.

    Counted at the transport rather than inferred: the real client is wrapped so
    every outbound `create_order` is tallied, and the replay must both return the
    original gateway order id and add nothing to that tally. A local counter alone
    would prove the kernel *believes* it skipped the call; this proves no second
    HTTPS request was made.
    """

    class CountingClient:
        """Forwards to the real client, records what it forwarded."""

        def __init__(self, inner):
            self._inner = inner
            self.created: list[str] = []

        @property
        def key_id(self) -> str:
            return self._inner.key_id

        def create_order(self, amount_inr, **kwargs):
            order = self._inner.create_order(amount_inr, **kwargs)
            self.created.append(order["id"])
            return order

        def __getattr__(self, name):
            return getattr(self._inner, name)

    counting = CountingClient(live_razorpay)
    offer = make_offer("tier0")

    first = _checkout(offer, key="live-idem-001", client=counting)
    second = _checkout(offer, key="live-idem-001", client=counting)

    assert second.status == checkout_kernel.STATUS_REPLAYED
    assert second.order_id == first.order_id
    assert second.razorpay["order_id"] == first.razorpay["order_id"]

    # Exactly one real order creation reached Razorpay.
    assert len(counting.created) == 1
    assert counting.created == [first.razorpay["order_id"]]

    # And that one order genuinely exists at the gateway, unpaid.
    gateway = live_razorpay.fetch_order(first.razorpay["order_id"])
    assert gateway["amount_inr"] == offer["total_inr"]
    assert gateway["amount_paid_inr"] == 0


# ── Phase 2 deliverable 2: Rs 14,997 is held, and never reaches the gateway ────


def test_a_tier_2_cart_is_held_and_no_gateway_order_is_created(
    db, make_offer, live_razorpay, live_mode, mandate_for
):
    """Rs 14,997 exceeds the autonomous limit, so a human decides first.

    The strong form of the claim: not merely that the order is marked HELD, but
    that Razorpay was never contacted at all. Money must not be moveable while a
    human is still deciding.
    """
    offer = make_offer("tier2")
    assert offer["total_inr"] == 14_997
    assert offer["gate_tier"] == TIER_HUMAN

    result = _checkout(
        offer, key="live-tier2-001", client=live_razorpay, mandate_token=mandate_for(agent_id=AGENT)
    )

    assert result.status == checkout_kernel.STATUS_PENDING_APPROVAL
    assert result.state == orders.HELD
    assert result.approval_id
    assert not result.razorpay  # nothing was created at the gateway

    created = [
        entry
        for entry in ledger.recent(100)
        if entry.event == "razorpay.order.created"
    ]
    assert created == []


# ── Phase 2 deliverable 6: shadow mode computes everything, calls nothing ──────


def test_shadow_mode_reaches_no_gateway_even_with_real_credentials(
    db, make_offer, forbidden_razorpay
):
    """The claim is about behaviour, not about missing keys.

    Real credentials are present and usable in this run — `forbidden_razorpay`
    raises if anything touches it, so a passing test proves the path executed and
    still made no call, rather than proving it could not have.
    """
    offer = make_offer("tier0")

    result = _checkout(offer, key="live-shadow-001", client=forbidden_razorpay)

    assert result.status == checkout_kernel.STATUS_SHADOW
    assert result.would_have_charged is True
    assert result.policy_receipt["signature"]
    assert stock.live_holds() == []
    assert budgets.spent() == 0


# ── the ledger tells the truth about what happened ─────────────────────────────


def test_the_real_gateway_order_is_recorded_in_the_ledger(
    db, make_offer, live_razorpay, live_mode
):
    """A real money action that left no audit trail would defeat the point."""
    offer = make_offer("tier0")

    result = _checkout(offer, key="live-ledger-001", client=live_razorpay)

    recorded = next(
        entry
        for entry in ledger.recent(100)
        if entry.event == "razorpay.order.created"
    )
    assert recorded.payload["razorpay_order_id"] == result.razorpay["order_id"]
    assert recorded.payload["amount_inr"] == offer["total_inr"]
    assert recorded.reason

    report = ledger.verify_chain()
    assert report["intact"] is True
    assert report["broken_at"] is None


def test_a_real_checkout_holds_stock_and_reserves_budget(
    db, make_offer, live_razorpay, live_mode, mandate_for
):
    """The reservation is taken before the gateway call, not after it succeeds."""
    offer = make_offer("upsell")

    result = _checkout(
        offer, key="live-reserve-001", client=live_razorpay, mandate_token=mandate_for(agent_id=AGENT)
    )

    hold_ids, reserved = orders.reservation(result.order_id)
    assert hold_ids
    assert reserved > 0
    assert budgets.spent() == reserved
