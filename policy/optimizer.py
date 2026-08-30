from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence

from policy.pre_filter import CandidateDeal
from policy.snapshot import EffectivePolicy


@dataclass(frozen=True)
class ProductContext:
    """Dynamic context for each product."""
    sku: str
    stock_remaining: int
    inventory_age_days: int
    demand_velocity: Decimal  # units sold per day, trailing 7 days
    conversion_rate_pct: Decimal  # trailing conversion %
    return_rate_pct: Decimal
    variable_cost_inr: int


@dataclass(frozen=True)
class ScoreBreakdown:
    margin_score: Decimal
    conversion_score: Decimal
    aov_score: Decimal
    inventory_score: Decimal
    total_score: Decimal
    explanation: str  # human-readable, e.g. '₹3,100 selected because...'


@dataclass(frozen=True)
class RankedDeal:
    candidate: CandidateDeal
    score: Decimal
    breakdown: ScoreBreakdown
    rank: int


def _margin_score(candidate: CandidateDeal, product_context: dict[str, ProductContext]) -> Decimal:
    if candidate.total_inr <= 0:
        return Decimal(0)
    
    total_cost = sum(
        item.quantity * product_context[item.sku].variable_cost_inr
        for item in candidate.items
        if item.sku in product_context
    )
    revenue = candidate.total_inr
    if revenue <= 0: 
        return Decimal(0)
    
    score = Decimal(revenue - total_cost) / Decimal(revenue)
    return max(Decimal(0), min(Decimal(1), score))

def _conversion_score(candidate: CandidateDeal, buyer_budget: int | None) -> Decimal:
    if not buyer_budget or buyer_budget <= 0:
        return Decimal("0.5")
    
    x = (Decimal(buyer_budget) - Decimal(candidate.total_inr)) / Decimal(buyer_budget)
    try:
        k = Decimal(10)
        exp_val = Decimal(math.exp(float(-k * x)))
        score = Decimal(1) / (Decimal(1) + exp_val)
    except:
        score = Decimal(0)
        
    return max(Decimal(0), min(Decimal(1), score))

def _aov_score(candidate: CandidateDeal, max_total: int) -> Decimal:
    if max_total <= 0:
        return Decimal(0)
    score = Decimal(candidate.total_inr) / Decimal(max_total)
    return max(Decimal(0), min(Decimal(1), score))

def _inventory_score(candidate: CandidateDeal, product_context: dict[str, ProductContext]) -> Decimal:
    if not candidate.items:
        return Decimal(0)
        
    scores = []
    for item in candidate.items:
        ctx = product_context.get(item.sku)
        if not ctx:
            scores.append(Decimal(0))
            continue
            
        age_factor = min(Decimal(1), Decimal(ctx.inventory_age_days) / Decimal(120))
        demand_factor = max(Decimal(0), Decimal(1) - (ctx.demand_velocity / Decimal(10)))
        
        item_score = age_factor * demand_factor
        scores.append(item_score)
        
    avg_score = sum(scores) / Decimal(len(scores))
    return max(Decimal(0), min(Decimal(1), avg_score))

def _generate_explanation(candidate: CandidateDeal, breakdown: ScoreBreakdown | None, product_context: dict[str, ProductContext]) -> str:
    if breakdown is None:
        return f"₹{candidate.total_inr} selected because it provides optimal value."
    # Include numeric breakdown so the explanation is auditable, not generic.
    return (
        f"₹{candidate.total_inr} selected "
        f"(margin {breakdown.margin_score:.2f}, "
        f"conversion {breakdown.conversion_score:.2f}, "
        f"aov {breakdown.aov_score:.2f}, "
        f"inventory {breakdown.inventory_score:.2f}, "
        f"total {breakdown.total_score:.3f})."
    )


def optimize(
    candidates: Sequence[CandidateDeal],
    effective_policy: EffectivePolicy,
    product_context: dict[str, ProductContext],
    buyer_budget_inr: int | None = None,
) -> list[RankedDeal]:
    """
    Score and rank valid candidates using the merchant's objective weights.
    
    Score(d) = w_m * M(d) + w_c * C(d) + w_a * A(d) + w_i * I(d)
    
    M(d) = margin score: (revenue - variable_cost) / revenue, normalized 0-1
    C(d) = conversion score: estimated acceptance probability based on
           proximity to buyer budget (sigmoid-like function)
    A(d) = AOV score: total_inr normalized against the highest candidate total
    I(d) = inventory velocity score: higher for items with high age/low demand
    
    Weights come from effective_policy.objectives.
    Returns list sorted by score descending (rank 1 = best).
    """
    if not candidates:
        return []
        
    max_total = max((c.total_inr for c in candidates), default=0)
    
    w_m = Decimal(effective_policy.economic_objectives.margin_weight)
    w_c = Decimal(effective_policy.economic_objectives.conversion_weight)
    w_a = Decimal(effective_policy.economic_objectives.aov_weight)
    w_i = Decimal(effective_policy.economic_objectives.inventory_velocity_weight)
    
    scored_candidates = []
    
    for cand in candidates:
        m_score = _margin_score(cand, product_context)
        c_score = _conversion_score(cand, buyer_budget_inr)
        a_score = _aov_score(cand, max_total)
        i_score = _inventory_score(cand, product_context)
        
        total_score = (w_m * m_score) + (w_c * c_score) + (w_a * a_score) + (w_i * i_score)
        
        # Build breakdown first so explanation can include numeric values.
        provisional = ScoreBreakdown(
            margin_score=m_score,
            conversion_score=c_score,
            aov_score=a_score,
            inventory_score=i_score,
            total_score=total_score,
            explanation="",
        )
        explanation = _generate_explanation(cand, provisional, product_context)
        
        breakdown = ScoreBreakdown(
            margin_score=m_score,
            conversion_score=c_score,
            aov_score=a_score,
            inventory_score=i_score,
            total_score=total_score,
            explanation=explanation
        )
        
        scored_candidates.append((total_score, cand, breakdown))
        
    scored_candidates.sort(key=lambda x: (x[0], x[1].candidate_id), reverse=True)
    
    ranked_deals = []
    for rank, (score, cand, breakdown) in enumerate(scored_candidates, 1):
        ranked_deals.append(RankedDeal(
            candidate=cand,
            score=score,
            breakdown=breakdown,
            rank=rank
        ))
        
    return ranked_deals
