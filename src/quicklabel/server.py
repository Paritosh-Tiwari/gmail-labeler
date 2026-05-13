"""FastAPI server for QuickLabel.

Binds to 127.0.0.1 only. POST /apply requires a per-server-startup nonce
that's embedded in the proposal page, to prevent other local processes
from blindly issuing label-apply requests.
"""
from __future__ import annotations

import json
import secrets
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lib.config import CREDENTIALS_PATH
from lib.gmail_client import list_filters, list_labels, search_count

from .actions import ActionChoices, describe, to_label_mutations
from .auth import (
    SetupState,
    authorize_interactive,
    build_service,
    get_credentials,
    setup_state,
)
from .token_store import delete_token
from .body import extract_body
from .headers import fingerprint
from .intelligence import intelligent_propose, to_proposal as ip_to_proposal
from .middleware import (
    AuditLogMiddleware,
    CSPHeaderMiddleware,
    HostValidationMiddleware,
    SetupRedirectMiddleware,
)
from .preflight import find_filter_conflicts, is_destructive, should_confirm
from .proposal import build_proposal
from .resolve import resolve_to_message
from .scan import scan_recent
from .sender_stats import compute_sender_stats
from .service import backprop_label, create_label_filter, ensure_label, reverse_backprop
from .settings import HOST, PORT, _SETTINGS_PATH, Settings, load_settings, save_settings
from .signals import extract_signals
from .storage import Storage


_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "quicklabel.db"

app = FastAPI(title="QuickLabel", version="0.1.0")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# Per-startup nonce. Anyone POSTing to /apply must echo this value back.
# Embedded in the proposal page; not sent over the network.
NONCE = secrets.token_urlsafe(32)

_creds_cache = None
_service_cache = None
_storage_cache: Storage | None = None


def get_service():
    """Lazy-build Gmail service. OAuth happens on first call."""
    global _creds_cache, _service_cache
    if _service_cache is None:
        _creds_cache = get_credentials()
        _service_cache = build_service(_creds_cache)
    return _service_cache


def get_storage() -> Storage:
    global _storage_cache
    if _storage_cache is None:
        _storage_cache = Storage(_DB_PATH)
    return _storage_cache


# Middleware ordering: app.add_middleware adds OUTER first. We want the
# CSP header on every response (outermost), audit logging to capture
# everything including blocked requests, setup redirect to send
# unfinished installs to the wizard, and host validation to reject
# DNS-rebinding attempts before any of the above. So we add them
# inner-first (each `add_middleware` wraps an OUTER layer around the
# previous).
app.add_middleware(HostValidationMiddleware, port=PORT)
app.add_middleware(SetupRedirectMiddleware,
                   is_ready_fn=lambda: setup_state() == SetupState.READY)
app.add_middleware(AuditLogMiddleware, get_storage_fn=get_storage)
app.add_middleware(CSPHeaderMiddleware)


_user_labels_cache: tuple[float, list[str]] | None = None
_USER_LABELS_TTL_SEC = 300  # 5 min


def get_user_label_paths(svc) -> list[str]:
    """Return the user's non-system label names (paths like 'Parent/Child').

    Cached for 5 minutes so the LLM-prompt build doesn't pay a list_labels
    Gmail call on every /label request.
    """
    global _user_labels_cache
    import time
    now = time.time()
    if _user_labels_cache and now - _user_labels_cache[0] < _USER_LABELS_TTL_SEC:
        return _user_labels_cache[1]

    labels = list_labels(svc)
    user_paths = [
        lbl["name"] for lbl in labels
        if lbl.get("type") == "user"
        and not lbl["name"].startswith("CATEGORY_")
    ]
    user_paths.sort()
    _user_labels_cache = (now, user_paths)
    return user_paths


def label_status(path: str, existing: set[str]) -> str:
    """Return 'existing' | 'extends_existing' | 'new'.

    'existing'         - exact path match
    'extends_existing' - any ancestor path matches (e.g. 'Finance' exists,
                         and the proposal is 'Finance/Citi/Alerts')
    'new'              - neither
    """
    if not path:
        return "new"
    if path in existing:
        return "existing"
    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        if "/".join(parts[:i]) in existing:
            return "extends_existing"
    return "new"


