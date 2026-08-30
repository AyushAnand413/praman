"""The `ab_sessions` table — where measured results live.

The A/B harness is measurement, not commerce: its rows record what a buyer
agent did (arm, persona, basket, upsells taken) so the report can be computed
from stored facts instead of from whatever the runner happened to print. The
table is written by the harness runner after each session and read by the
report; nothing in the money path depends on it, which keeps the experiment
from being able to pay for itself.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from store.db import get_connection, transaction
from store.ids import new_id, AB_SESSION
from store.timestamps import now_ts


def record_session(
    *,
    session_id: str,
    arm: str,
    persona: str,
    basket_inr: int = 0,
    upsells_shown: int = 0,
    upsells_taken: int = 0,
    completed: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str:
    """One row per simulated shopping session. Returns the ab_session id."""
    if arm not in ("control", "treatment"):
        raise ValueError(f"arm must be 'control' or 'treatment', got {arm!r}")
    ab_id = new_id(AB_SESSION)
    conn = conn or get_connection()
    with transaction(conn):
        conn.execute(
            """INSERT INTO ab_sessions
                   (ab_session_id, session_id, arm, persona, basket_inr,
                    upsells_shown, upsells_taken, completed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ab_id,
                session_id,
                arm,
                persona,
                int(basket_inr),
                int(upsells_shown),
                int(upsells_taken),
                int(completed),
                now_ts(),
            ),
        )
    return ab_id


def rows(arm: str | None = None, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Every recorded session, oldest first — optionally one arm's only."""
    conn = conn or get_connection()
    if arm is None:
        cursor = conn.execute("SELECT * FROM ab_sessions ORDER BY created_at")
    else:
        cursor = conn.execute(
            "SELECT * FROM ab_sessions WHERE arm = ? ORDER BY created_at", (arm,)
        )
    return [dict(row) for row in cursor.fetchall()]
