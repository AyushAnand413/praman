# Issues — `integrations/` (Shopify Bridge)

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


## High

| # | File:line | Issue | Impact |
|---|---|---|---|
| I1 | `integrations/shopify.py:sync_catalog` | Single-variant only (`variants[0]`) — multi-variant stores lose SKUs silently, sync shows `skipped` but merchant loses catalog without warning | Add variant expansion or explicit error when `variants.length > 1` |
| I2 | `integrations/shopify.py:map_product` derived cost | `cost = price * (100 - assumed_margin)/100` with `SHOPIFY_ASSUMED_MARGIN_PCT=40` — arbitrary; if real COGS higher, floor too low → sells below true margin | Require explicit cost import or merchant override UI |
| I3 | `integrations/shopify.py:sync_catalog` pagination | Uses `since_id = max(id)` but breaks when page <100 even if gaps (deleted IDs) hide more products; `Link` header not used | Switch to `Link` header pagination |
| I4 | `integrations/shopify.py` | No retry/backoff on Shopify 429 — burst sync fails with partial catalog; `seed_database_from_rows` not called if page fails mid-way | Add 429 retry with `Retry-After` |

## Medium

| # | Issue |
|---|---|
| I5 | `shopify.py:map_product` raises `ShopifyError` for missing price/variants — caught as `skipped_titles[20]` but logs only titles not IDs — hard to debug |
| I6 | `shopify.py:create_refund` uses `float(total_paid)` vs `int` rupees elsewhere — paise mismatch risk |
| I7 | `SHOPIFY_SYNC_PAGE_LIMIT=100` + `SHOPIFY_ADMIN_API_BASE_TEMPLATE 2024-10` hardcoded — will drift on Shopify version deprecation |
| I8 | `shopify.py:sync_catalog(conn: Any)` loses type safety + ignores `tenancy.store_id` — sync overwrites `default` store only, multi-store not isolated |

## Low

- Sync is correctly idempotent (upserts) — safe to re-run.
- Transport is pure `httpx` with `X-Shopify-Access-Token` — no SDK magic.