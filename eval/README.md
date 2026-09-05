# PRAMAN Evaluation Framework (v3 — 9 Metrics, 369 Scenarios)

Comprehensive, adversarial evaluation harness for **PRAMAN (Policy-Restricted Autonomous Merchant Agentic Network)** across 9 core dimensions, testing both AI policy boundaries and cryptographic money rails.

---

## 1. Metric Overview & Scenario Breakdown

| Metric # | Dimension | Scenarios | Primary Invariant / Defense Layer |
|:---:|---|:---:|---|
| **1** | **Price Floor Invariance** | **52** | Bound 3 (Cost multiplier + explicit per-SKU floor). Zero breaches below ₹27,992. |
| **2** | **Discount Cap Precision** | **56** | Bound 1 (Per-SKU 12% ceiling) & Bound 2 (Cart 15% ceiling). |
| **3** | **Live Prompt Injection Defense** | **50** | Dual-Layer Defense (Strict system prompt boundary + deterministic kernel veto). |
| **4** | **Provider Failure & Fallback** | **21** | Vyapaari Fallback Ladder (429, 504, malformed JSON, missing fields, hallucinated SKUs). |
| **5** | **Dual-Gate HITL Escalation** | **54** | Bound 6: Tier 0 ≤₹2,000 (auto), Tier 1 ₹2,000–₹6,000 (mandate), Tier 2 >₹6,000 (human hold). |
| **6** | **Cryptographic Audit Trail** | **24** | HMAC-SHA256 receipt binding, database bit-level tamper detection, and hash chain integrity. |
| **7** | **Search Latency Benchmark** | **30** | Sub-200ms search SLA across Cold, Warm, and Concurrent burst queries. |
| **8** | **Incremental Basket Lift (AOV)** | **20** | Positive AOV companion upsell + Bound 10 Relatedness veto on irrelevant companions. |
| **9** | **Mandate & Payment Settlement** | **62** | 8-Stage Ed25519 mandate verifier + Razorpay direct capture and webhook settlement flows. |
| **TOTAL** | **9 Metrics Combined** | **369** | **Full System Verification** |

---

## 2. Directory Structure

```text
eval/
├── README.md                          # This comprehensive reference guide
├── run_all.py                         # Master orchestrator for single or all metrics
│
├── common/                            # Shared utilities (DRY architecture)
│   ├── scenario.py                    # Unified Scenario dataclass
│   ├── assertions.py                  # Reusable verification assertions
│   ├── client.py                      # Real Praman kernel & AI caller
│   └── report.py                      # Markdown & JSON scorecard generator
│
├── metric_01_price_floor/             # 52 Scenarios: 16 techniques x 3 SKUs + 4 Hinglish
├── metric_02_discount_cap/            # 56 Scenarios: 14 techniques x 4 categories
├── metric_03_prompt_injection/        # 50 Scenarios: 14 base + 30 structural + 6 combined
├── metric_04_provider_failure/        # 21 Scenarios: 7 modes x 3 timing contexts (A, B, C)
├── metric_05_hitl_gate/               # 54 Scenarios: Cart aggregation, split orders, precedence
├── metric_06_audit_trail/             # 24 Scenarios: 6 attack types x 4 target fields
├── metric_07_search_latency/          # 30 Scenarios: 10 shapes x Cold/Warm/Concurrent
├── metric_08_basket_lift/             # 20 Scenarios: 5 test types x 4 SKU pairings
├── metric_09_mandate_settlement/      # 62 Scenarios: 40 Part A (verifier) + 22 Part B (settlement)
│
└── reports/
    ├── scorecard.md                   # Human-readable evaluation scorecard for judges
    └── scorecard.json                 # Machine-readable evaluation scorecard
```

---

## 3. How to Run

### Run All 369 Scenarios
```bash
python eval/run_all.py
```

### Run a Single Metric Individually
```bash
# Run Metric 1 (Price Floor)
python eval/run_all.py --metric 1

# Run Metric 5 (HITL Gates)
python eval/run_all.py --metric 5

# Run Metric 9 (Mandate Verification & Settlement)
python eval/run_all.py --metric 9
```

---

## 4. Multi-Key Round-Robin Rotation Architecture

To guarantee **100% genuine LLM calls with zero fallback drops**, the test suite uses a **4-key round-robin rotation pool** on OpenRouter (`dots-studio/dots-3-note-preview:free`):
- Every scenario request automatically rotates to the next API key.
- If an individual key hits a per-minute or daily rate limit (**HTTP 429**), the client immediately fails over to the next key in the pool without dropping the request to fallback.
- Combined capacity: **4 keys × 50 requests/day = 200 live requests**, giving ample headroom.

---

## 5. Honest Reporting Standard (Documented Architectural Findings)

Praman follows an honest reporting standard:
- **Metric 5 Split-Order Cart Evasion**:
  3 separate orders of ₹1,999 evaluate independently against the cart limit (each ₹1,999 ≤ ₹2,000), clearing under Tier 0 (auto-proceed without mandate). The session aggregate is ₹5,997.
  This is documented honestly in the scorecard under **Architectural Findings & Limitations** to clearly distinguish per-transaction policy boundaries from multi-order session velocity governance.
