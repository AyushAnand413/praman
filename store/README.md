# PRAMAN / BAZAAR — Store & Persistence Layer (`store/`)

> **Simple:** The memory and filing cabinet. Code in `api/` and `kernel/` call here to read products, save offers/orders, lock stock, and write the tamper-evident ledger. The in-memory `CatalogCache` makes reads fast; SQLite WAL makes writes safe.

The `store` package provides the persistent storage, state machines, catalog caching, multi-tenant context, and tamper-evident audit infrastructure for the PRAMAN / BAZAAR agent-commerce platform.

## Simple — what each file does & who it serves

| File | Plain job | Used by |
|---|---|---|
| `db.py` | Opens SQLite WAL, creates 15 tables, `BEGIN IMMEDIATE` transactions | every `store/*` |
| `catalog.py` | Loads `catalog.json`, validates, caches, `to_public()` whitelists 7 fields (private never leaks) | `api/agent.py`, `kernel/search.py`, `vyapaari/envelope.py` |
| `ledger.py` | Append-only hash chain `SHA256(prev+canonical_json)`, SQL triggers block edits | `kernel/checkout.py`, `api/audit.py`, `mandate/verifier.py` |
| `orders.py` | Order state machine `PENDING→HELD→CAPTURED→CONFIRMED/REFUNDED`, handles out-of-order webhooks | `kernel/checkout.py`, `api/webhooks.py` |
| `offers.py` | Offer store — single source of truth for price (client cannot override) | `kernel/offer.py`, `kernel/checkout.py` |
| `stock_holds` via `stock.py` bridge | `AVAILABLE→HELD→COMMITTED` rows with TTL | `kernel/stock.py` |
| `pairings.py` | Learned `frequently-bought-together` with half-life decay | `kernel/relations.py`, `vyapaari/tools.py` |
| `sessions.py` | Buyer session + `offers_made` counter for Bound 5 | `kernel/bounds.py`, `api/agent.py` |
| `approvals.py` / `tenancy.py` / `tdr_store.py` / `mec_store.py` | Human queue, multi-tenant `store_id`, decision records, policy versions | `kernel/approvals.py`, `policy/*` |
| `canonical.py` / `ids.py` / `timestamps.py` | Sorted-keys JSON + SHA256, `secrets` IDs, ISO `Z` timestamps | `store/ledger.py`, `kernel/receipt.py` |

It implements a robust, single-writer SQLite storage engine running in **WAL (Write-Ahead Logging)** mode with strict ACID isolation, guaranteed money safety, and fail-closed security properties.

---

## Key Architectural Principles

1. **Strict Public Serialization Whitelist (`to_public`)**
   The single entry point from the database to an HTTP response body is `catalog.to_public()`. Private merchant economics (`cost_inr`, `margin_pct`, `floor_price_inr`, `max_discount_pct`, `attach_candidates`, `attach_rate`, `tier_up_sku`, `offerable`) are never exposed or trusted across API boundaries.
2. **Tamper-Evident Hash-Chained Ledger (`ledger.py`)**
   Every money action, policy decision, and mandate acceptance is written to an append-only ledger chained with SHA-256 hashes (`entry_hash = SHA256(prev_hash + canonical_json(core))`). Database-level SQL triggers immediately abort any `UPDATE` or `DELETE` operations.
3. **Mandatory Reason Rule**
   Any ledger event where `money_delta_inr != 0` strictly requires a non-empty `reason` string, enforced both at write time in Python and at the database schema level via a SQL `CHECK` constraint.
4. **Order State Machine & Webhook Tolerance (`orders.py`)**
   Order states (`PENDING`, `HELD`, `AUTHORIZED`, `CAPTURED`, `CONFIRMED`, `VOIDED`, `REFUNDED`, `FAILED`) follow an explicit directed graph. Webhook deliveries out of sequence are handled gracefully via `advance()`, returning `advanced`, `already`, or `stale` without rolling back progress or failing valid webhooks.
