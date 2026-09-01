"""The merchant side — deciding whether to believe a mandate.

A mandate is the buyer-agent's proof that a human authorised it to spend. This
module is where that proof is tested. It holds no private keys, signs nothing,
and trusts nothing about the token beyond what it can check.

The eight checks run in a fixed order, cheapest first, and stop at the first
failure:

    1. shape          is this even a mandate?
    2. issuer         do we know who signed it?          ← rejects strangers
    3. signature      did they actually sign it?         ← rejects forgeries
    4. expiry         is it still valid?
    5. nonce          has it been used before?           ← rejects replay
    6. agent_id       was it issued to this caller?      ← rejects stolen tokens
    7. scope          does it cover what's in the cart?
    8. amount         do its limits cover the total?

The order is not cosmetic. Cryptography is the expensive step, so an unknown
issuer is turned away before a signature is verified — an attacker cannot make
the merchant burn CPU by posting garbage signed by nobody. And the identity
checks come before the commercial ones so that a stolen mandate is rejected as
stolen, not as over-budget.

Every failure writes its own ledger entry naming the exact check that failed. A
mandate that was silently ignored is indistinguishable from one that was never
presented, and an audit trail that cannot tell those apart is not an audit trail.

**Unknown issuer escalates; forgery rejects.** These are different failures. A
token signed by a key the merchant has never seen may be a real wallet the
merchant simply has not onboarded — that is a business question, so it routes to
human review. A token whose signature does not verify against a key we *do*
hold is a forgery, and no human needs to be consulted about it. `MandateVerdict`
exposes this as `escalates_to_human`.

Nonce replay protection lives in the ledger. Accepting a mandate appends a
`mandate.accepted` entry carrying its nonce, and a UNIQUE partial index over
those entries makes the database the authority: two concurrent presentations of
the same nonce cannot both succeed, because the second INSERT fails. The
pre-check below exists to produce a clean verdict in the ordinary case, not to
provide the guarantee.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

try:
    import psycopg2  # type: ignore
except ImportError:
    psycopg2 = None  # type: ignore
from datetime import datetime
from typing import Any, Iterable

from nacl.exceptions import BadSignatureError

from mandate import token as token_format
from mandate.issuers import TrustedIssuerRegistry, registry as default_registry
from mandate.token import MalformedToken
from store import ledger
from store.db import get_connection
from store.timestamps import parse as parse_ts, to_ts, utc_now

# ── Rejection codes ───────────────────────────────────────────────────────────
# One per check. Distinct codes rather than a shared "invalid mandate", because
# "we don't know this wallet" and "this signature is forged" call for different
# responses and must be separable in the audit trail.

MALFORMED_MANDATE = "MALFORMED_MANDATE"
UNKNOWN_ISSUER = "UNKNOWN_ISSUER"
BAD_SIGNATURE = "BAD_SIGNATURE"
EXPIRED = "EXPIRED"
NONCE_REPLAYED = "NONCE_REPLAYED"
AGENT_MISMATCH = "AGENT_MISMATCH"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
AMOUNT_EXCEEDED = "AMOUNT_EXCEEDED"

#: In check order. Exported so tests can assert every code is reachable.
REJECTION_CODES = (
    MALFORMED_MANDATE,
    UNKNOWN_ISSUER,
    BAD_SIGNATURE,
    EXPIRED,
    NONCE_REPLAYED,
    AGENT_MISMATCH,
    SCOPE_MISMATCH,
    AMOUNT_EXCEEDED,
)

#: Which check produced each code.
CHECK_NUMBERS = {code: index + 1 for index, code in enumerate(REJECTION_CODES)}

#: The one failure that means "ask a human", not "refuse". See module docstring.
ESCALATING_CODES = frozenset({UNKNOWN_ISSUER})

ACCEPTED_EVENT = "mandate.accepted"

#: `mandate.rejected.bad_signature` and friends — the event name itself states
#: the check, so the ledger is readable without decoding the payload.
REJECTED_EVENT_PREFIX = "mandate.rejected."


def rejection_event(code: str) -> str:
    return f"{REJECTED_EVENT_PREFIX}{code.lower()}"


@dataclass(frozen=True)
class MandateVerdict:
    """The outcome of verification. Never carries the token or any key material."""

    valid: bool
    code: str | None = None
    check: int | None = None
    detail: str = ""
    issuer: str | None = None
    subject: str | None = None
    agent_id: str | None = None
    nonce: str | None = None
    max_amount_inr: int | None = None
    max_single_txn_inr: int | None = None
    valid_until: str | None = None
    scope: tuple[str, ...] = ()
    ledger_seq: int | None = None

    @property
    def rejected(self) -> bool:
        return not self.valid

    @property
    def escalates_to_human(self) -> bool:
        """True when the failure is a business question rather than an attack."""
        return self.code in ESCALATING_CODES

    @property
    def issuer_trusted(self) -> bool | None:
        """Feeds the gate's three-valued issuer input.

        True when a known issuer signed it, False when the issuer is unknown,
        None when the question was never reached — the token was malformed, so
        there is no issuer to have an opinion about.
        """
        if self.code == MALFORMED_MANDATE:
            return None
        if self.code == UNKNOWN_ISSUER:
            return False
        return True

    def as_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "valid": self.valid,
            "issuer": self.issuer,
            "subject": self.subject,
            "agent_id": self.agent_id,
            "nonce": self.nonce,
        }
        if self.valid:
            body.update(
                {
                    "scope": list(self.scope),
                    "max_amount_inr": self.max_amount_inr,
                    "max_single_txn_inr": self.max_single_txn_inr,
                    "valid_until": self.valid_until,
                }
            )
        else:
            body.update(
                {
                    "code": self.code,
                    "check": self.check,
                    "detail": self.detail,
                    "escalates_to_human": self.escalates_to_human,
                }
            )
        return body


#: The accepted-nonce lookup, built once from constants. The event name is
#: written into the statement as a literal rather than bound as a parameter
#: because that is what lets SQLite match the query against the partial index's
#: own `WHERE event = 'mandate.accepted'` predicate and probe it directly; a
#: bound parameter cannot be proven to satisfy that predicate at plan time.
_NONCE_LOOKUP_SQL = (
    "SELECT seq FROM ledger "
    f"WHERE event = '{ACCEPTED_EVENT}' "
    "  AND json_extract(payload, '$.nonce') = ? "
    "LIMIT 1"
)


def nonce_seen(nonce: str, conn: sqlite3.Connection | None = None) -> bool:
    """Has this nonce already been accepted? One indexed probe of the ledger."""
    conn = conn or get_connection()
    return conn.execute(_NONCE_LOOKUP_SQL, (nonce,)).fetchone() is not None


def _positive_int(value: Any) -> bool:
    """Ints only, and bool is not an int here — `True` is not a spending limit."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_shape(
    header: dict[str, Any], claims: dict[str, Any]
) -> tuple[str, ...]:
    """Check 1. Raises MalformedToken with a specific reason, or returns scope."""
    algorithm = header.get("alg")
    if algorithm != token_format.ALGORITHM:
        raise MalformedToken(
            f"mandate declares alg={algorithm!r}; only "
            f"{token_format.ALGORITHM!r} is accepted"
        )
    if header.get("typ") != token_format.TOKEN_TYPE:
        raise MalformedToken(
            f"mandate declares typ={header.get('typ')!r}; expected "
            f"{token_format.TOKEN_TYPE!r}"
        )

    missing = [name for name in token_format.REQUIRED_CLAIMS if name not in claims]
    if missing:
        raise MalformedToken(f"mandate is missing required claim(s): {missing}")

    for name in ("sub", "agent_id", "iss", "nonce", "valid_until"):
        value = claims.get(name)
        if not isinstance(value, str) or not value.strip():
            raise MalformedToken(f"claim {name!r} must be a non-empty string")

    for name in ("max_amount_inr", "max_single_txn_inr"):
        if not _positive_int(claims.get(name)):
            raise MalformedToken(
                f"claim {name!r} must be a positive integer number of whole "
                f"rupees; got {claims.get(name)!r}"
            )

    kid = header.get("kid")
    if kid is not None and kid != claims["iss"]:
        raise MalformedToken(
            "mandate header names a different issuer than its signed claims "
            f"(kid={kid!r}, iss={claims['iss']!r})"
        )

    try:
        parse_ts(claims["valid_until"])
    except (ValueError, TypeError) as exc:
        raise MalformedToken(
            f"claim 'valid_until' is not a parseable timestamp: "
            f"{claims['valid_until']!r}"
        ) from exc

    return token_format.normalize_scope(claims["scope"])


