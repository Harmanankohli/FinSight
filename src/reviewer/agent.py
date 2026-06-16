from openai import AsyncOpenAI

from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from shared.agent_models import ReviewerAgentOutput
from shared.settings import LLM_BASE_URL, LLM_SUMMARY_MODEL

from .guardrails import confidence_range_guardrail, payload_structure_guardrail
from .tools.contradiction import check_contradictions
from .tools.verification import verify_sources
from .tools.confidence import score_confidence
from .tools.validation import validate_recommendation

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
