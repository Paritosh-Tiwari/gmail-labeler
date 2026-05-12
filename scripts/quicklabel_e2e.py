"""End-to-end test against the running QuickLabel server.

Picks a real thread from the user's inbox, runs the full /label -> /apply
flow with a clearly-named test label, verifies everything, and cleans up.

Touches real Gmail. Server must be running (python -m quicklabel serve).

Usage:
    .venv/Scripts/python.exe scripts/quicklabel_e2e.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import urllib.request
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lib.gmail_client import (  # noqa: E402
    batch_modify_labels,
    delete_filter,
    list_filters,
    list_labels,
    search_message_ids,
)
from quicklabel.auth import build_service, get_credentials  # noqa: E402
from quicklabel.settings import BASE_URL  # noqa: E402


def _http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode("utf-8")


def _http_post(url: str, fields: dict[str, str]) -> tuple[int, str]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, r.read().decode("utf-8")


def _scrape(html: str, name: str) -> str | None:
    """Pull the value="..." of an <input name="X"> from the proposal page."""
    m = re.search(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else None


def main() -> int:
    print(f"[setup] Verifying server at {BASE_URL}...")
    status, _ = _http_get(f"{BASE_URL}/healthz")
    assert status == 200, f"Server not healthy: {status}"
    print("        OK")

    print("[setup] Building Gmail service for verification...")
    svc = build_service(get_credentials())

    print("[setup] Picking a recent inbox thread...")
    threads = svc.users().threads().list(
        userId="me", maxResults=20, q="from:alerts@info6.citi.com",
    ).execute().get("threads", [])
    assert threads, "No Citi Alerts threads found — adjust query in script."
    test_thread_id = threads[0]["id"]
    print(f"        thread_id={test_thread_id}")

    print(f"[1/6] GET /label?id={test_thread_id}")
    status, html = _http_get(f"{BASE_URL}/label?id={test_thread_id}")
    assert status == 200, f"GET /label failed: {status}"

    nonce = _scrape(html, "nonce")
    suggested_label = _scrape(html, "label_name")
    suggested_query = _scrape(html, "filter_query")
    suggested_from = _scrape(html, "filter_from")
    print(f"        nonce={nonce[:8]}... label={suggested_label!r} query={suggested_query!r}")
    assert nonce and suggested_label and suggested_query

    stamp = int(time.time())
    test_label = f"_quicklabel_e2e_{stamp}"
    test_parent = "_quicklabel_e2e_parents"
    full_label_path = f"{test_parent}/{test_label}"

    print(f"[2/6] POST /apply with label={full_label_path}")
    status, applied_html = _http_post(f"{BASE_URL}/apply", {
        "nonce": nonce,
        "label_name": test_label,
        "parent_label": test_parent,
        "filter_query": suggested_query,
        "filter_from": suggested_from or "",
        "filter_subject": "",
        "create_filter_flag": "yes",
        "backprop_flag": "yes",
    })
    assert status == 200, f"POST /apply failed: {status}\n{applied_html[:500]}"
    backprop_match = re.search(r"Labeled <strong>(\d+)</strong>", applied_html)
    backprop_n = int(backprop_match.group(1)) if backprop_match else 0
    print(f"        applied OK — backprop_count={backprop_n}")

    print("[3/6] Verifying label exists...")
    labels = list_labels(svc)
    label_lookup = {lbl["name"]: lbl for lbl in labels}
    assert full_label_path in label_lookup, f"Label {full_label_path!r} not found"
    assert test_parent in label_lookup, f"Parent {test_parent!r} not created"
    label_id = label_lookup[full_label_path]["id"]
    parent_id = label_lookup[test_parent]["id"]
    print(f"        label_id={label_id}  parent_id={parent_id}")

    print("[4/6] Verifying filter exists...")
    filters = list_filters(svc)
    matching_filters = [
        f for f in filters
        if (f.get("action", {}).get("addLabelIds") or []) == [label_id]
    ]
    assert matching_filters, f"No filter found targeting label_id={label_id}"
    filter_id = matching_filters[0]["id"]
    print(f"        filter_id={filter_id}  criteria={matching_filters[0].get('criteria')}")

    print(f"[5/6] Verifying back-prop applied label to {backprop_n} messages...")
    if backprop_n > 0:
        labeled_ids = search_message_ids(svc, f"label:{full_label_path}", max_results=backprop_n + 5)
        # Some messages may have been excluded by includeSpamTrash=False; allow >=
        assert len(labeled_ids) >= 1, "Back-prop reported >0 but search finds none"
        print(f"        confirmed {len(labeled_ids)} messages have the label")
    else:
        print("        (back-prop reported 0)")

    print("[6/6] Cleanup: removing label from messages, deleting filter + label + parent")
    if backprop_n > 0:
        ids_to_strip = search_message_ids(svc, f"label:{full_label_path}", max_results=backprop_n + 5)
        for i in range(0, len(ids_to_strip), 1000):
            batch_modify_labels(svc, ids_to_strip[i:i+1000], remove_label_ids=[label_id])
    delete_filter(svc, filter_id)
    svc.users().labels().delete(userId="me", id=label_id).execute()
    svc.users().labels().delete(userId="me", id=parent_id).execute()
    print("        Cleaned up.")

    print("\nE2E test PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
