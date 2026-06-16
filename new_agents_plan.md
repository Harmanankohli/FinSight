# Implementation Plan: Analytics Agent + Reviewer Agent

## Context

The FinSight multi-agent investment system currently has 3 sub-agents (RAG/LlamaIndex, Quant/LangGraph, Market Context/CrewAI) coordinated by an ADK orchestrator. We're adding two new agents, each using a **different framework** to maximize architectural diversity (6 agents, 6 frameworks):

1. **Analytics Agent** (port 8005) — **PydanticAI + pydantic-graph** — runs in parallel with existing agents, provides trend detection, forecasting, chart data, statistical analysis, and anomaly detection
2. **Reviewer Agent** (port 8006) — **OpenAI Agents SDK** — runs *after* all Phase 1 agents complete, cross-validates their outputs and produces a meta-review with contradiction checks, confidence calibration, and recommendation validation

Both frameworks connect to the local LM Studio instance at `http://localhost:1234/v1` (OpenAI-compatible API). PydanticAI uses `OpenAIProvider(base_url=...)`, OpenAI Agents SDK uses `OpenAIChatCompletionsModel(base_url=...)`.

This changes the orchestrator flow from single-phase parallel to two-phase: Phase 1 (4 agents in parallel) → Phase 2 (reviewer validates all outputs).

---

## Phase 1: Pydantic Output Models

**File**: `src/shared/agent_models.py`

### Analytics Agent models (add after MarketContextOutput section)

```python
# ── Analytics Agent models ──────────────────────────────────────────────────

class TrendAnalysis(BaseModel):
    trend_direction: str = "neutral"          # "bullish" / "bearish" / "neutral"
    ma_crossover_signal: Optional[str] = None # "golden_cross" / "death_cross"
    momentum_shift: Optional[str] = None      # "accelerating" / "decelerating"
    trend_strength: float = 0.0               # 0.0–1.0
    supporting_indicators: list[str] = Field(default_factory=list)

class ForecastResult(BaseModel):
    method: str = "exponential_smoothing"
    horizon_days: int = 30
    forecast_prices: list[float] = Field(default_factory=list)
    forecast_dates: list[str] = Field(default_factory=list)
    confidence_lower: list[float] = Field(default_factory=list)
    confidence_upper: list[float] = Field(default_factory=list)
    mape: Optional[float] = None

class ChartPayload(BaseModel):
    chart_type: str = "candlestick"  # "candlestick" / "line" / "area"
    labels: list[str] = Field(default_factory=list)
    datasets: list[dict] = Field(default_factory=list)
    annotations: list[dict] = Field(default_factory=list)

class StatisticalSummary(BaseModel):
    return_distribution: Optional[str] = None  # "normal" / "leptokurtic" / "platykurtic"
    skewness: Optional[float] = None
    kurtosis: Optional[float] = None
    jarque_bera_pvalue: Optional[float] = None
    correlations: dict[str, float] = Field(default_factory=dict)
    regression_beta: Optional[float] = None
    regression_r_squared: Optional[float] = None

class AnomalyReport(BaseModel):
    price_anomalies: list[dict] = Field(default_factory=list)
    volume_anomalies: list[dict] = Field(default_factory=list)
    fundamental_anomalies: list[str] = Field(default_factory=list)
    anomaly_count: int = 0
    severity: str = "none"  # "none" / "low" / "medium" / "high"

class AnalyticsAgentOutput(BaseModel):
    ticker: str
    trend_analysis: Optional[TrendAnalysis] = None
    forecast: Optional[ForecastResult] = None
    charts: list[ChartPayload] = Field(default_factory=list)
    statistical_summary: Optional[StatisticalSummary] = None
    anomalies: Optional[AnomalyReport] = None
    analytics_confidence: float = 0.0
    analytics_signal: str = "neutral"
```

### Reviewer Agent models (add after AnalyticsAgentOutput)

```python
# ── Reviewer Agent models ───────────────────────────────────────────────────

class ContradictionFlag(BaseModel):
    agents: list[str]
    field: str
    description: str
    severity: str = "low"  # "low" / "medium" / "high"

class SourceVerification(BaseModel):
    agent_name: str
    claims_checked: int = 0
    claims_verified: int = 0
    verification_rate: float = 0.0
    unverified_claims: list[str] = Field(default_factory=list)

class ConfidenceBreakdown(BaseModel):
    agent_scores: dict[str, float] = Field(default_factory=dict)
    agreement_score: float = 0.0
    data_quality_score: float = 0.0
    meta_confidence: float = 0.0

class RecommendationValidation(BaseModel):
    recommendation: str = "HOLD"
    evidence_supports: bool = True
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    evidence_strength: str = "moderate"  # "weak" / "moderate" / "strong"

class ReviewerAgentOutput(BaseModel):
    ticker: str
    verdict: str = "HOLD"
    review_summary: str = ""
    contradictions: list[ContradictionFlag] = Field(default_factory=list)
    source_verifications: list[SourceVerification] = Field(default_factory=list)
    confidence_breakdown: Optional[ConfidenceBreakdown] = None
    recommendation_validation: Optional[RecommendationValidation] = None
    flags: list[str] = Field(default_factory=list)
    review_confidence: float = 0.0
```

### Update ValidatedAgentOutputs

Add `analytics: Optional[AnalyticsAgentOutput] = None` and `reviewer: Optional[ReviewerAgentOutput] = None` fields. Update `has_all_agents` to include both.

---

