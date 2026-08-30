# PRAMAN Policy Kernel (`kernel/`)

> **Simple:** The bouncer and cashier. `vyapaari` suggests a deal, this folder decides if it is allowed. No AI lives here — just math and yes/no rules. If a deal is bad (too cheap, no stock, too big), it blocks it and writes the reason to `ISSUES.md`. If good, it runs the 11-step money path and calls Razorpay last.

The **Policy Kernel** is the deterministic authority and financial execution core of PRAMAN. It acts as an uncompromising boundary between untrusted AI generation (LLM proposals from `vyapaari/`) and real-world commercial actions (inventory holds, ledger commits, and Razorpay payment transactions).

## Simple — what each file does & who it calls

| File | Plain job | Who it talks to |
|---|---|---|
| `offer.py` | Turns LLM proposal → Offer A/B, checks 10 bounds, scrubs prose, signs receipt | `vyapaari/proposer.py`, `kernel/bounds.py`, `kernel/gates.py`, `kernel/receipt.py`, `store/offers.py` |
| `bounds.py` | 10 yes/no veto rules (discount, floor `cost×1.20`, budget, stock, TTL, idempotency, relatedness) | `store/*`, `kernel/relations.py` |
| `gates.py` | Picks Tier 0/1/2 (auto / mandate / human) | `kernel/bounds.py`, `mandate/verifier.py` |
| `checkout.py` | 11-step money path: idempotency → re-check bounds → hold stock → ledger intent → Razorpay → commit | `store/offers.py`, `store/orders.py`, `kernel/stock.py`, `kernel/payments.py`, `store/ledger.py` |
| `stock.py` | `AVAILABLE → HELD (120s TTL) → COMMITTED`, formula `available = on-hand − live holds` | `store/db.py` `stock_holds` table |
| `payments.py` | Only file with Razorpay keys; converts ₹↔paise; HMAC checks | Razorpay API |
| `receipt.py` | HMAC-SHA256 signature over offer verdicts/gate/totals | `store/canonical.py` |
| `saga.py` | If stock vanishes after payment, refunds + heals SKU | `kernel/payments.py`, `store/catalog.py`, `store/ledger.py` |
| `approvals.py` | Tier-2 hold queue — no timeout that approves | `store/approvals.py` |
| `search.py` | Keyword search (no AI) | `store/catalog.py` `CatalogCache` |
| `budgets.py` / `idempotency.py` / `mode.py` / `reasons.py` / `relations.py` | Budget, double-charge guard, shadow/live switch, prose scrub, pairing evidence | `store/*`, `settings.py` |

---

## 🏛 Core Architectural Philosophy & Invariants

1. **Deterministic Authority**:
   - The policy kernel contains **zero LLMs, zero prompt engineering, and zero non-deterministic logic**.
   - Every financial threshold, bound check, and authorization gate is evaluated via pure Python functions and exact integer/Decimal arithmetic.

2. **The Import Boundary**:
   - `kernel.payments` is the **only** module across the entire project holding payment credentials and communicating with the payment gateway (Razorpay).
   - AST-based boundary tests enforce that code in `vyapaari/` can never import `kernel.payments` directly or bypass the policy kernel.

3. **Immutable Auditing & Policy Receipts**:
   - Every offer and checkout decision produces a cryptographic HMAC-SHA256 **Policy Receipt** (`receipt.py`) binding the item verdicts, reason strings, gate tiers, and calculation inputs prior to executing any financial transaction.

4. **Shadow vs. Live Execution (`mode.py`)**:
   - In `SHADOW` mode, every bound, gate, and policy receipt runs identically, but external side effects (Razorpay calls, inventory commits, budget spending) are strictly prohibited at the kernel level.

---

## 🔄 Core Lifecycles & Execution Flow

```
[ Model Proposal (vyapaari) ]
              │
              ▼
    1. Offer Assembly (`offer.py`)
              │
              ├──► 10 Bound Checks (`bounds.py` + `relations.py`)
              ├──► Outbound Prose Scrubbing (`reasons.py`)
              ├──► Gate Tier Assignment (`gates.py`)
              └──► HMAC-SHA256 Policy Receipt Issuance (`receipt.py`)
              │
              ▼
    2. Checkout Execution (`checkout.py`)
              │
              ├──► 1. Idempotency Claim (`idempotency.py`)
              ├──► 2. Offer Freshness & Revalidation (`bounds.py`)
              ├──► 3. Gate Tier Verification:
              │       ├─ Tier 0: Auto-proceed
              │       ├─ Tier 1: AP2 Mandate Verification (`mandate/`)
              │       └─ Tier 2: Human Approval Halt (`approvals.py`)
              ├──► 4. Atomic Stock Hold (`stock.py`)
              ├──► 5. Pre-Charge Ledger Intent Commit (`store/ledger.py`)
              ├──► 6. Razorpay Order Creation & Capture (`payments.py`)
              ├──► 7. Post-Capture Stock Commitment (`stock.py`)
              │       └─ [If Oversold Race Condition] ──► Compensating Saga (`saga.py`)
              ├──► 8. Daily Discount Budget Accrual (`budgets.py`)
              └──► 9. Payment Confirmation & Public Audit URL
```

