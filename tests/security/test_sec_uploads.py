"""Malicious upload rejection.

Only JPEG, PNG and PDF, identified by their bytes; the name has to agree; size
and pixel limits hold; metadata and anything appended to an image are gone;
active PDFs are refused; files are stored encrypted under random names that
no route serves."""
from __future__ import annotations

import io
import os
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import ROOT
from silverfile import service
from silverfile.security import audit, auth, uploads
from webhelpers import answer, start_taxpayer, token, interrupt_id

JPEG = (ROOT / "demo" / "w2_image_1.jpg").read_bytes()


def _png(w=40, h=30) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(b, format="PNG")
    return b.getvalue()


def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (60, 40), "white")
    exif = Image.Exif()
    exif[0x010F] = "SpyCam Inc"  # Make
    exif[0x8825] = {2: (42.0, 26.0, 40.0), 4: (76.0, 29.0, 49.0)}  # GPS latitude / longitude
    b = io.BytesIO()
    img.save(b, format="JPEG", exif=exif.tobytes())
    return b.getvalue()


def _pdf(extra: bytes = b"") -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(612, 792)
    b = io.BytesIO()
    w.write(b)
    data = b.getvalue()
    return data.replace(b"%%EOF", extra + b"\n%%EOF") if extra else data


@pytest.mark.parametrize("name, data, why", [
    ("w2.jpg", b"MZ\x90\x00\x03" + b"\x00" * 200, "Only JPEG or PNG photos and PDF files"),  # a Windows executable renamed
    ("w2.png", b"<html><script>alert(1)</script></html>", "Only JPEG or PNG"),
    ("w2.jpg", b"#!/bin/sh\nrm -rf /\n", "Only JPEG or PNG"),
    ("w2.svg", b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>", "Only JPEG or PNG"),
    ("w2.json", b'{"kind": "W-2", "wages": 1}', "Only JPEG or PNG"),
    ("w2.png", JPEG, "contents are JPEG but its name ends in .png"),
    ("w2.exe", JPEG, "contents are JPEG but its name ends in .exe"),
    ("w2.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64, "could not be read"),  # a JPEG header and nothing else
    ("empty.jpg", b"", "empty"),
], ids=lambda value: f"binary-{len(value)}" if isinstance(value, bytes) else value)
def test_wrong_types_and_lying_names_are_refused(name, data, why):
    with pytest.raises(uploads.UploadRejected, match=why):
        uploads.check(name, data)


def test_size_limit(monkeypatch):
    monkeypatch.setenv("SILVERFILE_MAX_UPLOAD_MB", "0.001")
    with pytest.raises(uploads.UploadRejected, match="larger than"):
        uploads.check("w2.jpg", JPEG)


def test_decompression_bomb_is_refused():
    # 20000 x 20000 pixels of white: a few hundred KB compressed, 1.2 GB decoded
    raw = zlib.compress(b"\x00" + b"\xff" * 3 * 20000, 9)
    bomb = _png(1, 1)
    # rewrite the header to claim huge dimensions; Pillow refuses before decoding
    big = bytearray(bomb)
    big[16:24] = (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big")
    with pytest.raises(uploads.UploadRejected):
        uploads.check("w2.png", bytes(big))
    assert raw  # (the payload a real bomb would carry)


def test_exif_and_gps_are_stripped():
    data = _jpeg_with_gps()
    assert Image.open(io.BytesIO(data)).getexif().get(0x010F) == "SpyCam Inc"
    kind, clean = uploads.check("photo.jpg", data)
    img = Image.open(io.BytesIO(clean))
    assert kind == "jpeg" and not img.getexif() and "exif" not in img.info
    assert b"SpyCam" not in clean


def test_a_polyglot_trailer_does_not_survive():
    zip_trailer = b"PK\x03\x04" + b"malicious.sh" + b"\x00" * 40
    kind, clean = uploads.check("w2.jpg", JPEG + zip_trailer)
    assert b"PK\x03\x04" not in clean and b"malicious" not in clean


@pytest.mark.parametrize("payload", [b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >>", b"/Launch << /F (cmd.exe) >>", b"/EmbeddedFile 5 0 R", b"/AcroForm << >>"])
def test_active_pdfs_are_refused(payload):
    with pytest.raises(uploads.UploadRejected, match="scripts, actions, forms or attached files"):
        uploads.check("1099.pdf", _pdf(payload))


def test_a_plain_pdf_is_accepted_and_unchanged():
    data = _pdf()
    assert uploads.check("1099.pdf", data) == ("pdf", data)


def test_stored_under_a_random_name_encrypted_and_never_served(web, data_dir):
    doc = uploads.store("ret-0123456789abcdef0123456789abcdef", "../../etc/passwd W2 (1).jpg", JPEG)
    p = Path(doc["path"])
    assert p.is_relative_to(Path(data_dir) / "uploads") and p.suffix == ".enc" and len(p.stem) == 32
    assert "passwd" not in str(p) and doc["original_name"] == "passwd W2 _1_.jpg"
    assert p.read_bytes()[:3] != b"\xff\xd8\xff" and uploads.read_bytes(p)[:3] == b"\xff\xd8\xff"
    if os.name != "nt":  # Windows chmod does not expose POSIX owner/group permission bits.
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    c = TestClient(web)
    for url in (f"/static/../uploads/{p.parent.name}/{p.name}", f"/uploads/{p.parent.name}/{p.name}", f"/static/{p.name}"):
        assert c.get(url).status_code == 404


def test_the_upload_step_refuses_a_bad_file_and_logs_it(web):
    c = TestClient(web)
    start_taxpayer(c)
    rid = auth.load_session(c.cookies["sf_taxpayer"], "taxpayer").subject
    # drive the conversation to the upload step the quick way: answer every question with the scripted script
    for text in service.fixture_answers("case1_clean"):
        page = c.get("/t").text
        if 'action="/t/upload"' in page:
            break
        answer(c, text)
    page = c.get("/t").text
    assert 'action="/t/upload"' in page
    r = c.post("/t/upload", data={"csrf_token": token(page), "interrupt_id": interrupt_id(page), "text": "done"}, files={"files": ("w2.jpg", b"MZ\x90\x00not an image", "image/jpeg")})
    assert r.status_code == 400 and "Only JPEG or PNG" in r.text
    assert service.pending(rid)["type"] == "upload"  # still waiting: the bad file never reached the graph
    ev = audit.recent(1, kind="upload_rejected")[0]
    assert ev["return_id"] == rid and ev["actor"] == f"taxpayer:{rid}"
    r = c.post("/t/upload", data={"csrf_token": token(page), "interrupt_id": interrupt_id(page), "text": "done"}, files={"files": ("w2.jpg", JPEG, "image/jpeg")}, follow_redirects=False)
    assert r.status_code == 303
    docs = service.get_state(rid)["documents_in"]
    assert docs[-1]["media_type"] == "image/jpeg" and docs[-1]["path"].endswith(".enc")


def test_documents_must_come_from_an_upload_or_a_fixture(data_dir):
    rid = service.create_return([], None, actor=auth.system("test"))
    with pytest.raises(service.ServiceError):
        service._trusted_document({"id": "x", "kind": "W-2", "path": "/etc/passwd"})
    with pytest.raises(service.ServiceError):
        service._trusted_document({"id": "x", "kind": "W-2", "path": str(ROOT / "README.md"), "evil": True})
    assert rid
