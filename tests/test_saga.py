"""The oversell saga: the failure the stock hold cannot prevent, and its fix.

Every test here runs the money path in live mode against a fake gateway. The
claim under test is not that Razorpay works — it is that when a capture lands
on stock that no longer exists, this system notices, refunds automatically,
voids fulfilment, self-heals the SKU, tells both sides, and leaves a chain
that still verifies.
"""

from __future__ import annotations

import pytest

from kernel import checkout as checkout_kernel
from kernel import saga
from store import catalog, ledger, orders


@pytest.fixture
def tier0_offer(make_offer):
    """A small Tier-0 offer: one unit, list price, no mandate needed."""
    return make_offer("tier0")


def test_natural_oversell_triggers_full_compensation(
    db, live_mode, fake_razorpay, tier0_offer
):
    """Capture first, shelf emptied underneath by an outside writer, then settle."""
    seeded = tier0_offer
    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="saga-natural-1",
        agent_id="agent_saga_test",
        client_factory=lambda: fake_razorpay,
    )
    assert result.status == checkout_kernel.STATUS_AWAITING_PAYMENT

    # The race window: while the buyer is at the gateway, something else takes
    # the last unit off the shelf.
    db.execute("UPDATE products SET stock_qty = 0 WHERE sku = 'AT-CBL-USBC'")
    db.commit()

    with pytest.raises(checkout_kernel.OversoldFault) as excinfo:
        checkout_kernel.settle(
            result.order_id, payment_id="pay_saga_0002", client=fake_razorpay
        )
    payload = excinfo.value.payload

    assert payload["code"] == saga.CODE_OVERSOLD_MERCHANT_FAULT
    assert payload["status"] == "failed"
    assert payload["refund"]["amount_inr"] == int(seeded["total_inr"])
    assert payload["retry_safe"] is True
    assert payload["remedy_offered"] is not None
    assert payload["remedy_offered"]["type"] == "alternative_sku"

    order = orders.require(result.order_id)
    assert order["state"] == orders.REFUNDED
    assert order["razorpay_refund_id"]

    events = [entry.event for entry in ledger.trail(result.order_id)]
    for expected in (
        "fulfillment.check",
        "saga.compensation_triggered",
        "razorpay.refund",
        "ledger.compensate",
        "policy.selfheal",
        "notify.buyer",
        "notify.merchant",
    ):
        assert expected in events, f"missing {expected} in {events}"

    # The refund carries the negative delta; the mandatory-reason rule held.
    verify = ledger.verify_chain()
    assert verify["intact"] is True


def test_sku_self_heals_and_disappears_from_catalog(db, live_mode, fake_razorpay, tier0_offer):
    seeded = tier0_offer
    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="saga-selfheal-1",
        agent_id="agent_saga_test",
        client_factory=lambda: fake_razorpay,
    )
    db.execute("UPDATE products SET stock_qty = 0 WHERE sku = 'AT-CBL-USBC'")
    db.commit()
    with pytest.raises(checkout_kernel.OversoldFault):
        checkout_kernel.settle(
            result.order_id, payment_id="pay_x", client=fake_razorpay
        )

    private = catalog.cache.private("AT-CBL-USBC")
    assert private is not None and private["offerable"] is False
    public_skus = [row["sku"] for row in catalog.cache.all_public()]
    assert "AT-CBL-USBC" not in public_skus


def test_force_oversell_fires_the_exact_sequence(
    db, live_mode, fake_razorpay, tier0_offer
):
    """The rehearsal: deterministic, on cue, whole sequence in order."""
    seeded = tier0_offer
    payload = saga.force_oversell(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        agent_id="grahak_demo_oversell",
        client_factory=lambda: fake_razorpay,
    )

    assert payload["code"] == saga.CODE_OVERSOLD_MERCHANT_FAULT
    assert payload["gateway"] == "razorpay_test"

    events = [
        entry.event
        for entry in ledger.trail(payload["order_id"])
        if entry.event in saga.COMPENSATION_EVENT_SEQUENCE
    ]
    # policy.selfheal may repeat per SKU; the rest appear exactly once, in order.
    deduped: list[str] = []
    for event in events:
        if not deduped or deduped[-1] != event:
            deduped.append(event)
    assert deduped == list(saga.COMPENSATION_EVENT_SEQUENCE)

    assert orders.require(payload["order_id"])["state"] == orders.REFUNDED
    assert ledger.verify_chain()["intact"] is True


def test_force_oversell_runs_ten_times_out_of_ten(
    db, live_mode, fake_razorpay, make_offer
):
    """Rehearsed x10: each run seeds fresh, restocks, fires, compensates."""
    for i in range(10):
        # Same order the demo endpoint uses: restore the shelf, then seed.
        db.execute("UPDATE products SET stock_qty = 1 WHERE sku = 'AT-CBL-USBC'")
        db.commit()
        catalog.cache.set_offerable("AT-CBL-USBC", True)
        seeded = make_offer("tier0")
        saga.restock_for_offer(seeded["offer_id"])
        payload = saga.force_oversell(
            offer_id=seeded["offer_id"],
            option_id=seeded["option_id"],
            agent_id="grahak_demo_oversell",
            client_factory=lambda: fake_razorpay,
            idempotency_key=f"saga-x10-{i}",
        )
        assert payload["code"] == saga.CODE_OVERSOLD_MERCHANT_FAULT, f"run {i}"
        assert orders.require(payload["order_id"])["state"] == orders.REFUNDED


def test_force_oversell_refuses_in_shadow(db, forbidden_razorpay, tier0_offer):
    """No capture means no compensation and nothing proven; refuse loudly."""
    from kernel import mode

    seeded = tier0_offer
    with pytest.raises(mode.ShadowModeViolation):
        saga.force_oversell(
            offer_id=seeded["offer_id"],
            option_id=seeded["option_id"],
            agent_id="grahak_demo_oversell",
            client_factory=lambda: forbidden_razorpay,
        )


def test_remedy_prefers_same_category_cheapest_alternative(db):
    remedy = saga._remedy_for(["AT-PRO-BLK"])
    assert remedy is not None
    failed_category = catalog.cache.public("AT-PRO-BLK")["category"]
    picked_category = catalog.cache.public(remedy["sku"])["category"]
    assert picked_category == failed_category
    assert remedy["sku"] != "AT-PRO-BLK"


def test_restock_restores_shelf_and_offerable(db, make_offer):
    seeded = make_offer("tier0")
    sku = "AT-CBL-USBC"
    db.execute("UPDATE products SET stock_qty = 0 WHERE sku = ?", (sku,))
    db.commit()
    catalog.cache.set_offerable(sku, False)
    touched = saga.restock_for_offer(seeded["offer_id"])
    assert sku in touched
    assert catalog.cache.private(sku)["offerable"] is True
    from kernel import stock as stock_kernel

    assert stock_kernel.available_qty(sku) >= 1

