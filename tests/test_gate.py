"""Session API keys (dashboard/gate.py): the header "API key" control is available
whenever the server has no key of its own; the full-page gate is the opt-in
SILVERFILE_REQUIRE_KEY=1 version. The visitor's key is checked once, kept in
memory under a cookie-borne session id, used through llm.request_key for their
requests, and forgotten on request."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from silverfile import llm
from silverfile.dashboard import gate
from silverfile.dashboard.app import create_app
from silverfile.security import ratelimit
from webhelpers import anon_token, login, start_taxpayer

GOOD = "sk-ant-api03-" + "x" * 60


def _configure(tmp_path, monkeypatch, require_key: bool):
    """No env key, fresh data dir, and a fake key check (no network). Returns the list of keys checked."""
    monkeypatch.setenv("SILVERFILE_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    if require_key:
        monkeypatch.setenv("SILVERFILE_REQUIRE_KEY", "1")
    else:
        monkeypatch.delenv("SILVERFILE_REQUIRE_KEY", raising=False)
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
    return checked


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A shared-copy configuration with the full-page gate on (SILVERFILE_REQUIRE_KEY=1)."""
    checked = _configure(tmp_path, monkeypatch, require_key=True)
    with TestClient(create_app()) as client:
        yield client, checked


@pytest.fixture
def local(tmp_path, monkeypatch):
    """No server key and no gate: offline mode, with the header control available."""
    checked = _configure(tmp_path, monkeypatch, require_key=False)
    with TestClient(create_app()) as client:
        yield client, checked


def test_the_gate_is_off_without_the_require_key_setting(local):
    client, _ = local
    assert not gate.gate_enabled() and gate.key_ui_enabled()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"  # no key wall, straight to staff login
    login(client, "gate-local")
    r = client.get("/")
    assert r.status_code == 200 and "Start a return" in r.text
    assert "Offline mode" in r.text and 'name="api_key"' in r.text  # the header control, closed until clicked
    assert 'action="/key/forget"' not in r.text  # no forget button yet (the control's help text mentions the phrase)
    assert client.get("/key").status_code == 200  # the page behind the control also exists locally


def test_the_header_control_takes_a_key_without_the_gate(local):
    client, checked = local
    csrf = login(client, "hdr-staff")
    r = client.post("/key", data={"api_key": GOOD, "csrf_token": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and checked == [GOOD]
    sid = client.cookies.get(gate.SESSION_COOKIE)
    assert sid and GOOD not in sid and gate.session_key(sid) == GOOD
    html = client.get("/").text
    assert "Using your key" in html and "Forget my key" in html and "Offline mode" not in html
    assert GOOD not in html
    r = client.post("/key/forget", data={"csrf_token": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and gate.session_key(sid) is None
    assert "Offline mode" in client.get("/").text


def test_the_header_form_needs_the_csrf_token(local):
    client, checked = local
    login(client, "hdr-csrf")
    assert client.post("/key", data={"api_key": GOOD}).status_code == 403
    assert checked == [] and gate.SESSION_COOKIE not in client.cookies


def test_next_cannot_leave_the_site(local):
    client, _ = local
    csrf = login(client, "hdr-next")
    r = client.post("/key", data={"api_key": GOOD, "csrf_token": csrf, "next": "https://evil.example/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_a_bad_key_in_the_header_is_refused_and_shows_why(local):
    client, checked = local
    csrf = login(client, "hdr-bad")
    r = client.post("/key", data={"api_key": "hello", "csrf_token": csrf, "next": "/"})
    assert r.status_code == 400 and "start with sk-ant-" in r.text
    assert checked == [] and gate.SESSION_COOKIE not in client.cookies


def test_key_submissions_are_rate_limited(local):
    client, _ = local
    ratelimit.reset_all()
    for _ in range(10):
        assert client.post("/key", data={"api_key": "nope", "csrf_token": anon_token(client)}).status_code == 400
    r = client.post("/key", data={"api_key": "nope", "csrf_token": anon_token(client)})
    assert r.status_code == 429 and "Retry-After" in r.headers
    ratelimit.reset_all()


def test_taxpayer_pages_do_not_show_the_key_control(local):
    client, _ = local
    assert 'name="api_key"' not in client.get("/start").text
    start_taxpayer(client)
    page = client.get("/t").text
    assert 'name="api_key"' not in page and "Offline mode" not in page


def test_no_control_when_the_server_has_its_own_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SILVERFILE_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-owner")
    monkeypatch.delenv("SILVERFILE_SEED_DEMO", raising=False)
    assert not gate.key_ui_enabled()
    with TestClient(create_app()) as client:
        login(client, "hdr-owner")
        html = client.get("/").text
        assert 'name="api_key"' not in html and "Offline mode" not in html
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
