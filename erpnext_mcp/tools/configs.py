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
	# v0.235.0. Payroll settings (decision 9): stricter — publish, stage and roll back are a
	# person's, in the Desk; a preview reruns a real pay period live and staged.
	"overtime_rule": {"label": "Overtime rule", "store": "Farm Config Version (Payroll Setting)", "key": "overtime_rule"},
}
PAYROLL_KINDS = ("overtime_rule",)
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
	elif kind in PAYROLL_KINDS:
		data = _data(phone_configs.list_phone_configs({"kind": "payroll_setting", "key": kind, "status": as_str(args, "status")}))
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
	if kind in PAYROLL_KINDS:
		from .. import payroll_settings

		inner = {"kind": "payroll_setting", "key": kind}
		if version not in (None, ""):
			inner["version"] = version
		try:
			return _data(phone_configs.get_phone_config(inner))
		except ToolError:
			return {"built_in": True, "in_force": payroll_settings.overtime()}
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
	days = max(1, min(as_int(args, "days", 1), MAX_PREVIEW_DAYS))
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
	if kind in PAYROLL_KINDS:
		from .. import payroll_settings

		body = args.get("body")
		if not isinstance(body, dict):
			raise ToolError("body is the overtime rule to try: {weekly_threshold_hours, multiplier, effective_from}.")
		try:
			data = payroll_settings.preview(kind, {"schema_version": 1, "key": kind, **body}, args)
		except ValueError as exc:
			raise ToolError(f"{exc}. Nothing was calculated.") from None
		return ToolResult(
			data={"kind": kind, "key": kind, "preview": data, "written": False},
			summary=("identical pay" if data["identical"] else f"{data['changed']} employee(s) paid differently, "
			         f"{len(data['flagged'])} over {data['flag_threshold_pct']:g}%") + " (nothing written)",
		)
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


# ── the write side (v0.234.1) ───────────────────────────────────────────────
#
# Each action routes to the specific tool for its kind, so every check that tool makes
# — validation, roles, Spanish completeness, the sandbox — still runs. Over MCP a draft
# is always AI-proposed, and an AI-proposed version is published only in the Desk or on
# the phone (decision 5): `publish_config` refuses it and says where to go.
ACTIONS = ("draft", "stage", "publish", "rollback")


def _kinds_allowed(action: str) -> list:
	"""The per-kind allow list for an action (ERPNext MCP Settings). Blank = every kind."""
	from .. import settings

	raw = str(settings.get_settings().get(f"config_{action}_kinds") or "")
	return [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]


def _require_kind_allowed(action: str, kind: str) -> None:
	allowed = _kinds_allowed(action)
	if allowed and kind not in allowed:
		raise ToolError(
			f"{action}_config is not allowed for {kind} on this site (ERPNext MCP Settings → "
			f"config_{action}_kinds lists {', '.join(allowed)}). Nothing was changed."
		)


_PHONE_DRAFT = {
	"wizard": ("create_wizard_definition", "update_wizard_definition"),
	"tile": ("create_tile", "update_tile"),
	"label_profile": (None, "update_label_profile"),
}


def draft_config(args: dict) -> ToolResult:
	"""Create or change a Draft of any kind. Over MCP it is always AI-proposed."""
	kind = kind_of(args)
	_require_kind_allowed("draft", kind)
	key = as_str(args, "key", required=True)
	fields = dict(args.get("fields") or {})
	notes = as_str(args, "notes")
	if kind in PAYROLL_KINDS:
		from .. import payroll_settings, phone_config

		body = args.get("body")
		if not isinstance(body, dict):
			raise ToolError("body is required: {weekly_threshold_hours, multiplier, effective_from}.")
		try:
			doc, _report = phone_config.save_draft(payroll_settings.KIND, kind, {"schema_version": 1, "key": kind, **body},
			                                       notes or "Drafted through draft_config.", "AI-proposed")
		except phone_config.ConfigError as exc:
			raise ToolError(str(exc)) from None
		return ToolResult(
			data={"kind": kind, "key": kind, "draft": phone_config.describe(doc), "authored_by": "AI-proposed",
			      "next": "preview_config (it reruns a pay period live and staged); a person publishes it in the Desk."},
			summary=f"drafted {doc.name}",
			docstatus_delta="0 → 0 (draft)",
		)
	if kind in PHONE_KINDS:
		from .. import phone_config

		body = args.get("body")
		if body is None:
			raise ToolError("body is required for a phone config draft.")
		create, update = _PHONE_DRAFT[kind]
		exists = bool(phone_config.rows(phone_config.SLUG_KINDS[kind], key))
		handler = getattr(phone_configs, update if exists or not create else create)
		data = _data(handler({"key": key, "body": body, "notes": notes or "Drafted through draft_config.",
		                      "authored_by": "AI-proposed"}))
	elif kind == "extraction_config":
		data = _data(moments.update_extraction_config(
			{"document_type": key, "config": args.get("body") or fields.get("config"), "notes": notes or "Drafted through draft_config.",
			 "authored_by": "AI-proposed"}))
	elif kind in RULE_KINDS:
		inner = {**fields, "authored_by": "AI-proposed"}
		inner.setdefault("rule_id", key)
		if kind == "trigger_rule":
			inner.setdefault("category", "Work Timing")
		data = _data(rules.propose_compliance_rule(inner))
	elif kind == "inspection_template":
		data = _data(sessions.update_inspection_template({**fields, "name": key}))
	else:
		exists = bool(frappe.db.exists("Farm Task Template", key))
		if exists:
			data = _data(tasktemplates.update_farm_task_template({**fields, "template": key}))
		else:
			data = _data(tasktemplates.create_farm_task_template({**fields, "template_name": key}))
	return ToolResult(
		data={"kind": kind, "key": key, "draft": data, "authored_by": "AI-proposed",
		      "next": "preview_config, then stage_config; publishing happens in the Desk or on the phone."},
		summary=f"drafted {kind} {key}",
		docstatus_delta="0 → 0 (draft)",
	)


