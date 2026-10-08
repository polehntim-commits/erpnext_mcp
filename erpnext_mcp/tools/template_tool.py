# SPDX-License-Identifier: MIT
"""One tool for every kind of template. v0.279.0 (consolidation phase 1; docs/design/plan_patterns_consolidation_2026_10.md).

`template(action, kind, …)` builds, edits, previews, forks, versions and publishes any template — farm task (and
with it shop items, daily checks, scouting), inspection, training type, trade document, reporting, asset type, and
the config-backed kinds (wizard, job template, seasonal program, market commodity, supplier connector, input plan,
pollination plan).

IT ADDS NOTHING NEW UNDER THE HOOD. Every action CALLS THE EXISTING TOOL for that kind — through that tool's own
switch, site check and OAuth scope — so each kind keeps its own validation, permissions and answer. A write through
`template` needs BOTH `allow_template` and the kind's own switch (e.g. `allow_create_inspection_template`), so
switching this tool on widens nothing. The old tools stay exactly as they are (Tim, 2026-10-08: keep them).

`kinds` lists the kinds; `describe` gives one kind's body (generated from the existing tools' own schemas), so a
client learns any kind's fields without a code change.
"""

from __future__ import annotations

import frappe

from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult

ACTIONS = ("kinds", "describe", "list", "get", "create", "update", "fork", "preview", "enable", "disable", "history",
           "diff", "stage", "publish", "rollback")
CONFIG_ACTIONS = ("list", "get", "create", "update", "fork", "preview", "history", "diff", "stage", "publish", "rollback")


def _doc_kind(label, doctype, name_field, tools, note=""):
	return {"label": label, "store": doctype, "doctype": doctype, "name_field": name_field, "tools": tools, "note": note}


def _config_kind(label, config_kind, note=""):
	return {"label": label, "store": f"Farm Config Version ({config_kind})", "config_kind": config_kind, "note": note}


#: kind → how each action is done. A doctype kind's tools: action → (tool, the argument that names the template,
#: extra arguments). A config kind goes through the config lifecycle tools with its config kind.
KINDS = {
	"farm_task": _doc_kind("Farm task template (also shop work, daily checks, scouting)", "Farm Task Template",
	                       "template_name", {
		                       "list": ("list_farm_task_templates", None, {}),
		                       "get": ("get_farm_task_template", "template", {}),
		                       "create": ("create_farm_task_template", None, {}),
		                       "update": ("update_farm_task_template", "template", {}),
		                       "preview": ("preview_farm_task_template", "template", {}),
		                       "enable": ("update_farm_task_template", "template", {"enabled": True}),
		                       "disable": ("update_farm_task_template", "template", {"enabled": False}),
	                       }),
	"inspection": _doc_kind("Inspection template", "Inspection Template", "template_name", {
		"list": ("list_inspection_templates", None, {}),
		"get": ("get_inspection_template", "name", {}),
		"create": ("create_inspection_template", None, {}),
		"update": ("update_inspection_template", "name", {}),
		"preview": ("preview_inspection_template", "template", {}),
		"enable": ("approve_inspection_template", "name", {}),
		"disable": ("deactivate_inspection_template", "name", {}),
	}, "enable = approve (a person's sign-off); disable needs a reason"),
	"training_type": _doc_kind("Training type", "Training Type", "name", {
		"create": ("create_training_type", None, {}),
		"update": ("update_training_type", "training_type", {}),
		"enable": ("update_training_type", "training_type", {"active": True}),
		"disable": ("deactivate_training_type", "training_type", {}),
	}, "disable needs a reason"),
	"trade_document": _doc_kind("Trade document template", "Trade Document Template", "template_name", {
		"list": ("list_trade_document_templates", None, {}),
		"create": ("create_trade_document_template", None, {}),
	}, "update goes through update_document's whitelist"),
	"reporting": _doc_kind("Reporting template", "Reporting Template", "template_name", {
		"list": ("list_reporting_templates", None, {}),
		"get": ("get_reporting_template", "reporting_template", {}),
		"create": ("create_reporting_template", None, {}),
		"update": ("update_reporting_template", "reporting_template", {}),
		"enable": ("update_reporting_template", "reporting_template", {"enabled": True}),
		"disable": ("update_reporting_template", "reporting_template", {"enabled": False}),
	}),
	"asset_type": _doc_kind("Asset type (the template an asset is registered from)", "Asset Type", "type_name", {
		"list": ("list_asset_types", None, {"include_disabled": True}),
		"get": ("get_asset_type", "name", {}),
		"create": ("create_asset_type", None, {}),
		"update": ("update_asset_type", "name", {}),
		"enable": ("update_asset_type", "name", {"enabled": True}),
		"disable": ("update_asset_type", "name", {"enabled": False}),
	}),
	"wizard": _config_kind("Phone wizard", "wizard"),
	"job_template": _config_kind("Contractor / supplier / pollination job template", "job_template"),
	"seasonal_program": _config_kind("Seasonal checklist program (winterize / start-up)", "seasonal_program",
	                                 "its checklists are farm_task templates"),
	"market_commodity": _config_kind("Market commodity", "market_commodity"),
	"supplier_connector": _config_kind("Supplier connector", "supplier_connector"),
	"input_plan": _config_kind("Input plan (live budget)", "input_plan"),
	"pollination_plan": _config_kind("Pollination plan", "pollination_plan"),
}


