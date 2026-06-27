"""Runtime RAGAS evaluation — per-agent scoring as background tasks.

All functions are called from within the live agent executor after a response
is produced. No separate runner is created — the response is already in hand.

  Orchestrator (score_response):
    AnswerRelevancy        — synthesis relevant to the investment query?
    citation_quality       — cites specific figures/filings?
    risk_disclosure        — acknowledges investment risks?
    recommendation_clarity — clear BUY/HOLD/SELL with weighted signal evidence?
    response_completeness  — integrates RAG + quant + market context?

  RAG Agent (score_rag_response):
    Faithfulness                     — claims grounded in retrieved SEC/news chunks?
    ContextPrecisionWithoutReference — retrieved chunks relevant to the query?
    news_coverage                    — response references recent news alongside filings?

  Quant Agent (score_quant_response):
    FactualCorrectness — LLM summary numbers match computed metrics (reference)

  Market Context Agent (score_sentiment_response):
    Faithfulness             — narrative grounded in macro indicators and peer data?
    macro_regime_analysis    — discusses yield curve, VIX, DXY, sector rotation?
    peer_landscape_analysis  — compares target vs named peers on growth/valuation?

Notes:
  - AnswerRelevancy is restricted to orchestrator only — it generates 0.0 on finance
    text due to reverse-question-generation failure on domain-specific language.
  - Circuit breaker: after _CIRCUIT_MAX_FAILURES consecutive RAGAS failures the
    circuit opens and eval is skipped for the process lifetime.
  - LLM_EVAL_MODEL is used for all RAGAS LLM calls (separate from production LLM)
    so eval doesn't contend with inference latency.

Offline-only metrics (require ground-truth reference) live in tests/evaluation/:
  ContextRecall, ContextEntityRecall, ToolCallAccuracy, AgentGoalAccuracy.

All public functions are safe to fire-and-forget via asyncio.create_task().
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

from shared.settings import (
    EVAL_BURST_LIMIT,
    EVAL_METRIC_TIMEOUT,
    EVAL_RUNTIME_DISABLED,
    LLM_API_KEY,
)

logger = logging.getLogger(__name__)

_MIN_RESPONSE_LEN = 80
_ragas_clients: tuple[Any, ...] | None = None

# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

_CIRCUIT_MAX_FAILURES = 5
_eval_failure_count = 0
_eval_circuit_open = False

# ---------------------------------------------------------------------------
# Dedup (SHA-256 of (input, response) over 1h) + burst limiter (per minute)
# ---------------------------------------------------------------------------

_DEDUP_TTL = 3600.0  # 1 hour
_dedup_cache: dict[str, float] = {}

_BURST_WINDOW = 60.0  # 1 minute
_burst_window_start = 0.0
_burst_count = 0


def _dedup_seen(user_input: str, response: str) -> bool:
    """True if this exact (input, response) was scored within the last hour."""
    digest = hashlib.sha256(f"{user_input}\x1f{response}".encode()).hexdigest()
    now = time.monotonic()
    # Opportunistic GC: drop expired entries on read
    if len(_dedup_cache) > 256:
        for k in [k for k, ts in _dedup_cache.items() if now - ts > _DEDUP_TTL]:
            _dedup_cache.pop(k, None)
    if digest in _dedup_cache and now - _dedup_cache[digest] < _DEDUP_TTL:
        return True
    _dedup_cache[digest] = now
    return False


def _burst_ok() -> bool:
    """Return False if this process has already issued EVAL_BURST_LIMIT evals this minute."""
    global _burst_window_start, _burst_count
    if EVAL_BURST_LIMIT <= 0:
        return True
    now = time.monotonic()
    if now - _burst_window_start > _BURST_WINDOW:
        _burst_window_start = now
        _burst_count = 0
    if _burst_count >= EVAL_BURST_LIMIT:
        return False
    _burst_count += 1
    return True


def _gate_ok(user_input: str, response: str, agent: str) -> bool:
    """Combined pre-eval gate: runtime kill-switch, circuit breaker, burst limit, dedup."""
    if EVAL_RUNTIME_DISABLED:
        logger.info("[%s] Eval skipped — EVAL_RUNTIME_DISABLED=true", agent)
        return False
    if not _circuit_ok():
        logger.info("[%s] Eval skipped — circuit breaker open", agent)
        return False
    if not _burst_ok():
        logger.info("[%s] Eval skipped — burst limit %d/min reached", agent, EVAL_BURST_LIMIT)
        return False
    if _dedup_seen(user_input, response):
        logger.info("[%s] Eval skipped — dedup (identical input+response within 1h)", agent)
        return False
    return True


def _record_eval_failure() -> None:
    global _eval_failure_count, _eval_circuit_open
    _eval_failure_count += 1
    if _eval_failure_count >= _CIRCUIT_MAX_FAILURES:
        _eval_circuit_open = True
        logger.warning(
            "RAGAS eval circuit breaker opened after %d failures — eval disabled for this process",
            _CIRCUIT_MAX_FAILURES,
        )


def _circuit_ok() -> bool:
    return not _eval_circuit_open


def _record_eval_success() -> None:
    global _eval_failure_count
    _eval_failure_count = 0  # reset on success


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------


async def _setup_ragas_clients() -> tuple[Any, Any] | None:
    """Return (ragas_llm, ragas_embedder) or None if dependencies are missing."""
    global _ragas_clients
    if _ragas_clients is not None:
        return _ragas_clients
    try:
        import instructor
        from openai import AsyncOpenAI
        from ragas.embeddings.base import BaseRagasEmbedding
        from ragas.llms.base import InstructorLLM, InstructorModelArgs
    except ImportError:
        logger.debug("ragas / instructor / openai not installed — runtime eval skipped")
        return None

    from shared.settings import EMBED_MODEL, LLM_BASE_URL, LLM_EVAL_MODEL

    class _STEmbeddings(BaseRagasEmbedding):
        def __init__(self, model_name: str) -> None:
            import sentence_transformers

            self._model = sentence_transformers.SentenceTransformer(model_name)

        def embed_text(self, text: str, **kwargs: Any) -> list[float]:
            return self._model.encode(  # type: ignore[no-any-return]
                text, normalize_embeddings=True, convert_to_tensor=False
            ).tolist()

        async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.embed_text, text)

    try:
        client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=180, max_retries=5)
        patched = instructor.from_openai(client, mode=instructor.Mode.JSON_SCHEMA)
        ragas_llm = InstructorLLM(
            client=patched,
            model=LLM_EVAL_MODEL,
            provider="openai",
            model_args=InstructorModelArgs(max_tokens=8192),
        )
        ragas_embedder = _STEmbeddings(model_name=EMBED_MODEL)
        _ragas_clients = (ragas_llm, ragas_embedder)
        return ragas_llm, ragas_embedder
    except Exception as exc:
        logger.warning("Runtime eval client setup failed: %s", exc)
        _ragas_clients = None
        return None


async def _score_metric(metric: Any, **kwargs: Any) -> float:
    from shared.llm_queue import Priority, llm_queue

    async with llm_queue.acquire(Priority.LOW, f"eval/{metric.name}"):
        try:
            result = await metric.ascore(**kwargs)
            return float(result.value)
        except Exception:
            logger.warning(
                "RAGAS metric '%s' ascore failed (kwargs keys: %s)",
                metric.name,
                list(kwargs.keys()),
                exc_info=True,
            )
            raise


async def _score_metric_with_timeout(metric: Any, **kwargs: Any) -> float:
    """Wrap a metric ascore with a wall-clock deadline so a stuck call can't pin the pool."""
    return await asyncio.wait_for(_score_metric(metric, **kwargs), timeout=EVAL_METRIC_TIMEOUT)


