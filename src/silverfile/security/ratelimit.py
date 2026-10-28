"""Rate limits: on requests (by client address or session) and on model spend.

Request limits are sliding windows in memory: per-process, which is right for
the single-process deployment here; a multi-instance deployment would move
them to a shared store.

The model budget is per return and persisted (tokens survive a restart):
- at most SILVERFILE_LLM_CALLS_PER_MINUTE model calls per return per minute,
  and three times that across the whole server;
- at most SILVERFILE_MAX_TOKENS_PER_RETURN tokens (in plus out) per return.
llm.complete checks the budget before every call and records the spend after.
An exhausted budget raises BudgetExceeded; each agent then falls back to its
offline path or hands the item to a person, and the stop is logged.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import defaultdict, deque

from ..config import settings


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"too many requests; retry in {retry_after}s")
        self.retry_after = retry_after


class BudgetExceeded(RuntimeError):
    """The return's model budget (calls per minute or total tokens) is used up."""


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        """Count one request for `key`, or raise RateLimited."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise RateLimited(int(self.window - (now - q[0])) + 1)
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


LOGIN = SlidingWindow(10, 15 * 60)  # per client address, on top of the per-account lockout
CODE_REDEEM = SlidingWindow(10, 15 * 60)  # access-code guesses per client address
START = SlidingWindow(20, 60 * 60)  # new taxpayer returns per client address
ANSWER = SlidingWindow(30, 60)  # answers and uploads per taxpayer session
STAFF_WRITE = SlidingWindow(120, 60)  # staff actions per staff session


def reset_all() -> None:
    for w in (LOGIN, CODE_REDEEM, START, ANSWER, STAFF_WRITE):
        w.reset()
    _calls.reset()
    _global_calls.reset()


# ---------------------------------------------------------------- model budget
def calls_per_minute() -> int:
    return int(os.environ.get("SILVERFILE_LLM_CALLS_PER_MINUTE", "20"))


def max_tokens_per_return() -> int:
    return int(os.environ.get("SILVERFILE_MAX_TOKENS_PER_RETURN", "250000"))


_calls = SlidingWindow(10**9, 60)  # limit applied per call from the environment, see check()
_global_calls = SlidingWindow(10**9, 60)
_db_lock = threading.Lock()

SCHEMA = "CREATE TABLE IF NOT EXISTS spend (scope TEXT PRIMARY KEY, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0)"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings().security_db), check_same_thread=False, timeout=30)
    conn.execute(SCHEMA)
    return conn


def spent(scope: str) -> int:
    with _db() as conn:
        row = conn.execute("SELECT tokens_in + tokens_out FROM spend WHERE scope = ?", (scope,)).fetchone()
    return int(row[0]) if row else 0


def check(scope: str | None, max_tokens: int) -> None:
    """Before a model call: raise BudgetExceeded if this call would break a limit."""
    scope = scope or "unscoped"
    cap = max_tokens_per_return()
    used = spent(scope)
    if used + max_tokens > cap:
        raise BudgetExceeded(f"return {scope} has used {used} of its {cap} model tokens")
    _limit(_calls, scope, calls_per_minute())
    _limit(_global_calls, "*", calls_per_minute() * 3)


def _limit(window: SlidingWindow, key: str, limit: int) -> None:
    window.limit = limit
    try:
        window.hit(key)
    except RateLimited as e:
        raise BudgetExceeded(f"model call rate limit reached ({limit}/min); retry in {e.retry_after}s") from None


def record(scope: str | None, tokens_in: int, tokens_out: int) -> None:
    scope = scope or "unscoped"
    with _db_lock, _db() as conn:
        conn.execute("INSERT INTO spend (scope, tokens_in, tokens_out, calls) VALUES (?, ?, ?, 1) ON CONFLICT(scope) DO UPDATE SET tokens_in = tokens_in + excluded.tokens_in, tokens_out = tokens_out + excluded.tokens_out, calls = calls + 1",
                     (scope, int(tokens_in), int(tokens_out)))


def forget(scope: str) -> None:
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM spend WHERE scope = ?", (scope,))
