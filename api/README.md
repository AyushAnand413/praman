# PRAMAN API Layer (`api/`)

> **Simple:** This is the front door. AI buyer agents, human merchants, Shopify, and public auditors all talk to PRAMAN through here. It takes their HTTP requests, checks them, and calls the right internal component. Nothing financial happens without passing through `kernel/`.

The `api` package serves as the primary HTTP and Model Context Protocol (MCP) surface for the **PRAMAN / Aether** agent commerce platform. It coordinates the interactions between autonomous buyer agents, human merchants, payment gateways (Razorpay), external store integrations (Shopify), and public auditors.

## Simple — what each file does & who it calls

| File | Plain-English job | Calls / Connected to |
|---|---|---|
| `app.py` | Starts the server, creates DB tables, warms catalog cache, mounts all routes | `store/db.py`, `store/catalog.py`, `store/ledger.py`, `mandate/issuers.py` |
| `agent.py` | Buyer shopping rail: search → get offer → checkout → poll order | `kernel/offer.py`, `kernel/checkout.py`, `kernel/search.py`, `store/*` |
| `approvals.py` | Human approval queue (Tier-2) — approve/reject/counter | `kernel/approvals.py`, `store/approvals.py`, `store/ledger.py` |
| `audit.py` | Public ledger explorer (anyone can verify chain) | `store/ledger.py` |
| `dashboard.py` | Merges metrics + approvals + feed + bounds for merchant UI | `store/*`, `kernel/bounds.py`, `store/ledger.py` |
| `demo.py` | `POST /demo/force_oversell` — triggers oversell saga deterministically | `kernel/saga.py`, `kernel/stock.py` |
| `manifest.py` | `/.well-known/agent-commerce.json` — what agents discover (static, <50ms) | `settings.py` |
| `mcp.py` | MCP tools (`search_products`, `get_offer`, `buy`, `check_order`) — same logic as HTTP | `api/agent.py` handlers (shared models) |
| `ops.py` | `POST /shopify/sync` — merchant imports Shopify catalog | `integrations/shopify.py`, `store/catalog.py` |
| `ratelimit.py` | 60 req/60s per agent/IP on `/catalog` (in-memory) | `api/agent.py` |
| `webhooks.py` | Razorpay callbacks (`payment.authorized/captured/failed`, `refund.processed`) | `kernel/payments.py` (HMAC), `store/orders.py`, `store/ledger.py` |
| `static/panel/*` | Merchant browser console (HTML/CSS/JS) | `api/dashboard.py`, `api/approvals.py` |

---

## 🏛️ Core Principles & Architecture

1. **Deterministic Kernel Veto**: While LLMs can propose offers and upsells, the deterministic policy kernel retains final veto power over all pricing, discounts, stock, and transaction limits.
2. **Dual Client Surfaces (HTTP & MCP)**: External autonomous agents can interact either via standard REST endpoints (`/agent/v1/*`) or via the Model Context Protocol server mounted at `/mcp`. Both surfaces execute the exact same underlying validation models, rate limits, and kernel checks.
3. **Public & Tamper-Evident Audit Trail**: Every significant business event (queries, offers, checkouts, holds, compensations, webhook events) is recorded in an append-only SHA-256 hash-chained ledger exposed publicly at `/audit`.
4. **Data Isolation & Single Serializer**: Private product costs and sensitive internal flags never leak over the network. Product payloads leaving the API pass through strict sanitizing serializers (`to_public`).
5. **Fail-Closed Security**: Missing environment secrets or unauthorized calls fail closed (returning `503 Service Unavailable` or `401 Unauthorized`) to prevent accidental unauthorized monetary transactions.

---

## 📁 File-by-File Breakdown

### 1. `__init__.py`
- **Purpose**: Package initialization and architecture declaration for the HTTP surface.
- **Key Details**: Documents the single DB-to-HTTP serialization rule ensuring no private store data leaves the system unvetted.

