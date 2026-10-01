# SPDX-License-Identifier: MIT
"""Server-driven extraction config. v0.206.0.

docs/design/config_flags_triage.md §1. What the phone's on-device reader is told
(FoundationModels instructions and schema), where it looks (sections), what it
reads deterministically (extractors), what it checks (rules) and which of the
on-device model's findings are dropped (advisory_drop) — as DATA, versioned in
`Extraction Config`, so a field report is fixed by an MCP call and not by an
App Store release. The phone downloads configuration, never code.

THE SAME SEMANTICS ON BOTH SIDES. The phone runs sections, extractors and rules
on every scan; this module runs them identically in `preview` and applies the
same `advisory_drop` list in `document_intel.merge_llm_assessment`. That is why
patterns are held to a portable subset (`regex_problems`): a pattern that means
one thing to Python `re` and another to ICU is a config that reads two ways.

WHERE A CONFIG COMES FROM, in order: the Published row for the type; the
built-in file in `erpnext_mcp/extraction/` (the same file the app bundles);
nothing. Built-ins are `"<type>@builtin-1"`.
"""

from __future__ import annotations

import json
import pathlib
import re

import frappe

from . import compat

DOCTYPE = "Extraction Config"
SCHEMA_VERSION = 1
BUILTIN_REVISION = 1
#: A built-in whose bundled file has changed since v0.206.0. Receipt: v0.211.0 (the
#: `receipt` block — quick_wins_2026_10.md §4).
BUILTIN_REVISIONS = {"Receipt": 2}
BUILTIN_DIR = pathlib.Path(__file__).parent / "extraction"
BUILTINS = {
	"Pesticide Label": "pesticide_label",
	"Receipt": "receipt",
	"I-9 Document": "i9_document",
}
DRAFT, PUBLISHED, SUPERSEDED = "Draft", "Published", "Superseded"
#: v0.211.0. Served to the accounts in `staged_users` only; everyone else keeps Published.
STAGED = "Staged"
FIELD_TYPES = ("string", "number", "integer", "boolean", "array")
RULE_KINDS = ("pattern", "required", "sum_max", "sum_min", "range", "one_of", "not_applicable")
SEVERITIES = ("error", "warning", "info")
DEFAULT_MESSAGE_LIMIT = 240
TOP_KEYS = (
	"schema_version",
	"document_type",
	"instructions",
	"fields",
	"sections",
	"extractors",
	"rules",
	"advisory_drop",
	"llm",
	# v0.211.0. How a receipt's amount, merchant and line items are chosen on
	# the phone. Optional; an app older than 0.23.0 ignores it.
	"receipt",
)
RECEIPT_LISTS = ("total_labels", "subtotal_labels", "never_amount", "not_items")
RECEIPT_KEYS = (*RECEIPT_LISTS, "charge_patterns", "merchant_domains")
RECEIPT_LIST_CAP = 60
RECEIPT_PHRASE_CAP = 60
RECEIPT_DOMAIN_CAP = 200


def version_string(document_type, version) -> str:
	return f"{str(document_type or '').strip()}@{version}"


# ── the portable regex subset ───────────────────────────────────────────────

_REFUSED = (
	(re.compile(r"\(\?<[=!]"), "look-behind"),
	(re.compile(r"\(\?P?<[A-Za-z_]"), "a named group"),
	(re.compile(r"\(\?P[=>]"), "a named reference"),
	(re.compile(r"\\k<"), "a named backreference"),
	(re.compile(r"(?<!\\)(?:\\\\)*\\[1-9]"), "a backreference"),
	(re.compile(r"\(\?[aiLmsux-]+[):]"), "an inline flag other than a leading (?i)"),
)


def regex_problems(pattern) -> list:
	"""Why `pattern` is not in the portable subset, or []. §1.2."""
	if not isinstance(pattern, str) or not pattern:
		return ["a pattern must be a non-empty string"]
	body = pattern[4:] if pattern.startswith("(?i)") else pattern
	problems = [f"{pattern!r} uses {what}" for check, what in _REFUSED if check.search(body)]
	if not problems:
		try:
			re.compile(body)
		except re.error as exc:
			problems.append(f"{pattern!r} does not compile: {exc}")
	return problems


