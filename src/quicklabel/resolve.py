"""Resolve any Gmail email identifier to a full message dict.

Three accepted forms (in order):
  1. API thread ID — 12-16 char lowercase hex, e.g. '19e0a308065b6875'.
  2. API message ID — same shape, used for individual messages.
  3. URL permalink ID — base-40 obfuscated, e.g. 'FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX',
     pulled from the Gmail web URL hash. Decoded via gmail_id.decode_permalink.
"""
from __future__ import annotations

from googleapiclient.errors import HttpError

from .gmail_id import decode_permalink, looks_like_api_id, looks_like_permalink


def resolve_to_message(service, id_str: str) -> dict:
    """Resolve any of the three ID forms above to a full Gmail message dict."""
    if not id_str:
        raise ValueError("empty id_str")

    # If it's a permalink, decode to the API thread ID first
    candidates: list[str] = []
    if looks_like_permalink(id_str):
        decoded = decode_permalink(id_str)
        if decoded:
            candidates.append(decoded)
    if looks_like_api_id(id_str):
        candidates.append(id_str)
    # As a last resort, try the raw string (covers shapes we didn't anticipate)
    if id_str not in candidates:
        candidates.append(id_str)

    last_error: Exception | None = None
    for cand in candidates:
        # Try thread first, then message
        try:
            thread = service.users().threads().get(
                userId="me", id=cand, format="full",
            ).execute()
            msgs = thread.get("messages", [])
            if msgs:
                return msgs[-1]
        except HttpError as e:
            last_error = e
            if e.resp.status not in (404, 400):
                raise

        try:
            return service.users().messages().get(
                userId="me", id=cand, format="full",
            ).execute()
        except HttpError as e:
            last_error = e
            if e.resp.status not in (404, 400):
                raise

    raise RuntimeError(
        f"Could not resolve {id_str!r} as thread or message ID "
        f"(tried: {candidates}). Last error: {last_error}"
    )
