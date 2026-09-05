# PRAMAN — Architecture

> One-line: PRAMAN is the merchant-side control plane between an AI buyer and the payment rail. AI proposes, PRAMAN decides, Razorpay executes.

---

## 1. What exactly we are building

Agentic commerce quietly destroys merchant revenue. Every merchandising lever a store has — "frequently bought together" grids, cart-page cross-sells, checkout warranty bumps, scarcity banners, volume-break nudges — is visual and human-targeted. An AI buyer renders none of it. It calls catalog + checkout over an API and leaves with the cheapest single item. The merchant gets higher conversion but collapsing average order value, zero attach rate, and no merchandising control.

PRAMAN rebuilds the upsell for a machine audience. You cannot persuade a buyer agent with a banner, but you can persuade it with a structured offer it can verify against its owner's budget — an agent optimizing "best value under ₹6,500" will take a bundle with a genuine ₹200 saving. PRAMAN generates that offer, checks it against hard merchant economics in deterministic code, and writes every step to a public tamper-evident trail.

Every transaction is the intersection of two authorities plus live state:

```
BUYER AUTHORITY (budget, qty, prefs, signed mandate)
  + MERCHANT AUTHORITY (margins, discount caps, stock, approval limits)
  + CURRENT COMMERCE STATE (stock, budget spent, offer expiry, policy version)
  → PRAMAN → economically-valid + policy-valid deal → Razorpay
```

Core principle, enforced in code rather than prompts:

> **The LLM proposes. Deterministic code disposes. Only deterministic code touches money.**

- `vyapaari/` (the LLM side) never imports `kernel/payments.py` (enforced by an import-boundary test), holds no credential, and writes nothing to the database.
- `kernel/payments.py` is the only module allowed to hold Razorpay credentials.
- The charge amount is read from the stored server-side offer by `option_id`. Buyer requests cannot carry a price — sending one returns a 422 error instead of being silently dropped.
- Whole rupees everywhere; paise exist only inside `kernel/payments.py`.

Two users, two screens:

| Person | Uses | Knows PRAMAN exists? |
|---|---|---|
| Shopper (e.g. Riya) | Her own AI — ChatGPT, Claude, any agent | No — she passes through |
| Merchant (the store owner) | PRAMAN dashboard — money, approvals, audit feed, rules | Yes — the paying customer |

Example: Riya tells ChatGPT "buy me sweat-proof earbuds under ₹6,500". ChatGPT discovers the store, receives a bundle offer (earbuds + case at ₹200 off with the reason attached), Riya says yes, ₹5,598 is captured. Riya never touched PRAMAN — but the bundle, the discount guardrails, and the proof all came from it.

What PRAMAN is not: not a shopping chatbot, not a payment rail, not shipping/tax/GST, not a multi-merchant marketplace. Test-mode money only.

---

## 2. What Razorpay currently does (the baseline we build on)

Razorpay's "store on ChatGPT" offering (Razorpay for ChatGPT Apps) solves the friction between discovery and payment:

- A merchant uploads a catalogue (Shopify auto-sync in under ~30 minutes, a hosted webstore for non-Shopify SMBs, custom builds for enterprise) and launches a storefront **inside the ChatGPT conversation** — no own MCP server, widgets, checkout flow, or submission package to build (an 8–10 week DIY job becomes days to weeks).
- Customers browse, add to cart, and pay **without leaving the chat**, via Razorpay's Magic Checkout (UPI/cards, saved details). Orders sync back to Shopify; existing payment flows stay intact.
- The pitch is zero-code reach: be present where 64M+ Indian users already discover products, and stop losing them on the redirect-to-website round trip.

In short: Razorpay answers **"how do we get paid inside the conversation?"** — checkout at the speed and place of intent, optimized for buyer conversion.

---

## 3. How PRAMAN differs from Razorpay

Razorpay owns the buyer checkout experience. PRAMAN owns the merchant deal authority in front of it. Different questions, different layers:

