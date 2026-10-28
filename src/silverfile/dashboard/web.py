"""Web-layer security for the dashboard: headers, CSRF, sessions in cookies,
and the request body cap. The rules about who may do what live in
security/auth.py and service.py; this module only carries them over HTTP.

- SecurityMiddleware (pure ASGI, outermost): a per-response CSP nonce, strict
  security headers, no caching of pages that hold PII, an anonymous CSRF
  cookie for the forms used before login, and a hard cap on request bodies
  (counted as they stream, so a missing Content-Length does not get around it).
- CSRF: every POST carries a csrf_token field. Signed-in forms use the token
  stored with the session; the login, start, resume and key forms use the
  double-submit cookie. A cross-site Origin is refused outright.
- Cookies: sf_staff and sf_taxpayer are separate (one browser can hold a staff
  and a taxpayer session in a demo without either seeing the other's pages),
  HttpOnly, SameSite=Strict, Secure behind HTTPS.
"""
from __future__ import annotations

import hmac
import re
import secrets
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from ..security import audit, auth, uploads
from ..security.auth import Principal

STAFF_COOKIE = "sf_staff"
TAXPAYER_COOKIE = "sf_taxpayer"
ANON_CSRF_COOKIE = "sf_csrf"
RETURN_ID_RE = re.compile(r"^(ret|demo)-[0-9a-f]{8,32}$")


class LoginRequired(Exception):
    def __init__(self, kind: str = "staff") -> None:
        super().__init__(kind)
        self.kind = kind


class CSRFFailed(Exception):
    pass


def max_body() -> int:
    return uploads.MAX_FILES * uploads.max_bytes() + 1024 * 1024


def secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- middleware
class SecurityMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        limit = max_body()
        try:
            declared = int(headers.get("content-length", "0") or 0)
        except ValueError:
            declared = limit + 1
        if declared > limit:
            await PlainTextResponse("Request too large.", status_code=413)(scope, receive, send)
            return
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get("cookie", ""))
        except Exception:
            pass
        existing = cookies[ANON_CSRF_COOKIE].value if ANON_CSRF_COOKIE in cookies else None
        anon = existing if existing and re.fullmatch(r"[A-Za-z0-9_-]{20,64}", existing) else secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(16)
        state = scope.setdefault("state", {})
        state["csp_nonce"], state["anon_csrf"], state["anon_csrf_from_cookie"] = nonce, anon, existing == anon
        is_https = scope.get("scheme") == "https" or headers.get("x-forwarded-proto", "").lower() == "https"
        path = scope.get("path", "")
        seen = 0

        async def capped_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge()
            return msg

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                h = list(message.get("headers") or [])
                add = {
                    "content-security-policy": f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                    "x-content-type-options": "nosniff",
                    "x-frame-options": "DENY",
                    "referrer-policy": "no-referrer",
                    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
                    "cross-origin-opener-policy": "same-origin",
                    "cross-origin-resource-policy": "same-origin",
                }
                if not path.startswith("/static/"):
                    add["cache-control"] = "no-store"
                if is_https:
                    add["strict-transport-security"] = "max-age=31536000; includeSubDomains"
                present = {k.decode("latin-1").lower() for k, _ in h}
                h += [(k.encode(), v.encode()) for k, v in add.items() if k not in present]
                if not state["anon_csrf_from_cookie"]:
                    flags = "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if is_https else "")
                    h.append((b"set-cookie", f"{ANON_CSRF_COOKIE}={anon}{flags}".encode()))
                message["headers"] = h
            await send(message)

        try:
            await self.app(scope, capped_receive, send_with_headers)
        except BodyTooLarge:
            await PlainTextResponse("Request too large.", status_code=413)(scope, receive, send)


class BodyTooLarge(Exception):
    pass


# ---------------------------------------------------------------- sessions in cookies
def staff(request: Request) -> Principal | None:
    return auth.load_session(request.cookies.get(STAFF_COOKIE), "staff")


def taxpayer(request: Request) -> Principal | None:
    return auth.load_session(request.cookies.get(TAXPAYER_COOKIE), "taxpayer")


def require_staff(request: Request, permission: str | None = None) -> Principal:
    p = staff(request)
    if p is None:
        raise LoginRequired("staff")
    if permission and not p.can(permission):
        audit.record("access_denied", actor=p.label, path=request.url.path, permission=permission)
        raise PermissionError("Your role does not allow that.")
    request.state.principal = p
    return p


def require_taxpayer(request: Request) -> Principal:
    p = taxpayer(request)
    if p is None:
        raise LoginRequired("taxpayer")
    request.state.principal = p
    return p


def set_cookie(response: Response, request: Request, name: str, token: str, max_age: int) -> None:
    response.set_cookie(name, token, max_age=max_age, httponly=True, samesite="strict", secure=secure(request), path="/")


def clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/")


def check_return_id(rid: str) -> str:
    if not RETURN_ID_RE.match(rid or ""):
        raise KeyError(rid[:40] if rid else "")
    return rid


def safe_next(url: str | None) -> str:
    """Only same-site relative paths (no open redirect)."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url or urlsplit(url).netloc:
        return "/"
    return url


# ---------------------------------------------------------------- CSRF
def csrf_for(request: Request) -> str:
    p = getattr(request.state, "principal", None)
    return p.csrf if p and p.csrf else request.state.anon_csrf


async def verify_csrf(request: Request, principal: Principal | None = None) -> None:
    origin = request.headers.get("origin")
    if origin and origin != "null":
        if urlsplit(origin).netloc != request.url.netloc:
            audit.record("csrf_failed", actor=principal.label if principal else None, path=request.url.path, reason="cross-site origin")
            raise CSRFFailed()
    form = await request.form()
    sent = str(form.get("csrf_token") or "")
    if principal is not None and principal.csrf:
        expected = principal.csrf
    else:
        expected = request.state.anon_csrf if request.state.anon_csrf_from_cookie else ""
    if not expected or not hmac.compare_digest(sent, expected):
        audit.record("csrf_failed", actor=principal.label if principal else None, path=request.url.path, reason="token missing or wrong")
        raise CSRFFailed()
