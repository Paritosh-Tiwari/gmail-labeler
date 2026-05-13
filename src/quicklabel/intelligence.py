"""LLM-backed intelligent proposer.

Calls a local Ollama model with rich context:
  - email metadata + signal flags
  - sender-history stats
  - body excerpt
  - the user's existing labels (so the LLM can place mail into existing
    buckets instead of inventing a new one every time)

Default model is qwen2.5:14b-instruct-q5_K_M; override with the
`QUICKLABEL_LLM_MODEL` environment variable (e.g. set it to
`gpt-oss:20b` for a larger model).

Returns an IntelligentProposal with a chosen label path, filter rule,
suggested actions, rationale, and a confidence score (0-1). The
to_proposal() adapter converts to the existing Proposal dataclass so
templates and downstream code keep working unchanged.

Failure modes (LLM unreachable, garbage output) fall back to the
deterministic heuristic with a low confidence so the UI can flag it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .actions import ActionChoices
from .headers import EmailFingerprint
from .proposal import (
    LabelProposal, FilterProposal, Proposal,
    SenderStats,
    propose_filter, propose_label,
)
from .signals import Signals
from .storage import Storage


MAX_LABELS_IN_PROMPT = 500
# Body excerpt strategy: head + tail rather than head-only. Newsletters and
# transactional emails often put distinguishing content (sender brand,
# unsubscribe context, footer signatures) at the bottom — a head-only
# excerpt misses those signals.
BODY_HEAD_CHARS = 1500
BODY_TAIL_CHARS = 800

from .settings import DEFAULT_LLM_MODEL, load_settings


def get_model_name() -> str:
    """Return the LLM model to use. Read on every call from settings.json
    (with QUICKLABEL_LLM_MODEL env override) so a /settings change is
    picked up by subsequent proposals without restart in the common case."""
    name = load_settings().llm_model.strip()
    return name or DEFAULT_LLM_MODEL


@dataclass
class IntelligentProposal:
    chosen_label: str
    is_new_label: bool
    rationale: str
    filter_query: str
    filter_criteria: dict
    suggested_actions: ActionChoices | None
    confidence: float
    cache_key: str
    raw_response: str = ""
    prompt: str = ""
    model: str = ""


# --------------------------- cache key ---------------------------

def _cache_key(email: EmailFingerprint, body: str) -> str:
    h = hashlib.sha256()
    h.update((email.sender_email or "").lower().encode("utf-8"))
    h.update(b"|")
    h.update((email.subject or "").encode("utf-8"))
    h.update(b"|")
    h.update((body or "")[:500].encode("utf-8"))
    return h.hexdigest()[:32]


# --------------------------- prompt ---------------------------

_SYSTEM_PROMPT = """You are an email organization assistant for Gmail. Your job has two parts:
  1. Choose where this email belongs in the user's existing label tree, or
     propose a new sub-label under an existing parent. Strongly prefer
     reusing existing labels over creating new ones.
  2. Design a Gmail filter rule that will catch similar FUTURE mail from
     the same source/category — not just this exact email.

KEY DECISION — does this sender use a STABLE subject pattern, or one-offs?
Before picking the filter type, judge whether THIS SENDER tends to send
mail with a recurring subject template or unique one-off subjects:

  Recurring-template indicators (favor 'from_subject'):
    - Subject reads like a template — contains words like 'Your', 'Order',
      'Receipt', 'Statement', 'Alert', 'Notification', 'Charged',
      'Shipped', 'Invoice', 'Confirmation'.
    - Sender history shows a strong shared subject prefix (count is given
      in the user prompt below).
    - Subject has a clear template + variable portion (numbers, dates,
      amounts, IDs, recipient names).
    Example: 'Your Citi card was charged $48.20 at WHOLE FOODS' — the
    prefix 'Your Citi card was charged' is the stable template.

  One-off subject indicators (favor 'from_keyword' or 'from'):
    - Subject reads like editorial / announcement content — 'The Future of
      X', 'Announcing Y', 'New course: Z starts April 1', 'Why we built'.
    - Each prior email from this sender has a distinct subject (low or
      zero shared-prefix count in the sender history).
    - The only stable signal is the sender + a distinctive keyword that
      appears in the body of similar emails.
    Example: 'New Maven course: AI for PMs starts April 1' — subject is
    one-off, but 'course' or 'Maven course' is in every announcement body.

