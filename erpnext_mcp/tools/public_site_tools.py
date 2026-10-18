# SPDX-License-Identifier: MIT
"""MCP tools for the public website. v0.335.0. See `erpnext_mcp/public_site.py`.

EVERYTHING IS BUILDABLE OVER MCP. Settings, pages, content blocks, crew-information
entries, form fields and their order, categories, routing, QR codes and job
openings are drafted, previewed and published here, and the submissions read back.

THE LIFECYCLE IS THE SHARED ONE (`Farm Config Version`, kind Public Site): every
edit lands in the key's open Draft; `preview_public_site` renders the drafts over
what is live; `publish_public_site` makes them live (its own switch, default OFF,
which is how an operator lets an AI publish this kind); `rollback_public_site`
goes back one version; `unpublish_public_site_page` retires a page.

WHAT NO TOOL DOES: switch the site on. `public_site_enabled` is ticked by a person
in ERPNext MCP Settings. Until then nothing is served, whatever is published.
"""

from __future__ import annotations

import base64
import copy
import json

import frappe

from .. import compat, config_lifecycle, phone_config, public_site, security
from ..args import as_bool, as_int, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

KIND = public_site.KIND


def _require() -> None:
	if not phone_config.ready():
		raise ToolError("this site has no Farm Config Version doctype — run `bench migrate`.")


def _actor() -> str:
	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "") or "Administrator"


def _current(key: str, default=None) -> dict:
	"""The body an edit starts from: the open draft, else what is live, else the neutral default."""
	drafts = public_site.bodies(drafts=True)
	if key in drafts:
		return copy.deepcopy(drafts[key])
	if default is not None:
		return copy.deepcopy(default)
	defaults = public_site.all_defaults()
	if key in defaults:
		return copy.deepcopy(defaults[key])
	raise ToolError(f"there is no public-site config {key!r} yet.")


def _save(key: str, body: dict, note: str) -> dict:
	try:
		doc, report = phone_config.save_draft(KIND, key, body, note, authored_by=config_lifecycle.AI)
	except phone_config.ConfigError as exc:
		raise ToolError(f"{exc}\nNothing was saved.") from None
	return {
		"config_version": phone_config.version_string(doc),
		"status": doc.status,
		"warnings": report.get("warnings") or [],
	}


def _bilingual(value, label: str) -> dict:
	"""`{en, es}` from a dict or a JSON string; a plain string is English."""
	if value in (None, ""):
		return {}
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			return {"en": value, "es": ""}
	if not isinstance(value, dict) or not set(value) <= {"en", "es"}:
		raise ToolError(f'{label} is {{"en": …, "es": …}} or plain English text.')
	return {"en": str(value.get("en") or ""), "es": str(value.get("es") or "")}


def _obj(args: dict, key: str):
	raw = args.get(key)
	if isinstance(raw, str) and raw.strip().startswith(("{", "[")):
		try:
			return json.loads(raw)
		except ValueError:
			raise ToolError(f"{key} is not valid JSON.") from None
	return raw


# ── setup ────────────────────────────────────────────────────────────────────


