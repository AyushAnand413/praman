"""GET /audit/{id} and GET /audit/verify — the public audit trail.

Both are unauthenticated by design: an audit trail nobody can read is not
evidence. Budget < 200ms, plain indexed reads.

`/audit/verify` recomputes the entire chain and reports the first break, so a
tampered row surfaces as `{"intact": false, "broken_at": N}` rather than as a
quiet inconsistency.

`/audit/{id}` takes either a sequence number or an entity id. A number reads one
entry; `ORD-...`, `OF-...`, `SES-...` or `APV-...` returns every entry that
mentions it, in order, which is the URL handed to a buyer agent at checkout. One
path serves both because a buyer who is given an audit link should not have to
know which kind of identifier they were given.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from store import ledger

router = APIRouter(prefix="/audit", tags=["audit"])


# Declared before /audit/{ref} so the literal path always wins the match.
@router.get("/verify", summary="Recompute the ledger hash chain")
def verify() -> dict[str, Any]:
    return ledger.verify_chain()


@router.get("/{ref}", summary="Read one ledger entry, or one entity's trail")
def entry(ref: str) -> dict[str, Any]:
    if ref.isdigit():
        found = ledger.get(int(ref))
        if found is None:
            raise HTTPException(
                status_code=404, detail=f"no ledger entry with seq {ref}"
            )
        return found.as_public()

    entries = ledger.trail(ref)
    if not entries:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no ledger entries mention {ref!r}. Pass a sequence number for a "
                f"single entry, or one of {', '.join(ledger.TRAIL_KEYS)}"
            ),
        )
    return {
        "id": ref,
        "entry_count": len(entries),
        "money_delta_inr": sum(e.money_delta_inr for e in entries),
        "first_seq": entries[0].seq,
        "last_seq": entries[-1].seq,
        "entries": [e.as_public() for e in entries],
        "verify_url": "/audit/verify",
    }
