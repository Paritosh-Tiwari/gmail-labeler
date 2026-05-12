"""Tests for Gmail URL permalink ID decoder."""
from __future__ import annotations

import pytest

from quicklabel.gmail_id import (
    decode_permalink,
    looks_like_api_id,
    looks_like_permalink,
)


@pytest.mark.parametrize("s,expected", [
    ("19e0a308065b6875", True),
    ("19e0bdaecaf629f6", True),
    ("FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX", False),  # has uppercase, too long
    ("ABCDEFGHIJ", False),  # has vowels - not valid hex
    ("", False),
])
def test_looks_like_api_id(s, expected):
    assert looks_like_api_id(s) is expected


@pytest.mark.parametrize("s,expected", [
    ("FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX", True),
    ("19e0a308065b6875", False),  # hex, has vowels (a, e) - not in base-40 alphabet
    ("FMfcgz", False),  # too short
    ("", False),
])
def test_looks_like_permalink(s, expected):
    assert looks_like_permalink(s) is expected


def test_decode_permalink_known_value():
    """Verified against the user's actual Gmail email permalink."""
    decoded = decode_permalink("FMfcgzQgLjWTDMKwKVNdsPmqBfvLLvbX")
    assert decoded == "19e0bdaecaf629f6"


def test_decode_permalink_returns_none_for_non_permalink():
    assert decode_permalink("19e0a308065b6875") is None
    assert decode_permalink("") is None
    assert decode_permalink("not_a_permalink") is None


def test_decode_permalink_handles_garbage_gracefully():
    # All-base40-alphabet chars but doesn't decode to f:<digits>
    assert decode_permalink("BBBBBBBBBBBBBBBBBBBB") is None