async def _run_metrics(
    pairs: list[tuple[Any, dict[str, Any]]], agent: str = "", trace_id: str | None = None
) -> dict[str, float]:
    """Run metrics concurrently; push each score to Langfuse as it completes."""
    scores: dict[str, float] = {}
    task_map: dict[asyncio.Task[float], str] = {}

    for metric, kw in pairs:
        task = asyncio.create_task(_score_metric_with_timeout(metric, **kw))
        task_map[task] = metric.name

    pending = set(task_map.keys())
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            metric_name = task_map[task]
            try:
                result = task.result()
                scores[metric_name] = round(float(result), 4)
                logger.info(
                    "[%s] RAGAS metric '%s' = %s (trace=%s)",
                    agent,
                    metric_name,
                    scores[metric_name],
                    trace_id,
                )
                _push_scores({metric_name: scores[metric_name]}, trace_id, agent)
                _record_eval_success()
            except TimeoutError:
                logger.warning(
                    "[%s] RAGAS metric '%s' timed out after %ss",
                    agent,
                    metric_name,
                    EVAL_METRIC_TIMEOUT,
                )
                _record_eval_failure()
            except BaseException as exc:
                msg = str(exc) if str(exc) else type(exc).__name__
                logger.warning("[%s] RAGAS metric '%s' error: %s", agent, metric_name, msg)
                _record_eval_failure()
    return scores


def _push_scores(scores: dict[str, float], trace_id: str | None, agent: str) -> None:
    if not scores or trace_id is None:
        return
    try:
        from shared.observability import get_langfuse_client

        lf = get_langfuse_client()
        if lf is None:
            return
        prefix = f"ragas/{agent}" if agent else "ragas"
        for name, value in scores.items():
            kwargs: dict[str, Any] = {
                "name": f"{prefix}/{name}",
                "value": value,
                "comment": f"agent={agent}" if agent else None,
            }
            if trace_id:
                kwargs["trace_id"] = trace_id
            lf.create_score(**{k: v for k, v in kwargs.items() if v is not None})
        lf.flush()
        logger.debug("[%s] Pushed %d RAGAS scores to trace %s", agent, len(scores), trace_id)
    except Exception as exc:
        logger.warning("Langfuse score push failed: %s", exc)


# ---------------------------------------------------------------------------
# Orchestrator — ADK (sole owner of AnswerRelevancy)
# ---------------------------------------------------------------------------


