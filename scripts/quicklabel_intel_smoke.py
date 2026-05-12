"""Quick live smoke test: hit Ollama with a synthetic email and confirm
we get a parseable IntelligentProposal back. Doesn't touch Gmail.
"""
from __future__ import annotations

import time

from quicklabel.headers import EmailFingerprint
from quicklabel.intelligence import intelligent_propose
from quicklabel.proposal import SenderStats
from quicklabel.signals import Signals


def _safe_print(*args, **kwargs):
    """Print that doesn't choke on Unicode in Windows cp1252 consoles."""
    text = " ".join(str(a) for a in args)
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"), **kwargs)


def main() -> int:
    email = EmailFingerprint(
        msg_id="m1",
        sender_email="news@sequoiacap.com",
        sender_name="Sequoia Capital",
        subject="Weekly insights #234 - The AI infra wave",
        list_id="weekly.sequoiacap.com",
        list_unsubscribe=True,
    )
    body = (
        "This week we look at the build-out of AI infrastructure: data center "
        "investment, the GPU supply chain, and where margins will land in 2026. "
        "Three companies we're watching in inference optimization..."
    )
    signals = Signals(
        has_unsubscribe=True, is_no_reply=False,
        looks_marketing=True, looks_transactional=False, is_personal=False,
    )
    stats = SenderStats(
        total_from_sender=87, total_from_list=87,
        common_subject_prefix="Weekly insights", prefix_match_count=85,
    )
    existing_labels = [
        "AI Newsletters", "AI Newsletters/The Information",
        "Finance", "Finance/Citi", "Finance/Citi/Alerts",
        "Receipts", "Travel", "Travel/Flights",
        "Personal", "Personal/Family",
    ]

    _safe_print("-> Calling Ollama (default model is gpt-oss:20b; first call ~30s while it loads)")
    t0 = time.time()
    p = intelligent_propose(
        email=email, body=body, signals=signals,
        sender_stats=stats, existing_labels=existing_labels,
        storage=None,  # no caching for smoke
    )
    dt = time.time() - t0

    _safe_print(f"\n=== Response in {dt:.1f}s (model: {p.model}) ===")
    _safe_print(f"Chosen label   : {p.chosen_label}")
    _safe_print(f"Is new label   : {p.is_new_label}")
    _safe_print(f"Confidence     : {p.confidence:.2f}")
    _safe_print(f"Filter query   : {p.filter_query}")
    _safe_print(f"Filter criteria: {p.filter_criteria}")
    if p.suggested_actions:
        sa = p.suggested_actions
        _safe_print(f"Actions        : inbox={sa.inbox} importance={sa.importance} "
              f"mark_read={sa.mark_read} star={sa.star} never_spam={sa.never_spam}")
    _safe_print(f"Rationale      : {p.rationale}")
    _safe_print("")

    if p.confidence < 0.3:
        print("FAIL: very low confidence — likely a fallback")
        return 1
    if not p.chosen_label:
        print("FAIL: no chosen_label")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
