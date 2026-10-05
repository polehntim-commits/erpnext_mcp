# SPDX-License-Identifier: MIT
"""The four things compliance needs that operations do not produce.

SPRINT 7'S WHOLE STANCE IS THAT COMPLIANCE IS A LENS ON OPERATIONAL DATA. Every
spray IS a Worker Protection Standard record, every hire IS an I-9 record, every
bucket IS an FSMA traceability record — so the compliance columns go on the spray,
the employee and the bucket, where the work already is. That is `compliance_fields.py`,
and it is most of the framework.

This module is the remainder, and the remainder is real. Four kinds of evidence
arrive from OUTSIDE the operation and have no operational act to hang off:

  * **Compliance Policy** — nobody writes a harvest hygiene SOP by harvesting.
  * **Certification** — the certificate comes from a certifier, not from a field.
  * **Regulatory Filing** — the agency's acknowledgement is theirs, not ours.
  * **Audit Event** — an auditor's findings are an outside party's conclusions.

THE TEST THAT KEEPS THIS SET SMALL is the same one that put the spray columns on
the spray record, run in the other direction: if a proposed doctype would be
filled in AFTER an operational act, describing that act, it is a shadow record and
belongs in the operational doctype. If it records something that happened outside
the operation and would exist even if the farm did nothing that day, it belongs
here. Four passes; a fifth would need to pass the same test.

WHAT THE FOUR HAVE IN COMMON, AND WHY IT MATTERS. Each one is the ONLY copy. A
Journal Entry restates a bank statement; a Spray Log restates what the applicator
did. But there is no second copy of the certifier's certificate, of the agency's
docket number, or of whether corrective action four was ever closed — which is
why `before_uninstall` names all four, and why nothing in this module deletes
anything. Superseding a policy writes a link. Renewing a certificate appends to a
history. Closing an audit writes a date and refuses to write it over an open
finding.

THE READS DEFAULT ON AND THE WRITES DEFAULT OFF, like everything else here.
"""

import frappe

from .. import audit_packets, compat
from ..args import (
	as_bool,
	as_choice,
	as_date,
	as_int,
	as_limit,
	as_str,
	resolve_company,
)
from ..errors import ToolError
from ..result import ToolResult
from . import files

POLICY = "Compliance Policy"
CERTIFICATION = "Certification"
FILING = "Regulatory Filing"
AUDIT = "Audit Event"

RELATED_PARTY = "Related Party"
FAMILY = "Family"
EMPLOYEE = "Employee"

REGISTER_CAP = 500

#: Shortest a `reason` may be and still be an explanation. Matches the ceiling
#: `update_journal_entry_party` uses for the same judgement — a mandatory field
#: somebody types "fix" into has been satisfied without being answered.
MIN_REASON = 8

_POLICY_FIELDS = (
	"name",
	"policy_name",
	"category",
	"version",
	"company",
	"policy_owner",
	"status",
	"effective_date",
	"review_due_date",
	"supersedes",
	"superseded_by",
	# v0.245.0. What an SOP covers (decides approvers; gates that work).
	"covers_task_types",
	"covers_positions",
	"attached_document",
	"notes",
	"creation",
	"modified",
	"owner",
)

_CERT_FIELDS = (
	"name",
	"cert_name",
	"cert_type",
	"status",
	"company",
	"holder",
	"issuing_body",
	"issued_date",
	"expiration_date",
	"renewal_window_days",
	"certificate_number",
	"attached_certificate",
	"notes",
	"creation",
	"modified",
	"owner",
)

_FILING_FIELDS = (
	"name",
	"filing_name",
	"agency",
	"filing_type",
	"company",
	"period_covered",
	"status",
	"submission_date",
	"docket_number",
	"response_due_date",
	"response_received_date",
	"response",
	"attached_filing",
	"attached_response",
	"notes",
	"creation",
	"modified",
	"owner",
)

_AUDIT_FIELDS = (
	"name",
	"audit_name",
	"audit_type",
	"auditor",
	"company",
	"audit_date",
	"result",
	"scope",
	"findings",
	"corrective_actions_closed",
	"closed_by",
	"closure_note",
	"attached_report",
	"notes",
	"creation",
	"modified",
	"owner",
)

_ACTION_FIELDS = (
	"finding",
	"severity",
	"status",
	"assigned_to",
	"due_date",
	"closed_date",
	"corrective_action",
	"evidence",
	"notes",
)

#: Corrective-action statuses that mean the work is not done.
OPEN_STATUSES = ("Open", "In Progress")


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _entity(args: dict, required: bool = False) -> str | None:
	return resolve_company(as_str(args, "company"), required=required)


def _date(value) -> str | None:
	return str(value or "") or None


def _row(doctype: str, name: str, fields, label: str, finder: str, company: str = "") -> dict:
	"""One record as a dict, from its docname or from its title field.

	Every one of these four doctypes is named after its title, so a caller who
	knows what a thing is called can name it. A title that matches several records
	is refused with all of them listed rather than resolved to the first — the
	whole point of these records is that somebody relies on them, and quietly
	picking one of two certificates is how the wrong certificate ends up in a
	packet.
	"""
	name = (name or "").strip()
	if not name:
		raise ToolError(f"{label} is required (a {doctype} docname). {finder} has the register.")
	selected = compat.existing_fields(doctype, fields)

	if frappe.db.exists(doctype, name):
		row = dict(frappe.db.get_value(doctype, name, selected, as_dict=True) or {})
		if company and row.get("company") and row["company"] != company:
			raise ToolError(f"{doctype} {name!r} belongs to {row['company']!r}, not {company!r}")
		return row

	filters = {}
	if company:
		filters["company"] = company
	matches = frappe.db.get_all(doctype, filters=filters, fields=selected, limit=REGISTER_CAP)
	hits = [dict(match) for match in matches if str(match.get("name") or "").lower() == name.lower()]
	if len(hits) == 1:
		return hits[0]
	if len(hits) > 1:
		names = ", ".join(sorted(str(hit["name"]) for hit in hits))
		raise ToolError(f"{name!r} matches {len(hits)} records: {names}. Pass the docname.")
	raise ToolError(f"no {doctype} called {name!r} on this site. {finder} has the register.")


def _stage(changes: dict, doc, field: str, wanted) -> None:
	"""Set one field, recording before → after only when it actually moved."""
	before = doc.get(field)
	before = "" if before is None else before
	if str(before) == str(wanted):
		return
	changes[field] = [before or None, wanted or None]
	doc.set(field, wanted if wanted != "" else None)


def _resolve_holder(holder: str) -> tuple:
	"""Which register a certificate holder's name is in, if any.

	Free text with resolution on read, exactly as `Family.related_to` does it and
	for the same reason: a holder may be a Related Party, a Family member, an
	Employee or a company, and a Frappe Link points at exactly one doctype. A name
	in no register is not an error — an applicator licence held by a contractor
	who is on nobody's payroll is precisely what the fallback is for.

	Never raises. It runs on every row of `list_certifications`.
	"""
	holder = str(holder or "").strip()
	if not holder:
		return None, None
	try:
		for doctype, title_field in (
			(RELATED_PARTY, "party_name"),
			(FAMILY, "family_member_name"),
			(EMPLOYEE, "employee_name"),
		):
			if not compat.doctype_exists(doctype):
				continue
			if frappe.db.exists(doctype, holder):
				return doctype, holder
			if compat.has_field(doctype, title_field):
				match = frappe.db.get_value(doctype, {title_field: holder}, "name")
				if match:
					return doctype, match
		if compat.doctype_exists("Company") and frappe.db.exists("Company", holder):
			return "Company", holder
	except Exception:  # pragma: no cover - a half-migrated site mid-`bench migrate`
		return None, None
	return None, None


def _reason(args: dict, key: str = "reason") -> str:
	reason = as_str(args, key, required=True)
	if len(reason) < MIN_REASON:
		raise ToolError(
			f"{key} must be a real explanation, not a word. It is the part of this record "
			"nobody can reconstruct later. Nothing was changed."
		)
	return reason


# ── Compliance Policy ───────────────────────────────────────────────────────
def _policy_attachments(names) -> dict:
	"""`{policy: [file_url, ...]}` for a batch, in one query.

	WHY THIS EXISTS AT ALL. `Compliance Policy.attached_document` is an Attach
	field, and `attach_file_to_document` — the generic door, and the one an
	operator reaches for — creates a File linked to the policy WITHOUT writing
	that field, because it is a generic tool and knows nothing about which of a
	doctype's fields is meant to hold a document. So a policy with the SOP
	genuinely attached read as `has_document: false`, and both the register and
	the audit packet reported a written procedure as an unsupported claim.

	Reading the File table settles it. The field stays authoritative when it is
	set — `create_compliance_policy` writes it — and this is the second place to
	look rather than a replacement for the first.
	"""
	names = [str(name) for name in names if name]
	if not names or not compat.doctype_exists("File"):  # pragma: no cover - every site has File
		return {}
	found: dict = {}
	for row in (
		frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": POLICY, "attached_to_name": ("in", names)},
			# `attached_to_name` is a framework column rather than one of File's own
			# fields on some sites, so it is asked for by name: a batched read that
			# lost it would file every attachment under one empty key.
			fields=["name", "attached_to_name", "file_name", "file_url"],
			limit=REGISTER_CAP * 4,
		)
		or []
	):
		entry = dict(row)
		found.setdefault(str(entry.get("attached_to_name") or ""), []).append(
			{
				"file": entry.get("name"),
				"file_name": entry.get("file_name"),
				"file_url": entry.get("file_url"),
			}
		)
	return found