def compile_pattern(pattern: str) -> re.Pattern:
	"""A portable pattern as Python reads it: a leading (?i) is IGNORECASE."""
	if pattern.startswith("(?i)"):
		return re.compile(pattern[4:], re.IGNORECASE)
	return re.compile(pattern)


# ── checking a body ─────────────────────────────────────────────────────────


def _patterns_of(config: dict):
	for name, section in (config.get("sections") or {}).items():
		if isinstance(section, dict):
			for key in ("exclude_patterns", "start_patterns"):
				for pattern in section.get(key) or ():
					yield f"sections.{name}.{key}", pattern
	for index, row in enumerate(config.get("extractors") or ()):
		if isinstance(row, dict):
			yield f"extractors[{index}].pattern", row.get("pattern")
	for index, row in enumerate(config.get("rules") or ()):
		if isinstance(row, dict) and row.get("kind") == "pattern":
			yield f"rules[{index}].pattern", row.get("pattern")
	for index, row in enumerate(config.get("advisory_drop") or ()):
		if isinstance(row, dict) and row.get("message_pattern") is not None:
			yield f"advisory_drop[{index}].message_pattern", row.get("message_pattern")
	receipt = config.get("receipt")
	if isinstance(receipt, dict) and isinstance(receipt.get("charge_patterns"), list):
		for index, pattern in enumerate(receipt["charge_patterns"]):
			yield f"receipt.charge_patterns[{index}]", pattern


