"""Merchant orders — list and detail, scoped by store.

Read-only. Every row carries store_id via tenancy context.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from api.approvals import _require_merchant
from api import events
from store import ledger, offers, orders
from store.tenancy import current_store, set_current, resolve

router = APIRouter(prefix="/merchant/v1", tags=["merchant"])


def _store_from_header(store_id: str | None) -> str:
    if store_id:
        try:
            resolved = resolve(store_id)
            set_current(resolved)
            return resolved
        except Exception:
            pass
    # fallback to default
    set_current("default")
    return "default"


def _wire(entry: Any) -> dict[str, Any]:
    """Shape one ledger entry for the wire, carrying its tone with it.

    The timeline in the console colours each step by outcome. That judgement is
    made once in api/events.py so the trail and the live feed can never disagree
    about whether the same event was a failure.
    """
    raw = entry.__dict__ if hasattr(entry, "__dict__") else entry
    return events.annotate(dict(raw))


@router.get("/orders", summary="List orders for store")
def list_orders(
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    state: str | None = Query(default=None, description="filter by state"),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    _store_from_header(store_id)
    all_orders: list[dict[str, Any]] = []
    if state:
        if state not in orders.STATES:
            raise HTTPException(status_code=400, detail={"code": "bad_state", "message": f"unknown state {state}"})
        all_orders = orders.in_state(state)
    else:
        # Single query instead of 8 in_state loops (Issue 11)
        if hasattr(orders, "list_all"):
            all_orders = orders.list_all()
        else:
            for s in orders.STATES:
                all_orders.extend(orders.in_state(s))
    all_orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    sliced = all_orders[:limit]
    # enrich with offer snapshot titles
    enriched = []
    for o in sliced:
        offer = offers.get(o["offer_id"]) if o.get("offer_id") else None
        title = ""
        if offer:
            try:
                opt = offers.option(offer, o["option_id"])
                items = opt.get("items", [])
                title = ", ".join(i.get("title", i.get("sku", "")) for i in items[:2])
            except Exception:
                title = offer.get("base_sku", "")
        enriched.append({**o, "title_summary": title})
    return {"store_id": current_store(), "count": len(enriched), "orders": enriched}


@router.get("/orders/{order_id}", summary="Order detail + audit trail")
def order_detail(
    order_id: str,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    _require_merchant(merchant_key, authorization)
    _store_from_header(store_id)
    order = orders.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "order not found"})
    offer = offers.get(order["offer_id"]) if order.get("offer_id") else None
    trail = ledger.trail(order_id)
    offer_trail = ledger.trail(order["offer_id"]) if order.get("offer_id") else []
    return {
        "store_id": current_store(),
        "order": order,
        "offer": offer,
        "trail": [_wire(e) for e in trail],
        "offer_trail": [_wire(e) for e in offer_trail],
        "audit_url": f"/audit/{order_id}",
    }
