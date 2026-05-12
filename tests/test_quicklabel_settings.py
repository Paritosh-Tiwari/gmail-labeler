"""Tests for settings.py: file + env precedence, atomic save, roundtrip."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quicklabel.settings import (
    DEFAULT_LLM_MODEL,
    DEFAULT_LOG_LEVEL,
    DEFAULT_PORT,
    Settings,
    load_settings,
    save_settings,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Strip QUICKLABEL_* env vars so each test starts clean."""
    for k in ("QUICKLABEL_PORT", "QUICKLABEL_LLM_MODEL", "QUICKLABEL_LOG_LEVEL"):
        monkeypatch.delenv(k, raising=False)


def test_load_returns_defaults_when_file_missing(tmp_path: Path):
    s = load_settings(tmp_path / "missing.json")
    assert s.port == DEFAULT_PORT
    assert s.llm_model == DEFAULT_LLM_MODEL
    assert s.log_level == DEFAULT_LOG_LEVEL


def test_load_reads_file_values(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "port": 9000, "llm_model": "qwen2.5:7b-instruct", "log_level": "DEBUG"
    }))
    s = load_settings(p)
    assert s.port == 9000
    assert s.llm_model == "qwen2.5:7b-instruct"
    assert s.log_level == "DEBUG"


def test_env_overrides_file(tmp_path: Path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"port": 9000, "llm_model": "from-file", "log_level": "INFO"}))
    monkeypatch.setenv("QUICKLABEL_PORT", "9999")
    monkeypatch.setenv("QUICKLABEL_LLM_MODEL", "from-env")
    monkeypatch.setenv("QUICKLABEL_LOG_LEVEL", "warning")
    s = load_settings(p)
    assert s.port == 9999
    assert s.llm_model == "from-env"
    assert s.log_level == "WARNING"


def test_env_invalid_port_silently_ignored(tmp_path: Path, monkeypatch):
    """Garbage env shouldn't brick the server; it falls back to file/default."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"port": 9000}))
    monkeypatch.setenv("QUICKLABEL_PORT", "not-a-number")
    s = load_settings(p)
    assert s.port == 9000


def test_load_handles_corrupt_json(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text("not valid json {")
    s = load_settings(p)
    # Falls back to defaults rather than crashing
    assert s.port == DEFAULT_PORT
    assert s.llm_model == DEFAULT_LLM_MODEL


def test_save_creates_file_and_parents(tmp_path: Path):
    p = tmp_path / "deep" / "settings.json"
    save_settings(Settings(port=9001, llm_model="m", log_level="DEBUG"), p)
    assert p.exists()
    payload = json.loads(p.read_text())
    assert payload == {"llm_model": "m", "log_level": "DEBUG", "port": 9001}


def test_save_then_load_roundtrip(tmp_path: Path):
    p = tmp_path / "settings.json"
    original = Settings(port=8080, llm_model="qwen2.5:7b-instruct", log_level="WARNING")
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded == original


def test_save_overwrites_atomically(tmp_path: Path):
    """Save twice with different content; tmp file should not survive."""
    p = tmp_path / "settings.json"
    save_settings(Settings(port=1111), p)
    save_settings(Settings(port=2222), p)
    loaded = load_settings(p)
    assert loaded.port == 2222
    # No leftover .json.tmp from the atomic-write dance
    assert not (tmp_path / "settings.json.tmp").exists()


def test_empty_env_var_does_not_override(tmp_path: Path, monkeypatch):
    """An empty env value shouldn't blank out the file's setting."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"llm_model": "from-file"}))
    monkeypatch.setenv("QUICKLABEL_LLM_MODEL", "   ")
    s = load_settings(p)
    assert s.llm_model == "from-file"
