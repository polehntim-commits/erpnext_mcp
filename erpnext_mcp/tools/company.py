# SPDX-License-Identifier: MIT
"""Companies, and the party types a family operation actually pays.

WHY A COMPANY TOOL AT ALL. Every other tool in this app takes a company and
none of them could make one, so standing up a second entity meant leaving the
model and clicking through the Desk. For an operation whose structure is a
holding company, an operating company and a trust, "add the opco" is not an
administrative afterthought — it is the step everything else waits on.

WHAT `create_company` DOES NOT DO. It does not choose a chart of accounts beyond
naming one ERPNext already ships, it does not post an opening balance, and it
does not copy anything from an existing company. ERPNext's own Company
controller builds the chart, the root cost centers and the default warehouses on
insert; this tool's job is to hand it a correct set of arguments and then report
what it actually got, which is not always what was asked for.

WHAT `update_company` REFUSES, AND WHY THOSE THREE. The abbreviation, because it
is baked into the docname of every account, cost center and parcel on the books —
renaming it is a migration, not an edit. The currency, once there is a single GL
entry, because every one of those entries was measured in the old one and
changing the label silently restates the lot. The fiscal year start month, once
a fiscal year exists, because a year that changes shape mid-cycle produces two
overlapping periods and no way to say which a posting belongs to.

THE TWO CUSTOM PARTY TYPES ARE THE POINT OF THE OTHER HALF OF THIS MODULE.
ERPNext ships Customer, Supplier, Employee and Shareholder. A family operation
pays two kinds of people that fit none of them:

  * `Family` — a brother, a son, a parent. Money moves and it is neither payroll
    nor a purchase. Recording those as Suppliers puts family transfers into
    vendor spend and, worse, into a 1099 pre-fill.
  * `Contact` — the consultant who looks at the orchard twice a year, the
    neighbour who runs a tractor for a weekend. Not a formal Supplier, but paid
    for services, which is exactly the shape the IRS cares about.

They are registered as real Party Type records so a Journal Entry can carry
them, and the 1099 pre-fill treats them differently on purpose: Family is
excluded and says so, Contact is included as BORDERLINE. Neither is silently
dropped. See `tools/tax.py`.
"""

import frappe

from .. import compat
from ..args import as_bool, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

PARTY_TYPE = "Party Type"
CUSTOM_FIELD = "Custom Field"

#: v0.97.0. Who advises this operation on pest management, as a child table on
#: Company rather than a single Link.
#:
#: A LINK WOULD BE WRONG THE FIRST TIME SOMEBODY ANSWERED IT HONESTLY. This site
#: runs apples, pears and cherries; a farm growing pome fruit and stone fruit
#: commonly retains a different consultant for each, and one Link holds whichever
#: was typed last while reading as though it were the whole answer. The table
#: costs one small doctype and does not have to be migrated the first time a
#: second consultant is named.
#:
#: ON COMPANY RATHER THAN A SINGLETON, for the reason the housing-deduction
#: default is on Company: this app is multi-company, and a `"issingle": 1`
#: settings doctype holds one row for the whole site — which would need a
#: per-company child table to be correct, i.e. this table with an extra column.
PEST_PROVIDER_FIELD = "pest_management_providers"
PEST_PROVIDER_DOCTYPE = "Pest Management Provider"

#: The party types this app registers: the account type each settles against, and
#: — the part that is not obvious and that v0.12.1 exists because of — the DocType
#: each one RESOLVES TO.
#:
#: ERPNext does not store a posting's counterparty as free text. `Party Type` names
#: itself `field:party_type`, and that field is a Link to **DocType**. A Journal
#: Entry line then carries `party`, a **Dynamic Link** resolved through
#: `party_type`. So a party type called X is only registrable if there is a DocType
#: called X, and a posting to it is only valid if the party is a record in that
#: DocType. Customer, Supplier, Employee and Shareholder each have one.
#:
#: `Contact` already has one: it is core Frappe, which is why v0.12.0 registered it
#: successfully and then died on `Family` in the same loop. `Family` had none, so
#: this app now ships it.
#:
#: Both are payees: a Family transfer and a Contact invoice are money going out,
#: which makes them Payable in ERPNext's terms.
#:
#: `Family` exists because the alternative — recording a transfer to a brother
#: as a Supplier payment — puts it in vendor spend and in the 1099 pre-fill,
#: both of which are wrong. A family transfer below the gift threshold needs no
#: W-9 and produces no form.
#:
#: `Contact` exists because the alternative is the same mistake in the other
#: direction: leaving an occasional consultant unclassified, where the pre-fill
#: has nothing to go on. A Contact payment IS potentially reportable, which is
#: why it lands as borderline rather than exempt.
CUSTOM_PARTY_TYPES = {
	"Family": {
		"account_type": "Payable",
		"doctype": "Family",
		"ships_with_this_app": True,
		"why": (
			"Family members receiving money who are not Employees, Suppliers, Shareholders or "
			"Customers. Excluded from the 1099 pre-fill: a family transfer below the IRS gift "
			"threshold is not a payment for services and needs no W-9."
		),
	},
	"Contact": {
		"account_type": "Payable",
		"doctype": "Contact",
		"ships_with_this_app": False,
		"why": (
			"Professionals and one-off consultants paid occasionally who are not formal "
			"Suppliers. Surfaced by the 1099 pre-fill as BORDERLINE — a payment for services "
			"is reportable unless the W-9 says otherwise, and nothing on this site knows which."
		),
	},
}

