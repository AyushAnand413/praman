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
from store.db import get_connection, transaction
from store.tenancy import configured_stores, current_store, set_current, resolve
from store.timestamps import utc_now, to_ts
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


def _merchant_id_from_token(authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            from store.auth import get_by_token
            row = get_by_token(token)
            if row:
                return row["merchant_id"]
        except Exception:
            pass
    return None


def _record_merchant_store(merchant_id: str, store_id: str, platform: str, domain: str | None = None, url: str | None = None):
    conn = get_connection()
    now = to_ts(utc_now())
    try:
        with transaction(conn):
            conn.execute(
                """INSERT INTO merchant_stores (merchant_id, store_id, platform, domain, url, connected_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (merchant_id, store_id, platform) DO UPDATE SET domain=excluded.domain, url=excluded.url, connected_at=excluded.connected_at""",
                (merchant_id, store_id, platform, domain, url, now),
            )
    except Exception:
        try:
            conn._pg.rollback()
        except Exception:
            pass


def _create_sync_job(store_id: str, platform: str, merchant_id: str | None) -> str:
    job_id = ids.new_id("SYNC")
    conn = get_connection()
    now = to_ts(utc_now())
    with transaction(conn):
        conn.execute(
            "INSERT INTO sync_jobs (job_id, merchant_id, store_id, platform, status, imported, skipped, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, merchant_id, store_id, platform, "pending", 0, 0, now, now),
        )
    return job_id


def _update_sync_job(job_id: str, status: str, imported: int = 0, skipped: int = 0, error: str | None = None):
    conn = get_connection()
    now = to_ts(utc_now())
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE sync_jobs SET status=?, imported=?, skipped=?, error=?, updated_at=? WHERE job_id=?",
                (status, imported, skipped, error, now, job_id),
            )
    except Exception:
        try:
            conn._pg.rollback()
        except Exception:
            pass


def _do_shopify_sync(job_id: str, store_id: str, domain: str, token: str):
    _update_sync_job(job_id, "running")
    try:
        base_url = f"https://{domain}/admin/api/2024-10"
        client = shopify_integration.ShopifyClient(access_token=token, base_url=base_url)
        result = shopify_integration.sync_catalog(client)
        _update_sync_job(job_id, "done", imported=result["imported"], skipped=result["skipped"])
        ledger.append(
            "merchant",
            "catalog.synced",
            {"sync_id": job_id, "source": "shopify", "store_id": store_id, "domain": domain, "imported": result["imported"], "skipped": result["skipped"]},
            reason=f"Shopify catalog sync for {store_id}: {result['imported']} imported",
        )
    except Exception as exc:
        _update_sync_job(job_id, "failed", error=str(exc)[:500])
        ledger.append(
            "merchant",
            "catalog.synced",
            {"sync_id": job_id, "source": "shopify", "store_id": store_id, "domain": domain, "imported": 0, "error": str(exc)[:200]},
            reason=f"Shopify sync failed for {store_id}: {exc}",
        )


@router.get("/stores", summary="List configured stores")
def list_stores(
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    # persisted stores for this merchant
    merchant_id = _merchant_id_from_token(authorization)
    persisted = []
    if merchant_id:
        try:
            conn = get_connection()
            rows = conn.execute("SELECT store_id, platform, domain, url, connected_at FROM merchant_stores WHERE merchant_id=?", (merchant_id,)).fetchall()
            persisted = [dict(r) for r in rows]
        except Exception:
            pass
    stores = list(configured_stores()) if settings.PRAMAN_STORES else ["default"]
    # merge persisted store_ids
    for p in persisted:
        if p["store_id"] not in stores:
            stores.append(p["store_id"])
    counts = {}
    for sid in stores:
        try:
            set_current(sid)
            counts[sid] = len(catalog.cache.all_public())
        except Exception:
            counts[sid] = 0
    return {"stores": stores, "catalog_counts": counts, "current": current_store(), "connected": persisted}


@router.get("/stores/sync/{job_id}", summary="Poll Shopify sync job")
def sync_status(
    job_id: str,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    conn = get_connection()
    row = conn.execute("SELECT * FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "job not found"})
    return dict(row)


@router.post("/stores/connect/shopify", summary="Connect Shopify for a store", status_code=202)
async def connect_shopify(
    body: ShopifyConnect,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    sid = _use_store(store_id)
    merchant_id = _merchant_id_from_token(authorization)
    # Validate creds quickly — ShopifyClient.__init__ only raises if token is None
    # and no env var is set; it does NOT connect to Shopify yet (lazy).
    base_url = f"https://{body.domain}/admin/api/2024-10" if body.domain else None
    try:
        shopify_integration.ShopifyClient(access_token=body.token, base_url=base_url)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "shopify_unconfigured", "message": str(exc)}) from exc

    job_id = _create_sync_job(sid, "shopify", merchant_id)
    if merchant_id:
        _record_merchant_store(merchant_id, sid, "shopify", domain=body.domain)

    # Run sync in a thread via asyncio so the response (202) is sent immediately.
    # NOTE: On Vercel serverless, FastAPI BackgroundTasks are killed after response
    # send. asyncio.ensure_future + run_in_executor lets the event loop keep the
    # thread alive as long as the function container is up (~30s Pro, ~10s Hobby).
    # For 100-product Shopify stores (~46s), the container may still be killed;
    # the job row will stay in "running" state and the UI poll will show "pending".
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    asyncio.ensure_future(
        loop.run_in_executor(executor, _do_shopify_sync, job_id, sid, body.domain, body.token)
    )

    return {"status": "accepted", "store_id": sid, "job_id": job_id, "poll_url": f"/merchant/v1/stores/sync/{job_id}"}


@router.post("/stores/connect/woocommerce", summary="Connect WooCommerce (mocked)")
def connect_woo(
    body: WooConnect,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    sid = _use_store(store_id)
    merchant_id = _merchant_id_from_token(authorization)
    if merchant_id:
        _record_merchant_store(merchant_id, sid, "woocommerce", url=body.url)
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
    merchant_id = _merchant_id_from_token(authorization)
    if merchant_id:
        _record_merchant_store(merchant_id, sid, "custom")
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
