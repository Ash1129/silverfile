"""Structural guarantees, checked against the code itself rather than its behaviour.

- Never file: there is no outbound filing path. No module opens a network
  connection except the Anthropic client (model calls) and the local MCP stdio
  pipe; no function, route or status is about filing, submitting or transmitting.
- Least privilege: each agent imports only what its job needs and calls only
  its tax_math functions; models are given no tools.
- Secrets: no key is tracked in git, and the ignore files cover every secret
  and runtime store. The container runs unprivileged.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from conftest import ROOT
from silverfile.dashboard.app import create_app
from silverfile.security.capabilities import AGENT_TOOLS
from silverfile.state import ReturnStatus

SRC = ROOT / "src" / "silverfile"
NETWORK = {"socket", "ssl", "smtplib", "ftplib", "telnetlib", "poplib", "imaplib", "xmlrpc", "requests", "urllib3", "aiohttp", "httpx", "http", "urllib", "websocket", "websockets", "paramiko", "subprocess", "asyncssh"}
PARSING_ONLY = {"urllib.parse", "http.cookies", "http"}  # string parsing and status codes; no connection
NETWORK_ALLOWED = {("anthropic", "llm.py"), ("anthropic", "gate.py"), ("mcp", "client.py"), ("mcp", "server.py"),
                   ("subprocess", "keysetup.py")}  # one local `git check-ignore .env` during setup; no network
FILING_WORDS = re.compile(r"\b(e-?file|efile|submit_return|file_return|transmit|irs\.gov|modernized e-file|mef_|send_to_irs)\b", re.I)


def _modules() -> dict[Path, ast.Module]:
    return {p: ast.parse(p.read_text()) for p in SRC.rglob("*.py")}


def _imports(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add(("." * node.level) + (node.module or ""))
    return out


# ---------------------------------------------------------------- never file
def test_no_module_opens_a_network_connection_of_its_own():
    offenders = []
    for path, tree in _modules().items():
        for mod in _imports(tree):
            top = mod.lstrip(".").split(".")[0]
            if mod in PARSING_ONLY:
                continue
            if (top in NETWORK or top in ("anthropic", "mcp")) and (top, path.name) not in NETWORK_ALLOWED:
                offenders.append(f"{path.relative_to(SRC)} imports {mod}")
    assert offenders == []


def test_nothing_in_the_code_is_about_filing():
    hits = []
    for path in SRC.rglob("*"):
        if path.suffix in (".py", ".html"):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if FILING_WORDS.search(line):
                    hits.append(f"{path.relative_to(SRC)}:{i}: {line.strip()[:80]}")
    assert hits == []
    assert not {"filed", "submitted", "transmitted"} & set(ReturnStatus.__args__)
    paths = [r.path for r in create_app().routes]
    assert not [p for p in paths if re.search(r"file|submit|transmit|irs", p.replace("/form", ""))]


def test_the_model_door_takes_no_tools():
    src = (SRC / "llm.py").read_text()
    call = re.search(r"messages\.create\((.*?)\)\n", src).group(1)
    assert "tools" not in call and "tool_choice" not in call
    assert "tools" not in re.search(r"def complete\((.*?)\)", src, re.S).group(1)


# ---------------------------------------------------------------- least privilege
LANES = {
    # agent module -> silverfile modules it must not import
    "intake.py": {"tax_math", "service", "dashboard", "agents.computation", "agents.review"},
    "extraction.py": {"tax_math", "service", "dashboard", "agents.computation"},
    "eligibility.py": {"llm", "service", "dashboard", "tax_math.client"},
    "computation.py": {"llm", "service", "dashboard", "agents.extraction"},
    "review.py": {"service", "dashboard", "agents.computation", "tax_math.client"},
    "orchestrator.py": {"llm", "tax_math", "service", "dashboard"},
    "escalation.py": {"llm", "tax_math", "service", "dashboard"},
}


def test_each_agent_imports_only_its_lane():
    for name, banned in LANES.items():
        tree = ast.parse((SRC / "agents" / name).read_text())
        imported = {m.lstrip(".") for m in _imports(tree)}
        bad = [m for m in imported for b in banned if m == b or m.startswith(b + ".") or m.endswith("." + b)]
        assert bad == [], f"{name} imports {bad}"


def _attrs_of(name: str, var: str = "tm") -> set[str]:
    tree = ast.parse((SRC / "agents" / name).read_text())
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == var}


def test_the_tool_allowance_matches_what_each_agent_calls():
    assert _attrs_of("eligibility.py") <= AGENT_TOOLS["eligibility"] | {"FILING_STATUSES"}
    assert _attrs_of("review.py") <= AGENT_TOOLS["review"]
    tree = ast.parse((SRC / "agents" / "computation.py").read_text())
    called = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "call" and n.args and isinstance(n.args[0], ast.Constant)}
    assert called == AGENT_TOOLS["computation"]
    assert "is_1040sr_eligible" not in AGENT_TOOLS["computation"] and not AGENT_TOOLS["eligibility"] & {"income_tax", "sum_amounts"}


def test_an_agent_cannot_reach_a_tool_outside_its_allowance():
    import pytest

    from silverfile.agents import eligibility, review
    from silverfile.security.capabilities import ToolNotAllowed
    from silverfile.tax_math.client import TaxMathClient

    assert not hasattr(eligibility.tm, "income_tax") and not hasattr(review.tm, "taxable_1099r")
    with TaxMathClient(direct=True, agent="eligibility") as c, pytest.raises(ToolNotAllowed):
        c.call("income_tax", filing_status="single", taxable_income=1)


# ---------------------------------------------------------------- secrets and the container
SECRET_PATTERNS = [re.compile(p) for p in (r"sk-ant-(api|admin)\d{2}-[A-Za-z0-9_\-]{20,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"AKIA[0-9A-Z]{16}", r"ghp_[A-Za-z0-9]{30,}")]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def test_no_secret_is_tracked_in_git():
    tracked = _git("ls-files").split()
    assert ".env" not in tracked and not [t for t in tracked if t.endswith((".sqlite", ".key", ".pem", ".data_key"))]
    leaks = []
    for t in tracked:
        p = ROOT / t
        if p.is_file() and p.stat().st_size < 2_000_000:
            text = p.read_text(errors="ignore")
            leaks += [f"{t}: {rx.pattern[:20]}" for rx in SECRET_PATTERNS if rx.search(text)]
    assert leaks == []


def test_ignore_files_cover_secrets_and_runtime_data():
    for path in (".env", ".env.local", ".env.production", "data/runtime/checkpoints.sqlite", "data/runtime/.data_key", "server.key", "x.pem", "data/runtime/uploads/ret-1/abc.enc"):
        assert _git("check-ignore", path).strip() == path, f"{path} is not gitignored"
    assert _git("check-ignore", ".env.example").strip() == ""
    docker_ignored = (ROOT / ".dockerignore").read_text().split()
    assert {".env", ".env.*", "data/runtime", "*.sqlite", ".git"} <= set(docker_ignored)


def test_the_container_runs_unprivileged_with_no_baked_in_secret():
    df = (ROOT / "Dockerfile").read_text()
    users = re.findall(r"^USER\s+(\S+)", df, re.M)
    assert users and users[-1] not in ("root", "0")
    assert "SILVERFILE_ENV=production" in df
    assert not re.search(r"(ANTHROPIC_API_KEY|SILVERFILE_DATA_KEY|PASSWORD)\s*=\s*\S", df)
    compose = (ROOT / "docker-compose.yml").read_text()
    for needle in ("read_only: true", "no-new-privileges:true", "cap_drop", "127.0.0.1:8000:8000"):
        assert needle in compose, needle
