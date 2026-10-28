"""Encryption at rest and the server's integrity key.

One secret, SILVERFILE_DATA_KEY (in .env locally, a generated secret on the
host), is stretched with HKDF into two keys:
- a Fernet key (AES-128-CBC with HMAC-SHA256, so ciphertext is authenticated)
  for the checkpoint database, uploaded documents and the index's name and
  summary columns;
- an HMAC-SHA256 key for the provenance seal and the security event log.
Several comma-separated secrets may be given; the first encrypts, all decrypt,
which is how a key is rotated.

Without SILVERFILE_DATA_KEY a development checkout generates one into the data
directory (mode 0600) so `python run.py serve` works out of the box. That is a
convenience, not protection, because the key then sits next to the data: with
SILVERFILE_ENV=production (the Dockerfile sets it) a missing key stops the server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from langgraph.checkpoint.serde.base import CipherProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

DEV_KEY_FILE = ".data_key"
CIPHER_NAME = "fernet"


class KeyMissing(RuntimeError):
    pass


class Undecryptable(ValueError):
    """Ciphertext that fails authentication: tampered, truncated, or written with another key."""


def production() -> bool:
    return os.environ.get("SILVERFILE_ENV", "").strip().lower() == "production"


def _secrets() -> list[str]:
    raw = os.environ.get("SILVERFILE_DATA_KEY", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    if production():
        raise KeyMissing("SILVERFILE_DATA_KEY is not set. In production the data key must come from the environment (a secret store), never from the data directory.")
    from ..config import settings

    p = settings().data_dir / DEV_KEY_FILE
    if not p.exists():
        p.write_text(secrets.token_urlsafe(32))
        os.chmod(p, 0o600)
        sys.stderr.write(f"[silverfile] no SILVERFILE_DATA_KEY: generated a development key at {p} (set one in .env for anything real)\n")
    return [p.read_text().strip()]


def _derive(secret: str, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"silverfile-v1", info=purpose).derive(secret.encode())


@lru_cache(maxsize=4)
def _fernet(key_material: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([Fernet(base64.urlsafe_b64encode(_derive(s, b"encrypt-at-rest"))) for s in key_material])


def fernet() -> MultiFernet:
    return _fernet(tuple(_secrets()))


def mac_key() -> bytes:
    return _derive(_secrets()[0], b"integrity-hmac")


def encrypt(data: bytes) -> bytes:
    return fernet().encrypt(data)


def decrypt(token: bytes) -> bytes:
    try:
        return fernet().decrypt(token)
    except InvalidToken as e:
        raise Undecryptable("ciphertext failed authentication") from e


def encrypt_text(text: str | None) -> str | None:
    return None if text is None else encrypt(text.encode()).decode("ascii")


def decrypt_text(token: str | None) -> str | None:
    if token is None:
        return None
    try:
        return decrypt(token.encode("ascii")).decode()
    except (Undecryptable, UnicodeEncodeError):
        return None


def sign(data: bytes) -> str:
    return hmac.new(mac_key(), data, hashlib.sha256).hexdigest()


def verify_signature(data: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(data), signature or "")


def lookup_hash(secret_value: str) -> str:
    """A keyed hash for looking up a high-entropy secret (session token, access code) without storing it."""
    return hmac.new(mac_key(), secret_value.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- LangGraph checkpoint serde
class _FernetCipher(CipherProtocol):
    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        return CIPHER_NAME, encrypt(plaintext)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != CIPHER_NAME:
            raise Undecryptable(f"unexpected cipher {ciphername!r}")
        return decrypt(ciphertext)


class StrictEncryptedSerializer(EncryptedSerializer):
    """LangGraph's EncryptedSerializer reads unencrypted blobs as-is; this one refuses
    them, so a row written into the database by hand cannot become a return's state."""

    def loads_typed(self, data: tuple[str, bytes]):
        if "+" not in data[0] and data[0] not in ("empty", "null"):
            raise Undecryptable("unencrypted checkpoint blob refused")
        return super().loads_typed(data)


def checkpoint_serde() -> StrictEncryptedSerializer:
    return StrictEncryptedSerializer(_FernetCipher(), JsonPlusSerializer())


def restrict_file(path: Path) -> None:
    """Owner read/write only, for databases and uploads (no-op where chmod is unsupported)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
