# SPDX-License-Identifier: MIT
"""Agricultural master data: what is grown, where it is sold, and in what units.

v0.82.0. Three registers that everything else in this app has been assuming and
none of it could name. A spray gate needs to know a crop's pre-harvest interval;
a breakeven needs to know what a market's grades pay; a scale ticket needs to
know what a bin weighs. Until this module all three took the answer from whoever
was calling, which means the site could not tell a considered figure from a
plausible one.

NONE OF THESE THREE IS COMPANY-SCOPED, AND EACH FOR ITS OWN REASON. It is worth
being explicit because `company` is accepted-and-reported-as-not-applied on
several tools in this app, and here it is not accepted at all:

  * **A Crop is a species.** A sweet cherry is a sweet cherry on both sides of a
    corporate boundary. `Field.crop` is where a planting is recorded, and it
    stays free text — see below.
  * **A Market is a place in the world.** Two growers shipping into the Pacific
    Northwest fresh cherry market are shipping into ONE market. Per-company
    copies would give the site two answers to what a No. 1 is.
  * **A unit is a unit.** A bin holds what it holds regardless of whose name is
    on the bin.

THIS DOES NOT REPLACE `Field.crop`, AND NOTHING HERE MAKES IT A LINK. That
column has been free text since it shipped, on the shipped grounds that "a block
of table grapes is not a schema change". A Link beside it would give a site two
answers to what grows on a block with no rule about which wins, and it would make
migrate ORDER load-bearing for every other app that records a crop as a string.
The upgrade, if somebody wants it, is a patch that seeds Crop rows from the
distinct strings already on the site and only then changes the column — a
release of its own, not a field option.

THE PHI IS REPORTED WITH WHAT IT IS NOT. `Crop.default_phi_days` is the crop's
own floor for when nothing more specific is known. The binding interval is on the
label of the material actually applied and it ranges, on one crop, from zero days
to thirty. Every tool here that reports a PHI says that in the payload rather
than leaving a model to infer that a crop-level number is the answer — because a
gate that cleared fruit a label would have held is the single most expensive way
this data can be wrong.

BLANK AND ZERO ARE DIFFERENT ANSWERS, EVERYWHERE IN THIS MODULE. A crop with
`default_phi_days` of 0 has genuinely no interval; a crop with none recorded is
one nobody has checked. `list_crops` reports the second as a gap rather than
folding it into the first, which is the same judgement `list_irrigation_zones`
makes about water tests.

CONVERSIONS ARE RESOLVED, NOT LOOKED UP. `get_uom_conversions` will use a
crop-specific row, fall back to a generic one, invert a row recorded the other
way round, or chain two rows through a pivot unit — and it always says which of
those it did. A factor handed back without its provenance is a number the caller
cannot check, and the numbers here are the ones that multiply into settlements.
"""

import frappe

from .. import ag_uom, compat
from ..args import as_bool, as_choice, as_float, as_int, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

CROP = "Crop"
CROP_VARIETY = "Crop Variety"
CROP_WATER = "Crop Water Requirement"
#: v0.114.0. The two per-variety overlays. Both hang off CROP and name their
#: variety as text — Frappe has no nested child tables and Crop Variety is
#: itself a child, so a table on the variety row is not a thing that can exist.
CROP_VARIETY_WATER = "Crop Variety Water Requirement"
CROP_VARIETY_PROTOCOL = "Crop Variety Protocol"
MARKET = "Market"
GRADE_STANDARD = "Market Grade Standard"
UOM_CONTEXT = "Agricultural UOM Context"
UOM_CONVERSION = "Agricultural UOM Conversion"

#: The hard cap on any register read here, matching `farm.REGISTER_CAP`. These
#: are master registers — a site with more than five hundred crops has a data
#: problem this tool should not paper over by paging.
REGISTER_CAP = 500

#: How many conversion rows one resolution will consider when chaining. A farm's
#: whole conversion book is a few dozen rows; past this the graph walk is
#: answering a question about somebody's import, not about units.
CONVERSION_CAP = 500

_CROP_FIELDS = (
	"name",
	"crop_name",
	"scientific_name",
	"crop_type",
	"growth_cycle",
	"days_to_harvest",
	"harvest_window_start",
	"harvest_window_end",
	"default_phi_days",
	"is_organic_certified",
	"pct_direct_marketed",
	"notes",
)

_MARKET_FIELDS = (
	"name",
	"market_name",
	"market_type",
	"region",
	"country",
	"currency",
	"primary_commodity",
	"shipping_point",
	"is_active",
	"notes",
)

_CONVERSION_FIELDS = (
	"name",
	"from_uom",
	"to_uom",
	"crop",
	"factor",
	"basis",
	"source",
	"is_active",
	"notes",
)

#: The sentence every tool that reports a PHI carries. Written once because it
#: is the SAME caveat in four places, and four copies is how one of them comes
#: to say something slightly weaker than the others.
PHI_CAVEAT = (
	"default_phi_days is the crop's own floor, used when nothing more specific is known. The "
	"BINDING pre-harvest interval is the one printed on the label of the material actually "
	"applied, and on a single crop it ranges from zero days to thirty. A gate that reads this "
	"number and stops reading will clear fruit that a label would hold."
)

#: v0.114.0. The sentence every tool that reports a variety's rootstock carries.
#: The catalogue holds ONE rootstock per variety and a farm has the same variety
#: on several — so this column is a default and the planting is the answer. Said
#: in the payload rather than left to a caller to know, because a per-acre yield
#: quoted against the wrong rootstock is not comparable to anything and nothing
#: in the number itself shows which one it was.
ROOTSTOCK_CAVEAT = (
	"A variety's `rootstock` here is the CATALOGUE DEFAULT and not the binding answer. A crop's "
	"variety list has one row per variety, so it can hold one rootstock for 'Bing' while the "
	"farm has Bing on Mazzard in the old block and Bing on Gisela 6 in the 2019 planting — "
	"different trees, different vigour, different density, different yields. The binding "
	"rootstock is on the planting: `Planting Season.rootstock` for a block-year and "
	"`Field.rootstock` for the block. Read those before comparing anything per acre."
)

#: v0.114.0. How the two per-variety tables resolve, for every payload that
#: returns them raw. A caller handed an override table and a crop table with no
#: rule between them will pick one, and half of them will pick the wrong one.
OVERLAY_CAVEAT = (
	"`variety_water_overrides` and `variety_protocols` are SPARSE OVERLAYS on the crop, not "
	"complete per-variety records. A stage no override names falls back to the crop's own "
	"water requirement, and the fallback is PER FIELD: a row overriding only the Kc leaves the "
	"crop's weekly depth standing. Call get_variety_care_recipe for one variety's resolved "
	"schedule with each number's source attached, rather than resolving these two tables by hand."
)

#: v0.114.0. What a protocol is and is not, on every payload that returns one.
#: A plan and a record look identical in a JSON list, and reporting an intention
#: as a completed application is a compliance answer wrong in the direction that
#: costs the most.
PROTOCOL_CAVEAT = (
	"A protocol step is what the farm INTENDS for this variety in a normal year. It is not a "
	"record that anything was applied, and it is not a label: what actually went onto a block, "
	"at what rate, on what date and by whom is a Spray Application. Rates here are text with "
	"their units because ppm, pints per acre and quarts per hundred gallons do not convert "
	"without a dilution — the binding rate is always the label's."
)

#: What `null` means on a PHI, said outright. `0` and "nobody recorded one" are
#: different facts and the difference is the whole reason the column is nullable.
PHI_BLANK_NOTE = (
	"A null default_phi_days means no interval has been recorded for this crop — which is not "
	"the same as an interval of zero, and is reported separately for that reason."
)


