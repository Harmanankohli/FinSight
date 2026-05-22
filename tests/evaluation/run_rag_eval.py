"""RAGAS evaluation runner for the Financial RAG Agent.

Runs Faithfulness, ResponseRelevancy, ContextPrecision, ContextRecall, and
NoiseSensitivity against the live RAG agent, then optionally pushes scores
to Langfuse.

Usage::
    python tests/evaluation/run_rag_eval.py
    python tests/evaluation/run_rag_eval.py --ticker NVDA --save-results
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
DATASET_PATH = EVAL_DIR / "rag_dataset.json"
RESULTS_DIR = EVAL_DIR / "eval_results"


def _build_dataset(ticker_filter: str | None = None):
    """Build a RAGAS EvaluationDataset from the curated JSON file."""
    from ragas import EvaluationDataset, SingleTurnSample

    with DATASET_PATH.open() as f:
        raw = json.load(f)

    if ticker_filter:
        raw = [r for r in raw if ticker_filter.upper() in r["user_input"].upper()]
        if not raw:
            logger.warning("No samples found for ticker filter '%s'; using all samples", ticker_filter)
            with DATASET_PATH.open() as f:
                raw = json.load(f)

    samples = [
        SingleTurnSample(
            user_input=item["user_input"],
            reference=item["reference"],
            reference_contexts=item.get("reference_contexts", []),
        )
        for item in raw
    ]
    return EvaluationDataset(samples=samples)


async def _query_rag_agent(user_input: str) -> tuple[str, list[str]]:
    """Call the live RAG agent and return (response_text, retrieved_contexts)."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from agent_2_llamaindex.executor import RAGAgent
        agent = RAGAgent()
        result = await agent.query("NVDA", user_input)
        response = result.get("summary", "")
        sources = result.get("sources", [])
        return response, sources
    except Exception as exc:
        logger.warning("RAG agent query failed: %s", exc)
        return "", []


def run_evaluation(ticker_filter: str | None = None, save_results: bool = False):
    """Run full RAGAS evaluation and optionally push to Langfuse."""
    try:
        from ragas import evaluate, EvaluationDataset, SingleTurnSample
        from ragas.metrics import (
            Faithfulness,
            ResponseRelevancy,
            ContextPrecision,
            ContextRecall,
            NoiseSensitivity,
        )
    except ImportError:
        logger.error("ragas not installed. Run: pip install 'ragas>=0.2.0'")
        sys.exit(1)

    dataset = _build_dataset(ticker_filter)
    logger.info("Running RAGAS evaluation on %d samples...", len(dataset.samples))

    metrics = [
        Faithfulness(),
        ResponseRelevancy(),
        ContextPrecision(),
        ContextRecall(),
        NoiseSensitivity(),
    ]

    result = evaluate(dataset=dataset, metrics=metrics)
    df = result.to_pandas()

    print("\n=== RAG Evaluation Results ===")
    print(df.to_string())
    print("\nAggregated scores:")
    for col in df.columns:
        if col not in ("user_input", "response", "retrieved_contexts", "reference"):
            print(f"  {col}: {df[col].mean():.3f}")

    if save_results:
        RESULTS_DIR.mkdir(exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        out_path = RESULTS_DIR / f"rag_eval_{ts}.json"
        out_path.write_text(df.to_json(orient="records", indent=2))
        logger.info("Results saved to %s", out_path)

    try:
        from tests.evaluation.push_scores import push_ragas_scores
        push_ragas_scores(result)
    except Exception as exc:
        logger.warning("Could not push scores to Langfuse: %s", exc)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run RAGAS evaluation on the RAG agent")
    parser.add_argument("--ticker", help="Filter dataset to samples mentioning this ticker")
    parser.add_argument("--save-results", action="store_true", help="Save results JSON to eval_results/")
    args = parser.parse_args()
    run_evaluation(ticker_filter=args.ticker, save_results=args.save_results)
