# PRAMAN Test Harness (`harness/`) — the buyer who shops against you

> **Simple:** This folder is NOT the merchant — it is the shopper. `Wallet` holds the budget and signs permission slips; `Grahak` browses → asks for offers → checks out over HTTP just like a real AI buyer would. `ab.py` runs 200 vs 200 experiments to see if upsells help.

The `harness/` package implements the **buyer side** (counterparty) and the **measurement rigs** for the PRAMAN agentic commerce platform.

## Simple — what each file does & who it calls (over HTTP)

| File | Plain job | Calls |
|---|---|---|
| `grahak.py:Wallet` | Holds `max_amount/max_single/categories/ttl`, signs Ed25519 mandate scoped to cart | `mandate/signer.py` |
| `grahak.py:Grahak` | 5-step rail: `discover()` → `browse()` → `request_offer()` → `buy()` → `check()/accept_counter()` | `/.well-known/agent-commerce.json`, `/agent/v1/{catalog,offer,checkout,order/*}` |
| `grahak.py:PERSONAS` | 8 personas (budget_tight … deadline_driven) with budgets ₹2000–∞ and strategies | `Grahak.shop_as()` |
| `ab.py` | `control` (base only) vs `treatment` (persona choice), `run_session/run_ab/summarize` metrics | `harness/grahak.py` |

---

## Overview & Architectural Principles

Nothing in this package is part of the merchant system. Instead, it serves as the autonomous external counterparty that interacts with the store strictly over standard HTTP rails (discovery, catalog search, offer requests, checkout, order polling, and counter-offer acceptance).

```
 ┌─────────────────────────────────────────────────────────────┐
 │                       harness/ (Buyer)                      │
 │                                                             │
 │   ┌───────────────┐                  ┌──────────────────┐   │
 │   │    Wallet     │─── signs token ─▶│      Grahak      │   │
 │   │ (holds limit) │                  │  (Buyer Agent)   │   │
 │   └───────────────┘                  └─────────┬────────┘   │
 └────────────────────────────────────────────────┼────────────┘
                                                  │ HTTP Requests
                                                  ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                    store/ & kernel/ (Merchant)              │
 │                                                             │
 │   /.well-known/agent-commerce.json  (Discovery)             │
 │   /agent/v1/catalog                 (Browse & Search)       │
 │   /agent/v1/offer                   (Bounded Proposals)     │
 │   /agent/v1/checkout                (Gate, Policy, Order)   │
 │   /agent/v1/order/{id}              (Poll & Counter-Offers) │
 └─────────────────────────────────────────────────────────────┘
```

### Core Invariants:

1. **Strict Counterparty Boundary**:
   The buyer agent never imports merchant constants (`settings.py`, merchant database models, or private profit margin tables). It discovers routes and policies dynamically via `/.well-known/agent-commerce.json`.
2. **Separation of Agent and Wallet**:
   The `Grahak` agent holds no cryptographic keys and cannot mint spending authority. Spending limits reside in `Wallet`, which signs single-use, scoped Ed25519 mandate tokens only after verifying client-side constraints.
3. **No Price Stating by the Buyer**:
   The buyer specifies only the `offer_id` and `option_id`. The merchant's stored database row remains the authoritative source for prices.
4. **Hermetic Shadow-Mode Testing**:
   Simulation runs execute under `POLICY_MODE=shadow`, ensuring all bounds, tier gates, receipts, and ledger entries fire without moving real money.

---

## Files in `harness/`

### 1. `__init__.py`
* **Purpose**: Package initialization and architectural declaration.
* **Details**: Explicitly documents the package boundary as the counterparty and measurement instrumentation, exposing `grahak.py` and `ab.py`.

---