def configure_public_site(args: dict) -> ToolResult:
	"""The setup wizard: the site settings draft, plus neutral pages and forms where none exist yet."""
	_require()
	body = _current(public_site.SITE_KEY)
	if as_str(args, "company"):
		company = as_str(args, "company")
		if not frappe.db.exists("Company", company):
			raise ToolError(f"no Company {company!r} on this site. Nothing was saved.")
		body["company"] = company
	brand = body.setdefault("brand", {})
	for arg, key in (
		("brand_name", "name"),
		("logo", "logo"),
		("primary_color", "primary_color"),
		("accent_color", "accent_color"),
	):
		if as_str(args, arg):
			brand[key] = as_str(args, arg)
	if args.get("tagline") not in (None, ""):
		brand["tagline"] = _bilingual(args.get("tagline"), "tagline")
	for key in ("route_prefix", "base_url", "default_language"):
		if as_str(args, key):
			body[key] = as_str(args, key).rstrip("/") if key == "base_url" else as_str(args, key)
	if args.get("languages") not in (None, ""):
		languages = _obj(args, "languages")
		body["languages"] = [
			str(code) for code in (languages if isinstance(languages, list) else str(languages).split(","))
		]
	modules = _obj(args, "modules")
	if isinstance(modules, dict):
		body.setdefault("modules", {}).update({str(k): bool(v) for k, v in modules.items()})
	contact = _obj(args, "contact")
	if isinstance(contact, dict):
		merged = body.setdefault("contact", {})
		for key in ("phone", "email"):
			if key in contact:
				merged[key] = str(contact[key] or "")
		if "address" in contact:
			merged["address"] = _bilingual(contact["address"], "contact.address") or {"en": "", "es": ""}
	sources = _obj(args, "qr_sources")
	if isinstance(sources, list):
		body["qr_sources"] = [
			{"tag": str(s.get("tag") or ""), "label": str(s.get("label") or s.get("tag") or "")}
			for s in sources
			if isinstance(s, dict)
		]
	for key in (
		"max_upload_mb",
		"submissions_per_visitor_per_hour",
		"submissions_per_hour",
		"min_seconds_to_submit",
	):
		if args.get(key) not in (None, ""):
			body[key] = as_int(args, key)
	if args.get("set_home_page") not in (None, ""):
		body["set_home_page"] = bool(as_bool(args, "set_home_page"))
	result = {"site": _save(public_site.SITE_KEY, body, "configure_public_site")}

	created = []
	if as_bool(args, "seed_defaults", True):
		existing = {row["config_key"] for row in phone_config.rows(KIND)}
		defaults = {**public_site.default_pages(), **public_site.default_forms(body.get("company") or "")}
		for key, default in defaults.items():
			if key not in existing:
				_save(key, default, "neutral default from configure_public_site")
				created.append(key)
	result["defaults_drafted"] = created
	public_site.ensure_applicant_fields()
	result["next_step"] = (
		"preview_public_site shows any page as it would render. publish_public_site makes the drafts live "
		"(its switch is off by default). Nothing is served until a person ticks Public Site Enabled in "
		"ERPNext MCP Settings."
	)
	return ToolResult(result, f"public site settings drafted; {len(created)} default page(s)/form(s) drafted")


def get_public_site(args: dict) -> ToolResult:
	"""What is live, what is drafted, whether the site is on, and which paths must be reachable."""
	_require()
	live = public_site.bodies()
	drafts = public_site.bodies(drafts=True)
	cfg = public_site.site(drafts)
	rows = [
		phone_config.describe(frappe.get_doc(phone_config.DOCTYPE, row["name"]))
		for row in phone_config.rows(KIND)
	]
	warnings = []
	company = public_site.site(live).get("company")
	if company and compat.doctype_exists(public_site.JOB_OPENING):
		others = frappe.db.count(
			public_site.JOB_OPENING, {"publish": 1, "status": "Open", "company": ("!=", company)}
		)
		if others:
			warnings.append(
				f"{others} published Job Opening(s) belong to other companies; this site never shows them."
			)
	data = {
		"enabled": public_site.enabled(),
		"live_keys": sorted(live),
		"draft_keys": sorted(key for key in drafts if drafts.get(key) != live.get(key)),
		"site": cfg,
		"public_url": (cfg.get("base_url") or "") + f"/{cfg['route_prefix']}",
		"funnel_paths": public_site.funnel_paths(drafts),
		"versions": rows if as_bool(args, "include_versions", False) else len(rows),
		"open_jobs": public_site.open_jobs(live),
		"warnings": warnings,
		"note": (
			"Only funnel_paths need to be reachable from the internet; nothing else on this site is used by "
			"the public site. Go-live is Public Site Enabled in ERPNext MCP Settings, ticked by a person."
		),
	}
	return ToolResult(
		data,
		f"public site {'ON' if data['enabled'] else 'off'}; {len(data['live_keys'])} live, "
		f"{len(data['draft_keys'])} drafted",
	)


