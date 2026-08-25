"""The three gate tiers — exhaustive unit tests.

The bounds decide whether an offer is legal; the gate decides who has to say yes.
Full coverage of `kernel/gates.py` is a hard requirement, so every Tier-2 trigger
gets its own test and the composition rules get their own section.

Three properties matter more than the rest:

**Highest matching trigger wins.** A transaction that matches both a Tier-1 and a
Tier-2 condition is Tier 2. Tested per trigger, because an `elif` in the wrong
place would silently downgrade one of five distinct escalation reasons.

**Tier 0 is an allowlist, not a fallback.** Anything that fails to qualify for it
lands on Tier 1, including combinations the stated rules never named. The
`test_the_gap_*` tests pin that resolution so a future edit cannot quietly flip it
toward less authority.

**No timeout, no auto-approve.** There is no input to `assign_tier` that turns a
Tier-2 decision into a proceed, and no age at which one expires into approval.
Nothing in this module can be waited out.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kernel.gates import (
    GateDecision,
    GateTrigger,
    TIER_ACTIONS,
    TIER_AUTO,
    TIER_AUTO_MAX_DISCOUNT_PCT,
    TIER_AUTO_MAX_TOTAL_INR,
    TIER_HUMAN,
    TIER_HUMAN_DISCOUNT_PCT,
    TIER_MANDATE,
    TIER_NAMES,
    assign_tier,
)
from settings import MANDATE_REQUIRED_ABOVE_INR, MAX_TXN_WITHOUT_HUMAN_INR


def _codes(decision: GateDecision) -> set[str]:
    return {t.code for t in decision.triggers}


def _deciding_codes(decision: GateDecision) -> set[str]:
    return {t.code for t in decision.deciding_triggers}


# ── the tier table ─────────────────────────────────────────────────────────────


def test_there_are_exactly_three_tiers():
    """Three tiers, each with a name and an action. A fourth would need a design."""
    assert sorted(TIER_NAMES) == [TIER_AUTO, TIER_MANDATE, TIER_HUMAN] == [0, 1, 2]
    assert sorted(TIER_ACTIONS) == [0, 1, 2]


def test_tier_actions_say_what_happens_next():
    assert TIER_ACTIONS[TIER_AUTO] == "proceed"
    assert TIER_ACTIONS[TIER_MANDATE] == "verify_mandate_then_proceed"
    assert TIER_ACTIONS[TIER_HUMAN] == "halt_for_merchant_approval"


def test_no_action_is_an_auto_approve_after_waiting():
    """The absence being asserted is the point.

    Nothing in the tier table describes elapsing, timing out, or defaulting to
    proceed. A Tier-2 transaction has exactly one way forward: a person.
    """
    for action in TIER_ACTIONS.values():
        assert "timeout" not in action
        assert "expire" not in action
    assert TIER_ACTIONS[TIER_HUMAN].startswith("halt")


# ── Tier 0 ─────────────────────────────────────────────────────────────────────


def test_tier_0_for_a_small_undiscounted_cart():
    decision = assign_tier(total_inr=399, discount_pct=Decimal("0.00"))
    assert decision.tier == TIER_AUTO
    assert decision.name == "auto"
    assert decision.action == "proceed"
    assert decision.requires_mandate is False
    assert decision.requires_human is False
    assert _deciding_codes(decision) == {"within_auto_limits"}


def test_tier_0_needs_every_condition_at_once():
    """Five conditions, all required. Each one alone can remove Tier 0."""
    ok = dict(total_inr=1999, discount_pct=Decimal("5.00"))
    assert assign_tier(**ok).tier == TIER_AUTO

    assert assign_tier(**{**ok, "total_inr": TIER_AUTO_MAX_TOTAL_INR}).tier != TIER_AUTO
    assert assign_tier(**{**ok, "discount_pct": Decimal("5.01")}).tier != TIER_AUTO
    assert assign_tier(**ok, tripped_bounds=(1,)).tier != TIER_AUTO
    assert assign_tier(**ok, mandate_issuer_trusted=False).tier != TIER_AUTO
    assert assign_tier(**ok, agent_first_order=True).tier != TIER_AUTO


def test_tier_0_total_is_exclusive_at_its_ceiling():
    """Under Rs 2,000, not up to it — the mandate threshold owns that rupee."""
    assert assign_tier(total_inr=1999, discount_pct=0).tier == TIER_AUTO
    assert assign_tier(total_inr=2000, discount_pct=0).tier == TIER_MANDATE


def test_tier_0_discount_is_inclusive_at_its_ceiling():
    """At exactly 5% is inside Tier 0; a hundredth of a point over is not."""
    assert assign_tier(
        total_inr=1000, discount_pct=Decimal(TIER_AUTO_MAX_DISCOUNT_PCT)
    ).tier == TIER_AUTO
    assert assign_tier(total_inr=1000, discount_pct=Decimal("5.01")).tier == TIER_MANDATE


def test_a_trusted_issuer_does_not_prevent_tier_0():
    """Presenting a valid mandate on a small cart is allowed, not penalised."""
    decision = assign_tier(total_inr=500, discount_pct=0, mandate_issuer_trusted=True)
    assert decision.tier == TIER_AUTO


def test_no_mandate_presented_is_not_a_trigger():
    """None means "none presented", which a small cart is entitled to do."""
    decision = assign_tier(total_inr=500, discount_pct=0, mandate_issuer_trusted=None)
    assert decision.tier == TIER_AUTO
    assert "unknown_mandate_issuer" not in _codes(decision)


# ── Tier 1 ─────────────────────────────────────────────────────────────────────


def test_tier_1_across_the_mandate_band():
    for total in (MANDATE_REQUIRED_ABOVE_INR, 4599, MAX_TXN_WITHOUT_HUMAN_INR):
        decision = assign_tier(total_inr=total, discount_pct=Decimal("0.00"))
        assert decision.tier == TIER_MANDATE, total
        assert decision.requires_mandate is True
        assert decision.requires_human is False
        assert "amount_requires_mandate" in _deciding_codes(decision)


def test_tier_1_band_is_inclusive_at_both_ends():
    """Rs 6,000 is the last total a mandate can authorise on its own."""
    assert assign_tier(total_inr=MAX_TXN_WITHOUT_HUMAN_INR, discount_pct=0).tier == TIER_MANDATE
    assert assign_tier(total_inr=MAX_TXN_WITHOUT_HUMAN_INR + 1, discount_pct=0).tier == TIER_HUMAN


def test_the_gap_between_the_stated_rules_resolves_upward():
    """A Rs 1,500 cart at 7% matches no stated rule. It gets Tier 1, not Tier 0.

    Tier 0's ceilings and Tier 1's band do not tile the space: this cart is under
    Rs 2,000 so Tier 1's amount trigger misses it, and over 5% so Tier 0 excludes
    it. Resolving toward less authority would let an unreasoned-about combination
    through unchecked, which is precisely the case that deserves a check.
    """
    decision = assign_tier(total_inr=1500, discount_pct=Decimal("7.00"))
    assert decision.tier == TIER_MANDATE
    assert _deciding_codes(decision) == {"outside_auto_limits"}


def test_the_gap_never_claims_an_amount_trigger_it_does_not_have():
    """The explanation has to be honest about which rule caught the cart."""
    decision = assign_tier(total_inr=1500, discount_pct=Decimal("7.00"))
    assert "amount_requires_mandate" not in _codes(decision)
    assert "does not qualify for unchecked approval" in decision.summary


# ── Tier 2, one test per trigger ───────────────────────────────────────────────


def test_tier_2_on_amount():
    decision = assign_tier(total_inr=MAX_TXN_WITHOUT_HUMAN_INR + 1, discount_pct=0)
    assert decision.tier == TIER_HUMAN
    assert "amount_above_autonomous_limit" in _deciding_codes(decision)


def test_tier_2_on_discount_even_when_the_total_is_tiny():
    """A deep discount on a cheap cart is still generosity with someone's margin."""
    decision = assign_tier(total_inr=200, discount_pct=Decimal("8.01"))
    assert decision.tier == TIER_HUMAN
    assert "discount_above_human_threshold" in _deciding_codes(decision)


