"""JSON-structured logging with JsonFormatter and @logged/@logged_sync decorators.

Console output is ANSI-colored by log level and service name. File output is
plain JSON lines (no color codes) for log aggregators.

Color scheme
------------
Level colors (console StreamHandler only):
  DEBUG    → Cyan        \033[36m
  INFO     → Green       \033[32m
  WARNING  → Yellow      \033[33m
  ERROR    → Red         \033[31m
  CRITICAL → Bold white on red  \033[1;37;41m

Service badge colors (aligned with frontend CSS palette in globals.css):
  orchestrator   → Yellow    \033[33m   (#8b6f4e gold)
  rag            → Blue      \033[34m   (#2c4a7c)
  quant          → Green     \033[32m   (#2a6b2a)
  market_context → Brown     \033[38;5;130m  (#8b4513)
  mcp            → Magenta   \033[35m   (#5a3e7c)
  analytics      → Cyan      \033[36m   (#1a7a7a)
  reviewer       → Red       \033[31m   (#9b2335)

Decorator lifecycle markers (visible in both colored and plain output):
  →  Enter  (U+2192)
  ←  Exit   (U+2190)
  ✗  Fail   (U+2717)
  ⏱  timer  (U+23F1)

Environment controls:
  NO_COLOR=1      — disable all ANSI codes (https://no-color.org/)
  FORCE_COLOR=1   — force ANSI codes even on non-TTY (useful for CI pipelines)
"""

import functools
import inspect
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from shared.trace_context import current_session_id, current_trace_id

# Canonical log files written by setup_file_logging():
#   logs/orchestrator.log   — orchestrator
#   logs/rag_agent.log      — financial_rag
#   logs/quant.log          — quant
#   logs/market_context.log — market_context
#   logs/mcp.log            — mcp_tools

# Logs directory: two levels up from src/shared/ at project-root/logs/
_LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"

# ---------------------------------------------------------------------------
# ANSI color constants (console only — never written to JSON file logs)
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

# Log-level foreground colors
_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG:    "\033[36m",       # Cyan
    logging.INFO:     "\033[32m",       # Green
    logging.WARNING:  "\033[33m",       # Yellow
    logging.ERROR:    "\033[31m",       # Red
    logging.CRITICAL: "\033[1;37;41m",  # Bold white on red background
}

# Dim white/gray for timestamps — visually recedes to reduce noise
_TIMESTAMP_COLOR = "\033[2;37m"

# Service name badge colors — aligned with frontend CSS palette (globals.css)
_SERVICE_COLORS: dict[str, str] = {
    "orchestrator":   "\033[33m",        # Yellow   (--orch: #8b6f4e gold)
    "rag":            "\033[34m",        # Blue     (--rag: #2c4a7c)
    "quant":          "\033[32m",        # Green    (--quant: #2a6b2a)
    "market_context": "\033[38;5;130m",  # Brown    (--market: #8b4513)
    "mcp":            "\033[35m",        # Magenta  (--mcp: #5a3e7c)
    "analytics":      "\033[36m",        # Cyan     (--analytics: #1a7a7a)
    "reviewer":       "\033[31m",        # Red      (--reviewer: #9b2335)
    "adk_web":        "\033[33m",        # Yellow   (ADK web variant of orchestrator)
}

# Decorator lifecycle Unicode markers — visible in colored and plain output
_ENTER_MARKER = "→"   # →
_EXIT_MARKER  = "←"   # ←
_FAIL_MARKER  = "✗"   # ✗
_LATENCY_MARKER = "⏱" # ⏱


