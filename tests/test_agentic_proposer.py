"""The agentic proposer: explore with tools, then propose, under hard caps.

Every test here drives the loop with a scripted generator — no network, no
model. What is under test is the loop's contract:

* actions execute against the tools and land in the exploration trail
* a proposal ends the loop successfully
* dead ends are survivable (the model may reformulate)
* the tool-call and wall-clock caps are real
* budget spent without a proposal degrades into the ordinary ladder
* the kill-switch reverts to the one-shot path exactly
"""

from __future__ import annotations

import json

import pytest

import settings
from vyapaari import envelope as envelope_module
from vyapaari import proposer
from vyapaari.prompt import ProposalRequest
from vyapaari.schema import Proposal, ProposedItem, ProposedUpsell
from vyapaari.tools import ExplorationTools, envelope_search_factory


def _envelope():
    from scripts.seed_offer import SCENARIOS_BY_KEY  # noqa: F401 - catalog via conftest

    # Built by the caller's `db` fixture through kernel.offer normally; here we
    # build directly from the seeded catalog cache.
    from store import catalog as catalog_store

    rows = catalog_store.cache.all_public()
    private = {
        row["sku"]: catalog_store.cache.private(row["sku"]) or {} for row in rows
    }
    return envelope_module.build(rows, private)


def _request(**overrides):
    defaults = dict(need="gaming laptop", qty=1)
    defaults.update(overrides)
    return ProposalRequest(**defaults)


def _tools(envelope, recorded=None):
    def search(query: str):
        if recorded is not None:
            recorded.append(("search_catalog", query))
        return envelope_search_factory(envelope)(query)

    def pairings(sku: str):
        if recorded is not None:
            recorded.append(("get_pairings", sku))
        return [{"sku": "AT-CASE-01", "strength": 0.83, "samples": 12}]

    return ExplorationTools(search_catalog=search, get_pairings=pairings)


class ScriptedModel:
    """Replies with queued outputs; records every prompt it was handed."""

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, *, system: str, user: str, response_schema) -> str:
        self.prompts.append(user)
        if not self._replies:
            raise AssertionError("script exhausted — test did not expect this turn")
        return self._replies.pop(0)


def _proposal_json(base="AT-STUDIO-PRO", upsell=None):
    body = {
        "candidates": [
            {
                "base": {"sku": base, "qty": 1, "discount_pct": 0, "why": "Fits the need."},
                "proposed_upsells": [],
            }
        ]
    }
    if upsell:
        body["candidates"][0]["proposed_upsells"].append(
            {
                "type": "bundle_attach",
                "sku": upsell,
                "qty": 1,
                "discount_pct": 0,
                "why": "Pairs nicely.",
            }
        )
    return json.dumps(body)


def test_actions_execute_and_proposal_ends_the_loop(db):
    env = _envelope()
    model = ScriptedModel(
        json.dumps({"action": "search_catalog", "query": "studio headphones"}),
        json.dumps({"action": "get_pairings", "sku": "AT-STUDIO-PRO"}),
        _proposal_json(),
    )
    outcome = proposer.propose_with_tools(
        _request(), env, _tools(env), generate=model
    )

    assert outcome is not None and not outcome.refused
    assert outcome.source == proposer.SOURCE_LLM_AGENT
    kinds = [step["action"] for step in outcome.exploration]
    assert kinds == ["search_catalog", "get_pairings"]
    assert outcome.exploration[0]["results"], "search should have returned hits"


def test_dead_end_is_recoverable(db):
    """Search returns nothing; the agent reformulates instead of giving up."""
    env = _envelope()
    model = ScriptedModel(
        json.dumps({"action": "search_catalog", "query": "zzzz nonexistent"}),
        json.dumps({"action": "search_catalog", "query": "headphones"}),
        _proposal_json(),
    )
    outcome = proposer.propose_with_tools(_request(), env, _tools(env), generate=model)

    assert not outcome.refused
    first_results = outcome.exploration[0]["results"]
    assert first_results == []  # the dead end, honestly empty
    assert len(outcome.exploration[1]["results"]) > 0


def test_tool_call_cap_stops_a_runaway_agent(db):
    """More action replies than allowed: the loop stops and reports."""
    env = _envelope()
    endless = [
        json.dumps({"action": "search_catalog", "query": f"query {i}"})
        for i in range(settings.AGENT_MAX_TOOL_CALLS + 3)
    ]
    model = ScriptedModel(*endless)
    outcome = proposer.propose_with_tools(_request(), env, _tools(env), generate=model)

    assert outcome.refused
    assert len(outcome.exploration) == settings.AGENT_MAX_TOOL_CALLS
    assert any("without a proposal" in e or "time" in e for e in outcome.errors)


