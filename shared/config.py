"""Configuration shim — re-exports Settings fields as module-level constants.

This file exists for backward compatibility. Imports snapshot values at first
import (same behaviour as before). New code should import from shared.settings.
Removal scheduled for WP 3.5.
"""
from shared.settings import get_settings, IST  # noqa: F401 — IST re-exported

_s = get_settings()

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_MODEL = _s.llm_model
LLM_BASE_URL = _s.llm_base_url
LLM_API_KEY = _s.llm_api_key
ADK_MODEL = _s.adk_model
LLM_SUMMARY_MODEL = _s.llm_summary_model
LLM_EVAL_MODEL = _s.llm_eval_model
LLM_MAX_CONCURRENT = _s.llm_max_concurrent

# ── Embedding ─────────────────────────────────────────────────────────────────
EMBED_MODEL = _s.embed_model
RERANKER_MODEL = _s.reranker_model

# ── Host ──────────────────────────────────────────────────────────────────────
HOST = _s.host

# ── Agent discovery ───────────────────────────────────────────────────────────
AGENT_SEED_URLS = _s.agent_seed_urls

# ── MCP / A2A timeouts ────────────────────────────────────────────────────────
MCP_TIMEOUT = _s.mcp_timeout
MCP_MAX_RETRIES = _s.mcp_max_retries
A2A_TIMEOUT = _s.a2a_timeout
A2A_TIMEOUT_RAG = _s.a2a_timeout_rag
A2A_TIMEOUT_QUANT = _s.a2a_timeout_quant
A2A_TIMEOUT_MARKET_CONTEXT = _s.a2a_timeout_market_context
CHROMA_DIR = _s.chroma_dir
MCP_SERVER_URL = _s.mcp_server_url
MCP_SERVER_PORT = _s.mcp_server_port
AGENT_REGISTRY_URL = _s.agent_registry_url

# ── Langfuse ──────────────────────────────────────────────────────────────────
# Preserve placeholder strings so downstream callers that check for "pk-lf-..."
# continue to work until WP 3.5 removes this shim.
LANGFUSE_PUBLIC_KEY = _s.langfuse_public_key or "pk-lf-..."
LANGFUSE_SECRET_KEY = _s.langfuse_secret_key or "sk-lf-..."
LANGFUSE_HOST = _s.langfuse_host

# ── SEC EDGAR ─────────────────────────────────────────────────────────────────
SEC_API_BASE = _s.sec_api_base
SEC_USER_AGENT = _s.sec_user_agent

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = _s.redis_url

# ── Feature flags ─────────────────────────────────────────────────────────────
EVAL_ENABLED = _s.eval_trace_enabled
EVAL_RUNTIME_DISABLED = _s.eval_runtime_disabled
EVAL_BURST_LIMIT = _s.eval_burst_limit
EVAL_METRIC_TIMEOUT = _s.eval_metric_timeout


def validate() -> None:
    """Deprecated: use Settings.validate_runtime() via bootstrap() instead."""
    import logging
    logger = logging.getLogger(__name__)
    if LANGFUSE_PUBLIC_KEY in ("pk-lf-...", "", None):
        logger.warning(
            "LANGFUSE_PUBLIC_KEY is a placeholder — Langfuse traces will not be recorded"
        )
    if "dev-mode" in SEC_USER_AGENT:
        logger.warning(
            "SEC_USER_AGENT is placeholder — SEC may rate-limit or block. "
            "Set SEC_USER_AGENT='Your Name (your-email@example.com)' in .env"
        )
