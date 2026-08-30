from __future__ import annotations

import sqlite3
import dataclasses

from policy.mec import (
    MEC, 
    MECScope, 
    HardConstraints, 
    EconomicObjectives, 
    NegotiationPermissions, 
    ApprovalThresholds
)
from policy.snapshot import EffectivePolicy
from store.mec_store import get_latest_mec
from store.canonical import canonical_json, entry_hash
from store.timestamps import utc_now, to_ts
from store.ids import new_id
import settings

def _merge_hard_constraints(parent: HardConstraints, child: HardConstraints) -> HardConstraints:
    """Child values override parent values field by field if they are not None."""
    p_dict = dataclasses.asdict(parent)
    c_dict = dataclasses.asdict(child)
    
    merged = {}
    for k, v in p_dict.items():
        if k in c_dict and c_dict[k] is not None:
            merged[k] = c_dict[k]
        else:
            merged[k] = v
            
    # For nested structures like ApprovalThresholds, handle them separately if needed,
    # but based on prompt "child values override parent values field by field":
    if "approval_thresholds" in merged and isinstance(merged["approval_thresholds"], dict):
        merged["approval_thresholds"] = ApprovalThresholds(**merged["approval_thresholds"])
        
    return HardConstraints(**merged)

def _restrict_negotiation(parent: NegotiationPermissions, child: NegotiationPermissions) -> NegotiationPermissions:
    """Result is the AND of parent and child for each permission (child can only restrict, never widen)."""
    return NegotiationPermissions(
        price=parent.price and child.price,
        quantity=parent.quantity and child.quantity,
        bundles=parent.bundles and child.bundles,
        substitutes=parent.substitutes and child.substitutes,
        shipping=parent.shipping and child.shipping,
        delivery_date=parent.delivery_date and child.delivery_date,
    )

def resolve_effective_policy(
    store_id: str,
    category: str | None = None,
    sku: str | None = None,
    campaign_id: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> EffectivePolicy:
    """
    Merge the MEC hierarchy.
    Resolution order:
    1. Load store-level MEC (scope=STORE)
    2. If category provided, load category override (scope=CATEGORY, scope_value=category)
    3. If sku provided, load SKU override (scope=SKU, scope_value=sku)
    4. If campaign_id provided, load campaign override (scope=CAMPAIGN, scope_value=campaign_id)
    """
    chain = []
    
    # 1. Store level
    store_mec = get_latest_mec(store_id, MECScope.STORE, conn=conn)
    if store_mec:
        chain.append(store_mec)
        
    # 2. Category level
    if category:
        cat_mec = get_latest_mec(store_id, MECScope.CATEGORY, scope_value=category, conn=conn)
        if cat_mec:
            chain.append(cat_mec)
            
    # 3. SKU level
    if sku:
        sku_mec = get_latest_mec(store_id, MECScope.SKU, scope_value=sku, conn=conn)
        if sku_mec:
            chain.append(sku_mec)
            
    # 4. Campaign level
    if campaign_id:
        camp_mec = get_latest_mec(store_id, MECScope.CAMPAIGN, scope_value=campaign_id, conn=conn)
        if camp_mec:
            chain.append(camp_mec)
            
    if not chain:
        # Fall back to defaults
        hc_data = settings.DEFAULT_MEC_HARD_CONSTRAINTS.copy()
        if "approval_thresholds" in hc_data:
            hc_data["approval_thresholds"] = ApprovalThresholds(**hc_data["approval_thresholds"])
            
        hard_constraints = HardConstraints(**hc_data)
        economic_objectives = EconomicObjectives(**settings.DEFAULT_MEC_OBJECTIVES)
        negotiation = NegotiationPermissions(**settings.DEFAULT_MEC_NEGOTIATION)
        source_chain = []
    else:
        # Initial policy from first in chain (usually store)
        current = chain[0]
        hard_constraints = current.hard_constraints
        economic_objectives = current.objectives
        negotiation = current.negotiation
        source_chain = [f"{current.scope}:{current.version}"]
        
        # Merge subsequent
        for child in chain[1:]:
            source_chain.append(f"{child.scope}:{child.version}")
            if child.hard_constraints:
                hard_constraints = _merge_hard_constraints(hard_constraints, child.hard_constraints)
            if child.objectives:
                economic_objectives = child.objectives
            if child.negotiation:
                negotiation = _restrict_negotiation(negotiation, child.negotiation)
                
    policy_dict = {
        "hard_constraints": dataclasses.asdict(hard_constraints),
        "economic_objectives": {
            k: str(v) for k, v in dataclasses.asdict(economic_objectives).items()
        },
        "negotiation": dataclasses.asdict(negotiation)
    }
    
    # Convert Decimals in hard constraints to string
    policy_dict["hard_constraints"]["min_margin_pct"] = str(
        policy_dict["hard_constraints"]["min_margin_pct"]
    )
    
    import hashlib
    policy_hash = hashlib.sha256(canonical_json(policy_dict).encode('utf-8')).hexdigest()
    
    return EffectivePolicy(
        policy_id=new_id("POL"),
        store_id=store_id,
        hard_constraints=hard_constraints,
        economic_objectives=economic_objectives,
        negotiation=negotiation,
        source_chain=source_chain,
        hash=policy_hash,
        created_at=to_ts(utc_now())
    )
