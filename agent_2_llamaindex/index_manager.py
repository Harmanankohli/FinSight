import logging
from typing import Any

from llama_index.core import (
    Document,
    Settings,
    StorageContext,
    VectorStoreIndex,
)
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.query_engine import RouterQueryEngine
from llama_index.core.selectors import LLMMultiSelector
from llama_index.core.tools import QueryEngineTool, ToolMetadata
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.llms.groq import Groq
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb

from shared.config import GROQ_MODEL, EMBED_MODEL, CHROMA_DIR, GROQ_API_KEY

logger = logging.getLogger(__name__)


class FinancialIndexManager:
    def __init__(self):
        self.llm = Groq(
            model=GROQ_MODEL,
            api_key=GROQ_API_KEY,
        )
        self.embed_model = HuggingFaceEmbedding(
            model_name=f"sentence-transformers/{EMBED_MODEL}"
        )
        Settings.llm = self.llm
        Settings.embed_model = self.embed_model
        Settings.node_parser = SentenceSplitter(chunk_size=512, chunk_overlap=50)

        self._chroma = chromadb.PersistentClient(path=CHROMA_DIR)
        self._indexes: dict[str, VectorStoreIndex] = {}
        self._router: RouterQueryEngine | None = None

    def _get_or_create_index(self, collection_name: str) -> VectorStoreIndex:
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

    def _build_router(self) -> RouterQueryEngine:
        sec_index = self._get_or_create_index("sec_filings")
        earnings_index = self._get_or_create_index("earnings")
        news_index = self._get_or_create_index("news")
        analyst_index = self._get_or_create_index("analyst_reports")

        return RouterQueryEngine(
            selector=LLMMultiSelector.from_defaults(llm=self.llm),
            query_engine_tools=[
                QueryEngineTool(
                    query_engine=sec_index.as_query_engine(similarity_top_k=5),
                    metadata=ToolMetadata(
                        name="sec_filings",
                        description="Use for regulatory filings, annual reports, 10-K, 10-Q, 8-K, risk factors, management discussion",
                    ),
                ),
                QueryEngineTool(
                    query_engine=earnings_index.as_query_engine(similarity_top_k=5),
                    metadata=ToolMetadata(
                        name="earnings_transcripts",
                        description="Use for earnings call transcripts, management commentary, forward guidance, Q&A sessions",
                    ),
                ),
                QueryEngineTool(
                    query_engine=news_index.as_query_engine(similarity_top_k=5),
                    metadata=ToolMetadata(
                        name="financial_news",
                        description="Use for recent financial news, analyst updates, market reactions",
                    ),
                ),
                QueryEngineTool(
                    query_engine=analyst_index.as_query_engine(similarity_top_k=5),
                    metadata=ToolMetadata(
                        name="analyst_reports",
                        description="Use for analyst reports, price targets, sector analysis, investment theses",
                    ),
                ),
            ],
        )

    @property
    def router(self) -> RouterQueryEngine:
        if self._router is None:
            self._router = self._build_router()
        return self._router

    async def query(self, ticker: str, query_text: str) -> dict:
        try:
            response = self.router.aquery(
                f"Query: {query_text}\nTicker: {ticker}\nAnswer with specific data and cite sources."
            )
            from llama_index.core.response import Response
            if isinstance(response, Response):
                resp_text = str(response)
                sources = [
                    n.node.metadata.get("file_name", "unknown")
                    for n in response.source_nodes
                ]
                scores = [round(n.score, 3) for n in response.source_nodes]
            else:
                resp_text = str(response)
                sources = []
                scores = []

            return {
                "ticker": ticker,
                "summary": resp_text,
                "sources": sources[:5],
                "relevance_scores": scores[:5],
                "confidence_score": round(
                    max(scores) if scores else 0.0, 3
                ),
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

    async def query_sec_filings(self, ticker: str, query_text: str) -> dict:
        index = self._get_or_create_index("sec_filings")
        engine = index.as_query_engine(similarity_top_k=5)
        response = await engine.aquery(
            f"For {ticker}: {query_text}\nCite specific filing sections."
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

    async def query_earnings(self, ticker: str, query_text: str) -> dict:
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
