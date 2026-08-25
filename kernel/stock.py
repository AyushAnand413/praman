"""Stock holds — reserving inventory so two agents cannot sell the same unit.

Bound #7 asks whether stock is available. Answering it honestly at offer time is
useless if the answer can change before checkout, so availability here is not
just `products.stock_qty`:

    available = stock_qty - (quantity held by live reservations)

A hold is live when its state is ACTIVE *and* it has not passed its expiry. Both
conditions are in the SQL, which means availability is correct whether or not
the expiry sweep has run. The sweep is housekeeping — it keeps the table tidy and
makes the state column readable — but no correctness depends on it having run
recently. Availability that silently depended on a background job would be a
race waiting for the one time the job was late.

Reservations are all-or-nothing. A cart of three items that can only reserve two
of them has not been reserved; `reserve_cart` rolls back rather than handing back
a partial hold that the caller would then have to unwind.

Committing a hold is the moment inventory actually leaves the shelf, so it is
gated on policy mode: shadow mode computes everything and commits nothing. There
are two commit functions, and the difference is whether money has already moved.
`commit` is strict — a lapsed or non-ACTIVE hold raises, because before a capture
the right answer is to reserve again. `commit_settled` is for after a capture,
where refusing to decrement cannot undo the sale and would only lose track of it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Iterable, Mapping

from settings import STOCK_HOLD_TTL_SECONDS
from kernel import mode
from store import ids
from store.db import get_connection, transaction
from store.timestamps import parse, plus_seconds, to_ts, utc_now

ACTIVE = "ACTIVE"
COMMITTED = "COMMITTED"
RELEASED = "RELEASED"
EXPIRED = "EXPIRED"

#: A hold that is still holding something: claimed, and not yet timed out.
_LIVE_HOLD_CLAUSE = "state = 'ACTIVE' AND expires_at > ?"


class StockError(RuntimeError):
    pass


class InsufficientStock(StockError):
    """Not enough uncommitted, unheld stock to satisfy a reservation."""

    def __init__(self, sku: str, requested: int, available: int) -> None:
        self.sku = sku
        self.requested = requested
        self.available = available
        super().__init__(
            f"{sku}: requested {requested}, only {available} available "
            "(stock on hand minus live holds)"
        )


def held_qty(
    sku: str,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    conn = conn or get_connection()
    row = conn.execute(
        f"""SELECT COALESCE(SUM(qty), 0) AS held
              FROM stock_holds
             WHERE sku = ? AND {_LIVE_HOLD_CLAUSE}""",
        (sku, to_ts(now or utc_now())),
    ).fetchone()
    return int(row["held"])


def on_hand(sku: str, conn: sqlite3.Connection | None = None) -> int:
    """`products.stock_qty` — the shelf count, ignoring reservations."""
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT stock_qty FROM products WHERE sku = ?", (sku,)
    ).fetchone()
    if row is None:
        raise StockError(f"no such SKU {sku!r}")
    return int(row["stock_qty"])


def available_qty(
    sku: str,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """What can actually be sold right now. This is what bound #7 is given."""
    conn = conn or get_connection()
    return max(0, on_hand(sku, conn=conn) - held_qty(sku, now=now, conn=conn))


