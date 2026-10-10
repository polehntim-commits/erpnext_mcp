# SPDX-License-Identifier: MIT
"""One tool for every kind of configuration. v0.280.0 (consolidation phase 2; docs/design/plan_patterns_consolidation_2026_10.md).

`config(action, kind, …)` reads, drafts, previews, stages, publishes and rolls back any config kind the config
lifecycle knows: phone wizards, tiles, label profiles, extraction configs, inspection and task templates as config,
compliance and work-timing rules, the overtime rule, SOPs, quizzes, the pest degree-day models and offsets, and the
newer kinds (market commodity, seasonal program, job template, supplier connector, input plan, pollination plan).

NOTHING NEW UNDERNEATH. Each action runs the existing lifecycle tool (`list_configs`, `get_config`, `diff_config`,
`preview_config`, `draft_config`, `stage_config`, `publish_config`, `rollback_config`) under its own switch, so every
kind keeps its rules: an AI draft is published only by a person, payroll settings only in the Desk, the per-kind
allow lists (`config_<action>_kinds`) still apply. A write needs `allow_config` AND that action's own switch. The
old tools stay.
"""

from __future__ import annotations

from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult
from .template_tool import call

ACTIONS = ("kinds", "describe", "list", "get", "diff", "preview", "draft", "stage", "publish", "rollback")
TOOL_OF = {"list": "list_configs", "get": "get_config", "diff": "diff_config", "preview": "preview_config",
           "draft": "draft_config", "stage": "stage_config", "publish": "publish_config", "rollback": "rollback_config"}
#: Arguments each lifecycle tool takes besides kind and key (read from the registry, so new ones flow through).
PASSED = ("version", "status", "limit", "from_version", "to_version", "body", "fields", "notes", "patch", "as_of",
          "days", "compare_to", "company", "answers", "language", "surface", "as_user", "item", "validation",
          "template_body", "context", "change_note", "users", "roles", "companies", "to")


def _kinds_table() -> dict:
	from . import configs

	return configs.KINDS


def _rules(kind: str) -> list:
	from . import configs

	out = []
	if kind in configs.PAYROLL_KINDS:
		out.append("publish, stage and rollback are a person's, in the Desk (decision 9); preview reruns a real pay period")
	if kind in configs.IPM_KINDS:
		out.append("published and rolled back only by a person in the Desk; calibration and AI drafts suggest")
	if kind == "sop":
		out.append("stage = submit for review; approval (publish) is a person's, in the Desk or on the phone")
	out.append("an AI-drafted version goes live only when a person publishes it (decision 5)")
	return out


def _example(kind: str):
	"""A saved body of this kind to read the shape from, where one exists."""
	from .. import phone_config

	slug = {"pest_dd_models": "ipm_setting", "pest_dd_offsets": "ipm_setting", "overtime_rule": "payroll_setting",
	        "pay_policy": "payroll_setting", "wage_floor": "payroll_setting",
	        "oregon_withholding": "payroll_setting", "garnishment_policy": "payroll_setting",
	        "garnishment_limits": "payroll_setting", "pay_item_policy": "payroll_setting"}.get(kind, kind)
	try:
		kind_label = phone_config.SLUG_KINDS.get(slug)
	except Exception:
		kind_label = None
	if not kind_label:
		return None
	for row in phone_config.rows(kind_label):
		if slug != kind and row["config_key"] != kind:
			continue
		doc = phone_config.doc_of(row["config_kind"], row["config_key"], row["version"])
		return {"key": row["config_key"], "version": row["version"], "status": row["status"],
		        "body": phone_config.body_of(doc)}
	return None


def config(args: dict) -> ToolResult:
	from .. import registry

	action = as_str(args, "action", required=True).strip().lower()
	if action not in ACTIONS:
		raise ToolError(f"action is one of: {', '.join(ACTIONS)}.")
	table = _kinds_table()
	if action == "kinds":
		rows = [{"kind": k, "label": v["label"], "store": v["store"], **({"key": v["key"]} if v.get("key") else {}),
		         "rules": _rules(k)} for k, v in table.items()]
		return ToolResult(data={"kinds": rows, "count": len(rows), "actions": list(ACTIONS[2:])},
		                  summary=f"{len(rows)} config kinds")
	kind = as_str(args, "kind", required=True).strip().lower()
	if kind not in table:
		raise ToolError(f"kind is one of: {', '.join(table)} (config action=kinds).")
	if action == "describe":
		example = _example(kind)
		data = {"kind": kind, **table[kind], "rules": _rules(kind), "example": example,
		        "fields": {k: type(v).__name__ for k, v in ((example or {}).get("body") or {}).items()} if example else None,
		        "arguments": {a: sorted((registry.TOOLS[t]["inputSchema"].get("properties") or {}).keys())
		                      for a, t in TOOL_OF.items()}}
		return ToolResult(data=data, summary=f"{kind}: {table[kind]['label']}")
	inner = {"kind": kind, **({"key": args["name"]} if args.get("name") else {}),
	         **({"key": args["key"]} if args.get("key") else {})}
	inner.update({k: args[k] for k in PASSED if k in args})
	return call(TOOL_OF[action], inner)
