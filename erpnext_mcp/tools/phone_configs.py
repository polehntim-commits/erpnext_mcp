# SPDX-License-Identifier: MIT
"""Phone configuration and the compliance loop: the MCP half. v0.207.0.

docs/design/phone_config_and_compliance_loop.md. Nineteen tools:

  lifecycle   list_phone_configs / get_phone_config (read) · stage / publish /
              rollback / retire_phone_config (write)
  wizards     create / update_wizard_definition (write) · preview_wizard (read)
  tiles       create / update_tile (write) · preview_tiles (read)
  the loop    audit_compliance_loop / preview_compliance_loop (read)
  labels      update_label_profile / approve / reject_label_compliance (write) ·
              preview_label_profile / list_label_compliance (read)

Every write is off by default, needs System Manager or Farm Manager, and is
limited to 30 a minute per caller. Nothing here publishes itself: a Draft is
written, previewed, staged and published by separate calls.
"""

from __future__ import annotations

import json
import time

import frappe

from .. import (
	compat,
	compliance_loop,
	form_schema,
	label_compliance,
	phone_config,
	security,
	tiles,
	triage,
	wizard_config,
)
from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult

WRITE_RATE = 30
_recent: dict = {}


# ── helpers ─────────────────────────────────────────────────────────────────
def _gate(action: str) -> str:
	actor = triage.require_manager(action)
	window = [t for t in _recent.get(actor, []) if t > time.time() - 60]
	if len(window) >= WRITE_RATE:
		raise ToolError(
			f"{actor} has made {WRITE_RATE} configuration changes in the last minute; wait and retry."
		)
	window.append(time.time())
	_recent[actor] = window
	return actor


def _ready() -> None:
	if not phone_config.ready():
		raise ToolError(
			"this site has no Farm Config Version doctype yet — run `bench --site <site> migrate`."
		)


def _json(args: dict, key: str, required: bool = False):
	value = args.get(key)
	if isinstance(value, str) and value.strip():
		try:
			value = json.loads(value)
		except ValueError as exc:
			raise ToolError(f"{key} is not valid JSON: {exc}") from exc
	if value in (None, "") and required:
		raise ToolError(f"{key} is required.")
	return value


def _kind(args: dict) -> str:
	try:
		return phone_config.kind_of(as_str(args, "kind", required=True))
	except phone_config.ConfigError as exc:
		raise ToolError(str(exc)) from exc


def _note(args: dict) -> str:
	note = as_str(args, "change_note")
	if not note:
		raise ToolError("change_note is required — say why, for the audit.")
	return note


def _run(fn, *a, **k):
	try:
		return fn(*a, **k)
	except (phone_config.ConfigError, ValueError) as exc:
		raise ToolError(f"{exc} Nothing was changed.") from exc


def _draft(kind: str, args: dict, create_only: bool) -> ToolResult:
	_ready()
	_gate(f"write a {kind}")
	key = as_str(args, "key", required=True)
	body = _json(args, "body", required=True)
	notes = as_str(args, "notes")
	if not notes:
		raise ToolError("notes is required — why this version exists.")
	doc, report = _run(
		phone_config.save_draft,
		kind,
		key,
		body,
		notes,
		as_str(args, "authored_by") or "AI-proposed",
		create_only=create_only,
	)
	return ToolResult(
		data={
			**phone_config.describe(doc),
			"warnings": report["warnings"],
			"next": "preview it, then stage_phone_config (users first) or publish_phone_config.",
		},
		summary=f"drafted {doc.name}"
		+ (f" with {len(report['warnings'])} warning(s)" if report["warnings"] else ""),
		docstatus_delta="none → 0 (draft)",
	)


# ── lifecycle ───────────────────────────────────────────────────────────────
def list_phone_configs(args: dict) -> ToolResult:
	kind = _kind(args) if as_str(args, "kind") else ""
	rows = phone_config.rows(kind, as_str(args, "key"), as_str(args, "status") or None)
	listed = [phone_config.describe(frappe.get_doc(phone_config.DOCTYPE, r["name"])) for r in rows[:500]]
	return ToolResult(
		data={"configs": listed, "count": len(listed)}, summary=f"{len(listed)} config version(s)"
	)


