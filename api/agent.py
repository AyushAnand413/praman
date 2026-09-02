"""The buyer agent's rail: browse, get an offer, buy, poll.

This is the surface an autonomous buyer talks to. Four properties of the request
shapes carry most of the security:

**No amount field on checkout.** The body names an offer and an option. The price
comes from the stored offer row, so there is no field an agent could tamper with.
A request that tried to send a price would be rejected as an unknown field rather
than silently honoured.

**The idempotency key is required by the schema.** It is checked again inside the
kernel as bound 9, because a validation layer can be bypassed by a new caller and
a bound cannot. Two checks that agree is the intent, not redundancy to remove.

**Browsing is free and therefore rate-limited.** The catalog query needs no
mandate — an agent should be able to find out what a store sells without
authorising anything — which is exactly why it is the one endpoint here with a
request limit on it.

**The offer endpoint takes free text and treats it as data.** `need` is written by
an untrusted caller and reaches an LLM. Nothing downstream of it is allowed to
act on instructions found inside it; the model may only choose SKUs, quantities,
and discounts, and every one of those is re-checked by the kernel afterwards.

Errors are mapped to status codes, never swallowed. A refused request returns the
kernel's own reason and a link to the ledger entry that recorded the refusal, so
an agent can tell "you are not allowed to do this" from "something broke".
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import settings
from api import ratelimit
from kernel import approvals as approvals_kernel
from kernel import checkout as checkout_kernel
from kernel import idempotency
from kernel import offer as offer_kernel
from kernel import search
from store import catalog, ledger, orders

router = APIRouter(prefix="/agent/v1", tags=["agent"])

#: Most SKUs one catalog query will return. Re-exported from settings so this
#: module and api/mcp.py share one ceiling; raise it by env, not by edit.
MAX_CATALOG_RESULTS = settings.MAX_CATALOG_RESULTS


class CatalogRequest(BaseModel):
    """A product search. No mandate, no session, no cost.

    A POST rather than a GET because `need` is free text that may be long, and
    because an agent should not have its shopping request end up in a proxy's
    access log by way of a query string.
    """

    model_config = ConfigDict(extra="forbid")

    need: str = Field(
        default="",
        max_length=2_000,
        description="What the buyer is looking for, in their own words",
    )
    budget_inr: int | None = Field(
        default=None, ge=0, description="Upper bound on list price, whole rupees"
    )
    category: str | None = Field(default=None, max_length=64)
    agent_id: str | None = Field(
        default=None,
        max_length=128,
        description="Optional. Used only to key the request limit.",
    )
    limit: int = Field(default=MAX_CATALOG_RESULTS, ge=1, le=MAX_CATALOG_RESULTS)


class OfferRequest(BaseModel):
    """Ask the store to make an offer.

    `need` is untrusted free text on its way to a model. There is deliberately no
    field here for a price, a discount, or a policy instruction: an agent states
    what it wants and what it can spend, and the store decides the terms.
    """

    model_config = ConfigDict(extra="forbid")

    need: str = Field(min_length=1, max_length=2_000)
    agent_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(
        default=None,
        description=(
            "Continue an existing session. Offers are counted per session, so "
            "omitting this starts a fresh count."
        ),
    )
    qty: int = Field(default=1, ge=1, le=50)
    base_sku: str | None = Field(default=None, max_length=32)
    category: str | None = Field(default=None, max_length=64)
    budget_inr: int | None = Field(default=None, ge=0)
    delivery: str | None = Field(default=None, max_length=64)


class CheckoutRequest(BaseModel):
    """What a buyer agent sends to buy something.

    `extra="forbid"` is deliberate. An agent that sends `amount_inr` gets a 422
    telling it the field does not exist, rather than having it quietly ignored —
    a silently dropped price field is indistinguishable to the sender from an
    accepted one.
    """

    model_config = ConfigDict(extra="forbid")

    offer_id: str = Field(min_length=1, description="The offer being accepted")
    option_id: str = Field(min_length=1, description="Which option within it")
    agent_id: str = Field(min_length=1, description="Stable id of the buyer agent")
    mandate: str | None = Field(
        default=None,
        description="Signed mandate token, required above the mandate threshold",
    )
    payment_id: str | None = Field(
        default=None,
        description=(
            "An already-authorized Razorpay payment id. Omitted on the normal "
            "path, where this endpoint returns a gateway order to complete first."
        ),
    )


class SettleRequest(BaseModel):
    """Capture a payment the buyer completed against a gateway order."""

    model_config = ConfigDict(extra="forbid")

    payment_id: str = Field(min_length=1)


@router.post("/catalog", summary="Find products, no mandate required")
def catalog_query(body: CatalogRequest) -> dict[str, Any]:
    """Rank the catalog against a stated need and return public rows only.

    Every row goes through `catalog.to_public`, which builds a new dict of exactly
    the public fields rather than deleting private ones from the stored row. That
    direction matters: a serializer that removes known-bad keys leaks any new
    private column the day it is added, while one that copies known-good keys
    cannot.

    Ranking is `kernel.search`, the same deterministic scorer the fallback
    proposer uses. No model is called here — a product search does not need one,
    and the latency budget for this endpoint is 200ms.
    """
    started = time.perf_counter()
    limit_key = body.agent_id or "anonymous"
    try:
        remaining = ratelimit.catalog_limiter.check(limit_key)
    except ratelimit.RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": str(exc),
                "retry_after_seconds": exc.retry_after_seconds,
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    rows = [catalog.to_public(row) for row in catalog.cache.all_public()]
    if body.category:
        narrowed = [r for r in rows if r.get("category") == body.category]
        # An unmatched category is a hint that did not apply, not a filter that
        # empties the response. Same choice `envelope.pick_base` makes, for the
        # same reason: answering the request beats refusing it on a spelling.
        rows = narrowed or rows
    if body.budget_inr is not None:
        rows = [r for r in rows if int(r["list_price_inr"]) <= body.budget_inr]

    query = body.need.strip()
    if query:
        scored = [(search.relevance_for_row(r, query), r) for r in rows]
        matched = [pair for pair in scored if pair[0] > 0]
        # Fall back to the unranked list when nothing matches, rather than
        # returning nothing. An agent that gets an empty result cannot tell "you
        # do not sell this" from "your search is bad at synonyms".
        chosen = matched or [(0, r) for r in rows]
        chosen.sort(key=lambda pair: (-pair[0], int(pair[1]["list_price_inr"]), pair[1]["sku"]))
        rows = [r for _, r in chosen]
    else:
        rows.sort(key=lambda r: (int(r["list_price_inr"]), r["sku"]))

    results = rows[: body.limit]
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Sample catalog.query ledger writes to avoid bloat: every search was
    # appending a row and making dashboard safety scans (300 rows) slower over
    # time. Keep 10% by default, or set PRAMAN_CATALOG_LOG_SAMPLE env.
    import os as _os, random as _rnd
    try:
        _sample = float(_os.environ.get("PRAMAN_CATALOG_LOG_SAMPLE", "0.1"))
    except Exception:
        _sample = 0.1
    if _rnd.random() < _sample:
        try:
            ledger.append(
                "buyer_agent",
                "catalog.query",
                {
                    "agent_id": body.agent_id,
                    "need_chars": len(query),
                    "need_sha256": offer_kernel.need_fingerprint(query) if query else None,
                    "category": body.category,
                    "budget_inr": body.budget_inr,
                    "results": len(results),
                    "matched": len(rows),
                    "latency_ms": elapsed_ms,
                },
            )
        except Exception:
            pass

    return {
        "results": results,
        "count": len(results),
        "matched": len(rows),
        "requests_remaining": remaining,
        "latency_ms": elapsed_ms,
        "latency_budget_ms": settings.LATENCY_BUDGETS_MS["catalog"],
        "mandate_required": False,
    }


@router.post("/offer", summary="Ask for an offer on a stated need")
def offer(body: OfferRequest) -> dict[str, Any]:
    """Run the offer flow: model proposes, kernel bounds it, receipt signs it.

    No mandate is verified here, and that is not an omission. A mandate is
    single-use and is consumed at checkout; verifying one now would spend it on a
    quote. What the agent gets instead is the gate tier on each option, which is
    the store saying in advance whether buying it will need a mandate or a human.

    A refusal is a real answer and is returned as one, with the bound numbers that
    produced it and a ledger link. An agent that is told "bound 1 refused this"
    can adjust; an agent handed a 500 cannot.
    """
    started = time.perf_counter()
    try:
        issued = offer_kernel.build_offer(
            need=body.need,
            agent_id=body.agent_id,
            session_id=body.session_id,
            qty=body.qty,
            base_sku=body.base_sku,
            category=body.category,
            budget_inr=body.budget_inr,
            delivery=body.delivery,
        )
    except offer_kernel.OfferRefused as exc:
        raise HTTPException(
            status_code=exc.http_status, detail=exc.as_payload()
        ) from exc
    payload = issued.as_payload()
    payload["latency_ms"] = int((time.perf_counter() - started) * 1000)
    payload["latency_budget_ms"] = settings.LATENCY_BUDGETS_MS["offer"]
    return payload


@router.post("/checkout", summary="Buy an offered option")
def checkout(
    body: CheckoutRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Required. Retrying with the same key cannot double-charge.",
    ),
) -> dict[str, Any]:
    try:
        result = checkout_kernel.checkout(
            offer_id=body.offer_id,
            option_id=body.option_id,
            idempotency_key=idempotency_key,
            agent_id=body.agent_id,
            mandate_token=body.mandate,
            payment_id=body.payment_id,
        )
    except checkout_kernel.OversoldFault as exc:
        # The compensation has already run: money refunded, order voided for
        # fulfilment, SKU self-healed, ledger complete. The agent gets the
        # structured failure — fault, refund, remedy, retry-safe — not a bare
        # error.
        raise HTTPException(status_code=exc.http_status, detail=exc.payload) from exc
    except checkout_kernel.CheckoutError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except idempotency.FingerprintMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_key_reused", "message": str(exc)},
        ) from exc
    except idempotency.RequestInFlight as exc:
        # 409, not 202. The outcome is genuinely unknown and retrying is not
        # safe, so the agent is told to poll rather than encouraged to try again.
        raise HTTPException(
            status_code=409,
            detail={"code": "request_in_flight", "message": str(exc)},
        ) from exc
    return result.as_payload()


@router.post("/order/{order_id}/settle", summary="Capture a completed payment")
def settle(order_id: str, body: SettleRequest) -> dict[str, Any]:
    """Capture the payment the buyer authorised against this order's gateway order.

    Separate from checkout because a Razorpay test account generally cannot
    create a payment server-side: the buyer completes Checkout in a browser, and
    the resulting payment id arrives here.
    """
    try:
        order = checkout_kernel.settle(order_id, payment_id=body.payment_id)
    except orders.OrderNotFound as exc:
        raise HTTPException(
            status_code=404, detail={"code": "order_not_found", "message": str(exc)}
        ) from exc
    except checkout_kernel.OversoldFault as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.payload) from exc
    except checkout_kernel.CheckoutError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {
        "order_id": order_id,
        "state": order["state"],
        "amount_inr": int(order["amount_inr"]),
        "currency": "INR",
        "razorpay_payment_id": order["razorpay_payment_id"],
        "audit_url": checkout_kernel.audit_url_for(order_id),
    }


@router.get("/order/{order_id}", summary="Poll an order, held or otherwise")
def order_status(order_id: str) -> dict[str, Any]:
    """The poll URL a Tier-2 hold hands back.

    A held order reports `pending_merchant_approval` for as long as the merchant
    takes. Nothing here ever flips it to approved on its own.
    """
    try:
        return approvals_kernel.order_status(order_id)
    except orders.OrderNotFound as exc:
        raise HTTPException(
            status_code=404, detail={"code": "order_not_found", "message": str(exc)}
        ) from exc