FILTER TYPES (in preference order — pick the most specific that applies):
  1. list_id       Sender has a List-ID header. MOST precise. Always use
                   when available.
  2. from_subject  Sender + STABLE subject substring. Use for recurring
                   templates. Subject must NOT contain variable parts.
  3. from_keyword  Sender + a distinctive keyword/phrase that appears in
                   the BODY of similar emails (Gmail matches it anywhere
                   in the message). Use when subject is one-off but a
                   body keyword reliably distinguishes this category.
                   Multi-word phrases are fine and more precise. Avoid
                   generic words ('email', 'click', 'the', 'we').
  4. from          Sender alone. Use ONLY when neither subject nor body
                   has a stable distinguishing pattern AND the sender
                   exclusively sends mail of this single category.

FILTER DESIGN RULES:
- For from_subject: subject is substring-matched. NEVER include variable
  parts (amounts, dates, transaction IDs, order numbers, recipient names,
  edition numbers like '#234', times, account-last-4-digits).
- For from_keyword: pick a SPECIFIC distinctive keyword. 'invoice',
  'course', 'shipment', a product name, a platform name. Not
  conversational filler.
- Examples:

  (A) Recurring transactional — use from_subject:
      Subject 'Your Citi card was charged $123.45 at STARBUCKS on Mar 5'
      Sender history: 530 prior, 420 share prefix 'Your Citi card was charged'
        RIGHT:  type='from_subject', from='alerts@citi.com',
                subject='Your Citi card was charged'
        WRONG:  type='from'  (drops a strong recurring pattern)
        WRONG:  subject='Your Citi card was charged $123.45 ...' (variable parts)

  (B) One-off subject, distinctive body keyword — use from_keyword:
      Subject 'New Maven course: AI for PMs starts April 1'
      Sender history: 12 prior, no shared subject prefix
        RIGHT:  type='from_keyword', from='hello@maven.com',
                keyword='Maven course'
        WRONG:  type='from_subject', subject='New Maven course'
                (subject is one-off; future announcements may say
                'Just launched' or 'Now enrolling' instead)
        WRONG:  type='from'  (drops 'Maven course' which excludes the
                sender's other email types like billing or password resets)

  (C) Variable subject AND no recurring body keyword — use from:
      Subject 'Order #A8472 shipped — tracking 1Z999...'
      Sender history: 50 prior, no shared subject prefix
        RIGHT:  type='from', from='ship-confirm@amazon.com'

  (D) List-ID present — use list_id:
      Subject 'Weekly insights #234 — The AI infra wave'
      List-ID: weekly.sequoiacap.com
        RIGHT:  type='list_id', list_id='weekly.sequoiacap.com'

Always respond with a single JSON object and nothing else — no prose
before, no prose after, no markdown fences. Use English for all label
names and rationales.

WORKED EXAMPLES (input fragment → expected output):

EXAMPLE 1 — recurring transactional (from_subject):
Input:
  USER'S EXISTING LABELS:
  - Finance/Statements
  - Newsletters/Tech
  NEW EMAIL TO LABEL:
  From: Citi Alerts <alerts@citi.com>
  Subject: Your Citi card was charged $48.20 at WHOLE FOODS on May 12
  List-ID: (none)
  Sender history: 530 prior emails from this sender; 420 share the subject
  prefix 'Your Citi card was charged'

Output:
{
  "chosen_label": "Finance/Statements/Citi",
  "is_new_label": true,
  "rationale": "Recurring transactional alert from Citi; nests under Finance/Statements as a new sub-label. Subject prefix recurs in 420/530 prior emails so from_subject is safest.",
  "filter": {
    "type": "from_subject",
    "from": "alerts@citi.com",
    "subject": "Your Citi card was charged",
    "keyword": null,
    "list_id": null
  },
  "actions": {
    "inbox": "keep",
    "importance": "default",
    "categorize": "updates",
    "mark_read": true,
    "star": false,
    "never_spam": false
  },
  "confidence": 0.92
}

EXAMPLE 2 — one-off subject, distinctive body keyword (from_keyword):
Input:
  USER'S EXISTING LABELS:
  - Learning
  - Newsletters/Tech
  NEW EMAIL TO LABEL:
  From: Maven <hello@maven.com>
  Subject: AI for Product Managers — cohort starts April 1
  List-ID: (none)
  Sender history: 12 prior emails from this sender; no shared subject prefix

Output:
{
  "chosen_label": "Learning/Maven",
  "is_new_label": true,
  "rationale": "One-off course-announcement subject; sender mixes billing and course mail, so filter on sender + 'Maven course' keyword to capture only course announcements without catching unrelated mail.",
  "filter": {
    "type": "from_keyword",
    "from": "hello@maven.com",
    "subject": null,
    "keyword": "Maven course",
    "list_id": null
  },
  "actions": {
    "inbox": "keep",
    "importance": "default",
    "categorize": "none",
    "mark_read": false,
    "star": false,
    "never_spam": false
  },
  "confidence": 0.78
}
"""


def _body_excerpt(body: str) -> str:
    """Return body's head + tail. Whole body if it fits in head+tail."""
    body = body or ""
    if len(body) <= BODY_HEAD_CHARS + BODY_TAIL_CHARS:
        return body
    return (
        body[:BODY_HEAD_CHARS]
        + "\n\n... [middle truncated] ...\n\n"
        + body[-BODY_TAIL_CHARS:]
    )


def _format_labels_block(
    labels: list[str], usage_counts: dict[str, int] | None
) -> str:
    """Render the existing-labels list, ordering by usage count desc (so
    actively-used labels float to the top — anti-proliferation hint) and
    annotating each with `(N×)` if QuickLabel has applied it before.

    Labels with no usage history get listed alphabetically after the used
    ones.
    """
    if not labels:
        return "(none yet)"
    counts = usage_counts or {}
    used = sorted(
        (lbl for lbl in labels if lbl in counts),
        key=lambda l: (-counts[l], l.lower()),
    )
    unused = sorted(
        (lbl for lbl in labels if lbl not in counts),
        key=str.lower,
    )
    ordered = used + unused
    lines: list[str] = []
    for lbl in ordered:
        n = counts.get(lbl)
        if n:
            lines.append(f"- {lbl}  ({n}× applied via QuickLabel)")
        else:
            lines.append(f"- {lbl}")
    return "\n".join(lines)


def _criteria_to_gmail_query(criteria: dict) -> str:
    """Render a Gmail filter `criteria` dict back into a human-readable
    Gmail search query, for showing existing filters to the LLM."""
    parts: list[str] = []
    for key, val in (criteria or {}).items():
        if not val:
            continue
        if key == "from":
            parts.append(f"from:{val}")
        elif key == "to":
            parts.append(f"to:{val}")
        elif key == "subject":
            parts.append(f'subject:"{val}"')
        elif key == "query":
            parts.append(str(val))
        elif key == "negatedQuery":
            parts.append(f"-({val})")
        elif key == "hasAttachment":
            if val:
                parts.append("has:attachment")
    return " ".join(parts) if parts else "(empty criteria)"


def _format_existing_filters(filters: list[dict] | None, cap: int = 20) -> str:
    """Top N existing Gmail filters rendered as Gmail-search-style queries.
    The LLM uses these to avoid proposing a duplicate."""
    if not filters:
        return "(none)"
    queries: list[str] = []
    for f in filters[:cap]:
        q = _criteria_to_gmail_query(f.get("criteria") or {})
        if q and q != "(empty criteria)":
            queries.append(q)
    if not queries:
        return "(none)"
    return "\n".join(f"- {q}" for q in queries)


def _format_recent_applies(
    recent: list[dict] | None, cap: int = 5
) -> str:
    """Last N labels the user actually applied via QuickLabel, as
    few-shot examples reflecting THIS user's conventions (naming style,
    depth, filter shape preference). Beats generic worked examples."""
    if not recent:
        return "(no recent QuickLabel applies yet)"
    lines: list[str] = []
    for r in recent[:cap]:
        label = (r.get("label_name") or "").strip()
        fq = (r.get("filter_query") or "").strip()
        if not label:
            continue
        if fq:
            lines.append(f"- {label}    (filter: {fq})")
        else:
            lines.append(f"- {label}")
    return "\n".join(lines) if lines else "(no recent QuickLabel applies yet)"


def build_prompt(
    email: EmailFingerprint,
    body: str,
    signals: Signals,
    sender_stats: SenderStats,
    existing_labels: list[str],
    *,
    label_usage_counts: dict[str, int] | None = None,
    recent_applies: list[dict] | None = None,
    existing_filters: list[dict] | None = None,
) -> str:
    labels = existing_labels[:MAX_LABELS_IN_PROMPT]
    labels_block = _format_labels_block(labels, label_usage_counts)

    body_excerpt = _body_excerpt(body)

    signal_lines = "\n".join(f"- {s}" for s in signals.describe()) or "- (no notable signals)"

    sender_line = (
        f"{sender_stats.total_from_sender} prior emails from this sender"
        + (f"; {sender_stats.total_from_list} on this list" if sender_stats.total_from_list else "")
    )
    if sender_stats.common_subject_prefix:
        sender_line += (
            f"; {sender_stats.prefix_match_count} share the subject prefix "
            f"'{sender_stats.common_subject_prefix}'"
        )

    gmail_category_line = (
        f"Gmail's own category: {email.gmail_category}"
        if email.gmail_category
        else "Gmail's own category: (none assigned)"
    )

    recent_block = _format_recent_applies(recent_applies)
    filters_block = _format_existing_filters(existing_filters)

    return f"""USER'S EXISTING LABELS (prefer to place under one of these; counts show which are actively used):
{labels_block}

RECENT LABELS THIS USER APPLIED (style reference — match their naming conventions):
{recent_block}

EXISTING GMAIL FILTERS (if one of these already fits this email, propose the SAME filter — we'll attach your new label to it instead of creating a duplicate):
{filters_block}

NEW EMAIL TO LABEL:
From: {email.sender_name or ''} <{email.sender_email}>
Subject: {email.subject}
List-ID: {email.list_id or '(none)'}
{gmail_category_line}

Sender history: {sender_line}

Signals:
{signal_lines}

Body excerpt:
\"\"\"
{body_excerpt}
\"\"\"

TASK:
1. Pick the best label path for this email. Reuse an existing label
   verbatim if one fits, especially one with an apply count (proven
   in use). Otherwise propose a new sub-label under an existing parent
   ('Parent/NewLeaf'). Top-level new labels are a last resort — only
   when no existing parent fits. Match the user's naming style as seen
   in the RECENT LABELS section (case, separator depth, abbreviations).
2. Design a Gmail filter that catches FUTURE similar mail. Re-read the
   FILTER DESIGN RULES in the system message. If one of the EXISTING
   GMAIL FILTERS already fits this email, propose that same filter
   verbatim — we'll attach your new label to it rather than creating a
   duplicate. Only propose a new filter shape if no existing one fits.
3. Suggest auto-actions only when clearly appropriate. Examples:
   - heavy promotional mail: inbox=skip, mark_read=true, categorize=promotions
   - social-network notifications: categorize=social
   - update emails (shipped/delivered/account updates): categorize=updates
   - transactional alerts (receipts, statements): mark_read=true
   When unsure, leave them at the defaults (keep / default / none / false).

JSON FORMAT REMINDERS — strict:
- Booleans MUST be unquoted `true` / `false`, NEVER the strings "true"/"false".
- Nullable fields MUST be unquoted `null`, NEVER the string "null".
- Use straight double quotes (\"), never smart quotes.
- No trailing commas. No comments. No markdown fences (no ```).

OUTPUT — return EXACTLY one JSON object, no prose before or after:

{{
  "chosen_label": "<full path>",
  "is_new_label": <true|false>,
  "rationale": "<1-2 sentences. Mention WHY this label fits and WHY this filter shape (esp. subject vs keyword choice).>",
  "filter": {{
    "type": "<list_id | from_subject | from_keyword | from>",
    "from": "<sender email, or null if type='list_id'>",
    "subject": "<STABLE recurring substring (only when type='from_subject'). NEVER include amounts, dates, IDs, recipient names, edition numbers. Otherwise null.>",
    "keyword": "<distinctive word or short phrase from the body (only when type='from_keyword'). Avoid generic words. Otherwise null.>",
    "list_id": "<list id (only when type='list_id'). Otherwise null.>"
  }},
  "actions": {{
    "inbox": "<keep | skip | trash>",
    "importance": "<default | important | never_important>",
    "categorize": "<none | personal | social | promotions | updates | forums>",
    "mark_read": <true|false>,
    "star": <true|false>,
    "never_spam": <true|false>
  }},
  "confidence": <number 0.0-1.0; lower if you had to guess on either label or filter>
}}
"""


# --------------------------- response parsing ---------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _filter_query_from(spec: dict) -> tuple[str, dict]:
    """Convert the LLM's filter spec to (gmail_query, filter_criteria_dict).

    Filter types:
      list_id      Gmail filter `query`: list:<id>
      from_subject Gmail filter `from` + `subject` (substring match in subject)
      from_keyword Gmail filter `from` + `query` (matches keyword anywhere
                   in the message: subject + body + headers — broader than
                   from_subject; right call when subjects are one-off but
                   a body keyword reliably tags this category)
      from         Gmail filter `from` only
    """
    ftype = (spec.get("type") or "").lower()
    sender = (spec.get("from") or "").strip()
    subject = (spec.get("subject") or "").strip()
    keyword = (spec.get("keyword") or "").strip()
    list_id = (spec.get("list_id") or "").strip()

    if ftype == "list_id" or list_id:
        q = f"list:{list_id}"
        return q, {"query": q}
    if ftype == "from_subject" and sender and subject:
        q = f'from:{sender} subject:"{subject}"'
        return q, {"from": sender, "subject": subject}
    if ftype == "from_keyword" and sender and keyword:
        # Quote multi-word keywords so Gmail treats them as a phrase rather
        # than ANDing the words separately.
        if " " in keyword:
            q_kw = f'"{keyword}"'
        else:
            q_kw = keyword
        q = f"from:{sender} {q_kw}"
        return q, {"from": sender, "query": q_kw}
    if sender:
        q = f"from:{sender}"
        return q, {"from": sender}

    # Last-ditch: empty / unparseable filter
    return "", {}


def _coerce_bool(v) -> bool:
    """Robust bool coercion for LLM JSON. Local models often emit
    booleans as strings ('false', 'true', 'False'). bool('false') is
    True (non-empty string is truthy), which would invert destructive
    actions — fix by treating common string forms explicitly."""
    if v is True or v is False:
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "on")
    return bool(v) if v is not None else False


