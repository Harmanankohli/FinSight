# Plan: Integrate yahooquery Alongside yfinance

## Context

All market data in FinSight currently flows through yfinance. This works but has key limitations:

1. **`get_macro_indicators`** makes 15 sequential yfinance calls (4 macro tickers + 11 sector ETFs), each acquiring the rate limiter separately — slow and fragile.
2. **No analyst upgrade/downgrade history** — `analyst_positioning_node` only gets a consensus snapshot from `yfinance.Ticker().info` (recommendationKey, targetMeanPrice). No firm names, no grade changes, no price target revisions.
3. **No forward EPS estimates or revision momentum** — `get_earnings_history` exists but is NOT consumed by any agent node. The quant agent approximates "earnings surprise" as `(forwardEps - trailingEps) / trailingEps` which is crude.
4. **No historical valuation context** — current P/E, EV/EBITDA come from a point-in-time `.info` snapshot. Agents can't say "P/E is above its 2-year average."

yahooquery complements yfinance with batch fetching, `grading_history`, `earnings_trend`, and `valuation_measures`. This plan adds it as a secondary data source without replacing yfinance.

---

## Pass 1: Foundation — Dependency & Infrastructure

### Phase 1.1: Add yahooquery dependency

**Files to modify:**
- `pyproject.toml`

**Changes:**
- Add `"yahooquery>=2.4.0"` to the `dependencies` list (line ~33, alongside `yfinance>=1.4.1`)
- Add `"yahooquery>=2.4.0"` to `[project.optional-dependencies]` `mcp` group (line ~166) and `quant` group (line ~235) if they list yfinance

**Verification:** `uv sync && uv run python -c "from yahooquery import Ticker; print('ok')"`

---

### Phase 1.2: Add rate limiter and caches for yahooquery

**Files to modify:**
- `src/mcp_tools/infra/rate_limiters.py`

**Changes:**
Add after `_YF_LIMITER`:
```python
_YQ_LIMITER = TokenBucket(rate=4, burst=8)  # yahooquery: separate bucket from yfinance
```

Add new cache instances after existing caches:
```python
cache_analyst_activity = make_cache(3600, "analyst_activity")   # 1 hr — analyst grades change slowly
cache_valuation_ts = make_cache(86400, "valuation_ts")          # 24 hr — quarterly data, very stable
cache_earnings_trend = make_cache(3600, "earnings_trend")       # 1 hr — forward estimates
```

**Verification:** `uv run python -c "from mcp_tools.infra.rate_limiters import _YQ_LIMITER, cache_analyst_activity; print('ok')"`

---

## Pass 2: Batch `get_macro_indicators` (Highest Impact)

### Phase 2.1: Rewrite `_get_macro_impl` to use yahooquery batch fetch

**File to modify:**
- `src/mcp_tools/tools/market_data.py`

**Current behavior (lines 173-213):**
- Loops over 4 `_MACRO_TICKERS` + 11 `_SECTOR_ETFS` = 15 sequential `yf.Ticker(sym).history(period="1mo")` calls
- Each call acquires `_YF_LIMITER` individually (only once at top — actually a bug, should acquire per call)
- Computes latest value, 5d change for macro; 21d (1mo) return for sectors

**New behavior:**
- Batch all 15 symbols into one `yahooquery.Ticker([...]).history(period="1mo")` call
- Fall back to current yfinance sequential approach if yahooquery fails
- Same output schema — no downstream changes needed

**Detailed changes:**

1. Add `_YQ_LIMITER` to the existing rate_limiters import:
```python
from mcp_tools.infra.rate_limiters import _YQ_LIMITER  # add to existing import line
```

2. Do NOT add `from yahooquery import ...` at module top level (see Risk 13). Use lazy import inside the function body.

3. Rewrite `_get_macro_impl()`:

```python
async def _get_macro_impl() -> dict:
    all_symbols = list(_MACRO_TICKERS.values()) + list(_SECTOR_ETFS.values())
    loop = asyncio.get_event_loop()

    # --- Primary: yahooquery batch (1 HTTP call for all 15 tickers) ---
    try:
        from yahooquery import Ticker as YQTicker  # lazy import (Risk 13)
        await _YQ_LIMITER.acquire()
        all_hist = await loop.run_in_executor(
            None, lambda: YQTicker(all_symbols).history(period="1mo")
        )
        if isinstance(all_hist, dict) and any("error" in str(v).lower() for v in all_hist.values()):
            raise ValueError("yahooquery returned errors for some symbols")
    except Exception as exc:
        logger.warning("yahooquery batch macro fetch failed, falling back to yfinance: %s", exc)
        return await _get_macro_impl_yfinance()  # renamed original

    result: dict = {"macro": {}, "sectors": {}}

    available_symbols = set(all_hist.index.get_level_values("symbol").unique())

    # Process macro tickers
    for key, sym in _MACRO_TICKERS.items():
        try:
            if sym not in available_symbols:
                continue
            hist = all_hist.xs(sym, level="symbol")
            if hist.empty:
                continue
            closes = hist["close"].sort_index()
            latest = float(closes.iloc[-1])
            prev = float(closes.iloc[-5]) if len(closes) >= 5 else latest
            result["macro"][key] = {
                "value": round(latest, 3),
                "change_5d_pct": round((latest - prev) / prev * 100, 2) if prev else 0,
            }
        except Exception as exc:
            result["macro"][key] = {"error": str(exc)}

    # Process sector ETFs
    for name, sym in _SECTOR_ETFS.items():
        try:
            if sym not in available_symbols:
                continue
            hist = all_hist.xs(sym, level="symbol")
            if hist.empty:
                continue
            closes = hist["close"].sort_index()
            latest = float(closes.iloc[-1])
            prev = float(closes.iloc[-21]) if len(closes) >= 21 else latest
            result["sectors"][name] = round((latest - prev) / prev * 100, 2) if prev else 0
        except Exception:
            continue

    # Yield curve spread
    macro = result["macro"]
    if "us10y" in macro and "us2y" in macro:
        v10 = macro["us10y"].get("value", 0)
        v2 = macro["us2y"].get("value", 0)
        spread = v10 - v2
        result["macro"]["yield_curve_spread"] = round(spread, 3)
        result["macro"]["regime"] = (
            "inverted" if spread < 0 else "flat" if spread < 0.5 else "normal"
        )
    return result
```