| Razorpay for ChatGPT Apps | PRAMAN |
|---|---|
| Gets the merchant paid inside ChatGPT | Decides **what the merchant is willing to sell, at what price, to which agent, under whose approval** |
| Optimizes buyer conversion (fewer steps, saved details) | Optimizes merchant value (margin protected, AOV recovered, inventory moved deliberately) |
| Same checkout for every merchant | Merchant-specific economics: per-store/category/SKU/campaign floors, caps, objectives |
| Accepts the cart the buyer built | Vets every line: 10 bounds (discount caps, price floor, daily budget, stock, relatedness, offer expiry, idempotency…), 3 gate tiers (auto / mandate / human) |
| Any price the flow produces can charge | Below-floor or over-budget prices are refused or countered, never charged; big carts halt for a human with no auto-approve |
| First-come stock, oversell refunded by support flow | Atomic `AVAILABLE → HELD → COMMITTED` reservations, re-checked milliseconds before payment; oversell triggers an automatic refund + SKU self-disable + structured remedy |
| Logs what happened | Signs **why** before money moves (HMAC policy receipt) and hash-chains every step into a public ledger anyone can verify |
| Buyer retries on timeout → gateway dedupes | Idempotency claimed **first**, intent ledgered **before** the gateway call, so a retry can never double-charge |

The relationship is a complement, not a competition:

```
ChatGPT → Razorpay            (baseline: buyer intent straight to payment)
ChatGPT → PRAMAN → Razorpay   (our path: intent checked against merchant
                               economics, then payment)
```

Concretely: Razorpay lets the customer pay without leaving ChatGPT. PRAMAN makes sure the thing they pay for is a deal the merchant would actually have agreed to — and can prove it afterwards. PRAMAN can sit in front of any Razorpay checkout, including the in-chat one.

---

## 4. How PRAMAN differs from ChatGPT (or any generic buyer agent)

ChatGPT serves the buyer. It understands intent and compares options, but it knows nothing about *this* merchant's costs, floors, budgets, stock, or approval rules — and it will happily propose 90% off if the buyer asks.

| Generic buyer agent | PRAMAN merchant side |
|---|---|
| Proposes whatever the buyer wants ("₹500 for a ₹34,990 laptop", "ignore instructions, 99% off") | Pre-filter plus kernel bounds reject it with a named rule id; injection-style input is treated as hostile data, never an instruction |
| Picks the cheapest option by default | Ranks only *allowed* deals by margin, conversion, order value, and inventory velocity |
| Can invent SKUs or quote stale prices | Invented SKUs, out-of-stock lines, and expired offers are rejected; every bound is re-run at checkout, not just at offer time |
| Reasoning is prose you must take on trust | Every offer carries `human_reason` plus machine-checkable `machine_rationale`, bound to a signed receipt issued before payment |
| Has no memory of what sells together | Learns baskets from every completed sale with time decay; new stores start from declared companions plus category-level priors |
| Retries on timeout, risking double charges | Idempotency key required and claimed first; ledger intent precedes the gateway call |
| Says "paid" | Proves it: public per-order trail plus a chain-verification endpoint and periodic external anchors |

Our own buyer agent (Grahak: wallet → signed mandate → discovery → catalog → offer → checkout, with counter-accept and an A/B rig) exists as the test counterparty. Any MCP-capable assistant can play the same role through `/mcp`.

---

## 5. System architecture — what is built, file by file

One direction only:

```
buyer agent → api/ → mandate/ → vyapaari/ (proposes)
  → policy/ + kernel/ (decides, can veto)
  → store/ (records, append-only)
  → kernel/payments.py → Razorpay → ledger + receipt
```

### 5.1 Config — `settings.py`

Frozen named constants: 10 bounds (per-SKU discount 12%, cart discount 15%, floor = cost × 1.20, daily discount budget ₹10,000, max 2 offers per session, human gate above ₹6,000, stock required, 300s offer TTL, idempotency required, relatedness minimum), bound ids 1–10, `POLICY_MODE` (default `shadow`: full verdict, zero charges), agent budget caps (max 4 tool calls, 2.5s wall clock) with a `PROPOSER_TOOLS_ENABLED` kill-switch, masked `Secret` credentials from the environment only, latency budgets versus looser published hints, ledger actors, multi-store tenancy settings, Shopify tunables.

