"""The nine bounds — exhaustive unit tests.

Full coverage of `kernel/bounds.py` is a hard requirement, so these tests are
organised by bound rather than by scenario: every bound gets its pass case, its
fail case, and its boundary case, because an off-by-one in a money limit is the
defect most likely to survive a casual test.

Three properties get specific attention:

**The boundary is inclusive.** A discount of exactly the limit passes. `_exceeds_pct`
compares `delta * 100 > limit * base` and never divides, so this is exact rather
than nearly right.

**No float ever touches an amount.** `test_no_float_in_arithmetic` asserts the
type, not just the value: a percentage that came out correct via float is still a
latent rounding bug.

**A gating bound is not a rejection.** Bound 6 failing means "a human must decide",
and a cart that trips only bound 6 must still report `offer_failed == False`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kernel.bounds import (
    APPROVE,
    BOUND_FUNCTIONS,
    BoundResult,
    GATING_BOUNDS,
    ItemVerdict,
    LineItem,
    REJECT_ITEM,
    ROLE_BASE,
    ROLE_UPSELL,
    check_daily_discount_budget,
    check_floor_price,
    check_idempotency_key,
    check_max_cart_discount_pct,
    check_max_discount_pct_per_sku,
    check_max_offers_per_session,
    check_max_txn_without_human,
    check_offer_fresh,
    check_stock_available,
    discount_pct,
    effective_max_discount_pct,
    evaluate_cart,
    evaluate_checkout,
    evaluate_item,
    evaluate_offer,
    floor_price_inr,
    _exceeds_pct,
    _payload_number,
)
from settings import (
    BOUND_IDS,
    DAILY_DISCOUNT_BUDGET_INR,
    MAX_CART_DISCOUNT_PCT,
    MAX_DISCOUNT_PCT_PER_SKU,
    MAX_OFFERS_PER_SESSION,
    MAX_TXN_WITHOUT_HUMAN_INR,
    OFFER_TTL_SECONDS,
)

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── the registry itself ────────────────────────────────────────────────────────


def test_every_declared_bound_has_an_implementation():
    """The declared set and the implemented set are the same set.

    This is the test that makes "ten bounds" a checkable claim rather than a
    number in a README. Adding a bound to settings without writing its function,
    or vice versa, fails here.
    """
    assert set(BOUND_FUNCTIONS) == set(BOUND_IDS)
    assert sorted(BOUND_FUNCTIONS) == list(range(1, 11))


def test_each_bound_returns_its_own_number():
    """A bound function cannot report a number other than its own."""
    calls = {
        1: dict(sku="X", list_total_inr=100, offered_total_inr=100),
        2: dict(cart_list_total_inr=100, cart_offered_total_inr=100),
        3: dict(sku="X", offered_price_inr=100, cost_inr=10),
        4: dict(discount_inr=0, spent_today_inr=0),
        5: dict(offers_made=0),
        6: dict(total_inr=1),
        7: dict(sku="X", requested_qty=1, available_qty=1),
        8: dict(issued_at=NOW, now=NOW),
        9: dict(key="k"),
        10: dict(sku="Y", base_sku="X", related_skus=frozenset({"Y"})),
    }
    for number, function in BOUND_FUNCTIONS.items():
        result = function(**calls[number])
        assert result.bound == number
        assert result.bound_id == BOUND_IDS[number]
        assert result.detail, f"bound {number} produced no explanation"


def test_only_bound_six_is_gating():
    """The gating set is exactly bound 6, and every bound knows which it is.

    A second bound joining this set would silently stop refusing carts, so the
    membership is asserted as a set rather than checked per-bound.
    """
    assert GATING_BOUNDS == frozenset({6})
    assert check_max_txn_without_human(total_inr=1).is_gating is True
    assert check_idempotency_key(key="k").is_gating is False


# ── bound 1: per-SKU discount ──────────────────────────────────────────────────


def test_bound_1_passes_under_the_cap():
    result = check_max_discount_pct_per_sku(
        sku="AT-PRO-BLK", list_total_inr=5000, offered_total_inr=4600
    )
    assert result.passed is True
    assert result.observed == Decimal("8.00")
    assert result.limit == MAX_DISCOUNT_PCT_PER_SKU


def test_bound_1_at_exactly_the_cap_passes():
    """12% of Rs 5,000 is Rs 600. Exactly the limit is inside it."""
    result = check_max_discount_pct_per_sku(
        sku="X", list_total_inr=5000, offered_total_inr=4400
    )
    assert result.observed == Decimal("12.00")
    assert result.passed is True


def test_bound_1_one_rupee_over_the_cap_fails():
    result = check_max_discount_pct_per_sku(
        sku="X", list_total_inr=5000, offered_total_inr=4399
    )
    assert result.passed is False
    assert "exceeds" in result.detail


def test_bound_1_sku_cap_can_tighten_but_never_widen():
    tight = check_max_discount_pct_per_sku(
        sku="X", list_total_inr=1000, offered_total_inr=940, sku_max_discount_pct=5
    )
    assert tight.passed is False and tight.limit == 5

    # A catalog row asking for 90% is clamped to the global 12%.
    wide = check_max_discount_pct_per_sku(
        sku="X", list_total_inr=1000, offered_total_inr=100, sku_max_discount_pct=90
    )
    assert wide.limit == MAX_DISCOUNT_PCT_PER_SKU
    assert wide.passed is False


def test_bound_1_ignores_a_price_above_list():
    """A line priced above list is not a negative discount."""
    result = check_max_discount_pct_per_sku(
        sku="X", list_total_inr=1000, offered_total_inr=1200
    )
    assert result.passed is True
    assert result.observed == Decimal("0.00")


def test_effective_cap_defaults_to_the_global_one():
    assert effective_max_discount_pct(None) == MAX_DISCOUNT_PCT_PER_SKU
    assert effective_max_discount_pct(3) == 3
    assert effective_max_discount_pct(99) == MAX_DISCOUNT_PCT_PER_SKU


# ── bound 2: cart discount ─────────────────────────────────────────────────────


def test_bound_2_catches_stacking_that_every_line_passes():
    """The reason bound 2 is not bound 1 applied to a total.

    Base at a legal 12% plus an upsell at a legal 12% is a 12% cart, which is
    fine. But a base at 12% and a much smaller upsell cut far deeper can push
    the cart past 15% while both lines individually pass.
    """
    result = check_max_cart_discount_pct(
        cart_list_total_inr=1000, cart_offered_total_inr=800
    )
    assert result.observed == Decimal("20.00")
    assert result.passed is False


def test_bound_2_at_exactly_the_cap_passes():
    result = check_max_cart_discount_pct(
        cart_list_total_inr=1000, cart_offered_total_inr=850
    )
    assert result.observed == Decimal("15.00")
    assert result.passed is True
    assert result.limit == MAX_CART_DISCOUNT_PCT


def test_bound_2_one_rupee_over_fails():
    result = check_max_cart_discount_pct(
        cart_list_total_inr=1000, cart_offered_total_inr=849
    )
    assert result.passed is False


# ── bound 3: floor price ───────────────────────────────────────────────────────


def test_bound_3_floor_rounds_up_not_down():
    """Rs 3,299 x 1.20 is Rs 3,958.80. The floor is Rs 3,959, not Rs 3,958."""
    assert floor_price_inr(3299) == 3959


def test_bound_3_explicit_floor_wins_when_it_is_stricter():
    assert floor_price_inr(3299, 4100) == 4100


def test_bound_3_explicit_floor_cannot_undercut_the_computed_one():
    """A catalog row cannot authorise selling below cost x 1.20."""
    assert floor_price_inr(3299, 1) == 3959


def test_bound_3_at_the_floor_passes_and_below_fails():
    at_floor = check_floor_price(sku="X", offered_price_inr=3959, cost_inr=3299)
    assert at_floor.passed is True
    below = check_floor_price(sku="X", offered_price_inr=3958, cost_inr=3299)
    assert below.passed is False
    assert below.limit == 3959


def test_bound_3_ignores_quantity():
    """Buying more of an underpriced item does not make the price legal."""
    single = check_floor_price(sku="X", offered_price_inr=100, cost_inr=1000)
    assert single.passed is False
    assert single.observed == 100  # the unit price, not a line total


# ── bound 4: daily discount budget ─────────────────────────────────────────────


def test_bound_4_measures_the_projected_total_not_the_current_one():
    """The cart that would breach the budget is the one refused."""
    result = check_daily_discount_budget(
        discount_inr=1, spent_today_inr=DAILY_DISCOUNT_BUDGET_INR
    )
    assert result.passed is False
    assert result.observed == DAILY_DISCOUNT_BUDGET_INR + 1


def test_bound_4_exactly_exhausting_the_budget_passes():
    result = check_daily_discount_budget(
        discount_inr=100, spent_today_inr=DAILY_DISCOUNT_BUDGET_INR - 100
    )
    assert result.passed is True
    assert result.observed == DAILY_DISCOUNT_BUDGET_INR


def test_bound_4_treats_a_negative_discount_as_zero():
    result = check_daily_discount_budget(discount_inr=-500, spent_today_inr=100)
    assert result.observed == 100


# ── bound 5: offers per session ────────────────────────────────────────────────


def test_bound_5_allows_the_declared_number_and_no_more():
    for already_made in range(MAX_OFFERS_PER_SESSION):
        assert check_max_offers_per_session(offers_made=already_made).passed is True
    refused = check_max_offers_per_session(offers_made=MAX_OFFERS_PER_SESSION)
    assert refused.passed is False
    assert str(MAX_OFFERS_PER_SESSION) in refused.detail


def test_bound_5_counts_the_offer_being_asked_about():
    """`offers_made` is the count so far, so the observed value is one more."""
    assert check_max_offers_per_session(offers_made=0).observed == 1


# ── bound 6: autonomous transaction limit (gating) ─────────────────────────────


def test_bound_6_at_the_limit_passes():
    result = check_max_txn_without_human(total_inr=MAX_TXN_WITHOUT_HUMAN_INR)
    assert result.passed is True


def test_bound_6_one_rupee_over_needs_a_human():
    result = check_max_txn_without_human(total_inr=MAX_TXN_WITHOUT_HUMAN_INR + 1)
    assert result.passed is False
    assert result.is_gating is True
    assert "approval" in result.detail


def test_bound_6_the_14997_case():
    """The headline Tier-2 case: 3 x Rs 4,999 needs a human."""
    result = check_max_txn_without_human(total_inr=14997)
    assert result.passed is False
    assert result.observed == 14997


# ── bound 7: stock ─────────────────────────────────────────────────────────────


def test_bound_7_requires_enough_for_the_requested_quantity():
    assert check_stock_available(sku="X", requested_qty=3, available_qty=3).passed
    assert not check_stock_available(sku="X", requested_qty=3, available_qty=2).passed


def test_bound_7_refuses_zero_stock_even_for_zero_requested():
    """Nothing sells at zero available; the minimum is a positive quantity."""
    assert not check_stock_available(sku="X", requested_qty=0, available_qty=0).passed


# ── bound 8: offer freshness ───────────────────────────────────────────────────


def test_bound_8_within_ttl_passes_and_at_the_ttl_still_passes():
    fresh = check_offer_fresh(issued_at=NOW, now=NOW + timedelta(seconds=1))
    assert fresh.passed is True
    at_edge = check_offer_fresh(
        issued_at=NOW, now=NOW + timedelta(seconds=OFFER_TTL_SECONDS)
    )
    assert at_edge.passed is True


def test_bound_8_one_second_past_the_ttl_fails():
    stale = check_offer_fresh(
        issued_at=NOW, now=NOW + timedelta(seconds=OFFER_TTL_SECONDS + 1)
    )
    assert stale.passed is False
    assert "past its" in stale.detail


def test_bound_8_refuses_an_offer_timestamped_in_the_future():
    """A future timestamp is a clock problem or a forged row, never a fresh offer."""
    result = check_offer_fresh(issued_at=NOW + timedelta(seconds=60), now=NOW)
    assert result.passed is False
    assert "future" in result.detail


def test_bound_8_honours_an_explicit_shorter_ttl():
    result = check_offer_fresh(
        issued_at=NOW, now=NOW + timedelta(seconds=10), ttl_seconds=5
    )
    assert result.passed is False
    assert result.limit == 5


# ── bound 9: idempotency key ───────────────────────────────────────────────────


@pytest.mark.parametrize("key", [None, "", "   ", "\t\n"])
def test_bound_9_refuses_an_absent_or_blank_key(key):
    result = check_idempotency_key(key=key)
    assert result.passed is False
    assert result.observed == "absent"


def test_bound_9_accepts_a_real_key():
    result = check_idempotency_key(key="checkout-abc-1")
    assert result.passed is True
    assert result.observed == "present"


# ── arithmetic ─────────────────────────────────────────────────────────────────


def test_no_float_in_arithmetic():
    """The type matters as much as the value.

    A percentage computed through float can be exactly right on the cases a test
    happens to pick and wrong on the case a buyer picks.
    """
    assert isinstance(discount_pct(4999, 4599), Decimal)
    assert not isinstance(discount_pct(4999, 4599), float)


def test_discount_pct_is_exact_to_two_places():
    assert discount_pct(4999, 4599) == Decimal("8.00")
    assert discount_pct(3, 2) == Decimal("33.33")


def test_discount_pct_never_reports_negative_or_divides_by_zero():
    assert discount_pct(1000, 1200) == Decimal("0.00")
    assert discount_pct(0, 0) == Decimal("0.00")
    assert discount_pct(-5, 10) == Decimal("0.00")


def test_exceeds_pct_never_divides_and_is_inclusive():
    assert _exceeds_pct(120, 1000, 12) is False  # exactly 12%
    assert _exceeds_pct(121, 1000, 12) is True
    assert _exceeds_pct(1, 0, 12) is False  # no base, nothing to exceed


def test_decimal_is_rendered_as_a_string_for_canonical_json():
    """Canonical JSON rejects Decimal, so payloads carry fixed-2dp strings.

    Quantizing uses Decimal's default half-even rounding, so 8.005 renders as
    "8.00" rather than "8.01". That is safe only because this function is display
    only: no bound compares a rendered percentage, so a rounded string can never
    admit a discount the exact comparison in `_exceeds_pct` would refuse.
    """
    assert _payload_number(Decimal("8.005")) == "8.00"
    assert _payload_number(Decimal("8.015")) == "8.02"
    assert _payload_number(Decimal("8.006")) == "8.01"
    assert _payload_number(12) == 12
    assert _payload_number("present") == "present"


# ── LineItem validation ────────────────────────────────────────────────────────


def test_line_item_totals_multiply_by_quantity():
    item = LineItem("X", 3, 4999, 4599, role=ROLE_BASE)
    assert item.list_total_inr == 14997
    assert item.offered_total_inr == 13797
    assert item.discount_inr == 1200


def test_line_item_discount_never_goes_negative():
    assert LineItem("X", 1, 100, 150).discount_inr == 0


@pytest.mark.parametrize(
    "kwargs, exc",
    [
        (dict(sku="X", qty=1, list_price_inr=100, offered_price_inr=90, role="free"), ValueError),
        (dict(sku="X", qty=0, list_price_inr=100, offered_price_inr=90), ValueError),
        (dict(sku="X", qty=1, list_price_inr=0, offered_price_inr=0), ValueError),
        (dict(sku="X", qty=1, list_price_inr=100, offered_price_inr=-1), ValueError),
        (dict(sku="X", qty=1.5, list_price_inr=100, offered_price_inr=90), TypeError),
        (dict(sku="X", qty=1, list_price_inr=100.0, offered_price_inr=90), TypeError),
        (dict(sku="X", qty=True, list_price_inr=100, offered_price_inr=90), TypeError),
    ],
)
def test_line_item_refuses_malformed_input(kwargs, exc):
    """A bool is not a quantity, and a float is not money."""
    with pytest.raises(exc):
        LineItem(**kwargs)


# ── per-item composition ───────────────────────────────────────────────────────


def _item(offered: int, *, qty: int = 1, list_price: int = 5000, role=ROLE_BASE):
    return LineItem("AT-TEST", qty, list_price, offered, role=role)


def test_evaluate_item_runs_every_bound_even_after_one_fails():
    """A partial evaluation would reveal the second violation only later.

    This line is both below its floor and over its discount cap. Both must be
    reported now, so one audit covers the whole problem.
    """
    verdict = evaluate_item(
        _item(1000), cost_inr=4000, explicit_floor_inr=None, available_qty=0
    )
    assert verdict.decision == REJECT_ITEM
    assert {b.bound for b in verdict.failed_bounds} == {1, 3, 7}
    assert len(verdict.bounds) == 3


def test_evaluate_item_quotes_the_first_failure_to_the_buyer():
    verdict = evaluate_item(_item(1000), cost_inr=4000, available_qty=10)
    assert verdict.failed_bound is not None
    assert verdict.failed_bound.bound == 1
    payload = verdict.as_payload()
    assert payload["failed_bound"] == 1
    assert payload["decision"] == REJECT_ITEM


def test_evaluate_item_approves_a_clean_line():
    verdict = evaluate_item(_item(4800), cost_inr=3000, available_qty=10)
    assert verdict.approved is True
    assert verdict.decision == APPROVE
    assert verdict.failed_bound is None
    assert "failed_bound" not in verdict.as_payload()


# ── cart composition ───────────────────────────────────────────────────────────

PRIVATE = {
    "AT-BASE": {"cost_inr": 3000, "floor_price_inr": 3600, "max_discount_pct": 12},
    "AT-UP": {"cost_inr": 200, "floor_price_inr": 250, "max_discount_pct": 12},
}
AVAILABLE = {"AT-BASE": 50, "AT-UP": 50}


def _cart(base_offered: int, upsell_offered: int | None = None):
    items = [LineItem("AT-BASE", 1, 5000, base_offered, role=ROLE_BASE)]
    if upsell_offered is not None:
        items.append(LineItem("AT-UP", 1, 500, upsell_offered, role=ROLE_UPSELL))
    return items


def test_empty_cart_is_a_programming_error_not_a_rejection():
    with pytest.raises(ValueError):
        evaluate_cart(
            [],
            private_by_sku=PRIVATE,
            available_by_sku=AVAILABLE,
            offers_made=0,
            spent_today_inr=0,
            issued_at=NOW,
            now=NOW,
        )


def test_a_rejected_base_item_fails_the_whole_offer():
    evaluation = evaluate_offer(
        _cart(1000),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    assert evaluation.offer_failed is True
    assert "base item rejected" in evaluation.failure_detail


def test_a_rejected_upsell_is_dropped_and_the_offer_survives():
    """An upsell that breaks a bound is removed, not fatal.

    The buyer still gets the thing they asked for; they just do not get the
    attachment that could not be priced legally.
    """
    evaluation = evaluate_offer(
        _cart(4800, upsell_offered=100),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    assert evaluation.offer_failed is False
    assert [i.sku for i in evaluation.approved_items] == ["AT-BASE"]
    assert [v.item.sku for v in evaluation.rejected_items] == ["AT-UP"]
    assert evaluation.total_inr == 4800


def test_cart_bounds_run_against_survivors_not_the_original_cart():
    """A dropped line must not also fail the cart it is no longer part of.

    Were the cart-discount bound measured before the upsell was removed, this
    cart would be refused for a discount on an item that is not being sold.
    """
    evaluation = evaluate_offer(
        _cart(4800, upsell_offered=1),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    cart_discount = next(b for b in evaluation.cart_bounds if b.bound == 2)
    assert cart_discount.passed is True
    assert evaluation.list_total_inr == 5000  # the upsell is gone from both totals


def test_a_cart_where_nothing_survives_reports_that():
    evaluation = evaluate_offer(
        [LineItem("AT-UP", 1, 500, 1, role=ROLE_UPSELL)],
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    assert evaluation.offer_failed is True
    assert evaluation.failure_detail == "no items survived the bounds"


def test_tripping_only_the_gating_bound_does_not_fail_the_offer():
    """The single most important distinction in this module.

    Rs 14,997 trips bound 6. The offer is valid, priced, and sellable; it just
    cannot complete without a human. Treating this as a rejection would turn the
    approval queue into an error page.
    """
    evaluation = evaluate_offer(
        [LineItem("AT-BASE", 3, 5000, 4999, role=ROLE_BASE)],
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    assert evaluation.total_inr == 14997
    assert evaluation.tripped_bounds == (6,)
    assert evaluation.rejecting_bounds == ()
    assert evaluation.offer_failed is False


def test_a_failing_cart_bound_that_is_not_gating_does_fail_the_offer():
    evaluation = evaluate_offer(
        _cart(4800),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=DAILY_DISCOUNT_BUDGET_INR,
        now=NOW,
    )
    assert 4 in evaluation.rejecting_bounds
    assert evaluation.offer_failed is True


def test_offer_time_checks_the_session_quota_but_not_freshness_or_idempotency():
    """The switches are the contract between the two moments.

    Freshness cannot apply to an offer that does not exist yet, and requiring an
    idempotency key to receive a quote would mean committing before seeing a price.
    """
    evaluation = evaluate_offer(
        _cart(4800),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    numbers = {b.bound for b in evaluation.cart_bounds}
    assert 5 in numbers
    assert 8 not in numbers
    assert 9 not in numbers


def test_checkout_time_checks_freshness_and_idempotency_but_not_the_quota():
    """The quota was consumed when the offer was issued.

    Charging it again would refuse the second half of a legitimate two-offer
    session, where the agent accepts the offer it was already given.
    """
    evaluation = evaluate_checkout(
        _cart(4800),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        spent_today_inr=0,
        issued_at=NOW,
        now=NOW,
        idempotency_key="k-1",
    )
    numbers = {b.bound for b in evaluation.cart_bounds}
    assert 8 in numbers
    assert 9 in numbers
    assert 5 not in numbers


def test_checkout_re_evaluates_stock_that_moved_since_the_offer():
    """An offer is evidence of what was true, not authority for what happens now."""
    evaluation = evaluate_checkout(
        _cart(4800),
        private_by_sku=PRIVATE,
        available_by_sku={"AT-BASE": 0, "AT-UP": 0},
        spent_today_inr=0,
        issued_at=NOW,
        now=NOW,
        idempotency_key="k-1",
    )
    assert evaluation.offer_failed is True
    assert 7 in evaluation.rejecting_bounds


def test_checkout_refuses_a_missing_idempotency_key():
    evaluation = evaluate_checkout(
        _cart(4800),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        spent_today_inr=0,
        issued_at=NOW,
        now=NOW,
        idempotency_key=None,
    )
    assert evaluation.offer_failed is True
    assert 9 in evaluation.rejecting_bounds


def test_checkout_refuses_a_stale_offer():
    evaluation = evaluate_checkout(
        _cart(4800),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        spent_today_inr=0,
        issued_at=NOW,
        now=NOW + timedelta(seconds=OFFER_TTL_SECONDS + 1),
        idempotency_key="k-1",
    )
    assert evaluation.offer_failed is True
    assert 8 in evaluation.rejecting_bounds


def test_a_missing_sku_in_availability_reads_as_zero_not_as_unlimited():
    """Absence of a stock number is not permission to sell."""
    evaluation = evaluate_offer(
        _cart(4800),
        private_by_sku=PRIVATE,
        available_by_sku={},
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    assert evaluation.offer_failed is True
    assert 7 in evaluation.rejecting_bounds


# ── evaluation payloads ────────────────────────────────────────────────────────


def test_evaluation_payload_carries_the_numbers_not_just_the_verdict():
    evaluation = evaluate_offer(
        _cart(4600, upsell_offered=480),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    payload = evaluation.as_payload()
    assert payload["total_inr"] == 5080
    assert payload["list_total_inr"] == 5500
    assert payload["discount_inr"] == 420
    assert payload["discount_pct"] == "7.64"
    assert isinstance(payload["discount_pct"], str)  # canonical JSON has no Decimal
    assert payload["offer_failed"] is False


def test_all_bounds_includes_both_item_and_cart_level_results():
    evaluation = evaluate_offer(
        _cart(4800, upsell_offered=480),
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    numbers = {b.bound for b in evaluation.all_bounds}
    # Two items x three item bounds, plus the cart bounds offer time applies.
    assert {1, 3, 7} <= numbers
    assert {2, 4, 5, 6} <= numbers


def test_bound_result_payload_omits_sku_when_the_bound_is_cart_level():
    cart_level = check_max_cart_discount_pct(
        cart_list_total_inr=100, cart_offered_total_inr=100
    ).as_payload()
    assert "sku" not in cart_level

    item_level = check_floor_price(
        sku="AT-BASE", offered_price_inr=100, cost_inr=10
    ).as_payload()
    assert item_level["sku"] == "AT-BASE"


def test_tripped_bounds_are_sorted_and_deduplicated():
    """Two items failing the same bound name it once."""
    evaluation = evaluate_offer(
        [
            LineItem("AT-BASE", 1, 5000, 1000, role=ROLE_BASE),
            LineItem("AT-UP", 1, 500, 100, role=ROLE_UPSELL),
        ],
        private_by_sku=PRIVATE,
        available_by_sku=AVAILABLE,
        offers_made=0,
        spent_today_inr=0,
        now=NOW,
    )
    tripped = evaluation.tripped_bounds
    assert list(tripped) == sorted(set(tripped))
