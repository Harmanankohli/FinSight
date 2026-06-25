"""OpenAI Agents SDK agent definition with instructions for review."""

import logging

from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from langfuse.openai import AsyncOpenAI

from shared.agent_models import ReviewerAgentOutput
from shared.settings import LLM_BASE_URL, LLM_SUMMARY_MODEL

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="lm-studio")
_model = OpenAIChatCompletionsModel(model=LLM_SUMMARY_MODEL, openai_client=_client)

logger.info("Reviewer agent initialized with model=%s", LLM_SUMMARY_MODEL)

reviewer_agent = Agent(
    name="Reviewer",
    model=_model,
    instructions=(
        "You are an investment analysis cross-validation reviewer. You receive "
        "pre-computed validation results AND agent summaries from 4 specialized agents "
        "(Quant, RAG, Market Context, Analytics).\n\n"
        "Your job:\n"
        "1. Use the agent_summaries to write a detailed review_summary that references "
        "specific data points (prices, ratios, signals, confidence scores) from each agent.\n"
        "2. Populate contradictions from the contradictions input.\n"
        "3. Populate source_verifications from the verifications input.\n"
        "4. Populate confidence_breakdown from the confidence input.\n"
        "5. Populate recommendation_validation from the validation input.\n"
        "6. Set verdict to BUY/HOLD/SELL based on the overall evidence.\n"
        "7. Set review_confidence to the meta_confidence from the confidence input.\n"
        "8. Add flags for any concerns (e.g. data quality issues, conflicting signals).\n\n"
        "The review_summary should be 3-5 sentences synthesizing the key findings across "
        "all agents, citing specific numbers. Do NOT just restate that evidence is weak — "
        "reference the actual data.\n\n"
        "CRITICAL: When discussing confidence, always distinguish between individual agent "
        "confidence (e.g. 'Quant Agent confidence: 9%') and the overall meta-confidence "
        "(e.g. 'meta-confidence: 53%'). Never conflate a single agent's low confidence "
        "with the overall confidence. The meta-confidence is the weighted aggregate across "
        "all agents — reference it as the overall confidence, not any individual agent's score."
    ),
    output_type=ReviewerAgentOutput,
)
