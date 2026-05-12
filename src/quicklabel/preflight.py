"""Preflight checks: filter conflicts and destructive-action detection.

Used by the apply endpoints to decide whether to interrupt the user with
a confirmation page or just execute. Also used by the confirm page to
explain why we paused.
"""
from __future__ import annotations

from .actions import ActionChoices


# When back-prop will affect more than this many existing emails AND a
# destructive action is in play, show the confirmation page.
DESTRUCTIVE_BACKPROP_THRESHOLD = 50


def is_destructive(actions: ActionChoices) -> bool:
    """True if applying these actions to existing mail is hard to reverse.

    'Mark as read' and 'Categorize as' are reversible at low cost — not
    destructive. Skip-inbox / Move-to-trash / Never-important rearrange
    visible inbox state in ways the user might regret.
    """
    if actions.inbox in ("skip", "trash"):
        return True
    if actions.importance == "never_important":
        return True
    return False


def _extract_from_and_list(criteria: dict) -> tuple[str, str]:
    """Pull (from, list_id) out of a criteria dict whether they're in
    dedicated fields or buried in the `query` string. Lowercased,
    stripped of quotes, ready to compare."""
    raw_from = (criteria.get("from") or "").lower().strip()
    raw_query = (criteria.get("query") or "").lower().strip()
    list_id = ""
    # Walk the query for embedded from:/list: tokens so we catch the
    # case where the new criteria buries `from:` inside `query` rather
    # than using the dedicated field. Same tokenization on both sides
    # of the conflict comparison means new and existing can't disagree
    # about where a token lives.
    for tok in raw_query.split():
        # Strip outer quotes/whitespace from the token, then strip again
        # AFTER removing the from:/list: prefix so quoted values like
        # `list:"foo.com"` end up as `foo.com`, not `"foo.com`.
        low = tok.strip().strip('"')
        if low.startswith("from:") and not raw_from:
            raw_from = low[len("from:"):].strip('"')
        elif low.startswith("list:"):
            list_id = low[len("list:"):].strip('"')
    return raw_from, list_id


def find_filter_conflicts(new_criteria: dict, existing_filters: list[dict]) -> list[dict]:
    """Find existing filters that overlap on the same from/list criterion.

    `new_criteria` is the proposed filter's criteria dict (what we'd POST
    to filters.create). `existing_filters` is the list returned by
    Gmail's filters.list API.

    Returns the conflicting existing filters (full dicts so the caller
    can show their action / id).
    """
    new_from, new_list = _extract_from_and_list(new_criteria)

    conflicts: list[dict] = []
    for f in existing_filters:
        crit = f.get("criteria") or {}
        ex_from, ex_list = _extract_from_and_list(crit)

        if new_from and ex_from and new_from == ex_from:
            conflicts.append(f)
            continue
        if new_list and ex_list and new_list == ex_list:
            conflicts.append(f)
            continue
    return conflicts


def should_confirm(
    scope: str,
    actions: ActionChoices,
    backprop_count: int,
    conflicts: list[dict],
) -> bool:
    """Decide whether to interrupt with a full server-side confirmation page.

    The page is reserved for cases that genuinely need the user to study a
    list of conflicting filters or stop and think. Lighter cases (skip
    inbox / never-important on >50 existing) are handled by an inline
    warning + native JS confirm() on the proposal page itself, which the
    user finds far less jarring than a navigation step.

    Triggers for the full page:
      - Filter conflicts exist (any scope that creates a filter)
      - Trash on existing mail (always — it's the most destructive)
      - Very large destructive backprop (>500 messages)
    """
    if conflicts and scope in ("future_only", "both"):
        return True
    affects_existing = scope in ("existing_only", "both")
    if affects_existing and actions.inbox == "trash" and backprop_count > 0:
        return True
    if (
        affects_existing
        and is_destructive(actions)
        and backprop_count > 500
    ):
        return True
    return False
