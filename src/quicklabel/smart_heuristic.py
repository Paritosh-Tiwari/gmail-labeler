"""Smarter (no-LLM) proposer used by the queue scan path.

Uses signal flags + sender history + the user's existing labels to pick:
  - a candidate label (preferring an existing match by sender domain stem)
  - sensible default actions (skip-inbox for heavy marketing, mark-read
    for no-reply notifications, etc.)

Faster than calling the LLM, which keeps `Scan now` snappy. The user can
still hit `Regenerate with AI` per row to get the LLM proposal.
"""
from __future__ import annotations

from .actions import ActionChoices
from .headers import EmailFingerprint
from .proposal import LabelProposal, propose_label
from .signals import Signals
from .proposal import SenderStats


# Personal-mail providers — sender stem isn't a useful label hint here.
_GENERIC_PROVIDERS = {
    "gmail", "yahoo", "outlook", "hotmail", "icloud", "aol",
    "protonmail", "live", "msn", "me",
}


def sender_domain_stem(sender_email: str) -> str | None:
    """'alerts@citi.com' -> 'citi'; 'alice@gmail.com' -> None.

    Skips generic personal-mail providers. Picks the second-from-last
    domain part for compound TLDs (e.g. .co.uk).
    """
    if not sender_email or "@" not in sender_email:
        return None
    domain = sender_email.split("@", 1)[1].lower().strip()
    parts = [p for p in domain.split(".") if p]
    if len(parts) < 2:
        return None
    # For .co.uk-style TLDs, prefer parts[-3]; else parts[-2]
    compound_tlds = {"co", "com", "org", "net", "ac", "gov"}
    if len(parts) >= 3 and parts[-2] in compound_tlds:
        stem = parts[-3]
    else:
        stem = parts[-2]
    if stem in _GENERIC_PROVIDERS:
        return None
    return stem


def _segments_match(stem: str, segment: str, min_len: int = 4) -> bool:
    """Bidirectional substring match for label-segment vs domain-stem.

    Either string must be at least min_len chars to count, then either
    stem in segment OR segment in stem (case-insensitive).
    """
    s = stem.lower()
    g = segment.lower()
    if len(s) < min_len and len(g) < min_len:
        return False
    if len(s) >= min_len and s in g:
        return True
    if len(g) >= min_len and g in s:
        return True
    return False


def domain_match_label(sender_email: str, existing_labels: list[str]) -> str | None:
    """Return the existing label whose path contains the sender's domain stem.

    Match is bidirectional substring on each path segment (so 'sequoiacap'
    matches 'Sequoia', and 'citi' matches 'Citi'). When multiple labels
    match, prefers the deepest (most specific). Returns None if no match
    or domain is generic.
    """
    stem = sender_domain_stem(sender_email)
    if not stem:
        return None

    matches: list[tuple[int, str]] = []  # (depth, path)
    for label in existing_labels:
        parts = label.split("/")
        if any(_segments_match(stem, seg) for seg in parts):
            matches.append((len(parts), label))
    if not matches:
        return None
    matches.sort(key=lambda t: (-t[0], t[1]))
    return matches[0][1]


def suggest_label_smart(
    email: EmailFingerprint,
    signals: Signals,
    sender_stats: SenderStats,
    existing_labels: list[str],
) -> LabelProposal:
    """Best-guess label without LLM. Prefers an existing-label domain match;
    falls back to the cleaned display-name heuristic."""
    matched = domain_match_label(email.sender_email, existing_labels)
    if matched:
        return LabelProposal(
            suggested_name=matched,
            suggested_parent=None,  # full path returned as suggested_name
            rationale=f"Sender domain matches existing label '{matched}'.",
        )
    # Fall back to existing heuristic
    return propose_label(email)


def suggest_actions_smart(signals: Signals, sender_stats: SenderStats) -> ActionChoices:
    """Default actions inferred from signal patterns.

    Rules in priority order (first match wins for inbox state):
      - Heavy marketing (has_unsubscribe + looks_marketing + count>=20):
          skip inbox + mark read + categorize as Promotions
      - No-reply non-transactional notifications: skip inbox + mark read
      - Transactional alerts: mark read only (don't skip — receipts matter)
      - Anything else: keep defaults
    """
    a = ActionChoices()

    high_volume_marketing = (
        signals.has_unsubscribe
        and signals.looks_marketing
        and sender_stats.total_from_sender >= 20
    )

    if high_volume_marketing:
        a.inbox = "skip"
        a.mark_read = True
        a.categorize = "promotions"
        return a

    if signals.is_no_reply and not signals.looks_transactional and not signals.is_personal:
        a.inbox = "skip"
        a.mark_read = True
        return a

    if signals.looks_transactional:
        a.mark_read = True
        return a

    return a
