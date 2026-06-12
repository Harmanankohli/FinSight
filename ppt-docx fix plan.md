# Plan: Fix Report Generation — Capture Agent Outputs + HTML-First + Playwright Export

## Context

Reports (PPTX/DOCX) are generated incorrectly because structured agent data is lost during persistence. Three sub-agents return rich structured JSON via A2A:

- **RAG** → `{ticker, summary, sources, confidence_score, context_texts}`
- **Quant** → `{ticker, metrics, dcf_valuation, monte_carlo, fundamentals, technicals, peer_comparison, recommendation, reasoning, ...}`
- **Market** → `{narrative, overall_signal, confidence_score, key_tailwinds, key_headwinds, peer_comparison}`

But `_store_memory()` in `agent_executor.py:479` only stores `{"response_text": "<LLM prose>"}` via `store_minimal()`. The extraction pipeline (`extraction.py:1320`) then regex-parses this prose — lossy, fragile, and incomplete. Meanwhile, the structured extraction path (`extraction.py:1124`) that handles `rag_insights`/`quant_metrics` keys **already exists and works** — it just never fires because nobody stores structured data.

The fix: capture agent outputs at the `send_message` tool level, store them alongside the synthesis, feed them to the extraction pipeline, and add Playwright-based HTML→PPTX/PDF conversion.

---

## Phase 1: Capture and Store Agent Outputs

### 1.1 Add response capture in `agent_1_adk/agent.py`

Add a module-level dict to store raw agent responses keyed by session, with timestamps for cleanup:

```python
import time
_agent_responses: dict[str, tuple[float, dict[str, dict]]] = {}
# Key: session_id → (timestamp, {agent_name: parsed_dict})
```

Modify `send_message()` (after line 57, before `return result`) to side-effect capture each agent's parsed response:

```python
session_id = tool_context.session.id if tool_context and tool_context.session else None
if session_id and result:
    ts, bucket = _agent_responses.get(session_id, (time.monotonic(), {}))
    # Parse JSON to avoid double-serialization when stored in brief_json
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        parsed = {"_raw_text": result[:5000]}
    bucket[resolved] = parsed
    _agent_responses[session_id] = (ts, bucket)
```

**Critical detail**: `SubAgentClient.send_message()` returns a JSON **string** from `_data_part_to_json()` (which calls `json.dumps(MessageToDict(part.data))`). We `json.loads()` it here to store parsed dicts — avoiding double-serialization when the whole `brief_json` is later `json.dumps()`-ed.

When the response is plain text (error cases, timeouts), the `json.loads` fails and we fall back to `{"_raw_text": text}`.

### 1.2 Add public accessor + cleanup in `agent_1_adk/agent.py`

```python
def pop_agent_responses(session_id: str) -> dict[str, dict]:
    """Pop and return captured agent responses for a session. Also sweeps stale entries."""
    # Defensive sweep: remove entries older than 5 minutes
    cutoff = time.monotonic() - 300
    stale = [k for k, (ts, _) in _agent_responses.items() if ts < cutoff]
    for k in stale:
        _agent_responses.pop(k, None)
    # Pop the requested session
    entry = _agent_responses.pop(session_id, None)
    return entry[1] if entry else {}
```

### 1.3 Modify `_store_memory()` in `agent_1_adk/agent_executor.py`

Import the accessor. In the first-time store path (line 529), fetch agent outputs and pass as `extra_data`:

```python
from agent_1_adk.agent import pop_agent_responses

# After ticker extraction (line 490), before the existing dedup check:
agent_outputs = pop_agent_responses(session_id)
```

Build the extra data dict with normalized keys:

```python
extra = {}
for agent_name, data in agent_outputs.items():
    name_lower = agent_name.lower()
    if "rag" in name_lower:
        # Truncate context_texts (can be very large RAG chunks)
        if "context_texts" in data:
            data["context_texts"] = [t[:500] for t in data["context_texts"][:3]]
        extra["rag_response"] = data
    elif "quant" in name_lower:
        extra["quant_response"] = data
    elif "market" in name_lower or "sentiment" in name_lower:
        extra["sentiment_response"] = data
```

