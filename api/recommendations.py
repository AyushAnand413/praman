"""Recommendations API: algorithmic bundle recommendations and companions.

Pure pairings + catalog data: no LLM, no session, no mandate.
"""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException

from kernel.recommender import recommend_upsells
from store import catalog, pairings as pairings_store
from vyapaari import envelope as envelope_module

router = APIRouter(prefix="/agent/v1", tags=["recommendations"])


@router.get("/recommendations/{sku}")
def get_recommendations(
    sku: str,
    budget_inr: int | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Returns algorithmic companion recommendations and bundles for a base SKU."""
    product = catalog.cache.public(sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"SKU {sku!r} not found in catalog")

    raw_pairs = pairings_store.pairs_for(sku, limit=limit)

    # Build sellable envelope to determine in-stock bundles
    public_rows = catalog.cache.all_public()
    private_by_sku = catalog.cache.all_private_by_sku()
    envelope = envelope_module.build(public_rows, private_by_sku)
    by_sku = envelope_module.by_sku(envelope)

    upsells = recommend_upsells(sku, by_sku, budget_inr=budget_inr, limit=limit)

    base_price = product.get("list_price_inr", 0)
    bundle_options = [
        {
            "option_id": "A",
            "items": [sku],
            "total_inr": base_price,
            "title": f"{product.get('title')} (Single)",
        }
    ]

    current_items = [sku]
    current_total = base_price
    option_letters = ["B", "C", "D", "E"]

    for idx, u in enumerate(upsells):
        comp_product = catalog.cache.public(u.sku)
        comp_price = comp_product.get("list_price_inr", 0) if comp_product else 0
        current_items = [*current_items, u.sku]
        current_total += comp_price
        opt_letter = option_letters[idx] if idx < len(option_letters) else f"OPT_{idx+2}"
        bundle_options.append(
            {
                "option_id": opt_letter,
                "items": list(current_items),
                "total_inr": current_total,
                "title": f"Bundle with {comp_product.get('title') if comp_product else u.sku}",
                "why": u.why,
            }
        )

    return {
        "base_sku": sku,
        "product": product,
        "recommendations": raw_pairs,
        "bundle_options": bundle_options,
    }