#: Fields worth reporting about a company, filtered against the site's own meta
#: so a version without one of them degrades instead of erroring.
_COMPANY_FIELDS = (
	"name",
	"abbr",
	"default_currency",
	"country",
	"tax_id",
	"parent_company",
	"is_group",
	"chart_of_accounts",
	"cost_center",
	"domain",
	"date_of_establishment",
	"company_logo",
	"creation",
)

#: ERPNext's Company doctype names the fiscal year start month field
#: inconsistently across versions, and on several it has none at all — the
#: Fiscal Year records are the truth. Named here so the fallback is visible.
_FY_START_FIELDS = ("fy_start_date_month", "fiscal_year_start_month")

#: What an abbreviation may be. Two characters because one is not an
#: abbreviation of anything and collides immediately; five because it becomes the
#: tail of every account docname on the books and `"1100 - Cash - CONSTANCY"` is
#: a docname nobody reads twice.
ABBR_MIN, ABBR_MAX = 2, 5

#: The chart ERPNext builds when nobody says otherwise. Numbered on purpose: this
#: app's own tools resolve accounts by number as well as by name, and a chart with
#: no numbers makes `resolve_account("1100")` impossible on a brand-new company.
DEFAULT_CHART = "Standard with Numbers"

MONTHS = (
	"January",
	"February",
	"March",
	"April",
	"May",
	"June",
	"July",
	"August",
	"September",
	"October",
	"November",
	"December",
)


# ── pest management providers ───────────────────────────────────────────────
def ensure_pest_provider_field() -> bool:
	"""Give Company the child table the pest management consultants sit in.

	The ninth place this app installs a Custom Field at migrate time, and it uses
	the same arrangement as the eight before it: created by `install.py` and again
	here on first use, so a bench that pulled the code without running the
	installer answers the first call.

	NEVER RAISES. A site that will not take the field loses the register and keeps
	everything else; the reads report `pest_management_providers_installed: false`
	rather than reporting a company with no consultants, which is a different
	claim.
	"""
	try:
		if compat.has_field("Company", PEST_PROVIDER_FIELD):
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists("Company"):
			return False
		if not compat.doctype_exists(PEST_PROVIDER_DOCTYPE):
			# The child doctype ships with this app. Without it the Table field
			# would render as a form Frappe cannot build, which is worse than the
			# column being absent.
			return False
		if frappe.db.exists(CUSTOM_FIELD, {"dt": "Company", "fieldname": PEST_PROVIDER_FIELD}):
			return True
	except Exception:
		return False

	try:
		doc = frappe.new_doc(CUSTOM_FIELD)
		doc.dt = "Company"
		doc.fieldname = PEST_PROVIDER_FIELD
		doc.label = "Pest Management Providers"
		doc.fieldtype = "Table"
		doc.options = PEST_PROVIDER_DOCTYPE
		doc.module = "ERPNext MCP"
		doc.description = (
			"Who advises this entity on pest management, one row per consultant per "
			"commodity. A row with no commodity covers the whole operation. Left empty, "
			"nobody has answered — which is not the same as the farm having no consultant."
		)
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title=f"erpnext_mcp: could not add {PEST_PROVIDER_FIELD} to Company",
			message=compat.traceback_text(),
		)
		return False

	try:
		frappe.clear_cache(doctype="Company")
	except Exception:
		pass
	return compat.has_field("Company", PEST_PROVIDER_FIELD)


def _pest_providers(company: str) -> dict:
	"""The consultants recorded for one company, read through the parent.

	READ THROUGH THE PARENT AND NOT BY FILTERING THE CHILD DOCTYPE. A child table
	is reached from the document that owns it; querying the child by `parent` is
	the shape that works on a bench and returns nothing under a test double, and
	the difference does not show up until the site.
	"""
	if not compat.has_field("Company", PEST_PROVIDER_FIELD):
		return {
			"pest_management_providers_installed": False,
			"pest_management_providers_note": (
				"Company has no pest_management_providers table on this site, so nobody is "
				"recorded either way. It is a Custom Field this app installs — run `bench "
				"--site <site> migrate`."
			),
		}
	try:
		rows = frappe.get_doc("Company", company).get(PEST_PROVIDER_FIELD) or []
	except Exception:
		rows = []
	providers = [
		{
			"provider": row.get("provider"),
			"commodity": row.get("commodity") or None,
			"commodity_scope": "the whole operation" if not row.get("commodity") else None,
			"service_type": row.get("service_type") or None,
			"license_number": row.get("license_number") or None,
		}
		for row in rows
	]
	return {
		"pest_management_providers_installed": True,
		"pest_management_providers": providers,
	}