def test_budget_exhaustion_degrades_into_the_fallback_ladder(db, monkeypatch):
    """propose() falls back to the deterministic offer when exploration fails."""
    env = _envelope()
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_CALLS", 1)
    endless = ScriptedModel(
        json.dumps({"action": "search_catalog", "query": "a"}),
        json.dumps({"action": "search_catalog", "query": "b"}),
        # The degraded one-shot ladder gets these two: malformed twice, so it
        # exhausts its single retry and lands on the deterministic fallback.
        "not json at all",
        "still not json",
    )
    outcome = proposer.propose(
        _request(base_sku="AT-CBL-USBC"),
        env,
        generate=endless,
        tools=_tools(env),
    )
    assert outcome.source == proposer.SOURCE_FALLBACK
    assert not outcome.refused
    assert outcome.candidates[0].base.sku == "AT-CBL-USBC"
    # The spent exploration stays visible in the outcome payload.
    payload = outcome.as_payload()
    assert any("agentic" in e for e in payload["schema_errors"])


def test_wall_clock_cap_ends_the_exploration(db, monkeypatch):
    env = _envelope()
    ticks = {"value": 0.0}

    def slow_clock():
        ticks["value"] += 1.0
        return ticks["value"]

    monkeypatch.setattr(settings, "AGENT_WALL_CLOCK_SECONDS", 2.0)
    model = ScriptedModel(
        *[json.dumps({"action": "search_catalog", "query": f"q{i}"}) for i in range(10)]
    )
    outcome = proposer.propose_with_tools(
        _request(), env, _tools(env), generate=model, clock=slow_clock
    )
    # Each turn advances the fake clock by 1s; the 2s cap must bite well before
    # ten turns could run.
    assert outcome.refused
    assert len(outcome.exploration) < settings.AGENT_MAX_TOOL_CALLS + 1


def test_malformed_turn_consumes_budget_but_does_not_crash(db):
    env = _envelope()
    model = ScriptedModel(
        "I am just going to write some prose instead of JSON.",
        _proposal_json(),
    )
    outcome = proposer.propose_with_tools(_request(), env, _tools(env), generate=model)
    assert not outcome.refused
    assert any("neither" in e for e in outcome.errors)


def test_kill_switch_reverts_to_one_shot_behaviour(db, monkeypatch):
    env = _envelope()
    monkeypatch.setattr(settings, "PROPOSER_TOOLS_ENABLED", False)

    one_shot_reply = json.dumps(
        {
            "candidates": [
                {
                    "base": {"sku": "AT-CBL-USBC", "qty": 1, "discount_pct": 0,
                             "why": "Matches what you asked for."},
                    "proposed_upsells": [],
                }
            ]
        }
    )
    model = ScriptedModel(one_shot_reply)
    outcome = proposer.propose(_request(), env, generate=model, tools=_tools(env))

    # One call only: no exploration phase ran at all.
    assert len(model.prompts) == 1
    assert outcome.source == proposer.SOURCE_LLM
    assert outcome.exploration == ()


def test_exploration_travels_into_the_ledger_payload(db):
    env = _envelope()
    model = ScriptedModel(
        json.dumps({"action": "get_pairings", "sku": "AT-STUDIO-PRO"}),
        _proposal_json(),
    )
    outcome = proposer.propose_with_tools(_request(), env, _tools(env), generate=model)
    payload = outcome.as_payload()
    assert payload["source"] == "llm_agent"
    assert payload["exploration"][0]["action"] == "get_pairings"


def test_no_secrets_reach_the_agent_prompt(db, monkeypatch):
    secret = "super-secret-agent-value-123"
    monkeypatch.setenv("POLICY_RECEIPT_HMAC_SECRET", secret)
    env = _envelope()
    model = ScriptedModel(_proposal_json())

    def guarded_generate(*, system, user, response_schema):
        assert secret not in system
        assert secret not in user
        return _proposal_json()

    proposer.propose_with_tools(
        _request(), env, _tools(env), generate=guarded_generate
    )


def test_empty_query_is_an_honest_nothing(db):
    """A blank search is answered plainly, not with a padded shelf."""
    search = envelope_search_factory(_envelope())
    assert search("") == []
    assert search("   ") == []
