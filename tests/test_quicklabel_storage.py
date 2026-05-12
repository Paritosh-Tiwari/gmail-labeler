"""Tests for the QuickLabel SQLite storage layer."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quicklabel.storage import (
    ProposalRecord,
    Storage,
)


@pytest.fixture
def store(tmp_path: Path) -> Storage:
    db = tmp_path / "test.db"
    return Storage(db)


def test_init_creates_tables(store: Storage):
    with sqlite3.connect(store.db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "proposal_log" in names
    assert "scan_state" in names


def test_record_proposal_then_get_pending(store: Storage):
    rec = ProposalRecord(
        sender_email="news@example.com",
        list_id=None,
        subject_sample="Weekly digest",
        proposed_label="Example News",
        proposed_query="from:news@example.com",
        recent_count=5,
    )
    pid = store.record_proposal(rec)
    assert pid > 0

    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0].sender_email == "news@example.com"
    assert pending[0].id == pid
    assert pending[0].decision is None


def test_decision_removes_from_pending(store: Storage):
    pid = store.record_proposal(ProposalRecord(
        sender_email="a@x.com",
        list_id=None,
        subject_sample="hello",
        proposed_label="X",
        proposed_query="from:a@x.com",
        recent_count=1,
    ))
    store.record_decision(pid, "applied")
    assert store.get_pending() == []


def test_was_dismissed_for_sender_only_for_dismissals(store: Storage):
    # Apply on one sender — should NOT count as dismissed
    pid_a = store.record_proposal(ProposalRecord(
        sender_email="apply@x.com", list_id=None,
        subject_sample="s", proposed_label="L",
        proposed_query="from:apply@x.com", recent_count=1,
    ))
    store.record_decision(pid_a, "applied")
    assert store.was_dismissed_for_sender("apply@x.com") is False

    # Skip on second sender — counts as dismissed
    pid_b = store.record_proposal(ProposalRecord(
        sender_email="skip@x.com", list_id=None,
        subject_sample="s", proposed_label="L",
        proposed_query="from:skip@x.com", recent_count=1,
    ))
    store.record_decision(pid_b, "skipped")
    assert store.was_dismissed_for_sender("skip@x.com") is True

    # Never on third sender — also dismissed
    pid_c = store.record_proposal(ProposalRecord(
        sender_email="never@x.com", list_id=None,
        subject_sample="s", proposed_label="L",
        proposed_query="from:never@x.com", recent_count=1,
    ))
    store.record_decision(pid_c, "never")
    assert store.was_dismissed_for_sender("never@x.com") is True


def test_dismissed_check_is_case_insensitive(store: Storage):
    pid = store.record_proposal(ProposalRecord(
        sender_email="Mixed@Case.COM", list_id=None,
        subject_sample="s", proposed_label="L",
        proposed_query="from:Mixed@Case.COM", recent_count=1,
    ))
    store.record_decision(pid, "skipped")
    assert store.was_dismissed_for_sender("mixed@case.com") is True
    assert store.was_dismissed_for_sender("MIXED@CASE.COM") is True


def test_pending_dedup_by_sender(store: Storage):
    """If a pending proposal already exists for a sender, don't double-record."""
    rec = ProposalRecord(
        sender_email="dup@x.com", list_id=None,
        subject_sample="s1", proposed_label="L",
        proposed_query="from:dup@x.com", recent_count=2,
    )
    p1 = store.record_proposal(rec)
    rec2 = ProposalRecord(
        sender_email="dup@x.com", list_id=None,
        subject_sample="s2", proposed_label="L2",
        proposed_query="from:dup@x.com", recent_count=3,
    )
    p2 = store.record_proposal(rec2)
    # Either same row updated or no new pending row added
    pending = store.get_pending()
    assert len(pending) == 1
    # Newer subject_sample / count should win
    assert pending[0].recent_count == 3
    assert p2 == p1 or p2 > 0  # implementation choice — either reuse id or new id


