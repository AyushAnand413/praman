"""The oversell saga — what happens when money moves and stock does not exist.

The stock hold prevents most oversells. It cannot prevent all of them: the
payment is captured inside Razorpay, roughly 2.6 seconds in which the lock and
the money live in different systems. A manual stock adjustment, a lapsed hold
reconciled too generously, any writer outside this process — the shelf can change
underneath a capturing payment, and no reservation scheme fixes that, because
the reservation is not where the money is.

So the system ships both halves: the hold AND a compensating transaction. This
module is the second half. When `settle` commits stock against a captured
payment and finds a line it cannot fulfil, it hands the order here, and this
module runs the fixed compensation sequence onto the ledger:

    fulfillment.check            the detection, named
    saga.compensation_triggered  the decision to fix it automatically
    razorpay.refund              FULL, automatic, reason = oversold_merchant_fault
    ledger.compensate            order voided for fulfilment, capture linked to refund
    policy.selfheal              the SKU is disabled so it cannot recur
    notify.buyer                 structured failure + a way forward
    notify.merchant              what happened, in the merchant's words

Four properties worth keeping intact while editing:

**Fault is attributed honestly.** The buyer did nothing wrong; the code says so.

**The refund is automatic.** No support ticket stands between the charge and
the return of the money.

**The SKU self-disables.** An oversold SKU stops being offered immediately,
which turns "will happen again" into "cannot happen again until a human
restocks".

**retry_safe is true.** The refund plus the buyer's idempotency discipline mean
a retry cannot double-charge. The structured failure says so explicitly rather
than leaving the agent to guess.

The order ends REFUNDED, not VOIDED: VOIDED is reserved for orders that were
never charged. The compensating entry records that the order was voided for
fulfilment, which is the truth the plan's sequence describes, expressed in the
state machine's vocabulary.

`force_oversell` makes the race deterministic on demand, by shrinking the shelf
inside the window between the gateway order and the stock commit — the exact
moment an external writer would act. It backs the demo control endpoint.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Callable

import settings
from kernel import mode
from kernel.payments import RazorpayClient
from store import catalog, ids, ledger, orders
from store.db import get_connection, transaction
from store.timestamps import utc_now

#: The machine-readable name of the primary failure. Part of the agent-facing
#: contract, so it is a constant rather than a literal scattered through code.
CODE_OVERSOLD_MERCHANT_FAULT = "OVERSOLD_MERCHANT_FAULT"

#: What the buyer is told about when the money comes back. Razorpay test-mode
#: refunds to cards settle in this window; stated rather than implied.
REFUND_ETA_DAYS = "5-7"

#: The ordered event names of one compensation run. The demo asserts against
#: this list, so a rehearsal proves the sequence, not just that "something"
#: happened.
COMPENSATION_EVENT_SEQUENCE = (
    "fulfillment.check",
    "saga.compensation_triggered",
    "razorpay.refund",
    "ledger.compensate",
    "policy.selfheal",
    "notify.buyer",
    "notify.merchant",
)


# ---------------------------------------------------------------------------
# Detection and compensation
# ---------------------------------------------------------------------------


def compensate(
    order: dict[str, Any],
    *,
    stock_report: dict[str, Any],
    client: RazorpayClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full compensation sequence for an oversold captured payment.

    Called by `kernel.checkout.settle` when the post-capture stock commit
    reports a line it could not decrement. Returns the structured failure
    payload the buyer agent receives; the caller raises it onward as an
    `OversoldFault`.
    """
    moment = now or utc_now()
    order_id = order["order_id"]
    amount_inr = int(order["amount_inr"])
    payment_id = order["razorpay_payment_id"]
    oversold_lines = list(stock_report.get("oversold") or [])
    skus = sorted({line["sku"] for line in oversold_lines})

    # ── Detection, on the record ─────────────────────────────────────────────
    ledger.append(
        "policy_kernel",
        "fulfillment.check",
        {
            "order_id": order_id,
            "razorpay_payment_id": payment_id,
            "outcome": "OVERSOLD",
            "oversold": oversold_lines,
        },
        reason=(
            f"order {order_id}: payment {payment_id} is captured but the shelf "
            f"cannot cover {', '.join(skus)} — money moved for stock that is "
            "gone. Merchant-side fault."
        ),
    )

    # ── The decision: compensate automatically ───────────────────────────────
    ledger.append(
        "policy_kernel",
        "saga.compensation_triggered",
        {
            "order_id": order_id,
            "razorpay_payment_id": payment_id,
            "amount_inr": amount_inr,
            "action": "auto_refund_full+selfheal_sku",
        },
        reason=(
            f"SAGA COMPENSATION TRIGGERED for order {order_id}: issuing a full "
            f"automatic refund of INR {amount_inr}, voiding fulfilment, and "
            f"disabling {', '.join(skus)}"
        ),
    )

    # ── Refund, full, automatic ──────────────────────────────────────────────
    # Razorpay's own idempotency header guards the one surface where the
    # gateway offers it: if this process dies between the call and the ledger
    # write, a retried refund cannot become two.
    refund = client.refund_payment(
        payment_id,
        notes={"order_id": order_id, "reason": "oversold_merchant_fault"},
        idempotency_key=f"saga-refund-{order_id}",
    )
    refund_amount = int(refund.get("amount_inr") or amount_inr)
    refund_entry = ledger.append(
        "razorpay",
        "razorpay.refund",
        {
            "order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_refund_id": refund.get("id"),
            "amount_inr": refund_amount,
            "status": refund.get("status"),
            "reason_code": "oversold_merchant_fault",
        },
        money_delta_inr=-refund_amount,
        reason=(
            f"automatic full refund of INR {refund_amount} for order {order_id}: "
            "oversold_merchant_fault — we charged and could not fulfil. That is "
            "our fault, said plainly."
        ),
    )

    # ── Void fulfilment, link capture to refund ──────────────────────────────
    orders.transition(
        order_id,
        orders.REFUNDED,
        expect=orders.CAPTURED,
        razorpay_refund_id=refund.get("id"),
    )
    capture_seq = _latest_capture_seq(order_id)
    ledger.append(
        "policy_kernel",
        "ledger.compensate",
        {
            "order_id": order_id,
            "order_state": orders.REFUNDED,
            "voided_for_fulfilment": True,
            "links": {
                "captured_at_seq": capture_seq,
                "refunded_at_seq": refund_entry.seq,
            },
            "refund_id": refund.get("id"),
            "amount_inr": refund_amount,
        },
        reason=(
            f"order {order_id} compensated: capture #{capture_seq} is answered "
            f"by refund #{refund_entry.seq}. Fulfilment is voided; the net "
            "money delta for this order is zero."
        ),
    )

    # ── Self-heal: the SKU cannot be sold again until a human restocks ───────
    for sku in skus:
        catalog.cache.set_offerable(sku, False)
        ledger.append(
            "policy_kernel",
            "policy.selfheal",
            {
                "order_id": order_id,
                "sku": sku,
                "offerable": False,
                "resumes_when": "a merchant restocks and re-enables the SKU",
            },
            reason=(
                f"{sku} oversold on order {order_id}; removed from the offerable "
                "catalog automatically so the same failure cannot repeat"
            ),
        )

    # ── Release the dead holds ───────────────────────────────────────────────
    # An oversold hold was never committed — its units do not exist — so it
    # would otherwise sit ACTIVE until its TTL lapses, silently shrinking
    # availability and refusing the next honest buyer. The money has been
    # returned; the promise has to go too.
    from kernel import stock as stock_kernel

    dead_holds = [line["hold_id"] for line in oversold_lines]
    missing_holds = list(stock_report.get("missing") or [])
    if dead_holds:
        stock_kernel.release(dead_holds)
    if missing_holds:
        stock_kernel.release(missing_holds)

    # ── Tell both sides ──────────────────────────────────────────────────────
    failure = _structured_failure(
        order,
        refund={
            "id": refund.get("id"),
            "amount_inr": refund_amount,
            "status": refund.get("status") or "processed",
        },
        skus=skus,
        now=moment,
    )
    ledger.append(
        "system",
        "notify.buyer",
        dict(failure),
        reason=(
            f"buyer notified: order {order_id} failed with "
            f"{CODE_OVERSOLD_MERCHANT_FAULT}; full refund issued and an "
            "alternative offered"
        ),
    )
    ledger.append(
        "system",
        "notify.merchant",
        {
            "order_id": order_id,
            "summary": (
                f"Oversell. Auto-refunded INR {refund_amount}. "
                f"SKU disabled: {', '.join(skus)}."
            ),
            "skus_disabled": skus,
            "refund_id": refund.get("id"),
        },
        reason=(
            f"merchant notified: order {order_id} oversold, INR {refund_amount} "
            f"auto-refunded, {', '.join(skus)} disabled pending restock"
        ),
    )
    return failure


