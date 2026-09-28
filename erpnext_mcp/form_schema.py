# SPDX-License-Identifier: MIT
"""The form schema: one field vocabulary the phone renders generically. v0.204.0.

docs/design/form_schema_and_labels.md §1. Tim, on templates created through
MCP: "That would be awsome." — any Farm Task Template or Inspection Template a
model authors must render on the iPhone with no code change. This module is the
whole vocabulary, in one place, used by:

  * Farm Task Template `form_schema` (snapshotted onto Farm Task),
  * Inspection Template sections' `field_prompts` (as a list),
  * the mobile wizard spec (`form` per step).

THE SERVER EVALUATES WHAT THE PHONE EVALUATES. `validate` refuses a schema the
phone cannot render; `check_answers` refuses a completion the schema does not
allow, with the same `show_if` / `required_if` the phone applies — a hidden
field is neither required nor stored.

A TEMPLATE WITH NO SCHEMA STILL RENDERS. `legacy_fields` turns the old
checklist into fields, and `ticks_from_answers` turns answers back into the old
ticks, so every reader of `checklist_status` is unchanged.
"""

from __future__ import annotations

import json
import re

TYPES = (
	"select",
	"multi_select",
	"check",
	"attestation",
	"text",
	"long_text",
	"number",
	"measurement",
	"date",
	"datetime",
	"photo",
	"signature",
	"gps",
	"link",
	"group",
	"approval",
	"info",
)
FIELD_KEYS = {
	"key",
	"type",
	"label",
	"help",
	"required",
	"show_if",
	"required_if",
	"options",
	"min",
	"max",
	"step",
	"uom",
	"min_count",
	"max_count",
	"link",
	"fields",
	"role",
	"before_start",
	"statement",
}
LINK_DOCTYPES = ("Item", "Asset Register", "Housing Unit", "Employee")
CONTEXT_KEYS = (
	"occupancy_at_creation",
	"bait_placement",
	"location_doctype",
	"asset_type",
	"task_type",
	"template",
	"language",
)
OPERATORS = ("equals", "not_equals", "in", "not_in", "truthy", "falsy")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,59}$")

#: Legacy checklist evidence_type → field type (§1.5).
LEGACY_TYPES = {"None": "check", "Photo": "photo", "Text": "text", "Measurement": "measurement"}

#: Warnings, as (code, pattern over the English label, message). §1.4.
_PROSE_BRANCHING = re.compile(r"n/a (only )?if|if (un)?occupied|only if", re.IGNORECASE)
_PRODUCT_WORDS = re.compile(r"\b(product|epa reg)", re.IGNORECASE)
_EACH_WORDS = re.compile(r"\b(each|every)\b[^.]{0,30}\b(station|room|placement|location)", re.IGNORECASE)
_APPROVAL_WORDS = re.compile(r"\bapprov(al|ed)\b", re.IGNORECASE)
_SAFETY_WORDS = re.compile(
	r"\b(not near|out of reach|keep away|never|do not|ppe|gloves|children|pets|food)\b", re.IGNORECASE
)


class SchemaError(ValueError):
	"""A schema the phone cannot render. `findings` carries every error."""

	def __init__(self, findings: list):
		self.findings = findings
		super().__init__("; ".join(f"{row['path']}: {row['message']}" for row in findings))


# ── parsing ─────────────────────────────────────────────────────────────────
def as_fields(raw) -> list:
	"""A schema from JSON text, a list, or nothing. A dict (legacy `field_prompts`) is read as fields."""
	if raw in (None, "", [], {}):
		return []
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError as exc:
			raise SchemaError([_finding("", "not_json", f"the form is not valid JSON: {exc}")]) from exc
	if isinstance(raw, dict):
		# §1.7: the legacy `field_prompts` dict {key: {type, label, label_es}}.
		out = []
		for key, spec in raw.items():
			spec = spec if isinstance(spec, dict) else {}
			kind = {"boolean": "check", "bool": "check", "string": "text"}.get(
				str(spec.get("type") or "check"), str(spec.get("type") or "check")
			)
			out.append(
				{
					"key": str(key),
					"type": kind if kind in TYPES else "check",
					"label": {"en": str(spec.get("label") or key), "es": str(spec.get("label_es") or "")},
				}
			)
		return out
	if not isinstance(raw, list):
		raise SchemaError([_finding("", "not_a_list", "the form must be a JSON list of fields")])
	return [dict(field) if isinstance(field, dict) else field for field in raw]


