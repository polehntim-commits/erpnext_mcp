# SPDX-License-Identifier: MIT
"""Periodic reporting: the shape of a report, the numbers that fill it, the list that says it is finished.

FOUR OBJECTS, AND THE DISTINCTIONS BETWEEN THEM ARE THE DESIGN:

    REPORTING TEMPLATE     which SECTIONS a report has, and where each is filled from
    MD&A DATA FEED         the numbers a management discussion is written FROM
    SEGMENT REPORT         the same numbers cut by the parts of the business
    DISCLOSURE CHECKLIST   which DISCLOSURES the filing must make, and who decided

A section and a disclosure are not the same thing and are deliberately not one
table: "Results of Operations" is a section that may carry four disclosures or
none, and folding them together would mean either a section per disclosure — a
report with sixty headings — or a disclosure that could only exist where somebody
had already written a section for it, which is precisely how a disclosure gets
omitted.

NOTHING HERE FILES ANYTHING. Every generator returns a working paper: a
skeleton with its sections named and its sources pointed at, a feed of figures
with their windows stated, a segment cut with its thresholds shown. A person
writes the report. This app's job is to make sure the numbers came from the
ledger rather than from a spreadsheet somebody maintained by hand, and to say out
loud which numbers it could not get.

WHAT THE FEEDS DO WHEN A SOURCE IS MISSING. They report the gap and carry on.
A site with no KPI definitions, no budget, or no compliance calendar still gets
an MD&A feed — with `unavailable` naming what could not be read and why. The
alternative is a generator that raises on the first missing input, which on a
farm mid-setup means a tool that never works and is therefore never used. A feed
that is honest about its holes is the useful thing; a feed that pretends to
completeness is the dangerous one.

SEGMENTS ARE COST CENTRES, and only where somebody has made them mean something.
ASC 280's test is quantitative — ten per cent of combined revenue or of combined
profit — and this applies it and SHOWS ITS WORKING rather than returning a
verdict. Whether the orchard and the packing line are genuinely different
operating segments is a judgement about how the business is managed, which no
query can make and this tool does not pretend to.
"""

import frappe

from .. import compat, enforcement
from ..args import as_bool, as_choice, as_date, as_int, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.disclosure_checklist.disclosure_checklist import (
	FILED,
	FINAL_STATES,
	NOT_APPLICABLE,
	OUTSTANDING,
	SETTLED_STATES,
	cell,
	set_cell,
)
from ..errors import ToolError
from ..result import ToolResult

TEMPLATE = "Reporting Template"
CHECKLIST = "Disclosure Checklist"

DISCLOSURE_COMPLETENESS = "disclosure_completeness"

#: ASC 280's quantitative threshold for a reportable segment: ten per cent of
#: combined revenue, or of combined profit. Applied and SHOWN rather than
#: returned as a verdict — see the module docstring.
SEGMENT_THRESHOLD = 0.10

#: ASC 280 again: the reportable segments together have to carry 75% of external
#: revenue, and where they do not, more segments are added until they do. Reported
#: as a check somebody has to act on rather than acted on automatically.
SEGMENT_COVERAGE_TARGET = 0.75

_TEMPLATE_FIELDS = (
	"name",
	"template_name",
	"company",
	"report_type",
	"enabled",
	"label_en",
	"label_es",
	"regulatory_basis",
	"description_en",
	"description_es",
	"shipped_default",
	"notes",
)

_CHECKLIST_FIELDS = (
	"name",
	"company",
	"filing_type",
	"status",
	"period_start",
	"period_end",
	"due_date",
	"fiscal_year",
	"reporting_template",
	"prepared_by",
	"reviewed_by",
	"filed_on",
	"notes",
	"creation",
	"owner",
)


def available() -> bool:
	return compat.doctype_exists(TEMPLATE) and compat.doctype_exists(CHECKLIST)


def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _language(args: dict) -> str:
	return (as_str(args, "language") or "en").strip().lower() or "en"


def _label(row, base: str, language: str, missing: list, where: str) -> str:
	"""The label in the caller's language, falling back to English and SAYING SO."""
	if language.startswith("es"):
		value = str(row.get("label_es") or "").strip()
		if value:
			return value
		missing.append(where)
	value = str(row.get("label_en") or "").strip()
	return value or str(row.get(base) or "")


# ── reporting templates ─────────────────────────────────────────────────────
def _describe_template(doc, language: str = "en") -> dict:
	missing: list = []
	rows = [
		{
			key: cell(row, key)
			for key in (
				"section_name",
				"label_en",
				"label_es",
				"sequence",
				"required",
				"data_source",
				"description",
				"idx",
			)
		}
		for row in (doc.get("sections") or [])
	]
	sections = []
	for index, row in enumerate(
		sorted(
			rows, key=lambda row: (frappe.utils.cint(row.get("sequence")), frappe.utils.cint(row.get("idx")))
		),
		start=1,
	):
		where = f"sections[{row.get('idx') or index}]"
		sections.append(
			{
				"section_name": row.get("section_name"),
				"label": _label(row, "section_name", language, missing, where),
				"label_en": row.get("label_en") or row.get("section_name"),
				"label_es": row.get("label_es") or None,
				"sequence": frappe.utils.cint(row.get("sequence")),
				"required": bool(frappe.utils.cint(row.get("required"))),
				"data_source": row.get("data_source") or None,
				"description": row.get("description") or None,
			}
		)
	data = {
		"name": doc.name,
		"template_name": doc.template_name,
		"company": doc.company,
		"report_type": doc.report_type,
		"enabled": bool(frappe.utils.cint(doc.enabled)),
		"label": _label(doc.as_dict(), "template_name", language, missing, "template"),
		"label_en": doc.label_en or doc.template_name,
		"label_es": doc.label_es or None,
		"regulatory_basis": doc.regulatory_basis or None,
		"description_en": doc.description_en or None,
		"description_es": doc.description_es or None,
		"shipped_default": bool(frappe.utils.cint(doc.shipped_default)),
		"notes": doc.notes or None,
		"sections": sections,
		"section_count": len(sections),
		"required_section_count": len([row for row in sections if row["required"]]),
		"sections_without_source": [row["section_name"] for row in sections if not row["data_source"]],
	}
	if missing:
		data["untranslated"] = missing
		data["language_note"] = (
			f"{len(missing)} label(s) have no Spanish and were served in English. Reported rather "
			"than hidden: silently serving English means nobody finds out until somebody who "
			"needed the Spanish did not get it."
		)
	return data


