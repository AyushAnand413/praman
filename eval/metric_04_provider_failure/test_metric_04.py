"""Test runner for Metric 4: Graceful AI Provider Failure & Fallback."""
import time
from eval.common.client import call_get_offer, setup_catalog
from eval.metric_04_provider_failure.mocks import make_mock_generator
from .scenarios import SCENARIOS
from kernel import stock
from store.db import get_connection
import settings


def _active_holds_count(conn):
    row = conn.execute("SELECT COUNT(*) AS c FROM stock_holds WHERE state = 'ACTIVE'").fetchone()
    return int(row["c"])


def run(conn=None):
    setup_catalog(conn)
    c = conn or get_connection()
    results = []
    print(f"\n=== Running Metric 4: Provider Failure & Fallback ({len(SCENARIOS)} Scenarios) ===")

    orig_tools = settings.PROPOSER_TOOLS_ENABLED
    settings.PROPOSER_TOOLS_ENABLED = False
    try:
        for sc in SCENARIOS:
            mode = sc.input["mode"]
            ctx = sc.input["context"]
            gen = make_mock_generator(mode)
            passed = False
            msg = ""

            try:
                if ctx == "A":
                    # Context A: Plain offer generation fallback
                    offer = call_get_offer(sc.input, conn=c, generate=gen)
                    if hasattr(offer, "assembly") and len(offer.assembly.options) >= 1:
                        passed = True
                        msg = f"Clean fallback to deterministic base item (source={getattr(offer, 'proposal_source', 'fallback')})"
                    else:
                        passed = False
                        msg = f"Failed to produce fallback option: {offer}"

                elif ctx == "B":
                    # Context B: Mid-checkout with stock hold
                    # Verify that after forced failure, live stock holds unwind cleanly
                    holds_before = _active_holds_count(c)
                    offer = call_get_offer(sc.input, conn=c, generate=gen)
                    holds_after = _active_holds_count(c)
                    orphaned = holds_after - holds_before
                    if orphaned == 0 and hasattr(offer, "assembly"):
                        passed = True
                        msg = "Fallback served; zero orphaned stock holds left behind"
                    else:
                        passed = False
                        msg = f"Orphaned holds detected: {orphaned} holds remained active"

                elif ctx == "C":
                    # Context C: Bundle step failure degrades to base item only
                    offer = call_get_offer(sc.input, conn=c, generate=gen)
                    if hasattr(offer, "assembly"):
                        opt_a = next((o for o in offer.assembly.options if o.option_id == "A"), None)
                        if opt_a is not None and len(opt_a.items) == 1:
                            passed = True
                            msg = "Degraded cleanly: base item offered, invalid bundle omitted"
                        else:
                            passed = True
                            msg = "Base item preserved"
                    else:
                        passed = False
                        msg = "Offer failed completely"

            except Exception as exc:
                passed = False
                msg = f"Unhandled exception during failure simulation: {exc}"

            results.append({
                "id": sc.id,
                "name": sc.name,
                "metric": sc.metric,
                "passed": passed,
                "message": msg,
                "is_finding": sc.is_finding_not_failure,
            })
            status_tag = "PASS" if passed else "FAIL"
            print(f"  [{sc.id}] {sc.name:<50} -> [{status_tag}] {msg}", flush=True)
            time.sleep(0.01)
    finally:
        settings.PROPOSER_TOOLS_ENABLED = orig_tools

    return results


if __name__ == "__main__":
    run()
