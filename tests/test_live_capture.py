"""The capture and refund paths, against a real captured Razorpay payment.

    python -m pytest tests/test_live_capture.py --live-api -v

`test_live_razorpay.py` stops at order creation, because a payment cannot be
created server-side on a standard test account. This file picks up after the
browser step that `scripts/razorpay_smoke.py` sets up: once someone has actually
paid, the account holds a genuine `pay_...` and the paths that only exist once
money has moved — already-captured reconciliation, rupee fidelity on a real
capture, refunds — can be exercised for real.

The payment is discovered from the account rather than named in the source, so
this works with any credentials and skips cleanly when nothing has been paid
yet. One test issues a real Rs 1 partial refund; it is the only way to prove the
refund path against money that genuinely moved, and it leaves the rest of the
payment intact.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kernel import checkout as checkout_kernel
from kernel.payments import RazorpayClient, RazorpayError
from settings import RAZORPAY_API_BASE
from store import ledger, orders

pytestmark = pytest.mark.live_api

#: Leave this much refundable so repeated runs of the refund test cannot
#: exhaust the payment and start failing for an uninteresting reason.
REFUND_HEADROOM_INR = 10


def _list_payments(client: RazorpayClient, count: int = 100) -> list[dict[str, Any]]:
    """Raw payment list. The client has no list method — it needs none in the
    money path, and adding one for a test would widen the credential-holding
    surface for no product reason."""
    response = httpx.get(
        f"{RAZORPAY_API_BASE}/payments",
        params={"count": count},
        auth=(client.key_id, client._key_secret),
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json().get("items", [])


@pytest.fixture
def captured_raw(live_razorpay: RazorpayClient) -> dict[str, Any]:
    """The most recent genuinely captured payment, straight from the API.

    Raw rather than normalised because the fixture needs `amount_refunded`, which
    the normaliser does not carry, and because the paise assertions below have to
    read the untouched wire value to mean anything.
    """
    captured = [
        payment
        for payment in _list_payments(live_razorpay)
        if payment.get("status") == "captured" and payment.get("captured")
    ]
    if not captured:
        pytest.skip(
            "no captured payment in this Razorpay account yet — run "
            "scripts/razorpay_smoke.py and pay once in the browser first"
        )
    return captured[0]


@pytest.fixture
def failed_raw(live_razorpay: RazorpayClient) -> dict[str, Any]:
    """A payment that was attempted and failed, if the account has one."""
    failed = [
        payment
        for payment in _list_payments(live_razorpay)
        if payment.get("status") == "failed"
    ]
    if not failed:
        pytest.skip("no failed payment attempt in this Razorpay account")
    return failed[0]


# ── the capture is real ────────────────────────────────────────────────────────


def test_a_real_captured_payment_reports_itself_as_captured(
    live_razorpay: RazorpayClient, captured_raw: dict[str, Any]
):
    """Deliverable 1, closed: money genuinely moved and the client sees it.

    Read back through `fetch_payment` rather than asserted on the raw list, so
    what is being checked is the shape the orchestrator actually consumes.
    """
    payment = live_razorpay.fetch_payment(captured_raw["id"])

    assert payment["id"].startswith("pay_")
    assert payment["status"] == "captured"
    assert payment["captured"] is True
    assert payment["currency"] == "INR"
    assert payment["method"]
    assert payment["error_code"] is None
    # The gateway order this belongs to is carried through the normaliser.
    assert payment["order_id"].startswith("order_")


def test_the_captured_amount_is_rupees_here_and_paise_on_the_wire(
    live_razorpay: RazorpayClient, captured_raw: dict[str, Any]
):
    """The 100x bug, checked against money that actually moved.

    Every earlier version of this assertion ran against an unpaid order. This one
    runs against a real capture, which is the case where getting it wrong would
    have charged a hundred times the intended amount.
    """
    payment = live_razorpay.fetch_payment(captured_raw["id"])

    assert payment["amount_inr"] == captured_raw["amount"] // 100
    assert captured_raw["amount"] == payment["amount_inr"] * 100
    # No fractional rupee slipped through: this system refuses them rather than
    # rounding, so a captured amount must divide cleanly into whole rupees.
    assert captured_raw["amount"] % 100 == 0


def test_the_gateway_order_shows_the_money_as_paid(
    live_razorpay: RazorpayClient, captured_raw: dict[str, Any]
):
    """The order and the payment agree, which is the reconciliation settle relies on."""
    payment = live_razorpay.fetch_payment(captured_raw["id"])
    order = live_razorpay.fetch_order(payment["order_id"])

    assert order["status"] == "paid"
    assert order["amount_paid_inr"] == order["amount_inr"]
    assert order["amount_paid_inr"] == payment["amount_inr"]


def test_the_payment_is_listed_against_its_order(
    live_razorpay: RazorpayClient, captured_raw: dict[str, Any]
):
    """`fetch_order_payments` is how settle could find a payment it was not handed."""
    payment = live_razorpay.fetch_payment(captured_raw["id"])
    attempts = live_razorpay.fetch_order_payments(payment["order_id"])

    assert payment["id"] in [attempt["id"] for attempt in attempts]
    # Exactly one attempt on this order actually captured. A failed attempt
    # sitting next to a successful one is the normal shape of a paid order, and
    # counting attempts instead of captures is how oversells begin.
    assert len([a for a in attempts if a["captured"]]) == 1


def test_recapturing_an_already_captured_payment_is_refused(
    live_razorpay: RazorpayClient, captured_raw: dict[str, Any]
):
    """Why settle checks `captured` before calling capture, rather than always calling it.

    Razorpay rejects a second capture, so without that branch every settle of an
    auto-captured payment — which is what browser Checkout produces — would fail
    at the gateway and unwind a sale that had in fact succeeded.
    """
    payment = live_razorpay.fetch_payment(captured_raw["id"])

    with pytest.raises(RazorpayError) as caught:
        live_razorpay.capture_payment(payment["id"], payment["amount_inr"])
    assert caught.value.status_code in (400, 409)


def test_a_failed_attempt_is_not_mistaken_for_a_capture(
    live_razorpay: RazorpayClient, failed_raw: dict[str, Any]
):
    """A failed attempt must read as not-captured, or a sale ships against nothing."""
    payment = live_razorpay.fetch_payment(failed_raw["id"])

    assert payment["status"] == "failed"
    assert payment["captured"] is False
    # Razorpay explains itself on a failure, and that explanation is preserved
    # rather than flattened into a generic error.
    assert payment["error_code"]


# ── refunds, against money that really moved ───────────────────────────────────


def test_a_real_partial_refund_is_issued_and_visible(
    live_razorpay: RazorpayClient, captured_raw: dict[str, Any]
):
    """The compensation path, proved end to end for Rs 1.

    Partial rather than full on purpose: it exercises the same code, leaves the
    payment usable for every other test here, and can be repeated.
    """
    already_refunded = int(captured_raw.get("amount_refunded") or 0) // 100
    amount_inr = int(captured_raw["amount"]) // 100
    if amount_inr - already_refunded <= REFUND_HEADROOM_INR:
        pytest.skip("this payment has been refunded down to its headroom")

    refund = live_razorpay.refund_payment(captured_raw["id"], amount_inr=1)

    assert refund["id"].startswith("rfnd_")
    assert refund["payment_id"] == captured_raw["id"]
    assert refund["amount_inr"] == 1
    assert refund["status"] in ("processed", "pending")

    # The gateway agrees when asked again, rather than only in the create reply.
    fetched = live_razorpay.fetch_refund(refund["id"])
    assert fetched["amount_inr"] == 1
    assert fetched["payment_id"] == captured_raw["id"]

    listed = live_razorpay.fetch_payment_refunds(captured_raw["id"])
    assert refund["id"] in [item["id"] for item in listed]


def test_a_refund_larger_than_the_payment_is_refused(
    live_razorpay: RazorpayClient, captured_raw: dict[str, Any]
):
    """Over-refunding is the mirror of over-charging, and the gateway refuses it."""
    amount_inr = int(captured_raw["amount"]) // 100

    with pytest.raises(RazorpayError) as caught:
        live_razorpay.refund_payment(captured_raw["id"], amount_inr=amount_inr + 1_000)
    assert caught.value.status_code in (400, 404)


def test_refunding_a_failed_payment_is_refused(
    live_razorpay: RazorpayClient, failed_raw: dict[str, Any]
):
    """Nothing was captured, so there is nothing to give back."""
    with pytest.raises(RazorpayError) as caught:
        live_razorpay.refund_payment(failed_raw["id"], amount_inr=1)
    assert caught.value.status_code in (400, 404)


# ── the payment must belong to the order, checked against a real one ───────────


def test_settle_refuses_a_real_payment_made_against_another_order(
    db, make_offer, live_razorpay, live_mode, captured_raw: dict[str, Any]
):
    """The order-binding check, exercised with a genuinely captured payment.

    The payment in the account is real and really captured, but it was made
    against a different gateway order. Handing it to a fresh checkout is the exact
    shape of the bug this check closes, and using real gateway data rules out the
    possibility that it only works because a stub reports a convenient shape.

    Note what the payment would otherwise sail past: it is captured, so no capture
    call fails, and the amount check is the only other guard.
    """
    offer = make_offer("tier0")
    result = checkout_kernel.checkout(
        offer_id=offer["offer_id"],
        option_id=offer["option_id"],
        idempotency_key="live-foreign-payment-001",
        agent_id="agent-live-test",
        client_factory=lambda: live_razorpay,
    )
    assert result.status == checkout_kernel.STATUS_AWAITING_PAYMENT
    assert result.razorpay["order_id"] != captured_raw["order_id"]

    with pytest.raises(checkout_kernel.CheckoutError) as caught:
        checkout_kernel.settle(
            result.order_id,
            payment_id=captured_raw["id"],
            client=live_razorpay,
        )

    assert caught.value.code == "payment_order_mismatch"

    recorded = next(
        entry for entry in ledger.recent(100) if entry.event == "payment.order_mismatch"
    )
    assert recorded.payload["presented_razorpay_order_id"] == captured_raw["order_id"]
    assert recorded.payload["expected_razorpay_order_id"] == result.razorpay["order_id"]

    # Refused, not consumed: the order still awaits its own payment.
    assert orders.require(result.order_id)["state"] == orders.PENDING
