"""OAuth + service builder for QuickLabel.

Uses a SEPARATE token store from the parent project so that the parent's
narrow `gmail.modify` scope is preserved. QuickLabel needs the extra
`gmail.settings.basic` scope to create server-side filters.

The refresh token lives in the OS keyring (see token_store.py); the
legacy data/quicklabel_token.json file is migrated on first load.
"""
from __future__ import annotations

import json
from enum import Enum

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from lib.config import CREDENTIALS_PATH, PROJECT_ROOT
from lib.gmail_client import build_service as _build_service

from .token_store import load_token, save_token


class SetupState(Enum):
    """Where the user is in the one-time OAuth setup."""
    NEEDS_CREDENTIALS = "needs_credentials"      # no credentials.json (or malformed)
    NEEDS_AUTHORIZATION = "needs_authorization"  # have credentials.json, no token in keyring
    READY = "ready"                              # credentials + token both present

# Legacy on-disk path kept as the migration source only. New code never
# writes here; load_token() deletes it after migrating into the keyring.
QUICKLABEL_TOKEN_PATH = PROJECT_ROOT / "data" / "quicklabel_token.json"

# QuickLabel-specific scopes:
# - gmail.modify: read messages, add/remove labels, batchModify
# - gmail.settings.basic: create/list/delete server-side filters
QUICKLABEL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


def _verify_scopes(creds: Credentials) -> None:
    granted = set(creds.scopes or [])
    requested = set(QUICKLABEL_SCOPES)
    if granted != requested:
        raise RuntimeError(
            f"OAuth scope mismatch.\n"
            f"  Requested: {sorted(requested)}\n"
            f"  Granted:   {sorted(granted)}\n"
            f"Run `quicklabel reset-auth` and re-authorize."
        )


def setup_state() -> SetupState:
    """Inspect on-disk + keyring state to decide which wizard step is next.

    Cheap (no network calls). The /setup wizard polls this on every
    page load to know which section to highlight.
    """
    if not CREDENTIALS_PATH.exists():
        return SetupState.NEEDS_CREDENTIALS
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SetupState.NEEDS_CREDENTIALS
    # Desktop-app credentials JSON has top-level key "installed".
    # Web-app would be "web" -- not what we want, treat as missing so the
    # wizard prompts user to re-download with Application type = Desktop.
    if not isinstance(data.get("installed"), dict):
        return SetupState.NEEDS_CREDENTIALS

    if load_token(legacy_file=QUICKLABEL_TOKEN_PATH) is None:
        return SetupState.NEEDS_AUTHORIZATION
    return SetupState.READY


def authorize_interactive() -> Credentials:
    """Run the InstalledAppFlow OAuth flow and persist the token to keyring.

    Blocks while the user completes the consent in their browser. Caller
    is responsible for telling the user "this will open a browser window".
    """
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            "Upload it via /setup first."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH), QUICKLABEL_SCOPES
    )
    creds = flow.run_local_server(port=0)
    save_token(creds.to_json())
    _verify_scopes(creds)
    return creds


def get_credentials() -> Credentials:
    """Load cached creds, refresh if expired, or run interactive OAuth flow."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            "Complete docs/google-cloud-setup.md first."
        )

    creds = None
    token_json = load_token(legacy_file=QUICKLABEL_TOKEN_PATH)
    if token_json:
        creds = Credentials.from_authorized_user_info(
            json.loads(token_json), QUICKLABEL_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), QUICKLABEL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        save_token(creds.to_json())

    _verify_scopes(creds)
    return creds


def build_service(creds: Credentials):
    return _build_service(creds)
