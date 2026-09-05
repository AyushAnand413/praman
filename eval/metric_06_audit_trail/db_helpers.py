"""Database helpers for Metric 6 with guaranteed try...finally restoration."""
from __future__ import annotations

import json
from typing import Any
from store import ledger
from store.db import get_connection, transaction


def get_latest_ledger_entry(conn=None) -> dict[str, Any]:
    c = conn or get_connection()
    row = c.execute("SELECT seq, prev_hash, payload, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    if not row:
        ledger.append("eval", "audit.init", {"init": True}, conn=c)
        row = c.execute("SELECT seq, prev_hash, payload, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    return dict(row)


def execute_with_tamper_restoration(
    seq: int,
    mutate_sql: str,
    mutate_params: dict[str, Any],
    restore_sql: str,
    restore_params: dict[str, Any],
    conn=None,
) -> dict[str, Any]:
    """Execute a tamper test with guaranteed restoration in finally block."""
    c = conn or get_connection()
    try:
        c.execute("ALTER TABLE ledger DISABLE TRIGGER ledger_no_update")
        c.execute(mutate_sql, mutate_params)
        report_tampered = ledger.verify_chain(conn=c)
        return report_tampered
    finally:
        c.execute(restore_sql, restore_params)
        c.execute("ALTER TABLE ledger ENABLE TRIGGER ledger_no_update")
        restored_report = ledger.verify_chain(conn=c)
        assert restored_report["intact"] is True, f"CRITICAL: Failed to restore ledger hash chain: {restored_report}"
