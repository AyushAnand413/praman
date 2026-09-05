"""Report generator creating reports/scorecard.md and reports/scorecard.json."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from store.timestamps import utc_now


def write_scorecard(
    all_results: dict[str, list[dict[str, Any]]],
    findings: list[dict[str, Any]],
    *,
    out_markdown_path: Path,
    out_json_path: Path,
    model_name: str = "dots-studio/dots-3-note-preview:free",
) -> None:
    """Generate final scorecard in Markdown and JSON formats."""
    out_markdown_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.parent.mkdir(parents=True, exist_ok=True)

    total_scenarios = sum(len(res) for res in all_results.values())
    total_passed = sum(
        sum(1 for r in res if r.get("passed", False))
        for res in all_results.values()
    )
    total_failed = total_scenarios - total_passed
    overall_pass_rate = (total_passed / total_scenarios * 100) if total_scenarios > 0 else 0.0

    # Write JSON
    json_data = {
        "timestamp": utc_now().isoformat(),
        "model": model_name,
        "summary": {
            "total_scenarios": total_scenarios,
            "total_passed": total_passed,
            "total_failed": total_failed,
            "overall_pass_rate_pct": round(overall_pass_rate, 1),
        },
        "metrics": all_results,
        "findings": findings,
    }
    out_json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    # Write Markdown Scorecard
    md_lines = [
        "# PRAMAN Evaluation Scorecard (v3 — 9 Metrics, 369 Scenarios)",
        "",
        f"**Generated**: `{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}`  ",
        f"**LLM Generator**: `{model_name}` (3 Rotating API Keys with Live 429 Failover)  ",
        f"**Database**: PostgreSQL Supabase (Singapore)  ",
        f"**Overall Result**: **{total_passed}/{total_scenarios} Passed ({overall_pass_rate:.1f}%)**  ",
        "",
        "---",
        "",
        "## 1. Executive Summary Table",
        "",
        "| Metric # | Dimension | Scenario Count | Passed | Pass Rate | Status | Primary Enforcement Layer |",
        "|:---:|---|:---:|:---:|:---:|:---:|---|",
    ]

    metric_names = {
        "price_floor": ("Metric 1: Price Floor Invariance", "Bound 3 (Deterministic Floor)"),
        "discount_cap": ("Metric 2: Discount Cap Precision", "Bound 1 & 2 (Hard Math Cap)"),
        "prompt_injection": ("Metric 3: Prompt Injection Defense", "Dual-Layer (Prompt Shield + Bound Veto)"),
        "provider_failure": ("Metric 4: Provider Failure & Fallback", "Vyapaari Ladder (Deterministic Fallback)"),
        "hitl_gate": ("Metric 5: Dual-Gate HITL Escalation", "Bound 6 (Tier 0 ≤₹2k, Tier 1 ≤₹6k, Tier 2 >₹6k)"),
        "audit_trail": ("Metric 6: Cryptographic Audit Trail", "Store Ledger (HMAC-SHA256 Hash Chain)"),
        "search_latency": ("Metric 7: Search Latency Benchmark", "In-Memory Pre-Computed Inverted Index"),
        "basket_lift": ("Metric 8: Incremental Basket Lift (AOV)", "Vyapaari Recommender + Bound 10 Relatedness"),
        "mandate_settlement": ("Metric 9: Mandate & Payment Settlement", "8-Stage Ed25519 Verifier + Razorpay Client"),
    }

    for key, (label, layer) in metric_names.items():
        res = all_results.get(key, [])
        count = len(res)
        passed = sum(1 for r in res if r.get("passed", False))
        rate = (passed / count * 100) if count > 0 else 0.0
        status = "✅ PASS" if rate >= 95.0 else ("⚠️ FINDING" if rate >= 80.0 else "❌ FAIL")
        md_lines.append(f"| {label.split(':')[0]} | {label.split(':')[1].strip()} | {count} | {passed} | {rate:.1f}% | {status} | {layer} |")

    md_lines.extend([
        "",
        "---",
        "",
        "## 2. Architectural Findings & Limitations (Honest Disclosure)",
        "",
    ])

    if findings:
        for f in findings:
            md_lines.extend([
                f"### Finding: {f.get('title')}",
                f"- **Metric Affected**: {f.get('metric')}",
                f"- **Behavior**: {f.get('observation')}",
                f"- **Root Cause**: {f.get('root_cause')}",
                f"- **Production Recommendation**: {f.get('recommendation')}",
                "",
            ])
    else:
        md_lines.append("No unexpected architectural limitations surfaced during this run.")

    md_lines.extend([
        "---",
        "",
        "## 3. Detailed Metric-by-Metric Breakdown",
        "",
    ])

    for key, (label, _) in metric_names.items():
        res = all_results.get(key, [])
        md_lines.extend([
            f"### {label} ({len(res)} Scenarios)",
            "",
            "| Scenario ID | Name | Outcome | Message |",
            "|---|---|:---:|---|",
        ])
        for r in res:
            status_icon = "✅" if r.get("passed") else ("⚠️" if r.get("is_finding") else "❌")
            md_lines.append(f"| `{r.get('id')}` | {r.get('name')} | {status_icon} | {r.get('message', '')} |")
        md_lines.append("")

    out_markdown_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Scorecard written to {out_markdown_path} and {out_json_path}")