def _sections_from(args: dict) -> list:
	"""The `sections` argument, validated into rows. Refuses a shape it cannot store."""
	raw = args.get("sections")
	if raw is None:
		return []
	if not isinstance(raw, list):
		raise ToolError(
			f"sections must be a list, got {type(raw).__name__}. Each entry is either a section "
			'name, or an object like {"section_name": "Liquidity and Capital Resources", '
			'"data_source": "get_cash_flow_summary", "required": true, "sequence": 20}.'
		)
	rows = []
	for index, entry in enumerate(raw, start=1):
		if isinstance(entry, str):
			rows.append({"section_name": entry.strip(), "sequence": index * 10, "required": 1})
			continue
		if not isinstance(entry, dict):
			raise ToolError(
				f"sections[{index}] is a {type(entry).__name__}; it must be a string or an object."
			)
		name = str(entry.get("section_name") or "").strip()
		if not name:
			raise ToolError(f"sections[{index}] has no section_name.")
		# Not `cint(...) or index * 10`: `cint` answers 0 for an absent sequence and for a
		# stated 0 alike, so a caller numbering their first section 0 had it renumbered to
		# 10 and could be reordered behind a later section that stated 5.
		raw_sequence = entry.get("sequence")
		rows.append(
			{
				"section_name": name,
				"label_en": str(entry.get("label_en") or "").strip(),
				"label_es": str(entry.get("label_es") or "").strip(),
				"data_source": str(entry.get("data_source") or "").strip(),
				"description": str(entry.get("description") or "").strip(),
				"sequence": (
					frappe.utils.cint(raw_sequence) if raw_sequence not in (None, "") else index * 10
				),
				"required": 0 if entry.get("required") is False else 1,
			}
		)
	return rows


def create_reporting_template(args: dict) -> ToolResult:
	"""Define the shape of a periodic report: its sections, in order, and where each is filled from."""
	_require(TEMPLATE)
	company = resolve_company(as_str(args, "company"), required=True)
	template_name = as_str(args, "template_name", required=True)
	report_type = as_choice(
		TEMPLATE, "report_type", as_str(args, "report_type", required=True), "report_type"
	)
	sections = _sections_from(args)
	if not sections:
		raise ToolError(
			"A reporting template with no sections is not a shape. Pass `sections` — a list of "
			"names, or of objects naming the tool each is filled from. Nothing was created."
		)

	abbr = frappe.db.get_value("Company", company, "abbr") or ""
	docname = f"{template_name} - {abbr}" if abbr else template_name
	if frappe.db.exists(TEMPLATE, docname):
		raise ToolError(
			f"Reporting Template {docname!r} already exists. Change it with "
			"update_reporting_template, or choose another name. Nothing was created."
		)

	doc = frappe.new_doc(TEMPLATE)
	doc.company = company
	doc.template_name = template_name
	doc.report_type = report_type
	doc.enabled = 0 if as_bool(args, "enabled", default=True) is False else 1
	for field in ("label_en", "label_es", "regulatory_basis", "description_en", "description_es", "notes"):
		value = as_str(args, field)
		if value:
			doc.set(field, value)
	for row in sections:
		doc.append("sections", row)
	doc.insert()

	data = _describe_template(doc, _language(args))
	if data["sections_without_source"]:
		data["next_step"] = (
			f"{len(data['sections_without_source'])} section(s) name no data source. That is a "
			"real answer for a narrative section somebody writes — but where a section IS filled "
			"from a tool on this site, naming it is what makes "
			"generate_quarterly_report_skeleton hand somebody a section that already knows where "
			"its numbers come from."
		)
	return ToolResult(
		data=data,
		summary=f"reporting template {doc.name}: {report_type}, {len(sections)} section(s)",
		docstatus_delta="none → 0 (draft)",
	)


def get_reporting_template(args: dict) -> ToolResult:
	"""One template in full, with its sections in order."""
	_require(TEMPLATE)
	name = as_str(args, "reporting_template", required=True)
	if not frappe.db.exists(TEMPLATE, name):
		raise ToolError(
			f"Reporting Template {name!r} does not exist. list_reporting_templates has the register."
		)
	doc = frappe.get_doc(TEMPLATE, name)
	return ToolResult(data=_describe_template(doc, _language(args)), summary=f"reporting template {name}")


def list_reporting_templates(args: dict) -> ToolResult:
	"""What report shapes this site has, without pulling every section of every one."""
	_require(TEMPLATE)
	company = resolve_company(as_str(args, "company"), required=True)
	filters = {"company": company}
	report_type = as_str(args, "report_type")
	if report_type:
		filters["report_type"] = as_choice(TEMPLATE, "report_type", report_type, "report_type")
	if not as_bool(args, "include_disabled", default=False):
		filters["enabled"] = 1
	limit = as_limit(args)
	language = _language(args)

	rows = frappe.db.get_all(
		TEMPLATE,
		filters=filters,
		fields=compat.existing_fields(TEMPLATE, _TEMPLATE_FIELDS),
		order_by="report_type asc, template_name asc",
		limit=limit + 1,
	)
	missing: list = []
	templates = []
	for row in rows[:limit]:
		row = dict(row)
		templates.append(
			{
				"name": row.get("name"),
				"template_name": row.get("template_name"),
				"report_type": row.get("report_type"),
				"enabled": bool(frappe.utils.cint(row.get("enabled"))),
				"label": _label(row, "template_name", language, missing, str(row.get("name"))),
				"regulatory_basis": row.get("regulatory_basis") or None,
				"section_count": frappe.db.count("Reporting Template Section", {"parent": row.get("name")})
				if compat.doctype_exists("Reporting Template Section")
				else None,
			}
		)
	data = {
		"company": company,
		"count": len(templates),
		"truncated": len(rows) > limit,
		"reporting_templates": templates,
		"language": language,
	}
	if missing:
		data["untranslated"] = missing
	return ToolResult(data=data, summary=f"{len(templates)} reporting template(s) for {company}")


