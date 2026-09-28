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

#: Schema version 1 (v0.204.0): the seventeen kinds every app since v0.204.0 renders.
V1_KINDS = (
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

#: Schema version 2 (v0.205.0, docs/design/programs_and_field_kinds.md B1):
#: Frappe's field types 1:1, and the farm kinds.
V2_KINDS = (
	"data",
	"small_text",
	"text_editor",
	"dynamic_link",
	"time",
	"duration",
	"int",
	"float",
	"currency",
	"percent",
	"rating",
	"table",
	"attach",
	"attach_image",
	"geolocation",
	"barcode",
	"color",
	"html",
	"read_only",
	"scan",
	"map_area",
	"timer",
	"audio_note",
	"document",
	"computed",
)
TYPES = (*V1_KINDS, *V2_KINDS)
#: Refused outright: a form never collects a secret.
REFUSED_KINDS = ("password",)
#: Kinds with no answer — shown, never sent.
DISPLAY_KINDS = ("info", "html", "read_only", "document")
#: What each schema version renders.
SCHEMA_KINDS = {1: frozenset(V1_KINDS), 2: frozenset(TYPES)}
CURRENT_SCHEMA = 2

#: Frappe fieldtype spellings → the kind (case-insensitive, spaces or underscores).
ALIASES = {
	"data": "data",
	"small text": "small_text",
	"long text": "long_text",
	"text": "long_text",
	"text editor": "text_editor",
	"select": "select",
	"link": "link",
	"dynamic link": "dynamic_link",
	"date": "date",
	"datetime": "datetime",
	"time": "time",
	"duration": "duration",
	"check": "check",
	"int": "int",
	"float": "float",
	"currency": "currency",
	"percent": "percent",
	"rating": "rating",
	"table": "table",
	"attach": "attach",
	"attach image": "attach_image",
	"signature": "signature",
	"geolocation": "geolocation",
	"barcode": "barcode",
	"color": "color",
	"password": "password",
	"html": "html",
	"read only": "read_only",
}
SCAN_KINDS = ("asset_tag", "upc", "badge", "qr", "any")
DOCUMENT_SOURCES = ("item_label", "sop", "file", "url")
FALLBACKS = ("text", "photo")


def canonical(kind) -> str:
	"""A field type in the kind vocabulary: v1/v2 names as they are, Frappe spellings mapped."""
	text = str(kind or "").strip()
	if text in TYPES or text in REFUSED_KINDS:
		return text
	key = text.lower().replace("_", " ")
	return ALIASES.get(key, text)


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
	# v0.205.0 (B1).
	"safety_critical",
	"fallback",
	"currency",
	"scan",
	"min_points",
	"purpose",
	"max_seconds",
	"document",
	"formula",
	"precision",
	"source",
}
#: v0.204.0's fixed list; v0.205.0 reads `phone_link_doctypes` (B3) and this is its floor.
LINK_DOCTYPES = ("Item", "Asset Register", "Housing Unit", "Employee")
DEFAULT_PHONE_LINK_DOCTYPES = (
	"Item",
	"Asset Register",
	"Housing Unit",
	"Employee",
	"Field",
	"Parcel",
	"Irrigation Zone",
	"Warehouse",
	"Supplier",
	"Customer",
	"Crop",
	"UOM",
	"Farm Task",
	"Certification",
)


def phone_link_doctypes() -> tuple:
	"""The doctypes a phone form may link to and search (ERPNext MCP Settings). Never raises."""
	try:
		from . import settings

		raw = settings._value("phone_link_doctypes")
	except Exception:
		raw = None
	if not str(raw or "").strip():
		return DEFAULT_PHONE_LINK_DOCTYPES
	return tuple(line.strip() for line in str(raw).replace(",", "\n").splitlines() if line.strip())


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
	return [_normalised(field) for field in raw]


