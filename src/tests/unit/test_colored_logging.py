"""Unit tests for the colored logging system in shared/logging_config.py."""

import io
import json
import logging

import pytest

from shared.logging_config import (
    _ENTER_MARKER,
    _EXIT_MARKER,
    _FAIL_MARKER,
    _LEVEL_COLORS,
    _SERVICE_COLORS,
    _TIMESTAMP_COLOR,
    ColoredFormatter,
    JsonFormatter,
    SanitizeFilter,
    _should_colorize,
    logged_sync,
)

# ---------------------------------------------------------------------------
# _should_colorize
# ---------------------------------------------------------------------------


def test_should_colorize_no_color_env_var(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    stream = io.StringIO()
    assert _should_colorize(stream) is False


def test_should_colorize_force_color_env_var(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = io.StringIO()  # StringIO.isatty() returns False
    assert _should_colorize(stream) is True


def test_should_colorize_non_tty_returns_false(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    stream = io.StringIO()
    assert _should_colorize(stream) is False


# ---------------------------------------------------------------------------
# ColoredFormatter — level colors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level,levelno", [
    ("DEBUG",    logging.DEBUG),
    ("INFO",     logging.INFO),
    ("WARNING",  logging.WARNING),
    ("ERROR",    logging.ERROR),
    ("CRITICAL", logging.CRITICAL),
])
def test_colored_formatter_applies_level_colors(level, levelno):
    formatter = ColoredFormatter()
    record = logging.LogRecord(
        name="test.logger", level=levelno, pathname="", lineno=0,
        msg=f"test {level} message", args=(), exc_info=None,
    )
    output = formatter.format(record)
    expected_color = _LEVEL_COLORS[levelno]
    assert expected_color in output, f"Expected {expected_color!r} for {level} in {output!r}"


def test_colored_formatter_dims_timestamp():
    formatter = ColoredFormatter()
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert _TIMESTAMP_COLOR in output


# ---------------------------------------------------------------------------
# ColoredFormatter — service badge
# ---------------------------------------------------------------------------


def test_colored_formatter_service_badge_rag():
    formatter = ColoredFormatter(service_name="rag")
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "[rag]" in output
    assert _SERVICE_COLORS["rag"] in output  # blue


def test_colored_formatter_no_badge_when_no_service():
    formatter = ColoredFormatter()
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "[" not in output or "test.logger" not in output.split("[")[0]


# ---------------------------------------------------------------------------
# JsonFormatter — must never contain ANSI codes
# ---------------------------------------------------------------------------


def test_json_formatter_no_ansi_codes():
    formatter = JsonFormatter(service_name="orchestrator")
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg="no color here", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "\033" not in output, "ANSI escape found in JSON log output"
    # Must be valid JSON
    parsed = json.loads(output)
    assert parsed["message"] == "no color here"
    assert parsed["level"] == "INFO"
    assert parsed["service"] == "orchestrator"


def test_json_formatter_no_ansi_with_decorator_markers():
    """Decorator markers (Unicode) are fine in JSON; ANSI codes are not."""
    formatter = JsonFormatter(service_name="quant")
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg=f"{_ENTER_MARKER} Enter MyService.do_work",
        args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "\033" not in output
    parsed = json.loads(output)
    assert _ENTER_MARKER in parsed["message"]


# ---------------------------------------------------------------------------
# SanitizeFilter — works with ColoredFormatter
# ---------------------------------------------------------------------------


def test_sanitize_filter_scrubs_api_key_before_coloring():
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg="api_key='sk-supersecretkey12345678'",
        args=(), exc_info=None,
    )
    sanitize = SanitizeFilter()
    sanitize.filter(record)
    formatter = ColoredFormatter(service_name="rag")
    output = formatter.format(record)
    assert "supersecretkey" not in output
    assert "***" in output
    assert "\033" in output  # colors still present


# ---------------------------------------------------------------------------
# Decorator lifecycle markers
# ---------------------------------------------------------------------------


def test_logged_sync_enter_marker():
    log_records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record)

    # logged_sync uses getLogger(fn.__module__); attach to root so propagation catches it
    handler = _Handler()
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)

    @logged_sync()
    def my_func(x: int) -> int:
        return x * 2

    try:
        my_func(5)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    messages = [r.getMessage() for r in log_records]
    enter_msgs = [m for m in messages if m.startswith(_ENTER_MARKER)]
    exit_msgs = [m for m in messages if m.startswith(_EXIT_MARKER)]
    assert enter_msgs, f"No enter marker found. Messages: {messages}"
    assert exit_msgs, f"No exit marker found. Messages: {messages}"


def test_logged_sync_fail_marker():
    log_records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record)

    handler = _Handler()
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)

    @logged_sync()
    def broken_func() -> None:
        raise ValueError("oops")

    try:
        with pytest.raises(ValueError):
            broken_func()
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    messages = [r.getMessage() for r in log_records]
    fail_msgs = [m for m in messages if m.startswith(_FAIL_MARKER)]
    assert fail_msgs, f"No fail marker found. Messages: {messages}"


# ---------------------------------------------------------------------------
# ColoredFormatter — decorator-aware coloring
# ---------------------------------------------------------------------------


def test_colored_formatter_enter_message_uses_cyan():
    formatter = ColoredFormatter(service_name="orchestrator")
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg=f"{_ENTER_MARKER} Enter MyService.do_work — args=() kwargs={{}}",
        args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "\033[36m" in output  # cyan for enter


def test_colored_formatter_fail_message_uses_bold_red():
    formatter = ColoredFormatter(service_name="orchestrator")
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="", lineno=0,
        msg=f"{_FAIL_MARKER} Fail MyService.do_work ⏱ 42ms: ValueError",
        args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "\033[1;31m" in output  # bold red for fail
