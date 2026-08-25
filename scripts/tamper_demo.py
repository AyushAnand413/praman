"""The tamper demo.

    python scripts/tamper_demo.py

Hand-edits a historic ledger row and shows `/audit/verify` reporting
`{intact: false, broken_at: N}`.

Two things this demonstrates, in order:

1. A plain `UPDATE` on the ledger is refused outright — `store.db` installs
   BEFORE UPDATE / BEFORE DELETE triggers, so "never UPDATE, never DELETE" is
   enforced by the database, not by everyone remembering.

2. Even after dropping that guard — which is what an attacker with database
   write access would do — the hash chain still catches the edit. That is the
   honest claim: tamper-EVIDENCE, not tamper-proofing.

Operates on a COPY by default. Tampering is not repairable: the chain is
append-only, so there is no way to un-break a real ledger. `--in-place` exists
but will permanently break the database you point it at.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._console import use_utf8_stdout  # noqa: E402
from settings import DATABASE_PATH, DATA_DIR  # noqa: E402
from store import ledger  # noqa: E402
from store.db import connect, init_db  # noqa: E402

DROP_GUARDS = "DROP TRIGGER IF EXISTS ledger_no_update; DROP TRIGGER IF EXISTS ledger_no_delete;"
RESTORE_GUARDS = """
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
"""


def ensure_history(conn: sqlite3.Connection) -> None:
    """Give the chain enough entries that a mid-chain edit is meaningful."""
    while ledger.tip(conn)[0] < 4:
        seq = ledger.tip(conn)[0] + 1
        ledger.append(
            "policy_kernel",
            "policy.approved",
            {"filler": True, "step": seq, "sku": "AT-PRO-BLK"},
            reason="tamper-demo fixture entry",
            conn=conn,
        )


def show(label: str, report: dict) -> None:
    keep = {k: report[k] for k in ("intact", "broken_at", "entries_checked", "detail")
            if k in report}
    print(f"{label:<26} {json.dumps(keep)}")


def main() -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-place", action="store_true",
                        help="tamper the real database - PERMANENT, not repairable")
    parser.add_argument("--target-seq", type=int, default=2,
                        help="which historic entry to edit (default 2)")
    args = parser.parse_args()

    if args.in_place:
        target_db = DATABASE_PATH
        print(f"!! tampering the REAL database in place: {target_db}")
    else:
        target_db = DATA_DIR / "tamper_demo.db"
        target_db.parent.mkdir(parents=True, exist_ok=True)
        if DATABASE_PATH.exists():
            shutil.copy2(DATABASE_PATH, target_db)
            print(f"working on a copy  {target_db}")
        else:
            print(f"no database at {DATABASE_PATH}; building a fresh one at {target_db}")

    conn = connect(target_db)
    init_db(conn)
    ensure_history(conn)

    print()
    show("before tampering", ledger.verify_chain(conn))

    row = conn.execute(
        "SELECT * FROM ledger WHERE seq = ?", (args.target_seq,)
    ).fetchone()
    if row is None:
        print(f"FAIL - no ledger entry at seq {args.target_seq}")
        return 1
    print(f"target entry               seq={row['seq']} event={row['event']} "
          f"payload={row['payload'][:60]}")

    # Step 1 - the guard refuses a normal edit.
    try:
        conn.execute(
            "UPDATE ledger SET payload = ? WHERE seq = ?",
            ('{"tampered":true}', args.target_seq),
        )
        print("\nUNEXPECTED - the UPDATE succeeded; the append-only trigger is missing")
        return 1
    except sqlite3.IntegrityError as exc:
        print(f"\nUPDATE refused             {exc}")

    # Step 2 - an attacker with write access drops the guard and edits anyway.
    print("dropping the append-only triggers (simulating DB write access)...")
    conn.executescript(DROP_GUARDS)
    tampered_payload = json.dumps(
        {**json.loads(row["payload"]), "tampered": True}, sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "UPDATE ledger SET payload = ? WHERE seq = ?",
        (tampered_payload, args.target_seq),
    )
    conn.executescript(RESTORE_GUARDS)
    print(f"row {args.target_seq} rewritten, guards restored\n")

    report = ledger.verify_chain(conn)
    show("after tampering", report)

    ok = report["intact"] is False and report["broken_at"] == args.target_seq
    print()
    if ok:
        print(f"PASS - chain break detected at seq {report['broken_at']}: {report['detail']}")
        print("Note: this is tamper-evidence, not tamper-proofing. Write access to "
              "the database allows rewriting the whole chain; what it cannot do is "
              "rewrite it unnoticed.")
        return 0
    print("FAIL - the edit was not detected. The chain is not doing its job.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
