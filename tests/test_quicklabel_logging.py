"""Tests for the rotating file logger setup."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from quicklabel._logging import setup_logging


@pytest.fixture(autouse=True)
def _clean_root_logger():
    """Snapshot root handlers + level around each test so logging state
    doesn't leak between tests in the same pytest process."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        if hasattr(h, "close"):
            try:
                h.close()
            except Exception:
                pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_setup_creates_log_file_and_writes(tmp_path: Path):
    log = tmp_path / "ql.log"
    setup_logging(log)
    logging.getLogger("quicklabel.test").info("hello world")
    for h in logging.getLogger().handlers:
        if isinstance(h, RotatingFileHandler):
            h.flush()
    assert log.exists()
    content = log.read_text(encoding="utf-8")
    assert "hello world" in content
    assert "quicklabel.test" in content


def test_setup_creates_parent_dir(tmp_path: Path):
    log = tmp_path / "deep" / "subdir" / "ql.log"
    setup_logging(log)
    logging.getLogger("x").info("ping")
    for h in logging.getLogger().handlers:
        if isinstance(h, RotatingFileHandler):
            h.flush()
    assert log.exists()


def test_setup_is_idempotent_for_same_path(tmp_path: Path):
    """Calling setup_logging twice with the same path doesn't stack."""
    log = tmp_path / "ql.log"
    setup_logging(log)
    setup_logging(log)
    setup_logging(log)
    handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, RotatingFileHandler)
        and Path(h.baseFilename) == log
    ]
    assert len(handlers) == 1


def test_setup_for_different_paths_keeps_both(tmp_path: Path):
    """Different log paths are different handlers — don't blow away an
    unrelated one."""
    log_a = tmp_path / "a.log"
    log_b = tmp_path / "b.log"
    setup_logging(log_a)
    setup_logging(log_b)
    paths = {
        Path(h.baseFilename) for h in logging.getLogger().handlers
        if isinstance(h, RotatingFileHandler)
    }
    assert log_a in paths
    assert log_b in paths


def test_rotation_triggers_at_size(tmp_path: Path):
    """When the file exceeds maxBytes, a backup is written."""
    log = tmp_path / "ql.log"
    setup_logging(log, max_bytes=200, backup_count=2)
    log_obj = logging.getLogger("quicklabel.rot")
    for i in range(50):
        log_obj.info("x" * 50 + f" line {i}")
    for h in logging.getLogger().handlers:
        if isinstance(h, RotatingFileHandler):
            h.flush()
    backup = tmp_path / "ql.log.1"
    assert backup.exists(), f"expected backup file at {backup}"
