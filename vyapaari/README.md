# Vyapaari — LLM-Facing Sales Layer

> **Simple:** The creative side — the only place an LLM touches the system. It reads a sanitized catalog (no costs), talks to Gemini, and returns a *proposal* (SKUs + qty + discount + why). The proposal has no power until `kernel/offer.py` checks it. If the model fails twice, it falls back to a deterministic pick.

`vyapaari` is the intelligence and negotiation layer of PRAMAN (Aether Audio). It acts as the merchant-side sales agent, interfacing directly with LLMs (Google Gemini) to interpret autonomous buyer agent requests, explore product pairings, formulate base offers, and suggest contextual upsells and discounts.

## Simple — what each file does & its seam

| File | Plain job | Connected to |
|---|---|---|
| `envelope.py` | Joins public+private→`SellableSku` (exposes only `discount_headroom_pct`, not `cost`), profit-ranks attaches (`margin×rate`), `pick_base()` fallback | `store/catalog.py`, `store/db.py` (read-only) |
| `prompt.py` | Builds Gemini prompt: system rules + envelope + `BEGIN/END BUYER NEED` fencing + `assert_no_secrets_in_prompt()` | `settings.py`, `vyapaari/envelope.py` |
| `gemini.py` | Text-in/text-out Gemini wrapper, `response_mime_type="application/json"`, `LLMUnavailable` | Google GenAI API |
| `schema.py` | Strict parser: 1 base + ≤2 upsells (`bundle_attach/tier_upgrade/volume_break`), rejects unknown keys/markdown/duplicate SKU | `vyapaari/proposer.py` |
| `tools.py` | Read-only exploration: `search_catalog()`, `get_pairings()` | `vyapaari/envelope.py`, `store/pairings.py` |
| `proposer.py` | Orchestrates: agentic loop (≤tool calls, wall-clock) → one-shot → one retry → fallback | all above + `kernel/offer.py` consumer |
| `__init__.py` | Public exports, import-boundary warning | tests `test_import_boundary.py` |

---

## 🏛 Architectural Principles & Security Boundaries

In the PRAMAN architecture, data flows in one strict direction:

$$\text{vyapaari (Proposes)} \longrightarrow \text{kernel (Decides \& Vetoes)} \longrightarrow \text{store (Records Append-Only)}$$

1. **Propose-Only Separation**:
   - `vyapaari` **only proposes; it never decides, authorizes payments, or writes to the database**.
   - What leaves `vyapaari` is a *proposal* (a structured request containing SKUs, quantities, discount percentages, and justifications). It is **not** an offer or contract.
   - `vyapaari` does not pre-filter proposals against kernel bounds; ensuring that proposals reach the deterministic policy kernel keeps the kernel's veto mechanism continuously exercised and tested.

2. **Hard Import Boundary**:
   - Enforced by AST analysis in `tests/test_import_boundary.py`.
   - `vyapaari` cannot import `kernel.payments`, cannot access database write interfaces, and holds no payment credentials or secret environment variables.

3. **Zero Secrets in Prompts**:
   - All prompts are dynamically validated via `settings.assert_no_secrets_in_prompt()` before dispatching to any model transport.

4. **Graceful Degradation (Fallback Guarantee)**:
   - System resilience over perfection: If the LLM transport fails or returns invalid schemas repeatedly (`MAX_ATTEMPTS = 2`), `vyapaari` automatically falls back to a deterministic, base-item-only proposal chosen by token relevance and price.

---

## 🔄 The Data Pipeline

Data moves through `vyapaari` in a strict lifecycle:

```
[Public Catalog + Private Economics]
                 │
                 ▼
          envelope.py         --> Creates sanitized SellableSku without exposing cost/margins
                 │
                 ▼
           prompt.py          --> Prepares cache-friendly prompt; fences buyer text as untrusted data
                 │
                 ▼
       tools.py / gemini.py   --> Bounded catalog exploration & Gemini JSON generation
                 │
                 ▼
           schema.py          --> Validates and parses raw text into a strict Proposal dataclass
                 │
                 ▼
          proposer.py         --> Handles retries, repair prompts, or deterministic fallback
                 │
                 ▼
         [ProposalOutcome]    --> Passed to kernel/offer.py for policy bounds validation
```

---

## 📁 File-by-File Reference

### 1. `__init__.py`
Defines the public export interface for `vyapaari`. It exposes primary domain models (`Proposal`, `ProposalRequest`, `ProposedItem`, `ProposedUpsell`, `SellableSku`, `Attach`, `ProposalOutcome`), client classes (`GeminiClient`), error types (`LLMUnavailable`, `SchemaError`), constants (`MAX_UPSELLS`, `UPSELL_TYPES`, `SOURCE_*`), and top-level functions (`propose`, `is_configured`).

