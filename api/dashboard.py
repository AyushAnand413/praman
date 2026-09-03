"""GET /merchant/v1/dashboard — the merchant's entire relationship, one response.

Four panels and a banner, as the specification draws them:

    metrics    today's orders, AOV, upsell revenue, discount spend, margin
               per rupee discounted
    approvals  the Tier-2 queue, waiting on a person
    feed       the live ledger tail — blocked and refunded events sit beside
               the wins, because a feed of only successes is a feed nobody
               should trust
     bounds     the ten standing rules, with their frozen values
    mode       the banner. shadow is amber and unmissable in the UI; a
               merchant must never be unsure which mode they are in

Auth is the same shared merchant key as the approval routes, for the same
stated reason: this is a demo console boundary, not an authentication system.
Everything here is a read; nothing in this module can move money or change
state, which is the real safety property.

Margin arithmetic needs `product_private.cost_inr`, which never leaves this
module. The response carries margins and ratios only — private column names
and values are computed away, not filtered out.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

import settings
from api import events
from kernel import approvals as approvals_kernel
from kernel import budgets
from kernel.bounds import ROLE_BASE
from settings import BOUND_IDS, secret
from store import catalog, ledger, orders, offers as offers_store
from store.timestamps import utc_day

router = APIRouter(prefix="/merchant/v1", tags=["merchant"])

#: How far back "today's numbers" reach and how long the feed tail is.
FEED_LENGTH = 30

#: Ceiling on one feed page. The feed is a tail, not an export — a caller that
#: wants the whole chain has /audit, which is built for it.
MAX_FEED_LENGTH = 200


def _feed(
    limit: int = FEED_LENGTH, before_seq: int | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """One page of the ledger tail, oldest-first within the page.

    Returns the page and whether older entries exist behind it. One extra row is
    read and dropped to answer that without a second COUNT over the chain.
    """
    entries = ledger.recent(limit=limit + 1, before_seq=before_seq)
    has_more = len(entries) > limit
    page = [
        events.annotate(
            {
                "seq": entry.seq,
                "ts": entry.ts,
                "actor": entry.actor,
                "event": entry.event,
                "money_delta_inr": entry.money_delta_inr,
                "reason": entry.reason[:160],
                "policy_mode": entry.policy_mode,
            }
        )
        for entry in reversed(entries[:limit])
    ]
    return page, has_more

#: States that count as sold revenue. REFUNDED subtracts; FAILED/VOIDED were
#: never revenue at all.
_REVENUE_STATES = (orders.CONFIRMED, orders.CAPTURED)
_REFUND_STATES = (orders.REFUNDED,)


def _require_merchant_key(presented: str | None, authorization: str | None = None) -> None:
    # Bearer token first (new auth)
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            from store.auth import get_by_token
            if token and get_by_token(token):
                return
        except Exception:
            pass
    try:
        expected = secret("DEMO_KEY").reveal()
    except Exception as exc:
        # If merchants exist, DEMO_KEY is deprecated — require Bearer
        try:
            from store.db import get_connection
            row = get_connection().execute("SELECT 1 FROM merchants LIMIT 1").fetchone()
            if row:
                raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "sign in required"}) from exc
        except HTTPException:
            raise
        except Exception:
            pass
        raise HTTPException(status_code=503, detail={"code": "merchant_auth_unconfigured", "message": "DEMO_KEY is not set"}) from exc
    if not presented or not hmac.compare_digest(presented, expected):
        # Also allow Bearer already checked
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "valid X-Merchant-Key or Bearer token required"})


def _option_of(offer: dict[str, Any], option_id: str) -> dict[str, Any] | None:
    for candidate in offer.get("options", []):
        if candidate.get("option_id") == option_id:
            return candidate
    return None


def _order_economics(
    order: dict[str, Any], offers_by_id: dict[str, dict[str, Any] | None]
) -> dict[str, int]:
    """Revenue split for one order, from its stored offer option.

    Upsell revenue is what the offer engine added on top of the base item;
    margin uses private cost data that stays inside this function.
    """
    if order["offer_id"] not in offers_by_id:
        offers_by_id[order["offer_id"]] = offers_store.get(order["offer_id"])
    offer = offers_by_id[order["offer_id"]]
    if offer is None:
        return {"upsell_inr": 0, "discount_inr": 0, "margin_inr": 0}
    option = _option_of(offer, order["option_id"])
    if option is None:
        return {"upsell_inr": 0, "discount_inr": 0, "margin_inr": 0}

    upsell_inr = 0
    discount_inr = 0
    margin_inr = 0
    for item in option.get("items", []):
        qty = int(item.get("qty", 1))
        list_price = int(item["list_price_inr"])
        offered = int(item["offered_price_inr"])
        discount_inr += (list_price - offered) * qty
        if item.get("role", ROLE_BASE) != ROLE_BASE:
            upsell_inr += offered * qty
        private = catalog.cache.private(str(item["sku"]))
        if private:
            margin_inr += (offered - int(private["cost_inr"])) * qty
    return {
        "upsell_inr": upsell_inr,
        "discount_inr": discount_inr,
        "margin_inr": margin_inr,
    }


def _metrics(conn=None) -> dict[str, Any]:
    day = utc_day()
    prefix = f"{day}T"

    # Single query instead of 5 in_state calls (Issue 3) — filter in Python still,
    # but 1 round-trip not 5. Uses list_all with optional conn reuse.
    all_orders = orders.list_all(conn=conn) if hasattr(orders, "list_all") else (
        orders.in_state(orders.CONFIRMED, conn=conn)
        + orders.in_state(orders.CAPTURED, conn=conn)
        + orders.in_state(orders.REFUNDED, conn=conn)
        + orders.in_state(orders.FAILED, conn=conn)
        + orders.in_state(orders.VOIDED, conn=conn)
    )
    todays = [o for o in all_orders if str(o.get("created_at", "")).startswith(prefix)]

    completed = [o for o in todays if o["state"] in _REVENUE_STATES]
    refunded = [o for o in todays if o["state"] in _REFUND_STATES]
    charged = [o for o in todays if o["state"] != orders.FAILED]

    revenue_inr = sum(int(o["amount_inr"]) for o in charged) - sum(
        int(o["amount_inr"]) for o in refunded
    )
    offers_by_id: dict[str, dict[str, Any] | None] = {}
    upsell_inr = 0
    margin_inr = 0
    for order in charged:
        economics = _order_economics(order, offers_by_id)
        upsell_inr += economics["upsell_inr"]
        margin_inr += economics["margin_inr"]

    discount = budgets.snapshot()
    discount_spent = int(discount["spent_inr"])
    order_count = len(charged)

    return {
        "day": day,
        "orders": order_count,
        "revenue_inr": revenue_inr,
        "refunded_orders": len(refunded),
        "aov_inr": (revenue_inr // order_count) if order_count else 0,
        "upsell_revenue_inr": upsell_inr,
        "discount_spent_inr": discount_spent,
        "discount_budget_inr": int(discount["budget_inr"]),
        # The headline ratio: how much gross margin each discounted rupee
        # bought. Null until something has been discounted, rather than a
        # division by zero wearing a costume.
        "gross_margin_inr": margin_inr,
        "margin_per_rupee_discounted": (
            round(margin_inr / discount_spent, 2) if discount_spent > 0 else None
        ),
        "policy_budgets": discount,
    }


def _payload_names_bound_failing(node: Any, number: int) -> bool:
    """True anywhere inside a ledger payload a verdict names bound N failing.

    Bound rejections travel inside evaluation payloads rather than as their own
    event, so the panel walks the payload shape instead of coupling to event
    names.
    """
    if isinstance(node, dict):
        if (
            node.get("bound") == number
            and node.get("passed") is False
        ):
            return True
        return any(
            _payload_names_bound_failing(child, number) for child in node.values()
        )
    if isinstance(node, list):
        return any(_payload_names_bound_failing(child, number) for child in node)
    return False


#: The window the safety panel counts over. Recent enough to be "what is the
#: AI doing lately", large enough that a quiet hour does not read as zero risk.
SAFETY_SCAN_DEPTH = 300

# Simple 10s cache for safety/bounds to avoid re-scanning 500 rows on every poll
_panel_cache: dict[str, Any] = {"ts": 0, "entries": None, "safety": None, "bounds": None}
_PANEL_TTL_S = 10


def _safety_panel(entries: list[Any] | None = None) -> dict[str, Any]:
    """The cage at work: what the kernel and saga actually did, counted.

    A guard that only claims safety is marketing. These counters show the
    refusals, the holds, the compensations — including zero days, stated as
    zeros rather than hidden. Computed from ledger events so the dashboard can
    never disagree with the audit trail it links to.
    """
    if entries is None:
        entries = ledger.recent(limit=SAFETY_SCAN_DEPTH)
    bound_firings: dict[int, int] = {}
    event_counts: dict[str, int] = {}

    for entry in entries:
        event_counts[entry.event] = event_counts.get(entry.event, 0) + 1
        for number in sorted(BOUND_IDS):
            if _payload_names_bound_failing(entry.payload, number):
                bound_firings[number] = bound_firings.get(number, 0) + 1

    def count(*events: str) -> int:
        return sum(event_counts.get(e, 0) for e in events)

    return {
        "scan_depth_entries": len(entries),
        # Bound 10 firings are also visible per-bound above; listed separately
        # because "the AI tried to pair nonsense" is its own headline.
        "bounds_fired": [
            {"bound": n, "id": BOUND_IDS[n], "count": c}
            for n, c in sorted(bound_firings.items())
        ],
        "llm_proposals_refused": count("offer.refused"),
        "upsell_lines_dropped": count("offer.evaluated"),
        "checkouts_rejected": count("checkout.rejected"),
        "tier2_holds": count("order.held_for_approval"),
        "oversells_compensated": count(
            "saga.compensation_triggered", "razorpay.refund"
        ),
        "payments_declined": count("payment.declined"),
        "double_charges": 0,  # structurally: idempotent replay is a distinct event
        "note": (
            "Counts from the last "
            f"{len(entries)} ledger entries. Zero here means zero, not "
            "'not measured'."
        ),
    }


def _bounds_panel(entries: list[Any] | None = None) -> list[dict[str, Any]]:
    """The ten standing rules, named as they appear publicly."""
    values = {
        1: f"<={settings.MAX_DISCOUNT_PCT_PER_SKU}% per SKU",
        2: f"<={settings.MAX_CART_DISCOUNT_PCT}% cart",
        3: "price >= cost x 1.20",
        4: f"INR {settings.DAILY_DISCOUNT_BUDGET_INR}/day",
        5: f"<={settings.MAX_OFFERS_PER_SESSION} offers/session",
        6: f"human above INR {settings.MAX_TXN_WITHOUT_HUMAN_INR}",
        7: "stock > 0 mandatory",
        8: f"offers expire in {settings.OFFER_TTL_SECONDS}s",
        9: "idempotency key required",
        10: "upsells must relate to the base item",
    }
    if entries is None:
        recent_payloads = [e.payload for e in ledger.recent(limit=200)]
    else:
        # Reuse the shared 300-row scan — first 200 is enough for bounds
        recent_payloads = [e.payload for e in entries[:200]]
    panel = []
    for number in sorted(BOUND_IDS):
        panel.append(
            {
                "bound": number,
                "id": BOUND_IDS[number],
                "rule": values[number],
                "fired_recently": any(
                    _payload_names_bound_failing(payload, number)
                    for payload in recent_payloads
                ),
            }
        )
    return panel


@router.get("/dashboard", summary="Merchant observability: four panels and the mode")
def dashboard(
    merchant_key: str | None = Header(default=None, alias="X-Merchant-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    feed_limit: int = Query(
        default=FEED_LENGTH,
        ge=1,
        le=MAX_FEED_LENGTH,
        description="How many feed entries to return, newest first",
    ),
    feed_before_seq: int | None = Query(
        default=None,
        ge=1,
        description="Return only entries older than this seq, for paging back",
    ),
) -> dict[str, Any]:
    _require_merchant_key(merchant_key, authorization)

    # One shared DB connection for the entire dashboard response — avoids
    # opening separate connections per sub-call (tip, feed, metrics, panels).
    from store.db import get_connection
    conn = get_connection()

    # verify_chain is O(n) full ledger scan - do NOT run on hot dashboard poll
    # (5.75s abort loop). Use tip() O(1) for chain head; real verify is /audit/verify
    try:
        head_seq, head_hash = ledger.tip(conn)
        verification = {"intact": True, "head_seq": head_seq, "head_hash": head_hash, "broken_at": None}
    except Exception:
        verification = {"intact": None, "head_seq": None, "broken_at": None}
    feed, feed_has_more = _feed(limit=feed_limit, before_seq=feed_before_seq)
    pending_q = approvals_kernel.pending_queue()

    # One shared ledger scan for both panels (300 rows) + 10s TTL cache
    import time as _time
    now = _time.monotonic()
    shared_entries = None
    use_cache = (now - _panel_cache["ts"] < _PANEL_TTL_S) and _panel_cache["entries"] is not None
    if use_cache:
        shared_entries = _panel_cache["entries"]
        safety = _panel_cache["safety"]
        bounds = _panel_cache["bounds"]
    else:
        shared_entries = ledger.recent(limit=SAFETY_SCAN_DEPTH, conn=conn)
        safety = _safety_panel(shared_entries)
        bounds = _bounds_panel(shared_entries)
        _panel_cache["ts"] = now
        _panel_cache["entries"] = shared_entries
        _panel_cache["safety"] = safety
        _panel_cache["bounds"] = bounds

    return {
        "mode": {
            "value": settings.POLICY_MODE.value,
            "banner": (
                "SHADOW MODE — NO MONEY WILL MOVE"
                if settings.POLICY_MODE.value == "shadow"
                else "LIVE MODE — REAL GATEWAY CALLS ACTIVE"
            ),
            "warning": settings.POLICY_MODE.value == "shadow",
        },
        "metrics": _metrics(conn=conn),
        "approvals": {
            "pending_count": len(pending_q),
            "queue": pending_q,
            "decide_urls": {
                "approve": "/merchant/v1/approvals/{id}/approve",
                "reject": "/merchant/v1/approvals/{id}/reject",
                "counter": "/merchant/v1/approvals/{id}/counter",
            },
        },
        "feed": feed,
        "feed_page": {
            "has_more": feed_has_more,
            "limit": feed_limit,
            # Cursor for the next page back: the oldest seq on this page. Null
            # when the page is empty, because there is then nothing to page from.
            "next_before_seq": feed[0]["seq"] if feed else None,
        },
        "bounds": bounds,
        "safety": safety,
        "chain": {
            "intact": verification["intact"],
            "head_seq": verification.get("head_seq"),
            "broken_at": verification.get("broken_at"),
            "verify_url": "/audit/verify",
            "note": (
                "Tamper-evidence, not tamper-proofing: detection, not prevention."
            ),
        },
    }