def call(tool: str, args: dict) -> ToolResult:
	"""Run an existing tool exactly as a client calling it would: its site check, switch and scope first."""
	from .. import oauth, registry, settings

	spec = registry.TOOLS.get(tool)
	if spec is None:
		raise ToolError(f"internal: no tool {tool!r}.")
	if not registry.is_available(tool):
		raise ToolError(f"{tool} is not available on this site: it requires {spec['requires'] or 'a missing component'}.")
	if not settings.tool_enabled(tool):
		raise ToolError(f"this needs {tool}, which is switched off here — tick 'allow_{tool}' in ERPNext MCP Settings. "
		                "Nothing was changed.")
	if not oauth.permits(tool):
		raise ToolError(f"this needs {tool}, which is outside this client's OAuth scopes. Nothing was changed.")
	result = spec["handler"](dict(args))
	return result if isinstance(result, ToolResult) else ToolResult(data=result or {}, summary="")


def _kind(args: dict) -> dict:
	kind = as_str(args, "kind", required=True).strip().lower()
	if kind not in KINDS:
		raise ToolError(f"kind is one of: {', '.join(KINDS)}.")
	return {"key": kind, **KINDS[kind]}


def _actions(kind: dict) -> list:
	if kind.get("config_kind"):
		return list(CONFIG_ACTIONS)
	have = set(kind["tools"]) | {"history", "fork"}
	if kind["key"] == "trade_document":
		have |= {"update", "get"}
	if "list" not in have or "get" not in have:
		have |= {"list", "get"}  # read straight from the register
	return [a for a in ACTIONS if a in have]


def _schema_of(tool: str) -> dict:
	from .. import registry

	spec = registry.TOOLS.get(tool) or {}
	schema = spec.get("inputSchema") or {}
	return {"properties": schema.get("properties") or {}, "required": schema.get("required") or []}


def _kinds(_args: dict) -> ToolResult:
	rows = [{"kind": k, "label": v["label"], "store": v["store"], "actions": _actions({"key": k, **v}),
	         **({"note": v["note"]} if v.get("note") else {})} for k, v in KINDS.items()]
	return ToolResult(data={"kinds": rows, "count": len(rows)}, summary=f"{len(rows)} template kinds")


