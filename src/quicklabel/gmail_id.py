"""Decode Gmail URL permalink IDs (e.g. 'FMfcgzQgLj...') to API thread IDs.

Gmail's web URL fragments use a base-40 obfuscated encoding distinct from
the lowercase-hex thread IDs the Gmail API accepts. This decoder is the
fallback for cases where the bookmarklet couldn't read the DOM
(`h2.hP[data-legacy-thread-id]`) and had to grab the permalink from the URL.

Algorithm reverse-engineered by Arsenal Recon (2019):
  1. Treat the 32-char permalink as a base-40 big-endian number, alphabet
     'BCDFGHJKLMNPQRSTVWXZbcdfghjklmnpqrstvwxz' (consonants only).
  2. Convert to hex.
  3. Decode hex bytes as ASCII -> a string like 'f:1864698804158474742'.
  4. Take the decimal after 'f:' and convert to lowercase hex.
"""
from __future__ import annotations

import re

_BASE40_ALPHABET = "BCDFGHJKLMNPQRSTVWXZbcdfghjklmnpqrstvwxz"
_BASE40_INDEX = {c: i for i, c in enumerate(_BASE40_ALPHABET)}

# API thread IDs are 12-16 char lowercase hex
_API_ID_RE = re.compile(r"^[0-9a-f]{12,16}$")
# URL permalink IDs are 24-40 chars from the base-40 alphabet
_PERMALINK_RE = re.compile(r"^[BCDFGHJKLMNPQRSTVWXZbcdfghjklmnpqrstvwxz]{20,}$")


def looks_like_api_id(s: str) -> bool:
    return bool(_API_ID_RE.match(s))


def looks_like_permalink(s: str) -> bool:
    return bool(_PERMALINK_RE.match(s))


def decode_permalink(url_id: str) -> str | None:
    """Decode a Gmail URL permalink ID to the API-compatible lowercase-hex thread ID.

    Returns None if `url_id` doesn't fit the expected base-40 shape or doesn't
    decode to an 'f:<digits>' payload.
    """
    if not looks_like_permalink(url_id):
        return None

    # base-40 -> int
    n = 0
    for c in url_id:
        n = n * 40 + _BASE40_INDEX[c]

    # int -> hex bytes
    hex_str = format(n, "x")
    if len(hex_str) % 2:
        hex_str = "0" + hex_str
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return None

    text = raw.decode("ascii", errors="replace")
    m = re.search(r"f:(\d+)", text)
    if not m:
        return None

    try:
        thread_int = int(m.group(1))
    except ValueError:
        return None

    return format(thread_int, "x")
