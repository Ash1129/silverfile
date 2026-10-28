"""Rate limits on model calls and the per-return token budget, and retention."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from silverfile import llm, service
from silverfile.agents.extraction import extract_one
from silverfile.security import audit, auth, ratelimit, retention
from conftest import CASE1, DEMO, load_intake

SYSTEM = auth.system("test")


class _FakeAnthropic:
    """Stands in for the SDK client: counts calls, reports usage."""

    def __init__(self) -> None:
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        assert "tools" not in kw
        return SimpleNamespace(content=[SimpleNamespace(text="{}")], usage=SimpleNamespace(input_tokens=400, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0))


@pytest.fixture
def fake_model(monkeypatch, data_dir):
    fake = _FakeAnthropic()
    monkeypatch.setattr(llm, "client", lambda: fake)
    return fake


def test_calls_are_charged_to_the_return_and_stop_at_the_cap(fake_model, monkeypatch):
    monkeypatch.setenv("SILVERFILE_MAX_TOKENS_PER_RETURN", "1500")
    token = llm.current_return.set("ret-budget")
    try:
        llm.complete("s", [{"role": "user", "content": "x"}], model="m", max_tokens=200)
        llm.complete("s", [{"role": "user", "content": "x"}], model="m", max_tokens=200)
        assert ratelimit.spent("ret-budget") == 1000
        with pytest.raises(ratelimit.BudgetExceeded, match="model tokens"):
            llm.complete("s", [{"role": "user", "content": "x"}], model="m", max_tokens=600)
    finally:
        llm.current_return.reset(token)
    assert fake_model.calls == 2  # the refused call never reached the API
    assert audit.recent(1, kind="budget_exceeded")[0]["return_id"] == "ret-budget"
    assert ratelimit.spent("ret-other") == 0  # budgets are per return


def test_calls_per_minute_are_limited(fake_model, monkeypatch):
    monkeypatch.setenv("SILVERFILE_LLM_CALLS_PER_MINUTE", "3")
    token = llm.current_return.set("ret-rate")
    try:
        for _ in range(3):
            llm.complete("s", [{"role": "user", "content": "x"}], model="m", max_tokens=10)
        with pytest.raises(ratelimit.BudgetExceeded, match="rate limit"):
            llm.complete("s", [{"role": "user", "content": "x"}], model="m", max_tokens=10)
    finally:
        llm.current_return.reset(token)
    assert fake_model.calls == 3


def test_the_return_scope_reaches_the_agents_inside_the_graph(data_dir, monkeypatch):
    seen: list = []
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: seen.append(llm.current_return.get()) or llm.LLMResult('{"intent": "answer", "updates": {"answering_party": "self"}}', 5, 5, "fake"))
    rid = service.create_return([], None, actor=SYSTEM)
    service.run(rid)
    service.answer(rid, "yes, it's me", actor=auth.taxpayer_principal(rid))
    assert seen == [rid] and llm.current_return.get() is None


def test_an_exhausted_budget_hands_the_document_to_a_person(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)

    def broke(*a, **k):
        raise ratelimit.BudgetExceeded("return ret-x has used 250000 of its 250000 model tokens")

    monkeypatch.setattr(llm, "complete", broke)
    rec = extract_one({"id": "w2", "kind": "W-2", "path": str(DEMO / "w2_image_1.jpg")})
    assert rec["confidence"] == 0.0 and "model budget reached" in rec["issues"][0] and rec["fields"] == {}


def test_request_rate_limits():
    w = ratelimit.SlidingWindow(2, 60)
    w.hit("ip")
    w.hit("ip")
    with pytest.raises(ratelimit.RateLimited):
        w.hit("ip")
    w.hit("other-ip")


# ---------------------------------------------------------------- retention
def _age(data_dir, rid: str, iso: str) -> None:
    with sqlite3.connect(data_dir / "returns.sqlite") as db:
        db.execute("UPDATE returns SET updated_at = ? WHERE return_id = ?", (iso, rid))


def test_abandoned_returns_are_deleted_with_everything_they_hold(data_dir):
    jpeg = (DEMO / "w2_image_1.jpg").read_bytes()
    stale = service.create_return([], None, actor=SYSTEM)
    service.run(stale)
    doc = service.save_upload(stale, "w2.jpg", jpeg, actor=SYSTEM)
    code = auth.issue_access_code(stale)
    fresh = service.create_return([], None, actor=SYSTEM)
    done = service.create_return(service.fixture_documents("case1_clean"), load_intake(CASE1), actor=SYSTEM)
    service.run(done)
    for rid in (stale, done):
        _age(data_dir, rid, "2020-01-01T00:00:00+00:00")
    gone = retention.purge_abandoned(30)
    assert gone == [stale]
    with pytest.raises(KeyError):
        service.get_state(stale)
    assert service.index_row(stale) is None and auth.redeem_access_code(code) is None
    assert not (data_dir / "uploads" / stale).exists() and not __import__("pathlib").Path(doc["path"]).exists()
    assert service.index_row(fresh) and service.index_row(done)  # recent, or finished and kept for the preparer
    ev = audit.recent(1, kind="return_deleted")[0]
    assert ev["return_id"] == stale and ev["actor"] == "system:retention"
    assert retention.purge_abandoned(30, include_finished=True) == [done]