def preview_public_site(args: dict) -> ToolResult:
	"""One path rendered as the visitor would see it — drafts over what is live, unless drafts=false."""
	_require()
	drafts = bool(as_bool(args, "drafts", True))
	path = as_str(args, "path").strip("/")
	config = public_site.bodies(drafts=drafts)
	if public_site.SITE_KEY not in config:
		config[public_site.SITE_KEY] = public_site.default_site()
	page = public_site.render(
		path, form={"lang": as_str(args, "lang"), "src": as_str(args, "src")}, config=config, drafts=True
	)
	body = page.body if isinstance(page.body, str) else ""
	return ToolResult(
		{
			"path": "/" + public_site.site(config)["route_prefix"] + ("/" + path if path else ""),
			"status": page.status,
			"drafts": drafts,
			"html": body,
			"bytes": len(body.encode()),
		},
		f"previewed /{path or ''} ({page.status}, {'drafts' if drafts else 'live'})",
	)


# ── pages ────────────────────────────────────────────────────────────────────


def _page_key(slug: str) -> str:
	return f"page_{slug.replace('-', '_')}"


def set_public_site_page(args: dict) -> ToolResult:
	"""Create or change a page draft: title, order, menu, and its blocks (replace, or one block operation)."""
	_require()
	slug = as_str(args, "slug", required=True).lower()
	key = _page_key(slug)
	body = _current(
		key,
		default={
			"schema_version": 1,
			"key": key,
			"slug": slug,
			"module": "custom",
			"order": 50,
			"nav": True,
			"title": {"en": slug.title(), "es": ""},
			"blocks": [],
		},
	)
	if args.get("title") not in (None, ""):
		body["title"] = _bilingual(args.get("title"), "title")
	if as_str(args, "module"):
		body["module"] = as_str(args, "module")
	if args.get("order") not in (None, ""):
		body["order"] = as_int(args, "order")
	if args.get("nav") not in (None, ""):
		body["nav"] = bool(as_bool(args, "nav"))
	blocks = _obj(args, "blocks")
	if blocks is not None and blocks != "":
		if not isinstance(blocks, list):
			raise ToolError("blocks is a list of blocks. Nothing was saved.")
		body["blocks"] = [_block(block) for block in blocks]
	op = as_str(args, "block_op")
	if op:
		body["blocks"] = _block_op(body.get("blocks") or [], op, args)
	saved = _save(key, body, as_str(args, "note") or "set_public_site_page")
	return ToolResult(
		{"slug": slug, "key": key, **saved, "blocks": len(body.get("blocks") or [])},
		f"page {slug!r} drafted ({saved['config_version']})",
	)


def _block(raw) -> dict:
	if not isinstance(raw, dict):
		raise ToolError("each block is an object with a type. Nothing was saved.")
	block = {"type": str(raw.get("type") or "text")}
	for key in ("text", "alt"):
		if raw.get(key) not in (None, ""):
			block[key] = _bilingual(raw[key], f"block.{key}")
	if raw.get("items"):
		block["items"] = [_bilingual(item, "block.items[]") for item in raw["items"]]
	for key in ("image", "entry"):
		if raw.get(key):
			block[key] = str(raw[key])
	return block


def _block_op(blocks: list, op: str, args: dict) -> list:
	blocks = list(blocks)
	index = as_int(args, "index", None)
	if op == "add":
		block = _block(_obj(args, "block"))
		blocks.insert(len(blocks) if index is None else max(0, min(index, len(blocks))), block)
		return blocks
	if index is None or not 0 <= index < len(blocks):
		raise ToolError(f"index must point at one of the {len(blocks)} block(s). Nothing was saved.")
	if op == "update":
		blocks[index] = {**blocks[index], **_block({**blocks[index], **(_obj(args, "block") or {})})}
	elif op == "remove":
		blocks.pop(index)
	elif op == "move":
		to = as_int(args, "to", None)
		if to is None or not 0 <= to < len(blocks):
			raise ToolError("to must be a block position. Nothing was saved.")
		blocks.insert(to, blocks.pop(index))
	else:
		raise ToolError("block_op is add, update, remove or move. Nothing was saved.")
	return blocks


