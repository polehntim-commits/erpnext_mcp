# SPDX-License-Identifier: MIT
"""The Employee file: one view of everything about a person. v0.223.0.

docs/design/employee_file.md §2–§3. One read, `build`, behind the MCP tool, the
Desk dialog and the phone. Identity, badge, access, training, signed documents,
housing and tasks — each section named even when it is unavailable, every
lapsing date flagged, and nothing secret: SSN and document numbers appear as
their last four only, bank details not at all, and no credential of any kind.

WHO SEES WHAT is decided here, once, for all three surfaces (§2.3): the person
themself sees their whole file; System Manager and HR Manager every section;
HR User all but discipline; Farm Manager the operational sections only.
"""

from __future__ import annotations

import datetime
import html
import json
import re

import frappe

from . import compat, settings

EMPLOYEE = "Employee"
I9 = "I-9 Form"
W4 = "W-4 Form"
BADGE_MAP = "Bucket Log Badge Map"
CARD_JOB = "Card Print Job"
GRANT = "Mobile Access Grant"
DEVICE = "Mobile Device Enrollment"
TRAINING_RECORD = "Employee Training Record"
SESSION = "Training Session"
ATTENDEE = "Training Session Attendee"
CERTIFICATION = "Certification"
DISCIPLINE = "Farm Incident Record"
EVIDENCE = "Signing Evidence"
HOUSING = "Housing Assignment"
TASK = "Farm Task"

SECTIONS = ("identity", "badge", "access", "training", "signed_documents", "housing", "tasks")
FULL_ROLES = frozenset({"System Manager", "HR Manager"})
HR_USER = "HR User"
FARM_MANAGER = "Farm Manager"
FARM_SECTIONS = frozenset({"badge", "access", "training", "housing", "tasks"})
DEFAULT_WINDOWS = {
	"work_authorization": 90,
	"i9_document": 60,
	"i9_receipt": 30,
	"training": 30,
	"certification": 60,
}
ROW_CAP = 200
LOGINS = 10


class NotAllowed(Exception):
	"""The viewer may not read this file. The message is for them."""


# ── who may see what ────────────────────────────────────────────────────────
def employee_of_user(user: str) -> str:
	if not user or not compat.doctype_exists(EMPLOYEE):
		return ""
	return str(frappe.db.get_value(EMPLOYEE, {"user_id": user}, "name") or "")


def visible_sections(viewer: str, employee: str) -> dict:
	"""{section: True | "reason it is hidden"} for this viewer on this file. Raises NotAllowed."""
	if viewer and employee_of_user(viewer) == employee:
		return {name: True for name in SECTIONS} | {"_self": True}
	held = set(frappe.get_roles(viewer) or []) if viewer else set()
	if held & FULL_ROLES:
		return {name: True for name in SECTIONS}
	if HR_USER in held:
		return {name: True for name in SECTIONS} | {"_no_discipline": True}
	if FARM_MANAGER in held:
		return {
			name: (True if name in FARM_SECTIONS else "needs HR Manager or HR User") for name in SECTIONS
		} | {"_devices_only": True}
	raise NotAllowed(
		f"{viewer or 'this caller'} may not read {employee}'s file: it is not their own, and they hold "
		"none of HR Manager, HR User, Farm Manager or System Manager."
	)


# ── redaction ───────────────────────────────────────────────────────────────
def last4(value) -> str | None:
	text = re.sub(r"[^0-9A-Za-z]", "", str(value or ""))
	return f"•••• {text[-4:]}" if text else None


# ── expiry ──────────────────────────────────────────────────────────────────
def windows(override_days=None) -> dict:
	out = dict(DEFAULT_WINDOWS)
	raw = settings._value("employee_file_expiry_windows")
	try:
		configured = json.loads(raw) if raw else {}
	except (TypeError, ValueError):
		configured = {}
	if isinstance(configured, dict):
		for key, value in configured.items():
			try:
				out[str(key)] = max(0, int(value))
			except (TypeError, ValueError):
				continue
	if override_days not in (None, ""):
		out = {key: max(0, int(override_days)) for key in out}
	return out


