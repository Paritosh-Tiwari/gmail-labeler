"""File-based logging setup for QuickLabel.

`serve` calls `setup_logging()` before uvicorn starts so application +
uvicorn logs go to `data/quicklabel.log` (rotating) in addition to
stdout. Means the terminal can close without losing debuggability —
just tail the log file to see what happened.

Rotation defaults: 5 MB per file, 3 backups -> ~20 MB cap.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3


def setup_logging(
    log_path: Path | str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
    level: int = logging.INFO,
) -> RotatingFileHandler:
    """Attach a rotating file handler to the root logger.

    Idempotent: calling twice with the same path replaces the prior
    handler instead of stacking duplicates (matters for in-process
    test reuse).
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_path:
            root.removeHandler(h)
            h.close()
    root.addHandler(handler)
    return handler
