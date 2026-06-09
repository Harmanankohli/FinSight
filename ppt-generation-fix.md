# Plan: Fix PPT Generation — Executive Summary, Scenario Analysis, Peer Analysis

## Context

The PPTX generator in `shared/report_generator.py` has three broken/underperforming sections:
1. **Executive Summary** — renders at 16pt font (too small for the key narrative slide)
2. **Scenario Analysis** — scenarios dict rarely populated; slide often skipped entirely
3. **Peer Analysis** — structured path never reads peer_comparison data; slide often skipped

All three formats (PPTX, DOCX, HTML) share `_extract_deck_data()` for data extraction. HTML works because its template degrades gracefully; PPTX silently skips slides when data is missing.

**All changes are in a single file**: `shared/report_generator.py`.

### Data Flow Reality

Most briefs are stored via `store_minimal()` which saves `{"response_text": "..."}` only — the "minimal path" (line 350+). The "structured InvestmentBrief path" (line 290) is rare and only triggers for full `InvestmentBrief` serializations.

**Critical implication**: For scenarios and peers, the **regex/markdown extraction path is what actually matters**. The structured path extractions are future-proofing (harmless but rarely hit).

- `quant_metrics` → `QuantMetrics` model → does NOT include `peer_comparison` or `monte_carlo`
- `sentiment_intelligence` → `SentimentIntelligence` model → DOES include `peer_comparison: list[dict]`
- Monte Carlo values (p10/p50/p90) appear only in `response_text` as prose from `llm_summary_node`

---

## Issue 1: Executive Summary Too Small

**Root cause**: Font size is 16pt (compare: HTML uses 24px). Content truncated to 800 chars.

### Changes

**1a. Increase character caps from 800 → 1200** (3 locations):

| Line | Current | New |
|------|---------|-----|
| 292 | `rationale[:800]` | `rationale[:1200]` |
| 365 | `(first.body or first.title)[:800]` | `(first.body or first.title)[:1200]` |
| 368 | `_strip_markdown(cleaned_text[:800])` | `_strip_markdown(cleaned_text[:1200])` |

> **Overflow note**: At 20pt with word-wrap in a 3.6-inch text area, ~900-1000 chars fit cleanly. For the rare summary exceeding that, python-pptx silently clips the overflow. Real-world summaries are 200-500 chars, so this is low risk. The `_MAX_SLIDE_CHARS = 1200` constant on line 70 already exists for extra-slide chunks.

**1b. Increase font and box size in `_pptx_slide_thesis`** (lines 908-915):

| Line | Current | New |
|------|---------|-----|
| 908 | `thesis_top = 2.6` | `thesis_top = 2.4` |
| 909 | `thesis_h = 3.5` | `thesis_h = 4.2` |
| 915 | `deck.executive_summary, 16, color=_TEXT` | `deck.executive_summary, 20, color=_TEXT` |

The `thesis_h` variable propagates to the border shape (line 910) and rounded rect (line 914) automatically.

---

## Issue 2: Scenario Analysis Not Generating

**Root cause**: `deck.scenarios` dict is only populated from (a) `quant_metrics.dcf_intrinsic_value` → `dcf`, (b) `sentiment_intelligence.avg_price_target` → `base`, and (c) a fragile bull-case regex. No Monte Carlo extraction. No bear case at all.

### Changes

**2a. Structured path — extract Monte Carlo scenarios** (insert after line 325, before line 327):

> **Note**: This is future-proofing. Currently `qm.get("monte_carlo")` and `brief_data.get("monte_carlo")` will be None because `QuantMetrics` doesn't include MC data and the orchestrator doesn't pass it through. The MC data reaches the report only via `response_text` (handled in 2c). Adding this extraction is harmless and will activate if the pipeline is updated to pass MC data through.

