"""Exploration tools — what the agentic proposer may look at.

The proposer used to receive the whole catalog in one prompt and answer once.
That breaks at real catalog sizes and, worse, it cannot discover anything: a
buyer asking for a laptop gets laptop-shaped prompt lines, so a cooling pad
never enters the model's field of view no matter how well it would sell.

The fix is tools. The model may, mid-thought:

    search_catalog("cooling pad")   → real SKUs matching that idea, or none
    get_pairings("ROG-STRIX-01")    → what buyers of that item actually took

and only then commit to a proposal. Two properties are structural, not hoped
for:

**It can only propose what a tool returned.** The search functions read from
the selling envelope — the same sanitized view the prompt always used — so an
invented SKU has no origin anywhere in the loop, and the kernel's invented-SKU
rejection stays as the backstop rather than the only defence.

**It holds nothing dangerous.** The toolbelt is two read functions over data
already destined for the model. No credentials (the import-boundary test walks
this file), no writes, no payment surface — exploration cannot do anything but
look.

`get_pairings` is injected as a callable rather than imported here on purpose:
the pairings store lives behind the caller (kernel/offer.py wires it to
`store.pairings.pairs_for`), which keeps this module pure computation over
whatever evidence its constructor hands it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from kernel.search import relevance
from vyapaari.envelope import SellableSku

#: A search hit, shaped for the model. Envelope fields only — everything here
#: is already published to the buyer in machine-readable form elsewhere.
SearchFn = Callable[[str], list[dict[str, Any]]]
PairLookup = Callable[[str], list[dict[str, Any]]]

#: Default result ceiling per search. Enough shelf for a proposal, few enough
#: tokens that four searches still fit comfortably in the context.
DEFAULT_SEARCH_LIMIT = 12


@dataclass(frozen=True)
class ExplorationTools:
    """The two reads an exploring agent is allowed."""

    search_catalog: SearchFn
    get_pairings: PairLookup

    #: Names as the agent protocol names them. Kept beside the functions so the
    #: loop and any future introspection agree on the vocabulary.
    ACTION_NAMES = ("search_catalog", "get_pairings")


def envelope_search_factory(
    envelope: Sequence[SellableSku],
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> SearchFn:
    """Build `search_catalog` over an already-built selling envelope.

    Deterministic token relevance (`kernel.search`) over the envelope's public
    fields, in-stock first, cheapest as tie-break. Zero-relevance matches are
    omitted rather than padded: "no such thing" is information the agent needs,
    and dead ends are how it learns to reformulate.
    """

    def search(query: str) -> list[dict[str, Any]]:
        wanted = str(query or "").strip()
        if not wanted:
            return []
        scored = []
        for item in envelope:
            if not item.in_stock:
                continue
            score = relevance(
                wanted,
                sku=item.sku,
                title=item.title,
                category=item.category,
                attrs=dict(item.attrs),
            )
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].list_price_inr, pair[1].sku))
        return [_hit(item) for _, item in scored[:limit]]

    return search


def _hit(item: SellableSku) -> dict[str, Any]:
    return {
        "sku": item.sku,
        "title": item.title,
        "category": item.category,
        "list_price_inr": item.list_price_inr,
        "available_qty": item.available_qty,
        "discount_headroom_pct": item.discount_headroom_pct,
    }
