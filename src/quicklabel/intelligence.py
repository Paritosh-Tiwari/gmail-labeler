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
  2. Design a Gmail filter rule that will catch similar FUTURE mail that
     belongs in the same label — not just this exact email, and not just
     this sender if the category arrives from many senders.

CORE FILTER-DESIGN QUESTION (ask this first, every time):
  "If I applied this label, which OTHER emails in this inbox should
  ALSO receive it?"
That defines the intended PATTERN. Then pick the SMALLEST SET of
constraints that captures only that pattern. A too-narrow filter
creates duplicate labels for the same category from different senders;
a too-broad filter mislabels unrelated mail.

IDENTIFY THE STABLE SIGNAL across the pattern:
  - SENDER: Does this category come from ONE sender (e.g. a specific
    newsletter, a specific person), or MANY senders (e.g. OTP /
    verification codes arrive from Okta, Google, Slack, Apple, banks —
    all different senders, same category)?
  - SUBJECT: Does the subject share a stable prefix or template across
    the category ('Your order has shipped', 'Verify your email'), or
    is each subject unique?
  - BODY KEYWORD: Is there a distinctive phrase that reliably appears
    in this category ('verification code', 'shipping confirmation',
    'invoice attached')?
  - MAILING LIST: Does the message have a List-ID header? (Most
    precise — always use it when available.)

FILTER TYPES — match them to which signals are stable:
  list_id       Mailing list with List-ID header. Use whenever present.
  from          ONE sender, ONE category they send. Use when this
                sender exclusively sends mail you want under this label
                (a single-creator newsletter, a specific colleague).
  from_subject  ONE sender, several categories, ONE stable subject
                template distinguishes the category you want. Use for
                recurring transactional mail ('Your X was charged',
                'Order #N shipped').
  from_keyword  ONE sender, several categories, ONE body keyword tags
                the category you want. Use when the sender mixes mail
                types and a phrase distinguishes the one you want.
  subject_only  MANY senders, ONE stable subject pattern. Use when the
                same category arrives from many senders with the same
                subject template (e.g. 'Order confirmation' across
                retailers).
  keyword_only  MANY senders, ONE stable body keyword. Use for
                cross-sender category mail — OTPs / verification codes,
                password resets, generic receipts. Combine multiple
                phrases with OR for broader coverage:
                '"verification code" OR "one-time code" OR "security code"'.

DECISION TABLE (pick the row that matches the stable signals):
  Has List-ID?                          → list_id
  1 sender, sends only this category    → from
  1 sender, stable subject prefix       → from_subject
  1 sender, stable body keyword         → from_keyword
  Many senders, stable subject          → subject_only
  Many senders, stable body keyword     → keyword_only

DOMAIN vs FULL EMAIL for `from:`:
  Gmail's `from:` accepts a bare domain. When the same brand emails
  from many sub-addresses (alerts@info6.citi.com, service@citi.com,
  customercare@citicards.com), prefer `from:citi.com` over a single
  full address. Use the full address only when the user clearly wants
  just that one sub-address.

FILTER VALUE CONSTRAINTS:
  - subject substrings: NEVER include variable parts (amounts, dates,
    transaction IDs, order numbers, recipient names, edition numbers
    like '#234', times, account-last-4-digits, OTP codes themselves).
  - keywords: pick a SPECIFIC distinctive phrase. Not conversational
    filler ('email', 'click', 'the', 'we', 'your'). Phrases of 2-3
    words are usually best.
  - keyword_only filters: prefer OR-combined phrases that cover the
    full category, not a single narrow keyword.

Always respond with a single JSON object and nothing else — no prose
before, no prose after, no markdown fences. Use English for all label
names and rationales.

WORKED EXAMPLES (two contrasting cases that anchor the decision axis):

EXAMPLE 1 — category email from many senders (keyword_only):
Input:
  USER'S EXISTING LABELS:
  - Security/OTP   (24× applied via QuickLabel)
  - Newsletters/Tech
  NEW EMAIL TO LABEL:
  From: Okta <noreply@okta.com>
  Subject: Your Okta verification code is 482910
  Sender history: 8 prior, no shared subject prefix

Output:
{
  "chosen_label": "Security/OTP",
  "is_new_label": false,
  "rationale": "OTP / verification-code email. This category arrives from many senders (Okta, Google, banks, social apps); pinning to Okta would only catch this one source. Keyword-only filter on OR-combined OTP phrases broadens across the category without overmatching.",
  "filter": {
    "type": "keyword_only",
    "from": null,
    "subject": null,
    "keyword": "\\"verification code\\" OR \\"one-time code\\" OR \\"security code\\"",
    "list_id": null
  },
  "actions": {
    "inbox": "keep",
    "importance": "default",
    "categorize": "updates",
    "mark_read": false,
    "star": false,
    "never_spam": false
  },
  "confidence": 0.85
}

EXAMPLE 2 — single creator, all their mail goes here (from):
Input:
  USER'S EXISTING LABELS:
  - Newsletters
  - Learning
  NEW EMAIL TO LABEL:
  From: Stratechery <ben@stratechery.com>
  Subject: The case for vertical AI agents
  Sender history: 132 prior, no shared subject prefix

