# Changelog

## v2.10 — Comprehensive Codebase Documentation (46cb44e)

### Docstrings & JSDoc Coverage across 181 Files (46cb44e)

- **162 Python files (+4,216 lines)**: Added detailed Google-style docstrings with Args, Returns, and Examples to every function, method, and class across all agents, shared modules, MCP servers, and tests.
- **19 Next.js files (+2,000 lines)**: Added JSDoc comments to all TypeScript functions, React components, and API route handlers in the web frontend.
- **`src/shared/metrics.py`**: All 7 computation functions (`compute_rsi_wilder`, `compute_sharpe_ratio`, `compute_beta`, `compute_sortino_ratio`, `compute_calmar_ratio`, `compute_alpha`, `compute_information_ratio`) and `MetricValue` class documented with mathematical formulas and edge-case behavior.
- **`src/shared/agent_models.py`**: All Pydantic model fields documented with field descriptions and validation constraints.
- **`src/shared/reports/`**: All extraction, renderer, and template functions documented.
- **Test files**: All test functions and fixtures documented with behavior descriptions.
- **No behavioral changes** — documentation-only commit.

## v2.9 — Metric Standardization, P/B Valuation, Extraction Guard Dedup, Valuation Label Unification (364e3b9–beadc55)

### P/B Valuation Method for Banks & Financial Firms (bc9c59d, 6cc3f98)

- **`src/quant/nodes/dcf.py`**: DCF node now supports a `method` field (`"dcf"` or `"pb"`). Financial institutions (banks, insurance) use P/B residual income model instead of traditional DCF. When `method == "pb"`: computes `fair_pb_multiple` from ROE-COE spread, applies P/B ratio to book value per share.
- **`src/shared/agent_models.py`**: `DCFValuation` model extended with: `method` (str), `pb_ratio_used` (float), `fair_pb_multiple` (float), `scenarios` (dict), `valuation_reliability` (float), `dcf_assumptions` (dict), `sensitivity_matrix` (dict), `revenue_growth`, `earnings_growth`, `equity_value`, `market_enterprise_value`, `net_debt`.
- **`src/shared/reports/extraction.py`**: New P/B-specific DCF breakdown in `_populate_from_validated_outputs()` — shows P/B Ratio and Fair P/B Multiple when method is "pb"; adds market EV, net debt, equity value, revenue/earnings growth, DCF scenarios, sensitivity matrix, risk-free rate, beta, cost of equity/debt, tax rate for DCF method.
- **`src/shared/reports/deck_model.py`**: New `sensitivity_matrix: dict` field in `DeckData`.
- **`src/reviewer/tools/dcf_validator.py`**: Handle `None` fcf_age_days to prevent crash. P/B validation skips FCF-specific checks.
- **DCF input corrections**: WACC, growth rate, net debt, D/E ratio, and market enterprise value now correctly sourced from MCP data. Terminal growth, revenue growth, earnings growth fields added.

### Metric Standardization with Shared `MetricValue` (5215613)

- **`src/shared/metrics.py` (new)**: `MetricValue` dataclass (name, value, weight, score, metadata) providing a single computation pattern across all agents. Replaces 5 near-duplicate metric-computation code paths in analytics and quant.
- **`src/analytics/nodes/statistics.py`**, `src/analytics/nodes/forecast.py`, `src/analytics/nodes/trend.py`, `src/analytics/nodes/anomaly.py`, `src/analytics/nodes/summary.py`, `src/analytics/nodes/charts.py`: Refactored to use `MetricValue` instead of raw dicts. All analytics node outputs now pass through `MetricValue` constructors.
- **`src/quant/nodes/calculations.py`**, `src/quant/nodes/dcf.py`, `src/quant/nodes/technical.py`, `src/quant/nodes/monte_carlo.py`, `src/quant/nodes/summary.py`, `src/quant/nodes/bank_valuation.py`, `src/quant/nodes/data_fetch.py`: Same MetricValue refactor — quant nodes produce typed metric objects instead of dicts.
- **`src/quant/state.py`**: `QuantAnalysisState` updated to work with `MetricValue` typed nodes.
- **`src/reviewer/tools/confidence.py`**, `src/reviewer/tools/contradiction.py`, `src/reviewer/tools/validation.py`, `src/reviewer/tools/verification.py`: Reviewer tools consume `MetricValue` objects.
- **`src/reviewer/executor.py`**: Updated to pass `MetricValue`-structured agent summaries to LLM.
- **`src/shared/__init__.py`**: Re-exports `MetricValue`.
- **Why**: Each agent previously computed and stored metrics in its own ad-hoc dict format, causing key mismatches, missing data, and inconsistent aggregation. A shared `MetricValue` class ensures every metric has a name, numeric value, weight, score threshold, and metadata dict — no more guessing whether a value is a string or float or what its valid range is.

### MACD Model: String/Numeric → Boolean (5215613)

- **`src/shared/agent_models.py`**: `TechnicalIndicators.macd_signal` (Union[float, str]) → `macd_bullish` (Optional[bool]). The previous type was ambiguous — float meant "MACD histogram value", string meant "Bullish" or "Bearish". Now a single `macd_bullish: bool` explicitly signals whether MACD is bullish or bearish.
- **`src/shared/reports/extraction.py`**: Both `_populate_from_agent_outputs()` and `_populate_from_validated_outputs()` use `macd_bullish` boolean directly. Fallback: numeric MACD > 0 → bullish, else bearish.
- **`src/quant/nodes/technical.py`**: Computes `macd_bullish` from MACD histogram sign instead of storing raw `macd_signal`.
- **Impact**: Extraction no longer needs separate handling for float-vs-string `macd_signal`. Technicals table MACD row gets a clear bullish/bearish label.

### Extraction Guard Deduplication — Structured Data Never Overwritten by Regex (0fc9c47, 2dc0466, 5215613)

- **`src/shared/reports/extraction.py`** — every extraction function now checks `_existing_labels` / `existing_chip_labels` / `existing_scorecard_dims` before appending from regex fallbacks:
  - `_stage_metrics()`: Skips KPI chip labels already populated from Pydantic model fields. Prevents regex values from duplicating structured data.
  - `_stage_scenarios()`: All valuation table entries guarded by `_existing_labels` set. Structured Pydantic model values (via `_populate_from_validated_outputs`) are never overwritten by regex matches. Each value type (target price, upside, DCF, bull/bear, p50, prob, CVaR, current price) has its own pre-check before regex fallback.
  - `_stage_financials_scorecard()`: Scorecard dimensions skipped if already populated from validated outputs.
  - `_populate_from_agent_outputs()` / `_populate_from_validated_outputs()`: Legacy agent output path now respects `_pre_verifs` / `_pre_sections` / `_pre_scorecard` snapshots to avoid duplicating reviewer/section/scorecard data when validated path already populated them.
- **Why**: The old pattern blindly appended regex-extracted values after Pydantic model data, resulting in duplicate KPI chips, duplicated valuation entries, and overwritten structured values. The guard pattern gives structured data priority — regex fallback only fires for fields the validated path didn't populate.

### Valuation Label Standardization (0fc9c47)

- All extraction paths now use consistent labels across all code paths:
  - `"Bull Case (p90)"` / `"Bull Case (MC p90)"` → `"95th Percentile Target"`
  - `"Base Case (p50)"` / `"Base Case (MC p50)"` → `"Median Target (p50)"`
  - `"Bear Case (p10)"` / `"Bear Case (MC p10)"` → `"5th Percentile Target"`
- **Files updated**: `_populate_from_agent_outputs()`, `_populate_from_validated_outputs()`, `_extract_deck_data()`, `_enrich_from_markdown()`, `_stage_scenarios()`.
- **Why**: Inconsistent labels across code paths caused downstream display issues (both "Bull Case (p90)" and "95th Percentile Target" appearing in the same report). The standard labels match the institutional style of percentile-based targets.

### Peer Filtering by Actual Data (5215613)

- **`src/shared/reports/extraction.py`**: Peer ticker selection now filters by `_metric_keys = ["pe", "ev_ebitda", "rev_growth", "op_margin", "roe"]` — only peers with actual numeric values for at least one key metric are included. Both `_populate_from_agent_outputs()` and `_populate_from_validated_outputs()` use this filter. Previously, any peer ticker from the peer list was included even if its comparison data was empty.

### Cited Sources: HTML Format + Dedup (5215613)

- **`src/shared/reports/extraction.py`**: Sources section now uses `<ul><li>` HTML format instead of raw `- item` markdown. Sources are deduplicated by label within a section — duplicate sources (common when RAG agent returns the same filing from multiple queries) only appear once. Empty source labels are filtered out.

### Recommendation Drivers Auto-Generated Section (5215613)

- **`src/shared/reports/extraction.py`**: New `Recommendation Drivers` section auto-generated from actual metric signals:
  - **Positive signals**: DCF upside > 20%, RSI oversold (< 30), revenue growth > 10%, bullish analytics trend, supporting reviewer validation
  - **Negative signals**: DCF downside > 10%, RSI overbought (> 70), negative ROE, bearish analytics trend, contradicting reviewer validation
  - Formatted as `<strong>Positive Signals</strong><ul><li>...</li></ul>` + negative mirror

### Verification Rate Normalization (5215613)

- **`src/shared/reports/extraction.py`**: Source verification rates > 1.0 are divided by 100 — catches the common data source mismatch where percentages are stored as `0.85` in some cases and `85` in others. Both `_populate_from_agent_outputs()` and `_populate_from_validated_outputs()` apply the normalization.

### Template & Report Rendering Fixes (2dc0466, 0fc9c47)

- **`src/shared/templates/investment_deck.html`**:
  - Section title uppercasing removed: `{{ sec.title|upper }}` → `{{ sec.title }}`.
  - Section body now supports raw HTML: `{{ sec.body|safe }}` — enables HTML rich content like `<ul>` lists in Recommendation Drivers and Cited Sources.
  - DCF section renamed: "DCF Model" → "Valuation Model", "Discounted Cash Flow Breakdown" → "Valuation Breakdown" — generic enough to cover both DCF and P/B methods.
  - Stress test section: "Scenario Impact Analysis" → "Historical Scenario Simulation". Added disclaimer about hypothetical projections.
  - Confidence scores now display `methodology` text below each bar (if available) — small gray text explaining how the score was derived.
  - Forecast chart: Added 80% confidence interval range display when `ci_low` and `ci_high` are present.
- **`src/shared/reports/html_renderer.py`**: Unchanged (no rendering-specific fixes needed).

### Analytics Trend Direction Label Fix (5215613)

- **`src/shared/reports/extraction.py`**: Analytics trend direction now uses `.replace("_", " ").title()` instead of `.capitalize()`. Fixes multi-word trend labels like "strong_bullish" → "Strong Bullish" instead of "Strong_bullish".

### Evidence Strength Section Removed (a703a49)

- **`src/shared/reports/extraction.py`**: The "Evidence Strength" auto-generated section was removed from the report. It was redundant — the reviewer validation data already captures supporting/contradicting evidence. Eliminates the duplicate "Strong evidence supports the recommendation." text in every report.

### Orchesrator Ticker Resolution Fix (0624813)

- **`src/orchestrator/agui_bridge.py`**: Report ticker now read from orchestrator state instead of fragile regex extraction from response text. Fixes ticker mismatches where the regex extracted a wrong ticker symbol.

### Full Commit List (beadc55..364e3b9)

- `364e3b9` fix: handle None fcf_age_days in DCF validator to prevent reviewer crash
- `6cc3f98` fix: correct DCF valuation inputs — WACC, growth, net debt, D/E, and add market EV
- `5215613` fix: standardize metric computations with shared MetricValue and replace raw dicts with Pydantic model constructors
- `b813204` Update generated report files: remove old JPM, add new AAPL
- `2dc0466` fix: resolve report rendering bugs and reviewer output normalization
- `bc9c59d` feat: implement plan.md DCF improvements — Sprints 1-3 complete
- `6f6eda7` feat: enhance valuation, technical analysis, and reviewer consistency
- `0624813` fix: use orchestrator state for report ticker instead of fragile regex extraction
- `0fc9c47` fix: deduplicate extraction guards, standardize valuation labels, add P/B support
- `24c309a` fix: handle P/B valuation in format_output_node reasoning string
- `966be81` feat: implement plan.md metric fixes — Sprint 1 + 2 complete
- `db152af` fix: improve report metrics quality — normalize confidence scores, enhance signal descriptions, and refine validation messages
- `03da6de` fix: resolve report narrative inconsistencies and raw anomaly leaks
- `46d6979` refactor: move range validations from reviewer tools to Pydantic Field constraints
- `a703a49` fix: improve report metrics quality — remove internal scores, add methodology
- `dceba58` fix: correct VaR(95%) validation range and forecast date check

## v2.8 — SendMessageInput Model, Pre-Reviewer Integrity Gate, Observability Dashboard, Report Calculation Fixes (1dda8cc–b3a259d)

### SendMessageInput Pydantic Model Eliminates Ticker Hallucination (7859ccc)

- **`src/orchestrator/agent.py`**: `send_message` tool signature changed from free-text `task: str` to `SendMessageInput(agent_name: str, ticker: str)` Pydantic model. Task templates code-built from `_AGENT_TASK_TEMPLATES` dict — the LLM no longer invents different tickers for different agents. Fixes a whole class of bugs where the orchestrator would analyze "NVDA" with the quant agent but "AAPL" with the RAG agent.
- **`src/shared/ticker_utils.py`**: Added "TASK" to `_FINANCIAL_STOP_WORDS` to prevent `<<<TASK>>>` separator from being extracted as a ticker.
- **AG-UI bridge**: `asyncio.wait` replaces `asyncio.wait_for` to prevent `CancelledError` propagating into ADK runner. LLM timeout increased to 600s.
- **Why**: The free-text `task` field gave the LLM unbounded freedom to specify tickers per agent call. The Pydantic model constrains the LLM to a single ticker input, making cross-agent ticker consistency the path of least resistance.

### Pre-Reviewer Metric Integrity Gate (0ca4a48)

- **`src/reviewer/tools/integrity.py` (new, 208 lines)**: `validate_metric_integrity()` checks mathematical invariants before the reviewer's synthesis step:
  - DCF upside consistency (intrinsic > market implies upside > 0)
  - MC prob_profit must be in [0,1], Sharpe in [-5,5], VaR in [0,1], RSI in [0,100], MAPE ≥ 0
  - Beta in [-3,6], momentum shift bounds, anomaly count ≥ 0