## Phase 2: Settings & Configuration

**File**: `src/shared/settings.py`

Add to `Settings` class (after `agent_port_market`):
- `agent_port_analytics: int = 8005`
- `agent_port_reviewer: int = 8006`
- `a2a_timeout_analytics: float = 600.0`
- `a2a_timeout_reviewer: float = 300.0`

Update `agent_seed_urls` default to include ports 8005 and 8006.

Add module-level convenience constants at the bottom:
- `A2A_TIMEOUT_ANALYTICS = _s.a2a_timeout_analytics`
- `A2A_TIMEOUT_REVIEWER = _s.a2a_timeout_reviewer`

---

## Phase 3: Analytics Agent (PydanticAI + pydantic-graph)

**Framework**: PydanticAI v1.x with `pydantic-graph` module
**New directory**: `src/analytics/`

### File structure
```
src/analytics/
├── __init__.py              # empty
├── server.py                # A2A server entry point
├── executor.py              # AnalyticsAgent(BaseAgent)
├── graph.py                 # AnalyticsPipeline — pydantic-graph Graph
├── state.py                 # AnalyticsState dataclass (graph state)
├── deps.py                  # AnalyticsDeps — dependency injection container
├── nodes/
│   ├── __init__.py          # re-exports all node classes
│   ├── data_fetch.py        # FetchPricesNode, FetchFundamentalsNode
│   ├── trend.py             # TrendDetectionNode
│   ├── forecast.py          # ForecastNode
│   ├── charts.py            # ChartGenerationNode
│   ├── statistics.py        # StatisticalAnalysisNode
│   ├── anomaly.py           # AnomalyDetectionNode
│   └── summary.py           # FormatOutputNode, LLMSummaryNode
└── Dockerfile
```

### `deps.py` — Dependency injection container

PydanticAI uses typed dependency injection (like FastAPI). Define a deps dataclass that flows through all nodes:

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class AnalyticsDeps:
    ticker: str
    period: str
    mcp_client: Any
    langfuse_handler: Any | None = None
```

### `state.py` — Graph state dataclass

Unlike LangGraph's TypedDict with Annotated reducers, pydantic-graph uses a mutable dataclass as shared state. All nodes read/write through this object — no concurrent-write reducers needed because pydantic-graph executes nodes sequentially within each topological layer.

```python
from dataclasses import dataclass, field

@dataclass
class AnalyticsState:
    ticker: str = ""
    period: str = "1y"
    # Raw data (populated by fetch nodes)
    price_data: dict = field(default_factory=dict)         # date→close
    ohlcv_data: list[dict] = field(default_factory=list)   # full OHLCV records
    fundamentals_data: dict = field(default_factory=dict)
    # Analysis outputs (populated by analysis nodes)
    trend_analysis: dict | None = None
    forecast_result: dict | None = None
    chart_payloads: list[dict] = field(default_factory=list)
    statistical_summary: dict | None = None
    anomaly_report: dict | None = None
    # Final outputs (populated by format/summary nodes)
    analytics_signal: str = "neutral"
    analytics_confidence: float = 0.0
    reasoning: str = ""
```

### `graph.py` — pydantic-graph pipeline

pydantic-graph defines nodes as classes with typed edges (return type annotations control the DAG). The graph is built declaratively:

```python
from pydantic_graph import Graph, GraphRunContext
from pydantic_graph.nodes import BaseNode, End

# Each node class defines:
#   async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> <next_node_type>
# Return type = the next node to execute. Union types = conditional branching.
# End[dict] = terminal node that returns final result.
```

**Execution topology** (same logical DAG as before, different framework primitives):

```
Phase 1 — Data Fetch (parallel via asyncio.gather inside a single FetchDataNode):
  FetchDataNode → runs fetch_prices + fetch_fundamentals concurrently
                   stores results in state.price_data, state.ohlcv_data, state.fundamentals_data

Phase 2 — Analysis (parallel via asyncio.gather inside a single AnalyzeNode):
  AnalyzeNode → runs trend_detection, forecast, statistics, anomaly_detection, chart_generation
                concurrently, each as an async helper function
                stores results in state.trend_analysis, state.forecast_result, etc.

Phase 3 — Aggregation:
  FormatOutputNode → deterministic signal/confidence aggregation

Phase 4 — LLM Summary:
  LLMSummaryNode → PydanticAI Agent with output_type=str, produces narrative → End[dict]
```

**Key design note**: pydantic-graph executes nodes sequentially along the graph path. To achieve parallelism *within* a node, use `asyncio.gather` inside the node's `run()` method. This is simpler than LangGraph's fan-out/fan-in — no reducer annotations needed. The `FetchDataNode` and `AnalyzeNode` each internally parallelize their sub-tasks.

```python
class FetchDataNode(BaseNode[AnalyticsState, AnalyticsDeps]):
    async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> AnalyzeNode:
        prices, fundamentals = await asyncio.gather(
            _fetch_prices(ctx.deps.mcp_client, ctx.deps.ticker, ctx.deps.period),
            _fetch_fundamentals(ctx.deps.mcp_client, ctx.deps.ticker),
        )
        ctx.state.price_data = prices["close_data"]
        ctx.state.ohlcv_data = prices["ohlcv_data"]
        ctx.state.fundamentals_data = fundamentals
        return AnalyzeNode()

