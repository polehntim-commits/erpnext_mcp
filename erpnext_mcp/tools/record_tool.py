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
#: v0.319.0. Registers that have a create tool but no get / list / update of their own: those actions run through
#: the two generic tools already in the catalogue — get / list through query_doctype (this account's DocPerms; no
#: Password field), update through update_document (draft or non-submittable only, every field on the operator's
#: Updatable Fields list). Kept out of `tables()` so superseded.covering() never calls those two generic tools
#: "covered". Money, HR, share links and security stay off, as on the rest of this tool.
GENERIC: dict = {
	"accident_report": "Accident Report", "accounting_dimension": "Accounting Dimension",
	"activity_cost_pool": "Activity Cost Pool", "advisory_agreement": "Advisory Agreement",
	"ag_uom_context": "Agricultural UOM Context", "asset": "Asset", "asset_category": "Asset Category",
	"backup_record": "Backup Record", "branch": "Branch", "break_policy": "Labor Break Policy",
	"breakeven_analysis": "Breakeven Analysis", "change_management_log": "Change Management Log",
	"company": "Company", "contractor_job": "Contractor Job", "cost_center": "Cost Center",
	"crop_observation": "Crop Observation", "department": "Department", "designation": "Designation",
	"employment_type": "Employment Type", "farm_task": "Farm Task", "fiscal_year": "Fiscal Year",
	"hazard_analysis": "Hazard Analysis", "heat_exposure_event": "Heat Exposure Event",
	"housing_assignment": "Housing Assignment", "iot_reading": "IoT Reading", "ipm_relationship": "IPM Relationship",
	"item_group": "Item Group", "monitoring_record": "Monitoring Record",
	"normalization_adjustment": "Normalization Adjustment", "planting_season": "Planting Season",
	"scale_ticket": "Scale Ticket", "shipment": "Trade Shipment", "soil_compaction_profile": "Soil Compaction Profile",
	"spray_application": "Spray Application", "spray_nozzle_config": "Spray Nozzle Config",
	"spray_tank_mix": "Spray Tank Mix", "todo": "ToDo", "traceability_lot": "Traceability Lot Code",
	"trade_document_template": "Trade Document Template", "training_session": "Training Session",
	"training_type": "Training Type", "warehouse": "Warehouse",
}
GENERIC_TOOL = {"get": "query_doctype", "list": "query_doctype", "update": "update_document"}
_NOT_COLUMNS = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Fold", "Heading", "Table",
                "Table MultiSelect", "Password", "Image"}
_LIST_COLUMNS = ("company", "status", "docstatus", "posting_date", "date", "modified")


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


def _generic(table: dict, type_: str) -> dict:
	"""{action: generic tool} this type gets from GENERIC — only the actions it has no tool of its own for."""
	calls = table.get(type_) or {}
	if type_ not in GENERIC or "create" not in calls:
		return {}
	return {a: t for a, t in GENERIC_TOOL.items() if a not in calls}


def _types_answer(table: dict, kind: str) -> ToolResult:
	rows = []
	for t, actions in sorted(table.items()):
		row = {"type": t, "actions": sorted(set(actions) | set(_generic(table, t)) if kind == "record" else actions)}
		if kind == "record" and _generic(table, t):
			row["generic"] = sorted(_generic(table, t))
		rows.append(row)
	return ToolResult(data={"types": rows, "count": len(rows)}, summary=f"{len(rows)} {kind} types")


def _describe(table: dict, type_: str, kind: str) -> ToolResult:
	if type_ not in table:
		raise ToolError(f"no {kind} type {type_!r} ({kind} action=types lists them).")
	calls = dict(table[type_])
	if kind == "record":
		calls.update(_generic(table, type_))
	data = {"type": type_, "calls": calls, "arguments": {a: _schema(t) for a, t in calls.items()}}
	if kind == "record" and _generic(table, type_):
		data["generic"] = {"doctype": GENERIC[type_], "note": "get / list: name, filters (fieldname → value) and "
		                   "limit; update: name and body (fieldname → value; each field must be on Updatable Fields)."}
	return ToolResult(data=data, summary=f"{type_}: {', '.join(sorted(calls))}")


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
	if not tool and kind == "record" and action in _generic(table, type_):
		return _run_generic(type_, action, args)
	if not tool:
		raise ToolError(f"{type_} has no {action} (it has {', '.join(sorted(table[type_]))}).")
	body = dict(args.get("body") or {})
	if action == "list":
		body = dict(args.get("filters") or body)
	for extra in ("reason", "change_note"):
		if args.get(extra) and extra in _schema(tool)["properties"]:
			body.setdefault(extra, args[extra])
	return call(tool, _with_name(tool, body, as_str(args, "name")))


def _columns(doctype: str) -> list:
	import frappe

	fields = [f for f in (getattr(frappe.get_meta(doctype), "fields", None) or [])]
	names = []
	for field in fields:
		get = field.get if hasattr(field, "get") else (lambda k, f=field: getattr(f, k, None))
		if get("fieldname") and get("fieldtype") not in _NOT_COLUMNS:
			names.append(get("fieldname"))
	return ["name", *[n for n in names if n != "name"]]


