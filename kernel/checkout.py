"""The checkout orchestrator — the complete money path, in a fixed order.

This module is the one place where a purchase actually happens, and the order of
its steps is the security property:

     1. Idempotency   key claimed before anything else
     2. Offer load    exists, not expired, names a real option
     3. Revalidate    every bound and the mandate, re-run from scratch
        Gate          Tier 2 halts here: order HELD, approval PENDING
        Authority     Tier 1 halts here unless a valid mandate was presented
     4. Stock hold    atomic reserve
     5. Ledger        intent recorded BEFORE any external call
     6. Razorpay      order created, amount fixed server-side
     7. Razorpay      payment captured
     8. Stock commit  decrement against the hold
     9. Ledger        payment confirmed
    10. Webhook       Razorpay confirms independently (see api/webhooks.py)
    11. Response      confirmation plus an audit url

Two of those orderings are not negotiable.

**Step 1 first.** An agent that retries on timeout must not be charged twice, and
a check that runs after the offer lookup is a check that can be skipped by a
request that fails the lookup on its second attempt.

**Step 5 before step 6.** The intent reaches the ledger before the external call.
If this process dies mid-payment, the ledger still records what was attempted,
which is the difference between a reconcilable gap and a mystery.

**Amount authority.** The charge amount is read from the stored offer, by
option_id. The request body cannot carry a price. This is why price tampering is
structurally impossible here rather than merely validated against.

**Mandate authority.** The gate decides what authority a cart needs; this module
is what makes that decision binding. A tier that requires a mandate and did not
get a valid one is refused here, before any hold and any gateway call. Verifying
a mandate when one happens to be attached is not the same guarantee — the tier is
what obliges the caller to attach one.

Two shapes of completion, because of how Razorpay test mode works. Creating a
payment server-side requires per-account activation, so the normal path is: this
module creates the gateway order and returns it, the buyer completes Checkout,
and `settle` captures the resulting payment id. When a caller already holds an
authorized payment id, `checkout` runs straight through and captures in one call.

**A reservation outlives the request that took it.** The two-step path takes out
stock holds and reserves discount budget, then returns. `settle` arrives later
knowing only an order id, so what was reserved is written on the order row and
read back there — otherwise the stock would never decrement and the budget would
never come back. `expire_abandoned` closes the case where the buyer never returns
at all.

**Shadow mode changes what happens, not what is decided.** Every bound, the gate,
the mandate, and the receipt all run identically. Steps 6 to 8 are skipped, no
gateway is constructed, no stock is committed, and no budget is consumed. The
ledger records the amount that *would* have moved, tagged `would_have_charged`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx

from settings import CHECKOUT_ABANDONED_AFTER_SECONDS, OFFER_TTL_SECONDS
from kernel import budgets, idempotency, mode, receipt as receipts, stock
from kernel.bounds import (
    LineItem,
    ROLE_BASE,
    check_idempotency_key,
    evaluate_checkout,
)
from kernel.gates import TIER_HUMAN, TIER_MANDATE, assign_tier
from kernel.payments import RazorpayClient, RazorpayError
from mandate import verifier
from store import approvals as approvals_store
from store import catalog, ids, ledger, offers, orders, pairings, tenancy
from store.timestamps import parse, plus_seconds, utc_now

#: What the buyer agent is told. These strings are part of the agent-facing
#: contract, so they are constants rather than inline literals.
STATUS_CONFIRMED = "confirmed"
STATUS_AWAITING_PAYMENT = "awaiting_payment"
STATUS_PENDING_APPROVAL = "pending_merchant_approval"
STATUS_REJECTED = "rejected"
STATUS_REPLAYED = "replayed"
STATUS_SHADOW = "shadow_complete"


class CheckoutError(RuntimeError):
    """A checkout that cannot proceed. Carries the reason the caller should see."""

    def __init__(self, message: str, *, code: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class OversoldFault(CheckoutError):
    """A payment was captured for stock that no longer exists.

    Raised only after the compensation saga has fully run: by the time the
    caller sees this, the refund has been issued, the order is REFUNDED, the
    SKU is self-healed, and the whole sequence is on the ledger. `payload` is
    the structured OVERSOLD_MERCHANT_FAULT body the buyer agent receives —
    fault attributed, money returned, remedy offered, retry declared safe.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(
            str(payload.get("human_message", "oversold; refunded automatically")),
            code=str(payload.get("code", "OVERSOLD_MERCHANT_FAULT")),
            http_status=409,
        )
        self.payload = payload


@dataclass(frozen=True)
class CheckoutResult:
    order_id: str
    status: str
    state: str
    amount_inr: int
    gate_tier: int
    policy_mode: str
    audit_url: str
    policy_receipt: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    razorpay: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None
    poll_url: str | None = None
    would_have_charged: bool = False

    def as_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "order_id": self.order_id,
            "status": self.status,
            "state": self.state,
            "amount_inr": self.amount_inr,
            "currency": "INR",
            "gate_tier": self.gate_tier,
            "policy_mode": self.policy_mode,
            "policy_receipt": self.policy_receipt,
            "reasons": list(self.reasons),
            "audit_url": self.audit_url,
        }
        if self.razorpay:
            body["razorpay"] = self.razorpay
        if self.approval_id:
            body["approval_id"] = self.approval_id
        if self.poll_url:
            body["poll_url"] = self.poll_url
        if self.would_have_charged:
            body["would_have_charged"] = True
        return body


def audit_url_for(order_id: str) -> str:
    return f"/audit/{order_id}"


def _default_client() -> RazorpayClient:
    return RazorpayClient()