def update_reporting_template(args: dict) -> ToolResult:
	"""Change a template: its labels, its basis, whether it is offered, or its whole section list."""
	_require(TEMPLATE)
	name = as_str(args, "reporting_template", required=True)
	if not frappe.db.exists(TEMPLATE, name):
		raise ToolError(f"Reporting Template {name!r} does not exist. Nothing was changed.")
	doc = frappe.get_doc(TEMPLATE, name)

	changed = []
	report_type = as_str(args, "report_type")
	if report_type:
		doc.report_type = as_choice(TEMPLATE, "report_type", report_type, "report_type")
		changed.append("report_type")
	if "enabled" in args:
		doc.enabled = 1 if as_bool(args, "enabled", default=True) else 0
		changed.append("enabled")
	for field in ("label_en", "label_es", "regulatory_basis", "description_en", "description_es", "notes"):
		if field in args:
			doc.set(field, as_str(args, field) or None)
			changed.append(field)

	if args.get("sections") is not None:
		sections = _sections_from(args)
		if not sections:
			raise ToolError(
				"`sections` was given as an empty list. A template with no sections is not a "
				"shape — to withdraw one, set enabled=false instead. Nothing was changed."
			)
		# RESTATED WHOLE, never merged. A partial section update would need a
		# stable row key, and the only candidate is the section name — which is
		# exactly what somebody renaming a section is changing.
		doc.set("sections", [])
		for row in sections:
			doc.append("sections", row)
		changed.append("sections")

	if not changed:
		raise ToolError(
			"Nothing to change — no field was given. Pass at least one of: report_type, enabled, "
			"label_en, label_es, regulatory_basis, description_en, description_es, sections, notes."
		)
	doc.save()

	data = _describe_template(doc, _language(args))
	data["changed"] = sorted(set(changed))
	if "sections" in changed:
		data["sections_note"] = (
			"The section list was restated whole rather than merged: a partial update would need "
			"a stable row key, and the only candidate is the section name, which is the thing "
			"somebody renaming a section is changing."
		)
	return ToolResult(
		data=data, summary=f"reporting template {name} updated ({', '.join(sorted(set(changed)))})"
	)


# ── the numbers ─────────────────────────────────────────────────────────────
def _try(label: str, unavailable: list, call, *args, **kwargs):
	"""Run one feed source. A source that fails is REPORTED, never fatal.

	See the module docstring: a generator that raises on the first missing input
	is a tool that never works on a farm mid-setup, and is therefore never used.
	"""
	try:
		result = call(*args, **kwargs)
		return result.data if isinstance(result, ToolResult) else result
	except ToolError as exc:
		unavailable.append({"source": label, "reason": str(exc), "kind": "refused"})
	except Exception as exc:  # pragma: no cover - a site missing a doctype entirely
		unavailable.append({"source": label, "reason": f"{type(exc).__name__}: {exc}", "kind": "error"})
	return None