def _today() -> datetime.date:
	return datetime.date.fromisoformat(str(frappe.utils.today())[:10])


def expiry(date, kind: str, wins: dict) -> dict | None:
	text = str(date or "")[:10]
	if not text:
		return None
	try:
		days = (datetime.date.fromisoformat(text) - _today()).days
	except ValueError:
		return None
	window = wins.get(kind, 30)
	state = "expired" if days < 0 else ("expiring" if days <= window else "ok")
	return {"date": text, "state": state, "days_left": days, "window_days": window}


def _flag(flags: list, section: str, label: str, exp: dict | None) -> None:
	if exp and exp["state"] != "ok":
		flags.append({"section": section, "item": label, **exp})


# ── reads ───────────────────────────────────────────────────────────────────
def _all(doctype, filters, fields, order_by="modified desc", limit=ROW_CAP) -> list:
	if not compat.doctype_exists(doctype):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			doctype,
			filters=filters,
			fields=compat.existing_fields(doctype, fields),
			order_by=order_by,
			limit=limit,
		)
		or []
	]


def _missing(doctype: str) -> dict:
	return {"available": False, "rows": [], "note": f"this site has no {doctype}"}


def _identity(emp: dict, wins: dict, flags: list) -> dict:
	out = {
		"available": True,
		"i9": None,
		"w4": [],
		"note": "SSN and document numbers are shown as their last 4 only.",
	}
	if compat.doctype_exists(I9):
		rows = _all(I9, {"employee": emp["name"], "status": ["!=", "Destroyed"]}, ("name",), limit=1)
		if rows:
			doc = frappe.get_doc(I9, rows[0]["name"])
			documents = []
			for part in ("a", "b", "c"):
				title = doc.get(f"list_{part}_doc_title")
				if not title:
					continue
				exp = expiry(doc.get(f"list_{part}_doc_expiry"), "i9_document", wins)
				_flag(flags, "identity", f"I-9 List {part.upper()}: {title}", exp)
				documents.append(
					{
						"list": part.upper(),
						"title": title,
						"authority": doc.get(f"list_{part}_doc_authority"),
						"number": last4(doc.get(f"list_{part}_doc_number")),
						"is_receipt": bool(doc.get(f"list_{part}_is_receipt")),
						"expiry": exp,
					}
				)
			work = expiry(doc.get("alien_work_authorization_expiry"), "work_authorization", wins)
			_flag(flags, "identity", "Work authorization", work)
			receipt = (
				expiry(doc.get("receipt_expires_on"), "i9_receipt", wins)
				if doc.get("receipt_pending")
				else None
			)
			_flag(flags, "identity", "I-9 receipt", receipt)
			out["i9"] = {
				"form": doc.name,
				"status": doc.get("status"),
				"hire_date": str(doc.get("hire_date") or "") or None,
				"citizenship_status": doc.get("citizenship_status"),
				"ssn": last4(doc.get("ssn_last_four")),
				"section_1_signed_at": str(doc.get("section_1_signed_at") or "") or None,
				"section_2_signed_at": str(doc.get("section_2_signed_at") or "") or None,
				"verification_date": str(doc.get("verification_date") or "") or None,
				"document_path": doc.get("document_path"),
				"documents": documents,
				"work_authorization": work,
				"receipt": receipt,
				"reverifications": [
					{
						"date": str(row.get("reverification_date") or "") or None,
						"reason": row.get("reason"),
						"document": row.get("document_title"),
						"number": last4(row.get("document_number")),
						"expiry": expiry(row.get("document_expiry"), "i9_document", wins),
					}
					for row in (doc.get("reverifications") or [])
				],
			}
		else:
			flags.append({"section": "identity", "item": "No I-9 on file", "state": "missing"})
	out["w4"] = [
		{
			"form": row.get("name"),
			"tax_year": row.get("tax_year"),
			"status": row.get("status"),
			"effective_date": str(row.get("effective_date") or "") or None,
			"filing_status": row.get("filing_status"),
			"signed_at": str(row.get("signed_at") or "") or None,
		}
		for row in _all(
			W4,
			{"employee": emp["name"]},
			("name", "tax_year", "status", "effective_date", "filing_status", "signed_at"),
			limit=10,
		)
	]
	return out


