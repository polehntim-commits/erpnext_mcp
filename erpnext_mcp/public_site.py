# SPDX-License-Identifier: MIT
"""A public farm website on the ERPNext site itself — configured, not coded. v0.335.0.

Tim: "All of that and via ERPNext built in website", and "remember this is an app
that others can use so it needs to be configurable to other systems."

WHAT IT IS. A small bilingual, mobile-first site — about the farm, jobs, crew
information, report a problem, visitor check-in and check-out — served by the
Frappe website router (a `page_renderer`) under ONE configurable path prefix, and
nothing else. Every word, page, form field, category, routing rule, colour, logo
and QR source tag is DATA in the shared config lifecycle (`Farm Config Version`,
kind "Public Site"): drafted, previewed, published and rolled back over MCP or in
the Desk. Nothing about any one farm is in this file — no names, places, phone
numbers, crops or hostnames. A fresh install gets neutral wording to edit.

THE KEYS (one versioned body each):

  * `site` — the public company (any Company; every other company is private and
    never appears), brand (name, tagline, logo, colours), languages, modules on,
    route prefix, base public URL, contact block, QR source tags, upload cap,
    rate limits, and whether `/` should point here;
  * `page_<slug>` — a page: title, order, menu, and blocks of EN/ES text, lists,
    images and contact details. `home` is the landing page; `crew` holds crew
    information entries;
  * `form_report` / `form_visit` / `form_apply` — the three guest forms: their
    fields (added, removed, reordered as data), categories, wording, the visitor
    acknowledgement, and the routing of what is submitted (company, task type,
    who is told).

WHY NOT FRAPPE WEB FORMS. A guest Web Form inserts into its doctype with no
honeypot and no time trap, and a photo field needs the site-wide "allow guests to
upload files" setting, whose upload endpoint has no rate limit. The forms here are
served on the same website by this module instead: one handler, rate-limited per
visitor and per hour, a hidden honeypot, a signed time token, and an image-only
upload with a size cap, read into memory and stored PRIVATE. Guests get no read
access to any doctype: pages are rendered server-side from published config and
from the public company's open Job Openings, and an image is served only when a
published page names it.

GO-LIVE IS A PERSON'S. Nothing is served until `public_site_enabled` is ticked in
ERPNext MCP Settings (the Desk; no MCP tool sets it), whatever is published.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import html
import json
import re
import time

import frappe

from . import compat

KIND = "Public Site"
SLUG = "public_site"
SITE_KEY = "site"
FORMS = ("report", "visit", "apply")
MODULES = ("about", "jobs", "crew", "report", "visit")
LANGS = ("en", "es")
ENTRY = "Public Site Entry"
JOB_OPENING = "Job Opening"
JOB_APPLICANT = "Job Applicant"
FIELD_TYPES = ("text", "textarea", "select", "phone", "email", "check", "image", "date")
BLOCK_TYPES = ("heading", "text", "list", "image", "contact", "callout")
PAGE_MODULES = ("about", "crew", "custom")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
SRC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
#: Prefixes the site may not take: they are Frappe's, the Desk's, or this app's API.
RESERVED_PREFIXES = (
	"app",
	"api",
	"assets",
	"files",
	"private",
	"desk",
	"login",
	"logout",
	"farmops",
	"website",
	"update-password",
	"me",
	"list",
	"print",
	"printview",
	"socket.io",
)
#: Image types accepted from a guest, by magic bytes — never by the name or the header the browser sent.
IMAGE_MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"RIFF", "webp"))
#: Form-field names the form itself uses (token, honeypot, language, source, acknowledgement).
RESERVED_FIELDS = ("_token", "website", "lang", "src", "acknowledged", "csrf_token", "code", "u")
#: Job Applicant columns this app adds (`ensure_applicant_fields`).
APPLICANT_FIELDS = ("public_answers", "preferred_language", "public_source")


class SiteError(ValueError):
	"""A refusal a tool turns into a ToolError sentence."""


# ── text ─────────────────────────────────────────────────────────────────────


def t(value, lang: str = "en") -> str:
	"""The `lang` text of a `{en, es}` value (or a plain string), falling back to English."""
	if isinstance(value, dict):
		return str(value.get(lang) or value.get("en") or "")
	return str(value or "")


def bi(en: str, es: str = "") -> dict:
	return {"en": en, "es": es}


def esc(text) -> str:
	return html.escape(str(text or ""), quote=True)


# ── neutral defaults (what a fresh install starts from; every word is editable) ──


def default_site(company: str = "", brand: str = "") -> dict:
	return {
		"schema_version": 1,
		"key": SITE_KEY,
		"company": company,
		"brand": {
			"name": brand or company or "Our Farm",
			"tagline": bi("A family farm.", "Una granja familiar."),
			"logo": "",
			"primary_color": "#2f6b3a",
			"accent_color": "#b8862b",
		},
		"languages": ["en", "es"],
		"default_language": "en",
		"route_prefix": "farm",
		"base_url": "",
		"modules": {module: True for module in MODULES},
		"contact": {"phone": "", "email": "", "address": bi("", "")},
		"qr_sources": [],
		"set_home_page": False,
		"max_upload_mb": 5,
		"submissions_per_visitor_per_hour": 5,
		"submissions_per_hour": 120,
		"min_seconds_to_submit": 4,
	}


def default_pages() -> dict:
	page = lambda slug, module, order, title, blocks, nav=True: {  # noqa: E731
		"schema_version": 1,
		"key": f"page_{slug.replace('-', '_')}",
		"slug": slug,
		"module": module,
		"order": order,
		"nav": nav,
		"title": title,
		"blocks": blocks,
	}
	return {
		"page_home": page(
			"home",
			"about",
			0,
			bi("Welcome", "Bienvenidos"),
			[
				{"type": "heading", "text": bi("Welcome to our farm", "Bienvenidos a nuestra granja")},
				{
					"type": "text",
					"text": bi(
						"Replace this with a few words about the farm.",
						"Reemplace esto con unas palabras sobre la granja.",
					),
				},
			],
			nav=False,
		),
		"page_story": page(
			"story",
			"about",
			10,
			bi("Our story", "Nuestra historia"),
			[
				{"type": "text", "text": bi("Tell your story here.", "Cuente su historia aquí.")},
			],
		),
		"page_contact": page(
			"contact",
			"about",
			20,
			bi("Location & contact", "Ubicación y contacto"),
			[
				{"type": "contact"},
			],
		),
		"page_crew": page(
			"crew",
			"crew",
			30,
			bi("Crew information", "Información para el equipo"),
			[
				{"type": "heading", "text": bi("Housing rules", "Reglas de vivienda"), "entry": "housing"},
				{
					"type": "text",
					"text": bi("Add the housing rules.", "Agregue las reglas de vivienda."),
					"entry": "housing",
				},
				{"type": "heading", "text": bi("Pay day schedule", "Calendario de pago"), "entry": "payday"},
				{
					"type": "text",
					"text": bi(
						"Add the pay day schedule (no amounts).",
						"Agregue el calendario de pago (sin cantidades).",
					),
					"entry": "payday",
				},
				{
					"type": "heading",
					"text": bi("Required postings", "Avisos obligatorios"),
					"entry": "postings",
				},
				{
					"type": "text",
					"text": bi(
						"Where the required notices are posted.", "Dónde están los avisos obligatorios."
					),
					"entry": "postings",
				},
				{"type": "heading", "text": bi("Heat and safety", "Calor y seguridad"), "entry": "safety"},
				{
					"type": "text",
					"text": bi(
						"Drink water, rest in the shade, tell your lead if you feel ill.",
						"Tome agua, descanse en la sombra, avise a su líder si se siente mal.",
					),
					"entry": "safety",
				},
				{"type": "heading", "text": bi("Who to call", "A quién llamar"), "entry": "contacts"},
				{
					"type": "text",
					"text": bi("Add who to call, by role.", "Agregue a quién llamar, por puesto."),
					"entry": "contacts",
				},
			],
		),
	}


def _field(name, en, es, kind="text", required=False, options=None, target=""):
	out = {"name": name, "label": bi(en, es), "type": kind, "required": required}
	if options:
		out["options"] = [{"value": value, "label": bi(oen, oes)} for value, oen, oes in options]
	if target:
		out["target"] = target
	return out


def default_forms(company: str = "") -> dict:
	routing = {"company": company, "notify_roles": ["Farm Manager"], "notify_users": []}
	return {
		"form_report": {
			"schema_version": 1,
			"key": "form_report",
			"form": "report",
			"title": bi("Report a problem", "Reportar un problema"),
			"intro": bi(
				"Tell us what you saw. You can stay anonymous.", "Díganos lo que vio. Puede quedar anónimo."
			),
			"thanks": bi("Thank you. Your report was received.", "Gracias. Recibimos su reporte."),
			"fields": [
				_field(
					"category",
					"Category",
					"Categoría",
					"select",
					True,
					[
						("safety", "Safety", "Seguridad"),
						("equipment", "Equipment", "Equipo"),
						("water", "Water / irrigation", "Agua / riego"),
						("housing", "Housing", "Vivienda"),
						("other", "Other", "Otro"),
					],
				),
				_field("description", "What happened?", "¿Qué pasó?", "textarea", True),
				_field("location", "Where?", "¿Dónde?"),
				_field("photo", "Photo (optional)", "Foto (opcional)", "image"),
				_field("name", "Your name (optional)", "Su nombre (opcional)"),
				_field("phone", "Phone (optional)", "Teléfono (opcional)", "phone"),
			],
			"routing": {**routing, "create": "intake", "task_type": "Repair", "reporter_employee": ""},
		},
		"form_visit": {
			"schema_version": 1,
			"key": "form_visit",
			"form": "visit",
			"title": bi("Visitor check-in", "Registro de visitantes"),
			"intro": bi(
				"Please check in before entering the farm.",
				"Por favor regístrese antes de entrar a la granja.",
			),
			"thanks": bi(
				"You are checked in. Keep this page to check out.",
				"Ya está registrado. Guarde esta página para salir.",
			),
			"acknowledgement": bi(
				"I will follow the farm's biosecurity and food-safety rules, stay out of posted areas, "
				"and wash my hands before handling crops.",
				"Seguiré las reglas de bioseguridad e inocuidad de la granja, no entraré a las áreas "
				"señaladas y me lavaré las manos antes de tocar los cultivos.",
			),
			"show_rei_warning": True,
			"fields": [
				_field("name", "Your name", "Su nombre", required=True),
				_field("company", "Company", "Empresa"),
				_field(
					"purpose",
					"Purpose of visit",
					"Motivo de la visita",
					"select",
					True,
					[
						("delivery", "Delivery", "Entrega"),
						("service", "Service / repair", "Servicio / reparación"),
						("inspection", "Inspection / audit", "Inspección / auditoría"),
						("meeting", "Meeting", "Reunión"),
						("other", "Other", "Otro"),
					],
				),
				_field("host", "Who are you visiting?", "¿A quién visita?"),
				_field("vehicle", "Vehicle / plate", "Vehículo / placa"),
				_field("areas", "Areas you will visit", "Áreas que visitará", "textarea"),
				_field("phone", "Phone", "Teléfono", "phone"),
			],
			"routing": {**routing, "create": "intake"},
		},
		"form_apply": {
			"schema_version": 1,
			"key": "form_apply",
			"form": "apply",
			"title": bi("Apply", "Solicitar empleo"),
			"intro": bi(
				"No email? Leave your phone and we will call you.",
				"¿No tiene correo? Deje su teléfono y le llamaremos.",
			),
			"thanks": bi("Thank you. We received your application.", "Gracias. Recibimos su solicitud."),
			"fields": [
				_field("name", "Full name", "Nombre completo", required=True, target="applicant_name"),
				_field("phone", "Phone", "Teléfono", "phone", True, target="phone_number"),
				_field("email", "Email (optional)", "Correo (opcional)", "email", target="email_id"),
				_field(
					"preferred_language",
					"Preferred language",
					"Idioma preferido",
					"select",
					False,
					[("en", "English", "Inglés"), ("es", "Spanish", "Español"), ("other", "Other", "Otro")],
					target="preferred_language",
				),
				_field("availability", "When can you start?", "¿Cuándo puede empezar?", "date"),
				_field(
					"authorized",
					"Are you legally authorized to work in this country?",
					"¿Está autorizado legalmente para trabajar en este país?",
					"select",
					False,
					[("yes", "Yes", "Sí"), ("no", "No", "No")],
				),
				_field(
					"experience",
					"Experience (optional)",
					"Experiencia (opcional)",
					"textarea",
					target="cover_letter",
				),
			],
			"routing": {**routing, "notify_roles": ["HR Manager"], "create": "job_applicant"},
		},
	}


def all_defaults(company: str = "", brand: str = "") -> dict:
	return {SITE_KEY: default_site(company, brand), **default_pages(), **default_forms(company)}


# ── validation (the config lifecycle's per-kind check) ───────────────────────


def validate(body: dict, *, key: str, for_publish: bool = False) -> dict:
	errors, warnings = [], []
	if key == SITE_KEY:
		_check_site(body, errors, warnings, for_publish)
	elif key.startswith("page_"):
		_check_page(body, key, errors, warnings)
	elif key.startswith("form_"):
		_check_form(body, key, errors, warnings, for_publish)
	else:
		errors.append("a public_site key is `site`, `page_<slug>` or `form_report|form_visit|form_apply`")
	return {"errors": errors, "warnings": warnings}


def _check_site(body, errors, warnings, for_publish):
	company = str(body.get("company") or "")
	if company and compat.doctype_exists("Company") and not frappe.db.exists("Company", company):
		errors.append(f"company {company!r} is not a Company on this site")
	if for_publish and not company:
		errors.append("company is required before publishing — it is the ONLY company the site may show")
	prefix = str(body.get("route_prefix") or "")
	if not PREFIX_RE.match(prefix) or prefix in RESERVED_PREFIXES:
		errors.append(
			f"route_prefix {prefix!r} must be lower-case letters, digits or dashes and not one of "
			f"{', '.join(RESERVED_PREFIXES)}"
		)
	languages = body.get("languages") or []
	if not languages or any(lang not in LANGS for lang in languages):
		errors.append(f"languages must be a non-empty list of {', '.join(LANGS)}")
	if body.get("default_language") not in languages:
		errors.append("default_language must be one of languages")
	modules = body.get("modules") or {}
	unknown = sorted(set(modules) - set(MODULES))
	if unknown:
		errors.append(f"unknown modules: {', '.join(unknown)} (known: {', '.join(MODULES)})")
	brand = body.get("brand") or {}
	if not str(brand.get("name") or "").strip():
		errors.append("brand.name is required")
	for color in ("primary_color", "accent_color"):
		if brand.get(color) and not COLOR_RE.match(str(brand[color])):
			errors.append(f"brand.{color} must be #rrggbb")
	base = str(body.get("base_url") or "")
	if base and not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", base):
		errors.append("base_url must be an http(s) URL")
	if for_publish and not base:
		warnings.append("base_url is empty — QR codes need it")
	for source in body.get("qr_sources") or []:
		if not SRC_RE.match(str((source or {}).get("tag") or "")):
			errors.append(f"QR source tag {source!r} must be lower-case letters, digits, - or _")
	for name, low, high in (
		("max_upload_mb", 1, 20),
		("submissions_per_visitor_per_hour", 1, 60),
		("submissions_per_hour", 1, 5000),
		("min_seconds_to_submit", 0, 120),
	):
		try:
			value = int(body.get(name))
		except (TypeError, ValueError):
			errors.append(f"{name} must be a whole number")
			continue
		if not low <= value <= high:
			errors.append(f"{name} must be between {low} and {high}")
	if body.get("set_home_page"):
		warnings.append("set_home_page points the site's / at this public site when published")


def _check_page(body, key, errors, warnings):
	slug = str(body.get("slug") or "")
	if not SLUG_RE.match(slug):
		errors.append(f"slug {slug!r} must be lower-case letters, digits or dashes")
	elif key != f"page_{slug.replace('-', '_')}":
		errors.append(f"the key for slug {slug!r} is page_{slug.replace('-', '_')}")
	if slug in ("jobs", "report", "visit", "media", "logo", "checkout"):
		errors.append(f"slug {slug!r} is taken by the site itself")
	if body.get("module") not in PAGE_MODULES:
		errors.append(f"module must be one of {', '.join(PAGE_MODULES)}")
	if not t(body.get("title")):
		errors.append("title.en is required")
	for index, block in enumerate(body.get("blocks") or []):
		kind = (block or {}).get("type")
		if kind not in BLOCK_TYPES:
			errors.append(f"blocks[{index}].type must be one of {', '.join(BLOCK_TYPES)}")
		elif kind == "image" and not str(block.get("image") or "").startswith(("/files/", "/private/files/")):
			errors.append(f"blocks[{index}].image must be a File url (/files/… or /private/files/…)")


def _check_form(body, key, errors, warnings, for_publish):
	form = body.get("form")
	if form not in FORMS or key != f"form_{form}":
		errors.append(f"form must be one of {', '.join(FORMS)} and match the key")
	names = []
	for index, field in enumerate(body.get("fields") or []):
		name = str((field or {}).get("name") or "")
		if not re.match(r"^[a-z][a-z0-9_]{0,40}$", name):
			errors.append(f"fields[{index}].name {name!r} must be lower_snake_case")
		if name in names:
			errors.append(f"field {name!r} appears twice")
		if name in RESERVED_FIELDS:
			errors.append(f"field name {name!r} is used by the form itself")
		names.append(name)
		if field.get("type") not in FIELD_TYPES:
			errors.append(f"field {name!r} type must be one of {', '.join(FIELD_TYPES)}")
		if field.get("type") == "select" and not field.get("options"):
			errors.append(f"select field {name!r} needs options")
		if not t(field.get("label")):
			errors.append(f"field {name!r} needs a label")
	if form == "visit" and not t(body.get("acknowledgement")):
		errors.append("the visitor form needs an acknowledgement text")
	if form == "apply":
		targets = {f.get("target") for f in body.get("fields") or []}
		if "applicant_name" not in targets:
			errors.append("the apply form needs a field with target applicant_name")
	routing = body.get("routing") or {}
	if routing.get("create") not in ("intake", "farm_task", "job_applicant"):
		errors.append("routing.create must be intake, farm_task or job_applicant")
	if routing.get("create") == "farm_task" and not routing.get("reporter_employee"):
		errors.append(
			"routing.create = farm_task needs routing.reporter_employee — the Employee a guest report is filed as"
		)
	if (
		routing.get("reporter_employee")
		and compat.doctype_exists("Employee")
		and not frappe.db.exists("Employee", routing["reporter_employee"])
	):
		errors.append(f"routing.reporter_employee {routing['reporter_employee']!r} is not an Employee")
	company = str(routing.get("company") or "")
	if company and compat.doctype_exists("Company") and not frappe.db.exists("Company", company):
		errors.append(f"routing.company {company!r} is not a Company on this site")
	if not routing.get("notify_roles") and not routing.get("notify_users"):
		warnings.append("nobody is told when this form is submitted")


# ── reading the config ───────────────────────────────────────────────────────


def bodies(drafts: bool = False) -> dict:
	"""{key: body} of the Published versions — with drafts=True, the newest Draft/Staged over them."""
	from . import phone_config

	if not phone_config.ready():
		return {}
	out = {}
	for row in phone_config.rows(KIND, "", phone_config.PUBLISHED):
		out[row["config_key"]] = phone_config.body_of(frappe.get_doc(phone_config.DOCTYPE, row["name"]))
	if drafts:
		newest = {}
		for row in phone_config.rows(KIND, "", (phone_config.DRAFT, phone_config.STAGED)):
			if int(row["version"]) > int(newest.get(row["config_key"], {}).get("version") or 0):
				newest[row["config_key"]] = row
		for key, row in newest.items():
			out[key] = phone_config.body_of(frappe.get_doc(phone_config.DOCTYPE, row["name"]))
	return out


def site(config: dict) -> dict:
	return {**default_site(), **(config.get(SITE_KEY) or {})}


def enabled() -> bool:
	try:
		from . import settings

		return settings.as_bool(getattr(settings.get_settings(), "public_site_enabled", 0))
	except Exception:
		return False


def live_prefix() -> str:
	"""The prefix the renderer answers on — only while the site is enabled and its config published."""
	if not enabled():
		return ""
	config = bodies()
	if SITE_KEY not in config:
		return ""
	return str(site(config).get("route_prefix") or "")


def module_on(config: dict, module: str) -> bool:
	return bool((site(config).get("modules") or {}).get(module))


def pages(config: dict) -> list:
	out = [body for key, body in config.items() if key.startswith("page_")]
	return sorted(out, key=lambda page: (int(page.get("order") or 0), page.get("slug") or ""))


def media_allowed(config: dict) -> set:
	"""Every File url a page or the brand names — the only files a guest may fetch."""
	urls = {str(site(config).get("brand", {}).get("logo") or "")}
	for page in pages(config):
		for block in page.get("blocks") or []:
			if block.get("type") == "image":
				urls.add(str(block.get("image") or ""))
	return {url for url in urls if url}


def funnel_paths(config: dict) -> list:
	prefix = site(config).get("route_prefix") or "farm"
	return [f"/{prefix}", f"/{prefix}/*"]


# ── the guest-facing guards ──────────────────────────────────────────────────


def _secret() -> bytes:
	secret = ""
	try:
		from frappe.utils.password import get_encryption_key

		secret = str(get_encryption_key() or "")
	except Exception:
		secret = str((getattr(frappe, "conf", None) or {}).get("encryption_key") or "")
	return hashlib.sha256(f"erpnext_mcp.public_site|{secret or frappe.local.site}".encode()).digest()


def form_token(form: str, now: float | None = None) -> str:
	issued = int(now if now is not None else time.time())
	mac = hmac.new(_secret(), f"{form}|{issued}".encode(), hashlib.sha256).hexdigest()[:24]
	return f"{issued}.{mac}"


def check_token(form: str, token: str, min_seconds: int, now: float | None = None) -> str:
	""" "" when the token is this site's, for this form, at least `min_seconds` old and under 2 h; else why not."""
	try:
		issued_text, mac = str(token or "").split(".")
		issued = int(issued_text)
	except ValueError:
		return "the form was not loaded from this site"
	expected = hmac.new(_secret(), f"{form}|{issued}".encode(), hashlib.sha256).hexdigest()[:24]
	if not hmac.compare_digest(expected, mac):
		return "the form was not loaded from this site"
	age = (now if now is not None else time.time()) - issued
	if age < min_seconds:
		return "submitted too quickly"
	if age > 7200:
		return "the form expired — reload the page"
	return ""


