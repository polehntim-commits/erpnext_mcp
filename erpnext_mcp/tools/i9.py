# SPDX-License-Identifier: MIT
"""Structured I-9 workflow: create, fill, verify, reverify, track, destroy.

v0.27.0. Replaces the opaque file attachment that `onboard_employee` used to
make with a structured record carrying Section 1, Section 2, retention dates,
and an immutable audit trail.

EVERY MUTATING ACTION WRITES AN I-9 AUDIT LOG ROW. The log is append-only —
its controller refuses updates — so the trail survives a form edit and answers
"who touched this I-9, when, and from where" without relying on Version history
that somebody with System Manager can amend.

SSN: THE LAST FOUR DIGITS ARE ALWAYS WHAT THIS APP READS. `submit_i9_section_1`
strips to the last four before writing them, and the I-9 Form controller does the
same on every save, so a full SSN that arrives in a JSON payload is reduced
before it touches the `ssn_last_four` column. v0.47.0 added a SECOND column,
`ssn_full`, and it is off by default and stays off unless an operator switches
`store_full_ssn` on in I-9 Settings: E-Verify submits nine digits and cannot be
run from four, so a site that runs E-Verify needs somewhere to keep them and a
site that does not should not have them. It is a Frappe Password field, which
means Frappe writes it to the encrypted `__Auth` table rather than to a column;
NO TOOL IN THIS APP READS IT BACK, `get_i9_form` does not return it, and the
controller blanks it on every save while the switch is off.

────────────────────────────────────────────────────────────────────────────
v0.47.0: THE THREE FEDERAL GAPS
────────────────────────────────────────────────────────────────────────────

SECTION 1 ASKS FOR ONE OF THREE IDENTIFIERS, not for the A-number alone. An
Alien Authorized to Work gives a USCIS/A-Number, OR a Form I-94 admission
number, OR a foreign passport number WITH the country that issued it. Only the
first was storable, so the other two arrived at a form that had nowhere to put
them and were dropped. `submit_i9_section_1` now takes all three and refuses a
status of Alien Authorized to Work that carries none of them — that refusal is
Section 1's own rule, and a form filed without an answer to it is not filed.
A foreign passport number without a country is refused for the same reason: it
identifies nobody.

SECTION 2 CHECKS THE TITLE AGAINST THE LIST IT CLAIMS TO BE FROM. `i9_documents.py`
has seeded all 24 USCIS-accepted documents since v0.27.0 and Section 2 accepted
free text, so nothing stopped a List B document being recorded in the List A slot
— which is a form that says one document proved both identity and work
authorization when it proved neither. The check runs off the I-9 Document Type
table and only where that table has enabled rows for the category, so a site
that has deactivated a document gets its own answer and a site mid-migrate is
not locked out of filing an I-9.

A RECEIPT IS TEMPORARILY ACCEPTABLE AND THE FORM IS STILL COMPLETE. 8 CFR
274a.2(b)(1)(vi) lets an employee who has lost a document present a receipt for
the replacement and work while it is coming, for 90 days from the hire date.
The status therefore stays Complete — the person may work — and `receipt_pending`
with `receipt_expires_on` carries what is still owed. `list_pending_i9_verifications`
reports them, and `reverify_i9` is where the real document lands.

SECTION 3 IS A CHILD TABLE, NOT A SECOND SET OF COLUMNS. `reverify_i9` appends
an `I-9 Reverification` row and never touches Section 2's — what was examined on
the day of hire is the record §1324a asks the employer to have kept, and a
seasonal worker on a renewing authorization is reverified once a season for as
long as they keep coming back. Reverifying an expiring authorization moves
`alien_work_authorization_expiry` forward to the new document's date, so
`list_expiring_work_authorizations` follows the document currently in force
rather than the one it replaced — and moves `Employee.i9_status` off `Expired`,
which is the ONLY write this app makes to that column and the only one it should.
`_clear_expired_i9_column` carries that argument in full.

────────────────────────────────────────────────────────────────────────────
v0.47.1: THE FORM ITSELF, AND THE COPY THAT COMES BACK SIGNED
────────────────────────────────────────────────────────────────────────────

EVERYTHING ABOVE COLLECTED THE DATA AND NOTHING PRODUCED THE FORM. Four
sprints of Section 1, Section 2, receipts, Section 3 and a retention clock, and
what an operator could actually put in a folder was a Desk print of a doctype —
a two-column dump of every field on the record, in the order the JSON declares
them, which is not Form I-9 and is not what an ICE inspection under 8 U.S.C.
§1324a(b)(3) asks to see. `render_i9_pdf` fills the government's own fillable
PDF, which this app now ships at `templates/i9_form.pdf`; `i9_pdf.py` is the
field table and argues its own case, including the four things it leaves
deliberately blank.

`attach_signed_i9` CLOSES THE LOOP AND IS THE HALF THAT MATTERS. The rendered
page is printed and signed by two people with a pen — the signature boxes are
empty on purpose, because 8 CFR 274a.2(h) has requirements a string typed into
a PDF does not meet — and the scan comes back to `signed_pdf`. That file is the
retained federal record. `generated_pdf` is only the page it was printed from,
and the doctype's own field descriptions say so.

`_full_ssn` IS THE CALL SITE THE SSN PARAGRAPH ABOVE PREDICTED. It is the only
code in this app that reads `ssn_full` back, it needs `include_full_ssn` from
the caller AND `store_full_ssn` on the site, and it writes `full_ssn: true` into
the audit row so a page carrying somebody's number is findable afterwards.
`get_i9_form` still does not return it.

────────────────────────────────────────────────────────────────────────────
v0.67.1: THE ONE WAY BACK INTO A SECTION 1 THAT IS ALREADY FILED
────────────────────────────────────────────────────────────────────────────

EVERY TOOL ABOVE MOVES A FORM FORWARD, AND THAT WAS A HOLE. `submit_i9_section_1`
takes a Draft and leaves it at `Section 1 Complete`; nothing takes it back. So a
Section 1 filed with a blank date of birth — because the caller that filed it
never sent one — had no route to a date of birth through any tool in this app,
on any status. The form read Complete, its PDF was rendered, and the retained
federal record was missing a box Section 1 asks for.

`patch_i9_section_1` IS THAT ROUTE AND IS DELIBERATELY THE NARROWEST ONE THAT
CLOSES IT. Four columns — date of birth, email, phone, the last four of the SSN
— each of which is a TRANSCRIPTION of something the employee already told the
employer. The name, the address, the citizenship status and the immigration
identifier are the attestation itself, sworn above a signature, and are refused
BY NAME rather than ignored: a form whose sworn answers were edited after the
signature was made is a form whose signature no longer covers what it says.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import frappe
from frappe.utils import getdate

from .. import compat, i9_pdf, roles, security
from ..args import as_bool, as_date, as_datetime_claim, as_gps, as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import artifacts, files, signers
from . import employee as employee_tool

I9_FORM = "I-9 Form"
I9_AUDIT_LOG = "I-9 Audit Log"
I9_SETTINGS = "I-9 Settings"
I9_DOCUMENT_TYPE = "I-9 Document Type"
I9_REVERIFICATION = "I-9 Reverification"
EMPLOYEE = "Employee"

#: How long a receipt for a lost, stolen or damaged document stands in for the
#: document itself. Read off the I-9 Form controller rather than restated, so the
#: tool and the doctype cannot come to disagree about a statutory deadline.
RECEIPT_VALID_DAYS = 90

#: The statuses a Section 3 entry may be written against. A Draft or a form whose
#: Section 2 was never signed has nothing to reverify — there is no first
#: verification for a second one to follow.
REVERIFIABLE_STATUSES = ("Complete", "Reverification Needed", "Expired")

#: What `reverify_i9` will accept as a reason, mirroring the child doctype's own
#: Select. Kept here as well because the refusal has to name them, and a refusal
#: that says "invalid reason" without saying which ones are valid is a support
#: ticket.
REVERIFICATION_REASONS = ("Work Authorization Expired", "Rehire", "Receipt Replaced", "Name Change")

#: The three identifiers Section 1 will take from an Alien Authorized to Work,
#: any ONE of which answers the question. USCIS calls them exactly this.
ALIEN_IDENTIFIERS = ("alien_registration_number", "i94_admission_number", "foreign_passport_number")

# ── v0.67.1: correcting a Section 1 that has already been filed ────────────

#: The four Section 1 columns `patch_i9_section_1` will write, and the whole of
#: what it will write. Each one is a TRANSCRIPTION of something the employee
#: already told the employer — a date, an address to reach them at, four digits
#: — so a wrong one is a typing mistake and correcting it changes nothing the
#: form attests to. The name, the address, the citizenship status and the
#: immigration identifiers are the attestation itself, sworn under penalty of
#: perjury, and they are deliberately absent for that reason.
PATCHABLE_SECTION_1_FIELDS = ("date_of_birth", "email", "phone", "ssn_last_four")

#: Section 1 arguments this REFUSES BY NAME rather than ignores.
#: `submit_i9_section_1` takes every one of them, so a caller reaching for a
#: correction reaches for the names it already knows; dropping them silently
#: would return a success saying the form was corrected while leaving the wrong
#: name on it. `ssn` — the nine-digit form — is here too: it has its own storage
#: policy and its own site switch (see this module's header), and a correction
#: path that quietly wrote the encrypted column would route around both.
UNPATCHABLE_SECTION_1_FIELDS = (
	"legal_first_name",
	"legal_middle_name",
	"legal_last_name",
	"other_last_names",
	"address_street",
	"address_city",
	"address_state",
	"address_zip",
	"citizenship_status",
	"alien_registration_number",
	"i94_admission_number",
	"foreign_passport_number",
	"foreign_passport_country",
	"alien_work_authorization_expiry",
	"ssn",
	"section_1_signature",
	"preparer_used",
	"preparer_name",
	"preparer_address",
	"preparer_signature",
)

#: The statuses a Section 1 correction may be written against. `Draft` is absent
#: because `submit_i9_section_1` is what fills a draft and it is the tool
#: carrying Section 1's own rules — an Alien Authorized to Work still has to
#: answer with one of the three identifiers, and a patch tool that accepted a
#: draft would be a second way in that skips them. `Destroyed` is absent for the
#: reason `render_i9_pdf` gives about the same status.
CORRECTABLE_STATUSES = ("Section 1 Complete", "Complete")

#: Who may reach back into a Section 1 that has already been filed. NARROWER BY
#: ONE ROLE than `employee.HR_ROLES`, and the missing one is `Farm Manager`.
#: That set is what this app gates HIRING on, because on this site the farm
#: manager is the person who actually hires. A filed I-9 is a different object:
#: it is the retained record 8 U.S.C. §1324a asks an inspection to be shown, and
#: who may amend one afterwards is the personnel-records question, not the
#: hiring one.
CORRECTION_ROLES = ("System Manager", "HR Manager", "HR User")

#: The audit action a correction writes. Lowercase and underscored where every
#: other action on this doctype is Title Case, because this is the string the
#: I-9 Audit Log's own Select declares and a log written under a second spelling
#: is a log `get_i9_audit_log` filters and misses.
CORRECTION_ACTION = "section_1_correction"

#: What a signed copy of an I-9 is allowed to arrive as. A wet-signed form comes
#: back as a scan, and a scan is a PDF or a photograph of one — nothing here is
#: a document format, because this file is never opened, only stored and handed
#: back. `.html` and `.svg` are absent for the reason `api/files.py` states:
#: both execute script when served.
SIGNED_COPY_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif")

#: The Company columns the employer block is built from, where they exist.
COMPANY = "Company"


def _log_action(i9_form: str, employee: str, action: str, details: dict | None = None) -> None:
	"""Write one immutable I-9 Audit Log row. Best effort, never fatal."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": I9_AUDIT_LOG,
				"i9_form": i9_form,
				"employee": employee,
				"timestamp": frappe.utils.now(),
				"user": frappe.session.user if hasattr(frappe, "session") else "Administrator",
				"ip_address": frappe.local.request.remote_addr
				if hasattr(frappe, "local") and hasattr(frappe.local, "request") and frappe.local.request
				else "",
				"action": action,
				"details": json.dumps(details or {}, default=str),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
	except Exception:
		pass


def _resolve_employee(args: dict) -> str:
	"""Accept employee by docname, name, or employee_name."""
	emp = as_str(args, "employee") or as_str(args, "name") or as_str(args, "employee_name")
	if not emp:
		raise ToolError("employee is required.")
	if frappe.db.exists(EMPLOYEE, emp):
		return emp
	found = frappe.db.get_value(EMPLOYEE, {"employee_name": emp}, "name")
	if found:
		return str(found)
	raise ToolError(f"no Employee called {emp!r} on this site.")


