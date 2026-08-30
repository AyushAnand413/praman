"""One classification of ledger events for every merchant-facing surface.

The feed and the order trail both need to know whether an event went well, went
wrong, or is still in flight. That question was previously answered in the
browser by a hardcoded set of event names, which meant an event the set had
never heard of rendered as though nothing had happened — a blocked sale looked
like a quiet one.

So the answer is computed here, server-side, and travels with the entry as
`tone` and `is_failure`.

`classify` is deliberately two-layered. `_TONES` is the explicit answer for the
events this codebase writes today. `_BAD_MARKERS` is the backstop: an event name
nobody has classified yet still reads as a failure if it *says* it is one. That
second layer is the part that matters, because the recurring bug is not a wrong
entry in the map — it is a new event that never got added to it.
"""

from __future__ import annotations

from typing import Any, Literal

Tone = Literal["good", "bad", "warn", "neutral"]

#: Explicit tone per event this codebase writes. Grouped by what the merchant
#: reading the feed is being told, not by which module emits it.
_TONES: dict[str, Tone] = {
    # Money arrived, policy held, a store came online.
    "payment.captured": "good",
    "payment.authorized": "good",
    "offer.issued": "good",
    "catalog.synced": "good",
    "ledger.genesis": "good",
    "mandate.accepted": "good",
    "approval.granted": "good",
    "budget.committed": "good",
    # In flight, or waiting on a human. Not wrong yet.
    "payment.intent": "warn",
    "razorpay.order.created": "warn",
    "order.held_for_approval": "warn",
    "approval.countered": "warn",
    "checkout.abandoned": "warn",
    "checkout.idempotent_replay": "warn",
    "payment.shadow_skipped": "warn",
    "policy.selfheal": "warn",
    "webhook.ignored": "warn",
    "fulfillment.check": "warn",
    # Refused by policy, broken, or reversed. A refund completing is mechanically
    # a success, but it is shown as a failure because it means an earlier sale
    # did not hold — the merchant wants it to stand out, not to read as revenue.
    "payment.failed": "bad",
    "payment.declined": "bad",
    "payment.amount_mismatch": "bad",
    "payment.order_mismatch": "bad",
    "offer.refused": "bad",
    "checkout.rejected": "bad",
    "pre_filter.rejected_all": "bad",
    "approval.rejected": "bad",
    "approval.revalidation_failed": "bad",
    "saga.compensation_triggered": "bad",
    "ledger.compensate": "bad",
    "razorpay.refund": "bad",
    "refund.processed": "bad",
    "stock.commit_anomaly": "bad",
    "learning.record_failed": "bad",
    "webhook.rejected": "bad",
    "webhook.unmatched": "bad",
    # Informational: the machine narrating itself.
    "catalog.query": "neutral",
    "offer.request": "neutral",
    "offer.evaluated": "neutral",
    "proposal.emitted": "neutral",
    "optimizer.ranked": "neutral",
    "policy.updated": "neutral",
    "notify.buyer": "neutral",
    "notify.merchant": "neutral",
}

#: Substrings that mark an unclassified event as a failure. The point of this
#: list is to fail loud: a new `*.rejected` or `*_mismatch` event highlights on
#: the day it is written, without waiting for someone to remember this file.
_BAD_MARKERS = (
    "reject",
    "refus",
    "fail",
    "declin",
    "mismatch",
    "anomaly",
    "compensat",
    "refund",
    "unmatched",
    "breach",
    "tamper",
    "oversell",
    "oversold",
)

#: Substrings that mark an unclassified event as in-flight rather than finished.
_WARN_MARKERS = ("pending", "held", "hold", "intent", "abandon", "retry", "stale")


def classify(event: str | None) -> Tone:
    """Return the tone for one ledger event name.

    Unknown names are resolved by marker, then default to neutral. Neutral is
    the right default only because anything that names itself a failure has
    already been caught above.
    """
    if not event:
        return "neutral"
    known = _TONES.get(event)
    if known is not None:
        return known
    lowered = event.lower()
    if any(marker in lowered for marker in _BAD_MARKERS):
        return "bad"
    if any(marker in lowered for marker in _WARN_MARKERS):
        return "warn"
    return "neutral"


def is_failure(event: str | None) -> bool:
    """True when the event represents a refused, broken, or reversed outcome."""
    return classify(event) == "bad"


def annotate(entry: dict[str, Any]) -> dict[str, Any]:
    """Return `entry` plus its `tone` and `is_failure`.

    Takes and returns a plain dict so it can sit on the edge of any endpoint
    that has already shaped a ledger row for the wire.
    """
    tone = classify(entry.get("event"))
    return {**entry, "tone": tone, "is_failure": tone == "bad"}
