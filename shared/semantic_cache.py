"""Semantic query cache backed by ChromaDB + sentence-transformers.

Stores query embeddings in a dedicated Chroma collection. On lookup, finds
the nearest stored query by cosine similarity; if the similarity exceeds the
threshold and the entry is within the TTL, returns the cached response.

Cache keys are date-scoped (YYYY-MM-DD): an identical query on a different
day will miss the cache so analyses always reflect the current trading day.

Usage::

    cache = SemanticCache()
    cached = cache.get("analyze Nvidia")
    if cached:
        return cached
    result = run_pipeline(query)
    cache.set(query, result)
"""

from __future__ import annotations

import logging
import time
from datetime import date
from uuid import uuid4

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "query_cache"
_DEFAULT_THRESHOLD = 0.95
_DEFAULT_TTL_HOURS = 1


def _today_key() -> str:
    return date.today().isoformat()


class SemanticCache:
    def __init__(
        self,
        threshold: float = _DEFAULT_THRESHOLD,
        ttl_hours: float = _DEFAULT_TTL_HOURS,
    ):
        self._threshold = threshold
        self._ttl = ttl_hours * 3600
        self._col = None
        self._embedder = None
        self._init_started = False

    # Lazy initialisation: Chroma client + embedding model are created on first
    # use, not at construction. This avoids import-time failures when deps are
    # unavailable and keeps cache instantiation cheap for non-cached queries.
    def _ensure_ready(self) -> None:
        if self._col is not None:
            return
        if self._init_started:
            return
        self._init_started = True
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer

            from shared.settings import CHROMA_DIR, EMBED_MODEL

            client = chromadb.PersistentClient(path=CHROMA_DIR)
            self._col = client.get_or_create_collection(
                _COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            self._embedder = SentenceTransformer(f"sentence-transformers/{EMBED_MODEL}")
        except Exception as exc:
            logger.warning("SemanticCache unavailable: %s", exc)

    # Cosine-similarity check against today's cached queries only. Returns the
    # cached response if the nearest neighbour exceeds the threshold AND its
    # timestamp is within TTL. Cross-day matches are filtered out via the
    # `date` metadata so identical queries on different days don't collide.
    def get(self, query: str, *, user_id: str | None = None) -> str | None:
        self._ensure_ready()
        if self._col is None or self._embedder is None:
            return None
        try:
            emb = self._embedder.encode(query).tolist()
            _where: dict = {"date": _today_key()}
            if user_id:
                _where["user_id"] = user_id
            results = self._col.query(
                query_embeddings=[emb],
                n_results=1,
                where=_where,
                include=["metadatas", "distances"],
            )
            distances = results.get("distances", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            if not distances or not metadatas:
                return None
            # Chroma cosine space: distance = 1 - similarity
            similarity = 1.0 - distances[0]
            if similarity >= self._threshold:
                meta = metadatas[0]
                if time.time() - float(meta.get("ts", 0)) < self._ttl:
                    logger.debug(
                        "Semantic cache hit (sim=%.3f) for query: %s", similarity, query[:60]
                    )
                    return meta.get("response")
        except Exception as exc:
            logger.warning("SemanticCache.get failed: %s", exc)
        return None

    # Stores query + response embedding with today's date and a timestamp.
    # Response is truncated to 4000 chars to keep Chroma metadata size bounded.
    def set(self, query: str, response: str, *, user_id: str | None = None) -> None:
        self._ensure_ready()
        if self._col is None or self._embedder is None:
            return
        try:
            emb = self._embedder.encode(query).tolist()
            self._col.add(
                embeddings=[emb],
                documents=[query],
                metadatas=[
                    {
                        "response": response[:4000],
                        "ts": str(time.time()),
                        "date": _today_key(),
                        "user_id": user_id or "__system__",
                    }
                ],
                ids=[str(uuid4())],
            )
        except Exception as exc:
            logger.warning("SemanticCache.set failed: %s", exc)