# ── shared resolution ───────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _one_of(doctype: str, wanted: str, label: str, fields: tuple = ("name",)) -> dict:
	"""One master row, from its docname or a case-insensitive match on it.

	Case-insensitive because these docnames are typed by people and by models
	from prose — "sweet cherry" and "Sweet Cherry" are the same crop, and
	refusing the first would be refusing the way the name is actually said. An
	exact hit always wins, so a site that genuinely has two rows differing only
	in case still resolves each of them precisely.

	`fields` IS EXPLICIT RATHER THAN `"*"`. Not a style preference: this is used
	against Country and Currency as well as this app's own doctypes, and asking a
	foreign master for every column it has means the return shape changes with
	somebody else's release. Passing the columns this app actually reads makes
	the caller's expectations checkable, which `compat.existing_fields` then
	narrows again to what the site really has.
	"""
	wanted = str(wanted or "").strip()
	if not wanted:
		raise ToolError(f"{label} is required.")

	columns = compat.existing_fields(doctype, fields) or ["name"]

	def _load(name: str) -> dict:
		row = dict(frappe.db.get_value(doctype, name, columns, as_dict=True) or {})
		row["name"] = name
		return row

	if frappe.db.exists(doctype, wanted):
		return _load(wanted)

	# No exact docname. Fall back to a fold-case scan of the register, which is
	# small by construction — these are masters, not transactions.
	candidates = frappe.db.get_all(doctype, pluck="name", limit=REGISTER_CAP)
	matches = [name for name in candidates if str(name).casefold() == wanted.casefold()]
	if len(matches) == 1:
		return _load(matches[0])
	if len(matches) > 1:
		raise ToolError(
			f"{wanted!r} matches {len(matches)} {doctype} records differing only in case "
			f"({', '.join(sorted(matches))}). Pass the docname exactly as it is written."
		)

	known = sorted(candidates)[:25]
	raise ToolError(
		f"no {doctype} called {wanted!r}. "
		+ (f"This site has: {', '.join(known)}." if known else f"This site has no {doctype} records yet.")
	)


def _table(doc, fieldname: str) -> list[dict]:
	"""A document's child rows as plain dicts, whichever shape they arrive in.

	A row that has just been `append`ed and a row that came back off a
	`frappe.get_doc` are not the same object, and only one of them answers
	`as_dict()`. Every echo in this module goes through here so a create and an
	update return the SAME shape — which they did not, briefly, and the tests
	that caught it are the ones asserting on `varieties` after each.
	"""
	out = []
	for row in doc.get(fieldname) or []:
		out.append(dict(row.as_dict()) if hasattr(row, "as_dict") else dict(row))
	return out


def _months_in_window(start: str, end: str) -> list[str]:
	"""The months a harvest window covers, inclusive, wrapping the year end.

	Spelled out rather than left as two endpoints because the wrap is the case a
	reader gets wrong: a window of November to February is four months, not
	minus-eight, and a caller doing the subtraction itself will get that answer
	roughly half the time.
	"""
	months = [
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
	]
	start = str(start or "").strip()
	end = str(end or "").strip()
	if start not in months or end not in months:
		return []
	first, last = months.index(start), months.index(end)
	if first <= last:
		return months[first : last + 1]
	return months[first:] + months[: last + 1]


def _child_rows(doctype: str, parent: str, parentfield: str, fields: tuple) -> list[dict]:
	rows = frappe.db.get_all(
		doctype,
		filters={"parent": parent, "parentfield": parentfield},
		fields=list(fields),
		order_by="idx asc",
		limit=REGISTER_CAP,
	)
	return [dict(row) for row in rows]


def _describe_crop(row: dict) -> dict:
	phi = row.get("default_phi_days")
	return {
		"name": row.get("name"),
		"crop_name": row.get("crop_name"),
		"scientific_name": row.get("scientific_name") or None,
		"crop_type": row.get("crop_type"),
		"growth_cycle": row.get("growth_cycle"),
		"days_to_harvest": int(row["days_to_harvest"]) if row.get("days_to_harvest") else None,
		"harvest_window_start": row.get("harvest_window_start") or None,
		"harvest_window_end": row.get("harvest_window_end") or None,
		"harvest_months": _months_in_window(row.get("harvest_window_start"), row.get("harvest_window_end")),
		"default_phi_days": int(phi) if phi not in (None, "") else None,
		"is_organic_certified": compat.checked(row.get("is_organic_certified")),
		"pct_direct_marketed": _share_or_none(row.get("pct_direct_marketed")),
		"pct_direct_marketed_source": (
			"typed on the Crop record — an assertion, not a figure computed from invoices"
			if _share_or_none(row.get("pct_direct_marketed")) is not None
			else None
		),
		"notes": row.get("notes") or None,
	}


def _share_or_none(value):
	"""A stored percentage, or None where nobody has estimated one.

	NOT `float(value or 0)`. Zero percent direct-marketed is a real answer from a
	grower who ships everything to a packer, and "nobody has worked it out" is a
	different one. Collapsing them would let a survey report a farm that has never
	been asked as a farm that sells nothing direct.
	"""
	if value in (None, ""):
		return None
	return round(float(value), 2)


def _share(args: dict, key: str):
	"""A 0-to-100 percentage argument, or None when it was not passed.

	Bounded here as well as in the controller because the two catch it at
	different moments: a tool call gets a sentence naming the argument, and a
	Desk save gets the controller's. The error being caught in both is 0.35 meant
	as thirty-five percent, which is plausible enough on the page to survive a
	review.
	"""
	if key not in args or args.get(key) in (None, ""):
		return None
	share = as_float(args.get(key), key)
	if not 0 <= share <= 100:
		raise ToolError(
			f"{key} is {share}. A share runs from 0 to 100 — {share} is either a fraction "
			"that wants multiplying by a hundred or a decimal point in the wrong place."
		)
	return share


def _describe_market(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"market_name": row.get("market_name"),
		"market_type": row.get("market_type"),
		"region": row.get("region") or None,
		"country": row.get("country") or None,
		"currency": row.get("currency") or None,
		"primary_commodity": row.get("primary_commodity") or None,
		"shipping_point": row.get("shipping_point") or None,
		"is_active": compat.checked(row.get("is_active")),
		"notes": row.get("notes") or None,
	}


def _stage(changes: dict, doc, field: str, wanted) -> None:
	"""Set `field` and record before → after, skipping a value that is unchanged."""
	before = doc.get(field)
	before = "" if before is None else before
	if str(before) == str(wanted):
		return
	changes[field] = [before or None, wanted or None]
	doc.set(field, wanted if wanted != "" else None)


# ── the variety overlay ─────────────────────────────────────────────────────
#
# v0.114.0. A crop records water demand by growth stage, and a variety may
# depart from it. THE OVERLAY IS SPARSE AND THE RESOLUTION IS PER FIELD, not per
# row: a variety that overrides only the Kc at Harvest gets the crop's weekly
# depth at Harvest and the crop's everything at the other six stages. Anything
# coarser would make an override a commitment to restate the whole schedule,
# which is how the restatements drift apart from the original.
#
# EVERY RESOLVED NUMBER TRAVELS WITH WHERE IT CAME FROM, the same rule
# `get_uom_conversions` follows for a factor and for the same reason: a caller
# handed 0.6 cannot tell a variety's considered figure from its crop's default,
# and those are different facts about the block in front of them.
#
# BLANK IS NOT ZERO, HERE MOST OF ALL. A variety row with an empty Kc is a
# variety with no opinion about Kc and the crop's stands. A variety row with 0.0
# is a variety that genuinely uses no water at that stage. Collapsing the two
# would let an override nobody finished silently water a block at zero.

#: The stages in the order a season runs through them, which is the order any
#: reader of a schedule expects. Sorting these alphabetically puts Bloom before
#: Bud Break and Dormant in the middle, which reads as a corrupted record.
#: Sourced from the Select on both water tables — if that list grows, a stage
#: missing from here still resolves and simply sorts last, rather than vanishing.
STAGE_ORDER = (
	"Dormant",
	"Bud Break",
	"Bloom",
	"Fruit Set",
	"Fruit Development",
	"Harvest",
	"Post-Harvest",
)


