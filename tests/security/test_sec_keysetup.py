"""Entering your own keys: `python run.py setup` and `try --ask-key`.

The key goes only into .env (owner-only, gitignored) or, with --ask-key, only
into the server's memory. It is never echoed or printed, a malformed or
rejected key is never saved, and an existing data key is never replaced
(that would make every saved return unreadable)."""
from __future__ import annotations

import os
import stat

import pytest

from conftest import ROOT
from silverfile import keysetup
from silverfile.keysetup import SetupError, check_key_shape, run_setup

GOOD = "sk-ant-api03-" + "A" * 80
# Windows has no owner/group/other mode bits (chmod only sets read-only); there
# .env is protected by the folder's access list, which these tests do not check.
POSIX = os.name == "posix"


def _example(tmp_path):
    ex = tmp_path / ".env.example"
    ex.write_text("# comment kept\nANTHROPIC_API_KEY=\nSILVERFILE_DATA_KEY=\nSILVERFILE_DATA_DIR=data/runtime\n")
    return ex


def _run(tmp_path, secret: str, answer: str = "", check=lambda k: k):
    out: list[str] = []
    code = run_setup(env=tmp_path / ".env", example=_example(tmp_path), ask_secret=lambda _: secret, ask=lambda _: answer, say=out.append, check=check)
    return code, "\n".join(out), tmp_path / ".env"


def test_setup_saves_a_checked_key_owner_only_and_never_prints_it(tmp_path):
    code, printed, env = _run(tmp_path, "  " + GOOD + "  ")
    assert code == 0
    values = keysetup.read_values(env)
    assert values["ANTHROPIC_API_KEY"] == GOOD
    assert len(values["SILVERFILE_DATA_KEY"]) >= 40  # generated
    if POSIX:
        assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert GOOD not in printed and GOOD[:20] not in printed and "…AAAA" in printed
    text = env.read_text()
    assert "# comment kept" in text and "SILVERFILE_DATA_DIR=data/runtime" in text and text.count("ANTHROPIC_API_KEY=") == 1
    assert not list(tmp_path.glob(".env.*.tmp"))  # no temp file left behind


def test_blank_means_offline_and_still_generates_a_data_key(tmp_path):
    code, printed, env = _run(tmp_path, "")
    values = keysetup.read_values(env)
    assert code == 0 and values["ANTHROPIC_API_KEY"] == "" and values["SILVERFILE_DATA_KEY"]
    assert "offline" in printed


@pytest.mark.parametrize("bad", ["hello", "sk-ant-short", "sk-ant-api03-has a space" + "x" * 40, "sk-proj-" + "x" * 50, "sk-ant-api03-" + "x" * 40 + "\nEVIL=1"])
def test_a_malformed_key_is_refused_and_not_saved(tmp_path, bad):
    with pytest.raises(SetupError):
        check_key_shape(bad)
    code, _, env = _run(tmp_path, bad, answer="y")
    assert code == 1 and keysetup.read_values(env).get("ANTHROPIC_API_KEY") == ""
    assert "EVIL" not in env.read_text()


def test_a_key_anthropic_rejects_is_not_saved(tmp_path):
    def rejected(_):
        raise SetupError("Anthropic rejected that key. Check it in the console and try again.")

    code, printed, env = _run(tmp_path, GOOD, answer="y", check=rejected)
    assert code == 1 and "rejected" in printed and keysetup.read_values(env)["ANTHROPIC_API_KEY"] == ""


def test_offline_check_can_be_skipped_on_purpose(tmp_path):
    def unreachable(_):
        raise SetupError("Could not reach Anthropic to check the key. Try again in a moment.")

    assert _run(tmp_path, GOOD, answer="n", check=unreachable)[0] == 1
    code, _, env = _run(tmp_path, GOOD, answer="y", check=unreachable)
    assert code == 0 and keysetup.read_values(env)["ANTHROPIC_API_KEY"] == GOOD


def test_an_existing_data_key_is_never_replaced_and_a_key_can_be_kept(tmp_path):
    _run(tmp_path, GOOD)
    env = tmp_path / ".env"
    first = keysetup.read_values(env)
    code, printed, _ = _run(tmp_path, "")  # run again, keep everything
    again = keysetup.read_values(env)
    assert code == 0 and again == first and "left as it is" in printed and "Kept" in printed


@pytest.mark.skipif(not POSIX, reason="mode bits are POSIX only")
def test_a_loose_env_file_is_tightened(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=\n")
    os.chmod(env, 0o644)
    _run(tmp_path, "")
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_quoted_values_are_read_like_a_shell_would(tmp_path, monkeypatch):
    from silverfile import config

    env = tmp_path / ".env"
    env.write_text(f'export ANTHROPIC_API_KEY="{GOOD}"\nSILVERFILE_INTAKE_MODEL=\'claude-haiku-5-5\'\n')
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SILVERFILE_INTAKE_MODEL", raising=False)
    config._load_dotenv()
    assert os.environ["ANTHROPIC_API_KEY"] == GOOD and os.environ["SILVERFILE_INTAKE_MODEL"] == "claude-haiku-5-5"
    assert keysetup.read_values(env)["ANTHROPIC_API_KEY"] == GOOD


def test_the_real_env_file_is_gitignored():
    assert keysetup.git_ignored(ROOT / ".env") is True
