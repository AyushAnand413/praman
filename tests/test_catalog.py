"""Catalog loading, validation, and the in-memory cache."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from settings import FLOOR_PRICE_COST_MULTIPLIER, MAX_DISCOUNT_PCT_PER_SKU
from store import catalog as catalog_module
from store.catalog import CatalogError, cache, load_catalog_file


def _write_catalog(tmp_path, mutate):
    raw = load_catalog_file()
    mutate(raw)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_catalog_has_fourteen_skus():
    raw = load_catalog_file()
    assert len(raw["products"]) == 14
    assert len(raw["product_private"]) == 14


def test_cache_holds_all_fourteen(db):
    assert len(cache) == 14
    assert len(cache.all_public()) == 14


def test_catalog_query_does_not_touch_the_database(db, monkeypatch):
    """All 14 SKUs live in memory; a catalog read must not hit SQLite."""
    def explode():
        raise AssertionError("catalog served from memory must not open a connection")

    monkeypatch.setattr(catalog_module, "get_connection", explode)
    assert len(cache.all_public()) == 14
    assert cache.public("AT-PRO-BLK")["list_price_inr"] == 4999


def test_public_view_is_a_copy(db):
    first = cache.public("AT-PRO-BLK")
    first["list_price_inr"] = 1
    assert cache.public("AT-PRO-BLK")["list_price_inr"] == 4999


def test_missing_sku_returns_none(db):
    assert cache.public("NOT-A-SKU") is None
    assert cache.private("NOT-A-SKU") is None


# ── the validator refuses bad data rather than importing it ────────────────


def test_wrong_sku_count_is_rejected(tmp_path):
    path = _write_catalog(tmp_path, lambda raw: raw["products"].pop())
    with pytest.raises(CatalogError, match="expected 14"):
        load_catalog_file(path)


def test_extra_public_field_is_rejected(tmp_path):
    """A new public field is a decision about what buyers see."""
    def add_private_looking_field(raw):
        raw["products"][0]["cost_inr"] = 3299

    path = _write_catalog(tmp_path, add_private_looking_field)
    with pytest.raises(CatalogError, match="non-public fields"):
        load_catalog_file(path)


def test_join_mismatch_is_rejected(tmp_path):
    path = _write_catalog(tmp_path, lambda raw: raw["product_private"].pop())
    with pytest.raises(CatalogError):
        load_catalog_file(path)


def test_dangling_tier_up_reference_is_rejected(tmp_path):
    def break_ref(raw):
        raw["product_private"][0]["tier_up_sku"] = "AT-GHOST"

    path = _write_catalog(tmp_path, break_ref)
    with pytest.raises(CatalogError, match="not a real SKU"):
        load_catalog_file(path)


def test_dangling_attach_candidate_is_rejected(tmp_path):
    def break_ref(raw):
        raw["product_private"][0]["attach_candidates"] = [
            {"sku": "AT-GHOST", "attach_rate": 0.5, "margin_pct": 10}
        ]

    path = _write_catalog(tmp_path, break_ref)
    with pytest.raises(CatalogError, match="not a real SKU"):
        load_catalog_file(path)


# ── economics the policy kernel relies on ──────────────────────────────────


def test_every_floor_price_clears_the_cost_multiplier(db):
    """Bound #3: floor = cost x 1.20. Seed data must not violate its own bound."""
    for product in cache.all_public():
        private = cache.private(product["sku"])
        minimum = Decimal(private["cost_inr"]) * FLOOR_PRICE_COST_MULTIPLIER
        assert Decimal(private["floor_price_inr"]) >= minimum, product["sku"]


def test_no_sku_permits_more_discount_than_bound_one(db):
    for product in cache.all_public():
        assert cache.private(product["sku"])["max_discount_pct"] <= MAX_DISCOUNT_PCT_PER_SKU


def test_max_discount_never_breaches_the_floor(db):
    """A SKU discounted to its cap must still clear its floor price."""
    for product in cache.all_public():
        private = cache.private(product["sku"])
        floor_after_max = product["list_price_inr"] * (100 - private["max_discount_pct"]) / 100
        assert floor_after_max >= private["floor_price_inr"], product["sku"]


def test_out_of_stock_fixture_exists(db):
    """The stock bound needs a real out-of-stock SKU to prove bound #7 fires."""
    zero_stock = [p["sku"] for p in cache.all_public() if p["stock_qty"] == 0]
    assert zero_stock, "no out-of-stock SKU seeded; bound #7 would be untestable"