def _badge(emp: dict) -> dict:
	badges = _all(BADGE_MAP, {"employee": emp["name"]}, ("badge_id", "company", "active"), limit=20)
	jobs = _all(
		CARD_JOB,
		{"reference_doctype": EMPLOYEE, "reference_name": emp["name"]},
		(
			"name",
			"job_type",
			"status",
			"requested_by",
			"requested_from",
			"creation",
			"printed_at",
			"is_reprint",
			"reprint_reason",
			"error",
		),
		order_by="creation desc",
		limit=50,
	)
	return {
		"available": True,
		"badges": [
			{"badge_id": b.get("badge_id"), "company": b.get("company"), "active": bool(b.get("active"))}
			for b in badges
		],
		"photo": emp.get("image") or None,
		"card_print_jobs": [{**j, "creation": str(j.get("creation") or "")[:19] or None} for j in jobs],
	}


DEVICE_FIELDS = (
	"name",
	"device_name",
	"enrollment_status",
	"platform",
	"os_version",
	"app_version",
	"key_protection",
	"enrolled_at",
	"issued_by",
	"approval_method",
	"approved_by",
	"approved_at",
	"last_seen_on",
	"last_ip",
	"revoked_on",
	"revoked_by",
	"revocation_reason",
	"revocation_kind",
)


def _access(emp: dict, devices_only: bool) -> dict:
	user = str(emp.get("user_id") or "")
	out = {"available": True, "user": user or None, "grant": None, "devices": [], "login_card_filed": False}
	if user and compat.doctype_exists(GRANT) and frappe.db.exists(GRANT, user):
		grant = (
			frappe.db.get_value(
				GRANT,
				user,
				compat.existing_fields(
					GRANT,
					(
						"mobile_role",
						"state",
						"token_issued_on",
						"token_revoked_on",
						"last_seen_on",
						"last_qr_issued_on",
						"qr_expires_at",
						"qr_document",
						"revoked_on",
						"revoked_by",
						"revocation_reason",
					),
				),
				as_dict=True,
			)
			or {}
		)
		out["login_card_filed"] = bool(grant.pop("qr_document", None))
		out["grant"] = {
			k: (str(v)[:19] if isinstance(v, (datetime.date, datetime.datetime)) else v)
			for k, v in grant.items()
		}
		out["devices"] = _all(
			DEVICE, {"parent": user, "parenttype": GRANT}, DEVICE_FIELDS, order_by="idx asc", limit=50
		)
	if not devices_only and user:
		out["roles"] = sorted(r for r in (frappe.get_roles(user) or []) if r not in ("All", "Guest"))
		out["recent_logins"] = [
			{"at": str(r.get("creation") or "")[:19], "status": r.get("status"), "from": r.get("ip_address")}
			for r in _all(
				"Activity Log",
				{"user": user, "operation": "Login"},
				("creation", "status", "ip_address"),
				order_by="creation desc",
				limit=LOGINS,
			)
		]
	if not user:
		out["note"] = "This employee has no user account, so there is no phone or login to show."
	return out


