from __future__ import annotations

from decimal import Decimal
import pytest

from policy.snapshot import EffectivePolicy, PolicySource
from policy.mec import HardConstraints, EconomicObjectives, NegotiationPermissions
from policy.negotiation import (
    evaluate_buyer_proposal,
    NegotiationOutcome,
    NegotiationState,
    NegotiationRound,
)

@pytest.fixture
def dummy_policy():
    return EffectivePolicy(
        policy_id="pol_1",
        store_id="store-1",
        hard_constraints=HardConstraints(
            min_margin_pct=Decimal("10.0"),
            max_discount_pct_per_sku=50,
            max_cart_discount_pct=50,
            max_txn_without_human_inr=6000
        ),
        economic_objectives=EconomicObjectives(
            margin_weight=Decimal("0.5"),
            conversion_weight=Decimal("0.5"),
            aov_weight=Decimal("0.0"),
            inventory_velocity_weight=Decimal("0.0")
        ),
        negotiation=NegotiationPermissions(),
        source_chain=["STORE:1"],
        hash="hash",
        created_at="2023-01-01T00:00:00Z"
    )

def test_proposed_price_within_feasible_range(dummy_policy):
    # variable cost 90, margin 10% -> floor = 100
    res = evaluate_buyer_proposal(
        proposed_price_inr=105,
        sku="SKU-1",
        quantity=1,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        stock_available=10
    )
    assert res.outcome == NegotiationOutcome.ACCEPTED
    assert res.accepted_price_inr == 105

def test_proposed_price_above_list_price(dummy_policy):
    # Merchant doesn't overcharge
    res = evaluate_buyer_proposal(
        proposed_price_inr=150,
        sku="SKU-1",
        quantity=1,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        stock_available=10
    )
    assert res.outcome == NegotiationOutcome.ACCEPTED
    assert res.accepted_price_inr == 120

def test_proposed_price_below_merchant_floor(dummy_policy):
    res = evaluate_buyer_proposal(
        proposed_price_inr=95,
        sku="SKU-1",
        quantity=1,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        stock_available=10
    )
    assert res.outcome == NegotiationOutcome.COUNTER
    assert res.counter_offers[0].counter_price_inr == 100
    assert res.counter_offers[0].savings_vs_list_inr == 20

def test_lower_quantity_counter_offer(dummy_policy):
    res = evaluate_buyer_proposal(
        proposed_price_inr=200,
        sku="SKU-1",
        quantity=3,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        buyer_budget_inr=250,
        stock_available=10
    )
    assert res.outcome == NegotiationOutcome.COUNTER
    assert len(res.counter_offers) == 2
    # First is same qty
    assert res.counter_offers[0].counter_price_inr == 300
    assert res.counter_offers[0].counter_items[0].quantity == 3
    # Second is affordable qty
    assert res.counter_offers[1].counter_price_inr == 200
    assert res.counter_offers[1].counter_items[0].quantity == 2

def test_no_overlap(dummy_policy):
    res = evaluate_buyer_proposal(
        proposed_price_inr=80,
        sku="SKU-1",
        quantity=1,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        buyer_budget_inr=95,
        stock_available=10
    )
    assert res.outcome == NegotiationOutcome.NO_FEASIBLE_DEAL

def test_escalate_on_round_limit(dummy_policy):
    res = evaluate_buyer_proposal(
        proposed_price_inr=95,
        sku="SKU-1",
        quantity=1,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        current_round=3,
        stock_available=10
    )
    assert res.outcome == NegotiationOutcome.ESCALATE

def test_insufficient_stock(dummy_policy):
    res = evaluate_buyer_proposal(
        proposed_price_inr=105,
        sku="SKU-1",
        quantity=2,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        stock_available=1
    )
    assert res.outcome == NegotiationOutcome.NO_FEASIBLE_DEAL

def test_zero_or_negative_price(dummy_policy):
    res = evaluate_buyer_proposal(
        proposed_price_inr=0,
        sku="SKU-1",
        quantity=1,
        effective_policy=dummy_policy,
        variable_cost_inr=90,
        list_price_inr=120,
        stock_available=10
    )
    assert res.outcome == NegotiationOutcome.NO_FEASIBLE_DEAL