def build_existing_filter_labels_map(svc) -> dict:
    """Map sender-email and list-id (lowercased) -> list of label NAMES that
    existing filters apply. Used by the queue UI to surface 'currently
    auto-labeled as: X' so the user knows what they're about to overwrite.
    """
    from lib.gmail_client import list_filters
    filters = list_filters(svc)
    labels = list_labels(svc)
    id_to_name = {l["id"]: l["name"] for l in labels if l.get("type") == "user"}

    out = {"emails": {}, "lists": {}}
    for f in filters:
        crit = f.get("criteria") or {}
        action = f.get("action") or {}
        add_ids = action.get("addLabelIds") or []
        # Only user labels — system labels (INBOX, IMPORTANT, etc.) and
        # CATEGORY_* aren't useful to surface here
        names = [id_to_name[i] for i in add_ids if i in id_to_name]
        if not names:
            continue

        keys_email: list[str] = []
        keys_list: list[str] = []
        if crit.get("from"):
            keys_email.append(crit["from"].lower().strip())
        if crit.get("query"):
            for tok in crit["query"].split():
                low = tok.lower().strip().strip('"')
                if low.startswith("from:"):
                    keys_email.append(low[len("from:"):])
                elif low.startswith("list:"):
                    keys_list.append(low[len("list:"):])

        for k in keys_email:
            out["emails"].setdefault(k, []).extend(names)
        for k in keys_list:
            out["lists"].setdefault(k, []).extend(names)
    return out


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    from .bookmarklet import bookmarklet_url
    return templates.TemplateResponse(
        request, "landing.html",
        {"host": HOST, "port": PORT, "bookmarklet": bookmarklet_url()},
    )


