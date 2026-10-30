"""`python run.py setup`: put your own keys in place, safely, in one step.

For a grader or anyone running their own copy. It
- creates `.env` from `.env.example` if there is none, owner-only (0600);
- asks for an Anthropic key with hidden input (never echoed, never printed back
  except as its last four characters), checks its shape, and checks it with
  Anthropic before saving; blank keeps the current one, or offline mode;
- generates SILVERFILE_DATA_KEY if it is empty, and never replaces one that is
  set, because that key decrypts every saved return;
- writes the file atomically (a 0600 temporary file renamed into place), so a
  crash cannot leave a half-written or world-readable `.env`;
- confirms `.env` is ignored by git.

Not saving the key at all is also an option: `python run.py try --ask-key`
asks for it in the browser and keeps it in the server's memory only.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .config import ROOT

KEY_PREFIX = "sk-ant-"
LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class SetupError(ValueError):
    pass


def check_key_shape(key: str) -> str:
    """The cleaned key, or SetupError. Shape only; `verify` asks Anthropic."""
    key = (key or "").strip().strip('"').strip("'")
    if not key.startswith(KEY_PREFIX) or len(key) < 40 or any(c.isspace() for c in key) or "=" in key or "#" in key:
        raise SetupError("That does not look like an Anthropic API key (they start with sk-ant- and have no spaces).")
    return key


def masked(key: str | None) -> str:
    return f"…{key[-4:]}" if key else "none"


def read_values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = LINE_RE.match(line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(1)] = unquote(m.group(2))
    return out


def unquote(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def write_values(path: Path, updates: dict[str, str]) -> None:
    """Set these variables in the file, keeping every other line and comment as it was."""
    lines = path.read_text().splitlines() if path.exists() else []
    done: set[str] = set()
    for i, line in enumerate(lines):
        m = LINE_RE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            done.add(m.group(1))
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def ensure_env_file(env: Path, example: Path) -> bool:
    """Create .env from the template (0600) if it is missing. True when it was created."""
    if env.exists():
        os.chmod(env, 0o600)
        return False
    template = example.read_text() if example.exists() else ""
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(template)
    return True


def git_ignored(path: Path) -> bool | None:
    """True/False from git, None when this is not a git checkout."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=path.parent, capture_output=True)
    except (OSError, FileNotFoundError):
        return None
    return {0: True, 1: False}.get(r.returncode)


def verify(key: str) -> str:
    """Ask Anthropic whether the key works (one cheap call). Raises SetupError with a safe message."""
    from .dashboard.gate import validate_key

    try:
        return validate_key(key)
    except ValueError as e:
        raise SetupError(str(e)) from None


def run_setup(*, env: Path | None = None, example: Path | None = None, ask_secret: Callable[[str], str], ask: Callable[[str], str],
              say: Callable[[str], None] = print, check: Callable[[str], str] = verify) -> int:
    env = env or ROOT / ".env"
    example = example or ROOT / ".env.example"
    created = ensure_env_file(env, example)
    who = "readable by you only" if os.name == "posix" else "protected by this folder's Windows permissions"
    say(f"{'Created' if created else 'Using'} {env.name} ({who}).")
    current = read_values(env)
    updates: dict[str, str] = {}

    have = current.get("ANTHROPIC_API_KEY") or None
    say("\nAnthropic API key: lets the agents call Claude. Get one at https://platform.claude.com/settings/keys")
    say("Leave blank to " + (f"keep the current key ({masked(have)})." if have else "run offline (scripted parser, same numbers, no cost)."))
    entered = ask_secret("Paste key (input hidden): ").strip()
    if entered:
        try:
            key = check_key_shape(entered)
            say("Checking the key with Anthropic…")
            key = check(key)
        except SetupError as e:
            say(f"Not saved: {e}")
            unreachable = "reach Anthropic" in str(e) or "returned an error" in str(e)
            if not entered.strip().startswith(KEY_PREFIX) or not unreachable:
                return 1  # a malformed or rejected key is never saved
            if ask("Anthropic could not be reached to check it. Save it anyway? [y/N] ").strip().lower() != "y":
                return 1
            key = check_key_shape(entered)
        updates["ANTHROPIC_API_KEY"] = key
        say(f"Saved the key ending {masked(key)}. It is billed to that key's account.")
    else:
        say(f"Kept: {masked(have)}." if have else "No key: offline mode.")

    if current.get("SILVERFILE_DATA_KEY"):
        say("\nSILVERFILE_DATA_KEY is already set; left as it is (changing it would make saved returns unreadable).")
    else:
        updates["SILVERFILE_DATA_KEY"] = secrets.token_urlsafe(32)
        say("\nGenerated SILVERFILE_DATA_KEY (encrypts saved returns and uploads; Docker needs it). Back up .env if the data matters.")

    if updates:
        write_values(env, updates)
    ignored = git_ignored(env)
    if ignored is False:
        say(f"\nWARNING: {env.name} is NOT ignored by git here. Do not commit it; add it to .gitignore.")
    elif ignored:
        say(f"\n{env.name} is ignored by git, so it cannot be committed by accident.")
    say("\nNext: python run.py try   (opens the app with demo logins)")
    return 0
