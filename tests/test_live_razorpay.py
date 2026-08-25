"""Phase 1 and Phase 2 verified against the real Razorpay test-mode API.

    python -m pytest tests/test_live_razorpay.py --live-api -v

Everything here makes a genuine HTTPS call to Razorpay with the credentials in
`.env`. Deselected without `--live-api`, so the default suite stays offline.

Scope, and the honest boundary of it: order creation, fetching, authentication,
amount fidelity, refund error paths, and webhook signature verification are all
exercised for real. **Capturing a payment is not**, because a payment cannot be
created server-side on a standard test account — Razorpay gates that API behind
per-account activation, and this account returns 404 for it. A real capture
therefore needs a card entered once in a browser, which is what
`scripts/razorpay_smoke.py` sets up. `test_live_capture.py` picks up from there.

The webhook tests are worth reading closely, because they are the part people
assume needs a real payment and does not. Razorpay signs a webhook with an HMAC
over the raw request body under `RAZORPAY_WEBHOOK_SECRET`. That secret is real
and in `.env`, so a body signed with it is byte-for-byte indistinguishable from
one Razorpay sent. Delivering it over a public tunnel would test ngrok, not this
code.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from kernel.payments import RazorpayClient, RazorpayError, verify_webhook_signature
from settings import RAZORPAY_API_BASE
from store import ledger

pytestmark = pytest.mark.live_api

#: The demo's own number: AT-PRO-BLK (4999) + AT-CASE-01 (599).
SMOKE_AMOUNT_INR = 5_598


# ── the credentials work, and only the right ones do ───────────────────────────


def test_the_real_test_key_authenticates(live_razorpay: RazorpayClient):
    """Deliverable 1's precondition: these credentials genuinely reach Razorpay."""
    order = live_razorpay.create_order(
        SMOKE_AMOUNT_INR,
        receipt="live-auth-check",
        notes={"purpose": "live_api_test"},
    )
    assert order["id"].startswith("order_")
    assert order["status"] == "created"


def test_a_wrong_secret_is_rejected_by_razorpay(live_razorpay: RazorpayClient):
    """The negative half. Without it, a test that passes proves only that *some*
    request succeeded, not that the credential was the thing that made it work.

    This is the exact failure a trailing space on a pasted key produces.
    """
    client = RazorpayClient(
        key_id=live_razorpay.key_id, key_secret="definitely-not-the-secret"
    )
    with pytest.raises(RazorpayError) as caught:
        client.create_order(100, receipt="live-bad-secret")
    assert caught.value.status_code == 401


def test_a_key_with_trailing_whitespace_fails_authentication(
    real_credentials: dict[str, str],
):
    """Regression guard for a real bug: the secret in `.env` had a trailing space.

    HTTP Basic auth transmits the padded value verbatim, so Razorpay answers 401
    and it reads as an invalid key rather than a malformed one. `_env.parse_env_file`
    strips values now; this asserts the underlying failure is real so the strip is
    never mistaken for cosmetic tidying.
    """
    padded = real_credentials["RAZORPAY_KEY_SECRET"] + " "
    response = httpx.get(
        f"{RAZORPAY_API_BASE}/orders?count=1",
        auth=(real_credentials["RAZORPAY_KEY_ID"], padded),
        timeout=20.0,
    )
    assert response.status_code == 401

    clean = httpx.get(
        f"{RAZORPAY_API_BASE}/orders?count=1",
        auth=(
            real_credentials["RAZORPAY_KEY_ID"],
            real_credentials["RAZORPAY_KEY_SECRET"],
        ),
        timeout=20.0,
    )
    assert clean.status_code == 200


def test_a_live_key_is_refused_before_any_network_call():
    """The guard fires at construction, so a live key cannot reach the wire."""
    with pytest.raises(RazorpayError, match="must be a test key"):
        RazorpayClient(key_id="rzp_live_something", key_secret="x")


# ── amounts survive the round trip ─────────────────────────────────────────────


