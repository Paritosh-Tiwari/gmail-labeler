"""Filter actions beyond add-label.

Gmail's filter API expresses every action through `addLabelIds` and
`removeLabelIds` (system labels included). This module exposes those
choices as a flat dataclass and converts them to the (add, remove) lists
the API expects.

Mutex-by-design (radio/select groups in the UI):
  inbox      : 'keep' | 'skip' | 'trash'
  importance : 'default' | 'important' | 'never_important'
  categorize : 'none' | 'personal' | 'social' | 'promotions' | 'updates' | 'forums'

Additive (checkboxes):
  mark_read, star, never_spam
"""
from __future__ import annotations

from dataclasses import dataclass


_VALID_INBOX = {"keep", "skip", "trash"}
_VALID_IMPORTANCE = {"default", "important", "never_important"}
_VALID_CATEGORIZE = {"none", "personal", "social", "promotions", "updates", "forums"}

_CATEGORY_LABEL_IDS = {
    "personal":   "CATEGORY_PERSONAL",
    "social":     "CATEGORY_SOCIAL",
    "promotions": "CATEGORY_PROMOTIONS",
    "updates":    "CATEGORY_UPDATES",
    "forums":     "CATEGORY_FORUMS",
}


@dataclass
class ActionChoices:
    inbox: str = "keep"               # keep | skip | trash
    importance: str = "default"       # default | important | never_important
    categorize: str = "none"          # none | personal | social | promotions | updates | forums
    mark_read: bool = False
    star: bool = False
    never_spam: bool = False

    @classmethod
    def from_form(
        cls,
        inbox_action: str = "keep",
        importance_action: str = "default",
        categorize_action: str = "none",
        mark_read: str = "",
        star: str = "",
        never_spam: str = "",
    ) -> "ActionChoices":
        return cls(
            inbox=inbox_action or "keep",
            importance=importance_action or "default",
            categorize=categorize_action or "none",
            mark_read=bool(mark_read),
            star=bool(star),
            never_spam=bool(never_spam),
        )


def to_label_mutations(c: ActionChoices) -> tuple[list[str], list[str]]:
    """Return (add_label_ids, remove_label_ids) for these choices."""
    if c.inbox not in _VALID_INBOX:
        raise ValueError(f"invalid inbox value: {c.inbox!r}")
    if c.importance not in _VALID_IMPORTANCE:
        raise ValueError(f"invalid importance value: {c.importance!r}")
    if c.categorize not in _VALID_CATEGORIZE:
        raise ValueError(f"invalid categorize value: {c.categorize!r}")

    add: list[str] = []
    remove: list[str] = []

    if c.inbox == "skip":
        remove.append("INBOX")
    elif c.inbox == "trash":
        # Adding TRASH causes Gmail to remove INBOX automatically; don't
        # double-list INBOX in remove.
        add.append("TRASH")

    if c.importance == "important":
        add.append("IMPORTANT")
    elif c.importance == "never_important":
        remove.append("IMPORTANT")

    if c.categorize != "none":
        add.append(_CATEGORY_LABEL_IDS[c.categorize])

    if c.mark_read:
        remove.append("UNREAD")
    if c.star:
        add.append("STARRED")
    if c.never_spam:
        remove.append("SPAM")

    return add, remove


def describe(c: ActionChoices) -> list[str]:
    """Human-friendly bullet list of what these actions will do."""
    out: list[str] = []
    if c.inbox == "skip":
        out.append("Skip inbox (auto-archive)")
    elif c.inbox == "trash":
        out.append("Move to trash (auto-delete)")
    if c.categorize != "none":
        out.append(f"Categorize as {c.categorize.title()}")
    if c.mark_read:
        out.append("Mark as read")
    if c.importance == "important":
        out.append("Mark as important")
    elif c.importance == "never_important":
        out.append("Never mark as important")
    if c.star:
        out.append("Star")
    if c.never_spam:
        out.append("Never send to spam")
    return out
