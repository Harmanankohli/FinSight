import json
import logging

logger = logging.getLogger(__name__)

_SEPARATOR = "\n<<<TASK>>>\n"


def inject_trace_context(task_text: str, trace_id: str, parent_span_id: str) -> str:
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
    trace_ctx, clean_query = extract_trace_context(task_text)
    if trace_ctx:
        return trace_ctx.get("trace_id"), trace_ctx.get("parent_span_id"), clean_query
    return None, None, clean_query