def _training(emp: dict, wins: dict, flags: list) -> dict:
	records = []
	for row in _all(
		TRAINING_RECORD,
		{"employee": emp["name"]},
		("name", "training_type", "status", "completed_date", "expires_date", "provider"),
		order_by="completed_date desc",
	):
		exp = expiry(row.get("expires_date"), "training", wins)
		_flag(flags, "training", f"Training: {row.get('training_type')}", exp)
		records.append(
			{
				**row,
				"completed_date": str(row.get("completed_date") or "") or None,
				"expires_date": None,
				"expiry": exp,
			}
		)
	sessions = []
	attended = _all(
		ATTENDEE,
		{"employee": emp["name"], "parenttype": SESSION},
		("parent", "attended", "signed_at"),
		order_by="creation desc",
	)
	by_parent = {str(a.get("parent")): a for a in attended}
	if by_parent:
		for row in _all(
			SESSION,
			{"name": ["in", list(by_parent)]},
			("name", "training_type", "status", "session_date", "expires_date"),
			order_by="session_date desc",
		):
			a = by_parent.get(row["name"], {})
			sessions.append(
				{
					"session": row["name"],
					"training_type": row.get("training_type"),
					"status": row.get("status"),
					"date": str(row.get("session_date") or "") or None,
					"attended": bool(a.get("attended")),
					"signed_at": str(a.get("signed_at") or "")[:19] or None,
					"expiry": expiry(row.get("expires_date"), "training", wins),
				}
			)
	certs = []
	for row in _all(
		CERTIFICATION,
		{"holder": ["in", [v for v in (emp["name"], emp.get("employee_name")) if v]]},
		("name", "cert_name", "cert_type", "status", "issuing_body", "expiration_date"),
		order_by="expiration_date asc",
	):
		exp = expiry(row.get("expiration_date"), "certification", wins)
		_flag(flags, "training", f"Certification: {row.get('cert_name')}", exp)
		certs.append({**{k: v for k, v in row.items() if k != "expiration_date"}, "expiry": exp})
	return {"available": True, "records": records, "sessions": sessions, "certifications": certs}


def _signed(emp: dict, no_discipline: bool) -> dict:
	out = {"available": True}
	if no_discipline:
		out["discipline"] = {"available": False, "note": "needs HR Manager"}
	else:
		out["discipline"] = [
			{
				**row,
				"issued_on": str(row.get("issued_on") or "") or None,
				"sealed_pdf": bool(row.get("sealed_pdf")),
			}
			for row in _all(
				DISCIPLINE,
				{"employee": emp["name"]},
				(
					"name",
					"discipline_type",
					"step_number",
					"status",
					"issued_on",
					"issued_by_name",
					"employee_acknowledged",
					"employee_declined_to_sign",
					"followup_date",
					"sealed_pdf",
				),
				order_by="issued_on desc",
			)
		]
	groups: dict = {}
	for row in _all(
		EVIDENCE,
		{"signer": emp["name"]},
		(
			"name",
			"document_type",
			"document_name",
			"signature_role",
			"verification_method",
			"signed_at",
			"status",
			"sealed_pdf",
		),
		order_by="signed_at desc",
	):
		groups.setdefault(str(row.get("document_type") or "Other"), []).append(
			{
				"evidence": row["name"],
				"document": row.get("document_name"),
				"role": row.get("signature_role"),
				"method": row.get("verification_method"),
				"signed_at": str(row.get("signed_at") or "")[:19] or None,
				"status": row.get("status"),
				"sealed_pdf": bool(row.get("sealed_pdf")),
			}
		)
	out["signing_evidence"] = groups
	out["policy_acknowledgments"] = groups.get("Compliance Policy", [])
	return out


def _housing(emp: dict) -> dict:
	rows = _all(
		HOUSING,
		{"employee": emp["name"]},
		("name", "unit", "assigned_date", "end_date", "status"),
		order_by="assigned_date desc",
	)
	return {
		"available": True,
		"assignments": [
			{
				**r,
				"assigned_date": str(r.get("assigned_date") or "") or None,
				"end_date": str(r.get("end_date") or "") or None,
			}
			for r in rows
		],
	}


def _tasks(emp: dict) -> dict:
	fields = ("name", "task_name", "task_type", "state", "urgency", "template", "completed_at", "modified")
	assigned = _all(TASK, {"assigned_to": emp["name"]}, fields, limit=100)
	about = _all(TASK, {"subject_doctype": EMPLOYEE, "subject_docname": emp["name"]}, fields, limit=100)
	seen, rows = set(), []
	for row, relation in [(r, "assigned") for r in assigned] + [(r, "about this person") for r in about]:
		if row["name"] in seen:
			continue
		seen.add(row["name"])
		rows.append(
			{k: (str(v)[:19] if k in ("completed_at", "modified") and v else v) for k, v in row.items()}
			| {"relation": relation}
		)
	return {"available": True, "tasks": rows}


EMPLOYEE_FIELDS = (
	"name",
	"employee_name",
	"company",
	"department",
	"designation",
	"status",
	"date_of_joining",
	"user_id",
	"image",
)


