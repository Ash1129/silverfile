"""Fill the supplied 2025 IRS form with an explicit, versioned field map.

This is presentation only: no tax calculations, guessed facts, refund elections,
or signatures. The four official pages remain interactive and are marked draft.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, Fit, FloatObject, NameObject, NumberObject, TextStringObject

from ..security import pii

TEMPLATE = Path(__file__).with_name("templates") / "f1040sr_2025.pdf"
TEMPLATE_SHA256 = "162a646c25af23338d103193f1a82a058ae61f9086b41d445edfb8672d5d0072"
ROOT = "topmostSubform[0]."


def field(page: int, name: str) -> str:
    return f"{ROOT}Page{page}[0].{name}"


# Internal line -> exact official AcroForm field. 7 maps to printed line 7a.
LINE_FIELDS = {
    "1z": field(1, "f1_57[0]"),
    "2a": field(2, "f2_01[0]"), "2b": field(2, "f2_02[0]"),
    "3a": field(2, "f2_03[0]"), "3b": field(2, "f2_04[0]"),
    "4a": field(2, "Line4a_ReadOrder[0].f2_05[0]"), "4b": field(2, "f2_06[0]"),
    "5a": field(2, "Line5a_ReadOrder[0].f2_08[0]"), "5b": field(2, "f2_09[0]"),
    "6a": field(2, "Line6a_ReadOrder[0].f2_11[0]"), "6b": field(2, "f2_12[0]"),
    "7": field(2, "f2_13[0]"), "8": field(2, "f2_15[0]"), "9": field(2, "f2_16[0]"),
    "10": field(2, "f2_17[0]"), "11a": field(2, "f2_18[0]"),
    "12e": field(2, "f2_20[0]"), "13b": field(2, "f2_22[0]"),
    "14": field(2, "f2_23[0]"), "15": field(2, "f2_24[0]"), "16": field(2, "f2_26[0]"),
    "17": field(2, "f2_27[0]"), "18": field(2, "f2_28[0]"), "19": field(2, "f2_29[0]"),
    "20": field(2, "f2_30[0]"), "21": field(2, "f2_31[0]"), "22": field(2, "f2_32[0]"),
    "23": field(2, "f2_33[0]"), "24": field(2, "f2_34[0]"),
    "25a": field(3, "f3_01[0]"), "25b": field(3, "f3_02[0]"), "25c": field(3, "f3_03[0]"),
    "25d": field(3, "f3_04[0]"), "26": field(3, "f3_05[0]"), "32": field(3, "f3_12[0]"),
    "33": field(3, "f3_13[0]"), "34": field(3, "f3_14[0]"), "37": field(3, "f3_19[0]"),
}
ALIASES = {field(1, "f1_47[0]"): "1z", field(2, "f2_19[0]"): "11a"}
FILING_FIELDS = {
    "single": (field(1, "Single_ReadOrder[0].c1_8[0]"), "/1"),
    "mfj": (field(1, "Single_ReadOrder[0].c1_8[1]"), "/2"),
    "mfs": (field(1, "Single_ReadOrder[0].c1_8[2]"), "/3"),
    "hoh": (field(1, "c1_8[0]"), "/4"), "qss": (field(1, "c1_8[1]"), "/5"),
}
AGE_FIELDS = {"taxpayer_65": "c2_17[0]", "taxpayer_blind": "c2_18[0]", "spouse_65": "c2_19[0]", "spouse_blind": "c2_20[0]"}


def _ssn(value: Any) -> str:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return "XXXXX" + digits[-4:] if len(digits) in (4, 9) else ""


def field_values(state: dict[str, Any]) -> dict[str, str]:
    facts, eligibility = state.get("intake") or {}, state.get("eligibility") or {}
    tp, sp = facts.get("taxpayer") or {}, facts.get("spouse") or {}
    status = eligibility.get("filing_status") or facts.get("filing_status")
    values = {field(1, "f1_14[0]"): pii.mask(str(tp.get("first_name") or "")),
              field(1, "f1_15[0]"): pii.mask(str(tp.get("last_name") or "")),
              field(1, "YourSocial[0].f1_16[0]"): _ssn(tp.get("ssn"))}
    if status in ("mfj", "mfs"):
        values[field(1, "f1_19[0]")] = _ssn(sp.get("ssn"))
        if status == "mfj":
            values.update({field(1, "f1_17[0]"): pii.mask(str(sp.get("first_name") or "")), field(1, "f1_18[0]"): pii.mask(str(sp.get("last_name") or ""))})
        else:
            values[field(1, "Single_ReadOrder[0].f1_28[0]")] = pii.mask(f"{sp.get('first_name', '')} {sp.get('last_name', '')}".strip())
    if status in FILING_FIELDS:
        name, choice = FILING_FIELDS[status]
        values[name] = choice
    for person, start in ((tp, 5), (sp, 8)):
        death = str(person.get("date_of_death") or "")
        if len(death) == 10 and death[4] == death[7] == "-":
            year, month, day = death.split("-")
            for offset, part in enumerate((month, day, year)):
                values[field(1, f"f1_{start+offset:02}[0]")] = part
            if person is tp:
                values[field(1, "c1_3[0]")] = "/1"
    dependents = facts.get("dependents") or []
    if len(dependents) > 4:
        values[field(1, "Dependents_ReadOrder[0].c1_11[0]")] = "/1"
    for index, dependent in enumerate(dependents[:4]):
        parts = str(dependent.get("name") or "").rsplit(" ", 1)
        first, last = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
        for row, number, text in ((1, 31+index, first), (2, 35+index, last), (3, 39+index, _ssn(dependent.get("ssn"))), (4, 43+index, str(dependent.get("relationship") or ""))):
            values[field(1, f"Table_Dependents[0].Row{row}[0].f1_{number:02}[0]")] = pii.mask(text)
    for key, name in AGE_FIELDS.items():
        if (eligibility.get("boxes_12d") or {}).get(key) is True:
            values[field(2, f"Line12d_ReadOrder[0].{name}")] = "/1"
    for key, name in (("can_be_claimed_as_dependent", "c2_13[0]"), ("mfs_spouse_itemizes", "c2_15[0]"), ("dual_status_alien", "c2_16[0]")):
        if facts.get(key) is True and (key != "mfs_spouse_itemizes" or status == "mfs"):
            values[field(2, name)] = "/1"
    lines = state.get("lines") or {}
    for line, name in LINE_FIELDS.items():
        if lines.get(line) is not None:
            values[name] = f"{float(lines[line]):,.2f}"
    for name, line in ALIASES.items():
        if lines.get(line) is not None:
            values[name] = f"{float(lines[line]):,.2f}"
    count = lines.get("12d")
    if count is None and eligibility.get("filing_status_valid") and eligibility.get("boxes_12d"):
        count = eligibility.get("box_count")
    if count is not None:
        values[field(4, "f4_01[0]")] = str(int(count))
    return values


def _full_name(widget) -> str:
    parts, node = [], widget
    while node:
        if node.get("/T"):
            parts.insert(0, str(node["/T"]))
        parent = node.get("/Parent")
        node = parent.get_object() if parent else None
    return ".".join(parts)


def _checkbox_appearances(writer, widget) -> None:
    """Vector check marks render without the template's viewer-specific symbol fonts."""
    x0, y0, x1, y1 = (float(v) for v in widget["/Rect"])
    width, height = x1-x0, y1-y0
    original = widget.get("/AP", {}).get("/N", {})
    choices = [key for key in original if key != "/Off"]
    appearances = DictionaryObject()
    for choice in ["/Off", *choices]:
        stream = DecodedStreamObject()
        drawing = "q Q" if choice == "/Off" else f"q 0 G 1 w 1.3 1.3 m {width-1.3} {height-1.3} l S 1.3 {height-1.3} m {width-1.3} 1.3 l S Q"
        stream.set_data(drawing.encode("ascii"))
        stream.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"), NameObject("/BBox"): ArrayObject([FloatObject(v) for v in (0, 0, width, height)]), NameObject("/Resources"): DictionaryObject()})
        appearances[NameObject(choice)] = writer._add_object(stream)
    widget[NameObject("/AP")] = DictionaryObject({NameObject("/N"): appearances})


