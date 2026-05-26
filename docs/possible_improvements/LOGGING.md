# Logging Improvements

Analysis of current logging infrastructure and concrete improvements,
ordered by impact. No new dependencies required — all changes use
Python stdlib only.

## Current State

`shared/logging_config.py` provides `setup_file_logging(service_name, level)`
called at module level in each server entry point:

```
Format:  %(asctime)s %(levelname)-8s %(name)s: %(message)s
Output:  logs/<service>.log (RotatingFileHandler, 10 MB, 5 backups)
         + stderr (StreamHandler)
Example: 2026-05-26 14:32:01,045 INFO    orchestrator: Agent discovered 'Quant Analysis Agent'
```

| Service | Log File |
|---------|----------|
| Orchestrator | `logs/orchestrator.log` |
| RAG Agent | `logs/rag_agent.log` |
| Quant Agent | `logs/quant.log` |
| Sentiment Agent | `logs/sentiment.log` |
| MCP Server | `logs/mcp.log` |
| Memory callback | `logs/memory_callback.log` |

## Limitations

1. **No correlation IDs** — A single user query crosses MCP server,
   orchestrator, and 3 sub-agents. Each leaves logs in a different file
   with no shared identifier. Debugging a failed query means manually
   correlating timestamps across 5 files.

2. **Plain text format** — Not parseable by log aggregators (Loki, ELK).
   Any future centralized logging requires either writing a custom parser
   or re-ingesting all historical logs.

3. **Blocking file I/O on event loop** — `RotatingFileHandler.write()` is
   synchronous. In an `asyncio` application, disk writes stall the event
   loop. Under log volume (verbose DEBUG, MCP tool calls, A2A events),
   this adds measurable latency.

4. **Mixed log levels across services** — One level for everything.
   Setting `level=DEBUG` for the orchestrator floods the MCP log.
   Setting `level=INFO` for the MCP server hides tool-level detail.

5. **No structured fields** — `trace_id`, `ticker`, `agent_name`,
   `latency_ms` exist in code but are embedded in human-readable strings.
   Grepping for "NVDA" blindly searches all fields.

6. **No unified timing decorator** — Every MCP tool call, A2A message,
   and agent stream manually wraps with `time.monotonic()` or doesn't log
   timing at all. Inconsistent.

7. **No log sanitization** — `api_key="lmstudio"` appears in logs at
   DEBUG level. SEC EDGAR URLs appear at INFO. No redaction.

---

## Improvement 1 — Structured JSON Logging

**What**: Replace the plaintext formatter with a `logging.Formatter`
subclass that outputs JSON lines.

**Files to touch**: `shared/logging_config.py` only.

```json
{
  "ts": "2026-05-26T14:32:01.045Z",
  "level": "INFO",
  "service": "orchestrator",
  "logger": "agent_1_adk.agent",
  "message": "Agent discovered 'Quant Analysis Agent'",
  "trace_id": "abc123def456",
  "ticker": null,
  "latency_ms": null
}
```

**Benefits**: Ingests directly into Loki, ELK, CloudWatch, DataDog
without custom parsing. Grepping with `jq` is more powerful than
plaintext `grep` (`jq 'select(.trace_id=="abc123") | .message' logs/*.log`).

**Cost**: ~30 lines in `shared/logging_config.py`. Current
RotatingFileHandler stays — only the formatter changes. Plaintext
compatibility can be maintained via a separate StreamHandler with the
old format for terminal use.

---

## Improvement 2 — Correlation ID Propagation

**What**: Inject the Langfuse `trace_id` into every log line across
all 5 services.

**Files to touch**: `shared/trace_context.py` (add a
`get_current_trace_id()` wrapper that sets `contextvars.ContextVar`),
`shared/logging_config.py` (read the context in the formatter).

The mechanism already exists: `sub_agent_client.py` injects trace
context into A2A task text, and each agent extracts it. The same
propagation path can seed a Python `contextvars.ContextVar`:

```python
# shared/trace_context.py — add:
import contextvars
current_trace_id: contextvars.ContextVar[str | None] = \
    contextvars.ContextVar("trace_id", default=None)
```

