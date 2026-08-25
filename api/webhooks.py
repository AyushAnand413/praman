"""POST /webhooks/razorpay — Razorpay's independent confirmation.

This is the last step of the money path and the only one this system does not
initiate. Four properties, in the order they are enforced:

**Signature before trust.** The raw bytes are HMAC-verified before the body is
parsed. A bad or missing signature is a 401 and nothing is read from the payload —
not the event name, not the order id. An unverified webhook is an anonymous
stranger claiming a payment succeeded.

**Idempotent.** Razorpay retries until it gets a 2xx, so the same event arrives
more than once as a matter of course. The ledger is the record of what has been
processed, so it is also the authority on what has not: a redelivery is
recognised by its event id and returns the original outcome.

**Out-of-order tolerant.** `orders.advance` reports `advanced`, `already`, or
`stale` rather than raising, so `payment.captured` arriving before
`payment.authorized` does not fail and does not move the order backwards.

**2xx for everything the signature admits.** Once the signature checks out, an
unknown event, an unknown order, or an amount discrepancy all return 200 with the
outcome named and recorded. Returning an error would make Razorpay retry an event
this system will never handle differently, and a retry storm is worse than a
logged anomaly. Genuine internal faults are the exception: those return 500,
because there a retry is exactly what should happen.

This endpoint observes; it does not authorise. It advances state up to CAPTURED
and never to CONFIRMED — confirming an order also commits stock and closes the
ledger, and that belongs to the orchestrator that took the holds.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from settings import MissingSecret
from kernel import payments
from store import ledger, orders

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: The payload key that carries Razorpay's event id. Only webhook entries use it,
#: which is what makes it a safe key to detect a redelivery on.
EVENT_ID_KEY = "webhook_event_id"

#: Razorpay event -> the order state it implies. Events outside this map are
#: acknowledged and recorded, not acted on.
EVENT_STATES = {
    "payment.authorized": orders.AUTHORIZED,
    "payment.captured": orders.CAPTURED,
    "payment.failed": orders.FAILED,
    "refund.processed": orders.REFUNDED,
}

#: Which sub-entity of the payload carries the event's subject.
EVENT_ENTITY = {
    "payment.authorized": "payment",
    "payment.captured": "payment",
    "payment.failed": "payment",
    "refund.processed": "refund",
}


def _record(
    event: str,
    payload: dict[str, Any],
    *,
    money_delta_inr: int = 0,
    reason: str = "",
) -> int:
    """Append one webhook entry as the `razorpay` actor and return its seq."""
    entry = ledger.append(
        "razorpay",
        event,
        payload,
        money_delta_inr=money_delta_inr,
        reason=reason,
    )
    return entry.seq


def _locate_order(razorpay_event: str, entity: dict[str, Any]) -> dict[str, Any] | None:
    """Find the order this event is about, by whichever id the event carries."""
    if razorpay_event.startswith("refund."):
        payment_id = entity.get("payment_id")
        return orders.by_razorpay_payment(payment_id) if payment_id else None

    order_id = entity.get("order_id")
    if order_id:
        found = orders.by_razorpay_order(order_id)
        if found is not None:
            return found
    payment_id = entity.get("id")
    return orders.by_razorpay_payment(payment_id) if payment_id else None


@router.post("/razorpay", summary="Razorpay event callback")
async def razorpay_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    event_id: str | None = Header(default=None, alias="X-Razorpay-Event-Id"),
) -> dict[str, Any]:
    raw = await request.body()

    # Step 1, before anything else looks at the body.
    try:
        genuine = payments.verify_webhook_signature(raw, signature)
    except MissingSecret as exc:
        # No secret means no webhook can be verified, so none can be trusted.
        # Failing closed is the only safe direction here.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "webhook_secret_unconfigured",
                "message": (
                    "RAZORPAY_WEBHOOK_SECRET is not set, so no webhook can be "
                    "verified. Refusing to process an unverifiable payload."
                ),
            },
        ) from exc
    if not genuine:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_signature",
                "message": (
                    "X-Razorpay-Signature did not match an HMAC-SHA256 of the "
                    "request body. The payload was not read."
                ),
            },
        )

    # Only now is the body worth parsing.
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "malformed_body", "message": "signed body is not JSON"},
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"code": "malformed_body", "message": "expected a JSON object"},
        )

    # A signed body with no event id still needs a stable dedupe key, and its own
    # bytes are one: an identical redelivery hashes identically.
    delivery_id = event_id or f"sha256:{hashlib.sha256(raw).hexdigest()}"

    seen = ledger.find_by_payload(EVENT_ID_KEY, delivery_id)
    if seen:
        return {
            "received": True,
            "outcome": "duplicate",
            "event_id": delivery_id,
            "first_seen_seq": seen[0].seq,
            "detail": "already processed; Razorpay may stop retrying this event",
        }

    razorpay_event = str(body.get("event", ""))
    entity_name = EVENT_ENTITY.get(razorpay_event)
    entity = {}
    if entity_name:
        entity = (body.get("payload") or {}).get(entity_name, {}).get("entity", {}) or {}

    base: dict[str, Any] = {
        EVENT_ID_KEY: delivery_id,
        "razorpay_event": razorpay_event or "(absent)",
    }

    target_state = EVENT_STATES.get(razorpay_event)
    if target_state is None:
        seq = _record("webhook.ignored", base)
        return {
            "received": True,
            "outcome": "ignored",
            "event_id": delivery_id,
            "ledger_seq": seq,
            "detail": f"{razorpay_event or 'event'} is not one this system acts on",
        }

    order = _locate_order(razorpay_event, entity)
    if order is None:
        seq = _record(
            "webhook.unmatched",
            {
                **base,
                "razorpay_order_id": entity.get("order_id"),
                "razorpay_payment_id": entity.get("payment_id") or entity.get("id"),
            },
        )
        return {
            "received": True,
            "outcome": "unknown_order",
            "event_id": delivery_id,
            "ledger_seq": seq,
            "detail": "no local order carries these Razorpay ids",
        }

    order_id = order["order_id"]
    fields: dict[str, Any] = {}
    if razorpay_event.startswith("refund."):
        fields["razorpay_refund_id"] = entity.get("id")
        fields["razorpay_payment_id"] = entity.get("payment_id")
    else:
        fields["razorpay_payment_id"] = entity.get("id")
    fields = {k: v for k, v in fields.items() if v}

    try:
        outcome, order = orders.advance(order_id, target_state, **fields)
    except orders.IllegalTransition as exc:
        # A refund on a voided order, say. Real information, and not something a
        # retry will fix, so it is recorded rather than raised.
        seq = _record(
            "webhook.rejected",
            {**base, "order_id": order_id, "state": order["state"],
             "attempted_state": target_state, "detail": str(exc)},
        )
        return {
            "received": True,
            "outcome": "illegal_transition",
            "event_id": delivery_id,
            "order_id": order_id,
            "state": order["state"],
            "ledger_seq": seq,
            "detail": str(exc),
        }

    # Razorpay reports paise. A mismatch against the amount this system
    # authorised is the single most important thing a webhook can tell us, so it
    # is checked and named rather than assumed.
    discrepancy = None
    reported_paise = entity.get("amount")
    if isinstance(reported_paise, int):
        expected_paise = int(order["amount_inr"]) * 100
        if razorpay_event.startswith("refund."):
            # A partial refund is legitimate; only an overshoot is wrong.
            if reported_paise > expected_paise:
                discrepancy = (
                    f"refund of {reported_paise} paise exceeds the order's "
                    f"{expected_paise} paise"
                )
        elif reported_paise != expected_paise:
            discrepancy = (
                f"Razorpay reports {reported_paise} paise, order authorised "
                f"{expected_paise} paise"
            )

    money_delta = 0
    reason = ""
    if razorpay_event == "refund.processed" and isinstance(reported_paise, int):
        # A refund is the one webhook that moves money this system did not
        # already record an intent for, so it carries the delta and therefore
        # needs a reason.
        if reported_paise % 100 == 0:
            money_delta = -(reported_paise // 100)
            reason = (
                f"Razorpay refund {entity.get('id')} processed for order {order_id}"
            )
        else:
            # This system has no paise representation. Flooring would understate
            # a refund on the ledger, so the anomaly is named instead.
            discrepancy = (
                f"refund of {reported_paise} paise is not a whole number of rupees; "
                "recorded without a money delta"
            )

    seq = _record(
        f"webhook.{razorpay_event.replace('.', '_')}",
        {
            **base,
            "order_id": order_id,
            "offer_id": order["offer_id"],
            "session_id": order["session_id"],
            "state": order["state"],
            "transition": outcome,
            "razorpay_payment_id": order["razorpay_payment_id"],
            "amount_paise_reported": reported_paise,
            "discrepancy": discrepancy,
        },
        money_delta_inr=money_delta,
        reason=reason,
    )

    return {
        "received": True,
        "outcome": outcome,
        "event_id": delivery_id,
        "order_id": order_id,
        "state": order["state"],
        "ledger_seq": seq,
        "discrepancy": discrepancy,
        "audit_url": f"/audit/{order_id}",
    }
