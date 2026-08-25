"""The three gate tiers — how much authority the transaction needs.

The bounds decide whether an offer is *allowed*. The gate decides **who has to
say yes**: nobody, the buyer's mandate, or a human merchant.

    Tier 0  Auto      proceed
    Tier 1  Mandate   verify signature, scope, amount, expiry, then proceed
    Tier 2  Human     halt and wait for the merchant

Tier is assigned by the highest trigger that matches, and any Tier-2 condition
wins outright. The function returns every trigger it found rather than just the
winner, because the policy receipt has to be able to state *why* a transaction
was held, and "it was over the limit" and "it was over the limit and the issuer
was unknown" are different explanations to a merchant.

A gap in the stated rules, resolved deliberately: Tier 0 requires a total under
Rs 2,000 *and* a discount at or under 5%, while Tier 1's stated trigger is a
total between Rs 2,000 and Rs 6,000. A Rs 1,500 cart discounted 7% therefore
matches neither. It is resolved toward more authority, not less — Tier 0 is a
narrow allowlist for transactions that need no check at all, so anything that
fails to qualify for it starts at Tier 1. Defaulting the other way would let an
unlisted combination through unchecked, and an unlisted combination is exactly
the case nobody reasoned about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from settings import MANDATE_REQUIRED_ABOVE_INR, MAX_TXN_WITHOUT_HUMAN_INR

#: The tiers.
TIER_AUTO = 0
TIER_MANDATE = 1
TIER_HUMAN = 2

TIER_NAMES: dict[int, str] = {
    TIER_AUTO: "auto",
    TIER_MANDATE: "mandate",
    TIER_HUMAN: "human",
}

TIER_ACTIONS: dict[int, str] = {
    TIER_AUTO: "proceed",
    TIER_MANDATE: "verify_mandate_then_proceed",
    TIER_HUMAN: "halt_for_merchant_approval",
}

#: Tier 0's own ceilings. Distinct constants from the bounds: bound 6 is the
#: point above which a human is required, whereas these are the point above
#: which *some* check is required.
TIER_AUTO_MAX_TOTAL_INR = MANDATE_REQUIRED_ABOVE_INR
TIER_AUTO_MAX_DISCOUNT_PCT = 5

#: Above this discount a human decides, regardless of amount. A deep discount on
#: a cheap cart is still the kernel being generous with someone else's margin.
TIER_HUMAN_DISCOUNT_PCT = 8


@dataclass(frozen=True)
class GateTrigger:
    """One reason a tier was considered. Carried into the policy receipt."""

    tier: int
    code: str
    detail: str

    def as_payload(self) -> dict[str, Any]:
        return {"tier": self.tier, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class GateDecision:
    tier: int
    triggers: tuple[GateTrigger, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return TIER_NAMES[self.tier]

    @property
    def action(self) -> str:
        return TIER_ACTIONS[self.tier]

    @property
    def requires_mandate(self) -> bool:
        return self.tier >= TIER_MANDATE

    @property
    def requires_human(self) -> bool:
        return self.tier == TIER_HUMAN

    @property
    def deciding_triggers(self) -> tuple[GateTrigger, ...]:
        """Only the triggers at the winning tier — the actual reasons."""
        return tuple(t for t in self.triggers if t.tier == self.tier)

    @property
    def summary(self) -> str:
        reasons = [t.detail for t in self.deciding_triggers]
        joined = "; ".join(reasons) if reasons else "no elevating conditions"
        return f"tier {self.tier} ({self.name}): {joined}"

    def as_payload(self) -> dict[str, Any]:
        return {
            "gate_tier": self.tier,
            "tier_name": self.name,
            "action": self.action,
            "requires_mandate": self.requires_mandate,
            "requires_human": self.requires_human,
            "triggers": [t.as_payload() for t in self.triggers],
            "summary": self.summary,
        }


def assign_tier(
    *,
    total_inr: int,
    discount_pct: Decimal | int,
    tripped_bounds: tuple[int, ...] | list[int] = (),
    mandate_issuer_trusted: bool | None = None,
    agent_first_order: bool = False,
) -> GateDecision:
    """Assign the tier this transaction has to clear.

    `mandate_issuer_trusted` is three-valued: True for a mandate from a known
    issuer, False for one from an issuer outside the trusted registry, and None
    when no mandate was presented. False is a Tier-2 trigger — an unknown issuer
    is a stranger claiming authority. None is not, because a small cart is
    allowed to arrive without a mandate at all; whether one was *required* is
    then answered by `requires_mandate` on the result.

    `tripped_bounds` includes bound 6, whose trip means "needs a human". Passing
    it in is what makes the amount rule and the bound agree instead of being two
    thresholds that can drift apart.
    """
    total = int(total_inr)
    pct = Decimal(discount_pct)
    tripped = tuple(sorted(set(int(b) for b in tripped_bounds)))

    triggers: list[GateTrigger] = []

    # -- Tier 2 conditions. Any one of these wins outright. -----------------
    if total > MAX_TXN_WITHOUT_HUMAN_INR:
        triggers.append(
            GateTrigger(
                TIER_HUMAN,
                "amount_above_autonomous_limit",
                f"Rs {total} exceeds the Rs {MAX_TXN_WITHOUT_HUMAN_INR} "
                "autonomous transaction limit",
            )
        )
    if pct > TIER_HUMAN_DISCOUNT_PCT:
        triggers.append(
            GateTrigger(
                TIER_HUMAN,
                "discount_above_human_threshold",
                f"discount {pct}% exceeds the {TIER_HUMAN_DISCOUNT_PCT}% "
                "threshold for autonomous approval",
            )
        )
    if tripped:
        listed = ", ".join(str(b) for b in tripped)
        triggers.append(
            GateTrigger(
                TIER_HUMAN,
                "bounds_tripped",
                f"bound(s) {listed} tripped during evaluation",
            )
        )
    if mandate_issuer_trusted is False:
        triggers.append(
            GateTrigger(
                TIER_HUMAN,
                "unknown_mandate_issuer",
                "mandate issuer is not in the trusted-issuer registry",
            )
        )
    if agent_first_order:
        triggers.append(
            GateTrigger(
                TIER_HUMAN,
                "first_order_from_agent",
                "first order from this agent id",
            )
        )

    # -- Tier 1 --------------------------------------------------------------
    if MANDATE_REQUIRED_ABOVE_INR <= total <= MAX_TXN_WITHOUT_HUMAN_INR:
        triggers.append(
            GateTrigger(
                TIER_MANDATE,
                "amount_requires_mandate",
                f"Rs {total} is at or above the Rs {MANDATE_REQUIRED_ABOVE_INR} "
                "mandate threshold",
            )
        )

    # -- Tier 0: a narrow allowlist, not a fallback. -------------------------
    qualifies_for_auto = (
        total < TIER_AUTO_MAX_TOTAL_INR
        and pct <= TIER_AUTO_MAX_DISCOUNT_PCT
        and not tripped
        and mandate_issuer_trusted is not False
        and not agent_first_order
    )
    if qualifies_for_auto:
        triggers.append(
            GateTrigger(
                TIER_AUTO,
                "within_auto_limits",
                f"Rs {total} under Rs {TIER_AUTO_MAX_TOTAL_INR} at {pct}% "
                f"discount, all bounds passed",
            )
        )

    if any(t.tier == TIER_HUMAN for t in triggers):
        tier = TIER_HUMAN
    elif qualifies_for_auto:
        tier = TIER_AUTO
    else:
        # Everything else needs at least a mandate. This covers the stated
        # Tier-1 band and the gap described in the module docstring.
        tier = TIER_MANDATE
        if not any(t.tier == TIER_MANDATE for t in triggers):
            triggers.append(
                GateTrigger(
                    TIER_MANDATE,
                    "outside_auto_limits",
                    f"Rs {total} at {pct}% discount does not qualify for "
                    "unchecked approval",
                )
            )

    # Highest tier first, so the deciding reasons read first in the receipt.
    ordered = tuple(sorted(triggers, key=lambda t: -t.tier))
    return GateDecision(tier=tier, triggers=ordered)
