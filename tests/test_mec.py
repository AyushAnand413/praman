from __future__ import annotations

import pytest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from policy.mec import (
    ApprovalThresholds,
    EconomicObjectives,
    HardConstraints,
    MEC,
    MECScope,
    NegotiationPermissions,
    compute_hash,
    validate_objectives,
)
from store.timestamps import utc_now


def test_mec_creation_valid_data():
    hc = HardConstraints(
        min_margin_pct=Decimal("0.15"),
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    obj = EconomicObjectives(
        margin_weight=Decimal("0.4"),
        conversion_weight=Decimal("0.3"),
        aov_weight=Decimal("0.2"),
        inventory_velocity_weight=Decimal("0.1"),
    )
    neg = NegotiationPermissions()

    mec = MEC(
        mec_id="MEC-123",
        version=1,
        store_id="store-1",
        scope=MECScope.STORE,
        scope_value=None,
        hard_constraints=hc,
        objectives=obj,
        negotiation=neg,
        created_at=utc_now(),
        hash="",
    )

    assert mec.mec_id == "MEC-123"
    assert mec.scope == MECScope.STORE


def test_compute_hash_stable_and_reproducible():
    hc = HardConstraints(
        min_margin_pct=Decimal("0.15"),
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    obj = EconomicObjectives(
        margin_weight=Decimal("0.4"),
        conversion_weight=Decimal("0.3"),
        aov_weight=Decimal("0.2"),
        inventory_velocity_weight=Decimal("0.1"),
    )
    neg = NegotiationPermissions()

    mec1 = MEC(
        mec_id="MEC-123",
        version=1,
        store_id="store-1",
        scope=MECScope.STORE,
        scope_value=None,
        hard_constraints=hc,
        objectives=obj,
        negotiation=neg,
        created_at="2026-08-27T00:00:00Z",
        hash="",
    )

    hash1 = compute_hash(mec1)
    hash2 = compute_hash(mec1)

    assert hash1 == hash2

    mec2 = MEC(
        mec_id="MEC-999",  # different ID
        version=2,         # different version
        store_id="store-2",# different store
        scope=MECScope.CATEGORY, # different scope
        scope_value="cat1",
        hard_constraints=hc,
        objectives=obj,
        negotiation=neg,
        created_at="2026-08-27T01:00:00Z",
        hash="",
    )

    hash3 = compute_hash(mec2)
    
    # Hash only considers core content, not identifiers or temporal fields
    # So mec1 and mec2 should have the exact same hash
    assert hash1 == hash3


def test_compute_hash_different_content():
    hc1 = HardConstraints(
        min_margin_pct=Decimal("0.15"),
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    hc2 = HardConstraints(
        min_margin_pct=Decimal("0.20"), # Different
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    obj = EconomicObjectives(
        margin_weight=Decimal("0.4"),
        conversion_weight=Decimal("0.3"),
        aov_weight=Decimal("0.2"),
        inventory_velocity_weight=Decimal("0.1"),
    )
    neg = NegotiationPermissions()

    mec1 = MEC(
        mec_id="MEC-1", version=1, store_id="S1", scope=MECScope.STORE, scope_value=None,
        hard_constraints=hc1, objectives=obj, negotiation=neg, created_at="2026-01-01T00:00:00Z", hash=""
    )
    mec2 = MEC(
        mec_id="MEC-1", version=1, store_id="S1", scope=MECScope.STORE, scope_value=None,
        hard_constraints=hc2, objectives=obj, negotiation=neg, created_at="2026-01-01T00:00:00Z", hash=""
    )

    assert compute_hash(mec1) != compute_hash(mec2)


def test_validate_objectives_passes():
    obj = EconomicObjectives(
        margin_weight=Decimal("0.4"),
        conversion_weight=Decimal("0.3"),
        aov_weight=Decimal("0.2"),
        inventory_velocity_weight=Decimal("0.1"),
    )
    validate_objectives(obj)  # Should not raise


def test_validate_objectives_raises():
    obj = EconomicObjectives(
        margin_weight=Decimal("0.5"),
        conversion_weight=Decimal("0.3"),
        aov_weight=Decimal("0.2"),
        inventory_velocity_weight=Decimal("0.1"),
    )
    with pytest.raises(ValueError, match="Objective weights must sum to 1.0"):
        validate_objectives(obj)


def test_mec_is_frozen():
    hc = HardConstraints(
        min_margin_pct=Decimal("0.15"),
        max_discount_pct_per_sku=10,
        max_cart_discount_pct=15,
        max_txn_without_human_inr=5000,
    )
    obj = EconomicObjectives(
        margin_weight=Decimal("0.4"),
        conversion_weight=Decimal("0.3"),
        aov_weight=Decimal("0.2"),
        inventory_velocity_weight=Decimal("0.1"),
    )
    neg = NegotiationPermissions()

    mec = MEC(
        mec_id="MEC-1", version=1, store_id="S1", scope=MECScope.STORE, scope_value=None,
        hard_constraints=hc, objectives=obj, negotiation=neg, created_at="2026-01-01T00:00:00Z", hash=""
    )

    with pytest.raises(FrozenInstanceError):
        mec.version = 2


def test_mec_scope_enum():
    assert MECScope.STORE.value == "STORE"
    assert MECScope.CATEGORY.value == "CATEGORY"
    assert MECScope.SKU.value == "SKU"
    assert MECScope.CAMPAIGN.value == "CAMPAIGN"
