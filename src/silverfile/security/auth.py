"""Who may do what: staff accounts and roles, sessions, taxpayer access codes.

Two kinds of people use SilverFile, and they never share a door:
- Kushim staff log in with a username and password and get a role. The role's
  permissions decide what they see: a clerk opens returns and hands out access
  codes but never sees a draft; a preparer reads drafts and resolves
  escalations; an admin also reads the security log and deletes returns.
- A taxpayer has no account and no login by SSN. Starting a return issues a
  random access code (about 120 bits) shown once; the code opens a session
  bound to that one return. Typing an SSN, a name or a return id opens nothing.

Sessions are random 256-bit tokens in an HttpOnly cookie. The database keeps
only a keyed hash of each token, so a copy of auth.sqlite opens no session.
Sessions have an idle and an absolute lifetime, and each carries its own CSRF
token. Passwords are hashed with scrypt; five wrong passwords lock an account
for fifteen minutes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from ..config import settings
from . import audit, crypto

PERMISSIONS = {
    "list_returns", "create_return", "view_return", "view_draft", "resolve_escalation",
    "advance_return", "delete_return", "view_audit",
}
ROLES: dict[str, frozenset[str]] = {
    "clerk": frozenset({"list_returns", "create_return"}),
    "preparer": frozenset({"list_returns", "create_return", "view_return", "view_draft", "resolve_escalation", "advance_return"}),
    "admin": frozenset(PERMISSIONS),
}
STAFF_IDLE_SECONDS = 30 * 60
STAFF_MAX_SECONDS = 12 * 3600
TAXPAYER_IDLE_SECONDS = 60 * 60
TAXPAYER_MAX_SECONDS = 24 * 3600
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
MIN_PASSWORD = 12
USERNAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
SCRYPT = {"n": 2**14, "r": 8, "p": 1}

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
  username     TEXT PRIMARY KEY,
  pw_hash      TEXT NOT NULL,
  role         TEXT NOT NULL,
  created_at   REAL NOT NULL,
  disabled     INTEGER NOT NULL DEFAULT 0,
  failed       INTEGER NOT NULL DEFAULT 0,
  locked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  subject     TEXT NOT NULL,
  role        TEXT NOT NULL,
  csrf        TEXT NOT NULL,
  created_at  REAL NOT NULL,
  last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_codes (
  return_id   TEXT PRIMARY KEY,
  code_hash   TEXT NOT NULL UNIQUE,
  created_at  REAL NOT NULL
);
"""


class AuthError(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    """The authenticated actor behind a request: staff, a taxpayer, or the system itself (seeding, CLI)."""

    kind: str  # "staff" | "taxpayer" | "system"
    subject: str  # username, the taxpayer's return id, or a system label
    role: str  # a ROLES key, "taxpayer" or "system"
    csrf: str = ""

    def can(self, permission: str) -> bool:
        if self.kind == "system":
            return True
        return permission in ROLES.get(self.role, frozenset())

    def may_access(self, return_id: str) -> bool:
        """Read or act on this return at all (finer permissions still apply)."""
        if self.kind == "system":
            return True
        if self.kind == "taxpayer":
            return hmac.compare_digest(self.subject, return_id)
        return self.can("view_return")

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.subject}"


def system(label: str = "system") -> Principal:
    return Principal(kind="system", subject=label, role="system")


