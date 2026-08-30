"""Tier-2 human approval — what happens to an order the gate held.

A held order is stopped, not queued for eventual automatic release. Three things
can move it, and all three are a person:

    APPROVE   resume the checkout flow from the stock hold onward
    REJECT    void the order
    COUNTER   void the original terms and issue a fresh offer to accept or not

**No timeout approves.** There is no expiry that turns a pending approval into a
yes. If the merchant never decides, the order never charges and never ships. That
is the correct failure direction for money, and it is the whole reason the tier
exists — a human gate that resolves itself after twenty minutes is a delay, not a
control.

APPROVE deliberately re-validates. The approval may come minutes or hours after
the order was held, and stock, prices, and the daily budget can all have moved.
Approving means "yes, this transaction is acceptable to me", not "skip the
checks" — so the bounds run again, and an approved order can still fail on stock.
Refusing at that point is better than promising a unit that is no longer there.

**A held order holds no stock.** Reservations expire in 120 seconds, which cannot
span human deliberation, so the checkout flow stops before the hold and this
module takes it at approval time. That is also why rejecting releases nothing:
there was never anything to release.

COUNTER does not edit the order. The original is voided and a new offer created,
because a buyer who agreed to one price has not agreed to a different one — the
agent has to accept the counter explicitly. The counter is priced, bounded, gated
and signed like any other offer, which means a counter above the autonomous limit
is held again when the agent accepts it. The merchant then approves terms they
set themselves: a redundant click, and the alternative is a pre-approved flag
that skips the gate. A gate with a bypass is not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from settings import OFFER_TTL_SECONDS
from kernel import budgets, mode, receipt as receipts, stock
from kernel.bounds import LineItem, ROLE_BASE, evaluate_checkout, evaluate_offer
from kernel.checkout import (
    STATUS_AWAITING_PAYMENT,
    STATUS_CONFIRMED,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    STATUS_SHADOW,
    CheckoutError,
    _default_client,
    _line_items,
    _private_by_sku,
    audit_url_for,
    settle,
)
from kernel.gates import assign_tier
from kernel.payments import RazorpayClient
from kernel.relations import related_by_base_for_items
from store import approvals as approvals_store
from store import ids, ledger, offers, orders, sessions
from store.timestamps import plus_seconds, to_ts, utc_now

#: What the agent polling a countered order is told.
STATUS_COUNTERED = "countered"


class ApprovalError(RuntimeError):
    """A decision that cannot be applied. Carries the reason the caller sees."""

    def __init__(self, message: str, *, code: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class ApprovalOutcome:
    approval_id: str
    order_id: str
    decision: str
    order_state: str
    status: str
    amount_inr: int
    audit_url: str
    detail: str = ""
    counter_offer_id: str | None = None
    counter_amount_inr: int | None = None
    razorpay: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "approval_id": self.approval_id,
            "order_id": self.order_id,
            "decision": self.decision,
            "order_state": self.order_state,
            "status": self.status,
            "amount_inr": self.amount_inr,
            "currency": "INR",
            "audit_url": self.audit_url,
            "detail": self.detail,
        }
        if self.counter_offer_id:
            body["counter_offer_id"] = self.counter_offer_id
            body["counter_amount_inr"] = self.counter_amount_inr
        if self.razorpay:
            body["razorpay"] = self.razorpay
        return body


def pending_queue() -> list[dict[str, Any]]:
    """The merchant's decision queue, with each order and its receipt attached."""
    queue: list[dict[str, Any]] = []
    for approval in approvals_store.pending():
        order = orders.get(approval["order_id"])
        offer = offers.get(approval["offer_id"])
        queue.append(
            {
                **approval,
                "order_state": order["state"] if order else None,
                "gate_tier": order["gate_tier"] if order else None,
                "policy_receipt": offer["policy_receipt"] if offer else None,
            }
        )
    return queue


