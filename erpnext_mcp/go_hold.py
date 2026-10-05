# SPDX-License-Identifier: MIT
"""Go / Hold on a Farm Task. v0.240.0. docs/design/ccf_core_work_timing.md §4 (approved; queue item 5).

A WORK TIMING RULE is a Compliance Rule in category "Work Timing", targeting Farm Task, with a
condition tree and `set_go_hold` (or `block_start`) among its actions. Its scope filters say which
tasks it speaks to (by type, template, name…). Every such rule on a task must pass for Go
(decision 39).

* NOT SWEPT: the alert sweep skips these rules unless their actions include `alert` — a Hold is shown
  on the task, not filed in the inbox once per task per sweep.
* WHEN: at the day-start check (06:00, decision 16), at task start, and on demand (`check_go_hold`).
  Each check is logged on the task with each rule's id, version, verdict and the values it read
  (`go_hold_log`, newest first, capped) — "why was B7 held on Tuesday" is answerable later.
* AUTO-CLEAR (decision 15): a Hold is a verdict, not a state. The next check that passes sets Go and
  the log says it cleared.
* ADVISORY BY DEFAULT (decision 39): an Advisory rule's Hold is shown, the work can still start, and
  the start says so. Only a rule with `block_start` among its actions (Enforced) refuses the start — until a supervisor
  overrides it with a reason (decision 17). A Hold never touches time already worked (decision 16).
* NO STAGE (decision 12): when the only thing a rule could not judge is the crop stage, the verdict
  is "Go — verify stage" with the stage to check, not a Hold.
* Missing WEATHER is a Hold saying "no data" — never a silent Go.
"""

from __future__ import annotations

import json

import frappe

from . import ccf, compat, compliance_rules

DOCTYPE = "Farm Task"
CATEGORY = "Work Timing"
GO, HOLD, VERIFY = "Go", "Hold", "Go — verify stage"
OPEN_STATES = ("Draft", "Available", "Claimed", "Paused")
FIELDS = ("go_hold", "go_hold_reasons", "go_hold_checked_at", "go_hold_log", "go_hold_override")
LOG_CAP = 30
OVERRIDE_ROLES = ("System Manager", "Farm Manager", "Foreman")
GO_HOLD_ACTIONS = ("set_go_hold", "block_start")


def installed() -> bool:
	return compat.has_field(DOCTYPE, "go_hold")


def _actions(row: dict) -> set:
	try:
		return {a.get("type") for a in ccf.parse_actions(row.get("actions_json"))}
	except ccf.TreeError:
		return set()


def is_go_hold_rule(row: dict) -> bool:
	return (
		str(row.get("category") or "") == CATEGORY
		and str(row.get("target_doctype") or "") == DOCTYPE
		and str(row.get("condition_tree_json") or "").strip() not in ("", "{}")
		and bool(_actions(row) & set(GO_HOLD_ACTIONS))
	)


def enforced(row: dict) -> bool:
	"""ENFORCED MEANS THE RULE CARRIES `block_start`. `enforcement_mode` belongs to transaction gates
	(a control point, one rule each); a Work Timing rule says what it does in its actions."""
	return "block_start" in _actions(row)


def rules_for(task: dict) -> list:
	"""The live Go/Hold rules whose scope takes in this task."""
	out = []
	for row in compliance_rules.rule_rows():
		if not is_go_hold_rule(row):
			continue
		try:
			filters = compliance_rules.parse_filters(row.get("scope_filters_json"))
		except ValueError:
			continue
		matched, _ = compliance_rules.row_matches(task, filters, set(task))
		if matched:
			out.append(row)
	return out


def _leaf_paths(node) -> list:
	if not isinstance(node, dict):
		return []
	paths = [node["path"]] if node.get("path") else []
	for key in ("all", "any"):
		for child in node.get(key) or []:
			paths += _leaf_paths(child)
	if "not" in node:
		paths += _leaf_paths(node["not"])
	return paths


def _snapshot(tree: dict, values: dict) -> dict:
	"""The values the rule read — series cut to the window it looked at."""
	out = {}
	for path in _leaf_paths(tree):
		try:
			value = ccf.resolve(values, path)
		except Exception:
			value = None
		if isinstance(value, list):
			value = value[:16]
		out[path] = value
	return out