def _i9_fields() -> list[str]:
	"""The fields returned by get_i9_form.

	`ssn_full` IS NOT IN THIS LIST AND MUST NOT BE ADDED TO IT. It is a Frappe
	Password field, so reading it needs `get_decrypted_password` and would not
	come back through `get_value` anyway — but the reason it is absent is the
	policy rather than the mechanism: nothing in this app reads the full number
	back, and the day something needs to (an E-Verify submission) it should say
	so at its own call site rather than inherit it from a general-purpose read.
	"""
	return [
		"name",
		"employee",
		"employee_name",
		"company",
		"status",
		"hire_date",
		"legal_first_name",
		"legal_middle_name",
		"legal_last_name",
		"other_last_names",
		"address_street",
		"address_city",
		"address_state",
		"address_zip",
		"date_of_birth",
		"ssn_last_four",
		"email",
		"phone",
		"citizenship_status",
		"alien_registration_number",
		"i94_admission_number",
		"foreign_passport_number",
		"foreign_passport_country",
		"alien_work_authorization_expiry",
		"section_1_signed_at",
		"section_1_signed_ip",
		# v0.136.0. Beside the address it belongs with. See `_signed_gps` for why
		# one Data column holding "lat,lon" rather than two Floats: an unset
		# Float reads back as 0.0, and 0, 0 is a place in the Gulf of Guinea.
		"section_1_signed_gps",
		"preparer_used",
		"preparer_name",
		"preparer_address",
		"document_path",
		"list_a_doc_title",
		"list_a_doc_authority",
		"list_a_doc_number",
		"list_a_doc_expiry",
		"list_a_is_receipt",
		"list_a_doc_copy",
		"list_b_doc_title",
		"list_b_doc_authority",
		"list_b_doc_number",
		"list_b_doc_expiry",
		"list_b_is_receipt",
		"list_b_doc_copy",
		"list_c_doc_title",
		"list_c_doc_authority",
		"list_c_doc_number",
		"list_c_doc_expiry",
		"list_c_is_receipt",
		"list_c_doc_copy",
		"receipt_pending",
		"receipt_expires_on",
		"document_copies_stored",
		"verifier_name",
		"verifier_title",
		"section_2_signed_at",
		"section_2_signed_ip",
		"section_2_signed_gps",
		"verification_date",
		"retention_until",
		"destruction_eligible_date",
		"destroyed_at",
		# v0.47.1. The two halves of the printed form: the page this app filled
		# in and the page that came back with signatures on it. Both are Attach
		# columns holding a private File URL, and both are on this list because
		# a reader who cannot see whether a signed copy was ever filed cannot
		# tell a complete I-9 file from an incomplete one.
		"generated_pdf",
		"generated_pdf_on",
		"signed_pdf",
		"signed_pdf_on",
	]


#: What one Section 3 row reports. The child rows come back on every
#: `get_i9_form` because a reverification history nobody can read is a history
#: that gets collected twice.
REVERIFICATION_FIELDS = (
	"reverification_date",
	"reason",
	"rehire_date",
	"document_title",
	"issuing_authority",
	"document_number",
	"document_expiry",
	"verifier_name",
	"verifier_title",
	"signed_at",
	"signed_ip",
	"notes",
)


def _document_titles(category: str) -> list[str]:
	"""The enabled I-9 Document Type titles for one list category.

	An EMPTY LIST MEANS "do not check", and that is the deliberate reading rather
	than "nothing is acceptable". The table is seeded by `i9_documents.py` on
	every migrate, so it is empty in exactly two situations: a site between
	installing this version and running `bench migrate`, and a site where an
	operator has deactivated every document in a category. Refusing every I-9 on
	a site mid-migrate would make an upgrade a compliance outage, and this app's
	standing promise is that installing it cannot break a site.
	"""
	try:
		rows = frappe.db.get_all(
			I9_DOCUMENT_TYPE,
			filters={"enabled": 1, "list_category": category},
			fields=["doc_title"],
		)
	except Exception:
		return []
	return [str(r["doc_title"]) for r in rows if r.get("doc_title")]


def _check_document_title(title: str, category: str, label: str) -> str:
	"""The title, as the I-9 Document Type table spells it, or the refusal.

	MATCHED CASE-INSENSITIVELY AND RETURNED IN THE TABLE'S OWN SPELLING, because
	a phone that sent "u.s. passport" meant the U.S. Passport and storing the
	lowercase version would put a title on a federal form that does not match the
	list it claims to be from. The refusal names the category's whole accepted
	list: an operator reading "not a List A document" with no list to compare
	against has to go and find one.
	"""
	accepted = _document_titles(category)
	if not accepted:
		return title
	for known in accepted:
		if known.casefold() == title.casefold():
			return known
	raise ToolError(
		f"{label} {title!r} is not a List {category} document on this site. "
		f"List {category} accepts: {', '.join(sorted(accepted))}. "
		"list_i9_document_types has the whole table, including which documents an "
		"operator has deactivated here."
	)


# ── read-only tools ────────────────────────────────────────────────────────


def get_i9_settings(args: dict) -> ToolResult:
	"""Current I-9 configuration."""
	try:
		doc = frappe.get_doc(I9_SETTINGS)
	except Exception:
		return ToolResult(
			data={"note": "I-9 Settings does not exist yet. Run bench migrate."},
			summary="I-9 Settings not found",
		)
	data = {
		"store_document_copies": bool(int(doc.store_document_copies or 0)),
		"enrolled_in_e_verify": bool(int(doc.enrolled_in_e_verify or 0)),
		"store_full_ssn": bool(int(doc.get("store_full_ssn") or 0)),
		"business_legal_name": doc.business_legal_name or "",
		"business_address": doc.business_address or "",
		"business_ein": doc.business_ein or "",
		"reminder_days_before_doc_expiration": doc.reminder_days_before_doc_expiration or 90,
		"reminder_days_before_destruction": doc.reminder_days_before_destruction or 60,
	}
	return ToolResult(data=data, summary="I-9 settings returned")


def get_i9_form(args: dict) -> ToolResult:
	"""Full I-9 record for one employee."""
	employee = _resolve_employee(args)
	name = frappe.db.get_value(I9_FORM, {"employee": employee}, "name")
	if not name:
		raise ToolError(f"no I-9 Form for employee {employee!r}.")
	row = frappe.db.get_value(I9_FORM, name, _i9_fields(), as_dict=True)
	data = {k: (str(v) if v is not None else None) for k, v in row.items()}
	data["reverifications"] = _reverification_history(name)
	data["reverification_count"] = len(data["reverifications"])
	_log_action(name, employee, "Viewed")
	return ToolResult(data=data, summary=f"I-9 for {employee}: {row.get('status')}")


def _reverification_history(i9_name: str) -> list[dict]:
	"""Every Section 3 entry on one form, oldest first.

	Read off the child table directly rather than through `frappe.get_doc`,
	because this runs inside a read tool and loading the parent to reach its
	children would pull the Section 1 columns — including the encrypted SSN
	field — into memory for a caller who asked for a reverification history.
	"""
	try:
		rows = frappe.db.get_all(
			I9_REVERIFICATION,
			filters={"parent": i9_name, "parenttype": I9_FORM},
			fields=["name", *REVERIFICATION_FIELDS],
			order_by="idx asc",
		)
	except Exception:
		return []
	return [{k: (str(v) if v is not None else None) for k, v in dict(r).items()} for r in rows]


def list_i9_forms(args: dict) -> ToolResult:
	"""All I-9 forms with filtering."""
	filters = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	limit = as_int(args, "limit", 100)
	if limit and limit > 500:
		limit = 500

	rows = frappe.db.get_all(
		I9_FORM,
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"company",
			"status",
			"hire_date",
			"receipt_pending",
			"receipt_expires_on",
			"retention_until",
			"destruction_eligible_date",
		],
		limit_page_length=limit,
		order_by="modified desc",
	)
	data = {"forms": [dict(r) for r in rows], "count": len(rows)}
	return ToolResult(data=data, summary=f"{len(rows)} I-9 form(s)")


def list_pending_i9_verifications(args: dict) -> ToolResult:
	"""I-9 forms awaiting employer verification (Section 2), and the receipts running out.

	TWO KINDS OF OUTSTANDING WORK, REPORTED SEPARATELY BECAUSE THEY ARE NOT THE
	SAME OBLIGATION. `pending` is a Section 2 that has not been signed at all,
	and the deadline is three business days from the hire date. `receipts_outstanding`
	is a Section 2 that WAS signed against a receipt for a lost or stolen document
	— the form is Complete, the person may work, and the actual document is owed
	within 90 days. Merging them into one list would put a worker who is lawfully
	employed in the same bucket as one whose paperwork was never done.
	"""
	company = as_str(args, "company")
	scoped = {"company": resolve_company(company)} if company else {}

	filters = {"status": ["in", ["Section 1 Complete", "Awaiting Verification"]], **scoped}
	rows = frappe.db.get_all(
		I9_FORM,
		filters=filters,
		fields=["name", "employee", "employee_name", "company", "status", "hire_date"],
		order_by="hire_date asc",
	)
	today = date.today()
	for row in rows:
		if row.get("hire_date"):
			hire = getdate(row["hire_date"])
			days_since = (today - hire).days
			row["days_since_hire"] = days_since
			row["overdue"] = days_since > 3

	receipts = frappe.db.get_all(
		I9_FORM,
		filters={"receipt_pending": 1, "status": ["not in", ["Destroyed"]], **scoped},
		fields=[
			"name",
			"employee",
			"employee_name",
			"company",
			"status",
			"hire_date",
			"receipt_expires_on",
			"list_a_is_receipt",
			"list_b_is_receipt",
			"list_c_is_receipt",
		],
		order_by="receipt_expires_on asc",
	)
	for row in receipts:
		row["receipt_lists"] = [
			category
			for category, flag in (
				("A", "list_a_is_receipt"),
				("B", "list_b_is_receipt"),
				("C", "list_c_is_receipt"),
			)
			if int(row.get(flag) or 0)
		]
		if row.get("receipt_expires_on"):
			expires = getdate(row["receipt_expires_on"])
			row["days_until_receipt_expiry"] = (expires - today).days
			row["overdue"] = expires < today

	data = {
		"pending": [dict(r) for r in rows],
		"count": len(rows),
		"receipts_outstanding": [dict(r) for r in receipts],
		"receipts_count": len(receipts),
	}
	summary = f"{len(rows)} I-9(s) pending verification"
	if receipts:
		summary += f", {len(receipts)} receipt(s) outstanding"
	return ToolResult(data=data, summary=summary)


def get_i9_audit_log(args: dict) -> ToolResult:
	"""Audit trail for one employee's I-9."""
	employee = _resolve_employee(args)
	limit = as_int(args, "limit", 100)
	if limit and limit > 500:
		limit = 500
	rows = frappe.db.get_all(
		I9_AUDIT_LOG,
		filters={"employee": employee},
		fields=["name", "i9_form", "timestamp", "user", "ip_address", "action", "details"],
		limit_page_length=limit,
		order_by="timestamp desc",
	)
	data = {"entries": [dict(r) for r in rows], "count": len(rows)}
	return ToolResult(data=data, summary=f"{len(rows)} audit log entries for {employee}")


def list_i9_document_types(args: dict) -> ToolResult:
	"""Accepted documents by list category.

	THE GROUPED SHAPE IS THE ONE A FORM ACTUALLY NEEDS, and it is returned
	alongside the flat list rather than instead of it. Section 2 is not a free
	choice among 24 documents: it is "one from List A" or "one from List B AND
	one from List C", and a caller drawing that form has to split the list on
	exactly that line before it can draw anything. Every caller doing the split
	itself is every caller having its own copy of which category is which.
	"""
	filters = {"enabled": 1}
	cat = as_str(args, "list_category")
	if cat:
		filters["list_category"] = cat
	rows = frappe.db.get_all(
		I9_DOCUMENT_TYPE,
		filters=filters,
		fields=["doc_title", "list_category", "uscis_code", "description", "requires_photo"],
		order_by="list_category asc, doc_title asc",
	)
	documents = [dict(r) for r in rows]
	data = {
		"documents": documents,
		"count": len(documents),
		"by_list": {
			category: [d for d in documents if d.get("list_category") == category]
			for category in ("A", "B", "C")
		},
	}
	return ToolResult(data=data, summary=f"{len(documents)} I-9 document type(s)")


def get_i9_retention_report(args: dict) -> ToolResult:
	"""I-9 forms approaching or past their retention date."""
	company = as_str(args, "company")
	filters = {"status": ["not in", ["Destroyed"]]}
	if company:
		filters["company"] = resolve_company(company)

	rows = frappe.db.get_all(
		I9_FORM,
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"company",
			"status",
			"hire_date",
			"retention_until",
			"destruction_eligible_date",
		],
		order_by="retention_until asc",
	)
	today = date.today()
	approaching = []
	eligible = []
	for row in rows:
		r = dict(row)
		if r.get("retention_until"):
			ret = getdate(r["retention_until"])
			r["days_until_retention"] = (ret - today).days
			if r["days_until_retention"] <= 0:
				eligible.append(r)
			elif r["days_until_retention"] <= 90:
				approaching.append(r)

	data = {
		"approaching_retention": approaching,
		"eligible_for_destruction": eligible,
		"approaching_count": len(approaching),
		"eligible_count": len(eligible),
	}
	return ToolResult(
		data=data,
		summary=f"{len(approaching)} approaching retention, {len(eligible)} eligible for destruction",
	)


