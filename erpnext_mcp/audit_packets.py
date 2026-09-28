# SPDX-License-Identifier: MIT
"""Assembling the evidence bundle an auditor is actually handed.

WHAT MAKES THIS DIFFERENT FROM A REPORT. A report answers a question. A packet is
the thing somebody carries into a room and defends, and the difference shows up in
three places:

  * **It pulls from the operational records, not from a copy.** The spray records
    in an FSMA packet are the Spray Applications this app writes itself — see
    `_spray_records`, and note that it reads farm_precision_ag's Spray Log too
    rather than instead. The worker facility records are the Housing Units. There is no shadow store this reads from, which is the whole
    Sprint 7 stance and the reason `compliance_fields.py` puts the applicator's
    name on the spray rather than in a compliance table beside it. A packet
    assembled from a shadow copy is a packet that can disagree with the records
    the auditor asks to see next.

  * **It refuses to be produced early.** See the kairotic gate below. This is the
    part that most distinguishes it from every "export to PDF" button ever
    written.

  * **It says what it could not find.** A section with no records says so, in the
    packet, with the reason — and the reason distinguishes an EMPTY PERIOD from a
    MISSING REGISTER, because those are different facts about a farm. A
    spray-records section that is silently absent reads as an operation with
    nothing to declare, and an auditor will find the difference faster than the
    operator will.

THE KAIROTIC GATE, AND WHY IT IS A REFUSAL RATHER THAN A WARNING.

    A packet is not ready because the period ended. It is ready when the period
    is genuinely CLOSED — which means every corrective action raised inside it
    has been closed, and the period itself is actually over.

An FSMA packet produced while a corrective action from June is still open does not
contain a hole; it contains a claim. It says "here is the evidence of a compliant
operation for this period", hands it over, and is contradicted by the auditor's
first question — which will be about the open item, because open items are what
auditors look for. Producing it with a warning at the top does not help: warnings
are the first thing removed when a document is printed, and the auditor is reading
the evidence, not the covering note. So it refuses, and names every open action.

Overriding it is possible and is deliberately awkward: `allow_open_actions=true`
produces the packet with the open items listed in a section of their own at the
FRONT, because an operation that genuinely needs to hand something over
mid-remediation is better served by disclosing the remediation than by hiding it.

ADDING AN AUDIT TYPE is one `AuditPacketType` in the table below. Each names the
sections it pulls and the filters that scope them; the section builders are shared,
so a new type is a declaration rather than code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import frappe

from . import __version__, compat

#: Every section a packet can contain, and what each one reads. The order here is
#: the order they appear in the document, chosen so an auditor reads the claim
#: (policies, certificates) before the evidence (sprays, buckets, housing) and the
#: history (filings, audits) last.
SECTION_ORDER = (
	"open_actions",
	"alerts",
	"policies",
	"certifications",
	"workforce",
	"training",
	"heat_exposure",
	"spray_records",
	"water",
	"traceability",
	"housing",
	"sessions",
	"filings",
	"audits",
)

#: Hard cap on any one section. A packet is meant to be read, and an appendix
#: with eleven thousand bucket log rows in it is not. A cap that bites is stated
#: in the packet, never silent — the same rule the compliance-packet framework
#: applies for the same reason.
SECTION_CAP = 750

#: The two pesticide-application registers this packet can read, and the order it
#: reads them in. `Spray Application` is erpnext_mcp's own and ships with the app;
#: `Spray Log` is farm_precision_ag's and predates it. See `_spray_records`.
SPRAY_APPLICATION = "Spray Application"
SPRAY_APPLICATION_BLOCK = "Spray Application Block"
SPRAY_LOG = "Spray Log"

#: What a packet writes into a column the record left blank. It is a marker and
#: never an empty string, because an empty cell in a printed table reads as an
#: oversight in the packet rather than a gap in the record.
UNRECORDED = "(unrecorded)"

_SPRAY_COLUMNS = (
	"spray",
	"date",
	"block",
	"product",
	"applicator_name",
	"epa_reg_number",
	"rei_hours",
	"phi_days",
	"source",
)


@dataclass(frozen=True)
class AuditPacketType:
	"""One audit regime, and what it asks to see.

	`sections` names what goes in. `policy_categories`, `cert_types`,
	`audit_types` and `agencies` scope the evidence sections to what this regime
	is actually about — a DOL packet has no business containing a GlobalGAP
	certificate, and including it invites a question nobody wanted to answer.
	An empty tuple means "everything of that kind".
	"""

	key: str
	title: str
	regulator: str
	purpose: str
	sections: tuple
	policy_categories: tuple = ()
	cert_types: tuple = ()
	audit_types: tuple = ()
	agencies: tuple = ()
	#: Which `Employee Training Record.regimes` tags the training section pulls.
	#: v0.19.0. Empty means EVERY record, including untagged ones, which is right
	#: only for the two unscoped bundles (`Other`, `USDA_NIFA`) — an EPA packet
	#: containing a worker's organic-handling training invites a question nobody
	#: wanted to answer, exactly as a DOL packet containing a GlobalGAP
	#: certificate would. Overridable per call: `generate_audit_packet` takes a
	#: `regime` argument for the buyer who asks for one scheme by name.
	training_regimes: tuple = ()

	def describe(self) -> dict:
		return {
			"audit_type": self.key,
			"title": self.title,
			"regulator": self.regulator,
			"purpose": self.purpose,
			"sections": [section for section in SECTION_ORDER if section in self.sections],
			"scoped_to": {
				"policy_categories": list(self.policy_categories) or ["(every category)"],
				"certificate_types": list(self.cert_types) or ["(every type)"],
				"audit_types": list(self.audit_types) or ["(every type)"],
				"agencies": list(self.agencies) or ["(every agency)"],
				"training_regimes": list(self.training_regimes)
				or ["(every regime, including untagged records)"],
			},
			"switch": "allow_generate_audit_packet",
		}


TYPES: dict = {}


def register(spec: AuditPacketType) -> AuditPacketType:
	if spec.key in TYPES:
		raise RuntimeError(f"duplicate audit packet type {spec.key!r}")
	TYPES[spec.key] = spec
	return spec


def names() -> list:
	return sorted(TYPES)


def get(key: str):
	return TYPES.get(str(key or "").strip())


# ── the eight regimes ───────────────────────────────────────────────────────
register(
	AuditPacketType(
		key="FSMA",
		title="FSMA Produce Safety Rule",
		regulator="FDA (21 CFR 112), via a state-contracted inspector in Oregon",
		purpose=(
			"Worker health and hygiene, agricultural water, worker facilities, and "
			"traceability from the field to the shipment. The inspection an operation "
			"gets whether or not anybody buys on certification."
		),
		sections=(
			"open_actions",
			"alerts",
			"policies",
			"certifications",
			"workforce",
			"training",
			"water",
			"traceability",
			"housing",
			"sessions",
			"spray_records",
			"audits",
		),
		policy_categories=(
			"Harvest Hygiene",
			"Water Testing",
			"Worker Training",
			"Equipment Sanitation",
			"Recall and Traceability",
			"Housing",
		),
		audit_types=("FSMA", "FDA", "ODA"),
		# Subpart C personnel training is a named record type under §112.30(b),
		# and WPS training is pulled in beside it because a covered farm's
		# pesticide handlers are the same crew and the inspector asks about both.
		training_regimes=("FSMA", "WPS"),
	)
)

register(
	AuditPacketType(
		key="GAP",
		title="USDA Good Agricultural Practices",
		regulator="USDA AMS, or a licensed third-party auditor",
		purpose=(
			"The buyer-driven food safety audit. Broadly FSMA's ground with more weight "
			"on documented procedures and on the traceability exercise being timed."
		),
		sections=(
			"open_actions",
			"alerts",
			"policies",
			"certifications",
			"workforce",
			"training",
			"water",
			"sessions",
			"traceability",
			"spray_records",
			"audits",
		),
		policy_categories=(
			"Harvest Hygiene",
			"Water Testing",
			"Worker Training",
			"Equipment Sanitation",
			"Recall and Traceability",
			"Food Defense",
		),
		cert_types=("GAP", "GlobalGAP", "PrimusGFS", "Food Safety Training", "Water Test Certification"),
		audit_types=("GAP", "GlobalGAP", "PrimusGFS", "Buyer Audit", "Internal Audit"),
		# "Worker health & hygiene training log" is a named GAP record, and GAP
		# inherits the WPS 12-month requirement rather than restating it.
		training_regimes=("GAP", "WPS"),
	)
)

register(
	AuditPacketType(
		key="GlobalGAP",
		title="GLOBALG.A.P. Integrated Farm Assurance",
		regulator="A GLOBALG.A.P.-approved certification body",
		purpose=(
			"The export and retailer-facing scheme. Everything GAP asks plus worker "
			"welfare and a documented environmental position."
		),
		sections=(
			"open_actions",
			"alerts",
			"policies",
			"certifications",
			"workforce",
			"training",
			"water",
			"traceability",
			"housing",
			"sessions",
			"spray_records",
			"audits",
		),
		cert_types=("GlobalGAP", "GAP", "PrimusGFS", "Organic", "Food Safety Training"),
		audit_types=("GlobalGAP", "GAP", "PrimusGFS", "Buyer Audit"),
		training_regimes=("GlobalGAP", "GAP", "PrimusGFS", "WPS"),
	)
)

register(
	AuditPacketType(
		key="OSHA",
		title="Worker safety — OSHA and Oregon OSHA",
		regulator="Oregon OSHA (OAR 437), or federal OSHA",
		purpose=(
			"Heat illness prevention, field sanitation, agricultural labor housing, "
			"pesticide handler protection, and the 300A log. The inspection that "
			"follows a complaint or an injury."
		),
		sections=(
			"open_actions",
			"alerts",
			"policies",
			"certifications",
			"workforce",
			"training",
			"heat_exposure",
			"housing",
			"sessions",
			"spray_records",
			"filings",
			"audits",
		),
		policy_categories=("Worker Safety", "Worker Training", "Housing"),
		cert_types=("First Aid / CPR", "Applicator License", "Food Safety Training"),
		audit_types=("OSHA", "ODA"),
		agencies=("OSHA", "DOL", "WA-L&I"),
		# Heat illness (OAR 437-004-1131), field sanitation hygiene (-1110(9)),
		# hazard communication (-9800), PPE (-1005(10)) and seasonal orientation
		# (-0240) all land on the OR-OSHA tag; WPS handler training is pulled in
		# because Oregon enforces it at OAR 437-004-6501 in the same inspection.
		training_regimes=("OR-OSHA", "WPS"),
	)
)

register(
	AuditPacketType(
		key="DOL",
		title="Labor — MSPA, wage and hour, farm labor contracting",
		regulator="US Department of Labor Wage and Hour Division; Oregon BOLI",
		purpose=(
			"Employment eligibility, wage-law jurisdiction, farm labor contractor "
			"licensing, and the housing an employer provided. What a wage claim or an "
			"MSPA investigation asks for."
		),
		sections=(
			"open_actions",
			"alerts",
			"policies",
			"certifications",
			"workforce",
			"training",
			"housing",
			"sessions",
			"traceability",
			"filings",
			"audits",
		),
		policy_categories=("Worker Training", "Worker Safety", "Housing"),
		# MSPA asks what the crew was told about the terms and conditions of
		# employment, and an OR-OSHA-tagged safety orientation is the record that
		# most often carries it.
		training_regimes=("OR-OSHA",),
		cert_types=("Farm Labor Contractor License", "Commercial Driver License"),
		audit_types=("DOL", "OSHA"),
		agencies=("DOL", "OR-BOLI", "IRS", "OR-DOR", "WA-L&I"),
	)
)

register(
	AuditPacketType(
		key="EPA",
		title="Pesticides — FIFRA, the Worker Protection Standard, and ODA",
		regulator="EPA; Oregon Department of Agriculture (ORS 634)",
		purpose=(
			"Application records, applicator licensing, restricted-entry and "
			"pre-harvest intervals, and the drift conditions on the day. What a drift "
			"complaint or a residue detection is investigated from."
		),
		sections=(
			"open_actions",
			"alerts",
			"policies",
			"certifications",
			"spray_records",
			"water",
			"sessions",
			"workforce",
			"training",
			"filings",
			"audits",
		),
		policy_categories=("Spray SOP", "Worker Training", "Water Testing"),
		cert_types=("Applicator License",),
		# 40 CFR 170.401/.501 worker and handler training, kept two years at the
		# establishment per §170.309. This is the section a drift complaint is
		# investigated from after the spray records themselves.
		training_regimes=("WPS",),
		audit_types=("EPA", "ODA"),
		agencies=("EPA", "ODA"),
	)
)

register(
	AuditPacketType(
		key="USDA_NIFA",
		title="USDA NIFA — grant and programme reporting",
		regulator="USDA National Institute of Food and Agriculture",
		purpose=(
			"The compliance posture a grant application asserts and a programme review "
			"verifies. Certifications held, procedures in force, filings made."
		),
		sections=(
			"open_actions",
			"alerts",
			"policies",
			"certifications",
			"filings",
			"audits",
			"workforce",
			"training",
		),
		agencies=("USDA", "ODA", "IRS"),
	)
)

register(
	AuditPacketType(
		key="Other",
		title="Everything on file for the period",
		regulator="Whoever asked",
		purpose=(
			"An unscoped bundle, for a buyer's own audit or a question nobody has a "
			"template for. Wider than any of the named types and correspondingly "
			"harder to read — prefer a named type where one fits."
		),
		sections=SECTION_ORDER,
	)
)


# ── section builders ────────────────────────────────────────────────────────
def _rows(doctype: str, filters, fields, order_by: str = "", limit: int = SECTION_CAP + 1) -> list:
	if not compat.doctype_exists(doctype):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			doctype,
			filters=filters,
			fields=compat.existing_fields(doctype, fields),
			order_by=order_by or "modified desc",
			limit=limit,
		)
		or []
	]


def _section(title: str, purpose: str, rows: list, columns: tuple, absent: str = "") -> dict:
	"""One section, with its cap and its emptiness both stated rather than implied."""
	truncated = len(rows) > SECTION_CAP
	kept = rows[:SECTION_CAP]
	out = {
		"title": title,
		"purpose": purpose,
		"columns": list(columns),
		"row_count": len(kept),
		"rows": kept,
		"truncated": truncated,
	}
	if truncated:
		out["truncation_note"] = (
			f"More than {SECTION_CAP} records matched and this section holds the first "
			f"{SECTION_CAP}. THIS PACKET IS INCOMPLETE for this section — narrow the period "
			"before relying on it."
		)
	if not kept:
		out["empty_note"] = absent or (
			"No records matched for this period. That is a statement about the period, not "
			"about whether the records exist — check the dates before concluding anything."
		)
	return out


def _company_filter(company: str, fieldname: str = "company") -> dict:
	return {fieldname: company} if company else {}


def _policies(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Every procedure that was IN FORCE during the period.

	In force during the period, not in force today. A packet covering last summer
	wants the procedure the crew was actually working to last summer, which may
	well have been superseded since — and presenting today's SOP as evidence about
	last July is the single easiest way to be caught rewriting history.
	"""
	filters = _company_filter(company)
	rows = []
	found = _rows(
		"Compliance Policy",
		filters,
		(
			"name",
			"policy_name",
			"category",
			"version",
			"status",
			"effective_date",
			"review_due_date",
			"policy_owner",
			"superseded_by",
			"attached_document",
		),
		order_by="category asc, policy_name asc",
	)
	attached = _files_attached_to("Compliance Policy", [row["name"] for row in found])
	for row in found:
		if spec.policy_categories and str(row.get("category") or "") not in spec.policy_categories:
			continue
		if str(row.get("status") or "") == "Draft":
			# A draft was never adopted. Presenting one as a procedure in force
			# would be misleading in exactly the direction that matters.
			continue
		effective = str(row.get("effective_date") or "")
		if effective and effective > end:
			continue
		rows.append(
			{
				"policy": row["name"],
				"category": row.get("category"),
				"version": row.get("version"),
				"status": row.get("status"),
				"effective_date": effective or None,
				"in_force_at_period_end": str(row.get("status") or "") == "Active",
				"superseded_by": row.get("superseded_by") or None,
				# The Attach FIELD or a File linked to the record. `attach_file_to_document`
				# is a generic door and writes the second without the first, so reading only
				# the field printed a written procedure as an unsupported claim.
				"document_attached": bool(row.get("attached_document")) or bool(attached.get(row["name"])),
			}
		)
	section = _section(
		"Written procedures in force",
		(
			"The procedures the operation was working to during this period — including any "
			"since superseded, because the crew was working to those at the time and today's "
			"version is not evidence about last July. Drafts are excluded: one was never adopted."
		),
		rows,
		("policy", "category", "version", "status", "effective_date", "document_attached"),
		absent="No compliance policies are recorded for this company and audit type at all.",
	)

	# WHAT IS NOT HERE, which no list of what IS here can state. This regime
	# declares the categories its packet pulls; a category it pulls and this
	# company has no in-force policy for is a hole the section cannot show by
	# listing rows, because the missing procedure has no row. An auditor reading
	# six categories' worth of expectation against three policies works this out
	# in a minute — being shown it is strictly better than being found out.
	expected = list(spec.policy_categories)
	section["categories_expected"] = expected
	if expected:
		in_force = {row["category"] for row in rows if row["in_force_at_period_end"] and row["category"]}
		gaps = [category for category in expected if category not in in_force]
		section["categories_without_a_policy"] = gaps
		if gaps:
			section["problem_note"] = (
				f"{len(gaps)} of the {len(expected)} procedure categories this audit asks about "
				f"have NO policy in force at the end of the period: {', '.join(gaps)}. "
				"get_policy_coverage has the work list."
			)
	else:
		section["scope_note"] = (
			"This audit type pulls every policy category rather than a named list, so this "
			"packet cannot say which procedures it expected and did not find."
		)
	undocumented = [row["policy"] for row in rows if not row["document_attached"]]
	if undocumented:
		section["without_a_document"] = undocumented
		section["document_note"] = (
			f"{len(undocumented)} of these records assert a procedure with no document attached. "
			"An auditor asks to read the procedure, and the record is not the procedure."
		)
	return section