def set_crew_info_entry(args: dict) -> ToolResult:
	"""One crew-information entry (a heading and its text) on the crew page: add, update or remove."""
	_require()
	entry = as_str(args, "entry", required=True).lower()
	op = as_str(args, "op") or "upsert"
	key = _page_key("crew")
	body = _current(key)
	blocks = [b for b in body.get("blocks") or [] if b.get("entry") != entry]
	position = next((i for i, b in enumerate(body.get("blocks") or []) if b.get("entry") == entry), None)
	if op != "remove":
		title = _bilingual(args.get("title"), "title")
		text = _bilingual(args.get("text"), "text")
		if not title.get("en") or not text.get("en"):
			raise ToolError("title and text are required (English at least). Nothing was saved.")
		new = [
			{"type": "heading", "text": title, "entry": entry},
			{"type": "text", "text": text, "entry": entry},
		]
		at = position if position is not None else len(blocks)
		blocks[at:at] = new
	elif position is None:
		raise ToolError(f"there is no crew entry {entry!r}. Nothing was saved.")
	body["blocks"] = blocks
	saved = _save(key, body, f"crew entry {entry} {op}")
	return ToolResult({"entry": entry, "op": op, **saved}, f"crew entry {entry!r} {op} drafted")


def list_public_site_pages(args: dict) -> ToolResult:
	_require()
	live = public_site.bodies()
	drafts = public_site.bodies(drafts=True)
	rows = []
	for page in public_site.pages(drafts):
		key = _page_key(page["slug"])
		rows.append(
			{
				"slug": page["slug"],
				"title": page.get("title"),
				"module": page.get("module"),
				"order": page.get("order"),
				"nav": page.get("nav"),
				"blocks": len(page.get("blocks") or []),
				"live": key in live,
				"draft_differs": drafts.get(key) != live.get(key),
			}
		)
	return ToolResult({"pages": rows, "count": len(rows)}, f"{len(rows)} page(s)")


def unpublish_public_site_page(args: dict) -> ToolResult:
	"""Take a page off the site (its versions are kept; rollback_public_site brings it back)."""
	_require()
	_require_publish()
	slug = as_str(args, "slug", required=True).lower()
	if slug == "home":
		raise ToolError("home cannot be unpublished; edit it instead. Nothing was changed.")
	try:
		moved = phone_config.retire(
			KIND, _page_key(slug), as_str(args, "change_note") or "unpublished", _actor()
		)
	except phone_config.ConfigError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	return ToolResult(
		{"slug": slug, "retired": moved}, f"page {slug!r} unpublished ({len(moved)} version(s) retired)"
	)


# ── forms ────────────────────────────────────────────────────────────────────


