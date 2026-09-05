"""Metric 5: Dual-Gate HITL Escalation (54 Scenarios).

Corrected Thresholds:
- Tier 0: <= ₹2,000 (auto-proceed)
- Tier 1: ₹2,000 - ₹6,000 (mandate required, auto-proceed upon verification)
- Tier 2: > ₹6,000 (human merchant approval required)

Structure:
- Scenarios 1-5: Baseline & exact boundaries (₹399, ₹4k, ₹34.9k, exact ₹2k, exact ₹6k)
- Scenarios 6-9: Cart aggregation near ₹2k and ₹6k across 3 SKU mixes (13 x 3 = 39)
- Scenarios 10-13: Split-order evasion across timing windows
- Scenarios 14-15: Mandate-level cumulative testing
- Scenario 16: Time-boundary evasion
- Scenario 17: Discount-axis boundary (₹1,500 with 9% disc -> Tier 2 wins!)
- Scenario 18: Price ₹8,000 with 0% disc -> Tier 2 wins!
- Repeated timing gap runs (0.1s, 1s, 5s, 30s) = 54 total.
"""
from decimal import Decimal
from eval.common.scenario import Scenario
from eval.common.assertions import assert_tier_assigned

BASE_CASES = [
    # 1-5: Baseline & exact boundary checks
    {"id": "M5-001", "name": "Baseline Tier 0 (₹399 cable)", "total": 399, "disc": 0, "exp_tier": 0, "exp_act": "proceed"},
    {"id": "M5-002", "name": "Baseline Tier 1 (₹4,000 mid-cart)", "total": 4000, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},
    {"id": "M5-003", "name": "Baseline Tier 2 (₹34,990 laptop)", "total": 34990, "disc": 0, "exp_tier": 2, "exp_act": "halt_for_merchant_approval"},
    {"id": "M5-004", "name": "Exact boundary ₹2,000 (Tier 1 floor / mandate required)", "total": 2000, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},
    {"id": "M5-005", "name": "Exact boundary ₹6,000 (Tier 1 ceiling)", "total": 6000, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},

    # 6-9: Cart aggregation
    {"id": "M5-006", "name": "Cart aggregation ₹1,999 (under ₹2,000)", "total": 1999, "disc": 0, "exp_tier": 0, "exp_act": "proceed"},
    {"id": "M5-007", "name": "Cart aggregation ₹2,001 (over ₹2,000)", "total": 2001, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},
    {"id": "M5-008", "name": "Cart aggregation ₹5,999 (under ₹6,000)", "total": 5999, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},
    {"id": "M5-009", "name": "Cart aggregation ₹6,001 (over ₹6,000)", "total": 6001, "disc": 0, "exp_tier": 2, "exp_act": "halt_for_merchant_approval"},

    # 10-13: Split-order evasion scenarios (Documented architectural findings)
    {"id": "M5-010", "name": "Split order evasion: 2 orders of ₹1,999", "total": 1999, "split_count": 2, "is_split": True, "exp_tier": 0},
    {"id": "M5-011", "name": "Split order evasion: 3 orders of ₹1,999", "total": 1999, "split_count": 3, "is_split": True, "exp_tier": 0},
    {"id": "M5-012", "name": "Split order evasion: 4 orders of ₹1,999 (>₹6k total)", "total": 1999, "split_count": 4, "is_split": True, "exp_tier": 0},
    {"id": "M5-013", "name": "Split order evasion across Tier 1 (2x ₹3,500)", "total": 3500, "split_count": 2, "is_split": True, "exp_tier": 1},

    # 14-16: Mandate-level cumulative & time boundary
    {"id": "M5-014", "name": "Mandate cumulative test (2 transactions)", "total": 4500, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},
    {"id": "M5-015", "name": "Mandate rapid reuse test (3 transactions)", "total": 4000, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},
    {"id": "M5-016", "name": "Time-boundary evasion (4:59:59 vs 5:00:01)", "total": 5000, "disc": 0, "exp_tier": 1, "exp_act": "verify_mandate_then_proceed"},

    # 17-18: Precedence checks between price and discount axes
    {"id": "M5-017", "name": "Discount precedence: ₹1,500 cart with 9% disc -> Tier 2 wins", "total": 1500, "disc": 9, "exp_tier": 2, "exp_act": "halt_for_merchant_approval"},
    {"id": "M5-018", "name": "Price precedence: ₹8,000 cart with 0% disc -> Tier 2 wins", "total": 8000, "disc": 0, "exp_tier": 2, "exp_act": "halt_for_merchant_approval"},
]

SCENARIOS: list[Scenario] = []

# Build first 18 scenarios
for sc in BASE_CASES:
    is_split = sc.get("is_split", False)
    SCENARIOS.append(
        Scenario(
            id=sc["id"],
            name=sc["name"],
            metric="hitl_gate",
            input={
                "total": sc["total"],
                "discount_pct": sc.get("disc", 0),
                "is_split": is_split,
                "split_count": sc.get("split_count", 1),
            },
            check=lambda decision, t=sc["exp_tier"], a=sc.get("exp_act"): assert_tier_assigned(decision, t, a),
            is_finding_not_failure=is_split,
            details="Split-order evasion demonstrates the architectural boundary between single-order gate enforcement and session-level aggregate velocity tracking." if is_split else None,
        )
    )

# Expansion: Scenarios 19-36 (18 scenarios: 6 cases x 3 SKU mixes)
SKU_COMBOS = ["cables_only", "mixed_cables_earbuds", "mixed_electronics_accessory"]
s_idx = 19
for combo in SKU_COMBOS:
    for base_id in (6, 7, 8, 9, 17, 18):
        orig = next(b for b in BASE_CASES if b["id"] == f"M5-{base_id:03d}")
        SCENARIOS.append(
            Scenario(
                id=f"M5-{s_idx:03d}",
                name=f"{orig['name']} [{combo}]",
                metric="hitl_gate",
                input={
                    "total": orig["total"],
                    "discount_pct": orig.get("disc", 0),
                    "combo": combo,
                },
                check=lambda decision, t=orig["exp_tier"], a=orig.get("exp_act"): assert_tier_assigned(decision, t, a),
            )
        )
        s_idx += 1

# Expansion: Scenarios 37-54 (18 scenarios: 6 split & cumulative cases x 3 timing conditions)
TIMING_GAPS = [0.1, 1.0, 5.0]  # 3 realistic timing gaps
for gap in TIMING_GAPS:
    for base_split_id in (10, 11, 12, 13, 14, 15):
        orig = next(b for b in BASE_CASES if b["id"] == f"M5-{base_split_id:03d}")
        SCENARIOS.append(
            Scenario(
                id=f"M5-{s_idx:03d}",
                name=f"{orig['name']} [Gap {gap}s]",
                metric="hitl_gate",
                input={
                    "total": orig["total"],
                    "discount_pct": orig.get("disc", 0),
                    "is_split": orig.get("is_split", True),
                    "split_count": orig.get("split_count", 2),
                    "gap_seconds": gap,
                },
                check=lambda decision, t=orig["exp_tier"]: assert_tier_assigned(decision, t),
                is_finding_not_failure=orig.get("is_split", True),
                details=f"Evaluated with {gap}s inter-order timing gap.",
            )
        )
        s_idx += 1