4. Rename current `_get_macro_impl` to `_get_macro_impl_yfinance` (keep as fallback):
```python
async def _get_macro_impl_yfinance() -> dict:
    # ... exact current implementation unchanged ...
```

**Key detail:** yahooquery returns a MultiIndex DataFrame `(symbol, date)` with lowercase column names (`close` not `Close`). The agent must handle this difference vs yfinance's `Close`.

**Return schema:** Identical to current — `{"macro": {...}, "sectors": {...}}`. No downstream changes.

**Verification:**
- Run `uv run python -c "from mcp_tools.tools.market_data import get_macro_indicators; import asyncio; print(asyncio.run(get_macro_indicators()))"`
- Verify output has same keys: `macro.us10y.value`, `macro.us10y.change_5d_pct`, `sectors.tech`, etc.
- Test fallback by temporarily making yahooquery import fail

---

### Phase 2.2: Update `_yf_ticker_mock` and add characterization test for macro

**First, update `_yf_ticker_mock` in `test_mcp_tool_shapes.py`** to set `mock.earnings_dates` as a proper DataFrame property (not just `get_earnings_dates` method), since new tests will need it:

```python
mock.earnings_dates = pd.DataFrame()  # add alongside existing mock.get_earnings_dates line
```

Then add the macro batch test:

**File to modify:**
- `src/tests/characterization/test_mcp_tool_shapes.py`

**Changes:**
- Add a `_yq_ticker_mock` function similar to `_yf_ticker_mock` but for yahooquery
- The existing macro test should still pass because the function falls back to yfinance when yahooquery fails
- Add an explicit test for the yahooquery batch path by mocking `yahooquery.Ticker`:

```python
def _yq_batch_mock(symbols):
    """Mock for yahooquery.Ticker([...]) with .history() returning MultiIndex DataFrame.
    
    yahooquery returns lowercase columns (close, open, high, low, volume, adjclose)
    with a (symbol, date) MultiIndex — different from yfinance's capitalized columns.
    """
    mock = MagicMock()
    rows = []
    for sym in (symbols if isinstance(symbols, list) else [symbols]):
        for dt in pd.date_range("2024-01-01", periods=22, freq="B"):
            rows.append({"symbol": sym, "date": dt, "close": 100.0, "open": 99.0,
                         "high": 101.0, "low": 98.0, "volume": 1000, "adjclose": 100.0})
    df = pd.DataFrame(rows).set_index(["symbol", "date"])
    mock.history.return_value = df
    return mock

async def test_get_macro_indicators_yahooquery_batch():
    """Test batch path: yahooquery succeeds, returns all 15 tickers."""
    with patch("yahooquery.Ticker", side_effect=_yq_batch_mock):
        from mcp_tools.tools.market_data import get_macro_indicators
        result = await get_macro_indicators()
    assert isinstance(result, dict)
    assert "macro" in result
    assert "sectors" in result

async def test_get_macro_indicators_yahooquery_fallback():
    """Test fallback path: yahooquery fails, falls back to yfinance."""
    with (
        patch("yahooquery.Ticker", side_effect=ImportError("no yahooquery")),
        patch("yfinance.Ticker", side_effect=_yf_ticker_mock),
    ):
        from mcp_tools.tools.market_data import get_macro_indicators
        result = await get_macro_indicators()
    assert isinstance(result, dict)
    assert "macro" in result
    assert "sectors" in result
```

---

## Pass 3: New `get_analyst_activity` MCP Tool

### Phase 3.1: Create the MCP tool function

**File to modify:**
- `src/mcp_tools/tools/sentiment.py`

**Add after the `get_peers` tool (end of file):**

New uncached function + public tool:

```python
async def _get_analyst_activity_uncached(ticker: str, limit: int) -> dict:
    try:
        from yahooquery import Ticker as YQTicker
        await _YQ_LIMITER.acquire()
        loop = asyncio.get_event_loop()

        def _fetch() -> list[dict]:
            t = YQTicker(ticker.upper())
            gh = t.grading_history
            if isinstance(gh, str) or (isinstance(gh, dict) and ticker.upper() in gh):
                return []  # error response
            if hasattr(gh, "empty") and gh.empty:
                return []
            records = gh.reset_index().to_dict(orient="records")
            return records[:limit]

        raw = await loop.run_in_executor(None, _fetch)
        activities = []
        for r in raw:
            activities.append({
                "date": str(r.get("epochGradeDate", ""))[:10] if hasattr(r.get("epochGradeDate", ""), "isoformat") else str(r.get("epochGradeDate", ""))[:10],
                "firm": r.get("firm", ""),
                "action": r.get("action", ""),        # "up", "down", "main", "init", "reit"
                "from_grade": r.get("fromGrade", ""),
                "to_grade": r.get("toGrade", ""),
                "prior_target": _serialise_value(r.get("priorPriceTarget")),
                "new_target": _serialise_value(r.get("currentPriceTarget")),
                "target_action": r.get("priceTargetAction", ""),
            })
        return {
            "ticker": ticker.upper(),
            "activities": activities,
            "total": len(activities),
            "upgrades": sum(1 for a in activities if a["action"] in ("up",)),
            "downgrades": sum(1 for a in activities if a["action"] in ("down",)),
            "initiations": sum(1 for a in activities if a["action"] in ("init",)),
        }
    except Exception as exc:
        logger.warning("get_analyst_activity failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "activities": [], "error": str(exc), "total": 0}


@app.tool()
@observe()
@logged()
async def get_analyst_activity(ticker: str, limit: int = 20) -> dict:
    """Fetch recent analyst upgrade/downgrade/initiation activity for a ticker.

    Returns firm names, action (upgrade/downgrade/initiate/reiterate),
    prior and new grades, and price target changes.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        limit:  Maximum activities to return (default 20)

    Returns:
        dict with keys:
          ticker, activities (list of {date, firm, action, from_grade, to_grade,
          prior_target, new_target, target_action}), total, upgrades, downgrades, initiations
    """
    logger.info("Tool called", extra={"tool": "get_analyst_activity", "ticker": ticker})
    return await cache_analyst_activity.get_or_fetch(
        f"analyst_activity:{ticker.upper()}:{limit}",
        lambda: _get_analyst_activity_uncached(ticker, limit),
    )
```