def _pest_provider_rows(value) -> list:
	"""Validate a whole providers list before any of it is written.

	THE WHOLE LIST IS CHECKED BEFORE THE FIRST ROW IS KEPT, because this write
	replaces the table wholesale — a half-validated list would leave a company
	with some of its consultants and no way to tell which half was dropped.

	Two rows naming one consultant for one commodity are refused: they are two
	answers to one question and which one a report reads depends on row order.
	"""
	if value in (None, ""):
		return []
	if not isinstance(value, list):
		raise ToolError(
			f"{PEST_PROVIDER_FIELD} must be a list of objects, each with `provider` and "
			"optionally commodity, service_type and license_number. Nothing was changed."
		)
	out, seen = [], set()
	for position, entry in enumerate(value, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"row {position} is not an object. Nothing was changed.")
		unknown = set(entry) - {"provider", "commodity", "service_type", "license_number"}
		if unknown:
			raise ToolError(
				f"row {position} has unrecognised key(s): {', '.join(sorted(unknown))}. A key "
				"silently ignored is a consultant somebody thinks they recorded. Nothing was "
				"changed."
			)
		provider = str(entry.get("provider") or "").strip()
		if not provider:
			raise ToolError(f"row {position} names no provider. Nothing was changed.")
		if compat.doctype_exists("Supplier") and not frappe.db.exists("Supplier", provider):
			raise ToolError(
				f"row {position}: no Supplier called {provider!r} on this site. A consultant is "
				"a Supplier on these books — create them with create_supplier first. Nothing "
				"was changed."
			)
		commodity = str(entry.get("commodity") or "").strip()
		if commodity and compat.doctype_exists("Crop") and not frappe.db.exists("Crop", commodity):
			known = frappe.db.get_all("Crop", pluck="name", limit=25) or []
			raise ToolError(
				f"row {position}: no Crop called {commodity!r} on this site. Known: "
				f"{', '.join(sorted(known)) or '<none>'}. Leave commodity out for a consultant "
				"covering the whole operation. Nothing was changed."
			)
		key = (provider, commodity)
		if key in seen:
			raise ToolError(
				f"row {position} repeats {provider!r} for "
				f"{commodity or 'the whole operation'}. Two rows are two answers to one "
				"question and which one a report reads depends on row order. Nothing was "
				"changed."
			)
		seen.add(key)
		out.append(
			{
				"provider": provider,
				"commodity": commodity,
				"service_type": str(entry.get("service_type") or "").strip(),
				"license_number": str(entry.get("license_number") or "").strip(),
			}
		)
	return out


# ── shared ──────────────────────────────────────────────────────────────────
def _company_row(company: str) -> dict:
	fields = compat.existing_fields("Company", _COMPANY_FIELDS)
	return dict(frappe.db.get_value("Company", company, fields, as_dict=True) or {})


def _tax_id_status(tax_id) -> dict:
	"""Whether a tax id is on file, and its last four — never the whole number.

	The full EIN is on the SS-4 and belongs there. What a caller needs from a
	register is "is this set, and is it the one I am thinking of", and four
	digits answers both without putting a taxpayer id into a model's context.
	"""
	digits = "".join(character for character in str(tax_id or "") if character.isdigit())
	return {
		"tax_id_on_file": bool(digits),
		"tax_id_last4": digits[-4:] if len(digits) >= 4 else "",
	}


def _month_number(value, key: str) -> int:
	"""A month as 1-12, from a number or from its name.

	A model asked for "the fiscal year start month" is as likely to say `"April"`
	as `4`, and refusing one of those is a round trip for nothing.
	"""
	if value is None or value == "":
		return 0
	text = str(value).strip()
	if text.isdigit():
		number = int(text)
		if not 1 <= number <= 12:
			raise ToolError(f"{key} must be 1-12 (1 = January), got {value!r}. Nothing was created.")
		return number
	for index, name in enumerate(MONTHS, start=1):
		if name.lower().startswith(text.lower()) and len(text) >= 3:
			return index
	raise ToolError(
		f"{key} must be a month: 1-12, or a name such as 'April'. Got {value!r}. Nothing was created."
	)


def _fiscal_year_dates(start_month: int, today: str) -> tuple[str, str, str]:
	"""The fiscal year containing `today` for a year starting in `start_month`.

	Returned as `(year_name, start_date, end_date)`. A calendar year is named for
	its year; anything else is named for the span it covers, which is the
	convention ERPNext's own installer uses and the one an accountant reads
	without asking.
	"""
	year = int(str(today)[:4])
	month = int(str(today)[5:7])
	if month < start_month:
		year -= 1
	start = f"{year:04d}-{start_month:02d}-01"
	end_year = year + 1 if start_month > 1 else year
	end_month = start_month - 1 if start_month > 1 else 12
	end = f"{end_year:04d}-{end_month:02d}-{_last_day(end_year, end_month):02d}"
	name = str(year) if start_month == 1 else f"{year}-{year + 1}"
	return name, start, end


def _last_day(year: int, month: int) -> int:
	if month == 2:
		leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
		return 29 if leap else 28
	return 30 if month in (4, 6, 9, 11) else 31


def chart_templates(country: str) -> list | None:
	"""Chart-of-accounts templates this site offers, or None if it cannot say.

	ERPNext keeps these as JSON files rather than as records, so the only honest
	way to enumerate them is its own helper. That helper is an ERPNext internal
	and this app imports nothing from ERPNext anywhere else — so the import is
	guarded, and a version that has moved it gives `None`, meaning "cannot
	check". `None` is not an empty list: refusing every template because the
	lookup failed would be worse than accepting one ERPNext will reject with its
	own message.
	"""
	try:
		from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import (
			get_charts_for_country,
		)
	except Exception:
		return None
	try:
		return list(get_charts_for_country(country) or []) or None
	except Exception:
		return None


def _abbr_collisions(abbr: str) -> dict:
	"""Everything already on this site whose docname ends in `" - <abbr>"`.

	A duplicate `Company.abbr` is the obvious collision and it is not the only
	one. Delete a company in the Desk and ERPNext does not always take its whole
	chart with it; the orphaned accounts keep the suffix, and a new company
	reusing that abbreviation inherits docnames that look like its own and are
	not. So the accounts are checked too, and reported separately, because the
	two need different words.
	"""
	tail = f" - {abbr}"
	out = {"company": frappe.db.get_value("Company", {"abbr": abbr}, "name"), "orphans": []}
	for doctype in ("Account", "Cost Center"):
		if not compat.doctype_exists(doctype):
			continue
		rows = frappe.db.get_all(doctype, filters={"name": ("like", f"%{tail}")}, pluck="name", limit=5)
		out["orphans"].extend(rows or [])
	return out


