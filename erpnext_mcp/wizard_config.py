# SPDX-License-Identifier: MIT
"""Wizards as versioned data. v0.207.0.

docs/design/phone_config_and_compliance_loop.md §2. A wizard is a Farm Config
Version of kind Wizard: steps whose `form` is the SAME form_schema v2 that task
and inspection templates use (one renderer on the phone), branching by
`next: [{if, go}]`, English and Spanish, and a submit HANDLER from a fixed list.
The route table is no longer the allowlist — `HANDLERS` is.
"""

from __future__ import annotations

import json

import frappe

from . import audit, compat, form_schema, phone_config

MAX_STEPS = 20
MAX_FIELDS_PER_STEP = 40
MAX_FIELDS = 150
MAX_ANSWER_BYTES = 256 * 1024
CATEGORIES = ("HR", "Safety", "Assets", "Compliance", "Operations", "Other")
CONTEXT_KEYS = ("source_alert", "location_doctype", "location")
HR_ROLES = ("System Manager", "HR Manager", "HR User", "Farm Manager")

#: handler → the mobile route it files through, the params it must receive, who
#: may file it, and what it produces (for the compliance-loop preview).
HANDLERS = {
	"create_accident_report": {
		"route": "create_accident_report",
		"required": ("occurred_at", "incident_description"),
		"roles": (),
		"produces": "Accident Report",
	},
	"create_discipline_record": {
		"route": "create_discipline_record",
		"required": ("employee", "discipline_type"),
		"roles": HR_ROLES,
		"produces": "Employee discipline record",
	},
	"register_asset": {
		"route": "register_asset",
		"required": ("asset_type",),
		"roles": (),
		"produces": "Asset Register",
	},
	"create_employee": {
		"route": "create_employee",
		"required": ("first_name", "last_name"),
		"roles": HR_ROLES,
		"produces": "Employee",
	},
	"start_inspection": {
		"route": "start_inspection",
		"required": ("template",),
		"roles": (),
		"produces": "Inspection Session",
	},
	"report_field_task": {
		"route": "report_field_task",
		"required": ("description",),
		"roles": (),
		"produces": "Farm Task",
	},
	"report_asset_issue": {
		"route": "report_asset_issue",
		"required": ("asset_name", "description"),
		"roles": (),
		"produces": "Farm Task",
	},
	"start_template_task": {
		"route": "start_template_task",
		"required": ("template",),
		"roles": (),
		"produces": "Farm Task",
	},
}


def route_of(handler: str):
	spec = HANDLERS.get(handler)
	if not spec:
		return None
	return next((r for r in _routes() if r.path.rsplit("/", 1)[-1] == spec["route"]), None)


def _routes():
	from .farmops_api import routes as route_table

	return route_table.ROUTES


def accepted(handler: str) -> set:
	from .farmops_api import routes as route_table

	route = route_of(handler)
	return set(route_table.accepted_arguments(route.handler)) - {"user"} if route else set()


# ── the body ────────────────────────────────────────────────────────────────
def all_fields(body: dict) -> list:
	return [field for step in body.get("steps") or [] for field in (step.get("form") or [])]