Output:
{
  "chosen_label": "Newsletters/Stratechery",
  "is_new_label": true,
  "rationale": "Single-creator newsletter, editorial one-off subjects. Sender exclusively sends this newsletter — pin to sender. Bare domain so any future sub-address still matches.",
  "filter": {
    "type": "from",
    "from": "stratechery.com",
    "subject": null,
    "keyword": null,
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
  "confidence": 0.88
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
    "type": "<list_id | from | from_subject | from_keyword | subject_only | keyword_only>",
    "from": "<sender email OR bare domain (e.g. 'citi.com'). Required for from/from_subject/from_keyword. Null for list_id/subject_only/keyword_only.>",
    "subject": "<STABLE recurring substring. Required for from_subject and subject_only. NEVER include variable parts (amounts, dates, IDs, recipient names, edition numbers, OTP codes). Otherwise null.>",
    "keyword": "<distinctive word or short phrase. Required for from_keyword and keyword_only. For keyword_only, prefer OR-combined phrases like '\\\"verification code\\\" OR \\\"one-time code\\\"'. Otherwise null.>",
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
      list_id       Gmail filter `query`: list:<id>
      from          Gmail filter `from` only (sender email OR bare domain)
      from_subject  Gmail filter `from` + `subject` (substring match)
      from_keyword  Gmail filter `from` + `query` (keyword anywhere in the
                    message). Multi-word keywords quoted.
      subject_only  Gmail filter `subject` only — many senders, one stable
                    subject template
      keyword_only  Gmail filter `query` only — many senders, one stable
                    body keyword (or OR-combined keyword group). For
                    category-style mail (OTPs, password resets, etc.)
    """
    ftype = (spec.get("type") or "").lower()
    sender = (spec.get("from") or "").strip()
    subject = (spec.get("subject") or "").strip()
    keyword = (spec.get("keyword") or "").strip()
    list_id = (spec.get("list_id") or "").strip()

    def _quote_kw(kw: str) -> str:
        # If the keyword already contains quotes or OR (i.e. already a
        # composed query), pass through verbatim. Otherwise quote multi-
        # word phrases so Gmail treats them as a phrase rather than ANDing.
        if not kw:
            return kw
        if '"' in kw or " OR " in kw or " or " in kw:
            return kw
        if " " in kw:
            return f'"{kw}"'
        return kw

    if ftype == "list_id" or list_id:
        q = f"list:{list_id}"
        return q, {"query": q}
    if ftype == "from_subject" and sender and subject:
        q = f'from:{sender} subject:"{subject}"'
        return q, {"from": sender, "subject": subject}
    if ftype == "from_keyword" and sender and keyword:
        q_kw = _quote_kw(keyword)
        q = f"from:{sender} {q_kw}"
        return q, {"from": sender, "query": q_kw}
    if ftype == "subject_only" and subject:
        q = f'subject:"{subject}"'
        return q, {"subject": subject}
    if ftype == "keyword_only" and keyword:
        q_kw = _quote_kw(keyword)
        return q_kw, {"query": q_kw}
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


def _prepare_prompt_for_model(prompt: str, model: str) -> str:
    """Per-model prompt shims.

    Qwen3 ships with reasoning ('thinking') mode ON by default — the
    model emits hidden <think> tokens before any visible output, exactly
    like gpt-oss. In QuickLabel's context the thinking burns the
    num_predict budget without measurably improving label accuracy
    (see model-comparison runs). Appending `/no_think` to the user
    prompt is Qwen3's documented way to disable thinking for a turn.

    No shim is available for gpt-oss style models (their reasoning is
    architectural, not prompt-controlled); for those we rely on the
    retry-once logic in intelligent_propose.
    """
    name = (model or "").lower()
    if name.startswith("qwen3") and "/no_think" not in prompt:
        return prompt.rstrip() + "\n\n/no_think"
    return prompt


def _default_chat_fn() -> ChatFn:
    """Default chat_fn that calls Ollama via the existing helper.

    Honors QUICKLABEL_LLM_MODEL env var on every call so the user can
    swap models mid-process (e.g. via `os.environ` in a debug session)
    without restarting.
    """
    from lib.ollama_client import chat, get_client
    client = get_client()

    def _call(prompt: str, system: str = _SYSTEM_PROMPT,
              num_predict: int = 1500) -> str:
        model = get_model_name()
        return chat(
            client,
            prompt=_prepare_prompt_for_model(prompt, model),
            model=model, system=system,
            # temp=0 for max determinism — labels and filters are decisions,
            # not creative writing. (Reasoning models like gpt-oss are still
            # non-deterministic here despite temp=0 because their hidden
            # thinking branch varies; we paper over that with retry logic
            # in intelligent_propose.)
            temperature=0.0, num_predict=num_predict,
            # Default Ollama context (2048) is too small for system prompt
            # + body + label list + JSON schema. 16K leaves plenty.
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

    # Retry once with a larger prediction budget if the first call returned
    # empty or unparseable JSON. Reasoning models like gpt-oss:20b consume
    # tokens on hidden 'thinking' before visible output and sometimes burn
    # the whole num_predict budget — leaving an empty visible response.
    # Truncated-mid-JSON responses get a second chance for the same reason.
    retried = False
    try:
        p = parse_llm_response(raw)
    except ValueError as first_err:
        try:
            raw_retry = chat(prompt, system=_SYSTEM_PROMPT, num_predict=3500)
            retried = True
        except Exception:
            raw_retry = ""
        try:
            p = parse_llm_response(raw_retry) if raw_retry else None
        except ValueError:
            p = None
        if p is None:
            fb = _heuristic_fallback(
                email, sender_stats, cache_key,
                f"invalid LLM JSON: {first_err}",
            )
            fb.prompt = prompt
            fb.model = model + (
                " (invalid JSON twice → heuristic)" if retried
                else " (invalid JSON → heuristic)"
            )
            fb.raw_response = raw_retry or raw
            return fb
        # Retry succeeded — use its response for caching too
        raw = raw_retry

    p.cache_key = cache_key
    p.prompt = prompt
    p.model = model + (" (retry)" if retried else "")
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
