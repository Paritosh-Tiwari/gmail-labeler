"""Scan recent inbox mail and write proposals to local storage.

Cheap by design: metadata-only fetch (Gmail returns a `snippet` field for
free that we use as a body preview, no body download needed). Signals
that need the unsubscribe link rely on the `List-Unsubscribe` header so
we still don't pay for the full body.

For each candidate sender we run the smart-heuristic proposer, which
prefers an existing-label domain match and seeds sensible default
actions per signal pattern (heavy marketing -> skip + read + Promotions;
no-reply notification -> skip + read; transactional -> read; etc.). The
LLM is NOT called here — `Regenerate with AI` per row triggers that.

Senders that already have a filter are NOT excluded — the user wants to
review them too (existing labels may be wrong). The queue UI surfaces
the current filter's label so it's clear what's being changed.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from lib.gmail_client import search_message_ids

from .headers import EmailFingerprint, fingerprint
from .proposal import (
    SenderStats,
    propose_filter,
)
from .signals import extract_signals
from .smart_heuristic import suggest_actions_smart, suggest_label_smart
from .storage import PendingProposal, ProposalRecord, Storage


SCAN_CAP = 200             # max messages we fetch per scan
DEFAULT_HOURS = 24         # how far back to look on first scan
SNIPPET_PREVIEW_CHARS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_query(hours: int) -> str:
    if hours <= 24:
        return f"in:inbox newer_than:{hours}h"
    days = max(1, hours // 24)
    return f"in:inbox newer_than:{days}d"


def _covered_senders(filters: list[dict]) -> dict:
    """Extract sender emails and list-ids that existing filters already cover."""
    emails: set[str] = set()
    lists: set[str] = set()
    for f in filters:
        crit = f.get("criteria", {}) or {}
        if "from" in crit and crit["from"]:
            emails.add(crit["from"].lower().strip())
        if "query" in crit and crit["query"]:
            for tok in crit["query"].split():
                low = tok.lower().strip().strip('"')
                if low.startswith("from:"):
                    emails.add(low[len("from:"):])
                elif low.startswith("list:"):
                    lists.add(low[len("list:"):])
    return {"emails": emails, "lists": lists}


def _is_covered(fp: EmailFingerprint, covered: dict) -> bool:
    if fp.list_id and fp.list_id.lower() in covered["lists"]:
        return True
    if fp.sender_email and fp.sender_email.lower() in covered["emails"]:
        return True
    return False


def _own_applied_targets(storage: Storage) -> tuple[set[str], set[str]]:
    """Return (sender_emails, list_ids) this tool has applied to and not undone.
    Used to exclude already-handled rows from the next scan."""
    senders: set[str] = set()
    lists: set[str] = set()
    for q in storage.get_active_applied_filter_queries():
        for tok in (q or "").split():
            low = tok.lower().strip().strip('"')
            if low.startswith("from:"):
                senders.add(low[len("from:"):])
            elif low.startswith("list:"):
                lists.add(low[len("list:"):])
    return senders, lists


def scan_recent(
    service,
    storage: Storage,
    hours: int = DEFAULT_HOURS,
    cap: int = SCAN_CAP,
    existing_labels: list[str] | None = None,
) -> list[PendingProposal]:
    """Scan recent inbox mail, write new proposals, return current pending list.

    `existing_labels`: full label paths the user already has. Used by the
    smart heuristic to suggest reusing an existing label when the sender
    domain matches. Pass None to skip the domain-match step.
    """
    existing_labels = existing_labels or []

    query = _build_query(hours)
    msg_ids = search_message_ids(service, query, max_results=cap)

    # Fetch metadata + snippet for each. Snippet comes back automatically.
    fps: list[tuple[EmailFingerprint, str]] = []  # (fp, snippet)
    for mid in msg_ids:
        msg = service.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "List-ID", "List-Unsubscribe"],
        ).execute()
        fp = fingerprint(msg)
        if fp.sender_email:
            snippet = (msg.get("snippet") or "")[:SNIPPET_PREVIEW_CHARS]
            fps.append((fp, snippet))

    # Group by list-id (if present) else sender_email — newest first within each
    groups: dict[str, list[tuple[EmailFingerprint, str]]] = {}
    for fp, snip in fps:
        key = (fp.list_id or fp.sender_email).lower()
        groups.setdefault(key, []).append((fp, snip))

    # Senders / list-ids that THIS tool already labeled (and the user hasn't
    # undone). We exclude these from the queue — they're done. Pre-existing
    # filters (from Gmail before this tool touched anything) are NOT
    # excluded; the queue UI surfaces them so the user can override.
    own_senders, own_lists = _own_applied_targets(storage)

    for _key, group in groups.items():
        sample, sample_snippet = group[0]
        if storage.was_dismissed_for_sender(sample.sender_email):
            continue
        if sample.sender_email and sample.sender_email.lower() in own_senders:
            continue
        if sample.list_id and sample.list_id.lower() in own_lists:
            continue

        # Snippet is empty for many HTML-heavy emails (Citi alerts,
        # branded marketing). Fall back to a real body extraction so
        # the queue row has a useful preview. We only pay this cost
        # when needed and only for the sample message per group.
        if not (sample_snippet or "").strip():
            try:
                full = service.users().messages().get(
                    userId="me", id=sample.msg_id, format="full",
                ).execute()
                from .body import extract_body
                sample_snippet = extract_body(full, max_chars=200)
            except Exception:
                pass

        # Lightweight stats from the scan window only
        total_from_sender = sum(1 for f, _ in group if f.sender_email == sample.sender_email)
        total_from_list = (
            sum(1 for f, _ in group if f.list_id == sample.list_id)
            if sample.list_id else 0
        )
        stats = SenderStats(
            total_from_sender=total_from_sender,
            total_from_list=total_from_list,
            common_subject_prefix=None,
            prefix_match_count=0,
        )

        # Signals + smart label/action suggestions (no LLM)
        signals = extract_signals(sample, sample_snippet)
        fp_proposal = propose_filter(sample, stats)
        lp_proposal = suggest_label_smart(sample, signals, stats, existing_labels)
        suggested_actions = suggest_actions_smart(signals, stats)

        storage.record_proposal(ProposalRecord(
            sender_email=sample.sender_email,
            list_id=sample.list_id,
            subject_sample=sample.subject,
            proposed_label=lp_proposal.suggested_name,
            proposed_query=fp_proposal.query,
            recent_count=len(group),
            body_preview=sample_snippet,
            suggested_actions_json=json.dumps(asdict(suggested_actions)),
        ))

    storage.set_last_scan_at(_now_iso())
    return storage.get_pending()
