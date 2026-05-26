"""Entry point for the ADK web UI runner.

Initializes observability, instruments the ADK runtime, and exports the
root_agent so `adk web` can discover and serve it.
"""
from shared.observability import init_langfuse

init_langfuse(service_name="orchestrator")

from openinference.instrumentation.google_adk import GoogleADKInstrumentor

GoogleADKInstrumentor().instrument()

from .agent import root_agent

__all__ = ["root_agent"]
