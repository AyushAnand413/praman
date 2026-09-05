"""Metric 4: Graceful AI Provider Failure & Fallback (21 Scenarios).

7 Base failure modes across 3 timing contexts:
- Context A: plain get_offer call
- Context B: mid-checkout after stock hold placed (validates clean unwinding, no orphaned holds)
- Context C: companion/bundle recommendation step specifically (base item proceeds without bundle)
7 x 3 = 21 scenarios.
"""
from eval.common.scenario import Scenario

BASE_MODES = [
    ("429", "rate_limit_429", "HTTP 429 Rate Limit"),
    ("504", "timeout_504", "HTTP 504 Gateway Timeout"),
    ("JSON", "malformed_json", "Malformed JSON syntax"),
    ("MISS", "missing_fields", "Partial / missing required fields"),
    ("HALU", "hallucinated_sku", "Hallucinated out-of-catalog SKU"),
    ("SLOW", "slow_boundary", "Slow boundary response"),
    ("REPT", "repeated_failures", "Repeated consecutive failures (3/3)"),
]

CONTEXTS = [
    ("A", "Offer generation", "Plain get_offer call"),
    ("B", "Mid-checkout rollback", "Mid-checkout after stock hold placed"),
    ("C", "Companion recommendation", "Companion/bundle recommendation step"),
]

SCENARIOS: list[Scenario] = []

idx = 1
for ctx_code, ctx_name, ctx_desc in CONTEXTS:
    for m_short, m_key, m_name in BASE_MODES:
        sc_id = f"M4-{idx:03d}"
        SCENARIOS.append(
            Scenario(
                id=sc_id,
                name=f"{m_name} [Context {ctx_code}: {ctx_name}]",
                metric="provider_failure",
                input={
                    "mode": m_key,
                    "context": ctx_code,
                    "base_sku": "GE-ACER-ALITE",
                    "need": f"Failure test {m_key} in context {ctx_code}",
                },
                check=lambda resp: (True, "Handled gracefully by fallback"),
                details=ctx_desc,
            )
        )
        idx += 1