def _finding(path: str, code: str, message: str) -> dict:
	return {"path": path or "(form)", "code": code, "message": message}


def text_of(value, language: str = "en") -> str:
	"""A `{en, es}` label in one language, English where the other is missing."""
	if isinstance(value, dict):
		return str(value.get(language) or value.get("en") or "")
	return str(value or "")


# ── validation ──────────────────────────────────────────────────────────────
def validate(raw, *, context_keys=CONTEXT_KEYS) -> dict:
	"""`{errors, warnings, fields}` for a schema. Never raises."""
	errors: list = []
	warnings: list = []
	try:
		fields = as_fields(raw)
	except SchemaError as exc:
		return {"errors": exc.findings, "warnings": [], "fields": []}
	_check_level(fields, "", errors, warnings, top=fields, context_keys=context_keys, depth=0)
	return {"errors": errors, "warnings": warnings, "fields": fields}


def require_valid(raw) -> list:
	"""The fields, or `SchemaError` naming every error. Warnings are not refused."""
	report = validate(raw)
	if report["errors"]:
		raise SchemaError(report["errors"])
	return report["fields"]


def _check_level(fields, prefix, errors, warnings, *, top, context_keys, depth, siblings=None):
	seen = set()
	for index, field in enumerate(fields):
		path = f"{prefix}[{index}]"
		if not isinstance(field, dict):
			errors.append(_finding(path, "not_an_object", "every field must be an object"))
			continue
		key = str(field.get("key") or "")
		path = f"{prefix}{key or f'[{index}]'}"
		if not _KEY.match(key):
			errors.append(_finding(path, "bad_key", f"key {key!r} must be lower_snake_case, 1–60 characters"))
		elif key in seen:
			errors.append(_finding(path, "duplicate_key", f"key {key!r} is used twice"))
		seen.add(key)
		unknown = set(field) - FIELD_KEYS
		if unknown:
			errors.append(
				_finding(path, "unknown_attribute", f"unknown attribute(s): {', '.join(sorted(unknown))}")
			)
		kind = str(field.get("type") or "")
		if kind not in TYPES:
			errors.append(_finding(path, "unknown_type", f"type {kind!r} is not one of {', '.join(TYPES)}"))
			continue
		label = field.get("label")
		if not (isinstance(label, dict) and str(label.get("en") or "").strip()) and kind != "info":
			errors.append(_finding(path, "no_label", "label.en is required"))
		for attr in ("label", "help", "statement"):
			value = field.get(attr)
			if isinstance(value, dict) and value.get("en") and not value.get("es"):
				warnings.append(_finding(path, "english_only", f"{attr} has no Spanish (es)"))
		if kind in ("select", "multi_select"):
			options = field.get("options")
			if not isinstance(options, list) or not options:
				errors.append(_finding(path, "no_options", f"a {kind} needs options"))
			else:
				for option in options:
					if not isinstance(option, dict) or option.get("value") in (None, ""):
						errors.append(_finding(path, "bad_option", "every option needs a value"))
						break
		if kind == "link":
			link = field.get("link")
			if not isinstance(link, dict) or link.get("doctype") not in LINK_DOCTYPES:
				errors.append(
					_finding(path, "bad_link", f"link.doctype must be one of {', '.join(LINK_DOCTYPES)}")
				)
			elif link.get("filters") is not None and not isinstance(link.get("filters"), dict):
				errors.append(
					_finding(path, "bad_link", "link.filters must be an object of equality filters")
				)
		if kind == "measurement":
			uom = field.get("uom")
			if uom is not None:
				if not isinstance(uom, dict) or not (uom.get("fixed") or uom.get("from_field")):
					errors.append(
						_finding(path, "bad_uom", "uom is {fixed: <UOM>} or {from_field: <link key>}")
					)
				elif uom.get("from_field"):
					target = _find(top, siblings or fields, str(uom["from_field"]))
					if (
						not target
						or target.get("type") != "link"
						or (target.get("link") or {}).get("doctype") != "Item"
					):
						errors.append(
							_finding(
								path,
								"bad_uom",
								f"uom.from_field {uom['from_field']!r} must name a link to Item",
							)
						)
		if kind == "group":
			children = field.get("fields")
			if not isinstance(children, list) or not children:
				errors.append(_finding(path, "empty_group", "a group needs fields"))
			elif depth >= 1:
				errors.append(_finding(path, "nested_group", "groups may not be nested"))
			else:
				_check_level(
					children,
					f"{path}.",
					errors,
					warnings,
					top=top,
					context_keys=context_keys,
					depth=depth + 1,
					siblings=children,
				)
		if kind == "approval" and not str(field.get("role") or "").strip():
			errors.append(_finding(path, "no_role", "an approval needs a role"))
		if kind == "attestation" and not isinstance(field.get("statement") or field.get("label"), dict):
			errors.append(_finding(path, "no_statement", "an attestation needs a statement"))
		for attr in ("min_count", "max_count", "min", "max", "step"):
			if attr in field and field[attr] is not None and not isinstance(field[attr], (int, float)):
				errors.append(_finding(path, "bad_number", f"{attr} must be a number"))
		for attr in ("show_if", "required_if"):
			if field.get(attr) is not None:
				problem = _condition_problem(field[attr], top, siblings or fields, context_keys)
				if problem:
					errors.append(_finding(path, "bad_condition", f"{attr}: {problem}"))
		english = text_of(label, "en") if isinstance(label, dict) else ""
		if english:
			if _PROSE_BRANCHING.search(english) and not field.get("show_if"):
				warnings.append(_finding(path, "prose_branching", "branching written as prose — use show_if"))
			if kind == "text" and _PRODUCT_WORDS.search(english):
				warnings.append(
					_finding(path, "free_text_product", "the product is free text — use a link to Item")
				)
			if kind == "photo" and _EACH_WORDS.search(english) and depth == 0:
				warnings.append(
					_finding(path, "one_photo_for_many", "one photo for many — use a group per station/room")
				)
			if kind == "text" and _APPROVAL_WORDS.search(english):
				warnings.append(
					_finding(path, "approval_as_text", "an approval typed as text — use an approval step")
				)
			if kind == "check" and _SAFETY_WORDS.search(english):
				warnings.append(
					_finding(
						path, "safety_without_attestation", "a safety statement — consider an attestation"
					)
				)