def build(employee: str, viewer: str, sections=None, warning_days=None) -> dict:
	"""The whole file for one employee, as this viewer may see it. Raises NotAllowed / ValueError."""
	if not compat.doctype_exists(EMPLOYEE) or not frappe.db.exists(EMPLOYEE, employee):
		raise ValueError(f"no Employee {employee!r}")
	emp = dict(
		frappe.db.get_value(
			EMPLOYEE, employee, compat.existing_fields(EMPLOYEE, EMPLOYEE_FIELDS), as_dict=True
		)
		or {}
	)
	emp["name"] = employee
	allowed = visible_sections(viewer, employee)
	wanted = [s for s in (sections or SECTIONS) if s in SECTIONS]
	wins = windows(warning_days)
	flags: list = []
	out = {
		"employee": employee,
		"employee_name": emp.get("employee_name"),
		"company": emp.get("company"),
		"department": emp.get("department"),
		"designation": emp.get("designation"),
		"status": emp.get("status"),
		"date_of_joining": str(emp.get("date_of_joining") or "") or None,
		"viewer": viewer,
		"own_file": bool(allowed.get("_self")),
		"as_of": str(frappe.utils.today())[:10],
		"expiry_windows_days": wins,
		"sections": {},
	}
	builders = {
		"identity": lambda: _identity(emp, wins, flags),
		"badge": lambda: _badge(emp),
		"access": lambda: _access(emp, bool(allowed.get("_devices_only"))),
		"training": lambda: _training(emp, wins, flags),
		"signed_documents": lambda: _signed(emp, bool(allowed.get("_no_discipline"))),
		"housing": lambda: _housing(emp),
		"tasks": lambda: _tasks(emp),
	}
	for name in wanted:
		if allowed.get(name) is not True:
			out["sections"][name] = {"available": False, "note": str(allowed.get(name))}
			continue
		try:
			out["sections"][name] = builders[name]()
		except Exception as exc:  # pragma: no cover - one broken register does not hide the rest
			out["sections"][name] = {"available": False, "note": f"could not be read ({type(exc).__name__})"}
	order = {"expired": 0, "missing": 1, "expiring": 2}
	flags.sort(
		key=lambda f: (
			order.get(f.get("state"), 3),
			f.get("days_left") if f.get("days_left") is not None else 0,
		)
	)
	out["flags"] = flags
	out["summary"] = {
		"expired": sum(1 for f in flags if f.get("state") == "expired"),
		"expiring": sum(1 for f in flags if f.get("state") == "expiring"),
		"missing": sum(1 for f in flags if f.get("state") == "missing"),
	}
	return out


# ── §3 the per-person audit packet ──────────────────────────────────────────
def _esc(value) -> str:
	return html.escape("" if value is None else str(value))


def _table(rows: list, columns: list) -> str:
	if not rows:
		return "<p><i>None on file.</i></p>"
	head = "".join(f"<th>{_esc(label)}</th>" for _key, label in columns)
	body = "".join(
		"<tr>" + "".join(f"<td>{_esc(_cell(row.get(key)))}</td>" for key, _label in columns) + "</tr>"
		for row in rows
	)
	return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _cell(value) -> str:
	if isinstance(value, dict) and "state" in value:
		return f"{value.get('date')} ({value['state']})"
	if isinstance(value, bool):
		return "yes" if value else "no"
	return "" if value is None else str(value)


