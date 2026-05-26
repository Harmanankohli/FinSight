from shared.observability import init_langfuse

init_langfuse(service_name="orchestrator")

from openinference.instrumentation.google_adk import GoogleADKInstrumentor

GoogleADKInstrumentor().instrument()

from .agent import root_agent

__all__ = ["root_agent"]