def generate_mda_data_feed(args: dict) -> ToolResult:
	"""The figures a Management Discussion and Analysis is written from, with their windows stated.

	NOT A DRAFT MD&A. It is the evidence pack: KPIs with their trends, cash
	movement, budget variance, the compliance posture, and the operational
	measures that explain a farm's year. Somebody writes the discussion — this
	makes sure the numbers in it came from the ledger.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	to_date = as_date(args, "to_date") or frappe.utils.today()
	from_date = as_date(args, "from_date") or str(frappe.utils.add_days(to_date, -90))
	if from_date > to_date:
		raise ToolError(f"from_date {from_date} is after to_date {to_date}.")

	unavailable: list = []
	feed: dict = {}

	from . import banking_bridge, budget, calendar, kpidefs

	kpis = _try(
		"compute_all_kpis",
		unavailable,
		kpidefs.compute_all_kpis,
		{"company": company, "period_start": from_date, "period_end": to_date},
	)
	if kpis is not None:
		feed["kpis"] = kpis

	cash = _try(
		"get_cash_flow_summary",
		unavailable,
		banking_bridge.get_cash_flow_summary,
		{"company": company, "from_date": from_date, "to_date": to_date},
	)
	if cash is not None:
		feed["cash_flow"] = cash

	variance = _try(
		"get_budget_variance_report",
		unavailable,
		budget.get_budget_variance_report,
		{"company": company, "from_date": from_date, "to_date": to_date},
	)
	if variance is not None:
		feed["budget_variance"] = variance

	readiness = _try("get_audit_readiness", unavailable, calendar.get_audit_readiness, {"company": company})
	if readiness is not None:
		feed["compliance_status"] = readiness

	segments = _try(
		"generate_segment_report",
		unavailable,
		generate_segment_report,
		{"company": company, "from_date": from_date, "to_date": to_date},
	)
	if segments is not None:
		feed["segments"] = {
			"reportable_count": segments.get("reportable_count"),
			"segment_count": segments.get("segment_count"),
			"segments": segments.get("segments"),
			"note": segments.get("reportability_note"),
		}

	related = None
	if compat.doctype_exists("Transfer Pricing Documentation"):
		from . import related_party_controls

		related = _try(
			"generate_related_party_disclosure",
			unavailable,
			related_party_controls.generate_related_party_disclosure,
			{"company": company, "from_date": from_date, "to_date": to_date},
		)
	if related is not None:
		feed["related_party"] = {
			"party_count": related.get("party_count"),
			"total": related.get("total"),
			"undocumented_total": related.get("undocumented_total"),
			"coverage_pct": related.get("coverage_pct"),
		}

	data = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
		"generated_at": frappe.utils.now(),
		"feed": feed,
		"sources_read": sorted(feed),
		"unavailable": unavailable,
		"complete": not unavailable,
		"what_this_is": (
			"The evidence pack an MD&A is written FROM, not a draft of one. Every figure carries "
			"the window it was measured over. Nothing here is filed, stored or attached — it is a "
			"working paper."
		),
		"what_it_could_not_read": (
			f"{len(unavailable)} source(s) could not be read and are named in `unavailable` with "
			"the reason. They are ABSENT rather than zero: a feed that pretended to completeness "
			"would be the dangerous kind."
		)
		if unavailable
		else "Every source was read.",
	}
	return ToolResult(
		data=data,
		summary=(
			f"MD&A data feed for {company}, {from_date} to {to_date}: {len(feed)} source(s) read, "
			f"{len(unavailable)} unavailable"
		),
	)


def _account_types(company: str) -> dict:
	"""{account: root_type}, read once for a whole segment report."""
	rows = frappe.db.get_all(
		"Account",
		filters={"company": company},
		fields=compat.existing_fields("Account", ("name", "root_type", "account_type")),
		limit=5000,
	)
	return {row.get("name"): row.get("root_type") for row in rows}


def generate_segment_report(args: dict) -> ToolResult:
	"""Revenue, expense and result by cost centre, with ASC 280's threshold applied and shown."""
	company = resolve_company(as_str(args, "company"), required=True)
	to_date = as_date(args, "to_date") or frappe.utils.today()
	from_date = as_date(args, "from_date") or str(frappe.utils.add_days(to_date, -365))
	if from_date > to_date:
		raise ToolError(f"from_date {from_date} is after to_date {to_date}.")
	# No `or int(SEGMENT_THRESHOLD * 100)`: `threshold_pct: 0` means "every segment,
	# however small", which is a coherent ask and not the same as not asking.
	threshold = as_int(args, "threshold_pct", default=int(SEGMENT_THRESHOLD * 100))

	root_types = _account_types(company)
	fields = compat.existing_fields(
		"GL Entry",
		("posting_date", "account", "cost_center", "debit", "credit", "is_cancelled", "is_opening"),
	)
	rows = frappe.db.get_all(
		"GL Entry",
		filters={"company": company, "posting_date": ("between", (from_date, to_date)), "is_cancelled": 0},
		fields=fields,
		limit=100000,
	)

	segments: dict = {}
	unassigned = {"revenue": 0.0, "expense": 0.0}
	for row in rows:
		if str(row.get("is_opening") or "").strip().lower() == "yes":
			continue
		root = root_types.get(row.get("account"))
		if root not in ("Income", "Expense"):
			continue
		debit = float(row.get("debit") or 0)
		credit = float(row.get("credit") or 0)
		# Income is credit-natured, expense debit-natured. Both are reported as
		# positive magnitudes, which is how a segment note reads.
		value = (credit - debit) if root == "Income" else (debit - credit)
		cost_center = row.get("cost_center")
		if not cost_center:
			unassigned["revenue" if root == "Income" else "expense"] += value
			continue
		bucket = segments.setdefault(cost_center, {"segment": cost_center, "revenue": 0.0, "expense": 0.0})
		bucket["revenue" if root == "Income" else "expense"] += value

	combined_revenue = sum(bucket["revenue"] for bucket in segments.values()) + unassigned["revenue"]
	# ASC 280 measures the profit test against the GREATER of combined profit of
	# the profitable segments and combined loss of the loss-making ones, which is
	# what stops a business that nets to nothing from having no reportable
	# segments at all.
	profits = [bucket["revenue"] - bucket["expense"] for bucket in segments.values()]
	combined_profit = max(
		sum(value for value in profits if value > 0), abs(sum(value for value in profits if value < 0))
	)

	out = []
	for bucket in segments.values():
		result = bucket["revenue"] - bucket["expense"]
		revenue_share = (bucket["revenue"] / combined_revenue * 100) if combined_revenue else 0.0
		profit_share = (abs(result) / combined_profit * 100) if combined_profit else 0.0
		reasons = []
		if revenue_share >= threshold:
			reasons.append(f"revenue is {revenue_share:.1f}% of combined revenue")
		if profit_share >= threshold:
			reasons.append(f"result is {profit_share:.1f}% of combined profit or loss")
		out.append(
			{
				"segment": bucket["segment"],
				"revenue": round(bucket["revenue"], 2),
				"expense": round(bucket["expense"], 2),
				"result": round(result, 2),
				"margin_pct": round(result / bucket["revenue"] * 100, 1) if bucket["revenue"] else None,
				"revenue_share_pct": round(revenue_share, 1),
				"profit_share_pct": round(profit_share, 1),
				"reportable": bool(reasons),
				"reportable_because": reasons,
			}
		)
	out.sort(key=lambda row: -row["revenue"])

	reportable = [row for row in out if row["reportable"]]
	reportable_revenue = sum(row["revenue"] for row in reportable)
	coverage = (reportable_revenue / combined_revenue) if combined_revenue else 1.0

	data = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
		"segment_basis": "Cost Center",
		"threshold_pct": threshold,
		"segment_count": len(out),
		"reportable_count": len(reportable),
		"segments": out,
		"combined_revenue": round(combined_revenue, 2),
		"combined_expense": round(sum(row["expense"] for row in out) + unassigned["expense"], 2),
		"combined_result": round(
			combined_revenue - (sum(row["expense"] for row in out) + unassigned["expense"]), 2
		),
		"unassigned": {
			"revenue": round(unassigned["revenue"], 2),
			"expense": round(unassigned["expense"], 2),
			"why_it_matters": (
				"Postings with no cost centre belong to no segment and are excluded from every "
				"segment's figures while still counting towards the combined totals — which is "
				"why the segments can sum to less than the company. A large number here means "
				"the segment cut is describing part of the business."
			),
		},
		"reportable_revenue_coverage_pct": round(coverage * 100, 1),
		"meets_75_percent_test": coverage >= SEGMENT_COVERAGE_TARGET,
		"reportability_note": (
			f"ASC 280's quantitative test is applied at {threshold}% and its working is shown per "
			"segment. It is applied, NOT decided: whether these cost centres are genuinely "
			"different operating segments is a judgement about how the business is managed and "
			"how the chief operating decision maker reviews it, which no query can make. Where "
			"the reportable segments carry less than 75% of revenue, ASC 280 asks for more of "
			"them — `meets_75_percent_test` says whether that is the case here."
		),
	}
	if not combined_revenue:
		data["empty_note"] = (
			f"No income or expense postings between {from_date} and {to_date}, so every share is "
			"zero and nothing is reportable. That is an empty window rather than a business with "
			"no segments."
		)
	return ToolResult(
		data=data,
		summary=(
			f"segment report for {company}, {from_date} to {to_date}: {len(out)} segment(s), "
			f"{len(reportable)} reportable"
		),
	)


# ── disclosure checklists ───────────────────────────────────────────────────
def _describe_checklist(doc, language: str = "en") -> dict:
	items = []
	for index, row in enumerate(doc.get("items") or [], start=1):
		name = cell(row, "disclosure_item")
		label_es = cell(row, "label_es")
		status = cell(row, "status")
		label = label_es if (language.startswith("es") and str(label_es or "").strip()) else name
		items.append(
			{
				"idx": cell(row, "idx") or index,
				"disclosure_item": name,
				"label": label,
				"label_es": label_es or None,
				"status": status,
				"required": bool(frappe.utils.cint(cell(row, "required"))),
				"requirement_reference": cell(row, "requirement_reference") or None,
				"assigned_to": cell(row, "assigned_to") or None,
				"completed_by": cell(row, "completed_by") or None,
				"completed_on": str(cell(row, "completed_on") or "") or None,
				"evidence_reference": cell(row, "evidence_reference") or None,
				"notes": cell(row, "notes") or None,
				"settled": status in SETTLED_STATES,
			}
		)
	outstanding_required = [row for row in items if row["required"] and not row["settled"]]
	return {
		"name": doc.name,
		"company": doc.company,
		"filing_type": doc.filing_type,
		"status": doc.status,
		"period_start": str(doc.period_start or "") or None,
		"period_end": str(doc.period_end or "") or None,
		"due_date": str(doc.due_date or "") or None,
		"fiscal_year": doc.fiscal_year or None,
		"reporting_template": doc.reporting_template or None,
		"prepared_by": doc.prepared_by or None,
		"reviewed_by": doc.reviewed_by or None,
		"independently_reviewed": bool(doc.reviewed_by and doc.reviewed_by != doc.prepared_by),
		"filed_on": str(doc.filed_on or "") or None,
		"notes": doc.notes or None,
		"items": items,
		"item_count": len(items),
		"required_count": len([row for row in items if row["required"]]),
		"settled_count": len([row for row in items if row["settled"]]),
		"not_applicable_count": len([row for row in items if row["status"] == NOT_APPLICABLE]),
		"outstanding_required": [row["disclosure_item"] for row in outstanding_required],
		"unassigned_outstanding": [
			row["disclosure_item"] for row in items if not row["settled"] and not row["assigned_to"]
		],
		"complete": not outstanding_required,
		"completion_pct": round(len([row for row in items if row["settled"]]) / len(items) * 100, 1)
		if items
		else 100.0,
	}