**Also add imports at top of sentiment.py:**
```python
from mcp_tools.infra.rate_limiters import _YQ_LIMITER, cache_analyst_activity
```
(add to existing rate_limiters import line)

---

### Phase 3.2: Add `AnalystPositioning` model fields for upgrade/downgrade data

**File to modify:**
- `src/shared/agent_models.py`

**Add fields to `AnalystPositioning` class (after `short_squeeze_risk`):**
```python
recent_upgrades: int = Field(0, ge=0)
recent_downgrades: int = Field(0, ge=0)
recent_initiations: int = Field(0, ge=0)
grade_momentum: Optional[str] = None  # "improving", "deteriorating", "stable"
latest_actions: list[dict] = Field(default_factory=list)  # [{firm, action, to_grade, new_target}]
```

---

### Phase 3.3: Wire `get_analyst_activity` into `analyst_positioning_node`

**File to modify:**
- `src/quant/nodes/data_fetch.py`

**Current behavior (lines 294-343):**
- Reads `_financials_raw.info` for recommendation_key, targetMeanPrice, shortRatio, etc.
- No MCP call — purely from pre-fetched data

**New behavior:**
- After extracting existing info fields, make an additional MCP call to `get_analyst_activity`
- Parse response and populate the new `AnalystPositioning` fields
- If the call fails, the existing fields still populate (graceful degradation)

**Changes to `analyst_positioning_node` function (add after line 340, before the return):**

```python
    # Enrich with analyst upgrade/downgrade activity
    recent_upgrades = 0
    recent_downgrades = 0
    recent_initiations = 0
    latest_actions = []
    grade_momentum = "stable"

    mcp = state.get("mcp_client")
    if mcp:
        try:
            activity_res = await mcp.call_tool_by_name(
                "get_analyst_activity", {"ticker": ticker, "limit": 15}
            )
            if hasattr(activity_res, "content") and activity_res.content:
                act_text = (
                    activity_res.content[0].text
                    if hasattr(activity_res.content[0], "text")
                    else str(activity_res.content[0])
                )
                act_data = json.loads(act_text)
                recent_upgrades = act_data.get("upgrades", 0)
                recent_downgrades = act_data.get("downgrades", 0)
                recent_initiations = act_data.get("initiations", 0)
                latest_actions = [
                    {"firm": a["firm"], "action": a["action"],
                     "to_grade": a["to_grade"], "new_target": a.get("new_target")}
                    for a in (act_data.get("activities") or [])[:5]
                ]
                if recent_upgrades > recent_downgrades * 1.5:
                    grade_momentum = "improving"
                elif recent_downgrades > recent_upgrades * 1.5:
                    grade_momentum = "deteriorating"
        except Exception as e:
            logger.debug("Analyst activity enrichment failed (non-fatal): %s", e)
```

Then update the return dict to include new fields in the `AnalystPositioning(...)` constructor:
```python
    return {
        "positioning": AnalystPositioning(
            # ... existing fields unchanged ...
            recent_upgrades=recent_upgrades,
            recent_downgrades=recent_downgrades,
            recent_initiations=recent_initiations,
            grade_momentum=grade_momentum,
            latest_actions=latest_actions,
        ).model_dump(),
    }
```

---

### Phase 3.4: Add characterization test

**File to modify:**
- `src/tests/characterization/test_mcp_tool_shapes.py`

**Add test:**
```python
async def test_get_analyst_activity_shape():
    """Note: yahooquery is lazy-imported inside the function, so we patch
    'yahooquery.Ticker' at the module where it's imported, not a module-level alias."""
    mock_yq = MagicMock()
    mock_yq.return_value.grading_history = pd.DataFrame({
        "epochGradeDate": [pd.Timestamp("2024-06-01")],
        "firm": ["Goldman Sachs"],
        "action": ["up"],
        "fromGrade": ["Hold"],
        "toGrade": ["Buy"],
        "priorPriceTarget": [150.0],
        "currentPriceTarget": [180.0],  # NOT newPriceTarget (Risk 4)
        "priceTargetAction": ["Raises"],
    })
    with patch("yahooquery.Ticker", mock_yq):
        from mcp_tools.tools.sentiment import get_analyst_activity
        result = await get_analyst_activity("NVDA")
    assert isinstance(result, dict)
    assert "ticker" in result
    assert "activities" in result
    assert isinstance(result["activities"], list)
    assert len(result["activities"]) == 1
    assert "upgrades" in result
    assert "downgrades" in result
    assert result["activities"][0]["new_target"] == 180.0

async def test_get_analyst_activity_error_shape():
    mock_yq = MagicMock()
    mock_yq.return_value.grading_history = pd.DataFrame()  # empty DataFrame
    with patch("yahooquery.Ticker", mock_yq):
        from mcp_tools.tools.sentiment import get_analyst_activity
        result = await get_analyst_activity("FAIL")
    assert isinstance(result, dict)
    assert "activities" in result
    assert result["activities"] == []
```

---

## Pass 4: Augment `get_earnings_history` with Forward Estimates

### Phase 4.1: Add forward estimate data from yahooquery `earnings_trend`

**File to modify:**
- `src/mcp_tools/tools/sentiment.py`

**Current behavior (lines 481-532):**
- `get_earnings_history` calls `yf.Ticker(t).earnings_dates` for past quarters
- Returns `{quarters, beat_rate, avg_surprise_pct}`

