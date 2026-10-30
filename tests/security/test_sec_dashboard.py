"""Unauthorized dashboard access.

Who can reach what: nobody without a login, a taxpayer only their own
conversation, a clerk no draft, a preparer no security log. Session ids and
access codes are unguessable and never stored raw; an SSN, a name or a return
id opens nothing; forms need a CSRF token; displayed values are escaped.
"""
from __future__ import annotations

import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from conftest import CASE1, CASE2, load_intake
from silverfile import service
from silverfile.dashboard import web as webmod
from silverfile.security import audit, auth, ratelimit
from webhelpers import CODE_RE, PASSWORD, anon_token, answer, interrupt_id, login, make_staff, start_taxpayer, token

SYSTEM = auth.system("test")


def _prefilled(intake, docs=None) -> str:
    rid = service.create_return(docs or [], intake, actor=SYSTEM)
    service.run(rid)
    return rid


def _hitl_return() -> str:
    """A prefilled return waiting on a person (spouse died: filing-status escalation)."""
    rid = _prefilled(load_intake(CASE2), service.fixture_documents("case2_hitl"))
    assert service.pending(rid)["type"] == "escalation"
    return rid


STAFF_GETS = ["/", "/returns/{rid}", "/returns/{rid}/form", "/returns/{rid}/form.pdf", "/returns/{rid}/state.json", "/returns/{rid}/pending.json", "/audit"]
STAFF_POSTS = ["/returns/new", "/returns/{rid}/resolve", "/returns/{rid}/run", "/returns/{rid}/access-code", "/returns/{rid}/delete"]