def _line_items(option: dict[str, Any]) -> list[LineItem]:
    """Rebuild the priced cart from the stored option.

    Rebuilt from storage, never from the request, so the items being bounded are
    the items that were authorised.
    """
    items: list[LineItem] = []
    for raw in option.get("items", []):
        items.append(
            LineItem(
                sku=raw["sku"],
                qty=int(raw.get("qty", 1)),
                list_price_inr=int(raw["list_price_inr"]),
                offered_price_inr=int(raw["offered_price_inr"]),
                role=raw.get("role", ROLE_BASE),
            )
        )
    if not items:
        raise CheckoutError(
            "the stored offer option contains no line items",
            code="malformed_offer",
            http_status=500,
        )
    return items


def _private_by_sku(items: list[LineItem]) -> dict[str, dict[str, Any]]:
    private: dict[str, dict[str, Any]] = {}
    for item in items:
        row = catalog.cache.private(item.sku)
        if row is None:
            raise CheckoutError(
                f"SKU {item.sku} is no longer in the catalog",
                code="unknown_sku",
            )
        private[item.sku] = row
    return private


def _refuse(
    idempotency_key: str,
    message: str,
    *,
    code: str,
    http_status: int = 400,
) -> CheckoutError:
    """Build a refusal for a failure that happened before any external call.

    Drops the idempotency claim on the way out. Nothing was charged and no order
    exists, so the key protects nothing — while a claim with no recorded outcome
    would answer the caller's next attempt with `request_in_flight` forever. The
    case that matters is a Tier 1 cart refused for a missing mandate: the agent is
    expected to attach one and retry the identical request, and it cannot do that
    if the first attempt burned the key.

    Refusals that DO create an order (a failed bound, an out-of-stock cart) go on
    completing the key as normal: there the outcome is real and must be replayable.
    """
    idempotency.release(idempotency_key)
    return CheckoutError(message, code=code, http_status=http_status)