---

## 📁 File Descriptions & Module Breakdown

### 1. `__init__.py`
- **Purpose**: Defines the policy kernel package and outlines core architectural boundaries.
- **Key Invariants**: Documents that pure Python rules govern execution, that LLM components cannot make financial decisions, and that `payments.py` is the sole custodian of payment credentials.

---

### 2. `approvals.py`
- **Purpose**: Implements the Tier-2 Human Merchant Approval state machine and decision handling for held orders.
- **Key Concepts**:
  - **Decisions**: `APPROVE`, `REJECT`, or `COUNTER`.
  - **No Timeout Approvals**: Pending human approvals never auto-expire into an approval. Unresolved orders never charge or ship.
  - **Zero Stock Held While Pending**: Held orders do not lock inventory (which would lock stock indefinitely). Stock is revalidated upon human approval.
  - **Counter Offers**: A counter-offer voids the original terms and issues a new cryptographic offer that the buyer agent must explicitly accept.

---

### 3. `bounds.py`
- **Purpose**: The kernel's 10-point veto surface. Pure mathematical functions comparing proposals against hard-coded policy constraints.
- **The 10 Bounds**:
  1. **Bound 1 (`check_max_discount_pct_per_sku`)**: Enforces the maximum permissible discount percentage per SKU.
  2. **Bound 2 (`check_max_cart_discount_pct`)**: Enforces the overall cart discount ceiling.
  3. **Bound 3 (`check_floor_price`)**: Enforces cost-derived floor price ($Cost \times 1.20$ minimum margin). Limit is private and never leaked in public receipts.
  4. **Bound 4 (`check_daily_discount_budget`)**: Enforces remaining daily merchant discount budget (in INR).
  5. **Bound 5 (`check_max_offers_per_session`)**: Limits maximum proposal rounds per buyer session.
  6. **Bound 6 (`check_max_txn_without_human`)**: **Gating Bound** — Cart totals exceeding this limit trigger human merchant approval (Tier 2).
  7. **Bound 7 (`check_stock_available`)**: Ensures uncommitted, unreserved inventory is available above safety thresholds.
  8. **Bound 8 (`check_offer_fresh`)**: Ensures checkout occurs within the 300-second offer TTL window.
  9. **Bound 9 (`check_idempotency_key`)**: Requires a non-empty, unique client idempotency key.
  10. **Bound 10 (`check_relatedness`)**: Validates that upsell items relate to the base product using learned and declared pairings.
- **Key Design**: Exact integer/Decimal math to avoid floating-point rounding vulnerabilities.

---

