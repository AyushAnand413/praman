"""The selling envelope — what the model is allowed to know.

The model needs to know how far it may go. It does not need to know what
anything cost. Those are different facts, and this module is the seam between
them: it takes the public catalog row plus the private economics row and emits a
third thing that is neither.

What crosses into the envelope from the private view:

* `discount_headroom_pct` — the tighter of the global per-SKU cap and this
  SKU's own cap. A ceiling, not a cost.
* `attach` — which SKUs pair with this one, and how often, as a whole
  percentage.
* `upgrade_sku` — the SKU one tier up, when there is one.

What never crosses: `cost_inr`, `margin_pct`, `floor_price_inr`. The floor is
the interesting omission. Giving the model a minimum price would look helpful,
but the floor is only ever *slacker* than the percentage cap on this catalog, so
it would add nothing the model can act on while handing it a number from which
the cost is one division away. The floor stays inside the kernel, which is where
the veto lives anyway.

The names differ from the private column names on purpose. If a future bug ever
serialised an envelope into a response, the leak test's field-name check would
still be looking for `max_discount_pct` and `attach_rate` and would miss it — so
this module does not reuse those names, and there is nothing for it to miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping, Sequence

from kernel.bounds import effective_max_discount_pct
from kernel.search import relevance


@dataclass(frozen=True)
class Attach:
    """A SKU that pairs with another, and how often buyers take it."""

    sku: str
    popularity_pct: int


@dataclass(frozen=True)
class SellableSku:
    """One product as the model sees it."""

    sku: str
    title: str
    category: str
    list_price_inr: int
    available_qty: int
    returns_window_days: int
    attrs: Mapping[str, Any]
    discount_headroom_pct: int
    attach: tuple[Attach, ...] = ()
    upgrade_sku: str | None = None

    @property
    def in_stock(self) -> bool:
        return self.available_qty > 0

    def max_discount_inr(self, qty: int = 1) -> int:
        """The largest whole-rupee discount the headroom allows on `qty` units.

        Rounded down, so a headroom of 12% on Rs 4,999 is Rs 599 rather than
        Rs 600. Rounding a discount up would put the offered price a rupee below
        what the cap actually permits, and the kernel would refuse the line the
        model was told it could sell.
        """
        gross = Decimal(self.list_price_inr * qty) * Decimal(
            self.discount_headroom_pct
        ) / Decimal(100)
        return int(gross)

    def as_prompt_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sku": self.sku,
            "title": self.title,
            "category": self.category,
            "list_price_inr": self.list_price_inr,
            "available_qty": self.available_qty,
            "returns_window_days": self.returns_window_days,
            "attrs": dict(self.attrs),
            "discount_headroom_pct": self.discount_headroom_pct,
        }
        if self.attach:
            body["attach"] = [
                {"sku": a.sku, "popularity_pct": a.popularity_pct} for a in self.attach
            ]
        if self.upgrade_sku:
            body["upgrade_sku"] = self.upgrade_sku
        return body


def _popularity_pct(attach_rate: Any) -> int:
    """An attach rate as a whole percentage.

    Rounded to an integer rather than passed through as 0.31, so the value the
    model sees is not the value the private view holds. The rounding is real
    information loss and that is the point: the model gets "roughly a third of
    buyers", which is all a sales argument needs.
    """
    try:
        rate = Decimal(str(attach_rate))
    except (TypeError, ValueError):
        return 0
    return int((rate * 100).to_integral_value(rounding=ROUND_HALF_UP))


def build(
    public_rows: Iterable[Mapping[str, Any]],
    private_by_sku: Mapping[str, Mapping[str, Any]],
    *,
    available_by_sku: Mapping[str, int] | None = None,
) -> tuple[SellableSku, ...]:
    """Join the two catalog views into the envelope, dropping the economics.

    `available_by_sku` carries live availability — on-hand minus active holds —
    so the model is not invited to sell a unit another session has already
    reserved. Without it the catalog's own `stock_qty` is used, which is right
    for a prompt built outside a request.

    A SKU with no private row is dropped rather than defaulted. A missing
    economics row means the catalog is half-loaded, and inventing a headroom for
    it would be the one kind of guess that costs money.
    """
    built: list[SellableSku] = []
    for row in public_rows:
        sku = str(row["sku"])
        private = private_by_sku.get(sku)
        if private is None:
            continue
        available = (
            int(available_by_sku[sku])
            if available_by_sku is not None and sku in available_by_sku
            else int(row.get("stock_qty", 0))
        )
        attach = tuple(
            Attach(sku=str(c["sku"]), popularity_pct=_popularity_pct(c.get("attach_rate")))
            for c in private.get("attach_candidates", ())
            if c.get("sku")
        )
        built.append(
            SellableSku(
                sku=sku,
                title=str(row.get("title", "")),
                category=str(row.get("category", "")),
                list_price_inr=int(row["list_price_inr"]),
                available_qty=available,
                returns_window_days=int(row.get("returns_window_days", 0)),
                attrs=dict(row.get("attrs") or {}),
                discount_headroom_pct=effective_max_discount_pct(
                    private.get("max_discount_pct")
                ),
                attach=attach,
                upgrade_sku=private.get("tier_up_sku") or None,
            )
        )
    return tuple(sorted(built, key=lambda s: s.sku))


def by_sku(envelope: Sequence[SellableSku]) -> dict[str, SellableSku]:
    return {item.sku: item for item in envelope}


def as_prompt_payload(envelope: Sequence[SellableSku]) -> list[dict[str, Any]]:
    return [item.as_prompt_payload() for item in envelope]


def pick_base(
    envelope: Sequence[SellableSku],
    *,
    query: str | None = None,
    category: str | None = None,
    budget_inr: int | None = None,
    qty: int = 1,
) -> SellableSku | None:
    """Choose a base SKU without a model. Deterministic, and deliberately dull.

    This is what the fallback path sells when the model is unavailable or
    returned nothing usable twice: the best token match for the stated need
    among the in-stock items that fit the category and the budget, cheapest
    first when nothing matches. It is a worse salesman than the model by design
    — the claim being made is that the system degrades to a plain storefront,
    not that it degrades to something clever.

    Returns None when nothing in stock fits, which the caller must treat as a
    refusal rather than as an empty offer.
    """
    candidates = [item for item in envelope if item.available_qty >= qty]
    if category:
        narrowed = [c for c in candidates if c.category == category]
        # An unmatched category is a hint that did not apply, not a filter that
        # must exclude everything: falling back to the whole catalog answers the
        # request rather than refusing it on a spelling.
        candidates = narrowed or candidates
    if budget_inr is not None:
        candidates = [c for c in candidates if c.list_price_inr * qty <= budget_inr]
    if not candidates:
        return None

    if query:
        scored = [
            (
                relevance(
                    query,
                    sku=c.sku,
                    title=c.title,
                    category=c.category,
                    attrs=c.attrs,
                ),
                c,
            )
            for c in candidates
        ]
        matched = [(score, c) for score, c in scored if score > 0]
        if matched:
            # Best match first, then cheapest, then SKU. Price breaks the tie
            # rather than deciding the pick: a buyer who described a gym use case
            # should get the sweatproof earbuds, not the cheapest cable that
            # happens to be in stock.
            return min(matched, key=lambda pair: (-pair[0], pair[1].list_price_inr, pair[1].sku))[1]

    # Sorted by SKU as the tie-break so two identical calls cannot disagree.
    return min(candidates, key=lambda c: (c.list_price_inr, c.sku))
