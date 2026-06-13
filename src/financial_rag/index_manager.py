import logging
import math
import re
import threading
from datetime import date, datetime

import chromadb
from llama_index.core import (
    Document,
    Settings,
    StorageContext,
    VectorStoreIndex,
)
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.response_synthesizers import get_response_synthesizer
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.llms.openai_like import OpenAILike
from llama_index.vector_stores.chroma import ChromaVectorStore

from shared.logging_config import logged, logged_sync
from shared.settings import CHROMA_DIR, EMBED_MODEL, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hybrid scoring helpers (§8.5.1 + §8.5.2)
# ---------------------------------------------------------------------------


def _keyword_score(query_text: str, node_text: str) -> float:
    """Term-frequency keyword overlap between query terms and node text."""
    q_terms = set(re.findall(r"\w{3,}", query_text.lower()))  # skip stop-word length tokens
    if not q_terms:
        return 0.0
    n_lower = node_text.lower()
    n_total = max(1, len(re.findall(r"\w+", n_lower)))
    match_count = sum(n_lower.count(t) for t in q_terms)
    return min(1.0, match_count / (n_total * 0.08 + 1))


def _temporal_decay(doc_date: str, decay_lambda: float = 0.004) -> float:
    """Exponential decay by age: recent docs score near 1.0, year-old ~0.23."""
    if not doc_date:
        return 0.65  # unknown date — moderate penalty
    try:
        pub = datetime.fromisoformat(doc_date[:10]).date()
        days_old = max(0, (date.today() - pub).days)
        return math.exp(-decay_lambda * days_old)
    except (ValueError, TypeError):
        return 0.65


def _hybrid_score(
    vector_score: float,
    node_text: str,
    doc_date: str,
    query_text: str,
    w_vector: float = 0.60,
    w_keyword: float = 0.20,
    w_temporal: float = 0.20,
) -> float:
    """Weighted combination of dense vector score, keyword overlap, and temporal decay."""
    kw = _keyword_score(query_text, node_text)
    td = _temporal_decay(doc_date)
    return round(w_vector * vector_score + w_keyword * kw + w_temporal * td, 4)


def _classify_query_intent(query_text: str) -> list[str]:
    """Return which ChromaDB collections to search based on keywords in the query."""
    q = query_text.lower()
    collections = ["sec_filings"]
    if any(
        kw in q
        for kw in (
            "news",
            "recent",
            "latest",
            "today",
            "sentiment",
            "market",
            "headline",
            "article",
            "impact",
            "event",
            "announce",
            "breaking",
            "update",
        )
    ):
        collections.append("news")
    if any(
        kw in q
        for kw in (
            "earnings",
            "revenue",
            "guidance",
            "quarter",
            "call",
            "eps",
            "beat",
            "miss",
            "forecast",
        )
    ):
        collections.append("earnings")
    # Broad analytical queries: search all collections
    if any(
        kw in q
        for kw in (
            "analyze",
            "analysis",
            "overview",
            "outlook",
            "recommend",
            "should i",
            "what do you think",
            "bull",
            "bear",
        )
    ):
        return ["sec_filings", "news", "earnings"]
    return collections


