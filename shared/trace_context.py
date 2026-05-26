"""Trace-context injection/extraction for A2A inter-agent messages.

The protocol: a JSON prefix with ``_trace`` envelope is prepended to the
task text, separated by a sentinel newline + marker that is vanishingly
unlikely to appear in real investment text.  Downstream agents strip the
prefix before processing, preserving parent-child span relationships
across the agent DAG.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Sentinel separator: a marker between the JSON prefix and the real task text.
# Using a triple-angled delimiter + newline makes accidental collisions with
# user-provided text extremely unlikely.
_SEPARATOR = "\n<<<TASK>>>\n"


def inject_trace_context(task_text: str, trace_id: str, parent_span_id: str) -> str:
    """Prepend a JSON trace envelope to *task_text*.

    The caller (orchestrator or parent agent) injects its current trace_id
    and parent_span_id so the downstream agent can create child spans that
    form a unified trace tree across A2A boundaries.
    """
    if not trace_id or not parent_span_id:
        return task_text
    prefix = json.dumps({
        "_trace": {
            "trace_id": trace_id,
            "parent_span_id": parent_span_id,
        }
    })
    return f"{prefix}{_SEPARATOR}{task_text}"


def extract_trace_context(task_text: str) -> tuple[dict | None, str]:
    """Strip and parse the trace prefix, returning (trace_dict, clean_text).

    Returns (None, task_text) if no valid prefix is found — the agent
    simply proceeds without trace context.
    """
    if _SEPARATOR not in task_text:
        return None, task_text
    prefix_raw, clean_task = task_text.split(_SEPARATOR, 1)
    try:
        prefix = json.loads(prefix_raw)
        trace_ctx = prefix.get("_trace")
        if trace_ctx and "trace_id" in trace_ctx and "parent_span_id" in trace_ctx:
            return trace_ctx, clean_task
    except (json.JSONDecodeError, AttributeError):
        pass
    return None, task_text


def extract_trace_ids(task_text: str) -> tuple[str | None, str | None, str]:
    """Convenience: extract only trace_id + parent_span_id + clean text."""
    trace_ctx, clean_query = extract_trace_context(task_text)
    if trace_ctx:
        return trace_ctx.get("trace_id"), trace_ctx.get("parent_span_id"), clean_query
    return None, None, clean_query