def _items_from(args: dict) -> list:
	raw = args.get("items")
	if raw is None:
		return []
	if not isinstance(raw, list):
		raise ToolError(
			f"items must be a list, got {type(raw).__name__}. Each entry is either a disclosure "
			'name, or an object like {"disclosure_item": "Related party transactions", '
			'"requirement_reference": "ASC 850", "required": true, "assigned_to": "cfo@farm"}.'
		)
	rows = []
	for index, entry in enumerate(raw, start=1):
		if isinstance(entry, str):
			rows.append({"disclosure_item": entry.strip(), "status": OUTSTANDING, "required": 1})
			continue
		if not isinstance(entry, dict):
			raise ToolError(f"items[{index}] is a {type(entry).__name__}; it must be a string or an object.")
		name = str(entry.get("disclosure_item") or "").strip()
		if not name:
			raise ToolError(f"items[{index}] has no disclosure_item.")
		rows.append(
			{
				"disclosure_item": name,
				"label_es": str(entry.get("label_es") or "").strip(),
				"status": str(entry.get("status") or OUTSTANDING).strip() or OUTSTANDING,
				"required": 0 if entry.get("required") is False else 1,
				"requirement_reference": str(entry.get("requirement_reference") or "").strip(),
				"assigned_to": str(entry.get("assigned_to") or "").strip(),
				"notes": str(entry.get("notes") or "").strip(),
			}
		)
	return rows


def _completeness_findings(data: dict, company: str, name: str) -> list:
	"""The findings behind the disclosure completeness control, from a described checklist."""
	if not data["outstanding_required"]:
		return []
	return [
		enforcement.Finding(
			control_point=DISCLOSURE_COMPLETENESS,
			message=(
				f"{data['filing_type']} checklist {name} for {data['period_start']} to "
				f"{data['period_end']} is being marked {data['status']} with "
				f"{len(data['outstanding_required'])} required disclosure(s) outstanding: "
				f"{', '.join(data['outstanding_required'][:5])}"
				+ ("…" if len(data["outstanding_required"]) > 5 else "")
				+ "."
			),
			remedy=(
				"Settle each one with complete_disclosure_item — either Complete with its "
				"evidence, or Not Applicable with the reason, which is equally a decision and is "
				"counted as one."
			),
			source_doctype=CHECKLIST,
			source_docname=name,
			company=company,
			detail={
				"outstanding_required": data["outstanding_required"],
				"required_count": data["required_count"],
				"completion_pct": data["completion_pct"],
			},
		)
	]


def create_disclosure_checklist(args: dict) -> ToolResult:
	"""Open the checklist for one filing: which disclosures it must make, and who owes each."""
	_require(CHECKLIST)
	company = resolve_company(as_str(args, "company"), required=True)
	filing_type = as_choice(
		CHECKLIST, "filing_type", as_str(args, "filing_type", required=True), "filing_type"
	)
	period_start = as_date(args, "period_start", required=True)
	period_end = as_date(args, "period_end", required=True)
	if period_end < period_start:
		raise ToolError(
			f"period_end {period_end} is before period_start {period_start}. Nothing was created."
		)
	items = _items_from(args)
	if not items:
		raise ToolError(
			"A disclosure checklist with no items is not a checklist. Pass `items` — a list of "
			"disclosure names, or of objects carrying the requirement reference and who owes "
			"each. Nothing was created."
		)

	template = as_str(args, "reporting_template")
	if template and not frappe.db.exists(TEMPLATE, template):
		raise ToolError(f"Reporting Template {template!r} does not exist. Nothing was created.")

	doc = frappe.new_doc(CHECKLIST)
	doc.company = company
	doc.filing_type = filing_type
	doc.period_start = period_start
	doc.period_end = period_end
	doc.status = as_choice(CHECKLIST, "status", as_str(args, "status") or "Open", "status")
	for field, value in (
		("due_date", as_date(args, "due_date")),
		("fiscal_year", as_str(args, "fiscal_year")),
		("reporting_template", template),
		("prepared_by", as_str(args, "prepared_by") or frappe.session.user),
		("reviewed_by", as_str(args, "reviewed_by")),
		("notes", as_str(args, "notes")),
	):
		if value:
			doc.set(field, value)
	for row in items:
		doc.append("items", row)
	doc.insert()

	data = _describe_checklist(doc, _language(args))
	if data["unassigned_outstanding"]:
		data["next_step"] = (
			f"{len(data['unassigned_outstanding'])} outstanding item(s) are assigned to nobody. "
			"The commonest reason a disclosure is omitted is not that somebody refused it — it "
			"is that nobody owed it."
		)
	return ToolResult(
		data=data,
		summary=f"disclosure checklist {doc.name}: {filing_type}, {len(items)} item(s)",
		docstatus_delta="none → 0 (draft)",
	)


def get_disclosure_checklist(args: dict) -> ToolResult:
	"""One checklist in full, with what enforcement would say about finalising it."""
	_require(CHECKLIST)
	name = as_str(args, "disclosure_checklist", required=True)
	if not frappe.db.exists(CHECKLIST, name):
		raise ToolError(f"Disclosure Checklist {name!r} does not exist.")
	doc = frappe.get_doc(CHECKLIST, name)
	data = _describe_checklist(doc, _language(args))
	data["control"] = enforcement.evaluate(
		DISCLOSURE_COMPLETENESS,
		_completeness_findings(data, doc.company, name),
		company=doc.company,
		raise_on_enforced=False,
	)
	return ToolResult(data=data, summary=f"disclosure checklist {name} ({data['completion_pct']}% settled)")