def _latest_capture_seq(order_id: str) -> int | None:
    """The ledger seq of this order's payment.captured entry, for the link."""
    for entry in reversed(ledger.trail(order_id)):
        if entry.event == "payment.captured":
            return entry.seq
    return None


def _structured_failure(
    order: dict[str, Any],
    *,
    refund: dict[str, Any],
    skus: list[str],
    now: datetime,
) -> dict[str, Any]:
    """The response body an agent receives instead of an order confirmation.

    Offers a path forward rather than just erroring: the remedy names a real,
    in-stock alternative from the same category, priced from the public catalog.
    """
    order_id = order["order_id"]
    amount_inr = int(order["amount_inr"])
    remedy = _remedy_for(skus)
    payload: dict[str, Any] = {
        "status": "failed",
        "code": CODE_OVERSOLD_MERCHANT_FAULT,
        "human_message": (
            f"We charged you INR {amount_inr} and couldn't fulfil. That's our "
            f"fault. Full refund of INR {refund['amount_inr']} issued to the "
            "original method."
        ),
        "order_id": order_id,
        "amount_inr": amount_inr,
        "currency": "INR",
        "refund": {
            "id": refund["id"],
            "amount_inr": refund["amount_inr"],
            "status": refund["status"],
            "eta_days": REFUND_ETA_DAYS,
        },
        "remedy_offered": remedy,
        "audit_url": f"/audit/{order_id}",
        "retry_safe": True,
    }
    return payload