def receipt_problems(block) -> list:
	"""What is wrong with a `receipt` block, or []. quick_wins_2026_10.md §4.2."""
	if block is None:
		return []
	if not isinstance(block, dict):
		return ["receipt must be an object"]
	out = []
	unknown = sorted(set(block) - set(RECEIPT_KEYS))
	if unknown:
		out.append(f"receipt has unknown keys: {', '.join(unknown)}")
	for key in (*RECEIPT_LISTS, "charge_patterns"):
		value = block.get(key, [])
		if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
			out.append(f"receipt.{key} must be a list of non-empty strings")
			continue
		if len(value) > RECEIPT_LIST_CAP:
			out.append(f"receipt.{key} has more than {RECEIPT_LIST_CAP} entries")
		if key != "charge_patterns" and any(len(v) > RECEIPT_PHRASE_CAP for v in value):
			out.append(f"receipt.{key} has a phrase over {RECEIPT_PHRASE_CAP} characters")
	for index, pattern in enumerate(block.get("charge_patterns") or ()):
		if isinstance(pattern, str):
			try:
				groups = compile_pattern(pattern).groups
			except re.error:
				continue  # reported by regex_problems
			if groups != 1:
				out.append(f"receipt.charge_patterns[{index}] needs exactly one capture group (the amount)")
	domains = block.get("merchant_domains", {})
	if not isinstance(domains, dict) or not all(
		isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip() for k, v in domains.items()
	):
		out.append("receipt.merchant_domains must map a domain to a trading name")
	else:
		if len(domains) > RECEIPT_DOMAIN_CAP:
			out.append(f"receipt.merchant_domains has more than {RECEIPT_DOMAIN_CAP} entries")
		bad = sorted(k for k in domains if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", k))
		if bad:
			out.append(
				"receipt.merchant_domains keys are bare lower-case domains (homedepot.com): "
				+ ", ".join(bad[:5])
			)
	return out


def problems(config) -> list:
	"""Everything that would make the phone refuse this body, or []. §1.4."""
	if not isinstance(config, dict):
		return ["the config must be a JSON object"]
	out = []
	if config.get("schema_version") != SCHEMA_VERSION:
		out.append(f"schema_version must be {SCHEMA_VERSION}")
	unknown = sorted(set(config) - set(TOP_KEYS))
	if unknown:
		out.append(f"unknown top-level keys: {', '.join(unknown)}")
	instructions = config.get("instructions")
	if not isinstance(instructions, dict) or not str(instructions.get("en") or "").strip():
		out.append("instructions.en is required")

	names = []
	for index, field in enumerate(config.get("fields") or ()):
		if not isinstance(field, dict) or not str(field.get("name") or "").strip():
			out.append(f"fields[{index}] needs a name")
			continue
		names.append(field["name"])
		if field.get("type") not in FIELD_TYPES:
			out.append(
				f"field {field['name']!r} has type {field.get('type')!r}; one of {', '.join(FIELD_TYPES)}"
			)
		guide = field.get("guide")
		if guide is not None and not (isinstance(guide, dict) and isinstance(guide.get("any_of"), list)):
			out.append(f"field {field['name']!r}: guide must be {{any_of: [...]}} or null")
	duplicates = sorted({name for name in names if names.count(name) > 1})
	if duplicates:
		out.append(f"field names are not unique: {', '.join(duplicates)}")

	sections = config.get("sections") or {}
	if not isinstance(sections, dict):
		out.append("sections must be an object")
		sections = {}
	for name, section in sections.items():
		if not isinstance(section, dict) or not (
			section.get("start_headings") or section.get("start_patterns")
		):
			out.append(f"section {name!r} needs start_headings or start_patterns")
	referenced = [
		(f"field {f.get('name')!r}", f.get("section"))
		for f in config.get("fields") or ()
		if isinstance(f, dict)
	]
	referenced += [
		(f"extractor for {row.get('field')!r}", row.get("section"))
		for row in config.get("extractors") or ()
		if isinstance(row, dict)
	]
	for who, section in referenced:
		if section and section not in sections:
			out.append(f"{who} names section {section!r}, which is not defined")

	for index, row in enumerate(config.get("extractors") or ()):
		if not isinstance(row, dict) or not row.get("field"):
			out.append(f"extractors[{index}] needs a field")
		elif not isinstance(row.get("group", 0), int):
			out.append(f"extractors[{index}].group must be an integer")

	for index, rule in enumerate(config.get("rules") or ()):
		if not isinstance(rule, dict):
			out.append(f"rules[{index}] must be an object")
			continue
		kind = rule.get("kind")
		if kind not in RULE_KINDS:
			out.append(f"rules[{index}] kind {kind!r} is not one of {', '.join(RULE_KINDS)}")
			continue
		if kind == "not_applicable":
			if not rule.get("fields"):
				out.append(f"rules[{index}] not_applicable needs fields")
		else:
			if not rule.get("code") or not rule.get("field"):
				out.append(f"rules[{index}] needs a code and a field")
			if rule.get("severity", "warning") not in SEVERITIES:
				out.append(f"rules[{index}] severity must be one of {', '.join(SEVERITIES)}")
		if kind == "sum_max" and "max" not in rule:
			out.append(f"rules[{index}] sum_max needs max")
		if kind == "sum_min" and "min" not in rule:
			out.append(f"rules[{index}] sum_min needs min")
		if kind == "range" and "min" not in rule and "max" not in rule:
			out.append(f"rules[{index}] range needs min or max")
		if kind == "one_of" and not isinstance(rule.get("values"), list):
			out.append(f"rules[{index}] one_of needs values")

	codes = {rule.get("code") for rule in config.get("rules") or () if isinstance(rule, dict)}
	for index, row in enumerate(config.get("advisory_drop") or ()):
		if not isinstance(row, dict):
			out.append(f"advisory_drop[{index}] must be an object")
			continue
		if not (row.get("field") or row.get("fields") or row.get("message_pattern")):
			out.append(f"advisory_drop[{index}] needs a field, fields or message_pattern")
		if row.get("unless_rule_failed") and row["unless_rule_failed"] not in codes:
			out.append(
				f"advisory_drop[{index}] names rule {row['unless_rule_failed']!r}, which is not defined"
			)

	out.extend(receipt_problems(config.get("receipt")))
	for where, pattern in _patterns_of(config):
		out.extend(f"{where}: {problem}" for problem in regex_problems(pattern))
	return out


# ── running a body: sections, extractors, rules ─────────────────────────────


def _lines(ocr_text) -> list:
	return [line.strip() for line in str(ocr_text or "").splitlines() if line.strip()]


def _starts_with_heading(line: str, headings) -> bool:
	upper = line.upper()
	return any(upper.startswith(str(heading).upper()) for heading in headings or ())


def carve(ocr_text, sections) -> dict:
	"""{section name: text}. Heading form runs from the first line starting with
	a start heading to the next line starting with an end heading, dropping lines
	that match an exclude pattern; pattern form is the first matching line and
	the `lines`−1 after it. A section that is not found is ""."""
	lines = _lines(ocr_text)
	out = {}
	for name, section in (sections or {}).items():
		excluded = [compile_pattern(p) for p in section.get("exclude_patterns") or ()]
		picked: list = []
		if section.get("start_headings"):
			inside = False
			for line in lines:
				if not inside:
					inside = _starts_with_heading(line, section["start_headings"])
					if not inside:
						continue
				elif _starts_with_heading(line, section.get("end_headings")):
					break
				picked.append(line)
		else:
			starts = [compile_pattern(p) for p in section.get("start_patterns") or ()]
			count = max(1, int(section.get("lines") or 1))
			for index, line in enumerate(lines):
				if any(pattern.search(line) for pattern in starts):
					picked = lines[index : index + count]
					break
		out[name] = "\n".join(line for line in picked if not any(p.search(line) for p in excluded))
	return out


def extract(ocr_text, config: dict, carved=None) -> dict:
	"""{field: value} from the extractors. First match wins per field."""
	carved = carve(ocr_text, config.get("sections")) if carved is None else carved
	whole = "\n".join(_lines(ocr_text))
	out: dict = {}
	for row in config.get("extractors") or ():
		field = row.get("field")
		if not field or field in out:
			continue
		text = carved.get(row["section"], "") if row.get("section") else whole
		match = compile_pattern(row["pattern"]).search(text)
		if match:
			try:
				out[field] = (match.group(int(row.get("group") or 0)) or "").strip()
			except IndexError:
				continue
	return out


def _blank(value) -> bool:
	return value is None or value == "" or value == [] or value == {}


def lookup(values: dict, path: str):
	"""A field's value; "a.b" is the list of `b` across the items of `a`."""
	head, _, tail = str(path or "").partition(".")
	value = (values or {}).get(head)
	if not tail:
		return value
	return [item.get(tail) for item in value or () if isinstance(item, dict)]


def _number(value):
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def when_holds(when, values: dict, context=None) -> bool:
	"""A rule's `when {field, equals}`: context keys (e.g. the server's
	`pesticide_use_scope`) are read first, then the values. No `when` holds."""
	if not when:
		return True
	field = when.get("field")
	value = (context or {}).get(field)
	if _blank(value):
		value = lookup(values, field)
	return (
		str(value if value is not None else "").strip().lower()
		== str(when.get("equals") or "").strip().lower()
	)


def evaluate_rules(config: dict, values: dict, context=None) -> dict:
	"""{results: [{code, field, kind, ok, skipped, severity, message}], not_applicable: [fields]}.

	A rule whose `when` does not hold, or whose field is not applicable, is
	`skipped`. `required` fails on a blank; every other kind passes on a blank
	(a missing value is `required`'s business, not theirs)."""
	not_applicable: list = []
	for rule in config.get("rules") or ():
		if rule.get("kind") == "not_applicable" and when_holds(rule.get("when"), values, context):
			not_applicable.extend(rule.get("fields") or ())
	results = []
	for rule in config.get("rules") or ():
		kind = rule.get("kind")
		if kind == "not_applicable":
			continue
		field = rule.get("field")
		skipped = (
			not when_holds(rule.get("when"), values, context) or str(field).split(".")[0] in not_applicable
		)
		ok = True
		if not skipped:
			ok = _passes(rule, lookup(values, field))
		results.append(
			{
				"code": rule.get("code"),
				"field": field,
				"kind": kind,
				"ok": ok,
				"skipped": skipped,
				"severity": rule.get("severity", "warning"),
				"message": (rule.get("message") or {}).get("en", ""),
			}
		)
	return {"results": results, "not_applicable": sorted(set(not_applicable))}


def _passes(rule: dict, value) -> bool:
	kind = rule.get("kind")
	if kind == "required":
		return not _blank(value) and not (isinstance(value, list) and all(_blank(v) for v in value))
	if _blank(value):
		return True
	if kind == "pattern":
		return bool(compile_pattern(rule["pattern"]).search(str(value)))
	if kind in ("sum_max", "sum_min"):
		total = sum(
			n for n in (_number(v) for v in (value if isinstance(value, list) else [value])) if n is not None
		)
		return total <= float(rule["max"]) if kind == "sum_max" else total >= float(rule["min"])
	if kind == "range":
		number = _number(value)
		if number is None:
			return False
		return not (
			("min" in rule and number < float(rule["min"])) or ("max" in rule and number > float(rule["max"]))
		)
	if kind == "one_of":
		allowed = {str(v).strip().lower() for v in rule.get("values") or ()}
		return str(value).strip().lower() in allowed
	return True


# ── advisory findings ───────────────────────────────────────────────────────


def _rule_failed(code: str, config: dict, evaluated, deterministic: dict) -> bool:
	"""A rule "failed" when it ran and failed on the values, or when the
	server's own checks raised an error or warning on the rule's field (or
	under the rule's code). The second is what v0.202.0 §8.3 asked: an EPA
	finding stands only when the rules judged the number too."""
	rule = next((r for r in config.get("rules") or () if r.get("code") == code), None)
	if rule is None:
		return False
	if evaluated and any(
		r["code"] == code and not r["ok"] and not r["skipped"] for r in evaluated["results"]
	):
		return True
	field = str(rule.get("field") or "").split(".")[0]
	return any(
		(row.get("code") == code or row.get("field") == field) and row.get("severity") in ("error", "warning")
		for row in (deterministic or {}).get("issues") or ()
	)


def drop_reason(entry: dict, config, deterministic=None, values=None) -> str:
	"""Why this advisory finding is dropped under `config`, or ""."""
	if not config:
		return ""
	deterministic = deterministic or {}
	values = values or {}
	field = str(entry.get("field") or "")
	message = str(entry.get("message") or "")
	evaluated = evaluate_rules(config, values, deterministic) if values else None
	# §1.2: a `not_applicable` rule's fields are ignored, and any finding on them dropped.
	for index, rule in enumerate(config.get("rules") or ()):
		if (
			rule.get("kind") == "not_applicable"
			and field
			and field in (rule.get("fields") or ())
			and when_holds(rule.get("when"), values, deterministic)
		):
			return f"rules[{index}] ({field} is not applicable)"
	for index, row in enumerate(config.get("advisory_drop") or ()):
		wanted = list(row.get("fields") or ()) + ([row["field"]] if row.get("field") else [])
		if wanted and field not in wanted:
			continue
		if row.get("message_pattern") and not compile_pattern(row["message_pattern"]).search(message):
			continue
		if not when_holds(row.get("when"), values, deterministic):
			continue
		unless = row.get("unless_rule_failed")
		if unless and _rule_failed(unless, config, evaluated, deterministic):
			continue
		return f"advisory_drop[{index}]" + (f" (the rules accept it: {unless})" if unless else "")
	return ""


def message_limit(config) -> int:
	try:
		return int(((config or {}).get("llm") or {}).get("max_message_chars") or DEFAULT_MESSAGE_LIMIT)
	except (TypeError, ValueError):
		return DEFAULT_MESSAGE_LIMIT


# ── where a config comes from ───────────────────────────────────────────────


def builtin(document_type: str):
	"""(body, config_version) from the bundled file, or (None, "")."""
	slug = BUILTINS.get(document_type)
	if not slug:
		return None, ""
	path = BUILTIN_DIR / f"{slug}.json"
	if not path.exists():
		return None, ""
	revision = BUILTIN_REVISIONS.get(document_type, BUILTIN_REVISION)
	return json.loads(path.read_text()), version_string(document_type, f"builtin-{revision}")


def _doctype_ready() -> bool:
	try:
		return compat.doctype_exists(DOCTYPE)
	except Exception:
		return False


def rows(document_type: str = "") -> list:
	if not _doctype_ready():
		return []
	filters = {"document_type": document_type} if document_type else {}
	return frappe.db.get_all(
		DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"document_type",
			"version",
			"status",
			"config_version",
			"notes",
			"authored_by",
			"published_by",
			"published_on",
			"modified",
		],
		order_by="document_type asc, version desc",
		limit=100000,
	)