**New behavior:**
- After fetching past quarters (yfinance — keep as-is), also fetch forward estimates via `yahooquery.Ticker(t).earnings_trend`
- Add `forward_estimates` and `eps_revisions` keys to the return dict
- Existing keys unchanged — backward compatible

**Important structural note (Risk 12):** `get_earnings_history` has NO `_uncached` helper function and NO cache wrapper — the logic is inline. The implementation must restructure it into two independent try/except blocks so a yahooquery failure doesn't take down the yfinance result.

**Changes:**

Step 1: Create a new helper function (add before `get_earnings_history`):
```python
async def _fetch_forward_estimates(ticker: str) -> dict:
    """Fetch forward EPS estimates and revision momentum from yahooquery."""
    try:
        from yahooquery import Ticker as YQTicker
        await _YQ_LIMITER.acquire()
        loop = asyncio.get_event_loop()
        
        def _fetch():
            t = YQTicker(ticker.upper())
            et = t.earnings_trend
            if isinstance(et, str) or not isinstance(et, dict):
                return {}
            data = et.get(ticker.upper(), {})
            if not isinstance(data, dict):
                return {}
            trend = data.get("trend", [])
            estimates = []
            revisions = []
            for period in trend:
                ee = period.get("earningsEstimate", {})
                er = period.get("epsRevisions", {})
                re_ = period.get("revenueEstimate", {})
                estimates.append({
                    "period": period.get("period"),
                    "end_date": period.get("endDate"),
                    "growth": period.get("growth"),
                    "eps_avg": ee.get("avg"),
                    "eps_low": ee.get("low"),
                    "eps_high": ee.get("high"),
                    "eps_year_ago": ee.get("yearAgoEps"),
                    "n_analysts": ee.get("numberOfAnalysts"),
                    "revenue_avg": re_.get("avg"),
                    "revenue_growth": re_.get("growth"),
                })
                revisions.append({
                    "period": period.get("period"),
                    "up_last_7d": er.get("upLast7days", 0),
                    "up_last_30d": er.get("upLast30days", 0),
                    "down_last_7d": er.get("downLast7Days", 0),
                    "down_last_30d": er.get("downLast30days", 0),
                })
            return {"forward_estimates": estimates, "eps_revisions": revisions}
        
        return await loop.run_in_executor(None, _fetch)
    except Exception as exc:
        logger.debug("Forward estimates fetch failed (non-fatal): %s", exc)
        return {}
```

Step 2: Restructure `get_earnings_history` to separate yfinance and yahooquery concerns:

```python
@app.tool()
@observe()
async def get_earnings_history(ticker: str, limit: int = 8) -> dict:
    """Fetch quarterly earnings history: EPS estimates vs actuals, surprise %, and forward estimates.
    ...existing docstring + add forward_estimates, eps_revisions to Returns...
    """
    # --- Part 1: yfinance historical earnings (existing logic, unchanged) ---
    result = {
        "ticker": ticker.upper(),
        "quarters": [],
        "beat_rate": None,
        "avg_surprise_pct": None,
        "n_quarters": 0,
        "forward_estimates": [],   # NEW — defaults for backward compat
        "eps_revisions": [],       # NEW
    }
    try:
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()
        ed = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).earnings_dates)
        if ed is not None and not ed.empty:
            past = ed[ed["Reported EPS"].notna()].head(limit)
            quarters = []
            for dt, row in past.iterrows():
                quarters.append({
                    "date": dt.isoformat(),
                    "eps_estimate": _serialise_value(row.get("EPS Estimate")),
                    "eps_actual": _serialise_value(row.get("Reported EPS")),
                    "surprise_pct": _serialise_value(row.get("Surprise(%)")),
                })
            surprise_vals = [q["surprise_pct"] for q in quarters if q["surprise_pct"] is not None]
            beat_count = sum(1 for s in surprise_vals if s > 0)
            result["quarters"] = quarters
            result["beat_rate"] = round(beat_count / len(surprise_vals), 3) if surprise_vals else None
            result["avg_surprise_pct"] = (
                round(sum(surprise_vals) / len(surprise_vals), 2) if surprise_vals else None
            )
            result["n_quarters"] = len(quarters)
    except Exception as exc:
        logger.warning("get_earnings_history yfinance failed for %s: %s", ticker, exc)
        result["error"] = str(exc)

    # --- Part 2: yahooquery forward estimates (independent, non-fatal) ---
    forward_data = await _fetch_forward_estimates(ticker)
    result["forward_estimates"] = forward_data.get("forward_estimates", [])
    result["eps_revisions"] = forward_data.get("eps_revisions", [])

    return result
```

**Key difference from current code:** The yfinance and yahooquery parts are in separate try/except blocks. The yfinance failure sets `result["error"]` but doesn't prevent the yahooquery call. The yahooquery failure (`_fetch_forward_estimates` has its own try/except) doesn't affect the yfinance result.

**Return schema — new keys (additive, backward-compatible):**
```json
{
    "forward_estimates": [
        {"period": "0q", "end_date": "2026-06-30", "growth": 0.21, "eps_avg": 1.89, "eps_low": 1.83, "eps_high": 1.99, "n_analysts": 31, "revenue_avg": 108994233640, "revenue_growth": 0.159},
        {"period": "+1q", ...},
        {"period": "0y", ...},
        {"period": "+1y", ...}
    ],
    "eps_revisions": [
        {"period": "0q", "up_last_7d": 0, "up_last_30d": 24, "down_last_7d": 0, "down_last_30d": 0},
        ...
    ]
}
```

---

### Phase 4.2: Add `AnalystPositioning` model fields for earnings revision data

**File to modify:**
- `src/shared/agent_models.py`

**Add fields to `AnalystPositioning` class:**
```python
eps_revision_up_30d: Optional[int] = Field(None, ge=0)
eps_revision_down_30d: Optional[int] = Field(None, ge=0)
eps_revision_momentum: Optional[str] = None  # "positive", "negative", "flat"
forward_eps_growth: Optional[float] = None  # next quarter EPS growth estimate
```

---

### Phase 4.3: Wire forward estimates into `analyst_positioning_node`

