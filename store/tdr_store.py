from __future__ import annotations

import json
import sqlite3
import dataclasses

from policy.tdr import TransactionDecisionRecord
from store.canonical import canonical_json, entry_hash
from store.db import get_connection

def save_tdr(tdr: TransactionDecisionRecord, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    body_dict = dataclasses.asdict(tdr)
    body_str = canonical_json(body_dict)
    # Using entry_hash from canonical as the hash
    tdr_hash = entry_hash(body_str)
    
    conn.execute(
        """
        INSERT INTO transaction_decision_records (transaction_id, tdr_json, tdr_hash, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (tdr.transaction_id, body_str, tdr_hash, tdr.created_at)
    )

def get_tdr(transaction_id: str, *, conn: sqlite3.Connection | None = None) -> TransactionDecisionRecord | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT tdr_json FROM transaction_decision_records WHERE transaction_id = ?",
        (transaction_id,)
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["tdr_json"])
    return TransactionDecisionRecord(**data)

def update_tdr_outcome(transaction_id: str, outcome: str, *, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT tdr_json FROM transaction_decision_records WHERE transaction_id = ?",
        (transaction_id,)
    ).fetchone()
    if not row:
        return
        
    data = json.loads(row["tdr_json"])
    data["outcome"] = outcome
    new_json = canonical_json(data)
    new_hash = entry_hash(new_json)
    
    conn.execute(
        """
        UPDATE transaction_decision_records 
        SET tdr_json = ?, tdr_hash = ?
        WHERE transaction_id = ?
        """,
        (new_json, new_hash, transaction_id)
    )
