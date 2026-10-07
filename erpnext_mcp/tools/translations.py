# SPDX-License-Identifier: MIT
"""The string register: what a worker reads, in the language they read it in.

────────────────────────────────────────────────────────────────────────────
WHY THIS IS NOT FRAPPE'S OWN Translation DOCTYPE
────────────────────────────────────────────────────────────────────────────

Frappe ships a `Translation` doctype and it is the right tool for the job it
does: translating the FRAMEWORK's own interface, keyed by the SOURCE STRING. You
write "Submit" in English and it finds the Spanish for "Submit".

That key is wrong for this app, and the reason is not stylistic. Keyed by source
text, every one of these breaks a translation silently:

  * REWORDING THE ENGLISH. A farm that changes "Bucket rejected — coverage too
    low" to "Bucket not counted — not full enough" has just orphaned the Spanish
    for it. Nothing errors. Spanish-speaking pickers start seeing English.
  * TWO STRINGS THAT ARE THE SAME IN ENGLISH AND NOT IN SPANISH. "Open" as a
    shift status is `abierto`; "Open" as a button is `abrir`. One source key
    cannot hold both, and whichever was written second wins for both.
  * ASKING WHAT IS MISSING. "Which strings have no Spanish?" has no answer when
    the key is the English, because there is no register of keys — only a
    register of translations that happen to exist.

So a `Farm Translation` row is keyed by a STABLE DOTTED KEY the app writes:
`shift.status.open`, `error.shift.already_open`, `task_type.harvest`. The
English is a row like any other. Rewording it is editing a row, and the Spanish
stays attached. `list_translations(missing_only=true)` answers the third
question in one query, which is the question an operator actually has.

THIS APP DOES NOT REPLACE FRAPPE'S. Nothing here touches `tabTranslation`, and
`frappe._()` still does what it always did for Desk labels and for the framework's
own messages. The two registers answer different questions and a site has both.

────────────────────────────────────────────────────────────────────────────
FALL BACK, AND BE LOUD ABOUT IT
────────────────────────────────────────────────────────────────────────────

The same rule `tools/wizards.py` argues at length, applied to every string in
the app. A missing Spanish translation serves the ENGLISH and SAYS SO — never a
blank, never a refusal, never the raw key. The three alternatives are all worse:

  * A BLANK is a screen a worker cannot act on and cannot report.
  * A REFUSAL locks a crew out of a flow over one untranslated sentence.
  * THE RAW KEY (`error.shift.already_open`) is what a system shows when it has
    given up, and it is indistinguishable to the reader from a bug.

Every read path here reports what fell back — `fell_back`, `untranslated`,
`missing` — so the gap is findable from the Desk rather than discoverable by a
worker standing in front of a screen they cannot read.

────────────────────────────────────────────────────────────────────────────
WHOSE LANGUAGE, AND WHY THE HEADER LOSES
────────────────────────────────────────────────────────────────────────────

`Employee.preferred_language` is the authority and `Accept-Language` is the
fallback, in that order and never the other way round. That ordering is a
compliance position, not a preference: OSHA 1910.1200(h) and the Worker
Protection Standard (40 CFR 170.501) require hazard communication "in a manner
the employee can understand", and this app's claim to have done that rests on a
column somebody FILLED IN about a person — not on a device setting. A phone set
to English by whoever handed it over says nothing about who is holding it now.

The header is honoured where the column is empty, because a site that has not
filled the column in yet is better served by the phone's guess than by English,
and because a worker who set their own phone to Spanish has told us something.
`resolve_language` returns the source alongside the code, so a caller can always
tell which of the two answered — and the mobile surface puts that in the
response, so "why is this person seeing English" is answerable.

────────────────────────────────────────────────────────────────────────────
KEYS AS VALUES: THE `tr:` PREFIX
────────────────────────────────────────────────────────────────────────────

`Wizard Definition` already carries `label_en` / `label_es` columns, and those
are right for a string that belongs to exactly one wizard. They are wrong for a
string that appears in nine places — "Photograph required", "Block", "Signature"
— because nine copies drift and the ninth is the one nobody fixed.

So a translatable column whose value is `tr:some.key` is a REFERENCE, resolved
through this register at read time. `tr:` and not a bare key, because a wizard
label that legitimately reads "harvest.notes" must not silently become a lookup;
an escape that can be typed by accident is not an escape. Anything without the
prefix is a literal and behaves exactly as it did before this release, which is
what makes the change safe on a site with wizards an operator has already edited.
"""

from __future__ import annotations

import re

import frappe

from .. import compat
from ..args import as_bool, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

DOCTYPE = "Farm Translation"
EMPLOYEE = "Employee"

#: The languages this app ships strings in. NOT a restriction on what may be
#: asked for — an unknown code falls back to English and says so — but the set
#: the seeder writes and the set the `language` Select column offers.
LANGUAGES = ("en", "es")
DEFAULT_LANGUAGE = "en"

#: The categories the register groups by. Mirrors the Select on the doctype;
#: named here so `update_translation` can refuse a typo with the list rather
#: than writing a row into a group nobody filters for.
CATEGORIES = (
	"Task Types",
	"Wizard Labels",
	"Compliance Forms",
	"Shift Status",
	"Error Messages",
	"Units and Time",
	"Other",
)

#: The escape that turns a stored string into a lookup. See the module docstring.
KEY_PREFIX = "tr:"

#: What a key may contain. Dotted, lowercase, digits and underscores — the shape
#: the whole catalogue below uses. Enforced on write so `list_translations`
#: prefix filtering keeps meaning something: a key with a space in it is a key
#: nobody will ever guess from the Desk.
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$")