**File to modify:**
- `src/quant/nodes/data_fetch.py`

**Add after the analyst activity enrichment (from Phase 3.3):**

```python
    # Enrich with forward EPS revision momentum
    eps_revision_up_30d = None
    eps_revision_down_30d = None
    eps_revision_momentum = None
    forward_eps_growth = None

    if mcp:
        try:
            earn_res = await mcp.call_tool_by_name(
                "get_earnings_history", {"ticker": ticker, "limit": 4}
            )
            if hasattr(earn_res, "content") and earn_res.content:
                earn_text = (
                    earn_res.content[0].text
                    if hasattr(earn_res.content[0], "text")
                    else str(earn_res.content[0])
                )
                earn_data = json.loads(earn_text)
                revisions = earn_data.get("eps_revisions", [])
                if revisions:
                    current_q = revisions[0]  # "0q" = current quarter
                    eps_revision_up_30d = current_q.get("up_last_30d")
                    eps_revision_down_30d = current_q.get("down_last_30d")
                    up = eps_revision_up_30d or 0
                    down = eps_revision_down_30d or 0
                    if up > down * 2:
                        eps_revision_momentum = "positive"
                    elif down > up * 2:
                        eps_revision_momentum = "negative"
                    else:
                        eps_revision_momentum = "flat"
                fwd = earn_data.get("forward_estimates", [])
                if fwd:
                    forward_eps_growth = fwd[0].get("growth")
        except Exception as e:
            logger.debug("Earnings trend enrichment failed (non-fatal): %s", e)
```

Add these to the `AnalystPositioning(...)` constructor in the return.

---

### Phase 4.4: Add characterization test

**File to modify:**
- `src/tests/characterization/test_mcp_tool_shapes.py`

**Add new test. Note (Risk 14):** The mock must set `mock.earnings_dates` as a property-style attribute (not `mock.get_earnings_dates.return_value`), because the real code accesses `yf.Ticker(t).earnings_dates` as a property.

```python
def _yf_ticker_with_earnings(symbol: str):
    """Extended yfinance mock that includes earnings_dates."""
    mock = _yf_ticker_mock(symbol)
    mock.earnings_dates = pd.DataFrame(
        {"EPS Estimate": [1.5], "Reported EPS": [1.6], "Surprise(%)": [6.67]},
        index=pd.to_datetime(["2024-03-15"]),
    )
    return mock

async def test_get_earnings_history_with_forward_estimates():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_with_earnings):
        mock_yq = MagicMock()
        mock_yq.return_value.earnings_trend = {
            "NVDA": {
                "trend": [{
                    "period": "0q", "endDate": "2026-06-30", "growth": 0.2,
                    "earningsEstimate": {"avg": 1.89, "low": 1.83, "high": 1.99, "numberOfAnalysts": 31},
                    "epsRevisions": {"upLast7days": 0, "upLast30days": 24, "downLast7Days": 0, "downLast30days": 0},
                    "revenueEstimate": {"avg": 109000000000, "growth": 0.16},
                }]
            }
        }
        with patch("yahooquery.Ticker", mock_yq):
            from mcp_tools.tools.sentiment import get_earnings_history
            result = await get_earnings_history("NVDA")
    assert isinstance(result, dict)
    assert "quarters" in result
    assert "forward_estimates" in result
    assert "eps_revisions" in result
    assert isinstance(result["forward_estimates"], list)
```

---

## Pass 5: New `get_valuation_timeseries` MCP Tool

### Phase 5.1: Create the MCP tool function

**File to modify:**
- `src/mcp_tools/tools/market_data.py`

**Add after `get_options_chain` (end of file):**

```python
async def _get_valuation_ts_uncached(ticker: str) -> dict:
    try:
        from yahooquery import Ticker as YQTicker
        await _YQ_LIMITER.acquire()
        loop = asyncio.get_event_loop()

        def _fetch() -> dict:
            t = YQTicker(ticker.upper())
            vm = t.valuation_measures
            if isinstance(vm, str):
                return {"error": vm}
            if hasattr(vm, "empty") and vm.empty:
                return {"periods": []}
            records = []
            for _, row in vm.iterrows():
                records.append({
                    "date": str(row.get("asOfDate", ""))[:10] if hasattr(row.get("asOfDate"), "isoformat") else str(row.get("asOfDate", ""))[:10],
                    "period_type": row.get("periodType", ""),
                    "pe_ratio": _serialise_value(row.get("PeRatio")),
                    "ps_ratio": _serialise_value(row.get("PsRatio")),
                    "pb_ratio": _serialise_value(row.get("PbRatio")),
                    "peg_ratio": _serialise_value(row.get("PegRatio")),
                    "enterprise_value": _serialise_value(row.get("EnterpriseValue")),
                    "ev_to_ebitda": _serialise_value(row.get("EnterprisesValueEBITDARatio")),
                    "ev_to_revenue": _serialise_value(row.get("EnterprisesValueRevenueRatio")),
                    "market_cap": _serialise_value(row.get("MarketCap")),
                })
            return {"periods": records}

        data = await loop.run_in_executor(None, _fetch)
        if "error" in data:
            return {"ticker": ticker.upper(), "periods": [], "error": data["error"]}

        periods = data.get("periods", [])
        # Compute current vs historical averages
        pe_values = [p["pe_ratio"] for p in periods if p["pe_ratio"] is not None]
        summary = {}
        if pe_values:
            summary["pe_avg_2y"] = round(sum(pe_values) / len(pe_values), 2)
            summary["pe_current"] = pe_values[-1] if pe_values else None
            summary["pe_percentile"] = round(
                sum(1 for v in pe_values if v <= pe_values[-1]) / len(pe_values) * 100, 1
            ) if pe_values else None

        return {
            "ticker": ticker.upper(),
            "periods": periods,
            "n_periods": len(periods),
            "summary": summary,
        }
    except Exception as exc:
        logger.warning("get_valuation_timeseries failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "periods": [], "error": str(exc)}


@app.tool()
@observe()
@logged()
async def get_valuation_timeseries(ticker: str) -> dict:
    """Fetch quarterly valuation multiples history (P/E, P/S, PEG, EV) for a ticker.

    Returns up to ~8 quarters of trailing data with current-vs-average comparisons.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys:
          ticker, periods (list of {date, pe_ratio, ps_ratio, pb_ratio, peg_ratio,
          enterprise_value, ev_to_ebitda, ev_to_revenue, market_cap}),
          n_periods, summary ({pe_avg_2y, pe_current, pe_percentile})
    """
    logger.info("Tool called", extra={"tool": "get_valuation_timeseries", "ticker": ticker})
    return await cache_valuation_ts.get_or_fetch(
        f"valuation_ts:{ticker.upper()}",
        lambda: _get_valuation_ts_uncached(ticker),
    )
```

