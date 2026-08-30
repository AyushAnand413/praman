"""The merchant panel, ops routes, profit-weighted attach order, and cluster priors.

The panel is static assets served by the same process; its behaviour is the
endpoints it calls, which are tested here directly. The cluster priors are
the user's design: related stores pool ANONYMOUS category-level ratios so a
new store starts smart — without SKUs or orders ever crossing tenants.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import settings
from store import pairings, tenancy
from store.timestamps import utc_now
from vyapaari.envelope import build as build_envelope


@pytest.fixture(autouse=True)
def clean_tenant():
    tenancy.reset_current()
    yield
    tenancy.reset_current()


@pytest.fixture
def key(test_secrets):
    return test_secrets["DEMO_KEY"]


# ── panel is served ────────────────────────────────────────────────────────────


def test_panel_html_is_served(client: TestClient):
    response = client.get("/panel/")
    assert response.status_code == 200
    assert "PRAMAN" in response.text
    assert "panel.js" in response.text


def test_panel_assets_served(client: TestClient):
    assert client.get("/panel/panel.css").status_code == 200
    js = client.get("/panel/panel.js")
    assert js.status_code == 200
    # The panel speaks only the real authenticated API.
    assert "/merchant/v1/dashboard" in js.text


# ── shopify sync endpoint ──────────────────────────────────────────────────────


def test_sync_requires_merchant_key(client: TestClient):
    response = client.post("/merchant/v1/shopify/sync")
    assert response.status_code == 401


def test_sync_without_shopify_config_is_a_clean_503(client: TestClient, key, monkeypatch):
    monkeypatch.setattr(settings, "SHOPIFY_STORE_DOMAIN", "")
    response = client.post(
        "/merchant/v1/shopify/sync", headers={"X-Merchant-Key": key}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "shopify_unconfigured"


def test_sync_runs_and_ledgers(client: TestClient, key, db, monkeypatch):
    from integrations import shopify as sh

    monkeypatch.setattr(settings, "SHOPIFY_STORE_DOMAIN", "demo.myshopify.com")

    class FakeSync:
        def fetch_products_page(self, *, since_id=None):
            if since_id:
                return []
            return [
                {
                    "id": 1, "title": "Test Charger", "product_type": "chargers",
                    "handle": "test-charger",
                    "variants": [{"id": 2, "sku": "TST-CHG", "price": "999.00",
                                  "inventory_quantity": 4}],
                }
            ]

    monkeypatch.setattr(sh, "ShopifyClient", lambda **kw: FakeSync())
    response = client.post("/merchant/v1/shopify/sync", headers={"X-Merchant-Key": key})

    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 1

    from store import ledger

    events = [e.event for e in ledger.recent(20)]
    assert "catalog.synced" in events


# ── margin-weighted attach ordering (F-merchant-profit) ────────────────────────


def test_attach_candidates_are_profit_ranked_not_catalog_ordered(db):
    """Higher margin x attach_rate floats to the top of what the model sees."""
    public = [{
        "sku": "PHONE", "title": "Phone", "list_price_inr": 20000,
        "stock_qty": 5, "attrs": {}, "category": "phones",
        "returns_window_days": 7,
    }]
    private = {
        "PHONE": {
            "cost_inr": 14000, "margin_pct": 30, "floor_price_inr": 16800,
            "max_discount_pct": 12, "tier_up_sku": None, "offerable": True,
            # Declared order: cheap-cable first. Profit order: case first.
            "attach_candidates": [
                {"sku": "CABLE", "attach_rate": 0.6, "margin_pct": 10},
                {"sku": "CASE", "attach_rate": 0.3, "margin_pct": 60},
            ],
        },
        "CABLE": {"cost_inr": 80, "margin_pct": 10, "floor_price_inr": 100,
                  "max_discount_pct": 12, "attach_candidates": [],
                  "tier_up_sku": None, "offerable": True},
        "CASE": {"cost_inr": 300, "margin_pct": 60, "floor_price_inr": 400,
                 "max_discount_pct": 12, "attach_candidates": [],
                 "tier_up_sku": None, "offerable": True},
    }

    envelope = build_envelope(public, private)
    phone = next(s for s in envelope if s.sku == "PHONE")

    # CASE earns 0.60*0.3 = 18 vs CABLE's 0.10*0.6 = 6 → CASE first.
    assert [a.sku for a in phone.attach] == ["CASE", "CABLE"]
    # The private margins themselves never crossed into the envelope.
    assert not hasattr(phone.attach[0], "margin_pct")


def test_missing_margin_falls_back_to_declared_order(db):
    public = [{
        "sku": "X", "title": "X", "list_price_inr": 100, "stock_qty": 1,
        "attrs": {}, "category": "c", "returns_window_days": 7,
    }]
    private = {
        "X": {"cost_inr": 50, "margin_pct": 50, "floor_price_inr": 60,
              "max_discount_pct": 12, "tier_up_sku": None, "offerable": True,
              "attach_candidates": [
                  {"sku": "A", "attach_rate": 0.2},
                  {"sku": "B", "attach_rate": 0.9},
              ]},
        "A": {"cost_inr": 10, "margin_pct": 50, "floor_price_inr": 12,
              "max_discount_pct": 12, "attach_candidates": [],
              "tier_up_sku": None, "offerable": True},
        "B": {"cost_inr": 10, "margin_pct": 50, "floor_price_inr": 12,
              "max_discount_pct": 12, "attach_candidates": [],
              "tier_up_sku": None, "offerable": True},
    }
    envelope = build_envelope(public, private)
    # No margins on candidates → declared order preserved.
    assert [a.sku for a in envelope[0].attach] == ["A", "B"]


# ── cluster priors (related stores share anonymous category ratios) ───────────


def test_cluster_pool_records_category_ratios(db):
    now = utc_now()
    for _ in range(8):
        pairings.record_category_basket("phones", ["chargers"], cluster="electronics", now=now)
    pairings.record_category_basket("phones", [], cluster="electronics", now=now)

    priors = {p["category"]: p for p in pairings.cluster_pairs_for("phones", cluster="electronics")}
    assert priors["chargers"]["strength"] == pytest.approx(8 / 9, abs=0.01)


def test_clusters_do_not_mix(db):
    now = utc_now()
    pairings.record_category_basket("phones", ["cases"], cluster="electronics", now=now)

    assert pairings.cluster_pairs_for("phones", cluster="grocery") == []
    assert pairings.cluster_pairs_for("phones", cluster="electronics")


def test_new_store_gets_cluster_suggestions_only_through_declared_skus(
    db, monkeypatch
):
    """GadgetHub opens: electronics priors suggest its own declared companion."""
    monkeypatch.setattr(settings, "PRAMAN_STORE_CLUSTER_MAP_JSON",
                        '{"gadgethub": "electronics"}')
    monkeypatch.setattr(settings, "PRAMAN_STORES", ("voltmart", "gadgethub"))

    now = utc_now()
    # The pool learns: phones go with earbud-accessories (via any store).
    for _ in range(settings.CLUSTER_PRIOR_MIN_OWN_SAMPLES + 2):
        pairings.record_category_basket(
            "audio_accessories", ["earbud_accessories"], cluster="electronics", now=now
        )

    tenancy.set_current("gadgethub")
    suggestions = pairings.suggest_from_cluster("AT-AIR-BLK")  # young store

    # AT-CASE-01 is declared on AT-AIR-BLK AND sits in a suggested category.
    assert any(s["sku"] == "AT-CASE-01" and s["via_cluster"] == "electronics"
               for s in suggestions)


def test_old_store_does_not_need_cluster_suggestions(db, monkeypatch):
    monkeypatch.setattr(settings, "PRAMAN_STORE_CLUSTER_MAP_JSON",
                        '{"voltmart": "electronics"}')
    now = utc_now()

    for _ in range(settings.CLUSTER_PRIOR_MIN_OWN_SAMPLES + 1):
        pairings.record_order_basket(
            "AT-PRO-BLK", ["AT-CASE-01"], store_id="voltmart", now=now
        )

    # The store crossed the threshold on its OWN data: cluster priors are
    # never even consulted.
    assert pairings.suggest_from_cluster("AT-PRO-BLK", store_id="voltmart", now=now) == []


def test_settle_records_both_levels_of_learning(
    db, live_mode, fake_razorpay, make_offer, mandate_for, monkeypatch
):
    from kernel import checkout as checkout_kernel

    monkeypatch.setattr(settings, "PRAMAN_STORE_CLUSTER_MAP_JSON",
                        '{"default": "electronics"}')

    seeded = make_offer("upsell")  # base audio_accessories + two companions
    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="cluster-e2e-1",
        agent_id="agent-test",
        mandate_token=mandate_for(),
        payment_id="pay_cluster_0001",
        client_factory=lambda: fake_razorpay,
    )
    assert result.status == checkout_kernel.STATUS_CONFIRMED

    priors = pairings.cluster_pairs_for("audio_accessories", cluster="electronics")
    categories = {p["category"] for p in priors}
    assert {"earbud_accessories", "cables"} <= categories


def test_suggest_from_cluster_edge_branches(db, monkeypatch):
    """Unknown SKU, no-private-row, and sku-less candidates all degrade safely."""
    now = utc_now()
    for _ in range(settings.CLUSTER_PRIOR_MIN_OWN_SAMPLES + 2):
        pairings.record_category_basket(
            "audio_accessories", ["earbud_accessories"], cluster="electronics", now=now
        )

    # Unknown base SKU: nothing to anchor on.
    assert pairings.suggest_from_cluster("NOT-A-SKU") == []

    # A young store whose base IS known gets suggestions or an empty list
    # honestly - here the catalog has no declared candidates for AT-CBL-USBC,
    # and its category has no pool priors, so [] either way.
    assert pairings.suggest_from_cluster("AT-CBL-USBC") == []

    # Private row vanished while public row exists: degrade, don't crash.
    from store import catalog as cat

    original_private = cat.cache.private

    monkeypatch.setattr(settings, "PRAMAN_STORE_CLUSTER_MAP_JSON",
                        '{"default": "electronics"}')

    def private_none(sku):
        if sku == "AT-AIR-BLK":
            return None
        return original_private(sku)

    monkeypatch.setattr(cat.cache, "private", private_none)
    # Pool has electronics priors (recorded above), so this reaches the
    # private-row lookup and must degrade honestly.
    assert pairings.suggest_from_cluster("AT-AIR-BLK") == []


def test_suggest_skips_candidates_without_sku(db, monkeypatch):
    """A malformed attach candidate is skipped, not fatal."""
    now = utc_now()
    for _ in range(settings.CLUSTER_PRIOR_MIN_OWN_SAMPLES + 2):
        pairings.record_category_basket(
            "audio_accessories", ["earbud_accessories"], cluster="electronics", now=now
        )

    monkeypatch.setattr(settings, "PRAMAN_STORE_CLUSTER_MAP_JSON",
                        '{"default": "electronics"}')

    from store import catalog as cat

    original_private = cat.cache.private

    def private_with_junk(sku):
        row = original_private(sku)
        if sku == "AT-AIR-BLK" and row:
            row = dict(row)
            row["attach_candidates"] = [{"margin_pct": 50}, "not-a-dict",
                                        {"sku": "", "attach_rate": 0.9},
                                        {"sku": "AT-CASE-01", "attach_rate": 0.3}]
        return row

    monkeypatch.setattr(cat.cache, "private", private_with_junk)
    suggestions = pairings.suggest_from_cluster("AT-AIR-BLK")
    assert [s["sku"] for s in suggestions] == ["AT-CASE-01"]
