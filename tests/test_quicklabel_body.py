"""Tests for the Gmail body extractor."""
from __future__ import annotations

import base64

from quicklabel.body import extract_body


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def test_simple_text_plain():
    msg = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64("Hello, world.\nThis is a test.")},
        }
    }
    assert extract_body(msg) == "Hello, world.\nThis is a test."


def test_multipart_alternative_prefers_plain():
    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Plain version")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>HTML version</p>")}},
            ],
        }
    }
    assert "Plain version" in extract_body(msg)


def test_html_only_falls_back_to_html_to_text():
    msg = {
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _b64("<p>Hello <b>bold</b> world</p><br><p>Second para</p>")},
        }
    }
    out = extract_body(msg)
    assert "Hello" in out
    assert "bold" in out
    assert "<p>" not in out  # HTML stripped
    assert "<br>" not in out


def test_nested_multipart():
    inner = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Inner plain")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>Inner html</p>")}},
        ],
    }
    msg = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [inner, {"mimeType": "application/pdf", "body": {"data": _b64("ignore")}}],
        }
    }
    assert "Inner plain" in extract_body(msg)


def test_truncation():
    long_text = "x" * 10_000
    msg = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(long_text)},
        }
    }
    out = extract_body(msg, max_chars=200)
    assert len(out) <= 200


def test_empty_message_returns_empty_string():
    msg = {"payload": {}}
    assert extract_body(msg) == ""


def test_strips_quoted_reply_blocks():
    body = (
        "Hi Alice,\n"
        "Quick question about the proposal.\n"
        "\n"
        "Thanks,\n"
        "Bob\n"
        "\n"
        "On Mon, Mar 1 2026, Alice <a@x.com> wrote:\n"
        "> Original message that should be excluded\n"
        "> from the extracted body\n"
    )
    msg = {"payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}}}
    out = extract_body(msg)
    assert "Quick question" in out
    assert "Original message that should be excluded" not in out


def test_handles_missing_data_in_body():
    """When parts have no data field, skip them gracefully."""
    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {}},  # no 'data' key
                {"mimeType": "text/html", "body": {"data": _b64("<p>fallback</p>")}},
            ],
        }
    }
    out = extract_body(msg)
    assert "fallback" in out


def test_falls_through_to_html_when_plain_data_decodes_to_empty():
    """Bug #12 regression: an empty-decode text/plain part used to
    'win' the walk and shadow a usable text/html sibling, so the body
    came back empty even though HTML was available."""
    # Non-empty data field that base64-decodes to empty bytes.
    # '====' is valid base64 padding-only and decodes to b''.
    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain",
                 "body": {"data": "===="}},  # decodes to empty
                {"mimeType": "text/html",
                 "body": {"data": _b64("<p>HTML fallback</p>")}},
            ],
        }
    }
    out = extract_body(msg)
    assert "HTML fallback" in out