def row(document_type: str, version=None):
	"""The Extraction Config doc: `version`, else the Published one. None when absent."""
	if not _doctype_ready():
		return None
	filters = {"document_type": document_type}
	if version not in (None, ""):
		filters["version"] = int(version)
	else:
		filters["status"] = PUBLISHED
	name = frappe.db.get_value(DOCTYPE, filters, "name")
	return frappe.get_doc(DOCTYPE, name) if name else None


def body_of(doc) -> dict:
	try:
		return json.loads(doc.config_json or "{}")
	except ValueError:
		return {}


def staged(document_type: str):
	"""The Staged doc for a type, or None. At most one."""
	if not _doctype_ready() or not compat.has_field(DOCTYPE, "staged_users"):
		return None
	name = frappe.db.get_value(DOCTYPE, {"document_type": document_type, "status": STAGED}, "name")
	return frappe.get_doc(DOCTYPE, name) if name else None


def staged_users(doc) -> list:
	return [
		u.strip().lower()
		for u in str(doc.get("staged_users") or "").replace(",", "\n").splitlines()
		if u.strip()
	]


def active(document_type: str, user: str = ""):
	"""(body, config_version) in use for a type: Staged for the accounts it is staged
	to, else Published, then built-in."""
	if user:
		early = staged(document_type)
		if early is not None and str(user).strip().lower() in staged_users(early):
			body = body_of(early)
			if not problems(body):
				return body, early.config_version or version_string(document_type, early.version)
	doc = row(document_type)
	if doc is not None:
		body = body_of(doc)
		if not problems(body):
			return body, doc.config_version or version_string(document_type, doc.version)
	return builtin(document_type)


