from __future__ import annotations

import json
import sqlite3
import dataclasses

try:
    import psycopg2  # type: ignore
except ImportError:
    psycopg2 = None  # type: ignore
from typing import Optional

from policy.mec import (
    MEC,
    MECScope,
    HardConstraints,
    EconomicObjectives,
    NegotiationPermissions,
    ApprovalThresholds,
)
from store.canonical import canonical_json
from store.db import get_connection

def save_mec_version(mec: MEC, *, conn: sqlite3.Connection | None = None) -> None:
    """Save a new version of a MEC."""
    conn = conn or get_connection()
    body_dict = dataclasses.asdict(mec)
    body_dict["scope"] = mec.scope.value if hasattr(mec.scope, "value") else mec.scope
    
    # Convert Decimals in objectives and hard constraints
    if "objectives" in body_dict:
        for k, v in body_dict["objectives"].items():
            from decimal import Decimal
            if isinstance(v, Decimal):
                body_dict["objectives"][k] = str(v)
    if "hard_constraints" in body_dict:
        from decimal import Decimal
        for k, v in body_dict["hard_constraints"].items():
            if isinstance(v, Decimal):
                body_dict["hard_constraints"][k] = str(v)
    
    # The store_id and scope might be properties of mec, assume mec has them
    body_str = canonical_json(body_dict)
    
    # We must construct a hash
    import hashlib
    mec_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
    
    # Assume mec has these fields. If scope_value is None, we store NULL
    try:
        conn.execute(
            """
            INSERT INTO mec_versions (
                mec_id, version, store_id, scope, scope_value, body, hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mec.mec_id,
                mec.version,
                mec.store_id,
                (mec.scope.value if hasattr(mec.scope, "value") else mec.scope).lower(),
                mec.scope_value,
                body_str,
                mec_hash,
                mec.created_at,
            ),
        )
    except (sqlite3.IntegrityError, psycopg2.IntegrityError) as e:  # type: ignore
        raise ValueError(f"MEC version already exists: {e}")
    except Exception as e:
        if psycopg2 and isinstance(e, psycopg2.IntegrityError):  # type: ignore
            raise ValueError(f"MEC version already exists: {e}") from e
        raise

def _row_to_mec(row: sqlite3.Row) -> MEC:
    data = json.loads(row["body"])
    
    if "scope" in data:
        data["scope"] = MECScope(data["scope"].upper())
        
    if "hard_constraints" in data:
        from decimal import Decimal
        hc = data["hard_constraints"]
        if "min_margin_pct" in hc:
            hc["min_margin_pct"] = Decimal(hc["min_margin_pct"])
        if "approval_thresholds" in hc and isinstance(hc["approval_thresholds"], dict):
            hc["approval_thresholds"] = ApprovalThresholds(**hc["approval_thresholds"])
        data["hard_constraints"] = HardConstraints(**hc)
        
    if "objectives" in data:
        from decimal import Decimal
        obj = data["objectives"]
        for k, v in obj.items():
            obj[k] = Decimal(str(v))
        data["objectives"] = EconomicObjectives(**obj)
        
    if "negotiation" in data:
        data["negotiation"] = NegotiationPermissions(**data["negotiation"])
        
    return MEC(**data)

def get_mec(mec_id: str, version: int, *, conn: sqlite3.Connection | None = None) -> MEC | None:
    """Fetch a specific version of a MEC."""
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT body FROM mec_versions WHERE mec_id = ? AND version = ?",
        (mec_id, version)
    ).fetchone()
    if not row:
        return None
    return _row_to_mec(row)

def get_latest_mec(
    store_id: str, 
    scope: MECScope, 
    scope_value: str | None = None, 
    *, 
    conn: sqlite3.Connection | None = None
) -> MEC | None:
    """Fetch the latest version of a MEC for a given scope."""
    conn = conn or get_connection()
    scope_str = (scope.value if hasattr(scope, "value") else scope).lower()
    
    if scope_value is None:
        row = conn.execute(
            """
            SELECT body FROM mec_versions 
            WHERE store_id = ? AND scope = ? AND scope_value IS NULL 
            ORDER BY version DESC LIMIT 1
            """,
            (store_id, scope_str)
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT body FROM mec_versions 
            WHERE store_id = ? AND scope = ? AND scope_value = ? 
            ORDER BY version DESC LIMIT 1
            """,
            (store_id, scope_str, scope_value)
        ).fetchone()
        
    if not row:
        return None
    return _row_to_mec(row)

def list_mec_history(store_id: str, *, conn: sqlite3.Connection | None = None) -> list[MEC]:
    """List all MEC versions for a store, newest first."""
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT body FROM mec_versions WHERE store_id = ? ORDER BY version DESC",
        (store_id,)
    ).fetchall()
    return [_row_to_mec(row) for row in rows]
