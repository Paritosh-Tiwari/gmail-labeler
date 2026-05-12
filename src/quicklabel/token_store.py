"""Persistent OAuth refresh-token storage for QuickLabel.

Uses the OS keyring (Windows Credential Manager, macOS Keychain) so the
token doesn't sit in plaintext on disk. Compared to the previous
data/quicklabel_token.json file, this means the token won't be picked
up by:
  - accidental folder zips / shared archives
  - OneDrive / iCloud / Dropbox sync of the project folder
  - `git add .` followed by a public push
  - other user accounts on the same machine

One-shot migration: the first load_token() call after upgrading checks
for the legacy file, copies its contents into the keyring, and deletes
the file.
"""
from __future__ import annotations

from pathlib import Path

import keyring
import keyring.errors

_SERVICE = "quicklabel"
_USERNAME = "oauth-token"


def save_token(token_json: str) -> None:
    keyring.set_password(_SERVICE, _USERNAME, token_json)


def load_token(legacy_file: Path | None = None) -> str | None:
    """Return the stored token JSON or None.

    If `legacy_file` is provided and the keyring has no token but the
    file exists, the file's contents are migrated into the keyring and
    the file is deleted (one-shot upgrade migration).
    """
    existing = keyring.get_password(_SERVICE, _USERNAME)
    if existing is not None:
        return existing
    if legacy_file is not None and legacy_file.exists():
        token_json = legacy_file.read_text(encoding="utf-8")
        save_token(token_json)
        try:
            legacy_file.unlink()
        except OSError:
            pass
        return token_json
    return None


def delete_token() -> None:
    try:
        keyring.delete_password(_SERVICE, _USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass
