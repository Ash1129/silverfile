"""The Kushim dashboard: list returns, run the taxpayer conversation, resolve
escalations, read the agent trace, and open the draft form (HTML / PDF).

Everything goes through silverfile.service so the CLI and the tests share the
same code path. Works offline (no API key): the graph then uses its scripted
intake parser, fixture / sidecar extraction and rules-only review.

Run:  uvicorn silverfile.dashboard.app:create_app --factory --port 8765
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import seed, service
from . import gate
from ..forms.render import jinja_env, render_html, render_pdf, status_label
from ..memory import session_summary

HERE = Path(__file__).resolve().parent
BUSY_STATUSES = ("extracting", "eligibility", "computing", "review")
FILING_STATUSES = [("", "leave as is"), ("single", "Single"), ("mfj", "Married filing jointly"), ("mfs", "Married filing separately"), ("hoh", "Head of household"), ("qss", "Qualifying surviving spouse")]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """With SILVERFILE_SEED_DEMO set and an empty index, seed the demo returns
    before serving (see seed.py), so a fresh deployment is not an empty list."""
    if seed.seed_enabled():
        await run_in_threadpool(seed.seed_demo_returns)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="SilverFile dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(env=jinja_env())

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    # ------------------------------------------------------------ key gate (shared copies only; see gate.py)
    app.add_middleware(gate.KeyGate, render_gate=lambda request: page(request, "key.html"))

    @app.get("/key", response_class=HTMLResponse)
    async def key_page(request: Request):
        if not gate.gate_enabled():
            return RedirectResponse("/", status_code=303)
        return page(request, "key.html")

    @app.post("/key")
    async def key_submit(request: Request, api_key: str = Form("")):
        if not gate.gate_enabled():
            return RedirectResponse("/", status_code=303)
        try:
            key = await run_in_threadpool(gate.validate_key, api_key)
        except ValueError as e:
            return page(request, "key.html", status_code=400, error=str(e))
        gate.end_session(request.cookies.get(gate.SESSION_COOKIE))
        response = RedirectResponse("/", status_code=303)
        gate.set_session_cookie(response, request, gate.start_session(key))
        return response

    @app.post("/key/forget")
    async def key_forget(request: Request):
        gate.end_session(request.cookies.get(gate.SESSION_COOKIE))
        response = RedirectResponse("/key" if gate.gate_enabled() else "/", status_code=303)
        gate.clear_session_cookie(response)
        return response

    @app.exception_handler(gate.AuthFailed)
    async def _key_rejected(request: Request, exc: gate.AuthFailed):
        gate.end_session(request.cookies.get(gate.SESSION_COOKIE))
        response = page(request, "key.html", status_code=401, error="Anthropic rejected your key mid-run (revoked, or out of credit). Enter a working key to continue.")
        gate.clear_session_cookie(response)
        return response

    @app.exception_handler(service.ServiceError)
    async def _bad_request(request: Request, exc: service.ServiceError):
        return page(request, "error.html", status_code=400, title="That did not fit the return's current state", message=str(exc), back=request.headers.get("referer") or "/")

    @app.exception_handler(KeyError)
    async def _not_found(request: Request, exc: KeyError):
        return page(request, "error.html", status_code=404, title="No such return", message=f"Return {exc.args[0] if exc.args else ''} is not in this dashboard's data directory.", back="/")

    # ------------------------------------------------------------ list / new
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        rows = await run_in_threadpool(service.list_returns)
        return page(request, "index.html", rows=rows, fixtures=service.list_fixtures(), needs_person=sum(1 for r in rows if r.get("waiting_on_human")))

    @app.post("/returns/new")
    async def new_return(request: Request):
        form = await request.form()
        fixture = (form.get("fixture") or "").strip() or None
        prefilled = form.get("prefilled") in ("1", "on", "true")
        rid = service.new_return_id()
        docs: list[dict[str, Any]] = []
        for f in form.getlist("files"):
            if isinstance(f, UploadFile) and f.filename:
                docs.append(service.save_upload(rid, f.filename, await f.read()))
        intake = None
        if fixture:
            docs.extend(service.fixture_documents(fixture))
            if prefilled:
                intake = service.fixture_intake(fixture)
        await run_in_threadpool(service.create_return, docs, intake, None, return_id=rid, fixture=fixture)
        await run_in_threadpool(service.run, rid)
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    # ------------------------------------------------------------ one return
    @app.get("/returns/{rid}", response_class=HTMLResponse)
    async def show_return(request: Request, rid: str):
        state = await run_in_threadpool(service.get_state, rid)
        pend = service.pending(rid)
        row = service.index_row(rid) or {}
        open_esc = [e for e in state.get("escalations", []) if e.get("status") == "open"]
        return page(
            request, "return.html", rid=rid, state=state, pending=pend, row=row, fixture=row.get("fixture"),
            transcript=service.transcript_from_state(state), summary=session_summary(state), open_escalations=open_esc,
            escalation=(pend.get("escalation") if pend and pend.get("type") == "escalation" else (open_esc[0] if open_esc else None)),
            documents_in=state.get("documents_in") or [], filing_statuses=FILING_STATUSES,
            auto_refresh=state.get("status") in BUSY_STATUSES, busy=BUSY_STATUSES, status_text=status_label(state.get("status")),
            hop_chain=_compress_hops(state.get("handoffs") or []),
        )

    @app.post("/returns/{rid}/answer")
    async def answer(rid: str, text: str = Form(""), use_fixture: str | None = Form(None)):
        pend = service.pending(rid)
        docs: list[dict[str, Any]] = []
        if pend and pend.get("type") == "upload":
            # the fixture's documents are attached at the upload step automatically; they may already be on the return
            row = service.index_row(rid) or {}
            if row.get("fixture"):
                docs = _not_yet_attached(rid, service.fixture_documents(row["fixture"]))
            text = text.strip() or "done"
        await run_in_threadpool(service.answer, rid, text.strip(), docs or None)
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    @app.post("/returns/{rid}/upload")
    async def upload(request: Request, rid: str):
        form = await request.form()
        docs: list[dict[str, Any]] = []
        for f in form.getlist("files"):
            if isinstance(f, UploadFile) and f.filename:
                docs.append(service.save_upload(rid, f.filename, await f.read()))
        if form.get("use_fixture"):
            row = service.index_row(rid) or {}
            if row.get("fixture"):
                docs.extend(_not_yet_attached(rid, service.fixture_documents(row["fixture"])))
        text = (form.get("text") or "").strip() or "done"
        pend = service.pending(rid)
        if not pend or pend.get("type") != "upload":
            raise service.ServiceError("The conversation is not at the upload step; answer the current question first.")
        await run_in_threadpool(service.answer, rid, text, docs)
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    @app.post("/returns/{rid}/resolve")
    async def resolve(rid: str, action: str = Form(...), resolved_by: str = Form("kushim-employee"), note: str = Form(""), filing_status: str = Form(""), facts_patch: str = Form("")):
        patch: dict[str, Any] = {}
        if facts_patch.strip():
            try:
                parsed = json.loads(facts_patch)
            except json.JSONDecodeError as e:
                raise service.ServiceError(f"facts_patch is not valid JSON: {e}") from e
            if not isinstance(parsed, dict):
                raise service.ServiceError("facts_patch must be a JSON object, for example {\"filing_status\": \"mfj\"}.")
            patch.update(parsed)
        if filing_status.strip():
            patch["filing_status"] = filing_status.strip()
        resolution = {"action": action, "resolved_by": resolved_by.strip() or "kushim-employee", "note": note.strip()}
        if patch:
            resolution["facts_patch"] = patch
        await run_in_threadpool(service.resolve, rid, resolution)
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    @app.post("/returns/{rid}/run")
    async def run_again(rid: str):
        await run_in_threadpool(service.run, rid)
        return RedirectResponse(f"/returns/{rid}", status_code=303)

    # ------------------------------------------------------------ outputs
    @app.get("/returns/{rid}/form", response_class=HTMLResponse)
    async def form_html(rid: str):
        state = await run_in_threadpool(service.get_state, rid)
        return HTMLResponse(render_html(state, dashboard_url=f"/returns/{rid}"))

    @app.get("/returns/{rid}/form.pdf")
    async def form_pdf(rid: str):
        state = await run_in_threadpool(service.get_state, rid)
        pdf = await run_in_threadpool(render_pdf, state)
        return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="1040sr-draft-{rid}.pdf"'})

    @app.get("/returns/{rid}/state.json")
    async def state_json(rid: str):
        return JSONResponse(await run_in_threadpool(service.state_json, rid))

    @app.get("/returns/{rid}/pending.json")
    async def pending_json(rid: str):
        return JSONResponse({"return_id": rid, "status": service.get_state(rid).get("status"), "pending": service.pending(rid)})

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
