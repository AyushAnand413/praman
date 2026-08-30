"""The proposer's failure ladder, exercised without a network.

`vyapaari.proposer` makes exactly one promise: something usable comes back.
A well-formed model answer is used as-is; a malformed one earns one retry with
the broken field named; anything worse falls back to a base item at list price
chosen by rule. These tests walk every rung of that ladder by injecting fake
generators, which the module was built to allow.

The fallback path matters more than the happy path here. A model outage must
degrade to a plain storefront, not to a failed request — and a fallback that
sneaked in a discount or an upsell would be guessing at money decisions with
nothing left to check its work.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kernel.offer import unit_discount_inr
from store import catalog
from vyapaari import envelope as envelope_module
from vyapaari import proposer
from vyapaari.gemini import LLMUnavailable
from vyapaari.prompt import ProposalRequest
from vyapaari.schema import Proposal, ProposedItem, SchemaError


# ── helpers ────────────────────────────────────────────────────────────────────


def _envelope(db):
    """The selling envelope for the seeded 14-SKU catalog."""
    public_rows = catalog.cache.all_public()
    private_by_sku = {}
    for row in public_rows:
        private = catalog.cache.private(row["sku"])
        if private is not None:
            private_by_sku[row["sku"]] = dict(private)
    return envelope_module.build(public_rows, private_by_sku)


def _request(**overrides) -> ProposalRequest:
    fields = {"need": "wireless earbuds for commuting", "qty": 1}
    fields.update(overrides)
    return ProposalRequest(**fields)


def _llm_reply(base_sku: str, *, qty: int = 1, discount_pct=0) -> str:
    """A response shaped exactly as RESPONSE_SCHEMA declares."""
    return json.dumps(
        {
            "candidates": [
                {
                    "base": {
                        "sku": base_sku,
                        "qty": qty,
                        "discount_pct": discount_pct,
                        "why": "Solid battery life for a full commute.",
                    },
                    "proposed_upsells": [],
                }
            ]
        }
    )


@pytest.fixture
def stocky(db):
    """An in-stock SKU to propose against, plus its envelope entry."""
    env = _envelope(db)
    sellable = next(s for s in env if s.available_qty >= 1)
    return env, sellable


# ── the happy path ─────────────────────────────────────────────────────────────


def test_a_well_formed_answer_is_used_on_the_first_attempt(stocky):
    env, sellable = stocky
    calls: list[int] = []

    def generate(*, system, user, response_schema):
        calls.append(1)
        return _llm_reply(sellable.sku)

    outcome = proposer.propose(_request(), env, generate=generate, model="test-model")

    assert outcome.source == proposer.SOURCE_LLM
    assert outcome.attempts == 1
    assert len(calls) == 1
    assert not outcome.refused
    assert outcome.candidates[0].base.sku == sellable.sku
    assert outcome.from_model


# ── one retry, then the fallback ───────────────────────────────────────────────


def test_a_malformed_answer_is_retried_once_with_the_field_named(stocky):
    """The retry prompt names what broke, so it is a correction not a coin flip."""
    env, sellable = stocky
    seen_notes: list[str] = []

    def generate(*, system, user, response_schema):
        if len(seen_notes) == 0:
            # A price field the schema never promised. Silently dropping it
            # would produce a proposal that reads as priced when nothing did.
            seen_notes.append(user)
            return json.dumps(
                {
                    "candidates": [
                        {
                            "base": {
                                "sku": sellable.sku,
                                "qty": 1,
                                "discount_pct": 0,
                                "why": "fine",
                                "final_price_inr": 999,
                            },
                            "proposed_upsells": [],
                        }
                    ]
                }
            )
        assert "final_price_inr" in user, "retry must name the offending field"
        return _llm_reply(sellable.sku)

    outcome = proposer.propose(_request(), env, generate=generate)

    assert outcome.source == proposer.SOURCE_LLM_RETRY
    assert outcome.attempts == 2
    assert any("final_price_inr" in e for e in outcome.errors)


def test_two_malformed_answers_fall_back_to_the_deterministic_offer(stocky):
    """One retry, not three. The third attempt would spend latency for nothing."""
    env, _ = stocky
    calls: list[int] = []

    def generate(*, system, user, response_schema):
        calls.append(1)
        return "I am sorry, I cannot help with that."

    outcome = proposer.propose(_request(), env, generate=generate)

    assert calls == [1, 1]
    assert outcome.source == proposer.SOURCE_FALLBACK
    assert outcome.attempts == 2
    assert outcome.candidates[0].base.qty == 1


def test_a_model_that_never_answers_falls_back_after_one_attempt(stocky):
    """A timeout twice in a row waits out the same ceiling twice. Not worth it."""
    env, _ = stocky
    calls: list[int] = []

    def generate(*, system, user, response_schema):
        calls.append(1)
        raise LLMUnavailable("timed out")

    outcome = proposer.propose(_request(), env, generate=generate)

    assert calls == [1]
    assert outcome.source == proposer.SOURCE_FALLBACK
    assert any("timed out" in e for e in outcome.errors)


def test_no_configured_model_serves_the_fallback_without_calling_anything(db, monkeypatch):
    """An unconfigured deploy degrades rather than fails, and reaches nowhere."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def explode(*, system, user, response_schema):
        raise AssertionError("no transport exists to call")

    outcome = proposer.propose(_request(), _envelope(db), generate=None)

    assert outcome.source == proposer.SOURCE_FALLBACK
    assert "llm_not_configured" in outcome.errors


