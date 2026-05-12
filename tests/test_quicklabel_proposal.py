"""Tests for the proposal engine."""
from __future__ import annotations

from quicklabel.headers import EmailFingerprint
from quicklabel.proposal import (
    LabelProposal,
    SenderStats,
    build_proposal,
    common_subject_prefix,
    propose_filter,
    propose_label,
)


# ------------------ subject prefix detection ------------------

def test_common_subject_prefix_finds_stable_prefix():
    subjects = [
        "AI Atlas - Week of May 5",
        "AI Atlas - Week of April 28",
        "AI Atlas - Week of April 21",
        "AI Atlas - Week of April 14",
    ]
    prefix, count = common_subject_prefix(subjects)
    assert prefix is not None
    assert prefix.startswith("AI Atlas")
    assert count == 4


def test_common_subject_prefix_handles_re_fwd():
    subjects = [
        "Re: Order shipped",
        "Order shipped",
        "Fwd: Order shipped",
    ]
    prefix, count = common_subject_prefix(subjects)
    assert prefix == "Order shipped"
    assert count == 3


def test_common_subject_prefix_returns_none_for_unrelated():
    subjects = [
        "Your invoice for May",
        "Welcome to the team",
        "Random unrelated thing",
        "Yet another topic",
    ]
    prefix, count = common_subject_prefix(subjects)
    assert prefix is None or count < 2


def test_common_subject_prefix_too_few_subjects():
    assert common_subject_prefix([]) == (None, 0)
    assert common_subject_prefix(["only one"]) == (None, 0)


# ------------------ filter proposal ------------------

def _fp(sender="a@b.com", name=None, subject="Subj", list_id=None, lu=False):
    return EmailFingerprint(
        msg_id="m1", sender_email=sender, sender_name=name,
        subject=subject, list_id=list_id, list_unsubscribe=lu,
    )


def test_propose_filter_uses_list_id_when_available():
    email = _fp(list_id="ai-atlas.sequoiacap.com")
    stats = SenderStats(total_from_sender=50, total_from_list=23,
                        common_subject_prefix=None, prefix_match_count=0)
    fp = propose_filter(email, stats)
    assert fp.criteria == {"query": "list:ai-atlas.sequoiacap.com"}
    assert fp.estimated_match_count == 23
    assert "most precise" in fp.rationale.lower() or "list" in fp.rationale.lower()


def test_propose_filter_narrows_by_subject_when_prefix_dominant():
    email = _fp(sender="news@x.com")
    stats = SenderStats(
        total_from_sender=10, total_from_list=0,
        common_subject_prefix="AI Atlas - Week of",
        prefix_match_count=8,  # 80% of 10
    )
    fp = propose_filter(email, stats)
    assert "subject" in fp.criteria
    assert "AI Atlas" in fp.criteria["subject"]
    assert fp.estimated_match_count == 8


def test_propose_filter_falls_back_to_sender_only():
    email = _fp(sender="generic@x.com")
    stats = SenderStats(total_from_sender=12, total_from_list=0,
                        common_subject_prefix=None, prefix_match_count=0)
    fp = propose_filter(email, stats)
    assert fp.criteria == {"from": "generic@x.com"}
    assert fp.estimated_match_count == 12


def test_propose_filter_does_not_narrow_when_prefix_too_weak():
    email = _fp(sender="mixed@x.com")
    stats = SenderStats(
        total_from_sender=20, total_from_list=0,
        common_subject_prefix="Update",
        prefix_match_count=5,  # only 25%
    )
    fp = propose_filter(email, stats)
    assert "subject" not in fp.criteria
    assert fp.criteria == {"from": "mixed@x.com"}


# ------------------ label proposal ------------------

def test_propose_label_prefers_display_name():
    email = _fp(sender="news@sequoiacap.com", name="Sequoia Capital")
    lp = propose_label(email)
    assert isinstance(lp, LabelProposal)
    assert "Sequoia" in lp.suggested_name


def test_propose_label_strips_stopwords():
    email = _fp(sender="a@x.com", name="The AI Newsletter Team")
    lp = propose_label(email)
    # stripped: "the", "newsletter", "team" -> "AI"
    assert "Newsletter" not in lp.suggested_name
    assert "AI" in lp.suggested_name


def test_propose_label_uses_domain_when_no_name():
    email = _fp(sender="noreply@stripe.com", name=None)
    lp = propose_label(email)
    assert "Stripe" in lp.suggested_name or "stripe" in lp.suggested_name.lower()


def test_propose_label_handles_dotted_subdomain():
    email = _fp(sender="x@mail.linkedin.com", name=None)
    lp = propose_label(email)
    assert "linkedin" in lp.suggested_name.lower()


# ------------------ end-to-end build_proposal ------------------

def test_build_proposal_no_questions_for_clean_list_email():
    email = _fp(sender="news@sequoiacap.com", name="Sequoia",
                list_id="ai-atlas.sequoiacap.com")
    # list and sender match — clean case
    stats = SenderStats(total_from_sender=23, total_from_list=23,
                        common_subject_prefix="AI Atlas",
                        prefix_match_count=23)
    p = build_proposal(email, stats)
    assert p.questions == []
    assert "list:" in p.filter.query


def test_build_proposal_asks_when_list_narrower_than_sender():
    email = _fp(sender="notifications@linkedin.com", name="LinkedIn",
                list_id="job-alerts.linkedin.com")
    # sender sends way more than the list
    stats = SenderStats(total_from_sender=400, total_from_list=50,
                        common_subject_prefix=None, prefix_match_count=0)
    p = build_proposal(email, stats)
    assert len(p.questions) == 1
    assert p.questions[0].field == "filter"
    assert any("400" in opt["label"] for opt in p.questions[0].options)
    assert any("50" in opt["label"] for opt in p.questions[0].options)


def test_build_proposal_asks_when_subject_prefix_is_partial():
    email = _fp(sender="updates@brand.com")
    stats = SenderStats(
        total_from_sender=20, total_from_list=0,
        common_subject_prefix="Order shipped",
        prefix_match_count=8,  # 40% — partial
    )
    p = build_proposal(email, stats)
    assert len(p.questions) == 1
    assert "Order shipped" in p.questions[0].text