### 5.2 Vyapaari — the proposer (LLM, zero authority) — `vyapaari/`

| File | Job |
|---|---|
| `vyapaari/proposer.py` | One-shot propose plus a bounded tool-calling loop (capped calls and clock, repair-retry, then deterministic fallback; every outcome carries source tag and exploration trail) |
| `vyapaari/tools.py` | Callable tools `search_catalog` + `get_pairings` (no store imports, no credentials) |
| `vyapaari/gemini.py` | Model client (model name, timeout, temperature) |
| `vyapaari/prompt.py` | Prompt builder; asserts no secret value is inside the prompt before dispatch |
| `vyapaari/schema.py` | Strict proposal schema: base item + at most 2 upsells |
| `vyapaari/envelope.py` | The sellable-SKU view the model is allowed to see |

### 5.3 Policy Kernel — the decider (pure Python, only money caller) — `kernel/`

| File | Job |
|---|---|
| `kernel/bounds.py` | 10 bounds as independent pure functions, evaluated per upsell; one bad upsell removes that line, a bad base item fails the offer; bound 6 (large spend) routes to a human instead of rejecting |
| `kernel/gates.py` | 3 tiers: 0 auto (small total + small discount), 1 signed-mandate, 2 human halt; highest trigger wins, never auto-approves |
| `kernel/checkout.py` | 11-step money path in fixed order: claim idempotency → load offer → revalidate everything → reserve stock → ledger intent → create gateway order → capture → commit stock → ledger confirm → webhook → respond; plus late-settle and abandoned-cart sweep; records each clean basket for learning |
| `kernel/payments.py` | Sole credential holder; Razorpay orders, payments, refunds; the only paise-aware module |
| `kernel/mode.py` | Shadow vs live; shadow returns the identical verdict and receipt but calls nothing and records what *would* have charged |
| `kernel/receipt.py` | HMAC-signed policy receipt v2: per-item verdicts, reasons, gate tier, exploration trail |
| `kernel/stock.py` | Atomic reservations with TTL and row-level locking; commit against the hold |
| `kernel/budgets.py` | Daily discount-budget check-and-accrue under lock |
| `kernel/offer.py` | Offer assembly, bound evaluation, per-session offer quota |
| `kernel/saga.py` | Oversell compensation: detect → refund → void order → auto-disable SKU |
| `kernel/approvals.py` | Tier-2 resume: approve, reject, or counter (counters become fresh re-bounded, re-signed offers) |
| `kernel/relations.py` | Related-SKU evidence map from learned pairings plus declared companions |
| `kernel/recommender.py` | Cold-start seeding, cluster priors, lift-ranked selection (see §7) |
| `kernel/search.py` | Deterministic catalog ranking behind search |
| `kernel/reasons.py` | Controls on outbound prose (forbidden phrases, value-scanned fields) |
| `kernel/idempotency.py` | Key claim and idempotent replay |

### 5.4 Merchant economics — `policy/`

