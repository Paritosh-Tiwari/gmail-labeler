"""Tests for the cheap signal-flag extractor."""
from __future__ import annotations

from quicklabel.headers import EmailFingerprint
from quicklabel.signals import Signals, extract_signals


def fp(sender_email: str, sender_name: str | None = None,
       subject: str = "", list_id: str | None = None,
       list_unsubscribe: bool = False) -> EmailFingerprint:
    return EmailFingerprint(
        msg_id="m", sender_email=sender_email, sender_name=sender_name,
        subject=subject, list_id=list_id, list_unsubscribe=list_unsubscribe,
    )


def test_unsubscribe_from_header():
    s = extract_signals(fp("a@x.com", list_unsubscribe=True), body="")
    assert s.has_unsubscribe is True


def test_unsubscribe_from_body():
    s = extract_signals(fp("a@x.com"), body="To unsubscribe click here.")
    assert s.has_unsubscribe is True


def test_no_unsubscribe():
    s = extract_signals(fp("alice@friend.com"), body="Hey what's up")
    assert s.has_unsubscribe is False


def test_no_reply_sender():
    assert extract_signals(fp("no-reply@example.com"), body="").is_no_reply
    assert extract_signals(fp("noreply@example.com"), body="").is_no_reply
    assert extract_signals(fp("donotreply@x.com"), body="").is_no_reply
    assert not extract_signals(fp("alice@x.com"), body="").is_no_reply


def test_looks_marketing_from_subject_keywords():
    s = extract_signals(fp("a@x.com", subject="Weekly digest #234"), body="")
    assert s.looks_marketing is True


def test_looks_marketing_from_listid():
    s = extract_signals(fp("a@x.com", list_id="weekly.example.com"), body="")
    assert s.looks_marketing is True


def test_looks_transactional_from_subject():
    assert extract_signals(fp("a@x.com", subject="Your receipt for order #4"), body="").looks_transactional
    assert extract_signals(fp("a@x.com", subject="Payment confirmation"), body="").looks_transactional
    assert extract_signals(fp("a@x.com", subject="Invoice #2934"), body="").looks_transactional
    assert extract_signals(fp("a@x.com", subject="Shipment delivered"), body="").looks_transactional


def test_personal_thread_when_not_no_reply_and_no_listid():
    """If sender isn't no-reply, has no list-id, and doesn't look marketing/transactional."""
    s = extract_signals(fp("alice@friend.com", subject="Lunch tomorrow?"), body="Hey, want to grab lunch?")
    assert s.is_personal is True


def test_personal_false_when_marketing():
    s = extract_signals(fp("news@x.com", subject="Weekly digest"), body="")
    assert s.is_personal is False


def test_describe_returns_human_readable_list():
    s = Signals(has_unsubscribe=True, is_no_reply=True, looks_marketing=True,
                looks_transactional=False, is_personal=False)
    out = s.describe()
    text = " | ".join(out).lower()
    assert "unsubscribe" in text
    assert "no-reply" in text or "no reply" in text
    assert "marketing" in text or "newsletter" in text
