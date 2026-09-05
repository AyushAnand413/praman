# PRAMAN — Merchant Control Plane for Agentic Commerce

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python) ![Next.js](https://img.shields.io/badge/Next.js-Dashboard-black?logo=next.js) ![MCP](https://img.shields.io/badge/MCP-Compatible-purple) ![Razorpay](https://img.shields.io/badge/Payments-Razorpay-blue)

> **AI proposes. PRAMAN decides. Razorpay executes.**

PRAMAN sits between an AI buyer and the payment rail. The AI proposes a deal, PRAMAN decides if it is allowed, and Razorpay moves the money.

Shoppers use their own AI (ChatGPT, Claude, any MCP client). Merchants use PRAMAN's dashboard. The shopper never signs up — the bundle offer they see came from PRAMAN.

---

## Why it exists

AI buyers skip every visual upsell (bundles, cross-sell, warranty bumps, volume breaks) and leave with the cheapest single item. PRAMAN rebuilds the upsell as a structured, verifiable offer the buyer agent can check against its owner's budget — every rupee bounded, gated, and on a public proof trail.

## How it differs

| | PRAMAN | Razorpay | ChatGPT |
|---|---|---|---|
| **Role** | Merchant control plane (decider) | Payment rail (executor) | Buyer agent (proposer) |
| **Authority** | Enforces margins, floors, budgets, stock | Moves money on instruction | Proposes deals for the buyer |
| **Baseline vs PRAMAN** | `ChatGPT → PRAMAN → Razorpay` | `ChatGPT → Razorpay` (no merchant economics) | Happily proposes 90% off |

---

## How it works

```
Buyer intent → Vyapaari (LLM proposes) → Policy + Kernel (decides, can veto)
  → Reservation + re-check → Razorpay → Ledger + Receipt
```

| Module | Role |
|---|---|
| `vyapaari/` | Proposes only — no credentials, no DB writes, no payment imports |
| `kernel/` | Decides: 10 bounds, 3 gate tiers, 11-step checkout. Only module touching Razorpay keys |
| `policy/` | Merchant economics (MEC hierarchy, optimizer, negotiation) + 8 immutable safety invariants |
| `store/` | Postgres tables, catalog cache, append-only hash-chained ledger |
| `mandate/` | Verifies buyer authority (Ed25519 scope + budget + expiry) |

### Two engines worth knowing

- **Negotiation** (`policy/negotiation.py`, `kernel/approvals.py`, `harness/grahak.py`) — rejects below-floor bids, counters at floor or reduced qty (max 3 rounds), or returns `NO_FEASIBLE_DEAL`. Tier-2 orders support merchant approve / reject / counter.
- **Recommendations** (`store/pairings.py`, `kernel/recommender.py`, `api/recommendations.py`) — learns what sells together from every sale (confidence + lift, 45-day half-life decay), seeds new stores with declared companions + category priors, filters by live stock and budget. Bound 10 (`relatedness_required`) blocks nonsense combos in the kernel.

---

## Repo map

| Folder | Job |
|---|---|
| `api/` | HTTP + MCP front door (buyers, merchants, auditors) |
| `kernel/` | Deterministic money authority |
| `policy/` | Merchant economics, optimizer, negotiation, TDR |
| `store/` | Postgres persistence, catalog, ledger, holds, learning table |
| `vyapaari/` | LLM proposer (zero authority) |
| `mandate/` | Buyer-authority tokens |
| `harness/` | Grahak buyer agent + A/B rig |
| `integrations/` | Shopify bridge (catalog in, orders/refunds out) |
| `dashboard/` | Next.js merchant console |
| `scripts/` | init DB, seed, smoke, tamper demo, chain anchor |
| `eval/` | 8-metric eval harness → scorecard |
| `tests/` | Hermetic suite (live Razorpay tests opt-in only) |

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in DATABASE_URL + keys; never commit .env
python scripts/init_db.py
python -m uvicorn api.app:app --reload   # http://localhost:8000
python -m pytest                          # hermetic; --live-api opts into real Razorpay
```

## Routes

`/.well-known/agent-commerce.json` · `/health` · `/agent/v1/{catalog,offer,checkout,order/*,recommendations/*}` · `/merchant/v1/{dashboard,approvals,orders,policy,stores}` · `/merchant/v1/shopify/sync` · `/audit/{verify,trail}` · `/webhooks/razorpay` · `/mcp` · `/panel` · `/demo/force_oversell`

---

## Shop with MCP (Claude Desktop / any MCP client)

Same store, same rules as HTTP — the tools call the same handlers, so validation, ledger writes, and the kernel veto all apply. Full setup, tools, and troubleshooting: [`mcp.md`](mcp.md).

- **Endpoint:** `http://localhost:8000/mcp` · prod: `https://<brain>/mcp` (SSE fallback at `…/mcp-sse`)
- **Tools:** `search_products` → `get_offer` → `buy` → `check_order`

```json
{
  "mcpServers": {
    "praman": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp"]
    }
  }
}
```

---

## Connect Shopify (real catalog in, orders out)

Three steps. Products flow Shopify → PRAMAN; completed sales and saga refunds push back so you fulfil in the Shopify admin you already use.

**1. Create a custom app in Shopify admin** → Apps → Develop apps → create one with Admin API access: `read_products` (catalog import), `write_orders` (order + refund push). Install it and copy the token (`shpat_…`) and your domain (`my-store.myshopify.com`).

**2. Connect (needs merchant login — sign in first, use the Bearer token):**

```bash
curl -X POST http://localhost:8000/merchant/v1/stores/connect/shopify \
  -H "Authorization: Bearer <token>" -H "X-Store-Id: default" \
  -H "Content-Type: application/json" \
  -d '{"domain":"my-store.myshopify.com","token":"shpat_…"}'
```

**3. Poll the job until done:**

```bash
curl http://localhost:8000/merchant/v1/stores/sync/SYNC-… \
  -H "Authorization: Bearer <token>"
# → {"status":"done","imported":100,"skipped":2} (pending → running → done/failed)
```

Alternative (server-side): set `SHOPIFY_STORE_DOMAIN` + `SHOPIFY_ADMIN_ACCESS_TOKEN` in `.env`, then `POST /merchant/v1/shopify/sync` with the Bearer token.

> **Limits:** Single-variant products only (first variant imported). Unit cost is derived at `SHOPIFY_ASSUMED_MARGIN_PCT` (default 40%) — correct it in the dashboard. Big catalogs sync page-by-page; if the 25s window cuts off, just sync again. Every sync is written to the ledger.

---

## Rules that never move

- Whole rupees everywhere (paise only inside `kernel/payments.py`)
- No price field in buyer requests (`extra="forbid"` → 422)
- `POLICY_MODE` lives in `kernel/mode.py`, default `shadow` (verdicts computed, nothing charged)
- Ledger is append-only; corrections are new compensating entries
- Private catalog fields never leave the server (`store/catalog.py:to_public()` whitelist)

---

Full architecture: [`praman_arch.md`](praman_arch.md)
