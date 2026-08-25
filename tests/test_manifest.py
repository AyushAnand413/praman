"""The discovery manifest: valid JSON, < 50ms, open.

The latency assertion is the one that matters most: this endpoint is a buyer
agent's first impression and its timeout calibration, so it does no work per
request.
"""

from __future__ import annotations

import json
import time

from settings import (
    CAPABILITIES,
    LATENCY_HINTS_MS,
    MANDATE_REQUIRED_ABOVE_INR,
    MAX_OFFERS_PER_SESSION,
    OFFER_TTL_SECONDS,
    POLICY_MODE,
)

MANIFEST = "/.well-known/agent-commerce.json"


def test_manifest_returns_valid_json(client):
    response = client.get(MANIFEST)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    json.loads(response.text)  # raises on malformed output


def test_manifest_is_unauthenticated(client):
    """No mandate, no key, no header - discovery requires no mandate."""
    assert client.get(MANIFEST).status_code == 200


def test_manifest_is_under_50ms(client):
    """The published budget. Best-of-N so a scheduler hiccup does not flake the suite."""
    client.get(MANIFEST)  # warm the code path
    best = min(
        (lambda t0=time.perf_counter(): (client.get(MANIFEST), time.perf_counter() - t0)[1])()
        for _ in range(20)
    )
    assert best < 0.050, f"manifest took {best * 1000:.1f}ms, budget is 50ms"


def test_manifest_publishes_latency_hints(client):
    """The three highest-value lines in the file."""
    hints = client.get(MANIFEST).json()["latency_hints_ms"]
    assert hints == LATENCY_HINTS_MS
    assert {"catalog", "offer", "checkout"} <= set(hints)


def test_published_hints_are_never_tighter_than_the_real_budget():
    """Checkout is budgeted 2-4s, so the hint must not be below 4000ms."""
    assert LATENCY_HINTS_MS["checkout"] >= 4_000
    assert LATENCY_HINTS_MS["offer"] >= 3_000


def test_manifest_discloses_policy(client):
    disclosure = client.get(MANIFEST).json()["policy_disclosure"]
    assert disclosure["max_offers_per_session"] == MAX_OFFERS_PER_SESSION == 2
    assert disclosure["price_hold_seconds"] == OFFER_TTL_SECONDS == 300
    assert disclosure["returns_window_days"] == 7
    assert disclosure["policy_mode"] == POLICY_MODE.value


def test_manifest_declares_identity_and_capabilities(client):
    body = client.get(MANIFEST).json()
    assert body["currency"] == "INR"
    assert body["merchant"]
    assert set(body["capabilities"]) == set(CAPABILITIES)
    assert body["auth"]["mandate"]["required_above_inr"] == MANDATE_REQUIRED_ABOVE_INR == 2000
    assert body["auth"]["scheme"] == "ed25519-signed-jwt"


def test_manifest_maps_every_agent_endpoint(client):
    endpoints = client.get(MANIFEST).json()["endpoints"]
    for key in ("catalog", "offer", "checkout", "order_status", "audit_verify"):
        assert endpoints[key].startswith("/")


def test_audit_claim_stays_honest(client):
    """The word 'immutable' must never appear here."""
    body = client.get(MANIFEST).text
    assert "tamper-evidence" in body
    assert "immutable" not in body.lower()


def test_health_reports_mode_and_cache(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["catalog_skus"] == 14
    assert body["policy_mode"] == POLICY_MODE.value