def _describe(args: dict) -> ToolResult:
	kind = _kind(args)
	out = {"kind": kind["key"], "label": kind["label"], "store": kind["store"], "actions": _actions(kind)}
	if kind.get("config_kind"):
		from .. import phone_config

		slug = kind["config_kind"]
		example = None
		for row in phone_config.rows(phone_config.SLUG_KINDS.get(slug, slug)):
			doc = phone_config.doc_of(row["config_kind"], row["config_key"], row["version"])
			example = {"key": row["config_key"], "version": row["version"], "status": row["status"],
			           "body": phone_config.body_of(doc)}
			break
		out["body"] = {"shape": "a JSON object, the whole config", "example": example,
		               "validated_by": f"the {kind['label']} validator (phone_config.validate), at draft and publish",
		               "fields": {k: type(v).__name__ for k, v in ((example or {}).get("body") or {}).items()}}
		out["calls"] = {"create / update": "draft_config", "list": "list_configs", "get": "get_config",
		                "preview": "preview_config", "diff": "diff_config", "stage": "stage_config",
		                "publish": "publish_config", "rollback": "rollback_config"}
	else:
		create = kind["tools"].get("create")
		out["body"] = _schema_of(create[0]) if create else {}
		out["calls"] = {action: entry[0] for action, entry in kind["tools"].items()}
		if kind["key"] == "trade_document":
			out["calls"]["update"] = "update_document"
	if kind.get("note"):
		out["note"] = kind["note"]
	return ToolResult(data=out, summary=f"{kind['key']}: {len(out['actions'])} actions")


def _register_read(kind: dict, name: str = "", limit: int = 200):
	doctype = kind["doctype"]
	if name:
		if not frappe.db.exists(doctype, name):
			raise ToolError(f"no {kind['label']} {name!r}.")
		return frappe.get_doc(doctype, name).as_dict()
	return frappe.db.get_all(doctype, fields=["name"], order_by="modified desc", limit=limit) or []


def _doc_action(kind: dict, action: str, args: dict) -> ToolResult:
	name = as_str(args, "name")
	body = dict(args.get("body") or {})
	if action in ("list", "get") and action not in kind["tools"]:
		data = _register_read(kind, name if action == "get" else "")
		return ToolResult(data={"kind": kind["key"], "template" if action == "get" else "templates": data},
		                  summary=f"{kind['key']} {action}")
	if action == "update" and kind["key"] == "trade_document":
		return call("update_document", {"doctype": kind["doctype"], "docname": name, "updates": body})
	if action == "fork":
		return _fork_doc(kind, args)
	if action == "history":
		return _history_doc(kind, name)
	if action not in kind["tools"]:
		raise ToolError(f"{kind['key']} does not support {action} (it supports {', '.join(_actions(kind))}).")
	tool, id_arg, extra = kind["tools"][action]
	if action == "preview" and body and not name and kind["key"] == "farm_task":
		return call(tool, {"template_body": body, **({"company": args["company"]} if args.get("company") else {})})
	inner = {**body, **extra}
	if action == "list":
		inner = {**dict(args.get("filters") or {}), **extra}
	if id_arg:
		if not name:
			raise ToolError(f"name: which {kind['label']}.")
		inner[id_arg] = name
	if action == "disable" and as_str(args, "reason"):
		inner["reason"] = as_str(args, "reason")
	return call(tool, inner)


def _fork_doc(kind: dict, args: dict) -> ToolResult:
	source = as_str(args, "name", required=True)
	new_name = as_str(args, "new_name", required=True)
	if "create" not in kind["tools"]:
		raise ToolError(f"{kind['key']} has no create path to fork into.")
	allowed = set(_schema_of(kind["tools"]["create"][0])["properties"])
	if "get" in kind["tools"]:
		tool, id_arg, _extra = kind["tools"]["get"]
		got = call(tool, {id_arg: source}).data or {}
		got = got.get("template") if isinstance(got.get("template"), dict) else got
	else:
		got = _register_read(kind, source)
	body = {k: v for k, v in dict(got).items() if k in allowed and v not in (None, "", [], {})}
	body.update(dict(args.get("overrides") or {}))
	body[kind["name_field"] if kind["name_field"] in allowed else next(iter(sorted(allowed)))] = new_name
	result = call(kind["tools"]["create"][0], body)
	data = dict(result.data or {})
	data["forked_from"] = source
	return ToolResult(data=data, summary=f"forked {source} → {new_name}", docstatus_delta=result.docstatus_delta)