# ── the fallback's own terms ───────────────────────────────────────────────────


def test_the_fallback_sells_list_price_with_no_upsell_and_no_discount(stocky):
    """No discount and no upsells: neither is a guess this layer gets to make."""
    env, sellable = stocky

    outcome = proposer.propose(_request(base_sku=sellable.sku), env, generate=None)

    proposal = outcome.candidates[0]
    assert isinstance(proposal, Proposal)
    assert proposal.upsells == ()
    assert proposal.base.discount_pct == Decimal(0)
    assert proposal.base.sku == sellable.sku
    assert unit_discount_inr(sellable.list_price_inr, proposal.base.discount_pct) == 0


def test_the_fallback_honours_a_named_in_stock_sku(stocky):
    _, sellable = stocky

    outcome = proposer.propose(_request(base_sku=sellable.sku), stocky[0], generate=None)

    assert outcome.candidates[0].base.sku == sellable.sku


def test_a_named_sku_without_stock_is_not_quietly_substituted(db):
    """Naming an out-of-stock SKU is refused, not swapped behind the buyer's back."""
    env = _envelope(db)
    scarce = next(s for s in env if s.available_qty == 0)

    outcome = proposer.propose(
        _request(base_sku=scarce.sku), env, generate=None
    )

    # The named SKU cannot ship, so pick_base chooses on the stated need instead.
    assert outcome.candidates[0].base.sku != scarce.sku
    assert outcome.candidates[0].base.discount_pct == Decimal(0)


def test_nothing_fitting_the_request_is_a_refusal_not_an_empty_offer(db):
    """A quantity no SKU can cover must read as `refused`, never as a blank sale."""
    env = _envelope(db)
    largest_stock = max(s.available_qty for s in env)

    outcome = proposer.propose(_request(qty=largest_stock + 1), env, generate=None)

    assert outcome.refused
    assert len(outcome.candidates) == 0


# ── the schema does not pre-filter policy ─────────────────────────────────────


def test_a_ninety_percent_discount_parses_cleanly_and_reaches_the_kernel(stocky):
    """A legal-shaped illegal discount must reach the bounds, not die at parse.

    Filtering it here would leave the veto untested — the kernel only proves it
    is in control when the bad proposal actually arrives and is actually refused.
    """
    from vyapaari.schema import parse

    _, sellable = stocky
    candidates = parse(_llm_reply(sellable.sku, discount_pct=90))

    assert candidates[0].base.discount_pct == Decimal("90")
