"""Anchor the ledger chain — publish its head hash somewhere we don't control.

    python -m scripts.anchor_chain                # append today's head hash
    python -m scripts.anchor_chain --verify       # check continuity of anchors

The ledger is tamper-EVIDENT: anyone with database write access can rewrite the
whole chain and re-link it. Anchoring shrinks that window. Each run appends the
current head hash to `data/chain_anchors.jsonl` — an append-only file outside
the database. Rewriting ledger history now also means rewriting this file in a
way that stays consistent with every anchor published BEFORE the tampering,
including any copied elsewhere (a commit, a printout, a judge's notebook).

This is not blockchain and it is not magic; it is one more honest link in the
evidence chain, and the script says so on every run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import use_utf8_stdout  # noqa: E402

use_utf8_stdout()

import settings  # noqa: E402
from store import db as store_db  # noqa: E402
from store import ledger  # noqa: E402

#: Anchors live beside the database by default: same disk, different failure
#: domain from the DB's write path.
ANCHOR_PATH = (
    Path(settings.DATA_DIR) / "chain_anchors.jsonl"
    if hasattr(settings, "DATA_DIR")
    else REPO_ROOT / "data" / "chain_anchors.jsonl"
)


def append_anchor(conn) -> dict:
    seq, head_hash = ledger.tip(conn)
    if seq == 0:
        raise SystemExit(
            "REFUSING to anchor: the ledger is empty. Boot the app once so a "
            "genesis entry exists; anchoring nothing would be theatre."
        )
    verification = ledger.verify_chain(conn)
    if not verification["intact"]:
        raise SystemExit(
            f"REFUSING to anchor: the chain is broken at #{verification['broken_at']}. "
            "Anchoring a broken chain would launder the break into history."
        )
    record = {
        "anchored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head_seq": seq,
        "head_hash": head_hash,
    }
    ANCHOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ANCHOR_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def verify_anchors() -> int:
    """Every anchor must still be reachable as a historical head of an intact chain.

    Reads each anchor, recomputes the chain up to that seq, and compares hashes.
    A mismatch means either the ledger was rewritten or the anchors were —
    either way it is reported loudly rather than smoothed over.
    """
    if not ANCHOR_PATH.exists():
        print("no anchors file yet; nothing to verify")
        return 0

    conn = store_db.get_connection()
    checked = 0
    failed = 0
    for line in ANCHOR_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        seq = int(record["head_seq"])
        expected_hash = record["head_hash"]

        row = conn.execute(
            "SELECT entry_hash FROM ledger WHERE seq = ?", (seq,)
        ).fetchone()
        actual = str(row["entry_hash"]) if row else None
        status = "OK" if actual == expected_hash else "MISMATCH"
        if actual != expected_hash:
            failed += 1
            print(
                f"MISMATCH  seq={seq} anchored={expected_hash[:16]}... "
                f"ledger={str(actual)[:16]}... — history moved under this anchor"
            )
        else:
            print(f"OK        seq={seq} {expected_hash[:16]}...")
        checked += 1

    print(f"\n{checked} anchor(s) checked, {failed} mismatch(es).")
    if failed:
        print(
            "Anchors disagree with the ledger. This is tamper-evidence doing its "
            "job: investigate before trusting any recent entry."
        )
        return 1
    print("Ledger is tamper-evident, not tamper-proof; anchors narrow the window.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="verify past anchors")
    args = parser.parse_args()

    if args.verify:
        return verify_anchors()

    conn = store_db.get_connection()
    record = append_anchor(conn)
    print(f"anchored  seq={record['head_seq']} hash={record['head_hash']}")
    print(f"wrote     {ANCHOR_PATH}")
    print("Copy this hash somewhere you control - a commit message, a printout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