def set_public_site_form(args: dict) -> ToolResult:
	"""A guest form's wording, fields (add / update / remove / move), categories and routing."""
	_require()
	form = as_str(args, "form", required=True).lower()
	if form not in public_site.FORMS:
		raise ToolError(f"form is one of {', '.join(public_site.FORMS)}.")
	key = f"form_{form}"
	body = _current(key)
	for arg in ("title", "intro", "thanks", "acknowledgement"):
		if args.get(arg) not in (None, ""):
			body[arg] = _bilingual(args.get(arg), arg)
	if args.get("show_rei_warning") not in (None, ""):
		body["show_rei_warning"] = bool(as_bool(args, "show_rei_warning"))
	fields = list(body.get("fields") or [])
	op = as_str(args, "field_op")
	if op:
		name = as_str(args, "field", required=True)
		position = next((i for i, f in enumerate(fields) if f.get("name") == name), None)
		if op in ("add", "update"):
			raw = _obj(args, "definition") or {}
			if not isinstance(raw, dict):
				raise ToolError("definition is an object. Nothing was saved.")
			base = (
				fields[position]
				if position is not None
				else {"name": name, "type": "text", "required": False}
			)
			if op == "add" and position is not None:
				raise ToolError(f"field {name!r} already exists — use field_op=update. Nothing was saved.")
			if op == "update" and position is None:
				raise ToolError(f"there is no field {name!r}. Nothing was saved.")
			field = dict(base)
			for key_ in ("type", "target"):
				if raw.get(key_) not in (None, ""):
					field[key_] = str(raw[key_])
			if "required" in raw:
				field["required"] = bool(raw["required"])
			if raw.get("label") not in (None, ""):
				field["label"] = _bilingual(raw["label"], "label")
			if raw.get("options") is not None:
				field["options"] = [
					{"value": str(o.get("value")), "label": _bilingual(o.get("label"), "option label")}
					for o in raw["options"]
					if isinstance(o, dict)
				]
			if position is None:
				index = as_int(args, "index", None)
				fields.insert(len(fields) if index is None else max(0, min(index, len(fields))), field)
			else:
				fields[position] = field
		elif op == "remove":
			if position is None:
				raise ToolError(f"there is no field {name!r}. Nothing was saved.")
			fields.pop(position)
		elif op == "move":
			to = as_int(args, "to", None)
			if position is None or to is None or not 0 <= to < len(fields):
				raise ToolError("move needs an existing field and a position `to`. Nothing was saved.")
			fields.insert(to, fields.pop(position))
		else:
			raise ToolError("field_op is add, update, remove or move. Nothing was saved.")
	categories = _obj(args, "categories")
	if isinstance(categories, list):
		target = "purpose" if form == "visit" else "category"
		field = next((f for f in fields if f.get("name") == target), None)
		if field is None:
			raise ToolError(f"the {form} form has no {target!r} field for categories. Nothing was saved.")
		field["type"] = "select"
		field["options"] = [
			{"value": str(c.get("value")), "label": _bilingual(c.get("label"), "category label")}
			for c in categories
			if isinstance(c, dict)
		]
	body["fields"] = fields
	routing = _obj(args, "routing")
	if isinstance(routing, dict):
		body["routing"] = {**(body.get("routing") or {}), **routing}
	saved = _save(key, body, as_str(args, "note") or "set_public_site_form")
	return ToolResult(
		{"form": form, **saved, "fields": [f["name"] for f in fields], "routing": body.get("routing")},
		f"{form} form drafted ({saved['config_version']})",
	)


# ── publish ──────────────────────────────────────────────────────────────────


def _require_publish() -> None:
	from .. import settings

	if not settings.tool_enabled("publish_public_site"):
		raise ToolError(
			"publishing the public site over MCP is switched off (Publish Public Site in ERPNext MCP Settings, "
			"off by default). The drafts are saved and preview_public_site shows them; a person publishes them "
			"in the Desk (Farm Config Version → Publish) or turns the switch on. Nothing was changed."
		)


def publish_public_site(args: dict) -> ToolResult:
	"""Make the drafts live (all, or the keys named). Switch-gated, default OFF. Does NOT switch the site on."""
	_require()
	_require_publish()
	change_note = as_str(args, "change_note")
	if len(change_note) < 10:
		raise ToolError("change_note (10+ characters) says what changed. Nothing was published.")
	wanted = _obj(args, "keys")
	rows = phone_config.rows(KIND, "", phone_config.DRAFT)
	if isinstance(wanted, list) and wanted:
		rows = [row for row in rows if row["config_key"] in wanted]
	# The site settings first: a page cannot be validated for publish against settings that do not exist.
	rows.sort(key=lambda row: (row["config_key"] != public_site.SITE_KEY, row["config_key"]))
	published, refused = [], []
	for row in rows:
		try:
			doc, _previous, _same = phone_config.publish(
				KIND, row["config_key"], row["version"], change_note, _actor()
			)
			published.append(phone_config.version_string(doc))
		except phone_config.ConfigError as exc:
			refused.append({"key": row["config_key"], "reason": str(exc)})
	_apply_home_page()
	data = {
		"published": published,
		"refused": refused,
		"enabled": public_site.enabled(),
		"note": "Live content changed; the site is served only while Public Site Enabled is ticked (a person, in the Desk).",
	}
	return ToolResult(data, f"published {len(published)} public-site version(s); {len(refused)} refused")


