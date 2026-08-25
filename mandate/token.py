"""The mandate wire format — the vocabulary both sides must agree on.

A mandate is a compact ed25519-signed token, laid out the way a JWS is:

    base64url(header) "." base64url(claims) "." base64url(signature)

The header names the algorithm and the key; the claims say who is buying, on
whose authority, for what, and up to how much; the signature covers the first
two segments exactly as they arrived on the wire.

That last point is the one worth stating plainly. The verifier signs nothing and
re-serializes nothing — it verifies over the received bytes. Re-encoding the
claims before checking the signature would mean a token whose JSON differs from
ours in whitespace or key order fails for the wrong reason, and worse, that two
different byte strings could be treated as the same claim set. The bytes on the
wire are the thing that was signed, so the bytes on the wire are what gets
checked.

This module holds only the format and the claim vocabulary. It performs no
policy: it will happily decode a mandate that is expired, forged, or issued by a
stranger. Deciding what to believe is `verifier.py`'s job.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from store.canonical import canonical_json

#: The only algorithm accepted. Named explicitly so a token asking for a weaker
#: one — or for "none" — is rejected rather than accommodated.
ALGORITHM = "EdDSA"

TOKEN_TYPE = "JWT"

#: Every claim below must be present. A mandate missing any of them is malformed,
#: not "partially valid" — an absent limit is not an unlimited one.
REQUIRED_CLAIMS = (
    "sub",                  # the human on whose behalf the agent acts
    "agent_id",             # the specific agent instance holding this mandate
    "scope",                # what it may buy
    "max_amount_inr",       # ceiling across the mandate's life
    "max_single_txn_inr",   # ceiling for any one transaction
    "valid_until",          # expiry
    "nonce",                # single use
    "iss",                  # issuing wallet
)

#: Scopes are namespaced so a purchase authority can never be mistaken for some
#: other kind of authority. `purchase:*` is accepted but is deliberately
#: conspicuous — a real wallet should enumerate categories.
SCOPE_PREFIX = "purchase:"
SCOPE_WILDCARD = "purchase:*"


class MalformedToken(ValueError):
    """The token is not a well-formed mandate. Carries no key material."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    """Decode an unpadded base64url segment.

    Padding is restored rather than required: unpadded is what goes on the wire,
    and rejecting a token for missing padding would be rejecting it for the
    wrong reason.
    """
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise MalformedToken("mandate segment is not valid base64url") from exc


def encode_json_segment(payload: dict[str, Any]) -> str:
    """Serialize a header or claim set for the wire.

    Canonical JSON, so the same claims always produce the same bytes and the
    same signature.
    """
    return _b64url_encode(canonical_json(payload).encode("ascii"))


def decode_json_segment(segment: str) -> dict[str, Any]:
    raw = _b64url_decode(segment)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedToken("mandate segment is not valid JSON") from exc
    if not isinstance(value, dict):
        raise MalformedToken("mandate header and claims must both be JSON objects")
    return value


def build_header(issuer_id: str) -> dict[str, Any]:
    """`kid` names the key; the authoritative issuer is still the `iss` claim.

    Both are present because that is how the format works, and the verifier
    requires them to agree — a token whose header points at one key while its
    signed claims name another is confused about its own identity.
    """
    return {"alg": ALGORITHM, "typ": TOKEN_TYPE, "kid": issuer_id}


def signing_input(header_segment: str, claims_segment: str) -> bytes:
    """The bytes an ed25519 signature covers."""
    return f"{header_segment}.{claims_segment}".encode("ascii")


def assemble(header_segment: str, claims_segment: str, signature: bytes) -> str:
    return f"{header_segment}.{claims_segment}.{_b64url_encode(signature)}"


def split(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Decode a token into (header, claims, signature, signed bytes).

    The fourth element is the material to verify against — taken from the token
    itself, never rebuilt from the decoded claims.
    """
    if not isinstance(token, str) or not token:
        raise MalformedToken("mandate token is empty")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise MalformedToken(
            f"mandate token must have 3 dot-separated segments; got {len(parts)}"
        )
    header_segment, claims_segment, signature_segment = parts
    if not signature_segment:
        raise MalformedToken("mandate token carries no signature")
    header = decode_json_segment(header_segment)
    claims = decode_json_segment(claims_segment)
    signature = _b64url_decode(signature_segment)
    return header, claims, signature, signing_input(header_segment, claims_segment)


def scope_for_categories(categories) -> tuple[str, ...]:
    """Turn product categories into scope strings, deduplicated and ordered."""
    return tuple(sorted({f"{SCOPE_PREFIX}{c}" for c in categories}))


def normalize_scope(scope: Any) -> tuple[str, ...]:
    """Accept a single scope string or a list of them; reject anything else."""
    if isinstance(scope, str):
        values = (scope,)
    elif isinstance(scope, (list, tuple)):
        values = tuple(scope)
    else:
        raise MalformedToken("scope must be a string or a list of strings")
    if not values:
        raise MalformedToken("scope must not be empty")
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise MalformedToken("every scope entry must be a non-empty string")
    return tuple(values)


def scope_covers(scope: Any, categories) -> tuple[bool, tuple[str, ...]]:
    """Does the mandate's scope authorise every category in the cart?

    Returns (covered, uncovered_categories). Every category must be named — a
    mandate covering three of four categories authorises three of four, and the
    fourth item is not a rounding error.
    """
    granted = set(normalize_scope(scope))
    if SCOPE_WILDCARD in granted:
        return True, ()
    uncovered = tuple(
        sorted(
            {
                category
                for category in categories
                if f"{SCOPE_PREFIX}{category}" not in granted
            }
        )
    )
    return (not uncovered), uncovered
