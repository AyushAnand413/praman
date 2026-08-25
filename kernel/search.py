"""Deterministic product search. No model, no embeddings, no network.

Two callers need to turn a sentence into a shortlist of SKUs, and neither can
afford to ask a model to do it:

* the catalog endpoint, which has a 200ms budget and answers before any model is
  involved;
* the fallback offer path, which runs precisely because no model is available.

So this is token matching over the fields the catalog already publishes, with a
weight per field and a small expansion table for the words buyers actually use.
It is a worse retriever than a semantic one and it is meant to be — its job is to
be *predictable*, so that the same sentence always returns the same shortlist and
a wrong result can be traced to a missing word rather than to a temperature.

The expansion table is hand-maintained and that is a real cost. It stays small on
purpose: a buyer typing "workout" should find the IPX7 earbuds, and a buyer
typing something nobody anticipated gets a relevance of zero and a
cheapest-first list, which is a plain storefront rather than a broken one.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

#: Words that appear in half the catalog and in most sentences. Matching on them
#: makes everything equally relevant, which is the same as matching on nothing.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "best", "but", "buy",
        "can", "for", "from", "get", "good", "has", "have", "how", "i", "if",
        "in", "is", "it", "me", "my", "need", "new", "no", "not", "of", "on",
        "one", "or", "our", "please", "smth", "some", "something", "that",
        "the", "their", "them", "then", "there", "these", "they", "this", "to",
        "too", "under", "up", "use", "want", "wants", "was", "we", "what",
        "which", "will", "with", "would", "you", "your",
    }
)

#: Buyer vocabulary mapped onto catalog vocabulary. One-directional: a query
#: token expands into catalog tokens, never the reverse, so adding an entry can
#: only widen what a search finds and never change what a product claims to be.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "workout": ("gym",),
    "workouts": ("gym",),
    "exercise": ("gym",),
    "running": ("gym",),
    "run": ("gym",),
    "sport": ("gym",),
    "sports": ("gym",),
    "training": ("gym",),
    "flight": ("travel",),
    "flights": ("travel",),
    "flying": ("travel",),
    "plane": ("travel",),
    "trip": ("travel",),
    "holiday": ("travel",),
    "office": ("calls", "desk"),
    "meeting": ("calls",),
    "meetings": ("calls",),
    "call": ("calls",),
    "zoom": ("calls",),
    "noise": ("anc",),
    "cancelling": ("anc",),
    "canceling": ("anc",),
    "cancellation": ("anc",),
    "quiet": ("anc", "isolation"),
    "silence": ("anc", "isolation"),
    "waterproof": ("water", "ipx7", "ip67"),
    "sweatproof": ("water", "ipx7"),
    "sweat": ("water",),
    "rain": ("water",),
    "shower": ("water",),
    "commuting": ("commute",),
    "train": ("commute",),
    "bus": ("commute",),
    "podcast": ("creator",),
    "podcasts": ("creator",),
    "recording": ("creator", "studio"),
    "streaming": ("creator",),
    "youtube": ("creator",),
    "mixing": ("studio",),
    "mastering": ("studio",),
    "reference": ("studio",),
    "studying": ("study",),
    "revision": ("study",),
    "loud": ("party",),
    "bass": ("party",),
    "garden": ("outdoor",),
    "camping": ("outdoor",),
    "picnic": ("outdoor",),
    "charger": ("charging", "pad"),
    "charge": ("charging",),
    "wire": ("cable", "wired"),
    "aux": ("wired", "dac"),
    "mic": ("microphone", "lavalier"),
    "buds": ("earbuds", "earbud"),
    "tws": ("true", "wireless"),
    "overear": ("over", "ear"),
    "headset": ("headphones", "headphone"),
    "case": ("carry", "case"),
    "pouch": ("case",),
    "tips": ("tip", "foam"),
    "eartips": ("tip", "foam"),
}

#: How much a match is worth, by where it was found. A word in the title is a
#: stronger signal than the same word buried in an attribute list.
_WEIGHT_TITLE = 4
_WEIGHT_SKU = 4
_WEIGHT_CATEGORY = 3
_WEIGHT_ATTRS = 2

_SPLIT = re.compile(r"[^a-z0-9]+")


def tokenize(text: Any) -> set[str]:
    """Lowercase alphanumeric tokens, plus a naive singular for each plural.

    Both sides of the comparison run through this, so "earbuds" in a query and
    "Earbuds" in a title meet in the middle without a stemmer as a dependency.
    """
    out: set[str] = set()
    for piece in _SPLIT.split(str(text).lower()):
        if len(piece) < 2 or piece in STOPWORDS:
            continue
        out.add(piece)
        if len(piece) > 3 and piece.endswith("s") and not piece.endswith("ss"):
            out.add(piece[:-1])
    return out


def _flatten(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            yield key
            yield from _flatten(inner)
    elif isinstance(value, (list, tuple, set)):
        for inner in value:
            yield from _flatten(inner)
    elif isinstance(value, bool):
        # A bare `True` would tokenize to "true" and make every flag look like
        # the same word. Only the key matters, and the key is yielded above.
        return
    else:
        yield value


def _derived_tokens(attrs: Mapping[str, Any]) -> set[str]:
    """Words a buyer uses for facts the catalog states in another form.

    `ip_rating: "IPX7"` is what the spec sheet says; "waterproof" is what the
    buyer types. Deriving the second from the first here keeps the derivation in
    one place instead of scattering it through the synonym table.
    """
    out: set[str] = set()
    rating = str(attrs.get("ip_rating", "")).lower()
    if rating.startswith("ip") and rating not in ("", "none"):
        out.update({"water", "waterproof", "resistant"})
    if attrs.get("water_resistant"):
        out.update({"water", "waterproof", "resistant"})
    if attrs.get("anc"):
        out.update({"anc", "noise", "cancelling"})
    if attrs.get("wired_mode"):
        out.add("wired")
    form = str(attrs.get("form_factor", "")).lower()
    if "wireless" in form:
        out.add("wireless")
    if "over_ear" in form:
        out.update({"over", "ear", "headphones", "headphone"})
    return out


def expand(query: str) -> set[str]:
    """Query tokens plus their catalog-vocabulary equivalents."""
    tokens = tokenize(query)
    expanded = set(tokens)
    for token in tokens:
        expanded.update(SYNONYMS.get(token, ()))
    return expanded


def relevance(
    query: str,
    *,
    sku: str,
    title: str,
    category: str,
    attrs: Mapping[str, Any] | None = None,
) -> int:
    """How well one product answers one sentence. Zero means no signal at all.

    Counted per distinct query token, so repeating a word in the query cannot
    inflate a score, and a product matching two different words always outranks
    one matching the same word twice.
    """
    wanted = expand(query)
    if not wanted:
        return 0
    attrs = attrs or {}
    fields = (
        (tokenize(title), _WEIGHT_TITLE),
        (tokenize(sku), _WEIGHT_SKU),
        (tokenize(category), _WEIGHT_CATEGORY),
        (
            {t for value in _flatten(attrs) for t in tokenize(value)}
            | {t for key in attrs for t in tokenize(key)}
            | _derived_tokens(attrs),
            _WEIGHT_ATTRS,
        ),
    )
    total = 0
    for token in wanted:
        # The best field this token was found in, not the sum across fields: a
        # word appearing in both the title and the attrs is one piece of
        # evidence stated twice.
        best = 0
        for haystack, weight in fields:
            if token in haystack and weight > best:
                best = weight
        total += best
    return total


def relevance_for_row(row: Mapping[str, Any], query: str) -> int:
    """`relevance` against a public catalog row."""
    return relevance(
        query,
        sku=str(row.get("sku", "")),
        title=str(row.get("title", "")),
        category=str(row.get("category", "")),
        attrs=row.get("attrs") or {},
    )
