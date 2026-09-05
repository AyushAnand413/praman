"""Test runner for Metric 8: Incremental Basket Lift / AOV Expansion."""
import time
from eval.common.client import call_get_offer, setup_catalog
from .scenarios import SCENARIOS


def run(conn=None):
    setup_catalog(conn)
    results = []
    print(f"\n=== Running Metric 8: Incremental Basket Lift ({len(SCENARIOS)} Scenarios) ===")

    for sc in SCENARIOS:
        t_type = sc.input["test_type"]
        b_sku = sc.input["base_sku"]
        no_bundle = sc.input.get("no_bundle", False)
        forbidden = sc.input.get("forbidden", [])
        passed = False
        msg = ""

        offer = call_get_offer(sc.input, conn=conn)
        if hasattr(offer, "assembly"):
            options = offer.assembly.options
            opt_a = next((o for o in options if o.option_id == "A"), None)
            opt_b = next((o for o in options if o.option_id == "B"), None)

            if no_bundle:
                # When no relevant companion exists, system must NOT force an irrelevant bundle
                if opt_b is None or len(opt_b.items) == 1:
                    passed = True
                    msg = "Graceful bundle suppression: no irrelevant bundle forced"
                else:
                    passed = False
                    msg = f"Forced invalid bundle on stand-alone item: {opt_b.items}"

            elif t_type == "T3":
                # Explicit decline respected: Option A serves base item alone
                if opt_a is not None and len(opt_a.items) == 1:
                    passed = True
                    msg = "Decline respected: base item offered cleanly"
                else:
                    passed = False
                    msg = "Failed to offer single base item on decline"

            elif t_type == "T2":
                # Bound 10 Relatedness check: forbidden SKUs must not appear in Option B
                if opt_b:
                    b_skus = [i.sku for i in opt_b.items]
                    has_forbidden = any(f in b_skus for f in forbidden)
                    if not has_forbidden:
                        passed = True
                        msg = f"Bound 10 vetoed irrelevant items (forbidden: {forbidden})"
                    else:
                        passed = False
                        msg = f"Irrelevant companion leaked into bundle: {b_skus}"
                else:
                    passed = True
                    msg = "No irrelevant bundle generated"

            else:
                # Relevant companion & AOV Lift (T1, T4, T5)
                if opt_a and opt_b:
                    lift = opt_b.total_inr - opt_a.total_inr
                    if lift > 0:
                        passed = True
                        msg = f"Positive AOV Lift: +₹{lift:,} (Option A: ₹{opt_a.total_inr:,} -> Option B: ₹{opt_b.total_inr:,})"
                    else:
                        passed = False
                        msg = f"Zero or negative lift: {lift}"
                else:
                    passed = False
                    msg = "Option B bundle was not generated"

        else:
            passed = False
            msg = f"Failed to generate offer: {offer}"

        results.append({
            "id": sc.id,
            "name": sc.name,
            "metric": sc.metric,
            "passed": passed,
            "message": msg,
            "is_finding": sc.is_finding_not_failure,
        })
        status_tag = "PASS" if passed else "FAIL"
        print(f"  [{sc.id}] {sc.name:<45} -> [{status_tag}] {msg}", flush=True)
        time.sleep(0.1)

    return results


if __name__ == "__main__":
    run()
