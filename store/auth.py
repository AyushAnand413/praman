"""Merchant auth store — email/password per store with simple hash.

Uses PBKDF2-HMAC-SHA256, no extra dependency. Token is random hex stored on row.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

from store.db import get_connection, transaction
from store.timestamps import utc_now, to_ts

if TYPE_CHECKING:
    from store.db import _PGWrapper


def _hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return dk.hex()


def create_merchant(*, email: str, password: str, store_id: str = "default", conn: " _PGWrapper | None" = None) -> dict:
    email = email.strip().lower()
    if "@" not in email or len(password) < 6:
        raise ValueError("email must contain @ and password >=6 chars")
    conn = conn or get_connection()
    salt_hex = secrets.token_hex(16)
    pwd_hash = _hash_password(password, salt_hex)
    token = secrets.token_hex(32)
    now = to_ts(utc_now())
    with transaction(conn):
        conn.execute(
            "INSERT INTO merchants (merchant_id, email, password_hash, salt_hex, store_id, active_token, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"MCH-{secrets.token_hex(6)}", email, pwd_hash, salt_hex, store_id, token, now),
        )
    row = conn.execute("SELECT * FROM merchants WHERE email = ? AND store_id = ?", (email, store_id)).fetchone()
    return dict(row)


def get_by_email(email: str, store_id: str = "default", conn: " _PGWrapper | None" = None) -> dict | None:
    conn = conn or get_connection()
    row = conn.execute("SELECT * FROM merchants WHERE email = ? AND store_id = ?", (email.strip().lower(), store_id)).fetchone()
    return dict(row) if row else None


def get_by_token(token: str, conn: " _PGWrapper | None" = None) -> dict | None:
    conn = conn or get_connection()
    row = conn.execute("SELECT * FROM merchants WHERE active_token = ?", (token,)).fetchone()
    return dict(row) if row else None


def verify_password(email: str, password: str, store_id: str = "default", conn: " _PGWrapper | None" = None) -> dict | None:
    row = get_by_email(email, store_id, conn=conn)
    if not row:
        return None
    calc = _hash_password(password, row["salt_hex"])
    if calc != row["password_hash"]:
        return None
    return row


def rotate_token(merchant_id: str, conn: " _PGWrapper | None" = None) -> str:
    conn = conn or get_connection()
    token = secrets.token_hex(32)
    conn.execute("UPDATE merchants SET active_token = ? WHERE merchant_id = ?", (token, merchant_id))
    conn.commit()
    return token