def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	from . import tiles

	errors: list = []
	warnings: list = []
	if not str((body.get("title") or {}).get("en") or "").strip():
		errors.append("title.en is required")
	if body.get("category") not in CATEGORIES:
		errors.append(f"category must be one of {', '.join(CATEGORIES)}")
	if body.get("icon") and body["icon"] not in tiles.ICONS:
		errors.append(f"icon {body['icon']!r} is not on the allowlist (tiles.ICONS)")
	roles = body.get("required_roles") or []
	if not isinstance(roles, list):
		errors.append("required_roles must be a list")
		roles = []
	steps = body.get("steps")
	if not isinstance(steps, list) or not steps:
		return {"errors": [*errors, "steps must be a non-empty list"], "warnings": warnings}
	if len(steps) > MAX_STEPS:
		errors.append(f"at most {MAX_STEPS} steps")
	keys = [str(step.get("key") or "") for step in steps]
	for index, step_key in enumerate(keys):
		if not form_schema._KEY.match(step_key):
			errors.append(f"steps[{index}].key must be lower_snake_case")
	if len(set(keys)) != len(keys):
		errors.append("step keys must be unique")

	# One flat answer object → every field key unique, and conditions may name
	# a field on an earlier step: validate the whole wizard as one form.
	fields = all_fields(body)
	if len(fields) > MAX_FIELDS:
		errors.append(f"at most {MAX_FIELDS} fields in a wizard")
	report = form_schema.validate(fields, context_keys=form_schema.CONTEXT_KEYS + CONTEXT_KEYS)
	errors += [f"form {f['path']}: {f['message']}" for f in report["errors"]]
	warnings += [f"form {f['path']}: {f['message']}" for f in report["warnings"]]
	field_keys = [f.get("key") for f in fields if isinstance(f, dict)]
	duplicated = sorted({k for k in field_keys if k and field_keys.count(k) > 1})
	if duplicated:
		errors.append(f"field keys repeat across steps: {', '.join(duplicated)}")

	for index, step in enumerate(steps):
		form = step.get("form") or []
		if not isinstance(form, list):
			errors.append(f"steps[{index}].form must be a list")
			continue
		if len(form) > MAX_FIELDS_PER_STEP:
			errors.append(f"steps[{index}] has more than {MAX_FIELDS_PER_STEP} fields")
		if not str((step.get("title") or {}).get("en") or "").strip():
			errors.append(f"steps[{index}].title.en is required")
		for condition in [step.get("show_if")] + [
			n.get("if") for n in step.get("next") or [] if isinstance(n, dict)
		]:
			if condition:
				problem = form_schema._condition_problem(
					condition, fields, fields, form_schema.CONTEXT_KEYS + CONTEXT_KEYS
				)
				if problem:
					errors.append(f"steps[{index}] condition: {problem}")
		nxt = step.get("next", [])
		if not isinstance(nxt, list):
			errors.append(f"steps[{index}].next must be a list of {{if?, go}}")
			continue
		for rule in nxt:
			if not isinstance(rule, dict) or rule.get("go") not in keys:
				errors.append(f"steps[{index}].next names no step: {rule!r}")
	errors += _graph_problems(steps, keys)
	errors += _submit_problems(body, fields)
	for_roles = {"roles": roles} if roles else {}
	people = phone_config.audience_people(for_roles)
	if not people:
		(errors if for_publish else warnings).append("nobody with a phone holds the required roles")
	if for_publish and fields:
		from . import device_capabilities

		for line in device_capabilities.problems(fields):
			(errors if line.startswith("BLOCKING") else warnings).append(line)
	lang_errors, lang_warnings = phone_config.language_findings(body, for_publish)
	return {"errors": errors + lang_errors, "warnings": warnings + lang_warnings}


def _successors(step: dict) -> list:
	return [rule["go"] for rule in step.get("next") or [] if isinstance(rule, dict) and rule.get("go")]


def _graph_problems(steps: list, keys: list) -> list:
	by_key = {str(s.get("key")): s for s in steps}
	problems = []
	seen, stack = set(), set()

	def visit(key):
		if key in stack:
			problems.append(f"the steps loop back to {key!r}")
			return
		if key in seen or key not in by_key:
			return
		stack.add(key)
		for nxt in _successors(by_key[key]):
			visit(nxt)
		stack.discard(key)
		seen.add(key)

	if keys:
		visit(keys[0])
	unreachable = [k for k in keys if k not in seen]
	if unreachable:
		problems.append(f"steps no path reaches: {', '.join(unreachable)}")
	return problems


