"""Helpers for driving the dashboard in tests: CSRF tokens, staff logins, taxpayer sessions."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from silverfile.dashboard import web
from silverfile.security import auth, ratelimit

PASSWORD = "correct horse battery staple"
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
INTERRUPT_RE = re.compile(r'name="interrupt_id" value="([^"]*)"')
CODE_RE = re.compile(r'class="access-code"><code>([A-Z0-9-]+)</code>')


def token(html: str) -> str:
    m = TOKEN_RE.search(html)
    assert m, "no CSRF token on the page"
    return m.group(1)


def interrupt_id(html: str) -> str:
    m = INTERRUPT_RE.search(html)
    assert m, "no interrupt id on the page"
    return m.group(1)


def anon_token(client: TestClient) -> str:
    """The double-submit token for the forms used before login (set on any first response)."""
    if web.ANON_CSRF_COOKIE not in client.cookies:
        client.get("/healthz")
    return client.cookies[web.ANON_CSRF_COOKIE]


def make_staff(username: str, role: str) -> None:
    try:
        auth.create_staff(username, PASSWORD, role)
    except ValueError:
        pass  # already there in this data dir


def login(client: TestClient, username: str, role: str = "preparer") -> str:
    """Log `username` in (creating the account); returns the session's CSRF token from the list page."""
    make_staff(username, role)
    ratelimit.reset_all()
    r = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": anon_token(client), "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return token(client.get("/").text)


def staff_client(app, username: str, role: str = "preparer") -> tuple[TestClient, str]:
    c = TestClient(app)
    return c, login(c, username, role)


def start_taxpayer(client: TestClient) -> str:
    """Start a new return as a taxpayer; returns the access code shown once."""
    r = client.post("/start", data={"csrf_token": anon_token(client)})
    assert r.status_code == 200, r.text[:500]
    m = CODE_RE.search(r.text)
    assert m, "no access code on the page"
    return m.group(1)


def answer(client: TestClient, text: str):
    page = client.get("/t").text
    return client.post("/t/answer", data={"text": text, "csrf_token": token(page), "interrupt_id": interrupt_id(page)}, follow_redirects=False)
