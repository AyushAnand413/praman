"""MCP server — the same store, spoken to in Model Context Protocol.

Four tools over remote HTTP transport at `/mcp`: `search_products`, `get_offer`,
`buy`, `check_order`. An agent that speaks MCP can transact here without anyone
writing an integration for this store specifically.

This is a wrapper, and it is important that it stays one. Every tool calls the
same Python function the corresponding HTTP endpoint calls, with the same Pydantic
request model — so the field validation, the rate limit, the ledger writes, and
the kernel's veto are shared code rather than a second implementation that has to
be kept in agreement with the first. A tool here cannot accidentally be more
permissive than the endpoint beside it, because there is no separate path for it
to be permissive on.

Two consequences worth stating:

* Tool docstrings are read by a model deciding which tool to call, so they are
  written for that reader. The commentary for whoever maintains this file lives in
  comments like this one.
* Errors are raised, not returned. MCP marks a raised tool call as an error, which
  is what tells a calling agent that its request did not happen. A refusal that
  came back as ordinary content would read like a successful purchase.

The message carries the kernel's own code and, for a policy refusal, the bound
numbers that produced it — enough for an agent to adjust rather than retry blindly.
"""

from __future__ import annotations

from typing import Any

import anyio.to_thread
from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from api import agent as agent_api

SERVER_NAME = "aether-audio"

INSTRUCTIONS = """\
Aether Audio sells audio hardware to autonomous agents.

Browse with `search_products` — it is free and needs no authorisation. Ask for a
price with `get_offer`, which returns one or two options, each with its own total
and a `gate_tier` saying what buying it will require: tier 0 needs nothing, tier 1
needs a signed mandate, tier 2 needs a human to approve it. Buy with `buy`, which
needs the offer id, the option id, and an idempotency key you generate. Poll with
`check_order`.

Prices are whole rupees (INR) and come from the offer, not from you. There is no
field anywhere for you to state a price or a discount; the store decides those and
a policy kernel checks them. Offers expire, so read `expires_in_seconds` and do
not reuse an old offer id.

Every offer carries an `audit_url`. The decision behind it, including anything the
store refused to do, is recorded in a public hash-chained ledger you can read.\
"""


def _unwrap(exc: HTTPException) -> str:
    """An HTTP error as a sentence an agent can act on.

    The endpoints raise `HTTPException` with a structured detail. Flattening it
    here keeps the two surfaces telling the same story: same code, same message,
    same bound numbers, differently framed.
    """
    detail = exc.detail
    if not isinstance(detail, dict):
        return str(detail)
    code = detail.get("code", "error")
    message = detail.get("message", "")
    parts = [f"{code}: {message}" if message else str(code)]
    bounds = detail.get("rejecting_bounds")
    if bounds:
        parts.append(f"(refused by bound {', '.join(str(b) for b in bounds)})")
    if detail.get("retry_after_seconds"):
        parts.append(f"(retry in {detail['retry_after_seconds']}s)")
    return " ".join(parts)


async def _call(fn, *args, **kwargs) -> Any:
    """Run a blocking endpoint handler off the event loop.

    The handlers do synchronous SQLite work, which is exactly what FastAPI already
    does with them — it runs sync endpoints in a worker thread. Doing the same here
    keeps the two surfaces on identical execution paths, and the store's
    connections are thread-local so a worker gets its own.
    """
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except HTTPException as exc:
        raise ToolError(_unwrap(exc)) from exc


def build_server() -> FastMCP:
    """A fresh MCP server, ready to be mounted.

    Built per call rather than as a module singleton: each server owns a session
    manager that may only be started once, so two apps in one process — which is
    what a test suite is — need two of them.
    """
    # Stateless because every tool call here is self-contained: the store's own
    # continuity lives in `session_id` on an offer and in the offer row itself, not
    # in transport state. A stateless server can also be restarted or scaled
    # without an agent losing a conversation it thought it had.
    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
    )

    @mcp.tool(
        name="search_products",
        title="Search the catalog",
        description=(
            "Find products matching a stated need. Free, needs no authorisation, "
            "and returns public product data only: sku, title, list price, stock, "
            "category, attributes, returns window. Use this before asking for an "
            "offer so you can name a base_sku."
        ),
    )
    async def search_products(
        need: str = "",
        budget_inr: int | None = None,
        category: str | None = None,
        limit: int = agent_api.MAX_CATALOG_RESULTS,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        body = agent_api.CatalogRequest(
            need=need,
            budget_inr=budget_inr,
            category=category,
            limit=limit,
            agent_id=agent_id,
        )
        return await _call(agent_api.catalog_query, body)

    @mcp.tool(
        name="get_offer",
        title="Request an offer",
        description=(
            "Ask the store to price a purchase. Returns one or two options, each "
            "with a total, a saving, a reason, and a gate_tier saying what buying "
            "it requires. Also returns a signed policy_receipt and an audit_url. "
            "A session may receive at most two offers, so state the full need in "
            "one call rather than negotiating."
        ),
    )
    async def get_offer(
        need: str,
        agent_id: str,
        session_id: str | None = None,
        qty: int = 1,
        base_sku: str | None = None,
        category: str | None = None,
        budget_inr: int | None = None,
        delivery: str | None = None,
    ) -> dict[str, Any]:
        body = agent_api.OfferRequest(
            need=need,
            agent_id=agent_id,
            session_id=session_id,
            qty=qty,
            base_sku=base_sku,
            category=category,
            budget_inr=budget_inr,
            delivery=delivery,
        )
        return await _call(agent_api.offer, body)

    @mcp.tool(
        name="buy",
        title="Buy an offered option",
        description=(
            "Accept one option from an offer. You supply the offer id, the option "
            "id, and an idempotency_key you generate — retrying with the same key "
            "cannot charge twice, and a key is required. Above the mandate "
            "threshold a signed mandate token is required; above the human "
            "threshold the order is held for merchant approval and you poll it "
            "with check_order. You do not send an amount: the price comes from "
            "the stored offer."
        ),
    )
    async def buy(
        offer_id: str,
        option_id: str,
        agent_id: str,
        idempotency_key: str,
        mandate: str | None = None,
        payment_id: str | None = None,
    ) -> dict[str, Any]:
        body = agent_api.CheckoutRequest(
            offer_id=offer_id,
            option_id=option_id,
            agent_id=agent_id,
            mandate=mandate,
            payment_id=payment_id,
        )
        return await _call(
            agent_api.checkout, body, idempotency_key=idempotency_key
        )

    @mcp.tool(
        name="check_order",
        title="Check an order",
        description=(
            "Poll an order's state. An order held for merchant approval reports "
            "pending_merchant_approval until a human decides; nothing about "
            "polling advances it."
        ),
    )
    async def check_order(order_id: str) -> dict[str, Any]:
        return await _call(agent_api.order_status, order_id)

    return mcp