def _action_choices_from(spec: dict | None) -> ActionChoices:
    if not spec:
        return ActionChoices()
    return ActionChoices(
        inbox=spec.get("inbox", "keep") or "keep",
        importance=spec.get("importance", "default") or "default",
        categorize=spec.get("categorize", "none") or "none",
        mark_read=_coerce_bool(spec.get("mark_read")),
        star=_coerce_bool(spec.get("star")),
        never_spam=_coerce_bool(spec.get("never_spam")),
    )


def parse_llm_response(raw: str) -> IntelligentProposal:
    """Extract the JSON object from `raw` and build an IntelligentProposal."""
    if not raw:
        raise ValueError("empty LLM response")

    # Find the outermost JSON object — local models sometimes wrap in prose
    text = raw.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(text)
        if not m:
            raise ValueError(f"no JSON object found in response: {raw[:200]}...")
        data = json.loads(m.group(0))

    chosen = (data.get("chosen_label") or "").strip()
    if not chosen:
        raise ValueError("LLM did not return a chosen_label")

    filter_query, filter_criteria = _filter_query_from(data.get("filter") or {})

    confidence = data.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else 0.5
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return IntelligentProposal(
        chosen_label=chosen,
        is_new_label=bool(data.get("is_new_label", True)),
        rationale=(data.get("rationale") or "").strip(),
        filter_query=filter_query,
        filter_criteria=filter_criteria,
        suggested_actions=_action_choices_from(data.get("actions")),
        confidence=confidence,
        cache_key="",  # filled in by caller
        raw_response=raw,
    )