5. **Two-Step Checkout Reservations**
   Orders durably record their active stock holds (`stock_hold_ids`) and reserved discount budgets (`budget_reserved_inr`) so that two-phase checkouts can safely survive gateway interruptions and allow the `/settle` endpoint to cleanly commit or release resources.
6. **Durable Human Approvals (`approvals.py`)**
   For Tier-2 gate approvals, "there is no timeout that approves". Pending approvals never auto-resolve to affirmative decisions, preserving the integrity of human oversight. Decisions are final and guarded against race conditions.
7. **Decayed Frequently-Bought-Together Learning (`pairings.py`)**
   Real basket co-occurrences are tracked with exponential half-life time decay (`PAIRING_HALF_LIFE_DAYS`), separating observed commercial evidence from cold-start seeded priors, with multi-tenant isolation and anonymous cross-store category pooling.
8. **Multi-Tenancy & Fail-Closed Resolution (`tenancy.py`)**
   Execution context is tracked through Python `contextvars`. Store identifiers are validated against configured slugs with no wildcard mechanisms, ensuring tenant isolation.

---

## Database Tables (`store.db`)

| Table Name | Description | Key Constraints & Invariants |
| :--- | :--- | :--- |
| `products` | Public product catalog data (SKU, title, list price, stock, category). | `list_price_inr > 0`, `stock_qty >= 0`, indexed on `category`. |
| `product_private` | Confidential economic margins, floor prices, and attach rules. | `cost_inr > 0`, `floor_price_inr > 0`, cascade on SKU delete. |
| `sessions` | Buyer agent conversation sessions and offer counters. | Tracks `offers_made` for bound #5 (`max_offers_per_session`). |
| `offers` | Authoritative server-side price quotes, options, and receipts. | Stores signed policy receipts, gate tiers, and strict TTL `expires_at`. |
| `orders` | Order state machine, payment gateway references, and reservations. | Unique partial index on `razorpay_order_id`, tracks stock hold IDs & discount budget. |
| `ledger` | Append-only, SHA-256 hash-chained financial & decision audit trail. | Triggers abort UPDATE/DELETE; mandatory reason on non-zero money movements; unique partial index on mandate nonces. |
| `idempotency_keys` | Idempotency registry for API requests. | `key` primary key with unique index to eliminate double-charge races. |
| `stock_holds` | Active inventory reservations held during shopping/checkout. | States: `ACTIVE`, `COMMITTED`, `RELEASED`, `EXPIRED`. |
| `policy_budgets` | Daily merchant discount expenditure tracking. | Keyed by UTC day (`YYYY-MM-DD`); tracks `discount_spent_inr >= 0`. |
| `approvals` | Merchant queue for Tier-2 orders requiring human decisions. | States: `PENDING`, `APPROVED`, `REJECTED`, `COUNTERED`; counter-offer references. |
| `ab_sessions` | Measurement records for A/B testing experiment runs. | Arm (`control`/`treatment`), persona, basket size, upsells shown/taken. |
| `pairing_denominators`| Base SKU basket denominator counts with exponential decay. | Keyed by `(store_id, base_sku)`. |
| `pairings` | SKU co-occurrence counts (`observed` vs `seeded`). | Keyed by `(store_id, base_sku, paired_sku, source)`. |
| `cluster_pairings` | Anonymous category-level priors pooled across cluster stores. | Category-level co-occurrences without exposing private SKUs. |
| `mec_versions` | Versioned Merchant Economic Constitutions (MEC). | Keyed by `(mec_id, version)` with SHA-256 content hashes. |
| `transaction_decision_records` | Immutable Transaction Decision Records (TDR). | Stores canonical JSON decision payloads and cryptographic hashes. |

---

## File-by-File Module Reference

### 1. `__init__.py`
Defines package metadata and restates the single DB-to-HTTP serialization rule. Serves as the package entry point documenting the relationship between schema management, catalog loading, and the ledger.

