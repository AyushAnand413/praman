# `api/static/` — Static Merchant Console Assets

## What this folder does (simple)

Serves the browser UI that merchants open at `GET /panel`. No Python runs here — just HTML/CSS/JS files mounted by `api/app.py:66`.

- Connected to: `api/app.py` (mounts `/panel`), `api/dashboard.py` (data source for `panel.js`)

## Files

| File | What it does | Connects to |
|---|---|---|
| `panel/index.html` | SPA shell — header, metrics grid, approvals queue, ledger feed, bounds panel | `panel.js`, `panel.css` |
| `panel/panel.css` | Dark terminal theme (monospace, amber/green/red tokens) | `index.html` |
| `panel/panel.js` | Fetches `GET /merchant/v1/dashboard` every 5s, renders approvals, handles approve/reject clicks | `api/dashboard.py`, `api/approvals.py` |

## How it works

1. Merchant enters `X-Merchant-Key` (stored in `localStorage`)
2. `panel.js` polls `/merchant/v1/dashboard` and renders panels
3. Approve/Reject buttons POST to `/merchant/v1/approvals/{id}/{action}`

## Issues — `api/static/`

| # | Issue | Fix |
|---|---|---|
| S1 | No `Content-Security-Policy` meta/header — `index.html` loads inline JS | Add CSP via `api/app.py` static mount or meta tag |
| S2 | `panel.js` polling has no backoff/visibility pause — background tab hammers backend | Add `document.hidden` pause + exponential backoff on 5xx |
| S3 | No cache headers on static mount — every reload refetches CSS/JS | Add `Cache-Control: public, max-age=300` |
