# SPDX-License-Identifier: MIT
"""Wizards as data: the handset draws what the site says, in the worker's language.

THE PROBLEM IS THE APP STORE. An onboarding flow compiled into Swift needs a
release to change a question — and the questions change when the law does, which
is on somebody else's schedule. A farm that discovers in July that its state now
requires a heat-illness acknowledgement at hire cannot wait three weeks for a
build and a review.

So a wizard is a ROW. `get_wizard_definition` returns ordered steps, the fields
on each, their validation, and the conditional logic that decides what comes
next; iOS renders whatever arrives. Adding a wizard is adding a record. That is
the "config not code" pattern this app already uses for compliance rules, KPI
definitions and farm task templates, applied to the last thing still hard-coded.

WHAT THE HANDSET IS TRUSTED WITH, AND WHAT IT IS NOT. The definition carries
validation so a worker finds out about a malformed VIN at the machine rather
than at submission — that is a courtesy, not a control. THE SERVER STILL
VALIDATES EVERYTHING at the endpoint the wizard submits to, because a client-side
rule is a suggestion to anybody holding the credential. Nothing in this module
is a gate; `submit_method` names an endpoint whose own refusals are unchanged.

────────────────────────────────────────────────────────────────────────────
THE LANGUAGE IS THE WORKER'S, AND A MISSING TRANSLATION IS REPORTED
────────────────────────────────────────────────────────────────────────────

A farm workforce in this region works in English and Spanish, and a system that
works well in one of them works badly for half the crew. So every label, title,
description, help text and select option carries its translations beside it, and
`get_wizard_definition` resolves them against the caller's own
`preferred_language` before it answers — the handset gets one set of strings and
does not carry a lookup table that drifts from the server's.

A MISSING TRANSLATION FALLS BACK TO ENGLISH AND SAYS SO. `untranslated` lists
every key that fell back, so an operator maintaining a wizard can see the gap.
The alternative — silently serving English — means nobody finds out until a
worker is standing in front of a screen they cannot read, and the alternative
after that (refusing to serve an untranslated wizard) locks the crew out of a
flow over a missing string. Fall back, and be loud about it.

THE SAME PATTERN EXTENDS. `label_en` / `label_es` are columns because two
languages are what this farm needs and a JSON blob of locales would be
unqueryable and unmaintainable in the Desk; `translations_for` is where a third
language gets added, and the shape of the answer does not change when it is.
"""

from __future__ import annotations

import json

import frappe

from .. import compat
from ..args import as_bool, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import translations

WIZARD = "Wizard Definition"
STEP = "Wizard Step"
FIELD = "Wizard Field"
EMPLOYEE = "Employee"

#: Every column `_field` reads off a Wizard Field row. Named once because the
#: rows are now FETCHED rather than read off a document somebody loaded, and a
#: query that forgot a column would serve a field with no help text and no
#: validation and look exactly like a wizard nobody finished writing.
FIELD_COLUMNS = (
	"name",
	"idx",
	"fieldname",
	"field_type",
	"label_en",
	"label_es",
	"placeholder_en",
	"placeholder_es",
	"help_en",
	"help_es",
	"required",
	"options",
	"default_value",
	"min_value",
	"max_value",
	"max_length",
	"pattern",
	"visible_if",
	"target_field",
)

#: The languages this app ships strings in. NOT a restriction on what may be
#: stored or asked for — an unknown code falls back to English and says so — but
#: the set `install_wizard_definitions` writes and the set a refusal names.
LANGUAGES = ("en", "es")
DEFAULT_LANGUAGE = "en"

#: Fields whose value is translated, as (base name, the suffix pattern). Named
#: once so `_resolve` and the seeder cannot come to disagree about which strings
#: are translatable.
TRANSLATABLE = ("label", "title", "description", "placeholder", "help")

REGISTER_CAP = 100

#: The most fields one step may serve. A cap rather than no cap because this is
#: now a query per step and an unbounded one on a table an operator writes is
#: how a read tool becomes a way to make the site slow. Twelve is the widest
#: shipped step; a hundred is a number no honest wizard reaches.
FIELD_CAP = 100