def _find(top: list, siblings: list, key: str) -> dict | None:
	for pool in (siblings, top):
		for field in pool or []:
			if isinstance(field, dict) and field.get("key") == key:
				return field
	return None


def _condition_problem(condition, top, siblings, context_keys) -> str:
	if not isinstance(condition, dict) or not condition:
		return "a condition must be an object"
	for group in ("all", "any"):
		if group in condition:
			parts = condition[group]
			if not isinstance(parts, list) or not parts:
				return f"{group} needs a list of conditions"
			for part in parts:
				problem = _condition_problem(part, top, siblings, context_keys)
				if problem:
					return problem
			return ""
	if "not" in condition:
		return _condition_problem(condition["not"], top, siblings, context_keys)
	if "field" in condition:
		if not _find(top, siblings, str(condition["field"])):
			return f"no field {condition['field']!r}"
	elif "context" in condition:
		if condition["context"] not in context_keys:
			return f"context must be one of {', '.join(context_keys)}"
	else:
		return "name a field or a context"
	ops = [op for op in OPERATORS if op in condition]
	if len(ops) != 1:
		return f"exactly one operator of {', '.join(OPERATORS)}"
	if ops[0] in ("in", "not_in") and not isinstance(condition[ops[0]], list):
		return f"{ops[0]} needs a list"
	return ""


