"""Catalog loading, caching, and the single DB -> HTTP serializer.

The rule, restated: **`to_public()` is the only path from the database to an
HTTP response body.** Nothing else in this codebase may build a product dict.

`to_public` is a whitelist, not a filter. It constructs a fresh dict from the
seven fields declared public, so a private field added to the schema in month
three is invisible to buyers by default. A blacklist would leak it, and the
leak test would only catch the names someone remembered to list.

All 14 SKUs are held in memory, so a catalog query never touches SQLite.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from settings import CATALOG_PATH, CATALOG_SKU_COUNT
from store.db import get_connection, transaction

#: The seven public product fields. This tuple is the contract.
PUBLIC_FIELDS: tuple[str, ...] = (
    "sku",
    "title",
    "list_price_inr",
    "stock_qty",
    "attrs",
    "category",
    "returns_window_days",
)

#: Field names that must never appear in a response body. The leak test reads
#: this list, so adding a private column here automatically extends the test.
PRIVATE_FIELDS: tuple[str, ...] = (
    "cost_inr",
    "margin_pct",
    "floor_price_inr",
    "max_discount_pct",
    "attach_candidates",
    "attach_rate",
    "tier_up_sku",
    "offerable",
)


class CatalogError(RuntimeError):
    """The catalog file is malformed or internally inconsistent."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# The serializer
# ---------------------------------------------------------------------------


def to_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """The only DB -> HTTP path for product data.

    Builds a new dict containing exactly PUBLIC_FIELDS. Anything else in the
    input — including private economics joined in by an over-eager query — is
    dropped rather than trusted.
    """
    source = dict(row)
    attrs = source.get("attrs")
    if isinstance(attrs, str):
        attrs = json.loads(attrs)
    return {
        "sku": source["sku"],
        "title": source["title"],
        "list_price_inr": int(source["list_price_inr"]),
        "stock_qty": int(source["stock_qty"]),
        "attrs": attrs,
        "category": source["category"],
        "returns_window_days": int(source["returns_window_days"]),
    }


# ---------------------------------------------------------------------------
# File loading + validation
# ---------------------------------------------------------------------------


def load_catalog_file(path: Path | str | None = None) -> dict[str, Any]:
    """Parse and validate catalog.json. Raises rather than importing bad data."""
    target = Path(path) if path is not None else CATALOG_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))

    public = raw.get("products")
    private = raw.get("product_private")
    if not isinstance(public, list) or not isinstance(private, list):
        raise CatalogError("catalog.json needs `products` and `product_private` arrays")

    if len(public) != CATALOG_SKU_COUNT:
        raise CatalogError(f"expected {CATALOG_SKU_COUNT} products, found {len(public)}")

    for product in public:
        extra = set(product) - set(PUBLIC_FIELDS)
        missing = set(PUBLIC_FIELDS) - set(product)
        if extra:
            raise CatalogError(
                f"{product.get('sku')}: public row has non-public fields {sorted(extra)}. "
                "Adding a public field is a decision about what buyers may see."
            )
        if missing:
            raise CatalogError(f"{product.get('sku')}: public row missing {sorted(missing)}")

    public_skus = {p["sku"] for p in public}
    private_skus = {p["sku"] for p in private}
    if public_skus != private_skus:
        raise CatalogError(
            f"products / product_private do not join: "
            f"{sorted(public_skus ^ private_skus)} appears in only one"
        )

    for row in private:
        for ref in (row.get("tier_up_sku"),):
            if ref is not None and ref not in public_skus:
                raise CatalogError(f"{row['sku']}: tier_up_sku {ref} is not a real SKU")
        for candidate in row.get("attach_candidates", []):
            if candidate["sku"] not in public_skus:
                raise CatalogError(
                    f"{row['sku']}: attach candidate {candidate['sku']} is not a real SKU"
                )

    return raw


