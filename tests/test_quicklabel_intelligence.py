"""Tests for the LLM-backed intelligent proposer.

These tests use a fake chat function so we don't hit Ollama.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quicklabel.headers import EmailFingerprint
from quicklabel.intelligence import (
    IntelligentProposal,
    build_prompt,
    intelligent_propose,
    parse_llm_response,
    to_proposal,
)
from quicklabel.proposal import SenderStats
from quicklabel.signals import Signals
from quicklabel.storage import Storage


def _email() -> EmailFingerprint:
    return EmailFingerprint(
        msg_id="m1", sender_email="news@sequoiacap.com",
        sender_name="Sequoia Capital", subject="Weekly insights #234",
        list_id="weekly.sequoiacap.com", list_unsubscribe=True,
    )


def _stats() -> SenderStats:
    return SenderStats(
        total_from_sender=87, total_from_list=87,
        common_subject_prefix="Weekly insights", prefix_match_count=80,
    )


def _signals() -> Signals:
    return Signals(
        has_unsubscribe=True, is_no_reply=False,
        looks_marketing=True, looks_transactional=False, is_personal=False,
    )


# --------------------------- prompt building ---------------------------

def test_build_prompt_includes_required_context():
    prompt = build_prompt(
        email=_email(), body="A weekly look at AI investing trends...",
        signals=_signals(), sender_stats=_stats(),
        existing_labels=["AI Newsletters", "AI Newsletters/Foo", "Finance"],
    )
    assert "news@sequoiacap.com" in prompt
    assert "Weekly insights #234" in prompt
    assert "weekly.sequoiacap.com" in prompt
    assert "AI Newsletters" in prompt
    assert "Finance" in prompt
    assert "87" in prompt  # sender count
    # JSON schema instructions present
    assert "JSON" in prompt or "json" in prompt
    assert "confidence" in prompt.lower()


def test_build_prompt_truncates_huge_label_lists():
    """If the user has 5000 labels, we don't dump them all in the prompt."""
    labels = [f"Label/Sub{i}" for i in range(2000)]
    prompt = build_prompt(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=labels,
    )
    # Should be capped (max ~500 labels in prompt)
    assert prompt.count("Label/Sub") <= 600


# --------------------------- response parsing ---------------------------

def test_parse_clean_json_response():
    raw = json.dumps({
        "chosen_label": "AI Newsletters/Sequoia",
        "is_new_label": True,
        "rationale": "Sender is Sequoia Capital, content is investment thesis.",
        "filter": {"type": "list_id", "from": None, "subject": None,
                   "list_id": "weekly.sequoiacap.com"},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.88,
    })
    p = parse_llm_response(raw)
    assert p.chosen_label == "AI Newsletters/Sequoia"
    assert p.is_new_label is True
    assert p.confidence == 0.88
    assert "list:weekly.sequoiacap.com" in p.filter_query


def test_parse_handles_extra_text_around_json():
    """Some local models prefix/suffix prose around the JSON object."""
    raw = (
        "Here is my analysis:\n"
        '{"chosen_label": "Receipts", "is_new_label": false, '
        '"rationale": "Order receipt.", '
        '"filter": {"type": "from", "from": "noreply@shop.com", "subject": null, "list_id": null}, '
        '"actions": {"inbox": "skip", "importance": "default", '
        '"mark_read": true, "star": false, "never_spam": false}, '
        '"confidence": 0.7}'
        "\nDone."
    )
    p = parse_llm_response(raw)
    assert p.chosen_label == "Receipts"
    assert p.suggested_actions.inbox == "skip"
    assert p.suggested_actions.mark_read is True


def test_parse_invalid_json_raises():
    with pytest.raises(ValueError):
        parse_llm_response("nope, this is not json at all")


def test_parse_handles_string_booleans_from_local_models():
    """Bug #4 regression: bool('false') is True. Coerce safely."""
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": "false", "star": "true", "never_spam": "False"},
        "confidence": 0.8,
    })
    p = parse_llm_response(raw)
    assert p.suggested_actions.mark_read is False
    assert p.suggested_actions.star is True
    assert p.suggested_actions.never_spam is False


