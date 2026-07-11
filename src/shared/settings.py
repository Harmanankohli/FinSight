"""Pydantic-settings configuration — single source of truth for all env vars.

Supersedes the flat-constant approach in shared/config.py (which is now a shim).
New code should import from here; shim removed in WP 3.5.

Every field has a default fallback.  At runtime pydantic-settings overlays:
  1. Actual OS environment variables (highest priority)
  2. .env file values
  3. Class defaults (lowest priority)
So the defaults here are only used when neither .env nor the OS env provides a value.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import timedelta, timezone

import pydantic
from pydantic import AliasChoices, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# IST timezone — not env-controlled; exported here for central import
# Used by all agents that need India-market timestamps (e.g. NSE/BSE hours).
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class Settings(BaseSettings):
    """Application-wide configuration loaded from .env + environment variables.

    Instantiate via ``get_settings()`` singleton rather than directly.
    Each field corresponds to one env var; the Python name uses snake_case
    and pydantic-settings auto-maps it to the uppercase env var name
    (e.g. ``llm_model`` ← ``LLM_MODEL``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deployment environment label — controls strictness of validate_runtime().
    # "development" = relaxed checks; "production" = enforces auth + hardened settings.
    env: str = "development"

    # ── LLM ───────────────────────────────────────────────────────────────
    # Primary chat-completion model used by all agents for analysis/reasoning.
    # Expects an OpenAI-compatible endpoint (LM Studio, vLLM, etc.).
    llm_model: str = "mistralai/ministral-3-14b-reasoning"
    # Base URL of the LLM server.  Default points to LM Studio's local port.
    llm_base_url: str = "http://localhost:1234/v1"
    # API key sent in the Authorization header.  LM Studio ignores it; real
    # providers (OpenAI, Together, etc.) require a valid key here.
    llm_api_key: str = "lmstudio"
    # Model identifier for the ADK (Agent Development Kit) framework.
    # Uses OpenAI-compatible routing format: ``provider/model-vendor/model-name``.
    adk_model: str = "openai/mistralai/ministral-3-14b-reasoning"
    # Separate model for summary synthesis (e.g. brief-generation, report
    # condensation).  When empty the _resolve_back_compat validator copies
    # llm_model at init time.
    llm_summary_model: str = ""
    # Separate model for RAGAS evaluation calls (LLM-as-judge).  When empty
    # falls back to llm_model via the same validator.
    llm_eval_model: str = ""
    # Max parallel LLM requests per process.  Keeps the endpoint from being
    # overwhelmed by concurrent agent calls.
    llm_max_concurrent: int = 2

    # ── Embedding ─────────────────────────────────────────────────────────
    # Sentence-transformer model for text → vector (ChromaDB indexing, semantic
    # search).  Downloaded from HuggingFace Hub on first use.
    embed_model: str = "all-MiniLM-L6-v2"
    # Cross-encoder model for re-ranking retrieved chunks after the initial
    # embedding similarity search.  Improves RAG precision.
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # ── Host / ports ──────────────────────────────────────────────────────
    # Bind address for all agent HTTP servers.  Use "0.0.0.0" for containerised
    # deployments where the server must accept connections on all interfaces.
    host: str = "localhost"
    # Port the orchestrator FastAPI app listens on.
    orchestrator_port: int = 8001
    # Per-agent HTTP ports for the A2A (Agent-to-Agent) protocol.
    agent_port_rag: int = 8002       # Financial RAG agent
    agent_port_quant: int = 8003     # Quantitative analysis agent
    agent_port_market: int = 8004    # Market context agent
    agent_port_analytics: int = 8005 # Analytics agent
    agent_port_reviewer: int = 8006  # Reviewer/synthesis agent
    # MCP (Model Context Protocol) server bind address.  Separate from
    # host because MCP is an internal bus, not a public-facing API.
    mcp_host: str = "127.0.0.1"
    # Port the MCP ``finsight_server`` process listens on (for agent→MCP comms).
    mcp_port: int = 8010
    # Port that MCP *clients* (agents, orchestrator) connect to.  In most
    # setups this is the same as mcp_port; split when a proxy is involved.
    mcp_server_port: int = 8010
    # Full SSE endpoint URL for MCP client connections.
    mcp_server_url: str = "http://localhost:8010/sse"
    # Base URL of the agent registry (also served by the MCP server).
    # Used for agent card lookups and dynamic discovery.
    agent_registry_url: str = "http://localhost:8010"

    # ── Agent discovery ───────────────────────────────────────────────────
    # Comma-separated list of agent endpoints the orchestrator pings on
    # startup to register them.  Each agent independently registers itself
    # via the MCP server; these URLs serve as a static fallback seed list.
    agent_seed_urls: str = "http://localhost:8002,http://localhost:8003,http://localhost:8004,http://localhost:8005,http://localhost:8006"

    # ── MCP / A2A timeouts ────────────────────────────────────────────────
    # Max seconds to wait for an MCP tool-call response before raising
    # a timeout error.  Short for lightweight queries; long-running tools
    # (heavy analysis) should use A2A timeouts instead.
    mcp_timeout: float = 30.0
    # Number of retries for transient MCP failures (network blips, server
    # momentarily busy).  Total wait = retries × mcp_timeout.
    mcp_max_retries: int = 3
    # Default A2A request timeout (seconds).  Used when no per-agent
    # override is set.  300s = 5 minutes.
    a2a_timeout: float = 300.0
    # Per-agent A2A timeouts.  RAG/quant/analytics need longer because
    # they may make multiple LLM calls, run Monte Carlo simulations, or
    # fetch external documents.  Reviewer is faster — it just synthesises
    # already-collected results.
    a2a_timeout_rag: float = 600.0           # 10 min — document retrieval + synthesis
    a2a_timeout_quant: float = 600.0         # 10 min — technical analysis + Monte Carlo
    a2a_timeout_market_context: float = 600.0 # 10 min — multi-source sentiment + news
    a2a_timeout_analytics: float = 600.0     # 10 min — peer comps + statistics
    a2a_timeout_reviewer: float = 300.0      #  5 min — synthesis + quality scoring
    # Legacy env var fallback: when A2A_TIMEOUT_MARKET_CONTEXT is not set but
    # A2A_TIMEOUT_SENTIMENT is, the _resolve_back_compat validator copies it.

    # ── Storage ───────────────────────────────────────────────────────────
    # Directory for ChromaDB persistent vector store (embeddings + metadata).
    chroma_dir: str = "./db/chroma_db"
    # SQLite database path for the FinSight memory store (session state,
    # agent response cache, brief_json snapshots).
    finsight_db_path: str = "./db/finsight_memory.db"
    # Redis connection string (optional).  When set, used for cross-worker
    # MCP state sharing and eval-trace deduplication.  Leave empty for
    # single-process / in-process mode.
    redis_url: str = ""
    # Number of days before FinSight memory records (sessions, traces) are
    # pruned on orchestrator startup.  Shorter = less disk usage, longer
    # = more historical context for the reviewer agent.
    memory_retention_days: int = 90

    # ── Observability ─────────────────────────────────────────────────────
    # Langfuse public key.  Set to None (or leave default) to disable
    # Langfuse tracing entirely.  Set to a valid key to enable LLM
    # observability (token counts, latency, trace timelines).
    langfuse_public_key: str | None = None
    # Langfuse secret key.  Must be set when public_key is non-None.
    langfuse_secret_key: str | None = None
    # Langfuse server hostname.  Accepts both LANGFUSE_HOST and
    # LANGFUSE_BASE_URL env vars for backward compatibility with different
    # Langfuse deployment flavours (cloud vs self-hosted).
    langfuse_host: str = pydantic.Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    )

    # ── SEC EDGAR ─────────────────────────────────────────────────────────
    # Base URL for SEC EDGAR filings API.  Change to a proxy/mirror if
    # running in a region with restricted SEC access.
    sec_api_base: str = "https://www.sec.gov"
    # User-Agent header sent with every SEC request.  SEC requires a
    # meaningful UA (email preferred) to avoid rate-limiting.  Replace
    # the dev-mode placeholder with your contact email in production.
    sec_user_agent: str = "FinSight Research (dev-mode-set-SEC_USER_AGENT)"

    # ── Eval ──────────────────────────────────────────────────────────────
    # Master switch for RAGAS evaluation tracing.  When True, every LLM
    # call that goes through the eval wrapper has its input/output/scores
    # written to disk as JSON traces.  Set False to skip all eval overhead.
    eval_trace_enabled: bool = True
    # Runtime kill switch — when True, RAGAS scoring is bypassed without
    # requiring a restart.  Useful for hot-fixing a stuck eval pipeline
    # in production while keeping the service up.
    eval_runtime_disabled: bool = False
    # Per-minute rate limit for eval metric calls per process.  Prevents
    # a burst of slow LLM-as-judge evaluations from saturating the model
    # endpoint.  0 = unlimited.
    eval_burst_limit: int = 30
    # Per-metric deadline (seconds).  If a single RAGAS metric call (e.g.
    # answer_relevancy) hangs or takes too long, it is killed so it can't
    # pin the eval worker pool indefinitely.
    eval_metric_timeout: float = 90.0
    # Max seconds a deferred eval task can sit in the background queue
    # before being dropped.  Prevents queue build-up when the eval worker
    # is slower than the request rate.
    eval_defer_timeout: float = 120.0

    # ── HuggingFace ───────────────────────────────────────────────────────
    # When True, disables HF Hub update checks (no network calls to
    # huggingface.co at import time).  Set False when downloading a model
    # for the first time to allow the hub handshake.
    hf_hub_offline: bool = True

    # ── Auth (WP 2.1) ─────────────────────────────────────────────────────
    # Master toggle for JWT-based API authentication.  Disabled by default
    # for local dev; must be enabled in production.
    auth_enabled: bool = False
    # Comma-separated list of HMAC-SHA256 signing keys.  The first key is
    # used for signing new tokens; all keys are accepted for verification
    # (enables key rotation).  Each key must be >= 32 characters.
    auth_jwt_secrets: str = ""
    # Access token TTL in seconds.  900s = 15 minutes.  Short-lived to
    # limit exposure if a token leaks.
    auth_access_ttl_seconds: int = 900
    # Refresh token TTL in seconds.  1_209_600 = 14 days.  Long-lived so
    # users don't need to re-authenticate frequently; rotated on each use.
    auth_refresh_ttl_seconds: int = 1_209_600
    # Shared secret used for service-to-service (agent→agent) authentication.
    # Must be >= 16 characters when auth is enabled.
    service_auth_token: str = ""
    # Max failed login attempts per IP before temporary lockout.
    login_max_attempts: int = 5
    # Comma-separated list of trusted reverse-proxy IPs (e.g. Next.js
    # container, ingress).  X-Forwarded-For header is only honoured when
    # the direct peer matches this list.  Leave empty for direct client
    # connections.
    trusted_proxies: str = ""

    # ── Sandbox ───────────────────────────────────────────────────────────
    # Code-execution sandbox mode for untrusted LLM-generated code:
    #   "ast"      — ast.literal_eval only (fast, but no resource limits on Windows)
    #   "container" — Docker-based full sandbox (requires Docker daemon)
    #   "disabled" — no sandboxing (run arbitrary code; USE WITH CAUTION)
    sandbox_mode: str = "ast"

    # ── CORS ──────────────────────────────────────────────────────────────
    # Comma-separated list of allowed CORS origins.  Default allows the
    # Next.js dev server on its two common local addresses.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # ── Reports ───────────────────────────────────────────────────────────
    # How to render placeholders in generated reports when real data is
    # unavailable: "label" shows a descriptive label, "skip" omits the
    # section, "error" raises a visible warning.
    report_placeholder_policy: str = "label"
    # When True, the report generation endpoint returns immediately with a
    # stub response instead of running the full agent pipeline.  Used in
    # CI/demo environments that don't need real reports.
    reports_offline: bool = False

    @model_validator(mode="after")
    def _resolve_back_compat(self) -> Settings:
        # Pydantic v2 models are frozen by default after validation.
        # object.__setattr__ bypasses the frozen-model guard so we can
        # backfill deprecated / missing fields post-construction.  Safe
        # because this validator runs exactly once at init, before the
        # instance is shared with any consumer.

        # Migration: A2A_TIMEOUT_SENTIMENT was renamed to
        # A2A_TIMEOUT_MARKET_CONTEXT.  If only the old name exists in
        # the OS environment, copy its value over so existing .env files
        # don't break.
        if "A2A_TIMEOUT_MARKET_CONTEXT" not in os.environ and "A2A_TIMEOUT_SENTIMENT" in os.environ:
            try:
                object.__setattr__(
                    self,
                    "a2a_timeout_market_context",
                    float(os.environ["A2A_TIMEOUT_SENTIMENT"]),
                )
            except (ValueError, TypeError):
                pass
        # Summary and eval models default to the primary llm_model when
        # not explicitly configured.  Lets users set only LLM_MODEL and
        # get reasonable behaviour without duplication.
        if not self.llm_summary_model:
            object.__setattr__(self, "llm_summary_model", self.llm_model)
        if not self.llm_eval_model:
            object.__setattr__(self, "llm_eval_model", self.llm_model)
        return self

    def validate_runtime(self) -> None:
        """Check config for problems that would cause runtime failures.

        Collects every issue into a list, then raises ``OSError`` once with
        all messages joined — fail-fast, but shows every problem in one go so
        users don't have to fix, restart, and discover the next issue.

        Called by ``bootstrap()`` during service startup.

        Rules enforced:
        - Auth enabled  → signing key ≥ 32 chars, service token ≥ 16 chars.
        - Production    → auth mandatory, SEC UA not a placeholder,
                          sandbox-mode compatible with platform.
        """
        problems: list[str] = []
        if self.auth_enabled:
            signing = self.auth_jwt_secrets.split(",")[0] if self.auth_jwt_secrets else ""
            if len(signing) < 32:
                problems.append(
                    "AUTH_ENABLED=true requires AUTH_JWT_SECRETS (first key >= 32 chars)"
                )
            if len(self.service_auth_token) < 16:
                problems.append("AUTH_ENABLED=true requires SERVICE_AUTH_TOKEN (>= 16 chars)")
        if self.env == "production":
            if not self.auth_enabled:
                problems.append("ENV=production requires AUTH_ENABLED=true")
            if "dev-mode" in self.sec_user_agent:
                problems.append("SEC_USER_AGENT placeholder in production")
            if self.sandbox_mode == "ast" and sys.platform == "win32":
                problems.append(
                    "SANDBOX_MODE=ast is insecure on Windows (no resource limits); "
                    "set SANDBOX_MODE=disabled or use container"
                )
            if self.sandbox_mode == "container" and shutil.which("docker") is None:
                problems.append(
                    "SANDBOX_MODE=container requires Docker to be installed and running"
                )
        if problems:
            raise OSError(
                "FinSight config errors:\n" + "\n".join(f"  - {p}" for p in problems)
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Lazily constructs ``Settings()`` on first call (which reads .env + env
    vars).  Subsequent calls return the same cached instance, so every
    module in the process sees identical config.  Call
    ``reset_settings_for_tests()`` between test cases to force a re-read.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    """Clear the settings singleton.

    The next call to ``get_settings()`` will construct a fresh ``Settings()``
    instance, re-reading .env and environment variables.  Intended for test
    isolation — no other code should call this at runtime.
    """
    global _settings
    _settings = None


# ── Module-level convenience constants (migrated from shared/config.py) ──
# These snapshot at first import of this module, matching the old
# shared/config.py behaviour.  They are *static* — changes to env vars or
# .env after import are NOT reflected here.
#
# New code should use ``get_settings()`` for dynamic access, especially in
# long-running services where config may need to be reloaded or where
# test isolation matters.
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
A2A_TIMEOUT_ANALYTICS = _s.a2a_timeout_analytics
A2A_TIMEOUT_REVIEWER = _s.a2a_timeout_reviewer
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
