"""Tests for llm_status: installed_models, is_model_installed, test_model.

Use a fake Ollama client (duck-typed, no network) so tests don't depend
on a running Ollama. Cover both dict-style and pydantic-style responses
since the lib has shipped both shapes.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from quicklabel.llm_status import (
    ModelInfo,
    installed_models,
    is_model_installed,
    probe_model,
)


# --------------------------- fake Ollama client ---------------------------

@dataclass
class _FakeModelObj:
    """Mimics the pydantic ListResponse.Model the real lib returns."""
    model: str
    size: int = 0


class _FakeListRespObj:
    def __init__(self, models):
        self.models = models


class _FakeMessageObj:
    def __init__(self, content):
        self.content = content


class _FakeChatRespObj:
    def __init__(self, content):
        self.message = _FakeMessageObj(content)


class _FakeOllamaClient:
    """Captures calls + returns canned responses."""

    def __init__(self, *, list_response=None, list_raises=None,
                 chat_response=None, chat_raises=None):
        self._list_response = list_response
        self._list_raises = list_raises
        self._chat_response = chat_response
        self._chat_raises = chat_raises
        self.list_calls = 0
        self.chat_calls: list[dict] = []

    def list(self):
        self.list_calls += 1
        if self._list_raises:
            raise self._list_raises
        return self._list_response

    def chat(self, model, messages, options):
        self.chat_calls.append({"model": model, "messages": messages, "options": options})
        if self._chat_raises:
            raise self._chat_raises
        return self._chat_response


# --------------------------- installed_models ---------------------------

def test_installed_models_pydantic_response():
    client = _FakeOllamaClient(list_response=_FakeListRespObj([
        _FakeModelObj(model="gpt-oss:20b", size=13_000_000_000),
        _FakeModelObj(model="qwen2.5:7b-instruct", size=5_000_000_000),
    ]))
    out = installed_models(client)
    assert [m.name for m in out] == ["gpt-oss:20b", "qwen2.5:7b-instruct"]
    assert out[0].size_bytes == 13_000_000_000
    assert out[0].size_gb == 12.1  # 13_000_000_000 / (1024**3) rounded to 1


def test_installed_models_dict_response():
    """Older ollama lib returned dicts. Helper should still work."""
    client = _FakeOllamaClient(list_response={
        "models": [
            {"model": "gpt-oss:20b", "size": 13_000_000_000},
            {"name": "old-style:7b", "size": 5_000_000_000},
        ]
    })
    out = installed_models(client)
    assert {m.name for m in out} == {"gpt-oss:20b", "old-style:7b"}


def test_installed_models_handles_missing_size():
    client = _FakeOllamaClient(list_response={
        "models": [{"model": "no-size:1b"}]
    })
    out = installed_models(client)
    assert out[0].name == "no-size:1b"
    assert out[0].size_bytes == 0
    assert out[0].size_gb == 0.0


def test_installed_models_swallows_connection_error():
    """Ollama unreachable -> empty list, no exception bubbles up."""
    client = _FakeOllamaClient(list_raises=ConnectionError("connection refused"))
    out = installed_models(client)
    assert out == []


def test_installed_models_swallows_arbitrary_error():
    client = _FakeOllamaClient(list_raises=RuntimeError("boom"))
    assert installed_models(client) == []


def test_installed_models_skips_entries_without_name():
    client = _FakeOllamaClient(list_response={
        "models": [
            {"size": 100},  # no name -- skip
            {"model": "real:1b", "size": 200},
        ]
    })
    out = installed_models(client)
    assert [m.name for m in out] == ["real:1b"]


# --------------------------- is_model_installed ---------------------------

def test_is_model_installed_exact_match():
    client = _FakeOllamaClient(list_response={
        "models": [{"model": "gpt-oss:20b"}]
    })
    assert is_model_installed("gpt-oss:20b", client) is True


def test_is_model_installed_case_insensitive():
    client = _FakeOllamaClient(list_response={
        "models": [{"model": "GPT-OSS:20b"}]
    })
    assert is_model_installed("gpt-oss:20b", client) is True


def test_is_model_installed_no_match():
    client = _FakeOllamaClient(list_response={
        "models": [{"model": "gpt-oss:20b"}]
    })
    assert is_model_installed("qwen2.5:7b-instruct", client) is False


def test_is_model_installed_empty_name():
    """Empty / whitespace name should never match anything."""
    client = _FakeOllamaClient(list_response={
        "models": [{"model": "anything"}]
    })
    assert is_model_installed("", client) is False
    assert is_model_installed("   ", client) is False


def test_is_model_installed_when_ollama_down():
    client = _FakeOllamaClient(list_raises=ConnectionError())
    assert is_model_installed("gpt-oss:20b", client) is False


# --------------------------- test_model ---------------------------

def test_test_model_success_pydantic():
    client = _FakeOllamaClient(chat_response=_FakeChatRespObj("OK"))
    r = probe_model("gpt-oss:20b", client)
    assert r.ok is True
    assert r.response == "OK"
    assert r.error == ""
    assert r.latency_ms >= 0
    assert client.chat_calls[0]["model"] == "gpt-oss:20b"
    assert client.chat_calls[0]["options"]["temperature"] == 0


def test_test_model_success_dict():
    client = _FakeOllamaClient(chat_response={"message": {"content": "Sure: OK"}})
    r = probe_model("any:1b", client)
    assert r.ok is True
    assert r.response == "Sure: OK"


def test_test_model_strips_whitespace_from_response():
    client = _FakeOllamaClient(chat_response={"message": {"content": "  OK  \n"}})
    r = probe_model("any:1b", client)
    assert r.ok is True
    assert r.response == "OK"


def test_test_model_empty_response_treated_as_failure():
    """Some models with too-small num_ctx return an empty content -- a
    classic gotcha. We surface it as a failure so the user knows."""
    client = _FakeOllamaClient(chat_response={"message": {"content": ""}})
    r = probe_model("broken:1b", client)
    assert r.ok is False
    assert "empty" in r.error.lower()
    assert r.latency_ms >= 0  # we still record how long it took


def test_test_model_chat_exception_surfaced_as_error():
    """Ollama raises (e.g. model not pulled) -> ok=False with the error."""
    client = _FakeOllamaClient(chat_raises=Exception("model 'foo' not found"))
    r = probe_model("foo", client)
    assert r.ok is False
    assert "not found" in r.error
    assert r.latency_ms == 0  # raised before timing the response


def test_test_model_empty_name_short_circuits():
    """No model -> immediate failure, no chat() call."""
    client = _FakeOllamaClient(chat_response=_FakeChatRespObj("never reached"))
    r = probe_model("", client)
    assert r.ok is False
    assert "no model" in r.error.lower()
    assert client.chat_calls == []


def test_test_model_strips_input_name():
    """User might paste a name with surrounding whitespace -- strip it."""
    client = _FakeOllamaClient(chat_response=_FakeChatRespObj("OK"))
    r = probe_model("  gpt-oss:20b  ", client)
    assert r.ok is True
    assert client.chat_calls[0]["model"] == "gpt-oss:20b"


# --------------------------- ModelInfo ---------------------------

def test_model_info_size_gb_rounding():
    assert ModelInfo("x", 0).size_gb == 0.0
    assert ModelInfo("x", 1024 ** 3).size_gb == 1.0
    assert ModelInfo("x", int(2.5 * 1024 ** 3)).size_gb == 2.5