def _db() -> sqlite3.Connection:
    path = settings().auth_db
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if fresh:
        crypto.restrict_file(path)
    return conn


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return "scrypt${n}${r}${p}${salt}${dk}".format(**SCRYPT, salt=base64.b64encode(salt).decode(), dk=base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32, n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # so an unknown username costs the same time as a wrong password


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Passwords need at least {MIN_PASSWORD} characters.")
    if password.lower() in ("password1234", "123456789012", "silverfile123", "kushim123456"):
        raise ValueError("That password is too common.")


# ---------------------------------------------------------------- staff
def normalise_username(username: str) -> str:
    u = (username or "").strip().lower()
    if not (2 <= len(u) <= 40) or set(u) - USERNAME_CHARS:
        raise ValueError("Usernames are 2-40 characters: letters, digits, dot, dash, underscore.")
    return u


def create_staff(username: str, password: str, role: str) -> None:
    u = normalise_username(username)
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    check_password_policy(password)
    with _lock, _db() as conn:
        if conn.execute("SELECT 1 FROM staff WHERE username = ?", (u,)).fetchone():
            raise ValueError(f"staff account {u!r} already exists")
        conn.execute("INSERT INTO staff (username, pw_hash, role, created_at) VALUES (?, ?, ?, ?)", (u, hash_password(password), role, time.time()))
    audit.record("staff_created", actor="system", username=u, role=role)


def set_password(username: str, password: str) -> None:
    u = normalise_username(username)
    check_password_policy(password)
    with _lock, _db() as conn:
        if not conn.execute("UPDATE staff SET pw_hash = ?, failed = 0, locked_until = 0 WHERE username = ?", (hash_password(password), u)).rowcount:
            raise ValueError(f"no staff account {u!r}")
        conn.execute("DELETE FROM sessions WHERE kind = 'staff' AND subject = ?", (u,))
    audit.record("staff_password_set", actor="system", username=u)


def list_staff() -> list[dict]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT username, role, created_at, disabled, failed, locked_until FROM staff ORDER BY username")]


def staff_count() -> int:
    with _db() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM staff WHERE disabled = 0").fetchone()[0])


def bootstrap_admin_from_env() -> bool:
    """Create the first admin from SILVERFILE_ADMIN_USER / _PASSWORD when no staff account exists."""
    user, pw = os.environ.get("SILVERFILE_ADMIN_USER", "").strip(), os.environ.get("SILVERFILE_ADMIN_PASSWORD", "")
    if not (user and pw) or staff_count():
        return False
    create_staff(user, pw, "admin")
    return True


def authenticate(username: str, password: str, *, ip: str = "") -> Principal | None:
    """A staff Principal (without a session yet), or None. Wrong user and wrong password look the same."""
    try:
        u = normalise_username(username)
    except ValueError:
        verify_password(password or "", _DUMMY_HASH)
        audit.record("login_failed", actor=None, ip=ip, reason="malformed username")
        return None
    now = time.time()
    with _lock, _db() as conn:
        row = conn.execute("SELECT * FROM staff WHERE username = ?", (u,)).fetchone()
        if row is None or row["disabled"]:
            verify_password(password or "", _DUMMY_HASH)
            audit.record("login_failed", actor=u, ip=ip, reason="unknown or disabled account")
            return None
        if row["locked_until"] > now:
            audit.record("login_locked", actor=u, ip=ip)
            return None
        if not verify_password(password or "", row["pw_hash"]):
            failed = row["failed"] + 1
            locked = now + LOCKOUT_SECONDS if failed >= MAX_FAILED_LOGINS else 0
            conn.execute("UPDATE staff SET failed = ?, locked_until = ? WHERE username = ?", (0 if locked else failed, locked, u))
            audit.record("login_failed", actor=u, ip=ip, reason="wrong password", locked=bool(locked))
            return None
        conn.execute("UPDATE staff SET failed = 0, locked_until = 0 WHERE username = ?", (u,))
    audit.record("login_ok", actor=u, ip=ip, role=row["role"])
    return Principal(kind="staff", subject=u, role=row["role"])


# ---------------------------------------------------------------- sessions
def start_session(p: Principal) -> tuple[str, Principal]:
    """A fresh session (a new token every login: no fixation). Returns (cookie token, principal with its CSRF token)."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.time()
    with _lock, _db() as conn:
        conn.execute("INSERT INTO sessions (token_hash, kind, subject, role, csrf, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (crypto.lookup_hash(token), p.kind, p.subject, p.role, csrf, now, now))
    return token, Principal(kind=p.kind, subject=p.subject, role=p.role, csrf=csrf)


def load_session(token: str | None, kind: str) -> Principal | None:
    if not token or len(token) > 100:
        return None
    idle, longest = (STAFF_IDLE_SECONDS, STAFF_MAX_SECONDS) if kind == "staff" else (TAXPAYER_IDLE_SECONDS, TAXPAYER_MAX_SECONDS)
    now = time.time()
    h = crypto.lookup_hash(token)
    with _lock, _db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token_hash = ? AND kind = ?", (h, kind)).fetchone()
        if row is None:
            return None
        if now - row["last_seen"] > idle or now - row["created_at"] > longest:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (h,))
            return None
        if kind == "staff":
            staff = conn.execute("SELECT role, disabled FROM staff WHERE username = ?", (row["subject"],)).fetchone()
            if staff is None or staff["disabled"] or staff["role"] != row["role"]:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (h,))
                return None
        conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, h))
    return Principal(kind=row["kind"], subject=row["subject"], role=row["role"], csrf=row["csrf"])


def end_session(token: str | None) -> None:
    if not token:
        return
    with _lock, _db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (crypto.lookup_hash(token),))


def end_sessions_for_return(return_id: str) -> None:
    with _lock, _db() as conn:
        conn.execute("DELETE FROM sessions WHERE kind = 'taxpayer' AND subject = ?", (return_id,))


# ---------------------------------------------------------------- taxpayer access codes
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U: easier to read aloud and type


def _normalise_code(code: str) -> str:
    return "".join(c for c in (code or "").upper() if c.isalnum())


def issue_access_code(return_id: str) -> str:
    """A new code for the return (any earlier code stops working). Shown once; only its hash is kept."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(24))  # 24 * log2(30) ≈ 118 bits
    with _lock, _db() as conn:
        conn.execute("INSERT INTO access_codes (return_id, code_hash, created_at) VALUES (?, ?, ?) ON CONFLICT(return_id) DO UPDATE SET code_hash = excluded.code_hash, created_at = excluded.created_at",
                     (return_id, crypto.lookup_hash(raw), time.time()))
        conn.execute("DELETE FROM sessions WHERE kind = 'taxpayer' AND subject = ?", (return_id,))
    return "-".join(raw[i:i + 4] for i in range(0, 24, 4))


def redeem_access_code(code: str) -> str | None:
    """The return id the code opens, or None."""
    norm = _normalise_code(code)
    if len(norm) != 24:
        return None
    with _db() as conn:
        row = conn.execute("SELECT return_id FROM access_codes WHERE code_hash = ?", (crypto.lookup_hash(norm),)).fetchone()
    return row["return_id"] if row else None


def delete_access(return_id: str) -> None:
    with _lock, _db() as conn:
        conn.execute("DELETE FROM access_codes WHERE return_id = ?", (return_id,))
        conn.execute("DELETE FROM sessions WHERE kind = 'taxpayer' AND subject = ?", (return_id,))


def taxpayer_principal(return_id: str) -> Principal:
    return Principal(kind="taxpayer", subject=return_id, role="taxpayer")