def _submit_problems(body: dict, fields: list) -> list:
	submit = body.get("submit") or {}
	handler = submit.get("handler")
	if handler not in HANDLERS:
		return [f"submit.handler must be one of {', '.join(HANDLERS)}"]
	taken = accepted(handler)
	if not taken:
		return [f"handler {handler} has no route on this site"]
	mapping = submit.get("map") or {}
	context = submit.get("context") or {}
	if not isinstance(mapping, dict) or not isinstance(context, dict):
		return ["submit.map and submit.context must be objects"]
	field_keys = {f.get("key") for f in fields if isinstance(f, dict)}
	out = []
	for source, target in mapping.items():
		if source not in field_keys:
			out.append(f"submit.map names no field {source!r}")
		if target not in taken:
			out.append(f"submit.map target {target!r} is not a parameter of {handler}")
	for name in context:
		if name not in taken:
			out.append(f"submit.context key {name!r} is not a parameter of {handler}")
	covered = set(mapping.values()) | {k for k in field_keys if k in taken} | set(context)
	missing = [name for name in HANDLERS[handler]["required"] if name not in covered]
	if missing:
		out.append(f"{handler} needs {', '.join(missing)} — map a field or set submit.context")
	return out


def unmapped(body: dict) -> list:
	"""Field keys the handler will not receive (a warning, reported to the phone too)."""
	submit = body.get("submit") or {}
	taken = accepted(submit.get("handler"))
	mapping = submit.get("map") or {}
	return sorted(
		f["key"]
		for f in all_fields(body)
		if isinstance(f, dict)
		and f.get("key")
		and f["key"] not in mapping
		and f["key"] not in taken
		and f.get("type") not in form_schema.DISPLAY_KINDS
	)


# ── the path answers take ───────────────────────────────────────────────────
def path(body: dict, answers: dict, context: dict | None = None) -> list:
	"""The step keys a worker visits with these answers, in order."""
	context = context or {}
	steps = body.get("steps") or []
	by_key = {str(s.get("key")): s for s in steps}
	out, key = [], str(steps[0].get("key")) if steps else ""
	while key and key in by_key and key not in out:
		step = by_key[key]
		if not step.get("show_if") or form_schema.holds(step["show_if"], answers, context):
			out.append(key)
		key = ""
		for rule in step.get("next") or []:
			if not rule.get("if") or form_schema.holds(rule["if"], answers, context):
				key = rule.get("go") or ""
				break
	return out


# ── what the phone is sent ──────────────────────────────────────────────────
_LEGACY = {
	"text": "text",
	"long_text": "text",
	"small_text": "text",
	"data": "text",
	"number": "number",
	"int": "number",
	"float": "number",
	"currency": "number",
	"percent": "number",
	"measurement": "number",
	"date": "date",
	"datetime": "date",
	"select": "select",
	"check": "select",
	"photo": "photo",
	"signature": "signature",
	"scan": "qr",
	"barcode": "qr",
}


def phone_spec(doc, body: dict, language: str = "en") -> dict:
	"""The `get_wizard_definition` answer for a config wizard: the v2 `form` per
	step plus the legacy per-field shape 0.20.x apps decode."""
	from .api.mobile import SUBMIT_WIZARD

	def en_es(value):
		value = value or {}
		return str(value.get("en") or ""), (str(value.get("es") or "") or None)

	title_en, title_es = en_es(body.get("title"))
	steps = []
	for step in body.get("steps") or []:
		step_en, step_es = en_es(step.get("title"))
		help_en, help_es = en_es(step.get("description"))
		legacy = []
		for field in step.get("form") or []:
			kind = field.get("type")
			if kind in form_schema.DISPLAY_KINDS:
				continue
			label_en, label_es = en_es(
				field.get("label") if isinstance(field.get("label"), dict) else {"en": field.get("label")}
			)
			options = [
				{
					"value": str(o.get("value")),
					"label_en": form_schema.text_of(o.get("label") or o.get("value"), "en"),
					"label_es": form_schema.text_of(o.get("label"), "es") or None,
				}
				for o in field.get("options") or []
				if isinstance(o, dict)
			]
			if kind == "check" and not options:
				options = [
					{"value": "1", "label_en": "Yes", "label_es": "Sí"},
					{"value": "0", "label_en": "No", "label_es": "No"},
				]
			legacy.append(
				{
					"key": field.get("key"),
					"fieldname": field.get("key"),
					"type": _LEGACY.get(kind, "unsupported"),
					"server_field_type": kind,
					"label_en": label_en,
					"label_es": label_es,
					"required": bool(field.get("required")),
					"options": options,
				}
			)
		steps.append(
			{
				"key": step.get("key"),
				"step_key": step.get("key"),
				"title_en": step_en,
				"title_es": step_es,
				"help_en": help_en,
				"help_es": help_es,
				"optional": False,
				"show_if": step.get("show_if"),
				"next": step.get("next") or [],
				"form": form_schema.for_phone(step.get("form") or []),
				"fields": legacy,
			}
		)
	success_en, success_es = en_es(body.get("success"))
	handler = (body.get("submit") or {}).get("handler")
	return {
		"name": doc.config_key,
		"wizard_key": doc.config_key,
		"config_version": phone_config.version_string(doc),
		"version": doc.version,
		"title_en": title_en,
		"title_es": title_es,
		"title": form_schema.text_of(body.get("title"), language),
		"description": form_schema.text_of(body.get("description"), language),
		"category": body.get("category"),
		"icon": body.get("icon"),
		"required_roles": body.get("required_roles") or [],
		"required_role": ", ".join(body.get("required_roles") or []) or None,
		"language": language,
		"steps": steps,
		"step_count": len(steps),
		"submit_method": handler,
		"submit_endpoint": f"farmops/api/mobile/{SUBMIT_WIZARD}",
		"submit_context": {},
		"submit_unmapped": unmapped(body),
		"success_en": success_en or None,
		"success_es": success_es,
		"source": "config",
	}


