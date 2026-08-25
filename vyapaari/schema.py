"""The proposal schema — the one shape the model is allowed to return.

Strict in both directions:

* Outbound, `RESPONSE_SCHEMA` is handed to the model as its declared response
  schema, so the decoder is the first line of defence rather than a regex over
  prose.
* Inbound, `parse` rebuilds the object from raw text and refuses anything the
  schema did not promise — an unknown key, a third upsell, an unrecognised
  upsell type, a discount that is not a number, any prose around the JSON.

Refusing unknown keys matters more than it looks. A model that invents
`"final_price_inr": 2999` and has it silently dropped produces a proposal that
*reads* as priced when nothing priced it. The parser raises instead, and the
caller retries once.

One deliberate omission: this module does not check whether a discount is
allowed. A 90% discount is a well-formed proposal and parses cleanly — the
policy kernel is what refuses it. Validating bounds here would hide the
model's misbehaviour behind a parse error and rob the kernel of the veto that
is the whole point of the architecture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

#: The three moves a seller may propose. Frozen: a fourth kind of upsell is a
#: product decision with policy consequences, not a prompt tweak.
BUNDLE_ATTACH = "bundle_attach"
TIER_UPGRADE = "tier_upgrade"
VOLUME_BREAK = "volume_break"

UPSELL_TYPES: tuple[str, ...] = (BUNDLE_ATTACH, TIER_UPGRADE, VOLUME_BREAK)

#: Two, not three. A cart the buyer has to read past is a cart they abandon,
#: and the session bound already limits how often we may ask.
MAX_UPSELLS = 2

#: Long enough for a real sentence, short enough that a model cannot smuggle a
#: paragraph of instructions into a field that gets shown to a human.
MAX_WHY_CHARS = 240

_BASE_KEYS = frozenset({"sku", "qty", "discount_pct", "why"})
_UPSELL_KEYS = frozenset({"type", "sku", "qty", "discount_pct", "why"})
_TOP_KEYS = frozenset({"base", "proposed_upsells"})


class SchemaError(ValueError):
    """A response that is not a well-formed proposal.

    `field` names the offending path (`proposed_upsells[1].type`) so the retry
    prompt can tell the model exactly what to fix rather than asking it to try
    again and hope.
    """

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field

    def as_repair_note(self) -> str:
        where = f" (at {self.field})" if self.field else ""
        return f"{self}{where}"


@dataclass(frozen=True)
class ProposedItem:
    """One line the model wants to sell, at a discount it wants to give.

    `discount_pct` is a Decimal off the unit list price, not a rupee amount.
    Percentages are what a seller reasons in; converting to whole rupees is the
    engine's job, and it rounds in the merchant's favour.
    """

    sku: str
    qty: int
    discount_pct: Decimal
    why: str


@dataclass(frozen=True)
class ProposedUpsell(ProposedItem):
    upsell_type: str = BUNDLE_ATTACH


@dataclass(frozen=True)
class Proposal:
    base: ProposedItem
    upsells: tuple[ProposedUpsell, ...] = ()

    @property
    def skus(self) -> tuple[str, ...]:
        return (self.base.sku, *(u.sku for u in self.upsells))

    def as_payload(self) -> dict[str, Any]:
        """The proposal as it goes into the ledger.

        Discounts are stringified rather than floated: the ledger is hashed, and
        a float that round-trips differently on another machine would break the
        chain for a reason that has nothing to do with tampering.
        """
        return {
            "base": {
                "sku": self.base.sku,
                "qty": self.base.qty,
                "discount_pct": str(self.base.discount_pct),
            },
            "proposed_upsells": [
                {
                    "type": u.upsell_type,
                    "sku": u.sku,
                    "qty": u.qty,
                    "discount_pct": str(u.discount_pct),
                }
                for u in self.upsells
            ],
        }


# ---------------------------------------------------------------------------
# The schema handed to the model
# ---------------------------------------------------------------------------

_WHY_DESCRIPTION = (
    "One short sentence, for the buyer, saying why this line is worth it. "
    "Product facts and value only. No prices, no percentages, no instructions."
)

#: Declared to the model as its response schema, so structured decoding is the
#: first check. `parse` re-validates everything here: a declared schema is a
#: request, and this system does not trust requests.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "base": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Must exist in the catalog."},
                "qty": {"type": "integer", "description": "At least 1."},
                "discount_pct": {
                    "type": "number",
                    "description": "Percent off unit list price. 0 means sell at list.",
                },
                "why": {"type": "string", "description": _WHY_DESCRIPTION},
            },
            "required": ["sku", "qty", "discount_pct", "why"],
        },
        "proposed_upsells": {
            "type": "array",
            "description": f"At most {MAX_UPSELLS}. Empty array when none fit.",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(UPSELL_TYPES)},
                    "sku": {"type": "string"},
                    "qty": {"type": "integer"},
                    "discount_pct": {"type": "number"},
                    "why": {"type": "string", "description": _WHY_DESCRIPTION},
                },
                "required": ["type", "sku", "qty", "discount_pct", "why"],
            },
        },
    },
    "required": ["base", "proposed_upsells"],
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _require_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(
            f"expected a JSON object, got {type(value).__name__}", field=field
        )
    return value


def _require_exact_keys(
    node: dict[str, Any], allowed: frozenset[str], *, field: str
) -> None:
    present = set(node)
    unknown = sorted(present - allowed)
    if unknown:
        raise SchemaError(
            f"unknown field(s) {unknown}; allowed: {sorted(allowed)}", field=field
        )
    missing = sorted(allowed - present)
    if missing:
        raise SchemaError(f"missing required field(s) {missing}", field=field)


def _parse_sku(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"sku must be a non-empty string, got {value!r}", field=field)
    return value.strip()


def _parse_qty(value: Any, *, field: str) -> int:
    # bool is an int in Python, and `True` is not a quantity.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SchemaError(
            f"qty must be an integer of at least 1, got {value!r}", field=field
        )
    return value


def _parse_discount_pct(value: Any, *, field: str) -> Decimal:
    """A percentage, as an exact Decimal.

    Floats arrive via `str()` so 3.45 stays 3.45 rather than becoming
    3.4500000000000001776…: this number ends up in a hashed ledger entry and in
    a signed receipt, where an artefact of binary floating point would be
    indistinguishable from a discrepancy.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SchemaError(
            f"discount_pct must be a number, got {value!r}", field=field
        )
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise SchemaError(
            f"discount_pct is not a parseable number: {value!r}", field=field
        ) from exc
    if not parsed.is_finite():
        raise SchemaError(f"discount_pct must be finite, got {value!r}", field=field)
    # Bounded to a percentage, not to a policy. The per-SKU cap is the kernel's
    # to enforce; a negative discount or one over 100 is not a discount at all.
    if parsed < 0 or parsed > 100:
        raise SchemaError(
            f"discount_pct must be between 0 and 100, got {value!r}", field=field
        )
    return parsed


