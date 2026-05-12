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
MAX_BODY_CHARS_IN_PROMPT = 2500

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

_SYSTEM_PROMPT = (
    "You are an email organization assistant for Gmail. Your job has two parts:\n"
    "  1. Choose where this email belongs in the user's existing label tree, "
    "or propose a new sub-label under an existing parent. Strongly prefer "
    "reusing existing labels over creating new ones.\n"
    "  2. Design a Gmail filter rule that will catch similar FUTURE mail "
    "from the same source/category — not just this exact email.\n"
    "\n"
    "FILTER DESIGN RULES (critical — most common failure mode):\n"
    "- The 'subject' field in a Gmail filter does substring matching. So a\n"
    "  subject filter must be a STABLE substring that recurs across this\n"
    "  sender's emails — never the full one-off subject line.\n"
    "- DO NOT include variable parts in subject filters: dollar amounts, "
    "dates, transaction IDs, order numbers, names of recipients, "
    "issue/edition numbers (e.g. '#234'), times, account-last-4-digits.\n"
    "- Choosing filter type:\n"
    "    1. If a list-id is present, USE it. Most precise.\n"
    "    2. Else, if Sender history shows a stable subject prefix shared\n"
    "       across many emails (the prompt will tell you), USE\n"
    "       'from_subject' with that prefix as the subject. Don't drop\n"
    "       a clear pattern — it makes the filter narrower and safer.\n"
    "    3. Only fall back to sender-alone ('from') if there's no list-id\n"
    "       AND no shared subject prefix in the sender history.\n"
    "- Examples:\n"
    "    Subject 'Your Citi card was charged $123.45 at STARBUCKS on Mar 5'\n"
    "    Sender history: 530 prior, 420 share prefix 'Your Citi card was charged'\n"
    "      RIGHT: type='from_subject', from='alerts@citi.com',\n"
    "             subject='Your Citi card was charged'\n"
    "      WRONG: type='from'  (drops the strong, recurring pattern)\n"
    "      WRONG: subject='Your Citi card was charged $123.45 ...'  (variable parts)\n"
    "    Subject 'Order #A8472 shipped — tracking 1Z999...'\n"
    "    Sender history: 50 prior, no shared subject prefix\n"
    "      RIGHT: type='from', from='ship-confirm@amazon.com'\n"
    "    Subject 'Weekly insights #234 — The AI infra wave'\n"
    "    List-ID: weekly.sequoiacap.com\n"
    "      RIGHT: type='list_id', list_id='weekly.sequoiacap.com'\n"
    "\n"
    "Always respond with a single JSON object and nothing else — no prose "
    "before, no prose after, no markdown fences."
)


def build_prompt(
    email: EmailFingerprint,
    body: str,
    signals: Signals,
    sender_stats: SenderStats,
    existing_labels: list[str],
) -> str:
    labels = existing_labels[:MAX_LABELS_IN_PROMPT]
    labels_block = "\n".join(f"- {lbl}" for lbl in labels) if labels else "(none yet)"

    body_excerpt = (body or "")[:MAX_BODY_CHARS_IN_PROMPT]
    if len(body or "") > MAX_BODY_CHARS_IN_PROMPT:
        body_excerpt += "..."

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

    return f"""USER'S EXISTING LABELS (prefer to place under one of these):
{labels_block}

NEW EMAIL TO LABEL:
From: {email.sender_name or ''} <{email.sender_email}>
Subject: {email.subject}
List-ID: {email.list_id or '(none)'}

Sender history: {sender_line}

Signals:
{signal_lines}

Body excerpt:
\"\"\"
{body_excerpt}
\"\"\"

TASK:
1. Pick the best label path for this email. Reuse an existing label
   verbatim if one fits. Otherwise propose a new sub-label under an
   existing parent ('Parent/NewLeaf'). Top-level new labels are a last
   resort — only when no existing parent fits.
2. Design a Gmail filter that catches FUTURE similar mail. Re-read the
   FILTER DESIGN RULES in the system message before choosing the
   subject substring. If subject must be variable, prefer sender-only.
3. Suggest auto-actions only when clearly appropriate. Examples:
   - heavy promotional mail: inbox=skip, mark_read=true, categorize=promotions
   - social-network notifications: categorize=social
   - update emails (shipped/delivered/account updates): categorize=updates
   - transactional alerts (receipts, statements): mark_read=true
   When unsure, leave them at the defaults (keep / default / none / false).

OUTPUT — return EXACTLY one JSON object, no prose before or after, no markdown:

{{
  "chosen_label": "<full path>",
  "is_new_label": <true|false>,
  "rationale": "<1-2 sentences. Mention WHY this label fits and WHY this filter shape (esp. subject choice).>",
  "filter": {{
    "type": "<list_id | from | from_subject>",
    "from": "<sender email or null>",
    "subject": "<STABLE recurring substring, or null. NEVER include $ amounts, dates, IDs, recipient names, edition numbers.>",
    "list_id": "<list id, or null>"
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
    """Convert the LLM's filter spec to (gmail_query, filter_criteria_dict)."""
    ftype = (spec.get("type") or "").lower()
    sender = (spec.get("from") or "").strip()
    subject = (spec.get("subject") or "").strip()
    list_id = (spec.get("list_id") or "").strip()

    if ftype == "list_id" or list_id:
        q = f"list:{list_id}"
        return q, {"query": q}
    if ftype == "from_subject" and sender and subject:
        q = f'from:{sender} subject:"{subject}"'
        return q, {"from": sender, "subject": subject}
    if ftype == "from_keyword" and sender and subject:
        # Same shape as from_subject for our purposes
        q = f'from:{sender} subject:"{subject}"'
        return q, {"from": sender, "subject": subject}
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
) -> IntelligentProposal:
    """Top-level: returns an IntelligentProposal. Caches via storage if given."""
    cache_key = _cache_key(email, body)

    prompt = build_prompt(email, body, signals, sender_stats, existing_labels)
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