Pass to `store_minimal()`:
```python
await tm.store_minimal(..., extra_data=extra if extra else None)
```

**Dedup path (lines 494-513)**: When a brief already exists today and the new `response_text` is longer, `update_response_text()` fires. This method reads existing `brief_json`, updates only `response_text`, and writes back — preserving any `rag_response`/`quant_response`/`sentiment_response` keys already stored. No change needed here.

**Edge case**: If `_store_memory` runs but `_agent_responses` was already popped (e.g., concurrent task), `pop_agent_responses()` returns `{}` — falls through to prose-only storage. Existing behavior, no regression.

### 1.4 Extend `TickerMemory.store_minimal()` in `shared/memory/ticker_memory.py`

Add `extra_data: dict | None = None` parameter. At line 88:

```python
brief_payload = {"response_text": response_text[:5000]}
if extra_data:
    brief_payload.update(extra_data)
json.dumps(brief_payload)
```

---

## Phase 2: Wire Extraction Pipeline to Agent Outputs

### 2.1 Add new extraction path in `shared/reports/extraction.py`

In `_extract_deck_data()`, between the structured InvestmentBrief path (line 1124) and the prose fallback path (line 1320), insert:

```python
# ── Raw agent outputs path ─────────────────────────────────────────
if any(k in brief_data for k in ("quant_response", "rag_response", "sentiment_response")):
    _populate_from_agent_outputs(data, brief_data, response_text)
    # If we got enough structured data, skip prose regex; otherwise fall through
    if data.kpi_chips and (data.financials or data.executive_summary):
        return data
```

### 2.2 Implement `_populate_from_agent_outputs()`

New function (~150 lines) that parses each agent's dict and populates `DeckData`. Each agent's data is accessed with `.get()` for safety:

**From `quant_response`** (dict with: `metrics`, `dcf_valuation`, `monte_carlo`, `fundamentals`, `technicals`, `peer_comparison`, `stress_test`, `recommendation`, `reasoning`):
- `metrics` dict → `kpi_chips` (sharpe_ratio, annual_volatility, beta)
- `metrics` dict → `financials` table (Beta, Sharpe, VaR)
- `dcf_valuation` → `valuation_table`, `scenarios["dcf"]`
- `monte_carlo` → `scenarios["bull"/"base"/"bear"]` (p90/p50/p10)
- `fundamentals` → `financials` table rows (P/E, ROE, margins etc.)
- `technicals` → `scorecard` ("Technical Outlook") + `kpi_chips` (RSI)
- `peer_comparison` → `peers`, `peer_names`
- `stress_test` → `valuation_table` (CVaR, max drawdown)
- `recommendation` → `scorecard` ("Quant Signal")

**From `rag_response`** (dict with: `ticker`, `summary`, `sources`, `confidence_score`, `context_texts`):
- `summary` → `executive_summary` (primary narrative)
- `sources` → `sections` ("Cited Sources") for provenance

**From `sentiment_response`** (dict with: `narrative`, `overall_signal`, `confidence_score`, `key_tailwinds`, `key_headwinds`, `peer_comparison`):
- `key_tailwinds` → `opportunities`
- `key_headwinds` → `risks`
- `peer_comparison` → supplement `peers` data (list of `{ticker, metrics}`)
- `narrative` → `sections` ("Market Narrative")
- `overall_signal` → `scorecard` ("Market Sentiment")

**Executive summary synthesis**: Combine Quant `reasoning`, RAG `summary`, and Market `narrative` into a cohesive `executive_summary` with the key data points.

**JSON-parse safety**: Each response value in `brief_data` could be a dict (normal) or a string (if stored from an older code path). Handle both:
```python
def _safe_parse(val):
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}
```

### 2.3 Backward compatibility

- Existing briefs with only `{"response_text": "..."}` → new path doesn't fire (no agent keys), falls through to existing prose regex. No regression.
- Existing regression tests pass with no changes — they test the prose path with `{"response_text": "..."}` inputs.
- The full `InvestmentBrief` path (line 1124, checks for `rag_insights`/`final_recommendation`) remains untouched — if anyone builds a full Pydantic brief, it still works.

