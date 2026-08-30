# Issues — `vyapaari/` 

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


LLM proposer — zero payment authority, by design.

## High

| # | File | Issue |
|---|---|---|
| V1 | `vyapaari/gemini.py` latency | `GEMINI_TIMEOUT_SECONDS=12` > `LATENCY_BUDGETS_MS['offer']=3000ms` and `AGENT_WALL_CLOCK_SECONDS=2.5s` — single call can blow offer budget; fallback mitigates but p99 remains 12s |
| V2 | `vyapaari/prompt.py` injection defense | `BEGIN/END BUYER NEED` delimiters + "ignore instructions" text is minimal — LLM could still obey injected urgency; safety relies on downstream kernel veto (correct by design) but prompt could exfiltrate catalog via upsell prose |
| V3 | `vyapaari/proposer.py` duplicate | `_default_generator` defined twice (line ~167 and 375) — second shadows first — dead code, remove one |
| V4 | `vyapaari/gemini.py:_api_key` | Plain string, no `__repr__` masking — if client object logged, key material could appear in debug output. `settings.Secret` discipline bypassed here per comment — align or document why |

## Medium

| # | Issue |
|---|---|
| V5 | `vyapaari/envelope.py:_profit_ranked_attach` sorts by `margin_pct * attach_rate` using float — imprecision vs Decimal economics; tie-break uses catalog declared order, not deterministic SKU sort |
| V6 | `vyapaari/proposer.py:AGENT_SYSTEM_ADDENDUM` action protocol is text-JSON `{"action":"search_catalog"}` not native function calling — fragile; markdown-wrapped action fails parsing and consumes wall-clock budget |
| V7 | `vyapaari/proposer.py:propose_with_tools` swallows `SchemaError` silently (`pass`) to try action parsing — malformed proposal that is also malformed action burns tool-call budget without surfacing error |
| V8 | `vyapaari/schema.py:_parse_discount_pct` does `Decimal(str(value))` for float `0.30000000000000004` — str retains artefact, parsed Decimal carries it then rounds via kernel — normalize via `quantize` |
| V9 | AST import-boundary test checks static imports only — dynamic `__import__('kernel.payments')` / `importlib` bypass not caught |

## Low

- `vyapaari/tools.py` correctly read-only (`search_catalog`, `get_pairings`) — no side effects, good.
- `MAX_ATTEMPTS=2` fallback guarantee works — model outage degrades to `pick_base`.
