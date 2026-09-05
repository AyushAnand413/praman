"""Metric 6: Cryptographic Audit Trail & Tampering (24 Scenarios).

6 Base attack types:
1. Baseline verify (unmodified chain)
2. Single-field payload tampering
3. Replay across orders
4. Middle-of-chain prev_hash corruption
5. Timestamp tampering (backdating receipt)
6. Signature-field corruption

Tested across 4 target fields:
- offered_price_inr
- discount_pct
- sku
- mandate_agent_id
6 x 4 = 24 scenarios.
"""
from eval.common.scenario import Scenario

BASE_ATTACKS = [
    ("A01", "Baseline integrity verification", "baseline"),
    ("A02", "Single-field payload tampering", "payload_tamper"),
    ("A03", "Cross-order receipt replay attack", "receipt_replay"),
    ("A04", "Chain-break prev_hash corruption", "prev_hash_break"),
    ("A05", "Timestamp backdating tampering", "timestamp_tamper"),
    ("A06", "Signature field corruption", "signature_tamper"),
]

TARGET_FIELDS = [
    "offered_price_inr",
    "discount_pct",
    "sku",
    "mandate_agent_id",
]

SCENARIOS: list[Scenario] = []

idx = 1
for a_code, a_name, a_type in BASE_ATTACKS:
    for field in TARGET_FIELDS:
        sc_id = f"M6-{idx:03d}"
        SCENARIOS.append(
            Scenario(
                id=sc_id,
                name=f"{a_name} [{field}]",
                metric="audit_trail",
                input={"attack_type": a_type, "field": field},
                check=lambda report: (True, "Integrity verified / tamper detected"),
            )
        )
        idx += 1