### 4. `budgets.py`
- **Purpose**: Atomic tracking and accounting of the daily discount budget (Bound #4).
- **Key Features**:
  - **Atomic SQL Upserts**: Accrues discount spending atomically via SQLite transactions (`ON CONFLICT DO UPDATE`), preventing race conditions when concurrent checkouts occur.
  - **Read/Write Separation**: Read-only queries (`would_exceed`, `remaining`, `spent`) never lock or mutate budget state.
  - **UTC Day Accounting**: Enforces daily rollover keyed on UTC dates.

---

### 5. `checkout.py`
- **Purpose**: The central checkout orchestrator managing the immutable money execution sequence.
- **Execution Order**:
  1. Idempotency key reservation.
  2. Offer loading and freshness validation.
  3. Re-running all bounds and mandate/tier checks from scratch.
  4. Atomic stock hold reservation.
  5. Ledger intent recorded *before* network calls.
  6. Razorpay gateway order creation and payment capture.
  7. Stock commitment against the reservation.
  8. Final ledger confirmation.
- **Security Invariant**: Price is always sourced server-side from the cryptographic offer record; client-submitted prices are ignored to prevent price tampering.

---

### 6. `gates.py`
- **Purpose**: Assigns authority tiers based on cart total value, discount depth, and risk criteria.
- **Authority Tiers**:
  - **Tier 0 (`auto`)**: Transactions under ₹2,000 with $\le 5\%$ discount. Can proceed autonomously.
  - **Tier 1 (`mandate`)**: Transactions between ₹2,000 and ₹6,000 (or discounts $>5\%$). Requires verification of a signed AP2 mandate.
  - **Tier 2 (`human`)**: Transactions over ₹6,000 (Bound 6 trip) or discounts $>8\%$. Halts execution for human merchant approval.

---

### 7. `idempotency.py`
- **Purpose**: Anti-replay and double-charge protection layer.
- **Key Features**:
  - Pre-execution key claiming via atomic SQL inserts before reaching external payment gateways.
  - Canonical JSON request fingerprinting (`fingerprint()`) to detect payload mutations reusing identical keys (`FingerprintMismatch`).
  - Handling of in-flight crashes (`RequestInFlight`) to safely prevent uncoordinated retries while payment status is unknown.

---

### 8. `mode.py`
- **Purpose**: System-wide policy execution switch between `LIVE` and `SHADOW` modes.
- **Key Features**:
  - Gated guard `assert_may_move_money(action)` called directly before any money-moving operation.
  - Guarantees that in shadow mode, bounds and receipts are fully evaluated and recorded to the ledger as `would_have_charged`, while payments and stock decrements are strictly blocked.

---

### 9. `offer.py`
- **Purpose**: Assembles validated, store-binding commercial offers from agent proposals.
- **Key Responsibilities**:
  - Maps model proposals into Option A (base item requested) and Option B (bundle with approved upsells).
  - Rounds prices to integer rupees toward the merchant.
  - Prunes invalid or non-compliant upsells while preserving the base offer.
  - Scans model prose through `reasons.py`.
  - Computes HMAC-SHA256 policy receipts for all surviving options.

---

### 10. `payments.py`
- **Purpose**: Thin, secure Razorpay HTTP client for order creation, payment capture, and refunds.
- **Key Invariants**:
  - Sole module holding `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET`.
  - Strict currency boundary: Internal kernel arithmetic operates in whole rupees (`INR`); conversion to/from Razorpay paise occurs exclusively here (`_to_paise`, `_to_rupees`).
  - Constant-time HMAC-SHA256 webhook signature verification (`verify_webhook_signature`).

---

### 11. `reasons.py`
- **Purpose**: Outbound prose moderation and safety boundary for buyer-facing text.
- **Key Safeguards**:
  - **Leak Prevention**: Scans generated text for confidential numbers (exact cost, floor price, attach rates) and private schema fields.
  - **Forbidden Phrase Filtering**: Blocks internal merchant terms (`margin`, `wholesale`, `cost price`, `system prompt`, prompt injection markers).
  - **Deterministic Fallback**: Automatically replaces non-compliant prose with clean, pre-approved merchant explanation templates (`render_upsell_reason`).

---

### 12. `receipt.py`
- **Purpose**: Cryptographic Policy Receipt generation and verification.
- **Structure**:
  - Signs canonical JSON representation of `receipt_id`, `offer_id`, `issued_at`, `gate_tier`, `policy_mode`, `verdicts`, `reasons`, `gate`, `totals`, and `exploration` traces.
  - Uses HMAC-SHA256 with server-side secrets and constant-time verification (`hmac.compare_digest`).
  - Ensures merchants and buyers have tamper-evident proof of why a transaction was permitted or rejected before payment execution.

---

### 13. `relations.py`
- **Purpose**: Aggregates product pairing evidence for Bound #10 (Relatedness).
- **Evidence Sources**:
  1. **Learned Pairings** (`store.pairings`): Baskets historically purchased together exceeding confidence/frequency thresholds.
  2. **Declared Companions**: Merchant-curated attach candidates and tier-up SKUs in catalog metadata.
  - Intentionally excludes coarse category matching to prevent nonsensical pairings.

---

### 14. `saga.py`
- **Purpose**: Compensating transaction saga managing post-capture inventory oversell race conditions.
- **Execution Flow**:
  1. `fulfillment.check`: Detects unfulfillable inventory after Razorpay capture.
  2. `saga.compensation_triggered`: Initiates automated remediation.
  3. `razorpay.refund`: Executes an immediate, automatic 100% refund (`OVERSOLD_MERCHANT_FAULT`).
  4. `ledger.compensate`: Links the capture and refund on the immutable ledger.
  5. `policy.selfheal`: Disables the oversold SKU in catalog storage until restocked.
  6. `notify.buyer` & `notify.merchant`: Dispatches structured failure notifications explaining next steps.

---

### 15. `search.py`
- **Purpose**: Fast, fully deterministic catalog keyword search.
- **Key Features**:
  - Zero embeddings, zero network, zero LLMs (sub-millisecond catalog resolution).
  - Uses field-weighted token scoring, stopword pruning, and a curated one-way synonym expansion dictionary.

---

### 16. `stock.py`
- **Purpose**: High-concurrency inventory reservation and hold management.
- **Key Features**:
  - **Live Availability Formula**: $\text{Available} = \text{Stock on Hand} - \text{Live Active Holds}$.
  - **TTL-Enforced Holds**: 120-second stock hold reservation window.
  - **All-or-Nothing Holds**: Multi-item cart holds roll back entirely if any single SKU cannot be satisfied.
  - **Two Commit Modes**: Strict `commit` (pre-capture) and `commit_settled` (post-capture with oversell detection).