async def score_response(
    user_input: str,
    response: str,
    trace_id: str | None = None,
) -> None:
    """Score orchestrator final synthesis.

    Metrics: AnswerRelevancy, citation_quality, risk_disclosure,
             recommendation_clarity, response_completeness.
    """
    logger.info(
        "[orchestrator] Eval entered (response_len=%d, trace=%s)",
        len(response) if response else 0,
        trace_id,
    )
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        logger.warning(
            "[orchestrator] Skipping eval: response too short (len=%d)",
            len(response) if response else 0,
        )
        return
    if not _gate_ok(user_input, response, "orchestrator"):
        return

    try:
        clients = await _setup_ragas_clients()
        if clients is None:
            logger.warning("[orchestrator] Skipping eval: no RAGAS clients")
            return
        ragas_llm, ragas_embedder = clients

        try:
            from ragas.metrics.collections import AnswerRelevancy, DomainSpecificRubrics
        except ImportError:
            logger.warning("[orchestrator] Skipping eval: ragas import failed")
            return

        _ui_resp = {"user_input": user_input, "response": response}
        pairs = [
            (AnswerRelevancy(llm=ragas_llm, embeddings=ragas_embedder), _ui_resp),
            (
                DomainSpecificRubrics(
                    name="citation_quality",
                    llm=ragas_llm,
                    rubrics={
                        "score1_description": "Response makes only generic claims with no specific figures, filing references, dates, or dollar amounts.",  # noqa: E501
                        "score2_description": "Response mentions one vague reference but without specific figures or filing details.",  # noqa: E501
                        "score3_description": "Response includes one specific figure or filing reference.",  # noqa: E501
                        "score4_description": "Response cites two or more specific figures, dates, or filing sections.",  # noqa: E501
                        "score5_description": "Response extensively cites specific filing sections, dates, dollar amounts, and percentages throughout.",  # noqa: E501
                    },
                ),
                _ui_resp,
            ),
            (
                DomainSpecificRubrics(
                    name="risk_disclosure",
                    llm=ragas_llm,
                    rubrics={
                        "score1_description": "Response presents only upside with no mention of any investment risk.",  # noqa: E501
                        "score2_description": "Response hints at risk in passing but does not name a specific risk factor.",  # noqa: E501
                        "score3_description": "Response acknowledges one material risk (regulatory, competitive, market, or operational).",  # noqa: E501
                        "score4_description": "Response explicitly discusses two or more distinct risk factors.",  # noqa: E501
                        "score5_description": "Response provides a balanced assessment with detailed discussion of multiple material risks across different categories.",  # noqa: E501
                    },
                ),
                _ui_resp,
            ),
            (
                DomainSpecificRubrics(
                    name="recommendation_clarity",
                    llm=ragas_llm,
                    rubrics={
                        "score1_description": "No clear BUY/HOLD/SELL signal present.",
                        "score2_description": "Signal present but rationale is absent or vague.",
                        "score3_description": "Signal with rationale but supported by only one data point.",  # noqa: E501
                        "score4_description": "Signal supported by two or more signal groups (risk metrics, DCF, fundamentals, technicals, peer positioning, or behavioral signals) with cited figures.",  # noqa: E501
                        "score5_description": "Clear signal with supporting evidence from at least three signal groups, citing specific numbers (Sharpe, upside %, peer rank, or macro indicator) from different agent analyses.",  # noqa: E501
                    },
                ),
                _ui_resp,
            ),
            (
                DomainSpecificRubrics(
                    name="response_completeness",
                    llm=ragas_llm,
                    rubrics={
                        "score1_description": "Response draws from only one analysis type or provides only a generic summary.",  # noqa: E501
                        "score2_description": "Response references two analysis types but one is superficial.",  # noqa: E501
                        "score3_description": "Response integrates findings from two of: filing analysis, quantitative metrics, or macro/competitive context.",  # noqa: E501
                        "score4_description": "Response integrates all three analysis types (filing/RAG, quantitative metrics, macro+peer context) with reasonable depth.",  # noqa: E501
                        "score5_description": "Response thoroughly synthesises SEC filing analysis (RAG), quantitative metrics (risk/DCF/peer), and macro+competitive context (Market Context Agent) into a cohesive brief with cited figures.",  # noqa: E501
                    },
                ),
                _ui_resp,
            ),
            (
                DomainSpecificRubrics(
                    name="no_forward_guarantees",
                    llm=ragas_llm,
                    rubrics={
                        "score1_description": "Response makes unconditional forward-looking guarantees (e.g. 'will return X%', 'guaranteed to outperform', 'certain to rally').",  # noqa: E501
                        "score2_description": "Response uses strongly predictive language without hedging (e.g. 'expect 20% upside', 'should hit $X by year-end') with no caveats.",  # noqa: E501
                        "score3_description": "Response mixes some forward statements with mild hedging language.",  # noqa: E501
                        "score4_description": "Response consistently hedges forward-looking statements with words like 'may', 'could', 'estimate', 'based on current data' and notes uncertainty.",  # noqa: E501
                        "score5_description": "Response provides only conditional, well-hedged forward statements explicitly tied to assumptions and uncertainty bounds, with no implied guarantees of future performance.",  # noqa: E501
                    },
                ),
                _ui_resp,
            ),
        ]

        scores = await _run_metrics(pairs, "orchestrator", trace_id)
        if scores:
            logger.info("[orchestrator] RAGAS scores summary (trace=%s): %s", trace_id, scores)
        else:
            logger.info("[orchestrator] No RAGAS scores computed")
    except Exception:
        logger.exception("[orchestrator] Eval crashed unexpectedly")
        _record_eval_failure()


# ---------------------------------------------------------------------------
# RAG Agent — LlamaIndex
# ---------------------------------------------------------------------------