def list_expiring_work_authorizations(args: dict) -> ToolResult:
	"""Employees whose work authorization expires within N days."""
	company = as_str(args, "company")
	# NOT `... or 90`: `as_int` already answers 90 for a missing value, so the
	# trailing `or` only ever caught an explicit 0 — which is the real question
	# "who is expired or expiring today", widened without being asked to three
	# months. It errs toward showing too much rather than too little, which is
	# why it survived, but it is still not what the caller asked.
	days_ahead = as_int(args, "days_ahead", 90)
	filters = {
		"status": ["not in", ["Destroyed", "Expired"]],
		"citizenship_status": ["in", ["Alien Authorized to Work", "Lawful Permanent Resident"]],
		"alien_work_authorization_expiry": ["is", "set"],
	}
	if company:
		filters["company"] = resolve_company(company)

	rows = frappe.db.get_all(
		I9_FORM,
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"company",
			"citizenship_status",
			"alien_work_authorization_expiry",
		],
		order_by="alien_work_authorization_expiry asc",
	)
	today = date.today()
	cutoff = today + timedelta(days=days_ahead)
	expiring = []
	for row in rows:
		r = dict(row)
		exp = getdate(r["alien_work_authorization_expiry"])
		if exp <= cutoff:
			r["days_until_expiry"] = (exp - today).days
			expiring.append(r)

	data = {"expiring": expiring, "count": len(expiring), "days_ahead": days_ahead}
	return ToolResult(
		data=data, summary=f"{len(expiring)} work authorization(s) expiring within {days_ahead} days"
	)


# ── mutating tools ─────────────────────────────────────────────────────────


