"""MCP server exposing tax_math as tools over stdio.

Run directly: `python -m silverfile.tax_math.server`. The Computation agent
starts it as a subprocess (see client.py), so every number on the return is the
result of a tool call that can be logged with its inputs.
"""
from __future__ import annotations

import functools
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import rules

server = MCPServer("silverfile-tax-math", instructions="Deterministic 2025 Form 1040-SR arithmetic. No estimates.")


def _wrap(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError) as e:  # spec violations travel to the client as tool errors
            raise ToolError(str(e)) from e

    return inner


# The client starts one server per agent and names that agent's tools (security/capabilities.py);
# the server registers only those, so a tool the agent may not use does not exist in its session.
_only = {t for t in os.environ.get("SILVERFILE_TAXMATH_TOOLS", "").split(",") if t}
for _name, _fn in rules.TOOL_FUNCTIONS.items():
    if _only and _name not in _only:
        continue
    server.tool(name=_name, description=(_fn.__doc__ or _name).strip().splitlines()[0])(_wrap(_fn))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