def _cost_center_tree(company: str) -> list:
	"""The company's cost centers as `{name, is_group, parent}`, parents first."""
	if not compat.doctype_exists("Cost Center"):
		return []
	rows = frappe.db.get_all(
		"Cost Center",
		filters={"company": company},
		fields=compat.existing_fields(
			"Cost Center", ("name", "cost_center_name", "is_group", "parent_cost_center")
		),
		order_by="lft asc",
		limit=200,
	)
	return [
		{
			"name": row.get("name"),
			"cost_center_name": row.get("cost_center_name"),
			"is_group": compat.checked(row.get("is_group")),
			"parent": row.get("parent_cost_center") or None,
		}
		for row in rows or []
	]


def _gl_facts(company: str) -> dict:
	"""How much ledger a company has, which is what decides what may be changed.

	Three cheap queries rather than one big one. Reading every posting date and
	counting the rows in Python is the obvious way to write this and the wrong
	one: `list_companies` calls it once per company, and a real ledger has
	hundreds of thousands of GL entries — so the obvious version pulls the whole
	general ledger into memory to answer "are there any". A COUNT and two
	LIMIT 1 lookups answer the same question in constant space.
	"""
	filters = {"company": company, "is_cancelled": 0}
	count = frappe.db.count("GL Entry", filters)
	if not count:
		return {"gl_entry_count": 0, "first_gl_entry": None, "last_gl_entry": None}

	def edge(direction: str):
		rows = frappe.db.get_all(
			"GL Entry",
			filters=filters,
			fields=["posting_date"],
			order_by=f"posting_date {direction}",
			limit=1,
		)
		return str(rows[0].get("posting_date") or "") or None if rows else None

	return {"gl_entry_count": count, "first_gl_entry": edge("asc"), "last_gl_entry": edge("desc")}


# ── 96. list_companies ──────────────────────────────────────────────────────
def list_companies(args: dict) -> ToolResult:
	"""Every company on the site, with enough about each to tell them apart."""
	limit = as_limit(args)
	names = frappe.db.get_all("Company", pluck="name", order_by="name asc", limit=limit + 1) or []
	truncated = len(names) > limit
	names = names[:limit]

	# Fiscal Year is site-wide on every ERPNext this app supports, not per-company,
	# so it is read once rather than once per company. A version that scopes years
	# to a company would need this moved back inside the loop — the child table
	# `companies` is the tell, and `create_company` already writes it where it exists.
	years = frappe.db.get_all(
		"Fiscal Year",
		fields=compat.existing_fields("Fiscal Year", ("name", "year_start_date", "year_end_date")),
		order_by="year_start_date asc",
		limit=200,
	)

	companies = []
	for name in names:
		row = _company_row(name)
		gl = _gl_facts(name)
		companies.append(
			{
				"company": name,
				"abbr": row.get("abbr") or None,
				"default_currency": row.get("default_currency") or None,
				"country": row.get("country") or None,
				"parent_company": row.get("parent_company") or None,
				"is_group": compat.checked(row.get("is_group")),
				"chart_of_accounts": row.get("chart_of_accounts") or None,
				"default_cost_center": row.get("cost_center") or None,
				**_tax_id_status(row.get("tax_id")),
				"fiscal_year_start_month": _fiscal_year_start_month(row, years),
				"fiscal_year_first": years[0]["name"] if years else None,
				"fiscal_year_last": years[-1]["name"] if years else None,
				"fiscal_year_count": len(years),
				"cost_center_count": frappe.db.count("Cost Center", {"company": name}),
				"account_count": frappe.db.count("Account", {"company": name}),
				**_pest_providers(name),
				**gl,
			}
		)

	data = {
		"company_count": len(companies),
		"companies": companies,
		"truncated": truncated,
		"party_types": _party_type_status(),
	}
	if truncated:
		data["note"] = f"more than {limit} companies on this site; raise limit to see the rest."
	posted = [row for row in companies if row["gl_entry_count"]]
	return ToolResult(
		data=data,
		summary=(
			f"{len(companies)} compan{'y' if len(companies) == 1 else 'ies'}, "
			f"{len(posted)} with posted entries"
		),
	)


def _fiscal_year_start_month(row: dict, years) -> int | None:
	"""The month a fiscal year starts in, from the company or from the years.

	Company carries the field on some ERPNext versions and not others, and even
	where it exists it is what the *installer* was told rather than what the
	Fiscal Year records say. So the records win where there are any, which is
	also the only answer that can be checked against a posting.
	"""
	for field in _FY_START_FIELDS:
		value = row.get(field)
		if value:
			for index, name in enumerate(MONTHS, start=1):
				if str(value).strip().lower().startswith(name.lower()[:3]):
					return index
			if str(value).isdigit():
				return int(value)
	if years:
		start = str(years[-1].get("year_start_date") or "")
		if len(start) >= 7:
			return int(start[5:7])
	return None


