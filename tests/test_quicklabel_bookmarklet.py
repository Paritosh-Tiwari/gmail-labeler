"""Bookmarklet generation + URL-extraction tests.

The behavior tests below run against `extract_id_from_hash`, the Python
mirror of the bookmarklet's JS URL-parsing branch. Mirror must stay in
sync with `_BOOKMARKLET_JS` — these tests document expected behavior.
"""
from __future__ import annotations

from urllib.parse import unquote

import pytest

from quicklabel.bookmarklet import bookmarklet_url, extract_id_from_hash


# --------------------------- bookmarklet_url shape ---------------------------

def test_bookmarklet_starts_with_javascript_scheme():
    assert bookmarklet_url().startswith("javascript:")


def test_bookmarklet_contains_localhost_target():
    js = unquote(bookmarklet_url())
    assert "127.0.0.1:8765/label?id=" in js


def test_bookmarklet_handles_custom_base_url():
    js = unquote(bookmarklet_url("http://localhost:9000"))
    assert "localhost:9000/label?id=" in js


def test_bookmarklet_uses_url_hash_and_last_segment():
    js = unquote(bookmarklet_url())
    assert "window.location.hash" in js
    assert "parts.length-1" in js
    assert "encodeURIComponent" in js


def test_bookmarklet_warns_when_no_id():
    js = unquote(bookmarklet_url())
    assert "alert" in js
    assert "Click an email in Gmail first" in js


def test_bookmarklet_validates_id_pattern():
    """Should reject things like '#inbox' that aren't real thread IDs."""
    js = unquote(bookmarklet_url())
    assert "id.length < 10" in js
    assert "/^[A-Za-z0-9_-]+$/" in js


def test_bookmarklet_checks_gmail_host_first():
    """#1: must reject non-Gmail hosts so we don't POST random IDs."""
    js = unquote(bookmarklet_url())
    assert "mail.google.com" in js
    assert "QuickLabel only works inside Gmail" in js


def test_bookmarklet_handles_blocked_popup():
    """#2: must check window.open return value and surface a message
    when a popup blocker prevents the new tab."""
    js = unquote(bookmarklet_url())
    assert "if (!w)" in js or "if(!w)" in js
    assert "blocked" in js.lower()


# --------------------------- extract_id_from_hash behavior ---------------------------

@pytest.mark.parametrize("hash_str,expected", [
    # Standard inbox conversation
    ("#inbox/FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX", "FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX"),
    # All-mail / sent / drafts / starred views
    ("#all/FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX", "FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX"),
    ("#sent/FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX", "FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX"),
    ("#starred/FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX", "FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX"),
    # Search results — query may have its own slashes (URL-encoded)
    ("#search/from%3Afoo%40bar.com/FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX",
     "FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX"),
    # Category tabs (Promotions / Social / etc.)
    ("#category/promotions/FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX",
     "FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX"),
    # Hash with query suffix (?compose=..., ?projector=1, etc.) — strip the ?
    ("#inbox/FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX?compose=new",
     "FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX"),
    # API-style hex thread ID (when bookmarklet runs in a context that
    # already gave us the API form)
    ("#inbox/19e0bdaecaf629f6", "19e0bdaecaf629f6"),
])
def test_extract_id_from_valid_hash(hash_str, expected):
    assert extract_id_from_hash(hash_str) == expected


@pytest.mark.parametrize("hash_str", [
    # User on inbox list (no email open)
    "#inbox",
    "#all",
    "#sent",
    "#starred",
    "#drafts",
    # Empty hash (Gmail home before any nav)
    "",
    # Just the # symbol
    "#",
    # Search without an open email
    "#search/from%3Afoo%40bar.com",
    # Settings/labels pages
    "#settings/general",
    "#label/MyLabel",
    # Inbox pagination
    "#inbox/p2",
    # Too-short trailing segment (< 10 chars)
    "#inbox/abc",
    # Invalid chars (e.g., from a non-Gmail page)
    "#inbox/has spaces here",
    "#inbox/has!special@chars",
])
def test_extract_id_rejects_non_email_hashes(hash_str):
    assert extract_id_from_hash(hash_str) is None


def test_extract_id_handles_double_hash():
    """If hash somehow has '##X', leading # is stripped — but the
    next char is still '#' which fails the validation regex."""
    assert extract_id_from_hash("##FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX") is None


def test_extract_id_handles_none_input_safely():
    assert extract_id_from_hash(None) is None  # type: ignore[arg-type]