```python
        # Monte Carlo scenarios (future-proofing — currently flows via response_text)
        mc = qm.get("monte_carlo") or brief_data.get("monte_carlo") or {}
        if mc:
            if mc.get("p90") and "bull" not in data.scenarios:
                data.scenarios["bull"] = _fmt_dollar(mc["p90"])
                data.valuation_table.append(("Bull Case (MC p90)", _fmt_dollar(mc["p90"])))
            if mc.get("p50") and "base" not in data.scenarios:
                data.scenarios["base"] = _fmt_dollar(mc["p50"])
                data.valuation_table.append(("Base Case (MC p50)", _fmt_dollar(mc["p50"])))
            if mc.get("p10"):
                data.scenarios["bear"] = _fmt_dollar(mc["p10"])
                data.valuation_table.append(("Bear Case (MC p10)", _fmt_dollar(mc["p10"])))
```

**2b. Markdown path — add bear case regex** (insert after line 555, after the existing bull case block):

```python
    bear_m = re.search(r"bear\s+case[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if bear_m:
        data.valuation_table.append(("Bear Case Target", f"${bear_m.group(1)}"))
        data.scenarios["bear"] = f"${bear_m.group(1)}"
```

**2c. Markdown path — add Monte Carlo percentile regexes** (insert after 2b):

This is the **primary fix** for scenario data. The quant agent's `llm_summary_node` includes MC values in the response_text as `"p10=$X, p50=$Y, p90=$Z"` or `"90th percentile: $X"` style prose.

```python
    # Monte Carlo percentile extraction from LLM text
    mc_p90_pats = [
        r"p90\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"90th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"bull(?:ish)?\s+(?:scenario|outcome)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "bull" not in data.scenarios:
        for pat in mc_p90_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                data.valuation_table.append(("Bull Case (p90)", f"${m.group(1)}"))
                data.scenarios["bull"] = f"${m.group(1)}"
                break

    mc_p50_pats = [
        r"p50\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"50th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"median\s+(?:outcome|price|target|value)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "base" not in data.scenarios:
        for pat in mc_p50_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                data.valuation_table.append(("Base Case (p50)", f"${m.group(1)}"))
                data.scenarios["base"] = f"${m.group(1)}"
                break

    mc_p10_pats = [
        r"p10\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"10th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"bear(?:ish)?\s+(?:scenario|outcome)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "bear" not in data.scenarios:
        for pat in mc_p10_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                data.valuation_table.append(("Bear Case (p10)", f"${m.group(1)}"))
                data.scenarios["bear"] = f"${m.group(1)}"
                break
```

**2d. Add bear case card to PPTX slide** (lines 1012-1018 in `_pptx_slide_valuation`):

After the existing `dcf` entry, add:
```python
    if deck.scenarios.get("bear"):
        scenario_list.append(("Bear Case", deck.scenarios["bear"], _RED))
```

> **Layout check**: 4 scenario cards (bull+base+dcf+bear) at card_h=0.95, gap=0.2 → bottom edge at y=6.80. Footer at y=7.0. Fits with 0.2-inch margin.

**2e. Add bear case to HTML template context** (line ~1593 in `_deck_to_template_context`):

After the `dcf` scenario_cards entry, add:
```python
    if deck.scenarios.get("bear"):
        scenario_cards.append(("Bear Case", deck.scenarios["bear"], "var(--red)"))
```

---

## Issue 3: Peer Analysis Not Generating

**Root cause**: Structured path (lines 289-348) **never reads peer_comparison data**. The markdown path extracts peer *names* via regex but often fails to find metric *data*. Both `deck.peers` AND `deck.peer_names` must be non-empty or the slide is skipped entirely.

### Changes

**3a. Structured path — extract from sentiment peer data** (insert after line 340, before "Extra sections"):

> **Note on quant path**: `qm.get("peer_comparison")` will be None because `QuantMetrics` model doesn't have this field and `format_output_node` doesn't return it. The `or brief_data.get("peer_comparison") or {}` fallback also resolves to `{}`. This is harmless future-proofing. The **sentiment path** (`si.get("peer_comparison")`) is the one that works — `SentimentIntelligence` model has `peer_comparison: list[dict]` and the CrewAI executor populates it with pre-formatted metrics like `{"ticker": "AAPL", "metrics": {"Revenue Growth": "8.5%", "P/E Ratio": "32.1x"}}`.

