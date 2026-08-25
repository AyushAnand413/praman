"""Drive a real payment through the checkout orchestrator, end to end.

    python scripts/checkout_live.py --live
    python scripts/checkout_live.py --live --settle ORD-xxxxxxxx

`razorpay_smoke.py` proves the gateway transport by talking to Razorpay
directly. This proves the *money path*: an offer goes through bounds, the gate,
the mandate check, a stock hold, a budget reservation and the ledger, and the
gateway order at the end belongs to a stored order that `settle` will only
accept the matching payment for.

Two steps for the same reason the smoke test has two: a payment cannot be
created server-side on a standard test account, so a browser posts the card and
`settle` runs afterwards with the resulting payment id.

    1. seed an offer, run `checkout`, write a Checkout page for its order,
    2. on `--settle`, find that order's payment and settle through the kernel.

`--live` is required, because both steps move real test-mode money and commit
stock. Without it nothing is created and the reason is printed.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._console import use_utf8_stdout  # noqa: E402
from scripts._env import load_env_file  # noqa: E402
load_env_file()

import settings  # noqa: E402
from settings import DATA_DIR, MERCHANT_NAME, MissingSecret, PolicyMode  # noqa: E402
from kernel import checkout as checkout_kernel  # noqa: E402
from kernel.gates import TIER_HUMAN, TIER_MANDATE  # noqa: E402
from kernel.payments import RazorpayClient, RazorpayError  # noqa: E402
from mandate import signer  # noqa: E402
from mandate.issuers import bootstrap_demo_issuer  # noqa: E402
from scripts import seed_offer  # noqa: E402
from store import catalog, ledger, orders  # noqa: E402
from store.db import get_connection, init_db  # noqa: E402

AGENT_ID = "agent-checkout-live"

#: Every category the seeded scenarios draw from, so a mandate issued here covers
#: whichever one was chosen rather than failing as SCOPE_MISMATCH.
MANDATE_CATEGORIES = (
    "audio_accessories",
    "cables",
    "charging_accessories",
    "earbud_accessories",
)

CHECKOUT_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Aether Audio — checkout capture</title>
<style>
  body {{ font: 15px/1.6 system-ui, sans-serif; max-width: 34rem;
         margin: 4rem auto; padding: 0 1rem; }}
  code {{ background: #f4f4f5; padding: .15em .4em; border-radius: 3px; }}
  button {{ font-size: 1rem; padding: .7em 1.4em; cursor: pointer; }}
  ol {{ padding-left: 1.2rem; }}
  .meta {{ color: #52525b; font-size: .9rem; }}
</style>
<h2>Checkout capture &mdash; through the policy kernel</h2>
<p>Order <code>{order_id}</code> &middot; &#8377;{amount_inr}</p>
<p class="meta">Gateway order <code>{gateway_order_id}</code> &middot;
   tier {gate_tier} &middot; priced by the server, not by the caller</p>
<ol>
  <li>Click Pay.</li>
  <li>Card <code>4111 1111 1111 1111</code>, any future expiry, any CVV,
      OTP <code>1111</code>. If card is declined on this account, pick
      <b>Netbanking</b> and choose success.</li>
  <li>Back in the terminal:
      <br><code>python scripts/checkout_live.py --live --settle {order_id}</code></li>
</ol>
<button id="pay">Pay &#8377;{amount_inr}</button>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
document.getElementById('pay').onclick = function () {{
  new Razorpay({{
    key: '{key_id}',
    order_id: '{gateway_order_id}',
    amount: {amount_paise},
    currency: 'INR',
    name: '{merchant}',
    description: 'Order {order_id}',
    handler: function (r) {{
      document.body.insertAdjacentHTML('beforeend',
        '<p><b>Paid:</b> <code>' + r.razorpay_payment_id +
        '</code><br>Now run the settle command above.</p>');
    }}
  }}).open();
}};
</script>
"""


def _bootstrap() -> None:
    """The same startup the app performs, minus the HTTP server."""
    conn = get_connection()
    init_db(conn)
    count = catalog.seed_database(conn=conn)
    catalog.cache.load(conn)
    if ledger.tip(conn)[0] == 0:
        ledger.append(
            "system",
            "ledger.genesis",
            {"merchant": MERCHANT_NAME, "catalog_skus": count},
            conn=conn,
        )
    bootstrap_demo_issuer()


