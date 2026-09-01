"""The idempotency layer — the last line of defence against a double charge.

An agent that retries a request must not be charged twice. Networks drop
responses, agents time out and retry, and a webhook can arrive while a retry is
in flight. None of that is exceptional, so the protection cannot be advisory.

The mechanism is a unique key claimed *before* any external call:

    1. INSERT the key. A UNIQUE violation means someone else already claimed it.
    2. Only the caller who won the INSERT talks to Razorpay.
    3. That caller writes the response back under the key.
    4. Every later caller with the same key gets the stored response.

The order matters more than the mechanism. Claiming after the call would leave a
window where a crash between charging and recording means the retry charges
again — and that window is exactly when retries happen.

Two cases are handled deliberately rather than conveniently:

**Same key, different request.** The stored fingerprint is compared against the
new request. A mismatch raises rather than returning the old response, because a
key reused for different content is a client bug, and answering it with someone
else's receipt would hide the bug behind a plausible reply.

**Claimed but unfinished.** If the original caller claimed the key and then died
before recording a response, a retry finds a claim with no answer. It is told the
request is in flight. It is *not* allowed to proceed: the first attempt may have
reached Razorpay, and the only safe assumption about an unknown outcome to a
payment call is that it might have succeeded.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

try:
    import psycopg2  # type: ignore
except ImportError:
    psycopg2 = None  # type: ignore
from hashlib import sha256
from typing import Any

from store.canonical import canonical_json
from store.db import get_connection, transaction
from store.timestamps import now_ts


class IdempotencyError(RuntimeError):
    pass


class FingerprintMismatch(IdempotencyError):
    """The same key was reused for a materially different request."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"idempotency key {key!r} was already used for a different request. "
            "A key identifies one request; reusing it for another would return "
            "the wrong result."
        )


class RequestInFlight(IdempotencyError):
    """The key is claimed but its outcome was never recorded."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"idempotency key {key!r} is claimed but has no recorded outcome. "
            "The original attempt may have reached the payment gateway, so this "
            "request will not retry it. Poll the order, or use a new key for a "
            "genuinely new purchase."
        )


def fingerprint(payload: dict[str, Any]) -> str:
    """A stable digest of the request that a key stands for.

    Canonical JSON, so key order and formatting cannot make two identical
    requests look different.
    """
    return sha256(canonical_json(payload).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Claim:
    """Who owns the work for an idempotency key."""

    key: str
    is_new: bool
    created_at: str
    order_id: str | None = None
    response: dict[str, Any] | None = None

    @property
    def is_replay(self) -> bool:
        """A previous request already completed under this key."""
        return not self.is_new and self.response is not None

    @property
    def in_progress(self) -> bool:
        """Claimed by someone who never recorded an outcome."""
        return not self.is_new and self.response is None


def _row_to_claim(row: sqlite3.Row, *, is_new: bool) -> Claim:
    stored = row["response_json"]
    return Claim(
        key=row["key"],
        is_new=is_new,
        created_at=row["created_at"],
        order_id=row["order_id"],
        response=json.loads(stored) if stored else None,
    )


def claim(
    key: str,
    *,
    request_fingerprint: str,
    conn: sqlite3.Connection | None = None,
) -> Claim:
    """Claim the key, or report who already holds it.

    Returns a Claim with `is_new=True` for exactly one caller per key. Everyone
    else gets `is_new=False` — either with the recorded response (a replay) or
    without one (still in flight).
    """
    if not key or not key.strip():
        raise IdempotencyError("an idempotency key is required and must not be blank")

    conn = conn or get_connection()
    stamp = now_ts()
    try:
        with transaction(conn):
            conn.execute(
                """INSERT INTO idempotency_keys
                       (key, order_id, request_fingerprint, response_json, created_at)
                   VALUES (?, NULL, ?, NULL, ?)""",
                (key, request_fingerprint, stamp),
            )
    except (sqlite3.IntegrityError, psycopg2.IntegrityError) as exc:  # type: ignore
        # Someone else got there first. The unique index is what makes this a
        # reliable answer rather than a guess.
        if psycopg2 and isinstance(exc, psycopg2.IntegrityError):  # type: ignore
            try:
                conn._pg.rollback()  # type: ignore
            except Exception:
                pass
        row = _require_row(key, conn=conn)
        if row["request_fingerprint"] != request_fingerprint:
            raise FingerprintMismatch(key) from None
        return _row_to_claim(row, is_new=False)
    except Exception as e:
        if psycopg2 and isinstance(e, psycopg2.IntegrityError):  # type: ignore
            try:
                conn._pg.rollback()  # type: ignore
            except Exception:
                pass
            row = _require_row(key, conn=conn)
            if row["request_fingerprint"] != request_fingerprint:
                raise FingerprintMismatch(key) from None
            return _row_to_claim(row, is_new=False)
        raise

    return Claim(key=key, is_new=True, created_at=stamp)


def complete(
    key: str,
    *,
    response: dict[str, Any],
    order_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record the outcome so every later retry gets this exact answer.

    Refuses to overwrite an outcome that is already recorded. A key's answer is
    fixed once given; changing it would mean two retries of one request could
    legitimately disagree.
    """
    conn = conn or get_connection()
    with transaction(conn):
        row = conn.execute(
            "SELECT response_json FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise IdempotencyError(
                f"cannot record an outcome for unclaimed key {key!r}; claim it "
                "before doing the work it protects"
            )
        if row["response_json"] is not None:
            raise IdempotencyError(
                f"key {key!r} already has a recorded outcome; an idempotency "
                "key's answer does not change"
            )
        conn.execute(
            """UPDATE idempotency_keys
                  SET response_json = ?, order_id = COALESCE(?, order_id)
                WHERE key = ?""",
            (json.dumps(response), order_id, key),
        )


def attach_order(
    key: str, order_id: str, conn: sqlite3.Connection | None = None
) -> None:
    """Link the key to its order as soon as the order id exists.

    Written before the gateway call, so a crashed attempt still leaves a trail
    pointing at the order that was being paid for.
    """
    conn = conn or get_connection()
    with transaction(conn):
        conn.execute(
            "UPDATE idempotency_keys SET order_id = ? WHERE key = ?",
            (order_id, key),
        )


def release(key: str, conn: sqlite3.Connection | None = None) -> bool:
    """Drop an unfinished claim so the same key may be used again.

    Only correct for a refusal taken before any external call: nothing was
    charged, so there is no outcome to protect, and holding the claim open would
    answer the caller's corrected retry with `request_in_flight` forever. The
    common case is a Tier 1 cart refused for a missing mandate — the caller is
    meant to attach a mandate and retry the identical request.

    Refuses to touch a claim that already carries an outcome: that answer is
    fixed, and deleting it would let one request be charged twice.
    """
    conn = conn or get_connection()
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM idempotency_keys WHERE key = ? AND response_json IS NULL",
            (key,),
        )
        return cursor.rowcount > 0


def _require_row(
    key: str, conn: sqlite3.Connection | None = None
) -> sqlite3.Row:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise IdempotencyError(f"idempotency key {key!r} is not claimed")
    return row


def get(
    key: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["response"] = (
        json.loads(record["response_json"]) if record["response_json"] else None
    )
    return record


def recorded_response(
    key: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    record = get(key, conn=conn)
    return record["response"] if record else None
