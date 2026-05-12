"""Pure proposal engine: given an email fingerprint + sender history,
produce a suggested filter rule, label name, and any clarifying questions.

No I/O — caller fetches the data and passes it in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .headers import EmailFingerprint


# Words we strip from sender display names when proposing a label
_LABEL_STOPWORDS = {
    "newsletter", "newsletters", "notification", "notifications",
    "team", "info", "noreply", "no-reply", "updates", "alerts",
    "the", "a", "an",
}

# Subject prefixes/markers we strip before comparing across emails
_SUBJECT_NOISE = re.compile(
    r"^(re:|fwd:|fw:)\s*|"
    r"\s*\[\d+\]\s*|"  # [123]
    r"\s*\(\d+\)\s*",  # (123)
    flags=re.IGNORECASE,
)


@dataclass
class SenderStats:
    """Aggregate facts about prior mail from this sender (and optionally list)."""
    total_from_sender: int                       # count of from:sender@x
    total_from_list: int                         # count of list:list-id (0 if no list-id)
    common_subject_prefix: str | None            # stable prefix in last N subjects, if any
    prefix_match_count: int                      # how many of total_from_sender match the prefix


@dataclass
class FilterProposal:
    criteria: dict[str, str]   # for Gmail API filters.create(criteria=...)
    query: str                 # equivalent Gmail search query
    estimated_match_count: int
    rationale: str


@dataclass
class LabelProposal:
    suggested_name: str
    suggested_parent: str | None
    rationale: str


@dataclass
class Question:
    field: str                 # "filter" or "label"
    text: str
    options: list[dict]        # [{label: "...", value: {...patch to apply...}}, ...]


@dataclass
class Proposal:
    email: EmailFingerprint
    stats: SenderStats
    filter: FilterProposal
    label: LabelProposal
    questions: list[Question] = field(default_factory=list)


# --------------------------- subject prefix detection ----------------------------

def _normalize_subject(subj: str) -> str:
    """Strip common reply/fwd/markers; collapse whitespace."""
    s = _SUBJECT_NOISE.sub("", subj or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def common_subject_prefix(subjects: list[str], min_share: float = 0.5) -> tuple[str | None, int]:
    """Find the longest word-level prefix shared by >= min_share of subjects.

    Returns (prefix, match_count). Returns (None, 0) if nothing meaningful.
    """
    if not subjects:
        return None, 0
    norm = [_normalize_subject(s) for s in subjects if s]
    norm = [s for s in norm if s]
    if not norm:
        return None, 0

    # Word-level prefix tree, walk longest path that >= min_share have
    threshold = max(2, int(len(norm) * min_share))
    if len(norm) < 2:
        return None, 0

    # Constrain candidates to subjects still matching the prefix-so-far.
    # That way `count` is truly the # of subjects that begin with the d-word prefix,
    # and we can stop extending once the count starts to drop (variable part begins).
    word_lists = [s.split(" ") for s in norm]
    matching = list(word_lists)
    prefix: list[str] = []
    while True:
        depth = len(prefix)
        cands: dict[str, int] = {}
        for wl in matching:
            if depth < len(wl):
                cands[wl[depth]] = cands.get(wl[depth], 0) + 1
        if not cands:
            break
        word, count = max(cands.items(), key=lambda kv: kv[1])
        # Only extend if doing so doesn't shrink our matching set
        # (i.e., this word is shared by *every* subject currently in the prefix group).
        # That's the boundary between stable prefix and variable suffix.
        if count == len(matching) and count >= threshold:
            prefix.append(word)
            matching = [wl for wl in matching if depth < len(wl) and wl[depth] == word]
        else:
            break

    if not prefix:
        return None, 0
    candidate = " ".join(prefix).strip()
    if len(candidate) < 4:
        return None, 0
    actual = sum(1 for s in norm if s.startswith(candidate))
    return candidate, actual


# --------------------------- filter proposal ------------------------------

def propose_filter(email: EmailFingerprint, stats: SenderStats) -> FilterProposal:
    """Build the most precise filter rule we can confidently propose."""
    # Tier 1: list-id is highest precision
    if email.list_id and stats.total_from_list > 0:
        return FilterProposal(
            criteria={"query": f"list:{email.list_id}"},
            query=f"list:{email.list_id}",
            estimated_match_count=stats.total_from_list,
            rationale=f"Mailing list ID '{email.list_id}' is the most precise filter — won't catch other mail from this sender.",
        )

    # Tier 2: from + stable subject prefix
    if (
        stats.common_subject_prefix
        and stats.prefix_match_count >= 3
        and stats.prefix_match_count >= 0.6 * stats.total_from_sender
    ):
        prefix = stats.common_subject_prefix
        return FilterProposal(
            criteria={"from": email.sender_email, "subject": prefix},
            query=f'from:{email.sender_email} subject:"{prefix}"',
            estimated_match_count=stats.prefix_match_count,
            rationale=f"This sender sends multiple email types; narrowing to subject containing '{prefix}'.",
        )

    # Tier 3: sender alone
    return FilterProposal(
        criteria={"from": email.sender_email},
        query=f"from:{email.sender_email}",
        estimated_match_count=stats.total_from_sender,
        rationale=f"Filter on sender — {stats.total_from_sender} matching emails.",
    )


# --------------------------- label proposal ------------------------------

def _clean_label_token(s: str) -> str:
    """Strip stopwords, punctuation, title-case."""
    s = re.sub(r"[^\w\s\-/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    words = [w for w in s.split() if w.lower() not in _LABEL_STOPWORDS]
    if not words:
        words = s.split()  # fall back to keeping at least something
    return " ".join(w if w.isupper() else w.title() for w in words).strip()


def _domain_stem(email_addr: str) -> str:
    """'newsletter@sequoiacap.com' -> 'sequoiacap'."""
    if "@" not in email_addr:
        return email_addr
    domain = email_addr.split("@", 1)[1]
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2]
    return domain


def propose_label(email: EmailFingerprint) -> LabelProposal:
    """Best-guess label name. User will edit; we just want a sensible default."""
    # Prefer a cleaned display name if available
    if email.sender_name:
        cleaned = _clean_label_token(email.sender_name)
        if cleaned:
            return LabelProposal(
                suggested_name=cleaned,
                suggested_parent=None,
                rationale=f"From sender display name '{email.sender_name}'.",
            )

    # Else derive from domain
    stem = _domain_stem(email.sender_email)
    cleaned = _clean_label_token(stem)
    return LabelProposal(
        suggested_name=cleaned or stem,
        suggested_parent=None,
        rationale=f"From sender domain '{stem}'.",
    )


# --------------------------- question generation ------------------------------

def generate_questions(
    email: EmailFingerprint, stats: SenderStats, fproposal: FilterProposal
) -> list[Question]:
    """Surface ambiguities the user should resolve. Empty list = no ambiguity."""
    qs: list[Question] = []

    # If we picked list-id but sender-only would be much broader, ask
    if (
        email.list_id
        and stats.total_from_list > 0
        and stats.total_from_sender > stats.total_from_list * 1.5
    ):
        qs.append(Question(
            field="filter",
            text=(
                f"This sender ({email.sender_email}) sends multiple lists. "
                f"Narrow filter to just this list ({stats.total_from_list} emails) "
                f"or all sender mail ({stats.total_from_sender})?"
            ),
            options=[
                {"label": f"Just this list ({stats.total_from_list})",
                 "value": {"criteria": {"query": f"list:{email.list_id}"}}},
                {"label": f"All sender mail ({stats.total_from_sender})",
                 "value": {"criteria": {"from": email.sender_email}}},
            ],
        ))

    # If we picked from-only but a strong subject prefix exists with weaker coverage, ask
    if (
        not email.list_id
        and "subject" not in fproposal.criteria
        and stats.common_subject_prefix
        and 0.3 * stats.total_from_sender <= stats.prefix_match_count < 0.6 * stats.total_from_sender
    ):
        prefix = stats.common_subject_prefix
        qs.append(Question(
            field="filter",
            text=(
                f"Sender sends mixed content. Apply to all sender mail "
                f"({stats.total_from_sender}) or narrow to subject containing "
                f"'{prefix}' ({stats.prefix_match_count})?"
            ),
            options=[
                {"label": f"All sender mail ({stats.total_from_sender})",
                 "value": {"criteria": {"from": email.sender_email}}},
                {"label": f'Narrow to "{prefix}" ({stats.prefix_match_count})',
                 "value": {"criteria": {"from": email.sender_email, "subject": prefix}}},
            ],
        ))

    return qs


# --------------------------- top-level ------------------------------

def build_proposal(email: EmailFingerprint, stats: SenderStats) -> Proposal:
    """Top-level entry point. Pure function."""
    fproposal = propose_filter(email, stats)
    lproposal = propose_label(email)
    questions = generate_questions(email, stats, fproposal)
    return Proposal(
        email=email,
        stats=stats,
        filter=fproposal,
        label=lproposal,
        questions=questions,
    )
