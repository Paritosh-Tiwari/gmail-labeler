"""Compute SenderStats for a sender (and optionally list-id) from Gmail."""
from __future__ import annotations

from .headers import EmailFingerprint, fingerprint
from .proposal import SenderStats, common_subject_prefix


SUBJECT_SAMPLE_CAP = 50  # how many subjects we fetch to detect a stable prefix
COUNT_CAP = 1000         # cap on search counts for back-prop estimate


def _fetch_subjects(service, query: str, max_msgs: int) -> list[str]:
    """Fetch up to max_msgs message IDs matching query and return their subjects."""
    from lib.gmail_client import search_message_ids

    ids = search_message_ids(service, query, max_results=max_msgs)
    subjects: list[str] = []
    for mid in ids:
        msg = service.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["Subject"],
        ).execute()
        fp = fingerprint(msg)
        subjects.append(fp.subject)
    return subjects


def compute_sender_stats(service, email: EmailFingerprint) -> SenderStats:
    """Hit Gmail for sender (and list) counts + subject prefix detection."""
    from lib.gmail_client import search_count

    sender_q = f"from:{email.sender_email}"
    total_from_sender = search_count(service, sender_q, cap=COUNT_CAP)

    total_from_list = 0
    if email.list_id:
        total_from_list = search_count(service, f"list:{email.list_id}", cap=COUNT_CAP)

    # Sample subjects for prefix detection (use list query if narrower, else sender)
    sample_query = (
        f"list:{email.list_id}"
        if email.list_id and total_from_list and total_from_list < total_from_sender
        else sender_q
    )
    subjects = _fetch_subjects(service, sample_query, SUBJECT_SAMPLE_CAP)
    prefix, prefix_count = common_subject_prefix(subjects)

    return SenderStats(
        total_from_sender=total_from_sender,
        total_from_list=total_from_list,
        common_subject_prefix=prefix,
        prefix_match_count=prefix_count,
    )
