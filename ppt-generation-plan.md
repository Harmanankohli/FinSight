# PPT Generator Refactoring Plan

## Context

The current PPTX generator in `shared/report_generator.py` produces inconsistent, often empty output:
- **3-slide deck** when hitting the `response_text` (markdown) path — almost nothing extracted
- **8-slide deck** on the structured `InvestmentBrief` path — but Financial Performance & Valuation tables render empty, only 2/4 KPI chips populate, peer comparison never appears
- Hardcoded `_TICKER_NAMES` dict (25 tickers) means unknown tickers show raw symbols as company names
- Regex-based extraction from free-form LLM text is inherently unreliable across different stocks

### CRITICAL FINDING: Only `response_text` path is used in production

**All production data stored in the database is `{"response_text": "..."}`** — never a full structured `InvestmentBrief`.

Evidence:
- `agents/finsight_agent/agent.py` line 277: `_auto_save_brief()` only calls `store_minimal()`, which stores `{"response_text": text}`
- `store_brief()` (which stores structured `InvestmentBrief`) is only called in test fixtures
- Database inspection confirms: both WMT and NVDA briefs have `keys: ['response_text']`

**This means:** The markdown regex extraction path (`_enrich_from_markdown()`) is the PRIMARY code path, not a fallback. All improvements must prioritize this path. The structured `InvestmentBrief` path is aspirational/future code.

**Design reference:** `docs/ppt-generation template/Investment Deck - Walmart.html` — the gold standard for slide layout and styling.

**Goal:** A robust, data-driven pipeline that produces a polished 8-9 slide deck for ANY ticker from `response_text` markdown. Two outputs from one data source: Jinja2 HTML (browser preview) + python-pptx PPTX (editable PowerPoint).

---

## Phase 1: Data Layer — Robust `DeckData` Extraction

All downstream rendering (HTML + PPTX) depends on `DeckData` being fully populated. This phase makes extraction reliable for any ticker.

### 1.1: Dynamic Ticker Resolution via yfinance

**File:** `shared/report_generator.py`

**Delete:**
- `_TICKER_NAMES` dict (lines 207–235)
- `_extract_company_name()` function (lines 238–249)

**Create** `_resolve_ticker_info(ticker: str, text: str) -> tuple[str, str, str]`:

```python
_ticker_cache: dict[str, tuple[str, str, str]] = {}

_EXCHANGE_MAP = {
    "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ",
    "NYQ": "NYSE", "NYS": "NYSE", "PCX": "NYSE ARCA",
    "BTS": "CBOE", "LSE": "LSE", "TYO": "TSE",
}

def _resolve_ticker_info(ticker: str, text: str) -> tuple[str, str, str]:
    """Return (company_name, sector, exchange) for a ticker.
    
    Uses yfinance with in-memory cache. Falls back to regex from text,
    then to (ticker, "", "").
    """
    ticker = ticker.upper()
    if ticker in _ticker_cache:
        return _ticker_cache[ticker]

    # Try yfinance
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        name = info.get("longName") or info.get("shortName") or ""
        sector = info.get("sector") or ""
        raw_exchange = info.get("exchange") or ""
        exchange = _EXCHANGE_MAP.get(raw_exchange, raw_exchange)
        if name:
            result = (name, sector, exchange)
            _ticker_cache[ticker] = result
            return result
    except Exception:
        pass

    # Fallback: regex from text
    import re
    m = re.search(
        r"(?:for|about|of)\s+([A-Z][A-Za-z\s&.]+?)\s*\(" + re.escape(ticker) + r"\)",
        text,
    )
    if m:
        result = (m.group(1).strip(), "", "")
        _ticker_cache[ticker] = result
        return result

    m = re.search(r"([A-Z][A-Za-z\s&.]+?)\s*\(" + re.escape(ticker) + r"\)", text)
    if m:
        result = (m.group(1).strip(), "", "")
        _ticker_cache[ticker] = result
        return result

    return (ticker, "", "")
```

**Update caller at line 261:**
```python
# Old:
name, sector, exchange = _extract_company_name(ticker, response_text)
# New:
name, sector, exchange = _resolve_ticker_info(ticker, response_text)
```

### 1.2: Add `peer_comparison` to Data Models

**File:** `shared/models.py`

Add one field to `SentimentIntelligence` (line 63, after `key_catalysts`):
```python
peer_comparison: list[dict] = []
```

This is a **non-breaking change** — default `[]` means:
- Existing `SentimentIntelligence(...)` calls in tests (lines `test_models.py:40-47`, `test_ticker_memory.py:32-37`) still work — no `peer_comparison` arg needed
- Existing serialized JSON in SQLite is valid — Pydantic fills the default
- `InvestmentBrief.model_validate(data)` round-trips with or without the field

**File:** `agent_4_crewai/executor.py`