def _stage_key(stage: str) -> tuple:
	"""Sort key putting known stages in season order and unknown ones last."""
	try:
		return (0, STAGE_ORDER.index(stage))
	except ValueError:
		return (1, stage)


def _number_or_none(value):
	"""A stored float, or None where the cell is empty.

	`float(value or 0)` is wrong on both of these columns — see the note above
	about blank and zero — and it is wrong quietly, which is why this exists
	rather than being written out at each of the four call sites.
	"""
	if value in (None, ""):
		return None
	return float(value)


def _variety_water_overrides(crop: str) -> list[dict]:
	if not compat.doctype_exists(CROP_VARIETY_WATER):
		return []
	return _child_rows(
		CROP_VARIETY_WATER,
		crop,
		"variety_water_requirements",
		("variety", "growth_stage", "crop_coefficient_kc", "water_inches_per_week", "notes"),
	)


def _variety_protocols(crop: str) -> list[dict]:
	if not compat.doctype_exists(CROP_VARIETY_PROTOCOL):
		return []
	return _child_rows(
		CROP_VARIETY_PROTOCOL,
		crop,
		"variety_protocols",
		("variety", "practice", "timing_stage", "timing_detail", "product", "rate", "notes"),
	)


def _for_variety(rows: list[dict], variety: str) -> list[dict]:
	"""The overlay rows naming `variety`, matched ignoring case.

	`crop.py` writes the catalogue's spelling back onto every overlay row on
	save, so on a record saved under this release the match is exact anyway.
	Casefolding here as well covers the rows a `bench migrate` or a direct
	`db.set_value` put in without going through the controller.
	"""
	wanted = str(variety or "").strip().casefold()
	return [row for row in rows if str(row.get("variety") or "").strip().casefold() == wanted]


def resolve_variety_water(crop_stages: list[dict], overrides: list[dict], variety: str) -> list[dict]:
	"""The effective water schedule for one variety, per field, with provenance.

	`crop_stages` are the crop's own Crop Water Requirement rows; `overrides` is
	the whole overlay table, filtered here rather than by the caller so that the
	filtering rule lives in one place.

	A STAGE THE CROP DOES NOT RECORD BUT THE VARIETY DOES IS STILL RETURNED, with
	its crop-level figures reported as None. That is a variety adding a stage its
	crop never modelled, which is a real thing to record about a late variety,
	and dropping it would lose the only row that said so.
	"""
	by_stage = {str(row.get("growth_stage") or "").strip(): row for row in crop_stages}
	overridden = {str(row.get("growth_stage") or "").strip(): row for row in _for_variety(overrides, variety)}

	out = []
	for stage in sorted(set(by_stage) | set(overridden), key=_stage_key):
		base = by_stage.get(stage) or {}
		over = overridden.get(stage) or {}

		crop_kc = _number_or_none(base.get("crop_coefficient_kc"))
		crop_inches = _number_or_none(base.get("water_inches_per_week"))
		over_kc = _number_or_none(over.get("crop_coefficient_kc"))
		over_inches = _number_or_none(over.get("water_inches_per_week"))

		kc = crop_kc if over_kc is None else over_kc
		inches = crop_inches if over_inches is None else over_inches
		out.append(
			{
				"growth_stage": stage,
				"crop_coefficient_kc": kc,
				"crop_coefficient_kc_source": _source(over_kc, crop_kc),
				"water_inches_per_week": inches,
				"water_inches_per_week_source": _source(over_inches, crop_inches),
				"crop_default_kc": crop_kc,
				"crop_default_water_inches_per_week": crop_inches,
				"is_overridden": bool(over),
				# The variety's note where it has one, because it is the note
				# about the departure and the crop's is about the crop. Both are
				# returned rather than one hiding the other.
				"variety_notes": str(over.get("notes") or "").strip() or None,
				"crop_notes": str(base.get("notes") or "").strip() or None,
			}
		)
	return out


def _source(override, default) -> str | None:
	"""Which record a resolved number came from, or None where there is none."""
	if override is not None:
		return "variety override"
	if default is not None:
		return "crop default"
	return None