@app.get("/label", response_class=HTMLResponse)
def label_page(request: Request, id: str = Query(..., min_length=4)):
    """Render the proposal page for a Gmail thread/message ID.

    Uses the LLM-backed intelligent proposer; falls back to the heuristic
    when Ollama is unreachable or returns garbage (handled inside
    intelligent_propose, surfaced via low confidence).
    """
    svc = get_service()
    try:
        msg = resolve_to_message(svc, id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not load email {id!r}: {e}")

    email = fingerprint(msg)
    if not email.sender_email:
        raise HTTPException(status_code=400, detail="Email has no From header.")

    stats = compute_sender_stats(svc, email)
    body = extract_body(msg)
    signals = extract_signals(email, body)
    user_labels = get_user_label_paths(svc)

    ip = intelligent_propose(
        email=email, body=body, signals=signals,
        sender_stats=stats, existing_labels=user_labels,
        storage=get_storage(),
    )
    proposal = ip_to_proposal(email, stats, ip)

    status = label_status(ip.chosen_label, set(user_labels))

    # Show enough body in the hero that the user can identify the email
    # without flipping back to Gmail. ~400 chars is roughly 2-3 lines.
    body_preview = (body or "").strip()[:400]

    return templates.TemplateResponse(
        request,
        "proposal.html",
        {
            "email": email,
            "stats": stats,
            "proposal": proposal,
            "questions": proposal.questions,
            "nonce": NONCE,
            "raw_id": id,
            "intel": ip,
            "signals": signals,
            "suggested_actions": ip.suggested_actions or ActionChoices(),
            "label_status_value": status,
            "body_preview": body_preview,
        },
    )


def _maybe_delete_conflicting_filters(svc, criteria: dict, replace: bool) -> list[str]:
    """If `replace` is true, delete any existing filters whose criteria
    overlap with `criteria`. Returns the list of deleted filter IDs."""
    if not replace:
        return []
    from lib.gmail_client import delete_filter
    conflicts = find_filter_conflicts(criteria, list_filters(svc))
    deleted: list[str] = []
    for c in conflicts:
        try:
            delete_filter(svc, c["id"])
            deleted.append(c["id"])
        except Exception:
            pass
    return deleted


def _maybe_render_confirm(
    request: Request,
    *,
    resubmit_action: str,
    form_fields: dict,
    label_name: str,
    filter_query: str,
    criteria: dict,
    scope: str,
    choices: ActionChoices,
    confirmed: str,
):
    """If preflight says confirmation is needed, render confirm.html and
    return the response. Otherwise return None and the caller proceeds.
    Treats `confirmed='yes'` as the user already confirming."""
    if confirmed == "yes":
        return None

    svc = get_service()
    conflicts: list[dict] = []
    backprop_count = 0
    if scope in ("future_only", "both"):
        conflicts = find_filter_conflicts(criteria, list_filters(svc))
    if scope in ("existing_only", "both"):
        # cap at 1000 — exact count beyond that doesn't change the warning
        backprop_count = search_count(svc, filter_query.strip(), cap=1000)

    if not should_confirm(scope, choices, backprop_count, conflicts):
        return None

    return templates.TemplateResponse(
        request, "confirm.html",
        {
            "resubmit_action": resubmit_action,
            "form_fields": form_fields,
            "label_name": label_name,
            "filter_query": filter_query,
            "scope": scope,
            "action_summary": describe(choices),
            "choices": choices,
            "conflicts": conflicts,
            "backprop_count": backprop_count,
            "is_destructive_flag": is_destructive(choices),
        },
    )


@app.post("/apply", response_class=HTMLResponse)
def apply(
    request: Request,
    nonce: str = Form(...),
    label_name: str = Form(...),
    parent_label: str = Form(""),
    filter_query: str = Form(...),
    apply_scope: str = Form("both"),
    inbox_action: str = Form("keep"),
    importance_action: str = Form("default"),
    categorize_action: str = Form("none"),
    mark_read: str = Form(""),
    star: str = Form(""),
    never_spam: str = Form(""),
    confirmed: str = Form(""),
    replace_conflicts: str = Form("no"),
):
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    if apply_scope not in ("future_only", "existing_only", "both"):
        raise HTTPException(status_code=400, detail=f"invalid apply_scope: {apply_scope!r}")

    svc = get_service()

    # Build the full label name (with parent if provided)
    full_name = f"{parent_label.strip()}/{label_name.strip()}" if parent_label.strip() else label_name.strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="Label name required.")

    # Action choices
    choices = ActionChoices.from_form(
        inbox_action=inbox_action,
        importance_action=importance_action,
        categorize_action=categorize_action,
        mark_read=mark_read,
        star=star,
        never_spam=never_spam,
    )
    # Always derive criteria from the user's editable filter_query so user
    # edits are honored. (Previous design had hidden filter_from /
    # filter_subject inputs that didn't sync with the textbox — bug.)
    criteria = _criteria_from_query(filter_query)

    # Preflight: maybe interrupt with the confirm page
    confirm_resp = _maybe_render_confirm(
        request,
        resubmit_action="/apply",
        form_fields={
            "nonce": nonce, "label_name": label_name, "parent_label": parent_label,
            "filter_query": filter_query, "apply_scope": apply_scope,
            "inbox_action": inbox_action, "importance_action": importance_action,
            "categorize_action": categorize_action,
            "mark_read": mark_read, "star": star, "never_spam": never_spam,
        },
        label_name=full_name, filter_query=filter_query, criteria=criteria,
        scope=apply_scope, choices=choices, confirmed=confirmed,
    )
    if confirm_resp is not None:
        return confirm_resp

    # Replace existing conflicting filters if user opted in
    _maybe_delete_conflicting_filters(svc, criteria, replace_conflicts == "yes")

    # Ensure the label exists (and parent)
    label_id = ensure_label(svc, full_name)

    extra_add, extra_remove = to_label_mutations(choices)
    action_summary = describe(choices)

    do_filter = apply_scope in ("future_only", "both")
    do_backprop = apply_scope in ("existing_only", "both")

    filter_created = None
    if do_filter:
        try:
            filter_created = create_label_filter(
                svc, criteria, label_id,
                extra_add_label_ids=extra_add,
                extra_remove_label_ids=extra_remove,
            )
        except Exception as e:
            filter_created = {"error": str(e)}

    backprop_ids: list[str] = []
    if do_backprop:
        backprop_ids = backprop_label(
            svc, filter_query, label_id,
            extra_add_label_ids=extra_add,
            extra_remove_label_ids=extra_remove,
        )

    # Record for the apply log / undo page
    filter_id = filter_created.get("id") if (filter_created and not filter_created.get("error")) else None
    storage = get_storage()
    apply_id = storage.record_apply(
        label_name=full_name, label_id=label_id,
        filter_query=filter_query, filter_id=filter_id,
        backprop_message_ids=backprop_ids,
        extra_add_label_ids=extra_add, extra_remove_label_ids=extra_remove,
        apply_scope=apply_scope, action_summary=" · ".join(action_summary),
    )

    # Clean up any queue rows that match this same sender or list-id.
    # Pull both out of the parsed criteria so we don't have to re-tokenize
    # filter_query — and so the same logic works whether the user typed
    # `from:` directly or via a `list:` that includes a `from:` token.
    from .preflight import _extract_from_and_list
    sender_for_match, list_id_for_match = _extract_from_and_list(criteria)
    cleaned = storage.mark_pending_applied_for_sender(sender_for_match, list_id_for_match or None)

    return templates.TemplateResponse(
        request,
        "applied.html",
        {
            "label_name": full_name,
            "filter_query": filter_query,
            "filter_created": filter_created,
            "backprop_count": len(backprop_ids),
            "action_summary": action_summary,
            "apply_scope": apply_scope,
            "apply_id": apply_id,
            "queue_rows_cleaned": cleaned,
        },
    )


