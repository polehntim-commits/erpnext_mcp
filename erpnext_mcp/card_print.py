# SPDX-License-Identifier: MIT
"""The card print queue. v0.208.0.

docs/design/card_print_queue.md. ERPNext holds the queue; one Mac's agent claims
the oldest job for its station, prints it through CUPS and reports back; the
phone and the Desk only ever ask. Every card has a row and rows are never
deleted.

  Queued ─claim→ Printing ─complete→ Printed | Failed | back to Queued (retry)
  Queued ─cancel→ Cancelled        Failed ─retry→ Queued

THE THREE DOORS — Desk and the agent (`api/card_print.py`), the phone
(`api/mobile.py`) and MCP (`tools/card_prints.py`) — all land here, so the
permission, idempotency, reprint and routing rules exist once.
"""

from __future__ import annotations

import base64
import hashlib

import frappe

from . import card_art, compat

JOB = "Card Print Job"
STATION = "Card Print Station"
JOB_TYPES = {"Employee ID": "Employee", "Asset Tag": "Asset Register"}
QUEUED, PRINTING, PRINTED, FAILED, CANCELLED = "Queued", "Printing", "Printed", "Failed", "Cancelled"
#: v0.209.0. A card PDF downloaded to print by hand — recorded, never queued.
DOWNLOADED = "Downloaded"
STATUSES = (QUEUED, PRINTING, PRINTED, FAILED, CANCELLED, DOWNLOADED)
SIDES = ("Single", "Dual")
PAGES = ("Both", "Front", "Back")
#: Amendment 4 §D1. Manual: no agent — a person downloads the PDF, prints it, marks the job.
MANUAL = "Manual"
DUPLEX_MODES = (MANUAL, "Simplex", "Duplex")
SOURCES = ("iOS", "Desk", "API")
REQUESTER_ROLE = "Card Print Requester"
STATION_ROLE = "Card Print Station"
ADMIN_ROLE = "System Manager"
#: Amendment 4 §D3. Who may ask for a card, see the queue, or close a job. Nobody else.
REQUEST_ROLES = (REQUESTER_ROLE, "Farm Manager", ADMIN_ROLE)
ROLES = (REQUESTER_ROLE, STATION_ROLE)
MAX_COPIES = 5
MAX_ATTEMPTS = 3
STUCK_MINUTES = 10
OFFLINE_SECONDS = 120
PER_MINUTE = 10
PER_DAY = 100
READY = "Ready"
STATION_STATES = (READY, "Paused", "Printer error", "Offline", MANUAL)
DEFAULT_STATION = "primacy2-main"
#: v0.226.0. How an asset tag is made: Card on the card printer; the other two from a QR sheet.
CARD, OUTDOOR, SHEET = "Card", "Outdoor label", "Sheet"
TAG_FORMATS = (CARD, OUTDOOR, SHEET)
HOUSING_UNIT = "Housing Unit"
REPRINT_MARKER = "reprint_reason_required"
ROW_FIELDS = [
	"name",
	"job_type",
	"reference_doctype",
	"reference_name",
	"reference_title",
	"company",
	"status",
	"print_station",
	"copies",
	"sides",
	"requested_by",
	"requested_from",
	"client_request_id",
	"claimed_at",
	"claimed_by",
	"printed_at",
	"attempts",
	"error",
	"is_reprint",
	"reprint_reason",
	"pages",
	"back_pending",
	"front_job",
	"creation",
]


class CardPrintError(Exception):
	"""A refusal. `kind` is forbidden / not_found / invalid, for the door's status code."""

	def __init__(self, message: str, kind: str = "invalid"):
		super().__init__(message)
		self.kind = kind


# ── small helpers ───────────────────────────────────────────────────────────
def ready() -> bool:
	return compat.doctype_exists(JOB) and compat.doctype_exists(STATION)


def lines(raw) -> list:
	if isinstance(raw, (list, tuple)):
		items = raw
	else:
		items = str(raw or "").replace(",", "\n").splitlines()
	return [str(item).strip() for item in items if str(item).strip()]


def roles_of(user: str) -> set:
	"""Every role on the account: `frappe.get_roles`, and `Has Role` when that is empty
	(the same fallback `guard.roles_held` keeps)."""
	try:
		held = set(frappe.get_roles(user) or [])
	except Exception:
		held = set()
	if not held:
		try:
			from . import roles as role_lib

			held = set(role_lib.all_roles_of(user) or [])
		except Exception:
			held = set()
	return held


def can_request(user: str) -> bool:
	return bool(roles_of(user) & set(REQUEST_ROLES))


def require_requester(user: str) -> None:
	if not user or user == "Guest" or not can_request(user):
		raise CardPrintError(
			f"{user or 'this account'} may not print cards or see the print queue: that takes the "
			f"{REQUESTER_ROLE} role (or Farm Manager / System Manager). An ID card is a credential, so "
			"asking for one is permissioned. Nothing was queued.",
			"forbidden",
		)


def require_station(user: str) -> None:
	if STATION_ROLE not in roles_of(user):
		raise CardPrintError(
			f"only a print station ({STATION_ROLE}) may claim or complete jobs.", "forbidden"
		)


def _seconds_since(stamp) -> float | None:
	if not stamp:
		return None
	try:
		now = frappe.utils.get_datetime(frappe.utils.now())
		return (now - frappe.utils.get_datetime(str(stamp))).total_seconds()
	except Exception:
		return None


# ── stations and routing ────────────────────────────────────────────────────
def stations() -> list:
	if not ready():
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			STATION,
			fields=[
				"name",
				"enabled",
				"priority",
				"job_types",
				"companies",
				"media",
				"artwork_width_mm",
				"artwork_height_mm",
				"duplex",
				"front_orientation",
				"back_orientation",
				"last_seen_at",
				"printer_state",
				"printer_message",
				"agent_version",
			],
			limit=200,
		)
	]


