import functools
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from shared.trace_context import current_trace_id, current_session_id

# Logs directory: one level up from shared/ at project-root/logs/
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


class JsonFormatter(logging.Formatter):
    """JSON log formatter — each line is valid JSON ingestible by Loki/CloudWatch/Datadog."""

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # trace_id / session_id: prefer explicit extra= on the record, fall back to ContextVar
        # so that any log line emitted after extract_trace_ids() automatically carries the ID.
        trace_id = getattr(record, "trace_id", None) or current_trace_id.get()
        session_id = getattr(record, "session_id", None) or current_session_id.get()
        if trace_id:
            payload["trace_id"] = trace_id
        if session_id:
            payload["session_id"] = session_id
        for k in ("ticker", "latency_ms", "tool"):
            val = getattr(record, k, None)
            if val is not None:
                payload[k] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class SanitizeFilter(logging.Filter):
    """Scrub known secret patterns from log records before they are written."""

    _PATTERNS = [
        (re.compile(r"(api_key\s*=\s*)['\"]?[^'\"\s,)]+['\"]?"), r"\1***"),
        (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
        (re.compile(r"pk-[A-Za-z0-9]{20,}"), "pk-***"),
        (re.compile(r"(Authorization:\s*Bearer\s+)\S+"), r"\1***"),
        (re.compile(r"(LANGFUSE_(?:PUBLIC|SECRET)_KEY\s*[=:]\s*)\S+"), r"\1***"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pat, repl in self._PATTERNS:
                record.msg = pat.sub(repl, record.msg)
        if record.args:
            new_args = []
            for a in record.args:
                if isinstance(a, str):
                    for pat, repl in self._PATTERNS:
                        a = pat.sub(repl, a)
                new_args.append(a)
            record.args = tuple(new_args)
        return True


def logged(level: int = logging.INFO):
    """Decorator: logs enter/exit/latency for async functions on hot paths.

    Apply only to hot-path callables (MCP tools, agent stream(), send_message).
    Emits 'latency_ms' as a structured field in the JSON file log.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            _logger = logging.getLogger(fn.__module__)
            _logger.log(level, "Enter %s", fn.__qualname__)
            t0 = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
                elapsed = (time.monotonic() - t0) * 1000
                _logger.log(
                    level, "Exit %s (%.0fms)", fn.__qualname__, elapsed,
                    extra={"latency_ms": int(elapsed)},
                )
                return result
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                _logger.log(
                    level, "Fail %s (%.0fms): %s", fn.__qualname__, elapsed, exc,
                    extra={"latency_ms": int(elapsed)},
                )
                raise
        return wrapper
    return decorator


def setup_file_logging(service_name: str, level: int | None = None) -> None:
    """Configure the root logger to write to logs/<service_name>.log.

    Safe to call multiple times — duplicate handlers are skipped.
    StreamHandler uses plaintext (readable in terminals); file handler uses
    JSON lines (ingestible by log aggregators without custom parsers).
    """
    if level is None:
        env_key = f"LOG_LEVEL_{service_name.upper().replace('-', '_')}"
        level_str = os.environ.get(env_key) or os.environ.get("LOG_LEVEL", "INFO")
        level = getattr(logging, level_str.upper(), logging.INFO)

    _LOGS_DIR.mkdir(exist_ok=True)
    log_path = _LOGS_DIR / f"{service_name}.log"

    root = logging.getLogger()
    root.setLevel(level)

    # Skip if a RotatingFileHandler for this exact path is already registered.
    # Makes setup_file_logging idempotent — safe to call from every service startup.
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and h.baseFilename == str(log_path):
            return

    plain_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    sanitize = SanitizeFilter()

    # Console handler: plaintext so terminal output stays readable.
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(plain_fmt)
        sh.addFilter(sanitize)
        root.addHandler(sh)

    # File handler: JSON lines, 10 MB per file, keep 5 backups.
    fh = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(JsonFormatter(service_name))
    fh.addFilter(sanitize)
    root.addHandler(fh)
