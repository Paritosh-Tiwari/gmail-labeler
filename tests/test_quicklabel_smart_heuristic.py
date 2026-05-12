"""Tests for the smarter (no-LLM) proposer used during scan."""
from __future__ import annotations

from quicklabel.headers import EmailFingerprint
from quicklabel.proposal import SenderStats
from quicklabel.signals import Signals
from quicklabel.smart_heuristic import (
    domain_match_label,
    sender_domain_stem,
    suggest_actions_smart,
    suggest_label_smart,
)


def fp(sender: str, name: str | None = None, list_id: str | None = None) -> EmailFingerprint:
    return EmailFingerprint(
        msg_id="m", sender_email=sender, sender_name=name,
        subject="x", list_id=list_id, list_unsubscribe=False,
    )


# --------------------------- domain stem ---------------------------

def test_sender_domain_stem_basics():
    assert sender_domain_stem("alerts@citi.com") == "citi"
    assert sender_domain_stem("hello@sequoiacap.com") == "sequoiacap"
    assert sender_domain_stem("noreply@news.example.co.uk") == "example"


def test_sender_domain_stem_drops_generic_providers():
    """We don't want personal-mail providers polluting label matches."""
    assert sender_domain_stem("alice@gmail.com") is None
    assert sender_domain_stem("bob@yahoo.com") is None
    assert sender_domain_stem("c@outlook.com") is None
    assert sender_domain_stem("d@hotmail.com") is None
    assert sender_domain_stem("e@icloud.com") is None


# --------------------------- domain → label ---------------------------

def test_domain_matches_existing_leaf_label():
    existing = ["Finance", "Finance/Citi", "AI Newsletters"]
    assert domain_match_label("alerts@citi.com", existing) == "Finance/Citi"


def test_domain_matches_existing_top_level():
    existing = ["Sequoia", "Other"]
    assert domain_match_label("news@sequoiacap.com", existing) == "Sequoia"


def test_domain_match_prefers_deepest_path():
    """If 'Citi' appears at multiple depths, prefer the deepest match."""
    existing = ["Citi", "Finance", "Finance/Citi", "Finance/Citi/Alerts"]
    assert domain_match_label("alerts@citi.com", existing) == "Finance/Citi/Alerts"


def test_domain_match_returns_none_when_no_overlap():
    assert domain_match_label("a@notmatchinganything.com", ["Finance", "Travel"]) is None


def test_domain_match_returns_none_for_generic_providers():
    assert domain_match_label("alice@gmail.com", ["Finance", "Personal"]) is None


# --------------------------- label suggestion ---------------------------

def test_suggest_label_uses_domain_match_when_available():
    email = fp("alerts@citi.com", name="Citi Alerts")
    sigs = Signals(looks_transactional=True)
    stats = SenderStats(total_from_sender=530, total_from_list=0,
                        common_subject_prefix=None, prefix_match_count=0)
    p = suggest_label_smart(email, sigs, stats, ["Finance", "Finance/Citi"])
    assert p.suggested_name == "Finance/Citi"
    assert p.suggested_parent is None  # full path returned as suggested_name when matching existing


def test_suggest_label_falls_back_to_display_name_when_no_match():
    email = fp("hello@somenewco.com", name="Some New Co")
    sigs = Signals(is_personal=False)
    stats = SenderStats(total_from_sender=3, total_from_list=0,
                        common_subject_prefix=None, prefix_match_count=0)
    p = suggest_label_smart(email, sigs, stats, ["Finance", "AI Newsletters"])
    # Should fall back to existing propose_label (cleaned display name)
    assert "Some New Co" in p.suggested_name


# --------------------------- action defaults ---------------------------

def test_suggest_actions_marketing_high_volume():
    """Marketing + has_unsubscribe + heavy sender => skip + mark_read + Promotions."""
    sigs = Signals(has_unsubscribe=True, looks_marketing=True)
    stats = SenderStats(total_from_sender=200, total_from_list=200,
                        common_subject_prefix=None, prefix_match_count=0)
    a = suggest_actions_smart(sigs, stats)
    assert a.inbox == "skip"
    assert a.mark_read is True
    assert a.categorize == "promotions"


def test_suggest_actions_transactional_only_marks_read():
    sigs = Signals(looks_transactional=True, is_no_reply=True)
    stats = SenderStats(total_from_sender=10, total_from_list=0,
                        common_subject_prefix=None, prefix_match_count=0)
    a = suggest_actions_smart(sigs, stats)
    assert a.mark_read is True
    # Don't auto-skip transactional — user might need to see receipts
    assert a.inbox == "keep"


def test_suggest_actions_personal_keeps_defaults():
    sigs = Signals(is_personal=True)
    stats = SenderStats(total_from_sender=2, total_from_list=0,
                        common_subject_prefix=None, prefix_match_count=0)
    a = suggest_actions_smart(sigs, stats)
    assert a.inbox == "keep"
    assert a.mark_read is False
    assert a.categorize == "none"


def test_suggest_actions_no_reply_notification_skips():
    """Notifications from no-reply that aren't transactional → skip + read."""
    sigs = Signals(is_no_reply=True, looks_marketing=False, looks_transactional=False)
    stats = SenderStats(total_from_sender=50, total_from_list=0,
                        common_subject_prefix=None, prefix_match_count=0)
    a = suggest_actions_smart(sigs, stats)
    assert a.inbox == "skip"
    assert a.mark_read is True
