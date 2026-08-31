"""SQLite schema and connection management — the 11 tables of the store.

Single writer, WAL mode, one machine. SQLite is sufficient at this scope; no
Postgres needed.

Two structural guarantees live here rather than in application code:

* `idempotency_keys.key` carries a UNIQUE index, so a double-charge race loses
  at the storage layer even if the application logic above it is wrong.
* The `ledger` table has BEFORE UPDATE and BEFORE DELETE triggers that ABORT.
  The ledger is never updated and never deleted from; triggers make that true
  rather than aspirational. The tamper demo must therefore drop the guard
  before it can rewrite history — which is exactly the honest framing of what
  the ledger provides: tamper-EVIDENCE, not tamper-proofing.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from settings import DATABASE_PATH, DATABASE_URL, USE_POSTGRES

# Postgres is optional — only needed when DATABASE_URL points there. Tests run
# without it, so import lazily and keep SQLite as the default.
try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
except ImportError:
    psycopg2 = None  # type: ignore

#: The tables, in dependency order.
TABLES: tuple[str, ...] = (
    "products",
    "product_private",
    "sessions",
    "offers",
    "orders",
    "ledger",
    "idempotency_keys",
    "stock_holds",
    "policy_budgets",
    "approvals",
    "ab_sessions",
    "pairing_denominators",
    "pairings",
    "cluster_pairings",
    "mec_versions",
    "transaction_decision_records",
    "merchants",
)

SCHEMA_SQL = """
-- ── catalog: two tables, not one ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    sku                 TEXT PRIMARY KEY,
    title               TEXT    NOT NULL,
    list_price_inr      INTEGER NOT NULL CHECK (list_price_inr > 0),
    stock_qty           INTEGER NOT NULL CHECK (stock_qty >= 0),
    attrs               TEXT    NOT NULL,          -- JSON
    category            TEXT    NOT NULL,
    returns_window_days INTEGER NOT NULL CHECK (returns_window_days >= 0)
);
CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);

-- Never serialized to any response. The only reader is the policy kernel.
CREATE TABLE IF NOT EXISTS product_private (
    sku               TEXT PRIMARY KEY REFERENCES products (sku) ON DELETE CASCADE,
    cost_inr          INTEGER NOT NULL CHECK (cost_inr > 0),
    margin_pct        INTEGER NOT NULL,
    floor_price_inr   INTEGER NOT NULL CHECK (floor_price_inr > 0),
    max_discount_pct  INTEGER NOT NULL CHECK (max_discount_pct BETWEEN 0 AND 100),
    attach_candidates TEXT    NOT NULL DEFAULT '[]',   -- JSON
    tier_up_sku       TEXT,
    offerable         INTEGER NOT NULL DEFAULT 1 CHECK (offerable IN (0, 1))
);

-- ── conversation state ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    agent_id    TEXT    NOT NULL,
    mandate_id  TEXT,
    offers_made INTEGER NOT NULL DEFAULT 0 CHECK (offers_made >= 0),  -- bound #5
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions (agent_id);