def test_scan_state_roundtrip(store: Storage):
    assert store.get_last_scan_at() is None
    store.set_last_scan_at("2026-05-09T12:00:00Z")
    assert store.get_last_scan_at() == "2026-05-09T12:00:00Z"
    store.set_last_scan_at("2026-05-10T08:00:00Z")
    assert store.get_last_scan_at() == "2026-05-10T08:00:00Z"


def test_get_pending_returns_in_recency_order(store: Storage):
    """Highest recent_count first, ties broken by most-recent insertion."""
    p1 = store.record_proposal(ProposalRecord(
        sender_email="a@x.com", list_id=None,
        subject_sample="s", proposed_label="A",
        proposed_query="from:a@x.com", recent_count=2,
    ))
    p2 = store.record_proposal(ProposalRecord(
        sender_email="b@x.com", list_id=None,
        subject_sample="s", proposed_label="B",
        proposed_query="from:b@x.com", recent_count=10,
    ))
    p3 = store.record_proposal(ProposalRecord(
        sender_email="c@x.com", list_id=None,
        subject_sample="s", proposed_label="C",
        proposed_query="from:c@x.com", recent_count=5,
    ))
    pending = store.get_pending()
    assert [p.sender_email for p in pending] == ["b@x.com", "c@x.com", "a@x.com"]


def test_apply_log_record_and_get(store: Storage):
    eid = store.record_apply(
        label_name="AI Newsletters/Sequoia", label_id="Label_1",
        filter_query="list:weekly.sequoiacap.com", filter_id="filter_abc",
        backprop_message_ids=["m1", "m2", "m3"],
        extra_add_label_ids=["INBOX"],
        extra_remove_label_ids=["UNREAD"],
        apply_scope="both",
        action_summary="Skip inbox · Mark as read",
    )
    assert eid > 0

    entries = store.get_apply_log()
    assert len(entries) == 1
    e = entries[0]
    assert e.id == eid
    assert e.label_name == "AI Newsletters/Sequoia"
    assert e.filter_id == "filter_abc"
    assert e.backprop_message_ids == ["m1", "m2", "m3"]
    assert e.extra_add_label_ids == ["INBOX"]
    assert e.undone_at is None


