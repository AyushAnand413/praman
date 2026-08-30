"""Tenancy — which store is this request acting for.

One deployment hosts many merchants. Every learning row, and eventually every
domain row, carries a `store_id`; this module is the single authority on what
that id is for the code running right now.

The mechanism is a context variable: the API layer resolves the caller's store
once (from the `X-Store-Id` header) into the context, and everything downstream
— pairings, learning hooks, offer assembly — reads it through `current_store()`
without threading a parameter through every signature.

Two properties carry the security weight:

**Fail-closed resolution.** An unknown or missing store id never widens into
"all stores" or "anyone's store". Single-store deployments resolve to the one
configured store; multi-store deployments resolve unknown ids to the FIRST
configured store only when unauthenticated browsing makes naming pointless —
and any route that reads or writes per-store data must resolve explicitly.
Where isolation matters (learning writes), callers pass the resolved id
through; nothing here guesses.

**No wildcard, ever.** There is no id that means "every store". Cross-store
visibility is not a permission to grant; it is a query that cannot be
expressed. That is what makes the isolation tests adversarial rather than
ceremonial.
"""

from __future__ import annotations

import contextvars
import re

import settings

#: The tenant every row lands under until multi-store mode is switched on.
DEFAULT_STORE_ID = "default"

#: Store slugs are lowercase kebab: safe in headers, URLs, and SQL alike.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_current_store: contextvars.ContextVar[str] = contextvars.ContextVar(
     "praman_current_store", default=DEFAULT_STORE_ID
)

# Cache for parsed PRAMAN_STORE_CLUSTER_MAP_JSON to avoid reparsing per call.
_cached_map: dict[str, str] | None = None
_cached_json_str: str | None = None


class UnknownStore(RuntimeError):
    """A store id that is not configured on this deployment."""


def configured_stores() -> tuple[str, ...]:
    """Every store slug this deployment serves, in priority order."""
    return settings.PRAMAN_STORES or (DEFAULT_STORE_ID,)


def resolve(requested: str | None) -> str:
    """Validate a requested store id against the deployment's configuration.

    Raises UnknownStore for anything unconfigured — the caller decides whether
    that means 400, 404, or falling back; tenancy refuses to guess silently.
    """
    if not requested:
        raise UnknownStore("no store id was presented")
    slug = requested.strip().lower()
    if not _SLUG.match(slug):
        raise UnknownStore(f"{requested!r} is not a valid store id")
    if slug not in configured_stores():
        raise UnknownStore(
            f"store {slug!r} is not hosted here; hosted: "
            f"{', '.join(configured_stores())}"
        )
    return slug


def set_current(store_id: str) -> None:
    """Bind this execution context to a store. Resolved first, always."""
    _current_store.set(resolve(store_id))


def current_store() -> str:
    """The store this context acts for. Never raises, never returns a wildcard."""
    return _current_store.get()


def reset_current() -> None:
    """Back to the default tenant. Used by tests between scenarios."""
    _current_store.set(DEFAULT_STORE_ID)


def cluster_for_store(store_id: str | None = None) -> str:
    """The learning cluster this store pools anonymous priors with."""
    import json

    global _cached_map, _cached_json_str
    store_id = store_id or current_store()
    raw = settings.PRAMAN_STORE_CLUSTER_MAP_JSON
    # Use cached parse when the underlying JSON string has not changed;
    # malformed JSON is handled gracefully by falling back to empty mapping
    # and caching that result until the string changes.
    if _cached_map is None or _cached_json_str != raw:
        try:
            parsed = json.loads(raw)
            _cached_map = {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            _cached_map = {}
        _cached_json_str = raw
    return str(_cached_map.get(store_id, store_id))