def _require() -> None:
	compat.require_doctype(
		WIZARD,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def preferred_language(user: str = "", employee: str = "") -> str:
	"""The language this person reads, or "" where nothing says.

	READ OFF THE EMPLOYEE RECORD, which is where `install_compliance_fields`
	adds `preferred_language`. Never guessed from a locale header: a phone set to
	English by whoever handed it over says nothing about who is holding it now,
	and getting this wrong silently is the failure the column exists to prevent.
	"""
	if employee and compat.doctype_exists(EMPLOYEE) and compat.has_field(EMPLOYEE, "preferred_language"):
		try:
			value = frappe.db.get_value(EMPLOYEE, employee, "preferred_language")
			if value:
				return str(value).strip().lower()[:12]
		except Exception:  # pragma: no cover - a site shaping the column differently
			pass
	if user and compat.doctype_exists(EMPLOYEE) and compat.has_field(EMPLOYEE, "preferred_language"):
		try:
			rows = frappe.db.get_all(
				EMPLOYEE, filters={"user_id": user}, fields=["preferred_language"], limit=1
			)
			if rows and rows[0].get("preferred_language"):
				return str(rows[0]["preferred_language"]).strip().lower()[:12]
		except Exception:  # pragma: no cover
			pass
	return ""


def _resolve(row: dict, base: str, language: str, missing: list, where: str) -> str:
	"""One translatable string in the requested language, or English, or "".

	Records the fall-back in `missing` rather than swallowing it — see the
	module docstring on why a silent English default is the worst of the three
	options.

	v0.85.0: A `tr:`-PREFIXED VALUE IS A KEY, not a label. The per-wizard columns
	stay exactly right for a string that belongs to one wizard, and stay the
	default — anything without the prefix behaves as it did before this release,
	which is what makes the change safe on a site whose wizards an operator has
	already edited. What the prefix buys is the string that appears in nine
	wizards: "Photograph", "Signature", "Block". Nine copies of a label drift,
	and the ninth is the one nobody fixed. `tr:wizard.field.photo` is one row in
	`Farm Translation` that all nine read, and a fall-back through it is reported
	on exactly the same `missing` channel as a fall-back on a column — so one gap
	looks the same to a reader wherever it came from.
	"""
	wanted = row.get(f"{base}_{language}") if language else None
	if wanted:
		return _dereference(str(wanted), language, missing, f"{where}.{base}")
	english = row.get(f"{base}_{DEFAULT_LANGUAGE}")
	if english and translations_available() and translations.is_reference(english):
		# THE ENGLISH COLUMN HOLDS A KEY, so there is nothing to fall back FROM:
		# the register is asked for the requested language directly, and reports
		# its own fall-back if it has one. Recording a column-level miss here as
		# well would double-count one gap.
		return _dereference(str(english), language, missing, f"{where}.{base}")
	if language and language != DEFAULT_LANGUAGE and english:
		missing.append({"where": where, "key": base, "language": language})
	return str(english or "")


def translations_available() -> bool:
	"""Whether this site has the string register. Never raises.

	Checked on every dereference rather than once at import, because a bench
	mid-migrate has the wizards and not yet the register — and the right
	behaviour there is the pre-v0.85.0 one: a `tr:` value is served as the
	literal it is, which is ugly and visible rather than blank and silent.
	"""
	try:
		return translations.available()
	except Exception:  # pragma: no cover - a bench with no doctype table yet
		return False


def _dereference(value: str, language: str, missing: list, where: str) -> str:
	"""Resolve a `tr:` reference through the register; pass a literal through."""
	if not translations.is_reference(value):
		return value
	if not translations_available():
		return value
	return translations.dereference(value, language or DEFAULT_LANGUAGE, missing, where)


def _options(raw, language: str, missing: list, where: str) -> list:
	"""A select's choices, translated where the definition translates them.

	Two accepted shapes and both are right for different fields: a list of plain
	strings where the values ARE the labels (an item code), and a list of objects
	where they are not (a severity, whose Spanish a worker needs).
	"""
	if not raw:
		return []
	try:
		parsed = json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, ValueError, TypeError):
		return []
	if not isinstance(parsed, list):
		return []
	out = []
	for entry in parsed:
		if isinstance(entry, str):
			out.append({"value": entry, "label": entry})
			continue
		if not isinstance(entry, dict):
			continue
		value = str(entry.get("value") or "")
		out.append(
			{
				"value": value,
				"label": _resolve(entry, "label", language, missing, f"{where}.options.{value}") or value,
			}
		)
	return out


def _condition(raw):
	"""A `visible_if` blob, parsed. Malformed means always-visible, never hidden.

	THE FAIL-OPEN DIRECTION IS THE SAFE ONE HERE. A condition nobody can parse
	should show the field — a worker seeing a question that did not apply is a
	minor annoyance; a required question silently hidden by a typo in a JSON blob
	is a record with a hole in it that nobody notices.
	"""
	if not raw:
		return None
	try:
		parsed = json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, ValueError, TypeError):
		return None
	return parsed if isinstance(parsed, dict) else None


def _field(row: dict, language: str, missing: list, step_key: str) -> dict:
	where = f"{step_key}.{row.get('fieldname')}"
	return {
		"fieldname": row.get("fieldname"),
		"type": row.get("field_type") or "text",
		"label": _resolve(row, "label", language, missing, where),
		"placeholder": _resolve(row, "placeholder", language, missing, where) or None,
		"help": _resolve(row, "help", language, missing, where) or None,
		"required": bool(frappe.utils.cint(row.get("required"))),
		"options": _options(row.get("options"), language, missing, where),
		"default": row.get("default_value") or None,
		"validation": {
			"min": float(row["min_value"]) if row.get("min_value") not in (None, "") else None,
			"max": float(row["max_value"]) if row.get("max_value") not in (None, "") else None,
			"max_length": int(row["max_length"]) if row.get("max_length") else None,
			"pattern": row.get("pattern") or None,
		},
		"visible_if": _condition(row.get("visible_if")),
		"target_field": row.get("target_field") or row.get("fieldname"),
	}


