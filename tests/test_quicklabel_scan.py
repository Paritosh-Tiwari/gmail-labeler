"""Tests for the QuickLabel scan engine.

Uses a hand-rolled FakeGmailService so we don't need network access.
The fake mimics just the surface scan.py touches:
  - users().messages().list(...).execute() -> {messages: [{id}, ...]}
  - users().messages().get(...).execute() -> message dict
  - users().settings().filters().list(...).execute() -> {filter: [...]}
"""
from __future__ import annotations

from pathlib import Path

import pytest

from quicklabel.scan import _covered_senders, _is_covered, scan_recent
from quicklabel.storage import Storage


# --------------------------- fake gmail service ---------------------------

class _Exec:
    def __init__(self, value):
        self._v = value
    def execute(self):
        return self._v


class _FakeMessages:
    def __init__(self, by_id: dict, list_response: list[dict]):
        self.by_id = by_id
        self.list_response = list_response
    def list(self, **kwargs):
        return _Exec({"messages": self.list_response})
    def get(self, *, userId, id, format, metadataHeaders=None):
        return _Exec(self.by_id[id])
    def batchModify(self, **kwargs):
        return _Exec({})


class _FakeFilters:
    def __init__(self, filters: list[dict]):
        self.filters = filters
    def list(self, **kwargs):
        return _Exec({"filter": self.filters})


class _FakeSettings:
    def __init__(self, filters: list[dict]):
        self._filters = _FakeFilters(filters)
    def filters(self):
        return self._filters


class _FakeUsers:
    def __init__(self, msgs: _FakeMessages, settings: _FakeSettings):
        self._msgs = msgs
        self._settings = settings
    def messages(self):
        return self._msgs
    def settings(self):
        return self._settings


class FakeService:
    def __init__(self, messages_by_id: dict, list_response: list[dict],
                 filters: list[dict] | None = None):
        self._users = _FakeUsers(
            _FakeMessages(messages_by_id, list_response),
            _FakeSettings(filters or []),
        )
    def users(self):
        return self._users


def _msg(mid: str, sender: str, subject: str, list_id: str | None = None) -> dict:
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
    ]
    if list_id:
        headers.append({"name": "List-ID", "value": list_id})
    return {"id": mid, "payload": {"headers": headers}}


# --------------------------- _covered_senders / _is_covered ---------------------------

def test_covered_senders_extracts_from_field():
    filters = [
        {"id": "1", "criteria": {"from": "Already@Filtered.com"}, "action": {"addLabelIds": ["X"]}},
        {"id": "2", "criteria": {"query": "list:foo.example.com"}, "action": {"addLabelIds": ["Y"]}},
        {"id": "3", "criteria": {"query": 'from:bar@baz.com subject:"Weekly"'}, "action": {"addLabelIds": ["Z"]}},
    ]
    covered = _covered_senders(filters)
    assert "already@filtered.com" in covered["emails"]
    assert "bar@baz.com" in covered["emails"]
    assert "foo.example.com" in covered["lists"]


def test_is_covered_matches_email():
    covered = {"emails": {"a@x.com"}, "lists": set()}
    from quicklabel.headers import EmailFingerprint
    fp = EmailFingerprint("m", "a@x.com", "A", "subj", None, False)
    assert _is_covered(fp, covered) is True
    fp2 = EmailFingerprint("m", "b@x.com", "B", "subj", None, False)
    assert _is_covered(fp2, covered) is False


def test_is_covered_matches_list():
    covered = {"emails": set(), "lists": {"foo.example.com"}}
    from quicklabel.headers import EmailFingerprint
    fp = EmailFingerprint("m", "anyone@example.com", "X", "s", "foo.example.com", False)
    assert _is_covered(fp, covered) is True


# --------------------------- scan_recent end-to-end ---------------------------

@pytest.fixture
def store(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "scan_test.db")


