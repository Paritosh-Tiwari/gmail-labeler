"""Tests for header parsing."""
from __future__ import annotations

import pytest

from quicklabel.headers import (
    EmailFingerprint,
    _extract_gmail_category,
    fingerprint,
    headers_to_dict,
    parse_address,
    parse_list_id,
)


# --------------------------- Gmail category extraction ---------------------------

@pytest.mark.parametrize("label_ids,expected", [
    (["INBOX", "CATEGORY_PROMOTIONS"], "Promotions"),
    (["CATEGORY_PERSONAL"], "Primary"),
    (["CATEGORY_SOCIAL", "UNREAD"], "Social"),
    (["CATEGORY_UPDATES"], "Updates"),
    (["CATEGORY_FORUMS"], "Forums"),
    (["INBOX", "IMPORTANT"], None),  # no CATEGORY_*
    ([], None),
    (None, None),
])
def test_extract_gmail_category(label_ids, expected):
    assert _extract_gmail_category(label_ids) == expected


def test_fingerprint_extracts_gmail_category_from_label_ids():
    msg = {
        "id": "m1",
        "labelIds": ["INBOX", "CATEGORY_PROMOTIONS", "UNREAD"],
        "payload": {"headers": [
            {"name": "From", "value": "promo@store.com"},
            {"name": "Subject", "value": "50% off"},
        ]},
    }
    fp = fingerprint(msg)
    assert fp.gmail_category == "Promotions"


def test_fingerprint_gmail_category_none_when_absent():
    msg = {
        "id": "m1",
        "labelIds": ["INBOX"],
        "payload": {"headers": [
            {"name": "From", "value": "boss@work.com"},
            {"name": "Subject", "value": "meeting tomorrow"},
        ]},
    }
    fp = fingerprint(msg)
    assert fp.gmail_category is None


@pytest.mark.parametrize("raw,expected", [
    ("Sequoia Capital <newsletter@sequoiacap.com>",
     ("Sequoia Capital", "newsletter@sequoiacap.com")),
    ("Lenny Rachitsky <news@substack.com>",
     ("Lenny Rachitsky", "news@substack.com")),
    ('"Quoted Name" <a@b.com>', ("Quoted Name", "a@b.com")),
    ("plain@b.com", (None, "plain@b.com")),
    ("Plain@B.COM", (None, "plain@b.com")),
    ("", (None, "")),
])
def test_parse_address(raw, expected):
    assert parse_address(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("AI Atlas <ai-atlas.sequoiacap.com>", "ai-atlas.sequoiacap.com"),
    ("ai-atlas.sequoiacap.com", "ai-atlas.sequoiacap.com"),
    ("", None),
    ("<list.example.com>", "list.example.com"),
])
def test_parse_list_id(raw, expected):
    assert parse_list_id(raw) == expected


def test_headers_to_dict_lowercases_and_keeps_first():
    h = [
        {"name": "From", "value": "a@b.com"},
        {"name": "Subject", "value": "Hello"},
        {"name": "from", "value": "duplicate@b.com"},  # ignored
    ]
    out = headers_to_dict(h)
    assert out == {"from": "a@b.com", "subject": "Hello"}


def test_fingerprint_full_message():
    msg = {
        "id": "abc123",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sequoia <newsletter@sequoiacap.com>"},
                {"name": "Subject", "value": "AI Atlas - Week of May 5"},
                {"name": "List-ID", "value": "AI Atlas <ai-atlas.sequoiacap.com>"},
                {"name": "List-Unsubscribe", "value": "<mailto:u@example.com>"},
            ]
        },
    }
    fp = fingerprint(msg)
    assert isinstance(fp, EmailFingerprint)
    assert fp.msg_id == "abc123"
    assert fp.sender_email == "newsletter@sequoiacap.com"
    assert fp.sender_name == "Sequoia"
    assert fp.subject == "AI Atlas - Week of May 5"
    assert fp.list_id == "ai-atlas.sequoiacap.com"
    assert fp.list_unsubscribe is True


def test_fingerprint_no_list_headers():
    msg = {
        "id": "x",
        "payload": {"headers": [{"name": "From", "value": "x@y.com"}]},
    }
    fp = fingerprint(msg)
    assert fp.list_id is None
    assert fp.list_unsubscribe is False
    assert fp.subject == ""
