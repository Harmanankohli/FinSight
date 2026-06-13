"""Characterization test fixtures and heavy-dependency stubs.

These stubs prevent import errors in CI/dev environments where optional
heavy packages (langfuse, transformers, google-adk, etc.) are not installed.
They are inserted into sys.modules BEFORE any test imports trigger them.
"""

import sys
from unittest.mock import MagicMock

_STUB_MODULES = [
    # Langfuse (observability)
    "langfuse",
    "langfuse.client",
    "langfuse.span_filter",
    "langfuse.decorators",
    "langfuse.decorators.langfuse_decorator",
    # OpenInference instrumentation
    "openinference",
    "openinference.instrumentation",
    "openinference.instrumentation.google_adk",
    "openinference.instrumentation.llama_index",
    "openinference.instrumentation.crewai",
    "openinference.instrumentation.langchain",
    # OpenTelemetry
    "opentelemetry.instrumentation.starlette",
    "opentelemetry.instrumentation.httpx",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    # Google ADK (full hierarchy so submodule lookups work)
    "google.adk",
    "google.adk.runners",
    "google.adk.sessions",
    "google.adk.agents",
    "google.adk.events",
    "google.adk.tools",
    "google.adk.tools.tool_context",
    "google.adk.models",
    "google.adk.memory",
    "google.adk.memory.base_memory_service",
    "google.adk.memory.memory_entry",
    # google.genai
    "google.genai",
    "google.genai.types",
    # google.protobuf
    "google.protobuf.json_format",
    "google.protobuf.struct_pb2",
    # ML models (slow to load)
    "transformers",
    "sentence_transformers",
]


def _identity_decorator(*args, **kwargs):
    """Return an identity decorator — used to stub @observe(), @logged(), etc."""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]  # used as @decorator (no parens)
    return lambda f: f  # used as @decorator(...)


for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        stub = MagicMock()
        # Make any attribute of the stub behave as an identity decorator
        # so @stub.observe() / @stub.something() wraps don't break real functions
        stub.__call__ = _identity_decorator
        sys.modules[_mod] = stub

# Make langfuse.observe specifically an identity decorator (used as @observe())
import langfuse as _langfuse_mod  # noqa: E402 (already stubbed above)

_langfuse_mod.observe = _identity_decorator
if hasattr(_langfuse_mod, "decorators"):
    _langfuse_mod.decorators.observe = _identity_decorator

# Stub orchestrator.agent so api_routes lazy-imports don't trigger real ADK+a2a-sdk
# (a2a-sdk loads real protobuf descriptors at import time which requires libprotobuf)
_mock_agent_client = MagicMock()
_mock_agent_client.list_agents.return_value = []
_mock_agent_client._agents = {}

_agent_mod_stub = MagicMock()
_agent_mod_stub._client = _mock_agent_client

if "orchestrator.agent" not in sys.modules:
    sys.modules["orchestrator.agent"] = _agent_mod_stub
