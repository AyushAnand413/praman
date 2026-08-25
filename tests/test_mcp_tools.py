"""The MCP tools, driven the way a real client drives them.

The MCP server is a wrapper, and these tests hold it to that: every tool must
reach the store through the same handler functions and request models the HTTP
endpoints use, which means validation, rate limiting, ledger writes, and the
kernel's veto are all in force on this surface too.

Two properties get their own proof here:

* Errors are **raised**, never returned as content. A refusal delivered as
  ordinary tool output reads to a calling agent like a successful purchase;
  a raised ToolError reads as the failure it is.
* A purchase through `buy` completes end to end — offer, checkout, order poll —
  in shadow policy mode, with a forbidden gateway standing witness that no
  payment call happened.
"""

from __future__ import annotations

import json

import pytest

from api.mcp import SERVER_NAME, build_server
from kernel import approvals as approvals_kernel
from kernel import checkout as checkout_kernel

TOOL_NAMES = {"search_products", "get_offer", "buy", "check_order"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    """The deterministic fallback proposes, so no network exists anywhere."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture
def forbidden_gateway(monkeypatch, forbidden_razorpay):
    """Any payment call fails the test; shadow mode must make none."""
    monkeypatch.setattr(checkout_kernel, "_default_client", lambda: forbidden_razorpay)
    monkeypatch.setattr(approvals_kernel, "_default_client", lambda: forbidden_razorpay)


@pytest.fixture
def mcp():
    return build_server()


async def _call_tool(mcp, name: str, arguments: dict):
    """Invoke one tool and normalise across SDK result shapes.

    Newer SDKs return `(content_blocks, structured)`; older ones return the
    structured payload directly, and json_response mode wraps text blocks in
    JSON strings. Callers want the dict, whichever shape arrived.
    """
    result = await mcp.call_tool(name, arguments)
    if isinstance(result, tuple):
        result = result[-1]
    if isinstance(result, list):
        texts = [getattr(block, "text", "") for block in result]
        joined = "".join(texts)
        return json.loads(joined) if joined else {}
    if isinstance(result, str):
        return json.loads(result)
    return result


# ── the surface ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_exactly_the_four_store_tools_are_registered(mcp):
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == TOOL_NAMES
    assert mcp.name == SERVER_NAME


@pytest.mark.anyio
async def test_search_products_returns_public_fields_only(db, mcp):
    body = await _call_tool(mcp, "search_products", {"need": "earbuds"})

    assert body["count"] >= 1
    private_markers = {"cost_inr", "floor_price_inr", "margin_pct", "max_discount_pct"}
    for row in body["results"]:
        leaked = private_markers & set(row)
        assert not leaked, f"private fields crossed the wrapper: {leaked}"


# ── a purchase through the wrapper ────────────────────────────────────────────


@pytest.mark.anyio
async def test_an_agent_can_buy_through_mcp_end_to_end(db, forbidden_gateway, mcp):
    search = await _call_tool(mcp, "search_products", {"need": "usb-c cable"})
    assert search["count"] >= 1
    base_sku = min(search["results"], key=lambda r: int(r["list_price_inr"]))["sku"]

    offer = await _call_tool(
        mcp,
        "get_offer",
        {
            "need": "a usb-c cable for my desk",
            "agent_id": "agent-mcp-test",
            "base_sku": base_sku,
        },
    )
    option = next(
        o for o in offer["options"] if o["option_id"] == offer["recommended_option_id"]
    )
    assert option["gate"]["gate_tier"] == 0, "scenario must stay inside the free tier"
    assert offer["policy_mode"] == "shadow"

    purchase = await _call_tool(
        mcp,
        "buy",
        {
            "offer_id": offer["offer_id"],
            "option_id": option["option_id"],
            "agent_id": "agent-mcp-test",
            "idempotency_key": "mcp-test-key-0001",
        },
    )

    assert purchase["order_id"].startswith("ORD-")
    assert int(purchase["amount_inr"]) == int(option["total_inr"])
    assert purchase["policy_mode"] == "shadow"

    polled = await _call_tool(mcp, "check_order", {"order_id": purchase["order_id"]})
    assert polled["order_id"] == purchase["order_id"]


@pytest.mark.anyio
async def test_buying_the_same_option_twice_with_one_key_replays(db, forbidden_gateway, mcp):
    """Idempotency holds on this surface because it is the same kernel code."""
    offer = await _call_tool(
        mcp,
        "get_offer",
        {"need": "usb-c cable", "agent_id": "agent-mcp-idem"},
    )
    option = offer["options"][0]

    first = await _call_tool(
        mcp,
        "buy",
        {
            "offer_id": offer["offer_id"],
            "option_id": option["option_id"],
            "agent_id": "agent-mcp-idem",
            "idempotency_key": "mcp-idem-key-0001",
        },
    )
    replay = await _call_tool(
        mcp,
        "buy",
        {
            "offer_id": offer["offer_id"],
            "option_id": option["option_id"],
            "agent_id": "agent-mcp-idem",
            "idempotency_key": "mcp-idem-key-0001",
        },
    )

    assert first["order_id"] == replay["order_id"]
    assert replay.get("status") == "replayed"
    assert first.get("status") != "replayed"


# ── refusals raise, they are never returned as content ────────────────────────


@pytest.mark.anyio
async def test_an_unknown_offer_is_a_raised_error_not_a_payload(db, mcp):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as caught:
        await _call_tool(
            mcp,
            "buy",
            {
                "offer_id": "OF-does-not-exist",
                "option_id": "A",
                "agent_id": "agent-mcp-err",
                "idempotency_key": "mcp-err-key-0001",
            },
        )

    # The code travels in the message so the calling agent can branch on it.
    assert "offer_not_found" in str(caught.value) or "not found" in str(caught.value)


@pytest.mark.anyio
async def test_a_caller_cannot_smuggle_an_amount_field_into_buy(db, mcp):
    """There is no path by which a caller states a price, even here.

    The wrapper's tool signature names its parameters; anything else an MCP
    client sends is not forwarded to the request model at all. The attempt must
    change nothing: the call fails for the reason it would have failed anyway
    (unknown offer), never because a supplied amount was honoured.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as caught:
        await _call_tool(
            mcp,
            "buy",
            {
                "offer_id": "OF-whatever",
                "option_id": "A",
                "agent_id": "agent-mcp-err",
                "idempotency_key": "mcp-err-key-0002",
                "amount_inr": 1,
            },
        )

    assert "offer_not_found" in str(caught.value)
    assert "amount_inr" not in str(caught.value) or "unknown" in str(caught.value)


# ── error framing shared with HTTP ────────────────────────────────────────────


def test_http_refusals_unwrap_into_sentences_that_carry_the_bounds():
    from fastapi import HTTPException

    from api.mcp import _unwrap

    message = _unwrap(
        HTTPException(
            status_code=409,
            detail={
                "code": "policy_refused",
                "message": "the base item was refused",
                "rejecting_bounds": [1, 3],
            },
        )
    )
    assert "policy_refused" in message
    assert "bound 1, 3" in message

    retry = _unwrap(
        HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "slow down",
                "retry_after_seconds": 7,
            },
        )
    )
    assert "7" in retry
