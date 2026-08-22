# SPDX-License-Identifier: MIT
"""erpnext_mcp — a Model Context Protocol server that ships as a Frappe app.

Install it into any Frappe/ERPNext bench and an MCP client (Claude Desktop,
Claude Code, or anything else that speaks the protocol) can read your ledger
and — only behind per-tool switches an operator turns on by hand — post to it.

Everything the server can do lives in `erpnext_mcp.registry`. The transport is
`erpnext_mcp.mcp`. Nothing in this app is specific to any one ERPNext install:
company, account and fiscal-year names are all discovered at call time.
"""

__version__ = "0.118.0"