def _only_stage_missing(failures: list) -> bool:
	"""True when every failing check failed for want of a crop stage (decision 12)."""
	leaves = []

	def flat(entries):
		for entry in entries:
			if entry.get("leaves"):
				flat(entry["leaves"])
			else:
				leaves.append(entry)

	flat(failures)
	return bool(leaves) and all(leaf.get("missing") and str(leaf.get("path") or "").startswith("phenology.") for leaf in leaves)


def evaluate(task: dict, language: str = "en", as_of: str = "") -> dict:
	"""The verdict for one task, without writing anything."""
	rules = rules_for(task)
	if not rules:
		return {"status": "", "rules": [], "reasons": [], "enforced_hold": False}
	needed = set()
	parsed = []
	for row in rules:
		tree = ccf.parse_tree(row.get("condition_tree_json"))
		needed |= ccf.providers_in(tree)
		parsed.append((row, tree))
	values = ccf.build_context(task, doctype=DOCTYPE, company=str(task.get("company") or ""), as_of=as_of,
	                           providers=needed)
	results, reasons, verify, enforced_hold = [], [], [], False
	for row, tree in parsed:
		result = ccf.evaluate(tree, values, language)
		if result["passed"]:
			verdict = GO
		elif _only_stage_missing(result["failures"]):
			verdict = VERIFY
			verify.append(f"{row.get('title') or row.get('rule_id')}: no crop stage recorded for "
			              f"{task.get('location') or 'this block'} — check the stage in the field.")
		else:
			verdict = HOLD
			reasons.append(result["hold"])
			enforced_hold = enforced_hold or enforced(row)
		results.append(
			{
				"rule_id": row.get("rule_id"),
				"rule": row.get("name"),
				"version": int(row.get("version") or 1),
				"enforcement": "Enforced" if enforced(row) else "Advisory",
				"verdict": verdict,
				"hold": result["hold"],
				"read": _snapshot(tree, values),
			}
		)
	status = HOLD if reasons else (VERIFY if verify else GO)
	return {"status": status, "rules": results, "reasons": reasons + verify, "enforced_hold": enforced_hold}


def _load_json(raw, default):
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
	except ValueError:
		return default
	return value if isinstance(value, type(default)) else default


def check(task_name: str, moment: str = "on_demand", language: str = "en", write: bool = True) -> dict:
	"""Evaluate and (by default) record the verdict on the task. Returns the verdict plus `changed`."""
	if not installed() or not frappe.db.exists(DOCTYPE, task_name):
		return {"status": "", "rules": [], "reasons": [], "enforced_hold": False, "changed": False}
	task = dict(frappe.get_doc(DOCTYPE, task_name).as_dict())
	verdict = evaluate(task, language)
	before = str(task.get("go_hold") or "")
	verdict["changed"] = verdict["status"] != before
	verdict["cleared"] = before == HOLD and verdict["status"] in (GO, VERIFY)
	if not write or (not verdict["rules"] and not before):
		return verdict
	now = frappe.utils.now()
	log = _load_json(task.get("go_hold_log"), [])
	log.insert(
		0,
		{
			"at": now,
			"moment": moment,
			"status": verdict["status"],
			"from": before,
			"cleared": verdict["cleared"],
			"rules": verdict["rules"],
		},
	)
	frappe.db.set_value(
		DOCTYPE,
		task_name,
		{
			"go_hold": verdict["status"],
			"go_hold_reasons": "\n".join(verdict["reasons"]),
			"go_hold_checked_at": now,
			"go_hold_log": json.dumps(log[:LOG_CAP], default=str),
		},
		update_modified=False,
	)
	return verdict


def override_today(task: dict) -> dict | None:
	"""Today's supervisor override, if one was given (it lapses at midnight)."""
	override = _load_json(task.get("go_hold_override"), {})
	if override and str(override.get("on") or "")[:10] == str(frappe.utils.today())[:10]:
		return override
	return None


def can_override(user: str) -> bool:
	return user == "Administrator" or bool(set(frappe.get_roles(user) or []) & set(OVERRIDE_ROLES))