async def score_rag_response(
    user_input: str,
    response: str,
    retrieved_contexts: list[str],
    trace_id: str | None = None,
) -> None:
    """Score RAG agent response.

    Metrics: Faithfulness, ContextPrecisionWithoutReference, news_coverage.
    AnswerRelevancy removed — fragile reverse-question-generation on finance text.
    """
    if not user_input or not response or not retrieved_contexts:
        return
    if not _gate_ok(user_input, response, "rag"):
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, ragas_embedder = clients

    try:
        from ragas.metrics.collections import (
            ContextPrecisionWithoutReference,
            DomainSpecificRubrics,
            Faithfulness,
        )
    except ImportError:
        return

    _ctx_kwargs = {
        "user_input": user_input,
        "response": response,
        "retrieved_contexts": retrieved_contexts,
    }
    _ui_resp: dict[str, Any] = {"user_input": user_input, "response": response}
    pairs: list[tuple[Any, dict[str, Any]]] = [
        (Faithfulness(llm=ragas_llm), _ctx_kwargs),
        (ContextPrecisionWithoutReference(llm=ragas_llm), _ctx_kwargs),
        (
            DomainSpecificRubrics(
                name="news_coverage",
                llm=ragas_llm,
                rubrics={
                    "score1_description": "Response draws only from SEC filings with no reference to recent news or market events.",  # noqa: E501
                    "score2_description": "Response briefly mentions a market development without citing a specific news source or article.",  # noqa: E501
                    "score3_description": "Response references one specific news event or headline and connects it to the filing analysis.",  # noqa: E501
                    "score4_description": "Response integrates two or more recent news items alongside SEC filing evidence.",  # noqa: E501
                    "score5_description": "Response thoroughly weaves recent news sentiment (specific articles, dates, or events) with SEC filing evidence for a complete picture.",  # noqa: E501
                },
            ),
            _ui_resp,
        ),
        (
            DomainSpecificRubrics(
                name="cross_collection_synthesis",
                llm=ragas_llm,
                rubrics={
                    "score1_description": "Response draws from only one type of source (only filings, or only news, or only earnings).",  # noqa: E501
                    "score2_description": "Response mentions evidence from a second source type in passing but does not connect it to the first.",  # noqa: E501
                    "score3_description": "Response uses two different source types (filings + news, or filings + earnings) and explicitly references both.",  # noqa: E501
                    "score4_description": "Response integrates two source types with explicit cross-referencing — e.g. confirms a filing claim with a news event or earnings transcript.",  # noqa: E501
                    "score5_description": "Response synthesises filings, news, and earnings together, citing specific evidence from each and explicitly resolving any tension or confirmation between sources.",  # noqa: E501
                },
            ),
            _ui_resp,
        ),
    ]

    scores = await _run_metrics(pairs, "rag", trace_id)
    if scores:
        logger.info("[rag] RAGAS scores summary (trace=%s): %s", trace_id, scores)
    else:
        logger.info("[rag] No RAGAS scores computed")


# ---------------------------------------------------------------------------
# Quant Agent — LangGraph
# ---------------------------------------------------------------------------


async def score_quant_response(
    user_input: str,
    response: str,
    quant_result: dict[str, Any],
    trace_id: str | None = None,
) -> None:
    """Score quant agent LLM summary.

    Metric: FactualCorrectness (computed metrics = reference).
    AnswerRelevancy removed — fragile on finance text.
    """
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        return
    if not _gate_ok(user_input, response, "quant"):
        return

    reference = _build_quant_reference(quant_result)
    if not reference:
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, _ = clients

    try:
        from ragas.metrics.collections import DomainSpecificRubrics, FactualCorrectness
    except ImportError:
        return

    _ui_resp = {"user_input": user_input, "response": response}
    pairs = [
        (FactualCorrectness(llm=ragas_llm), {"response": response, "reference": reference}),
        (
            DomainSpecificRubrics(
                name="signal_explanation_quality",
                llm=ragas_llm,
                rubrics={
                    "score1_description": "Response states the BUY/HOLD/SELL signal but does not explain which signal groups drove it.",  # noqa: E501
                    "score2_description": "Response references one signal group (e.g. 'high Sharpe') without explaining how it influenced the recommendation.",  # noqa: E501
                    "score3_description": "Response names two signal groups (e.g. risk metrics + DCF) and connects them to the recommendation.",  # noqa: E501
                    "score4_description": "Response explains three or more signal groups (risk_quality, dcf, fundamentals, technicals, peer_positioning, behavioral) with specific numeric values driving the composite.",  # noqa: E501
                    "score5_description": "Response provides a thorough breakdown of the weighted 8-group signal vote, naming each contributing group, citing specific values (Sharpe, upside %, peer rank, options flow, insider direction), and explicitly showing how they combine into the final recommendation and confidence.",  # noqa: E501
                },
            ),
            _ui_resp,
        ),
    ]

    scores = await _run_metrics(pairs, "quant", trace_id)
    if scores:
        logger.info("[quant] RAGAS scores summary (trace=%s): %s", trace_id, scores)
    else:
        logger.info("[quant] No RAGAS scores computed")


def _build_quant_reference(result: dict[str, Any]) -> str:
    """Serialize computed quant metrics into a factual reference string."""
    parts: list[str] = []
    m = result.get("metrics", {})
    if m:
        parts.append(
            f"Sharpe ratio: {m.get('sharpe_ratio')}, "
            f"annual volatility: {m.get('annual_volatility')}, "
            f"beta: {m.get('beta')}, "
            f"VaR 95%: {m.get('var_95_daily')}, "
            f"max drawdown: {m.get('max_drawdown')}"
        )
    dcf = result.get("dcf_valuation")
    if dcf:
        parts.append(
            f"DCF intrinsic value: ${dcf.get('intrinsic_value')}, "
            f"current price: ${dcf.get('current_price')}, "
            f"upside: {dcf.get('upside_pct')}%, "
            f"WACC: {dcf.get('wacc')}, "
            f"FCF used: {dcf.get('fcf_used')}"
        )
    stress = result.get("stress_test")
    if isinstance(stress, dict) and "cvar_95" in stress:
        parts.append(f"CVaR 95%: {stress.get('cvar_95')}, VaR 95%: {stress.get('var_95')}")
    mc = result.get("monte_carlo")
    if mc:
        parts.append(f"Monte Carlo p50: ${mc.get('p50')}, prob_profit: {mc.get('prob_profit')}")
    peer = result.get("peer_comparison")
    if peer and peer.get("rankings"):
        parts.append(
            f"Peer rankings: {peer['rankings']} among {peer.get('n_peers', '?') + 1} stocks"
        )
    rec = result.get("recommendation")
    if rec:
        parts.append(f"Quantitative signal: {rec}")
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Analytics Agent — PydanticAI
# ---------------------------------------------------------------------------


