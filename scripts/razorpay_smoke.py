"""Prove the one external dependency actually works.

    python scripts/razorpay_smoke.py                  # create the order
    python scripts/razorpay_smoke.py --order order_X  # capture after paying

Prints a real `order_...` id and a captured `pay_...` from test mode.

Why this is two steps rather than one: creating an order is a pure server-side
API call, but creating a *payment* is not. Razorpay's server-to-server payment
API requires per-account activation, so the portable test-mode path is
Checkout — a browser posts the card, then we capture server-side. This script
therefore:

    1. creates the order (headless, works with any test key),
    2. writes a local Checkout page pre-filled with that order,
    3. on re-run, finds the authorized payment and captures it.

Every call and its result is written to the ledger, so the smoke test is itself
auditable — including the money event, which carries a mandatory reason.
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

from settings import DATA_DIR, MissingSecret  # noqa: E402
from kernel.payments import RazorpayClient, RazorpayError  # noqa: E402
from store import ledger  # noqa: E402
from store.db import get_connection, init_db  # noqa: E402

# Rs 5,598 = AT-PRO-BLK (4999) + AT-CASE-01 (599) — the amount used in the
# compensation walkthrough, so this smoke test rehearses the demo's numbers.
DEFAULT_AMOUNT_INR = 5598

CHECKOUT_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Aether Audio — Razorpay test capture</title>
<style>
  body {{ font: 15px/1.6 system-ui, sans-serif; max-width: 34rem;
         margin: 4rem auto; padding: 0 1rem; }}
  code {{ background: #f4f4f5; padding: .15em .4em; border-radius: 3px; }}
  button {{ font-size: 1rem; padding: .7em 1.4em; cursor: pointer; }}
  ol {{ padding-left: 1.2rem; }}
</style>
<h2>Razorpay test-mode capture</h2>
<p>Order <code>{order_id}</code> &middot; &#8377;{amount_inr}</p>
<ol>
  <li>Click Pay.</li>
  <li>Choose <b>Card</b> and use test card <code>4111 1111 1111 1111</code>,
      any future expiry, any CVV, OTP <code>1111</code>.</li>
  <li>Come back to the terminal and run:
      <br><code>python scripts/razorpay_smoke.py --order {order_id}</code></li>
</ol>
<button id="pay">Pay &#8377;{amount_inr}</button>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
document.getElementById('pay').onclick = function () {{
  new Razorpay({{
    key: '{key_id}',
    order_id: '{order_id}',
    amount: {amount_paise},
    currency: 'INR',
    name: 'Aether Audio',
    description: 'Razorpay test-mode smoke test',
    handler: function (r) {{
      document.body.insertAdjacentHTML('beforeend',
        '<p><b>Authorized:</b> <code>' + r.razorpay_payment_id +
        '</code><br>Now run the capture command above.</p>');
    }}
  }}).open();
}};
</script>
"""


def create(client: RazorpayClient, amount_inr: int, *, open_browser: bool) -> str:
    order = client.create_order(
        amount_inr,
        receipt=f"smoke-{amount_inr}",
        notes={"purpose": "razorpay_smoke_test"},
    )
    print(f"order created  {order['id']}")
    print(f"  amount       Rs {order['amount_inr']}")
    print(f"  status       {order['status']}")

    # No money has moved yet, so this entry carries no money_delta and needs
    # no reason. The capture below is the money event.
    ledger.append(
        "razorpay",
        "razorpay.order.created",
        {
            "razorpay_order_id": order["id"],
            "amount_inr": order["amount_inr"],
            "receipt": order["receipt"],
            "status": order["status"],
            "source": "smoke_test",
        },
    )

    page = DATA_DIR / "checkout_test.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        CHECKOUT_HTML.format(
            order_id=order["id"],
            amount_inr=order["amount_inr"],
            amount_paise=order["amount_inr"] * 100,
            key_id=client.key_id,
        ),
        encoding="utf-8",
    )
    print(f"\ncheckout page  {page}")
    print("  pay with test card 4111 1111 1111 1111, then run:")
    print(f"    python scripts/razorpay_smoke.py --order {order['id']}")

    if open_browser:
        webbrowser.open(page.resolve().as_uri())
    return str(order["id"])


def capture(client: RazorpayClient, order_id: str) -> int:
    order = client.fetch_order(order_id)
    payments = client.fetch_order_payments(order_id)
    if not payments:
        print(f"order {order_id} has no payment attempts yet.")
        print("Open data/checkout_test.html and pay with the test card first.")
        return 1

    already = next((p for p in payments if p["captured"]), None)
    if already:
        print(f"payment captured  {already['id']} (already captured)")
        print(f"  amount          Rs {already['amount_inr']}")
        return 0

    authorized = next((p for p in payments if p["status"] == "authorized"), None)
    if authorized is None:
        for payment in payments:
            print(f"payment {payment['id']} status={payment['status']} "
                  f"{payment.get('error_description') or ''}")
        print("\nNo authorized payment to capture.")
        return 1

    payment = client.capture_payment(authorized["id"], order["amount_inr"])
    print(f"payment captured  {payment['id']}")
    print(f"  amount          Rs {payment['amount_inr']}")
    print(f"  status          {payment['status']} captured={payment['captured']}")

    entry = ledger.append(
        "razorpay",
        "payment.captured",
        {"razorpay_order_id": order_id, "razorpay_payment_id": payment["id"],
         "amount_inr": payment["amount_inr"], "method": payment.get("method"),
         "source": "smoke_test"},
        money_delta_inr=payment["amount_inr"],
        reason=(
            f"Captured Rs {payment['amount_inr']} for Razorpay order "
            f"{order_id} during the test-mode smoke test."
        ),
    )
    print(f"  ledger seq      {entry.seq} entry_hash={entry.entry_hash[:16]}...")
    return 0


def main() -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amount", type=int, default=DEFAULT_AMOUNT_INR,
                        help=f"amount in whole rupees (default {DEFAULT_AMOUNT_INR})")
    parser.add_argument("--order", help="existing order id: skip creation and capture")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the checkout page automatically")
    args = parser.parse_args()

    init_db(get_connection())

    try:
        client = RazorpayClient()
    except MissingSecret as exc:
        print(f"FAIL - {exc}")
        print("\nGet test keys from dashboard.razorpay.com, Settings, API Keys, then:")
        print('    $env:RAZORPAY_KEY_ID="rzp_test_..."; $env:RAZORPAY_KEY_SECRET="..."')
        return 2
    except RazorpayError as exc:
        print(f"FAIL - {exc}")
        return 2

    try:
        if args.order:
            return capture(client, args.order)
        create(client, args.amount, open_browser=not args.no_browser)
        return 0
    except RazorpayError as exc:
        print(f"FAIL - Razorpay: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
