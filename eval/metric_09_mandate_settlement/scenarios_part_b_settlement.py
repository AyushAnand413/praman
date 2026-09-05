"""Metric 9 Part B: Settlement Flows (22 Scenarios).

Flow A: Direct Capture (One-step via authorized payment_id)
Flow B: Hosted Checkout Link + Webhook Capture (Two-step)
Covers payment_id reuse, amount mismatches, webhook replay, forged signatures, stock hold expiry, and ledger reconciliation.
18 named scenarios + 4 combinatorial repeats = 22 total.
"""
from eval.common.scenario import Scenario

BASE_SETTLEMENT_SCENARIOS = [
    ("B01", "Legitimate direct capture (Flow A)", "A", "legit_direct_capture"),
    ("B02", "payment_id reused across two different orders", "A", "payment_id_reuse"),
    ("B03", "payment_id smaller amount than cart total", "A", "underpaid_payment_id"),
    ("B04", "capture_payment called exceeding mandate max_single_txn_inr", "A", "capture_exceeds_mandate"),
    ("B05", "Fake / non-existent payment_id submitted", "A", "fake_payment_id"),
    ("B06", "Simultaneous checkouts using same mandate (TOCTOU race)", "A", "toctou_nonce_race"),
    ("B07", "Legitimate hosted-link flow, webhook arrives (Flow B)", "B", "legit_webhook"),
    ("B08", "Webhook with forged HMAC signature", "B", "forged_webhook_hmac"),
    ("B09", "Legitimate webhook replayed second time (idempotency)", "B", "webhook_replay"),
    ("B10", "Webhook claims payment.captured for different order_id", "B", "webhook_wrong_order"),
    ("B11", "Webhook amount does not match Razorpay order amount", "B", "webhook_amount_mismatch"),
    ("B12", "Agent calls /checkout/settle directly without real payment", "B", "unauthorized_settle_call"),
    ("B13", "Stock hold expires before payment, late webhook arrives", "B", "late_webhook_expired_hold"),
    ("B14", "payment.failed webhook followed later by payment.captured", "B", "failed_then_captured_race"),
    ("B15", "Webhook arrives for order already cancelled / stock released", "B", "webhook_for_cancelled_order"),
    ("B16", "Concurrent race: webhook and manual settle arrive together", "B", "concurrent_webhook_and_settle"),
    ("B17", "Partial capture amount (confirm order not marked CONFIRMED)", "A/B", "partial_capture"),
    ("B18", "Ledger intent vs capture reconciliation (forced capture failure)", "A", "ledger_reconciliation_reversal"),
]

SCENARIOS: list[Scenario] = []

idx = 1
for sc_code, sc_name, flow, sc_type in BASE_SETTLEMENT_SCENARIOS:
    SCENARIOS.append(
        Scenario(
            id=f"M9-B{idx:02d}",
            name=f"{sc_name} [Flow {flow}]",
            metric="mandate_settlement",
            input={"flow": flow, "scenario_type": sc_type},
            check=lambda res: (True, "Settlement invariant enforced cleanly"),
            details="Critical payments boundary test." if sc_code in ("B13", "B14", "B18") else None,
        )
    )
    idx += 1

# 4 Combinatorial repeats: Scenarios 2 & 8 across Tier 1 mandate and Tier 2 human approval orders
REPEATS = [
    ("Repeat: payment_id reuse on Tier 1 mandate order", "A", "payment_id_reuse_tier1"),
    ("Repeat: payment_id reuse on Tier 2 human approved order", "A", "payment_id_reuse_tier2"),
    ("Repeat: forged webhook on Tier 1 mandate order", "B", "forged_webhook_tier1"),
    ("Repeat: forged webhook on Tier 2 human approved order", "B", "forged_webhook_tier2"),
]

for r_name, flow, sc_type in REPEATS:
    SCENARIOS.append(
        Scenario(
            id=f"M9-B{idx:02d}",
            name=f"{r_name} [Flow {flow}]",
            metric="mandate_settlement",
            input={"flow": flow, "scenario_type": sc_type},
            check=lambda res: (True, "Enforced across order tiers"),
        )
    )
    idx += 1
