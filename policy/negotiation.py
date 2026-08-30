from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from policy.snapshot import EffectivePolicy

MAX_NEGOTIATION_ROUNDS = 3

class NegotiationOutcome:
    ACCEPTED = "ACCEPTED"
    COUNTER = "COUNTER"
    NO_FEASIBLE_DEAL = "NO_FEASIBLE_DEAL"
    ESCALATE = "ESCALATE"

@dataclass(frozen=True)
class CounterOffer:
    counter_price_inr: int
    counter_items: tuple[CounterItem, ...]
    savings_vs_list_inr: int
    reason: str

@dataclass(frozen=True)
class CounterItem:
    sku: str
    quantity: int
    unit_price_inr: int

@dataclass(frozen=True)
class NegotiationResponse:
    outcome: str
    accepted_price_inr: int | None = None
    counter_offers: tuple[CounterOffer, ...] = ()
    explanation: str = ""
    round_number: int = 1
    feasible_range: tuple[int, int] | None = None

@dataclass(frozen=True)
class NegotiationState:
    session_id: str
    sku: str
    quantity: int
    rounds: tuple[NegotiationRound, ...] = ()
    
    @property
    def round_count(self) -> int:
        return len(self.rounds)

@dataclass(frozen=True)
class NegotiationRound:
    round_number: int
    buyer_proposed_inr: int
    response: NegotiationResponse

def _compute_merchant_floor(variable_cost_inr: int, min_margin_pct: Decimal, quantity: int) -> int:
    """Compute the minimum acceptable price for the total quantity.

    Guarded against margin >=100% which would invert or divide by zero;
    callers should treat that as misconfiguration rather than a price.
    """
    if min_margin_pct >= 100:
        raise ValueError("Margin cannot be 100% or more.")
    margin_frac = min_margin_pct / Decimal("100")
    # margin_factor <=0 is already filtered in pre_filter; this path mirrors
    # that guard so negotiation never divides by zero even if called directly.
    if margin_frac >= Decimal("1.0"):
        raise ValueError("Margin cannot be 100% or more.")
    unit_floor = Decimal(variable_cost_inr) / (Decimal("1.0") - margin_frac)
    return math.ceil(unit_floor) * quantity

def _compute_feasible_range(floor: int, ceiling: int) -> tuple[int, int]:
    return (floor, min(ceiling, ceiling))

def _generate_counter_offers(
    sku: str,
    original_qty: int,
    buyer_budget_inr: int | None,
    unit_floor_inr: int,
    list_price_inr: int,
    stock_available: int
) -> tuple[CounterOffer, ...]:
    offers = []
    
    # 1. Counter at the exact same quantity, best price (floor)
    total_floor = unit_floor_inr * original_qty
    list_total = list_price_inr * original_qty
    savings = list_total - total_floor
    if original_qty <= stock_available:
        offers.append(
            CounterOffer(
                counter_price_inr=total_floor,
                counter_items=(CounterItem(sku=sku, quantity=original_qty, unit_price_inr=unit_floor_inr),),
                savings_vs_list_inr=max(0, savings),
                reason=f"We can offer {original_qty} units for {total_floor} INR."
            )
        )
    
    # 2. Fit within budget by reducing quantity
    if buyer_budget_inr is not None and buyer_budget_inr >= unit_floor_inr:
        affordable_qty = buyer_budget_inr // unit_floor_inr
        affordable_qty = min(affordable_qty, stock_available, original_qty - 1)
        if affordable_qty > 0:
            affordable_total = unit_floor_inr * affordable_qty
            affordable_list_total = list_price_inr * affordable_qty
            affordable_savings = affordable_list_total - affordable_total
            offers.append(
                CounterOffer(
                    counter_price_inr=affordable_total,
                    counter_items=(CounterItem(sku=sku, quantity=affordable_qty, unit_price_inr=unit_floor_inr),),
                    savings_vs_list_inr=max(0, affordable_savings),
                    reason=f"To stay within your budget, we can offer {affordable_qty} units for {affordable_total} INR."
                )
            )
            
    return tuple(offers)

def _format_explanation(outcome: str, floor: int, buyer_budget: int | None) -> str:
    if outcome == NegotiationOutcome.ACCEPTED:
        return "Proposal accepted."
    elif outcome == NegotiationOutcome.ESCALATE:
        return "Negotiation round limit reached. Escalating to human review."
    elif outcome == NegotiationOutcome.NO_FEASIBLE_DEAL:
        return f"Cannot meet the proposed budget. The minimum feasible price is {floor} INR."
    elif outcome == NegotiationOutcome.COUNTER:
        return "Proposed price is below our minimum threshold. Here are alternative options."
    return ""