def checkout(
    *,
    offer_id: str,
    option_id: str,
    idempotency_key: str | None,
    agent_id: str,
    mandate_token: str | None = None,
    payment_id: str | None = None,
    now: datetime | None = None,
    client_factory: Callable[[], RazorpayClient] = _default_client,
) -> CheckoutResult:
    """Run the checkout flow. The only entry point that can lead to a charge."""
    moment = now or utc_now()

    # ── Step 1. Idempotency, before anything else ────────────────────────────
    key_bound = check_idempotency_key(key=idempotency_key)
    if not key_bound.passed:
        ledger.append(
            "policy_kernel",
            "checkout.rejected",
            {
                "reason_code": "idempotency_key_required",
                "bound": key_bound.as_payload(),
                "offer_id": offer_id,
                "agent_id": agent_id,
            },
            reason=(
                "checkout refused before any lookup: "
                f"{key_bound.detail}"
            ),
        )
        raise CheckoutError(key_bound.detail, code="idempotency_key_required")

    assert idempotency_key is not None  # guaranteed by the bound above
    request_fingerprint = idempotency.fingerprint(
        {
            "offer_id": offer_id,
            "option_id": option_id,
            "agent_id": agent_id,
        }
    )
    claim = idempotency.claim(
        idempotency_key, request_fingerprint=request_fingerprint
    )
    if claim.is_replay:
        stored = dict(claim.response or {})
        ledger.append(
            "policy_kernel",
            "checkout.idempotent_replay",
            {
                "idempotency_key": idempotency_key,
                "order_id": claim.order_id,
                "offer_id": offer_id,
            },
            reason=(
                f"idempotency key {idempotency_key} replayed; returned the "
                "original outcome without contacting the payment gateway"
            ),
        )
        stored["status"] = STATUS_REPLAYED
        return _result_from_payload(stored)
    if claim.in_progress:
        raise idempotency.RequestInFlight(idempotency_key)

    # ── Step 2. Offer load ───────────────────────────────────────────────────
    offer = offers.get(offer_id)
    if offer is None:
        raise _refuse(
            idempotency_key,
            f"no offer {offer_id!r}",
            code="offer_not_found",
            http_status=404,
        )
    try:
        option = offers.option(offer, option_id)
    except offers.OptionNotFound as exc:
        raise _refuse(
            idempotency_key, str(exc), code="option_not_found", http_status=404
        ) from exc

    session_id = offer["session_id"]
    # The amount comes from here and from nowhere else.
    amount_inr = int(option["total_inr"])
    base_sku = offer["base_sku"]

    # ── Step 3. Revalidate everything ────────────────────────────────────────
    # Both of these refuse on a catalog that moved under a stored offer, which is
    # a refusal taken before anything external happens.
    try:
        items = _line_items(option)
        private_by_sku = _private_by_sku(items)
    except CheckoutError:
        idempotency.release(idempotency_key)
        raise
    available_by_sku = stock.available_for((item.sku for item in items), now=moment)

    # Reservations from abandoned two-step checkouts still hold discount budget.
    # Sweeping here rather than on a schedule keeps `budgets.spent()` below honest
    # without adding a background job: the request that would be refused by phantom
    # spend is the request that clears it.
    expire_abandoned(now=moment)

    evaluation = evaluate_checkout(
        items,
        private_by_sku=private_by_sku,
        available_by_sku=available_by_sku,
        spent_today_inr=budgets.spent(),
        issued_at=parse(offer["created_at"]),
        now=moment,
        idempotency_key=idempotency_key,
        ttl_seconds=OFFER_TTL_SECONDS,
    )

    mandate_verdict = None
    if mandate_token:
        mandate_verdict = verifier.verify(
            mandate_token,
            agent_id=agent_id,
            cart_total_inr=evaluation.total_inr,
            categories=[
                (catalog.cache.public(item.sku) or {}).get("category", "")
                for item in items
            ],
            now=moment,
        )

    gate = assign_tier(
        total_inr=evaluation.total_inr,
        discount_pct=evaluation.discount_pct,
        tripped_bounds=evaluation.tripped_bounds,
        mandate_issuer_trusted=(
            mandate_verdict.issuer_trusted if mandate_verdict else None
        ),
        # `agent_first_order` is deliberately not supplied. The gate accepts it
        # because "never seen this agent before" is a real risk signal, but the
        # only agent identity available here arrived in the request body with
        # nothing vouching for it. A caller can mint a fresh agent_id per request
        # or borrow a familiar one, so deriving the signal from this field stops
        # no adversary while sending every honest agent's first purchase — a
        # ₹399 cable included — to a human. Authority comes from the amount rule,
        # the bounds, and the signed mandate. Feed this again when there is an
        # authenticated agent identity to feed it from.
    )

    reasons = _reasons_for(option, evaluation, gate, mandate_verdict)
    policy_receipt = receipts.issue(
        offer_id=offer_id,
        evaluation=evaluation,
        gate=gate,
        reasons=reasons,
    ).as_payload()

    # A mandate that fails for any reason other than an unknown issuer is a
    # refusal, not an escalation. An unknown issuer is a business question and
    # falls through to the gate, which routes it to a human.
    if mandate_verdict is not None and mandate_verdict.rejected:
        if not mandate_verdict.escalates_to_human:
            raise _refuse(
                idempotency_key,
                f"mandate rejected at check {mandate_verdict.check} "
                f"({mandate_verdict.code}): {mandate_verdict.detail}",
                code=f"mandate_{mandate_verdict.code.lower()}",
                http_status=403,
            )

    # ── Authority. A tier that requires a mandate does not proceed without one ─
    #
    # The gate says what authority this cart needs; this is where that becomes
    # binding. Scoped to Tier 1 on purpose: Tier 2 also reports
    # `requires_mandate`, but a Tier 2 cart is meant to reach a person, and
    # refusing it here would replace human review with a 403. Tier 2 falls
    # through to the hold below.
    #
    # Nothing has been reserved and no order exists at this point, so the refusal
    # costs nothing and the caller can retry the same request with a mandate
    # attached.
    if gate.tier == TIER_MANDATE and not (
        mandate_verdict is not None and mandate_verdict.valid
    ):
        missing = mandate_verdict is None
        detail = (
            "no mandate was presented"
            if missing
            else f"the mandate presented was not accepted ({mandate_verdict.code})"
        )
        ledger.append(
            "policy_kernel",
            "checkout.rejected",
            {
                "offer_id": offer_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "reason_code": "mandate_required",
                "amount_inr": amount_inr,
                "gate": gate.as_payload(),
                "mandate_presented": not missing,
                "policy_receipt": policy_receipt,
            },
            reason=(
                f"checkout refused before any reservation: INR {amount_inr} needs a "
                f"signed mandate ({gate.summary}) and {detail}"
            ),
        )
        raise _refuse(
            idempotency_key,
            f"this cart requires a signed spending mandate: {gate.summary}. "
            f"Refused because {detail}.",
            code="mandate_required",
            http_status=403,
        )

    # An offer whose base item failed a bound cannot be sold at all.
    if evaluation.offer_failed:
        order = orders.create(
            order_id=ids.order_id(),
            session_id=session_id,
            offer_id=offer_id,
            option_id=option_id,
            amount_inr=amount_inr,
            gate_tier=gate.tier,
            policy_mode=mode.mode_value(),
        )
        orders.transition(order["order_id"], orders.FAILED)
        ledger.append(
            "policy_kernel",
            "checkout.rejected",
            {
                "order_id": order["order_id"],
                "offer_id": offer_id,
                "reason_code": "bounds_failed",
                "evaluation": evaluation.as_payload(),
                "policy_receipt": policy_receipt,
            },
            reason=f"checkout refused: {evaluation.failure_detail}",
        )
        result = CheckoutResult(
            order_id=order["order_id"],
            status=STATUS_REJECTED,
            state=orders.FAILED,
            amount_inr=amount_inr,
            gate_tier=gate.tier,
            policy_mode=mode.mode_value(),
            audit_url=audit_url_for(order["order_id"]),
            policy_receipt=policy_receipt,
            reasons=reasons,
        )
        idempotency.complete(
            idempotency_key,
            response=result.as_payload(),
            order_id=order["order_id"],
        )
        return result

    order_id = ids.order_id()
    order = orders.create(
        order_id=order_id,
        session_id=session_id,
        offer_id=offer_id,
        option_id=option_id,
        amount_inr=amount_inr,
        gate_tier=gate.tier,
        policy_mode=mode.mode_value(),
    )
    idempotency.attach_order(idempotency_key, order_id)

    # ── Gate. Tier 2 stops here, before any hold and any external call ───────
    if gate.tier == TIER_HUMAN:
        orders.transition(order_id, orders.HELD, expect=orders.PENDING)
        approval = approvals_store.request(
            order_id=order_id,
            offer_id=offer_id,
            amount_inr=amount_inr,
            note=gate.summary,
        )
        ledger.append(
            "policy_kernel",
            "order.held_for_approval",
            {
                "order_id": order_id,
                "offer_id": offer_id,
                "approval_id": approval["approval_id"],
                "amount_inr": amount_inr,
                "gate": gate.as_payload(),
                "policy_receipt": policy_receipt,
            },
            reason=(
                f"order {order_id} held for merchant approval: {gate.summary}. "
                "No timeout approves this; it waits for a person."
            ),
        )
        result = CheckoutResult(
            order_id=order_id,
            status=STATUS_PENDING_APPROVAL,
            state=orders.HELD,
            amount_inr=amount_inr,
            gate_tier=gate.tier,
            policy_mode=mode.mode_value(),
            audit_url=audit_url_for(order_id),
            policy_receipt=policy_receipt,
            reasons=reasons,
            approval_id=approval["approval_id"],
            poll_url=f"/agent/v1/order/{order_id}",
        )
        idempotency.complete(
            idempotency_key, response=result.as_payload(), order_id=order_id
        )
        return result

    return _proceed_to_payment(
        order=order,
        offer=offer,
        option=option,
        items=items,
        evaluation=evaluation,
        gate=gate,
        reasons=reasons,
        policy_receipt=policy_receipt,
        idempotency_key=idempotency_key,
        agent_id=agent_id,
        base_sku=base_sku,
        payment_id=payment_id,
        moment=moment,
        client_factory=client_factory,
    )


