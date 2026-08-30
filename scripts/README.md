# PRAMAN Scripts (`scripts/`)

> **Simple:** Toolbox for humans. Initialize DB, prove ledger is tamper-evident, poke Razorpay with real test cards, have a fake buyer shop the live server, seed offers without an LLM, and run 400-session A/B shows.

This directory contains executable utility, validation, benchmarking, and demonstration scripts for the **PRAMAN** autonomous commerce kernel. These scripts provide end-to-end tooling to initialize the store, demonstrate tamper-evident cryptographic ledgers, exercise live payment flows with Razorpay, simulate buyer agent journeys, seed test offers through the policy engine, and execute A/B measurement experiments.

## Simple — what each script does & who it touches

| Script | One-line job | Touches |
|---|---|---|
| `init_db.py` | Create/seed `bazaar.db` + genesis block (idempotent) | `store/db.py`, `store/catalog.py`, `store/ledger.py` |
| `anchor_chain.py` | Save head hash to `data/chain_anchors.jsonl` + ` --verify` | `store/ledger.py` |
| `tamper_demo.py` | Show trigger blocks `UPDATE`, then `DROP TRIGGER` + edit → `verify_chain` breaks (on copy by default) | `store/ledger.py`, `data/tamper_demo.db` |
| `seed_offer.py` | Create tier0/1/2/upsell offers without LLM + emit `curl` | `kernel/offer.py`, `store/offers.py` |
| `razorpay_smoke.py` | Create order → browser `checkout_test.html` → capture | `kernel/payments.py`, Razorpay |
| `checkout_live.py` | Seed offer → mandate if needed → stock hold → Razorpay → settle | `kernel/checkout.py`, `mandate/*` |
| `demo_buy.py` | Grahak walks 4-step rail against live server | `harness/grahak.py`, `api/agent.py` |
| `run_ab.py` | 200 control vs 200 treatment shadow run → uplift metrics | `harness/ab.py` |
| `_console.py` / `_env.py` | UTF-8 fix for Windows `₹`, zero-dep `.env` parser (script-only) | `settings.py` (app uses `os.environ` directly) |

---

## Directory Overview

