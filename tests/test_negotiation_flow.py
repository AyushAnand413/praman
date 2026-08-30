"""The negotiation loop: two agents bargaining through the human gate.

This is the stage moment as an executable claim. Buyer agent asks for a cart
above the autonomous limit; the kernel holds it; the merchant counters at new
terms; those terms become a bounded, signed offer; the buyer polls, finds it,
and accepts — every hop on the ledger, no mocks of either side.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(client: TestClient):
    class Api:
        def __init__(self, c):
            self.c = c

        def offer_tier2(self):
            """3 x flagship: above Rs 6,000, so Tier 2 holds it."""
            from scripts.seed_offer import SCENARIOS_BY_KEY, seed as seed_scenario

            return seed_scenario(SCENARIOS_BY_KEY["tier2"])

        def checkout(self, offer_id, option_id, mandate, key):
            return self.c.post(
                "/agent/v1/checkout",
                json={
                    "offer_id": offer_id,
                    "option_id": option_id,
                    "agent_id": "grahak_negotiator",
                    "mandate": mandate,
                },
                headers={"Idempotency-Key": key},
            )

        def poll(self, order_id):
            return self.c.get(f"/agent/v1/order/{order_id}").json()

        def counter(self, approval_id, amount):
            return self.c.post(
                f"/merchant/v1/approvals/{approval_id}/counter",
                json={"counter_amount_inr": amount, "decided_by": "stage_merchant"},
                headers={"X-Merchant-Key": "test-demo-key-0123456789"},
            ).json()

    return Api(client)


def test_full_negotiation_hold_counter_accept(
    api, db, live_mode, fake_razorpay, trusted_issuer, mandate_for
):
    seeded = api.offer_tier2()
    original_total = int(seeded["total_inr"])
    assert seeded["gate_tier"] == 2

    # The buyer accepts the offered terms; the gate halts it for a human.
    first = api.checkout(
        seeded["offer_id"], seeded["option_id"],
        mandate=mandate_for(agent_id="grahak_negotiator"),
        key="nego-1",
    )
    assert first.status_code == 200
    held = first.json()
    assert held["status"] == "pending_merchant_approval"
    order_id = held["order_id"]

    # The buyer polls. Nothing auto-approves, ever.
    polled = api.poll(order_id)
    assert polled["status"] == "pending_merchant_approval"
    assert "counter_offer_id" not in polled

    # The merchant counters below the original price.
    counter_amount = original_total - 1200
    decision = api.counter(held["approval_id"], counter_amount)
    assert decision["decision"] == "COUNTERED"
    assert decision["counter_offer_id"]

    # The buyer's next poll surfaces the counter as an offer it can accept.
    polled = api.poll(order_id)
    assert polled["status"] == "countered"
    assert polled["counter_offer_id"] == decision["counter_offer_id"]
    assert int(polled["counter_amount_inr"]) == counter_amount

    # Acceptance is an ordinary checkout against the counter offer: bounds,
    # gate and receipt all run again on the merchant's own terms.
    from harness.grahak import Grahak

    grahak = Grahak(api.c, agent_id="grahak_negotiator")
    purchase = grahak.accept_counter(
        order_id,
        mandate=mandate_for(
            agent_id="grahak_negotiator", max_amount_inr=50_000
        ),
        idempotency_key="nego-counter-1",
    )
    assert purchase.amount_inr == counter_amount
    assert purchase.payload["policy_receipt"]["receipt_id"]


def test_polling_an_ordinary_order_has_no_counter(api, db, live_mode, fake_razorpay):
    """A plain confirmed order must not advertise a negotiation that never was."""
    from scripts.seed_offer import SCENARIOS_BY_KEY, seed as seed_scenario

    seeded = seed_scenario(SCENARIOS_BY_KEY["tier0"])
    result = __import__("kernel").checkout.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="nego-plain-1",
        agent_id="grahak_plain",
        payment_id="pay_nego_plain",
        client_factory=lambda: fake_razorpay,
    )
    polled = api.poll(result.order_id)
    assert "counter_offer_id" not in polled