class AnalyzeNode(BaseNode[AnalyticsState, AnalyticsDeps]):
    async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> FormatOutputNode:
        trend, forecast, stats, anomalies, charts = await asyncio.gather(
            _detect_trends(ctx.state.price_data),
            _run_forecast(ctx.state.price_data),
            _compute_statistics(ctx.state.price_data, ctx.deps.mcp_client),
            _detect_anomalies(ctx.state.price_data, ctx.state.ohlcv_data, ctx.state.fundamentals_data),
            _generate_charts(ctx.state.ohlcv_data, ctx.state.price_data),
        )
        ctx.state.trend_analysis = trend
        ctx.state.forecast_result = forecast
        ctx.state.statistical_summary = stats
        ctx.state.anomaly_report = anomalies
        ctx.state.chart_payloads = charts
        return FormatOutputNode()
```

The helper functions (`_detect_trends`, `_run_forecast`, etc.) live in the `nodes/` submodules and contain the same computation logic described below.

### Node computation details (in `nodes/` submodules)

**`nodes/data_fetch.py`** — async helper functions:
- `_fetch_prices()`: Call `mcp.call_tool_by_name("get_prices", {"ticker": ticker, "period": "1y", "interval": "1d"})`. Parse into `close_data` (date→close dict) and `ohlcv_data` (full records). Follow `src/quant/nodes/data_fetch.py` MCP call pattern.
- `_fetch_fundamentals()`: Call `mcp.call_tool_by_name("get_financials", {"ticker": ticker})`.

**`nodes/trend.py`** (`_detect_trends()`):
- Compute SMA-20, SMA-50, SMA-200 from close prices using numpy
- Detect golden cross (SMA-50 crosses above SMA-200) and death cross (opposite)
- Compute MACD (EMA-12 - EMA-26) direction
- Rate-of-change momentum (20d, 60d)
- Determine `trend_direction` based on majority vote of indicators
- Pure computation, no LLM

**`nodes/forecast.py`** (`_run_forecast()`):
- Implement Holt-Winters exponential smoothing using scipy/numpy
- 30-day forward forecast with 80% confidence bands
- Compute MAPE on last 20% holdout for accuracy estimate
- Graceful fallback: if <60 data points, return None with note

**`nodes/charts.py`** (`_generate_charts()`):
- Build candlestick chart payload from `ohlcv_data` (last 90 trading days)
- Build line chart with SMA overlays (20/50/200) from `price_data`
- Build area chart for forecast range from `forecast_result` (if available)
- Output: list of `ChartPayload` dicts (structured data for frontend, NOT image files)

**`nodes/statistics.py`** (`_compute_statistics()`):
- Compute daily log returns from close prices
- Skewness, kurtosis via `scipy.stats.skew`, `scipy.stats.kurtosis`
- Jarque-Bera normality test via `scipy.stats.jarque_bera`
- Classify distribution: kurtosis > 3 → leptokurtic, < 3 → platykurtic, else normal
- Correlation with SPY benchmark (fetch SPY prices via MCP)
- Simple OLS regression: beta and R² against SPY

**`nodes/anomaly.py`** (`_detect_anomalies()`):
- Price anomalies: Z-score on daily returns, flag |z| > 2.5
- Volume anomalies: Z-score on volume, flag |z| > 3.0
- Fundamental anomalies (if `fundamentals_data` available): IQR method on key ratios (PE, debt-to-equity), flag values outside 1.5*IQR of sector norms
- Return `anomaly_count`, `severity` (none/low/medium/high based on count and z-scores)

**`nodes/summary.py`** — `FormatOutputNode` and `LLMSummaryNode`:
- `FormatOutputNode.run()`: Deterministic aggregation — weighted vote across trend/forecast/stats/anomaly to produce `analytics_signal` and `analytics_confidence`. Returns `LLMSummaryNode()`.
- `LLMSummaryNode.run()`: Creates a PydanticAI `Agent` with `output_type=str` and the LM Studio model (`OpenAIProvider(base_url=LLM_BASE_URL)`). Uses `llm_queue.acquire(Priority.CRITICAL, "analytics-summary")` before calling `agent.run_sync()`. Produces a 3-4 sentence narrative summary. Returns `End(result_dict)`.

```python
class LLMSummaryNode(BaseNode[AnalyticsState, AnalyticsDeps]):
    async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> End[dict]:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIModel
        from shared.llm_queue import llm_queue, Priority
        from shared.settings import LLM_BASE_URL, LLM_SUMMARY_MODEL

        model = OpenAIModel(LLM_SUMMARY_MODEL, provider=OpenAIProvider(base_url=LLM_BASE_URL))
        summary_agent = Agent(model=model, output_type=str, system_prompt="...")

        async with llm_queue.acquire(Priority.CRITICAL, "analytics-summary"):
            result = await summary_agent.run(prompt_with_analysis_data)

        ctx.state.reasoning = result.output
        return End(self._build_output_dict(ctx.state))
