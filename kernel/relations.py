"""Relatedness inputs — the evidence set bound 10 judges against.

Bound 10 ("an upsell must relate to what it accompanies") is a pure function:
it takes a set of related SKUs and answers membership. This module is where
that set comes from. Three sources, unioned:

1. **Learned pairings** (`store.pairings`) — companions that real completed
   baskets actually confirm, past the minimum-samples threshold. The strongest
   evidence and the only kind that grows by itself.
2. **Declared companions** — the catalog's own `attach_candidates` and
   `tier_up_sku` from `product_private`. The hand-authored prior generation:
   merchant knowledge written before any data existed. As orders accumulate,
   learned evidence gradually covers these; until then they keep the check
   honest on a cold catalog instead of rejecting every historical pairing.
3. Nothing else, deliberately. "Same category" is NOT relatedness here — the
   demo catalog's categories are coarse enough that category-matching would
   wave through genuine nonsense, and a phone case is earbud-accessory-adjacent
   while being meaningless next to a laptop. Evidence or declaration, or no.

The module reads only public/private catalog views and the pairings store —
no network, no LLM, no money state — so its output is deterministic for a given
database instant, which is exactly what a bound's input must be.
"""

from __future__ import annotations

from typing import Any

from store import catalog, pairings


def declared_companions(base_sku: str) -> frozenset[str]:
    """Companions the catalog itself declares: attach candidates + tier-up."""
    private = catalog.cache.private(base_sku)
    if private is None:
        return frozenset()
    declared: set[str] = set()
    for candidate in private.get("attach_candidates") or []:
        sku = candidate.get("sku") if isinstance(candidate, dict) else candidate
        if sku:
            declared.add(str(sku))
    tier_up = private.get("tier_up_sku")
    if tier_up:
        # A tier upgrade is inherently related: it is the same product, more
        # of it. Bound 1 still guards its price like any other line.
        declared.add(str(tier_up))
    return frozenset(declared)


def related_by_base_for_items(
    items: list[Any],
    *,
    store_id: str = pairings.DEFAULT_STORE_ID,
) -> dict[str, frozenset[str]]:
    """The map evaluate_cart consumes to enable bound 10.

    Keys are the cart's base-item SKUs; values are every SKU considered related
    to that base — learned companions past the sample threshold, plus declared
    ones (attach candidates, tier-up). Callers pass this straight into
    `evaluate_offer` / `evaluate_checkout`. An empty dict (or omitting the
    parameter entirely) disables the bound — the enforcement sites, not the
    signature default, decide when relatedness applies.

    Items are `kernel.bounds.LineItem`s; only role==base keys matter, but the
    map is built defensively for any base present in the list.
    """
    from kernel.bounds import ROLE_BASE

    related: dict[str, frozenset[str]] = {}
    bases = sorted({item.sku for item in items if item.role == ROLE_BASE})
    for base_sku in bases:
        related[base_sku] = (
            pairings.related_skus(base_sku, store_id=store_id)
            | declared_companions(base_sku)
        )
    return related
