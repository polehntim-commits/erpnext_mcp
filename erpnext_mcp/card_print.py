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
STATUSES = (QUEUED, PRINTING, PRINTED, FAILED, CANCELLED)
SIDES = ("Single", "Dual")
SOURCES = ("iOS", "Desk", "API")
REQUESTER_ROLE = "Card Print Requester"
STATION_ROLE = "Card Print Station"
ADMIN_ROLE = "System Manager"
ROLES = (REQUESTER_ROLE, STATION_ROLE)
MAX_COPIES = 5
MAX_ATTEMPTS = 3
STUCK_MINUTES = 10
OFFLINE_SECONDS = 120
PER_MINUTE = 10
PER_DAY = 100
READY = "Ready"
STATION_STATES = (READY, "Paused", "Printer error", "Offline")
DEFAULT_STATION = "primacy2-main"
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
	return bool(roles_of(user) & {REQUESTER_ROLE, ADMIN_ROLE})


def require_requester(user: str) -> None:
	if not user or user == "Guest" or not can_request(user):
		raise CardPrintError(
			f"{user or 'this account'} may not print cards: it does not hold the {REQUESTER_ROLE} role. "
			"An ID card is a credential, so asking for one is permissioned. Nothing was queued.",
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
	if age is None or age > OFFLINE_SECONDS:
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
	}


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
	doc.insert(ignore_permissions=True)
	return [doc.name]


# ── rows ────────────────────────────────────────────────────────────────────
def describe(row, user: str = "") -> dict:
	get = row.get
	status = get("status")
	mine = bool(user) and get("requested_by") == user
	allowed = mine or (bool(user) and can_request(user))
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
		"can_cancel": bool(allowed and status == QUEUED),
		"can_retry": bool(allowed and status == FAILED),
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


def _employee_card(row: dict, company: str) -> dict:
	"""The card's facts and badge QR. An existing badge is REUSED; a first badge is
	issued only when the caller may issue badges (the hiring roles) — printing a
	card is not a second way to mint an identifier."""
	from .errors import ToolError
	from .tools import badges

	badge_id, created = badges._choose_badge_id(row, company, {}, "")
	rendered = badges._render(badge_id, badges.BADGE_ERROR_CORRECTION)
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


def _asset(reference: str) -> tuple:
	doctype = "Asset Register"
	if not reference or not frappe.db.exists(doctype, reference):
		raise CardPrintError(f"no asset called {reference!r}. Nothing was queued.", "not_found")
	fields = compat.existing_fields(
		doctype, ("name", "asset_type", "company", "description", "qr_url", "retired_at")
	)
	row = dict(frappe.db.get_value(doctype, reference, fields, as_dict=True) or {})
	row["name"] = reference
	if row.get("retired_at"):
		raise CardPrintError(f"{reference} was retired; a tag is for an asset in use. Nothing was queued.")
	return reference, reference, str(row.get("company") or ""), row


def _asset_qr(row: dict) -> bytes:
	from .render import qr

	if not qr.available():
		return b""
	payload = str(row.get("qr_url") or f"/scan/{row['name']}")
	return qr.render(payload, error="M", scale=10, border=4)["png"]


