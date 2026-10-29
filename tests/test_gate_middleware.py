"""The key middleware on its own (pure ASGI, no web framework needed): with no
server key it never blocks unless SILVERFILE_REQUIRE_KEY=1, and a visitor's
session key reaches the model door only inside their own request."""
from __future__ import annotations

import asyncio

import pytest
from starlette.responses import PlainTextResponse

from silverfile import llm
from silverfile.dashboard import gate

GOOD = "sk-ant-api03-" + "x" * 60


def _call(path: str = "/", method: str = "GET", cookie: str | None = None):
    """Drive KeyGate once. Returns (status, body, seen) where seen = [state flag, key seen by the app]."""
    seen: list = []

    async def inner(scope, receive, send):
        seen.append(scope.get("state", {}).get("key_session", False))
        seen.append(llm.current_key())
        await PlainTextResponse("app")(scope, receive, send)

    mw = gate.KeyGate(inner, render_gate=lambda request: PlainTextResponse("gate"))
    headers = [(b"cookie", f"{gate.SESSION_COOKIE}={cookie}".encode())] if cookie else []
    scope = {"type": "http", "method": method, "path": path, "headers": headers, "query_string": b"", "scheme": "http", "server": ("test", 80)}
    out: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] = out.get("body", b"") + msg.get("body", b"")

    asyncio.run(mw(scope, receive, send))
    return out["status"], out["body"].decode(), seen


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("SILVERFILE_REQUIRE_KEY", raising=False)
    gate._sessions.clear()
    yield
    gate._sessions.clear()


def test_offline_mode_blocks_nothing():
    status, body, seen = _call("/")
    assert (status, body) == (200, "app") and seen == [False, None]


def test_the_gate_still_blocks_when_required(monkeypatch):
    monkeypatch.setenv("SILVERFILE_REQUIRE_KEY", "1")
    assert _call("/")[:2] == (200, "gate")
    assert _call("/returns/x", method="POST")[0] == 403
    assert _call("/key")[:2] == (200, "app")  # the key page itself is exempt
    assert _call("/healthz")[:2] == (200, "app")


def test_a_session_key_reaches_only_that_request():
    sid = gate.start_session(GOOD)
    status, body, seen = _call("/", cookie=sid)
    assert (status, body) == (200, "app") and seen == [True, GOOD]
    assert llm.current_key() is None  # nothing leaks outside the request
    assert _call("/")[2] == [False, None]  # the next visitor has no key


def test_the_key_pages_see_the_session_too():
    sid = gate.start_session(GOOD)
    assert _call("/key", cookie=sid)[2] == [True, GOOD]
    assert _call("/key/forget", method="POST", cookie=sid)[2] == [True, GOOD]


def test_static_files_do_not_touch_sessions():
    sid = gate.start_session(GOOD)
    assert _call("/static/style.css", cookie=sid)[2] == [False, None]


def test_the_server_own_key_switches_everything_off(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-owner")
    monkeypatch.setenv("SILVERFILE_REQUIRE_KEY", "1")
    assert not gate.key_ui_enabled() and not gate.gate_enabled()
    sid = gate.start_session(GOOD)
    flag, key = _call("/", cookie=sid)[2]
    assert flag is False and key == "sk-ant-owner"  # the server's key is used, never the visitor's