def stage_config(args: dict) -> ToolResult:
	"""Put a Draft in front of a chosen audience first (it takes real effect for them — decision 4)."""
	kind = kind_of(args)
	if kind in PAYROLL_KINDS:
		from .. import config_lifecycle

		config_lifecycle.refuse_payroll_publish(f"the {kind}")
	_require_kind_allowed("stage", kind)
	key = as_str(args, "key", required=True)
	version = args.get("version")
	if kind in PHONE_KINDS:
		data = _data(phone_configs.stage_phone_config(
			{"kind": kind, "key": key, "version": version, "change_note": as_str(args, "change_note", required=True),
			 "users": args.get("users") or [], "roles": args.get("roles") or [], "companies": args.get("companies") or []}))
	elif kind == "extraction_config":
		data = _data(moments.stage_extraction_config({"document_type": key, "version": version, "users": args.get("users") or []}))
	else:
		raise ToolError(
			f"{kind} has no staged audience yet: it goes live when approved in the Desk. Staging rules to "
			"chosen blocks and crews arrives with the Work Timing rules (queue item 5). Nothing was changed."
		)
	return ToolResult(data={"kind": kind, "key": key, "staged": data}, summary=f"staged {kind} {key}",
	                  docstatus_delta="0 → 0 (staged)")


def publish_config(args: dict) -> ToolResult:
	"""Publish a version — only one a person wrote; an AI-proposed one is published in the Desk."""
	kind = kind_of(args)
	if kind in PAYROLL_KINDS:
		from .. import config_lifecycle

		config_lifecycle.refuse_payroll_publish(f"the {kind}")
	_require_kind_allowed("publish", kind)
	key = as_str(args, "key", required=True)
	version = args.get("version")
	if kind in PHONE_KINDS:
		data = _data(phone_configs.publish_phone_config(
			{"kind": kind, "key": key, "version": version, "change_note": as_str(args, "change_note", required=True)}))
	elif kind == "extraction_config":
		data = _data(moments.publish_extraction_config({"document_type": key, "version": version}))
	elif kind in RULE_KINDS:
		data = _data(rules.approve_compliance_rule({"name": key}))
	elif kind == "inspection_template":
		data = _data(sessions.approve_inspection_template({"name": key}))
	else:
		raise ToolError("a task template goes live when it is enabled in the Desk. Nothing was changed.")
	return ToolResult(data={"kind": kind, "key": key, "published": data}, summary=f"published {kind} {key}",
	                  docstatus_delta="0 → 0 (published)")


def rollback_config(args: dict) -> ToolResult:
	"""Back to the previous published version, or `to: none` to retire / deactivate."""
	kind = kind_of(args)
	if kind in PAYROLL_KINDS:
		from .. import config_lifecycle

		config_lifecycle.refuse_payroll_publish(f"the {kind}")
	_require_kind_allowed("rollback", kind)
	key = as_str(args, "key", required=True)
	note = as_str(args, "change_note", required=True)
	retire = as_str(args, "to") == "none"
	if kind in PHONE_KINDS:
		handler = phone_configs.retire_phone_config if retire else phone_configs.rollback_phone_config
		data = _data(handler({"kind": kind, "key": key, "change_note": note}))
	elif kind in RULE_KINDS and retire:
		data = _data(rules.deactivate_compliance_rule({"name": key, "reason": note}))
	elif kind == "inspection_template" and retire:
		data = _data(sessions.deactivate_inspection_template({"name": key, "reason": note}))
	else:
		raise ToolError(
			f"{kind} rolls back by approving its earlier version in the Desk (or to: none to switch it off). "
			"Nothing was changed."
		)
	return ToolResult(data={"kind": kind, "key": key, "rolled_back": data, "retired": retire},
	                  summary=f"{'retired' if retire else 'rolled back'} {kind} {key}", docstatus_delta="0 → 0")
