"""Lazy SentenceTransformer loader.

Defers the ~80 MB model download until first use so servers that never call
agent-card tools don't pay the download cost at import time.
"""

from __future__ import annotations

import logging

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_EMBED_MODEL: SentenceTransformer | None = None


def get_embed_model(model_name: str) -> SentenceTransformer:
    """Return (and cache) the singleton SentenceTransformer instance."""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        logger.info("Loading embedding model: %s", model_name)
        _EMBED_MODEL = SentenceTransformer(model_name)
        logger.info("Embedding model %s loaded", model_name)
    return _EMBED_MODEL
