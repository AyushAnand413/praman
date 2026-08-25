"""Merchant approval endpoints — the human end of the Tier-2 gate.

    GET  /merchant/v1/approvals              the pending queue
    POST /merchant/v1/approvals/{id}/approve release the order
    POST /merchant/v1/approvals/{id}/reject  void it
    POST /merchant/v1/approvals/{id}/counter void it and offer new terms

There is no endpoint that approves on a timer, and there is no query parameter
that skips revalidation. Those absences are the feature: a held order can only
move because a person acted, and a person's yes authorises the transaction rather
than suspending the bounds.

Authentication is a shared demo key in the `X-Merchant-Key` header. That is
deliberately thin and deliberately stated — a hackathon merchant console does not
need session management, but it does need to not be a route anyone on the internet
can POST to. The disclosure calls this a simulation rather than an auth system.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from settings import secret
from kernel import approvals as approvals_kernel
from kernel import checkout as checkout_kernel
from store import approvals as approvals_store

router = APIRouter(prefix="/merchant/v1", tags=["merchant"])

#: Who a decision is attributed to when the console does not name an operator.
DEFAULT_OPERATOR = "merchant_console"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided_by: str = Field(
        default=DEFAULT_OPERATOR,
        min_length=1,
        description="Who is making this call — recorded in the ledger verbatim",
    )
    note: str | None = Field(
        default=None, description="Shown to the buyer and stored on the approval"
    )
    payment_id: str | None = Field(
        default=None,
        description=(
            "An already-authorized payment id. Omitted normally, where approving "
            "returns a gateway order for the buyer to complete."
        ),
    )


class CounterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counter_amount_inr: int = Field(
        gt=0, description="The merchant's price, in whole rupees"
    )
    decided_by: str = Field(default=DEFAULT_OPERATOR, min_length=1)
    note: str | None = None


def _require_merchant(presented: str | None) -> None:
    """Gate the merchant routes on the demo key.

    Constant-time comparison, and a missing configured key is a 503 rather than an
    open door: failing closed on a misconfiguration is the only safe direction for
    an endpoint that can release money.
    """
    try:
        expected = secret("DEMO_KEY").reveal()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "merchant_auth_unconfigured",
                "message": (
                    "DEMO_KEY is not set, so merchant routes cannot authenticate a "
                    "caller and refuse to serve rather than serve everyone"
                ),
            },
        ) from exc
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "valid X-Merchant-Key required"},
        )


def _handle(call) -> dict[str, Any]:
    """Run a kernel decision and map its refusals onto status codes."""
    try:
        return call().as_payload()
    except approvals_kernel.ApprovalError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    except approvals_store.AlreadyDecided as exc:
        raise HTTPException(
            status_code=409, detail={"code": "already_decided", "message": str(exc)}
        ) from exc
    except checkout_kernel.CheckoutError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.get("/approvals", summary="Orders waiting on a human")
def queue(
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
) -> dict[str, Any]:
    _require_merchant(merchant_key)
    pending = approvals_kernel.pending_queue()
    return {
        "pending_count": len(pending),
        "approvals": pending,
        "note": (
            "Nothing in this queue expires into an approval. An order stays held "
            "until a person decides."
        ),
    }


@router.post("/approvals/{approval_id}/approve", summary="Release a held order")
def approve(
    approval_id: str,
    body: Decision | None = None,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
) -> dict[str, Any]:
    _require_merchant(merchant_key)
    decision = body or Decision()
    return _handle(
        lambda: approvals_kernel.approve(
            approval_id,
            decided_by=decision.decided_by,
            note=decision.note,
            payment_id=decision.payment_id,
        )
    )


@router.post("/approvals/{approval_id}/reject", summary="Void a held order")
def reject(
    approval_id: str,
    body: Decision | None = None,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
) -> dict[str, Any]:
    _require_merchant(merchant_key)
    decision = body or Decision()
    return _handle(
        lambda: approvals_kernel.reject(
            approval_id, decided_by=decision.decided_by, note=decision.note
        )
    )


@router.post("/approvals/{approval_id}/counter", summary="Offer different terms")
def counter(
    approval_id: str,
    body: CounterDecision,
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
) -> dict[str, Any]:
    _require_merchant(merchant_key)
    return _handle(
        lambda: approvals_kernel.counter(
            approval_id,
            decided_by=body.decided_by,
            counter_amount_inr=body.counter_amount_inr,
            note=body.note,
        )
    )
