# SPDX-License-Identifier: MIT
"""Label-driven compliance. v0.207.0.

docs/design/phone_config_and_compliance_loop.md §5. A Label Profile (a Farm
Config Version kind) says: WHEN a product's label facts look like this, ATTACH
these programs, templates, rules and requirements. Registering and validating a
label matches the product against every Published profile. What is already
live attaches at once; anything that would activate something new becomes a
proposal of MCP calls a person approves in one step. No rule is edited per
product — the rules and templates stay generic and read the product's facts.
"""

from __future__ import annotations

import json

import frappe

from . import compat, phone_config

ITEM = "Item"
OPS = ("equals", "in", "truthy", "falsy", "gte", "lte", "contains")
STATES = ("Active", "Proposed", "Rejected")
#: The Item columns this module owns — written by attach/approve, never by create_item.
ITEM_FIELDS = ("compliance_state", "compliance_profiles_json", "compliance_proposal_json")
PART_KEYS = ("programs", "task_templates", "inspection_templates", "compliance_rules")


def facts() -> tuple:
	from . import product_labels

	return tuple(product_labels.KEY_FIELDS)


# ── validation ──────────────────────────────────────────────────────────────
def _when_problems(when, path="when") -> list:
	if not isinstance(when, dict) or not when:
		return [f"{path} must be an object"]
	for group in ("all", "any"):
		if group in when:
			parts = when[group]
			if not isinstance(parts, list) or not parts:
				return [f"{path}.{group} needs a list"]
			out = []
			for index, part in enumerate(parts):
				out += _when_problems(part, f"{path}.{group}[{index}]")
			return out
	if when.get("fact") not in facts():
		return [f"{path}.fact must be a label field ({', '.join(facts())})"]
	if when.get("op") not in OPS:
		return [f"{path}.op must be one of {', '.join(OPS)}"]
	if when["op"] in ("equals", "in", "gte", "lte", "contains") and "value" not in when:
		return [f"{path} needs a value"]
	return []


def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors = []
	if not str((body.get("title") or {}).get("en") or "").strip():
		errors.append("title.en is required")
	errors += _when_problems(body.get("when"))
	attach = body.get("attach") or {}
	if not isinstance(attach, dict):
		errors.append("attach must be an object")
		attach = {}
	for part in PART_KEYS:
		if not isinstance(attach.get(part, []), list):
			errors.append(f"attach.{part} must be a list")
	for program in attach.get("programs") or []:
		from . import programs

		if program not in programs.SHIPPED:
			errors.append(f"no shipped program {program!r}")
	for name in attach.get("task_templates") or []:
		if not frappe.db.exists("Farm Task Template", name):
			errors.append(f"no Farm Task Template {name!r}")
	for name in attach.get("inspection_templates") or []:
		from . import sessions

		if not sessions.resolve_template(name):
			errors.append(f"no Inspection Template {name!r}")
	for rule in attach.get("compliance_rules") or []:
		from . import compliance_rules

		if not compliance_rules.resolve(rule):
			errors.append(f"no Compliance Rule {rule!r}")
	requirements = attach.get("requirements") or {}
	if not isinstance(requirements, dict) or not isinstance(requirements.get("certifications", []), list):
		errors.append("attach.requirements.certifications must be a list")
	lang_errors, lang_warnings = phone_config.language_findings(body, for_publish)
	return {"errors": errors + lang_errors, "warnings": lang_warnings}


# ── matching ────────────────────────────────────────────────────────────────
def _holds(when: dict, item: dict) -> bool:
	if "all" in when:
		return all(_holds(part, item) for part in when["all"])
	if "any" in when:
		return any(_holds(part, item) for part in when["any"])
	value = item.get(when["fact"])
	op, wanted = when["op"], when.get("value")
	if op == "truthy":
		return bool(value) and str(value).strip().lower() not in ("0", "no", "false", "none")
	if op == "falsy":
		return not _holds({**when, "op": "truthy"}, item)
	if op == "equals":
		return str(value if value is not None else "").strip().lower() == str(wanted).strip().lower()
	if op == "in":
		return str(value if value is not None else "").strip().lower() in {
			str(v).strip().lower() for v in wanted or []
		}
	if op == "contains":
		return str(wanted).strip().lower() in str(value or "").lower()
	try:
		number, bound = float(value), float(wanted)
	except (TypeError, ValueError):
		return False
	return number >= bound if op == "gte" else number <= bound


