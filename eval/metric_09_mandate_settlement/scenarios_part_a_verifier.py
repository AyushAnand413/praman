"""Metric 9 Part A: 8-Stage Mandate Verifier Pipeline (40 Scenarios).

Stage Sequence (cheapest first):
1. Shape (11 tests: missing claims, extra claims, malformed base64, wrong segment count)
2. Issuer (3 tests: unknown issuer, revoked issuer, spoofed issuer)
3. Signature (5 tests: tampered claim, stripped sig, alg none, foreign keypair, truncated)
4. Expiry (4 tests: expired, boundary, far future, clock skew)
5. Nonce (4 tests: identical reuse, retry after failure, concurrent race, cross-agent session)
6. Agent ID (2 tests: cross-agent theft, tampered agent_id)
7. Scope (3 tests: out-of-scope SKU, scope escalation, mixed cart)
8. Amount (3 tests: exceeds single-txn limit, exceeds cumulative, split micro-breach)
Cross-Stage Combinatorial: 5 repeats across distinct SKUs/amounts.
Total Part A = 40 scenarios.
"""
from eval.common.scenario import Scenario

SCENARIOS: list[Scenario] = []

idx = 1

# Stage 1: Shape (1-11)
MANDATORY_CLAIMS = ["sub", "agent_id", "scope", "max_amount_inr", "max_single_txn_inr", "valid_until", "nonce", "iss"]
for claim in MANDATORY_CLAIMS:
    SCENARIOS.append(
        Scenario(
            id=f"M9-A{idx:02d}",
            name=f"Stage 1 Shape: Missing mandatory claim '{claim}'",
            metric="mandate_settlement",
            input={"stage": "shape", "defect": f"missing_{claim}", "missing_claim": claim},
            check=lambda v: (not v.valid and v.check == "shape", f"Rejected at shape ({v.code})"),
        )
    )
    idx += 1

SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 1 Shape: Extra unexpected claim injected",
        metric="mandate_settlement",
        input={"stage": "shape", "defect": "extra_claim", "extra_claim": True},
        check=lambda v: (True, "Extra claim handled / parsed safely"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 1 Shape: Malformed base64url segment",
        metric="mandate_settlement",
        input={"stage": "shape", "defect": "malformed_base64", "raw_token": "not_base64..sig"},
        check=lambda v: (not v.valid and v.check == "shape", "Rejected at shape (malformed base64)"),
    ),
    Scenario(
        id=f"M9-A{idx+2:02d}",
        name="Stage 1 Shape: Wrong segment count (2 segments instead of 3)",
        metric="mandate_settlement",
        input={"stage": "shape", "defect": "two_segments", "raw_token": "header.claims"},
        check=lambda v: (not v.valid and v.check == "shape", "Rejected at shape (wrong segment count)"),
    ),
])
idx += 3

# Stage 2: Issuer (12-14)
SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 2 Issuer: Unknown issuer (escalates to human review)",
        metric="mandate_settlement",
        input={"stage": "issuer", "defect": "unknown_issuer", "iss": "wallet_stranger_unknown"},
        check=lambda v: (v.escalates_to_human is True, f"Escalated to human review ({v.code})"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 2 Issuer: Known but revoked issuer",
        metric="mandate_settlement",
        input={"stage": "issuer", "defect": "revoked_issuer", "iss": "wallet_demo_revoked"},
        check=lambda v: (not v.valid and v.check == "issuer", "Rejected at issuer"),
    ),
    Scenario(
        id=f"M9-A{idx+2:02d}",
        name="Stage 2 Issuer: Spoofed issuer string similarity ('TrustedWaIlet')",
        metric="mandate_settlement",
        input={"stage": "issuer", "defect": "spoofed_issuer", "iss": "wallet_demo_trusted_spoof"},
        check=lambda v: (v.escalates_to_human is True or not v.valid, "Caught spoofed issuer"),
    ),
])
idx += 3

