"""Merchant Economic Constitution (MEC) data models.

This module defines the merchant's machine-readable business rulebook, establishing
hard constraints, economic objectives, and negotiation permissions.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from store.canonical import canonical_json


@dataclass(frozen=True)
class ApprovalThresholds:
    """Thresholds governing autonomous vs. manual approvals."""
    auto_max_inr: int = 2000
    mandate_max_inr: int = 6000


@dataclass(frozen=True)
class HardConstraints:
    """Unbreakable constraints applied to the transaction."""
    min_margin_pct: Decimal
    max_discount_pct_per_sku: int
    max_cart_discount_pct: int
    max_txn_without_human_inr: int
    min_stock_qty: int = 1
    offer_ttl_seconds: int = 300
    daily_discount_budget_inr: int = 10000
    max_offers_per_session: int = 2
    approval_thresholds: ApprovalThresholds = ApprovalThresholds()


@dataclass(frozen=True)
class EconomicObjectives:
    """Weights for the economic optimization function. Must sum to 1.0."""
    margin_weight: Decimal
    conversion_weight: Decimal
    aov_weight: Decimal
    inventory_velocity_weight: Decimal


def validate_objectives(obj: EconomicObjectives) -> None:
    """Validates that the economic objective weights sum exactly to 1.0.

    Args:
        obj: The EconomicObjectives instance to validate.

    Raises:
        ValueError: If the sum of the weights is not 1.0.
    """
    total = (
        obj.margin_weight
        + obj.conversion_weight
        + obj.aov_weight
        + obj.inventory_velocity_weight
    )
    if total != Decimal("1.0"):
        raise ValueError(f"Objective weights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class NegotiationPermissions:
    """Flags detailing what the autonomous agent is allowed to negotiate."""
    price: bool = True
    quantity: bool = True
    bundles: bool = True
    substitutes: bool = True
    shipping: bool = False
    delivery_date: bool = False


class MECScope(Enum):
    """The scope at which a Merchant Economic Constitution applies."""
    STORE = "STORE"
    CATEGORY = "CATEGORY"
    SKU = "SKU"
    CAMPAIGN = "CAMPAIGN"


@dataclass(frozen=True)
class MEC:
    """The Merchant Economic Constitution."""
    mec_id: str
    version: int
    store_id: str
    scope: MECScope
    scope_value: Optional[str]
    hard_constraints: HardConstraints
    objectives: EconomicObjectives
    negotiation: NegotiationPermissions
    created_at: str
    hash: str


def compute_hash(mec: MEC) -> str:
    """Computes a stable SHA-256 hash of the MEC's core content.

    Excludes the hash field itself to avoid circular dependency.

    Args:
        mec: The MEC instance to hash.

    Returns:
        A hex-encoded SHA-256 digest string.
    """
    payload = {
        "hard_constraints": asdict(mec.hard_constraints),
        "objectives": {
            k: str(v) for k, v in asdict(mec.objectives).items()
        },
        "negotiation": asdict(mec.negotiation),
    }
    
    # Convert Decimals in hard constraints to string
    payload["hard_constraints"]["min_margin_pct"] = str(
        payload["hard_constraints"]["min_margin_pct"]
    )
    
    encoded = canonical_json(payload)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
