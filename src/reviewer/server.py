# ruff: noqa: E402
import logging

from shared.bootstrap import bootstrap

_settings = bootstrap("reviewer")

from shared.observability import init_instrumentation

init_instrumentation("reviewer")

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.agent_server import build_agent_app
from shared.logging_config import logged

from .executor import ReviewerAgent

logger = logging.getLogger(__name__)

agent_card = AgentCard(
    name="Reviewer Agent",
    description="Cross-validates all agent outputs, checks for contradictions, and produces calibrated meta-confidence using OpenAI Agents SDK",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url=f"http://{_settings.host}:{_settings.agent_port_reviewer}/a2a",
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="contradiction_check",
            name="Contradiction Check",
            description="Cross-agent contradiction detection: quant vs analytics trend, quant vs RAG sentiment, macro vs quant signal",
            tags=["contradiction", "validation", "cross-check"],
            examples=["Check for contradictions between quant and analytics for AAPL"],
        ),
        AgentSkill(
            id="source_verification",
            name="Source Verification",
            description="Verify each agent's output for internal consistency and plausible value ranges",
            tags=["verification", "consistency", "plausibility"],
            examples=["Verify DCF upside calculation is consistent"],
        ),
        AgentSkill(
            id="confidence_scoring",
            name="Confidence Scoring",
            description="Aggregate per-agent confidence, agreement score, data quality, and meta-confidence",
            tags=["confidence", "meta-analysis", "calibration"],
            examples=["Score overall confidence across all agents for NVDA"],
        ),
        AgentSkill(
            id="recommendation_validation",
            name="Recommendation Validation",
            description="Gather supporting and contradicting evidence for the quant recommendation and rate evidence strength",
            tags=["recommendation", "validation", "evidence"],
            examples=["Validate the BUY recommendation for TSLA"],
        ),
        AgentSkill(
            id="structured_review",
            name="Structured Review",
            description="Produce a structured meta-review with verdict, flags, and calibrated confidence for the orchestrator",
            tags=["review", "meta-review", "verdict"],
            examples=["Produce a complete review for AAPL analysis"],
        ),
    ],
)


app = build_agent_app(
    agent_card=agent_card,
    agent=ReviewerAgent(),
    service_name="reviewer",
    accept=frozenset({"service"}),
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=_settings.agent_port_reviewer)