```python
        # ── Peer comparison from structured data ──
        # Quant agent path (future-proofing — currently doesn't flow through)
        qm_peers = qm.get("peer_comparison") or brief_data.get("peer_comparison") or {}
        if isinstance(qm_peers, dict) and qm_peers.get("comparison") and qm_peers.get("peers"):
            peer_tickers = [p for p in qm_peers["peers"] if p != data.ticker][:2]
            data.peer_names = peer_tickers
            comparison = qm_peers["comparison"]
            # Per-metric formatting: fractions → percentage, absolutes → decimal
            _pct_metrics = {"rev_growth", "op_margin", "roe"}
            metric_labels = [
                ("pe", "P/E Ratio"), ("ev_ebitda", "EV/EBITDA"),
                ("rev_growth", "Revenue Growth"), ("op_margin", "Operating Margin"),
                ("roe", "ROE"), ("debt_to_equity", "D/E Ratio"),
            ]
            ticker_data = comparison.get(data.ticker, {})
            for key, label in metric_labels:
                row = {"metric": label}
                tv = ticker_data.get(key)
                if isinstance(tv, (int, float)):
                    row["col0"] = f"{tv * 100:.1f}%" if key in _pct_metrics else f"{tv:.1f}"
                else:
                    row["col0"] = "N/A"
                for ci, pt in enumerate(peer_tickers):
                    pv = comparison.get(pt, {}).get(key)
                    if isinstance(pv, (int, float)):
                        row[f"col{ci + 1}"] = f"{pv * 100:.1f}%" if key in _pct_metrics else f"{pv:.1f}"
                    else:
                        row[f"col{ci + 1}"] = "N/A"
                data.peers.append(row)

        # Sentiment agent path (CrewAI — pre-formatted string metrics)
        if not data.peers and si.get("peer_comparison"):
            si_peers = si["peer_comparison"]
            if isinstance(si_peers, list) and si_peers:
                for pc_entry in si_peers[:2]:
                    sym = pc_entry.get("ticker", "")
                    if sym and sym != data.ticker and sym not in data.peer_names:
                        data.peer_names.append(sym)
                all_metrics_keys = set()
                for pc_entry in si_peers[:2]:
                    all_metrics_keys.update((pc_entry.get("metrics") or {}).keys())
                for metric_name in sorted(all_metrics_keys):
                    row = {"metric": metric_name, "col0": "N/A"}
                    for ci, pc_entry in enumerate(si_peers[:2]):
                        val = (pc_entry.get("metrics") or {}).get(metric_name, "N/A")
                        row[f"col{ci + 1}"] = str(val)
                    data.peers.append(row)
```

**3b. Update `_pptx_slide_peers` guard for partial data** (lines 1068-1073):

Change the early-return guard from `if not deck.peers or not deck.peer_names` to just `if not deck.peer_names`. Then add a fallback rendering when names exist but metrics don't:

```python
def _pptx_slide_peers(deck: DeckData, h: SimpleNamespace) -> None:
    if not deck.peer_names:
        return
    s = h.add_slide()
    h.slide_label(s, "COMPETITIVE LANDSCAPE")
    h.slide_title(s, "Peer Comparison")
    if not deck.peers:
        peer_text = f"Identified peers: {', '.join(deck.peer_names[:5])}"
        h.rounded_rect(s, 0.83, 2.8, 11.5, 1.5, _SURFACE)
        h.text(s, 1.2, 3.0, 10.8, 1.0, peer_text, 18, color=_TEXT, font=h.FONT)
        h.text(s, 1.2, 3.8, 10.8, 0.5, "Detailed metrics not available for this analysis.",
               14, color=_TEXT_LIGHT, font=h.FONT)
        h.footer(s)
        return
    # ... existing table rendering continues unchanged from current line 1074
```