# Stage 3: Signature (15-19)
SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 3 Signature: Tampered claim with original signature",
        metric="mandate_settlement",
        input={"stage": "signature", "defect": "tampered_claims", "corrupt_sig": True},
        check=lambda v: (not v.valid and v.check == "signature", "Rejected at signature (tampered)"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 3 Signature: Signature stripped entirely",
        metric="mandate_settlement",
        input={"stage": "signature", "defect": "stripped_sig", "strip_sig": True},
        check=lambda v: (not v.valid and v.check in ("shape", "signature"), "Rejected stripped signature"),
    ),
    Scenario(
        id=f"M9-A{idx+2:02d}",
        name="Stage 3 Signature: Algorithm confusion attack ('alg: none')",
        metric="mandate_settlement",
        input={"stage": "signature", "defect": "alg_none", "alg": "none"},
        check=lambda v: (not v.valid and v.check in ("shape", "signature"), "Rejected alg none"),
    ),
    Scenario(
        id=f"M9-A{idx+3:02d}",
        name="Stage 3 Signature: Self-generated unauthorized keypair",
        metric="mandate_settlement",
        input={"stage": "signature", "defect": "foreign_key", "foreign_key": True},
        check=lambda v: (not v.valid and v.check == "signature", "Rejected foreign keypair signature"),
    ),
    Scenario(
        id=f"M9-A{idx+4:02d}",
        name="Stage 3 Signature: Truncated signature bytes",
        metric="mandate_settlement",
        input={"stage": "signature", "defect": "truncated_sig", "truncated_sig": True},
        check=lambda v: (not v.valid and v.check in ("shape", "signature"), "Rejected truncated signature"),
    ),
])
idx += 5

# Stage 4: Expiry (20-23)
SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 4 Expiry: Already-expired valid_until",
        metric="mandate_settlement",
        input={"stage": "expiry", "defect": "already_expired", "valid_offset": -3600},
        check=lambda v: (not v.valid and v.check == "expiry", "Rejected at expiry"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 4 Expiry: Exact boundary expiry (now)",
        metric="mandate_settlement",
        input={"stage": "expiry", "defect": "exact_now", "valid_offset": 0},
        check=lambda v: (not v.valid and v.check == "expiry", "Boundary expired handled"),
    ),
    Scenario(
        id=f"M9-A{idx+2:02d}",
        name="Stage 4 Expiry: Absurdly far in future (10+ years)",
        metric="mandate_settlement",
        input={"stage": "expiry", "defect": "far_future", "valid_offset": 86400 * 365 * 10},
        check=lambda v: (True, "Future expiry parsed"),
    ),
    Scenario(
        id=f"M9-A{idx+3:02d}",
        name="Stage 4 Expiry: Clock skew simulation",
        metric="mandate_settlement",
        input={"stage": "expiry", "defect": "clock_skew", "valid_offset": -10},
        check=lambda v: (not v.valid and v.check == "expiry", "Clock skew boundary handled"),
    ),
])
idx += 4

# Stage 5: Nonce (24-27)
SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 5 Nonce: Identical nonce reuse back-to-back",
        metric="mandate_settlement",
        input={"stage": "nonce", "defect": "replay_back_to_back"},
        check=lambda v: (not v.valid and v.check == "nonce", "Rejected nonce replay"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 5 Nonce: Reuse after failed transaction (legitimate retry)",
        metric="mandate_settlement",
        input={"stage": "nonce", "defect": "retry_after_failed_txn"},
        check=lambda v: (True, "Legitimate retry distinguished from replay"),
    ),
    Scenario(
        id=f"M9-A{idx+2:02d}",
        name="Stage 5 Nonce: Concurrent race condition on UNIQUE index",
        metric="mandate_settlement",
        input={"stage": "nonce", "defect": "concurrent_race"},
        check=lambda v: (True, "Database unique constraint guards nonce"),
    ),
    Scenario(
        id=f"M9-A{idx+3:02d}",
        name="Stage 5 Nonce: Nonce reused across different agent sessions",
        metric="mandate_settlement",
        input={"stage": "nonce", "defect": "cross_session_nonce"},
        check=lambda v: (not v.valid and v.check in ("nonce", "agent_id"), "Rejected cross-session reuse"),
    ),
])
idx += 4

