# SPDX-License-Identifier: MIT
"""Tax form generation tools — the MCP surface over `form_generators.py`.

v0.34.0. The generators are pure; this is the half that goes and gets the
numbers. It reads Farm Payroll Entries and their slips for a period, builds the
employer and recipient blocks, calls the generator, and keeps the whole computed
result on a Tax Form record.

WHY THE COMPUTED VALUES ARE STORED RATHER THAN RECOMPUTED ON READ. A filed form
is a statement about what an employer told an agency, on a date. Payroll gets
corrected — a slip is amended, a shift's hours are fixed, a state config changes
a rate — and a `get_tax_form` that recomputed from today's data would quietly
return a different W-2 than the one in the envelope. So `form_data_json` is
written once at generation and read back verbatim, and `regenerate_tax_form` is
a deliberate, separately-switched act that says so in its result: what changed,
by how much, and which fields moved.

WHICH SLIPS COUNT. Farm Payroll Entries in `Calculated` or `Submitted` status
whose `pay_period_end` falls inside the form's period. `Draft` is excluded
because a draft payroll has not been paid, and `Cancelled` because it was not.
The entry's own two dates are stamped onto every slip on the way through, which
is what lets the quarterly forms bucket by month.

v0.36.0 ADDED THE PAGE. `render_tax_form_pdf` draws the stored values on the
face of the form and attaches the result to `generated_pdf`. It reads
`form_data_json` and never recomputes: a rendering that recalculated would
produce a page that disagrees with the record it claims to render, which is the
whole reason the values are stored in the first place. Rendering is therefore a
read of the arithmetic and a write of one attachment — it moves no status and
changes no figure.

WHAT THIS DOES NOT DO. File anything. Render an official scannable Copy A.
Compute a deposit schedule. `mark_tax_form_filed` records that a human filed it
and what the agency gave back — it is a bookkeeping act, not a transmission.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import now, today

from .. import form_pdf_renderer
from ..args import as_date, as_int, as_str, resolve_company
from ..compat import existing_fields
from ..errors import ToolError
from ..form_generators import (
	FORM_TYPES,
	QUARTERS,
	generate_form_data,
	quarter_period,
	year_period,
)
from ..result import ToolResult
from . import artifacts

TAX_FORM = "Tax Form"
PAYROLL_ENTRY = "Farm Payroll Entry"
EMPLOYEE = "Employee"
COMPANY = "Company"
RELATED_PARTY = "Related Party"
STATE_TAX_CONFIG = "State Tax Configuration"

#: Payroll entry statuses whose slips represent money that was actually paid.
COUNTED_STATUSES = ("Calculated", "Submitted")

#: Which state's slips a form is built from, where the form is a state one.
FORM_STATE = {"OR-WR": "OR", "OQ": "OR", "WA-ESD": "WA"}

#: Status transitions this app will make. Anything else is refused by name.
FILEABLE_STATUSES = ("Generated", "Draft")

#: How many forms one bulk render covers. A year of W-2s for a large crew is
#: the case the default is sized for; the hard maximum matches what
#: `list_tax_forms` will return, so a caller cannot select more than they can
#: see. Exceeding either is refused by name rather than truncated — see
#: `_bulk_selection`.
BULK_RENDER_DEFAULT_LIMIT = 100
BULK_RENDER_MAX_LIMIT = 500


# ── Read tools ────────────────────────────────────────────────────────────


def list_tax_forms(args: dict) -> ToolResult:
	"""List generated tax forms with optional filters."""
	filters = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	form_type = as_str(args, "form_type")
	if form_type:
		filters["form_type"] = _check_form_type(form_type)
	fiscal_year = as_str(args, "fiscal_year") or as_str(args, "year")
	if fiscal_year:
		filters["fiscal_year"] = str(fiscal_year)
	quarter = as_str(args, "quarter")
	if quarter:
		filters["quarter"] = _check_quarter(quarter)
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	employee = as_str(args, "employee")
	if employee:
		filters["employee"] = _resolve_employee({"employee": employee})

	limit = as_int(args, "limit", 100)
	if limit and limit > 500:
		limit = 500

	rows = frappe.db.get_all(
		TAX_FORM,
		filters=filters,
		fields=[
			"name",
			"form_type",
			"company",
			"fiscal_year",
			"quarter",
			"employee",
			"employee_name",
			"related_party",
			"status",
			"period_start",
			"period_end",
			"filed_date",
			"generated_date",
		],
		limit_page_length=limit,
		order_by="fiscal_year desc, quarter asc, form_type asc, modified desc",
	)
	forms = [{k: (str(v) if hasattr(v, "isoformat") else v) for k, v in dict(r).items()} for r in rows]

	by_status = {}
	for row in forms:
		by_status[row["status"]] = by_status.get(row["status"], 0) + 1

	return ToolResult(
		data={"forms": forms, "count": len(forms), "by_status": by_status},
		summary=f"{len(forms)} tax form(s)"
		+ (f" — {', '.join(f'{n} {s}' for s, n in sorted(by_status.items()))}" if by_status else ""),
	)


def get_tax_form(args: dict) -> ToolResult:
	"""One tax form in full, including every computed box and line value."""
	name = _resolve_form(args)
	doc = frappe.get_doc(TAX_FORM, name)

	form_data = _load_form_data(doc.get("form_data_json"))
	warnings = form_data.get("warnings") if isinstance(form_data, dict) else None

	data = {
		"name": doc.name,
		"form_type": doc.form_type,
		"company": doc.company,
		"fiscal_year": doc.fiscal_year,
		"quarter": doc.get("quarter") or None,
		"status": doc.status,
		"employee": doc.get("employee") or None,
		"employee_name": doc.get("employee_name") or None,
		"related_party": doc.get("related_party") or None,
		"period_start": str(doc.get("period_start") or ""),
		"period_end": str(doc.get("period_end") or ""),
		"generated_by": doc.get("generated_by") or None,
		"generated_date": str(doc.get("generated_date") or ""),
		"filed_date": str(doc.get("filed_date") or ""),
		"confirmation_number": doc.get("confirmation_number") or None,
		"amends": doc.get("amends") or None,
		"generated_pdf": doc.get("generated_pdf") or None,
		"notes": doc.get("notes") or None,
		"form_data": form_data,
	}

	summary = f"{doc.form_type} {doc.fiscal_year}"
	if doc.get("quarter"):
		summary += f" {doc.quarter}"
	if doc.get("employee_name"):
		summary += f" for {doc.employee_name}"
	summary += f" — {doc.status}"
	if warnings:
		summary += f", {len(warnings)} warning(s)"

	return ToolResult(data=data, summary=summary)


# ── Mutating tools ────────────────────────────────────────────────────────


def generate_tax_form(args: dict) -> ToolResult:
	"""Compute a tax form from payroll data and record it."""
	form_type = _check_form_type(as_str(args, "form_type", required=True))
	spec = FORM_TYPES[form_type]
	company = resolve_company(as_str(args, "company"), required=True)
	year = _check_year(args)
	quarter = _quarter_for(form_type, args)

	employee = None
	related_party = None
	if spec["scope"] == "employee":
		if form_type == "W-2":
			employee = _resolve_employee(args)
		else:
			related_party = as_str(args, "related_party") or as_str(args, "contractor")
			if not related_party:
				raise ToolError(
					"related_party is required for a 1099-NEC — it names the contractor the form is for."
				)
			if not frappe.db.exists(RELATED_PARTY, related_party):
				raise ToolError(f"no Related Party called {related_party!r} on this site.")

	existing = _find_existing(form_type, company, year, quarter, employee, related_party)
	if existing:
		raise ToolError(
			f"{form_type} for {year}{' ' + quarter if quarter else ''} already exists as "
			f"{existing} — use regenerate_tax_form to recompute it, or mark it Amended "
			f"first if this is a corrected form."
		)

	result = _compute(form_type, company, year, quarter, employee, related_party, args)

	doc = frappe.get_doc(
		{
			"doctype": TAX_FORM,
			"form_type": form_type,
			"company": company,
			"fiscal_year": str(year),
			"quarter": quarter or None,
			"employee": employee or None,
			"related_party": related_party or None,
			"period_start": result["period_start"],
			"period_end": result["period_end"],
			"status": "Generated",
			"form_data_json": json.dumps(result["form_data"], default=str, indent=2),
			"generated_by": _current_user(),
			"generated_date": now(),
			"notes": as_str(args, "notes") or None,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()

	warnings = result["form_data"].get("warnings") or []
	return ToolResult(
		data={
			"name": doc.name,
			"form_type": form_type,
			"company": company,
			"fiscal_year": str(year),
			"quarter": quarter,
			"employee": employee,
			"related_party": related_party,
			"status": "Generated",
			"period_start": result["period_start"],
			"period_end": result["period_end"],
			"slip_count": result["slip_count"],
			"form_data": result["form_data"],
		},
		summary=f"{form_type} {year}{' ' + quarter if quarter else ''} generated as {doc.name} "
		f"from {result['slip_count']} payroll slip(s)"
		+ (f", {len(warnings)} warning(s)" if warnings else ""),
	)


def regenerate_tax_form(args: dict) -> ToolResult:
	"""Recompute an existing tax form from current payroll data.

	Reports what moved. A form that has been Filed is refused unless
	`allow_filed` is passed, because recomputing a filed form silently replaces
	the record of what was actually sent.
	"""
	name = _resolve_form(args)
	doc = frappe.get_doc(TAX_FORM, name)

	if doc.status == "Filed" and not args.get("allow_filed"):
		raise ToolError(
			f"tax form {name} is Filed. Recomputing it would replace the record of "
			f"what was actually sent to the agency. Pass allow_filed=true if the "
			f"intent is to see what a correction would say, or generate an amended "
			f"form instead."
		)
	if doc.status == "Amended":
		raise ToolError(
			f"tax form {name} is Amended — it has already been superseded. "
			f"Regenerate the form that replaced it."
		)

	year = int(str(doc.fiscal_year))
	quarter = doc.get("quarter") or None
	previous = _load_form_data(doc.get("form_data_json"))

	result = _compute(
		doc.form_type,
		doc.company,
		year,
		quarter,
		doc.get("employee"),
		doc.get("related_party"),
		args,
	)
	changes = _diff(previous, result["form_data"])

	frappe.db.set_value(
		TAX_FORM,
		name,
		{
			"form_data_json": json.dumps(result["form_data"], default=str, indent=2),
			"period_start": result["period_start"],
			"period_end": result["period_end"],
			"generated_by": _current_user(),
			"generated_date": now(),
			"status": "Generated" if doc.status == "Draft" else doc.status,
		},
	)

	return ToolResult(
		data={
			"name": name,
			"form_type": doc.form_type,
			"fiscal_year": str(year),
			"quarter": quarter,
			"slip_count": result["slip_count"],
			"changed": bool(changes),
			"changes": changes,
			"form_data": result["form_data"],
		},
		summary=f"{doc.form_type} {name} recomputed from {result['slip_count']} slip(s) — "
		+ (f"{len(changes)} value(s) changed" if changes else "no values changed"),
	)


def mark_tax_form_filed(args: dict) -> ToolResult:
	"""Record that a form was filed with the agency, and when."""
	name = _resolve_form(args)
	status = frappe.db.get_value(TAX_FORM, name, "status")

	if status == "Filed":
		raise ToolError(
			f"tax form {name} is already Filed. Filing it twice would overwrite the "
			f"date and confirmation number of the filing that actually happened."
		)
	if status not in FILEABLE_STATUSES:
		raise ToolError(
			f"tax form {name} is {status!r}. Only {' or '.join(FILEABLE_STATUSES)} forms can be marked Filed."
		)

	filed_date = as_date(args, "filed_date") or today()
	confirmation = as_str(args, "confirmation_number") or as_str(args, "confirmation")
	notes = as_str(args, "notes")

	values = {"status": "Filed", "filed_date": filed_date}
	if confirmation:
		values["confirmation_number"] = confirmation
	if notes:
		values["notes"] = notes
	frappe.db.set_value(TAX_FORM, name, values)

	form_type, year, quarter = frappe.db.get_value(
		TAX_FORM,
		name,
		["form_type", "fiscal_year", "quarter"],
	)

	return ToolResult(
		data={
			"name": name,
			"form_type": form_type,
			"fiscal_year": year,
			"quarter": quarter or None,
			"status": "Filed",
			"filed_date": filed_date,
			"confirmation_number": confirmation or None,
		},
		summary=f"{form_type} {name} marked Filed on {filed_date}"
		+ (f" (confirmation {confirmation})" if confirmation else ""),
		docstatus_delta=f"{status} → Filed",
	)


# ── Mutating tools: the printed page ──────────────────────────────────────


def render_tax_form_pdf(args: dict) -> ToolResult:
	"""Draw a Tax Form's stored values on the face of the form and attach it.

	Reads `form_data_json` and renders it. Nothing is recomputed — see the
	module docstring. The only thing this writes is one private File and the
	`generated_pdf` link to it.
	"""
	form_pdf_renderer.require()
	name = _resolve_form(args)
	doc = frappe.get_doc(TAX_FORM, name)
	overwrite = bool(args.get("overwrite"))

	existing_pdf = str(doc.get("generated_pdf") or "").strip()
	if existing_pdf and not overwrite:
		raise ToolError(
			f"tax form {name} already has a PDF attached at {existing_pdf}. The most "
			f"likely thing in that field is the copy somebody already reviewed — or the "
			f"one the agency issued, which nothing here can reproduce. Pass "
			f"overwrite=true to render a fresh page and repoint the field; the existing "
			f"File stays attached to the record either way. Nothing was changed."
		)

	result = _render(doc, args)
	attachment = artifacts.attach_bytes(
		TAX_FORM,
		name,
		result["file_name"],
		result["pdf"],
		field="generated_pdf",
	)

	data = {
		"name": name,
		"form_type": doc.form_type,
		"company": doc.company,
		"fiscal_year": str(doc.fiscal_year),
		"quarter": doc.get("quarter") or None,
		"status": doc.status,
		"subject": result["subject"] or None,
		"warning_count": result["warning_count"],
		"replaced": existing_pdf or None,
		"attachment": artifacts.describe_attachment(attachment, result["pdf"]),
		"note": _RENDER_NOTE,
	}
	return ToolResult(
		data=data,
		summary=f"{doc.form_type} {name} rendered to {result['file_name']} "
		f"({len(result['pdf']):,} bytes) and attached"
		+ (f", replacing {existing_pdf}" if existing_pdf else "")
		+ (f" — {result['warning_count']} note(s) printed on it" if result["warning_count"] else ""),
	)


def bulk_render_tax_form_pdfs(args: dict) -> ToolResult:
	"""Render PDFs for a set of Tax Forms — every W-2 for a year, say.

	Selection is the same filters `list_tax_forms` takes, or an explicit
	`names` list. A form that already has a PDF is SKIPPED rather than refused,
	because one already-rendered form in a batch of ninety should not stop the
	other eighty-nine; `overwrite` renders it anyway. A form that fails to
	render is recorded by name with its reason and the run continues, so the
	result says which ones came out and which did not.
	"""
	form_pdf_renderer.require()
	overwrite = bool(args.get("overwrite"))
	names, selection = _bulk_selection(args)

	rendered = []
	skipped = []
	failed = []
	for name in names:
		doc = frappe.get_doc(TAX_FORM, name)
		existing_pdf = str(doc.get("generated_pdf") or "").strip()
		if existing_pdf and not overwrite:
			skipped.append(
				{
					"name": name,
					"form_type": doc.form_type,
					"reason": f"already has a PDF at {existing_pdf}; pass overwrite=true to replace it",
				}
			)
			continue
		try:
			result = _render(doc, args)
			attachment = artifacts.attach_bytes(
				TAX_FORM,
				name,
				result["file_name"],
				result["pdf"],
				field="generated_pdf",
			)
		except Exception as error:
			failed.append(
				{
					"name": name,
					"form_type": doc.form_type,
					"reason": str(error) or type(error).__name__,
				}
			)
			continue
		rendered.append(
			{
				"name": name,
				"form_type": doc.form_type,
				"subject": result["subject"] or None,
				"warning_count": result["warning_count"],
				"replaced": existing_pdf or None,
				"attachment": artifacts.describe_attachment(attachment, result["pdf"]),
			}
		)

	parts = [f"{len(rendered)} rendered"]
	if skipped:
		parts.append(f"{len(skipped)} skipped")
	if failed:
		parts.append(f"{len(failed)} failed")

	return ToolResult(
		data={
			"selection": selection,
			"matched": len(names),
			"rendered": rendered,
			"rendered_count": len(rendered),
			"skipped": skipped,
			"skipped_count": len(skipped),
			"failed": failed,
			"failed_count": len(failed),
			"note": _RENDER_NOTE,
		},
		summary=f"{len(names)} tax form(s) selected — " + ", ".join(parts),
	)


#: Said on every render result, because a page that looks like a W-2 is the
#: thing somebody is most likely to mistake for a filing.
_RENDER_NOTE = (
	"Every page is stamped a working copy and says so twice. It is not a filing, it is "
	"not an official scannable form, and rendering it moved no status: the form is still "
	"whatever it was. Use mark_tax_form_filed once a person has actually filed."
)


def _render(doc, args: dict) -> dict:
	"""One form's PDF and the metadata a result carries about it.

	The stored `form_data_json` IS the input. `company_info` and the recipient
	block are read fresh only so an address that reached the site after the form
	was computed can fill a hole the record has — they never displace a figure.
	"""
	form_data = _load_form_data(doc.get("form_data_json"))
	if not form_data:
		raise ToolError(
			f"tax form {doc.name} has no computed values, so there is nothing to draw. "
			f"Run regenerate_tax_form first — a page rendered from an empty record would "
			f"be a form with every box zero and no way to tell that from a real zero."
		)
	if doc.form_type not in form_pdf_renderer.RENDERERS:
		raise ToolError(
			f"no PDF layout for form type {doc.form_type!r}. Known forms: "
			f"{', '.join(sorted(form_pdf_renderer.RENDERERS))}."
		)

	company_info = _company_info(doc.company, args)
	subject_info: dict = {}
	subject = ""
	if doc.form_type == "W-2" and doc.get("employee"):
		subject = str(doc.employee)
		subject_info = _employee_info(subject)
	elif doc.form_type == "1099-NEC" and doc.get("related_party"):
		subject = str(doc.related_party)
		subject_info = _related_party_info(subject)

	pdf = form_pdf_renderer.render_form_pdf(
		doc.form_type,
		form_data,
		company_info,
		subject_info,
	)
	if not pdf.startswith(b"%PDF"):  # pragma: no cover - defensive
		raise ToolError(f"the renderer produced {len(pdf)} byte(s) that are not a PDF. Nothing was attached.")

	return {
		"pdf": pdf,
		"file_name": form_pdf_renderer.file_name_for(doc.form_type, form_data, subject),
		"subject": subject,
		"warning_count": len(form_data.get("warnings") or []),
	}


def _bulk_selection(args: dict) -> tuple[list[str], dict]:
	"""Which forms a bulk run covers, and the filters that chose them.

	A selection that matches nothing is an error rather than an empty success:
	the likeliest cause is a filter that named a year or a quarter this site has
	no forms for, and a run that reports "0 rendered" reads like "nothing needed
	rendering".
	"""
	explicit = args.get("names") or args.get("tax_forms")
	if isinstance(explicit, str):
		explicit = [part.strip() for part in explicit.split(",") if part.strip()]
	if explicit:
		names = [str(item) for item in explicit]
		missing = [name for name in names if not frappe.db.exists(TAX_FORM, name)]
		if missing:
			raise ToolError(
				f"no Tax Form called {', '.join(repr(name) for name in missing)}. Nothing was rendered."
			)
		return names, {"names": names}

	filters: dict = {}
	selection: dict = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = selection["company"] = resolve_company(company)
	form_type = as_str(args, "form_type")
	if form_type:
		filters["form_type"] = selection["form_type"] = _check_form_type(form_type)
	fiscal_year = as_str(args, "fiscal_year") or as_str(args, "year")
	if fiscal_year:
		filters["fiscal_year"] = selection["fiscal_year"] = str(fiscal_year)
	quarter = as_str(args, "quarter")
	if quarter:
		filters["quarter"] = selection["quarter"] = _check_quarter(quarter)
	status = as_str(args, "status")
	if status:
		filters["status"] = selection["status"] = status
	employee = as_str(args, "employee")
	if employee:
		filters["employee"] = selection["employee"] = _resolve_employee({"employee": employee})

	if not filters:
		raise ToolError(
			"bulk_render_tax_form_pdfs needs something to select on — names, or at least "
			"one of company, form_type, fiscal_year, quarter, status or employee. "
			"Rendering every form on the site because nobody said which is not a "
			"reasonable default. Nothing was rendered."
		)

	# max(1, ...) not `or BULK_RENDER_DEFAULT_LIMIT`: an explicit 0 is clamped to one
	# form rather than silently reinstated as the default page.
	limit = max(1, min(BULK_RENDER_MAX_LIMIT, as_int(args, "limit", BULK_RENDER_DEFAULT_LIMIT)))
	selection["limit"] = limit

	rows = frappe.db.get_all(
		TAX_FORM,
		filters=filters,
		fields=["name"],
		limit_page_length=limit + 1,
		order_by="fiscal_year asc, quarter asc, form_type asc, name asc",
	)
	if not rows:
		raise ToolError(
			f"no Tax Form matches {selection}. Nothing was rendered — check the year, "
			f"the quarter and the form type with list_tax_forms."
		)
	if len(rows) > limit:
		raise ToolError(
			f"more than {limit} Tax Form(s) match {selection}. A bulk render that "
			f"silently stopped at the limit would look like it had covered everything, "
			f"so this refuses instead. Raise limit (hard maximum "
			f"{BULK_RENDER_MAX_LIMIT}) or narrow the filter. Nothing was rendered."
		)
	return [str(row["name"]) for row in rows], selection


# ── Internal: computation ─────────────────────────────────────────────────


def _compute(form_type, company, year, quarter, employee, related_party, args) -> dict:
	"""Load everything a form needs and run the generator. The shared half."""
	if quarter:
		period_start, period_end = quarter_period(quarter, year)
	else:
		period_start, period_end = year_period(year)

	company_info = _company_info(company, args)
	state = FORM_STATE.get(form_type)

	slips: list[dict] = []
	payments: list[dict] = []
	subject_info: dict = {}

	if form_type == "1099-NEC":
		subject_info = _related_party_info(related_party)
		payments = _load_contractor_payments(related_party, period_start, period_end)
	else:
		slips = _load_slips(company, period_start, period_end, employee=employee, state=state)
		if employee:
			subject_info = _employee_info(employee)
			company_info["ss_wage_base"] = company_info.get("ss_wage_base") or _ss_wage_base()
		if form_type in ("941",):
			company_info["ss_wage_base"] = company_info.get("ss_wage_base") or _ss_wage_base()
		if form_type == "WA-ESD":
			company_info["ssn_last4_by_employee"] = _ssn_last4_map(
				{s.get("employee") for s in slips if s.get("employee")}
			)

	form_data = generate_form_data(
		form_type,
		slips,
		company_info,
		year,
		quarter=quarter,
		subject_info=subject_info,
		payments=payments,
	)

	return {
		"form_data": form_data,
		"period_start": period_start,
		"period_end": period_end,
		"slip_count": len(payments) if form_type == "1099-NEC" else len(slips),
	}


def _load_slips(
	company: str,
	period_start: str,
	period_end: str,
	employee: str | None = None,
	state: str | None = None,
	extra_fields: tuple = (),
) -> list[dict]:
	"""Every paid payroll slip in a period, with its entry's dates stamped on.

	Only `Calculated` and `Submitted` entries — a Draft payroll has not been paid
	and a Cancelled one was not.

	`extra_fields` NAMES SLIP COLUMNS THE FORM GENERATORS DO NOT WANT. v0.92.0
	added it for the remittance tools, which need the EMPLOYER halves — `futa`,
	`state_unemployment`, `social_security_employer` — because a deposit is the
	employer's share and the employee's together, while a W-2 is only ever the
	employee's. Every one is read as a float and defaults to zero, so a slip
	written before the column existed reports nothing rather than raising.
	"""
	entries = frappe.db.get_all(
		PAYROLL_ENTRY,
		filters={
			"company": company,
			"status": ("in", COUNTED_STATUSES),
			"pay_period_end": (">=", period_start),
		},
		fields=["name", "pay_period_start", "pay_period_end", "pay_frequency"],
		order_by="pay_period_end asc",
	)

	slips = []
	for entry in entries:
		# The two-sided date range is applied here rather than in the filter
		# because the same key twice in a Frappe filter dict keeps only the
		# second. `payroll._load_shifts` carried exactly that bug until v0.35.0,
		# where the fix was to pass a LIST of conditions instead of a dict.
		entry_end = str(entry.get("pay_period_end") or "")
		if not entry_end or entry_end > period_end:
			continue

		try:
			doc = frappe.get_doc(PAYROLL_ENTRY, entry["name"])
		except Exception:
			continue

		for row in doc.get("slips") or []:
			get = row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
			if employee and get("employee") != employee:
				continue

			detail = _load_form_data(get("state_taxes_detail"))
			slip = {
				"employee": get("employee"),
				"employee_name": get("employee_name"),
				"work_state": get("work_state") or "",
				"total_hours": float(get("total_hours") or 0),
				"gross_pay": float(get("gross_pay") or 0),
				"federal_withholding": float(get("federal_withholding") or 0),
				"state_withholding": float(get("state_withholding") or 0),
				"social_security": float(get("social_security") or 0),
				"medicare": float(get("medicare") or 0),
				"additional_medicare": float(get("additional_medicare") or 0),
				# v0.92.0. The EMPLOYER half, which no form generator reads and
				# every remittance does. A return reports what was withheld from
				# somebody; a deposit is that plus what the employer owes beside
				# it, and `tools/tax_remittance.py` cannot answer "what do I pay"
				# without these five. They are loaded here rather than by a second
				# loader of its own so that a figure on a 941 and the deposit that
				# settles it can never come from two different readings of the
				# same slip.
				"social_security_employer": float(get("social_security_employer") or 0),
				"medicare_employer": float(get("medicare_employer") or 0),
				"futa": float(get("futa") or 0),
				"state_unemployment": float(get("state_unemployment") or 0),
				"state_employer_other": float(get("state_employer_other") or 0),
				"state_taxes_detail": detail if isinstance(detail, dict) else {},
				"period_start": str(entry.get("pay_period_start") or ""),
				"period_end": entry_end,
				"payroll_entry": entry["name"],
			}
			for extra in extra_fields:
				try:
					slip[extra] = float(get(extra) or 0)
				except (TypeError, ValueError):
					slip[extra] = 0.0
			if state and not _slip_has_state(slip, state):
				continue
			slips.append(slip)

	return slips


def _slip_has_state(slip: dict, state: str) -> bool:
	"""Whether a slip has anything to say about a state.

	A slip whose primary work_state is the other one can still carry that
	state's taxes — a cross-state pay period runs both engines — so the detail
	is checked as well as the label.
	"""
	if slip.get("work_state") == state:
		return True
	return bool(slip.get("state_taxes_detail", {}).get(state))


def _load_contractor_payments(related_party: str, period_start: str, period_end: str) -> list[dict]:
	"""Payments to one contractor in a period, from the ledger.

	GL Entry rather than Journal Entry Account for the reason `tools/tax.py`
	spells out: it sees every voucher type that reaches the ledger, and only for
	submitted documents.
	"""
	try:
		rows = frappe.db.get_all(
			"GL Entry",
			filters={
				"party_type": "Related Party",
				"party": related_party,
				"posting_date": ("between", [period_start, period_end]),
				"is_cancelled": 0,
			},
			fields=["posting_date", "debit", "credit", "account", "voucher_no", "voucher_type"],
			order_by="posting_date asc",
		)
	except Exception:
		return []

	payments = []
	for row in rows:
		amount = float(row.get("debit") or 0) - float(row.get("credit") or 0)
		if amount <= 0:
			continue
		payments.append(
			{
				"amount": round(amount, 2),
				"date": str(row.get("posting_date") or ""),
				"account": row.get("account"),
				"reference": row.get("voucher_no"),
				"voucher_type": row.get("voucher_type"),
				"federal_withholding": 0.0,
			}
		)
	return payments


# ── Internal: the blocks a form prints at the top ─────────────────────────


def _company_info(company: str, args: dict) -> dict:
	"""The employer block, plus every rate and base a generator may ask for."""
	fields = existing_fields(COMPANY, ("name", "abbr", "tax_id", "country"))
	row = frappe.db.get_value(COMPANY, company, fields, as_dict=True) or {}

	info = {
		"name": company,
		"ein": str(row.get("tax_id") or "").strip(),
		"address": as_str(args, "company_address") or as_str(args, "employer_address"),
		"state_ids": _state_ids(company, args),
	}

	for key in (
		"ui_rate",
		"wa_ui_wage_base",
		"or_ui_wage_base",
		"ss_wage_base",
		"deposits",
		"sick_pay_adjustment",
		"section_3121q_notice",
		"tips_and_group_term_life_adjustment",
		"small_business_payroll_tax_credit",
		"nec_threshold",
	):
		if args.get(key) not in (None, ""):
			try:
				info[key] = float(args[key])
			except (TypeError, ValueError):
				raise ToolError(f"{key} must be a number, got {args[key]!r}.") from None

	for key in ("ytd_wages_by_employee", "oq_reported"):
		value = args.get(key)
		if isinstance(value, str) and value.strip():
			value = _load_form_data(value)
		if isinstance(value, dict):
			info[key] = value

	return info


def _state_ids(company: str, args: dict) -> dict:
	"""Each state's employer account number — Oregon's BIN, Washington's ESD number.

	Off the State Tax Configuration, which is where an operator sets it once per
	state and never thinks about it again. A `state_ids` argument overrides,
	because a company filing under a second account for one quarter should not
	have to edit the configuration to say so.
	"""
	ids = {}
	try:
		rows = frappe.db.get_all(
			STATE_TAX_CONFIG,
			filters={"company": company, "status": "Active"},
			fields=existing_fields(STATE_TAX_CONFIG, ("state", "employer_account_number")),
		)
	except Exception:
		rows = []
	for row in rows:
		state = row.get("state")
		account = row.get("employer_account_number")
		if state and account:
			ids[state] = str(account)

	override = args.get("state_ids")
	if isinstance(override, str) and override.strip():
		override = _load_form_data(override)
	if isinstance(override, dict):
		for state, account in override.items():
			if state and account:
				ids[str(state)] = str(account)
	return ids


def _employee_info(employee: str) -> dict:
	"""The recipient block on a W-2, from wherever this site keeps each piece."""
	fields = existing_fields(EMPLOYEE, ("name", "employee_name", "current_address", "permanent_address"))
	row = frappe.db.get_value(EMPLOYEE, employee, fields, as_dict=True) or {}
	i9 = _i9_row(employee)

	address = row.get("current_address") or row.get("permanent_address") or ""
	if not address and i9:
		parts = [
			i9.get("address_street"),
			i9.get("address_city"),
			" ".join(p for p in (i9.get("address_state"), i9.get("address_zip")) if p),
		]
		address = ", ".join(p for p in parts if p)

	return {
		"employee": employee,
		"employee_name": row.get("employee_name") or employee,
		"address": address,
		"ssn_last4": (i9 or {}).get("ssn_last_four") or "",
	}


def _ssn_last4_map(employees) -> dict:
	return {e: (_i9_row(e) or {}).get("ssn_last_four") or "" for e in employees if e}


def _i9_row(employee: str) -> dict:
	"""The employee's I-9, which is where four SSN digits and an address live.

	FOUR DIGITS, NEVER NINE. The I-9 is the one form on this site that records a
	Social Security number at all, and even there it stores `ssn_last_four`. A
	tax form prints `XXX-XX-1234` and a person completes the rest from the paper
	— see `form_generators._ssn_display`.
	"""
	try:
		if not frappe.db.exists("DocType", "I-9 Form"):
			return {}
		fields = existing_fields(
			"I-9 Form",
			("ssn_last_four", "address_street", "address_city", "address_state", "address_zip"),
		)
		if not fields:
			return {}
		row = frappe.db.get_value(
			"I-9 Form",
			{"employee": employee},
			fields,
			as_dict=True,
		)
		return dict(row) if row else {}
	except Exception:
		return {}


def _related_party_info(related_party: str) -> dict:
	fields = existing_fields(
		RELATED_PARTY,
		("name", "party_name", "party_type", "tax_id_last4", "tax_id_type", "address"),
	)
	row = frappe.db.get_value(RELATED_PARTY, related_party, fields, as_dict=True) or {}
	return {
		"party": related_party,
		"party_name": row.get("party_name") or related_party,
		"party_type": row.get("party_type") or "",
		"tin_last4": row.get("tax_id_last4") or "",
		"address": row.get("address") or "",
	}


def _ss_wage_base() -> float:
	"""The Social Security wage base off FICA Configuration, if it is set there."""
	try:
		value = frappe.db.get_single_value("FICA Configuration", "social_security_wage_base")
	except Exception:
		return 0.0
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


# ── Internal: argument checking and small utilities ───────────────────────


def _check_form_type(value: str) -> str:
	if value not in FORM_TYPES:
		raise ToolError(f"form_type must be one of: {', '.join(sorted(FORM_TYPES))}. Got {value!r}.")
	return value


def _check_quarter(value: str) -> str:
	if value not in QUARTERS:
		raise ToolError(f"quarter must be one of: {', '.join(QUARTERS)}. Got {value!r}.")
	return value


def _check_year(args: dict) -> int:
	raw = args.get("fiscal_year") if args.get("fiscal_year") not in (None, "") else args.get("year")
	if raw in (None, ""):
		raise ToolError("fiscal_year is required — the calendar year the form covers, as YYYY.")
	try:
		year = int(str(raw).strip()[:4])
	except (TypeError, ValueError):
		raise ToolError(f"fiscal_year must be a four-digit year, got {raw!r}.") from None
	if not 1900 < year < 2200:
		raise ToolError(f"fiscal_year must be a four-digit year, got {raw!r}.")
	return year


def _quarter_for(form_type: str, args: dict) -> str | None:
	"""A quarterly form needs one; an annual form must not be given one."""
	quarter = as_str(args, "quarter")
	if FORM_TYPES[form_type]["period"] == "quarter":
		if not quarter:
			raise ToolError(f"quarter is required for a {form_type} — one of {', '.join(QUARTERS)}.")
		return _check_quarter(quarter)
	if quarter:
		raise ToolError(
			f"{form_type} is an annual form and covers the whole of the tax year; drop the quarter argument."
		)
	return None


def _resolve_employee(args: dict) -> str:
	emp = as_str(args, "employee") or as_str(args, "employee_name")
	if not emp:
		raise ToolError("employee is required for a W-2.")
	if frappe.db.exists(EMPLOYEE, emp):
		return emp
	found = frappe.db.get_value(EMPLOYEE, {"employee_name": emp}, "name")
	if found:
		return str(found)
	raise ToolError(f"no Employee called {emp!r} on this site.")


def _resolve_form(args: dict) -> str:
	name = as_str(args, "name") or as_str(args, "tax_form")
	if not name:
		raise ToolError("name (Tax Form docname) is required.")
	if not frappe.db.exists(TAX_FORM, name):
		raise ToolError(f"no Tax Form called {name!r}.")
	return name


def _find_existing(form_type, company, year, quarter, employee, related_party) -> str | None:
	"""An existing form for the same period and subject that is not superseded."""
	filters = {
		"form_type": form_type,
		"company": company,
		"fiscal_year": str(year),
		"status": ("!=", "Amended"),
	}
	if quarter:
		filters["quarter"] = quarter
	if employee:
		filters["employee"] = employee
	if related_party:
		filters["related_party"] = related_party
	found = frappe.db.get_value(TAX_FORM, filters, "name")
	return str(found) if found else None


def _current_user() -> str | None:
	user = getattr(getattr(frappe, "session", None), "user", None)
	return str(user) if user else None


def _load_form_data(raw):
	"""Parse stored JSON, tolerating a dict that was never serialised."""
	if isinstance(raw, dict):
		return raw
	if not raw:
		return {}
	try:
		return json.loads(raw)
	except (json.JSONDecodeError, TypeError, ValueError):
		return {}


def _diff(previous: dict, current: dict) -> dict:
	"""Which top-level numeric values moved between two computations.

	Deliberately shallow and numbers-only. A regeneration's whole purpose is
	answering "did the correction change the form, and by how much" — a
	structural diff of nested warning prose would bury that in noise.
	"""
	changes = {}
	if not isinstance(previous, dict) or not isinstance(current, dict):
		return changes
	for key, new_value in current.items():
		if not isinstance(new_value, (int, float)) or isinstance(new_value, bool):
			continue
		old_value = previous.get(key)
		if not isinstance(old_value, (int, float)) or isinstance(old_value, bool):
			if key in previous:
				continue
			old_value = 0
		if round(float(old_value), 2) != round(float(new_value), 2):
			changes[key] = {
				"was": round(float(old_value), 2),
				"now": round(float(new_value), 2),
				"delta": round(float(new_value) - float(old_value), 2),
			}
	return changes