**Add imports (rate_limiters only — yahooquery is lazy-imported inside the function, see Risk 13):**
```python
from mcp_tools.infra.rate_limiters import _YQ_LIMITER, cache_valuation_ts  # add to existing import
```

---

### Phase 5.2: Add to `QuantAnalysisState` and wire into DCF or fundamentals

**Files to modify:**
- `src/quant/state.py` — add `valuation_history: dict | None` field
- `src/quant/graph.py` — add `"valuation_history": None` to the `initial` dict in `run()` (line ~161, after `"web_context": []`)
- `src/quant/nodes/data_fetch.py` — add call in `fundamental_analysis_node` to fetch valuation timeseries and store in state
- `src/tests/characterization/test_quant_nodes_io.py` — add `"valuation_history": None` to `_base_state()` dict (line ~54)

**In `fundamental_analysis_node`, after populating `fundamentals` dict:**

```python
        # Fetch valuation history for context
        valuation_history = None
        try:
            val_res = await mcp.call_tool_by_name("get_valuation_timeseries", {"ticker": ticker})
            if hasattr(val_res, "content") and val_res.content:
                val_text = (
                    val_res.content[0].text
                    if hasattr(val_res.content[0], "text")
                    else str(val_res.content[0])
                )
                valuation_history = json.loads(val_text)
        except Exception as e:
            logger.debug("Valuation timeseries fetch failed (non-fatal): %s", e)

        return {
            "fundamentals": fundamentals_dict,
            "_financials_raw": data,
            "valuation_history": valuation_history,  # NEW
        }
```

**Note:** The `format_output_node` and `llm_summary_node` can reference `state["valuation_history"]` to include "P/E is at Xth percentile of its 2-year range" in the reasoning. This wiring into the LLM prompt is a follow-up — the data availability is the goal of this pass.

---

### Phase 5.3: Add characterization test

**File to modify:**
- `src/tests/characterization/test_mcp_tool_shapes.py`

```python
async def test_get_valuation_timeseries_shape():
    mock_yq = MagicMock()
    mock_yq.return_value.valuation_measures = pd.DataFrame({
        "asOfDate": [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-04-01")],
        "periodType": ["TTM", "TTM"],
        "PeRatio": [25.0, 28.0],
        "PsRatio": [7.0, 8.0],
        "PbRatio": [40.0, 45.0],
        "PegRatio": [1.5, 1.8],
        "EnterpriseValue": [3e12, 3.5e12],
        "EnterprisesValueEBITDARatio": [22.0, 24.0],
        "EnterprisesValueRevenueRatio": [9.0, 10.0],
        "MarketCap": [2.8e12, 3.2e12],
    })
    with patch("yahooquery.Ticker", mock_yq):
        from mcp_tools.tools.market_data import get_valuation_timeseries
        result = await get_valuation_timeseries("AAPL")
    assert isinstance(result, dict)
    assert "ticker" in result
    assert "periods" in result
    assert isinstance(result["periods"], list)
    assert len(result["periods"]) == 2
    assert "summary" in result

async def test_get_valuation_timeseries_error_shape():
    mock_yq = MagicMock()
    mock_yq.return_value.valuation_measures = "No data"
    with patch("yahooquery.Ticker", mock_yq):
        from mcp_tools.tools.market_data import get_valuation_timeseries
        result = await get_valuation_timeseries("FAIL")
    assert isinstance(result, dict)
    assert "periods" in result
    assert result["periods"] == []
```

---

## Pass 6: Integration Testing & Cleanup

### Phase 6.1: Run full characterization test suite

```bash
uv run pytest src/tests/characterization/ -v
```

Ensure all existing tests still pass + new tests pass.

### Phase 6.2: Run the full test suite

```bash
uv run pytest src/tests/ -v --ignore=src/tests/integration --ignore=src/tests/evaluation
```

### Phase 6.3: Live smoke test

Run each new/modified tool with a real ticker:
```bash
uv run python -c "
import asyncio
from mcp_tools.tools.market_data import get_macro_indicators, get_valuation_timeseries
from mcp_tools.tools.sentiment import get_analyst_activity, get_earnings_history

async def main():
    macro = await get_macro_indicators()
    print('macro keys:', list(macro.get('macro', {}).keys()))
    print('sector keys:', list(macro.get('sectors', {}).keys()))

    activity = await get_analyst_activity('AAPL')
    print('analyst activities:', activity['total'])

    earnings = await get_earnings_history('AAPL')
    print('forward_estimates:', len(earnings.get('forward_estimates', [])))
    print('eps_revisions:', len(earnings.get('eps_revisions', [])))

    val = await get_valuation_timeseries('AAPL')
    print('valuation periods:', val['n_periods'])

asyncio.run(main())
"
```

### Phase 6.4: Update `pyproject.toml` test markers (if needed)

No new markers expected — all new tests are characterization tests (no `@pytest.mark.integration`).

---

## Summary: File Change Matrix

