"""Order rows and the order state machine.

The state machine is the reason this module exists. Money moves in stages, and
each stage is only reachable from particular predecessors — an order cannot be
captured before it is authorised, and a refunded order cannot quietly become
authorised again. Enforcing that here, in one place, means no caller can invent
a transition by writing a different string into the column.

Three outcomes are distinguished for every transition:

    advanced   the order moved
    already    it was in that state; nothing to do
    stale      the requested state is behind where the order already is

That third case is what makes webhook handling safe. Razorpay does not promise
delivery order, so an `authorized` event can arrive after the `captured` event
for the same payment. Treating that as an error would fail a healthy webhook;
treating it as an update would walk the order backwards. It is neither — it is
old news, and `advance` says so.
"""

from __future__ import annotations

import json
import sqlite3  # kept for type hints only — runtime connection is _PGWrapper (psycopg2)
from typing import Any

from store.db import get_connection, transaction
from store.timestamps import now_ts

PENDING = "PENDING"
HELD = "HELD"
AUTHORIZED = "AUTHORIZED"
CAPTURED = "CAPTURED"
CONFIRMED = "CONFIRMED"
VOIDED = "VOIDED"
REFUNDED = "REFUNDED"
FAILED = "FAILED"

STATES = (PENDING, HELD, AUTHORIZED, CAPTURED, CONFIRMED, VOIDED, REFUNDED, FAILED)

#: Nothing leaves these.
TERMINAL_STATES = frozenset({VOIDED, REFUNDED, FAILED})

#: The legal graph. Absence of an edge is a refusal, not an oversight.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({HELD, AUTHORIZED, VOIDED, FAILED}),
    # A held order resumes into the normal path once a human approves it, or is
    # voided if they reject. It never skips ahead to CAPTURED.
    HELD: frozenset({AUTHORIZED, VOIDED, FAILED}),
    AUTHORIZED: frozenset({CAPTURED, VOIDED, FAILED}),
    CAPTURED: frozenset({CONFIRMED, REFUNDED}),
    CONFIRMED: frozenset({REFUNDED}),
    VOIDED: frozenset(),
    REFUNDED: frozenset(),
    FAILED: frozenset(),
}

#: How far along the money path a state is. Used only to recognise stale
#: out-of-order events; it is not an alternative to ALLOWED_TRANSITIONS.
_PROGRESS_RANK = {
    PENDING: 0,
    HELD: 1,
    AUTHORIZED: 2,
    CAPTURED: 3,
    CONFIRMED: 4,
    REFUNDED: 5,
    VOIDED: 5,
    FAILED: 5,
}

ADVANCED = "advanced"
ALREADY = "already"
STALE = "stale"

#: Columns a transition may also set. Anything else is rejected, so a typo
#: cannot silently write to a column nobody meant to touch.
_UPDATABLE_FIELDS = frozenset(
    {"razorpay_order_id", "razorpay_payment_id", "razorpay_refund_id", "gate_tier"}
)


class OrderNotFound(LookupError):
    pass


class IllegalTransition(RuntimeError):
    """A state change the machine does not permit."""


