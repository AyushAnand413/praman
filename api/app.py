"""FastAPI application factory.

The app wires the endpoints an agent transacts through: discovery, audit, the
catalog and offer surface, the checkout rail, the merchant approval queue, the
Razorpay webhook, and the MCP server at `/mcp`. The order they were built in is
the point — the audit trail and the money rail are load-bearing before anything is
allowed to be clever.

Run locally:
    python -m uvicorn api.app:app --reload
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from settings import (
    DASHBOARD_ORIGIN,
    MERCHANT_NAME,
    POLICY_MODE,
    SECRET_ENV_VARS,
)
from api import agent, approvals, audit, auth, dashboard, demo, manifest, ops, orders, policy, stores, webhooks
from api import mcp as mcp_module
from kernel import checkout as checkout_kernel
from mandate.issuers import DEMO_ISSUER_ID, bootstrap_demo_issuer
from mandate.keys import MandateKeyError
from store import catalog, ledger
from store.db import get_connection, init_db, journal_mode

log = logging.getLogger("aether")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the schema, seed the catalog, warm the in-memory cache.

    The catalog belongs in memory before the first request, not on the first
    request — pre-warm so that the first call is not the slowest. The trusted
    issuer registry gets the same treatment for the same reason, except there the
    cost of a cold start is not latency but a wrong answer.
    """
    conn = get_connection()
    init_db(conn)
    count = catalog.seed_database(conn=conn)
    catalog.cache.load(conn)

    # Establish the chain on a fresh database so there is always a genesis
    # entry to link from. Only on an empty ledger — not once per boot.
    if ledger.tip(conn)[0] == 0:
        ledger.append(
            "system",
            "ledger.genesis",
            {"merchant": MERCHANT_NAME, "catalog_skus": count},
            conn=conn,
        )

    # The trusted-issuer registry lives in process memory, so it has to be
    # populated before the first request that presents a mandate. Left empty, a
    # perfectly good mandate is rejected as UNKNOWN_ISSUER — and that code
    # escalates to a human rather than refusing, so the failure would look like a
    # policy decision instead of a misconfiguration.
    try:
        public_hex = bootstrap_demo_issuer()
        log.info("trusted issuer | %s | key=%s", DEMO_ISSUER_ID, public_hex[:16])
    except MandateKeyError as exc:
        # Only a malformed MANDATE_SIGNING_SEED reaches here; an absent one gets
        # an ephemeral key. Warn rather than fail, consistent with the secrets
        # policy below: no registered issuer means every mandate is untrusted,
        # which refuses purchases instead of authorising them.
        log.warning("demo mandate issuer not registered: %s", exc)

    # Reservations from abandoned two-step checkouts hold discount budget that
    # nothing else will ever release. Sweep at boot so a restart cannot inherit
    # yesterday's phantom spend.
    # Seed test merchant for dashboard sign-in (auth page). Keeps frontend design intact.
    try:
        from store.auth import create_merchant, get_by_email
        if not get_by_email("merchant@aether.test", "default"):
            create_merchant(email="merchant@aether.test", password="praman123", store_id="default")
            log.info("seeded test merchant merchant@aether.test / praman123 (store default)")
        if not get_by_email("merchant@voltmart.test", "voltmart"):
            try:
                create_merchant(email="merchant@voltmart.test", password="praman123", store_id="voltmart")
            except Exception:
                pass
    except Exception as e:
        log.warning("auth seed skipped: %s", e)

    released = checkout_kernel.expire_abandoned()
    if released:
        log.info("released %d abandoned checkout reservation(s)", len(released))

    missing = [name for name in SECRET_ENV_VARS if not os.environ.get(name)]
    if missing:
        # A warning rather than a failure, because a missing secret disables a
        # path instead of corrupting one: each endpoint that needs a credential
        # refuses at call time with a 503 naming the variable. Booting lets the
        # catalog, audit, and shadow-mode paths still work without any of them.
        log.warning("secrets not set: %s", ", ".join(missing))

    log.info(
        "ready | POLICY_MODE=%s | %d SKUs cached | journal_mode=%s",
        POLICY_MODE.value, len(catalog.cache), journal_mode(conn),
    )

    # The MCP transport keeps its own session manager, which has to be running
    # before `/mcp` will answer and stopped when the app shuts down. Wrapping the
    # yield rather than starting it separately means its lifetime is exactly the
    # app's, with no window where a mounted route exists but cannot serve.
    async with app.state.mcp_server.session_manager.run():
        yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{MERCHANT_NAME} — agent commerce",
        version="0.1.0",
        description=(
            "An agentic storefront: an LLM sells, a deterministic kernel holds "
            "veto power, and every money action is hash-chained and public."
        ),
        lifespan=lifespan,
    )

    # Dashboard origin only. Agent clients are server-side and do not need
    # CORS; a wildcard here would exist purely to be abused.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[DASHBOARD_ORIGIN],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Merchant-Key",
            "X-Demo-Key",
            "Idempotency-Key",
            "X-Razorpay-Signature",
            "X-Store-Id",
        ],
    )

    app.include_router(manifest.router)
    app.include_router(audit.router)
    app.include_router(agent.router)
    app.include_router(approvals.router)
    app.include_router(webhooks.router)
    app.include_router(demo.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(ops.router)
    app.include_router(orders.router)
    app.include_router(stores.router)
    app.include_router(policy.router)

    # The merchant panel: a static single-page console served by this same
    # process, so a demo is "open the browser" and nothing else. It speaks to
    # the exact same authenticated JSON endpoints an external client would —
    # there is no second, friendlier API behind it.
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path

    panel_dir = Path(__file__).resolve().parent.parent / "public" / "panel"
    if panel_dir.exists():
        app.mount("/panel", StaticFiles(directory=panel_dir, html=True), name="panel")

    # One MCP server per app, held on app.state so the lifespan can start its
    # session manager. A module-level singleton would be shared between two apps
    # in the same process — which is what a test suite is — and a session manager
    # may only be started once.
    #
    # Mounted at /mcp with the server's own path set to "/", rather than mounting
    # at "/" and letting the server route /mcp itself. A mount at the root would
    # sit in front of every route above it.
    app.state.mcp_server = mcp_module.build_server()
    app.mount("/mcp", app.state.mcp_server.streamable_http_app(), name="mcp")

    @app.get("/health", tags=["ops"], summary="Liveness + mode")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "policy_mode": POLICY_MODE.value,
            "catalog_skus": len(catalog.cache),
            "ledger_head_seq": ledger.tip()[0],
        }

    return app


app = create_app()