@app.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    applied: str = Query(""),
    count: str = Query(""),
    apply_id: str = Query(""),
):
    """Show pending proposals from past scans (does not trigger a new scan).

    Optional query params for the post-apply success banner:
      applied: label name just applied
      count: number of existing emails affected
      apply_id: id of the apply_log row (for an Undo link)
    """
    storage = get_storage()
    pending = storage.get_pending()
    last_scan_at = storage.get_last_scan_at()

    # Annotate each pending row with a label-status flag and the labels
    # any existing filter currently applies for that sender.
    statuses: dict[int, str] = {}
    current_labels_by_id: dict[int, list[str]] = {}
    if pending:
        try:
            svc = get_service()
            existing = set(get_user_label_paths(svc))
            for p in pending:
                statuses[p.id] = label_status(p.proposed_label, existing)

            filter_labels = build_existing_filter_labels_map(svc)
            for p in pending:
                names: list[str] = []
                if p.list_id and p.list_id.lower() in filter_labels["lists"]:
                    names.extend(filter_labels["lists"][p.list_id.lower()])
                if p.sender_email and p.sender_email.lower() in filter_labels["emails"]:
                    names.extend(filter_labels["emails"][p.sender_email.lower()])
                # de-dupe while preserving order
                seen: set[str] = set()
                deduped = [n for n in names if not (n in seen or seen.add(n))]
                if deduped:
                    current_labels_by_id[p.id] = deduped
        except Exception:
            # If Gmail unreachable / not authed yet, skip annotation
            pass

    # Parse the per-row suggested_actions_json into ActionChoices so the
    # actions partial can render its radios/checkboxes pre-filled.
    suggested_by_id: dict[int, ActionChoices] = {}
    for p in pending:
        if p.suggested_actions_json:
            try:
                suggested_by_id[p.id] = ActionChoices(**json.loads(p.suggested_actions_json))
            except Exception:
                pass

    return templates.TemplateResponse(
        request, "queue.html",
        {
            "pending": pending,
            "last_scan_at": last_scan_at,
            "nonce": NONCE,
            "label_statuses": statuses,
            "suggested_by_id": suggested_by_id,
            "current_labels_by_id": current_labels_by_id,
            "flash_applied": applied,
            "flash_count": count,
            "flash_apply_id": apply_id,
        },
    )