def station_state(row: dict) -> dict:
	"""What a requester is shown: Ready / Paused / Printer error / Offline."""
	age = _seconds_since(row.get("last_seen_at"))
	state = str(row.get("printer_state") or "")
	if is_manual(row):
		state, message = MANUAL, "printed by hand from ERPNext: download the card PDF, print it, mark the job"
	elif age is None or age > OFFLINE_SECONDS:
		state, message = (
			"Offline",
			(
				"the print station has not checked in — cards will print when it is back"
				if age is not None
				else "the print station has never checked in"
			),
		)
	else:
		state = state if state in STATION_STATES else "Printer error"
		message = str(row.get("printer_message") or "")
	return {
		"station": row.get("name"),
		"state": state,
		"message": message,
		"last_seen_at": str(row.get("last_seen_at") or "") or None,
		"media": row.get("media"),
		"job_types": lines(row.get("job_types")),
		"enabled": bool(compat.checked(row.get("enabled"))),
		"duplex": row.get("duplex") or MANUAL,
	}


def is_manual(station: dict) -> bool:
	return str((station or {}).get("duplex") or MANUAL) == MANUAL


def station_for(job_type: str, company: str) -> dict:
	"""The enabled station that prints this job type for this company, lowest priority first."""
	matching = [
		row
		for row in stations()
		if compat.checked(row.get("enabled"))
		and job_type in lines(row.get("job_types"))
		and (not lines(row.get("companies")) or company in lines(row.get("companies")))
	]
	if not matching:
		raise CardPrintError(
			f"no print station is set up for {job_type} cards"
			+ (f" at {company}" if company else "")
			+ ". Add or enable a Card Print Station. Nothing was queued."
		)
	matching.sort(key=lambda row: (int(row.get("priority") or 0), str(row.get("name"))))
	return matching[0]


def seed() -> list:
	"""The default station, create-only."""
	if not ready() or frappe.db.exists(STATION, DEFAULT_STATION):
		return []
	doc = frappe.new_doc(STATION)
	doc.station_name = DEFAULT_STATION
	doc.enabled = 1
	doc.priority = 10
	doc.job_types = "\n".join(JOB_TYPES)
	doc.media = "CR80 PVC card 85.6 x 54 mm"
	doc.artwork_width_mm = 85.6
	doc.artwork_height_mm = 54.0
	doc.duplex = MANUAL
	doc.front_orientation = "Landscape"
	doc.back_orientation = card_art.DEFAULT_BACK_ORIENTATION
	doc.insert(ignore_permissions=True)
	return [doc.name]


# ── rows ────────────────────────────────────────────────────────────────────
def describe(row, user: str = "") -> dict:
	get = row.get
	status = get("status")
	allowed = bool(user) and can_request(user)
	full_name = ""
	if get("requested_by"):
		full_name = frappe.db.get_value("User", get("requested_by"), "full_name") or ""
	return {
		"name": get("name"),
		"job_type": get("job_type"),
		"reference_doctype": get("reference_doctype"),
		"reference_name": get("reference_name"),
		"reference_title": get("reference_title") or get("reference_name"),
		"company": get("company"),
		"status": status,
		"print_station": get("print_station"),
		"copies": int(get("copies") or 1),
		"sides": get("sides") or "Single",
		"requested_by": get("requested_by"),
		"requested_by_name": full_name or get("requested_by"),
		"requested_from": get("requested_from"),
		"requested_at": str(get("creation") or "") or None,
		"claimed_at": str(get("claimed_at") or "") or None,
		"printed_at": str(get("printed_at") or "") or None,
		"attempts": int(get("attempts") or 0),
		"error": get("error") or None,
		"is_reprint": bool(compat.checked(get("is_reprint"))),
		"reprint_reason": get("reprint_reason") or None,
		"pages": get("pages") or "Both",
		"back_pending": bool(compat.checked(get("back_pending"))),
		"front_job": get("front_job") or None,
		"can_cancel": bool(allowed and status == QUEUED),
		"can_retry": bool(allowed and status == FAILED),
		"can_print_back": bool(allowed and status == PRINTED and compat.checked(get("back_pending"))),
		"can_mark_printed": bool(allowed and status in (QUEUED, DOWNLOADED, FAILED)),
		"can_mark_failed": bool(allowed and status in (QUEUED, DOWNLOADED)),
		# v0.226.0. On an asset tag only, so the frozen ID-card shape is unchanged.
		**(
			{"tag_format": get("tag_format") or CARD, "location_label": get("location_label") or None}
			if get("job_type") == "Asset Tag"
			else {}
		),
	}


def _job(name: str):
	if not name or not frappe.db.exists(JOB, name):
		raise CardPrintError(f"no print job called {name!r}.", "not_found")
	return frappe.get_doc(JOB, name)


def _save(doc) -> None:
	doc.flags.ignore_permissions = True
	doc.flags.queue_move = True
	doc.save(ignore_permissions=True)


# ── the reference and its artwork ───────────────────────────────────────────
def _employee(reference: str) -> tuple:
	"""(docname, title, company, card facts) for an Active employee; issues a badge if none."""
	from .tools import badges

	try:
		row = badges._employee_row(reference)
	except Exception as exc:
		raise CardPrintError(f"no employee called {reference!r}. Nothing was queued.", "not_found") from exc
	if str(row.get("status") or "Active") != "Active":
		raise CardPrintError(
			f"{row.get('employee_name') or row['name']} is {row.get('status')}, not Active — an ID card is "
			"for somebody on the payroll. Nothing was queued."
		)
	company = str(row.get("company") or "")
	return row["name"], row.get("employee_name") or row["name"], company, row


def _employee_card(row: dict, company: str, allow_issue: bool = True) -> dict:
	"""The card's facts and badge QR. An existing badge is REUSED; a first badge is
	issued only when the caller may issue badges (the hiring roles) — printing a
	card is not a second way to mint an identifier."""
	from .errors import ToolError
	from .tools import badges

	badge_id, created = badges._choose_badge_id(row, company, {}, "")
	rendered = badges._render(badge_id, badges.BADGE_ERROR_CORRECTION)
	if created and not allow_issue:
		raise CardPrintError(
			f"{row.get('employee_name') or row['name']} has no badge yet. Use the Print ID Card button, "
			"which issues one and records the card."
		)
	if created:
		try:
			badges._record_badge(row, company, badge_id, {})
		except ToolError as exc:
			raise CardPrintError(
				f"{row.get('employee_name') or row['name']} has no badge yet, and issuing one needs a hiring "
				"role (Farm Manager, HR, Foreman or Crew Leader). Ask one of them to issue the badge — or to "
				"print this card, which issues it. Nothing was queued.",
				"forbidden",
			) from exc
	return badges._card(row, badge_id, company, rendered)


