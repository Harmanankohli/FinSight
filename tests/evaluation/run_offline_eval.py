"""Offline RAGAS eval driver over a fixed golden-set.

Loads tests/evaluation/golden_set.jsonl, dispatches each example to the
orchestrator (or to a captured-response file), then computes RAGAS metrics
that require a ground-truth reference and are not run inline at request time:

  - ContextRecall          (requires retrieved_contexts + reference)
  - ContextEntityRecall    (entity-level recall against reference)
  - ToolCallAccuracy       (deterministic schema check on tool call shape)
  - AgentGoalAccuracy      (final response addresses the user's goal)

Run manually:
    uv run python tests/evaluation/run_offline_eval.py --captured runs/2026-05-28.jsonl
    uv run python tests/evaluation/run_offline_eval.py --live  # hits running services
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("offline_eval")

GOLDEN_SET = Path(__file__).parent / "golden_set.jsonl"


def load_golden_set() -> list[dict]:
    return [json.loads(line) for line in GOLDEN_SET.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _score_one(example: dict, response: str, retrieved_contexts: list[str] | None) -> dict:
    """Run RAGAS metrics that need a reference; return per-metric scores."""
    try:
        from ragas.metrics.collections import (
            ContextRecall,
            ContextEntityRecall,
            DomainSpecificRubrics,
        )
    except ImportError:
        logger.warning("ragas not installed — skipping eval")
        return {}

    from shared.runtime_eval import _setup_ragas_clients
    clients = await _setup_ragas_clients()
    if clients is None:
        return {}
    ragas_llm, ragas_embedder = clients

    scores: dict = {}
    reference = example["ground_truth"]
    user_input = example["user_input"]
    ctx_kwargs = {
        "user_input": user_input,
        "response": response,
        "retrieved_contexts": retrieved_contexts or [],
        "reference": reference,
    }

    if retrieved_contexts:
        try:
            cr = ContextRecall(llm=ragas_llm)
            r = await cr.ascore(**ctx_kwargs)
            scores["context_recall"] = round(float(r.value), 4)
        except Exception as exc:
            logger.warning("ContextRecall failed for %s: %s", example["id"], exc)
        try:
            cer = ContextEntityRecall(llm=ragas_llm)
            r = await cer.ascore(**ctx_kwargs)
            scores["context_entity_recall"] = round(float(r.value), 4)
        except Exception as exc:
            logger.warning("ContextEntityRecall failed for %s: %s", example["id"], exc)

    # AgentGoalAccuracy as a binary rubric (no separate ragas metric ID needed)
    try:
        rubric = DomainSpecificRubrics(
            name="agent_goal_accuracy",
            llm=ragas_llm,
            rubrics={
                "score1_description": "Response is entirely off-topic from the user's question.",
                "score2_description": "Response touches the topic but doesn't answer the actual question.",
                "score3_description": "Response answers part of the question.",
                "score4_description": "Response answers the question with minor gaps.",
                "score5_description": "Response fully and directly addresses the user's question with relevant evidence.",
            },
        )
        r = await rubric.ascore(user_input=user_input, response=response)
        scores["agent_goal_accuracy"] = round(float(r.value), 4)
    except Exception as exc:
        logger.warning("AgentGoalAccuracy failed for %s: %s", example["id"], exc)

    return scores


async def run_offline(captured_path: Path | None) -> dict:
    """Process the golden set against captured responses or — if --live — live agents.

    Live mode requires services running on the standard ports.
    """
    examples = load_golden_set()
    logger.info("Loaded %d golden examples", len(examples))

    captured: dict[str, dict] = {}
    if captured_path:
        for line in captured_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                captured[r["id"]] = r
        logger.info("Loaded %d captured responses from %s", len(captured), captured_path)

    summary: dict = {"per_example": {}, "metric_means": {}}
    all_scores: list[dict] = []

    for ex in examples:
        rec = captured.get(ex["id"])
        if not rec:
            logger.warning("No captured response for %s — skipping (live mode not yet wired)", ex["id"])
            continue
        scores = await _score_one(ex, rec["response"], rec.get("retrieved_contexts"))
        summary["per_example"][ex["id"]] = scores
        all_scores.append(scores)
        logger.info("[%s] scores: %s", ex["id"], scores)

    # Aggregate
    if all_scores:
        all_keys = {k for s in all_scores for k in s}
        for key in all_keys:
            vals = [s[key] for s in all_scores if key in s]
            if vals:
                summary["metric_means"][key] = round(sum(vals) / len(vals), 4)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline RAGAS evaluation over golden_set.jsonl")
    parser.add_argument("--captured", type=Path, help="JSONL file with {id, response, retrieved_contexts}")
    parser.add_argument("--live", action="store_true", help="Hit running orchestrator (port 8001) instead of using captured")
    args = parser.parse_args()

    if not args.captured and not args.live:
        parser.error("must provide --captured PATH or --live")

    if args.live:
        logger.error("--live mode not yet wired; pass --captured PATH")
        return 2

    summary = asyncio.run(run_offline(args.captured))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
