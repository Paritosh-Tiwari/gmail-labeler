"""Extract a plain-text body from a Gmail API message dict.

Walks the (possibly nested) MIME tree, decodes base64url payloads, and
prefers text/plain over HTML. Falls back to a cheap HTML→text strip when
only HTML is present. Quoted reply blocks are heuristically removed.
"""
from __future__ import annotations

import base64
import re


_QUOTE_HEADER_RE = re.compile(
    r"^\s*On\s+.+\s+wrote:\s*$",  # "On <date>, <name> <email> wrote:"
    flags=re.IGNORECASE | re.MULTILINE,
)
_QUOTED_LINE_RE = re.compile(r"^\s*>", flags=re.MULTILINE)


def _b64url_decode(data: str | None) -> str:
    if not data:
        return ""
    # Gmail uses URL-safe base64 with no padding
    pad = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode((data + pad).encode("ascii"))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    """Cheap HTML→text. Good enough for prompts; not for rendering."""
    # Drop script/style content entirely
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    # Convert <br> and <p> closes to newlines so paragraphs survive
    html = re.sub(r"</?(br|p|div|li|tr)\s*/?>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode common entities
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    # Collapse whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _strip_quoted_replies(text: str) -> str:
    """Heuristic: drop everything from the first 'On ... wrote:' line onward,
    or trim trailing '>'-prefixed quote lines."""
    m = _QUOTE_HEADER_RE.search(text)
    if m:
        text = text[: m.start()].rstrip()

    # Drop trailing quoted-line block ('>' prefix) at end of message
    lines = text.split("\n")
    while lines and _QUOTED_LINE_RE.match(lines[-1] or ""):
        lines.pop()
    return "\n".join(lines).rstrip()


def _walk(part: dict) -> tuple[str | None, str | None]:
    """Walk a payload subtree. Return (best_plain, best_html) found.

    Treats decode failures (empty result from non-empty data) as 'no
    content here' so siblings can still contribute. Without this, a
    base64-corrupted text/plain part would shadow a perfectly good
    text/html sibling and the whole body would come back empty.
    """
    mime = (part.get("mimeType") or "").lower()
    body_data = (part.get("body") or {}).get("data")

    if mime == "text/plain" and body_data:
        decoded = _b64url_decode(body_data)
        if decoded:
            return decoded, None
        # else: fall through to walk siblings
    if mime == "text/html" and body_data:
        decoded = _b64url_decode(body_data)
        if decoded:
            return None, decoded

    plain: str | None = None
    html: str | None = None
    for child in part.get("parts") or []:
        p, h = _walk(child)
        plain = plain or p
        html = html or h
        if plain and html:
            break
    return plain, html


def extract_body(message: dict, max_chars: int = 3000) -> str:
    """Extract plain-text body from a Gmail message dict (format=full)."""
    payload = message.get("payload") or {}
    plain, html = _walk(payload)

    text = plain if plain else (_strip_html(html) if html else "")
    text = _strip_quoted_replies(text)
    # Final whitespace tidy
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text
