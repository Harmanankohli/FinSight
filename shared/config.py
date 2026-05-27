"""Central configuration — all env vars read at import time.

Defaults target local development with LM Studio (port 1234).
"""
import os
import sys
from datetime import timezone, timedelta
from pathlib import Path

# Indian Standard Time (UTC+5:30) — used throughout for timestamps
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Load .env from project root before any config reads
_dotenv_path = Path(__file__).resolve().parent.parent / ".env"
if _dotenv_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_dotenv_path)
    except ImportError:
        for line in _dotenv_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

# Skip HuggingFace Hub network checks — models are expected to be cached locally.
# Set HF_HUB_OFFLINE=0 in .env to re-enable update checks (e.g. to pull a new model).
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# Ensure stdout/stderr use UTF-8 so RAGAS log messages with Unicode characters
# (curly quotes, em-dashes) don't trigger UnicodeEncodeError on Windows (cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ── LLM (LM Studio / OpenAI-compatible local) ─────────────────────────────
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-oss-20b")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "lmstudio")
ADK_MODEL = os.environ.get("ADK_MODEL", "openai/gpt-oss-20b")

# ── Embedding ─────────────────────────────────────────────────────────────
EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")

# ── Host ──────────────────────────────────────────────────────────────────
HOST = os.environ.get("HOST", "localhost")

# ── Agent discovery (A2A sub-agent URLs, comma-separated) ────────────────
AGENT_SEED_URLS = os.environ.get("AGENT_SEED_URLS", "http://localhost:8002,http://localhost:8003,http://localhost:8004")

# ── MCP (Model Context Protocol) ─────────────────────────────────────────
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "30.0"))
MCP_MAX_RETRIES = int(os.environ.get("MCP_MAX_RETRIES", "3"))
A2A_TIMEOUT = float(os.environ.get("A2A_TIMEOUT", "180.0"))
A2A_TIMEOUT_RAG = float(os.environ.get("A2A_TIMEOUT_RAG", "60.0"))
A2A_TIMEOUT_QUANT = float(os.environ.get("A2A_TIMEOUT_QUANT", "90.0"))
A2A_TIMEOUT_SENTIMENT = float(os.environ.get("A2A_TIMEOUT_SENTIMENT", "45.0"))
CHROMA_DIR = os.environ.get("CHROMA_DIR", "./db/chroma_db")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8010/sse")
MCP_SERVER_PORT = int(os.environ.get("MCP_SERVER_PORT", "8010"))
AGENT_REGISTRY_URL = os.environ.get("AGENT_REGISTRY_URL", "http://localhost:8010")

# ── Langfuse (observability / tracing) ───────────────────────────────────
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-...")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-...")
LANGFUSE_HOST = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

# ── SEC EDGAR ─────────────────────────────────────────────────────────────
SEC_API_BASE = os.environ.get("SEC_API_BASE", "https://www.sec.gov")
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "FinSight Research (dev-mode-set-SEC_USER_AGENT)",
)

# ── Feature flags ─────────────────────────────────────────────────────────
EVAL_ENABLED = os.environ.get("EVAL_TRACE_ENABLED", "true").lower() == "true"


def validate() -> None:
    """Raise EnvironmentError if required configuration is missing or has placeholder values."""
    issues = []

    if not LLM_BASE_URL or LLM_BASE_URL == "http://localhost:1234/v1":
        pass  # local default is acceptable for dev

    if not MCP_SERVER_URL:
        issues.append("MCP_SERVER_URL is not set")

    if LANGFUSE_PUBLIC_KEY in ("pk-lf-...", "", None):
        import logging
        logging.getLogger(__name__).warning(
            "LANGFUSE_PUBLIC_KEY is a placeholder — Langfuse traces will not be recorded"
        )

    if "dev-mode" in SEC_USER_AGENT:
        import logging
        logging.getLogger(__name__).warning(
            "SEC_USER_AGENT is placeholder — SEC may rate-limit or block. "
            "Set SEC_USER_AGENT='Your Name (your-email@example.com)' in .env"
        )

    if issues:
        raise EnvironmentError(
            "FinSight configuration errors:\n" + "\n".join(f"  - {i}" for i in issues)
        )