def _run_generic(type_: str, action: str, args: dict) -> ToolResult:
	"""get / list / update for a GENERIC type, through query_doctype / update_document (their switches, their rules)."""
	from .. import compat

	doctype = GENERIC[type_]
	if not compat.doctype_exists(doctype):
		raise ToolError(f"{type_}: the {doctype} DocType is not on this site.")
	name = as_str(args, "name")
	if action == "update":
		if not name:
			raise ToolError("update needs name (the record).")
		body = dict(args.get("body") or {})
		if not body:
			raise ToolError("update needs body: fieldname → value.")
		if type_ == "farm_task" and any(key in body for key in PLACE_KEYS):
			return _place_task(name, body)
		result = call("update_document", {"doctype": doctype, "docname": name, "updates": body})
		result.data = {"type": type_, "doctype": doctype, "via": "update_document", **(result.data or {})}
		return result
	if action == "get":
		if not name:
			raise ToolError("get needs name (the record).")
		result = call("query_doctype", {"doctype": doctype, "filters": {"name": name}, "fields": _columns(doctype),
		                                "limit": 1})
		rows = (result.data or {}).get("records") or []
		if not rows:
			raise ToolError(f"no {doctype} {name!r} that this account can read.")
		return ToolResult(data={"type": type_, "doctype": doctype, "via": "query_doctype", "record": rows[0]},
		                  summary=f"{doctype} {name}")
	filters = dict(args.get("filters") or args.get("body") or {})
	limit = filters.pop("limit", None) or 50
	columns = ["name", *[c for c in _LIST_COLUMNS if compat.has_field(doctype, c) or c in ("docstatus", "modified")]]
	query = {"doctype": doctype, "filters": filters, "fields": columns, "limit": limit}
	result = call("query_doctype", query)
	rows = (result.data or {}).get("records") or []
	data = result.data or {}
	return ToolResult(data={"type": type_, "doctype": doctype, "via": "query_doctype", "rows": rows,
	                        "count": len(rows), "truncated": data.get("truncated"), "acting_user": data.get("acting_user"),
	                        "permissions_bounding": data.get("permissions_bounding")},
	                  summary=f"{len(rows)} {doctype}")


#: v0.323.0. A Farm Task's places are set through `task_locations` — validated (each place exists, is a Field, tag or
#: Parcel on the task's company, not retired; a closed task refused) — never through the Updatable Fields whitelist.
PLACE_KEYS = ("locations", "work_spot", "clear_work_spot")


def _place_task(name: str, body: dict) -> ToolResult:
	"""record update farm_task with body.locations / work_spot / clear_work_spot (+ any other fields)."""
	from .. import oauth, settings, task_locations

	if not settings.tool_enabled("create_farm_task"):
		raise ToolError("placing a task needs create_farm_task, which is switched off here — tick "
		                "'allow_create_farm_task' in ERPNext MCP Settings. Nothing was changed.")
	if not oauth.permits("create_farm_task"):
		raise ToolError("placing a task is outside this client's OAuth scopes. Nothing was changed.")
	if not frappe_exists(task_locations.TASK, name):
		raise ToolError(f"no Farm Task called {name!r}. Nothing was changed.")
	places = body.pop("locations", None)
	pin = body.pop("work_spot", None)
	clear = bool(body.pop("clear_work_spot", False))
	try:
		# Checked in full before anything is written, so a bad place changes nothing.
		company = str(frappe_value(task_locations.TASK, name, "company") or "")
		if places is not None:
			task_locations.resolve_all(places, company)
		task_locations.parse_pin(pin)
	except task_locations.LocationError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	other = None
	if body:
		other = call("update_document", {"doctype": task_locations.TASK, "docname": name, "updates": body}).data
	try:
		placed = task_locations.set_locations(name, places, pin=pin, clear_pin=clear)
	except task_locations.LocationError as exc:
		raise ToolError(f"{exc} Nothing more was changed.") from None
	data = {"type": "farm_task", "doctype": task_locations.TASK, "via": "task_locations", **placed}
	if other:
		data["updated"] = other
	where = ", ".join(r["location"] for r in placed["locations"]) or "no place"
	return ToolResult(data=data, summary=f"{name} covers {where}"
	                  + (" (work spot pinned)" if placed.get("work_spot") else ""))


def frappe_exists(doctype: str, name: str) -> bool:
	import frappe

	return bool(frappe.db.exists(doctype, name))


def frappe_value(doctype: str, name: str, field: str):
	import frappe

	return frappe.db.get_value(doctype, name, field)


def record(args: dict) -> ToolResult:
	records, _ = tables()
	return _run(records, "record", RECORD_ACTIONS, args)


def document(args: dict) -> ToolResult:
	_, documents = tables()
	return _run(documents, "document", DOCUMENT_ACTIONS, args)