def _describe_policy(row: dict, today: str = "", attachments=None) -> dict:
	today = today or frappe.utils.today()
	review = _date(row.get("review_due_date"))
	overdue = False
	days = None
	if review:
		try:
			days = int(frappe.utils.date_diff(review, today))
			overdue = days < 0
		except Exception:  # pragma: no cover - an unparseable stored date
			overdue = False
	return {
		"name": row.get("name"),
		"policy_name": row.get("policy_name"),
		"category": row.get("category") or None,
		"version": row.get("version") or None,
		"company": row.get("company") or None,
		"policy_owner": row.get("policy_owner") or None,
		"status": row.get("status") or "Active",
		# v0.245.0. "Approved" (through review) is in force, as "Active" (before review existed) is.
		"in_force": (row.get("status") or "Active") in ("Active", "Approved"),
		"effective_date": _date(row.get("effective_date")),
		"review_due_date": review,
		"days_until_review": days,
		"review_overdue": bool(overdue and (row.get("status") or "Active") in ("Active", "Approved")),
		"covers_task_types": [t for t in str(row.get("covers_task_types") or "").splitlines() if t.strip()],
		"covers_positions": [t for t in str(row.get("covers_positions") or "").splitlines() if t.strip()],
		"supersedes": row.get("supersedes") or None,
		"superseded_by": row.get("superseded_by") or None,
		"attached_document": row.get("attached_document") or None,
		"attachments": list(attachments or []),
		"has_document": bool(row.get("attached_document")) or bool(attachments),
		"document_source": (
			"attached_document"
			if row.get("attached_document")
			else ("file attachment" if attachments else None)
		),
		"notes": row.get("notes") or None,
	}


def _policy_notes(policy: dict) -> list:
	out = []
	if not policy["has_document"]:
		out.append(
			"No document is attached. This record asserts that a procedure exists, which is "
			"not the same as a procedure existing — an auditor asks to read it."
		)
	if not policy["effective_date"]:
		out.append(
			"No effective date. An audit asks which procedure was in force on the day "
			"something happened, and without this that question has no answer."
		)
	if not policy["review_due_date"] and policy["in_force"]:
		out.append(
			"No review date, so nothing will ever say this is stale. GAP and GlobalGAP both "
			"expect an annual review."
		)
	if policy["review_overdue"]:
		out.append(f"Overdue for review since {policy['review_due_date']}, and still in force.")
	if policy["status"] == "Draft":
		out.append(
			"Status is Draft: written but not adopted. It will not be produced in an audit "
			"packet, because presenting a draft as a procedure in force would be misleading."
		)
	return out