def rollback_public_site(args: dict) -> ToolResult:
	_require()
	_require_publish()
	key = as_str(args, "key", required=True)
	try:
		target, was = phone_config.rollback(KIND, key, as_str(args, "change_note") or "rolled back", _actor())
	except phone_config.ConfigError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	_apply_home_page()
	return ToolResult(
		{"key": key, "live": phone_config.version_string(target), "was": was},
		f"{key} rolled back to {phone_config.version_string(target)}",
	)


def _apply_home_page() -> None:
	"""Point the website's `/` at the public site when its settings ask, and only then."""
	if not compat.doctype_exists("Website Settings"):
		return
	cfg = public_site.site(public_site.bodies())
	current = str(frappe.db.get_single_value("Website Settings", "home_page") or "")
	if cfg.get("set_home_page") and public_site.SITE_KEY in public_site.bodies():
		if current != cfg["route_prefix"]:
			frappe.db.set_single_value("Website Settings", "home_page", cfg["route_prefix"])
	elif current and current == cfg.get("route_prefix"):
		frappe.db.set_single_value("Website Settings", "home_page", "")


# ── jobs ─────────────────────────────────────────────────────────────────────


def create_public_job_opening(args: dict) -> ToolResult:
	"""A Job Opening for the public company, shown on the site while Open and published."""
	if not compat.doctype_exists(public_site.JOB_OPENING):
		raise ToolError("this site has no Job Opening doctype — HRMS is not installed.")
	cfg = public_site.site(public_site.bodies(drafts=True))
	company = as_str(args, "company") or cfg.get("company")
	if not company:
		raise ToolError(
			"the public site has no company yet — configure_public_site(company=...). Nothing was created."
		)
	if company != cfg.get("company"):
		raise ToolError(
			f"{company} is not the public site's company ({cfg.get('company')}); only its openings are "
			"shown. Nothing was created."
		)
	designation = as_str(args, "designation", required=True)
	if not frappe.db.exists("Designation", designation):
		raise ToolError(f"no Designation {designation!r}. Nothing was created.")
	description = _bilingual(args.get("description"), "description")
	html_ = "".join(
		f'<p lang="{lang}">{public_site.esc(text)}</p>' for lang, text in description.items() if text
	)
	doc = frappe.new_doc(public_site.JOB_OPENING)
	doc.update(
		{
			"job_title": as_str(args, "job_title", required=True),
			"company": company,
			"designation": designation,
			"status": "Open",
			"publish": 1 if as_bool(args, "publish", True) else 0,
			"description": html_,
		}
	)
	for key in ("closes_on", "location", "employment_type"):
		if as_str(args, key) and compat.has_field(public_site.JOB_OPENING, key):
			doc.set(key, as_str(args, key))
	if args.get("planned_vacancies") not in (None, "") and compat.has_field(
		public_site.JOB_OPENING, "planned_vacancies"
	):
		doc.planned_vacancies = as_int(args, "planned_vacancies")
	doc.insert(ignore_permissions=True)
	return ToolResult(
		{"name": doc.name, "job_title": doc.job_title, "company": company, "publish": doc.publish},
		f"job opening {doc.name} created",
		docstatus_delta="none → 0 (created)",
	)


def close_public_job_opening(args: dict) -> ToolResult:
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(public_site.JOB_OPENING, name):
		raise ToolError(f"no Job Opening {name!r}.")
	values = {"status": "Closed", "publish": 0}
	if compat.has_field(public_site.JOB_OPENING, "closed_on"):
		values["closed_on"] = frappe.utils.today()
	frappe.db.set_value(public_site.JOB_OPENING, name, values)
	return ToolResult({"name": name, "status": "Closed"}, f"job opening {name} closed")


# ── submissions ──────────────────────────────────────────────────────────────