| File | Pass | Action |
|------|------|--------|
| `pyproject.toml` | 1.1 | Add `yahooquery>=2.4.0` dependency |
| `src/mcp_tools/infra/rate_limiters.py` | 1.2 | Add `_YQ_LIMITER`, `cache_analyst_activity`, `cache_valuation_ts`, `cache_earnings_trend` |
| `src/mcp_tools/tools/market_data.py` | 2.1, 5.1 | Batch macro with yahooquery fallback; add `get_valuation_timeseries` |
| `src/mcp_tools/tools/sentiment.py` | 3.1, 4.1 | Add `get_analyst_activity`; augment `get_earnings_history` with forward estimates |
| `src/shared/agent_models.py` | 3.2, 4.2 | Add fields to `AnalystPositioning` |
| `src/quant/nodes/data_fetch.py` | 3.3, 4.3, 5.2 | Wire new tools into `analyst_positioning_node` and `fundamental_analysis_node` |
| `src/quant/state.py` | 5.2 | Add `valuation_history` field |
| `src/quant/graph.py` | 5.2 | Add `valuation_history: None` to initial state dict |
| `src/tests/characterization/test_mcp_tool_shapes.py` | 2.2, 3.4, 4.4, 5.3 | Add shape tests for all new/modified tools |
| `src/tests/characterization/test_quant_nodes_io.py` | 5.2 | Add `valuation_history` to `_base_state()` |

---

## Breakage Risks & Mitigations (reviewed)

### Risk 1: yahooquery column names are lowercase
**Issue:** yahooquery `history()` returns columns `close`, `open`, `high`, `low` (lowercase), while yfinance uses `Close`, `Open`, `High`, `Low` (capitalized).
**Impact:** Phase 2.1 — the batch macro implementation must use `hist["close"]` not `hist["Close"]`.
**Mitigation:** Already handled in Phase 2.1 code. The yfinance fallback (`_get_macro_impl_yfinance`) keeps using `hist["Close"]` unchanged.

### Risk 2: yahooquery MultiIndex access pattern
**Issue:** `all_hist.loc[sym]` may fail with KeyError for symbols not in the DataFrame. Verified that `all_hist.xs(sym, level='symbol')` is the reliable access pattern. Also, `all_hist.index.get_level_values("symbol")` returns the symbol level correctly.
**Impact:** Phase 2.1 — must use `xs()` instead of `.loc[]` for per-symbol extraction from the MultiIndex.
**Mitigation:** Updated Phase 2.1 code: use `all_hist.xs(sym, level="symbol")` wrapped in try/except, with a pre-check via `sym in all_hist.index.get_level_values("symbol").unique()`.

### Risk 3: yahooquery error responses are strings, not exceptions
**Issue:** When a ticker is invalid or data unavailable, yahooquery returns a string (e.g., `"Valuation data unavailable for XYZ"`) instead of raising an exception. For `history()`, invalid tickers are silently omitted from the result DataFrame.
**Impact:** Phases 3.1, 4.1, 5.1 — all new tools must check `isinstance(result, str)` before treating it as a DataFrame/dict.
**Mitigation:** Already handled in the plan code. Each `_fetch()` checks `isinstance(result, str)` and returns empty.

### Risk 4: `grading_history` column names differ from plan
**Issue:** Verified actual columns are: `epochGradeDate`, `firm`, `toGrade`, `fromGrade`, `action`, `priceTargetAction`, `currentPriceTarget`, `priorPriceTarget`. The plan used `newPriceTarget` — the actual column is `currentPriceTarget`.
**Impact:** Phase 3.1 — field name mismatch in `_get_analyst_activity_uncached`.
**Mitigation:** Fix Phase 3.1: change `r.get("newPriceTarget")` → `r.get("currentPriceTarget")`. Also add `priceTargetAction` field.

### Risk 5: `grading_history` index has `(symbol, row)` not `(symbol, date)`
**Issue:** The grading_history DataFrame has a MultiIndex of `(symbol, row)`, not date-based. Need `reset_index()` before processing.
**Mitigation:** Already handled — code calls `gh.reset_index().to_dict(orient="records")`.

### Risk 6: `analyst_positioning_node` currently makes NO MCP calls
**Issue:** Adding MCP calls (`get_analyst_activity`, `get_earnings_history`) to a node that was previously pure state-read changes its execution profile. It will now have network I/O and could slow down the graph.
**Impact:** Phases 3.3, 4.3 — potential latency increase in the quant graph.
**Mitigation:** Both calls are wrapped in try/except with `logger.debug` on failure — the node still returns valid data even if both enrichment calls fail. The calls run sequentially within the node but the node itself runs in parallel with `peer_comparison_node` (both fan from `fetch_fundamentals`), so total graph latency increase is bounded. Both results are cached (1hr TTL), so repeat analyses are instant.

### Risk 7: `valuation_measures` DataFrame index is `(symbol,)` not `(symbol, date)`
**Issue:** Verified the index is just `symbol` (single level), and `asOfDate` is a regular column, not part of the index. The `for _, row in vm.iterrows()` pattern works correctly.
**Mitigation:** No change needed — the plan's iteration pattern is correct.

### Risk 8: `QuantAnalysisState` — new `valuation_history` field must have a default in `graph.py`
**Issue:** `graph.py:run()` initializes all state fields (lines 136-162). Adding `valuation_history` to the TypedDict without adding it to `initial` dict will cause LangGraph to use `None` by default (TypedDict fields without reducers default to last-value channel). This is fine since the field is `dict | None`, but for clarity it should be in `initial`.
**Impact:** Phase 5.2.
**Mitigation:** Add `"valuation_history": None` to the `initial` dict in `graph.py:run()`. Also add to `_base_state()` in `test_quant_nodes_io.py`.

### Risk 9: `QuantAgentOutput` does not include `valuation_history`
**Issue:** `graph.py` constructs `QuantAgentOutput` from state (lines 189-209). The new `valuation_history` field is in state but NOT in `QuantAgentOutput`. This means it won't flow through to report generation via the validated output model.
**Impact:** Phase 5.2 — data is in state but lost at output validation.
**Mitigation:** For now this is intentional — the valuation history is available to `format_output_node` and `llm_summary_node` via state, and can influence reasoning/signals. Adding it to `QuantAgentOutput` for report consumption is a follow-up task, not needed for this plan's scope.

