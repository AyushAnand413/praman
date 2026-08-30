"""Frozen policy snapshots embedded in Transaction Decision Records.

These ensure that each transaction carries a complete, immutable copy
of the policy that governed it at the exact moment of execution.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Optional

from store.canonical import canonical_json

from .mec import EconomicObjectives, HardConstraints, NegotiationPermissions


@dataclass(frozen=True)
class PolicySource:
    """Identifiers tracing the origin of the effective policy rules."""
    store: Optional[str] = None
    category: Optional[str] = None
    sku: Optional[str] = None
    campaign: Optional[str] = None


@dataclass(frozen=True)
class EffectivePolicy:
    """The finalized, fully resolved policy for a specific transaction."""
    policy_id: str
    store_id: str
    hard_constraints: HardConstraints
    economic_objectives: EconomicObjectives
    negotiation: NegotiationPermissions
    source_chain: list[str]
    hash: str
    created_at: str

    def as_payload(self) -> dict[str, Any]:
        """Serializes the EffectivePolicy for storage.

        Returns:
            A dictionary representation of the effective policy.
        """
        payload = asdict(self)
        payload["economic_objectives"] = {
            k: str(v) for k, v in payload["economic_objectives"].items()
        }
        payload["hard_constraints"]["min_margin_pct"] = str(
            payload["hard_constraints"]["min_margin_pct"]
        )
        return payload


def compute_hash(policy: EffectivePolicy) -> str:
    """Computes a stable SHA-256 hash of the EffectivePolicy's core content.

    Excludes identifying/temporal fields like policy_id, policy_hash, and resolved_at.

    Args:
        policy: The EffectivePolicy to hash.

    Returns:
        A hex-encoded SHA-256 digest string.
    """
    payload = {
        "source_chain": policy.source_chain,
        "hard_constraints": asdict(policy.hard_constraints),
        "economic_objectives": {k: str(v) for k, v in asdict(policy.economic_objectives).items()},
        "negotiation": asdict(policy.negotiation),
    }
    payload["hard_constraints"]["min_margin_pct"] = str(
        payload["hard_constraints"]["min_margin_pct"]
    )
    
    encoded = canonical_json(payload)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
