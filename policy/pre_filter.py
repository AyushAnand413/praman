from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from policy.snapshot import EffectivePolicy


@dataclass(frozen=True)
class CandidateDeal:
    """One possible deal from Vyapaari."""
    candidate_id: str
    items: tuple[CandidateItem, ...]
    total_inr: int
    discount_pct: Decimal
    rationale: str


@dataclass(frozen=True)
class CandidateItem:
    sku: str
    quantity: int
    unit_price_inr: int
    discount_pct: Decimal
    role: str  # 'base' or 'upsell'


@dataclass(frozen=True)
class FilterResult:
    valid: tuple[CandidateDeal, ...]
    filtered: tuple[FilteredCandidate, ...]


@dataclass(frozen=True)
class FilteredCandidate:
    candidate: CandidateDeal
    reason: str
    bound_violated: str | None


def pre_filter(
    candidates: Sequence[CandidateDeal],
    effective_policy: EffectivePolicy,
    product_economics: dict[str, dict[str, Any]],
    stock_levels: dict[str, int],
) -> FilterResult:
    """
    Fast rejection of impossible candidates.
    
    Checks (for each candidate):
    1. All SKUs exist in product_economics
    2. Per-SKU discount <= effective_policy.effective_rules.max_discount_pct_per_sku
    3. Cart discount <= effective_policy.effective_rules.max_cart_discount_pct
    4. Price >= floor (cost / (1 - min_margin_pct/100))
    5. Stock available >= quantity needed
    
    A candidate that fails ANY check is filtered out with a reason.
    Returns FilterResult with valid and filtered lists.
    """
    valid = []
    filtered = []
    
    rules = effective_policy.hard_constraints
    max_sku_discount = Decimal(rules.max_discount_pct_per_sku)
    max_cart_discount = Decimal(rules.max_cart_discount_pct)
    min_margin = Decimal(rules.min_margin_pct)
    
    # margin_factor <=0 means misconfigured 100%+ margin; treat as no floor
    # so the check below does not divide by zero (mirrors negotiation guard).
    margin_factor = Decimal(1) - (min_margin / Decimal(100))

    for cand in candidates:
        reason = None
        bound = None
        
        # 3. Cart discount
        if cand.discount_pct > max_cart_discount:
            reason = f"Cart discount {cand.discount_pct}% exceeds max {max_cart_discount}%"
            bound = "max_cart_discount_pct"
            filtered.append(FilteredCandidate(cand, reason, bound))
            continue
            
        item_failed = False
        for item in cand.items:
            # 1. SKU exists
            if item.sku not in product_economics:
                reason = f"Unknown SKU: {item.sku}"
                bound = "sku_existence"
                item_failed = True
                break
                
            # 2. Per-SKU discount
            if item.discount_pct > max_sku_discount:
                reason = f"Item {item.sku} discount {item.discount_pct}% exceeds max {max_sku_discount}%"
                bound = "max_discount_pct_per_sku"
                item_failed = True
                break
                
            # 4. Price >= floor
            cost = Decimal(product_economics[item.sku]["cost_inr"])
            explicit_floor = product_economics[item.sku].get("floor_price_inr")
            if explicit_floor is not None:
                floor_price = Decimal(explicit_floor)
            elif margin_factor <= Decimal(0):
                floor_price = Decimal(0)
            else:
                floor_price = cost / margin_factor
                
            if Decimal(item.unit_price_inr) < floor_price:
                reason = f"Item {item.sku} price {item.unit_price_inr} below floor"
                bound = "min_margin_pct"
                item_failed = True
                break
                
            # 5. Stock available
            stock = stock_levels.get(item.sku, 0)
            if stock < item.quantity:
                reason = f"Item {item.sku} quantity {item.quantity} exceeds stock {stock}"
                bound = "stock_available"
                item_failed = True
                break
                
        if item_failed:
            filtered.append(FilteredCandidate(cand, reason, bound))
        else:
            valid.append(cand)
            
    return FilterResult(tuple(valid), tuple(filtered))
