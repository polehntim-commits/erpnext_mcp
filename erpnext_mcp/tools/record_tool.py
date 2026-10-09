# SPDX-License-Identifier: MIT
"""One tool for the registers, one for documents' state changes. v0.281.0 (consolidation phase 3;
docs/design/plan_patterns_consolidation_2026_10.md).

`record(action, type, …)` — create, update, deactivate, delete, get, list — for every register that has its own
tools today (supplier, field, crop, asset, warehouse, …). `document(action, type, name)` — submit, cancel, approve,
reject, close, reopen — for every document that has its own state-change tools (purchase order / receipt / invoice,
stock entry, scale ticket, farm task, leave request, …).

THE TABLES ARE READ FROM THE REGISTRY, by the tools' own names (`create_supplier` → type `supplier`), so a register
that gains tools is on these two with no code change. NOTHING NEW UNDERNEATH: every action runs that type's
existing tool under its own switch, so each register keeps its validation, company scoping, permissions and answer;
a write needs `allow_record` / `allow_document` AND the type's own switch. Money, HR and security tools (I-9,
payroll, NACHA, bank, tax, journals, signing, tokens, roles, employees) are not on these — they keep their own
tools. The per-type tools all remain.
"""

from __future__ import annotations

import re

from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult
from .template_tool import call

RECORD_ACTIONS = ("create", "update", "deactivate", "delete", "get", "list")
DOCUMENT_ACTIONS = ("submit", "cancel", "approve", "reject", "close", "reopen")
#: Not on the generic tools: money, HR, signing and security keep their own (the plan's special-purpose family).
SPECIAL = re.compile(r"i9|w4|payroll|nacha|prenote|garnish|tax|941|futa|bank|payment|signature|sign_|seal|signing|"
                     r"journal|opening_balance|ledger|gl_|_gl|reconcil|statement|anchor|1099|fica|pay_stub|"
                     r"direct_deposit|owner_draw|loan|note_payable|cap_table|token|mcp_client|oauth|feature_flag|role|"
                     r"updatable_fields|security|access|employee|family_member|accounting_period")
#: Generic tools that are not a register of their own.
NOT_TYPES = {"document", "record", "template", "config"}
_CACHE: dict = {}


def _singulars(noun: str):
	for suffix, replacement in (("ies", "y"), ("es", ""), ("s", "")):
		if noun.endswith(suffix):
			yield noun[: -len(suffix)] + replacement


def tables() -> tuple:
	"""({type: {action: tool}} for records, {type: {action: tool}} for documents) — from the registry's own names."""
	from .. import registry

	key = len(registry.TOOLS)
	if _CACHE.get("key") == key:
		return _CACHE["records"], _CACHE["documents"]
	records: dict = {}
	documents: dict = {}
	for name in registry.TOOLS:
		if SPECIAL.search(name):
			continue
		verb, _, noun = name.partition("_")
		if not noun or noun in NOT_TYPES:
			continue
		if verb in DOCUMENT_ACTIONS:
			documents.setdefault(noun, {})[verb] = name
		if verb in ("create", "update", "delete", "deactivate", "get"):
			records.setdefault(noun, {})[verb] = name
	for name in registry.TOOLS:
		if name.startswith("list_") and not SPECIAL.search(name):
			for noun in _singulars(name[5:]):
				if noun in records:
					records[noun].setdefault("list", name)
					break
	records = {k: v for k, v in records.items() if set(v) & {"create", "update", "delete", "deactivate"}}
	_CACHE.update(key=key, records=records, documents=documents)
	return records, documents


def _schema(tool: str) -> dict:
	from .. import registry

	schema = (registry.TOOLS.get(tool) or {}).get("inputSchema") or {}
	return {"properties": schema.get("properties") or {}, "required": list(schema.get("required") or [])}


def _with_name(tool: str, body: dict, name: str) -> dict:
	"""Put `name` under the target tool's own identifying argument (its first required one the body lacks)."""
	inner = dict(body)
	if name:
		schema = _schema(tool)
		missing = [arg for arg in schema["required"] if arg not in inner]
		target = missing[0] if missing else next((a for a in ("name", "docname") if a in schema["properties"]), None)
		if target:
			inner[target] = name
	return inner


def _types_answer(table: dict, kind: str) -> ToolResult:
	rows = [{"type": t, "actions": sorted(actions)} for t, actions in sorted(table.items())]
	return ToolResult(data={"types": rows, "count": len(rows)}, summary=f"{len(rows)} {kind} types")


def _describe(table: dict, type_: str, kind: str) -> ToolResult:
	if type_ not in table:
		raise ToolError(f"no {kind} type {type_!r} ({kind} action=types lists them).")
	calls = table[type_]
	return ToolResult(data={"type": type_, "calls": calls, "arguments": {a: _schema(t) for a, t in calls.items()}},
	                  summary=f"{type_}: {', '.join(sorted(calls))}")


def _run(table: dict, kind: str, actions: tuple, args: dict) -> ToolResult:
	action = as_str(args, "action", required=True).strip().lower()
	if action == "types":
		return _types_answer(table, kind)
	type_ = as_str(args, "type", required=True).strip().lower().replace(" ", "_")
	if action == "describe":
		return _describe(table, type_, kind)
	if action not in actions:
		raise ToolError(f"action is one of: types, describe, {', '.join(actions)}.")
	if type_ not in table:
		raise ToolError(f"no {kind} type {type_!r} ({kind} action=types lists them).")
	tool = table[type_].get(action)
	if not tool:
		raise ToolError(f"{type_} has no {action} (it has {', '.join(sorted(table[type_]))}).")
	body = dict(args.get("body") or {})
	if action == "list":
		body = dict(args.get("filters") or body)
	for extra in ("reason", "change_note"):
		if args.get(extra) and extra in _schema(tool)["properties"]:
			body.setdefault(extra, args[extra])
	return call(tool, _with_name(tool, body, as_str(args, "name")))


def record(args: dict) -> ToolResult:
	records, _ = tables()
	return _run(records, "record", RECORD_ACTIONS, args)


def document(args: dict) -> ToolResult:
	_, documents = tables()
	return _run(documents, "document", DOCUMENT_ACTIONS, args)