def _proceed_to_payment(
    *,
    order: dict[str, Any],
    offer: dict[str, Any],
    option: dict[str, Any],
    items: list[LineItem],
    evaluation: Any,
    gate: Any,
    reasons: tuple[str, ...],
    policy_receipt: dict[str, Any],
    idempotency_key: str,
    agent_id: str,
    base_sku: str,
    payment_id: str | None,
    moment: datetime,
    client_factory: Callable[[], RazorpayClient],
) -> CheckoutResult:
    """Steps 4 to 9. Reached only when the gate cleared the order to proceed."""
    order_id = order["order_id"]
    amount_inr = int(order["amount_inr"])
    discount_inr = evaluation.discount_inr
    shadow = mode.is_shadow()

    # ── Step 4. Stock hold ───────────────────────────────────────────────────
    quantities: dict[str, int] = {}
    for item in items:
        quantities[item.sku] = quantities.get(item.sku, 0) + item.qty
    try:
        holds = stock.reserve_cart(quantities, session_id=order["session_id"], now=moment)
    except stock.InsufficientStock as exc:
        orders.transition(order_id, orders.FAILED)
        ledger.append(
            "policy_kernel",
            "checkout.rejected",
            {
                "order_id": order_id,
                "reason_code": "insufficient_stock",
                "sku": exc.sku,
                "requested": exc.requested,
                "available": exc.available,
            },
            reason=f"checkout refused: {exc}",
        )
        result = CheckoutResult(
            order_id=order_id,
            status=STATUS_REJECTED,
            state=orders.FAILED,
            amount_inr=amount_inr,
            gate_tier=gate.tier,
            policy_mode=mode.mode_value(),
            audit_url=audit_url_for(order_id),
            policy_receipt=policy_receipt,
            reasons=reasons + (str(exc),),
        )
        idempotency.complete(
            idempotency_key, response=result.as_payload(), order_id=order_id
        )
        return result

    # Budget is reserved before the gateway call so simultaneous checkouts
    # cannot collectively overspend the day. Released again if the call fails.
    budget_reserved = 0
    if not shadow and discount_inr > 0:
        try:
            budgets.check_and_accrue(discount_inr)
            budget_reserved = discount_inr
        except budgets.BudgetExceeded as exc:
            stock.release(holds.values())
            orders.transition(order_id, orders.FAILED)
            ledger.append(
                "policy_kernel",
                "checkout.rejected",
                {
                    "order_id": order_id,
                    "reason_code": "daily_discount_budget_exhausted",
                    "requested_inr": exc.requested,
                    "remaining_inr": exc.remaining,
                },
                reason=f"checkout refused: {exc}",
            )
            result = CheckoutResult(
                order_id=order_id,
                status=STATUS_REJECTED,
                state=orders.FAILED,
                amount_inr=amount_inr,
                gate_tier=gate.tier,
                policy_mode=mode.mode_value(),
                audit_url=audit_url_for(order_id),
                policy_receipt=policy_receipt,
                reasons=reasons + (str(exc),),
            )
            idempotency.complete(
                idempotency_key, response=result.as_payload(), order_id=order_id
            )
            return result

    # Write the reservation onto the order before anything external happens. The
    # two-step path returns after creating the gateway order, so this row is the
    # only thing that will still know what was held when `settle` arrives — and if
    # this process dies during the gateway call, it is the only thing that knows
    # there is anything to release.
    orders.record_reservation(
        order_id,
        hold_ids=sorted(holds.values()),
        budget_reserved_inr=budget_reserved,
    )

    # ── Step 5. Ledger the intent, BEFORE any external call ──────────────────
    ledger.append(
        "policy_kernel",
        "payment.intent",
        {
            "order_id": order_id,
            "offer_id": offer["offer_id"],
            "option_id": order["option_id"],
            "amount_inr": amount_inr,
            "discount_inr": discount_inr,
            "agent_id": agent_id,
            "base_sku": base_sku,
            "holds": sorted(holds.values()),
            "gate": gate.as_payload(),
            "policy_receipt": policy_receipt,
            "idempotency_key": idempotency_key,
            "would_have_charged": shadow,
        },
        money_delta_inr=amount_inr,
        reason=(
            f"intent to charge INR {amount_inr} for order {order_id} "
            f"({gate.name} tier): {'; '.join(reasons) if reasons else gate.summary}"
        ),
    )

    if shadow:
        # Everything above ran. Nothing below runs. The verdict, the receipt and
        # the ledger are identical to the live path; only the money is absent.
        stock.release(holds.values())
        orders.clear_reservation(order_id)
        ledger.append(
            "policy_kernel",
            "payment.shadow_skipped",
            {
                "order_id": order_id,
                "amount_inr": amount_inr,
                "would_have_charged": True,
                "razorpay_called": False,
            },
            money_delta_inr=amount_inr,
            reason=(
                f"shadow mode: order {order_id} would have charged INR "
                f"{amount_inr}. No gateway call was made and no stock committed."
            ),
        )
        result = CheckoutResult(
            order_id=order_id,
            status=STATUS_SHADOW,
            state=orders.PENDING,
            amount_inr=amount_inr,
            gate_tier=gate.tier,
            policy_mode=mode.mode_value(),
            audit_url=audit_url_for(order_id),
            policy_receipt=policy_receipt,
            reasons=reasons,
            would_have_charged=True,
        )
        idempotency.complete(
            idempotency_key, response=result.as_payload(), order_id=order_id
        )
        return result

    # ── Step 6. Razorpay order. Amount fixed server-side ─────────────────────
    client = client_factory()
    try:
        gateway_order = client.create_order(
            amount_inr,
            receipt=order_id,
            notes={
                "order_id": order_id,
                "offer_id": offer["offer_id"],
                "agent_id": agent_id,
                "gate_tier": str(gate.tier),
            },
        )
    except (RazorpayError, httpx.HTTPError) as exc:
        _unwind(order_id, holds.values(), budget_reserved)
        ledger.append(
            "policy_kernel",
            "payment.failed",
            {
                "order_id": order_id,
                "stage": "order_create",
                "error": str(exc)[:400],
            },
            money_delta_inr=0,
            reason=f"gateway order creation failed for {order_id}: {str(exc)[:200]}",
        )
        raise CheckoutError(
            f"payment gateway rejected order creation: {exc}",
            code="gateway_error",
            http_status=502,
        ) from exc
    except Exception as exc:
        # Unexpected error — not a gateway failure, do not mask programming errors.
        _unwind(order_id, holds.values(), budget_reserved)
        ledger.append(
            "policy_kernel",
            "payment.failed",
            {
                "order_id": order_id,
                "stage": "order_create",
                "error": str(exc)[:400],
            },
            money_delta_inr=0,
            reason=f"gateway order creation failed for {order_id}: {str(exc)[:200]}",
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
            "razorpay_order_id": gateway_order["id"],
            "amount_inr": gateway_order["amount_inr"],
            "status": gateway_order["status"],
        },
        reason=(
            f"gateway order {gateway_order['id']} created for INR "
            f"{gateway_order['amount_inr']}"
        ),
    )

    if payment_id is None:
        # Try to create a hosted payment link for chat UX (optional, never blocks checkout)
        # Skip during tests to avoid real network call
        import os

        payment_url = None
        if not os.getenv("PYTEST_CURRENT_TEST"):
            try:
                link = client.create_payment_link(
                    amount_inr,
                    reference_id=order_id,
                    description=f"Aether Audio order {order_id} — ₹{amount_inr}",
                    notes={"order_id": order_id, "offer_id": offer["offer_id"]},
                )
                payment_url = link.get("short_url")
            except Exception:
                pass
        # The buyer completes Checkout, then `settle` captures. The order stays
        # PENDING: nothing has been authorised yet, and the hold keeps the stock
        # for the length of its TTL. The reservation recorded above is what lets
        # that later, separate request commit the right holds and release the
        # right budget — and what lets `expire_abandoned` clean up if the buyer
        # never comes back at all.
        razorpay_payload: dict[str, Any] = {
            "order_id": gateway_order["id"],
            "amount_inr": gateway_order["amount_inr"],
            "currency": gateway_order["currency"],
            "key_id": client.key_id,
        }
        if payment_url:
            razorpay_payload["payment_url"] = payment_url
            razorpay_payload["payment_link"] = payment_url
        result = CheckoutResult(
            order_id=order_id,
            status=STATUS_AWAITING_PAYMENT,
            state=orders.PENDING,
            amount_inr=amount_inr,
            gate_tier=gate.tier,
            policy_mode=mode.mode_value(),
            audit_url=audit_url_for(order_id),
            policy_receipt=policy_receipt,
            reasons=reasons,
            razorpay=razorpay_payload,
        )
        idempotency.complete(
            idempotency_key, response=result.as_payload(), order_id=order_id
        )
        return result

    settled = settle(
        order_id,
        payment_id=payment_id,
        holds=list(holds.values()),
        client=client,
        budget_reserved=budget_reserved,
    )
    result = CheckoutResult(
        order_id=order_id,
        status=STATUS_CONFIRMED,
        state=settled["state"],
        amount_inr=amount_inr,
        gate_tier=gate.tier,
        policy_mode=mode.mode_value(),
        audit_url=audit_url_for(order_id),
        policy_receipt=policy_receipt,
        reasons=reasons,
        razorpay={
            "order_id": gateway_order["id"],
            "payment_id": payment_id,
            "amount_inr": amount_inr,
            "currency": "INR",
        },
    )
    idempotency.complete(
        idempotency_key, response=result.as_payload(), order_id=order_id
    )
    return result