def tag_subject(reference: str) -> tuple:
	"""v0.226.0. `(doctype, name, title, company, row, location)` for an asset or a housing unit,
	by docname or by the UUID on its tag."""
	from . import offline_create

	reference = str(reference or "").strip()
	if offline_create.is_uuid(reference):
		_doctype, found = offline_create.resolve(reference)
		if found:
			reference = found
	if (
		reference
		and not frappe.db.exists("Asset Register", reference)
		and frappe.db.exists(HOUSING_UNIT, reference)
	):
		fields = compat.existing_fields(
			HOUSING_UNIT, ("name", "unit_name", "unit_type", "parcel", "owning_entity", "tag_uuid")
		)
		row = dict(frappe.db.get_value(HOUSING_UNIT, reference, fields, as_dict=True) or {})
		row["name"] = reference
		title = row.get("unit_name") or reference
		return (
			HOUSING_UNIT,
			reference,
			title,
			str(row.get("owning_entity") or ""),
			row,
			str(row.get("parcel") or ""),
		)
	name, title, company, row = _asset(reference)
	return "Asset Register", name, title, company, row, str(row.get("location") or "")


def _asset(reference: str) -> tuple:
	doctype = "Asset Register"
	if not reference or not frappe.db.exists(doctype, reference):
		raise CardPrintError(f"no asset called {reference!r}. Nothing was queued.", "not_found")
	fields = compat.existing_fields(
		doctype, ("name", "asset_type", "company", "description", "qr_url", "retired_at", "location")
	)
	row = dict(frappe.db.get_value(doctype, reference, fields, as_dict=True) or {})
	row["name"] = reference
	if row.get("retired_at"):
		raise CardPrintError(f"{reference} was retired; a tag is for an asset in use. Nothing was queued.")
	return reference, reference, str(row.get("company") or ""), row


def file_bytes(url) -> bytes | None:
	"""A File's bytes by its URL, or None. Never raises: a missing picture must not lose a card."""
	url = str(url or "").strip()
	if not url:
		return None
	try:
		name = frappe.db.get_value("File", {"file_url": url}, "name")
		if not name:
			return None
		content = frappe.get_doc("File", name).get_content()
		return content.encode() if isinstance(content, str) else bytes(content)
	except Exception:
		return None


def company_logo(company: str) -> bytes | None:
	"""The Company's badge logo, else its ordinary logo, as bytes."""
	if not company or not compat.doctype_exists("Company"):
		return None
	for field in ("badge_logo", "company_logo"):
		if compat.has_field("Company", field):
			data = file_bytes(frappe.db.get_value("Company", company, field))
			if data:
				return data
	return None


#: Amendment 2 §B1. What the colour bar can say. The bar prints the option upper-cased.
BADGE_CATEGORIES = ("Employee", "Management", "Owner / Operator", "Contractor", "Volunteer", "Visitor")
EMPLOYMENT_TYPE = "Employment Type"

#: §B3. Read ONCE, at migrate, to give each Employment Type a first category. At
#: print time the Employment Type record is the only authority.
_FIRST_CATEGORIES = (
	(("operator", "owner"), "Owner / Operator"),
	(("contract", "1099"), "Contractor"),
	(("volunteer",), "Volunteer"),
	(("visitor",), "Visitor"),
	(
		(
			"full",
			"part",
			"season",
			"tempor",
			"hourly",
			"salar",
			"piece",
			"commission",
			"intern",
			"apprentice",
			"probation",
			"h-2a",
		),
		"Employee",
	),
)


def first_category(employment_type: str) -> str:
	text = str(employment_type or "").lower()
	for words, category in _FIRST_CATEGORIES:
		if any(word in text for word in words):
			return category
	return ""


def install_badge_fields() -> list:
	"""Employee.badge_title, Employee.badge_category, Employment Type.badge_category,
	and each Employment Type's first category. Idempotent; never overwrites a value."""
	options = "\n" + "\n".join(BADGE_CATEGORIES)
	specs = (
		(
			"Employee",
			"badge_title",
			"Badge Title",
			"Data",
			"",
			"designation",
			"Printed under the name on the ID card. Leave blank to print the Designation.",
		),
		(
			"Employee",
			"badge_category",
			"Badge Category",
			"Select",
			options,
			"badge_title",
			"The colour bar on the ID card. Leave blank to use the Employment Type's category.",
		),
		(
			EMPLOYMENT_TYPE,
			"badge_category",
			"Badge Category",
			"Select",
			options,
			"employee_type_name",
			"The ID card's colour bar for people of this type, unless their Employee record says otherwise. "
			"Employee is for people on the payroll.",
		),
	)
	made = []
	for doctype, fieldname, label, fieldtype, choices, after, description in specs:
		if not compat.doctype_exists(doctype) or compat.has_field(doctype, fieldname):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Custom Field",
				"dt": doctype,
				"fieldname": fieldname,
				"label": label,
				"fieldtype": fieldtype,
				"options": choices,
				"insert_after": after if compat.has_field(doctype, after) else "",
				"description": description,
				"module": "ERPNext MCP",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		made.append(f"{doctype}.{fieldname}")
	if compat.doctype_exists(EMPLOYMENT_TYPE) and compat.has_field(EMPLOYMENT_TYPE, "badge_category"):
		for row in frappe.db.get_all(EMPLOYMENT_TYPE, fields=["name", "badge_category"], limit=0):
			category = "" if row.get("badge_category") else first_category(row["name"])
			if category:
				frappe.db.set_value(EMPLOYMENT_TYPE, row["name"], "badge_category", category)
				made.append(f"{row['name']} → {category}")
	return made


