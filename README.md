# PRAMAN 2.0 — Merchant Economic & Trust Control Plane

**What this is:** PRAMAN is not a shopping bot. It is the **merchant-side control plane** that sits between an AI buyer (Grahak) and the payment rail (Razorpay). The AI proposes deals, PRAMAN decides if they are economically and policy-valid, and Razorpay moves the money.

> **AI can propose. PRAMAN decides. Payment rail executes.**

## Big picture flow

```
Buyer Intent (natural language + budget)
        ↓
Intent Gateway  →  Vyapaari (LLM proposer)  →  Policy Resolver (MEC hierarchy)
        ↓               ↓                            ↓
   Pre-Filter → Optimizer → Negotiation → Final Policy Kernel
        ↓               ↓                            ↓
   Reservation + Execution Guard → Razorpay → TDR + Hash-Chained Ledger
```

## Folder map — what lives where and who it connects to

| Folder | Simple job | Connected to |
|---|---|---|
| `api/` | HTTP + MCP front door for buyers, merchants, auditors | `kernel/`, `store/`, `mandate/` |
| `kernel/` | Deterministic money authority — 10 bounds, 3 gate tiers, 11-step checkout | `store/`, `policy/`, `mandate/`, `vyapaari/` |
| `store/` | SQLite persistence, catalog cache, append-only ledger, stock holds | `kernel/`, `api/` |
| `policy/` | Merchant Economic Constitution (MEC), optimizer, negotiation, TDR states | `kernel/`, `store/` |
| `vyapaari/` | Merchant-side LLM that *proposes* candidates (never decides) | `kernel/` (proposes → kernel vetoes) |
| `mandate/` | Ed25519 buyer-authority tokens (budget + scope + expiry) | `kernel/checkout.py`, `harness/` |
| `harness/` | Buyer counterparty — Grahak agent + Wallet + A/B rig | `api/` (over HTTP) |
| `dashboard/` | Next.js merchant console (metrics, approvals, ledger feed) | `api/dashboard.py` |
| `integrations/` | Shopify bridge (pull catalog, push orders/refunds) | `store/catalog.py`, `api/ops.py` |
| `scripts/` | CLI helpers: init DB, seed offers, tamper demo, Razorpay smoke, A/B run | all modules |
| `data/` | Runtime SQLite DB (`bazaar.db`) + generated artifacts | `store/db.py` |
| `tests/` | Hermetic suite (282 pass, 38 live skipped) | everything |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in keys — never commit .env
python scripts/init_db.py      # idempotent, cannot drop tables

python -m uvicorn api.app:app --reload   # http://localhost:8000
python -m pytest                          # hermetic, no network/keys needed
```

## Seven PRAMAN components (from `PRAMAN_2.0_Architecture.md`)

1. **Intent Gateway** — normalizes buyer natural language → structured request (qty, budget, preferences, authority)
2. **Vyapaari** — generates candidate deals (zero payment authority)
3. **Economic Optimizer** — scores candidates `Score = w_m·M + w_c·C + w_a·A + w_i·I`
4. **Negotiation Engine** — counters invalid buyer prices with best permitted offer
5. **Merchant Economic Constitution (MEC)** — hierarchy `STORE → CATEGORY → SKU → CAMPAIGN`
6. **Reservation + Execution Guard** — atomic `AVAILABLE → HELD → COMMITTED` + re-check before payment
7. **TDR + Audit Ledger** — immutable hash chain `SHA256(prev_hash + canonical_json(core))`

## Eight safety invariants

1. No LLM output can move money directly
2. Every payment references an approved deterministic verdict
3. Amount executed == amount approved
4. Cart executed == cart authorized
5. Inventory reserved/revalidated before commit
6. Every state-change is idempotent
7. Every completed transaction has a reconstructable TDR
8. Payment references exactly one immutable TDR with matching amount+cart

## Public / private split

`catalog.json` has two arrays: `products` (7 public fields) vs `product_private` (cost, floor, margin, attach rules). `store/catalog.py:to_public()` is a whitelist.

## Routes (from `api/`)

`/.well-known/agent-commerce.json` · `/health` · `/agent/v1/{catalog,offer,checkout,order/*}` · `/merchant/v1/{dashboard,approvals}` · `/merchant/v1/shopify/sync` · `/audit/{verify,trail}` · `/webhooks/razorpay` · `/mcp` · `/panel` · `/demo/force_oversell`

## Config & money rules

- Whole rupees everywhere (paise only inside `kernel/payments.py`)
- `extra="forbid"` on request models — unknown fields get 422, not silent drop
- `POLICY_MODE` lives in `kernel/mode.py` — default `shadow` (no real charges)
- `LATENCY_HINTS_MS` published in manifest — do not tighten below real budgets
- IDs via `secrets` in `store/ids.py`, timestamps lex-sortable via `store/timestamps.py`

See `plan.md` for the §-numbered spec and `BAZAAR_BUILD_PHASES.md` for phases. Do not write §/phase numbers into code comments.