```

### `executor.py` — AnalyticsAgent(BaseAgent)

Follow `src/quant/executor.py` pattern but adapted for pydantic-graph:
- `__init__`: `super().__init__(agent_name="Analytics Agent", ...)`, build the `Graph` instance
- `stream()`: yields `await self._build_response(query)`
- `_build_response()`: extract trace IDs → open Langfuse span → extract/validate ticker → create `AnalyticsDeps` and `AnalyticsState` → run graph via `graph.run(FetchDataNode(), state=state, deps=deps)` → validate through `AnalyticsAgentOutput.model_validate()` → return response dict
- Use `@logged()` and `@logged_sync()` decorators from `shared/logging_config`

### `server.py` — A2A server

Follow `src/quant/server.py` pattern:
- `bootstrap("analytics")` → `init_instrumentation("analytics")`
- AgentCard: `name="Analytics Agent"`, port=`_settings.agent_port_analytics`
- Skills: `trend_detection`, `forecasting`, `chart_generation`, `statistical_analysis`, `anomaly_detection`
- `build_agent_app(agent_card=agent_card, agent=AnalyticsAgent(), service_name="analytics", accept=frozenset({"service"}))`

### `Dockerfile`

Follow `src/quant/Dockerfile`:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[analytics]"
COPY src/analytics/ ./analytics/
COPY src/shared/ ./shared/
RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser
ENV HOST="0.0.0.0"
EXPOSE 8005
CMD ["uvicorn", "analytics.server:app", "--host", "0.0.0.0", "--port", "8005"]
```

---

## Phase 4: Reviewer Agent (OpenAI Agents SDK)

**Framework**: OpenAI Agents SDK (`openai-agents`) with LiteLLM extension for LM Studio
**New directory**: `src/reviewer/`

### File structure
```
src/reviewer/
├── __init__.py
├── server.py                # A2A server entry point
├── executor.py              # ReviewerAgent(BaseAgent)
├── agent.py                 # OpenAI Agents SDK agent definition + tools
├── tools/
│   ├── __init__.py          # re-exports all tool functions
│   ├── contradiction.py     # check_contradictions tool
│   ├── verification.py      # verify_sources tool
│   ├── confidence.py        # score_confidence tool
│   └── validation.py        # validate_recommendation tool
├── guardrails.py            # input/output guardrails
├── output_model.py          # structured output type (re-uses ReviewerAgentOutput)
└── Dockerfile
```

### Critical design difference

The Reviewer does NOT call MCP tools. The orchestrator passes it a JSON payload containing all Phase 1 agent outputs:
```json
{"ticker": "AAPL", "agent_outputs": {"quant": {...}, "rag": {...}, "market_context": {...}, "analytics": {...}}}
```

The executor parses this JSON from the task text instead of extracting a ticker from natural language.

### Architecture — OpenAI Agents SDK primitives

The SDK provides 4 primitives that map naturally to the reviewer's job:

1. **Agent** — the reviewer agent with system prompt, tools, and structured `output_type`
2. **Tools** — each validation check is a Python function decorated as a tool
3. **Guardrails** — input guardrail validates the JSON payload structure; output guardrail validates the review result
4. **Runner** — `Runner.run()` drives the agent loop: LLM calls tools → collects results → produces structured output

```python
from agents import Agent, Runner, function_tool, InputGuardrail, OutputGuardrail
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
```

### `agent.py` — Agent definition

```python
from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from shared.settings import LLM_BASE_URL, LLM_SUMMARY_MODEL

# Connect to LM Studio via OpenAI-compatible API
_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="lm-studio")
_model = OpenAIChatCompletionsModel(model=LLM_SUMMARY_MODEL, openai_client=_client)

reviewer_agent = Agent(
    name="Reviewer",
    model=_model,
    instructions="""You are an investment analysis reviewer. You have tools to validate
    agent outputs. Call ALL four tools (check_contradictions, verify_sources,
    score_confidence, validate_recommendation) then synthesize into a verdict.""",
    tools=[check_contradictions, verify_sources, score_confidence, validate_recommendation],
    output_type=ReviewerAgentOutput,
    input_guardrails=[payload_structure_guardrail],
    output_guardrails=[confidence_range_guardrail],
)
```

**Key design**: The LLM decides which tools to call and in what order. The system prompt instructs it to call all 4 tools. The SDK handles:
- Automatic schema generation from tool function signatures
- Pydantic validation of the structured `output_type`
- Retry on validation failure

### Tool implementations (in `tools/` submodules)

Each tool is a `@function_tool` decorated async function. The agent_outputs dict is passed via the tool's parameters (the LLM receives it in the system prompt context and passes relevant slices to each tool).

**`tools/contradiction.py`** — `check_contradictions(agent_outputs: dict) -> list[dict]`:
- Quant recommendation vs analytics trend direction (BUY + bearish trend = contradiction)
- Quant recommendation vs RAG sentiment (BUY + negative filing summary = contradiction)
- Market context signal vs quant signal (bearish macro + bullish quant = contradiction)
- Analytics anomaly severity vs quant confidence (high anomalies + high confidence = flag)
- Each contradiction gets a severity: low/medium/high based on how fundamental the disagreement is
- Returns list of `ContradictionFlag` dicts

**`tools/verification.py`** — `verify_sources(agent_outputs: dict) -> list[dict]`:
Per-agent consistency checks:
- Quant: verify DCF `upside_pct` matches `(intrinsic_value - current_price) / current_price * 100`
- Quant: verify `metrics.sharpe_ratio` and `metrics.var_95_daily` are in plausible ranges
- RAG: verify `confidence_score` is in [0,1], sources list is non-empty when summary is non-empty
- Market Context: verify `confidence_score` is in [0,1], `overall_signal` is one of expected values
- Analytics: verify forecast dates are future, chart datasets are non-empty
- Returns list of `SourceVerification` dicts

