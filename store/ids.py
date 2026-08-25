"""Identifier generation — one prefix per kind of thing.

Prefixed, human-legible ids rather than bare UUIDs. When a merchant reads a
ledger trail or a judge reads a demo transcript, `ORD-a3f9c2d1b4e5` says what it
is without a lookup, and an id pasted into the wrong endpoint fails obviously
instead of subtly.

Randomness comes from `secrets`, not `random`: order and offer ids appear in
URLs, so a guessable id would let anyone enumerate other buyers' audit trails.
"""

from __future__ import annotations

import secrets

#: Prefixes, so the set is enumerable and collisions between kinds are visible.
OFFER = "OF"
ORDER = "ORD"
RECEIPT = "PR"
SESSION = "SES"
HOLD = "HOLD"
APPROVAL = "APV"
MANDATE = "MDT"
AB_SESSION = "AB"

#: 12 hex characters = 48 bits. Ample for a demo-scale store, and short enough
#: to read aloud.
_ENTROPY_BYTES = 6


def new_id(prefix: str, *, entropy_bytes: int = _ENTROPY_BYTES) -> str:
    return f"{prefix}-{secrets.token_hex(entropy_bytes)}"


def offer_id() -> str:
    return new_id(OFFER)


def order_id() -> str:
    return new_id(ORDER)


def receipt_id() -> str:
    return new_id(RECEIPT)


def session_id() -> str:
    return new_id(SESSION)


def hold_id() -> str:
    return new_id(HOLD)


def approval_id() -> str:
    return new_id(APPROVAL)


def mandate_nonce() -> str:
    """Mandate nonces are single-use, so entropy matters more than brevity."""
    return secrets.token_hex(16)
