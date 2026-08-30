"""Pairings â€” the store's memory of what actually sells together.

One question answered here: *when people buy X, what else do they buy, how
often?* Every completed order is a basket; every basket updates counters; the
counters are the evidence the offer engine proposes from and bound 10 judges
against. Nobody writes a rule for any of it.

The mechanics, kept deliberately simple:

**Counting.** For a basket with base item B and companions C1..Cn: the
denominator for B (baskets containing B at all) rises by 1, and each Bâ†’Ci
co-occurrence counter rises once. Strength is `together / denominator`, so it
is always the recent ratio of real baskets â€” frequently-bought-together
arithmetic, stated honestly rather than dressed up as magic. A base item
bought alone counts too: it raises the denominator and honestly dilutes every
companion claim.

The denominator lives in `pairing_denominators`, one row per (store, base).
Pair rows in `pairings` are purely about pairs, which keeps their foreign keys
honest â€” no empty-SKU sentinels.

**Decay.** Counts age with an exponential half-life (`PAIRING_HALF_LIFE_DAYS`),
applied lazily on write: touching a row first shrinks its counts by however
long they sat untouched, then adds the new observation. Reads never decay, so
the table costs nothing until commerce touches it, and yesterday's habit fades
instead of haunting next season's catalog.

**Two sources.** `observed` rows come from completed orders and carry real
money evidence. `seeded` rows are cold-start priors with no samples behind
them. On read, observed evidence always wins; a seeded row only surfaces where
nothing observed exists yet, and it never gains samples â€” if reality never
confirms it, it stays second-class forever.

**Isolation.** `store_id` is on every row from day one. Multi-store later is a
query discipline, not a migration â€” one merchant's customer behaviour must
never become another merchant's suggestion feed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import settings
from store import tenancy
from store.db import get_connection, transaction
from store.timestamps import parse, to_ts, utc_now

#: The fallback tenant key. Resolution goes through `store.tenancy`, so a
#: deployment hosting several stores gets per-store learning automatically and
#: every public function below still reads like single-store code.
DEFAULT_STORE_ID = tenancy.DEFAULT_STORE_ID


def _resolve_store(store_id: str | None) -> str:
    """Explicit id wins; otherwise the request's resolved tenant."""
    return store_id or tenancy.current_store()

OBSERVED = "observed"
SEEDED = "seeded"


class PairingError(RuntimeError):
    pass


def _decay_factor(*, last_updated: datetime, now: datetime) -> float:
    """Exponential shrinkage of stale counts: 0.5^(days / half-life)."""
    days = max(0.0, (now - last_updated).total_seconds()) / 86_400.0
    return 0.5 ** (days / settings.PAIRING_HALF_LIFE_DAYS)


# Decay is applied lazily on write: touching a row first shrinks its counts
# by elapsed time, then adds the new observation. Reads also apply decay
# arithmetically via _decayed_denominator / _decayed_together without
# persisting it, so idle SKUs naturally lose strength without a background job.


def _decayed_denominator(
    conn: sqlite3.Connection,
    *,
    store_id: str,
    base_sku: str,
    now: datetime | None = None,
) -> float:
    """The base count as of `now`, with the half-life applied arithmetically.

    Reads decay without writing: evidence ages whether or not commerce touches
    it, and a read can never cost a lock or surprise a writer.
    """
    now = now or utc_now()
    row = conn.execute(
        """SELECT base_count, updated_at FROM pairing_denominators
            WHERE store_id=? AND base_sku=?""",
        (store_id, base_sku),
    ).fetchone()
    if row is None:
        return 0.0
    factor = _decay_factor(last_updated=parse(row["updated_at"]), now=now)
    return float(row["base_count"]) * factor


def _decayed_together(
    conn: sqlite3.Connection,
    *,
    store_id: str,
    base_sku: str,
    paired_sku: str,
    now: datetime | None = None,
) -> float | None:
    """The pair's co-occurrence count, decayed to `now`. None when absent."""
    now = now or utc_now()
    row = conn.execute(
        """SELECT together_count, updated_at FROM pairings
            WHERE store_id=? AND base_sku=? AND paired_sku=? AND source=?""",
        (store_id, base_sku, paired_sku, OBSERVED),
    ).fetchone()
    if row is None:
        return None
    factor = _decay_factor(last_updated=parse(row["updated_at"]), now=now)
    return float(row["together_count"]) * factor


