"""Tests for preflight checks (filter conflict + destructive-action detection)."""
from __future__ import annotations

from quicklabel.actions import ActionChoices
from quicklabel.preflight import (
    find_filter_conflicts,
    is_destructive,
    should_confirm,
)


# --------------------------- is_destructive ---------------------------

def test_is_destructive_default_choices_is_safe():
    assert is_destructive(ActionChoices()) is False


def test_skip_inbox_is_destructive():
    assert is_destructive(ActionChoices(inbox="skip")) is True


def test_trash_is_destructive():
    assert is_destructive(ActionChoices(inbox="trash")) is True


def test_never_important_is_destructive():
    assert is_destructive(ActionChoices(importance="never_important")) is True


def test_mark_read_is_not_destructive():
    """Mark-as-read is reversible, so not flagged as destructive."""
    assert is_destructive(ActionChoices(mark_read=True)) is False


def test_categorize_alone_is_not_destructive():
    assert is_destructive(ActionChoices(categorize="promotions")) is False


# --------------------------- find_filter_conflicts ---------------------------

def test_no_filters_returns_empty():
    assert find_filter_conflicts({"from": "a@x.com"}, []) == []


def test_finds_conflict_on_same_from():
    existing = [
        {"id": "f1", "criteria": {"from": "alerts@citi.com"}, "action": {"addLabelIds": ["X"]}},
        {"id": "f2", "criteria": {"from": "other@y.com"}, "action": {"addLabelIds": ["Y"]}},
    ]
    conflicts = find_filter_conflicts({"from": "alerts@citi.com"}, existing)
    assert len(conflicts) == 1
    assert conflicts[0]["id"] == "f1"


def test_finds_conflict_on_same_list_in_query():
    existing = [
        {"id": "f1", "criteria": {"query": "list:weekly.example.com"},
         "action": {"addLabelIds": ["X"]}},
    ]
    new_criteria = {"query": "list:weekly.example.com"}
    conflicts = find_filter_conflicts(new_criteria, existing)
    assert len(conflicts) == 1


def test_no_conflict_for_different_sender():
    existing = [
        {"id": "f1", "criteria": {"from": "a@x.com"}, "action": {"addLabelIds": ["X"]}},
    ]
    assert find_filter_conflicts({"from": "b@x.com"}, existing) == []


def test_case_insensitive_match():
    existing = [
        {"id": "f1", "criteria": {"from": "Alerts@CITI.com"}, "action": {"addLabelIds": ["X"]}},
    ]
    conflicts = find_filter_conflicts({"from": "alerts@citi.com"}, existing)
    assert len(conflicts) == 1


def test_finds_conflict_when_new_from_is_inside_query_field():
    """Bug #10 regression: new criteria's `from:` could be embedded in
    `query` (e.g. when criteria is {'query': 'from:foo@bar.com list:x'}).
    Detection used to only look at the dedicated `from` field."""
    existing = [
        {"id": "f1", "criteria": {"from": "foo@bar.com"},
         "action": {"addLabelIds": ["X"]}},
    ]
    new = {"query": "from:foo@bar.com list:newsletter.bar.com"}
    conflicts = find_filter_conflicts(new, existing)
    assert len(conflicts) == 1


def test_finds_conflict_when_new_list_in_dedicated_query_field():
    existing = [
        {"id": "f1", "criteria": {"query": "list:weekly.example.com"},
         "action": {"addLabelIds": ["X"]}},
    ]
    new = {"from": "anyone@example.com", "query": "list:weekly.example.com"}
    conflicts = find_filter_conflicts(new, existing)
    assert len(conflicts) == 1


# --------------------------- should_confirm ---------------------------

def test_confirm_for_trash_on_any_existing():
    """Trash on existing always shows the full confirm page (most destructive)."""
    assert should_confirm(
        scope="both", actions=ActionChoices(inbox="trash"),
        backprop_count=1, conflicts=[],
    ) is True


def test_confirm_for_very_large_destructive_backprop():
    """Skip-inbox on >500 still warrants the full page."""
    assert should_confirm(
        scope="both", actions=ActionChoices(inbox="skip"),
        backprop_count=1000, conflicts=[],
    ) is True


def test_no_full_confirm_for_skip_inbox_on_modest_count():
    """Skip-inbox on 200 is handled by inline warning + JS confirm,
    not the full server-side confirm page."""
    assert should_confirm(
        scope="both", actions=ActionChoices(inbox="skip"),
        backprop_count=200, conflicts=[],
    ) is False


def test_should_confirm_when_conflicts_present():
    assert should_confirm(
        scope="future_only", actions=ActionChoices(),
        backprop_count=0, conflicts=[{"id": "f1"}],
    ) is True


def test_no_confirm_when_safe_and_no_conflicts():
    assert should_confirm(
        scope="both", actions=ActionChoices(mark_read=True),
        backprop_count=500, conflicts=[],
    ) is False


def test_no_confirm_when_destructive_but_few_existing():
    """Skip inbox on 5 existing is not scary enough to interrupt."""
    assert should_confirm(
        scope="both", actions=ActionChoices(inbox="skip"),
        backprop_count=5, conflicts=[],
    ) is False


def test_no_confirm_when_destructive_but_scope_future_only():
    """Future-only never affects existing mail, so destructive is fine without confirm."""
    assert should_confirm(
        scope="future_only", actions=ActionChoices(inbox="trash"),
        backprop_count=999, conflicts=[],
    ) is False