def _party_type_status() -> dict:
	"""Which of this app's custom party types are registered on this site.

	Reported by `list_companies` because it is the tool a client calls when it is
	working out what this site can express, and "can I book a Journal Entry line
	to a Family member" is exactly that question.
	"""
	if not compat.doctype_exists(PARTY_TYPE):
		return {"available": False, "registered": [], "missing": sorted(CUSTOM_PARTY_TYPES)}
	registered, missing = [], []
	for name in sorted(CUSTOM_PARTY_TYPES):
		(registered if frappe.db.exists(PARTY_TYPE, name) else missing).append(name)
	out = {
		"available": True,
		"registered": registered,
		"missing": missing,
		"resolves_to_doctype": {
			name: spec.get("doctype") or name for name, spec in sorted(CUSTOM_PARTY_TYPES.items())
		},
	}
	if missing:
		out["hint"] = (
			f"{', '.join(missing)} not registered. They are seeded on install and on every "
			"`bench --site <site> migrate`; run one, or use register_party_types."
		)
	return out


# ── 97. create_company ──────────────────────────────────────────────────────
def create_company(args: dict) -> ToolResult:
	"""Stand up one Company, and report the chart ERPNext actually built for it."""
	company_name = as_str(args, "company_name", required=True)
	abbr = as_str(args, "abbr", required=True).strip()
	country = as_str(args, "country") or "United States"
	currency = (as_str(args, "default_currency") or "USD").upper()
	tax_id = as_str(args, "tax_id")
	parent_company = as_str(args, "parent_company")
	notes = as_str(args, "notes")
	chart = as_str(args, "chart_of_accounts") or DEFAULT_CHART
	start_month = _month_number(args.get("fiscal_year_start_month"), "fiscal_year_start_month") or 1
	dry_run = as_bool(args, "dry_run", False)

	if frappe.db.exists("Company", company_name):
		raise ToolError(f"a Company called {company_name!r} is already on this site. Nothing was created.")
	if not abbr.replace("-", "").replace(" ", "").isalnum():
		raise ToolError(
			f"abbr {abbr!r} has to be letters and digits — it becomes the tail of every account "
			"docname on these books. Nothing was created."
		)
	if not ABBR_MIN <= len(abbr) <= ABBR_MAX:
		raise ToolError(
			f"abbr {abbr!r} is {len(abbr)} character(s); it has to be {ABBR_MIN} to {ABBR_MAX}. "
			"One character is not an abbreviation of anything and collides immediately; past "
			f"{ABBR_MAX} every account docname on these books carries it — '1100 - Cash - {abbr}' "
			"is a name nobody reads twice. Nothing was created."
		)

	collisions = _abbr_collisions(abbr)
	if collisions["company"]:
		raise ToolError(
			f"abbreviation {abbr!r} is already {collisions['company']!r}'s. Every account, cost "
			"center and parcel docname on a company ends in its abbreviation, so two companies "
			"sharing one would make those docnames ambiguous. Nothing was created."
		)
	if collisions["orphans"]:
		raise ToolError(
			f"no company holds the abbreviation {abbr!r}, but {len(collisions['orphans'])} "
			f"docname(s) already end in ' - {abbr}': {', '.join(sorted(collisions['orphans']))}. "
			"That is usually a chart left behind by a company somebody deleted, and a new "
			"company reusing the abbreviation would inherit docnames that look like its own and "
			"are not. Clear those first, or pick another abbreviation. Nothing was created."
		)

	if compat.doctype_exists("Country") and not frappe.db.exists("Country", country):
		raise ToolError(
			f"no Country called {country!r} on this site. ERPNext ships the ISO list, so this is "
			"a spelling rather than a missing record — 'United States', not 'USA'. Nothing was "
			"created."
		)
	if compat.doctype_exists("Currency") and not frappe.db.exists("Currency", currency):
		raise ToolError(f"no Currency called {currency!r} on this site. Nothing was created.")

	available_charts = chart_templates(country)
	if available_charts is not None and chart not in available_charts:
		raise ToolError(
			f"no chart of accounts template called {chart!r} for {country}. This site offers: "
			f"{', '.join(sorted(available_charts))}. A template ERPNext cannot find produces a "
			"company with no accounts at all, which looks like a success and is not. Nothing was "
			"created."
		)
	if parent_company:
		if not frappe.db.exists("Company", parent_company):
			raise ToolError(f"no Company called {parent_company!r} to be the parent. Nothing was created.")
		if not compat.checked(frappe.db.get_value("Company", parent_company, "is_group")):
			raise ToolError(
				f"{parent_company!r} is not a group company, so nothing can consolidate under it. "
				"Tick 'Is Group' on it first. Nothing was created."
			)

	# The year containing today, and the one before it. A company stood up in
	# March is a company whose first task is often entering last year's closing
	# balances, and an opening-balance journal entry with no fiscal year to land
	# in is refused by ERPNext with a message about a period that does not exist.
	# Creating both costs one row and saves that conversation.
	today = frappe.utils.today()
	wanted = [_fiscal_year_dates(start_month, today)]
	previous_anchor = f"{int(str(today)[:4]) - 1}{str(today)[4:]}"
	earlier = _fiscal_year_dates(start_month, previous_anchor)
	if earlier[0] != wanted[0][0]:
		wanted.insert(0, earlier)
	fiscal_years = [
		{
			"name": name,
			"year_start_date": start,
			"year_end_date": end,
			"already_exists": bool(frappe.db.exists("Fiscal Year", name)),
		}
		for name, start, end in wanted
	]

	plan = {
		"company": company_name,
		"abbr": abbr,
		"country": country,
		"default_currency": currency,
		"parent_company": parent_company or None,
		"fiscal_year_start_month": start_month,
		"fiscal_year_start_month_name": MONTHS[start_month - 1],
		"chart_of_accounts": chart,
		"fiscal_years": fiscal_years,
		# The year containing today, kept under its old key so a caller written
		# against v0.12.0 keeps working.
		"fiscal_year": fiscal_years[-1],
		**_tax_id_status(tax_id),
	}
	if dry_run:
		return ToolResult(
			data={**plan, "dry_run": True, "created": False},
			summary=(
				f"dry run: would create {company_name} ({abbr}) and "
				f"{len([year for year in fiscal_years if not year['already_exists']])} fiscal year(s)"
			),
		)

	doc = frappe.new_doc("Company")
	doc.company_name = company_name
	doc.abbr = abbr
	doc.default_currency = currency
	doc.country = country
	if tax_id and compat.has_field("Company", "tax_id"):
		doc.tax_id = tax_id
	if parent_company:
		doc.parent_company = parent_company
	if chart:
		doc.chart_of_accounts = chart
	if notes and compat.has_field("Company", "company_description"):
		doc.company_description = notes
	doc.insert(ignore_permissions=True)

	created_years = []
	for year in fiscal_years:
		if year["already_exists"]:
			continue
		_ensure_fiscal_year(year["name"], year["year_start_date"], year["year_end_date"], company_name)
		created_years.append(year["name"])

	row = _company_row(doc.name)
	tree = _cost_center_tree(doc.name)
	built = {
		"account_count": frappe.db.count("Account", {"company": doc.name}),
		"cost_center_count": len(tree),
		"cost_center_tree": tree,
		"default_cost_center": row.get("cost_center") or None,
		"chart_of_accounts": row.get("chart_of_accounts") or chart,
	}
	warnings = []
	if not built["account_count"]:
		warnings.append(
			"ERPNext built no accounts for this company. That happens when the named chart of "
			"accounts does not exist on this site — import one with import_chart_of_accounts, or "
			"the ledger has nowhere to post."
		)
	if not built["cost_center_count"]:
		warnings.append("No cost centers were created. create_cost_center can add the tree by hand.")
	if notes and not compat.has_field("Company", "company_description"):
		warnings.append("This ERPNext's Company has no description field, so `notes` was not stored.")

	data = {
		**plan,
		"created": True,
		"name": doc.name,
		**built,
		"fiscal_years_created": created_years,
		"next_step": (
			"Point this company's standard account fields at real accounts with "
			"set_company_defaults — default_receivable_account, default_payable_account, "
			"round_off_account and the rest. ERPNext books to those without asking, and a "
			"company whose defaults are empty fails at the first invoice rather than here."
		),
	}
	if warnings:
		data["warnings"] = warnings
	return ToolResult(
		data=data,
		summary=(
			f"created company {doc.name} ({abbr}), {built['account_count']} accounts, "
			f"{built['cost_center_count']} cost centers, fiscal year(s) "
			f"{', '.join(created_years) or 'already present'}"
		),
		docstatus_delta="none → 0 (created)",
	)