### 2. `app.py`
- **Purpose**: FastAPI application factory and lifespan lifecycle management.
- **Key Details**:
  - `lifespan(app)`: Initializes the database schema, pre-warms the in-memory catalog cache, seeds the genesis block of the ledger, registers the trusted demo mandate issuer, purges stale/abandoned checkout reservations, validates required secrets, and manages the MCP server session lifecycle.
  - `create_app()`: Configures CORS, mounts all sub-routers, mounts the static merchant console UI (`/panel`), mounts the streamable MCP server (`/mcp`), and exposes the `/health` endpoint for ops liveness checks.

### 3. `agent.py`
- **Purpose**: The autonomous buyer agent's transactional rail.
- **Endpoints**:
  - `POST /agent/v1/catalog`: Semantic/keyword catalog discovery with budget and category filtering. Free to call and protected by rate-limiting.
  - `POST /agent/v1/offer`: Requests a pricing offer for a stated buyer need. Evaluated by the model, bounded by the policy kernel, and signed with a cryptographic policy receipt.
  - `POST /agent/v1/checkout`: Initiates checkout for a specific offer option. Enforces required `Idempotency-Key` headers, verifies mandate tokens above threshold amounts, and prevents price tampering by omitting client-controlled amount fields.
  - `POST /agent/v1/order/{order_id}/settle`: Captures completed gateway payments.
  - `GET /agent/v1/order/{order_id}`: Polling endpoint for checking order state, especially orders held in Tier-2 merchant approval queues.

### 4. `approvals.py`
- **Purpose**: Merchant human-in-the-loop (Tier-2 gate) approval workflow.
- **Endpoints**:
  - `GET /merchant/v1/approvals`: Retrieves the list of orders held for manual merchant review.
  - `POST /merchant/v1/approvals/{approval_id}/approve`: Approves and releases a held order to proceed with payment and fulfillment.
  - `POST /merchant/v1/approvals/{approval_id}/reject`: Voids a held order.
  - `POST /merchant/v1/approvals/{approval_id}/counter`: Voids the original order and issues a counter-offer with revised pricing terms.
- **Key Details**: All routes are protected by constant-time verification of the `X-Merchant-Key` header. Decisions are immutably attributed in the audit ledger.

### 5. `audit.py`
- **Purpose**: Public, unauthenticated verification of the system ledger and transaction trail.
- **Endpoints**:
  - `GET /audit/verify`: Recomputes and verifies the integrity of the entire SHA-256 ledger hash chain, identifying any tampering or broken links.
  - `GET /audit/{ref}`: Retrieves a single ledger entry by its sequence number, or the complete chronological audit trail for an entity (`ORD-...`, `OF-...`, `SES-...`, `APV-...`).

### 6. `dashboard.py`
- **Purpose**: Comprehensive merchant observability and analytics aggregation.
- **Endpoint**:
  - `GET /merchant/v1/dashboard`: Aggregates real-time business and safety metrics into a single response, including:
    - **Mode Banner**: Explicit live vs. shadow mode indicator.
    - **Business Metrics**: Daily order volume, net revenue, AOV, upsell revenue, discount budget consumption, and gross margin per discounted rupee.
    - **Approvals Queue**: Active pending human approval requests.
    - **Live Feed**: Recent audit ledger events.
    - **Bounds Panel**: Status of all 10 standing safety bounds.
    - **Safety Panel**: Counts of refused proposals, dropped upsell lines, checkout rejections, saga compensations, and declined payments.
    - **Hash Chain Status**: Head sequence and chain integrity state.

### 7. `demo.py`
- **Purpose**: Deterministic demonstration and testing harness for system safety invariants.
- **Endpoint**:
  - `POST /demo/force_oversell`: Simulates a race condition by modifying stock mid-flight during a live checkout. Verifies that the autonomous saga triggers compensation, executes automatic Razorpay refunds, void-heals the SKU, and logs the fault to the ledger. Protected by `X-Demo-Key` and locked to `POLICY_MODE=live`.

