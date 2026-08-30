# PRAMAN Test Suite (	ests/)

> **Simple:** The safety net. 41 hermetic tests prove money logic without real keys/network. conftest.py fakes Razorpay and forbids real charges in shadow. Every bound, gate, ledger hash, and private-leak is asserted.

## Simple — what each file proves & which source it guards

| File | What it proves | Guards |
|---|---|---|
| conftest.py | Temp DB, TEST_SECRETS, FakeRazorpay/ForbiddenRazorpay, offer factories | whole suite |
| 	est_bounds.py / 	est_gates.py | 10 bounds + 3 tiers at 100% line coverage | kernel/bounds.py, kernel/gates.py |
| 	est_checkout.py / 	est_saga.py | 11-step path + oversell refund saga | kernel/checkout.py, kernel/saga.py |
| 	est_import_boundary.py | AST+BFS walk: vyapaari never reaches kernel.payments | yapaari/* |
| 	est_no_private_leak.py | Crawls all routes, asserts cost/floor/margin never in JSON | store/catalog.py:to_public() |
| 	est_mandate.py | 8-step mandate pipeline in order | mandate/verifier.py |
| 	est_ledger.py / 	est_receipt.py | Hash chain + HMAC receipt | store/ledger.py, kernel/receipt.py |

---

# PRAMAN Test Suite (`tests/`)

Welcome to the test suite for **PRAMAN** (Bazaar / Aether Audio) — an autonomous merchant-side agent-commerce platform where LLM selling engines propose deals, a deterministic policy kernel holds absolute veto power, and all financial actions are immutably hash-chained into a tamper-evident ledger.

---

## 📖 Overview & Testing Philosophy

The PRAMAN test suite is designed with uncompromising correctness, safety, and isolation guarantees:

1. **Hermetic by Default**: The default suite is fully offline, fast, and hermetic. It requires no network, no external API credentials, and no real payment gateway access.
2. **Deterministic Arithmetic**: Zero floating-point arithmetic is permitted in financial calculations or bounds checks (`_exceeds_pct` uses exact cross-multiplication, and amounts are strictly whole rupees or exact paise integers).
3. **Hard Structural Boundaries**: Tests enforce AST-level architectural isolation (e.g., the LLM layer `vyapaari/` cannot reach `kernel.payments` or touch payment credentials by any import path).
4. **Defensive Kernel Veto**: Tests verify that hallucinated SKUs, excessive discounts, over-allocated stock, and prompt injections are intercepted and vetoed by the kernel with traceable bound IDs.
5. **Tamper-Evident Ledgers & Cryptographic Receipts**: Tests assert the cryptographic chaining of ledger events (SHA-256) and the HMAC coverage of signed policy receipts and thought trails.
6. **Opt-in Live Testing**: Tests that hit the real Razorpay test-mode API are marked with `@pytest.mark.live_api` and are deselected by default unless `--live-api` is explicitly passed.

---

## 🚀 Running the Tests

### 1. Run the Hermetic Suite (Default)
```bash
python -m pytest
```

### 2. Run a Specific Test Module or Test Case
```bash
# Run a specific test file
python -m pytest tests/test_bounds.py

# Run a specific test function
python -m pytest tests/test_checkout.py::test_a_tier_0_checkout_creates_a_real_gateway_order

# Filter by keyword
python -m pytest -k "shadow"
```

### 3. Check Policy Kernel Coverage
`kernel.bounds` and `kernel.gates` are held at 100% line coverage:
```bash
python -m pytest --cov=kernel.bounds --cov=kernel.gates --cov-report=term-missing
```

### 4. Run Live Gateway Tests (Requires `.env` Configuration)
```bash
python -m pytest --live-api
```

---

## 📂 Subsystem Categorization

The 41 Python files in `tests/` are organized across key functional domains:

| Domain | Files |
| :--- | :--- |
| **Fixtures & Infrastructure** | `conftest.py`, `test_db_schema.py` |
| **Core Policy Kernel & Bounds** | `test_bounds.py`, `test_bound_relatedness.py`, `test_gates.py`, `test_core_safety.py`, `test_states.py` |
| **PRAMAN 2.0 MEC & Optimization** | `test_mec.py`, `test_policy_resolver.py`, `test_pre_filter.py`, `test_optimizer.py`, `test_tdr.py`, `test_negotiation_engine.py`, `test_negotiation_flow.py` |
| **Checkout, Orders & Sagas** | `test_checkout.py`, `test_saga.py`, `test_backup_failures.py`, `test_demo_endpoint.py` |
| **LLM Proposer & Defense Boundaries** | `test_proposer.py`, `test_agentic_proposer.py`, `test_kernel_catches_llm.py`, `test_import_boundary.py` |
| **Ledger, Receipts & Mandates** | `test_ledger.py`, `test_receipt.py`, `test_receipt_trail.py`, `test_mandate.py` |
| **Catalog, Tenancy & Store Learning** | `test_catalog.py`, `test_pairings.py`, `test_tenancy.py`, `test_no_private_leak.py` |
| **Personas, Protocols & Merchant Panel** | `test_manifest.py`, `test_mcp_tools.py`, `test_grahak_personas.py`, `test_ab_harness.py`, `test_dashboard_api.py`, `test_panel_and_clusters.py`, `test_shopify_connector.py` |
| **Payments & Live Gateways** | `test_payments.py`, `test_live_razorpay.py`, `test_live_checkout.py`, `test_live_capture.py` |

---

## 📄 Detailed File Reference

### 1. Infrastructure & Shared Fixtures
* **`conftest.py`**: Configures pytest session hooks, custom CLI option `--live-api`, dynamic marker filtering, temporary SQLite test databases with seeded catalog data, deterministic secret environments (`TEST_SECRETS`), `FakeRazorpay` and `ForbiddenRazorpay` stubs, live Razorpay/Gemini clients, and offer/mandate generators.
* **`test_db_schema.py`**: Verifies SQLite database schema initialization, WAL journal mode enforcement, table creation (14 tables), foreign key constraints, and unique indices (e.g. `idx_idempotency_keys_key`).

### 2. Core Policy Kernel & Bounds
* **`test_bounds.py`**: Exhaustive unit testing for the 10 policy bounds in `kernel/bounds.py` (floor price, maximum SKU discount, maximum cart discount, stock availability, daily budget, idempotency, offer freshness, session caps, human threshold). Validates inclusive boundaries and zero-float arithmetic.
* **`test_bound_relatedness.py`**: Validates Bound 10 (cross-sell / upsell relatedness), ensuring proposed add-ons have documented pairing evidence or shared category relationships with the base item.
* **`test_gates.py`**: Exhaustive testing of gate tier assignment (`Tier 0: Auto`, `Tier 1: Mandate`, `Tier 2: Human Approval`). Ensures highest matching trigger wins, Tier 0 acts as a strict allowlist, and decisions cannot time out into approval.
* **`test_core_safety.py`**: Validates PRAMAN 2.0 core safety invariants (1 through 8), guaranteeing deterministic proposal sources, valid verdicts, price consistency, hash integrity, and pre/post-payment state guards.
* **`test_states.py`**: Tests the transaction lifecycle state machine (`TransactionState`), verifying valid state transitions (from `INTENT_CREATED` to `FULFILLED` or `COMPENSATING`) and rejecting invalid transitions or exits from terminal states.

### 3. PRAMAN 2.0 MEC & Optimization
* **`test_mec.py`**: Unit tests for Merchant Economic Constitutions (MEC), testing dataclass immutability, hard constraint validation, economic objective weight normalization, and deterministic SHA-256 hashing.
* **`test_policy_resolver.py`**: Tests hierarchical MEC resolution and inheritance across store-level, category-level, and SKU-level rules to construct an `EffectivePolicy` snapshot.
* **`test_pre_filter.py`**: Verifies deterministic candidate deal pre-filtering, pruning deals violating SKU existence, floor prices, or discount limits before reaching optimization.
* **`test_optimizer.py`**: Tests multi-objective deal scoring and Pareto ranking against configurable economic weights (margin, conversion probability, AOV, inventory velocity).
* **`test_tdr.py`**: Validates Transaction Decision Records (TDRs), checking cryptographic linkage across buyer authority, effective policy, cart snapshot, and payment reference.
* **`test_negotiation_engine.py`**: Tests buyer-merchant algorithmic bargaining evaluations, feasible price range calculations, and counter-offer proposals.
* **`test_negotiation_flow.py`**: End-to-end integration test of multi-round agent negotiation: high-value offer triggers Tier-2 hold, merchant counters via API, and buyer polls and accepts.

### 4. Checkout, Orders & Sagas
* **`test_checkout.py`**: Tests the complete 11-step orchestrated checkout pipeline in `kernel/checkout.py`. Ensures strict ordering: idempotency registration -> stock hold -> ledger intent -> payment call -> ledger settlement -> stock commit.
* **`test_saga.py`**: Tests the oversell compensation saga handling stock race conditions where inventory vanishes before capture settlement. Verifies automated Razorpay refund, stock healing, and compensating ledger entries.
* **`test_backup_failures.py`**: Tests catastrophic failure paths: card declines, gateway errors, webhook retries, expired offers, and forged mandates, ensuring state unwinds cleanly and the ledger logs all faults.
* **`test_demo_endpoint.py`**: Tests the demo control surface (`POST /demo/force_oversell`), validating key authentication, shadow mode refusal, and reproducible compensation execution.

### 5. LLM Proposer & Defense Boundaries
* **`test_proposer.py`**: Tests the `vyapaari.proposer` failure ladder: well-formed responses pass, malformed JSON gets one retry with feedback, and complete model failures fall back to deterministic list-price proposals.
* **`test_agentic_proposer.py`**: Tests the multi-turn agentic exploration loop with tool execution (`search_catalog`, `get_pairings`), tool-call limits, wall-clock timeout caps, and kill-switch fallback.
* **`test_kernel_catches_llm.py`**: Tests policy kernel defenses against malicious or broken LLM proposals (hallucinated SKUs, negative prices, excessive discounts, prompt injection attacks).
* **`test_import_boundary.py`**: Enforces the architectural invariant by walking ASTs and building the first-party import graph to verify that `vyapaari/` has zero direct or transitive imports to `kernel.payments` or payment credential env vars.

### 6. Cryptography, Receipts & Ledgers
* **`test_ledger.py`**: Tests the append-only cryptographic ledger (`store/ledger.py`), verifying genesis block linking (`0000...`), SHA-256 chain verification (`/audit/verify`), and rejection of empty reason codes.
* **`test_receipt.py`**: Tests HMAC-SHA256 signed policy receipts (`kernel/receipt.py`), verifying canonical JSON encoding, complete field coverage, and signature breakages upon data tampering.
* **`test_receipt_trail.py`**: Tests Version 2 policy receipts binding LLM agent tool-call exploration trails into the signed HMAC material for verifiable reasoning.
* **`test_mandate.py`**: Tests the 8 verification checks for signed buyer mandates (issuer validation, ed25519 signature checking, expiry, category scope, max amount limits, and single-use nonce burning).

### 7. Catalog, Tenancy & Store Learning
* **`test_catalog.py`**: Validates the 14-SKU catalog dataset, in-memory caching performance (no database queries on read), public/private catalog separation, and schema validators.
* **`test_pairings.py`**: Tests the dynamic co-occurrence pairing store, verifying basket strength ratios, half-life exponential decay over time, and prior seeding.
* **`test_tenancy.py`**: Enforces strict multi-tenant isolation, ensuring store settings, pairing statistics, and policies do not leak across store boundaries.
* **`test_no_private_leak.py`**: Automated security crawler across all registered FastAPI routes checking that private fields (`cost_inr`, `floor_price_inr`, `margin_pct`) never appear in response payloads.

### 8. Personas, Protocols & Merchant Panel
* **`test_manifest.py`**: Tests discovery endpoint `/.well-known/agent-commerce.json`, validating response structure, open access, <50ms latency budget, and latency hint declarations.
* **`test_mcp_tools.py`**: Tests Model Context Protocol (MCP) tool endpoints (`search_products`, `get_offer`, `buy`, `check_order`), verifying identical policy enforcement to HTTP endpoints and proper exception raising.
* **`test_grahak_personas.py`**: Tests 8 distinct buyer agent personas through full discovery, offer generation, and checkout pipelines under strict latency budgets.
* **`test_ab_harness.py`**: Tests the A/B testing harness, validating identical rails for control and treatment arms, upsell attach rates, and metric aggregations.
* **`test_dashboard_api.py`**: Tests authenticated merchant observability endpoint `GET /merchant/v1/dashboard`, verifying panels (mode banner, metrics, approvals, ledger feed, bounds, safety).
* **`test_panel_and_clusters.py`**: Tests merchant admin panel static assets, Shopify synchronization endpoints, profit-weighted attach ordering, and cross-store cluster priors.
* **`test_shopify_connector.py`**: Tests Shopify connector catalog transformation, variant mapping, price/inventory sync, and push formatting without external network calls.

### 9. Payments & Live Gateways
* **`test_payments.py`**: Unit tests for `kernel/payments.py` amount conversions (whole rupees to paise and vice versa), float rejection, credential handling, and secret masking.
* **`test_live_razorpay.py`** (`live_api`): Validates live Razorpay test-mode API connectivity, order creation, credential verification, and HMAC webhook signature checking.
* **`test_live_checkout.py`** (`live_api`): Drives the complete checkout orchestrator against live Razorpay APIs to generate real test orders (`order_...`).
* **`test_live_capture.py`** (`live_api`): Verifies payment capture reconciliation, rupee-to-paise wire fidelity, and partial refunds on genuine captured test payments.
``