def badge_words(row: dict) -> tuple:
	"""(title, category, warning) for an ID card. §B2: the Employee's own fields, then
	the Designation and the Employment Type's category. Never a guessed "Employee"."""
	name = row["name"]
	own = {}
	fields = compat.existing_fields("Employee", ("badge_title", "badge_category", "employment_type"))
	if fields:
		own = dict(frappe.db.get_value("Employee", name, fields, as_dict=True) or {})
	title = str(own.get("badge_title") or "").strip() or str(row.get("designation") or "").strip()
	category = str(own.get("badge_category") or "").strip()
	kind = str(own.get("employment_type") or "").strip()
	if not category and kind and compat.has_field(EMPLOYMENT_TYPE, "badge_category"):
		category = str(frappe.db.get_value(EMPLOYMENT_TYPE, kind, "badge_category") or "").strip()
	if category:
		return title, category, ""
	who = row.get("employee_name") or name
	where = f"Employment Type {kind!r} has no Badge Category" if kind else f"{who} has no Employment Type"
	return (
		title,
		"",
		f"The colour bar is blank: {where}. Set Badge Category on the Employee, or on the Employment Type.",
	)


def _matrix(text: str, error: str) -> list:
	from .render import qr

	return qr.qr_matrix(text, error) if qr.available() else []


def card_sides(job_type: str, row: dict, company: str, allow_issue: bool = True) -> tuple:
	"""([front ops, back ops], warnings) — the approved layout for one record.

	`allow_issue=False` (the print format) never issues a first badge."""
	logo = company_logo(company)
	warnings = [w for w in [card_art.logo_warning(logo, f"{company}'s logo")] if w]
	if job_type == "Employee ID":
		card = _employee_card(row, company, allow_issue)
		badge_id = str(card.get("badge_id") or "")
		matrix = _matrix(badge_id, "H")
		photo = file_bytes(row.get("image"))
		title, category, unresolved = badge_words(row)
		if unresolved:
			warnings.append(unresolved)
		if not photo:
			warnings.append(
				f"{card.get('employee_name')} has no photo on their Employee record; the card shows initials."
			)
		sides = card_art.employee_sides(
			{
				"employee_name": card.get("employee_name"),
				"designation": title,
				"company": company,
				"badge_id": badge_id,
				"band": category,
				"qr": matrix,
				"photo": photo,
				"logo": logo,
			}
		)
	else:
		matrix = _matrix(str(row.get("qr_url") or f"/scan/{row['name']}"), "M")
		description = str(row.get("description") or "").strip().splitlines()
		sides = card_art.asset_sides(
			{
				"asset_id": row["name"],
				"asset_name": (description[0] if description else "") or row.get("asset_type") or "",
				"company": company,
				"qr": matrix,
				"logo": logo,
			}
		)
	if not matrix:
		raise CardPrintError(
			"no QR encoder is installed on this server (segno), so the card cannot be drawn. Nothing was queued."
		)
	return sides, warnings


def _rotations(station: dict) -> list:
	return [
		card_art.FRONT_ORIENTATIONS.get(str(station.get("front_orientation") or "Landscape"), 0),
		card_art.BACK_ORIENTATIONS.get(
			str(station.get("back_orientation") or card_art.DEFAULT_BACK_ORIENTATION), 90
		),
	]


def render_artwork(job_type: str, row: dict, company: str, pages: str, station: dict) -> tuple:
	"""(pdf, warnings): one card-sized page per side this job prints."""
	sides, warnings = card_sides(job_type, row, company)
	rotations = _rotations(station)
	keep = {"Both": (0, 1), "Front": (0,), "Back": (1,)}[pages]
	try:
		pdf = card_art.to_pdf(
			[sides[i] for i in keep], [rotations[i] for i in keep], title=f"{job_type} {row['name']}"
		)
	except card_art.ArtError as exc:
		raise CardPrintError(str(exc)) from exc
	return pdf, warnings


def pages_for(sides: str, station: dict) -> str:
	"""What a job on this station prints: both sides where it can duplex, and where a
	person prints it (the PDF is the whole card; they flip it)."""
	if sides != "Dual":
		return "Front"
	return "Front" if str(station.get("duplex") or MANUAL) == "Simplex" else "Both"


def preview(user: str, job_type: str, reference_name: str, may_read=None) -> dict:
	"""What the card will look like, and where it would go. Writes nothing but a first badge."""
	require_requester(user)
	if job_type not in JOB_TYPES:
		raise CardPrintError(f"job_type is one of {', '.join(JOB_TYPES)}.")
	name, title, company, row = (_employee if job_type == "Employee ID" else _asset)(
		str(reference_name or "")
	)
	if may_read is not None and not may_read(JOB_TYPES[job_type], name, company):
		raise CardPrintError(f"no {JOB_TYPES[job_type]} called {reference_name!r}.", "not_found")
	sides, warnings = card_sides(job_type, row, company)
	words = badge_words(row) if job_type == "Employee ID" else ("", "", "")
	try:
		station = station_for(job_type, company)
		state = station_state(station)
	except CardPrintError as exc:
		station, state = {}, None
		warnings.append(str(exc))
	printed = frappe.db.get_all(
		JOB,
		filters={"job_type": job_type, "reference_name": name, "status": PRINTED},
		fields=["name", "printed_at"],
		limit=1,
	)
	return {
		"job_type": job_type,
		"reference_name": name,
		"title": title,
		"company": company,
		"front_svg": card_art.to_svg(sides[0], css_width_mm=85.6),
		"back_svg": card_art.to_svg(sides[1], css_width_mm=54.0),
		"badge_title": words[0],
		"badge_category": words[1],
		"warnings": warnings,
		"station": state,
		"agent_online": bool(state and state["state"] not in ("Offline", MANUAL)),
		"already_printed_on": str(printed[0].get("printed_at") or "")[:10] if printed else None,
	}


# ── requester methods ───────────────────────────────────────────────────────
def _rate_check(user: str) -> None:
	today = str(frappe.utils.today())
	rows = frappe.db.get_all(
		JOB, filters={"requested_by": user, "creation": (">=", today)}, fields=["creation"], limit=PER_DAY + 5
	)
	if len(rows) >= PER_DAY:
		raise CardPrintError(f"{user} has asked for {PER_DAY} cards today, which is the daily limit.")
	recent = sum(1 for row in rows if (_seconds_since(row.get("creation")) or 999) < 60)
	if recent >= PER_MINUTE:
		raise CardPrintError("that is 10 print requests in a minute — wait a moment and try again.")


