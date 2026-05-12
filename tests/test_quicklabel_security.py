"""Security middleware tests: host validation, audit log, CSP."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# We can't import server.py at module level because it eagerly creates the
# Storage at the production data path. Instead we build a clean app per
# test using the same middleware stack pointed at a tmp DB.

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from quicklabel.middleware import (
    AuditLogMiddleware,
    CSPHeaderMiddleware,
    HostValidationMiddleware,
)
from quicklabel.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "sec_test.db")


def _make_app(storage: Storage, port: int = 8765) -> FastAPI:
    """Build a stripped-down app with the same middleware stack as
    production, pointed at a tmp Storage."""
    app = FastAPI()

    @app.get("/")
    def root(): return JSONResponse({"ok": True})

    @app.get("/whoami")
    def whoami(): return JSONResponse({"ok": True})

    # Same ordering as server.py: inner-first
    app.add_middleware(HostValidationMiddleware, port=port)
    app.add_middleware(AuditLogMiddleware, get_storage_fn=lambda: storage)
    app.add_middleware(CSPHeaderMiddleware)
    return app


# --------------------------- Host validation ---------------------------

def test_host_validation_accepts_loopback_ipv4(storage):
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "127.0.0.1:8765"})
    assert r.status_code == 200


def test_host_validation_accepts_localhost(storage):
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "localhost:8765"})
    assert r.status_code == 200


def test_host_validation_rejects_foreign_host(storage):
    """The DNS-rebinding attack: request lands on 127.0.0.1 but the
    Host header still says 'evil.com' because that's where the browser
    thinks it's connecting. We must reject it."""
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "evil.com:8765"})
    assert r.status_code == 421
    assert "invalid Host header" in r.text


def test_host_validation_rejects_evil_host_without_port(storage):
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "evil.com"})
    assert r.status_code == 421


def test_host_validation_rejects_empty_host(storage):
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": ""})
    assert r.status_code == 421


def test_host_validation_is_case_insensitive(storage):
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "LOCALHOST:8765"})
    assert r.status_code == 200


def test_host_validation_blocks_post_endpoints_too(storage):
    """Critical: the rebinding attack is most useful on POSTs (apply,
    decide, etc.). Make sure POSTs are blocked too."""
    app = _make_app(storage)

    @app.post("/danger")
    def danger(): return {"did": "something destructive"}

    client = TestClient(app)
    # legit POST works
    r = client.post("/danger", headers={"host": "127.0.0.1:8765"})
    assert r.status_code == 200
    # rebound POST blocked
    r = client.post("/danger", headers={"host": "evil.com"})
    assert r.status_code == 421


# --------------------------- CSP headers ---------------------------

def test_csp_header_present_on_all_responses(storage):
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "127.0.0.1:8765"})
    assert r.status_code == 200
    assert "Content-Security-Policy" in r.headers
    csp = r.headers["Content-Security-Policy"]
    # Critical directives present
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp


def test_csp_present_even_on_blocked_requests(storage):
    """Defense in depth: don't strip the header just because the request
    is being rejected. Browser still benefits."""
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "evil.com"})
    assert r.status_code == 421
    assert "Content-Security-Policy" in r.headers


def test_extra_security_headers_present(storage):
    client = TestClient(_make_app(storage))
    r = client.get("/", headers={"host": "127.0.0.1:8765"})
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy") == "no-referrer"


# --------------------------- Audit log ---------------------------

def test_audit_log_records_legit_requests(storage):
    client = TestClient(_make_app(storage))
    client.get("/", headers={"host": "127.0.0.1:8765"})
    client.get("/whoami", headers={"host": "127.0.0.1:8765"})
    entries = storage.get_audit_log()
    paths = [e.path for e in entries]
    assert "/" in paths
    assert "/whoami" in paths


def test_audit_log_records_blocked_requests_with_blocked_flag(storage):
    """The whole point of audit logging is that you can spot DNS-rebinding
    attempts. Blocked requests must be in the log AND flagged."""
    client = TestClient(_make_app(storage))
    client.get("/", headers={"host": "evil.com"})
    entries = storage.get_audit_log()
    blocked = [e for e in entries if e.blocked]
    assert len(blocked) >= 1
    assert blocked[0].host == "evil.com"
    assert blocked[0].status == 421


def test_audit_log_captures_host_and_origin(storage):
    client = TestClient(_make_app(storage))
    client.get("/", headers={
        "host": "127.0.0.1:8765",
        "origin": "https://attacker.example.com",
        "referer": "https://attacker.example.com/page",
    })
    entries = storage.get_audit_log()
    e = entries[0]
    assert e.host == "127.0.0.1:8765"
    assert e.origin == "https://attacker.example.com"
    assert e.referer == "https://attacker.example.com/page"


def test_audit_log_returns_newest_first(storage):
    client = TestClient(_make_app(storage))
    for path in ["/", "/whoami", "/", "/whoami"]:
        client.get(path, headers={"host": "127.0.0.1:8765"})
    entries = storage.get_audit_log(limit=10)
    # IDs strictly decreasing
    ids = [e.id for e in entries]
    assert ids == sorted(ids, reverse=True)
