import json
import logging
from collections.abc import AsyncIterable

from agents import Runner

from shared.base_agent import BaseAgent
from shared.llm_queue import Priority, llm_queue
from shared.logging_config import logged, logged_sync
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_reviewer_deterministic
from shared.settings import EVAL_ENABLED
from shared.ticker_utils import extract_ticker
from shared.trace_context import extract_trace_ids

from .agent import reviewer_agent
from .tools.confidence import score_confidence
from .tools.contradiction import check_contradictions
from .tools.integrity import validate_metric_integrity
from .tools.validation import validate_recommendation
from .tools.verification import verify_sources

logger = logging.getLogger(__name__)


class ReviewerAgent(BaseAgent):
    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        super().__init__(
            agent_name="Reviewer Agent",
            description="Cross-validates all agent outputs, checks contradictions, and produces calibrated meta-confidence",
            content_types=["text", "application/json"],
        )

    async def stream(self, query: str, context_id: str, task_id: str) -> AsyncIterable[dict]:
        logger.info("ReviewerAgent.stream() called: query=%s...", query[:80])
        yield await self._build_response(query, context_id)

    @logged()
    async def _build_response(self, query: str, context_id: str = "") -> dict:
        trace_id, parent_span_id, query = extract_trace_ids(query)

        langfuse = get_langfuse_client()
        trace_ctx = (
            {"trace_id": trace_id, "parent_span_id": parent_span_id}
            if trace_id and parent_span_id
            else None
        )
        with langfuse.start_as_current_observation(
            as_type="span",
            name="reviewer-agent-stream",
            input=query,
            trace_context=trace_ctx,
        ) as span:
            payload = {}
            try:
                payload = json.loads(query)
                ticker = payload.get("ticker", "")
                session_id = payload.get("session_id", "")
            except json.JSONDecodeError:
                ticker = extract_ticker(query) or ""
                session_id = ""
                span.update(output={"warning": "Query was not JSON, parsed ticker from text"})

            # Fallback: use A2A context_id if session_id not in payload
            if not session_id and context_id:
                session_id = context_id
                logger.info("Reviewer using A2A context_id as session_id fallback: %s", session_id)

            # Try inline agent_outputs first (most reliable — orchestrator injects them directly)
            agent_outputs: dict[str, dict] = {}
            inline = payload.get("agent_outputs", {})
            if inline:
                agent_outputs = inline
                logger.info("Reviewer using inline agent_outputs (%d agents)", len(agent_outputs))

            # Fallback: fetch from shared SQLite store
            if not agent_outputs and session_id:
                from shared.memory.agent_output_store import get_agent_outputs
                agent_outputs = await get_agent_outputs(session_id)
                logger.info("Reviewer fetched %d agent outputs from store for session=%s", len(agent_outputs), session_id)

            if not agent_outputs:
                logger.warning("Reviewer has NO agent outputs for session=%s — tools will run on empty data", session_id)

            # Pre-reviewer integrity gate
            integrity_alerts = validate_metric_integrity(agent_outputs)
            critical_alerts = [a for a in integrity_alerts if a["severity"] == "critical"]
            if critical_alerts:
                logger.warning(
                    "Metric integrity: %d critical alert(s) for %s: %s",
                    len(critical_alerts), ticker,
                    "; ".join(a["message"] for a in critical_alerts),
                )

            # Call 4 tools directly in Python (no LLM round-trips)
            contradictions = check_contradictions(agent_outputs)
            verifications = verify_sources(agent_outputs)
            confidence_result = score_confidence(agent_outputs)
            validation_result = validate_recommendation(agent_outputs)

            # Build agent summaries for the LLM with key fields for synthesis
            agent_summaries = {}
            for key, data in agent_outputs.items():
                if key == "quant":
                    metrics = data.get("metrics") or {}
                    dcf = data.get("dcf_valuation") or {}
                    fundamentals = data.get("fundamentals") or {}
                    technicals = data.get("technicals") or {}
                    mc = data.get("monte_carlo") or {}
                    agent_summaries["quant"] = {
                        "recommendation": data.get("recommendation"),
                        "reasoning": (data.get("reasoning") or "")[:500],
                        "quant_signal": metrics.get("quant_signal"),
                        "quant_confidence": metrics.get("quant_confidence"),
                        "sharpe_ratio": metrics.get("sharpe_ratio"),
                        "beta": metrics.get("beta"),
                        "var_95_daily": metrics.get("var_95_daily"),
                        "dcf_intrinsic_value": dcf.get("intrinsic_value"),
                        "dcf_current_price": dcf.get("current_price"),
                        "dcf_upside_pct": dcf.get("upside_pct"),
                        "pe_ratio": fundamentals.get("trailing_pe"),
                        "roe": fundamentals.get("roe"),
                        "revenue_growth": fundamentals.get("revenue_growth"),
                        "debt_to_equity": fundamentals.get("debt_to_equity"),
                        "rsi": technicals.get("rsi") or technicals.get("rsi_14"),
                        "golden_cross": technicals.get("golden_cross"),
                        "trend": technicals.get("trend"),
                        "mc_p50": mc.get("p50"),
                        "mc_prob_profit": mc.get("prob_profit"),
                    }
                elif key == "rag":
                    agent_summaries["rag"] = {
                        "summary": (data.get("summary") or "")[:600],
                        "confidence_score": data.get("confidence_score"),
                        "source_count": len(data.get("sources", [])),
                    }
                elif key == "market_context":
                    agent_summaries["market_context"] = {
                        "overall_signal": data.get("overall_signal"),
                        "confidence_score": data.get("confidence_score"),
                        "narrative": (data.get("narrative") or "")[:500],
                        "tailwinds": data.get("key_tailwinds", [])[:3],
                        "headwinds": data.get("key_headwinds", [])[:3],
                        "macro_regime": data.get("macro_regime"),
                    }
                elif key == "analytics":
                    trend = data.get("trend_analysis") or {}
                    anomalies = data.get("anomalies") or {}
                    agent_summaries["analytics"] = {
                        "analytics_signal": data.get("analytics_signal"),
                        "analytics_confidence": data.get("analytics_confidence"),
                        "trend_direction": trend.get("trend_direction"),
                        "trend_strength": trend.get("trend_strength"),
                        "ma_crossover_signal": trend.get("ma_crossover_signal"),
                        "momentum_shift": trend.get("momentum_shift"),
                        "anomaly_severity": anomalies.get("severity"),
                        "anomaly_count": anomalies.get("anomaly_count"),
                    }

            # Build synthesis prompt with tool results AND agent summaries
            synthesis_data = {
                "ticker": ticker,
                "agent_summaries": agent_summaries,
                "contradictions": contradictions,
                "verifications": verifications,
                "confidence": confidence_result,
                "validation": validation_result,
            }
            if integrity_alerts:
                synthesis_data["integrity_alerts"] = integrity_alerts
            synthesis_prompt = json.dumps(synthesis_data)

            async with llm_queue.acquire(Priority.CRITICAL, "reviewer-report"):
                result = await Runner.run(reviewer_agent, input=synthesis_prompt)

            output = result.final_output
            output_dict = output.model_dump() if hasattr(output, "model_dump") else output

            schema_checks = score_reviewer_deterministic(output_dict)
            if not schema_checks.get("passed", False):
                failing = [k for k, v in schema_checks.items() if k != "passed" and not v]
                logger.warning(
                    "Reviewer deterministic checks failed for %s: %s", ticker, failing
                )
            output_dict["schema_validation"] = schema_checks
            if integrity_alerts:
                output_dict["integrity_alerts"] = integrity_alerts

            # Validate confidence range in Python (replaces output guardrail)
            review_confidence = output_dict.get("review_confidence")
            if review_confidence is not None and not (0.0 <= review_confidence <= 1.0):
                logger.warning("Reviewer confidence %s out of range, clamping", review_confidence)
                output_dict["review_confidence"] = max(0.0, min(1.0, review_confidence))

            span.update(output={"ticker": ticker, "verdict": output_dict.get("verdict")})

            if EVAL_ENABLED:
                from shared.eval_gate import defer_eval
                from shared.runtime_eval import score_reviewer_response as _eval_reviewer

                defer_eval(
                    _eval_reviewer,
                    synthesis_prompt,
                    output_dict.get("review_summary", ""),
                    output_dict,
                    trace_id,
                )

            return {
                "is_task_complete": True,
                "content": json.dumps(output_dict),
            }
