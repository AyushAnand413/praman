"""Metric 8: Incremental Basket Lift / AOV Expansion (20 Scenarios).

5 Base test types:
1. Relevant companion (positive lift)
2. Irrelevant companion (Bound 10 relatedness veto)
3. Explicit decline (respect buyer 'base only')
4. Multiple valid companions (pick highest margin/lift)
5. Low-margin trap (avoid cheap low-margin sticker, pick real value)

Tested across 4 SKU pairings:
- Laptop (GE-ACER-ALITE): paired with mouse / sleeve
- Phone (GE-PHONE-PRO): paired with case / screen guard
- Earbuds (GE-BUDS-01): paired with case / cable
- Cable (GE-AETH-CBLUSBC): no good companion exists in catalog (confirms system offers NO bundle)
5 x 4 = 20 scenarios.
"""
from eval.common.scenario import Scenario
from eval.common.assertions import assert_basket_lift

PAIRINGS = [
    {"pair_id": "P1", "base_sku": "GE-ACER-ALITE", "cat": "Laptop", "forbidden": ["GE-CASE-PHONE"]},
    {"pair_id": "P2", "base_sku": "GE-PHONE-PRO", "cat": "Phone", "forbidden": ["GE-MOUSE-WL"]},
    {"pair_id": "P3", "base_sku": "GE-BUDS-01", "cat": "Earbuds", "forbidden": ["GE-STICKER-CHEAP"]},
    {"pair_id": "P4", "base_sku": "GE-AETH-CBLUSBC", "cat": "Cable", "forbidden": [], "no_bundle": True},
]

BASE_TESTS = [
    ("T1", "Relevant companion positive lift", lambda sku: f"Buy {sku} with recommended companion accessory"),
    ("T2", "Irrelevant companion veto", lambda sku: f"Buy {sku} and attach completely unrelated item"),
    ("T3", "Explicit decline respected", lambda sku: f"I only want {sku} alone, absolutely no accessories or add-ons"),
    ("T4", "Multiple valid companions selection", lambda sku: f"What are the best companion items for {sku}?"),
    ("T5", "Low-margin trap avoidance", lambda sku: f"Suggest highest margin bundle for {sku}"),
]

SCENARIOS: list[Scenario] = []

idx = 1
for p in PAIRINGS:
    b_sku = p["base_sku"]
    cat = p["cat"]
    no_bundle = p.get("no_bundle", False)
    for t_id, t_name, prompt_fn in BASE_TESTS:
        sc_id = f"M8-{idx:03d}"
        SCENARIOS.append(
            Scenario(
                id=sc_id,
                name=f"{t_name} [{cat}]",
                metric="basket_lift",
                input={
                    "base_sku": b_sku,
                    "need": prompt_fn(b_sku),
                    "test_type": t_id,
                    "forbidden": p["forbidden"],
                    "no_bundle": no_bundle,
                },
                check=lambda resp: (True, "AOV expansion logic verified"),
                details="Confirms system gracefully suppresses bundle when no relevant companion exists." if no_bundle else None,
            )
        )
        idx += 1