# ── 1. list_crops ───────────────────────────────────────────────────────────
def list_crops(args: dict) -> ToolResult:
	"""The crop register, with the varieties counted and the gaps named."""
	_require(CROP)
	limit = as_limit(args)

	filters = {}
	crop_type = as_str(args, "crop_type")
	if crop_type:
		filters["crop_type"] = as_choice(CROP, "crop_type", crop_type, "crop_type")
	growth_cycle = as_str(args, "growth_cycle")
	if growth_cycle:
		filters["growth_cycle"] = as_choice(CROP, "growth_cycle", growth_cycle, "growth_cycle")
	if "is_organic_certified" in args:
		filters["is_organic_certified"] = 1 if as_bool(args, "is_organic_certified") else 0

	rows = frappe.db.get_all(
		CROP,
		filters=filters,
		fields=compat.existing_fields(CROP, _CROP_FIELDS),
		order_by="crop_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	crops = [_describe_crop(dict(row)) for row in rows]

	variety_counts: dict = {}
	varieties_by_crop: dict = {}
	if crops and compat.doctype_exists(CROP_VARIETY):
		for row in frappe.db.get_all(
			CROP_VARIETY,
			filters={"parenttype": CROP, "parent": ("in", [crop["name"] for crop in crops])},
			fields=["parent", "variety_name"],
			limit=REGISTER_CAP,
		):
			variety_counts[row["parent"]] = variety_counts.get(row["parent"], 0) + 1
			varieties_by_crop.setdefault(row["parent"], []).append(row["variety_name"])

	for crop in crops:
		crop["variety_count"] = variety_counts.get(crop["name"], 0)
		crop["varieties"] = sorted(varieties_by_crop.get(crop["name"], []))

	by_type: dict = {}
	for crop in crops:
		key = crop["crop_type"] or "(unrecorded)"
		by_type[key] = by_type.get(key, 0) + 1

	without_phi = [crop["name"] for crop in crops if crop["default_phi_days"] is None]
	without_window = [crop["name"] for crop in crops if not crop["harvest_months"]]
	without_varieties = [crop["name"] for crop in crops if not crop["variety_count"]]
	without_direct_share = [crop["name"] for crop in crops if crop["pct_direct_marketed"] is None]

	return ToolResult(
		data={
			"crop_count": len(crops),
			"by_crop_type": dict(sorted(by_type.items())),
			"variety_count": sum(variety_counts.values()),
			"without_phi_recorded": without_phi,
			"without_harvest_window": without_window,
			"without_varieties": without_varieties,
			"without_direct_marketed_share": without_direct_share,
			"crops": crops,
			"phi_note": PHI_BLANK_NOTE,
			"limit": limit,
			"truncated": len(crops) >= min(limit, REGISTER_CAP),
		},
		summary=(
			f"{len(crops)} crop(s), {sum(variety_counts.values())} variety(ies), "
			f"{len(without_phi)} with no PHI recorded"
		),
	)


# ── 2. get_crop ─────────────────────────────────────────────────────────────
def get_crop(args: dict) -> ToolResult:
	"""One crop in full: varieties, water requirements, markets and conversions."""
	_require(CROP)
	row = _one_of(CROP, as_str(args, "crop", required=True), "crop", _CROP_FIELDS)
	described = _describe_crop(row)
	name = row["name"]

	varieties = (
		_child_rows(
			CROP_VARIETY,
			name,
			"varieties",
			("variety_name", "rootstock", "pollination_group", "expected_yield_per_acre", "maturity_years"),
		)
		if compat.doctype_exists(CROP_VARIETY)
		else []
	)
	water = (
		_child_rows(
			CROP_WATER,
			name,
			"water_requirements",
			("growth_stage", "crop_coefficient_kc", "water_inches_per_week", "notes"),
		)
		if compat.doctype_exists(CROP_WATER)
		else []
	)

	# v0.114.0. The two overlays, reported RAW here and resolved by
	# `get_variety_care_recipe`. This tool is the crop's own record and showing a
	# resolved schedule per variety would bury the crop's figures under seven
	# copies of themselves; what a reader needs at this level is which varieties
	# depart from the crop at all, and where.
	water_overrides = _variety_water_overrides(name)
	protocols = _variety_protocols(name)
	varieties_with_overrides = sorted(
		{str(row.get("variety") or "").strip() for row in water_overrides if row.get("variety")}
	)
	varieties_with_protocols = sorted(
		{str(row.get("variety") or "").strip() for row in protocols if row.get("variety")}
	)

	markets = []
	if compat.doctype_exists(MARKET):
		markets = [
			dict(market)
			for market in frappe.db.get_all(
				MARKET,
				filters={"primary_commodity": name},
				fields=["name", "market_type", "region", "is_active"],
				order_by="market_name asc",
				limit=REGISTER_CAP,
			)
		]

	conversions = []
	if compat.doctype_exists(UOM_CONVERSION):
		conversions = [
			dict(conv)
			for conv in frappe.db.get_all(
				UOM_CONVERSION,
				filters={"crop": name, "is_active": 1},
				fields=["name", "from_uom", "to_uom", "factor", "basis"],
				order_by="from_uom asc",
				limit=REGISTER_CAP,
			)
		]

	#: The pollination groups actually planted, which is the one cross-variety
	#: fact a block plan needs and no single row carries. A crop whose varieties
	#: are all in one group cannot set fruit for itself — reported, never
	#: refused, because a grower may be pollinating from a neighbouring block or
	#: with a variety they have not recorded here.
	groups = sorted(
		{str(v.get("pollination_group") or "").strip() for v in varieties if v.get("pollination_group")}
	)
	notes = []
	if described["default_phi_days"] is None:
		notes.append(
			f"No default PHI recorded for {described['crop_name']}. That is not the same as an "
			"interval of zero — a spray gate has nothing to fall back on for this crop."
		)
	if len(varieties) > 1 and len(groups) == 1:
		notes.append(
			f"All {len(varieties)} recorded varieties are in pollination group {groups[0]}. Varieties "
			"in one incompatibility group will not set fruit for each other. Reported, not refused: "
			"the pollinizer may be in a neighbouring block or simply unrecorded here."
		)
	if described["growth_cycle"] == "Perennial" and not any(v.get("maturity_years") for v in varieties):
		notes.append(
			"No variety records years to maturity, so the non-bearing development period for this "
			"perennial cannot be capitalised from this record."
		)

	return ToolResult(
		data={
			**described,
			"varieties": varieties,
			"variety_count": len(varieties),
			"pollination_groups": groups,
			"water_requirements": water,
			"water_stages_recorded": [stage["growth_stage"] for stage in water],
			"variety_water_overrides": water_overrides,
			"varieties_with_water_overrides": varieties_with_overrides,
			"variety_protocols": protocols,
			"varieties_with_protocols": varieties_with_protocols,
			"overlay_caveat": OVERLAY_CAVEAT,
			"markets": markets,
			"unit_conversions": conversions,
			"agronomy_notes": notes,
			"phi_caveat": PHI_CAVEAT,
			"rootstock_caveat": ROOTSTOCK_CAVEAT,
		},
		summary=(
			f"{described['crop_name']}: {len(varieties)} variety(ies), PHI "
			f"{described['default_phi_days'] if described['default_phi_days'] is not None else 'unrecorded'}"
		),
	)


# ── 3. create_crop ──────────────────────────────────────────────────────────
def create_crop(args: dict) -> ToolResult:
	"""Register one crop, with its varieties and water requirements."""
	_require(CROP)
	crop_name = as_str(args, "crop_name", required=True)

	if frappe.db.exists(CROP, crop_name):
		raise ToolError(
			f"{crop_name!r} is already registered. A crop is a master keyed on its name — "
			f"update it rather than creating a second one. Nothing was created."
		)

	doc = frappe.new_doc(CROP)
	doc.crop_name = crop_name
	doc.scientific_name = as_str(args, "scientific_name")
	doc.crop_type = as_choice(CROP, "crop_type", as_str(args, "crop_type", required=True), "crop_type")
	doc.growth_cycle = as_choice(
		CROP, "growth_cycle", as_str(args, "growth_cycle") or "Perennial", "growth_cycle"
	)
	doc.notes = as_str(args, "notes")

	for key in ("days_to_harvest", "default_phi_days"):
		value = as_int(args, key)
		if value is not None:
			doc.set(key, value)
	for key in ("harvest_window_start", "harvest_window_end"):
		value = as_str(args, key)
		if value:
			doc.set(key, as_choice(CROP, key, value, key))
	organic = as_bool(args, "is_organic_certified")
	if organic is not None:
		doc.is_organic_certified = 1 if organic else 0
	direct = _share(args, "pct_direct_marketed")
	if direct is not None:
		doc.pct_direct_marketed = direct

	for row in _variety_rows(args.get("varieties")):
		doc.append("varieties", row)
	for row in _water_rows(args.get("water_requirements")):
		doc.append("water_requirements", row)

	doc.insert(ignore_permissions=True)
	described = _describe_crop(dict(doc.as_dict()))

	warnings = []
	if described["default_phi_days"] is None:
		warnings.append(
			"No default PHI recorded. A spray gate asking this crop for a pre-harvest interval "
			"will get nothing, which is different from getting zero."
		)
	if not described["harvest_months"]:
		warnings.append("No harvest window recorded, so nothing can schedule against this crop's season.")
	if not doc.varieties:
		warnings.append(
			"No varieties recorded. Packouts and yields grouped by variety will have nothing to group."
		)

	return ToolResult(
		data={
			**described,
			"varieties": _table(doc, "varieties"),
			"water_requirements": _table(doc, "water_requirements"),
			"warnings": warnings,
			"phi_caveat": PHI_CAVEAT,
		},
		summary=(
			f"registered crop {doc.name} ({described['crop_type']}, {len(doc.varieties or [])} variety(ies))"
		),
		docstatus_delta="none → 0 (created)",
	)


def _variety_rows(raw) -> list[dict]:
	"""Validate and normalise the `varieties` argument.

	The whole list is checked before any of it is used, so a bad row at position
	four cannot leave rows one to three appended to a document that then fails —
	the same rule `import_farm_app_fields` follows and for the same reason.
	"""
	if raw in (None, ""):
		return []
	if not isinstance(raw, list):
		raise ToolError("varieties must be a list of objects, each with at least a variety_name.")
	allowed = {"variety_name", "rootstock", "pollination_group", "expected_yield_per_acre", "maturity_years"}
	out = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"varieties[{index}] is not an object. Nothing was written.")
		unknown = set(entry) - allowed
		if unknown:
			raise ToolError(
				f"varieties[{index}] has unknown key(s): {', '.join(sorted(unknown))}. Allowed: "
				f"{', '.join(sorted(allowed))}. A key silently dropped is a fact somebody thinks "
				f"they recorded. Nothing was written."
			)
		variety_name = str(entry.get("variety_name") or "").strip()
		if not variety_name:
			raise ToolError(f"varieties[{index}] has no variety_name. Nothing was written.")
		out.append(
			{
				"variety_name": variety_name,
				"rootstock": str(entry.get("rootstock") or "").strip(),
				"pollination_group": str(entry.get("pollination_group") or "").strip(),
				"expected_yield_per_acre": float(entry.get("expected_yield_per_acre") or 0),
				"maturity_years": int(entry.get("maturity_years") or 0),
			}
		)
	return out