def test_tier_2_discount_threshold_is_exclusive():
    """Exactly 8% is not above 8%."""
    assert assign_tier(
        total_inr=200, discount_pct=Decimal(TIER_HUMAN_DISCOUNT_PCT)
    ).tier != TIER_HUMAN
    assert assign_tier(total_inr=200, discount_pct=Decimal("8.01")).tier == TIER_HUMAN


def test_tier_2_on_any_tripped_bound():
    decision = assign_tier(total_inr=200, discount_pct=0, tripped_bounds=(4,))
    assert decision.tier == TIER_HUMAN
    assert "bounds_tripped" in _deciding_codes(decision)
    assert "bound(s) 4" in decision.summary


def test_tier_2_on_an_unknown_mandate_issuer():
    """An issuer outside the registry is a stranger claiming authority."""
    decision = assign_tier(total_inr=200, discount_pct=0, mandate_issuer_trusted=False)
    assert decision.tier == TIER_HUMAN
    assert "unknown_mandate_issuer" in _deciding_codes(decision)


def test_tier_2_on_a_first_order_from_an_agent():
    """The first transaction from an agent id is seen by a person, whatever it is."""
    decision = assign_tier(total_inr=1, discount_pct=0, agent_first_order=True)
    assert decision.tier == TIER_HUMAN
    assert "first_order_from_agent" in _deciding_codes(decision)


