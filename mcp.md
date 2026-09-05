# PRAMAN MCP — shop the store from any AI agent

Same store, same rules as the HTTP API. The MCP tools call the same handlers with the same validation, so rate limits, ledger writes, and the kernel's veto all apply. A refusal comes back as a raised error, never as plain text.

- **Server code:** `api/mcp.py` (mounted in `api/app.py`)
- **Discovery:** `/.well-known/agent-commerce.json` advertises `"mcp": "/mcp"`

## Endpoints

| Transport | URL | Use when |
|---|---|---|
| Streamable HTTP | `http://localhost:8000/mcp` · prod `https://<brain>/mcp` | Default for modern clients |
| SSE fallback | `…/mcp-sse` | Client chokes on chunked streamable HTTP on serverless (e.g. `mcp-remote` on Vercel) |

The server is stateless (`stateless_http=True` in `api/mcp.py`): continuity lives in `session_id` and the offer row, so restarts and scaling never break an agent mid-flow.

## Tools (4)

| Tool | What it does | Key params |
|---|---|---|
| `search_products` | Find products. Free, no authorisation, public fields only | `need`, `budget_inr`, `category`, `limit`, `agent_id` |
| `get_offer` | Price a purchase. Returns 1–2 options with totals, savings, reasons, `gate_tier`, signed `policy_receipt`, `audit_url` | `need`, `agent_id` (required), `session_id`, `qty`, `base_sku`, `category`, `budget_inr` |
| `buy` | Accept one option. Price comes from the stored offer — you never send an amount | `offer_id`, `option_id`, `agent_id` (required), `idempotency_key`, `mandate`, `payment_id` |
| `check_order` | Poll order state. Held orders report `pending_merchant_approval` until a human decides | `order_id` |

Flow: `search_products` → `get_offer` → `buy` → `check_order`.

## Run it

**Local:**

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in DATABASE_URL + keys
python -m uvicorn api.app:app --reload   # MCP live at http://localhost:8000/mcp
```

No extra process — `/mcp` starts with the API (one server per app on `app.state`, lifespan-managed).

**Prod:** deploy the backend with a public HTTPS URL and env vars set on the host. `/mcp` comes free with it. Health check: `GET /health` should report `catalog_skus` > 0 and a ledger head.

## Connect a client

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "praman": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp"]
    }
  }
}
```

Swap in `https://<brain>/mcp` for prod (or `…/mcp-sse` if the client struggles with streaming). Restart Claude Desktop, then ask: *"search Aether Audio for sweat-proof earbuds under ₹6,500, get an offer, and buy the bundle."*

**MCP Inspector** (visual test):

```bash
npx @modelcontextprotocol/inspector
```

Enter the `/mcp` URL, connect, and you should see the 4 tools.

**curl** (protocol handshake):

```bash
curl -X POST http://localhost:8000/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1.0\"}}}"
```

## Rules an agent should know

- Max 2 offers per session — state the full need in one `get_offer` call.
- Offers expire in 300s (`expires_in_seconds`); never reuse an old `offer_id`.
- `buy` without `idempotency_key` gets a random one — pass your own so a retry can't double-charge; same key + same args replays the original order.
- Gate tiers: 0 proceeds, 1 needs a signed mandate token, 2 waits for merchant approval (poll with `check_order`).
- Prices are whole rupees (INR). Refusals name the rule, e.g. `(refused by bound 3)` — adjust instead of retrying blindly.
- Every offer has an `audit_url`; the decision and any refusal are on the public hash-chained ledger.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Browser shows `Not Acceptable: Client must accept text/event-stream` | Expected — a browser GET isn't MCP. Use Inspector, curl above, or a real client |
| `mcp-remote` fails on Vercel streaming | Use the `…/mcp-sse` URL |
| `get_offer` keeps falling back / slow | Model endpoint failing → deterministic fallback answers; check model name/timeout env |
| Order stuck at `pending_merchant_approval` | Tier 2 — a human must approve/reject/counter in the dashboard; polling never advances it |