def _history_doc(kind: dict, name: str) -> ToolResult:
	if not name:
		raise ToolError(f"name: which {kind['label']}.")
	if not frappe.db.exists(kind["doctype"], name):
		raise ToolError(f"no {kind['label']} {name!r}.")
	rows = []
	from .. import compat

	if compat.doctype_exists("Version"):
		rows = frappe.db.get_all("Version", filters={"ref_doctype": kind["doctype"], "docname": name},
		                         fields=["name", "owner", "creation", "data"], order_by="creation desc", limit=100) or []
	current = frappe.db.get_value(kind["doctype"], name, ["modified", "modified_by"], as_dict=True) or {}
	version = frappe.db.get_value(kind["doctype"], name, "version") if compat.has_field(kind["doctype"], "version") else None
	return ToolResult(data={"kind": kind["key"], "name": name, "version": version,
	                        "modified": str(current.get("modified") or ""), "modified_by": current.get("modified_by"),
	                        "changes": [{"at": str(r.get("creation")), "by": r.get("owner"), "change": r.get("data")}
	                                    for r in rows]},
	                  summary=f"{name}: {len(rows)} recorded change(s)")


def _config_action(kind: dict, action: str, args: dict) -> ToolResult:
	ck = kind["config_kind"]
	name = as_str(args, "name")
	base = {"kind": ck, **({"key": name} if name else {})}
	if action == "list":
		return call("list_configs", {**base, **({"status": args["status"]} if args.get("status") else {})})
	if action == "history":
		if not name:
			raise ToolError("name: which config key.")
		return call("list_configs", base)
	if action == "get":
		return call("get_config", {**base, **({"version": args["version"]} if args.get("version") else {})})
	if action in ("create", "update"):
		if not name:
			raise ToolError("name: the config key.")
		return call("draft_config", {**base, "body": dict(args.get("body") or {}),
		                             **({"notes": args["change_note"]} if args.get("change_note") else {})})
	if action == "fork":
		new_name = as_str(args, "new_name", required=True)
		got = call("get_config", {**base, **({"version": args["version"]} if args.get("version") else {})}).data or {}
		got = got.get("config") if isinstance(got.get("config"), dict) else got
		body = dict(got.get("body") or got.get("in_force") or {})
		body.update(dict(args.get("overrides") or {}))
		body.pop("key", None)
		result = call("draft_config", {"kind": ck, "key": new_name, "body": body,
		                               "notes": f"Forked from {name} v{got.get('version') or ''}."})
		return ToolResult(data={**dict(result.data or {}), "forked_from": name}, summary=f"forked {name} → {new_name}",
		                  docstatus_delta=result.docstatus_delta)
	if action == "preview":
		return call("preview_config", {**base, **{k: args[k] for k in ("body", "version") if k in args}})
	if action == "diff":
		return call("diff_config", {**base, **{k: args[k] for k in ("from_version", "to_version") if k in args}})
	if action in ("stage", "publish"):
		return call(f"{action}_config", {**base, **{k: args[k] for k in ("version", "change_note", "users", "roles",
		                                                                  "companies") if k in args}})
	if action == "rollback":
		return call("rollback_config", {**base, **{k: args[k] for k in ("to", "change_note") if k in args}})
	raise ToolError(f"{kind['key']} does not support {action} (it supports {', '.join(CONFIG_ACTIONS)}).")


def template(args: dict) -> ToolResult:
	action = as_str(args, "action", required=True).strip().lower()
	if action not in ACTIONS:
		raise ToolError(f"action is one of: {', '.join(ACTIONS)}.")
	if action == "kinds":
		return _kinds(args)
	if action == "describe":
		return _describe(args)
	kind = _kind(args)
	if kind.get("config_kind"):
		if action in ("enable", "disable"):
			raise ToolError(f"{kind['key']} is versioned config: publish a version to put it in service, roll back or "
			                "retire to take it out.")
		return _config_action(kind, action, args)
	if action in ("stage", "publish", "rollback", "diff"):
		raise ToolError(f"{kind['key']} is a register, not versioned config: use enable / disable, and history.")
	return _doc_action(kind, action, args)
