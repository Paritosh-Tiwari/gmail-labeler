"""Runtime settings for QuickLabel.

Resolution order for each setting:
  1. environment variable (launch-time override, useful for dev)
  2. settings.json on disk (data/settings.json — edited via /settings)
  3. hardcoded default

The /settings page reads + writes the JSON file. Env vars stay as an
escape hatch for one-off overrides without editing the file (e.g.
`$env:QUICKLABEL_PORT=8766` to dodge a port collision).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

# HOST must be a loopback address. middleware.HostValidationMiddleware
# allows only 127.0.0.1 / localhost / [::1] in the Host header, and this
# bind value has to stay symmetric. Do NOT change to 0.0.0.0 or a LAN IP
# without also widening the allow-list — otherwise the server becomes
# reachable from the network and the rebinding defense becomes moot.
HOST = "127.0.0.1"

DEFAULT_PORT = 8765
_MIN_PORT, _MAX_PORT = 1024, 65535
DEFAULT_LLM_MODEL = "gpt-oss:20b"
DEFAULT_LOG_LEVEL = "INFO"

_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "data" / "settings.json"


@dataclass
class Settings:
    port: int = DEFAULT_PORT
    llm_model: str = DEFAULT_LLM_MODEL
    log_level: str = DEFAULT_LOG_LEVEL


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _coerce_port(value: int) -> int:
    """Clamp a port to the unprivileged-TCP range or fall back to default.
    Avoids uvicorn crashing on out-of-range values from a stale settings
    file or a typo in QUICKLABEL_PORT."""
    if _MIN_PORT <= value <= _MAX_PORT:
        return value
    return DEFAULT_PORT


def load_settings(settings_path: Path = _SETTINGS_PATH) -> Settings:
    """Build a Settings object from file + env overrides + defaults."""
    data = _read_json(settings_path)
    s = Settings(
        port=_coerce_port(int(data.get("port", DEFAULT_PORT))),
        llm_model=str(data.get("llm_model", DEFAULT_LLM_MODEL)),
        log_level=str(data.get("log_level", DEFAULT_LOG_LEVEL)),
    )
    # Env-var overrides (silently ignore garbage so a bad shell env
    # doesn't brick the server)
    if "QUICKLABEL_PORT" in os.environ:
        try:
            s.port = _coerce_port(int(os.environ["QUICKLABEL_PORT"]))
        except ValueError:
            pass
    if "QUICKLABEL_LLM_MODEL" in os.environ:
        v = os.environ["QUICKLABEL_LLM_MODEL"].strip()
        if v:
            s.llm_model = v
    if "QUICKLABEL_LOG_LEVEL" in os.environ:
        v = os.environ["QUICKLABEL_LOG_LEVEL"].strip().upper()
        if v:
            s.log_level = v
    return s


def save_settings(settings: Settings, settings_path: Path = _SETTINGS_PATH) -> None:
    """Atomic write of settings to disk."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(settings), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(settings_path)


# Module-level mirrors for code that still imports `PORT` directly.
# These reflect the values at import time; the /settings page warns
# the user that changes require a restart.
_initial = load_settings()
PORT = _initial.port
BASE_URL = f"http://{HOST}:{PORT}"
