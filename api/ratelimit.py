"""A per-process rate limit for the browsing endpoint.

Catalog queries need no mandate and cost nothing, which is the right design —
browsing should be free — and also the reason the endpoint needs a limit. An
unauthenticated free endpoint that runs a relevance scan over the catalog is the
cheapest thing in the system to abuse.

This is a fixed-window counter held in process memory. Being explicit about what
that means: it resets when the process restarts, it counts per worker rather than
per deployment, and it keys on a caller-supplied agent id that nothing
authenticates. It stops an agent stuck in a retry loop from hammering the store,
which is the failure this system will actually see. It is not a defence against a
determined attacker, and the fix for that is a limiter at the edge holding shared
state — not a cleverer version of this file.

Deliberately not applied to the offer or checkout endpoints. Those already cost
something to call: an offer consumes one of the session's two, and a checkout
needs an idempotency key and a mandate. Limits that overlap with a bound would
give two components an opinion about the same refusal.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

#: Requests one caller may make per window. Set well above what a working agent
#: needs — a buyer agent makes a handful of catalog queries per purchase — so the
#: limit is only ever reached by something that has gone wrong.
DEFAULT_LIMIT = 60
DEFAULT_WINDOW_SECONDS = 60


@dataclass
class _Window:
    started: float
    count: int


class RateLimitExceeded(RuntimeError):
    """The caller is over its limit. Carries how long to wait."""

    def __init__(self, key: str, *, limit: int, retry_after_seconds: int) -> None:
        self.key = key
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"rate limit of {limit} requests exceeded; "
            f"retry in {retry_after_seconds}s"
        )


class FixedWindowLimiter:
    """Counts requests per key per window.

    Locked because uvicorn serves this app from a thread pool, and two threads
    incrementing the same counter would let a caller past the limit by exactly the
    number of workers. A cheap lock around an integer is not the bottleneck in a
    handler that also reads the catalog.
    """

    def __init__(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}

    def check(self, key: str) -> int:
        """Record one request and return how many remain. Raises when over.

        Raises rather than returning a boolean so a caller cannot forget to look
        at the answer — a rate limiter whose result is ignorable is not one.
        """
        now = self._clock()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.started >= self.window_seconds:
                self._windows[key] = _Window(started=now, count=1)
                return self.limit - 1
            if window.count >= self.limit:
                elapsed = now - window.started
                raise RateLimitExceeded(
                    key,
                    limit=self.limit,
                    retry_after_seconds=max(
                        1, int(self.window_seconds - elapsed) + 1
                    ),
                )
            window.count += 1
            return self.limit - window.count

    def reset(self, key: str | None = None) -> None:
        """Forget one caller's window, or all of them. For tests and restarts."""
        with self._lock:
            if key is None:
                self._windows.clear()
            else:
                self._windows.pop(key, None)


#: The limiter the catalog endpoint uses. One instance, module level, so every
#: request in this process is counted against the same windows.
catalog_limiter = FixedWindowLimiter()
