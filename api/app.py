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
from api import agent, approvals, audit, auth, dashboard, demo, manifest, ops, orders, policy, recommendations, stores, webhooks
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

    Serverless note: Neon free sleeps after 5 min idle. The first request after
    sleep must wake the DB (5-10s) and still finish before Vercel's
    FUNCTION_INVOCATION_TIMEOUT (10s hobby / 30s pro). Every DB step here is
    therefore guarded — a sleeping DB must not crash the function, health must
    still answer, and the catalog must lazy-load on first real request.
    """
    db_ready = False
    count = 0
    try:
        conn = get_connection()
        init_db(conn)
        count = catalog.seed_database(conn=conn)
        catalog.cache.load(conn)
        db_ready = True

        # Establish the chain on a fresh database so there is always a genesis
        # entry to link from. Only on an empty ledger — not once per boot.
        if ledger.tip(conn)[0] == 0:
            ledger.append(
                "system",
                "ledger.genesis",
                {"merchant": MERCHANT_NAME, "catalog_skus": count},
                conn=conn,
            )
    except Exception as exc:
        # Neon sleeping / cold-start wake — don't crash the serverless function.
        # /health will report db: degraded, next request will retry.
        log.warning("db init deferred (will retry on next request): %s", exc)

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

    # Seed test merchant for dashboard sign-in (auth page). Keeps frontend design intact.
    if db_ready:
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

        try:
            released = checkout_kernel.expire_abandoned()
            if released:
                log.info("released %d abandoned checkout reservation(s)", len(released))
        except Exception as e:
            log.warning("expire_abandoned skipped (db wake): %s", e)

    missing = [name for name in SECRET_ENV_VARS if not os.environ.get(name)]
    if missing:
        # A warning rather than a failure, because a missing secret disables a
        # path instead of corrupting one: each endpoint that needs a credential
        # refuses at call time with a 503 naming the variable. Booting lets the
        # catalog, audit, and shadow-mode paths still work without any of them.
        log.warning("secrets not set: %s", ", ".join(missing))

    try:
        jm = journal_mode(get_connection()) if db_ready else "deferred"
    except Exception:
        jm = "deferred"
    if db_ready:
        try:
            from kernel.recommender import seed_pairings_from_catalog
            n_seeded = seed_pairings_from_catalog()
            log.info("recommender | cold-start seeded %d pairs", n_seeded)
        except Exception as exc:
            log.warning("recommender | could not seed pairings: %s", exc)

    log.info(
        "ready | POLICY_MODE=%s | %d SKUs cached | journal_mode=%s | db_ready=%s",
        POLICY_MODE.value, len(catalog.cache), jm, db_ready,
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

    # Dashboard origin + preview. Agent clients are server-side and do not need
    # CORS; a wildcard would be abused. Also allow Vercel preview suffix.
    origins = [o for o in [DASHBOARD_ORIGIN, "https://praman-seven.vercel.app"] if o]
    # allow preview deployments too (praman-xxx.vercel.app)
    if DASHBOARD_ORIGIN and "vercel.app" not in DASHBOARD_ORIGIN:
        origins.append("https://praman-seven.vercel.app")
    origins = list(dict.fromkeys(origins))  # dedupe
    # also allow any praman preview via regex — CORSMiddleware doesn't support regex, so we allow all vercel preview via setting allow_origin_regex
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=r"https://praman.*\.vercel\.app",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
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
    app.include_router(recommendations.router)

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
    # SSE fallback for clients like mcp-remote that fail on Vercel's chunked streamable HTTP
    try:
        app.mount("/mcp-sse", app.state.mcp_server.sse_app(), name="mcp-sse")
    except Exception:
        pass

    @app.get("/health", tags=["ops"], summary="Liveness + mode")
    def health() -> dict[str, Any]:
        # Health must never wait for a sleeping DB — Vercel kills the function
        # at 10s and the caller sees 504. Return degraded instead of hanging.
        # Use a short statement timeout so health never blocks 15s on Supabase pooler.
        try:
            from store.db import get_connection, reset_connection
            conn = get_connection()
            try:
                conn.execute("SET LOCAL statement_timeout = '2000'")
            except Exception:
                pass
            head = ledger.tip(conn)[0]
            db = "ok"
        except Exception as exc:
            head = None
            db = f"degraded: {exc.__class__.__name__}"
            log.warning("health db degraded: %s", exc)
            # Rollback + drop thread-local connection so next request gets a
            # fresh one. Without this, InFailedSqlTransaction persists for the
            # lifetime of the serverless container.
            try:
                from store.db import get_connection as _gc, reset_connection as _rc
                _gc()._pg.rollback()
            except Exception:
                pass
            try:
                from store.db import reset_connection as _reset
                _reset()
            except Exception:
                pass
        try:
            catalog.cache.load()
        except Exception:
            pass
        body: dict[str, Any] = {
            "status": "ok" if db == "ok" else "degraded",
            "policy_mode": POLICY_MODE.value,
            "catalog_skus": len(catalog.cache),
            "ledger_head_seq": head,
            "db": db,
        }
        return body

    return app


app = create_app()
