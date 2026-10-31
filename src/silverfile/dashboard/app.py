"""The Kushim dashboard and the taxpayer's conversation, behind separate doors.

Staff (Kushim employees) log in and see what their role allows: a clerk opens
returns and hands out access codes; a preparer reads drafts and resolves
escalations; an admin also reads the security log and deletes returns.
Taxpayers never log in with an SSN: starting a return gives them an access
code, and their session opens only their own return, only its conversation.
A taxpayer cannot see a draft, an escalation's details, another return, or
any page that drives the agents; their only input is an answer to the
question the Intake agent asked.

Everything goes through silverfile.service so the CLI and the tests share the
same code path (and the same permission checks). Works offline (no API key).

Run:  uvicorn silverfile.dashboard.app:create_app --factory --port 8765
"""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile  # what request.form() returns (FastAPI's UploadFile is a subclass, so isinstance against it never matched)

from .. import seed, service
from ..comms.bus import digest
from ..forms.render import jinja_env, mask_ssn_deep, render_html, render_pdf, status_label
from ..memory import session_summary
from ..agents.escalation import accept_requirements
from ..reconciliation import active_documents, evidence_conflicts
from ..security import audit, auth, integrity, pii, ratelimit, retention, uploads
from ..security.pii import install_log_redaction
from . import gate, web
from .web import CSRFFailed, LoginRequired, check_return_id, require_staff, require_taxpayer, verify_csrf