def next_version(document_type: str) -> int:
	versions = [int(r.get("version") or 0) for r in rows(document_type)]
	return max(versions or [0]) + 1


def create_draft(document_type: str, config: dict, notes: str = "", authored_by: str = "Operator"):
	found = problems(config)
	if found:
		raise ValueError("; ".join(found))
	config = dict(config)
	config.setdefault("document_type", document_type)
	doc = frappe.new_doc(DOCTYPE)
	doc.document_type = document_type
	doc.version = next_version(document_type)
	doc.status = DRAFT
	doc.config_json = json.dumps(config, indent=1, ensure_ascii=False)
	doc.notes = notes
	doc.authored_by = authored_by
	doc.insert(ignore_permissions=True)
	return doc


def publish(document_type: str, version, user: str = ""):
	"""Draft → Published; the previous Published → Superseded. Returns (doc, previous name)."""
	doc = row(document_type, version)
	if doc is None:
		raise LookupError(f"{document_type} has no version {version}.")
	if doc.status not in (DRAFT, STAGED):
		raise ValueError(f"{doc.name} is {doc.status}; only a Draft or a Staged version can be published.")
	for other in frappe.db.get_all(
		DOCTYPE, filters={"document_type": document_type, "status": STAGED}, fields=["name"], limit=20
	):
		if other["name"] != doc.name:
			frappe.db.set_value(DOCTYPE, other["name"], "status", SUPERSEDED)
	previous = row(document_type)
	if previous is not None:
		previous.status = SUPERSEDED
		previous.save(ignore_permissions=True)
	doc.status = PUBLISHED
	doc.published_by = user or getattr(frappe.session, "user", "") or ""
	doc.published_on = frappe.utils.now()
	doc.save(ignore_permissions=True)
	return doc, previous.name if previous is not None else ""


