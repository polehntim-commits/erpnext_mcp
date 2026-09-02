# SPDX-License-Identifier: MIT
"""Assets that are used by more than one part of the business, and financed.

WHAT THIS MODULE ADDS TO ERPNEXT, AND WHAT IT LEAVES ALONE. ERPNext already has
an Asset doctype, an Asset Category, and a depreciation schedule. It does not
have the two things an orchard actually needs:

  * **A cost split.** A tractor is not a Harvest asset or a Perennial Care asset;
    it is 40% one and 60% the other, and its depreciation should land that way
    every period without anyone re-deciding it. ERPNext files an asset under one
    cost center.
  * **Note-tenor discipline.** When an asset is financed, the month the note is
    paid off and the month the asset is fully depreciated should be the same
    month. Nothing in ERPNext enforces that, and the divergence is invisible
    until the last year of the loan, when interest is still being paid on
    something with no book value left.

THE SIDECAR. Both live in an `Asset Cost Profile`, one per Asset, rather than in
custom fields grafted onto Asset. The app manifest promises that installing this
app changes the behaviour of nothing already on the site and that uninstalling it
gives the site back; ten custom fields and two child tables on ERPNext's own
Asset would break both halves of that promise. The Asset this module creates is
an ordinary ERPNext Asset that an operator can open, edit and delete without ever
knowing this app exists.

WHY `calculate_depreciation` IS TURNED OFF ON THE ASSET. This is the single most
important line in the file. ERPNext runs a daily scheduled job that posts
depreciation for every asset with `calculate_depreciation` set, using ERPNext's
own schedule and ERPNext's own single cost center. If this app also posted, the
asset would depreciate twice — silently, monthly, in the background. So an asset
created here has ERPNext's schedule switched off and this app owns the schedule
outright: `run_depreciation_cycle` is the only thing that writes depreciation for
it, and the periods it has already written are recorded on the profile so a
second run cannot repeat one.

DRAFTS, LIKE EVERYTHING ELSE HERE. A depreciation run produces DRAFT Journal
Entries. Nothing in this app posts to the general ledger except
`submit_journal_entry`, behind its own switch, and depreciation is not the place
to make an exception — a catch-up run over eleven months is exactly the kind of
thing a human should read before it moves a balance.
"""

import datetime

import frappe

from .. import compat, kpi
from ..args import (
	as_bool,
	as_date,
	as_float,
	as_int,
	as_str,
	resolve_account,
	resolve_company,
	resolve_cost_center,
)
from ..errors import ToolError
from ..result import ToolResult
from . import mutate

PROFILE = "Asset Cost Profile"

#: Percentage points of slack on the allocation total. Two decimals of Percent
#: means three equal shares are 33.33 + 33.33 + 33.34; a tenth of a point is a
#: typo rather than rounding.
ALLOCATION_TOLERANCE = 0.011

#: Currency rounding used for every computed amount. ERPNext's own precision is
#: configurable, but a depreciation line that does not add up to the period total
#: is worse than one rounded to cents, so the split is reconciled explicitly
#: against this.
MONEY_PLACES = 2

_PROFILE_FIELDS = (
	"name",
	"asset",
	"company",
	"gross_purchase_amount",
	"salvage_value",
	"depreciation_method",
	"useful_life_months",
	"depreciation_frequency_months",
	"depreciation_start_date",
	"depreciation_expense_account",
	"accumulated_depreciation_account",
	"linked_note_doctype",
	"linked_note",
	"note_tenor_months",
	"note_maturity_date",
	"tenor_enforced",
	"notes",
)

#: Fields a note document might carry its tenor in, in the order they are tried.
#: Since v0.8.0 this app ships a `Note Payable` doctype, which records a
#: `maturity_date` and no tenor — but a site may equally have its own custom notes
#: doctype whose author picked one of these names, and asking for all of them
#: costs nothing and saves the caller having to know which.
_NOTE_TENOR_FIELDS = ("tenor_months", "term_months", "number_of_months", "no_of_months")
_NOTE_MATURITY_FIELDS = ("maturity_date", "note_maturity_date", "end_date", "repayment_end_date", "due_date")

#: Doctypes a note reference is looked for in when the caller does not say, most
#: specific first. `Note Payable` is this app's own, so it is tried before a
#: site's custom `Notes Payable` and long before Journal Entry, which is the last
#: resort for a note that was never given a document of its own.
_NOTE_DOCTYPES = ("Note Payable", "Notes Payable", "Loan", "Journal Entry")


# ── dates ───────────────────────────────────────────────────────────────────
def add_months(date, months: int) -> datetime.date:
	"""`date` shifted by whole months, clamped to the end of the target month.

	Hand-rolled rather than `frappe.utils.add_months` on purpose. This module's
	arithmetic decides which period a depreciation entry belongs to, and the app's
	own test double would have to reimplement the framework helper to test it —
	at which point the double, not ERPNext, is what the schedule agrees with. A
	dozen lines of standard-library date maths agree with everybody.

	The clamp is the case that matters: a schedule starting 31 January has its
	second period start on 28 February, not on 3 March.
	"""
	date = frappe.utils.getdate(date)
	total = date.month - 1 + months
	year = date.year + total // 12
	month = total % 12 + 1
	last_day = _days_in_month(year, month)
	return datetime.date(year, month, min(date.day, last_day))


def _days_in_month(year: int, month: int) -> int:
	if month == 12:
		return 31
	return (datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)).day


def months_between(start, end) -> int:
	"""Whole months from `start` to `end`, rounded down, never negative.

	"Whole" means a month that has actually elapsed: 2026-01-31 to 2026-02-28 is
	one month, 2026-01-01 to 2026-01-31 is zero.
	"""
	start = frappe.utils.getdate(start)
	end = frappe.utils.getdate(end)
	if end < start:
		return 0
	months = (end.year - start.year) * 12 + (end.month - start.month)
	if add_months(start, months) > end:
		months -= 1
	return max(0, months)


# ── lookups ─────────────────────────────────────────────────────────────────
def _require_assets() -> None:
	compat.require_doctype(
		"Asset", "It ships with ERPNext's Assets module; enable it if this site hid the module."
	)
	compat.require_doctype(
		PROFILE, "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."
	)


def resolve_asset(asset: str, company: str = "") -> str:
	"""An Asset docname from a docname or from its asset_name."""
	asset = (asset or "").strip()
	if not asset:
		raise ToolError("asset is required (an Asset docname, or the asset_name it was created with)")
	if frappe.db.exists("Asset", asset):
		found = frappe.db.get_value("Asset", asset, ["name", "company"], as_dict=True)
		if company and found and found.get("company") != company:
			raise ToolError(f"Asset {asset!r} belongs to company {found.get('company')!r}, not {company!r}")
		return asset
	filters = {"asset_name": asset}
	if company:
		filters["company"] = company
	matches = frappe.db.get_all("Asset", filters=filters, pluck="name", limit=25)
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ToolError(
			f"{asset!r} matches {len(matches)} assets: {', '.join(sorted(matches)[:10])}. "
			"Pass the Asset docname."
		)
	raise ToolError(f"no Asset named or called {asset!r} on this site.")


def _profile_row(asset_docname: str) -> dict:
	fields = compat.existing_fields(PROFILE, _PROFILE_FIELDS)
	row = frappe.db.get_value(PROFILE, {"asset": asset_docname}, fields, as_dict=True)
	if not row:
		raise ToolError(
			f"Asset {asset_docname} has no Asset Cost Profile, so this app does not know how its "
			"cost is shared out or how it depreciates. Assets created with create_asset get one "
			"automatically; an asset created in the Desk needs one before these tools can act on "
			"it."
		)
	return dict(row)