> **HTML note**: The HTML template guard at line 500 (`{% if deck.peers and deck.peer_names %}`) stays unchanged — only PPTX gets the simplified fallback. HTML has no equivalent simplified rendering path and would show an empty table otherwise.

---

## What Does NOT Change

- **`DeckData` dataclass** — no new fields added
- **HTML template** (`shared/templates/investment_deck.html`) — no changes needed (bear case flows through `scenario_cards` automatically)
- **DOCX generator** — uses same `_extract_deck_data` for improved extraction, but renders only exec summary, key metrics, analysis sections, risks/opportunities. No DOCX-specific peer/scenario slides exist, so no breakage.
- **Existing slide rendering code** for all other slides (title, key metrics, financials, scorecard, risk-reward, conclusion)
- **`_parse_markdown_sections`**, **`_parse_markdown_tables`**, **`_extract_bullets`** — untouched

---

## Verification

### 1. Run existing tests
```
python -m pytest tests/regression/test_pptx_regression.py -v
```
All 7 tests must pass. Expected behavioral changes:
- **`test_pptx_generates_valid_output`** (WMT): Gets one additional slide (simplified peer slide with "Identified peers: COST, AMZN"). Test checks `≥6 slides` → still passes.
- **`test_pptx_very_long_summary`**: 3000 "A"s truncated to 1200 instead of 800. Font now 20pt. Test only checks `len(data) > 2000` → still passes.
- **`test_pptx_with_markdown_tables`**: Pre-existing behavior where "Current"/"YoY Change" columns are misidentified as peer names. No change — both `peers` and `peer_names` are populated from the table, so the full table peer slide renders as before.
- **All other tests**: No behavioral change — empty/minimal briefs still produce expected output.

### 2. Manual verification
Generate a PPTX for a real ticker and visually check:
- Executive Summary slide: font visibly larger (20pt), more content visible
- Scenario Analysis slide: bull/base/bear/dcf cards render when data is available
- Peer Comparison slide: table renders with structured data OR simplified "Identified peers" fallback

### 3. Cross-format consistency
Generate HTML for the same ticker and verify:
- Bear case scenario card appears if data is available
- Peer section still renders when both peers and peer_names are populated

---

## Summary of All Edits

| Location | Change | Issue | Notes |
|----------|--------|-------|-------|
| Line 292 | `[:800]` → `[:1200]` | 1 | |
| Line 365 | `[:800]` → `[:1200]` | 1 | |
| Line 368 | `[:800]` → `[:1200]` | 1 | |
| Line 908 | `thesis_top = 2.6` → `2.4` | 1 | |
| Line 909 | `thesis_h = 3.5` → `4.2` | 1 | |
| Line 915 | Font `16` → `20` | 1 | |
| After line 325 | Insert MC structured extraction (~10 lines) | 2 | Future-proofing; currently dead path |
| After line 340 | Insert peer structured extraction (~30 lines) | 3 | Quant path = future-proofing; sentiment path works now |
| After line 555 | Insert bear regex + MC percentile regexes (~35 lines) | 2 | **Primary fix** for scenario data |
| Lines 1012-1018 | Add bear case to scenario_list | 2 | |
| Lines 1068-1073 | Relax guard + add partial-data peer rendering | 3 | Only PPTX; HTML guard unchanged |
| Line ~1593 | Add bear case to `_deck_to_template_context` | 2 | |

## Critical Files

- **Edit**: `shared/report_generator.py` — single file, all changes
- **Test**: `tests/regression/test_pptx_regression.py` — must still pass (read-only)
- **Reference** (read-only, for data shapes):
  - `shared/models.py` — `QuantMetrics`, `SentimentIntelligence` field names
  - `agent_3_langgraph/nodes.py` — `peer_comparison_node` output shape, `_run_monte_carlo` output shape
  - `agent_4_crewai/executor.py` — CrewAI `peer_comparison` format (pre-formatted strings)
  - `shared/memory/ticker_memory.py` — `store_minimal` saves only `response_text`