def test_rupees_survive_the_round_trip_through_razorpay(live_razorpay: RazorpayClient):
    """Rupees out, paise on the wire, rupees back — against the real gateway.

    The unit test proves the conversion functions. This proves Razorpay agrees:
    a rupees/paise confusion here is a 100x money bug, and it is exactly the kind
    of thing a stub cannot catch because the stub was written by the same hand.
    """
    order = live_razorpay.create_order(SMOKE_AMOUNT_INR, receipt="live-amount-check")
    assert order["amount_inr"] == SMOKE_AMOUNT_INR

    fetched = live_razorpay.fetch_order(order["id"])
    assert fetched["amount_inr"] == SMOKE_AMOUNT_INR
    assert fetched["amount_paid_inr"] == 0
    assert fetched["currency"] == "INR"

    # And the raw API really is holding paise, which is the claim being made.
    raw = httpx.get(
        f"{RAZORPAY_API_BASE}/orders/{order['id']}",
        auth=(live_razorpay.key_id, live_razorpay._key_secret),
        timeout=20.0,
    ).json()
    assert raw["amount"] == SMOKE_AMOUNT_INR * 100


def test_the_server_fixes_the_amount_not_the_caller(live_razorpay: RazorpayClient):
    """Two orders for different amounts do not bleed into each other."""
    small = live_razorpay.create_order(1_499, receipt="live-small")
    large = live_razorpay.create_order(14_997, receipt="live-large")

    assert live_razorpay.fetch_order(small["id"])["amount_inr"] == 1_499
    assert live_razorpay.fetch_order(large["id"])["amount_inr"] == 14_997


def test_notes_carry_the_orders_provenance(live_razorpay: RazorpayClient):
    """Checkout writes order/offer/agent/tier into notes; the audit story needs them."""
    order = live_razorpay.create_order(
        SMOKE_AMOUNT_INR,
        receipt="live-notes",
        notes={"order_id": "ord_live_test", "gate_tier": "1"},
    )
    fetched = live_razorpay.fetch_order(order["id"])
    assert fetched["notes"]["order_id"] == "ord_live_test"
    assert fetched["notes"]["gate_tier"] == "1"
    assert fetched["receipt"] == "live-notes"


# ── errors come back as errors ─────────────────────────────────────────────────


def test_an_unknown_order_raises_rather_than_returning_empty(
    live_razorpay: RazorpayClient,
):
    with pytest.raises(RazorpayError) as caught:
        live_razorpay.fetch_order("order_doesNotExist99")
    assert caught.value.status_code in (400, 404)


def test_refunding_a_nonexistent_payment_raises(live_razorpay: RazorpayClient):
    """The refund path reaches Razorpay and its rejection is surfaced, not swallowed.

    A refund against a real captured payment belongs in the compensation saga;
    this establishes that the transport and error mapping work before then.
    """
    with pytest.raises(RazorpayError) as caught:
        live_razorpay.refund_payment("pay_doesNotExist99", amount_inr=100)
    assert caught.value.status_code in (400, 404)


def test_a_fresh_order_has_no_payments(live_razorpay: RazorpayClient):
    order = live_razorpay.create_order(SMOKE_AMOUNT_INR, receipt="live-no-payments")
    assert live_razorpay.fetch_order_payments(order["id"]) == []


def test_negative_and_float_amounts_never_reach_the_network():
    """Rejected locally, so a malformed amount cannot become a gateway round trip."""
    client = RazorpayClient(key_id="rzp_test_local", key_secret="unused")
    with pytest.raises(ValueError):
        client.create_order(-1, receipt="never-sent")
    with pytest.raises(TypeError):
        client.create_order(99.5, receipt="never-sent")


# ── webhook signatures, verified with the real secret ──────────────────────────


