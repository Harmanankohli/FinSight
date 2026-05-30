"""Shared AG-UI SSE framing helper."""

import json
import time

# Top-level event keys that are optional metadata — safe to strip when null.
# Keys like 'snapshot', 'delta', 'input', 'content' carry user data where
# null is semantically meaningful and must be preserved.
_STRIP_KEYS = {"rawEvent", "parentRunId", "parentMessageId", "result",
               "name", "code", "encryptedValue", "role"}


def _clean(obj, depth=0):
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


def _strip_message_nulls(messages):
    """Strip null optional fields inside message objects (name, encryptedValue)."""
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append({k: v for k, v in m.items() if v is not None or k in ("content", "role", "id")})
        else:
            out.append(m)
    return out


def sse(event_obj) -> str:
    """Format an AG-UI event as a Server-Sent Events data frame."""
    if event_obj.timestamp is None:
        event_obj.timestamp = int(time.time() * 1000)
    data = event_obj.model_dump(by_alias=True)

    # Strip null optional fields at event envelope level
    data = {k: v for k, v in data.items()
            if not (v is None and k in _STRIP_KEYS)}

    # Strip null fields inside input.messages (name, encryptedValue, etc.)
    if "input" in data and isinstance(data["input"], dict):
        inp = data["input"]
        inp = {k: v for k, v in inp.items() if not (v is None and k in _STRIP_KEYS)}
        if "messages" in inp:
            inp["messages"] = _strip_message_nulls(inp["messages"])
        data["input"] = inp

    return f"data: {json.dumps(data)}\n\n"