def test_all_five_tier_2_triggers_exist_and_are_distinct():
    """Five independent reasons a human is required, each separately reportable."""
    found = set()
    for kwargs in (
        dict(total_inr=MAX_TXN_WITHOUT_HUMAN_INR + 1, discount_pct=0),
        dict(total_inr=100, discount_pct=Decimal("50")),
        dict(total_inr=100, discount_pct=0, tripped_bounds=(1,)),
        dict(total_inr=100, discount_pct=0, mandate_issuer_trusted=False),
        dict(total_inr=100, discount_pct=0, agent_first_order=True),
    ):
        decision = assign_tier(**kwargs)
        assert decision.tier == TIER_HUMAN
        found |= _deciding_codes(decision)
    assert found == {
        "amount_above_autonomous_limit",
        "discount_above_human_threshold",
        "bounds_tripped",
        "unknown_mandate_issuer",
        "first_order_from_agent",
    }


# ── composition ────────────────────────────────────────────────────────────────


def test_the_highest_matching_trigger_wins():
    """A cart in the mandate band that also trips a bound is Tier 2, not Tier 1."""
    decision = assign_tier(
        total_inr=4599, discount_pct=Decimal("0.00"), tripped_bounds=(4,)
    )
    assert decision.tier == TIER_HUMAN
    # The Tier-1 trigger still matched and is still reported.
    assert "amount_requires_mandate" in _codes(decision)
    # But it is not one of the reasons the transaction was held.
    assert "amount_requires_mandate" not in _deciding_codes(decision)


def test_every_matching_trigger_is_reported_not_just_the_winner():
    """"Over the limit" and "over the limit and the issuer was unknown" are
    different explanations to a merchant deciding whether to approve."""
    decision = assign_tier(
        total_inr=20000,
        discount_pct=Decimal("30.00"),
        tripped_bounds=(1, 6),
        mandate_issuer_trusted=False,
        agent_first_order=True,
    )
    assert decision.tier == TIER_HUMAN
    assert len(decision.deciding_triggers) == 5


def test_triggers_are_ordered_highest_tier_first():
    """The deciding reasons read first in a receipt, before the also-matched ones."""
    decision = assign_tier(
        total_inr=4599, discount_pct=Decimal("0.00"), tripped_bounds=(4,)
    )
    tiers = [t.tier for t in decision.triggers]
    assert tiers == sorted(tiers, reverse=True)
    assert tiers[0] == TIER_HUMAN


def test_deciding_triggers_only_returns_the_winning_tier():
    decision = assign_tier(total_inr=4599, discount_pct=0, tripped_bounds=(4,))
    assert all(t.tier == TIER_HUMAN for t in decision.deciding_triggers)
    assert len(decision.triggers) > len(decision.deciding_triggers)


