"""Bound 10 — an upsell must relate to what it accompanies.

The pure-function contract, the composer wiring, and one end-to-end proof:
a nonsense upsell is dropped from a live offer with the bound named on the
receipt, while evidence-backed companions sail through.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import settings
from kernel.bounds import (
    LineItem,
    ROLE_BASE,
    ROLE_UPSELL,
    check_relatedness,
    evaluate_offer,
)
from kernel.relations import related_by_base_for_items
from store import pairings
from store.timestamps import utc_now


def test_check_relatedness_passes_on_known_companion():
    verdict = check_relatedness(
        sku="CHARGER", base_sku="PHONE", related_skus=frozenset({"CHARGER", "CASE"})
    )
    assert verdict.passed is True
    assert verdict.bound == 10
    assert verdict.bound_id == "relatedness_required"


def test_check_relatedness_rejects_the_unknown():
    verdict = check_relatedness(
        sku="CAT-FOOD", base_sku="PHONE", related_skus=frozenset({"CHARGER"})
    )
    assert verdict.passed is False
    assert "not known to accompany" in verdict.detail


def _cart_items(upsell_sku: str) -> list[LineItem]:
    return [
        LineItem(
            sku="AT-PRO-BLK",
            qty=1,
            list_price_inr=4999,
            offered_price_inr=4999,
            role=ROLE_BASE,
        ),
        LineItem(
            sku=upsell_sku,
            qty=1,
            list_price_inr=599,
            offered_price_inr=599,
            role=ROLE_UPSELL,
        ),
    ]


def _private(items):
    return {
        i.sku: {"cost_inr": int(i.offered_price_inr * 0.7), "max_discount_pct": 12}
        for i in items
    }


def test_bound_10_is_inert_when_no_map_is_supplied():
    """Omitting the parameter reproduces the original nine bounds exactly."""
    items = _cart_items("AT-CASE-01")
    evaluation = evaluate_offer(
        items,
        private_by_sku=_private(items),
        available_by_sku={i.sku: 10 for i in items},
        offers_made=0,
        spent_today_inr=0,
        now=utc_now(),
    )
    all_bounds = [b.bound for b in evaluation.all_bounds]
    assert 10 not in all_bounds


def test_bound_10_drops_unrelated_upsell_and_keeps_the_base(db):
    # No learned data, no declared companion connects AT-DAC-01 to the phone.
    items = _cart_items("AT-DAC-01")
    evaluation = evaluate_offer(
        items,
        private_by_sku=_private(items),
        available_by_sku={i.sku: 10 for i in items},
        offers_made=0,
        spent_today_inr=0,
        now=utc_now(),
        related_by_base={"AT-PRO-BLK": frozenset({"AT-CASE-01"})},
    )

    assert not evaluation.offer_failed  # the base survives; only the line drops
    rejected = [v.item.sku for v in evaluation.rejected_items]
    assert rejected == ["AT-DAC-01"]
    failed = [b for v in evaluation.item_verdicts for b in v.failed_bounds]
    assert any(b.bound == 10 for b in failed)


def test_bound_10_passes_declared_companion_end_to_end(db):
    """The full offer path: attach_candidates keep legacy pairings alive."""
    items = [
        LineItem("AT-PRO-BLK", 1, 4999, 4699, ROLE_BASE),
        LineItem("AT-CASE-01", 1, 599, 549, ROLE_UPSELL),
    ]
    related = related_by_base_for_items(items)
    assert "AT-CASE-01" in related["AT-PRO-BLK"]  # declared in product_private

    evaluation = evaluate_offer(
        items,
        private_by_sku={
            "AT-PRO-BLK": {"cost_inr": 3299, "max_discount_pct": 12},
            "AT-CASE-01": {"cost_inr": 380, "max_discount_pct": 12},
        },
        available_by_sku={i.sku: 10 for i in items},
        offers_made=0,
        spent_today_inr=0,
        now=utc_now(),
        related_by_base=related,
    )
    assert evaluation.offer_failed is False
    assert len(evaluation.approved_items) == 2


def test_learned_evidence_promotes_a_companion_into_enforcement(db):
    """Once real orders cross the sample threshold, the bound trusts them."""
    base = "AT-PRO-BLK"
    for _ in range(settings.RELATEDNESS_MIN_SAMPLES):
        pairings.record_order_basket(base, ["AT-TIP-FOAM"])

    related = related_by_base_for_items(_cart_items("AT-TIP-FOAM"))
    assert "AT-TIP-FOAM" in related[base]


def test_low_sample_pairs_stay_out_of_enforcement(db):
    """Two baskets are an anecdote; the bound refuses to weaponise them."""
    base = "AT-PRO-BLK"
    for _ in range(settings.RELATEDNESS_MIN_SAMPLES - 1):
        pairings.record_order_basket(base, ["AT-SPK-MINI"])

    related = related_by_base_for_items(_cart_items("AT-SPK-MINI"))
    assert "AT-SPK-MINI" not in related[base]


def test_completed_sale_feeds_learning_through_settle(
    db, live_mode, fake_razorpay, make_offer, mandate_for
):
    """The loop closes: capture → basket recorded → pairings table grows."""
    from kernel import checkout as checkout_kernel

    # The upsell scenario sits in the mandate band (Rs 5,697), so a signed
    # mandate rides along; the learning hook runs after capture either way.
    seeded = make_offer("upsell")
    before = pairings.snapshot()["observed_pairs"]

    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="learning-e2e-1",
        agent_id="agent-test",
        mandate_token=mandate_for(),
        payment_id="pay_learn_0001",
        client_factory=lambda: fake_razorpay,
    )
    assert result.status == checkout_kernel.STATUS_CONFIRMED

    base_sku = "AT-PRO-BLK"
    companions = {p["sku"] for p in pairings.pairs_for(base_sku)}
    assert {"AT-CASE-01", "AT-CBL-USBC"} <= companions

    # One basket: the denominator advanced by exactly one.
    pairs = {p["sku"]: p for p in pairings.pairs_for(base_sku)}
    assert pairs["AT-CASE-01"]["samples"] == 1
    assert pairings.snapshot()["observed_pairs"] >= before + 2

def test_cart_without_base_item_skips_relatedness_even_when_map_supplied():
    """A base-less cart cannot anchor relatedness; the bound stays out of it."""
    items = [
        LineItem(
            sku="AT-CASE-01",
            qty=1,
            list_price_inr=599,
            offered_price_inr=599,
            role=ROLE_UPSELL,
        )
    ]
    evaluation = evaluate_offer(
        items,
        private_by_sku=_private(items),
        available_by_sku={i.sku: 10 for i in items},
        offers_made=0,
        spent_today_inr=0,
        now=utc_now(),
        related_by_base={},  # map supplied, but no base exists to anchor on
    )
    assert 10 not in [b.bound for b in evaluation.all_bounds]
    assert evaluation.offer_failed is False


def test_declared_companions_of_unknown_sku_are_empty(db):
    from kernel.relations import declared_companions

    assert declared_companions("NOT-A-REAL-SKU") == frozenset()