- **`src/reviewer/executor.py`**: Integrity alerts surfaced in synthesis prompt as `integrity_alerts` dict. Inline `agent_outputs` now preferred over SQLite store fetch (avoids race condition when outputs haven't been flushed yet).
- **Richer agent summaries**: LLM now receives condensed metrics per agent (beta, VaR, DCF intrinsic/value, PE, ROE, growth, D/E, RSI, golden cross, trend, MC p50/prob_profit, macro_regime, trend_strength, anomaly_count) instead of raw full outputs.

### Report Calculation Fixes (1dda8cc)

- **Sharpe ratio**: Now subtracts risk-free rate (4.3% annual / 252 daily). Previously assumed 0% risk-free rate, inflating positive-Sharpe stocks.
- **Momentum**: 20d/60d momentum values no longer double-multiplied by 100 (was showing 777% instead of 7.77%).
- **MAPE format**: Changed from `:.2%` to `:.2f%` to avoid double-percentage formatting (was showing "328.00%%").
- **Stress test**: Market decline division by 100 removed (was applying 0.5% decline instead of 50%).
- **Beta label**: Changed to `"Regression Beta (1Y)"` to distinguish from quant's 5Y covariance beta.
- **MC BUY→HOLD downgrade**: Recommendation auto-downgraded from BUY to HOLD when Monte Carlo `prob_profit < 50%` — preventing BUY signals with negative expected returns.
- **Forecast MAPE holdout**: Capped at 30 days to match forecast horizon (was 252 days, comparing against wrong timeframe).
- **Extraction key fix**: `market_context_raw` corrected from `market_raw` in extraction output.

### Unified Ticker Validation Across All Agents (b2b5f25)

- **`src/shared/ticker_utils.py`**: `resolve_and_validate_ticker()` helper extracted, replacing per-agent `_resolve_ticker`/`_validate_ticker` private methods. RAG, Quant, Market Context, Analytics, and Reviewer all call the same shared function. Eliminates 5 near-duplicate ticker resolution code paths.
- **Trace context nesting fix**: `parent_span_id=span.id` set in RAG, MarketContext, and Reviewer executors — Langfuse traces now correctly nest sub-agent spans under the orchestrator's parent span instead of appearing as disconnected root traces.
- **RAG stream simplification**: Intermediate "Index is warming for {ticker}..." WORKING event yield removed. RAG now follows the same single-yield pattern as all other agents (one data response on completion).
- **Reviewer response shape fixed**: Error responses now include `response_type`, `is_error`, `require_user_input` fields for proper A2A protocol compliance.
- **Confidence tool rewritten**: `score_confidence()` uses `_derive_narrative_bullishness`, `_derive_narrative_clarity`, `_derive_data_quality`, `_derive_agreement` helpers instead of monolithic regex parsing.
- **Contradiction tool expanded**: New cross-checks: SELL+bullish technicals, DCF vs market price misalignment, Monte Carlo vs recommendation inconsistency, analytics trend vs quant trend disagreement.
- **Validation tool expanded**: Checks fundamentals (ROE > 0, D/E < 10, growth within bounds), RSI vs trend consistency, golden cross validity, Sharpe sign vs recommendation, anomaly count plausibility, tailwind/headwind balance.
- **Eval gate timer**: Timer reset on each `defer_eval()` call (was cumulative from first call). Timeout exposed via `settings.eval_defer_timeout`.
- **`EVAL_TRACE_ENABLED` consolidated**: Canonical env var is now `EVAL_ENABLED` — `EVAL_TRACE_ENABLED` removed.

### Observability Dashboard Replaces Trace Page (e65a7fa)

- **`/dashboard` route** replaces `/trace`: KPIs, agent metrics, latency charts, RAGAS score visualizations. New `src/web/nextjs-app/app/dashboard/page.tsx`.
- **New API routes**: `/api/dashboard` (aggregated metrics), `/api/dashboard/scores` (RAGAS score timeseries).
- **`lib/traceFilter.ts` deleted**: Replaced by `lib/langfuse.ts` helper for Langfuse client queries.
- **New `lib/agentColors.ts`**: Consistent color mapping across dashboard, sidebar, and research page.
- **Sidebar**: Removed `traceOpen` from Zustand store. Nav updated to point to `/dashboard`.
- **`plan.md` deleted**: Replaced by `dashboard-plan.md`.
- **Bootstrap**: Log level resolution fixed for per-service `LOG_LEVEL_<SERVICE>` env vars.

### Web UI Agent Response Capture (b3a259d)

- **`src/orchestrator/web/agent.py`**: `_collect_agent_extra()` pops agent responses from `send_message` events and maps them into `brief_json` keys for the ADK web UI path. Previously, the web UI path never called `pop_agent_responses()` — structured agent data was silently lost on every ADK web query.
- **`src/shared/memory/ticker_memory.py`**: `update_brief_json()` extended with optional `recommendation`/`confidence` params. Both web and A2A paths now consistently detect recommendation changes and update DB columns on same-day re-analysis.

### Frontend: Analytics & Reviewer Agent Tiles (268fc8a)

- **CSS**: Added teal (analytics) and crimson (reviewer) tile color rules and spinner animations.
- **Research page**: 5-agent array with `phase` field (Phase 1 vs Phase 2). Phase-aware `tileStatus` with `running` param for stale-state safety.
- **Overview flow diagram**: Now shows 4 Phase 1 agents + Reviewer in Phase 2.
- **Operator page**: Analytics and Reviewer get correct capability card colors.
- **Trace page**: Analytics/reviewer span color matchers and legend entries.

### Refactoring: Seed User Path, Shared Re-exports, Lazy Import Reversal (9a3e14d)

- **`seed_user.py` moved**: `src/seed_user.py` → `src/scripts/seed_user.py` (consistent with other scripts).
- **`src/shared/__init__.py` (new)**: Explicit re-exports for commonly imported symbols (`get_shared_mcp`, `GenericAgentExecutor`, `BaseAgent`, `build_agent_app`, etc.).
- **Lazy imports removed** (`src/shared/memory/__init__.py`): The `__getattr__`-based lazy import pattern (introduced in v2.1) replaced with explicit direct imports. The lazy pattern caused IDE resolution failures and obscured import errors at startup.
- **Agent server debug logging**: Added `agent_card` debug log statements.
- **Makefile**: Added `fmt` and `fmt-check` targets for ruff formatting.

### Full Commit List (c51db16..b3a259d)

- `1dda8cc` fix: correct report calculation bugs — momentum, MAPE, stress test, Sharpe, MC recommendation
- `0ca4a48` feat: add pre-reviewer metric integrity gate
- `b2b5f25` refactor: unify ticker validation, fix trace context nesting, clean up dead code
- `268fc8a` feat: add Analytics & Reviewer agent tiles to frontend
- `9a3e14d` Implement plan.md: seed_user move, shared re-exports, _build_response refactor, lazy imports, inline _extract, memory __getattr__ removal, agent_card comments, Makefile fmt targets
- `3401189` Format entire codebase with ruff format
- `7859ccc` fix: resolve orchestrator cancellation, reviewer data loss, and ticker hallucination
- `e65a7fa` feat: replace trace page with observability dashboard, update docs and bootstrap logging
- `b3a259d` fix: capture agent responses in web UI path and update stale recommendations

## v2.7 — Shared Agent Output Store, Rich HTML Report Sections, Reviewer Refactor, Auth Bypass (dc6ec77)

### Shared SQLite Agent Output Store (dc6ec77)

- **`src/shared/memory/agent_output_store.py` (new, 75 lines)**: SQLite table `agent_output_store` keyed by `(session_id, agent_name)`. Three functions: `store_agent_output(session_id, agent_name, output)` persists full structured agent output, `get_agent_outputs(session_id)` returns all outputs for a session as a dict keyed by agent_name, `prune_stale_outputs(max_age=600)` deletes rows older than TTL and returns count removed.
- **`src/shared/memory/store.py`**: Schema v5→v6 migration adds `agent_output_store` table. `init_db()` now runs v6 migration.
- **`src/shared/memory/__init__.py`**: Lazy imports for `store_agent_output`, `get_agent_outputs`, `prune_stale_outputs`.
- **`src/orchestrator/agent.py`**: `send_message` callback now calls `store_agent_output()` to persist sub-agent responses before returning. Reviewer payload changed from inline JSON to `{"ticker": "...", "session_id": "..."}` only.
- **`src/orchestrator/main.py`**: Startup calls `prune_stale_outputs(600)` to clean expired outputs.
- **Why**: Eliminates inline JSON payload bloat in A2A messages (agent outputs could exceed 50 KB). Provides cross-process persistence so reviewer and orchestrator share data without per-process coupling.

### Reviewer Synthesis-Only Refactor (dc6ec77)

- **`src/reviewer/executor.py`**: Tools called directly in Python instead of OpenAI Agents SDK `Runner.run()`: `check_contradictions()`, `verify_sources()`, `score_confidence()`, `validate_recommendation()`. These run in ~4 ms total — no LLM round-trips. Only the final synthesis step uses the LLM, receiving compressed agent summaries.
- **`src/reviewer/agent.py`**: System prompt simplified — no longer receives full agent output payloads. Receives only `agent_summaries` dict with condensed findings from each sub-agent.
- **Why**: SDK Runner introduced ~20s overhead for handshake + context setup. The reviewer is a batch verification pipeline, not an interactive agent. Direct Python calls are simpler, faster, and have no SDK versioning risks.

### 10+ New HTML Report Sections with Chart.js (dc6ec77)

- **`src/shared/reports/deck_model.py`**: 14 new `DeckData` fields: `technicals_table`, `signals_table`, `forecast_chart`, `monte_carlo_summary`, `stress_scenarios`, `dcf_breakdown`, `stats_table`, `anomaly_alerts`, `trend_data`, `reviewer_contradictions`, `reviewer_verifications`, `reviewer_confidence`, `reviewer_validation`, `macro_regime`.
- **`src/shared/reports/extraction.py`**: New extraction logic for each section — trend analysis, forecast distribution data, Monte Carlo percentiles (p10/p50/p75/p90), stress scenario cards, DCF sensitivity breakdown, anomaly detection, statistical metrics table, and cross-validation verdict fields.
- **`src/shared/templates/investment_deck.html`**: 768→1609 lines. Chart.js 4.4.4 from CDN embedded in Jinja2 template. Interactive charts: forecast distribution (bar+line mixed), Monte Carlo histogram, trend overlay (price + SMA 20/50), stress scenario bars, DCF breakdown doughnut, anomaly alert badges, signals table with color coding.
- **Why**: Chart.js from CDN enables interactive charts without bundler, build step, or server-side rendering. Reports are self-contained HTML files.

### Frontend Auth Bypass (dc6ec77)

- **`src/web/nextjs-app/contexts/AuthContext.tsx`**: `NEXT_PUBLIC_AUTH_ENABLED=false` short-circuits all auth checks (no token validation, no login redirect, no HTTP calls to auth endpoints).
- **`.env.example`**: Added `NEXT_PUBLIC_AUTH_ENABLED=false` commented-out entry.
- **Why**: Independent frontend auth toggle from backend `AUTH_ENABLED`. Single env var would require Next.js rebuild on every auth config change.

### Test Fixes (dc6ec77)

- **Windows file locking fix**: `test_auth_routes.py` uses `tmp_path` per-test database files to avoid SQLite locking on Windows where `NamedTemporaryFile` cannot be reopened while open.
- **DDG mock pattern**: `test_web_search_tool.py` mocks DuckDuckGo at the `aiohttp.ClientSession` level instead of `DDGS` to avoid real HTTP calls during CI.
- **New test file**: `src/tests/unit/memory/test_agent_output_store.py` (10 tests) — store/get/prune, cross-agent isolation, TTL expiry, session scoping.

### Config Changes (dc6ec77)

- `.env.example`: Default model changed to `ministral-3-14b-reasoning` for all agent configs.
- `AGENT_SEED_URLS` default expanded to include `:8005` (Analytics) and `:8006` (Reviewer).
- CI test job count updated to include analytics/reviewer agent contract tests.

## v2.6 — Analytics & Reviewer Agents, Two-Phase Orchestration

### New Agents (2 agents, 5 new test files)

- **Analytics Agent** (`src/analytics/`, PydanticAI, port 8005): Trend analysis, forecast computation, anomaly detection, statistical metrics. Graph node functions in `src/analytics/nodes.py`: `trend_node` (linear regression, moving averages), `forecast_node` (ARIMA-like projection), `stats_node` (mean, std, skew, kurtosis), `anomaly_node` (z-score based outlier detection), `chart_node` (chart-ready data formatting).
- **Reviewer Agent** (`src/reviewer/`, OpenAI Agents SDK, port 8006): Cross-validation of Phase 1 agent outputs. Deterministic tools: `check_contradictions()` (flags conflicting recommendations across agents), `verify_sources()` (checks source citation completeness), `score_confidence()` (aggregates confidence scores with weighted averaging), `validate_recommendation()` (checks BUY/HOLD/SELL consistency with underlying data).
- **New model** (`src/shared/agent_models.py`): `AnalyticsAgentOutput`, `ReviewerAgentOutput` Pydantic models.
- **5 new test files**: `test_analytics_nodes.py` (10 tests), `test_reviewer_tools.py` (8 tests), `test_new_agent_models.py` (4 tests), `test_analytics_smoke.py` (2 integration), `test_reviewer_smoke.py` (2 integration).

### Two-Phase Orchestration (Phase 1 + Phase 2 + Synthesis)

- **Review phase added** (`src/orchestrator/agent.py`): After all Phase 1 responses collected, orchestrator calls `send_message` for Reviewer Agent with JSON payload containing all agent outputs. Reviewer runs 4 deterministic tools, produces cross-validation verdict.
- **Synthesis enhanced**: LLM receives Phase 1 outputs + Reviewer's cross-validation verdict before producing final BUY/HOLD/SELL recommendation.

### AG-UI Eval Hook

- **`src/orchestrator/agui_bridge.py`**: `_stream` now fires `score_response()` and `_release_sub_agent_evals()` after synthesis completes. Previously evals only ran through the A2A executor and ADK Web paths — the most common user-facing path (CopilotKit) silently skipped all runtime scoring.

## v2.5 — Pydantic Agent Models, Scrollable HTML Reports, Fan-In Fix, AG-UI Eval Hook (9664a84–5b57c3e)

### Pydantic Output Models for All Agents (f543de8)

- **`src/shared/agent_models.py` (new, 258 lines)**: Typed Pydantic models at every agent boundary replacing ~220 lines of fragile `.get()`/regex extraction chains. Models: `QuantAgentOutput`, `MarketContextOutput`, `RAGAgentOutput`. Fixes zeroed KPI chips (fundamentals fallback), raw JSON narrative from CrewAI (`output_pydantic`), and DCF key mismatch (`intrinsic_value` vs `fair_value`). Validated path falls back to legacy dict extraction for old briefs without agent output metadata.
- **`src/shared/reports/extraction.py`**: `_populate_from_agent_outputs()` now uses typed model parsing via `model_validate()` with `mode="json"`. Falls back to legacy dict extraction when `extra_data` is missing or malformed.
- **`src/quant/graph.py`**: Quant agent graph output now validated through `QuantAgentOutput` model before returning to orchestrator.
- **`src/market_context/crew.py`**: CrewAI crew now uses `output_pydantic=MarketContextOutput` to enforce structured JSON output from the LLM instead of relying on prose parsing.
- **`src/financial_rag/index_manager.py`**: RAG agent output validated through `RAGAgentOutput` model.

### Scrollable HTML Report & A4 PDF (fe405d0)

- **Template replaced** (`src/shared/templates/investment_deck.html`): The deck-stage slide presentation template replaced with a full scrollable HTML page using the same design theme. Sticky PDF download bar, responsive layout, section break-inside-avoid.
- **PDF generation** (`src/shared/reports/playwright_export.py`): Playwright renders A4 portrait with 18/16/20/16mm margins. Print CSS: cover page, section breaks, conclusion back page.
- **Frontend simplified** (`src/web/nextjs-app/app/research/page.tsx`): PPTX and DOCX download options removed. HTML + PDF only.
- **Extraction limit increased** (`src/shared/reports/extraction.py`): Executive summary limit raised from 1200 to 4000 chars for the scrollable format.

### AG-UI Bridge Eval Hook & Sentence-Aware Truncation (24d0807)

- **Missing eval hook** (`src/orchestrator/agui_bridge.py`): `_stream` now calls `score_response()` and `_release_sub_agent_evals()` after synthesis. Previously evals only ran through the A2A executor and ADK Web paths — the AG-UI bridge (used by CopilotKit frontend) silently skipped all runtime scoring.
- **Sentence-aware truncation** (`src/shared/reports/extraction.py`): New `_truncate_at_sentence(text, max_chars)` prevents mid-word cuts in executive summary and market narrative fields. Truncates at the last sentence boundary before `max_chars`.
- **Debug logging** (`src/market_context/crew.py`): Added structured debug logging for CrewAI crew output parsing, including raw output type and length.

### CrewAI Future Annotations Fix & seed_user.py (47f2a7d)

- **`from __future__ import annotations` removed** (`src/shared/agent_models.py`): CrewAI's `generate_model_description` reads `__annotations__` directly; `from __future__ import annotations` stringifies them, causing `AttributeError` on `field_type.__name__` for generic types like `list[str]`.
- **`src/scripts/seed_user.py` (new)**: Script for creating test login credentials with Argon2 hashing. Used by CI and manual testing.

### Quant Fan-In Redundant LLM Fix (9664a84)

- **Data-readiness guard** (`src/quant/nodes/summary.py`): `llm_summary_node` now checks that all predecessor branches (`metrics`, `reasoning`, `recommendation`, `fundamentals`) have written their data before firing the LLM call. The 5-way fan-in at `format_output` caused LangGraph to fire `format_output` (and consequently `llm_summary`) multiple times as predecessors completed in different supersteps — resulting in 4 sequential LLM calls (~78s) instead of 1.
- **Duplicate LangChainInstrumentor removed** (`src/shared/observability.py`): The quant agent's auto-instrumentation was doubling every span because the executor already passes a Langfuse `CallbackHandler` into LangGraph. Removed the redundant `LangChainInstrumentor().instrument()` call.

### Case-Insensitive Username Matching (5b57c3e)

- **Username normalization** (`src/shared/memory/user_store.py`): Usernames normalized to lowercase at creation and lookup. Login now accepts any casing (e.g. "Admin" matches "admin").
- **Test isolation fix** (`src/shared/memory/user_store.py`): `_schema_v4_ensured` flag now reset between tests to prevent cross-test contamination.

## v2.4 — Auth Public Endpoints, ADK Web Discovery Fix, RAG Filing Source Switch (d84a3cc–274d4b5)

### Agent Discovery in ADK Web Mode (d84a3cc)

- **`before_agent_callback` triggers discovery** (`src/orchestrator/web/agent.py`): When running under `adk web`, `main.py`'s lifespan handler is never used — agent discovery now fires inside `_memory_cache_callback` on the first turn. A module-level `_discovery_done` flag ensures discovery runs exactly once.
- **`root_agent.instruction` made callable** (`src/orchestrator/agent.py`): Changed from a static string to `_instruction_provider(_ctx=None)` — a callable that invokes `_build_instruction()` on every turn. This ensures newly discovered agents are reflected in the system prompt dynamically, without requiring a process restart.
- **`services.py` moved to `orchestrator/`** (`src/orchestrator/services.py`): ADK's `load_services_module()` rewrites `agents_dir` to the parent directory when it detects `web/agent.py` exists. The file now lives at `src/orchestrator/services.py` so ADK can find it. Added docstring explaining this constraint.

### RAG Filing Ingestion: `get_financial_filings` (d84a3cc)

- **MCP tool switch** (`src/financial_rag/executor.py`): `_ensure_ingested` switched from `get_company_filings` (which mixed 8-Ks with 10-K/10-Qs and drowned out annual/quarterly reports for large filers) to `get_financial_filings` with `annual_limit=3, quarterly_limit=4`. The response format changed from a flat `filings[]` to separate `annual[]` + `quarterly[]` arrays, both concatenated for the ingestion pipeline.

### Auth Hardening & Public Endpoints (d84a3cc, 6110b9d, 274d4b5)

- **`SERVICE_AUTH_TOKEN` wired** (`src/orchestrator/agent.py`): `SubAgentClient` now accepts a `bearer_token` parameter from `settings.service_auth_token`. When `AUTH_ENABLED=true`, orchestrator-to-sub-agent A2A requests carry the service bearer token.
- **`runtime_eval` Langfuse client fix** (`src/shared/runtime_eval.py`): `_push_scores` switched from creating a new `Langfuse()` instance to using `get_langfuse_client()` from `shared.observability` — prevents duplicate Langfuse client creation and ensures the shared singleton is used.
- **`/api/agents` made public** (`src/orchestrator/api_routes.py`, `src/shared/auth/middleware.py`): The endpoint no longer requires admin role, and `PUBLIC_PREFIXES` includes `/api/agents` so the Next.js operator page can fetch agent lists without auth headers.
- **`/api/reports` made public** (`src/shared/auth/middleware.py`): Added to `PUBLIC_PREFIXES` so frontend report download fetches (which go through Next.js rewrites without auth headers) work for authenticated users.

### Frontend Auth Middleware & Redirect (0bbfa0d, fcf4000, ff9664b)

- **Next.js auth middleware** (`src/web/nextjs-app/middleware.ts`): Reads `finsight_session` cookie and redirects unauthenticated users to `/login?redirect=<original_path>`. Created as part of 0bbfa0d.
- **`AuthProvider` redirect replaces middleware** (`src/web/nextjs-app/contexts/AuthContext.tsx`): Next.js 16 deprecated middleware in favor of proxy. The auth guard moved into `AuthProvider` using `useEffect` that redirects to `/login` when no user is authenticated and redirects away from `/login` when a session exists. `middleware.ts` deleted (fcf4000).
- **Service token fallback in CopilotKit route** (`src/web/nextjs-app/app/api/copilotkit/route.ts`): When no user JWT is available (anonymous/unauthenticated users), the route handler falls back to `SERVICE_AUTH_TOKEN` in the `X-FinSight-Auth-Token` header so the orchestrator's `/a2a-agui` endpoint accepts the request.

### .env.example Expansion (d84a3cc)

- **All missing env vars added**: `AUTH_ENABLED`, `AUTH_SECRET_KEY`, `AUTH_TOKEN_EXPIRE_MINUTES`, `SERVICE_AUTH_TOKEN`, `REPORT_DIR`, `DB_PATH`, `SANDBOX_MODE`, `CORS_ORIGINS`, `ORCHESTRATOR_PORT`, `RAG_PORT`, `QUANT_PORT`, `MARKET_CONTEXT_PORT` — making the example a complete reference.

## v2.3 — Source Tree Restructure to `src/` Layout (ef596a7)

### Directory Layout Migration (ef596a7)

All Python packages, web frontend, tests, and scripts moved under `src/`:

- **Agent packages moved**: `agent_1_adk/` → `src/orchestrator/`, `agent_3_langgraph/` → `src/quant/`, `agent_4_crewai/` → `src/market_context/`, `mcp_servers/` → `src/mcp_tools/`, `shared/` → `src/shared/`
- **Web frontend moved**: `web/` → `src/web/`
- **Tests moved**: `tests/` → `src/tests/`
- **Scripts moved**: `scripts/` → `src/scripts/`
- **All configuration updated**: `pyproject.toml` (packages.find where=["src"]), Makefile (mypy targets), docker-compose.yml + 5 Dockerfiles (COPY paths), CI workflow (mypy, pytest, frontend, openapi, Docker build matrix), batch files (Next.js and ADK web directory references)
- **Path traversal fixes**: 6 files updated (logging, store, agent_registry, openapi, etc.)
- **Missing `__init__.py`** created in `src/mcp_tools/`
- **13 documentation files** updated with new `src/` paths
- **218 unit tests passing**, all 5 package imports verified

### Model Rename: SentimentIntelligence → MarketContext (da2688e)

- **`shared/models.py`** → `src/shared/models.py`: `SentimentIntelligence` renamed to `MarketContext` with restructured fields: `key_risks`/`catalysts` → `key_headwinds`/`key_tailwinds`, added `macro_regime` and `relative_peer_positioning` fields.
- **`shared/reports/extraction.py`** → `src/shared/reports/extraction.py`: Updated all references to use the new `MarketContext` model and field names.
- **Asset rename**: All references to "Sentiment agent" replaced with "Market Context agent" across codebase and docs.

## v2.2 — Agent Output Capture, Playwright Export, Ticker Hardening

### Agent Output Capture for Structured Brief Storage (db6472e, e505b33)

- **Sub-agent response capture** (`agent_1_adk/agent.py`): `send_message` tool callback now captures parsed sub-agent responses (RAG summary, quant metrics, sentiment narrative) and stores them in session event metadata via an `extra_data` parameter on `store_minimal()`. This enables downstream extraction from structured agent outputs instead of parsing prose.
- **Extraction pipeline wired to agent outputs** (`shared/reports/extraction.py`): New `_populate_from_agent_outputs()` extracts quant KPIs, financials, valuation, scorecard, peer comparison, RAG summary/sources, and sentiment risks/opportunities from structured agent data. Falls back to prose extraction when agent outputs are unavailable.
- **`TickerMemory.store_minimal()` extended** (`shared/memory/ticker_memory.py`): Accepts optional `extra_data` dict stored as JSON in the brief record. Used by the orchestrator to persist structured agent responses alongside the synthesis text.

### Playwright-Based Report Export (7d52cbf)

- **`shared/reports/playwright_export.py` (new, 77 lines)**: Async Playwright-based export from HTML to PPTX (screenshot-based via `html_to_pptx`) and PDF (print-mode via `html_to_pdf`). Uses deck-stage's built-in APIs for slide rendering. Falls back to HTML download when Playwright is unavailable.
- **`shared/reports/__init__.py`**: New `generate_pptx_async()` and `generate_pdf_async()` functions wire Playwright export with legacy fallback. `generate_pptx()` now tries Playwright first (screenshot-based PPTX) before falling back to python-pptx.
- **`agent_1_adk/api_routes.py`**: PDF format added to report endpoints. Routes support `format=pptx|docx|html|pdf`.
- **`pyproject.toml`**: Added `playwright>=1.40.0` dependency.
- **Frontend download buttons** (`web/nextjs-app/app/research/page.tsx`): 4 download buttons (PPTX, DOCX, HTML, PDF) with HTML as primary.

### Agent Output Extraction Tests (f493288)

- **`tests/unit/test_agent_outputs_extraction.py` (new, 409 lines)**: 16 tests covering `_populate_from_agent_outputs` (quant KPIs/financials/valuation/scorecard/peers, RAG summary/sources, sentiment risks/opportunities, string/empty edge cases) and `_extract_deck_data` routing (agent path, prose fallback, partial agents). End-to-end PPTX+HTML generation from structured agent data.

### Model Rename: SentimentIntelligence → MarketContext (da2688e)

- **`shared/models.py`**: `SentimentIntelligence` renamed to `MarketContext` with restructured fields: `key_risks`/`catalysts` → `key_headwinds`/`key_tailwinds`, added `macro_regime` and `relative_peer_positioning` fields.
- **`shared/reports/extraction.py`**: Updated all references to use the new `MarketContext` model and field names.

### Ticker Extraction Hardening (70a4b81, 3779574)

- **English pronouns added to stop words** (`shared/ticker_utils.py`): Common 1-2 letter English words (I, AM, AN, IF, IT, IS, IN, AT, etc.) added to `_FINANCIAL_STOP_WORDS`. Prevents the pronoun "I" from being extracted as a ticker symbol.
- **Holdings extraction tightened** (`shared/ticker_utils.py`): Added `\b` word boundaries to all `_HOLDINGS_PATTERNS` regexes so partial-word prefixes (e.g. "everything" → "EVERY") are not captured. Pattern 4 now requires "I" or "we" subject. Extracted holdings filtered against stop words and noise words.

### Anonymous User ID Persistence (b0b1d4c)

- **Stable anonymous user ID** (`web/nextjs-app/app/api/copilotkit/route.ts`): Next.js API route generates a stable `anon-{uuid}` on first visit and sets it as a `finsight_user_id` cookie (1-year TTL). Subsequent requests send the same ID via `X-FinSight-User-Id` header, so `_get_today_cached_text` and `_build_memory_context` find existing briefs instead of creating new anonymous users on every request.

### AG-UI Bridge Per-Event Timeout (7e104ea)

- **Dynamic per-event timeout** (`agent_1_adk/agui_bridge.py`): When the LLM is unavailable (model removed from LM Studio), `runner.run_async()` now times out after 120s for LLM response or `A2A_TIMEOUT+30s` for tool/sub-agent execution. Sends a `RunError` event to the frontend with a clear message about model unavailability instead of hanging indefinitely.

### Extraction Pipeline Fixes (5fb9543, 3241ce5, a17bf96, a26f9b7, 34e95c9, e5b253f)

- **Numeric MACD handling** (`shared/reports/extraction.py`): `_enrich_from_markdown()` now handles MACD values that are numeric (float/int) instead of only string format.
- **Playwright ProactorEventLoop fix** (`shared/reports/playwright_export.py`): Uses `asyncio.new_event_loop()` on Windows to avoid ProactorEventLoop incompatibility with Playwright's subprocess management.
- **HTML section extraction**: `_parse_markdown_sections()` handles edge cases in section boundary detection for nested heading levels.
- **Narrative JSON parsing**: `_extract_deck_data()` gracefully handles sentiment narrative fields that arrive as JSON strings instead of dicts.
- **Sync `generate_pptx()` tries Playwright first** (`shared/reports/__init__.py`): `generate_pptx()` sync wrapper now calls `html_to_pptx_sync()` (Playwright screenshot-based PPTX) before falling back to python-pptx legacy renderer, matching the async path behavior.
- **PDF fallback on Playwright error** (`shared/reports/playwright_export.py`): `generate_pdf_async()` catches Playwright exceptions and returns raw HTML bytes instead of propagating the error — ensures PDF download never fails when Playwright is unavailable.
- **Context ID pass-through** (`agent_1_adk/agent_executor.py`): `context_id` threaded through `_process_response` to fix session ID mismatch when popping agent outputs — the ADK runner's internal session ID may differ from the A2A `context_id`.
- **Dedup merge fix** (`agent_1_adk/agent_executor.py`): `_store_memory` now merges new agent outputs into an existing brief via `update_brief_json` instead of discarding them on dedup hits. Previously, repeat queries overwrote structured agent data with empty dicts.
- **`update_brief_json` method** (`shared/memory/ticker_memory.py`): New `TickerMemory.update_brief_json()` for partial brief updates — merges `extra_data` into an existing brief's `brief_json` without overwriting the full record. Used by the dedup merge fix above.
- **Debug logging for session ID tracing** (`agent_1_adk/agent.py`, `agent_1_adk/agent_executor.py`): Added structured log lines to `send_message` capture and `pop_agent_responses` with session ID and context ID for traceability across the A2A bridge.
- **`</script>` fix in deck-stage.js** (`shared/templates/deck-stage.js`): A comment containing `</script>` inside the inline JS broke HTML parsing — the browser's HTML parser interpreted it as closing the parent `<script>` tag. Fixed by splitting the string literal so `</s` + `cript>` does not appear contiguously.
- **Playwright `wait_for_function` reliability** (`shared/reports/playwright_export.py`): Wait predicate now checks `customElements.get('deck-stage')` before querying `_slides` — prevents `ReferenceError` when the custom element definition hasn't finished upgrading.
- **Removed obsolete refactor plan**: Deleted stale `refactor-plan.md` (refactor already completed). Added `ppt-docx-fix-plan.md` documenting known PPTX/DOCX rendering issues.
- **Agent output capture in AG-UI bridge** (`agent_1_adk/agui_bridge.py`): Bridge path now captures sub-agent responses for structured brief storage, matching the A2A executor path.
- **Today-cache bypass for bridge**: AG-UI bridge path correctly bypasses today-cache when the user explicitly requests a fresh analysis.
- **Silent Playwright fallbacks logged**: When Playwright export fails, the fallback to python-pptx/HTML is logged at INFO level instead of silently degrading.

## v2.1 — Post-Phase-3 CI Hardening & Runtime Fixes

### Runtime Error Resolutions (9264985)

- **A2A AgentCard security scheme API**: Fixed protobuf `get_or_create`/`.values` usage — `securitySchemes` and `securityRequirements` use `StringList`/`RepeatedCompositeContainer` correctly.
- **LangGraph node name collision**: `peer_comparison` and `insider_signals` node names conflicted with `QuantAnalysisState` key names — renamed to avoid `INVALID_CONCURRENT_GRAPH_UPDATE`.
- **Protobuf Part serialization** (`sub_agent_client.py`): `MessageToDict` replaces manual dict construction for `Part` protobuf messages — prevents `TypeError` on non-serializable fields.
- **CrewAI instrumentor crash** (`crewai>=0.95`): `try/except ModuleNotFoundError` around CrewAIInstrumentor import — prevents crash when `crewai` is not installed in the test env.
- **LANGFUSE_BASE_URL resolution**: Pydantic `AliasChoices` replaces `os.environ` fallback — `LANGFUSE_BASE_URL` env var now correctly overrides the default.
- **Sidebar hydration mismatch** (`Sidebar.tsx`): `getRecentQueries` deferred to `useEffect` — prevents Next.js SSR hydration error from localStorage reads.
- **Proxy.ts disabled**: `proxy.ts` renamed to `proxy.ts.disabled` — the middleware forced login redirect to CopilotKit Cloud even with `AUTH_ENABLED=false`.

### CI Pipeline Hardening (096922e–b2dc5a4)

- **Ruff cleanup**: `ruff check --fix` + `ruff format` across 121 files (4581 insertions, 2256 deletions). Auto-fixed 221 issues (E501 line length, I001 import sort, E402 import order, F401 unused imports, E701 statement style). Inline `# noqa: E501` on unbreakable long strings. File-level `# ruff: noqa: E402` on entrypoints with bootstrap-before-import pattern. Added `ExtractionCtx` to `shared/reports/__init__.__all__` (F401). Renamed `l` → `lbl` in extraction generators (E741).
- **uv venv for Python jobs**: All CI Python jobs now create a uv venv before installing — fixes `PermissionError` on editable installs with `uv pip install --system`.
- **--break-system-packages flag**: `UV_BREAK_SYSTEM_PACKAGES` env var doesn't work with `uv`; the `--break-system-packages` flag must be passed directly to each `uv pip install --system` call for PEP 668 compliance on Ubuntu 24.04.
- **`__init__.py` for mypy**: Added missing `__init__.py` files in `agent_1_adk/` and `shared/` packages. Extended mypy overrides to `shared.*` and `agent_1_adk.*` (never strict-clean).
- **Docker build context fix**: Dockerfiles reference `pyproject.toml` and `shared/` from repo root, but CI was using agent subdirectory as context. Changed to `-f` flag with `.` context.
- **Slim test deps**: Test job installs only ~15 packages instead of all 293 base deps (avoids PyTorch, CUDA, all agent frameworks) via `--no-deps -e .`. Scope test job to `tests/unit/` only. Enable uv cache on all Python CI jobs.
- **Lazy memory imports**: `shared/memory/__init__.py` imports made lazy via `__getattr__` — importing `store.py` or `ticker_memory.py` no longer pulls in `google.adk`.
- **22 test failure fixes**:
  - `extract_ticker()` case bug: removed `query.upper()` that turned every word into a ticker candidate
  - Isolated `test_settings` from `.env` file and CI `AUTH_ENABLED` env var leak
  - Isolated `TestAuthOff` from CI `AUTH_ENABLED` env var
  - Wrapped `agents_list` handler in try/except for missing `google.adk`
  - Added `pytest.importorskip("yfinance")` for deck extraction tests
  - Used neutral metrics in HOLD test to avoid weight redistribution
  - Fixed signal_scores nesting in runtime_eval gate test
  - Set `SANDBOX_MODE=disabled` in production validation test for Windows
- **Coverage/pytest hang fixes**: `uv run pytest --cov` hangs indefinitely after tests complete. Replaced with coverage run directly via venv binary, then removed coverage entirely — run pytest directly. Added `pytest_unconfigure` hook that detects leaked non-daemon aiosqlite threads and `os._exit()`s with real session status. 5-minute timeout on CI test step as safety net.
- **`_REPORTS_OFFLINE` patching**: CI sets `REPORTS_OFFLINE=true` which skips the yfinance code path at module import time. Tests now patch the module-level flag to `False` so the yfinance mock is actually exercised.
- **Google ADK stubs in slim CI**: `google.adk`/`google.genai` stubs added to `test_save_brief_persists_synthesis` — prevents `ModuleNotFoundError` when ADK is not installed in slim test env.

### Documentation Sync (ed9293c)

- **`.md` → `.html` sync for 9 doc pairs**: All Markdown documentation files covering versions v1.41 through v2.1 regenerated as HTML — `CHANGELOG.md → CHANGELOG.html`, `AGENTS.md → AGENTS.html`, `ARCHITECTURE.md → ARCHITECTURE.html`, `SECURITY.md → SECURITY.html`, `DEMO.md → DEMO.html`, `API.md → API.html`, `UNIFIED_IMPLEMENTATION_PLAN.md → UNIFIED_IMPLEMENTATION_PLAN.html`, plus diagram pages. Ensures the browser-readable `docs/` HTML files stay current with their Markdown sources after Phase 3 content updates.

## v2.0 — Phase 3: Quality, Observability, Docs & Shim Removal

### WP 3.1 — Auth Contract Tests & CI Matrix
- **Auth matrix CI**: `AUTH_ENABLED={false,true}` matrix in CI; tests run twice (auth on/off).
- **Auth marker**: `pytest -m auth` for auth-related tests; `pytest -m openapi` for spec tests.
- **Contract tests**: `test_auth_contract.py` — parametrized auth × route matrix: every REST route × {auth on/off} × {none, user, service, admin}. Covers public paths, protected routes, admin gating.
- **A2A protocol test**: `test_a2a_protocol.py` — in-process `GenericAgentExecutor` lifecycle: WORKING→artifact→COMPLETED, FAILED, CANCELED, INPUT_REQUIRED, structured data artifacts.
- **Coverage gates**: `test-auth` Makefile target; coverage config in pyproject.toml.

### WP 3.2 — FastAPI Sub-App & OpenAPI Spec
- **Response models**: Extended `shared/models.py` with `HealthResponse`, `ErrorResponse`, `MemoryTickerItem`, `MemoryTickerChangedResponse`, `SessionListItem`, `SessionEventsResponse`, `AgentListItem`, `AgentHealthResponse`, `LoginRequest`, `LoginResponse`, `TokenResponse`, `MeResponse`, `LogoutResponse`, `UserInfo`.
- **FastAPI spec app**: `agent_1_adk/api_fastapi.py` — FastAPI app shadowing all REST + auth routes with response models, tags, and summaries. Used exclusively for OpenAPI spec generation.
- **OpenAPI spec generated**: `docs/openapi.json` — 13 paths, 16 schemas, auto-generated.
- **Spec regeneration script**: `scripts/generate_openapi.py` — `python scripts/generate_openapi.py` writes spec; `--check` verifies it's current.
- **CI enforcement**: `openapi` job checks spec is up to date.

### WP 3.3 — Langfuse User Context & Trace Filter
- **User_id propagation**: `trace_with_user()` helper in `shared/observability.py` tags Langfuse observations with `current_user_id` from ContextVar.
- **Auth denied logging**: Structured `auth.denied reason=... path=...` log lines in `AuthMiddleware` for missing header, short token, wrong kind, and invalid token.
- **Trace filter**: `web/nextjs-app/lib/traceFilter.ts` — heuristics classifying trace entries by auth category (`auth_denied`, `auth_success`, `auth_lockout`). `filterAuthTraces()`, `countAuthDeniedByReason()`, `countAuthDeniedByUser()` utilities.

### WP 3.4 — Documentation Updates
- **CHANGELOG**: Phase 3 entries added.
- **ARCHITECTURE.md**: Trust-boundary diagram for A/B/C auth boundaries.
- **CI docs**: OpenAPI spec check job documented.

### WP 3.5 — Shim Removal & Cleanup
- **`shared/config.py` removed**: All imports migrated to `shared.settings`. Last known consumers updated.
- **`shared/report_generator.py` removed**: All imports migrated to `shared.reports`. Last known consumers updated.
- **`ppt-generation-fix.md` removed**: All six fix markers verified applied in `shared/reports/` (WP 1.2). Doc was stale and no longer matched line numbers.
- **ruff ratchet**: `ruff check .` clean.
- **mypy ratchet**: `mypy shared agent_1_adk` clean.
- **Zero DeprecationWarning**: Suite emits no deprecation warnings.

### WP 3.6 — Auth Audit Gap Fixes (01eaafa)
- **`TRUSTED_PROXIES` setting**: `_client_ip()` now only reads `X-Forwarded-For` when the direct peer IP is in `TRUSTED_PROXIES`. Empty list (default) always uses socket address — closes IP-spoofing lockout bypass (EC5).
- **`sub_agent_client.py` NameError fix**: `__get_data_parts` renamed to `_get_data_parts` (double underscore caused `NameError` at both call sites — line 280, 387). `a2a.types` import block moved above the function.
- **`ppt-generation-fix.md` deleted**: Stale audit doc — all six markers verified applied in `shared/reports/`. Line numbers no longer matched codebase.
- **SECURITY.md updated**: Documented trusted-proxy lockout behaviour and known username-DoS tradeoff. Fixed stale `shared/config.py` reference.

## v1.43 — Phase 2: Full Auth Implementation (WP 2.1–2.7)

### WP 2.1 — Shared Auth Toolkit
- **`shared/auth/` package**: `tokens.py` (JWT generation/validation with HS256, access+refresh tokens), `middleware.py` (Starlette ASGI `AuthMiddleware` with path-based exemptions, principal-kind routing), `audit.py` (structured auth.denied logs).
- **Principal-kind routing**: Middleware accepts path-specific `accept` sets (`{"user"}`, `{"service"}`, `{"user","service"}`). Orchestrator `/api/*` paths accept user tokens; sub-agent `/a2a` paths accept service tokens; `/health` paths are public.

### WP 2.2 — User Store & Auth Routes
- **`shared/memory/user_store.py`**: Argon2id password hashing, user CRUD, refresh token management with rotation. SQLite-backed.
- **`agent_1_adk/auth_routes.py`**: `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout` with httpOnly refresh cookie + JSON access token response.
- **Next.js auth UI**: `web/nextjs-app/app/login/page.tsx` — login form, `AuthContext.tsx` — React context for auth state, `Providers.tsx` — wraps app with auth provider, `lib/auth.ts` — client-side token management.
- **Rate-limited lockout**: 5 failed attempts → 60s cooldown per username+IP. IP from socket address unless peer is in `TRUSTED_PROXIES`.
- **`scripts/create_user.py`**: Interactive user creation with Argon2 password hashing.

### WP 2.3 — Resource Scoping
- **`ticker_memory.py`**: Brief queries filtered by `user_id` — users only see their own briefs.
- **`session_repo.py`**: Session queries filtered by `user_id` — users only see their own sessions.
- **`api_routes.py`**: All memory/session/agent endpoints read `X-FinSight-User-Id` header for user identity.
- **`semantic_cache.py`**: Cache keys include `user_id` — multi-user cache won't cross-pollinate.

### WP 2.4 — A2A Service Auth
- **`sub_agent_client.py`**: Service bearer token injected into `A2AClientRequest.default_headers`. Propagated to sub-agents on every A2A call.
- **`agent_server.py`**: `AuthMiddleware(accept={"service"})` wraps sub-agent A2A endpoints (`/a2a`, `/release-evals`). Service token validated before any A2A handler runs.
- **User context propagation**: `_user` envelope in A2A message metadata carries `current_user_id` across service boundaries.

### WP 2.5 — MCP Service Auth
- **`finsight_server.py`**: `AuthMiddleware(accept={"service"})` wraps the SSE mount. Only valid service tokens can establish SSE connections.
- **`mcp_client.py`**: `MCPServerConfig` carries `token` field — injected as `Authorization: Bearer <token>` in the SSE `Url` header on connect.

### WP 2.6 — Sandbox Hardening
- **Container mode**: `SANDBOX_MODE=container` runs user code in `docker run --rm --network none --memory 512m --cpus 1 --read-only --tmpfs /tmp:size=64m --user 65534:65534 python:3.12-slim`. Falls back to `ast` mode with warning if Docker unavailable.
- **Audit logging**: `log_sandbox_execution()` logs principal, mode, exit code, and truncated output for every sandbox invocation.
- **Production guard**: `PRODUCTION=true` refuses to start with `ast` mode on Windows or `container` without Docker.

### WP 2.7 — Secrets & Transport Docs
- **`deploy/Caddyfile.example`**: TLS termination config with reverse proxy for all 5 services.
- **`.env.example`**: `AUTH_JWT_SECRETS`, `SERVICE_AUTH_TOKEN`, `TRUSTED_PROXIES` documented.
- **`SECURITY.md` rewrite**: Full auth architecture, hardening history, known limitations table.
- **Agent cards updated**: All 4 agent cards include `securitySchemes` + `securityRequirements` for A2A SDK negotiation.

### Auth Contract Tests (042/042 passing)
- `test_auth_tokens.py` (132 lines) — JWT generation, validation, expiry, rotation, signature verification
- `test_auth_middleware.py` (189 lines) — middleware chain, path exemptions, principal-kind routing, auth-off mode
- `test_auth_routes.py` (145 lines) — login/refresh/logout cycle, rate-limited lockout, cookie signing
- `test_auth_audit.py` (41 lines) — structured auth.denied logging patterns
- `test_user_store.py` (146 lines) — Argon2 hashing, user CRUD, refresh token rotation

## v1.42 — Phase R: Table Classification, Bounded Bull/Bear Extraction, Corpus Harness

### Table Classification & Staged Extraction Pipeline

**`shared/reports/extraction.py`** (d071c70):
- **Table classification**: `_classify_table_types()` detects and classifies markdown tables into valuation, financial, scorecard, peer comparison, or general types — enables type-appropriate extraction.
- **Bounded bull/bear extraction**: `_extract_bull_bear_bounded()` uses section boundary markers to extract exactly one "Bull Case" and one "Bear Case" block — prevents the LLM's verbose output from flooding the extraction with duplicate narratives.
- **Staged pipeline**: Extraction proceeds in ordered stages: section parser → table classifier → bounded extraction → metric enrichment → fallback defaults. Each stage feeds the next; earlier stages don't block later ones.
- **Robust header detection**: `_parse_markdown_sections()` handles `##`, `###`, `####` and mixed heading styles without losing content to regex boundary mismatches.
- **Bare ticker peer extraction**: When no `CompanyName (TICKER)` format is found, a fallback scans for bare tickers in peer context ("peers like DLTR", "from COST") — populates `peer_names` from informal mentions.
- **Risk/opportunity from inline bold blocks**: Extracts risks from `**Bearish Signals:**` and `**Headwinds:**` blocks, opportunities from `**Bullish Signals:**` and `**Tailwinds:**` blocks — fallback when no dedicated section header matches.

### Corpus Regression Harness

**`tests/regression/test_corpus_invariants.py`** (126 lines, new):
- 7 corpus fixtures (`hostile.json`, `one_liner.json`, `quant_heavy.json`, `sentiment_heavy.json`, `table_free_prose.json`, `table_heavy.json`, `unicode_long.json`) covering real-world LLM output patterns.
- Invariant tests: no crashes, no empty sections, required fields populated for every corpus entry.
- Fixture-driven: adding a new corpus JSON file automatically creates test cases via parametrization.

**`scripts/export_brief_fixtures.py`** (new, 44 lines): Exports real `TickerMemory` briefs as corpus fixtures for regression testing.

## v1.41 — Phase 1: Centralized Settings, Module Splits, Guardrails, Docker Hardening

### WP 1.1 — MCP Server Module Split

**`mcp_servers/finsight_server.py`** (2095 lines) split into:
- `mcp_servers/tools/` (agent_registry, market_data, edgar, ticker, sentiment, sandbox) — per-tool modules
- `mcp_servers/infra/` (rate_limiters, embed, news_fetch) — shared infrastructure
- `mcp_servers/_app.py` (78-line composition root) — `get_app()` factory, wires everything

### WP 1.2 — Report Generator Module Split

**`shared/report_generator.py`** (1638 lines) split into `shared/reports/` package:
- `deck_model.py` — `DeckData` dataclass
- `extraction.py` — extraction pipeline (`_extract_deck_data`)
- `pptx_renderer.py` — `generate_pptx()`
- `docx_renderer.py` — `generate_docx()`
- `html_renderer.py` — `generate_html()`
- Back-compat shim preserved in `shared/report_generator.py`

### WP 1.3 — LangGraph Nodes Split

**`agent_3_langgraph/nodes.py`** (1286 lines) split into `agent_3_langgraph/nodes/` package:
- `calculations.py`, `data_fetch.py`, `technical.py`, `dcf.py`, `monte_carlo.py`, `portfolio.py`, `summary.py`

### WP 1.4 — Agent Server Factory

**`shared/agent_server.py`**: `build_agent_app()` factory for A2A sub-agents. All three sub-agent servers (`agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py`) converted to use the shared factory pattern — eliminates duplicate Starlette setup code.

### WP 1.5 — Centralized Pydantic Settings

**`shared/settings.py`** (new, 169 lines): pydantic-settings `BaseSettings` class with:
- Back-compat aliases (`LLM_BASE_URL` ↔ `OPENAI_BASE_URL` ↔ `LM_STUDIO_BASE_URL`)
- `validate_runtime()` — production-mode enforcement
- `get_settings()` singleton — lazy-loaded, cached after first call
- All env vars migrated from `shared/config.py` re-exports

**`shared/bootstrap.py`** (new, 62 lines): Centralises process-level side-effects — sets event loop policy, `HF_HUB_OFFLINE`, stdout encoding, `LANGFUSE_SECONDARY_KEY` patching. Called once per process.

### WP 1.6 — Performance Fixes

- **`shared/memory/session_repo.py`**: `JOIN` query replaces N+1 session list pattern — single SQL query instead of 1 + N round-trips for session event counts.
- **`agent_1_adk/api_routes.py`**: Input validation with error envelopes, filename safety in report downloads.
- **`shared/memory/ticker_memory.py`**: `has_changed()` old/new parameter swap fix — was comparing current against current, always returning `False`.

### WP 1.7 — Guardrails Unification

- **`shared/guardrails.py`**: `_NON_INVESTMENT_RE` regex unified from two separate copies.
- **Dead `_TODAY` constant removed** from `agent_1_adk/agent.py` (G6).
- **Silent except→logger.debug(exc_info=True)**: Executor, bridge, and agent error handlers now log with traceback instead of swallowing.

### WP 1.8 — Docker Hardening

- `.dockerignore` added
- `pyproject.toml`: `setuptools.packages.find` + per-service extras (`orchestrator`/`rag`/`quant`/`market`/`mcp_server`)
- All 5 Dockerfiles: `pip install ".[svc]"` + non-root `USER` + repo-root build context
- `docker-compose.yml`: Python urllib healthchecks (G19), `FINSIGHT_DB_PATH` volume (G3), `env_file`, `restart: unless-stopped`
- `shared/memory/store.py`: DB path from settings (G2) — no more hardcoded paths

## v1.40 — Report Extraction Hardening, Logging Overhaul, Agent Instruction Fixes

### Report Generator — Extraction Hardening (cf285b8)

- **Section parser handles `###`/`####` headers**: `_parse_markdown_sections()` regex updated from `\n##\s+` to `\n#{2,6}\s+`. LLM output frequently uses deeper heading levels; the old parser silently merged all sub-section content into a single block.
- **Executive summary augmented from Rationale**: If a "Rationale" section exists in the brief, its content is appended to the executive summary (up to 1200 chars, up from 800). Adds the analyst's stated reasoning alongside the metric-driven summary sentences.
- **Bear Case target extraction**: New pattern captures "bear case: $X" and appends it to the valuation table as "Bear Case Target".
- **Bare ticker peer extraction**: When no `CompanyName (TICKER)` format is found, a fallback scans for bare tickers in peer context ("peers like DLTR", "from COST", "vs. AAPL") to populate `peer_names`.
- **Risk/opportunity from inline bold blocks**: `_enrich_from_markdown()` now extracts risks from `**Bearish Signals:**` and `**Headwinds:**` blocks, and opportunities from `**Bullish Signals:**` and `**Tailwinds:**` blocks — as a fallback when no dedicated section header matches.
- **Upside pattern additions**: Patterns now match `expected return = X%` and `analyst upside: X%` in addition to existing colon-separated forms.
- **Structured peer comparison from agents**: Quant agent Monte Carlo output (p10/p50/p90) populates scenario cards when available. Structured peer rows built from both Quant agent `peer_comparison` dict and Sentiment agent `peer_comparison` list, using `col0`–`col2` column keys.

### Agent — `load_memory` Wrapper + Strengthened Instructions (cf285b8)

- **Custom `load_memory` wrapper** (`agent_1_adk/agent.py`): Replaces the ADK built-in `load_memory` tool with a custom async wrapper that calls `tool_context.search_memory()` and returns a plain `str`. The ADK tool previously returned a `LoadMemoryResponse` Pydantic model which the AG-UI bridge's `json.dumps()` could not serialize. The wrapper extracts text parts from memory events and joins them with `\n---\n`.
- **Strengthened system prompt**: `send_message` is now explicitly the *first* action for any stock analysis request — the LLM must emit ALL `send_message` calls in one turn before calling any other tool. `load_memory` is restricted to queries where the user explicitly asks about past recommendations.
- **Tools list**: `generate_report` removed from the LLM-visible tool list. Still callable via API; removed to prevent premature LLM invocation before analysis completes.

### Shared — Circular Import Fix + ui_sample Removal (cf285b8)

- **`shared/trace_context.py`**: Removed unused `from shared.logging_config import ...` that created a circular import chain `logging_config → trace_context → logging_config`. Any module importing `trace_context` at startup could deadlock the import system.
- **`ui_sample/` removed**: Deprecated static HTML prototypes replaced by the Next.js frontend (`web/nextjs-app/`).

### Logging Overhaul — Coverage, Operational Statements, Noise Suppression (8df085b)

- **11 silent `except` blocks fixed**: Bare `except: pass` and `except Exception: return None` across sandbox, report_generator, memory/store, ticker_memory, performance_tracker, api_routes, and trace_context replaced with `logger.warning(..., exc_info=True)`. Errors swallowed silently now appear in log files.
- **Logger boilerplate added to 7 files**: `shared/rate_limiter.py`, `shared/ttl_cache.py`, `shared/agui_sse.py`, `shared/base_agent.py`, `shared/memory/portfolio_store.py`, `agents/services.py`, and memory module files — all now use `logging.getLogger(__name__)`.
- **Operational log statements**: Cache hit/miss/eviction (`ttl_cache.py`); sandbox entry/exit (`code_sandbox.py`); DB open/close/migrate/prune (`memory/store.py`); brief store/update (`ticker_memory.py`); portfolio upsert (`portfolio_store.py`); recommendation record (`performance_tracker.py`); report generation with byte counts (`api_routes.py`).
- **Noisy third-party loggers suppressed**: `httpx`, `chromadb`, `langfuse`, `hpack`, `urllib3`, `asyncio` set to `WARNING` by default inside `setup_file_logging()`. Overridable via `LOG_LEVEL_<LIB>` env vars (e.g. `LOG_LEVEL_HTTPX=DEBUG`).
- **`@logged` on `GenericAgentExecutor.execute()`**: Entry point now emits structured `Enter`/`Exit`/`Fail` log lines with `latency_ms` for every sub-agent A2A request.
- **Hardcoded logger name fixed** (`tests/evaluation/run_offline_eval.py`): `logging.getLogger("finsight_eval")` → `logging.getLogger(__name__)`.
- **39 stale log files deleted** from `logs/`. Canonical names: `orchestrator.log`, `rag_agent.log`, `quant.log`, `market_context.log`, `mcp.log`.

### Diagram & Doc Link Fixes (ae5ca9c, a47c8c9, 345606d)

- **Mermaid syntax** (`docs/diagrams/component-orch.html`): Fixed invalid node label syntax that caused the Mermaid parser to fail silently and render a blank diagram.
- **Mermaid diagram sizing** (`docs/diagrams/shared.js`): Disabled `useMaxWidth` and switched to `getBBox()` for correct centering and sizing — prevents diagrams from being clipped or misaligned at different viewport sizes.
- **Zoom/drag** (`docs/diagrams/shared.js`): Fixed pointer event handling so pan and zoom interactions work correctly on all diagram pages.
- **15 broken internal links fixed** across `docs/` HTML files — corrected stale `#` anchor references, missing file paths, and relative path mismatches.
- **Diagrams link added to all nav bars**: Every documentation page's navigation bar now includes a link to the diagrams index (`docs/diagrams/`), making diagrams discoverable from any document.

## v1.39 — Report Generation: Data Layer, HTML Engine, Modular Slides, Regression Tests

### Phase 1 — Robust DeckData Extraction (`_extract_deck_data`)

**`shared/report_generator.py`** (72733d0):

- **`_extract_content()` → `_extract_deck_data()`**: Complete rewrite from fragile dict-based extraction to a `DeckData` dataclass with typed fields. New robust regex pipeline enumerates all extraction dimensions sequentially and fills every `DeckData` field with fallback defaults — no more silent empty slides.
- **`DeckData` dataclass**: 16+ typed fields (`company_name`, `ticker`, `recommendation`, `confidence`, `analysis_date`, `exchange`, `sector`, `executive_summary`, `kpi_chips`, `financials`, `valuation_table`, `scenarios`, `scorecard`, `peers`, `peer_names`, `risks`, `opportunities`, `sections`, `disclaimer`). All fields have safe defaults (empty lists/strings/dicts).
- **Metric extraction**: 12+ metric patterns with breadth-first matching — `Revenue Growth`, `ROE`, `Operating Margin`, `P/E Ratio`, `Beta`, `Sharpe Ratio`, `RSI`, `Volatility`, `Debt/Equity`, `Dividend Yield`, `EPS`, `Current Ratio`, `Net Margin`. Additional natural-language pattern variants ("X of Y", "X stands at Y") for `f90e40d`.
- **Scorecard extraction**: 7 dimensions (`Fundamentals`, `Technical Outlook`, `Valuation`, `Risk Profile`, `Profitability`, `Momentum`, `Analyst Sentiment`) with regex + mapping logic for badge assignment (`strong`/`bullish`/`expensive`/`moderate`).
- **Risk/opportunity extraction**: Multi-strategy pipeline — parsed sections → labelled blocks → inline comma-separated → Bull Case/Bear Case paragraphs → growth drivers → fallback defaults.
- **Peer name extraction**: Two formats: `CompanyName (TICKER)` and `TICKER (CompanyName)`. Financial abbreviation filter prevents DCF, MACD, RSI from being mis-identified as tickers.
- **Executive summary synthesis**: Graceful cascade — structured fundamental metrics → DCF vs price comparison → analyst target → technical outlook → top opportunity → key risk → fallback to raw text extraction.

**`shared/report_generator.py`** (f90e40d, 6f5cc13):

- **Additional regex patterns**: Added "X of Y" variants for all 13 metrics (e.g. `"operating margin of 4.2%"`), improving extraction from prose-format LLM output.
- **Current price extraction**: `"current price of $X"`, `"trading at $X"` patterns inserted at position 0 of valuation table.
- **Analyst Sentiment: consensus recommendation ordering**: `"consensus 'buy' recommend"` pattern must match before general `"recommends 'buy'"` to prevent false positives from sub-agent task text.
- **Technical Outlook lookbehind fix**: `(?<!lack of a)(?<!lack of )` prevents `"lack of uptrend"` from being identified as a bullish signal.
- **Valuation: overvalued/undervalued patterns**: `"may be overvalued"`, `"appears undervalued"`, `"may be overvalued relative"`.
- **Executive summary expansion**: P/E Ratio included in fundamental metrics (with "x" suffix). DCF vs current price comparison injected. Technical outlook from scorecard. Top opportunity and key risk inserted. Summary now reads as a cohesive narrative rather than a formulaic template.
- **Peer extraction: financial abbreviation filter**: `_FINANCIAL_ABBREVS` set blocks DCF, MACD, VIX, RSI, EPS, EBITDA, etc. from being extracted as peer tickers.

### Phase 2 — Momentum/RSI Scorecard Dimension

**`shared/report_generator.py`** (2b65ade):

- **"Momentum" dimension** added to scorecard: `RSI=85` → Overbought/expensive, `RSI=65` → Bullish/bullish, `RSI=40` → Neutral/moderate, `RSI=25` → Oversold/strong. Two regex patterns: `RSI(= X)` and `RSI of X`.

### Phase 3 — Jinja2 HTML Template Engine

**`shared/report_generator.py`** + **`shared/templates/`** (6e6dc9f):

- **`generate_html()`**: New public function returning a standalone HTML string. Uses `_extract_deck_data()` → `_deck_to_template_context()` → Jinja2 render. Signature matches `generate_pptx()`/`generate_docx()` for interchangeability.
- **`_deck_to_template_context()`**: Builds a context dict with `deck`, `rec_colors`, `confidence_pct`, `scenario_cards` for template rendering.
- **`_get_jinja_env()`**: Lazy Jinja2 `Environment` loader — first call loads once, subsequent calls return cached. Template directory is `shared/templates/`.
- **`shared/templates/investment_deck.html`** (~589 lines): `<deck-stage>` custom element wrapping 8 slide sections (title, key metrics, thesis, financials, valuation/scenarios, scorecard, peer comparison, risk-reward). Embedded `deck-stage.js` web component with keyboard navigation (arrow keys), slide indicator, expand/collapse sections.
- **CSS design system**: CSS custom properties for the clay/ivory/blue palette, responsive grid, rounded cards, Consolas for KPI values, serif headings.
- **`shared/templates/deck-stage.js`** (~1818 lines): `DeckStage` web component — slide management, keyboard nav, section visibility, standalone HTML. Inlined into the HTML output (no separate `src=` reference).

### Phase 4 — Modular Slide Generators

**`shared/report_generator.py`** (3be2e4d):

- **`generate_pptx()` refactored**: Monolithic slide generation (800+ lines of inline code) broken into 9 slide functions: `_pptx_slide_title`, `_pptx_slide_metrics`, `_pptx_slide_thesis` (executive summary), `_pptx_slide_financials`, `_pptx_slide_valuation`, `_pptx_slide_scorecard`, `_pptx_slide_peers`, `_pptx_slide_risk_reward`, `_pptx_slide_conclusion`.
- **`_SlidesHelper` namespace**: `SimpleNamespace` bundling shared slide helpers (`_text`, `_rounded_rect`, `_slide_label`, `_slide_title`, `_kpi_chip`, `_footer`) passed to each function. Eliminates repetitive parameter passing.
- **Constants extracted to module level**: Color constants (`_NAVY`, `_BLUE`, `_GREEN_DARK`, `_RED`, `_AMBER`, `_SURFACE`, etc.), font names (`_FONT`, `_MONO`), badge color maps. All previously inline in `generate_pptx()`.
- **DOCX generator updated**: Reuses `_extract_deck_data()` via `_extract_docx_content()` wrapper. DOCX-specific rendering preserves the existing Word document structure.

### Phase 5 — PPTX Visual Redesign

**`shared/report_generator.py`** (72733d0):

- **Dark title slide**: Navy background (`#1A1D23`), white text, exchange/sector subtitle, bottom KPI row (Recommendation, Confidence, Median Target). `_CLAY`/`_IVORY` color scheme replaced with professional dark theme.
- **Key Metrics slide**: Centered KPI chips (rounded rects) with label + value + context. Monospace values, green for positive, red for negative. Dynamic horizontal centering based on chip count.
- **Investment Thesis slide**: Blue left-border accent, surface-gray background card, 16pt serif text.
- **Financial Performance slide**: Clean table with `METRIC | CURRENT | CONTEXT` columns. Monospace for numbers, serif for labels. Alternating row highlighting.
- **Scenario Analysis slide**: Left-side valuation table + right-side scenario cards (Bull/Base/DCF). Color-coded by scenario (green/blue/amber).
- **Investment Scorecard slide**: 3×2 grid of dimension cards with colored badge pills. Badge type determines background + text color (strong=green, bullish=blue, expensive=red, moderate=gray).
- **Peer Comparison slide**: Table with `METRIC | TICKER | PEER1 | PEER2` columns. Centered numeric values in monospace. Highlighted rows for the primary ticker.
- **Risk-Reward slide**: Two-column layout — green-tinted Growth Opportunities column with bullet dots, red-tinted Key Risks column. Rounded border containers.
- **Conclusion slide**: Dark background, centered recommendation in large monospace text, confidence percentage, shortened executive summary excerpt.

### Phase 6 — DOCX & HTML Regression Tests

**`tests/regression/test_docx_regression.py`** (dbb0625, new, 145 lines):

- `test_docx_generates_valid_output` — Realistic WMT brief → DOCX non-empty (> 2000 bytes)
- `test_docx_with_empty_brief` — Empty dict → still generates minimal DOCX
- `test_docx_with_unknown_ticker` — PLTR with yfinance mock → valid output
- `test_docx_with_unicode` — São Paulo → no encoding errors
- `test_docx_with_markdown_tables` — Markdown tables parsed into structured data

**`tests/regression/test_html_regression.py`** (12c4670, new, 184 lines):

- `test_html_generates_valid_output` — Company name in title, all sections present
- `test_html_with_empty_brief` — Minimal HTML, no crash
- `test_html_with_unknown_ticker` — yfinance fallback, still generates
- `test_html_with_nonstandard_rec` — "STRONG BUY" → renders without crash
- `test_html_with_unicode` — Unicode characters properly encoded
- `test_html_with_markdown_tables` — Markdown tables → rendered sections
- `test_html_deck_stage_js_embedded` — deck-stage.js is inline, no external `src=`
- `test_html_autoescape_prevents_xss` — `<script>` tags escaped as `&lt;script&gt;`

**`tests/regression/test_pptx_regression.py`** (12c4670, new, 185 lines):

- `test_pptx_generates_valid_output` — ≥6 slides, non-empty
- `test_pptx_with_empty_brief` — ≥3 slides, no crash
- `test_pptx_with_unknown_ticker` — yfinance fallback
- `test_pptx_with_nonstandard_rec` — Default color mapping
- `test_pptx_with_unicode` — Unicode in company name → no encoding errors
- `test_pptx_with_markdown_tables` — Markdown tables → rendered without empty slides
- `test_pptx_very_long_summary` — Long text truncated, no overflow

### API Fix — Route Ordering

**`agent_1_adk/api_routes.py`** (6f5cc13): `/api/reports/ticker/{symbol}/latest/{format}` moved before `/api/reports/{brief_id}/{format}`. Starlette resolves routes top-to-bottom — without ordering, a request to `/api/reports/ticker/WMT/latest/pptx` matched `{brief_id}` as "ticker" and failed. The `/ticker/{symbol}/latest/{format}` route must appear first.

### AG-UI Bridge — Serialize ADK Responses

**`agent_1_adk/agui_bridge.py`** (f90e40d): Function response serialization now handles non-string types — `dict`, `list`, `int`, `float`, `bool`, `None` are JSON-serialized. Pydantic models with `model_dump()` are converted to dicts first. Previously only `str` responses were accepted; structured tool results caused `TypeError` in CopilotKit's event stream.

### Module Docstring Update

**`shared/report_generator.py`** (97a1edc): Docstring updated to include `generate_html` in the Public API section. Now lists all three output formats.

## v1.38 — Deferred Eval Gate + AG-UI Bridge Auto-Save + Confidence Extraction

### Infrastructure — Deferred Sub-Agent Eval Gate

**`shared/eval_gate.py` (new, 74 lines)**: Per-process deferred eval queue that holds sub-agent eval LLM calls until the orchestrator finishes its final answer synthesis. Sub-agent evals were firing immediately via `asyncio.create_task()`, hitting LM Studio from 3 separate processes right when the orchestrator needed it for synthesis. The process-local `LLMPriorityQueue` couldn't coordinate across processes.

- **`defer_eval(fn, *args, **kwargs)`**: Replaces `asyncio.create_task(eval_fn(...))` in all three sub-agent executors (RAG, Quant, Market Context). Queues the eval coroutine in a module-level list instead of executing immediately.
- **`release_evals()`**: Fires all deferred evals as `asyncio.create_task()` calls. Returns count of evals released. Cancels the auto-release safety-net when called explicitly.
- **`_auto_release()`**: Safety-net background task that fires after `EVAL_DEFER_TIMEOUT` (120s) if the orchestrator never calls `/release-evals`. Prevents evals from being silently dropped if the orchestrator crashes mid-synthesis.

**`POST /release-evals` endpoint** added to all three sub-agent servers (`agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py`): Calls `release_evals()` and returns `{"released": N}`.

**Orchestrator fires release after synthesis** — both `agents/finsight_agent/agent.py` (`after_agent_callback`) and `agent_1_adk/agent_executor.py` (`FinSightAgentExecutor`) call `_release_sub_agent_evals()` as a fire-and-forget `asyncio.create_task()` after scheduling the orchestrator's own eval. The `_release_sub_agent_evals()` helper POSTs to each sub-agent's `/release-evals` endpoint with a 5-second HTTP timeout.

### AG-UI Bridge — Null-Stripping + Auto-Save Briefs

**`agent_1_adk/agui_bridge.py`**:
- **RunStartedEvent no longer echoes input**: The `input=payload.model_dump(by_alias=True)` parameter was removed from `RunStartedEvent` construction. CopilotKit Cloud injected `encryptedValue: null` into the input payload, which caused Zod validation failures downstream because null is not accepted for optional fields in the AG-UI schema version CopilotKit targets.
- **Bridge auto-saves investment briefs**: After the orchestrator stream completes, `_auto_save_brief()` extracts ticker, recommendation, and confidence from the synthesis text using regex, then persists to `TickerMemory` via `store_minimal()` and `PerformanceTracker`. Checks for today's existing brief — if found and the existing response text is shorter, calls `update_response_text()` to overwrite with the longer synthesis. This ensures repeat queries via the bridge return cached results from memory.

**`shared/agui_sse.py`**:
- **Recursive `_clean()` null stripping**: Replaced the flat `_strip_message_nulls()` approach with a depth-aware recursive cleaner that strips null-valued optional fields at any nesting depth. `_STRIP_KEYS` expanded to include `"input"` — previously the list excluded `input` because it carried user data where null was semantically meaningful, but CopilotKit Cloud's `encryptedValue: null` is now injected at arbitrary nesting depths. The recursive approach handles all depths uniformly.
- **Removed `_strip_message_nulls()` helper**: The old function that specifically handled `input.messages[*].name/encryptedValue/role` nulls is gone — the recursive `_clean()` handles these paths naturally.

### Confidence Score Extraction — "Confidence Score: X" Format

**`agents/finsight_agent/agent.py`** (`_CONF_PATTERN`): Regex updated from `(?:confidence|conf)[:\s]*(\d+(?:\.\d+)?)...` to `(?:confidence|conf)(?:\s+score)?[:\s]*(\d+(?:\.\d+)?)...`. The LLM output uses "Confidence Score: 0.75" (with the word "Score") but the regex only matched "confidence: 0.75" and "75% confidence". The optional `(?:\s+score)?` group handles the new format without breaking existing matches.

**`agent_1_adk/agent_executor.py`** (`_store_memory` method): The A2A executor path now extracts confidence from the response text using the same regex pattern instead of hardcoding `confidence=0.5`. Also added `confidence=round(confidence, 2)` to both `store_minimal()` and `record_recommendation()` calls — previously the executor passed `confidence=0.5` to both, meaning briefs saved via the A2A path always recorded 50% confidence regardless of the actual response.

## v1.37 — LLM Priority Queue

- **`shared/llm_queue.py` (new, 105 lines)**: `LLMPriorityQueue` — process-local async priority semaphore using `heapq` + `(priority, seq, asyncio.Future)`. Three tiers: `Priority.CRITICAL` (0), `Priority.NORMAL` (1), `Priority.LOW` (2). Slot handoff on release preserves priority ordering. Default `LLM_MAX_CONCURRENT=2`. Configurable via env var.

- **`shared/config.py`**: `LLM_MAX_CONCURRENT` env var (default 2) controls queue size.

- **`agent_3_langgraph/nodes.py`**: `llm_summary_node` LLM call uses `Priority.CRITICAL`.

- **`agent_3_langgraph/server.py`**: Pre-warmup LLM ping uses `Priority.NORMAL`.

- **`agent_4_crewai/crew.py`**: `crew.kickoff()` LLM call uses `Priority.CRITICAL`.

- **`shared/runtime_eval.py`**: All RAGAS eval LLM calls (orchestrator, RAG, Quant, Market Context) use `Priority.LOW`.

## v1.36 — AG-UI Bridge + Next.js CopilotKit Frontend + Auto-Save Brief Fallback

### FEATURE — AG-UI Bridge & Next.js Frontend

**AG-UI bridge layer** (`agent_1_adk/agui_bridge.py`, `shared/agui_sse.py`, `agent_1_adk/api_routes.py`):
- **`POST /a2a-agui` endpoint** (`agui_bridge.py`): Streams AG-UI-compatible events from the ADK runner with off-topic guardrail, today's brief cache, memory context injection, and `active_agents` state tracking via `STATE_DELTA`/`STATE_SNAPSHOT`.
- **`shared/agui_sse.py`**: SSE framing utility with camelCase aliases, timestamp stamping, and null-stripping for CopilotKit Zod compatibility.
- **`agent_1_adk/api_routes.py`**: REST routes for `/api/memory/ticker`, `/api/sessions`, `/api/agents` with direct SQLite queries.
- **`agent_1_adk/agui_endpoint.py`**: Uses shared SSE helper, passes input to `RunStartedEvent`.
- **`agent_1_adk/main.py`**: Wires bridge, REST routes, CORS for `localhost:3000`.
- **`ui_sample/`** (6 new files): Sample HTML pages — `index.html`, `research.html`, `operator.html`, `trace.html`, `memory.html` plus `finsight.css` — matching the clay/ivory design system.

**Next.js frontend** (`web/nextjs-app/`, 25+ files):
- **Next.js 16 + CopilotKit 1.59 + @ag-ui/client**: 5-page app — Overview, Research (CopilotKit chat + agent tiles + trace strip), Trace (Langfuse span visualization), Memory (expandable ticker briefs), Operator (service health dashboard).
- **Design system**: Clay/ivory palette matching `ui_sample/`, serif headings, JetBrains Mono, agent color-coding, signal badges (BUY/HOLD/SELL).
- **Server-side proxies**: `/api/copilotkit` — CopilotRuntime with `HttpAgent` → `/a2a-agui`; `/api/traces` — Langfuse proxy (keys from env, never exposed to browser); `/api/health` — CORS-safe health check proxy.
- **Scripts**: `run_ui.bat` / `stop_ui.bat` — start/stop all services including Next.js on `:3000`.

### Agent — Auto-Save Brief Fallback

**`agents/finsight_agent/agent.py`**:
- **`_auto_save_brief()`**: When the LLM fails to call `save_brief` after a `send_message` turn (common with smaller local models like ministral-3b), the `after_agent_callback` detects `send_message` usage as the analysis signal and programmatically persists the brief via `TickerMemory` + `PerformanceTracker`. The gate logic: `_has_send_message_call()` checks for `send_message` invocations in the turn. When `send_message` was called but `save_brief` wasn't, `_auto_save_brief` extracts recommendation/confidence from the response text and saves it.
- **`_extract_recommendation_from_text()`**: Best-effort regex extraction of `BUY|HOLD|SELL` + confidence percentage from freeform LLM text. Handles `"confidence: 85%"`, `"conf: 0.85"`, `"75% confidence"` formats.
- **Callback updated**: `_is_analysis_turn()` → `_is_analysis_turn() or _has_send_message_call()` — non-analysis turns (e.g. `load_memory`-only queries) are still skipped.

### After-Turn Callback — Full Synthesis Persistence + Case-Insensitive Ticker Lookup

**`agents/finsight_agent/agent.py`** (`_persist_memory_callback`):
- **Full analysis text overwrite**: After the callback persists events to memory, it extracts the longest LLM-generated text from the current turn and calls `TickerMemory.update_response_text()` when it's longer than what `save_brief` stored. This ensures the saved brief contains the complete analysis even when the LLM generates it after calling `save_brief` (ADK often splits the analysis and function call into separate events). Fixes `save_brief` returning only "Brief saved for NVDA: BUY" on same-day cache hits.

**`shared/ticker_utils.py`**:
- **Case-insensitive ticker extraction**: `extract_ticker()` now uppercases its output, so `wmt` matches the cached `WMT` brief in the memory cache callback.

## v1.35 — MCP Performance, RAGAS Eval Tuning, Infrastructure Hardening

### RAG Synthesis Fix (09e0424)

- **RAG agent no longer mentions missing historical data** (`agent_2_llamaindex/executor.py`): When ChromaDB returns zero results for a ticker (no filings ingested yet), the query response previously included phrases like "I don't have any historical financial data" — which confused the orchestrator into thinking the ticker had no fundamentals. Now returns a concise `"Index is warming for {ticker}..."` status message instead, matching the A2A WORKING event pattern.

### Quant — Weighted Vote Normalization & Misc Fixes

**`agent_3_langgraph/nodes.py`**:
- **FCF period sorting**: `_get_financials` now sorts free cash flow data descending by period so the most recent year is used first in DCF valuation.
- **Volatility scoring fix**: Signal scoring logic corrected for the volatility gate — ensures `risk_quality` signal accurately reflects computed volatility tier.
- **Weight redistribution**: When a signal group is absent (no data), its weight is redistributed proportionally across remaining present groups instead of being silently dropped — prevents confidence inflation on sparse data.
- **`golden_cross` defaults to `False`**: The `technicals` dict now explicitly sets `golden_cross=False` when the indicator is absent or null, preventing downstream `None` comparison errors.

### Infrastructure — DB Paths, Connection Cleanup, Misc Fixes

- **`agent_1_adk/api_routes.py`**: Memory API routes now use `_ADK_SESSION_DB` constant instead of hardcoded path. Proper `try/finally` blocks ensure connections are always cleaned up. Dead code removed.
- **`shared/a2a_store.py`**: Task preload from SQLite fixed to avoid touching the private `InMemoryTaskStore._impl` attribute — uses the public `upsert()` API instead.
- **`shared/generic_executor.py`**: Corrected `TaskState` enum value reference for cancellation handling.
- **`shared/mcp_client.py`**: Transient error tuple `_TRANSIENT_EXC` extracted to module level (was inline in function scope) — enables reuse by other modules.
- **`shared/memory/store.py`**: `prune_old_records()` now uses an `_ALLOWED_TABLES` whitelist (`frozenset`) to prevent SQL injection via table name — defensive hardening.
- **`shared/memory/ticker_memory.py`**: Change detection (`has_changed`) now correctly identifies both upgrades (SELL→BUY) and downgrades (BUY→SELL) instead of treating all changes uniformly.
- **`shared/rate_limiter.py`**: Added type annotations to all public methods.
- **`shared/semantic_cache.py`**: Added `_init_started` guard to prevent re-entrancy during `SemanticCache.__init__` when the ChromaDB client creation triggers import side-effects.

### MCP Server — yfinance Blocking Calls Moved to Thread Executor

- **`get_insider_transactions` converted to async** (`mcp_servers/finsight_server.py`): `yf.Ticker(ticker).insider_transactions` (synchronous DataFrame fetch) now runs via `loop.run_in_executor()` instead of blocking the asyncio event loop. Prevents concurrent tool calls from stalling while insider data is fetched.
- **`_get_scenario_shocks_uncached` converted to async** (`mcp_servers/finsight_server.py`): `yf.Ticker(sym).history(period="max")` (25+ years of price data) now runs via `loop.run_in_executor()`. Prevents long-running history fetches from starving concurrent MCP requests.
- **7 additional yfinance tools wrapped in `run_in_executor`** (`mcp_servers/finsight_server.py`): Followup to the two above — `_get_prices_uncached` (`stock.history()`), `_get_financials_uncached` (`.financials`/`.balance_sheet`/`.cashflow`/`.info`), `_get_macro_impl` (both macro/sector `history()` loops), `get_options_chain` (`option_chain()` and `.options`), `get_earnings_calendar` (`stock.calendar`), `get_sentiment_indicators` (`stock.info`), and `get_earnings_history` (`stock.earnings_dates`) all moved off the event loop. Together with the first two, every synchronous yfinance call in the server now runs via thread executor. Fixes `httpx.ReadError` SSE keepalive failures mid-call.

### Quant & Market Context — Peer Concurrency Cap

- **`asyncio.Semaphore(3)` in peer financials fetch** (`agent_3_langgraph/nodes.py` and `agent_4_crewai/executor.py`): Both agents now limit concurrent `get_financials` MCP calls for peer tickers to 3 at a time. Previously all peer requests fired simultaneously — the MCP server's yfinance rate limiter queued them internally, but the per-attempt timeout could expire before the queue cleared. Semaphore ensures no more than 3 in-flight requests, keeping response time within the timeout window.

### MCP Client — Timeout Simplification

- **Removed fail-fast first-attempt timeout** (`shared/mcp_client.py`): The previous logic used `self.timeout / 3` on the first retry attempt (5s for a 15s timeout) to fail fast, then fell back to full timeout on subsequent attempts. This was unnecessary for yfinance calls where latency varies naturally — the reduced timeout caused false-positive timeouts during normal yfinance slowness. Now all attempts use the full configured timeout.

### Infrastructure — Redis Auto-Start in run_adk_web.bat

- **Redis auto-detection and startup** (`run_adk_web.bat`): Reads `REDIS_URL` from `.env` — when set and `redis-server` is in PATH, the batch file auto-starts Redis before launching agent services. Logs a clear message when `REDIS_URL` is set but `redis-server` is not found (suggests `winget install Redis.Redis`). Falls back gracefully to in-process TTLCache when Redis is unavailable.
- **Redis cleanup in stop_servers.bat** (`stop_servers.bat`): `stop_servers.bat` now kills `redis-server.exe` when `REDIS_URL` is configured. Uses both `taskkill` and WMI-based process termination for robustness.
- **Terminal cleanup updated**: The WMI process filter in `stop_servers.bat` now also matches `redis-server` command lines, ensuring all terminal windows are properly cleaned up.

### MCP Client — Transient Error Expansion

- **httpx network errors added to retryable set** (`shared/mcp_client.py`): `httpx.ReadError`, `httpx.ConnectError`, and `httpx.NetworkError` added to `_TRANSIENT_EXC` tuple. These errors surface when the MCP server's SSE connection resets due to event-loop blocking (see yfinance executor fixes above). Previously, a brief SSE blip during a yfinance call would fail the entire MCP request immediately. Now they retry like any other transient network error. The import is guarded with `try/except ImportError` so `httpx` is not a hard dependency of the module.

### RAGAS Eval — Retry Tuning & Timeout Logging

- **AsyncOpenAI `max_retries=5`** (`shared/runtime_eval.py`): Increased from 1 to 5 so LM Studio idle-unload retries are absorbed at the httpx/SDK layer rather than failing instructor structured-output calls outright. The previous `max_retries=1` was meant to suppress retry storms but also caused spurious failures when LM Studio briefly unloaded the model between requests.
- **Removed instructor `max_retries=1` override**: `instructor.from_openai()` now falls back to the client's retry count (5), so instructor calls get the same retry budget as raw client calls.
- **`asyncio.TimeoutError` caught separately** (`_run_metrics`): Now logged with a dedicated warning message including the `EVAL_METRIC_TIMEOUT` value instead of appearing as a bare colon in the generic `BaseException` handler.
- **Empty exception messages logged as type name**: When `str(exc)` is empty (common for `TimeoutError` and `CancelledError`), logs `type(exc).__name__` instead of a bare colon in the warning line.

### Model — Local Switch to Ministral-3-14b

- **`.env` model changed to `mistralai/ministral-3-14b-reasoning`**: The active development model switched from `qwen/qwen3-30b-a3b-2507` to `ministral-3-14b-reasoning` for faster inference (~3-5s vs ~8-12s per call) and lower RAM consumption (~3-4GB vs ~6-8GB). The `config.py` default remains `qwen3-30b-a3b-2507` as the reference model — developers override via `.env`.
- **Impact**: Ministral's inconsistent `save_brief` calling directly motivated the auto-save brief fallback and full-synthesis after-turn update features above. The trade-off (speed/resource efficiency vs tool-calling reliability) is acceptable for local development.

## v1.34 — Phase 5: Dynamic Peers, Sector-Relative Scoring, Live Scenario Shocks, Insider MCP Tool

### Quant Agent — Sector-Relative Fundamental Scoring + Live Scenario Shocks

- **Sector-relative fundamental scoring** (`agent_3_langgraph/nodes.py`): `_score_fundamental_value()` and `_score_fundamental_quality()` accept optional `medians` dict from `peer_comparison_node`. When available, scores are computed relative to sector median instead of absolute universal thresholds. New `_relative_score(value, median, higher_is_better)` returns [-1, 1] based on ratio to median. PE/EV/EBITDA use lower-is-better logic; ROE/margin/D/E use higher-is-better logic.
- **Sector medians computed in `peer_comparison_node`** (`nodes.py:919-935`): After ranking peers, computes median per metric (PE, EV/EBITDA, RevGrowth, OpMargin, ROE, D/E) from peer values. Passed to `format_output_node` via `peer_comparison.medians`.
- **Live sector-aware scenario shocks** (`mcp_servers/finsight_server.py`): New `get_scenario_shocks` MCP tool computes historical crash returns from live price data using sector-specific ETFs (QQQ for Tech, XLP for Consumer Defensive, XLK, XLF, etc.) via `_SECTOR_ETFs` mapping. Falls back to S&P 500 (^GSPC) when ETF lacks history for a given window. 4 scenarios: market_crash_2008, covid_crash_2020, dot_com_bubble, mild_recession (2022). Cached 7 days (`_cache_shocks`).
- **Stress test uses live shocks** (`nodes.py:677-720`): `stress_test_node` fetches `get_scenario_shocks(sector)` via MCP before applying beta adjustment. When MCP returns live data, overrides the hardcoded S&P fallback values with sector-specific crash returns. Logs `index_used` for traceability.
- **Monte Carlo runs on both paths** (`nodes.py:832-850`, `state.py:88`): `dcf_valuation_node` now also runs `_run_monte_carlo()` for low-volatility tickers (routed to DCF). `monte_carlo` state field uses `_last_nonnull` reducer so format_output always gets whichever node produced the MC.
- **`debt_to_equity` added to peer comparison** (`nodes.py:898`): Fundamental comparison now includes D/E ratio alongside PE, EV/EBITDA, revenue growth, op margin, ROE.

### Quant Agent — Fan-In Reducer Fixes + Graph Topology

- **Annotated reducers for multi-writer keys** (`agent_3_langgraph/state.py`): Added `_merge_dict`, `_last_str`, `_last_nonnull` reducers to `metrics`, `reasoning`, `recommendation`, `stress_test_result`, `dcf_error` fields. LangGraph requires explicit `Annotated[type, reducer]` when multiple nodes write to the same state key in the same checkpoint step.
- **`_merge_stress_test` reducer** (`state.py:20-42`): Custom reducer that prefers the stress_test_result with real scenario data over the "skipped" placeholder. When `format_output_node` writes a placeholder before the real stress data arrives, this ensures the real data wins.
- **Removed diamond dependency edge** (`agent_3_langgraph/graph.py`): Removed the direct `fetch_fundamentals → format_output` edge. `fetch_fundamentals` already fans into `peer_comparison_node` which fans into `format_output` — the direct edge created a diamond pattern where `format_output` received two distinct inputs in the same step, violating LangGraph's fan-in constraint.
- **Removed passthrough keys from `format_output_node`** (`agent_3_langgraph/nodes.py`): Was returning passthrough copies of `positioning`, `dcf_valuation`, `correlation_matrix`, `fundamentals` that other nodes already wrote. Now only emits `recommendation`, `reasoning`, `metrics` (with signals/confidence), and `stress_test_result`. Fixes `INVALID_CONCURRENT_GRAPH_UPDATE` at `positioning` key.

### Dynamic Peer Discovery — yfinance Industry/Sector Classes

- **`get_peers` MCP tool rewritten** (`mcp_servers/finsight_server.py`): Now uses yfinance `Industry(slug).top_companies` and `Sector(slug).top_companies` instead of the Yahoo Finance HTTP `recommendationsBySymbol` API. No scraping, no cookies, no rate limits. Falls through to Sector if Industry returns nothing. `_industry_to_slug()` converts yfinance strings to URL slugs. Cached 24h.
- **Both Quant and Market Context use `get_peers`**: `peer_comparison_node` and `MarketContextAgent._collect_data_parallel()` call `mcp.call_tool_by_name("get_peers", {"ticker": ...})` for dynamic peer discovery.

### Peer Sets — Expanded + Normalised Key Matching

- **`shared/peer_sets.py` — 80+ entries** (up from 33): Added comprehensive sector coverage: Banks, Asset Management, Insurance (Life and P&C), Capital Markets, Financial Services, Healthcare (Drug Manufacturers, Medical Devices, Biotech), Consumer Defensive (Discount Stores, Grocery, Household, Packaged Foods, Beverages, Tobacco), Consumer Cyclical (Retail, Auto, Restaurants, Apparel, Leisure), Energy (Integrated, E&P, Refining, Midstream), Industrials (Aerospace, Rail, Trucking, Construction, Distribution, Electrical Equipment), Communication Services, Basic Materials (Specialty Chemicals, Gold, Copper), Real Estate (Industrial, Office, Residential, Retail REITs), Utilities (Regulated Electric, Regulated Gas), Food Distribution.
- **Normalised key matching** (`shared/peer_sets.py`): New `_norm()` function collapses em-dashes/en-dashes/hyphens to single hyphen, lowercases, strips. `_NORM_MAP` pre-built for O(1) fuzzy lookup. Lookup order: exact industry → normalised industry → exact sector → normalised sector. Handles yfinance's inconsistent punctuation (e.g. "Banks—Regional" vs "Banks - Regional").

### Insider Signals — Structured MCP Tool

- **`get_insider_transactions` MCP tool** (`mcp_servers/finsight_server.py`): Uses yfinance `Ticker.insider_transactions` DataFrame — structured buy/sell data with share counts, values, and transaction types. No Form 4 keyword parsing. Returns `transactions` list and `summary` dict with total/buys/sells/direction/net_shares/net_value. 90-day lookback default.
- **`insider_signals_node` rewritten** (`agent_3_langgraph/nodes.py`): Now calls `get_insider_transactions` instead of `get_company_filings` + keyword matching on Form 4 text. Uses structured `summary.buys/sells/direction` fields. Returns `net_shares` and `net_value` from the summary.
- **Agent card updated**: Insider skill description updated to reflect structured data source.

### Options Flow — Zero-Volume Edge Case

- **Zero-volume guard** (`agent_3_langgraph/nodes.py:979-993`): When `call_vol + put_vol == 0`, returns `flow_signal: "no_data"` with `put_call_volume_ratio: None` instead of a misleading 1.0. Uses `None` for ratios when denominator is zero (no calls at all).

### Schema Validator — Null-Safe Signal Key Matching

- **`score_quant_deterministic()` updated** (`shared/runtime_eval.py`): Signal group keys changed to match `_SIGNAL_WEIGHTS` in `nodes.py` (e.g. `"dcf"` → `"dcf_value"`, `"fund_value"` → `"fundamental_value"`). Now reads `signal_scores` from `metrics.signal_scores` nested path instead of top-level `signal_scores`. Reads `quant_confidence` from `metrics.quant_confidence` instead of top-level `confidence_score`.

### RAG Agent — A2A WORKING Event Warm-Up

- **A2A WORKING events for index warm-up** (`agent_2_llamaindex/executor.py`): When the RAG agent receives a query for an un-ingested ticker, it returns a `status_update` with `state: WORKING` and message `"Index is warming for {ticker}..."`. The orchestrator's streaming event handler skips WORKING events and waits for a COMPLETED/WORKING-termination signal. This replaces the previous polling pattern where the orchestrator re-queried RAG until the index was ready.

### Market Context Agent — Sector/Industry Fix

- **Restored sector/industry extraction** (`agent_4_crewai/executor.py`): `_collect_data_parallel()` now extracts `sector` and `industry` from `get_financials` response and passes them to the crew context. These keys were previously undefined (referenced but never populated) after the peer discovery refactor in Phase 3 — the crew context had empty strings for both fields. Fix closes the v1.31 known issue.

## v1.33 — Quant Graph Fixes: Concurrent Update Resolution & Fan-In Reducers

### Quant Graph — Fan-In Passthrough Key Removal

- **Fixed `INVALID_CONCURRENT_GRAPH_UPDATE` at `positioning` key** (`agent_3_langgraph/nodes.py`): `format_output_node` was returning passthrough copies of state keys (`positioning`, `dcf_valuation`, `correlation_matrix`, `fundamentals`) that other nodes already wrote in the same checkpoint step. LangGraph's fan-in saw conflicting writes, triggering the error.
- **Fix**: Removed all passthrough keys from `format_output_node`'s return dict. It now only emits what it actually computes: `recommendation`, `reasoning`, `metrics` (with signals/confidence), and `stress_test_result`. Full state from `ainvoke()` still carries every key via the owning nodes, so `graph.run()` reads are unaffected.

## v1.32 — Phase 4 Final Items: Date-Aware Semantic Cache + RAG Startup Warm-Up

### Semantic Cache — Date-Scoped Keys

- **`SemanticCache.set()` tags entries with today's `YYYY-MM-DD`** (`shared/semantic_cache.py`): On every cache write, the current date is embedded in the stored metadata.
- **`SemanticCache.get()` filters by date** (`shared/semantic_cache.py`): Queries use a ChromaDB `where` filter that only matches entries tagged with today's date. Same query on different days → cache miss → fresh analysis.
- **Why**: Without date scoping, a user asking "Analyze NVDA" on Tuesday would see Monday's cached response — missing overnight news, price moves, or macro shifts.

### RAG Agent — Startup Warm-Up

- **`_do_prewarm()` at `agent_2_llamaindex/server.py`**: Runs once on Starlette startup in a thread executor via `asyncio.to_thread`. Pre-loads:
  - HuggingFace `all-MiniLM-L6-v2` embedder + dummy encode call
  - Three ChromaDB collections (`sec_filings`, `news`, `earnings`)
  - CrossEncoder reranker from `hybrid_search.py`
- **Per-stage timing logged**: Each warm-up phase logs elapsed seconds so the cold-start budget is visible in server logs.
- **Effect**: First RAG query no longer pays ~3-5s model-load tax. Cold-start drops from ~12s to ~9s (ChromDB query still happens on first hit).

## v1.31 — Phase 3/4: Market Context Rebrand, Quant Behavioral Signals, Redis Cache, Eval Hardening

### Market Context Agent — Sentiment Rebrand + Macro/Peer Inputs

- **Agent renamed** (`agent_4_crewai/executor.py:13-21`): `SentimentAgent` → `MarketContextAgent`, `SentimentIntelligenceCrew` → `MarketContextCrew` (`crew.py:16`). Langfuse trace name: `market-context-agent-stream`. Agent card path: `agent_cards/market_context_agent.json`.
- **New MCP tool** `get_macro_indicators()` (`mcp_servers/finsight_server.py:305`): Fetches 10Y/2Y Treasury yields, VIX, DXY, yield-curve regime, sector ETF (XLK/XLE/XLF/…) 1mo performance. Cached 15 min via `_cache_macro`.
- **New `shared/peer_sets.py`**: Hand-curated peer map for 10+ sectors (NVDA→AMD/INTC/AVGO/TSM, JPM→BAC/WFC/C/GS, etc.). 33 entries. Shared between Market Context and Quant.
- **`_collect_data_parallel` rewritten** (`executor.py:39-86`): 3-step pipeline: macro + primary financials → resolve peers → parallel peer financials + prices. No longer fetches `get_news_sentiment` or `get_company_filings` (those routes are RAG's domain post-Phase 1).
- **MarketContextCrew rewritten** (`crew.py`): Single-agent crew with role "Market Context Analyst". Task expected_output: JSON with `narrative`, `macro_regime`, `relative_peer_positioning`, `overall_signal`, `confidence_score`, `key_tailwinds`, `key_headwinds`.
- **`_extract_context_contexts`** (`executor.py:196`): Replaces `_extract_sentiment_contexts` — builds RAGAS context strings from macro indicators + peer financial summaries.
- **Agent card** (`agent_cards/market_context_agent.json`): Two skills: `macro_regime_analysis`, `peer_landscape_analysis`. Version 2.0.0.

### Quant Agent — Behavioral Signals + Monte Carlo + Peer Comparison

- **`options_flow_node`** (`agent_3_langgraph/nodes.py:900`): Put/call volume ratio, OI ratio, flow signal (bullish/bearish/neutral/unusual).
- **`insider_signals_node`** (`nodes.py:921`): Form 4 filing count (90-day window), net direction (buy/sell/neutral), CEO/CFO weighting.
- **`analyst_positioning_node`** (`nodes.py:979`): Consensus score, analyst upside %, short interest, squeeze risk.
- **Monte Carlo simulation** (`nodes.py:42`): GBM-based, 10,000 paths, 60-day horizon. Returns p10/p25/p50/p75/p90, prob of profit, MC VaR(95).
- **`peer_comparison_node`** (`nodes.py:801`): Ranks primary ticker vs peers on PE, EV/EBITDA, RevGrowth, OpMargin, ROE.
- **Stress test beta-adjusted** (`nodes.py:169`): `beta_adj_decline = mkt_decline × beta`, floored at −95%.
- **8-group weighted voting** (`nodes.py:107-117`, `_weighted_vote` at `nodes.py:228`): `_SIGNAL_WEIGHTS` sums to 1.0 across 8 groups (technical, fundamental, narrative, options, insider, positioning, macro, risk). Confidence = `|composite| × (1 − std(present_signals))`. Fix: raw weighted sum (not normalized) — normalized weights distorted confidence when few signals were present.
- **`QuantAnalysisState` expanded** (`state.py:27-31`): `monte_carlo`, `peer_comparison`, `options_signals`, `insider_signals`, `positioning` fields.
- **Agent card** (`agent_cards/quant_agent.json`): Three new skills: `options_flow_analysis`, `insider_transaction_analysis`, `positioning_signals`.

### MCP Server — DuckDuckGo Fallback, Sentiment Indicators, Earnings History

- **DuckDuckGo 4th-tier news fallback** (`mcp_servers/finsight_server.py:1248`): `_fetch_ddg_news()` called when Yahoo Finance + RSS feeds return zero articles. Uses `duckduckgo_search` library with 5s timeout.
- **New MCP tool `get_sentiment_indicators()`** (`finsight_server.py:1628`): Short interest %, analyst consensus breakdown (buy/hold/sell), institutional ownership %.
- **New MCP tool `get_earnings_history()`** (`finsight_server.py:1684`): Last N quarters EPS estimates vs actuals, beat rate, average surprise %.
- **Empty news result caching** (`finsight_server.py:1397`): Normalized cache key on ticker only (not ticker+limit). Empty results cached 5 min to avoid repeated fallback fetches.

### Infrastructure — Redis Two-Level Cache + MCP Client Improvements

- **`shared/redis_cache.py`** (new): `RedisCache` class with L1 (in-process TTLCache) + L2 (Redis write-through). `make_cache()` factory returns `RedisCache` when `REDIS_URL` is set, else bare `TTLCache`. Write-through: every L1 set propagates to Redis; L1 miss reads from Redis; Redis sets populate L1.
- **`shared/mcp_client.py`** (`mcp_client.py:51-56`): Fail-fast retry classification — `_is_retryable_error()` skips exponential backoff for 404s, "not found", "invalid". Default timeout 30s → 15s.
- **`shared/config.py`**: `LLM_SUMMARY_MODEL` env var for optional smaller summary model. `REDIS_URL`, `A2A_TIMEOUT_MARKET_CONTEXT`, `EVAL_RUNTIME_DISABLED`, `EVAL_BURST_LIMIT`, `EVAL_METRIC_TIMEOUT`, `RAGAS_LLM_MODEL`, `RAGAS_LLM_BASE_URL`.

### Runtime Evaluation — Circuit Breaker, SHA-256 Dedup, Burst Limiter

- **Circuit breaker** (`shared/runtime_eval.py`): `_CIRCUIT_MAX_FAILURES=5` — after 5 consecutive metric failures, all eval is skipped for 5 min. `_last_failure` tracks per-metric for granular reset.
- **SHA-256 dedup**: `_dedup_seen` TTL dict (1h) — identical (input, response) pairs skip eval to avoid redundant LLM scoring.
- **Burst limiter**: `_burst_ok()` — enforces `EVAL_BURST_LIMIT` evaluations per minute per process. Uses a deque of timestamps, oldest evicted on overflow.
- **Per-metric timeout**: `_score_metric_with_timeout(metric, timeout=EVAL_METRIC_TIMEOUT)` — wraps each RAGAS metric in `asyncio.wait_for` (default 90s). Cancels sibling metrics on timeout via `asyncio.CancelledError`.
- **`_gate_ok()`**: Unified pre-eval gate: `EVAL_RUNTIME_DISABLED → False` | circuit tripped → `False` | burst exceeded → `False` | dedup hit → `False` | else `True`.
- **`score_quant_deterministic()`**: Zero-LLM schema validator — checks 8 signal groups present, weight sum = 1.0, MC consistency, peer field presence, recommendation + confidence invariants.
- **Metric catalog cleanup**: `AnswerRelevancy` removed from RAG/Quant/Sentiment (kept on Orchestrator only). `score_sentiment_response`: removed `catalyst_identification` + `insider_signal_discussion`, added `macro_regime_quality` + `peer_positioning_quality` rubrics.
- **`cross_collection_synthesis` rubric** on RAG score: checks if response cites sources from ≥2 collections (sec_filings/news/earnings).
- **Offline evaluation** (`tests/evaluation/`): `golden_set.jsonl` (5 golden examples), `run_offline_eval.py` — loads golden set, runs RAGAS ContextRecall/ContextEntityRecall/AgentGoalAccuracy.
- **`no_forward_guarantees` AspectCritic** added to orchestrator: flags any language suggesting guaranteed future performance.

### Test Suite — Behavioral E2E + Eval Gates + Parallel Dispatch

- **`tests/integration/test_behavioral_signals_e2e.py`** (5 E2E tests): Covers options flow, insider signals, positioning, peer comparison, Monte Carlo existence.
- **`tests/unit/test_runtime_eval_gates.py`**: Tests circuit breaker, SHA-256 dedup, burst limiter, kill switch, `_gate_ok`, `score_quant_deterministic`.
- **`tests/unit/test_parallel_dispatch.py`**: Tests concurrent gather, timeout map matching, key isolation (no "sentiment" in "Market Context Agent").
- **`tests/unit/test_quant_graph_nodes.py`**: Fixed stale field name `decline_pct` → `beta_adj_decline_pct`.

### Docs Consolidation

- **Removed stale possible_improvements/ files**: `IMPLEMENTATION_PLAN.md/.html`, `LOGGING.md/.html`, `TEST_PLAN.md/.html`, `AGUI_FRONTEND_PLAN.html`, `ARCHITECTURE.md/.html`, `improvements.html`. All consolidated into `UNIFIED_IMPLEMENTATION_PLAN.html`.
- **Sentiment→Market Context rename propagated**: `AGENTS.md/.html`, `ARCHITECTURE.md/.html`, `DEMO.md/.html`.

## v1.30 — Phase 2: Parallel Agent Dispatch, Parallel Filing Downloads, Single-Flight Cache

### Orchestrator — Parallel Dispatch via System Prompt

- **`_STATIC_PREAMBLE` step 2 updated** (`agent_1_adk/agent.py:184-189`): Now explicitly instructs the LLM to emit ALL `send_message` calls in a SINGLE assistant response for parallel execution: "you MUST emit ALL `send_message` calls in a SINGLE assistant response so they execute in PARALLEL. Do NOT wait for one agent's result before issuing the next call."
- **Step 5 updated**: "After ALL agents have responded (you will receive their results together in the next turn)" — makes the parallel contract explicit.
- **Agent responsibility boundaries** (`agent.py:253-260`): `_build_instruction()` appends a responsibility block clarifying: RAG Agent owns ALL document and news retrieval; Market Context Agent provides macro regime and peer narrative; Quant Agent owns numeric risk, fundamentals, technicals, DCF, and behavioral signals. Prevents the LLM from routing news queries to the Sentiment agent.

### RAG Agent — Parallel Filing Downloads

- **`_ensure_ingested()` filing fetch converted to `asyncio.gather`** (`agent_2_llamaindex/executor.py:74-107`): The per-filing sequential `get_filing_content` loop replaced with a two-phase pattern: filter candidates → `asyncio.gather(*[_fetch_one(f) for f in candidates])`. Filing downloads for a 5-filing ingest now complete in ~max(per-filing) latency (~3-5s) instead of sum (~15-25s).
- **`get_filing_content` server-side truncation** (`mcp_servers/finsight_server.py:842`): Content truncated at 25,000 chars on the MCP server instead of 20,000 on the client — reduces bandwidth on large 10-K fetches.
- **`query()` fire-and-forget ingestion** (`executor.py:162-166`): `_ensure_ingested` and `_ensure_news_ingested` converted from `await asyncio.gather(...)` to `asyncio.create_task()` (fire-and-forget). Query returns immediately from whatever ChromaDB content is already indexed. First-query partial-data warning via `_warming` flag.
- **`index_manager.py` warming signal**: When `index.query()` finds zero matching documents, returns `{"_warming": True, "summary": "Index is warming for {ticker}..."}`.

### MCP Server — Single-Flight News Cache

- **`get_news_sentiment` refactored** (`mcp_servers/finsight_server.py:1406-1551`): Extracted core fetch logic into `_get_news_sentiment_impl()`. The outer tool wrapper delegates to `_cache_news.get_or_fetch()` — two concurrent callers for the same uncached ticker share one RSS round-trip instead of both fetching independently.

## v1.29 — Phase 1: RAG News Ingestion, Quant Fundamentals/Technicals, Data-Driven DCF

### RAG Agent — News & Earnings Ingestion

- **`_ensure_news_ingested()` added** (`agent_2_llamaindex/executor.py`): Fetches `get_news_sentiment` (15 articles) and ingests into ChromaDB `news` collection via `DocumentIngestionPipeline.ingest_news_article()`. Separate daily dedup key (`news_{ticker}`) so news and SEC filing ingestion run independently.
- **`query()` parallel ingest**: `asyncio.gather` for filings + news ingestion — both paths run concurrently.
- **Multi-collection query routing** (`agent_2_llamaindex/index_manager.py`): New `_classify_query_intent()` routes across `sec_filings`/`news`/`earnings` collections based on keywords; broad analytical queries hit all three. Deduplicates by text hash, synthesizes via `LlamaIndex response_synthesizer`. Returns warming signal when index is empty.
- **Agent card** (`agent_cards/rag_agent.json`): Added `financial_news_retrieval` skill with news/sentiment/market tags.

### Quant Agent — Fundamentals, Technicals, Data-Driven DCF

- **`fundamental_analysis_node`** (`agent_3_langgraph/nodes.py`): Fetches `get_financials`, extracts 25+ ratios (PE, PB, EV/EBITDA, ROE, ROA, margins, D/E, growth) + derived signals (pct from 52w high/low, golden cross, net debt). Sets `_financials_raw` for DCF reuse.
- **`technical_analysis_node`**: SMA 20/50/200, EMA 12/26, MACD + crossover, RSI(14), Bollinger bands, momentum 20d/60d, support/resistance, trend classification (strong_uptrend → downtrend).
- **`dcf_valuation_node` — data-driven assumptions**: WACC via CAPM + after-tax cost-of-debt (weighted by capital structure). Growth rate blended from `revenueGrowth`/`earningsGrowth` (bounded 2–25%). Tapered 5-year projection fading to terminal. Reuses `_financials_raw` when available.
- **`format_output_node` — expanded voting**: Adds fundamental signals (`low_pe`, `strong_roe`) and technical signals (`bullish_trend`, `oversold_rsi`) to signal voting. Reasoning surfaces DCF WACC/growth percentages.
- **`llm_summary_node` — enriched prompt**: 3-4 sentence summary including fundamentals + technicals; `max_tokens` 256→384.
- **Graph topology — parallel fan-out**: `START` → `fetch_prices` ∥ `fetch_fundamentals`; `fetch_prices` → `compute_base_metrics` ∥ `technical_analysis`; fan-in at `portfolio_correlation` + `format_output`.
- **State** (`agent_3_langgraph/state.py`): Added `fundamentals`, `technicals`, `_financials_raw` fields.

## v1.28 — Full Synthesis in save_brief + Cache-Hit on Company Names + RAG Latency Cut

### Full Synthesis Persistence

- **`save_brief` now stores full analysis** (`agent_1_adk/agent.py`): New `_synthesis_text_from_context()` reads the longest LLM-generated text from `session.events` on the first write. Both ADK-web and A2A paths persist the complete BUY/HOLD/SELL analysis instead of the short rationale. Falls back to rationale only when no model output exists in the turn.
- **`update_response_text` removed from `_persist_memory_callback`** (`agents/finsight_agent/agent.py`): The post-turn overwrite was unreliable (response-text extraction depended on event ordering) and blind to the A2A executor path (which doesn't fire `after_agent_callback`). Synthesis is now captured at `save_brief` time, which all paths call.
- **New test**: `tests/unit/memory/test_save_brief_persists_synthesis.py` (2 test cases) — covers both the synthesis-wins case and the rationale-fallback case.

### Ticker-Resolution Cache Fix

- **_memory_cache_callback falls back to MCP resolve** (`agents/finsight_agent/agent.py`): When the regex-extracted token misses in DB (e.g. user types "VISA" but the brief is stored under canonical "V"), the before-agent callback runs `resolve_company_ticker` and retries the cache lookup. Closes the asymmetry where `save_brief` dedup hit but the cache lookup missed.

### RAG Latency Reduction (~5x)

- **`response_mode="compact"` with `similarity_top_k=3`** (`agent_2_llamaindex/index_manager.py`): All three RAG query engines (SEC filings, financial filings, earnings) switched from default mode (N sequential `refine` calls) to `compact` — a single LLM call instead of one per retrieved chunk. `similarity_top_k` reduced from 5 to 3. Total LLM calls per RAG query: 1 (was up to 5).

### Configuration Changes

- **A2A timeouts bumped** (`shared/config.py`): `A2A_TIMEOUT` 180→680s, `A2A_TIMEOUT_RAG` 60→600s, `A2A_TIMEOUT_QUANT` 90→600s, `A2A_TIMEOUT_SENTIMENT` 45→600s — tolerates slow local LLM inference without spurious timeouts.
- **Default model updated** (`shared/config.py`): `LLM_MODEL` → `qwen/qwen3-30b-a3b-2507`, `ADK_MODEL` → `openai/qwen/qwen3-30b-a3b-2507`. Both already reflected in `.env.example`; default now matches.
- **`.env.example` cleaned** (`shared/config.py`): Removed unused `GOOGLE_API_KEY`.

## v1.27 — Security Sandbox Hardening + 60 AST-Gate Tests

### Sandbox Extraction & Hardening

- **`shared/sandbox.py` (new, 263 lines)**: Extracted the three-layer Python sandbox from `mcp_servers/finsight_server.py` into a dedicated shared module. `run_sandbox(code, timeout)` provides the same signature that `execute_python` called inline before.
- **Expanded import blocklist**: Added 20 new restricted modules: `shlex`, `concurrent`, `ssl`, `http`, `urllib`, `requests`, `ftplib`, `poplib`, `smtplib`, `telnetlib`, `xmlrpc`, `socketserver`, `pathlib`, `io`, `glob`, `fnmatch`, `tempfile`, `zipfile`, `tarfile`, `gzip`, `bz2`, `lzma`, `base64`, `codecs` — covering filesystem access, network protocols, and encoding-based escape vectors.
- **Windows-safe `resource` import**: Moved `import resource` inside `_sandbox_preexec()` (guarded by try/except) instead of a module-level `sys.platform` guard — avoids import errors on Windows while preserving Unix resource limits.
- **`mcp_servers/finsight_server.py`**: 230 lines removed — `_RESTRICTED_IMPORTS`, `_RESTRICTED_CALLS`, `_RESTRICTED_ATTRS`, `_check_code_safety()`, `_sandbox_preexec()`, `_SANDBOX_RUNNER`, and `execute_python` body replaced with `from shared.sandbox import run_sandbox as _run_sandbox` + a single delegation call.

### Security Test Suite

- **`tests/security/test_sandbox.py`** (~60 test cases): 45+ parametrized negative cases covering every restricted module, builtin, dunder attribute, getattr-with-dunder, and subscript-with-dunder pattern. 13 positive cases verifying safe code (math, json, list comprehensions, `isinstance`, `str`) is not blocked. Integration tests (marked `integration`) spawn the actual subprocess to verify runtime sandbox enforcement, timeout handling, and runtime import blocking.

## v1.26 — Pragmatic Test Suite (88 Unit Tests)

### New Test Suite

- **88 unit tests added** across 10 test files, covering all core primitives — models, quant graph nodes, ticker utilities, TTL cache, rate limiter, trace context, memory store, and ticker memory.
- **`tests/conftest.py` (new)**: Shared fixtures: `_clean_env` (autouse) monkeypatches `LLM_API_KEY`, `LLM_BASE_URL`, `LANGFUSE_*` for test isolation. `memory_db` fixture provides per-test isolated SQLite database by resetting the module-level connection singleton.
- **`tests/unit/test_models.py`** (10 tests): `QueryContext`, `RAGInsights`, `QuantMetrics`, `SentimentIntelligence`, `InvestmentBrief` — construction, serialization round-trip (`model_dump` → `model_validate`), optional fields, multiple recommendations.
- **`tests/unit/test_quant_graph_nodes.py`** (18 tests): All three LangGraph nodes tested directly with synthetic log-normal price data from `numpy.random.default_rng`. Covers Sharpe/VaR/max-drawdown correctness, high/low volatility branching, stress-test CVaR ≤ VaR invariant, empty-price edge cases, BUY/HOLD/SELL signal logic.
- **`tests/unit/test_ticker_utils.py`** (11 tests): Parametrized `is_valid_ticker_format`, `extract_ticker` with stop-word blocklist, `extract_holdings` with comma/and/colon syntax, `clean_query_for_resolution`.
- **`tests/unit/test_ttl_cache.py`** (9 tests): Cache miss/hit/expiry, single-flight dedup (N concurrent callers share one fetch), LRU eviction at `max_entries`, exception propagation.
- **`tests/unit/test_rate_limiter.py`** (4 tests): Burst consumption speed, rate enforcement after burst, token refill over time, burst=1 bucket timing.
- **`tests/unit/test_trace_context.py`** (8 tests): Inject/extract round-trip, missing prefix returns None, separator in task text, double injection, `current_trace_id` contextvar.
- **`tests/unit/memory/test_memory_store.py`** (5 tests): Table creation, idempotent `init_db`, schema version, WAL journal mode, required indexes.
- **`tests/unit/memory/test_ticker_memory.py`** (7 tests): Store/get_latest, case-insensitive ticker lookup, history retrieval, flip detection (`has_changed`), minimal store path.
- **`tests/integration/test_mcp_server_smoke.py`** (4 tests, marked `integration` + `external`): HTTP reachability of MCP server (port 8010) and agent card endpoints (ports 8002–8004).

### Configuration

- **`pyproject.toml`**: Added `asyncio_default_fixture_loop_scope = "function"`, custom pytest markers `integration` and `external`.

## v1.25 — Env Var Hardening & SQLite Connection Singleton

### Configuration: Env Vars for Secrets

- **`SEC_USER_AGENT` env var** (`shared/config.py`): Replaces hardcoded `"FinSight Research (contact@finsight.com)"` in `_SEC_HEADERS`. Defaults to a `"dev-mode-set-SEC_USER_AGENT"` placeholder with a startup warning when unset. Set `SEC_USER_AGENT=Your Name (your-email@example.com)` in `.env` for production.
- **`LLM_API_KEY` env var** (`shared/config.py`): Replaces `api_key="lmstudio"` hardcoded in 3 agent files. Defaults to `"lmstudio"` for backward compatibility with LM Studio. Enables switching to OpenAI/Anthropic by changing `.env` only.
- **Agent files updated**: `agent_2_llamaindex/index_manager.py`, `agent_3_langgraph/nodes.py`, `agent_4_crewai/crew.py` — import and use `LLM_API_KEY` from config.
- **MCP server updated** (`mcp_servers/finsight_server.py`): Imports `SEC_USER_AGENT` from config for the SEC EDGAR `_SEC_HEADERS`.
- **`.env.example` updated**: Documents both `LLM_API_KEY` and `SEC_USER_AGENT` with usage comments.

### SQLite Long-Lived Connection Singleton

- **Singleton connection** (`shared/memory/store.py`): `get_db()` now returns a module-level `_db_conn` singleton instead of opening a new connection per call. Double-checked locking via `_init_lock` prevents race conditions on first access.
- **Write lock** (`shared/memory/store.py`): New `write_lock()` function returns a module-level `asyncio.Lock`. All writers (`store_brief`, `store_minimal`, `update_response_text`, `upsert_from_context`, `update_holdings`, `record_recommendation`, `evaluate_all` updates, `add_session_to_memory`, `add_events_to_memory`, `mark_filing_ingested`) wrapped with `async with write_lock()`.
- **Reader cleanup**: All `try/finally + await conn.close()` removed from read paths — readers use the shared connection directly without close.
- **WAL + busy_timeout**: Set once at singleton init (no longer on every `get_db()` call).
- **`close_db()` added**: New function for process-shutdown cleanup.
- **Files changed**: `shared/memory/store.py`, `shared/memory/ticker_memory.py`, `shared/memory/portfolio_store.py`, `shared/memory/performance_tracker.py`, `shared/memory/memory_service.py` — 284 insertions, 304 deletions across 5 files.

### Token-Bucket Rate Limiter

- **`shared/rate_limiter.py` (new)**: `TokenBucket` class using `asyncio.Lock` + `time.monotonic()` with configurable rate and burst. Loop-based acquire waits exactly the deficit time instead of recursing.
- **SEC limiter** (`_sec_limiter`): 8 req/s, burst 10 — applied to 5 HTTP call sites in `_EdgarClient` (ticker map fetch, submissions, filing content, full-text search, EDGAR filing fetch). SEC's published limit is 10 req/s; 8/s leaves headroom.
- **yfinance limiter** (`_yfinance_limiter`): 4 req/s, burst 8 — applied to `get_prices`, `get_financials`, `get_options_chain`, `get_earnings_calendar`. Yahoo has no published cap; conservative rate avoids 429s.
- **RSS limiter** (`_rss_limiter`): 2 req/s, burst 4 — applied to `_fetch_rss` and `_fetch_yf_news` Yahoo fallback. News feeds are the least latency-sensitive.

### TTL Cache with Single-Flight Dedup

- **`shared/ttl_cache.py` (new)**: Async `TTLCache` class replacing the old threaded `_TTLCache`. Supports `get_or_fetch()` with single-flight dedup — N concurrent callers for the same key share one in-flight fetch, each receiving the result when done. Also exposes `get()`/`set()` for tools needing conditional caching.
- **Single-flight dedup**: Double-checked locking pattern — after acquiring the asyncio lock, re-checks cache to avoid redundant fetches on race wins. Pending fetches tracked in `_inflight` dict of `asyncio.Future` objects, created via `loop.create_future()`.
- **TTL updates**: Prices 5 min → 1 min (intraday prices change every trade), financials 24 h → 1 hr (quarterly data doesn't change intraday, but 24h was unnecessarily long; 1h matches typical session refreshes), news 15 min → 5 min (fresher headlines without hammering RSS).
- **Extracted uncached helpers**: `_get_prices_uncached` and `_get_financials_uncached` — the actual yfinance logic separated from the tool wrapper, making the cache layering explicit.
- **Filing/submission caches**: Keep their existing behavior (`_cache_filing` permanent LRU-200, `_cache_submissions` 6h) — now backed by `TTLCache` instead of `_TTLCache`.
- **Files changed**: `shared/ttl_cache.py` (new, 74 lines), `mcp_servers/finsight_server.py` (119 insertions, 78 deletions).

### Structured JSON Logging

- **`JsonFormatter` added** (`shared/logging_config.py`): New formatter for the file handler that writes one JSON object per line with keys: `ts`, `level`, `service`, `logger`, `message`, plus optional `trace_id`, `session_id`, `ticker`, `latency_ms` when set on the LogRecord. Exception tracebacks serialized as `exc` key.
- **StreamHandler kept plaintext**: Terminal output remains readable — only the file handler uses JSON. Prevents the "wall of JSON" problem in interactive terminals.
- **No code changes needed in callers**: `setup_file_logging(service_name)` signature unchanged. Existing callers (`setup_file_logging("orchestrator")`, etc.) automatically get JSON file logs after this change.

### Per-Service Log Levels via Env

- **Env-based log level resolution** (`shared/logging_config.py`): `setup_file_logging(service_name, level=None)` now reads `LOG_LEVEL_<SERVICE>` (e.g. `LOG_LEVEL_MCP`), falls back to `LOG_LEVEL`, then defaults to `INFO`. Callers can still pass `level=` explicitly to override env.
- **`.env.example` updated**: Documents `LOG_LEVEL` for global default and per-service override examples (`LOG_LEVEL_ORCHESTRATOR=DEBUG`, `LOG_LEVEL_QUANT=WARNING`).

### Log Sanitization Filter

- **`SanitizeFilter` added** (`shared/logging_config.py`): `logging.Filter` subclass with compiled regex patterns that scrub `api_key=` values, `sk-`/`pk-` tokens, `Bearer` authorization headers, and `LANGFUSE_PUBLIC/SECRET_KEY` values before the log line reaches any handler.
- **Attached to both handlers**: The filter is added to both the StreamHandler (terminal) and RotatingFileHandler (file), so secrets are never written to disk or displayed in console output.
- **Args scrubbing**: The `filter()` method also iterates `record.args` to catch formatted strings where secret values appear in `%s` placeholders (`"GET /api?key=%s" % secret`).

### SQLiteTaskStore Replacing InMemoryTaskStore

- **`shared/a2a_store.py` (new, 102 lines)**: `SQLiteTaskStore` implementing the A2A `TaskStore` protocol. Wraps `InMemoryTaskStore` for fast in-process get/list/delete and adds SQLite write-through via the `a2a_tasks` table. On cold start, all rows from SQLite are loaded into the in-memory store once — tasks survive process restarts.
- **Double-checked lazy load**: `_ensure_loaded()` uses an `asyncio.Lock` with double-checked locking to populate the in-memory store from SQLite exactly once on first access.
- **4 entry points updated**: `agent_1_adk/main.py`, `agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py` — `InMemoryTaskStore()` → `SQLiteTaskStore()`.
- **Schema migration**: `shared/memory/store.py` adds `a2a_tasks` table to `CREATE_TABLES_SQL`, bumps `SCHEMA_VERSION` to 3, and includes a migration block for existing databases.

### Memory Pruning / Retention Policy

- **`prune_old_records()` added** (`shared/memory/store.py`): Deletes rows older than `MEMORY_RETENTION_DAYS` (default 90) from `ticker_briefs`, `recommendation_records`, and `memory_entries`. Returns a dict of `{table: rows_deleted}`. Uses the existing `write_lock()` for concurrency safety.
- **Startup pruning** (`agent_1_adk/main.py`): Called on orchestrator startup — best-effort, wrapped in `try/except`. Logs deleted counts if any. Deliberately does not `VACUUM` to avoid blocking startup on large DB rewrites.
- **`.env.example` updated**: Documents `MEMORY_RETENTION_DAYS=90`.

### MCP Client Singleton with Auto-Reconnect

- **`get_shared_mcp()` added** (`shared/mcp_client.py`): Process-wide `MCPClient` singleton with double-checked async lock. Connects on first call; returns cached client on subsequent calls. Replaces per-request connect/disconnect in all executors, eliminating ~100–500ms SSE handshake overhead per request.
- **Auto-reconnect in `call_tool_by_name`**: On `ConnectionError`/`EOFError`/`asyncio.IncompleteReadError`, marks `_connected = False`, reconnects once, and retries the call. After 2 failures, raises. Prevents permanent MCP death on transient network blips.
- **`_connected` flag added**: Tracks connection state on `MCPClient`. Set `True` after `connect_all()`, `False` on reconnect attempts and in `disconnect_all()`.
- **`atexit` shutdown hook** (`_shutdown_mcp_sync`): Best-effort synchronous disconnect at process exit via a temporary event loop.
- **4 executors simplified**: `agent_1_adk/agent_executor.py`, `agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py` — removed `_ensure_connected()`, `_disconnect()`, `finally` blocks in `stream()`, and temporary MCP creation. Now call `get_shared_mcp()` directly. Net: −131 lines across 4 files.

### Lazy OpenTelemetry Instrumentation

- **`init_instrumentation()` added** (`shared/observability.py`): New function that wraps all `*Instrumentor().instrument()` calls — each server calls it once at startup. A `_instrumented` set prevents double-instrumentation even if `init_instrumentation()` is called multiple times in the same process.
- **Deferred imports**: All OTel/OpenInference imports moved inside `init_instrumentation()` — importing a server module in pytest no longer triggers OTel side-effects (OTLP exporter threads, span processor startup).
- **5 entry points simplified**: `agent_1_adk/main.py`, `agent_1_adk/sub_agent_client.py`, `agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py`, `agents/finsight_agent/__init__.py` — replaced module-level `*Instrumentor().instrument()` with `init_instrumentation("<agent_type>")`.

### Correlation-ID Propagation via ContextVar

- **`current_trace_id` / `current_session_id` ContextVars** (`shared/trace_context.py`): New `contextvars.ContextVar` instances carrying the active trace and session IDs across async boundaries without explicit parameter passing.
- **`extract_trace_ids()` now sets ContextVar**: After parsing the trace prefix from an inbound task, automatically sets `current_trace_id` — any subsequent log line carries the ID without manual `extra=` passing.
- **`JsonFormatter` fallback to ContextVar**: The formatter checks `record.trace_id` first, then falls back to `current_trace_id.get()`. Same for `session_id`. This means log lines emitted by MCP tool handlers automatically include the caller's trace_id if one has been set.
- **`generic_executor` sets both ContextVars**: Before executing an inbound A2A task, extracts trace_id from the query and sets `current_trace_id` + `current_session_id` from the task's `context_id`.
- **`sub_agent_client` sets ContextVar**: Before injecting trace context into an outbound task, sets `current_trace_id` from the Langfuse current trace.
- **MCP tool log lines**: Hot-path tools (`get_prices`, `get_financials`, `get_company_filings`, `get_news_sentiment`) now emit `logger.info("Tool called", extra={"tool": "...", "ticker": "..."})` — the formatter automatically adds the active `trace_id` from ContextVar, so `grep <trace_id> logs/*.log` returns the full cross-service flow.

### Deduplicate Ticker Validation across Executors

- **`validate_ticker()` and `resolve_ticker()` added** (`shared/ticker_utils.py`): Module-level wrappers that use `get_shared_mcp()` singleton internally. Replace the copy-pasted `_validate_ticker`/`_resolve_ticker` private methods across all 4 executors.
- **~80 lines removed from executors**: `agent_1_adk/agent_executor.py`, `agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py` — all private `_validate_ticker` and `_resolve_ticker` methods deleted. Each executor now calls the shared functions.
- **ADK executor inline MCP block replaced**: The input guardrail ticker pre-check in `agent_1_adk/agent_executor.py` (which previously created a temporary MCP client) now uses `validate_ticker()` from the shared singleton, eliminating the last ad-hoc MCP lifecycle in the codebase.

### Unified `@logged` Timing Decorator

- **`logged()` decorator added** (`shared/logging_config.py`): Emits `Enter` / `Exit` / `Fail` log lines with `latency_ms` as a structured JSON field. Uses `time.monotonic()` for precision and `fn.__qualname__` for consistent function identification.
- **Applied to sub-agent `_build_response()`**: `agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py` — all three async generators wrapped. (Only `_build_response` is decorated, not `stream()`, because decorators on async generators would break the `yield` protocol.)
- **Applied to `SubAgentClient.send_message()`**: Captures latency for each outbound A2A call from the orchestrator to a sub-agent.
- **Applied to 4 MCP tool handlers** (`finsight_server.py`): `get_prices`, `get_financials`, `get_company_filings`, `get_news_sentiment` — every hot-path MCP call emits structured latency lines.
- **Value**: `grep "Exit" logs/*.log` shows per-call latencies across every service layer — orchestrator, sub-agents, MCP — in a single command.

### Cancellation Support + Per-Agent Timeouts

- **`GenericAgentExecutor.cancel()` implemented** (`shared/generic_executor.py`): Stores `asyncio.current_task()` on `execute()`, catches `asyncio.CancelledError` to emit `TASK_STATE_CANCELED` before re-raising. `cancel()` now calls `self._task.cancel()` instead of raising `NotImplementedError`.
- **`FinSightAgentExecutor.cancel()` implemented** (`agent_1_adk/agent_executor.py`): Same pattern — stores task, cancels on request. Replaces `NotImplementedError`.
- **Per-agent timeouts** (`agent_1_adk/sub_agent_client.py`): `send_message()` wraps the streaming loop in `asyncio.wait_for()` with per-agent timeouts (RAG=60s, Quant=90s, Sentiment=45s) derived from agent name; falls back to global `A2A_TIMEOUT` (180s). `TimeoutError` returns a clean `{"error": "agent_timeout", "agent": ..., "timeout": ...}` JSON payload instead of crashing.
- **`shared/config.py`**: Added `A2A_TIMEOUT_RAG`, `A2A_TIMEOUT_QUANT`, `A2A_TIMEOUT_SENTIMENT` env vars with sensible defaults.
- **Eval-trace write moved to `finally`**: In `send_message()`, the eval-trace write block moved from `try` body to `finally` — ensures traces are captured even on timeout.

## v1.24 — Before-Agent Cache Callback, IST Timezone & Stale Test Cleanup

### Before-Agent Cache Callback (`_memory_cache_callback`)

- **Two-tier same-day cache** (`agents/finsight_agent/agent.py`): New `_memory_cache_callback` registered as `root_agent.before_agent_callback` — fires before the LLM runs, extracts the user's ticker, queries `TickerMemory.get_latest()`, and returns today's cached brief (`types.Content`) directly if available. Short-circuits the LLM entirely, saving 30-60s per repeat same-day query.
- **Strict prompt directive** (`agent_1_adk/agent.py`): `[TODAY]` tag changed from "you MAY return it directly" to **"you MUST return it directly"** — reduces LLM variance on same-day cache hits.
- **Executor-level cache** (`agent_1_adk/agent_executor.py`): `_get_today_cached_text()` provides a parallel short-circuit for the A2A executor path, checking before `RUNNER.run_async()` is called.

### Response Text Overwrite for Cache Quality

- **`update_response_text()` added** (`shared/memory/ticker_memory.py`): Overwrites `brief_json.response_text` on an existing record after the agent turn completes. The `save_brief` tool's rationale is a short LLM-written summary; after the full synthesis finishes, the real analysis text replaces it — so the same-day cache returns the rich analysis, not the abbreviated rationale.
- **Integration in `_persist_memory_callback`** (`agents/finsight_agent/agent.py`): After memory persist, extracts the response text and calls `tm.update_response_text()`.

### IST Timezone Standardization

- **`IST` constant added** (`shared/config.py`): `IST = timezone(timedelta(hours=5, minutes=30))`. All `datetime.now()` calls across the system converted to use `IST` explicitly — agent timestamps, memory timestamps, analysis_date comparisons. Previously mixed between UTC and local machine time, causing same-day cache mismatches on non-IST systems.
- **Files changed**: `shared/config.py`, `shared/memory/memory_service.py`, `shared/memory/performance_tracker.py`, `shared/memory/portfolio_store.py`, `shared/memory/store.py`, `shared/memory/ticker_memory.py`, `agent_1_adk/agent.py`, `agent_1_adk/agent_executor.py`.

### Programmatic Dedup in save_brief & _store_memory

- **`save_brief` dedup** (`agent_1_adk/agent.py`): Checks if today's brief already exists for the ticker before inserting. Returns early with a confirmation message instead of creating a duplicate row.
- **`_store_memory` dedup** (`agent_1_adk/agent_executor.py`): Same check at the executor level — if `save_brief` already stored today's brief, `_store_memory` skips its own insert. Creates two-layer defense against duplicate records.

### Ticker Extraction: Dotted & Single-Char Tickers

- **Dotted tickers supported** (`shared/ticker_utils.py`): Patterns now match `[A-Z]{1,5}(?:\.[A-Z]{1,2})?` — handles Berkshire Hathaway (`BRK.A`, `BRK.B`) and other class-share tickers.
- **Single-char tickers**: Pattern 5 changed from `[A-Z]{2}` to `[A-Z]{1,2}`, enabling detection of tickers like `V` (Visa) and `Y` (Alleghany). New mixed-case parens pattern detects `V (Visa)` → `V`.
- **`$` prefix widened**: Pattern 3 (dollar prefix) now matches `[A-Z]{1,5}` instead of just `[A-Z]{1,2}`.

### _build_response Extracted from stream()

- **All three sub-agents refactored** (`agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py`): The core response logic moved from inside `stream()` to a new `_build_response(query) → dict` method. `stream()` is now a thin wrapper: `yield await self._build_response(query)` in a `try/finally` block (ensuring `_disconnect()` runs). This makes the response-building logic independently testable and callable.

### DB Path Consolidation

- **Session DB separated** (`agent_1_adk/main.py`): ADK session store moved from `db/finsight_memory.db` to `db/adk_sessions.db` — separates conversational session data from ticker briefs and memory, preventing one schema migration from affecting the other.
- **User-id-agnostic cache queries**: `_get_today_cached_text()` and `_build_memory_context()` now query `TickerMemory.get_latest(ticker, user_id=None)` to avoid cache misses across different user_id values (a2a_user, user, eval_user).

### Stale Test Removal

- **17 test files removed**: All `tests/*.py` files and `tests/evaluation/` suite deleted — these were unmaintained fixtures from earlier architecture iterations that no longer matched the current codebase. Offline RAGAS evaluation pipeline, stale rubric tests, and outdated memory/integration tests all removed.
- **Test count: 0** — no automated test suite remains. Testing is performed manually via the ADK Web UI.

## v1.23 — Same-Day Memory Cache, analysis_date Column & Unified db/ Folder

### Same-Day Recommendation Cache

- **Date-aware memory injection** (`agent_1_adk/agent_executor.py`): `_build_memory_context()` now compares the stored brief's `analysis_date` against today's date. Context tagged `[TODAY]` when brief is from today (LLM may return directly without calling agents); tagged `[STALE]` when from a prior day (LLM must call all agents for fresh analysis).
- **Duplicate write prevention**: `_process_response()` skips the `_store_memory()` background task when `[TODAY]` is present in the injected user message, preventing identical records accumulating on same-day repeated queries.
- **Agent instruction updated** (`agent_1_adk/agent.py`): Both `_STATIC_PREAMBLE` and `_STATIC_PREAMBLE_FALLBACK` include a *MEMORY CONTEXT RULES* block instructing the LLM how to handle each tag.

### analysis_date Column

- **New `analysis_date TEXT` column** (`shared/memory/ticker_memory.py`, `shared/memory/store.py`): Added to `ticker_briefs` via idempotent `ALTER TABLE` migration in `init_db()`. Both `store_brief()` and `store_minimal()` write `date.today().isoformat()` into this column on every insert.
- **Read path updated**: `get_latest()` and `get_history()` select and return `analysis_date` as `row[9]`. Sort order changed to `ORDER BY COALESCE(analysis_date, created_at) DESC` — uses explicit date where available, falls back to `created_at` for legacy rows.

### Unified db/ Folder

- **All databases consolidated under `db/`**: `shared/memory/store.py` `DB_PATH` → `db/finsight_memory.db`. Session DB URL in `agent_1_adk/main.py` → `sqlite+aiosqlite:///./db/finsight_memory.db`. LangChain cache in `agent_3_langgraph/nodes.py` → `db/.langchain_cache.db`. ChromaDB default in `shared/config.py` (`CHROMA_DIR`) → `./db/chroma_db`.
- **`.gitignore` simplified**: All scattered per-file DB ignore entries replaced with a single `db/` rule. Removed duplicate entries and stale repeated lines.
- `db/` directory auto-created on first run via `path.parent.mkdir(parents=True, exist_ok=True)` in `get_db()`.

## v1.22 — ADK 2.x, Eval Toggle, Memory Pollution Fix & Score Namespacing

### Google ADK 2.x Upgrade

- **`google-adk` bumped to `>=2.0,<3.0`** (`pyproject.toml`): installed `2.1.0`. No code changes required — all public API surfaces (`LlmAgent`, `Runner`, `DatabaseSessionService`, `BaseMemoryService`, `google.adk.tools.{google_search, load_memory}`, `google.adk.cli.service_registry.get_service_registry`) verified stable. Custom `SQLiteMemoryService` still satisfies the 2.x `BaseMemoryService` signatures. Project's `BaseAgent` in `shared/base_agent.py` is unaffected (it is a Pydantic class, not ADK's `BaseAgent`).

### `EVAL_TRACE_ENABLED` Feature Flag

- **`EVAL_ENABLED` constant added** (`shared/config.py`): reads `EVAL_TRACE_ENABLED` from `.env` (default `True`). Single source of truth for whether sidecar RAGAS evals fire.
- **All `asyncio.create_task(_eval_*)` calls gated**: every agent now checks `if EVAL_ENABLED:` before scheduling its eval task. Sites: `agent_1_adk/agent_executor.py`, `agents/finsight_agent/agent.py` (orchestrator), `agent_2_llamaindex/executor.py` (RAG), `agent_3_langgraph/executor.py` (quant), `agent_4_crewai/executor.py` (sentiment). Set `EVAL_TRACE_ENABLED=False` in `.env` to disable all sidecar evals with no code changes.

### Orchestrator Eval Moved to `after_agent_callback`

- **Problem**: when running via `adk web`, the orchestrator goes through ADK's built-in runner — `FinSightAgentExecutor` is never invoked. The eval call in `agent_executor.py` only fired for A2A clients hitting `agent_1_adk/main.py`. With the orchestrator A2A server removed from the bat file, evals stopped firing entirely.
- **Fix** (`agents/finsight_agent/agent.py`): added orchestrator eval scheduling into the existing `_persist_memory_callback`. After memory persist, extracts user query + final agent text from `session.events`, pulls current Langfuse `trace_id`, and fires `asyncio.create_task(_eval_score_response(...))`. Works for both `adk web` and any other ADK runner path.

### Memory Persist + Eval Gated on `save_brief`

- **Problem**: `_persist_memory_callback` fired on every agent turn — including pure recall turns where the user asked "what were my last recommendations?". That conversational exchange was being indexed into long-term memory and evaluated, polluting future memory searches and inflating eval volume.
- **Fix** (`agents/finsight_agent/agent.py`): added `_is_analysis_turn()` which walks back to the most recent user message and checks whether `save_brief` was called after it. If not, both memory persist and eval are skipped. Logs `"Skipping persist + eval — turn did not call save_brief"` for visibility.
- Behaviour: "Analyze AAPL" → `save_brief` called → persists + evals. "What were my last recommendations?" → only `load_memory` → skipped. "Show me last NVDA brief, then analyze TSLA" → `save_brief` called for TSLA → persists.

### Langfuse Score Namespacing by Agent

- **`_push_scores` now prefixes scores by agent** (`shared/runtime_eval.py`): `ragas/{name}` → `ragas/{agent}/{name}` (e.g. `ragas/orchestrator/AnswerRelevancy`, `ragas/rag/Faithfulness`). The previous flat namespace made it impossible to distinguish "the orchestrator's AnswerRelevancy" from "RAG's AnswerRelevancy" in Langfuse.
- **`comment="agent=<name>"` added** to each `lf.create_score()` call for an additional structured tag.

### `RubricsScoreWithoutReference` Import Fix

- **Problem**: `score_response()` orchestrator eval imported `RubricsScoreWithoutReference` from `ragas.metrics.collections` — that class does not exist in ragas 0.4.x. The import failed and the entire orchestrator eval bailed out with `[orchestrator] Skipping eval: ragas import failed`.
- **Fix** (`shared/runtime_eval.py`): `recommendation_clarity` metric now uses `DomainSpecificRubrics` (the actual reference-free rubric class in 0.4.x). Same scoring rubric, working import.

### Removed Duplicate Batch-Eval Runner

- **Deleted from `shared/runtime_eval.py`**: `_invoke_agent()`, `_run_batch_eval()`, `_BATCH_EVAL_CASES`, and the `if __name__ == "__main__":` block. `_invoke_agent` spun up its own `Runner` + `InMemorySessionService` to invoke the orchestrator — duplicating exactly what `FinSightAgentExecutor` and `after_agent_callback` already do for live traffic. The live executor has the response in hand; no second runner needed.
- Batch evaluation with ground-truth references still lives in `tests/evaluation/run_orchestrator_eval.py`.

### Bat-File Cleanup

- **Orchestrator A2A server removed from `run_adk_web.bat`**: `agent_1_adk/main.py` is no longer started. The orchestrator runs through `adk web` on port `8080`. The A2A endpoint at `:8001` is no longer exposed by default; bring it back manually with `uv run python -m agent_1_adk.main` if needed for A2A clients.
- `stop_servers.bat` already kills the port; PowerShell terminal-close command targets all `uv run` and `lms server` windows reliably.

## v1.21 — Runtime RAGAS Robustness & Debuggability

### RAGAS Client Caching

- **`_setup_ragas_clients()` now caches** (`shared/runtime_eval.py`): Module-level `_ragas_clients` tuple stores `(InstructorLLM, _STEmbeddings)` after first call. Subsequent calls return cached clients instead of reloading `SentenceTransformer(all-MiniLM-L6-v2)` (~1-2s, ~80MB) on every agent response. Eliminates 4× model reload per query.

### Per-Metric Streaming

- **`_run_metrics` switched to `asyncio.wait(FIRST_COMPLETED)`** (`shared/runtime_eval.py`): Replaced `asyncio.gather` (waits for all metrics). Each metric is now logged and pushed to Langfuse the moment its `ascore` finishes. Fast metrics (AnswerRelevancy, DomainSpecificRubrics ~3-5s) appear immediately instead of waiting for slow metrics (Faithfulness ~180s timeout).

### Error Handling

- **`BaseException` instead of `Exception`** in `_run_metrics` result loop: `CancelledError` inherits from `BaseException`, not `Exception` — the old `isinstance(result, Exception)` check missed cancelled tasks, fell through to `round(float(result), 4)`, crashed with `TypeError`, and silently killed the entire eval via `create_task` fire-and-forget. Fixed by checking `isinstance(result, BaseException)`.
- **`float()` conversion guarded** with `try/except (TypeError, ValueError)` — any unexpected result types are logged instead of crashing.
- **`_score_metric` try/except** added: wraps `metric.ascore()` and logs full traceback with `exc_info=True` when a metric fails internally.
- **All scoring functions wrapped in try/except**: Orchestrator, Sentiment eval bodies catch unexpected exceptions and log them with full traceback instead of silently disappearing.

### Debuggability

- **Entry logs added**: Each scoring function logs `[agent] Eval entered (response_len=..., trace=...)` at INFO level on entry, confirming the function was reached.
- **Early-return warnings**: Silent `return` on short responses or import failures now logs `[agent] Skipping eval: ...` with reason.
- **Fallback logs promoted**: `logger.debug("[agent] No RAGAS scores computed")` → `logger.info` — visible at default log level.

### Timeout & Encoding Fixes

- **HTTP timeout 60s → 180s** (`shared/runtime_eval.py`): `AsyncOpenAI(timeout=180)` — Faithfulness makes multiple sequential LLM calls (decompose claims → verify each), each taking ~20-30s on the 20B model. The old 60s timeout failed on the second call.
- **UTF-8 stdout/stderr** (`shared/config.py`): `sys.stdout.reconfigure(encoding='utf-8')` prevents `UnicodeEncodeError` when RAGAS log messages containing curly quotes (`\u2010`, `\u2011`) hit Windows cp1252 console.

### Langfuse Push Cleanup

- **`_push_scores` skips when trace_id is None** (`shared/runtime_eval.py`): With placeholder Langfuse API keys (`pk-lf-...`), `create_score()` with no `trace_id` resulted in "Bad request" API errors. Now returns early when `trace_id is None`.

### Sentiment Narrative Key Fallback

- **`narrative` key fallback** (`agent_4_crewai/executor.py`): CrewAI LLM may return JSON with `investment_narrative` or `analysis` instead of `narrative`. The eval now tries `narrative` → `investment_narrative` → `analysis` → full JSON dump before giving up.

### Gitignore

- **`tests/evaluation/eval_results/` added to `.gitignore`**: Runtime-generated trace JSON artifacts excluded from version control.

## v1.20 — Runtime RAGAS Evaluation & Offline HF Model Loading

### Runtime RAGAS Evaluation

- **`shared/runtime_eval.py` (new)**: Fire-and-forget RAGAS scoring for all four agents as background tasks. Uses `ragas` metrics without requiring ground-truth references — scores are computed at runtime using the LLM itself as the judge.

- **Orchestrator scoring** (`agent_1_adk/agent_executor.py`): After response processing, fires `asyncio.create_task(_eval_score_response(...))`. Metrics: `ResponseRelevancy`, `citation_quality` (AspectCritic), `risk_disclosure` (AspectCritic), `recommendation_clarity` (RubricsScoreWithoutReference), `response_completeness` (AspectCritic).

- **RAG agent scoring** (`agent_2_llamaindex/executor.py`): After query response, fires `asyncio.create_task(_eval_rag_response(...))`. Metrics: `Faithfulness`, `ResponseRelevancy`, `LLMContextPrecisionWithoutReference`. Requires `context_texts` from ChromaDB source nodes.

- **Quant agent scoring** (`agent_3_langgraph/executor.py`): After analysis, fires `asyncio.create_task(_eval_quant_response(...))`. Metrics: `FactualCorrectness` (uses computed metrics as reference — catches hallucinated numbers), `ResponseRelevancy`.

- **Sentiment agent scoring** (`agent_4_crewai/executor.py`): After narrative, fires `asyncio.create_task(_eval_sentiment_response(...))`. Metrics: `ResponseRelevancy`, `catalyst_identification` (AspectCritic), `insider_signal_discussion` (AspectCritic), `Faithfulness` (when news/filing contexts available).

- **Score push to Langfuse**: All scores pushed to Langfuse `create_score()` per-trace, linked by `trace_id` when available. Enables regression tracking across model/prompt changes.

- **LM Studio compatibility patched**: RAGAS defaults to `instructor.Mode.JSON` which sends `response_format.type="json_object"` — LM Studio only supports `"json_schema"` or `"text"`. Patched with `instructor.Mode.JSON_SCHEMA` in `_setup_ragas_clients()`. HuggingFace embeddings wrapped via custom `_STEmbeddings` (RAGAS 0.4.x `BaseRagasEmbedding`) to avoid broken pydantic integration.

- **Eval trace directory updated** (`agent_1_adk/sub_agent_client.py`): Changed from `eval_traces/` to `tests/evaluation/eval_results/orchestrator_traces/` to align with test suite layout.

### CrewAI Simplification

- **Sentiment crew reduced from 2 agents to 1** (`agent_4_crewai/crew.py`): Removed separate Synthesis Agent — the Analysis Agent now produces the full narrative directly. `build_crew()` simplified from a 2-agent `Crew` with `sequential` process to a single-agent `Crew`. Reduces LLM calls per sentiment query from 2 to 1.

### Offline HuggingFace Model Loading

- **`HF_HUB_OFFLINE=1` default** (`shared/config.py`): Set at import time before any HuggingFace code runs. Prevents network calls to `huggingface.co` when loading `sentence-transformers` or `all-MiniLM-L6-v2` — models are expected to be cached locally from a prior online run. Set `HF_HUB_OFFLINE=0` in `.env` to re-enable download checks.

### Index Manager Cleanup

- **Duplicated query methods removed** (`agent_2_llamaindex/index_manager.py`): `query_sec_filings()` and `query_earnings()` were dead code — the RAG agent only calls `query()` (which routes via `RouterQueryEngine` with fallback). Removed both methods along with `query_earnings` index collection setup.

### Configuration

- **`A2A_TIMEOUT` default reduced** (`shared/config.py`): Changed from `300.0` to `180.0` — 5 minutes was excessive for local LLM inference; 3 minutes provides sufficient margin while failing faster on genuinely stuck agents.

## v1.19 — MCP Connection Cleanup & Server Script Fixes

### MCP Connection Cleanup

- **`_disconnect()` added to all sub-agent executors** (`agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py`): New `async def _disconnect()` method calls `mcp.disconnect_all()` in a `try/finally` block, ensuring MCP sockets close gracefully after each analysis stream completes. Prevents `ConnectionResetError: [WinError 10054]` on Windows caused by lingering async sockets.
- **Orchestrator temporary MCP cleanup** (`agent_1_adk/agent_executor.py`): Pre-flight ticker validation's temporary MCP connection now wrapped in `try/finally` with `await _mcp.disconnect_all()` in the `finally` block.
- **All four agents disconnect MCP after stream**: Quant, RAG, Sentiment agents call `await self._disconnect()` in their `finally` blocks. Orchestrator cleans up the ticker-validation MCP client after use.

### Server Script Fixes

- **`run_adk_web.bat`**: Changed `cmd /c` to `cmd /k` for all server start commands — terminal windows stay open if a server crashes, allowing error inspection.
- **`stop_servers.bat`**: Rewrote window-closing logic. Switched from unreliable `taskkill /fi "WINDOWTITLE eq"` to PowerShell `Get-Process cmd | Where-Object { $_.MainWindowTitle -like 'FinSight*' } | Stop-Process -Force`, which reliably closes terminal windows by title.

### Bug Fixes

- **Date placeholder removed from ADK prompt** (`agent_1_adk/agent.py`): Removed `{date}` template variable from the orchestrator system prompt — the date was not being populated, leaving a raw `{date}` string visible in the LLM context.

## v1.18 — Caching, Guardrails, Evaluation & Observability

### Caching

- **TTL tool-result cache in MCP server** (`mcp_servers/finsight_server.py`): `_TTLCache` class using `OrderedDict` + `time.monotonic()`. Cache instances per tool: `get_prices` (5 min), `get_financials` (24 h), `get_news_sentiment` (15 min), `get_filing_content` (permanent LRU-200), `_fetch_submissions` (6 h). No new dependencies.
- **LangChain SQLiteCache** (`agent_3_langgraph/nodes.py`): `SQLiteCache(database_path="db/.langchain_cache.db")` wraps the quant agent's LLM summary call — identical ticker+metrics inputs reuse the cached LLM response. Requires `langchain-community>=0.3.0`.
- **KV cache prefix optimization** (`agent_1_adk/agent.py`, `agent_4_crewai/crew.py`): Static PROCEDURE block extracted to module-level `_STATIC_PREAMBLE` constant. `_build_instruction()` now only appends today's date and the dynamic agent list, keeping the large static prefix stable across requests for LM Studio KV-cache reuse. Backstory strings for CrewAI agents moved to module-level constants.
- **Semantic cache** (`shared/semantic_cache.py`): ChromaDB + `all-MiniLM-L6-v2` cosine similarity cache (threshold 0.95, TTL 1 h). Wired into `agent_1_adk/agent_executor.py`: cache checked before `runner.run_async`, hit returns immediately; successful responses stored. Controlled by `SEMANTIC_CACHE_ENABLED=true` env var (off by default).

### Guardrails

- **Input guardrails** (`agent_1_adk/agent_executor.py`): Off-topic regex filter (`_NON_INVESTMENT_RE`) rejects weather/recipe/entertainment queries with a canned message in < 100 ms. Pre-flight ticker validation calls MCP `validate_ticker` before spawning sub-agents — invalid tickers rejected in < 2 s with no sub-agent cost.
- **Output guardrails** (`agent_1_adk/agent_executor.py`): Empty/short response (< 50 chars) marked `TASK_STATE_FAILED`. Missing BUY/HOLD/SELL signal on a stock analysis query logs a Langfuse warning with `missing_signal: true` metadata.
- **Double-`else` syntax bug fixed** (`agent_1_adk/agent_executor.py`): Two `else` clauses for the same `if final_event:` block collapsed into one, fixing a `SyntaxError` that prevented the service from starting.

### RAG & Memory

- **Incremental RAG ingestion** (`shared/memory/store.py`, `agent_2_llamaindex/executor.py`): New `ingested_filings` table tracks already-indexed SEC filing URLs. `_ensure_ingested()` skips URLs already in the table; marks new ones after successful batch ingest. Persists across restarts — immutable historical filings are never re-ingested.
- **Embedding model pre-warm** (`agent_2_llamaindex/server.py`): `FinancialIndexManager` instantiated in a thread executor via `on_startup` hook, eliminating first-query latency caused by lazy model download.
- **Live price capture in PerformanceTracker** (`shared/memory/performance_tracker.py`): `record_recommendation()` now auto-fetches current price via `yfinance` in a thread executor when `price=None`. Enables accurate `realized_return` calculation in `evaluate_all()`.
- **Automated past-recommendation evaluation** (`agent_1_adk/agent.py`): `save_brief()` now fires `asyncio.create_task(_evaluate_past_recommendations(ticker))` in background — `PerformanceTracker.evaluate_all()` runs without blocking the response.
- **SQLite schema v2** (`shared/memory/store.py`): `ingested_filings` table added with `idx_ingested_ticker` index. `SCHEMA_VERSION` bumped to 2 for clean migration.

### Evaluation

- **RAGAS evaluation pipeline** (`tests/evaluation/`): `run_rag_eval.py` measures Faithfulness, ResponseRelevancy, ContextPrecision, ContextRecall, NoiseSensitivity. `run_orchestrator_eval.py` measures ToolCallAccuracy and AgentGoalAccuracy via eval traces written by sub-agent client. `financial_rubrics.py` provides custom `AspectCritic` metrics: citation quality, risk disclosure, recommendation clarity. `push_scores.py` pushes all scores to Langfuse per-trace.
- **Eval trace capture** (`agent_1_adk/sub_agent_client.py`): When `EVAL_TRACE_ENABLED=true`, each sub-agent call appends `{agent_name, task_sent, response, latency_ms}` to a JSON file in `tests/evaluation/eval_results/orchestrator_traces/`.
- **Curated RAG dataset** (`tests/evaluation/rag_dataset.json`): 10 Q&A pairs for NVDA, AAPL, MSFT, JPM with reference contexts from real SEC filings.

### Observability

- **LangGraph / LangChain instrumentation** (`agent_3_langgraph/server.py`): `LangChainInstrumentor().instrument()` added — quant agent LLM calls now appear in Langfuse traces. Requires `openinference-instrumentation-langchain>=0.1.0`.
- **Sub-agent latency tracking** (`agent_1_adk/sub_agent_client.py`): `send_message()` now records wall-clock latency per sub-agent call and emits a Langfuse span with `latency_ms` and agent name metadata.
- **Config validation** (`shared/config.py`): `validate()` function checks required env vars (`MCP_SERVER_URL`) and warns on placeholder Langfuse keys. Called at startup in each server entry point.

### Deployment

- **Health endpoints** (`agent_1_adk/main.py`, `agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py`, `mcp_servers/finsight_server.py`): All five services expose `GET /health → {"status":"ok","agent":"..."}`. MCP server mounts health alongside the SSE app via a Starlette wrapper.
- **docker-compose hardening** (`docker-compose.yml`): All services gain `healthcheck` blocks. `depends_on` updated to `condition: service_healthy`. `finsight_memory` named volume added for the orchestrator DB. `SEMANTIC_CACHE_ENABLED=false` added to `agent-adk` env.
- **Orchestrator Dockerfile** (`agent_1_adk/Dockerfile`): New container image following `agent_3_langgraph/Dockerfile` pattern — Python 3.12-slim, copies `agent_1_adk/` + `shared/`, exposes port 8001.
- **`langchain-community>=0.3.0`** and **`openinference-instrumentation-langchain>=0.1.0`** added to `pyproject.toml` deps. **`ragas>=0.2.0`** added to `[project.optional-dependencies] dev`.

## v1.17 — Centralized File Logging

- **`shared/logging_config.py` added**: `setup_file_logging(service_name)` configures the root logger with a `StreamHandler` (stderr) and a `RotatingFileHandler` (10 MB, 5 backups). Safe to call multiple times — duplicate handlers are skipped.
- **All services write to `logs/`**: Orchestrator → `logs/orchestrator.log`, RAG → `logs/rag_agent.log`, Quant → `logs/quant.log`, Sentiment → `logs/sentiment.log`, MCP → `logs/mcp.log`. Directory is created automatically if absent.
- **`memory_callback.log` moved to `logs/`**: Was written to the project root; now at `logs/memory_callback.log`.
- **`basicConfig` removed from all servers**: Stale `logging.basicConfig(level=logging.INFO)` calls replaced by module-level `setup_file_logging(...)`, so logging is configured whether the server is imported (uvicorn) or run directly.

## v1.16 — Code Streamlining & Bug Fixes

- **Ticker validation consolidated**: `_validate_ticker()` and `_resolve_ticker()` were copy-pasted verbatim (~108 LOC) across all three sub-agent executors. Extracted as `validate_ticker_via_mcp(mcp, ticker)` and `resolve_ticker_via_mcp(mcp, query, exclude_ticker)` in `shared/ticker_utils.py`. Each agent's methods are now ~7-line wrappers.
- **Dead config vars removed from `shared/config.py`**: `RAG_AGENT_URL`, `QUANT_AGENT_URL`, `SENTIMENT_AGENT_URL` (superseded by `AGENT_SEED_URLS`) and `ORCHESTRATOR_PORT`, `RAG_PORT`, `QUANT_PORT`, `SENTIMENT_PORT` (hardcoded in each server file, never imported from config).
- **`import json` inside loops fixed**: `agent_3_langgraph/executor.py` and `agent_4_crewai/executor.py` had `import json` inside `try` blocks inside loops; hoisted to module level.
- **RAG agent MCP connection refactored**: Inline connect pattern repeated in `_ensure_ingested`, `_validate_ticker`, and `_resolve_ticker` consolidated into a single `_ensure_mcp_connected()` helper.
- **Correlation matrix auto-trigger fixed**: Stored portfolio holdings from memory context were silently injected into every query, causing the quant agent to compute a full correlation matrix even for single-ticker requests. Fixed by (1) labelling the memory-context portfolio line as background reference and (2) updating the orchestrator prompt to only pass holdings to the quant agent when the user explicitly requests portfolio correlation in their current message.

## v1.15 — Dead Code Cleanup

- **`shared/types.py` removed**: Defined `ServerConfig`, `PlannerTask`, `TaskList`, `AgentResponse` — none were imported or referenced by any file in the project. These types were superseded by models in `shared/models.py`.
- **`shared/workflow.py` removed**: Defined `WorkflowGraph`, `WorkflowNode`, `Status` — never imported by any production code. The system uses LangGraph for the quant agent's state machine instead.
- **`tests/test_workflow.py` removed**: 8 tests for the unused `WorkflowGraph` implementation.
- **`ui/memory_test.html` and `ui/test.html` removed**: Standalone HTML pages with zero references from any source or configuration.
- **README updated**: Removed stale reference to `shared/types.py` from project structure diagram.
- **TESTS.md updated**: Test count corrected from 72 → 64.

## v1.14 — `load_memory` Fix & RAG Timeout Optimization

- **`load_memory` now returns results**: Root cause was `SQLiteMemoryService.add_events_to_memory()` requiring `app_name` and `user_id` as mandatory args, but ADK's `Context.add_events_to_memory()` only passes `events` and `custom_metadata`. Fixed by making `app_name` and `user_id` optional with defaults, and extracting them from `custom_metadata` when not provided directly.
- **`after_agent_callback` signature corrected**: ADK's `CallbackContext.add_events_to_memory()` takes `(self, *, events, custom_metadata=None)` — not `(app_name, user_id, events, session_id)`. Updated `agents/finsight_agent/agent.py` to pass events via `custom_metadata` with user_id, session_id, and app_name embedded.
- **Dual persistence path**: Events are now persisted to `memory_entries` both via `after_agent_callback` (ADK web UI path) and `_persist_to_memory` (A2A executor path), ensuring memory works regardless of how the agent is invoked.
- **`_persist_to_memory` added to `agent_executor.py`**: After each successful response, events are directly persisted to the runner's memory service. This bypasses the unreliable callback chain for A2A requests.
- **RAG retrieval deduplication**: Reduced `similarity_top_k` from 5 → 3 across all index query engines in `index_manager.py` to cut context size and LLM inference time by ~40%.

- **`DatabaseSessionService` replaces `InMemorySessionService`**: ADK's built-in `DatabaseSessionService` with `sqlite+aiosqlite:///./db/finsight_memory.db` provides persistent session/event storage across restarts. Full conversation history (user messages, agent responses, tool calls) is saved to SQLite.
- **`SQLiteMemoryService` for cross-session memory search**: Custom implementation of ADK's `BaseMemoryService` that persists conversation events to SQLite. The `load_memory` tool can search past conversations across sessions and restarts. Sessions are auto-ingested after each successful response.
- **`TickerMemory` for structured brief history**: Stores per-ticker investment recommendations with ticker, recommendation (BUY/HOLD/SELL), confidence, full response text, and timestamp. Provides `format_context()` that generates a compact (~300 token) memory summary injected into the orchestrator's system prompt before each query.
- **`PortfolioStore` for user profile persistence**: Auto-captures portfolio holdings from each query's context. Merges holdings over time — users never need to explicitly set their portfolio. Stores risk profile and investment horizon.
- **`PerformanceTracker` for recommendation outcomes**: Records each BUY/HOLD/SELL recommendation with optional price snapshot. Can evaluate past recommendations against current market prices via yfinance. Provides accuracy stats (win rate by recommendation type).
- **Memory context injection**: Before each query, the executor extracts the ticker, retrieves the latest recommendation from `TickerMemory`, and prepends it to the user message. This enables the LLM to answer "Has the outlook for NVDA changed since last time?"
- **Auto-save on every response**: `agent_executor.py` automatically stores briefs, recommendations, and portfolio updates after every successful response — no LLM action required.
- **`save_brief` tool removed**: Simplified to auto-save only. The LLM no longer needs to explicitly call a tool to persist its analysis.
- **`load_memory` tool added to orchestrator**: The ADK `load_memory` tool is now available to the orchestrator LLM for searching past conversations.
- **`db/` folder added to `.gitignore`**: All database files (`finsight_memory.db`, `chroma_db/`, `.langchain_cache.db`) consolidated under `db/` and excluded via a single rule.
- **16 tests passing** in `tests/test_memory.py`: covers all four memory stores (TickerMemory, PortfolioStore, PerformanceTracker, SQLiteMemoryService) plus the SQLite foundation.

## v1.12 — A2A Span Noise Filtering

- **Noisy A2A internal spans filtered**: Replaced `should_export_span=lambda span: True` with `is_default_export_span` from `langfuse.span_filter`. A2A SDK internal spans (`a2a-python-sdk` instrumentation scope) and HTTPX transport spans are no longer exported to Langfuse, keeping traces clean and focused on high-level workflow steps and LLM calls.
- **What's preserved**: `finsight-query` root traces, `orchestrator-execute`, `rag-agent-stream`, `quant-agent-stream`, `sentiment-agent-stream`, and all LLM spans (LiteLLM, LlamaIndex, LangChain, CrewAI).
- **What's filtered**: A2A `send_message` internals, `DefaultRequestHandler`, HTTPX transport spans, and other infrastructure spans.

## v1.11 — Portfolio Holdings Extraction & Correlation Matrix Fix

- **`extract_holdings()` added to `shared/ticker_utils.py`**: Extracts portfolio holdings from natural language queries using 4 regex patterns covering common phrasing: "My portfolio holds AAPL, MSFT", "I own MSFT and GOOGL", "My portfolio: TSLA, AMZN, META", "My current holdings are JPM, BAC, WFC".
- **Holdings passed through Quant agent chain**: `stream()` → `analyze(portfolio_holdings=...)` → `graph.run(portfolio_holdings=...)` → `correlation_node`. Previously holdings were always `None` regardless of user input.
- **Correlation matrix now returns helpful notes**: Instead of empty `{}`, returns `{"note": "No portfolio holdings provided..."}` when no holdings mentioned, and `{"error": "..."}` when correlation computation fails.
- **Orchestrator LLM instructed to pass holdings**: Updated orchestrator system prompt (step 4) telling the LLM to include portfolio holdings in the task text for the Quant Analysis Agent.
- **6 new tests for `extract_holdings()`**: Covers portfolio holds, colon syntax, and/or connector, no holdings mentioned, exclude target ticker, current positions phrasing.
- **14 total tests in `test_trace_propagation.py`**: 8 trace propagation + 6 holdings extraction.

## v1.10 — Langfuse Distributed Tracing Fix

- **Cross-process trace propagation fixed**: Sub-agent spans now correctly link to the orchestrator's root trace instead of creating orphan traces. Each agent process previously created its own root trace because `start_observation(trace_context=...)` was not properly linking spans across process boundaries.
- **`extract_trace_ids()` helper added**: New function in `shared/trace_context.py` that returns `(trace_id, parent_span_id, clean_query)` — a convenience wrapper over `extract_trace_context()` for the common case of needing explicit IDs.
- **`start_observation()` with explicit `trace_context`**: All three sub-agents (RAG, Quant, Sentiment) now use `langfuse.start_observation(..., trace_context=trace_ctx)` where `trace_ctx` is built from the injected `trace_id` and `parent_span_id` passed through the A2A message text prefix.
- **`CallbackHandler(trace_context=...)` for LangGraph**: Quant agent passes `trace_context` dict to Langfuse's LangChain `CallbackHandler` so internal graph nodes (fetch_prices, compute_metrics, dcf_valuation, llm_summary) are also linked to the parent trace.
- **`start_observation` over `start_as_current_observation`**: Used `start_observation()` (manual, no OTel context management) instead of `start_as_current_observation()` (context manager) because the latter conflicts with async generators — OTel context tokens are created in a different async context, causing `ValueError: Token was created in a different Context`.
- **Trace context injection unchanged**: `inject_trace_context()` in `sub_agent_client.py` already serialized `trace_id` + `parent_span_id` as a JSON prefix in the A2A task text. The fix was on the extraction/usage side.
- **8 trace propagation tests passing**: Added `test_extract_trace_ids_roundtrip` and `test_extract_trace_ids_no_context` to verify the new helper.

## v1.9 — Logging Overhaul & DCF Skip Messaging

- **Comprehensive logging added**: 11 new `logger.info/warning/debug` calls across `graph.py`, `nodes.py`, and `executor.py` — routing decisions, metric computation failures, DCF fallbacks, beta calculation errors, format-output summaries, and execution lifecycle
- **`dcf_error` now set on high-volatility route**: `compute_metrics_node` sets `dcf_error` when volatility > 35% so callers see why DCF was skipped (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead")
- **`dcf_error` included in graph output**: `graph.run()` now returns `dcf_error` in its result dict — previously only kept in state, never surfaced
- **`dcf_error` added to reasoning**: `format_output_node` includes `dcf_error` in the reasoning string when DCF is null with an error, so the LLM summary has context
- **`dcf_error: None` in initial state**: Added missing field to graph initial state for TypedDict consistency
- **`quant.log` routing diagnostics**: `_route_on_volatility` logs which branch was taken with ticker + volatility value
- **FCF debug logging**: `_get_fcf_from_financials` logs every FCF candidate examined per period (debug level) — no more silent "null" on cash flow parsing

## v1.8 — Documentation & Housekeeping

- **All docs updated**: README, TESTS, ARCHITECTURE, AGENTS, MCP_SERVERS, DEMO, CHANGELOG, DESIGN_DECISIONS synced with codebase
- **README diagram expanded**: MCP tool list updated to include all 13 tools (added `get_options_chain`, `get_financial_filings`, `get_filing_content`, `validate_ticker`, `resolve_company_ticker`, `get_earnings_calendar`)
- **README shared section**: Added `ticker_utils.py` to project structure
- **TESTS.md**: Corrected test count from 39→42, added `test_orchestrator_tools.py`, updated per-file test counts
- **ARCHITECTURE.md**: LLM models updated from `gpt-oss-20b` to `qwen/qwen3-30b-a3b-2507`; MCP tool diagram expanded
- **MCP_SERVERS.md**: Added `get_financial_filings`, `validate_ticker`, `resolve_company_ticker` tool documentation
- **DEMO.md**: Updated to reflect parallel calling with qwen model; corrected tool description to single `send_message`

## v1.7 — MCP Server Refactoring & News Fallback

- **`_EdgarClient` refactored**: Extracted `_build_filing_urls()` and `_fetch_submissions()` methods. `_INDEX_ONLY_FORMS` moved to module level. Added `FINANCIAL_FORM_TYPES` constant.
- **New `get_financial_filings` tool**: Fetches 10-K/10-Q filings separately with balanced annual/quarterly limits. Prevents the common failure of `get_company_filings` returning mostly 8-Ks. Separates annual from quarterly in response.
- **RSS fetching rewritten**: `_fetch_rss()` now returns a structured dict (`entries`, `status`, `error`) instead of a raw feedparser object. All three feeds fetched concurrently via `asyncio.gather`. Each source gets individual `feed_status` diagnostics.
- **Yahoo Finance news API fallback**: `_fetch_yf_news()` tries Yahoo Finance's structured news search when all RSS feeds are unreachable or return zero matching articles. Results are pre-filtered to the ticker — no keyword matching needed.
- **`get_news_sentiment` enhanced**: Added `feed_status` (per-source diagnostics), `source_used` field ("rss" / "yahoo_finance_api" / "none"), and improved error messaging. Distinguishes rss_unreachable vs rss_no_match.
- **Keyword matching improved**: Extracted `_resolve_company_keywords()` to build ticker+company-name keyword lists with bigrams and split-at-2 variants. `_keyword_matches()` uses word boundaries for single-word keywords to avoid substring false positives.

## v1.6 — DCF Robustness, Ticker False Positives & Date Awareness

- **DCF finds first positive FCF**: `_get_fcf_from_financials()` skips negative FCF periods instead of returning null. Logs detailed `dcf_error` at each failure point (no MCP client, empty cash flow, no positive FCF, missing shares/price).
- **`dcf_error` field in state**: New `QuantAnalysisState.dcf_error` captures exact DCF failure reason.
- **Financial stop-word blocklist**: `_FINANCIAL_STOP_WORDS` filters out "SEC", "EPS", "CEO", "NYSE", "NASDAQ", "INC", "GAAP" from regex ticker matches — prevents "Analyze GE SEC filings" from extracting "SEC" instead of "GE".
- **Query noise cleanup**: `clean_query_for_resolution()` strips analysis noise words before sending to MCP's `resolve_company_ticker`. Failed ticker candidates are excluded from the resolution query.
- **Date hallucination fix**: All LLM prompts now include `Today's date: {current_date}` — orchestrator, RAG queries, quant summary, and sentiment crew.
- **MCP: `follow_redirects=True`**: httpx client follows 301 redirects automatically.
- **MCP: CIK leading-zero fix**: EDGAR archive URLs use `str(int(cik))` instead of 10-digit zero-padded CIK, preventing 301 redirects.
- **MCP: IXBRL viewer pages parsed**: Removed "XBRL Viewer" skip — pages parsed with `html.parser`. `lxml-xml` fallback to `html.parser`.
- **MCP: MarketWatch RSS updated**: `feeds.content.dowjones.io/public/rss/mw_topstories`.

## v1.5 — Company Name to Ticker Resolution

- **New `resolve_company_ticker` MCP tool**: Resolves natural language company names ("Mastercard", "Apple") to ticker symbols via SEC reverse index (instant, local cache) with Yahoo Finance search API fallback
- **`_resolve_ticker()` on all agents**: When regex `extract_ticker()` returns empty, agents now call MCP `resolve_company_ticker` before giving up — handles "analyze Mastercard" → "MA" correctly
- **Fixed `re.IGNORECASE` bug in `extract_ticker()`**: Added `.isupper()` guard to prevent lowercase words ("the", "in") from being captured as tickers
- **Pattern 4 changed from `matches[-1]` to `matches[0]`**: The last-matching heuristic picked up trailing stop words ("SEC", "EPS") over the actual ticker. First-match prefers the ticker, which typically appears earlier in LLM-generated task text. Falls back to company name resolution if wrong.
- **`is_valid_ticker_format()` guard**: New shared function rejecting non-standard tickers (digits, periods, >5 chars). Applied in all agents' `_resolve_ticker()` to prevent mutual fund identifiers from reaching validation.
- **`_validate_ticker` fallback to resolution**: When regex-extracted ticker fails SEC validation, agents retry with company name resolution before returning an error.

## v1.4 — Ticker Extraction Decoupled from SEC Validation

- **`extract_ticker()` simplified**: `shared/ticker_utils.py` is now pure regex — no SEC API calls, no `httpx` dependency, instant execution. All SEC validation moved to MCP server.
- **`_validate_ticker()` added to all agents**: Consistent `tuple[bool, str, str]` return type across agent_2 (RAG), agent_3 (Quant), and agent_4 (Sentiment). Connects MCP, calls `validate_ticker` tool, falls back to regex guess on MCP failure.
- **Fixed broken `_connect()` call in agent_2**: `RAGAgent.stream()` was calling `await self._connect()` which didn't exist. Now uses `_validate_ticker()`.
- **Error message for missing ticker**: All agents now show: *"Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."*
- **MCP `validate_ticker` pre-warming**: SEC ticker map pre-loaded on first tool call (`_prewarm_ticker_map()`) so subsequent validations are instant.
- **All agents handle MCP downtime**: If MCP connect or `validate_ticker` call fails, agents log a warning and proceed with the regex-extracted ticker (best-effort).

## v1.3 — MCP Server Fixes & RAG Content Ingestion

- **Docker-compose MCP fix**: Replaced 4 broken MCP services (`mcp-yfinance`, `mcp-sec-edgar`, `mcp-reddit`, `mcp-python-runner`) that referenced non-existent files with single unified `finsight-mcp` service
- **New `get_filing_content` tool**: Added MCP tool to fetch and extract text from raw SEC EDGAR filing URLs with IXBRL fallback
- **RAG content ingestion fix**: RAG agent now fetches actual filing content (10-K, 10-Q, 8-K text) via `get_filing_content()` instead of just storing metadata. Returns raw document URL (`edgar_url`) plus viewer fallback (`ix_url`)
- **Quant DCF fix**: DCF valuation now correctly reads free cash flow from `cash_flow` statement instead of `income_statement` (was returning null)
- **MCP response parsing utility**: Added `parse_mcp_result()` to `shared/mcp_client.py` for consistent handling across all agents

## v1.2 — Model Update & MCP Server Hardening

- **Model change**: All agents migrated from `gpt-oss-20b` to `qwen3-30b-a3b-2507` — ~5-10x faster inference per LLM call
- **`.env` / `.env.example`**: Updated `LLM_MODEL` and `ADK_MODEL` defaults to qwen

- **Windows compatibility**: `import resource` guarded by `sys.platform != "win32"` check
- **Lazy agent registry**: Model download deferred to first tool call (`_ensure_registry`), no blocking at import time
- **Thread-safe SSE app**: `get_app()` with double-checked locking (`_starlette_app` singleton)
- **Inline imports**: `import re as _re` inside `_resolve_company_keywords` and `_normalise_for_match` for localised scope
- **NaN/Inf serialisation**: `_serialise_value` handles NaN, infinity, numpy types, datetimes
- **EDGAR caching**: In-memory CIK/ticker/title map with lock-protected lazy loading
- **Expanded sandbox restricted imports**: `builtins`, `gc`, `threading`, `multiprocessing`, `signal`, `mmap`, `resource`, `pwd`, `grp`, `crypt` added to blocklist
- **SEC earnings fallback**: `get_earnings_calendar` falls back to EDGAR XBRL when yfinance lacks data
- **Retry logic**: EDGAR company filings URL fetch uses 3-attempt exponential backoff
- **42 tests passing**

## v1.1 — A2A Protocol Alignment

- **A2A discovery**: Replaced sync raw HTTP with async `A2ACardResolver` — standard `/.well-known/agent-card.json`, protobuf `AgentCard` types, backwards compatibility
- **A2A client**: Replaced `create_client()` with `ClientFactory` — proper transport negotiation, matches official A2A SDK pattern
- **Single `send_message` tool**: Removed per-agent tool generation. LLM now uses one tool with `agent_name` parameter, matching all A2A sample projects (Google, bhancockio, theailanguage)
- **Removed `list_remote_agents`**: LLM already sees agents in the instruction prompt
- **Pre-fetch removed**: `FinSightAgentExecutor` no longer pre-fetches sub-agent data. LLM routes via `send_message` tool, matching A2A sample executor pattern
- **Streaming event handling**: Correctly skips SUBMITTED/WORKING events, captures `artifact_update` (data + text parts) and terminal `status_update` events
- **Background async discovery**: Supports both ADK Web UI (running event loop → `loop.create_task()`) and CLI (`asyncio.run()`)
- **Windows event loop fix**: `WindowsSelectorEventLoopPolicy` prevents noisy `ConnectionResetError`
- **Programmatic AgentCards**: All servers now build `AgentCard` in code using protobuf types — removed `agent_1_adk/agent_card.json`
- **Agent card descriptions**: Updated from "Ollama" to "LM Studio"
- **44 tests passing** (42 standard + 2 orchestrator tool tests removed with `list_remote_agents`)

## v1.0 — LM Studio Migration

- **Model change**: All agents migrated from Ollama (`qwen2.5:7b`) to LM Studio (`gpt-oss-20b`) — OpenAI-compatible local API
- **Config**: Removed `OLLAMA_BASE_URL`, changed `LLM_BASE_URL` default to `http://localhost:1234/v1`
- **Dependencies**: Replaced `llama-index-llms-ollama` with `llama-index-llms-openai-like`, `langchain-ollama` with `langchain-openai`
- **Agent 3 (Quant)**: Switched from direct `yfinance` calls to MCP tools (`get_prices`, `get_financials`)
- **Agent 2 (RAG)**: Removed static `mcp_config.yaml` — MCP server URL passed inline via `MCPServerConfig`
- **Agent 4 (Sentiment)**: Removed static `mcp_config.yaml` — same inline pattern
- **`.env`**: Cleaned up obsolete Ollama variables

## v0.9 — Model Migration to qwen2.5:7b

- **Model change**: All agents migrated from `llama3.2` to `qwen2.5:7b`
- **`.env.example`**: Updated default models

## v0.8 — Streamlined ADK Agent

- **ADK agent restructured**: Replaced legacy modules with clean `agent.py` + `sub_agent_client.py` + `agent_executor.py` + `main.py`
- **39 tests passing**

## v0.7 — v0.1

- Earlier iterations: model testing, MCP consolidation, initial A2A SDK integration, project scaffolding
