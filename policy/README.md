# Policy Engine & Economic Governance (`policy`)

> **Simple:** The merchant's rulebook. The merchant sets "floor = cost/(1-margin)%, max discounts, what AI can negotiate" here. `resolver.py` merges `STORE→CATEGORY→SKU→CAMPAIGN` rules, `pre_filter` drops impossible deals early, `optimizer` picks the best valid one, `negotiation` counters bad buyer prices.

The `policy` package serves as the **merchant economic governance and deterministic rule execution core** of PRAMAN. It provides the algorithmic guardrails, economic optimization, autonomous negotiation logic, transaction state machines, and cryptographic audit trail mechanisms required for autonomous agentic commerce.

## Simple — what each file does & who it calls

| File | Plain job | Connected to |
|---|---|---|
| `mec.py` | Defines MEC dataclasses (`HardConstraints`, `EconomicObjectives`, `NegotiationPermissions`) + `compute_hash()` | `policy/resolver.py`, `store/mec_store.py` |
| `resolver.py` | Merges hierarchy: child overrides + `AND` narrows permissions → `EffectivePolicy` | `store/mec_store.py`, `kernel/bounds.py` |
| `snapshot.py` | Freezes `EffectivePolicy` snapshot into TDR for audit | `policy/tdr.py`, `store/tdr_store.py` |
| `pre_filter.py` | Drops candidates failing floor/discount/stock/SKU existence (before optimizer) | `policy/mec.py`, `kernel/bounds.py` |
| `optimizer.py` | Scores valid deals `w_m·M+w_c·C+w_a·A+w_i·I` (margin, conversion sigmoid, AOV, inventory age) | `policy/mec.py` weights |
| `negotiation.py` | Up to 3 rounds: computes `floor=cost/(1−margin%)`, offers floor or reduced qty | `policy/mec.py` |
| `states.py` | `TransactionState` FSM `INTENT→PROPOSING→DEAL_PROPOSED→APPROVED→HELD→CAPTURED→COMMITTED` | `kernel/checkout.py` |
| `tdr.py` | `build_tdr()` / `verify_tdr()` binds intent+cart+policy+payment with hash | `store/timestamps.py`, `store/canonical.py` |
| `core_safety.py` | 8 invariants `check_all_pre/post_payment` (fail-closed) | `kernel/checkout.py` |

---

## 🛡️ Guiding Principles & Architecture

1. **Pure Business Logic & Math**: This package never handles sensitive payment credentials (e.g., API keys, CVVs, card tokens) nor does it perform external network I/O. It operates strictly on deterministic mathematical models, invariant verifications, and structured data structures.
2. **Determinism over LLM Hallucinations**: LLM agents (such as Vyapaari) can propose deals or initiate negotiations, but no generative AI output can directly authorize money movement or alter merchant boundaries.
3. **Cryptographic Immutability**: Policies, cart snapshots, and transaction decisions are canonicalized and hashed (SHA-256) into immutable Transaction Decision Records (TDRs).

```
                      ┌───────────────────────────────────────────────┐
                      │          Merchant Economic Constitution        │
                      │         (Store / Category / SKU / Camp)       │
                      └──────────────────────┬────────────────────────┘
                                             │
                                             ▼
                                  ┌────────────────────┐
                                  │ Effective Policy   │
                                  │    Resolver        │
                                  └──────────┬─────────┘
                                             │
                       ┌─────────────────────┴──────────────────────┐
                       │                                            │
                       ▼                                            ▼
           ┌──────────────────────┐                     ┌──────────────────────┐
           │ Candidate Pre-Filter │                     │ Autonomous Negotiator│
           │ (Hard Constraints)   │                     │ (Multi-round Engine) │
           └───────────┬──────────┘                     └──────────┬───────────┘
                       │                                           │
                       ▼                                           │
           ┌──────────────────────┐                                │
           │  Economic Optimizer  │                                │
           │ (Multi-objective Fn) │                                │
           └───────────┬──────────┘                                │
                       │                                           │
                       └─────────────────────┬─────────────────────┘
                                             │
                                             ▼
                               ┌───────────────────────────┐
                               │  Core Safety Invariants   │
                               │      (Invariants 1-8)     │
                               └─────────────┬─────────────┘
                                             │
                                             ▼
                               ┌───────────────────────────┐
                               │ Transaction Decision      │
                               │ Record (TDR) & States     │
                               └───────────────────────────┘
```

---

## 📁 File-by-File Overview