def stage(document_type: str, version, users, user: str = ""):
	"""Draft → Staged for `users` (logins). They are served it; nobody else is.

	A second look before everybody gets it: the phones of the people named fetch
	this version at their next capture, while the Published one stays in force
	for the rest. One Staged version per type — staging another returns the
	earlier one to Draft. Staging an already-Staged version replaces its users."""
	doc = row(document_type, version)
	if doc is None:
		raise LookupError(f"{document_type} has no version {version}.")
	if doc.status not in (DRAFT, STAGED):
		raise ValueError(f"{doc.name} is {doc.status}; only a Draft is staged.")
	if not compat.has_field(DOCTYPE, "staged_users"):
		raise ValueError("this site's Extraction Config has no staging yet — run `bench migrate`.")
	people = sorted({str(u).strip().lower() for u in users or () if str(u).strip()})
	if not people:
		raise ValueError("name who gets it first: users=[their login email, …].")
	missing = [u for u in people if not frappe.db.exists("User", u)]
	if missing:
		raise ValueError(f"no User called {', '.join(missing)} on this site.")
	found = problems(body_of(doc))
	if found:
		raise ValueError("the phone would refuse this config: " + "; ".join(found))
	for other in frappe.db.get_all(
		DOCTYPE, filters={"document_type": document_type, "status": STAGED}, fields=["name"], limit=20
	):
		if other["name"] != doc.name:
			frappe.db.set_value(DOCTYPE, other["name"], "status", DRAFT)
	doc.status = STAGED
	doc.staged_users = "\n".join(people)
	doc.save(ignore_permissions=True)
	return doc, people