def create(
    *,
    order_id: str,
    session_id: str,
    offer_id: str,
    option_id: str,
    amount_inr: int,
    gate_tier: int,
    policy_mode: str,
    state: str = PENDING,
    razorpay_order_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"{state!r} is not an order state; expected one of {STATES}")
    conn = conn or get_connection()
    stamp = now_ts()
    with transaction(conn):
        conn.execute(
            """INSERT INTO orders
                   (order_id, session_id, offer_id, option_id, amount_inr,
                    state, gate_tier, policy_mode, razorpay_order_id,
                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                session_id,
                offer_id,
                option_id,
                int(amount_inr),
                state,
                int(gate_tier),
                policy_mode,
                razorpay_order_id,
                stamp,
                stamp,
            ),
        )
    return get(order_id, conn=conn)  # type: ignore[return-value]


def get(
    order_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    return dict(row) if row else None


def require(order_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    order = get(order_id, conn=conn)
    if order is None:
        raise OrderNotFound(f"no order {order_id!r}")
    return order


def by_razorpay_order(
    razorpay_order_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM orders WHERE razorpay_order_id = ?", (razorpay_order_id,)
    ).fetchone()
    return dict(row) if row else None


def by_razorpay_payment(
    razorpay_payment_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM orders WHERE razorpay_payment_id = ?", (razorpay_payment_id,)
    ).fetchone()
    return dict(row) if row else None


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def _apply(
    conn: sqlite3.Connection,
    order_id: str,
    to_state: str,
    fields: dict[str, Any],
) -> None:
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(
            f"cannot set {sorted(unknown)} during a transition; updatable "
            f"fields are {sorted(_UPDATABLE_FIELDS)}"
        )
    assignments = ["state = ?", "updated_at = ?"]
    values: list[Any] = [to_state, now_ts()]
    for name, value in sorted(fields.items()):
        assignments.append(f"{name} = ?")
        values.append(value)
    values.append(order_id)
    conn.execute(
        f"UPDATE orders SET {', '.join(assignments)} WHERE order_id = ?",
        tuple(values),
    )


def transition(
    order_id: str,
    to_state: str,
    *,
    expect: str | tuple[str, ...] | None = None,
    conn: sqlite3.Connection | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Move an order to `to_state`, or raise.

    `expect` optionally pins the state the caller believes the order is in. It
    is a guard against acting on a stale read: if another request moved the
    order in the meantime, this raises instead of overwriting their work.

    The read and the write share one transaction, so the check cannot be
    invalidated between checking and writing.
    """
    if to_state not in STATES:
        raise ValueError(f"{to_state!r} is not an order state")
    conn = conn or get_connection()
    with transaction(conn):
        row = conn.execute(
            "SELECT state FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise OrderNotFound(f"no order {order_id!r}")
        current = row["state"]

        if expect is not None:
            expected = (expect,) if isinstance(expect, str) else tuple(expect)
            if current not in expected:
                raise IllegalTransition(
                    f"order {order_id} is {current}, expected one of "
                    f"{list(expected)}; refusing to act on a stale read"
                )

        if not can_transition(current, to_state):
            raise IllegalTransition(
                f"order {order_id} cannot go {current} -> {to_state}; legal next "
                f"states are {sorted(ALLOWED_TRANSITIONS[current])}"
            )
        _apply(conn, order_id, to_state, fields)
    return require(order_id, conn=conn)


def advance(
    order_id: str,
    to_state: str,
    *,
    conn: sqlite3.Connection | None = None,
    **fields: Any,
) -> tuple[str, dict[str, Any]]:
    """Transition tolerantly, for event-driven callers such as webhooks.

    Returns (outcome, order) where outcome is `advanced`, `already`, or `stale`.
    Repeat deliveries and out-of-order deliveries are both normal here, so
    neither is an error — but neither is allowed to move the order backwards.
    """
    if to_state not in STATES:
        raise ValueError(f"{to_state!r} is not an order state")
    conn = conn or get_connection()
    with transaction(conn):
        row = conn.execute(
            "SELECT state FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise OrderNotFound(f"no order {order_id!r}")
        current = row["state"]

        if current == to_state:
            outcome = ALREADY
        elif can_transition(current, to_state):
            outcome = ADVANCED
            _apply(conn, order_id, to_state, fields)
        elif _PROGRESS_RANK[to_state] <= _PROGRESS_RANK[current]:
            outcome = STALE
        else:
            raise IllegalTransition(
                f"order {order_id} cannot go {current} -> {to_state}; legal next "
                f"states are {sorted(ALLOWED_TRANSITIONS[current])}"
            )
    return outcome, require(order_id, conn=conn)


def attach_payment_ids(
    order_id: str,
    *,
    razorpay_order_id: str | None = None,
    razorpay_payment_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Record Razorpay identifiers without changing state.

    Used between creating a gateway order and the payment resolving, when there
    is an id worth persisting but nothing has happened to the money yet.
    """
    conn = conn or get_connection()
    assignments = ["updated_at = ?"]
    values: list[Any] = [now_ts()]
    if razorpay_order_id is not None:
        assignments.append("razorpay_order_id = ?")
        values.append(razorpay_order_id)
    if razorpay_payment_id is not None:
        assignments.append("razorpay_payment_id = ?")
        values.append(razorpay_payment_id)
    values.append(order_id)
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE orders SET {', '.join(assignments)} WHERE order_id = ?",
            tuple(values),
        )
        if cursor.rowcount == 0:
            raise OrderNotFound(f"no order {order_id!r}")
    return require(order_id, conn=conn)


def in_state(
    state: str, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE state = ? ORDER BY created_at", (state,)
    ).fetchall()
    return [dict(row) for row in rows]


def list_all(conn=None) -> list[dict[str, Any]]:
    """All orders in one query — use for dashboard metrics instead of 5 in_state calls."""
    conn = conn or get_connection()
    rows = conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]


def list_today(day_prefix: str, conn=None) -> list[dict[str, Any]]:
    """Orders created on a specific UTC day — single SQL-filtered scan for dashboard metrics.

    `day_prefix` is 'YYYY-MM-DD'. Uses a LIKE filter on the text created_at column
    which is always stored as ISO-8601, so the prefix match is exact and index-friendly.
    """
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE created_at LIKE ? ORDER BY created_at",
        (f"{day_prefix}%",),
    ).fetchall()
    return [dict(row) for row in rows]



# ── reservations ───────────────────────────────────────────────────────────────
#
# A two-step checkout takes out stock holds and reserves discount budget, then
# returns a gateway order and ends. The capture arrives later as a separate
# request carrying only an order id and a payment id. These three functions are
# the handoff: what was reserved is written on the order, read back by settle,
# and cleared once consumed. Deliberately outside `transition`, because a
# reservation is not a state change — an order's state says where the money is,
# not what is being held on its behalf.


def record_reservation(
    order_id: str,
    *,
    hold_ids: list[str] | tuple[str, ...] = (),
    budget_reserved_inr: int = 0,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Write what this order is holding, before any external call.

    Written before the gateway is contacted so that a process that dies mid-call
    still leaves a recoverable reservation rather than a leak.
    """
    conn = conn or get_connection()
    payload = json.dumps(sorted(hold_ids)) if hold_ids else None
    with transaction(conn):
        cursor = conn.execute(
            """UPDATE orders
                  SET stock_hold_ids = ?, budget_reserved_inr = ?, updated_at = ?
                WHERE order_id = ?""",
            (payload, int(budget_reserved_inr), now_ts(), order_id),
        )
        if cursor.rowcount == 0:
            raise OrderNotFound(f"no order {order_id!r}")
    return require(order_id, conn=conn)


def reservation(
    order_id: str, conn: sqlite3.Connection | None = None
) -> tuple[list[str], int]:
    """(hold_ids, budget_reserved_inr) for one order. Empty when nothing is held."""
    conn = conn or get_connection()
    row = conn.execute(
        """SELECT stock_hold_ids, budget_reserved_inr FROM orders
            WHERE order_id = ?""",
        (order_id,),
    ).fetchone()
    if row is None:
        raise OrderNotFound(f"no order {order_id!r}")
    raw = row["stock_hold_ids"]
    hold_ids: list[str] = []
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = []
        if isinstance(decoded, list):
            hold_ids = [str(value) for value in decoded]
    return hold_ids, int(row["budget_reserved_inr"] or 0)


def clear_reservation(
    order_id: str, conn: sqlite3.Connection | None = None
) -> None:
    """Forget the reservation once it has been committed or released.

    Called after the holds are committed or put back, so that a second settle or
    a later sweep cannot act on a reservation that no longer exists. Clearing is
    what makes both of those operations safe to run twice.
    """
    conn = conn or get_connection()
    with transaction(conn):
        conn.execute(
            """UPDATE orders
                  SET stock_hold_ids = NULL, budget_reserved_inr = 0, updated_at = ?
                WHERE order_id = ?""",
            (now_ts(), order_id),
        )


def with_open_reservation(
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Orders still short of a payment that are holding something.

    The input to the abandonment sweep. PENDING and HELD are the only states an
    order can sit in indefinitely; past AUTHORIZED the money path is running and
    the reservation belongs to whoever is running it.
    """
    conn = conn or get_connection()
    rows = conn.execute(
        f"""SELECT * FROM orders
             WHERE state IN ('{PENDING}', '{HELD}')
               AND (stock_hold_ids IS NOT NULL OR budget_reserved_inr > 0)
             ORDER BY created_at"""
    ).fetchall()
    return [dict(row) for row in rows]


def for_session(
    session_id: str, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]
