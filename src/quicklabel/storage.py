"""SQLite-backed memory of past proposals and scan state.

DB lives at `data/quicklabel.db` (gitignored). Tables:

  proposal_log
    id, sender_email, list_id, subject_sample, proposed_label,
    proposed_query, recent_count, decision, created_at, decided_at

  scan_state
    key TEXT PRIMARY KEY, value TEXT
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# --------------------------- migrations ---------------------------
#
# Versioned schema. Add new migrations as `(version, name, sql)` tuples
# at the END of `_MIGRATIONS`. Never edit a shipped migration — write a
# new one. The runner records applied versions in `schema_migrations`
# and skips already-applied ones on startup.
#
# v1 collapses everything QuickLabel shipped pre-productization. Legacy
# DBs (created before this runner existed) are detected by presence of
# `proposal_log` without `schema_migrations` and back-filled to v1.

_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS proposal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_email TEXT NOT NULL,
    list_id TEXT,
    subject_sample TEXT,
    proposed_label TEXT NOT NULL,
    proposed_query TEXT NOT NULL,
    recent_count INTEGER NOT NULL DEFAULT 0,
    decision TEXT,                 -- NULL = pending; 'applied' | 'skipped' | 'never'
    created_at TEXT NOT NULL,
    decided_at TEXT,
    body_preview TEXT,
    suggested_actions_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposal_log_sender_lower
    ON proposal_log (LOWER(sender_email));
CREATE INDEX IF NOT EXISTS idx_proposal_log_decision
    ON proposal_log (decision);

CREATE TABLE IF NOT EXISTS scan_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS intel_cache (
    cache_key TEXT PRIMARY KEY,
    proposal_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apply_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_at TEXT NOT NULL,
    label_name TEXT NOT NULL,
    label_id TEXT NOT NULL,
    filter_query TEXT NOT NULL,
    filter_id TEXT,
    backprop_message_ids TEXT,
    extra_add_label_ids TEXT,
    extra_remove_label_ids TEXT,
    apply_scope TEXT NOT NULL,
    action_summary TEXT,
    undone_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_apply_log_applied_at
    ON apply_log (applied_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    host TEXT,
    status INTEGER,
    origin TEXT,
    referer TEXT,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log (id DESC);
"""

_MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "initial schema", _MIGRATION_V1),
]


@dataclass
class ProposalRecord:
    sender_email: str
    list_id: str | None
    subject_sample: str
    proposed_label: str
    proposed_query: str
    recent_count: int
    body_preview: str = ""
    suggested_actions_json: str = ""


@dataclass
class AuditEntry:
    id: int
    ts: str
    method: str
    path: str
    host: str | None
    status: int | None
    origin: str | None
    referer: str | None
    blocked: bool


@dataclass
class ApplyLogEntry:
    id: int
    applied_at: str
    label_name: str
    label_id: str
    filter_query: str
    filter_id: str | None
    backprop_message_ids: list[str]
    extra_add_label_ids: list[str]
    extra_remove_label_ids: list[str]
    apply_scope: str
    action_summary: str
    undone_at: str | None


