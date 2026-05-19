import base64
import logging
import os
from typing import Any

from langfuse.span_filter import is_default_export_span
from shared.config import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

logger = logging.getLogger(__name__)

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
    # other instrumentors also send to Langfuse.
    otel_endpoint = f"{LANGFUSE_HOST.rstrip('/')}/api/public/otel"
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", otel_endpoint)
    os.environ.setdefault(
        "OTEL_EXPORTER_OTLP_HEADERS",
        f"Authorization=Basic {auth}",
    )

    from langfuse import Langfuse

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
    if not _initialized:
        return init_langfuse()
    return _langfuse_client


def flush_langfuse() -> None:
    if _initialized and _langfuse_client:
        _langfuse_client.flush()


def shutdown_langfuse() -> None:
    if _initialized and _langfuse_client:
        _langfuse_client.flush()
        _langfuse_client.shutdown()
