"""Push RAGAS evaluation scores to Langfuse as scored observations.

Usage::
    from tests.evaluation.push_scores import push_ragas_scores
    push_ragas_scores(ragas_result, trace_ids)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def push_ragas_scores(
    ragas_result,
    trace_ids: list[str] | None = None,
) -> None:
    """Push RAGAS metric scores to Langfuse.

    Args:
        ragas_result: The object returned by ragas.evaluate().
        trace_ids:    Optional list of Langfuse trace IDs to attach scores to.
                      If None, scores are logged without trace association.
    """
    try:
        from langfuse import Langfuse
        lf = Langfuse()
    except Exception as exc:
        logger.warning("Langfuse unavailable, skipping score push: %s", exc)
        return

    try:
        scores_df = ragas_result.to_pandas()
    except Exception as exc:
        logger.warning("Could not convert RAGAS result to DataFrame: %s", exc)
        return

    metric_cols = [c for c in scores_df.columns if c not in ("user_input", "response", "retrieved_contexts", "reference")]

    for i, row in scores_df.iterrows():
        trace_id = trace_ids[i] if trace_ids and i < len(trace_ids) else None
        for metric in metric_cols:
            val = row.get(metric)
            if val is None:
                continue
            try:
                score_kwargs = dict(name=metric, value=float(val))
                if trace_id:
                    score_kwargs["trace_id"] = trace_id
                lf.score(**score_kwargs)
            except Exception as exc:
                logger.warning("Failed to push score %s=%s: %s", metric, val, exc)

    try:
        lf.flush()
    except Exception:
        pass

    logger.info("Pushed RAGAS scores for %d samples to Langfuse", len(scores_df))
