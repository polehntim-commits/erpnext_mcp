# SPDX-License-Identifier: MIT
"""Which per-type tools a generic tool now covers, and whether `tools/list` may leave them out. v0.283.0
(consolidation phase 5; docs/design/plan_patterns_consolidation_2026_10.md §2 rules 3–4).

THE MAP IS READ FROM THE GENERIC TOOLS THEMSELVES — the tools `template`, `config`, `record`, `document` and
`share_link` actually run — so it is exact, and a register that joins `record` joins here with no edit. Nothing is
"superseded" that the generic tool cannot do.

HIDING IS OFF BY DEFAULT (Tim, 2026-10-08: keep the old calls around). With `hide_superseded_tools` on, an old tool
is left out of `tools/list` only while its generic tool is itself listed for this client (switched on, in its OAuth
scopes, available here) — so turning the setting on never takes a capability out of a client's view. Hidden tools
stay callable by name, under their own switch, exactly as before; the scheduled runs call by name.

EVERY OLD-NAME CALL IS LOGGED with the generic tool that covers it (MCP Action Log → Superseded By), which is the
evidence the 30-days-of-zero-use rule needs before anything is ever removed. A call made THROUGH a generic tool is
logged once, under the generic tool's name.
"""

from __future__ import annotations

from . import settings

_CACHE: dict = {}


def covering() -> dict:
	"""{old tool: the generic tool that runs it}. Generic tools themselves are never in it.

	Where two generic tools run the same old tool, the more specific one names it: template, config, share_link,
	document, then record (`create_job_link` is a share_link call even though `record` can reach it too)."""
	from . import registry

	key = len(registry.TOOLS)
	if _CACHE.get("key") == key:
		return _CACHE["map"]
	out: dict = {}
	try:
		from .tools import config_tool, record_tool, share_tool, template_tool

		for kind in template_tool.KINDS.values():
			for entry in (kind.get("tools") or {}).values():
				out.setdefault(entry[0], "template")
		for tool in config_tool.TOOL_OF.values():
			out.setdefault(tool, "config")
		for kind in share_tool.KINDS.values():
			for tool, _ in kind["calls"].values():
				out.setdefault(tool, "share_link")
		records, documents = record_tool.tables()
		for calls in documents.values():
			for tool in calls.values():
				out.setdefault(tool, "document")
		for calls in records.values():
			for tool in calls.values():
				out.setdefault(tool, "record")
	except Exception:  # a broken generic table must never break tools/list or the audit row
		out = {}
	generic = set(out.values())
	out = {old: new for old, new in out.items() if old in registry.TOOLS and old not in generic}
	_CACHE.update(key=key, map=out)
	return out


def superseded_by(tool_name: str) -> str:
	return covering().get(tool_name, "")


def hide_enabled() -> bool:
	return settings.as_bool(settings._value("hide_superseded_tools"))


def hidden(tool_name: str) -> bool:
	"""Leave this tool out of `tools/list`? Only with the setting on AND its generic tool listed for this client."""
	if not hide_enabled():
		return False
	generic = superseded_by(tool_name)
	if not generic:
		return False
	from . import oauth, registry

	return settings.tool_enabled(generic) and oauth.permits(generic) and registry.is_available(generic)
