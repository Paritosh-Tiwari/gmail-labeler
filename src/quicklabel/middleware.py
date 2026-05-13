"""ASGI middleware for QuickLabel: host validation, audit log, CSP.

The host validation is the security-critical piece — it defeats DNS
rebinding attacks against 127.0.0.1:8765, which would otherwise let any
website the user visits read/write their Gmail through this server.

Audit log captures method/path/Host/status for every request (including
rejected ones) so the user can spot weird traffic.

CSP is defense in depth — Jinja auto-escapes templates, but a strong CSP
header gives a second line of defense against any future XSS.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response


def _allowed_hosts(port: int) -> set[str]:
    """The Host header values we accept. Browsers send the original
    hostname (not the resolved IP), so DNS rebinding attempts arrive
    with a foreign Host like 'evil.com:8765' or 'evil.com' and get
    rejected here even though the request landed on 127.0.0.1.

    This allow-list MUST stay in sync with `settings.HOST`. The current
    bind is loopback-only (127.0.0.1) — if HOST is ever widened to
    0.0.0.0 or a LAN address, every external Host that can reach the
    socket also needs to be added here, or the rebinding defense
    silently breaks.
    """
    return {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
        # Without ports too, in case a reverse proxy rewrites
        "127.0.0.1",
        "localhost",
        "[::1]",
    }


class HostValidationMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Host header isn't a localhost variant.

    Defeats DNS-rebinding attacks: even if attacker.com's DNS rebinds
    to 127.0.0.1, the browser still sends 'attacker.com' (or whatever
    the original hostname was) as the Host header.
    """

    def __init__(self, app, port: int):
        super().__init__(app)
        self._allowed = _allowed_hosts(port)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = (request.headers.get("host") or "").lower().strip()
        if host not in self._allowed:
            # 421 Misdirected Request — semantically right for "you sent
            # this to a Host I don't serve"
            return PlainTextResponse(
                f"Forbidden: invalid Host header {host!r}. "
                f"QuickLabel only accepts requests to localhost.",
                status_code=421,
            )
        return await call_next(request)


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Record every request (method, path, Host, status, Origin, Referer)
    to the audit_log table. Includes blocked requests so the user can
    see DNS-rebinding attempts that HostValidationMiddleware rejected.

    The storage write is fail-soft (record_audit catches exceptions) so
    audit logging can never break the request flow.
    """

    def __init__(self, app, get_storage_fn: Callable):
        super().__init__(app)
        self._get_storage = get_storage_fn

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        try:
            self._get_storage().record_audit(
                method=request.method,
                path=request.url.path,
                host=request.headers.get("host"),
                status=response.status_code,
                origin=request.headers.get("origin"),
                referer=request.headers.get("referer"),
                blocked=(response.status_code == 421),
            )
        except Exception:
            pass
        return response


class SetupRedirectMiddleware(BaseHTTPMiddleware):
    """If QuickLabel isn't fully OAuth-set-up yet, redirect non-setup
    requests to /setup. This stops the rest of the app from blowing up
    with FileNotFoundError on credentials.json or RuntimeError on a
    missing token, and routes the user straight to the wizard.

    Bypasses /setup* (the wizard itself) and /healthz (the install
    script polls it to know when the server is up)."""

    def __init__(self, app, is_ready_fn: Callable[[], bool]):
        super().__init__(app)
        self._is_ready = is_ready_fn

    @staticmethod
    def _bypass(path: str) -> bool:
        if path == "/healthz":
            return True
        if path == "/setup" or path.startswith("/setup/"):
            return True
        return False

    async def dispatch(self, request: Request, call_next):
        if self._bypass(request.url.path):
            return await call_next(request)
        if self._is_ready():
            return await call_next(request)
        return RedirectResponse(url="/setup", status_code=303)


class CSPHeaderMiddleware(BaseHTTPMiddleware):
    """Add Content-Security-Policy + a few other defense-in-depth headers
    to every response. We keep 'unsafe-inline' for script/style because
    the templates use inline event handlers and style attributes;
    removing those would be a bigger refactor. Even with 'unsafe-inline'
    the CSP still blocks loading scripts from external origins, which
    is the most common XSS exfiltration path."""

    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", self._CSP)
        # Belt-and-suspenders against a few other classes
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response
