import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Logs directory: one level up from shared/ at project-root/logs/
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup_file_logging(service_name: str, level: int = logging.INFO) -> None:
    """Configure the root logger to write to logs/<service_name>.log.

    Safe to call multiple times — duplicate handlers are skipped.
    Adds both a StreamHandler (stderr) and a RotatingFileHandler.
    """
    _LOGS_DIR.mkdir(exist_ok=True)
    log_path = _LOGS_DIR / f"{service_name}.log"

    root = logging.getLogger()
    root.setLevel(level)

    # Skip if a RotatingFileHandler for this exact path is already registered.
    # This makes setup_file_logging idempotent — safe to call from every
    # service's startup without risk of duplicate log lines.
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and h.baseFilename == str(log_path):
            return

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    # Add console handler only if none present yet
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # Rotating file handler: 10 MB per file, keep 5 backups (oldest rotated out).
    fh = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