def settle(
    order_id: str,
    *,
    payment_id: str,
    holds: list[str] | None = None,
    client: RazorpayClient | None = None,
    client_factory: Callable[[], RazorpayClient] = _default_client,
    budget_reserved: int | None = None,
) -> dict[str, Any]:
    """Steps 7 to 9: capture the payment, commit stock, ledger the confirmation.

    Separate from `checkout` because Razorpay test accounts generally cannot
    create a payment server-side. The buyer completes Checkout in a browser, and
    this runs with the resulting payment id.

    The captured amount comes from the stored order, so a payment id for a
    different amount cannot be used to settle this order cheaply. Razorpay
    rejects a mismatch as well, which makes it two independent checks. The
    payment must additionally have been made against this order's own gateway
    order, so a payment for an unrelated order of the same total is refused
    rather than quietly consumed.

    `holds` and `budget_reserved` default to what the order row recorded when the
    reservation was taken. That default is what makes the two-step path correct:
    the HTTP request that captures a payment knows an order id and nothing else,
    and stock that never decrements is the kind of bug that only shows up as an
    oversell weeks later. Passing them explicitly is for the single-call path,
    which still has them in hand.
    """
    mode.assert_may_move_money("capturing a payment")
    order = orders.require(order_id)
    amount_inr = int(order["amount_inr"])
    client = client or client_factory()

    recorded_holds, recorded_budget = orders.reservation(order_id)
    if holds is None:
        holds = recorded_holds
    if budget_reserved is None:
        budget_reserved = recorded_budget

    # ── Step 7. Capture ──────────────────────────────────────────────────────
    try:
        payment = client.fetch_payment(payment_id)
    except (RazorpayError, httpx.HTTPError) as exc:
        _unwind(order_id, holds or [], budget_reserved)
        ledger.append(
            "policy_kernel",
            "payment.failed",
            {
                "order_id": order_id,
                "razorpay_payment_id": payment_id,
                "stage": "fetch",
                "error": str(exc)[:400],
            },
            reason=(
                f"could not read payment {payment_id} for order {order_id}: "
                f"{str(exc)[:200]}"
            ),
        )
        raise CheckoutError(
            f"payment lookup failed: {exc}", code="capture_failed", http_status=502
        ) from exc
    except Exception as exc:
        # Unexpected error — not a gateway failure, do not mask programming errors.
        _unwind(order_id, holds or [], budget_reserved)
        ledger.append(
            "policy_kernel",
            "payment.failed",
            {
                "order_id": order_id,
                "razorpay_payment_id": payment_id,
                "stage": "fetch",
                "error": str(exc)[:400],
            },
            reason=(
                f"could not read payment {payment_id} for order {order_id}: "
                f"{str(exc)[:200]}"
            ),
        )
        raise CheckoutError(
            f"payment lookup failed: {exc}", code="capture_failed", http_status=502
        ) from exc

    # The payment has to belong to the gateway order this order created. Amount
    # equality is not enough on its own: two orders for the same total would
    # otherwise each be settleable with the other's payment, and a single payment
    # could settle both of them, shipping twice for one sum of money.
    #
    # No unwind here, unlike a failed capture. Nothing moved and nothing failed —
    # the caller named the wrong payment — so the order is still legitimately
    # awaiting its own, and releasing its stock hold would penalise it for
    # someone else's mistake. The abandonment sweep still collects it later.
    presented_gateway_order = payment.get("order_id")
    expected_gateway_order = order["razorpay_order_id"]
    if presented_gateway_order != expected_gateway_order:
        ledger.append(
            "policy_kernel",
            "payment.order_mismatch",
            {
                "order_id": order_id,
                "razorpay_payment_id": payment_id,
                "expected_razorpay_order_id": expected_gateway_order,
                "presented_razorpay_order_id": presented_gateway_order,
            },
            reason=(
                f"payment {payment_id} belongs to gateway order "
                f"{presented_gateway_order} but order {order_id} was placed "
                f"against {expected_gateway_order}"
            ),
        )
        raise CheckoutError(
            "payment does not belong to this order",
            code="payment_order_mismatch",
            http_status=409,
        )

    try:
        if payment["captured"]:
            captured = payment
        else:
            captured = client.capture_payment(payment_id, amount_inr)
    except (RazorpayError, httpx.HTTPError) as exc:
        _unwind(order_id, holds or [], budget_reserved)
        ledger.append(
            "policy_kernel",
            "payment.failed",
            {
                "order_id": order_id,
                "razorpay_payment_id": payment_id,
                "stage": "capture",
                "error": str(exc)[:400],
            },
            reason=f"capture failed for order {order_id}: {str(exc)[:200]}",
        )
        raise CheckoutError(
            f"payment capture failed: {exc}", code="capture_failed", http_status=502
        ) from exc
    except Exception as exc:
        # Unexpected error — not a gateway failure, do not mask programming errors.
        _unwind(order_id, holds or [], budget_reserved)
        ledger.append(
            "policy_kernel",
            "payment.failed",
            {
                "order_id": order_id,
                "razorpay_payment_id": payment_id,
                "stage": "capture",
                "error": str(exc)[:400],
            },
            reason=f"capture failed for order {order_id}: {str(exc)[:200]}",
        )
        raise CheckoutError(
            f"payment capture failed: {exc}", code="capture_failed", http_status=502
        ) from exc

    if captured["amount_inr"] != amount_inr:
        # Should be unreachable — the amount was sent from the stored order — but
        # a captured amount that disagrees with the authorised one is exactly the
        # thing this system exists to refuse to paper over.
        ledger.append(
            "policy_kernel",
            "payment.amount_mismatch",
            {
                "order_id": order_id,
                "razorpay_payment_id": payment_id,
                "authorised_inr": amount_inr,
                "captured_inr": captured["amount_inr"],
            },
            reason=(
                f"captured INR {captured['amount_inr']} does not match the "
                f"authorised INR {amount_inr} for order {order_id}"
            ),
        )
        raise CheckoutError(
            "captured amount does not match the authorised amount",
            code="amount_mismatch",
            http_status=500,
        )

    # A gateway that answers rather than raises can report a decline: a test
    # card that always fails comes back shaped like a success with
    # `captured: False`. Trusting the shape instead of the flag would advance
    # this order to CAPTURED on money that never moved, so the flag is the
    # check. Nothing was charged; the reservations are unwound and the caller
    # may retry with another method under a fresh key.
    if not captured.get("captured"):
        _unwind(order_id, holds or [], budget_reserved)
        ledger.append(
            "razorpay",
            "payment.declined",
            {
                "order_id": order_id,
                "razorpay_order_id": order["razorpay_order_id"],
                "razorpay_payment_id": payment_id,
                "amount_inr": amount_inr,
                "gateway_status": captured.get("status"),
                "error_code": captured.get("error_code"),
                "error_description": captured.get("error_description"),
            },
            reason=(
                f"payment {payment_id} was declined by the gateway for order "
                f"{order_id}. No charge occurred; stock and discount budget "
                "were released."
            ),
        )
        raise CheckoutError(
            "the payment was declined by the gateway; no charge was made and "
            "it is safe to retry with a different method",
            code="payment_declined",
            http_status=402,
        )

    orders.advance(
        order_id, orders.AUTHORIZED, razorpay_payment_id=payment_id
    )
    orders.advance(order_id, orders.CAPTURED, razorpay_payment_id=payment_id)

    # ── Step 8. Commit stock against the hold ────────────────────────────────
    # The payment is captured by now, so this uses the tolerant commit: a hold
    # that lapsed while the buyer was in the card form still has to decrement the
    # shelf, and anything it could not reconcile is named on the ledger below
    # rather than raised at a caller who can no longer refuse the sale.
    stock_report: dict[str, Any] = {}
    if holds:
        stock_report = stock.commit_settled(holds)
        if stock_report["recovered"] or stock_report["oversold"] or stock_report["missing"]:
            ledger.append(
                "policy_kernel",
                "stock.commit_anomaly",
                {
                    "order_id": order_id,
                    "razorpay_payment_id": payment_id,
                    "recovered": stock_report["recovered"],
                    "oversold": stock_report["oversold"],
                    "missing": stock_report["missing"],
                },
                reason=(
                    f"stock committed for order {order_id} after capture with "
                    f"{len(stock_report['recovered'])} lapsed hold(s) recovered, "
                    f"{len(stock_report['oversold'])} line(s) unfulfillable, "
                    f"{len(stock_report['missing'])} hold(s) not found"
                ),
            )

    # The reservation has been consumed. Cleared so a second settle cannot
    # decrement twice and the abandonment sweep cannot release a budget that was
    # actually spent.
    orders.clear_reservation(order_id)
    if budget_reserved:
        # Reserved before the gateway call, spent now that the capture succeeded.
        # The accrual already stands; what is dropped is the claim on it.
        ledger.append(
            "policy_kernel",
            "budget.committed",
            {
                "order_id": order_id,
                "discount_inr": int(budget_reserved),
                "budget": budgets.snapshot(),
            },
            reason=(
                f"discount of INR {budget_reserved} reserved for order {order_id} "
                "is now spent against the day's budget"
            ),
        )

    # ── Step 9. Ledger the confirmation ──────────────────────────────────────
    ledger.append(
        "razorpay",
        "payment.captured",
        {
            "order_id": order_id,
            "razorpay_order_id": order["razorpay_order_id"],
            "razorpay_payment_id": payment_id,
            "amount_inr": amount_inr,
            "method": captured.get("method"),
            "status": captured.get("status"),
            "holds_committed": sorted(stock_report.get("committed", [])),
        },
        money_delta_inr=amount_inr,
        reason=(
            f"payment {payment_id} captured for INR {amount_inr} against order "
            f"{order_id}"
        ),
    )

    # ── The store learns from every completed basket ─────────────────────────
    # Recorded only on a clean completion: a refunded oversell is not evidence
    # of what sells together, it is evidence of what didn't. Failures here are
    # swallowed deliberately — learning must never break the sale that fed it.
    if not stock_report.get("oversold"):
        try:
            sold_offer = offers.get(order["offer_id"])
            if sold_offer is not None:
                sold_option = offers.option(sold_offer, order["option_id"])
                base_skus = [
                    str(i["sku"])
                    for i in sold_option.get("items", [])
                    if i.get("role") == ROLE_BASE
                ]
                companions = [
                    str(i["sku"])
                    for i in sold_option.get("items", [])
                    if i.get("role") != ROLE_BASE
                ]
                if base_skus:
                    pairings.record_order_basket(base_skus[0], companions)

                    # The anonymous category-level prior, pooled with related
                    # stores in the same learning cluster.
                    base_public = catalog.cache.public(base_skus[0])
                    if base_public is not None:
                        companion_categories = []
                        for sku in companions:
                            public_row = catalog.cache.public(sku)
                            if public_row is not None:
                                companion_categories.append(public_row["category"])
                        pairings.record_category_basket(
                            base_public["category"],
                            companion_categories,
                            cluster=tenancy.cluster_for_store(),
                        )
        except Exception:  # noqa: BLE001 - learning is never load-bearing
            ledger.append(
                "system",
                "learning.record_failed",
                {"order_id": order_id},
                reason=(
                    f"pairing observation for order {order_id} could not be "
                    "recorded; the sale stands and the lesson is skipped"
                ),
            )

    # ── The failure the hold could not prevent ───────────────────────────────
    # The capture is on the ledger, and the shelf could not cover it. The
    # compensation saga runs now, in full, before anything is raised: by the
    # time the caller sees an error the refund already exists. The discount
    # budget stays spent — the discount was genuinely given — and the net
    # money delta for the order is zero once the refund entry lands.
    if stock_report.get("oversold"):
        from kernel import saga

        compensated = saga.compensate(
            orders.require(order_id),
            stock_report=stock_report,
            client=client,
        )
        raise OversoldFault(compensated)

    return orders.require(order_id)


