# Issues — `dashboard/` (Next.js Merchant Console)

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


## High

| # | File:line | Issue | Fix |
|---|---|---|---|
| D1 | `dashboard/app/page.jsx:localStorage` | Merchant key stored in `window.localStorage` plaintext persistent — XSS can steal, no httpOnly | Demo scope OK; prod: `sessionStorage` or memory + re-auth, or httpOnly cookie |
| D2 | `dashboard/app/page.jsx:fetch` | `setInterval 5000ms` fixed without backoff, abort controller, or `document.hidden` pause — background tab hammers backend | Add visibility pause + exponential backoff on 5xx |
| D3 | `dashboard/app/page.jsx:fetch` | Missing try around `JSON.parse` on dashboard response; `decide()` POST has no error toast | Wrap fetches in try/catch + surface toast |
| D4 | `dashboard/app/page.jsx:money()` | `Number(n).toLocaleString('en-IN')` — browser locale differences | Format server-side or pin `Intl.NumberFormat('en-IN')` |

## Medium

| # | Issue |
|---|---|
| D5 | `NEXT_PUBLIC_API_URL` defaults to `http://localhost:8000` — prod deploy without env points to localhost with confusing errors |
| D6 | `BAD_EVENTS` hardcoded set — new failure events (e.g. `pre_filter.rejected_all`) won't highlight as failures (green). Derive from API `is_failure` flag. |
| D7 | No pagination for feed (`FEED_LENGTH 30` server-side) — client assumes fit; large approval queue not paginated |
| D8 | `dashboard/app/page.jsx:load(key)` called without await race — rapid key re-entry interleaves fetches |

## Low

- `vercel.json` framework preset correct but no env var mapping for preview vs prod documented.
- `next.config.mjs` `reactStrictMode: true` good.
