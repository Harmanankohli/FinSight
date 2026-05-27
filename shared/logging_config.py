import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
        # Structured extras picked up when set on the LogRecord (e.g. by #13 correlation IDs).
        for k in ("trace_id", "session_id", "ticker", "latency_ms"):
            val = getattr(record, k, None)
            if val is not None:
                payload[k] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


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

    # Console handler: plaintext so terminal output stays readable.
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(plain_fmt)
        root.addHandler(sh)

    # File handler: JSON lines, 10 MB per file, keep 5 backups.
    fh = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(JsonFormatter(service_name))
    root.addHandler(fh)