def existing_for(client_request_id: str):
	name = frappe.db.get_value(JOB, {"client_request_id": client_request_id}, "name")
	return frappe.get_doc(JOB, name) if name else None


def request(
	user: str,
	job_type: str,
	reference_name: str,
	client_request_id: str,
	copies=1,
	sides: str = "",
	reprint_reason: str = "",
	requested_from: str = "API",
	may_read=None,
	tag_format: str = "",
) -> dict:
	"""Create a Queued job, or return the one this request already made. §4.1.

	v0.226.0. `tag_format` for an asset tag: Card (default — the card printer),
	Outdoor label or Sheet (no station: printed from a QR sheet by location, see
	`print_for_location`). A housing unit takes an Outdoor label or a Sheet."""
	if not ready():
		raise CardPrintError("this site has no card print queue yet — run `bench --site <site> migrate`.")
	key = str(client_request_id or "").strip()
	if not 8 <= len(key) <= 64:
		raise CardPrintError(
			"client_request_id is required (8–64 characters, a UUID) — it is what stops a retried call "
			"printing a second card. Nothing was queued."
		)
	require_requester(user)
	held = existing_for(key)
	if held is not None:
		if held.requested_by != user:
			raise CardPrintError("that client_request_id belongs to somebody else's request.", "forbidden")
		return _answer(held, user, created=False, duplicate=True)

	if job_type not in JOB_TYPES:
		raise CardPrintError(f"job_type is one of {', '.join(JOB_TYPES)}. Nothing was queued.")
	sides = sides or "Dual"
	if sides not in SIDES:
		raise CardPrintError("sides is Single or Dual. Nothing was queued.")
	try:
		copies = 1 if copies in (None, "") else int(copies)
	except (TypeError, ValueError):
		copies = 0
	if not 1 <= copies <= MAX_COPIES:
		raise CardPrintError(f"copies must be 1 to {MAX_COPIES}. Nothing was queued.")

	fmt, doctype, location = "", JOB_TYPES[job_type], ""
	if job_type == "Employee ID":
		name, title, company, row = _employee(str(reference_name or ""))
	else:
		fmt = str(tag_format or CARD).strip()
		if fmt not in TAG_FORMATS:
			raise CardPrintError(f"tag_format is one of {', '.join(TAG_FORMATS)}. Nothing was queued.")
		doctype, name, title, company, row, location = tag_subject(str(reference_name or ""))
		if doctype == HOUSING_UNIT and fmt == CARD:
			raise CardPrintError(
				"a housing unit's tag is an Outdoor label or a Sheet — the card printer makes asset cards. "
				"Nothing was queued."
			)
	if may_read is not None and not may_read(doctype, name, company):
		raise CardPrintError(f"no {doctype} called {reference_name!r}. Nothing was queued.", "not_found")

	open_job = frappe.db.get_value(
		JOB,
		{"job_type": job_type, "reference_name": name, "status": ("in", (QUEUED, PRINTING))},
		"name",
	)
	if open_job:
		return _answer(frappe.get_doc(JOB, open_job), user, created=False, already_queued=True)

	reason = str(reprint_reason or "").strip()[:140]
	printed = frappe.db.get_all(
		JOB,
		filters={"job_type": job_type, "reference_name": name, "status": PRINTED},
		fields=["name", "printed_at"],
		limit=1,
	)
	if printed and not reason:
		raise CardPrintError(
			f"this card was already printed on {str(printed[0].get('printed_at') or '')[:10]}; say why it is "
			f"being reprinted (Lost, Damaged, Details changed, or Other). Nothing was queued. ({REPRINT_MARKER})"
		)
	_rate_check(user)
	on_card = fmt in ("", CARD)
	if on_card:
		station = station_for(job_type, company)
		pages = pages_for(sides, station)
		pdf, warnings = render_artwork(job_type, row, company, pages, station)
	else:
		# A label or a sheet never goes to the card printer: no station, no card art.
		station, pages, pdf, warnings, sides = None, "Front", b"", [], "Single"

	doc = frappe.new_doc(JOB)
	doc.job_type = job_type
	doc.reference_doctype = doctype
	doc.reference_name = name
	doc.reference_title = str(title)[:140]
	doc.company = company or None
	doc.status = QUEUED
	doc.print_station = station["name"] if station else None
	doc.copies = copies
	doc.sides = sides
	doc.pages = pages
	if fmt and compat.has_field(JOB, "tag_format"):
		doc.tag_format = fmt
		doc.location_label = (location or "")[:140] or None
	doc.requested_by = user
	doc.requested_from = requested_from if requested_from in SOURCES else "API"
	doc.client_request_id = key
	doc.attempts = 0
	doc.is_reprint = 1 if printed else 0
	doc.reprint_reason = reason if printed else None
	doc.artwork_sha256 = hashlib.sha256(pdf).hexdigest() if pdf else None
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	from .tools import artifacts

	if pdf:
		artifacts.attach_bytes(JOB, doc.name, f"{doc.name}.pdf", pdf, field="artwork")
	answer = _answer(frappe.get_doc(JOB, doc.name), user, created=True, warnings=warnings)
	# v0.214.0. A CARD ASKED FOR WITH NO PHOTO RAISES THE BADGE PHOTO TASK. The
	# card still queues — initials print — and the answer says a photo has been
	# asked for. `auto_request` never raises and answers None when a site turned
	# it off or the person already has one.
	if job_type == "Employee ID" and not str(row.get("image") or "").strip():
		from . import badge_photo

		task = badge_photo.auto_request(name, origin="")
		if task:
			answer["badge_photo_task"] = task
	return answer


