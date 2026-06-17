from openai import AsyncOpenAI

from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from shared.agent_models import ReviewerAgentOutput
from shared.settings import LLM_BASE_URL, LLM_SUMMARY_MODEL

_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="lm-studio")
_model = OpenAIChatCompletionsModel(model=LLM_SUMMARY_MODEL, openai_client=_client)

reviewer_agent = Agent(
    name="Reviewer",
    model=_model,
    instructions="You are an investment analysis reviewer. Given pre-computed validation results, synthesize into a verdict.",
    output_type=ReviewerAgentOutput,
)