### 2. `approvals.py`
Manages the Tier-2 human approval queue for held orders.
- **Key Functions:** `request()`, `get()`, `require()`, `for_order()`, `pending_for_order()`, `pending()`, `decide()`.
- **States:** `PENDING`, `APPROVED`, `REJECTED`, `COUNTERED`.
- **Core Invariant:** Decisions are immutable once cast (`AlreadyDecided`). Counter-offers carry both a counter amount and an offer ID to allow negotiation to complete through the protocol rail.

### 3. `canonical.py`
Provides deterministic, byte-stable JSON serialization and SHA-256 ledger hashing.
- **Key Functions:** `canonical_json(payload)`, `entry_hash(prev_hash, core)`.
- **Rules:** Normalizes floats, collapses `-0.0` to `0`, rejects `NaN`/`Infinity`, converts int-like floats, rejects non-string dict keys, and produces whitespace-free sorted ASCII JSON to prevent verification failures across platforms.

### 4. `catalog.py`
Handles catalog loading from `catalog.json`, validation against strict SKU constraints, database seeding, in-memory caching, and public filtering.
- **Key Classes / Functions:** `CatalogCache`, `cache`, `to_public(row)`, `load_catalog_file(path)`, `seed_database()`, `seed_database_from_rows()`.
- **Core Invariant:** `PUBLIC_FIELDS` defines the whitelist contract. `PRIVATE_FIELDS` are stripped before data reaches callers. `CatalogCache` holds all products in memory for zero-database query latency during standard flows.

### 5. `db.py`
Central database connection management, WAL configuration, DDL execution, schema migrations, and transaction controls.
- **Key Functions:** `connect()`, `get_connection()`, `reset_connection()`, `transaction()`, `init_db()`, `migrate()`, `existing_tables()`.
- **Core Invariant:** Uses `BEGIN IMMEDIATE` transactions to prevent dirty read-then-write races on ledger tips and inventory holds. Manages triggers preventing ledger deletion/modification.

### 6. `ids.py`
Generates human-readable, cryptographically random prefixed identifiers.
- **Prefixes:** `OF-` (Offer), `ORD-` (Order), `PR-` (Policy Receipt), `SES-` (Session), `HOLD-` (Stock Hold), `APV-` (Approval), `MDT-` (Mandate), `AB-` (A/B Session).
- **Key Functions:** `new_id(prefix)`, `order_id()`, `offer_id()`, `session_id()`, `mandate_nonce()`. Uses `secrets.token_hex` for unguessable URL-safe entropy.

### 7. `ledger.py`
The append-only, SHA-256 hash-chained audit ledger tracking all state, mandate, and financial transitions.
- **Key Classes / Functions:** `LedgerEntry`, `append()`, `tip()`, `get()`, `recent()`, `trail(entity_id)`, `find_by_payload(key, value)`, `verify_chain()`.
- **Core Invariant:** Verifies complete hash continuity starting from genesis (`LEDGER_GENESIS_PREV_HASH`). Enforces `MandatoryReasonMissing` when `money_delta_inr != 0` and validates actors against `LEDGER_ACTORS`.

### 8. `measurement.py`
Stores and retrieves A/B testing experiment session records in `ab_sessions`.
- **Key Functions:** `record_session()`, `rows()`.
- **Usage:** Captures simulation results (arm, persona, basket size, upsell conversions) strictly out-of-band so experimental measurements cannot interfere with payment flows.

### 9. `mec_store.py`
Versioned storage for Merchant Economic Constitutions (MEC) across store, category, SKU, and campaign scopes.
- **Key Functions:** `save_mec_version()`, `get_mec()`, `get_latest_mec()`, `list_mec_history()`.
- **Integrity:** Serializes MEC dataclasses into canonical JSON and stores SHA-256 hashes to guarantee policy provenance.

### 10. `offers.py`
Server-side persistence and retrieval of quoted customer offers.
- **Key Functions:** `create()`, `get()`, `require()`, `option()`, `amount_for()`, `is_expired()`, `seconds_remaining()`, `for_session()`, `expired_before()`.
- **Core Invariant:** Acts as the single authoritative source of price totals during checkout; requests cannot override option pricing stored in the database.