def request_back(
	name: str, user: str = "", companies=None, client_request_id: str = "", requested_from: str = "Desk"
) -> dict:
	"""Queue the BACK of a card whose front printed on a simplex station. §A4."""
	user = user or str(frappe.session.user)
	require_requester(user)
	front = _job(name)
	_may_touch(front, user, companies)
	if front.get("pages") == "Back":
		front = _job(front.front_job) if front.get("front_job") else front
	if front.status != PRINTED or not compat.checked(front.get("back_pending")):
		raise CardPrintError(
			f"{front.name} has no back waiting: the back is printed after a front has printed on a "
			"single-sided printer. Nothing was queued."
		)
	open_back = frappe.db.get_value(
		JOB, {"front_job": front.name, "status": ("in", (QUEUED, PRINTING))}, "name"
	)
	if open_back:
		return _answer(frappe.get_doc(JOB, open_back), user, created=False, already_queued=True)
	_rate_check(user)
	station = next((s for s in stations() if s["name"] == front.print_station), None) or station_for(
		front.job_type, front.company or ""
	)
	_name, _title, company, row = (_employee if front.job_type == "Employee ID" else _asset)(
		front.reference_name
	)
	pdf, warnings = render_artwork(front.job_type, row, company, "Back", station)
	doc = frappe.new_doc(JOB)
	doc.job_type = front.job_type
	doc.reference_doctype = front.reference_doctype
	doc.reference_name = front.reference_name
	doc.reference_title = front.reference_title
	doc.company = front.company
	doc.status = QUEUED
	doc.print_station = station["name"]
	doc.copies = 1
	doc.sides = "Single"
	doc.pages = "Back"
	doc.front_job = front.name
	doc.requested_by = user
	doc.requested_from = requested_from if requested_from in SOURCES else "API"
	doc.client_request_id = (
		str(client_request_id or "").strip() or f"back-{front.name}-{frappe.utils.now()}"
	)[:64]
	doc.attempts = 0
	doc.artwork_sha256 = hashlib.sha256(pdf).hexdigest()
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	from .tools import artifacts

	artifacts.attach_bytes(JOB, doc.name, f"{doc.name}.pdf", pdf, field="artwork")
	return _answer(frappe.get_doc(JOB, doc.name), user, created=True, warnings=warnings)


def download(user: str, job_type: str, reference_name: str, reprint_reason: str = "", may_read=None) -> tuple:
	"""(job doc, pdf, warnings): the whole card to print by hand, RECORDED as Downloaded. §A5."""
	if not ready():
		raise CardPrintError("this site has no card print queue yet — run `bench --site <site> migrate`.")
	require_requester(user)
	if job_type not in JOB_TYPES:
		raise CardPrintError(f"job_type is one of {', '.join(JOB_TYPES)}.")
	name, title, company, row = (_employee if job_type == "Employee ID" else _asset)(
		str(reference_name or "")
	)
	if may_read is not None and not may_read(JOB_TYPES[job_type], name, company):
		raise CardPrintError(f"no {JOB_TYPES[job_type]} called {reference_name!r}.", "not_found")
	_rate_check(user)
	try:
		station = station_for(job_type, company)
	except CardPrintError:
		station = {}
	pdf, warnings = render_artwork(job_type, row, company, "Both", station)
	printed = frappe.db.get_all(
		JOB,
		filters={"job_type": job_type, "reference_name": name, "status": PRINTED},
		fields=["name"],
		limit=1,
	)
	doc = frappe.new_doc(JOB)
	doc.job_type = job_type
	doc.reference_doctype = JOB_TYPES[job_type]
	doc.reference_name = name
	doc.reference_title = str(title)[:140]
	doc.company = company or None
	doc.status = DOWNLOADED
	doc.print_station = station.get("name")
	doc.copies = 1
	doc.sides = "Dual"
	doc.pages = "Both"
	doc.requested_by = user
	doc.requested_from = "Desk"
	doc.client_request_id = (
		f"download-{hashlib.sha256((name + str(frappe.utils.now()) + user).encode()).hexdigest()[:24]}"
	)
	doc.attempts = 0
	doc.is_reprint = 1 if printed else 0
	doc.reprint_reason = (str(reprint_reason or "").strip()[:140] or None) if printed else None
	doc.artwork_sha256 = hashlib.sha256(pdf).hexdigest()
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	from .tools import artifacts

	artifacts.attach_bytes(JOB, doc.name, f"{doc.name}.pdf", pdf, field="artwork")
	return frappe.get_doc(JOB, doc.name), pdf, warnings


def _answer(doc, user: str, created=False, duplicate=False, already_queued=False, warnings=None) -> dict:
	station = next((s for s in stations() if s["name"] == doc.print_station), None)
	return {
		"job": describe(doc, user),
		"created": created,
		"duplicate": duplicate,
		"already_queued": already_queued,
		"station": station_state(station) if station else None,
		"warnings": list(warnings or []),
	}


def list_jobs(
	user: str, companies=None, status: str = "", mine_only=True, reference_name: str = "", limit=50
) -> dict:
	require_requester(user)
	if not ready():
		return {"jobs": [], "count": 0, "stations": [], "can_request": True}
	filters: dict = {}
	if status:
		if status not in STATUSES:
			raise CardPrintError(f"status is one of {', '.join(STATUSES)}.")
		filters["status"] = status
	if reference_name:
		filters["reference_name"] = reference_name
	if mine_only:
		filters["requested_by"] = user
	if companies:
		filters["company"] = ("in", list(companies))
	try:
		limit = max(1, min(200, int(limit or 50)))
	except (TypeError, ValueError):
		limit = 50
	rows = frappe.db.get_all(JOB, filters=filters, fields=ROW_FIELDS, order_by="creation desc", limit=limit)
	rows = sorted((dict(r) for r in rows), key=lambda r: str(r.get("creation") or ""), reverse=True)
	return {
		"jobs": [describe(row, user) for row in rows],
		"count": len(rows),
		"stations": [station_state(s) for s in stations() if compat.checked(s.get("enabled"))],
		"can_request": can_request(user),
	}


def _tag_rows(companies=None, location: str = "", statuses=(QUEUED, FAILED)) -> list:
	filters: dict = {"job_type": "Asset Tag", "status": ("in", list(statuses))}
	if location and compat.has_field(JOB, "location_label"):
		filters["location_label"] = location
	if companies:
		filters["company"] = ("in", list(companies))
	fields = compat.existing_fields(JOB, (*ROW_FIELDS, "tag_format", "location_label"))
	rows = frappe.db.get_all(JOB, filters=filters, fields=fields, order_by="creation asc", limit=500)
	return sorted((dict(r) for r in rows or []), key=lambda r: str(r.get("creation") or ""))


