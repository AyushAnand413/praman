"""Canonical JSON — the one serialization used for hashing and signing.

The rule: sorted keys, no whitespace variance, fixed number formatting.
Otherwise the same data hashes differently and verification fails spuriously.

Everything that gets hashed (ledger entries) or signed (policy receipts) goes
through `canonical_json`. There is no second encoder. If a value cannot be
encoded deterministically, this module raises instead of guessing — a spurious
verification failure six weeks from now is far more expensive than a loud error
at write time.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

from settings import CANONICAL_JSON_SEPARATORS


class NonCanonicalValue(TypeError):
    """A value that cannot be serialized deterministically."""


def _normalize(value: Any, path: str = "$") -> Any:
    """Reject or normalize anything whose JSON form is not byte-stable."""
    if value is None or isinstance(value, (str, bool)):
        return value

    if isinstance(value, int):
        # bool is a subclass of int and was handled above.
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise NonCanonicalValue(f"{path}: NaN/Infinity has no canonical form")
        if value == 0.0:
            return 0  # collapses -0.0 and 0.0 onto the same token as 0
        if value.is_integer() and abs(value) < 2**53:
            # 3.0 and 3 must not hash differently.
            return int(value)
        return value

    if isinstance(value, Decimal):
        # Deliberate: Decimal("1.10") and Decimal("1.1") are equal but format
        # differently. Convert at the call site so the choice is explicit.
        raise NonCanonicalValue(
            f"{path}: convert Decimal to int (whole rupees) or str before hashing"
        )

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValue(
                    f"{path}: object keys must be strings, got {type(key).__name__}"
                )
            out[key] = _normalize(item, f"{path}.{key}")
        return out

    if isinstance(value, (list, tuple)):
        return [_normalize(item, f"{path}[{i}]") for i, item in enumerate(value)]

    if isinstance(value, (set, frozenset)):
        # Iteration order is not stable across processes.
        raise NonCanonicalValue(f"{path}: sets have no canonical order; pass a sorted list")

    raise NonCanonicalValue(f"{path}: {type(value).__name__} is not JSON-canonical")


def canonical_json(payload: Any) -> str:
    """Serialize to the one canonical form: sorted keys, no whitespace, ASCII.

    ensure_ascii=True on purpose — the output is pure ASCII, so the bytes that
    get hashed cannot depend on anyone's encoding assumptions.
    """
    return json.dumps(
        _normalize(payload),
        sort_keys=True,
        separators=CANONICAL_JSON_SEPARATORS,
        ensure_ascii=True,
        allow_nan=False,
    )


def entry_hash(prev_hash: str, core: Any) -> str:
    """The ledger link function: SHA256(prev_hash followed by canonical_json(core))."""
    material = prev_hash + canonical_json(core)
    return hashlib.sha256(material.encode("ascii")).hexdigest()
