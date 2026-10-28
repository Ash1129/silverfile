"""Upload handling: what a taxpayer's file has to pass before any agent reads it.

1. Type allowlist by content: JPEG, PNG or PDF, identified by magic bytes. The
   extension must agree with the bytes; the browser's Content-Type is ignored.
2. Size: at most SILVERFILE_MAX_UPLOAD_MB per file (10 by default) and
   MAX_FILES per request; images at most MAX_PIXELS (decompression bombs).
3. Images are decoded and re-encoded with Pillow: EXIF (GPS position, device,
   time) and every other metadata chunk are dropped, and anything appended
   after the image data (a polyglot file) does not survive the re-encode.
4. PDFs are parsed with pypdf and refused if encrypted, too long, or carrying
   active content (JavaScript, launch actions, embedded files, forms).
5. Storage: under the data directory (never under the web root; no route serves
   uploads), in a folder per return, under a random name, encrypted with the
   data key (security/crypto.py), mode 0600. The original file name is kept
   only as a cleaned label.
Nothing uploaded is ever executed or rendered: images go to the vision model as
bytes, and the dashboard shows only the label and the extracted fields (escaped).
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from ..config import settings
from ..provenance import now_iso
from . import crypto

MAX_FILES = 10
MAX_PIXELS = 40_000_000
MAX_SIDE = 12_000
MAX_PDF_PAGES = 20
ENCRYPTED_SUFFIX = ".enc"
TYPES: dict[str, dict[str, Any]] = {
    "jpeg": {"magic": (b"\xff\xd8\xff",), "suffixes": {".jpg", ".jpeg"}, "media_type": "image/jpeg"},
    "png": {"magic": (b"\x89PNG\r\n\x1a\n",), "suffixes": {".png"}, "media_type": "image/png"},
    "pdf": {"magic": (b"%PDF-",), "suffixes": {".pdf"}, "media_type": "application/pdf"},
}
ACCEPT = ",".join(sorted(s for t in TYPES.values() for s in t["suffixes"]))
# PDF names that run code, open other things or carry files: none belong on a tax form.
PDF_ACTIVE = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/OpenAction", b"/AA", b"/RichMedia", b"/XFA", b"/AcroForm", b"/SubmitForm", b"/ImportData", b"/GoToR")

Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow raises DecompressionBombError past twice this, and warns past it


class UploadRejected(ValueError):
    """A file that is not accepted; the message is safe to show to the person who sent it."""


def max_bytes() -> int:
    return int(float(os.environ.get("SILVERFILE_MAX_UPLOAD_MB", "10")) * 1024 * 1024)


def sniff(data: bytes) -> str | None:
    for name, t in TYPES.items():
        if any(data.startswith(m) for m in t["magic"]):
            return name
    return None


def clean_label(filename: str) -> str:
    name = Path(filename or "upload").name
    return (re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip("._ ") or "upload")[:80]


def check(filename: str, data: bytes) -> tuple[str, bytes]:
    """(type name, the bytes to store), or UploadRejected."""
    if not data:
        raise UploadRejected("The file is empty.")
    if len(data) > max_bytes():
        raise UploadRejected(f"The file is larger than {max_bytes() // (1024 * 1024)} MB.")
    kind = sniff(data)
    if kind is None:
        raise UploadRejected("Only JPEG or PNG photos and PDF files are accepted.")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in TYPES[kind]["suffixes"]:
        raise UploadRejected(f"The file's contents are {kind.upper()} but its name ends in {suffix or '(nothing)'}; rename it or upload the original.")
    return kind, (_clean_image(data, kind) if kind in ("jpeg", "png") else _check_pdf(data))


def _clean_image(data: bytes, kind: str) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()  # structure check without decoding the pixels
        with Image.open(io.BytesIO(data)) as img:
            if img.format != kind.upper():
                raise UploadRejected("The image's contents do not match its type.")
            w, h = img.size
            if w * h > MAX_PIXELS or max(w, h) > MAX_SIDE:
                raise UploadRejected("The image's dimensions are too large.")
            img = ImageOps.exif_transpose(img)  # keep the photo upright once the orientation tag is gone
            if kind == "jpeg" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            # A fresh encode from pixels: no exif=, no icc_profile=, no pnginfo= -> no metadata carried over
            if kind == "jpeg":
                img.save(out, format="JPEG", quality=92)
            else:
                img.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except UploadRejected:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as e:
        raise UploadRejected("The image could not be read; it may be damaged.") from e


def _check_pdf(data: bytes) -> bytes:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    found = [n.decode() for n in PDF_ACTIVE if re.search(re.escape(n) + rb"(?![A-Za-z])", data)]
    if found:
        raise UploadRejected("PDFs with scripts, actions, forms or attached files are not accepted; print the form to a plain PDF or upload a photo.")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise UploadRejected("Password-protected PDFs are not accepted.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise UploadRejected(f"PDFs of more than {MAX_PDF_PAGES} pages are not accepted.")
        # the byte scan above misses names inside compressed object streams; the parsed catalog does not
        root = reader.trailer["/Root"]
        names = root.get("/Names") or {}
        if any(k in root for k in ("/OpenAction", "/AA", "/AcroForm")) or any(k in names for k in ("/JavaScript", "/EmbeddedFiles")):
            raise UploadRejected("PDFs with scripts, actions, forms or attached files are not accepted; print the form to a plain PDF or upload a photo.")
    except UploadRejected:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, RecursionError) as e:
        raise UploadRejected("The PDF could not be read; it may be damaged.") from e
    return data


def store(return_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """Check the file and write it encrypted under a random name. Returns a DocumentInput (kind not yet inferred)."""
    kind, clean = check(filename, data)
    folder = settings().uploads_dir / Path(return_id).name
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(16)
    dest = folder / f"{token}{ENCRYPTED_SUFFIX}"
    dest.write_bytes(crypto.encrypt(clean))
    crypto.restrict_file(dest)
    return {
        "id": f"upload-{token[:10]}", "kind": "unknown", "path": str(dest), "declared_by": "taxpayer", "uploaded_at": now_iso(),
        "media_type": TYPES[kind]["media_type"], "original_name": clean_label(filename), "sha256": hashlib.sha256(clean).hexdigest(), "size": len(clean),
    }


def read_bytes(path: str | Path) -> bytes:
    """A stored document's bytes: decrypted for an upload, as-is for a fixture or demo file."""
    p = Path(path)
    raw = p.read_bytes()
    return crypto.decrypt(raw) if p.suffix == ENCRYPTED_SUFFIX else raw