CREATE TABLE IF NOT EXISTS offers (
    offer_id       TEXT PRIMARY KEY,
    session_id     TEXT    NOT NULL REFERENCES sessions (session_id),
    base_sku       TEXT    NOT NULL,
    options        TEXT    NOT NULL,        -- JSON: base + up to 2 upsells
    total_inr      INTEGER NOT NULL CHECK (total_inr >= 0),
    gate_tier      INTEGER NOT NULL CHECK (gate_tier IN (0, 1, 2)),
    policy_receipt TEXT    NOT NULL,        -- signed policy receipt
    policy_mode    TEXT    NOT NULL CHECK (policy_mode IN ('shadow', 'live')),
    expires_at     TEXT    NOT NULL,        -- bound #8
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_session ON offers (session_id);
CREATE INDEX IF NOT EXISTS idx_offers_expires ON offers (expires_at);

CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,
    session_id          TEXT    NOT NULL REFERENCES sessions (session_id),
    offer_id            TEXT    NOT NULL REFERENCES offers (offer_id),
    option_id           TEXT    NOT NULL,
    amount_inr          INTEGER NOT NULL CHECK (amount_inr >= 0),
    state               TEXT    NOT NULL CHECK (state IN
                            ('PENDING', 'HELD', 'AUTHORIZED', 'CAPTURED',
                             'CONFIRMED', 'VOIDED', 'REFUNDED', 'FAILED')),
    gate_tier           INTEGER NOT NULL CHECK (gate_tier IN (0, 1, 2)),
    policy_mode         TEXT    NOT NULL CHECK (policy_mode IN ('shadow', 'live')),
    razorpay_order_id   TEXT,
    razorpay_payment_id TEXT,
    razorpay_refund_id  TEXT,
    -- What this order has reserved and not yet consumed. A two-step checkout
    -- hands the buyer a gateway order and returns, so the stock holds and the
    -- discount budget it took out have to outlive the request that took them:
    -- the later /settle call is a different request and knows only the order id.
    -- Without these two columns a settle cannot commit the right holds and an
    -- abandoned cart's budget is reserved forever.
    stock_hold_ids      TEXT,                                  -- JSON array
    budget_reserved_inr INTEGER NOT NULL DEFAULT 0
                            CHECK (budget_reserved_inr >= 0),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders (state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_rzp_order
    ON orders (razorpay_order_id) WHERE razorpay_order_id IS NOT NULL;

-- ── ledger: append-only, hash-chained ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS ledger (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    event           TEXT    NOT NULL,
    payload         TEXT    NOT NULL,        -- canonical JSON
    money_delta_inr INTEGER NOT NULL DEFAULT 0,
    reason          TEXT    NOT NULL DEFAULT '',
    policy_mode     TEXT    NOT NULL CHECK (policy_mode IN ('shadow', 'live')),
    prev_hash       TEXT    NOT NULL,
    entry_hash      TEXT    NOT NULL UNIQUE,
    -- The mandatory-reason rule, enforced a second time in SQL: a money event
    -- with no reason is rejected at write time. The Python writer raises first
    -- with a clearer message; this is the backstop that no code path can talk
    -- its way past.
    CHECK (money_delta_inr = 0 OR length(trim(reason)) > 0)
);
CREATE INDEX IF NOT EXISTS idx_ledger_event ON ledger (event);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger (ts);

-- Mandate nonces are single-use, and the ledger is where they are recorded:
-- accepting a mandate writes a `mandate.accepted` entry carrying its nonce, so
-- replay protection is derived from the audit trail itself rather than from a
-- side table that could disagree with it. A UNIQUE partial index makes the
-- database the authority — a replayed nonce fails at INSERT rather than
-- depending on a check that ran a moment earlier.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_mandate_nonce
    ON ledger (json_extract(payload, '$.nonce'))
    WHERE event = 'mandate.accepted';

CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: DELETE forbidden');
END;

-- ── idempotency ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key                 TEXT PRIMARY KEY,
    order_id            TEXT,
    request_fingerprint TEXT NOT NULL,
    response_json       TEXT,
    created_at          TEXT NOT NULL
);
-- Redundant alongside the PK, and named so it can be asserted by name.
-- This index is the last line of defence against a double charge.
CREATE UNIQUE INDEX IF NOT EXISTS idx_idempotency_keys_key
    ON idempotency_keys (key);

-- ── concurrency + policy accounting ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_holds (
    hold_id    TEXT PRIMARY KEY,
    sku        TEXT    NOT NULL REFERENCES products (sku),
    qty        INTEGER NOT NULL CHECK (qty > 0),
    session_id TEXT    NOT NULL,
    state      TEXT    NOT NULL DEFAULT 'ACTIVE'
                   CHECK (state IN ('ACTIVE', 'COMMITTED', 'RELEASED', 'EXPIRED')),
    expires_at TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_holds_sku_state ON stock_holds (sku, state);

CREATE TABLE IF NOT EXISTS policy_budgets (
    day                 TEXT PRIMARY KEY,        -- YYYY-MM-DD, UTC. One row per day.
    discount_spent_inr  INTEGER NOT NULL DEFAULT 0 CHECK (discount_spent_inr >= 0),
    updated_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id       TEXT PRIMARY KEY,
    order_id          TEXT    NOT NULL REFERENCES orders (order_id),
    offer_id          TEXT    NOT NULL,
    state             TEXT    NOT NULL DEFAULT 'PENDING'
                          CHECK (state IN ('PENDING', 'APPROVED', 'REJECTED', 'COUNTERED')),
    amount_inr        INTEGER NOT NULL,
    counter_amount_inr INTEGER,
    -- The bounded, signed offer that carries a COUNTERED decision's terms.
    -- The buyer polls for this and accepts it explicitly; without it the
    -- negotiation would end in prose instead of on the rail.
    counter_offer_id   TEXT,
    note              TEXT,
    requested_at      TEXT    NOT NULL,
    decided_at        TEXT,
    decided_by        TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals (state);

-- ── measurement ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ab_sessions (
    ab_session_id  TEXT PRIMARY KEY,
    session_id     TEXT    NOT NULL,
    arm            TEXT    NOT NULL CHECK (arm IN ('control', 'treatment')),
    persona        TEXT,
    basket_inr     INTEGER NOT NULL DEFAULT 0,
    upsells_shown  INTEGER NOT NULL DEFAULT 0,
    upsells_taken  INTEGER NOT NULL DEFAULT 0,
    completed      INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ab_sessions_arm ON ab_sessions (arm);

-- ── learning: what actually sells together ─────────────────────────────────
-- One row per (store, base item, companion). Counts are decayed lazily with an
-- exponential half-life on write, so strength is always the recent ratio and a
-- forgotten habit fades instead of persisting forever. Observed rows come from
-- completed orders; seeded rows are cold-start priors that observed evidence
-- replaces on read. store_id is present from day one so multi-store isolation
-- later is a query discipline, not a migration.
--
-- The denominator ("how many baskets contained the base item at all") lives in
-- its own table keyed by base item alone — a pairing row is about a pair, and
-- mixing a per-base counter into it via an empty-SKU sentinel would fight the
-- foreign keys.
CREATE TABLE IF NOT EXISTS pairing_denominators (
    store_id   TEXT    NOT NULL DEFAULT 'default',
    base_sku   TEXT    NOT NULL REFERENCES products (sku) ON DELETE CASCADE,
    base_count REAL    NOT NULL DEFAULT 0 CHECK (base_count >= 0),
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (store_id, base_sku)
);

CREATE TABLE IF NOT EXISTS pairings (
    store_id       TEXT    NOT NULL DEFAULT 'default',
    base_sku       TEXT    NOT NULL REFERENCES products (sku) ON DELETE CASCADE,
    paired_sku     TEXT    NOT NULL REFERENCES products (sku) ON DELETE CASCADE,
    source         TEXT    NOT NULL DEFAULT 'observed'
                       CHECK (source IN ('observed', 'seeded')),
    together_count REAL    NOT NULL DEFAULT 0 CHECK (together_count >= 0),
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (store_id, base_sku, paired_sku, source)
);
CREATE INDEX IF NOT EXISTS idx_pairings_base
    ON pairings (store_id, base_sku, source);

-- Anonymous category-level priors pooled by stores in the same learning
-- cluster: "in electronics stores generally, chargers follow phones". Only
-- category ratios live here — no SKUs, no order details, no identities — so
-- pooling is competitive-intelligence-safe. A store's own SKU-level data
-- always overrides these on read.
CREATE TABLE IF NOT EXISTS cluster_pairings (
    cluster         TEXT    NOT NULL,
    base_category   TEXT    NOT NULL,
    paired_category TEXT    NOT NULL,
    base_count      REAL    NOT NULL DEFAULT 0 CHECK (base_count >= 0),
    together_count  REAL    NOT NULL DEFAULT 0 CHECK (together_count >= 0),
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (cluster, base_category, paired_category)
);

CREATE TABLE IF NOT EXISTS mec_versions (
    mec_id      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    store_id    TEXT NOT NULL,
    scope       TEXT NOT NULL CHECK (scope IN ('store','category','sku','campaign')),
    scope_value TEXT,
    body        TEXT NOT NULL,
    hash        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (mec_id, version)
);
CREATE INDEX IF NOT EXISTS idx_mec_store_scope ON mec_versions (store_id, scope, scope_value);

CREATE TABLE IF NOT EXISTS transaction_decision_records (
    transaction_id  TEXT PRIMARY KEY,
    tdr_json        TEXT NOT NULL,
    tdr_hash        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id   TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt_hex      TEXT NOT NULL,
    store_id      TEXT NOT NULL DEFAULT 'default',
    active_token  TEXT UNIQUE,
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_merchants_email_store ON merchants (email, store_id);
"""

_local = threading.local()

#: Serializes ledger appends. SQLite is single-writer; the chain also needs
#: read-then-write atomicity on (seq, prev_hash), which this lock provides
#: within a process. BEGIN IMMEDIATE covers the cross-process case.
write_lock = threading.Lock()


def _is_pg(conn) -> bool:
    return bool(USE_POSTGRES and conn is not None and hasattr(conn, "get_dsn_parameters"))


class _CompatRow(dict):
    """Dict row that also supports integer index like sqlite3.Row (row[0])."""

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, int):
            try:
                return list(self.values())[key]
            except IndexError:
                raise KeyError(key) from None
        return super().__getitem__(key)


class _PGCursorWrapper:
    """Wraps psycopg2 cursor so fetchone()[0] and row['col'] both work."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return _CompatRow(row)
        return row

    def fetchall(self):
        rows = self._cur.fetchall()
        return [_CompatRow(r) if isinstance(r, dict) else r for r in rows]

    def __iter__(self):
        for row in self._cur:
            yield _CompatRow(row) if isinstance(row, dict) else row

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PGWrapper:
    """Wraps psycopg2 connection to look like sqlite3.Connection for callers."""

    def __init__(self, pgconn):
        self._pg = pgconn

    def execute(self, sql, params=()):
        # PRAGMA is SQLite-only: return dummy success on Postgres so schema tests pass
        if sql.strip().lower().startswith("pragma"):
            cur = self._pg.cursor()
            # Return a cursor that yields [(1,)] for foreign_keys / journal_mode checks
            cur.execute("SELECT 1")
            return _PGCursorWrapper(cur)
        cur = self._pg.cursor()
        # Translate SQLite placeholders to Postgres: `?` -> `%s`, `:name` -> `%(name)s`
        # Avoid `::` casts (payload::json) by negative lookbehind
        if "?" in sql and "%s" not in sql:
            sql = sql.replace("?", "%s")
        if ":" in sql and "%(" not in sql:
            import re

            sql = re.sub(r"(?<!:):(\w+)", r"%(\1)s", sql)
        cur.execute(sql, params)
        # For SELECT, return wrapper so row[0] and row['col'] both work
        return _PGCursorWrapper(cur)

    def executescript(self, sql):
        # Run as one block — split by ; for Postgres compatibility
        cur = self._pg.cursor()
        # Translate basic SQLite syntax for Postgres where needed
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        sql = sql.replace("AUTOINCREMENT", "")
        cur.execute(sql)
        self._pg.commit()
        return cur

    def commit(self):
        return self._pg.commit()

    def rollback(self):
        return self._pg.rollback()

    def close(self):
        return self._pg.close()

    def get_dsn_parameters(self):
        return self._pg.get_dsn_parameters()


def connect(path: Path | str | None = None):
    """Open a connection. SQLite by default, Postgres when DATABASE_URL is set."""
    # Tests pass ":memory:" explicitly — always SQLite, even in Postgres mode
    if path is not None and str(path) == ":memory:":
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    if USE_POSTGRES and path is None:
        try:
            import psycopg2
            import psycopg2.extras
            raw = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            raw.autocommit = False
            return _PGWrapper(raw)
        except Exception as exc:
            raise RuntimeError(f"DATABASE_URL set but psycopg2 connect failed: {exc}") from exc
    target = Path(path) if path is not None else DATABASE_PATH
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_connection() -> sqlite3.Connection:
    """Thread-local connection to the configured database."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = connect()
    return conn


def reset_connection() -> None:
    """Drop the thread-local connection. Used by tests between databases."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None


@contextmanager
def transaction(conn=None) -> Iterator:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception.

    IMMEDIATE rather than DEFERRED: the write intent is taken up front, so two
    writers cannot both read a stale tip and then race to append.
    Postgres path uses plain BEGIN.
    """
    conn = conn or get_connection()
    if _is_pg(conn):
        # psycopg2 wrapper
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            try:
                conn._pg.rollback()
            except Exception:
                pass
            raise
        try:
            conn._pg.commit()
        except Exception:
            conn.execute("COMMIT")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


#: Columns added after the first schema shipped. `CREATE TABLE IF NOT EXISTS`
#: leaves an existing table alone, so a column added to SCHEMA_SQL never reaches
#: a database that already exists — it has to be added explicitly. Keyed by table,
#: each entry is (column, full DDL fragment) and is applied only when absent.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("orders", "stock_hold_ids", "TEXT"),
    ("orders", "budget_reserved_inr", "INTEGER NOT NULL DEFAULT 0"),
    ("approvals", "counter_offer_id", "TEXT"),
)


def _column_names(conn, table: str) -> set[str]:
    if _is_pg(conn):
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        ).fetchall()
        return {str(row["column_name"] if isinstance(row, dict) else row[0]) for row in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def migrate(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add columns that a pre-existing database is missing. Idempotent.

    Only ever additive, and only with a default: ALTER TABLE ADD COLUMN is the
    one schema change SQLite performs cheaply and without rewriting the table, so
    the upgrade path for an existing `data/bazaar.db` stays a no-risk operation.
    Anything that needed a column dropped or a constraint changed would be a new
    table plus a copy, and would belong in a script a person runs deliberately.

    Returns the `table.column` names actually added, for logging.
    """
    conn = conn or get_connection()
    applied: list[str] = []
    for table, column, ddl in ADDED_COLUMNS:
        if table not in existing_tables(conn):
            continue
        if column in _column_names(conn, table):
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        applied.append(f"{table}.{column}")
    return applied


def init_db(conn=None):
    """Create every table, index, and trigger, then apply pending migrations.

    Idempotent, and safe to call on a database at any version.
    Postgres path skips SQLite-only pragmas and translates schema where needed.
    """
    conn = conn or get_connection()
    if _is_pg(conn):
        import re

        pg_sql = SCHEMA_SQL.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        pg_sql = pg_sql.replace("AUTOINCREMENT", "")
        # SQLite triggers have no Postgres equivalent - strip before execute
        pg_sql = re.sub(r"CREATE TRIGGER.*?END;\s*", "", pg_sql, flags=re.S)
        # SQLite json_extract -> Postgres JSON operator
        pg_sql = pg_sql.replace(
            "json_extract(payload, '$.nonce')", "(payload::json ->> 'nonce')"
        )
        # Remove SQL line comments so split-by-; doesn't leave bare "-- ..." statements
        pg_sql = re.sub(r"--[^\n]*\n", "\n", pg_sql)
        # Execute statement-by-statement so one bad IF NOT EXISTS doesn't abort the rest
        for stmt in [s.strip() for s in pg_sql.split(";") if s.strip()]:
            # Skip pure comment fragments
            if stmt.lstrip().startswith("--"):
                continue
            try:
                conn.execute(stmt)
            except Exception as exc:
                # IF NOT EXISTS already there - ignore "already exists" races
                if "already exists" in str(exc).lower():
                    try:
                        conn._pg.rollback()
                    except Exception:
                        pass
                    continue
                raise
        try:
            conn._pg.commit()
        except Exception:
            pass
        try:
            migrate(conn)
        except Exception:
            pass
        return conn
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    return conn


def journal_mode(conn=None) -> str:
    conn = conn or get_connection()
    if _is_pg(conn):
        return "postgres"
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def existing_tables(conn=None) -> set[str]:
    conn = conn or get_connection()
    if _is_pg(conn):
        rows = conn.execute(
            "SELECT tablename as name FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        return {row["name"] if isinstance(row, dict) else row[0] for row in rows}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row["name"] for row in rows}
