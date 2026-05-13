"""Inspect the local Ollama install for the /settings page.

Two questions /settings needs to answer:
  1. Which models does the user have pulled? (Surfaced in a datalist
     so they pick from what they've actually installed.)
  2. Does the chosen model actually respond? (The "Test" button.)

Both calls swallow Ollama-unreachable errors and return a benign
fallback so the settings page renders even when Ollama is down --
the user gets to SEE the error rather than getting a blank screen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import ollama


@dataclass
class ModelInfo:
    name: str
    size_bytes: int = 0  # 0 = unknown / not reported

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / (1024 ** 3), 1) if self.size_bytes else 0.0


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: int = 0
    response: str = ""
    error: str = ""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read an attribute or dict key, supporting both pydantic
    Ollama responses (newer lib) and bare dicts (older lib / mocks)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def installed_models(client: ollama.Client | None = None) -> list[ModelInfo]:
    """List models in local Ollama. Returns [] if Ollama is unreachable
    -- callers should treat empty as 'we don't know', not 'definitely
    none installed'."""
    try:
        c = client or ollama.Client()
        resp = c.list()
    except Exception:
        return []

    models = _get(resp, "models") or []
    out: list[ModelInfo] = []
    for m in models:
        # Newer ollama lib uses .model; older used .name.
        name = _get(m, "model") or _get(m, "name") or ""
        size = _get(m, "size") or 0
        if name:
            out.append(ModelInfo(name=str(name), size_bytes=int(size) if size else 0))
    return out


def is_model_installed(name: str, client: ollama.Client | None = None) -> bool:
    if not name or not name.strip():
        return False
    target = name.strip().lower()
    return any(m.name.strip().lower() == target for m in installed_models(client))


def probe_model(name: str, client: ollama.Client | None = None) -> ProbeResult:
    """Send a tiny prompt and time the response. ok=True iff the model
    returned non-empty text. Used by /settings to verify that the chosen
    model actually responds before the user commits to it.

    Reasoning models (qwen3*, gpt-oss) emit hidden <think> tokens before
    visible output. A tiny num_predict gets burned on thinking and the
    visible response is empty — falsely failing the probe. We apply the
    qwen3 /no_think shim and use a generous num_predict so the probe
    answers the question 'does this model respond at all' rather than
    'can this model think AND respond in 8 tokens'.
    """
    if not name or not name.strip():
        return ProbeResult(ok=False, error="No model specified")

    target = name.strip()
    user_prompt = "Reply with exactly: OK"
    if target.lower().startswith("qwen3"):
        user_prompt = user_prompt + "\n\n/no_think"

    c = client or ollama.Client()
    started = time.monotonic()
    try:
        resp = c.chat(
            model=target,
            messages=[{"role": "user", "content": user_prompt}],
            options={"num_predict": 256, "temperature": 0},
        )
    except Exception as e:
        return ProbeResult(ok=False, error=str(e))

    latency_ms = int((time.monotonic() - started) * 1000)
    msg = _get(resp, "message") or {}
    text = _get(msg, "content") or ""
    text = (text or "").strip()
    if not text:
        return ProbeResult(
            ok=False, latency_ms=latency_ms,
            error="Model returned empty response. Try `ollama show <model>` "
                  "to confirm it's pulled, or check Ollama logs.",
        )
    return ProbeResult(ok=True, latency_ms=latency_ms, response=text)
