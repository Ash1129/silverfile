"""The key gate: a shared copy of the dashboard that runs the live models on
each visitor's own Anthropic key.

With SILVERFILE_REQUIRE_KEY=1 and no ANTHROPIC_API_KEY in the environment,
every page except the key page itself asks the visitor for a key first. The
key is checked with one cheap API call, kept in this process's memory under a
random session id (the cookie carries only the id), used through
llm.request_key for that visitor's requests, and dropped after TTL seconds or
when they click "Forget my key". It is never written to disk or to the logs
and never appears in a page. Each visitor's runs are billed to their own key.

Locally, with ANTHROPIC_API_KEY set or SILVERFILE_REQUIRE_KEY unset, the gate
is off and nothing here runs.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .. import llm
from ..config import settings

try:  # the error the SDK raises when a key stops working mid-run; the app maps it back to the key page
    from anthropic import AuthenticationError as AuthFailed
except ImportError:  # pragma: no cover - the SDK is a hard requirement, this keeps import order honest
    class AuthFailed(Exception):  # type: ignore[no-redef]
        pass

SESSION_COOKIE = "sf_key_session"
TTL_SECONDS = 12 * 3600
EXEMPT_PATHS = {"/key", "/key/forget", "/favicon.ico"}
EXEMPT_PREFIXES = ("/static/",)
KEY_PREFIX = "sk-ant-"

_sessions: dict[str, tuple[str, float]] = {}  # sid -> (api key, expiry)
_lock = threading.Lock()


def gate_enabled() -> bool:
    wanted = os.environ.get("SILVERFILE_REQUIRE_KEY", "0").strip().lower() in ("1", "true", "on", "yes")
    return wanted and not settings().api_key


def _purge(now: float) -> None:
    dead = [sid for sid, (_, exp) in _sessions.items() if exp <= now]
    for sid in dead:
        llm.forget_client(_sessions.pop(sid)[0])


def session_key(sid: str | None) -> str | None:
    if not sid:
        return None
    with _lock:
        now = time.time()
        _purge(now)
        entry = _sessions.get(sid)
        if entry is None:
            return None
        key, _ = entry
        _sessions[sid] = (key, now + TTL_SECONDS)  # sliding expiry
        return key


def start_session(key: str) -> str:
    sid = secrets.token_urlsafe(32)
    with _lock:
        _purge(time.time())
        _sessions[sid] = (key, time.time() + TTL_SECONDS)
    return sid


def end_session(sid: str | None) -> None:
    if not sid:
        return
    with _lock:
        entry = _sessions.pop(sid, None)
    if entry:
        llm.forget_client(entry[0])


def session_count() -> int:
    with _lock:
        _purge(time.time())
        return len(_sessions)


def validate_key(key: str) -> str:
    """Return the cleaned key, or raise ValueError with a message safe to show."""
    key = (key or "").strip()
    if not key:
        raise ValueError("Paste an Anthropic API key to continue.")
    if not key.startswith(KEY_PREFIX) or len(key) < 40 or any(c.isspace() for c in key):
        raise ValueError("That does not look like an Anthropic API key (they start with sk-ant-).")
    import anthropic

    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        raise ValueError("Anthropic rejected that key. Check it in the console and try again.") from None
    except anthropic.PermissionDeniedError:
        raise ValueError("That key is not allowed to use the API (check its permissions in the console).") from None
    except anthropic.APIConnectionError:
        raise ValueError("Could not reach Anthropic to check the key. Try again in a moment.") from None
    except anthropic.APIStatusError as e:
        raise ValueError(f"Anthropic returned an error while checking the key ({e.status_code}). Try again.") from None
    return key


def is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def set_session_cookie(response: Response, request: Request, sid: str) -> None:
    response.set_cookie(SESSION_COOKIE, sid, max_age=TTL_SECONDS, httponly=True, samesite="lax", secure=secure_cookie(request), path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


class KeyGate:
    """Pure ASGI middleware, so the context variable it sets is visible to the endpoint and its threads."""

    def __init__(self, app: Any, render_gate: Callable[[Request], Response]) -> None:
        self.app = app
        self.render_gate = render_gate

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not gate_enabled() or is_exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        key = session_key(request.cookies.get(SESSION_COOKIE))
        if key is None:
            if scope.get("method") in ("GET", "HEAD"):  # HEAD too, so a health check sees 200
                response = self.render_gate(request)
            else:
                response = HTMLResponse("<p>Enter your API key first: <a href='/key'>/key</a></p>", status_code=403)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["key_session"] = True
        token = llm.request_key.set(key)
        try:
            await self.app(scope, receive, send)
        finally:
            llm.request_key.reset(token)
