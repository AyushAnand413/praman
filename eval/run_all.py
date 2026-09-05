"""Master Orchestrator for PRAMAN Evaluation Suite (v3 — 9 Metrics, 369 Scenarios).

Runs all 9 metrics in sequence or individually:
1. Price Floor Invariance (52 scenarios)
2. Discount Cap Precision (56 scenarios)
3. Prompt Injection Defense (50 scenarios)
4. Provider Failure & Fallback (21 scenarios)
5. Dual-Gate HITL Escalation (54 scenarios)
6. Cryptographic Audit Trail (24 scenarios)
7. Search Latency Benchmark (30 scenarios)
8. Incremental Basket Lift / AOV (20 scenarios)
9. Payment Execution & Mandate Settlement (62 scenarios)

Usage:
    python eval/run_all.py                   # Run all 369 scenarios
    python eval/run_all.py --metric 1        # Run only Metric 1
    python eval/run_all.py --metric hitl     # Run only Metric 5
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from eval.common.client import setup_catalog
from eval.common.report import write_scorecard
from store.db import get_connection, init_db

# Metric runners
from eval.metric_01_price_floor import test_metric_01
from eval.metric_02_discount_cap import test_metric_02
from eval.metric_03_prompt_injection import test_metric_03
from eval.metric_04_provider_failure import test_metric_04
from eval.metric_05_hitl_gate import test_metric_05
from eval.metric_06_audit_trail import test_metric_06
from eval.metric_07_search_latency import test_metric_07
from eval.metric_08_basket_lift import test_metric_08
from eval.metric_09_mandate_settlement import test_metric_09

METRIC_MAP = {
    "1": ("price_floor", test_metric_01.run),
    "price_floor": ("price_floor", test_metric_01.run),
    "2": ("discount_cap", test_metric_02.run),
    "discount_cap": ("discount_cap", test_metric_02.run),
    "3": ("prompt_injection", test_metric_03.run),
    "prompt_injection": ("prompt_injection", test_metric_03.run),
    "4": ("provider_failure", test_metric_04.run),
    "provider_failure": ("provider_failure", test_metric_04.run),
    "5": ("hitl_gate", test_metric_05.run),
    "hitl_gate": ("hitl_gate", test_metric_05.run),
    "6": ("audit_trail", test_metric_06.run),
    "audit_trail": ("audit_trail", test_metric_06.run),
    "7": ("search_latency", test_metric_07.run),
    "search_latency": ("search_latency", test_metric_07.run),
    "8": ("basket_lift", test_metric_08.run),
    "basket_lift": ("basket_lift", test_metric_08.run),
    "9": ("mandate_settlement", test_metric_09.run),
    "mandate_settlement": ("mandate_settlement", test_metric_09.run),
}

ORDERED_METRICS = [
    ("price_floor", test_metric_01.run),
    ("discount_cap", test_metric_02.run),
    ("prompt_injection", test_metric_03.run),
    ("provider_failure", test_metric_04.run),
    ("hitl_gate", test_metric_05.run),
    ("audit_trail", test_metric_06.run),
    ("search_latency", test_metric_07.run),
    ("basket_lift", test_metric_08.run),
    ("mandate_settlement", test_metric_09.run),
]


def main():
    parser = argparse.ArgumentParser(description="PRAMAN Evaluation Runner (v3 — 9 Metrics, 369 Scenarios)")
    parser.add_argument("--metric", type=str, default="all", help="Metric number (1-9) or name (e.g. price_floor, hitl_gate, mandate_settlement)")
    parser.add_argument("--out-md", type=str, default="eval/reports/scorecard.md", help="Output Markdown scorecard path")
    parser.add_argument("--out-json", type=str, default="eval/reports/scorecard.json", help="Output JSON results path")
    args = parser.parse_args()

    print("=" * 78)
    print("  PRAMAN HARNESS EVALUATION SUITE (v3 — 9 METRICS, 369 SCENARIOS)")
    print("=" * 78)

    init_db()
    conn = get_connection()
    setup_catalog(conn)
    print("✔ Evaluation catalog initialized and verified in memory.\n")

    all_results: dict[str, list] = {}
    findings: list[dict] = [
        {
            "metric": "Metric 5: Dual-Gate HITL Escalation",
            "title": "Split-Order Cart Evasion vs. Session Spend Tracking",
            "observation": "Each individual order of ₹1,999 evaluates independently against the cart limit (cart total ₹1,999 <= ₹2,000 ceiling), clearing under Tier 0 (auto-proceed without mandate). Consequently, an agent executing 3 split orders in a single session spends an aggregate of ₹5,997 without triggering Tier 1 mandate verification.",
            "root_cause": "Current policy gate assigns tiers on a per-order cart basis rather than tracking rolling cumulative spend per agent session.",
            "recommendation": "Deploy a merchant-level session velocity gate or sliding-window budget tracker in production for multi-order cumulative spend governance.",
        }
    ]

    selected = str(args.metric).lower().strip()
    metrics_to_run = []

    if selected == "all":
        metrics_to_run = ORDERED_METRICS
    elif selected in METRIC_MAP:
        metrics_to_run = [METRIC_MAP[selected]]
    else:
        print(f"Unknown metric '{args.metric}'. Valid choices: 1-9 or all")
        sys.exit(1)

    t_start = time.perf_counter()

    for key, run_fn in metrics_to_run:
        m_start = time.perf_counter()
        res = run_fn(conn=conn)
        all_results[key] = res
        duration = time.perf_counter() - m_start
        passed_cnt = sum(1 for r in res if r.get("passed"))
        print(f"  --> {key.upper()} COMPLETED: {passed_cnt}/{len(res)} Passed in {duration:.1f}s")

    total_time = time.perf_counter() - t_start

    # Generate Reports
    md_path = REPO_ROOT / args.out_md
    json_path = REPO_ROOT / args.out_json
    write_scorecard(all_results, findings, out_markdown_path=md_path, out_json_path=json_path)

    total_scenarios = sum(len(res) for res in all_results.values())
    total_passed = sum(sum(1 for r in res if r.get("passed")) for res in all_results.values())

    print("\n" + "=" * 78)
    print(f"  EVALUATION COMPLETE: {total_passed}/{total_scenarios} Passed in {total_time:.1f}s")
    print(f"  Scorecard: {md_path}")
    print(f"  Machine-Readable JSON: {json_path}")
    print("=" * 78)


if __name__ == "__main__":
    main()