def _allocation_rows(profile_name: str) -> list[dict]:
	doc = frappe.get_doc(PROFILE, profile_name)
	return [
		{
			"cost_center": row.get("cost_center"),
			"percentage": float(row.get("percentage") or 0),
			"bbch_stage": row.get("bbch_stage") or None,
			"note": row.get("note") or None,
		}
		for row in (doc.get("cost_center_allocation") or [])
	]


def _posting_rows(profile_name: str) -> list[dict]:
	doc = frappe.get_doc(PROFILE, profile_name)
	return [
		{
			"period_index": int(row.get("period_index") or 0),
			"period_start": str(row.get("period_start") or ""),
			"period_end": str(row.get("period_end") or ""),
			"amount": float(row.get("amount") or 0),
			"journal_entry": row.get("journal_entry"),
		}
		for row in (doc.get("depreciation_postings") or [])
	]


def _validated_allocation(raw, company: str) -> list[dict]:
	"""Check a cost_center_allocation argument all the way through before anything is written."""
	if raw in (None, ""):
		return []
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"cost_center_allocation must be a non-empty list of objects, e.g. "
			'[{"cost_center": "Harvest", "percentage": 40}, '
			'{"cost_center": "Perennial Care", "percentage": 60}]'
		)
	out, seen, total = [], set(), 0.0
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"cost_center_allocation[{index}] must be an object, got {type(entry).__name__}")
		unknown = sorted(set(entry) - {"cost_center", "percentage", "bbch_stage", "note"})
		if unknown:
			raise ToolError(
				f"cost_center_allocation[{index}] has unsupported field(s): {', '.join(unknown)}. "
				"Supported: cost_center, percentage, bbch_stage, note."
			)
		cost_center = resolve_cost_center(str(entry.get("cost_center") or ""), company)
		row = frappe.db.get_value(
			"Cost Center",
			cost_center,
			compat.existing_fields("Cost Center", ("name", "is_group", "disabled", "company")),
			as_dict=True,
		)
		if int(row.get("is_group") or 0):
			raise ToolError(
				f"cost_center_allocation[{index}]: {cost_center!r} is a group cost center, and "
				"ERPNext files postings against leaf cost centers only. Nothing was written."
			)
		if int(row.get("disabled") or 0):
			raise ToolError(
				f"cost_center_allocation[{index}]: {cost_center!r} is disabled, so depreciation "
				"could not be filed against it. Nothing was written."
			)
		if cost_center in seen:
			raise ToolError(
				f"cost_center_allocation names {cost_center!r} twice. Combine them into one share."
			)
		seen.add(cost_center)
		percentage = as_float(entry.get("percentage"), f"cost_center_allocation[{index}].percentage")
		if percentage <= 0 or percentage > 100:
			raise ToolError(
				f"cost_center_allocation[{index}].percentage must be above 0 and at most 100, "
				f"got {percentage}"
			)
		total += percentage
		bbch_stage = str(entry.get("bbch_stage") or "").strip()
		if bbch_stage:
			_validate_bbch_stage(bbch_stage, index)
		out.append(
			{
				"cost_center": cost_center,
				"percentage": percentage,
				"bbch_stage": bbch_stage or None,
				"note": str(entry.get("note") or "").strip() or None,
			}
		)
	if abs(total - 100.0) > ALLOCATION_TOLERANCE:
		raise ToolError(
			f"cost_center_allocation totals {round(total, 4)}%, not 100%. An asset allocated to "
			"99% of itself under-depreciates the business by 1% for the rest of its life. "
			"Nothing was written."
		)
	return out


def _validate_bbch_stage(value: str, index: int) -> None:
	"""A bbch_stage is only usable if the site has the dimension it names."""
	field = compat.field_meta("Journal Entry Account", "bbch_stage")
	if field is None:
		raise ToolError(
			f"cost_center_allocation[{index}] sets bbch_stage, but Journal Entry Account has no "
			"bbch_stage field on this site, so the tag would be written to nothing. Create the "
			"dimension first with create_accounting_dimension(dimension_name='BBCH Stage', "
			"create_master_if_missing=true). Nothing was written."
		)
	options = str(field.get("options") or "")
	if str(field.get("fieldtype") or "") == "Link" and options and not frappe.db.exists(options, value):
		raise ToolError(
			f"cost_center_allocation[{index}].bbch_stage is {value!r}, which is not a {options} "
			f"on this site. Add it with create_dimension_value. Nothing was written."
		)


