"""Runtime RAGAS evaluation — per-agent scoring as background tasks.

Runtime-feasible metrics (no ground-truth reference needed unless noted):

  Orchestrator (score_response):
    ResponseRelevancy       — synthesis relevant to the investment query?
    citation_quality        — AspectCritic: cites specific figures/filings?
    risk_disclosure         — AspectCritic: acknowledges investment risks?
    recommendation_clarity  — RubricsScore 1-5: clear BUY/HOLD/SELL with evidence?
    response_completeness   — AspectCritic: integrates RAG + quant + sentiment findings?

  RAG Agent (score_rag_response):
    Faithfulness                         — claims grounded in retrieved SEC chunks?
    ResponseRelevancy                    — answer addresses the filing/earnings query?
    LLMContextPrecisionWithoutReference  — retrieved chunks relevant to the query?

  Quant Agent (score_quant_response):
    FactualCorrectness   — LLM summary numbers match computed metrics (computed = reference)
    ResponseRelevancy    — quant analysis addresses the risk/valuation query?

  Sentiment Agent (score_sentiment_response):
    Faithfulness              — narrative claims grounded in fetched news/filings?
    ResponseRelevancy         — sentiment analysis matches the ticker/context?
    catalyst_identification   — AspectCritic: identifies key business catalysts?
    insider_signal_discussion — AspectCritic: incorporates insider/institutional signals?

Offline-only (require ground-truth reference — live in tests/evaluation/):
  ContextRecall, ContextEntityRecall, ContextPrecision (standard),
  ToolCallAccuracy, AgentGoalAccuracy, FactualCorrectness (RAG/Sentiment)

All public functions are safe to fire-and-forget via asyncio.create_task().
LLM calls go to LM Studio at LLM_BASE_URL.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_MIN_RESPONSE_LEN = 80
_ragas_clients: tuple | None = None


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

async def _setup_ragas_clients():
    """Return (ragas_llm, ragas_embedder) or None if dependencies are missing."""
    global _ragas_clients
    if _ragas_clients is not None:
        return _ragas_clients
    try:
        import instructor
        from openai import AsyncOpenAI
        from ragas.llms.base import InstructorLLM, InstructorModelArgs
        from ragas.embeddings.base import BaseRagasEmbedding
    except ImportError:
        logger.debug("ragas / instructor / openai not installed — runtime eval skipped")
        return None

    from shared.config import LLM_BASE_URL, LLM_MODEL, EMBED_MODEL

    # ragas default uses instructor.Mode.JSON → response_format.type="json_object"
    # LM Studio only accepts "json_schema" or "text", so patch with JSON_SCHEMA mode.
    # ragas 0.4.x uses BaseRagasEmbedding (embed_text + aembed_text).
    # HuggingfaceEmbeddings is a broken pydantic dataclass; implement directly.
    class _STEmbeddings(BaseRagasEmbedding):
        def __init__(self, model_name: str) -> None:
            import sentence_transformers
            self._model = sentence_transformers.SentenceTransformer(model_name)

        def embed_text(self, text: str) -> list:
            return self._model.encode(
                text, normalize_embeddings=True, convert_to_tensor=False
            ).tolist()

        async def aembed_text(self, text: str) -> list:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.embed_text, text)

    try:
        client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="lm-studio", timeout=180)
        patched = instructor.from_openai(client, mode=instructor.Mode.JSON_SCHEMA)
        ragas_llm = InstructorLLM(
            client=patched,
            model=LLM_MODEL,
            provider="openai",
            model_args=InstructorModelArgs(max_tokens=2048),
        )
        ragas_embedder = _STEmbeddings(model_name=EMBED_MODEL)
        _ragas_clients = (ragas_llm, ragas_embedder)
        return ragas_llm, ragas_embedder
    except Exception as exc:
        logger.warning("Runtime eval client setup failed: %s", exc)
        _ragas_clients = None
        return None


async def _score_metric(metric, **kwargs) -> float:
    try:
        result = await metric.ascore(**kwargs)
        return result.value
    except Exception:
        logger.warning("RAGAS metric '%s' ascore failed (kwargs keys: %s)", metric.name, list(kwargs.keys()), exc_info=True)
        raise


async def _run_metrics(pairs: list) -> dict[str, float]:
    """Run (metric, kwargs) pairs concurrently, return name→score dict."""
    results = await asyncio.gather(
        *[_score_metric(m, **kw) for m, kw in pairs],
        return_exceptions=True,
    )
    scores: dict[str, float] = {}
    for (metric, _), result in zip(pairs, results):
        if isinstance(result, Exception):
            logger.warning("RAGAS metric '%s' error: %s", metric.name, result)
        elif result is not None:
            scores[metric.name] = round(float(result), 4)
    return scores


def _push_scores(scores: dict[str, float], trace_id: str | None, agent: str) -> None:
    if not scores or trace_id is None:
        return
    try:
        from langfuse import Langfuse
        lf = Langfuse()
        for name, value in scores.items():
            kwargs: dict = {"name": f"ragas/{name}", "value": value}
            if trace_id:
                kwargs["trace_id"] = trace_id
            lf.create_score(**kwargs)
        lf.flush()
        logger.debug("[%s] Pushed %d RAGAS scores to trace %s", agent, len(scores), trace_id)
    except Exception as exc:
        logger.warning("Langfuse score push failed: %s", exc)


# ---------------------------------------------------------------------------
# Orchestrator — ADK
# ---------------------------------------------------------------------------

async def score_response(
    user_input: str,
    response: str,
    trace_id: str | None = None,
) -> None:
    """Score orchestrator final synthesis.

    Metrics: ResponseRelevancy, citation_quality, risk_disclosure,
             recommendation_clarity, response_completeness.
    """
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, ragas_embedder = clients

    try:
        from ragas.metrics.collections import DomainSpecificRubrics, RubricsScoreWithoutReference, AnswerRelevancy
    except ImportError:
        return

    _ui_resp = {"user_input": user_input, "response": response}
    pairs = [
        (AnswerRelevancy(llm=ragas_llm, embeddings=ragas_embedder), _ui_resp),
        (DomainSpecificRubrics(
            name="citation_quality",
            llm=ragas_llm,
            rubrics={
                "score1_description": "Response makes only generic claims with no specific figures, filing references, dates, or dollar amounts.",
                "score2_description": "Response mentions one vague reference but without specific figures or filing details.",
                "score3_description": "Response includes one specific figure or filing reference.",
                "score4_description": "Response cites two or more specific figures, dates, or filing sections.",
                "score5_description": "Response extensively cites specific filing sections, dates, dollar amounts, and percentages throughout.",
            },
        ), _ui_resp),
        (DomainSpecificRubrics(
            name="risk_disclosure",
            llm=ragas_llm,
            rubrics={
                "score1_description": "Response presents only upside with no mention of any investment risk.",
                "score2_description": "Response hints at risk in passing but does not name a specific risk factor.",
                "score3_description": "Response acknowledges one material risk (regulatory, competitive, market, or operational).",
                "score4_description": "Response explicitly discusses two or more distinct risk factors.",
                "score5_description": "Response provides a balanced assessment with detailed discussion of multiple material risks across different categories.",
            },
        ), _ui_resp),
        (RubricsScoreWithoutReference(
            name="recommendation_clarity",
            llm=ragas_llm,
            rubrics={
                "score1_description": "No clear BUY/HOLD/SELL signal present.",
                "score2_description": "Signal present but rationale is absent or vague.",
                "score3_description": "Signal with rationale but supported by only one data point.",
                "score4_description": "Signal with rationale supported by two or more data points from different sources.",
                "score5_description": "Clear signal with specific supporting evidence from at least two agent analyses (quant, filing, or sentiment) with cited figures.",
            },
        ), _ui_resp),
        (DomainSpecificRubrics(
            name="response_completeness",
            llm=ragas_llm,
            rubrics={
                "score1_description": "Response draws from only one analysis type or provides only a generic summary.",
                "score2_description": "Response references two analysis types but one is superficial.",
                "score3_description": "Response integrates findings from two of: filing analysis, quantitative metrics, or news sentiment.",
                "score4_description": "Response integrates all three analysis types with reasonable depth.",
                "score5_description": "Response thoroughly synthesises filing analysis, quantitative metrics (Sharpe, DCF, VaR), and news/sentiment into a cohesive brief.",
            },
        ), _ui_resp),
    ]

    scores = await _run_metrics(pairs)
    if scores:
        logger.info("[orchestrator] RAGAS scores (trace=%s): %s", trace_id, scores)
        _push_scores(scores, trace_id, "orchestrator")
    else:
        logger.debug("[orchestrator] No RAGAS scores computed")


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

    Metrics: Faithfulness, ResponseRelevancy, LLMContextPrecisionWithoutReference.
    Requires retrieved_contexts (text of ChromaDB source nodes).
    """
    if not user_input or not response or not retrieved_contexts:
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, ragas_embedder = clients

    try:
        from ragas.metrics.collections import (
            Faithfulness,
            AnswerRelevancy,
            ContextPrecisionWithoutReference,
        )
    except ImportError:
        return

    _ctx_kwargs = {"user_input": user_input, "response": response, "retrieved_contexts": retrieved_contexts}
    pairs = [
        (Faithfulness(llm=ragas_llm), _ctx_kwargs),
        (AnswerRelevancy(llm=ragas_llm, embeddings=ragas_embedder), {"user_input": user_input, "response": response}),
        (ContextPrecisionWithoutReference(llm=ragas_llm), _ctx_kwargs),
    ]

    scores = await _run_metrics(pairs)
    if scores:
        logger.info("[rag] RAGAS scores (trace=%s): %s", trace_id, scores)
        _push_scores(scores, trace_id, "rag")
    else:
        logger.debug("[rag] No RAGAS scores computed")


