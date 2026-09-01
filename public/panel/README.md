# `api/static/panel/` — Merchant Panel UI

## What this folder does (simple)

The actual browser app the merchant sees.

- `index.html` — page skeleton
- `panel.css` — dark monospace styling (14KB)
- `panel.js` — fetches dashboard JSON, renders metrics/approvals/feed, handles key storage

Connected to: `api/app.py` (serves at `/panel`), `api/dashboard.py` + `api/approvals.py` (data + actions)

## Issues — `api/static/panel/`

| # | Issue | Detail |
|---|---|---|
| P1 | Merchant key in `localStorage` (persistent, readable by any XSS) | Demo scope OK, but prod should use `sessionStorage` or httpOnly cookie |
| P2 | `panel.js:money()` uses `Number(n).toLocaleString('en-IN')` — locale-dependent formatting | Format server-side or fix locale |
| P3 | `BAD_EVENTS` set in JS hardcodes failure highlights — new ledger event names won't highlight as failures | Derive from API or include explicit `is_failure` flag from `api/dashboard.py` |