def verify(
    mandate_token: str,
    *,
    agent_id: str,
    cart_total_inr: int,
    categories: Iterable[str] = (),
    now: datetime | None = None,
    issuers: TrustedIssuerRegistry | None = None,
    record: bool = True,
    conn: sqlite3.Connection | None = None,
) -> MandateVerdict:
    """Run the eight checks. Returns a verdict; never raises on a bad mandate.

    A malformed or forged mandate is an expected input, not an exceptional one —
    the whole point of this layer is that untrusted callers present untrusted
    tokens. Raising would push error handling onto every call site and invite
    someone to wrap it in a bare `except`.

    `record=False` skips the ledger writes. Use it only for pure verification
    tests; the production path always records, because a rejection nobody can
    see later is a rejection that did not happen.
    """
    issuers = issuers if issuers is not None else default_registry
    moment = now or utc_now()
    categories = tuple(categories)
    cart_total_inr = int(cart_total_inr)

    # ── 1. shape ─────────────────────────────────────────────────────────────
    try:
        header, claims, signature, signed_bytes = token_format.split(mandate_token)
        scope = _validate_shape(header, claims)
    except MalformedToken as exc:
        return _reject(
            MALFORMED_MANDATE,
            str(exc),
            presented_agent_id=agent_id,
            record=record,
            conn=conn,
        )

    issuer = claims["iss"]
    context = {
        "issuer": issuer,
        "subject": claims["sub"],
        "agent_id": claims["agent_id"],
        "nonce": claims["nonce"],
        "presented_agent_id": agent_id,
        "cart_total_inr": cart_total_inr,
    }

    # ── 2. issuer ────────────────────────────────────────────────────────────
    verify_key = issuers.get(issuer)
    if verify_key is None:
        return _reject(
            UNKNOWN_ISSUER,
            f"issuer {issuer!r} is not in the trusted-issuer registry; "
            f"known issuers: {list(issuers.issuer_ids())}",
            record=record,
            conn=conn,
            **context,
        )

    # ── 3. signature ─────────────────────────────────────────────────────────
    try:
        verify_key.verify(signed_bytes, signature)
    except BadSignatureError:
        return _reject(
            BAD_SIGNATURE,
            f"signature does not verify against the registered key for issuer "
            f"{issuer!r}; the mandate was altered or signed by another key",
            record=record,
            conn=conn,
            **context,
        )

    # ── 4. expiry ────────────────────────────────────────────────────────────
    expires_at = parse_ts(claims["valid_until"])
    if expires_at <= moment:
        return _reject(
            EXPIRED,
            f"mandate expired at {claims['valid_until']}; now {to_ts(moment)}",
            record=record,
            conn=conn,
            **context,
        )

    # ── 5. nonce ─────────────────────────────────────────────────────────────
    if nonce_seen(claims["nonce"], conn=conn):
        return _reject(
            NONCE_REPLAYED,
            f"nonce {claims['nonce']} has already been accepted; a mandate is "
            "single-use",
            record=record,
            conn=conn,
            **context,
        )

    # ── 6. agent identity ────────────────────────────────────────────────────
    if claims["agent_id"] != agent_id:
        return _reject(
            AGENT_MISMATCH,
            f"mandate was issued to agent {claims['agent_id']!r} but presented "
            f"by {agent_id!r}",
            record=record,
            conn=conn,
            **context,
        )

    # ── 7. scope ─────────────────────────────────────────────────────────────
    covered, uncovered = token_format.scope_covers(scope, categories)
    if not covered:
        return _reject(
            SCOPE_MISMATCH,
            f"mandate scope {list(scope)} does not cover cart category/ies "
            f"{list(uncovered)}",
            record=record,
            conn=conn,
            **context,
        )

    # ── 8. amount limits ─────────────────────────────────────────────────────
    single = claims["max_single_txn_inr"]
    overall = claims["max_amount_inr"]
    if cart_total_inr > single or cart_total_inr > overall:
        exceeded = "max_single_txn_inr" if cart_total_inr > single else "max_amount_inr"
        limit = single if cart_total_inr > single else overall
        return _reject(
            AMOUNT_EXCEEDED,
            f"cart total INR {cart_total_inr} exceeds {exceeded} of INR {limit}",
            record=record,
            conn=conn,
            **context,
        )

    verdict = MandateVerdict(
        valid=True,
        issuer=issuer,
        subject=claims["sub"],
        agent_id=claims["agent_id"],
        nonce=claims["nonce"],
        max_amount_inr=overall,
        max_single_txn_inr=single,
        valid_until=claims["valid_until"],
        scope=scope,
    )

    if not record:
        return verdict

    # Burning the nonce is the acceptance. It happens only here, after all eight
    # checks pass, so a mandate rejected on scope or amount can be corrected and
    # presented again rather than being destroyed by a failed attempt.
    try:
        entry = ledger.append(
            "policy_kernel",
            ACCEPTED_EVENT,
            {
                **verdict.as_payload(),
                "checks_passed": len(REJECTION_CODES),
                "cart_total_inr": cart_total_inr,
                "categories": list(categories),
            },
            reason=(
                f"mandate from {issuer} accepted for agent {agent_id}: all "
                f"{len(REJECTION_CODES)} checks passed for a cart of INR "
                f"{cart_total_inr}"
            ),
            conn=conn,
        )
    except (sqlite3.IntegrityError, psycopg2.IntegrityError) as e:  # type: ignore
        # The UNIQUE index on accepted nonces fired: another request accepted
        # this same nonce between the check above and this write. The database
        # is the authority on replay, so this is a replay.
        if psycopg2 and isinstance(e, psycopg2.IntegrityError):  # type: ignore
            try:
                conn._pg.rollback()  # type: ignore
            except Exception:
                pass
        return _reject(
            NONCE_REPLAYED,
            f"nonce {claims['nonce']} was accepted concurrently by another "
            "request; a mandate is single-use",
            record=record,
            conn=conn,
            **context,
        )

    return replace(verdict, ledger_seq=entry.seq)


def _reject(
    code: str,
    detail: str,
    *,
    record: bool,
    conn: sqlite3.Connection | None = None,
    issuer: str | None = None,
    subject: str | None = None,
    agent_id: str | None = None,
    nonce: str | None = None,
    presented_agent_id: str | None = None,
    cart_total_inr: int | None = None,
) -> MandateVerdict:
    """Build the verdict and record the refusal under its own event name."""
    verdict = MandateVerdict(
        valid=False,
        code=code,
        check=CHECK_NUMBERS[code],
        detail=detail,
        issuer=issuer,
        subject=subject,
        agent_id=agent_id,
        nonce=nonce,
    )
    if not record:
        return verdict

    entry = ledger.append(
        "policy_kernel",
        rejection_event(code),
        {
            **verdict.as_payload(),
            "presented_agent_id": presented_agent_id,
            "cart_total_inr": cart_total_inr,
        },
        reason=f"mandate rejected at check {verdict.check} ({code}): {detail}",
        conn=conn,
    )
    return replace(verdict, ledger_seq=entry.seq)