def _remedy_for(skus: list[str]) -> dict[str, Any] | None:
    """A concrete alternative for the first oversold SKU, or None.

    Deterministic: same category first, cheapest; if the SKU was alone in its
    category — a sole cable, say — the cheapest in-stock item anywhere stands
    in rather than offering nothing. The note tells the agent to confirm with
    its principal either way: switching the goods is the buyer's call, ours.
    """
    fallbacks = [
        row
        for row in catalog.cache.all_public(offerable_only=True)
        if int(row["stock_qty"]) > 0
    ]
    fallbacks.sort(key=lambda r: (int(r["list_price_inr"]), r["sku"]))
    for sku in skus:
        failed = catalog.cache.public(sku)
        if failed is None:
            continue
        candidates = [
            row
            for row in fallbacks
            if row["category"] == failed["category"] and row["sku"] != sku
        ]
        same_category = True
        if not candidates:
            candidates = [row for row in fallbacks if row["sku"] != sku]
            same_category = False
        if not candidates:
            continue
        pick = candidates[0]
        cheaper = int(pick["list_price_inr"]) <= int(failed["list_price_inr"])
        note = (
            f"Closest available alternative to {sku}"
            + (" — cheaper than the original." if cheaper else ".")
            + ("" if same_category else " Different category than the original.")
            + " Confirm with your principal before purchasing."
        )
        return {
            "type": "alternative_sku",
            "sku": pick["sku"],
            "title": pick["title"],
            "price_inr": int(pick["list_price_inr"]),
            "category": pick["category"],
            "note": note,
        }
    return None


# ---------------------------------------------------------------------------
# The deterministic rehearsal
# ---------------------------------------------------------------------------


class SimulatedGateway:
    """A credential-free stand-in for Razorpay, for rehearsals only.

    Speaks the same normalized shapes `kernel.payments.RazorpayClient`
    produces, holds no credentials, and reaches nothing. The oversell demo
    must fire on cue ten times out of ten; routing the rehearsal through the
    real gateway would make the demo dependent on a browser round-trip test
    mode does not allow server-side anyway. Every response it fabricates is
    labelled `simulated`, so nothing it touches can be mistaken for a real
    transaction on the ledger.
    """

    def __init__(self) -> None:
        self.key_id = "rzp_test_simulated"
        self.calls: list[str] = []
        self._counter = 0
        self.last_order_id: str | None = None

    def _next(self, prefix: str) -> str:
        # Globally unique, not per-instance: every rehearsal creates a fresh
        # gateway object, and the orders table's UNIQUE index on
        # razorpay_order_id is what stops two simulations from colliding.
        self._counter += 1
        return f"{prefix}_sim{self._counter:04d}_{secrets.token_hex(4)}"

    def new_payment_id(self) -> str:
        return self._next("pay")

    def create_order(self, amount_inr: int, *, receipt: str, notes=None, currency="INR"):
        self.calls.append("create_order")
        self.last_order_id = self._next("order")
        return {
            "id": self.last_order_id,
            "amount_inr": amount_inr,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
        }

    def fetch_payment(self, payment_id: str):
        self.calls.append("fetch_payment")
        return {
            "id": payment_id,
            "order_id": self.last_order_id,
            "status": "authorized",
            "amount_inr": None,
            "currency": "INR",
            "captured": False,
            "method": "card",
        }

    def capture_payment(self, payment_id: str, amount_inr: int, *, currency="INR"):
        self.calls.append("capture_payment")
        return {
            "id": payment_id,
            "amount_inr": amount_inr,
            "currency": currency,
            "status": "captured",
            "captured": True,
            "method": "card",
        }

    def refund_payment(self, payment_id: str, **kwargs):
        self.calls.append("refund_payment")
        return {
            "id": self._next("rfnd"),
            "payment_id": payment_id,
            "amount_inr": kwargs.get("amount_inr"),
            "status": "processed",
        }