def _should_colorize(stream: Any) -> bool:
    """Return True if ANSI colors should be emitted on this stream.

    Respects NO_COLOR (https://no-color.org/) and FORCE_COLOR conventions.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    try:
        return hasattr(stream, "isatty") and stream.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """JSON log formatter — each line is valid JSON ingestible by Loki/CloudWatch/Datadog."""

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
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


class ColoredFormatter(logging.Formatter):
    """Console formatter that applies ANSI colors by log level and service.

    Dims timestamps, colors the level name, renders a bold service badge,
    and colors the message. Falls back to no-op when colors are disabled.

    Decorator lifecycle messages (starting with →/←/✗) get their own colors
    independent of log level to make function entry/exit/failure stand out.
    """

    def __init__(self, service_name: str = ""):
        super().__init__()
        self._service_name = service_name
        self._svc_color = _SERVICE_COLORS.get(service_name, "")

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.mmm
        level = record.levelname
        level_color = _LEVEL_COLORS.get(record.levelno, "")
        msg = record.getMessage()

        # Decorator lifecycle messages get their own color scheme
        if msg.startswith(_ENTER_MARKER):
            msg_colored = f"\033[36m{msg}{_RESET}"     # Cyan for enter
        elif msg.startswith(_EXIT_MARKER):
            msg_colored = f"\033[32m{msg}{_RESET}"     # Green for exit
        elif msg.startswith(_FAIL_MARKER):
            msg_colored = f"\033[1;31m{msg}{_RESET}"   # Bold red for fail
        else:
            msg_colored = f"{level_color}{msg}{_RESET}"

        svc_badge = (
            f" {self._svc_color}{_BOLD}[{self._service_name}]{_RESET}"
            if self._service_name
            else ""
        )

        line = (
            f"{_TIMESTAMP_COLOR}{ts}{_RESET} "
            f"{level_color}{_BOLD}{level:<8}{_RESET} "
            f"{record.name}{svc_badge}: "
            f"{msg_colored}"
        )

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class SanitizeFilter(logging.Filter):
    """Scrub known secret patterns from log records before they are written."""

    _PATTERNS = [
        (re.compile(r"(api_key\s*=\s*)['\"]?[^'\"\s,)]+['\"]?"), r"\1***"),
        (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
        (re.compile(r"pk-[A-Za-z0-9]{20,}"), "pk-***"),
        (re.compile(r"(Authorization:\s*Bearer)\s+\S+", re.IGNORECASE), r"\1 ***"),
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


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _trunc_repr(obj: Any, max_len: int = 500) -> str:
    """Safely truncate repr of an object for logging."""
    try:
        s = repr(obj)
        if len(s) > max_len:
            return s[:max_len] + "..."
        return s
    except Exception:
        return "<repr-error>"


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def logged(level: int = logging.INFO, log_args: bool = True, log_result: bool = True) -> Any:
    """Decorator: logs enter/exit/latency for async functions.

    Logs function name, arguments (input), return value (output), and elapsed
    time.  Emits 'latency_ms' as a structured field in the JSON file log.

    Args:
        level:      Logging level (default INFO).
        log_args:   Whether to log function arguments (default True).
        log_result: Whether to log the return value (default True).
    """

    def decorator(fn: Any) -> Any:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            _logger = logging.getLogger(fn.__module__)
            _log_name = fn.__qualname__
            if log_args:
                _logger.log(
                    level,
                    "%s Enter %s — args=%s kwargs=%s",
                    _ENTER_MARKER,
                    _log_name,
                    _trunc_repr(args),
                    _trunc_repr(kwargs),
                )
            else:
                _logger.log(level, "%s Enter %s", _ENTER_MARKER, _log_name)
            t0 = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
                elapsed = (time.monotonic() - t0) * 1000
                if log_result:
                    _logger.log(
                        level,
                        "%s Exit %s %s %.0fms → %s",
                        _EXIT_MARKER,
                        _log_name,
                        _LATENCY_MARKER,
                        elapsed,
                        _trunc_repr(result),
                        extra={"latency_ms": int(elapsed)},
                    )
                else:
                    _logger.log(
                        level,
                        "%s Exit %s %s %.0fms",
                        _EXIT_MARKER,
                        _log_name,
                        _LATENCY_MARKER,
                        elapsed,
                        extra={"latency_ms": int(elapsed)},
                    )
                return result
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                _logger.log(
                    level,
                    "%s Fail %s %s %.0fms: %s",
                    _FAIL_MARKER,
                    _log_name,
                    _LATENCY_MARKER,
                    elapsed,
                    exc,
                    extra={"latency_ms": int(elapsed)},
                )
                raise

        return wrapper

    return decorator


def logged_sync(level: int = logging.INFO, log_args: bool = True, log_result: bool = True) -> Any:
    """Synchronous version of ``logged()`` — for non-async functions."""

    def decorator(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _logger = logging.getLogger(fn.__module__)
            _log_name = fn.__qualname__
            if log_args:
                _logger.log(
                    level,
                    "%s Enter %s — args=%s kwargs=%s",
                    _ENTER_MARKER,
                    _log_name,
                    _trunc_repr(args),
                    _trunc_repr(kwargs),
                )
            else:
                _logger.log(level, "%s Enter %s", _ENTER_MARKER, _log_name)
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                elapsed = (time.monotonic() - t0) * 1000
                if log_result:
                    _logger.log(
                        level,
                        "%s Exit %s %s %.0fms → %s",
                        _EXIT_MARKER,
                        _log_name,
                        _LATENCY_MARKER,
                        elapsed,
                        _trunc_repr(result),
                        extra={"latency_ms": int(elapsed)},
                    )
                else:
                    _logger.log(
                        level,
                        "%s Exit %s %s %.0fms",
                        _EXIT_MARKER,
                        _log_name,
                        _LATENCY_MARKER,
                        elapsed,
                        extra={"latency_ms": int(elapsed)},
                    )
                return result
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                _logger.log(
                    level,
                    "%s Fail %s %s %.0fms: %s",
                    _FAIL_MARKER,
                    _log_name,
                    _LATENCY_MARKER,
                    elapsed,
                    exc,
                    extra={"latency_ms": int(elapsed)},
                )
                raise

        return wrapper

    return decorator


def logged_class(level: int = logging.INFO, log_args: bool = True, log_result: bool = True) -> Any:
    """Class-level decorator: applies ``logged()`` to every public method.

    Skips dunder methods and abstract stubs.

    Usage::

        @logged_class()
        class MyService:
            async def do_work(self, x: int) -> str:
                ...
    """

    def class_decorator(cls: Any) -> Any:
        for attr_name in dir(cls):
            if attr_name.startswith("_") and attr_name not in ("__init__",):
                continue
            attr = getattr(cls, attr_name)
            if not callable(attr):
                continue
            if inspect.iscoroutinefunction(attr):
                setattr(
                    cls,
                    attr_name,
                    logged(level=level, log_args=log_args, log_result=log_result)(attr),
                )
            elif not inspect.isbuiltin(attr):
                setattr(
                    cls,
                    attr_name,
                    logged_sync(level=level, log_args=log_args, log_result=log_result)(attr),
                )
        return cls

    return class_decorator


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup_file_logging(service_name: str, level: int | None = None) -> None:
    """Configure the root logger to write to logs/<service_name>.log.

    Safe to call multiple times — duplicate handlers are skipped.
    StreamHandler uses ColoredFormatter on TTYs (plain text fallback when
    NO_COLOR is set or output is not a terminal); file handler uses JSON lines
    (ingestible by log aggregators without custom parsers, no ANSI codes).
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

    # Enable ANSI escape code support on Windows terminals.
    if sys.platform == "win32":
        try:
            import colorama
            colorama.just_fix_windows_console()
        except ImportError:
            pass  # degrade gracefully on environments without colorama

    sanitize = SanitizeFilter()

    # Console handler: colored on TTYs, plain fallback otherwise.
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler(sys.stderr)
        if _should_colorize(sys.stderr):
            sh.setFormatter(ColoredFormatter(service_name))
        else:
            plain_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
            sh.setFormatter(plain_fmt)
        sh.addFilter(sanitize)
        root.addHandler(sh)

    # File handler: JSON lines, 10 MB per file, keep 5 backups. Never colored.
    fh = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter(service_name))
    fh.addFilter(sanitize)
    root.addHandler(fh)

    # Suppress noisy third-party loggers to WARNING unless explicitly overridden.
    # Override individual libraries with LOG_LEVEL_HTTPX=DEBUG etc.
    _NOISY_LOGGERS = [
        "httpx",
        "httpcore",
        "urllib3",
        "asyncio",
        "aiosqlite",
        "chromadb",
        "sentence_transformers",
        "langfuse",
        "opentelemetry",
        "grpc",
    ]
    for _lib in _NOISY_LOGGERS:
        _lib_logger = logging.getLogger(_lib)
        _env_key = f"LOG_LEVEL_{_lib.upper().replace('.', '_')}"
        _override = os.environ.get(_env_key)
        if _override:
            _lib_logger.setLevel(getattr(logging, _override.upper(), logging.WARNING))
        elif _lib_logger.level == logging.NOTSET or _lib_logger.level < logging.WARNING:
            _lib_logger.setLevel(logging.WARNING)

    # OTel context-detach errors are harmless in async — the token was
    # created in a different asyncio.Task's contextvars.Context.
    _otel_ctx = logging.getLogger("opentelemetry.context")
    if not os.environ.get("LOG_LEVEL_OPENTELEMETRY_CONTEXT"):
        _otel_ctx.setLevel(logging.CRITICAL)