def matched_facts(when: dict, item: dict) -> dict:
	out = {}
	if "all" in when or "any" in when:
		for part in when.get("all") or when.get("any") or []:
			out.update(matched_facts(part, item))
		return out
	out[when["fact"]] = item.get(when["fact"])
	return out


def item_facts(item_code: str) -> dict:
	fields = [f for f in facts() if compat.has_field(ITEM, f)]
	return dict(frappe.db.get_value(ITEM, item_code, ["name", *fields], as_dict=True) or {})


def profiles() -> list:
	"""[(doc, body)] of every Published (or staged-for-nobody-in-particular) profile."""
	return phone_config.served("Label Profile")


def _part_state(kind: str, name: str) -> str:
	"""'active', 'missing' or 'inactive' for one attached part."""
	if kind == "programs":
		from . import programs

		try:
			report = programs.status(programs.shipped(name))
		except Exception:
			return "missing"
		return "active" if report.get("installed") == "complete" else "missing"
	if kind == "task_templates":
		if not frappe.db.exists("Farm Task Template", name):
			return "missing"
		return (
			"active"
			if compat.checked(frappe.db.get_value("Farm Task Template", name, "enabled"))
			else "inactive"
		)
	if kind == "inspection_templates":
		from . import sessions

		live = sessions.live_template(name) or (
			name if frappe.db.exists("Inspection Template", name) else None
		)
		return (
			"active"
			if live and compat.checked(frappe.db.get_value("Inspection Template", live, "active"))
			else "missing"
		)
	from . import compliance_rules

	rule = compliance_rules.resolve(name)
	if not rule:
		return "missing"
	return "active" if compat.checked(frappe.db.get_value("Compliance Rule", rule, "enabled")) else "inactive"


def _calls_for(kind: str, name: str, state: str) -> list:
	if kind == "programs":
		return [
			{
				"tool": "import_program",
				"arguments": {"program": name, "dry_run": False},
				"why": f"install the {name} program",
			}
		]
	if kind == "task_templates" and state == "inactive":
		return [
			{
				"tool": "update_farm_task_template",
				"arguments": {"template": name, "enabled": True},
				"why": f"enable {name}",
			}
		]
	if kind == "compliance_rules" and state == "inactive":
		return [
			{
				"tool": "approve_compliance_rule",
				"arguments": {"name": name},
				"why": f"approve and enable {name}",
			}
		]
	return [
		{
			"tool": "(none)",
			"arguments": {},
			"why": f"{kind[:-1]} {name} is missing and has no automatic install",
		}
	]


def evaluate(item_code: str, only=None) -> dict:
	"""What would attach to one product: {matched, attached, proposed_calls, state}. Reads only."""
	item = item_facts(item_code)
	matched, parts, calls = [], [], []
	for doc, body in only if only is not None else profiles():
		when = body.get("when") or {}
		if not when or not _holds(when, item):
			continue
		attach = body.get("attach") or {}
		entry = {
			"profile": doc.config_key,
			"config_version": phone_config.version_string(doc),
			"matched_facts": matched_facts(when, item),
			"requirements": attach.get("requirements") or {},
			"parts": [],
		}
		for kind in PART_KEYS:
			for name in attach.get(kind) or []:
				state = _part_state(kind, name)
				entry["parts"].append({"kind": kind, "name": name, "state": state})
				if state != "active":
					calls += _calls_for(kind, name, state)
		matched.append(entry)
		parts += entry["parts"]
	state = "" if not matched else ("Proposed" if calls else "Active")
	return {"item": item_code, "matched": matched, "proposed_calls": calls, "state": state}


def attach(item_code: str, source: str = "label registration") -> dict:
	"""Match and write: Active straight away, or Proposed with calls. Never enables anything."""
	result = evaluate(item_code)
	if not compat.has_field(ITEM, "compliance_state") or not result["matched"]:
		return result
	values = {
		"compliance_profiles_json": json.dumps(result["matched"], default=str),
		"compliance_state": result["state"],
	}
	if result["state"] == "Proposed":
		values["compliance_proposal_json"] = json.dumps(
			{"calls": result["proposed_calls"], "proposed_at": frappe.utils.now(), "source": source},
			default=str,
		)
	else:
		values["compliance_proposal_json"] = None
	frappe.db.set_value(ITEM, item_code, values, update_modified=False)
	return result


