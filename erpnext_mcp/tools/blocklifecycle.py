# SPDX-License-Identifier: MIT
"""What a block cost, what it returned, and where it is in its own life.

────────────────────────────────────────────────────────────────────────────
THE PROBLEM THIS SOLVES, WHICH IS NOT A REPORTING PROBLEM
────────────────────────────────────────────────────────────────────────────

A tree fruit block planted in 2021 spends four years costing money and returning
none, then a decade returning more than it costs, then a decline somebody has to
decide the end of. Read one of those years as a profit and loss statement and
the block is a catastrophe; read the whole life and it is the best investment on
the farm. Every general ledger in existence answers the first question, and no
general ledger answers the second, because the ledger's period is the fiscal
year and the block's period is fifteen of them.

`Planting Season` is what makes the second question askable: a junction between
a block and what is growing on it, with a plant year that stays fixed and a
season year that moves. `get_block_profitability` is what answers it, and it
refuses to present an establishing block's negative cash flow as a loss — see
below.

────────────────────────────────────────────────────────────────────────────
TWO SOURCES OF COST, AND THE DOUBLE COUNT BETWEEN THEM
────────────────────────────────────────────────────────────────────────────

Cost reaches a block two ways:

  * THE LEDGER, through the block's cost center. This is authoritative and it is
    where most costs already are, because ERPNext has been booking them there
    all along.
  * `Block Cost Entry`, for the three things the ledger cannot hold: a shared
    cost split across blocks, a cost with no ledger entry at all (owner labour,
    an in-kind trade), and the capitalisation decision per cost.

Adding those two together is WRONG whenever a Block Cost Entry was swept from a
GL entry that the ledger sweep also counts. So `get_block_cost_summary` reports
them SEPARATELY and names the overlap it can see, and its `total` uses the
ledger plus only the attribution rows that are not of ledger origin. The rows it
cannot rule out are counted and named rather than silently included or silently
dropped — see `_overlap`.

────────────────────────────────────────────────────────────────────────────
REVENUE IS AN ATTRIBUTION, NOT A RECOGNITION
────────────────────────────────────────────────────────────────────────────

The ledger records that a settlement paid $84,000. Nothing in the ledger records
that four blocks grew the fruit. `Block Revenue Entry` is that attribution, and
`get_block_revenue_summary` will build it from Scale Tickets — which already
carry a field and a block — when asked to. A figure summed from there is not the
company's revenue and must never be presented as it; the two answer 'what did
the business earn' and 'which ground earned it'.
"""

from __future__ import annotations

import frappe

from .. import compat, timezones
from ..args import (
	as_bool,
	as_choice,
	as_date,
	as_float,
	as_int,
	as_limit,
	as_str,
	resolve_company,
	resolve_cost_center,
)
from ..errors import ToolError
from ..result import ToolResult

PLANTING_SEASON = "Planting Season"
COST_ENTRY = "Block Cost Entry"
REVENUE_ENTRY = "Block Revenue Entry"
FIELD = "Field"
GL_ENTRY = "GL Entry"
SCALE_TICKET = "Scale Ticket"

ESTABLISHING = "Establishing"
PRODUCTIVE = "Productive"
DECLINING = "Declining"
REMOVED = "Removed"

PERENNIAL = "Perennial"
ANNUAL = "Annual"

LIST_CAP = 200

#: Most ledger rows one block summary reads. A block with more GL entries than
#: this in one window is a cost center somebody is using as a catch-all, and a
#: total built off a silently truncated read would be wrong without saying so.
GL_CAP = 5000

_SEASON_FIELDS = (
	"name",
	"season_label",
	"status",
	"company",
	"field",
	"block_name",
	"lifecycle",
	"crop",
	"variety",
	"rootstock",
	"cost_center",
	"plant_year",
	"season_year",
	"productive_from",
	"productive_through",
	"acres",
	"trees_planted",
	"spacing_in_row_ft",
	"spacing_between_rows_ft",
	"trees_per_acre",
	"expected_yield_per_acre",
	"yield_uom",
	"actual_yield",
	"removed_on",
	"removal_reason",
	"replaced_by",
	"notes",
)

_COST_FIELDS = (
	"name",
	"planting_season",
	"company",
	"field",
	"block_name",
	"posting_date",
	"season_year",
	"cost_category",
	"capitalized",
	"amount",
	"acres",
	"per_acre",
	"quantity",
	"quantity_uom",
	"source",
	"account",
	"cost_center",
	"journal_entry",
	"gl_entry",
	"allocation_basis",
	"allocation_pct",
	"description",
)

