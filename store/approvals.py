"""Approval rows — the queue a Tier-2 order waits in.

When the gate sends a transaction to a human, two things must be durable: the
order stops in `HELD`, and a row appears here saying what is being asked and of
whom. This module owns the second.

One rule shapes the whole module: **there is no timeout that approves.** A
pending approval stays pending. It can be approved, rejected, or countered by a
person, and nothing else moves it. An expiry that defaulted to "yes" would make
the human gate decorative — the outcome would be the same as no gate at all,
just slower. If a merchant never answers, the order never charges, which is the
correct failure direction for money.

`decide` is guarded so a second decision on the same row is refused rather than
overwriting the first. A merchant who clicks Approve twice gets one approval; a
merchant whose Reject races someone else's Approve gets a clear error instead of
a coin flip.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from store import ids
from store.db import get_connection, transaction
from store.timestamps import now_ts

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
COUNTERED = "COUNTERED"

STATES = (PENDING, APPROVED, REJECTED, COUNTERED)

#: Every decision is final. Reopening an approval is not a state change, it is a
#: new approval request.
DECIDED_STATES = frozenset({APPROVED, REJECTED, COUNTERED})


class ApprovalNotFound(LookupError):
    pass


class AlreadyDecided(RuntimeError):
    """A second decision on an approval that has already been decided."""


def request(
    *,
    order_id: str,
    offer_id: str,
    amount_inr: int,
    note: str | None = None,
    approval_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Open a pending approval for a held order."""
    conn = conn or get_connection()
    approval_id = approval_id or ids.approval_id()
    with transaction(conn):
        conn.execute(
            """INSERT INTO approvals
                   (approval_id, order_id, offer_id, state, amount_inr,
                    counter_amount_inr, note, requested_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                approval_id,
                order_id,
                offer_id,
                PENDING,
                int(amount_inr),
                note,
                now_ts(),
            ),
        )
    return get(approval_id, conn=conn)  # type: ignore[return-value]


def get(
    approval_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    return dict(row) if row else None


def require(
    approval_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    approval = get(approval_id, conn=conn)
    if approval is None:
        raise ApprovalNotFound(f"no approval {approval_id!r}")
    return approval


def for_order(
    order_id: str, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT * FROM approvals WHERE order_id = ? ORDER BY requested_at",
        (order_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def pending_for_order(
    order_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    row = conn.execute(
        """SELECT * FROM approvals
            WHERE order_id = ? AND state = ?
            ORDER BY requested_at LIMIT 1""",
        (order_id, PENDING),
    ).fetchone()
    return dict(row) if row else None


def pending(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """The merchant's queue, oldest first."""
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT * FROM approvals WHERE state = ? ORDER BY requested_at",
        (PENDING,),
    ).fetchall()
    return [dict(row) for row in rows]


def decide(
    approval_id: str,
    *,
    state: str,
    decided_by: str,
    counter_amount_inr: int | None = None,
    note: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Record a human decision. Refuses to decide an already-decided approval."""
    if state not in DECIDED_STATES:
        raise ValueError(
            f"{state!r} is not a decision; expected one of {sorted(DECIDED_STATES)}"
        )
    if state == COUNTERED and counter_amount_inr is None:
        raise ValueError("a counter-offer must name counter_amount_inr")
    if state != COUNTERED and counter_amount_inr is not None:
        raise ValueError(
            "counter_amount_inr only applies to a COUNTERED decision"
        )

    conn = conn or get_connection()
    with transaction(conn):
        row = conn.execute(
            "SELECT state FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ApprovalNotFound(f"no approval {approval_id!r}")
        if row["state"] != PENDING:
            raise AlreadyDecided(
                f"approval {approval_id} is already {row['state']}; a decision "
                "cannot be revised, only superseded by a new request"
            )
        conn.execute(
            """UPDATE approvals
                  SET state = ?, counter_amount_inr = ?,
                      note = COALESCE(?, note), decided_at = ?, decided_by = ?
                WHERE approval_id = ?""",
            (
                state,
                None if counter_amount_inr is None else int(counter_amount_inr),
                note,
                now_ts(),
                decided_by,
                approval_id,
            ),
        )
    return require(approval_id, conn=conn)
