import os
from pathlib import Path

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

# LLM (LM Studio / OpenAI-compatible local)
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-oss-20b")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
ADK_MODEL = os.environ.get("ADK_MODEL", "openai/gpt-oss-20b")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

# Embedding
EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")

# Host
HOST = os.environ.get("HOST", "localhost")

# Agent URLs
RAG_AGENT_URL = os.environ.get("RAG_AGENT_URL", "http://localhost:8002")
QUANT_AGENT_URL = os.environ.get("QUANT_AGENT_URL", "http://localhost:8003")
SENTIMENT_AGENT_URL = os.environ.get("SENTIMENT_AGENT_URL", "http://localhost:8004")
AGENT_SEED_URLS = os.environ.get("AGENT_SEED_URLS", "http://localhost:8002,http://localhost:8003,http://localhost:8004")

# Ports
ORCHESTRATOR_PORT = int(os.environ.get("ORCHESTRATOR_PORT", "8001"))
RAG_PORT = int(os.environ.get("RAG_PORT", "8002"))
QUANT_PORT = int(os.environ.get("QUANT_PORT", "8003"))
SENTIMENT_PORT = int(os.environ.get("SENTIMENT_PORT", "8004"))

# MCP
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "30.0"))
MCP_MAX_RETRIES = int(os.environ.get("MCP_MAX_RETRIES", "3"))
A2A_TIMEOUT = float(os.environ.get("A2A_TIMEOUT", "180.0"))
CHROMA_DIR = os.environ.get("CHROMA_DIR", "./chroma_db")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8010/sse")
MCP_SERVER_PORT = int(os.environ.get("MCP_SERVER_PORT", "8010"))

# Agent Registry (MCP-based discovery, hosted on the unified finsight MCP server)
AGENT_REGISTRY_URL = os.environ.get("AGENT_REGISTRY_URL", "http://localhost:8010")

# Reddit
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")

# SEC
SEC_API_BASE = os.environ.get("SEC_API_BASE", "https://www.sec.gov")
