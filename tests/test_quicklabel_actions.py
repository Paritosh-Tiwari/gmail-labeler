"""Tests for the actions converter (form fields -> Gmail label-id mutations)."""
from __future__ import annotations

import pytest

from quicklabel.actions import ActionChoices, to_label_mutations


def test_default_choices_no_mutations():
    out = to_label_mutations(ActionChoices())
    assert out == ([], [])


def test_skip_inbox_removes_inbox():
    out = to_label_mutations(ActionChoices(inbox="skip"))
    assert out == ([], ["INBOX"])


def test_trash_adds_trash():
    out = to_label_mutations(ActionChoices(inbox="trash"))
    # Adding TRASH implicitly archives, but we don't double-remove INBOX
    assert out == (["TRASH"], [])


def test_mark_important_adds_important():
    out = to_label_mutations(ActionChoices(importance="important"))
    assert out == (["IMPORTANT"], [])


def test_never_important_removes_important():
    out = to_label_mutations(ActionChoices(importance="never_important"))
    assert out == ([], ["IMPORTANT"])


def test_mark_read_removes_unread():
    out = to_label_mutations(ActionChoices(mark_read=True))
    assert out == ([], ["UNREAD"])


def test_star_adds_starred():
    out = to_label_mutations(ActionChoices(star=True))
    assert out == (["STARRED"], [])


def test_never_spam_removes_spam():
    out = to_label_mutations(ActionChoices(never_spam=True))
    assert out == ([], ["SPAM"])


def test_combined_actions():
    out = to_label_mutations(ActionChoices(
        inbox="skip", importance="important",
        mark_read=True, star=True, never_spam=True,
    ))
    add, remove = out
    assert set(add) == {"IMPORTANT", "STARRED"}
    assert set(remove) == {"INBOX", "UNREAD", "SPAM"}


def test_invalid_inbox_value_raises():
    with pytest.raises(ValueError):
        to_label_mutations(ActionChoices(inbox="bogus"))


def test_invalid_importance_value_raises():
    with pytest.raises(ValueError):
        to_label_mutations(ActionChoices(importance="bogus"))


def test_action_choices_from_form():
    """Convenience constructor: build ActionChoices from FastAPI form values."""
    choices = ActionChoices.from_form(
        inbox_action="skip",
        importance_action="default",
        mark_read="yes",
        star="",
        never_spam="yes",
    )
    assert choices.inbox == "skip"
    assert choices.importance == "default"
    assert choices.categorize == "none"
    assert choices.mark_read is True
    assert choices.star is False
    assert choices.never_spam is True


def test_categorize_promotions_adds_category_promotions():
    out = to_label_mutations(ActionChoices(categorize="promotions"))
    assert out == (["CATEGORY_PROMOTIONS"], [])


def test_categorize_combines_with_other_actions():
    out = to_label_mutations(ActionChoices(
        inbox="skip", categorize="updates", mark_read=True,
    ))
    add, remove = out
    assert "CATEGORY_UPDATES" in add
    assert "INBOX" in remove
    assert "UNREAD" in remove


def test_invalid_categorize_value_raises():
    import pytest
    with pytest.raises(ValueError):
        to_label_mutations(ActionChoices(categorize="bogus"))


def test_describe_includes_categorize():
    from quicklabel.actions import describe
    text = " ".join(describe(ActionChoices(categorize="social"))).lower()
    assert "social" in text


def test_action_choices_from_form_treats_missing_as_default():
    choices = ActionChoices.from_form(
        inbox_action="keep",
        importance_action="default",
        mark_read="",
        star="",
        never_spam="",
    )
    assert choices == ActionChoices()


def test_describe_human_summary():
    """describe() returns a short human-readable list of what'll happen."""
    from quicklabel.actions import describe
    summary = describe(ActionChoices(inbox="skip", mark_read=True, star=True))
    text = " ".join(summary).lower()
    assert "skip inbox" in text or "archive" in text
    assert "read" in text
    assert "star" in text


def test_describe_empty_for_default():
    from quicklabel.actions import describe
    assert describe(ActionChoices()) == []
