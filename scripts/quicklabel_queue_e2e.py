"""Live end-to-end test for the QuickLabel /queue feature.

Assumes the QuickLabel server is running on http://127.0.0.1:8765.

What it does:
  1. Triggers /queue/refresh (real Gmail scan over last 24h)
  2. Fetches /queue and parses out at least one pending proposal
  3. Picks the LAST (lowest-recent_count) proposal as the test target
  4. POSTs /queue/decide action=skip on it
  5. Fetches /queue again, verifies the test proposal is gone
  6. Cleans up by deleting the test row directly from data/quicklabel.db,
     so we don't leave a permanent 'skipped' decision the user didn't make

This script touches the user's real Gmail (read-only — it only fetches
metadata) and the user's real local DB (writes one row, then deletes it).
It does NOT create any labels or filters in Gmail.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import urllib.request
import urllib.parse


BASE_URL = "http://127.0.0.1:8765"
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "quicklabel.db"


def http_get(path: str) -> str:
    with urllib.request.urlopen(BASE_URL + path) as r:
        return r.read().decode("utf-8")


def http_post(path: str, fields: dict) -> str:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as r:
        return r.read().decode("utf-8")


def extract_nonce(html: str) -> str:
    m = re.search(r'name="nonce"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError("could not find nonce on page")
    return m.group(1)


def extract_pending_ids(html: str) -> list[int]:
    """Pull proposal_id values from the queue page (each pending row has one)."""
    return [int(x) for x in re.findall(r'name="proposal_id"\s+value="(\d+)"', html)]


def cleanup_db_row(proposal_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM proposal_log WHERE id = ?", (proposal_id,))


def main() -> int:
    print(f"-> Hitting {BASE_URL} (server must be running)")
    queue_html = http_get("/queue")
    print("   server reachable.")
    nonce = extract_nonce(queue_html)
    print(f"   nonce: {nonce[:8]}...")

    print("-> POST /queue/refresh hours=24")
    refreshed = http_post("/queue/refresh", {"nonce": nonce, "hours": "24"})
    pending_after_refresh = extract_pending_ids(refreshed)
    print(f"   {len(pending_after_refresh)} pending proposals after scan")

    if not pending_after_refresh:
        print(
            "   No pending proposals (likely: every recent sender already has "
            "a filter, was previously dismissed, or there's no recent inbox mail).\n"
            "   Skip this run."
        )
        return 0

    # Pick the LAST id (lowest recent_count) so we touch the least important sender
    test_id = pending_after_refresh[-1]
    print(f"-> Test target: proposal_id={test_id}")

    print(f"-> POST /queue/decide action=skip proposal_id={test_id}")
    decided = http_post(
        "/queue/decide",
        {
            "nonce": nonce,
            "proposal_id": str(test_id),
            "action": "skip",
            "label_name": "",
            "filter_query": "",
        },
    )
    pending_after_skip = extract_pending_ids(decided)
    if test_id in pending_after_skip:
        print(f"   FAIL: proposal_id={test_id} still in pending after skip")
        return 2
    print("   skip worked: proposal removed from queue")

    print(f"-> Cleaning up: deleting row id={test_id} from {DB_PATH.name}")
    cleanup_db_row(test_id)
    print("   cleanup done.")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
