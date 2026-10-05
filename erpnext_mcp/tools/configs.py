# SPDX-License-Identifier: MIT
"""One config lifecycle, read side. v0.234.0. docs/design/config_lifecycle_and_tool_consolidation.md §3.

FOUR GENERIC READS, KEYED BY `kind`, OVER EVERY "FLOW DATA, NOT CODE" FEATURE:
`list_configs`, `get_config`, `diff_config`, `preview_config`. Each kind is routed to
the tool that already answers that question for it, so the generic answer and the
specific one cannot disagree — the specific tools stay (decision 7: deprecated names
are kept until Tim approves their removal) and the write side follows in v0.234.1.

A compliance rule previews three ways the old tool could not: an UNSAVED `patch`
merged over the live row, a `days` window (would last Tuesday have raised?), and a
`compare_to: live` side by side. Nothing here writes.
"""

from __future__ import annotations

import datetime
import json

import frappe

from .. import compliance_rules
from ..alerts import engine
from ..args import as_date, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import calendar, moments, phone_configs, rules, sessions, tasktemplates, wizards

#: kind → what it is and where it lives.
KINDS = {
	"wizard": {"label": "Wizard", "store": "Farm Config Version"},
	"tile": {"label": "Tile", "store": "Farm Config Version"},
	"label_profile": {"label": "Label Profile", "store": "Farm Config Version"},
	"extraction_config": {"label": "Extraction Config", "store": "Document Extraction Config", "key": "document_type"},
	"inspection_template": {"label": "Inspection Template", "store": "Inspection Template", "key": "template"},
	"task_template": {"label": "Farm Task Template", "store": "Farm Task Template", "key": "template"},
	"compliance_rule": {"label": "Compliance Rule", "store": "Compliance Rule", "key": "rule_id or docname"},
	"trigger_rule": {
		"label": "Work Timing rule",
		"store": "Compliance Rule (category Work Timing)",
		"key": "rule_id or docname",
	},
}
PHONE_KINDS = ("wizard", "tile", "label_profile")
RULE_KINDS = ("compliance_rule", "trigger_rule")
MAX_PREVIEW_DAYS = 31


def kind_of(args: dict) -> str:
	kind = as_str(args, "kind", required=True).strip().lower()
	if kind not in KINDS:
		raise ToolError(f"kind is one of: {', '.join(KINDS)}.")
	return kind


def _data(result) -> dict:
	return result.data if isinstance(result, ToolResult) else dict(result or {})


# ── list ────────────────────────────────────────────────────────────────────
def list_configs(args: dict) -> ToolResult:
	kind = kind_of(args)
	key = as_str(args, "key")
	if kind in PHONE_KINDS:
		data = _data(phone_configs.list_phone_configs({"kind": kind, "key": key, "status": as_str(args, "status")}))
	elif kind == "extraction_config":
		data = _data(moments.list_extraction_configs({"document_type": key} if key else {}))
	elif kind == "inspection_template":
		data = _data(sessions.list_inspection_templates({"limit": as_int(args, "limit", 100)}))
	elif kind == "task_template":
		data = _data(tasktemplates.list_farm_task_templates({"limit": as_int(args, "limit", 100)}))
	else:
		inner = {"limit": as_int(args, "limit", 100)}
		if kind == "trigger_rule":
			inner["category"] = "Work Timing"
		data = _data(calendar.list_compliance_rules(inner))
	return ToolResult(data={"kind": kind, "store": KINDS[kind]["store"], **data}, summary=f"{kind} configs")


# ── get ─────────────────────────────────────────────────────────────────────
def _get(kind: str, key: str, version) -> dict:
	if kind in PHONE_KINDS:
		inner = {"kind": kind, "key": key}
		if version not in (None, ""):
			inner["version"] = version
		return _data(phone_configs.get_phone_config(inner))
	if kind == "extraction_config":
		inner = {"document_type": key}
		if version not in (None, ""):
			inner["version"] = version
		return _data(moments.get_extraction_config(inner))
	if kind == "inspection_template":
		return _data(sessions.get_inspection_template({"template": key}))
	if kind == "task_template":
		return _data(tasktemplates.get_farm_task_template({"template": key}))
	if version not in (None, ""):
		# A rule's versions are rows sharing a rule_id; `superseded_by` links them.
		live = compliance_rules.resolve(key)
		rule_id = (compliance_rules.rule_row(live).get("rule_id") if live else "") or key
		for row in compliance_rules.rule_rows(include_inactive=True):
			if row.get("rule_id") == rule_id and int(row.get("version") or 0) == int(version):
				return _data(rules.get_compliance_rule({"name": row["name"]}))
		raise ToolError(f"rule {rule_id!r} has no version {version}.")
	return _data(rules.get_compliance_rule({"name": key}))


def get_config(args: dict) -> ToolResult:
	kind = kind_of(args)
	key = as_str(args, "key", required=True)
	data = _get(kind, key, args.get("version"))
	return ToolResult(data={"kind": kind, "key": key, "config": data}, summary=f"{kind} {key}")