| Script | Category | Description | Primary Command |
| :--- | :--- | :--- | :--- |
| [`init_db.py`](#init_dbpy) | Setup | Initializes database tables, seeds catalog SKUs, and logs genesis ledger entry. | `python scripts/init_db.py` |
| [`anchor_chain.py`](#anchor_chainpy) | Audit & Security | Anchors ledger head hashes externally to `data/chain_anchors.jsonl` and verifies historical integrity. | `python -m scripts.anchor_chain` |
| [`tamper_demo.py`](#tamper_demopy) | Audit & Security | Demonstrates SQLite trigger protections and cryptographic hash-chain verification under data tampering. | `python scripts/tamper_demo.py` |
| [`seed_offer.py`](#seed_offerpy) | Testing & Fixtures | Evaluates policy bounds, assigns gate tiers, signs receipts, and seeds offer scenarios without an LLM. | `python -m scripts.seed_offer` |
| [`razorpay_smoke.py`](#razorpay_smokepy) | Payments | Exercises direct Razorpay test-mode gateway communication (order creation, card authorization, payment capture). | `python scripts/razorpay_smoke.py` |
| [`checkout_live.py`](#checkout_livepy) | Payments | Runs an end-to-end purchase through the full kernel (bounds, gate tier, mandates, stock holds, and settlement). | `python scripts/checkout_live.py --live` |
| [`demo_buy.py`](#demo_buypy) | Simulation | Simulates the *Grahak* buyer agent across the full 4-step shopping rail against a live running server. | `python -m scripts.demo_buy` |
| [`run_ab.py`](#run_abpy) | Experimentation | Runs a 400-session simulated A/B testing experiment in shadow mode and reports uplift and margin efficiency. | `python -m scripts.run_ab` |
| [`_console.py`](#_consolepy) | Internal Helper | Reconfigures stdout/stderr streams to UTF-8 to prevent cp1252 encoding crashes on Windows terminals. | *Imported by scripts* |
| [`_env.py`](#_envpy) | Internal Helper | Standalone, zero-dependency parser to load `.env` variables into `os.environ` for CLI workflows. | *Imported by scripts* |

---

## Detailed File Reference

### 1. `init_db.py`
* **Purpose**: Sets up the SQLite database schema, populates the catalog with seed SKUs, loads the in-memory catalog cache, and registers the initial `ledger.genesis` audit entry.
* **Key Features**:
  * **Idempotent & Safe**: Can be executed repeatedly without destroying existing audit logs or resetting sequence counters.
  * **Strict Append-Only Guarantee**: Does not run destructive `DROP TABLE` operations, safeguarding historical ledger continuity.
  * Verifies SQLite WAL mode (`journal_mode`) and asserts that all defined database tables exist.
* **Usage**:
  ```bash
  python scripts/init_db.py
  ```

---

### 2. `anchor_chain.py`
* **Purpose**: Provides out-of-band anchoring for the append-only ledger hash chain. By recording point-in-time head hashes to an external JSONL file (`data/chain_anchors.jsonl`), it narrows the window for retroactive ledger rewrite attacks.
* **Key Features**:
  * Prevents anchoring broken or uninitialized chains.
  * `--verify` mode validates historical anchors against current ledger entries to flag retroactive modifications.
* **Usage**:
  ```bash
  # Record the current ledger head hash
  python -m scripts.anchor_chain

  # Verify past anchored checkpoints against the current database
  python -m scripts.anchor_chain --verify
  ```

---

### 3. `tamper_demo.py`
* **Purpose**: Demonstrates the tamper-evident security model of the PRAMAN ledger.
* **Key Features**:
  * **Two-Stage Proof**:
    1. Proves that database triggers (`ledger_no_update` / `ledger_no_delete`) block raw SQL `UPDATE` queries.
    2. Simulates a privileged attacker dropping the triggers and modifying a historical row payload, showing that `ledger.verify_chain()` immediately detects the broken cryptographic link (`{intact: false, broken_at: N}`).
  * Operates on a safe temporary copy (`data/tamper_demo.db`) by default to protect development data.
* **Usage**:
  ```bash
  # Run safe tampering test on a database copy
  python scripts/tamper_demo.py

  # Target a specific sequence number
  python scripts/tamper_demo.py --target-seq 3

  # Permanently tamper the active database (destructive)
  python scripts/tamper_demo.py --in-place
  ```

---

### 4. `seed_offer.py`
* **Purpose**: Evaluates and seeds valid, deterministic offer records directly into SQLite without relying on an LLM proposer, allowing manual verification of pricing bounds, gate tiers, and checkout flows.
* **Key Features**:
  * Executes the policy kernel bounds evaluator, checks stock availability, evaluates category relations, assigns Gate Tiers (0, 1, or 2), and signs HMAC policy receipts.
  * Generates ready-to-use `curl` commands for checkout testing.
  * Built-in test scenarios:
    * `tier0`: Sub-₹2,000 cart, autonomous capture, no mandate required.
    * `tier1`: ₹2,000–₹6,000 cart, requires a cryptographically signed user mandate.
    * `tier2`: High value (>₹6,000), trips autonomous limit and requires merchant approval.
    * `tier2_discount`: Discount >8%, routes to Tier 2 human approval.
    * `upsell`: Base item bundled with accessory lines under aggregate cart bounds.
    * `refused_stock`: Out-of-stock item refused at offer evaluation time.
    * `refused_floor`: Pricing below catalog floor price refused by bounds.
* **Usage**:
  ```bash
  # Seed all predefined scenarios
  python -m scripts.seed_offer

  # List available scenarios and what they prove
  python -m scripts.seed_offer --list

  # Seed a single scenario
  python -m scripts.seed_offer --only tier1

  # Output structured JSON
  python -m scripts.seed_offer --json
  ```

---

### 5. `razorpay_smoke.py`
* **Purpose**: Smoke tests transport-level communication with the Razorpay payment gateway using test-mode API keys (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`).
* **Key Features**:
  * **Two-Step Capture Process**:
    1. Creates a Razorpay gateway order and generates a local browser payment page (`data/checkout_test.html`).
    2. After test card authorization in the browser, captures the payment server-side and logs the monetary delta to the audit ledger.
* **Usage**:
  ```bash
  # Step 1: Create the test order and launch checkout page
  python scripts/razorpay_smoke.py --amount 5598

  # Step 2: Capture authorized payment after completing browser checkout
  python scripts/razorpay_smoke.py --order order_xxxxxxxxxxxxxx
  ```

---

### 6. `checkout_live.py`
* **Purpose**: End-to-end verification of the full autonomous purchase flow through the policy kernel and Razorpay in live/test mode.
* **Key Features**:
  * Seeds a real scenario offer, checks policy gates, auto-issues ECDSA user mandates when required (Tier 1), reserves stock holds, tracks budget reservations, and creates a Razorpay gateway order.
  * Handles human approval holds (Tier 2) without sending charges to Razorpay.
  * Settles completed payments, releases reservation locks, and confirms ledger audit integrity.
* **Usage**:
  ```bash
  # Step 1: Place an order through the kernel
  python scripts/checkout_live.py --live --scenario tier0

  # Step 2: Settle the placed order following payment authorization
  python scripts/checkout_live.py --live --settle ORD-xxxxxxxx
  ```

---

### 7. `demo_buy.py`
* **Purpose**: Runs a simulated buyer agent (*Grahak*) against a live running PRAMAN API server to demonstrate autonomous negotiation and purchasing.
* **Key Features**:
  * Walks the four-stage agent rail:
    1. **Discovery** (`GET /agent/v1/discovery`)
    2. **Catalog Browse** (`GET /agent/v1/catalog`)
    3. **Offer Request** (`POST /agent/v1/offers`)
    4. **Checkout & Purchase** (`POST /agent/v1/checkout`)
  * Emits live events visible on the Merchant Dashboard (`/panel/`), reflecting real-time ledger updates and approval triggers.
* **Usage**:
  ```bash
  # Run against default local instance (http://127.0.0.1:8090)
  python -m scripts.demo_buy

  # Custom server address and search prompt
  python -m scripts.demo_buy --base-url http://localhost:8000 --need "wireless noise cancelling headphones"
  ```

---

### 8. `run_ab.py`
* **Purpose**: Executes automated A/B benchmark experiments (200 control vs. 200 treatment sessions by default) in shadow mode.
* **Key Features**:
  * Evaluates base-only offers (control) against smart bundle upsell offers (treatment).
  * Runs on an isolated in-memory or temporary database (`tempfile`) to prevent inventory depletion from skewing conversion numbers.
  * Calculates core business metrics:
    * Conversion rate (%)
    * Average Order Value (AOV)
    * Upsell attach rate (%)
    * **Gross Margin per ₹1 Discounted** (measuring discount efficiency against private SKU cost tables).
* **Usage**:
  ```bash
  # Run standard 400-session experiment
  python -m scripts.run_ab

  # Quick rehearsal run
  python -m scripts.run_ab --sessions-per-arm 10

  # Export JSON metrics for analysis
  python -m scripts.run_ab --json
  ```

---

### 9. `_console.py`
* **Purpose**: Terminal output encoding helper.
* **Key Features**:
  * Exports `use_utf8_stdout()`.
  * Reconfigures `sys.stdout` and `sys.stderr` to UTF-8 with fallback replacement characters (`errors="replace"`), preventing `UnicodeEncodeError` exceptions on Windows consoles (e.g., `cp1252` encoding errors when rendering the rupee symbol `₹`, box-drawing lines, or em dashes).

---

### 10. `_env.py`
* **Purpose**: Lightweight environment variable loader for CLI scripts.
* **Key Features**:
  * Exports `parse_env_file()` and `load_env_file()`.
  * Parses key-value pairs from the repository root `.env` without introducing third-party dependencies like `python-dotenv`.
  * Preserves explicitly set shell environment variables by default without silent overrides.

---

## Recommended Execution Flow

To set up and verify a local PRAMAN environment from scratch:

```bash
# 1. Initialize schema and catalog
python scripts/init_db.py

# 2. Check initial ledger anchor
python -m scripts.anchor_chain

# 3. Verify ledger tamper resistance
python scripts/tamper_demo.py

# 4. Seed and inspect test policy offers
python -m scripts.seed_offer --list
python -m scripts.seed_offer --only tier0

# 5. Run an A/B benchmark simulation
python -m scripts.run_ab --sessions-per-arm 25
```
