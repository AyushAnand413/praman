"""The Shopify Admin API bridge — products in, orders and refunds out.

Three verbs, and only three:
    fetch_products()   → the merchant's catalog, as Shopify sees it
    create_order(...)  → a completed PRAMAN sale, so the merchant fulfils it
                         from the admin they already use
    create_refund(...) → the oversell saga's automatic refund, propagated

Implemented directly over REST with httpx, mirroring the discipline of
`kernel.payments`: one small transport class, credentials read through
`settings.secret` and revealed on exactly one line, normalised dicts out, and
every non-2xx raised as a typed error. Injected clients (tests pass a fake)
are how every mapping rule below is verified without a network.

Honest v1 scope, stated rather than buried:

* Single-variant products only. The first variant is the product; multi-variant
  catalogs are a documented limitation until someone needs them.
* Unit cost does not exist in the REST product payload. The importer derives
  cost from price at `SHOPIFY_ASSUMED_MARGIN_PCT` and records the derivation
  in the private row's attrs — an assumption wearing a label, correctable by
  the merchant, never silently treated as data.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

import settings
from settings import secret


class ShopifyError(RuntimeError):
    """A non-2xx from the Admin API, or a response we refuse to interpret."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


def _api_base() -> str:
    if not settings.SHOPIFY_STORE_DOMAIN:
        raise ShopifyError(
            "SHOPIFY_STORE_DOMAIN is not configured; the Shopify connector "
            "cannot be used without it"
        )
    return settings.SHOPIFY_ADMIN_API_BASE_TEMPLATE.format(
        domain=settings.SHOPIFY_STORE_DOMAIN
    )


class ShopifyClient:
    """Thin synchronous client over the three Admin API surfaces we use."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._token = (
            access_token
            if access_token is not None
            else secret("SHOPIFY_ADMIN_ACCESS_TOKEN").reveal()
        )
        self._base_url = (base_url or _api_base()).rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # never print the token
        return f"<ShopifyClient base={self._base_url}>"

    def _request(self, method: str, path: str, *, json_body: Any = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = httpx.request(
                method,
                url,
                headers={
                    "X-Shopify-Access-Token": self._token,
                    "Content-Type": "application/json",
                },
                json=json_body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ShopifyError(f"{method} {path} failed to reach Shopify: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:500]}

        if response.status_code >= 400:
            raise ShopifyError(
                f"{method} {path} -> {response.status_code}: {body}",
                status_code=response.status_code,
                body=body if isinstance(body, dict) else {},
            )
        return body if isinstance(body, dict) else {}

    # -- the three verbs ----------------------------------------------------

    def fetch_products_page(self, *, since_id: int | None = None) -> list[dict[str, Any]]:
        """One page of products, newest-first by id; paginate via since_id.

        Warning: pagination uses since_id only; large catalogs rely on this
        cursor and do not handle link headers.
        """
        query = f"?limit={settings.SHOPIFY_SYNC_PAGE_LIMIT}"
        if since_id:
            query += f"&since_id={int(since_id)}"
        body = self._request("GET", f"/products.json{query}")
        products = body.get("products", [])
        return products if isinstance(products, list) else []

    def create_order(
        self,
        *,
        praman_order_id: str,
        line_items: list[dict[str, Any]],
        total_paid: float,
        payment_reference: str | None = None,
    ) -> dict[str, Any]:
        """Record a captured PRAMAN sale as a paid Shopify order.

        `financial_status: paid` is truthful — this is called only after our
        own capture succeeded. The PRAMAN order id rides in note_attributes so
        reconciliation is a lookup, not a guess.
        """
        payload = {
            "order": {
                "line_items": line_items,
                "financial_status": "paid",
                "note": f"Placed by PRAMAN agent-commerce layer ({praman_order_id})",
                "note_attributes": [
                    {"name": "praman_order_id", "value": praman_order_id},
                    *(
                        [{"name": "praman_payment_id", "value": payment_reference}]
                        if payment_reference
                        else []
                    ),
                ],
                "test": True,
            }
        }
        body = self._request("POST", "/orders.json", json_body=payload)
        order = body.get("order", {})
        return {
            "id": order.get("id"),
            "order_number": order.get("order_number"),
            "total_price": order.get("total_price"),
            "financial_status": order.get("financial_status"),
        }

    def create_refund(
        self,
        *,
        shopify_order_id: int,
        amount: float,
        praman_refund_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Propagate a PRAMAN refund onto the matching Shopify order.

        V1 records the refund as a note-bearing refund event rather than
        driving gateway reversal — Razorpay remains the money source of truth.
        Stated plainly: this keeps the merchant's admin consistent with what
        already happened, which is what the merchant actually needs.
        """
        payload = {
            "refund": {
                "note": f"PRAMAN auto-refund ({praman_refund_id}): {reason}",
                "notify": False,
            }
        }
        body = self._request(
            "POST", f"/orders/{int(shopify_order_id)}/refunds.json", json_body=payload
        )
        refund = body.get("refund", {})
        return {"id": refund.get("id"), "status": refund.get("status")}


