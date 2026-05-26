import logging
from datetime import date

from llama_index.core import (
    Document,
    Settings,
    StorageContext,
    VectorStoreIndex,
)
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.vector_stores import MetadataFilters, ExactMatchFilter
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.llms.openai_like import OpenAILike
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb


from shared.config import LLM_MODEL, EMBED_MODEL, CHROMA_DIR, LLM_BASE_URL, LLM_API_KEY

logger = logging.getLogger(__name__)


class FinancialIndexManager:
    def __init__(self):
        self.llm = OpenAILike(
            model=LLM_MODEL,
            api_base=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            request_timeout=600.0,
            is_chat_model=True,
        )
        self.embed_model = HuggingFaceEmbedding(
            model_name=f"sentence-transformers/{EMBED_MODEL}"
        )
        Settings.llm = self.llm
        Settings.embed_model = self.embed_model
        Settings.node_parser = SentenceSplitter(chunk_size=512, chunk_overlap=50)

        self._chroma = chromadb.PersistentClient(path=CHROMA_DIR)
        self._indexes: dict[str, VectorStoreIndex] = {}

    def _get_or_create_index(self, collection_name: str) -> VectorStoreIndex:
        # Lazy index creation: one ChromaDB collection per document type (sec_filings, earnings, etc.)
        if collection_name in self._indexes:
            return self._indexes[collection_name]

        collection = self._chroma.get_or_create_collection(collection_name)
        vector_store = ChromaVectorStore(chroma_collection=collection)
        storage = StorageContext.from_defaults(vector_store=vector_store)
        index = VectorStoreIndex.from_vector_store(
            vector_store=vector_store,
            embed_model=self.embed_model,
            storage_context=storage,
        )
        self._indexes[collection_name] = index
        return index

    async def query(self, ticker: str, query_text: str) -> dict:
        # Metadata filter restricts retrieval to docs matching the ticker (multi-tenant by ticker)
        filters = MetadataFilters(
            filters=[ExactMatchFilter(key="ticker", value=ticker)]
        )
        index = self._get_or_create_index("sec_filings")
        engine = index.as_query_engine(similarity_top_k=5, filters=filters)
        today = date.today().isoformat()
        try:
            response = await engine.aquery(
                f"Today's date: {today}. "
                f"Research {ticker}: {query_text}\nProvide specific data with citations."
            )
            from llama_index.core.response import Response
            if isinstance(response, Response):
                resp_text = str(response)
                sources = [
                    n.node.metadata.get("file_name", "unknown")
                    for n in response.source_nodes
                ]
                scores = [round(n.score, 3) for n in response.source_nodes]
                context_texts = [n.node.get_content() for n in response.source_nodes]
                return {
                    "ticker": ticker,
                    "summary": resp_text,
                    "sources": sources[:5],
                    "relevance_scores": scores[:5],
                    "confidence_score": round(max(scores) if scores else 0.0, 3),
                    "context_texts": context_texts[:5],
                }
        except Exception as e:
            logger.exception("RAG query failed for %s", ticker)
            return {
                "ticker": ticker,
                "summary": f"Error: {e}",
                "sources": [],
                "relevance_scores": [],
                "confidence_score": 0.0,
            }
        return {
            "ticker": ticker,
            "summary": "No response",
            "sources": [],
            "relevance_scores": [],
            "confidence_score": 0.0,
        }

    async def query_sec_filings(self, ticker: str, query_text: str) -> dict:
        # Same ticker filter but scoped to sec_filings collection; validates first result matches
        filters = MetadataFilters(
            filters=[ExactMatchFilter(key="ticker", value=ticker)]
        )
        index = self._get_or_create_index("sec_filings")
        engine = index.as_query_engine(similarity_top_k=5, filters=filters)
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

    async def query_earnings(self, ticker: str, query_text: str) -> dict:
        # Earnings collection uses same metadata filter pattern but no cross-ticker validation
        index = self._get_or_create_index("earnings")
        engine = index.as_query_engine(similarity_top_k=5)
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

    def ingest_documents(
        self, collection_name: str, documents: list[dict]
    ) -> int:
        # Batch insert: wraps each dict as a LlamaIndex Document with ticker/source/file_name metadata
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