_REVENUE_FIELDS = (
	"name",
	"planting_season",
	"company",
	"field",
	"block_name",
	"posting_date",
	"season_year",
	"revenue_type",
	"amount",
	"quantity",
	"quantity_uom",
	"price_per_unit",
	"variety",
	"grade",
	"source",
	"settlement_statement",
	"sales_invoice",
	"scale_ticket",
	"customer",
	"allocation_basis",
	"allocation_pct",
	"pool_total",
	"description",
)


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _number(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _season_doc(reference: str, company: str = "") -> dict:
	"""One Planting Season, by docname or by a readable description of it.

	A model will say "the 2021 Gala block" as often as it will say a hash, so a
	non-docname is matched against the label. Ambiguity is refused with the
	candidates listed rather than resolved by picking the first — two plantings
	on one field is the ordinary case this exists to support.
	"""
	reference = str(reference or "").strip()
	if not reference:
		raise ToolError("planting_season is required.")
	if frappe.db.exists(PLANTING_SEASON, reference):
		row = frappe.db.get_value(
			PLANTING_SEASON, reference, compat.existing_fields(PLANTING_SEASON, _SEASON_FIELDS), as_dict=True
		)
		return dict(row)

	filters: dict = {"season_label": ("like", f"%{reference}%")}
	if company:
		filters["company"] = company
	matches = frappe.db.get_all(PLANTING_SEASON, filters=filters, pluck="name", limit=10)
	if len(matches) == 1:
		return _season_doc(matches[0])
	if len(matches) > 1:
		raise ToolError(
			f"{reference!r} matches {len(matches)} plantings: {', '.join(sorted(matches)[:6])}. "
			"A field worked as two plantings is the ordinary case this record exists for, so pass "
			"the docname. list_planting_seasons has them."
		)
	raise ToolError(f"no Planting Season matching {reference!r}. list_planting_seasons has the register.")


# ── create_planting_season ──────────────────────────────────────────────────
def create_planting_season(args: dict) -> ToolResult:
	"""Open a planting: one block, one crop, one year of its life."""
	_require(PLANTING_SEASON)
	_require(FIELD)
	company = resolve_company(as_str(args, "company"))

	field = as_str(args, "field", required=True)
	if not frappe.db.exists(FIELD, field):
		raise ToolError(
			f"no Field called {field!r} on this site. list_fields has the register, and create_field "
			"opens one. Nothing was created."
		)
	field_row = (
		frappe.db.get_value(
			FIELD,
			field,
			compat.existing_fields(
				FIELD, ("name", "acreage", "crop", "variety", "rootstock", "cost_center", "owning_entity")
			),
			as_dict=True,
		)
		or {}
	)

	crop = as_str(args, "crop") or str(field_row.get("crop") or "")
	if not crop:
		raise ToolError(
			f"crop is required — neither the argument nor {field!r} carries one. A planting is a "
			"block and what is growing on it. Nothing was created."
		)
	plant_year = as_int(args, "plant_year")
	if not plant_year:
		raise ToolError(
			"plant_year is required. It is what a block's age is measured from, and on a perennial "
			"it is the one year that never changes. Nothing was created."
		)
	block_name = as_str(args, "block_name")
	season_year = as_int(args, "season_year")

	lifecycle = as_str(args, "lifecycle") or PERENNIAL
	lifecycle = as_choice(PLANTING_SEASON, "lifecycle", lifecycle, "lifecycle")
	if lifecycle == ANNUAL and season_year and season_year != plant_year:
		raise ToolError(
			f"this is Annual with plant_year {plant_year} and season_year {season_year}. An annual "
			"IS its planting — it goes in, produces and comes out, so the two years are the same "
			"one. A planting that spans years is Perennial. Left as written, several years of "
			"establishment cost would land against one year's revenue and this block would read as "
			"ruinous in one season and free in the others. Nothing was created."
		)

	# ONE PLANTING PER BLOCK PER CROP PER SEASON. Two would give every cost and
	# every settlement two places to land, and no rule for choosing — which is
	# not a duplicate-record annoyance but a silent halving of a block's costs.
	clash_filters = {
		"field": field,
		"block_name": block_name or "",
		"crop": crop,
		"plant_year": plant_year,
		"season_year": season_year or 0,
	}
	clash = frappe.db.get_value(PLANTING_SEASON, clash_filters, "name")
	if clash:
		raise ToolError(
			f"{field} already has a {crop} planting for {season_year or plant_year} ({clash}). Two "
			"plantings of one crop on one block in one season give every cost two places to land "
			"and no rule for choosing. To record a second variety on the same ground, give it its "
			"own block_name. Nothing was created."
		)

	cost_center = as_str(args, "cost_center")
	if cost_center:
		cost_center = resolve_cost_center(cost_center, company or "")
	else:
		cost_center = str(field_row.get("cost_center") or "")

	doc = frappe.new_doc(PLANTING_SEASON)
	doc.company = company or str(field_row.get("owning_entity") or "") or None
	doc.field = field
	doc.block_name = block_name or None
	doc.crop = crop
	doc.variety = as_str(args, "variety") or str(field_row.get("variety") or "") or None
	doc.rootstock = as_str(args, "rootstock") or str(field_row.get("rootstock") or "") or None
	doc.lifecycle = lifecycle
	doc.plant_year = plant_year
	doc.season_year = season_year or (plant_year if lifecycle == ANNUAL else 0)
	doc.productive_from = as_date(args, "productive_from")
	doc.productive_through = as_date(args, "productive_through")
	doc.cost_center = cost_center or None
	# Not `as_float(...) or _number(...)`: `as_float` answers 0.0 for absent and for an
	# explicit 0 alike, so a caller stating `acres: 0` silently inherited the Field's
	# acreage instead — a different number, from a different source, reported as theirs.
	raw_acres = args.get("acres")
	doc.acres = (
		as_float(raw_acres, "acres") if raw_acres not in (None, "") else _number(field_row.get("acreage"))
	)
	doc.trees_planted = as_int(args, "trees_planted") or 0
	doc.spacing_in_row_ft = as_float(args.get("spacing_in_row_ft"), "spacing_in_row_ft")
	doc.spacing_between_rows_ft = as_float(args.get("spacing_between_rows_ft"), "spacing_between_rows_ft")
	doc.expected_yield_per_acre = as_float(args.get("expected_yield_per_acre"), "expected_yield_per_acre")
	doc.yield_uom = as_str(args, "yield_uom") or None
	doc.notes = as_str(args, "notes") or None
	status = as_str(args, "status")
	doc.status = as_choice(PLANTING_SEASON, "status", status, "status") if status else ESTABLISHING
	doc.insert(ignore_permissions=True)

	described = _describe_season(dict(doc.as_dict()))
	warnings = []
	if not described["cost_center"]:
		warnings.append(
			f"No cost center on this planting or on {field}, so ledger costs cannot be swept to "
			"this block at all — get_block_cost_summary will see only the attribution rows. "
			"link_field_to_cost_center points the block at one."
		)
	if not described["acres"]:
		warnings.append(
			"No acres, so every per-acre figure on this block is unavailable. That is the number "
			"blocks are actually compared on."
		)
	if lifecycle == PERENNIAL and not described["productive_from"]:
		warnings.append(
			"No productive_from date on a perennial, so nothing can say when establishment ends. "
			"It is the boundary between costs that capitalise into the block's basis and costs "
			"that are expensed against a crop."
		)
	if described["status"] == PRODUCTIVE and not described["productive_from"]:
		warnings.append("Marked Productive with no productive_from date on record.")

	return ToolResult(
		data={**described, "warnings": warnings},
		summary=(
			f"planting {doc.name}: {crop} on {field}, {lifecycle.lower()}, "
			f"planted {plant_year}, {described['status'].lower()}"
		),
		docstatus_delta="none → 0 (created)",
	)


def _describe_season(row: dict) -> dict:
	plant_year = int(row.get("plant_year") or 0)
	season_year = int(row.get("season_year") or 0)
	this_year = int(str(frappe.utils.nowdate())[:4])
	reference_year = season_year or this_year
	return {
		"name": row.get("name"),
		"season_label": row.get("season_label") or None,
		"status": row.get("status"),
		"company": row.get("company") or None,
		"field": row.get("field"),
		"block_name": row.get("block_name") or None,
		"lifecycle": row.get("lifecycle") or PERENNIAL,
		"crop": row.get("crop"),
		"variety": row.get("variety") or None,
		"rootstock": row.get("rootstock") or None,
		"cost_center": row.get("cost_center") or None,
		"plant_year": plant_year or None,
		"season_year": season_year or None,
		# Leaf year is how tree fruit actually talks about block age: the year it
		# was planted is first leaf. Reported rather than stored because it moves
		# every January and a stored copy would be wrong for eleven months.
		"leaf_year": (reference_year - plant_year + 1) if plant_year else None,
		"productive_from": str(row.get("productive_from") or "") or None,
		"productive_through": str(row.get("productive_through") or "") or None,
		"acres": round(_number(row.get("acres")), 3),
		"trees_planted": int(row.get("trees_planted") or 0) or None,
		"spacing_in_row_ft": round(_number(row.get("spacing_in_row_ft")), 2) or None,
		"spacing_between_rows_ft": round(_number(row.get("spacing_between_rows_ft")), 2) or None,
		"trees_per_acre": round(_number(row.get("trees_per_acre")), 1) or None,
		"expected_yield_per_acre": round(_number(row.get("expected_yield_per_acre")), 3) or None,
		"yield_uom": row.get("yield_uom") or None,
		"actual_yield": round(_number(row.get("actual_yield")), 3) or None,
		"removed_on": str(row.get("removed_on") or "") or None,
		"removal_reason": row.get("removal_reason") or None,
		"replaced_by": row.get("replaced_by") or None,
		"notes": row.get("notes") or None,
	}


# ── list_planting_seasons ───────────────────────────────────────────────────
def list_planting_seasons(args: dict) -> ToolResult:
	"""Plantings on file, newest planting year first."""
	_require(PLANTING_SEASON)
	company = resolve_company(as_str(args, "company"))

	filters: dict = {}
	if company:
		filters["company"] = company
	for key in ("field", "crop", "variety", "lifecycle", "status"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	if as_int(args, "plant_year"):
		filters["plant_year"] = as_int(args, "plant_year")
	if as_int(args, "season_year"):
		filters["season_year"] = as_int(args, "season_year")
	if not as_bool(args, "include_removed", False) and not as_str(args, "status"):
		filters["status"] = ("!=", REMOVED)

	rows = frappe.db.get_all(
		PLANTING_SEASON,
		filters=filters,
		fields=compat.existing_fields(PLANTING_SEASON, _SEASON_FIELDS),
		order_by="plant_year desc, field asc",
		limit=min(as_limit(args), LIST_CAP),
	)
	seasons = [_describe_season(dict(row)) for row in rows or []]

	by_status: dict = {}
	by_lifecycle: dict = {}
	for season in seasons:
		by_status[season["status"]] = by_status.get(season["status"], 0) + 1
		by_lifecycle[season["lifecycle"]] = by_lifecycle.get(season["lifecycle"], 0) + 1
	acres = round(sum(season["acres"] for season in seasons), 3)
	establishing_acres = round(
		sum(season["acres"] for season in seasons if season["status"] == ESTABLISHING), 3
	)

	return ToolResult(
		data={
			"count": len(seasons),
			"total_acres": acres,
			"establishing_acres": establishing_acres,
			"by_status": by_status,
			"by_lifecycle": by_lifecycle,
			"without_cost_center": [s["name"] for s in seasons if not s["cost_center"]],
			"planting_seasons": seasons,
		},
		summary=(
			f"{len(seasons)} planting(s) over {acres:g} ac, {by_status.get(ESTABLISHING, 0)} establishing"
		),
	)


# ── get_planting_season ─────────────────────────────────────────────────────
def get_planting_season(args: dict) -> ToolResult:
	"""One planting, with where it stands in its own life."""
	_require(PLANTING_SEASON)
	company = resolve_company(as_str(args, "company"))
	row = _season_doc(as_str(args, "planting_season", required=True), company or "")
	described = _describe_season(row)

	field_detail = None
	if frappe.db.exists(FIELD, described["field"]):
		field_row = frappe.db.get_value(
			FIELD,
			described["field"],
			compat.existing_fields(FIELD, ("name", "field_name", "parcel", "acreage", "cost_center")),
			as_dict=True,
		)
		field_detail = dict(field_row or {})

	siblings = frappe.db.get_all(
		PLANTING_SEASON,
		filters={"field": described["field"], "name": ("!=", described["name"])},
		fields=["name", "season_label", "crop", "variety", "plant_year", "season_year", "status", "acres"],
		order_by="plant_year desc",
		limit=50,
	)

	return ToolResult(
		data={
			**described,
			"field_detail": field_detail,
			"other_plantings_on_this_field": [dict(s) for s in siblings or []],
			"lifecycle_notes": _lifecycle_notes(described),
		},
		summary=(
			f"{described['season_label'] or described['name']}: {described['status']}, "
			f"leaf year {described['leaf_year'] or '?'}, {described['acres']:g} ac"
		),
	)


def _lifecycle_notes(season: dict) -> list[str]:
	"""What the dates and the status say about each other, in words."""
	notes = []
	today = str(frappe.utils.nowdate())
	if season["lifecycle"] == PERENNIAL and season["status"] == ESTABLISHING:
		notes.append(
			f"Establishing, leaf year {season['leaf_year'] or '?'}. Costs on this block are "
			"pre-productive: they generally capitalise into its basis rather than being expensed "
			"against a crop, so a negative cash flow here is an investment and not a loss. Mark "
			"Block Cost Entry rows Capitalised to keep that distinction on the record."
		)
	if season["productive_from"] and season["status"] == ESTABLISHING and season["productive_from"] < today:
		notes.append(
			f"Planned to be productive from {season['productive_from']}, which has passed, and it "
			"is still marked Establishing. That is a legitimate state — the transition is a "
			"judgement somebody makes standing in the block, not a date arriving — but it is worth "
			"a look, because every cost booked here is still capitalising."
		)
	if (
		season["productive_through"]
		and season["productive_through"] < today
		and season["status"] not in (REMOVED, DECLINING)
	):
		notes.append(
			f"Past its planned productive life ({season['productive_through']}) and still marked "
			f"{season['status']}. Either the block is outliving the plan, which is worth knowing, "
			"or the status has not been moved to Declining."
		)
	if season["status"] == REMOVED and not season["replaced_by"]:
		notes.append(
			"Removed with nothing recorded as replacing it. If the ground was replanted, linking "
			"the new planting here is what makes the block's history a chain rather than a set of "
			"unrelated records."
		)
	if season["lifecycle"] == PERENNIAL and not season["productive_through"]:
		notes.append(
			"No planned end to the productive life. It is an estimate, and it is what a replant "
			"schedule and a depreciation life are both built on — a farm that has never written it "
			"down replants by surprise."
		)
	return notes


# ── get_block_cost_summary ──────────────────────────────────────────────────
def _window(args: dict, season: dict) -> tuple[str, str, str]:
	"""`(from_date, to_date, description)` — the season's year unless told otherwise."""
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		return from_date, to_date, f"{from_date} to {to_date}"
	# `is None`, not `or`: a stated `season_year: 0` used to fall through to the
	# planting's own year. It now reaches the refusal below, which is already worded
	# for exactly this caller.
	year = as_int(args, "season_year")
	if year is None:
		year = season.get("season_year") or season.get("plant_year")
	if not year:
		raise ToolError(
			"no window to sum over. Pass from_date and to_date, or season_year, or give the "
			"planting a season year."
		)
	return f"{year}-01-01", f"{year}-12-31", f"calendar {year}"


def _ledger_costs(season: dict, company: str, from_date: str, to_date: str) -> dict:
	"""Ledger cost against this block's cost center. The authoritative half.

	Debits less credits, so a credit note against the block reduces it rather
	than being reported as a separate positive figure somewhere else.
	"""
	out = {
		"measurable": False,
		"amount": 0.0,
		"entry_count": 0,
		"truncated": False,
		"by_account": {},
		"note": "",
	}
	cost_center = season.get("cost_center")
	if not cost_center:
		out["note"] = (
			"This planting has no cost center, so no ledger cost can be attributed to it. "
			"link_field_to_cost_center points the block at one; until then only the attribution "
			"rows below are visible."
		)
		return out
	if not compat.doctype_exists(GL_ENTRY):
		out["note"] = "This site has no GL Entry doctype."
		return out

	filters: dict = {
		"cost_center": cost_center,
		"is_cancelled": 0,
		"posting_date": ("between", [from_date, to_date]),
	}
	if company:
		filters["company"] = company
	try:
		rows = frappe.db.get_all(
			GL_ENTRY,
			filters=filters,
			fields=["name", "account", "debit", "credit", "voucher_no"],
			limit=GL_CAP + 1,
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		out["note"] = "The ledger could not be read on this site."
		return out

	rows = [dict(row) for row in rows or []]
	out["truncated"] = len(rows) > GL_CAP
	rows = rows[:GL_CAP]
	by_account: dict = {}
	total = 0.0
	for row in rows:
		amount = _number(row.get("debit")) - _number(row.get("credit"))
		total += amount
		account = str(row.get("account") or "")
		by_account[account] = round(by_account.get(account, 0.0) + amount, 2)
	out["measurable"] = True
	out["amount"] = round(total, 2)
	out["entry_count"] = len(rows)
	out["by_account"] = dict(sorted(by_account.items(), key=lambda pair: -abs(pair[1])))
	if out["truncated"]:
		out["note"] = (
			f"More than {GL_CAP} ledger rows in this window, so the total above is of the first "
			f"{GL_CAP} only and is UNDERSTATED. Narrow the window."
		)
	return out


def _overlap(rows: list[dict]) -> dict:
	"""Which attribution rows are already in the ledger figure, and which may be.

	    THREE BUCKETS, and the middle one is the honest part. A row whose `source` is
	    'GL Sweep' was built from a ledger entry and is definitely double counted. A
	    row with a `journal_entry` or a `gl_entry` on it is probably. A row with
	    neither is standalone — owner labour, an in-kind trade — and belongs in the
	    total.

	The middle bucket is reported rather than assumed either way. Including it
	silently inflates a block's costs; dropping it silently understates them;
	and a farm that is told which rows are in question can settle it in a minute
	from the voucher numbers.
	"""
	definite, probable, standalone = [], [], []
	for row in rows:
		if str(row.get("source") or "") == "GL Sweep":
			definite.append(row)
		elif row.get("journal_entry") or row.get("gl_entry"):
			probable.append(row)
		else:
			standalone.append(row)
	return {"definite": definite, "probable": probable, "standalone": standalone}


def get_block_cost_summary(args: dict) -> ToolResult:
	"""What one planting cost — from the ledger, from attributions, and the overlap."""
	_require(PLANTING_SEASON)
	company = resolve_company(as_str(args, "company"))
	season = _season_doc(as_str(args, "planting_season", required=True), company or "")
	described = _describe_season(season)
	company = company or described.get("company") or ""
	from_date, to_date, window = _window(args, described)

	ledger = _ledger_costs(described, company, from_date, to_date)

	attributions: list[dict] = []
	if compat.doctype_exists(COST_ENTRY):
		filters: dict = {
			"planting_season": described["name"],
			"posting_date": ("between", [from_date, to_date]),
		}
		rows = frappe.db.get_all(
			COST_ENTRY,
			filters=filters,
			fields=compat.existing_fields(COST_ENTRY, _COST_FIELDS),
			order_by="posting_date asc",
			limit=LIST_CAP * 5,
		)
		attributions = [dict(row) for row in rows or []]

	buckets = _overlap(attributions)
	standalone_total = round(sum(_number(row.get("amount")) for row in buckets["standalone"]), 2)
	probable_total = round(sum(_number(row.get("amount")) for row in buckets["probable"]), 2)
	definite_total = round(sum(_number(row.get("amount")) for row in buckets["definite"]), 2)

	by_category: dict = {}
	capitalised = expensed = 0.0
	for row in attributions:
		amount = _number(row.get("amount"))
		category = str(row.get("cost_category") or "Other")
		by_category[category] = round(by_category.get(category, 0.0) + amount, 2)
		if compat.checked(row.get("capitalized")):
			capitalised += amount
		else:
			expensed += amount

	total = round(_number(ledger["amount"]) + standalone_total, 2)
	acres = described["acres"]

	return ToolResult(
		data={
			"planting_season": described["name"],
			"season_label": described["season_label"],
			"field": described["field"],
			"crop": described["crop"],
			"status": described["status"],
			"lifecycle": described["lifecycle"],
			"leaf_year": described["leaf_year"],
			"acres": acres,
			"window": window,
			"from_date": from_date,
			"to_date": to_date,
			"ledger": ledger,
			"attributions": {
				"count": len(attributions),
				"standalone_total": standalone_total,
				"probably_already_in_ledger": probable_total,
				"definitely_already_in_ledger": definite_total,
				"by_cost_category": dict(sorted(by_category.items(), key=lambda p: -abs(p[1]))),
				"capitalised": round(capitalised, 2),
				"expensed": round(expensed, 2),
				"rows": attributions,
			},
			"total_cost": total,
			"total_cost_basis": (
				"ledger cost against the block's cost center, PLUS attribution rows that have no "
				"ledger origin. Rows marked 'probably already in ledger' are EXCLUDED from this "
				"total and reported separately — including them would double count and excluding "
				"them silently would understate, so they are named instead."
			),
			"cost_per_acre": round(total / acres, 2) if acres else None,
			"cost_per_tree": (
				round(total / described["trees_planted"], 4) if described["trees_planted"] else None
			),
			"notes": _cost_notes(described, ledger, buckets, probable_total),
		},
		summary=(
			f"{described['season_label'] or described['name']}: {total:,.2f} over {window}"
			+ (f" ({round(total / acres, 2):,.2f}/ac)" if acres else "")
		),
	)


def _cost_notes(season, ledger, buckets, probable_total) -> list[str]:
	notes = []
	if ledger.get("note"):
		notes.append(ledger["note"])
	if buckets["probable"]:
		names = ", ".join(str(row.get("name")) for row in buckets["probable"][:5])
		notes.append(
			f"{len(buckets['probable'])} attribution row(s) totalling {probable_total:,.2f} carry a "
			f"journal entry or GL entry reference, so they are probably already inside the ledger "
			f"figure. They are EXCLUDED from the total and listed here instead: {names}"
			+ ("…" if len(buckets["probable"]) > 5 else "")
			+ ". Check the vouchers and either clear the reference or drop the row."
		)
	if buckets["definite"]:
		notes.append(
			f"{len(buckets['definite'])} row(s) were swept from the ledger and are excluded from "
			"the total, which counts the ledger directly."
		)
	if season["status"] == ESTABLISHING:
		notes.append(
			"This block is Establishing, so this figure is establishment cost rather than a cost "
			"of production. It generally capitalises into the block's basis under IRC 263A, and "
			"reading it as a loss against a crop that does not exist yet is the single most common "
			"way a tree fruit operation misreads its own numbers."
		)
	if not season["acres"]:
		notes.append("No acres on this planting, so no per-acre figure could be given.")
	return notes


# ── get_block_revenue_summary ───────────────────────────────────────────────
def get_block_revenue_summary(args: dict) -> ToolResult:
	"""What one planting returned, and on what basis it was attributed."""
	_require(PLANTING_SEASON)
	company = resolve_company(as_str(args, "company"))
	season = _season_doc(as_str(args, "planting_season", required=True), company or "")
	described = _describe_season(season)
	company = company or described.get("company") or ""
	from_date, to_date, window = _window(args, described)

	entries: list[dict] = []
	if compat.doctype_exists(REVENUE_ENTRY):
		rows = frappe.db.get_all(
			REVENUE_ENTRY,
			filters={
				"planting_season": described["name"],
				"posting_date": ("between", [from_date, to_date]),
			},
			fields=compat.existing_fields(REVENUE_ENTRY, _REVENUE_FIELDS),
			order_by="posting_date asc",
			limit=LIST_CAP * 5,
		)
		entries = [dict(row) for row in rows or []]

	total = round(sum(_number(row.get("amount")) for row in entries), 2)
	quantity = round(sum(_number(row.get("quantity")) for row in entries), 3)
	by_type: dict = {}
	by_basis: dict = {}
	for row in entries:
		amount = _number(row.get("amount"))
		revenue_type = str(row.get("revenue_type") or "Other")
		basis = str(row.get("allocation_basis") or "Direct")
		by_type[revenue_type] = round(by_type.get(revenue_type, 0.0) + amount, 2)
		by_basis[basis] = round(by_basis.get(basis, 0.0) + amount, 2)

	tickets = _scale_tickets(described, company, from_date, to_date)
	acres = described["acres"]
	uoms = sorted({str(row.get("quantity_uom") or "") for row in entries} - {""})

	notes = []
	if not entries:
		notes.append(
			"No revenue attributed to this planting in this window. A settlement covering several "
			"blocks is one ledger transaction and no attribution — the attribution has to be "
			"written, and the Scale Tickets below are the evidence for doing it on delivered "
			"weight."
		)
	if len(uoms) > 1:
		notes.append(
			f"Quantities are in more than one unit ({', '.join(uoms)}), so the quantity total above "
			"is a sum of unlike things and the price per unit derived from it means nothing. Report "
			"per unit, or convert first."
		)
	unattributed = [t for t in tickets if not t.get("attributed")]
	if unattributed:
		notes.append(
			f"{len(unattributed)} Scale Ticket(s) name this block in the window with no revenue "
			"entry pointing at them. Those are deliveries whose return has not been attributed "
			"back to the ground that grew them."
		)
	if described["status"] == ESTABLISHING and total:
		notes.append(
			"This block is Establishing and has returned revenue. That is ordinary in the first "
			"bearing year and it is also the signal to consider moving the status to Productive — "
			"costs on an Establishing block are still capitalising."
		)
	notes.append(
		"This is an ATTRIBUTION of revenue the ledger already recognised, not a recognition of "
		"revenue. It answers 'which ground earned it', not 'what did the business earn'."
	)

	return ToolResult(
		data={
			"planting_season": described["name"],
			"season_label": described["season_label"],
			"field": described["field"],
			"crop": described["crop"],
			"variety": described["variety"],
			"status": described["status"],
			"acres": acres,
			"window": window,
			"from_date": from_date,
			"to_date": to_date,
			"total_revenue": total,
			"revenue_per_acre": round(total / acres, 2) if acres else None,
			"total_quantity": quantity,
			"quantity_uoms": uoms,
			"yield_per_acre": round(quantity / acres, 3) if acres and quantity else None,
			"price_per_unit": round(total / quantity, 4) if quantity else None,
			"by_revenue_type": dict(sorted(by_type.items(), key=lambda p: -abs(p[1]))),
			"by_allocation_basis": by_basis,
			"entry_count": len(entries),
			"entries": entries,
			"scale_tickets": tickets,
			"notes": notes,
		},
		summary=(
			f"{described['season_label'] or described['name']}: {total:,.2f} over {window}"
			+ (f" ({round(total / acres, 2):,.2f}/ac)" if acres else "")
		),
	)


def _scale_tickets(season: dict, company: str, from_date: str, to_date: str) -> list[dict]:
	"""Deliveries off this block in the window — the evidence for a weight share.

	Read rather than written. This tool does not create attributions; it shows
	what the attribution would be based on, because splitting a pool is a
	judgement and the judgement belongs to a person.
	"""
	if not compat.doctype_exists(SCALE_TICKET):
		return []
	filters: dict = {"field": season["field"], "date": ("between", [from_date, to_date])}
	if company:
		filters["company"] = company
	if season.get("block_name"):
		filters["block"] = season["block_name"]
	try:
		rows = frappe.db.get_all(
			SCALE_TICKET,
			filters=filters,
			fields=compat.existing_fields(
				SCALE_TICKET,
				(
					"name",
					"ticket_number",
					"date",
					"field",
					"block",
					"variety",
					"grade",
					"net_weight",
					"weight_uom",
					"settlement",
					"status",
				),
			),
			order_by="date asc",
			limit=LIST_CAP,
		)
	except Exception:  # pragma: no cover
		return []
	tickets = [dict(row) for row in rows or []]
	if not tickets or not compat.doctype_exists(REVENUE_ENTRY):
		return tickets
	names = [t["name"] for t in tickets]
	try:
		attributed = {
			str(dict(row).get("scale_ticket"))
			for row in frappe.db.get_all(
				REVENUE_ENTRY,
				filters={"scale_ticket": ("in", names)},
				fields=["scale_ticket"],
				limit=LIST_CAP * 2,
			)
			or []
		}
	except Exception:  # pragma: no cover
		attributed = set()
	for ticket in tickets:
		ticket["attributed"] = ticket["name"] in attributed
	return tickets


# ── get_block_profitability ─────────────────────────────────────────────────
def get_block_profitability(args: dict) -> ToolResult:
	"""Cost against return for one planting — read against its own lifecycle.

	AN ESTABLISHING BLOCK'S NEGATIVE MARGIN IS NOT A LOSS AND THIS TOOL REFUSES
	TO CALL IT ONE. A fourth-leaf cherry block that spent $180,000 and returned
	$4,000 has not lost $176,000; it has invested it, and the investment is
	carried in the block's basis. So `is_meaningful` is false for an establishing
	planting and the verdict says why. Every figure is still reported — the
	numbers are real and somebody needs them — but the app does not put the word
	'loss' next to them.

	`cumulative` IS THE POINT ON A PERENNIAL. A single season of a block that
	takes fifteen years to pay back is not an answer to any question anybody has.
	When `cumulative` is set, every planting on the same field with the same crop
	and plant year is summed, which is the block's whole life to date.
	"""
	_require(PLANTING_SEASON)
	company = resolve_company(as_str(args, "company"))
	season = _season_doc(as_str(args, "planting_season", required=True), company or "")
	described = _describe_season(season)
	company = company or described.get("company") or ""
	cumulative = bool(as_bool(args, "cumulative", False))

	if cumulative:
		siblings = frappe.db.get_all(
			PLANTING_SEASON,
			filters={
				"field": described["field"],
				"crop": described["crop"],
				"plant_year": described["plant_year"],
			},
			pluck="name",
			limit=100,
		)
		scope = sorted(set(siblings or []) | {described["name"]})
		from_date = f"{described['plant_year']}-01-01"
		to_date = str(frappe.utils.nowdate())
		window = f"planting life to date ({from_date} to {to_date})"
	else:
		scope = [described["name"]]
		from_date, to_date, window = _window(args, described)

	cost_total = revenue_total = 0.0
	capitalised = 0.0
	quantity = 0.0
	per_season = []
	for name in scope:
		row = _season_doc(name)
		one = _describe_season(row)
		costs = get_block_cost_summary(
			{
				"planting_season": name,
				"company": company,
				"from_date": from_date,
				"to_date": to_date,
			}
		).data
		revenue = get_block_revenue_summary(
			{
				"planting_season": name,
				"company": company,
				"from_date": from_date,
				"to_date": to_date,
			}
		).data
		cost_total += _number(costs.get("total_cost"))
		capitalised += _number((costs.get("attributions") or {}).get("capitalised"))
		revenue_total += _number(revenue.get("total_revenue"))
		quantity += _number(revenue.get("total_quantity"))
		per_season.append(
			{
				"planting_season": name,
				"season_label": one["season_label"],
				"season_year": one["season_year"],
				"status": one["status"],
				"cost": _number(costs.get("total_cost")),
				"revenue": _number(revenue.get("total_revenue")),
				"margin": round(_number(revenue.get("total_revenue")) - _number(costs.get("total_cost")), 2),
			}
		)

	cost_total = round(cost_total, 2)
	revenue_total = round(revenue_total, 2)
	margin = round(revenue_total - cost_total, 2)
	acres = described["acres"]
	establishing = described["status"] == ESTABLISHING
	meaningful = not establishing

	if establishing:
		verdict = (
			f"NOT A PROFIT AND LOSS. This planting is Establishing in leaf year "
			f"{described['leaf_year'] or '?'} — it is not expected to return anything yet, and the "
			f"{abs(margin):,.2f} it is down is establishment INVESTMENT carried in the block's "
			"basis, not a loss against a crop. Judge it against the establishment budget and the "
			"date it is planned to bear, not against zero."
		)
	elif described["status"] == REMOVED:
		verdict = (
			"This planting has been removed. The figures are its final ones; a block's whole-life "
			"return is read with cumulative=true."
		)
	elif margin >= 0:
		verdict = (
			f"Returned {margin:,.2f} over cost"
			+ (f", {round(margin / acres, 2):,.2f} per acre" if acres else "")
			+ f", on a {described['status'].lower()} planting in leaf year "
			f"{described['leaf_year'] or '?'}."
		)
	else:
		verdict = (
			f"Cost {abs(margin):,.2f} more than it returned"
			+ (f", {round(abs(margin) / acres, 2):,.2f} per acre" if acres else "")
			+ f", on a {described['status'].lower()} planting. On a Declining block that is the "
			"signal a replant decision is due; on a Productive one it is a season to explain."
		)

	notes = []
	if not acres:
		notes.append("No acres on this planting, so no per-acre figure could be given.")
	if not described["cost_center"]:
		notes.append(
			"No cost center, so the ledger contributes nothing to the cost side and this margin is "
			"built from attribution rows alone. It is almost certainly understating cost."
		)
	if capitalised:
		notes.append(
			f"{capitalised:,.2f} of the cost above is marked Capitalised. It is included in the "
			"cost figure because the question here is what the ground consumed; for a profit and "
			"loss reading, that portion belongs in the block's basis rather than against the "
			"season."
		)
	if not cumulative and described["lifecycle"] == PERENNIAL:
		notes.append(
			"This is one season of a perennial. A block that takes years to pay back cannot be "
			"judged on one of them — pass cumulative=true for the planting's life to date."
		)
	if revenue_total and not quantity:
		notes.append("Revenue is attributed but no quantity is, so no price per unit could be given.")

	clock = timezones.Renderer(args)
	return ToolResult(
		data={
			"planting_season": described["name"],
			"season_label": described["season_label"],
			"field": described["field"],
			"crop": described["crop"],
			"variety": described["variety"],
			"status": described["status"],
			"lifecycle": described["lifecycle"],
			"plant_year": described["plant_year"],
			"leaf_year": described["leaf_year"],
			"acres": acres,
			"cumulative": cumulative,
			"window": window,
			"from_date": from_date,
			"to_date": to_date,
			"seasons_included": scope,
			"total_cost": cost_total,
			"total_revenue": revenue_total,
			"margin": margin,
			"capitalised_cost_included": round(capitalised, 2),
			"cost_per_acre": round(cost_total / acres, 2) if acres else None,
			"revenue_per_acre": round(revenue_total / acres, 2) if acres else None,
			"margin_per_acre": round(margin / acres, 2) if acres else None,
			"total_quantity": round(quantity, 3),
			"cost_per_unit": round(cost_total / quantity, 4) if quantity else None,
			"is_meaningful_as_profit_and_loss": meaningful,
			"verdict": verdict,
			"per_season": per_season,
			"notes": notes,
			"timezone": clock.block(),
		},
		summary=(
			f"{described['season_label'] or described['name']}: "
			f"cost {cost_total:,.2f}, revenue {revenue_total:,.2f}, margin {margin:,.2f}"
			+ (" (establishing — not a P&L)" if establishing else "")
		),
	)
