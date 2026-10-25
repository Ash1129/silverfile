"""Render a ReturnState as a draft Form 1040-SR.

render_html(state) -> str   the Jinja2 template dashboard/templates/form.html,
                            standalone (inline CSS, one tiny inline script);
                            every amount carries a provenance popover.
render_pdf(state)  -> bytes a reportlab mirror of the same lines in large type,
                            with a provenance appendix.

Both work from a plain state dict; no web server is involved. SSNs are masked
to the last four digits everywhere.
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..provenance import ALL_LINES, LINE_LABELS

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
BANNER = "DRAFT — prepared by SilverFile for review by a Kushim preparer. Not filed. Not advice."
SSN_RE = re.compile(r"\b(\d{3})[- ]?(\d{2})[- ]?(\d{4})\b")

FILING_STATUS_LABELS = {"single": "Single", "mfj": "Married filing jointly", "mfs": "Married filing separately", "hoh": "Head of household", "qss": "Qualifying surviving spouse"}
BOX_LABELS_12D = [("taxpayer_65", "You were born before January 2, 1961"), ("taxpayer_blind", "You are blind"), ("spouse_65", "Spouse was born before January 2, 1961"), ("spouse_blind", "Spouse is blind")]
SECTIONS: list[tuple[str, list[str]]] = [
    ("Income", ["1z", "2a", "2b", "3a", "3b", "4a", "4b", "5a", "5b", "6a", "6b", "7", "8", "9"]),
    ("Adjusted gross income", ["10", "11a"]),
    ("Standard deduction", ["12d", "12e", "13b", "14", "15"]),
    ("Tax and credits", ["16", "17", "18", "19", "20", "21", "22", "23", "24"]),
    ("Payments", ["25a", "25b", "25c", "25d", "26", "32", "33"]),
    ("Refund or amount you owe", ["34", "37"]),
]
COUNT_LINES = {"12d"}  # a count of boxes, not dollars
STATUS_LABELS = {"new": "New", "intake": "In conversation", "extracting": "Reading documents", "eligibility": "Checking eligibility", "computing": "Computing", "review": "In review", "waiting_human": "Needs a person", "complete": "Draft complete", "halted": "Halted"}


# ---------------------------------------------------------------- small helpers (also used as Jinja filters)
def mask_ssn(value: Any) -> str:
    """Show only the last four digits of anything that looks like an SSN."""
    if value in (None, ""):
        return ""
    return SSN_RE.sub(lambda m: f"***-**-{m.group(3)}", str(value))


def mask_ssn_deep(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: mask_ssn_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_ssn_deep(v) for v in obj]
    if isinstance(obj, str):
        return mask_ssn(obj)
    return obj


def fmt_money(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"(${abs(v):,.2f})" if v < 0 else f"${v:,.2f}"


def fmt_when(value: Any) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt.tzinfo else dt.strftime("%Y-%m-%d %H:%M")


def pretty(value: Any) -> str:
    if value in (None, {}, []):
        return "—"
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def status_label(value: Any) -> str:
    return STATUS_LABELS.get(str(value), str(value or ""))


def filing_status_label(value: Any) -> str:
    return FILING_STATUS_LABELS.get(str(value), "not yet decided" if not value else str(value))


_env: Environment | None = None


def jinja_env() -> Environment:
    """One Jinja2 environment shared by render_html and the dashboard, with the same filters."""
    global _env
    if _env is None:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html", "xml"]), trim_blocks=True, lstrip_blocks=True)
        env.filters.update(money=fmt_money, when=fmt_when, mask=mask_ssn, pretty=pretty, status_label=status_label, fs_label=filing_status_label)
        env.globals.update(BANNER=BANNER)
        _env = env
    return _env


# ---------------------------------------------------------------- the view model
def _row(line: str, lines: dict[str, Any], prov: dict[str, Any], doc_ids: set[str]) -> dict[str, Any]:
    p = prov.get(line) or {}
    amount = lines.get(line)
    if line in COUNT_LINES:
        n = int(float(amount or 0))
        display = f"{n} box{'es' if n != 1 else ''} checked" if amount is not None else "—"
    else:
        display = fmt_money(amount)
    sources = []
    for s in p.get("sources") or []:
        s = str(s)
        sources.append({"id": s, "href": f"#doc-{s}" if s in doc_ids else (f"#line-{s}" if s in LINE_LABELS else None)})
    return {
        "line": line, "label": LINE_LABELS.get(line, line), "amount": amount, "display": display, "is_count": line in COUNT_LINES,
        "prov": p or None, "sources": sources, "has_prov": bool(p), "on_form": line in lines,
        "method": p.get("method") or ("—" if not p else ""), "source_kind": p.get("source_kind") or "", "transport": p.get("transport") or "",
        "inputs_pretty": pretty(p.get("inputs")) if p else "—", "citations": list(p.get("citations") or []),
        "entered_by": p.get("entered_by"), "entered_at": p.get("entered_at"), "note": p.get("note"),
        "mismatch": bool(p) and amount is not None and abs(float(p.get("amount", 0) or 0) - float(amount or 0)) > 0.005,
    }


def form_context(state: dict[str, Any], dashboard_url: str | None = None) -> dict[str, Any]:
    """Everything the form template (and the PDF) needs, computed once from the state."""
    facts = state.get("intake") or {}
    tp = facts.get("taxpayer") or {}
    sp = facts.get("spouse") or {}
    elig = state.get("eligibility") or {}
    lines = state.get("lines") or {}
    prov = state.get("provenance") or {}
    extracted = state.get("extracted") or {}
    doc_ids = set(extracted)
    fs = elig.get("filing_status") or facts.get("filing_status")
    boxes = elig.get("boxes_12d") or {}
    documents = []
    for doc_id, d in extracted.items():
        fields = []
        for k, v in (d.get("fields") or {}).items():
            fields.append({"field": k, "box": (d.get("boxes") or {}).get(k, ""), "value": mask_ssn(v) if isinstance(v, str) else v})
        documents.append({"id": doc_id, "kind": d.get("kind"), "method": d.get("method"), "confidence": d.get("confidence"), "source_ref": Path(str(d.get("source_ref") or "")).name, "issues": list(d.get("issues") or []), "out_of_scope": bool(d.get("out_of_scope")), "version": d.get("version"), "fields": fields, "extracted_at": d.get("extracted_at")})
    sections = [{"title": title, "rows": [_row(l, lines, prov, doc_ids) for l in ls]} for title, ls in SECTIONS]
    return {
        "banner": BANNER,
        "dashboard_url": dashboard_url,
        "return_id": state.get("return_id"),
        "tax_year": state.get("tax_year", 2025),
        "status": state.get("status"),
        "created_at": state.get("created_at"),
        "has_lines": bool(lines),
        "header": {
            "name": f"{tp.get('first_name', '')} {tp.get('last_name', '')}".strip() or "—",
            "ssn": mask_ssn(tp.get("ssn")) or "—",
            "dob": tp.get("dob") or "—",
            "blind": tp.get("blind"),
            "filing_status": fs,
            "filing_status_label": filing_status_label(fs),
            "filing_status_valid": elig.get("filing_status_valid"),
            "spouse": ({"name": f"{sp.get('first_name', '')} {sp.get('last_name', '')}".strip(), "ssn": mask_ssn(sp.get("ssn")) or "—", "dob": sp.get("dob") or "—", "date_of_death": sp.get("date_of_death")} if sp else None),
            "boxes_12d": [{"key": k, "label": label, "checked": bool(boxes.get(k))} for k, label in BOX_LABELS_12D],
            "box_count": elig.get("box_count", int(float(lines.get("12d", 0) or 0))),
            "dependents": [{"name": d.get("name", ""), "ssn": mask_ssn(d.get("ssn")) or "—", "relationship": d.get("relationship", "")} for d in (facts.get("dependents") or [])],
            "answering_party": facts.get("answering_party"),
            "refund_account": facts.get("refund_account_description"),
            "schedules": list(elig.get("schedules") or []),
        },
        "sections": sections,
        "provenance_rows": [_row(l, lines, prov, doc_ids) for l in ALL_LINES],
        "documents": documents,
        "eligibility": elig,
        "determinations": list(elig.get("determinations") or []),
        "review_history": list(state.get("review_history") or []),
        "computation_notes": list(state.get("computation_notes") or []),
        "computation_version": state.get("computation_version"),
        "escalations": list(state.get("escalations") or []),
        "noted_questions": list(facts.get("noted_questions") or []),
        "halted_reason": state.get("halted_reason"),
    }


def render_html(state: dict[str, Any], dashboard_url: str | None = None) -> str:
    return jinja_env().get_template("form.html").render(**form_context(state, dashboard_url=dashboard_url))


# ---------------------------------------------------------------- PDF
def render_pdf(state: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    ctx = form_context(state)
    h = ctx["header"]
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=14, leading=19)
    small = ParagraphStyle("small", parent=body, fontSize=11.5, leading=15)
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=body, fontName="Helvetica-Bold", fontSize=22, leading=27, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=17, leading=22, spaceBefore=14, spaceAfter=6)
    h3 = ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", fontSize=13.5, leading=17, spaceBefore=8, spaceAfter=2)
    mono = ParagraphStyle("mono", fontName="Courier", fontSize=9.5, leading=12, leftIndent=12)
    banner_style = ParagraphStyle("banner", parent=body, fontName="Helvetica-Bold", fontSize=13, leading=17, textColor=colors.HexColor("#7a2e00"))

    def P(text: str, style=body) -> Paragraph:
        return Paragraph(escape(str(text)), style)

    def banner_block():
        t = Table([[Paragraph(escape(BANNER), banner_style)]], colWidths=[7.1 * inch])
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#b4530a")), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff3e6")), ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        return t

    def lines_table(rows):
        data = [[P("Line", small), P("Description", small), Paragraph("Amount", ParagraphStyle("hr", parent=small, alignment=TA_RIGHT))]]
        for r in rows:
            data.append([P(r["line"]), P(r["label"]), P(r["display"], right)])
        t = Table(data, colWidths=[0.7 * inch, 4.6 * inch, 1.8 * inch], repeatRows=1)
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black), ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#b8b8b8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("BACKGROUND", (2, 1), (2, -1), colors.HexColor("#f4f6f8")),
        ]))
        return t

    story: list[Any] = [banner_block(), Spacer(1, 10), P(f"Form 1040-SR (draft) — U.S. Tax Return for Seniors — {ctx['tax_year']}", title), P(f"Return {ctx['return_id']} · status {status_label(ctx['status'])} · generated {fmt_when(datetime.now().astimezone().isoformat())}", small), Spacer(1, 8)]

    ident = [["Your name", h["name"]], ["Your SSN (masked)", h["ssn"]], ["Date of birth", h["dob"]], ["Filing status", h["filing_status_label"]]]
    if h["spouse"]:
        ident += [["Spouse", h["spouse"]["name"] or "—"], ["Spouse SSN (masked)", h["spouse"]["ssn"]], ["Spouse date of birth", h["spouse"]["dob"]]]
        if h["spouse"].get("date_of_death"):
            ident.append(["Spouse date of death", h["spouse"]["date_of_death"]])
    ident.append(["Age / blindness (line 12d)", "; ".join(f"[{'X' if b['checked'] else ' '}] {b['label']}" for b in h["boxes_12d"])])
    ident.append(["Dependents", "; ".join(f"{d['name']} ({d['relationship']}, SSN {d['ssn']})" for d in h["dependents"]) or "none"])
    if h["schedules"]:
        ident.append(["Schedules", ", ".join(h["schedules"])])
    it = Table([[P(a, small), P(b)] for a, b in ident], colWidths=[2.2 * inch, 4.9 * inch])
    it.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8b8b8")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(it)

    if not ctx["has_lines"]:
        story += [Spacer(1, 14), P("No lines have been computed yet. The conversation or a person's decision is still pending.", body)]
    for sec in ctx["sections"]:
        heading, table = P(sec["title"], h2), lines_table(sec["rows"])
        if len(sec["rows"]) <= 9:
            story.append(KeepTogether([heading, table]))  # a short section never leaves its heading orphaned
        else:
            story += [heading, table]  # the long income table may split; its header row repeats

    # ---- appendix A: provenance per line
    story += [PageBreak(), P("Appendix A — Provenance (every amount, how it was produced)", h2), P("Method is the tax_math tool that produced the figure, or 'direct user input'. Inputs are the exact arguments, with box references where a document was read. Sources are document ids or earlier lines.", small)]
    for r in ctx["provenance_rows"]:
        block = [P(f"Line {r['line']} — {r['label']}: {r['display']}", h3)]
        if not r["has_prov"]:
            block.append(P("No provenance record." if r["on_form"] else "Not on this draft.", small))
        else:
            meta = f"Source kind: {r['source_kind']}   ·   Method: {r['method']}" + (f"   ·   Transport: {r['transport']}" if r["transport"] and r["transport"] != "n/a" else "")
            block.append(P(meta, small))
            block.append(P("Sources: " + (", ".join(s["id"] for s in r["sources"]) or "none") + "   ·   Citations: " + (", ".join(r["citations"]) or "none"), small))
            if r["source_kind"] == "user" or r["entered_by"]:
                block.append(P(f"Entered by: {r['entered_by'] or 'unknown'} at {fmt_when(r['entered_at'])}", small))
            if r["note"]:
                block.append(P(f"Note: {r['note']}", small))
            if r["inputs_pretty"] != "—":
                block.append(Preformatted(r["inputs_pretty"], mono, maxLineLength=85))
        story.append(KeepTogether(block))

    # ---- appendix B: documents, eligibility, review, notes
    story += [PageBreak(), P("Appendix B — Documents read", h2)]
    if not ctx["documents"]:
        story.append(P("No documents were read.", body))
    for d in ctx["documents"]:
        block = [P(f"{d['id']} — {d['kind']} (read via {d['method']}, confidence {d['confidence']})" + (" — OUT OF SCOPE" if d["out_of_scope"] else ""), h3)]
        data = [[P("Field", small), P("Box", small), P("Value", small)]] + [[P(f["field"], small), P(f["box"], small), P("" if f["value"] is None else f["value"], small)] for f in d["fields"]]
        t = Table(data, colWidths=[2.3 * inch, 1.6 * inch, 3.2 * inch])
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black), ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#cccccc")), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        block.append(t)
        for issue in d["issues"]:
            block.append(P(f"Issue: {issue}", small))
        story.append(KeepTogether(block))
    story += [P("Eligibility determinations", h2)]
    for det in ctx["determinations"]:
        story.append(P(f"{det.get('claim')}: {det.get('result')}  [{det.get('citation')}]", small))
    if not ctx["determinations"]:
        story.append(P("Eligibility has not run yet.", small))
    story += [P("Review rounds", h2)]
    for rr in ctx["review_history"]:
        story.append(P(f"Round {rr.get('round')}: {'passed' if rr.get('passed') else 'failed'} ({rr.get('method')}, {len(rr.get('findings') or [])} finding(s))", small))
        for f in rr.get("findings") or []:
            story.append(P(f"  {f.get('severity')} · line {f.get('line') or '—'} · {f.get('check')}: {f.get('detail')}", small))
    if not ctx["review_history"]:
        story.append(P("No review has run yet.", small))
    if ctx["computation_notes"]:
        story.append(P("Computation notes", h2))
        for n in ctx["computation_notes"]:
            story.append(P(n, small))
    if ctx["escalations"]:
        story.append(P("Escalations", h2))
        for e in ctx["escalations"]:
            res = e.get("resolution") or {}
            story.append(P(f"{e.get('kind')} ({e.get('status')}): {e.get('reason')}" + (f" — resolved by {e.get('resolved_by')}: {res.get('action')} {res.get('note') or ''}" if e.get("status") == "resolved" else ""), small))

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 9.5)
        canvas.setFillColor(colors.HexColor("#7a2e00"))
        canvas.drawString(0.7 * inch, 0.55 * inch, BANNER)
        canvas.setFillColor(colors.black)
        canvas.setFont("Helvetica", 9.5)
        canvas.drawRightString(letter[0] - 0.7 * inch, 0.35 * inch, f"Return {ctx['return_id']} · page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch, topMargin=0.75 * inch, bottomMargin=0.85 * inch, title=f"Draft Form 1040-SR — {h['name']}", author="SilverFile (draft, not filed)")
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