def _normalised(field):
	"""A field with its Frappe-spelled type mapped to the kind, children too."""
	if not isinstance(field, dict):
		return field
	field = dict(field)
	if "type" in field:
		field["type"] = canonical(field["type"])
	if isinstance(field.get("fields"), list):
		field["fields"] = [_normalised(child) for child in field["fields"]]
	return field


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
		kind = canonical(field.get("type"))
		if kind in REFUSED_KINDS:
			errors.append(_finding(path, "refused_type", "a form never collects a password or other secret"))
			continue
		if kind not in TYPES:
			errors.append(_finding(path, "unknown_type", f"type {kind!r} is not one of {', '.join(TYPES)}"))
			continue
		_check_v2(field, kind, path, errors, top, siblings or fields)
		label = field.get("label")
		if not (isinstance(label, dict) and str(label.get("en") or "").strip()) and kind not in (
			"info",
			"html",
		):
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
			allowed = phone_link_doctypes()
			if not isinstance(link, dict) or link.get("doctype") not in allowed:
				errors.append(
					_finding(
						path,
						"bad_link",
						f"link.doctype must be a phone-searchable doctype ({', '.join(allowed)}) — add it to "
						"ERPNext MCP Settings › phone_link_doctypes to allow it",
					)
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
		if kind in ("group", "table"):
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


def _check_v2(field: dict, kind: str, path: str, errors: list, top: list, siblings: list) -> None:
	"""The v0.205.0 kinds' own rules (B1, B5)."""
	fallback = field.get("fallback")
	if fallback is not None and fallback not in FALLBACKS:
		errors.append(_finding(path, "bad_fallback", f"fallback must be one of {', '.join(FALLBACKS)}"))
	if kind == "dynamic_link":
		link = field.get("link") or {}
		source = str(link.get("doctype_from") or "")
		if not source or not _find(top, siblings, source):
			errors.append(
				_finding(path, "bad_link", "dynamic_link needs link.doctype_from naming another field")
			)
	if kind == "rating" and field.get("max") is not None and not isinstance(field["max"], int):
		errors.append(_finding(path, "bad_number", "rating max must be a whole number"))
	if kind == "scan":
		kinds = (field.get("scan") or {}).get("kinds") or ["any"]
		bad = [entry for entry in kinds if entry not in SCAN_KINDS]
		if bad:
			errors.append(_finding(path, "bad_scan", f"scan.kinds must be among {', '.join(SCAN_KINDS)}"))
	if kind == "document":
		spec = field.get("document") or {}
		source = spec.get("source")
		if source not in DOCUMENT_SOURCES:
			errors.append(
				_finding(
					path, "bad_document", f"document.source must be one of {', '.join(DOCUMENT_SOURCES)}"
				)
			)
		elif source == "item_label":
			target = _find(top, siblings, str(spec.get("from_field") or ""))
			if not target or (target.get("link") or {}).get("doctype") != "Item":
				errors.append(_finding(path, "bad_document", "document.from_field must name a link to Item"))
		elif source == "file" and not spec.get("file"):
			errors.append(_finding(path, "bad_document", "document.file is required for source file"))
		elif source == "url" and not str(spec.get("url") or "").startswith(("https://", "http://")):
			errors.append(_finding(path, "bad_document", "document.url must be an http(s) URL"))
	if kind == "computed":
		try:
			names = formula_names(str(field.get("formula") or ""))
		except ValueError as exc:
			errors.append(_finding(path, "bad_formula", str(exc)))
		else:
			for name in names:
				head = name.split(".", 1)[0]
				if not _find(top, siblings, head):
					errors.append(_finding(path, "bad_formula", f"the formula names no field {head!r}"))
	if kind == "read_only":
		source = str(field.get("source") or "")
		if source.startswith("answer:"):
			if not _find(top, siblings, source[7:]):
				errors.append(_finding(path, "bad_source", f"no field {source[7:]!r}"))
		elif source.startswith("context:"):
			if source[8:] not in CONTEXT_KEYS:
				errors.append(
					_finding(path, "bad_source", f"context must be one of {', '.join(CONTEXT_KEYS)}")
				)
		elif source:
			errors.append(_finding(path, "bad_source", "source is answer:<key> or context:<key>"))


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
	# v0.205.0. Computed fields are the SERVER's answer, recomputed from what was
	# stored — the phone's live value is a preview.
	_compute(fields, stored, problems, language)
	return {"answers": stored, "problems": problems}


def _compute(fields: list, stored: dict, problems: list, language: str) -> None:
	for field in fields:
		if field.get("type") != "computed":
			continue
		try:
			value = evaluate_formula(str(field.get("formula") or ""), stored)
		except (ValueError, ZeroDivisionError) as exc:
			problems.append(
				f"{text_of(field.get('label'), language) or field['key']}: cannot compute ({exc})"
			)
			continue
		if value is not None and field.get("precision") is not None:
			value = round(value, int(field["precision"]))
		stored[field["key"]] = value


def _check_level_answers(fields, answers, context, language, approvals, problems, local, prefix):
	stored = {}
	source = local if local is not None else answers
	for field in fields:
		kind = field.get("type")
		key = field.get("key")
		name = text_of(field.get("label") or field.get("statement") or key, language) or key
		if kind in DISPLAY_KINDS or kind == "computed":
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
		# v0.205.0 (B4). An app that cannot render a kind sends its fallback;
		# accepted for anything that is not safety-critical, and stored as given.
		if isinstance(value, dict) and value.get("fallback") in FALLBACKS and kind not in ("text", "photo"):
			if field.get("safety_critical"):
				problems.append(
					f"{prefix}{name}: this safety-critical field needs the app updated to answer it"
				)
				continue
			stored[key] = value
			continue
		problem, clean = _check_value(field, value, answers, local)
		if problem:
			problems.append(f"{prefix}{name}: {problem}")
			continue
		if kind in ("group", "table"):
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
	if kind in ("text", "long_text", "date", "datetime", "gps", "signature", "link", "data", "small_text"):
		if isinstance(value, (dict, list)):
			return "must be a single value", None
		return "", str(value).strip()
	v2 = _check_v2_value(field, kind, value)
	if v2 is not None:
		return v2
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
	if kind in ("photo", "group", "table", "attach", "attach_image"):
		items = value if isinstance(value, list) else [value]
		low = int(field.get("min_count") if field.get("min_count") is not None else 1)
		high = int(field.get("max_count") or (50 if kind in ("group", "table") else 10))
		if len(items) < low:
			return f"needs at least {low}", None
		if len(items) > high:
			return f"takes at most {high}", None
		return "", items
	return "", value


_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SCRIPT = re.compile(
	r"<\s*(script|style|iframe|object|embed)[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_EVENT_ATTR = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)


def sanitise_html(text: str) -> str:
	"""Rich text from a phone: no scripts, frames or inline handlers."""
	return _EVENT_ATTR.sub("", _SCRIPT.sub("", str(text or "")))


def _number(value):
	try:
		return float(value.get("value") if isinstance(value, dict) else value)
	except (TypeError, ValueError):
		return None


def _check_v2_value(field: dict, kind: str, value):
	"""`(problem, clean)` for a v0.205.0 kind, or None when it is not one."""
	if kind == "text_editor":
		return "", sanitise_html(value if isinstance(value, str) else json.dumps(value))
	if kind == "time":
		text = str(value).strip()
		return ("", text) if _TIME.match(text) else ("must be HH:MM or HH:MM:SS", None)
	if kind in ("duration", "int", "rating"):
		number = _number(value)
		if number is None or number != int(number):
			return "must be a whole number", None
		number = int(number)
		low = 1 if kind == "rating" else field.get("min", 0 if kind == "duration" else None)
		high = int(field.get("max") or 5) if kind == "rating" else field.get("max")
		if low is not None and number < low:
			return f"must be at least {low}", None
		if high is not None and number > high:
			return f"must be at most {high}", None
		return "", number
	if kind in ("float", "currency", "percent"):
		number = _number(value)
		if number is None:
			return "must be a number", None
		low = field.get("min", 0 if kind == "percent" else None)
		high = field.get("max", 100 if kind == "percent" else None)
		if low is not None and number < float(low):
			return f"must be at least {low}", None
		if high is not None and number > float(high):
			return f"must be at most {high}", None
		return "", number
	if kind == "dynamic_link":
		if not isinstance(value, dict) or not value.get("doctype") or not value.get("name"):
			return "must be {doctype, name}", None
		if value["doctype"] not in phone_link_doctypes():
			return f"{value['doctype']} is not a phone-searchable doctype", None
		return "", {"doctype": str(value["doctype"]), "name": str(value["name"])}
	if kind == "geolocation":
		if isinstance(value, str) and "," in value:
			lat, lon = (part.strip() for part in value.split(",", 1))
			value = {"type": "Point", "coordinates": [float(lon), float(lat)]}
		if (
			not isinstance(value, dict)
			or value.get("type") != "Point"
			or len(value.get("coordinates") or []) != 2
		):
			return "must be a GeoJSON Point", None
		return "", value
	if kind == "map_area":
		ring = (value.get("coordinates") or [[]])[0] if isinstance(value, dict) else []
		if not isinstance(value, dict) or value.get("type") != "Polygon":
			return "must be a GeoJSON Polygon", None
		points = len(ring) - (1 if ring and ring[0] == ring[-1] else 0)
		if points < int(field.get("min_points") or 3):
			return f"needs at least {int(field.get('min_points') or 3)} points", None
		return "", value
	if kind in ("barcode", "scan"):
		if isinstance(value, str):
			value = {"code": value}
		if not isinstance(value, dict) or not str(value.get("code") or "").strip():
			return "must carry a code", None
		clean = {"code": str(value["code"]).strip()}
		for attr in ("symbology", "kind", "doctype", "name"):
			if value.get(attr):
				clean[attr] = str(value[attr])
		return "", clean
	if kind == "color":
		text = str(value).strip()
		return ("", text.upper()) if _COLOR.match(text) else ("must be #RRGGBB", None)
	if kind == "timer":
		if not isinstance(value, dict):
			return "must be {started_at, stopped_at, seconds}", None
		seconds = _number(value.get("seconds"))
		if seconds is None or seconds < 0:
			return "seconds must be zero or more", None
		return "", {
			"started_at": value.get("started_at"),
			"stopped_at": value.get("stopped_at"),
			"seconds": int(seconds),
		}
	if kind == "audio_note":
		if isinstance(value, list):
			value = value[0] if value else ""
		return ("", str(value)) if str(value).strip() else ("must be a recording", None)
	return None


# ── computed fields: a whitelist arithmetic reader (B1, B5) ─────────────────
_FUNCTIONS = ("min", "max", "round", "sum")


def formula_names(formula: str) -> list:
	"""The answer keys (and table.column pairs) a formula reads. Raises ValueError."""
	import ast

	if not formula.strip():
		raise ValueError("a computed field needs a formula")
	try:
		tree = ast.parse(formula, mode="eval")
	except SyntaxError as exc:
		raise ValueError(f"the formula is not arithmetic: {exc.msg}") from None
	names: list = []

	def walk(node):
		if isinstance(node, ast.Expression):
			return walk(node.body)
		if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
			walk(node.left)
			return walk(node.right)
		if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
			return walk(node.operand)
		if (
			isinstance(node, ast.Constant)
			and isinstance(node.value, (int, float))
			and not isinstance(node.value, bool)
		):
			return None
		if isinstance(node, ast.Name):
			names.append(node.id)
			return None
		if (
			isinstance(node, ast.Call)
			and isinstance(node.func, ast.Name)
			and node.func.id in _FUNCTIONS
			and not node.keywords
		):
			if node.func.id == "sum":
				if (
					len(node.args) != 1
					or not isinstance(node.args[0], ast.Attribute)
					or not isinstance(node.args[0].value, ast.Name)
				):
					raise ValueError("sum takes one table.column")
				names.append(f"{node.args[0].value.id}.{node.args[0].attr}")
				return None
			for arg in node.args:
				walk(arg)
			return None
		raise ValueError(
			f"the formula may use numbers, answers, + - * / ( ) and {', '.join(_FUNCTIONS)} only"
		)

	walk(tree)
	return names


def evaluate_formula(formula: str, answers: dict):
	"""The formula's value over the answers; None when an answer it needs is missing."""
	import ast

	formula_names(formula)
	tree = ast.parse(formula, mode="eval")

	def value(node):
		if isinstance(node, ast.Expression):
			return value(node.body)
		if isinstance(node, ast.Constant):
			return float(node.value)
		if isinstance(node, ast.Name):
			return _number(answers.get(node.id))
		if isinstance(node, ast.UnaryOp):
			inner = value(node.operand)
			return None if inner is None else (-inner if isinstance(node.op, ast.USub) else inner)
		if isinstance(node, ast.BinOp):
			left, right = value(node.left), value(node.right)
			if left is None or right is None:
				return None
			if isinstance(node.op, ast.Add):
				return left + right
			if isinstance(node.op, ast.Sub):
				return left - right
			if isinstance(node.op, ast.Mult):
				return left * right
			return left / right
		if isinstance(node, ast.Call):
			if node.func.id == "sum":
				table, column = node.args[0].value.id, node.args[0].attr
				rows = answers.get(table) or []
				return sum(_number(row.get(column)) or 0 for row in rows if isinstance(row, dict))
			args = [value(arg) for arg in node.args]
			if any(arg is None for arg in args):
				return None
			if node.func.id == "round":
				return round(args[0], int(args[1]) if len(args) > 1 else 0)
			return (min if node.func.id == "min" else max)(args)
		raise ValueError("unsupported")

	return value(tree)


# ── capabilities (B4) ───────────────────────────────────────────────────────
def kinds_used(fields: list, *, safety_critical_only: bool = False) -> set:
	"""Every kind a form uses (groups and tables included)."""
	out = set()
	for field in fields or []:
		if not isinstance(field, dict):
			continue
		if not safety_critical_only or field.get("safety_critical"):
			out.add(canonical(field.get("type")))
		out |= kinds_used(field.get("fields") or [], safety_critical_only=safety_critical_only)
	return out


def kinds_of(capabilities) -> frozenset:
	"""The kinds a client renders: its reported list, else its schema version's."""
	if isinstance(capabilities, str):
		try:
			capabilities = json.loads(capabilities)
		except ValueError:
			capabilities = {}
	capabilities = capabilities or {}
	reported = capabilities.get("field_kinds")
	if isinstance(reported, list) and reported:
		return frozenset(canonical(kind) for kind in reported)
	version = int(capabilities.get("schema_version") or 1)
	return SCHEMA_KINDS.get(version, SCHEMA_KINDS[1])


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
	"qr_scan": ("scan", None),
	"audio_note": ("audio_note", None),
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