def test_tripped_bounds_are_deduplicated_and_sorted_in_the_explanation():
    decision = assign_tier(
        total_inr=100, discount_pct=0, tripped_bounds=[6, 1, 6, 4, 1]
    )
    trigger = next(t for t in decision.triggers if t.code == "bounds_tripped")
    assert "bound(s) 1, 4, 6 tripped" in trigger.detail


def test_a_bare_tier_decision_with_no_triggers_still_summarises():
    """The empty case has to render, because a receipt always states a reason."""
    decision = GateDecision(tier=TIER_AUTO)
    assert decision.summary == "tier 0 (auto): no elevating conditions"
    assert decision.deciding_triggers == ()


# ── inputs ─────────────────────────────────────────────────────────────────────


def test_discount_pct_accepts_an_int_as_well_as_a_decimal():
    """Callers holding a whole-number percentage should not have to wrap it."""
    assert assign_tier(total_inr=100, discount_pct=9).tier == TIER_HUMAN
    assert assign_tier(total_inr=100, discount_pct=Decimal("9")).tier == TIER_HUMAN


def test_tripped_bounds_accepts_a_list_or_a_tuple():
    assert assign_tier(total_inr=100, discount_pct=0, tripped_bounds=[2]).tier == TIER_HUMAN
    assert assign_tier(total_inr=100, discount_pct=0, tripped_bounds=(2,)).tier == TIER_HUMAN


def test_an_empty_tripped_bounds_is_not_a_trigger():
    decision = assign_tier(total_inr=100, discount_pct=0, tripped_bounds=())
    assert "bounds_tripped" not in _codes(decision)


def test_assign_tier_is_keyword_only():
    """Positional money arguments are how a total and a percentage get swapped."""
    with pytest.raises(TypeError):
        assign_tier(4599, Decimal("0.00"))  # type: ignore[misc]


def test_the_decision_is_frozen():
    """A tier assignment is a verdict, not a mutable working value."""
    decision = assign_tier(total_inr=100, discount_pct=0)
    with pytest.raises(Exception):
        decision.tier = TIER_AUTO  # type: ignore[misc]


# ── the payload the receipt signs ──────────────────────────────────────────────


def test_payload_carries_the_tier_the_action_and_every_trigger():
    decision = assign_tier(
        total_inr=14997, discount_pct=Decimal("0.00"), tripped_bounds=(6,)
    )
    payload = decision.as_payload()
    assert payload["gate_tier"] == TIER_HUMAN
    assert payload["tier_name"] == "human"
    assert payload["action"] == "halt_for_merchant_approval"
    assert payload["requires_mandate"] is True
    assert payload["requires_human"] is True
    assert len(payload["triggers"]) == 2
    assert payload["summary"].startswith("tier 2 (human):")


def test_payload_is_json_safe():
    """The gate payload goes into a signed receipt, so it must canonicalise."""
    from store.canonical import canonical_json

    decision = assign_tier(total_inr=4599, discount_pct=Decimal("8.00"))
    canonical_json(decision.as_payload())  # raises if anything is unserialisable


def test_trigger_payload_is_flat_and_named():
    trigger = GateTrigger(TIER_HUMAN, "code_here", "human-readable reason")
    assert trigger.as_payload() == {
        "tier": 2,
        "code": "code_here",
        "detail": "human-readable reason",
    }


# ── the headline case ──────────────────────────────────────────────────────────


def test_the_14997_case_routes_to_tier_2():
    """The deliverable, at the gate layer: 3 x Rs 4,999 needs a person.

    Two independent triggers agree — the raw amount rule and bound 6 — which is
    the point of passing `tripped_bounds` in rather than re-deriving the threshold
    here. Two thresholds that could drift apart would eventually disagree.
    """
    decision = assign_tier(
        total_inr=14997, discount_pct=Decimal("0.00"), tripped_bounds=(6,)
    )
    assert decision.tier == TIER_HUMAN
    assert decision.requires_human is True
    assert decision.action == "halt_for_merchant_approval"
    assert _deciding_codes(decision) == {
        "amount_above_autonomous_limit",
        "bounds_tripped",
    }
