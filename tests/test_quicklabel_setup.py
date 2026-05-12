"""Tests for the OAuth setup wizard: state detection and redirect middleware."""
from __future__ import annotations

import json
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from quicklabel import auth as auth_mod
from quicklabel.auth import SetupState, setup_state
from quicklabel.middleware import SetupRedirectMiddleware


# --------------------------- fixtures ---------------------------

class _MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError as e:
            raise keyring.errors.PasswordDeleteError("not found") from e


@pytest.fixture(autouse=True)
def _isolated_keyring():
    saved = keyring.get_keyring()
    keyring.set_keyring(_MemoryKeyring())
    yield
    keyring.set_keyring(saved)


@pytest.fixture
def tmp_creds(tmp_path: Path, monkeypatch):
    """Point auth.CREDENTIALS_PATH at a temp file so we can write/delete
    freely without touching the real one."""
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", p)
    monkeypatch.setattr(auth_mod, "QUICKLABEL_TOKEN_PATH", tmp_path / "legacy_token.json")
    return p


def _valid_desktop_creds() -> str:
    return json.dumps({
        "installed": {
            "client_id": "fake-id.apps.googleusercontent.com",
            "client_secret": "fake-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    })


# --------------------------- setup_state ---------------------------

def test_setup_state_no_credentials_file(tmp_creds: Path):
    assert not tmp_creds.exists()
    assert setup_state() == SetupState.NEEDS_CREDENTIALS


def test_setup_state_corrupt_credentials_file(tmp_creds: Path):
    tmp_creds.write_text("this is not json {", encoding="utf-8")
    assert setup_state() == SetupState.NEEDS_CREDENTIALS


def test_setup_state_web_app_credentials_treated_as_missing(tmp_creds: Path):
    """A 'Web application' OAuth client has 'web' at top level instead
    of 'installed'. QuickLabel needs Desktop -- treat web-app as missing
    so the wizard tells the user to re-download."""
    tmp_creds.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
    assert setup_state() == SetupState.NEEDS_CREDENTIALS


def test_setup_state_credentials_only_no_token(tmp_creds: Path):
    tmp_creds.write_text(_valid_desktop_creds(), encoding="utf-8")
    assert setup_state() == SetupState.NEEDS_AUTHORIZATION


def test_setup_state_credentials_and_token(tmp_creds: Path):
    from quicklabel.token_store import save_token
    tmp_creds.write_text(_valid_desktop_creds(), encoding="utf-8")
    save_token('{"refresh_token": "abc"}')
    assert setup_state() == SetupState.READY


# --------------------------- SetupRedirectMiddleware ---------------------------

def _make_app(is_ready: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/")
    def root(): return JSONResponse({"page": "landing"})

    @app.get("/queue")
    def queue(): return JSONResponse({"page": "queue"})

    @app.get("/setup")
    def setup(): return JSONResponse({"page": "setup"})

    @app.get("/setup/credentials")
    def setup_creds(): return JSONResponse({"page": "setup_creds"})

    @app.get("/healthz")
    def healthz(): return JSONResponse({"ok": True})

    app.add_middleware(SetupRedirectMiddleware, is_ready_fn=lambda: is_ready)
    return app


def test_redirect_when_not_ready_for_landing():
    client = TestClient(_make_app(is_ready=False))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_redirect_when_not_ready_for_queue():
    client = TestClient(_make_app(is_ready=False))
    r = client.get("/queue", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_setup_path_bypassed_even_when_not_ready():
    client = TestClient(_make_app(is_ready=False))
    r = client.get("/setup")
    assert r.status_code == 200
    assert r.json() == {"page": "setup"}


def test_setup_subpath_bypassed_even_when_not_ready():
    client = TestClient(_make_app(is_ready=False))
    r = client.get("/setup/credentials")
    assert r.status_code == 200
    assert r.json() == {"page": "setup_creds"}


def test_healthz_bypassed_even_when_not_ready():
    """The install script polls /healthz to know when the server is up;
    must NOT redirect or it'll never see a 200."""
    client = TestClient(_make_app(is_ready=False))
    r = client.get("/healthz")
    assert r.status_code == 200


def test_no_redirect_when_ready():
    client = TestClient(_make_app(is_ready=True))
    for path in ("/", "/queue", "/setup", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} should not redirect when READY"


def test_bypass_does_not_match_setupx_prefix():
    """A path like /setupxyz must NOT match /setup* bypass -- exact
    word boundary. (The middleware uses startswith /setup/, not /setup.)"""
    app = FastAPI()

    @app.get("/setupxyz")
    def x(): return JSONResponse({"page": "x"})

    app.add_middleware(SetupRedirectMiddleware, is_ready_fn=lambda: False)
    client = TestClient(app)
    r = client.get("/setupxyz", follow_redirects=False)
    assert r.status_code == 303