class _ShelfFault:
    """Wraps a gateway client and moves the shelf mid-flight.

    The wrapped `create_order` behaves normally, then — while the money is
    conceptually outside, in flight at the gateway — sets the relevant SKUs'
    on-hand stock to zero, exactly as an external writer (a manual adjustment,
    another sales channel) would. The subsequent capture succeeds; the stock
    commit then finds nothing to decrement. That gap is the whole failure, made
    reproducible.
    """

    def __init__(self, inner: Any, skus: list[str]) -> None:
        self._inner = inner
        self._skus = sorted(set(skus))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def create_order(self, amount_inr: int, **kwargs: Any) -> dict[str, Any]:
        gateway_order = self._inner.create_order(amount_inr, **kwargs)
        conn = get_connection()
        with transaction(conn):
            for sku in self._skus:
                conn.execute(
                    "UPDATE products SET stock_qty = 0 WHERE sku = ?", (sku,)
                )
        return gateway_order


def force_oversell(
    *,
    offer_id: str,
    option_id: str,
    agent_id: str,
    idempotency_key: str | None = None,
    payment_id: str | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Drive one checkout into the race window and let the saga answer it.

    Requires POLICY_MODE=live: the entire point is that money moves and is
    given back. In shadow mode there is no capture to compensate, so a
    rehearsal here would be theatre, and the guard refuses it.

    Without an explicit `client_factory` the run uses the internal simulated
    gateway, which keeps the rehearsal deterministic and hermetic. Passing a
    real client and a real `payment_id` runs the identical path against
    Razorpay test mode.

    Returns the structured OVERSOLD_MERCHANT_FAULT payload on success — the
    rehearsal succeeding means the sale failing gracefully.
    """
    mode.assert_may_move_money("forcing an oversell (a capture must really happen)")

    from kernel import checkout as checkout_kernel
    from store import offers

    offer = offers.require(offer_id)
    option = offers.option(offer, option_id)
    skus = [str(item["sku"]) for item in option.get("items", [])]

    simulated: SimulatedGateway | None = None
    if client_factory is None and payment_id is None:
        simulated = SimulatedGateway()
        gateway_client = simulated
        payment_id = simulated.new_payment_id()
    else:
        gateway_client = (
            client_factory() if client_factory is not None
            else checkout_kernel._default_client()
        )
        if payment_id is None:
            # A single-call rehearsal needs a payment id even though no real
            # one exists; the gateway stub accepts any id shaped like one.
            payment_id = f"pay_forced_{ids.order_id().lower()}"

    faulty = _ShelfFault(gateway_client, skus)

    # A fresh key per rehearsal: the demo fires repeatedly against the same
    # offer, and a replayed key would hand back the previous attempt's answer.
    key = idempotency_key or f"demo-oversell-{ids.order_id().lower()}"

    try:
        checkout_kernel.checkout(
            offer_id=offer_id,
            option_id=option_id,
            idempotency_key=key,
            agent_id=agent_id,
            payment_id=payment_id,
            client_factory=lambda: faulty,
        )
    except checkout_kernel.OversoldFault as exc:
        payload = dict(exc.payload)
        payload["gateway"] = "simulated" if simulated is not None else "razorpay_test"
        payload["rehearsal"] = True
        return payload

    raise RuntimeError(
        "the forced oversell did not fire: the checkout completed normally, "
        "which means the shelf fault did not land in the race window"
    )


def restock_for_offer(offer_id: str) -> list[str]:
    """Put the SKUs an offer needs back on the shelf and re-enable them.

    Demo plumbing, not policy: a rehearsal consumes a unit and self-heals the
    SKU away, so running it twice needs the shelf restored first. Restoring
    before the run — never after — keeps the compensation itself untouched.
    """
    from store import offers

    offer = offers.require(offer_id)
    needed: dict[str, int] = {}
    for option in offer["options"]:
        for item in option.get("items", []):
            sku = str(item["sku"])
            needed[sku] = max(needed.get(sku, 0), int(item.get("qty", 1)))

    conn = get_connection()
    touched: list[str] = []
    with transaction(conn):
        for sku, qty in sorted(needed.items()):
            conn.execute(
                """UPDATE products SET stock_qty = MAX(stock_qty, ?) WHERE sku = ?""",
                (qty, sku),
            )
            touched.append(sku)
    for sku in touched:
        catalog.cache.set_offerable(sku, True)
    return touched
