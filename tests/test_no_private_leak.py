"""No private product data in any response body.

The rule: no private field name appears in any API response body, ever. This
goes further and checks values too, but selectively — see the comment on
VALUE_CHECKED_FIELDS for why checking every private value would produce false
positives rather than security.

The endpoint list is discovered from the app's own route table, so endpoints
added later are covered automatically. That is what makes re-running this test
across new endpoints free.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import pytest

from store.catalog import PRIVATE_FIELDS, PUBLIC_FIELDS, cache, load_catalog_file, to_public

#: Private fields distinctive enough to search for by value.
#:
#: `margin_pct` and `max_discount_pct` are deliberately excluded: they are
#: 1–2 digit integers that legitimately equal public attribute values
#: (AT-PRO-BLK has margin_pct 34 and battery_hrs 34; AT-TIP-FOAM has
#: margin_pct 60, and AT-STUDIO-PRO publishes battery_hrs 60). A value scan on
#: those would fail on innocent data, and a test that cries wolf gets deleted.
#: Their field NAMES are still checked, which is what the rule requires.
#:
#: `tier_up_sku` is excluded for the same reason: its value is a real public
#: SKU that appears in the catalog by design.
VALUE_CHECKED_FIELDS = ("cost_inr", "floor_price_inr", "attach_rate")


def _private_scalars() -> tuple[set[float], set[str]]:
    """The private values to hunt for, split into numbers and strings.

    Compared numerically rather than as substrings of the response text. A
    substring scan reports AT-DAC-01's cost of 799 as a leak because it appears
    inside AT-CHG-PAD's public list price of 1799 — a false positive that says
    nothing about confidentiality. 799 != 1799 settles it.
    """
    raw = load_catalog_file()
    numbers: set[float] = set()
    strings: set[str] = set()

    def record(value: Any) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            numbers.add(float(value))
        elif isinstance(value, str):
            strings.add(value)

    for row in raw["product_private"]:
        for field in VALUE_CHECKED_FIELDS:
            if field in row:
                record(row[field])
        for candidate in row.get("attach_candidates", []):
            if "attach_rate" in candidate:
                record(candidate["attach_rate"])
    return numbers, strings


def _walk_scalars(node: Any) -> Iterator[Any]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_scalars(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_scalars(item)
    else:
        yield node


def _assert_no_private_values(payload: Any, where: str) -> None:
    """Structural scan: no leaf value in `payload` is a private value.

    Three checks, each scoped to keep false positives out:

    1. Numbers by equality. A substring scan of the response text reports
       AT-DAC-01's cost of 799 as a leak because it sits inside AT-CHG-PAD's
       public list price of 1799. 799 != 1799 settles it.
    2. Strings that are numeric, by numeric value — catches `{"cost": "3299"}`.
    3. Prose strings by containment — catches "our cost is 3299", which equality
       alone would miss. Only strings containing whitespace are scanned: a
       64-char hex digest contains almost any three-digit number by chance, and
       ledger hashes are all over these responses.
    """
    numbers, strings = _private_scalars()
    number_text = {_as_text(n) for n in numbers}

    for scalar in _walk_scalars(payload):
        if isinstance(scalar, bool) or scalar is None:
            continue

        if isinstance(scalar, (int, float)):
            assert float(scalar) not in numbers, \
                f"{where} leaked private value {scalar!r}"
            continue

        if not isinstance(scalar, str):
            continue

        assert scalar not in strings, f"{where} leaked private value {scalar!r}"

        as_number = _try_number(scalar)
        if as_number is not None:
            assert as_number not in numbers, \
                f"{where} leaked private value {scalar!r} as a string"

        if any(ch.isspace() for ch in scalar):
            for text in number_text:
                assert text not in scalar, \
                    f"{where} leaked private value {text!r} inside {scalar!r}"


def _try_number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _as_text(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def _walk_keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _walk_keys(item)
    return keys


def _collect_response_bodies(client) -> dict[str, str]:
    """Every GET route the app exposes that needs no arguments, plus /audit/1."""
    bodies: dict[str, str] = {}
    for route in client.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods or "{" in path:
            continue
        response = client.get(path)
        bodies[path] = response.text
    bodies["/audit/1"] = client.get("/audit/1").text
    bodies["/audit/99999"] = client.get("/audit/99999").text
    return bodies


def test_to_public_emits_exactly_the_public_fields(db):
    """The serializer is a whitelist: it cannot pass through an unknown field."""
    row = dict(db.execute("SELECT * FROM products WHERE sku = 'AT-PRO-BLK'").fetchone())
    # Simulate the classic accident: a JOIN that drags private columns along.
    row.update({"cost_inr": 3299, "margin_pct": 34, "floor_price_inr": 4100})

    result = to_public(row)

    assert set(result) == set(PUBLIC_FIELDS)
    for field in PRIVATE_FIELDS:
        assert field not in result


def test_no_private_field_names_in_serialized_catalog(db):
    serialized = json.dumps(cache.all_public())
    for field in PRIVATE_FIELDS:
        assert field not in serialized, f"private field name {field!r} leaked"
    for product in cache.all_public():
        assert set(product) == set(PUBLIC_FIELDS)


def test_no_private_values_in_serialized_catalog(db):
    _assert_no_private_values(cache.all_public(), "the catalog")


def test_the_value_scan_can_actually_fail(db):
    """Guard the guard: a test that cannot fail proves nothing.

    Injects a real cost into a public row and asserts the scan catches it —
    otherwise a future refactor could quietly neuter the check above.
    """
    poisoned = cache.all_public()
    poisoned[0] = {**poisoned[0], "note": "cost_basis 3299"}
    with pytest.raises(AssertionError, match="3299"):
        _assert_no_private_values(poisoned, "poisoned catalog")

    with pytest.raises(AssertionError, match="4100"):
        _assert_no_private_values([{"sku": "AT-PRO-BLK", "x": 4100}], "poisoned row")

    with pytest.raises(AssertionError, match="3299"):
        _assert_no_private_values([{"sku": "AT-PRO-BLK", "x": "3299"}], "stringified")


def test_no_private_data_in_any_response_body(client):
    """The core assertion, across every argument-free GET route the app has."""
    bodies = _collect_response_bodies(client)
    assert bodies, "no routes were exercised — the discovery loop is broken"

    for path, body in bodies.items():
        for field in PRIVATE_FIELDS:
            assert field not in body, f"{path} leaked private field name {field!r}"

        if body.strip().startswith(("{", "[")):
            payload = json.loads(body)
            _assert_no_private_values(payload, path)
            for key in _walk_keys(payload):
                assert key not in PRIVATE_FIELDS, f"{path} has private key {key!r}"


def test_private_view_is_reachable_only_through_an_explicit_call(db):
    """Private economics exist and are usable — just never via a public path."""
    assert cache.private("AT-PRO-BLK")["cost_inr"] == 3299
    assert cache.public("AT-PRO-BLK") is not None
    assert "cost_inr" not in cache.public("AT-PRO-BLK")


def test_non_offerable_skus_are_omitted_not_flagged(db):
    """Self-heal must not make `offerable` inferable from a response."""
    cache.set_offerable("AT-PRO-BLK", False, conn=db)
    try:
        listed = {p["sku"] for p in cache.all_public()}
        assert "AT-PRO-BLK" not in listed
        assert "offerable" not in json.dumps(cache.all_public())
    finally:
        cache.set_offerable("AT-PRO-BLK", True, conn=db)
