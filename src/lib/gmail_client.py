"""Gmail API client: OAuth, service builder, and retry wrapper.

Hard-constraint verification happens here:
- We only ever request `gmail.modify` (config.GMAIL_SCOPES).
- After OAuth completes, we verify the granted token's scopes match
  what we requested exactly. Any drift causes RuntimeError.
"""
from __future__ import annotations

import time
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import CREDENTIALS_PATH, TOKEN_PATH, GMAIL_SCOPES


def _verify_scopes(creds: Credentials) -> None:
    granted = set(creds.scopes or [])
    requested = set(GMAIL_SCOPES)
    if granted != requested:
        raise RuntimeError(
            f"OAuth scope mismatch — refusing to proceed (Hard Constraint 1).\n"
            f"  Requested: {sorted(requested)}\n"
            f"  Granted:   {sorted(granted)}\n"
            f"Delete token.json and re-run to redo the OAuth flow."
        )


def get_credentials() -> Credentials:
    """Load cached creds, refresh if expired, or run interactive OAuth flow."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            "Complete docs/google-cloud-setup.md first."
        )

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), GMAIL_SCOPES
            )
            # port=0 -> picks a random free port; opens system default browser
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    _verify_scopes(creds)
    return creds


def build_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _execute_with_backoff(call, max_attempts: int = 6, base_delay: float = 1.0):
    """Execute a Gmail API call with exponential backoff on 429 / 5xx."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return call.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                time.sleep(base_delay * (2 ** attempt))
                last_exc = e
                continue
            raise
    if last_exc:
        raise last_exc


def get_profile(service) -> dict:
    return _execute_with_backoff(service.users().getProfile(userId="me"))


def list_labels(service) -> list[dict]:
    """Return all labels in the user's mailbox (system + user-created)."""
    resp = _execute_with_backoff(service.users().labels().list(userId="me"))
    return resp.get("labels", [])


def list_all_message_ids(service) -> Iterator[str]:
    """Yield every message ID in the user's mailbox, paginating server-side."""
    page_token = None
    while True:
        req = service.users().messages().list(
            userId="me",
            maxResults=500,
            pageToken=page_token,
            includeSpamTrash=True,
        )
        resp = _execute_with_backoff(req)
        for m in resp.get("messages", []):
            yield m["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def fetch_message(service, msg_id: str) -> dict:
    req = service.users().messages().get(userId="me", id=msg_id, format="full")
    return _execute_with_backoff(req)


def create_label(service, name: str, parent_visible: bool = True) -> dict:
    """Create a user label. Idempotent-ish — caller should check list_labels first."""
    body = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show" if parent_visible else "hide",
    }
    req = service.users().labels().create(userId="me", body=body)
    return _execute_with_backoff(req)


def batch_modify_labels(
    service,
    message_ids: list[str],
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
) -> None:
    """Add and/or remove labels on up to 1000 messages in one API call.

    Returns nothing on success. The Gmail API also returns nothing on
    success (HTTP 204). Raises on failure (after retry).
    """
    if not message_ids:
        return
    if len(message_ids) > 1000:
        raise ValueError(f"batch_modify_labels accepts <=1000 ids, got {len(message_ids)}")
    body: dict = {"ids": message_ids}
    if add_label_ids:
        body["addLabelIds"] = add_label_ids
    if remove_label_ids:
        body["removeLabelIds"] = remove_label_ids
    req = service.users().messages().batchModify(userId="me", body=body)
    _execute_with_backoff(req)


def search_message_ids(service, query: str, max_results: int = 500) -> list[str]:
    """Run a Gmail search and return matching message IDs (up to max_results)."""
    out: list[str] = []
    page_token = None
    while len(out) < max_results:
        req = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(500, max_results - len(out)),
            pageToken=page_token,
            includeSpamTrash=False,
        )
        resp = _execute_with_backoff(req)
        for m in resp.get("messages", []):
            out.append(m["id"])
            if len(out) >= max_results:
                break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def search_count(service, query: str, cap: int = 1000) -> int:
    """Return number of messages matching `query`. Caps at `cap`."""
    return len(search_message_ids(service, query, max_results=cap))


def list_filters(service) -> list[dict]:
    """Return all server-side filters configured on the account."""
    req = service.users().settings().filters().list(userId="me")
    resp = _execute_with_backoff(req)
    return resp.get("filter", [])


def create_filter(
    service,
    criteria: dict,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
) -> dict:
    """Create a Gmail server-side filter. `criteria` keys: from, to, subject, query, etc."""
    action: dict = {}
    if add_label_ids:
        action["addLabelIds"] = add_label_ids
    if remove_label_ids:
        action["removeLabelIds"] = remove_label_ids
    body = {"criteria": criteria, "action": action}
    req = service.users().settings().filters().create(userId="me", body=body)
    return _execute_with_backoff(req)


def delete_filter(service, filter_id: str) -> None:
    req = service.users().settings().filters().delete(userId="me", id=filter_id)
    _execute_with_backoff(req)