def available_for(
    skus: Iterable[str],
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """Availability for several SKUs, as the mapping the bound checks expect."""
    conn = conn or get_connection()
    moment = now or utc_now()
    return {sku: available_qty(sku, now=moment, conn=conn) for sku in set(skus)}


def _insert_hold(
    conn: sqlite3.Connection,
    *,
    sku: str,
    qty: int,
    session_id: str,
    moment: datetime,
    ttl_seconds: int,
) -> str:
    """Check availability and write one hold. Caller owns the transaction."""
    qty = int(qty)
    if qty < 1:
        raise ValueError(f"a hold must be for at least 1 unit; got {qty}")

    available = on_hand(sku, conn=conn) - held_qty(sku, now=moment, conn=conn)
    if qty > available:
        raise InsufficientStock(sku, qty, max(0, available))

    hold_id = ids.hold_id()
    conn.execute(
        """INSERT INTO stock_holds
               (hold_id, sku, qty, session_id, state, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            hold_id,
            sku,
            qty,
            session_id,
            ACTIVE,
            to_ts(plus_seconds(moment, ttl_seconds)),
            to_ts(moment),
        ),
    )
    return hold_id


def reserve(
    sku: str,
    qty: int,
    *,
    session_id: str,
    ttl_seconds: int = STOCK_HOLD_TTL_SECONDS,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Reserve one SKU. Returns the hold id.

    The availability check and the insert share one IMMEDIATE transaction, so two
    requests cannot both see the last unit as free.
    """
    conn = conn or get_connection()
    moment = now or utc_now()
    with transaction(conn):
        return _insert_hold(
            conn,
            sku=sku,
            qty=qty,
            session_id=session_id,
            moment=moment,
            ttl_seconds=ttl_seconds,
        )


def reserve_cart(
    quantities: Mapping[str, int],
    *,
    session_id: str,
    ttl_seconds: int = STOCK_HOLD_TTL_SECONDS,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, str]:
    """Reserve every line in a cart, or nothing. Returns {sku: hold_id}.

    A raised InsufficientStock rolls the whole transaction back, so a failed
    reservation leaves no holds behind for a sweep to clean up later.
    """
    conn = conn or get_connection()
    moment = now or utc_now()
    holds: dict[str, str] = {}
    with transaction(conn):
        for sku in sorted(quantities):
            holds[sku] = _insert_hold(
                conn,
                sku=sku,
                qty=quantities[sku],
                session_id=session_id,
                moment=moment,
                ttl_seconds=ttl_seconds,
            )
    return holds


def get(
    hold_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM stock_holds WHERE hold_id = ?", (hold_id,)
    ).fetchone()
    return dict(row) if row else None


def is_live(hold: dict[str, Any], now: datetime | None = None) -> bool:
    return hold["state"] == ACTIVE and parse(hold["expires_at"]) > (now or utc_now())


def commit(
    hold_ids: Iterable[str],
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Convert holds into sold units: decrement stock and mark COMMITTED.

    Refuses in shadow mode. The checkout path already skips this call when money
    is not moving; this is the backstop that makes skipping it unnecessary to get
    right, because getting it wrong raises instead of quietly selling stock.
    """
    mode.assert_may_move_money("committing reserved stock")

    conn = conn or get_connection()
    moment = to_ts(now or utc_now())
    committed = 0
    with transaction(conn):
        for hold_id in hold_ids:
            row = conn.execute(
                "SELECT sku, qty, state, expires_at FROM stock_holds WHERE hold_id = ?",
                (hold_id,),
            ).fetchone()
            if row is None:
                raise StockError(f"no hold {hold_id!r}")
            if row["state"] != ACTIVE:
                raise StockError(
                    f"hold {hold_id} is {row['state']}, not ACTIVE; it cannot be "
                    "committed"
                )
            if row["expires_at"] <= moment:
                raise StockError(
                    f"hold {hold_id} expired at {row['expires_at']}; reserve again "
                    "rather than committing a lapsed hold"
                )

            cursor = conn.execute(
                """UPDATE products
                      SET stock_qty = stock_qty - ?
                    WHERE sku = ? AND stock_qty >= ?""",
                (row["qty"], row["sku"], row["qty"]),
            )
            if cursor.rowcount == 0:
                raise StockError(
                    f"cannot commit {row['qty']} of {row['sku']}: on-hand stock is "
                    "lower than the hold, which means stock was reduced elsewhere"
                )
            conn.execute(
                "UPDATE stock_holds SET state = ? WHERE hold_id = ?",
                (COMMITTED, hold_id),
            )
            committed += 1
    return committed


def commit_settled(
    hold_ids: Iterable[str],
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Commit holds for a payment that has already been captured.

    `commit` is strict on purpose: a lapsed hold means the units were handed back
    to availability, and committing it anyway would sell stock the system had
    stopped promising. That is the right answer everywhere except here. By the time
    this is called the money has moved, so refusing to decrement does not undo the
    sale — it just loses track of it, and lost track of a sold unit is an oversell
    on the next order rather than an error on this one.

    The hold TTL is 120 seconds and a person completing a card form in a browser
    takes longer than that routinely, so a lapsed hold on the two-step path is the
    normal case, not an edge one.

    So: commit what is still live, and for anything lapsed, decrement the shelf
    directly. Every deviation is named in the returned report so the caller can put
    it on the ledger — including the one case this cannot fix, where the shelf no
    longer has the units because something else sold them in the gap. That is a
    genuine oversell and it is reported rather than swallowed.

    Returns `{committed, recovered, oversold, missing}` where the last three hold
    per-hold detail dicts. Never raises for a lapsed hold; still refuses in shadow
    mode, where no payment can have been captured in the first place.
    """
    mode.assert_may_move_money("committing reserved stock")

    conn = conn or get_connection()
    moment = to_ts(now or utc_now())
    committed: list[str] = []
    recovered: list[dict[str, Any]] = []
    oversold: list[dict[str, Any]] = []
    missing: list[str] = []

    with transaction(conn):
        for hold_id in hold_ids:
            row = conn.execute(
                "SELECT sku, qty, state, expires_at FROM stock_holds WHERE hold_id = ?",
                (hold_id,),
            ).fetchone()
            if row is None:
                missing.append(hold_id)
                continue
            if row["state"] == COMMITTED:
                # Already done. Makes a repeated settle idempotent at the stock
                # layer instead of double-decrementing.
                committed.append(hold_id)
                continue

            lapsed = row["state"] != ACTIVE or row["expires_at"] <= moment
            cursor = conn.execute(
                """UPDATE products
                      SET stock_qty = stock_qty - ?
                    WHERE sku = ? AND stock_qty >= ?""",
                (row["qty"], row["sku"], row["qty"]),
            )
            if cursor.rowcount == 0:
                oversold.append(
                    {
                        "hold_id": hold_id,
                        "sku": row["sku"],
                        "qty": int(row["qty"]),
                        "state": row["state"],
                        "detail": (
                            "on-hand stock is lower than the sold quantity; the units "
                            "were sold elsewhere while this payment was in flight"
                        ),
                    }
                )
                continue

            conn.execute(
                "UPDATE stock_holds SET state = ? WHERE hold_id = ?",
                (COMMITTED, hold_id),
            )
            committed.append(hold_id)
            if lapsed:
                recovered.append(
                    {
                        "hold_id": hold_id,
                        "sku": row["sku"],
                        "qty": int(row["qty"]),
                        "state": row["state"],
                        "expires_at": row["expires_at"],
                    }
                )

    return {
        "committed": committed,
        "recovered": recovered,
        "oversold": oversold,
        "missing": missing,
    }


def release(
    hold_ids: Iterable[str], conn: sqlite3.Connection | None = None
) -> int:
    """Give reserved units back. Safe to call twice; only ACTIVE holds change."""
    conn = conn or get_connection()
    released = 0
    with transaction(conn):
        for hold_id in hold_ids:
            cursor = conn.execute(
                "UPDATE stock_holds SET state = ? WHERE hold_id = ? AND state = ?",
                (RELEASED, hold_id, ACTIVE),
            )
            released += cursor.rowcount
    return released


def release_session(
    session_id: str, conn: sqlite3.Connection | None = None
) -> int:
    """Release everything a session is still holding."""
    conn = conn or get_connection()
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE stock_holds SET state = ? WHERE session_id = ? AND state = ?",
            (RELEASED, session_id, ACTIVE),
        )
    return cursor.rowcount


def expire_stale(
    *, now: datetime | None = None, conn: sqlite3.Connection | None = None
) -> int:
    """Mark lapsed holds EXPIRED. Housekeeping: availability is already correct."""
    conn = conn or get_connection()
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE stock_holds SET state = ? WHERE state = ? AND expires_at <= ?",
            (EXPIRED, ACTIVE, to_ts(now or utc_now())),
        )
    return cursor.rowcount


def live_holds(
    *, now: datetime | None = None, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    conn = conn or get_connection()
    rows = conn.execute(
        f"SELECT * FROM stock_holds WHERE {_LIVE_HOLD_CLAUSE} ORDER BY created_at",
        (to_ts(now or utc_now()),),
    ).fetchall()
    return [dict(row) for row in rows]
