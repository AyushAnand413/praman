"""Live Test Suite: Gada Electronics Seeding & MCP Recommendation Scenarios.

Scenarios tested:
1. Seeding Gada Electronics into DB with attach candidates & priors.
2. MCP Tool `get_offer`: Cold-start bundle generation (Option A & Option B).
3. MCP Tool `get_offer`: Out-of-stock filtering (stock 0 item is skipped).
4. MCP Tool `get_offer`: Budget ceiling enforcement.
5. Real-Time Learning: Simulate orders of another accessory -> verify it promotes to Option B!
6. MCP Tool `buy`: Completing purchase of the recommended bundle.
7. REST API: GET /agent/v1/recommendations/{sku} standalone output.
"""
from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

# Add project root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from api.app import create_app
from api.mcp import build_server
from kernel.recommender import recommend_upsells, seed_pairings_from_catalog
from store import catalog, pairings as pairings_store
from store.db import get_connection, init_db, transaction
from store.timestamps import utc_now
from vyapaari import envelope as envelope_module
from fastapi.testclient import TestClient


# ── GADA ELECTRONICS CATALOG DATA ─────────────────────────────────────────────

GADA_PUBLIC = [
    {
        "sku": "GE-BUDS-01",
        "title": "Gada True Wireless Earbuds",
        "list_price_inr": 1999,
        "stock_qty": 50,
        "attrs": {"brand": "Gada Electronics", "color": "classic black", "battery_hrs": 24},
        "category": "audio_accessories",
        "returns_window_days": 7,
    },
    {
        "sku": "GE-CASE-SILICON",
        "title": "Gada Silicone Protective Case",
        "list_price_inr": 299,
        "stock_qty": 30,
        "attrs": {"brand": "Gada Electronics", "color": "crimson red"},
        "category": "audio_accessories",
        "returns_window_days": 7,
    },
    {
        "sku": "GE-CBL-FAST",
        "title": "Gada Type-C Fast Charging Cable",
        "list_price_inr": 199,
        "stock_qty": 40,
        "attrs": {"brand": "Gada Electronics", "length": "1.2m braided"},
        "category": "audio_accessories",
        "returns_window_days": 7,
    },
    {
        "sku": "GE-FOAM-TIPS",
        "title": "Gada Memory Foam Ear Tips",
        "list_price_inr": 149,
        "stock_qty": 0,  # Intentional OUT OF STOCK fixture
        "attrs": {"brand": "Gada Electronics", "size": "medium"},
        "category": "audio_accessories",
        "returns_window_days": 7,
    },
    {
        "sku": "GE-BUDS-PRO",
        "title": "Gada Pro ANC Earbuds",
        "list_price_inr": 3499,
        "stock_qty": 20,
        "attrs": {"brand": "Gada Electronics", "anc": True, "battery_hrs": 36},
        "category": "audio_accessories",
        "returns_window_days": 14,
    },
]

GADA_PRIVATE = [
    {
        "sku": "GE-BUDS-01",
        "cost_inr": 1199,
        "margin_pct": 40,
        "floor_price_inr": 1599,
        "max_discount_pct": 15,
        "attach_candidates": [
            {"sku": "GE-CASE-SILICON", "attach_rate": 0.35, "margin_pct": 50},
            {"sku": "GE-CBL-FAST", "attach_rate": 0.25, "margin_pct": 45},
            {"sku": "GE-FOAM-TIPS", "attach_rate": 0.20, "margin_pct": 60},
        ],
        "tier_up_sku": "GE-BUDS-PRO",
        "offerable": True,
    },
    {
        "sku": "GE-CASE-SILICON",
        "cost_inr": 100,
        "margin_pct": 66,
        "floor_price_inr": 220,
        "max_discount_pct": 10,
        "attach_candidates": [],
        "tier_up_sku": None,
        "offerable": True,
    },
    {
        "sku": "GE-CBL-FAST",
        "cost_inr": 80,
        "margin_pct": 60,
        "floor_price_inr": 150,
        "max_discount_pct": 10,
        "attach_candidates": [],
        "tier_up_sku": None,
        "offerable": True,
    },
    {
        "sku": "GE-FOAM-TIPS",
        "cost_inr": 50,
        "margin_pct": 66,
        "floor_price_inr": 100,
        "max_discount_pct": 10,
        "attach_candidates": [],
        "tier_up_sku": None,
        "offerable": True,
    },
    {
        "sku": "GE-BUDS-PRO",
        "cost_inr": 2199,
        "margin_pct": 37,
        "floor_price_inr": 2799,
        "max_discount_pct": 12,
        "attach_candidates": [
            {"sku": "GE-CASE-SILICON", "attach_rate": 0.40, "margin_pct": 50}
        ],
        "tier_up_sku": None,
        "offerable": True,
    },
]