def _ensure_fiscal_year(name: str, start: str, end: str, company: str) -> None:
	"""Create the fiscal year the company's first postings will need.

	Wrapped in its own function because ERPNext's Fiscal Year is site-wide rather
	than per-company on every version this app supports — the company link is a
	child table that only some versions have — and a caller reading the tool
	result should not have to know that.
	"""
	doc = frappe.new_doc("Fiscal Year")
	doc.year = name
	doc.year_start_date = start
	doc.year_end_date = end
	if compat.has_field("Fiscal Year", "companies"):
		doc.append("companies", {"company": company})
	doc.insert(ignore_permissions=True)


# ── 98. update_company ──────────────────────────────────────────────────────
def update_company(args: dict) -> ToolResult:
	"""Change a company's country, tax id, description or logo. Refuses the three that re-key books."""
	company = as_str(args, "company", required=True)
	if not frappe.db.exists("Company", company):
		match = frappe.db.get_value("Company", {"abbr": company}, "name")
		if not match:
			raise ToolError(f"no Company called {company!r} on this site. Nothing was changed.")
		company = match

	for forbidden, why in (
		(
			"abbr",
			"the abbreviation is the tail of every account, cost center, parcel and lease "
			"docname on these books. Changing it is a rename of thousands of documents, which "
			"is a migration rather than an edit",
		),
		(
			"company_name",
			"the company name IS the docname, and every document on the site links to it by "
			"that name. Renaming is Frappe's own rename tool, not this",
		),
	):
		if as_str(args, forbidden):
			raise ToolError(f"{forbidden} cannot be changed here: {why}. Nothing was changed.")

	row = _company_row(company)
	gl = _gl_facts(company)
	changes, unchanged = {}, []

	currency = as_str(args, "default_currency").upper()
	if currency:
		if gl["gl_entry_count"]:
			raise ToolError(
				f"{company} has {gl['gl_entry_count']} posted GL entries "
				f"({gl['first_gl_entry']} to {gl['last_gl_entry']}), every one of them measured in "
				f"{row.get('default_currency')}. Relabelling the currency would restate the whole "
				"ledger without touching a single number. Nothing was changed."
			)
		if compat.doctype_exists("Currency") and not frappe.db.exists("Currency", currency):
			raise ToolError(f"no Currency called {currency!r} on this site. Nothing was changed.")
		_stage(changes, unchanged, row, "default_currency", currency)

	start_month = _month_number(args.get("fiscal_year_start_month"), "fiscal_year_start_month")
	if start_month:
		years = frappe.db.count("Fiscal Year")
		if years:
			raise ToolError(
				f"this site already has {years} fiscal year(s). Moving the start month now would "
				"produce two periods claiming the same days, and no way to say which one a "
				"posting belongs to. A change of fiscal year is a short year deliberately "
				"created with create_fiscal_year, not an edit to a company. Nothing was changed."
			)
		raise ToolError(
			"this site has no fiscal years at all, so there is nothing for a start month to "
			"change — create the year you want with create_fiscal_year. Nothing was changed."
		)

	country = as_str(args, "country")
	if country:
		if compat.doctype_exists("Country") and not frappe.db.exists("Country", country):
			raise ToolError(f"no Country called {country!r} on this site. Nothing was changed.")
		_stage(changes, unchanged, row, "country", country)

	if "tax_id" in args:
		if not compat.has_field("Company", "tax_id"):
			raise ToolError("this ERPNext's Company has no tax_id field. Nothing was changed.")
		_stage(changes, unchanged, row, "tax_id", as_str(args, "tax_id"))

	if "notes" in args:
		if not compat.has_field("Company", "company_description"):
			raise ToolError(
				"this ERPNext's Company has no description field to hold notes. Nothing was changed."
			)
		_stage(changes, unchanged, row, "company_description", as_str(args, "notes"))

	if "company_logo" in args:
		if not compat.has_field("Company", "company_logo"):
			raise ToolError("this ERPNext's Company has no company_logo field. Nothing was changed.")
		_stage(changes, unchanged, row, "company_logo", _file_url(as_str(args, "company_logo")))

	# v0.203.0. The rodent bait season (docs/design/rodent_bait_program.md §9):
	# MM-DD, refused when malformed so a typo never silently means March–October.
	for key in ("pest_season_start", "pest_season_end"):
		if key not in args:
			continue
		if not compat.has_field("Company", key):
			raise ToolError(
				f"this site's Company has no {key} column — it is a compliance field this app adds on "
				"`bench migrate`. Nothing was changed."
			)
		value = as_str(args, key)
		if value:
			from .. import rodent_bait

			if not rodent_bait.valid_mmdd(value):
				raise ToolError(f"{key} must be MM-DD, e.g. 03-01, got {value!r}. Nothing was changed.")
			value = rodent_bait._mmdd(value, value)
		_stage(changes, unchanged, row, key, value)

	# The consultants table is REPLACED WHOLESALE when passed, never merged. A
	# merge needs a stable row key and these rows have none a caller can see;
	# omitting the argument leaves the table untouched, and passing an empty list
	# is how a caller genuinely clears it. Validated in full before anything is
	# written — see `_pest_provider_rows`.
	providers = None
	if PEST_PROVIDER_FIELD in args:
		if not ensure_pest_provider_field():
			raise ToolError(
				f"this site's Company has no {PEST_PROVIDER_FIELD} table and it could not be "
				"installed — it is a Custom Field this app adds, so run `bench --site <site> "
				"migrate`. Nothing was changed."
			)
		providers = _pest_provider_rows(args.get(PEST_PROVIDER_FIELD))

	if not changes and not unchanged and providers is None:
		raise ToolError(
			"nothing to change. This tool takes country, tax_id, notes, company_logo, "
			f"{PEST_PROVIDER_FIELD} and — only on a company with no postings — default_currency."
		)

	if changes or providers is not None:
		doc = frappe.get_doc("Company", company)
		for field, (_before, after) in changes.items():
			doc.set(field, after or None)
		if providers is not None:
			before = len(doc.get(PEST_PROVIDER_FIELD) or [])
			doc.set(PEST_PROVIDER_FIELD, [])
			for entry in providers:
				doc.append(PEST_PROVIDER_FIELD, entry)
			changes[PEST_PROVIDER_FIELD] = [f"{before} row(s)", f"{len(providers)} row(s)"]
		doc.save(ignore_permissions=True)

	reported = {
		field: [_redact(field, before), _redact(field, after)] for field, (before, after) in changes.items()
	}
	return ToolResult(
		data={
			"company": company,
			"changed": reported,
			"unchanged": unchanged,
			**_pest_providers(company),
			**_tax_id_status(changes.get("tax_id", (row.get("tax_id"), row.get("tax_id")))[1]),
			**gl,
		},
		summary=(
			f"{company}: {len(changes)} field(s) changed"
			if changes
			else f"{company}: already as asked, nothing changed"
		),
		docstatus_delta="0 → 0 (updated)" if changes else "",
	)