def _files_attached_to(doctype: str, names: list) -> dict:
	"""`{docname: [file, ...]}` for a batch, in one query.

	`attached_to_name` is asked for BY NAME rather than through
	`compat.existing_fields`: it is a framework column, `existing_fields` drops
	those, and a batched read that loses it files every attachment under one
	empty key.
	"""
	names = [str(name) for name in names if name]
	if not names or not compat.doctype_exists("File"):  # pragma: no cover - every site has File
		return {}
	found: dict = {}
	for row in (
		frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": doctype, "attached_to_name": ("in", names)},
			fields=["name", "attached_to_name", "file_name", "file_url"],
			limit=SECTION_CAP * 4,
		)
		or []
	):
		entry = dict(row)
		found.setdefault(str(entry.get("attached_to_name") or ""), []).append(entry)
	return found


def _certifications(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Certificates that were VALID at some point during the period."""
	rows = []
	for row in _rows(
		"Certification",
		_company_filter(company),
		(
			"name",
			"cert_name",
			"cert_type",
			"status",
			"holder",
			"issuing_body",
			"issued_date",
			"expiration_date",
			"certificate_number",
			"attached_certificate",
		),
		order_by="expiration_date desc",
	):
		if spec.cert_types and str(row.get("cert_type") or "") not in spec.cert_types:
			continue
		issued = str(row.get("issued_date") or "")
		expires = str(row.get("expiration_date") or "")
		if expires and expires < start:
			# Expired before the period began. Not evidence about the period.
			continue
		if issued and issued > end:
			continue
		covered = not expires or expires >= end
		rows.append(
			{
				"certificate": row["name"],
				"type": row.get("cert_type"),
				"holder": row.get("holder"),
				"issuing_body": row.get("issuing_body"),
				"number": row.get("certificate_number"),
				"issued": issued or None,
				"expires": expires or None,
				"covered_whole_period": covered,
				"document_attached": bool(row.get("attached_certificate")),
			}
		)
	gaps = [row["certificate"] for row in rows if not row["covered_whole_period"]]
	section = _section(
		"Certificates and licences",
		(
			"Every certificate that was valid at some point in the period. "
			"`covered_whole_period` is false where one expired part way through, which is a "
			"gap in coverage rather than an absence of evidence — it is shown rather than "
			"filtered out, because an auditor who finds it themselves asks a harder question."
		),
		rows,
		("certificate", "type", "holder", "issued", "expires", "covered_whole_period"),
		absent="No certificate of the types this audit is scoped to was valid during the period.",
	)
	if gaps:
		section["coverage_gaps"] = gaps
	return section


def _workforce(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Employment eligibility and wage-law jurisdiction, per person.

	Reads the compliance fields `install_compliance_fields` puts ON the Employee
	record — not a parallel HR compliance table, because there is not one. If the
	fields are not installed the section says so by name, which is a far more
	useful answer than an empty table.
	"""
	if not compat.doctype_exists("Employee"):
		return _section(
			"Workforce eligibility",
			"Employment eligibility, withholding and the wage law governing each person.",
			[],
			("employee", "i9_status", "w4_status", "jurisdiction"),
			absent=(
				"This site has no Employee register — no HR app is installed — so there is "
				"nothing to report. This is not evidence of a compliant workforce."
			),
		)
	if not compat.has_field("Employee", "i9_status"):
		return _section(
			"Workforce eligibility",
			"Employment eligibility, withholding and the wage law governing each person.",
			[],
			("employee", "i9_status", "w4_status", "jurisdiction"),
			absent=(
				"The compliance fields are not installed on this site's Employee doctype, so "
				"I-9 and W-4 status cannot be reported. Run install_compliance_fields. Until "
				"then this section is ABSENT rather than empty, and an auditor should be told so."
			),
		)

	filters = _company_filter(company)
	if compat.has_field("Employee", "status"):
		filters["status"] = "Active"
	rows = []
	for row in _rows(
		"Employee",
		filters,
		(
			"name",
			"employee_name",
			"company",
			"status",
			"date_of_joining",
			"i9_status",
			"w4_status",
			"jurisdiction",
			"flc_license_status",
			"flc_license_expiration",
		),
		order_by="employee_name asc",
	):
		rows.append(
			{
				"employee": row["name"],
				"name": row.get("employee_name"),
				"joined": str(row.get("date_of_joining") or "") or None,
				"i9_status": row.get("i9_status") or "(unrecorded)",
				"w4_status": row.get("w4_status") or "(unrecorded)",
				"jurisdiction": row.get("jurisdiction") or "(unrecorded)",
				"flc_license_status": row.get("flc_license_status") or None,
				"flc_license_expiration": str(row.get("flc_license_expiration") or "") or None,
			}
		)
	section = _section(
		"Workforce eligibility",
		(
			"I-9 and W-4 status and the wage law governing each active employee, read from the "
			"employee record itself. There is no separate HR compliance table — the columns are "
			"on the register payroll runs off, which is what makes them true."
		),
		rows,
		("employee", "name", "i9_status", "w4_status", "jurisdiction"),
	)
	problems = [row["employee"] for row in rows if row["i9_status"] in ("Expired", "(unrecorded)")]
	if problems:
		section["employees_without_a_verified_i9"] = problems
		section["problem_note"] = (
			f"{len(problems)} active employee(s) have an expired or unrecorded I-9. This is "
			"disclosed in the packet rather than filtered out of it: an auditor who finds it "
			"themselves asks a much harder question than one who was shown it."
		)
	return section


def _training(spec: AuditPacketType, company: str, start: str, end: str, regime: str = "") -> dict:
	"""What the crew was taught during the period, scoped to this audit's regimes.

	v0.19.0. THE SECTION THAT EXISTS BECAUSE ONE AFTERNOON ANSWERS FOUR AUDITS.
	A session covering hygiene, pesticide safety and heat satisfies GAP, WPS and
	OR-OSHA at once, and the record carries all three tags — so this pulls the
	subset THIS auditor is entitled to see rather than the whole register. An EPA
	packet containing a worker's organic-handling training invites a question
	nobody wanted to answer, exactly as a DOL packet containing a GlobalGAP
	certificate would.

	SCOPED BY `completed_date`, NOT BY EXPIRY. A record whose training happened
	inside the period belongs in the packet for that period whether or not it has
	since lapsed: an auditor asking about last season is asking what the crew had
	been taught by then, and silently dropping a since-expired record would
	overstate the position rather than understate it.

	THE §112.161 GAPS ARE DISCLOSED IN THE PACKET. Unsigned records and records
	with no supervisor review are counted and named, because "missing supervisor
	sign-off on the §112.161(b) records" is a finding FDA writes up even where the
	underlying training was fine — and an auditor who finds it themselves asks a
	much harder question than one who was shown it.
	"""
	from . import training as training_records

	if not compat.doctype_exists(training_records.DOCTYPE):
		return _section(
			"Worker training",
			"What each person was taught, when, on what topics, and who signed for it.",
			[],
			("record", "person", "training", "completed", "expires", "regimes"),
			absent=(
				"This site has no Employee Training Record DocType — it ships with erpnext_mcp "
				"from v0.19.0, so run `bench --site <site> migrate`. Until then this section is "
				"ABSENT rather than empty, and an auditor should be told which: an empty "
				"training table reads as a crew nobody trained."
			),
		)

	wanted = [
		regime_key
		for regime_key in ([training_records.canon(regime)] if regime else list(spec.training_regimes))
		if regime_key
	]
	rows = []
	seen = set()
	for regime_key in wanted or [""]:
		for row in training_records.for_regime(regime_key, company, start, end, limit=SECTION_CAP + 1):
			if row["name"] in seen:
				continue
			seen.add(row["name"])
			described = training_records.describe(row)
			rows.append(
				{
					"record": described["name"],
					"person": described["employee_name"] or described["employee"],
					"training": described["training_type"],
					"provider": described["provider"] or "(not recorded)",
					"completed": described["completed_date"],
					"activity_datetime": described["activity_datetime"],
					"expires": described["expires_date"] or "(one-time)",
					"regimes": ", ".join(described["regimes"]),
					"topics": "; ".join(described["content_topics_covered"]) or "(not recorded)",
					"trainee_signed": described["trainee_signed"],
					"supervisor_reviewed": described["supervisor_reviewed"],
					"supervisor_reviewed_on": described["supervisor_reviewed_on"],
					"status_at_period_end": training_records.status_for(row.get("expires_date"), end),
				}
			)
	rows.sort(key=lambda row: (str(row["completed"] or ""), str(row["record"])))

	# An audit type with NO `training_regimes` — `Other` and `USDA_NIFA`, the two
	# unscoped bundles — pulls every record INCLUDING untagged ones. That is right
	# for a bundle whose whole purpose is "everything on file", and it is stated
	# rather than implied, because "every tagged regime" would be a claim that
	# quietly excluded the records most likely to need attention.
	scope = ", ".join(wanted) if wanted else "every regime, including records carrying no tag"
	section = _section(
		"Worker training",
		(
			f"Training delivered during the period and tagged {scope}. One session can satisfy "
			"several regimes at once — the record carries every tag it earned and this section "
			"pulls the subset this audit is entitled to see. Scoped by the date the training "
			"HAPPENED, not by whether it is still current today: an auditor asking about the "
			"period is asking what the crew had been taught by then."
		),
		rows,
		("record", "person", "training", "completed", "expires", "regimes", "supervisor_reviewed"),
		absent=(
			f"No training record tagged {scope} was completed during this period. That is a "
			"statement about the period and the tags, not about whether anybody was trained — "
			"an untagged record exists but appears in no packet."
		),
	)
	section["regimes_pulled"] = wanted or ["(every tagged regime)"]
	unsigned = [row["record"] for row in rows if not row["trainee_signed"]]
	unreviewed = [row["record"] for row in rows if not row["supervisor_reviewed"]]
	lapsed = [row["record"] for row in rows if row["status_at_period_end"] == "Expired"]
	if unsigned:
		section["without_trainee_signature"] = unsigned[:100]
	if unreviewed:
		section["without_supervisor_review"] = unreviewed[:100]
	if lapsed:
		section["expired_by_period_end"] = lapsed[:100]
	problems = []
	if unsigned:
		problems.append(
			f"{len(unsigned)} record(s) carry no trainee signature (FSMA §112.161(a)(4), and one "
			"of the standard GAP section failures)"
		)
	if unreviewed:
		problems.append(
			f"{len(unreviewed)} record(s) have no supervisor review (FSMA §112.161(b) — the "
			"element a GAP-only operation most often lacks, cited even where the training was fine)"
		)
	if lapsed:
		problems.append(f"{len(lapsed)} record(s) had already expired by {end}")
	if problems:
		section["problem_note"] = (
			"; ".join(problems)
			+ ". Disclosed in the packet rather than filtered out of it: an auditor who finds "
			"this themselves asks a much harder question than one who was shown it."
		)
	return section


def _heat_exposure(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Every documented hot shift in the period, and what each one claims.

	v0.19.3, AND IT IS ON THE OSHA PACKET ALONE. OAR 437-004-1131 is Oregon OSHA's
	rule and nobody else asks about it: a GAP auditor handed a heat register is
	being shown evidence for a scheme they do not audit, which invites a question
	nobody wanted to answer, exactly as a DOL packet containing a GlobalGAP
	certificate would.

	SCOPED BY `event_date`, which is the day of the shift. A record filed a week
	later about a shift inside the period belongs in the packet for that period —
	the question is when the crew was exposed, not when somebody typed it in.

	DRAFTS ARE EXCLUDED. Submitting the record is the supervisor's attestation, so
	a draft is a page of ticks nobody has signed, and presenting one as evidence
	would be misleading in exactly the direction that matters.

	THE GAPS ARE DISCLOSED IN THE PACKET rather than left to be found. A record
	that does not claim shade was provided is on the face of the section, because
	an inspector who finds it themselves asks a much harder question than one who
	was shown it — and the shift behind each record carries the timeline that
	either supports the ticks or does not.
	"""
	from . import shifts as shift_records

	if not compat.doctype_exists(shift_records.HEAT_DOCTYPE):
		return _section(
			"Heat exposure (OAR 437-004-1131)",
			"What was done about the heat, per shift.",
			[],
			("record", "shift", "date", "max_heat_index_f", "water", "shade", "rest", "training"),
			absent=(
				"This site has no Heat Exposure Event DocType — it ships with erpnext_mcp from "
				"v0.19.3, so run `bench --site <site> migrate`. Until then this section is ABSENT "
				"rather than empty, and an inspector should be told which: an empty heat table "
				"reads as a season nobody documented."
			),
		)

	rows = []
	for row in _rows(
		shift_records.HEAT_DOCTYPE,
		_company_filter(company),
		shift_records.HEAT_FIELDS,
		order_by="event_date asc",
	):
		date = str(row.get("event_date") or "")
		if date and not (start <= date <= end):
			continue
		if int(row.get("docstatus") or 0) != 1:
			continue
		described = shift_records.describe_heat_event(row, with_plan=False)
		rows.append(
			{
				"record": row["name"],
				"shift": described["farm_shift"],
				"date": date or None,
				"max_heat_index_f": described["max_heat_index_f"],
				"water": described["water_provided"],
				"shade": described["shade_provided"],
				"rest": described["mandatory_rest_taken"],
				"training": described["training_verified"],
				"signs_observed": described["heat_illness_signs_observed"],
				"emergency_response": described["emergency_response_activated"],
				"signed": described["supervisor_signed"],
				"gaps": len(shift_records.heat_gaps(described)),
			}
		)

	section = _section(
		"Heat exposure (OAR 437-004-1131)",
		(
			"One record per documented hot shift: water at the required rate, shade within "
			"reach, the rest cycle TAKEN rather than offered, the crew observed for signs, and "
			"the training current. Each one points at a Farm Shift whose crew list and "
			"compliance-event timeline are the evidence behind the claims — the ticks are the "
			"assertion and the timeline is what supports them. Drafts are excluded: submitting "
			"the record is the supervisor's attestation, and an unsigned page of ticks is not "
			"evidence that anybody attested to anything."
		),
		rows,
		("record", "shift", "date", "max_heat_index_f", "water", "shade", "rest", "training"),
		absent=(
			"No Heat Exposure Event was filed for this company in this period. That is the right "
			"answer for a season that never reached an 80 °F heat index and a gap for one that "
			"did — and Oregon OSHA asks about the second."
		),
	)
	incomplete = [row["record"] for row in rows if row["gaps"]]
	incidents = [row["record"] for row in rows if row["signs_observed"]]
	if incomplete:
		section["with_unmet_obligations"] = incomplete
		section["problem_note"] = (
			f"{len(incomplete)} of {len(rows)} record(s) do not claim every -1131 obligation was "
			"met. They are on the face of this packet rather than left to be found: an inspector "
			"who finds a gap themselves asks a much harder question than one who was shown it, "
			"and the notes on each record say what happened instead."
		)
	if incidents:
		section["with_signs_observed"] = incidents
	return section


def _spray_records(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Pesticide applications in the period, from the application record itself.

	TWO REGISTERS, AND WHICH ONE IS THIS APP'S. `Spray Application` is
	erpnext_mcp's own doctype (v0.79.0, `tools/spray.py`) and ships with the app,
	so on any migrated site it EXISTS — which is why this section stopped being
	one that "will be empty here" for five of the eight packet types. `Spray Log`
	is farm_precision_ag's, predates it, and is read second so that a site which
	sprayed under the old app still hands over its history. Both are read; each
	row says which register it came from, because an auditor comparing the packet
	to the records has to know which screen to open.

	APPLIED, NOT PLANNED. A Planned application is an intention and a Cancelled
	one is a pass that did not happen; neither is evidence of anything going on
	the ground, and a packet that listed them would be claiming applications it
	cannot show. They are counted and named in `excluded_by_status` rather than
	dropped in silence — an auditor who finds a spray in the register that is
	missing from the packet asks a much harder question than one who was told.

	THE PRODUCTS COME OFF THE APPLICATION, NOT OFF THE TANK MIX. `products_applied`
	is the snapshot written at the moment of the pass, with the label numbers as
	they read on the day. Joining live to the mix or the Item would answer a
	question about today, and the question is about April.
	"""
	has_application = compat.doctype_exists(SPRAY_APPLICATION)
	has_log = compat.doctype_exists(SPRAY_LOG)
	if not has_application and not has_log:
		return _section(
			"Pesticide applications",
			"Every application made in the period, with the applicator, the product and the intervals.",
			[],
			_SPRAY_COLUMNS,
			absent=(
				"This site has neither a Spray Application register nor farm_precision_ag's "
				"Spray Log, so there is nothing to read. Spray Application ships with "
				"erpnext_mcp — run `bench migrate`. If applications were made in this period "
				"they are recorded somewhere else, and this packet does not contain them."
			),
		)

	rows, excluded = _spray_application_rows(company, start, end)
	rows.extend(_spray_log_rows(company, start, end))
	rows.sort(key=lambda row: (row["date"] or "", row["spray"]))

	section = _section(
		"Pesticide applications",
		(
			"Every application in the period as the register records it. The applicator, the "
			"registration number and the intervals are columns ON that record rather than a "
			"compliance copy of it — which is why they cannot have drifted from what was "
			"actually done."
		),
		rows,
		_SPRAY_COLUMNS,
	)
	if excluded:
		section["excluded_by_status"] = excluded
		section["status_note"] = (
			f"{len(excluded)} application(s) in this window are Planned or Cancelled and are "
			"NOT in the rows above: nothing went on the ground for them. They are named here "
			"so that a register compared against this packet reconciles."
		)
	incomplete = [
		row["spray"]
		for row in rows
		if row["applicator_name"] == UNRECORDED or row["epa_reg_number"] == UNRECORDED
	]
	if incomplete:
		section["incomplete_records"] = incomplete
		section["problem_note"] = (
			f"{len(incomplete)} application(s) have no applicator or no registration number on "
			"the record. They cannot be completed retroactively without inventing facts, and "
			"they are shown as they are."
		)
	unlicensed = [row["spray"] for row in rows if row["applicator_license"] == UNRECORDED]
	if unlicensed:
		section["without_applicator_licence"] = unlicensed
	return section


def _spray_application_rows(company: str, start: str, end: str) -> tuple:
	"""`(rows, excluded)` off this app's own Spray Application register."""
	if not compat.doctype_exists(SPRAY_APPLICATION):
		return [], []
	filters = _company_filter(company)
	filters["completed_at"] = ("between", [f"{start} 00:00:00", f"{end} 23:59:59"])
	found = _rows(
		SPRAY_APPLICATION,
		filters,
		(
			"name",
			"status",
			"completed_at",
			"started_at",
			"applicator",
			"applicator_license",
			"total_acres",
			"products_applied",
			"rei_hours",
			"phi_days",
			"phi_clears_on",
			"wind_speed_mph",
			"wind_direction",
			"temperature_f",
			"tank_mix",
			"sprayer",
		),
		order_by="completed_at asc",
	)
	applied, excluded = [], []
	for row in found:
		status = str(row.get("status") or "Applied")
		if status == "Applied":
			applied.append(row)
		else:
			excluded.append(f"{row['name']} ({status})")
	blocks = _spray_blocks([row["name"] for row in applied])
	names = _user_full_names(row.get("applicator") for row in applied)

	rows = []
	for row in applied:
		products = _spray_products(row.get("products_applied"))
		applicator = str(row.get("applicator") or "")
		rows.append(
			{
				"spray": row["name"],
				"date": str(row.get("completed_at") or row.get("started_at") or "")[:10] or None,
				"block": _joined(blocks.get(row["name"], ())),
				"product": _joined(line.get("item_name") or line.get("item") for line in products),
				"applicator_name": names.get(applicator) or applicator or UNRECORDED,
				"applicator_license": row.get("applicator_license") or UNRECORDED,
				"epa_reg_number": _joined(line.get("epa_reg_number") for line in products),
				"rei_hours": row.get("rei_hours"),
				"phi_days": row.get("phi_days"),
				"acres": row.get("total_acres"),
				"wind_mph": row.get("wind_speed_mph"),
				"wind_direction": row.get("wind_direction"),
				"temp_f": row.get("temperature_f"),
				"phi_clears_on": str(row.get("phi_clears_on") or "")[:10] or None,
				"tank_mix": row.get("tank_mix"),
				"sprayer": row.get("sprayer"),
				"source": SPRAY_APPLICATION,
			}
		)
	return rows, excluded


def _spray_log_rows(company: str, start: str, end: str) -> list:
	"""farm_precision_ag's register, for a site that sprayed before this app."""
	if not compat.doctype_exists(SPRAY_LOG):
		return []
	date_field = compat.first_field(SPRAY_LOG, "application_date", "spray_date", "date", "posting_date")
	filters = _company_filter(company)
	if date_field:
		filters[date_field] = ("between", (start, end))
	rows = []
	for row in _rows(
		SPRAY_LOG,
		filters,
		(
			"name",
			date_field or "creation",
			"field",
			"block",
			"product",
			"applicator_name",
			"applicator_license",
			"epa_reg_number",
			"rei_hours",
			"phi_hours",
			"weather_temp_f",
			"weather_wind_mph",
			"wind_direction",
			"target_pest",
		),
		order_by=f"{date_field} asc" if date_field else "creation asc",
	):
		rows.append(
			{
				"spray": row["name"],
				"date": str(row.get(date_field) or row.get("creation") or "")[:10] or None,
				"block": row.get("field") or row.get("block") or UNRECORDED,
				"product": row.get("product") or UNRECORDED,
				"applicator_name": row.get("applicator_name") or UNRECORDED,
				"applicator_license": row.get("applicator_license") or UNRECORDED,
				"epa_reg_number": row.get("epa_reg_number") or UNRECORDED,
				"rei_hours": row.get("rei_hours"),
				# Deliberately not converted into `phi_days`. The two registers
				# record the pre-harvest interval in different units and this
				# packet is evidence, so each row carries the number its own
				# record holds rather than one this file worked out.
				"phi_days": None,
				"phi_hours": row.get("phi_hours"),
				"target_pest": row.get("target_pest"),
				"wind_mph": row.get("weather_wind_mph"),
				"wind_direction": row.get("wind_direction"),
				"temp_f": row.get("weather_temp_f"),
				"source": SPRAY_LOG,
			}
		)
	return rows


def _spray_blocks(names) -> dict:
	"""`{application: [block, ...]}` for a batch, in one query.

	Read through the parent link rather than per row: a packet section is capped
	at 750 applications and 750 extra queries is how a packet stops being
	generated at all.
	"""
	names = [name for name in names if name]
	if not names or not compat.doctype_exists(SPRAY_APPLICATION_BLOCK):
		return {}
	found: dict = {}
	for row in (
		frappe.db.get_all(
			SPRAY_APPLICATION_BLOCK,
			filters={"parent": ("in", names)},
			# `parent` is asked for by name and NOT through `compat.existing_fields`.
			# It is a framework column rather than one of the DocType's own fields,
			# so `existing_fields` drops it — and a batched child read that loses
			# `parent` files every row under one empty key, which reads here as a
			# packet full of applications over nowhere.
			fields=["parent", "block", "acres"],
			limit=SECTION_CAP * 8,
		)
		or []
	):
		entry = dict(row)
		block = entry.get("block")
		if block:
			found.setdefault(str(entry.get("parent") or ""), []).append(block)
	return found


def _spray_products(raw) -> list:
	"""The product lines off the application's own stored snapshot."""
	if not raw:
		return []
	try:
		parsed = json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, ValueError, TypeError):
		return []
	if not isinstance(parsed, list):
		return []
	return [line for line in parsed if isinstance(line, dict)]


def _user_full_names(users) -> dict:
	"""`{user: full_name}` for a batch. A packet names people, not login ids."""
	wanted = sorted({str(user) for user in users if user})
	if not wanted or not compat.doctype_exists("User"):
		return {}
	found = {}
	for row in (
		frappe.db.get_all(
			"User",
			filters={"name": ("in", wanted)},
			fields=compat.existing_fields("User", ("name", "full_name")),
			limit=len(wanted),
		)
		or []
	):
		entry = dict(row)
		if entry.get("full_name"):
			found[str(entry.get("name"))] = entry["full_name"]
	return found


def _joined(values) -> str:
	"""Distinct, order-preserving, comma-joined — or the unrecorded marker."""
	seen = []
	for value in values:
		text = str(value).strip() if value not in (None, "") else ""
		if text and text not in seen:
			seen.append(text)
	return ", ".join(seen) if seen else UNRECORDED


def _water(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Agricultural water testing, per block and per zone."""
	rows = []
	for row in _rows(
		"Field",
		_company_filter(company, "owning_entity"),
		(
			"name",
			"field_name",
			"owning_entity",
			"crop",
			"condition",
			"water_test_last_date",
			"last_spray_date",
			"food_safety_zone",
			"worker_hygiene_station_present",
		),
		order_by="field_name asc",
	):
		tested = str(row.get("water_test_last_date") or "")
		rows.append(
			{
				"block": row["name"],
				"crop": row.get("crop"),
				"condition": row.get("condition"),
				"water_test_last_date": tested or None,
				"tested_within_period": bool(tested and start <= tested <= end),
				"current_at_period_end": bool(
					tested and tested >= str(frappe.utils.add_days(frappe.utils.getdate(end), -90))
				),
				"last_spray_date": str(row.get("last_spray_date") or "") or None,
				"worker_hygiene_station": compat.checked(row.get("worker_hygiene_station_present")),
			}
		)
	for row in _rows(
		"Irrigation Zone",
		_company_filter(company, "owning_entity"),
		(
			"name",
			"zone_name",
			"field",
			"water_source",
			"water_right_id",
			"water_test_last_date",
			"water_source_class",
			"chlorination_active",
		),
		order_by="zone_name asc",
	):
		tested = str(row.get("water_test_last_date") or "")
		rows.append(
			{
				"block": f"{row['name']} (zone)",
				"crop": row.get("water_source"),
				"condition": row.get("water_source_class"),
				"water_test_last_date": tested or None,
				"tested_within_period": bool(tested and start <= tested <= end),
				"current_at_period_end": bool(
					tested and tested >= str(frappe.utils.add_days(frappe.utils.getdate(end), -90))
				),
				"last_spray_date": None,
				"worker_hygiene_station": compat.checked(row.get("chlorination_active")),
			}
		)
	section = _section(
		"Agricultural water",
		(
			"Water test dates per block and per irrigation zone. `current_at_period_end` applies "
			"the Produce Safety Rule Subpart E ninety-day window to the END of the period, "
			"which is the question an inspector asks — not to today, which would flatter a "
			"packet about last season."
		),
		rows,
		(
			"block",
			"water_test_last_date",
			"tested_within_period",
			"current_at_period_end",
			"worker_hygiene_station",
		),
		absent="No Field or Irrigation Zone records exist for this company.",
	)
	stale = [row["block"] for row in rows if not row["current_at_period_end"]]
	if stale:
		section["without_a_current_test_at_period_end"] = stale
	return section


def _traceability(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""The bucket-to-shipment chain: every Bucket Log Entry in the period.

	Bucket Log Entry is erpnext_mcp's own doctype as of v0.44.0 (`bucket_log.py`,
	`bucket_bridge.py`) rather than an external app's — see
	`compliance_fields.py`'s Target entry for it, `mode="verify"`. So this reads
	`employee` (resolved from `worker_badge` by `link_badge_to_employee`) for the
	picker, and `verdict` for the disposition, rather than assuming columns a
	third-party doctype might or might not carry.
	"""
	doctype = "Bucket Log Entry"
	date_field = compat.first_field(
		doctype,
		"timestamp",
		"logged_at",
		"log_date",
		"date",
		"posting_date",
		"creation",
	)
	filters = _company_filter(company)
	if date_field and date_field != "creation":
		filters[date_field] = ("between", (start, end))
	rows = []
	for row in _rows(
		doctype,
		filters,
		(
			"name",
			date_field or "creation",
			"employee",
			"worker_badge",
			"crew_id",
			"block_id",
			"bin_id",
			"shipment_id",
			"verdict",
		),
		order_by=f"{date_field} asc" if date_field else "creation asc",
	):
		rows.append(
			{
				"entry": row["name"],
				"date": str(row.get(date_field) or row.get("creation") or "")[:10] or None,
				"picker": row.get("employee") or row.get("worker_badge") or "(unlinked)",
				"crew_id": row.get("crew_id") or "(unlinked)",
				"block_id": row.get("block_id") or "(unlinked)",
				"bin_id": row.get("bin_id") or "(unlinked)",
				"shipment_id": row.get("shipment_id") or "(unlinked)",
				"disposition": row.get("verdict"),
			}
		)
	section = _section(
		"Harvest traceability",
		(
			"The critical tracking events the FSMA Food Traceability Rule asks for, one row per "
			"bucket. A buyer's mock recall is timed, and the chain is only as good as its "
			"weakest link — unlinked columns are shown as such."
		),
		rows,
		("entry", "date", "picker", "crew_id", "block_id", "bin_id", "shipment_id"),
	)
	broken = [
		row["entry"] for row in rows if "(unlinked)" in (row["block_id"], row["bin_id"], row["shipment_id"])
	]
	if broken:
		section["chain_breaks"] = broken[:100]
		section["problem_note"] = (
			f"{len(broken)} entry/entries are missing a block, bin or shipment link. Each one is "
			"a bucket that cannot be traced end to end, which is the exercise the auditor times."
		)
	return section


def _housing(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Worker facilities: what there is, its condition, and who slept in it."""
	units = []
	for row in _rows(
		"Housing Unit",
		_company_filter(company, "owning_entity"),
		(
			"name",
			"unit_name",
			"unit_type",
			"parcel",
			"capacity",
			"condition",
			"fsma_worker_facility",
			"or_housing_law_compliant",
			"max_occupants_per_or_law",
			"last_habitability_inspection",
			"smoke_detector_last_test",
			"co_detector_last_test",
		),
		order_by="parcel asc, unit_name asc",
	):
		units.append(
			{
				"unit": row["name"],
				"type": row.get("unit_type"),
				"parcel": row.get("parcel"),
				"capacity": row.get("capacity"),
				"lawful_occupancy": row.get("max_occupants_per_or_law"),
				"condition": row.get("condition"),
				"fsma_worker_facility": compat.checked(row.get("fsma_worker_facility")),
				"last_habitability_inspection": str(row.get("last_habitability_inspection") or "") or None,
				"smoke_detector_last_test": str(row.get("smoke_detector_last_test") or "") or None,
				"co_detector_last_test": str(row.get("co_detector_last_test") or "") or None,
			}
		)
	assignments = _rows(
		"Housing Assignment",
		{"assigned_date": ("<=", end)},
		(
			"name",
			"unit",
			"employee",
			"employee_name",
			"assigned_date",
			"end_date",
			"housing_deduction_from_wages",
			"deposit_paid",
			"deposit_returned",
		),
		order_by="assigned_date asc",
	)
	occupancy = []
	for row in assignments:
		ended = str(row.get("end_date") or "")
		if ended and ended < start:
			continue
		occupancy.append(
			{
				"assignment": row["name"],
				"unit": row.get("unit"),
				"person": row.get("employee_name") or row.get("employee"),
				"from": str(row.get("assigned_date") or "") or None,
				"to": ended or "(still current)",
				"wage_deduction": row.get("housing_deduction_from_wages") or "Unknown",
			}
		)

	section = _section(
		"Worker facilities and occupancy",
		(
			"Every building, its condition and detector tests, and who slept in it during the "
			"period. The occupancy roster is the audit trail behind an IRS Section 119 exclusion "
			"and the answer to an ORS 653 wage-deduction claim, and it exists nowhere else."
		),
		units,
		("unit", "type", "parcel", "capacity", "condition", "last_habitability_inspection"),
		absent="No housing units are registered for this company.",
	)
	section["occupancy"] = occupancy[:SECTION_CAP]
	section["occupancy_count"] = len(occupancy[:SECTION_CAP])
	uninspected = [unit["unit"] for unit in units if not unit["last_habitability_inspection"]]
	if uninspected:
		section["never_inspected"] = uninspected
	undeclared = [row["assignment"] for row in occupancy if row["wage_deduction"] == "Unknown"]
	if undeclared:
		section["wage_deduction_unrecorded"] = undeclared[:100]
	# v0.203.0. Rodent bait is camp maintenance, so it is filed here: every
	# placement, check, removal and occupant notice in the period, by place,
	# with the tier it was done in (docs/design/rodent_bait_program.md §11).
	try:
		from . import rodent_bait

		pest = rodent_bait.audit_rows(company or "", start, end)
	except Exception:  # pragma: no cover - a packet must not fail over one register
		pest = []
	if pest:
		section["pest_control"] = pest[:SECTION_CAP]
	# v0.204.0. The application records themselves (docs/design/form_schema_and_labels.md §5).
	try:
		from . import pest_control

		applications = [
			row
			for row in pest_control.list_applications(company or "", "", SECTION_CAP)
			if (not start or str(row.get("applied_at") or "")[:10] >= start)
			and (not end or str(row.get("applied_at") or "")[:10] <= end)
		]
	except Exception:  # pragma: no cover
		applications = []
	if applications:
		section["pest_control_applications"] = applications
	return section


def _sessions(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Which visit produced which record. v0.21.0, and the chain of custody.

	THIS SECTION ADDS NO RECORD TO THE PACKET. Every compliance record a session
	produced is already in the packet, in the section that reads its own register
	— a Housing Inspection is in `housing` whether a session wrote it or a camp
	manager typed it, which is the point of producing the records separately in
	the first place.

	What was missing is the SENTENCE JOINING THEM. Three Housing Inspections, two
	Detector Tests and a Water Test filed within a minute of each other read, to
	an auditor, either as one afternoon's work or as somebody catching up on
	paperwork, and nothing in the register said which. This section says which:
	*these three records were captured in a single Cabin Opening session on
	2026-04-15 by Ana Ramos, foreman Miguel Torres, worked from version 2 of the
	template, evidence timestamped and signed*.

	SCOPED TO SUBMITTED SESSIONS IN THE PERIOD. A session started and never
	submitted produced nothing, and listing it beside the ones that did would put
	an abandoned visit in an audit packet with nothing to say it was abandoned.
	`open_sessions_in_period` counts them instead, which is the honest version of
	the same fact.
	"""
	from . import sessions as session_records

	if not compat.doctype_exists(session_records.SESSION_DOCTYPE):
		return _section(
			"Visits that produced these records",
			"The chain of custody between one worker's afternoon and the records it produced.",
			[],
			("session", "date", "template", "location", "worker", "records"),
			absent=(
				"This site has no Inspection Session DocType — run `bench migrate`. Every "
				"compliance record in this packet was filed one at a time, which is the pre-v0.21.0 "
				"shape and is complete evidence; what is absent is only the statement of which "
				"records were captured on the same visit."
			),
		)

	filters = dict(_company_filter(company))
	filters["state"] = ("in", [session_records.STATE_SUBMITTED, session_records.STATE_REVIEWED])
	filters["submitted_at"] = ("between", [start, f"{end} 23:59:59"])
	rows = _rows(
		session_records.SESSION_DOCTYPE,
		filters,
		(
			"name",
			"template",
			"template_version",
			"state",
			"location",
			"location_doctype",
			"worker",
			"worker_name",
			"foreman",
			"foreman_name",
			"visit_id",
			"farm_task",
			"started_at",
			"submitted_at",
		),
		order_by="submitted_at asc",
	)

	names = [str(row["name"]) for row in rows]
	produced = _produced_by_session(names)
	evidence = _session_evidence_counts(names)
	titles = _template_titles({str(row.get("template") or "") for row in rows})

	visits = []
	for row in rows:
		made = produced.get(str(row["name"])) or []
		visits.append(
			{
				"session": row["name"],
				"date": str(row.get("submitted_at") or "")[:10] or None,
				"template": titles.get(str(row.get("template") or ""), row.get("template")),
				"template_version": int(row.get("template_version") or 0) or None,
				"location": row.get("location"),
				"location_doctype": row.get("location_doctype"),
				"worker": row.get("worker_name") or row.get("worker"),
				"foreman": row.get("foreman_name") or row.get("foreman"),
				"records": ", ".join(sorted(made)) or "(evidence only)",
				"record_count": len(made),
				"evidence_files": evidence.get(str(row["name"]), 0),
				"started_at": str(row.get("started_at") or "") or None,
				"farm_task": row.get("farm_task") or None,
			}
		)

	section = _section(
		"Visits that produced these records",
		(
			"Which afternoon produced which record. Every compliance record listed elsewhere in "
			"this packet appears there on its own merits; this says which of them were captured "
			"on ONE visit, by whom, from which version of which template, with evidence shared "
			"between them. A photograph on the Housing Inspection and a photograph on the "
			"Detector Test from the same session are the SAME photograph, not two of the same wall."
		),
		visits,
		("session", "date", "template", "location", "worker", "foreman", "records"),
		absent=(
			"No templated visits were submitted in this period. Every compliance record in this "
			"packet was filed one at a time, which is complete evidence — what is absent is only "
			"the statement that several of them came from one trip."
		),
	)
	section["records_captured_in_sessions"] = sum(entry["record_count"] for entry in visits)
	try:
		open_now = frappe.db.count(
			session_records.SESSION_DOCTYPE,
			{
				**_company_filter(company),
				"state": ("in", list(session_records.OPEN_SESSION_STATES)),
				"started_at": ("between", [start, f"{end} 23:59:59"]),
			},
		)
	except Exception:
		open_now = 0
	if open_now:
		section["open_sessions_in_period"] = open_now
		section["problem_note"] = (
			f"{open_now} visit(s) were started in this period and never submitted, so they "
			"produced no compliance record and are not listed above. That is disclosed rather "
			"than left to be discovered: an abandoned visit is not evidence of anything, and a "
			"place whose work was started and dropped is a place whose alerts are still open."
		)
	return section


def _produced_by_session(names: list) -> dict:
	"""session docname → the doctypes its sections actually produced."""
	from . import sessions as session_records

	if not (names and compat.doctype_exists(session_records.SUBMISSION_DOCTYPE)):
		return {}
	# NOT through `_rows`: it filters the field list against the doctype's own
	# declared fields, and `parent` is a framework column rather than a declared
	# one — it would be dropped, and every row would come back without the key
	# this groups on.
	rows = frappe.db.get_all(
		session_records.SUBMISSION_DOCTYPE,
		filters={"parent": ("in", names), "parenttype": session_records.SESSION_DOCTYPE},
		fields=["parent", "produces_record_doctype", "produced_record_link"],
		order_by="idx asc",
		limit=SECTION_CAP * 8,
	)
	out = {}
	for row in rows or []:
		link = str(row.get("produced_record_link") or "").strip()
		if not link:
			continue
		# By record and not by row: two sections that produced ONE Detector Test
		# are one Detector Test, and counting them twice would tell an auditor
		# there is a record they cannot find.
		out.setdefault(str(row["parent"]), set()).add(link)
	return {name: sorted(links) for name, links in out.items()}


def _session_evidence_counts(names: list) -> dict:
	"""session docname → how many DISTINCT files its tray holds.

	Distinct files rather than rows, for the reason `list_visits` counts them the
	same way: one signature filed against three sections is one photograph, and
	reporting three would overstate the evidence in an audit packet.
	"""
	from . import sessions as session_records

	if not (names and compat.doctype_exists(session_records.EVIDENCE_DOCTYPE)):
		return {}
	rows = frappe.db.get_all(
		session_records.EVIDENCE_DOCTYPE,
		filters={"parent": ("in", names), "parenttype": session_records.SESSION_DOCTYPE},
		fields=["parent", "file", "file_url"],
		order_by="idx asc",
		limit=SECTION_CAP * 8,
	)
	seen = {}
	for row in rows or []:
		reference = str(row.get("file") or row.get("file_url") or "")
		if reference:
			seen.setdefault(str(row["parent"]), set()).add(reference)
	return {name: len(files) for name, files in seen.items()}


def _template_titles(names: set) -> dict:
	"""template docname → its human name, so a packet reads 'Cabin Opening'."""
	from . import sessions as session_records

	names = {name for name in names if name}
	if not (names and compat.doctype_exists(session_records.TEMPLATE_DOCTYPE)):
		return {}
	rows = _rows(
		session_records.TEMPLATE_DOCTYPE,
		{"name": ("in", sorted(names))},
		("name", "template_name"),
		order_by="name asc",
		limit=SECTION_CAP,
	)
	return {str(row["name"]): row.get("template_name") or row["name"] for row in rows}


def _filings(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Filings submitted during the period."""
	rows = []
	for row in _rows(
		"Regulatory Filing",
		_company_filter(company),
		(
			"name",
			"filing_name",
			"agency",
			"filing_type",
			"status",
			"period_covered",
			"submission_date",
			"docket_number",
			"response_received_date",
			"attached_filing",
		),
		order_by="submission_date asc",
	):
		if spec.agencies and str(row.get("agency") or "") not in spec.agencies:
			continue
		submitted = str(row.get("submission_date") or "")
		if not submitted or submitted < start or submitted > end:
			continue
		if str(row.get("status") or "") == "Draft":
			continue
		rows.append(
			{
				"filing": row["name"],
				"agency": row.get("agency"),
				"type": row.get("filing_type"),
				"period_covered": row.get("period_covered"),
				"submitted": submitted,
				"docket_number": row.get("docket_number"),
				"response_received": str(row.get("response_received_date") or "") or None,
				"status": row.get("status"),
				"document_attached": bool(row.get("attached_filing")),
			}
		)
	return _section(
		"Regulatory filings submitted",
		(
			"What went to an agency during this period, with the confirmation number that "
			"proves it. A filing nobody can prove was made is a filing that was not made, and "
			"the agency's position in a dispute is that they have no record. Drafts are "
			"excluded: nothing was sent."
		),
		rows,
		("filing", "agency", "type", "submitted", "docket_number", "status"),
		absent="No filing was submitted to a relevant agency during this period.",
	)


def _audits(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Audits during the period, and every corrective action with its closure."""
	rows = []
	actions = []
	for row in _rows(
		"Audit Event",
		_company_filter(company),
		(
			"name",
			"audit_name",
			"audit_type",
			"auditor",
			"audit_date",
			"result",
			"corrective_actions_closed",
			"attached_report",
		),
		order_by="audit_date asc",
	):
		if spec.audit_types and str(row.get("audit_type") or "") not in spec.audit_types:
			continue
		audit_date = str(row.get("audit_date") or "")
		if not audit_date or audit_date < start or audit_date > end:
			continue
		rows.append(
			{
				"audit": row["name"],
				"type": row.get("audit_type"),
				"auditor": row.get("auditor"),
				"date": audit_date,
				"result": row.get("result"),
				"closed": str(row.get("corrective_actions_closed") or "") or None,
				"report_attached": bool(row.get("attached_report")),
			}
		)
		for index, action in enumerate(_actions_of(row["name"]), start=1):
			actions.append({"audit": row["name"], "index": index, **action})

	section = _section(
		"Audits and inspections",
		(
			"Every audit and inspection in the period, with each corrective action and its "
			"closure. An operation is not judged on having no findings — every audit produces "
			"some — it is judged on closing them, which is what the second table shows."
		),
		rows,
		("audit", "type", "auditor", "date", "result", "closed"),
		absent="No audit or inspection of a relevant type took place during this period.",
	)
	section["corrective_actions"] = actions[:SECTION_CAP]
	section["corrective_action_count"] = len(actions[:SECTION_CAP])
	section["all_closed"] = not any(action["status"] in ("Open", "In Progress") for action in actions)
	return section


def _actions_of(audit: str) -> list:
	try:
		doc = frappe.get_doc("Audit Event", audit)
	except Exception:  # pragma: no cover
		return []
	return [
		{
			"finding": row.get("finding"),
			"severity": row.get("severity") or "Minor",
			"status": row.get("status") or "Open",
			"due_date": str(row.get("due_date") or "") or None,
			"closed_date": str(row.get("closed_date") or "") or None,
			"corrective_action": row.get("corrective_action"),
			"evidence": row.get("evidence"),
		}
		for row in doc.get("corrective_actions_required") or []
	]


def _open_actions_section(spec: AuditPacketType, company: str, start: str, end: str) -> dict:
	"""Only ever present when the kairotic gate was overridden. See `open_actions`."""
	rows = [
		{
			"audit": entry["audit"],
			"finding": entry["finding"],
			"severity": entry["severity"],
			"due_date": entry["due_date"],
			"days_overdue": entry["days_overdue"],
			"assigned_to": entry.get("assigned_to"),
		}
		for entry in open_actions(company, start, end)
	]
	return _section(
		"OPEN CORRECTIVE ACTIONS — remediation in progress",
		(
			"This packet was produced while these findings were still open, because somebody "
			"passed allow_open_actions=true. They are at the FRONT rather than buried: an "
			"operation that has to hand something over mid-remediation is far better served by "
			"disclosing the remediation than by having the auditor find it."
		),
		rows,
		("audit", "finding", "severity", "due_date", "days_overdue"),
		absent="No corrective action was open. This section should not have been produced.",
	)


def _alerts(spec: AuditPacketType, company: str, start: str, end: str, regime: str = "") -> dict:
	"""What the compliance calendar still has open, scoped to this audit's regimes.

	v0.19.2, AND IT IS THE SECTION MOST LIKELY TO BE QUESTIONED, so the argument
	for it goes here rather than in a release note.

	A packet asserts a compliant period. Putting the operation's own list of
	outstanding items into it looks, at first glance, like handing an auditor the
	prosecution's case. It is the opposite, for the same reason
	`_open_actions_section` exists and for the reason the readiness gate is a
	refusal rather than a warning: THE GATE ALREADY REFUSED THE PACKET IF ANY
	CORRECTIVE ACTION FROM INSIDE THE PERIOD IS STILL OPEN. So on an ordinary
	packet this section is not a confession — it is the operation demonstrating
	that it knows what it owes, on a system that generated the list from its own
	records rather than from somebody's memory the night before. An auditor's
	question is never "do you have open items"; it is "do you know what they are".

	SCOPED TO THE PACKET'S REGIMES, which is what keeps it honest in the other
	direction. An EPA inspector is entitled to the WPS items and has no business
	with the camp's detector schedule, exactly as the certificate section does not
	hand a DOL auditor a GlobalGAP certificate. An audit type with no
	`training_regimes` — the two unscoped bundles — gets everything, and the
	section says which it did.

	SNOOZED AND DISMISSED ITEMS ARE EXCLUDED. A dismissal is somebody's recorded
	judgement that an item did not need doing, and a snooze is "not this week";
	neither is an open obligation, and listing them would turn a disclosure into a
	diary. What IS included is everything currently live, whatever its severity.

	IT IS THE ONLY SECTION NOT SCOPED TO THE PERIOD, and the asymmetry is the
	point rather than an oversight. Every other section is EVIDENCE ABOUT A
	PERIOD — what was sprayed, who was trained, which cabins were walked. An open
	alert is a fact about TODAY: an expired licence is expired now, whatever
	quarter the packet covers. Filtering it by the packet's dates would produce a
	list of what was outstanding last March, which is a different document and a
	much less useful one — and on a properly closed period it would be empty
	every time, which reads as an operation with nothing outstanding.
	"""
	if not compat.doctype_exists("Compliance Alert"):
		return _section(
			"Compliance calendar — open items",
			"What the operation's own alert engine still has open for this audit.",
			[],
			("alert", "severity", "what", "due_date", "open_since", "regimes"),
			absent=(
				"This site has no Compliance Alert DocType, so the section is ABSENT rather than "
				"empty — an empty list of open items reads as an operation with none."
			),
		)

	from . import alerts as alert_engine
	from . import training as training_records

	wanted = [
		key for key in ([training_records.canon(regime)] if regime else list(spec.training_regimes)) if key
	]

	filters = {"dismissed": 0}
	if company:
		filters["company"] = company
	rows = _rows(
		"Compliance Alert",
		filters,
		(
			"name",
			"alert_type",
			"severity",
			"category",
			"company",
			"source_doctype",
			"source_docname",
			"alert_message",
			"due_date",
			"first_seen",
			"snoozed_until",
		),
		order_by="due_date asc",
		limit=SECTION_CAP * 2 + 1,
	)

	tags = alert_engine.regimes_for_alerts([row.get("name") for row in rows])
	today = frappe.utils.today()
	out = []
	for row in rows:
		snoozed = str(row.get("snoozed_until") or "")
		if snoozed and snoozed >= today:
			continue
		found = tags.get(str(row.get("name")), [])
		# By TOKEN. `"GlobalGAP"` contains `"GAP"`, and a substring match would put
		# another scheme's open findings in front of a USDA GAP auditor.
		if wanted and not any(key in found for key in wanted):
			continue
		out.append(
			{
				"alert": row.get("name"),
				"severity": row.get("severity"),
				"category": row.get("category"),
				"what": row.get("alert_message"),
				"source": f"{row.get('source_doctype') or ''} {row.get('source_docname') or ''}".strip(),
				"due_date": str(row.get("due_date") or "") or None,
				"open_since": str(row.get("first_seen") or "") or None,
				"regimes": ", ".join(found),
			}
		)

	scope = ", ".join(wanted) if wanted else "every regime, including items carrying no tag"
	section = _section(
		"Compliance calendar — open items",
		(
			f"Everything this operation's own alert engine currently has open for {scope}, "
			"generated from the state of the records rather than compiled by hand. The gate "
			"this packet passed already refused it over any open corrective action from inside "
			"the period, so what is here is forward-looking work rather than unfinished "
			"remediation — an operation that knows what it owes."
		),
		out,
		("alert", "severity", "what", "due_date", "open_since", "regimes"),
		absent=(
			f"Nothing is open on the compliance calendar for {scope} as at {today}. Snoozed and "
			"dismissed items are excluded by design: neither is an open obligation."
		),
	)
	section["regimes_pulled"] = wanted or ["(every tagged regime)"]
	return section


_BUILDERS = {
	"open_actions": _open_actions_section,
	"alerts": _alerts,
	"policies": _policies,
	"certifications": _certifications,
	"workforce": _workforce,
	"training": _training,
	"heat_exposure": _heat_exposure,
	"spray_records": _spray_records,
	"water": _water,
	"traceability": _traceability,
	"housing": _housing,
	"sessions": _sessions,
	"filings": _filings,
	"audits": _audits,
}


# ── the kairotic gate ───────────────────────────────────────────────────────
def open_actions(company: str, start: str, end: str) -> list:
	"""Corrective actions raised in the period that are still open.

	Scoped to the period on purpose. An action from an audit two years after this
	packet's period has nothing to do with whether this period is closed, and
	blocking on it would make a packet about 2024 impossible to produce forever.
	"""
	if not compat.doctype_exists("Audit Event"):
		return []
	today = frappe.utils.today()
	out = []
	for row in _rows(
		"Audit Event",
		{**_company_filter(company), "corrective_actions_closed": ("is", "not set")},
		("name", "audit_name", "audit_type", "audit_date", "company"),
		order_by="audit_date asc",
		limit=SECTION_CAP,
	):
		audit_date = str(row.get("audit_date") or "")
		if not audit_date or audit_date < start or audit_date > end:
			continue
		try:
			doc = frappe.get_doc("Audit Event", row["name"])
		except Exception:  # pragma: no cover
			continue
		for index, action in doc.open_actions():
			due = str(action.get("due_date") or "")
			overdue = None
			if due:
				try:
					overdue = max(0, int(frappe.utils.date_diff(today, due)))
				except Exception:  # pragma: no cover
					overdue = None
			out.append(
				{
					"audit": row["name"],
					"audit_type": row.get("audit_type"),
					"index": index,
					"finding": action.get("finding"),
					"severity": action.get("severity") or "Minor",
					"status": action.get("status") or "Open",
					"due_date": due or None,
					"days_overdue": overdue,
					"assigned_to": action.get("assigned_to"),
				}
			)
	return out


def readiness(spec: AuditPacketType, company: str, start: str, end: str, today: str = "") -> dict:
	"""Whether the period is genuinely CLOSED, and what is stopping it if not.

	Two conditions, and neither is "the end date has passed on a calendar":

	  1. The period is actually over. A packet covering a period that has not
	     finished is a packet about records that do not exist yet.
	  2. Every corrective action raised inside it has been closed. This is the
	     kairotic half — the packet asserts a compliant period, and an open finding
	     inside that period contradicts the assertion.
	"""
	today = today or frappe.utils.today()
	blockers = []
	if end > today:
		blockers.append(
			{
				"blocker": "period_not_over",
				"detail": (
					f"This packet covers up to {end}, which is in the future — today is {today}. "
					"The records for the rest of it do not exist yet, so a packet produced now "
					"would assert evidence of a period that has not happened."
				),
			}
		)
	pending = open_actions(company, start, end)
	if pending:
		blockers.append(
			{
				"blocker": "open_corrective_actions",
				"detail": (
					f"{len(pending)} corrective action(s) raised by audits INSIDE this period are "
					"still open. This packet would assert a compliant period and be contradicted "
					"by the auditor's first question, which will be about the open item — open "
					"items are what auditors look for."
				),
				"actions": pending,
			}
		)
	return {
		"ready": not blockers,
		"today": today,
		"blockers": blockers,
		"open_action_count": len(pending),
	}


# ── assembly ────────────────────────────────────────────────────────────────
def build(
	spec: AuditPacketType,
	company: str,
	start: str,
	end: str,
	allow_open_actions: bool = False,
	regime: str = "",
) -> dict:
	"""Assemble one packet. Reads only; writes nothing.

	`regime` (v0.19.0) narrows the training section to ONE regime tag, overriding
	the audit type's own list. It exists for the buyer who asks for one scheme by
	name — "send me your WPS training records for 2026" — and for the auditor
	wearing a different hat from the one the packet is titled for, which in Oregon
	is the ordinary case: the same ODA inspector runs a GAP audit one day and an
	FDA-contracted FSMA inspection the next.

	v0.19.2 MADE `regime` NARROW THE OPEN-ITEMS SECTION TOO, and that is the whole
	of the change: the two regime-aware sections now answer to the same argument.
	Before it, a packet narrowed to WPS carried WPS training records beside a
	calendar section listing the camp's detector schedule, which is the mismatch
	the argument exists to prevent.
	"""
	sections = []
	for key in SECTION_ORDER:
		if key not in spec.sections:
			continue
		if key == "open_actions" and not allow_open_actions:
			# Only ever present on an overridden gate. On a clean packet there is
			# nothing to disclose and a section saying "none" would be noise.
			continue
		if key in ("training", "alerts"):
			# The two sections that are ABOUT regimes rather than merely scoped by
			# one. Everything else takes the audit type's own filters and does not
			# know `regime` exists.
			sections.append({"key": key, **_BUILDERS[key](spec, company, start, end, regime)})
			continue
		sections.append({"key": key, **_BUILDERS[key](spec, company, start, end)})

	counts = {section["key"]: section["row_count"] for section in sections}
	problems = []
	for section in sections:
		if section.get("problem_note"):
			problems.append({"section": section["key"], "detail": section["problem_note"]})
		if section.get("truncated"):
			problems.append({"section": section["key"], "detail": section["truncation_note"]})
		if section.get("empty_note") and not section["row_count"]:
			problems.append({"section": section["key"], "detail": section["empty_note"]})

	return {
		"audit_type": spec.key,
		"title": spec.title,
		"regulator": spec.regulator,
		"purpose": spec.purpose,
		"company": company or None,
		"period_start": start,
		"period_end": end,
		"sections": sections,
		"section_counts": counts,
		"total_records": sum(counts.values()),
		"disclosures": problems,
		"generated_at": frappe.utils.now(),
		"generated_by": str(frappe.session.user),
		"site": str(frappe.local.site),
		"generator": "erpnext_mcp",
		"generator_version": __version__,
		"produced_over_open_actions": bool(allow_open_actions),
		# KEPT UNDER ITS v0.19.0 NAME as well as the new one. It scopes two
		# sections now rather than only the training register, so
		# `training_regime_override` has become the narrower word for the wider
		# thing — but it is on every packet produced since v0.19.0, and a key
		# renamed is a key that silently reads as None in whatever was consuming
		# it. The two always hold the same value.
		"training_regime_override": str(regime or "") or None,
		"regime_override": str(regime or "") or None,
	}


def document_sections(packet: dict):
	"""The packet as a (kind, payload) stream both renderers consume.

	One description, two output formats. The same shape `investment_report` uses,
	and for the same reason: a PDF and a DOCX that were built by two separate
	walks of the same data will differ within one release, and the difference will
	be discovered by whoever printed the wrong one.
	"""
	yield (
		"title",
		(
			f"{packet['title']} — Audit Packet",
			(
				packet["company"] or "all companies on this site",
				f"{packet['period_start']} to {packet['period_end']}",
				f"Prepared for: {packet['regulator']}",
			),
		),
	)
	yield ("paragraph", packet["purpose"])

	yield ("heading", "What is in this packet")
	yield (
		"table",
		(
			["Section", "Records"],
			[[section["title"], str(section["row_count"])] for section in packet["sections"]],
			("l", "r"),
		),
	)
	if packet["produced_over_open_actions"]:
		yield (
			"paragraph",
			"THIS PACKET WAS PRODUCED WHILE CORRECTIVE ACTIONS WERE STILL OPEN. They are listed "
			"in the first section. It is disclosed here rather than left to be discovered.",
		)
	if packet["disclosures"]:
		yield ("subheading", "Disclosures")
		yield ("bullets", [f"{entry['section']}: {entry['detail']}" for entry in packet["disclosures"]])

	for section in packet["sections"]:
		yield ("page_break", None)
		yield ("heading", section["title"])
		yield ("paragraph", section["purpose"])
		if not section["row_count"]:
			yield ("paragraph", section.get("empty_note") or "No records.")
			continue
		columns = section["columns"]
		yield (
			"table",
			(
				[column.replace("_", " ").title() for column in columns],
				[[_cell(row.get(column)) for column in columns] for row in section["rows"]],
				tuple("l" for _ in columns),
			),
		)
		if section.get("truncation_note"):
			yield ("paragraph", section["truncation_note"])
		if section.get("problem_note"):
			yield ("paragraph", section["problem_note"])
		if section.get("occupancy"):
			yield ("subheading", "Occupancy during the period")
			yield (
				"table",
				(
					["Assignment", "Unit", "Person", "From", "To", "Wage deduction"],
					[
						[
							row["assignment"],
							row["unit"],
							_cell(row["person"]),
							_cell(row["from"]),
							_cell(row["to"]),
							row["wage_deduction"],
						]
						for row in section["occupancy"]
					],
					("l", "l", "l", "l", "l", "l"),
				),
			)
		if section.get("corrective_actions"):
			yield ("subheading", "Corrective actions")
			yield (
				"table",
				(
					["Audit", "Finding", "Severity", "Status", "Due", "Closed"],
					[
						[
							row["audit"],
							_cell(row["finding"])[:120],
							row["severity"],
							row["status"],
							_cell(row["due_date"]),
							_cell(row["closed_date"]),
						]
						for row in section["corrective_actions"]
					],
					("l", "l", "l", "l", "l", "l"),
				),
			)

	yield ("page_break", None)
	yield ("heading", "Provenance")
	yield (
		"key_values",
		[
			("Audit type", packet["audit_type"]),
			("Company", packet["company"] or "all companies"),
			("Period", f"{packet['period_start']} to {packet['period_end']}"),
			("Records in this packet", str(packet["total_records"])),
			("Generated at", packet["generated_at"]),
			("Generated by", packet["generated_by"]),
			("Site", packet["site"]),
			("Generator", f"erpnext_mcp {packet['generator_version']}"),
		],
	)
	yield (
		"paragraph",
		"Every record in this packet was read from the operational document that produced it — "
		"the spray records ARE the spray logs, the worker facility records ARE the housing "
		"register, the traceability rows ARE the bucket log. Nothing here is a copy kept for "
		"compliance purposes, which is why nothing here can have drifted from what was actually "
		"done.",
	)
	yield (
		"paragraph",
		"A packet is refused on a period whose corrective actions are still open, and on a "
		"period that has not finished. Neither is a warning at the top of a document nobody "
		"reads; both are refusals.",
	)


def _cell(value) -> str:
	if value is None:
		return ""
	if isinstance(value, bool):
		return "yes" if value else "no"
	return str(value)