---

## Phase 3: Playwright-Based Export (HTML → PPTX/PDF)

### 3.1 Add dependency

Add `playwright>=1.40.0` to `pyproject.toml` orchestrator dependency group. Post-install: `playwright install chromium`.

### 3.2 Create `shared/reports/playwright_export.py` (new file)

The `<deck-stage>` web component has built-in export support:
- **`noscale` attribute**: renders at authored 1920x1080 size (no viewport scaling) — "the PPTX exporter sets this so its DOM capture sees unscaled geometry" (deck-stage.js line 17)
- **`goTo(i)` public API** (line 1809): navigates to slide `i`, sets `data-deck-active`
- **Print mode** (line 620-625): `beforeprint` sets `data-deck-active` on ALL slides — one page per slide

Two async functions + sync wrappers:

**`async html_to_pptx(html: str) -> BytesIO`**:
1. Inject `noscale` attribute: `html = html.replace('<deck-stage ', '<deck-stage noscale ')`
2. Launch headless Chromium, viewport 1920x1080
3. `page.set_content(html, wait_until="networkidle")`
4. Wait for fonts: `page.wait_for_function("!document.querySelector('deck-stage')?.hasAttribute('data-fonts-pending')")` (deck-stage delays rendering until webfonts load)
5. Count slides: `total = await page.evaluate("document.querySelector('deck-stage')._slides.length")`
6. For each slide `i` in `range(total)`:
   - `await page.evaluate(f"document.querySelector('deck-stage').goTo({i})")`
   - `await page.wait_for_timeout(100)` (allow render/transition)
   - `section = await page.query_selector('section[data-deck-active]')`
   - `screenshot = await section.screenshot(type="png")`
7. Build PPTX with python-pptx: blank layout, 13.333" x 7.5", full-bleed PNG image per slide
8. Close browser, return BytesIO

**`async html_to_pdf(html: str) -> BytesIO`**:
1. Launch headless Chromium, viewport 1920x1080
2. `page.set_content(html, wait_until="networkidle")`
3. `await page.emulate_media(media="print")` — triggers deck-stage's `beforeprint` handler which sets `data-deck-active` on ALL slides
4. `buf = await page.pdf(landscape=True, width="1920px", height="1080px", print_background=True)` — deck-stage's `@media print` CSS lays each slide as its own page
5. Close browser, return BytesIO

**Sync wrappers** for regression tests and standalone use:
```python
def html_to_pptx_sync(html: str) -> BytesIO:
    return asyncio.run(html_to_pptx(html))
```

**Async/sync boundary**: `_build_report_response()` in `api_routes.py` is `async def`, so it can `await` the Playwright functions directly — no nested event loop issue. Sync wrappers are only for tests/CLI.

**Fallback**: If Playwright is not installed (`ImportError` or `launch()` raises), log a warning and fall back to the existing `pptx_renderer.generate_pptx()`. System doesn't break if browsers aren't available.

**Google Fonts**: The HTML template loads DM Sans/DM Mono from Google Fonts CDN. Playwright loads these in headless mode since `wait_until="networkidle"` waits for external resources. The deck-stage font-pending guard provides additional safety.

### 3.3 Update `shared/reports/__init__.py`

- Add `generate_pdf` and async variants (`generate_pptx_async`, `generate_pdf_async`) to public API
- `generate_pptx()` (sync): try Playwright sync wrapper first, fall back to existing renderer
- `generate_pptx_async()`: for use in async API routes — `await`s Playwright directly
- `generate_pdf_async()`: async-only (PDF has no legacy fallback)
- Keep `generate_docx` unchanged (benefits from structured data in Phase 2)

### 3.4 Update `api_routes.py:_build_report_response()`

Add `"pdf"` to `_REPORT_CONTENT_TYPES`:
```python
"pdf": "application/pdf",
```

Since `_build_report_response()` is `async def`, use async Playwright functions directly:
- `html` → `generate_html()`, return directly
- `pptx` → `generate_html()` then `await generate_pptx_async(html)` (fallback: existing sync renderer)
- `docx` → existing sync `generate_docx()` (now has structured data)
- `pdf` → `generate_html()` then `await generate_pdf_async(html)` (fallback: return HTML)