After line 95 (`result["_retrieved_contexts"] = ...`), add peer structuring:
```python
# Structure peer financials for report generator
peer_comparison = []
for sym, pdata in data.get("peers", {}).items():
    pinfo = (pdata.get("financials") or {}).get("info", {})
    if pinfo:
        metrics = {}
        if pinfo.get("revenueGrowth") is not None:
            metrics["Revenue Growth"] = f"{pinfo['revenueGrowth']*100:.1f}%"
        if pinfo.get("returnOnEquity") is not None:
            metrics["ROE"] = f"{pinfo['returnOnEquity']*100:.1f}%"
        if pinfo.get("operatingMargins") is not None:
            metrics["Operating Margin"] = f"{pinfo['operatingMargins']*100:.1f}%"
        if pinfo.get("trailingPE") is not None:
            metrics["P/E Ratio"] = f"{pinfo['trailingPE']:.1f}x"
        if metrics:
            peer_comparison.append({"ticker": sym, "metrics": metrics})
result["peer_comparison"] = peer_comparison[:3]
```

**Note on data flow:** This peer data flows as: executor → crew result dict → A2A response text → orchestrator LLM reads it. The structured `peer_comparison` list is NOT automatically forwarded through the A2A pipeline into the stored brief_json. The LLM may mention peers in its response_text, which the report generator must extract via regex. The `peer_comparison` structured field is for FUTURE use when the orchestrator is enhanced to store structured data.

### 1.3: Fix the Markdown Table Parsing Bug (CRITICAL)

**File:** `shared/report_generator.py`, function `_strip_markdown()` (lines 112-123)

**Bug:** Line 118 deletes ALL markdown table rows: `re.sub(r"^\|.+\|$", "", text, flags=re.MULTILINE)`. This destroys valuable data like peer comparison tables and financial metrics tables that the LLM commonly outputs.

**Fix:** Create a new function `_parse_markdown_tables(text: str) -> tuple[str, list[dict]]` that:
1. Finds markdown tables in the text
2. Parses them into structured dicts: `[{"header1": "val1", "header2": "val2"}, ...]`
3. Returns the text with tables removed AND the parsed table data

```python
def _parse_markdown_tables(text: str) -> tuple[str, list[dict]]:
    """Extract markdown tables into structured data, return cleaned text + parsed rows."""
    table_pattern = re.compile(
        r"((?:^\|.+\|\s*\n)+)",  # consecutive lines starting and ending with |
        re.MULTILINE,
    )
    parsed_tables: list[list[dict]] = []

    for match in table_pattern.finditer(text):
        block = match.group(1)
        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
        # Skip separator rows (|---|---|)
        data_lines = [l for l in lines if not re.match(r"^\|[\s\-:]+\|$", l)]
        if len(data_lines) < 2:
            continue
        # First data line is header
        headers = [c.strip() for c in data_lines[0].split("|")[1:-1]]
        rows = []
        for line in data_lines[1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
        if rows:
            parsed_tables.append(rows)

    # Remove tables from text
    cleaned = table_pattern.sub("", text)
    # Flatten all rows into a single list
    all_rows = [row for table in parsed_tables for row in table]
    return cleaned, all_rows
```

**Integrate into `_extract_deck_data()`** — replace the response_text path (lines 336-344):

**BEFORE (current code):**
```python
    # ── Minimal response_text path ────────────────────────────────────────────
    response_text = brief_data.get("response_text", "")
    if not response_text:
        data.executive_summary = "No analysis content available."
        return data

    sections = _parse_markdown_sections(response_text)

    # Try to extract structured data from the markdown text
    _enrich_from_markdown(data, response_text, sections)
```

**AFTER (new code):**
```python
    # ── Minimal response_text path ────────────────────────────────────────────
    response_text = brief_data.get("response_text", "")
    if not response_text:
        data.executive_summary = "No analysis content available."
        return data

    # Parse tables FIRST — before _strip_markdown destroys them
    cleaned_text, table_rows = _parse_markdown_tables(response_text)
    sections = _parse_markdown_sections(cleaned_text)

    # Try to extract structured data from the markdown text
    _enrich_from_markdown(data, cleaned_text, sections, table_rows=table_rows)
```

**Also update the fallback executive summary at line 351** to use `cleaned_text`:
```python
# Old (line 351):
data.executive_summary = _strip_markdown(response_text[:800])
# New:
data.executive_summary = _strip_markdown(cleaned_text[:800])
```

**Also update `_strip_markdown()` line 118** — remove the table-stripping regex since `_parse_markdown_tables()` already handles table removal:
```python
# DELETE this line from _strip_markdown():
text = re.sub(r"^\|.+\|$", "", text, flags=re.MULTILINE)
```

**Update `_enrich_from_markdown()` signature** to accept `table_rows: list[dict]`:
```python
def _enrich_from_markdown(data: DeckData, text: str, sections: list[Section], table_rows: list[dict] = None) -> None:
```

Use `table_rows` to populate financials, peer comparisons, and KPI data when available, BEFORE falling back to regex patterns.

### 1.4: Improve Markdown Regex Extraction

**File:** `shared/report_generator.py`, function `_enrich_from_markdown()` (lines 356–688)

