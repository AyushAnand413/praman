"""One-command buyer: run a real AI agent against your running server.

    python -m scripts.demo_buy                       # buy at http://127.0.0.1:8090
    python -m scripts.demo_buy --base-url http://localhost:8000

This is Grahak, the buyer agent, walking the full rail against YOUR server:
discovery -> catalog -> offer -> checkout. Watch the merchant panel
(/panel/) as it runs — every step below appears in the live ledger feed,
the metrics move, and anything above Rs 6,000 lands in "Needs your approval".

Load .env first so the wallet can sign mandates (same as the server):
this script reads it directly, the way other scripts do.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import use_utf8_stdout  # noqa: E402

use_utf8_stdout()

import os  # noqa: E402

from scripts._env import parse_env_file  # noqa: E402

for _key, _value in parse_env_file().items():
    os.environ.setdefault(_key, _value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8090",
                        help="your running server (default 127.0.0.1:8090)")
    parser.add_argument("--need", default="sweat-proof earbuds under 6500",
                        help="what the buyer agent should ask for")
    args = parser.parse_args()

    import httpx

    from harness.grahak import Grahak

    client = httpx.Client(base_url=args.base_url, timeout=30.0)

    grahak = Grahak(client, agent_id="grahak_demo_terminal")
    print(f"buyer agent : {grahak.agent_id}")
    print(f"store       : {args.base_url}")
    print(f"need        : {args.need!r}")
    print("-" * 60)

    print("[1/4] discovery  ...", end=" ", flush=True)
    discovery = grahak.discover()
    print(f"ok ({discovery.latency_ms}ms)")

    print("[2/4] catalog    ...", end=" ", flush=True)
    results = grahak.browse(args.need)
    print(f"{len(results)} product(s)")

    print("[3/4] offer      ...", end=" ", flush=True)
    offer = grahak.request_offer(args.need)
    print(f"{len(offer.options)} option(s), expires in {offer.expires_in_seconds}s")
    for option in offer.options:
        items = " + ".join(i["sku"] for i in option.get("items", []))
        marker = " <-recommended" if option is offer.recommended else ""
        print(f"        {option['option_id']}: {items} = Rs {option['total_inr']}{marker}")

    print("[4/4] checkout   ...", end=" ", flush=True)
    purchase = grahak.buy(offer)
    print("done")
    print("-" * 60)
    print(f"order      : {purchase.order_id}")
    print(f"status     : {purchase.status}")
    print(f"amount     : Rs {purchase.amount_inr}")
    print(f"mode       : {purchase.policy_mode}")
    if purchase.held_for_human:
        print("\nHELD for merchant approval -> open /panel/ and click Approve,")
        print("then poll:  GET " + (purchase.poll_url or ""))
    elif purchase.would_have_charged:
        print("\nShadow mode: the full verdict ran, Rs 0 moved.")
    print("\nWatch /panel/ — the feed, metrics and cage counters just moved.")

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