def _stage(changes: dict, unchanged: list, row: dict, field: str, wanted: str) -> None:
	before = str(row.get(field) or "")
	if before == wanted:
		unchanged.append(field)
	else:
		changes[field] = [before, wanted]


def _file_url(value: str) -> str:
	"""A value for an Attach Image field, or a refusal naming the tool that produces one.

	AN ATTACH FIELD HOLDS A URL ON THIS SITE, NOT A PATH ON THE CALLER'S DISK.
	The two look alike enough that `/Users/me/logo.jpg` would be stored without
	complaint and render as a broken image on every badge and print format that
	reads it — a failure nobody sees until something is printed. The site's own
	File records live under /files or /private/files, so anything else is either
	a local path or a bare filename, and both mean the file was never uploaded.
	A full http(s) URL is allowed: ERPNext accepts one, and a logo served from a
	CDN is a deliberate choice rather than a mistake.
	"""
	if not value:
		return ""
	if value.startswith(("/files/", "/private/files/", "http://", "https://")):
		return value
	raise ToolError(
		f"{value!r} is not a file URL on this site. company_logo holds the URL of a file "
		"already uploaded here — '/files/…', '/private/files/…' or a full http(s) URL — not a "
		"path on your own machine. Upload it with attach_file_to_document and pass the file_url "
		"it returns. Nothing was changed."
	)