HERE = Path(__file__).resolve().parent
BUSY_STATUSES = ("extracting", "eligibility", "computing", "review")
FILING_STATUSES = [("", "leave as is"), ("single", "Single"), ("mfj", "Married filing jointly"), ("mfs", "Married filing separately"), ("hoh", "Head of household"), ("qss", "Qualifying surviving spouse")]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """On start: create the first admin from the environment if there is no staff
    account, delete abandoned returns past retention, then (if asked) seed the demo set."""
    install_log_redaction()
    if await run_in_threadpool(auth.bootstrap_admin_from_env):
        sys.stderr.write("[silverfile] created the first admin account from SILVERFILE_ADMIN_USER\n")
    elif not await run_in_threadpool(auth.staff_count):
        sys.stderr.write("[silverfile] no staff account yet: run `python run.py staff add <name> --role admin` (or set SILVERFILE_ADMIN_USER and SILVERFILE_ADMIN_PASSWORD)\n")
    await run_in_threadpool(retention.purge_abandoned)
    if seed.seed_enabled():
        await run_in_threadpool(seed.seed_demo_returns)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="SilverFile dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(env=jinja_env())

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        me = getattr(request.state, "principal", None)
        ctx.setdefault("me", me)
        ctx.setdefault("can", (lambda perm: bool(me and me.can(perm))))
        ctx.setdefault("csrf", web.csrf_for(request))
        ctx.setdefault("csp_nonce", request.state.csp_nonce)
        ctx.setdefault("key_ui", gate.key_ui_enabled())  # the header "API key" control
        ctx.setdefault("gate_on", gate.gate_enabled())
        ctx.setdefault("key_next", web.safe_next(request.url.path) if request.method == "GET" else "/")  # where the header form returns to
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def error(request: Request, status_code: int, title: str, message: str, back: str = "/") -> HTMLResponse:
        back = web.safe_next(back)
        return page(request, "error.html", status_code=status_code, title=title, message=message, back=back,
                    back_label="Back to this return" if back.startswith("/returns/") else "Go back")

    def error_back(request: Request) -> str:
        """Use the known return id; privacy headers intentionally suppress Referer."""
        rid = request.path_params.get("rid")
        if isinstance(rid, str) and web.RETURN_ID_RE.fullmatch(rid):
            return f"/returns/{rid}"
        return web.safe_next(request.headers.get("referer"))

    # ------------------------------------------------------------ middleware (the last added runs first)
    app.add_middleware(gate.KeyGate, render_gate=lambda request: page(request, "key.html"))
    app.add_middleware(web.SecurityMiddleware)

    # ------------------------------------------------------------ errors
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if exc.kind == "taxpayer":
            return RedirectResponse("/start", status_code=303)
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return error(request, 401, "Please log in", "Your session has ended. Log in again to continue.", "/login")

    @app.exception_handler(PermissionError)
    async def _forbidden(request: Request, exc: PermissionError):
        return error(request, 403, "Not allowed", str(exc) or "You are not allowed to do that.")

    @app.exception_handler(CSRFFailed)
    async def _csrf(request: Request, exc: CSRFFailed):
        return error(request, 403, "The form expired", "That form could not be verified (it may have been open too long, or sent from another site). Reload the page and try again.", error_back(request))

    @app.exception_handler(ratelimit.RateLimited)
    async def _limited(request: Request, exc: ratelimit.RateLimited):
        audit.record("rate_limited", path=request.url.path, ip=web.client_ip(request))
        r = error(request, 429, "Too many attempts", f"Please wait {exc.retry_after} seconds and try again.", error_back(request))
        r.headers["Retry-After"] = str(exc.retry_after)
        return r

    @app.exception_handler(service.StateRejected)
    async def _rejected(request: Request, exc: service.StateRejected):
        return error(request, 409, "This return was not loaded", str(exc))

    @app.exception_handler(gate.AuthFailed)
    async def _key_rejected(request: Request, exc: gate.AuthFailed):
        gate.end_session(request.cookies.get(gate.SESSION_COOKIE))
        response = page(request, "key.html", status_code=401, error="Anthropic rejected your key mid-run (revoked, or out of credit). Enter a working key to continue.")
        gate.clear_session_cookie(response)
        return response

    @app.exception_handler(service.ServiceError)
    async def _bad_request(request: Request, exc: service.ServiceError):
        return error(request, 409 if isinstance(exc, service.Stale) else 400, "That did not fit the return's current state", str(exc), error_back(request))

    @app.exception_handler(KeyError)
    async def _not_found(request: Request, exc: KeyError):
        # the same answer whether the return does not exist or the visitor may not see it
        return error(request, 404, "No such return", "There is no return with that id here.")

    # ------------------------------------------------------------ health
    @app.get("/healthz")
    async def healthz():
        return Response("ok", media_type="text/plain")

    # ------------------------------------------------------------ session API keys (the header control and the optional gate; see gate.py)
    @app.get("/key", response_class=HTMLResponse)
    async def key_page(request: Request):
        if not gate.key_ui_enabled():
            return RedirectResponse("/", status_code=303)
        return page(request, "key.html")

    @app.post("/key")
    async def key_submit(request: Request, api_key: str = Form(""), next: str = Form("/")):
        if not gate.key_ui_enabled():
            return RedirectResponse("/", status_code=303)
        # the header form carries the logged-in session's token, the key page the pre-login one
        await verify_csrf(request, web.staff(request) or web.taxpayer(request))
        ratelimit.KEY_CHECK.hit(web.client_ip(request))
        try:
            key = await run_in_threadpool(gate.validate_key, api_key)
        except ValueError as e:
            return page(request, "key.html", status_code=400, error=str(e))
        gate.end_session(request.cookies.get(gate.SESSION_COOKIE))
        response = RedirectResponse(web.safe_next(next), status_code=303)
        gate.set_session_cookie(response, request, gate.start_session(key))
        return response

    @app.post("/key/forget")
    async def key_forget(request: Request, next: str = Form("/")):
        await verify_csrf(request, web.staff(request) or web.taxpayer(request))
        gate.end_session(request.cookies.get(gate.SESSION_COOKIE))
        response = RedirectResponse("/key" if gate.gate_enabled() else web.safe_next(next), status_code=303)
        gate.clear_session_cookie(response)
        return response

    # ------------------------------------------------------------ staff login
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        if web.staff(request):
            return RedirectResponse(web.safe_next(next), status_code=303)
        return page(request, "login.html", next=web.safe_next(next), no_staff=not auth.staff_count())

    @app.post("/login")
    async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
        await verify_csrf(request)
        ratelimit.LOGIN.hit(web.client_ip(request))
        p = await run_in_threadpool(auth.authenticate, username, password, ip=web.client_ip(request))
        if p is None:
            return page(request, "login.html", status_code=401, next=web.safe_next(next), error="That username and password do not match an active account (after five wrong tries an account is locked for fifteen minutes).")
        auth.end_session(request.cookies.get(web.STAFF_COOKIE))
        token, _ = auth.start_session(p)
        response = RedirectResponse(web.safe_next(next), status_code=303)
        web.set_cookie(response, request, web.STAFF_COOKIE, token, auth.STAFF_MAX_SECONDS)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        me = web.staff(request)
        await verify_csrf(request, me)
        auth.end_session(request.cookies.get(web.STAFF_COOKIE))
        if me:
            audit.record("logout", actor=me.label)
        response = RedirectResponse("/login", status_code=303)
        web.clear_cookie(response, web.STAFF_COOKIE)
        return response

    # ------------------------------------------------------------ the front door
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        me = web.staff(request)
        if me is None:
            if web.taxpayer(request):
                return RedirectResponse("/t", status_code=303)
            return RedirectResponse("/login", status_code=303)
        request.state.principal = me
        if not me.can("list_returns"):
            raise PermissionError("Your role does not allow listing returns.")
        rows = await run_in_threadpool(service.list_returns)
        if not me.can("view_return"):
            rows = [{**r, "summary": None} for r in rows]  # a clerk sees who and where, not the details
        return page(request, "index.html", rows=rows, fixtures=service.list_fixtures(), needs_person=sum(1 for r in rows if r.get("waiting_on_human")), accept=uploads.ACCEPT)

    # ------------------------------------------------------------ staff: returns
    @app.post("/returns/new")
    async def new_return(request: Request):
        me = require_staff(request, "create_return")
        await verify_csrf(request, me)
        ratelimit.STAFF_WRITE.hit(me.subject)
        form = await request.form()
        fixture = (form.get("fixture") or "").strip() or None
        prefilled = form.get("prefilled") in ("1", "on", "true")
        rid = service.new_return_id()
        files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        if len(files) > uploads.MAX_FILES:
            raise service.ServiceError(f"At most {uploads.MAX_FILES} files at a time.")
        docs: list[dict[str, Any]] = []
        for f in files:
            docs.append(service.save_upload(rid, f.filename, await f.read(uploads.max_bytes() + 1), actor=me))
        intake = None
        if fixture:
            docs.extend(service.fixture_documents(fixture))
            if prefilled:
                intake = service.fixture_intake(fixture)
        await run_in_threadpool(lambda: service.create_return(docs, intake, None, return_id=rid, fixture=fixture, actor=me))
        await run_in_threadpool(service.run, rid, me)
        code = auth.issue_access_code(rid) if intake is None else None
        if code:
            audit.record("access_code_issued", actor=me.label, return_id=rid)
        return page(request, "created.html", rid=rid, code=code, fixture=fixture, anon_csrf=request.state.anon_csrf)

    async def render_return(request: Request, rid: str, *, status_code: int = 200, form_values: dict[str, str] | None = None, facts_patch_error: str | None = None, document_values: dict[str, str] | None = None, document_error: str | None = None):
        me = require_staff(request, "view_return")
        check_return_id(rid)
        state = await run_in_threadpool(service.get_state, rid)
        pend = service.pending(rid)
        row = service.index_row(rid) or {}
        open_esc = [e for e in state.get("escalations", []) if e.get("status") == "open"]
        return page(
            request, "return.html", status_code=status_code, rid=rid, state=state, pending=pend, pending_id=service.pending_id(rid), row=row, fixture=row.get("fixture"),
            transcript=service.transcript_from_state(state), summary=session_summary(state), open_escalations=open_esc,
            escalation=(pend.get("escalation") if pend and pend.get("type") == "escalation" else (open_esc[0] if open_esc else None)),
            documents_in=state.get("documents_in") or [], filing_statuses=FILING_STATUSES,
            auto_refresh=state.get("status") in BUSY_STATUSES and not facts_patch_error and not document_error, busy=BUSY_STATUSES, status_text=status_label(state.get("status")),
            hop_chain=_compress_hops(state.get("handoffs") or []), seal=integrity.verify(state), bus=digest(state),
            security_events=audit.recent(20, return_id=rid) if me.can("view_audit") else [],
            accept_requirements=accept_requirements(state), form_values=form_values or {}, facts_patch_error=facts_patch_error,
            evidence_conflicts=evidence_conflicts(state), active_documents=active_documents(state), document_values=document_values or {}, document_error=document_error,
        )

    @app.get("/returns/{rid}", response_class=HTMLResponse)
    async def show_return(request: Request, rid: str):
        return await render_return(request, rid)

    @app.get("/returns/{rid}/hitl.json")
    async def human_workflow(request: Request, rid: str):
        require_staff(request, "view_return")
        check_return_id(rid)
        state = await run_in_threadpool(service.get_state, rid)
        return JSONResponse(pii.redact_deep({"blocking_review": state.get("blocking_review"), "human_edit": state.get("human_edit"),
                                           "required_actions": accept_requirements(state), "evidence_conflicts": evidence_conflicts(state), "recompute_stage": state.get("recompute_stage"), "log": state.get("hitl_log") or []}))

    @app.get("/returns/{rid}/documents/{document_id}/source")
    async def source_document(request: Request, rid: str, document_id: str):
        me = require_staff(request, "resolve_escalation")
        check_return_id(rid)
        content, media = await run_in_threadpool(lambda: service.original_document(rid, document_id, actor=me))
        extension = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf", "application/json": "json"}[media]
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="original-source.{extension}"', "Cache-Control": "no-store"})

    @app.post("/returns/{rid}/document-correction")
    async def correct_document(request: Request, rid: str, document_id: str = Form(""), expected_version: str = Form(""), fields_json: str = Form("{}"), evidence: str = Form(""), superseded_by: str = Form(""), interrupt_id: str = Form("")):
        me = require_staff(request, "resolve_escalation")
        check_return_id(rid)
        await verify_csrf(request, me)
        ratelimit.STAFF_WRITE.hit(me.subject)
        try:
            try:
                fields = json.loads(fields_json)
            except json.JSONDecodeError as exc:
                raise service.FactsPatchError(f"Invalid document JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.") from exc
            try:
                version = int(expected_version)
            except ValueError as exc:
                raise service.FactsPatchError("The document version must be an integer. Reload the current return.") from exc
            correction = {"document_id": document_id, "expected_version": version, "fields": fields, "evidence": evidence}
            if superseded_by.strip():
                correction["superseded_by"] = superseded_by.strip()
            await run_in_threadpool(lambda: service.resolve(rid, {"action": "correct_document", "document_patch": correction}, actor=me, interrupt_id=interrupt_id))
        except service.FactsPatchError as exc:
            if request.headers.get("accept") == "application/json":
                return JSONResponse({"error": {"field": "document_patch", "message": str(exc)}, "return_url": f"/returns/{rid}"}, status_code=400)
            return await render_return(request, rid, status_code=400, document_error=str(exc),
                                       document_values={"document_id": document_id, "expected_version": expected_version, "fields_json": fields_json, "evidence": evidence, "superseded_by": superseded_by})
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    @app.post("/returns/{rid}/resolve")
    async def resolve(request: Request, rid: str, action: str = Form(...), note: str = Form(""), filing_status: str = Form(""), facts_patch: str = Form(""), interrupt_id: str = Form(""), note_transfer_id: str = Form(""), note_chunk_index: int = Form(0), note_final_chunk: bool = Form(False)):
        me = require_staff(request, "resolve_escalation")
        check_return_id(rid)
        await verify_csrf(request, me)
        ratelimit.STAFF_WRITE.hit(me.subject)

        async def invalid_facts(message: str):
            if request.headers.get("accept") == "application/json":
                return JSONResponse({"error": {"field": "facts_patch", "message": message}, "return_url": f"/returns/{rid}"}, status_code=400)
            return await render_return(request, rid, status_code=400,
                                       form_values={"facts_patch": facts_patch, "filing_status": filing_status, "note": note, "action": action},
                                       facts_patch_error=message)

        patch: dict[str, Any] = {}
        if facts_patch.strip():
            try:
                parsed = json.loads(facts_patch)
            except json.JSONDecodeError as e:
                return await invalid_facts(f"Invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}. Correct it and try again.")
            if not isinstance(parsed, dict):
                return await invalid_facts('Facts must be a JSON object, for example {"estimated_payments": 500}.')
            patch.update(parsed)
        if filing_status.strip():
            patch["filing_status"] = filing_status.strip()
        resolution: dict[str, Any] = {"action": action, "note": note if action == "append_intervention_note" else note.strip()}
        if action == "append_intervention_note":
            resolution.update(note_transfer_id=note_transfer_id, note_chunk_index=note_chunk_index, note_final_chunk=note_final_chunk)
        if patch:
            resolution["facts_patch"] = patch
        try:
            result = await run_in_threadpool(lambda: service.resolve(rid, resolution, actor=me, interrupt_id=interrupt_id or ""))
        except service.FactsPatchError as exc:
            return await invalid_facts(str(exc))
        if request.headers.get("accept") == "application/json":
            return JSONResponse(pii.redact_deep({"interrupt_id": service.pending_id(rid), "last_log": (result.get("hitl_log") or [None])[-1]}))
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    @app.post("/returns/{rid}/run")
    async def run_again(request: Request, rid: str):
        me = require_staff(request, "advance_return")
        check_return_id(rid)
        await verify_csrf(request, me)
        await run_in_threadpool(service.run, rid, me)
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    @app.post("/returns/{rid}/access-code")
    async def new_access_code(request: Request, rid: str):
        me = require_staff(request, "create_return")
        check_return_id(rid)
        await verify_csrf(request, me)
        if not service.index_row(rid):
            raise KeyError(rid)
        code = auth.issue_access_code(rid)
        audit.record("access_code_issued", actor=me.label, return_id=rid, reissued=True)
        return page(request, "created.html", rid=rid, code=code, fixture=(service.index_row(rid) or {}).get("fixture"), reissued=True, anon_csrf=request.state.anon_csrf)

    @app.post("/returns/{rid}/delete")
    async def delete(request: Request, rid: str):
        me = require_staff(request, "delete_return")
        check_return_id(rid)
        await verify_csrf(request, me)
        await run_in_threadpool(lambda: service.delete_return(rid, actor=me))
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------ staff: outputs
    @app.get("/returns/{rid}/form", response_class=HTMLResponse)
    async def form_html(request: Request, rid: str):
        require_staff(request, "view_draft")
        check_return_id(rid)
        state = await run_in_threadpool(service.get_state, rid)
        return HTMLResponse(render_html(state, dashboard_url=f"/returns/{rid}", csp_nonce=request.state.csp_nonce))

    @app.get("/returns/{rid}/form.pdf")
    async def form_pdf(request: Request, rid: str):
        require_staff(request, "view_draft")
        check_return_id(rid)
        state = await run_in_threadpool(service.get_state, rid)
        pdf = await run_in_threadpool(render_pdf, state)
        return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="1040sr-draft-{rid}.pdf"'})

    @app.get("/returns/{rid}/state.json")
    async def state_json(request: Request, rid: str):
        require_staff(request, "view_draft")
        check_return_id(rid)
        return JSONResponse(await run_in_threadpool(service.state_json, rid))

    @app.get("/returns/{rid}/messages.json")
    async def messages_json(request: Request, rid: str):
        require_staff(request, "view_return")
        check_return_id(rid)
        state = await run_in_threadpool(service.get_state, rid)
        return JSONResponse({"return_id": rid, "mailbox": mask_ssn_deep(state.get("mailbox") or [])})

    @app.get("/returns/{rid}/pending.json")
    async def pending_json(request: Request, rid: str):
        require_staff(request, "view_return")
        check_return_id(rid)
        return JSONResponse({"return_id": rid, "status": service.get_state(rid).get("status"), "pending": service.pending(rid)})

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request):
        require_staff(request, "view_audit")
        intact, bad, n = await run_in_threadpool(audit.verify_chain)
        return page(request, "audit.html", events=audit.recent(300), intact=intact, bad=bad, n=n)

    # ------------------------------------------------------------ taxpayers
    @app.get("/start", response_class=HTMLResponse)
    async def start_page(request: Request):
        if web.taxpayer(request):
            return RedirectResponse("/t", status_code=303)
        return page(request, "start.html")

    @app.post("/start")
    async def start(request: Request):
        await verify_csrf(request)
        ratelimit.START.hit(web.client_ip(request))
        rid = service.new_return_id()
        public = auth.system("taxpayer-start")
        await run_in_threadpool(lambda: service.create_return([], None, None, return_id=rid, actor=public))
        await run_in_threadpool(service.run, rid, public)
        code = auth.issue_access_code(rid)
        token, me = auth.start_session(auth.taxpayer_principal(rid))
        audit.record("taxpayer_session_started", actor=me.label, return_id=rid, how="new return", ip=web.client_ip(request))
        request.state.principal = me
        response = page(request, "taxpayer_code.html", code=code)
        web.set_cookie(response, request, web.TAXPAYER_COOKIE, token, auth.TAXPAYER_MAX_SECONDS)
        return response

    @app.post("/resume")
    async def resume(request: Request, code: str = Form("")):
        await verify_csrf(request)
        ratelimit.CODE_REDEEM.hit(web.client_ip(request))
        rid = auth.redeem_access_code(code)
        if rid is None or not service.index_row(rid):
            audit.record("taxpayer_resume_failed", ip=web.client_ip(request))
            return page(request, "start.html", status_code=401, error="That code does not open a return. Check it and try again, or ask Kushim for a new one.")
        auth.end_session(request.cookies.get(web.TAXPAYER_COOKIE))
        token, me = auth.start_session(auth.taxpayer_principal(rid))
        audit.record("taxpayer_session_started", actor=me.label, return_id=rid, how="access code", ip=web.client_ip(request))
        response = RedirectResponse("/t", status_code=303)
        web.set_cookie(response, request, web.TAXPAYER_COOKIE, token, auth.TAXPAYER_MAX_SECONDS)
        return response

    @app.get("/t", response_class=HTMLResponse)
    async def taxpayer_home(request: Request):
        me = require_taxpayer(request)
        rid = me.subject
        state = await run_in_threadpool(service.get_state, rid)
        pend = service.pending_for_taxpayer(rid)
        return page(request, "taxpayer.html", state=state, pending=pend if pend and pend.get("type") in ("question", "upload") else None,
                    pending_id=service.pending_id(rid), waiting_on_kushim=bool(pend and pend.get("type") == "escalation") or state.get("status") == "waiting_human",
                    transcript=service.transcript_from_state(state), auto_refresh=state.get("status") in BUSY_STATUSES or state.get("status") == "new",
                    has_fixture=bool((service.index_row(rid) or {}).get("fixture")), accept=uploads.ACCEPT, max_mb=uploads.max_bytes() // (1024 * 1024))

    @app.post("/t/answer")
    async def taxpayer_answer(request: Request, text: str = Form(""), interrupt_id: str = Form("")):
        me = require_taxpayer(request)
        await verify_csrf(request, me)
        ratelimit.ANSWER.hit(me.subject)
        rid = me.subject
        pend = service.pending(rid)
        docs: list[dict[str, Any]] = []
        if pend and pend.get("type") == "upload":
            row = service.index_row(rid) or {}
            if row.get("fixture"):
                docs = _not_yet_attached(rid, service.fixture_documents(row["fixture"]))
            text = text.strip() or "done"
        try:
            await run_in_threadpool(lambda: service.answer(rid, text, docs or None, actor=me, interrupt_id=interrupt_id or ""))
        except service.Stale:
            pass  # a double-submitted form: refused and logged; the page shows the current question
        return RedirectResponse("/t", status_code=303)

    @app.post("/t/upload")
    async def taxpayer_upload(request: Request):
        me = require_taxpayer(request)
        await verify_csrf(request, me)
        ratelimit.ANSWER.hit(me.subject)
        rid = me.subject
        form = await request.form()
        interrupt_id = str(form.get("interrupt_id") or "")
        try:
            service.check_interrupt(rid, interrupt_id)  # before any file is saved
        except service.Stale:
            return RedirectResponse("/t", status_code=303)
        pend = service.pending(rid)
        if not pend or pend.get("type") != "upload":
            raise service.ServiceError("The conversation is not at the upload step; answer the current question first.")
        files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        if len(files) > uploads.MAX_FILES:
            raise service.ServiceError(f"At most {uploads.MAX_FILES} files at a time.")
        docs = [service.save_upload(rid, f.filename, await f.read(uploads.max_bytes() + 1), actor=me) for f in files]
        if form.get("use_fixture"):
            row = service.index_row(rid) or {}
            if row.get("fixture"):
                docs.extend(_not_yet_attached(rid, service.fixture_documents(row["fixture"])))
        text = str(form.get("text") or "").strip() or "done"
        try:
            await run_in_threadpool(lambda: service.answer(rid, text, docs, actor=me, interrupt_id=interrupt_id))
        except service.Stale:
            pass  # answered by a concurrent submit while these files were saved
        return RedirectResponse("/t", status_code=303)

    @app.post("/t/leave")
    async def taxpayer_leave(request: Request):
        me = web.taxpayer(request)
        await verify_csrf(request, me)
        auth.end_session(request.cookies.get(web.TAXPAYER_COOKIE))
        response = RedirectResponse("/start", status_code=303)
        web.clear_cookie(response, web.TAXPAYER_COOKIE)
        return response

    return app


def _compress_hops(handoffs: list[str]) -> list[tuple[str, int]]:
    """The agent chain without the orchestrator (it sits between every pair), consecutive repeats counted."""
    out: list[tuple[str, int]] = []
    for h in handoffs:
        if h == "orchestrator":
            continue
        if out and out[-1][0] == h:
            out[-1] = (h, out[-1][1] + 1)
        else:
            out.append((h, 1))
    return out


def _not_yet_attached(rid: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    have = {d.get("id") for d in (service.get_state(rid).get("documents_in") or [])}
    return [d for d in docs if d.get("id") not in have]
