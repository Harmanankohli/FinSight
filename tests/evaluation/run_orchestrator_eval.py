"""RAGAS evaluation runner for the ADK Orchestrator.

Evaluates ToolCallAccuracy and AgentGoalAccuracy by replaying recorded
orchestrator traces. Traces are collected when EVAL_TRACE_ENABLED=true.

Usage::
    EVAL_TRACE_ENABLED=true python -m agent_1_adk.main  # collect traces
    python tests/evaluation/run_orchestrator_eval.py --save-results
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
TRACES_DIR = EVAL_DIR / "eval_results" / "orchestrator_traces"
RESULTS_DIR = EVAL_DIR / "eval_results"


def _load_traces() -> list[dict]:
    """Load all recorded orchestrator traces."""
    traces = []
    if not TRACES_DIR.exists():
        logger.warning("No traces directory found at %s", TRACES_DIR)
        return traces
    for path in sorted(TRACES_DIR.glob("*.json")):
        try:
            with path.open() as f:
                traces.append(json.load(f))
        except Exception as exc:
            logger.warning("Could not load trace %s: %s", path, exc)
    return traces


def _build_dataset(traces: list[dict]):
    """Build a RAGAS MultiTurnSample dataset from orchestrator traces."""
    try:
        from ragas import EvaluationDataset
        from ragas.messages import HumanMessage, ToolCall, ToolMessage, AIMessage
    except ImportError:
        logger.error("ragas not installed. Run: pip install 'ragas>=0.2.0'")
        sys.exit(1)

    samples = []
    for trace in traces:
        user_input = trace.get("user_input", "")
        agent_calls = trace.get("agent_calls", [])
        final_response = trace.get("final_response", "")

        messages = [HumanMessage(content=user_input)]
        for call in agent_calls:
            messages.append(ToolCall(
                name=call.get("agent_name", "unknown"),
                args={"task": call.get("task_sent", "")},
            ))
            messages.append(ToolMessage(content=call.get("response", "")))
        messages.append(AIMessage(content=final_response))

        try:
            from ragas import MultiTurnSample
            samples.append(MultiTurnSample(
                user_input=messages,
                reference=trace.get("reference", final_response),
            ))
        except Exception as exc:
            logger.warning("Could not build sample from trace: %s", exc)

    try:
        return EvaluationDataset(samples=samples)
    except Exception:
        return None


def run_evaluation(save_results: bool = False):
    """Run RAGAS orchestrator evaluation."""
    try:
        from ragas import evaluate
        from ragas.metrics import ToolCallAccuracy, AgentGoalAccuracy
    except ImportError:
        logger.error("ragas not installed. Run: pip install 'ragas>=0.2.0'")
        sys.exit(1)

    traces = _load_traces()
    if not traces:
        logger.warning("No traces found — run with EVAL_TRACE_ENABLED=true first")
        return None

    dataset = _build_dataset(traces)
    if dataset is None or not dataset.samples:
        logger.warning("Could not build evaluation dataset from traces")
        return None

    logger.info("Running orchestrator evaluation on %d traces...", len(dataset.samples))

    metrics = [ToolCallAccuracy(), AgentGoalAccuracy()]
    result = evaluate(dataset=dataset, metrics=metrics)
    df = result.to_pandas()

    print("\n=== Orchestrator Evaluation Results ===")
    print(df.to_string())
    print("\nAggregated scores:")
    for col in df.columns:
        if col not in ("user_input", "response"):
            print(f"  {col}: {df[col].mean():.3f}")

    if save_results:
        RESULTS_DIR.mkdir(exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        out_path = RESULTS_DIR / f"orchestrator_eval_{ts}.json"
        out_path.write_text(df.to_json(orient="records", indent=2))
        logger.info("Results saved to %s", out_path)

    try:
        from tests.evaluation.push_scores import push_ragas_scores
        push_ragas_scores(result)
    except Exception as exc:
        logger.warning("Could not push scores to Langfuse: %s", exc)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run RAGAS evaluation on the orchestrator")
    parser.add_argument("--save-results", action="store_true", help="Save results JSON to eval_results/")
    args = parser.parse_args()
    run_evaluation(save_results=args.save_results)
