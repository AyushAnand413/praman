"""The one timestamp format, and the only parser for it.

Every timestamp written to SQLite or into a hashed payload uses this format:
UTC, microsecond precision, `Z` suffix. Two reasons it lives in one place:

* The ledger hashes timestamps, so a second format would produce two different
  hashes for the same instant.
* String comparison on this format sorts chronologically, which is what lets
  `expires_at < ?` work as a plain indexed SQL comparison instead of requiring
  a date function.

`parse` accepts a `Z` suffix or an explicit offset and always returns an
aware datetime in UTC, so nothing downstream has to guess whether a naive
datetime meant local time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: UTC, microseconds, literal Z. Lexically sortable.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now() -> datetime:
    """Current instant as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_ts(moment: datetime) -> str:
    """Aware datetime -> the canonical string form."""
    if moment.tzinfo is None:
        raise ValueError(
            "refusing to format a naive datetime: an ambiguous timestamp in a "
            "hashed payload is a verification failure waiting to happen"
        )
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def now_ts() -> str:
    """Current instant in canonical string form."""
    return to_ts(utc_now())


def parse(value: str) -> datetime:
    """Canonical string (or any ISO-8601 with a zone) -> aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def plus_seconds(moment: datetime, seconds: int) -> datetime:
    return moment + timedelta(seconds=int(seconds))


def utc_day(moment: datetime | None = None) -> str:
    """YYYY-MM-DD in UTC — the key of a `policy_budgets` row."""
    return (moment or utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%d")
