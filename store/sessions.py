"""Session rows — the conversation state a bound is counted against.

A session is what makes bound #5 (`max_offers_per_session`) meaningful: without
a durable place to count offers, an agent could ask for a better price
indefinitely by opening a new conversation each time. `record_offer` increments
that counter inside a transaction, so two concurrent offer requests cannot both
read "1 offer made" and both proceed.

This module deals in rows. It writes no ledger entries and makes no policy
decisions — deciding what a count means belongs to the kernel.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from store import ids
from store.db import get_connection, transaction
from store.timestamps import now_ts


class SessionNotFound(LookupError):
    pass


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def create(
    *,
    agent_id: str,
    session_id: str | None = None,
    mandate_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    conn = conn or get_connection()
    session_id = session_id or ids.session_id()
    stamp = now_ts()
    with transaction(conn):
        conn.execute(
            """INSERT INTO sessions
                   (session_id, agent_id, mandate_id, offers_made, created_at,
                    updated_at)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (session_id, agent_id, mandate_id, stamp, stamp),
        )
    return get(session_id, conn=conn)  # type: ignore[return-value]


def get(
    session_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    return _row_to_dict(
        conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    )


def require(
    session_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    row = get(session_id, conn=conn)
    if row is None:
        raise SessionNotFound(f"no session {session_id!r}")
    return row


def ensure(
    session_id: str | None,
    *,
    agent_id: str,
    mandate_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Fetch the session, or create it. Used at the top of an agent request.

    An existing session keeps its own `agent_id`: the caller does not get to
    reassign a session to a different agent, because the offer count attached to
    it was accumulated under the original identity.
    """
    if session_id:
        existing = get(session_id, conn=conn)
        if existing is not None:
            return existing
    return create(
        agent_id=agent_id,
        session_id=session_id,
        mandate_id=mandate_id,
        conn=conn,
    )


def offers_made(session_id: str, conn: sqlite3.Connection | None = None) -> int:
    """How many offers this session has already been given. 0 if unknown."""
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT offers_made FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row["offers_made"]) if row else 0


def record_offer(
    session_id: str, conn: sqlite3.Connection | None = None
) -> int:
    """Increment the offer counter and return the new total.

    The read and the write happen in one transaction so the returned number is
    the one this caller is responsible for, not a value another request may have
    already moved past.
    """
    conn = conn or get_connection()
    with transaction(conn):
        cursor = conn.execute(
            """UPDATE sessions
                  SET offers_made = offers_made + 1, updated_at = ?
                WHERE session_id = ?""",
            (now_ts(), session_id),
        )
        if cursor.rowcount == 0:
            raise SessionNotFound(f"no session {session_id!r}")
        row = conn.execute(
            "SELECT offers_made FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return int(row["offers_made"])


def attach_mandate(
    session_id: str,
    mandate_id: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    conn = conn or get_connection()
    with transaction(conn):
        conn.execute(
            "UPDATE sessions SET mandate_id = ?, updated_at = ? WHERE session_id = ?",
            (mandate_id, now_ts(), session_id),
        )


def order_count_for_agent(
    agent_id: str, conn: sqlite3.Connection | None = None
) -> int:
    """Orders this agent id has ever placed.

    Feeds the gate's first-order trigger. Counted across sessions, because an
    agent's history is a property of the agent, not of one conversation —
    otherwise "first order from this agent" would be trivially resettable.
    """
    conn = conn or get_connection()
    row = conn.execute(
        """SELECT COUNT(*) AS n
             FROM orders o
             JOIN sessions s ON s.session_id = o.session_id
            WHERE s.agent_id = ?""",
        (agent_id,),
    ).fetchone()
    return int(row["n"])


def is_first_order_for_agent(
    agent_id: str, conn: sqlite3.Connection | None = None
) -> bool:
    return order_count_for_agent(agent_id, conn=conn) == 0