def place(client: RazorpayClient, scenario_key: str, *, open_browser: bool) -> int:
    offer = seed_offer.seed(seed_offer.SCENARIOS_BY_KEY[scenario_key])
    tier = offer["gate_tier"]
    print(f"offer seeded   {offer['offer_id']}")
    print(f"  total        Rs {offer['total_inr']}")
    print(f"  gate tier    {tier}")

    # A mandate only where the gate demands one. Issuing unconditionally would
    # hide the refusal that Tier 1 is supposed to produce without it.
    mandate_token = None
    if tier >= TIER_MANDATE:
        mandate_token = signer.issue(
            subject="user-checkout-live",
            agent_id=AGENT_ID,
            categories=MANDATE_CATEGORIES,
            max_amount_inr=50_000,
            max_single_txn_inr=50_000,
        )
        print("  mandate      issued (tier requires one)")

    result = checkout_kernel.checkout(
        offer_id=offer["offer_id"],
        option_id=offer["option_id"],
        idempotency_key=f"live-{offer['offer_id']}",
        agent_id=AGENT_ID,
        mandate_token=mandate_token,
    )

    print(f"\norder          {result.order_id}")
    print(f"  status       {result.status}")
    print(f"  state        {result.state}")
    print(f"  amount       Rs {result.amount_inr}")
    print(f"  receipt sig  {result.policy_receipt['signature'][:16]}...")

    if tier >= TIER_HUMAN:
        print(f"\nHELD for human approval (approval {result.approval_id}).")
        print("Nothing was sent to Razorpay, which is the point of this tier.")
        print("Pick a cheaper scenario to exercise the payment path:")
        print("    python scripts/checkout_live.py --live --scenario tier0")
        return 0

    if not result.razorpay:
        print("\nNo gateway order was created. Is POLICY_MODE live?")
        return 1

    gateway_order_id = result.razorpay["order_id"]
    print(f"  gateway      {gateway_order_id}")

    page = DATA_DIR / "checkout_live.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        CHECKOUT_HTML.format(
            order_id=result.order_id,
            gateway_order_id=gateway_order_id,
            amount_inr=result.amount_inr,
            amount_paise=result.amount_inr * 100,
            gate_tier=tier,
            key_id=client.key_id,
            merchant=MERCHANT_NAME,
        ),
        encoding="utf-8",
    )
    print(f"\ncheckout page  {page}")
    print("  pay in the browser, then run:")
    print(f"    python scripts/checkout_live.py --live --settle {result.order_id}")

    if open_browser:
        webbrowser.open(page.resolve().as_uri())
    return 0


def settle(client: RazorpayClient, order_id: str) -> int:
    try:
        order = orders.require(order_id)
    except Exception as exc:
        print(f"FAIL - no such order {order_id}: {exc}")
        return 1

    gateway_order_id = order["razorpay_order_id"]
    if not gateway_order_id:
        print(f"order {order_id} has no gateway order — was it held or refused?")
        return 1

    print(f"order          {order_id}")
    print(f"  state        {order['state']}")
    print(f"  amount       Rs {order['amount_inr']}")
    print(f"  gateway      {gateway_order_id}")

    payments = client.fetch_order_payments(gateway_order_id)
    if not payments:
        print("\nNo payment attempts on this order yet.")
        print(f"Open {DATA_DIR / 'checkout_live.html'} and pay first.")
        return 1

    usable = next(
        (p for p in payments if p["captured"] or p["status"] == "authorized"), None
    )
    if usable is None:
        for payment in payments:
            print(f"  attempt      {payment['id']} status={payment['status']} "
                  f"{payment.get('error_description') or ''}")
        print("\nNo authorized or captured payment to settle.")
        return 1

    print(f"  payment      {usable['id']} status={usable['status']}")

    try:
        settled = checkout_kernel.settle(order_id, payment_id=usable["id"])
    except checkout_kernel.CheckoutError as exc:
        print(f"\nFAIL - settle refused: [{exc.code}] {exc}")
        return 1

    print("\nsettled through the kernel")
    print(f"  state        {settled['state']}")
    print(f"  payment      {settled['razorpay_payment_id']}")
    print(f"  amount       Rs {settled['amount_inr']}")

    # The reservation should be gone: consumed by the capture, not left for the
    # abandonment sweep to release as though the sale had never happened.
    holds, reserved = orders.reservation(order_id)
    print(f"  reservation  holds={holds or 'cleared'} budget={reserved}")

    report = ledger.verify_chain()
    print(f"\nledger         {report['entries_checked']} entries, "
          f"intact={report['intact']}")
    return 0 if report["intact"] else 1


def main() -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="required: this moves real test-mode money")
    parser.add_argument("--scenario", default="tier0",
                        choices=sorted(seed_offer.SCENARIOS_BY_KEY),
                        help="which seeded cart to place (default tier0)")
    parser.add_argument("--settle", metavar="ORDER_ID",
                        help="settle an order placed by an earlier run")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the checkout page automatically")
    args = parser.parse_args()

    if not args.live:
        print("Refusing to run without --live.")
        print("Both steps move real test-mode money and commit stock, so the")
        print("intent has to be explicit rather than inherited from POLICY_MODE.")
        return 2

    # Patched on the settings module, not on kernel.mode: `current_mode` reads the
    # attribute fresh on every call, which is the indirection being relied on here.
    settings.POLICY_MODE = PolicyMode.LIVE

    _bootstrap()

    try:
        client = RazorpayClient()
    except MissingSecret as exc:
        print(f"FAIL - {exc}")
        print("\nSet RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env first.")
        return 2
    except RazorpayError as exc:
        print(f"FAIL - {exc}")
        return 2

    try:
        if args.settle:
            return settle(client, args.settle)
        return place(client, args.scenario, open_browser=not args.no_browser)
    except RazorpayError as exc:
        print(f"FAIL - Razorpay: {exc}")
        return 1
    except checkout_kernel.CheckoutError as exc:
        print(f"FAIL - checkout refused: [{exc.code}] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