def test_apply_log_get_entry_by_id(store: Storage):
    eid = store.record_apply(
        label_name="X", label_id="L1",
        filter_query="from:a@x.com", filter_id="f1",
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    e = store.get_apply_entry(eid)
    assert e is not None and e.id == eid
    assert store.get_apply_entry(99999) is None


def test_mark_undone_flips_field(store: Storage):
    eid = store.record_apply(
        label_name="X", label_id="L1",
        filter_query="from:a@x.com", filter_id="f1",
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    assert store.get_apply_entry(eid).undone_at is None
    store.mark_undone(eid)
    assert store.get_apply_entry(eid).undone_at is not None


def test_apply_log_orders_newest_first(store: Storage):
    a = store.record_apply(
        label_name="A", label_id="L", filter_query="q", filter_id=None,
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    b = store.record_apply(
        label_name="B", label_id="L", filter_query="q", filter_id=None,
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    entries = store.get_apply_log()
    assert [e.id for e in entries] == [b, a]


def test_get_active_applied_filter_queries_excludes_undone(store: Storage):
    a = store.record_apply(
        label_name="A", label_id="L", filter_query="from:a@x.com", filter_id="f1",
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    b = store.record_apply(
        label_name="B", label_id="L", filter_query="list:foo.example.com", filter_id="f2",
        backprop_message_ids=[], extra_add_label_ids=[], extra_remove_label_ids=[],
        apply_scope="future_only",
    )
    store.mark_undone(a)
    queries = store.get_active_applied_filter_queries()
    assert "list:foo.example.com" in queries
    assert "from:a@x.com" not in queries


def test_mark_pending_applied_for_sender(store: Storage):
    p1 = store.record_proposal(ProposalRecord(
        sender_email="a@x.com", list_id=None,
        subject_sample="s", proposed_label="L", proposed_query="from:a@x.com",
        recent_count=1,
    ))
    p2 = store.record_proposal(ProposalRecord(
        sender_email="b@x.com", list_id=None,
        subject_sample="s", proposed_label="L", proposed_query="from:b@x.com",
        recent_count=1,
    ))
    n = store.mark_pending_applied_for_sender("a@x.com", None)
    assert n == 1
    pending_after = {p.sender_email for p in store.get_pending()}
    assert pending_after == {"b@x.com"}


def test_mark_pending_applied_by_list_id(store: Storage):
    p = store.record_proposal(ProposalRecord(
        sender_email="varies@x.com", list_id="weekly.foo.com",
        subject_sample="s", proposed_label="L",
        proposed_query="list:weekly.foo.com", recent_count=2,
    ))
    n = store.mark_pending_applied_for_sender("never-matches@x.com", "weekly.foo.com")
    assert n == 1
    assert store.get_pending() == []


def test_mark_pending_with_list_id_does_not_touch_other_lists_for_same_sender(store: Storage):
    """Bug #6 regression: applying to one list shouldn't sweep all
    pending rows for the same sender. Sender may have multiple newsletters."""
    p_a = store.record_proposal(ProposalRecord(
        sender_email="noreply@substack.com", list_id="newsletter-a.substack.com",
        subject_sample="s", proposed_label="L_A",
        proposed_query="list:newsletter-a.substack.com", recent_count=2,
    ))
    p_b = store.record_proposal(ProposalRecord(
        sender_email="noreply@substack.com", list_id="newsletter-b.substack.com",
        subject_sample="s", proposed_label="L_B",
        proposed_query="list:newsletter-b.substack.com", recent_count=2,
    ))
    n = store.mark_pending_applied_for_sender("noreply@substack.com", "newsletter-a.substack.com")
    assert n == 1
    pending = {p.list_id for p in store.get_pending()}
    assert pending == {"newsletter-b.substack.com"}


def test_mark_pending_sender_only_skips_rows_with_list_id(store: Storage):
    """Bug #6 corollary: sender-only apply must not touch rows that
    are bound to a list_id (those are list-scoped and need their own apply)."""
    p_list = store.record_proposal(ProposalRecord(
        sender_email="x@y.com", list_id="news.y.com",
        subject_sample="s", proposed_label="LX",
        proposed_query="list:news.y.com", recent_count=1,
    ))
    p_plain = store.record_proposal(ProposalRecord(
        sender_email="z@y.com", list_id=None,
        subject_sample="s", proposed_label="LZ",
        proposed_query="from:z@y.com", recent_count=1,
    ))
    # Sender x@y.com sender-only apply: should NOT touch the list-bound row
    # (list-bound rows are addressed by their own list_id apply)
    n = store.mark_pending_applied_for_sender("x@y.com", None)
    assert n == 0
    pending = {p.sender_email for p in store.get_pending()}
    assert pending == {"x@y.com", "z@y.com"}


def test_intel_cache_roundtrip(store: Storage):
    assert store.get_intel("k1") is None
    store.set_intel("k1", '{"label":"A","confidence":0.9}')
    assert store.get_intel("k1") == '{"label":"A","confidence":0.9}'
    # overwrite
    store.set_intel("k1", '{"label":"B"}')
    assert store.get_intel("k1") == '{"label":"B"}'
    # clear
    store.clear_intel("k1")
    assert store.get_intel("k1") is None


def test_fresh_db_marks_v1_applied(tmp_path: Path):
    """A new DB ends up with schema_migrations populated for v1."""
    db = tmp_path / "fresh.db"
    Storage(db)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert rows[0][0] == 1
    assert "initial" in rows[0][1].lower()


def test_legacy_db_backfilled_to_v1(tmp_path: Path):
    """A DB created by the pre-runner code (proposal_log present, no
    schema_migrations) gets back-filled to v1 with no data loss."""
    db = tmp_path / "legacy.db"
    # Simulate the OLD pre-runner schema: tables without schema_migrations
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE proposal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_email TEXT NOT NULL,
                list_id TEXT,
                subject_sample TEXT,
                proposed_label TEXT NOT NULL,
                proposed_query TEXT NOT NULL,
                recent_count INTEGER NOT NULL DEFAULT 0,
                decision TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                body_preview TEXT,
                suggested_actions_json TEXT
            );
            CREATE TABLE scan_state (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.execute(
            "INSERT INTO proposal_log "
            "(sender_email, proposed_label, proposed_query, recent_count, created_at) "
            "VALUES ('legacy@x.com', 'L', 'q', 1, '2026-01-01T00:00:00Z')"
        )

    # Open through Storage — should back-fill, not nuke existing rows
    store = Storage(db)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0].sender_email == "legacy@x.com"

    with sqlite3.connect(db) as conn:
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
    assert versions == [1]


def test_really_old_legacy_db_gets_columns_added(tmp_path: Path):
    """A DB even older than the body_preview ALTER (ie. before that code
    shipped) should have those columns added during back-fill."""
    db = tmp_path / "really_old.db"
    with sqlite3.connect(db) as conn:
        # No body_preview / suggested_actions_json columns
        conn.executescript("""
            CREATE TABLE proposal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_email TEXT NOT NULL,
                list_id TEXT,
                subject_sample TEXT,
                proposed_label TEXT NOT NULL,
                proposed_query TEXT NOT NULL,
                recent_count INTEGER NOT NULL DEFAULT 0,
                decision TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
        """)

    Storage(db)  # should add the missing columns
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(proposal_log)")}
    assert "body_preview" in cols
    assert "suggested_actions_json" in cols


def test_migrations_idempotent_on_reinit(tmp_path: Path):
    """Re-opening an already-migrated DB doesn't re-run migrations."""
    db = tmp_path / "reinit.db"
    Storage(db)
    with sqlite3.connect(db) as conn:
        first = conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1"
        ).fetchone()[0]

    Storage(db)  # second init
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == first  # not re-applied (timestamp unchanged)


def test_new_migration_runs_on_next_init(tmp_path: Path, monkeypatch):
    """Adding a migration after a DB is at v1 runs only the new one."""
    from quicklabel import storage as storage_mod
    db = tmp_path / "v2.db"
    Storage(db)  # v1

    extra = (2, "test v2 — add a fake table",
             "CREATE TABLE IF NOT EXISTS fake_v2 (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(storage_mod, "_MIGRATIONS",
                        storage_mod._MIGRATIONS + [extra])

    Storage(db)  # should run only v2
    with sqlite3.connect(db) as conn:
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert versions == [1, 2]
    assert "fake_v2" in tables


def test_get_pending_excludes_dismissed_and_applied(store: Storage):
    p_app = store.record_proposal(ProposalRecord(
        sender_email="app@x.com", list_id=None,
        subject_sample="s", proposed_label="L",
        proposed_query="q", recent_count=1,
    ))
    p_skip = store.record_proposal(ProposalRecord(
        sender_email="skip@x.com", list_id=None,
        subject_sample="s", proposed_label="L",
        proposed_query="q", recent_count=1,
    ))
    p_pending = store.record_proposal(ProposalRecord(
        sender_email="pend@x.com", list_id=None,
        subject_sample="s", proposed_label="L",
        proposed_query="q", recent_count=1,
    ))
    store.record_decision(p_app, "applied")
    store.record_decision(p_skip, "skipped")
    pending = store.get_pending()
    assert [p.sender_email for p in pending] == ["pend@x.com"]