# ── submitting ──────────────────────────────────────────────────────────────
def _reference_key(reference: str) -> str:
	return f"wizard_submit:{reference}"[:140]


def earlier_submit(reference: str):
	"""The recorded answer of a submit already filed under this client_reference, or None."""
	if not reference:
		return None
	rows = frappe.db.get_all(
		audit.LOG_DOCTYPE,
		filters={"tool_name": _reference_key(reference), "result_status": audit.STATUS_SUCCESS},
		fields=["result_summary", "timestamp"],
		limit=1,
	)
	if not rows:
		return None
	try:
		return {
			"first": json.loads(rows[0]["result_summary"] or "{}"),
			"first_filed_at": str(rows[0]["timestamp"]),
		}
	except ValueError:
		return {"first": None, "first_filed_at": str(rows[0]["timestamp"])}


def record_submit(reference: str, answer: dict) -> None:
	if not reference:
		return
	compact = {
		"wizard": answer.get("wizard"),
		"config_version": answer.get("config_version"),
		"submit_method": answer.get("submit_method"),
		"result_name": (answer.get("result") or {}).get("name"),
	}
	audit.record(
		_reference_key(reference), {"client_reference": reference}, audit.STATUS_SUCCESS, json.dumps(compact)
	)


def load_for_submit(key: str, config_version: str = "", user: str = ""):
	"""(doc, body) the submit is checked against: the version the phone started with, else in force."""
	if config_version:
		kind_slug, _, rest = str(config_version).partition(":")
		wanted_key, _, version = rest.partition("@")
		if kind_slug != "wizard" or wanted_key != key or not version.isdigit():
			raise ValueError(f"config_version {config_version!r} is not a version of wizard {key!r}")
		doc = phone_config.doc_of("Wizard", key, int(version))
		if doc is None or doc.status in (phone_config.DRAFT,):
			raise ValueError(f"{config_version} is not a version this site serves")
	else:
		person = phone_config.person_of(user)
		doc, _body = phone_config.in_force("Wizard", key, user, "", person.get("roles") or ())
		if doc is None and phone_config.rows("Wizard", key):
			raise ValueError("this form was withdrawn — nothing was filed")
	if doc is None:
		return None, None
	if phone_config.rows("Wizard", key) and not phone_config.rows(
		"Wizard", key, (phone_config.STAGED, phone_config.PUBLISHED)
	):
		raise ValueError("this form was withdrawn — nothing was filed")
	return doc, phone_config.body_of(doc)