# --------------------------- main entry ---------------------------

ChatFn = Callable[..., str]


def _default_chat_fn() -> ChatFn:
    """Default chat_fn that calls Ollama via the existing helper.

    Honors QUICKLABEL_LLM_MODEL env var on every call so the user can
    swap models mid-process (e.g. via `os.environ` in a debug session)
    without restarting. Comment used to claim this and lie — the model
    name was being captured once at first call.
    """
    from lib.ollama_client import chat, get_client
    client = get_client()

    def _call(prompt: str, system: str = _SYSTEM_PROMPT) -> str:
        return chat(
            client, prompt=prompt, model=get_model_name(), system=system,
            # temp=0 for max determinism — labels and filters are decisions,
            # not creative writing. Two regens on the same email should
            # match.
            temperature=0.0, num_predict=800,
            # Default Ollama context (2048) is too small for system prompt
            # + 3KB body + 138-label list + JSON schema. 16K leaves plenty.
            num_ctx=16384,
        )

    return _call


def _heuristic_fallback(
    email: EmailFingerprint, sender_stats: SenderStats, cache_key: str,
    reason: str,
) -> IntelligentProposal:
    """Convert a deterministic-heuristic proposal into a low-confidence IntelligentProposal."""
    fp = propose_filter(email, sender_stats)
    lp = propose_label(email)
    return IntelligentProposal(
        chosen_label=lp.suggested_name,
        is_new_label=True,
        rationale=f"(heuristic fallback: {reason}) {lp.rationale}",
        filter_query=fp.query,
        filter_criteria=fp.criteria,
        suggested_actions=ActionChoices(),
        confidence=0.3,
        cache_key=cache_key,
        raw_response="",
    )