def after_label(item_code: str, validation: dict) -> dict | None:
	"""Called by register_product_label. Never raises."""
	try:
		if str(validation.get("status") or "") == "Rejected" or int(validation.get("error_count") or 0):
			return {"state": "", "skipped": "the label did not validate", "matched": [], "proposed_calls": []}
		result = attach(item_code)
		return {
			"state": result["state"],
			"matched": [m["profile"] for m in result["matched"]],
			"attached": [p for m in result["matched"] for p in m["parts"] if p["state"] == "active"],
			"proposed_calls": result["proposed_calls"],
		}
	except Exception:
		frappe.log_error(title="label compliance", message=frappe.get_traceback())
		return None


def rematch_all() -> list:
	"""After a profile publishes: every label-validated Item, through the same path."""
	if not compat.has_field(ITEM, "label_scan_validation"):
		return []
	done = []
	for row in frappe.db.get_all(
		ITEM, filters={"label_scan_validation": ("not in", ("", None))}, fields=["name"], limit=5000
	):
		attach(row["name"], "profile publish")
		done.append(row["name"])
	return done


# ── runtime: what a product demands of the work ─────────────────────────────
def requirements(item_code: str) -> dict:
	"""{certifications, ppe_attestation} from a product's ACTIVE profiles."""
	out = {"certifications": [], "ppe_attestation": False}
	if not item_code or not compat.has_field(ITEM, "compliance_state"):
		return out
	row = (
		frappe.db.get_value(ITEM, item_code, ["compliance_state", "compliance_profiles_json"], as_dict=True)
		or {}
	)
	if row.get("compliance_state") != "Active":
		return out
	try:
		attached = json.loads(row.get("compliance_profiles_json") or "[]")
	except ValueError:
		return out
	for entry in attached or []:
		req = entry.get("requirements") or {}
		out["certifications"] += [
			c for c in req.get("certifications") or [] if c not in out["certifications"]
		]
		out["ppe_attestation"] = out["ppe_attestation"] or bool(req.get("ppe_attestation"))
	return out


def products_of(task: dict) -> list:
	"""The Items a task handles: bait_product, materials_used, and form Item links."""
	out = []
	if task.get("bait_product"):
		out.append(task["bait_product"])
	try:
		materials = (
			json.loads(task.get("materials_used") or "[]")
			if isinstance(task.get("materials_used"), str)
			else task.get("materials_used") or []
		)
	except ValueError:
		materials = []
	out += [m.get("item_code") for m in materials if isinstance(m, dict) and m.get("item_code")]
	return [code for code in dict.fromkeys(out) if code]


def certifications_for(task: dict) -> list:
	out = []
	for code in products_of(task):
		out += [c for c in requirements(code)["certifications"] if c not in out]
	return out


# ── seeds ───────────────────────────────────────────────────────────────────
SEEDS = {
	"rodenticide_bait_station": {
		"title": {"en": "Rodenticide in bait stations", "es": "Rodenticida en estaciones de cebo"},
		"when": {
			"all": [
				{"fact": "pesticide_use_scope", "op": "equals", "value": "Non-crop"},
				{
					"any": [
						{"fact": "tamper_resistant_station_required", "op": "truthy"},
						{"fact": "product_form", "op": "contains", "value": "bait"},
					]
				},
			]
		},
		"attach": {
			"programs": ["rodent_bait"],
			"task_templates": [],
			"inspection_templates": [],
			"compliance_rules": [],
			"requirements": {"certifications": [], "ppe_attestation": False},
		},
		"priority": 10,
	},
	"restricted_use_pesticide": {
		"title": {"en": "Restricted-use pesticide", "es": "Plaguicida de uso restringido"},
		"when": {"fact": "restricted_use", "op": "truthy"},
		"attach": {"requirements": {"certifications": ["Applicator License"], "ppe_attestation": False}},
		"priority": 20,
	},
	"danger_signal_word": {
		"title": {"en": "Signal word DANGER", "es": "Palabra de advertencia PELIGRO"},
		"when": {"fact": "signal_word", "op": "equals", "value": "Danger"},
		"attach": {"requirements": {"certifications": [], "ppe_attestation": True}},
		"priority": 30,
	},
}


def seed() -> list:
	made = []
	for key, body in SEEDS.items():
		name = phone_config.seed(
			"Label Profile", key, body, "Built-in label profile, seeded at install (v0.207.0)."
		)
		if name:
			made.append(name)
	return made
