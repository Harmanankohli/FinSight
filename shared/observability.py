import base64
import logging
import os
from typing import Any

from langfuse.span_filter import is_default_export_span
from shared.config import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

from shared.logging_config import logged_sync

logger = logging.getLogger(__name__)

# Lazy singleton: _langfuse_client is created once on first init_langfuse() call.
# This avoids importing / configuring Langfuse at import time, which is important
# because the SDK may try to connect on construction in some configs.
_langfuse_client: Any = None
_initialized = False


def init_langfuse(service_name: str = "finsight") -> Any:
    global _langfuse_client, _initialized

    if _initialized:
        return _langfuse_client

    auth = base64.b64encode(
        f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()
    ).decode()

    # Set OTLP env vars for any auto-instrumentation (OpenInference etc.)
    # The Langfuse SDK v4 creates its own OTLP exporter, but these vars help
    # other instrumentors (e.g. LlamaIndex, LangChain callbacks) also send
    # traces to the same Langfuse backend without their own config.
    otel_endpoint = f"{LANGFUSE_HOST.rstrip('/')}/api/public/otel"
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", otel_endpoint)
    os.environ.setdefault(
        "OTEL_EXPORTER_OTLP_HEADERS",
        f"Authorization=Basic {auth}",
    )

    from langfuse import Langfuse

    # Langfuse client: sends traces/spans/scores via OTLP or direct REST.
    # should_export_span filters out Langfuse's own internal spans to avoid noise.
    _langfuse_client = Langfuse(
        public_key=LANGFUSE_PUBLIC_KEY,
        secret_key=LANGFUSE_SECRET_KEY,
        base_url=LANGFUSE_HOST,
        should_export_span=is_default_export_span,
        additional_headers={
            "x-langfuse-ingestion-version": "4",
        },
    )

    _initialized = True
    logger.info("Langfuse initialized for %s (%s)", service_name, LANGFUSE_HOST)
    return _langfuse_client


def get_langfuse_client() -> Any:
    """Return existing client or lazily initialise on first access."""
    if not _initialized:
        return init_langfuse()
    return _langfuse_client


def flush_langfuse() -> None:
    """Flush pending traces to Langfuse.  Call after a batch of scores/spans."""
    if _initialized and _langfuse_client:
        _langfuse_client.flush()


def shutdown_langfuse() -> None:
    """Flush and shut down the Langfuse client.  Call once on application exit."""
    if _initialized and _langfuse_client:
        _langfuse_client.flush()
        _langfuse_client.shutdown()


_instrumented: set[str] = set()


def init_instrumentation(agent_type: str) -> None:
    """Lazily instrument the current process for the given agent type.

    Called once per process from each server's startup path.  All imports are
    deferred so that importing a server module in pytest does not trigger OTel
    side-effects (e.g. OTLP exporter connections or span processor threads).
    """
    if agent_type in _instrumented:
        return
    _instrumented.add(agent_type)
    if agent_type == "orchestrator":
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        HTTPXClientInstrumentor().instrument()
        GoogleADKInstrumentor().instrument()
    elif agent_type == "rag":
        from openinference.instrumentation.llama_index import LlamaIndexInstrumentor
        from opentelemetry.instrumentation.starlette import StarletteInstrumentor
        LlamaIndexInstrumentor().instrument()
        StarletteInstrumentor().instrument()
    elif agent_type == "quant":
        from opentelemetry.instrumentation.starlette import StarletteInstrumentor
        StarletteInstrumentor().instrument()
        try:
            from openinference.instrumentation.langchain import LangChainInstrumentor
            LangChainInstrumentor().instrument()
        except ImportError:
            pass
    elif agent_type in ("market_context", "sentiment"):
        # "sentiment" retained as a back-compat key for the renamed Market Context agent
        from openinference.instrumentation.crewai import CrewAIInstrumentor
        from opentelemetry.instrumentation.starlette import StarletteInstrumentor
        CrewAIInstrumentor().instrument(skip_dep_check=True)
        StarletteInstrumentor().instrument()