def _redact(field: str, value):
	"""Never echo a whole taxpayer id back, even one the caller just sent."""
	if field != "tax_id":
		return value
	digits = "".join(character for character in str(value or "") if character.isdigit())
	return f"…{digits[-4:]}" if len(digits) >= 4 else ("" if not digits else "set")


# ── 99. register_party_types ────────────────────────────────────────────────
def register_party_types(args: dict) -> ToolResult:
	"""Make `Family` and `Contact` real Party Types on this site. Idempotent."""
	compat.require_doctype(
		PARTY_TYPE,
		"It is core ERPNext — a site without it is not an ERPNext site.",
	)
	dry_run = as_bool(args, "dry_run", False)

	created, existing, skipped = [], [], {}
	for name, spec in sorted(CUSTOM_PARTY_TYPES.items()):
		if frappe.db.exists(PARTY_TYPE, name):
			existing.append(name)
			continue
		blocker = party_type_blocker(name, spec)
		if blocker:
			skipped[name] = blocker
			continue
		if dry_run:
			created.append(name)
			continue
		doc = frappe.new_doc(PARTY_TYPE)
		doc.party_type = name
		if compat.has_field(PARTY_TYPE, "account_type"):
			doc.account_type = spec["account_type"]
		doc.insert(ignore_permissions=True)
		created.append(name)

	data = {
		"created": created,
		"already_registered": existing,
		"skipped": skipped,
		"dry_run": bool(dry_run),
		"party_types": {
			name: {
				"account_type": spec["account_type"],
				"resolves_to_doctype": spec.get("doctype") or name,
				"why": spec["why"],
			}
			for name, spec in sorted(CUSTOM_PARTY_TYPES.items())
		},
		"note": (
			"Existing rules and Journal Entries using Shareholder, Employee or Supplier are "
			"untouched — this adds party types, it does not reclassify anything. A party type "
			"resolves to a DocType of the same name: a posting's `party` is a Dynamic Link "
			"through its `party_type`, so `party_type='Family'` needs `party` to be a record "
			"on the Family register."
		),
	}
	if skipped:
		data["warning"] = (
			f"{len(skipped)} party type(s) could not be registered: "
			+ "; ".join(f"{name} — {why}" for name, why in sorted(skipped.items()))
			+ ". Nothing else was affected."
		)
	return ToolResult(
		data=data,
		summary=(
			f"party types: {len(created)} {'would be ' if dry_run else ''}registered, "
			f"{len(existing)} already there" + (f", {len(skipped)} skipped" if skipped else "")
		),
		docstatus_delta="none → 0 (created)" if created and not dry_run else "",
	)


def party_type_blocker(name: str, spec: dict) -> str:
	"""Why this party type cannot be registered on this site, or "".

	The whole reason v0.12.1 exists. `Party Type.party_type` is a Link to DocType,
	so registering one whose DocType is absent is not a warning Frappe issues —
	it is a `LinkValidationError` raised from `_validate_links()`, and raised
	inside a patch it aborts the entire `bench migrate`.

	Checked BEFORE the insert rather than caught after it, because an exception
	inside a patch leaves the transaction in a state the next statement cannot
	rely on, and because "we did not try, and here is why" is a better thing for
	a migrate to print than a traceback.
	"""
	target = spec.get("doctype") or name
	if compat.doctype_exists(target):
		return ""
	if spec.get("ships_with_this_app"):
		return (
			f"the {target} DocType ships with erpnext_mcp but is not on this site yet — it is "
			"created by the same `bench migrate` that runs this, so a second run will find it"
		)
	return (
		f"there is no DocType called {target!r} on this site. A Party Type's name has to be a "
		"DocType, because a posting's `party` is a Dynamic Link resolved through its "
		"`party_type`"
	)


def ensure_party_types() -> dict:
	"""Seed the custom party types. Called on install and after every migrate.

	NEVER RAISES, and that is a deliberate change from v0.12.0. This runs from a
	patch and from `after_migrate`; an exception in either aborts `bench migrate`
	for the whole bench, which in v0.12.0 meant a single unregistrable party type
	stopped the settings defaults being seeded and left the operator with a
	traceback instead of an app. A party type that cannot be registered is worth
	reporting; it is not worth taking the migration down over.

	Returns `{"created": [...], "existing": [...], "skipped": {name: why}}`.
	"""
	out = {"created": [], "existing": [], "skipped": {}}
	if not compat.doctype_exists(PARTY_TYPE):
		out["skipped"] = {name: "this site has no Party Type DocType at all" for name in CUSTOM_PARTY_TYPES}
		return out

	for name, spec in sorted(CUSTOM_PARTY_TYPES.items()):
		if frappe.db.exists(PARTY_TYPE, name):
			out["existing"].append(name)
			continue
		blocker = party_type_blocker(name, spec)
		if blocker:
			out["skipped"][name] = blocker
			continue
		doc = frappe.new_doc(PARTY_TYPE)
		doc.party_type = name
		if compat.has_field(PARTY_TYPE, "account_type"):
			doc.account_type = spec["account_type"]
		doc.insert(ignore_permissions=True)
		out["created"].append(name)
	return out