**1.4a: Use parsed table data first**

At the top of `_enrich_from_markdown()`, process `table_rows`:
```python
if table_rows:
    for row in table_rows:
        # Each row is a dict like {"Metric": "Revenue Growth", "Current": "+7.3%", "YoY Change": "+7.3%"}
        # Or {"Metric": "Revenue Growth", "Walmart": "7.3%", "Costco": "21.5%"}
        metric = row.get("Metric") or row.get("metric") or ""
        # Try to use as financials data
        for val_key in ("Current", "Value", "current", "value"):
            if val_key in row:
                data.financials.append((metric, row[val_key], row.get("YoY Change", row.get("Context", ""))))
                break
        # Try to use as peer data (table has peer ticker columns)
        peer_cols = [k for k in row.keys() if k not in ("Metric", "metric", "") and k.upper() != data.ticker]
        if peer_cols and metric:
            # This is a peer comparison table
            for peer_name in peer_cols[:2]:
                if peer_name not in data.peer_names:
                    data.peer_names.append(peer_name)
```

**1.4b: Add more metric patterns to `metric_patterns` list (line 367):**

Add these after the existing patterns:
```python
("Debt/Equity", [
    r"(?:debt[/\\]equity|D/E)\s*[\(:=]\s*([+-]?\d+\.?\d*)",
], False),
("Dividend Yield", [
    r"dividend\s+yield\s*[\(:=]\s*([+-]?\d+\.?\d*)\s*%",
], True),
("EPS", [
    r"(?:diluted\s+)?EPS\s*[\(:=]\s*\$?\s*([+-]?\d+\.?\d*)",
], False),
("Current Ratio", [
    r"current\s+ratio\s*[\(:=]\s*([+-]?\d+\.?\d*)",
], False),
("Net Margin", [
    r"net\s+(?:profit\s+)?margin\s*[\(:=]\s*([+-]?\d+\.?\d*)\s*%",
], True),
```

**1.4c: Add more scorecard patterns (line 514):**

Add to the `scorecard_entries` list:
```python
("Momentum", [
    r"RSI\s*[\(:=]\s*(\d+)",  # RSI > 70 overbought, < 30 oversold
], {"overbought": "expensive", "oversold": "strong"}),
```
Special handling for RSI: extract the numeric value, classify:
- RSI > 70: "Overbought" → expensive
- RSI 50-70: "Bullish" → bullish
- RSI 30-50: "Neutral" → moderate
- RSI < 30: "Oversold" → strong

**1.4d: Improve peer extraction from narrative text:**

After the existing risk/opportunity extraction, add:
```python
# Extract peer mentions: "Costco (COST)" or "vs. COST"
peer_pattern = re.compile(
    r"([A-Z][A-Za-z\s]+?)\s*\(([A-Z]{2,5})\)",
)
if not data.peer_names:
    for m in peer_pattern.finditer(text):
        sym = m.group(2)
        if sym != data.ticker and sym not in data.peer_names:
            data.peer_names.append(sym)
            if len(data.peer_names) >= 2:
                break
```

### 1.5: Improve KPI Chip Selection Logic

**File:** `shared/report_generator.py`, within `_enrich_from_markdown()`

Currently (line 422): `list(extracted.items())[:4]` — just takes first 4 extracted metrics.

Replace with priority-based selection:
```python
_KPI_PRIORITY = [
    "Revenue Growth", "ROE", "Operating Margin", "P/E Ratio",
    "Sharpe Ratio", "Beta", "Dividend Yield", "RSI",
    "Volatility", "EPS", "Net Margin", "Debt/Equity", "Current Ratio",
]

selected = []
for label in _KPI_PRIORITY:
    if label in extracted and len(selected) < 4:
        selected.append(label)
# Fill remaining with any extracted metrics not in priority list
for label in extracted:
    if label not in selected and len(selected) < 4:
        selected.append(label)

for label in selected:
    val = extracted[label]
    is_pos = not val.startswith("-")
    data.kpi_chips.append({
        "label": label, "value": val,
        "context": _kpi_context.get(label, ""),
        "positive": is_pos,
    })
```

### 1.6: Unit Tests for Data Extraction

**File:** `tests/unit/test_deck_data_extraction.py` (new)

Follow existing test conventions: `tests/unit/` directory, `pytest`, no test classes.

