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


def test_parse_filter_keyword_only_no_from_clause():
    """keyword_only filters drop the from: clause entirely so the same
    category (e.g. OTP / verification code) gets caught regardless of
    sender — Okta, Google, Slack, banks all emit different sender
    addresses for the same email category."""
    raw = json.dumps({
        "chosen_label": "Security/OTP", "is_new_label": False,
        "rationale": "Category arrives from many senders.",
        "filter": {"type": "keyword_only", "from": None, "subject": None,
                   "keyword": '"verification code" OR "one-time code"',
                   "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.85,
    })
    p = parse_llm_response(raw)
    assert "from:" not in p.filter_query
    assert '"verification code"' in p.filter_query
    assert '"one-time code"' in p.filter_query
    # Criteria sent to Gmail: no from, no subject — just the body query
    assert "from" not in p.filter_criteria
    assert "subject" not in p.filter_criteria
    assert p.filter_criteria.get("query") == '"verification code" OR "one-time code"'


def test_parse_filter_subject_only_no_from_clause():
    """subject_only filters use a stable subject template across many
    senders. Common for cross-retailer shipping confirmations."""
    raw = json.dumps({
        "chosen_label": "Shopping/Shipping", "is_new_label": True,
        "rationale": "Stable subject across many retailers.",
        "filter": {"type": "subject_only", "from": None,
                   "subject": "Your order has shipped",
                   "keyword": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.8,
    })
    p = parse_llm_response(raw)
    assert "from:" not in p.filter_query
    assert 'subject:"Your order has shipped"' in p.filter_query
    assert "from" not in p.filter_criteria
    assert p.filter_criteria.get("subject") == "Your order has shipped"


def test_parse_filter_keyword_already_composed_query_passes_through():
    """When the LLM emits a keyword that's already a composed Gmail query
    (contains quotes or OR), we must NOT re-quote it — that would produce
    invalid Gmail search syntax."""
    raw = json.dumps({
        "chosen_label": "OTP", "is_new_label": True, "rationale": "...",
        "filter": {"type": "keyword_only", "from": None, "subject": None,
                   "keyword": '"verification code" OR "security code"',
                   "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.8,
    })
    p = parse_llm_response(raw)
    # The pre-composed query passes through verbatim — no double-quoting.
    assert p.filter_query == '"verification code" OR "security code"'


def test_build_prompt_includes_new_filter_types_in_schema():
    """subject_only and keyword_only must appear in the OUTPUT schema
    enum or the LLM won't emit them."""
    prompt = build_prompt(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
    )
    assert "subject_only" in prompt
    assert "keyword_only" in prompt


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


# --------------------------- new prompt inputs ---------------------------

def test_labels_block_orders_by_usage_count():
    from quicklabel.intelligence import _format_labels_block
    block = _format_labels_block(
        labels=["Finance", "Newsletters/Tech", "Old/Abandoned", "Travel"],
        usage_counts={"Newsletters/Tech": 12, "Finance": 4},
    )
    lines = block.splitlines()
    # Used labels first (desc by count), then unused alphabetical
    assert lines[0].startswith("- Newsletters/Tech")
    assert "12×" in lines[0]
    assert lines[1].startswith("- Finance")
    assert "4×" in lines[1]
    # Unused labels follow, alphabetically
    assert lines[2].startswith("- Old/Abandoned")
    assert "×" not in lines[2]
    assert lines[3].startswith("- Travel")


def test_labels_block_handles_empty():
    from quicklabel.intelligence import _format_labels_block
    assert _format_labels_block([], None) == "(none yet)"


def test_criteria_to_gmail_query_renders_known_keys():
    from quicklabel.intelligence import _criteria_to_gmail_query
    assert _criteria_to_gmail_query(
        {"from": "x@y.com", "subject": "Receipt"}
    ) == 'from:x@y.com subject:"Receipt"'
    assert _criteria_to_gmail_query(
        {"query": "list:weekly.example.com"}
    ) == "list:weekly.example.com"
    assert _criteria_to_gmail_query(
        {"from": "a@b.com", "hasAttachment": True}
    ) == "from:a@b.com has:attachment"


def test_format_existing_filters_caps_at_n():
    from quicklabel.intelligence import _format_existing_filters
    filters = [{"criteria": {"from": f"sender{i}@x.com"}} for i in range(50)]
    out = _format_existing_filters(filters, cap=5)
    assert out.count("\n") == 4  # 5 lines = 4 newlines
    assert "sender0@x.com" in out
    assert "sender49@x.com" not in out


def test_format_existing_filters_empty_input():
    from quicklabel.intelligence import _format_existing_filters
    assert _format_existing_filters([]) == "(none)"
    assert _format_existing_filters(None) == "(none)"


def test_format_recent_applies_renders_label_and_filter():
    from quicklabel.intelligence import _format_recent_applies
    out = _format_recent_applies([
        {"label_name": "Finance/Citi", "filter_query": "from:alerts@citi.com"},
        {"label_name": "Newsletters/Tech", "filter_query": "list:weekly.x.com"},
    ])
    assert "Finance/Citi" in out
    assert "from:alerts@citi.com" in out
    assert "Newsletters/Tech" in out


def test_format_recent_applies_empty():
    from quicklabel.intelligence import _format_recent_applies
    assert "(no recent" in _format_recent_applies(None)
    assert "(no recent" in _format_recent_applies([])


def test_build_prompt_includes_new_sections_when_provided():
    prompt = build_prompt(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(),
        existing_labels=["Newsletters/Tech", "Finance"],
        label_usage_counts={"Newsletters/Tech": 7},
        recent_applies=[
            {"label_name": "Newsletters/Tech/Sequoia",
             "filter_query": "list:weekly.sequoiacap.com"}
        ],
        existing_filters=[
            {"criteria": {"from": "alerts@citi.com", "subject": "Charged"}}
        ],
    )
    # Usage count shown on the label
    assert "Newsletters/Tech" in prompt and "7×" in prompt
    # Recent applies section
    assert "RECENT LABELS THIS USER APPLIED" in prompt
    assert "Newsletters/Tech/Sequoia" in prompt
    # Existing filters section
    assert "EXISTING GMAIL FILTERS" in prompt
    assert "from:alerts@citi.com" in prompt
    # JSON format reminder
    assert "JSON FORMAT REMINDERS" in prompt
    assert "Booleans MUST be unquoted" in prompt


def test_existing_filters_block_encourages_reuse_not_avoidance():
    """The prompt should tell the LLM that if an existing filter fits,
    propose the same one (so the new label is attached) — NOT to avoid
    duplicates. The earlier 'don't propose a duplicate' framing
    discouraged the desired reuse behavior."""
    prompt = build_prompt(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
        existing_filters=[{"criteria": {"from": "alerts@citi.com"}}],
    )
    # Reuse language present
    assert "propose the SAME filter" in prompt or "propose the same filter" in prompt.lower()
    # Anti-duplicate language gone
    assert "don't propose a duplicate" not in prompt.lower()


def test_build_prompt_shows_gmail_category_when_present():
    from quicklabel.headers import EmailFingerprint
    email = EmailFingerprint(
        msg_id="m1", sender_email="a@b.com", sender_name="A",
        subject="x", list_id=None, list_unsubscribe=False,
        gmail_category="Promotions",
    )
    prompt = build_prompt(
        email=email, body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
    )
    assert "Gmail's own category: Promotions" in prompt


def test_build_prompt_indicates_no_category_when_absent():
    prompt = build_prompt(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
    )
    # _email() doesn't set gmail_category
    assert "(none assigned)" in prompt


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


def test_intelligent_propose_retries_on_empty_response(store: Storage):
    """Reasoning models like gpt-oss:20b sometimes return empty visible
    output because their hidden 'thinking' burned the num_predict budget.
    First call empty -> retry once with bigger budget -> use that."""
    calls = []

    def flaky_chat(prompt: str, **kwargs) -> str:
        calls.append(kwargs.get("num_predict"))
        if len(calls) == 1:
            return ""  # first call: empty (simulates reasoning-burnt budget)
        return json.dumps({  # retry: good JSON
            "chosen_label": "FI Updates/Monarch", "is_new_label": True,
            "rationale": "Personal finance update.",
            "filter": {"type": "from", "from": "email@email.monarch.com",
                       "subject": None, "list_id": None},
            "actions": {"inbox": "keep", "importance": "default",
                        "mark_read": False, "star": False, "never_spam": False},
            "confidence": 0.8,
        })

    p = intelligent_propose(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
        storage=store, chat_fn=flaky_chat,
    )
    # Retry succeeded — got the good JSON, not the heuristic fallback
    assert p.chosen_label == "FI Updates/Monarch"
    assert p.confidence == 0.8
    assert len(calls) == 2  # one initial + one retry
    # The retry call used a bigger num_predict than the initial
    assert calls[1] is None or calls[1] > 1500


def test_qwen3_prompt_shim_appends_no_think():
    """Qwen3 ships with thinking mode ON; /no_think disables it for a turn."""
    from quicklabel.intelligence import _prepare_prompt_for_model
    prompt = "Pick a label for this email."
    out = _prepare_prompt_for_model(prompt, "qwen3:14b")
    assert out.endswith("/no_think")
    assert prompt in out


def test_qwen3_prompt_shim_idempotent():
    """If /no_think is already present, don't append a second one."""
    from quicklabel.intelligence import _prepare_prompt_for_model
    prompt = "Pick a label for this email.\n\n/no_think"
    out = _prepare_prompt_for_model(prompt, "qwen3:14b")
    assert out.count("/no_think") == 1


def test_qwen3_prompt_shim_matches_variants():
    """Catches all qwen3 tag variants regardless of size/quant suffix."""
    from quicklabel.intelligence import _prepare_prompt_for_model
    for model in ("qwen3:14b", "qwen3:32b", "qwen3:8b-instruct-q4_K_M", "QWEN3:7B"):
        out = _prepare_prompt_for_model("test", model)
        assert "/no_think" in out, f"shim missed {model}"


def test_no_shim_for_non_qwen3_models():
    """Other models — qwen2.5, gemma, llama, mistral, gpt-oss — should
    not get /no_think appended (it's a Qwen3-specific token)."""
    from quicklabel.intelligence import _prepare_prompt_for_model
    for model in ("qwen2.5:14b", "gemma3:27b", "llama3", "mistral",
                  "gpt-oss:20b", "phi-4"):
        out = _prepare_prompt_for_model("test", model)
        assert "/no_think" not in out, f"shim applied to {model} by mistake"


def test_intelligent_propose_falls_back_when_retry_also_fails(store: Storage):
    """Two empty responses in a row should still fall back cleanly."""
    def empty_chat(prompt: str, **_) -> str:
        return ""

    p = intelligent_propose(
        email=_email(), body="x", signals=_signals(),
        sender_stats=_stats(), existing_labels=[],
        storage=store, chat_fn=empty_chat,
    )
    assert p.confidence < 0.5
    assert "heuristic" in p.rationale.lower() or "fallback" in p.rationale.lower()


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