def packet_html(data: dict) -> str:
	s = data["sections"]
	parts = [
		"<html><head><meta charset='utf-8'><style>body{font-family:sans-serif;font-size:10pt}"
		"table{border-collapse:collapse;width:100%;margin:4pt 0}td,th{border:1px solid #999;padding:2pt 4pt;text-align:left}"
		"h1{font-size:16pt}h2{font-size:12pt;margin-top:12pt}.flag{color:#a00}</style></head><body>",
		f"<h1>Employee file — {_esc(data.get('employee_name'))} ({_esc(data['employee'])})</h1>",
		f"<p>{_esc(data.get('company'))} · {_esc(data.get('designation'))} · joined {_esc(data.get('date_of_joining'))} · "
		f"as of {_esc(data['as_of'])} · prepared for {_esc(data.get('viewer'))}</p>",
		"<h2>Flags</h2>",
		_table(
			data["flags"],
			[
				("section", "Section"),
				("item", "Item"),
				("state", "State"),
				("date", "Date"),
				("days_left", "Days left"),
			],
		),
	]
	for name in SECTIONS:
		section = s.get(name)
		if section is None:
			continue
		parts.append(f"<h2>{_esc(name.replace('_', ' ').title())}</h2>")
		if section.get("available") is False:
			parts.append(f"<p><i>{_esc(section.get('note'))}</i></p>")
			continue
		if name == "identity":
			i9 = section.get("i9") or {}
			parts.append(
				_table(
					[i9] if i9 else [],
					[
						("form", "I-9"),
						("status", "Status"),
						("hire_date", "Hired"),
						("ssn", "SSN"),
						("work_authorization", "Work authorization"),
					],
				)
			)
			parts.append(
				_table(
					i9.get("documents") or [],
					[("list", "List"), ("title", "Document"), ("number", "Number"), ("expiry", "Expires")],
				)
			)
			parts.append(
				_table(
					section.get("w4") or [],
					[
						("form", "W-4"),
						("tax_year", "Year"),
						("status", "Status"),
						("effective_date", "Effective"),
						("signed_at", "Signed"),
					],
				)
			)
		elif name == "badge":
			parts.append(
				_table(
					section.get("badges") or [],
					[("badge_id", "Badge"), ("company", "Company"), ("active", "Active")],
				)
			)
			parts.append(
				_table(
					section.get("card_print_jobs") or [],
					[
						("name", "Print job"),
						("status", "Status"),
						("creation", "Requested"),
						("printed_at", "Printed"),
						("reprint_reason", "Reprint reason"),
					],
				)
			)
		elif name == "access":
			parts.append(
				_table(
					section.get("devices") or [],
					[
						("device_name", "Device"),
						("enrollment_status", "Status"),
						("app_version", "App"),
						("enrolled_at", "Enrolled"),
						("last_seen_on", "Last seen"),
						("revoked_on", "Revoked"),
					],
				)
			)
			if section.get("roles") is not None:
				parts.append(f"<p>Roles: {_esc(', '.join(section['roles']))}</p>")
		elif name == "training":
			parts.append(
				_table(
					section.get("records") or [],
					[("training_type", "Training"), ("completed_date", "Completed"), ("expiry", "Expires")],
				)
			)
			parts.append(
				_table(
					section.get("sessions") or [],
					[
						("session", "Session"),
						("training_type", "Type"),
						("date", "Date"),
						("attended", "Attended"),
					],
				)
			)
			parts.append(
				_table(
					section.get("certifications") or [],
					[("cert_name", "Certification"), ("status", "Status"), ("expiry", "Expires")],
				)
			)
		elif name == "signed_documents":
			if isinstance(section.get("discipline"), list):
				parts.append(
					_table(
						section["discipline"],
						[
							("name", "Record"),
							("discipline_type", "Type"),
							("step_number", "Step"),
							("issued_on", "Issued"),
							("employee_acknowledged", "Acknowledged"),
						],
					)
				)
			for doctype, rows in (section.get("signing_evidence") or {}).items():
				parts.append(
					f"<p><b>{_esc(doctype)}</b></p>"
					+ _table(
						rows,
						[
							("document", "Document"),
							("role", "As"),
							("method", "Method"),
							("signed_at", "Signed"),
						],
					)
				)
		elif name == "housing":
			parts.append(
				_table(
					section.get("assignments") or [],
					[("unit", "Unit"), ("assigned_date", "From"), ("end_date", "To"), ("status", "Status")],
				)
			)
		elif name == "tasks":
			parts.append(
				_table(
					section.get("tasks") or [],
					[
						("name", "Task"),
						("task_name", "What"),
						("state", "State"),
						("relation", "Relation"),
						("completed_at", "Completed"),
					],
				)
			)
	parts.append(
		"<p><i>I-9 and W-4 forms are not reproduced: they carry the full SSN and document numbers.</i></p></body></html>"
	)
	return "".join(parts)


