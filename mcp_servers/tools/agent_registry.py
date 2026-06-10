"""Agent Registry MCP tools: find_agent, get_agent_cards, get_agent_card.

Semantic search over agent card JSON files using SentenceTransformer embeddings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
from langfuse import observe
from sentence_transformers import SentenceTransformer

from mcp_servers._app import app
from mcp_servers.infra.embed import get_embed_model

logger = logging.getLogger(__name__)

# Agent cards live in the parent project root, not inside mcp_servers/, to keep
# them editable by non-engineers who may not know the server layout.
AGENT_CARDS_DIR = Path(__file__).resolve().parent.parent.parent / "agent_cards"

# Injected at server startup by finsight_server.py from _settings.embed_model
EMBED_MODEL_NAME: str = "all-MiniLM-L6-v2"

# ──────────────────────────────────────────────
# Lazy Agent Registry (no model download at import time)
# ──────────────────────────────────────────────

# Lazy initialisation: SentenceTransformer downloads ~80 MB on first call.
# We defer everything until the first tool invocation so import time stays fast
# and servers that never call find_agent don't pay the model download cost.
_registry_lock = asyncio.Lock()
_registry_ready = False
_model_embed: SentenceTransformer | None = None
_df_registry: pd.DataFrame = pd.DataFrame(
    columns=["card_uri", "agent_card", "card_embeddings"]
)


def _load_agent_cards() -> tuple[list[str], list[dict]]:
    """Load agent card JSON files from AGENT_CARDS_DIR synchronously (called once).

    Runs inside run_in_executor so the GIL doesn't block the event loop during
    file I/O, even though the work is trivially fast for typical card counts.
    """
    card_uris, agent_cards = [], []
    if not AGENT_CARDS_DIR.is_dir():
        logger.warning("Agent cards directory not found: %s", AGENT_CARDS_DIR)
        return card_uris, agent_cards
    for filename in sorted(os.listdir(AGENT_CARDS_DIR)):
        if filename.lower().endswith(".json"):
            file_path = AGENT_CARDS_DIR / filename
            if file_path.is_file():
                try:
                    with file_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    stem = Path(filename).stem
                    card_uris.append(f"resource://agent_cards/{stem}")
                    agent_cards.append(data)
                except Exception as exc:
                    logger.error("Error loading %s: %s", filename, exc)
    logger.info("Loaded %d agent cards", len(agent_cards))
    return card_uris, agent_cards


async def _ensure_registry() -> None:
    """Initialise the embedding model and registry DataFrame on first use."""
    global _registry_ready, _model_embed, _df_registry
    # Fast path: avoid lock acquisition when already initialised.
    if _registry_ready:
        return
    # Double-checked locking: only one coroutine downloads the model.
    async with _registry_lock:
        if _registry_ready:
            return
        loop = asyncio.get_running_loop()

        def _init():
            card_uris, agent_cards = _load_agent_cards()
            if not agent_cards:
                return None, pd.DataFrame(
                    columns=["card_uri", "agent_card", "card_embeddings"]
                )
            # SentenceTransformer downloads ~80 MB on first call — delay until needed.
            model = get_embed_model(EMBED_MODEL_NAME)
            df = pd.DataFrame({"card_uri": card_uris, "agent_card": agent_cards})
            df["card_embeddings"] = df["agent_card"].apply(
                lambda c: model.encode(json.dumps(c))
            )
            return model, df

        _model_embed, _df_registry = await loop.run_in_executor(None, _init)
        _registry_ready = True


# ──────────────────────────────────────────────
# Agent Registry Tools
# ──────────────────────────────────────────────

@app.tool(
    name="find_agent",
    description="Finds the most relevant agent card based on a natural language query string",
)
@observe()
async def find_agent(query: str) -> str:
    """Semantic search over agent cards: embed query, return highest cosine-similarity card."""
    await _ensure_registry()
    if _df_registry.empty or _model_embed is None:
        return json.dumps({"error": "No agent cards loaded"})
    loop = asyncio.get_running_loop()

    def _search():
        q_emb = _model_embed.encode(query)
        dots = np.dot(np.stack(_df_registry["card_embeddings"]), q_emb)
        return int(np.argmax(dots))

    best_idx = await loop.run_in_executor(None, _search)
    return json.dumps(_df_registry.iloc[best_idx]["agent_card"])


@app.resource("resource://agent_cards/list", mime_type="application/json")
async def get_agent_cards() -> dict:
    """List all available agent card URIs as a JSON resource."""
    await _ensure_registry()
    return {
        "agent_cards": _df_registry["card_uri"].to_list()
        if not _df_registry.empty
        else []
    }


@app.resource("resource://agent_cards/{card_name}", mime_type="application/json")
async def get_agent_card(card_name: str) -> dict:
    """Retrieve a single agent card by name (e.g. resource://agent_cards/analyst)."""
    await _ensure_registry()
    if _df_registry.empty:
        return {"agent_card": None}
    uri = f"resource://agent_cards/{card_name}"
    cards = _df_registry.loc[
        _df_registry["card_uri"] == uri, "agent_card"
    ].to_list()
    return {"agent_card": cards[0]} if cards else {"agent_card": None}
