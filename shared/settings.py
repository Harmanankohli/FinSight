"""Pydantic-settings configuration — single source of truth for all env vars.

Supersedes the flat-constant approach in shared/config.py (which is now a shim).
New code should import from here; shim removed in WP 3.5.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import timezone, timedelta
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# IST timezone — not env-controlled; exported here for central import
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"

    # ── LLM ───────────────────────────────────────────────────────────────
    llm_model: str = "qwen/qwen3-30b-a3b-2507"
    llm_base_url: str = "http://localhost:1234/v1"
    llm_api_key: str = "lmstudio"
    adk_model: str = "openai/qwen/qwen3-30b-a3b-2507"
    llm_summary_model: str = ""  # falls back to llm_model via validator
    llm_eval_model: str = ""     # falls back to llm_model via validator
    llm_max_concurrent: int = 2

    # ── Embedding ─────────────────────────────────────────────────────────
    embed_model: str = "all-MiniLM-L6-v2"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # ── Host / ports ──────────────────────────────────────────────────────
    host: str = "localhost"
    orchestrator_port: int = 8001
    agent_port_rag: int = 8002
    agent_port_quant: int = 8003
    agent_port_market: int = 8004
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8010          # env: MCP_PORT (finsight_server)
    mcp_server_port: int = 8010   # env: MCP_SERVER_PORT (clients)
    mcp_server_url: str = "http://localhost:8010/sse"
    agent_registry_url: str = "http://localhost:8010"

    # ── Agent discovery ───────────────────────────────────────────────────
    agent_seed_urls: str = "http://localhost:8002,http://localhost:8003,http://localhost:8004"

    # ── MCP / A2A timeouts ────────────────────────────────────────────────
    mcp_timeout: float = 30.0
    mcp_max_retries: int = 3
    a2a_timeout: float = 680.0
    a2a_timeout_rag: float = 600.0
    a2a_timeout_quant: float = 600.0
    a2a_timeout_market_context: float = 600.0  # also accepts A2A_TIMEOUT_SENTIMENT

    # ── Storage ───────────────────────────────────────────────────────────
    chroma_dir: str = "./db/chroma_db"
    finsight_db_path: str = "./db/finsight_memory.db"  # G2
    redis_url: str = ""
    memory_retention_days: int = 90

    # ── Observability ─────────────────────────────────────────────────────
    langfuse_public_key: Optional[str] = None   # None → Langfuse disabled
    langfuse_secret_key: Optional[str] = None
    langfuse_host: str = "https://cloud.langfuse.com"  # also accepts LANGFUSE_BASE_URL

    # ── SEC EDGAR ─────────────────────────────────────────────────────────
    sec_api_base: str = "https://www.sec.gov"
    sec_user_agent: str = "FinSight Research (dev-mode-set-SEC_USER_AGENT)"

    # ── Eval ──────────────────────────────────────────────────────────────
    eval_trace_enabled: bool = True    # env: EVAL_TRACE_ENABLED
    eval_runtime_disabled: bool = False
    eval_burst_limit: int = 30
    eval_metric_timeout: float = 90.0

    # ── HuggingFace ───────────────────────────────────────────────────────
    hf_hub_offline: bool = True

    # ── Auth (WP 2.1) ─────────────────────────────────────────────────────
    auth_enabled: bool = False
    auth_jwt_secrets: str = ""         # comma-separated; first key signs
    auth_access_ttl_seconds: int = 900
    auth_refresh_ttl_seconds: int = 1_209_600
    service_auth_token: str = ""
    login_max_attempts: int = 5
    trusted_proxies: str = ""          # comma-separated IPs; X-Forwarded-For only trusted from these

    # ── Sandbox ───────────────────────────────────────────────────────────
    sandbox_mode: str = "ast"

    # ── Reports ───────────────────────────────────────────────────────────
    report_placeholder_policy: str = "label"
    reports_offline: bool = False

    @model_validator(mode="after")
    def _resolve_back_compat(self) -> "Settings":
        # A2A_TIMEOUT_SENTINEL → a2a_timeout_market_context (old env var name)
        if (
            "A2A_TIMEOUT_MARKET_CONTEXT" not in os.environ
            and "A2A_TIMEOUT_SENTIMENT" in os.environ
        ):
            try:
                object.__setattr__(
                    self,
                    "a2a_timeout_market_context",
                    float(os.environ["A2A_TIMEOUT_SENTIMENT"]),
                )
            except (ValueError, TypeError):
                pass
        # LANGFUSE_BASE_URL is an alias for LANGFUSE_HOST
        langfuse_base = os.environ.get("LANGFUSE_BASE_URL")
        if langfuse_base:
            object.__setattr__(self, "langfuse_host", langfuse_base)
        # llm_summary_model / llm_eval_model fall back to llm_model when unset
        if not self.llm_summary_model:
            object.__setattr__(self, "llm_summary_model", self.llm_model)
        if not self.llm_eval_model:
            object.__setattr__(self, "llm_eval_model", self.llm_model)
        return self

    def validate_runtime(self) -> None:
        """Raise EnvironmentError if config is invalid for the current env.

        Called by bootstrap(); runs in every env when auth_enabled=true (E4).
        """
        problems: list[str] = []
        if self.auth_enabled:
            signing = self.auth_jwt_secrets.split(",")[0] if self.auth_jwt_secrets else ""
            if len(signing) < 32:
                problems.append(
                    "AUTH_ENABLED=true requires AUTH_JWT_SECRETS (first key >= 32 chars)"
                )
            if len(self.service_auth_token) < 16:
                problems.append(
                    "AUTH_ENABLED=true requires SERVICE_AUTH_TOKEN (>= 16 chars)"
                )
        if self.env == "production":
            if not self.auth_enabled:
                problems.append("ENV=production requires AUTH_ENABLED=true")
            if "dev-mode" in self.sec_user_agent:
                problems.append("SEC_USER_AGENT placeholder in production")
            if self.sandbox_mode == "ast" and sys.platform == "win32":
                problems.append("SANDBOX_MODE=ast is insecure on Windows (no resource limits); set SANDBOX_MODE=disabled or use container")
            if self.sandbox_mode == "container" and shutil.which("docker") is None:
                problems.append("SANDBOX_MODE=container requires Docker to be installed and running")
        if problems:
            raise EnvironmentError(
                "FinSight config errors:\n" + "\n".join(f"  - {p}" for p in problems)
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    """Clear the settings singleton so the next call to get_settings() re-reads env."""
    global _settings
    _settings = None


# ── Module-level convenience constants (migrated from shared/config.py) ──
# These snapshot at first import, matching the old behavior. New code should
# use get_settings() for dynamic access.
_s = get_settings()

LLM_MODEL = _s.llm_model
LLM_BASE_URL = _s.llm_base_url
LLM_API_KEY = _s.llm_api_key
ADK_MODEL = _s.adk_model
LLM_SUMMARY_MODEL = _s.llm_summary_model
LLM_EVAL_MODEL = _s.llm_eval_model
LLM_MAX_CONCURRENT = _s.llm_max_concurrent
EMBED_MODEL = _s.embed_model
RERANKER_MODEL = _s.reranker_model
HOST = _s.host
AGENT_SEED_URLS = _s.agent_seed_urls
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
LANGFUSE_PUBLIC_KEY = _s.langfuse_public_key or "pk-lf-..."
LANGFUSE_SECRET_KEY = _s.langfuse_secret_key or "sk-lf-..."
LANGFUSE_HOST = _s.langfuse_host
SEC_API_BASE = _s.sec_api_base
SEC_USER_AGENT = _s.sec_user_agent
REDIS_URL = _s.redis_url
EVAL_ENABLED = _s.eval_trace_enabled
EVAL_RUNTIME_DISABLED = _s.eval_runtime_disabled
EVAL_BURST_LIMIT = _s.eval_burst_limit
EVAL_METRIC_TIMEOUT = _s.eval_metric_timeout
