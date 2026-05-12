"""Tests for token_store: keyring-backed OAuth token persistence + the
one-shot legacy-file migration."""
from __future__ import annotations

from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest


class _MemoryKeyring(keyring.backend.KeyringBackend):
    """In-memory backend so tests don't pollute the real OS keyring."""
    priority = 1

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError as e:
            raise keyring.errors.PasswordDeleteError("not found") from e


@pytest.fixture(autouse=True)
def _memory_keyring():
    """Swap the real keyring for an in-memory one; restore afterward."""
    saved = keyring.get_keyring()
    keyring.set_keyring(_MemoryKeyring())
    yield
    keyring.set_keyring(saved)


def test_save_then_load_roundtrip():
    from quicklabel.token_store import load_token, save_token
    save_token('{"refresh_token": "abc"}')
    assert load_token() == '{"refresh_token": "abc"}'


def test_load_returns_none_when_unset():
    from quicklabel.token_store import load_token
    assert load_token() is None


def test_delete_then_load_returns_none():
    from quicklabel.token_store import delete_token, load_token, save_token
    save_token("payload")
    assert load_token() == "payload"
    delete_token()
    assert load_token() is None


def test_delete_when_unset_does_not_raise():
    from quicklabel.token_store import delete_token
    delete_token()  # nothing in store; must not raise


def test_legacy_file_migrated_on_first_load(tmp_path: Path):
    """An existing data/quicklabel_token.json gets sucked into the
    keyring on first load and the file is deleted."""
    from quicklabel.token_store import load_token
    legacy = tmp_path / "quicklabel_token.json"
    legacy.write_text('{"refresh_token": "from-disk"}', encoding="utf-8")

    result = load_token(legacy_file=legacy)
    assert result == '{"refresh_token": "from-disk"}'
    assert not legacy.exists(), "legacy file should be deleted after migration"

    # Subsequent calls (no legacy file) read from keyring
    assert load_token() == '{"refresh_token": "from-disk"}'


def test_legacy_migration_skipped_when_keyring_already_has_token(tmp_path: Path):
    """If the keyring already has a token, don't read or delete the
    legacy file (it might be stale, but the keyring is authoritative)."""
    from quicklabel.token_store import load_token, save_token
    save_token('{"refresh_token": "from-keyring"}')
    legacy = tmp_path / "quicklabel_token.json"
    legacy.write_text('{"refresh_token": "stale-disk"}', encoding="utf-8")

    result = load_token(legacy_file=legacy)
    assert result == '{"refresh_token": "from-keyring"}'
    assert legacy.exists(), "legacy file must NOT be deleted when keyring is authoritative"


def test_load_no_legacy_file_returns_none():
    from quicklabel.token_store import load_token
    assert load_token(legacy_file=Path("/does/not/exist.json")) is None


def test_save_overwrites_previous():
    from quicklabel.token_store import load_token, save_token
    save_token("v1")
    save_token("v2")
    assert load_token() == "v2"