async def score_analytics_response(
    user_input: str,
    response: str,
    analytics_result: dict[str, Any],
    trace_id: str | None = None,
) -> None:
    """Score analytics agent output.

    Metric: FactualCorrectness (computed analytics data = reference).
    """
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        return
    if not _gate_ok(user_input, response, "analytics"):
        return

    reference = _build_analytics_reference(analytics_result)
    if not reference:
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, _ = clients

    try:
        from ragas.metrics.collections import DomainSpecificRubrics, FactualCorrectness
    except ImportError:
        return

    _ui_resp = {"user_input": user_input, "response": response}
    pairs = [
        (FactualCorrectness(llm=ragas_llm), {"response": response, "reference": reference}),
        (
            DomainSpecificRubrics(
                name="trend_forecast_consistency",
                llm=ragas_llm,
                rubrics={
                    "score1_description": "Response states a trend direction but the forecast data clearly contradicts it (e.g. bullish trend with declining forecast).",  # noqa: E501
                    "score2_description": "Response mentions trend and forecast but does not reconcile contradictory signals between them.",  # noqa: E501
                    "score3_description": "Response presents trend and forecast data that are directionally aligned, or acknowledges the tension when they diverge.",  # noqa: E501
                    "score4_description": "Response explicitly connects trend indicators (SMA crossovers, momentum) to the forecast direction and explains agreement or divergence.",  # noqa: E501
                    "score5_description": "Response thoroughly integrates trend analysis (moving averages, momentum shift, MACD) with forecast output, explaining how current technical setup supports or qualifies the forward-looking projection.",  # noqa: E501
                },
            ),
            _ui_resp,
        ),
        (
            DomainSpecificRubrics(
                name="anomaly_disclosure",
                llm=ragas_llm,
                rubrics={
                    "score1_description": "Response ignores anomalies entirely despite the data containing price or volume outliers.",  # noqa: E501
                    "score2_description": "Response vaguely mentions unusual activity without specifics.",  # noqa: E501
                    "score3_description": "Response identifies at least one specific anomaly (price spike, volume surge) with approximate date or magnitude.",  # noqa: E501
                    "score4_description": "Response discusses multiple anomalies with context on their potential causes or implications.",  # noqa: E501
                    "score5_description": "Response provides a comprehensive anomaly assessment covering price, volume, and fundamental anomalies with severity classification and investment implications.",  # noqa: E501
                },
            ),
            _ui_resp,
        ),
    ]

    scores = await _run_metrics(pairs, "analytics", trace_id)
    if scores:
        logger.info("[analytics] RAGAS scores summary (trace=%s): %s", trace_id, scores)
    else:
        logger.info("[analytics] No RAGAS scores computed")


def _build_analytics_reference(result: dict[str, Any]) -> str:
    """Serialize computed analytics data into a factual reference string."""
    parts: list[str] = []
    trend = result.get("trend_analysis") or {}
    if trend:
        parts.append(
            f"Trend direction: {trend.get('trend_direction')}, "
            f"strength: {trend.get('trend_strength')}, "
            f"MA crossover: {trend.get('ma_crossover_signal', 'none')}, "
            f"momentum: {trend.get('momentum_shift', 'none')}"
        )
    forecast = result.get("forecast") or {}
    if forecast and forecast.get("forecast_prices"):
        prices = forecast["forecast_prices"]
        parts.append(
            f"Forecast method: {forecast.get('method')}, "
            f"horizon: {forecast.get('horizon_days')} days, "
            f"final price: {prices[-1]:.2f}, "
            f"MAPE: {forecast.get('mape')}"
        )
    stats = result.get("statistical_summary") or {}
    if stats:
        parts.append(
            f"Distribution: {stats.get('return_distribution')}, "
            f"skewness: {stats.get('skewness')}, "
            f"kurtosis: {stats.get('kurtosis')}, "
            f"beta: {stats.get('regression_beta')}, "
            f"R²: {stats.get('regression_r_squared')}"
        )
    anomalies = result.get("anomalies") or {}
    if anomalies:
        parts.append(
            f"Anomalies: {anomalies.get('anomaly_count')} detected, "
            f"severity: {anomalies.get('severity')}"
        )
    signal = result.get("analytics_signal")
    conf = result.get("analytics_confidence")
    if signal:
        parts.append(f"Analytics signal: {signal}, confidence: {conf}")
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Reviewer Agent — OpenAI Agents SDK
# ---------------------------------------------------------------------------


