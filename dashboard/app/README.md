# `dashboard/app/` — Next.js App Router

## What this folder does (simple)

The merchant web app the merchant actually opens.

| File | What it does | Connects to |
|---|---|---|
| `layout.jsx:4` | Root HTML shell — `<html lang="en">`, title `Aether Audio · BAZAAR` | Next.js framework |
| `page.jsx` | Main React client (`"use client"`): key input → poll `GET /merchant/v1/dashboard` every 5s → render metrics/approvals/feed/bounds/chain | `api/dashboard.py`, `api/approvals.py` |
| `globals.css` | Dark terminal theme — CSS vars `--bg`, `--green`, `--amber`, monospace font stack | `layout.jsx` |

## How it works (simple)

1. Merchant types `X-Merchant-Key` → saved to `localStorage`
2. `page.jsx` polls `/merchant/v1/dashboard` and renders 6 panels: mode banner, business metrics, approvals queue, live feed, bounds, hash chain
3. Approve/Reject buttons POST to `/merchant/v1/approvals/{id}/{approve,reject}`

## Issues — `dashboard/app/`

| # | Issue | Detail |
|---|---|---|
| A1 | `localStorage` for merchant key — XSS stealable | Use `sessionStorage` or httpOnly cookie in prod |
| A2 | Polling every 5s with no backoff/visibility check | Add `document.hidden` pause |
| A3 | `BAD_EVENTS` hardcoded highlight list | Server should send `is_failure: bool` |
| A4 | No loading/error skeleton on first paint | Add spinner + error boundary |

