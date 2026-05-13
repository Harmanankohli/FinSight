import logging
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder

from shared.config import RERANKER_MODEL
from llama_index.core.schema import NodeWithScore, TextNode

logger = logging.getLogger(__name__)


class HybridSearchPipeline:
    def __init__(
        self,
        sparse_top_k: int = 10,
        dense_top_k: int = 10,
        rerank_top_n: int = 5,
        rrf_k: int = 60,
    ):
        self.sparse_top_k = sparse_top_k
        self.dense_top_k = dense_top_k
        self.rerank_top_n = rerank_top_n
        self.rrf_k = rrf_k
        self._reranker: CrossEncoder | None = None
        self._bm25: BM25Okapi | None = None
        self._corpus: list[str] = []

    @property
    def reranker(self) -> CrossEncoder:
        if self._reranker is None:
            self._reranker = CrossEncoder(
                RERANKER_MODEL,
                max_length=512,
            )
        return self._reranker

    def build_sparse_index(self, documents: list[str]) -> None:
        tokenized = [doc.lower().split() for doc in documents]
        self._bm25 = BM25Okapi(tokenized)
        self._corpus = documents
        logger.info("Built BM25 index with %d documents", len(documents))

    async def sparse_retrieve(self, query: str, top_k: int | None = None) -> list[NodeWithScore]:
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

    async def dense_retrieve(
        self, query: str, nodes: list[NodeWithScore], top_k: int | None = None
    ) -> list[NodeWithScore]:
        k = top_k or self.dense_top_k
        sorted_nodes = sorted(nodes, key=lambda n: n.score or 0.0, reverse=True)
        return sorted_nodes[:k]

    @staticmethod
    def _rrf_merge(
        sparse: list[NodeWithScore],
        dense: list[NodeWithScore],
        k: int = 60,
    ) -> list[NodeWithScore]:
        seen: dict[str, float] = {}

        for rank, node in enumerate(sparse):
            text = node.node.text
            seen[text] = seen.get(text, 0.0) + 1.0 / (k + rank + 1)

        for rank, node in enumerate(dense):
            text = node.node.text
            seen[text] = seen.get(text, 0.0) + 1.0 / (k + rank + 1)

        merged = sorted(seen.items(), key=lambda x: x[1], reverse=True)
        return [
            NodeWithScore(node=TextNode(text=text), score=score)
            for text, score in merged
        ]

    async def rerank(
        self, query: str, nodes: list[NodeWithScore]
    ) -> list[NodeWithScore]:
        if not nodes:
            return []
        pairs = [(query, n.node.text) for n in nodes]
        scores = self.reranker.predict(pairs)
        for node, score in zip(nodes, scores):
            node.score = float(score)
        return sorted(nodes, key=lambda n: n.score or 0.0, reverse=True)[:self.rerank_top_n]

    async def retrieve(
        self, query: str, dense_nodes: list[NodeWithScore]
    ) -> list[NodeWithScore]:
        sparse_nodes = await self.sparse_retrieve(query)
        merged = self._rrf_merge(sparse_nodes, dense_nodes, k=self.rrf_k)
        reranked = await self.rerank(query, merged)
        return reranked
