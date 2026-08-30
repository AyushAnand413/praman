"""GET /merchant/v1/dashboard — the observability layer.

Four panels and the mode banner, over an authenticated merchant route. The
private-leak discipline applies here too even though this is a merchant view:
margins are computed into ratios server-side and cost column names never
appear in a response body.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def key(test_secrets):
    return test_secrets["DEMO_KEY"]


def _get(client, key=None):
    headers = {"X-Merchant-Key": key} if key else {}
    return client.get("/merchant/v1/dashboard", headers=headers)


def test_requires_merchant_key(client):
    assert _get(client).status_code == 401


def test_four_panels_and_banner(client, key):
    body = _get(client, key).json()

    assert set(body) >= {
        "mode", "metrics", "approvals", "feed", "bounds", "safety", "chain"
    }

    # The banner: unmissable about which mode is in force.
    assert body["mode"]["value"] in ("shadow", "live")
    if body["mode"]["value"] == "shadow":
        assert "NO MONEY" in body["mode"]["banner"]
        assert body["mode"]["warning"] is True

    # Metrics panel: whole rupees, budget alongside spend.
    metrics = body["metrics"]
    assert metrics["orders"] >= 0
    assert metrics["revenue_inr"] >= 0
    assert metrics["discount_budget_inr"] > 0
    assert metrics["discount_spent_inr"] <= metrics["discount_budget_inr"]

    # Bounds panel: all ten, named by their public ids.
    bounds = {row["bound"]: row for row in body["bounds"]}
    assert sorted(bounds) == list(range(1, 11))
    assert "floor_price_inr" not in json_of(body)
    assert all(row["id"] for row in body["bounds"])

    # Feed: newest last, public entry shape only.
    seqs = [entry["seq"] for entry in body["feed"]]
    assert seqs == sorted(seqs)

    # Chain panel agrees with the public verify endpoint.
    verify = client.get("/audit/verify").json()
    assert body["chain"]["intact"] == verify["intact"]
    assert body["chain"]["head_seq"] == verify["head_seq"]

    # The safety panel: zeros stated as zeros, counters from the ledger.
    safety = body["safety"]
    assert safety["double_charges"] == 0
    assert isinstance(safety["bounds_fired"], list)
    assert "not measured" in safety["note"]


def json_of(payload) -> str:
    import json

    return json.dumps(payload)


def test_feed_reflects_activity(client, key, live_mode, fake_razorpay, make_offer):
    """A purchase shows up; a compensation shows up just as prominently."""
    from kernel import saga

    seeded = make_offer("tier0")
    payload = saga.force_oversell(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        agent_id="agent_dash",
        client_factory=lambda: fake_razorpay,
    )
    assert payload["code"] == saga.CODE_OVERSOLD_MERCHANT_FAULT

    body = _get(client, key).json()
    events = [entry["event"] for entry in body["feed"]]
    assert "payment.captured" in events or "razorpay.refund" in events
    assert "policy.selfheal" in events

    metrics = body["metrics"]
    assert metrics["refunded_orders"] >= 1


def test_no_private_leak_in_dashboard_body(client, key):
    from store.catalog import PRIVATE_FIELDS

    raw = json_of(_get(client, key).json())
    for field in PRIVATE_FIELDS:
        assert field not in raw, f"private field {field} leaked into dashboard"
