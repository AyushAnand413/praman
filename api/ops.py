"""Merchant ops routes — actions from the web panel that aren't approvals.

    POST /merchant/v1/shopify/sync   pull the connected Shopify catalog

Same demo-key gate as every other merchant route. A sync is a real write to
the catalog tables, so it lands on the ledger: what was imported, when, and
what was skipped is auditable like everything else.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException

import settings
from api.approvals import _require_merchant
from integrations import shopify as shopify_integration
from store import ids, ledger

router = APIRouter(prefix="/merchant/v1", tags=["merchant"])


@router.post("/shopify/sync", summary="Pull the connected Shopify catalog")
def shopify_sync(
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)

    try:
        client = shopify_integration.ShopifyClient()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "shopify_unconfigured",
                "message": (
                    "Shopify connector is not usable: set SHOPIFY_STORE_DOMAIN "
                    "and SHOPIFY_ADMIN_ACCESS_TOKEN. "
                    f"({type(exc).__name__})"
                ),
            },
        ) from exc

    try:
        result = shopify_integration.sync_catalog(client)
    except shopify_integration.ShopifyError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "shopify_error", "message": str(exc)},
        ) from exc

    ledger.append(
        "merchant",
        "catalog.synced",
        {
            "sync_id": ids.new_id("SYNC"),
            "source": "shopify",
            "store_domain": settings.SHOPIFY_STORE_DOMAIN or "(unset)",
            "imported": result["imported"],
            "skipped": result["skipped"],
            "skipped_titles": result["skipped_titles"],
        },
        reason=(
            f"Shopify catalog sync: {result['imported']} product(s) imported, "
            f"{result['skipped']} skipped as unmappable"
        ),
    )
    return {"status": "ok", **result}