def test_scan_groups_by_sender_and_proposes(store: Storage):
    by_id = {
        "m1": _msg("m1", "News <news@example.com>", "Daily digest 1"),
        "m2": _msg("m2", "News <news@example.com>", "Daily digest 2"),
        "m3": _msg("m3", "Other <other@thing.com>", "Single thing"),
    }
    listing = [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
    svc = FakeService(by_id, listing, filters=[])

    pending = scan_recent(svc, store)

    assert len(pending) == 2
    senders = {p.sender_email for p in pending}
    assert senders == {"news@example.com", "other@thing.com"}
    # news has 2 recent, other has 1 -> news comes first
    assert pending[0].sender_email == "news@example.com"
    assert pending[0].recent_count == 2
    assert pending[1].recent_count == 1


def test_scan_skips_senders_we_already_applied_to(store: Storage):
    """Senders that THIS tool already applied a label to (and not undone)
    should be excluded from the next scan — that work is done."""
    # Pre-record an apply on filtered@x.com
    store.record_apply(
        label_name="X", label_id="L1",
        filter_query="from:filtered@x.com", filter_id="f1",
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    by_id = {
        "m1": _msg("m1", "Filtered <filtered@x.com>", "thing"),
        "m2": _msg("m2", "Fresh <fresh@x.com>", "thing"),
    }
    listing = [{"id": "m1"}, {"id": "m2"}]
    svc = FakeService(by_id, listing, filters=[])
    pending = scan_recent(svc, store)
    assert [p.sender_email for p in pending] == ["fresh@x.com"]


def test_scan_undone_apply_no_longer_excludes(store: Storage):
    """If the apply was undone, the sender is fair game again."""
    apply_id = store.record_apply(
        label_name="X", label_id="L1",
        filter_query="from:filtered@x.com", filter_id="f1",
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    store.mark_undone(apply_id)
    by_id = {"m1": _msg("m1", "Filtered <filtered@x.com>", "thing")}
    svc = FakeService(by_id, [{"id": "m1"}], filters=[])
    pending = scan_recent(svc, store)
    assert [p.sender_email for p in pending] == ["filtered@x.com"]


def test_scan_includes_senders_with_existing_filter(store: Storage):
    """Senders that already have a filter should still appear — the queue
    UI surfaces the existing label so the user can choose to overwrite."""
    by_id = {
        "m1": _msg("m1", "Filtered <filtered@x.com>", "thing"),
        "m2": _msg("m2", "Fresh <fresh@x.com>", "thing"),
    }
    listing = [{"id": "m1"}, {"id": "m2"}]
    filters = [
        {"id": "f1", "criteria": {"from": "filtered@x.com"}, "action": {"addLabelIds": ["X"]}},
    ]
    svc = FakeService(by_id, listing, filters=filters)

    pending = scan_recent(svc, store)
    senders = {p.sender_email for p in pending}
    assert senders == {"filtered@x.com", "fresh@x.com"}


def test_scan_skips_dismissed_senders(store: Storage):
    # Pre-dismiss a sender
    from quicklabel.storage import ProposalRecord
    pid = store.record_proposal(ProposalRecord(
        sender_email="dismissed@x.com", list_id=None,
        subject_sample="x", proposed_label="L",
        proposed_query="from:dismissed@x.com", recent_count=1,
    ))
    store.record_decision(pid, "skipped")

    by_id = {
        "m1": _msg("m1", "Dismissed <dismissed@x.com>", "thing"),
        "m2": _msg("m2", "Fresh <fresh@x.com>", "thing"),
    }
    listing = [{"id": "m1"}, {"id": "m2"}]
    svc = FakeService(by_id, listing, filters=[])

    pending = scan_recent(svc, store)
    assert [p.sender_email for p in pending] == ["fresh@x.com"]


def test_scan_groups_by_list_id_when_present(store: Storage):
    by_id = {
        "m1": _msg("m1", "News1 <a@news.com>", "Issue 1", list_id="<weekly.news.com>"),
        "m2": _msg("m2", "News2 <b@news.com>", "Issue 2", list_id="<weekly.news.com>"),
    }
    listing = [{"id": "m1"}, {"id": "m2"}]
    svc = FakeService(by_id, listing, filters=[])

    pending = scan_recent(svc, store)
    # Same list-id -> grouped into one proposal
    assert len(pending) == 1
    assert pending[0].list_id == "weekly.news.com"
    assert pending[0].recent_count == 2


def test_scan_updates_last_scan_at(store: Storage):
    assert store.get_last_scan_at() is None
    by_id = {"m1": _msg("m1", "x@y.com", "z")}
    svc = FakeService(by_id, [{"id": "m1"}], filters=[])
    scan_recent(svc, store)
    assert store.get_last_scan_at() is not None


def test_scan_dedup_when_run_twice(store: Storage):
    by_id = {"m1": _msg("m1", "x@y.com", "subj")}
    svc = FakeService(by_id, [{"id": "m1"}], filters=[])
    scan_recent(svc, store)
    pending_first = store.get_pending()
    scan_recent(svc, store)
    pending_second = store.get_pending()
    # Same sender — should still be a single pending row
    assert len(pending_first) == 1
    assert len(pending_second) == 1
    assert pending_second[0].id == pending_first[0].id


def test_scan_skips_messages_without_sender(store: Storage):
    # Message with no From header
    by_id = {"m1": {"id": "m1", "payload": {"headers": [{"name": "Subject", "value": "x"}]}}}
    svc = FakeService(by_id, [{"id": "m1"}], filters=[])
    pending = scan_recent(svc, store)
    assert pending == []