# ---------------------------------------------------------------------------
# Quant Agent — LangGraph
# ---------------------------------------------------------------------------

async def score_quant_response(
    user_input: str,
    response: str,
    quant_result: dict,
    trace_id: str | None = None,
) -> None:
    """Score quant agent LLM summary.

    Metrics: FactualCorrectness (computed metrics = reference), ResponseRelevancy.

    FactualCorrectness checks whether the LLM summary's numerical claims (Sharpe,
    VaR, DCF values) match the deterministically computed values — catching
    hallucinated numbers without needing external ground truth.
    """
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        return

    reference = _build_quant_reference(quant_result)
    if not reference:
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, ragas_embedder = clients

    try:
        from ragas.metrics.collections import FactualCorrectness, AnswerRelevancy
    except ImportError:
        return

    pairs = [
        (FactualCorrectness(llm=ragas_llm), {"response": response, "reference": reference}),
        (AnswerRelevancy(llm=ragas_llm, embeddings=ragas_embedder), {"user_input": user_input, "response": response}),
    ]

    scores = await _run_metrics(pairs)
    if scores:
        logger.info("[quant] RAGAS scores (trace=%s): %s", trace_id, scores)
        _push_scores(scores, trace_id, "quant")
    else:
        logger.debug("[quant] No RAGAS scores computed")


