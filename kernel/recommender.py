"""Recommendation engine — algorithmic upsell selection, no LLM required.

Reads from ``store.pairings`` (the co-occurrence / confidence table that
checkout.py feeds after every completed sale) and the catalog envelope to
produce ranked companion proposals the offer assembler can use directly.

The LLM is not in this path and does not need to be. The ranking is:

    1. Observed pairings with ``samples >= MIN_SAMPLES``, sorted by
       confidence descending (= ``strength`` from pairings.pairs_for).
    2. Seeded (catalog-declared) pairings with samples == 0, used as
       cold-start priors when real data is absent.
    3. Both are filtered by in-stock and optional budget ceiling.

The ``why`` sentences here are plain, store-authored text. They are flagged
``PROSE_STORE`` by the assembler and appear in the receipt as such — the
buyer and an auditor can tell store-authored copy from model copy.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Mapping, Sequence

import settings
from store import pairings as pairings_store
from vyapaari.envelope import SellableSku
from vyapaari.schema import BUNDLE_ATTACH, MAX_UPSELLS, TIER_UPGRADE, ProposedUpsell

log = logging.getLogger("aether")

# ── Prose ────────────────────────────────────────────────────────────────────
# Store-authored sentences — short, factual, no prices or percentages.

_WHY_ATTACH = (
    "Pairs well with your purchase and is popular with buyers who chose this item."
)
_WHY_TIER = (
    "The next model up — better specs for buyers who want more performance."
)

# ── Thresholds ───────────────────────────────────────────────────────────────

#: Minimum confidence (together / denominator) for an observed pair to rank
#: above seeded ones. Below this the evidence is too thin to lead with.
MIN_CONFIDENCE = Decimal("0.05")  # 5% of base-item buyers took the companion

#: Minimum lift for observed pairs. lift < 1 means the pair is *less* common
#: than chance — suppress those even if confidence technically clears.
MIN_LIFT = Decimal("1.0")


def _lift(pair: dict[str, Any], total_base_orders: int = 0) -> Decimal:
    """Confidence / P(companion) — how much more likely than random."""
    if "lift" in pair:
        return Decimal(str(pair["lift"]))
    samples = int(pair.get("samples", 0))
    if samples == 0:
        return Decimal("1.0")
    strength = Decimal(str(pair.get("strength", 0)))
    return max(Decimal("1.0"), strength)


def recommend_upsells(
    base_sku: str,
    envelope_by_sku: Mapping[str, SellableSku],
    *,
    base_qty: int = 1,
    budget_inr: int | None = None,
    limit: int = MAX_UPSELLS,
    store_id: str | None = None,
    conn: Any = None,
) -> list[ProposedUpsell]:
    """Return ranked upsell proposals for ``base_sku`` from the pairings table.

    Arguments
    ---------
    base_sku:
        The item the buyer is purchasing.
    envelope_by_sku:
        The current selling envelope (available_qty reflects live stock holds).
    base_qty:
        How many units of the base item are in the cart — used for budget math.
    budget_inr:
        If set, companions whose addition would push the cart total above this
        are silently skipped.
    limit:
        Maximum companions returned. Defaults to ``schema.MAX_UPSELLS`` so the
        output drops straight into a Proposal.
    store_id:
        Store / tenant key. Defaults to current tenant.
    conn:
        Database connection.

    Returns
    -------
    list[ProposedUpsell]
        Ordered highest-confidence first (observed), then seeded. Empty when
        nothing suitable exists — the caller treats that as "no bundle".
    """
    base = envelope_by_sku.get(base_sku)
    if base is None:
        return []

    base_price_total = base.list_price_inr * base_qty
    raw_pairs = pairings_store.pairs_for(base_sku, limit=20, store_id=store_id, conn=conn)

    observed: list[tuple[Decimal, str, str]] = []   # (confidence, sku, utype)
    seeded: list[tuple[str, str]] = []              # (sku, utype)

    for pair in raw_pairs:
        companion_sku: str = pair["sku"]
        companion = envelope_by_sku.get(companion_sku)
        if companion is None:
            continue
        if not companion.in_stock:
            continue
        if budget_inr is not None:
            if base_price_total + companion.list_price_inr > budget_inr:
                continue

        # Determine upsell type
        utype = TIER_UPGRADE if (
            base.upgrade_sku and base.upgrade_sku == companion_sku
        ) else BUNDLE_ATTACH

        samples = int(pair.get("samples", 0))
        strength = Decimal(str(pair.get("strength", 0)))
        lift = Decimal(str(pair.get("lift", 1.0)))
        source = pair.get("source", "seeded")

        if source == "observed" and samples > 0:
            if strength >= MIN_CONFIDENCE and lift >= MIN_LIFT:
                observed.append((lift, strength, companion_sku, utype))
        else:
            # Seeded / cold-start — include but rank below observed
            seeded.append((companion_sku, utype))

    # Sort observed by lift descending, then strength descending
    observed.sort(key=lambda t: (-t[0], -t[1], t[2]))

    seen: set[str] = set()
    results: list[ProposedUpsell] = []

    for lift, confidence, sku, utype in observed:
        if sku not in seen and len(results) < limit:
            results.append(_make_upsell(sku, utype))
            seen.add(sku)

    for sku, utype in seeded:
        if sku not in seen and len(results) < limit:
            results.append(_make_upsell(sku, utype))
            seen.add(sku)

    log.debug(
        "recommender | base=%s observed=%d seeded=%d returning=%d",
        base_sku, len(observed), len(seeded), len(results),
    )
    return results


def _make_upsell(sku: str, utype: str) -> ProposedUpsell:
    return ProposedUpsell(
        upsell_type=utype,
        sku=sku,
        qty=1,
        discount_pct=Decimal(0),
        why=_WHY_TIER if utype == TIER_UPGRADE else _WHY_ATTACH,
    )


def seed_pairings_from_catalog(conn=None) -> int:
    """Seed cold-start pairings from catalog.json ``attach_candidates``.

    Idempotent — ``seed_pairing`` uses ``ON CONFLICT DO NOTHING``.
    Called once at app startup so Bound 10 passes for catalog-declared
    companions even before any real orders exist.

    Returns the number of pairing rows written (0 on subsequent calls when
    all rows already exist).
    """
    from store import catalog as catalog_module

    if not catalog_module.cache._private:
        catalog_module.cache.load(conn=conn)

    pairs_to_seed = []
    for sku in catalog_module.cache._private:  # noqa: SLF001 — peer-module access
        private = catalog_module.cache.private(sku)
        if private is None:
            continue
        for candidate in (private.get("attach_candidates") or []):
            paired = (
                candidate.get("sku") if isinstance(candidate, dict) else candidate
            )
            if paired:
                pairs_to_seed.append((str(sku), str(paired)))
        tier_up = private.get("tier_up_sku")
        if tier_up:
            pairs_to_seed.append((str(sku), str(tier_up)))
    return pairings_store.seed_pairings_batch(pairs_to_seed, conn=conn)
