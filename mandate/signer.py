"""The wallet side — issuing mandates.

This module signs. It belongs conceptually to the buyer, not the merchant: in a
real deployment the code here runs inside a wallet app or a bank's agent
service, and the merchant never sees it. It lives in this repository because the
demo has to play both parts, and because a verifier nobody can produce valid
input for is a verifier nobody can trust.

The separation is kept sharp anyway. `signer.py` is the only module that touches
a private key. `verifier.py` imports nothing from here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from nacl.signing import SigningKey

from mandate import keys, token
from mandate.issuers import DEMO_ISSUER_ID
from store import ids
from store.timestamps import plus_seconds, to_ts, utc_now

#: How long an issued mandate stays valid. Short by design: a mandate is
#: authority to spend, and authority that outlives the shopping session it was
#: granted for is authority waiting to be misused.
DEFAULT_TTL_SECONDS = 900


def build_claims(
    *,
    subject: str,
    agent_id: str,
    categories: Iterable[str] | None = None,
    scope: Any = None,
    max_amount_inr: int,
    max_single_txn_inr: int,
    issuer: str = DEMO_ISSUER_ID,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    nonce: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the claim set.

    Pass `categories` to have scopes derived, or `scope` to state them directly.
    One or the other is required — there is no default scope, because a mandate
    that quietly authorises everything is the failure this whole layer exists to
    prevent.
    """
    if scope is None and categories is None:
        raise ValueError(
            "a mandate needs a scope: pass categories=[...] or scope=[...]"
        )
    resolved_scope = (
        list(token.scope_for_categories(categories))
        if scope is None
        else list(token.normalize_scope(scope))
    )

    moment = now or utc_now()
    return {
        "sub": subject,
        "agent_id": agent_id,
        "scope": resolved_scope,
        "max_amount_inr": int(max_amount_inr),
        "max_single_txn_inr": int(max_single_txn_inr),
        "issued_at": to_ts(moment),
        "valid_until": to_ts(plus_seconds(moment, ttl_seconds)),
        "nonce": nonce or ids.mandate_nonce(),
        "iss": issuer,
    }


def sign(
    claims: dict[str, Any],
    *,
    signing_key: SigningKey | None = None,
    issuer: str | None = None,
) -> str:
    """Sign a claim set into a mandate token.

    The header's `kid` defaults to the `iss` claim so the two agree. Overriding
    `issuer` lets a test build the mismatched token the verifier must reject.
    """
    key = signing_key or keys.wallet_signing_key()
    header = token.build_header(issuer or claims.get("iss", ""))
    header_segment = token.encode_json_segment(header)
    claims_segment = token.encode_json_segment(claims)
    signature = key.sign(
        token.signing_input(header_segment, claims_segment)
    ).signature
    return token.assemble(header_segment, claims_segment, signature)


def issue(
    *,
    subject: str,
    agent_id: str,
    categories: Iterable[str] | None = None,
    scope: Any = None,
    max_amount_inr: int,
    max_single_txn_inr: int,
    issuer: str = DEMO_ISSUER_ID,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    nonce: str | None = None,
    now: datetime | None = None,
    signing_key: SigningKey | None = None,
) -> str:
    """Build and sign in one call — what the buyer-agent harness uses."""
    claims = build_claims(
        subject=subject,
        agent_id=agent_id,
        categories=categories,
        scope=scope,
        max_amount_inr=max_amount_inr,
        max_single_txn_inr=max_single_txn_inr,
        issuer=issuer,
        ttl_seconds=ttl_seconds,
        nonce=nonce,
        now=now,
    )
    return sign(claims, signing_key=signing_key, issuer=issuer)