def list_tag_queue(user: str, companies=None) -> dict:
	"""v0.226.0. Asset and housing tags waiting to print, grouped by where they are."""
	require_requester(user)
	if not ready():
		return {"locations": [], "count": 0}
	groups: dict = {}
	for row in _tag_rows(companies):
		key = str(row.get("location_label") or "")
		group = groups.setdefault(key, {"location": key or None, "jobs": [], "formats": {}})
		job = describe(row, user)
		group["jobs"].append(job)
		group["formats"][job["tag_format"]] = group["formats"].get(job["tag_format"], 0) + 1
	locations = sorted(groups.values(), key=lambda g: (g["location"] is None, str(g["location"] or "")))
	for group in locations:
		group["count"] = len(group["jobs"])
	return {"locations": locations, "count": sum(g["count"] for g in locations)}


def print_for_location(user: str, companies=None, location: str = "", template: str = "") -> dict:
	"""v0.226.0. Print every waiting tag for one location.

	Card jobs are already in the card printer's queue (a Failed one is put back);
	Outdoor label and Sheet jobs become ONE QR sheet, and are marked Printed — the
	reprint rule (`is_reprint`, a reason) then applies to any later copy."""
	require_requester(user)
	location = str(location or "").strip()
	rows = _tag_rows(companies, location) if location else []
	if location and not compat.has_field(JOB, "location_label"):
		raise CardPrintError("this site has no tag locations yet — run `bench --site <site> migrate`.")
	cards, sheet = [], []
	for row in rows:
		(cards if (row.get("tag_format") or CARD) == CARD else sheet).append(row)
	for row in cards:
		if row.get("status") == FAILED:
			retry(row["name"], user, companies)
	labels, errors = [], []
	if sheet:
		from . import asset_tag_sheet
		from .erpnext_mcp.doctype.asset_register.asset_register import _build_qr_url
		from .render import qr

		for row in sheet:
			reference = str(row.get("reference_name") or "")
			doctype = str(row.get("reference_doctype") or "Asset Register")
			tag = ""
			if compat.has_field(doctype, "tag_uuid"):
				tag = str(frappe.db.get_value(doctype, reference, "tag_uuid") or "")
			url = _build_qr_url(tag or reference)
			try:
				rendered = qr.render(url)
				labels.append(
					{
						"asset_name": row.get("reference_title") or reference,
						"location": location,
						"qr_url": url,
						"png_base64": base64.b64encode(rendered["png"]).decode("ascii"),
						"modules": rendered["modules"],
					}
				)
			except Exception as exc:
				errors.append({"asset_name": reference, "error": str(exc)})
		html = asset_tag_sheet.sheet_html(labels, errors, template or asset_tag_sheet.DEFAULT_TEMPLATE)
		for row in sheet:
			doc = _job(row["name"])
			doc.claimed_by = user
			doc.claimed_at = frappe.utils.now()
			_printed(doc)
			_save(doc)
	else:
		html = ""
	return {
		"location": location or None,
		"cards_queued": len(cards),
		"sheet_labels": len(labels),
		"sheet_html": html or None,
		"errors": errors,
		"note": (
			f"{len(cards)} card(s) are in the card printer's queue; "
			f"{len(labels)} label(s) on the sheet — print it at 100 % on the label stock."
		),
	}


def _may_touch(doc, user: str, companies=None) -> None:
	"""§D3–D4: the role, always — having asked for a card is not a pass — and the job's
	company inside the caller's. Out of scope reads as not there."""
	require_requester(user)
	if companies and (not doc.company or doc.company not in companies):
		raise CardPrintError(f"no print job called {doc.name!r}.", "not_found")


def in_scope(company: str, companies) -> bool:
	"""§D4. `companies` None or empty = unrestricted (Frappe's rule for Desk accounts)."""
	return not companies or (bool(company) and company in companies)


def mark(name: str, user: str = "", companies=None, printed=True, error: str = "") -> dict:
	"""A person closes a job they printed by hand — or says it did not print. §D2."""
	user = user or str(frappe.session.user)
	require_requester(user)  # before the lookup: no role, no word on what exists
	doc = _job(name)
	_may_touch(doc, user, companies)
	printed = _truthy(printed)
	if doc.status == PRINTED and printed:
		return {"job": describe(doc, user), "already": True}
	allowed_from = (QUEUED, DOWNLOADED, FAILED) if printed else (QUEUED, DOWNLOADED)
	if doc.status not in allowed_from:
		raise CardPrintError(
			f"{doc.name} is {doc.status}; it can be marked {'Printed' if printed else 'Failed'} only from "
			f"{' or '.join(allowed_from)}. Nothing was changed."
		)
	reason = str(error or "").strip()
	if not printed and not reason:
		raise CardPrintError("say what went wrong (error) when marking a card Failed. Nothing was changed.")
	doc.claimed_by = user
	doc.claimed_at = frappe.utils.now()
	if printed:
		_printed(doc)
	else:
		doc.status = FAILED
		doc.error = reason[:900]
	_save(doc)
	return {"job": describe(doc, user), "already": False}


def _printed(doc) -> None:
	doc.status = PRINTED
	doc.printed_at = frappe.utils.now()
	doc.error = None
	# §A4: a two-sided card on a simplex station has its back still to come.
	if doc.get("pages") == "Front" and doc.sides == "Dual":
		doc.back_pending = 1
	if doc.get("pages") == "Back" and doc.get("front_job") and frappe.db.exists(JOB, doc.front_job):
		frappe.db.set_value(JOB, doc.front_job, "back_pending", 0)


def cancel(name: str, user: str = "", companies=None) -> dict:
	user = user or str(frappe.session.user)
	require_requester(user)  # before the lookup: no role, no word on what exists
	doc = _job(name)
	_may_touch(doc, user, companies)
	if doc.status == CANCELLED:
		return {"job": describe(doc, user), "already": True}
	if doc.status != QUEUED:
		raise CardPrintError(
			f"{doc.name} is {doc.status}; only a Queued job can be cancelled"
			+ (" — the card is already in the printer." if doc.status == PRINTING else ".")
		)
	doc.status = CANCELLED
	doc.error = f"Cancelled by {user}."
	_save(doc)
	return {"job": describe(doc, user), "already": False}


