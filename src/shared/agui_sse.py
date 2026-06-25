"""Shared AG-UI SSE framing helper."""

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Optional event keys where null must be omitted (Zod .optional() rejects null).
# Data-carrying keys like 'snapshot', 'delta', 'content' are NOT listed here
# because null may be semantically meaningful for JSON Patch / state management.
_STRIP_KEYS = {
    "rawEvent",
    "parentRunId",
    "parentMessageId",
    "result",
    "name",
    "code",
    "encryptedValue",
    "role",
    "input",
}


def _clean(obj: Any, depth: int = 0) -> Any:
    """Strip null values only from the event envelope and message-level metadata.

    Preserves null in data-carrying fields (snapshot, delta values, input nested
    content) so JSON Patch and CopilotKit state management work correctly.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if v is None and k in _STRIP_KEYS:
                continue
            if isinstance(v, dict):
                out[k] = _clean(v, depth + 1)
            elif isinstance(v, list):
                out[k] = _clean(v, depth + 1)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_clean(item, depth + 1) for item in obj]
    return obj


def _strip_message_nulls(messages: Any) -> Any:
    """Strip null optional fields inside message objects (name, encryptedValue)."""
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append(
                {k: v for k, v in m.items() if v is not None or k in ("content", "role", "id")}
            )
        else:
            out.append(m)
    return out


def sse(event_obj: Any) -> str:
    """Format an AG-UI event as a Server-Sent Events data frame."""
    if event_obj.timestamp is None:
        event_obj.timestamp = int(time.time() * 1000)
    data = event_obj.model_dump(by_alias=True)
    data = _clean(data)
    return f"data: {json.dumps(data)}\n\n"