# ── conditions ──────────────────────────────────────────────────────────────
def holds(condition, answers: dict, context: dict, local: dict | None = None) -> bool:
	"""Evaluate a condition. A missing answer is falsy and equals nothing."""
	if not condition:
		return True
	if "all" in condition:
		return all(holds(part, answers, context, local) for part in condition["all"])
	if "any" in condition:
		return any(holds(part, answers, context, local) for part in condition["any"])
	if "not" in condition:
		return not holds(condition["not"], answers, context, local)
	if "field" in condition:
		key = condition["field"]
		value = local[key] if local and key in local else answers.get(key)
	else:
		value = context.get(condition.get("context"))
	if "equals" in condition:
		return value == condition["equals"]
	if "not_equals" in condition:
		return value != condition["not_equals"]
	if "in" in condition:
		return value in condition["in"]
	if "not_in" in condition:
		return value not in condition["not_in"]
	if "truthy" in condition:
		return bool(value) is bool(condition["truthy"])
	if "falsy" in condition:
		return (not value) is bool(condition["falsy"])
	return True


# ── answers ─────────────────────────────────────────────────────────────────
def check_answers(
	fields: list, answers, context: dict, *, language: str = "en", approvals: dict | None = None
) -> dict:
	"""Validate a completion's answers. Returns `{answers, problems}`; answers are the stored ones.

	Hidden fields are dropped. `approvals` is `{key: {...}}` of steps already
	approved — an approval field is satisfied only by that, never by the phone.
	"""
	if isinstance(answers, str):
		try:
			answers = json.loads(answers) if answers.strip() else {}
		except ValueError:
			return {"answers": {}, "problems": ["form_answers is not valid JSON"]}
	answers = dict(answers or {})
	problems: list = []
	stored = _check_level_answers(fields, answers, context, language, approvals or {}, problems, None, "")
	return {"answers": stored, "problems": problems}


def _check_level_answers(fields, answers, context, language, approvals, problems, local, prefix):
	stored = {}
	source = local if local is not None else answers
	for field in fields:
		kind = field.get("type")
		key = field.get("key")
		name = text_of(field.get("label") or field.get("statement") or key, language) or key
		if kind == "info":
			continue
		if field.get("show_if") and not holds(field["show_if"], answers, context, local):
			continue
		required = bool(field.get("required")) or (
			bool(field.get("required_if")) and holds(field["required_if"], answers, context, local)
		)
		value = source.get(key)
		if kind == "approval":
			if required and not approvals.get(key):
				problems.append(f"{prefix}{name}: waiting for approval by {field.get('role')}")
			continue
		if _empty(value, kind):
			if required:
				problems.append(f"{prefix}{name}: required")
			continue
		problem, clean = _check_value(field, value, answers, local)
		if problem:
			problems.append(f"{prefix}{name}: {problem}")
			continue
		if kind == "group":
			rows = []
			for index, row in enumerate(clean):
				rows.append(
					_check_level_answers(
						field.get("fields") or [],
						answers,
						context,
						language,
						approvals,
						problems,
						row if isinstance(row, dict) else {},
						f"{prefix}{name} #{index + 1} — ",
					)
				)
			clean = rows
		if kind in ("check", "attestation") and required and clean is not True:
			problems.append(f"{prefix}{name}: must be confirmed")
			continue
		stored[key] = clean
	return stored


def _empty(value, kind) -> bool:
	if value is None or value == "" or value == []:
		return True
	if kind == "measurement" and isinstance(value, dict) and value.get("value") in (None, ""):
		return True
	return False


