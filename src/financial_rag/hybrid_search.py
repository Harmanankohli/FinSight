"""Hybrid search pipeline combining BM25 sparse retrieval, dense embedding similarity, RRF fusion, and CrossEncoder reranking."""  # noqa: E501

import asyncio
import logging

import numpy as np
from llama_index.core.schema import NodeWithScore, TextNode
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder

from shared.logging_config import logged, logged_sync
from shared.settings import RERANKER_MODEL

logger = logging.getLogger(__name__)


class HybridSearchPipeline:
    """BM25 sparse retrieval + embedding-based dense retrieval → RRF merge → CrossEncoder rerank pipeline."""  # noqa: E501

    @logged_sync(log_args=False, log_result=False)
    def __init__(
        self,
        sparse_top_k: int = 10,
        dense_top_k: int = 10,
        rerank_top_n: int = 5,
        rrf_k: int = 60,
    ):
        """Configure top-k values for sparse retrieval, dense retrieval, RRF fusion constant, and final rerank count."""  # noqa: E501
        self.sparse_top_k = sparse_top_k
        self.dense_top_k = dense_top_k
        self.rerank_top_n = rerank_top_n
        self.rrf_k = rrf_k
        self._reranker: CrossEncoder | None = None
        self._bm25: BM25Okapi | None = None
        self._corpus: list[str] = []

    @property
    def reranker(self) -> CrossEncoder:
        """Lazy-loaded CrossEncoder model for query-document pairwise relevance scoring."""  # noqa: E501
        if self._reranker is None:
            logger.info("Loading reranker model: %s", RERANKER_MODEL)
            self._reranker = CrossEncoder(
                RERANKER_MODEL,
                max_length=512,
            )
        return self._reranker

    @logged_sync(log_args=False, log_result=False)
    def build_sparse_index(self, documents: list[str]) -> None:
        """Tokenize and index a corpus of text documents for BM25 Okapi lexical retrieval."""  # noqa: E501
        tokenized = [doc.lower().split() for doc in documents]
        self._bm25 = BM25Okapi(tokenized)
        self._corpus = documents
        logger.info("Built BM25 index with %d documents", len(documents))

    @logged()
    async def sparse_retrieve(self, query: str, top_k: int | None = None) -> list[NodeWithScore]:
        """Run BM25 keyword-based bag-of-words retrieval against the tokenized corpus; returns scored nodes."""  # noqa: E501
        if self._bm25 is None:
            return []
        k = top_k or self.sparse_top_k
        tokenized = query.lower().split()
        scores = self._bm25.get_scores(tokenized)
        top_indices = np.argsort(scores)[::-1][:k]
        results = []
        for idx in top_indices:
            results.append(
                NodeWithScore(
                    node=TextNode(text=self._corpus[idx]),
                    score=float(scores[idx]),
                )
            )
        return results

    @logged()
    async def dense_retrieve(
        self, query: str, nodes: list[NodeWithScore], top_k: int | None = None
    ) -> list[NodeWithScore]:
        """Select top-k nodes by dense embedding cosine similarity score (pre-computed by the vector store)."""  # noqa: E501
        k = top_k or self.dense_top_k
        sorted_nodes = sorted(nodes, key=lambda n: n.score or 0.0, reverse=True)
        return sorted_nodes[:k]

    @staticmethod
    @logged_sync()
    def _rrf_merge(
        sparse: list[NodeWithScore],
        dense: list[NodeWithScore],
        k: int = 60,
    ) -> list[NodeWithScore]:
        """Fuse sparse and dense ranked lists using Reciprocal Rank Fusion; higher k reduces score difference between ranks."""  # noqa: E501
        seen: dict[str, float] = {}

        for rank, node in enumerate(sparse):
            text = node.node.text
            seen[text] = seen.get(text, 0.0) + 1.0 / (k + rank + 1)

        for rank, node in enumerate(dense):
            text = node.node.text
            seen[text] = seen.get(text, 0.0) + 1.0 / (k + rank + 1)

        merged = sorted(seen.items(), key=lambda x: x[1], reverse=True)
        return [NodeWithScore(node=TextNode(text=text), score=score) for text, score in merged]

    @logged()
    async def rerank(self, query: str, nodes: list[NodeWithScore]) -> list[NodeWithScore]:
        """Jointly score each (query, document) pair via CrossEncoder for fine-grained relevance; returns top N."""  # noqa: E501
        if not nodes:
            return []
        pairs = [(query, n.node.text) for n in nodes]
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(None, self.reranker.predict, pairs)
        for node, score in zip(nodes, scores):
            node.score = float(score)
        return sorted(nodes, key=lambda n: n.score or 0.0, reverse=True)[: self.rerank_top_n]

    @logged()
    async def retrieve(self, query: str, dense_nodes: list[NodeWithScore]) -> list[NodeWithScore]:
        """Run full hybrid pipeline: sparse retrieval → RRF merge → CrossEncoder rerank → top N results."""  # noqa: E501
        sparse_nodes = await self.sparse_retrieve(query)
        merged = self._rrf_merge(sparse_nodes, dense_nodes, k=self.rrf_k)
        reranked = await self.rerank(query, merged)
        return reranked
