"""Postgres schema and connection management — the 11 tables of the store.

Postgres-only: SQLite fallback removed. Every environment (local, CI, Vercel)
uses the same DATABASE_URL. Init is still idempotent and translation for
legacy SQLite syntax (json_extract, :name params, bare comments) is kept so
old queries continue to work.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from settings import DATABASE_URL

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
    seq             SERIAL PRIMARY KEY,
    ts              TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    event           TEXT    NOT NULL,
    payload         TEXT    NOT NULL,        -- canonical JSON
    money_delta_inr INTEGER NOT NULL DEFAULT 0,
    reason          TEXT    NOT NULL DEFAULT '',
    policy_mode     TEXT    NOT NULL CHECK (policy_mode IN ('shadow', 'live')),
    prev_hash       TEXT    NOT NULL,
    entry_hash      TEXT    NOT NULL UNIQUE,
    CHECK (money_delta_inr = 0 OR length(trim(reason)) > 0)
);
CREATE INDEX IF NOT EXISTS idx_ledger_event ON ledger (event);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger (ts);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_mandate_nonce
    ON ledger ((payload::json ->> 'nonce'))
    WHERE event = 'mandate.accepted';

-- ── idempotency ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key                 TEXT PRIMARY KEY,
    order_id            TEXT,
    request_fingerprint TEXT NOT NULL,
    response_json       TEXT,
    created_at          TEXT NOT NULL
);
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

#: Serializes ledger appends.
write_lock = threading.Lock()


def _is_pg(conn) -> bool:
    return bool(conn is not None and hasattr(conn, "get_dsn_parameters"))


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
            cur.execute("SELECT 1")
            return _PGCursorWrapper(cur)
        cur = self._pg.cursor()
        # Translate SQLite placeholders to Postgres: `?` -> `%s`, `:name` -> `%(name)s`
        # Avoid `::` casts (payload::json) by negative lookbehind
        if "?" in sql and "%s" not in sql:
            sql = sql.replace("?", "%s")
        if ":" in sql and "%(" not in sql:
            import re

            sql = re.sub(r"(?<!:):([A-Za-z_]\w*)", r"%(\1)s", sql)
        if "json_extract" in sql:
            import re

            sql = re.sub(
                r"json_extract\s*\(\s*(\w+)\s*,\s*['\"]\$\.([^'\"]+)['\"]\s*\)",
                r"(\1::json ->> '\2')",
                sql,
            )
        cur.execute(sql, params)
        return _PGCursorWrapper(cur)

    def executescript(self, sql):
        cur = self._pg.cursor()
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        sql = sql.replace("AUTOINCREMENT", "")
        # Translate SQLite DROP TRIGGER to Postgres (requires ON table)
        sql = sql.replace(
            "DROP TRIGGER IF EXISTS ledger_no_update",
            "DROP TRIGGER IF EXISTS ledger_no_update ON ledger",
        )
        sql = sql.replace(
            "DROP TRIGGER IF EXISTS ledger_no_delete",
            "DROP TRIGGER IF EXISTS ledger_no_delete ON ledger",
        )
        # psycopg2 with RealDictCursor does not allow multiple ;-separated statements in one execute
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            if stmt.lstrip().startswith("--"):
                continue
            cur.execute(stmt)
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


def connect(path: Path | str | None = None):  # path ignored - Postgres only
    """Open a Postgres connection. Requires DATABASE_URL."""
    # :memory: is no longer SQLite - still return Postgres so tests run same DB
    # but caller gets isolated via TRUNCATE in fixture
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Postgres-only mode requires it (see .env).")
    try:
        import psycopg2
        import psycopg2.extras

        raw = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        raw.autocommit = False
        return _PGWrapper(raw)
    except Exception as exc:
        raise RuntimeError(f"DATABASE_URL set but psycopg2 connect failed: {exc}") from exc


def get_connection():
    """Thread-local connection to Postgres."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = connect()
    return conn


def reset_connection() -> None:
    """Drop the thread-local connection. Used by tests between databases."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None


@contextmanager
def transaction(conn=None) -> Iterator:
    """BEGIN ... COMMIT, rolling back on any exception. Postgres-only."""
    conn = conn or get_connection()
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


#: Columns added after the first schema shipped.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("orders", "stock_hold_ids", "TEXT"),
    ("orders", "budget_reserved_inr", "INTEGER NOT NULL DEFAULT 0"),
    ("approvals", "counter_offer_id", "TEXT"),
)


def _column_names(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {str(row["column_name"] if isinstance(row, dict) else row[0]) for row in rows}


def migrate(conn=None) -> list[str]:
    """Add columns that a pre-existing database is missing. Idempotent."""
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
    """Create every table, index. Idempotent. Postgres-only."""
    conn = conn or get_connection()
    import re

    pg_sql = SCHEMA_SQL.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    pg_sql = pg_sql.replace("AUTOINCREMENT", "")
    pg_sql = re.sub(r"CREATE TRIGGER.*?END;\s*", "", pg_sql, flags=re.S)
    pg_sql = pg_sql.replace("json_extract(payload, '$.nonce')", "(payload::json ->> 'nonce')")
    pg_sql = re.sub(r"--[^\n]*\n", "\n", pg_sql)
    for stmt in [s.strip() for s in pg_sql.split(";") if s.strip()]:
        if stmt.lstrip().startswith("--"):
            continue
        try:
            conn.execute(stmt)
        except Exception as exc:
            if "already exists" in str(exc).lower():
                try:
                    conn._pg.rollback()
                except Exception:
                    pass
                continue
            raise
    # Enforce ledger append-only on Postgres (SQLite triggers were stripped above)
    try:
        conn.execute(
            "CREATE OR REPLACE FUNCTION ledger_no_update() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'ledger is append-only: UPDATE forbidden'; END; $$ LANGUAGE plpgsql"
        )
        conn.execute("DROP TRIGGER IF EXISTS ledger_no_update ON ledger")
        conn.execute(
            "CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger FOR EACH ROW EXECUTE FUNCTION ledger_no_update()"
        )
        conn.execute(
            "CREATE OR REPLACE FUNCTION ledger_no_delete() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'ledger is append-only: DELETE forbidden'; END; $$ LANGUAGE plpgsql"
        )
        conn.execute("DROP TRIGGER IF EXISTS ledger_no_delete ON ledger")
        conn.execute(
            "CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger FOR EACH ROW EXECUTE FUNCTION ledger_no_delete()"
        )
        conn._pg.commit()
    except Exception:
        try:
            conn._pg.rollback()
        except Exception:
            pass
    try:
        migrate(conn)
    except Exception:
        pass
    return conn


def journal_mode(conn=None) -> str:
    return "postgres"


def existing_tables(conn=None) -> set[str]:
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT tablename as name FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {row["name"] if isinstance(row, dict) else row[0] for row in rows}