def record_order_basket(
    base_sku: str,
    other_skus: list[str] | tuple[str, ...],
    *,
    store_id: str | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Fold one completed order into the tables. Returns rows written.

    The base line anchors the observation â€” recommendations are always "for a
    given base item, what follows" â€” and each distinct companion gains one
    co-occurrence. A basket of B + C + C still counts C once: quantity is not
    evidence strength.
    """
    base = str(base_sku)
    companions = sorted({str(s) for s in other_skus if str(s) != base})
    moment = now or utc_now()

    conn = conn or get_connection()
    store_id = _resolve_store(store_id)
    touched = 0
    with transaction(conn):
        denominator = _decayed_denominator(
            conn, store_id=store_id, base_sku=base, now=moment
        ) + 1.0
        conn.execute(
            """INSERT INTO pairing_denominators
                   (store_id, base_sku, base_count, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (store_id, base_sku) DO UPDATE SET
                   base_count = excluded.base_count,
                   updated_at = excluded.updated_at""",
            (store_id, base, denominator, to_ts(moment)),
        )
        touched += 1

        for companion in companions:
            together = (
                _decayed_together(
                    conn,
                    store_id=store_id,
                    base_sku=base,
                    paired_sku=companion,
                    now=moment,
                )
                or 0.0
            ) + 1.0
            conn.execute(
                """INSERT INTO pairings
                       (store_id, base_sku, paired_sku, source,
                        together_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (store_id, base_sku, paired_sku, source) DO UPDATE SET
                       together_count = excluded.together_count,
                       updated_at = excluded.updated_at""",
                (store_id, base, companion, OBSERVED, together, to_ts(moment)),
            )
            touched += 1

    return touched


def pairs_for(
    base_sku: str,
    *,
    store_id: str | None = None,
    limit: int = 10,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """The companions proposed for one base item, strongest first.

    Observed rows carry their sample count (the decayed denominator); seeded
    rows appear only where no observed evidence exists for the same companion,
    and report zero samples. Callers gate on `samples >=
    RELATEDNESS_MIN_SAMPLES` before trusting a strength â€” three baskets is an
    anecdote, not evidence.
    """
    conn = conn or get_connection()
    store_id = _resolve_store(store_id)
    moment = now or utc_now()
    denominator = _decayed_denominator(
        conn, store_id=store_id, base_sku=str(base_sku), now=moment
    )
    samples = int(round(denominator))

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in conn.execute(
        """SELECT paired_sku, together_count, updated_at FROM pairings
            WHERE store_id=? AND base_sku=? AND source=? AND together_count > 0""",
        (store_id, str(base_sku), OBSERVED),
    ):
        together = float(row["together_count"]) * _decay_factor(
            last_updated=parse(row["updated_at"]), now=moment
        )
        strength = min(1.0, together / denominator) if denominator > 0 else 0.0
        results.append(
            {
                "sku": row["paired_sku"],
                "strength": round(strength, 4),
                "samples": samples,
                "source": OBSERVED,
            }
        )
        seen.add(row["paired_sku"])
    results.sort(key=lambda r: (-r["strength"], r["sku"]))

    for row in conn.execute(
        """SELECT paired_sku FROM pairings
            WHERE store_id=? AND base_sku=? AND source=? AND together_count = 0""",
        (store_id, str(base_sku), SEEDED),
    ):
        if row["paired_sku"] not in seen:
            results.append(
                {
                    "sku": row["paired_sku"],
                    "strength": 0.0,
                    "samples": 0,
                    "source": SEEDED,
                }
            )

    return results[: max(0, int(limit))]


def related_skus(
    base_sku: str,
    *,
    store_id: str | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> frozenset[str]:
    """Companions trusted as related to this base item.

    Observed companions need at least RELATEDNESS_MIN_SAMPLES baskets behind
    them before they can veto anything; below that they are anecdotes and stay
    out of the enforcement set (they still surface in `pairs_for`, where being
    wrong costs nothing). Seeded companions are included by name â€” they were
    deliberately declared, and bound 10 failing on them would make cold-start
    seeding pointless.

    The sample threshold is evaluated against the DECAYED denominator: a base
    item whose evidence has aged past the half-lives loses enforcement power
    along with its strength, which is the honest direction to fail.
    """
    conn = conn or get_connection()
    store_id = _resolve_store(store_id)
    moment = now or utc_now()
    threshold = settings.RELATEDNESS_MIN_SAMPLES
    denominator = _decayed_denominator(
        conn, store_id=store_id, base_sku=str(base_sku), now=moment
    )
    trusted: set[str] = set()

    if int(round(denominator)) >= threshold:
        for row in conn.execute(
            """SELECT paired_sku FROM pairings
                WHERE store_id=? AND base_sku=? AND source=? AND together_count > 0""",
            (store_id, str(base_sku), OBSERVED),
        ):
            trusted.add(row["paired_sku"])

    for row in conn.execute(
        """SELECT paired_sku FROM pairings
            WHERE store_id=? AND base_sku=? AND source=?""",
        (store_id, str(base_sku), SEEDED),
    ):
        trusted.add(row["paired_sku"])

    return frozenset(trusted)


def seed_pairing(
    base_sku: str,
    paired_sku: str,
    *,
    store_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Declare a cold-start prior: 'these belong together' before any sale.

    Idempotent. Seeded rows never gain counts from seeding â€” only completed
    orders create observed evidence â€” so an unconfirmed seed can be told apart
    from a proven pairing at read time.
    """
    conn = conn or get_connection()
    store_id = _resolve_store(store_id)
    with transaction(conn):
        conn.execute(
            """INSERT INTO pairings
                   (store_id, base_sku, paired_sku, source,
                    together_count, updated_at)
               VALUES (?, ?, ?, ?, 0, ?)
               ON CONFLICT (store_id, base_sku, paired_sku, source)
               DO NOTHING""",
            (store_id, str(base_sku), str(paired_sku), SEEDED, to_ts(utc_now())),
        )


def snapshot(
    *, store_id: str | None = None, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """A small health view of the learning tables, for dashboards."""
    conn = conn or get_connection()
    store_id = _resolve_store(store_id)
    observed_pairs = conn.execute(
        """SELECT count(*) FROM pairings
            WHERE store_id=? AND source=? AND together_count > 0""",
        (store_id, OBSERVED),
    ).fetchone()[0]
    seeded_pairs = conn.execute(
        """SELECT count(*) FROM pairings
            WHERE store_id=? AND source=? AND together_count = 0""",
        (store_id, SEEDED),
    ).fetchone()[0]
    bases = conn.execute(
        "SELECT count(*) FROM pairing_denominators WHERE store_id=?",
        (store_id,),
    ).fetchone()[0]
    return {
        "store_id": store_id,
        "bases_observed": int(bases),
        "observed_pairs": int(observed_pairs),
        "seeded_pairs": int(seeded_pairs),
        "half_life_days": settings.PAIRING_HALF_LIFE_DAYS,
        "min_samples_trusted": settings.RELATEDNESS_MIN_SAMPLES,
    }





# ---------------------------------------------------------------------------
# Cluster priors - what RELATED stores learned, pooled anonymously
# ---------------------------------------------------------------------------


def record_category_basket(base_category, paired_categories, *, cluster, now=None, conn=None):
    """Fold one completed basket into the cluster's anonymous category ratios.

    Called alongside record_order_basket: the SKU-level row stays private to
    the store; only the fact "in baskets containing a <base_category>, a
    <paired_category> appeared" reaches the pool. No SKUs cross stores, ever.
    """
    base = str(base_category)
    companions = sorted({str(c) for c in paired_categories if str(c) != base})
    moment = now or utc_now()

    conn = conn or get_connection()
    touched = 0
    with transaction(conn):
        row = conn.execute(SELECT_BASE_DENOMINATOR, (cluster, base)).fetchone()
        denominator = (float(row["base_count"]) if row else 0.0) + 1.0
        conn.execute(INSERT_DENOMINATOR, (cluster, base, denominator, to_ts(moment)))
        touched += 1
        for companion in companions:
            conn.execute(
                INSERT_TOGETHER, (cluster, base, companion, denominator, to_ts(moment))
            )
            touched += 1
    return touched


SELECT_BASE_DENOMINATOR = (
    "SELECT base_count FROM cluster_pairings "
    "WHERE cluster=? AND base_category=? AND paired_category=''"
)
INSERT_DENOMINATOR = (
    "INSERT INTO cluster_pairings "
    "(cluster, base_category, paired_category, base_count, together_count, updated_at) "
    "VALUES (?, ?, '', ?, 0, ?) "
    "ON CONFLICT (cluster, base_category, paired_category) DO UPDATE SET "
    "base_count = excluded.base_count, updated_at = excluded.updated_at"
)
INSERT_TOGETHER = (
    "INSERT INTO cluster_pairings "
    "(cluster, base_category, paired_category, base_count, together_count, updated_at) "
    "VALUES (?, ?, ?, ?, 1, ?) "
    "ON CONFLICT (cluster, base_category, paired_category) DO UPDATE SET "
    "together_count = together_count + 1, updated_at = excluded.updated_at"
)


def cluster_pairs_for(base_category, *, cluster, limit=5, conn=None):
    """The pool's category-level priors for one base category, strongest first."""
    conn = conn or get_connection()
    denom_row = conn.execute(
        SELECT_BASE_DENOMINATOR, (str(cluster), str(base_category))
    ).fetchone()
    denominator = float(denom_row["base_count"]) if denom_row else 0.0

    results = []
    for row in conn.execute(
        "SELECT paired_category, together_count FROM cluster_pairings "
        "WHERE cluster=? AND base_category=? AND paired_category != '' "
        "AND together_count > 0 ORDER BY together_count DESC",
        (str(cluster), str(base_category)),
    ):
        strength = (
            min(1.0, float(row["together_count"]) / denominator)
            if denominator > 0
            else 0.0
        )
        results.append(
            {"category": row["paired_category"], "strength": round(strength, 4)}
        )
    return results[: max(0, int(limit))]


def suggest_from_cluster(base_sku, *, store_id=None, now=None, conn=None):
    """Cluster-prior SKUs to SUGGEST when the store's own evidence is young.

    Reads the pool at the base SKU's category and returns declared attach
    candidates sitting in a suggested category - suggestions only ever name
    SKUs the merchant already declared pairable. Suggestions never feed
    bound 10: enforcement stays on the store's own confirmed data until it
    crosses CLUSTER_PRIOR_MIN_OWN_SAMPLES.

    Returns [] whenever the store is old enough not to need them.
    """
    from store import catalog
    from store import tenancy as tenancy_module

    resolved = _resolve_store(store_id)
    moment = now or utc_now()
    threshold = settings.CLUSTER_PRIOR_MIN_OWN_SAMPLES

    denominator = _decayed_denominator(
        conn or get_connection(), store_id=resolved, base_sku=base_sku, now=moment
    )
    if int(round(denominator)) >= threshold:
        return []

    cluster = tenancy_module.cluster_for_store(resolved)
    public = catalog.cache.public(base_sku)
    if public is None:
        return []
    pool = cluster_pairs_for(public.get("category", ""), cluster=cluster, conn=conn)
    if not pool:
        return []

    suggested_categories = {p["category"] for p in pool}
    private = catalog.cache.private(base_sku)
    if not private:
        return []
    declared = []
    for candidate in private.get("attach_candidates") or []:
        sku = candidate.get("sku") if isinstance(candidate, dict) else candidate
        if not sku:
            continue
        candidate_public = catalog.cache.public(str(sku))
        if candidate_public and candidate_public.get("category") in suggested_categories:
            declared.append(
                {
                    "sku": str(sku),
                    "via_cluster": cluster,
                    "category": candidate_public["category"],
                }
            )
    return declared