def intelligent_propose(
    email: EmailFingerprint,
    body: str,
    signals: Signals,
    sender_stats: SenderStats,
    existing_labels: list[str],
    storage: Storage | None = None,
    chat_fn: ChatFn | None = None,
    *,
    label_usage_counts: dict[str, int] | None = None,
    recent_applies: list[dict] | None = None,
    existing_filters: list[dict] | None = None,
) -> IntelligentProposal:
    """Top-level: returns an IntelligentProposal. Caches via storage if given."""
    cache_key = _cache_key(email, body)

    prompt = build_prompt(
        email, body, signals, sender_stats, existing_labels,
        label_usage_counts=label_usage_counts,
        recent_applies=recent_applies,
        existing_filters=existing_filters,
    )
    model = get_model_name()

    if storage is not None:
        cached = storage.get_intel(cache_key)
        if cached:
            try:
                p = parse_llm_response(cached)
                p.cache_key = cache_key
                p.prompt = prompt
                p.model = model + " (cached)"
                return p
            except ValueError:
                # Bad cache entry — drop and re-fetch
                storage.clear_intel(cache_key)

    chat = chat_fn or _default_chat_fn()

    try:
        raw = chat(prompt, system=_SYSTEM_PROMPT)
    except Exception as e:
        p = _heuristic_fallback(email, sender_stats, cache_key, f"LLM unreachable: {e}")
        p.prompt = prompt
        p.model = model + " (failed → heuristic)"
        return p

    try:
        p = parse_llm_response(raw)
    except ValueError as e:
        p = _heuristic_fallback(email, sender_stats, cache_key, f"invalid LLM JSON: {e}")
        p.prompt = prompt
        p.model = model + " (invalid JSON → heuristic)"
        p.raw_response = raw
        return p

    p.cache_key = cache_key
    p.prompt = prompt
    p.model = model
    if storage is not None:
        # Cache the raw response so a future parse uses the same input shape
        storage.set_intel(cache_key, raw)

    return p


