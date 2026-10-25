"""MCP server exposing tax_math as tools over stdio.

Run directly: `python -m silverfile.tax_math.server`. The Computation agent
starts it as a subprocess (see client.py), so every number on the return is the
result of a tool call that can be logged with its inputs.
"""
from __future__ import annotations

import functools

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


for _name, _fn in rules.TOOL_FUNCTIONS.items():
    server.tool(name=_name, description=(_fn.__doc__ or _name).strip().splitlines()[0])(_wrap(_fn))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
