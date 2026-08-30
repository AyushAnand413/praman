"""Schema shape: all declared tables exist, and journal_mode is WAL."""

from __future__ import annotations

import sqlite3

import pytest

from store.db import TABLES, existing_tables, journal_mode


def test_all_declared_tables_exist(db):
    present = existing_tables(db)
    missing = [name for name in TABLES if name not in present]
    assert not missing, f"missing tables: {missing}"
    assert len(TABLES) == 17


def test_journal_mode_is_wal(db):
    assert journal_mode(db) == "wal"


def test_foreign_keys_are_enforced(db):
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO stock_holds (hold_id, sku, qty, session_id, expires_at, created_at)
               VALUES ('h1', 'NO-SUCH-SKU', 1, 's1', '2026-01-01T00:00:00Z',
                       '2026-01-01T00:00:00Z')"""
        )


def test_idempotency_key_is_unique(db):
    """The unique key is the last line of defence against a double charge."""
    db.execute(
        """INSERT INTO idempotency_keys (key, order_id, request_fingerprint, created_at)
           VALUES ('idem-1', 'ORD-1', 'fp', '2026-01-01T00:00:00Z')"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO idempotency_keys (key, order_id, request_fingerprint, created_at)
               VALUES ('idem-1', 'ORD-2', 'fp', '2026-01-01T00:00:00Z')"""
        )


def test_named_unique_index_on_idempotency_keys_exists(db):
    """The schema names this index explicitly, so assert it by name, not just behaviour."""
    indexes = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='idempotency_keys'"
        )
    }
    assert "idx_idempotency_keys_key" in indexes


def test_seeded_catalog_has_fourteen_rows_in_both_tables(db):
    assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 14
    assert db.execute("SELECT count(*) FROM product_private").fetchone()[0] == 14


def test_order_state_is_constrained(db):
    """An order cannot drift into an undefined state."""
    db.execute(
        """INSERT INTO sessions (session_id, agent_id, offers_made, created_at, updated_at)
           VALUES ('s1', 'agent-1', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
    )
    db.execute(
        """INSERT INTO offers (offer_id, session_id, base_sku, options, total_inr,
                               gate_tier, policy_receipt, policy_mode, expires_at, created_at)
           VALUES ('OF-1', 's1', 'AT-PRO-BLK', '[]', 5598, 0, 'sig', 'shadow',
                   '2026-01-01T00:05:00Z', '2026-01-01T00:00:00Z')"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO orders (order_id, session_id, offer_id, option_id, amount_inr,
                                   state, gate_tier, policy_mode, created_at, updated_at)
               VALUES ('ORD-1', 's1', 'OF-1', 'opt-1', 5598, 'PROBABLY_FINE', 0,
                       'shadow', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
        )