def list_disclosure_checklists(args: dict) -> ToolResult:
	"""Checklists on file, most recent period first."""
	_require(CHECKLIST)
	company = resolve_company(as_str(args, "company"), required=True)
	filters = {"company": company}
	for key in ("filing_type", "status", "fiscal_year"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	if as_bool(args, "open_only", default=False):
		filters["status"] = ("not in", ("Filed",))
	limit = as_limit(args)

	rows = frappe.db.get_all(
		CHECKLIST,
		filters=filters,
		fields=compat.existing_fields(CHECKLIST, _CHECKLIST_FIELDS),
		order_by="period_end desc",
		limit=limit + 1,
	)
	checklists = []
	for row in rows[:limit]:
		row = dict(row)
		doc = frappe.get_doc(CHECKLIST, row["name"])
		described = _describe_checklist(doc)
		checklists.append(
			{
				"name": described["name"],
				"filing_type": described["filing_type"],
				"status": described["status"],
				"period_start": described["period_start"],
				"period_end": described["period_end"],
				"due_date": described["due_date"],
				"item_count": described["item_count"],
				"required_count": described["required_count"],
				"outstanding_required": described["outstanding_required"],
				"completion_pct": described["completion_pct"],
				"complete": described["complete"],
				"independently_reviewed": described["independently_reviewed"],
			}
		)
	data = {
		"company": company,
		"count": len(checklists),
		"truncated": len(rows) > limit,
		"disclosure_checklists": checklists,
		"incomplete": [row["name"] for row in checklists if not row["complete"]],
		"filed_incomplete": [
			row["name"] for row in checklists if row["status"] == FILED and not row["complete"]
		],
	}
	if data["filed_incomplete"]:
		data["filed_incomplete_note"] = (
			"These were marked Filed with required disclosures still outstanding. Under advisory "
			"that is exactly what is supposed to be visible: it is the list an operation reads "
			"before deciding whether to enforce."
		)
	return ToolResult(data=data, summary=f"{len(checklists)} disclosure checklist(s) for {company}")


def update_disclosure_checklist(args: dict) -> ToolResult:
	"""Change a checklist's status, dates, reviewer or item list. The gate fires on finalising."""
	_require(CHECKLIST)
	name = as_str(args, "disclosure_checklist", required=True)
	if not frappe.db.exists(CHECKLIST, name):
		raise ToolError(f"Disclosure Checklist {name!r} does not exist. Nothing was changed.")
	doc = frappe.get_doc(CHECKLIST, name)

	changed = []
	status = as_str(args, "status")
	new_status = doc.status
	if status:
		new_status = as_choice(CHECKLIST, "status", status, "status")

	# THE GATE IS CONSULTED BEFORE THE SAVE. A checklist being moved to Complete
	# or Filed is making a claim, and the claim is what the control tests — so it
	# is tested against the checklist as it stands, before anything is written.
	if new_status in FINAL_STATES and doc.status not in FINAL_STATES:
		current = _describe_checklist(doc)
		enforcement.evaluate(
			DISCLOSURE_COMPLETENESS,
			_completeness_findings({**current, "status": new_status}, doc.company, name),
			company=doc.company,
		)

	if status:
		doc.status = new_status
		changed.append("status")
	for field, key in (
		("period_start", "period_start"),
		("period_end", "period_end"),
		("due_date", "due_date"),
		("filed_on", "filed_on"),
	):
		value = as_date(args, key)
		if value:
			doc.set(field, value)
			changed.append(field)
	for field in ("fiscal_year", "reporting_template", "prepared_by", "reviewed_by", "notes"):
		if field in args:
			doc.set(field, as_str(args, field) or None)
			changed.append(field)
	if args.get("items") is not None:
		items = _items_from(args)
		if not items:
			raise ToolError(
				"`items` was given as an empty list. A checklist with no items is not a checklist."
			)
		doc.set("items", [])
		for row in items:
			doc.append("items", row)
		changed.append("items")

	if not changed:
		raise ToolError(
			"Nothing to change — no field was given. Pass at least one of: status, period_start, "
			"period_end, due_date, filed_on, fiscal_year, reporting_template, prepared_by, "
			"reviewed_by, items, notes."
		)
	doc.save()

	data = _describe_checklist(doc, _language(args))
	data["changed"] = sorted(set(changed))
	data["control"] = enforcement.evaluate(
		DISCLOSURE_COMPLETENESS,
		_completeness_findings(data, doc.company, name),
		company=doc.company,
		raise_on_enforced=False,
	)
	return ToolResult(
		data=data, summary=f"disclosure checklist {name} updated ({', '.join(sorted(set(changed)))})"
	)


def complete_disclosure_item(args: dict) -> ToolResult:
	"""Settle one disclosure: made, or decided not to apply — both are decisions."""
	_require(CHECKLIST)
	name = as_str(args, "disclosure_checklist", required=True)
	if not frappe.db.exists(CHECKLIST, name):
		raise ToolError(f"Disclosure Checklist {name!r} does not exist. Nothing was changed.")
	item = as_str(args, "disclosure_item", required=True)
	status = as_str(args, "status") or "Complete"
	if status not in ("Complete", NOT_APPLICABLE, "In Progress", OUTSTANDING):
		raise ToolError(
			f"{status!r} is not a disclosure item status. It is one of Complete, Not Applicable, "
			"In Progress or Outstanding."
		)

	doc = frappe.get_doc(CHECKLIST, name)
	target = None
	for row in doc.get("items") or []:
		if str(cell(row, "disclosure_item") or "").strip().casefold() == item.strip().casefold():
			target = row
			break
	if target is None:
		raise ToolError(
			f"{item!r} is not an item on checklist {name}. Its items are: "
			f"{', '.join(str(cell(row, 'disclosure_item')) for row in (doc.get('items') or []))}. "
			"Nothing was changed."
		)

	notes = as_str(args, "notes")
	evidence = as_str(args, "evidence_reference")
	if status == NOT_APPLICABLE and not (notes or cell(target, "notes")):
		raise ToolError(
			"Marking a disclosure Not Applicable without a reason records a decision nobody can "
			"check. The reason IS the disclosure — pass `notes` saying why this does not apply. "
			"Nothing was changed."
		)

	before = cell(target, "status")
	set_cell(target, "status", status)
	if evidence:
		set_cell(target, "evidence_reference", evidence)
	if notes:
		set_cell(target, "notes", notes)
	if status in SETTLED_STATES:
		set_cell(target, "completed_by", as_str(args, "completed_by") or frappe.session.user)
		set_cell(target, "completed_on", frappe.utils.now())
	doc.save()

	data = _describe_checklist(doc, _language(args))
	data["item"] = item
	data["status_change"] = f"{before} → {status}"
	if status == "Complete" and not (evidence or cell(target, "evidence_reference")):
		data["note"] = (
			"No evidence reference was recorded. A completed item that points at nothing is a "
			"tick — name the working paper, the note number, or the tool that produced it."
		)
	if data["complete"]:
		data["all_settled"] = (
			"Every required disclosure on this checklist is now settled. The completeness "
			"control will pass when it is marked Complete or Filed."
		)
	return ToolResult(
		data=data,
		summary=f"disclosure item {item!r} on {name}: {before} → {status}",
	)


def generate_quarterly_report_skeleton(args: dict) -> ToolResult:
	"""Assemble the pieces into a structured report skeleton: sections, numbers, and what is missing.

	THE OUTPUT IS A SKELETON AND SAYS SO. Each section arrives with its heading,
	the tool its numbers come from, and — where that tool could be run here — the
	numbers themselves. What it never contains is prose: nobody's management
	discussion is generated by this app, and a section that arrived pre-written
	would be the one nobody read before it was filed.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	to_date = as_date(args, "to_date") or frappe.utils.today()
	from_date = as_date(args, "from_date") or str(frappe.utils.add_days(to_date, -90))
	if from_date > to_date:
		raise ToolError(f"from_date {from_date} is after to_date {to_date}.")
	language = _language(args)

	template_name = as_str(args, "reporting_template")
	template = None
	if template_name:
		if not frappe.db.exists(TEMPLATE, template_name):
			raise ToolError(f"Reporting Template {template_name!r} does not exist.")
		template = frappe.get_doc(TEMPLATE, template_name)
	elif compat.doctype_exists(TEMPLATE):
		report_type = as_str(args, "report_type") or "Quarterly"
		rows = frappe.db.get_all(
			TEMPLATE,
			filters={"company": company, "report_type": report_type, "enabled": 1},
			fields=["name"],
			order_by="template_name asc",
			limit=1,
		)
		if rows:
			template = frappe.get_doc(TEMPLATE, rows[0]["name"])

	described = _describe_template(template, language) if template else None

	feed = generate_mda_data_feed({"company": company, "from_date": from_date, "to_date": to_date}).data
	numbers = feed.get("feed", {})

	sections = []
	if described:
		for row in described["sections"]:
			source = row["data_source"]
			key = _feed_key(source)
			sections.append(
				{
					"section_name": row["section_name"],
					"heading": row["label"],
					"sequence": row["sequence"],
					"required": row["required"],
					"data_source": source,
					"data": numbers.get(key) if key else None,
					"data_available": bool(key and key in numbers),
					"to_be_written_by_a_person": not (key and key in numbers),
					"description": row["description"],
				}
			)

	checklist_name = as_str(args, "disclosure_checklist")
	checklist = None
	if checklist_name:
		if not frappe.db.exists(CHECKLIST, checklist_name):
			raise ToolError(f"Disclosure Checklist {checklist_name!r} does not exist.")
		checklist = _describe_checklist(frappe.get_doc(CHECKLIST, checklist_name), language)

	gaps = []
	if not described:
		gaps.append(
			"No reporting template — the skeleton has numbers but no structure. Define one with "
			"create_reporting_template."
		)
	if not checklist:
		gaps.append(
			"No disclosure checklist was named, so nothing here says which disclosures this "
			"filing has to make. Open one with create_disclosure_checklist."
		)
	elif checklist["outstanding_required"]:
		gaps.append(
			f"{len(checklist['outstanding_required'])} required disclosure(s) are outstanding on "
			f"checklist {checklist['name']}."
		)
	for entry in feed.get("unavailable", []):
		gaps.append(f"{entry['source']} could not be read: {entry['reason']}")

	data = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
		"generated_at": frappe.utils.now(),
		"language": language,
		"reporting_template": described["name"] if described else None,
		"report_label": described["label"] if described else None,
		"sections": sections,
		"section_count": len(sections),
		"sections_with_data": len([row for row in sections if row["data_available"]]),
		"sections_needing_a_writer": [
			row["section_name"] for row in sections if row["to_be_written_by_a_person"]
		],
		"disclosure_checklist": checklist,
		"data_feed": {"sources_read": feed.get("sources_read"), "unavailable": feed.get("unavailable")},
		"gaps": gaps,
		"ready": not gaps,
		"what_this_is": (
			"A SKELETON: headings, the source behind each, and the figures where this site could "
			"produce them. It contains no prose and never will — a management discussion that "
			"arrived pre-written is the one nobody reads before it is filed. Nothing here is "
			"stored or submitted anywhere."
		),
	}
	return ToolResult(
		data=data,
		summary=(
			f"quarterly report skeleton for {company}, {from_date} to {to_date}: "
			f"{len(sections)} section(s), {len(gaps)} gap(s)"
		),
	)


#: Which MD&A feed key a section's `data_source` corresponds to. A section naming
#: a tool this app cannot run here still arrives in the skeleton — with
#: `to_be_written_by_a_person`, which is the honest label for it.
_FEED_KEYS = {
	"compute_all_kpis": "kpis",
	"compute_kpi": "kpis",
	"get_cash_flow_summary": "cash_flow",
	"get_budget_variance_report": "budget_variance",
	"get_audit_readiness": "compliance_status",
	"generate_segment_report": "segments",
	"generate_related_party_disclosure": "related_party",
}


def _feed_key(data_source: str) -> str:
	return _FEED_KEYS.get(str(data_source or "").strip(), "")


# ── the shipped shapes ──────────────────────────────────────────────────────
#: Three templates every operation heading towards a filing needs, with each
#: section pointing at the tool on this site that fills it. SEEDED, NOT FIXTURED,
#: for the reason `compliance_rules.py` gives at length: a fixture is re-imported
#: by every `bench migrate` with no ability to skip what a site already has, so an
#: operator who reworded a section would have it corrected back on the next
#: upgrade. The seeder writes only where nothing exists.
#:
#: THE SPANISH IS ON THE SECTIONS AND NOT ON THE NUMBERS, which is the whole of
#: what "bilingual where applicable" means here. A section heading is read by a
#: person; a segment table is read in the language of the ledger. Translating
#: half of a financial statement would be worse than translating none of it.
SHIPPED_TEMPLATES = (
	{
		"template_name": "10-K Sections",
		"report_type": "Annual",
		"label_en": "Annual Report — Sections",
		"label_es": "Informe anual — Secciones",
		"regulatory_basis": "SEC Form 10-K",
		"description_en": (
			"The annual report's structure. Shipped as a starting point rather than as a "
			"compliant filing: what a given registrant must include is a question for counsel."
		),
		"description_es": (
			"La estructura del informe anual. Es un punto de partida, no una presentación "
			"conforme: lo que cada empresa debe incluir es una cuestión para su asesor legal."
		),
		"sections": (
			("Business", "Negocio", "", "What the operation does, where, and under what regulation."),
			(
				"Risk Factors",
				"Factores de riesgo",
				"",
				"Weather, water, labour supply, commodity price, concentration.",
			),
			("Properties", "Propiedades", "list_parcels", "Land, plantings, buildings and equipment."),
			("Legal Proceedings", "Procedimientos legales", "", "Written by counsel."),
			(
				"Management's Discussion and Analysis",
				"Análisis y discusión de la gerencia",
				"compute_all_kpis",
				"Results, liquidity and the year's operating story, written from the MD&A feed.",
			),
			(
				"Financial Statements",
				"Estados financieros",
				"get_cash_flow_summary",
				"The statements themselves.",
			),
			(
				"Segment Information",
				"Información por segmento",
				"generate_segment_report",
				"Where the business is run in parts.",
			),
			(
				"Related Party Transactions",
				"Transacciones con partes relacionadas",
				"generate_related_party_disclosure",
				"Dealings with members, managers, family and their entities.",
			),
			(
				"Controls and Procedures",
				"Controles y procedimientos",
				"get_audit_readiness",
				"Management's assertion about internal control, and what it rests on.",
			),
		),
	},
	{
		"template_name": "10-Q Sections",
		"report_type": "Quarterly",
		"label_en": "Quarterly Report — Sections",
		"label_es": "Informe trimestral — Secciones",
		"regulatory_basis": "SEC Form 10-Q",
		"description_en": "The quarterly report's structure — the annual one's shape, at a quarter's depth.",
		"description_es": "La estructura del informe trimestral: la forma del anual, con la profundidad de un trimestre.",
		"sections": (
			("Financial Statements", "Estados financieros", "get_cash_flow_summary", "Condensed, unaudited."),
			(
				"Management's Discussion and Analysis",
				"Análisis y discusión de la gerencia",
				"compute_all_kpis",
				"The quarter against the same quarter last year, and against budget.",
			),
			(
				"Budget Variance",
				"Variación presupuestaria",
				"get_budget_variance_report",
				"Where the quarter went differently from the plan.",
			),
			(
				"Segment Information",
				"Información por segmento",
				"generate_segment_report",
				"The quarter cut by cost centre.",
			),
			(
				"Controls and Procedures",
				"Controles y procedimientos",
				"get_audit_readiness",
				"Any change in internal control during the quarter.",
			),
			(
				"Related Party Transactions",
				"Transacciones con partes relacionadas",
				"generate_related_party_disclosure",
				"Dealings in the quarter, and whether each is documented.",
			),
		),
	},
	{
		"template_name": "MD&A",
		"report_type": "Quarterly",
		"label_en": "Management Discussion and Analysis",
		"label_es": "Análisis y discusión de la gerencia",
		"regulatory_basis": "SEC Regulation S-K Item 303",
		"description_en": (
			"The discussion on its own, for an operation that writes one for a lender or a board "
			"long before it writes one for the SEC."
		),
		"description_es": (
			"La discusión por sí sola, para una operación que la escribe para un prestamista o "
			"un consejo mucho antes de escribirla para la SEC."
		),
		"sections": (
			("Overview", "Resumen", "", "The season in a paragraph."),
			(
				"Results of Operations",
				"Resultados de operación",
				"compute_all_kpis",
				"Revenue, cost and yield against the prior period.",
			),
			(
				"Liquidity and Capital Resources",
				"Liquidez y recursos de capital",
				"get_cash_flow_summary",
				"Cash in, cash out, and what is committed.",
			),
			(
				"Budget to Actual",
				"Presupuesto contra real",
				"get_budget_variance_report",
				"Where the plan and the year parted company.",
			),
			(
				"Critical Accounting Estimates",
				"Estimaciones contables críticas",
				"",
				"Biological assets, depreciation lives, inventory valuation.",
			),
			(
				"Known Trends and Uncertainties",
				"Tendencias e incertidumbres conocidas",
				"",
				"Water, labour, and the price of the crop.",
			),
			(
				"Compliance and Controls",
				"Cumplimiento y controles",
				"get_audit_readiness",
				"What the compliance calendar says as at the period end.",
			),
		),
	},
)


def install_reporting_templates(company: str = "") -> dict:
	"""Seed the three shipped report shapes, per company. Idempotent, and never raises.

	NEVER OVERWRITES. A template an operator has reworded, reordered or disabled
	stays exactly as they left it across every future migrate — the check is "does
	this docname exist", made BEFORE anything is written. That is the whole
	difference between this and a Frappe fixture, and it is why the word
	`fixtures` appears nowhere in this app.

	NEVER RAISES: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	report = {"created": [], "present": [], "failed": []}
	if not compat.doctype_exists(TEMPLATE):
		return report
	try:
		companies = (
			[company] if company else [row["name"] for row in frappe.db.get_all("Company", fields=["name"])]
		)
	except Exception as exc:  # pragma: no cover - a site mid-import
		report["failed"].append({"name": "Company", "reason": f"{type(exc).__name__}: {exc}"})
		return report

	for entity in companies:
		abbr = frappe.db.get_value("Company", entity, "abbr") or ""
		for spec in SHIPPED_TEMPLATES:
			docname = f"{spec['template_name']} - {abbr}" if abbr else spec["template_name"]
			try:
				if frappe.db.exists(TEMPLATE, docname):
					report["present"].append(docname)
					continue
				doc = frappe.new_doc(TEMPLATE)
				doc.company = entity
				doc.template_name = spec["template_name"]
				doc.report_type = spec["report_type"]
				doc.enabled = 1
				doc.shipped_default = 1
				for field in ("label_en", "label_es", "regulatory_basis", "description_en", "description_es"):
					doc.set(field, spec.get(field))
				for index, (name, name_es, source, description) in enumerate(spec["sections"], start=1):
					doc.append(
						"sections",
						{
							"section_name": name,
							"label_en": name,
							"label_es": name_es,
							"data_source": source,
							"description": description,
							"sequence": index * 10,
							"required": 1,
						},
					)
				doc.insert(ignore_permissions=True)
				report["created"].append(docname)
			except Exception as exc:  # pragma: no cover - reported, never raised
				report["failed"].append({"name": docname, "reason": f"{type(exc).__name__}: {exc}"})
	return report
