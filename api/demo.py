"""POST /demo/force_oversell — the failure on cue.

A failure demo that fails to fail is worse than no failure demo, so the race is
made deterministic rather than hoped for. This endpoint restocks the SKUs an
offer needs, drives one checkout through the real money path with the shelf
moved underneath it mid-flight, and returns the structured OVERSOLD_MERCHANT_
FAULT payload the saga produced. Run the same call ten times, get the same
compensation ten times.

Two gates sit in front of it:

**The demo key.** Same shared-key pattern as the merchant routes, same honest
framing — this is not an authentication system, it is a latch that stops a
public endpoint from spending money whenever someone feels like it. DEMO_KEY
unset means 503, never an open door.

**POLICY_MODE=live.** A forced oversell in shadow mode is theatre: nothing is
captured, so there is nothing to compensate and nothing proven. The endpoint
refuses with an explanation rather than pretending. Flip the mode, then flip
the switch here.

Without an `offer_id` the endpoint seeds a fresh deterministic offer through
the same path every other consumer uses — bounds evaluated, tier assigned,
receipt signed — so a rehearsal needs no setup and touches no LLM.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from kernel import mode, saga
from scripts.seed_offer import SCENARIOS_BY_KEY, seed as seed_scenario
from settings import secret
from store import catalog, offers
from store.db import get_connection, transaction

router = APIRouter(prefix="/demo", tags=["demo"])

#: The seeded scenario a no-argument rehearsal uses: one unit, list price,
#: Tier 0 — the smallest cart that can complete without a mandate or a human.
DEFAULT_SCENARIO_KEY = "tier0"


class ForceOversellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_id: str | None = Field(
        default=None,
        description=(
            "An existing offer to oversell. Omitted, a fresh deterministic "
            "offer is seeded for this rehearsal."
        ),
    )
    option_id: str | None = Field(
        default=None,
        description="Which option of the offer. Defaults to its first.",
    )
    agent_id: str = Field(
        default="grahak_demo_oversell",
        min_length=1,
        max_length=128,
    )
    payment_id: str | None = Field(
        default=None,
        description=(
            "Run against a real Razorpay payment instead of the simulated "
            "gateway. Requires RAZORPAY keys configured."
        ),
    )


def _require_demo_key(presented: str | None) -> None:
    try:
        expected = secret("DEMO_KEY").reveal()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "demo_auth_unconfigured",
                "message": (
                    "DEMO_KEY is not set, so demo routes refuse to serve "
                    "rather than serve everyone"
                ),
            },
        ) from exc
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "valid X-Demo-Key required"},
        )


def _pre_restock_default() -> None:
    """Restore the shelf a fresh seeded rehearsal needs, before seeding it.

    A previous rehearsal self-healed the SKU away and consumed its unit, so
    seeding first would be refused by bound 7. Restocking before the run —
    never compensating afterwards — keeps the saga itself untouched.
    """
    from scripts.seed_offer import SCENARIOS_BY_KEY as _s
    from store import catalog

    scenario = _s[DEFAULT_SCENARIO_KEY]
    conn = get_connection()
    with transaction(conn):
        for line in scenario.lines:
            conn.execute(
                "UPDATE products SET stock_qty = MAX(stock_qty, ?) WHERE sku = ?",
                (line.qty, line.sku),
            )
    for line in scenario.lines:
        catalog.cache.set_offerable(line.sku, True)


def _resolve_offer(
    offer_id: str | None, option_id: str | None
) -> tuple[str, str]:
    """The offer this rehearsal runs against: supplied, or freshly seeded."""
    if offer_id is None:
        _pre_restock_default()
        summary = seed_scenario(SCENARIOS_BY_KEY[DEFAULT_SCENARIO_KEY])
        if summary.get("refused"):
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "demo_seed_refused",
                    "message": (
                        f"the default demo offer was refused by the bounds: "
                        f"{summary.get('detail')}"
                    ),
                },
            )
        return str(summary["offer_id"]), str(summary["option_id"])

    offer = offers.require(offer_id)  # 404 via handler below
    chosen = option_id or str(offer["options"][0]["option_id"])
    if offers.is_expired(offer):
        raise HTTPException(
            status_code=410,
            detail={
                "code": "offer_expired",
                "message": (
                    f"offer {offer_id} expired at {offer['expires_at']}. Pass "
                    "no offer_id and a fresh one will be seeded."
                ),
            },
        )
    return offer_id, chosen


@router.post("/force_oversell", summary="Fire the oversell compensation on cue")
def force_oversell(
    body: ForceOversellRequest | None = None,
    x_demo_key: str | None = Header(default=None, alias="X-Demo-Key"),
) -> dict[str, Any]:
    _require_demo_key(x_demo_key)

    if not mode.is_live():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "shadow_mode",
                "message": (
                    "POLICY_MODE=shadow moves no money, so there is no capture "
                    "to compensate and nothing proven by rehearsing. Set "
                    "POLICY_MODE=live and retry."
                ),
            },
        )

    request = body or ForceOversellRequest()
    try:
        resolved_offer, resolved_option = _resolve_offer(request.offer_id, request.option_id)
    except offers.OfferNotFound as exc:
        raise HTTPException(
            status_code=404, detail={"code": "offer_not_found", "message": str(exc)}
        ) from exc

    # The rehearsal consumes a unit and self-heals the SKU away, so restore
    # the shelf first — otherwise the second showing is the one that flops.
    saga.restock_for_offer(resolved_offer)

    try:
        payload = saga.force_oversell(
            offer_id=resolved_offer,
            option_id=resolved_option,
            agent_id=request.agent_id,
            payment_id=request.payment_id,
        )
    except mode.ShadowModeViolation as exc:
        raise HTTPException(
            status_code=409, detail={"code": "shadow_mode", "message": str(exc)}
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "oversell_did_not_fire", "message": str(exc)},
        ) from exc

    payload["audit_url"] = payload.get("audit_url") or f"/audit/{payload.get('order_id')}"
    payload["verify_url"] = "/audit/verify"
    return payload