async def _call_mcp_tool(mcp_server, name: str, args: dict) -> dict:
    """Invoke tool on FastMCP server and return normalized dict."""
    result = await mcp_server.call_tool(name, args)
    if isinstance(result, tuple):
        result = result[-1]
    if isinstance(result, list):
        texts = [getattr(b, "text", "") for b in result]
        joined = "".join(texts)
        return json.loads(joined) if joined else {}
    if isinstance(result, str):
        return json.loads(result)
    return result


async def main():
    print("=" * 70)
    print("🚀 PRAMAN RECOMMENDATION ENGINE — MCP & LIVE SCENARIOS VERIFICATION")
    print("=" * 70)

    conn = get_connection()
    init_db(conn)

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 1: Seed Gada Electronics into DB
    # ──────────────────────────────────────────────────────────────────────────
    print("\n📦 STEP 1: Seeding Gada Electronics products & attach candidates into DB...")
    with transaction(conn):
        conn.execute("DELETE FROM pairings WHERE base_sku LIKE :pat OR paired_sku LIKE :pat", {"pat": "GE-%"})
        conn.execute("DELETE FROM pairing_denominators WHERE base_sku LIKE :pat", {"pat": "GE-%"})
    catalog.seed_database_from_rows(GADA_PUBLIC, GADA_PRIVATE, conn=conn, seed_priors=True)
    catalog.cache.load(conn)
    print("✅ Gada Electronics products inserted and catalog cache reloaded.")

    pairs = pairings_store.pairs_for("GE-BUDS-01", conn=conn)
    print(f"✅ Cold-start pairings in DB for GE-BUDS-01: {len(pairs)} seeded pairs.")
    for p in pairs:
        print(f"   • SKU: {p['sku']} | source: {p['source']} | lift: {p['lift']} | samples: {p['samples']}")

    # Build MCP server
    mcp_server = build_server()

    # ──────────────────────────────────────────────────────────────────────────
    # SCENARIO 1: Cold-Start Offer via MCP
    # ──────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("🧪 SCENARIO 1: Cold-Start Day-1 Offer Request via MCP `get_offer`")
    print("Customer intent: 'I want Gada Wireless Earbuds with accessories'")
    print("─" * 70)

    offer_resp = await _call_mcp_tool(
        mcp_server,
        "get_offer",
        {
            "need": "wireless earbuds with accessories from Gada Electronics",
            "base_sku": "GE-BUDS-01",
            "agent_id": "claude-shopper-01",
        },
    )

    print(f"Offer ID: {offer_resp['offer_id']}")
    print(f"Recommended Option: {offer_resp.get('recommended_option_id')}")
    print(f"Total Options generated: {len(offer_resp['options'])}")

    for opt in offer_resp["options"]:
        opt_id = opt["option_id"]
        total = opt["total_inr"]
        savings = opt.get("you_save_inr", opt.get("discount_inr", 0))
        items = [f"{i['sku']} (qty {i['qty']})" for i in opt["items"]]
        print(f"\n   [Option {opt_id}] Total: ₹{total} (Savings: ₹{savings})")
        print(f"      Items: {', '.join(items)}")
        if "why" in opt:
            print(f"      Why: {opt['why']}")
        elif "human_reason" in opt:
            print(f"      Why: {opt['human_reason']}")

    # Assertions for Scenario 1
    assert len(offer_resp["options"]) >= 2, "Expected both Option A (single) and Option B (bundle)!"
    opt_b = next(o for o in offer_resp["options"] if o["option_id"] == "B")
    b_skus = [i["sku"] for i in opt_b["items"]]
    assert "GE-BUDS-01" in b_skus
    attach_accessories = [s for s in b_skus if s in ("GE-CASE-SILICON", "GE-CBL-FAST")]
    assert attach_accessories, f"Expected attach accessory in Option B bundle, got: {b_skus}"
    print(f"   Bundle accessories: {attach_accessories}")
    print("\n✅ SCENARIO 1 PASSED: Algorithmic bundle (Option B) successfully generated on Day 1!")

    # ──────────────────────────────────────────────────────────────────────────
    # SCENARIO 2: Out-of-Stock Filtering
    # ──────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("🧪 SCENARIO 2: Dynamic Out-Of-Stock Protection")
    print("GE-FOAM-TIPS has stock_qty=0 in inventory. Recommender must omit it.")
    print("─" * 70)

    all_offered_skus = {
        item["sku"]
        for opt in offer_resp["options"]
        for item in opt["items"]
    }
    assert "GE-FOAM-TIPS" not in all_offered_skus, "Out-of-stock item GE-FOAM-TIPS was offered!"
    print("✅ SCENARIO 2 PASSED: Out-of-stock item GE-FOAM-TIPS was correctly filtered out.")

    # ──────────────────────────────────────────────────────────────────────────
    # SCENARIO 3: Budget Ceiling Enforcement
    # ──────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("🧪 SCENARIO 3: Strict Buyer Budget Ceiling Enforcement")
    print("Customer states budget of ₹2,100. Base is ₹1,999. Case is ₹299 (1999 + 299 = 2298 > 2100).")
    print("─" * 70)

    budget_offer = await _call_mcp_tool(
        mcp_server,
        "get_offer",
        {
            "need": "cheap earbuds strictly under 2100 rs",
            "base_sku": "GE-BUDS-01",
            "budget_inr": 2100,
            "agent_id": "budget-shopper-02",
        },
    )

    for opt in budget_offer["options"]:
        print(f"   [Option {opt['option_id']}] Total: ₹{opt['total_inr']} <= ₹2,100 Budget? {opt['total_inr'] <= 2100}")
        assert opt["total_inr"] <= 2100, f"Option {opt['option_id']} violated budget ceiling!"

    print("✅ SCENARIO 3 PASSED: Budget constraint strictly enforced.")

    # ──────────────────────────────────────────────────────────────────────────
    # SCENARIO 4: Real-Time Self-Learning (Observed Lift Promotion)
    # ──────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("🧪 SCENARIO 4: Self-Learning (Observed Orders Promote Fast Cable)")
    print("Simulating 6 real customer orders of: GE-BUDS-01 + GE-CBL-FAST (Fast Cable).")
    print("─" * 70)

    moment = utc_now()
    for _ in range(6):
        pairings_store.record_order_basket("GE-BUDS-01", ["GE-CBL-FAST"], now=moment, conn=conn)

    updated_pairs = pairings_store.pairs_for("GE-BUDS-01", conn=conn)
    print("Updated pairings ranking in DB:")
    for p in updated_pairs:
        print(f"   • {p['sku']}: source={p['source']} | confidence={p['strength']} | lift={p.get('lift')} | samples={p['samples']}")

    top_pair = updated_pairs[0]
    assert top_pair["sku"] == "GE-CBL-FAST", "GE-CBL-FAST should be ranked #1 after real purchases!"
    assert top_pair["source"] == "observed", "GE-CBL-FAST should now be an OBSERVED source!"

    learned_offer = await _call_mcp_tool(
        mcp_server,
        "get_offer",
        {
            "need": "earbuds from Gada Electronics",
            "base_sku": "GE-BUDS-01",
            "agent_id": "learning-tester-03",
        },
    )

    learned_opt_b = next(o for o in learned_offer["options"] if o["option_id"] == "B")
    learned_b_skus = [i["sku"] for i in learned_opt_b["items"]]
    print(f"\nNew Option B Bundle Items: {', '.join(learned_b_skus)}")
    assert "GE-CBL-FAST" in learned_b_skus, "Fast cable should now be chosen in Option B!"
    print("✅ SCENARIO 4 PASSED: System self-learned and promoted the Fast Cable automatically!")

    # ──────────────────────────────────────────────────────────────────────────
    # SCENARIO 5: Complete MCP Buy Flow
    # ──────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("🧪 SCENARIO 5: Executing MCP `buy` for the Recommended Bundle")
    print("─" * 70)

    from mandate.issuers import bootstrap_demo_issuer
    from mandate import signer

    bootstrap_demo_issuer()
    mandate_token = signer.issue(
        subject="user-gada-buyer",
        agent_id="buyer-agent-04",
        categories=("audio_accessories", "cables", "charging_accessories", "earbud_accessories"),
        max_amount_inr=50_000,
        max_single_txn_inr=50_000,
    )

    buy_resp = await _call_mcp_tool(
        mcp_server,
        "buy",
        {
            "offer_id": learned_offer["offer_id"],
            "option_id": "B",
            "agent_id": "buyer-agent-04",
            "mandate": mandate_token,
        },
    )

    print(f"Order Captured: {buy_resp.get('order_id')}")
    print(f"Amount Charged: ₹{buy_resp.get('amount_inr')}")
    print(f"Order Status: {buy_resp.get('status')}")
    assert buy_resp.get("order_id", "").startswith("ORD-")

    poll_resp = await _call_mcp_tool(
        mcp_server,
        "check_order",
        {"order_id": buy_resp["order_id"]},
    )
    print(f"Polled Order State: {poll_resp.get('status')}")
    print("✅ SCENARIO 5 PASSED: Full MCP buy & poll cycle succeeded with bundle.")

    # ──────────────────────────────────────────────────────────────────────────
    # SCENARIO 6: Standalone Recommendations REST API
    # ──────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("🧪 SCENARIO 6: Standalone REST Endpoint `GET /agent/v1/recommendations/{sku}`")
    print("─" * 70)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/agent/v1/recommendations/GE-BUDS-01")
        assert resp.status_code == 200
        data = resp.json()

        print(f"Base SKU: {data['base_sku']}")
        print(f"Product Title: {data['product']['title']}")
        print(f"Bundle Options count: {len(data['bundle_options'])}")
        for b_opt in data["bundle_options"]:
            print(f"   • [{b_opt['option_id']}] {b_opt['title']} -> ₹{b_opt['total_inr']} (Items: {b_opt['items']})")

    print("\n✅ SCENARIO 6 PASSED: Dedicated recommendations REST endpoint verified.")

    print("\n" + "=" * 70)
    print("🎉 ALL 6 REAL-WORLD SCENARIOS VERIFIED SUCCESSFULLY VIA MCP & REST!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