def override(task_name: str, reason: str, user: str, employee: str = "") -> dict:
	"""A supervisor lets an Enforced Hold start today, with a reason (decision 17)."""
	reason = str(reason or "").strip()
	if len(reason) < 5:
		raise ValueError("a reason is required (a few words: why it is safe to start today).")
	if not can_override(user):
		raise PermissionError(f"{user} cannot override a Hold — a Foreman, Farm Manager or System Manager can.")
	task = dict(frappe.get_doc(DOCTYPE, task_name).as_dict())
	if str(task.get("state") or "") not in OPEN_STATES:
		raise ValueError(f"{task_name} is {task.get('state')}; only a task not yet started can be overridden.")
	now = frappe.utils.now()
	entry = {"on": str(frappe.utils.today())[:10], "at": now, "by": user, "employee": employee, "reason": reason,
	         "status_then": task.get("go_hold") or "", "reasons_then": task.get("go_hold_reasons") or ""}
	log = _load_json(task.get("go_hold_log"), [])
	log.insert(0, {"at": now, "moment": "override", "status": task.get("go_hold") or "", "override": entry})
	frappe.db.set_value(
		DOCTYPE, task_name,
		{"go_hold_override": json.dumps(entry), "go_hold_log": json.dumps(log[:LOG_CAP], default=str)},
		update_modified=False,
	)
	return entry


def at_start(task_name: str, language: str = "en") -> dict:
	"""The check at task start. Raises ValueError for an Enforced Hold with no override today;
	otherwise returns what the start should say (empty when no rule speaks to the task)."""
	verdict = check(task_name, "task_start", language)
	if not verdict["rules"]:
		return {}
	out = {"status": verdict["status"], "reasons": verdict["reasons"]}
	if verdict["status"] == HOLD:
		task = dict(frappe.get_doc(DOCTYPE, task_name).as_dict())
		given = override_today(task)
		if verdict["enforced_hold"] and not given:
			raise ValueError(
				f"{task_name} is on Hold: " + "; ".join(verdict["reasons"])
				+ ". A supervisor can let it start today with a reason (override_hold)."
			)
		if given:
			out["override"] = given
			out["note"] = f"On Hold, started under {given['by']}'s override: {given['reason']}"
		else:
			out["note"] = "Advisory Hold — started anyway: " + "; ".join(verdict["reasons"])
	elif verdict["status"] == VERIFY:
		out["note"] = "; ".join(verdict["reasons"])
	return out


def day_start(company: str = "") -> dict:
	"""06:00: re-check every open task a Go/Hold rule speaks to (decision 16). Advisory; never raises."""
	report = {"checked": 0, "hold": 0, "cleared": 0, "failed": 0}
	if not installed():
		return report
	if not any(is_go_hold_rule(row) for row in compliance_rules.rule_rows()):
		return report
	filters = {"state": ("in", list(OPEN_STATES))}
	if company:
		filters["company"] = company
	for row in frappe.db.get_all(DOCTYPE, filters=filters, fields=["name"], limit=2000) or []:
		try:
			verdict = check(row["name"], "day_start")
		except Exception:
			report["failed"] += 1
			frappe.log_error(title="Go/Hold day-start check failed", message=frappe.get_traceback())
			continue
		if verdict["rules"]:
			report["checked"] += 1
			report["hold"] += verdict["status"] == HOLD
			report["cleared"] += bool(verdict.get("cleared"))
	return report


def scheduled_day_start() -> None:
	"""The 06:00 cron entry."""
	day_start()


def describe(task: dict) -> dict:
	if not installed():
		return {}
	return {
		"go_hold": task.get("go_hold") or "",
		"go_hold_reasons": [r for r in str(task.get("go_hold_reasons") or "").splitlines() if r],
		"go_hold_checked_at": task.get("go_hold_checked_at"),
		"override_today": override_today(task),
	}


# ── presets (seeded DISABLED; decision 5: switched on by a person) ─────────
def _preset(rule_id, title, description, tree, scope, purpose):
	return {
		"rule_id": rule_id,
		"title": title,
		"category": CATEGORY,
		"target_doctype": DOCTYPE,
		"condition_tree": tree,
		"evaluation": {"when": ["day_start", "task_start"]},
		"actions": [{"type": "set_go_hold"}],
		"scope_filters": [{"field": "state", "op": "in", "value": list(OPEN_STATES)}] + scope,
		"kairotic_gate_description": description,
		"regimes": ["Internal"],
		"enabled": 0,
		"authored_by": "System",
		"purpose": purpose,
		"default_severity": "Info",
	}


