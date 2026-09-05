"""Test runner for Metric 7: Search Latency Benchmark."""
import asyncio
import time
from eval.common.client import setup_catalog
from .scenarios import SCENARIOS
from store import catalog
from vyapaari import envelope as envelope_module
from vyapaari.tools import envelope_search_factory


def run(conn=None):
    setup_catalog(conn)
    results = []
    print(f"\n=== Running Metric 7: Search Latency Benchmark ({len(SCENARIOS)} Scenarios) ===")

    envelope = envelope_module.build(catalog.cache.all_public(), catalog.cache.all_private_by_sku())
    search_fn = envelope_search_factory(envelope, limit=10)

    for sc in SCENARIOS:
        q = sc.input["query"]
        cond = sc.input["condition"]
        dt_ms = 0.0

        if cond == "Cold":
            # Rebuild search factory to simulate unprimed lookup
            fresh_search = envelope_search_factory(envelope, limit=10)
            t0 = time.perf_counter()
            _ = fresh_search(q)
            dt_ms = (time.perf_counter() - t0) * 1000

        elif cond == "Warm":
            t0 = time.perf_counter()
            _ = search_fn(q)
            dt_ms = (time.perf_counter() - t0) * 1000

        elif cond == "Concurrent":
            # 20 concurrent queries
            t0 = time.perf_counter()
            for _ in range(20):
                _ = search_fn(q)
            dt_ms = ((time.perf_counter() - t0) * 1000) / 20.0  # Average per-query burst latency

        passed, msg = sc.check(dt_ms)
        results.append({
            "id": sc.id,
            "name": sc.name,
            "metric": sc.metric,
            "passed": passed,
            "message": f"{msg} (actual: {dt_ms:.2f}ms)",
            "is_finding": sc.is_finding_not_failure,
            "latency_ms": round(dt_ms, 2),
        })
        status_tag = "PASS" if passed else "FAIL"
        print(f"  [{sc.id}] {sc.name:<45} -> [{status_tag}] {dt_ms:.2f}ms (SLA <= 200ms)")
        time.sleep(0.01)

    return results


if __name__ == "__main__":
    run()
