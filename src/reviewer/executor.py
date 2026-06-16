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

logger = logging.getLogger(__name__)


class ReviewerAgent(BaseAgent):
    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        super().__init__(
            agent_name="Reviewer Agent",
            description="Cross-validates all agent outputs, checks contradictions, and produces calibrated meta-confidence using OpenAI Agents SDK",
            content_types=["text", "application/json"],
        )

    async def stream(self, query: str, context_id: str, task_id: str) -> AsyncIterable[dict]:
        logger.info("ReviewerAgent.stream() called: query=%s...", query[:80])
        yield await self._build_response(query)

    @logged()
    async def _build_response(self, query: str) -> dict:
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
            try:
                payload = json.loads(query)
                ticker = payload.get("ticker", "")
                agent_outputs = payload.get("agent_outputs", {})
            except json.JSONDecodeError:
                ticker = extract_ticker(query) or ""
                agent_outputs = {}
                span.update(output={"warning": "Query was not JSON, parsed ticker from text"})

            prompt = json.dumps({"ticker": ticker, "agent_outputs": agent_outputs})

            async with llm_queue.acquire(Priority.CRITICAL, "reviewer-report"):
                result = await Runner.run(reviewer_agent, input=prompt)

            output = result.final_output
            output_dict = output.model_dump() if hasattr(output, "model_dump") else output

            schema_checks = score_reviewer_deterministic(output_dict)
            if not schema_checks.get("passed", False):
                failing = [k for k, v in schema_checks.items() if k != "passed" and not v]
                logger.warning(
                    "Reviewer deterministic checks failed for %s: %s", ticker, failing
                )
            output_dict["schema_validation"] = schema_checks

            span.update(output={"ticker": ticker, "verdict": output_dict.get("verdict")})

            if EVAL_ENABLED:
                from shared.eval_gate import defer_eval
                from shared.runtime_eval import score_reviewer_response as _eval_reviewer

                defer_eval(
                    _eval_reviewer,
                    prompt,
                    output_dict.get("review_summary", ""),
                    output_dict,
                    trace_id,
                )

            return {
                "is_task_complete": True,
                "content": json.dumps(output_dict),
            }
