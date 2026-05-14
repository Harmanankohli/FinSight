import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

AGENT_CARDS_DIR = Path(__file__).resolve().parent.parent / "agent_cards"
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
HOST = os.environ.get("REGISTRY_HOST", "localhost")
PORT = int(os.environ.get("REGISTRY_PORT", "10200"))


def load_agent_cards():
    card_uris = []
    agent_cards = []
    if not AGENT_CARDS_DIR.is_dir():
        logger.error("Agent cards directory not found: %s", AGENT_CARDS_DIR)
        return card_uris, agent_cards

    logger.info("Loading agent cards from %s", AGENT_CARDS_DIR)
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
                except Exception as e:
                    logger.error("Error loading %s: %s", filename, e)

    logger.info("Loaded %d agent cards", len(agent_cards))
    return card_uris, agent_cards


def build_embeddings(df: pd.DataFrame, model: SentenceTransformer) -> pd.DataFrame:
    df["card_embeddings"] = df["agent_card"].apply(
        lambda card: model.encode(json.dumps(card))
    )
    return df


app = FastMCP("agent-registry", host=HOST, port=PORT)


@app.tool(
    name="find_agent",
    description="Finds the most relevant agent card based on a natural language query string",
)
def find_agent(query: str) -> str:
    query_embedding = model.encode(query)
    dot_products = np.dot(
        np.stack(df["card_embeddings"]), query_embedding
    )
    best_match_index = int(np.argmax(dot_products))
    best_score = float(dot_products[best_match_index])
    logger.debug("Best match index %d with score %.4f", best_match_index, best_score)
    return json.dumps(df.iloc[best_match_index]["agent_card"])


@app.resource("resource://agent_cards/list", mime_type="application/json")
def get_agent_cards() -> dict:
    return {"agent_cards": df["card_uri"].to_list()}


@app.resource("resource://agent_cards/{card_name}", mime_type="application/json")
def get_agent_card(card_name: str) -> dict:
    cards = df.loc[
        df["card_uri"] == f"resource://agent_cards/{card_name}",
        "agent_card",
    ].to_list()
    return {"agent_card": cards[0]} if cards else {"agent_card": None}


_card_uris, _agent_cards = load_agent_cards()
if _agent_cards:
    _model = SentenceTransformer(EMBED_MODEL_NAME)
    df = build_embeddings(
        pd.DataFrame({"card_uri": _card_uris, "agent_card": _agent_cards}),
        _model,
    )
else:
    _model = None
    df = pd.DataFrame(columns=["card_uri", "agent_card", "card_embeddings"])


def get_app():
    return app.sse_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import uvicorn
    uvicorn.run(get_app(), host=HOST, port=PORT)