# ── diff ────────────────────────────────────────────────────────────────────
def _flatten(value, prefix: str = "", out: dict | None = None) -> dict:
	out = {} if out is None else out
	if isinstance(value, dict):
		for key in sorted(value):
			_flatten(value[key], f"{prefix}.{key}" if prefix else str(key), out)
	else:
		out[prefix] = value
	return out


#: Bookkeeping that differs between any two versions and says nothing about them.
_NOISE = {"modified", "creation", "name", "version", "status", "published_on", "published_by", "staged_on",
          "staged_by", "body_hash", "validation", "checked_at"}


def diff_config(args: dict) -> ToolResult:
	kind = kind_of(args)
	key = as_str(args, "key", required=True)
	left_version = args.get("from_version")
	right_version = args.get("to_version")
	left = _flatten(_get(kind, key, left_version))
	right = _flatten(_get(kind, key, right_version))
	changes = {}
	for path in sorted(set(left) | set(right)):
		if path.split(".")[-1] in _NOISE:
			continue
		a, b = left.get(path), right.get(path)
		if json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str):
			changes[path] = {"from": a, "to": b}
	return ToolResult(
		data={
			"kind": kind,
			"key": key,
			"from_version": left_version or "live",
			"to_version": right_version or "live",
			"changes": changes,
			"changed": len(changes),
		},
		summary=f"{kind} {key}: {len(changes)} field(s) differ",
	)


# ── preview ─────────────────────────────────────────────────────────────────
def _patched_rule(key: str, patch: dict) -> dict:
	"""The live rule with an UNSAVED patch over it, validated like a write would be."""
	name = compliance_rules.resolve(key)
	if not name:
		raise ToolError(f"no Compliance Rule {key!r}.")
	row = dict(compliance_rules.rule_row(name))
	blobs = {argument: (column, parser) for argument, column, parser in compliance_rules._PRIMITIVE_BLOBS}
	for field, value in (patch or {}).items():
		if field in blobs:
			column, parser = blobs[field]
			try:
				parsed = parser(value, field)
			except ValueError as exc:
				raise ToolError(f"patch.{field}: {exc}") from None
			row[column] = json.dumps(parsed)
		elif field in ("scope_filters",):
			row["scope_filters_json"] = json.dumps(compliance_rules.parse_filters(value))
		else:
			row[field] = value
	return row


def _rule_preview(kind: str, key: str, args: dict) -> dict:
	patch = args.get("patch") or {}
	if not isinstance(patch, dict):
		raise ToolError("patch is an object of rule fields, e.g. {\"condition_tree\": {...}}.")
	company = as_str(args, "company")
	start = as_date(args, "as_of") or frappe.utils.today()
	days = max(1, min(as_int(args, "days", 1) or 1, MAX_PREVIEW_DAYS))
	row = _patched_rule(key, patch)
	live = compliance_rules.rule_row(compliance_rules.resolve(key)) if args.get("compare_to") == "live" else None
	out = []
	first = datetime.date.fromisoformat(str(start)[:10])
	for offset in range(days):
		day = (first - datetime.timedelta(days=offset)).isoformat()
		result = engine.preview(row, {"today": day, "company": company or ""})
		entry = {"day": day, "observed": result["observed"], "observations": result["observations"]}
		if live is not None:
			entry["live_observed"] = engine.preview(live, {"today": day, "company": company or ""})["observed"]
		out.append(entry)
	return {
		"rule": row.get("rule_id"),
		"patched": sorted(patch),
		"days": out,
		"note": (
			"NOTHING WAS WRITTEN. Each day is judged as of that date on today's records; weather replays "
			"from saved snapshots once the weather provider lands (decision 3)."
		),
	}


def preview_config(args: dict) -> ToolResult:
	kind = kind_of(args)
	key = as_str(args, "key")
	if kind in RULE_KINDS:
		if not key:
			raise ToolError("key (the rule) is required.")
		data = _rule_preview(kind, key, args)
	elif kind == "wizard":
		data = _data(phone_configs.preview_wizard({k: args[k] for k in ("key", "version", "body", "answers", "language") if k in args}))
	elif kind == "tile":
		inner = {k: args[k] for k in ("surface", "as_user", "asset", "app_version", "key", "version", "body") if k in args}
		inner.setdefault("surface", "today")
		data = _data(phone_configs.preview_tiles(inner))
	elif kind == "label_profile":
		data = _data(phone_configs.preview_label_profile({k: args[k] for k in ("key", "version", "body", "item") if k in args}))
	elif kind == "extraction_config":
		inner = {k: args[k] for k in ("validation", "version", "config") if k in args}
		if key:
			inner["document_type"] = key
		data = _data(moments.preview_extraction_config(inner))
	elif kind == "inspection_template":
		data = _data(sessions.preview_inspection_template({"template": key, **({"language": args["language"]} if "language" in args else {})}))
	else:
		inner = {k: args[k] for k in ("template_body", "language", "context", "answers") if k in args}
		if key:
			inner["template"] = key
		data = _data(tasktemplates.preview_farm_task_template(inner))
	return ToolResult(data={"kind": kind, "key": key or None, "preview": data, "written": False},
	                  summary=f"preview of {kind} {key or ''} (nothing written)".strip())


def describe_kinds() -> list:
	return [{"kind": kind, **spec} for kind, spec in KINDS.items()]
