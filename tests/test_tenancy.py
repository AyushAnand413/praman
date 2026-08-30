"""Tenancy: per-store isolation is structural, not a convention.

The learning tables carry store_id from day one; these tests prove the
resolution rules and, more importantly, the adversarial direction — data
recorded under one tenant cannot leak into another's reads even when both
exist in the same database.
"""

from __future__ import annotations

import pytest

from store import pairings, tenancy
from store.timestamps import utc_now


@pytest.fixture(autouse=True)
def clean_context():
    tenancy.reset_current()
    yield
    tenancy.reset_current()


def test_default_tenant_is_the_single_store_case():
    assert tenancy.current_store() == tenancy.DEFAULT_STORE_ID


def test_resolve_rejects_missing_and_unknown_stores():
    with pytest.raises(tenancy.UnknownStore):
        tenancy.resolve(None)
    with pytest.raises(tenancy.UnknownStore):
        tenancy.resolve("not-a-hosted-store")


def test_set_current_requires_a_configured_store(monkeypatch):
    monkeypatch.setattr(settings_stores(), "PRAMAN_STORES", ("voltmart", "gadgethub"))
    tenancy.set_current("VoltMart ")  # case/space normalised by resolve
    assert tenancy.current_store() == "voltmart"
    with pytest.raises(tenancy.UnknownStore):
        tenancy.set_current("evil-store")


def settings_stores():
    import settings

    return settings


def test_learning_follows_the_current_tenant(db, monkeypatch):
    monkeypatch.setattr(settings_stores(), "PRAMAN_STORES", ("voltmart", "gadgethub"))
    now = utc_now()

    tenancy.set_current("voltmart")
    for _ in range(5):
        pairings.record_order_basket("AT-PRO-BLK", ["AT-CBL-USBC"])

    # GadgetHub's context sees nothing of VoltMart's history...
    tenancy.set_current("gadgethub")
    assert pairings.pairs_for("AT-PRO-BLK") == []
    assert pairings.related_skus("AT-PRO-BLK") == frozenset()

    # ...and an explicit id cannot reach across either.
    explicit = pairings.related_skus("AT-PRO-BLK", store_id="voltmart")
    assert "AT-CBL-USBC" in explicit
    cross = pairings.related_skus(
        "AT-PRO-BLK", store_id="voltmart"
    ) - pairings.related_skus("AT-PRO-BLK", store_id="gadgethub")
    assert cross  # voltmart knows something gadgethub does not


def test_explicit_id_beats_context(db, monkeypatch):
    monkeypatch.setattr(settings_stores(), "PRAMAN_STORES", ("a", "b"))
    tenancy.set_current("b")
    pairings.seed_pairing("AT-PRO-BLK", "AT-CASE-01", store_id="a")

    # Context says b; explicit a wins for this call.
    pairs = pairings.pairs_for("AT-PRO-BLK", store_id="a")
    assert [p["sku"] for p in pairs] == ["AT-CASE-01"]