### 2. `envelope.py` — The Selling Envelope
Acts as the security and abstraction boundary between internal financial metrics and model-visible product data.
- **Joins Public and Private Data**: Combines public catalog rows with private merchant economics while stripping sensitive financial fields (`cost_inr`, `margin_pct`, `floor_price_inr`).
- **Exposes Bounds, Not Costs**: Emits `SellableSku` containing only `discount_headroom_pct`, live availability, returns window, and profit-ranked attachment candidates.
- **Profit-Ranked Attachments**: Pre-sorts upsell candidates by expected margin contribution ($\text{margin\_pct} \times \text{attach\_rate}$) in deterministic code behind the envelope seam before the LLM sees them.
- **Deterministic Base Selection (`pick_base`)**: A model-free fallback algorithm that selects the best-matching in-stock product based on token relevance, budget, and category constraints.

### 3. `gemini.py` — Model Transport Layer
A lightweight wrapper around Google GenAI's client (`google.genai`).
- **Strictly Text-In / Text-Out**: Decoupled from domain logic; does not parse proposals or check business rules.
- **Structured Output Config**: Enforces structured JSON output via `response_mime_type="application/json"` and `response_schema`.
- **Fault Categorization**: Distinguishes transport/network/auth failures (`LLMUnavailable`, which trigger immediate fallback) from schema/syntax errors (which trigger a repair retry).

### 4. `prompt.py` — Prompt Construction
Constructs prompt payloads sent to the LLM.
- **Prompt Caching Optimization**: Places static, invariant data (system instruction, catalog envelope, rules) at the beginning of the prompt to maximize provider prompt cache hits.
- **Prompt Injection Defense**: Untrusted buyer requirements are isolated within strict `BEGIN BUYER NEED` / `END BUYER NEED` delimiters and framed as data rather than instructions.
- **Secret Leak Verification**: Runs every assembled prompt through `settings.assert_no_secrets_in_prompt()` prior to return.
- **Repair Guidance**: Injects structured repair notes on retries to guide the model when correcting previous schema violations.

### 5. `schema.py` — Proposal Grammar & Strict Parser
Defines the single valid structural shape for proposals generated by the LLM.
- **Enforced Shapes**:
  - `Proposal`: Contains one `base` item and up to 2 `proposed_upsells`.
  - `ProposedItem` & `ProposedUpsell`: Strict schema requiring `sku`, `qty`, `discount_pct` (parsed as exact `Decimal`), and a concise explanation `why` ($\le 240$ chars).
  - Supported upsell types: `bundle_attach`, `tier_upgrade`, `volume_break`.
- **Strict Decoding**: Refuses unknown keys, markdown code fences, invalid UTF-8, duplicate SKUs, and non-numeric discounts with precise `SchemaError` exceptions indicating the exact field path.

### 6. `tools.py` — Agentic Catalog Exploration
Provides read-only exploration tools for interactive proposer sessions.
- **Tools Provided**:
  - `search_catalog(query)`: Deterministic keyword/token relevance search over in-stock envelope items.
  - `get_pairings(sku)`: Lookup historical buyer attachment patterns.
- **Safety**: Purely read-only computations operating strictly within the sanitized `envelope` context, preventing invented SKUs and side effects.

### 7. `proposer.py` — Proposal Orchestration & Fallback Engine
Coordinates the proposal generation workflow under tight latency and attempt budgets.
- **Execution Ladder**:
  1. *Agentic Exploration (Optional)*: Allows tool-based catalog search up to `AGENT_MAX_TOOL_CALLS` and wall-clock deadlines.
  2. *One-Shot Generation*: Invokes LLM with schema guidance.
  3. *Single Repair Retry*: If a `SchemaError` occurs, retries once with specific repair notes.
  4. *Deterministic Fallback*: If the model fails twice or is unavailable, generates a list-price fallback proposal via `envelope.pick_base`.
- **Audit Outcome (`ProposalOutcome`)**: Tracks proposal origin (`llm`, `llm_retry`, `llm_agent`, or `fallback`), latency in milliseconds, attempt counts, schema errors, and exploration trace for public audit ledgers.

---

## 🔒 Summary of Invariants

| Invariant | Implementation File | Verification |
| :--- | :--- | :--- |
| **No DB / Payment Access** | `envelope.py`, `proposer.py` | `tests/test_import_boundary.py` |
| **No Secret In Prompts** | `prompt.py`, `proposer.py` | `settings.assert_no_secrets_in_prompt` |
| **Exact Price/Discount Types** | `schema.py` | `Decimal` conversion, whole rupee logic |
| **Guaranteed Non-Empty Offer** | `proposer.py` | `pick_base` fallback ladder |
| **Max Upsells Capped at 2** | `schema.py`, `prompt.py` | `MAX_UPSELLS` validation |