def _unwind(order_id: str, holds, budget_reserved: int) -> None:
    """Undo the reservations a failed attempt took out.

    Stock goes back on the shelf and reserved budget is returned. The order is
    marked FAILED and its reservation record cleared, so nothing sweeps it a
    second time. None of this pretends the attempt did not happen — the ledger
    keeps the intent entry, and the failure gets its own entry alongside it.
    """
    if holds:
        stock.release(holds)
    if budget_reserved:
        budgets.release(budget_reserved)
    try:
        orders.clear_reservation(order_id)
    except orders.OrderNotFound:
        pass
    try:
        orders.transition(order_id, orders.FAILED)
    except orders.IllegalTransition:
        # Already terminal. Nothing to unwind at the order level.
        pass


def expire_abandoned(
    *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Release the reservations of two-step checkouts the buyer never completed.

    The two-step path hands out a gateway order and waits. If the buyer closes the
    tab, the stock hold times out on its own — availability is computed against
    live holds, so the units come back without anyone doing anything. The reserved
    discount budget does not: it is a row in `policy_budgets` that only a release
    reduces, so every abandoned cart would permanently shrink the day's budget and
    the shortfall would look like real spend.

    That is what this closes, and the timing is the whole difficulty. A lapsed hold
    is *not* evidence of abandonment: the hold TTL is 120 seconds and buyers spend
    longer than that in a card form, which is exactly why `stock.commit_settled`
    exists. Sweeping on hold expiry alone would mark orders FAILED underneath
    buyers who are still paying, and settle would then refuse a captured payment
    because FAILED is terminal. So abandonment needs two things to be true: every
    hold is dead, and the order has been untouched for
    `CHECKOUT_ABANDONED_AFTER_SECONDS` — long past any gateway checkout session.

    Cheap and idempotent — clearing the reservation is what makes a second run a
    no-op — so it can run at boot and at the top of each checkout rather than
    needing a scheduler this project does not have.

    Returns one summary per order swept.
    """
    moment = now or utc_now()
    cutoff = plus_seconds(moment, -CHECKOUT_ABANDONED_AFTER_SECONDS)
    swept: list[dict[str, Any]] = []

    for order in orders.with_open_reservation():
        order_id = order["order_id"]

        # The grace window. `updated_at` last moved when the buyer was handed a
        # gateway order, so it is the right clock: it measures how long this order
        # has been waiting on a person, not how long ago it was created.
        if parse(order["updated_at"]) > cutoff:
            continue

        hold_ids, budget_reserved = orders.reservation(order_id)

        # A hold that is somehow still live means the units are still promised to
        # this order, and releasing the budget while the stock is held would leave
        # the two records disagreeing. Redundant at the default TTL, and correct if
        # the TTL is ever raised past the grace window.
        if any(
            (hold := stock.get(hold_id)) is not None and stock.is_live(hold, moment)
            for hold_id in hold_ids
        ):
            continue

        if hold_ids:
            stock.release(hold_ids)
        if budget_reserved:
            budgets.release(budget_reserved)
        orders.clear_reservation(order_id)
        try:
            orders.transition(order_id, orders.FAILED)
        except orders.IllegalTransition:
            pass

        ledger.append(
            "policy_kernel",
            "checkout.abandoned",
            {
                "order_id": order_id,
                "offer_id": order["offer_id"],
                "session_id": order["session_id"],
                "state_before": order["state"],
                "holds_released": sorted(hold_ids),
                "budget_released_inr": int(budget_reserved),
                "budget": budgets.snapshot(),
            },
            reason=(
                f"order {order_id} abandoned before payment: untouched since "
                f"{order['updated_at']} with every stock hold lapsed, so INR "
                f"{budget_reserved} of reserved discount budget was returned to "
                "the day"
            ),
        )
        swept.append(
            {
                "order_id": order_id,
                "holds_released": sorted(hold_ids),
                "budget_released_inr": int(budget_reserved),
            }
        )

    return swept


def _reasons_for(
    option: dict[str, Any],
    evaluation: Any,
    gate: Any,
    mandate_verdict: Any,
) -> tuple[str, ...]:
    """The human-readable strings recorded in the receipt.

    Sourced from the offer the buyer actually saw, plus whatever the policy layer
    added at checkout. The receipt must carry the same prose the human was shown,
    so the offer's own reason comes first and verbatim.
    """
    reasons: list[str] = []
    if option.get("human_reason"):
        reasons.append(str(option["human_reason"]))
    for verdict in evaluation.rejected_items:
        failed = verdict.failed_bound
        reasons.append(
            f"{verdict.item.sku} removed: {failed.detail}" if failed
            else f"{verdict.item.sku} removed by policy"
        )
    reasons.append(gate.summary)
    if mandate_verdict is not None and mandate_verdict.rejected:
        reasons.append(
            f"mandate check {mandate_verdict.check} failed: {mandate_verdict.detail}"
        )
    return tuple(reasons)


def _result_from_payload(payload: dict[str, Any]) -> CheckoutResult:
    """Rebuild a result from a stored idempotent response."""
    return CheckoutResult(
        order_id=payload.get("order_id", ""),
        status=payload.get("status", STATUS_REPLAYED),
        state=payload.get("state", ""),
        amount_inr=int(payload.get("amount_inr", 0)),
        gate_tier=int(payload.get("gate_tier", 0)),
        policy_mode=payload.get("policy_mode", mode.mode_value()),
        audit_url=payload.get("audit_url", ""),
        policy_receipt=payload.get("policy_receipt", {}),
        reasons=tuple(payload.get("reasons", ())),
        razorpay=payload.get("razorpay", {}),
        approval_id=payload.get("approval_id"),
        poll_url=payload.get("poll_url"),
        would_have_charged=bool(payload.get("would_have_charged", False)),
    )