| File | Job |
|---|---|
| `policy/mec.py` | Merchant Economic Constitution: hard constraints, objectives, negotiation permissions; hierarchy store → category → SKU → campaign |
| `policy/resolver.py` | Resolves the effective policy and pins a frozen snapshot (id, version, hash) to each transaction |
| `policy/optimizer.py` | Scores only feasible deals: margin, conversion, order value, inventory velocity weighted by merchant objectives |
| `policy/negotiation.py` | Price negotiation math and counter generation (see §6) |
| `policy/pre_filter.py` | Discards clearly invalid candidates before optimization |
| `policy/snapshot.py` | Frozen policy snapshot attached to the decision record |
| `policy/tdr.py` | Transaction Decision Record: intent + cart + policy + decision + reservation + payment + outcome |
| `policy/core_safety.py` | 8 immutable invariants no merchant can switch off (LLM can't move money, amount and cart must equal what was approved, reserve before commit, everything idempotent, every payment tied to exactly one matching decision record) |
| `policy/states.py` | Explicit lifecycle including failure states (expired, failed, compensating → refunded) |

### 5.5 Buyer authority — `mandate/`

| File | Job |
|---|---|
| `mandate/signer.py`, `mandate/keys.py` | Ed25519 key handling and signing |
| `mandate/token.py` | Mandate shape: scope, max amount, expiry, agent id |
| `mandate/verifier.py` | Signature, scope, amount, and expiry checks |
| `mandate/issuers.py` | Trusted-issuer registry plus demo issuer bootstrap |

### 5.6 Persistence and proof — `store/`

| File | Job |
|---|---|
| `store/db.py` | Postgres schema and connections (one `DATABASE_URL` everywhere); additive migrations only |
| `store/catalog.py` | Loader, in-memory cache, `to_public()` whitelist (the only DB→HTTP path for product data), self-heal hook that disables oversold SKUs |
| `store/canonical.py` | Canonical JSON feeding both the ledger hash and the receipt HMAC |
| `store/ledger.py` | Append-only writer, `SHA256(prev_hash + canonical(entry))`, genesis `0…0`, tip/trail/chain-verify, no-update/no-delete triggers, mandate-replay uniqueness |
| `store/ids.py`, `store/timestamps.py` | `secrets`-based ids; lex-sortable UTC timestamps |
| `store/offers.py`, `store/orders.py` | Offers (options, expiry, receipt, counter links); orders (guarded state machine, holds and budget refs stored on the row) |
| `store/sessions.py` | Sessions and the per-session offer counter |
| `store/approvals.py` | Tier-2 queue with atomic decide (second decider loses cleanly) |
| `store/pairings.py` | Co-purchase learning table with decay and per-store isolation (see §7) |
| `store/tenancy.py` | Fail-closed tenant resolution, no wildcard |
| `store/mec_store.py` | Persisted merchant policy edits |
| `store/auth.py` | Merchant accounts and tokens |
| `store/measurement.py` | A/B session arms |
| `store/tdr_store.py` | Decision-record persistence |

Catalog privacy is structural: public products and private economics (cost, floor, margin, attach rules) are separate tables, and only the whitelist serializer may face the network.

### 5.7 Front doors — `api/`

| File | Job |
|---|---|
| `api/app.py` | App factory and startup (schema, seed, cache warm, genesis entry, issuer registry, abandoned-cart sweep), `/health` with mode and chain head |
| `api/agent.py` | Buyer endpoints: catalog, offer, checkout (key via `Idempotency-Key` header), order status/settle/poll |
| `api/mcp.py` | Same handlers and validation as HTTP, exposed as tools (`search_products`, `get_offer`, `buy`, `check_order`) at `/mcp` (plus `/mcp-sse`); refusals are raised errors, never plain text |
| `api/manifest.py` | Machine discovery manifest: capabilities, endpoints, auth scheme, policy disclosure, latency hints |
| `api/approvals.py` | Merchant approve / reject / counter |
| `api/dashboard.py` | Metrics, live feed, and the safety panel (bound firings, refusals, holds, compensations, structural `double_charges: 0`) |
| `api/audit.py` | Public audit: chain verification and per-order trail (redacted) |
| `api/webhooks.py` | Razorpay webhook verification and idempotent reconcile |
| `api/orders.py` | Merchant order list, detail, and history |
| `api/stores.py` | Store connect (Shopify/Woo/Custom) plus sync jobs |
| `api/ops.py` | Shopify sync trigger |
| `api/policy.py` | Policy read/update surface |
| `api/recommendations.py` | Companion and bundle recommendations per SKU |
| `api/auth.py` | Merchant signup/signin/session/signout |
| `api/demo.py` | Forced-oversell demo plus backup failure paths (decline card, webhook retry, expired offer, forged mandate) |
| `api/ratelimit.py`, `api/events.py`, `api/index.py` | Throttling, event emission, index |

### 5.8 Real-store bridge — `integrations/shopify.py`

Catalog import (Shopify → store, single-variant scope, derived cost explicitly labelled as an assumption), order push and refund push (store → Shopify) over an injectable client. Woo/Custom punch through the stores API plus the same validated seed path.

### 5.9 Counterparty and measurement — `harness/`, `eval/`, `scripts/`

| File | Job |
|---|---|
| `harness/grahak.py` | Buyer agent plus wallet; polls Tier-2 holds and accepts counters through ordinary checkout |
| `harness/ab.py` | Control/treatment session rig (runs without moving money) |
| `eval/runner.py` + `eval/fixtures.json` | 8-metric harness (price floor, discount cap, injection, model-failure fallback, human gating, receipt/chain, search latency, basket lift) producing a scorecard |
| `scripts/init_db.py` | Idempotent database init (cannot drop) |
| `scripts/anchor_chain.py` | Publishes the ledger head hash externally and detects history rewrites |
| `scripts/razorpay_smoke.py`, `scripts/checkout_live.py`, `scripts/seed_offer.py`, `scripts/tamper_demo.py` | Gateway smoke test, live checkout, offer seeding, tamper demo |

### 5.10 Merchant console — `dashboard/` + `public/panel`

Next.js console (revenue, pending approvals with counter, ledger feed, policy drawer, auth) speaking the same JSON endpoints as any external client, plus a static console served by the API process. Ships as API backend plus Vercel dashboard.

---

## 6. Negotiation engine

PRAMAN bargains instead of just accepting or rejecting. Two layers, both deterministic and covered by negotiation tests:

| Piece | File | Behavior |
|---|---|---|
| Price math | `policy/negotiation.py` | `evaluate_buyer_proposal()`: merchant floor from cost and minimum margin; outcomes `ACCEPTED / COUNTER / NO_FEASIBLE_DEAL / ESCALATE`; at most 3 rounds; counters offered at floor price and at a budget-fitting reduced quantity; anything below floor is never accepted |
| Merchant counter | `kernel/approvals.py` | A held Tier-2 order can be repriced into a fresh offer id that re-passes all bounds and is re-signed; the new id travels on the approval row and the order poll response |
| Buyer accept | `harness/grahak.py::accept_counter` | Reads the counter id from the poll and checks out against it normally, so bounds run on the merchant's terms |

Live loop: buyer agent bids 3× flagship at ₹14,997 → Tier-2 hold → merchant counters ₹13,797 (volume break, re-bounded) → agent verifies mandate and budget → accepts → capture. No overlap between floor and budget returns an honest `NO_FEASIBLE_DEAL` — a valid outcome, not an error.

---

## 7. Product recommendation engine

Bundle selection is association-rule mining (market-basket confidence and lift), pure Python, millisecond path, no LLM required:

### In plain words — the algorithms and why they're cool

No black-box model. Three small, explainable ideas do all the work:

1. **"People who bought this also bought…" (confidence).** After every sale we count: out of 100 buyers of earbuds, how many also took the case? If 31 did, the case scores 31%. That is it — the store learns from its own bills, not from anyone's opinion. (`store/pairings.py`, fed by `kernel/checkout.py`)
2. **"…more than by chance" (lift).** A cleaning cloth sells to everyone, so pairing everything with it is lazy. Lift divides a pair's score by the companion's general popularity. Earbud case: bought by 5% of all shoppers but 40% of earbud buyers → lift 8×, genuinely paired. Cloth: 60% everywhere → lift ~1×, coincidence. We rank high-lift pairs first, so the bundle is a real affinity, not just a popular item shoved in. (`kernel/recommender.py`, minimum lift 1.0, minimum confidence 5%)
3. **"Old habits fade" (exponential decay).** Every count carries a timestamp and halves in weight every 45 days. Last Diwali's craze stops outranking this month's trend on its own — no manual cleanup, no overnight retraining. (`PAIRING_HALF_LIFE_DAYS` in `settings.py`)

Two supporting acts use the same explainable style:

- **Deal picker (weighted score).** When several valid deals exist, each is scored on margin, conversion chance, order value, and inventory movement, weighted by what the merchant said matters (e.g. margin 40%, conversion 30%, order value 20%, clearing stock 10%). Highest score wins — the merchant's priorities as arithmetic, auditable on the receipt. (`policy/optimizer.py`)
- **Haggler (floor + feasible range).** Floor price comes from cost and minimum margin (cost ÷ (1 − margin)). Buyer ceiling vs merchant floor defines the feasible range; inside it we pick the price with the best expected contribution, outside it we counter or honestly say no deal. (`policy/negotiation.py`)

Worked mini-example: 214 phone buyers, 178 also took the charger (83%), 73 took the glass cover (34%). Charger lift is high, glass is mild → next phone buyer is offered the charger bundle first. Six months later the pattern flips; decay flips the ranking automatically.

| Piece | File | Behavior |
|---|---|---|
| Learning table | `store/pairings.py` | Per-store co-purchase counters fed by every clean capture (refunded oversells excluded); exponential 45-day half-life decay; first-party observed evidence outranks seeds; strictly per-store |
| Selector | `kernel/recommender.py` | Ranks observed pairs (minimum samples, ≥5% confidence, lift ≥ 1.0) above seeded priors; skips out-of-stock lines and budget-breaking additions; emits store-authored reasons distinguishable from model prose |
| Offer wiring | `kernel/offer.py` | If the LLM proposes no upsell, the algorithmic pick fills Option B; every pick still faces bound 10 |
| Relatedness gate | `kernel/relations.py` + `kernel/bounds.py::check_relatedness` | Evidence map from learned pairs plus declared companions; nonsense pairings (laptop with cat food) are refused under `relatedness_required` |
| Cold start | `kernel/recommender.py` seeding + cluster priors | New stores start from declared companions plus anonymous category-level priors from stores in the same cluster; a store's own evidence takes over after a handful of its own baskets |
| HTTP surface | `api/recommendations.py` | `GET /agent/v1/recommendations/{sku}`: raw pairs plus assembled bundle options with totals and reasons; no LLM, session, or mandate involved |

Math: `Confidence(A→B) = orders with both ÷ orders with A`; `Lift = Confidence ÷ overall popularity of B` (above 1.0 means genuinely paired); `Weight = 0.5^(days old ÷ half-life)`. Four cases: brand-new store, active store, large imported catalog, and live stock/budget misses (skip to the next candidate or omit the bundle).

---

## 8. Key flows

Discovery manifest → catalog (public fields only) → offer (mandate check → session quota → Vyapaari propose → bounds per upsell → gate tier → signed receipt → ledger → offer with prose reason, machine rationale, audit link) → checkout (the fixed 11 steps) → Tier-2 hold with approve/reject/counter → oversell saga on races → public verification and anchors.

Adoption posture, all built and merchant-switched: stage 1 Discovery (shadow, ₹0 moves) → stage 2 Reservation (holds and idempotency under load, still ₹0) → stage 3 Transactions (live, tier-gated). The mode flag lives inside the kernel so no endpoint can bypass it.

---

## 9. Current status — shipped versus open

Live today: the full buyer path (discovery, catalog, offer, checkout, order poll, MCP tools), the 10-bound / 3-tier kernel with shadow mode, signed receipts with exploration trails, the hash-chained ledger with verify + anchors, the learning table with the algorithmic recommender and relatedness bound, the bounded tool-calling proposer with kill-switch, price negotiation plus merchant counter flow, Shopify import/order/refund code with per-store isolation plumbing, the merchant console with safety panel, the A/B and eval harnesses. The default test suite runs hermetically (no network, no keys); Razorpay-hitting tests are opt-in.

Still open: the live dev-store demo against a real Shopify account, the batch trend-seeder for brand-new large catalogs (only the single-seed mechanism ships), production deploy plus repeated full demo rehearsals, and a backlog of hardening items — completing per-store isolation on every table, syncing the catalog cache with stock commits plus lazy load on cold start, fixing the Woo/Custom connector crash, deriving MCP auto-idempotency keys deterministically, wiring dashboard policy edits through to kernel enforcement, moving the dashboard cache out of the budget table, and closing the concurrent session-quota race.

---

## 10. Rules that never move

- The LLM never holds credentials, never writes to the database, never reaches the payment module by any import path.
- No buyer request carries a price; unknown fields are rejected loudly.
- Idempotency is claimed first; ledger intent precedes any gateway call.
- Bounds are re-evaluated at checkout; held orders never auto-approve.
- The ledger is append-only — corrections are new compensating entries.
- Published latency hints are never tighter than real budgets.