### 8. `manifest.py`
- **Purpose**: Machine-readable discovery manifest for autonomous agent commerce.
- **Endpoint**:
  - `GET /.well-known/agent-commerce.json`: Static, cached JSON (<50ms) describing merchant capabilities, auth schemes, endpoint URLs, policy disclosures (max offers per session, offer TTL, return policies, policy mode), latency hints, and audit guarantees.

### 9. `mcp.py`
- **Purpose**: Model Context Protocol (MCP) server implementation.
- **Key Details**:
  - Exposes FastMCP tools at `/mcp`: `search_products`, `get_offer`, `buy`, and `check_order`.
  - Directly delegates execution to the handlers in `api.agent`, ensuring identical business logic, Pydantic validation, and error reporting across both MCP and HTTP transports.

### 10. `ops.py`
- **Purpose**: Operational merchant actions and third-party integrations.
- **Endpoint**:
  - `POST /merchant/v1/shopify/sync`: Triggers synchronization of the product catalog from a connected Shopify store, recording imported and skipped SKU counts in the audit ledger. Protected by `X-Merchant-Key`.

### 11. `ratelimit.py`
- **Purpose**: In-memory, thread-safe rate limiting for public endpoints.
- **Key Details**: Implements a `FixedWindowLimiter` (default: 60 requests / 60 seconds) keyed by `agent_id` or IP address. Specifically protects the free `/agent/v1/catalog` endpoint against scraping and runaway retry loops.

### 12. `webhooks.py`
- **Purpose**: Razorpay payment gateway event callback handler.
- **Endpoint**:
  - `POST /webhooks/razorpay`: Processes asynchronous gateway events (`payment.authorized`, `payment.captured`, `payment.failed`, `refund.processed`).
- **Key Details**:
  - Enforces HMAC-SHA256 signature verification before reading payload data.
  - Implements event deduplication via ledger lookups.
  - Handles out-of-order event transitions cleanly.
  - Checks for paise vs. INR amount discrepancies and logs all outcomes to the public ledger.

---

## 🚦 Endpoints Summary

| Method | Path | Auth Required | Purpose |
|---|---|---|---|
| `GET` | `/.well-known/agent-commerce.json` | None | Discovery manifest |
| `GET` | `/health` | None | Service liveness & state |
| `POST` | `/agent/v1/catalog` | None (Rate Limited) | Search catalog |
| `POST` | `/agent/v1/offer` | None | Request bounded offer |
| `POST` | `/agent/v1/checkout` | Idempotency Key / Mandate | Purchase offered option |
| `POST` | `/agent/v1/order/{id}/settle` | None | Capture gateway payment |
| `GET` | `/agent/v1/order/{id}` | None | Poll order status |
| `GET` | `/audit/verify` | None | Verify ledger hash chain |
| `GET` | `/audit/{ref}` | None | Fetch entry / entity trail |
| `GET` | `/merchant/v1/dashboard` | `X-Merchant-Key` | Merchant dashboard data |
| `GET` | `/merchant/v1/approvals` | `X-Merchant-Key` | List pending approvals |
| `POST` | `/merchant/v1/approvals/{id}/approve` | `X-Merchant-Key` | Approve held order |
| `POST` | `/merchant/v1/approvals/{id}/reject` | `X-Merchant-Key` | Reject held order |
| `POST` | `/merchant/v1/approvals/{id}/counter` | `X-Merchant-Key` | Counter held order |
| `POST` | `/merchant/v1/shopify/sync` | `X-Merchant-Key` | Sync Shopify catalog |
| `POST` | `/demo/force_oversell` | `X-Demo-Key` | Trigger saga compensation demo |
| `POST` | `/webhooks/razorpay` | `X-Razorpay-Signature` | Razorpay webhook callback |
| `ALL` | `/mcp` | None / Tool-level | MCP protocol stream endpoint |
| `GET` | `/panel` | Browser | Static merchant console UI |

---

## 💻 Running the API

```bash
# Start with Uvicorn (from repository root)
python -m uvicorn api.app:app --reload --port 8000
```
