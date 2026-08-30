"""The pairings store: counting baskets, decaying evidence, seeding priors.

This table is where the store's sales experience accumulates. Every claim it
makes later (via `get_pairings` and bound 10) is only as good as what these
tests pin down: correct ratios, honest denominators, aging evidence, and
observed-beats-seeded precedence.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import settings
from store import pairings
from store.timestamps import utc_now


def test_basket_counting_produces_correct_strength(db):
    now = utc_now()
    # Five AT-PRO-BLK baskets; four include a AT-CBL-USBC.
    for i in range(4):
        pairings.record_order_basket("AT-PRO-BLK", ["AT-CBL-USBC"], now=now)
    pairings.record_order_basket("AT-PRO-BLK", [], now=now)

    pairs = {p["sku"]: p for p in pairings.pairs_for("AT-PRO-BLK")}
    assert pairs["AT-CBL-USBC"]["strength"] == pytest.approx(0.8)
    assert pairs["AT-CBL-USBC"]["samples"] == 5
    assert pairs["AT-CBL-USBC"]["source"] == "observed"


def test_duplicate_companion_in_one_basket_counts_once(db):
    now = utc_now()
    pairings.record_order_basket("AT-PRO-BLK", ["AT-CASE-01", "AT-CASE-01"], now=now)

    pairs = {p["sku"]: p for p in pairings.pairs_for("AT-PRO-BLK")}
    assert pairs["AT-CASE-01"]["samples"] == 1
    assert pairs["AT-CASE-01"]["strength"] == pytest.approx(1.0)


def test_evidence_ages_with_half_life_decay(db):
    now = utc_now()
    pairings.record_order_basket("AT-PRO-BLK", ["AT-CBL-USBC"], now=now)
    before = {p["sku"]: p for p in pairings.pairs_for("AT-PRO-BLK", now=now)}[
        "AT-CBL-USBC"
    ]
    assert before["strength"] == pytest.approx(1.0)

    # One half-life later, a fresh unrelated basket halves the old counts.
    # Reads evaluate evidence as of a moment — here, that later instant.
    later = now + timedelta(days=settings.PAIRING_HALF_LIFE_DAYS)
    pairings.record_order_basket("AT-PRO-BLK", [], now=later)

    after = {p["sku"]: p for p in pairings.pairs_for("AT-PRO-BLK", now=later)}[
        "AT-CBL-USBC"
    ]
    assert after["strength"] < before["strength"]
    # Numerator halved to ~0.5; denominator is ~0.5 decayed + 1 new = ~1.5.
    assert after["strength"] == pytest.approx(0.3333, abs=0.01)


def test_untrusted_pairs_are_excluded_from_relatedness_until_enough_samples(db):
    now = utc_now()
    # Below the threshold: an anecdote, not evidence.
    for _ in range(settings.RELATEDNESS_MIN_SAMPLES - 1):
        pairings.record_order_basket("AT-PRO-BLK", ["AT-CBL-USBC"], now=now)
    assert "AT-CBL-USBC" not in pairings.related_skus("AT-PRO-BLK")

    # Crossing the threshold promotes it to enforceable evidence.
    pairings.record_order_basket("AT-PRO-BLK", ["AT-CBL-USBC"], now=now)
    assert "AT-CBL-USBC" in pairings.related_skus("AT-PRO-BLK")


def test_seeded_rows_surface_but_never_become_evidence(db):
    now = utc_now()
    pairings.seed_pairing("AT-STUDIO-PRO", "AT-DAC-01")
    pairings.seed_pairing("AT-STUDIO-PRO", "AT-DAC-01")  # idempotent

    pairs = {p["sku"]: p for p in pairings.pairs_for("AT-STUDIO-PRO")}
    assert pairs["AT-DAC-01"]["source"] == "seeded"
    assert pairs["AT-DAC-01"]["samples"] == 0

    # Seeded names are enforceable relations from day one...
    assert "AT-DAC-01" in pairings.related_skus("AT-STUDIO-PRO")

    # ...but observed evidence replaces them on read, never merges counts.
    pairings.record_order_basket("AT-STUDIO-PRO", ["AT-DAC-01"], now=now)
    observed_only = [
        p for p in pairings.pairs_for("AT-STUDIO-PRO") if p["sku"] == "AT-DAC-01"
    ]
    assert len(observed_only) == 1
    assert observed_only[0]["source"] == "observed"


def test_stores_do_not_share_learning(db):
    now = utc_now()
    for _ in range(settings.RELATEDNESS_MIN_SAMPLES):
        pairings.record_order_basket(
            "AT-PRO-BLK", ["AT-CBL-USBC"], store_id="voltmart", now=now
        )

    # VoltMart's evidence is enforceable at VoltMart and invisible elsewhere.
    assert "AT-CBL-USBC" in pairings.related_skus("AT-PRO-BLK", store_id="voltmart")
    assert pairings.pairs_for("AT-PRO-BLK", store_id="gadgethub") == []
    assert pairings.related_skus("AT-PRO-BLK", store_id="gadgethub") == frozenset()


def test_snapshot_reports_health(db):
    now = utc_now()
    pairings.seed_pairing("AT-STUDIO-PRO", "AT-DAC-01")
    pairings.record_order_basket("AT-STUDIO-PRO", ["AT-DAC-01"], now=now)
    health = pairings.snapshot()
    assert health["bases_observed"] == 1
    assert health["observed_pairs"] == 1
    assert health["seeded_pairs"] == 1

