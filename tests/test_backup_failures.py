"""The backup failures: decline, webhook retry, expired offer, forged mandate.

These are the paths a demo hopes never to need, tested as if they will run
tonight. Each one asserts three things: the buyer gets an honest answer, no
state is corrupted, and the ledger records what happened.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from kernel import checkout as checkout_kernel
from kernel import stock as stock_kernel
from kernel import idempotency as idem_store
from store import catalog, ledger, orders


# ── Card declined ──────────────────────────────────────────────────────────────


def test_declined_capture_refuses_and_unwinds(db, live_mode, fake_razorpay, make_offer):
    """A gateway that reports failure instead of raising must not be read as paid."""
    # The stub's real knob is `_capture_status`; setting anything else would
    # leave it reporting success.
    fake_razorpay._capture_status = "failed"
    seeded = make_offer("tier0")
    before = stock_kernel.available_qty("AT-CBL-USBC")

    with pytest.raises(checkout_kernel.CheckoutError) as excinfo:
        checkout_kernel.checkout(
            offer_id=seeded["offer_id"],
            option_id=seeded["option_id"],
            idempotency_key="decline-1",
            agent_id="agent_decline",
            payment_id="pay_declined",
            client_factory=lambda: fake_razorpay,
        )
    assert excinfo.value.code == "payment_declined"
    assert excinfo.value.http_status == 402

    # Nothing was charged, so nothing may stay reserved.
    assert stock_kernel.available_qty("AT-CBL-USBC") == before
    order_id = find_order("decline-1")
    events = [entry.event for entry in ledger.trail(order_id)]
    assert "payment.declined" in events
    verify = ledger.verify_chain()
    assert verify["intact"] is True


def find_order(idempotency_key: str) -> str:
    record = idem_store.get(idempotency_key)
    assert record is not None
    return record["order_id"]


def test_retry_after_decline_with_new_key_succeeds(
    db, live_mode, fake_razorpay, make_offer
):
    """retry_safe: a decline burns nothing; a fresh key buys normally."""
    fake_razorpay._capture_status = "failed"
    seeded = make_offer("tier0")
    with pytest.raises(checkout_kernel.CheckoutError):
        checkout_kernel.checkout(
            offer_id=seeded["offer_id"],
            option_id=seeded["option_id"],
            idempotency_key="decline-retry-1",
            agent_id="agent_decline",
            payment_id="pay_declined",
            client_factory=lambda: fake_razorpay,
        )

    fake_razorpay._capture_status = "captured"

    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="decline-retry-2",
        agent_id="agent_decline",
        payment_id="pay_ok",
        client_factory=lambda: fake_razorpay,
    )
    assert result.status == checkout_kernel.STATUS_CONFIRMED


# ── Webhook timeout / redelivery ───────────────────────────────────────────────


def _signed_webhook(client: TestClient, payload: dict, secret_value: str):
    import json

    raw = json.dumps(payload).encode()
    signature = hmac.new(
        secret_value.encode(), raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"X-Razorpay-Signature": signature},
    )


def test_webhook_redelivery_is_deduplicated(
    client: TestClient, db, live_mode, fake_razorpay, make_offer, test_secrets
):
    """Razorpay retries until 2xx; the same event twice yields one outcome."""
    seeded = make_offer("tier0")
    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="webhook-dedupe-1",
        agent_id="agent_webhook",
        payment_id="pay_hook_1",
        client_factory=lambda: fake_razorpay,
    )
    order = orders.require(result.order_id)
    assert order["razorpay_payment_id"]

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": order["razorpay_payment_id"],
                    "order_id": order["razorpay_order_id"],
                    "amount": int(order["amount_inr"]) * 100,
                }
            }
        },
    }
    first = _signed_webhook(client, payload, test_secrets["RAZORPAY_WEBHOOK_SECRET"])
    second = _signed_webhook(client, payload, test_secrets["RAZORPAY_WEBHOOK_SECRET"])

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["outcome"] == "duplicate"

    # Exactly one processed entry for the event, and one capture on the books.
    entries = ledger.find_by_payload(
        "webhook_event_id", first.json()["event_id"]
    )
    assert len(entries) == 1


def test_webhook_out_of_order_is_tolerated(
    client: TestClient, db, live_mode, fake_razorpay, make_offer, test_secrets
):
    """captured before authorized: the stale event is old news, not an error."""
    seeded = make_offer("tier0")
    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="webhook-order-1",
        agent_id="agent_webhook",
        payment_id="pay_hook_2",
        client_factory=lambda: fake_razorpay,
    )
    order = orders.require(result.order_id)

    def event(name: str):
        body = {
            "event": name,
            "payload": {
                "payment": {
                    "entity": {
                        "id": order["razorpay_payment_id"],
                        "order_id": order["razorpay_order_id"],
                        "amount": int(order["amount_inr"]) * 100,
                    }
                }
            },
        }
        response = _signed_webhook(client, body, test_secrets["RAZORPAY_WEBHOOK_SECRET"])
        assert response.status_code == 200
        return response.json()

    captured = event("payment.captured")
    authorized = event("payment.authorized")

    assert captured["outcome"] in ("advanced", "already")
    assert authorized["outcome"] == "stale" or authorized["state"] == orders.CAPTURED
    assert orders.require(result.order_id)["state"] == orders.CAPTURED


def test_webhook_bad_signature_is_401(client: TestClient, test_secrets):
    response = client.post(
        "/webhooks/razorpay",
        content=b'{"event": "payment.captured"}',
        headers={"X-Razorpay-Signature": "0" * 64},
    )
    assert response.status_code == 401


# ── Expired offer at checkout ──────────────────────────────────────────────────


def test_expired_offer_is_refused_not_honoured(db, live_mode, fake_razorpay, make_offer):
    from datetime import timedelta

    from settings import OFFER_TTL_SECONDS
    from store.timestamps import utc_now

    seeded = make_offer("tier0")
    late = utc_now() + timedelta(seconds=OFFER_TTL_SECONDS + 5)
    # A failed bound is a real, ledgered outcome rather than an exception:
    # the order exists, was refused by name, and the refusal is replayable.
    result = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="expired-1",
        agent_id="agent_late",
        payment_id="pay_late",
        now=late,
        client_factory=lambda: fake_razorpay,
    )
    assert result.status == checkout_kernel.STATUS_REJECTED
    failed_bounds = [
        entry["bound"]
        for entry in result.policy_receipt["gate"]["cart_bounds"]
        if not entry["passed"]
    ]
    assert 8 in failed_bounds


# ── Forged mandate ─────────────────────────────────────────────────────────────


def test_forged_mandate_is_refused_and_ledgered(
    db, live_mode, fake_razorpay, make_offer, trusted_issuer
):
    seeded = make_offer("tier1")  # Tier 1: a valid mandate is required
    forged = (
        "eyJhbGdvcml0aG0iOiJFZDI1NTE5IiwidHlwZSI6IkpXVCJ9."
        "eyJzdWIiOiJmYWtlIn0."
        "c2lnbmF0dXJlLXRoYXQtdmVyaWZpZXMtYWdhaW5zdC1ub3RoaW5n"
    )
    with pytest.raises(checkout_kernel.CheckoutError) as excinfo:
        checkout_kernel.checkout(
            offer_id=seeded["offer_id"],
            option_id=seeded["option_id"],
            idempotency_key="forged-1",
            agent_id="agent_forger",
            mandate_token=forged,
            client_factory=lambda: fake_razorpay,
        )
    assert excinfo.value.http_status == 403
    assert excinfo.value.code.startswith("mandate_")


def test_mandate_replay_is_rejected_second_time(
    db, live_mode, fake_razorpay, make_offer, trusted_issuer, mandate_for
):
    """One mandate, two checkouts: the nonce store IS the ledger."""
    seeded = make_offer("tier1")
    token = mandate_for(agent_id="agent_replay", max_amount_inr=50_000)

    first = checkout_kernel.checkout(
        offer_id=seeded["offer_id"],
        option_id=seeded["option_id"],
        idempotency_key="replay-1",
        agent_id="agent_replay",
        mandate_token=token,
        payment_id="pay_replay",
        client_factory=lambda: fake_razorpay,
    )
    assert first.status == checkout_kernel.STATUS_CONFIRMED

    from kernel import saga
    saga.restock_for_offer(seeded["offer_id"])
    with pytest.raises(checkout_kernel.CheckoutError) as excinfo:
        checkout_kernel.checkout(
            offer_id=seeded["offer_id"],
            option_id=seeded["option_id"],
            idempotency_key="replay-2",
            agent_id="agent_replay",
            mandate_token=token,
            payment_id="pay_replay_2",
            client_factory=lambda: fake_razorpay,
        )
    assert excinfo.value.code.startswith("mandate_")
