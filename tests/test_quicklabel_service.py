"""Tests for service.py helpers — particularly the filter-merge logic
used when extending an existing Gmail filter instead of creating a
duplicate."""
from __future__ import annotations

from quicklabel.service import merge_filter_action


# --------------------------- merge_filter_action ---------------------------

def test_merge_adds_new_label_to_existing():
    existing = {"addLabelIds": ["Label_existing"]}
    merged = merge_filter_action(existing, "Label_new")
    assert merged["addLabelIds"] == ["Label_existing", "Label_new"]
    assert "removeLabelIds" not in merged


def test_merge_dedupes_when_label_already_present():
    existing = {"addLabelIds": ["Label_new", "Label_existing"]}
    merged = merge_filter_action(existing, "Label_new")
    # New label already in list — preserved order, no duplicate
    assert merged["addLabelIds"] == ["Label_new", "Label_existing"]


def test_merge_preserves_existing_remove_label_ids():
    existing = {"addLabelIds": ["Label_X"], "removeLabelIds": ["INBOX"]}
    merged = merge_filter_action(existing, "Label_Y")
    assert merged["removeLabelIds"] == ["INBOX"]
    assert merged["addLabelIds"] == ["Label_X", "Label_Y"]


def test_merge_unions_extra_add_label_ids():
    """User picked 'mark as important' on the proposal page (adds
    IMPORTANT to extra_add_label_ids) — that gets unioned with the
    existing filter's adds."""
    existing = {"addLabelIds": ["Label_X"]}
    merged = merge_filter_action(
        existing, "Label_Y", extra_add_label_ids=["IMPORTANT"],
    )
    assert merged["addLabelIds"] == ["Label_X", "Label_Y", "IMPORTANT"]


def test_merge_unions_extra_remove_label_ids():
    """User picked 'skip inbox' (adds INBOX to extras.remove) — gets
    unioned with the existing filter's removes."""
    existing = {"addLabelIds": ["Label_X"], "removeLabelIds": ["SPAM"]}
    merged = merge_filter_action(
        existing, "Label_Y", extra_remove_label_ids=["INBOX"],
    )
    assert merged["removeLabelIds"] == ["SPAM", "INBOX"]


def test_merge_preserves_forward_action():
    """Forward isn't something we ask the user about — keep existing."""
    existing = {
        "addLabelIds": ["Label_X"],
        "forward": "archive@example.com",
    }
    merged = merge_filter_action(existing, "Label_Y")
    assert merged["forward"] == "archive@example.com"


def test_merge_empty_existing_action():
    """Filter that had no action at all (rare but legal) gets a fresh
    action containing just the new label."""
    merged = merge_filter_action({}, "Label_new")
    assert merged == {"addLabelIds": ["Label_new"]}


def test_merge_handles_none_label_id():
    """If only extras are being merged (no new primary label), no
    addLabelIds is added from that bucket."""
    existing = {"addLabelIds": ["Label_X"]}
    merged = merge_filter_action(
        existing, None, extra_add_label_ids=["STARRED"],
    )
    assert merged["addLabelIds"] == ["Label_X", "STARRED"]


def test_merge_no_double_extras_when_already_present():
    existing = {"addLabelIds": ["Label_X", "IMPORTANT"]}
    merged = merge_filter_action(
        existing, "Label_Y", extra_add_label_ids=["IMPORTANT"],
    )
    # IMPORTANT shouldn't appear twice
    assert merged["addLabelIds"] == ["Label_X", "IMPORTANT", "Label_Y"]