def _check_value(field, value, answers, local) -> tuple:
	kind = field.get("type")
	if kind in ("check", "attestation"):
		if isinstance(value, str):
			value = value.strip().lower() in ("1", "true", "yes")
		return "", bool(value)
	if kind in ("text", "long_text", "date", "datetime", "gps", "signature", "link"):
		if isinstance(value, (dict, list)):
			return "must be a single value", None
		return "", str(value).strip()
	if kind == "select":
		allowed = [str(option.get("value")) for option in field.get("options") or []]
		return (
			("", str(value))
			if str(value) in allowed
			else (f"{value!r} is not one of {', '.join(allowed)}", None)
		)
	if kind == "multi_select":
		allowed = [str(option.get("value")) for option in field.get("options") or []]
		values = [str(entry) for entry in (value if isinstance(value, list) else [value])]
		bad = [entry for entry in values if entry not in allowed]
		return (f"{', '.join(bad)} not allowed", None) if bad else ("", values)
	if kind in ("number", "measurement"):
		number = value.get("value") if isinstance(value, dict) else value
		try:
			number = float(number)
		except (TypeError, ValueError):
			return "must be a number", None
		if field.get("min") is not None and number < float(field["min"]):
			return f"must be at least {field['min']}", None
		if field.get("max") is not None and number > float(field["max"]):
			return f"must be at most {field['max']}", None
		if kind == "number":
			return "", number
		uom = str(value.get("uom") or "") if isinstance(value, dict) else ""
		return "", {"value": number, "uom": uom or resolved_uom(field, answers, local) or ""}
	if kind in ("photo", "group"):
		items = value if isinstance(value, list) else [value]
		low = int(field.get("min_count") if field.get("min_count") is not None else 1)
		high = int(field.get("max_count") or (10 if kind == "photo" else 50))
		if len(items) < low:
			return f"needs at least {low}", None
		if len(items) > high:
			return f"takes at most {high}", None
		return "", items
	return "", value


def resolved_uom(field: dict, answers: dict, local: dict | None = None) -> str:
	"""A measurement's unit: fixed, or the linked Item's stock_uom."""
	uom = field.get("uom") or {}
	if uom.get("fixed"):
		return str(uom["fixed"])
	source = uom.get("from_field")
	if not source:
		return ""
	item = (local or {}).get(source) or answers.get(source)
	if not item:
		return ""
	try:
		import frappe

		return str(frappe.db.get_value("Item", item, "stock_uom") or "")
	except Exception:
		return ""


def link_values(fields: list, answers: dict, doctype: str = "Item") -> list:
	"""Every answered link to `doctype`, groups included, in form order."""
	out = []
	for field in fields:
		value = answers.get(field.get("key"))
		if field.get("type") == "link" and (field.get("link") or {}).get("doctype") == doctype and value:
			out.append(str(value))
		if field.get("type") == "group" and isinstance(value, list):
			for row in value:
				out.extend(
					link_values(field.get("fields") or [], row if isinstance(row, dict) else {}, doctype)
				)
	return out


# ── the legacy checklist ────────────────────────────────────────────────────
def slug(text: str) -> str:
	value = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
	value = value if value and value[0].isalpha() else f"item_{value}"
	return value[:60].rstrip("_") or "item"


def legacy_fields(checklist_items: list) -> list:
	"""§1.5: the old checklist as fields, keyed by a slug of each name."""
	out = []
	seen = set()
	for item in checklist_items or []:
		name = str(item.get("item_name") or "").strip()
		if not name:
			continue
		key = slug(name)
		while key in seen:
			key = (key[:57] + "_2")[:60]
		seen.add(key)
		kind = LEGACY_TYPES.get(str(item.get("evidence_type") or "None"), "check")
		field = {
			"key": key,
			"type": kind,
			"label": {"en": name},
			"required": bool(item.get("required", True)),
		}
		if kind == "photo":
			field["min_count"] = 1
		out.append(field)
	return out


def ticks_from_answers(fields: list, answers: dict, checklist_items: list) -> list:
	"""The legacy `checklist` argument a completion's answers amount to (§3.2)."""
	by_key = {field["key"]: field for field in legacy_fields(checklist_items)}
	names = {
		slug(str(item.get("item_name") or "")): str(item.get("item_name") or "")
		for item in checklist_items or []
	}
	out = []
	for key, value in (answers or {}).items():
		if key not in names:
			continue
		field = by_key.get(key) or {}
		done = bool(value) if field.get("type") == "check" else value not in (None, "", [], {})
		note = "" if isinstance(value, (bool, list, dict)) else str(value)
		if isinstance(value, dict) and "value" in value:
			note = f"{value.get('value')} {value.get('uom') or ''}".strip()
		out.append({"item_name": names[key], "done": done, **({"note": note} if note else {})})
	return out