def seed() -> list:
	"""Write each built-in as version 1 Published where a type has no row. Create-only."""
	if not _doctype_ready():
		return []
	made = []
	for document_type in BUILTINS:
		if rows(document_type):
			continue
		body, _version = builtin(document_type)
		if body is None:
			continue
		doc = frappe.new_doc(DOCTYPE)
		doc.document_type = document_type
		doc.version = 1
		doc.status = PUBLISHED
		doc.config_json = json.dumps(body, indent=1, ensure_ascii=False)
		doc.notes = "Built-in default, seeded at install (v0.206.0)."
		doc.authored_by = "System"
		doc.published_by = "Administrator"
		doc.published_on = frappe.utils.now()
		doc.insert(ignore_permissions=True)
		made.append(doc.name)
	return made


# ── preview ─────────────────────────────────────────────────────────────────


def _diff(recorded: dict, extracted: dict) -> list:
	out = []
	for field in sorted(set(extracted)):
		was = recorded.get(field)
		now = extracted.get(field)
		if str(was if was is not None else "") != str(now if now is not None else ""):
			out.append({"field": field, "recorded": was, "config": now})
	return out


def preview(config: dict, validation) -> dict:
	"""Run `config` against a stored Document Validation. Reads only."""
	ocr_text = validation.get("ocr_text") or ""
	try:
		recorded = json.loads(validation.get("extraction_json") or "{}")
	except ValueError:
		recorded = {}
	recorded = recorded if isinstance(recorded, dict) else {}
	recorded_fields = recorded.get("fields") if isinstance(recorded.get("fields"), dict) else recorded
	try:
		assessment = json.loads(validation.get("llm_assessment_json") or "{}")
	except ValueError:
		assessment = {}
	carved = carve(ocr_text, config.get("sections"))
	extracted = extract(ocr_text, config, carved)
	values = {**recorded_fields, **extracted}
	context = {}
	if validation.get("document_type") == "Pesticide Label":
		from erpnext_mcp import document_intel

		context["pesticide_use_scope"] = document_intel.pesticide_scope(values, ocr_text)
	deterministic = {**context, "issues": recorded.get("issues") or []}
	evaluated = evaluate_rules(config, values, context)
	dropped, kept = [], []
	for entry in (assessment or {}).get("issues") or ():
		if not isinstance(entry, dict):
			continue
		reason = drop_reason(entry, config, deterministic, values)
		(dropped if reason else kept).append({**entry, **({"dropped_by": reason} if reason else {})})
	return {
		"sections": carved,
		"extracted": extracted,
		"rules": evaluated["results"],
		"rule_failures": [r for r in evaluated["results"] if not r["ok"] and not r["skipped"]],
		"not_applicable": evaluated["not_applicable"],
		"advisory_dropped": dropped,
		"advisory_kept": kept,
		"diff": _diff(recorded_fields, extracted),
	}
