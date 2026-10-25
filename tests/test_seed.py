"""The demo seed (seed.py): a fresh data directory gets six real returns in the
states a preparer meets, seeding is idempotent, and it is off unless asked for."""
from __future__ import annotations

import pytest

from silverfile import seed, service


@pytest.fixture
def fresh_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SILVERFILE_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("SILVERFILE_SEED_DEMO", raising=False)
    return tmp_path


def test_seed_fills_an_empty_index_with_every_state(fresh_data_dir):
    ids = seed.seed_demo_returns()
    assert len(ids) == len(seed.DEMO_RETURNS)
    rows = {r["return_id"]: r for r in service.list_returns()}
    assert set(rows) == set(ids)
    statuses = sorted(r["status"] for r in rows.values())
    assert statuses == ["complete", "complete", "halted", "intake", "waiting_human", "waiting_human"]
    assert sum(r["waiting_on_human"] for r in rows.values()) == 2
    names = {r["taxpayer_name"] for r in rows.values()}
    assert {"Eleanor Whitfield", "Harold Benson", "Ruth Adler", "Samuel Ortega", "Margaret Okafor"} <= names
    # a completed return carries a drafted form; the conversation one is waiting on a question
    done = next(r for r in rows.values() if r["status"] == "complete" and r["taxpayer_name"] == "Eleanor Whitfield")
    assert service.get_state(done["return_id"])["lines"]
    chat = next(r for r in rows.values() if r["status"] == "intake")
    assert service.pending(chat["return_id"])["type"] == "question"


def test_seed_is_a_no_op_when_returns_exist(fresh_data_dir):
    first = seed.seed_demo_returns()
    assert first
    assert seed.seed_demo_returns() == []
    assert len(service.list_returns()) == len(first)
    assert len(seed.seed_demo_returns(force=True)) == len(seed.DEMO_RETURNS)


def test_seed_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("SILVERFILE_SEED_DEMO", raising=False)
    assert not seed.seed_enabled()
    monkeypatch.setenv("SILVERFILE_SEED_DEMO", "1")
    assert seed.seed_enabled()
    monkeypatch.setenv("SILVERFILE_SEED_DEMO", "0")
    assert not seed.seed_enabled()