# ── the schedule ────────────────────────────────────────────────────────────
def depreciation_schedule(profile: dict) -> list[dict]:
	"""The whole life of an asset as periods, computed rather than stored.

	Deterministic, and computed from the profile every time rather than read off
	saved rows, so that a catch-up run for eleven missed months produces exactly
	the same eleven amounts it would have produced month by month. The saved rows
	record only *which* periods have been written, never what they should have
	been — a schedule kept in two places is a schedule that eventually disagrees.

	The last period absorbs the rounding, so the asset lands exactly on its
	salvage value and never a cent below it.
	"""
	method = str(profile.get("depreciation_method") or "Straight Line")
	life = int(profile.get("useful_life_months") or 0)
	frequency = max(1, int(profile.get("depreciation_frequency_months") or 1))
	total_periods = max(1, life // frequency)
	gross = float(profile.get("gross_purchase_amount") or 0)
	salvage = float(profile.get("salvage_value") or 0)
	base = round(gross - salvage, MONEY_PLACES)
	start = frappe.utils.getdate(profile.get("depreciation_start_date"))

	if method == "Manual":
		raise ToolError(
			f"Asset {profile.get('asset')} depreciates by the Manual method, so this app "
			"computes nothing for it — its entries are written by hand. Change the method on "
			"the Asset Cost Profile if that is not what was meant."
		)
	if base <= 0:
		raise ToolError(
			f"Asset {profile.get('asset')} has nothing to depreciate: cost {gross} less salvage "
			f"{salvage} is {base}."
		)

	amounts = _period_amounts(method, base, gross, salvage, total_periods, profile)
	schedule = []
	for index in range(1, total_periods + 1):
		period_start = add_months(start, (index - 1) * frequency)
		period_end = add_months(start, index * frequency) - datetime.timedelta(days=1)
		schedule.append(
			{
				"period_index": index,
				"period_start": period_start.isoformat(),
				"period_end": period_end.isoformat(),
				"amount": amounts[index - 1],
			}
		)
	return schedule


def _period_amounts(method, base, gross, salvage, total_periods, profile) -> list[float]:
	"""One amount per period, totalling exactly `base`."""
	if method == "Straight Line":
		per_period = round(base / total_periods, MONEY_PLACES)
		amounts = [per_period] * total_periods
	elif method in ("Written Down Value", "Double Declining Balance"):
		rate = _declining_rate(method, gross, salvage, total_periods, profile)
		amounts, book = [], gross
		for _index in range(total_periods):
			# Never below salvage: a declining balance that ran past it would make
			# the last period a credit, which is not depreciation.
			amount = round(max(0.0, min(book * rate, book - salvage)), MONEY_PLACES)
			amounts.append(amount)
			book = round(book - amount, MONEY_PLACES)
	else:  # pragma: no cover - the Select has no other option
		raise ToolError(f"unknown depreciation_method {method!r}")

	# The final period takes whatever rounding left behind, so the asset lands on
	# its salvage value to the cent instead of a few cents either side of it.
	drift = round(base - sum(amounts), MONEY_PLACES)
	amounts[-1] = round(amounts[-1] + drift, MONEY_PLACES)
	if amounts[-1] < 0:
		raise ToolError(
			f"the computed schedule for {profile.get('asset')} over-depreciates by "
			f"{abs(amounts[-1])}; check the salvage value and useful life."
		)
	return amounts


def _declining_rate(method, gross, salvage, total_periods, profile) -> float:
	if method == "Double Declining Balance":
		return 2.0 / total_periods
	# Written Down Value. The textbook rate, 1 - (salvage/cost)^(1/n), is
	# undefined when salvage is zero — a declining balance never reaches nought —
	# so this refuses rather than inventing a floor and calling it WDV.
	if salvage <= 0:
		raise ToolError(
			f"Asset {profile.get('asset')} uses Written Down Value with a salvage value of 0. "
			"A declining balance never reaches zero, so the rate 1 - (salvage/cost)^(1/n) is "
			"undefined. Set a salvage value, or use Straight Line or Double Declining Balance."
		)
	return 1.0 - (salvage / gross) ** (1.0 / total_periods)


# ── 60. create_asset ────────────────────────────────────────────────────────
def create_asset(args: dict) -> ToolResult:
	"""Create an ERPNext Asset plus the cost profile that says how it is used."""
	_require_assets()
	company = resolve_company(as_str(args, "company"), required=True)
	asset_name = as_str(args, "asset_name", required=True)
	item_code = as_str(args, "item_code", required=True)
	asset_category = as_str(args, "asset_category", required=True)
	purchase_date = as_date(args, "purchase_date", required=True)
	purchase_amount = as_float(args.get("purchase_amount"), "purchase_amount")
	salvage_value = as_float(args.get("salvage_value"), "salvage_value")
	useful_life_months = as_int(args, "useful_life_months")
	# No `or 1`: it reinstated the default above the `<= 0` refusal below, so the one
	# value that refusal names could never reach it.
	frequency = as_int(args, "depreciation_frequency_months", 1)
	depreciation_start = as_date(args, "depreciation_start_date") or purchase_date
	method = as_str(args, "depreciation_method") or "Straight Line"
	method = _validated_method(method)

	if not useful_life_months or useful_life_months <= 0:
		raise ToolError("useful_life_months is required and must be positive. Nothing was created.")
	if frequency <= 0:
		raise ToolError("depreciation_frequency_months must be positive. Nothing was created.")
	if useful_life_months % frequency:
		raise ToolError(
			f"a frequency of {frequency} month(s) does not divide a useful life of "
			f"{useful_life_months} months exactly, which would leave a stub period at the end "
			"that nobody can reconcile. Nothing was created."
		)
	if purchase_amount <= 0:
		raise ToolError("purchase_amount must be positive. Nothing was created.")
	if salvage_value < 0 or salvage_value >= purchase_amount:
		raise ToolError(
			f"salvage_value must be at least 0 and less than purchase_amount ({purchase_amount}); "
			f"got {salvage_value}. Nothing was created."
		)

	if not frappe.db.exists("Asset Category", asset_category):
		known = frappe.db.get_all("Asset Category", pluck="name", limit=25)
		raise ToolError(
			f"no Asset Category named {asset_category!r}. This site has: "
			f"{', '.join(sorted(str(name) for name in known)) or '<none>'}. An asset category is "
			"where ERPNext keeps the fixed-asset, accumulated-depreciation and depreciation-"
			"expense accounts, so it has to exist first — create_asset_category makes one. "
			"Nothing was created."
		)

	allocation = _validated_allocation(args.get("cost_center_allocation"), company)
	if not allocation:
		default = compat.company_default_cost_center(company)
		if not default:
			raise ToolError(
				f"cost_center_allocation was not given and {company} has no default cost center "
				"to fall back on. Pass the split explicitly. Nothing was created."
			)
		allocation = [{"cost_center": default, "percentage": 100.0, "bbch_stage": None, "note": None}]

	# Note tenor, checked BEFORE anything is written: an asset created with a life
	# that disagrees with its note is the exact thing this module exists to prevent,
	# and creating it and then refusing to link it would be the worst of both.
	note = _note_terms(args, depreciation_start)
	if note.get("tenor_months") and int(note["tenor_months"]) != int(useful_life_months):
		raise ToolError(
			f"useful_life_months is {useful_life_months} but the linked note runs "
			f"{note['tenor_months']} month(s) from {depreciation_start}. When an asset is "
			"financed, its life is held equal to the note's tenor so that paid-off and "
			"fully-depreciated fall in the same month — otherwise interest is still being paid "
			"on something with no book value left. Set useful_life_months to "
			f"{note['tenor_months']}, or link the note afterwards with "
			"link_asset_to_note(enforce_tenor=false) if the divergence is deliberate. Nothing "
			"was created."
		)

	capex = _capex_argument(args, purchase_amount)

	item, item_created = _resolve_item(item_code, asset_category, args)

	doc = frappe.new_doc("Asset")
	doc.asset_name = asset_name
	doc.item_code = item
	doc.asset_category = asset_category
	doc.company = company
	doc.purchase_date = purchase_date
	if compat.has_field("Asset", "available_for_use_date"):
		doc.available_for_use_date = depreciation_start
	doc.gross_purchase_amount = purchase_amount
	if compat.has_field("Asset", "asset_quantity"):
		doc.asset_quantity = 1
	if compat.has_field("Asset", "is_existing_asset"):
		doc.is_existing_asset = 1
	# THE line: ERPNext's own scheduler posts depreciation for every asset with
	# this set. This app owns the schedule, so ERPNext must not also own it.
	if compat.has_field("Asset", "calculate_depreciation"):
		doc.calculate_depreciation = 0
	dominant = max(allocation, key=lambda row: row["percentage"])
	if compat.has_field("Asset", "cost_center"):
		doc.cost_center = dominant["cost_center"]
	location = as_str(args, "location")
	if location and compat.has_field("Asset", "location"):
		doc.location = location
	# v0.19.5. The maintenance/growth split, onto whichever of the four columns
	# this site's migrate has actually created.
	for fieldname, value in (capex.get("write") or {}).items():
		if compat.has_field("Asset", fieldname):
			doc.set(fieldname, value)
	doc.insert()

	profile = _insert_profile(
		doc.name,
		company,
		{
			"gross_purchase_amount": purchase_amount,
			"salvage_value": salvage_value,
			"depreciation_method": method,
			"useful_life_months": useful_life_months,
			"depreciation_frequency_months": frequency,
			"depreciation_start_date": depreciation_start,
			"depreciation_expense_account": _optional_account(args, "depreciation_expense_account", company),
			"accumulated_depreciation_account": _optional_account(
				args, "accumulated_depreciation_account", company
			),
			"linked_note_doctype": note.get("doctype") or "",
			"linked_note": note.get("name") or "",
			"note_tenor_months": note.get("tenor_months") or 0,
			"note_maturity_date": note.get("maturity_date") or None,
			"tenor_enforced": 1 if note.get("tenor_months") else 0,
			"notes": as_str(args, "notes"),
		},
		allocation,
	)

	schedule = depreciation_schedule(_profile_row(doc.name)) if method != "Manual" else []
	data = {
		"asset": doc.name,
		"asset_name": asset_name,
		"company": company,
		"item_code": item,
		"item_created": item_created,
		"asset_category": asset_category,
		"purchase_date": purchase_date,
		"purchase_amount": purchase_amount,
		"salvage_value": salvage_value,
		"docstatus": int(doc.docstatus or 0),
		"profile": profile.name,
		"depreciation_method": method,
		"useful_life_months": useful_life_months,
		"depreciation_frequency_months": frequency,
		"depreciation_start_date": depreciation_start,
		"period_count": len(schedule),
		"per_period_amount": schedule[0]["amount"] if schedule else None,
		"final_period_ends": schedule[-1]["period_end"] if schedule else None,
		"cost_center_allocation": allocation,
		# v0.19.5. Echoed rather than left to be read back off the Asset, because
		# the split is what a Sustainable CF/Acre figure is computed from and the
		# caller that just made the classification is the one best placed to notice
		# it landed wrong.
		"capex_type": capex.get("capex_type"),
		"maintenance_portion": capex.get("maintenance_portion"),
		"growth_portion": capex.get("growth_portion"),
		"capex_justification": capex.get("capex_justification"),
		"linked_note": note.get("name") or None,
		"note_tenor_months": note.get("tenor_months") or None,
		"erpnext_depreciation_disabled": True,
		"note": (
			"ERPNext's own depreciation is switched OFF on this asset (calculate_depreciation "
			"= 0) because this app owns its schedule — otherwise ERPNext's daily scheduler and "
			"run_depreciation_cycle would both post, and the asset would depreciate twice. The "
			f"Asset's own cost_center is set to {dominant['cost_center']}, the largest share, so "
			"anything ERPNext does file against it lands somewhere sane."
		),
		"next_step": (
			"The Asset is a DRAFT: submit it in ERPNext when the purchase is real. Depreciation "
			"is written by run_depreciation_cycle, which produces draft journal entries split "
			"across the allocation above."
		),
	}
	return ToolResult(
		data,
		f"created Asset {doc.name} ({asset_name}, {purchase_amount}) for {company} over "
		f"{useful_life_months} months across {_allocation_text(allocation)}",
		docstatus_delta="none → 0 (draft)",
	)


def _capex_argument(args: dict, purchase_amount: float) -> dict:
	"""The maintenance/growth classification, refused now rather than absent later.

	v0.19.5, AND IT IS A GATE RATHER THAN A DEFAULT, which is the whole decision.
	The obvious kindness would be to assume Maintenance when nobody says — most
	farm purchases are — and it is the wrong kindness in the one direction that
	matters. Sustainable CF/Acre subtracts maintenance capex; an unclassified
	purchase silently read as maintenance makes the number LOWER, which is safe,
	but an operation that never has to answer the question stops distinguishing
	the two at all, and then the growth spending disappears into the maintenance
	line and the replacement budget is built on it. The question is cheap at the
	moment of purchase — the person raising it knows why they are buying the thing
	— and unanswerable eighteen months later from an invoice.

	IT ONLY BITES ONCE THE COLUMN EXISTS. The four fields arrive on the `bench
	migrate` that installs v0.19.5, and a tool that refused every asset for want of
	a field the site does not have yet would take asset creation down for the
	length of an upgrade. `kpi.capex_installed()` is that check.

	The JUSTIFICATION is required for Growth and Mixed and not for Maintenance,
	and the asymmetry is on purpose: classifying a purchase as growth takes it out
	of the maintenance figure and RAISES sustainable cash flow. That is the one
	direction in which a misclassification flatters the operation, so it is the one
	that has to carry a sentence.
	"""
	if not kpi.capex_installed():
		return {"write": {}, "installed": False, "capex_type": None}

	raw = as_str(args, "capex_type")
	if not raw:
		raise ToolError(
			"capex_type is required: one of "
			+ ", ".join(kpi.CAPEX_TYPES)
			+ ". MAINTENANCE replaces productive capacity that wore out (the failed "
			"irrigation pump, the worn-out tractor, a replant in kind); GROWTH adds capacity "
			"that was never there (a new block, a new zone, a second sprayer); MIXED is split "
			"across maintenance_portion and growth_portion, which must sum to purchase_amount. "
			"There is no default, and that is deliberate — Sustainable CF/Acre subtracts "
			"maintenance capex, so an unclassified purchase quietly read as maintenance would "
			"let growth spending disappear into the line the replacement budget is built on. "
			"The question is cheap now, while somebody knows why this is being bought, and "
			"unanswerable in eighteen months from an invoice. Nothing was created."
		)

	capex_type = next((option for option in kpi.CAPEX_TYPES if option.lower() == raw.lower()), "")
	if not capex_type:
		raise ToolError(
			f"capex_type must be one of: {', '.join(kpi.CAPEX_TYPES)}. Got {raw!r}. Nothing was created."
		)

	split = kpi.split_for(
		capex_type,
		purchase_amount,
		args.get("maintenance_portion"),
		args.get("growth_portion"),
	)
	if split["error"]:
		raise ToolError(f"{split['error']} Nothing was created.")

	justification = as_str(args, "capex_justification")
	if capex_type in (kpi.CAPEX_GROWTH, kpi.CAPEX_MIXED) and not justification:
		raise ToolError(
			f"capex_justification is required for a {capex_type} purchase: WHAT CAPACITY DOES "
			"THIS ADD? Classifying spend as growth takes it out of the maintenance figure and "
			"raises sustainable cash flow per acre — the one direction in which a "
			"misclassification flatters the operation, which is why it is the one that has to "
			"carry a sentence. Nothing was created."
		)

	return {
		"installed": True,
		"capex_type": capex_type,
		"maintenance_portion": split["maintenance"],
		"growth_portion": split["growth"],
		"capex_justification": justification or None,
		"write": {
			kpi.CAPEX_TYPE_FIELD: capex_type,
			kpi.MAINTENANCE_PORTION_FIELD: split["maintenance"],
			kpi.GROWTH_PORTION_FIELD: split["growth"],
			kpi.CAPEX_JUSTIFICATION_FIELD: justification,
		},
	}


def _validated_method(method: str) -> str:
	options = [
		line.strip()
		for line in str((compat.field_meta(PROFILE, "depreciation_method") or {}).get("options") or "").split(
			"\n"
		)
		if line.strip()
	]
	for option in options or ("Straight Line",):
		if option.lower() == method.lower():
			return option
	raise ToolError(
		f"depreciation_method must be one of: {', '.join(options)}. Got {method!r}. Nothing was created."
	)


def _optional_account(args: dict, key: str, company: str) -> str:
	value = as_str(args, key)
	return resolve_account(value, company) if value else ""


def _resolve_item(item_code: str, asset_category: str, args: dict) -> tuple:
	"""The Item an Asset hangs off, created when it is missing and that is allowed.

	ERPNext requires an Asset to name an Item flagged `is_fixed_asset`. An
	operator building a fixed-asset register through this app has no reason to
	have made one first, so the default is to create it — but an *existing* item
	that is not a fixed asset is refused rather than quietly edited, because
	flipping `is_fixed_asset` on an item that has stock movements is a change to
	the site's inventory, not to this asset.
	"""
	create_if_missing = as_bool(args, "create_item_if_missing", True)
	if frappe.db.exists("Item", item_code):
		row = frappe.db.get_value(
			"Item",
			item_code,
			compat.existing_fields("Item", ("name", "is_fixed_asset", "asset_category")),
			as_dict=True,
		)
		if compat.has_field("Item", "is_fixed_asset") and not int(row.get("is_fixed_asset") or 0):
			raise ToolError(
				f"Item {item_code!r} exists but is not flagged as a fixed asset, and ERPNext "
				"only lets an Asset hang off one that is. Setting the flag on an item that may "
				"have stock movements is an inventory decision, not an asset one — do it in the "
				"Desk, or name a different item_code. Nothing was created."
			)
		return item_code, False

	if not create_if_missing:
		raise ToolError(
			f"no Item named {item_code!r}, and create_item_if_missing is false. Every ERPNext "
			"Asset needs a fixed-asset Item. Nothing was created."
		)

	payload = {
		"doctype": "Item",
		"item_code": item_code,
		"item_name": as_str(args, "asset_name") or item_code,
		"is_fixed_asset": 1,
		"is_stock_item": 0,
		"asset_category": asset_category,
	}
	item_group = _first_existing("Item Group", ("All Item Groups", "Products", "Fixed Asset"))
	if item_group:
		payload["item_group"] = item_group
	uom = _first_existing("UOM", ("Nos", "Unit", "Each"))
	if uom:
		payload["stock_uom"] = uom
	item = frappe.get_doc(payload).insert()
	return item.name, True


def _first_existing(doctype: str, candidates) -> str:
	if not compat.doctype_exists(doctype):
		return ""
	for candidate in candidates:
		if frappe.db.exists(doctype, candidate):
			return candidate
	rows = frappe.db.get_all(doctype, pluck="name", limit=1)
	return rows[0] if rows else ""


def _insert_profile(asset: str, company: str, values: dict, allocation: list[dict]):
	doc = frappe.new_doc(PROFILE)
	doc.asset = asset
	doc.company = company
	for field, value in values.items():
		if value in ("", None):
			continue
		doc.set(field, value)
	for row in allocation:
		doc.append("cost_center_allocation", row)
	doc.insert()
	return doc


# ── note terms ──────────────────────────────────────────────────────────────
def _note_terms(args: dict, from_date: str) -> dict:
	"""What is known about the note financing an asset: its doctype, name and tenor.

	Returns `{}` when no note was named. Tenor is taken from the most explicit
	source available and each source is reported, because "the tool decided the
	note runs 84 months" is only useful with "…because you said so" or "…because
	the note's maturity_date is 2033-06-01" attached.
	"""
	note = as_str(args, "linked_note") or as_str(args, "note_doc_ref")
	if not note:
		return {}
	doctype = as_str(args, "note_doctype")
	doctype = _note_doctype(doctype, note)

	tenor = as_int(args, "note_tenor_months")
	maturity = as_date(args, "note_maturity_date")
	source = "argument"

	if not tenor and not maturity:
		maturity, tenor, source = _note_terms_from_document(doctype, note)

	if maturity and not tenor:
		tenor = months_between(from_date, maturity)
		if tenor <= 0:
			raise ToolError(
				f"the note matures on {maturity}, which is not after the depreciation start "
				f"{from_date}, so its remaining tenor is zero months. Nothing was changed."
			)
	if tenor and not maturity:
		maturity = add_months(from_date, int(tenor)).isoformat()

	return {
		"doctype": doctype,
		"name": note,
		"tenor_months": int(tenor or 0),
		"maturity_date": maturity,
		"tenor_source": source if (tenor or maturity) else "",
	}


def _note_doctype(doctype: str, note: str) -> str:
	"""Which doctype the note lives in, checked against the site."""
	if doctype:
		if not compat.doctype_exists(doctype):
			raise ToolError(f"no DocType named {doctype!r} on this site. Nothing was changed.")
		if not frappe.db.exists(doctype, note):
			raise ToolError(f"no {doctype} named {note!r} on this site. Nothing was changed.")
		return doctype
	for candidate in _NOTE_DOCTYPES:
		if compat.doctype_exists(candidate) and frappe.db.exists(candidate, note):
			return candidate
	raise ToolError(
		f"could not tell which DocType {note!r} is — it is not a "
		f"{', a '.join(_NOTE_DOCTYPES[:-1])} or a {_NOTE_DOCTYPES[-1]} on this site. Register it "
		"with create_note_payable, or pass note_doctype. Nothing was changed."
	)


def _note_terms_from_document(doctype: str, note: str) -> tuple:
	"""Read a tenor or a maturity off the note itself, where the doctype carries one."""
	for field in _NOTE_TENOR_FIELDS:
		if compat.has_field(doctype, field):
			value = frappe.db.get_value(doctype, note, field)
			if value:
				return None, int(value), f"{doctype}.{field}"
	for field in _NOTE_MATURITY_FIELDS:
		if compat.has_field(doctype, field):
			value = frappe.db.get_value(doctype, note, field)
			if value:
				return str(value), None, f"{doctype}.{field}"
	raise ToolError(
		f"{doctype} {note} does not record a tenor or a maturity date in any field this app "
		f"knows ({', '.join(_NOTE_TENOR_FIELDS + _NOTE_MATURITY_FIELDS)}), so the note's term "
		"cannot be read from it. Pass note_tenor_months or note_maturity_date. Nothing was "
		"changed."
	)


# ── 61. update_asset_allocation ─────────────────────────────────────────────
def update_asset_allocation(args: dict) -> ToolResult:
	"""Replace how an asset's cost is shared out, from the next period onwards."""
	_require_assets()
	company = as_str(args, "company")
	asset = resolve_asset(as_str(args, "asset") or as_str(args, "asset_name", required=True), company)
	profile = _profile_row(asset)
	company = profile["company"]

	raw = args.get("new_cost_center_allocation")
	if raw in (None, ""):
		raw = args.get("cost_center_allocation")
	allocation = _validated_allocation(raw, company)
	if not allocation:
		raise ToolError(
			"new_cost_center_allocation is required — a list of {cost_center, percentage} "
			"objects totalling 100. Nothing was changed."
		)

	before = _allocation_rows(profile["name"])
	if _same_allocation(before, allocation):
		raise ToolError(
			f"{asset} already has exactly that allocation; nothing to change. "
			f"Current: {_allocation_text(before)}."
		)

	doc = frappe.get_doc(PROFILE, profile["name"])
	doc.set("cost_center_allocation", [])
	for row in allocation:
		doc.append("cost_center_allocation", row)
	doc.save()

	posted = _posting_rows(profile["name"])
	data = {
		"asset": asset,
		"profile": profile["name"],
		"previous_allocation": before,
		"cost_center_allocation": allocation,
		"periods_already_written": len(posted),
		"note": (
			"Depreciation already written is untouched: the journal entries for those periods "
			"keep the split they were written with, which is the correct history. Only future "
			f"periods follow the new split. {len(posted)} period(s) have already been written."
		),
	}
	return ToolResult(
		data,
		f"reallocated Asset {asset}: {_allocation_text(before)} → {_allocation_text(allocation)}",
		docstatus_delta="",
	)


def _allocation_key(rows: list[dict]):
	return sorted(
		(row["cost_center"], round(float(row["percentage"]), 4), row.get("bbch_stage") or "") for row in rows
	)


def _same_allocation(before: list[dict], after: list[dict]) -> bool:
	return _allocation_key(before) == _allocation_key(after)


def _allocation_text(rows: list[dict]) -> str:
	return (
		", ".join(f"{row['cost_center']} {round(float(row['percentage']), 4)}%" for row in rows) or "<none>"
	)


# ── 62. link_asset_to_note ──────────────────────────────────────────────────
def link_asset_to_note(args: dict) -> ToolResult:
	"""Tie an asset to the note that financed it, and hold their terms together."""
	_require_assets()
	company = as_str(args, "company")
	asset = resolve_asset(as_str(args, "asset") or as_str(args, "asset_name", required=True), company)
	profile = _profile_row(asset)
	enforce = as_bool(args, "enforce_tenor", True)

	note_ref = as_str(args, "note_doc_ref") or as_str(args, "linked_note", required=True)
	note = _note_terms({**args, "linked_note": note_ref}, str(profile.get("depreciation_start_date") or ""))
	if not note:  # pragma: no cover - note_doc_ref is required above
		raise ToolError("note_doc_ref is required. Nothing was changed.")

	life = int(profile.get("useful_life_months") or 0)
	tenor = int(note.get("tenor_months") or 0)
	delta = life - tenor if tenor else None

	if enforce:
		if not tenor:
			raise ToolError(
				f"the note's tenor could not be established, so it cannot be checked against "
				f"the asset's {life}-month life. Pass note_tenor_months or note_maturity_date, "
				"or link it with enforce_tenor=false and accept that the two are not held "
				"together. Nothing was changed."
			)
		if delta:
			raise ToolError(
				f"Asset {asset} depreciates over {life} month(s) but the note runs {tenor} — a "
				f"{abs(delta)}-month divergence. Held apart, the asset is either fully "
				"depreciated while payments continue, or still on the books after the note is "
				"paid; either way the matching principle is broken and the mismatch is invisible "
				"until the final year. Change the useful life with a corrected create_asset, or "
				"link with enforce_tenor=false if the divergence is deliberate and documented. "
				"Nothing was changed."
			)

	doc = frappe.get_doc(PROFILE, profile["name"])
	doc.linked_note_doctype = note.get("doctype")
	doc.linked_note = note.get("name")
	doc.note_tenor_months = tenor or 0
	doc.note_maturity_date = note.get("maturity_date") or None
	doc.tenor_enforced = 1 if (enforce and tenor) else 0
	doc.save()

	data = {
		"asset": asset,
		"profile": profile["name"],
		"linked_note_doctype": note.get("doctype"),
		"linked_note": note.get("name"),
		"note_tenor_months": tenor or None,
		"note_maturity_date": note.get("maturity_date"),
		"tenor_source": note.get("tenor_source") or None,
		"useful_life_months": life,
		"delta_months": delta,
		"tenor_enforced": bool(enforce and tenor),
		"note": (
			"Life and tenor agree; paid-off and fully-depreciated fall in the same month."
			if enforce and tenor and not delta
			else (
				f"Linked WITHOUT the tenor check. The asset's {life}-month life and the note's "
				f"{tenor}-month tenor differ by {abs(delta)} month(s); "
				"depreciation_note_alignment_check will keep reporting it."
				if delta
				else "Linked without a known tenor, so nothing is being held together yet."
			)
		),
	}
	return ToolResult(
		data,
		f"linked Asset {asset} to {note.get('doctype')} {note.get('name')}"
		+ (f" ({tenor} months, matched)" if enforce and tenor and not delta else "")
		+ (f" ({tenor} months, {abs(delta)}-month divergence accepted)" if delta else ""),
		docstatus_delta="",
	)


# ── 63. run_depreciation_cycle ──────────────────────────────────────────────
def run_depreciation_cycle(args: dict) -> ToolResult:
	"""Write the depreciation due up to a date, split across each asset's cost centers.

	`dry_run` DEFAULTS TO TRUE, for the same reason `import_chart_of_accounts`
	does: this is the one tool here that writes to many documents at once, and a
	catch-up run over a year of missed periods is a page of journal entries
	somebody should read first.
	"""
	_require_assets()
	company = resolve_company(as_str(args, "company"), required=True)
	period_end = as_date(args, "period_end") or frappe.utils.today()
	dry_run = as_bool(args, "dry_run", True)

	filters = {"company": company}
	only = as_str(args, "asset")
	if only:
		filters["asset"] = resolve_asset(only, company)
	fields = compat.existing_fields(PROFILE, _PROFILE_FIELDS)
	profiles = frappe.db.get_all(PROFILE, filters=filters, fields=fields, order_by="asset asc", limit=500)
	if not profiles:
		raise ToolError(
			f"no Asset Cost Profile matches {filters!r}. Only assets created with create_asset "
			"(or given a profile by hand) are depreciated by this tool."
		)

	planned, skipped, entries = [], [], []
	total = 0.0
	for row in profiles:
		profile = dict(row)
		if str(profile.get("depreciation_method")) == "Manual":
			skipped.append({"asset": profile["asset"], "reason": "depreciation_method is Manual"})
			continue
		try:
			schedule = depreciation_schedule(profile)
		except ToolError as exc:
			skipped.append({"asset": profile["asset"], "reason": str(exc)})
			continue

		posted = {row["period_index"] for row in _posting_rows(profile["name"])}
		due = [
			period
			for period in schedule
			if period["period_end"] <= period_end and period["period_index"] not in posted
		]
		if not due:
			skipped.append(
				{
					"asset": profile["asset"],
					"reason": f"nothing due through {period_end} ({len(posted)} period(s) already written)",
				}
			)
			continue

		allocation = _allocation_rows(profile["name"])
		try:
			accounts = _depreciation_accounts(profile)
		except ToolError as exc:
			# One misconfigured asset must not take the whole run down with it: the
			# report is more useful listing what it could not do than refusing to
			# say anything about the assets that are fine.
			skipped.append({"asset": profile["asset"], "reason": str(exc)})
			continue
		for period in due:
			lines = _depreciation_lines(period["amount"], allocation, accounts, profile)
			plan = {
				"asset": profile["asset"],
				"period_index": period["period_index"],
				"period_start": period["period_start"],
				"period_end": period["period_end"],
				"amount": period["amount"],
				"lines": lines,
			}
			total = round(total + period["amount"], MONEY_PLACES)
			if not dry_run:
				doc = _post_period(profile, period, lines, accounts)
				plan["journal_entry"] = doc.name
				entries.append(doc.name)
			planned.append(plan)

	data = {
		"company": company,
		"period_end": period_end,
		"dry_run": bool(dry_run),
		"periods": planned,
		"period_count": len(planned),
		"total_depreciation": total,
		"journal_entries": entries,
		"assets_skipped": skipped,
		"note": (
			"DRY RUN — nothing was written. Every amount and every split above is what a real "
			"run would produce; call again with dry_run=false to write them."
			if dry_run
			else (
				"The journal entries are DRAFTS and have moved no balance. Post them with "
				"submit_journal_entry. Each period is recorded on its asset's cost profile, so "
				"a second run will not write it again."
			)
		),
	}
	verb = "would write" if dry_run else "wrote"
	return ToolResult(
		data,
		f"{verb} {len(planned)} depreciation period(s) totalling {total} for {company} "
		f"through {period_end}" + (f"; {len(skipped)} asset(s) skipped" if skipped else ""),
		docstatus_delta="" if dry_run else "none → 0 (draft)",
	)


def _depreciation_accounts(profile: dict) -> dict:
	"""The expense and accumulated-depreciation accounts for one asset.

	The profile first, then the Asset Category — which is where ERPNext keeps
	them, per company — and a refusal naming both places if neither answers.
	"""
	expense = profile.get("depreciation_expense_account") or ""
	accumulated = profile.get("accumulated_depreciation_account") or ""
	source = "Asset Cost Profile"

	if not (expense and accumulated):
		category = frappe.db.get_value("Asset", profile["asset"], "asset_category")
		if category and frappe.db.exists("Asset Category", category):
			for row in frappe.get_doc("Asset Category", category).get("accounts") or []:
				if row.get("company") and row.get("company") != profile["company"]:
					continue
				expense = expense or row.get("depreciation_expense_account") or ""
				accumulated = accumulated or row.get("accumulated_depreciation_account") or ""
				source = f"Asset Category {category}"
				break

	missing = [
		label
		for label, value in (
			("depreciation_expense_account", expense),
			("accumulated_depreciation_account", accumulated),
		)
		if not value
	]
	if missing:
		raise ToolError(
			f"Asset {profile['asset']} has no {' and no '.join(missing)}. ERPNext keeps these on "
			"the Asset Category, per company; set them there, or set them on the Asset Cost "
			"Profile for this asset alone. Nothing was written."
		)
	for account in (expense, accumulated):
		if not frappe.db.exists("Account", account):
			raise ToolError(f"account {account!r} does not exist on this site. Nothing was written.")
	return {"expense": expense, "accumulated": accumulated, "source": source}


def _depreciation_lines(amount: float, allocation: list[dict], accounts: dict, profile: dict) -> list[dict]:
	"""One debit per allocation row, one credit for the lot, adding up exactly.

	The last debit absorbs the rounding. Three cost centers over a 33.33/33.33/
	33.34 split otherwise leave a cent unaccounted for, and a journal entry that
	does not balance is not a rounding problem, it is a refused save.
	"""
	debits = []
	running = 0.0
	for index, row in enumerate(allocation, start=1):
		share = round(amount * float(row["percentage"]) / 100.0, MONEY_PLACES)
		if index == len(allocation):
			share = round(amount - running, MONEY_PLACES)
		running = round(running + share, MONEY_PLACES)
		if share <= 0:
			continue
		line = {
			"account": accounts["expense"],
			"debit": share,
			"cost_center": row["cost_center"],
			"user_remark": f"Depreciation {profile['asset']} — {row['percentage']}% {row['cost_center']}",
		}
		if row.get("bbch_stage"):
			line["dimensions"] = {"bbch_stage": row["bbch_stage"]}
		debits.append(line)
	credit = {
		"account": accounts["accumulated"],
		"credit": round(sum(line["debit"] for line in debits), MONEY_PLACES),
		"cost_center": allocation[0]["cost_center"],
		"user_remark": f"Accumulated depreciation {profile['asset']}",
	}
	return [*debits, credit]


def _post_period(profile: dict, period: dict, raw_lines: list[dict], accounts: dict):
	"""Write one period's draft Journal Entry and record that it has been written."""
	lines = mutate.validated_journal_lines(raw_lines, profile["company"])
	remark = (
		f"Depreciation of {profile['asset']} for period {period['period_index']} "
		f"({period['period_start']} to {period['period_end']}), split by its cost profile."
	)
	doc = mutate.insert_draft_journal_entry(profile["company"], period["period_end"], lines, remark)

	entry = frappe.get_doc(PROFILE, profile["name"])
	entry.append(
		"depreciation_postings",
		{
			"period_index": period["period_index"],
			"period_start": period["period_start"],
			"period_end": period["period_end"],
			"amount": period["amount"],
			"journal_entry": doc.name,
			"posted_on": frappe.utils.now(),
		},
	)
	entry.save()
	return doc


# ── 64. depreciation_note_alignment_check ───────────────────────────────────
def depreciation_note_alignment_check(args: dict) -> ToolResult:
	"""Where an asset's remaining life and its note's remaining term have parted.

	Read-only, and reports on every financed asset rather than only the broken
	ones, because "nothing is wrong" is an answer somebody has to be able to see.
	"""
	_require_assets()
	company = resolve_company(as_str(args, "company"), required=True)
	as_of = as_date(args, "as_of") or frappe.utils.today()

	fields = compat.existing_fields(PROFILE, _PROFILE_FIELDS)
	profiles = frappe.db.get_all(
		PROFILE, filters={"company": company}, fields=fields, order_by="asset asc", limit=500
	)

	assets, unlinked = [], []
	for row in profiles:
		profile = dict(row)
		if not profile.get("linked_note"):
			unlinked.append(profile["asset"])
			continue

		life = int(profile.get("useful_life_months") or 0)
		tenor = int(profile.get("note_tenor_months") or 0)
		start = str(profile.get("depreciation_start_date") or "")
		elapsed = months_between(start, as_of) if start else 0
		remaining_life = max(0, life - elapsed)
		maturity = str(profile.get("note_maturity_date") or "")
		remaining_note = months_between(as_of, maturity) if maturity else max(0, tenor - elapsed)
		delta = remaining_life - remaining_note

		assets.append(
			{
				"asset": profile["asset"],
				"linked_note_doctype": profile.get("linked_note_doctype"),
				"linked_note": profile.get("linked_note"),
				"useful_life_months": life,
				"note_tenor_months": tenor or None,
				"note_maturity_date": maturity or None,
				"depreciation_start_date": start or None,
				"months_elapsed": elapsed,
				"remaining_depreciation_months": remaining_life,
				"remaining_note_months": remaining_note,
				"delta_months": delta,
				"aligned": delta == 0,
				"tenor_enforced": bool(int(profile.get("tenor_enforced") or 0)),
				"reading": _alignment_reading(delta),
			}
		)

	diverged = [asset for asset in assets if not asset["aligned"]]
	data = {
		"company": company,
		"as_of": as_of,
		"assets": assets,
		"checked": len(assets),
		"diverged": diverged,
		"diverged_count": len(diverged),
		"assets_without_a_note": unlinked,
		"note": (
			"A divergence is not automatically wrong — an asset outliving its financing is "
			"ordinary. It is worth an explanation, and an explanation nobody has written down "
			"is the thing this check exists to surface. Remaining months are counted from the "
			"depreciation start date, so an asset not yet in service reads its full life."
		),
	}
	summary = f"{len(assets)} financed asset(s) checked for {company} as of {as_of}: {len(diverged)} diverged"
	return ToolResult(data, summary)


def _alignment_reading(delta: int) -> str:
	if delta == 0:
		return "Aligned: the asset is fully depreciated in the month the note is paid off."
	if delta > 0:
		return (
			f"The asset still has {delta} month(s) of depreciation left after the note is paid "
			"off — book value outlives the financing."
		)
	return (
		f"The note runs {abs(delta)} month(s) past the asset's last depreciation — interest is "
		"still being paid on something with no book value left."
	)


# ── 65. create_asset_category ───────────────────────────────────────────────

#: What ERPNext's own `AssetCategory.validate_account_types` insists each column
#: of an accounts row carries. Checked HERE, before the insert, so a caller that
#: passed the wrong account is told which argument was wrong and what the site
#: has instead — rather than receiving ERPNext's "Row #1: Account Type of
#: <docname> should be Fixed Asset" from a controller three layers down.
_CATEGORY_ACCOUNT_TYPES = (
	("fixed_asset_account", "Fixed Asset"),
	("accumulated_depreciation_account", "Accumulated Depreciation"),
	("depreciation_expense_account", "Depreciation"),
)

#: The Select on `Asset Finance Book.depreciation_method`, for the case where
#: this site's meta cannot answer for a doctype that is ERPNext's, not this
#: app's. Same four options as the Asset Cost Profile carries.
_FINANCE_BOOK_METHODS = ("Straight Line", "Double Declining Balance", "Written Down Value", "Manual")


def create_asset_category(args: dict) -> ToolResult:
	"""Create one ERPNext Asset Category — the thing `create_asset` refuses without.

	AN ASSET CATEGORY CANNOT BE CREATED FROM A NAME ALONE, and that is why this
	tool takes accounts rather than just `asset_category_name`. ERPNext marks the
	`accounts` table `reqd`, so Frappe's own mandatory check refuses a category
	with an empty table — "Data missing in table: Accounts" — before any
	controller runs. The refusal below is that same refusal, made earlier, in
	words that name the argument that was missing and list what the site has.

	ASKING FOR ONE THAT EXISTS RETURNS IT. An Asset Category is
	`autoname: field:asset_category_name`, so the name IS the docname and there
	can only ever be one. "Let there be a category called this" is already
	satisfied when it is, so this answers with the existing document and writes
	nothing, rather than raising at a caller who wanted the end state.

	THE FINANCE BOOK ROW IS ERPNEXT'S SCHEDULE, NOT THIS APP'S. It is what
	ERPNext's own depreciation would use, and this app switches that off on every
	asset `create_asset` makes (see the module docstring). It is written here
	because the category is a shared master an operator also uses from the Desk,
	and a half-configured one is worse than none — but nothing in this app reads
	it. The schedule this app runs lives on the Asset Cost Profile, per asset.
	"""
	compat.require_doctype(
		"Asset Category",
		"It ships with ERPNext's Assets module; enable it if this site hid the module.",
	)
	name = as_str(args, "asset_category_name", required=True)

	if frappe.db.exists("Asset Category", name):
		return _existing_asset_category(name)

	company = resolve_company(as_str(args, "company"), required=True)
	accounts = _category_accounts_row(args, company)
	finance_book = _category_finance_book(args)

	doc = frappe.new_doc("Asset Category")
	doc.asset_category_name = name
	# `company_name`, not `company` — ERPNext names the column on Asset Category
	# Account after the label, and this is the one place in this app that writes
	# that row rather than reading it.
	company_field = compat.first_field("Asset Category Account", "company_name", "company") or "company_name"
	doc.append("accounts", {company_field: company, **accounts})
	if finance_book:
		doc.append("finance_books", dict(finance_book))
	doc.insert()

	return ToolResult(
		{
			"name": doc.name,
			"asset_category_name": name,
			"created": True,
			"company": company,
			"accounts": [{"company": company, **accounts}],
			"finance_books": [finance_book] if finance_book else [],
			"note": (
				"An Asset Category has no docstatus and therefore no draft state: this one is "
				"live, and every company on the site can file assets under it. The accounts row "
				"is per company — a second company needs its own row, added in the Desk."
				+ (
					""
					if finance_book
					else " No finance book row was written, so ERPNext's own depreciation defaults "
					"for this category are unset. That is harmless for assets created by "
					"create_asset, which carry their schedule on the Asset Cost Profile instead."
				)
			),
			"next_step": (
				f"Pass {doc.name!r} as `asset_category` to create_asset, or to register_asset for "
				"an asset that is tracked but not depreciated here."
			),
		},
		f"created Asset Category {doc.name} for {company}",
		docstatus_delta="none → created",
	)


def _existing_asset_category(name: str) -> ToolResult:
	"""The category that was already there, read back through its parent.

	Not `frappe.db.get_all("Asset Category Account", filters={"parent": name})`:
	a child table is read through the document that owns it.
	"""
	doc = frappe.get_doc("Asset Category", name)
	company_field = compat.first_field("Asset Category Account", "company_name", "company") or "company_name"
	accounts = [
		{
			"company": row.get(company_field) or None,
			"fixed_asset_account": row.get("fixed_asset_account") or None,
			"accumulated_depreciation_account": row.get("accumulated_depreciation_account") or None,
			"depreciation_expense_account": row.get("depreciation_expense_account") or None,
		}
		for row in doc.get("accounts") or []
	]
	books = [
		{
			"depreciation_method": row.get("depreciation_method") or None,
			"total_number_of_depreciations": row.get("total_number_of_depreciations") or None,
			"frequency_of_depreciation": row.get("frequency_of_depreciation") or None,
		}
		for row in doc.get("finance_books") or []
	]
	return ToolResult(
		{
			"name": doc.name,
			"asset_category_name": doc.get("asset_category_name") or doc.name,
			"created": False,
			"accounts": accounts,
			"finance_books": books,
			"note": (
				"NOTHING WAS WRITTEN. An Asset Category is named after itself, so this one "
				"already is what was asked for. Its accounts are listed above so a caller can "
				"see whether the company it cares about has a row; adding one to an existing "
				"category is a Desk edit, not something this tool does."
			),
		},
		f"Asset Category {doc.name} already exists with {len(accounts)} account row(s); nothing was changed",
	)


def _category_accounts_row(args: dict, company: str) -> dict:
	"""The one accounts row ERPNext will not create an Asset Category without.

	The two depreciation accounts fall back to the Company's own defaults, which
	is where ERPNext keeps them and where every category on a site otherwise
	repeats them. `fixed_asset_account` has NO company default in ERPNext, so it
	is the one account this tool has to be given.
	"""
	fixed = as_str(args, "fixed_asset_account")
	if not fixed:
		raise ToolError(
			"fixed_asset_account is required. ERPNext marks the accounts table on an Asset "
			'Category as mandatory — a category with no accounts row is refused with "Data '
			'missing in table: Accounts" — and the fixed asset account is the one column of '
			"that row ERPNext has no company default to fall back on. Fixed Asset accounts on "
			f"{company}: {_account_candidates(company, 'Fixed Asset')}. Nothing was created."
		)
	row = {"fixed_asset_account": resolve_account(fixed, company)}
	for key in ("accumulated_depreciation_account", "depreciation_expense_account"):
		given = as_str(args, key)
		if given:
			row[key] = resolve_account(given, company)
		elif compat.has_field("Company", key):
			default = frappe.db.get_value("Company", company, key)
			if default:
				row[key] = default
	_validate_category_account_types(row, company)
	return row


def _validate_category_account_types(row: dict, company: str) -> None:
	"""ERPNext's account-type rule, applied before the insert instead of during it."""
	for key, expected in _CATEGORY_ACCOUNT_TYPES:
		account = row.get(key)
		if not account:
			continue
		actual = frappe.db.get_value("Account", account, "account_type") or ""
		if actual != expected:
			raise ToolError(
				f"{key} {account!r} is an account of type {actual or '<none>'!r}, and ERPNext's "
				f"Asset Category accepts only {expected!r} there — it refuses the save otherwise. "
				f"{expected} accounts on {company}: {_account_candidates(company, expected)}. "
				"Nothing was created."
			)


def _account_candidates(company: str, account_type: str) -> str:
	"""The site's own answer to "then which account did you mean", for a refusal."""
	names = frappe.db.get_all(
		"Account",
		filters={"company": company, "account_type": account_type, "is_group": 0},
		pluck="name",
		limit=10,
	)
	return ", ".join(sorted(str(one) for one in names)) or f"<none — {company} has no {account_type} account>"


def _category_finance_book(args: dict) -> dict:
	"""ERPNext's depreciation defaults for the category: all three, or none.

	`Asset Finance Book` marks the method, the total and the frequency all `reqd`
	and its parent throws on a total or a frequency below 1, so a row carrying
	one of the three is a row ERPNext will not save. Refusing here says which of
	them is missing; refusing there says "Row 1: Total Number of Depreciations
	must be greater than 0" after the category is already half-made.
	"""
	keys = ("depreciation_method", "total_number_of_depreciations", "frequency_of_depreciation")
	# Presence, not truth: `total_number_of_depreciations=0` is a value somebody
	# passed and is refused below by name, not silently read as "not given".
	given = [key for key in keys if args.get(key) not in (None, "")]
	if not given:
		return {}
	missing = [key for key in keys if key not in given]
	if missing:
		raise ToolError(
			"a finance book row needs depreciation_method, total_number_of_depreciations and "
			f"frequency_of_depreciation together; {', '.join(missing)} was not given. ERPNext "
			"marks all three mandatory on Asset Finance Book. Pass all three, or none of them "
			"to create the category without ERPNext depreciation defaults. Nothing was created."
		)
	total = as_int(args, "total_number_of_depreciations")
	frequency = as_int(args, "frequency_of_depreciation")
	if total < 1 or frequency < 1:
		raise ToolError(
			"total_number_of_depreciations and frequency_of_depreciation must both be at least "
			f"1; got {total} and {frequency}. ERPNext throws on either below 1. Nothing was created."
		)
	return {
		"depreciation_method": _validated_finance_book_method(as_str(args, "depreciation_method")),
		"total_number_of_depreciations": total,
		"frequency_of_depreciation": frequency,
	}


def _validated_finance_book_method(method: str) -> str:
	"""Like `_validated_method`, but reading ERPNext's Select rather than this app's."""
	options = [
		line.strip()
		for line in str(
			(compat.field_meta("Asset Finance Book", "depreciation_method") or {}).get("options") or ""
		).split("\n")
		if line.strip()
	] or list(_FINANCE_BOOK_METHODS)
	for option in options:
		if option.lower() == method.lower():
			return option
	raise ToolError(
		f"depreciation_method must be one of: {', '.join(options)}. Got {method!r}. Nothing was created."
	)