**`tools/confidence.py`** — `score_confidence(agent_outputs: dict) -> dict`:
- Extract per-agent confidence: quant's `metrics.quant_confidence`, RAG's `confidence_score`, market_context's `confidence_score`, analytics' `analytics_confidence`
- Agreement score: map each agent's signal to direction (bullish/bearish/neutral), compute % agreement
- Data quality score: count non-null fields across all agent outputs as fraction of total expected fields
- Meta-confidence: `0.4 * avg_agent_confidence + 0.3 * agreement_score + 0.3 * data_quality_score`
- Returns `ConfidenceBreakdown` dict

**`tools/validation.py`** — `validate_recommendation(agent_outputs: dict) -> dict`:
- Extract the quant recommendation (BUY/HOLD/SELL)
- Gather supporting evidence: list bullish signals from each agent (e.g., golden cross, positive DCF upside, bullish macro)
- Gather contradicting evidence: list bearish signals
- `evidence_supports`: True if supporting count > contradicting count for the given recommendation direction
- `evidence_strength`: "strong" if ratio > 3:1, "moderate" if > 1.5:1, "weak" otherwise
- Returns `RecommendationValidation` dict

### `guardrails.py` — Input/output validation

```python
from agents import InputGuardrail, OutputGuardrail, GuardrailFunctionOutput

@InputGuardrail
async def payload_structure_guardrail(ctx, agent, input_text: str) -> GuardrailFunctionOutput:
    """Validate the JSON payload has required structure before the agent runs."""
    try:
        payload = json.loads(input_text)
        if "ticker" not in payload or "agent_outputs" not in payload:
            return GuardrailFunctionOutput(output_info={"reason": "missing ticker or agent_outputs"}, tripwire_triggered=True)
    except json.JSONDecodeError:
        return GuardrailFunctionOutput(output_info={"reason": "invalid JSON"}, tripwire_triggered=True)
    return GuardrailFunctionOutput(output_info={"valid": True}, tripwire_triggered=False)

@OutputGuardrail
async def confidence_range_guardrail(ctx, agent, output: ReviewerAgentOutput) -> GuardrailFunctionOutput:
    """Verify confidence scores are in valid range."""
    if not (0.0 <= output.review_confidence <= 1.0):
        return GuardrailFunctionOutput(output_info={"reason": "confidence out of range"}, tripwire_triggered=True)
    return GuardrailFunctionOutput(output_info={"valid": True}, tripwire_triggered=False)
```

### `executor.py` — ReviewerAgent(BaseAgent)

Key difference from other executors — uses `Runner.run()` instead of a graph:

```python
async def _build_response(self, query: str) -> dict:
    trace_id, parent_span_id, query = extract_trace_ids(query)
    # Parse JSON payload from task text
    try:
        payload = json.loads(query)
        ticker = payload.get("ticker", "")
        agent_outputs = payload.get("agent_outputs", {})
    except json.JSONDecodeError:
        ticker = extract_ticker(query)
        agent_outputs = {}

    # Inject agent_outputs into the prompt for tool access
    prompt = json.dumps({"ticker": ticker, "agent_outputs": agent_outputs})

    async with llm_queue.acquire(Priority.CRITICAL, "reviewer-report"):
        result = await Runner.run(reviewer_agent, input=prompt)

    # result.final_output is already a validated ReviewerAgentOutput (Pydantic model)
    output = result.final_output
    return {
        "is_task_complete": True,
        "content": json.dumps(output.model_dump()),
    }
```

**Important**: The `Runner.run()` loop handles the full agent cycle:
1. Sends prompt to LLM
2. LLM calls tools (check_contradictions, verify_sources, etc.)
3. Tool results fed back to LLM
4. LLM produces structured `ReviewerAgentOutput`
5. Output guardrail validates the result
6. If guardrail fails, retries

The `llm_queue.acquire()` wraps the entire Runner.run() call since the LLM may make multiple calls within the loop.

### `server.py`

- AgentCard: `name="Reviewer Agent"`, port=`_settings.agent_port_reviewer`
- Skills: `contradiction_check`, `source_verification`, `confidence_scoring`, `recommendation_validation`, `structured_review`
- No `on_startup` warmup needed (no MCP, lightweight)

### `Dockerfile`

Same pattern. Port 8006. Uses `.[reviewer]` dependency group. Does NOT need MCP_SERVER_URL env var.

---

## Phase 5: Observability Integration

**File**: `src/shared/observability.py` — `init_instrumentation()`

Add a new branch for both agents. Neither uses LangGraph, so no LangChain instrumentor. Both use Starlette for their A2A server:

```python
elif agent_type in ("analytics", "reviewer"):
    from opentelemetry.instrumentation.starlette import StarletteInstrumentor
    StarletteInstrumentor().instrument()
    # Analytics uses PydanticAI (pydantic-graph) — Langfuse handler passed
    # explicitly via deps. Reviewer uses OpenAI Agents SDK — tracing is
    # handled by the SDK's built-in trace processor. Neither needs
    # LangChainInstrumentor.
```

---

## Phase 6: Orchestrator Modifications

### `src/orchestrator/agent.py`

**Update `_STATIC_PREAMBLE`** to describe two-phase execution:

```
PHASE 1 — Parallel Analysis:
2.  Call send_message for EVERY Phase 1 agent in a SINGLE response:
    Financial RAG Agent, Quant Analysis Agent, Market Context Agent, Analytics Agent
    
PHASE 2 — Review:
4.  After ALL Phase 1 agents respond, call send_message for the Reviewer Agent.
    Pass a JSON payload: {"ticker": "AAPL", "agent_outputs": {"quant": <result>, ...}}

PHASE 3 — Synthesis:
5.  Synthesize ALL findings including the reviewer's cross-validation into BUY/HOLD/SELL.
```

