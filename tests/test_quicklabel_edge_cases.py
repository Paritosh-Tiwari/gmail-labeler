"""Edge-case battery for QuickLabel.

Covers cases that don't fit cleanly into a per-module test file: weird
inputs, double-submit races, malformed LLM output, XSS attempts via
template rendering, etc. Things I can simulate without a real Gmail.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from quicklabel.actions import ActionChoices, to_label_mutations
from quicklabel.body import extract_body
from quicklabel.headers import EmailFingerprint, fingerprint, parse_address, parse_list_id
from quicklabel.intelligence import (
    IntelligentProposal,
    _coerce_bool,
    parse_llm_response,
    intelligent_propose,
    to_proposal,
)
from quicklabel.preflight import (
    _extract_from_and_list,
    find_filter_conflicts,
    is_destructive,
    should_confirm,
)
from quicklabel.proposal import SenderStats, common_subject_prefix
from quicklabel.signals import Signals, extract_signals
from quicklabel.smart_heuristic import (
    domain_match_label,
    sender_domain_stem,
    suggest_label_smart,
    suggest_actions_smart,
)
from quicklabel.storage import (
    ProposalRecord,
    Storage,
)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


# =====================================================================
# Header / fingerprint edge cases
# =====================================================================

@pytest.mark.parametrize("raw,expected_email", [
    ("", ""),
    ("no-bracket-format@x.com", "no-bracket-format@x.com"),
    ("Just A Name", "just a name"),  # no email -> falls through (current behavior)
    ('"Quoted Name" <real@x.com>', "real@x.com"),
    ("Multi <one@x.com>, <two@x.com>", "one@x.com"),  # takes first <>
    ("UPPER@CASE.COM", "upper@case.com"),  # lowercased
    ("emoji 🚀 <ok@x.com>", "ok@x.com"),  # unicode in display name
])
def test_parse_address_handles_weird_inputs(raw, expected_email):
    name, email = parse_address(raw)
    assert email == expected_email


def test_parse_list_id_handles_messy_inputs():
    assert parse_list_id("") is None
    assert parse_list_id("bare.list.com") == "bare.list.com"
    assert parse_list_id('"My List" <foo.example.com>') == "foo.example.com"
    # Multiple < > — takes the first
    assert parse_list_id("<a.com> stuff <b.com>") == "a.com"


def test_fingerprint_with_no_payload():
    fp = fingerprint({})
    assert fp.sender_email == ""
    assert fp.subject == ""
    assert fp.list_id is None


def test_fingerprint_with_no_headers():
    fp = fingerprint({"payload": {}})
    assert fp.sender_email == ""


def test_fingerprint_uses_first_header_when_duplicated():
    """Some clients include multiple From headers — take the first."""
    msg = {"payload": {"headers": [
        {"name": "From", "value": "first@x.com"},
        {"name": "From", "value": "second@x.com"},
    ]}}
    fp = fingerprint(msg)
    assert fp.sender_email == "first@x.com"


# =====================================================================
# Body extractor edge cases
# =====================================================================

def test_extract_body_unicode_and_emoji():
    body = "Hello 🚀 中文 السلام عليكم — combining chars éà"
    msg = {"payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}}}
    out = extract_body(msg)
    assert "🚀" in out
    assert "中文" in out
    assert "السلام" in out


def test_extract_body_handles_extreme_length():
    """A 1MB body should be truncated to max_chars without exploding."""
    body = "x" * 1_000_000
    msg = {"payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}}}
    out = extract_body(msg, max_chars=500)
    assert len(out) <= 500


def test_extract_body_strips_script_and_style_from_html():
    """Defense in depth — even though Jinja escapes, strip on the way in."""
    html = (
        "<style>body{color:red}</style>"
        "<script>alert('xss')</script>"
        "<p>Real content here</p>"
    )
    msg = {"payload": {"mimeType": "text/html", "body": {"data": _b64(html)}}}
    out = extract_body(msg)
    assert "Real content here" in out
    assert "alert" not in out
    assert "color:red" not in out


def test_extract_body_with_only_attachments():
    """Email with only application/* parts — no text. Should return empty."""
    msg = {"payload": {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "application/pdf", "body": {"data": _b64("fake pdf")}},
            {"mimeType": "application/octet-stream", "body": {"data": _b64("blob")}},
        ],
    }}
    assert extract_body(msg) == ""


# =====================================================================
# Sender domain stem / generic providers
# =====================================================================

@pytest.mark.parametrize("email,expected", [
    ("a@", None),     # no domain at all
    ("@b.com", "b"),  # no local part — stem still works
    ("a@x", None),    # no dot
    ("a@b.co.uk", "b"),  # compound TLD
    ("a@x.y.b.com", "b"),  # subdomain — stem is parent domain
    ("a@x.y.z.foo.com", "foo"),  # deeper subdomain
    ("ALICE@CITI.COM", "citi"),  # case-insensitive
])
def test_sender_domain_stem_edge_cases(email, expected):
    assert sender_domain_stem(email) == expected


def test_sender_domain_stem_short_tlds():
    """Single-letter TLDs (rare in practice — 'b.c'). Function returns the
    second-from-last part. Not strictly correct (real TLDs are 2+ chars)
    but harmless: at worst it produces a poor label suggestion that the
    user can override."""
    assert sender_domain_stem("a@b.c") == "b"


def test_domain_match_returns_none_with_no_existing_labels():
    assert domain_match_label("alerts@citi.com", []) is None


def test_domain_match_handles_label_with_special_chars():
    """Labels can have spaces, ampersands, etc. Match should still work."""
    existing = ["Banking & Finance/Citi (Personal)"]
    assert domain_match_label("alerts@citi.com", existing) == "Banking & Finance/Citi (Personal)"


# =====================================================================
# Common subject prefix detection
# =====================================================================

def test_common_subject_prefix_with_single_subject():
    """One subject can't have a 'common' prefix — return None."""
    assert common_subject_prefix(["Only one subject here"]) == (None, 0)


def test_common_subject_prefix_with_empty_list():
    assert common_subject_prefix([]) == (None, 0)


def test_common_subject_prefix_with_completely_different_subjects():
    assert common_subject_prefix([
        "Apple", "Banana", "Cherry", "Date",
    ]) == (None, 0)


def test_common_subject_prefix_with_unicode():
    """Should work on non-ASCII subjects too."""
    out, n = common_subject_prefix([
        "Weekly digest 第1期",
        "Weekly digest 第2期",
        "Weekly digest 第3期",
    ])
    assert out and "Weekly digest" in out
    assert n == 3


# =====================================================================
# LLM response parsing edge cases
# =====================================================================

def test_parse_llm_response_with_negative_confidence():
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": -0.5,
    })
    p = parse_llm_response(raw)
    assert p.confidence == 0.0


def test_parse_llm_response_with_string_confidence():
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": "0.85",  # string not number
    })
    p = parse_llm_response(raw)
    assert p.confidence == 0.85


def test_parse_llm_response_rejects_empty_label():
    raw = json.dumps({
        "chosen_label": "",
        "is_new_label": True, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.9,
    })
    with pytest.raises(ValueError):
        parse_llm_response(raw)


def test_parse_llm_response_with_xss_label():
    """Label name with HTML/script should pass through; Jinja will escape."""
    raw = json.dumps({
        "chosen_label": "<script>alert('xss')</script>",
        "is_new_label": True, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.9,
    })
    p = parse_llm_response(raw)
    # Parser doesn't sanitize — that's the template's job (Jinja auto-escape).
    assert "<script>" in p.chosen_label


def test_parse_llm_response_handles_null_filter_fields():
    """LLM may return JSON null for unused fields; we treat as missing."""
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from", "from": None, "subject": None, "list_id": None},
        "actions": {"inbox": "keep", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.5,
    })
    p = parse_llm_response(raw)
    # No from, no subject, no list — query is empty, criteria is empty
    assert p.filter_query == ""


def test_parse_llm_response_with_invalid_inbox_value():
    """Malformed inbox value falls through (parser is lenient); to_label_mutations
    will reject downstream."""
    raw = json.dumps({
        "chosen_label": "X", "is_new_label": False, "rationale": "...",
        "filter": {"type": "from", "from": "a@x.com", "subject": None, "list_id": None},
        "actions": {"inbox": "magic", "importance": "default",
                    "mark_read": False, "star": False, "never_spam": False},
        "confidence": 0.5,
    })
    p = parse_llm_response(raw)
    assert p.suggested_actions.inbox == "magic"
    with pytest.raises(ValueError):
        to_label_mutations(p.suggested_actions)


@pytest.mark.parametrize("v,expected", [
    (True, True), (False, False),
    ("true", True), ("True", True), ("TRUE", True),
    ("false", False), ("False", False),
    ("1", True), ("0", False),
    ("yes", True), ("no", False),
    ("on", True), ("off", False),
    (None, False),
    (1, True), (0, False),
    ("garbage", False),  # unknown string -> safe default False
    ("", False),
])
def test_coerce_bool_handles_all_forms(v, expected):
    assert _coerce_bool(v) == expected


# =====================================================================
# Storage concurrency / weird states
# =====================================================================

@pytest.fixture
def store(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "edge.db")


def test_storage_creates_parent_dir_if_missing(tmp_path: Path):
    """Storage with a path inside a non-existent dir should create it."""
    deep = tmp_path / "does" / "not" / "exist" / "yet" / "qq.db"
    s = Storage(deep)
    assert deep.exists()


def test_record_proposal_then_immediate_decision(store: Storage):
    """No race — back-to-back calls work."""
    pid = store.record_proposal(ProposalRecord(
        sender_email="a@x.com", list_id=None, subject_sample="s",
        proposed_label="L", proposed_query="from:a@x.com", recent_count=1,
    ))
    store.record_decision(pid, "applied")
    assert store.get_pending() == []


def test_double_decision_on_same_proposal(store: Storage):
    """Recording a decision twice — second one wins (timestamp updated)."""
    pid = store.record_proposal(ProposalRecord(
        sender_email="a@x.com", list_id=None, subject_sample="s",
        proposed_label="L", proposed_query="q", recent_count=1,
    ))
    store.record_decision(pid, "skipped")
    store.record_decision(pid, "applied")
    # Decision is now 'applied' (last write wins)
    assert store.was_dismissed_for_sender("a@x.com") is False


def test_undo_already_undone_entry_just_re_marks(store: Storage):
    """mark_undone is idempotent at storage layer — endpoint guards against
    user-initiated double-undo, but the storage call itself doesn't error."""
    eid = store.record_apply(
        label_name="X", label_id="L", filter_query="q", filter_id="f1",
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    store.mark_undone(eid)
    first_ts = store.get_apply_entry(eid).undone_at
    store.mark_undone(eid)
    second_ts = store.get_apply_entry(eid).undone_at
    # Either same timestamp (no-op) or updated — both fine; what matters
    # is no exception
    assert first_ts is not None
    assert second_ts is not None


def test_get_pending_with_corrupted_suggested_actions_json(store: Storage):
    """If the JSON column is malformed, pending listing should still work."""
    import sqlite3
    pid = store.record_proposal(ProposalRecord(
        sender_email="a@x.com", list_id=None, subject_sample="s",
        proposed_label="L", proposed_query="q", recent_count=1,
    ))
    # Manually corrupt the JSON
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE proposal_log SET suggested_actions_json = ? WHERE id = ?",
            ("not-valid-json", pid),
        )
    # Should not crash — get_pending returns the row with raw JSON;
    # consumers handle parse errors.
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0].suggested_actions_json == "not-valid-json"


def test_intel_cache_with_corrupted_value(store: Storage):
    """clear_intel + re-set should recover."""
    store.set_intel("key1", "{not json}")
    raw = store.get_intel("key1")
    # Storage layer doesn't parse — returns raw
    assert raw == "{not json}"
    store.clear_intel("key1")
    assert store.get_intel("key1") is None


# =====================================================================
# Preflight edge cases
# =====================================================================

def test_extract_from_handles_messy_query_tokens():
    """Quoted values, mixed case, extra whitespace."""
    out = _extract_from_and_list({"query": '  FROM:Alice@X.COM   list:"My.List.Com"  '})
    assert out[0] == "alice@x.com"
    assert out[1] == "my.list.com"


def test_extract_from_handles_subject_token_in_query():
    """subject: tokens in query are NOT extracted as from/list."""
    out = _extract_from_and_list({"query": "subject:Foo from:a@x.com"})
    # subject: is left for someone else, from: is extracted
    assert out == ("a@x.com", "")


def test_find_conflicts_with_empty_filter_list():
    assert find_filter_conflicts({"from": "a@x.com"}, []) == []


def test_find_conflicts_with_filter_lacking_criteria():
    """Defensive: a filter dict with missing 'criteria' shouldn't crash."""
    out = find_filter_conflicts({"from": "a@x.com"}, [{"id": "f1"}])
    assert out == []


def test_should_confirm_with_zero_existing():
    """Backprop count of 0 is never destructive enough to warrant confirm."""
    assert should_confirm(
        scope="both", actions=ActionChoices(inbox="trash"),
        backprop_count=0, conflicts=[],
    ) is False


def test_is_destructive_categorize_only():
    """Categorize-as is reversible, not destructive."""
    assert is_destructive(ActionChoices(categorize="promotions")) is False


# =====================================================================
# Smart heuristic edge cases
# =====================================================================

def test_suggest_actions_low_volume_marketing_doesnt_skip():
    """Marketing flags are NOT enough alone — need volume to suggest skip."""
    sigs = Signals(has_unsubscribe=True, looks_marketing=True)
    stats = SenderStats(total_from_sender=2, total_from_list=2,
                        common_subject_prefix=None, prefix_match_count=0)
    a = suggest_actions_smart(sigs, stats)
    # Volume threshold is 20; we're at 2 — fall through to default
    assert a.inbox == "keep"


def test_suggest_label_with_no_existing_labels():
    """Empty existing-labels list — fall back to display name heuristic."""
    from quicklabel.headers import EmailFingerprint
    email = EmailFingerprint("m", "alerts@new.com", "New Co", "x", None, False)
    sigs = Signals()
    stats = SenderStats(1, 0, None, 0)
    p = suggest_label_smart(email, sigs, stats, [])
    assert p.suggested_name  # not empty
    assert p.suggested_parent is None


# =====================================================================
# Actions edge cases
# =====================================================================

def test_actions_all_destructive_combinations():
    """Trash + skip-inbox via radio (mutex). Importance + mark_read + star
    + never_spam + categorize all set together."""
    a = ActionChoices(
        inbox="trash", importance="never_important", categorize="updates",
        mark_read=True, star=True, never_spam=True,
    )
    add, remove = to_label_mutations(a)
    assert "TRASH" in add
    assert "STARRED" in add
    assert "CATEGORY_UPDATES" in add
    assert "UNREAD" in remove
    assert "IMPORTANT" in remove
    assert "SPAM" in remove
    # TRASH covers archive, so INBOX shouldn't be double-removed
    assert "INBOX" not in remove


def test_actions_all_default_no_mutations():
    a = ActionChoices()
    assert to_label_mutations(a) == ([], [])


# =====================================================================
# Intelligence fallback path
# =====================================================================

def test_intelligent_propose_records_failure_reason_in_model_field(tmp_path):
    """When Ollama fails, the heuristic-fallback IntelligentProposal should
    have the failure reason visible in the model field."""
    store = Storage(tmp_path / "f.db")
    from quicklabel.headers import EmailFingerprint
    email = EmailFingerprint("m", "a@x.com", "A", "x", None, False)
    stats = SenderStats(1, 0, None, 0)
    sigs = Signals()

    def broken_chat(prompt, **_):
        raise RuntimeError("ollama dead")

    p = intelligent_propose(
        email=email, body="x", signals=sigs, sender_stats=stats,
        existing_labels=[], storage=store, chat_fn=broken_chat,
    )
    assert p.confidence < 0.5
    # We promised to surface the failure reason somewhere
    assert "fallback" in p.rationale.lower() or "ollama" in p.rationale.lower()


def test_intelligent_propose_caches_consistent_after_invalid_json_fallback(tmp_path):
    """Garbage LLM output triggers fallback — but we shouldn't poison the
    cache with the garbage so a future call retries."""
    store = Storage(tmp_path / "f.db")
    from quicklabel.headers import EmailFingerprint
    email = EmailFingerprint("m", "a@x.com", "A", "x", None, False)
    stats = SenderStats(1, 0, None, 0)
    sigs = Signals()

    calls = {"n": 0}

    def garbage_chat(prompt, **_):
        calls["n"] += 1
        return "this is not JSON"

    p1 = intelligent_propose(
        email=email, body="x", signals=sigs, sender_stats=stats,
        existing_labels=[], storage=store, chat_fn=garbage_chat,
    )
    p2 = intelligent_propose(
        email=email, body="x", signals=sigs, sender_stats=stats,
        existing_labels=[], storage=store, chat_fn=garbage_chat,
    )
    # Each intelligent_propose call now also retries once on parse
    # failure (defense against reasoning models with non-deterministic
    # empty / truncated output). So 2 propose calls = 4 chat calls.
    # The key invariant the test guards: garbage is NEVER cached, so
    # the second propose call goes back to the chat_fn instead of
    # returning a cached bad parse.
    assert calls["n"] == 4, "cache shouldn't store invalid JSON"
    assert p1.confidence < 0.5
    assert p2.confidence < 0.5


# =====================================================================
# to_proposal adapter edge cases
# =====================================================================

def test_to_proposal_with_label_having_only_root():
    """Label like 'Receipts' (no slash) — suggested_parent should be None."""
    from quicklabel.headers import EmailFingerprint
    ip = IntelligentProposal(
        chosen_label="Receipts", is_new_label=True, rationale="r",
        filter_query="from:a@x.com", filter_criteria={"from": "a@x.com"},
        suggested_actions=None, confidence=0.9, cache_key="ck",
    )
    email = EmailFingerprint("m", "a@x.com", "A", "x", None, False)
    stats = SenderStats(1, 0, None, 0)
    p = to_proposal(email, stats, ip)
    assert p.label.suggested_name == "Receipts"
    assert p.label.suggested_parent is None


def test_to_proposal_with_deeply_nested_label():
    from quicklabel.headers import EmailFingerprint
    ip = IntelligentProposal(
        chosen_label="A/B/C/D/E", is_new_label=True, rationale="r",
        filter_query="from:a@x.com", filter_criteria={"from": "a@x.com"},
        suggested_actions=None, confidence=0.9, cache_key="ck",
    )
    email = EmailFingerprint("m", "a@x.com", "A", "x", None, False)
    stats = SenderStats(1, 0, None, 0)
    p = to_proposal(email, stats, ip)
    assert p.label.suggested_parent == "A/B/C/D"
    assert p.label.suggested_name == "E"