def get_phone_config(args: dict) -> ToolResult:
	kind = _kind(args)
	key = as_str(args, "key", required=True)
	version = as_str(args, "version")
	if version:
		doc = phone_config.doc_of(kind, key, version)
	else:
		doc = phone_config.doc_of(kind, key, status=phone_config.PUBLISHED) or phone_config.doc_of(kind, key)
	if doc is None:
		raise ToolError(f"no {phone_config.KINDS[kind]} {key!r}{' version ' + version if version else ''}.")
	return ToolResult(
		data=phone_config.describe(doc, include_body=True), summary=f"{doc.name} ({doc.status})"
	)


def stage_phone_config(args: dict) -> ToolResult:
	_ready()
	actor = _gate("stage a phone config")
	kind = _kind(args)
	doc = _run(
		phone_config.stage,
		kind,
		as_str(args, "key", required=True),
		as_str(args, "version", required=True),
		_note(args),
		actor,
		users=args.get("users") or (),
		roles=args.get("roles") or (),
		companies=args.get("companies") or (),
	)
	return ToolResult(
		data={
			**phone_config.describe(doc),
			"next": "widen with set_feature_flag, then publish_phone_config.",
		},
		summary=f"staged {doc.name} behind {doc.rollout_flag}",
		docstatus_delta="0 → 0 (updated)",
	)


def publish_phone_config(args: dict) -> ToolResult:
	_ready()
	actor = _gate("publish a phone config")
	kind = _kind(args)
	doc, previous, already = _run(
		phone_config.publish,
		kind,
		as_str(args, "key", required=True),
		as_str(args, "version", required=True),
		_note(args),
		actor,
	)
	rematched = []
	if kind == "Label Profile" and not already:
		rematched = label_compliance.rematch_all()
	return ToolResult(
		data={
			**phone_config.describe(doc),
			"superseded": previous or None,
			"already_published": already,
			"rematched_items": rematched,
		},
		summary=f"{doc.name} is Published"
		+ (f", superseding {previous}" if previous else "")
		+ (" (it already was)" if already else ""),
		docstatus_delta="0 → 0 (updated)",
	)


def rollback_phone_config(args: dict) -> ToolResult:
	_ready()
	actor = _gate("roll back a phone config")
	target, was = _run(
		phone_config.rollback, _kind(args), as_str(args, "key", required=True), _note(args), actor
	)
	return ToolResult(
		data={**phone_config.describe(target), "rolled_back_from": was},
		summary=f"rolled back {was} → {target.name}",
		docstatus_delta="0 → 0 (updated)",
	)


def retire_phone_config(args: dict) -> ToolResult:
	_ready()
	actor = _gate("retire a phone config")
	moved = _run(phone_config.retire, _kind(args), as_str(args, "key", required=True), _note(args), actor)
	return ToolResult(
		data={
			"retired": moved,
			"note": "No phone is served this key now. publish_phone_config on any version enables it again.",
		},
		summary=f"retired {len(moved)} version(s)",
		docstatus_delta="0 → 0 (updated)",
	)


# ── wizards ─────────────────────────────────────────────────────────────────
def create_wizard_definition(args: dict) -> ToolResult:
	return _draft("Wizard", args, create_only=True)


def update_wizard_definition(args: dict) -> ToolResult:
	return _draft("Wizard", args, create_only=False)


def _body_for_preview(kind: str, args: dict):
	body = _json(args, "body")
	if body is not None:
		try:
			body = phone_config.parse_body(body)
		except phone_config.ConfigError as exc:
			raise ToolError(str(exc)) from exc
		key = as_str(args, "key") or str(body.get("key") or "preview")
		body.setdefault("key", key)
		body.setdefault("schema_version", phone_config.SCHEMA_VERSION)
		return key, body, "(unsaved)"
	key = as_str(args, "key", required=True)
	version = as_str(args, "version")
	doc = (
		phone_config.doc_of(kind, key, version)
		if version
		else (
			phone_config.doc_of(kind, key, status=phone_config.DRAFT)
			or phone_config.doc_of(kind, key, status=phone_config.PUBLISHED)
		)
	)
	if doc is None:
		raise ToolError(f"no {phone_config.KINDS[kind]} {key!r} to preview; pass body for an unsaved one.")
	return key, phone_config.body_of(doc), phone_config.version_string(doc)


