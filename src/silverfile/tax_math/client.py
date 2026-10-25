"""Synchronous client the Computation agent uses to call tax_math tools.

Default: a real MCP stdio round trip to `silverfile.tax_math.server`, one
subprocess per computation pass. With SILVERFILE_TAXMATH_DIRECT=1 (tests, or
environments without subprocess access) the same functions are called
in-process; the call log is identical either way, so provenance is unchanged.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

from . import rules


@dataclass
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    result: Any
    transport: str  # "mcp-stdio" | "direct"


@dataclass
class TaxMathClient:
    """Call tools and keep a log of every call, for provenance."""

    direct: bool | None = None
    calls: list[ToolCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.direct is None:
            self.direct = os.environ.get("SILVERFILE_TAXMATH_DIRECT", "0") == "1"
        self._session = None

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "TaxMathClient":
        if not self.direct:
            self._start_mcp()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "_loop", None) is not None:
            fut = asyncio.run_coroutine_threadsafe(self._aclose(), self._loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop = None

    # -- calls -----------------------------------------------------------
    def call(self, tool: str, **arguments: Any) -> Any:
        if tool not in rules.TOOL_FUNCTIONS:
            raise KeyError(f"unknown tax_math tool {tool}")
        if self.direct or self._session is None:
            result = rules.TOOL_FUNCTIONS[tool](**arguments)
            transport = "direct"
        else:
            fut = asyncio.run_coroutine_threadsafe(self._acall(tool, arguments), self._loop)
            result = fut.result(timeout=60)
            transport = "mcp-stdio"
        self.calls.append(ToolCall(tool, arguments, result, transport))
        return result

    def list_tools(self) -> list[str]:
        if self.direct or self._session is None:
            return sorted(rules.TOOL_FUNCTIONS)
        fut = asyncio.run_coroutine_threadsafe(self._alist(), self._loop)
        return fut.result(timeout=30)

    # -- MCP plumbing ----------------------------------------------------
    def _start_mcp(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._aopen(), self._loop)
        try:
            fut.result(timeout=60)
        except Exception as e:  # fall back rather than block a return on plumbing
            sys.stderr.write(f"[tax_math] MCP stdio unavailable ({e}); using direct calls\n")
            self._session = None
            self.direct = True

    async def _aopen(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=sys.executable, args=["-m", "silverfile.tax_math.server"], env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        self._stdio_cm = stdio_client(params)
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

    async def _aclose(self) -> None:
        try:
            if getattr(self, "_session_cm", None):
                await self._session_cm.__aexit__(None, None, None)
            if getattr(self, "_stdio_cm", None):
                await self._stdio_cm.__aexit__(None, None, None)
        finally:
            self._session = None

    async def _alist(self) -> list[str]:
        res = await self._session.list_tools()
        return sorted(t.name for t in res.tools)

    async def _acall(self, tool: str, arguments: dict[str, Any]) -> Any:
        res = await self._session.call_tool(tool, arguments)
        if getattr(res, "is_error", None) or getattr(res, "isError", None):
            text = " ".join(getattr(c, "text", "") for c in res.content)
            raise ValueError(text or f"{tool} failed")
        sc = getattr(res, "structured_content", None)
        if sc is None:
            sc = getattr(res, "structuredContent", None)
        if sc is not None:
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        text = " ".join(getattr(c, "text", "") for c in res.content)
        try:
            return json.loads(text)
        except Exception:
            return text