@app.post("/queue/refresh", response_class=HTMLResponse)
def queue_refresh(
    request: Request,
    nonce: str = Form(...),
    hours: int = Form(24),
):
    """Trigger a new scan, then redirect to /queue."""
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be 1..720")
    svc = get_service()
    storage = get_storage()
    user_labels = get_user_label_paths(svc)
    scan_recent(svc, storage, hours=hours, existing_labels=user_labels)
    # Redirect (don't call queue_page directly — that bypasses FastAPI's
    # Query() resolution and the flash params get printed as FieldInfo
    # repr instead of empty strings).
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/queue/decide", response_class=HTMLResponse)
def queue_decide(
    request: Request,
    nonce: str = Form(...),
    proposal_id: int = Form(...),
    action: str = Form(...),
    label_name: str = Form(""),
    filter_query: str = Form(""),
    apply_scope: str = Form("both"),
    inbox_action: str = Form("keep"),
    importance_action: str = Form("default"),
    categorize_action: str = Form("none"),
    mark_read: str = Form(""),
    star: str = Form(""),
    never_spam: str = Form(""),
    confirmed: str = Form(""),
    replace_conflicts: str = Form("no"),
):
    """Apply / Skip / Never on one queue row."""
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    if action not in ("apply", "skip", "never"):
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")

    storage = get_storage()

    if action in ("skip", "never"):
        decision = "skipped" if action == "skip" else "never"
        storage.record_decision(proposal_id, decision)
        return RedirectResponse(url="/queue", status_code=303)

    # action == "apply"
    if apply_scope not in ("future_only", "existing_only", "both"):
        raise HTTPException(status_code=400, detail=f"invalid apply_scope: {apply_scope!r}")

    name = (label_name or "").strip()
    query = (filter_query or "").strip()
    if not name or not query:
        raise HTTPException(status_code=400, detail="label_name and filter_query required for apply")

    choices = ActionChoices.from_form(
        inbox_action=inbox_action,
        importance_action=importance_action,
        categorize_action=categorize_action,
        mark_read=mark_read,
        star=star,
        never_spam=never_spam,
    )
    criteria = _criteria_from_query(query)

    # Preflight: maybe interrupt with the confirm page
    confirm_resp = _maybe_render_confirm(
        request,
        resubmit_action="/queue/decide",
        form_fields={
            "nonce": nonce, "proposal_id": str(proposal_id), "action": "apply",
            "label_name": name, "filter_query": query,
            "apply_scope": apply_scope,
            "inbox_action": inbox_action, "importance_action": importance_action,
            "categorize_action": categorize_action,
            "mark_read": mark_read, "star": star, "never_spam": never_spam,
        },
        label_name=name, filter_query=query, criteria=criteria,
        scope=apply_scope, choices=choices, confirmed=confirmed,
    )
    if confirm_resp is not None:
        return confirm_resp

    svc = get_service()
    _maybe_delete_conflicting_filters(svc, criteria, replace_conflicts == "yes")
    label_id = ensure_label(svc, name)
    extra_add, extra_remove = to_label_mutations(choices)
    action_summary = describe(choices)

    do_filter = apply_scope in ("future_only", "both")
    do_backprop = apply_scope in ("existing_only", "both")

    filter_created: dict | None = None
    if do_filter:
        try:
            filter_created = create_label_filter(
                svc, criteria, label_id,
                extra_add_label_ids=extra_add,
                extra_remove_label_ids=extra_remove,
            )
        except Exception as e:
            filter_created = {"error": str(e)}

    backprop_ids: list[str] = []
    if do_backprop:
        backprop_ids = backprop_label(
            svc, query, label_id,
            extra_add_label_ids=extra_add,
            extra_remove_label_ids=extra_remove,
        )

    storage.record_decision(proposal_id, "applied")

    filter_id = filter_created.get("id") if (filter_created and not filter_created.get("error")) else None
    apply_id = storage.record_apply(
        label_name=name, label_id=label_id,
        filter_query=query, filter_id=filter_id,
        backprop_message_ids=backprop_ids,
        extra_add_label_ids=extra_add, extra_remove_label_ids=extra_remove,
        apply_scope=apply_scope, action_summary=" · ".join(action_summary),
    )

    # User came from the queue; send them right back so they can keep
    # triaging. /queue reads ?applied + ?count for a one-line success banner.
    params = urllib.parse.urlencode({
        "applied": name,
        "count": str(len(backprop_ids)),
        "apply_id": str(apply_id),
    })
    return RedirectResponse(url=f"/queue?{params}", status_code=303)


def _criteria_from_query(query: str) -> dict:
    """Split a Gmail search query into Gmail filter criteria.

    Gmail filter `criteria` accepts independent `from`, `subject`, and a
    `query` field that's evaluated as a Gmail search. We split out
    `from:` and `subject:` into their dedicated fields and put any
    other tokens (e.g. `list:`, `has:attachment`, free text) into
    `query`. This means combinations like
    `from:a@b.com subject:"X" list:y.com`
    correctly become
    `{"from": "a@b.com", "subject": "X", "query": "list:y.com"}`
    instead of silently dropping the list constraint.
    """
    from_val: str | None = None
    subject_val: str | None = None
    other_tokens: list[str] = []

    import shlex
    try:
        tokens = shlex.split(query or "")
    except ValueError:
        tokens = (query or "").split()

    for tok in tokens:
        low = tok.lower()
        if low.startswith("from:"):
            from_val = tok[len("from:"):].strip('"')
        elif low.startswith("subject:"):
            subject_val = tok[len("subject:"):].strip('"')
        else:
            # list:, has:, in:, free text, etc. — preserve verbatim
            other_tokens.append(tok)

    crit: dict = {}
    if from_val:
        crit["from"] = from_val
    if subject_val:
        crit["subject"] = subject_val
    if other_tokens:
        crit["query"] = " ".join(other_tokens)
    if not crit:
        crit["query"] = (query or "").strip()
    return crit