_ENTRY_FIELDS = [
	"name",
	"entry_type",
	"status",
	"company",
	"submitted_at",
	"language",
	"source",
	"category",
	"description",
	"location",
	"photo",
	"contact_name",
	"contact_phone",
	"contact_email",
	"job_opening",
	"visitor_company",
	"purpose",
	"host",
	"vehicle",
	"areas_visited",
	"check_in",
	"check_out",
	"acknowledged",
	"rei_warning",
	"scan_date",
	"hits",
	"linked_doctype",
	"linked_document",
	"notes",
]


def list_public_site_entries(args: dict) -> ToolResult:
	"""Problem reports, visitor logs, applications and scan counts, newest first."""
	if not compat.doctype_exists(public_site.ENTRY):
		raise ToolError("this site has no Public Site Entry doctype — run `bench migrate`.")
	filters = {}
	kind = as_str(args, "entry_type")
	if kind:
		filters["entry_type"] = kind.title() if kind.lower() != "application" else "Application"
	for key in ("status", "company", "source"):
		if as_str(args, key):
			filters[key] = as_str(args, key)
	if as_bool(args, "on_site", False):
		filters.update({"entry_type": "Visit", "check_out": ("is", "not set")})
	rows = frappe.db.get_all(
		public_site.ENTRY,
		filters=filters,
		fields=compat.existing_fields(public_site.ENTRY, _ENTRY_FIELDS),
		order_by="submitted_at desc",
		limit=as_limit(args),
	)
	return ToolResult(
		{"entries": rows, "count": len(rows)},
		f"{len(rows)} public-site entr{'y' if len(rows) == 1 else 'ies'}",
	)


def get_public_site_entry(args: dict) -> ToolResult:
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(public_site.ENTRY, name):
		raise ToolError(f"no Public Site Entry {name!r}.")
	row = frappe.db.get_value(
		public_site.ENTRY,
		name,
		compat.existing_fields(public_site.ENTRY, [*_ENTRY_FIELDS, "answers_json", "acknowledgement_text"]),
		as_dict=True,
	)
	row = dict(row)
	try:
		row["answers"] = json.loads(row.pop("answers_json") or "{}")
	except ValueError:
		row["answers"] = {}
	return ToolResult(row, f"{row['entry_type']} {name}")


def update_public_site_entry(args: dict) -> ToolResult:
	"""Triage: status, notes, check a visitor out, or file a report as a Farm Task."""
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(public_site.ENTRY, name):
		raise ToolError(f"no Public Site Entry {name!r}.")
	values = {}
	status = as_str(args, "status")
	if status:
		if status not in ("New", "Triaged", "Closed", "Spam"):
			raise ToolError("status is New, Triaged, Closed or Spam.")
		values["status"] = status
	if as_str(args, "notes"):
		values["notes"] = as_str(args, "notes")
	entry_type = frappe.db.get_value(public_site.ENTRY, name, "entry_type")
	if as_bool(args, "check_out", False):
		if entry_type != "Visit":
			raise ToolError(f"{name} is a {entry_type}, not a visit.")
		values["check_out"] = frappe.utils.now()
	if values:
		frappe.db.set_value(public_site.ENTRY, name, values)
	promoted = None
	if as_bool(args, "promote", False):
		if entry_type != "Report":
			raise ToolError(f"{name} is a {entry_type}; only a report is filed as a Farm Task.")
		routing = (public_site.bodies().get("form_report") or {}).get("routing") or {}
		if as_str(args, "reporter_employee"):
			routing = {**routing, "reporter_employee": as_str(args, "reporter_employee")}
		if as_str(args, "task_type"):
			routing = {**routing, "task_type": as_str(args, "task_type")}
		try:
			promoted = public_site.promote_report(name, routing)
		except (public_site.SiteError, ToolError) as exc:
			raise ToolError(f"{exc} The entry was kept as it is.") from None
	return ToolResult(
		{"name": name, "updated": sorted(values), "promoted": promoted},
		f"{name} updated" + (f"; filed as {promoted['doctype']} {promoted['name']}" if promoted else ""),
	)


