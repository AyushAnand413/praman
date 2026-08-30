from __future__ import annotations

import pytest
from decimal import Decimal

from policy.mec import (
    ApprovalThresholds,
    EconomicObjectives,
    HardConstraints,
    MEC,
    MECScope,
    NegotiationPermissions,
)
from policy.resolver import resolve_effective_policy
from store.mec_store import save_mec_version
from store.timestamps import now_ts


def _create_mec(store_id: str, scope: MECScope, scope_value: str | None, version: int, hc: HardConstraints, neg: NegotiationPermissions) -> MEC:
    return MEC(
        mec_id=f"MEC-{scope.value}-{version}",
        version=version,
        store_id=store_id,
        scope=scope,
        scope_value=scope_value,
        hard_constraints=hc,
        objectives=EconomicObjectives(
            margin_weight=Decimal("0.4"),
            conversion_weight=Decimal("0.3"),
            aov_weight=Decimal("0.2"),
            inventory_velocity_weight=Decimal("0.1")
        ),
        negotiation=neg,
        created_at=now_ts(),
        hash="mock_hash"
    )

def test_resolver_fallback_to_defaults(db):
    policy = resolve_effective_policy("store-999", conn=db)
    
    # Check default values are populated (from settings)
    assert policy.hard_constraints is not None
    assert policy.economic_objectives is not None
    assert policy.negotiation is not None
    assert policy.source_chain == []
    assert policy.hash != ""


def test_resolver_store_level_only(db):
    hc = HardConstraints(
        min_margin_pct=Decimal("0.15"),
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    neg = NegotiationPermissions(price=True, quantity=True, bundles=True, substitutes=True)
    mec = _create_mec("store-1", MECScope.STORE, None, 1, hc, neg)
    save_mec_version(mec, conn=db)
    
    policy = resolve_effective_policy("store-1", conn=db)
    
    assert policy.source_chain == ["MECScope.STORE:1"]
    assert policy.hard_constraints.min_margin_pct == Decimal("0.15")


def test_resolver_category_override(db):
    hc_store = HardConstraints(
        min_margin_pct=Decimal("0.15"),
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    neg_store = NegotiationPermissions(price=True, bundles=True)
    mec_store = _create_mec("store-1", MECScope.STORE, None, 1, hc_store, neg_store)
    save_mec_version(mec_store, conn=db)
    
    hc_cat = HardConstraints(
        min_margin_pct=Decimal("0.20"), # overrides store
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    neg_cat = NegotiationPermissions(price=False, bundles=True) # restricts price
    mec_cat = _create_mec("store-1", MECScope.CATEGORY, "cat-1", 1, hc_cat, neg_cat)
    save_mec_version(mec_cat, conn=db)
    
    policy = resolve_effective_policy("store-1", category="cat-1", conn=db)
    
    assert policy.source_chain == ["MECScope.STORE:1", "MECScope.CATEGORY:1"]
    assert policy.hard_constraints.min_margin_pct == Decimal("0.20")
    assert policy.negotiation.price is False
    assert policy.negotiation.bundles is True


def test_resolver_sku_override(db):
    hc_store = HardConstraints(
        min_margin_pct=Decimal("0.15"),
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    neg_store = NegotiationPermissions()
    mec_store = _create_mec("store-1", MECScope.STORE, None, 1, hc_store, neg_store)
    save_mec_version(mec_store, conn=db)
    
    hc_sku = HardConstraints(
        min_margin_pct=Decimal("0.25"), # overrides store
        max_discount_pct_per_sku=5,     # overrides store
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    mec_sku = _create_mec("store-1", MECScope.SKU, "sku-1", 1, hc_sku, neg_store)
    save_mec_version(mec_sku, conn=db)
    
    policy = resolve_effective_policy("store-1", sku="sku-1", conn=db)
    
    assert policy.source_chain == ["MECScope.STORE:1", "MECScope.SKU:1"]
    assert policy.hard_constraints.min_margin_pct == Decimal("0.25")
    assert policy.hard_constraints.max_discount_pct_per_sku == 5


def test_resolver_full_4_layer(db):
    hc_store = HardConstraints(
        min_margin_pct=Decimal("0.10"), max_discount_pct_per_sku=10, max_cart_discount_pct=10, max_txn_without_human_inr=1000
    )
    mec_store = _create_mec("store-full", MECScope.STORE, None, 1, hc_store, NegotiationPermissions(price=True, quantity=True))
    save_mec_version(mec_store, conn=db)
    
    hc_cat = HardConstraints(
        min_margin_pct=Decimal("0.15"), max_discount_pct_per_sku=10, max_cart_discount_pct=10, max_txn_without_human_inr=1000
    )
    mec_cat = _create_mec("store-full", MECScope.CATEGORY, "cat-f", 1, hc_cat, NegotiationPermissions(price=True, quantity=False))
    save_mec_version(mec_cat, conn=db)
    
    hc_sku = HardConstraints(
        min_margin_pct=Decimal("0.20"), max_discount_pct_per_sku=10, max_cart_discount_pct=10, max_txn_without_human_inr=1000
    )
    mec_sku = _create_mec("store-full", MECScope.SKU, "sku-f", 1, hc_sku, NegotiationPermissions(price=True, quantity=True))
    save_mec_version(mec_sku, conn=db)
    
    hc_camp = HardConstraints(
        min_margin_pct=Decimal("0.25"), max_discount_pct_per_sku=50, max_cart_discount_pct=50, max_txn_without_human_inr=5000
    )
    mec_camp = _create_mec("store-full", MECScope.CAMPAIGN, "camp-f", 1, hc_camp, NegotiationPermissions(price=True, quantity=True))
    save_mec_version(mec_camp, conn=db)
    
    policy = resolve_effective_policy("store-full", category="cat-f", sku="sku-f", campaign_id="camp-f", conn=db)
    
    assert policy.source_chain == ["MECScope.STORE:1", "MECScope.CATEGORY:1", "MECScope.SKU:1", "MECScope.CAMPAIGN:1"]
    assert policy.hard_constraints.min_margin_pct == Decimal("0.25")
    assert policy.hard_constraints.max_discount_pct_per_sku == 50
    # Negotiation can only restrict. Category restricted quantity to False, so it must remain False.
    assert policy.negotiation.quantity is False
    assert policy.negotiation.price is True


def test_resolver_effective_policy_hash(db):
    hc = HardConstraints(
        min_margin_pct=Decimal("0.15"), max_discount_pct_per_sku=10, max_cart_discount_pct=15, max_txn_without_human_inr=5000,
    )
    mec = _create_mec("store-hash", MECScope.STORE, None, 1, hc, NegotiationPermissions())
    save_mec_version(mec, conn=db)
    
    policy = resolve_effective_policy("store-hash", conn=db)
    
    assert policy.hash is not None
    assert len(policy.hash) > 0
