"""One thin door to the model. Every call returns text plus token counts so the
agents can log usage (docs/metrics.md) and the context-window strategy can be
measured. With no API key the agents use their offline fallbacks instead.

The key comes from one of two places: the environment (ANTHROPIC_API_KEY, the
local setup) or `request_key`, a context variable the dashboard's key gate sets
for the duration of a request so that a visitor's own key is used for their
runs and nothing else. The context variable wins. Context is copied into the
threads the graph runs on (starlette's run_in_threadpool and LangGraph's
executor both do that), so the agents need no plumbing.
"""
from __future__ import annotations

import base64
import contextvars
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str

    def json(self) -> Any:
        """Parse the first JSON object or array in the text."""
        t = self.text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
        if m:
            t = m.group(1).strip()
        start = min([i for i in (t.find("{"), t.find("[")) if i >= 0] or [0])
        try:
            return json.loads(t[start:])
        except json.JSONDecodeError:
            depth, end = 0, None
            for i, ch in enumerate(t[start:], start):
                if ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            return json.loads(t[start:end]) if end else None


request_key: contextvars.ContextVar[str | None] = contextvars.ContextVar("silverfile_request_key", default=None)

_clients: dict[str, Any] = {}


def current_key() -> str | None:
    """The key this call should use: the request's own if the gate set one, else the environment's."""
    return request_key.get() or settings().api_key


def client():
    key = current_key()
    if not key:
        raise RuntimeError("no API key: SilverFile is in offline mode")
    if key not in _clients:
        import anthropic

        import os

        # The workspace header belongs to the local key; a visitor's key is billed to their own account.
        headers = {"anthropic-workspace-id": os.environ["ANTHROPIC_WORKSPACE_ID"]} if os.environ.get("ANTHROPIC_WORKSPACE_ID") and key == settings().api_key else {}
        _clients[key] = anthropic.Anthropic(api_key=key, default_headers=headers)
    return _clients[key]


def forget_client(key: str) -> None:
    _clients.pop(key, None)


def available() -> bool:
    return bool(current_key())


def estimate_tokens(text: str) -> int:
    """Rough offline estimate (about 4 characters per token) for the offline mode."""
    return max(1, len(text) // 4)


def complete(system: str, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1200) -> LLMResult:
    """Call Claude with a system prompt and a list of {role, content} messages."""
    resp = client().messages.create(model=model, system=system, messages=messages, max_tokens=max_tokens)
    text = "".join(getattr(b, "text", "") for b in resp.content)
    u = resp.usage
    tokens_in = int(getattr(u, "input_tokens", 0) or 0) + int(getattr(u, "cache_read_input_tokens", 0) or 0) + int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    return LLMResult(text=text, tokens_in=tokens_in, tokens_out=int(getattr(u, "output_tokens", 0) or 0), model=model)


def image_block(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