def _ssn_appearance(writer, widget, text: str, font) -> None:
    """Fit the nine characters inside the template's printed 3/2/4 groups."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    x0, y0, x1, y1 = (float(v) for v in widget["/Rect"])
    width, height = x1-x0, y1-y0
    # The verified template's separators are not an evenly spaced nine-cell grid.
    first, second = (35.3, 59.6) if "Table_Dependents" in _full_name(widget) else (33.0, 56.6)
    commands = ["q BT /SFHelvetica 9.5 Tf 0 g"]
    for start, end, characters in ((0, first, text[:3]), (first, second, text[3:5]), (second, width, text[5:])):
        cell = (end-start)/len(characters)
        for index, character in enumerate(characters):
            x = start+index*cell+(cell-stringWidth(character, "Helvetica", 9.5))/2
            y = (height-9.5)/2+1.9
            commands.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm ({character}) Tj")
    commands.append("ET Q")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    stream.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"), NameObject("/BBox"): ArrayObject([FloatObject(v) for v in (0, 0, width, height)]), NameObject("/Resources"): DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/SFHelvetica"): font})})})
    widget[NameObject("/AP")] = DictionaryObject({NameObject("/N"): writer._add_object(stream)})


def render_official_pdf(state: dict[str, Any], appendix: bytes) -> bytes:
    from reportlab.pdfgen.canvas import Canvas

    if state.get("tax_year", 2025) != 2025:
        raise ValueError("The official template supports tax year 2025 only.")
    template = TEMPLATE.read_bytes()
    if hashlib.sha256(template).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("The official PDF template does not match the verified 2025 field map.")
    source = PdfReader(io.BytesIO(template))
    writer = PdfWriter()
    writer.clone_document_from_reader(source)
    acroform = writer.root_object["/AcroForm"].get_object()
    acroform.pop(NameObject("/XFA"), None)  # viewers must use the updated AcroForm, not stale XFA packets
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica"), NameObject("/Encoding"): NameObject("/WinAnsiEncoding")}))
    acroform["/DR"]["/Font"][NameObject("/SFHelvetica")] = font
    values = field_values(state)
    missing = values.keys() - (writer.get_fields() or {}).keys()
    if missing:
        raise ValueError(f"Official form fields missing: {sorted(missing)}")
    reverse = {name: line for line, name in LINE_FIELDS.items()} | ALIASES
    widgets, ssn_widgets = [], []
    for page_number, page in enumerate(writer.pages):
        for ref in page.get("/Annots", []):
            widget = ref.get_object()
            if widget.get("/Subtype") != "/Widget":
                continue
            name = _full_name(widget)
            if widget.get("/FT") == "/Btn":
                values.setdefault(name, "/Off")
                _checkbox_appearances(writer, widget)
            if name in values and widget.get("/FT") == "/Tx":
                widget[NameObject("/DA")] = TextStringObject("/SFHelvetica 0 Tf 0 g")
                widget[NameObject("/Q")] = NumberObject(2 if name in reverse else 0)
                if str(values[name]).startswith("XXXXX") and len(values[name]) == 9:
                    widget[NameObject("/Ff")] = NumberObject(int(widget.get("/Ff", 0)) | (1 << 24))
                    widget[NameObject("/MaxLen")] = NumberObject(9)
                    widget[NameObject("/DA")] = TextStringObject("/SFHelvetica 9.5 Tf 0 g")
                    ssn_widgets.append((widget, values[name]))
            if name in reverse:
                line = reverse[name]
                provenance = (state.get("provenance") or {}).get(line) or {}
                widget[NameObject("/TU")] = TextStringObject(pii.mask(f"Line {line}; {provenance.get('method', 'not yet computed')}; sources: {', '.join(provenance.get('sources') or [])}. Click the adjacent line label for the provenance appendix."))
                widgets.append((page_number, list(widget["/Rect"]), line))
    writer.update_page_form_field_values(None, values, auto_regenerate=False)
    for widget, text in ssn_widgets:
        _ssn_appearance(writer, widget, text, font)
    for page in writer.pages:
        stamp = io.BytesIO()
        canvas = Canvas(stamp, pagesize=(612, 792))
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColorRGB(.45, .15, .08)
        canvas.drawString(36, 780, "SILVERFILE DRAFT - NOT FILED | SSNs masked | Linked line labels open the source appendix")
        canvas.saveState()
        canvas.setFillAlpha(.065)
        canvas.setFillColorRGB(.25, .25, .25)
        canvas.translate(105, 260)
        canvas.rotate(32)
        canvas.setFont("Helvetica-Bold", 42)
        canvas.drawString(0, 0, "DRAFT - NOT FILED")
        canvas.restoreState()
        canvas.save()
        page.merge_page(PdfReader(io.BytesIO(stamp.getvalue())).pages[0])
    audit = PdfReader(io.BytesIO(appendix))
    destinations = {}
    for index, page in enumerate(audit.pages):
        text = page.extract_text()
        for line in LINE_FIELDS:
            if f"Line {line} " in text:
                destinations.setdefault(line, 4 + index)
    writer.append(audit)
    for page_number, rectangle, line in widgets:
        if line in destinations and line in (state.get("lines") or {}):
            x0, y0, _, y1 = (float(value) for value in rectangle)
            writer.add_annotation(page_number, Link(rect=(x0-24, y0, x0-1, y1), target_page_index=destinations[line], fit=Fit.fit()))
    writer.add_metadata({"/Title": "2025 Form 1040-SR - SilverFile draft", "/Author": "SilverFile - draft, not filed", "/Subject": "Official form layout with masked identifiers and a provenance appendix", "/SilverFileTemplateSHA256": TEMPLATE_SHA256})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