def list_compliance_policies(args: dict) -> ToolResult:
	"""The SOP library: what is written down, at what version, and what is stale."""
	_require(POLICY)
	company = _entity(args)
	today = frappe.utils.today()

	filters = {}
	if company:
		filters["company"] = company
	for key in ("category", "status"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(POLICY, key, value, key)

	rows = frappe.db.get_all(
		POLICY,
		filters=filters,
		fields=compat.existing_fields(POLICY, _POLICY_FIELDS),
		order_by="category asc, policy_name asc",
		limit=min(as_limit(args), REGISTER_CAP),
	)
	attachments = _policy_attachments(dict(row).get("name") for row in rows)
	policies = [
		_describe_policy(dict(row), today, attachments.get(str(dict(row).get("name")), [])) for row in rows
	]
	if as_bool(args, "in_force_only", False):
		policies = [policy for policy in policies if policy["in_force"]]

	by_category: dict = {}
	for policy in policies:
		key = policy["category"] or "(uncategorised)"
		by_category[key] = by_category.get(key, 0) + 1

	overdue = [policy["name"] for policy in policies if policy["review_overdue"]]
	undocumented = [policy["name"] for policy in policies if not policy["has_document"]]

	return ToolResult(
		data={
			"company": company,
			"policy_count": len(policies),
			"in_force": len([policy for policy in policies if policy["in_force"]]),
			"by_category": dict(sorted(by_category.items())),
			"review_overdue": overdue,
			"without_a_document": undocumented,
			"policies": policies,
			"note": (
				"`without_a_document` is the list worth acting on first. A policy record with no "
				"attached procedure is a claim, and an auditor asks to read the procedure."
			),
		},
		summary=(
			f"{len(policies)} policy/policies, {len(overdue)} overdue for review, "
			f"{len(undocumented)} with no document attached"
		),
	)


def get_policy_coverage(args: dict) -> ToolResult:
	"""Which SOP categories each audit regime expects, and which this farm has none of.

	THE QUESTION THIS ANSWERS IS "WHAT DO I STILL HAVE TO WRITE". Every other
	read over this register answers a question about the policies that EXIST —
	`list_compliance_policies` counts them, groups them and flags the stale ones.
	None of them could say anything about the ones that do not, and an empty
	policy section in a GAP packet is a statement about absent records that no
	register view can make, because the absent records are not in it.

	The expectation is not invented here. Each `AuditPacketType` already declares
	the `policy_categories` its packet pulls — that declaration is what scopes the
	section, so a DOL packet carries no GlobalGAP procedure — and this reads the
	same declaration in the other direction. A category a regime pulls and this
	company has no in-force policy for is a section that will be short in the
	packet and a question at the audit.

	A REGIME THAT NAMES NO CATEGORIES IS REPORTED AS SUCH, never as satisfied.
	An empty `policy_categories` means the packet includes every policy of every
	kind, which makes it impossible for this function to say what is missing — and
	that is a gap in what this app encodes about the regime, not evidence about the
	farm. Answering "nothing missing" for GLOBALG.A.P. because nobody wrote its
	category list down would be the most flattering possible lie.

	COVERAGE IS ACTIVE-AND-EFFECTIVE, NOT MERELY PRESENT. A Draft was never
	adopted and a policy effective next month was not in force today, so neither
	covers anything. And a covered category whose policy has no document attached
	is reported separately rather than counted as a gap: the procedure exists on
	the record, which is a different problem from it not existing at all, and it
	needs a different fix.
	"""
	_require(POLICY)
	company = _entity(args)
	as_of = as_date(args, "as_of") or frappe.utils.today()

	wanted = as_str(args, "audit_type")
	if wanted:
		spec = audit_packets.get(wanted)
		if spec is None:
			raise ToolError(
				f"no audit packet type {wanted!r}. This app knows: "
				f"{', '.join(audit_packets.names())}. Nothing was read."
			)
		specs = [spec]
	else:
		specs = [audit_packets.TYPES[key] for key in audit_packets.names()]
	specs = [spec for spec in specs if "policies" in spec.sections]

	rows = (
		frappe.db.get_all(
			POLICY,
			filters={"company": company} if company else {},
			fields=compat.existing_fields(POLICY, _POLICY_FIELDS),
			order_by="category asc, policy_name asc",
			limit=REGISTER_CAP,
		)
		or []
	)
	attachments = _policy_attachments(dict(row).get("name") for row in rows)

	#: `{category: [policy, ...]}` for the policies actually in force on the day.
	in_force: dict = {}
	undocumented: dict = {}
	for raw in rows:
		row = dict(raw)
		policy = _describe_policy(row, as_of, attachments.get(str(row.get("name")), []))
		if not policy["in_force"]:
			continue
		if policy["effective_date"] and str(policy["effective_date"]) > str(as_of):
			continue
		category = policy["category"] or "(uncategorised)"
		in_force.setdefault(category, []).append(policy["name"])
		if not policy["has_document"]:
			undocumented.setdefault(category, []).append(policy["name"])

	regimes = []
	unscoped = []
	every_gap: set = set()
	for spec in specs:
		expected = list(spec.policy_categories)
		if not expected:
			unscoped.append(spec.key)
			regimes.append(
				{
					"audit_type": spec.key,
					"title": spec.title,
					"expected_categories": [],
					"covered": [],
					"missing": [],
					"covered_without_a_document": [],
					"coverage_percent": None,
					"note": (
						f"The {spec.key} packet pulls EVERY policy category rather than a named "
						"list, so no gap can be computed for it. That is a gap in what this app "
						"encodes about the regime and not a statement about this farm — read "
						"another regime's row for the categories that are genuinely missing."
					),
				}
			)
			continue
		covered = [category for category in expected if in_force.get(category)]
		missing = [category for category in expected if not in_force.get(category)]
		every_gap.update(missing)
		regimes.append(
			{
				"audit_type": spec.key,
				"title": spec.title,
				"expected_categories": expected,
				"covered": covered,
				"missing": missing,
				"covered_without_a_document": sorted(
					category for category in covered if undocumented.get(category)
				),
				"coverage_percent": round(100.0 * len(covered) / len(expected), 1),
				"ready": not missing,
			}
		)

	work_list = [
		{
			"category": category,
			"needed_by": sorted(spec.key for spec in specs if category in (spec.policy_categories or ())),
			"register_it_with": (
				"create_compliance_policy(policy_name=…, category="
				f"{category!r}, company=…, version=…, effective_date=…, review_due_date=…, "
				"file_content=<base64 of the SOP>, file_name=…)"
			),
		}
		for category in sorted(every_gap)
	]

	notes = []
	if work_list:
		notes.append(
			f"{len(work_list)} SOP category/categories have no policy in force on {as_of}. "
			"Each one is a section an audit packet will produce short, and the packet says so "
			"— which is better than silence and is not the same as having the procedure."
		)
	undocumented_policies = sorted({name for names in undocumented.values() for name in names})
	if undocumented:
		notes.append(
			f"{len(undocumented_policies)} in-force policy/policies have no document attached. The "
			"record asserts a procedure exists; an auditor asks to read it. Attach it with "
			"update_compliance_policy(policy=…, file_content=…, file_name=…)."
		)
	if unscoped:
		notes.append(
			f"{', '.join(unscoped)} name no policy categories in this app, so nothing was "
			"computed for them. Their rows say so rather than reporting full coverage."
		)

	scoped = [row for row in regimes if row["expected_categories"]]
	return ToolResult(
		data={
			"company": company,
			"as_of": as_of,
			"regimes": regimes,
			"categories_in_force": dict(sorted(in_force.items())),
			"categories_with_no_policy": sorted(every_gap),
			"in_force_without_a_document": dict(sorted(undocumented.items())),
			"policies_without_a_document": undocumented_policies,
			"work_list": work_list,
			"notes": notes,
		},
		summary=(
			f"{len(scoped)} regime(s) scored, {len(every_gap)} SOP category/categories missing, "
			f"{len(undocumented_policies)} in-force policy/policies with no document"
		),
	)


def get_compliance_policy(args: dict) -> ToolResult:
	"""One procedure in full, with its version chain and every audit that cited it."""
	_require(POLICY)
	company = _entity(args)
	row = _row(
		POLICY,
		as_str(args, "policy", required=True),
		_POLICY_FIELDS,
		"policy",
		"list_compliance_policies",
		company or "",
	)
	described = _describe_policy(row, "", _policy_attachments([row["name"]]).get(str(row["name"]), []))

	chain = _policy_chain(row["name"])
	citing = _audits_citing(row["name"])

	return ToolResult(
		data={
			**described,
			"version_chain": chain,
			"chain_length": len(chain),
			"cited_by_audits": citing,
			"compliance_notes": _policy_notes(described),
		},
		summary=(
			f"{row['name']}"
			+ (f" ({described['version']})" if described["version"] else "")
			+ f": {described['status']}"
			+ (", review overdue" if described["review_overdue"] else "")
		),
	)


def _policy_chain(name: str) -> list:
	"""Every version of this policy, oldest first.

	Walks back through `supersedes` and forward through `superseded_by`, refusing
	to visit a name twice. A chain somebody wrote by hand can be a cycle, and a
	walk with no memory would hang on it.
	"""
	seen = {name}
	backwards = []
	cursor = name
	while len(backwards) < 50:
		previous = frappe.db.get_value(POLICY, cursor, "supersedes")
		if not previous or previous in seen:
			break
		seen.add(previous)
		backwards.append(previous)
		cursor = previous

	forwards = []
	cursor = name
	while len(forwards) < 50:
		later = frappe.db.get_value(POLICY, cursor, "superseded_by")
		if not later or later in seen:
			break
		seen.add(later)
		forwards.append(later)
		cursor = later

	ordered = [*reversed(backwards), name, *forwards]
	out = []
	for entry in ordered:
		row = frappe.db.get_value(POLICY, entry, compat.existing_fields(POLICY, _POLICY_FIELDS), as_dict=True)
		if not row:
			continue
		described = _describe_policy(dict(row))
		out.append(
			{
				"name": described["name"],
				"version": described["version"],
				"status": described["status"],
				"effective_date": described["effective_date"],
				"is_this_one": entry == name,
			}
		)
	return out


def _audits_citing(policy: str) -> list:
	"""Audit Events whose corrective actions name this policy as evidence."""
	if not compat.doctype_exists(AUDIT):
		return []
	try:
		rows = frappe.db.get_all(
			"Audit Corrective Action",
			filters={"evidence": policy},
			fields=["parent", "finding", "status", "closed_date"],
			limit=REGISTER_CAP,
		)
	except Exception:  # pragma: no cover - a site whose child table is not queryable
		return []
	return [
		{
			"audit_event": row.get("parent"),
			"finding": row.get("finding"),
			"status": row.get("status"),
			"closed_date": _date(row.get("closed_date")),
		}
		for row in rows or []
	]


def _refuse_review_statuses(status: str) -> str:
	"""v0.245.0. In Review is reached through stage_config (kind sop); Approved only by a person."""
	if str(status).strip() in ("In Review", "Approved"):
		raise ToolError(
			f"status {status!r} is not set directly: submit an SOP for review with stage_config kind 'sop'; "
			"approval is a person's act in the Desk or on the phone (there is no MCP approve). Nothing was changed."
		)
	return status


def _newline_list(value) -> str:
	if isinstance(value, (list, tuple)):
		items = value
	else:
		items = str(value or "").replace(",", "\n").splitlines()
	return "\n".join(str(i).strip() for i in items if str(i).strip())


def create_compliance_policy(args: dict) -> ToolResult:
	"""Register one written procedure at one version."""
	_require(POLICY)
	company = _entity(args)
	policy_name = as_str(args, "policy_name", required=True)

	if frappe.db.exists(POLICY, policy_name):
		raise ToolError(
			f"there is already a Compliance Policy called {policy_name!r}. The version is a "
			"FIELD, not part of the name: a policy at v3 is the same policy it was at v1, and "
			"every audit that cited it should still resolve to it. To record a new version, "
			"either update this record's version, or create the new one under a genuinely "
			"different name and link them with supersede_compliance_policy. Nothing was created."
		)

	doc = frappe.new_doc(POLICY)
	doc.policy_name = policy_name
	doc.company = company
	doc.version = as_str(args, "version")
	doc.policy_owner = _resolve_user(as_str(args, "policy_owner"))
	doc.effective_date = as_date(args, "effective_date")
	doc.review_due_date = as_date(args, "review_due_date")
	doc.attached_document = as_str(args, "attached_document")
	doc.notes = as_str(args, "notes")

	category = as_str(args, "category", required=True)
	doc.category = as_choice(POLICY, "category", category, "category")
	status = as_str(args, "status")
	if status:
		doc.status = as_choice(POLICY, "status", _refuse_review_statuses(status), "status")
	# v0.245.0. What the SOP covers decides who approves it (decision 40).
	for key in ("covers_task_types", "covers_positions"):
		if key in args and compat.has_field(POLICY, key):
			doc.set(key, _newline_list(args.get(key)))
	# v0.245.0. A DRAFT NEW VERSION names the version it will replace; the old one stays in force until
	# the new one is approved, and approval writes the other end of the chain (`sop.approve`). For an
	# immediate replacement without review, supersede_compliance_policy is unchanged.
	previous = as_str(args, "supersedes")
	if previous:
		if not frappe.db.exists(POLICY, previous):
			raise ToolError(f"supersedes: no Compliance Policy {previous!r}. Nothing was created.")
		if (doc.status or "Draft") != "Draft":
			raise ToolError("supersedes is set on a Draft new version only. Nothing was created.")
		doc.supersedes = previous

	doc.insert(ignore_permissions=True)
	attached = _attach_the_procedure(args, doc, tail="The policy WAS created.")
	described = _describe_policy(dict(doc.as_dict()), "", attached)

	return ToolResult(
		data={**described, "warnings": _policy_notes(described)},
		summary=(
			f"registered compliance policy {doc.name} ({described['category']}"
			+ (f", {described['version']}" if described["version"] else "")
			+ (", document attached" if attached else "")
			+ ")"
		),
		docstatus_delta="none → 0 (created)",
	)


def _attach_the_procedure(args: dict, doc, tail: str) -> list:
	"""Put the SOP itself on the policy record, in the same call that made it.

	WHY THIS IS NOT LEFT TO `attach_file_to_document`. It could be done in two
	calls and for a long time that was the only way — which is why the register
	filled up with policy records asserting procedures nobody had uploaded.
	`_policy_notes` has always said what that means ("This record asserts that a
	procedure exists, which is not the same as a procedure existing"), and saying
	it is not the same as making the right thing easy. Registering an SOP is ONE
	act, so it is one call.

	THE ATTACH FIELD IS WRITTEN TOO, not just the File link. `attached_document`
	is what the audit packet reads first and what the Desk form shows; a File
	linked to the policy with that field left empty is the exact disagreement
	`_policy_attachments` exists to paper over, and papering over it here as well
	would be choosing to keep producing it.

	IT VALIDATES BEFORE THE INSERT AND ATTACHES AFTER. `file_name` without
	`file_content` is refused up front by `create_compliance_policy`'s caller
	path; a decode failure here happens with the policy already on the record,
	which is why `tail` says so rather than claiming nothing was created.
	"""
	file_content = as_str(args, "file_content")
	if not file_content:
		return []
	file_name = as_str(args, "file_name")
	if not file_name:
		raise ToolError(
			"file_name is required with file_content — a procedure whose file has no name is "
			f"one nobody can identify later. {tail}"
		)
	content = files.decode_base64_content(file_content, tail=tail)
	attachment = files.insert_attachment(
		file_name,
		content,
		is_private=True,
		doctype=POLICY,
		name=doc.name,
		attached_to_field="attached_document",
	)
	url = attachment.get("file_url")
	if url:
		doc.db_set("attached_document", url)
		doc.attached_document = url
	return [{"file": attachment.name, "file_name": file_name, "file_url": url}]


def _resolve_user(user: str) -> str | None:
	"""A User docname, or a refusal naming what was tried.

	Not silently dropped. A policy owner who does not exist means nobody is
	answerable for the procedure, and a field that quietly emptied itself would
	look exactly like a field nobody filled in.
	"""
	user = (user or "").strip()
	if not user:
		return None
	if not compat.doctype_exists("User"):  # pragma: no cover - every Frappe site has User
		return user
	if frappe.db.exists("User", user):
		return user
	match = frappe.db.get_value("User", {"full_name": user}, "name")
	if match:
		return match
	raise ToolError(
		f"no User {user!r} on this site. The policy owner is the person an auditor's question "
		"gets forwarded to, so it has to be somebody who can receive it — pass their login "
		"email, or leave it blank. Nothing was changed."
	)


def update_compliance_policy(args: dict) -> ToolResult:
	"""Change a procedure. Cannot re-key it and cannot rewrite its chain."""
	_require(POLICY)
	company = _entity(args)
	row = _row(
		POLICY,
		as_str(args, "policy", required=True),
		_POLICY_FIELDS,
		"policy",
		"list_compliance_policies",
		company or "",
	)

	if as_str(args, "policy_name"):
		raise ToolError(
			"policy_name cannot be changed: it is the docname, and every audit finding that "
			"cited this procedure points at it. Nothing was changed."
		)
	for key in ("supersedes", "superseded_by"):
		if key in args:
			raise ToolError(
				f"{key} is not set here. A version chain written from one end only says something "
				"different to a reader coming from the other end, so both links are written in "
				"one act by supersede_compliance_policy. Nothing was changed."
			)

	doc = frappe.get_doc(POLICY, row["name"])
	changes: dict = {}
	for key in ("version", "attached_document", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	for key in ("effective_date", "review_due_date"):
		if key in args:
			_stage(changes, doc, key, as_date(args, key) or "")
	for key in ("category", "status"):
		if key in args:
			value = as_str(args, key)
			if key == "status" and value:
				_refuse_review_statuses(value)
			_stage(changes, doc, key, as_choice(POLICY, key, value, key) if value else "")
	for key in ("covers_task_types", "covers_positions"):
		if key in args and compat.has_field(POLICY, key):
			_stage(changes, doc, key, _newline_list(args.get(key)))
	if "policy_owner" in args:
		_stage(changes, doc, "policy_owner", _resolve_user(as_str(args, "policy_owner")) or "")
	if "company" in args:
		_stage(changes, doc, "company", company or "")

	if not changes and not as_str(args, "file_content"):
		raise ToolError(
			"nothing to change. Pass at least one of: category, version, status, "
			"effective_date, review_due_date, policy_owner, company, attached_document, notes "
			"— or file_content with file_name to attach the procedure itself."
		)

	doc.save(ignore_permissions=True)
	attached = _attach_the_procedure(args, doc, tail="The other changes WERE saved.")
	if attached:
		changes["attached_document"] = [row["attached_document"], doc.attached_document]
	described = _describe_policy(
		dict(doc.as_dict()),
		"",
		attached or _policy_attachments([doc.name]).get(str(doc.name), []),
	)
	return ToolResult(
		data={
			**described,
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
			"compliance_notes": _policy_notes(described),
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


def supersede_compliance_policy(args: dict) -> ToolResult:
	"""Replace one procedure with another, writing BOTH ends of the chain.

	Both ends in one act, because a chain written from one side only tells a
	reader coming from the other side something different — and "which procedure
	was in force on the day this happened" is exactly the question an audit asks,
	from whichever end the auditor happens to start.
	"""
	_require(POLICY)
	company = _entity(args)
	old = _row(
		POLICY,
		as_str(args, "policy", required=True),
		_POLICY_FIELDS,
		"policy",
		"list_compliance_policies",
		company or "",
	)
	new = _row(
		POLICY,
		as_str(args, "superseded_by", required=True),
		_POLICY_FIELDS,
		"superseded_by",
		"list_compliance_policies",
		company or "",
	)
	reason = _reason(args)

	if old["name"] == new["name"]:
		raise ToolError(f"{old['name']} cannot supersede itself. Nothing was changed.")
	if old.get("superseded_by"):
		raise ToolError(
			f"{old['name']} was already superseded by {old['superseded_by']}. A procedure has "
			"one successor; two would make 'what was in force' unanswerable. If the chain is "
			"wrong, correct it in the Desk where both ends are visible. Nothing was changed."
		)
	if new.get("supersedes") and new["supersedes"] != old["name"]:
		raise ToolError(f"{new['name']} already supersedes {new['supersedes']}. Nothing was changed.")

	old_effective = _date(old.get("effective_date"))
	new_effective = _date(new.get("effective_date"))
	if old_effective and new_effective and new_effective < old_effective:
		raise ToolError(
			f"{new['name']} takes effect on {new_effective}, before {old['name']} took effect "
			f"on {old_effective}. A successor that predates its predecessor leaves a period "
			"with two procedures in force and no way to tell which. Nothing was changed."
		)

	if as_bool(args, "dry_run", False):
		return ToolResult(
			data={
				"policy": old["name"],
				"superseded_by": new["name"],
				"dry_run": True,
				"changed": False,
				"would_write": {
					old["name"]: {"superseded_by": new["name"], "status": "Superseded"},
					new["name"]: {"supersedes": old["name"]},
				},
			},
			summary=f"dry run: would supersede {old['name']} with {new['name']}",
		)

	old_doc = frappe.get_doc(POLICY, old["name"])
	old_doc.superseded_by = new["name"]
	old_status = old_doc.status
	old_doc.status = "Superseded"
	old_doc.save(ignore_permissions=True)

	new_doc = frappe.get_doc(POLICY, new["name"])
	new_doc.supersedes = old["name"]
	new_doc.save(ignore_permissions=True)

	_comment(
		POLICY,
		old["name"],
		f"Superseded by {new['name']}. {reason}",
	)
	_comment(
		POLICY,
		new["name"],
		f"Supersedes {old['name']}. {reason}",
	)

	return ToolResult(
		data={
			"policy": old["name"],
			"superseded_by": new["name"],
			"changed": True,
			"old_status": old_status,
			"new_status": "Superseded",
			"reason": reason,
			"note": (
				f"{old['name']} is now historical and will not be produced as a procedure in "
				"force. It is NOT deleted and NOT wrong: it was correct on the dates it was in "
				f"force, and an audit of those dates wants it. {new['name']} carries the "
				"forward link, so the chain reads the same from either end."
			),
		},
		summary=f"{old['name']} superseded by {new['name']} — {reason}",
		docstatus_delta="0 → 0 (updated)",
	)


def _comment(doctype: str, name: str, text: str) -> bool:
	"""Leave a Comment on a document's timeline. Never raises.

	A record of why something changed belongs on the document, where the next
	person to open it will see it without knowing to go and look somewhere else.
	Failing to write one is not a reason to fail the change that has already been
	made.
	"""
	try:
		comment = frappe.new_doc("Comment")
		comment.comment_type = "Comment"
		comment.reference_doctype = doctype
		comment.reference_name = name
		comment.content = text
		comment.insert(ignore_permissions=True)
		return True
	except Exception:
		return False


# ── Certification ───────────────────────────────────────────────────────────
def _describe_cert(row: dict, today: str = "") -> dict:
	today = today or frappe.utils.today()
	expires = _date(row.get("expiration_date"))
	remaining = None
	if expires:
		try:
			remaining = int(frappe.utils.date_diff(expires, today))
		except Exception:  # pragma: no cover - an unparseable stored date
			remaining = None
	window = int(row.get("renewal_window_days") or 0) or 90
	holder_doctype, holder_name = _resolve_holder(row.get("holder"))
	return {
		"name": row.get("name"),
		"cert_name": row.get("cert_name"),
		"cert_type": row.get("cert_type") or None,
		"status": row.get("status") or "Active",
		"company": row.get("company") or None,
		"holder": row.get("holder") or None,
		# Which register answered, so a caller that wants to follow the pointer can.
		# None is not a failure: a contractor on nobody's payroll is exactly the case.
		"holder_doctype": holder_doctype,
		"holder_name": holder_name,
		"issuing_body": row.get("issuing_body") or None,
		"issued_date": _date(row.get("issued_date")),
		"expiration_date": expires,
		"days_until_expiry": remaining,
		"expired": bool(remaining is not None and remaining < 0),
		"renewal_window_days": window,
		"inside_renewal_window": bool(remaining is not None and remaining <= window),
		"certificate_number": row.get("certificate_number") or None,
		"attached_certificate": row.get("attached_certificate") or None,
		"has_certificate": bool(row.get("attached_certificate")),
		"notes": row.get("notes") or None,
	}


def _cert_notes(cert: dict) -> list:
	out = []
	if not cert["expiration_date"]:
		out.append(
			"No expiration date. It is the single most load-bearing column on this record — "
			"nothing can say whether this certificate is currently a defence."
		)
	elif cert["expired"]:
		out.append(
			f"EXPIRED on {cert['expiration_date']}, {abs(cert['days_until_expiry'])} day(s) ago. "
			"It is not a defence and has not been one since that date."
		)
	elif cert["inside_renewal_window"]:
		out.append(
			f"Expires in {cert['days_until_expiry']} day(s), inside the {cert['renewal_window_days']}-day "
			"renewal window. That window is the issuing body's real lead time, not a reminder "
			"preference."
		)
	if not cert["has_certificate"]:
		out.append(
			"No certificate attached. An auditor asks to see the certificate, not a record saying one exists."
		)
	if cert["status"] == "Active" and cert["expired"]:
		out.append(
			"Status still reads Active over an expired date. Nothing rewrites the status when a "
			"date passes — a derived field that is only correct when somebody saves the document "
			"would leave the lapsed certificates reading current, which is the wrong way round."
		)
	return out


def list_certifications(args: dict) -> ToolResult:
	"""The certificate and licence register, soonest expiry first."""
	_require(CERTIFICATION)
	company = _entity(args)
	today = frappe.utils.today()

	filters = {}
	if company:
		filters["company"] = company
	for key in ("cert_type", "status"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(CERTIFICATION, key, value, key)
	holder = as_str(args, "holder")
	if holder:
		filters["holder"] = holder

	rows = frappe.db.get_all(
		CERTIFICATION,
		filters=filters,
		fields=compat.existing_fields(CERTIFICATION, _CERT_FIELDS),
		order_by="expiration_date asc",
		limit=min(as_limit(args), REGISTER_CAP),
	)
	certs = [_describe_cert(dict(row), today) for row in rows]
	if as_bool(args, "expiring_only", False):
		certs = [cert for cert in certs if cert["inside_renewal_window"]]

	expired = [cert["name"] for cert in certs if cert["expired"]]
	expiring = [cert["name"] for cert in certs if cert["inside_renewal_window"] and not cert["expired"]]

	by_type: dict = {}
	for cert in certs:
		key = cert["cert_type"] or "(untyped)"
		by_type[key] = by_type.get(key, 0) + 1

	return ToolResult(
		data={
			"company": company,
			"certification_count": len(certs),
			"expired": expired,
			"inside_renewal_window": expiring,
			"without_a_certificate": [cert["name"] for cert in certs if not cert["has_certificate"]],
			"by_type": dict(sorted(by_type.items())),
			"certifications": certs,
			"note": (
				"Sorted soonest-expiry first, which is the order somebody works them in. "
				"`expired` is read from the DATE, not from the status field — nothing rewrites "
				"a status when a date passes, so a lapsed certificate keeps saying Active."
			),
		},
		summary=(
			f"{len(certs)} certificate(s), {len(expired)} expired, {len(expiring)} inside their "
			"renewal window"
		),
	)


def get_certification(args: dict) -> ToolResult:
	"""One certificate with its full renewal history, lapses included."""
	_require(CERTIFICATION)
	company = _entity(args)
	row = _row(
		CERTIFICATION,
		as_str(args, "certification", required=True),
		_CERT_FIELDS,
		"certification",
		"list_certifications",
		company or "",
	)
	described = _describe_cert(row)

	doc = frappe.get_doc(CERTIFICATION, row["name"])
	renewals = []
	lapses = []
	for entry in doc.get("renewals") or []:
		renewed_on = _date(entry.get("renewed_on"))
		previous = _date(entry.get("previous_expiration"))
		lapsed_days = None
		if renewed_on and previous and renewed_on > previous:
			try:
				lapsed_days = int(frappe.utils.date_diff(renewed_on, previous))
			except Exception:  # pragma: no cover
				lapsed_days = None
		renewals.append(
			{
				"renewed_on": renewed_on,
				"previous_expiration": previous,
				"new_expiration": _date(entry.get("new_expiration")),
				"renewed_by": entry.get("renewed_by") or None,
				"certificate_number": entry.get("certificate_number") or None,
				"what_was_done": entry.get("reason") or None,
				"lapsed_days": lapsed_days,
			}
		)
		if lapsed_days:
			lapses.append({"from": previous, "to": renewed_on, "days": lapsed_days})

	return ToolResult(
		data={
			**described,
			"renewal_count": len(renewals),
			"renewals": renewals,
			"lapses": lapses,
			"compliance_notes": _cert_notes(described)
			+ (
				[
					f"This certificate has lapsed {len(lapses)} time(s) — a total of "
					f"{sum(entry['days'] for entry in lapses)} day(s) during which it was not a "
					"defence. Renewing late does not close a gap that already happened, and the "
					"history keeps it visible on purpose."
				]
				if lapses
				else []
			),
		},
		summary=(
			f"{row['name']}: {described['cert_type'] or 'untyped'}, "
			+ (
				f"EXPIRED {described['expiration_date']}"
				if described["expired"]
				else f"expires {described['expiration_date'] or 'unknown'}"
			)
			+ f", {len(renewals)} renewal(s)"
		),
	)


def create_certification(args: dict) -> ToolResult:
	"""Register one certificate or licence."""
	_require(CERTIFICATION)
	company = _entity(args)
	cert_name = as_str(args, "cert_name", required=True)

	if frappe.db.exists(CERTIFICATION, cert_name):
		raise ToolError(
			f"there is already a Certification called {cert_name!r}. A renewal is not a new "
			"record — use renew_certification, which moves the expiration out and keeps the "
			"history of every previous term. Nothing was created."
		)

	doc = frappe.new_doc(CERTIFICATION)
	doc.cert_name = cert_name
	doc.company = company
	doc.holder = as_str(args, "holder")
	doc.issuing_body = as_str(args, "issuing_body")
	doc.issued_date = as_date(args, "issued_date")
	doc.expiration_date = as_date(args, "expiration_date")
	doc.certificate_number = as_str(args, "certificate_number")
	doc.attached_certificate = as_str(args, "attached_certificate")
	doc.notes = as_str(args, "notes")

	window = as_int(args, "renewal_window_days")
	if window is not None:
		doc.renewal_window_days = window

	doc.cert_type = as_choice(
		CERTIFICATION, "cert_type", as_str(args, "cert_type", required=True), "cert_type"
	)
	status = as_str(args, "status")
	if status:
		doc.status = as_choice(CERTIFICATION, "status", status, "status")

	doc.insert(ignore_permissions=True)
	described = _describe_cert(dict(doc.as_dict()))

	return ToolResult(
		data={**described, "warnings": _cert_notes(described)},
		summary=(
			f"registered certification {doc.name} ({described['cert_type']}, expires "
			f"{described['expiration_date'] or 'unknown'})"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_certification(args: dict) -> ToolResult:
	"""Change a certificate's details. Extending the expiry is refused — that is a renewal."""
	_require(CERTIFICATION)
	company = _entity(args)
	row = _row(
		CERTIFICATION,
		as_str(args, "certification", required=True),
		_CERT_FIELDS,
		"certification",
		"list_certifications",
		company or "",
	)

	if as_str(args, "cert_name"):
		raise ToolError("cert_name cannot be changed: it is the docname. Nothing was changed.")

	wanted_expiry = as_date(args, "expiration_date") if "expiration_date" in args else None
	current_expiry = _date(row.get("expiration_date"))
	if wanted_expiry and current_expiry and wanted_expiry > current_expiry:
		raise ToolError(
			f"moving the expiration date forward, from {current_expiry} to {wanted_expiry}, is a "
			"RENEWAL — use renew_certification. It records what was actually done to earn the "
			"new term, and it keeps any period the certificate was allowed to lapse visible. "
			"Editing the date here would produce a certificate that looks as though it never "
			"expired. Nothing was changed."
		)

	doc = frappe.get_doc(CERTIFICATION, row["name"])
	changes: dict = {}
	for key in ("holder", "issuing_body", "certificate_number", "attached_certificate", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	for key in ("issued_date", "expiration_date"):
		if key in args:
			_stage(changes, doc, key, as_date(args, key) or "")
	for key in ("cert_type", "status"):
		if key in args:
			value = as_str(args, key)
			_stage(changes, doc, key, as_choice(CERTIFICATION, key, value, key) if value else "")
	if "renewal_window_days" in args:
		_stage(changes, doc, "renewal_window_days", as_int(args, "renewal_window_days") or 0)
	if "company" in args:
		_stage(changes, doc, "company", company or "")

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: cert_type, status, holder, issuing_body, "
			"issued_date, expiration_date, renewal_window_days, certificate_number, company, "
			"attached_certificate, notes."
		)

	doc.save(ignore_permissions=True)
	described = _describe_cert(dict(doc.as_dict()))
	return ToolResult(
		data={
			**described,
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
			"compliance_notes": _cert_notes(described),
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


def renew_certification(args: dict) -> ToolResult:
	"""Move the expiration out, and record what was actually done to earn it.

	A RENEWAL IS AN EVENT, NOT A FIELD EDIT. Editing the expiration date in place
	would produce a certificate that looks as though it never expired, which is
	exactly the fact somebody would want hidden and exactly the fact an auditor
	asks about. So the previous term is kept on the row, and a renewal recorded
	after the previous expiry is reported as the lapse it was.
	"""
	_require(CERTIFICATION)
	company = _entity(args)
	row = _row(
		CERTIFICATION,
		as_str(args, "certification", required=True),
		_CERT_FIELDS,
		"certification",
		"list_certifications",
		company or "",
	)
	new_expiration = as_date(args, "new_expiration", required=True)
	what_was_done = _reason(args, "what_was_done")
	renewed_on = as_date(args, "renewed_on") or frappe.utils.today()

	previous = _date(row.get("expiration_date"))
	if previous and new_expiration <= previous:
		raise ToolError(
			f"the new expiration {new_expiration} is not after the current one {previous}. A "
			"renewal moves the date OUT; a date that moves it back or leaves it alone is a "
			"correction, and recording it as a renewal would put a term in the history that "
			"never existed. Use update_certification. Nothing was changed."
		)
	if renewed_on > frappe.utils.today():
		raise ToolError(
			f"renewed_on {renewed_on} is in the future. A renewal is recorded when it happens. "
			"Nothing was changed."
		)

	lapsed_days = 0
	if previous and renewed_on > previous:
		try:
			lapsed_days = int(frappe.utils.date_diff(renewed_on, previous))
		except Exception:  # pragma: no cover
			lapsed_days = 0

	if as_bool(args, "dry_run", False):
		return ToolResult(
			data={
				"certification": row["name"],
				"dry_run": True,
				"changed": False,
				"previous_expiration": previous,
				"new_expiration": new_expiration,
				"renewed_on": renewed_on,
				"lapsed_days": lapsed_days,
			},
			summary=f"dry run: would renew {row['name']} to {new_expiration}",
		)

	doc = frappe.get_doc(CERTIFICATION, row["name"])
	doc.append(
		"renewals",
		{
			"renewed_on": renewed_on,
			"previous_expiration": previous,
			"new_expiration": new_expiration,
			"renewed_by": as_str(args, "renewed_by") or str(frappe.session.user),
			"certificate_number": as_str(args, "certificate_number"),
			"reason": what_was_done,
		},
	)
	doc.expiration_date = new_expiration
	number = as_str(args, "certificate_number")
	if number:
		doc.certificate_number = number
	attached = as_str(args, "attached_certificate")
	if attached:
		doc.attached_certificate = attached
	if str(doc.status or "") in ("Expired", "Not Yet Effective"):
		doc.status = "Active"
	doc.save(ignore_permissions=True)

	described = _describe_cert(dict(doc.as_dict()))
	warnings = _cert_notes(described)
	if lapsed_days:
		warnings.insert(
			0,
			f"This renewal was recorded {lapsed_days} day(s) AFTER the previous expiration of "
			f"{previous}. The certificate was not a defence during that gap, and renewing late "
			"does not close a gap that already happened — it is kept on the renewal row on "
			"purpose, because it is what an auditor asks about.",
		)

	return ToolResult(
		data={
			**described,
			"renewed_on": renewed_on,
			"previous_expiration": previous,
			"new_expiration": new_expiration,
			"lapsed_days": lapsed_days,
			"what_was_done": what_was_done,
			"warnings": warnings,
			"next_step": (
				"The certification_expiring alert on this record auto-dismisses on the next "
				"sweep, because the expiration has moved back outside the renewal window. Run "
				"refresh_compliance_alerts to see that happen now instead of tonight."
			),
		},
		summary=(
			f"renewed {row['name']} from {previous or 'no expiry'} to {new_expiration}"
			+ (f" — {lapsed_days} day lapse" if lapsed_days else "")
			+ f" — {what_was_done}"
		),
		docstatus_delta="0 → 0 (updated)",
	)


# ── Regulatory Filing ───────────────────────────────────────────────────────
def _describe_filing(row: dict, today: str = "") -> dict:
	today = today or frappe.utils.today()
	due = _date(row.get("response_due_date"))
	received = _date(row.get("response_received_date"))
	remaining = None
	if due and not received:
		try:
			remaining = int(frappe.utils.date_diff(due, today))
		except Exception:  # pragma: no cover
			remaining = None
	return {
		"name": row.get("name"),
		"filing_name": row.get("filing_name"),
		"agency": row.get("agency") or None,
		"filing_type": row.get("filing_type") or None,
		"company": row.get("company") or None,
		"period_covered": row.get("period_covered") or None,
		"status": row.get("status") or "Submitted",
		"submission_date": _date(row.get("submission_date")),
		"docket_number": row.get("docket_number") or None,
		"response_due_date": due,
		"response_received_date": received,
		"awaiting_response": bool(due and not received),
		"days_until_response_due": remaining,
		"response_overdue": bool(remaining is not None and remaining < 0),
		"response": row.get("response") or None,
		"attached_filing": row.get("attached_filing") or None,
		"attached_response": row.get("attached_response") or None,
		"has_filing_document": bool(row.get("attached_filing")),
		"notes": row.get("notes") or None,
	}


def _filing_notes(filing: dict) -> list:
	out = []
	if not filing["has_filing_document"] and filing["status"] != "Draft":
		out.append(
			"No filing document attached. The agency's position in a dispute is that they have "
			"no record, and 'we sent it' is worth nothing against that without the document."
		)
	if filing["status"] != "Draft" and not filing["docket_number"]:
		out.append(
			"No docket or confirmation number. It is what a follow-up call quotes, and it is "
			"the cheapest possible proof the filing arrived."
		)
	if filing["response_overdue"]:
		out.append(
			f"A response was due on {filing['response_due_date']}, "
			f"{abs(filing['days_until_response_due'])} day(s) ago, and none is recorded."
		)
	return out


def list_regulatory_filings(args: dict) -> ToolResult:
	"""What was filed, to whom, when, and what came back."""
	_require(FILING)
	company = _entity(args)
	today = frappe.utils.today()

	filters = {}
	if company:
		filters["company"] = company
	for key in ("agency", "status"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(FILING, key, value, key)
	filing_type = as_str(args, "filing_type")
	if filing_type:
		filters["filing_type"] = filing_type
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["submission_date"] = ("between", (from_date, to_date))
	elif from_date:
		filters["submission_date"] = (">=", from_date)
	elif to_date:
		filters["submission_date"] = ("<=", to_date)

	rows = frappe.db.get_all(
		FILING,
		filters=filters,
		fields=compat.existing_fields(FILING, _FILING_FIELDS),
		order_by="submission_date desc",
		limit=min(as_limit(args), REGISTER_CAP),
	)
	filings = [_describe_filing(dict(row), today) for row in rows]

	by_agency: dict = {}
	for filing in filings:
		key = filing["agency"] or "(unrecorded)"
		by_agency[key] = by_agency.get(key, 0) + 1

	return ToolResult(
		data={
			"company": company,
			"filing_count": len(filings),
			"by_agency": dict(sorted(by_agency.items())),
			"awaiting_response": [filing["name"] for filing in filings if filing["awaiting_response"]],
			"response_overdue": [filing["name"] for filing in filings if filing["response_overdue"]],
			"without_a_document": [
				filing["name"]
				for filing in filings
				if not filing["has_filing_document"] and filing["status"] != "Draft"
			],
			"filings": filings,
		},
		summary=(
			f"{len(filings)} filing(s), "
			f"{len([f for f in filings if f['awaiting_response']])} awaiting a response, "
			f"{len([f for f in filings if f['response_overdue']])} overdue"
		),
	)


def get_regulatory_filing(args: dict) -> ToolResult:
	"""One filing with its response and both attached documents."""
	_require(FILING)
	company = _entity(args)
	row = _row(
		FILING,
		as_str(args, "filing", required=True),
		_FILING_FIELDS,
		"filing",
		"list_regulatory_filings",
		company or "",
	)
	described = _describe_filing(row)
	return ToolResult(
		data={**described, "compliance_notes": _filing_notes(described)},
		summary=(
			f"{row['name']}: {described['agency'] or 'agency unrecorded'} "
			f"{described['filing_type'] or ''}, {described['status']}"
			+ (f", submitted {described['submission_date']}" if described["submission_date"] else "")
		).strip(),
	)


def create_regulatory_filing(args: dict) -> ToolResult:
	"""Record something submitted to an agency."""
	_require(FILING)
	company = _entity(args)
	filing_name = as_str(args, "filing_name", required=True)

	if frappe.db.exists(FILING, filing_name):
		raise ToolError(
			f"there is already a Regulatory Filing called {filing_name!r}. If this is an "
			"amended filing it is a separate record — name it so, and set its status to "
			"Amended. Nothing was created."
		)

	doc = frappe.new_doc(FILING)
	doc.filing_name = filing_name
	doc.company = company
	doc.filing_type = as_str(args, "filing_type", required=True)
	doc.period_covered = as_str(args, "period_covered")
	doc.submission_date = as_date(args, "submission_date")
	doc.docket_number = as_str(args, "docket_number")
	doc.response_due_date = as_date(args, "response_due_date")
	doc.response_received_date = as_date(args, "response_received_date")
	doc.response = as_str(args, "response")
	doc.attached_filing = as_str(args, "attached_filing")
	doc.attached_response = as_str(args, "attached_response")
	doc.notes = as_str(args, "notes")

	doc.agency = as_choice(FILING, "agency", as_str(args, "agency", required=True), "agency")
	status = as_str(args, "status")
	if status:
		doc.status = as_choice(FILING, "status", status, "status")

	doc.insert(ignore_permissions=True)
	described = _describe_filing(dict(doc.as_dict()))

	return ToolResult(
		data={**described, "warnings": _filing_notes(described)},
		summary=(
			f"recorded regulatory filing {doc.name} ({described['agency']} "
			f"{described['filing_type']}, {described['status']})"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_regulatory_filing(args: dict) -> ToolResult:
	"""Record the response, the docket number, the documents, the status."""
	_require(FILING)
	company = _entity(args)
	row = _row(
		FILING,
		as_str(args, "filing", required=True),
		_FILING_FIELDS,
		"filing",
		"list_regulatory_filings",
		company or "",
	)

	if as_str(args, "filing_name"):
		raise ToolError("filing_name cannot be changed: it is the docname. Nothing was changed.")

	doc = frappe.get_doc(FILING, row["name"])
	changes: dict = {}
	for key in (
		"filing_type",
		"period_covered",
		"docket_number",
		"response",
		"attached_filing",
		"attached_response",
		"notes",
	):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	for key in ("submission_date", "response_due_date", "response_received_date"):
		if key in args:
			_stage(changes, doc, key, as_date(args, key) or "")
	for key in ("agency", "status"):
		if key in args:
			value = as_str(args, key)
			_stage(changes, doc, key, as_choice(FILING, key, value, key) if value else "")
	if "company" in args:
		_stage(changes, doc, "company", company or "")

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: agency, filing_type, period_covered, "
			"status, submission_date, docket_number, response_due_date, response_received_date, "
			"response, company, attached_filing, attached_response, notes."
		)

	doc.save(ignore_permissions=True)
	described = _describe_filing(dict(doc.as_dict()))
	data = {
		**described,
		"changed": {key: [before, after] for key, (before, after) in changes.items()},
		"compliance_notes": _filing_notes(described),
	}
	if "response_received_date" in changes and described["response_received_date"]:
		data["next_step"] = (
			"The filing_response_due alert on this record auto-dismisses on the next sweep, "
			"because the thing being waited for has happened. Nobody has to switch it off."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ── Audit Event ─────────────────────────────────────────────────────────────
def _describe_action(index: int, row, today: str) -> dict:
	due = _date(row.get("due_date"))
	status = str(row.get("status") or "Open")
	overdue = False
	late = None
	if due and status in OPEN_STATUSES:
		try:
			late = int(frappe.utils.date_diff(today, due))
			overdue = late >= 0
		except Exception:  # pragma: no cover
			overdue = False
	return {
		"index": index,
		"finding": row.get("finding"),
		"severity": row.get("severity") or "Minor",
		"status": status,
		"open": status in OPEN_STATUSES,
		"assigned_to": row.get("assigned_to") or None,
		"due_date": due,
		"days_overdue": late if overdue else None,
		"overdue": overdue,
		"closed_date": _date(row.get("closed_date")),
		"corrective_action": row.get("corrective_action") or None,
		"evidence": row.get("evidence") or None,
		"notes": row.get("notes") or None,
	}


def _describe_audit(row: dict, actions: list, today: str = "") -> dict:
	today = today or frappe.utils.today()
	open_actions = [action for action in actions if action["open"]]
	overdue = [action for action in actions if action["overdue"]]
	return {
		"name": row.get("name"),
		"audit_name": row.get("audit_name"),
		"audit_type": row.get("audit_type") or None,
		"auditor": row.get("auditor") or None,
		"company": row.get("company") or None,
		"audit_date": _date(row.get("audit_date")),
		"result": row.get("result") or "Pending",
		"scope": row.get("scope") or None,
		"findings": row.get("findings") or None,
		"action_count": len(actions),
		"open_action_count": len(open_actions),
		"overdue_action_count": len(overdue),
		"worst_open_severity": _worst_severity(open_actions),
		"corrective_actions_closed": _date(row.get("corrective_actions_closed")),
		"closed": bool(row.get("corrective_actions_closed")),
		"closed_by": row.get("closed_by") or None,
		"closure_note": row.get("closure_note") or None,
		"attached_report": row.get("attached_report") or None,
		"has_report": bool(row.get("attached_report")),
		"notes": row.get("notes") or None,
	}


_SEVERITY_RANK = ("Critical", "Major", "Minor", "Observation")


def _worst_severity(actions: list) -> str | None:
	for severity in _SEVERITY_RANK:
		if any(action["severity"] == severity for action in actions):
			return severity
	return None


def _audit_actions(name: str, today: str) -> list:
	doc = frappe.get_doc(AUDIT, name)
	return [
		_describe_action(index, row, today)
		for index, row in enumerate(doc.get("corrective_actions_required") or [], start=1)
	]


def list_audit_events(args: dict) -> ToolResult:
	"""Every audit and inspection, with open corrective actions counted."""
	_require(AUDIT)
	company = _entity(args)
	today = frappe.utils.today()

	filters = {}
	if company:
		filters["company"] = company
	for key in ("audit_type", "result"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(AUDIT, key, value, key)
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["audit_date"] = ("between", (from_date, to_date))
	elif from_date:
		filters["audit_date"] = (">=", from_date)
	elif to_date:
		filters["audit_date"] = ("<=", to_date)
	if as_bool(args, "open_only", False):
		filters["corrective_actions_closed"] = ("is", "not set")

	rows = frappe.db.get_all(
		AUDIT,
		filters=filters,
		fields=compat.existing_fields(AUDIT, _AUDIT_FIELDS),
		order_by="audit_date desc",
		limit=min(as_limit(args), REGISTER_CAP),
	)
	audits = [_describe_audit(dict(row), _audit_actions(row["name"], today), today) for row in rows]

	by_type: dict = {}
	for audit in audits:
		key = audit["audit_type"] or "(untyped)"
		by_type[key] = by_type.get(key, 0) + 1

	open_actions = sum(audit["open_action_count"] for audit in audits)
	overdue_actions = sum(audit["overdue_action_count"] for audit in audits)

	return ToolResult(
		data={
			"company": company,
			"audit_count": len(audits),
			"by_type": dict(sorted(by_type.items())),
			"open_audits": [audit["name"] for audit in audits if not audit["closed"]],
			"open_corrective_actions": open_actions,
			"overdue_corrective_actions": overdue_actions,
			"with_overdue_actions": [audit["name"] for audit in audits if audit["overdue_action_count"]],
			"audits": audits,
			"note": (
				"An operation is not judged on having no findings — every audit produces some, "
				"and a clean report usually means the auditor did not look hard. It is judged on "
				"closing them, which is what `overdue_corrective_actions` counts."
			),
		},
		summary=(
			f"{len(audits)} audit(s), {open_actions} open corrective action(s), {overdue_actions} overdue"
		),
	)


def get_audit_event(args: dict) -> ToolResult:
	"""One audit in full: scope, findings, and every action with its deadline."""
	_require(AUDIT)
	company = _entity(args)
	row = _row(
		AUDIT,
		as_str(args, "audit", required=True),
		_AUDIT_FIELDS,
		"audit",
		"list_audit_events",
		company or "",
	)
	today = frappe.utils.today()
	actions = _audit_actions(row["name"], today)
	described = _describe_audit(row, actions, today)

	notes = []
	if not described["has_report"]:
		notes.append("No audit report attached. It is the auditor's own words, and nothing else is.")
	if described["overdue_action_count"]:
		notes.append(
			f"{described['overdue_action_count']} corrective action(s) are past their deadline. "
			"This is the first thing the same auditor asks about on the next visit, and it is "
			"what generate_audit_packet refuses to produce a packet over."
		)
	if described["open_action_count"] and described["closed"]:  # pragma: no cover - controller refuses
		notes.append("This audit is marked closed and has open actions. That should not be possible.")
	if not actions and described["result"] in ("Passed With Conditions", "Failed"):
		notes.append(
			f"The result is {described['result']} and no corrective actions are recorded. A "
			"conditional pass has conditions; a failure has reasons. Whatever they were, they "
			"are not written down here."
		)

	return ToolResult(
		data={
			**described,
			"corrective_actions": actions,
			"open_actions": [action for action in actions if action["open"]],
			"compliance_notes": notes,
		},
		summary=(
			f"{row['name']}: {described['audit_type'] or 'untyped'} "
			f"{described['audit_date'] or ''}, {described['result']}, "
			f"{described['open_action_count']} open action(s)"
		).replace("  ", " "),
	)


def _action_rows(args: dict, key: str = "corrective_actions") -> list:
	"""Validate an incoming corrective-actions list, refusing unknown keys by name."""
	raw = args.get(key)
	if raw is None:
		return []
	if not isinstance(raw, list):
		raise ToolError(f"{key} must be a list of objects, got {type(raw).__name__}.")
	out = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"{key}[{index}] must be an object, got {type(entry).__name__}.")
		unknown = sorted(set(entry) - set(_ACTION_FIELDS))
		if unknown:
			raise ToolError(
				f"{key}[{index}] has unsupported field(s): {', '.join(unknown)}. Supported: "
				f"{', '.join(_ACTION_FIELDS)}. Nothing was changed."
			)
		if not str(entry.get("finding") or "").strip():
			raise ToolError(
				f"{key}[{index}] has no finding. A corrective action with nothing to correct is "
				"a row nobody can act on. Nothing was changed."
			)
		row = {"finding": str(entry["finding"]).strip()}
		for field in ("severity", "status"):
			value = str(entry.get(field) or "").strip()
			if value:
				row[field] = as_choice("Audit Corrective Action", field, value, f"{key}[{index}].{field}")
		for field in ("assigned_to", "corrective_action", "evidence", "notes"):
			value = str(entry.get(field) or "").strip()
			if value:
				row[field] = value
		for field in ("due_date", "closed_date"):
			if entry.get(field):
				row[field] = as_date(entry, field)
		out.append(row)
	return out


def create_audit_event(args: dict) -> ToolResult:
	"""Record an audit with its findings and every corrective action it raised."""
	_require(AUDIT)
	company = _entity(args)
	audit_name = as_str(args, "audit_name", required=True)

	if frappe.db.exists(AUDIT, audit_name):
		raise ToolError(f"there is already an Audit Event called {audit_name!r}. Nothing was created.")

	doc = frappe.new_doc(AUDIT)
	doc.audit_name = audit_name
	doc.company = company
	doc.auditor = as_str(args, "auditor")
	doc.audit_date = as_date(args, "audit_date", required=True)
	doc.scope = as_str(args, "scope")
	doc.findings = as_str(args, "findings")
	doc.attached_report = as_str(args, "attached_report")
	doc.notes = as_str(args, "notes")

	doc.audit_type = as_choice(AUDIT, "audit_type", as_str(args, "audit_type", required=True), "audit_type")
	result = as_str(args, "result")
	if result:
		doc.result = as_choice(AUDIT, "result", result, "result")

	for row in _action_rows(args):
		doc.append("corrective_actions_required", row)

	doc.insert(ignore_permissions=True)
	today = frappe.utils.today()
	actions = _audit_actions(doc.name, today)
	described = _describe_audit(dict(doc.as_dict()), actions, today)

	warnings = []
	if not described["has_report"]:
		warnings.append("No audit report attached — it is the auditor's own words, and nothing else is.")
	if described["result"] in ("Passed With Conditions", "Failed") and not actions:
		warnings.append(
			f"Result {described['result']} with no corrective actions recorded. A conditional "
			"pass has conditions and a failure has reasons; neither is written down here, and "
			"nothing will chase them."
		)
	for action in actions:
		if not action["due_date"]:
			warnings.append(
				f"Corrective action {action['index']} has no due date, so nothing will ever say "
				"it is late. Most schemes give 28 days for a Major and days for a Critical."
			)

	return ToolResult(
		data={**described, "corrective_actions": actions, "warnings": warnings},
		summary=(
			f"recorded audit {doc.name} ({described['audit_type']}, {described['audit_date']}, "
			f"{len(actions)} corrective action(s))"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_audit_event(args: dict) -> ToolResult:
	"""Change an audit; add, amend and close individual corrective actions.

	`corrective_actions` REPLACES the whole table when given, which is the only
	safe semantics for a child table addressed by index: a merge would silently
	reorder rows and close the wrong finding. `close_corrective_action` and
	`add_corrective_actions` are the surgical paths, and they exist because
	replacing the table to close one row means resending every other row exactly.
	"""
	_require(AUDIT)
	company = _entity(args)
	row = _row(
		AUDIT,
		as_str(args, "audit", required=True),
		_AUDIT_FIELDS,
		"audit",
		"list_audit_events",
		company or "",
	)

	if as_str(args, "audit_name"):
		raise ToolError("audit_name cannot be changed: it is the docname. Nothing was changed.")
	if "corrective_actions_closed" in args:
		raise ToolError(
			"corrective_actions_closed is not set here — close_audit_event sets it, and refuses "
			"while any action is still open. A closure date over an open finding would be "
			"assembled into an audit packet as a finished audit. Nothing was changed."
		)

	doc = frappe.get_doc(AUDIT, row["name"])
	changes: dict = {}
	for key in ("auditor", "scope", "findings", "attached_report", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	if "audit_date" in args:
		_stage(changes, doc, "audit_date", as_date(args, "audit_date") or "")
	for key in ("audit_type", "result"):
		if key in args:
			value = as_str(args, key)
			_stage(changes, doc, key, as_choice(AUDIT, key, value, key) if value else "")
	if "company" in args:
		_stage(changes, doc, "company", company or "")

	replaced = False
	if "corrective_actions" in args:
		rows = _action_rows(args)
		before = len(doc.get("corrective_actions_required") or [])
		doc.set("corrective_actions_required", [])
		for entry in rows:
			doc.append("corrective_actions_required", entry)
		changes["corrective_actions_required"] = [f"{before} row(s)", f"{len(rows)} row(s)"]
		replaced = True

	added = _action_rows(args, "add_corrective_actions")
	for entry in added:
		doc.append("corrective_actions_required", entry)
	if added:
		changes["corrective_actions_added"] = [None, f"{len(added)} row(s)"]

	closed_index = as_int(args, "close_corrective_action")
	if closed_index is not None:
		rows = list(doc.get("corrective_actions_required") or [])
		if closed_index < 1 or closed_index > len(rows):
			raise ToolError(
				f"close_corrective_action {closed_index} is outside this audit, which has "
				f"{len(rows)} action(s). They are numbered from 1. get_audit_event lists them. "
				"Nothing was changed."
			)
		target = rows[closed_index - 1]
		what = as_str(args, "corrective_action") or str(target.get("corrective_action") or "")
		if not what.strip():
			raise ToolError(
				f"closing corrective action {closed_index} needs `corrective_action` — what "
				"actually changed. A tick in a box is what an auditor is trained to disbelieve. "
				"Nothing was changed."
			)
		target.corrective_action = what
		target.status = "Closed"
		target.closed_date = as_date(args, "closed_date") or frappe.utils.today()
		evidence = as_str(args, "evidence")
		if evidence:
			target.evidence = evidence
		changes[f"corrective_action_{closed_index}"] = ["Open", "Closed"]

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: audit_type, auditor, audit_date, result, "
			"scope, findings, company, attached_report, notes, corrective_actions, "
			"add_corrective_actions, close_corrective_action."
		)

	doc.save(ignore_permissions=True)
	today = frappe.utils.today()
	actions = _audit_actions(doc.name, today)
	described = _describe_audit(dict(doc.as_dict()), actions, today)

	data = {
		**described,
		"corrective_actions": actions,
		"changed": {key: [before, after] for key, (before, after) in changes.items()},
	}
	if replaced:
		data["note"] = (
			"The corrective actions table was REPLACED, not merged. A merge would reorder rows "
			"addressed by index and close the wrong finding — which is why close_corrective_action "
			"exists as its own argument."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {len(changes)} change(s), {described['open_action_count']} action(s) still open",
		docstatus_delta="0 → 0 (updated)",
	)


def close_audit_event(args: dict) -> ToolResult:
	"""Declare an audit finished. REFUSES while any corrective action is open."""
	_require(AUDIT)
	company = _entity(args)
	row = _row(
		AUDIT,
		as_str(args, "audit", required=True),
		_AUDIT_FIELDS,
		"audit",
		"list_audit_events",
		company or "",
	)
	today = frappe.utils.today()
	actions = _audit_actions(row["name"], today)
	open_actions = [action for action in actions if action["open"]]

	if row.get("corrective_actions_closed"):
		raise ToolError(
			f"{row['name']} was already closed on {row['corrective_actions_closed']}. Re-closing "
			"is a correction, and this tool only closes an open audit — edit it in the Desk if "
			"the date was genuinely wrong. Nothing was changed."
		)
	if open_actions:
		lines = "; ".join(
			f"{action['index']} ({action['severity']}) {str(action['finding'] or '')[:80]}"
			for action in open_actions
		)
		raise ToolError(
			f"{row['name']} has {len(open_actions)} corrective action(s) still open: {lines}. An "
			"audit marked closed over an open finding is the most misleading thing this app "
			"could record — it would be assembled into an audit packet as a finished audit and "
			"contradicted by the auditor's first question. Close each one with "
			"update_audit_event's close_corrective_action, saying what actually changed. "
			"Nothing was changed."
		)
	# An audit that raised no findings at all is legitimate and is worth closing —
	# a clean PrimusGFS is a real event — so the absence of actions is a note in
	# the result rather than a refusal.
	closure_date = as_date(args, "closed_date") or today
	if row.get("audit_date") and closure_date < str(row["audit_date"]):
		raise ToolError(
			f"the closure date {closure_date} is before the audit on {row['audit_date']}. "
			"Nothing was changed."
		)
	closure_note = _reason(args, "closure_note")

	if as_bool(args, "dry_run", False):
		return ToolResult(
			data={
				"audit": row["name"],
				"dry_run": True,
				"changed": False,
				"would_close_on": closure_date,
				"action_count": len(actions),
				"open_action_count": 0,
			},
			summary=f"dry run: would close {row['name']} on {closure_date}",
		)

	doc = frappe.get_doc(AUDIT, row["name"])
	doc.corrective_actions_closed = closure_date
	doc.closed_by = as_str(args, "closed_by") or str(frappe.session.user)
	doc.closure_note = closure_note
	doc.save(ignore_permissions=True)

	described = _describe_audit(dict(doc.as_dict()), actions, today)
	return ToolResult(
		data={
			**described,
			"corrective_actions": actions,
			"changed": True,
			"closed_on": closure_date,
			"closure_note": closure_note,
			"note": (
				(
					f"All {len(actions)} corrective action(s) were closed before this audit was. "
					if actions
					else "This audit raised no corrective actions. "
				)
				+ "generate_audit_packet will now include the period covering it; while an "
				"action was open it refused."
			),
			"next_step": ("The audit_action_overdue alert on this record auto-dismisses on the next sweep."),
		},
		summary=f"closed audit {row['name']} on {closure_date} — {closure_note}",
		docstatus_delta="0 → 0 (updated)",
	)