**Update `_build_instruction()`** agent responsibility boundaries to include:
- `Analytics Agent owns trend detection, forecasting, chart data, statistical analysis, and anomaly detection`
- `Reviewer Agent (PHASE 2 ONLY) cross-validates all agent outputs, checks for contradictions, and produces calibrated meta-confidence`

### `src/orchestrator/sub_agent_client.py`

Import `A2A_TIMEOUT_ANALYTICS` and `A2A_TIMEOUT_REVIEWER` from settings.

Update `_TIMEOUT_MAP` in `send_message()`:
```python
_TIMEOUT_MAP = {
    "rag": A2A_TIMEOUT_RAG,
    "quant": A2A_TIMEOUT_QUANT,
    "market context": A2A_TIMEOUT_MARKET_CONTEXT,
    "analytics": A2A_TIMEOUT_ANALYTICS,
    "reviewer": A2A_TIMEOUT_REVIEWER,
}
```

### Memory Storage — `src/orchestrator/agent_executor.py` and `src/orchestrator/agui_bridge.py`

Both files contain a `_store_memory` function that maps agent names to storage keys using substring matching:

```python
if "rag" in name_lower:
    extra["rag_response"] = data
elif "quant" in name_lower:
    extra["quant_response"] = data
elif "market" in name_lower or "sentiment" in name_lower:
    extra["sentiment_response"] = data
```

**Add two new branches** in BOTH files (order matters — add before the final elif):

```python
elif "analytics" in name_lower:
    extra["analytics_response"] = data
elif "reviewer" in name_lower:
    extra["reviewer_response"] = data
```

Also update the dedup check in both files. Currently it checks:
```python
has_new_agent_data = extra and not any(
    k in bj for k in ("quant_response", "rag_response", "sentiment_response")
)
```
**Add** `"analytics_response"` and `"reviewer_response"` to that tuple.

### Cache Validity Checks — 4 locations across 2 files

The `has_agent_data` check determines whether a cached brief is "complete enough" to serve from cache or whether to force a re-run. It appears in **4 places** — all must be updated to include the new keys:

1. **`agui_bridge.py:114`** — `_get_today_cached_text()`: determines if a today-cached brief can be returned directly
2. **`agui_bridge.py:151`** — `_build_memory_context()`: determines TODAY vs STALE label for memory context injection
3. **`agent_executor.py:112`** — `_get_today_cached_text()`: same purpose as #1
4. **`agent_executor.py:480`** — `_build_memory_context()`: same purpose as #2

All 4 currently check:
```python
any(k in data for k in ("quant_response", "rag_response", "sentiment_response"))
```
**Add** `"analytics_response"` and `"reviewer_response"` to each tuple. Without this, a brief that has all 5 agents' data would still be considered "missing agent outputs" if the original 3 keys are absent.

### Report Extraction Detection — `src/shared/reports/extraction.py`

The report extraction gate currently checks:
```python
if any(k in brief_data for k in ("quant_response", "rag_response", "sentiment_response")):
```
**Add** `"analytics_response"` and `"reviewer_response"` to this check.

In the `ValidatedAgentOutputs` construction block (~line 1786), add:
```python
analytics=AnalyticsAgentOutput.model_validate(brief_data["analytics_response"])
if brief_data.get("analytics_response") else None,
reviewer=ReviewerAgentOutput.model_validate(brief_data["reviewer_response"])
if brief_data.get("reviewer_response") else None,
```

Import `AnalyticsAgentOutput` and `ReviewerAgentOutput` alongside the existing imports.

---

## Phase 7: Infrastructure

### `pyproject.toml`

Add two new optional dependency groups:

**`analytics`**: pydantic-ai[openai], pydantic-graph, pandas, numpy, scipy, a2a-sdk, starlette, uvicorn, httpx, pydantic-settings, langfuse, opentelemetry-*, aiosqlite

**`reviewer`**: openai-agents[litellm], pandas, numpy, a2a-sdk, starlette, uvicorn, httpx, pydantic-settings, langfuse, opentelemetry-*, aiosqlite (no scipy — reviewer does no statistical computation)

Update `[tool.setuptools.packages.find]` include list: add `"analytics*"`, `"reviewer*"`

Update `[[tool.mypy.overrides]]` module list: add `"analytics.*"`, `"reviewer.*"`

### `docker-compose.yml`

Add two services after `market-context`:

**`analytics`**: port 8005, depends_on mcp, env `MCP_SERVER_URL=http://mcp:8010/sse`

**`reviewer`**: port 8006, NO depends_on mcp (no MCP calls), no MCP_SERVER_URL

Update `orchestrator` service:
- Add `AGENT_SEED_URLS` with ports 8005 and 8006
- Add `depends_on` for analytics and reviewer

### `run_adk_web.bat` AND `run_ui.bat`

Both startup scripts need the same additions. Add after Market Context Agent terminal (before ADK Web UI / Orchestrator terminal):

```batch
:: Terminal 5 - Analytics Agent (:8005)
start "FinSight Analytics" cmd /k "uv run python -m uvicorn analytics.server:app --host 0.0.0.0 --port 8005 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 6 - Reviewer Agent (:8006)
start "FinSight Reviewer" cmd /k "uv run python -m uvicorn reviewer.server:app --host 0.0.0.0 --port 8006 --log-level info"
timeout /t 3 /nobreak >nul
```