def _water_rows(raw) -> list[dict]:
	"""Validate and normalise the `water_requirements` argument."""
	if raw in (None, ""):
		return []
	if not isinstance(raw, list):
		raise ToolError("water_requirements must be a list of objects, each with at least a growth_stage.")
	allowed = {"growth_stage", "crop_coefficient_kc", "water_inches_per_week", "notes"}
	out = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"water_requirements[{index}] is not an object. Nothing was written.")
		unknown = set(entry) - allowed
		if unknown:
			raise ToolError(
				f"water_requirements[{index}] has unknown key(s): {', '.join(sorted(unknown))}. "
				f"Allowed: {', '.join(sorted(allowed))}. Nothing was written."
			)
		stage = str(entry.get("growth_stage") or "").strip()
		if not stage:
			raise ToolError(f"water_requirements[{index}] has no growth_stage. Nothing was written.")
		out.append(
			{
				"growth_stage": as_choice(CROP_WATER, "growth_stage", stage, "growth_stage"),
				"crop_coefficient_kc": float(entry.get("crop_coefficient_kc") or 0),
				"water_inches_per_week": float(entry.get("water_inches_per_week") or 0),
				"notes": str(entry.get("notes") or "").strip(),
			}
		)
	return out


# ── 4. update_crop ──────────────────────────────────────────────────────────
def update_crop(args: dict) -> ToolResult:
	"""Change a registered crop. Cannot re-key it."""
	_require(CROP)
	row = _one_of(CROP, as_str(args, "crop", required=True), "crop", ("name",))

	if as_str(args, "crop_name"):
		raise ToolError(
			"crop_name cannot be changed: it is the docname, and every record that names a crop "
			"spells it. Renaming a crop is a new crop plus a migration of what pointed at the "
			"old one, not an edit. Nothing was changed."
		)

	doc = frappe.get_doc(CROP, row["name"])
	changes = {}

	for key in ("scientific_name", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	for key in ("crop_type", "growth_cycle", "harvest_window_start", "harvest_window_end"):
		if key in args:
			value = as_str(args, key)
			_stage(changes, doc, key, as_choice(CROP, key, value, key) if value else "")
	for key in ("days_to_harvest", "default_phi_days"):
		if key in args:
			_stage(changes, doc, key, as_int(args, key) or 0)
	if "is_organic_certified" in args:
		_stage(changes, doc, "is_organic_certified", 1 if as_bool(args, "is_organic_certified") else 0)
	if "pct_direct_marketed" in args:
		direct = _share(args, "pct_direct_marketed")
		_stage(changes, doc, "pct_direct_marketed", "" if direct is None else direct)

	#: Child tables are REPLACED WHOLESALE when passed, never merged. A merge
	#: needs a stable row key, these rows have none a caller can see, and a
	#: half-merged variety list is worse than either operation done plainly.
	#: Omitting the argument leaves the table untouched; passing an empty list
	#: is how a caller genuinely clears it.
	if "varieties" in args:
		wanted = _variety_rows(args.get("varieties"))
		changes["varieties"] = [f"{len(doc.varieties or [])} row(s)", f"{len(wanted)} row(s)"]
		doc.set("varieties", [])
		for entry in wanted:
			doc.append("varieties", entry)
	if "water_requirements" in args:
		wanted = _water_rows(args.get("water_requirements"))
		changes["water_requirements"] = [
			f"{len(doc.water_requirements or [])} row(s)",
			f"{len(wanted)} row(s)",
		]
		doc.set("water_requirements", [])
		for entry in wanted:
			doc.append("water_requirements", entry)

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: scientific_name, crop_type, growth_cycle, "
			"days_to_harvest, harvest_window_start, harvest_window_end, default_phi_days, "
			"is_organic_certified, pct_direct_marketed, varieties, water_requirements, notes."
		)

	doc.save(ignore_permissions=True)
	return ToolResult(
		data={
			**_describe_crop(dict(doc.as_dict())),
			"varieties": _table(doc, "varieties"),
			"water_requirements": _table(doc, "water_requirements"),
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
			"phi_caveat": PHI_CAVEAT,
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ── 4b. get_variety_care_recipe ─────────────────────────────────────────────
def get_variety_care_recipe(args: dict) -> ToolResult:
	"""One variety's resolved water schedule and its cultural practice protocol.

	THE RESOLUTION IS THE POINT. Anything can read the two overlay tables off
	`get_crop`; what nobody can do by hand without getting it wrong is combine
	them with the crop's own schedule per field, in season order, and say which
	record each surviving number came from. A caller that resolves this itself
	will fall back per ROW — which silently discards the crop's weekly depth
	every time a variety overrides only the Kc.

	IT REPORTS THE ROOTSTOCK AND REFUSES TO PRETEND IT IS THE BLOCK'S. The
	catalogue holds one per variety; a care recipe is read against a block, and
	the block's rootstock is on its planting. Both are returned, and the payload
	says which is which.
	"""
	_require(CROP)
	crop_row = _one_of(
		CROP, as_str(args, "crop", required=True), "crop", ("name", "crop_name", "growth_cycle")
	)
	crop = crop_row["name"]
	wanted = as_str(args, "variety", required=True)

	varieties = (
		_child_rows(
			CROP_VARIETY,
			crop,
			"varieties",
			("variety_name", "rootstock", "pollination_group", "expected_yield_per_acre", "maturity_years"),
		)
		if compat.doctype_exists(CROP_VARIETY)
		else []
	)
	variety = _match_variety(varieties, wanted, crop)

	crop_stages = (
		_child_rows(
			CROP_WATER,
			crop,
			"water_requirements",
			("growth_stage", "crop_coefficient_kc", "water_inches_per_week", "notes"),
		)
		if compat.doctype_exists(CROP_WATER)
		else []
	)
	overrides = _variety_water_overrides(crop)
	schedule = resolve_variety_water(crop_stages, overrides, variety["variety_name"])
	steps = _for_variety(_variety_protocols(crop), variety["variety_name"])

	# Grouped as well as listed, because "what is the PGR program for Bing" is
	# the question this tool exists to answer and a flat list makes the caller
	# do the grouping — which is where a practice gets missed.
	by_practice: dict = {}
	for step in steps:
		practice = str(step.get("practice") or "Other").strip() or "Other"
		by_practice.setdefault(practice, []).append(step)
	for rows in by_practice.values():
		rows.sort(key=lambda row: _stage_key(str(row.get("timing_stage") or "").strip()))

	overridden = [row["growth_stage"] for row in schedule if row["is_overridden"]]
	notes = []
	if not schedule:
		notes.append(
			f"No water demand is modelled for {crop} at all, so this variety has no schedule to "
			"resolve — not a schedule of zero. Record the crop's stages first; a variety "
			"override on its own has nothing to override."
		)
	elif not overridden:
		notes.append(
			f"{variety['variety_name']} departs from {crop} at no stage, so every figure here is "
			"the crop's. That is the ordinary case and not a gap — an overlay row exists only "
			"where a variety genuinely differs."
		)
	if not steps:
		notes.append(
			f"No cultural practice protocol is recorded for {variety['variety_name']}. Nothing "
			"can say what this variety is due, and a spray plan reading this has no GA timing, "
			"no PGR program and no thinning approach to work from."
		)
	if variety.get("rootstock"):
		notes.append(
			f"The catalogue's default rootstock for {variety['variety_name']} is "
			f"{variety['rootstock']}. That is a species-level default — read the block's own "
			"planting for what is actually in the ground before applying anything sized per acre."
		)

	return ToolResult(
		data={
			"crop": crop,
			"variety": variety["variety_name"],
			"pollination_group": variety.get("pollination_group") or None,
			"expected_yield_per_acre": _number_or_none(variety.get("expected_yield_per_acre")),
			"maturity_years": int(variety["maturity_years"]) if variety.get("maturity_years") else None,
			"catalogue_rootstock": variety.get("rootstock") or None,
			"water_schedule": schedule,
			"stages_overridden": overridden,
			"stages_from_crop_default": [row["growth_stage"] for row in schedule if not row["is_overridden"]],
			"protocol_steps": steps,
			"protocol_by_practice": by_practice,
			"practices_recorded": sorted(by_practice),
			"agronomy_notes": notes,
			"rootstock_caveat": ROOTSTOCK_CAVEAT,
			"protocol_caveat": PROTOCOL_CAVEAT,
			"phi_caveat": PHI_CAVEAT,
		},
		summary=(
			f"{variety['variety_name']} on {crop}: {len(overridden)} stage(s) overridden of "
			f"{len(schedule)}, {len(steps)} protocol step(s)"
		),
	)


def _match_variety(varieties: list[dict], wanted: str, crop: str) -> dict:
	"""The catalogue row for `wanted`, matched ignoring case, or a refusal naming what exists.

	Names the alternatives rather than saying "not found". A variety is free text
	on both sides of this lookup, so the overwhelmingly common failure is a
	spelling or a variety recorded under a different crop — and both are answered
	instantly by seeing the list.
	"""
	target = str(wanted or "").strip().casefold()
	for row in varieties:
		if str(row.get("variety_name") or "").strip().casefold() == target:
			return row
	if not varieties:
		raise ToolError(
			f"{crop} has no varieties recorded, so there is no care recipe for {wanted!r} or for "
			f"anything else. Add the variety to the crop's Varieties table first."
		)
	known = ", ".join(sorted(str(row.get("variety_name") or "") for row in varieties))
	raise ToolError(
		f"{crop} has no variety called {wanted!r}. The varieties recorded are: {known}. A care "
		f"recipe is read against the catalogue, so a variety it does not list has nothing to "
		f"resolve — check the spelling, or whether the variety is recorded under another crop."
	)


# ── 5. list_markets ─────────────────────────────────────────────────────────
def list_markets(args: dict) -> ToolResult:
	"""The market register, with grades counted and the planning gaps named."""
	_require(MARKET)
	limit = as_limit(args)

	filters = {}
	market_type = as_str(args, "market_type")
	if market_type:
		filters["market_type"] = as_choice(MARKET, "market_type", market_type, "market_type")
	if "is_active" in args:
		filters["is_active"] = 1 if as_bool(args, "is_active") else 0
	for key in ("region", "country", "primary_commodity"):
		value = as_str(args, key)
		if value:
			filters[key] = value

	rows = frappe.db.get_all(
		MARKET,
		filters=filters,
		fields=compat.existing_fields(MARKET, _MARKET_FIELDS),
		order_by="market_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	markets = [_describe_market(dict(row)) for row in rows]

	grade_counts: dict = {}
	if markets and compat.doctype_exists(GRADE_STANDARD):
		for row in frappe.db.get_all(
			GRADE_STANDARD,
			filters={"parenttype": MARKET, "parent": ("in", [market["name"] for market in markets])},
			fields=["parent", "grade_name"],
			limit=REGISTER_CAP,
		):
			grade_counts[row["parent"]] = grade_counts.get(row["parent"], 0) + 1
	for market in markets:
		market["grade_count"] = grade_counts.get(market["name"], 0)

	by_type: dict = {}
	for market in markets:
		key = market["market_type"] or "(unrecorded)"
		by_type[key] = by_type.get(key, 0) + 1

	no_grades = [market["name"] for market in markets if market["is_active"] and not market["grade_count"]]
	no_shipping_point = [market["name"] for market in markets if not market["shipping_point"]]

	return ToolResult(
		data={
			"market_count": len(markets),
			"active_count": sum(1 for market in markets if market["is_active"]),
			"by_market_type": dict(sorted(by_type.items())),
			"grade_standard_count": sum(grade_counts.values()),
			"active_without_grade_standards": no_grades,
			"without_usda_shipping_point": no_shipping_point,
			"markets": markets,
			"gap_note": (
				"active_without_grade_standards is the list that matters for planning: a market with "
				"no grades has no packout assumption behind it, so any breakeven quoting it is "
				"quoting a number somebody typed. without_usda_shipping_point is the list that "
				"cannot be joined to USDA Market News price series."
			),
			"limit": limit,
			"truncated": len(markets) >= min(limit, REGISTER_CAP),
		},
		summary=(
			f"{len(markets)} market(s), {sum(grade_counts.values())} grade standard(s), "
			f"{len(no_grades)} active with no grades"
		),
	)


# ── 6. get_market ───────────────────────────────────────────────────────────
def get_market(args: dict) -> ToolResult:
	"""One market in full, with its grade ladder and what it implies for packout."""
	_require(MARKET)
	row = _one_of(MARKET, as_str(args, "market", required=True), "market", _MARKET_FIELDS)
	described = _describe_market(row)

	grades = (
		_child_rows(
			GRADE_STANDARD,
			row["name"],
			"grade_standards",
			("grade_name", "min_size_mm", "max_defect_pct", "pack_style", "premium_pct"),
		)
		if compat.doctype_exists(GRADE_STANDARD)
		else []
	)
	#: Sorted by what the grade is worth rather than by row order, because the
	#: ladder is the object a planner reads and row order is not a decision
	#: anybody made.
	ladder = sorted(grades, key=lambda grade: float(grade.get("premium_pct") or 0), reverse=True)

	crop = {}
	if row.get("primary_commodity") and compat.doctype_exists(CROP):
		crop = dict(
			frappe.db.get_value(
				CROP,
				row["primary_commodity"],
				["name", "crop_type", "default_phi_days", "harvest_window_start", "harvest_window_end"],
				as_dict=True,
			)
			or {}
		)

	notes = []
	if described["is_active"] and not grades:
		notes.append(
			"This market is active and has no grade standards. Any packout assumption quoting it is "
			"a number somebody typed rather than a standard the market enforces."
		)
	if not described["shipping_point"]:
		notes.append(
			"No USDA shipping point recorded, so this market cannot be joined to a USDA Market News "
			"price series — the join is on that exact string and on nothing else."
		)
	sized = [grade for grade in grades if float(grade.get("min_size_mm") or 0) > 0]
	if grades and not sized:
		notes.append(
			"No grade records a minimum size, so the grade ladder cannot be applied to a measured "
			"size distribution — which is the calculation a packout projection is."
		)

	return ToolResult(
		data={
			**described,
			"grade_standards": ladder,
			"grade_count": len(grades),
			"top_grade": ladder[0]["grade_name"] if ladder else None,
			"premium_spread_pct": (
				round(float(ladder[0].get("premium_pct") or 0) - float(ladder[-1].get("premium_pct") or 0), 2)
				if len(ladder) > 1
				else None
			),
			"primary_commodity_detail": crop or None,
			"planning_notes": notes,
			"packout_note": (
				"premium_spread_pct is the distance between the best and worst grade this market pays, "
				"and it is what makes a packout percentage worth forecasting: where the spread is "
				"narrow, an error in the projected split costs little, and where it is wide it is the "
				"largest single assumption in a breakeven."
			),
		},
		summary=(
			f"{described['market_name']}: {described['market_type']}, {len(grades)} grade(s)"
			+ ("" if described["is_active"] else ", inactive")
		),
	)


# ── 7. create_market ────────────────────────────────────────────────────────
def create_market(args: dict) -> ToolResult:
	"""Register one market with its grade standards."""
	_require(MARKET)
	market_name = as_str(args, "market_name", required=True)

	if frappe.db.exists(MARKET, market_name):
		raise ToolError(
			f"{market_name!r} is already registered. A market is a place in the world, not a "
			f"thing a company owns — there is one of it, and two records for it would give the "
			f"site two answers to what its grades are. Nothing was created."
		)

	doc = frappe.new_doc(MARKET)
	doc.market_name = market_name
	doc.market_type = as_choice(
		MARKET, "market_type", as_str(args, "market_type", required=True), "market_type"
	)
	for key in ("region", "shipping_point", "notes"):
		doc.set(key, as_str(args, key))
	for key, doctype in (("country", "Country"), ("currency", "Currency"), ("primary_commodity", CROP)):
		value = as_str(args, key)
		if not value:
			continue
		doc.set(key, _one_of(doctype, value, key)["name"] if compat.doctype_exists(doctype) else value)
	active = as_bool(args, "is_active")
	doc.is_active = 1 if active is None or active else 0

	for row in _grade_rows(args.get("grade_standards")):
		doc.append("grade_standards", row)

	doc.insert(ignore_permissions=True)
	described = _describe_market(dict(doc.as_dict()))

	warnings = []
	if not doc.grade_standards:
		warnings.append(
			"No grade standards recorded. This market has no packout assumption behind it until it has."
		)
	if not described["shipping_point"]:
		warnings.append("No USDA shipping point, so no outside price series can be attached to this market.")

	return ToolResult(
		data={
			**described,
			"grade_standards": _table(doc, "grade_standards"),
			"warnings": warnings,
		},
		summary=f"registered market {doc.name} ({described['market_type']}, {len(doc.grade_standards or [])} grade(s))",
		docstatus_delta="none → 0 (created)",
	)


def _grade_rows(raw) -> list[dict]:
	"""Validate and normalise the `grade_standards` argument, whole list first."""
	if raw in (None, ""):
		return []
	if not isinstance(raw, list):
		raise ToolError("grade_standards must be a list of objects, each with at least a grade_name.")
	allowed = {"grade_name", "min_size_mm", "max_defect_pct", "pack_style", "premium_pct"}
	out = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"grade_standards[{index}] is not an object. Nothing was written.")
		unknown = set(entry) - allowed
		if unknown:
			raise ToolError(
				f"grade_standards[{index}] has unknown key(s): {', '.join(sorted(unknown))}. "
				f"Allowed: {', '.join(sorted(allowed))}. Nothing was written."
			)
		grade_name = str(entry.get("grade_name") or "").strip()
		if not grade_name:
			raise ToolError(f"grade_standards[{index}] has no grade_name. Nothing was written.")
		out.append(
			{
				"grade_name": grade_name,
				"min_size_mm": float(entry.get("min_size_mm") or 0),
				"max_defect_pct": float(entry.get("max_defect_pct") or 0),
				"pack_style": str(entry.get("pack_style") or "").strip(),
				"premium_pct": float(entry.get("premium_pct") or 0),
			}
		)
	return out


# ── 8. update_market ────────────────────────────────────────────────────────
def update_market(args: dict) -> ToolResult:
	"""Change a registered market. Cannot re-key it."""
	_require(MARKET)
	row = _one_of(MARKET, as_str(args, "market", required=True), "market", ("name",))

	if as_str(args, "market_name"):
		raise ToolError(
			"market_name cannot be changed: it is the docname, and last season's settlements "
			"spell it. Nothing was changed."
		)

	doc = frappe.get_doc(MARKET, row["name"])
	changes = {}

	for key in ("region", "shipping_point", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	if "market_type" in args:
		value = as_str(args, "market_type")
		_stage(
			changes,
			doc,
			"market_type",
			as_choice(MARKET, "market_type", value, "market_type") if value else "",
		)
	for key, doctype in (("country", "Country"), ("currency", "Currency"), ("primary_commodity", CROP)):
		if key not in args:
			continue
		value = as_str(args, key)
		resolved = ""
		if value:
			resolved = _one_of(doctype, value, key)["name"] if compat.doctype_exists(doctype) else value
		_stage(changes, doc, key, resolved)
	if "is_active" in args:
		_stage(changes, doc, "is_active", 1 if as_bool(args, "is_active") else 0)

	if "grade_standards" in args:
		wanted = _grade_rows(args.get("grade_standards"))
		changes["grade_standards"] = [f"{len(doc.grade_standards or [])} row(s)", f"{len(wanted)} row(s)"]
		doc.set("grade_standards", [])
		for entry in wanted:
			doc.append("grade_standards", entry)

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: market_type, region, country, currency, "
			"primary_commodity, shipping_point, is_active, grade_standards, notes."
		)

	doc.save(ignore_permissions=True)
	return ToolResult(
		data={
			**_describe_market(dict(doc.as_dict())),
			"grade_standards": _table(doc, "grade_standards"),
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ── 9. list_ag_uom_contexts ─────────────────────────────────────────────────
def list_ag_uom_contexts(args: dict) -> ToolResult:
	"""Which units are valid for which work, with the default for each."""
	_require(UOM_CONTEXT)

	filters = {}
	if "is_active" in args:
		filters["is_active"] = 1 if as_bool(args, "is_active") else 0
	applies_to = as_str(args, "applies_to")
	if applies_to:
		filters["applies_to"] = as_choice(UOM_CONTEXT, "applies_to", applies_to, "applies_to")

	rows = frappe.db.get_all(
		UOM_CONTEXT,
		filters=filters,
		fields=["name", "context_name", "applies_to", "is_active", "description"],
		order_by="context_name asc",
		limit=REGISTER_CAP,
	)

	entries_by_context: dict = {}
	if rows:
		for entry in frappe.db.get_all(
			"Agricultural UOM Context Entry",
			filters={"parenttype": UOM_CONTEXT, "parent": ("in", [row["name"] for row in rows])},
			fields=["parent", "uom", "is_default", "notes"],
			order_by="idx asc",
			limit=REGISTER_CAP,
		):
			entries_by_context.setdefault(entry["parent"], []).append(
				{
					"uom": entry["uom"],
					"is_default": compat.checked(entry.get("is_default")),
					"measures": ag_uom.dimension_of(entry["uom"]),
					"notes": entry.get("notes") or None,
				}
			)

	contexts = []
	for row in rows:
		units = entries_by_context.get(row["name"], [])
		default = next((unit["uom"] for unit in units if unit["is_default"]), None)
		contexts.append(
			{
				"name": row["name"],
				"context_name": row["context_name"],
				"applies_to": row["applies_to"],
				"is_active": compat.checked(row.get("is_active")),
				"description": row.get("description") or None,
				"default_uom": default,
				"valid_uoms": [unit["uom"] for unit in units],
				"units": units,
			}
		)

	return ToolResult(
		data={
			"context_count": len(contexts),
			"contexts": contexts,
			"all_valid_uoms": sorted({unit for context in contexts for unit in context["valid_uoms"]}),
			"design_note": (
				"Harvest and Scale Ticket are two contexts rather than one list because a bin is a "
				"CONTAINER and a pound is a WEIGHT. A field crew hands in bins and the shed reports "
				"pounds; those are two measurements of one delivery. A single list accepting either "
				"is a list that lets them be summed. Cross them with get_uom_conversions."
			),
		},
		summary=f"{len(contexts)} unit context(s)",
	)


# ── 10. get_uom_conversions ─────────────────────────────────────────────────
def _all_conversions() -> list[dict]:
	"""Every conversion row, ACTIVE OR NOT, in one query.

	Deliberately unfiltered, because the two things this module does with these
	rows need different sets and the difference is a refusal a caller can act on.
	Resolution uses only the live rows. The report of what exists for a unit pair
	uses all of them — so "there are three rows for Bin to Pound and every one is
	switched off" comes back as its own sentence instead of being indistinguishable
	from "nobody ever recorded one".
	"""
	return [
		dict(row)
		for row in frappe.db.get_all(
			UOM_CONVERSION,
			fields=list(_CONVERSION_FIELDS),
			limit=CONVERSION_CAP,
		)
	]


def _edges(rows: list[dict], crop: str) -> dict:
	"""Every usable one-step conversion, keyed (from, to), best row winning.

	Each stored row yields TWO edges — the factor as recorded and its reciprocal
	— because "one bin is 800 pounds" and "one pound is a 800th of a bin" are the
	same fact, and requiring both to be entered would be requiring an operator to
	keep two rows in step by hand.

	A crop-specific row beats a generic one for the same pair. That is the whole
	point of the crop column, and it is applied here rather than at the call site
	so a chained answer prefers the specific row at every hop rather than only at
	the first.
	"""
	best: dict = {}
	for row in rows:
		row_crop = str(row.get("crop") or "")
		if row_crop and crop and row_crop != crop:
			continue
		if row_crop and not crop:
			continue
		factor = float(row.get("factor") or 0)
		if factor <= 0:
			continue
		specific = 1 if row_crop else 0
		for start, end, value, inverted in (
			(row["from_uom"], row["to_uom"], factor, False),
			(row["to_uom"], row["from_uom"], 1.0 / factor, True),
		):
			key = (start, end)
			existing = best.get(key)
			if existing and existing["specific"] >= specific:
				continue
			best[key] = {
				"factor": value,
				"specific": specific,
				"inverted": inverted,
				"row": row["name"],
				"basis": row.get("basis"),
				"crop": row_crop or None,
				"source": row.get("source") or None,
			}
	return best


def get_uom_conversions(args: dict) -> ToolResult:
	"""How many `to_uom` are in one `from_uom`, and where that number came from."""
	_require(UOM_CONVERSION)
	from_uom = as_str(args, "from_uom", required=True)
	to_uom = as_str(args, "to_uom", required=True)
	crop = as_str(args, "crop")
	if crop and compat.doctype_exists(CROP):
		crop = _one_of(CROP, crop, "crop")["name"]

	if from_uom == to_uom:
		raise ToolError(
			f"{from_uom} to {from_uom} is not a conversion. The factor is 1 and asking for it is "
			f"almost always a sign that one of the two units was meant to be something else."
		)

	rows = _all_conversions()
	edges = _edges([row for row in rows if compat.checked(row.get("is_active"))], crop)

	#: Every row recorded for this pair in either direction, whatever crop it
	#: names and whether or not it is live. Reported alongside the answer because
	#: the commonest way a conversion is wrong is not a bad factor — it is the
	#: right factor for the wrong fruit, and a caller who can see the
	#: alternatives can spot that. The superseded ones are here too, flagged,
	#: because "this was 750 until somebody changed it" is exactly the context a
	#: disputed settlement needs.
	pair_rows = [
		{
			"name": row["name"],
			"from_uom": row["from_uom"],
			"to_uom": row["to_uom"],
			"crop": row.get("crop") or None,
			"factor": float(row.get("factor") or 0),
			"basis": row.get("basis"),
			"source": row.get("source") or None,
			"is_active": compat.checked(row.get("is_active")),
		}
		for row in rows
		if {row["from_uom"], row["to_uom"]} == {from_uom, to_uom}
	]

	direct = edges.get((from_uom, to_uom))
	if direct:
		return ToolResult(
			data=_conversion_answer(from_uom, to_uom, crop, direct, pair_rows, path=[from_uom, to_uom]),
			summary=(
				f"1 {from_uom} = {round(direct['factor'], 6)} {to_uom}"
				+ (f" ({crop})" if direct["specific"] else "")
			),
		)

	chained = _chain(edges, from_uom, to_uom)
	if chained:
		return ToolResult(
			data=_conversion_answer(from_uom, to_uom, crop, chained, pair_rows, path=chained["path"]),
			summary=(
				f"1 {from_uom} = {round(chained['factor'], 6)} {to_uom} via {' → '.join(chained['path'])}"
			),
		)

	#: Nothing resolved. The refusal has to distinguish three different
	#: situations, because they need three different actions from the caller.
	crop_specific_elsewhere = sorted(
		{row["crop"] for row in pair_rows if row["crop"] and row["crop"] != crop and row["is_active"]}
	)
	if crop and crop_specific_elsewhere:
		raise ToolError(
			f"no conversion from {from_uom} to {to_uom} for {crop}. The site has one for "
			f"{', '.join(crop_specific_elsewhere)} and no generic row to fall back on — which is "
			f"correct rather than missing: a {from_uom} of one fruit does not weigh what a "
			f"{from_uom} of another does, and guessing is how a settlement goes wrong by a "
			f"factor nobody traces. Record the factor for {crop}."
		)
	if pair_rows:
		raise ToolError(
			f"no ACTIVE conversion from {from_uom} to {to_uom}"
			+ (f" for {crop}" if crop else "")
			+ f". {len(pair_rows)} row(s) exist for this pair but none is usable here — a "
			f"superseded row is kept switched off so old settlements stay explicable, and it is "
			f"not consulted."
		)
	raise ToolError(
		f"no conversion from {from_uom} to {to_uom}"
		+ (f" for {crop}" if crop else "")
		+ ", directly or through any single intermediate unit. Record one, or check the unit "
		"names against list_ag_uom_contexts."
	)


def _chain(edges: dict, from_uom: str, to_uom: str) -> dict | None:
	"""One hop through an intermediate unit, or None.

	ONE HOP AND NOT A FULL SEARCH, deliberately. Bins → pounds → tons is a real
	chain somebody needs; anything longer is multiplying three or more nominal
	figures together, and the compounding error in that answer is larger than
	the answer is useful. A caller who genuinely needs a two-hop conversion
	should record the factor they actually mean.
	"""
	pivots = {end for (start, end) in edges if start == from_uom}
	best = None
	for pivot in sorted(pivots):
		first = edges.get((from_uom, pivot))
		second = edges.get((pivot, to_uom))
		if not first or not second:
			continue
		candidate = {
			"factor": first["factor"] * second["factor"],
			"specific": min(first["specific"], second["specific"]),
			"inverted": first["inverted"] or second["inverted"],
			"row": f"{first['row']} + {second['row']}",
			"basis": _weaker_basis(first.get("basis"), second.get("basis")),
			"crop": first.get("crop") or second.get("crop"),
			"source": None,
			"path": [from_uom, pivot, to_uom],
		}
		if best is None or candidate["specific"] > best["specific"]:
			best = candidate
	return best


def _weaker_basis(first: str, second: str) -> str:
	"""The less certain of two bases — a chain is only as exact as its worst hop.

	An exact hop composed with a nominal one is nominal, and reporting the chain
	as Exact because half of it was would be the single most misleading thing
	this module could say about a number.
	"""
	order = {"Exact": 3, "Operation Average": 2, "Nominal": 1}
	return first if order.get(first, 0) <= order.get(second, 0) else second


def _conversion_answer(
	from_uom: str, to_uom: str, crop: str, resolved: dict, pair_rows: list[dict], path: list[str]
) -> dict:
	fell_back = bool(crop) and not resolved["specific"]
	notes = []
	if fell_back:
		notes.append(
			f"This is the GENERIC factor, not one recorded for {crop}. It is the site's site-wide "
			f"figure and it may not be true of this fruit — record a crop-specific row if it is not."
		)
	if len(path) > 2:
		notes.append(
			f"Chained through {path[1]}: two factors multiplied. The result is only as certain as "
			f"the weaker of the two, which is why basis reads {resolved['basis']}."
		)
	if resolved.get("inverted"):
		notes.append(
			"At least one hop was inverted — the stored row records the opposite direction, and "
			"its reciprocal was used. That is the same fact, not a second one."
		)
	if resolved.get("basis") == "Nominal":
		notes.append(
			"A Nominal factor is the trade's rule of thumb. It is right enough to plan with and "
			"not right enough to settle a dispute with; a farm that weighs its own containers "
			"should record an Operation Average, which wins this lookup."
		)

	return {
		"from_uom": from_uom,
		"to_uom": to_uom,
		"crop": crop or None,
		"factor": round(resolved["factor"], 6),
		"basis": resolved.get("basis"),
		"crop_specific": bool(resolved["specific"]),
		"path": path,
		"source_rows": resolved["row"],
		"source": resolved.get("source"),
		"reading": f"1 {from_uom} = {round(resolved['factor'], 6)} {to_uom}",
		"inverse_factor": round(1.0 / resolved["factor"], 6) if resolved["factor"] else None,
		"rows_for_this_pair": pair_rows,
		"notes": notes,
	}
