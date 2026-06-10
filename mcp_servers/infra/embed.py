"""Lazy SentenceTransformer loader.

Defers the ~80 MB model download until first use so servers that never call
agent-card tools don't pay the download cost at import time.
"""

from __future__ import annotations

from sentence_transformers import SentenceTransformer

_EMBED_MODEL: SentenceTransformer | None = None


def get_embed_model(model_name: str) -> SentenceTransformer:
    """Return (and cache) the singleton SentenceTransformer instance."""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        _EMBED_MODEL = SentenceTransformer(model_name)
    return _EMBED_MODEL