# ── what the phone is sent ──────────────────────────────────────────────────
def for_phone(fields: list, answers: dict | None = None) -> list:
	"""The fields as sent to the phone, with `uom.from_field` resolved where the Item is known."""
	out = []
	for field in fields:
		field = json.loads(json.dumps(field))
		if field.get("type") == "measurement" and (field.get("uom") or {}).get("from_field"):
			unit = resolved_uom(field, answers or {})
			if unit:
				field["uom"]["resolved"] = unit
		if field.get("type") == "group":
			field["fields"] = for_phone(field.get("fields") or [], {})
		out.append(field)
	return out


def resolve_language(fields: list, language: str) -> list:
	"""The fields with labels flattened to one language, for a preview a person reads."""
	out = []
	for field in fields:
		field = json.loads(json.dumps(field))
		for attr in ("label", "help", "statement"):
			if attr in field:
				field[attr] = text_of(field[attr], language)
		for option in field.get("options") or []:
			option["label"] = text_of(option.get("label") or option.get("value"), language)
		if field.get("type") == "group":
			field["fields"] = resolve_language(field.get("fields") or [], language)
		out.append(field)
	return out


def visible(fields: list, answers: dict, context: dict) -> list:
	"""The top-level fields a phone would show now, for the preview."""
	return [
		field for field in fields if not field.get("show_if") or holds(field["show_if"], answers, context)
	]


# ── wizards share the vocabulary (§1.6) ─────────────────────────────────────
_WIZARD_TYPES = {
	"text": ("text", None),
	"long_text": ("long_text", None),
	"number": ("number", None),
	"date": ("date", None),
	"datetime": ("datetime", None),
	"select": ("select", None),
	"multi_select": ("multi_select", None),
	"checkbox": ("check", None),
	"photo": ("photo", None),
	"signature": ("signature", None),
	"qr_scan": ("text", None),
	"audio_note": ("long_text", None),
	"employee_select": ("link", "Employee"),
	"asset_select": ("link", "Asset Register"),
}


def _wizard_condition(raw):
	"""`visible_if` → `show_if`. Malformed means always visible (None)."""
	if isinstance(raw, str):
		try:
			raw = json.loads(raw) if raw.strip() else None
		except ValueError:
			return None
	if not isinstance(raw, dict) or not raw.get("field"):
		return None
	out = {"field": slug(raw["field"]) if not _KEY.match(str(raw["field"])) else raw["field"]}
	for op in OPERATORS:
		if op in raw:
			out[op] = raw[op]
			return out
	return None


def from_wizard_fields(english: list, spanish: list | None = None) -> list:
	"""One wizard step's fields, in the form vocabulary. Unknown types become `text`."""
	es = {str(field.get("fieldname") or ""): field for field in spanish or []}
	out = []
	for field in english or []:
		name = str(field.get("fieldname") or "")
		key = name if _KEY.match(name) else slug(name)
		kind, doctype = _WIZARD_TYPES.get(str(field.get("type") or "text"), ("text", None))
		other = es.get(name) or {}
		row = {
			"key": key,
			"type": kind,
			"label": {"en": str(field.get("label") or name), "es": str(other.get("label") or "")},
			"required": bool(field.get("required")),
		}
		if field.get("help"):
			row["help"] = {"en": str(field["help"]), "es": str(other.get("help") or "")}
		if kind in ("select", "multi_select"):
			es_options = {
				str(option.get("value")): option
				for option in other.get("options") or []
				if isinstance(option, dict)
			}
			row["options"] = [
				{
					"value": str(option.get("value") if isinstance(option, dict) else option),
					"label": {
						"en": str((option.get("label") if isinstance(option, dict) else option) or ""),
						"es": str(
							(
								es_options.get(
									str(option.get("value") if isinstance(option, dict) else option)
								)
								or {}
							).get("label")
							or ""
						),
					},
				}
				for option in field.get("options") or []
			]
		if doctype:
			row["link"] = {"doctype": doctype}
		validation = field.get("validation") or {}
		if kind == "number":
			for attr in ("min", "max"):
				if validation.get(attr) is not None:
					row[attr] = validation[attr]
		condition = _wizard_condition(field.get("visible_if"))
		if condition:
			row["show_if"] = condition
		out.append(row)
	return out