### 2. `grahak.py` — The Autonomous Buyer Agent & Wallet
* **Purpose**: Implements the buyer agent (`Grahak`), the signing wallet (`Wallet`), response data structures, and synthetic buyer personas.
* **Key Components**:
  * **`Wallet`**:
    * Holds spending limits (`max_amount_inr`, `max_single_txn_inr`, `allowed_categories`, `ttl_seconds`).
    * Issues single-use signed mandates via `mandate.signer` scoped strictly to cart categories and transaction value.
    * Rejects purchases exceeding human limits before any request reaches the merchant wire (`WalletRefused`).
  * **`Grahak`**:
    * Orchestrates the 5-step commerce rail:
      1. `discover()`: Reads `/.well-known/agent-commerce.json` for paths, latency hints, and mandate thresholds.
      2. `browse(need, ...)`: Queries `/agent/v1/catalog` to inspect products and learn SKU-to-category associations.
      3. `request_offer(need, ...)`: Submits buyer intent to `/agent/v1/offer` and receives bounded options.
      4. `buy(offer, option_id, ...)`: Submits order to `/agent/v1/checkout`, attaching signed mandates when gate requirements dictate.
      5. `check(order_id)` / `accept_counter(order_id, ...)`: Polls held/countered orders and accepts modified terms.
      6. `shop(need, ...)` / `shop_as(persona)`: High-level composed helpers executing the entire flow.
  * **Response Models**:
    * `Discovery`: Encapsulates published manifest metadata.
    * `Offer`: Encapsulates multi-option quotes with selection helpers (`recommended`, `cheapest`, `within(budget)`).
    * `Purchase`: Models checkout outcomes (order ID, gate tier, policy mode, Razorpay payload, approval state).
  * **Personas (`Persona` & `PERSONAS`)**:
    * 8 diverse buyer profiles exercising varied operational scenarios:
      | Persona | Intent / Description | Budget / Qty | Choice Strategy |
      |---|---|---|---|
      | `budget_tight` | Commuter earbuds | ₹2,000 max | `cheapest` |
      | `feature_led` | Over-ear ANC headphones | Unbounded | `recommended` |
      | `gift_buyer` | Returnable premium headphones | ₹6,000 max | `budget` |
      | `bulk` | Support team headsets | 8 units | `cheapest` |
      | `brand_agnostic` | Wireless earphones | Unbounded | `recommended` |
      | `upgrade_seeker` | Studio-grade upgrade | Unbounded | `recommended` |
      | `replacement_part` | Silicone earbud tips | ₹2,000 max | `cheapest` |
      | `deadline_driven` | Express wired headset | ₹4,000 max | `budget` |

---

### 3. `ab.py` — The A/B Measurement Rig
* **Purpose**: Conducts rigorous, empirical A/B experiments measuring real revenue lift, conversion variations, and upsell take-rates.
* **Key Components**:
  * **Two-Arm Experiment Design**:
    * `control`: The buyer selects only the minimalist base item (`_base_only_option`).
    * `treatment`: The buyer evaluates the full merchandising proposal according to its persona rule (`choose()`).
  * **`SessionResult`**:
    * Dataclass capturing session outcomes (`arm`, `persona`, `completed`, `order_id`, `basket_inr`, `upsells_shown`, `upsells_taken`, `discount_inr`, `error`).
  * **`run_session(...)`**:
    * Executes a single end-to-end shopping attempt over HTTP.
    * Refusals (`StoreRefused`, `WalletRefused`) are recorded as structured data points rather than runtime crashes.
  * **`run_ab(...)`**:
    * Runs multi-session batches across rotated personas to ensure balanced population distribution between arms.
  * **`summarize(...)`**:
    * Computes per-arm aggregate metrics: total sessions, completed orders, conversion rate, total revenue (INR), Average Order Value (AOV), attach rate, upsells taken, and total discounts awarded.

---

## Usage Examples

### 1. Running a Buyer Agent Session
```python
from fastapi.testclient import TestClient
from api.app import create_app
from harness.grahak import Grahak, PERSONAS_BY_NAME

client = TestClient(create_app())
persona = PERSONAS_BY_NAME["budget_tight"]

agent = Grahak(client)
offer, purchase = agent.shop_as(persona)

print(f"Order: {purchase.order_id}, Status: {purchase.status}, Amount: ₹{purchase.amount_inr}")
```

### 2. Running the A/B Experiment via CLI
```bash
# Run full 400-session experiment (200 control, 200 treatment in shadow mode)
python -m scripts.run_ab

# Quick trial run with 8 sessions per arm
python -m scripts.run_ab --sessions-per-arm 8

# Output JSON report
python -m scripts.run_ab --json
```

### 3. Running Harness Tests
```bash
pytest tests/test_grahak_personas.py
pytest tests/test_ab_harness.py
```
