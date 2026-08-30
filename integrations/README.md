# PRAMAN Merchant Platform Integrations (`integrations/`)

> **Simple:** The adapter. Pulls Shopify catalog in, pushes PRAMAN orders/refunds out. Zero policy logic here — if it breaks, sync just fails; it can never widen a discount or bypass a bound (those live in `kernel/`).

The `integrations` package provides thin, injectable bridges between PRAMAN's autonomous agent-commerce negotiation engine and external e-commerce platforms (such as Shopify).

## Simple — what each file does & its limits

| File | One-line job | Connected to |
|---|---|---|
| `shopify.py:fetch_products_page()` | `GET /products.json` page (`since_id`, limit 100) | Shopify Admin REST `2024-10` |
| `shopify.py:map_product()` | Shopify JSON → `public_row` + `private_row` (derives `cost` via `SHOPIFY_ASSUMED_MARGIN_PCT=40`) | `store/catalog.py` |
| `shopify.py:sync_catalog()` | Paginate all products, upsert valid, count `skipped_titles` | `store/db.py`, `store/ledger.py` |
| `shopify.py:create_order()` / `create_refund()` | Push paid order / refund to Shopify with `note_attributes: praman_order_id` | `kernel/checkout.py`, `kernel/saga.py` |
| `__init__.py` | Declares "bridge-only, no policy" boundary | — |

**V1 limits:** single-variant only (`variants[0]`), derived cost, strict skip on zero price/missing variant.

---

## Architectural Philosophy

1. **No Policy in Connectors**:
   - Integrations contain **zero business logic, bounds, or policy rules**.
   - Margin constraints, floor prices, discount gates, and financial ledgers remain strictly enforced in `kernel/` and `store/`.
   - If a connector fails or misbehaves, it can at worst fail a sync or an API push; it can **never** bypass safety vetoes or widen discount allowances.

2. **Security & Credentials**:
   - All credentials (e.g., Shopify Admin Access Tokens) are managed through PRAMAN's secret management (`settings.secret`).
   - Tokens are revealed only at the exact line of HTTP client initialization and are never exposed in logs or object `__repr__` strings.

3. **Pure Transport & Predictable Mapping**:
   - Communication is implemented directly over REST using `httpx`.
   - Products and order lines are strictly validated during transformation. Unusable, unpriced, or malformed data raises typed errors rather than creating corrupt or half-mapped state.

4. **Networkless Testability**:
   - Clients are designed for dependency injection, allowing comprehensive mapping, idempotency, and push verification using fakes without requiring live network calls.

---

## Module Overview

### 1. `__init__.py`
Defines the package boundary and documents the architectural mandate: integrations act as isolated bridges to commerce platforms (pull catalog in, push orders/refunds out) with strict separation from kernel governance.

### 2. `shopify.py`
The primary bridge for Shopify Admin REST API integration. Implements three core verbs:
- **`fetch_products_page()`**: Pulls the merchant's catalog page-by-page (`/products.json`).
- **`create_order()`**: Pushes a captured PRAMAN sale to Shopify (`/orders.json`) with `financial_status: paid`, attaching PRAMAN tracking IDs (`praman_order_id`, `praman_payment_id`) in `note_attributes` so merchants can fulfill orders directly from their native Shopify Admin dashboard.
- **`create_refund()`**: Propagates automatic refunds (e.g., oversell saga handling) to Shopify (`/orders/{id}/refunds.json`) as note-bearing refund events to keep backend accounting synchronized.

#### Key Components in `shopify.py`:
- **`ShopifyError`**: Typed exception class for API failures (non-2xx HTTP codes) and schema validation rejections.
- **`ShopifyClient`**: Lightweight synchronous REST client over Shopify Admin API endpoints (`2024-10` release), handling auth headers (`X-Shopify-Access-Token`), request formatting, and error parsing.
- **`map_product(shopify_product)`**: Transforms raw Shopify product JSON into PRAMAN's dual-row structure:
  - `public_row`: Customer-facing catalog data (`sku`, `title`, `list_price_inr`, `stock_qty`, `category`, `attrs`).
  - `private_row`: Internal economics data (`cost_inr`, `floor_price_inr`, `margin_pct`, `max_discount_pct`, `offerable`).
- **`map_line_items(items, shopify_variant_ids)`**: Converts internal PRAMAN offer items into Shopify order line item payloads matched by variant ID.
- **`sync_catalog(client, conn)`**: Idempotent catalog synchronization function that traverses paginated Shopify products using `since_id`, validates and maps each item, logs skipped unmappable entries, and upserts valid items into PRAMAN's SQLite tables (`products` and `product_private`).

---

## Configuration & Environment Variables

The Shopify integration relies on the following settings defined in `settings.py`:

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `SHOPIFY_STORE_DOMAIN` | `str` | `""` | Merchant's Shopify store domain (e.g. `your-store.myshopify.com`). |
| `SHOPIFY_ADMIN_ACCESS_TOKEN` | `secret` | `""` | Shopify Custom App Admin API access token. |
| `SHOPIFY_ADMIN_API_BASE_TEMPLATE` | `str` | `https://{domain}/admin/api/2024-10` | Base endpoint format string for Admin API calls. |
| `SHOPIFY_ASSUMED_MARGIN_PCT` | `int` | `40` | Default profit margin percentage used to derive unit cost when importing REST catalog items. |
| `SHOPIFY_SYNC_PAGE_LIMIT` | `int` | `100` | Maximum number of products fetched per page during catalog synchronization. |

---

## V1 Scope & Operational Constraints

- **Single-Variant Support**: V1 maps the first variant of each product. Multi-variant matrices are reserved for future iterations.
- **Derived Unit Cost**: Because Shopify REST product payloads omit unit cost, the importer derives cost from `list_price_inr` using `SHOPIFY_ASSUMED_MARGIN_PCT` and tags the source in attrs for merchant auditing and override.
- **Strict Validation**: Products with zero price, missing SKUs/handles, or missing variants are skipped and reported in sync ledger records rather than corrupted.