def _render(html_text: str, data: dict) -> tuple:
	try:
		from frappe.utils.pdf import get_pdf

		pdf = get_pdf(html_text)
		if pdf:
			return pdf, "frappe.utils.pdf (wkhtmltopdf)"
	except Exception:
		pass
	from .render.pdf import PdfDocument

	doc = PdfDocument(
		title=f"Employee file — {data['employee']}",
		author="erpnext_mcp",
		subject="Employee file",
		footer=data["employee"],
	)
	doc.title_block(
		"Employee file", f"{data.get('employee_name')} ({data['employee']})", f"as of {data['as_of']}"
	)
	doc.heading("Flags")
	for flag in data["flags"] or [{"item": "none"}]:
		doc.paragraph(
			f"{flag.get('section', '')}: {flag.get('item')} {flag.get('state', '')} {flag.get('date', '')}".strip()
		)
	for name in SECTIONS:
		if name in data["sections"]:
			doc.heading(name.replace("_", " ").title())
			doc.paragraph(json.dumps(data["sections"][name], default=str)[:4000])
	return doc.render(), "erpnext_mcp render/pdf.py"


def _appendix_files(data: dict) -> list:
	"""Sealed PDFs of discipline and signing evidence, and attended sessions' PDFs. Never an I-9 or W-4."""
	out = []
	signed = data["sections"].get("signed_documents") or {}
	for row in signed.get("discipline") if isinstance(signed.get("discipline"), list) else []:
		if row.get("sealed_pdf"):
			out.append((DISCIPLINE, row["name"], "sealed_pdf"))
	for rows in (signed.get("signing_evidence") or {}).values():
		for row in rows:
			if row.get("sealed_pdf"):
				out.append((EVIDENCE, row["evidence"], "sealed_pdf"))
	for row in (data["sections"].get("training") or {}).get("sessions") or []:
		if row.get("attended"):
			out.append((SESSION, row["session"], "generated_pdf"))
	return out


def _file_bytes(url: str) -> bytes | None:
	try:
		name = frappe.db.get_value("File", {"file_url": url}, "name")
		return frappe.get_doc("File", name).get_content() if name else None
	except Exception:
		return None


def packet(employee: str, viewer: str) -> dict:
	"""Render the file and its sealed PDFs into one PRIVATE File on the Employee."""
	from .tools import artifacts

	data = build(employee, viewer)
	pdf, renderer = _render(packet_html(data), data)
	appended, skipped = [], []
	try:
		from pypdf import PdfReader, PdfWriter
	except Exception:
		PdfReader = PdfWriter = None
	if PdfWriter is not None:
		import io

		writer = PdfWriter()
		for page in PdfReader(io.BytesIO(pdf)).pages:
			writer.add_page(page)
		for doctype, name, field in _appendix_files(data):
			url = frappe.db.get_value(doctype, name, field)
			content = _file_bytes(url) if url else None
			if not content:
				skipped.append(f"{doctype} {name}")
				continue
			try:
				for page in PdfReader(io.BytesIO(content)).pages:
					writer.add_page(page)
				appended.append(f"{doctype} {name}")
			except Exception:
				skipped.append(f"{doctype} {name}")
		buffer = io.BytesIO()
		writer.write(buffer)
		pdf = buffer.getvalue()
	else:
		skipped = [f"{d} {n}" for d, n, _f in _appendix_files(data)]
	file_name = f"employee-file-{employee}-{data['as_of']}.pdf"
	attachment = artifacts.attach_bytes(EMPLOYEE, employee, file_name, pdf)
	return {
		"employee": employee,
		"file": getattr(attachment, "name", None),
		"file_url": getattr(attachment, "file_url", None),
		"file_name": file_name,
		"bytes": len(pdf),
		"renderer": renderer,
		"appended": appended,
		"not_appended": skipped,
		"flags": data["summary"],
		"note": "Filed PRIVATE on the Employee. I-9 and W-4 forms are never appended: they carry the full SSN.",
	}


