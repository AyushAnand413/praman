"""The A/B harness: both arms walk the same rail; only choice differs.

Runs a small experiment (not the full 400) against the real app over HTTP in
shadow mode, records the rows the report is computed from, and checks that
the control arm's attach rate is exactly zero — an arm that takes upsells it
never saw would invalidate every number above it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from harness import ab
from harness.grahak import PERSONAS
from store import measurement


#: Personas whose asks stay inside Tier 0/1, so a session can complete without
#: a human decision the harness cannot click.
SMALL_PERSONAS = tuple(
    p for p in PERSONAS if p.name in
    ("budget_tight", "gift_buyer", "replacement_part", "deadline_driven")
)


@pytest.fixture
def bundled_fallback(monkeypatch):
    """Make the deterministic fallback propose one cheap bundle_attach.

    Without a model configured the fallback proposes base-only by design, which
    would make both arms identical and prove nothing. This attaches a real
    upsell to the same fallback path AND seeds its relation into the pairings
    table — which is precisely how a production store would cold-start (F5):
    declare the prior, let observed sales replace it.
    """
    from vyapaari import proposer
    from vyapaari.envelope import SellableSku
    from vyapaari.prompt import ProposalRequest
    from vyapaari.schema import BUNDLE_ATTACH, Proposal, ProposedUpsell

    original = proposer._fallback_proposal

    def with_upsell(request: ProposalRequest, envelope: list[SellableSku]):
        outcome = original(request, envelope)
        if not outcome:
            return ()
        first = outcome[0]
        from store import pairings

        pairings.seed_pairing(first.base.sku, "AT-CASE-01")
        return (
            Proposal(
                base=first.base,
                upsells=(
                    ProposedUpsell(
                        sku="AT-CASE-01",
                        qty=1,
                        discount_pct=Decimal("0"),
                        upsell_type=BUNDLE_ATTACH,
                        why="A natural pairing.",
                    ),
                ),
            ),
        )

    monkeypatch.setattr(proposer, "_fallback_proposal", with_upsell)


@pytest.fixture
def transport_factory(client: TestClient):
    return lambda: client


def test_control_takes_no_upsells_and_treatment_can(
    db, transport_factory, monkeypatch, bundled_fallback
):
    # No Gemini key in the environment keeps the proposer on its deterministic
    # path, so the experiment does not depend on a model at all.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    results = ab.run_ab(
        sessions_per_arm=4,
        transport_factory=transport_factory,
        personas=SMALL_PERSONAS,
    )
    assert len(results) == 8

    summary = ab.summarize(results)
    assert set(("control", "treatment")) <= set(summary)
    assert summary["control"]["attach_rate"] == 0.0
    assert summary["control"]["sessions"] == 4
    assert summary["treatment"]["sessions"] == 4

    # Both arms bought something; only the offered arm took an upsell.
    if summary["control"]["orders"] < 1:
        print("RESULTS:", results)
        print("SUMMARY:", summary)
    assert summary["control"]["orders"] >= 1
    assert summary["treatment"]["orders"] >= 1
    assert summary["treatment"]["upsells_taken"] >= 1


def test_sessions_are_recorded_for_the_report(
    db, transport_factory, monkeypatch, bundled_fallback
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    results = ab.run_ab(
        sessions_per_arm=2,
        transport_factory=transport_factory,
        personas=SMALL_PERSONAS[:2],
    )
    for r in results:
        measurement.record_session(
            session_id=f"ab-{r.arm}-{r.index}",
            arm=r.arm,
            persona=r.persona,
            basket_inr=r.basket_inr,
            upsells_shown=r.upsells_shown,
            upsells_taken=r.upsells_taken,
            completed=r.completed,
        )

    rows = measurement.rows()
    assert len(rows) == 4
    arms = {row["arm"] for row in rows}
    assert arms == {"control", "treatment"}
    assert all(row["ab_session_id"].startswith("AB-") for row in rows)


def test_refusals_are_data_not_crashes(db):
    """A persona whose ask cannot be met still yields a structured result."""
    result = ab.run_session(
        PERSONAS[5],  # upgrade_seeker: asks for the studio model
        "control",
        transport_factory=lambda: None,
        index=0,
    )
    # transport None fails inside the agent if it runs — but whatever happens,
    # the harness records an outcome instead of raising past the runner.
    assert result.arm == "control"
    assert isinstance(result.error, str)