def render_artwork(job_type: str, row: dict, company: str, sides: str, station: dict) -> bytes:
	width = float(station.get("artwork_width_mm") or 85.6)
	height = float(station.get("artwork_height_mm") or 54.0)
	try:
		if job_type == "Employee ID":
			return card_art.employee_pdf(_employee_card(row, company), sides, width, height)
		if sides == "Dual":
			raise CardPrintError("an asset tag is one side. Nothing was queued.")
		return card_art.asset_pdf(row, _asset_qr(row), width, height)
	except card_art.ArtError as exc:
		raise CardPrintError(str(exc)) from exc


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
	sides: str = "Single",
	reprint_reason: str = "",
	requested_from: str = "API",
	may_read=None,
) -> dict:
	"""Create a Queued job, or return the one this request already made. §4.1."""
	if not ready():
		raise CardPrintError("this site has no card print queue yet — run `bench --site <site> migrate`.")
	key = str(client_request_id or "").strip()
	if not 8 <= len(key) <= 64:
		raise CardPrintError(
			"client_request_id is required (8–64 characters, a UUID) — it is what stops a retried call "
			"printing a second card. Nothing was queued."
		)
	held = existing_for(key)
	if held is not None:
		if held.requested_by != user:
			raise CardPrintError("that client_request_id belongs to somebody else's request.", "forbidden")
		return _answer(held, user, created=False, duplicate=True)

	require_requester(user)
	if job_type not in JOB_TYPES:
		raise CardPrintError(f"job_type is one of {', '.join(JOB_TYPES)}. Nothing was queued.")
	sides = sides or "Single"
	if sides not in SIDES:
		raise CardPrintError("sides is Single or Dual. Nothing was queued.")
	try:
		copies = 1 if copies in (None, "") else int(copies)
	except (TypeError, ValueError):
		copies = 0
	if not 1 <= copies <= MAX_COPIES:
		raise CardPrintError(f"copies must be 1 to {MAX_COPIES}. Nothing was queued.")

	name, title, company, row = (_employee if job_type == "Employee ID" else _asset)(
		str(reference_name or "")
	)
	if may_read is not None and not may_read(JOB_TYPES[job_type], name, company):
		raise CardPrintError(
			f"no {JOB_TYPES[job_type]} called {reference_name!r}. Nothing was queued.", "not_found"
		)

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
	station = station_for(job_type, company)
	pdf = render_artwork(job_type, row, company, sides, station)

	doc = frappe.new_doc(JOB)
	doc.job_type = job_type
	doc.reference_doctype = JOB_TYPES[job_type]
	doc.reference_name = name
	doc.reference_title = str(title)[:140]
	doc.company = company or None
	doc.status = QUEUED
	doc.print_station = station["name"]
	doc.copies = copies
	doc.sides = sides
	doc.requested_by = user
	doc.requested_from = requested_from if requested_from in SOURCES else "API"
	doc.client_request_id = key
	doc.attempts = 0
	doc.is_reprint = 1 if printed else 0
	doc.reprint_reason = reason if printed else None
	doc.artwork_sha256 = hashlib.sha256(pdf).hexdigest()
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	from .tools import artifacts

	artifacts.attach_bytes(JOB, doc.name, f"{doc.name}.pdf", pdf, field="artwork")
	return _answer(frappe.get_doc(JOB, doc.name), user, created=True)


def _answer(doc, user: str, created=False, duplicate=False, already_queued=False) -> dict:
	station = next((s for s in stations() if s["name"] == doc.print_station), None)
	return {
		"job": describe(doc, user),
		"created": created,
		"duplicate": duplicate,
		"already_queued": already_queued,
		"station": station_state(station) if station else None,
	}


def list_jobs(
	user: str, companies=None, status: str = "", mine_only=True, reference_name: str = "", limit=50
) -> dict:
	if not ready():
		return {"jobs": [], "count": 0, "stations": [], "can_request": False}
	filters: dict = {}
	if status:
		if status not in STATUSES:
			raise CardPrintError(f"status is one of {', '.join(STATUSES)}.")
		filters["status"] = status
	if reference_name:
		filters["reference_name"] = reference_name
	if mine_only or not can_request(user):
		filters["requested_by"] = user
	elif companies:
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


def _may_touch(doc, user: str, companies=None) -> None:
	if doc.requested_by == user:
		return
	require_requester(user)
	if companies and doc.company and doc.company not in companies:
		raise CardPrintError(f"no print job called {doc.name!r}.", "not_found")


def cancel(name: str, user: str = "", companies=None) -> dict:
	user = user or str(frappe.session.user)
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
		doc.status = PRINTED
		doc.printed_at = frappe.utils.now()
		doc.error = None
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