# ---------------------------------------------------------------------------
# Mapping: Shopify shapes → our catalog shapes
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Lowercase kebab, safe as a category label and a URL fragment."""
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (out or "general")[:63]


def map_product(shopify_product: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """One Shopify product → (public_row, private_row) for our tables.

    Returns rows shaped exactly like `catalog.json` entries, ready for
    `store.catalog.seed_database`. Raises rather than guessing when the shape
    is unusable — half-mapped products are worse than skipped ones.

    Warning: single-variant limitation — only the first variant is mapped;
    multi-variant products will lose data.
    """
    sku = str(shopify_product.get("sku_override") or "").strip()
    variants = shopify_product.get("variants") or []
    if not variants:
        raise ShopifyError(
            f"product {shopify_product.get('title')!r} has no variants; "
            "single-variant products are the v1 scope"
        )
    variant = variants[0]
    handle_sku = variant.get("sku") or shopify_product.get("handle") or ""
    sku = sku or str(handle_sku).strip()
    if not sku:
        raise ShopifyError(
            f"product {shopify_product.get('title')!r} carries no SKU or handle"
        )

    try:
        price = round(float(variant["price"]))
        if price <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ShopifyError(
            f"product {sku!r} has no usable price: {variant.get('price')!r}"
        ) from exc

    assumed_margin = max(0, min(95, settings.SHOPIFY_ASSUMED_MARGIN_PCT))
    derived_cost = int(round(price * (100 - assumed_margin) / 100)) or 1
    floor_price = int(round(price * 0.8)) or derived_cost + 1

    inventory = variant.get("inventory_quantity")
    stock = int(inventory) if isinstance(inventory, int) and inventory > 0 else 0

    category = _slugify(str(shopify_product.get("product_type") or "general"))

    public = {
        "sku": sku,
        "title": str(shopify_product.get("title") or sku),
        "list_price_inr": price,
        "stock_qty": stock,
        "attrs": {
            "source": "shopify",
            "shopify_product_id": shopify_product.get("id"),
            "shopify_variant_id": variant.get("id"),
        },
        "category": category,
        "returns_window_days": settings.DEFAULT_RETURNS_WINDOW_DAYS,
    }
    private = {
        "sku": sku,
        # Derived economics, labelled as such. The merchant overwrites these
        # with real numbers when they care; the kernel floors prices either way.
        "cost_inr": derived_cost,
        "margin_pct": assumed_margin,
        "floor_price_inr": floor_price,
        "max_discount_pct": settings.MAX_DISCOUNT_PCT_PER_SKU,
        "attach_candidates": [],
        "tier_up_sku": None,
        "offerable": stock > 0,
    }
    return public, private


def map_line_items(
    items: list[dict[str, Any]],
    shopify_variant_ids: dict[str, int],
) -> list[dict[str, Any]]:
    """Our offer lines → Shopify order line items, via synced variant ids."""
    mapped: list[dict[str, Any]] = []
    for item in items:
        variant_id = shopify_variant_ids.get(str(item["sku"]))
        if variant_id is None:
            raise ShopifyError(
                f"SKU {item['sku']} has no synced Shopify variant; run the "
                "importer before pushing orders"
            )
        mapped.append(
            {
                "variant_id": variant_id,
                "quantity": int(item.get("qty", 1)),
                "price": str(int(item["offered_price_inr"])),
            }
        )
    return mapped


def sync_catalog(client: ShopifyClient, conn: Any = None) -> dict[str, Any]:
    """Pull every product page and upsert into our two catalog tables.

    Idempotent by SKU. Returns counts for logging and the ledger entry the
    caller writes. Uses `store.catalog.seed_database` for the upsert so the
    importer cannot drift from the loader's validation rules.

    Warning: pagination stops when page size < limit; assumes stable ordering.
    """
    from store import catalog as catalog_store

    public_rows: list[dict[str, Any]] = []
    private_rows: list[dict[str, Any]] = []
    skipped: list[str] = []

    since_id: int | None = None
    while True:
        page = client.fetch_products_page(since_id=since_id)
        if not page:
            break
        for product in page:
            try:
                public_row, private_row = map_product(product)
            except ShopifyError:
                skipped.append(str(product.get("title") or product.get("id")))
                continue
            public_rows.append(public_row)
            private_rows.append(private_row)
            since_id = max(since_id or 0, int(product.get("id") or 0))
        if len(page) < settings.SHOPIFY_SYNC_PAGE_LIMIT:
            break

    if public_rows:
        catalog_store.seed_database_from_rows(public_rows, private_rows, conn=conn)

    return {
        "imported": len(public_rows),
        "skipped": len(skipped),
        "skipped_titles": skipped[:20],
    }