Every log line from every service then carries the same query's
trace_id. `grep abc123def456 logs/*.log` returns the complete flow.

---

## Improvement 3 — Async Logging (QueueHandler + QueueListener)

**What**: Replace direct `RotatingFileHandler` with `QueueHandler` +
`QueueListener` — writes happen on a background thread.

**Files to touch**: `shared/logging_config.py` only.

```python
import logging.handlers
import queue

log_queue = queue.SimpleQueue()
queue_handler = logging.handlers.QueueHandler(log_queue)
root.addHandler(queue_handler)

listener = logging.handlers.QueueListener(
    log_queue, file_handler, stream_handler
)
listener.start()
```

**Why**: In an asyncio application, blocking I/O on the event loop is
the #1 cause of latency variance. A RotatingFileHandler flushing 10 MB
of logs adds ~50-100ms of synchronous disk write. QueueListener pushes
writes to a dedicated thread — zero event loop impact.

---

## Improvement 4 — Per-Service Log Levels via Env Vars

**What**: Read log level from environment variables per service, with a
global fallback.

**Files to touch**: `shared/logging_config.py` + `.env.example`.

```
# .env additions:
LOG_LEVEL=INFO
LOG_LEVEL_ORCHESTRATOR=DEBUG
LOG_LEVEL_RAG=INFO
LOG_LEVEL_QUANT=WARNING
LOG_LEVEL_SENTIMENT=ERROR
LOG_LEVEL_MCP=DEBUG
```

**Impact**: Turn off verbose agent logging to WARNING during
development without touching any code. Set a single agent to DEBUG for
targeted debugging.

---

## Improvement 5 — Unified Timing Decorator

**What**: A decorator that logs entry, exit, and duration for MCP tool
calls, A2A handler invocations, and agent stream yields.

**Files to touch**: `shared/logging_config.py` (add the decorator), then
apply to hot paths across `finsight_server.py`, `generic_executor.py`,
`sub_agent_client.py`.

```python
import functools
import time

def logged(level=logging.INFO):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            logger = logging.getLogger(fn.__module__)
            logger.log(level, "Enter %s", fn.__qualname__)
            t0 = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
                elapsed = (time.monotonic() - t0) * 1000
                logger.log(level, "Exit %s (%.0fms)",
                           fn.__qualname__, elapsed)
                return result
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                logger.log(level, "Fail %s (%.0fms): %s",
                           fn.__qualname__, elapsed, exc)
                raise
        return wrapper
    return decorator

# Usage:
@logged()
async def get_prices(ticker: str, ...) -> dict:
    ...
```

**Benefits**: Consistent timing on every tool call and A2A handler.
Works with JSON logging for structured analysis.

---

## Improvement 6 — Log Sanitization

**What**: Redact known sensitive patterns from log output before writing.

**Files to touch**: `shared/logging_config.py` (add a `logging.Filter`
subclass).

```python
import re

class SanitizeFilter(logging.Filter):
    SENSITIVE_PATTERNS = [
        (re.compile(r"api_key=['\"]?\w+['\"]?"), "api_key=***"),
        (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "sk-***"),
        (re.compile(r"pk-[a-zA-Z0-9]{20,}"), "pk-***"),
        (re.compile(r"Authorization: Bearer \S+"),
         "Authorization: Bearer ***"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "msg") and isinstance(record.msg, str):
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                record.msg = pattern.sub(replacement, record.msg)
        return True
```

---

## Implementation Order

| # | Improvement | Effort | Benefit |
|---|-------------|--------|---------|
| 1 | Structured JSON logging | ~30 lines, 1 file | Foundation for everything else |
| 2 | Per-service log levels | ~15 lines, 2 files | Immediate debugging quality-of-life |
| 3 | Correlation ID propagation | ~20 lines, 2 files | Cross-service query tracing |
| 4 | Unified timing decorator | ~30 lines + annotations | Consistent latency tracking |
| 5 | Async QueueHandler | ~15 lines, 1 file | Performance under load |
| 6 | Log sanitization filter | ~25 lines, 1 file | Safety net for secrets |