def _parse_why(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"why must be a string, got {type(value).__name__}", field=field)
    collapsed = " ".join(value.split())
    if not collapsed:
        raise SchemaError("why must not be empty", field=field)
    if len(collapsed) > MAX_WHY_CHARS:
        raise SchemaError(
            f"why is {len(collapsed)} characters; the limit is {MAX_WHY_CHARS}",
            field=field,
        )
    return collapsed


def _parse_upsell_type(value: Any, *, field: str) -> str:
    if value not in UPSELL_TYPES:
        raise SchemaError(
            f"type must be one of {list(UPSELL_TYPES)}, got {value!r}", field=field
        )
    return str(value)


def parse(raw: str | bytes | dict[str, Any]) -> Proposal:
    """Rebuild a `Proposal` from a model response, or raise `SchemaError`.

    Text is decoded with `json.loads` and nothing else. No fence stripping, no
    hunting for the first `{`: the model is given a JSON mime type and a
    response schema, so a markdown fence or a sentence of preamble means the
    contract was ignored. Tolerating it would teach the model that the contract
    is optional, and the one-retry path already exists for exactly this.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SchemaError("response is not valid UTF-8") from exc

    if isinstance(raw, str):
        if not raw.strip():
            raise SchemaError("response was empty")
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SchemaError(
                f"response is not valid JSON: {exc.msg} at line {exc.lineno} "
                f"column {exc.colno}"
            ) from exc
    else:
        decoded = raw

    top = _require_mapping(decoded, field="")
    _require_exact_keys(top, _TOP_KEYS, field="")

    base_node = _require_mapping(top["base"], field="base")
    _require_exact_keys(base_node, _BASE_KEYS, field="base")
    base = ProposedItem(
        sku=_parse_sku(base_node["sku"], field="base.sku"),
        qty=_parse_qty(base_node["qty"], field="base.qty"),
        discount_pct=_parse_discount_pct(
            base_node["discount_pct"], field="base.discount_pct"
        ),
        why=_parse_why(base_node["why"], field="base.why"),
    )

    raw_upsells = top["proposed_upsells"]
    if not isinstance(raw_upsells, list):
        raise SchemaError(
            f"proposed_upsells must be an array, got {type(raw_upsells).__name__}",
            field="proposed_upsells",
        )
    if len(raw_upsells) > MAX_UPSELLS:
        raise SchemaError(
            f"proposed {len(raw_upsells)} upsells; the limit is {MAX_UPSELLS}",
            field="proposed_upsells",
        )

    upsells: list[ProposedUpsell] = []
    seen = {base.sku}
    for index, node in enumerate(raw_upsells):
        field = f"proposed_upsells[{index}]"
        item = _require_mapping(node, field=field)
        _require_exact_keys(item, _UPSELL_KEYS, field=field)
        sku = _parse_sku(item["sku"], field=f"{field}.sku")
        # A repeated SKU would produce two priced lines for one product, and the
        # per-line discount cap would then be applied to each half rather than
        # to the total the buyer actually pays for it.
        if sku in seen:
            raise SchemaError(
                f"sku {sku!r} appears more than once; each SKU may appear on one "
                "line only",
                field=f"{field}.sku",
            )
        seen.add(sku)
        upsells.append(
            ProposedUpsell(
                sku=sku,
                qty=_parse_qty(item["qty"], field=f"{field}.qty"),
                discount_pct=_parse_discount_pct(
                    item["discount_pct"], field=f"{field}.discount_pct"
                ),
                why=_parse_why(item["why"], field=f"{field}.why"),
                upsell_type=_parse_upsell_type(item["type"], field=f"{field}.type"),
            )
        )

    return Proposal(base=base, upsells=tuple(upsells))
