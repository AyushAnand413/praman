"""GET /.well-known/agent-commerce.json — the discovery manifest.

Static, unauthenticated, cacheable, and budgeted at **< 50ms**. It therefore
touches no database and does no work per request: the JSON bytes are built once
at import and handed back verbatim.

The two parts that matter beyond mere discovery:

* `policy_disclosure.max_offers_per_session: 2` is a public commitment not to
  spam the buyer agent. Restraint, declared up front, is what separates
  merchandising from dark patterns.
* `latency_hints_ms` tells a buyer agent how long to wait. An agent that times
  out at 1s on a 3s endpoint retries, and retries turn an idempotency bug into
  a double charge — so this is a correctness feature, not documentation.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Response

from settings import (
    CAPABILITIES,
    DEFAULT_RETURNS_WINDOW_DAYS,
    LATENCY_HINTS_MS,
    MANDATE_AUTH_SCHEME,
    MANDATE_REQUIRED_ABOVE_INR,
    MAX_OFFERS_PER_SESSION,
    MERCHANT_NAME,
    OFFER_TTL_SECONDS,
    POLICY_MODE,
    PolicyMode,
)

router = APIRouter(tags=["discovery"])

MANIFEST_PATH = "/.well-known/agent-commerce.json"


def build_manifest() -> dict[str, Any]:
    """The discovery document. Pure function of configuration."""
    return {
        "spec_version": "0.1",
        "merchant": MERCHANT_NAME,
        "currency": "INR",
        "amount_unit": "whole_rupees",
        "capabilities": list(CAPABILITIES),
        "auth": {
            # Machine-readable rather than the prose "required_above_inr_2000":
            # a buyer agent must be able to decide this without parsing English.
            "mandate": {"required_above_inr": MANDATE_REQUIRED_ABOVE_INR},
            "scheme": MANDATE_AUTH_SCHEME,
        },
        "endpoints": {
            "discovery": MANIFEST_PATH,
            "catalog": "/agent/v1/catalog",
            "offer": "/agent/v1/offer",
            "checkout": "/agent/v1/checkout",
            "order_status": "/agent/v1/order/{id}",
            "audit_entry": "/audit/{id}",
            "audit_verify": "/audit/verify",
            "mcp": "/mcp",
        },
        "policy_disclosure": {
            "max_offers_per_session": MAX_OFFERS_PER_SESSION,
            "price_hold_seconds": OFFER_TTL_SECONDS,
            "returns_window_days": DEFAULT_RETURNS_WINDOW_DAYS,
            # Disclosed on purpose: in shadow mode the full policy verdict is
            # computed and ledgered but no money moves. A buyer agent deserves
            # to know that before it tries to transact.
            "policy_mode": POLICY_MODE.value,
            "money_moves": POLICY_MODE is PolicyMode.LIVE,
        },
        "latency_hints_ms": dict(LATENCY_HINTS_MS),
        "audit": {
            "public": True,
            "chain": "sha256-linked, append-only",
            # Do not let this claim drift into "immutable".
            "guarantee": "tamper-evidence, not tamper-proof",
        },
    }


#: Serialized once. Requests do zero work beyond writing these bytes.
_MANIFEST_BYTES: bytes = json.dumps(build_manifest(), indent=2).encode("utf-8")


@router.get(MANIFEST_PATH, summary="Agent-commerce discovery manifest")
def agent_commerce_manifest() -> Response:
    return Response(
        content=_MANIFEST_BYTES,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )
