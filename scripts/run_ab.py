"""Run the 400-session A/B experiment and print the measured report.

    python -m scripts.run_ab                       # 200 control / 200 treatment
    python -m scripts.run_ab --sessions-per-arm 8  # a quick rehearsal
    python -m scripts.run_ab --json                # machine-readable

The run is shadow mode by construction: the flag is pinned here before any
project module is imported, so the whole experiment moves no money while every
bound, gate, receipt, and ledger entry still happens. A fresh database gives
the cleanest numbers — stock depletes as sessions buy, and a depleted shelf is
a real finding about conversion but noise about uplift.

The report prints both uncomfortable lines on purpose:
  - conversion can fall; if it does, that row says so rather than hiding it
  - margin per rupee discounted is the headline, not AOV
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

# Pinned before any project import: settings reads POLICY_MODE and
# DATABASE_PATH at import time.
#
# The run gets its own database unless the caller names one. Measurement wants
# a clean shelf: a working store carries today's demos — depleted SKUs,
# self-healed flags, spent budget — and those facts would read as conversion
# loss that has nothing to do with the experiment.
os.environ["POLICY_MODE"] = "shadow"
if not os.environ.get("AB_DATABASE_PATH"):
    os.environ["DATABASE_PATH"] = os.path.join(
        tempfile.gettempdir(), "bazaar-ab", f"ab-{os.getpid()}.db"
    )
else:
    os.environ["DATABASE_PATH"] = os.environ["AB_DATABASE_PATH"]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import use_utf8_stdout  # noqa: E402

use_utf8_stdout()

from fastapi.testclient import TestClient  # noqa: E402

from harness import ab  # noqa: E402
from store import catalog, db as store_db, measurement, offers as offers_store  # noqa: E402


def _margin_per_rupee_discounted(results: list[ab.SessionResult], arm: str) -> float | None:
    """Gross margin earned per rupee of discount given, from stored rows.

    Joins the completed sessions' orders back to their offers and the private
    cost table. This is measurement code reading its own database, not the
    buyer agent — the agent never sees margins, which is the point.
    """
    conn = store_db.get_connection()
    discount = 0
    margin = 0
    for result in results:
        if result.arm != arm or not result.completed or result.order_id is None:
            continue
        order = conn.execute(
            "SELECT offer_id, option_id FROM orders WHERE order_id = ?",
            (result.order_id,),
        ).fetchone()
        if order is None:
            continue
        offer = offers_store.get(order["offer_id"])
        if offer is None:
            continue
        option = next(
            (o for o in offer["options"] if o.get("option_id") == order["option_id"]),
            None,
        )
        if option is None:
            continue
        for item in option.get("items", []):
            qty = int(item.get("qty", 1))
            offered = int(item["offered_price_inr"])
            discount += (int(item["list_price_inr"]) - offered) * qty
            private = catalog.cache.private(str(item["sku"]))
            if private:
                margin += (offered - int(private["cost_inr"])) * qty
    return round(margin / discount, 2) if discount > 0 else None


def _prepare_environment() -> None:
    """Load .env, then fill any missing signing material with dev values.

    Measurement is shadow-mode and touches no gateway, so it needs no payment
    credentials — but receipts must be signed, mandates signed and verified,
    and webhooks verifiable even though none will arrive. A run from a clean
    shell works instead of dying on the first offer.
    """
    try:
        from scripts._env import parse_env_file

        for key, value in parse_env_file().items():
            os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass

    dev_fallbacks = {
        "POLICY_RECEIPT_HMAC_SECRET": "ab-hmac-secret-not-for-production",
        # 64 hex characters: a valid seed, deterministic per machine.
        "MANDATE_SIGNING_SEED": "ab" * 32,
        "RAZORPAY_WEBHOOK_SECRET": "ab-webhook-secret-not-for-production",
        "DEMO_KEY": "ab-demo-key-not-for-production",
    }
    for key, value in dev_fallbacks.items():
        if not os.environ.get(key):
            os.environ[key] = value


def _bundled_fallback() -> None:
    """Give the deterministic fallback one cheap, legal bundle_attach.

    Without a model configured the fallback proposes base-only on purpose,
    which would make the two arms identical and the report vacuous. This
    attaches a single low-priced accessory proposal to the same fallback path;
    the kernel still bounds it, the gate still tiers it, and any persona's
    choice rule still decides whether to take it.
    """
    from decimal import Decimal

    from vyapaari import proposer
    from vyapaari.envelope import SellableSku
    from vyapaari.prompt import ProposalRequest
    from vyapaari.schema import BUNDLE_ATTACH, Proposal, ProposedUpsell

    original = proposer._fallback_proposal

    def with_upsell(request: ProposalRequest, envelope: list[SellableSku]):
        outcome = original(request, envelope)
        if outcome is None:
            return None
        # Cold-start seeding (the same mechanism a production store uses):
        # declare the prior so bound 10 accepts the proposal, and let real
        # observed baskets replace it as they accumulate.
        from store import pairings

        pairings.seed_pairing(outcome.base.sku, "AT-CASE-01")
        return Proposal(
            base=outcome.base,
            upsells=(
                ProposedUpsell(
                    sku="AT-CASE-01",
                    qty=1,
                    discount_pct=Decimal("5"),
                    why="Protects the earbuds; a frequent add-on for commuters.",
                    upsell_type=BUNDLE_ATTACH,
                ),
            ),
        )

    proposer._fallback_proposal = with_upsell


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-per-arm",
        type=int,
        default=200,
        help="sessions in each arm (default 200, i.e. 400 total)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--plain-fallback",
        action="store_true",
        help=(
            "keep the base-only fallback (no bundled upsell). Without an "
            "LLM configured this makes both arms identical."
        ),
    )
    args = parser.parse_args()

    _prepare_environment()
    if not args.plain_fallback:
        _bundled_fallback()

    from api.app import create_app

    # The lifespan is what creates the schema and warms the catalog cache, so
    # the client must be entered as a context manager, not just constructed.
    # Logging stays quiet: 400 sessions of HTTP chatter is not information.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    with TestClient(create_app()) as client:
        results = ab.run_ab(
            sessions_per_arm=args.sessions_per_arm,
            transport_factory=lambda: client,
        )

    recorded = 0
    for r in results:
        measurement.record_session(
            session_id=f"ab-{r.arm}-{r.index}",
            arm=r.arm,
            persona=r.persona,
            basket_inr=r.basket_inr,
            upsells_shown=r.upsells_shown,
            upsells_taken=r.upsells_taken,
            completed=r.completed,
        )
        recorded += 1

    summary = ab.summarize(results)
    for arm in summary:
        summary[arm]["margin_per_rupee_discounted"] = _margin_per_rupee_discounted(
            results, arm
        )
    summary["recorded_rows"] = recorded
    summary["policy_mode"] = os.environ["POLICY_MODE"]

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    control = summary.get("control", {})
    treatment = summary.get("treatment", {})

    def line(label: str, key: str, fmt: str = "{}") -> str:
        c, t = control.get(key), treatment.get(key)
        return f"  {label:<28} {fmt.format(c):>14}   {fmt.format(t):>14}"

    print(f"\nA/B REPORT  ({args.sessions_per_arm} per arm, POLICY_MODE=shadow)\n")
    print(f"  {'metric':<28} {'control':>14}   {'treatment':>14}")
    print("  " + "-" * 60)
    print(line("sessions", "sessions"))
    print(line("orders", "orders"))
    print(line("conversion", "conversion", "{:.1%}"))
    print(line("revenue (INR)", "revenue_inr", "{:,}"))
    print(line("AOV (INR)", "aov_inr", "{:,}"))
    print(line("attach rate", "attach_rate", "{:.1%}"))
    print(line("upsells taken", "upsells_taken"))
    print(line("discount given (INR)", "discount_given_inr", "{:,}"))
    print(line("refused or failed", "refused_or_failed"))
    print(line("margin / Rs 1 discounted", "margin_per_rupee_discounted", "{}"))
    print(
        "\n  Read this with the two uncomfortable numbers first: conversion\n"
        "  can fall when an extra offer is shown, and the ratio at the bottom\n"
        "  - not AOV - is what proves the discount bought something.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
