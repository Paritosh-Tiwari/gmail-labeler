"""Thin wrapper over the ollama Python client.

All LLM inference is local via Ollama. No cloud LLM APIs.
"""
from __future__ import annotations

import ollama

OLLAMA_HOST = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
LLM_MODEL = "qwen2.5:14b-instruct-q5_K_M"


def get_client() -> ollama.Client:
    return ollama.Client(host=OLLAMA_HOST)


def verify_running(client: ollama.Client | None = None) -> list[str]:
    """Return list of model names available to Ollama. Raises if unreachable."""
    client = client or get_client()
    resp = client.list()
    return [m.get("name") or m.get("model") or "" for m in resp.get("models", [])]


def embed_batch(client: ollama.Client, texts: list[str],
                model: str = EMBED_MODEL) -> list[list[float]]:
    """Embed a batch of texts in a single Ollama call. Returns N x D vectors."""
    resp = client.embed(model=model, input=texts)
    return resp["embeddings"]


def chat(client: ollama.Client, prompt: str, model: str = LLM_MODEL,
         temperature: float = 0.2, num_predict: int = 256,
         num_ctx: int | None = None,
         system: str | None = None) -> str:
    """Single-turn chat with the local LLM. Returns the assistant message text.

    `num_ctx` is the Ollama context window in tokens. Default Ollama is
    2048 — small enough that a system prompt + 3KB body + label list
    will leave no room for generation and return an empty string. Pass
    e.g. 16384 for QuickLabel-style prompts.
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    options: dict = {"temperature": temperature, "num_predict": num_predict}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    resp = client.chat(
        model=model,
        messages=messages,
        options=options,
    )
    return resp["message"]["content"]
