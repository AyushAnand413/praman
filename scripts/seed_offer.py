"""Seed offer rows so the money path can be exercised without an LLM.

The checkout orchestrator reads its price from a stored offer row. That row is
normally written by the offer engine, which does not exist yet — so this script
writes them the way the engine will: run the real bounds, assign the real gate
tier, and sign a real policy receipt. Nothing here shortcuts the kernel, which
is the point. If a seeded offer could bypass the bounds, so could a real one.

    python -m scripts.seed_offer                 # every scenario
    python -m scripts.seed_offer --only tier2    # just one
    python -m scripts.seed_offer --list          # names and what they prove

Each scenario prints the offer id and a ready-to-paste curl for
POST /agent/v1/checkout.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any

import settings
from kernel.bounds import LineItem, ROLE_BASE, ROLE_UPSELL, evaluate_offer
from kernel.gates import assign_tier
from kernel import receipt as receipts
from kernel import stock
from kernel.relations import related_by_base_for_items
from store import catalog, ids, offers, sessions
from store.db import get_connection, init_db

AGENT_ID = "agent_seed_demo"


@dataclass(frozen=True)
class Line:
    """One priced line, before it becomes a LineItem."""

    sku: str
    qty: int = 1
    #: Whole rupees off the unit list price. 0 means sold at list.
    discount_inr: int = 0
    role: str = ROLE_BASE


@dataclass(frozen=True)
class Scenario:
    key: str
    proves: str
    base_sku: str
    lines: tuple[Line, ...]
    note: str = ""
    #: Offers already made in the seeded session, for the session-quota bound.
    offers_made: int = 0
    #: True when the bounds are supposed to refuse this cart. Such a scenario
    #: stores no offer — a refusal at offer time means checkout never sees it,
    #: which is the strongest form of the guarantee and worth demonstrating.
    expect_refusal: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


#: The seeded scenarios. Each one exists to make a specific claim testable by
#: hand, so the key names the claim rather than the products. Text stays ASCII:
#: this prints to a Windows console where a dash outside cp1252 will not encode.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="tier0",
        proves="total under Rs 2,000 at no discount routes to Tier 0 and captures "
               "with no mandate and no human",
        base_sku="AT-CBL-USBC",
        lines=(Line("AT-CBL-USBC", qty=1),),
    ),
    Scenario(
        key="tier1",
        proves="a Rs 2,000-6,000 total requires a signed mandate",
        base_sku="AT-PRO-BLK",
        lines=(Line("AT-PRO-BLK", qty=1, discount_inr=400),),
        note="Rs 400 off Rs 4,999 is an 8.00% line discount: inside the 12% "
             "per-SKU cap, and the Rs 4,599 total sits in the mandate band. "
             "Checkout refuses this offer outright with mandate_required until a "
             "valid mandate is attached - the tier is what obliges the caller to "
             "present one, so try it both ways.",
    ),
    Scenario(
        key="tier2",
        proves="Rs 14,997 exceeds the Rs 6,000 autonomous limit, so the order is "
               "HELD and an approvals row goes PENDING",
        base_sku="AT-PRO-BLK",
        lines=(Line("AT-PRO-BLK", qty=3),),
        note="3 x Rs 4,999 at list price. No discount, nothing refused - held "
             "purely on size, which is bound 6 doing its job. Bound 6 is a "
             "gating bound: tripping it means 'ask a human', not 'refuse'.",
    ),
    Scenario(
        key="tier2_discount",
        proves="a discount above 8% routes to Tier 2 even on a small total",
        base_sku="AT-SPK-MINI",
        lines=(Line("AT-SPK-MINI", qty=1, discount_inr=230),),
        note="Rs 230 off Rs 2,499 is 9.20%: legal under the 12% per-SKU cap, "
             "but past the 8% point where a human decides.",
    ),
    Scenario(
        key="upsell",
        proves="an offer with attached upsells prices each line separately and "
               "bounds the cart as a whole",
        base_sku="AT-PRO-BLK",
        lines=(
            Line("AT-PRO-BLK", qty=1, discount_inr=300),
            Line("AT-CASE-01", qty=1, role=ROLE_UPSELL),
            Line("AT-CBL-USBC", qty=1, role=ROLE_UPSELL),
        ),
        note="Only the base line is discounted. The per-SKU cap applies to that "
             "line; the cart cap applies to the Rs 5,697 whole.",
    ),
    Scenario(
        key="refused_stock",
        proves="an out-of-stock SKU is refused at offer time, so no offer row "
               "exists for checkout to read",
        base_sku="AT-TIP-FOAM",
        lines=(Line("AT-TIP-FOAM", qty=1),),
        note="AT-TIP-FOAM is genuinely at zero in the seed catalog. Bound 7 "
             "stops it here rather than at payment.",
        expect_refusal=True,
    ),
    Scenario(
        key="refused_floor",
        proves="a price below the floor is refused however small the percentage "
               "discount looks",
        base_sku="AT-PRO-BLK",
        lines=(Line("AT-PRO-BLK", qty=1, discount_inr=900),),
        note="Rs 4,099 is one rupee under the Rs 4,100 floor. Bound 3 refuses "
             "the line, and bound 1 refuses the 18% discount as well.",
        expect_refusal=True,
    ),
)

SCENARIOS_BY_KEY = {s.key: s for s in SCENARIOS}


def _to_line_items(scenario: Scenario) -> list[LineItem]:
    items: list[LineItem] = []
    for line in scenario.lines:
        public = catalog.cache.public(line.sku)
        if public is None:
            raise SystemExit(f"SKU {line.sku} is not in the catalog")
        list_price = int(public["list_price_inr"])
        items.append(
            LineItem(
                sku=line.sku,
                qty=line.qty,
                list_price_inr=list_price,
                offered_price_inr=list_price - line.discount_inr,
                role=line.role,
            )
        )
    return items


def _option_from_items(items: list[LineItem], evaluation: Any) -> dict[str, Any]:
    """The stored option: the exact shape the checkout orchestrator reads back."""
    return {
        "option_id": "primary",
        "items": [
            {
                "sku": item.sku,
                "qty": item.qty,
                "list_price_inr": item.list_price_inr,
                "offered_price_inr": item.offered_price_inr,
                "role": item.role,
            }
            for item in items
        ],
        "total_inr": evaluation.total_inr,
        "list_total_inr": evaluation.list_total_inr,
        "discount_inr": evaluation.discount_inr,
        "seeded": True,
    }


def seed(scenario: Scenario, *, conn=None) -> dict[str, Any]:
    """Bound, gate, sign, and store one offer. Returns a summary dict."""
    items = _to_line_items(scenario)
    # Availability comes from the stock module, not the catalog row, because live
    # holds reduce what is actually sellable and a seeded offer should be bounded
    # against the same number a real one would be.
    private_by_sku = {item.sku: catalog.cache.private(item.sku) or {} for item in items}
    available_by_sku = stock.available_for(item.sku for item in items)

    evaluation = evaluate_offer(
        items,
        private_by_sku=private_by_sku,
        available_by_sku=available_by_sku,
        offers_made=scenario.offers_made,
        spent_today_inr=0,
        now=None,
        related_by_base=related_by_base_for_items(items),
    )
    if evaluation.offer_failed:
        if scenario.expect_refusal:
            # The refusal is the result. No offer row is written, which is the
            # claim: there is nothing for a checkout request to name.
            return {
                "key": scenario.key,
                "proves": scenario.proves,
                "note": scenario.note,
                "refused": True,
                "detail": evaluation.failure_detail,
                "rejecting_bounds": list(evaluation.rejecting_bounds),
                "tripped_bounds": list(evaluation.tripped_bounds),
            }
        # A seeded offer the bounds refuse is a broken scenario, not a reason to
        # store it anyway.
        raise SystemExit(
            f"scenario {scenario.key!r} was refused by the bounds: "
            f"{evaluation.failure_detail}"
        )
    if scenario.expect_refusal:
        raise SystemExit(
            f"scenario {scenario.key!r} was expected to be refused and was not. "
            "Either the catalog moved or a bound stopped working."
        )

    gate = assign_tier(
        total_inr=evaluation.total_inr,
        discount_pct=evaluation.discount_pct,
        tripped_bounds=evaluation.tripped_bounds,
        mandate_issuer_trusted=None,
        agent_first_order=False,
    )
    session_id = ids.session_id()
    sessions.create(session_id=session_id, agent_id=AGENT_ID, conn=conn)
    offer_id = ids.offer_id()
    # Signed after the offer id exists, because the receipt covers the id it
    # authorises. A receipt issued against a placeholder and patched afterwards
    # would no longer verify.
    signed = receipts.issue(
        offer_id=offer_id,
        evaluation=evaluation,
        gate=gate,
        reasons=(f"seeded scenario {scenario.key}",),
    )
    offers.create(
        offer_id=offer_id,
        session_id=session_id,
        base_sku=scenario.base_sku,
        options=[_option_from_items(items, evaluation)],
        total_inr=evaluation.total_inr,
        gate_tier=gate.tier,
        policy_receipt=signed.as_payload(),
        policy_mode=settings.POLICY_MODE.value,
        conn=conn,
    )
    sessions.record_offer(session_id, conn=conn)

    return {
        "key": scenario.key,
        "proves": scenario.proves,
        "note": scenario.note,
        "offer_id": offer_id,
        "session_id": session_id,
        "option_id": "primary",
        "total_inr": evaluation.total_inr,
        "list_total_inr": evaluation.list_total_inr,
        "discount_inr": evaluation.discount_inr,
        "discount_pct": str(evaluation.discount_pct),
        "gate_tier": gate.tier,
        "gate": gate.name,
        "requires_mandate": gate.requires_mandate,
        "requires_human": gate.requires_human,
        "triggers": [t.detail for t in gate.deciding_triggers],
        "tripped_bounds": list(evaluation.tripped_bounds),
        "receipt_id": signed.receipt_id,
        "expires_in_s": settings.OFFER_TTL_SECONDS,
    }


def _curl(summary: dict[str, Any]) -> str:
    body = json.dumps(
        {
            "offer_id": summary["offer_id"],
            "option_id": summary["option_id"],
            "agent_id": AGENT_ID,
        }
    )
    return (
        f"curl -sS -X POST {settings.PUBLIC_BASE_URL}/agent/v1/checkout \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -H 'Idempotency-Key: seed-{summary['key']}-1' \\\n"
        f"  -d '{body}'"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS_BY_KEY))
    parser.add_argument("--list", action="store_true", help="describe and exit")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.key:16} {scenario.proves}")
        return 0

    conn = get_connection()
    init_db(conn)
    catalog.seed_database(conn=conn)
    catalog.cache.load(conn)

    chosen = [SCENARIOS_BY_KEY[k] for k in args.only] if args.only else list(SCENARIOS)
    results = [seed(scenario, conn=conn) for scenario in chosen]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"POLICY_MODE={settings.POLICY_MODE.value}\n")
    for summary in results:
        # ASCII only: this prints to a Windows console by default, where the
        # box-drawing characters used elsewhere in the project cannot encode.
        print(f"-- {summary['key']} " + "-" * (60 - len(summary["key"])))
        print(f"   proves      {summary['proves']}")
        if summary["note"]:
            print(f"   note        {summary['note']}")
        if summary.get("refused"):
            print(f"   REFUSED     {summary['detail']}")
            print(f"   bounds      rejecting={summary['rejecting_bounds']} "
                  f"tripped={summary['tripped_bounds']}")
            print("   result      no offer row written; nothing to check out\n")
            continue
        print(f"   offer       {summary['offer_id']}  option={summary['option_id']}")
        print(
            f"   money       Rs {summary['total_inr']} "
            f"(list Rs {summary['list_total_inr']}, "
            f"off Rs {summary['discount_inr']} = {summary['discount_pct']}%)"
        )
        print(
            f"   gate        Tier {summary['gate_tier']} {summary['gate']} "
            f"mandate={summary['requires_mandate']} human={summary['requires_human']}"
        )
        if summary["triggers"]:
            print(f"   because     {'; '.join(summary['triggers'])}")
        print(f"   receipt     {summary['receipt_id']}")
        print(f"   expires in  {summary['expires_in_s']}s\n")
        print(_curl(summary))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