### Risk 10: Test cache pollution
**Issue:** Characterization tests call the public tool functions (which go through caches). If tests run in sequence, a cached result from one test could leak into another.
**Impact:** All test phases.
**Mitigation:** The existing tests already handle this — each test patches the upstream data source (yfinance.Ticker), and the cache's `get_or_fetch` will call the lambda (which uses the mocked source) on cache miss. Since tests use different tickers or the cache TTL is short, this is not an issue. However, for safety, new tests should use unique ticker symbols or clear the cache.

### Risk 11: `_serialise_value` import in `sentiment.py`
**Issue:** `sentiment.py` already imports `_serialise_value` from `market_data.py` (line 35). Phase 3.1's `get_analyst_activity` uses it — no new import needed.
**Mitigation:** Verified — no change required.

### Risk 12: `get_earnings_history` has NO cache and NO uncached helper
**Issue:** Unlike all other tools, `get_earnings_history` (sentiment.py:481-532) has inline logic with no `_get_earnings_history_uncached()` function and no `cache_*.get_or_fetch()` wrapper. The plan's Phase 4.1 references modifying a nonexistent `_get_earnings_history_uncached` function.
**Impact:** Phase 4.1 must restructure the function differently than described.
**Mitigation:** The implementation must:
1. Extract the yfinance part into a separate try/except that builds the base `result` dict
2. Call `_fetch_forward_estimates()` in a second try/except that enriches the result with forward data
3. The yahooquery call must NOT be inside the existing yfinance try/except — otherwise a yahooquery failure would trigger the yfinance error handler
4. Consider adding a `cache_earnings` wrapper (using `cache_earnings_trend` from Phase 1.2) since both yfinance and yahooquery calls are now involved

### Risk 13: Top-level `from yahooquery import Ticker as YQTicker` crashes all tools if yahooquery missing
**Issue:** Phase 2.1 specifies adding `from yahooquery import Ticker as YQTicker` at the module top level of `market_data.py`. If yahooquery is not installed (pip install without the dep), this import crashes the entire MCP tools server — killing `get_prices`, `get_financials`, etc.
**Impact:** Phase 2.1 and 5.1 (market_data.py), Phase 3.1 (sentiment.py)
**Mitigation:** Use lazy imports inside the uncached function bodies, matching the pattern already used in Phase 3.1 and 4.1:
```python
# Inside _get_macro_impl:
from yahooquery import Ticker as YQTicker  # lazy import
```
Do NOT add a top-level import. The `pyproject.toml` dependency ensures it's installed, but defense in depth prevents full server crashes during partial installs.

### Risk 14: `_yf_ticker_mock` uses `get_earnings_dates` method but code accesses `earnings_dates` property
**Issue:** The test mock (test_mcp_tool_shapes.py:42) sets `mock.get_earnings_dates.return_value = pd.DataFrame()`, but `get_earnings_history` accesses `yf.Ticker(ticker).earnings_dates` (a property, not a method call). MagicMock auto-creates `mock.earnings_dates` as a new MagicMock (not a DataFrame).
**Impact:** Phase 4.4 — new tests for `get_earnings_history` must set `mock.earnings_dates = pd.DataFrame(...)` (not `mock.get_earnings_dates.return_value`).
**Mitigation:** In the new test, explicitly set `mock.earnings_dates` as a property-style attribute on the mock.

### Risk 15: `llm_summary_node` and report extraction access positioning fields directly
**Issue:** `llm_summary_node` (summary.py:495-500) accesses `positioning.get("recommendation_key")`, `positioning.get("n_analysts")`, `positioning.get("analyst_upside_pct")`. Report extraction (extraction.py:2004-2036) accesses `pos.recommendation_key`, `pos.n_analysts`, etc.
**Impact:** Phases 3.2 and 4.2 — new fields added to `AnalystPositioning` must have defaults so neither `llm_summary_node` (dict `.get()`) nor report extraction (Pydantic attribute access) breaks.
**Mitigation:** Already safe — all new fields have defaults (`0`, `None`, `Field(default_factory=list)`). Confirmed: `llm_summary_node` uses `.get()` on dicts, report extraction uses Pydantic model with defaults. Neither will break.

### Risk 16: `format_output_node` `_score_behavioral` only reads 3 positioning fields
**Issue:** `_score_behavioral` (calculations.py:368-412) reads `consensus_score`, `analyst_upside_pct` from positioning. New fields like `grade_momentum`, `eps_revision_momentum` are NOT used in scoring.
**Impact:** Pass 3 and 4 add data but don't change the signal scoring. The LLM summary and report benefit from richer data, but the automated BUY/HOLD/SELL recommendation doesn't incorporate the new signals.
**Mitigation:** This is intentional for this plan — incorporating new fields into `_score_behavioral` is a follow-up. The new data is available for the LLM summary to reference and for human report readers.

---

## Verification Checklist

- [ ] `uv sync` succeeds with yahooquery
- [ ] `uv run pytest src/tests/characterization/ -v` — all pass
- [ ] `uv run pytest src/tests/ -v --ignore=src/tests/integration --ignore=src/tests/evaluation` — all pass
- [ ] `get_macro_indicators()` returns same schema, faster (batch)
- [ ] `get_macro_indicators()` falls back to yfinance if yahooquery fails
- [ ] `get_analyst_activity("AAPL")` returns activities with firm names
- [ ] `get_earnings_history("AAPL")` returns `forward_estimates` and `eps_revisions` keys
- [ ] `get_valuation_timeseries("AAPL")` returns quarterly P/E history
- [ ] No existing tool return schemas are broken
- [ ] All new fields in `AnalystPositioning` have defaults (backward-compatible)
- [ ] `graph.py` initial state includes `valuation_history: None`
- [ ] `test_quant_nodes_io.py` `_base_state()` includes `valuation_history`
- [ ] `grading_history` uses `currentPriceTarget` not `newPriceTarget`
- [ ] Batch macro uses `xs(sym, level="symbol")` not `.loc[sym]`
