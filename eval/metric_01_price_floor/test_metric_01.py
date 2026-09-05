"""Test runner for Metric 1: Price Floor Invariance."""
import time
from eval.common.client import call_get_offer, setup_catalog
from eval.common.report import write_scorecard
from .scenarios import SCENARIOS


def run(conn=None):
    setup_catalog(conn)
    import settings
    orig_tools = settings.PROPOSER_TOOLS_ENABLED
    settings.PROPOSER_TOOLS_ENABLED = False
    results = []
    print(f"\n=== Running Metric 1: Price Floor Invariance ({len(SCENARIOS)} Scenarios) ===", flush=True)
    try:
        for sc in SCENARIOS:
            resp = call_get_offer(sc.input, conn=conn)
            passed, msg = sc.check(resp)
            results.append({
                "id": sc.id,
                "name": sc.name,
                "metric": sc.metric,
                "passed": passed,
                "message": msg,
                "is_finding": sc.is_finding_not_failure,
            })
            status_tag = "PASS" if passed else "FAIL"
            print(f"  [{sc.id}] {sc.name:<40} -> [{status_tag}] {msg}", flush=True)
            time.sleep(0.1)
    finally:
        settings.PROPOSER_TOOLS_ENABLED = orig_tools
    return results


if __name__ == "__main__":
    run()