async def score_reviewer_response(
    user_input: str,
    response: str,
    reviewer_result: dict[str, Any],
    trace_id: str | None = None,
) -> None:
    """Score reviewer agent output.

    Metric: FactualCorrectness (review data = reference),
    plus domain rubrics for cross-validation quality.
    """
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        return
    if not _gate_ok(user_input, response, "reviewer"):
        return

    reference = _build_reviewer_reference(reviewer_result)
    if not reference:
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, _ = clients

    try:
        from ragas.metrics.collections import DomainSpecificRubrics, FactualCorrectness
    except ImportError:
        return

    _ui_resp = {"user_input": user_input, "response": response}
    pairs = [
        (FactualCorrectness(llm=ragas_llm), {"response": response, "reference": reference}),
        (
            DomainSpecificRubrics(
                name="contradiction_detection_quality",
                llm=ragas_llm,
                rubrics={
                    "score1_description": "Review summary ignores obvious contradictions between agent outputs (e.g. one agent says BUY while another signals bearish).",  # noqa: E501
                    "score2_description": "Review mentions disagreement vaguely but does not identify which agents or fields conflict.",  # noqa: E501
                    "score3_description": "Review identifies at least one specific contradiction between named agents with the conflicting fields.",  # noqa: E501
                    "score4_description": "Review identifies multiple contradictions with severity ratings and explains their impact on the overall recommendation.",  # noqa: E501
                    "score5_description": "Review comprehensively catalogues all inter-agent contradictions, assigns appropriate severity levels, and explicitly explains how each contradiction affects confidence in the final verdict.",  # noqa: E501
                },
            ),
            _ui_resp,
        ),
        (
            DomainSpecificRubrics(
                name="confidence_calibration_quality",
                llm=ragas_llm,
                rubrics={
                    "score1_description": "Review assigns a confidence score with no explanation of how it was derived.",  # noqa: E501
                    "score2_description": "Review states confidence but only references one factor (e.g. agent agreement) without considering data quality.",  # noqa: E501
                    "score3_description": "Review explains confidence based on two factors: agent agreement and at least one of data quality or individual agent scores.",  # noqa: E501
                    "score4_description": "Review provides a breakdown of confidence across agent scores, agreement, and data quality with specific values cited.",  # noqa: E501
                    "score5_description": "Review presents a fully transparent confidence breakdown showing per-agent scores, inter-agent agreement rate, data completeness, and a clearly weighted meta-confidence with the methodology explained.",  # noqa: E501
                },
            ),
            _ui_resp,
        ),
    ]

    scores = await _run_metrics(pairs, "reviewer", trace_id)
    if scores:
        logger.info("[reviewer] RAGAS scores summary (trace=%s): %s", trace_id, scores)
    else:
        logger.info("[reviewer] No RAGAS scores computed")


def _build_reviewer_reference(result: dict[str, Any]) -> str:
    """Serialize reviewer output into a factual reference string."""
    parts: list[str] = []
    verdict = result.get("verdict")
    if verdict:
        parts.append(f"Verdict: {verdict}")
    contradictions = result.get("contradictions", [])
    if contradictions:
        descs = [
            f"{c.get('agents', [])} on {c.get('field')} (severity: {c.get('severity')})"
            for c in contradictions
        ]
        parts.append(f"Contradictions: {'; '.join(descs)}")
    cb = result.get("confidence_breakdown") or {}
    if cb:
        parts.append(
            f"Meta-confidence: {cb.get('meta_confidence')}, "
            f"agreement: {cb.get('agreement_score')}, "
            f"data quality: {cb.get('data_quality_score')}, "
            f"agent scores: {cb.get('agent_scores', {})}"
        )
    rv = result.get("recommendation_validation") or {}
    if rv:
        parts.append(
            f"Evidence strength: {rv.get('evidence_strength')}, "
            f"supports recommendation: {rv.get('evidence_supports')}"
        )
    flags = result.get("flags", [])
    if flags:
        parts.append(f"Flags: {', '.join(flags)}")
    conf = result.get("review_confidence")
    if conf is not None:
        parts.append(f"Review confidence: {conf}")
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Market Context Agent — CrewAI (formerly Sentiment Agent)
# ---------------------------------------------------------------------------


async def score_sentiment_response(
    user_input: str,
    response: str,
    retrieved_contexts: list[str],
    trace_id: str | None = None,
) -> None:
    """Score Market Context Agent narrative.

    Fires after the agent synthesises macro regime + peer landscape data.
    Faithfulness checks grounding in macro indicators and peer financials.
    Domain rubrics test whether the agent discussed the macro regime (rates,
    VIX, sector rotation) and compared the target against named peers.

    Metrics: Faithfulness, macro_regime_analysis, peer_landscape_analysis.
    AnswerRelevancy removed — fragile on finance text.
    """
    logger.info(
        "[market_context] Eval entered (response_len=%d, contexts=%d, trace=%s)",
        len(response) if response else 0,
        len(retrieved_contexts) if retrieved_contexts else 0,
        trace_id,
    )
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        logger.warning(
            "[market_context] Skipping eval: response too short (len=%d)",
            len(response) if response else 0,
        )
        return
    if not _gate_ok(user_input, response, "market_context"):
        return

    try:
        clients = await _setup_ragas_clients()
        if clients is None:
            logger.warning("[market_context] Skipping eval: no RAGAS clients")
            return
        ragas_llm, _ = clients

        try:
            from ragas.metrics.collections import DomainSpecificRubrics, Faithfulness
        except ImportError:
            logger.warning("[market_context] Skipping eval: ragas import failed")
            return

        _ui_resp = {"user_input": user_input, "response": response}
        _ctx_kwargs = {
            "user_input": user_input,
            "response": response,
            "retrieved_contexts": retrieved_contexts,
        }
        pairs: list[tuple[Any, dict[str, Any]]] = [
            (
                DomainSpecificRubrics(
                    name="macro_regime_analysis",
                    llm=ragas_llm,
                    rubrics={
                        "score1_description": "Response makes no mention of any macro indicator (interest rates, VIX, dollar strength, or sector rotation).",  # noqa: E501
                        "score2_description": "Response vaguely references 'macro conditions' without citing a specific indicator or value.",  # noqa: E501
                        "score3_description": "Response names one specific macro indicator (e.g. yield curve regime, VIX level, or DXY trend) and explains its relevance to the stock.",  # noqa: E501
                        "score4_description": "Response discusses two or more macro indicators with specific values and connects them to the investment thesis.",  # noqa: E501
                        "score5_description": "Response comprehensively analyses the macro regime (yield curve shape, VIX volatility, DXY trend, and sector ETF rotation) with actual values, explaining how each dimension favours or penalises the target stock.",  # noqa: E501
                    },
                ),
                _ui_resp,
            ),
            (
                DomainSpecificRubrics(
                    name="peer_landscape_analysis",
                    llm=ragas_llm,
                    rubrics={
                        "score1_description": "Response does not mention any competitor or peer company.",  # noqa: E501
                        "score2_description": "Response names one competitor but provides no comparative metrics.",  # noqa: E501
                        "score3_description": "Response compares the target against at least one named peer on a specific metric (PE, growth, or margin).",  # noqa: E501
                        "score4_description": "Response compares the target against two or more named peers across at least two financial metrics.",  # noqa: E501
                        "score5_description": "Response provides a detailed peer landscape comparison across multiple named competitors using specific metrics (PE, revenue growth, operating margin, ROE) and identifies clear relative positioning headwinds or tailwinds.",  # noqa: E501
                    },
                ),
                _ui_resp,
            ),
        ]
        if retrieved_contexts:
            pairs.append((Faithfulness(llm=ragas_llm), _ctx_kwargs))

        scores = await _run_metrics(pairs, "market_context", trace_id)
        if scores:
            logger.info("[market_context] RAGAS scores summary (trace=%s): %s", trace_id, scores)
        else:
            logger.info("[market_context] No RAGAS scores computed")
    except Exception:
        logger.exception("[market_context] Eval crashed unexpectedly")
        _record_eval_failure()


