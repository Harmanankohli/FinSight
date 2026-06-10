"""Persistent MemoryService backed by SQLite with BM25 + embedding search.

Implements ADK's BaseMemoryService interface so it works natively with
the ADK Runner and the load_memory tool. Stores conversation events as
searchable memory entries in the same SQLite database as other memory stores.

Search uses hybrid BM25 keyword matching + sentence-transformer semantic
similarity, fused with Reciprocal Rank Fusion (RRF).
"""

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from google.adk.events import Event
from google.adk.memory import BaseMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions import Session
from google.genai import types
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer, util

from shared.settings import IST
from shared.memory.store import DB_PATH, get_db, write_lock

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class SQLiteMemoryService(BaseMemoryService):
    """Persistent memory service using SQLite with hybrid BM25 + embedding search."""

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._model: Optional[SentenceTransformer] = None

    # Lazy-loads SentenceTransformer on first search to avoid heavy import cost at service initialization.
    def _get_model(self) -> SentenceTransformer:
        """Lazy-load the embedding model."""
        if self._model is None:
            self._model = SentenceTransformer(EMBEDDING_MODEL)
        return self._model

    # Called after a session completes. Extracts all events and persists as searchable memory entries.
    async def add_session_to_memory(self, session: Session) -> None:
        """Store all events from a session as memory entries."""
        async with write_lock():
            conn = await get_db(self._db_path)
            for event in session.events:
                content_text = self._extract_text(event)
                if not content_text:
                    continue

                entry_id = str(uuid.uuid4())
                content_json = json.dumps(self._event_to_dict(event))
                metadata_json = json.dumps({
                    "app_name": session.app_name,
                    "author": event.author,
                })

                await conn.execute(
                    """INSERT INTO memory_entries
                       (id, user_id, session_id, content_json, metadata_json, search_text, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        session.user_id,
                        session.id,
                        content_json,
                        metadata_json,
                        content_text,
                        datetime.now(IST).isoformat(),
                    ),
                )
            await conn.commit()

    async def add_events_to_memory(
        self,
        *,
        app_name: Optional[str] = None,
        user_id: Optional[str] = None,
        events: list[Event] | None = None,
        session_id: Optional[str] = None,
        custom_metadata: Optional[dict] = None,
    ) -> None:
        """Add a delta of events to memory without re-ingesting the full session.

        Compatible with both ADK's Context.add_events_to_memory (events only)
        and direct calls with full parameters.
        """
        if not events:
            return

        # Extract context values from custom_metadata if not provided directly
        if custom_metadata:
            user_id = user_id or custom_metadata.get("user_id")
            session_id = session_id or custom_metadata.get("session_id")
            app_name = app_name or custom_metadata.get("app_name")

        async with write_lock():
            conn = await get_db(self._db_path)
            for event in events:
                content_text = self._extract_text(event)
                if not content_text:
                    continue

                entry_id = str(uuid.uuid4())
                content_json = json.dumps(self._event_to_dict(event))
                metadata_json = json.dumps({
                    "app_name": app_name or "finsight",
                    "author": event.author,
                    **(custom_metadata or {}),
                })

                await conn.execute(
                    """INSERT INTO memory_entries
                       (id, user_id, session_id, content_json, metadata_json, search_text, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        user_id or "default_user",
                        session_id or "",
                        content_json,
                        metadata_json,
                        content_text,
                        datetime.now(IST).isoformat(),
                    ),
                )
            await conn.commit()

    # Hybrid retrieval pipeline: BM25 keyword matching + embedding similarity fused via RRF (Reciprocal Rank Fusion). Returns top-10 ranked memories.
    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        """Search stored memories using hybrid BM25 + embedding similarity."""
        conn = await get_db(self._db_path)
        cursor = await conn.execute(
            """SELECT id, content_json, metadata_json, search_text, created_at
               FROM memory_entries
               WHERE user_id = ?
               ORDER BY created_at DESC""",
            (user_id,),
        )
        rows = await cursor.fetchall()

        if not rows:
            return SearchMemoryResponse(memories=[])

        # Parse entries
        parsed = []
        for row in rows:
            entry_id, content_json, metadata_json, search_text, created_at = row
            try:
                content_dict = json.loads(content_json)
                metadata = json.loads(metadata_json)
            except json.JSONDecodeError:
                continue

            # Filter by app_name
            if metadata.get("app_name") != app_name:
                continue

            parts = []
            for part_dict in content_dict.get("parts", []):
                if "text" in part_dict:
                    parts.append(types.Part(text=part_dict["text"]))

            if not parts:
                continue

            content = types.Content(
                role=content_dict.get("role", "user"),
                parts=parts,
            )

            entry = MemoryEntry(
                content=content,
                custom_metadata=metadata,
                id=entry_id,
                author=metadata.get("author"),
                timestamp=created_at,
            )

            parsed.append({
                "entry": entry,
                "search_text": search_text or self._content_to_text(content),
            })

        if not parsed:
            return SearchMemoryResponse(memories=[])

        # BM25 scoring
        bm25_scores = self._bm25_score(query, parsed)

        # Embedding scoring
        emb_scores = self._embedding_score(query, parsed)

        # Fuse with RRF
        fused = self._rrf_fuse(bm25_scores, emb_scores)

        # Sort by fused score descending
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)

        id_to_entry = {p["entry"].id: p["entry"] for p in parsed}
        memories = []
        for entry_id, _score in ranked[:10]:
            if entry_id in id_to_entry:
                memories.append(id_to_entry[entry_id])

        return SearchMemoryResponse(memories=memories)

    # Tokenizes query and corpus, scores entries with BM25Okapi (term-frequency × inverse document frequency).
    @staticmethod
    def _bm25_score(query: str, parsed: list[dict]) -> dict[str, float]:
        """Compute BM25 scores for all entries against the query."""
        query_tokens = SQLiteMemoryService._tokenize(query)
        if not query_tokens:
            return {}

        corpus = [SQLiteMemoryService._tokenize(p["search_text"]) for p in parsed]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(query_tokens)

        return {p["entry"].id: float(s) for p, s in zip(parsed, scores)}

    # Encodes query and all entry texts with sentence-transformers, computes pairwise cosine similarity.
    def _embedding_score(self, query: str, parsed: list[dict]) -> dict[str, float]:
        """Compute cosine similarity between query embedding and entry embeddings."""
        try:
            model = self._get_model()
            query_emb = model.encode(query, convert_to_tensor=True)
            texts = [p["search_text"] for p in parsed]
            entry_embs = model.encode(texts, convert_to_tensor=True)
            sims = util.cos_sim(query_emb, entry_embs)[0].cpu().numpy()
            return {p["entry"].id: float(s) for p, s in zip(parsed, sims)}
        except Exception:
            logger.debug("Embedding search failed, using BM25 only", exc_info=True)
            return {}

    # Reciprocal Rank Fusion: combined_score = 1/(k+rank_bm25) + 1/(k+rank_emb). Default k=60 dampens high-rank dominance.
    @staticmethod
    def _rrf_fuse(
        bm25: dict[str, float],
        emb: dict[str, float],
        k: int = 60,
    ) -> dict[str, float]:
        """Reciprocal Rank Fusion of two score dicts."""
        all_ids = set(bm25.keys()) | set(emb.keys())
        if not all_ids:
            return {}

        bm25_ranked = sorted(bm25.items(), key=lambda x: x[1], reverse=True)
        emb_ranked = sorted(emb.items(), key=lambda x: x[1], reverse=True)

        bm25_ranks = {eid: i + 1 for i, (eid, _) in enumerate(bm25_ranked)}
        emb_ranks = {eid: i + 1 for i, (eid, _) in enumerate(emb_ranked)}

        fused = {}
        for eid in all_ids:
            r1 = bm25_ranks.get(eid, len(bm25_ranks) + 1)
            r2 = emb_ranks.get(eid, len(emb_ranks) + 1)
            fused[eid] = (1.0 / (k + r1)) + (1.0 / (k + r2))

        return fused

    @staticmethod
    def _extract_text(event: Event) -> str:
        """Extract all text content from an event."""
        texts = []
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    texts.append(part.text)
        return " ".join(texts)

    @staticmethod
    def _content_to_text(content: types.Content) -> str:
        """Extract text from a Content object."""
        texts = []
        if content.parts:
            for part in content.parts:
                if part.text:
                    texts.append(part.text)
        return " ".join(texts)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Simple whitespace + punctuation tokenizer."""
        return re.findall(r'[a-zA-Z0-9]+', text.lower())

    @staticmethod
    def _event_to_dict(event: Event) -> dict:
        """Serialize an Event to a JSON-compatible dict."""
        result: dict = {
            "author": event.author,
            "parts": [],
        }
        if event.content and event.content.parts:
            for part in event.content.parts:
                part_dict: dict = {}
                if part.text:
                    part_dict["text"] = part.text
                if part_dict:
                    result["parts"].append(part_dict)
        if event.content:
            result["role"] = event.content.role or ""
        return result
