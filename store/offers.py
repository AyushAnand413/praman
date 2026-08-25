"""Offer rows — the server-side record of what was promised, and for how much.

This table is the amount authority. When a checkout request arrives naming an
offer and an option, the price charged is read from here, never from the request
body. An agent can ask to buy option B; it cannot tell the merchant what option
B costs.

Each row stores its options, the signed policy receipt that authorised them, the
gate tier that was assigned, and an expiry. The receipt is stored alongside
rather than recomputed later, because the point of a receipt is that it records
what was decided at the time — a receipt regenerated at checkout would only
prove what the code believes now.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from settings import OFFER_TTL_SECONDS
from store.db import get_connection, transaction
from store.timestamps import now_ts, parse, plus_seconds, to_ts, utc_now


class OfferNotFound(LookupError):
    pass


class OptionNotFound(LookupError):
    pass


def _hydrate(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Row -> dict with the JSON columns parsed."""
    if row is None:
        return None
    offer = dict(row)
    offer["options"] = json.loads(offer["options"])
    offer["policy_receipt"] = json.loads(offer["policy_receipt"])
    return offer


def create(
    *,
    offer_id: str,
    session_id: str,
    base_sku: str,
    options: Iterable[dict[str, Any]],
    total_inr: int,
    gate_tier: int,
    policy_receipt: dict[str, Any],
    policy_mode: str,
    expires_at: str | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Persist an offer. `total_inr` is the headline option's total.

    `expires_at` defaults to the configured offer TTL from now, which is what
    bound #8 is checked against at checkout.
    """
    conn = conn or get_connection()
    moment = now or utc_now()
    option_list = [dict(option) for option in options]
    if not option_list:
        raise ValueError("an offer must contain at least one option")

    expiry = expires_at or to_ts(plus_seconds(moment, OFFER_TTL_SECONDS))
    with transaction(conn):
        conn.execute(
            """INSERT INTO offers
                   (offer_id, session_id, base_sku, options, total_inr,
                    gate_tier, policy_receipt, policy_mode, expires_at,
                    created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                offer_id,
                session_id,
                base_sku,
                json.dumps(option_list),
                int(total_inr),
                int(gate_tier),
                json.dumps(policy_receipt),
                policy_mode,
                expiry,
                to_ts(moment),
            ),
        )
    return get(offer_id, conn=conn)  # type: ignore[return-value]


def get(
    offer_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    conn = conn or get_connection()
    return _hydrate(
        conn.execute("SELECT * FROM offers WHERE offer_id = ?", (offer_id,)).fetchone()
    )


def require(offer_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    offer = get(offer_id, conn=conn)
    if offer is None:
        raise OfferNotFound(f"no offer {offer_id!r}")
    return offer


def option(offer: dict[str, Any], option_id: str) -> dict[str, Any]:
    """The named option from a hydrated offer. Raises if it is not there.

    Raising rather than returning None is deliberate: this function's result
    decides how much to charge, and a caller that forgot to check for None would
    otherwise be one `or {}` away from charging zero.
    """
    for candidate in offer["options"]:
        if candidate.get("option_id") == option_id:
            return candidate
    available = [c.get("option_id") for c in offer["options"]]
    raise OptionNotFound(
        f"offer {offer['offer_id']!r} has no option {option_id!r}; "
        f"available: {available}"
    )


def amount_for(offer: dict[str, Any], option_id: str) -> int:
    """The authoritative price of an option, in whole rupees."""
    return int(option(offer, option_id)["total_inr"])


def is_expired(offer: dict[str, Any], now: datetime | None = None) -> bool:
    return parse(offer["expires_at"]) <= (now or utc_now())


def seconds_remaining(offer: dict[str, Any], now: datetime | None = None) -> int:
    delta = parse(offer["expires_at"]) - (now or utc_now())
    return max(0, int(delta.total_seconds()))


def for_session(
    session_id: str, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT * FROM offers WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    return [_hydrate(row) for row in rows]  # type: ignore[misc]


def expired_before(
    moment: str | None = None, conn: sqlite3.Connection | None = None
) -> list[str]:
    """Offer ids past their expiry. Used by housekeeping, not by policy."""
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT offer_id FROM offers WHERE expires_at <= ?",
        (moment or now_ts(),),
    ).fetchall()
    return [row["offer_id"] for row in rows]
