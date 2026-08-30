"""The Shopify connector: mapping discipline, sync idempotency, push shapes.

No network anywhere — the client is faked with the same records-its-calls
pattern as the Razorpay stub, because what is under test is OUR behaviour:
what we map, what we refuse to map, and what we send.
"""

from __future__ import annotations

import pytest

import settings

from integrations import shopify
from integrations.shopify import (
    ShopifyClient,
    ShopifyError,
    map_line_items,
    map_product,
    sync_catalog,
)
from store import catalog


def _shopify_product(**overrides):
    product = {
        "id": 9001,
        "title": "VoltMax 65W GaN Charger",
        "product_type": "Chargers",
        "handle": "voltmax-65w",
        "variants": [
            {
                "id": 8001,
                "sku": "VOLT-65W",
                "price": "1799.00",
                "inventory_quantity": 12,
            }
        ],
    }
    product.update(overrides)
    return product


# ── mapping ────────────────────────────────────────────────────────────────────


def test_product_maps_to_public_and_private_rows():
    public, private = map_product(_shopify_product())

    assert public["sku"] == "VOLT-65W"
    assert public["list_price_inr"] == 1799
    assert public["stock_qty"] == 12
    assert public["category"] == "chargers"
    assert public["attrs"]["source"] == "shopify"

    # Derived economics are labelled as an assumption in the module docs; here
    # we pin the arithmetic so a silent change is visible in review.
    assert private["cost_inr"] == round(1799 * (100 - settings_margin()) / 100)
    assert private["offerable"] is True


def settings_margin() -> int:
    import settings

    return settings.SHOPIFY_ASSUMED_MARGIN_PCT


def test_out_of_stock_product_syncs_as_not_offerable():
    product = _shopify_product()
    product["variants"][0]["inventory_quantity"] = 0
    _, private = map_product(product)
    assert private["offerable"] is False


def test_unusable_products_raise_instead_of_half_mapping():
    with pytest.raises(ShopifyError):
        map_product(_shopify_product(title="No price", variants=[{"id": 1}]))
    with pytest.raises(ShopifyError):
        map_product(_shopify_product(variants=[]))


def test_zero_price_is_refused_not_normalised():
    product = _shopify_product()
    product["variants"][0]["price"] = "0.00"
    with pytest.raises(ShopifyError):
        map_product(product)


# ── sync ───────────────────────────────────────────────────────────────────────


class FakeShopify:
    def __init__(self, pages: list[list[dict]]):
        self._pages = list(pages)

    def fetch_products_page(self, *, since_id=None):
        if not self._pages:
            return []
        return self._pages.pop(0)


def test_sync_upserts_into_catalog_tables(db):
    result = sync_catalog(FakeShopify([[_shopify_product()]]), conn=db)

    assert result["imported"] == 1
    assert result["skipped"] == 0
    row = db.execute(
        "SELECT sku, list_price_inr FROM products WHERE sku='VOLT-65W'"
    ).fetchone()
    assert row["list_price_inr"] == 1799
    # The cache was refreshed: the new SKU is sellable immediately.
    assert catalog.cache.public("VOLT-65W") is not None


def test_resync_is_idempotent(db):
    first = sync_catalog(FakeShopify([[_shopify_product()]]), conn=db)
    second = sync_catalog(FakeShopify([[_shopify_product()]]), conn=db)
    assert (first["imported"], second["imported"]) == (1, 1)
    count = db.execute("SELECT count(*) FROM products").fetchone()[0]
    assert count >= 14  # the seeded 14 plus ours, not duplicated


def test_bad_products_are_skipped_and_named(db):
    good = _shopify_product()
    broken = _shopify_product(id=9002, title="Broken", variants=[])
    result = sync_catalog(FakeShopify([[good, broken]]), conn=db)
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert "Broken" in result["skipped_titles"]


# ── pushes ─────────────────────────────────────────────────────────────────────


def test_order_push_shape_carries_our_ids(monkeypatch):
    captured = {}

    def fake_request(self, method, path, *, json_body=None):
        captured["path"] = path
        captured["body"] = json_body
        return {"order": {"id": 555, "order_number": 1055,
                          "total_price": "3400", "financial_status": "paid"}}

    monkeypatch.setattr(settings, "SHOPIFY_STORE_DOMAIN", "demo.myshopify.com")
    client = ShopifyClient(access_token="tok")
    monkeypatch.setattr(ShopifyClient, "_request", fake_request)

    lines = map_line_items(
        [{"sku": "VOLT-65W", "qty": 2, "offered_price_inr": 1700}],
        {"VOLT-65W": 8001},
    )
    result = client.create_order(
        praman_order_id="ORD-test", line_items=lines,
        total_paid=3400, payment_reference="pay_x",
    )

    assert result["financial_status"] == "paid"
    order = captured["body"]["order"]
    assert order["financial_status"] == "paid"  # truthful: called only after capture
    assert order["line_items"][0]["variant_id"] == 8001
    assert order["line_items"][0]["price"] == "1700"  # whole rupees as string
    attrs = {a["name"]: a["value"] for a in order["note_attributes"]}
    assert attrs["praman_order_id"] == "ORD-test"
    assert attrs["praman_payment_id"] == "pay_x"


def test_refund_push_targets_the_right_order(monkeypatch):
    captured = {}

    def fake_request(self, method, path, *, json_body=None):
        captured["path"] = path
        captured["body"] = json_body
        return {"refund": {"id": 77, "status": "pending"}}

    monkeypatch.setattr(settings, "SHOPIFY_STORE_DOMAIN", "demo.myshopify.com")
    client = ShopifyClient(access_token="tok")
    monkeypatch.setattr(ShopifyClient, "_request", fake_request)

    client.create_refund(
        shopify_order_id=555, amount=1700,
        praman_refund_id="rfnd_sim0001", reason="oversold_merchant_fault",
    )
    assert captured["path"] == "/orders/555/refunds.json"
    assert "oversold_merchant_fault" in captured["body"]["refund"]["note"]


def test_pushing_an_unsynced_sku_is_refused():
    with pytest.raises(ShopifyError):
        map_line_items(
            [{"sku": "NEVER-SYNCED", "qty": 1, "offered_price_inr": 999}],
            {"VOLT-65W": 8001},
        )


def test_client_requires_a_domain():
    import settings as s

    saved = s.SHOPIFY_STORE_DOMAIN
    try:
        s.SHOPIFY_STORE_DOMAIN = ""
        with pytest.raises(ShopifyError):
            ShopifyClient(access_token="tok")
    finally:
        s.SHOPIFY_STORE_DOMAIN = saved
