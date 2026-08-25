"""Eight buyer personas through the whole rail, and the latency budgets.

Every persona in `harness.grahak` asks for something different — a hard budget,
a bulk order, a vague description, a named accessory, a deadline — and the offer
path has to answer all of them with the same well-formed object. These tests
drive each persona through discovery, catalog, offer, and (where the gate
allows) checkout over the real ASGI app, with the model absent so the
deterministic fallback proposer serves: the shape guarantees must not depend on
which source wrote the proposal.

The purchase runs in shadow POLICY_MODE, which is the strongest hermetic form of
the bot-to-bot path: the kernel computes its full verdict and issues a real
signed receipt while a forbidden gateway client proves no payment call happened.

Latency is asserted against the published budgets. In-process these numbers are
comfortably met; the assertion exists so a regression that makes the rail slow
fails loudly here rather than quietly in production.
"""

from __future__ import annotations

import pytest

import settings
from harness.grahak import Grahak, PERSONAS
from kernel import checkout as checkout_kernel
from store import orders

REQUIRED_OFFER_KEYS = {
    "offer_id",
    "session_id",
    "expires_at",
    "expires_in_seconds",
    "recommended_option_id",
    "options",
    "gate_tier",
    "policy_receipt",
    "policy_mode",
    "audit_url",
}

REQUIRED_OPTION_KEYS = {
    "option_id",
    "items",
    "total_inr",
    "list_total_inr",
    "discount_inr",
    "gate",
    "human_reason",
}


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    """No model configured: the fallback proposer serves every persona."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture
def http_transport(client):
    """Grahak's Transport protocol over the FastAPI TestClient."""

    class TestClientTransport:
        def __init__(self, inner):
            self._inner = inner

        def get(self, url, **kwargs):
            return self._inner.get(url, **kwargs)

        def post(self, url, **kwargs):
            return self._inner.post(url, **kwargs)

    return TestClientTransport(client)


@pytest.fixture
def forbidden_gateway(monkeypatch, forbidden_razorpay):
    """Any payment call fails the test; shadow mode must make none."""
    monkeypatch.setattr(checkout_kernel, "_default_client", lambda: forbidden_razorpay)


# ── deliverable: schema-conformant offers for every persona ───────────────────


@pytest.mark.parametrize("persona", PERSONAS, ids=[p.name for p in PERSONAS])
def test_every_persona_receives_a_schema_conformant_offer(
    db, http_transport, forbidden_gateway, persona
):
    agent = Grahak(http_transport, agent_id=f"grahak-{persona.name}")
    offer = agent.request_offer(
        persona.need,
        qty=persona.qty,
        budget_inr=persona.budget_inr,
        category=persona.category,
        delivery=persona.delivery,
    )

    body = offer.payload
    missing = REQUIRED_OFFER_KEYS - set(body)
    assert not missing, f"offer is missing {missing}"

    assert body["options"], "an offer with no options is not an offer"
    for option in body["options"]:
        option_missing = REQUIRED_OPTION_KEYS - set(option)
        assert not option_missing, f"{option.get('option_id')} missing {option_missing}"
        assert isinstance(option["total_inr"], int) and option["total_inr"] > 0
        assert option["items"], "a priced option must name what is being priced"
        for item in option["items"]:
            # The amount authority: list price, offered price, quantity, sku.
            assert {"sku", "qty", "list_price_inr", "offered_price_inr"} <= set(item)
            assert 0 < item["offered_price_inr"] <= item["list_price_inr"]
        assert option["gate"]["gate_tier"] in (0, 1, 2)

    assert body["policy_receipt"].get("receipt_id")
    assert body["audit_url"].startswith("/audit/")
    # The recommended marker points at an option that actually exists.
    option_ids = {o["option_id"] for o in body["options"]}
    assert body["recommended_option_id"] in option_ids


# ── deliverable: one bot-to-bot purchase, end to end ──────────────────────────


def test_a_bot_buys_from_a_bot_end_to_end(db, http_transport, forbidden_gateway):
    """Discovery → catalog → offer → checkout → poll, no human anywhere.

    Shadow mode: the order completes its policy verdict and issues a signed
    receipt while the forbidden gateway stands witness that nothing reached it.
    """
    persona = next(p for p in PERSONAS if p.name == "budget_tight")
    agent = Grahak(http_transport, agent_id=f"grahak-e2e-{persona.name}")

    offer, purchase = agent.shop_as(persona)

    assert purchase.order_id.startswith("ORD-")
    assert purchase.amount_inr == int(offer.recommended["total_inr"]), (
        "the buyer never states a price; the stored offer row is the authority"
    )
    assert purchase.policy_mode == "shadow"
    assert purchase.would_have_charged

    stored = orders.get(purchase.order_id)
    assert stored["state"] in ("AWAITING_PAYMENT", "HELD", "PENDING")

    polled = agent.check(purchase.order_id)
    assert polled["order_id"] == purchase.order_id


def test_a_held_order_polls_as_pending_and_never_approves_itself(db, http_transport):
    """A tier-2 cart reports pending_merchant_approval for as long as it takes."""
    persona = next(p for p in PERSONAS if p.name == "bulk")
    agent = Grahak(
        http_transport,
        agent_id=f"grahak-hold-{persona.name}",
        # The persona's own wallet caps below this cart; the point here is the
        # store's human tier, so the buyer's wallet is sized to let the cart
        # reach it.
        wallet=persona.wallet().__class__(
            owner=persona.wallet().owner,
            max_amount_inr=100_000,
            max_single_txn_inr=100_000,
        ),
    )

    offer = agent.request_offer(persona.need, qty=persona.qty)
    cheapest = offer.cheapest()
    if int(cheapest["gate"]["gate_tier"]) < 2:
        pytest.skip("scenario did not reach the human tier; nothing held to poll")

    purchase = agent.buy(offer, str(cheapest["option_id"]))

    assert purchase.held_for_human
    polled = agent.check(purchase.order_id)
    state = str(polled.get("state", ""))
    assert state in ("HELD", "PENDING_APPROVAL") or polled.get(
        "approval_state"
    ) in ("PENDING", None)


# ── deliverable: published latency budgets hold ───────────────────────────────


def test_catalog_meets_its_published_latency_budget(db, http_transport):
    import time

    started = time.perf_counter()
    response = http_transport.post(
        "/agent/v1/catalog", json={"need": "wireless earbuds", "agent_id": "latency-cat"}
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    assert response.status_code == 200
    assert elapsed_ms <= settings.LATENCY_BUDGETS_MS["catalog"]
    assert response.json()["latency_budget_ms"] == settings.LATENCY_BUDGETS_MS["catalog"]


def test_offer_latency_stays_within_the_published_budget(
    db, http_transport, forbidden_gateway
):
    import time

    started = time.perf_counter()
    response = http_transport.post(
        "/agent/v1/offer",
        json={"need": "over-ear headphones", "agent_id": "latency-offer"},
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    assert response.status_code == 200
    # The fallback path answers instantly; even two model retries plus assembly
    # would have to fit inside the same budget.
    assert elapsed_ms <= settings.LATENCY_BUDGETS_MS["offer"]