Update echo blocks in both files with new agent URLs. Renumber subsequent terminals.

### `stop_servers.bat`

Update port list: add 8005 and 8006.

### `stop_ui.bat`

Update port list: add 8005 and 8006 (currently lists `3000 8001 8002 8003 8004 8010`).

### Agent Card JSON Files — `agent_cards/`

The `agent_cards/` directory contains static JSON files used by the MCP `find_agent` semantic search tool. Add two new files:

**`agent_cards/analytics_agent.json`**: Follow `agent_cards/quant_agent.json` pattern exactly — name, description, url (`http://localhost:8005/`), version, capabilities, skills array, securitySchemes.

**`agent_cards/reviewer_agent.json`**: Same pattern — name, description, url (`http://localhost:8006/`), version, capabilities, skills array, securitySchemes.

### Frontend Operator Console — `src/web/nextjs-app/app/operator/page.tsx`

The operator page has a hardcoded `SERVICES` array for health monitoring. Add two entries:

```typescript
{ name: "Analytics Agent", url: "/api/health?svc=analytics", port: ":8005", fw: "PydanticAI" },
{ name: "Reviewer Agent", url: "/api/health?svc=reviewer", port: ":8006", fw: "OpenAI Agents SDK" },
```

### Health Proxy Route — `src/web/nextjs-app/app/api/health/route.ts`

The TARGETS map is hardcoded. Add:

```typescript
analytics: "http://localhost:8005/health",
reviewer: "http://localhost:8006/health",
```

### Existing Test Updates — `src/tests/unit/test_parallel_dispatch.py`

This test file validates the timeout map and parallel dispatch for 3 agents. Update:
- `test_three_dispatch_coroutines_run_concurrently`: Rename to reflect 4 agents, add `fake_send("Analytics Agent")` to the `asyncio.gather` call
- `test_timeout_map_uses_market_context_key`: Add assertions for `lookup("Analytics Agent") == A2A_TIMEOUT_ANALYTICS` and `lookup("Reviewer Agent") == A2A_TIMEOUT_REVIEWER`
- Import `A2A_TIMEOUT_ANALYTICS` and `A2A_TIMEOUT_REVIEWER` from settings

---

## Phase 8: Reports Integration

**File**: `src/shared/reports/extraction.py`

### `_populate_from_validated_outputs()` (~line 1342)

Add new sections after the existing market_context handling:

**Analytics section**: Extract analytics data into DeckData:
- `trend_analysis.trend_direction` → append to scorecard items
- `trend_analysis.ma_crossover_signal` → append to scorecard items (golden_cross positive, death_cross negative)
- `forecast.forecast_prices[-1]` → add "30d Forecast" to valuation_table
- `anomalies.severity` → if "medium" or "high", append anomaly descriptions to `data.risks`
- `statistical_summary.skewness` / `kurtosis` → add to financials table
- `charts` → store on `data` for frontend rendering (add `charts: list[dict]` field to `DeckData` if not present)

**Reviewer section**: Extract reviewer data into DeckData:
- `contradictions` with severity "high" → append descriptions to `data.risks`
- `confidence_breakdown.meta_confidence` → use as `data.confidence` if present
- `recommendation_validation.evidence_strength` → append to executive summary context
- `flags` → append to `data.risks`
- `review_summary` → append to executive summary or as separate section

### `_populate_from_agent_outputs()` (~line 1123)

Add parallel dict-extraction path for `analytics_response` and `reviewer_response` (same data, unvalidated dict access). Follow the existing pattern where each agent's data is parsed via `_safe_parse()`.

---

## Phase 9: `.env.example`

**File**: `.env.example`

Add after `AGENT_PORT_MARKET=8004`:
```
AGENT_PORT_ANALYTICS=8005
AGENT_PORT_REVIEWER=8006
```

Add after `A2A_TIMEOUT_MARKET_CONTEXT=600.0`:
```
A2A_TIMEOUT_ANALYTICS=600.0
A2A_TIMEOUT_REVIEWER=300.0
```

Update `AGENT_SEED_URLS`:
```
AGENT_SEED_URLS=http://localhost:8002,http://localhost:8003,http://localhost:8004,http://localhost:8005,http://localhost:8006
```

---

## Phase 10: Runtime Eval (Optional — can be deferred)

**File**: `src/shared/runtime_eval.py`

Each existing agent has a scoring function (`score_rag_response`, `score_quant_response`, `score_sentiment_response`). For completeness, add:

- `score_analytics_response()` — verify forecast MAPE is reasonable, trend indicators are consistent with price data
- `score_reviewer_response()` — verify contradiction flags reference real agent fields, meta-confidence is calibrated

These are optional and can be added in a follow-up. The agents will function without them. If skipped, omit the `defer_eval()` call in both executors.

---

## Phase 11: Tests

### Unit tests

**`src/tests/unit/test_analytics_nodes.py`**: Test each analytics node with synthetic price data. Verify trend detection identifies golden cross, forecast produces correct horizon length, statistics computes valid skewness/kurtosis, anomaly detection catches injected outliers.

**`src/tests/unit/test_reviewer_nodes.py`**: Test each reviewer node with mock agent output dicts. Verify contradiction detection, source verification catches inconsistent DCF, confidence scoring returns [0,1] range.

**`src/tests/unit/test_agent_models.py`** (update existing): Add validation tests for new Pydantic models.

### Smoke tests

**`src/tests/integration/test_analytics_smoke.py`**: Verify server starts and `/health` responds.

