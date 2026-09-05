from decimal import Decimal
from policy.pre_filter import CandidateDeal, CandidateItem, pre_filter
from policy.snapshot import EffectivePolicy, PolicySource
from policy.mec import HardConstraints, EconomicObjectives, NegotiationPermissions

def create_mock_policy():
    return EffectivePolicy(
        policy_id="p1",
        store_id="store-1",
        hard_constraints=HardConstraints(
            min_margin_pct=Decimal("10"),
            max_discount_pct_per_sku=20,
            max_cart_discount_pct=15,
            max_txn_without_human_inr=6000
        ),
        economic_objectives=EconomicObjectives(
            margin_weight=Decimal("0.25"),
            conversion_weight=Decimal("0.25"),
            aov_weight=Decimal("0.25"),
            inventory_velocity_weight=Decimal("0.25")
        ),
        negotiation=NegotiationPermissions(),
        source_chain=["STORE:1"],
        hash="h1",
        created_at="2024-01-01T00:00:00Z"
    )

def test_pre_filter_valid():
    policy = create_mock_policy()
    economics = {"SKU1": {"cost_inr": 80}}
    stock = {"SKU1": 10}
    
    valid_cand = CandidateDeal(
        candidate_id="c1",
        items=(CandidateItem("SKU1", 1, 100, Decimal("10"), "base"),),
        total_inr=100,
        discount_pct=Decimal("10"),
        rationale="valid"
    )
    
    res = pre_filter([valid_cand], policy, economics, stock)
    assert len(res.valid) == 1
    assert len(res.filtered) == 0

def test_pre_filter_unknown_sku():
    policy = create_mock_policy()
    economics = {}
    stock = {"SKU1": 10}
    cand = CandidateDeal("c1", (CandidateItem("SKU1", 1, 100, Decimal("0"), "base"),), 100, Decimal("0"), "")
    res = pre_filter([cand], policy, economics, stock)
    assert len(res.filtered) == 1
    assert res.filtered[0].bound_violated == "sku_existence"

def test_pre_filter_max_discount():
    policy = create_mock_policy()
    economics = {"SKU1": {"cost_inr": 80}}
    stock = {"SKU1": 10}
    cand = CandidateDeal("c1", (CandidateItem("SKU1", 1, 100, Decimal("25"), "base"),), 100, Decimal("0"), "")
    res = pre_filter([cand], policy, economics, stock)
    assert len(res.filtered) == 1
    assert res.filtered[0].bound_violated == "max_discount_pct_per_sku"

def test_pre_filter_floor_price():
    policy = create_mock_policy()
    economics = {"SKU1": {"cost_inr": 80}} # floor is 80 / 0.9 = 88.88
    stock = {"SKU1": 10}
    cand = CandidateDeal("c1", (CandidateItem("SKU1", 1, 85, Decimal("0"), "base"),), 85, Decimal("0"), "")
    res = pre_filter([cand], policy, economics, stock)
    assert len(res.filtered) == 1
    assert res.filtered[0].bound_violated == "min_margin_pct"

def test_pre_filter_insufficient_stock():
    policy = create_mock_policy()
    economics = {"SKU1": {"cost_inr": 80}}
    stock = {"SKU1": 0}
    cand = CandidateDeal("c1", (CandidateItem("SKU1", 1, 100, Decimal("0"), "base"),), 100, Decimal("0"), "")
    res = pre_filter([cand], policy, economics, stock)
    assert len(res.filtered) == 1
    assert res.filtered[0].bound_violated == "stock_available"

def test_pre_filter_empty():
    res = pre_filter([], create_mock_policy(), {}, {})
    assert len(res.valid) == 0
    assert len(res.filtered) == 0