def evaluate_buyer_proposal(
    *,
    proposed_price_inr: int,
    sku: str,
    quantity: int,
    effective_policy: EffectivePolicy,
    variable_cost_inr: int,
    list_price_inr: int,
    buyer_budget_inr: int | None = None,
    current_round: int = 1,
    stock_available: int = 0,
) -> NegotiationResponse:
    if quantity <= 0:
        return NegotiationResponse(
            outcome=NegotiationOutcome.NO_FEASIBLE_DEAL,
            explanation="Quantity must be greater than zero.",
            round_number=current_round
        )

    if proposed_price_inr <= 0:
        return NegotiationResponse(
            outcome=NegotiationOutcome.NO_FEASIBLE_DEAL,
            explanation="Proposed price must be greater than zero.",
            round_number=current_round
        )
        
    if quantity > stock_available:
        return NegotiationResponse(
            outcome=NegotiationOutcome.NO_FEASIBLE_DEAL,
            explanation="Insufficient stock.",
            round_number=current_round
        )

    min_margin = effective_policy.hard_constraints.min_margin_pct
    max_discount = effective_policy.hard_constraints.max_discount_pct_per_sku
    floor_total = _compute_merchant_floor(variable_cost_inr, min_margin, quantity)
    list_total = list_price_inr * quantity
    unit_floor = floor_total // quantity

    if proposed_price_inr >= floor_total:
        return NegotiationResponse(
            outcome=NegotiationOutcome.ACCEPTED,
            accepted_price_inr=min(proposed_price_inr, list_total),
            explanation="Proposal meets merchant constraints.",
            round_number=current_round
        )
    
    # 5. Check if overlap possible
    if buyer_budget_inr is not None and floor_total > buyer_budget_inr:
        if current_round >= MAX_NEGOTIATION_ROUNDS:
            return NegotiationResponse(
                outcome=NegotiationOutcome.ESCALATE,
                explanation=_format_explanation(NegotiationOutcome.ESCALATE, floor_total, buyer_budget_inr),
                round_number=current_round
            )
        # Try to find lower quantity counter offer
        counter_offers = _generate_counter_offers(sku, quantity, buyer_budget_inr, unit_floor, list_price_inr, stock_available)
        if len(counter_offers) > 1: # means affordable qty is available
            return NegotiationResponse(
                outcome=NegotiationOutcome.COUNTER,
                counter_offers=counter_offers,
                explanation=_format_explanation(NegotiationOutcome.COUNTER, floor_total, buyer_budget_inr),
                round_number=current_round,
                feasible_range=(floor_total, buyer_budget_inr if buyer_budget_inr else list_total)
            )
        else:
            return NegotiationResponse(
                outcome=NegotiationOutcome.NO_FEASIBLE_DEAL,
                explanation=_format_explanation(NegotiationOutcome.NO_FEASIBLE_DEAL, floor_total, buyer_budget_inr),
                round_number=current_round
            )

    # 4. Check round limit
    if current_round >= MAX_NEGOTIATION_ROUNDS:
        if proposed_price_inr >= floor_total:
            # Although round limit reached, if price is good we can accept
            accepted_price = min(proposed_price_inr, list_total)
            return NegotiationResponse(
                outcome=NegotiationOutcome.ACCEPTED,
                accepted_price_inr=accepted_price,
                explanation=_format_explanation(NegotiationOutcome.ACCEPTED, floor_total, buyer_budget_inr),
                round_number=current_round,
                feasible_range=(floor_total, list_total)
            )
        return NegotiationResponse(
            outcome=NegotiationOutcome.ESCALATE,
            explanation=_format_explanation(NegotiationOutcome.ESCALATE, floor_total, buyer_budget_inr),
            round_number=current_round
        )

    # 2. Check if accepted
    if proposed_price_inr >= floor_total:
        accepted_price = min(proposed_price_inr, list_total)
        return NegotiationResponse(
            outcome=NegotiationOutcome.ACCEPTED,
            accepted_price_inr=accepted_price,
            explanation=_format_explanation(NegotiationOutcome.ACCEPTED, floor_total, buyer_budget_inr),
            round_number=current_round,
            feasible_range=(floor_total, list_total)
        )

    # 3. Counter offer
    counter_offers = _generate_counter_offers(sku, quantity, buyer_budget_inr, unit_floor, list_price_inr, stock_available)
    return NegotiationResponse(
        outcome=NegotiationOutcome.COUNTER,
        counter_offers=counter_offers,
        explanation=_format_explanation(NegotiationOutcome.COUNTER, floor_total, buyer_budget_inr),
        round_number=current_round,
        feasible_range=(floor_total, list_total)
    )