### 11. `orders.py`
Encapsulates order creation, state transitions, gateway identifiers, and resource reservations.
- **Key Functions:** `create()`, `get()`, `require()`, `by_razorpay_order()`, `by_razorpay_payment()`, `transition()`, `advance()`, `attach_payment_ids()`, `record_reservation()`, `reservation()`, `clear_reservation()`, `with_open_reservation()`.
- **Core Invariant:** Manages legal transition graphs (`ALLOWED_TRANSITIONS`) and safely handles out-of-order webhooks via `advance()` rank comparisons.

### 12. `pairings.py`
Self-learning recommendation engine tracking co-occurring product baskets.
- **Key Functions:** `record_order_basket()`, `pairs_for()`, `related_skus()`, `seed_pairing()`, `snapshot()`, `record_category_basket()`, `cluster_pairs_for()`, `suggest_from_cluster()`.
- **Mechanics:** Applies exponential half-life time decay on write (`_decay_factor`), maintains separate denominators, enforces sample thresholds (`RELATEDNESS_MIN_SAMPLES`) before trusting observed correlation strengths, and provides privacy-preserving category cluster suggestions for cold stores.

### 13. `sessions.py`
Tracks conversational state between buyer agents and the storefront.
- **Key Functions:** `create()`, `get()`, `require()`, `ensure()`, `offers_made()`, `record_offer()`, `attach_mandate()`, `order_count_for_agent()`, `is_first_order_for_agent()`.
- **Usage:** Atomically increments `offers_made` to enforce Bound #5 (`max_offers_per_session`) and checks cross-session agent history to identify first-time buyers for tier gate evaluations.

### 14. `tdr_store.py`
Persists and retrieves Transaction Decision Records (TDR).
- **Key Functions:** `save_tdr()`, `get_tdr()`, `update_tdr_outcome()`.
- **Usage:** Records cryptographic snapshots of decisions at the moment of authorization, and updates final transaction outcomes with canonical hashing.

### 15. `tenancy.py`
Tenant context management and resolution.
- **Key Functions:** `configured_stores()`, `resolve()`, `set_current()`, `current_store()`, `reset_current()`, `cluster_for_store()`.
- **Core Invariant:** Uses Python `contextvars` to propagate `current_store()` across async/thread boundaries; validates store slugs and fails closed without wildcards.

### 16. `timestamps.py`
Standardizes timestamp generation and parsing across the system.
- **Format:** UTC ISO-8601 with microsecond precision and `Z` suffix (`%Y-%m-%dT%H:%M:%S.%fZ`).
- **Key Functions:** `utc_now()`, `to_ts()`, `now_ts()`, `parse()`, `plus_seconds()`, `utc_day()`.
- **Core Invariant:** Guarantees lexical sorting matching chronological sorting for fast SQL index range comparisons.

---

## Architectural Interaction Diagram

```
                 +--------------------------------------------+
                 |             HTTP API Layer                 |
                 +--------------------------------------------+
                       |                        |
                       v                        v
          +------------------------+   +----------------------+
          |   tenancy.py           |   | catalog.to_public()  |
          |   (ContextVar Store)   |   | (Whitelist Filter)   |
          +------------------------+   +----------------------+
                       |                        |
                       +-----------+------------+
                                   |
                                   v
             +---------------------------------------------+
             |         Policy Kernel & Gate Engine         |
             +---------------------------------------------+
               |             |             |             |
               v             v             v             v
        +-------------+ +----------+ +-----------+ +-------------+
        | sessions.py | |offers.py | |orders.py  | |approvals.py |
        +-------------+ +----------+ +-----------+ +-------------+
               |             |             |             |
               +-------------+-------------+-------------+
                             |
                             v
               +---------------------------+
               |         ledger.py         |
               |  (Append-Only SHA-256)    |
               +---------------------------+
               |        pairings.py        |
               | (Decayed Learning Matrix) |
               +---------------------------+
                             |
                             v
               +---------------------------+
               |           db.py           |
               | (SQLite WAL / Triggers)   |
               +---------------------------+
```