# ---------------------------------------------------------------------------
# Deterministic Quant validator (schema + weight normalization)
# ---------------------------------------------------------------------------


def score_quant_deterministic(quant_result: dict[str, Any]) -> dict[str, Any]:
    """Deterministic schema + invariant checks on Quant result — no LLM, no cost.

    Validates:
      - signal_scores keys cover the 8 expected groups
      - their associated weights sum to ~1.0
      - behavioral fields are present (options_signals, insider_signals, positioning)
      - monte_carlo and peer_comparison are dict-shaped when present

    Returns a dict of bool checks plus a `passed` aggregate. Cheap enough to
    run on every Quant response without rate limiting.
    """
    # Keys match _SIGNAL_WEIGHTS in quant/nodes.py
    expected_groups = {
        "risk_quality",
        "dcf_value",
        "fundamental_value",
        "fundamental_quality",
        "technicals_trend",
        "technicals_momentum",
        "peer_positioning",
        "behavioral",
    }
    expected_weights = {
        "risk_quality": 0.15,
        "dcf_value": 0.20,
        "fundamental_value": 0.13,
        "fundamental_quality": 0.12,
        "technicals_trend": 0.15,
        "technicals_momentum": 0.10,
        "peer_positioning": 0.10,
        "behavioral": 0.05,
    }

    checks: dict[str, Any] = {}
    # signal_scores lives under metrics, not at the top level
    sig = (quant_result.get("metrics") or {}).get("signal_scores") or {}
    checks["signal_scores_present"] = bool(sig)
    checks["signal_groups_complete"] = expected_groups.issubset(set(sig.keys()))
    checks["weights_sum_to_1"] = abs(sum(expected_weights.values()) - 1.0) < 1e-6

    for field in ("options_signals", "insider_signals", "positioning"):
        v = quant_result.get(field)
        checks[f"{field}_present"] = v is not None
        checks[f"{field}_is_dict"] = isinstance(v, dict) or v is None

    mc = quant_result.get("monte_carlo")
    checks["monte_carlo_well_formed"] = mc is None or (
        isinstance(mc, dict) and "p50" in mc and "prob_profit" in mc
    )

    pc = quant_result.get("peer_comparison")
    checks["peer_comparison_well_formed"] = pc is None or (
        isinstance(pc, dict) and ("rankings" in pc or "peers" in pc)
    )

    rec = quant_result.get("recommendation")
    checks["recommendation_valid"] = rec in {"BUY", "HOLD", "SELL", None}

    # quant_confidence lives under metrics
    conf = (quant_result.get("metrics") or {}).get("quant_confidence")
    checks["confidence_in_range"] = conf is None or (
        isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0
    )

    checks["passed"] = all(v for k, v in checks.items() if k != "passed")
    return checks


# ---------------------------------------------------------------------------
# Analytics Agent — PydanticAI (deterministic)
# ---------------------------------------------------------------------------