```python
"""Tests for shared.report_generator data extraction."""
import pytest
from unittest.mock import patch, MagicMock

# Test _resolve_ticker_info
def test_resolve_ticker_yfinance_success():
    """yfinance returns valid data → use it."""
    mock_info = {
        "longName": "NVIDIA Corporation",
        "sector": "Technology",
        "exchange": "NMS",
    }
    with patch("yfinance.Ticker") as mock_yf:
        mock_yf.return_value.info = mock_info
        from shared.report_generator import _resolve_ticker_info, _ticker_cache
        _ticker_cache.clear()  # reset cache
        name, sector, exchange = _resolve_ticker_info("NVDA", "")
        assert name == "NVIDIA Corporation"
        assert sector == "Technology"
        assert exchange == "NASDAQ"  # NMS mapped to NASDAQ

def test_resolve_ticker_yfinance_fails():
    """yfinance raises → fall back to regex from text."""
    with patch("yfinance.Ticker", side_effect=Exception("network error")):
        from shared.report_generator import _resolve_ticker_info, _ticker_cache
        _ticker_cache.clear()
        name, sector, exchange = _resolve_ticker_info(
            "WMT", "Analysis of Walmart Inc. (WMT) shows..."
        )
        assert name == "Walmart Inc."

def test_resolve_ticker_total_fallback():
    """Both yfinance and regex fail → return ticker symbol."""
    with patch("yfinance.Ticker", side_effect=Exception("fail")):
        from shared.report_generator import _resolve_ticker_info, _ticker_cache
        _ticker_cache.clear()
        name, sector, exchange = _resolve_ticker_info("XYZ", "some random text")
        assert name == "XYZ"

# Test _extract_deck_data with response_text
def test_extract_response_text_wmt():
    """Real WMT response text → reasonable DeckData."""
    from shared.report_generator import _extract_deck_data
    brief = {"response_text": (
        "## Investment Recommendation: HOLD\n\n"
        "Walmart Inc. (WMT) shows strong revenue growth (+7.3% YoY), "
        "ROE of 24.1%, and operating margin of 4.2%.\n\n"
        "## Valuation\n"
        "P/E Ratio: 41.9x. Analyst price target: $145.25. "
        "DCF fair value: $73.77.\n\n"
        "## Key Risks\n"
        "- Premium valuation multiples\n"
        "- MACD bearish momentum signal\n"
        "- Competitive pressure from Costco (COST)\n"
    )}
    with patch("yfinance.Ticker") as mock_yf:
        mock_yf.return_value.info = {"longName": "Walmart Inc.", "sector": "Consumer Defensive", "exchange": "NYQ"}
        deck = _extract_deck_data(brief, "WMT", "HOLD", 0.58, "2026-06-06")

    assert deck.company_name == "Walmart Inc."
    assert deck.exchange == "NYSE"
    assert len(deck.kpi_chips) >= 2  # at minimum Revenue Growth and ROE
    assert len(deck.risks) >= 2
    assert deck.executive_summary  # non-empty

def test_extract_empty_data():
    """Empty brief → minimal deck, no crash."""
    from shared.report_generator import _extract_deck_data
    with patch("yfinance.Ticker", side_effect=Exception("fail")):
        deck = _extract_deck_data({}, "XYZ", "UNKNOWN", 0.0, "2026-01-01")
    assert deck.company_name == "XYZ"
    assert deck.executive_summary == "No analysis content available."

def test_parse_markdown_tables():
    """Markdown tables are extracted into structured dicts."""
    from shared.report_generator import _parse_markdown_tables
    text = (
        "Some intro text.\n\n"
        "| Metric | Current | YoY Change |\n"
        "|--------|---------|------------|\n"
        "| Revenue | $177.8B | +7.3% |\n"
        "| Operating Income | $7.5B | +5.6% |\n"
        "\nSome outro text."
    )
    cleaned, rows = _parse_markdown_tables(text)
    assert len(rows) == 2
    assert rows[0]["Metric"] == "Revenue"
    assert "| Revenue" not in cleaned  # tables removed from text
```

---

## Phase 2: Jinja2 HTML Template Engine

Generate a standalone HTML deck from `DeckData` — viewable in any browser, styled identically to the Walmart template.

### 2.1: Create Template Directory and Files

**Create directory:** `shared/templates/`

**File:** `shared/templates/investment_deck.html` (new)

Base this on `docs/ppt-generation template/Investment Deck - Walmart.html` (lines 1–661). The template should:

1. **Keep the full `<style>` block** (lines 9–358 of the reference) verbatim — it defines all CSS variables, components, and animations
2. **Keep the `<link>` to Google Fonts** (line 7) for DM Sans + DM Mono
3. **Embed `deck-stage.js` inline** at the bottom: `<script>{% include "deck-stage.js" %}</script>` — this makes the HTML fully self-contained (no need for a separate JS file)
4. Replace each `<section>` with Jinja2-templated version

**Template structure (abbreviated):**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Investment Recommendation | {{ deck.company_name }} ({{ deck.ticker }})</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:...&family=DM+Mono:...&display=swap" rel="stylesheet">
<style>deck-stage:not(:defined){visibility:hidden}</style>
<style>
  /* ... full CSS from reference template, unchanged ... */
