"""Cheap content signal flags. Pure functions over headers + body excerpt.

These flags both prime the LLM prompt (as plain English) and act as a
deterministic fallback when the LLM is unavailable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .headers import EmailFingerprint


_NO_REPLY_RE = re.compile(
    r"^(no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|notifications?|alerts?)@",
    flags=re.IGNORECASE,
)
_MARKETING_KEYWORDS = re.compile(
    r"\b(newsletter|digest|weekly|monthly|edition|issue\s*#?\d+|"
    r"subscribe|unsubscribe|promotion|deals?|sale)\b",
    flags=re.IGNORECASE,
)
_TRANSACTIONAL_KEYWORDS = re.compile(
    r"\b(receipt|invoice|payment|order|shipment|shipped|delivered|"
    r"refund|confirmation|booking|reservation|ticket|statement)\b",
    flags=re.IGNORECASE,
)
_UNSUBSCRIBE_BODY_RE = re.compile(r"\bunsubscribe\b", flags=re.IGNORECASE)


@dataclass
class Signals:
    has_unsubscribe: bool = False
    is_no_reply: bool = False
    looks_marketing: bool = False
    looks_transactional: bool = False
    is_personal: bool = False

    def describe(self) -> list[str]:
        """Plain-English bullets, suitable for embedding in an LLM prompt."""
        out: list[str] = []
        if self.has_unsubscribe:
            out.append("Has an unsubscribe link (likely bulk/marketing)")
        if self.is_no_reply:
            out.append("Sender is a no-reply / notifications address")
        if self.looks_marketing:
            out.append("Subject/list-id matches newsletter or marketing patterns")
        if self.looks_transactional:
            out.append("Subject matches transactional patterns (receipt/order/invoice)")
        if self.is_personal:
            out.append("Looks like personal mail (not marketing or transactional)")
        return out


def extract_signals(email: EmailFingerprint, body: str) -> Signals:
    has_unsub = bool(email.list_unsubscribe) or bool(_UNSUBSCRIBE_BODY_RE.search(body or ""))
    is_no_reply = bool(_NO_REPLY_RE.match(email.sender_email or ""))

    subject = email.subject or ""
    looks_market = (
        bool(email.list_id)  # any list-id is a strong marketing/automation signal
        or bool(_MARKETING_KEYWORDS.search(subject))
    )
    looks_trans = bool(_TRANSACTIONAL_KEYWORDS.search(subject))

    is_personal = (
        not is_no_reply
        and not email.list_id
        and not looks_market
        and not looks_trans
        and not has_unsub
    )

    return Signals(
        has_unsubscribe=has_unsub,
        is_no_reply=is_no_reply,
        looks_marketing=looks_market,
        looks_transactional=looks_trans,
        is_personal=is_personal,
    )
