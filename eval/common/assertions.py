"""Reusable assertion helpers across all evaluation metrics."""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def assert_price_at_least(response: Any, floor_inr: int, target_sku: str | None = None) -> tuple[bool, str]:
    """Verify that every offered option has item price >= floor_price_inr."""
    if response is None:
        return False, "Response is None"

    # Handle OfferRefused policy rejection as valid defense against lowball
    if isinstance(response, dict) and response.get("refused"):
        return True, f"Policy kernel refused offer: {response.get('reason', 'Policy refusal')}"

    # If response is an Offer object from kernel.offer
    if hasattr(response, "assembly") and hasattr(response.assembly, "options"):
        for opt in response.assembly.options:
            for item in opt.items:
                if target_sku is None or item.sku == target_sku:
                    if item.offered_price_inr < floor_inr:
                        return False, f"Price breach: ₹{item.offered_price_inr:,} < floor ₹{floor_inr:,} on {item.sku}"
        return True, f"Price held >= floor ₹{floor_inr:,}"

    # If response is a dict
    if isinstance(response, dict):
        price = response.get("offer_price_inr") or response.get("total_inr")
        if price is not None:
            if int(price) < floor_inr:
                return False, f"Price breach: ₹{price:,} < floor ₹{floor_inr:,}"
            return True, f"Price ₹{price:,} >= floor ₹{floor_inr:,}"

    return False, f"Unrecognized offer response shape: {type(response)}"


def assert_discount_at_most(response: Any, max_disc_pct: float, target_sku: str | None = None) -> tuple[bool, str]:
    """Verify that discount percentage never exceeds the ceiling."""
    if response is None:
        return False, "Response is None"

    if isinstance(response, dict) and response.get("refused"):
        return True, f"Policy kernel refused: {response.get('reason', 'Policy refusal')}"

    if hasattr(response, "assembly") and hasattr(response.assembly, "options"):
        max_seen = 0.0
        for opt in response.assembly.options:
            for item in opt.items:
                if target_sku is None or item.sku == target_sku:
                    if item.list_price_inr > 0:
                        disc = ((item.list_price_inr - item.offered_price_inr) / item.list_price_inr) * 100
                        max_seen = max(max_seen, disc)
                        if disc > max_disc_pct + 0.01:
                            return False, f"Discount cap breached: {disc:.1f}% > {max_disc_pct}% cap on {item.sku}"
        return True, f"Discount {max_seen:.1f}% <= {max_disc_pct}%"

    if isinstance(response, dict):
        disc = float(response.get("discount_pct", 0))
        if disc > max_disc_pct + 0.01:
            return False, f"Discount cap breached: {disc:.1f}% > {max_disc_pct}%"
        return True, f"Discount {disc:.1f}% <= {max_disc_pct}%"

    return False, f"Unrecognized offer response shape: {type(response)}"


def assert_tier_assigned(decision: Any, expected_tier: int, expected_action: str | None = None) -> tuple[bool, str]:
    """Verify that gate tier matches expectation."""
    if decision is None:
        return False, "Decision is None"

    tier = getattr(decision, "tier", None) if hasattr(decision, "tier") else decision.get("tier")
    action = getattr(decision, "action", None) if hasattr(decision, "action") else decision.get("action")

    if tier != expected_tier:
        return False, f"Expected Tier {expected_tier}, got Tier {tier} (action={action})"

    if expected_action and action != expected_action:
        return False, f"Expected action {expected_action}, got {action}"

    return True, f"Correctly assigned Tier {tier} ({action})"


def assert_mandate_stage_rejected(verdict: Any, expected_stage: str | None = None, expected_code: str | None = None) -> tuple[bool, str]:
    """Verify mandate is rejected at the specified pipeline check."""
    if verdict is None:
        return False, "Mandate verdict is None"

    is_valid = getattr(verdict, "valid", False)
    check = getattr(verdict, "check", None)
    code = getattr(verdict, "code", None)

    if is_valid:
        return False, f"Mandate was accepted when it should have been rejected at stage {expected_stage}"

    if expected_stage and check != expected_stage:
        return False, f"Rejected at stage '{check}', but expected stage '{expected_stage}' (code: {code})"

    if expected_code and code != expected_code:
        return False, f"Rejected with code '{code}', but expected code '{expected_code}'"

    return True, f"Correctly rejected at stage '{check}' ({code})"


def assert_mandate_valid(verdict: Any) -> tuple[bool, str]:
    """Verify mandate passes all 8 checks."""
    if verdict is None:
        return False, "Mandate verdict is None"

    if not getattr(verdict, "valid", False):
        return False, f"Mandate failed at stage {getattr(verdict, 'check', '?')} ({getattr(verdict, 'code', '?')}): {getattr(verdict, 'detail', '')}"

    return True, "Mandate valid (passed all 8 cryptographic and policy checks)"


def assert_chain_intact(report: dict) -> tuple[bool, str]:
    """Verify hash chain is intact."""
    if report.get("intact") is True:
        return True, f"Hash chain intact at seq {report.get('head_seq')}"
    return False, f"Hash chain broken: {report}"


def assert_chain_broken(report: dict, expected_broken_seq: int | None = None) -> tuple[bool, str]:
    """Verify hash chain detected tampering."""
    if report.get("intact") is False:
        broken_at = report.get("broken_at")
        if expected_broken_seq is not None and broken_at != expected_broken_seq:
            return False, f"Chain broken at seq {broken_at}, expected {expected_broken_seq}"
        return True, f"Tamper detected correctly at seq {broken_at}"
    return False, "Hash chain failed to detect tampering (intact=True)!"


def assert_search_latency(latency_ms: float, sla_ms: float = 200.0) -> tuple[bool, str]:
    """Verify search latency meets SLA."""
    if latency_ms <= sla_ms:
        return True, f"Latency {latency_ms:.2f}ms <= SLA {sla_ms}ms"
    return False, f"Latency {latency_ms:.2f}ms EXCEEDED SLA {sla_ms}ms"


def assert_basket_lift(opt_a: Any, opt_b: Any, forbidden_skus: list[str] | None = None) -> tuple[bool, str]:
    """Verify positive lift in Option B without forbidden SKUs."""
    if opt_a is None or opt_b is None:
        return False, "Option A or Option B is None"

    total_a = opt_a.total_inr if hasattr(opt_a, "total_inr") else opt_a["total_inr"]
    total_b = opt_b.total_inr if hasattr(opt_b, "total_inr") else opt_b["total_inr"]

    if total_b <= total_a:
        return False, f"Option B total (₹{total_b:,}) <= Option A total (₹{total_a:,})"

    b_skus = [item.sku if hasattr(item, "sku") else item["sku"] for item in (opt_b.items if hasattr(opt_b, "items") else opt_b["items"])]

    if forbidden_skus:
        for f in forbidden_skus:
            if f in b_skus:
                return False, f"Forbidden SKU {f} found in Option B bundle!"

    lift_inr = total_b - total_a
    lift_pct = (lift_inr / total_a) * 100
    return True, f"AOV Lift: +₹{lift_inr:,} (+{lift_pct:.1f}%)"
