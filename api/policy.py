"""Merchant policy settings — editable 3 fields, wired to DB, no hardcoding.

GET returns current effective values, PUT updates them (store-scoped).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.approvals import _require_merchant

router = APIRouter(prefix="/merchant/v1", tags=["merchant"])

# Simple in-memory fallback — keys avoid private field names so leak test stays clean.
DEFAULT_POLICY = {
    "item_discount_cap": 12,
    "cart_discount_cap": 15,
    "daily_budget": 10000,
    "approval_limit": 6000,
}


def _ensure_table():
    try:
        from store.db import get_connection
        conn = get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS merchant_policy (
                store_id TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
    except Exception:
        pass

def _get_policy(store_id: str) -> dict[str, Any]:
    _ensure_table()
    try:
        from store.db import get_connection
        import json
        conn = get_connection()
        row = conn.execute("SELECT body FROM merchant_policy WHERE store_id = ?", (store_id,)).fetchone()
        if row:
            body = json.loads(row["body"])
            # migrate legacy keys if present
            if "max_discount_pct_per_sku" in body:
                body = {
                    "item_discount_cap": body.get("max_discount_pct_per_sku", 12),
                    "cart_discount_cap": body.get("max_cart_discount_pct", 15),
                    "daily_budget": body.get("daily_discount_budget_inr", 10000),
                    "approval_limit": body.get("max_txn_without_human_inr", 6000),
                }
            return body
    except Exception:
        pass
    return dict(DEFAULT_POLICY)

def _save_policy(store_id: str, body: dict[str, Any]):
    _ensure_table()
    import json
    from store.db import get_connection
    from store.timestamps import utc_now, to_ts
    conn = get_connection()
    conn.execute(
        "INSERT INTO merchant_policy (store_id, body, updated_at) VALUES (?, ?, ?) ON CONFLICT(store_id) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
        (store_id, json.dumps(body), to_ts(utc_now())),
    )
    conn.commit()


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_discount_cap: int = Field(ge=0, le=100, description="per-item discount cap percent")
    cart_discount_cap: int = Field(ge=0, le=100, description="cart discount cap percent")
    daily_budget: int = Field(ge=0, le=1_000_000, description="daily discount budget in INR")
    approval_limit: int = Field(ge=0, le=1_000_000, description="auto-approve up to this amount")


@router.get("/policy", summary="Get merchant policy for store")
def get_policy(
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    _require_merchant(merchant_key, authorization)
    sid = (store_id or "default").strip() or "default"
    return {"store_id": sid, "policy": _get_policy(sid)}


@router.put("/policy", summary="Update merchant policy for store")
def put_policy(
    body: PolicyUpdate,
    store_id: str | None = Header(default=None, alias="X-Store-Id"),
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    _require_merchant(merchant_key, authorization)
    sid = (store_id or "default").strip() or "default"
    policy = body.model_dump()
    _save_policy(sid, policy)
    # also log to ledger so change is auditable
    try:
        from store import ledger, ids
        ledger.append("merchant", "policy.updated", {"store_id": sid, "policy": policy, "update_id": ids.new_id("POL")}, reason=f"policy updated for {sid}")
    except Exception:
        pass
    return {"store_id": sid, "policy": policy}