| File | Purpose | Key Responsibilities & Functions |
|---|---|---|
| [`__init__.py`](./__init__.py) | Package Entry Point | Exposes high-level data structures, enums, and classes (`MEC`, `HardConstraints`, `EffectivePolicy`, `TransactionState`, etc.) for consumers of the `policy` package. |
| [`core_safety.py`](./core_safety.py) | System Invariant Verification | Enforces the 8 unbreakable commerce safety invariants. Provides pre-payment (`check_all_pre_payment`) and post-payment (`check_all_post_payment`) verification checkpoints. |
| [`mec.py`](./mec.py) | Merchant Economic Constitution Models | Defines data models for merchant constraints (`HardConstraints`, `EconomicObjectives`, `NegotiationPermissions`, `ApprovalThresholds`, `MECScope`) and canonical SHA-256 hashing. |
| [`resolver.py`](./resolver.py) | Effective Policy Hierarchical Resolver | Merges hierarchical rule scopes (`STORE` -> `CATEGORY` -> `SKU` -> `CAMPAIGN`). Narrows negotiation permissions (using logical AND) and applies overrides. |
| [`snapshot.py`](./snapshot.py) | Immutable Policy Snapshots | Captures frozen policy instances (`EffectivePolicy`, `PolicySource`) embedded within transaction records to guarantee post-hoc auditability. |
| [`pre_filter.py`](./pre_filter.py) | Candidate Deal Fast Rejection | Fast rejection pipeline validating candidate deals against margin floors, discount ceilings, valid SKUs, and inventory stock levels. |
| [`optimizer.py`](./optimizer.py) | Multi-Objective Economic Optimization | Evaluates and ranks valid candidate deals using weighted objective scoring (Margin, Conversion Probability, AOV, Inventory Velocity). |
| [`negotiation.py`](./negotiation.py) | Autonomous Negotiation Engine | Manages multi-round buyer-merchant bargaining sessions up to a maximum round limit (`MAX_NEGOTIATION_ROUNDS = 3`), computing merchant floors and automated counter-offers. |
| [`states.py`](./states.py) | Transaction Lifecycle State Machine | Defines valid finite-state machine transitions (`TransactionState`, `VALID_TRANSITIONS`), recovery paths, and terminal state validation. |
| [`tdr.py`](./tdr.py) | Transaction Decision Record (TDR) | Canonical data structure and verification routines (`build_tdr`, `verify_tdr`) binding intent, cart snapshot, policy hash, economic decision, inventory reservation, and payment reference. |

---

## 🔍 Detailed Component Descriptions

### 1. `core_safety.py`
Enforces the fundamental invariants that ensure financial safety in automated transactions:
- **Invariant 1**: No LLM output can directly trigger money movement.
- **Invariant 2**: Every payment must reference an approved deterministic policy verdict.
- **Invariant 3**: The amount executed must exactly match the amount approved.
- **Invariant 4**: The executed cart hash must match the authorized cart hash.
- **Invariant 5**: Inventory must be reserved (`HELD` or `COMMITTED`) before payment commit.
- **Invariant 6**: Every state-changing operation must include an idempotency key.
- **Invariant 7**: Completed transactions must produce a reconstructable decision record.
- **Invariant 8**: Execution must reference an immutable approved TDR with matching amounts and cart hash.

### 2. `mec.py` & `resolver.py`
The **Merchant Economic Constitution (MEC)** defines the merchant's business boundaries:
- **Hierarchical Scopes**: Rules cascade from `STORE` $\rightarrow$ `CATEGORY` $\rightarrow$ `SKU` $\rightarrow$ `CAMPAIGN`.
- **Constraint Overrides**: Child scopes can override specific field values (e.g., lower max discount on specific high-demand SKUs).
- **Permission Narrowing**: `NegotiationPermissions` (price, quantity, bundles, substitutes, shipping, delivery date) only restrict further in child scopes (`parent AND child`), preventing unauthorized permission expansion.
- **Objective Weighting**: `EconomicObjectives` validates that objective weights (`margin_weight`, `conversion_weight`, `aov_weight`, `inventory_velocity_weight`) sum exactly to `1.0`.

### 3. `pre_filter.py` & `optimizer.py`
High-speed deterministic filtering and scoring:
- **Pre-Filtering**: Rejects invalid candidate deals by checking SKU validity, per-SKU discount limits, cart discount limits, stock availability, and margin floors:
  $$\text{Floor Price} = \frac{\text{Variable Cost}}{1 - \frac{\text{Min Margin \%}}{100}}$$
- **Optimization Function**: Valid candidates are scored against merchant objectives:
  $$\text{Score}(d) = w_m \cdot M(d) + w_c \cdot C(d) + w_a \cdot A(d) + w_i \cdot I(d)$$
  Where:
  - $M(d)$: Margin score based on revenue vs. variable cost.
  - $C(d)$: Sigmoid conversion score based on buyer budget proximity.
  - $A(d)$: Average Order Value (AOV) normalized to max candidate revenue.
  - $I(d)$: Inventory velocity score prioritizing older inventory with lower demand velocity.

### 4. `negotiation.py`
Autonomous negotiation handling:
- Computes acceptable floor price per unit and total order.
- Generates intelligent counter-offers:
  1. Best price at requested quantity (floor price).
  2. Reduced quantity fitting within the buyer's stated budget.
- Limits negotiations to 3 rounds; escalates to human review or terminates if no mutually feasible deal exists.

### 5. `states.py`
Explicit transaction lifecycle state machine:
- **Happy Path**: `INTENT_CREATED` $\rightarrow$ `PROPOSING` $\rightarrow$ `NEGOTIATING` $\rightarrow$ `DEAL_PROPOSED` $\rightarrow$ `APPROVED` $\rightarrow$ `RESERVATION_HELD` $\rightarrow$ `PAYMENT_PENDING` $\rightarrow$ `PAYMENT_CAPTURED` $\rightarrow$ `COMMITTED` $\rightarrow$ `FULFILLED`.
- **Compensation & Failure Paths**: Supports transitions to `EXPIRED`, `RESERVATION_EXPIRED`, `PAYMENT_FAILED`, `INVENTORY_COMMIT_FAILED`, `COMPENSATING`, `REFUNDED`, and `NO_FEASIBLE_DEAL`.

### 6. `tdr.py` & `snapshot.py`
Auditability and record immutability:
- **EffectivePolicy Snapshot**: Records the exact policy configuration, source chain, and timestamp that authorized the transaction.
- **TDR (Transaction Decision Record)**: Binds `intent_id`, `buyer_authority`, `policy_reference`, `cart_snapshot`, `economic_decision`, `reservation_ref`, and `payment_ref`.
- **Cryptographic Verification**: Re-computes and matches canonical JSON SHA-256 hashes before finalizing transactions or auditing historical deals.