def _require_pending(approval_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load an approval that is still open, plus the HELD order behind it."""
    approval = approvals_store.get(approval_id)
    if approval is None:
        raise ApprovalError(
            f"no approval {approval_id!r}", code="approval_not_found", http_status=404
        )
    if approval["state"] != approvals_store.PENDING:
        raise ApprovalError(
            f"approval {approval_id} is already {approval['state']}; a decision is "
            "made once",
            code="already_decided",
            http_status=409,
        )
    order = orders.require(approval["order_id"])
    if order["state"] != orders.HELD:
        raise ApprovalError(
            f"order {order['order_id']} is {order['state']}, not HELD; there is "
            "nothing to release",
            code="order_not_held",
            http_status=409,
        )
    return approval, order


# ---------------------------------------------------------------------------
# APPROVE
# ---------------------------------------------------------------------------


def approve(
    approval_id: str,
    *,
    decided_by: str,
    note: str | None = None,
    payment_id: str | None = None,
    now: datetime | None = None,
    client_factory: Callable[[], RazorpayClient] = _default_client,
) -> ApprovalOutcome:
    """Release a held order and resume the money path.

    Revalidation runs before the decision is recorded. The human authorised the
    transaction, not a bypass of the bounds, and the world may have moved while
    the order waited.
    """
    moment = now or utc_now()
    approval, order = _require_pending(approval_id)
    order_id = order["order_id"]
    amount_inr = int(order["amount_inr"])
    offer = offers.require(approval["offer_id"])
    option = offers.option(offer, order["option_id"])
    items = _line_items(option)

    evaluation = evaluate_checkout(
        items,
        private_by_sku=_private_by_sku(items),
        available_by_sku=stock.available_for(
            (item.sku for item in items), now=moment
        ),
        spent_today_inr=budgets.spent(),
        # The offer's own clock is not re-applied. The order waited because this
        # system asked a human to look at it, and expiring it for the time that
        # took would charge the buyer for the merchant's deliberation. Every
        # other bound is re-checked in full.
        issued_at=moment,
        now=moment,
        idempotency_key=f"approval:{approval_id}",
        ttl_seconds=OFFER_TTL_SECONDS,
    )
    # The tier is recomputed for the record, not to route on. An order over the
    # autonomous limit is still over it after approval — that is precisely what
    # the human just authorised — so routing on this again would never terminate.
    gate = assign_tier(
        total_inr=evaluation.total_inr,
        discount_pct=evaluation.discount_pct,
        tripped_bounds=evaluation.tripped_bounds,
    )
    policy_receipt = receipts.issue(
        offer_id=offer["offer_id"],
        evaluation=evaluation,
        gate=gate,
        reasons=(
            f"held for merchant approval; approved by {decided_by}",
            *((note,) if note else ()),
        ),
    ).as_payload()

    if evaluation.offer_failed:
        return _refuse_after_approval(
            approval_id=approval_id,
            order_id=order_id,
            amount_inr=amount_inr,
            decided_by=decided_by,
            detail=evaluation.failure_detail or "revalidation failed",
            policy_receipt=policy_receipt,
        )

    approvals_store.decide(
        approval_id,
        state=approvals_store.APPROVED,
        decided_by=decided_by,
        note=note,
    )
    ledger.append(
        "merchant",
        "approval.granted",
        {
            "approval_id": approval_id,
            "order_id": order_id,
            "amount_inr": amount_inr,
            "decided_by": decided_by,
            "note": note,
            "revalidation": evaluation.as_payload(),
            "policy_receipt": policy_receipt,
        },
        reason=(
            f"{decided_by} approved order {order_id} for INR {amount_inr}"
            + (f": {note}" if note else "")
        ),
    )

    return _resume_payment(
        approval_id=approval_id,
        order=order,
        offer=offer,
        items=items,
        evaluation=evaluation,
        policy_receipt=policy_receipt,
        decided_by=decided_by,
        payment_id=payment_id,
        moment=moment,
        client_factory=client_factory,
    )


def _resume_payment(
    *,
    approval_id: str,
    order: dict[str, Any],
    offer: dict[str, Any],
    items: list[LineItem],
    evaluation: Any,
    policy_receipt: dict[str, Any],
    decided_by: str,
    payment_id: str | None,
    moment: datetime,
    client_factory: Callable[[], RazorpayClient],
) -> ApprovalOutcome:
    """Steps 4 to 9 of the checkout flow, entered from a merchant's yes."""
    order_id = order["order_id"]
    amount_inr = int(order["amount_inr"])
    discount_inr = evaluation.discount_inr
    shadow = mode.is_shadow()

    quantities: dict[str, int] = {}
    for item in items:
        quantities[item.sku] = quantities.get(item.sku, 0) + item.qty
    try:
        holds = stock.reserve_cart(
            quantities, session_id=order["session_id"], now=moment
        )
    except stock.InsufficientStock as exc:
        return _refuse_after_approval(
            approval_id=approval_id,
            order_id=order_id,
            amount_inr=amount_inr,
            decided_by=decided_by,
            detail=str(exc),
            policy_receipt=policy_receipt,
            approval_already_recorded=True,
        )

    budget_reserved = 0
    if not shadow and discount_inr > 0:
        try:
            budgets.check_and_accrue(discount_inr)
            budget_reserved = discount_inr
        except budgets.BudgetExceeded as exc:
            stock.release(holds.values())
            return _refuse_after_approval(
                approval_id=approval_id,
                order_id=order_id,
                amount_inr=amount_inr,
                decided_by=decided_by,
                detail=str(exc),
                policy_receipt=policy_receipt,
                approval_already_recorded=True,
            )

    # Record what this order is holding before anything external happens. The
    # approved order can also end in `awaiting_payment`, and the settle that
    # follows is a separate request that knows only the order id — same handoff as
    # the auto path, same reason it has to be persisted.
    orders.record_reservation(
        order_id,
        hold_ids=sorted(holds.values()),
        budget_reserved_inr=budget_reserved,
    )

    # Intent on the ledger before the external call, exactly as on the auto path.
    ledger.append(
        "policy_kernel",
        "payment.intent",
        {
            "order_id": order_id,
            "offer_id": offer["offer_id"],
            "option_id": order["option_id"],
            "amount_inr": amount_inr,
            "discount_inr": discount_inr,
            "approval_id": approval_id,
            "approved_by": decided_by,
            "holds": sorted(holds.values()),
            "resumed_after_approval": True,
            "policy_receipt": policy_receipt,
            "would_have_charged": shadow,
        },
        money_delta_inr=amount_inr,
        reason=(
            f"intent to charge INR {amount_inr} for order {order_id}, resumed "
            f"after approval by {decided_by}"
        ),
    )

    if shadow:
        stock.release(holds.values())
        orders.clear_reservation(order_id)
        ledger.append(
            "policy_kernel",
            "payment.shadow_skipped",
            {
                "order_id": order_id,
                "approval_id": approval_id,
                "amount_inr": amount_inr,
                "would_have_charged": True,
                "razorpay_called": False,
            },
            money_delta_inr=amount_inr,
            reason=(
                f"shadow mode: approved order {order_id} would have charged INR "
                f"{amount_inr}. No gateway call was made and no stock committed."
            ),
        )
        return ApprovalOutcome(
            approval_id=approval_id,
            order_id=order_id,
            decision=approvals_store.APPROVED,
            order_state=orders.HELD,
            status=STATUS_SHADOW,
            amount_inr=amount_inr,
            audit_url=audit_url_for(order_id),
            detail=f"approved by {decided_by}; shadow mode made no gateway call",
        )

    client = client_factory()
    try:
        gateway_order = client.create_order(
            amount_inr,
            receipt=order_id,
            notes={
                "order_id": order_id,
                "approval_id": approval_id,
                "approved_by": decided_by,
            },
        )
    except Exception as exc:
        stock.release(holds.values())
        if budget_reserved:
            budgets.release(budget_reserved)
        orders.clear_reservation(order_id)
        ledger.append(
            "policy_kernel",
            "payment.failed",
            {
                "order_id": order_id,
                "approval_id": approval_id,
                "stage": "order_create",
                "error": str(exc)[:400],
            },
            reason=(
                f"gateway order creation failed for approved order {order_id}: "
                f"{str(exc)[:200]}"
            ),
        )
        raise CheckoutError(
            f"payment gateway rejected order creation: {exc}",
            code="gateway_error",
            http_status=502,
        ) from exc

    orders.attach_payment_ids(order_id, razorpay_order_id=gateway_order["id"])
    ledger.append(
        "razorpay",
        "razorpay.order.created",
        {
            "order_id": order_id,
            "approval_id": approval_id,
            "razorpay_order_id": gateway_order["id"],
            "amount_inr": gateway_order["amount_inr"],
            "status": gateway_order["status"],
        },
        reason=(
            f"gateway order {gateway_order['id']} created for approved order "
            f"{order_id}"
        ),
    )

    if payment_id is None:
        # The buyer completes Checkout, then `settle` captures. The order stays
        # HELD until a payment is authorised against it. The reservation recorded
        # above is what that later settle reads to commit the right holds.
        return ApprovalOutcome(
            approval_id=approval_id,
            order_id=order_id,
            decision=approvals_store.APPROVED,
            order_state=orders.HELD,
            status=STATUS_AWAITING_PAYMENT,
            amount_inr=amount_inr,
            audit_url=audit_url_for(order_id),
            razorpay={
                "order_id": gateway_order["id"],
                "amount_inr": gateway_order["amount_inr"],
                "currency": gateway_order["currency"],
                "key_id": client.key_id,
            },
            detail=f"approved by {decided_by}; awaiting payment completion",
        )

    settled = settle(
        order_id,
        payment_id=payment_id,
        holds=list(holds.values()),
        client=client,
        budget_reserved=budget_reserved,
    )
    return ApprovalOutcome(
        approval_id=approval_id,
        order_id=order_id,
        decision=approvals_store.APPROVED,
        order_state=settled["state"],
        status=STATUS_CONFIRMED,
        amount_inr=amount_inr,
        audit_url=audit_url_for(order_id),
        razorpay={
            "order_id": gateway_order["id"],
            "payment_id": payment_id,
            "amount_inr": amount_inr,
            "currency": "INR",
        },
        detail=f"approved by {decided_by} and captured",
    )


# ---------------------------------------------------------------------------
# REJECT
# ---------------------------------------------------------------------------


def reject(
    approval_id: str,
    *,
    decided_by: str,
    note: str | None = None,
) -> ApprovalOutcome:
    """Void a held order. Nothing was charged, so nothing needs refunding.

    No stock is released either: a held order never took a reservation.
    """
    approval, order = _require_pending(approval_id)
    order_id = order["order_id"]
    amount_inr = int(order["amount_inr"])

    approvals_store.decide(
        approval_id,
        state=approvals_store.REJECTED,
        decided_by=decided_by,
        note=note,
    )
    orders.transition(order_id, orders.VOIDED, expect=orders.HELD)
    ledger.append(
        "merchant",
        "approval.rejected",
        {
            "approval_id": approval_id,
            "order_id": order_id,
            "amount_inr": amount_inr,
            "decided_by": decided_by,
            "note": note,
        },
        reason=(
            f"{decided_by} rejected order {order_id}; it was never charged"
            + (f": {note}" if note else "")
        ),
    )
    return ApprovalOutcome(
        approval_id=approval_id,
        order_id=order_id,
        decision=approvals_store.REJECTED,
        order_state=orders.VOIDED,
        status=STATUS_REJECTED,
        amount_inr=amount_inr,
        audit_url=audit_url_for(order_id),
        detail=f"rejected by {decided_by}" + (f": {note}" if note else ""),
    )


# ---------------------------------------------------------------------------
# COUNTER
# ---------------------------------------------------------------------------


def counter(
    approval_id: str,
    *,
    decided_by: str,
    counter_amount_inr: int,
    note: str | None = None,
    now: datetime | None = None,
) -> ApprovalOutcome:
    """Void the original terms and issue a new offer at the merchant's price.

    The counter reprices the base line and leaves upsell prices untouched, so
    there is one well-defined adjustment rather than an allocation rule spreading
    a total across lines. The new terms then go through the bounds, the gate and
    the receipt signer like any other offer — a counter below the floor price is
    refused, because the bounds are the merchant's standing policy and changing
    them is a code change, not a click on one order.
    """
    moment = now or utc_now()
    approval, order = _require_pending(approval_id)
    order_id = order["order_id"]
    amount_inr = int(order["amount_inr"])
    original = offers.require(approval["offer_id"])
    option = offers.option(original, order["option_id"])
    items = _line_items(option)

    counter_amount_inr = int(counter_amount_inr)
    counter_items, counter_option = _reprice(
        option,
        items,
        counter_amount_inr,
        original_offer_id=original["offer_id"],
        original_total_inr=amount_inr,
        note=note,
    )

    evaluation = evaluate_offer(
        counter_items,
        private_by_sku=_private_by_sku(counter_items),
        available_by_sku=stock.available_for(
            (item.sku for item in counter_items), now=moment
        ),
        # The counter is the merchant's own act, not another automated offer, so
        # it does not consume the session's offer quota.
        offers_made=0,
        spent_today_inr=budgets.spent(),
        now=moment,
        related_by_base=related_by_base_for_items(counter_items),
    )
    if evaluation.offer_failed:
        raise ApprovalError(
            f"a counter at INR {counter_amount_inr} does not pass the bounds: "
            f"{evaluation.failure_detail}",
            code="counter_rejected_by_bounds",
            http_status=422,
        )

    counter_gate = assign_tier(
        total_inr=evaluation.total_inr,
        discount_pct=evaluation.discount_pct,
        tripped_bounds=evaluation.tripped_bounds,
    )
    counter_offer_id = ids.offer_id()
    # A new authorisation gets a new signature. Reusing the original receipt
    # would attach a decision about one price to a different one.
    counter_receipt = receipts.issue(
        offer_id=counter_offer_id,
        evaluation=evaluation,
        gate=counter_gate,
        reasons=(
            counter_option["human_reason"],
            f"countered by {decided_by} in place of INR {amount_inr}",
        ),
    ).as_payload()

    approvals_store.decide(
        approval_id,
        state=approvals_store.COUNTERED,
        decided_by=decided_by,
        counter_amount_inr=counter_amount_inr,
        counter_offer_id=counter_offer_id,
        note=note,
    )
    orders.transition(order_id, orders.VOIDED, expect=orders.HELD)
    offers.create(
        offer_id=counter_offer_id,
        session_id=order["session_id"],
        base_sku=original["base_sku"],
        options=[counter_option],
        total_inr=counter_amount_inr,
        gate_tier=counter_gate.tier,
        policy_receipt=counter_receipt,
        policy_mode=mode.mode_value(),
        expires_at=to_ts(plus_seconds(moment, OFFER_TTL_SECONDS)),
        now=moment,
    )
    ledger.append(
        "merchant",
        "approval.countered",
        {
            "approval_id": approval_id,
            "order_id": order_id,
            "original_amount_inr": amount_inr,
            "counter_amount_inr": counter_amount_inr,
            "counter_offer_id": counter_offer_id,
            "counter_gate_tier": counter_gate.tier,
            "decided_by": decided_by,
            "note": note,
            "policy_receipt": counter_receipt,
        },
        reason=(
            f"{decided_by} countered order {order_id}: INR {amount_inr} -> INR "
            f"{counter_amount_inr}. The original was voided and offer "
            f"{counter_offer_id} issued for the agent to accept or decline."
        ),
    )
    return ApprovalOutcome(
        approval_id=approval_id,
        order_id=order_id,
        decision=approvals_store.COUNTERED,
        order_state=orders.VOIDED,
        status=STATUS_COUNTERED,
        amount_inr=amount_inr,
        audit_url=audit_url_for(order_id),
        counter_offer_id=counter_offer_id,
        counter_amount_inr=counter_amount_inr,
        detail=(
            f"countered by {decided_by} at INR {counter_amount_inr}; present "
            f"offer {counter_offer_id} at checkout to accept"
        ),
    )


def _reprice(
    option: dict[str, Any],
    items: list[LineItem],
    counter_amount_inr: int,
    *,
    original_offer_id: str,
    original_total_inr: int,
    note: str | None,
) -> tuple[list[LineItem], dict[str, Any]]:
    """Rebuild the cart with the base line repriced to hit the counter total.

    Only the base line moves. Upsells keep the prices they were offered at, so a
    counter is one unambiguous change to the main item rather than a
    redistribution the buyer would have to reverse-engineer.
    """
    if counter_amount_inr <= 0:
        raise ApprovalError(
            "a counter-offer must name a positive amount",
            code="invalid_counter_amount",
        )

    base_index = next(
        (i for i, item in enumerate(items) if item.role == ROLE_BASE), None
    )
    if base_index is None:
        raise ApprovalError(
            "the stored offer has no base line to reprice",
            code="malformed_offer",
            http_status=500,
        )
    base = items[base_index]
    upsell_total = sum(
        item.offered_total_inr for i, item in enumerate(items) if i != base_index
    )
    base_total = counter_amount_inr - upsell_total
    if base_total < base.qty:
        raise ApprovalError(
            f"INR {counter_amount_inr} leaves INR {base_total} for {base.qty} x "
            f"{base.sku} after INR {upsell_total} of accessories; counter above "
            "the accessory total, or counter on an offer without them",
            code="invalid_counter_amount",
            http_status=422,
        )
    if base_total % base.qty != 0:
        raise ApprovalError(
            f"INR {base_total} does not divide evenly across {base.qty} x "
            f"{base.sku}. This system prices in whole rupees and will not round "
            f"money, so the counter must leave a multiple of {base.qty}",
            code="invalid_counter_amount",
            http_status=422,
        )

    counter_items = list(items)
    counter_items[base_index] = LineItem(
        sku=base.sku,
        qty=base.qty,
        list_price_inr=base.list_price_inr,
        offered_price_inr=base_total // base.qty,
        role=ROLE_BASE,
    )

    counter_option = {
        **option,
        "option_id": "counter",
        "items": [
            {
                "sku": item.sku,
                "qty": item.qty,
                "list_price_inr": item.list_price_inr,
                "offered_price_inr": item.offered_price_inr,
                "role": item.role,
            }
            for item in counter_items
        ],
        "total_inr": counter_amount_inr,
        "human_reason": (
            note
            or f"Merchant counter-offer: INR {counter_amount_inr} instead of INR "
               f"{original_total_inr}."
        ),
        "countered_from": {
            "offer_id": original_offer_id,
            "option_id": option.get("option_id"),
            "amount_inr": original_total_inr,
        },
    }
    return counter_items, counter_option


# ---------------------------------------------------------------------------
# Shared refusal path, and the buyer agent's poll
# ---------------------------------------------------------------------------


def _refuse_after_approval(
    *,
    approval_id: str,
    order_id: str,
    amount_inr: int,
    decided_by: str,
    detail: str,
    policy_receipt: dict[str, Any],
    approval_already_recorded: bool = False,
) -> ApprovalOutcome:
    """An approval that cannot be honoured because the world moved.

    When the merchant's yes was already recorded it stays recorded — they did say
    yes, and the audit trail should show the refusal coming from policy
    re-evaluation rather than from the merchant changing their mind.
    """
    if not approval_already_recorded:
        approvals_store.decide(
            approval_id,
            state=approvals_store.REJECTED,
            decided_by="policy_kernel",
            note=f"revalidation refused it: {detail}",
        )
    orders.transition(order_id, orders.FAILED)
    ledger.append(
        "policy_kernel",
        "approval.revalidation_failed",
        {
            "approval_id": approval_id,
            "order_id": order_id,
            "amount_inr": amount_inr,
            "approved_by": decided_by,
            "detail": detail,
            "policy_receipt": policy_receipt,
        },
        reason=(
            f"order {order_id} was approved by {decided_by} but failed "
            f"revalidation and was not charged: {detail}"
        ),
    )
    return ApprovalOutcome(
        approval_id=approval_id,
        order_id=order_id,
        decision=(
            approvals_store.APPROVED
            if approval_already_recorded
            else approvals_store.REJECTED
        ),
        order_state=orders.FAILED,
        status=STATUS_REJECTED,
        amount_inr=amount_inr,
        audit_url=audit_url_for(order_id),
        detail=f"approved, but revalidation refused it: {detail}",
    )


def order_status(order_id: str) -> dict[str, Any]:
    """What the buyer agent sees when it polls a held order.

    The poll URL handed out with a Tier-2 hold resolves here. It reports the
    order's state and, where one exists, the approval's, so an agent can tell
    "still waiting on a person" from "decided" without guessing.
    """
    order = orders.require(order_id)
    history = approvals_store.for_order(order_id)
    # Newest last, and the newest is the one that governs: an order can be
    # requested, countered, and requested again.
    approval = history[-1] if history else None
    session = sessions.get(order["session_id"])
    body: dict[str, Any] = {
        "order_id": order_id,
        "state": order["state"],
        "amount_inr": int(order["amount_inr"]),
        "currency": "INR",
        "gate_tier": order["gate_tier"],
        "policy_mode": order["policy_mode"],
        "audit_url": audit_url_for(order_id),
        "agent_id": session["agent_id"] if session else None,
    }
    if approval:
        body["approval"] = {
            "approval_id": approval["approval_id"],
            "state": approval["state"],
            "decided_by": approval["decided_by"],
            "decided_at": approval["decided_at"],
            "counter_amount_inr": approval["counter_amount_inr"],
            "note": approval["note"],
        }
        if approval["state"] == approvals_store.PENDING:
            body["status"] = STATUS_PENDING_APPROVAL
            body["detail"] = (
                "waiting on a merchant decision. No timeout approves this order."
            )
        elif approval["state"] == approvals_store.COUNTERED:
            # The counter is a real, bounded, signed offer the agent can
            # accept explicitly — surface it so polling is all a buyer agent
            # needs to complete the negotiation without a human relay.
            body["status"] = approval["state"].lower()
            body["counter_offer_id"] = approval["counter_offer_id"]
            body["counter_amount_inr"] = approval["counter_amount_inr"]
        else:
            body["status"] = approval["state"].lower()
    else:
        body["status"] = order["state"].lower()
    return body