# --------------------------- adapter to existing Proposal ---------------------------

def to_proposal(
    email: EmailFingerprint,
    sender_stats: SenderStats,
    ip: IntelligentProposal,
) -> Proposal:
    """Wrap an IntelligentProposal in the existing Proposal shape so the
    templates can render without changes."""
    if "/" in ip.chosen_label and ip.is_new_label:
        parent, _, name = ip.chosen_label.rpartition("/")
        suggested_parent: str | None = parent or None
        suggested_name = name
    else:
        suggested_parent = None
        suggested_name = ip.chosen_label

    label = LabelProposal(
        suggested_name=suggested_name,
        suggested_parent=suggested_parent,
        rationale=ip.rationale,
    )

    # Estimate match count from the heuristic stats — LLM doesn't know this
    if "list:" in ip.filter_query and sender_stats.total_from_list:
        est = sender_stats.total_from_list
    elif "from:" in ip.filter_query:
        if sender_stats.common_subject_prefix and sender_stats.common_subject_prefix.lower() in ip.filter_query.lower():
            est = sender_stats.prefix_match_count
        else:
            est = sender_stats.total_from_sender
    else:
        est = 0

    fproposal = FilterProposal(
        criteria=ip.filter_criteria,
        query=ip.filter_query,
        estimated_match_count=est,
        rationale=ip.rationale,
    )

    return Proposal(
        email=email,
        stats=sender_stats,
        filter=fproposal,
        label=label,
        questions=[],
    )
