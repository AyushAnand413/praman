"""Mock generators and fault injection helpers for Metric 4."""
from __future__ import annotations

import json
import time
from typing import Any
from vyapaari.gemini import LLMUnavailable


def make_mock_generator(mode: str):
    """Return a generator simulating a specific failure mode."""
    if mode == "rate_limit_429":
        def gen(**kwargs):
            raise LLMUnavailable("HTTP 429: Provider TPM limit exceeded")
        return gen
    elif mode == "timeout_504":
        def gen(**kwargs):
            raise LLMUnavailable("HTTP 504: Gateway Timeout after 15000ms")
        return gen
    elif mode == "malformed_json":
        def gen(**kwargs):
            return "{\"proposal\": [\"broken json... unclosed string"
        return gen
    elif mode == "missing_fields":
        def gen(**kwargs):
            return json.dumps({"status": "ok", "unexpected_payload": True})
        return gen
    elif mode == "hallucinated_sku":
        def gen(**kwargs):
            return json.dumps({
                "candidates": [{
                    "base": {"sku": "GE-ACER-ALITE", "qty": 1, "discount_pct": "0", "why": "Laptop"},
                    "proposed_upsells": [{"sku": "GE-NONEXISTENT-ITEM-999", "qty": 1, "discount_pct": "0", "type": "bundle_attach"}]
                }]
            })
        return gen
    elif mode == "slow_boundary":
        def gen(**kwargs):
            time.sleep(0.05)
            return json.dumps({
                "candidates": [{
                    "base": {"sku": "GE-ACER-ALITE", "qty": 1, "discount_pct": "0", "why": "Laptop"},
                    "proposed_upsells": []
                }]
            })
        return gen
    elif mode == "repeated_failures":
        def gen(**kwargs):
            raise LLMUnavailable("Repeated failure 3/3 in current session")
        return gen

    return None
