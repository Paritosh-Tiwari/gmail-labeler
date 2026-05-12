"""Gmail-side write operations: ensure label exists, create filter, back-prop label.

Layered on top of lib/gmail_client.py.
"""
from __future__ import annotations

from googleapiclient.errors import HttpError

from lib.gmail_client import (
    batch_modify_labels,
    create_filter,
    create_label,
    list_labels,
    search_message_ids,
)


def ensure_label(service, full_name: str) -> str:
    """Ensure a label with `full_name` exists; return its label_id.

    For nested labels ('Parent/Child'), creates the parent first if missing.
    Case-insensitive existing-label lookup.
    """
    existing = {lbl["name"].lower(): lbl for lbl in list_labels(service)}

    # Walk parents
    parts = [p for p in full_name.split("/") if p]
    cumulative = ""
    last_id = None
    for part in parts:
        cumulative = f"{cumulative}/{part}" if cumulative else part
        key = cumulative.lower()
        if key in existing:
            last_id = existing[key]["id"]
            continue
        try:
            created = create_label(service, cumulative)
            last_id = created["id"]
            existing[key] = created
        except HttpError as e:
            # 409 = already exists (race or case mismatch); refetch and
            # try the lookup with both lowercase and the cumulative path
            # exactly. If neither matches, surface a clearer error than
            # KeyError.
            if e.resp.status == 409:
                existing = {lbl["name"].lower(): lbl for lbl in list_labels(service)}
                hit = existing.get(key) or existing.get(cumulative.lower())
                if hit is None:
                    raise RuntimeError(
                        f"Gmail returned 409 for label {cumulative!r} but it's not "
                        f"present after refetch. List of {len(existing)} labels: "
                        f"{sorted(existing.keys())[:20]}..."
                    ) from e
                last_id = hit["id"]
            else:
                raise
    if last_id is None:
        raise RuntimeError(f"failed to ensure label {full_name!r}")
    return last_id


def create_label_filter(
    service,
    criteria: dict,
    label_id: str | None,
    extra_add_label_ids: list[str] | None = None,
    extra_remove_label_ids: list[str] | None = None,
) -> dict:
    """Create a server-side filter for `criteria` that adds `label_id` and any
    extra system label mutations (e.g. ['INBOX'] in remove for skip-inbox)."""
    add_ids: list[str] = []
    if label_id:
        add_ids.append(label_id)
    if extra_add_label_ids:
        add_ids.extend(extra_add_label_ids)
    remove_ids = list(extra_remove_label_ids or [])
    return create_filter(
        service,
        criteria=criteria,
        add_label_ids=add_ids or None,
        remove_label_ids=remove_ids or None,
    )


def backprop_label(
    service,
    query: str,
    label_id: str | None,
    batch_size: int = 1000,
    cap: int = 5000,
    extra_add_label_ids: list[str] | None = None,
    extra_remove_label_ids: list[str] | None = None,
) -> list[str]:
    """Apply `label_id` (and any extras) to existing matches. Returns the
    list of affected message IDs (so the caller can persist them for undo).
    Caps at `cap`."""
    add_ids: list[str] = []
    if label_id:
        add_ids.append(label_id)
    if extra_add_label_ids:
        add_ids.extend(extra_add_label_ids)
    remove_ids = list(extra_remove_label_ids or [])

    if not add_ids and not remove_ids:
        return []

    ids = search_message_ids(service, query, max_results=cap)
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        batch_modify_labels(
            service, batch,
            add_label_ids=add_ids or None,
            remove_label_ids=remove_ids or None,
        )
    return ids


def reverse_backprop(
    service,
    message_ids: list[str],
    add_label_ids: list[str],
    remove_label_ids: list[str],
    batch_size: int = 1000,
) -> None:
    """Undo a backprop_label call: remove what was added, add back what was
    removed. The label is applied at the message level; this strips it back
    out for the same message set."""
    if not message_ids:
        return
    for i in range(0, len(message_ids), batch_size):
        batch = message_ids[i:i + batch_size]
        # Reverse: previously-added labels are now removed, vice versa
        batch_modify_labels(
            service, batch,
            add_label_ids=remove_label_ids or None,
            remove_label_ids=add_label_ids or None,
        )