@dataclass
class PendingProposal:
    id: int
    sender_email: str
    list_id: str | None
    subject_sample: str
    proposed_label: str
    proposed_query: str
    recent_count: int
    decision: str | None
    created_at: str
    body_preview: str = ""
    suggested_actions_json: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Storage:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            self._run_migrations(conn)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version INTEGER PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  applied_at TEXT NOT NULL"
            ")"
        )
        applied = {row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations"
        )}

        # Legacy DB back-fill: a `proposal_log` table without any
        # schema_migrations rows means this DB was created by the
        # pre-runner code path. Run v1 (idempotent — all CREATE IF NOT
        # EXISTS), patch up the two ALTER-added columns if a *very* old
        # DB is missing them, and mark v1 as applied.
        if not applied:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "proposal_log" in tables:
                conn.executescript(_MIGRATION_V1)
                cols = {row[1] for row in conn.execute(
                    "PRAGMA table_info(proposal_log)"
                )}
                if "body_preview" not in cols:
                    conn.execute("ALTER TABLE proposal_log ADD COLUMN body_preview TEXT")
                if "suggested_actions_json" not in cols:
                    conn.execute(
                        "ALTER TABLE proposal_log ADD COLUMN suggested_actions_json TEXT"
                    )
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (1, "initial schema", _now_iso()),
                )
                applied.add(1)

        for version, name, sql in _MIGRATIONS:
            if version in applied:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (version, name, _now_iso()),
            )
            applied.add(version)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # --------------------------- proposal_log ---------------------------

    def record_proposal(self, rec: ProposalRecord) -> int:
        """Record a proposal as pending (decision=NULL).

        Dedup key: (sender_email, list_id) when list_id is present, else
        (sender_email, no-list-id). This way a sender with multiple
        newsletters keeps a separate row per newsletter, and a sender
        with no list still dedupes against itself.
        """
        with self._conn() as conn:
            if rec.list_id:
                existing = conn.execute(
                    "SELECT id FROM proposal_log "
                    "WHERE LOWER(sender_email) = LOWER(?) "
                    "  AND LOWER(list_id) = LOWER(?) "
                    "  AND decision IS NULL",
                    (rec.sender_email, rec.list_id),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id FROM proposal_log "
                    "WHERE LOWER(sender_email) = LOWER(?) "
                    "  AND (list_id IS NULL OR list_id = '') "
                    "  AND decision IS NULL",
                    (rec.sender_email,),
                ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE proposal_log SET "
                    "  list_id = ?, subject_sample = ?, "
                    "  proposed_label = ?, proposed_query = ?, recent_count = ?, "
                    "  body_preview = ?, suggested_actions_json = ? "
                    "WHERE id = ?",
                    (
                        rec.list_id, rec.subject_sample,
                        rec.proposed_label, rec.proposed_query, rec.recent_count,
                        rec.body_preview, rec.suggested_actions_json,
                        existing["id"],
                    ),
                )
                return int(existing["id"])

            cur = conn.execute(
                "INSERT INTO proposal_log "
                "(sender_email, list_id, subject_sample, proposed_label, "
                " proposed_query, recent_count, body_preview, "
                " suggested_actions_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.sender_email, rec.list_id, rec.subject_sample,
                    rec.proposed_label, rec.proposed_query, rec.recent_count,
                    rec.body_preview, rec.suggested_actions_json,
                    _now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def record_decision(self, proposal_id: int, decision: str) -> None:
        if decision not in ("applied", "skipped", "never"):
            raise ValueError(f"invalid decision: {decision!r}")
        with self._conn() as conn:
            conn.execute(
                "UPDATE proposal_log SET decision = ?, decided_at = ? WHERE id = ?",
                (decision, _now_iso(), proposal_id),
            )

    def was_dismissed_for_sender(self, sender_email: str) -> bool:
        """True if any past proposal for this sender was skipped or never'd."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM proposal_log "
                "WHERE LOWER(sender_email) = LOWER(?) "
                "  AND decision IN ('skipped', 'never') "
                "LIMIT 1",
                (sender_email,),
            ).fetchone()
            return row is not None

    def get_pending(self) -> list[PendingProposal]:
        """All pending proposals, ordered by recent_count desc, newest first on ties."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, sender_email, list_id, subject_sample, "
                "       proposed_label, proposed_query, recent_count, "
                "       decision, created_at, body_preview, "
                "       suggested_actions_json "
                "FROM proposal_log WHERE decision IS NULL "
                "ORDER BY recent_count DESC, id DESC"
            ).fetchall()
            return [
                PendingProposal(
                    id=r["id"],
                    sender_email=r["sender_email"],
                    list_id=r["list_id"],
                    subject_sample=r["subject_sample"],
                    proposed_label=r["proposed_label"],
                    proposed_query=r["proposed_query"],
                    recent_count=r["recent_count"],
                    decision=r["decision"],
                    created_at=r["created_at"],
                    body_preview=r["body_preview"] or "",
                    suggested_actions_json=r["suggested_actions_json"] or "",
                )
                for r in rows
            ]

    # --------------------------- scan_state ---------------------------

    def get_last_scan_at(self) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM scan_state WHERE key = 'last_scan_at'"
            ).fetchone()
            return row["value"] if row else None

    def set_last_scan_at(self, iso: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO scan_state (key, value) VALUES ('last_scan_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (iso,),
            )

    # --------------------------- intel_cache ---------------------------

    def get_intel(self, cache_key: str) -> str | None:
        """Return raw JSON string of a cached LLM proposal, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT proposal_json FROM intel_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            return row["proposal_json"] if row else None

    def set_intel(self, cache_key: str, proposal_json: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO intel_cache (cache_key, proposal_json, created_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "  proposal_json = excluded.proposal_json, "
                "  created_at = excluded.created_at",
                (cache_key, proposal_json, _now_iso()),
            )

    def clear_intel(self, cache_key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM intel_cache WHERE cache_key = ?", (cache_key,))

    # --------------------------- apply_log ---------------------------

    def record_apply(
        self,
        *,
        label_name: str, label_id: str,
        filter_query: str, filter_id: str | None,
        backprop_message_ids: list[str],
        extra_add_label_ids: list[str], extra_remove_label_ids: list[str],
        apply_scope: str, action_summary: str = "",
    ) -> int:
        import json
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO apply_log "
                "(applied_at, label_name, label_id, filter_query, filter_id, "
                " backprop_message_ids, extra_add_label_ids, extra_remove_label_ids, "
                " apply_scope, action_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(), label_name, label_id, filter_query, filter_id,
                    json.dumps(backprop_message_ids),
                    json.dumps(extra_add_label_ids),
                    json.dumps(extra_remove_label_ids),
                    apply_scope, action_summary,
                ),
            )
            return int(cur.lastrowid)

    def get_apply_log(self, limit: int = 50) -> list[ApplyLogEntry]:
        import json
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, applied_at, label_name, label_id, filter_query, "
                "       filter_id, backprop_message_ids, extra_add_label_ids, "
                "       extra_remove_label_ids, apply_scope, action_summary, "
                "       undone_at "
                "FROM apply_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out: list[ApplyLogEntry] = []
            for r in rows:
                out.append(ApplyLogEntry(
                    id=r["id"],
                    applied_at=r["applied_at"],
                    label_name=r["label_name"],
                    label_id=r["label_id"],
                    filter_query=r["filter_query"],
                    filter_id=r["filter_id"],
                    backprop_message_ids=json.loads(r["backprop_message_ids"] or "[]"),
                    extra_add_label_ids=json.loads(r["extra_add_label_ids"] or "[]"),
                    extra_remove_label_ids=json.loads(r["extra_remove_label_ids"] or "[]"),
                    apply_scope=r["apply_scope"],
                    action_summary=r["action_summary"] or "",
                    undone_at=r["undone_at"],
                ))
            return out

    def get_apply_entry(self, entry_id: int) -> ApplyLogEntry | None:
        for e in self.get_apply_log(limit=1000):
            if e.id == entry_id:
                return e
        return None

    def mark_undone(self, entry_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE apply_log SET undone_at = ? WHERE id = ?",
                (_now_iso(), entry_id),
            )

    # --------------------------- audit_log ---------------------------

    def record_audit(
        self,
        method: str,
        path: str,
        host: str | None,
        status: int | None,
        origin: str | None = None,
        referer: str | None = None,
        blocked: bool = False,
    ) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO audit_log "
                    "(ts, method, path, host, status, origin, referer, blocked) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_now_iso(), method, path, host, status, origin, referer,
                     1 if blocked else 0),
                )
        except Exception:
            # Audit logging must never break the request flow
            pass

    def get_audit_log(self, limit: int = 200) -> list[AuditEntry]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, ts, method, path, host, status, origin, referer, blocked "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                AuditEntry(
                    id=r["id"], ts=r["ts"], method=r["method"], path=r["path"],
                    host=r["host"], status=r["status"], origin=r["origin"],
                    referer=r["referer"], blocked=bool(r["blocked"]),
                )
                for r in rows
            ]

    def get_active_applied_filter_queries(self) -> list[str]:
        """All filter_query strings from apply_log entries that are still
        active (not undone). Used by scan to skip senders this tool already
        applied a label to."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT filter_query FROM apply_log WHERE undone_at IS NULL"
            ).fetchall()
            return [r["filter_query"] for r in rows if r["filter_query"]]

    def get_label_usage_counts(self) -> dict[str, int]:
        """Count how many times each label has been applied (and not undone)
        via QuickLabel. Surfaces 'active' user labels in the LLM prompt so
        the model leans toward reusing labels that are getting traction,
        not labels the user tried once and abandoned."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT label_name, COUNT(*) AS n FROM apply_log "
                "WHERE undone_at IS NULL "
                "GROUP BY label_name"
            ).fetchall()
            return {r["label_name"]: int(r["n"]) for r in rows if r["label_name"]}

    def mark_pending_applied_for_sender(self, sender_email: str, list_id: str | None) -> int:
        """Mark pending proposals that match this apply as applied. Returns
        the number of rows marked. Used to clean up the queue after a /apply
        from the per-email page (where there's no proposal_id directly).

        Matching rule (precise to avoid over-deleting):
          - If list_id given: only mark rows with the same list_id (a sender
            may have multiple newsletters; applying to one shouldn't clear
            the others).
          - If list_id is None: mark rows with the same sender AND no
            list_id of their own (rows that *do* have a list_id are bound
            to that list and shouldn't be touched by a sender-only apply).
        """
        with self._conn() as conn:
            if list_id:
                cur = conn.execute(
                    "UPDATE proposal_log SET decision = 'applied', decided_at = ? "
                    "WHERE decision IS NULL AND LOWER(list_id) = LOWER(?)",
                    (_now_iso(), list_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE proposal_log SET decision = 'applied', decided_at = ? "
                    "WHERE decision IS NULL "
                    "  AND LOWER(sender_email) = LOWER(?) "
                    "  AND (list_id IS NULL OR list_id = '')",
                    (_now_iso(), sender_email),
                )
            return cur.rowcount
