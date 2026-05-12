"""Tests for the label_status helper that powers the existing/extends/new badge."""
from __future__ import annotations

from quicklabel.server import label_status


def test_exact_match_is_existing():
    existing = {"Finance", "Finance/Citi", "AI Newsletters"}
    assert label_status("Finance/Citi", existing) == "existing"
    assert label_status("Finance", existing) == "existing"


def test_extends_existing_when_parent_present():
    existing = {"Finance", "AI Newsletters"}
    assert label_status("Finance/Citi", existing) == "extends_existing"
    assert label_status("Finance/Citi/Alerts", existing) == "extends_existing"
    assert label_status("AI Newsletters/Sequoia", existing) == "extends_existing"


def test_new_when_no_ancestor_matches():
    existing = {"Finance", "AI Newsletters"}
    assert label_status("Travel", existing) == "new"
    assert label_status("Travel/Flights", existing) == "new"


def test_empty_path_is_new():
    assert label_status("", {"Finance"}) == "new"


def test_deep_nesting_walks_up():
    """A new leaf five levels deep under an existing root still 'extends'."""
    existing = {"A"}
    assert label_status("A/B/C/D/E", existing) == "extends_existing"


def test_case_sensitive_match():
    """Match is case-sensitive (Gmail labels are case-sensitive in the API)."""
    existing = {"Finance"}
    assert label_status("finance", existing) == "new"
    assert label_status("Finance/citi", existing) == "extends_existing"