def retry(name: str, user: str = "", companies=None) -> dict:
	user = user or str(frappe.session.user)
	require_requester(user)  # before the lookup: no role, no word on what exists
	doc = _job(name)
	_may_touch(doc, user, companies)
	if doc.status == QUEUED:
		return {"job": describe(doc, user), "already": True}
	if doc.status != FAILED:
		raise CardPrintError(f"{doc.name} is {doc.status}; only a Failed job is retried.")
	doc.status = QUEUED
	doc.attempts = 0
	doc.claimed_at = None
	doc.claimed_by = None
	_save(doc)
	return {"job": describe(doc, user), "already": False}


# ── station methods ─────────────────────────────────────────────────────────
def heartbeat(
	station: str, user: str, printer_state: str = "", printer_message: str = "", agent_version: str = ""
) -> dict:
	require_station(user)
	if not station or not frappe.db.exists(STATION, station):
		raise CardPrintError(f"no print station called {station!r}.", "not_found")
	state = (
		printer_state if printer_state in STATION_STATES else ("Printer error" if printer_state else READY)
	)
	frappe.db.set_value(
		STATION,
		station,
		{
			"last_seen_at": frappe.utils.now(),
			"printer_state": state,
			"printer_message": str(printer_message or "")[:500],
			"agent_version": str(agent_version or "")[:60],
		},
		update_modified=False,
	)
	return {"station": station, "state": state}


def _claim_payload(doc) -> dict:
	from .tools import files

	pdf = files.read_attached_bytes_unchecked(JOB, doc.name, f"{doc.name}.pdf")
	return {
		"name": doc.name,
		"job_type": doc.job_type,
		"copies": int(doc.copies or 1),
		"sides": doc.sides or "Single",
		"reference_title": doc.reference_title,
		"attempts": int(doc.attempts or 0),
		"pages": doc.get("pages") or "Both",
		# v0.209.0: both sides in one pass only where the station can and the PDF has two pages.
		"duplex": (doc.get("pages") or "Both") == "Both" and doc.sides == "Dual",
		"artwork_base64": base64.b64encode(pdf).decode(),
		"artwork_sha256": hashlib.sha256(pdf).hexdigest(),
		"file_name": f"{doc.name}.pdf",
	}


def claim(
	station: str, user: str, printer_state: str = "", printer_message: str = "", agent_version: str = ""
) -> dict:
	"""The oldest Queued job for the station, under a row lock. §4.2."""
	beat = heartbeat(station, user, printer_state, printer_message, agent_version)
	if not compat.checked(frappe.db.get_value(STATION, station, "enabled")):
		return {"job": None, "reason": "the station is disabled"}
	if is_manual({"duplex": frappe.db.get_value(STATION, station, "duplex")}):
		return {"job": None, "reason": "this station is printed by hand"}
	open_job = frappe.db.get_value(JOB, {"print_station": station, "status": PRINTING}, "name")
	if open_job:
		# One job at a time: an agent that died between claim and print gets its job back.
		return {"job": _claim_payload(frappe.get_doc(JOB, open_job)), "resumed": True}
	if beat["state"] != READY:
		return {"job": None, "reason": f"the printer is {beat['state']}"}
	candidates = frappe.db.get_all(
		JOB,
		filters={"print_station": station, "status": QUEUED},
		fields=["name", "creation"],
		order_by="creation asc",
		limit=10,
	)
	for row in sorted(candidates, key=lambda r: (str(r.get("creation") or ""), str(r["name"]))):
		try:
			status = frappe.db.get_value(JOB, row["name"], "status", for_update=True)
		except TypeError:  # an older Frappe without for_update on get_value
			status = frappe.db.get_value(JOB, row["name"], "status")
		if status != QUEUED:
			continue
		doc = frappe.get_doc(JOB, row["name"])
		doc.status = PRINTING
		doc.claimed_at = frappe.utils.now()
		doc.claimed_by = user
		doc.attempts = int(doc.attempts or 0) + 1
		doc.error = None
		_save(doc)
		return {"job": _claim_payload(doc), "resumed": False}
	return {"job": None, "reason": "nothing is queued"}


def complete(name: str, user: str, success, error: str = "", retryable=False, cups_job: str = "") -> dict:
	require_station(user)
	doc = _job(name)
	success = _truthy(success)
	if doc.status == PRINTED and success:
		return {"name": doc.name, "status": PRINTED, "already": True}
	if doc.status != PRINTING:
		raise CardPrintError(f"{doc.name} is {doc.status}, not Printing; nothing to complete.")
	if doc.claimed_by and doc.claimed_by != user:
		raise CardPrintError(f"{doc.name} was claimed by another station.", "forbidden")
	if cups_job:
		doc.cups_job = str(cups_job)[:140]
	if success:
		_printed(doc)
	else:
		doc.error = (str(error or "the print station reported a failure")[:900]) or None
		again = _truthy(retryable) and int(doc.attempts or 0) < MAX_ATTEMPTS
		doc.status = QUEUED if again else FAILED
	_save(doc)
	return {"name": doc.name, "status": doc.status, "already": False, "attempts": int(doc.attempts or 0)}


def _truthy(value) -> bool:
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


def sweep_stuck() -> int:
	"""Scheduler: a job Printing for over 10 minutes goes back to Queued, or fails after 3 tries."""
	if not ready():
		return 0
	moved = 0
	try:
		for row in frappe.db.get_all(
			JOB, filters={"status": PRINTING}, fields=["name", "claimed_at", "attempts"], limit=500
		):
			age = _seconds_since(row.get("claimed_at"))
			if age is not None and age < STUCK_MINUTES * 60:
				continue
			doc = frappe.get_doc(JOB, row["name"])
			doc.error = "the print station did not report back"
			doc.status = QUEUED if int(doc.attempts or 0) < MAX_ATTEMPTS else FAILED
			_save(doc)
			moved += 1
	except Exception:
		frappe.log_error(title="card print: stuck-job sweep", message=frappe.get_traceback())
	return moved


def desk(function, *args, **kwargs):
	"""Run a queue function for a Desk caller: a refusal becomes a frappe.throw."""
	try:
		return function(*args, **kwargs)
	except CardPrintError as exc:
		errors = {"forbidden": frappe.PermissionError, "not_found": frappe.DoesNotExistError}
		frappe.throw(str(exc), errors.get(exc.kind, frappe.ValidationError))
