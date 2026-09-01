"""The daily discount budget — bound #4's accounting.

One row per UTC day, holding the rupees of discount given away that day. Bound #4
compares a proposed discount against what is left of `DAILY_DISCOUNT_BUDGET_INR`.

Two properties matter more than the arithmetic:

**The increment is atomic.** `accrue` is a single upsert that adds to the stored
value in SQL, never a read-then-write in Python. Two checkouts completing at the
same instant both add their discount; neither overwrites the other's.

**Reserving and spending are separate.** `check_and_accrue` takes the whole
day's remaining budget under one transaction, so a discount that would exceed the
budget is refused *and* nothing is recorded. Callers that merely want to know the
answer use `spent` or `would_exceed`, which write nothing — asking about the
budget must never consume it.

The day key is UTC. A merchant in IST sees the budget roll over at 05:30 local,
which is a real limitation and is stated in the disclosures rather than papered
over with a guessed timezone.
"""

from __future__ import annotations

import sqlite3

from settings import DAILY_DISCOUNT_BUDGET_INR
from store.db import get_connection, transaction
from store.timestamps import now_ts, utc_day


class BudgetError(RuntimeError):
    pass


class BudgetExceeded(BudgetError):
    """The discount asked for is more than the day has left."""

    def __init__(self, day: str, requested: int, remaining: int) -> None:
        self.day = day
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"discount of INR {requested} exceeds the remaining daily budget of "
            f"INR {remaining} for {day}"
        )


def spent(
    day: str | None = None, conn: sqlite3.Connection | None = None
) -> int:
    """Discount already given away on `day` (default: today, UTC)."""
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT discount_spent_inr FROM policy_budgets WHERE day = ?",
        (day or utc_day(),),
    ).fetchone()
    return int(row["discount_spent_inr"]) if row else 0


def remaining(
    day: str | None = None, conn: sqlite3.Connection | None = None
) -> int:
    return max(0, DAILY_DISCOUNT_BUDGET_INR - spent(day, conn=conn))


def would_exceed(
    discount_inr: int,
    day: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Read-only. Asking about the budget does not consume any of it."""
    return spent(day, conn=conn) + int(discount_inr) > DAILY_DISCOUNT_BUDGET_INR


def _accrue_within(
    conn: sqlite3.Connection, day: str, discount_inr: int
) -> int:
    conn.execute(
        """INSERT INTO policy_budgets (day, discount_spent_inr, updated_at)
                VALUES (?, ?, ?)
           ON CONFLICT (day) DO UPDATE
                SET discount_spent_inr = policy_budgets.discount_spent_inr + excluded.discount_spent_inr,
                    updated_at = excluded.updated_at""",
        (day, int(discount_inr), now_ts()),
    )
    row = conn.execute(
        "SELECT discount_spent_inr FROM policy_budgets WHERE day = ?", (day,)
    ).fetchone()
    return int(row["discount_spent_inr"])


def accrue(
    discount_inr: int,
    *,
    day: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Add to the day's total and return the new total.

    Unconditional. Use `check_and_accrue` where the budget is a limit rather
    than a record.
    """
    discount_inr = int(discount_inr)
    if discount_inr < 0:
        raise ValueError(
            "the budget records discount given away; a negative accrual would be "
            "a refund, which is recorded as its own event"
        )
    conn = conn or get_connection()
    with transaction(conn):
        return _accrue_within(conn, day or utc_day(), discount_inr)


def check_and_accrue(
    discount_inr: int,
    *,
    day: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Reserve budget for a discount, or raise BudgetExceeded.

    The check and the increment are one transaction: two concurrent checkouts
    cannot both be told there is room for the last of the budget.
    """
    discount_inr = int(discount_inr)
    if discount_inr < 0:
        raise ValueError("cannot reserve a negative discount")
    conn = conn or get_connection()
    key = day or utc_day()
    with transaction(conn):
        row = conn.execute(
            "SELECT discount_spent_inr FROM policy_budgets WHERE day = ?", (key,)
        ).fetchone()
        already = int(row["discount_spent_inr"]) if row else 0
        if already + discount_inr > DAILY_DISCOUNT_BUDGET_INR:
            raise BudgetExceeded(
                key, discount_inr, max(0, DAILY_DISCOUNT_BUDGET_INR - already)
            )
        return _accrue_within(conn, key, discount_inr)


def release(
    discount_inr: int,
    *,
    day: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Give budget back after a reservation whose payment never completed.

    The compensating action for `check_and_accrue`. Budget is reserved before the
    gateway call so concurrent checkouts cannot collectively overspend the day;
    when that call then fails, the reservation must be undone or a failed payment
    would permanently consume budget it never spent.

    Clamped at zero. Releasing more than was reserved is a bug, and the floor
    means the bug shows up as an unchanged total rather than as a negative
    balance that makes the day look like it has budget it does not.
    """
    discount_inr = int(discount_inr)
    if discount_inr < 0:
        raise ValueError("cannot release a negative amount")
    conn = conn or get_connection()
    key = day or utc_day()
    with transaction(conn):
        conn.execute(
            """UPDATE policy_budgets
                  SET discount_spent_inr = CASE WHEN discount_spent_inr - ? < 0 THEN 0 ELSE discount_spent_inr - ? END,
                      updated_at = ?
                WHERE day = ?""",
            (discount_inr, discount_inr, now_ts(), key),
        )
        row = conn.execute(
            "SELECT discount_spent_inr FROM policy_budgets WHERE day = ?", (key,)
        ).fetchone()
    return int(row["discount_spent_inr"]) if row else 0


def snapshot(
    day: str | None = None, conn: sqlite3.Connection | None = None
) -> dict[str, int | str]:
    """The day's budget position, for disclosure and dashboard use."""
    key = day or utc_day()
    used = spent(key, conn=conn)
    return {
        "day": key,
        "budget_inr": DAILY_DISCOUNT_BUDGET_INR,
        "spent_inr": used,
        "remaining_inr": max(0, DAILY_DISCOUNT_BUDGET_INR - used),
    }