</style>
</head>
<body>
<deck-stage width="1920" height="1080">

  <!-- SLIDE 1: TITLE — always shown -->
  <section class="dark" data-label="Title" style="padding:100px 120px;justify-content:space-between;">
    <div>
      <div class="slide-label light anim-up">Equity Research Report</div>
      <h1 class="slide-title light anim-up" style="font-size:72px;letter-spacing:-3px;margin-bottom:16px;">
        {{ deck.company_name }}
      </h1>
      <p class="slide-subtitle light anim-up" style="font-size:28px;">
        {{ deck.exchange }}: {{ deck.ticker }}{% if deck.sector %} · {{ deck.sector }}{% endif %}
      </p>
    </div>
    <div class="kpi-row anim-up" style="grid-template-columns:repeat(3,auto);gap:40px;justify-content:start;">
      <div style="display:flex;flex-direction:column;gap:4px;">
        <span style="font-size:14px;color:rgba(255,255,255,0.45);font-weight:500;">Recommendation</span>
        <span style="font-family:'DM Mono',monospace;font-size:36px;font-weight:500;color:{{ rec_colors[deck.recommendation] | default('var(--blue)') }};">{{ deck.recommendation }}</span>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;">
        <span style="font-size:14px;color:rgba(255,255,255,0.45);font-weight:500;">Confidence</span>
        <span style="font-family:'DM Mono',monospace;font-size:36px;font-weight:500;color:#fff;">{{ confidence_pct }}</span>
      </div>
      {% if deck.scenarios.get('base') %}
      <div style="display:flex;flex-direction:column;gap:4px;">
        <span style="font-size:14px;color:rgba(255,255,255,0.45);font-weight:500;">Median Target</span>
        <span style="font-family:'DM Mono',monospace;font-size:36px;font-weight:500;color:#fff;">{{ deck.scenarios['base'] }}</span>
      </div>
      {% endif %}
    </div>
    <div class="slide-footer"><span>© 2026 Institutional Equity Research</span><span>For informational purposes only</span></div>
  </section>

  <!-- SLIDE 2: KEY METRICS — conditional -->
  {% if deck.kpi_chips %}
  <section data-label="Key Metrics">
    <div class="slide-label anim-up">Key Metrics</div>
    <h2 class="slide-title anim-up" style="margin-bottom:48px;">Performance Snapshot</h2>
    <div class="kpi-row" style="grid-template-columns:repeat({{ deck.kpi_chips|length }},1fr);">
      {% for chip in deck.kpi_chips[:4] %}
      <div class="kpi-chip anim-up">
        <span class="label">{{ chip.label }}</span>
        <span class="value {{ 'pos' if chip.positive else 'neg' }}">{{ chip.value }}</span>
        <span class="context">{{ chip.context }}</span>
      </div>
      {% endfor %}
    </div>
  </section>
  {% endif %}

  <!-- SLIDE 3-9: Same pattern — {% if data %} ... {% endif %} -->
  <!-- Follow the exact HTML structure from the Walmart reference for each slide -->
  <!-- Slide 3: Thesis, Slide 4: Financials table, Slide 5: Valuation 2-col,  -->
  <!-- Slide 6: Scorecard grid, Slide 7: Peer table, Slide 8: Risk-reward,   -->
  <!-- Slide 9: Conclusion (always shown)                                      -->

</deck-stage>
<script>
/* deck-stage.js contents embedded inline */
{% include "deck-stage.js" %}
</script>
</body>
</html>
```

**Important implementation details for each conditional slide:**
- **Slide 4 (Financials):** `{% for metric, current, context in deck.financials[:8] %}` → generate `<tr>` rows
- **Slide 5 (Valuation):** Left column: valuation_table as `<table>`. Right column: `{% for label, value, color in scenario_cards %}` → generate scenario cards. The `scenario_cards` list should be pre-built in the template context.
- **Slide 6 (Scorecard):** `{% for dim, rating, badge_type in deck.scorecard[:6] %}` → each gets class `badge-{{ badge_type }}`
- **Slide 7 (Peers):** Only if `deck.peers and deck.peer_names`. Table headers: `["Metric", deck.ticker] + deck.peer_names`. Use `{{ peer_row.get("col" ~ loop.index0, "N/A") }}`
- **Slide 8 (Risk-Reward):** Two-column `rr-grid`. Left: `{% for opp in deck.opportunities[:5] %}`. Right: `{% for risk in deck.risks[:5] %}`

**File:** `shared/templates/deck-stage.js` (new — copy from `docs/ppt-generation template/deck-stage.js`)

Copy the file verbatim. It's 1818 lines. The Jinja2 `{% include %}` directive will embed it into the HTML template at render time.

### 2.2: Template Context Builder

**File:** `shared/report_generator.py`

Add function to convert `DeckData` to template-friendly context:

```python
def _deck_to_template_context(deck: DeckData) -> dict:
    """Build Jinja2 template context from DeckData."""
    rec_colors = {"BUY": "var(--green)", "HOLD": "var(--blue)", "SELL": "var(--red)"}
    confidence_pct = f"{deck.confidence:.0%}"

    # Build scenario cards list: [(label, value, css_color)]
    scenario_cards = []
    if deck.scenarios.get("bull"):
        scenario_cards.append(("Bull Case", deck.scenarios["bull"], "var(--green-dark)"))
    if deck.scenarios.get("base"):
        scenario_cards.append(("Base Case", deck.scenarios["base"], "var(--blue)"))
    if deck.scenarios.get("dcf"):
        scenario_cards.append(("DCF Fair Value", deck.scenarios["dcf"], "var(--amber)"))

    return {
        "deck": deck,
        "rec_colors": rec_colors,
        "confidence_pct": confidence_pct,
        "scenario_cards": scenario_cards,
    }
