"""The policy package — merchant economic governance for PRAMAN 2.0.

This package owns the Merchant Economic Constitution (MEC), the Effective
Policy Resolver, the Transaction Decision Record (TDR), the Economic
Optimizer, and the core safety invariants.

Nothing in this package holds payment credentials or talks to external
services. It is pure business logic and math.
"""

from __future__ import annotations

from .mec import (
    ApprovalThresholds,
    EconomicObjectives,
    HardConstraints,
    MEC,
    MECScope,
    NegotiationPermissions,
)
from .snapshot import EffectivePolicy, PolicySource
from .states import TransactionState, VALID_TRANSITIONS

__all__ = [
    "MEC",
    "MECScope",
    "HardConstraints",
    "EconomicObjectives",
    "NegotiationPermissions",
    "ApprovalThresholds",
    "EffectivePolicy",
    "PolicySource",
    "TransactionState",
    "VALID_TRANSITIONS",
]
