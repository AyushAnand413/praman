"""The demo control surface: POST /demo/force_oversell.

The endpoint is the stage cue for the primary failure. What the tests pin
down: it is latched behind the demo key, it refuses in shadow mode rather
than pretending to rehearse, and it fires the same compensation every time —
ten rehearsals, ten refunds.
"""

from __future__ import annotations

import pytest

from kernel import saga
from store import orders


@pytest.fixture
def demo_key(test_secrets):
    return test_secrets["DEMO_KEY"]


def _post(client, key=None, body=None):
    headers = {"X-Demo-Key": key} if key else {}
    return client.post("/demo/force_oversell", json=body or {}, headers=headers)


def test_requires_demo_key(client):
    assert _post(client).status_code == 401
    assert _post(client, key="wrong-key").status_code == 401


def test_refuses_in_shadow_mode_with_explanation(client, demo_key):
    response = _post(client, key=demo_key)
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["code"] == "shadow_mode"
    # The refusal names the fix: flip POLICY_MODE, then retry.
    assert "POLICY_MODE=live" in body["detail"]["message"]


def test_fires_structured_failure_when_live(client, demo_key, live_mode):
    response = _post(client, key=demo_key)
    assert response.status_code == 200
    payload = response.json()

    assert payload["code"] == saga.CODE_OVERSOLD_MERCHANT_FAULT
    assert payload["status"] == "failed"
    assert payload["gateway"] == "simulated"
    assert payload["rehearsal"] is True
    assert payload["retry_safe"] is True
    assert payload["refund"]["amount_inr"] > 0
    assert payload["audit_url"].startswith("/audit/")
    assert orders.require(payload["order_id"])["state"] == orders.REFUNDED


def test_ten_rehearsals_ten_compensations(client, demo_key, live_mode, db):
    """A failure demo that fails to fail is worse than none; rehearse x10."""
    for i in range(10):
        response = _post(client, key=demo_key)
        assert response.status_code == 200, f"rehearsal {i} did not fire"
        payload = response.json()
        assert payload["code"] == saga.CODE_OVERSOLD_MERCHANT_FAULT
        assert orders.require(payload["order_id"])["state"] == orders.REFUNDED

    verify = client.get("/audit/verify").json()
    assert verify["intact"] is True


def test_unknown_offer_is_404(client, demo_key, live_mode):
    response = _post(
        client,
        key=demo_key,
        body={"offer_id": "OF-does-not-exist"},
    )
    assert response.status_code == 404


def test_ledger_records_the_whole_story(client, demo_key, live_mode):
    response = _post(client, key=demo_key)
    order_id = response.json()["order_id"]
    events = [entry.event for entry in __import__("store").ledger.trail(order_id)]
    for expected in saga.COMPENSATION_EVENT_SEQUENCE:
        assert expected in events
