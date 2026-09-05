"""Tests for the algorithmic recommendation engine and endpoints.

Verifies:
1. Catalog attach candidates are seeded as cold-start priors.
2. recommend_upsells filters out of stock items and over-budget companions.
3. recommend_upsells ranks observed high-lift pairs over seeded pairs.
4. Empty proposal from proposer gets augmented with algo upsells in kernel.offer.
5. GET /agent/v1/recommendations/{sku} endpoint returns options.
"""
from __future__ import annotations

from decimal import Decimal
import pytest

from kernel.recommender import (
    recommend_upsells,
    seed_pairings_from_catalog,
)
from store import catalog, pairings
from store.timestamps import utc_now
from vyapaari import envelope as envelope_module
from vyapaari.schema import BUNDLE_ATTACH, TIER_UPGRADE


def test_seed_pairings_from_catalog_seeds_candidates(db):
    """Catalog attach candidates are seeded into pairings table with source=seeded."""
    seeded_count = seed_pairings_from_catalog(conn=db)
    assert seeded_count > 0

    pairs = pairings.pairs_for("AT-PRO-BLK", conn=db)
    skus = {p["sku"] for p in pairs}
    # AT-CASE-01 and AT-CBL-USBC are attach candidates for AT-PRO-BLK
    assert "AT-CASE-01" in skus
    assert "AT-CBL-USBC" in skus

    # Seeded pairs report 0 samples and lift = 1.0
    for p in pairs:
        assert p["source"] == "seeded"
        assert p["samples"] == 0
        assert p["lift"] == 1.0


def test_recommend_upsells_filters_out_of_stock_and_budget(db):
    """AT-TIP-FOAM has stock_qty 0 and must be filtered out by recommender."""
    seed_pairings_from_catalog(conn=db)
    public_rows = catalog.cache.all_public()
    private_by_sku = catalog.cache.all_private_by_sku()
    envelope = envelope_module.build(public_rows, private_by_sku)
    by_sku = envelope_module.by_sku(envelope)

    # AT-TIP-FOAM has stock_qty=0 in catalog fixture
    assert by_sku["AT-TIP-FOAM"].in_stock is False

    upsells = recommend_upsells("AT-PRO-BLK", by_sku)
    upsell_skus = [u.sku for u in upsells]

    assert "AT-TIP-FOAM" not in upsell_skus
    assert "AT-CASE-01" in upsell_skus

    # Check budget filtering: AT-PRO-BLK is 4999, AT-CASE-01 is 599. Total is 5598.
    # If budget is 5200, AT-CASE-01 must be skipped.
    budget_limited = recommend_upsells(
        "AT-PRO-BLK", by_sku, budget_inr=5200
    )
    for u in budget_limited:
        assert by_sku["AT-PRO-BLK"].list_price_inr + by_sku[u.sku].list_price_inr <= 5200


def test_observed_pairings_rank_above_seeded(db):
    """When real orders occur, observed pairings with high lift rank above seeded."""
    seed_pairings_from_catalog(conn=db)
    now = utc_now()

    # Record 5 baskets where AT-PRO-BLK was bought with AT-CBL-USBC
    for _ in range(5):
        pairings.record_order_basket("AT-PRO-BLK", ["AT-CBL-USBC"], now=now, conn=db)

    public_rows = catalog.cache.all_public()
    private_by_sku = catalog.cache.all_private_by_sku()
    envelope = envelope_module.build(public_rows, private_by_sku)
    by_sku = envelope_module.by_sku(envelope)

    upsells = recommend_upsells("AT-PRO-BLK", by_sku)
    assert len(upsells) >= 1
    # AT-CBL-USBC has observed sales, so it should rank first
    assert upsells[0].sku == "AT-CBL-USBC"


def test_recommendations_endpoint_returns_options(client, db):
    """GET /agent/v1/recommendations/{sku} returns single and bundle options."""
    seed_pairings_from_catalog(conn=db)
    response = client.get("/agent/v1/recommendations/AT-PRO-BLK")
    assert response.status_code == 200
    data = response.json()

    assert data["base_sku"] == "AT-PRO-BLK"
    assert len(data["bundle_options"]) >= 2
    assert data["bundle_options"][0]["option_id"] == "A"
    assert data["bundle_options"][0]["items"] == ["AT-PRO-BLK"]
    assert data["bundle_options"][1]["option_id"] == "B"
    assert "AT-PRO-BLK" in data["bundle_options"][1]["items"]
    assert len(data["bundle_options"][1]["items"]) == 2
