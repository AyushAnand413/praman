"""The nine bounds — the kernel's veto surface.

Each bound is an independent pure function: same inputs, same verdict, no
database, no clock, no network. Every limit they compare against is a named
constant in `settings`, frozen at the start of the build, so changing a bound is
a code change with a diff rather than a config tweak.

Four properties the callers depend on:

* **Independence.** A bound never consults another bound. The composers below
  decide what to do with a set of results; the bounds themselves only measure.
* **Per-item evaluation.** Bounds are checked per proposed upsell,
  independently. One upsell failing rejects that item and the rest of the offer
  proceeds; a base-item failure fails the whole offer.
* **Named rejections.** Every result carries the bound number and its
  identifier, so a rejection is always ledgered with the specific bound that
  fired. A silent rejection is a bug.
* **Re-evaluated at checkout.** Nothing here caches or memoizes. The same
  functions run again before money moves, because stock, the daily budget, and
  the clock all move during the 300 seconds an offer stays valid.

Percentages are compared in exact arithmetic rather than by computing a float
first: `delta * 100 > limit * base` instead of `delta / base * 100 > limit`.
A bound on money must not depend on binary floating-point rounding, and a
discount of exactly 12% must pass a 12% limit every time.

One asymmetry worth naming, because it changes what callers do with the result:
eight of the nine bounds reject when they fail. Bound 6 — the largest
transaction allowed without a human — is different. Exceeding it is not an
error, it is the signal that a human must approve, so it reports as tripped and
the gate turns that into a hold rather than a refusal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from settings import (
    BOUND_IDS,
    DAILY_DISCOUNT_BUDGET_INR,
    FLOOR_PRICE_COST_MULTIPLIER,
    MAX_CART_DISCOUNT_PCT,
    MAX_DISCOUNT_PCT_PER_SKU,
    MAX_OFFERS_PER_SESSION,
    MAX_TXN_WITHOUT_HUMAN_INR,
    MIN_STOCK_QTY,
    OFFER_TTL_SECONDS,
)

#: Item roles. A base-item rejection fails the offer; an upsell rejection
#: removes only that line.
ROLE_BASE = "base"
ROLE_UPSELL = "upsell"

#: Per-item decisions.
APPROVE = "APPROVE"
REJECT_ITEM = "REJECT_ITEM"

#: Bound 6 is a gating bound, not a rejecting one: tripping it routes the
#: transaction to a human instead of refusing it. Callers must not treat a
#: bound-6 trip as a rejection.
GATING_BOUNDS: frozenset[int] = frozenset({6})

#: Bounds whose limit is confidential and must not be serialised. Bound 3's
#: limit is the floor price, which is derived from cost — see
#: `BoundResult.as_payload`.
PRIVATE_LIMIT_BOUNDS: frozenset[int] = frozenset({3})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundResult:
    """One bound's measurement of one thing.

    `observed` and `limit` are carried alongside the verdict so the ledger entry
    and the buyer-facing rejection can both state the actual numbers rather than
    just naming the rule.
    """

    bound: int
    passed: bool
    observed: Any
    limit: Any
    detail: str
    sku: str | None = None

    @property
    def bound_id(self) -> str:
        return BOUND_IDS[self.bound]

    @property
    def is_gating(self) -> bool:
        """True for a bound whose failure means 'needs a human', not 'refuse'."""
        return self.bound in GATING_BOUNDS

    def as_payload(self) -> dict[str, Any]:
        """Ledger/receipt form. Every rejection names its bound.

        Bound 3's limit is withheld. Both the ledger and the policy receipt are
        buyer-facing — the receipt ships with the offer and `/audit` is public —
        and the floor price is `cost x 1.20`, so publishing it publishes the cost
        one division away. The verdict still travels: `observed` is the price we
        quoted, which was public the moment it was quoted, and `passed` says
        whether it cleared. What is withheld is the margin of compliance.

        Bound 1's limit is published deliberately, and the distinction is the
        point: a per-SKU discount cap is a commitment about how far this store
        will go, and the selling envelope already hands it to the model. It is
        not a function of cost. The floor is.
        """
        payload: dict[str, Any] = {
            "bound": self.bound,
            "bound_id": self.bound_id,
            "passed": self.passed,
            "observed": _payload_number(self.observed),
            "limit": None if self.bound in PRIVATE_LIMIT_BOUNDS
            else _payload_number(self.limit),
            "detail": self.detail,
        }
        if self.sku is not None:
            payload["sku"] = self.sku
        return payload


@dataclass(frozen=True)
class LineItem:
    """One priced line in a proposal. Unit prices, whole rupees."""

    sku: str
    qty: int
    list_price_inr: int
    offered_price_inr: int
    role: str = ROLE_UPSELL

    def __post_init__(self) -> None:
        if self.role not in (ROLE_BASE, ROLE_UPSELL):
            raise ValueError(f"role must be {ROLE_BASE!r} or {ROLE_UPSELL!r}, got {self.role!r}")
        for name in ("qty", "list_price_inr", "offered_price_inr"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an int, got {type(value).__name__}")
        if self.qty < 1:
            raise ValueError("qty must be at least 1")
        if self.list_price_inr <= 0:
            raise ValueError("list_price_inr must be positive")
        if self.offered_price_inr < 0:
            raise ValueError("offered_price_inr must not be negative")

    @property
    def list_total_inr(self) -> int:
        return self.list_price_inr * self.qty

    @property
    def offered_total_inr(self) -> int:
        return self.offered_price_inr * self.qty

    @property
    def discount_inr(self) -> int:
        """Rupees given away on this line. Never negative."""
        return max(0, self.list_total_inr - self.offered_total_inr)


@dataclass(frozen=True)
class ItemVerdict:
    """The per-item outcome: a decision plus every bound that produced it."""

    item: LineItem
    decision: str
    bounds: tuple[BoundResult, ...]

    @property
    def approved(self) -> bool:
        return self.decision == APPROVE

    @property
    def failed_bounds(self) -> tuple[BoundResult, ...]:
        return tuple(b for b in self.bounds if not b.passed)

    @property
    def failed_bound(self) -> BoundResult | None:
        """The first bound that fired, which is the one quoted to the buyer."""
        failures = self.failed_bounds
        return failures[0] if failures else None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sku": self.item.sku,
            "role": self.item.role,
            "qty": self.item.qty,
            "offered_price_inr": self.item.offered_price_inr,
            "list_price_inr": self.item.list_price_inr,
            "decision": self.decision,
            "bounds": [b.as_payload() for b in self.bounds],
        }
        failed = self.failed_bound
        if failed is not None:
            payload["failed_bound"] = failed.bound
            payload["failed_bound_id"] = failed.bound_id
        return payload


# ---------------------------------------------------------------------------
# Exact arithmetic helpers
# ---------------------------------------------------------------------------


def _payload_number(value: Any) -> Any:
    """Decimals have no canonical JSON form; render them as fixed-2dp strings."""
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    return value


def discount_pct(list_total_inr: int, offered_total_inr: int) -> Decimal:
    """Discount as a percentage of list, exact to 2dp. Never negative.

    Reporting only. Comparisons use `_exceeds_pct`, which never divides.
    """
    if list_total_inr <= 0:
        return Decimal("0.00")
    delta = max(0, list_total_inr - offered_total_inr)
    return (Decimal(delta) * 100 / Decimal(list_total_inr)).quantize(Decimal("0.01"))


def _exceeds_pct(delta_inr: int, base_inr: int, limit_pct: int | Decimal) -> bool:
    """True when delta/base exceeds limit_pct, without ever dividing.

    `delta * 100 > limit * base` is equivalent for a positive base and is exact,
    so a discount of exactly the limit always passes.
    """
    if base_inr <= 0:
        return False
    return Decimal(delta_inr) * 100 > Decimal(limit_pct) * Decimal(base_inr)


def effective_max_discount_pct(sku_max_discount_pct: int | None) -> int:
    """The stricter of the global cap and the SKU's own cap.

    The minimum, never the maximum: a per-SKU limit exists to tighten the global
    one, and a catalog row must not be able to widen it.
    """
    if sku_max_discount_pct is None:
        return MAX_DISCOUNT_PCT_PER_SKU
    return min(MAX_DISCOUNT_PCT_PER_SKU, int(sku_max_discount_pct))


def floor_price_inr(cost_inr: int, explicit_floor_inr: int | None = None) -> int:
    """The lowest price this SKU may be sold at, in whole rupees.

    `cost x 1.20`, rounded UP, and never below an explicit floor from the
    catalog. Rounding up matters: at a cost of Rs 3,299 the multiplier gives
    Rs 3,958.80, and Rs 3,958 is below viable margin, so the floor is Rs 3,959.
    """
    computed = math.ceil(Decimal(int(cost_inr)) * FLOOR_PRICE_COST_MULTIPLIER)
    if explicit_floor_inr is None:
        return int(computed)
    return max(int(computed), int(explicit_floor_inr))


# ---------------------------------------------------------------------------
# The nine bounds
# ---------------------------------------------------------------------------


def check_max_discount_pct_per_sku(
    *,
    sku: str,
    list_total_inr: int,
    offered_total_inr: int,
    sku_max_discount_pct: int | None = None,
) -> BoundResult:
    """Bound 1 — runaway generosity on a single item."""
    limit = effective_max_discount_pct(sku_max_discount_pct)
    delta = max(0, list_total_inr - offered_total_inr)
    observed = discount_pct(list_total_inr, offered_total_inr)
    passed = not _exceeds_pct(delta, list_total_inr, limit)
    return BoundResult(
        bound=1,
        passed=passed,
        observed=observed,
        limit=limit,
        sku=sku,
        detail=(
            f"{sku}: discount {observed}% is within the {limit}% per-SKU cap"
            if passed
            else f"{sku}: discount {observed}% exceeds the {limit}% per-SKU cap"
        ),
    )


def check_max_cart_discount_pct(
    *, cart_list_total_inr: int, cart_offered_total_inr: int
) -> BoundResult:
    """Bound 2 — discount stacking across a cart.

    Separate from bound 1 on purpose: three items each discounted a legal 12%
    still produce a 12% cart, but a base item at 12% plus a deeply cut upsell
    can push the cart past 15% while every individual line looks fine.
    """
    delta = max(0, cart_list_total_inr - cart_offered_total_inr)
    observed = discount_pct(cart_list_total_inr, cart_offered_total_inr)
    passed = not _exceeds_pct(delta, cart_list_total_inr, MAX_CART_DISCOUNT_PCT)
    return BoundResult(
        bound=2,
        passed=passed,
        observed=observed,
        limit=MAX_CART_DISCOUNT_PCT,
        detail=(
            f"cart discount {observed}% is within the {MAX_CART_DISCOUNT_PCT}% cap"
            if passed
            else f"cart discount {observed}% exceeds the {MAX_CART_DISCOUNT_PCT}% cap"
        ),
    )


def check_floor_price(
    *,
    sku: str,
    offered_price_inr: int,
    cost_inr: int,
    explicit_floor_inr: int | None = None,
) -> BoundResult:
    """Bound 3 — selling below viable margin.

    Compares unit price against the unit floor. Quantity cannot rescue a line
    priced below its floor, so this deliberately ignores qty.
    """
    floor = floor_price_inr(cost_inr, explicit_floor_inr)
    passed = offered_price_inr >= floor
    return BoundResult(
        bound=3,
        passed=passed,
        observed=offered_price_inr,
        limit=floor,
        sku=sku,
        # The floor is not named in the detail, for the same reason
        # `as_payload` withholds the limit: this string travels into the policy
        # receipt and the public ledger, and the floor is cost x 1.20. The
        # verdict is what a buyer needs; the number behind it is not.
        detail=(
            f"{sku}: Rs {offered_price_inr} is at or above its floor price"
            if passed
            else f"{sku}: Rs {offered_price_inr} is below its floor price"
        ),
    )


def check_daily_discount_budget(
    *, discount_inr: int, spent_today_inr: int
) -> BoundResult:
    """Bound 4 — discounting the store to death overnight.

    Measures the total after this cart is added, not before, so the cart that
    would breach the budget is the one refused.
    """
    projected = int(spent_today_inr) + max(0, int(discount_inr))
    passed = projected <= DAILY_DISCOUNT_BUDGET_INR
    return BoundResult(
        bound=4,
        passed=passed,
        observed=projected,
        limit=DAILY_DISCOUNT_BUDGET_INR,
        detail=(
            f"daily discount spend would reach Rs {projected} of the "
            f"Rs {DAILY_DISCOUNT_BUDGET_INR} budget"
            if passed
            else f"daily discount spend would reach Rs {projected}, over the "
            f"Rs {DAILY_DISCOUNT_BUDGET_INR} budget"
        ),
    )


def check_max_offers_per_session(*, offers_made: int) -> BoundResult:
    """Bound 5 — nagging the buyer agent.

    `offers_made` is the count already issued, so the bound asks whether one
    more may be issued. The limit is published in the discovery manifest as a
    public commitment, which makes exceeding it a broken promise as well as a
    policy breach.
    """
    projected = int(offers_made) + 1
    passed = projected <= MAX_OFFERS_PER_SESSION
    return BoundResult(
        bound=5,
        passed=passed,
        observed=projected,
        limit=MAX_OFFERS_PER_SESSION,
        detail=(
            f"offer {projected} of {MAX_OFFERS_PER_SESSION} allowed this session"
            if passed
            else f"session already used all {MAX_OFFERS_PER_SESSION} offers"
        ),
    )


def check_max_txn_without_human(*, total_inr: int) -> BoundResult:
    """Bound 6 — large autonomous spend.

    A gating bound. Failing it does not reject the transaction; it means the
    transaction may not complete without a human, which the gate expresses as
    Tier 2 and the checkout path expresses as a held order.
    """
    total = int(total_inr)
    passed = total <= MAX_TXN_WITHOUT_HUMAN_INR
    return BoundResult(
        bound=6,
        passed=passed,
        observed=total,
        limit=MAX_TXN_WITHOUT_HUMAN_INR,
        detail=(
            f"Rs {total} is within the Rs {MAX_TXN_WITHOUT_HUMAN_INR} autonomous limit"
            if passed
            else f"Rs {total} exceeds the Rs {MAX_TXN_WITHOUT_HUMAN_INR} autonomous "
            "limit and needs merchant approval"
        ),
    )


def check_stock_available(
    *, sku: str, requested_qty: int, available_qty: int
) -> BoundResult:
    """Bound 7 — selling something that does not exist.

    `available_qty` is stock minus live holds, not the raw column: two agents
    must not both be sold the last unit. Mandatory and not tunable.
    """
    requested = int(requested_qty)
    available = int(available_qty)
    passed = available >= max(requested, MIN_STOCK_QTY)
    return BoundResult(
        bound=7,
        passed=passed,
        observed=available,
        limit=max(requested, MIN_STOCK_QTY),
        sku=sku,
        detail=(
            f"{sku}: {available} available covers the {requested} requested"
            if passed
            else f"{sku}: {available} available cannot cover the {requested} requested"
        ),
    )


def check_offer_fresh(
    *, issued_at: datetime, now: datetime, ttl_seconds: int = OFFER_TTL_SECONDS
) -> BoundResult:
    """Bound 8 — honouring stale prices.

    Derives the deadline from `issued_at` rather than trusting a stored
    `expires_at`, so an offer row whose expiry was widened is still refused.
    """
    age = (now - issued_at).total_seconds()
    passed = 0 <= age <= ttl_seconds
    age_shown = int(age)
    if age < 0:
        detail = f"offer is timestamped {abs(age_shown)}s in the future"
    elif passed:
        detail = f"offer is {age_shown}s old, within its {ttl_seconds}s validity"
    else:
        detail = f"offer is {age_shown}s old, past its {ttl_seconds}s validity"
    return BoundResult(
        bound=8,
        passed=passed,
        observed=age_shown,
        limit=ttl_seconds,
        detail=detail,
    )


def check_idempotency_key(*, key: str | None) -> BoundResult:
    """Bound 9 — double-charging.

    Presence only. Whether the key was seen before is a storage question, and
    the unique index in `idempotency_keys` answers it; this bound just refuses a
    checkout that arrived without one. Mandatory and not tunable.
    """
    text = (key or "").strip()
    passed = bool(text)
    return BoundResult(
        bound=9,
        passed=passed,
        observed="present" if passed else "absent",
        limit="required",
        detail=(
            "idempotency key present"
            if passed
            else "checkout requires an idempotency key so a retry cannot double-charge"
        ),
    )


#: Bound number -> the function that evaluates it. Used by the coverage test to
#: assert that every declared bound has an implementation and vice versa.
BOUND_FUNCTIONS = {
    1: check_max_discount_pct_per_sku,
    2: check_max_cart_discount_pct,
    3: check_floor_price,
    4: check_daily_discount_budget,
    5: check_max_offers_per_session,
    6: check_max_txn_without_human,
    7: check_stock_available,
    8: check_offer_fresh,
    9: check_idempotency_key,
}


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def evaluate_item(
    item: LineItem,
    *,
    cost_inr: int,
    explicit_floor_inr: int | None = None,
    sku_max_discount_pct: int | None = None,
    available_qty: int,
) -> ItemVerdict:
    """Run the three per-item bounds (1, 3, 7) against one line.

    Every bound runs even after one has failed. A partial evaluation would put
    a second violation in the ledger only once the first was fixed, which turns
    one audit into several.
    """
    results = (
        check_max_discount_pct_per_sku(
            sku=item.sku,
            list_total_inr=item.list_total_inr,
            offered_total_inr=item.offered_total_inr,
            sku_max_discount_pct=sku_max_discount_pct,
        ),
        check_floor_price(
            sku=item.sku,
            offered_price_inr=item.offered_price_inr,
            cost_inr=cost_inr,
            explicit_floor_inr=explicit_floor_inr,
        ),
        check_stock_available(
            sku=item.sku, requested_qty=item.qty, available_qty=available_qty
        ),
    )
    decision = APPROVE if all(r.passed for r in results) else REJECT_ITEM
    return ItemVerdict(item=item, decision=decision, bounds=results)


@dataclass(frozen=True)
class CartEvaluation:
    """The whole-cart outcome: which lines survived, and every bound result.

    `offer_failed` is the one field callers must branch on first — a rejected
    base item means there is nothing to sell, so no amount of surviving upsells
    makes the offer valid.
    """

    item_verdicts: tuple[ItemVerdict, ...]
    cart_bounds: tuple[BoundResult, ...]
    offer_failed: bool
    failure_detail: str | None

    @property
    def approved_items(self) -> tuple[LineItem, ...]:
        return tuple(v.item for v in self.item_verdicts if v.approved)

    @property
    def rejected_items(self) -> tuple[ItemVerdict, ...]:
        return tuple(v for v in self.item_verdicts if not v.approved)

    @property
    def all_bounds(self) -> tuple[BoundResult, ...]:
        item_bounds = tuple(b for v in self.item_verdicts for b in v.bounds)
        return item_bounds + self.cart_bounds

    @property
    def tripped_bounds(self) -> tuple[int, ...]:
        """Distinct bound numbers that failed, ascending. Includes bound 6.

        The gate reads this. Bound 6 belongs here even though it is not a
        rejection: the gate has to see it to assign Tier 2.
        """
        return tuple(sorted({b.bound for b in self.all_bounds if not b.passed}))

    @property
    def rejecting_bounds(self) -> tuple[int, ...]:
        """Failed bounds that actually refuse something — bound 6 excluded."""
        return tuple(b for b in self.tripped_bounds if b not in GATING_BOUNDS)

    @property
    def total_inr(self) -> int:
        """Charge for the surviving lines. The only amount that may be charged."""
        return sum(item.offered_total_inr for item in self.approved_items)

    @property
    def list_total_inr(self) -> int:
        return sum(item.list_total_inr for item in self.approved_items)

    @property
    def discount_inr(self) -> int:
        return max(0, self.list_total_inr - self.total_inr)

    @property
    def discount_pct(self) -> Decimal:
        return discount_pct(self.list_total_inr, self.total_inr)

    def as_payload(self) -> dict[str, Any]:
        return {
            "items": [v.as_payload() for v in self.item_verdicts],
            "cart_bounds": [b.as_payload() for b in self.cart_bounds],
            "offer_failed": self.offer_failed,
            "failure_detail": self.failure_detail,
            "total_inr": self.total_inr,
            "list_total_inr": self.list_total_inr,
            "discount_inr": self.discount_inr,
            "discount_pct": str(self.discount_pct),
            "tripped_bounds": list(self.tripped_bounds),
        }


def evaluate_cart(
    items: list[LineItem],
    *,
    private_by_sku: dict[str, dict[str, Any]],
    available_by_sku: dict[str, int],
    offers_made: int,
    spent_today_inr: int,
    issued_at: datetime,
    now: datetime,
    idempotency_key: str | None = None,
    check_freshness: bool = True,
    check_session_quota: bool = True,
    check_idempotency: bool = False,
    ttl_seconds: int = OFFER_TTL_SECONDS,
) -> CartEvaluation:
    """Evaluate every applicable bound over a whole cart.

    Two-stage on purpose. Per-item bounds run first and may drop upsell lines;
    the cart-level bounds then run against the *surviving* total, because a cart
    whose over-discounted upsell was already removed should not also fail the
    cart-discount bound for a line that is no longer being sold.

    The three switches exist because the same function serves both offer time
    and checkout time, and the two moments care about different bounds. Prefer
    `evaluate_offer` and `evaluate_checkout` over calling this directly — they
    set the switches to the combination each flow requires, so a call site cannot
    accidentally skip a bound that mattered.
    """
    if not items:
        raise ValueError("a cart with no items has nothing to bound")

    item_verdicts = tuple(
        evaluate_item(
            item,
            cost_inr=int(private_by_sku[item.sku]["cost_inr"]),
            explicit_floor_inr=private_by_sku[item.sku].get("floor_price_inr"),
            sku_max_discount_pct=private_by_sku[item.sku].get("max_discount_pct"),
            available_qty=int(available_by_sku.get(item.sku, 0)),
        )
        for item in items
    )

    base_rejected = [
        v for v in item_verdicts if v.item.role == ROLE_BASE and not v.approved
    ]
    survivors = [v.item for v in item_verdicts if v.approved]

    cart_bounds: list[BoundResult] = []
    if check_session_quota:
        cart_bounds.append(check_max_offers_per_session(offers_made=offers_made))
    if check_idempotency:
        cart_bounds.append(check_idempotency_key(key=idempotency_key))
    if check_freshness:
        cart_bounds.append(
            check_offer_fresh(issued_at=issued_at, now=now, ttl_seconds=ttl_seconds)
        )

    if survivors:
        list_total = sum(i.list_total_inr for i in survivors)
        offered_total = sum(i.offered_total_inr for i in survivors)
        cart_bounds.append(
            check_max_cart_discount_pct(
                cart_list_total_inr=list_total, cart_offered_total_inr=offered_total
            )
        )
        cart_bounds.append(
            check_daily_discount_budget(
                discount_inr=max(0, list_total - offered_total),
                spent_today_inr=spent_today_inr,
            )
        )
        cart_bounds.append(check_max_txn_without_human(total_inr=offered_total))

    failure_detail: str | None = None
    if base_rejected:
        failed = base_rejected[0].failed_bound
        failure_detail = (
            f"base item rejected: {failed.detail}"
            if failed
            else "base item rejected"
        )
    elif not survivors:
        failure_detail = "no items survived the bounds"
    else:
        rejecting = [
            b for b in cart_bounds if not b.passed and b.bound not in GATING_BOUNDS
        ]
        if rejecting:
            failure_detail = rejecting[0].detail

    return CartEvaluation(
        item_verdicts=item_verdicts,
        cart_bounds=tuple(cart_bounds),
        offer_failed=failure_detail is not None,
        failure_detail=failure_detail,
    )


def evaluate_offer(
    items: list[LineItem],
    *,
    private_by_sku: dict[str, dict[str, Any]],
    available_by_sku: dict[str, int],
    offers_made: int,
    spent_today_inr: int,
    now: datetime,
) -> CartEvaluation:
    """Offer time: eight bounds apply.

    The session quota (5) applies because this call is what would consume it.
    Freshness (8) does not — the offer being priced does not exist yet, so there
    is nothing to be stale. Neither does the idempotency key (9), which belongs
    to checkout; requiring one to receive a quote would mean an agent had to
    commit to a purchase before seeing the price.
    """
    return evaluate_cart(
        items,
        private_by_sku=private_by_sku,
        available_by_sku=available_by_sku,
        offers_made=offers_made,
        spent_today_inr=spent_today_inr,
        issued_at=now,
        now=now,
        check_freshness=False,
        check_session_quota=True,
        check_idempotency=False,
    )


def evaluate_checkout(
    items: list[LineItem],
    *,
    private_by_sku: dict[str, dict[str, Any]],
    available_by_sku: dict[str, int],
    spent_today_inr: int,
    issued_at: datetime,
    now: datetime,
    idempotency_key: str | None,
    ttl_seconds: int = OFFER_TTL_SECONDS,
) -> CartEvaluation:
    """Checkout time: the same bounds re-run, plus the two this moment adds.

    Every bound is evaluated again rather than trusting the offer's verdict.
    Stock, the daily budget, and the catalog can all have moved during the 300
    seconds an offer stays valid, so a decision made at offer time is evidence
    of what was true then, not authority for what happens now.

    The session quota (5) is not re-checked: it was consumed when the offer was
    issued, and charging it twice would refuse the second half of a legitimate
    two-offer session. Freshness (8) and the idempotency key (9) apply here and
    only here.
    """
    return evaluate_cart(
        items,
        private_by_sku=private_by_sku,
        available_by_sku=available_by_sku,
        offers_made=0,
        spent_today_inr=spent_today_inr,
        issued_at=issued_at,
        now=now,
        idempotency_key=idempotency_key,
        check_freshness=True,
        check_session_quota=False,
        check_idempotency=True,
        ttl_seconds=ttl_seconds,
    )