def preview_wizard(args: dict) -> ToolResult:
	key, body, version = _body_for_preview("Wizard", args)
	report = phone_config.validate("Wizard", key, body, for_publish=True)
	answers = _json(args, "answers") or {}
	language = as_str(args, "language") or "en"
	steps = [
		{
			"key": step.get("key"),
			"title": form_schema.text_of(step.get("title"), language),
			"show_if": step.get("show_if"),
			"next": step.get("next") or [],
			"form": {
				lang: form_schema.resolve_language(form_schema.for_phone(step.get("form") or []), lang)
				for lang in ("en", "es")
			},
		}
		for step in body.get("steps") or []
	]
	visited = wizard_config.path(body, answers) if body.get("steps") else []
	arguments, ignored = wizard_config.handler_arguments(body, answers, {}) if answers else ({}, [])
	from .. import device_capabilities

	return ToolResult(
		data={
			"key": key,
			"config_version": version,
			"validation": report,
			"publishable": not report["errors"],
			"steps": steps,
			"path": visited,
			"handler": (body.get("submit") or {}).get("handler"),
			"handler_would_receive": arguments,
			"ignored_answers": ignored,
			"unmapped_fields": wizard_config.unmapped(body) if body.get("submit") else [],
			"device_problems": device_capabilities.problems(wizard_config.all_fields(body)),
		},
		summary=f"{key} ({version}): {len(report['errors'])} error(s), {len(report['warnings'])} warning(s)",
	)


# ── tiles ───────────────────────────────────────────────────────────────────
def create_tile(args: dict) -> ToolResult:
	return _draft("Tile", args, create_only=True)


def update_tile(args: dict) -> ToolResult:
	return _draft("Tile", args, create_only=False)


def preview_tiles(args: dict) -> ToolResult:
	surface = as_str(args, "surface", required=True)
	if surface not in tiles.SURFACES:
		raise ToolError(f"surface is one of {', '.join(tiles.SURFACES)}.")
	user = as_str(args, "as_user") or security.caller_identity() or str(frappe.session.user)
	shown, hidden = tiles.for_user(
		user, surface, as_str(args, "app_version") or "99.0", as_str(args, "asset") or None, explain=True
	)
	draft = None
	if args.get("body") is not None or as_str(args, "key"):
		key, body, version = _body_for_preview("Tile", args)
		report = phone_config.validate("Tile", key, body, for_publish=True)
		person = phone_config.person_of(user)
		reason = tiles.hidden_reason(body, person, (person.get("companies") or [""])[0], "99.0", {})
		draft = {
			"key": key,
			"config_version": version,
			"validation": report,
			"would_show": not reason,
			"hidden": reason or None,
		}
	return ToolResult(
		data={"surface": surface, "as_user": user, "tiles": shown, "hidden": hidden, "draft": draft},
		summary=f"{len(shown)} tile(s) on {surface} for {user}",
	)


# ── the loop ────────────────────────────────────────────────────────────────
def audit_compliance_loop(args: dict) -> ToolResult:
	data = _run(compliance_loop.audit, as_str(args, "company"), as_str(args, "rule") or None)
	return ToolResult(
		data=data, summary=f"{data['count']} rule(s): {data['ok']} closed, {data['with_gaps']} with gaps"
	)


def preview_compliance_loop(args: dict) -> ToolResult:
	data = _run(
		compliance_loop.preview,
		as_str(args, "rule", required=True),
		as_str(args, "alert") or None,
		as_str(args, "as_user"),
		as_str(args, "language") or "en",
	)
	return ToolResult(
		data=compliance_loop.json_safe(data),
		summary=f"{data['rule']}: " + ("loop closed" if data["ok"] else f"{len(data['gaps'])} gap(s)"),
	)


# ── labels ──────────────────────────────────────────────────────────────────
def update_label_profile(args: dict) -> ToolResult:
	return _draft("Label Profile", args, create_only=False)


def preview_label_profile(args: dict) -> ToolResult:
	key, body, version = _body_for_preview("Label Profile", args)
	report = phone_config.validate("Label Profile", key, body, for_publish=True)
	fake = type("Preview", (), {"config_key": key, "config_kind": "Label Profile", "version": 0})()

	def evaluate(code):
		return label_compliance.evaluate(code, only=[(fake, body)]) if not report["errors"] else {}

	item = as_str(args, "item")
	matches = []
	if not report["errors"]:
		for row in (
			frappe.db.get_all(
				"Item", filters={"label_scan_validation": ("not in", ("", None))}, fields=["name"], limit=2000
			)
			if compat.has_field("Item", "label_scan_validation")
			else []
		):
			result = evaluate(row["name"])
			if result.get("matched"):
				matches.append(
					{
						"item": row["name"],
						"state": result["state"],
						"facts": result["matched"][0]["matched_facts"],
					}
				)
	one = evaluate(item) if item else None
	return ToolResult(
		data={
			"key": key,
			"config_version": version,
			"validation": report,
			"matching_items": matches,
			"item": one,
		},
		summary=f"{key}: {len(matches)} labelled product(s) match",
	)