def create_i9_form(args: dict) -> ToolResult:
	"""Create a Draft I-9 Form for an employee.

	v0.94.0: `require_hiring_role`, AND IT HAD NO ROLE GATE AT ALL BEFORE. That
	direction is worth stating plainly because it runs against the rest of this
	release: raising a federal hiring form on a named coworker was gated by
	ENROLMENT alone, so any picker with a working handset could open an I-9 on
	anybody in their company. This is a restriction against what shipped and a
	widening against putting it behind `HR_ROLES` — both are true, and the
	hiring role is the set that makes the foreman's hiring day work while a
	field worker is refused.

	THE ATTESTATIONS ARE NOT WHAT THIS GATES. Raising the form writes a Draft
	with an employee, a company and a hire date on it and nothing anybody swears
	to. Section 1 is the worker's own attestation and Section 2 is the
	employer's, and the second one answers to the authorized-signer roster —
	a per-person designation that no role on this list can substitute for.
	"""
	employee_tool.require_hiring_role()
	employee = _resolve_employee(args)
	company = resolve_company(as_str(args, "company"), required=True)
	hire_date = as_date(args, "hire_date", required=True)

	existing = frappe.db.get_value(I9_FORM, {"employee": employee, "status": ["!=", "Destroyed"]}, "name")
	if existing:
		raise ToolError(
			f"employee {employee!r} already has an active I-9 Form ({existing}). "
			"Destroy the existing one before creating a new one, or use the existing form."
		)

	doc = frappe.get_doc(
		{
			"doctype": I9_FORM,
			"employee": employee,
			"company": company,
			"hire_date": hire_date,
			"status": "Draft",
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()

	_log_action(doc.name, employee, "Created", {"hire_date": str(hire_date)})

	return ToolResult(
		data={"name": doc.name, "employee": employee, "status": "Draft"},
		summary=f"created Draft I-9 for {employee}",
		docstatus_delta="none → Draft",
	)


#: The two attestations a Form I-9 is not complete without, and the label each
#: one is refused by name under. Section 3 / Supplement B is deliberately absent:
#: a reverification is an EVENT on a form that was already complete, not a
#: precondition of it, and folding it in here would make a rehire reopen a
#: finished record.
REQUIRED_SIGNATURES = (
	("section_1_signature", "Section 1 (the employee's attestation)"),
	("section_2_signature", "Section 2 (the employer's attestation)"),
)


def _remote_addr() -> str:
	"""The caller's address, or "" where the request has none.

	A function rather than the expression it replaces, because it was written
	twice and the two copies were the two places a signing IP was being stamped
	for a signature that had not been made.
	"""
	try:
		request = getattr(getattr(frappe, "local", None), "request", None)
		return str(getattr(request, "remote_addr", "") or "") if request else ""
	except Exception:  # pragma: no cover - a context with no request
		return ""


def unsigned_boxes(doc) -> list:
	"""Which of the two required attestations this form is still missing.

	Empty means the form carries both and may be Complete. Returns the LABELS
	rather than the fieldnames, because every caller uses them in a sentence
	somebody reads.
	"""
	return [label for field, label in REQUIRED_SIGNATURES if not str(doc.get(field) or "").strip()]


def advance_if_signed(name: str) -> str:
	"""Move an I-9 from `Awaiting Verification` to `Complete` once it is signed.

	THE OTHER HALF OF THE GATE IN `submit_i9_section_2`, and without it that gate
	would be a trap. Section 2's documents are filed, the form rests at
	`Awaiting Verification` because a box was blank, and the signature that fills
	it arrives at the PAD rather than back through the submit call — which is the
	ordinary flow on a handset and the whole point of `collect_form_signature`.
	Something has to notice, and the signature landing is the moment.

	IT ONLY EVER MOVES ONE EDGE. `Awaiting Verification` → `Complete`, and only
	where Section 2 was genuinely filed (`verification_date` is what says so) and
	both attestations are now present. It will not advance a Draft, will not
	touch a form somebody set to Reverification Needed, Expired or Destroyed, and
	will not reopen a Complete one. A status machine that could be driven from a
	signature pad in any other direction would be a status machine.

	RETURNS THE NEW STATUS, or "" where nothing moved. NEVER RAISES: the
	signature is on the record by the time this runs and is the irreplaceable
	artefact — the same ordering rule every step after a capture obeys.
	"""
	try:
		doc = frappe.get_doc(I9_FORM, name)
		if str(doc.get("status") or "") != "Awaiting Verification":
			return ""
		if not doc.get("verification_date") or unsigned_boxes(doc):
			return ""
		doc.status = "Complete"
		doc.flags.ignore_permissions = True
		doc.save()
	except Exception:  # pragma: no cover - see the docstring
		return ""
	_log_action(
		name,
		str(doc.get("employee") or ""),
		"Completed",
		{"reason": "the last outstanding attestation was signed", "status": "Complete"},
	)
	return "Complete"


def _business_days_between(start: date, end: date) -> int:
	"""Count business days between two dates (inclusive of both)."""
	if end < start:
		start, end = end, start
	count = 0
	current = start
	while current <= end:
		if current.weekday() < 5:
			count += 1
		current += timedelta(days=1)
	return count


def _store_full_ssn_enabled() -> bool:
	"""Whether this site has asked to keep nine digits. Absent means no."""
	try:
		return bool(int(frappe.db.get_single_value(I9_SETTINGS, "store_full_ssn") or 0))
	except Exception:
		return False


def submit_i9_section_1(args: dict) -> ToolResult:
	"""Fill Section 1 of an I-9 Form (employee information).

	SECTION 1 ASKS AN ALIEN AUTHORIZED TO WORK FOR ONE OF THREE IDENTIFIERS and
	this refuses a form that answers with none of them: a USCIS/A-Number, a Form
	I-94 admission number, or a foreign passport number with its country of
	issuance. That is the form's own rule rather than this app's, and a Section 1
	filed without an answer to it is not a filed Section 1 — which is why it is a
	refusal here and not a warning somewhere an operator reads later.

	A FOREIGN PASSPORT NUMBER WITHOUT A COUNTRY IS REFUSED. Passport numbering is
	per-issuer; the number alone identifies nobody, and storing half the pair
	would leave a form that looks answered and is not.

	THE FULL SSN IS WRITTEN ONLY WHERE THE SITE ASKED FOR IT. `ssn` takes the
	whole number; `ssn_last_four` takes either. Both are stripped to four digits
	for `ssn_last_four` whatever arrives, and the nine-digit form reaches the
	encrypted `ssn_full` column only where `store_full_ssn` is on in I-9
	Settings — see this module's header for why that is a switch rather than a
	default. A full number sent to a site with the switch off is not an error and
	is not stored: the caller got what it asked for, which is an I-9 with the
	last four on it.

	v0.94.0: `require_hiring_role`, WHERE THERE WAS NO ROLE GATE. Section 1 is
	the employee's own attestation about themselves, and the foreman sitting
	with a new hire filling it in is the case this release exists for — but
	"anybody enrolled may file a Section 1 naming any coworker" was never the
	intended reading of that, and it is what the code said. The worker's own
	signature still arrives through the pad and lands in the column
	`advance_if_signed` reads, so widening WHO MAY PROCESS a Section 1 has never
	at any point let anybody forge one: a form whose Section 1 is unsigned does
	not reach Complete, whoever called this.
	"""
	employee_tool.require_hiring_role()
	employee = _resolve_employee(args)
	i9_name = frappe.db.get_value(I9_FORM, {"employee": employee, "status": "Draft"}, "name")
	if not i9_name:
		raise ToolError(f"no Draft I-9 Form for employee {employee!r}. Create one first with create_i9_form.")

	doc = frappe.get_doc(I9_FORM, i9_name)

	doc.legal_first_name = as_str(args, "legal_first_name", required=True)
	doc.legal_last_name = as_str(args, "legal_last_name", required=True)
	doc.legal_middle_name = as_str(args, "legal_middle_name")
	doc.other_last_names = as_str(args, "other_last_names")
	doc.address_street = as_str(args, "address_street")
	doc.address_city = as_str(args, "address_city")
	doc.address_state = as_str(args, "address_state")
	doc.address_zip = as_str(args, "address_zip")
	doc.date_of_birth = as_date(args, "date_of_birth")
	doc.email = as_str(args, "email")
	doc.phone = as_str(args, "phone")
	doc.citizenship_status = as_str(args, "citizenship_status", required=True)

	ssn = as_str(args, "ssn") or as_str(args, "ssn_last_four")
	if ssn:
		digits = "".join(c for c in ssn if c.isdigit())
		doc.ssn_last_four = digits[-4:] if len(digits) >= 4 else digits
		if len(digits) == 9 and _store_full_ssn_enabled():
			doc.ssn_full = digits

	if doc.citizenship_status in ("Lawful Permanent Resident", "Alien Authorized to Work"):
		doc.alien_registration_number = as_str(args, "alien_registration_number")
	if doc.citizenship_status == "Alien Authorized to Work":
		doc.i94_admission_number = as_str(args, "i94_admission_number")
		doc.foreign_passport_number = as_str(args, "foreign_passport_number")
		doc.foreign_passport_country = as_str(args, "foreign_passport_country")
		doc.alien_work_authorization_expiry = as_date(args, "alien_work_authorization_expiry")

		if doc.foreign_passport_number and not doc.foreign_passport_country:
			raise ToolError(
				"foreign_passport_number was given without foreign_passport_country. "
				"Passport numbers are issued per country and a number on its own identifies "
				"nobody — Section 1 asks for the pair."
			)
		if not any(doc.get(field) for field in ALIEN_IDENTIFIERS):
			raise ToolError(
				"citizenship_status 'Alien Authorized to Work' needs ONE of "
				"alien_registration_number (USCIS/A-Number), i94_admission_number "
				"(Form I-94/I-94A), or foreign_passport_number with "
				"foreign_passport_country. Section 1 of Form I-9 asks for one of the "
				"three and this form answered with none."
			)

	# THE MOMENT AND THE ADDRESS ARE WRITTEN ONLY WHERE A SIGNATURE IS. v0.64.2,
	# and the old shape stamped both unconditionally: a Section 1 with an empty
	# signature column carried a `section_1_signed_at` and a `section_1_signed_ip`
	# anyway. That is the 8 CFR § 274a.2(h) record of when the employee's
	# attestation was made, filled in for an attestation nobody made — a record
	# asserting more than it knows, and the direction that matters, because an
	# inspector reading a timestamp and an IP has been told somebody signed.
	#
	# A SIGNATURE ALREADY ON THE RECORD IS NOT RE-STAMPED EITHER. The pad may
	# have collected it before this call, and `collect_form_signature` wrote the
	# true moment; overwriting it with `now()` would replace the time the person
	# drew it with the time somebody typed the rest of the form.
	sig = as_str(args, "section_1_signature")
	if sig:
		doc.section_1_signature = sig
		doc.section_1_signed_at = frappe.utils.now()
		doc.section_1_signed_ip = _remote_addr()
		# WHERE, ON THE SAME TERMS AS WHEN AND FROM WHAT ADDRESS. v0.136.0.
		# Written only inside this branch for the reason the two lines above
		# are: a location stamped onto a section nobody signed is a record of
		# where an attestation that was never made was not made. Empty when the
		# client sent no fix — `as_gps` refuses half a pair and refuses (0, 0).
		doc.section_1_signed_gps = as_gps(args)

	doc.preparer_used = as_bool(args, "preparer_used", False)
	if doc.preparer_used:
		doc.preparer_name = as_str(args, "preparer_name")
		doc.preparer_address = as_str(args, "preparer_address")
		doc.preparer_signature = as_str(args, "preparer_signature")

	doc.status = "Section 1 Complete"
	doc.flags.ignore_permissions = True
	doc.save()

	_log_action(
		doc.name,
		employee,
		"Section 1 Submitted",
		{
			"citizenship_status": doc.citizenship_status,
			# WHICH identifier, never the identifier itself. The audit log answers
			# "was this form answered and how", and an A-number copied into a JSON
			# blob on a second doctype is one more place a personal identifier lives.
			"identifiers": [f for f in ALIEN_IDENTIFIERS if doc.get(f)],
			"full_ssn_stored": bool(doc.get("ssn_full")),
		},
	)

	return ToolResult(
		data={"name": doc.name, "employee": employee, "status": doc.status},
		summary=f"Section 1 submitted for {employee}",
		docstatus_delta="Draft → Section 1 Complete",
	)


def _require_correction_role() -> str:
	"""The principal a correction is attributed to, once it has proved it may make one.

	THE SAME SHAPE AS `employee.require_hr_role` AND DELIBERATELY NOT A CALL TO
	IT. That function gates on `HR_ROLES`, which this needs a strict subset of —
	see `CORRECTION_ROLES` for why `Farm Manager` is not on this one — and
	threading a role tuple through a function forty call sites depend on, for
	the sake of one, would put this decision somewhere nobody correcting an I-9
	would look for it.

	WHICH IDENTITY is the part worth copying, and the reasoning is that
	function's. `security.caller_identity()` is whoever Frappe authenticated
	THIS request — a handset or a Desk session presenting its own credential —
	and is empty on the ordinary MCP path, where the operator's client presents
	a shared token and there is no human identity to read. There the principal
	is `frappe.session.user`, which by the time any tool runs is the MCP System
	User the operator configured. Both are real principals whose roles the
	operator controls.
	"""
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(
			"this call has no identity to attribute an I-9 correction to. A correction to a "
			"retained federal record is worth nothing without the name of whoever made it, and "
			"there is nobody here to name. Nothing was changed."
		)
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(CORRECTION_ROLES):
		raise ToolError(
			f"{actor} may not correct a filed I-9: it holds none of {', '.join(CORRECTION_ROLES)}. "
			"This is the account this app acts as — an operator sets it with `mcp_system_user` on "
			"ERPNext MCP Settings, and grants it a role in the Desk. Nothing was changed."
		)
	return actor


def _correction_value(field: str, args: dict) -> str:
	"""One replacement value, normalised the way its column stores it.

	EMPTY IS REFUSED, FOR EVERY FIELD. A correction supplies the RIGHT answer;
	clearing a column is a deletion, and a tool whose entire justification is
	"an onboarding wizard failed to send three fields" should not also be the
	quickest way to empty three more. `submit_i9_section_1` is where a blank is
	an ordinary absence, because there the form has not been filed yet.
	"""
	if field == "date_of_birth":
		value = as_date(args, field) or ""
	elif field == "ssn_last_four":
		raw = as_str(args, field)
		digits = "".join(character for character in raw if character.isdigit())
		if digits and len(digits) < 4:
			raise ToolError(
				f"ssn_last_four {raw!r} carries {len(digits)} digit(s). That column holds the last "
				"four of a Social Security number, and a correction that shortened it would replace "
				"a wrong answer with a shorter wrong one. Nothing was changed."
			)
		value = digits[-4:]
	else:
		value = as_str(args, field)
	if not value:
		raise ToolError(
			f"{field} was named with no value. patch_i9_section_1 corrects a field to the right "
			"answer; it does not clear one. A blank column on a filed I-9 is the gap this tool "
			"exists to close, not one for it to open. Nothing was changed."
		)
	return value


def _redraw_generated_pdf(name: str) -> dict:
	"""Bring the rendered page back into step with the record it was drawn from.

	THE SAME DECISION `signatures._redraw` MAKES, for the same reason, and the
	reason is why this is not a bare `render_i9_pdf` call. A form that has never
	been rendered gets NOTHING: producing a federal form nobody asked for is
	this app deciding something that is not its to decide, and `render_i9_pdf`
	is one call away whenever the operator wants the page. What it will not
	leave behind is a rendered page that has gone STALE — the attached PDF is
	the copy an inspection is shown, and one still carrying the empty date of
	birth this call just filled in is the record and its printable copy
	disagreeing about the fact somebody would print it to prove. `overwrite` is
	passed for that; the File that was there stays attached to the record, so
	the copy somebody already printed is not lost.

	NEVER RAISES. The renderer needs `pypdf` and the shipped federal form on
	disk, and a site missing either ends with a corrected record and a stale
	PDF — a smaller problem than a correction this call threw away because it
	could not redraw a page afterwards. The note says which happened.
	"""
	try:
		existing = str(frappe.db.get_value(I9_FORM, name, "generated_pdf") or "").strip()
	except Exception:  # pragma: no cover - a site whose column is not migrated
		existing = ""
	if not existing:
		return {
			"regenerated": False,
			"note": (
				"no PDF had been rendered for this form, so there was nothing to bring up to "
				"date. render_i9_pdf draws one, with the correction on it."
			),
		}
	try:
		result = render_i9_pdf({"i9_form": name, "overwrite": True})
	except Exception as exc:
		return {
			"regenerated": False,
			"note": (
				f"the correction is on the record and the PDF was not redrawn ({exc}). The page "
				f"at {existing} is now out of date with the record it was drawn from; nothing "
				"about the correction depends on it, and render_i9_pdf with overwrite=true is "
				"what brings it back into step."
			),
		}
	data = getattr(result, "data", None) or {}
	return {
		"regenerated": True,
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"replaced": existing or None,
	}


def patch_i9_section_1(args: dict) -> ToolResult:
	"""Correct a transcription gap in a Section 1 that has already been filed.

	THE HOLE THIS FILLS. `submit_i9_section_1` works on a Draft and moves it to
	`Section 1 Complete`; from there the form only goes forward. So a Section 1
	filed with a blank date of birth — because the caller that filed it never
	sent one — had NO route to a date of birth at all, on any status, through
	any tool in this app. The form was complete, its PDF was rendered, and the
	federal record was missing a box that Section 1 asks for. That is the whole
	of the case for this tool, and it is why the tool is this narrow.

	IT WRITES FOUR COLUMNS AND WILL NOT BE TALKED INTO A FIFTH. Date of birth,
	email, phone, and the last four of the SSN — see `PATCHABLE_SECTION_1_FIELDS`
	for the line and `UNPATCHABLE_SECTION_1_FIELDS` for what is on the other
	side of it. The name, the address, the citizenship status and the
	immigration identifier are what the employee swore to under penalty of
	perjury above their own signature; a tool that could edit those after the
	signature was made would produce a form whose signature no longer covers
	what it says. Those are changed by re-attesting, not by patching, and a
	caller that names one is REFUSED rather than quietly ignored — a success
	reporting that a form was corrected while the wrong name is still on it is
	the worse of the two failures by a long way.

	IT MOVES NO STATUS AND SIGNS NOTHING. A `Complete` form stays Complete, and
	the two attestation timestamps are untouched: the employee signed on the day
	they signed, and fixing a typo afterwards does not make that a different day.

	EVERY CORRECTION IS LOGGED, AND THE LOG RECORDS WHICH FIELDS AND NOT WHAT
	THEY NOW SAY. That is the same rule `submit_i9_section_1` follows for the
	immigration identifiers and the reason is unchanged: an audit row is a
	second doctype, and copying a date of birth or four SSN digits into a JSON
	blob on it is one more place a personal identifier lives. What an inspection
	asks of a corrected I-9 is who changed what, and when — which is what a
	lined-through, initialled and dated paper correction records too. The values
	themselves are on the form, and Frappe's own Version row carries the before.

	THE RENDERED PDF IS REDRAWN WHERE THERE IS ONE. `_redraw_generated_pdf`
	argues that at length, including why a form that was never rendered is left
	alone.
	"""
	actor = _require_correction_role()
	name = _resolve_form(args)
	row = frappe.db.get_value(
		I9_FORM,
		name,
		["employee", "employee_name", "status", *PATCHABLE_SECTION_1_FIELDS],
		as_dict=True,
	)
	if not row:  # pragma: no cover - resolved a moment ago
		raise ToolError(f"no I-9 Form called {name!r} on this site.")

	status = str(row.get("status") or "")
	if status not in CORRECTABLE_STATUSES:
		detail = ""
		if status == "Draft":
			detail = (
				"This form's Section 1 has not been filed yet — submit_i9_section_1 is what fills "
				"a Draft, and it is the tool carrying Section 1's own rules. "
			)
		elif status == "Destroyed":
			detail = (
				"This record was certified as disposed of at the end of its retention period, and "
				"amending it afterwards is the one thing that certificate says did not happen. "
			)
		raise ToolError(
			f"I-9 {name} is {status!r}. A Section 1 correction may only be written against "
			f"{' or '.join(repr(state) for state in CORRECTABLE_STATUSES)}. {detail}"
			"Nothing was changed."
		)

	refused = [field for field in UNPATCHABLE_SECTION_1_FIELDS if field in args]
	if refused:
		raise ToolError(
			f"patch_i9_section_1 will not write {', '.join(refused)}. It corrects transcription "
			f"only — {', '.join(PATCHABLE_SECTION_1_FIELDS)} — and what it refused is the part of "
			"Section 1 the employee attested to under penalty of perjury: who they are, where they "
			"live, their citizenship status and the identifier behind it. A Form I-9 whose sworn "
			"answers were edited after the signature was made is a form whose signature no longer "
			"covers what it says. Those are changed by re-attesting. Nothing was changed."
		)

	named = [field for field in PATCHABLE_SECTION_1_FIELDS if field in args]
	if not named:
		raise ToolError(
			"patch_i9_section_1 was called naming none of the fields it can correct. It writes "
			f"{', '.join(PATCHABLE_SECTION_1_FIELDS)} and nothing else. Nothing was changed."
		)

	doc = frappe.get_doc(I9_FORM, name)
	changed = []
	for field in named:
		value = _correction_value(field, args)
		if str(doc.get(field) or "") == value:
			continue
		doc.set(field, value)
		changed.append(field)

	if not changed:
		return ToolResult(
			data={
				"name": name,
				"employee": row.get("employee"),
				"employee_name": row.get("employee_name"),
				"status": status,
				"changed": [],
				"corrected_by": actor,
				# ALWAYS PRESENT AND ALWAYS THE SAME SHAPE, on this path as on
				# the one below: a caller that had to test for the key would
				# have two code paths where it needs one.
				"pdf": {
					"regenerated": False,
					"note": (
						"every field named already held the value given, so nothing was written, "
						"nothing was logged, and there was nothing to redraw."
					),
				},
			},
			summary=f"I-9 {name} already carried what was sent; nothing was changed",
		)

	doc.flags.ignore_permissions = True
	doc.save()

	_log_action(
		name,
		str(row.get("employee") or ""),
		CORRECTION_ACTION,
		{
			"fields": changed,
			# WHICH boxes were blank before, never what they now say. See the
			# docstring; `submit_i9_section_1` logs the immigration identifiers the
			# same way and for the same reason.
			"was_blank": [field for field in changed if not str(row.get(field) or "")],
			"status": status,
			"corrected_by": actor,
			"reason": as_str(args, "reason"),
		},
	)

	redraw = _redraw_generated_pdf(name)

	return ToolResult(
		data={
			"name": name,
			"employee": row.get("employee"),
			"employee_name": row.get("employee_name"),
			"status": status,
			"changed": changed,
			"corrected_by": actor,
			"pdf": redraw,
		},
		summary=(
			f"I-9 {name}: Section 1 corrected — {', '.join(changed)}"
			+ (
				f"; the rendered page was redrawn as {redraw.get('file_name')}"
				if redraw.get("regenerated")
				else "; no rendered page was redrawn"
			)
		),
	)


def submit_i9_section_2(args: dict) -> ToolResult:
	"""Fill Section 2 of an I-9 Form (employer verification).

	THE DOCUMENT TITLES ARE CHECKED AGAINST THE LIST THEY CLAIM TO BE FROM. All
	24 USCIS-accepted documents are seeded by `i9_documents.py` and any of them
	may be recorded; what is refused is a title in the wrong slot — a driver's
	license in the List A slot is a form asserting that one document proved both
	identity and employment authorization, and it proved neither. A site whose
	I-9 Document Type table is empty is not checked at all; `_document_titles`
	sets out why that is the safe direction.

	A RECEIPT IS ACCEPTED AND THE FORM STILL COMPLETES. Under 8 CFR
	274a.2(b)(1)(vi) an employee whose document was lost, stolen or damaged may
	present a receipt for the replacement and work while it comes. So
	`list_a_is_receipt` and its two siblings do NOT hold the form open: the
	status goes to Complete, because the person may lawfully work, and
	`receipt_pending` with `receipt_expires_on` — hire date plus 90 days,
	computed by the controller — carries what is still owed.
	`list_pending_i9_verifications` reports them and `reverify_i9` closes them.

	THE TITLE IS STILL CHECKED WHEN IT IS A RECEIPT, because a receipt is a
	receipt FOR a named document and the document it replaces is what has to be
	on the list. A receipt whose slot is empty of a title is a form recording
	that something was examined without saying what.

	v0.48.0: THE VERIFIER IS CHECKED AGAINST A ROSTER, WHERE THERE IS ONE.
	Section 2 is an attestation under penalty of perjury that a named person
	examined the documents, and `verifier_name` was a string on a JSON body with
	nothing behind it. Where I-9 Settings has authorized signers configured, the
	calling account has to be one of them, and their own printed name and title
	are what go on the form — `signers.resolve_signature` decides, and its
	docstring carries the whole rule including when an explicit name is still
	accepted. Where NO signers are configured the tool behaves exactly as it did
	before, `verifier_name` included, which is what every site is on the day it
	upgrades.
	"""
	employee = _resolve_employee(args)
	i9_name = frappe.db.get_value(
		I9_FORM,
		{"employee": employee, "status": ["in", ["Section 1 Complete", "Awaiting Verification"]]},
		"name",
	)
	if not i9_name:
		raise ToolError(
			f"no I-9 Form in 'Section 1 Complete' or 'Awaiting Verification' status for {employee!r}. "
			"Section 1 must be completed first."
		)

	doc = frappe.get_doc(I9_FORM, i9_name)

	verification_date = as_date(args, "verification_date", required=True)
	hire = getdate(doc.hire_date)
	ver = getdate(verification_date)
	bdays = _business_days_between(hire, ver)
	if bdays > 4:
		raise ToolError(
			f"verification_date {verification_date} is {bdays - 1} business days after hire_date "
			f"{doc.hire_date}. Section 2 must be completed within 3 business days of the hire date."
		)

	doc.document_path = as_str(args, "document_path", required=True)
	if doc.document_path not in ("List A", "List B + C"):
		raise ToolError("document_path must be 'List A' or 'List B + C'.")

	if doc.document_path == "List A":
		doc.list_a_doc_title = _check_document_title(
			as_str(args, "list_a_doc_title", required=True), "A", "list_a_doc_title"
		)
		doc.list_a_doc_authority = as_str(args, "list_a_doc_authority")
		doc.list_a_doc_number = as_str(args, "list_a_doc_number")
		doc.list_a_doc_expiry = as_date(args, "list_a_doc_expiry")
		doc.list_a_is_receipt = as_bool(args, "list_a_is_receipt", False)
		doc.list_b_is_receipt = 0
		doc.list_c_is_receipt = 0
	else:
		doc.list_b_doc_title = _check_document_title(
			as_str(args, "list_b_doc_title", required=True), "B", "list_b_doc_title"
		)
		doc.list_b_doc_authority = as_str(args, "list_b_doc_authority")
		doc.list_b_doc_number = as_str(args, "list_b_doc_number")
		doc.list_b_doc_expiry = as_date(args, "list_b_doc_expiry")
		doc.list_b_is_receipt = as_bool(args, "list_b_is_receipt", False)
		doc.list_c_doc_title = _check_document_title(
			as_str(args, "list_c_doc_title", required=True), "C", "list_c_doc_title"
		)
		doc.list_c_doc_authority = as_str(args, "list_c_doc_authority")
		doc.list_c_doc_number = as_str(args, "list_c_doc_number")
		doc.list_c_doc_expiry = as_date(args, "list_c_doc_expiry")
		doc.list_c_is_receipt = as_bool(args, "list_c_is_receipt", False)
		doc.list_a_is_receipt = 0

	doc.document_copies_stored = as_bool(args, "document_copies_stored", False)
	# Resolved BEFORE the save and after the documents, so a caller who is not
	# authorized to sign is refused having changed nothing — `doc` is still in
	# memory at this point and the record on disk is untouched.
	signature = signers.resolve_signature(args, "I-9", "verifier_name", "verifier_title")
	doc.verifier_name = signature["name"]
	doc.verifier_title = signature["title"]
	doc.verification_date = verification_date

	# Same rule as Section 1's, and see the comment there for why.
	sig = as_str(args, "section_2_signature")
	if sig:
		doc.section_2_signature = sig
		doc.section_2_signed_at = frappe.utils.now()
		doc.section_2_signed_ip = _remote_addr()
		# Same rule and same reason as Section 1's. See there.
		doc.section_2_signed_gps = as_gps(args)

	# ── COMPLETE MEANS SIGNED, AND UNTIL v0.64.2 IT DID NOT ─────────────
	#
	# This line used to read `doc.status = "Complete"` unconditionally, so an
	# I-9 reached its terminal status with both signature boxes empty. The
	# missing-signature rules caught it afterwards and raised two Criticals —
	# which is a DETECTIVE control, and the whole value of a status called
	# Complete is that somebody can read it without running a sweep first.
	#
	# A form is not complete because its fields are full. Section 1 is the
	# employee's attestation under penalty of perjury and Section 2 is the
	# employer's; a Form I-9 carrying neither is a set of answers about
	# documents, and 8 CFR § 274a.2(b)(1) asks for the attestations.
	#
	# THE DOCUMENT DATA IS STILL WRITTEN. Refusing the call outright would throw
	# away the examination somebody actually performed — which documents were
	# produced, by whom, on what date, inside the three-business-day window this
	# function has already checked. So the work is filed and the form rests at
	# `Awaiting Verification`, which is a status this tool already ACCEPTS as
	# input: signing the outstanding box advances it (see `advance_if_signed`),
	# and re-submitting with the signature does too.
	missing = unsigned_boxes(doc)
	doc.status = "Awaiting Verification" if missing else "Complete"
	doc.flags.ignore_permissions = True
	doc.save()

	receipt_lists = [
		category
		for category, flag in (
			("A", "list_a_is_receipt"),
			("B", "list_b_is_receipt"),
			("C", "list_c_is_receipt"),
		)
		if int(doc.get(flag) or 0)
	]

	_log_action(
		doc.name,
		employee,
		"Section 2 Signed",
		{
			"document_path": doc.document_path,
			"verifier_name": doc.verifier_name,
			"verification_date": str(verification_date),
			"receipt_lists": receipt_lists,
			# Whether the name on the form was checked against a roster, and whether
			# it is the calling account's own. Both are facts an inspection asks
			# about a signature and neither is recoverable from the form afterwards.
			"signer_roster": bool(signature["configured"]),
			"signed_on_behalf_of": (signature["override"] or {}).get("user") or None,
		},
	)
	if receipt_lists:
		_log_action(
			doc.name,
			employee,
			"Receipt Accepted",
			{
				"receipt_lists": receipt_lists,
				"receipt_expires_on": str(doc.receipt_expires_on or ""),
			},
		)

	data = {
		"name": doc.name,
		"employee": employee,
		"status": doc.status,
		"receipt_pending": bool(int(doc.receipt_pending or 0)),
		"receipt_expires_on": str(doc.receipt_expires_on) if doc.receipt_expires_on else None,
		"verifier_name": doc.verifier_name,
		"verifier_title": doc.verifier_title or None,
		"signer_roster_enforced": bool(signature["configured"]),
		# v0.64.2. ALWAYS PRESENT, EMPTY WHERE THE FORM IS SIGNED. A caller that
		# had to test for the key would have two code paths where it needs one,
		# and the one it exercises least is the one that ships broken.
		"unsigned": missing,
	}
	if missing:
		data["unsigned_note"] = (
			f"The documents are examined and filed, and this form is NOT Complete: it is missing "
			f"{' and '.join(missing)}. A Form I-9 is complete when it carries the attestations, "
			f"not when its boxes are full — 8 CFR § 274a.2(b)(1) asks for the signatures, and a "
			f"status of Complete on an unsigned form is the one thing an inspection reads and "
			f"believes. Collect the outstanding one with collect_form_signature (or "
			f"submit_form_signature from a handset) and the form advances to Complete on its own."
		)
	summary = f"Section 2 filed for {employee} by {doc.verifier_name}" + (
		f"; AWAITING {' and '.join(missing)}" if missing else " — both attestations present, form Complete"
	)
	if receipt_lists:
		summary += (
			f" against a List {'/'.join(receipt_lists)} receipt — the document itself is "
			f"owed by {doc.receipt_expires_on}"
		)
	return ToolResult(
		data=data,
		summary=summary,
		docstatus_delta=f"Section 1 Complete → {doc.status}",
	)


def update_i9_settings(args: dict) -> ToolResult:
	"""Update I-9 site settings."""
	try:
		doc = frappe.get_doc(I9_SETTINGS)
	except Exception:
		raise ToolError("I-9 Settings does not exist. Run bench migrate.") from None

	changed = []
	for field in ("store_document_copies", "enrolled_in_e_verify", "store_full_ssn"):
		val = as_bool(args, field, None)
		if val is not None:
			setattr(doc, field, val)
			changed.append(field)

	for field in ("business_legal_name", "business_address", "business_ein"):
		val = as_str(args, field)
		if val:
			setattr(doc, field, val)
			changed.append(field)

	for field in ("reminder_days_before_doc_expiration", "reminder_days_before_destruction"):
		val = as_int(args, field)
		if val is not None:
			setattr(doc, field, val)
			changed.append(field)

	if not changed:
		raise ToolError("no fields to update. Pass at least one I-9 Settings field.")

	doc.flags.ignore_permissions = True
	doc.save()

	return ToolResult(
		data={"updated": changed},
		summary=f"I-9 settings updated: {', '.join(changed)}",
	)


def flag_i9_reverification(args: dict) -> ToolResult:
	"""Move an I-9 to Reverification Needed when work auth expires.

	THIS RAISES THE FLAG; `reverify_i9` LOWERS IT. Until v0.47.0 nothing lowered
	it — an I-9 could be marked as needing re-examination and there was no call
	that recorded the re-examination having happened, which left an operator with
	a Desk edit over Section 2's own columns and the day-of-hire record gone.
	"""
	employee = _resolve_employee(args)
	reason = as_str(args, "reason", required=True)
	i9_name = frappe.db.get_value(
		I9_FORM,
		{"employee": employee, "status": ["in", ["Complete", "Reverification Needed"]]},
		"name",
	)
	if not i9_name:
		raise ToolError(f"no Complete I-9 Form for {employee!r} to flag for reverification.")

	doc = frappe.get_doc(I9_FORM, i9_name)
	old_status = doc.status
	doc.status = "Reverification Needed"
	doc.flags.ignore_permissions = True
	doc.save()

	_log_action(
		doc.name,
		employee,
		"Reverification Flagged",
		{
			"reason": reason,
			"previous_status": old_status,
		},
	)

	return ToolResult(
		data={"name": doc.name, "employee": employee, "status": doc.status, "reason": reason},
		summary=f"I-9 for {employee} flagged for reverification: {reason}",
		docstatus_delta=f"{old_status} → Reverification Needed",
	)


def reverify_i9(args: dict) -> ToolResult:
	"""Record a Section 3 entry — Form I-9's Supplement B, Reverification and Rehire.

	THIS IS THE CALL A RETURNING WORKER'S EXPIRED I-9 NEEDS, and it is why it
	exists: `flag_i9_reverification` could say an I-9 needed re-examining and
	nothing in this app could then record that it had been. So an expiring
	authorization went one of two ways on a real site — a second I-9 created
	beside the first, which `create_i9_form` refuses outright, or the Section 2
	columns edited in the Desk, which overwrites what was examined on the day of
	hire. The record §1324a asks an employer to have kept is BOTH: the original
	verification and every reverification since.

	SO IT APPENDS AND NEVER OVERWRITES. Each call adds one `I-9 Reverification`
	row to the parent's table. A seasonal picker on a renewing EAD accumulates
	one a season, in order, and the row from four seasons ago still says what was
	examined four seasons ago.

	IT MOVES TWO COLUMNS AND NO OTHERS. `alien_work_authorization_expiry` goes to
	the new document's date where it carries one — that column is what
	`list_expiring_work_authorizations` reads, and leaving it on the document just
	replaced would go on reporting a renewed authorization as expiring. And
	`Employee.i9_status` moves off `Expired`, which is the only write this app
	makes to that column and the only one it should; `_clear_expired_i9_column`
	is where that argument is set out.

	A REVERIFICATION AGAINST AN ALREADY-EXPIRED DOCUMENT IS REFUSED. Recording
	one would produce a form asserting the employer examined evidence of
	continuing authorization on a day when the document showed the opposite.
	A reverification with NO expiry date is accepted — an unexpiring document is
	a real answer, and the alternative is refusing a lawful reverification for
	lacking a date that does not exist.

	WHICH DOCUMENTS ARE ACCEPTED: List A or List C. Reverification establishes
	continuing employment authorization; List B establishes identity, which does
	not expire and is not re-examined. The title is checked against whichever of
	the two lists it is on, and a List B title is refused with that sentence.

	'Receipt Replaced' IS A REVERIFICATION REASON rather than its own tool. What
	the employer does when the real document turns up is exactly what they do
	when an authorization is renewed — examine it, record it, sign it — and the
	only difference is that this one also clears `receipt_pending`, which it does
	by clearing the receipt flags the controller computes that from.

	v0.94.0: IT NOW RUNS `signers.resolve_signature`, AND IT HAD NO SIGNER CHECK
	AT ALL. Section 3 / Supplement B is an EMPLOYER ATTESTATION — the employer
	stating it examined evidence of continuing work authorization — which is the
	same legal act as Section 2 and was the one place in this module where any
	string could be typed into `verifier_name` and stored as the person who made
	it. `submit_i9_section_2` has resolved its verifier against the authorized-
	signer roster since v0.48.0; this is the same call, on the same roster, with
	the same `required=True`.

	IT IS THE ONE ITEM OF THIS RELEASE'S I-9 WORK THAT IS NEITHER A WIDENING NOR
	A NO-OP. On a site with no roster nothing changes — an explicit
	`verifier_name` is still required and still accepted, exactly as before. On a
	site that HAS named its signers, the name written here now has to be one of
	them.
	"""
	employee = _resolve_employee(args)
	i9_name = frappe.db.get_value(
		I9_FORM,
		{"employee": employee, "status": ["in", list(REVERIFIABLE_STATUSES)]},
		"name",
	)
	if not i9_name:
		raise ToolError(
			f"no I-9 Form for {employee!r} in a state that can be reverified. "
			f"Section 3 records a SECOND examination and needs a first one to follow: "
			f"the form must be in {', '.join(REVERIFIABLE_STATUSES)}. A Draft or a form "
			"whose Section 2 was never signed is completed with submit_i9_section_2, not "
			"reverified."
		)

	doc = frappe.get_doc(I9_FORM, i9_name)

	reason = as_str(args, "reason", required=True)
	if reason not in REVERIFICATION_REASONS:
		raise ToolError(
			f"reason {reason!r} is not one this form records. Accepted: {', '.join(REVERIFICATION_REASONS)}."
		)

	title, category = _reverification_document(as_str(args, "document_title", required=True))

	# Resolved BEFORE anything is appended, so a caller the roster does not
	# authorise is refused having written nothing at all — the same posture
	# `submit_w4` takes and the same reason. `required=True` matches
	# `submit_i9_section_2`: Section 3 is the same employer attestation, so an
	# unconfigured site goes on demanding an explicit `verifier_name` rather than
	# silently substituting one into a federal form.
	_section_3_signature = signers.resolve_signature(
		args, "I-9", "verifier_name", "verifier_title", required=True
	)

	reverification_date = as_date(args, "reverification_date") or str(date.today())
	document_expiry = as_date(args, "document_expiry")
	if document_expiry and getdate(document_expiry) < getdate(reverification_date):
		raise ToolError(
			f"document_expiry {document_expiry} is before reverification_date "
			f"{reverification_date}. A reverification records that the employer examined "
			"evidence of CONTINUING work authorization; a document that had already "
			"expired on the day it was examined is not that."
		)

	rehire_date = as_date(args, "rehire_date")
	if reason == "Rehire" and not rehire_date:
		raise ToolError("reason 'Rehire' needs rehire_date — Supplement B asks for it by name.")

	old_expiry = doc.alien_work_authorization_expiry
	row = doc.append(
		"reverifications",
		{
			"reverification_date": reverification_date,
			"reason": reason,
			"rehire_date": rehire_date,
			"document_title": title,
			"issuing_authority": as_str(args, "issuing_authority"),
			"document_number": as_str(args, "document_number"),
			"document_expiry": document_expiry,
			"verifier_name": _section_3_signature["name"],
			"verifier_title": _section_3_signature["title"] or as_str(args, "verifier_title"),
			"section_3_signature": as_str(args, "section_3_signature"),
			"notes": as_str(args, "notes"),
			"signed_at": frappe.utils.now(),
			"signed_ip": (
				frappe.local.request.remote_addr
				if hasattr(frappe, "local") and hasattr(frappe.local, "request") and frappe.local.request
				else ""
			),
		},
	)

	if document_expiry:
		doc.alien_work_authorization_expiry = document_expiry

	receipt_closed = False
	if reason == "Receipt Replaced":
		if not int(doc.receipt_pending or 0):
			raise ToolError(
				f"the I-9 for {employee!r} has no receipt outstanding, so there is nothing "
				"for a replacement to replace. Record a renewed authorization with reason "
				"'Work Authorization Expired'."
			)
		doc.list_a_is_receipt = 0
		doc.list_b_is_receipt = 0
		doc.list_c_is_receipt = 0
		receipt_closed = True

	old_status = doc.status
	doc.status = "Complete"
	doc.flags.ignore_permissions = True
	doc.save()

	column = _clear_expired_i9_column(employee)

	_log_action(
		doc.name,
		employee,
		"Section 3 Reverified",
		{
			"reason": reason,
			"document_title": title,
			"list_category": category,
			"document_expiry": str(document_expiry or ""),
			"previous_expiry": str(old_expiry or ""),
			"previous_status": old_status,
			"verifier_name": row.verifier_name,
			"receipt_closed": receipt_closed,
			"employee_i9_status": column,
			"entry": len(doc.reverifications),
		},
	)

	return ToolResult(
		data={
			"name": doc.name,
			"employee": employee,
			"status": doc.status,
			"reason": reason,
			"document_title": title,
			"list_category": category or None,
			"document_expiry": str(document_expiry) if document_expiry else None,
			"work_authorization_expiry": (
				str(doc.alien_work_authorization_expiry) if doc.alien_work_authorization_expiry else None
			),
			"receipt_pending": bool(int(doc.receipt_pending or 0)),
			"reverification_count": len(doc.reverifications),
			"employee_i9_status": column,
		},
		summary=(f"I-9 for {employee} reverified ({reason}) against {title} by {row.verifier_name}"),
		docstatus_delta=f"{old_status} → Complete",
	)


def _clear_expired_i9_column(employee: str) -> str | None:
	"""Move `Employee.i9_status` off Expired, and off NOTHING else.

	THE ONE PLACE THIS APP WRITES THAT COLUMN, and the narrowness is the whole
	argument for doing it at all. `i9_status` is a Custom Field
	`compliance_fields.py` installs; v0.46.2 established that no I-9 tool writes
	it and that `employee_detail` reconciles a stale Pending against a live
	record on the way out — while `Expired` is left ALONE, because an expired
	I-9 is somebody's deliberate statement and a Complete form from an earlier
	season is exactly the wrong thing to trust against it.

	A reverification is the one event that answers that statement. Leaving the
	column on Expired afterwards would have `get_employee` go on reporting the
	worker as needing an I-9 — the wizard would route them to `create_i9_form`,
	which refuses because they have one, and the `i9_expired` alert would go on
	firing about an authorization that was renewed this morning. So the deliberate
	statement is answered by an equally deliberate action, and by nothing else:
	a column reading anything OTHER than Expired is not touched, which leaves
	`employee_detail`'s reconciliation the only thing that moves a Pending.

	BEST EFFORT AND NEVER FATAL. The Section 3 row is filed and the audit row is
	written whatever happens here; a site that never ran `install_compliance_fields`
	has no such column, and losing a convenience column must not lose a federal
	record. Returns the value written, or None where nothing was.
	"""
	try:
		from .. import compat

		if not compat.has_field(EMPLOYEE, "i9_status"):
			return None
		current = str(frappe.db.get_value(EMPLOYEE, employee, "i9_status") or "").strip()
		if current != "Expired":
			return None
		# The site's own Select options are the arbiter, exactly as they are in
		# `employee.employee_detail`: an operator who edited the field gets their
		# value or none, never one invented from this module's idea of the options.
		from ..args import select_options

		options = select_options(EMPLOYEE, "i9_status")
		if options and "Verified" not in options:
			return None
		frappe.db.set_value(EMPLOYEE, employee, "i9_status", "Verified")
		return "Verified"
	except Exception:
		return None


def _reverification_document(title: str) -> tuple[str, str]:
	"""The document title as the table spells it, and which list it is on.

	THREE CASES, AND TELLING THEM APART IS THE WHOLE JOB. A List B title is
	refused with its own sentence, because "not a reverification document" and
	"not a document" are different things to read at four in the morning in a
	packing shed. A List A or List C title comes back in the table's own
	spelling. A table with no enabled rows checks nothing and returns the title
	as given — the same reading `_document_titles` takes and for the same reason.
	"""
	try:
		rows = frappe.db.get_all(
			I9_DOCUMENT_TYPE,
			filters={"enabled": 1},
			fields=["doc_title", "list_category"],
		)
	except Exception:
		rows = []
	if not rows:
		return title, ""

	for row in rows:
		if str(row.get("doc_title") or "").casefold() == title.casefold():
			category = str(row.get("list_category") or "")
			if category == "B":
				raise ToolError(
					f"{title!r} is a List B document. List B establishes IDENTITY, which "
					"does not expire and is not re-examined — a reverification records a "
					"List A or List C document establishing continuing employment "
					"authorization."
				)
			return str(row.get("doc_title")), category

	accepted = sorted(
		str(r["doc_title"])
		for r in rows
		if r.get("doc_title") and str(r.get("list_category") or "") in ("A", "C")
	)
	raise ToolError(
		f"document_title {title!r} is not an accepted I-9 document on this site. "
		f"A reverification records a List A or List C document: {', '.join(accepted)}. "
		"list_i9_document_types has the whole table."
	)


def destroy_i9(args: dict) -> ToolResult:
	"""Mark an I-9 as Destroyed after retention period has passed."""
	employee = _resolve_employee(args)
	i9_name = frappe.db.get_value(
		I9_FORM,
		{"employee": employee, "status": ["!=", "Destroyed"]},
		"name",
	)
	if not i9_name:
		raise ToolError(f"no active I-9 Form for {employee!r} to destroy.")

	doc = frappe.get_doc(I9_FORM, i9_name)

	if doc.retention_until:
		ret = getdate(doc.retention_until)
		if date.today() < ret:
			raise ToolError(
				f"this I-9 must be retained until {doc.retention_until}. "
				f"It cannot be destroyed until that date has passed."
			)

	cert = as_str(args, "destruction_certificate")
	old_status = doc.status
	doc.status = "Destroyed"
	doc.destroyed_at = frappe.utils.now()
	if cert:
		doc.destruction_certificate = cert
	doc.flags.ignore_permissions = True
	doc.save()

	_log_action(
		doc.name,
		employee,
		"Destroyed",
		{
			"previous_status": old_status,
			"has_certificate": bool(cert),
		},
	)

	return ToolResult(
		data={
			"name": doc.name,
			"employee": employee,
			"status": "Destroyed",
			"destroyed_at": str(doc.destroyed_at),
		},
		summary=f"I-9 for {employee} destroyed",
		docstatus_delta=f"{old_status} → Destroyed",
	)


# ── v0.47.1: the federal form itself ───────────────────────────────────────


def _resolve_form(args: dict) -> str:
	"""One I-9 Form docname, from a docname or from whoever it belongs to.

	THE DOCNAME IS TRIED FIRST AND `name` IS TRIED AS BOTH. Every other tool in
	this module takes `name` as an alias for `employee`, because every other
	tool is asked about a person; these two are asked about a form — `I9-2026-0001`
	is what a Desk user has in front of them and what the iOS app was given back
	when the form was created. Accepting only the employee would mean an operator
	holding the docname has to go and find whose it is first.
	"""
	explicit = as_str(args, "i9_form") or as_str(args, "form")
	if explicit:
		if frappe.db.exists(I9_FORM, explicit):
			return explicit
		raise ToolError(
			f"no I-9 Form called {explicit!r} on this site. list_i9_forms has the register; "
			f"pass employee= to look one up by the person it belongs to instead."
		)
	docname = as_str(args, "name")
	if docname and frappe.db.exists(I9_FORM, docname):
		return docname

	employee = _resolve_employee(args)
	found = frappe.db.get_value(I9_FORM, {"employee": employee}, "name")
	if not found:
		raise ToolError(f"no I-9 Form for employee {employee!r}.")
	return str(found)


def _employer_block(company: str) -> dict:
	"""Section 2's employer name and address, settings first, entity second.

	I-9 SETTINGS WINS WHERE IT HAS AN ANSWER. `business_legal_name` and
	`business_address` are there precisely so a farm whose Company record is
	named `FAFO` can put `FAFO Farms LLC` — the name on the EIN — on a federal
	form, and a site that has filled them in has said what it wants printed.
	The Company and its linked Address are the fallback, so a site that has
	filled in neither still gets an employer block rather than two empty boxes.

	NEVER RAISES. An employer block is not a reason to refuse to render a form
	that is otherwise complete; `render_i9_pdf` reports which parts came back
	empty, and an empty box on a printed I-9 is a box somebody writes in.
	"""
	block = {"name": "", "address": "", "ein": ""}
	try:
		settings = frappe.get_doc(I9_SETTINGS)
		block["name"] = str(settings.get("business_legal_name") or "").strip()
		block["address"] = str(settings.get("business_address") or "").strip()
		block["ein"] = str(settings.get("business_ein") or "").strip()
	except Exception:  # pragma: no cover - a site whose Single has not migrated
		pass

	if not block["name"]:
		block["name"] = str(company or "").strip()
	if not block["address"]:
		block["address"] = _company_address(company)
	return block


def _company_address(company: str) -> str:
	"""The hiring entity's own address as one line, or "".

	Read through ERPNext's Dynamic Link the same way `employee._jurisdiction_for`
	reads it, and defensively for the same reason: a site without ERPNext's
	address schema is a site that answers "" rather than one that cannot print
	an I-9.
	"""
	if not company:
		return ""
	try:
		if not (compat.doctype_exists("Address") and compat.doctype_exists("Dynamic Link")):
			return ""
		names = (
			frappe.db.get_all(
				"Dynamic Link",
				filters={"link_doctype": COMPANY, "link_name": company, "parenttype": "Address"},
				pluck="parent",
				limit=5,
			)
			or []
		)
		for name in names:
			row = (
				frappe.db.get_value(
					"Address", name, ["address_line1", "city", "state", "pincode"], as_dict=True
				)
				or {}
			)
			parts = [str(row.get(key) or "").strip() for key in ("address_line1", "city")]
			tail = " ".join(
				part
				for part in (str(row.get("state") or "").strip(), str(row.get("pincode") or "").strip())
				if part
			)
			line = ", ".join(part for part in [*parts, tail] if part)
			if line:
				return line
	except Exception:  # pragma: no cover - a site without ERPNext's address schema
		return ""
	return ""


def _full_ssn(i9_name: str, args: dict) -> str:
	"""The nine digits for the SSN box, and the two gates in front of them.

	THIS IS THE ONLY PLACE IN THIS APP THAT READS `ssn_full` BACK, and the
	module docstring said this day would come: a printed I-9 has an SSN box, so
	a call site that genuinely needs the number exists, and it says so here
	rather than inheriting the number from a general-purpose read. `get_i9_form`
	still does not return it and never will.

	BOTH GATES ARE REQUIRED. The caller has to pass `include_full_ssn`, and the
	site has to have `store_full_ssn` switched on — a site that never agreed to
	keep the nine digits has none to print, and a caller who did not ask for
	them gets a blank comb. The read is logged, with `full_ssn: true` in the
	audit row, because a printed page carrying somebody's Social Security
	number is an event a retention audit should be able to find.
	"""
	if not as_bool(args, "include_full_ssn", False):
		return ""
	if not _store_full_ssn_enabled():
		raise ToolError(
			"include_full_ssn was asked for and this site does not store full Social "
			"Security numbers. store_full_ssn is off in I-9 Settings, so there are no nine "
			"digits to print — only the last four, which the SSN box has no way to show as "
			"four. Render without it and the employee writes the number on the printed page, "
			"or switch store_full_ssn on and collect it first. Nothing was changed."
		)
	try:
		# Imported HERE rather than at the top of the module, and it is the only
		# import in this app that reaches into `frappe.utils`' submodules: the
		# encrypted read belongs to this one function, and an import at module
		# scope would put it in front of every tool in the file — including on a
		# bench where `frappe.utils.password` is not what this version calls it.
		from frappe.utils.password import get_decrypted_password

		stored = get_decrypted_password(I9_FORM, i9_name, "ssn_full", raise_exception=False)
	except Exception:  # pragma: no cover - a site whose __Auth row is gone
		stored = ""
	digits = "".join(character for character in str(stored or "") if character.isdigit())
	if len(digits) != 9:
		raise ToolError(
			f"include_full_ssn was asked for and I-9 {i9_name} has no stored full Social "
			f"Security number to print. store_full_ssn is on, but this form was filled in "
			f"before it was — or was filled in without one. Render without it. Nothing was changed."
		)
	return digits


def render_i9_pdf(args: dict) -> ToolResult:
	"""Fill the USCIS Form I-9 from this record and attach it to the record.

	THE PAGE IS THE GOVERNMENT'S. `i9_pdf.py` opens the USCIS fillable PDF this
	app ships, writes the collected values into its own named fields, and hands
	back a copy — so what comes out is Form I-9 with the boxes filled, not a
	reproduction of it and not a field dump. See that module for what it
	deliberately leaves blank: both signature boxes, the alternative-procedure
	tick, and the SSN unless it was asked for by name.

	A SNAPSHOT, NOT A VIEW. The attached PDF is the record as it was when the
	call was made. Anything that edits the form afterwards — a Section 2, a
	reverification, a corrected address — leaves it stale, which is why a second
	render REFUSES unless `overwrite=true` is passed: the likeliest thing in
	that field is the copy somebody already printed and had signed, and
	replacing it silently would repoint the record at a page nobody has seen.
	The old File stays attached either way.

	RENDERING IS NOT FILING AND MOVES NO STATUS. An I-9 is retained by the
	employer rather than filed with anybody, so there is nothing to move; the
	form is whatever it was. `attach_signed_i9` is what records that the printed
	page came back signed.

	Refused on a Destroyed I-9: `destroy_i9` recorded that the record was
	disposed of at the end of its retention period, and reconstituting a
	printable copy of it afterwards is the one thing that certificate says did
	not happen.
	"""
	i9_pdf.require()
	name = _resolve_form(args)
	row = frappe.db.get_value(I9_FORM, name, _i9_fields(), as_dict=True)
	if not row:  # pragma: no cover - resolved a moment ago
		raise ToolError(f"no I-9 Form called {name!r} on this site.")

	if str(row.get("status") or "") == "Destroyed":
		raise ToolError(
			f"I-9 {name} was destroyed on {row.get('destroyed_at') or 'an unrecorded date'} "
			f"at the end of its retention period. Rendering a fresh printable copy of a "
			f"destroyed record would contradict the destruction it certifies. Nothing was changed."
		)

	overwrite = as_bool(args, "overwrite", False)
	existing = str(frappe.db.get_value(I9_FORM, name, "generated_pdf") or "").strip()
	if existing and not overwrite:
		raise ToolError(
			f"I-9 {name} already has a rendered PDF at {existing}. The likeliest thing in "
			f"that field is the copy somebody printed and had signed. Pass overwrite=true to "
			f"render a fresh page and repoint the field; the existing File stays attached to "
			f"the record either way. Nothing was changed."
		)

	record = {key: value for key, value in row.items()}
	record["name"] = name
	# THE TWO CAPTURE COLUMNS ARE READ HERE AND NOT VIA `_i9_fields`, and the
	# bug this line fixes is the reason it is worth a comment. That list is
	# documented as "the fields returned by get_i9_form" and it does not carry
	# `section_1_signature` or `section_2_signature` — they are the URLs of the
	# ink itself, which a reader of the record has no use for. But this function
	# built its `record` out of exactly that list, so `_signature_captures`
	# below was looking for two keys that were never in the dict and returning
	# `{}` every single time. v0.51.0 says at length that the retained page
	# carries the signature the person actually made; from v0.51.0 to v0.57.0 it
	# never did, on any site, and nothing caught it because the stamping is
	# tested against `i9_pdf.fill_i9_pdf` directly, with `signatures=` passed in
	# by the test. Widening `_i9_fields` would have fixed it by also putting two
	# private file URLs into every `get_i9_form` answer, which is a different
	# decision made by accident.
	for column in ("section_1_signature", "section_2_signature"):
		record[column] = frappe.db.get_value(I9_FORM, name, column)
	reverifications = _reverification_history(name)
	employer = _employer_block(str(row.get("company") or ""))
	ssn = _full_ssn(name, args)
	notes = _render_notes(args)

	signatures = _signature_captures(record)
	pdf = i9_pdf.fill_i9_pdf(
		record, employer, reverifications, full_ssn=ssn, notes=notes, signatures=signatures
	)
	file_name = i9_pdf.file_name_for(record)
	attachment = artifacts.attach_bytes(I9_FORM, name, file_name, pdf, field="generated_pdf")
	frappe.db.set_value(I9_FORM, name, "generated_pdf_on", frappe.utils.now(), update_modified=False)

	overflow = max(0, len(reverifications) - i9_pdf.SUPPLEMENT_B_ROWS)
	_log_action(
		name,
		str(row.get("employee") or ""),
		"Printed",
		{
			"file": attachment.get("file_url"),
			"bytes": len(pdf),
			"full_ssn": bool(ssn),
			"replaced": existing or None,
			"edition": i9_pdf.EDITION,
		},
	)

	data = {
		"name": name,
		"employee": row.get("employee"),
		"employee_name": row.get("employee_name"),
		"status": row.get("status"),
		"edition": i9_pdf.EDITION,
		"file_name": file_name,
		"file_url": attachment.get("file_url"),
		"bytes": len(pdf),
		"full_ssn_printed": bool(ssn),
		"replaced": existing or None,
		"reverifications": len(reverifications),
		"reverifications_not_on_page": overflow,
		"employer": employer,
		"incomplete": _incomplete_boxes(record),
		"note": _RENDER_NOTE,
	}
	summary = f"I-9 {name} rendered onto the USCIS form as {file_name} ({len(pdf):,} bytes) and attached" + (
		f", replacing {existing}" if existing else ""
	)
	if data["incomplete"]:
		summary += f" — {len(data['incomplete'])} box(es) left blank for a pen"
	return ToolResult(data=data, summary=summary)


def _signature_captures(record: dict) -> dict:
	"""The two signature images off the record, as bytes, for `fill_i9_pdf`.

	THE READING HAPPENS HERE BECAUSE `i9_pdf` IS A PURE FUNCTION. That module
	takes dicts and returns bytes and touches no database, which is what makes
	it checkable against a fixture; resolving an Attach field to a File row is a
	site read and belongs on this side of the line.

	A CAPTURE THAT CANNOT BE READ IS NOT AN ERROR. A File deleted out from under
	the record, a permission that has changed, a path that moved between private
	and public — each of those costs the SIGNATURE and leaves the box empty for
	a pen, which is the page this app produced for its whole life before
	v0.51.0. Failing the render instead would mean an employer who lost one
	image cannot print the form at all.
	"""
	captures: dict = {}
	for key in i9_pdf.SIGNATURE_BOXES:
		url = str(record.get(f"{key}_signature") or "").strip()
		if not url:
			continue
		try:
			docname = frappe.db.get_value("File", {"file_url": url}, "name")
			if not docname:
				continue
			captures[key] = files.read_file_bytes(str(docname))
		except Exception:
			continue
	return captures


#: Said on every render, because a filled federal form is the thing somebody is
#: most likely to mistake for a completed one.
_RENDER_NOTE = (
	"Signatures captured in the app are stamped into the page content itself and the form is "
	"then flattened, so the retained copy cannot be edited back into an unsigned one. Who "
	"signed, when and from what address are printed in Additional Information — the record "
	"8 CFR 274a.2(h) asks for, since a signature image on its own is not an electronic "
	"signature. A box with no capture behind it is left empty for a pen: print the page, have "
	"it signed, and file the scan back with attach_signed_i9. Rendering moved no status."
)

#: Which of the boxes a complete I-9 needs are empty on this record, and what
#: each one is called on the form. Reported rather than refused: a Draft I-9
#: rendered on purpose — to hand a new hire a page with their own details
#: already on it — is a real and useful thing to do.
_REQUIRED_BOXES = (
	("legal_last_name", "Section 1: Last Name"),
	("legal_first_name", "Section 1: First Name"),
	("date_of_birth", "Section 1: Date of Birth"),
	("address_street", "Section 1: Address"),
	("citizenship_status", "Section 1: citizenship attestation"),
	("hire_date", "Section 2: First Day of Employment"),
	("verifier_name", "Section 2: employer representative"),
)


def _incomplete_boxes(record: dict) -> list[str]:
	"""The named boxes a printed copy of this record will have nothing in."""
	missing = [label for column, label in _REQUIRED_BOXES if not str(record.get(column) or "").strip()]
	documents = [
		key for key in ("list_a", "list_b", "list_c") if str(record.get(f"{key}_doc_title") or "").strip()
	]
	if not documents:
		missing.append("Section 2: no List A or List B+C document recorded")

	return missing


def _render_notes(args: dict) -> list[str]:
	"""Extra lines for the form's Additional Information box.

	Accepted as a list or as one string, because a JSON payload from a phone and
	a hand-typed MCP argument are both reasonable ways to send one line.
	"""
	raw = args.get("additional_information") or args.get("notes")
	if raw is None or raw == "":
		return []
	if isinstance(raw, str):
		return [raw]
	if isinstance(raw, (list, tuple)):
		return [str(item) for item in raw if str(item or "").strip()]
	raise ToolError(
		f"additional_information must be a string or a list of strings, got {type(raw).__name__}."
	)


#: The onboarding document kinds that are photographs of an EXAMINED document,
#: mapped to the column on the I-9 that should point at them. The keys are
#: `employee.ONBOARDING_KINDS` spellings; they are not repeated from memory
#: there, they are asserted equal to it in `tests_standalone/test_i9.py`.
#:
#: `i9_section_2_document` IS NOT ON THIS LIST. It is the kind the app uses for
#: a scan of the completed Section 2 page itself, which is a copy of the FORM
#: rather than of a document that was examined — `signed_pdf` is where that
#: belongs, through `attach_signed_i9`, which checks it is a PDF and refuses to
#: replace an existing one silently.
DOCUMENT_COPY_KINDS = {
	"i9_list_a_document": "list_a_doc_copy",
	"i9_list_b_document": "list_b_doc_copy",
	"i9_list_c_document": "list_c_doc_copy",
}


def link_document_copy(employee: str, kind: str, file_url: str) -> str:
	"""Point this worker's open I-9 at a document photograph just filed. v0.136.0.

	THE PHOTOGRAPHS WERE ALREADY BEING COLLECTED AND WERE GOING NOWHERE THE FORM
	COULD SEE THEM. The onboarding wizard has photographed the List A or List B+C
	documents at the tailgate for several releases and filed them with
	`attach_onboarding_document`, which hangs them off the EMPLOYEE. That is the
	right home for the bytes — it is one upload path with one permission check —
	but it left the I-9 itself holding a `document_copies_stored` tickbox and no
	way to answer "which copies, and are they still there". 8 CFR 274a.2(b)(3)
	says an employer who keeps copies must retain them WITH the I-9 and produce
	them with it, so a form that cannot name them is a form that cannot be
	produced complete.

	NOTHING IS MOVED, RE-UPLOADED OR RE-ATTACHED. The File stays attached to the
	Employee, where `attach_employee_document` put it and where its refusal to
	re-point an existing attachment keeps it. This writes the URL into an
	`Attach` column on the I-9 — which is a string column holding a path, exactly
	as `generated_pdf` and `signed_pdf` are — so the form REFERENCES the copy and
	two records do not each half-own one photograph.

	IT NEVER RAISES, AND THAT IS THE SAME ARGUMENT `signing_evidence.record`
	MAKES. The photograph is the irreplaceable artefact and it has already landed
	by the time this is called; a worker whose document is back in their pocket
	is not standing there any more. A wizard step that filed the picture and then
	failed on the cross-reference must not report a failure that would have the
	operator photograph a passport a second time. The caller reports what this
	returned instead — "" means nothing was linked.

	THE DESTROYED ROW IS EXCLUDED BY NAME. `employee` is not unique on I-9 Form:
	`destroy_i9` sets the status and SAVES rather than deleting, so a rehired
	worker has two or more rows by construction. Linking a fresh photograph of a
	current document onto a record that certifies its own disposal would
	reconstitute part of a form the destruction certificate says is gone.
	"""
	column = DOCUMENT_COPY_KINDS.get(str(kind or "").strip().lower())
	if not column or not str(file_url or "").strip():
		return ""
	try:
		rows = frappe.db.get_all(
			I9_FORM,
			filters={"employee": employee, "status": ["!=", "Destroyed"]},
			fields=["name"],
			order_by="modified desc",
			limit_page_length=1,
		)
		if not rows:
			return ""
		name = str(rows[0].get("name") or "")
		if not name:
			return ""
		# THE TICKBOX IS SET TOO, because otherwise the record contradicts
		# itself. `document_copies_stored` is what Section 2 was told by whoever
		# submitted it and what the Desk print format reports; a form holding a
		# photograph of a passport while answering "copies stored: no" is a
		# record an inspector would be right to distrust. Only ever set, never
		# cleared: this call knows a copy arrived and cannot know that one was
		# removed. Section 2's own write is the only thing that unticks it, and
		# it cannot run after this — the status has already moved past it.
		frappe.db.set_value(
			I9_FORM,
			name,
			{column: file_url, "document_copies_stored": 1},
			update_modified=False,
		)
	except Exception:  # pragma: no cover - a site mid-migrate, or no I-9 at all
		return ""
	return name


#: The signing metadata a phone-built I-9 carries home with it, per section.
#: v0.137.0. Each entry is (column prefix, the caller's two coordinate keys).
SIGNING_METADATA_SECTIONS = (
	("section_1", ("section_1_gps_lat", "section_1_gps_lon")),
	("section_2", ("section_2_gps_lat", "section_2_gps_lon")),
)


def _signing_metadata(args: dict, name: str, row: dict) -> dict:
	"""When and where each section was signed, as the handset reports it. v0.137.0.

	THE ARCHITECTURE MOVED AND THIS IS THE HOLE IT LEFT. The iOS app builds and
	seals the retained I-9 on the phone and files the finished file here, so the
	signatures no longer arrive at the server as separate calls — and every
	column that used to be filled as a side effect of receiving one
	(`section_1_signed_at`, `_signed_ip`, `_signed_gps`) stayed empty. 8 CFR
	274a.2(h)(2) asks for a record of WHO signed and WHEN, and a retained form
	whose only timestamp is "when the file arrived" does not have it: a crew
	signs in an orchard with no bars and the phone uploads at the shed an hour
	later.

	THE TIMESTAMP IS THE CLIENT'S CLAIM AND IS RECORDED AS ONE. `submit_signature`
	refuses a client-supplied `signed_on` and stamps its own, on the argument that
	a handset which could set it could backdate it. That argument is sound and
	does not survive the move: the server is no longer present at the signing, so
	stamping its own clock would record the upload and label it the attestation —
	a wrong answer rather than a missing one. So the claim is taken, and the
	server's own arrival time is kept separately and unaltered in `signed_pdf_on`.
	Both are on the record and an audit can compare them.

	A FUTURE TIMESTAMP IS REFUSED, because it is the one claim that cannot be
	true and the one a clock-skewed or tampered handset produces. Everything else
	is corroboration this app does not pretend to verify.

	NOTHING ALREADY RECORDED IS OVERWRITTEN. A signature that DID come through
	`collect_form_signature` was timed at the pad, by the server, at the moment
	it was drawn; a later upload restating it must not replace the better record
	with the weaker one.
	"""
	updates: dict = {}
	now = frappe.utils.now()
	for prefix, gps_keys in SIGNING_METADATA_SECTIONS:
		stamp = as_str(args, f"{prefix}_signed_at")
		fix = as_gps(args, gps_keys)
		if not stamp and not fix:
			continue
		if stamp:
			moment = as_datetime_claim(stamp, f"{prefix}_signed_at", now)
			if not str(row.get(f"{prefix}_signed_at") or "").strip():
				updates[f"{prefix}_signed_at"] = moment
				# The address is the server's own observation and is the one part
				# of this packet the caller cannot state. It goes on beside the
				# moment it belongs to rather than on its own.
				if not str(row.get(f"{prefix}_signed_ip") or "").strip():
					updates[f"{prefix}_signed_ip"] = _remote_addr()
		if fix and not str(row.get(f"{prefix}_signed_gps") or "").strip():
			updates[f"{prefix}_signed_gps"] = fix
	return updates


def attach_signed_i9(args: dict) -> ToolResult:
	"""File the signed or scanned copy against the I-9 record it belongs to.

	THIS IS THE COPY THAT MATTERS. Everything else on the record is the data
	that was collected; this is the page two people put their names on, and
	8 U.S.C. §1324a asks the employer to have kept exactly that for the
	retention period. `render_i9_pdf` produces the page to print; this is the
	other half of the loop.

	THE FILE IS UPLOADED FIRST AND NAMED HERE. It arrives as a File that already
	exists on the site — `stage_file_chunk` / `finalize_staged_file` for a
	phone, a Desk upload for anybody else — and this call attaches it to the I-9
	and points `signed_pdf` at it. Nothing is decoded here and no bytes cross
	this boundary: an endpoint that took a base64 body would be a second upload
	path with its own size limit, its own hash check and its own way of failing
	halfway through a bad link in an orchard.

	THE FILE IS MADE PRIVATE ON THE WAY IN, whatever it was. A signed I-9 names
	a person, their date of birth and their immigration status, and a public URL
	for it is a data breach that nobody has to guess a password for.

	A SECOND SIGNED COPY REFUSES unless `overwrite=true`. Replacing the signed
	original silently is the one write on this doctype that could not be undone
	from the record itself.
	"""
	name = _resolve_form(args)
	row = frappe.db.get_value(
		I9_FORM,
		name,
		[
			"employee",
			"employee_name",
			"status",
			"signed_pdf",
			# v0.137.0. Read so `_signing_metadata` can see what is already
			# recorded: a moment captured at the pad is better evidence than one
			# restated by an upload, and must not be replaced by it.
			"section_1_signed_at",
			"section_1_signed_ip",
			"section_1_signed_gps",
			"section_2_signed_at",
			"section_2_signed_ip",
			"section_2_signed_gps",
		],
		as_dict=True,
	)
	if not row:  # pragma: no cover - resolved a moment ago
		raise ToolError(f"no I-9 Form called {name!r} on this site.")

	if str(row.get("status") or "") == "Destroyed":
		raise ToolError(
			f"I-9 {name} was destroyed at the end of its retention period. A signed copy "
			f"filed against a destroyed record would contradict the destruction it certifies. "
			f"Nothing was changed."
		)

	overwrite = as_bool(args, "overwrite", False)
	existing = str(row.get("signed_pdf") or "").strip()
	if existing and not overwrite:
		raise ToolError(
			f"I-9 {name} already has a signed copy at {existing}. That is the retained "
			f"federal record for this hire. Pass overwrite=true only to replace a copy that "
			f"was filed in error; the existing File stays attached to the record either way. "
			f"Nothing was changed."
		)

	# RESOLVED BEFORE THE FILE IS TOUCHED. `_signing_metadata` refuses a
	# timestamp in the future, and a refusal that fired after the File had been
	# made private and re-pointed would leave the bytes moved and the record not
	# updated — the same ordering `submit_i9_section_2` uses for its signer check.
	metadata = _signing_metadata(args, name, row)

	file_name, file_url = _signed_copy(args)
	# THROUGH THE DOCUMENT, NOT THROUGH `db.set_value`, and the difference is the
	# whole reason this file is worth attaching. `is_private` is not a flag on a
	# row: making a public File private MOVES it from `public/files` to
	# `private/files` and rewrites `file_url`, and Frappe's File controller is
	# what does the moving. A `db.set_value` would flip the column, leave the
	# bytes in the public directory, and leave `signed_pdf` pointing at a URL
	# that says private and resolves public — which is the breach this is
	# supposed to prevent, with a record that says it did not happen.
	handle = frappe.get_doc("File", file_name)
	handle.attached_to_doctype = I9_FORM
	handle.attached_to_name = name
	handle.attached_to_field = "signed_pdf"
	handle.is_private = 1
	handle.flags.ignore_permissions = True
	handle.save()

	# The URL AFTER the move, not the one the caller named. A file that has just
	# changed directory has a different URL, and the form has to hold the one
	# that now resolves.
	stored = str(handle.get("file_url") or file_url)
	# `signed_pdf_on` IS THE SERVER'S OWN CLOCK AND STAYS THAT WAY. It records
	# when the file ARRIVED, which is a different fact from when either section
	# was signed, and keeping the two apart is what lets an audit compare them.
	frappe.db.set_value(
		I9_FORM,
		name,
		{"signed_pdf": stored, "signed_pdf_on": frappe.utils.now(), **metadata},
		update_modified=False,
	)

	_log_action(
		name,
		str(row.get("employee") or ""),
		"Signed Copy Filed",
		{
			"file": stored,
			"file_docname": file_name,
			"replaced": existing or None,
			# WHICH COLUMNS THIS UPLOAD FILLED, by name. The moment and the place
			# are the client's claim rather than the server's observation, so the
			# audit row records that they were taken and when they were taken.
			"signing_metadata": sorted(metadata) or None,
		},
	)

	return ToolResult(
		data={
			"name": name,
			"employee": row.get("employee"),
			"employee_name": row.get("employee_name"),
			"status": row.get("status"),
			"signed_pdf": stored,
			"file_docname": file_name,
			"replaced": existing or None,
			# Reported rather than assumed. A caller that sent a fix for a section
			# already carrying one is told it was kept, instead of believing it
			# overwrote it.
			"signing_metadata": {key: metadata[key] for key in sorted(metadata)},
		},
		summary=f"signed I-9 filed against {name}" + (f", replacing {existing}" if existing else ""),
	)


def _signed_copy(args: dict) -> tuple[str, str]:
	"""The File this call is about, as (docname, url), or the refusal.

	Takes a docname or a URL, because the two transports hand back different
	things: `finalize_staged_file` returns a `file_token` that IS the docname,
	and a Desk Attach field holds the URL. `tools/inspections._file_docname`
	makes the same accommodation for the same reason.
	"""
	reference = (
		as_str(args, "file_token")
		or as_str(args, "file")
		or as_str(args, "file_url")
		or as_str(args, "signed_pdf")
	)
	if not reference:
		raise ToolError(
			"attach_signed_i9 needs the file to attach — file_token (what "
			"finalize_staged_file hands back), or file_url for a File already on the site. "
			"Upload the scan first; this call only names it. Nothing was changed."
		)

	docname = ""
	if frappe.db.exists("File", reference):
		docname = reference
	elif reference.startswith("/") or reference.startswith("http"):
		docname = str(frappe.db.get_value("File", {"file_url": reference}, "name") or "")
	if not docname:
		raise ToolError(
			f"no File called {reference!r} on this site. Upload the signed copy first — "
			f"stage_file_chunk then finalize_staged_file from the app, or the Desk's own "
			f"attachment control — and pass what that hands back. Nothing was changed."
		)

	row = frappe.db.get_value("File", docname, ["file_name", "file_url"], as_dict=True) or {}
	stored_name = str(row.get("file_name") or "")
	extension = ("." + stored_name.rsplit(".", 1)[-1].lower()) if "." in stored_name else ""
	if extension not in SIGNED_COPY_EXTENSIONS:
		raise ToolError(
			f"{stored_name or reference!r} is a {extension or 'file with no extension'} and a "
			f"signed I-9 is a scan: {', '.join(SIGNED_COPY_EXTENSIONS)}. Nothing was changed."
		)
	return docname, str(row.get("file_url") or "")
