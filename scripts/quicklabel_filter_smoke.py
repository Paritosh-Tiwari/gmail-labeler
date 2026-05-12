"""Live smoke test for the new gmail_client filter helpers.

Creates a uniquely-named label + filter that match nothing, verifies
they appear in list calls, then cleans up. Touches real Gmail — only
run interactively.

Uses the QuickLabel token (separate from the parent project's). Will
trigger OAuth on first run.

Usage:
    .venv/Scripts/python.exe scripts/quicklabel_filter_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lib.gmail_client import (  # noqa: E402
    create_filter,
    create_label,
    delete_filter,
    list_filters,
    list_labels,
    search_count,
)
from quicklabel.auth import build_service, get_credentials  # noqa: E402


def main() -> int:
    creds = get_credentials()
    svc = build_service(creds)

    stamp = int(time.time())
    label_name = f"_quicklabel_smoke_{stamp}"
    bogus_from = f"definitely-not-real-{stamp}@example.invalid"

    print(f"[1/6] Creating test label: {label_name}")
    label = create_label(svc, label_name)
    label_id = label["id"]
    print(f"      label_id={label_id}")

    print(f"[2/6] Creating filter from:{bogus_from} -> +{label_name}")
    flt = create_filter(svc, criteria={"from": bogus_from}, add_label_ids=[label_id])
    filter_id = flt["id"]
    print(f"      filter_id={filter_id}")

    print("[3/6] Listing filters and confirming our id is present...")
    all_filters = list_filters(svc)
    found = any(f["id"] == filter_id for f in all_filters)
    assert found, f"Created filter {filter_id} not found in list_filters()"
    print(f"      OK -- {len(all_filters)} total filters, ours is in the list")

    print("[4/6] Listing labels and confirming our label_id is present...")
    all_labels = list_labels(svc)
    found_lbl = any(lbl["id"] == label_id for lbl in all_labels)
    assert found_lbl, f"Created label {label_id} not found in list_labels()"
    print(f"      OK -- {len(all_labels)} total labels, ours is in the list")

    print(f"[5/6] search_count for from:{bogus_from}...")
    n = search_count(svc, f"from:{bogus_from}")
    print(f"      {n} matching messages (expected 0)")

    print(f"[6/6] Cleaning up filter + label")
    delete_filter(svc, filter_id)
    svc.users().labels().delete(userId="me", id=label_id).execute()
    print("      Done.")

    print("\nAll filter/label CRUD operations succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
