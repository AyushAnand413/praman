"""Store connect + list — thin glue over integrations and catalog.

No policy. Just plumbing. Scoped by store_id.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.approvals import _require_merchant
from integrations import shopify as shopify_integration
from store import catalog, ids, ledger
from store.tenancy import configured_stores, current_store, set_current, resolve
import settings

router = APIRouter(prefix="/merchant/v1", tags=["merchant"])


class ShopifyConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(description="e.g. my-store.myshopify.com")
    token: str = Field(description="Admin token shpat_...")


class WooConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="https://myshop.in")
    key: str = Field(description="ck_...")
    secret: str = Field(description="cs_...")


class CustomConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="list of {sku,title,price,stock,category}")
    csv_url: str | None = None


def _use_store(store_id: str | None) -> str:
    if store_id:
        try:
            resolved = resolve(store_id)
            set_current(resolved)
            return resolved
        except Exception:
            pass
    set_current("default")
    return "default"


@router.get("/stores", summary="List configured stores")
def list_stores(
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    stores = list(configured_stores()) if settings.PRAMAN_STORES else ["default"]
    counts = {}
    for sid in stores:
        try:
            set_current(sid)
            counts[sid] = len(catalog.cache.all_public())
        except Exception:
            counts[sid] = 0
    return {"stores": stores, "catalog_counts": counts, "current": current_store()}


@router.post("/stores/connect/shopify", summary="Connect Shopify for a store")
async def connect_shopify(
    body: ShopifyConnect,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    sid = _use_store(store_id)
    # build client with supplied creds, don't rely on env
    base_url = f"https://{body.domain}/admin/api/2024-10" if body.domain else None
    try:
        client = shopify_integration.ShopifyClient(access_token=body.token, base_url=base_url)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "shopify_unconfigured", "message": str(exc)}) from exc
    # run sync in thread pool so event loop stays responsive - shopify httpx is sync blocking (46s for 100 products)
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    try:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = await loop.run_in_executor(pool, lambda: shopify_integration.sync_catalog(client))
    except shopify_integration.ShopifyError as exc:
        raise HTTPException(status_code=502, detail={"code": "shopify_error", "message": str(exc)}) from exc
    ledger.append(
        "merchant",
        "catalog.synced",
        {"sync_id": ids.new_id("SYNC"), "source": "shopify", "store_id": sid, "domain": body.domain, "imported": result["imported"], "skipped": result["skipped"]},
        reason=f"Shopify catalog sync for {sid}: {result['imported']} imported",
    )
    return {"status": "ok", "store_id": sid, **result}


@router.post("/stores/connect/woocommerce", summary="Connect WooCommerce (mocked)")
def connect_woo(
    body: WooConnect,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    sid = _use_store(store_id)
    # mocked: pretend 12 products, 10 importable
    fake_rows = [
        {"sku": f"WOO-{i:03d}", "title": f"Woo Product {i}", "list_price_inr": 999 + i * 100, "stock_qty": 20, "category": "audio_accessories", "attrs": {}, "returns_window_days": 7}
        for i in range(1, 11)
    ]
    result = catalog.seed_database_from_rows(
        [(r, {"cost_inr": int(r["list_price_inr"] * 0.6), "floor_price_inr": int(r["list_price_inr"] * 0.8), "margin_pct": 35, "max_discount_pct": 12, "offerable": True}) for r in fake_rows]
    )
    ledger.append(
        "merchant",
        "catalog.synced",
        {"sync_id": ids.new_id("SYNC"), "source": "woocommerce", "store_id": sid, "url": body.url, "imported": 10, "skipped": 2},
        reason=f"Woo catalog sync for {sid}: 10 imported",
    )
    return {"status": "ok", "store_id": sid, "imported": 10, "skipped": 2, "source": "woocommerce"}


@router.post("/stores/connect/custom", summary="Connect custom CSV/API rows")
def connect_custom(
    body: CustomConnect,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    sid = _use_store(store_id)
    rows = body.rows[:100]
    if not rows:
        raise HTTPException(status_code=400, detail={"code": "empty_rows", "message": "provide rows[]"})
    pairs = []
    for r in rows:
        public = {
            "sku": r["sku"],
            "title": r["title"],
            "list_price_inr": int(r["list_price_inr"]),
            "stock_qty": int(r.get("stock_qty", 10)),
            "category": r.get("category", "audio_accessories"),
            "attrs": r.get("attrs", {}),
            "returns_window_days": int(r.get("returns_window_days", 7)),
        }
        private = {
            "cost_inr": int(r.get("cost_inr", int(public["list_price_inr"] * 0.6))),
            "floor_price_inr": int(r.get("floor_price_inr", int(public["list_price_inr"] * 0.8))),
            "margin_pct": int(r.get("margin_pct", 35)),
            "max_discount_pct": int(r.get("max_discount_pct", 12)),
            "offerable": True,
        }
        pairs.append((public, private))
    catalog.seed_database_from_rows(pairs)
    catalog.cache.load()
    ledger.append(
        "merchant",
        "catalog.synced",
        {"sync_id": ids.new_id("SYNC"), "source": "custom", "store_id": sid, "imported": len(pairs), "skipped": 0},
        reason=f"Custom catalog sync for {sid}: {len(pairs)} imported",
    )
    return {"status": "ok", "store_id": sid, "imported": len(pairs), "skipped": 0}