def test_nothing_is_visible_without_logging_in(web):
    rid = _hitl_return()
    c = TestClient(web)
    for path in STAFF_GETS:
        r = c.get(path.format(rid=rid), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    for path in STAFF_POSTS:
        r = c.post(path.format(rid=rid), data={"action": "halt", "csrf_token": anon_token(c)}, follow_redirects=False)
        assert r.status_code in (401, 403, 422), (path, r.status_code)
    assert c.get("/t", follow_redirects=False).headers["location"] == "/start"
    assert service.pending(rid)["type"] == "escalation"  # nothing changed


def test_a_taxpayer_session_opens_no_staff_page(web):
    rid = _hitl_return()
    tp = TestClient(web)
    start_taxpayer(tp)
    assert tp.get("/", follow_redirects=False).headers["location"] == "/t"  # the front door sends a taxpayer to their own page
    for path in STAFF_GETS[1:]:
        r = tp.get(path.format(rid=rid), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_taxpayers_only_ever_see_their_own_return(web):
    a, b = TestClient(web), TestClient(web)
    start_taxpayer(a)
    start_taxpayer(b)
    assert answer(a, "yes it's me").status_code == 303
    assert answer(a, "Alice Avery").status_code == 303
    a_page, b_page = a.get("/t").text, b.get("/t").text
    assert "Alice Avery" in a_page and "Alice" not in b_page
    # A's form cannot answer B's question: the interrupt id is checked against A's own return
    r = a.post("/t/answer", data={"text": "Mallory", "csrf_token": token(a_page), "interrupt_id": interrupt_id(b_page)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/t"
    assert audit.recent(1, kind="stale_submission")[0]["return_id"] == auth.load_session(a.cookies[webmod.TAXPAYER_COOKIE], "taxpayer").subject
    rid_b = auth.load_session(b.cookies[webmod.TAXPAYER_COOKIE], "taxpayer").subject
    assert "Mallory" not in str(service.get_state(rid_b)["messages"])
    # there is no taxpayer route that takes a return id at all
    assert not [r.path for r in web.routes if r.path.startswith("/t") and "{" in r.path]


def test_a_double_submitted_answer_is_refused_and_lands_on_the_current_question(web):
    c = TestClient(web)
    start_taxpayer(c)
    rid = auth.load_session(c.cookies[webmod.TAXPAYER_COOKIE], "taxpayer").subject
    page = c.get("/t").text
    form = {"text": "yes it's me", "csrf_token": token(page), "interrupt_id": interrupt_id(page)}
    assert c.post("/t/answer", data=form, follow_redirects=False).status_code == 303
    asked = service.pending(rid)
    # the second click on Send carries the same, now answered, question
    again = c.post("/t/answer", data={**form, "text": "Mallory"}, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/t"
    assert service.pending(rid) == asked and "Mallory" not in str(service.get_state(rid)["messages"])
    assert audit.recent(1, kind="stale_submission")[0]["return_id"] == rid


@pytest.mark.parametrize("guess", ["912-34-5678", "Eleanor Whitfield", "ret-00000000", "", "AAAA-AAAA-AAAA-AAAA-AAAA-AAAA"])
def test_an_ssn_a_name_or_an_id_opens_nothing(web, guess):
    _prefilled(load_intake(CASE1), service.fixture_documents("case1_clean"))
    c = TestClient(web)
    r = c.post("/resume", data={"code": guess, "csrf_token": anon_token(c)}, follow_redirects=False)
    assert r.status_code == 401 and webmod.TAXPAYER_COOKIE not in c.cookies


def test_guessing_access_codes_is_rate_limited(web):
    c = TestClient(web)
    codes = [c.post("/resume", data={"code": f"XXXX-XXXX-XXXX-XXXX-XXXX-XXX{i}", "csrf_token": anon_token(c)}).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    assert audit.recent(1, kind="rate_limited")


def test_ids_codes_and_tokens_are_unguessable_and_not_stored_raw(web, data_dir):
    ids = {service.new_return_id() for _ in range(200)}
    assert len(ids) == 200 and all(re.fullmatch(r"ret-[0-9a-f]{32}", i) for i in ids)
    codes = {auth.issue_access_code(f"ret-{i:032x}") for i in range(50)}
    assert len(codes) == 50 and all(re.fullmatch(r"([A-HJ-NP-TV-Z2-9]{4}-){5}[A-HJ-NP-TV-Z2-9]{4}", c) for c in codes)
    c = TestClient(web)
    code = start_taxpayer(c)
    login(c, "pat")
    raw = (data_dir / "auth.sqlite").read_bytes()
    for secret in (c.cookies[webmod.STAFF_COOKIE], c.cookies[webmod.TAXPAYER_COOKIE], code.replace("-", "")):
        assert len(secret) >= 24 and secret.encode() not in raw


def test_roles_decide_what_staff_see(web):
    rid = _hitl_return()
    clerk, _ = TestClient(web), None
    csrf_clerk = login(clerk, "cleo", "clerk")
    listing = clerk.get("/")
    assert listing.status_code == 200 and "Harold" in listing.text  # who and where...
    assert "Waiting on a Kushim employee" not in listing.text  # ...but no summary of the case
    for path in ("/returns/{rid}", "/returns/{rid}/form", "/returns/{rid}/state.json", "/audit"):
        assert clerk.get(path.format(rid=rid)).status_code == 403, path
    r = clerk.post(f"/returns/{rid}/resolve", data={"action": "halt", "csrf_token": csrf_clerk})
    assert r.status_code == 403 and service.pending(rid)["type"] == "escalation"

    prep = TestClient(web)
    csrf_prep = login(prep, "pat", "preparer")
    assert prep.get(f"/returns/{rid}").status_code == 200 and prep.get(f"/returns/{rid}/form").status_code == 200
    assert prep.get("/audit").status_code == 403
    assert prep.post(f"/returns/{rid}/delete", data={"csrf_token": csrf_prep}).status_code == 403
    admin = TestClient(web)
    login(admin, "ada", "admin")
    assert admin.get("/audit").status_code == 200
    assert {e["kind"] for e in audit.recent(50, kind="access_denied")} == {"access_denied"}


def test_forms_without_a_valid_csrf_token_or_from_another_site_are_refused(web):
    rid = _hitl_return()
    c = TestClient(web)
    csrf = login(c, "pat")
    page = c.get(f"/returns/{rid}").text
    base = {"action": "halt", "interrupt_id": interrupt_id(page)}
    assert c.post(f"/returns/{rid}/resolve", data=base).status_code == 403
    assert c.post(f"/returns/{rid}/resolve", data={**base, "csrf_token": "forged"}).status_code == 403
    assert c.post(f"/returns/{rid}/resolve", data={**base, "csrf_token": csrf}, headers={"origin": "https://evil.example"}).status_code == 403
    assert service.pending(rid)["type"] == "escalation"
    assert len(audit.recent(10, kind="csrf_failed")) == 3
    # login itself needs the double-submit token
    other = TestClient(web)
    make_staff("pat", "preparer")
    assert other.post("/login", data={"username": "pat", "password": PASSWORD}).status_code == 403


def test_the_resolver_is_the_logged_in_preparer_not_a_form_field(web):
    rid = _hitl_return()
    c = TestClient(web)
    login(c, "pat")
    page = c.get(f"/returns/{rid}").text
    r = c.post(f"/returns/{rid}/resolve", data={"action": "continue", "filing_status": "mfj", "resolved_by": "mallory", "note": "decided", "csrf_token": token(page), "interrupt_id": interrupt_id(page)}, follow_redirects=False)
    assert r.status_code == 303
    esc = [e for e in service.get_state(rid)["escalations"] if e["status"] == "resolved"][0]
    assert esc["resolved_by"] == "pat" and esc["resolution"]["resolved_by_role"] == "preparer" and esc["resolved_at"]
    # a replay of the same form is refused
    again = c.post(f"/returns/{rid}/resolve", data={"action": "halt", "csrf_token": token(page), "interrupt_id": interrupt_id(page)})
    assert again.status_code in (400, 409)


def test_staff_cannot_answer_for_the_taxpayer(web):
    rid = service.create_return([], None, actor=SYSTEM)
    service.run(rid)
    with pytest.raises(service.Forbidden):
        service.answer(rid, "yes", actor=auth.Principal(kind="staff", subject="pat", role="admin"))
    assert not [r.path for r in web.routes if r.path.startswith("/returns") and r.path.endswith(("/answer", "/upload"))]


def test_logins_lock_after_repeated_failures_and_logout_ends_the_session(web):
    make_staff("pat", "preparer")
    c = TestClient(web)
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert c.post("/login", data={"username": "pat", "password": "wrong password!", "csrf_token": anon_token(c)}).status_code == 401
    assert c.post("/login", data={"username": "pat", "password": PASSWORD, "csrf_token": anon_token(c)}).status_code == 401  # locked
    assert audit.recent(1, kind="login_locked")
    with sqlite3.connect(auth.settings().auth_db) as db:
        db.execute("UPDATE staff SET locked_until = 0")
    csrf = login(c, "pat")
    cookie = c.cookies[webmod.STAFF_COOKIE]
    c.post("/logout", data={"csrf_token": csrf})
    replay = TestClient(web)
    replay.cookies.set(webmod.STAFF_COOKIE, cookie)
    assert replay.get("/", follow_redirects=False).headers["location"] == "/login"


def test_sessions_expire_when_idle(web, monkeypatch):
    c = TestClient(web)
    login(c, "pat")
    assert c.get("/").status_code == 200
    real = time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + auth.STAFF_IDLE_SECONDS + 5)
    assert c.get("/", follow_redirects=False).status_code == 303


def test_displayed_values_are_escaped_and_scripts_need_the_nonce(web):
    xss = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    intake = load_intake(CASE1)
    intake["taxpayer"]["first_name"] = "Eve" + xss
    intake["refund_account_description"] = xss
    docs = [{"id": "w2-xss", "kind": "W-2", "payload": {"kind": "W-2", "wages": 18400, "federal_withheld": 1210, "employer_name": "Library" + xss, "employee_name": "Eve Whitfield", "tax_year": "2025"}, "declared_by": "fixture", "uploaded_at": "2026-01-01T00:00:00+00:00"}]
    rid = _prefilled(intake, docs + service.fixture_documents("case1_clean")[1:])
    c = TestClient(web)
    login(c, "pat")
    for path in ("/", f"/returns/{rid}", f"/returns/{rid}/form"):
        r = c.get(path)
        assert r.status_code == 200
        assert "<script>alert(1)" not in r.text and "<img src=x" not in r.text, path
        assert "&lt;script&gt;alert(1)" in r.text or "&lt;img" in r.text, path
        csp = r.headers["content-security-policy"]
        assert "script-src 'nonce-" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
        for tag in re.findall(r"<script[^>]*>", r.text):
            assert 'nonce="' in tag, (path, tag)


def test_unknown_and_malformed_return_ids_look_the_same(web):
    c = TestClient(web)
    login(c, "pat")
    for rid in ("ret-" + "0" * 32, "../../etc/passwd", "ret-<script>"):
        r = c.get(f"/returns/{rid}")
        assert r.status_code == 404 and "<script>" not in r.text
