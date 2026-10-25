"""Deterministic tax arithmetic. No model ever does arithmetic in SilverFile.

`rules.py` holds the pure functions. `server.py` exposes them as MCP tools.
`client.py` is what the Computation agent uses to call them over MCP stdio.
"""
