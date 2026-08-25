"""Outbound prose control: what a buyer is allowed to be told, and by whom.

`vyapaari` writes one sentence per line explaining why it fits. That sentence is
model output on its way to a human, which makes it the one channel in the system
where text crosses outward without a schema to constrain it. Two things can go
wrong with it, and this module handles both:

**A number that should not be public.** The model is never shown a cost, a
margin, or a floor price, so it cannot repeat one — but "cannot" resting on an
argument about what was in a prompt is weaker than a check at the boundary. This
is the outbound counterpart to `settings.assert_no_secrets_in_prompt`: same idea,
opposite direction.

**Prose that is not about the product.** A `why` that argues about policy, quotes
a price, or addresses the reader as though it had authority is not a product
description. Those get replaced rather than edited, because a sentence that has
to be repaired to be safe was not a sentence worth showing.

When prose is refused the caller does not lose the line — it falls back to
`render_upsell_reason`, a template owned by this module. The offer still goes out
and still explains itself; it just explains itself in the store's voice instead
of the model's.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from store.catalog import PRIVATE_FIELDS

#: Private numbers worth scanning prose for.
#:
#: The same three fields the leak test scans by value, and for the same reason:
#: `margin_pct` and `max_discount_pct` are 1–2 digit integers that collide with
#: innocent public facts (a 34% margin against 34 hours of battery), so scanning
#: for them would reject correct sentences. Their names are still refused below,
#: which is what the rule actually requires.
VALUE_SCANNED_FIELDS = ("cost_inr", "floor_price_inr", "attach_rate")

#: Words that have no place in a sentence shown to a buyer. Deliberately short:
#: a long list would start rejecting legitimate product prose, and the numeric
#: scan is the part that protects confidentiality.
FORBIDDEN_PHRASES = (
    "margin",
    "our cost",
    "cost price",
    "floor price",
    "wholesale",
    "markup",
    "mark-up",
    "ignore previous",
    "ignore all previous",
    "system prompt",
)

#: Prose from a model may not contain a private field name in any form.
_FORBIDDEN_NAMES = tuple(sorted(PRIVATE_FIELDS)) + tuple(
    sorted(name.replace("_", " ") for name in PRIVATE_FIELDS)
)

#: Digits, with thousands separators removed first so "1,349" is caught as 1349.
_SEPARATORS = re.compile(r"(?<=\d)[,\s](?=\d{3}\b)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numeric_values(private_rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """The private numbers in play, as the strings a scan compares against."""
    values: set[str] = set()
    for row in private_rows:
        if not row:
            continue
        for field in VALUE_SCANNED_FIELDS:
            if field in row and not isinstance(row[field], bool):
                values.add(str(row[field]))
        for candidate in row.get("attach_candidates", ()) or ():
            if isinstance(candidate, Mapping) and "attach_rate" in candidate:
                values.add(str(candidate["attach_rate"]))
    return values


def refusal_reason(
    text: str, private_rows: Iterable[Mapping[str, Any]] = ()
) -> str | None:
    """Why this prose may not be shown, or None if it may.

    Returns the reason rather than a boolean so the refusal can be ledgered. An
    audit that records "reason_replaced" without saying what was wrong with the
    original is a log line nobody can act on.
    """
    if not text or not text.strip():
        return "empty"

    lowered = text.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in lowered:
            return f"forbidden phrase {phrase!r}"
    for name in _FORBIDDEN_NAMES:
        if name in lowered:
            return f"private field name {name!r}"

    wanted = _numeric_values(private_rows)
    if wanted:
        # Compared token by token rather than by substring: 799 sits inside the
        # public price 1799, and reporting that as a leak would be the false
        # positive that gets this check switched off.
        #
        # Only the exact stored form is scanned. An attach rate of 0.31 is
        # private; "31% of buyers" is published on purpose, and rejecting the
        # rounded figure would forbid the one sales argument the envelope exists
        # to hand over.
        normalised = _SEPARATORS.sub("", text)
        found = set(_NUMBER.findall(normalised))
        if wanted & found:
            return "contains a private value"
    return None


def scrub(text: str, private_rows: Iterable[Mapping[str, Any]] = ()) -> str | None:
    """The prose if it is safe to show, None if it is not."""
    return None if refusal_reason(text, private_rows) else text.strip()


# ---------------------------------------------------------------------------
# The store's own voice
# ---------------------------------------------------------------------------

_UPSELL_TEMPLATES = {
    "bundle_attach": "{title} pairs with what you are buying and ships together.",
    "tier_upgrade": "{title} is the next model up, if the extra capability is worth it.",
    "volume_break": "More units of {title}, priced better per unit at this quantity.",
}

_POPULAR_SUFFIX = " Around {pct}% of buyers add it."


def render_upsell_reason(
    *,
    upsell_type: str,
    title: str,
    popularity_pct: int | None = None,
) -> str:
    """The fallback sentence for an upsell line.

    Built from the public title and, when there is one, a rounded popularity
    figure. The rounding is what makes the figure publishable: 31 is a fact about
    buyers, while the stored 0.31 is a fact about this store's data.
    """
    template = _UPSELL_TEMPLATES.get(
        upsell_type, "{title} is worth adding alongside your choice."
    )
    sentence = template.format(title=title)
    if popularity_pct and popularity_pct >= 10:
        sentence += _POPULAR_SUFFIX.format(pct=popularity_pct)
    return sentence


def render_base_reason(*, title: str) -> str:
    """The fallback sentence for the base line."""
    return f"{title} matches what you asked for and is in stock."