def seed_database(path: Path | str | None = None, conn: sqlite3.Connection | None = None) -> int:
    """Load catalog.json into `products` + `product_private`. Idempotent."""
    raw = load_catalog_file(path)
    conn = conn or get_connection()
    with transaction(conn):
        for product in raw["products"]:
            conn.execute(
                """INSERT INTO products
                       (sku, title, list_price_inr, stock_qty, attrs, category,
                        returns_window_days)
                   VALUES (:sku, :title, :list_price_inr, :stock_qty, :attrs,
                           :category, :returns_window_days)
                   ON CONFLICT (sku) DO UPDATE SET
                       title = excluded.title,
                       list_price_inr = excluded.list_price_inr,
                       stock_qty = excluded.stock_qty,
                       attrs = excluded.attrs,
                       category = excluded.category,
                       returns_window_days = excluded.returns_window_days""",
                {**product, "attrs": json.dumps(product["attrs"], sort_keys=True)},
            )
        for row in raw["product_private"]:
            conn.execute(
                """INSERT INTO product_private
                       (sku, cost_inr, margin_pct, floor_price_inr, max_discount_pct,
                        attach_candidates, tier_up_sku, offerable)
                   VALUES (:sku, :cost_inr, :margin_pct, :floor_price_inr,
                           :max_discount_pct, :attach_candidates, :tier_up_sku, :offerable)
                   ON CONFLICT (sku) DO UPDATE SET
                       cost_inr = excluded.cost_inr,
                       margin_pct = excluded.margin_pct,
                       floor_price_inr = excluded.floor_price_inr,
                       max_discount_pct = excluded.max_discount_pct,
                       attach_candidates = excluded.attach_candidates,
                       tier_up_sku = excluded.tier_up_sku,
                       offerable = excluded.offerable""",
                {
                    **row,
                    "attach_candidates": json.dumps(
                        row.get("attach_candidates", []), sort_keys=True
                    ),
                    "offerable": int(row.get("offerable", True)),
                },
            )
    return len(raw["products"])


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


class CatalogCache:
    """All 14 SKUs in memory. A catalog query must not touch the DB.

    Public and private views are held in separate dicts so a caller cannot
    reach private economics by accident — asking for them is a distinct call
    to `private()`, which only the kernel makes.
    """

    def __init__(self) -> None:
        self._public: dict[str, dict[str, Any]] = {}
        self._private: dict[str, dict[str, Any]] = {}
        self._loaded_at: str | None = None
        self._lock = threading.Lock()

    def load(self, conn: sqlite3.Connection | None = None) -> "CatalogCache":
        conn = conn or get_connection()
        public: dict[str, dict[str, Any]] = {}
        private: dict[str, dict[str, Any]] = {}
        for row in conn.execute("SELECT * FROM products ORDER BY sku"):
            public[row["sku"]] = to_public(row)
        for row in conn.execute("SELECT * FROM product_private ORDER BY sku"):
            record = dict(row)
            record["attach_candidates"] = json.loads(record["attach_candidates"])
            record["offerable"] = bool(record["offerable"])
            private[record["sku"]] = record
        with self._lock:
            self._public, self._private = public, private
            self._loaded_at = _utc_now()
        return self

    @property
    def loaded_at(self) -> str | None:
        return self._loaded_at

    def __len__(self) -> int:
        return len(self._public)

    def public(self, sku: str) -> dict[str, Any] | None:
        """Public view of one SKU. Returns a copy — callers must not mutate."""
        found = self._public.get(sku)
        return dict(found) if found else None

    def all_public(self, *, offerable_only: bool = True) -> list[dict[str, Any]]:
        """Every serializable product.

        Self-healed SKUs are omitted entirely rather than shown as unavailable
        — `offerable` is a private policy flag and must not be inferable from
        a response.
        """
        skus = sorted(self._public)
        if offerable_only:
            skus = [s for s in skus if self._private.get(s, {}).get("offerable", True)]
        return [dict(self._public[s]) for s in skus]

    def private(self, sku: str) -> dict[str, Any] | None:
        """Kernel-only. Never pass this to a serializer or an LLM prompt."""
        found = self._private.get(sku)
        return dict(found) if found else None

    def set_offerable(
        self, sku: str, offerable: bool, conn: sqlite3.Connection | None = None
    ) -> None:
        """Self-heal hook for the oversell saga."""
        conn = conn or get_connection()
        with transaction(conn):
            conn.execute(
                "UPDATE product_private SET offerable = ? WHERE sku = ?",
                (int(offerable), sku),
            )
        with self._lock:
            if sku in self._private:
                self._private[sku]["offerable"] = offerable


#: Process-wide cache. Populated by the app factory at startup.
cache = CatalogCache()
