# ruff: noqa: E402
"""Entry point for the ADK web UI runner.

Initializes observability, instruments the ADK runtime, and exports the
root_agent so `adk web` can discover and serve it.
"""

from shared.observability import init_instrumentation, init_langfuse

init_langfuse(service_name="orchestrator")
init_instrumentation("orchestrator")

from .agent import root_agent

__all__ = ["root_agent"]