def preset_specs() -> list:
	return [
		_preset(
			"go_hold_pruning_canker",
			"Pruning: dry spell for wound healing (bacterial canker)",
			"Pruning cuts stay open to Pseudomonas while wet. Go when the chance of rain over the next 7 days "
			"stays under 40%, it has been dry 24 hours, and no frost in the next 2 days.",
			{"all": [
				{"id": "dry_ahead", "path": "weather.forecast.daily[0..6].rain_risk_cum_pct", "agg": "max", "op": "lt",
				 "value": 40, "basis": "local_judgment",
				 "reason": {"en": "Rain likely this week — cuts would stay wet", "es": "Lluvia probable esta semana — los cortes quedarían mojados"}},
				{"id": "dry_now", "path": "weather.recent.hours_since_rain", "op": "gte", "value": 24, "basis": "local_judgment",
				 "reason": {"en": "Rained in the last 24 hours", "es": "Llovió en las últimas 24 horas"}},
				{"id": "no_frost", "path": "weather.forecast.daily[0..1].tmin_f", "agg": "min", "op": "gt", "value": 28,
				 "reason": {"en": "Frost forecast", "es": "Pronóstico de helada"}},
			]},
			[{"field": "task_name", "op": "contains", "value": "prun"}],
			"Prune in dry weather so wounds close before bacterial canker gets in.",
		),
		_preset(
			"go_hold_spray_wind",
			"Spraying: wind at or under 10 mph",
			"Drift. Go when today's forecast wind is 10 mph or less (the label's own limit, where lower, is what "
			"the applicator follows).",
			{"id": "wind_ok", "path": "weather.forecast.daily[0].wind_mph", "op": "lte", "value": 10, "basis": "published",
			 "reason": {"en": "Too windy to spray (over 10 mph)", "es": "Demasiado viento para fumigar (más de 10 mph)"}},
			[{"field": "task_type", "op": "eq", "value": "Spray"}],
			"Keep spray on target.",
		),
		_preset(
			"go_hold_burn",
			"Burning: calm and not dry-windy",
			"Go when gusts stay under 15 mph and the high under 85 °F, today and tomorrow (decision 23: now and "
			"ahead). Check the county burn status and any red-flag warning before lighting.",
			{"all": [
				{"id": "gusts_ok", "path": "weather.forecast.daily[0..1].gust_mph", "agg": "max", "op": "lt", "value": 15,
				 "reason": {"en": "Gusty — no burning", "es": "Ráfagas — no se quema"}},
				{"id": "not_hot", "path": "weather.forecast.daily[0..1].tmax_f", "agg": "max", "op": "lt", "value": 85,
				 "reason": {"en": "Hot and dry — no burning", "es": "Calor y seco — no se quema"}},
			]},
			[{"field": "task_name", "op": "contains", "value": "burn"}],
			"Burn piles only when a fire stays where it is put.",
		),
		# v0.245.0. Work an SOP covers waits for that SOP's approval. Advisory first (the design note's
		# risk: a version left In Review must not stop real work by surprise).
		_preset(
			"go_hold_sop_approved",
			"SOP approved before the work it covers",
			"Go when every SOP covering this task's type is approved; a new version in review does not un-approve "
			"the one in force.",
			{"id": "sop_approved", "path": "sop.unapproved_count", "op": "eq", "value": 0,
			 "reason": {"en": "An SOP for this work is not approved yet", "es": "Un SOP para este trabajo aún no está aprobado"}},
			[],
			"Work follows an approved procedure.",
		),
		_preset(
			"go_hold_harvest_heat",
			"Harvest: heat",
			"Fruit picked hot bruises and softens, and crews are at risk. Go when today's high is under 95 °F; "
			"above that, pick early and stop by noon.",
			{"id": "not_too_hot", "path": "weather.forecast.daily[0].tmax_f", "op": "lt", "value": 95,
			 "reason": {"en": "Over 95 °F today — pick early, stop by noon", "es": "Más de 95 °F hoy — cosecha temprano, para al mediodía"}},
			[{"field": "task_type", "op": "eq", "value": "Harvest"}],
			"Fruit quality and crew safety in the heat.",
		),
	]


def seed() -> list:
	"""Create-only; never raises."""
	made = []
	if not compat.doctype_exists(compliance_rules.DOCTYPE):
		return made
	for spec in preset_specs():
		try:
			if frappe.db.exists(compliance_rules.DOCTYPE, {"rule_id": spec["rule_id"]}):
				continue
			compliance_rules.build_rule(spec).insert(ignore_permissions=True)
			made.append(spec["rule_id"])
		except Exception:
			frappe.log_error(title=f"Go/Hold preset {spec['rule_id']} not seeded", message=frappe.get_traceback())
	return made
