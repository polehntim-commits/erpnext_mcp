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
#: v0.313.0. Tools renamed outright — the old name stays callable and is logged as superseded by the new one.
RENAMED = {"create_packer_share": "create_customer_portal", "issue_packer_credential": "issue_portal_credential",
           "revoke_packer_access": "revoke_portal_access", "get_packer_share": "get_customer_portal",
           "get_packer_pack": "get_customer_portal_pack", "list_packer_access_log": "list_portal_access_log"}


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
	for old, new in RENAMED.items():
		if old in registry.TOOLS and new in registry.TOOLS:
			out[old] = new
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


#: Rows read for one usage report. A month of OML's log is ~10,000 rows; past this the answer says it stopped.
USAGE_ROW_CAP = 200000


def usage(days: int = 30) -> dict:
	"""Old-name calls in the last `days`, per tool, and the covered tools with none — the 30-day evidence.

	Read from MCP Action Log's Superseded By column, so it counts calls made by an old name only (a call through a
	generic tool is logged under the generic name). `log_starts` is the oldest row on the site: when it is newer
	than the window's start, the window is not yet fully covered and `window_covered` is false — zero calls then
	means "none since the log began", not 30 days of none. Removing a name stays Tim's call.
	"""
	import frappe

	from . import audit

	days = max(1, min(int(30 if days is None else days), 365))
	since = frappe.utils.add_days(frappe.utils.now(), -days)
	rows = frappe.db.get_all(
		audit.LOG_DOCTYPE,
		filters={"timestamp": (">=", since), "superseded_by": ("is", "set")},
		fields=["tool_name", "superseded_by", "timestamp", "agent_client", "agent_model", "caller_ip", "result_status"],
		order_by="timestamp desc",
		limit=USAGE_ROW_CAP + 1,
	) or []
	capped = len(rows) > USAGE_ROW_CAP
	rows = rows[:USAGE_ROW_CAP]
	used: dict = {}
	for row in rows:
		entry = used.setdefault(row.get("tool_name"), {
			"tool": row.get("tool_name"), "covered_by": row.get("superseded_by"), "calls": 0, "last_call": None,
			"clients": set(), "addresses": set(), "refused": 0})
		entry["calls"] += 1
		stamp = str(row.get("timestamp") or "")
		if stamp and (entry["last_call"] is None or stamp > entry["last_call"]):
			entry["last_call"] = stamp
		for key, column in (("clients", "agent_client"), ("clients", "agent_model"), ("addresses", "caller_ip")):
			if row.get(column):
				entry[key].add(str(row[column]))
		if row.get("result_status") in ("Blocked", "Error"):
			entry["refused"] += 1
	used_rows = sorted(({**e, "clients": sorted(e["clients"]), "addresses": sorted(e["addresses"])}
	                    for e in used.values()), key=lambda e: (-e["calls"], e["tool"]))
	unused: dict = {}
	for old, generic in sorted(covering().items()):
		if old not in used:
			unused.setdefault(generic, []).append(old)
	oldest = frappe.db.get_all(audit.LOG_DOCTYPE, fields=["timestamp"], order_by="timestamp asc", limit=1) or []
	log_starts = str(oldest[0].get("timestamp")) if oldest else None
	covered = bool(log_starts) and log_starts <= str(since)
	return {
		"days": days,
		"since": str(since),
		"log_starts": log_starts,
		"window_covered": covered,
		"old_name_calls": sum(e["calls"] for e in used_rows),
		"used": used_rows,
		"unused_count": sum(len(v) for v in unused.values()),
		"unused": unused,
		"rows_capped": capped,
		"hide_superseded_tools": hide_enabled(),
		"note": ("Old-name calls only; a call through a generic tool is logged under the generic name. A tool in "
		         "`unused` had no old-name call in the window" + ("" if covered else
		         " — but the log does not reach back the whole window yet, so this is not yet 30 days of evidence")
		         + ". Removing any name needs Tim's OK; nothing here removes or hides anything."),
	}
