"""Create the schema, seed the catalog, and report what exists.

    python scripts/init_db.py

Idempotent — safe to re-run. Deliberately does NOT drop anything: the ledger is
append-only, and a script that can wipe it would be a loaded gun sitting next to
the audit trail.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._console import use_utf8_stdout  # noqa: E402
from settings import DATABASE_PATH, POLICY_MODE  # noqa: E402
from store import catalog, ledger  # noqa: E402
from store.db import TABLES, existing_tables, get_connection, init_db, journal_mode  # noqa: E402


def main() -> int:
    use_utf8_stdout()
    conn = get_connection()
    init_db(conn)
    seeded = catalog.seed_database(conn=conn)
    catalog.cache.load(conn)

    if ledger.tip(conn)[0] == 0:
        ledger.append(
            "system", "ledger.genesis",
            {"merchant": "Aether Audio", "catalog_skus": seeded},
            conn=conn,
        )

    present = existing_tables(conn)
    missing = [name for name in TABLES if name not in present]

    print(f"database      {DATABASE_PATH}")
    print(f"journal_mode  {journal_mode(conn)}")
    print(f"tables        {len(present)} present, {len(TABLES)} expected")
    for name in TABLES:
        count = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  {'ok     ' if name in present else 'MISSING'} {name:<18} {count:>4} rows")
    print(f"catalog cache {len(catalog.cache)} SKUs, loaded_at={catalog.cache.loaded_at}")
    print(f"ledger head   seq={ledger.tip(conn)[0]}")
    print(f"POLICY_MODE   {POLICY_MODE.value}")

    if missing:
        print(f"\nFAIL - missing tables: {missing}")
        return 1
    print("\nok - schema, catalog, and ledger genesis are in place")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