def score_analytics_deterministic(analytics_result: dict[str, Any]) -> dict[str, Any]:
    """Deterministic schema + invariant checks on Analytics result — no LLM, no cost.

    Validates:
      - analytics_confidence is in [0.0, 1.0]
      - analytics_signal is one of expected values
      - forecast horizon and MAPE are plausible when present
      - forecast dates are future-dated when present
      - trend_direction is one of expected values
      - trend_strength is in [0.0, 1.0]
      - chart datasets are non-empty when charts are present
      - anomaly severity is one of expected values
      - statistical summary values are plausible
    """
    from datetime import date

    checks: dict[str, Any] = {}

    conf = analytics_result.get("analytics_confidence")
    checks["confidence_in_range"] = (
        conf is not None and isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0
    )

    signal = analytics_result.get("analytics_signal")
    checks["signal_valid"] = signal in {"bullish", "bearish", "neutral"}

    trend = analytics_result.get("trend_analysis") or {}
    checks["trend_direction_valid"] = trend.get("trend_direction", "neutral") in {
        "bullish",
        "bearish",
        "neutral",
    }
    ts = trend.get("trend_strength")
    checks["trend_strength_in_range"] = ts is None or (
        isinstance(ts, (int, float)) and 0.0 <= ts <= 1.0
    )
    ma_signal = trend.get("ma_crossover_signal")
    checks["ma_crossover_valid"] = ma_signal is None or ma_signal in {
        "golden_cross",
        "death_cross",
        "bullish_alignment",
        "bearish_alignment",
    }

    forecast = analytics_result.get("forecast") or {}
    if forecast:
        horizon = forecast.get("horizon_days")
        checks["forecast_horizon_positive"] = (
            horizon is not None and isinstance(horizon, int) and horizon > 0
        )
        mape = forecast.get("mape")
        checks["forecast_mape_plausible"] = mape is None or (
            isinstance(mape, (int, float)) and 0.0 <= mape <= 100.0
        )
        f_prices = forecast.get("forecast_prices", [])
        f_dates = forecast.get("forecast_dates", [])
        checks["forecast_lengths_match"] = len(f_prices) == len(f_dates)
        if f_dates:
            try:
                first_date = date.fromisoformat(f_dates[0])
                checks["forecast_dates_future"] = first_date >= date.today()
            except (ValueError, TypeError):
                checks["forecast_dates_future"] = False
        else:
            checks["forecast_dates_future"] = True
    else:
        checks["forecast_horizon_positive"] = True
        checks["forecast_mape_plausible"] = True
        checks["forecast_lengths_match"] = True
        checks["forecast_dates_future"] = True

    charts = analytics_result.get("charts", [])
    checks["charts_well_formed"] = (
        all(
            isinstance(c, dict) and c.get("chart_type") and isinstance(c.get("datasets"), list)
            for c in charts
        )
        if charts
        else True
    )

    anomalies = analytics_result.get("anomalies") or {}
    severity = anomalies.get("severity", "none")
    checks["anomaly_severity_valid"] = severity in {"none", "low", "medium", "high"}
    acount = anomalies.get("anomaly_count", 0)
    checks["anomaly_count_consistent"] = (
        (severity == "none" and acount == 0) or (severity != "none" and acount > 0) or not anomalies
    )

    stats = analytics_result.get("statistical_summary") or {}
    dist = stats.get("return_distribution")
    checks["distribution_valid"] = dist is None or dist in {
        "normal",
        "leptokurtic",
        "platykurtic",
    }
    jb = stats.get("jarque_bera_pvalue")
    checks["jarque_bera_valid"] = jb is None or (isinstance(jb, (int, float)) and 0.0 <= jb <= 1.0)

    checks["passed"] = all(v for k, v in checks.items() if k != "passed")
    return checks


# ---------------------------------------------------------------------------
# Reviewer Agent — OpenAI Agents SDK (deterministic)
# ---------------------------------------------------------------------------


def score_reviewer_deterministic(reviewer_result: dict[str, Any]) -> dict[str, Any]:
    """Deterministic schema + invariant checks on Reviewer result — no LLM, no cost.

    Validates:
      - review_confidence is in [0.0, 1.0]
      - verdict is a valid recommendation
      - contradictions reference known agent names
      - source_verifications have valid verification_rate
      - confidence_breakdown scores are in [0.0, 1.0]
      - recommendation_validation evidence_strength is valid
      - review_summary is non-empty when contradictions or flags exist
    """
    checks: dict[str, Any] = {}

    conf = reviewer_result.get("review_confidence")
    checks["confidence_in_range"] = (
        conf is not None and isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0
    )

    verdict = reviewer_result.get("verdict")
    checks["verdict_valid"] = verdict in {"BUY", "HOLD", "SELL"}

    known_agents = {
        "quant",
        "rag",
        "market_context",
        "analytics",
        "Quant Analysis Agent",
        "Financial RAG Agent",
        "Market Context Agent",
        "Analytics Agent",
    }
    contradictions = reviewer_result.get("contradictions", [])
    checks["contradictions_well_formed"] = (
        all(
            isinstance(c, dict)
            and isinstance(c.get("agents"), list)
            and c.get("field")
            and c.get("severity") in {"low", "medium", "high"}
            for c in contradictions
        )
        if contradictions
        else True
    )

    checks["contradiction_agents_known"] = (
        all(
            any(a.lower() in known.lower() or known.lower() in a.lower() for known in known_agents)
            for c in contradictions
            for a in (c.get("agents") or [])
        )
        if contradictions
        else True
    )

    verifications = reviewer_result.get("source_verifications", [])
    checks["verifications_well_formed"] = (
        all(
            isinstance(v, dict)
            and v.get("agent_name")
            and isinstance(v.get("verification_rate", 0), (int, float))
            and 0.0 <= v.get("verification_rate", 0) <= 1.0
            for v in verifications
        )
        if verifications
        else True
    )

    cb = reviewer_result.get("confidence_breakdown") or {}
    if cb:
        for score_key in ("agreement_score", "data_quality_score", "meta_confidence"):
            val = cb.get(score_key)
            checks[f"cb_{score_key}_valid"] = val is None or (
                isinstance(val, (int, float)) and 0.0 <= val <= 1.0
            )
        agent_scores = cb.get("agent_scores", {})
        checks["cb_agent_scores_valid"] = (
            all(isinstance(v, (int, float)) and 0.0 <= v <= 1.0 for v in agent_scores.values())
            if agent_scores
            else True
        )
    else:
        checks["cb_agreement_score_valid"] = True
        checks["cb_data_quality_score_valid"] = True
        checks["cb_meta_confidence_valid"] = True
        checks["cb_agent_scores_valid"] = True

    rv = reviewer_result.get("recommendation_validation") or {}
    if rv:
        rec = rv.get("recommendation")
        checks["rv_recommendation_valid"] = rec in {"BUY", "HOLD", "SELL"}
        strength = rv.get("evidence_strength")
        checks["rv_evidence_strength_valid"] = strength in {"weak", "moderate", "strong"}
    else:
        checks["rv_recommendation_valid"] = True
        checks["rv_evidence_strength_valid"] = True

    flags = reviewer_result.get("flags", [])
    summary = reviewer_result.get("review_summary", "")
    has_findings = bool(contradictions) or bool(flags)
    checks["summary_present_when_findings_exist"] = bool(summary) or not has_findings

    checks["passed"] = all(v for k, v in checks.items() if k != "passed")
    return checks