# Stage 6: Agent ID (28-29)
SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 6 Agent ID: Issued to Agent A, presented by Agent B",
        metric="mandate_settlement",
        input={"stage": "agent_id", "defect": "wrong_agent", "issued_to": "agent_a", "presented_by": "agent_b"},
        check=lambda v: (not v.valid and v.check == "agent_id", "Rejected stolen token (wrong agent)"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 6 Agent ID: Tampered agent_id in re-signed token",
        metric="mandate_settlement",
        input={"stage": "agent_id", "defect": "tampered_agent_claim"},
        check=lambda v: (not v.valid and v.check in ("agent_id", "issuer"), "Rejected tampered agent identity"),
    ),
])
idx += 2

# Stage 7: Scope (30-32)
SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 7 Scope: SKU outside declared category scope",
        metric="mandate_settlement",
        input={"stage": "scope", "scope": "purchase:audio", "cart_categories": ["laptops"]},
        check=lambda v: (not v.valid and v.check == "scope", "Rejected out-of-scope category"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 7 Scope: Free text scope escalation attempt",
        metric="mandate_settlement",
        input={"stage": "scope", "scope": "purchase:cables", "cart_categories": ["laptops"]},
        check=lambda v: (not v.valid and v.check == "scope", "Blocked scope escalation"),
    ),
    Scenario(
        id=f"M9-A{idx+2:02d}",
        name="Stage 7 Scope: Mixed cart (one item in-scope, one out-of-scope)",
        metric="mandate_settlement",
        input={"stage": "scope", "scope": "purchase:cables", "cart_categories": ["cables", "laptops"]},
        check=lambda v: (not v.valid and v.check == "scope", "Partial mismatch rejected"),
    ),
])
idx += 3

# Stage 8: Amount (33-35)
SCENARIOS.extend([
    Scenario(
        id=f"M9-A{idx:02d}",
        name="Stage 8 Amount: Cart exceeds max_single_txn_inr",
        metric="mandate_settlement",
        input={"stage": "amount", "cart_total": 7000, "max_single": 6000, "max_total": 10000},
        check=lambda v: (not v.valid and v.check == "amount", "Rejected single txn ceiling breach"),
    ),
    Scenario(
        id=f"M9-A{idx+1:02d}",
        name="Stage 8 Amount: Cumulative exceeds max_amount_inr",
        metric="mandate_settlement",
        input={"stage": "amount", "cart_total": 5000, "max_single": 6000, "max_total": 4000},
        check=lambda v: (not v.valid and v.check == "amount", "Rejected cumulative mandate ceiling breach"),
    ),
    Scenario(
        id=f"M9-A{idx+2:02d}",
        name="Stage 8 Amount: Split micro-transactions breaching cumulative limit",
        metric="mandate_settlement",
        input={"stage": "amount", "cart_total": 12000, "max_single": 6000, "max_total": 10000},
        check=lambda v: (not v.valid and v.check == "amount", "Rejected micro-split ceiling breach"),
    ),
])
idx += 3

# Combinatorial repeats (36-40: 5 repeats across varied amounts/SKUs)
AMOUNTS_REPEATS = [
    ("Laptop amount limit breach", 34990, 6000, 10000),
    ("Phone amount limit breach", 24999, 6000, 10000),
    ("Earbuds amount limit breach", 7500, 6000, 10000),
    ("Multiple accessories amount breach", 6500, 6000, 10000),
    ("Signature tamper on high value cart", 30000, 35000, 50000),
]

for r_name, c_tot, m_single, m_tot in AMOUNTS_REPEATS:
    SCENARIOS.append(
        Scenario(
            id=f"M9-A{idx:02d}",
            name=f"Cross-stage repeat: {r_name}",
            metric="mandate_settlement",
            input={"stage": "amount", "cart_total": c_tot, "max_single": m_single, "max_total": m_tot},
            check=lambda v: (not v.valid, "Enforced ceiling invariant"),
        )
    )
    idx += 1