```

### 2.3: `generate_html()` Function

**File:** `shared/report_generator.py`

```python
_jinja_env: "jinja2.Environment | None" = None

def _get_jinja_env() -> "jinja2.Environment":
    global _jinja_env
    if _jinja_env is None:
        from pathlib import Path
        import jinja2
        template_dir = Path(__file__).parent / "templates"
        _jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            autoescape=True,
        )
    return _jinja_env

def generate_html(
    brief_data: dict,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
) -> str:
    """Generate a standalone HTML investment deck. Returns HTML string."""
    deck = _extract_deck_data(brief_data, ticker, recommendation, confidence, analysis_date)
    ctx = _deck_to_template_context(deck)
    template = _get_jinja_env().get_template("investment_deck.html")
    return template.render(**ctx)
```

**Note on `autoescape=True`:** This escapes HTML in template variables (e.g., `<`, `>`, `&`). For the executive summary and bullet text this is correct and prevents XSS. The CSS/JS are in the template itself, not in variables, so they're unaffected.

### 2.4: Wire HTML Generation into API

**File:** `agent_1_adk/api_routes.py`

**Line 153-156** — add HTML to content types:
```python
_REPORT_CONTENT_TYPES = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
}
```

**Line 167** — update import:
```python
from shared.report_generator import generate_pptx, generate_docx, generate_html
```

**Lines 176-186** — update `_build_report_response()` to handle HTML:
```python
if fmt == "html":
    html_str = generate_html(brief_data, ticker, recommendation or "UNKNOWN",
                             confidence or 0.0, analysis_date or "")
    safe_date = (analysis_date or "report").replace(":", "-")
    filename = f"FinSight_{ticker}_{safe_date}.html"
    return Response(
        content=html_str,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

generator = generate_pptx if fmt == "pptx" else generate_docx
# ... rest unchanged
```

**Lines 194 and 215** — update validation error messages in both `report_by_id()` and `report_latest()`:
```python
# Line 194 (report_by_id) — old:
return JSONResponse({"error": "format must be pptx or docx"}, status_code=400)
# Line 194 (report_by_id) — new:
return JSONResponse({"error": "format must be pptx, docx, or html"}, status_code=400)

# Line 215 (report_latest) — old:
return JSONResponse({"error": "format must be pptx or docx"}, status_code=400)
# Line 215 (report_latest) — new:
return JSONResponse({"error": "format must be pptx, docx, or html"}, status_code=400)
```

**File:** `agent_1_adk/agent.py`

**Line 183** — update docstring:
```
format: Report format — "pptx" (PowerPoint), "docx" (Word), or "html" (browser deck).
    Defaults to "pptx".
```

**Line 190-191** — update validation:
```python
if fmt not in ("pptx", "docx", "html"):
    return f"Invalid format '{format}'. Use 'pptx', 'docx', or 'html'."
```

**Lines 211-224** — update generator dispatch + file write:
```python
from shared.report_generator import generate_pptx, generate_docx, generate_html

if fmt == "html":
    content = generate_html(
        brief_data, ticker,
        latest.get("recommendation", "UNKNOWN"),
        latest.get("confidence") or 0.0,
        latest.get("analysis_date") or "",
    )
    analysis_date = (latest.get("analysis_date") or "report").replace(":", "-")
    filename = f"FinSight_{ticker}_{analysis_date}.html"
    filepath = _REPORTS_DIR / filename
    filepath.write_text(content, encoding="utf-8")
else:
    generator = generate_pptx if fmt == "pptx" else generate_docx
    buf = generator(
        brief_data, ticker,
        latest.get("recommendation", "UNKNOWN"),
        latest.get("confidence") or 0.0,
        latest.get("analysis_date") or "",
    )
    analysis_date = (latest.get("analysis_date") or "report").replace(":", "-")
    filename = f"FinSight_{ticker}_{analysis_date}.{fmt}"
    filepath = _REPORTS_DIR / filename
    filepath.write_bytes(buf.getvalue())

download_path = f"/reports/{filename}"
fmt_label = {"pptx": "PowerPoint", "docx": "Word", "html": "HTML Deck"}[fmt]
return (
    f"{fmt_label} report generated for {ticker}.\n"
    f"Download: {_HOST_PORT}{download_path}"
)
```

---

## Phase 3: Refactored PPTX Generation

Improve `generate_pptx()` to produce slides matching the HTML template quality, driven by the same `DeckData`.

### 3.1: Modularize Slide Generators

**File:** `shared/report_generator.py`

Extract each slide block from the monolithic `generate_pptx()` (lines 692–1228) into standalone functions. The current code is divided by comment blocks (`# SLIDE 1: TITLE`, `# SLIDE 2: KEY METRICS`, etc.) — each becomes its own function.

**Helper namespace:** Create at the top of `generate_pptx()` and pass to each slide function:
```python
from types import SimpleNamespace

h = SimpleNamespace(
    add_slide=add_slide,  # the local closure
    text=_text,
    multiline=_multiline,
    rounded_rect=_rounded_rect,
    kpi_chip=_kpi_chip,
    slide_label=_slide_label,
    slide_title=_slide_title_fn,  # renamed to avoid collision with _slide_title slide function
    footer=_footer,
    rgb=rgb,
    FONT=FONT, MONO=MONO,
    # pptx imports
    Inches=Inches, Pt=Pt, Emu=Emu,
    PP_ALIGN=PP_ALIGN, MSO_ANCHOR=MSO_ANCHOR,
    blank_layout=blank_layout,
    prs=prs,
)
```

**Each slide function pattern:**
```python
def _pptx_slide_title(deck: DeckData, h: SimpleNamespace) -> None:
    """Slide 1: Title (dark). Always rendered."""
    s = h.add_slide(dark=True)
    h.slide_label(s, "EQUITY RESEARCH REPORT", light=True)
    # ... rest of current slide 1 code (lines 812-839)
    h.footer(s, dark=True)
```

**`generate_pptx()` becomes:**
```python
def generate_pptx(...) -> BytesIO:
    # ... setup (Presentation, helpers, etc.) ...
    h = SimpleNamespace(...)

    _pptx_slide_title(deck, h)
    _pptx_slide_key_metrics(deck, h)
    _pptx_slide_thesis(deck, h)
    _pptx_slide_financials(deck, h)
    _pptx_slide_valuation(deck, h)
    _pptx_slide_scorecard(deck, h)
    _pptx_slide_peers(deck, h)
    _pptx_slide_risk_reward(deck, h)
    for section in deck.sections[:4]:
        if section.body:
            _pptx_slide_extra(deck, h, section)
    _pptx_slide_conclusion(deck, h)

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf
```

### 3.2: Fix Empty Slide Problem

Each slide function starts with a guard:
```python
def _pptx_slide_key_metrics(deck, h):
    if not deck.kpi_chips:
        return  # skip entire slide
    # ...

def _pptx_slide_financials(deck, h):
    if not deck.financials:
        return

def _pptx_slide_valuation(deck, h):
    if not deck.valuation_table and not deck.scenarios:
        return

def _pptx_slide_scorecard(deck, h):
    if len(deck.scorecard) <= 1:
        return  # just the "Recommendation" badge isn't a useful slide

def _pptx_slide_peers(deck, h):
    if not deck.peers or not deck.peer_names:
        return
```

### 3.3: Fix Layout for Variable Content

**KPI chips adaptive width:**
```python
chips = deck.kpi_chips[:4]
n = len(chips)
chip_w = {1: 5.0, 2: 4.0, 3: 3.3, 4: 2.7}.get(n, 2.7)
gap = 0.3
total_w = n * chip_w + (n - 1) * gap
start_x = (13.33 - total_w) / 2
```

**Scorecard adaptive columns:**
```python
items = deck.scorecard[:6]
n = len(items)
if n <= 3:
    cols = n
elif n == 4:
    cols = 2
else:
    cols = 3
```

**Bullet truncation in Risk-Reward:**
```python
for i, opp in enumerate(deck.opportunities[:5]):
    opp_text = (opp[:117] + "...") if len(opp) > 120 else opp
```

### 3.4: Title Slide Improvements

- Company name from `_resolve_ticker_info()` — already handled by Phase 1.1
- Add analysis date to subtitle: `f"{exchange_label}{sector_label} · {deck.analysis_date}"` (line 817-818)

### 3.5: Peer Comparison Slide Data Format

Ensure the `deck.peers` list structure matches what the existing slide code (lines 1049-1098) expects. The code at line 1081 accesses: `peer_row.get("metric", "")` and `peer_row.get(f"col{ci}", "N/A")`.

So `deck.peers` must be:
```python
[
    {"metric": "Revenue Growth", "col0": "7.3%", "col1": "21.5%", "col2": "N/A"},
    {"metric": "ROE", "col0": "24.1%", "col1": "29.2%", "col2": "N/A"},
    ...
]
```
Where `col0` is the primary ticker's value, `col1` is first peer, `col2` is second peer.

Build this in `_extract_deck_data()` or `_enrich_from_markdown()` from the parsed table data.

---

## Phase 4: DOCX Generator Compatibility (Verification Only)

The DOCX generator (`generate_docx()`, lines 1244–1447) shares `_extract_deck_data()` via `_extract_docx_content()` (line 1233). Phase 1 extraction improvements automatically apply.

**DOCX uses these DeckData fields:**
- `deck.executive_summary` — for the summary section
- `deck.kpi_chips` — for the Key Metrics table
- `deck.sections` — for analysis sections
- `deck.opportunities`, `deck.risks` — for Risk/Opportunity bullets
- `deck.disclaimer` — for the footer

**DOCX does NOT use:**
- `deck.financials`, `deck.valuation_table`, `deck.scenarios`, `deck.scorecard`, `deck.peers`

**No DOCX code changes needed.** Just run a regression test: generate a DOCX after all changes and verify in Word that it opens correctly with populated sections.

---

## Phase 5: Integration & Testing

### 5.1: End-to-End Wiring

Verify all three format paths work:
- `GET /api/briefs/{id}/report/pptx` → PPTX download
- `GET /api/briefs/{id}/report/docx` → DOCX download  
- `GET /api/briefs/{id}/report/html` → HTML download
- Agent tool: `generate_report(ticker="WMT", format="html")` → HTML file + download URL

### 5.2: Test with Real Database Data

Generate PPTX + HTML + DOCX for WMT and NVDA using the actual stored `{"response_text": "..."}` briefs:

**PPTX check:** 
- Company name shows "Walmart Inc." (not "WMT")
- At least 3 KPI chips populated (Revenue Growth, ROE, Operating Margin from the WMT text)
- Financials table has rows (not empty)
- Scorecard has 4+ items
- Risk-Reward has real bullets (not just "Market volatility" defaults)
- No slides with headers but empty content
- Conclusion slide has summary text

**HTML check:**
- Opens in browser, deck-stage navigation works (arrow keys, click left/right)
- All conditional slides present when data exists
- Styling matches the Walmart reference template
- Self-contained (no broken links to external resources except Google Fonts)

### 5.3: Test with Unknown Tickers

Use a ticker NOT in the old hardcoded dict (e.g., PLTR, COIN, TSM):
- yfinance resolution provides company name, sector, exchange
- Graceful fallback if yfinance call fails (mock network failure)

### 5.4: Edge Cases

- **Empty brief_data**: `{}` → minimal 3-slide deck (title + "No analysis" thesis + conclusion), no crash
- **Very long executive summary**: Truncation at 800 chars in `DeckData`, no overflow in PPTX text boxes
- **Non-standard recommendation**: "STRONG BUY" → normalized to "BUY" color mapping; "ACCUMULATE" → falls back to default blue
- **Unicode characters**: Company names with non-ASCII → no encoding errors
- **Markdown with tables**: LLM response with `| Metric | Value |` tables → tables parsed and used for financials/peers, not deleted

### 5.5: Existing Test Suite Regression

Run: `python -m pytest tests/ -v`

Key files:
- `tests/unit/test_models.py` — Pydantic model construction after `peer_comparison` field addition (should pass — default value)
- `tests/unit/memory/test_ticker_memory.py` — InvestmentBrief storage after model change (should pass)
- `tests/unit/test_deck_data_extraction.py` — new tests (should pass)

---

## File Change Summary

| File | Action | Description |
|------|--------|-------------|
| `shared/report_generator.py` | Major refactor | Remove hardcoded tickers, add yfinance resolution, fix table parsing bug, modularize PPTX slides, add `generate_html()`, improve extraction |
| `shared/models.py` | 1-line edit | Add `peer_comparison: list[dict] = []` to `SentimentIntelligence` (line 63) |
| `shared/templates/investment_deck.html` | New file | Jinja2 HTML template based on the Walmart deck reference |
| `shared/templates/deck-stage.js` | New file (copy) | Copy from `docs/ppt-generation template/deck-stage.js` |
| `agent_1_adk/api_routes.py` | ~20 lines changed | Add HTML format support to content types + `_build_report_response()` |
| `agent_1_adk/agent.py` | ~25 lines changed | Add HTML format to `generate_report` tool, update validation + file write |
| `agent_4_crewai/executor.py` | ~15 lines added | Structure peer financials into `peer_comparison` list after line 95 |
| `tests/unit/test_deck_data_extraction.py` | New file | Unit tests for ticker resolution, data extraction, table parsing |

**Files verified safe (NO changes needed):**
- `shared/memory/ticker_memory.py` — `peer_comparison` default makes model change transparent
- `shared/memory/store.py` — SQL schema unchanged; `brief_json` is schema-agnostic JSON
- `tests/unit/test_models.py` — existing tests pass with default field
- `tests/unit/memory/test_ticker_memory.py` — same reason

---

## Implementation Order

**Phase 1 → Phase 2 + Phase 3 (parallel) → Phase 4 → Phase 5**

1. **Phase 1** (data layer) — MOST CRITICAL. Fixes root cause of empty slides. Must be done first.
2. **Phase 2** (HTML template) — depends on Phase 1. Can run in parallel with Phase 3.
3. **Phase 3** (PPTX refactor) — depends on Phase 1. Can run in parallel with Phase 2.
4. **Phase 4** (DOCX verification) — quick check after Phase 1.
5. **Phase 5** (integration testing) — after all phases complete.
