"""One thin door to the model. Every call returns text plus token counts so the
agents can log usage (docs/metrics.md) and the context-window strategy can be
measured. With no API key the agents use their offline fallbacks instead.
"""
from __future__ import annotations

import base64
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


_client = None


def client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=settings().api_key)
    return _client


def available() -> bool:
    return not settings().offline


def estimate_tokens(text: str) -> int:
    """Rough offline estimate (about 4 characters per token) for the offline mode."""
    return max(1, len(text) // 4)


def complete(system: str, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1200, temperature: float = 0.0) -> LLMResult:
    """Call Claude with a system prompt and a list of {role, content} messages."""
    resp = client().messages.create(model=model, system=system, messages=messages, max_tokens=max_tokens, temperature=temperature)
    text = "".join(getattr(b, "text", "") for b in resp.content)
    u = resp.usage
    tokens_in = int(getattr(u, "input_tokens", 0) or 0) + int(getattr(u, "cache_read_input_tokens", 0) or 0) + int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    return LLMResult(text=text, tokens_in=tokens_in, tokens_out=int(getattr(u, "output_tokens", 0) or 0), model=model)


def image_block(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