def test_parse_picks_up_categorize():
    """Bug #5 regression: categorize was missing from parser entirely."""
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "categorize": "promotions",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.8,
    })
    p = parse_llm_response(raw)
    assert p.suggested_actions.categorize == "promotions"


def test_build_prompt_includes_categorize_in_schema():
    """Bug #5 regression: prompt schema must list categorize as a field."""
    prompt = build_prompt(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
    )
    assert "categorize" in prompt
    # And mention valid categorize values
    assert "promotions" in prompt.lower()


def test_parse_clamps_confidence_to_unit_interval():
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 1.5,
    })
    p = parse_llm_response(raw)
    assert p.confidence == 1.0


def test_parse_filter_from_subject_combines_to_query():
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from_subject", "from": "a@x.com",
                   "subject": "Weekly", "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.8,
    })
    p = parse_llm_response(raw)
    assert "from:a@x.com" in p.filter_query
    assert "Weekly" in p.filter_query


def test_parse_filter_from_keyword_uses_query_field_not_subject():
    """from_keyword reads the 'keyword' field and produces a Gmail filter
    that matches the keyword anywhere in the message (uses the `query`
    criterion, not `subject`). This is the right shape for senders whose
    subjects are one-off but whose bodies share a distinctive word."""
    raw = json.dumps({
        "chosen_label": "Learning/Maven", "is_new_label": True,
        "rationale": "One-off course announcement.",
        "filter": {"type": "from_keyword", "from": "hello@maven.com",
                   "subject": None, "keyword": "Maven course", "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.78,
    })
    p = parse_llm_response(raw)
    assert "from:hello@maven.com" in p.filter_query
    assert '"Maven course"' in p.filter_query  # multi-word phrase quoted
    # Filter criteria sent to Gmail uses the `query` field, NOT `subject`
    # (subject would only match the subject line, missing keyword-in-body
    # which is the whole point of this filter type).
    assert p.filter_criteria.get("query") == '"Maven course"'
    assert p.filter_criteria.get("from") == "hello@maven.com"
    assert "subject" not in p.filter_criteria


def test_parse_filter_from_keyword_single_word_unquoted():
    """Single-word keywords don't need quoting in the Gmail query."""
    raw = json.dumps({
        "chosen_label": "Bills", "is_new_label": True, "rationale": "...",
        "filter": {"type": "from_keyword", "from": "billing@x.com",
                   "subject": None, "keyword": "invoice", "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.8,
    })
    p = parse_llm_response(raw)
    assert p.filter_query == "from:billing@x.com invoice"
    assert p.filter_criteria == {"from": "billing@x.com", "query": "invoice"}


def test_build_prompt_includes_from_keyword_in_filter_type_enum():
    """from_keyword must be discoverable in the OUTPUT schema or the LLM
    won't ever emit it."""
    prompt = build_prompt(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
    )
    assert "from_keyword" in prompt
    assert "keyword" in prompt


def test_body_excerpt_uses_head_and_tail_when_long():
    """Long bodies get head + tail rather than head-only, so distinguishing
    footer signals (sender brand, unsubscribe context) reach the LLM."""
    from quicklabel.intelligence import (
        BODY_HEAD_CHARS, BODY_TAIL_CHARS, _body_excerpt,
    )
    head_marker = "HEAD_MARKER_AT_TOP"
    tail_marker = "TAIL_MARKER_AT_BOTTOM"
    middle_filler = "x" * (BODY_HEAD_CHARS + BODY_TAIL_CHARS + 2000)
    body = head_marker + middle_filler + tail_marker
    excerpt = _body_excerpt(body)
    assert head_marker in excerpt
    assert tail_marker in excerpt
    assert "[middle truncated]" in excerpt
    # Bulk of the middle should be gone
    assert len(excerpt) < len(body)


def test_body_excerpt_returns_whole_body_when_short():
    from quicklabel.intelligence import _body_excerpt
    short = "Just a short message body."
    assert _body_excerpt(short) == short


# --------------------------- intelligent_propose end-to-end ---------------------------

@pytest.fixture
def store(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "intel_test.db")


def _fake_chat(payload: dict) -> str:
    """Return a chat_fn that always returns the given payload as JSON."""
    s = json.dumps(payload)
    def f(prompt: str, **_) -> str:
        return s
    return f


def test_intelligent_propose_calls_llm_and_caches(store: Storage):
    chat_fn = _fake_chat({
        "chosen_label": "AI Newsletters/Sequoia",
        "is_new_label": True,
        "rationale": "Investment newsletter.",
        "filter": {"type": "list_id", "from": None, "subject": None,
                   "list_id": "weekly.sequoiacap.com"},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.9,
    })

    p = intelligent_propose(
        email=_email(), body="Investing in AI", signals=_signals(),
        sender_stats=_stats(),
        existing_labels=["AI Newsletters", "Finance"],
        storage=store, chat_fn=chat_fn,
    )
    assert p.chosen_label == "AI Newsletters/Sequoia"
    assert p.confidence == 0.9

    # Now should be cached
    cache_key = p.cache_key
    assert store.get_intel(cache_key) is not None


def test_intelligent_propose_uses_cache_on_second_call(store: Storage):
    calls = {"n": 0}
    def counting_chat(prompt: str, **_) -> str:
        calls["n"] += 1
        return json.dumps({
            "chosen_label": "X", "is_new_label": False, "rationale": "r",
            "filter": {"type": "from", "from": "news@sequoiacap.com",
                       "subject": None, "list_id": None},
            "actions": {"inbox": "keep", "importance": "default",
                        "mark_read": False, "star": False, "never_spam": False},
            "confidence": 0.8,
        })

    args = dict(email=_email(), body="hello", signals=_signals(),
                sender_stats=_stats(), existing_labels=[],
                storage=store, chat_fn=counting_chat)
    intelligent_propose(**args)
    intelligent_propose(**args)
    assert calls["n"] == 1


def test_intelligent_propose_falls_back_on_llm_failure(store: Storage):
    def broken_chat(prompt: str, **_) -> str:
        raise RuntimeError("ollama unreachable")

    p = intelligent_propose(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
        storage=store, chat_fn=broken_chat,
    )
    # Fallback returns a low-confidence proposal from the heuristic
    assert p.confidence < 0.5
    assert p.chosen_label  # something was proposed
    assert "fallback" in p.rationale.lower() or "heuristic" in p.rationale.lower()


def test_intelligent_propose_falls_back_on_invalid_json(store: Storage):
    def garbage_chat(prompt: str, **_) -> str:
        return "the answer is 42"

    p = intelligent_propose(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
        storage=store, chat_fn=garbage_chat,
    )
    assert p.confidence < 0.5


# --------------------------- to_proposal adapter ---------------------------

def test_to_proposal_for_existing_label_sets_no_parent():
    ip = IntelligentProposal(
        chosen_label="Finance",
        is_new_label=False,
        rationale="...",
        filter_query="from:bank@x.com",
        filter_criteria={"from": "bank@x.com"},
        suggested_actions=None,
        confidence=0.9,
        cache_key="ck",
    )
    p = to_proposal(_email(), _stats(), ip)
    # When using existing label, suggested_name = full chosen label, no parent.
    assert p.label.suggested_name == "Finance"
    assert p.label.suggested_parent in (None, "")


def test_to_proposal_for_new_nested_label_splits_parent():
    ip = IntelligentProposal(
        chosen_label="AI Newsletters/Sequoia",
        is_new_label=True,
        rationale="...",
        filter_query="list:weekly.sequoiacap.com",
        filter_criteria={"query": "list:weekly.sequoiacap.com"},
        suggested_actions=None,
        confidence=0.85,
        cache_key="ck",
    )
    p = to_proposal(_email(), _stats(), ip)
    assert p.label.suggested_parent == "AI Newsletters"
    assert p.label.suggested_name == "Sequoia"