@app.post("/label/regenerate")
def label_regenerate(
    request: Request,
    nonce: str = Form(...),
    id: str = Form(...),
):
    """Bust the intel cache for this email and redirect back to /label?id=.

    Useful when the LLM picked a bad label/filter on the per-email view —
    user can re-run without leaving the page or going back to the queue.
    """
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    svc = get_service()
    try:
        msg = resolve_to_message(svc, id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not load email {id!r}: {e}")
    email = fingerprint(msg)
    body = extract_body(msg)
    from .intelligence import _cache_key
    get_storage().clear_intel(_cache_key(email, body))
    return RedirectResponse(
        url=f"/label?id={urllib.parse.quote(id, safe='')}",
        status_code=303,
    )


@app.post("/queue/regenerate")
def queue_regenerate(
    request: Request,
    nonce: str = Form(...),
    proposal_id: int = Form(...),
):
    """Re-propose one queue row using the LLM, then send the user to the
    full per-email proposal page so they can see the rationale + prompt
    + raw response (essential for debugging when the LLM picks badly)."""
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")

    storage = get_storage()
    pending = storage.get_pending()
    target = next((p for p in pending if p.id == proposal_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id} not pending")

    svc = get_service()
    if target.list_id:
        q = f"list:{target.list_id}"
    else:
        q = f"from:{target.sender_email}"
    from lib.gmail_client import search_message_ids
    ids = search_message_ids(svc, q, max_results=1)
    if not ids:
        raise HTTPException(status_code=404, detail="no messages found for this proposal")

    # Bust the cache so the LLM is actually re-called with the current
    # prompt/model. Then send the user to the full proposal view; that
    # page calls intelligent_propose itself and shows AI details.
    msg = svc.users().messages().get(userId="me", id=ids[0], format="full").execute()
    email = fingerprint(msg)
    body = extract_body(msg)
    from .intelligence import _cache_key
    storage.clear_intel(_cache_key(email, body))

    return RedirectResponse(
        url=f"/label?id={urllib.parse.quote(ids[0], safe='')}",
        status_code=303,
    )


@app.get("/proposal/{proposal_id}/full")
def proposal_full_view(proposal_id: int):
    """Find the latest message from this proposal's sender (or list) and
    302-redirect to the per-email LLM proposal view at /label?id=..."""
    storage = get_storage()
    target = next((p for p in storage.get_pending() if p.id == proposal_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id} not pending")
    svc = get_service()
    if target.list_id:
        q = f"list:{target.list_id}"
    else:
        q = f"from:{target.sender_email}"
    from lib.gmail_client import search_message_ids
    ids = search_message_ids(svc, q, max_results=1)
    if not ids:
        raise HTTPException(status_code=404, detail="no messages found for this proposal")
    return RedirectResponse(url=f"/label?id={ids[0]}", status_code=302)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    """Recent applies with one-click Undo."""
    storage = get_storage()
    entries = storage.get_apply_log(limit=100)
    return templates.TemplateResponse(
        request, "history.html",
        {"entries": entries, "nonce": NONCE},
    )


@app.post("/history/undo", response_class=HTMLResponse)
def history_undo(
    request: Request,
    nonce: str = Form(...),
    apply_id: int = Form(...),
):
    """Undo a previous apply: delete the filter, reverse the label changes
    on the message set we recorded at apply time."""
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    storage = get_storage()
    entry = storage.get_apply_entry(apply_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"apply id {apply_id} not found")
    if entry.undone_at:
        raise HTTPException(status_code=400, detail="already undone")

    svc = get_service()
    errors: list[str] = []

    # 1. Delete the filter (if one was created)
    if entry.filter_id:
        try:
            from lib.gmail_client import delete_filter
            delete_filter(svc, entry.filter_id)
        except Exception as e:
            errors.append(f"delete_filter failed: {e}")

    # 2. Reverse the label changes on the message set
    if entry.backprop_message_ids:
        # We previously added [label_id] + extra_add and removed extra_remove.
        added = [entry.label_id] + entry.extra_add_label_ids
        removed = entry.extra_remove_label_ids
        try:
            reverse_backprop(svc, entry.backprop_message_ids,
                             add_label_ids=added, remove_label_ids=removed)
        except Exception as e:
            errors.append(f"reverse_backprop failed: {e}")

    storage.mark_undone(apply_id)

    return templates.TemplateResponse(
        request, "undone.html",
        {"entry": entry, "errors": errors},
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_page(
    request: Request,
    error: str = Query(""),
):
    """One-time OAuth setup wizard. Always reachable; if QuickLabel is
    already set up it shows the 'all set' state.

    Note: `error` is rendered Jinja-escaped (no XSS) and is prefixed in
    the template with a fixed "Your last setup action failed:" label so
    an attacker who navigates the user to /setup?error=foo cannot fake
    a security-warning banner — only a misleading failure-reason string.
    """
    state = setup_state()
    return templates.TemplateResponse(
        request, "setup.html",
        {
            "state": state,
            "credentials_path": str(CREDENTIALS_PATH),
            "nonce": NONCE,
            "error": error,
        },
    )


def _setup_redirect_with_error(title: str, message: str) -> RedirectResponse:
    # `title` is no longer rendered as a separate bold heading (closed a
    # banner-spoofing surface); fold it into the message so internal
    # callers still get useful failure context.
    combined = f"{title}. {message}" if title else message
    qs = urllib.parse.urlencode({"error": combined})
    return RedirectResponse(url=f"/setup?{qs}", status_code=303)


@app.post("/setup/credentials")
async def setup_credentials(
    nonce: str = Form(...),
    file: UploadFile = File(...),
):
    """Receive an uploaded Google OAuth client JSON, validate it, save it
    to the project's credentials.json path."""
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")

    contents = await file.read()
    if len(contents) > 64_000:
        return _setup_redirect_with_error(
            "Upload too large",
            "OAuth client JSON files are usually <2 KB. The file you uploaded "
            "is unexpectedly large -- double-check it's the right file.",
        )

    try:
        data = json.loads(contents)
    except json.JSONDecodeError:
        return _setup_redirect_with_error(
            "Not valid JSON",
            "The uploaded file isn't valid JSON. Re-download from the Google "
            "Cloud Console Credentials page (the file is named "
            "client_secret_XXX.apps.googleusercontent.com.json).",
        )

    if not isinstance(data.get("installed"), dict):
        if isinstance(data.get("web"), dict):
            return _setup_redirect_with_error(
                "Wrong OAuth client type",
                "This is a 'Web application' client. QuickLabel needs a "
                "'Desktop app' client. In Cloud Console -> Credentials, click "
                "CREATE CREDENTIALS -> OAuth client ID, set Application type "
                "to 'Desktop app', then DOWNLOAD JSON and upload that file.",
            )
        return _setup_redirect_with_error(
            "Unrecognized credentials file",
            "The JSON doesn't look like an OAuth client configuration. It "
            "should have an \"installed\" object at the top level with "
            "client_id and client_secret inside.",
        )

    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_bytes(contents)
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/setup/authorize")
def setup_authorize(nonce: str = Form(...)):
    """Run the InstalledAppFlow OAuth dance. Blocks while user consents
    in their browser."""
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    if setup_state() == SetupState.NEEDS_CREDENTIALS:
        return _setup_redirect_with_error(
            "Credentials missing",
            "Upload your OAuth client JSON in step 1 first.",
        )

    try:
        authorize_interactive()
    except Exception as e:
        return _setup_redirect_with_error(
            "Authorization failed",
            f"OAuth flow did not complete: {e!s}. If you closed the consent "
            "window before approving, try again.",
        )

    # Reset cached service so the next get_service() picks up the new creds
    global _creds_cache, _service_cache
    _creds_cache = None
    _service_cache = None

    # Verify with a real Gmail call before claiming success
    try:
        svc = get_service()
        svc.users().getProfile(userId="me").execute()
    except Exception as e:
        return _setup_redirect_with_error(
            "Verification failed",
            f"Authorized successfully but the test Gmail call failed: {e!s}. "
            "Check that the Gmail API is enabled in your Cloud project.",
        )
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/setup/reset")
def setup_reset(nonce: str = Form(...)):
    """Drop the stored token (keeps credentials.json) so the user can
    re-authorize. Used when switching Google accounts or fixing a bad
    consent.

    POST-only with nonce: a GET-triggered reset was a CSRF vector — any
    link the user clicked that pointed here (or an <img src=...> in a
    rendered HTML email viewed in another tab) would silently delete
    their token.
    """
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    delete_token()
    global _creds_cache, _service_cache
    _creds_cache = None
    _service_cache = None
    return RedirectResponse(url="/setup", status_code=303)


def _settings_template_context(
    *,
    saved: bool = False,
    test_result=None,
    test_model_name: str = "",
) -> dict:
    """Build the template context for /settings, including the LLM
    install-status pill that's shown next to the configured model."""
    from .llm_status import installed_models

    s = load_settings()
    installed = installed_models()
    target = s.llm_model.strip().lower()
    match = next((m for m in installed if m.name.strip().lower() == target), None)

    if match is not None:
        model_status = "installed"
        current_size_gb = match.size_gb or None
    elif not installed:
        # Empty list means we couldn't reach Ollama (function swallows
        # exceptions). Show "Ollama down" rather than mis-claiming the
        # model isn't installed.
        model_status = "ollama_down"
        current_size_gb = None
    else:
        model_status = "not_installed"
        current_size_gb = None

    return {
        "settings": s,
        "settings_path": str(_SETTINGS_PATH),
        "saved": saved,
        "nonce": NONCE,
        "installed": installed,
        "model_status": model_status,
        "current_size_gb": current_size_gb,
        "test_result": test_result,
        "test_model_name": test_model_name,
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = Query("")):
    return templates.TemplateResponse(
        request, "settings.html",
        _settings_template_context(saved=saved == "1"),
    )


@app.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    nonce: str = Form(...),
    llm_model: str = Form(...),
    port: str = Form(...),
    log_level: str = Form(...),
):
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    try:
        port_int = int(port)
    except ValueError:
        raise HTTPException(status_code=400, detail="port must be an integer")
    if not (1 <= port_int <= 65535):
        raise HTTPException(status_code=400, detail="port must be 1..65535")
    log_level = log_level.strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise HTTPException(status_code=400, detail=f"invalid log_level: {log_level!r}")
    model = llm_model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="llm_model required")
    save_settings(Settings(port=port_int, llm_model=model, log_level=log_level))
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/test-llm", response_class=HTMLResponse)
def settings_test_llm(
    request: Request,
    nonce: str = Form(...),
    llm_model: str = Form(...),
    # Other fields are sent because formaction reuses the same form;
    # we accept them but ignore so a port-edited-but-not-saved value
    # doesn't block the test.
    port: str = Form(""),
    log_level: str = Form(""),
):
    if nonce != NONCE:
        raise HTTPException(status_code=403, detail="Invalid nonce.")
    from .llm_status import probe_model
    name = (llm_model or "").strip()
    result = probe_model(name)
    return templates.TemplateResponse(
        request, "settings.html",
        _settings_template_context(test_result=result, test_model_name=name),
    )


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    """Read-only view of the last N requests this server has handled.

    Useful for spotting weird traffic — a non-localhost Host header on
    any row means something tried to talk to QuickLabel from outside
    (most likely a DNS-rebinding attempt). Those requests will also
    have blocked=true since HostValidationMiddleware rejects them.
    """
    storage = get_storage()
    entries = storage.get_audit_log(limit=200)
    return templates.TemplateResponse(
        request, "audit.html",
        {"entries": entries},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


def main():
    import logging
    import uvicorn

    from ._logging import setup_logging
    s = load_settings()
    log_level = getattr(logging, s.log_level, logging.INFO)
    log_path = _DB_PATH.parent / "quicklabel.log"
    setup_logging(log_path, level=log_level)

    print(f"QuickLabel listening on http://{HOST}:{PORT}")
    print(f"Settings: {_SETTINGS_PATH}")
    print(f"Logs: {log_path}")
    print(f"Bookmarklet target: http://{HOST}:{PORT}/label?id=<gmail-thread-id>")
    uvicorn.run(app, host=HOST, port=PORT, log_level=s.log_level.lower())


if __name__ == "__main__":
    main()