**`src/tests/integration/test_reviewer_smoke.py`**: Same for reviewer.

---

## Implementation Order

1. Pydantic models (`src/shared/agent_models.py`) — foundation, no dependencies
2. Settings (`src/shared/settings.py`) + `.env.example` — ports and timeouts
3. Observability (`src/shared/observability.py`) — add instrumentation branches
4. Analytics Agent (PydanticAI): `deps.py` → `state.py` → `nodes/` helper functions → `graph.py` (pydantic-graph nodes) → `executor.py` → `server.py` → `Dockerfile`
5. Reviewer Agent (OpenAI Agents SDK): `tools/` (4 tool functions) → `guardrails.py` → `agent.py` (SDK Agent definition) → `executor.py` → `server.py` → `Dockerfile`
6. Orchestrator changes:
   - `src/orchestrator/sub_agent_client.py` — timeout map + imports
   - `src/orchestrator/agent.py` — preamble + `_build_instruction()` boundaries
   - `src/orchestrator/agent_executor.py` — agent name→key mapping (4 locations: `_store_memory` mapping, `_store_memory` dedup check, `_get_today_cached_text` has_agent_data, `_build_memory_context` has_agent_data)
   - `src/orchestrator/agui_bridge.py` — same 4 locations as agent_executor.py
7. `pyproject.toml` — dependency groups, package discovery, mypy overrides
8. `docker-compose.yml` — new services + orchestrator depends_on + AGENT_SEED_URLS
9. Startup/stop scripts — `run_adk_web.bat`, `run_ui.bat`, `stop_servers.bat`, `stop_ui.bat`
10. Agent card JSON files — `agent_cards/analytics_agent.json`, `agent_cards/reviewer_agent.json`
11. Frontend — `src/web/nextjs-app/app/operator/page.tsx` (SERVICES array) + `src/web/nextjs-app/app/api/health/route.ts` (TARGETS map)
12. Reports extraction (`src/shared/reports/extraction.py`) — detection gate, validated outputs construction, DeckData population
13. Tests — unit nodes, model validation, smoke tests, update `test_parallel_dispatch.py`
14. (Optional) Runtime eval (`src/shared/runtime_eval.py`) — scoring functions for new agents

---

## Verification

1. `uv run pytest src/tests/unit/test_analytics_nodes.py src/tests/unit/test_reviewer_nodes.py -v` — unit tests pass
2. Start all services via `run_adk_web.bat`
3. `curl http://localhost:8005/health` and `curl http://localhost:8006/health` — both return `{"status": "ok"}`
4. Run a full analysis query (e.g., "Analyze AAPL") through ADK Web UI
5. Confirm in Langfuse traces: orchestrator calls 4 agents in Phase 1, then reviewer in Phase 2
6. Verify reviewer output contains contradiction checks, source verifications, and meta-confidence
7. Verify the saved brief in SQLite contains `analytics_response` and `reviewer_response` keys in `brief_json`
8. Generate a report (`/api/reports/generate/{brief_id}/html`) and verify analytics + reviewer data appears

---

## Key Risks

### 1. Two-Phase Orchestrator Execution
The ADK `LlmAgent` relies on the LLM to follow the preamble instructions for Phase 1/Phase 2 sequencing. If the LLM calls all 5 agents in parallel (including reviewer), the reviewer receives empty outputs. The preamble must be extremely clear. If this proves unreliable with the local LLM, a programmatic fallback can be added: intercept "Reviewer Agent" in `send_message`, check if Phase 1 responses are captured in `_agent_responses`, and inject them into the task text automatically.

### 2. Reviewer JSON Payload Size
Passing all 4 agent outputs as JSON in the task text could be large. The A2A protocol supports arbitrary-length messages, but consider truncating verbose fields (correlation_matrix, chart datasets, context_texts) before passing to the reviewer. The reviewer executor should handle partial/missing fields gracefully.

### 3. PydanticAI pydantic-graph Maturity
The `pydantic-graph` module is still in beta (`pydantic_graph.beta`). The API may change between minor versions. Pin `pydantic-ai` to a specific version in `pyproject.toml` (e.g., `pydantic-ai[openai]>=1.70,<2.0`). If pydantic-graph proves unstable, the fallback is to replace the graph nodes with plain `asyncio.gather` orchestration in `executor.py` — the computation helpers in `nodes/` stay the same.

### 4. OpenAI Agents SDK Tool-Calling with Local LLMs
The OpenAI Agents SDK relies on the LLM to correctly generate tool calls and produce structured output. Local LLMs via LM Studio may not follow tool-calling schemas as reliably as OpenAI's models. Mitigations:
- Use a model with strong tool-calling support (e.g., Qwen 2.5, Llama 3.1+)
- Keep tool parameter schemas simple (single `agent_outputs: dict` parameter)
- The SDK retries on Pydantic validation failure of `output_type`, providing self-correction
- Fallback: if tool calling is unreliable, refactor tools into deterministic functions called directly in the executor, using the LLM only for the final narrative summary

### 5. LLM Contention
Two new agents each making LLM calls increases contention on the shared LM Studio instance. The analytics agent calls the LLM once (summary node). The reviewer agent's `Runner.run()` may make multiple LLM calls (tool selection + structured output generation). Both must use `llm_queue.acquire(Priority.CRITICAL, ...)` to ensure summaries complete before eval scoring. Consider bumping `LLM_MAX_CONCURRENT` from 2 to 3 if throughput is an issue.
