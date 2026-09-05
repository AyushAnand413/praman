"""Metric 7: Search Latency Benchmark (30 Scenarios).

10 Base query shapes across 3 load conditions:
- Cold: cache unprimed
- Warm: repeated query
- Concurrent: 20-query burst via asyncio.gather()
10 x 3 = 30 scenarios.
"""
from eval.common.scenario import Scenario
from eval.common.assertions import assert_search_latency

BASE_QUERIES = [
    ("Q01", "Simple category", "laptops"),
    ("Q02", "Exact SKU", "GE-ACER-ALITE"),
    ("Q03", "Price-filtered query", "accessories under 500"),
    ("Q04", "Typo / fuzzy matching", "accer lapotp"),
    ("Q05", "Natural language need", "best wireless earbuds for gym"),
    ("Q06", "Empty string query", ""),
    ("Q07", "Non-existent product", "flying quantum electric car"),
    ("Q08", "Special characters query", "@#$%&*(!?{}[]"),
    ("Q09", "Long 25-word query", "ultra lightweight business laptop with fast charging USB-C braided cable and long battery life for travel"),
    ("Q10", "Paginated deep query", "page 5 results for all store accessories and peripherals"),
]

LOAD_CONDITIONS = [
    ("Cold", "Cold cache query"),
    ("Warm", "Warm steady-state query"),
    ("Concurrent", "Concurrent 20x burst query"),
]

SCENARIOS: list[Scenario] = []

idx = 1
for cond_code, cond_name in LOAD_CONDITIONS:
    for q_id, q_name, q_text in BASE_QUERIES:
        sc_id = f"M7-{idx:03d}"
        SCENARIOS.append(
            Scenario(
                id=sc_id,
                name=f"{q_name} [{cond_code}]",
                metric="search_latency",
                input={"query": q_text, "condition": cond_code},
                check=lambda lat: assert_search_latency(lat, 200.0),
                details=f"Evaluated under {cond_name} load condition.",
            )
        )
        idx += 1