def client_hash(ip: str) -> str:
	return hmac.new(_secret(), f"ip|{ip or ''}".encode(), hashlib.sha256).hexdigest()[:32]


def over_limit(config: dict, client: str) -> str:
	if not compat.doctype_exists(ENTRY):
		return ""
	# The site's clock, the one `submitted_at` was stamped with — not the worker's.
	stamped = datetime.datetime.strptime(str(frappe.utils.now())[:19], "%Y-%m-%d %H:%M:%S")
	cutoff = (stamped - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
	base = {"submitted_at": (">", cutoff), "entry_type": ("!=", "Scan")}
	settings_ = site(config)
	if frappe.db.count(ENTRY, {**base, "client_hash": client}) >= int(
		settings_["submissions_per_visitor_per_hour"]
	):
		return "too many submissions from this device — try again in an hour"
	if frappe.db.count(ENTRY, base) >= int(settings_["submissions_per_hour"]):
		return "the site is receiving too many submissions — try again later"
	return ""


def image_kind(data: bytes) -> str:
	for magic, kind in IMAGE_MAGIC:
		if data.startswith(magic) and (kind != "webp" or data[8:12] == b"WEBP"):
			return kind
	return ""


# ── what a submission does ───────────────────────────────────────────────────


def submit(
	config: dict,
	form: str,
	values: dict,
	photo: bytes | None,
	*,
	lang: str,
	src: str,
	client: str,
	job_opening: str = "",
) -> dict:
	"""Validate one guest submission against its published form, store it, route it. Raises SiteError."""
	body = config.get(f"form_{form}")
	if not body or not module_on(config, {"apply": "jobs"}.get(form, form)):
		raise SiteError("this form is not available")
	settings_ = site(config)
	answers = {}
	for field in body.get("fields") or []:
		name, kind = field["name"], field.get("type")
		if kind == "image":
			continue
		raw = str(values.get(name) or "").strip()[:4000]
		if kind == "check":
			raw = "1" if raw in ("1", "on", "true", "yes") else ""
		if field.get("required") and not raw:
			raise SiteError(f"{t(field.get('label'), lang)}: required")
		if kind == "select" and raw and raw not in {o["value"] for o in field.get("options") or []}:
			raise SiteError(f"{t(field.get('label'), lang)}: not one of the choices")
		if kind == "email" and raw and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", raw):
			raise SiteError(f"{t(field.get('label'), lang)}: not an email address")
		if kind == "phone" and raw and not re.match(r"^[0-9+()\-.\s]{7,25}$", raw):
			raise SiteError(f"{t(field.get('label'), lang)}: not a phone number")
		if kind == "date" and raw and not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
			raise SiteError(f"{t(field.get('label'), lang)}: not a date")
		answers[name] = raw
	if photo:
		if not any(f.get("type") == "image" for f in body.get("fields") or []):
			raise SiteError("this form takes no photo")
		if len(photo) > int(settings_["max_upload_mb"]) * 1024 * 1024:
			raise SiteError(f"the photo is over {settings_['max_upload_mb']} MB")
		if not image_kind(photo):
			raise SiteError("the photo must be a JPEG, PNG or WebP image")
	if form == "visit" and values.get("acknowledged") not in ("1", "on", "true", "yes"):
		raise SiteError(t(bi("Please accept the acknowledgement.", "Por favor acepte la declaración."), lang))
	routing = body.get("routing") or {}
	company = routing.get("company") or settings_.get("company")
	entry = frappe.new_doc(ENTRY)
	entry.update(
		{
			"entry_type": {"report": "Report", "visit": "Visit", "apply": "Application"}[form],
			"company": company or None,
			"status": "New",
			"language": lang,
			"source": src or None,
			"submitted_at": frappe.utils.now(),
			"client_hash": client,
			"contact_name": answers.get("name") or None,
			"contact_phone": answers.get("phone") or None,
			"contact_email": answers.get("email") or None,
			"category": answers.get("category") or answers.get("purpose") or None,
			"description": answers.get("description") or None,
			"location": answers.get("location") or None,
			"answers_json": json.dumps(answers, ensure_ascii=False),
		}
	)
	checkout_code = ""
	if form == "visit":
		checkout_code = (
			base64.b32encode(
				hashlib.sha256(f"{client}|{time.time()}|{frappe.generate_hash()}".encode()).digest()[:10]
			)
			.decode()
			.rstrip("=")
		)
		entry.update(
			{
				"visitor_company": answers.get("company") or None,
				"purpose": answers.get("purpose") or None,
				"host": answers.get("host") or None,
				"vehicle": answers.get("vehicle") or None,
				"areas_visited": answers.get("areas") or None,
				"acknowledged": 1,
				"acknowledgement_text": t(body.get("acknowledgement"), lang),
				"rei_warning": rei_warning(company, lang) or None,
				"check_in": frappe.utils.now(),
				"checkout_code_hash": hashlib.sha256(checkout_code.encode()).hexdigest(),
			}
		)
	if form == "apply" and job_opening:
		entry.job_opening = job_opening
	entry.insert(ignore_permissions=True)
	if photo:
		_attach_photo(entry, photo)
	linked = _route(form, body, entry, answers, job_opening, lang, src)
	_notify(routing, entry, form)
	return {"entry": entry.name, "checkout_code": checkout_code, "linked": linked}


def _attach_photo(entry, photo: bytes) -> None:
	kind = image_kind(photo)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{entry.name}-photo.{kind}",
			"content": photo,
			"is_private": 1,
			"attached_to_doctype": ENTRY,
			"attached_to_name": entry.name,
			"attached_to_field": "photo",
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.insert(ignore_permissions=True)
	frappe.db.set_value(ENTRY, entry.name, "photo", file_doc.file_url)


def _route(form: str, body: dict, entry, answers: dict, job_opening: str, lang: str, src: str) -> dict | None:
	routing = body.get("routing") or {}
	try:
		if form == "apply" and routing.get("create") == "job_applicant":
			return _job_applicant(body, entry, answers, job_opening, lang, src)
		if form == "report" and routing.get("create") == "farm_task":
			return promote_report(entry.name, routing)
	except Exception as exc:  # the entry is kept whatever happens downstream
		frappe.db.set_value(ENTRY, entry.name, "notes", f"Not routed automatically: {exc}")
	return None


def _job_applicant(body, entry, answers, job_opening, lang, src) -> dict | None:
	if not compat.doctype_exists(JOB_APPLICANT):
		return None
	values = {}
	for field in body.get("fields") or []:
		target = field.get("target")
		if target and answers.get(field["name"]):
			values[target] = answers[field["name"]]
	if not values.get("email_id"):
		# HRMS requires an email on a Job Applicant. A phone-only application is kept as the entry.
		frappe.db.set_value(ENTRY, entry.name, "notes", "No email given: kept here for HR to call back.")
		return None
	doc = frappe.new_doc(JOB_APPLICANT)
	doc.update({key: value for key, value in values.items() if compat.has_field(JOB_APPLICANT, key)})
	if job_opening:
		doc.job_title = job_opening
	if compat.has_field(JOB_APPLICANT, "public_answers"):
		doc.public_answers = json.dumps(answers, ensure_ascii=False)
		doc.preferred_language = values.get("preferred_language") or lang
		doc.public_source = src or None
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(ENTRY, entry.name, {"linked_doctype": JOB_APPLICANT, "linked_document": doc.name})
	return {"doctype": JOB_APPLICANT, "name": doc.name}


def promote_report(entry_name: str, routing: dict) -> dict:
	"""File a public report as a field-reported Farm Task through `report_field_task`."""
	from .tools import dispatch

	entry = frappe.get_doc(ENTRY, entry_name)
	if entry.get("linked_document"):
		return {"doctype": entry.linked_doctype, "name": entry.linked_document, "already": True}
	reporter = routing.get("reporter_employee")
	if not reporter:
		raise SiteError("routing.reporter_employee is not set — the Employee a guest report is filed as")
	photo_token = ""
	if entry.get("photo"):
		photo_token = frappe.db.get_value("File", {"file_url": entry.photo}, "name") or ""
	args = {
		"reported_by": reporter,
		"company": entry.company,
		"description": f"[Public report {entry.name}] {entry.get('category') or ''}: {entry.get('description') or ''}"
		+ (f" — at {entry.location}" if entry.get("location") else ""),
		"task_type": routing.get("task_type") or "Repair",
		"urgency": "Normal",
	}
	if photo_token:
		args["photo_file_token"] = photo_token
	result = dispatch.report_field_task(args)
	name = result.data.get("name")
	frappe.db.set_value(
		ENTRY, entry_name, {"linked_doctype": "Farm Task", "linked_document": name, "status": "Triaged"}
	)
	return {"doctype": "Farm Task", "name": name}


def _notify(routing: dict, entry, form: str) -> None:
	users = set(routing.get("notify_users") or [])
	roles = routing.get("notify_roles") or []
	if roles and compat.doctype_exists("Has Role"):
		for row in frappe.db.get_all(
			"Has Role", filters={"role": ("in", list(roles)), "parenttype": "User"}, fields=["parent"]
		):
			users.add(row.get("parent"))
	users.discard("Guest")
	users.discard("Administrator")
	for user in sorted(user for user in users if user):
		try:
			if compat.doctype_exists("User") and not frappe.db.get_value("User", user, "enabled"):
				continue
			todo = frappe.new_doc("ToDo")
			todo.update(
				{
					"allocated_to": user,
					"reference_type": ENTRY,
					"reference_name": entry.name,
					"description": f"New public {form} submission {entry.name}"
					+ (f": {entry.get('category')}" if entry.get("category") else ""),
					"priority": "Medium",
				}
			)
			todo.insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(
				title="erpnext_mcp: public site notification failed", message=compat.traceback_text()
			)


def checkout(code: str) -> str:
	"""Check a visitor out by the code shown at check-in. Returns the entry name, or "" when unknown."""
	code = re.sub(r"[^A-Z2-7]", "", str(code or "").upper())
	if len(code) < 8:
		return ""
	name = frappe.db.get_value(
		ENTRY,
		{"checkout_code_hash": hashlib.sha256(code.encode()).hexdigest(), "entry_type": "Visit"},
		"name",
	)
	if not name:
		return ""
	if not frappe.db.get_value(ENTRY, name, "check_out"):
		frappe.db.set_value(ENTRY, name, "check_out", frappe.utils.now())
	return name


def record_scan(company: str, src: str) -> None:
	"""One more hit for (src, today). Only known source tags are counted.

	Committed here: a scan is a GET, and Frappe commits a web request only for unsafe methods,
	so without this every count is rolled back at the end of the request (seen on a live bench).
	"""
	if not src or not compat.doctype_exists(ENTRY):
		return
	try:
		today = frappe.utils.today()
		name = frappe.db.get_value(ENTRY, {"entry_type": "Scan", "source": src, "scan_date": today}, "name")
		if name:
			frappe.db.set_value(
				ENTRY,
				name,
				"hits",
				int(frappe.db.get_value(ENTRY, name, "hits") or 0) + 1,
				update_modified=False,
			)
			frappe.db.commit()
			return
		doc = frappe.new_doc(ENTRY)
		doc.update(
			{
				"entry_type": "Scan",
				"company": company or None,
				"status": "Closed",
				"source": src,
				"scan_date": today,
				"hits": 1,
				"submitted_at": frappe.utils.now(),
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass


def rei_warning(company: str, lang: str) -> str:
	"""Active restricted-entry intervals on the public company's blocks, as one sentence (no names)."""
	if not company:
		return ""
	try:
		from .tools import spray_rei

		rows = spray_rei.active_rows(company=company)
	except Exception:
		return ""
	places = sorted({str(row.get("block") or "") for row in rows or [] if row} - {""})
	if not places:
		return ""
	return t(
		bi(
			"Restricted entry — recently sprayed, do not enter: ",
			"Entrada restringida — aplicación reciente, no entre: ",
		),
		lang,
	) + ", ".join(places)


# ── the Job Applicant columns ────────────────────────────────────────────────


def ensure_applicant_fields() -> bool:
	"""Three Custom Fields on Job Applicant (answers, preferred language, QR source). Never raises."""
	try:
		if not compat.doctype_exists(JOB_APPLICANT) or not compat.doctype_exists("Custom Field"):
			return False
		spec = (
			{
				"fieldname": "public_answers",
				"label": "Public Site Answers",
				"fieldtype": "Long Text",
				"insert_after": "cover_letter",
				"read_only": 1,
				"description": "Everything the applicant answered on the public site's form, as submitted.",
			},
			{
				"fieldname": "preferred_language",
				"label": "Preferred Language",
				"fieldtype": "Data",
				"insert_after": "public_answers",
			},
			{
				"fieldname": "public_source",
				"label": "Public Site Source",
				"fieldtype": "Data",
				"insert_after": "preferred_language",
				"read_only": 1,
				"description": "The QR source tag the applicant arrived through, if any.",
			},
		)
		for field in spec:
			if compat.has_field(JOB_APPLICANT, field["fieldname"]):
				continue
			if frappe.db.exists("Custom Field", {"dt": JOB_APPLICANT, "fieldname": field["fieldname"]}):
				continue
			doc = frappe.new_doc("Custom Field")
			doc.dt = JOB_APPLICANT
			for key, value in field.items():
				doc.set(key, value)
			doc.insert(ignore_permissions=True)
		return all(compat.has_field(JOB_APPLICANT, name) for name in APPLICANT_FIELDS)
	except Exception:
		return False


def open_jobs(config: dict) -> list:
	"""The public company's open, published Job Openings — no other company's, ever."""
	company = site(config).get("company")
	if not company or not compat.doctype_exists(JOB_OPENING) or not module_on(config, "jobs"):
		return []
	fields = compat.existing_fields(
		JOB_OPENING,
		[
			"name",
			"job_title",
			"description",
			"location",
			"employment_type",
			"closes_on",
			"planned_vacancies",
			"lower_range",
			"upper_range",
			"currency",
			"publish_salary_range",
		],
	)
	return frappe.db.get_all(
		JOB_OPENING,
		filters={"company": company, "status": "Open", "publish": 1},
		fields=fields,
		order_by="creation desc",
		limit=100,
	)


# ── rendering ────────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d1d1b;background:#fbfaf7}
header{background:var(--p);color:#fff;padding:14px 16px}header a{color:#fff;text-decoration:none}
.brand{display:flex;gap:10px;align-items:center;font-weight:700;font-size:1.15rem}.brand img{height:40px;width:auto}
.tag{opacity:.85;font-size:.9rem;font-weight:400}nav{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:.95rem}
nav a{border-bottom:2px solid transparent}nav a.on{border-color:var(--a)}.lang{float:right;font-size:.9rem}
main{max-width:720px;margin:0 auto;padding:16px}h1{font-size:1.5rem;margin:.4em 0}h2{font-size:1.2rem;margin:1.2em 0 .3em}
.callout{border-left:4px solid var(--a);background:#fff;padding:10px 12px}.card{background:#fff;border:1px solid #e3dfd6;border-radius:8px;padding:12px;margin:10px 0}
img.block{max-width:100%;border-radius:8px}label{display:block;font-weight:600;margin:12px 0 4px}
input,select,textarea{width:100%;font:inherit;padding:10px;border:1px solid #c9c4b8;border-radius:6px;background:#fff}
textarea{min-height:110px}.check{display:flex;gap:8px;align-items:flex-start;font-weight:400}.check input{width:auto;margin-top:5px}
button{background:var(--p);color:#fff;border:0;border-radius:6px;padding:12px 18px;font:inherit;font-weight:700;margin-top:16px;width:100%}
.hp{position:absolute;left:-5000px;width:1px;height:1px;overflow:hidden}.err{background:#fdecea;border:1px solid #f5c2bd;padding:10px;border-radius:6px}
.ok{background:#eaf5ec;border:1px solid #bfdcc6;padding:10px;border-radius:6px}.warn{background:#fff4dd;border:1px solid #f0d59a;padding:10px;border-radius:6px}
footer{text-align:center;color:#77736a;font-size:.85rem;padding:24px 16px}.code{font:700 1.4rem/1.2 ui-monospace,monospace;letter-spacing:.12em}
"""


class Page:
	"""One rendered response: status, body, content type."""

	def __init__(self, status: int, body, content_type: str = "text/html; charset=utf-8"):
		self.status, self.body, self.content_type = status, body, content_type


def _href(prefix: str, path: str, lang: str, src: str) -> str:
	query = [f"lang={lang}"] + ([f"src={src}"] if src else [])
	return f"/{prefix}{('/' + path) if path else ''}?{'&'.join(query)}"


def shell(config: dict, title: str, inner: str, *, lang: str, src: str, current: str = "") -> str:
	cfg = site(config)
	prefix = cfg["route_prefix"]
	brand = cfg.get("brand") or {}
	logo = f'<img src="/{prefix}/media?u={esc(brand["logo"])}" alt="">' if brand.get("logo") else ""
	links = []
	for page in pages(config):
		if not page.get("nav") or (page.get("module") == "crew" and not module_on(config, "crew")):
			continue
		if page.get("module") == "about" and not module_on(config, "about"):
			continue
		links.append((page["slug"], t(page.get("title"), lang)))
	for module, en, es in (
		("jobs", "Jobs", "Empleos"),
		("report", "Report a problem", "Reportar un problema"),
		("visit", "Visitors", "Visitantes"),
	):
		if module_on(config, module):
			links.append((module, t(bi(en, es), lang)))
	nav = "".join(
		f'<a class="{"on" if slug == current else ""}" href="{_href(prefix, slug, lang, src)}">{esc(label)}</a>'
		for slug, label in links
	)
	other = [code for code in cfg.get("languages") or [] if code != lang]
	toggle = (
		f'<a class="lang" href="{_href(prefix, current if current != "home" else "", other[0], src)}">'
		f"{'English' if other[0] == 'en' else 'Español'}</a>"
		if other
		else ""
	)
	return (
		f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
		f'<meta name="viewport" content="width=device-width,initial-scale=1">'
		f'<meta name="robots" content="index,follow"><title>{esc(title)} · {esc(brand.get("name"))}</title>'
		f"<style>:root{{--p:{esc(brand.get('primary_color') or '#2f6b3a')};--a:{esc(brand.get('accent_color') or '#b8862b')}}}"
		f"{_CSS}</style></head><body><header>{toggle}"
		f'<a class="brand" href="{_href(prefix, "", lang, src)}">{logo}<span>{esc(brand.get("name"))}<br>'
		f'<span class="tag">{esc(t(brand.get("tagline"), lang))}</span></span></a><nav>{nav}</nav></header>'
		f"<main>{inner}</main><footer>{esc(brand.get('name'))}</footer></body></html>"
	)


def render_blocks(config: dict, blocks: list, lang: str) -> str:
	out = []
	cfg = site(config)
	for block in blocks or []:
		kind = block.get("type")
		if kind == "heading":
			out.append(f"<h2>{esc(t(block.get('text'), lang))}</h2>")
		elif kind == "text":
			paragraphs = [p for p in t(block.get("text"), lang).split("\n\n") if p.strip()]
			out.append("".join(f"<p>{esc(p).replace(chr(10), '<br>')}</p>" for p in paragraphs))
		elif kind == "callout":
			out.append(f'<p class="callout">{esc(t(block.get("text"), lang))}</p>')
		elif kind == "list":
			items = "".join(f"<li>{esc(t(item, lang))}</li>" for item in block.get("items") or [])
			out.append(f"<ul>{items}</ul>")
		elif kind == "image" and block.get("image"):
			out.append(
				f'<img class="block" src="/{cfg["route_prefix"]}/media?u={esc(block["image"])}" '
				f'alt="{esc(t(block.get("alt"), lang))}">'
			)
		elif kind == "contact":
			contact = cfg.get("contact") or {}
			lines = []
			if t(contact.get("address"), lang):
				lines.append(esc(t(contact.get("address"), lang)).replace("\n", "<br>"))
			if contact.get("phone"):
				lines.append(f'<a href="tel:{esc(contact["phone"])}">{esc(contact["phone"])}</a>')
			if contact.get("email"):
				lines.append(f'<a href="mailto:{esc(contact["email"])}">{esc(contact["email"])}</a>')
			if lines:
				out.append(f'<div class="card">{"<br>".join(lines)}</div>')
	return "".join(out)


def render_form(
	config: dict,
	form: str,
	lang: str,
	src: str,
	*,
	action: str,
	job: dict | None = None,
	error: str = "",
	values: dict | None = None,
	now: float | None = None,
) -> str:
	body = config.get(f"form_{form}") or {}
	values = values or {}
	cfg = site(config)
	parts = [
		f"<h1>{esc(t(body.get('title'), lang))}" + (f": {esc(job.get('job_title'))}" if job else "") + "</h1>"
	]
	if t(body.get("intro"), lang):
		parts.append(f"<p>{esc(t(body.get('intro'), lang))}</p>")
	if form == "visit" and body.get("show_rei_warning"):
		warning = rei_warning(cfg.get("company"), lang)
		if warning:
			parts.append(f'<p class="warn">{esc(warning)}</p>')
	if error:
		parts.append(f'<p class="err">{esc(error)}</p>')
	multipart = any(f.get("type") == "image" for f in body.get("fields") or [])
	parts.append(
		f'<form method="post" action="{esc(action)}"'
		+ (' enctype="multipart/form-data"' if multipart else "")
		+ ">"
	)
	parts.append(f'<input type="hidden" name="_token" value="{esc(form_token(form, now))}">')
	parts.append(
		f'<input type="hidden" name="lang" value="{esc(lang)}"><input type="hidden" name="src" value="{esc(src)}">'
	)
	csrf = getattr(getattr(frappe, "session", None), "data", None)
	token = (csrf or {}).get("csrf_token") if isinstance(csrf, dict) else getattr(csrf, "csrf_token", None)
	if token:
		parts.append(f'<input type="hidden" name="csrf_token" value="{esc(token)}">')
	parts.append(
		'<div class="hp" aria-hidden="true"><label>Website<input name="website" tabindex="-1" autocomplete="off"></label></div>'
	)
	for field in body.get("fields") or []:
		name, kind = field["name"], field.get("type")
		label = esc(t(field.get("label"), lang)) + (" *" if field.get("required") else "")
		required = " required" if field.get("required") else ""
		value = esc(values.get(name, ""))
		if kind == "textarea":
			control = f'<textarea name="{name}" id="f_{name}"{required}>{value}</textarea>'
		elif kind == "select":
			options = "".join(
				f'<option value="{esc(o["value"])}"{" selected" if values.get(name) == o["value"] else ""}>'
				f"{esc(t(o.get('label'), lang))}</option>"
				for o in field.get("options") or []
			)
			control = (
				f'<select name="{name}" id="f_{name}"{required}><option value=""></option>{options}</select>'
			)
		elif kind == "check":
			parts.append(
				f'<label class="check"><input type="checkbox" name="{name}" value="1"{required}> {label}</label>'
			)
			continue
		elif kind == "image":
			control = (
				f'<input type="file" name="{name}" id="f_{name}" accept="image/jpeg,image/png,image/webp" '
				f'capture="environment"{required}>'
			)
		else:
			html_type = {"phone": "tel", "email": "email", "date": "date"}.get(kind, "text")
			control = f'<input type="{html_type}" name="{name}" id="f_{name}" value="{value}"{required}>'
		parts.append(f'<label for="f_{name}">{label}</label>{control}')
	if form == "visit":
		parts.append(
			f'<label class="check"><input type="checkbox" name="acknowledged" value="1" required> '
			f"{esc(t(body.get('acknowledgement'), lang))}</label>"
		)
	parts.append(f"<button>{esc(t(bi('Send', 'Enviar'), lang))}</button></form>")
	return "".join(parts)


def render(
	path: str,
	*,
	method: str = "GET",
	form: dict | None = None,
	files: dict | None = None,
	ip: str = "",
	drafts: bool = False,
	config: dict | None = None,
	now: float | None = None,
) -> Page:
	"""One request to the public site. `path` is relative to the prefix ("" = home)."""
	config = config if config is not None else bodies(drafts=drafts)
	cfg = site(config)
	form = form or {}
	languages = cfg.get("languages") or ["en"]
	lang = str(form.get("lang") or "")
	lang = lang if lang in languages else cfg.get("default_language") or languages[0]
	src = str(form.get("src") or "")
	src = src if SRC_RE.match(src) and src in {s.get("tag") for s in cfg.get("qr_sources") or []} else ""
	prefix = cfg["route_prefix"]
	parts = [p for p in str(path or "").strip("/").split("/") if p]
	head = parts[0] if parts else ""
	not_found = Page(
		404,
		shell(
			config, "404", f"<h1>{esc(t(bi('Not found', 'No encontrado'), lang))}</h1>", lang=lang, src=src
		),
	)

	if head == "media":
		url = str(form.get("u") or "")
		if url not in media_allowed(config):
			return Page(404, b"", "text/plain")
		file_name = frappe.db.get_value("File", {"file_url": url}, "name")
		if not file_name:
			return Page(404, b"", "text/plain")
		content = frappe.get_doc("File", file_name).get_content()
		content = content if isinstance(content, bytes) else str(content).encode()
		kind = image_kind(content)
		if not kind:
			return Page(404, b"", "text/plain")
		return Page(200, content, {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[kind])

	if method == "GET" and src and not drafts:
		record_scan(cfg.get("company"), src)

	if head == "visit" and len(parts) == 2 and parts[1] == "out":
		if not module_on(config, "visit") or not config.get("form_visit"):
			return not_found
		title = t(bi("Check out", "Registrar salida"), lang)
		done = ""
		if method == "POST":
			done = checkout(form.get("code"))
			if done:
				inner = f'<h1>{esc(title)}</h1><p class="ok">{esc(t(bi("You are checked out. Thank you.", "Su salida quedó registrada. Gracias."), lang))}</p>'
				return Page(200, shell(config, title, inner, lang=lang, src=src, current="visit"))
		error = (
			f'<p class="err">{esc(t(bi("That code was not found.", "No se encontró ese código."), lang))}</p>'
			if method == "POST"
			else ""
		)
		inner = (
			f'<h1>{esc(title)}</h1>{error}<form method="post" action="/{prefix}/visit/out">'
			f'<input type="hidden" name="lang" value="{esc(lang)}">'
			f'<label for="code">{esc(t(bi("Check-out code", "Código de salida"), lang))}</label>'
			f'<input id="code" name="code" value="{esc(form.get("code") or "")}" required autocapitalize="characters">'
			f"<button>{esc(title)}</button></form>"
		)
		return Page(400 if error else 200, shell(config, title, inner, lang=lang, src=src, current="visit"))

	if (head in ("report", "visit") and len(parts) == 1) or (
		head == "jobs" and len(parts) == 3 and parts[2] == "apply"
	):
		form_name = "apply" if head == "jobs" else head
		job = None
		if form_name == "apply":
			job = next((j for j in open_jobs(config) if j["name"] == parts[1]), None)
			if not job:
				return not_found
		if not config.get(f"form_{form_name}") or not module_on(
			config, "jobs" if form_name == "apply" else form_name
		):
			return not_found
		action = f"/{prefix}/{'/'.join(parts)}"
		if method == "POST":
			error = _guard(config, form_name, form, ip, now)
			if not error:
				try:
					photo = _photo_bytes(files, int(cfg["max_upload_mb"]) * 1024 * 1024)
					result = submit(
						config,
						form_name,
						form,
						photo,
						lang=lang,
						src=src,
						client=client_hash(ip),
						job_opening=(job or {}).get("name", ""),
					)
				except SiteError as exc:
					error = str(exc)
				else:
					body = config.get(f"form_{form_name}") or {}
					inner = f'<h1>{esc(t(body.get("title"), lang))}</h1><p class="ok">{esc(t(body.get("thanks"), lang))}</p>'
					if result.get("checkout_code"):
						inner += (
							f"<p>{esc(t(bi('Your check-out code:', 'Su código de salida:'), lang))}</p>"
							f'<p class="code">{esc(result["checkout_code"])}</p>'
							f'<p><a href="{_href(prefix, "visit/out", lang, src)}&code={esc(result["checkout_code"])}">'
							f"{esc(t(bi('Check out when you leave', 'Registre su salida al irse'), lang))}</a></p>"
						)
					return Page(
						200,
						shell(config, t(body.get("title"), lang), inner, lang=lang, src=src, current=head),
					)
			if error == "spam":
				return Page(
					200,
					shell(
						config,
						"OK",
						f'<p class="ok">{esc(t(bi("Thank you.", "Gracias."), lang))}</p>',
						lang=lang,
						src=src,
					),
				)
			inner = render_form(
				config, form_name, lang, src, action=action, job=job, error=error, values=form, now=now
			)
			return Page(
				400 if error else 200, shell(config, form_name, inner, lang=lang, src=src, current=head)
			)
		inner = render_form(config, form_name, lang, src, action=action, job=job, now=now)
		return Page(
			200,
			shell(
				config,
				t((config.get(f"form_{form_name}") or {}).get("title"), lang),
				inner,
				lang=lang,
				src=src,
				current=head,
			),
		)

	if head in ("report", "visit"):
		return not_found

	if head == "jobs":
		if not module_on(config, "jobs"):
			return not_found
		jobs = open_jobs(config)
		if len(parts) == 2:
			job = next((j for j in jobs if j["name"] == parts[1]), None)
			if not job:
				return not_found
			apply = (
				f'<p><a href="{_href(prefix, "jobs/" + job["name"] + "/apply", lang, src)}">'
				f"<button>{esc(t(bi('Apply', 'Solicitar'), lang))}</button></a></p>"
			)
			description = (
				frappe.utils.strip_html(str(job.get("description") or ""))
				if hasattr(frappe.utils, "strip_html")
				else re.sub(r"<[^>]+>", " ", str(job.get("description") or ""))
			)
			inner = f"<h1>{esc(job.get('job_title'))}</h1><p>{esc(description)}</p>{apply}"
			return Page(200, shell(config, job.get("job_title"), inner, lang=lang, src=src, current="jobs"))
		page = config.get("page_jobs")
		intro = render_blocks(config, (page or {}).get("blocks"), lang)
		cards = (
			"".join(
				f'<div class="card"><a href="{_href(prefix, "jobs/" + j["name"], lang, src)}"><b>{esc(j.get("job_title"))}</b></a>'
				+ (f"<br>{esc(j.get('location'))}" if j.get("location") else "")
				+ (
					f"<br>{esc(t(bi('Closes', 'Cierra'), lang))} {esc(j.get('closes_on'))}"
					if j.get("closes_on")
					else ""
				)
				+ "</div>"
				for j in jobs
			)
			or f"<p>{esc(t(bi('No openings right now.', 'No hay vacantes por ahora.'), lang))}</p>"
		)
		inner = f"<h1>{esc(t(bi('Jobs', 'Empleos'), lang))}</h1>{intro}{cards}"
		return Page(
			200, shell(config, t(bi("Jobs", "Empleos"), lang), inner, lang=lang, src=src, current="jobs")
		)

	slug = head or "home"
	page = next((p for p in pages(config) if p.get("slug") == slug), None)
	if not page or (page.get("module") in ("about", "crew") and not module_on(config, page["module"])):
		return not_found
	inner = f"<h1>{esc(t(page.get('title'), lang))}</h1>" if slug != "home" else ""
	inner += render_blocks(config, page.get("blocks"), lang)
	return Page(200, shell(config, t(page.get("title"), lang), inner, lang=lang, src=src, current=slug))


def _guard(config: dict, form: str, values: dict, ip: str, now: float | None) -> str:
	"""The spam checks: honeypot (silently accepted and dropped), time token, rate limits."""
	if str(values.get("website") or "").strip():
		return "spam"
	reason = check_token(form, values.get("_token"), int(site(config)["min_seconds_to_submit"]), now)
	if reason:
		return reason
	return over_limit(config, client_hash(ip))


def _photo_bytes(files: dict | None, cap: int) -> bytes | None:
	"""The first uploaded file, read to at most `cap` + 1 bytes — never the whole of an oversized upload."""
	for upload in (files or {}).values():
		data = upload.read(cap + 1) if hasattr(upload, "read") else bytes(upload or b"")[: cap + 1]
		if data:
			return bytes(data)
	return None