# ── Desk: the button and the Connections ────────────────────────────────────
SCRIPT_NAME = "Employee — Employee File"
GET_METHOD = "erpnext_mcp.api.employee_file.get"
PACKET_METHOD = "erpnext_mcp.api.employee_file.packet"
SCRIPT_SOURCE = """// erpnext_mcp:employee-file-button@r1
// Added by erpnext_mcp (v0.223.0). Untick `enabled` above, or delete this row,
// to remove the button — the app will not put it back.
frappe.ui.form.on("Employee", {
	refresh: function (frm) {
		if (frm.is_new()) { return; }
		frm.add_custom_button(__("Employee file"), function () {
			frappe.call({ method: "%(get)s", args: { employee: frm.doc.name }, freeze: true }).then(function (r) {
				const answer = (r || {}).message || {};
				const d = new frappe.ui.Dialog({
					title: __("Employee file — {0}", [frm.doc.employee_name || frm.doc.name]),
					size: "extra-large",
					fields: [{ fieldtype: "HTML", fieldname: "file" }],
					primary_action_label: __("Export audit packet"),
					primary_action: function () {
						frappe.call({ method: "%(packet)s", args: { employee: frm.doc.name }, freeze: true }).then(function (p) {
							const out = (p || {}).message || {};
							if (out.file_url) { window.open(out.file_url); frm.reload_doc(); }
						});
					},
				});
				d.fields_dict.file.$wrapper.html(answer.html || "");
				d.show();
			});
		});
	},
});
""".replace("%(get)s", GET_METHOD).replace("%(packet)s", PACKET_METHOD)
CONNECTION_GROUP = "Employee file"
#: (doctype, the field on it that names the Employee). Seeded once each.
CONNECTIONS = (
	(I9, "employee"),
	(W4, "employee"),
	(TRAINING_RECORD, "employee"),
	(EVIDENCE, "signer"),
	(HOUSING, "employee"),
	(DISCIPLINE, "employee"),
	(GRANT, "employee"),
	(CARD_JOB, "reference_name"),
)


def seed_desk_button() -> dict:
	"""The "Employee file" button. Never raises; never overwrites; never re-adds a deleted one."""
	report = {"created": False, "name": SCRIPT_NAME, "reason": ""}
	try:
		if not frappe.db.exists("DocType", "Client Script") or not frappe.db.exists("DocType", EMPLOYEE):
			report["reason"] = "this site has no Client Script or Employee doctype"
			return report
		if frappe.db.exists("Client Script", SCRIPT_NAME):
			report["reason"] = "already present"
			return report
		doc = frappe.get_doc(
			{
				"doctype": "Client Script",
				"name": SCRIPT_NAME,
				"dt": EMPLOYEE,
				"view": "Form",
				"enabled": 1,
				"script": SCRIPT_SOURCE,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		report["created"] = True
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


def remove_desk_button() -> dict:
	report = {"removed": False, "name": SCRIPT_NAME}
	try:
		if frappe.db.exists("Client Script", SCRIPT_NAME):
			frappe.delete_doc("Client Script", SCRIPT_NAME, ignore_permissions=True, force=True)
			report["removed"] = True
	except Exception:  # pragma: no cover
		pass
	return report


def seed_connections() -> list:
	"""The Employee form's "Employee file" Connections group. Never raises; never duplicates."""
	made = []
	try:
		if not frappe.db.exists("DocType", "DocType Link") or not frappe.db.exists("DocType", EMPLOYEE):
			return made
		for doctype, fieldname in CONNECTIONS:
			if not compat.doctype_exists(doctype) or frappe.db.exists(
				"DocType Link", {"parent": EMPLOYEE, "link_doctype": doctype}
			):
				continue
			doc = frappe.get_doc(
				{
					"doctype": "DocType Link",
					"parent": EMPLOYEE,
					"parenttype": "DocType",
					"parentfield": "links",
					"link_doctype": doctype,
					"link_fieldname": fieldname,
					"group": CONNECTION_GROUP,
					"custom": 1,
				}
			)
			doc.insert(ignore_permissions=True)
			made.append(doctype)
		if made:
			try:
				frappe.clear_cache(doctype=EMPLOYEE)
			except Exception:  # pragma: no cover
				pass
	except Exception:  # pragma: no cover - a site mid-migrate
		pass
	return made
