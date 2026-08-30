# PRAMAN Merchant Dashboard (`dashboard`)

> **Simple:** The merchant's screen. Polls `GET /merchant/v1/dashboard` every 5s and shows revenue, approval queue (approve/reject with one click), live ledger feed, bounds status, and hash-chain health. Merchant key lives in `localStorage`.

The **PRAMAN Merchant Dashboard** (`aether-bazaar-dashboard`) is a dedicated Next.js web application providing a real-time merchant console and observability interface for the PRAMAN / Aether Bazaar agentic storefront.

## Simple — what each file does & who it calls

| File | Plain job | Calls |
|---|---|---|
| `app/page.jsx` | React console: key input + polling + 6 panels (mode, metrics, approvals, feed, bounds, chain) | `GET /merchant/v1/dashboard`, `POST /merchant/v1/approvals/{id}/{action}` |
| `app/layout.jsx` | HTML shell, title `Aether Audio · BAZAAR` | Next.js |
| `app/globals.css` | Terminal theme (16 tokens, monospace) | `page.jsx` |
| `next.config.mjs` / `vercel.json` / `package.json` | Build/deploy config | Vercel |
| `.env.example` | `NEXT_PUBLIC_API_URL=http://localhost:8000` template | `dashboard/app/page.jsx` |

It connects to the PRAMAN backend service (`FastAPI + MCP`) to offer full visibility and control over autonomous agent commerce operations, financial metrics, human-in-the-loop approval queues, standing policy bounds, and cryptographic tamper-evident audit ledgers.

---

## 🚀 Key Capabilities & Architecture

- **Real-Time Observability**: Continuously polls the backend (`GET /merchant/v1/dashboard`) every 5 seconds to ensure live telemetry of store activities.
- **Operating Mode Awareness**: Prominently highlights the active system mode (such as **Shadow Mode** in amber or **Live Mode** in green) so merchants always understand the execution context.
- **Financial & Unit Economics Dashboard**: Displays revenue, order volumes, Average Order Value (AOV), upsell revenue, discount budget usage, and margin efficiency per ₹1 discounted.
- **Human-in-the-Loop (Tier-2) Approvals**: Allows merchants to review, approve, or reject orders held by negotiation or policy thresholds via interactive single-click actions (`POST /merchant/v1/approvals/{id}/{action}`).
- **Live Ledger Feed with Failure Visibility**: Displays the sequential audit log with explicit highlighting for blocked events, saga compensations, refunds, policy rejections, and self-healing events.
- **Tamper-Evidence & Bounds Monitoring**: Tracks the 10 active policy bounds (highlighting rules triggered recently) and verifies the cryptographic hash-chain integrity of the ledger.

---

## 📁 Directory Structure & File Descriptions

```
dashboard/
├── .env.example          # Sample environment configuration template
├── .gitignore            # Git ignore patterns for Next.js and Node artifacts
├── next.config.mjs       # Next.js configuration module
├── package.json          # Package manifest, dependencies, and NPM scripts
├── vercel.json           # Vercel deployment configuration
└── app/
    ├── globals.css       # Monospace dark-mode terminal stylesheet
    ├── layout.jsx        # Root HTML layout and metadata definition
    └── page.jsx          # Main client-side merchant console application
```

### File Breakdown

| File / Directory | Purpose & Description |
| :--- | :--- |
| **`.env.example`** | Documents required frontend environment variables, specifying `NEXT_PUBLIC_API_URL` (defaults to `http://localhost:8000` where the FastAPI backend runs). |
| **`.gitignore`** | Excludes standard Node and Next.js build artifacts (`node_modules`, `.next`, `.env*.local`) from version control. |
| **`next.config.mjs`** | Configures Next.js with React Strict Mode enabled (`reactStrictMode: true`). |
| **`package.json`** | Defines project metadata (`aether-bazaar-dashboard`), execution scripts (`dev`, `build`, `start`), and dependencies (`next` v14.2.5, `react` v18.3.1, `react-dom` v18.3.1). |
| **`vercel.json`** | Specifies the deployment schema and framework preset (`framework: "nextjs"`) for zero-configuration hosting on Vercel. |
| **`app/layout.jsx`** | Sets up the root HTML document structure (`<html lang="en">`) and configures page metadata including Title (`"Aether Audio · BAZAAR"`) and Description. |
| **`app/globals.css`** | Custom dark-theme styling inspired by high-density developer consoles. Configures CSS custom properties for palette tokens (`--bg`, `--panel`, `--green`, `--amber`, `--red`), monospace font stacks (`ui-monospace`, `Cascadia Mono`), responsive grids, feed badge colors, and approval button styles. |
| **`app/page.jsx`** | Core client-side React component (`"use client"`). Implements the merchant dashboard UI: |
| | - **Merchant Key Authentication**: Inputs and stores the `X-Merchant-Key` in `localStorage`. |
| | - **Live Metrics Panel**: Renders today's orders, revenue, AOV, upsells, discount spend/budget, and margin ratios. |
| | - **Approval Queue Panel**: Lists pending Tier-2 approvals with `APPROVE` and `REJECT` action triggers. |
| | - **Live Ledger Feed Panel**: Real-time event log with sequence numbers, INR money deltas, event types, and reason codes. |
| | - **Active Bounds Panel**: Displays active standing bounds and monitors tamper-evident hash chain status (`intact` vs `BROKEN at #seq`). |

---

## 🛠️ Getting Started

### 1. Prerequisites
- **Node.js** (v18.17+ or v20+ recommended)
- **npm**, **pnpm**, or **yarn**
- Running PRAMAN FastAPI backend (typically at `http://localhost:8000`)

### 2. Installation
Navigate to the `dashboard` directory and install dependencies:
```bash
cd dashboard
npm install
```

### 3. Environment Setup
Create a `.env.local` file from `.env.example`:
```bash
cp .env.example .env.local
```
Ensure `NEXT_PUBLIC_API_URL` points to your backend:
```env
NEXT_PUBLIC_API_URL=http://localhost:8000
```

### 4. Running the Development Server
```bash
npm run dev
```
Open [http://localhost:3000](http://localhost:3000) in your browser.

### 5. Authentication
When prompted on the dashboard, enter the merchant key configured in your backend environment (default for local development: `DEMO_KEY`).

---

## 🔗 Backend API Integration

The dashboard communicates with the following endpoints on the backend:
- `GET /merchant/v1/dashboard` – Fetches aggregated metrics, approval queue, ledger feed tail, active bounds, and chain integrity status (requires header `X-Merchant-Key`).
- `POST /merchant/v1/approvals/{approval_id}/approve` – Approves and releases a held order.
- `POST /merchant/v1/approvals/{approval_id}/reject` – Rejects and voids a held order.