def list_label_compliance(args: dict) -> ToolResult:
	filters: dict = {"compliance_state": ("in", list(label_compliance.STATES))}
	if as_str(args, "state"):
		filters["compliance_state"] = as_str(args, "state")
	if as_str(args, "item"):
		filters = {"name": as_str(args, "item")}
	rows = frappe.db.get_all(
		"Item",
		filters=filters,
		fields=[
			"name",
			"item_name",
			"compliance_state",
			"compliance_profiles_json",
			"compliance_proposal_json",
		],
		limit=500,
	)
	live = {phone_config.version_string(doc) for doc, _b in label_compliance.profiles()}
	out = []
	for row in rows:
		profiles = json.loads(row.get("compliance_profiles_json") or "[]")
		out.append(
			{
				"item": row["name"],
				"item_name": row.get("item_name"),
				"state": row.get("compliance_state") or None,
				"profiles": profiles,
				"proposal": json.loads(row.get("compliance_proposal_json") or "null"),
				"stale": [p["config_version"] for p in profiles if p.get("config_version") not in live],
			}
		)
	return ToolResult(
		data={"items": out, "count": len(out)}, summary=f"{len(out)} product(s) with label compliance"
	)


def approve_label_compliance(args: dict) -> ToolResult:
	from .. import registry

	actor = _gate("approve label compliance")
	item = as_str(args, "item", required=True)
	row = frappe.db.get_value("Item", item, ["compliance_state", "compliance_proposal_json"], as_dict=True)
	if not row:
		raise ToolError(f"no Item {item!r}.")
	if row.get("compliance_state") != "Proposed":
		raise ToolError(
			f"{item} has no pending proposal (it is {row.get('compliance_state') or 'unmatched'})."
		)
	proposal = json.loads(row.get("compliance_proposal_json") or "{}")
	results = []
	for call in proposal.get("calls") or []:
		if call.get("tool") not in registry.TOOLS:
			results.append(
				{"tool": call.get("tool"), "ok": False, "summary": "not an automatic step — do it by hand"}
			)
			break
		outcome = registry.dispatch(call["tool"], call.get("arguments") or {}, security.caller_ip())
		ok = not outcome.get("isError")
		results.append(
			{
				"tool": call["tool"],
				"ok": ok,
				"summary": " ".join(p.get("text", "") for p in outcome.get("content") or [])[:300],
			}
		)
		if not ok:
			break
		frappe.db.commit()
	if not all(r["ok"] for r in results):
		proposal["last_attempt"] = {"by": actor, "at": frappe.utils.now(), "results": results}
		frappe.db.set_value("Item", item, "compliance_proposal_json", json.dumps(proposal, default=str))
		frappe.db.commit()
		return ToolResult(
			data={"item": item, "ok": False, "results": results}, summary=f"{item}: stopped — see results"
		)
	result = label_compliance.attach(item, f"approved by {actor}")
	return ToolResult(
		data={
			"item": item,
			"ok": result["state"] == "Active",
			"state": result["state"],
			"results": results,
			"note": args.get("note"),
		},
		summary=f"{item}: {result['state'] or 'no profile matches'}",
		docstatus_delta="0 → 0 (updated)",
	)


def reject_label_compliance(args: dict) -> ToolResult:
	actor = _gate("reject label compliance")
	item = as_str(args, "item", required=True)
	reason = as_str(args, "reason")
	if not reason:
		raise ToolError("reason is required.")
	row = frappe.db.get_value("Item", item, ["compliance_state", "compliance_proposal_json"], as_dict=True)
	if not row or row.get("compliance_state") != "Proposed":
		raise ToolError(f"{item} has no pending proposal.")
	proposal = json.loads(row.get("compliance_proposal_json") or "{}")
	proposal["rejected"] = {"by": actor, "at": frappe.utils.now(), "reason": reason}
	frappe.db.set_value(
		"Item", item, {"compliance_state": "Rejected", "compliance_proposal_json": json.dumps(proposal)}
	)
	return ToolResult(
		data={"item": item, "state": "Rejected", "reason": reason}, summary=f"{item}: proposal rejected"
	)
