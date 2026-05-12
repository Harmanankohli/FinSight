import pytest

from agent_2_llamaindex.hybrid_search import HybridSearchPipeline
from agent_2_llamaindex.document_ingestion import DocumentIngestionPipeline
from llama_index.core.schema import NodeWithScore, TextNode


class MockIndexManager:
    def __init__(self):
        self.collections = {}

    def ingest_documents(self, collection_name: str, documents: list[dict]) -> int:
        self.collections.setdefault(collection_name, []).extend(documents)
        return len(documents)


@pytest.fixture
def pipeline():
    return HybridSearchPipeline(sparse_top_k=5, dense_top_k=5, rerank_top_n=3)


def test_rrf_merge_deduplicates(pipeline):
    sparse = [
        NodeWithScore(node=TextNode(text="doc1"), score=0.8),
        NodeWithScore(node=TextNode(text="doc2"), score=0.3),
    ]
    dense = [
        NodeWithScore(node=TextNode(text="doc3"), score=0.9),
        NodeWithScore(node=TextNode(text="doc1"), score=0.7),
    ]
    merged = pipeline._rrf_merge(sparse, dense, k=60)
    assert len(merged) == 3
    texts = [m.node.text for m in merged]
    assert texts[0] == "doc1"


def test_rrf_merge_empty(pipeline):
    merged = pipeline._rrf_merge([], [], k=60)
    assert merged == []


def test_build_sparse_index(pipeline):
    pipeline.build_sparse_index(["doc1 about NVDA", "doc2 about AAPL", "doc3 about NVDA"])
    assert pipeline._bm25 is not None
    assert len(pipeline._corpus) == 3


@pytest.mark.asyncio
async def test_sparse_retrieve(pipeline):
    pipeline.build_sparse_index(["NVDA earnings beat estimates", "AAPL new product launch", "NVDA stock rally"])
    results = await pipeline.sparse_retrieve("NVDA", top_k=2)
    assert len(results) <= 2
    assert any("NVDA" in r.node.text for r in results)


@pytest.mark.asyncio
async def test_retrieve_no_sparse(pipeline):
    results = await pipeline.retrieve("test", [])
    assert results == []


def test_document_ingestion():
    mock_index = MockIndexManager()
    ingestion = DocumentIngestionPipeline(mock_index)

    count = ingestion.ingest_sec_filing("NVDA", {
        "form": "10-K",
        "description": "Annual report",
        "edgar_url": "http://sec.gov/1",
        "filing_date": "2024-01-01",
    })
    assert count == 1
    assert "sec_filings" in mock_index.collections
    assert mock_index.collections["sec_filings"][0]["ticker"] == "NVDA"


def test_bulk_ingest():
    mock_index = MockIndexManager()
    ingestion = DocumentIngestionPipeline(mock_index)

    counts = ingestion.ingest_from_mcp(
        "NVDA",
        sec_filings=[{"form": "10-K", "description": "AR", "edgar_url": "", "filing_date": "2024-01-01"}],
        news=[{"title": "NVDA up", "summary": "Stock rose", "url": "", "published_at": "2024-01-02"}],
    )
    assert counts["sec_filings"] == 1
    assert counts["news"] == 1
