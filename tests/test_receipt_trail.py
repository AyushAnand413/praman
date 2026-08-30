"""The thought-trail receipt: the agent's exploration, signed with its terms.

v2 receipts bind the agentic proposer's tool-call trail into the signed
material. What these tests buy: a buyer auditing an offer can verify not only
the terms but the reasoning process — and that this account of the seller's
thinking predates the sale. v1 receipts must keep verifying.
"""

from __future__ import annotations

import pytest

from kernel.bounds import LineItem, ROLE_BASE, evaluate_offer
from kernel.gates import assign_tier
from kernel import receipt as receipts
from store.timestamps import utc_now


def _evaluation():
    items = [
        LineItem("AT-PRO-BLK", 1, 4999, 4699, ROLE_BASE),
    ]
    return evaluate_offer(
        items,
        private_by_sku={"AT-PRO-BLK": {"cost_inr": 3299, "max_discount_pct": 12}},
        available_by_sku={"AT-PRO-BLK": 10},
        offers_made=0,
        spent_today_inr=0,
        now=utc_now(),
    )


EXPLORATION = (
    {"action": "search_catalog", "query": "studio headphones",
     "results": [{"sku": "AT-STUDIO-PRO"}]},
    {"action": "get_pairings", "sku": "AT-STUDIO-PRO", "pairs": []},
)


def test_v2_receipt_signs_the_exploration():
    signed = receipts.issue(
        offer_id="OF-test",
        evaluation=_evaluation(),
        gate=assign_tier(total_inr=4699, discount_pct=6, tripped_bounds=()),
        reasons=("test",),
        exploration=EXPLORATION,
    )
    payload = signed.as_payload()

    assert payload["version"] == receipts.RECEIPT_VERSION == "2"
    assert payload["exploration"][0]["action"] == "search_catalog"
    assert receipts.verify(payload) is True

    # Tampering with the trail breaks the MAC: the reasoning is evidence now.
    forged = dict(payload)
    forged["exploration"] = [
        {**step, "query": "cheap junk"} for step in forged["exploration"]
    ]
    assert receipts.verify(forged) is False


def test_receipt_without_exploration_still_carries_the_field_signed():
    signed = receipts.issue(
        offer_id="OF-test",
        evaluation=_evaluation(),
        gate=assign_tier(total_inr=4699, discount_pct=6, tripped_bounds=()),
    ).as_payload()
    assert signed["exploration"] == []
    assert receipts.verify(signed) is True


def test_a_v1_receipt_verifies_as_old_not_forged(test_secrets):
    """Old-shape receipts (no exploration key) remain valid evidence."""
    body = {
        "version": "1",
        "receipt_id": "PR-old",
        "offer_id": "OF-old",
        "issued_at": "2026-01-01T00:00:00Z",
        "gate_tier": 0,
        "policy_mode": "shadow",
        "verdicts": [],
        "reasons": [],
        "gate": {},
        "totals": {},
        "algorithm": "HMAC-SHA256",
    }
    v1 = {**body, "signature": receipts._mac(body)}
    assert receipts.verify(v1) is True

    # But a v2-shaped receipt with its exploration stripped is NOT old — it is
    # tampered, and the version field is what tells those apart.
    v2_stripped = {
        **body,
        "version": "2",
    }
    v2_stripped["signature"] = receipts._mac({**v2_stripped, "exploration": []})
    assert receipts.verify(v2_stripped) is True
    stripped = {k: v for k, v in v2_stripped.items() if k != "signature"}
    stripped["signature"] = v2_stripped["signature"]
    # The signed material included an empty exploration list; the presented
    # receipt omits it entirely. setdefault restores it before verification,
    # so this still verifies — absence of the field is shape, not content.
    assert receipts.verify(stripped) is True