### 3.5 Update frontend — HTML as primary download

In `web/nextjs-app/app/research/page.tsx`:
- Expand format type: `"html" | "pptx" | "docx" | "pdf"`
- Show four download buttons: **HTML** (primary, first), PPTX, DOCX, PDF
- HTML button styled distinctly as the recommended/primary option
- For HTML download: use `text/html` blob type, `.html` extension

---

## Phase 4: Testing & Verification

1. **Existing regression tests** (`test_pptx_regression.py`, `test_html_regression.py`, `test_docx_regression.py`) must pass unchanged — they test prose-only `{"response_text": "..."}` inputs which still follow the existing path
2. **New test**: Add a test with structured agent data in `brief_data` confirming the new extraction path populates KPIs, financials, scenarios, peers, risks from real agent output shapes
3. **Integration test**: Run full system, query a ticker, verify:
   - `ticker_briefs.brief_json` contains `rag_response`, `quant_response`, `sentiment_response`
   - HTML report renders all slides with structured data
   - PPTX download produces clean screenshot-based slides
   - PDF download produces pixel-perfect document
4. **Edge cases**: 
   - Agent returns error text (not JSON) → captured as `{"_raw_text": "..."}`, extraction gracefully skips
   - Only 2 of 3 agents respond → partial structured data + prose fallback for missing
   - Playwright not installed → falls back to existing renderers

---

## Files to Modify

| File | Change |
|------|--------|
| `agent_1_adk/agent.py` | Add `_agent_responses` dict + capture in `send_message()` + `pop_agent_responses()` accessor |
| `agent_1_adk/agent_executor.py` | Import accessor, call in `_store_memory()`, pass `extra_data` to `store_minimal()` |
| `shared/memory/ticker_memory.py` | Add `extra_data` param to `store_minimal()` |
| `shared/reports/extraction.py` | Add `_populate_from_agent_outputs()` + wire into `_extract_deck_data()` |
| `shared/reports/playwright_export.py` | **New file** — async `html_to_pptx()`, `html_to_pdf()` + sync wrappers |
| `shared/reports/__init__.py` | Add `generate_pdf`, update `generate_pptx` to use Playwright with fallback |
| `agent_1_adk/api_routes.py` | Add `pdf` format, HTML-first flow |
| `web/nextjs-app/app/research/page.tsx` | Add HTML/PDF buttons, make HTML primary |
| `pyproject.toml` | Add `playwright` dependency |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| **Double JSON serialization** | `json.loads()` agent responses at capture time in `send_message()`, store as parsed dicts |
| **Agent response size** | Truncate RAG `context_texts` to 3 items x 500 chars. Quant/Market dicts are bounded (<10KB each) |
| **Playwright not installed** | Fallback to existing `pptx_renderer` / return HTML. Log warning, don't crash |
| **Agent returns plain text (errors/timeouts)** | `json.loads` wrapped in try/except → store as `{"_raw_text": "..."}`, extraction skips gracefully |
| **Concurrency** | `_agent_responses` keyed by session_id (unique per request). `pop()` is atomic in CPython |
| **Memory leak** | Defensive sweep removes entries older than 5 minutes on each `pop_agent_responses()` call |
| **`update_response_text()` preserves agent data** | Verified: reads full `brief_json`, updates only `response_text`, writes back. Existing agent keys persist |
| **Regression tests** | All existing tests use `{"response_text": "..."}` → new path doesn't fire. No test changes needed |
| **`save_brief` tool is dead code** | Defined in `agent.py` but NOT in `root_agent.tools=[send_message, load_memory]`. LLM never calls it. Only `_store_memory()` in executor stores briefs — that's our single interception point |
| **`deck-stage` Shadow DOM** | Slides are slotted children (not in shadow DOM), so `page.query_selector('section[data-deck-active]')` works. The `goTo(i)` API is on the host element |
| **Google Fonts in headless** | Template loads DM Sans/DM Mono from CDN. Playwright `wait_until="networkidle"` + deck-stage's `data-fonts-pending` guard ensure fonts load before screenshots |