# ── QR ───────────────────────────────────────────────────────────────────────


def generate_public_site_qr(args: dict) -> ToolResult:
	"""A QR code for the public site (or one page), tagged with a source; PNG, CR80 card PDF or a sign PDF."""
	from ..render import qr

	cfg = public_site.site(public_site.bodies(drafts=True))
	base = str(cfg.get("base_url") or "").rstrip("/")
	if not base:
		raise ToolError("base_url is not set — configure_public_site(base_url=...) first.")
	src = as_str(args, "src")
	sources = {s.get("tag"): s.get("label") for s in cfg.get("qr_sources") or []}
	if src and src not in sources:
		raise ToolError(
			f"src {src!r} is not one of the configured QR sources ({', '.join(sources) or 'none'}): "
			"add it with configure_public_site(qr_sources=...)."
		)
	path = as_str(args, "path").strip("/")
	url = f"{base}/{cfg['route_prefix']}" + (f"/{path}" if path else "") + (f"?src={src}" if src else "")
	fmt = as_str(args, "format") or "png"
	if not qr.available():
		raise ToolError("no QR encoder is installed on this server (segno).")
	rendered = qr.render(url)
	data = {"url": url, "src": src or None, "format": fmt}
	brand = cfg.get("brand") or {}
	caption = as_str(args, "caption") or public_site.t(
		public_site.bi("Scan to visit us", "Escanee para visitarnos"), as_str(args, "lang") or "en"
	)
	if fmt == "png":
		data["png_base64"] = base64.b64encode(rendered["png"]).decode("ascii")
	elif fmt in ("card_pdf", "sign_pdf"):
		from .. import card_art

		sides = (
			_card(brand, rendered["matrix"], caption, url, sources.get(src))
			if fmt == "card_pdf"
			else _sign(brand, rendered["matrix"], caption, url)
		)
		try:
			pdf = card_art.to_pdf(sides, title=f"{brand.get('name')} QR")
		except card_art.ArtError as exc:
			raise ToolError(str(exc)) from None
		data["pdf_base64"] = base64.b64encode(pdf).decode("ascii")
		data["pdf_bytes"] = len(pdf)
	else:
		raise ToolError("format is png, card_pdf or sign_pdf.")
	return ToolResult(data, f"QR for {url} ({fmt})")


def _card(brand: dict, matrix: list, caption: str, url: str, source_label: str | None) -> list:
	from .. import card_art

	w, h = card_art.CARD_W, card_art.CARD_H
	front = [("rect", 0, 0, w, h, "#FFFFFF", None), ("qr", w - 30.0, (h - 25.0) / 2, 25.0, matrix)]
	front.append(
		card_art._text(
			5, h - 12, str(brand.get("name") or ""), "Helvetica-Bold", 11, card_art.BLACK, "left", w - 38, 6
		)
	)
	front.append(card_art._text(5, h - 18, caption, "Helvetica", 7.5, card_art.BLACK, "left", w - 38, 5))
	if source_label:
		front.append(card_art._text(5, 6, source_label, "Helvetica", 6, card_art.GREY, "left", w - 38, 4))
	return [front]


def _sign(brand: dict, matrix: list, caption: str, url: str) -> list:
	from .. import card_art

	w, h = 215.9, 279.4
	ops = [("rect", 0, 0, w, h, "#FFFFFF", None), ("qr", (w - 140) / 2, 80.0, 140.0, matrix)]
	ops.append(
		card_art._text(
			w / 2,
			h - 40,
			str(brand.get("name") or ""),
			"Helvetica-Bold",
			30,
			card_art.BLACK,
			"center",
			w - 30,
			12,
		)
	)
	ops.append(card_art._text(w / 2, h - 58, caption, "Helvetica", 18, card_art.BLACK, "center", w - 30, 9))
	ops.append(card_art._text(w / 2, 60, url, "Helvetica", 10, card_art.GREY, "center", w - 30, 6))
	return [ops]
