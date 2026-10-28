"""The key gate (dashboard/gate.py): off locally, on for shared copies; the
visitor's key is checked once, kept in memory under a cookie-borne session id,
used through llm.request_key for their requests, and forgotten on request."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from silverfile import llm
from silverfile.dashboard import gate
from silverfile.dashboard.app import create_app
from webhelpers import anon_token, login

GOOD = "sk-ant-api03-" + "x" * 60


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A shared-copy configuration: no env key, gate on, fresh data dir, no network for validation."""
    monkeypatch.setenv("SILVERFILE_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("SILVERFILE_REQUIRE_KEY", "1")
    monkeypatch.delenv("SILVERFILE_SEED_DEMO", raising=False)
    checked: list[str] = []

    def fake_validate(key: str) -> str:
        key = (key or "").strip()
        if not key.startswith(gate.KEY_PREFIX):
            raise ValueError("That does not look like an Anthropic API key (they start with sk-ant-).")
        if key.endswith("bad"):
            raise ValueError("Anthropic rejected that key. Check it in the console and try again.")
        checked.append(key)
        return key

    monkeypatch.setattr(gate, "validate_key", fake_validate)
    gate._sessions.clear()
    with TestClient(create_app()) as client:
        yield client, checked


def test_gate_is_off_locally(tmp_path, monkeypatch):
    monkeypatch.setenv("SILVERFILE_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("SILVERFILE_REQUIRE_KEY", raising=False)
    monkeypatch.delenv("SILVERFILE_SEED_DEMO", raising=False)
    assert not gate.gate_enabled()
    with TestClient(create_app()) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"  # no key page, straight to staff login
        login(client, "gate-local")
        r = client.get("/")
        assert r.status_code == 200 and "Start a return" in r.text and "api_key" not in r.text
        assert client.get("/key", follow_redirects=False).status_code == 303


def test_gate_is_off_when_the_server_has_its_own_key(monkeypatch):
    monkeypatch.setenv("SILVERFILE_REQUIRE_KEY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-owner")
    assert not gate.gate_enabled()


def test_every_page_asks_for_a_key_first(shared):
    client, _ = shared
    for path in ("/", "/returns/nope"):
        r = client.get(path)
        assert r.status_code == 200 and 'name="api_key"' in r.text and "Start a return" not in r.text
    assert client.get("/static/style.css").status_code == 200  # the page's own stylesheet is not gated
    assert client.head("/").status_code == 200  # a health check probe
    assert client.get("/favicon.ico").status_code == 404  # not served the key page
    assert client.post("/returns/new", data={}).status_code == 403


def test_bad_keys_are_refused_without_a_session(shared):
    client, checked = shared
    r = client.post("/key", data={"api_key": "hello", "csrf_token": anon_token(client)})
    assert r.status_code == 400 and "start with sk-ant-" in r.text
    r = client.post("/key", data={"api_key": GOOD + "bad", "csrf_token": anon_token(client)})
    assert r.status_code == 400 and "rejected" in r.text
    assert gate.SESSION_COOKIE not in client.cookies and checked == []


def test_a_good_key_opens_the_dashboard_and_can_be_forgotten(shared):
    client, checked = shared
    assert client.post("/key", data={"api_key": GOOD}).status_code == 403  # no CSRF token, no key
    assert checked == []
    r = client.post("/key", data={"api_key": "  " + GOOD + "  ", "csrf_token": anon_token(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert checked == [GOOD]
    sid = client.cookies.get(gate.SESSION_COOKIE)
    assert sid and GOOD not in sid and gate.session_key(sid) == GOOD
    csrf = login(client, "gate-shared")  # past the key gate, the dashboard still needs a staff login
    r = client.get("/")
    assert r.status_code == 200 and "Start a return" in r.text and "Forget my key" in r.text
    assert GOOD not in r.text
    r = client.post("/key/forget", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and gate.session_key(sid) is None
    assert 'name="api_key"' in client.get("/").text


def test_the_session_key_reaches_the_model_door(shared):
    """llm.available() / llm.current_key() see the visitor's key only inside their request."""
    client, _ = shared
    seen: list[str | None] = []
    app = client.app

    @app.get("/_probe")
    async def probe():
        from starlette.concurrency import run_in_threadpool

        seen.append(llm.current_key())
        seen.append(await run_in_threadpool(llm.current_key))  # the thread the graph runs on
        return {"available": llm.available()}

    assert client.get("/_probe").status_code == 200  # gated: the key page, not the probe
    assert seen == []
    client.post("/key", data={"api_key": GOOD, "csrf_token": anon_token(client)})
    assert client.get("/_probe").json() == {"available": True}
    assert seen == [GOOD, GOOD]
    assert llm.current_key() is None  # nothing leaks outside the request


def test_sessions_expire(monkeypatch):
    gate._sessions.clear()
    sid = gate.start_session(GOOD)
    assert gate.session_key(sid) == GOOD
    monkeypatch.setattr(gate.time, "time", lambda: 1e12)
    assert gate.session_key(sid) is None and gate.session_count() == 0