def check(body: dict, answers: dict, context: dict, user: str) -> dict:
	"""{answers, problems, path}: the answers on the path taken, checked like a task's."""
	roles = body.get("required_roles") or []
	if roles and not set(roles) & set(frappe.get_roles(user) or []):
		return {"answers": {}, "problems": [f"this form is for {' / '.join(roles)}"], "path": []}
	visited = path(body, answers, context)
	fields = [
		f for step in body.get("steps") or [] if step.get("key") in visited for f in step.get("form") or []
	]
	checked = form_schema.check_answers(fields, answers, context)
	return {"answers": checked["answers"], "problems": checked["problems"], "path": visited}


def handler_arguments(body: dict, answers: dict, context: dict) -> tuple:
	"""(arguments for the handler, ignored answer keys)."""
	submit = body.get("submit") or {}
	taken = accepted(submit.get("handler"))
	mapping = submit.get("map") or {}
	out = dict(submit.get("context") or {})
	ignored = []
	for key, value in answers.items():
		target = mapping.get(key) or key
		if target in taken:
			out[target] = value
		else:
			ignored.append(key)
	for key in ("location_doctype", "location"):
		if context.get(key) and key in taken and key not in out:
			out[key] = context[key]
	if context.get("source_alert") and "source_alert" in taken:
		out["source_alert"] = context["source_alert"]
	return out, sorted(ignored)


def seed_from_legacy() -> list:
	"""v0.207.0 patch: every Wizard Definition becomes version 1 Published of its key."""
	from .tools import wizards as wizard_tools

	made = []
	if not compat.doctype_exists("Wizard Definition"):
		return made
	for row in frappe.db.get_all("Wizard Definition", fields=["name", "enabled"], limit=500):
		key = str(row["name"])
		if not phone_config.KEY_PATTERN.match(key) or phone_config.rows("Wizard", key):
			continue
		try:
			body = legacy_body(key, wizard_tools)
		except Exception:
			frappe.log_error(title=f"wizard {key} not converted", message=frappe.get_traceback())
			continue
		name = phone_config.seed(
			"Wizard", key, body, "Converted from the legacy Wizard Definition (v0.207.0)."
		)
		if name:
			if not int(row.get("enabled") or 0):
				doc = frappe.get_doc(phone_config.DOCTYPE, name)
				doc.status = phone_config.RETIRED
				doc.change_note = "The legacy wizard was disabled."
				phone_config._save(doc)
			made.append(name)
	return made


def legacy_body(key: str, wizard_tools) -> dict:
	doc = frappe.get_doc("Wizard Definition", key)
	english = wizard_tools.describe(doc, "en")
	spanish = wizard_tools.describe(doc, "es")
	es_steps = {s.get("step_key"): s for s in spanish.get("steps") or []}
	steps = []
	raw_steps = english.get("steps") or []
	for index, step in enumerate(raw_steps):
		es = es_steps.get(step.get("step_key")) or {}
		form = form_schema.from_wizard_fields(step.get("fields") or [], es.get("fields") or [])
		nxt = step.get("next_step") or (
			raw_steps[index + 1].get("step_key") if index + 1 < len(raw_steps) else ""
		)
		steps.append(
			{
				"key": step.get("step_key"),
				"title": {
					"en": step.get("title") or "",
					"es": es.get("title") if es.get("title") != step.get("title") else "",
				},
				"description": {"en": step.get("description") or "", "es": es.get("description") or ""},
				"form": form,
				"next": [{"go": nxt}] if nxt else [],
			}
		)
	method = english.get("submit_method") or ""
	handler = method if method in HANDLERS else "report_field_task"
	roles = [r.strip() for r in str(english.get("required_role") or "").split(",") if r.strip()]
	return {
		"title": {
			"en": english.get("title") or key,
			"es": spanish.get("title") if spanish.get("title") != english.get("title") else "",
		},
		"description": {"en": english.get("description") or "", "es": spanish.get("description") or ""},
		"category": english.get("category") if english.get("category") in CATEGORIES else "Other",
		"icon": None,
		"required_roles": roles,
		"steps": steps,
		"submit": {"handler": handler, "map": {}, "context": {}},
		"allow_english_only": True,
	}