def fields_of(step) -> list:
	"""One step's Wizard Field rows, FETCHED — not read off the step in hand.

	THIS IS THE WHOLE OF THE v0.91.0 "nothing to fill" BUG. `Wizard Field` is a
	child table of `Wizard Step`, which is itself a child table of
	`Wizard Definition` — a grandchild, and Frappe traverses exactly one level in
	both directions. `Document.load_from_db` fills the definition's `steps` and
	stops, so `step.get("fields")` on a loaded document is EMPTY on every site,
	for every wizard, always. `describe()` read it anyway and answered
	`fields: []` five times over; the handset refused to draw a form with nothing
	on it, which was the correct call about an incorrect payload.

	So the rows are asked for by `parent`, which is the step row's own docname.
	`parenttype` and `parentfield` are filtered on as well as `parent` because a
	child docname is a hash from a shared pool — a `Wizard Field` and some other
	doctype's row cannot collide today, but filtering on the pointer's three
	halves is what makes that a property rather than a coincidence.

	THE IN-MEMORY ROWS ARE THE FALLBACK, AND ONLY FOR AN UNSAVED DOCUMENT. A
	definition being validated has its fields appended and no docnames yet, and
	that document is the one `WizardDefinition.validate` walks. Once it is
	saved, the fetch is the answer — including when the fetch finds nothing,
	which is a step whose fields were never written and is worth seeing as the
	empty step it is rather than papering over with whatever the caller happened
	to have in hand.
	"""
	parent = str(step.get("name") or "")
	if not parent:
		return [dict(row) for row in (step.get("fields") or [])]
	try:
		rows = (
			frappe.db.get_all(
				FIELD,
				filters={"parent": parent, "parenttype": STEP, "parentfield": "fields"},
				fields=compat.existing_fields(FIELD, FIELD_COLUMNS),
				order_by="idx asc",
				limit=FIELD_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover - a bench mid-migrate with no table yet
		rows = []
	return [dict(row) for row in rows]


def describe(doc, language: str) -> dict:
	"""One wizard, resolved into the language asked for."""
	missing: list = []
	row = dict(doc.as_dict()) if hasattr(doc, "as_dict") else dict(doc)
	steps = []
	for step in doc.get("steps") or []:
		step = dict(step) if not hasattr(step, "get") else step
		key = str(step.get("step_key") or "")
		steps.append(
			{
				"step_key": key,
				"title": _resolve(step, "title", language, missing, key),
				"description": _resolve(step, "description", language, missing, key) or None,
				"optional": bool(frappe.utils.cint(step.get("optional"))),
				"visible_if": _condition(step.get("visible_if")),
				"next_step": step.get("next_step") or None,
				"fields": [_field(dict(field), language, missing, key) for field in fields_of(step)],
			}
		)

	return {
		"wizard_key": row.get("wizard_key") or row.get("name"),
		"title": _resolve(row, "title", language, missing, "wizard"),
		"description": _resolve(row, "description", language, missing, "wizard") or None,
		"category": row.get("category") or None,
		"version": int(row.get("version") or 1),
		"submit_method": row.get("submit_method") or None,
		"icon": row.get("icon") or None,
		"required_role": row.get("required_role") or None,
		"language": language or DEFAULT_LANGUAGE,
		"step_count": len(steps),
		"steps": steps,
		"field_count": sum(len(step["fields"]) for step in steps),
		# EVERY STRING THAT FELL BACK, so a gap is visible to whoever maintains
		# the wizard rather than to the worker who hits it.
		"untranslated": missing,
		"fully_translated": not missing,
	}


# ── get_wizard_definition ───────────────────────────────────────────────────
def get_wizard_definition(args: dict) -> ToolResult:
	"""One wizard's spec, in the caller's language, for a handset to render."""
	_require()
	key = as_str(args, "wizard", required=True) or as_str(args, "wizard_key")
	if not frappe.db.exists(WIZARD, key):
		known = frappe.db.get_all(WIZARD, filters={"enabled": 1}, pluck="name", limit=REGISTER_CAP)
		raise ToolError(
			f"no wizard called {key!r} on this site. Available: "
			f"{', '.join(sorted(str(name) for name in known)) or '<none — run install_wizard_definitions>'}."
		)

	doc = frappe.get_doc(WIZARD, key)
	if not frappe.utils.cint(doc.enabled) and not as_bool(args, "include_disabled", False):
		raise ToolError(
			f"wizard {key!r} is disabled. An operator withdrew it, probably because it is being "
			"revised. Send include_disabled=true to read it anyway."
		)

	language = (as_str(args, "language") or "").strip().lower()
	if not language:
		language = preferred_language(as_str(args, "user"), as_str(args, "employee")) or DEFAULT_LANGUAGE

	data = describe(doc, language)
	if data["untranslated"]:
		data["translation_note"] = (
			f"{len(data['untranslated'])} string(s) have no {language} translation and fell back to "
			f"{DEFAULT_LANGUAGE}. The worker will see English for those. They are listed above so "
			"they can be filled in — a wizard half in one language is worse than one that is "
			"consistently in the wrong one."
		)
	data["server_validates_anyway"] = (
		"The validation in this definition is so a worker finds out at the machine rather than at "
		f"submission. It is not a control: {data['submit_method'] or 'the submit endpoint'} applies "
		"its own refusals to whatever arrives, because a client-side rule is a suggestion to "
		"anybody holding the credential."
	)
	return ToolResult(
		data=data,
		summary=(
			f"{data['wizard_key']} v{data['version']}: {data['step_count']} step(s), "
			f"{data['field_count']} field(s), {language}"
			+ ("" if data["fully_translated"] else f", {len(data['untranslated'])} untranslated")
		),
	)


# ── list_wizard_definitions ─────────────────────────────────────────────────
def list_wizard_definitions(args: dict) -> ToolResult:
	"""What this site can render, without pulling every field of every one."""
	_require()
	language = (
		(as_str(args, "language") or "").strip().lower()
		or preferred_language(as_str(args, "user"), as_str(args, "employee"))
		or DEFAULT_LANGUAGE
	)

	filters: dict = {}
	if not as_bool(args, "include_disabled", False):
		filters["enabled"] = 1
	category = as_str(args, "category")
	if category:
		filters["category"] = category

	rows = (
		frappe.db.get_all(
			WIZARD,
			filters=filters,
			fields=compat.existing_fields(
				WIZARD,
				(
					"name",
					"wizard_key",
					"title_en",
					"title_es",
					"description_en",
					"description_es",
					"category",
					"version",
					"submit_method",
					"icon",
					"required_role",
					"enabled",
					"shipped_default",
				),
			),
			order_by="category asc, wizard_key asc",
			limit=min(as_limit(args), REGISTER_CAP),
		)
		or []
	)

	missing: list = []
	wizards = []
	for raw in rows:
		row = dict(raw)
		key = str(row.get("wizard_key") or row.get("name"))
		wizards.append(
			{
				"wizard_key": key,
				"title": _resolve(row, "title", language, missing, key),
				"description": _resolve(row, "description", language, missing, key) or None,
				"category": row.get("category") or None,
				"version": int(row.get("version") or 1),
				"submit_method": row.get("submit_method") or None,
				"icon": row.get("icon") or None,
				"required_role": row.get("required_role") or None,
				"enabled": bool(frappe.utils.cint(row.get("enabled"))),
				"shipped_default": bool(frappe.utils.cint(row.get("shipped_default"))),
			}
		)

	return ToolResult(
		data={
			"language": language,
			"wizard_count": len(wizards),
			"wizards": wizards,
			"categories": sorted({w["category"] for w in wizards if w["category"]}),
			"untranslated": missing,
		},
		summary=f"{len(wizards)} wizard(s) available in {language}",
	)


# ══════════════════════════════════════════════════════════════════════════════
# The shipped definitions
#
# SEEDED AS RECORDS, NOT SERVED FROM THIS FILE. The whole argument of the module
# is that a wizard is data an operator can edit, and a shipped wizard that lived
# in Python would be one they could not. So `install_wizard_definitions` writes
# rows on migrate and NEVER OVERWRITES an existing one — a flow somebody tuned
# for their own crew being reset by an upgrade is what would make "config not
# code" a lie.
# ══════════════════════════════════════════════════════════════════════════════


def _f(fieldname, field_type, en, es, required=False, **extra):
	row = {
		"fieldname": fieldname,
		"field_type": field_type,
		"label_en": en,
		"label_es": es,
		"required": 1 if required else 0,
	}
	for key, value in extra.items():
		row[key] = (
			json.dumps(value) if key in ("options", "visible_if") and not isinstance(value, str) else value
		)
	return row


def _step(key, en, es, fields, description_en="", description_es="", **extra):
	return {
		"step_key": key,
		"title_en": en,
		"title_es": es,
		"description_en": description_en,
		"description_es": description_es,
		"fields": fields,
		**extra,
	}


SHIPPED_WIZARDS = (
	{
		"wizard_key": "accident_investigation",
		"title_en": "Report an Accident",
		"title_es": "Reportar un Accidente",
		"description_en": "What happened, who was hurt, who saw it. Root cause comes later.",
		"description_es": "Qué pasó, quién se lastimó, quién lo vio. La causa raíz viene después.",
		"category": "Safety",
		"submit_method": "create_accident_report",
		"icon": "alert-triangle",
		"steps": [
			_step(
				"what",
				"What Happened",
				"Qué Pasó",
				[
					_f(
						"occurred_at",
						"datetime",
						"When did it happen?",
						"¿Cuándo pasó?",
						required=True,
						help_en="The time it happened, not the time you are filling this in.",
						help_es="La hora en que pasó, no la hora en que llena esto.",
					),
					_f(
						"severity",
						"select",
						"How bad was it?",
						"¿Qué tan grave fue?",
						required=True,
						options=[
							{
								"value": "Near Miss",
								"label_en": "Near miss — nobody hurt",
								"label_es": "Casi accidente — nadie herido",
							},
							{
								"value": "First Aid",
								"label_en": "First aid only",
								"label_es": "Solo primeros auxilios",
							},
							{
								"value": "Medical Treatment",
								"label_en": "Needed a doctor",
								"label_es": "Necesitó un médico",
							},
							{"value": "Lost Time", "label_en": "Missed work", "label_es": "Faltó al trabajo"},
							{
								"value": "Hospitalisation",
								"label_en": "Hospitalised",
								"label_es": "Hospitalizado",
							},
							{"value": "Amputation", "label_en": "Amputation", "label_es": "Amputación"},
							{"value": "Fatality", "label_en": "Fatality", "label_es": "Fallecimiento"},
						],
					),
					_f(
						"incident_description",
						"long_text",
						"Describe what happened",
						"Describe qué pasó",
						required=True,
						help_en="In your own words. Be specific — what was being done, what went wrong, in what order.",
						help_es="Con tus propias palabras. Sé específico: qué se estaba haciendo, qué salió mal, en qué orden.",
					),
					_f(
						"narrative_audio",
						"audio_note",
						"Or record it instead of typing",
						"O grábelo en vez de escribirlo",
						help_en="Speak it. The phone writes it down and keeps the recording.",
						help_es="Háblelo. El teléfono lo escribe y guarda la grabación.",
					),
				],
				"The account written at the scene is worth more than the one written tonight.",
				"El relato escrito en el lugar vale más que el escrito esta noche.",
			),
			_step(
				"where",
				"Where",
				"Dónde",
				[
					_f(
						"location_description",
						"text",
						"Where exactly?",
						"¿Dónde exactamente?",
						help_en="'Third ladder from the north end of the packing line' beats a block name.",
						help_es="'Tercera escalera del extremo norte de la línea' es mejor que un nombre de bloque.",
					),
					_f("asset", "qr_scan", "Scan the machine's tag", "Escanea la etiqueta de la máquina"),
				],
			),
			_step(
				"who",
				"Who Was Hurt",
				"Quién se Lastimó",
				[
					_f("injured_person", "employee_select", "Who was injured?", "¿Quién se lastimó?"),
					_f(
						"injury_type",
						"select",
						"Type of injury",
						"Tipo de lesión",
						options=[
							{"value": "Laceration", "label_en": "Cut", "label_es": "Cortada"},
							{"value": "Fracture", "label_en": "Broken bone", "label_es": "Hueso roto"},
							{
								"value": "Sprain or Strain",
								"label_en": "Sprain or strain",
								"label_es": "Esguince o torcedura",
							},
							{"value": "Burn", "label_en": "Burn", "label_es": "Quemadura"},
							{
								"value": "Chemical Exposure",
								"label_en": "Chemical exposure",
								"label_es": "Exposición química",
							},
							{
								"value": "Heat Illness",
								"label_en": "Heat illness",
								"label_es": "Enfermedad por calor",
							},
							{"value": "Eye Injury", "label_en": "Eye injury", "label_es": "Lesión ocular"},
							{"value": "Crush", "label_en": "Crush", "label_es": "Aplastamiento"},
							{"value": "Other", "label_en": "Other", "label_es": "Otro"},
						],
					),
					_f("body_part", "text", "What part of the body?", "¿Qué parte del cuerpo?"),
					_f(
						"medical_treatment",
						"select",
						"What treatment was given?",
						"¿Qué tratamiento recibió?",
						options=[
							{"value": "None", "label_en": "None", "label_es": "Ninguno"},
							{
								"value": "First Aid Only",
								"label_en": "First aid only",
								"label_es": "Solo primeros auxilios",
							},
							{
								"value": "Medical Treatment Beyond First Aid",
								"label_en": "Saw a doctor",
								"label_es": "Vio a un médico",
							},
							{
								"value": "Emergency Room",
								"label_en": "Emergency room",
								"label_es": "Sala de emergencias",
							},
							{
								"value": "Hospitalised",
								"label_en": "Admitted to hospital",
								"label_es": "Ingresado al hospital",
							},
						],
					),
				],
				"Skip this on a near miss — a near miss is the same event with a better outcome, and worth recording.",
				"Omita esto si fue un casi accidente — vale la pena registrarlo igual.",
				visible_if=json.dumps({"field": "severity", "not_equals": "Near Miss"}),
			),
			_step(
				"witnesses",
				"Who Saw It",
				"Quién lo Vio",
				[
					_f(
						"witnesses",
						"multi_select",
						"Witnesses",
						"Testigos",
						help_en="Anybody who saw it, including people who do not work here.",
						help_es="Cualquiera que lo vio, incluso quien no trabaja aquí.",
					),
				],
			),
			_step(
				"immediate",
				"What Was Done",
				"Qué se Hizo",
				[
					_f(
						"immediate_actions",
						"long_text",
						"What did you do straight away?",
						"¿Qué hizo de inmediato?",
						required=True,
						help_en="Machine locked out, block closed, ambulance called.",
						help_es="Máquina bloqueada, bloque cerrado, ambulancia llamada.",
					),
					_f(
						"scene_photo",
						"photo",
						"Photograph the scene",
						"Fotografíe el lugar",
						help_en="Before anything is moved or tidied.",
						help_es="Antes de mover o limpiar algo.",
					),
				],
			),
		],
	},
	{
		"wizard_key": "progressive_discipline",
		"title_en": "Document Discipline",
		"title_es": "Documentar Disciplina",
		"description_en": "One step of a progressive chain, linked to the ones before it.",
		"description_es": "Un paso de una cadena progresiva, ligado a los anteriores.",
		"category": "HR",
		"submit_method": "create_discipline_record",
		"icon": "file-text",
		"required_role": "HR Manager",
		"steps": [
			_step(
				"who",
				"Who and What Level",
				"Quién y Qué Nivel",
				[
					_f("employee", "employee_select", "Employee", "Empleado", required=True),
					_f(
						"discipline_type",
						"select",
						"Step",
						"Paso",
						required=True,
						options=[
							{
								"value": "Verbal Warning",
								"label_en": "Verbal warning",
								"label_es": "Advertencia verbal",
							},
							{
								"value": "Written Warning",
								"label_en": "Written warning",
								"label_es": "Advertencia escrita",
							},
							{
								"value": "Final Warning",
								"label_en": "Final warning",
								"label_es": "Advertencia final",
							},
							{"value": "Suspension", "label_en": "Suspension", "label_es": "Suspensión"},
							{"value": "Termination", "label_en": "Termination", "label_es": "Despido"},
						],
						help_en="The app links this to the previous step automatically.",
						help_es="La aplicación lo liga al paso anterior automáticamente.",
					),
					_f(
						"incident_date",
						"date",
						"When did the incident happen?",
						"¿Cuándo pasó el incidente?",
						required=True,
					),
				],
			),
			_step(
				"incident",
				"The Incident",
				"El Incidente",
				[
					_f(
						"incident_description",
						"long_text",
						"What happened?",
						"¿Qué pasó?",
						required=True,
						help_en="Specifics. Dates, times, what was said or done. 'Attitude problem' cannot be defended.",
						help_es="Detalles. Fechas, horas, qué se dijo o hizo. 'Problema de actitud' no se puede defender.",
					),
					_f("policy_violated", "text", "Which rule or policy?", "¿Qué regla o política?"),
					_f("witnesses", "text", "Who else was there?", "¿Quién más estaba?"),
					_f(
						"narrative_audio",
						"audio_note",
						"Record the conversation account",
						"Grabe el relato de la conversación",
						help_en="Straight after the meeting, while you remember it.",
						help_es="Justo después de la reunión, mientras la recuerda.",
					),
				],
			),
			_step(
				"expectation",
				"What Has to Change",
				"Qué Debe Cambiar",
				[
					_f(
						"expected_improvement",
						"long_text",
						"What must improve?",
						"¿Qué debe mejorar?",
						required=True,
						help_en="Stated so both sides can tell later whether it happened. 'Clock in by 06:00 on every scheduled shift for 60 days' — not 'improve punctuality'.",
						help_es="Escrito para que ambos puedan verificar después. 'Marcar entrada a las 06:00 en cada turno por 60 días' — no 'mejorar la puntualidad'.",
					),
					_f(
						"followup_date",
						"date",
						"When will you check?",
						"¿Cuándo lo va a revisar?",
						required=True,
					),
					_f(
						"consequence_of_no_improvement",
						"text",
						"What happens if it does not?",
						"¿Qué pasa si no mejora?",
					),
				],
			),
			_step(
				"acknowledge",
				"Acknowledgement",
				"Reconocimiento",
				[
					_f(
						"employee_statement",
						"long_text",
						"The employee's own account",
						"El relato del propio empleado",
						help_en="Recording it makes the chain stronger, not weaker.",
						help_es="Registrarlo hace la cadena más fuerte, no más débil.",
					),
					_f("employee_signature", "signature", "Employee signature", "Firma del empleado"),
					_f(
						"manager_signature",
						"signature",
						"Manager signature",
						"Firma del supervisor",
						required=True,
					),
				],
				"An employee may decline to sign. Record that instead — it is an outcome, not a gap.",
				"El empleado puede negarse a firmar. Regístrelo — es un resultado, no un vacío.",
			),
		],
	},
	{
		"wizard_key": "asset_registration",
		"title_en": "Register an Asset",
		"title_es": "Registrar un Equipo",
		"description_en": "Photograph the plate, register the machine, print the tag.",
		"description_es": "Fotografía la placa, registra la máquina, imprime la etiqueta.",
		"category": "Assets",
		"submit_method": "register_asset",
		"icon": "tag",
		"steps": [
			_step(
				"identity",
				"What Is It",
				"Qué Es",
				[
					_f(
						"name",
						"text",
						"Tag ID",
						"ID de etiqueta",
						required=True,
						help_en="What gets printed on the label. This IS the record's name.",
						help_es="Lo que se imprime en la etiqueta. Este ES el nombre del registro.",
					),
					_f(
						"asset_type",
						"select",
						"Type",
						"Tipo",
						required=True,
						options=[
							{"value": "Tractor", "label_en": "Tractor", "label_es": "Tractor"},
							{"value": "Vehicle", "label_en": "Vehicle", "label_es": "Vehículo"},
							{"value": "Implement", "label_en": "Implement", "label_es": "Implemento"},
							{"value": "Sprayer", "label_en": "Sprayer", "label_es": "Aspersora"},
							{
								"value": "Irrigation Valve",
								"label_en": "Irrigation valve",
								"label_es": "Válvula de riego",
							},
							{"value": "Housing Unit", "label_en": "Housing unit", "label_es": "Vivienda"},
							{"value": "Cold Storage", "label_en": "Cold storage", "label_es": "Cámara fría"},
							{"value": "General", "label_en": "Other", "label_es": "Otro"},
						],
					),
					_f("description", "text", "What is it, in words?", "¿Qué es, en palabras?"),
				],
			),
			_step(
				"plate",
				"The Plate",
				"La Placa",
				[
					_f(
						"plate_photo",
						"photo",
						"Photograph the serial plate",
						"Fotografíe la placa de serie",
						help_en="The app reads the VIN off it.",
						help_es="La aplicación lee el VIN de ella.",
					),
					_f("serial_number", "text", "Serial or VIN", "Serie o VIN"),
					_f("model", "text", "Make and model", "Marca y modelo"),
				],
				"Only on machines. A valve has no plate.",
				"Solo en máquinas. Una válvula no tiene placa.",
				visible_if=json.dumps(
					{"field": "asset_type", "in": ["Tractor", "Vehicle", "Implement", "Sprayer"]}
				),
			),
			_step(
				"service",
				"Service Schedule",
				"Programa de Servicio",
				[
					_f(
						"service_interval_hours",
						"number",
						"Service every … hours",
						"Servicio cada … horas",
						min_value=0,
					),
					_f("service_interval_days", "number", "…or every … days", "…o cada … días", min_value=0),
					_f("last_service_date", "date", "Last serviced", "Último servicio"),
				],
				"Set it now while the manual is open. A machine with no schedule never comes up as due.",
				"Póngalo ahora que tiene el manual. Una máquina sin programa nunca aparece como vencida.",
				optional=1,
			),
		],
	},
	{
		"wizard_key": "employee_onboarding",
		"title_en": "New Employee",
		"title_es": "Empleado Nuevo",
		"description_en": "Identity, assignment, contact and the language they read.",
		"description_es": "Identidad, asignación, contacto y el idioma que leen.",
		"category": "HR",
		"submit_method": "create_employee",
		"icon": "user-plus",
		"required_role": "HR Manager",
		"steps": [
			_step(
				"identity",
				"Who",
				"Quién",
				[
					_f("first_name", "text", "First name", "Nombre", required=True),
					_f("last_name", "text", "Last name", "Apellido", required=True),
					_f("date_of_birth", "date", "Date of birth", "Fecha de nacimiento"),
					_f("date_of_joining", "date", "First day", "Primer día", required=True),
				],
			),
			_step(
				"language",
				"Language",
				"Idioma",
				[
					_f(
						"preferred_language",
						"select",
						"Which language do they read?",
						"¿Qué idioma leen?",
						required=True,
						options=[
							{"value": "es", "label_en": "Spanish", "label_es": "Español"},
							{"value": "en", "label_en": "English", "label_es": "Inglés"},
						],
						help_en="Every form, warning and task this person sees comes in this language.",
						help_es="Cada formulario, aviso y tarea que esta persona vea vendrá en este idioma.",
					),
				],
				"Asked at hire because getting it wrong silently is the failure — a phone set to English says nothing about who is holding it.",
				"Se pregunta al contratar porque equivocarse en silencio es el problema.",
			),
			_step(
				"assignment",
				"Assignment",
				"Asignación",
				[
					_f("branch", "select", "Branch", "Sucursal"),
					_f("department", "select", "Department", "Departamento"),
					_f("designation", "select", "Position", "Puesto"),
					_f(
						"jurisdiction",
						"text",
						"Wage-law state",
						"Estado de ley salarial",
						help_en="Where the work is performed, not where the employer sits.",
						help_es="Donde se hace el trabajo, no donde está el empleador.",
					),
				],
			),
			_step(
				"contact",
				"Contact",
				"Contacto",
				[
					_f("cell_number", "text", "Phone", "Teléfono"),
					_f("personal_email", "text", "Email", "Correo"),
					_f("photo", "photo", "Photograph for the badge", "Foto para la credencial"),
				],
			),
		],
	},
	{
		"wizard_key": "inspection_session",
		"title_en": "Run an Inspection",
		"title_es": "Hacer una Inspección",
		"description_en": "Pick the template, walk the checklist, file the evidence.",
		"description_es": "Elija la plantilla, siga la lista, archive la evidencia.",
		"category": "Compliance",
		"submit_method": "start_inspection",
		"icon": "clipboard-check",
		"steps": [
			_step(
				"target",
				"What Are You Inspecting",
				"Qué Va a Inspeccionar",
				[
					_f(
						"location",
						"qr_scan",
						"Scan the tag",
						"Escanee la etiqueta",
						required=True,
						help_en="The cabin, the machine, the block.",
						help_es="La cabaña, la máquina, el bloque.",
					),
					_f("template", "select", "Which inspection?", "¿Cuál inspección?", required=True),
				],
			),
			_step(
				"findings",
				"What You Found",
				"Lo Que Encontró",
				[
					_f(
						"findings_text",
						"long_text",
						"What did you find?",
						"¿Qué encontró?",
						required=True,
						help_en="Whether or not anything was wrong. 'Nothing found' is a finding.",
						help_es="Aunque no haya nada malo. 'Nada encontrado' es un hallazgo.",
					),
					_f("photos", "photo", "Photographs", "Fotografías", required=True),
					_f("narrative_audio", "audio_note", "Or say it", "O dígalo"),
				],
			),
			_step(
				"sign",
				"Sign Off",
				"Firmar",
				[
					_f(
						"signature",
						"signature",
						"Your signature",
						"Su firma",
						required=True,
						help_en="You are attesting to what you found.",
						help_es="Está certificando lo que encontró.",
					),
				],
			),
		],
	},
)


def _step_names(wizard: str) -> list:
	"""The docnames of one wizard's step rows, which is what a field points at."""
	try:
		return [
			str(row["name"])
			for row in frappe.db.get_all(
				STEP,
				filters={"parent": wizard, "parenttype": WIZARD, "parentfield": "steps"},
				fields=["name"],
				order_by="idx asc",
				limit=REGISTER_CAP,
			)
			or []
		]
	except Exception:  # pragma: no cover - a bench with no table yet
		return []


def _clear_fields(step_names: list) -> None:
	"""Delete every Wizard Field hanging off these steps. Never raises."""
	for parent in step_names:
		try:
			for row in (
				frappe.db.get_all(
					FIELD,
					filters={"parent": parent, "parenttype": STEP, "parentfield": "fields"},
					pluck="name",
					limit=FIELD_CAP,
				)
				or []
			):
				frappe.delete_doc(FIELD, row, force=True, ignore_permissions=True)
		except Exception:  # pragma: no cover - reported by the caller, never raised
			continue


def _write_fields(parent: str, fields: list) -> int:
	"""Insert one step's fields as `Wizard Field` DOCUMENTS OF THEIR OWN.

	FRAPPE WRITES ONE LEVEL OF CHILDREN AND THIS IS TWO. `Document.insert` walks
	`get_all_children()`, which reads the table fields off the PARENT's meta, and
	`db_insert` builds its row from `get_valid_dict()`, which has no column for a
	Table field. A `Wizard Field` appended onto a `Wizard Step` row therefore
	reaches no table at all: it is validated, it is never stored, and the next
	read of that wizard answers `fields: []`. That is what shipped, and it is why
	five seeded forms loaded on a handset with nothing on them.

	So each row is inserted directly, with the three columns that make it a child
	of its step set by hand — which is exactly what Frappe would have written if
	it traversed this far. `idx` is set from the spec's order because it IS the
	order the questions are asked in, and a step whose fields came back sorted by
	docname would ask for a signature before the incident.
	"""
	written = 0
	for index, field in enumerate(fields, start=1):
		row = frappe.new_doc(FIELD)
		for column, value in dict(field).items():
			row.set(column, value)
		row.parent = parent
		row.parenttype = STEP
		row.parentfield = "fields"
		row.idx = index
		row.insert(ignore_permissions=True)
		written += 1
	return written


def write_wizard_fields(doc) -> int:
	"""Persist the fields appended onto a saved definition's steps. PUBLIC.

	CALL THIS AFTER EVERY `insert()` OR `save()` OF A WIZARD DEFINITION YOU BUILT
	IN MEMORY, because `doc.insert()` alone does not store them and gives no sign
	that it has not — see `_write_fields`. Anything that appends a field onto a
	step row and stops there produces a wizard the handset reads as "nothing to
	fill", which is the shape five shipped forms had on Tim's site.

	Reads the rows off the DOCUMENT rather than off whatever spec produced it, so
	the step a field belongs to is the step it was appended to and no zip has to
	stay in step with anything. A step with no fields in hand is skipped rather
	than emptied: this function only ever writes, and a caller that means to
	replace a step's questions calls `_clear_fields` first and says so.
	"""
	written = 0
	for row in doc.get("steps") or []:
		parent = str(row.get("name") or "")
		fields = [dict(field) for field in (row.get("fields") or [])]
		if not parent or not fields:
			continue
		written += _write_fields(parent, fields)
	return written


def _repair_fields(key: str, spec: dict) -> int:
	"""Put the fields back on a shipped wizard that was stored without them.

	THE SEEDER WOULD NOT HAVE FIXED THIS ON ITS OWN, and that is the point of the
	pass. Every site that migrated a release before this one has five Wizard
	Definitions with their steps intact and not one field anywhere, and the
	promise `install_wizard_definitions` makes — never overwrite an existing
	wizard — means the next migration would leave them exactly as broken as it
	found them. An operator would have to know to pass `overwrite=True`, which
	would also throw away every edit they had made.

	SO THE REPAIR IS ADDITIVE AND CANNOT LOSE ANYTHING. A step that already has
	one field is left completely alone — an operator adding, removing or
	rewording questions is the whole point of the doctype, and a "repair" that
	restored the shipped set over the top of that would be the reset this
	function exists to avoid. Only a step with NOTHING on it is written to, and a
	step with nothing on it is not a decision anybody made: `WizardDefinition`
	refuses to save a field with no fieldname or no English label, but it has
	never required a step to have any fields, so an empty one is what the lost
	write left behind.

	MATCHED ON `step_key`, not on position. An operator who added their state's
	extra step in the middle has shifted every index after it, and repairing by
	position would put the accident wizard's injury questions on the step that
	asks who saw it.
	"""
	shipped = {
		str(step.get("step_key") or ""): [dict(field) for field in (step.get("fields") or [])]
		for step in spec["steps"]
	}
	written = 0
	try:
		rows = (
			frappe.db.get_all(
				STEP,
				filters={"parent": key, "parenttype": WIZARD, "parentfield": "steps"},
				fields=["name", "step_key"],
				order_by="idx asc",
				limit=REGISTER_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover - a bench with no table yet
		return 0
	for row in rows:
		fields = shipped.get(str(row.get("step_key") or ""))
		if not fields:
			continue
		if fields_of({"name": row.get("name")}):
			continue
		written += _write_fields(str(row.get("name")), fields)
	return written


def install_wizard_definitions(overwrite: bool = False) -> dict:
	"""Seed the shipped wizards. Idempotent, and NEVER raises.

	NEVER OVERWRITES BY DEFAULT, and that is the whole promise of "config not
	code": a flow an operator tuned for their own crew — reworded a question,
	added a step their state requires — being reset by the next `bench migrate`
	is what would make this doctype a lie. `overwrite=True` exists for a
	deliberate reset and is not what migration calls.

	Never raises for the reason `install_compliance_fields` does not: this runs
	from `after_migrate`, and an exception there takes somebody's whole migration
	down.
	"""
	report = {
		"created": [],
		"existing": [],
		"failed": [],
		"overwritten": [],
		# The wizards that HAD their fields put back — see `_repair_fields`. A
		# migration that repairs five is a migration that found five broken, and
		# an operator reading the report should see that rather than "existing".
		"repaired": [],
		"fields_written": 0,
	}
	if not compat.doctype_exists(WIZARD):
		return report

	for spec in SHIPPED_WIZARDS:
		key = spec["wizard_key"]
		try:
			exists = frappe.db.exists(WIZARD, key)
			if exists and not overwrite:
				report["existing"].append(key)
				written = _repair_fields(key, spec)
				if written:
					report["repaired"].append(key)
					report["fields_written"] += written
				continue
			if exists:
				# THE FIELD ROWS ARE NOT THE DEFINITION'S CHILDREN AND WILL NOT GO
				# WITH IT. `delete_doc` takes the `Wizard Step` rows because they are
				# the definition's own table; a `Wizard Field` points at a STEP, which
				# the cascade never looks at. Left alone they would outlive every
				# wizard that ever named them, and an overwrite would grow the table
				# by one wizard every time it ran.
				_clear_fields(_step_names(key))
				frappe.delete_doc(WIZARD, key, force=True, ignore_permissions=True)
				report["overwritten"].append(key)

			doc = frappe.new_doc(WIZARD)
			doc.wizard_key = key
			doc.__newname = key
			for field in (
				"title_en",
				"title_es",
				"description_en",
				"description_es",
				"category",
				"submit_method",
				"icon",
				"required_role",
			):
				if spec.get(field):
					doc.set(field, spec[field])
			doc.enabled = 1
			doc.version = 1
			doc.shipped_default = 1
			for step in spec["steps"]:
				fields = [dict(field) for field in (step.get("fields") or [])]
				row = doc.append("steps", {k: v for k, v in step.items() if k != "fields"})
				# APPENDED FOR `WizardDefinition.validate` AND FOR NOTHING ELSE. The
				# controller walks these looking for a field with no fieldname, two on
				# one step under one name, options that will not parse — checks worth
				# keeping, and they run against the document in memory. They are NOT
				# how the rows get stored: Frappe writes one level of children and a
				# grandchild appended here reaches no table at all. `_write_fields`
				# below is what actually persists them.
				if hasattr(row, "append"):
					for field in fields:
						row.append("fields", dict(field))
				else:  # pragma: no cover - a mapping-shaped row
					row.setdefault("fields", []).extend(dict(field) for field in fields)
			doc.insert(ignore_permissions=True)
			report["fields_written"] += write_wizard_fields(doc)
			report["created"].append(key)
		except Exception as exc:  # pragma: no cover - reported, never raised
			report["failed"].append({"wizard": key, "reason": f"{type(exc).__name__}: {exc}"})
	return report