#: `{name}` — the only substitution form. Matches the doctype controller's, and
#: deliberately so: the controller refuses a translation whose placeholders
#: disagree with the English, and it can only do that if both halves agree on
#: what a placeholder looks like.
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

REGISTER_CAP = 500

#: How long a resolved string may be. Not a database limit — Small Text holds
#: far more — but a guard on what goes into an error message that ends up in a
#: log line and a phone's alert box.
VALUE_CAP = 2000


def _require() -> None:
	compat.require_doctype(
		DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def available() -> bool:
	"""Whether this site has the register at all. Never raises.

	Every caller inside the app goes through here rather than assuming, because
	the translation layer must never be the reason something else fails: a site
	mid-migrate answers False and gets the English literal it would have got
	before this release existed.
	"""
	try:
		return compat.doctype_exists(DOCTYPE)
	except Exception:  # pragma: no cover - a bench with no doctype table yet
		return False


# ── the shipped catalogue ───────────────────────────────────────────────────
#
# EVERY KEY HERE HAS BOTH LANGUAGES. A shipped key with no Spanish is a promise
# this release does not keep, and it would be invisible: `missing_only` would
# report it beside the operator's own gaps and nobody would know which of the
# two it was. The Spanish is written for a farm workforce in the Pacific
# Northwest — `cubeta` for the picking bucket rather than `balde` or `cubo`, and
# `bin` left as `bin` because that is the word used on the block.

#: Farm task types. The keys mirror `Farm Task.task_type` values.
_TASK_TYPES = {
	"task_type.harvest": ("Harvest", "Cosecha"),
	"task_type.pruning": ("Pruning", "Poda"),
	"task_type.thinning": ("Thinning", "Raleo"),
	"task_type.spraying": ("Spraying", "Aplicación de pesticidas"),
	"task_type.irrigation": ("Irrigation", "Riego"),
	"task_type.planting": ("Planting", "Plantación"),
	"task_type.training": ("Training", "Capacitación"),
	"task_type.scouting": ("Scouting", "Monitoreo de plagas"),
	"task_type.inspection": ("Inspection", "Inspección"),
	"task_type.maintenance": ("Maintenance", "Mantenimiento"),
	"task_type.equipment_check": ("Equipment Check", "Revisión de equipo"),
	"task_type.housing_inspection": ("Housing Inspection", "Inspección de vivienda"),
	"task_type.water_test": ("Water Test", "Prueba de agua"),
	"task_type.frost_protection": ("Frost Protection", "Protección contra heladas"),
	"task_type.bin_moving": ("Bin Moving", "Movimiento de bins"),
	"task_type.other": ("Other", "Otro"),
}

#: Wizard-level labels that appear in more than one wizard. The per-wizard
#: strings stay on `Wizard Definition`'s own columns; these are the ones nine
#: copies of would drift apart.
_WIZARD_LABELS = {
	"wizard.action.next": ("Next", "Siguiente"),
	"wizard.action.back": ("Back", "Atrás"),
	"wizard.action.submit": ("Submit", "Enviar"),
	"wizard.action.cancel": ("Cancel", "Cancelar"),
	"wizard.action.save_draft": ("Save Draft", "Guardar borrador"),
	"wizard.field.photo": ("Photograph", "Fotografía"),
	"wizard.field.photo_help": (
		"Take a photograph. It is the evidence, so frame what you are reporting.",
		"Toma una foto. Es la evidencia, así que encuadra lo que estás reportando.",
	),
	"wizard.field.signature": ("Signature", "Firma"),
	"wizard.field.signature_help": (
		"Sign with your finger. This is your attestation that what you entered is true.",
		"Firma con el dedo. Es tu declaración de que lo que escribiste es cierto.",
	),
	"wizard.field.qr_scan": ("Scan Code", "Escanear código"),
	"wizard.field.audio_note": ("Voice Note", "Nota de voz"),
	"wizard.field.employee_select": ("Worker", "Trabajador"),
	"wizard.field.date": ("Date", "Fecha"),
	"wizard.field.time": ("Time", "Hora"),
	"wizard.field.location": ("Location", "Ubicación"),
	"wizard.field.block": ("Block", "Bloque"),
	"wizard.field.notes": ("Notes", "Notas"),
	"wizard.field.required": ("Required", "Obligatorio"),
	"wizard.field.optional": ("Optional", "Opcional"),
	"wizard.step.of": ("Step {current} of {total}", "Paso {current} de {total}"),
	"wizard.validation.required": (
		"This answer is required.",
		"Esta respuesta es obligatoria.",
	),
	"wizard.validation.too_short": (
		"Please write at least {min} characters.",
		"Escribe al menos {min} caracteres.",
	),
	"wizard.validation.bad_format": (
		"That is not the expected format.",
		"Ese no es el formato esperado.",
	),
}

#: Compliance form labels. The vocabulary an inspector and a worker both have to
#: read, and the half of this catalogue with the most legal weight on it.
_COMPLIANCE_FORMS = {
	"compliance.form.i9": (
		"Employment Eligibility Verification (Form I-9)",
		"Verificación de Elegibilidad de Empleo (Formulario I-9)",
	),
	"compliance.form.w4": (
		"Employee's Withholding Certificate (Form W-4)",
		"Certificado de Retenciones del Empleado (Formulario W-4)",
	),
	"compliance.form.heat_illness": ("Heat Illness Prevention", "Prevención de enfermedades por calor"),
	"compliance.form.pesticide_safety": (
		"Pesticide Safety Training",
		"Capacitación en seguridad de pesticidas",
	),
	"compliance.form.hazard_communication": ("Hazard Communication", "Comunicación de peligros"),
	"compliance.form.field_sanitation": ("Field Sanitation", "Saneamiento en el campo"),
	"compliance.form.housing_inspection": ("Housing Inspection", "Inspección de vivienda"),
	"compliance.form.accident_report": ("Accident Report", "Reporte de accidente"),
	"compliance.form.water_test": ("Water Quality Test", "Prueba de calidad del agua"),
	"compliance.label.employee_signature": ("Employee Signature", "Firma del empleado"),
	"compliance.label.supervisor_signature": ("Supervisor Signature", "Firma del supervisor"),
	"compliance.label.date_signed": ("Date Signed", "Fecha de firma"),
	"compliance.label.acknowledgement": (
		"I have received this training in a language I understand.",
		"He recibido esta capacitación en un idioma que entiendo.",
	),
	"compliance.label.rei_active": (
		"DO NOT ENTER. Restricted-entry interval is in effect until {until}.",
		"NO ENTRAR. El intervalo de entrada restringida está vigente hasta {until}.",
	),
	"compliance.label.rei_cleared": (
		"Restricted-entry interval has cleared. Entry is permitted.",
		"El intervalo de entrada restringida ha terminado. Se permite la entrada.",
	),
	"compliance.label.shade_water_rest": (
		"Shade, water and rest are available. Ask your supervisor.",
		"Hay sombra, agua y descanso disponibles. Pregúntale a tu supervisor.",
	),
	"compliance.label.evidence_required": (
		"This work needs evidence before it can be filed.",
		"Este trabajo necesita evidencia antes de poder registrarse.",
	),
}

#: Shift status messages. What a picker sees on the clock screen.
_SHIFT_STATUS = {
	"shift.status.open": ("Open", "Abierto"),
	"shift.status.closed": ("Closed", "Cerrado"),
	"shift.status.cancelled": ("Cancelled", "Cancelado"),
	"shift.status.on_break": ("On Break", "En descanso"),
	"shift.message.clocked_in": (
		"You are clocked in on {shift} since {since}.",
		"Estás registrado en {shift} desde {since}.",
	),
	"shift.message.clocked_out": (
		"You are clocked out. Your shift ran {hours} hours.",
		"Ya marcaste la salida. Tu turno duró {hours} horas.",
	),
	"shift.message.break_started": ("Your break started at {at}.", "Tu descanso empezó a las {at}."),
	"shift.message.break_ended": ("Your break ended at {at}.", "Tu descanso terminó a las {at}."),
	"shift.message.break_due": (
		"A rest break is due. Tell your supervisor if you have not had one.",
		"Te toca un descanso. Avísale a tu supervisor si no lo has tomado.",
	),
	"shift.message.no_open_shift": (
		"You are not clocked in on any shift.",
		"No estás registrado en ningún turno.",
	),
	"shift.message.awaiting_review": (
		"This shift is waiting for a supervisor to review and sign it.",
		"Este turno espera que un supervisor lo revise y lo firme.",
	),
	"shift.message.buckets_today": (
		"{accepted} accepted, {rejected} rejected today.",
		"{accepted} aceptadas, {rejected} rechazadas hoy.",
	),
}

#: Error messages a worker on a phone can actually hit. THE KEYS ARE THE
#: CONTRACT: the handset may map any of them to its own wording, and the value
#: here is what it shows when it does not recognise one. Keys are stable across
#: releases — renaming one orphans every client that switched on it.
_ERRORS = {
	"error.unspecified": (
		"Something went wrong and this app could not say what. Tell your supervisor.",
		"Algo salió mal y la aplicación no pudo decir qué. Avísale a tu supervisor.",
	),
	"error.mobile.disabled": (
		"The field app is switched off on this site. Your operator turned it off.",
		"La aplicación de campo está desactivada en este sitio. La desactivó tu operador.",
	),
	# THE ONE KEY EVERY ENROLMENT FAILURE GETS. There is deliberately no separate
	# key for "wrong role" or "no grant": `api/guard._not_enrolled` answers all
	# three checks with one message so an unauthorised caller cannot probe which
	# of them it failed, and a translation key that distinguished them would hand
	# back the oracle the English message is written to withhold.
	"error.mobile.no_grant": (
		"This login is not enrolled for the field app. Ask your operator to enrol you.",
		"Esta cuenta no está inscrita en la aplicación de campo. Pídele a tu operador que te inscriba.",
	),
	# v0.268.0. The company switcher named a company this login is not in. The phone clears its selection and
	# reloads; the sentence is what a person sees if it cannot.
	"error.mobile.company_not_member": (
		"That company is not one of yours. Choose one of your companies in the company switcher.",
		"Esa empresa no es una de las tuyas. Elige una de tus empresas en el selector de empresa.",
	),
	"error.mobile.rate_limited": (
		"Too many requests. Wait a moment and try again.",
		"Demasiadas solicitudes. Espera un momento e inténtalo de nuevo.",
	),
	"error.mobile.no_employee": (
		"This login is not linked to an employee record.",
		"Esta cuenta no está vinculada a un registro de empleado.",
	),
	"error.permission.denied": (
		"You are not permitted to do that.",
		"No tienes permiso para hacer eso.",
	),
	"error.shift.already_open": (
		"You already have an open shift. Close it before starting another.",
		"Ya tienes un turno abierto. Ciérralo antes de empezar otro.",
	),
	"error.shift.not_found": ("That shift was not found.", "No se encontró ese turno."),
	"error.shift.already_closed": (
		"That shift is already closed.",
		"Ese turno ya está cerrado.",
	),
	"error.shift.ends_before_start": (
		"That would end the shift before it began.",
		"Eso terminaría el turno antes de que comenzara.",
	),
	"error.task.not_assigned": (
		"That job is not assigned to you.",
		"Ese trabajo no está asignado a ti.",
	),
	"error.task.already_done": ("That job is already filed.", "Ese trabajo ya fue registrado."),
	"error.task.evidence_missing": (
		"This job needs {what} before it can be filed.",
		"Este trabajo necesita {what} antes de poder registrarse.",
	),
	"error.task.too_many_claims": (
		"You are holding as many jobs as you can at once. Finish one first.",
		"Ya tienes el máximo de trabajos a la vez. Termina uno primero.",
	),
	"error.badge.unknown": (
		"That badge is not registered to anybody.",
		"Esa credencial no está registrada a nombre de nadie.",
	),
	"error.badge.inactive": (
		"That badge belongs to somebody who is not active.",
		"Esa credencial pertenece a alguien que no está activo.",
	),
	"error.bucket.duplicate": (
		"That capture was already received.",
		"Esa captura ya fue recibida.",
	),
	"error.offline.queued": (
		"No signal. This is saved on the phone and will send itself when there is signal.",
		"Sin señal. Esto se guardó en el teléfono y se enviará solo cuando haya señal.",
	),
	"error.upload.failed": (
		"The file did not finish sending. Try again where there is better signal.",
		"El archivo no terminó de enviarse. Inténtalo de nuevo donde haya mejor señal.",
	),
	# v0.254.0. Keys raise sites already send (offline create, asset tags, business cards, punch review)
	# that had no row, so they reached a Spanish phone in English.
	"error.asset.bad_tag": (
		"This record's tag is not valid. Update the app and try again.",
		"La etiqueta de este registro no es válida. Actualiza la app e inténtalo de nuevo.",
	),
	"error.asset.name_taken": (
		"That name is already used by another tag. Rename it on the phone and send it again.",
		"Ese nombre ya lo usa otra etiqueta. Cámbiale el nombre en el teléfono y envíalo de nuevo.",
	),
	"error.asset.tag_in_use": ("That tag is already on another record.", "Esa etiqueta ya está en otro registro."),
	"error.asset.unknown_type": (
		"That asset type is not on the farm's list.",
		"Ese tipo de activo no está en la lista de la granja.",
	),
	"error.contact.duplicate": (
		"This card matches a contact already saved. Update that contact or save it as new.",
		"Esta tarjeta coincide con un contacto ya guardado. Actualiza ese contacto o guárdalo como nuevo.",
	),
	"error.punch.forbidden": (
		"Reviewing punch times is for a Farm Manager or HR.",
		"Revisar las marcas de tiempo es para un gerente o Recursos Humanos.",
	),
	"error.punch.not_found": ("That punch was not found.", "No se encontró esa marca de tiempo."),
	"error.validation.failed": (
		"Some answers need fixing before this can be sent.",
		"Algunas respuestas necesitan corregirse antes de enviar esto.",
	),
}

#: Units and the handful of time words that appear beside a number.
_UNITS = {
	"unit.bucket": ("bucket", "cubeta"),
	"unit.buckets": ("buckets", "cubetas"),
	"unit.bin": ("bin", "bin"),
	"unit.bins": ("bins", "bins"),
	"unit.pound": ("pound", "libra"),
	"unit.pounds": ("pounds", "libras"),
	"unit.hour": ("hour", "hora"),
	"unit.hours": ("hours", "horas"),
	"unit.minute": ("minute", "minuto"),
	"unit.minutes": ("minutes", "minutos"),
	"unit.acre": ("acre", "acre"),
	"unit.acres": ("acres", "acres"),
	"time.today": ("today", "hoy"),
	"time.yesterday": ("yesterday", "ayer"),
	"time.now": ("now", "ahora"),
}

#: The whole shipped catalogue, as `{key: (category, english, spanish)}`. Built
#: from the five groups above so a key can only ever be in one category — a
#: string filed under two is a string a translator gets handed twice.
SHIPPED: dict = {}
for _category, _group in (
	("Task Types", _TASK_TYPES),
	("Wizard Labels", _WIZARD_LABELS),
	("Compliance Forms", _COMPLIANCE_FORMS),
	("Shift Status", _SHIFT_STATUS),
	("Error Messages", _ERRORS),
	("Units and Time", _UNITS),
):
	for _key, (_en, _es) in _group.items():
		SHIPPED[_key] = (_category, _en, _es)

# v0.254.0. The refusals a worker hits on the phone, matched from their English (`error_phrases`), so
# the hundreds of raise sites that carry no key still reach a Spanish speaker in their language.
from .. import error_phrases as _error_phrases  # noqa: E402

for _key, _en, _es in _error_phrases.catalogue():
	SHIPPED[_key] = ("Error Messages", _en, _es)


# ── resolution ──────────────────────────────────────────────────────────────
def normalize_language(language: str) -> str:
	"""An ISO code from whatever a caller sent. `"es-MX"` → `"es"`, `""` → `""`.

	The region is dropped rather than refused. A handset that says `es-MX` and a
	handset that says `es-419` are both asking for the Spanish this site has, and
	a register that answered "no such language" to either would serve English to
	a Spanish speaker over a subtag nobody in this app has an opinion about.
	"""
	code = str(language or "").strip().lower().replace("_", "-")
	if not code:
		return ""
	return code.split("-", 1)[0][:12]


def accept_language(header: str) -> str:
	"""The best language out of an `Accept-Language` header, or `""`.

	Parses the q-value form (`es-MX,es;q=0.9,en;q=0.8`) and takes the highest
	weighted code THIS SITE HAS A CATALOGUE FOR. Preferring a known language over
	a higher-weighted unknown one is the whole job: a phone whose first choice is
	French and whose second is Spanish should get Spanish, not English.

	Never raises and never refuses. A header this cannot parse is a header that
	says nothing, and saying nothing is a valid thing for a client to do.
	"""
	raw = str(header or "").strip()
	if not raw:
		return ""
	scored: list = []
	for index, chunk in enumerate(raw.split(",")):
		part = chunk.strip()
		if not part:
			continue
		bits = part.split(";")
		code = normalize_language(bits[0])
		if not code or code == "*":
			continue
		weight = 1.0
		for extra in bits[1:]:
			extra = extra.strip()
			if extra.startswith("q="):
				try:
					weight = float(extra[2:])
				except ValueError:
					# An unparseable q-value is not "definitely wanted". RFC 9110
					# gives q=0 the meaning "not acceptable", and treating garbage
					# as a full-weight preference would let a malformed header
					# override a language the client asked for properly further
					# along the list.
					weight = 0.0
		if weight <= 0:
			continue
		# `index` breaks ties in the order the client wrote them, which is the
		# order the spec says to read equal weights in.
		scored.append((-weight, index, code))
	for _weight, _index, code in sorted(scored):
		if code in LANGUAGES:
			return code
	return ""


def preferred_language(user: str = "", employee: str = "") -> str:
	"""The language this person reads off their Employee record, or `""`.

	THE SAME COLUMN `tools/wizards.preferred_language` READS, and deliberately a
	second implementation of nothing: wizards.py owns that read, this delegates,
	and the two cannot come to different answers about the same person.
	"""
	from . import wizards

	try:
		return wizards.preferred_language(user=user, employee=employee)
	except Exception:  # pragma: no cover - a site without the column
		return ""


def resolve_language(
	language: str = "",
	user: str = "",
	employee: str = "",
	header: str = "",
) -> tuple:
	"""`(code, source)` — which language to answer in, and what decided it.

	The order is the compliance position from the module docstring, and the
	`source` is returned rather than discarded so a caller can put it in the
	response: "why is this person seeing English" has to be answerable without
	reading this function.

	  1. `explicit`   — the caller named one. An operator previewing Spanish, or
	                    a handset the worker just switched by hand.
	  2. `employee`   — `Employee.preferred_language`. THE AUTHORITY.
	  3. `header`     — `Accept-Language`. Only where the column is empty.
	  4. `default`    — English, because something has to be shown.
	"""
	explicit = normalize_language(language)
	if explicit:
		return explicit, "explicit"
	stated = normalize_language(preferred_language(user=user, employee=employee))
	if stated:
		return stated, "employee"
	from_header = accept_language(header)
	if from_header:
		return from_header, "header"
	return DEFAULT_LANGUAGE, "default"


def _row(key: str, language: str) -> str | None:
	"""One enabled row's value, or None. Never raises."""
	if not (key and language):
		return None
	try:
		row = frappe.db.get_value(
			DOCTYPE,
			{"translation_key": key, "language": language, "enabled": 1},
			["value"],
			as_dict=True,
		)
	except Exception:  # pragma: no cover - a site mid-migrate
		return None
	if not row:
		return None
	value = str(row.get("value") or "")
	return value or None


def resolve(key: str, language: str = DEFAULT_LANGUAGE) -> tuple:
	"""`(value, fell_back, found)` for one key.

	`found` False means this site has no row for the key in ANY language, which
	is a different problem from a missing translation and needs a different fix:
	the first is a key the app asks for and nobody seeded, the second is a
	translator's to-do. Conflating them is how a typo'd key gets filed as a
	translation gap and never looked at again.
	"""
	key = str(key or "").strip()
	if not key or not available():
		return "", False, False

	wanted = normalize_language(language) or DEFAULT_LANGUAGE
	direct = _row(key, wanted)
	if direct is not None:
		return direct[:VALUE_CAP], False, True

	english = _row(key, DEFAULT_LANGUAGE)
	if english is not None:
		# Falling back is only a fall-back when a different language was asked
		# for. English asked for and English served is a hit, not a miss, and
		# reporting it as one would fill `untranslated` with noise.
		return english[:VALUE_CAP], wanted != DEFAULT_LANGUAGE, True

	# Some site has the key in Spanish and not English. Rare, and serving it is
	# better than serving nothing — but it IS a fall-back, whatever the code.
	for other in LANGUAGES:
		value = _row(key, other)
		if value is not None:
			return value[:VALUE_CAP], True, True

	return "", False, False


def translate(key: str, language: str = DEFAULT_LANGUAGE, default: str = "", **fill) -> str:
	"""One string, rendered. Falls back to `default`, then to the key itself.

	THE CONVENIENCE THE REST OF THE APP CALLS. Never raises — a translation layer
	that could fail would become the reason a bucket sync failed, and the whole
	design here is that a language problem degrades to English rather than to an
	error. A key with no row and no `default` comes back as the key, which is
	ugly on purpose: it is visible in a screenshot and greppable in a log.
	"""
	value, _fell_back, found = resolve(key, language)
	if not found:
		value = default or key
	return render(value, **fill)


def render(value: str, **fill) -> str:
	"""Fill `{placeholders}`, tolerating the ones the caller did not supply.

	`str.format` raises KeyError on a placeholder with no value, and this is
	called on the error path — so a translation naming `{until}` where the caller
	passed none would turn a handled refusal into an unhandled crash, at the
	worst possible moment. Missing names are left as they were written, which is
	legible ("until {until}") and reports itself.
	"""
	text = str(value or "")
	if not fill or "{" not in text:
		return text

	def substitute(match):
		name = match.group(1)
		if name not in fill:
			return match.group(0)
		return str(fill[name])

	return PLACEHOLDER.sub(substitute, text)


def bundle(language: str = DEFAULT_LANGUAGE, category: str = "", prefix: str = "") -> dict:
	"""`{key: value}` for a whole group, one query, resolved with fall-back.

	What a handset pulls once at login instead of asking for one string at a
	time. The English is fetched in the same pass, so a bundle is complete even
	where the Spanish is not — a client that had to detect a missing key and go
	back for the English would show blanks for the length of that round trip.
	"""
	if not available():
		return {}
	wanted = normalize_language(language) or DEFAULT_LANGUAGE

	filters: dict = {"enabled": 1}
	if wanted == DEFAULT_LANGUAGE:
		filters["language"] = DEFAULT_LANGUAGE
	else:
		filters["language"] = ["in", [wanted, DEFAULT_LANGUAGE]]
	if category:
		filters["category"] = category
	if prefix:
		filters["translation_key"] = ["like", f"{prefix}%"]

	try:
		rows = (
			frappe.db.get_all(
				DOCTYPE,
				filters=filters,
				fields=["translation_key", "language", "value"],
				limit=REGISTER_CAP * 4,
			)
			or []
		)
	except Exception:  # pragma: no cover - a site mid-migrate
		return {}

	english: dict = {}
	native: dict = {}
	for row in rows:
		target = native if str(row.get("language")) == wanted else english
		target[str(row.get("translation_key"))] = str(row.get("value") or "")
	out = dict(english)
	out.update(native)
	return out


# ── the `tr:` escape, for Wizard Definition and anything else with columns ──
def is_reference(value) -> bool:
	"""Is this stored string a KEY rather than a literal? See the module docstring."""
	return str(value or "").strip().startswith(KEY_PREFIX)


def reference_key(value) -> str:
	"""The key out of a `tr:some.key` value, stripped. `""` if it is not one."""
	text = str(value or "").strip()
	if not text.startswith(KEY_PREFIX):
		return ""
	return text[len(KEY_PREFIX) :].strip()


def dereference(value, language: str, missing: list | None = None, where: str = "") -> str:
	"""Resolve a stored string that may be a `tr:` reference. Never raises.

	A literal comes back untouched, which is what makes this safe to put in front
	of every translatable column in the app: a site whose wizards predate this
	release behaves exactly as it did. A reference resolves, and a reference that
	fell back to English appends to `missing` — the same channel
	`get_wizard_definition` already reports `untranslated` through, so one gap
	looks the same wherever it came from.

	A reference to a key NOBODY HAS SEEDED comes back as the key text without the
	prefix. Not blank: a wizard step whose label vanished is unreportable by the
	worker looking at it, and `harvest.notes` at least tells whoever is sent the
	screenshot exactly which row to go and write.
	"""
	key = reference_key(value)
	if not key:
		return str(value or "")
	resolved, fell_back, found = resolve(key, language)
	if not found:
		if missing is not None:
			missing.append({"where": where or key, "key": key, "language": language, "reason": "no such key"})
		return key
	if fell_back and missing is not None:
		missing.append({"where": where or key, "key": key, "language": language})
	return resolved


# ── 1. list_translations ────────────────────────────────────────────────────
def list_translations(args: dict) -> ToolResult:
	"""The register, filtered the four ways somebody asks about strings."""
	_require()
	# NOT VALIDATED AGAINST `LANGUAGES`. A site that added a third language has
	# rows this app does not know about, and refusing to list them would make
	# the doctype's own extensibility unusable through the tool that reads it.
	language = normalize_language(as_str(args, "language"))
	missing_only = as_bool(args, "missing_only", False)

	filters: dict = {}
	if language:
		# `missing_only` ASKS FOR THE ENGLISH, and that is not a quirk of the
		# implementation — it is the only useful answer. A key with no Spanish
		# row has no Spanish row to return, so filtering the Spanish rows by
		# "keys with no Spanish row" is guaranteed to be empty. What a translator
		# needs handed to them is the ENGLISH they are being asked to translate.
		filters["language"] = (
			DEFAULT_LANGUAGE if (missing_only and language != DEFAULT_LANGUAGE) else language
		)
	category = as_str(args, "category")
	if category:
		filters["category"] = category
	prefix = as_str(args, "key_prefix")
	if prefix:
		filters["translation_key"] = ["like", f"{prefix}%"]
	if not as_bool(args, "include_disabled", False):
		filters["enabled"] = 1

	limit = min(as_limit(args), REGISTER_CAP)
	rows = (
		frappe.db.get_all(
			DOCTYPE,
			filters=filters,
			fields=compat.existing_fields(
				DOCTYPE,
				(
					"name",
					"translation_key",
					"language",
					"value",
					"category",
					"enabled",
					"shipped_default",
					"operator_edited",
					"notes",
				),
			),
			order_by="translation_key asc, language asc",
			limit=limit,
		)
		or []
	)

	described = [_describe(row) for row in rows]

	# THE GAP IS THE ANSWER MOST CALLERS WANT. Which keys this site has in
	# English and not in the language asked for — one more query rather than a
	# join, because the register is small and a LEFT JOIN through frappe.db.get_all
	# is not expressible without raw SQL this app would then have to own.
	missing: list = []
	if language and language != DEFAULT_LANGUAGE:
		missing = _missing_keys(language, category, prefix)
		if missing_only:
			described = [row for row in described if row["translation_key"] in set(missing)]

	data = {
		"language": language or None,
		"category": category or None,
		"key_prefix": prefix or None,
		"count": len(described),
		"translations": described,
		"languages_shipped": list(LANGUAGES),
		"categories": list(CATEGORIES),
	}
	if missing_only and language and language != DEFAULT_LANGUAGE:
		data["rows_are_in"] = DEFAULT_LANGUAGE
		data["missing_only_note"] = (
			f"These rows are the {DEFAULT_LANGUAGE} — the strings that need a {language} "
			f"translation written. There is by definition no {language} row to return for a key "
			f"that has none, so returning the source is the only useful answer. "
			f"update_translation(key=..., language='{language}', value=...) is how each is filled in."
		)
	if language and language != DEFAULT_LANGUAGE:
		data["missing_count"] = len(missing)
		data["missing_keys"] = missing[:REGISTER_CAP]
		if missing:
			data["translation_note"] = (
				f"{len(missing)} key(s) exist in {DEFAULT_LANGUAGE} and not in {language}. Each one "
				f"serves ENGLISH to a {language} reader and says so rather than showing a blank — "
				"which is the right failure, and still a failure. update_translation is how they "
				"get filled in."
			)
	if len(described) >= limit:
		data["truncated"] = (
			f"{limit} row(s) returned, which is this tool's cap. Narrow with category or "
			"key_prefix rather than raising the limit — the register is meant to be read a "
			"group at a time."
		)

	return ToolResult(
		data=data,
		summary=(
			f"{len(described)} translation(s)"
			+ (f" in {language}" if language else "")
			+ (f", {len(missing)} key(s) untranslated" if missing else "")
		),
	)


def _missing_keys(language: str, category: str = "", prefix: str = "") -> list:
	"""Keys with an enabled English row and no enabled row in `language`."""
	base: dict = {"language": DEFAULT_LANGUAGE, "enabled": 1}
	if category:
		base["category"] = category
	if prefix:
		base["translation_key"] = ["like", f"{prefix}%"]
	try:
		english = set(frappe.db.get_all(DOCTYPE, filters=base, pluck="translation_key") or [])
		theirs = dict(base)
		theirs["language"] = language
		translated = set(frappe.db.get_all(DOCTYPE, filters=theirs, pluck="translation_key") or [])
	except Exception:  # pragma: no cover - a site mid-migrate
		return []
	return sorted(str(key) for key in english - translated)


def _describe(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"translation_key": str(row.get("translation_key") or ""),
		"language": str(row.get("language") or ""),
		"value": str(row.get("value") or ""),
		"category": row.get("category") or None,
		"enabled": compat.checked(row.get("enabled")),
		"shipped_default": compat.checked(row.get("shipped_default")),
		"operator_edited": compat.checked(row.get("operator_edited")),
		"notes": row.get("notes") or None,
		"placeholders": sorted(set(PLACEHOLDER.findall(str(row.get("value") or "")))),
	}


# ── 2. get_translation ──────────────────────────────────────────────────────
def get_translation(args: dict) -> ToolResult:
	"""One key, resolved — with what fell back and what every language says."""
	_require()
	key = as_str(args, "key", required=True) or as_str(args, "translation_key")
	language, source = resolve_language(
		language=as_str(args, "language"),
		user=as_str(args, "user"),
		employee=as_str(args, "employee"),
	)

	value, fell_back, found = resolve(key, language)

	rows = (
		frappe.db.get_all(
			DOCTYPE,
			filters={"translation_key": key},
			fields=compat.existing_fields(
				DOCTYPE,
				(
					"name",
					"translation_key",
					"language",
					"value",
					"category",
					"enabled",
					"shipped_default",
					"operator_edited",
					"notes",
				),
			),
			order_by="language asc",
			limit=len(LANGUAGES) * 4,
		)
		or []
	)

	if not rows:
		raise ToolError(
			f"no translation key {key!r} on this site, in any language. That is a MISSING KEY "
			f"rather than a missing translation, and the two need different fixes: a key the app "
			f"asks for and nobody seeded is a bug or an un-run migrate, while a key with English "
			f"and no Spanish is a translator's to-do. list_translations(key_prefix=...) shows what "
			f"this site does have; update_translation adds one."
		)

	data = {
		"translation_key": key,
		"language": language,
		"language_source": source,
		"value": value,
		"rendered_from": DEFAULT_LANGUAGE if fell_back else language,
		"fell_back": fell_back,
		"found": found,
		"placeholders": sorted(set(PLACEHOLDER.findall(value))),
		"all_languages": {str(row.get("language")): str(row.get("value") or "") for row in rows},
		"rows": [_describe(row) for row in rows],
	}
	if fell_back:
		data["translation_note"] = (
			f"There is no {language} for {key!r} on this site, so this is the {DEFAULT_LANGUAGE}. "
			f"A worker who reads {language} will see English here — which is better than a blank "
			"and worse than a translation. update_translation fills it in."
		)
	return ToolResult(
		data=data,
		summary=f"{key} [{language}]: {value[:80]}"
		+ ("" if not fell_back else f" (fell back to {DEFAULT_LANGUAGE})"),
	)


# ── 3. update_translation ───────────────────────────────────────────────────
def update_translation(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Write or correct one string in one language."""
	_require()
	key = as_str(args, "key", required=True) or as_str(args, "translation_key")
	language = normalize_language(as_str(args, "language", required=True))
	value = as_str(args, "value", required=True)

	if not KEY_RE.match(key):
		raise ToolError(
			f"{key!r} is not a translation key. Keys are dotted, lowercase, and have at least two "
			"parts — 'shift.status.open', 'error.task.already_done' — because the prefix IS the "
			"grouping: list_translations(key_prefix='error.') is how somebody gets handed the "
			"error catalogue to translate. Nothing was written."
		)
	if not language:
		raise ToolError("language is required — an ISO code such as 'en' or 'es'.")

	category = as_str(args, "category")
	if category and category not in CATEGORIES:
		raise ToolError(
			f"{category!r} is not a category on this register. The categories are: "
			f"{', '.join(CATEGORIES)}. Nothing was written."
		)

	# THE PLACEHOLDER CHECK RUNS HERE AS WELL AS IN THE CONTROLLER, and the
	# duplication is deliberate: the controller's message is a Desk dialog and
	# this one is what a model reads and corrects itself from. Catching it here
	# also means nothing is written before the refusal, which the controller
	# cannot promise on a save that has already begun.
	if language != DEFAULT_LANGUAGE:
		english = _row(key, DEFAULT_LANGUAGE)
		if english:
			wanted = set(PLACEHOLDER.findall(english))
			got = set(PLACEHOLDER.findall(value))
			if wanted != got:
				raise ToolError(
					f"the {DEFAULT_LANGUAGE} for {key!r} uses "
					f"{_braced(sorted(wanted)) or '<no placeholders>'} and "
					f"this {language} uses "
					f"{_braced(sorted(got)) or '<no placeholders>'}. The "
					"caller fills placeholders BY NAME, so a mismatch is not a wording difference — "
					"it prints a literal brace to a worker, or silently drops the value they were "
					f"meant to act on. The {DEFAULT_LANGUAGE} reads: {english!r}. Nothing was written."
				)

	docname = f"{key}::{language}"
	existed = bool(frappe.db.exists(DOCTYPE, docname))
	if existed:
		doc = frappe.get_doc(DOCTYPE, docname)
		before = str(doc.value or "")
		doc.value = value
		if category:
			doc.category = category
		if as_str(args, "notes"):
			doc.notes = as_str(args, "notes")
		enabled = as_bool(args, "enabled", None)
		if enabled is not None:
			doc.enabled = 1 if enabled else 0
		# THE FLAG THE SEEDER READS. Once somebody has edited a string on
		# purpose, `install_translations` leaves it alone forever — see that
		# function on why a migration that restored the shipped wording every
		# upgrade would make this whole register decorative.
		doc.operator_edited = 1
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		action = "updated"
	else:
		doc = frappe.new_doc(DOCTYPE)
		doc.translation_key = key
		doc.language = language
		doc.value = value
		doc.category = category or _shipped_category(key) or "Other"
		doc.notes = as_str(args, "notes") or None
		doc.enabled = 1 if as_bool(args, "enabled", True) else 0
		doc.shipped_default = 0
		doc.operator_edited = 1
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		before = ""
		action = "created"

	data = {
		"name": doc.name,
		"translation_key": key,
		"language": language,
		"value": value,
		"previous_value": before or None,
		"category": doc.category,
		"action": action,
		"operator_edited": True,
		"placeholders": sorted(set(PLACEHOLDER.findall(value))),
		"seeder_note": (
			"This row is now marked operator_edited, so install_translations will never overwrite "
			"it on a later migrate. That is the point of the flag: a farm that reworded a phrase "
			"its crew kept misreading keeps the rewording."
		),
	}
	if language == DEFAULT_LANGUAGE:
		gaps = [code for code in LANGUAGES if code != DEFAULT_LANGUAGE and _row(key, code) is None]
		if gaps:
			data["untranslated_in"] = gaps
			data["translation_note"] = (
				f"{key!r} now has {DEFAULT_LANGUAGE} and no {', '.join(gaps)}. A worker who reads "
				f"{gaps[0]} will be served this English and told it fell back."
			)

	return ToolResult(
		data=data,
		summary=f"{action} {key} [{language}]: {value[:60]}",
		docstatus_delta=f"Farm Translation {doc.name} {action}",
	)


def _braced(names) -> str:
	"""`['block', 'until']` → `"{block}, {until}"`, for a message about braces."""
	return ", ".join("{" + str(name) + "}" for name in names)


def _shipped_category(key: str) -> str:
	"""The category this app files a shipped key under, or `""` for a new one."""
	entry = SHIPPED.get(key)
	return entry[0] if entry else ""


# ── the seeder ──────────────────────────────────────────────────────────────
def install_translations() -> dict:
	"""Write the shipped catalogue. NEVER overwrites a row somebody has edited.

	Runs on install AND after every migrate, so a site upgrading from any earlier
	version gets the strings on its next migrate rather than needing a patch, and
	so a key ADDED in a later release reaches sites that already have the rest.

	THE NON-OVERWRITE RULE HAS TWO HALVES AND BOTH MATTER:

	  * A row flagged `operator_edited` is never touched again. A farm whose crew
	    kept misreading a shipped phrase reworded it; putting the shipped wording
	    back every upgrade would make the register decorative.
	  * A row that is NOT flagged is brought up to date. That is what lets a
	    release fix a bad Spanish translation on every site that never edited it —
	    without which a shipped mistranslation would be permanent everywhere.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench. A key that cannot be written is reported and
	the rest of the catalogue still lands.
	"""
	report: dict = {"created": [], "updated": [], "left_alone": [], "failed": []}
	if not available():
		report["failed"].append({"key": "*", "reason": f"{DOCTYPE} does not exist on this site"})
		return report

	for key, (category, english, spanish) in sorted(SHIPPED.items()):
		for language, value in (("en", english), ("es", spanish)):
			docname = f"{key}::{language}"
			try:
				existing = frappe.db.get_value(DOCTYPE, docname, ["value", "operator_edited"], as_dict=True)
				if existing:
					if compat.checked(existing.get("operator_edited")):
						report["left_alone"].append(docname)
						continue
					if str(existing.get("value") or "") == value:
						continue
					doc = frappe.get_doc(DOCTYPE, docname)
					doc.value = value
					doc.category = category
					doc.shipped_default = 1
					doc.flags.ignore_permissions = True
					doc.save(ignore_permissions=True)
					report["updated"].append(docname)
					continue

				doc = frappe.new_doc(DOCTYPE)
				doc.translation_key = key
				doc.language = language
				doc.value = value
				doc.category = category
				doc.enabled = 1
				doc.shipped_default = 1
				doc.operator_edited = 0
				doc.flags.ignore_permissions = True
				doc.insert(ignore_permissions=True)
				report["created"].append(docname)
			except Exception as exc:  # pragma: no cover - one bad row must not stop the rest
				report["failed"].append({"key": docname, "reason": f"{type(exc).__name__}: {exc}"})

	return report
