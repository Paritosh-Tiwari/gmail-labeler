"""Pure header parsing — no I/O, easily unit testable."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EmailFingerprint:
    """Structured view of the bits of an email we care about for proposing
    a filter and a label."""
    msg_id: str
    sender_email: str
    sender_name: str | None
    subject: str
    list_id: str | None
    list_unsubscribe: bool
    # Gmail's own categorization (Primary/Social/Promotions/Updates/Forums)
    # if it tagged this message. Free signal — used as a prior in the LLM
    # prompt. None when Gmail didn't apply any CATEGORY_* system label.
    gmail_category: str | None = None


# Gmail system labels -> human-friendly category names. Categories not in
# this map are dropped (we don't surface CHAT, IMPORTANT, STARRED, etc.
# as "categories" — those have separate semantics).
_GMAIL_CATEGORY_MAP = {
    "CATEGORY_PERSONAL": "Primary",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}


def _extract_gmail_category(label_ids: list[str]) -> str | None:
    """First CATEGORY_* system label in `label_ids`, mapped to its name."""
    for lid in label_ids or []:
        if lid in _GMAIL_CATEGORY_MAP:
            return _GMAIL_CATEGORY_MAP[lid]
    return None


_ADDR_RE = re.compile(r"<([^>]+)>")
_LIST_ID_RE = re.compile(r"<([^>]+)>")  # List-ID: "Name" <list-id-here>


def parse_address(raw: str) -> tuple[str | None, str]:
    """'Sequoia <a@b.com>' -> ('Sequoia', 'a@b.com'); 'a@b.com' -> (None, 'a@b.com')."""
    if not raw:
        return None, ""
    m = _ADDR_RE.search(raw)
    if m:
        addr = m.group(1).strip()
        name = raw[: m.start()].strip().strip('"').strip()
        return (name or None), addr.lower()
    return None, raw.strip().lower()


def parse_list_id(raw: str) -> str | None:
    """Extract bare list-id from a List-ID header value.
    'My List <foo.example.com>' -> 'foo.example.com'.
    Bare 'foo.example.com' -> 'foo.example.com'."""
    if not raw:
        return None
    m = _LIST_ID_RE.search(raw)
    if m:
        return m.group(1).strip().lower()
    return raw.strip().lower() or None


def headers_to_dict(payload_headers: list[dict]) -> dict[str, str]:
    """Gmail API gives headers as [{'name': X, 'value': Y}, ...]. Lowercase keys."""
    out: dict[str, str] = {}
    for h in payload_headers or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:  # take first occurrence
            out[name] = h.get("value") or ""
    return out


def fingerprint(message: dict) -> EmailFingerprint:
    """Build an EmailFingerprint from a Gmail API message dict (format=full or =metadata)."""
    headers = headers_to_dict(message.get("payload", {}).get("headers", []))
    sender_name, sender_email = parse_address(headers.get("from", ""))
    return EmailFingerprint(
        msg_id=message.get("id", ""),
        sender_email=sender_email,
        sender_name=sender_name,
        subject=headers.get("subject", ""),
        list_id=parse_list_id(headers.get("list-id", "")),
        list_unsubscribe=bool(headers.get("list-unsubscribe")),
        gmail_category=_extract_gmail_category(message.get("labelIds", [])),
    )