def _sign(body: bytes, secret_value: str) -> str:
    return hmac.new(secret_value.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _webhook_body(event: str = "payment.captured", **entity) -> bytes:
    """A payload shaped like Razorpay's, serialised once so the bytes are stable."""
    payload = {
        "entity": "event",
        "event": event,
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": entity.get("payment_id", "pay_liveTest0001"),
                    "order_id": entity.get("order_id", "order_liveTest0001"),
                    "amount": entity.get("amount_paise", SMOKE_AMOUNT_INR * 100),
                    "currency": "INR",
                    "status": "captured",
                    "captured": True,
                    "method": "card",
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def webhook_secret(real_credentials: dict[str, str], monkeypatch) -> str:
    """Put the genuine webhook secret back over the autouse test stand-in."""
    value = real_credentials.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not value:
        pytest.skip("RAZORPAY_WEBHOOK_SECRET absent from .env")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", value)
    return value


def test_a_body_signed_with_the_real_secret_verifies(webhook_secret: str):
    """This is what Razorpay's own delivery would produce, byte for byte."""
    body = _webhook_body()
    assert verify_webhook_signature(body, _sign(body, webhook_secret)) is True


def test_a_body_signed_with_the_wrong_secret_is_refused(webhook_secret: str):
    body = _webhook_body()
    assert verify_webhook_signature(body, _sign(body, "not-the-secret")) is False


def test_a_tampered_amount_invalidates_the_signature(webhook_secret: str):
    """The attack the signature exists to stop: a payment inflated in transit."""
    body = _webhook_body()
    signature = _sign(body, webhook_secret)

    tampered = _webhook_body(amount_paise=99_999_900)
    assert tampered != body
    assert verify_webhook_signature(tampered, signature) is False


def test_a_missing_signature_is_refused(webhook_secret: str):
    body = _webhook_body()
    assert verify_webhook_signature(body, None) is False
    assert verify_webhook_signature(body, "") is False


def test_the_signature_covers_the_exact_bytes_not_the_parsed_object(
    webhook_secret: str,
):
    """Why the endpoint reads `await request.body()` and never re-serialises.

    Re-encoding the parsed JSON changes key order and spacing, and every genuine
    webhook would then fail verification. Same object, different bytes, no match.
    """
    body = _webhook_body()
    signature = _sign(body, webhook_secret)

    reserialised = json.dumps(json.loads(body), sort_keys=True, indent=2).encode("utf-8")
    assert json.loads(reserialised) == json.loads(body)
    assert reserialised != body
    assert verify_webhook_signature(reserialised, signature) is False


# ── the endpoint, with real signatures ─────────────────────────────────────────


def test_the_endpoint_rejects_a_forged_webhook(client, webhook_secret: str):
    """Phase 2 deliverable 7: a bad signature is a 401."""
    body = _webhook_body()
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": _sign(body, "forged-secret"),
            "X-Razorpay-Event-Id": "evt_live_forged",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401


def test_the_endpoint_accepts_a_genuinely_signed_webhook(client, webhook_secret: str):
    body = _webhook_body(order_id="order_liveAccept01")
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": _sign(body, webhook_secret),
            "X-Razorpay-Event-Id": "evt_live_accept_01",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200


def test_a_redelivered_webhook_is_processed_once(client, webhook_secret: str):
    """Razorpay retries until it gets a 2xx, so redelivery is routine, not an edge."""
    body = _webhook_body(order_id="order_liveReplay01")
    headers = {
        "X-Razorpay-Signature": _sign(body, webhook_secret),
        "X-Razorpay-Event-Id": "evt_live_replay_01",
        "Content-Type": "application/json",
    }

    first = client.post("/webhooks/razorpay", content=body, headers=headers)
    second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["outcome"] != "duplicate"
    assert second.json()["outcome"] == "duplicate"
    # The redelivery is answered from the ledger entry the first one wrote.
    assert second.json()["first_seen_seq"] == first.json()["ledger_seq"]

    seen = [
        entry
        for entry in ledger.recent(200)
        if entry.payload.get("webhook_event_id") == "evt_live_replay_01"
        and entry.event != "webhook.duplicate"
    ]
    assert len(seen) == 1


def test_an_unsigned_webhook_reads_nothing_from_the_body(client, webhook_secret: str):
    """A 401 must happen before the payload is parsed, let alone acted on."""
    body = _webhook_body(order_id="order_liveUnsigned01")
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401

    touched = [
        entry
        for entry in ledger.recent(200)
        if entry.payload.get("razorpay_order_id") == "order_liveUnsigned01"
    ]
    assert touched == []
