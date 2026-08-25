"""Persistence — SQLite schema, catalog loader, and the append-only ledger.

Holds the one serialization rule that matters: a single `to_public` function is
the only path from the database to an HTTP response body. Private product
economics — cost, margin, floor price, attach rates — never cross it.

Modules: db.py (11 tables, WAL, unique index on idempotency_keys.key),
catalog.py (loader + to_public + in-memory cache), ledger.py (canonical JSON,
SHA256 hash chain, mandatory-reason rule).
"""