class FinancialIndexManager:
    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        self.llm = OpenAILike(
            model=LLM_MODEL,
            api_base=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            request_timeout=600.0,
            is_chat_model=True,
        )
        self._embed_model = None
        Settings.llm = self.llm
        Settings.node_parser = SentenceSplitter(chunk_size=512, chunk_overlap=50)

        self._chroma = chromadb.PersistentClient(path=CHROMA_DIR)
        self._indexes: dict[str, VectorStoreIndex] = {}
        self._index_locks: dict[str, threading.Lock] = {}

    def _lazy_embed(self) -> HuggingFaceEmbedding:
        if self._embed_model is None:
            model = f"sentence-transformers/{EMBED_MODEL}"
            self._embed_model = HuggingFaceEmbedding(model_name=model)
            Settings.embed_model = self._embed_model
        return self._embed_model

    @logged_sync()
    def _get_or_create_index(self, collection_name: str) -> VectorStoreIndex:
        # Lazy index creation: one ChromaDB collection per document type (sec_filings, earnings, etc.)  # noqa: E501
        if collection_name not in self._index_locks:
            self._index_locks[collection_name] = threading.Lock()

        if collection_name in self._indexes:
            return self._indexes[collection_name]

        embed = self._lazy_embed()
        with self._index_locks[collection_name]:
            if collection_name in self._indexes:
                return self._indexes[collection_name]
            collection = self._chroma.get_or_create_collection(collection_name)
            vector_store = ChromaVectorStore(chroma_collection=collection)
            storage = StorageContext.from_defaults(vector_store=vector_store)
            index = VectorStoreIndex.from_vector_store(
                vector_store=vector_store,
                embed_model=embed,
                storage_context=storage,
            )
            self._indexes[collection_name] = index
        return index

    @logged()
    async def query(self, ticker: str, query_text: str) -> dict:
        """Multi-collection retrieval: routes across sec_filings/news/earnings based on intent."""
        filters = MetadataFilters(filters=[ExactMatchFilter(key="ticker", value=ticker)])
        collections = _classify_query_intent(query_text)
        today = date.today().isoformat()
        augmented_query = (
            f"Today's date: {today}. Research {ticker}: {query_text}\n"
            f"IMPORTANT: Answer using ONLY the information present in the retrieved documents. "
            f"Do NOT mention data that is absent, historical comparisons not in the documents, "
            f"or requests for additional information. If the documents contain recent data, "
            f"give your verdict based on that. Cite specific figures and filing dates."
        )

        all_nodes = []
        for coll in collections:
            try:
                index = self._get_or_create_index(coll)
                retriever = index.as_retriever(similarity_top_k=5, filters=filters)
                nodes = await retriever.aretrieve(augmented_query)
                logger.info("Retrieved %d nodes from %s for %s", len(nodes), coll, ticker)
                all_nodes.extend(nodes)
            except Exception as e:
                logger.warning("Retrieval from %s failed for %s: %s", coll, ticker, e)

        logger.info(
            "Total nodes across collections %s for %s: %d", collections, ticker, len(all_nodes)
        )

        if not all_nodes:
            return {
                "ticker": ticker,
                "summary": (
                    f"Index is warming for {ticker} — initial fetch in progress. "
                    f"Try again in 10–20 seconds for full results."
                ),
                "_warming": True,
                "sources": [],
                "relevance_scores": [],
                "confidence_score": 0.0,
                "context_texts": [],
            }

        # Hybrid re-scoring: blend dense vector score, keyword overlap, and temporal decay
        for n in all_nodes:
            doc_date = n.node.metadata.get("date", "")
            n.score = _hybrid_score(
                vector_score=n.score or 0.0,
                node_text=n.node.get_content(),
                doc_date=doc_date,
                query_text=augmented_query,
            )

        # Deduplicate by first-200-char hash, keep highest-score copy
        seen: set[int] = set()
        unique = []
        for n in sorted(all_nodes, key=lambda x: x.score or 0, reverse=True):
            h = hash(n.node.text[:200])
            if h not in seen:
                seen.add(h)
                unique.append(n)
        unique = unique[:5]

        try:
            synth = get_response_synthesizer(llm=self.llm, response_mode="compact")
            response = await synth.asynthesize(query=augmented_query, nodes=unique)
            resp_text = str(response)
            sources = [n.node.metadata.get("file_name", "unknown") for n in unique]
            scores = [round(n.score or 0.0, 3) for n in unique]
            context_texts = [n.node.get_content() for n in unique]
            confidence = round(max(scores) if scores else 0.0, 3)
            logger.info(
                "query result for %s: nodes=%d, collections=%s, confidence=%.3f",
                ticker,
                len(unique),
                collections,
                confidence,
            )
            from shared.agent_models import RAGAgentOutput

            return RAGAgentOutput(
                ticker=ticker,
                summary=resp_text,
                sources=sources,
                relevance_scores=scores,
                confidence_score=confidence,
                context_texts=context_texts,
            ).model_dump()
        except Exception as e:
            logger.exception("RAG synthesis failed for %s", ticker)
            from shared.agent_models import RAGAgentOutput

            return RAGAgentOutput(
                ticker=ticker,
                summary=f"Error: {e}",
            ).model_dump()

    @logged()
    async def query_sec_filings(self, ticker: str, query_text: str) -> dict:
        # Same ticker filter but scoped to sec_filings collection; validates first result matches
        filters = MetadataFilters(filters=[ExactMatchFilter(key="ticker", value=ticker)])
        index = self._get_or_create_index("sec_filings")
        engine = index.as_query_engine(similarity_top_k=3, filters=filters, response_mode="compact")
        today = date.today().isoformat()
        response = await engine.aquery(
            f"Today's date: {today}.\nFor {ticker}: {query_text}\nCite specific filing sections."
        )
        sources = [n.node.metadata.get("file_name", "unknown") for n in response.source_nodes]
        scores = [round(n.score, 3) for n in response.source_nodes]
        first_file = sources[0] if sources else ""
        if first_file and not first_file.upper().startswith(ticker.upper()):
            return {
                "ticker": ticker,
                "summary": f"No {ticker} filings found in the index.",
                "sources": [],
                "relevance_scores": [],
                "confidence_score": 0.0,
            }
        return {
            "ticker": ticker,
            "summary": str(response),
            "sources": sources[:5],
            "relevance_scores": scores[:5],
            "confidence_score": round(max(scores) if scores else 0.0, 3),
        }

    @logged()
    async def query_earnings(self, ticker: str, query_text: str) -> dict:
        # Earnings collection uses same metadata filter pattern but no cross-ticker validation
        index = self._get_or_create_index("earnings")
        engine = index.as_query_engine(similarity_top_k=3, response_mode="compact")
        response = await engine.aquery(
            f"For {ticker}: {query_text}\nReference specific earnings call sections."
        )
        sources = [n.node.metadata.get("file_name", "unknown") for n in response.source_nodes]
        scores = [round(n.score, 3) for n in response.source_nodes]
        return {
            "ticker": ticker,
            "summary": str(response),
            "sources": sources[:5],
            "relevance_scores": scores[:5],
            "confidence_score": round(max(scores) if scores else 0.0, 3),
        }

    @logged_sync()
    def ingest_documents(self, collection_name: str, documents: list[dict]) -> int:
        # Batch insert: wraps each dict as a LlamaIndex Document with ticker/source/file_name metadata  # noqa: E501
        index = self._get_or_create_index(collection_name)
        docs = [
            Document(
                text=d.get("text", ""),
                metadata={
                    "ticker": d.get("ticker", ""),
                    "source": d.get("source", ""),
                    "file_name": d.get("file_name", ""),
                    "date": d.get("date", ""),
                },
            )
            for d in documents
        ]
        for doc in docs:
            index.insert(doc)
        logger.info("Ingested %d docs into %s", len(docs), collection_name)
        return len(docs)