def _build_quant_reference(result: dict) -> str:
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
        parts.append(
            f"CVaR 95%: {stress.get('cvar_95')}, VaR 95%: {stress.get('var_95')}"
        )
    rec = result.get("recommendation")
    if rec:
        parts.append(f"Quantitative signal: {rec}")
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Sentiment Agent — CrewAI
# ---------------------------------------------------------------------------

async def score_sentiment_response(
    user_input: str,
    response: str,
    retrieved_contexts: list[str],
    trace_id: str | None = None,
) -> None:
    """Score sentiment agent narrative.

    Metrics: Faithfulness, ResponseRelevancy,
             catalyst_identification, insider_signal_discussion (AspectCritic).
    Requires retrieved_contexts (news article titles/summaries from MCP).
    """
    if not user_input or not response or len(response) < _MIN_RESPONSE_LEN:
        return

    clients = await _setup_ragas_clients()
    if clients is None:
        return
    ragas_llm, ragas_embedder = clients

    try:
        from ragas.metrics.collections import Faithfulness, AnswerRelevancy, DomainSpecificRubrics
    except ImportError:
        return

    _ui_resp = {"user_input": user_input, "response": response}
    _ctx_kwargs = {"user_input": user_input, "response": response, "retrieved_contexts": retrieved_contexts}
    pairs: list = [
        (AnswerRelevancy(llm=ragas_llm, embeddings=ragas_embedder), _ui_resp),
        (DomainSpecificRubrics(
            name="catalyst_identification",
            llm=ragas_llm,
            rubrics={
                "score1_description": "Response identifies no specific business catalysts or growth drivers.",
                "score2_description": "Response vaguely mentions positive developments without naming specific catalysts.",
                "score3_description": "Response identifies one specific catalyst (e.g. a product launch, earnings beat, or partnership).",
                "score4_description": "Response identifies two or more specific catalysts from news.",
                "score5_description": "Response comprehensively identifies and contextualises multiple specific catalysts (product launches, earnings beats, partnerships, regulatory approvals, or management changes) from the news.",
            },
        ), _ui_resp),
        (DomainSpecificRubrics(
            name="insider_signal_discussion",
            llm=ragas_llm,
            rubrics={
                "score1_description": "Response makes no mention of insider trading or institutional ownership signals.",
                "score2_description": "Response briefly mentions institutional activity without detail.",
                "score3_description": "Response discusses one specific insider or institutional signal relevant to the thesis.",
                "score4_description": "Response incorporates insider trading activity and institutional ownership changes with supporting detail.",
                "score5_description": "Response thoroughly analyses insider trading patterns and significant institutional buying/selling with direct relevance to the investment thesis.",
            },
        ), _ui_resp),
    ]
    if retrieved_contexts:
        pairs.append((Faithfulness(llm=ragas_llm), _ctx_kwargs))

    scores = await _run_metrics(pairs)
    if scores:
        logger.info("[sentiment] RAGAS scores (trace=%s): %s", trace_id, scores)
        _push_scores(scores, trace_id, "sentiment")
    else:
        logger.debug("[sentiment] No RAGAS scores computed")
