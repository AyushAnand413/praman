"""Client module wrapping real Praman kernel functions and live AI calls."""
from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import settings
from eval.fixtures import EVAL_PRIVATE_PRODUCTS, EVAL_PUBLIC_PRODUCTS
from kernel import bounds, gates, receipt, stock
from kernel.bounds import LineItem
from kernel.offer import OfferRefused, build_offer
from mandate import verifier
from store import catalog, ledger
from store.db import get_connection, init_db, transaction


def setup_catalog(conn=None) -> None:
    """Load evaluation catalog fixtures into database and memory caches."""
    init_db()
    c = conn or get_connection()
    with transaction(c):
        c.execute("DELETE FROM pairings WHERE base_sku LIKE :pat OR paired_sku LIKE :pat", {"pat": "GE-%"})
        c.execute("DELETE FROM pairing_denominators WHERE base_sku LIKE :pat", {"pat": "GE-%"})
    catalog.seed_database_from_rows(EVAL_PUBLIC_PRODUCTS, EVAL_PRIVATE_PRODUCTS, conn=c, seed_priors=True)
    catalog.cache.load(c)


def call_get_offer(req: dict[str, Any], *, conn=None, generate=None) -> Any:
    """Call real build_offer against live AI proposer."""
    c = conn or get_connection()
    try:
        return build_offer(
            need=req.get("need", ""),
            base_sku=req.get("base_sku"),
            budget_inr=req.get("budget_inr"),
            delivery=req.get("delivery"),
            agent_id=req.get("agent_id", "eval-runner"),
            session_id=req.get("session_id"),
            generate=generate,
            conn=c,
        )
    except OfferRefused as exc:
        return {"refused": True, "reason": str(exc), "code": exc.code}


def call_assign_tier(
    total_inr: int,
    discount_pct: Decimal = Decimal(0),
    tripped_bounds: tuple[str, ...] = (),
    mandate_issuer_trusted: bool | None = None,
) -> Any:
    """Call real gates.assign_tier."""
    return gates.assign_tier(
        total_inr=total_inr,
        discount_pct=discount_pct,
        tripped_bounds=tripped_bounds,
        mandate_issuer_trusted=mandate_issuer_trusted,
    )


def call_verify_mandate(
    token: str,
    agent_id: str,
    cart_total_inr: int,
    categories: list[str],
    now=None,
    conn=None,
) -> Any:
    """Call real verifier.verify."""
    return verifier.verify(
        mandate_token=token,
        agent_id=agent_id,
        cart_total_inr=cart_total_inr,
        categories=categories,
        now=now,
        conn=conn,
    )
