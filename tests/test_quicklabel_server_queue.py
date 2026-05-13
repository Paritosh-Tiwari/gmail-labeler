"""Tests for the queue-related helpers in server.py.

Endpoint behavior is exercised live via scripts/quicklabel_queue_e2e.py.
Here we just unit-test the pure helper for query -> criteria conversion.
"""
from __future__ import annotations

from quicklabel.server import _criteria_from_query, _sender_domain


def test_criteria_from_simple_from_query():
    assert _criteria_from_query("from:foo@bar.com") == {"from": "foo@bar.com"}


def test_criteria_from_list_only():
    assert _criteria_from_query("list:my.list.com") == {"query": "list:my.list.com"}


def test_criteria_from_from_plus_subject():
    out = _criteria_from_query('from:foo@bar.com subject:"Weekly digest"')
    assert out == {"from": "foo@bar.com", "subject": "Weekly digest"}


def test_criteria_combines_from_and_list_into_query_field():
    """Bug #2 regression: list: was silently dropped when from: was present."""
    out = _criteria_from_query("from:author@news.com list:newsletter.news.com")
    assert out == {"from": "author@news.com", "query": "list:newsletter.news.com"}


def test_criteria_combines_from_subject_and_list():
    out = _criteria_from_query('from:a@b.com subject:"Weekly" list:newsletter.b.com')
    assert out == {
        "from": "a@b.com",
        "subject": "Weekly",
        "query": "list:newsletter.b.com",
    }


def test_criteria_preserves_unknown_operators_in_query():
    """Unknown ops like has:attachment should land in query field, not be dropped."""
    out = _criteria_from_query("from:a@b.com has:attachment")
    assert out == {"from": "a@b.com", "query": "has:attachment"}


def test_criteria_falls_back_to_raw_query():
    # Free-text query that doesn't have known operators -> use as raw query
    out = _criteria_from_query("important contract pdf")
    assert out == {"query": "important contract pdf"}


def test_criteria_from_subject_only():
    assert _criteria_from_query('subject:"Invoice"') == {"subject": "Invoice"}


def test_criteria_handles_empty_input():
    assert _criteria_from_query("") == {"query": ""}


def test_sender_domain_subdomain_strips_to_brand():
    assert _sender_domain("alerts@info6.citi.com") == "citi.com"


def test_sender_domain_no_subdomain_passes_through():
    assert _sender_domain("noreply@okta.com") == "okta.com"


def test_sender_domain_deep_subdomain():
    assert _sender_domain("mail@a.b.c.example.com") == "example.com"


def test_sender_domain_handles_empty_and_malformed():
    assert _sender_domain("") == ""
    assert _sender_domain("no-at-sign") == ""


def test_sender_domain_lowercases():
    assert _sender_domain("noreply@INFO6.Citi.COM") == "citi.com"
