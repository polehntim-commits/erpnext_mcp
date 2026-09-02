# SPDX-License-Identifier: MIT
"""Fields and irrigation zones: the structure under a parcel.

WHY THE STRUCTURE LIVES HERE AND THE OPERATIONS DO NOT. A spray, a pick, a
water set and a soil test all happen to a *block*, and every one of them is
recorded by a different system. What none of those systems can be is the place
the block itself is defined, because a block outlives the app that last recorded
something against it — and because a cost centre, a lease and an appraisal all
need to point at the same ground. So ERPNext holds Parcel → Field → Irrigation
Zone, and the operational apps carry an id into it. That split is the whole
design: this app owns structure, the field apps own events.

WHY THE FOOD SAFETY FIELDS ARE ON THE FIELD AND NOT IN A COMPLIANCE MODULE. Try
removing `last_spray_date`. The WPS record-keeping report breaks — and so does
the crew's morning, because nobody can answer whether the re-entry interval on
block 3 has run. Try removing `worker_hygiene_station_present`: an inspector
loses a checkbox, and dispatch loses the fact that deciding whether a crew may
be sent to that block at all. Both break operations AND reporting, which is what
makes them woven rather than shadow. A separate "Field Compliance Log" that
somebody fills in after the fact would break neither, and that is exactly why
this release does not have one.

WHAT THE ACREAGE RULES ARE FOR. Blocks summing to more than their parcel, and
zones summing to more than their block, are the two failures a bad import
produces every time, and they are contradictions rather than judgements — two
numbers that cannot both be true. Everything softer is left alone: blocks
summing to *less* than the parcel is the normal case, because roads, ditches,
headlands and the house are all real.

`import_farm_app_fields` IS A FOUNDATION, NOT A SYNC. It takes a list of legacy
records and creates Fields carrying their ids, so that when the sync engine
arrives it has something to match on. It does not update, it does not delete,
and it does not run in reverse. Dry run is the default, and it refuses the whole
batch on the first bad record rather than half-importing a farm.
"""

import json

import frappe

from .. import compat, geo
from ..abbr import parcel_abbr
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
from .realestate import parcel_row

FIELD = "Field"
FIELD_VARIETY = "Field Variety"
IRRIGATION_ZONE = "Irrigation Zone"
PARCEL = "Parcel"

#: The columns a `varieties` argument may set on one child row, and the ones
#: `_field_varieties` reads back. Kept in one place so `create_field`,
#: `update_field` and `_field_varieties` cannot drift on what the table holds.
#: DELIBERATELY JUST THE LINK: `variety` names one of the block's crop's own
#: `Crop Variety` catalogue rows (checked on save by `Field._check_varieties`),
#: and `percentage`/`planting_year` are the two facts that are genuinely about
#: THIS planting rather than the cultivar in general. Rootstock, pollination
#: group, yield and Brix stay on `Crop Variety` and are read from there, not
#: copied here — a second copy is a second answer, and the one that goes stale
#: is always the one nobody edited.
_FIELD_VARIETY_FIELDS = (
	"variety",
	"percentage",
	"planting_year",
)

#: The Frappe app that records what happens on a block, and the doctype it
#: records sprays in. Both are optional: this app never imports from it, only
#: asks whether the site has it.
PRECISION_AG_APP = "farm_precision_ag"
SPRAY_LOG = "Spray Log"

#: The one `organic_status` value that means the certificate is in hand, spelled
#: once. The Field controller derives `organic_certified` from the same string;
#: two spellings of it would let a block report itself conventional and look fine.
CERTIFIED_ORGANIC = "Certified Organic"
TRANSITIONAL = "Transitional"

_FIELD_FIELDS = (
	"name",
	"field_name",
	"parcel",
	"owning_entity",
	"acreage",
	"crop",
	"variety",
	"rootstock",
	"planting_year",
	"planting_density_per_acre",
	"condition",
	"soil_profile",
	"productive_from_date",
	"productive_through_date",
	"pre_yield_end_date",
	"block_number",
	"block_ticker",
	"external_farm_app_id",
	"cost_center",
	"last_spray_date",
	"water_test_last_date",
	"wildlife_intrusion_last_report",
	"food_safety_zone",
	"worker_hygiene_station_present",
	"organic_status",
	"organic_certified",
	"organic_cert_agency",
	"transition_start_date",
	# v0.139.0. FSA's own identifiers for this block, written by
	# `import_fsa_clu_boundaries` off the county office's CLU file. Read here
	# because a register that cannot say which tract a block is on cannot answer
	# the question an acreage report is filed under.
	"fsa_farm_number",
	"fsa_tract_number",
	"fsa_clu_number",
	"fsa_clu_identifier",
	"fsa_calc_acres",
	"fsa_hel_type",
	"fsa_import_date",
	"boundary_geojson",
	"boundary_centroid_lat",
	"boundary_centroid_lon",
	"boundary_bbox_geojson",
	"h3_cells",
	"area_computed_acres",
	"satellite_provider",
	"imagery_asset_ref",
	"last_ndvi_pull_date",
	"last_ndvi_mean",
	"last_ndvi_stddev",
	"imagery_notes",
	"notes",
	"creation",
	"owner",
)

_ZONE_FIELDS = (
	"name",
	"zone_name",
	"field",
	"parcel",
	"owning_entity",
	"zone_number",
	"water_source",
	"water_right_id",
	"flow_rate_gpm",
	"sprinkler_type",
	"area_sq_ft",
	"area_acres",
	"water_test_last_date",
	"water_source_class",
	"chlorination_active",
	"boundary_geojson",
	"boundary_centroid_lat",
	"boundary_centroid_lon",
	"boundary_bbox_geojson",
	"h3_cells",
	"area_computed_acres",
	"notes",
	"creation",
	"owner",
)

#: Rows a register returns before the cap bites. The memory that scoped this
#: work put the whole operation at ~80 fields and ~1500 zones; 500 is the app's
#: standard ceiling and a parcel that needs paging is a parcel worth filtering.
REGISTER_CAP = 500

#: How many legacy records one import call will take. Past this the caller is
#: importing a database rather than a farm, and should be doing it in batches it
#: can check.
IMPORT_CAP = 500

#: The keys a legacy Farm App field record may carry. Anything else is a typo,
#: and a typo silently dropped is a field somebody thinks they imported.
IMPORT_KEYS = frozenset(
	{
		"name",
		"parcel_hint",
		"acreage",
		"variety",
		"planting_year",
		"block_number",
		"farm_app_uuid",
	}
)


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _entity(args: dict, required: bool = False) -> str | None:
	requested = as_str(args, "owning_entity") or as_str(args, "company")
	return resolve_company(requested, required=required)


def _date_str(value) -> str | None:
	return str(value or "") or None


def field_row(field: str, parcel: str = "", company: str = "") -> dict:
	"""One Field as a dict, from its docname or its bare field name.

	Two ways, for the same reason `parcel_row` takes two: whoever created it
	knows it as `"Yellow Camp Block 3"` and everything linking to it holds
	`"Yellow Camp Block 3 - MC"`.
	"""
	field = (field or "").strip()
	if not field:
		raise ToolError("field is required (a Field docname, or the field name such as 'Block 3')")
	fields = compat.existing_fields(FIELD, _FIELD_FIELDS)

	if frappe.db.exists(FIELD, field):
		row = dict(frappe.db.get_value(FIELD, field, fields, as_dict=True) or {})
		if company and row.get("owning_entity") and row["owning_entity"] != company:
			raise ToolError(f"Field {field!r} belongs to {row['owning_entity']!r}, not {company!r}")
		if parcel and row.get("parcel") != parcel:
			raise ToolError(f"Field {field!r} is on {row.get('parcel')!r}, not {parcel!r}")
		return row

	filters = {"field_name": field}
	if parcel:
		filters["parcel"] = parcel
	if company:
		filters["owning_entity"] = company
	matches = frappe.db.get_all(FIELD, filters=filters, fields=fields, limit=25)
	if len(matches) == 1:
		return dict(matches[0])
	if len(matches) > 1:
		names = ", ".join(sorted(str(match.get("name")) for match in matches))
		raise ToolError(
			f"{field!r} matches {len(matches)} fields: {names}. Pass the docname, or set parcel to narrow it."
		)
	scope = f" on {parcel}" if parcel else ""
	raise ToolError(f"no Field called {field!r}{scope}. list_fields has the register.")


def zone_row(zone: str, field: str = "", company: str = "") -> dict:
	"""One Irrigation Zone as a dict, from its docname or its bare zone name."""
	zone = (zone or "").strip()
	if not zone:
		raise ToolError("zone is required (an Irrigation Zone docname, or the zone name)")
	fields = compat.existing_fields(IRRIGATION_ZONE, _ZONE_FIELDS)

	if frappe.db.exists(IRRIGATION_ZONE, zone):
		row = dict(frappe.db.get_value(IRRIGATION_ZONE, zone, fields, as_dict=True) or {})
		if company and row.get("owning_entity") and row["owning_entity"] != company:
			raise ToolError(f"Irrigation Zone {zone!r} belongs to {row['owning_entity']!r}, not {company!r}")
		return row

	filters = {"zone_name": zone}
	if field:
		filters["field"] = field
	if company:
		filters["owning_entity"] = company
	matches = frappe.db.get_all(IRRIGATION_ZONE, filters=filters, fields=fields, limit=25)
	if len(matches) == 1:
		return dict(matches[0])
	if len(matches) > 1:
		names = ", ".join(sorted(str(match.get("name")) for match in matches))
		raise ToolError(
			f"{zone!r} matches {len(matches)} zones: {names}. Pass the docname, or set field to narrow it."
		)
	scope = f" on {field}" if field else ""
	raise ToolError(f"no Irrigation Zone called {zone!r}{scope}. list_irrigation_zones has the register.")


def _spray_log_available() -> bool:
	"""Is farm_precision_ag installed with a Spray Log this can read?

	Both halves are checked. An app can be installed with its doctypes half
	migrated, and a query against a table that is not there is a SQL error rather
	than an empty result.
	"""
	try:
		installed = PRECISION_AG_APP in (frappe.get_installed_apps() or [])
	except Exception:
		return False
	return installed and compat.doctype_exists(SPRAY_LOG)


def _observed_spray_dates(field_names) -> dict:
	"""The newest Spray Log date per field, where farm_precision_ag has one.

	Read lazily and never written back. A cached copy on the Field would be a
	second answer to a question that already has one, and the two would disagree
	the first time a spray was corrected. What IS stored on the Field is the date
	somebody recorded by hand, which is a different fact — it is what the farm
	knows on a site with no spray app at all.
	"""
	if not field_names or not _spray_log_available():
		return {}
	link = compat.first_field(SPRAY_LOG, "field", "erpnext_field", "block")
	date_field = compat.first_field(SPRAY_LOG, "application_date", "spray_date", "posting_date", "date")
	if not link or not date_field:
		return {}
	try:
		rows = frappe.db.get_all(
			SPRAY_LOG,
			filters={link: ("in", sorted(field_names))},
			fields=[link, date_field],
			limit=REGISTER_CAP * 20,
		)
	except Exception:
		return {}
	newest: dict = {}
	for row in rows or []:
		key = row.get(link)
		value = str(row.get(date_field) or "")[:10]
		if key and value:
			newest[key] = max(newest.get(key, ""), value)
	return newest


def _parcel_counties(parcel_names) -> dict:
	"""Parcel → county, for every parcel named, in one query.

	COUNTY IS READ THROUGH THE PARCEL AND IS NEVER COPIED ONTO A BLOCK. A second
	copy on the Field would be a second answer, and the one that is wrong is
	always the one nobody edited when the assessor redrew a line. So the register
	reports it, the survey groups by it, and there is exactly one place it is
	stored.

	Returns `{}` on a site whose Parcel has no county column rather than raising,
	so a block register still lists on a site that never recorded one.
	"""
	wanted = sorted({str(name) for name in (parcel_names or []) if name})
	if not wanted:
		return {}
	try:
		if not compat.doctype_exists(PARCEL) or not compat.has_field(PARCEL, "county"):
			return {}
		rows = frappe.db.get_all(
			PARCEL,
			filters={"name": ("in", wanted)},
			fields=["name", "county"],
			limit=max(len(wanted), 1),
		)
	except Exception:
		return {}
	return {row["name"]: (row.get("county") or None) for row in rows or []}


def _field_varieties(field_names) -> dict:
	"""Every named block's Field Variety rows, in one query.

	Batched on the same shape as `_parcel_counties` and `_observed_spray_dates`
	beside it — a per-row query here would be one extra round trip per block on
	every list call. A block with no rows of its own, or on a site that has not
	migrated the DocType in yet, is simply absent from the result rather than an
	error: `_describe_field` reads that as an empty list.
	"""
	wanted = sorted({str(name) for name in (field_names or []) if name})
	if not wanted or not compat.doctype_exists(FIELD_VARIETY):
		return {}
	rows = frappe.db.get_all(
		FIELD_VARIETY,
		filters={"parent": ("in", wanted), "parenttype": FIELD},
		fields=["parent", *_FIELD_VARIETY_FIELDS],
		order_by="parent asc, idx asc",
		limit=REGISTER_CAP * 20,
	)
	out: dict = {}
	for row in rows or []:
		out.setdefault(row["parent"], []).append(
			{
				"variety": row.get("variety") or None,
				"percentage": round(float(row.get("percentage") or 0), 2) or None,
				"planting_year": int(row.get("planting_year") or 0) or None,
			}
		)
	return out


def _describe_field(
	row: dict,
	observed: dict | None = None,
	counties: dict | None = None,
	varieties: dict | None = None,
) -> dict:
	observed = observed or {}
	if counties is None:
		counties = _parcel_counties([row.get("parcel")])
	if varieties is None:
		varieties = _field_varieties([row.get("name")])
	recorded = _date_str(row.get("last_spray_date"))
	seen = observed.get(row.get("name")) or None
	effective = max(filter(None, (recorded, seen)), default=None)
	if seen and (not recorded or seen > recorded):
		source = "farm_precision_ag Spray Log"
	elif recorded:
		source = "recorded on the Field"
	else:
		source = None
	return {
		"name": row.get("name"),
		"field_name": row.get("field_name"),
		"parcel": row.get("parcel"),
		"owning_entity": row.get("owning_entity") or None,
		"acreage": round(float(row.get("acreage") or 0), 2),
		"crop": row.get("crop") or None,
		"variety": row.get("variety") or None,
		"rootstock": row.get("rootstock") or None,
		"planting_year": int(row.get("planting_year") or 0) or None,
		"planting_density_per_acre": int(row.get("planting_density_per_acre") or 0) or None,
		# v0.142.0. `variety`/`rootstock`/`planting_year`/`planting_density_per_acre`
		# above stay the PRIMARY/LEGACY answer for a single-variety block. This is
		# the rest of it, for a block — the Pearl blocks among them — that carries
		# more than one cultivar and cannot be told apart by the single column.
		"varieties": varieties.get(row.get("name")) or [],
		"tree_count_estimate": _tree_count(row),
		"condition": row.get("condition") or None,
		# v0.116.0. Which Soil Compaction Profile this block's ground follows, and
		# therefore how long after a set the compaction overlay keeps machinery off
		# it. REPORTED AS THE COLUMN AND NOT AS THE RESOLVED HOURS: a register
		# listing says what is stored on the record, and the resolution — including
		# the fallback for a block that names none — belongs to `overlays.py`, which
		# is the one place that answers it the same way for every reader.
		"soil_profile": row.get("soil_profile") or None,
		"productive_from_date": _date_str(row.get("productive_from_date")),
		"productive_through_date": _date_str(row.get("productive_through_date")),
		"pre_yield_end_date": _date_str(row.get("pre_yield_end_date")),
		"block_number": row.get("block_number") or None,
		# v0.118.0. The buyer-facing code, and the only identifier on this record
		# somebody outside the farm ever types. Reported beside block_number rather
		# than instead of it: they answer different questions and a block commonly
		# has one and not the other.
		"block_ticker": row.get("block_ticker") or None,
		"external_farm_app_id": row.get("external_farm_app_id") or None,
		"cost_center": row.get("cost_center") or None,
		"last_spray_date": effective,
		"last_spray_date_recorded": recorded,
		"last_spray_date_observed": seen,
		"last_spray_source": source,
		"water_test_last_date": _date_str(row.get("water_test_last_date")),
		"wildlife_intrusion_last_report": _date_str(row.get("wildlife_intrusion_last_report")),
		"food_safety_zone": compat.checked(row.get("food_safety_zone")),
		"worker_hygiene_station_present": compat.checked(row.get("worker_hygiene_station_present")),
		"organic_status": row.get("organic_status") or None,
		"organic_certified": compat.checked(row.get("organic_certified")),
		"organic_cert_agency": row.get("organic_cert_agency") or None,
		"transition_start_date": _date_str(row.get("transition_start_date")),
		# Read through the parcel on every call and stored on none of them. See
		# `_parcel_counties`.
		"county": counties.get(row.get("parcel")) or None,
		**_boundary_summary(row),
		**_fsa_summary(row),
		"satellite_provider": row.get("satellite_provider") or None,
		"imagery_asset_ref": row.get("imagery_asset_ref") or None,
		"last_ndvi_pull_date": _date_str(row.get("last_ndvi_pull_date")),
		"last_ndvi_mean": _float_or_none(row.get("last_ndvi_mean")),
		"last_ndvi_stddev": _float_or_none(row.get("last_ndvi_stddev")),
		"imagery_notes": row.get("imagery_notes") or None,
		"notes": row.get("notes") or None,
	}


def _fsa_summary(row: dict) -> dict:
	"""FSA's identifiers for this block, and NOTHING AT ALL when it has none.

	v0.139.0. An `fsa` key present on every block on every farm would put seven
	nulls on every row of every register, for the majority of farms that have
	never imported a CLU file — so the key appears only where there is something
	in it. A caller reading `described.get("fsa")` gets the tract number or
	nothing, which is the same shape either way.

	FSA'S ACREAGE IS REPORTED BESIDE THE APP'S, NEVER INSTEAD OF IT.
	`fsa_calc_acres` is the figure a program payment is made on and
	`area_computed_acres` is what this app's arithmetic gets from the polygon;
	they routinely differ by a percent and a register that showed one of them
	would be hiding the disagreement rather than resolving it.
	"""
	found = {
		"farm_number": row.get("fsa_farm_number") or None,
		"tract_number": row.get("fsa_tract_number") or None,
		"clu_number": row.get("fsa_clu_number") or None,
		"clu_identifier": row.get("fsa_clu_identifier") or None,
		"calc_acres": _float_or_none(row.get("fsa_calc_acres")),
		"hel_type": row.get("fsa_hel_type") or None,
		"imported_on": _date_str(row.get("fsa_import_date")),
	}
	return {"fsa": found} if any(value is not None for value in found.values()) else {}


def _float_or_none(value):
	"""A stored number, or None when nothing was recorded.

	Not `float(value or 0)`: an NDVI of 0.0 is a real reading off bare ground and
	an unmeasured block is not the same thing. Collapsing them would make "we
	have not looked" indistinguishable from "we looked and it is dead".
	"""
	if value in (None, ""):
		return None
	return round(float(value), 4)


def _boundary_summary(row: dict) -> dict:
	"""What a read tool says about a boundary, without shipping the whole polygon.

	`boundary_geojson` IS returned, because a caller that wants to draw the shape
	has no other way to get it. Everything else is the derived index — and
	`h3_cells` is returned as counts rather than as the cells themselves, since a
	field can carry a hundred of them and a model reading a register of forty
	blocks does not need four thousand cell ids in its context. The cells are
	there for `find_fields_by_h3_cell`, which queries them server-side.
	"""
	cells = geo.stored_cells(row.get("h3_cells"))
	latitude = row.get("boundary_centroid_lat")
	longitude = row.get("boundary_centroid_lon")
	return {
		"has_boundary": bool(str(row.get("boundary_geojson") or "").strip()),
		"boundary_geojson": row.get("boundary_geojson") or None,
		"boundary_centroid": (
			{"lat": round(float(latitude), 7), "lon": round(float(longitude), 7)}
			if latitude not in (None, "") and longitude not in (None, "")
			else None
		),
		"boundary_bbox_geojson": row.get("boundary_bbox_geojson") or None,
		"h3_cell_counts": {resolution: len(entries) for resolution, entries in sorted(cells.items())},
		"area_computed_acres": _float_or_none(row.get("area_computed_acres")),
	}


def _tree_count(row: dict) -> int | None:
	acres = float(row.get("acreage") or 0)
	density = int(row.get("planting_density_per_acre") or 0)
	return round(acres * density) if acres and density else None


def _describe_zone(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"zone_name": row.get("zone_name"),
		"field": row.get("field"),
		"parcel": row.get("parcel") or None,
		"owning_entity": row.get("owning_entity") or None,
		"zone_number": int(row.get("zone_number") or 0) or None,
		"water_source": row.get("water_source") or None,
		"water_right_id": row.get("water_right_id") or None,
		"flow_rate_gpm": round(float(row.get("flow_rate_gpm") or 0), 2) or None,
		"sprinkler_type": row.get("sprinkler_type") or None,
		"area_sq_ft": round(float(row.get("area_sq_ft") or 0), 2),
		"area_acres": round(float(row.get("area_acres") or 0), 3),
		"water_test_last_date": _date_str(row.get("water_test_last_date")),
		"water_source_class": row.get("water_source_class") or None,
		"chlorination_active": compat.checked(row.get("chlorination_active")),
		**_boundary_summary(row),
		"notes": row.get("notes") or None,
	}


def _resolve_parcel(args: dict, key: str = "parcel", required: bool = True) -> str:
	value = as_str(args, key, required=required)
	if not value:
		return ""
	return str(parcel_row(value, _entity(args) or "")["name"])


def _known_varieties(company: str = "") -> list:
	"""Every variety already recorded on this site, for a caller to suggest from.

	This is the autosuggest. A hardcoded list would be wrong the first time
	somebody plants a variety that did not exist when this shipped; what is
	already in the ground cannot be.

	READS BOTH THE LEGACY COLUMN AND THE CHILD TABLE. A single-variety block that
	has never been touched since v0.142.0 still only has `Field.variety`; a
	multi-variety block's cultivars live in `Field Variety` and nowhere else. A
	suggestion list built from only one of the two would silently drop whichever
	half of the farm records its varieties the other way.
	"""
	filters = {"variety": ("is", "set")}
	if company:
		filters["owning_entity"] = company
	rows = frappe.db.get_all(FIELD, filters=filters, pluck="variety", limit=REGISTER_CAP) or []
	values = {str(value).strip() for value in rows if str(value or "").strip()}

	if compat.doctype_exists(FIELD_VARIETY):
		child_filters = {"variety": ("is", "set")}
		if company:
			# Field Variety carries no owning_entity of its own, so scoping to one
			# company means resolving that company's blocks first. None of them
			# means nothing to union — not "every company's varieties".
			field_names = frappe.db.get_all(
				FIELD, filters={"owning_entity": company}, pluck="name", limit=REGISTER_CAP
			)
			if not field_names:
				return sorted(values)
			child_filters["parent"] = ("in", field_names)
		child_rows = (
			frappe.db.get_all(FIELD_VARIETY, filters=child_filters, pluck="variety", limit=REGISTER_CAP * 20)
			or []
		)
		values |= {str(value).strip() for value in child_rows if str(value or "").strip()}

	return sorted(values)


# ── 100. list_fields ────────────────────────────────────────────────────────
def list_fields(args: dict) -> ToolResult:
	"""The block register, with acreage totalled and the plantings summarised."""
	_require(FIELD)
	company = _entity(args)
	limit = as_limit(args)

	filters = {}
	if company:
		filters["owning_entity"] = company
	parcel = as_str(args, "parcel")
	parcel_filter = _resolve_parcel(args) if parcel else None
	if parcel_filter:
		filters["parcel"] = parcel_filter
	for key in ("crop", "variety", "condition"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	# The ticker is folded to upper case on save, so a buyer quoting "yc-3"
	# has to be folded here too or the filter answers nothing and looks right.
	ticker = as_str(args, "block_ticker")
	if ticker:
		filters["block_ticker"] = ticker.strip().upper()
	if "food_safety_zone" in args:
		filters["food_safety_zone"] = 1 if as_bool(args, "food_safety_zone") else 0
	organic_status = as_str(args, "organic_status")
	if organic_status:
		filters["organic_status"] = as_choice(FIELD, "organic_status", organic_status, "organic_status")
	if "organic_certified" in args:
		filters["organic_certified"] = 1 if as_bool(args, "organic_certified") else 0
	linked = as_bool(args, "linked_to_cost_center")
	if linked is not None:
		filters["cost_center"] = ("is", "set") if linked else ("is", "not set")

	# A county filter is a PARCEL filter, resolved here, because the county is
	# the parcel's and a Field carries no copy of it. A county nothing is
	# registered in narrows to nothing rather than being ignored — silently
	# dropping it would answer a question about Wasco with every block on
	# the site.
	county = as_str(args, "county")
	if county:
		in_county = _parcels_in_county(county, company or "")
		if parcel_filter and parcel_filter not in in_county:
			raise ToolError(
				f"{parcel_filter} is not in {county!r}, so the two filters together match "
				"nothing. Pass one or the other."
			)
		if not parcel_filter:
			filters["parcel"] = ("in", in_county)

	rows = frappe.db.get_all(
		FIELD,
		filters=filters,
		fields=compat.existing_fields(FIELD, _FIELD_FIELDS),
		order_by="parcel asc, field_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	observed = _observed_spray_dates([row.get("name") for row in rows])
	counties = _parcel_counties([row.get("parcel") for row in rows])
	varieties = _field_varieties([row.get("name") for row in rows])
	fields = [_describe_field(dict(row), observed, counties, varieties) for row in rows]

	acreage = round(sum(row["acreage"] for row in fields), 2)
	planted = [row["planting_year"] for row in fields if row["planting_year"]]

	data = {
		"company": company,
		"parcel": parcel_filter,
		"county": county or None,
		"field_count": len(fields),
		"total_acreage": acreage,
		"average_acreage": round(acreage / len(fields), 2) if fields else 0.0,
		"oldest_planting_year": min(planted) if planted else None,
		"newest_planting_year": max(planted) if planted else None,
		"by_variety": dict(sorted(_variety_counts(fields).items())),
		"known_varieties": _known_varieties(company or ""),
		"without_acreage": [row["name"] for row in fields if not row["acreage"]],
		"spray_dates_from_farm_precision_ag": _spray_log_available(),
		**_organic_rollup(fields),
		**_county_rollup(fields),
		"fields": fields,
	}
	return ToolResult(
		data=data,
		summary=f"{len(fields)} field(s), {acreage} acres",
	)


def _parcels_in_county(county: str, company: str) -> list:
	"""Every parcel docname in one county, for the Field filter to run against.

	Refuses a county this site has no parcel in, and names the ones it does have.
	A filter that quietly matched nothing and a filter that quietly matched
	everything are both worse than a sentence: one reads as "we farm no ground
	there" and the other as "we farm all of it", and neither is what a typo means.
	"""
	if not compat.doctype_exists(PARCEL) or not compat.has_field(PARCEL, "county"):
		raise ToolError(
			"this site's Parcel has no county column, so blocks cannot be grouped by county. "
			"County is the parcel's fact and a Field never carries a copy of it."
		)
	scope = {"owning_entity": company} if company else {}
	names = frappe.db.get_all(PARCEL, filters={**scope, "county": county}, pluck="name", limit=REGISTER_CAP)
	if names:
		return sorted(names)
	known = sorted(
		{
			str(value).strip()
			for value in frappe.db.get_all(
				PARCEL, filters={**scope, "county": ("is", "set")}, pluck="county", limit=REGISTER_CAP
			)
			or []
			if str(value or "").strip()
		}
	)
	where = f" for {company}" if company else ""
	raise ToolError(
		f"no Parcel is recorded in {county!r}{where}. The counties this site does hold ground "
		f"in are: {', '.join(known) or '<none recorded>'}."
	)


def _organic_rollup(fields: list) -> dict:
	"""Certified and transitional acres, which is the survey line itself.

	CERTIFIED ACRES ARE SUMMED FROM THE STATUS, NOT COUNTED FROM THE CROP. One
	Crop record covers eight certified blocks and twelve conventional ones, so a
	crop-level flag cannot answer this at all — and the transitional figure is the
	one a crop-level flag cannot even represent.

	Blocks with no status are reported by name rather than folded into
	Conventional: a blank means nobody has answered, and quietly counting it as
	conventional would make an unanswered farm look like a fully-answered
	conventional one.
	"""
	acres: dict = {}
	for row in fields:
		key = row["organic_status"] or "(unrecorded)"
		acres[key] = round(acres.get(key, 0.0) + row["acreage"], 2)
	return {
		"organic_certified_acreage": acres.get(CERTIFIED_ORGANIC, 0.0),
		"organic_transitional_acreage": acres.get(TRANSITIONAL, 0.0),
		"acreage_by_organic_status": dict(sorted(acres.items())),
		"without_organic_status": [row["name"] for row in fields if not row["organic_status"]],
	}


def _variety_counts(fields: list) -> dict:
	"""How many blocks grow each variety.

	A MULTI-VARIETY BLOCK COUNTS ONCE UNDER EVERY VARIETY IT GROWS, read from the
	child table where a block records one there — the whole reason the table
	exists is that one block can carry more than one cultivar, and counting it
	once under whichever name the legacy column happens to hold would undercount
	every other variety it grows. A block with no child rows falls back to its
	single `variety` column, which is still the only answer a pre-v0.142.0 block
	has.
	"""
	counts: dict = {}
	for row in fields:
		names = {entry["variety"] for entry in row.get("varieties") or [] if entry.get("variety")}
		if not names:
			names = {row["variety"] or "(unrecorded)"}
		for name in names:
			counts[name] = counts.get(name, 0) + 1
	return counts


def _county_rollup(fields: list) -> dict:
	"""Acres by county, read through each block's parcel.

	This is "which counties do you operate in" as arithmetic. Operations rather
	than ownership is the point: leased ground counts, and it counts in the county
	of the parcel it sits on.
	"""
	acres: dict = {}
	for row in fields:
		key = row["county"] or "(unrecorded)"
		acres[key] = round(acres.get(key, 0.0) + row["acreage"], 2)
	return {
		"counties": sorted(key for key in acres if key != "(unrecorded)"),
		"acreage_by_county": dict(sorted(acres.items())),
	}


# ── 101. get_field ──────────────────────────────────────────────────────────
def get_field(args: dict) -> ToolResult:
	"""One block in full, with its zones and the water they are entitled to."""
	_require(FIELD)
	company = _entity(args)
	parcel = as_str(args, "parcel")
	row = field_row(
		as_str(args, "field", required=True),
		_resolve_parcel(args) if parcel else "",
		company or "",
	)
	observed = _observed_spray_dates([row.get("name")])
	described = _describe_field(row, observed)

	zones = []
	if compat.doctype_exists(IRRIGATION_ZONE):
		zones = [
			_describe_zone(dict(zone))
			for zone in frappe.db.get_all(
				IRRIGATION_ZONE,
				filters={"field": row["name"]},
				fields=compat.existing_fields(IRRIGATION_ZONE, _ZONE_FIELDS),
				order_by="zone_number asc, zone_name asc",
				limit=REGISTER_CAP,
			)
		]

	zone_acres = round(sum(zone["area_acres"] for zone in zones), 3)
	parcel_detail = {}
	if row.get("parcel"):
		parcel_detail = dict(
			frappe.db.get_value(
				PARCEL, row["parcel"], ["name", "parcel_name", "abbr", "acreage", "county"], as_dict=True
			)
			or {}
		)

	return ToolResult(
		data={
			**described,
			"parcel_detail": {
				"name": parcel_detail.get("name"),
				"parcel_name": parcel_detail.get("parcel_name"),
				"abbr": parcel_detail.get("abbr") or parcel_abbr(row.get("parcel") or ""),
				"acreage": round(float(parcel_detail.get("acreage") or 0), 2) or None,
				"county": parcel_detail.get("county") or None,
			}
			if parcel_detail
			else None,
			"zone_count": len(zones),
			"zone_acreage": zone_acres,
			"unzoned_acreage": round(max(0.0, described["acreage"] - zone_acres), 3),
			"water_rights": sorted({zone["water_right_id"] for zone in zones if zone["water_right_id"]}),
			"zones": zones,
		},
		summary=(
			f"{row['name']}: {described['acreage']} ac, {len(zones)} zone(s), "
			f"{described['variety'] or 'variety unrecorded'}"
		),
	)


def _field_variety_rows(raw) -> list[dict]:
	"""Validate and normalise the `varieties` argument.

	The whole list is checked before any of it is used, so a bad row at position
	four cannot leave rows one to three appended to a document that then fails —
	the same rule `agronomy._variety_rows` follows for Crop Variety, and for the
	same reason.

	SHAPE ONLY. Whether `variety` actually names one of this block's crop's own
	catalogue varieties, and whether the rows' `percentage` between them exceeds
	100, are both semantic checks that need the crop's own record in hand —
	`Field._check_varieties` makes them on `doc.insert()`/`doc.save()`, the same
	split `agronomy._water_rows` leaves to `Crop._check_water_requirements`
	rather than duplicating here.
	"""
	if raw in (None, ""):
		return []
	if not isinstance(raw, list):
		raise ToolError("varieties must be a list of objects, each with at least a variety.")
	allowed = set(_FIELD_VARIETY_FIELDS)
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
		variety = str(entry.get("variety") or "").strip()
		if not variety:
			raise ToolError(f"varieties[{index}] has no variety. Nothing was written.")
		out.append(
			{
				"variety": variety,
				"percentage": float(entry.get("percentage") or 0),
				"planting_year": int(entry.get("planting_year") or 0),
			}
		)
	return out


# ── 102. create_field ───────────────────────────────────────────────────────
def create_field(args: dict) -> ToolResult:
	"""Register one planted block under a parcel."""
	_require(FIELD)
	_require(PARCEL)
	parcel = _resolve_parcel(args)
	field_name = as_str(args, "field_name", required=True)

	existing = frappe.db.get_value(FIELD, {"field_name": field_name, "parcel": parcel}, "name")
	if existing:
		raise ToolError(
			f"{parcel} already has a block called {field_name!r} ({existing}). One block per name "
			"per parcel — change it with update_field, or name this one so a crew can tell them "
			"apart. Nothing was created."
		)

	uuid = as_str(args, "external_farm_app_id")
	if uuid:
		claimed = frappe.db.get_value(FIELD, {"external_farm_app_id": uuid}, "name")
		if claimed:
			raise ToolError(
				f"Farm App id {uuid!r} is already on {claimed!r}. That id is the other system's "
				"primary key, so two fields sharing it would make the sync bridge ambiguous. "
				"Nothing was created."
			)

	doc = frappe.new_doc(FIELD)
	doc.field_name = field_name
	doc.parcel = parcel
	doc.acreage = as_float(args.get("acreage"), "acreage")
	doc.crop = as_str(args, "crop") or "Cherry"
	doc.variety = as_str(args, "variety")
	doc.rootstock = as_str(args, "rootstock")
	doc.planting_year = as_int(args, "planting_year")
	doc.planting_density_per_acre = as_int(args, "planting_density_per_acre")
	doc.block_number = as_str(args, "block_number")
	doc.block_ticker = as_str(args, "block_ticker")
	doc.external_farm_app_id = uuid
	doc.notes = as_str(args, "notes")

	condition = as_str(args, "condition")
	if condition:
		doc.condition = as_choice(FIELD, "condition", condition, "condition")

	for date_key in (
		"last_spray_date",
		"water_test_last_date",
		"wildlife_intrusion_last_report",
		# v0.19.5. The dates that decide whether this block is in the per-acre
		# denominator at all. Written on creation because a block planted today is a
		# block whose pre-yield window is known today, and filling them in three
		# years later means reconstructing them from a planting year.
		"productive_from_date",
		"productive_through_date",
		"pre_yield_end_date",
	):
		doc.set(date_key, as_date(args, date_key))
	for check_key in ("food_safety_zone", "worker_hygiene_station_present"):
		value = as_bool(args, check_key)
		if value is not None:
			doc.set(check_key, 1 if value else 0)

	# v0.97.0. Certification is a fact about GROUND, so it is set on the block
	# and not on the crop. `organic_certified` is deliberately not settable —
	# the controller derives it from the status on every save.
	_reject_derived_organic(args)
	organic_status = as_str(args, "organic_status")
	if organic_status:
		doc.organic_status = as_choice(FIELD, "organic_status", organic_status, "organic_status")
	doc.organic_cert_agency = as_str(args, "organic_cert_agency")
	doc.transition_start_date = as_date(args, "transition_start_date")

	# v0.142.0. Multi-variety blocks — the Pearl blocks and any other field that
	# carries more than one cultivar. `variety`/`rootstock`/`planting_year`/
	# `planting_density_per_acre` above stay the primary answer for a
	# single-variety block; this is additive and nothing here is derived from it.
	for row in _field_variety_rows(args.get("varieties")):
		doc.append("varieties", row)

	doc.insert(ignore_permissions=True)

	described = _describe_field(dict(doc.as_dict()))
	warnings = _field_warnings(described, parcel)
	data = {**described, "known_varieties": _known_varieties(described["owning_entity"] or "")}
	if warnings:
		data["warnings"] = warnings
	return ToolResult(
		data=data,
		summary=f"registered field {doc.name} ({described['acreage']} ac on {parcel})",
		docstatus_delta="none → 0 (created)",
	)


def _field_warnings(described: dict, parcel: str) -> list:
	"""Say what is missing without refusing it.

	Every one of these is a real gap and none of them is this tool's business to
	block. A block with no acreage still needs a record so a spray can point at
	it; a block with no hygiene station is a fact worth writing down precisely
	*because* it stops a crew going in.
	"""
	out = []
	if not described["acreage"]:
		out.append(
			"No acreage recorded, so this block is not counted against the parcel's total and "
			"per-acre costing cannot include it."
		)
	if described["food_safety_zone"] and not described["worker_hygiene_station_present"]:
		out.append(
			"Marked a food safety zone with no worker hygiene station recorded. FSMA Subpart L "
			"and the Worker Protection Standard both require toilets and handwashing within a "
			"quarter mile before a crew works covered produce."
		)
	if described["food_safety_zone"] and not described["water_test_last_date"]:
		out.append(
			"Marked a food safety zone with no agricultural water test on record. Per-zone tests "
			"on the Irrigation Zone satisfy this too — record it in whichever place the water is "
			"actually managed."
		)
	out.extend(_organic_warnings(described))
	remaining = frappe.db.get_value(PARCEL, parcel, "acreage")
	if remaining:
		used = sum(
			float(row.get("acreage") or 0)
			for row in frappe.db.get_all(
				FIELD, filters={"parcel": parcel}, fields=["acreage"], limit=REGISTER_CAP
			)
		)
		out.append(
			f"{parcel} is {round(float(remaining), 2)} acres and its blocks now total {round(used, 2)}."
		)
	return out


def _organic_warnings(described: dict) -> list:
	"""The three ways the organic columns can contradict each other.

	NONE OF THEM REFUSES, and each is a real state some block is genuinely in. A
	block mid-application has a certifier and no certificate; a farm that has
	just started a transition may not have dug out the date yet. What is not
	acceptable is any of them passing unremarked, because the acreage sum is a
	number somebody signs.
	"""
	out = []
	status = described.get("organic_status")
	if described.get("organic_cert_agency") and status in (None, "Conventional"):
		out.append(
			f"A certifying agency is recorded and the organic status is "
			f"{status or 'unanswered'}. One of the two is out of date — the acreage sum reads "
			"the status, so this block is NOT counted as certified."
		)
	if status == TRANSITIONAL and not described.get("transition_start_date"):
		out.append(
			"Marked Transitional with no transition start date. The National Organic Program "
			"counts thirty-six months from the last prohibited application, so without the date "
			"nothing can say when this block becomes eligible."
		)
	if status == CERTIFIED_ORGANIC and not described.get("organic_cert_agency"):
		out.append(
			"Marked Certified Organic with no certifying agency recorded. The certificate is "
			"issued by somebody, and a survey line or a buyer asks who."
		)
	return out


# ── 103. update_field ───────────────────────────────────────────────────────
def update_field(args: dict) -> ToolResult:
	"""Change a registered block. Cannot re-key it and cannot move it to another parcel."""
	_require(FIELD)
	company = _entity(args)
	row = field_row(as_str(args, "field", required=True), "", company or "")

	if as_str(args, "field_name"):
		raise ToolError(
			"field_name cannot be changed: the docname is built from it and every irrigation "
			"zone points at that docname. Nothing was changed."
		)
	if as_str(args, "parcel"):
		raise ToolError(
			"a field cannot move between parcels. Ground does not move — a block that turns out "
			"to be on the neighbouring parcel was mis-registered, so delete it and register it "
			"where it is. Nothing was changed."
		)
	if as_str(args, "cost_center"):
		raise ToolError(
			"cost_center is set by link_field_to_cost_center, which checks the cost centre is on "
			"the same books and is not a group. Nothing was changed."
		)

	doc = frappe.get_doc(FIELD, row["name"])
	changes = {}

	for key in ("crop", "variety", "rootstock", "block_number", "block_ticker", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	for key in ("planting_year", "planting_density_per_acre"):
		if key in args:
			_stage(changes, doc, key, as_int(args, key) or 0)
	if "acreage" in args:
		_stage(changes, doc, "acreage", as_float(args.get("acreage"), "acreage"))
	if "condition" in args:
		value = as_str(args, "condition")
		_stage(changes, doc, "condition", as_choice(FIELD, "condition", value, "condition") if value else "")
	if "external_farm_app_id" in args:
		uuid = as_str(args, "external_farm_app_id")
		if uuid:
			claimed = frappe.db.get_value(
				FIELD, {"external_farm_app_id": uuid, "name": ("!=", row["name"])}, "name"
			)
			if claimed:
				raise ToolError(f"Farm App id {uuid!r} is already on {claimed!r}. Nothing was changed.")
		_stage(changes, doc, "external_farm_app_id", uuid)
	for key in (
		"last_spray_date",
		"water_test_last_date",
		"wildlife_intrusion_last_report",
		"productive_from_date",
		"productive_through_date",
		"pre_yield_end_date",
	):
		if key in args:
			_stage(changes, doc, key, as_date(args, key) or "")
	for key in ("food_safety_zone", "worker_hygiene_station_present"):
		if key in args:
			value = as_bool(args, key)
			_stage(changes, doc, key, 1 if value else 0)
	_reject_derived_organic(args)
	if "organic_status" in args:
		value = as_str(args, "organic_status")
		_stage(
			changes,
			doc,
			"organic_status",
			as_choice(FIELD, "organic_status", value, "organic_status") if value else "",
		)
	if "organic_cert_agency" in args:
		_stage(changes, doc, "organic_cert_agency", as_str(args, "organic_cert_agency"))
	if "transition_start_date" in args:
		_stage(changes, doc, "transition_start_date", as_date(args, "transition_start_date") or "")

	#: The child table is REPLACED WHOLESALE when passed, never merged — the same
	#: rule `agronomy.update_crop` follows for a crop's varieties, and for the
	#: same reason: these rows have no caller-visible stable key, so a merge has
	#: no way to tell "same variety, updated percentage" from "a different row
	#: that happens to share a name". Omitting the argument leaves the table
	#: untouched; passing an empty list is how a caller clears it.
	if "varieties" in args:
		wanted = _field_variety_rows(args.get("varieties"))
		changes["varieties"] = [f"{len(doc.varieties or [])} row(s)", f"{len(wanted)} row(s)"]
		doc.set("varieties", [])
		for entry in wanted:
			doc.append("varieties", entry)

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: acreage, crop, variety, rootstock, "
			"planting_year, planting_density_per_acre, varieties, condition, block_number, "
			"block_ticker, external_farm_app_id, last_spray_date, water_test_last_date, "
			"wildlife_intrusion_last_report, productive_from_date, productive_through_date, "
			"pre_yield_end_date, food_safety_zone, worker_hygiene_station_present, "
			"organic_status, organic_cert_agency, transition_start_date, "
			"notes."
		)

	doc.save(ignore_permissions=True)
	described = _describe_field(dict(doc.as_dict()))
	return ToolResult(
		data={
			**described,
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
			"warnings": _field_warnings(described, described["parcel"]),
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


def _reject_derived_organic(args: dict) -> None:
	"""`organic_certified` is computed, and a tool that took it would create drift.

	The same rule the boundary's derived columns are under, said out loud rather
	than ignored: a caller who sets the flag and leaves the status alone has two
	answers to "are these acres certified", and the next save overwrites theirs
	anyway. Refusing names the field that actually decides it.
	"""
	if "organic_certified" in args:
		raise ToolError(
			"organic_certified cannot be set: it is DERIVED from organic_status on every save, "
			"so a value written here would be overwritten by the next one. Set organic_status "
			f"to {CERTIFIED_ORGANIC!r} instead. Nothing was changed."
		)


def _stage(changes: dict, doc, field: str, wanted) -> None:
	"""Set one field, and record before → after only when it actually moved.

	Comparison is on the string form because the value coming in has been through
	`as_int`/`as_float`/`as_date` and the value on the document came out of the
	database, so `0` meets `"0"` and `2026-05-15` meets a `date` object. A
	comparison that got that wrong would report every unchanged field as changed,
	which makes the echo useless exactly when somebody is checking it.
	"""
	before = doc.get(field)
	before = "" if before is None else before
	if str(before) == str(wanted):
		return
	changes[field] = [before or None, wanted or None]
	doc.set(field, wanted if wanted != "" else None)


# ── 104. list_irrigation_zones ──────────────────────────────────────────────
def list_irrigation_zones(args: dict) -> ToolResult:
	"""The zone register, with area and flow totalled and the water rights named."""
	_require(IRRIGATION_ZONE)
	company = _entity(args)
	limit = as_limit(args)

	filters = {}
	if company:
		filters["owning_entity"] = company
	field = as_str(args, "field")
	if field:
		filters["field"] = field_row(field, "", company or "")["name"]
	parcel = as_str(args, "parcel")
	if parcel:
		filters["parcel"] = _resolve_parcel(args)
	for key in ("water_source", "sprinkler_type", "water_source_class"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	if "chlorination_active" in args:
		filters["chlorination_active"] = 1 if as_bool(args, "chlorination_active") else 0

	rows = frappe.db.get_all(
		IRRIGATION_ZONE,
		filters=filters,
		fields=compat.existing_fields(IRRIGATION_ZONE, _ZONE_FIELDS),
		order_by="parcel asc, field asc, zone_number asc",
		limit=min(limit, REGISTER_CAP),
	)
	zones = [_describe_zone(dict(row)) for row in rows]

	by_source: dict = {}
	for zone in zones:
		key = zone["water_source"] or "(unrecorded)"
		by_source[key] = by_source.get(key, 0) + 1

	untested = [zone["name"] for zone in zones if not zone["water_test_last_date"]]
	unrighted = [
		zone["name"]
		for zone in zones
		if not zone["water_right_id"] and zone["water_source"] in ("creek", "pond", "shared")
	]

	return ToolResult(
		data={
			"company": company,
			"zone_count": len(zones),
			"total_area_acres": round(sum(zone["area_acres"] for zone in zones), 3),
			"total_flow_gpm": round(sum(zone["flow_rate_gpm"] or 0 for zone in zones), 2),
			"by_water_source": dict(sorted(by_source.items())),
			"water_rights": sorted({zone["water_right_id"] for zone in zones if zone["water_right_id"]}),
			"without_water_test": untested,
			"surface_water_without_a_right": unrighted,
			"zones": zones,
		},
		summary=(
			f"{len(zones)} zone(s), {round(sum(zone['area_acres'] for zone in zones), 2)} acres, "
			f"{len(untested)} with no water test"
		),
	)


# ── 105. get_irrigation_zone ────────────────────────────────────────────────
def get_irrigation_zone(args: dict) -> ToolResult:
	"""One zone in full, with the block it waters and that block's share of it."""
	_require(IRRIGATION_ZONE)
	company = _entity(args)
	field = as_str(args, "field")
	row = zone_row(
		as_str(args, "zone", required=True),
		field_row(field, "", company or "")["name"] if field else "",
		company or "",
	)
	described = _describe_zone(row)

	block = {}
	if row.get("field"):
		block = dict(
			frappe.db.get_value(
				FIELD,
				row["field"],
				["name", "field_name", "acreage", "variety", "food_safety_zone"],
				as_dict=True,
			)
			or {}
		)
	siblings = frappe.db.get_all(
		IRRIGATION_ZONE,
		filters={"field": row.get("field")},
		fields=["name", "area_acres"],
		limit=REGISTER_CAP,
	)
	zoned = round(sum(float(sibling.get("area_acres") or 0) for sibling in siblings), 3)

	notes = []
	if compat.checked(block.get("food_safety_zone")) and not described["water_test_last_date"]:
		notes.append(
			"This zone waters a food safety block and has no agricultural water test on record. "
			"FSMA Subpart E sets the cadence by water source class, which is also unrecorded here."
		)
	if described["water_source"] in ("creek", "pond", "shared") and not described["water_right_id"]:
		notes.append(
			"Surface water with no water right recorded. In Oregon a surface diversion without a "
			"right or certificate is not something a record should be silent about."
		)

	return ToolResult(
		data={
			**described,
			"field_detail": {
				"name": block.get("name"),
				"field_name": block.get("field_name"),
				"acreage": round(float(block.get("acreage") or 0), 2) or None,
				"variety": block.get("variety") or None,
				"food_safety_zone": compat.checked(block.get("food_safety_zone")),
			}
			if block
			else None,
			"zones_on_this_field": len(siblings),
			"field_acreage_zoned": zoned,
			"share_of_field": (
				round(described["area_acres"] / float(block["acreage"]), 3) if block.get("acreage") else None
			),
			"compliance_notes": notes,
		},
		summary=(
			f"{row['name']}: {described['area_acres']} ac off "
			f"{described['water_source'] or 'an unrecorded source'}"
		),
	)


# ── 106. create_irrigation_zone ─────────────────────────────────────────────
def create_irrigation_zone(args: dict) -> ToolResult:
	"""Register one irrigation zone under a block."""
	_require(IRRIGATION_ZONE)
	_require(FIELD)
	company = _entity(args)
	field = field_row(as_str(args, "field", required=True), "", company or "")
	zone_name = as_str(args, "zone_name", required=True)

	existing = frappe.db.get_value(
		IRRIGATION_ZONE, {"zone_name": zone_name, "parcel": field.get("parcel")}, "name"
	)
	if existing:
		raise ToolError(
			f"{field.get('parcel')} already has a zone called {zone_name!r} ({existing}). Zone "
			"names are unique within a parcel because the docname is filed under the parcel — "
			"name this one for its field, the way 'YC3-Zone2' does. Nothing was created."
		)

	zone_number = as_int(args, "zone_number")
	if zone_number is not None:
		clash = frappe.db.get_value(
			IRRIGATION_ZONE, {"zone_number": zone_number, "field": field["name"]}, "name"
		)
		if clash:
			raise ToolError(
				f"zone number {zone_number} on {field['name']} is already {clash!r}. That number "
				"is what somebody types into the controller at two in the morning; two answers "
				"to it means water goes somewhere nobody chose. Nothing was created."
			)

	doc = frappe.new_doc(IRRIGATION_ZONE)
	doc.zone_name = zone_name
	doc.field = field["name"]
	doc.zone_number = zone_number
	doc.water_right_id = as_str(args, "water_right_id")
	doc.flow_rate_gpm = as_float(args.get("flow_rate_gpm"), "flow_rate_gpm")
	doc.area_sq_ft = as_float(args.get("area_sq_ft"), "area_sq_ft")
	doc.notes = as_str(args, "notes")
	doc.water_test_last_date = as_date(args, "water_test_last_date")

	for key in ("water_source", "sprinkler_type", "water_source_class"):
		value = as_str(args, key)
		if value:
			doc.set(key, as_choice(IRRIGATION_ZONE, key, value, key))
	chlorination = as_bool(args, "chlorination_active")
	if chlorination is not None:
		doc.chlorination_active = 1 if chlorination else 0

	if "area_acres" in args and "area_sq_ft" not in args:
		raise ToolError(
			"area_acres is computed from area_sq_ft and cannot be set directly. Two figures a "
			"caller can set independently are two figures that will disagree. Pass area_sq_ft "
			f"— {round(as_float(args.get('area_acres'), 'area_acres') * 43560, 2)} sq ft is the "
			"area you named. Nothing was created."
		)

	doc.insert(ignore_permissions=True)
	described = _describe_zone(dict(doc.as_dict()))
	warnings = []
	if not described["area_sq_ft"]:
		warnings.append(
			"No area recorded, so this zone is not counted against the block's acreage and per-"
			"zone water costing cannot include it."
		)
	if described["water_source"] in ("creek", "pond", "shared") and not described["water_right_id"]:
		warnings.append(
			"Surface water with no water right recorded. Oregon does not treat a creek diversion "
			"as self-evident."
		)
	if compat.checked(field.get("food_safety_zone")) and not described["water_test_last_date"]:
		warnings.append(
			f"{field['name']} is a food safety block and this zone has no water test on record "
			"(FSMA Subpart E)."
		)

	return ToolResult(
		data={**described, "warnings": warnings} if warnings else described,
		summary=f"registered zone {doc.name} ({described['area_acres']} ac on {field['name']})",
		docstatus_delta="none → 0 (created)",
	)


# ── 107. update_irrigation_zone ─────────────────────────────────────────────
def update_irrigation_zone(args: dict) -> ToolResult:
	"""Change a registered zone. Cannot re-key it and cannot move it to another block."""
	_require(IRRIGATION_ZONE)
	company = _entity(args)
	row = zone_row(as_str(args, "zone", required=True), "", company or "")

	if as_str(args, "zone_name"):
		raise ToolError("zone_name cannot be changed: the docname is built from it. Nothing was changed.")
	if as_str(args, "field"):
		raise ToolError(
			"a zone cannot move between blocks — pipe does not move. Register the zone where the "
			"water actually goes and retire this one. Nothing was changed."
		)
	if "area_acres" in args:
		raise ToolError(
			"area_acres is computed from area_sq_ft on every save and cannot be set directly. "
			"Nothing was changed."
		)

	doc = frappe.get_doc(IRRIGATION_ZONE, row["name"])
	changes = {}

	if "zone_number" in args:
		wanted = as_int(args, "zone_number")
		if wanted is not None:
			clash = frappe.db.get_value(
				IRRIGATION_ZONE,
				{"zone_number": wanted, "field": row["field"], "name": ("!=", row["name"])},
				"name",
			)
			if clash:
				raise ToolError(
					f"zone number {wanted} on {row['field']} is already {clash!r}. Nothing was changed."
				)
		_stage(changes, doc, "zone_number", wanted or 0)
	for key in ("water_right_id", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	for key in ("flow_rate_gpm", "area_sq_ft"):
		if key in args:
			_stage(changes, doc, key, as_float(args.get(key), key))
	for key in ("water_source", "sprinkler_type", "water_source_class"):
		if key in args:
			value = as_str(args, key)
			_stage(changes, doc, key, as_choice(IRRIGATION_ZONE, key, value, key) if value else "")
	if "water_test_last_date" in args:
		_stage(changes, doc, "water_test_last_date", as_date(args, "water_test_last_date") or "")
	if "chlorination_active" in args:
		_stage(changes, doc, "chlorination_active", 1 if as_bool(args, "chlorination_active") else 0)

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: zone_number, water_source, water_right_id, "
			"flow_rate_gpm, sprinkler_type, area_sq_ft, water_test_last_date, water_source_class, "
			"chlorination_active, notes."
		)

	doc.save(ignore_permissions=True)
	return ToolResult(
		data={
			**_describe_zone(dict(doc.as_dict())),
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ── 108. link_field_to_cost_center ──────────────────────────────────────────
def link_field_to_cost_center(args: dict) -> ToolResult:
	"""Point a block at the Cost Center its costs are booked to."""
	_require(FIELD)
	company = _entity(args)
	row = field_row(as_str(args, "field", required=True), "", company or "")
	entity = row.get("owning_entity") or company
	wanted = as_str(args, "cost_center", required=True)

	# Resolved against this block's own company first, so a bare "Harvest" finds
	# the right one on a multi-company site. Only if that finds nothing is it
	# resolved site-wide — and then the refusal can say WHY a cost center that
	# plainly exists is not usable here, which the resolver's own "belongs to
	# company X, not Y" says less usefully.
	try:
		cost_center = resolve_cost_center(wanted, entity or "")
	except ToolError:
		elsewhere = resolve_cost_center(wanted)
		owner = frappe.db.get_value("Cost Center", elsewhere, "company")
		raise ToolError(
			f"Cost Center {elsewhere!r} is on {owner!r}'s books and this block is on "
			f"{entity!r}'s. A cost allocated across two companies is an intercompany "
			"transaction, not a dimension — book it on this company's own tree, and settle "
			"between the two entities separately. Nothing was changed."
		) from None

	detail = dict(
		frappe.db.get_value(
			"Cost Center", cost_center, ["name", "company", "is_group", "disabled"], as_dict=True
		)
		or {}
	)
	if compat.checked(detail.get("is_group")):
		raise ToolError(
			f"{cost_center!r} is a group cost center, and ERPNext will not let a posting land on "
			"one. Pick a leaf underneath it. Nothing was changed."
		)
	if compat.checked(detail.get("disabled")):
		raise ToolError(f"Cost Center {cost_center!r} is disabled. Nothing was changed.")

	before = row.get("cost_center") or ""
	replace = as_bool(args, "replace", False)
	if before and before != cost_center and not replace:
		raise ToolError(
			f"{row['name']} is already booked to {before!r}. Repointing it means this season's "
			"costs and last season's land in different places — pass replace=true if that is "
			"what you want. Nothing was changed."
		)

	claimed = [
		name
		for name in (
			frappe.db.get_all(FIELD, filters={"cost_center": cost_center}, pluck="name", limit=25) or []
		)
		if name != row["name"]
	]
	dry_run = as_bool(args, "dry_run", False)
	data = {
		"field": row["name"],
		"cost_center": cost_center,
		"previous_cost_center": before or None,
		"company": entity,
		"acreage": round(float(row.get("acreage") or 0), 2),
		"shared_with": claimed,
		"dry_run": bool(dry_run),
		"changed": False,
	}
	if claimed:
		data["note"] = (
			f"{len(claimed)} other block(s) already book to {cost_center}: {', '.join(sorted(claimed))}. "
			"That is allowed and is often right — a cost center per orchard rather than per block "
			"— but per-block costing needs a cost center per block."
		)
	if dry_run:
		return ToolResult(data=data, summary=f"dry run: would book {row['name']} to {cost_center}")
	if before == cost_center:
		return ToolResult(data=data, summary=f"{row['name']} already books to {cost_center}")

	frappe.db.set_value(FIELD, row["name"], "cost_center", cost_center)
	data["changed"] = True
	return ToolResult(
		data=data,
		summary=f"{row['name']} now books to {cost_center}",
		docstatus_delta="0 → 0 (updated)",
	)


# ── 109. get_parcel_field_summary ───────────────────────────────────────────
def get_parcel_field_summary(args: dict) -> ToolResult:
	"""One parcel's blocks and zones rolled up: acres, ages, condition, water."""
	_require(FIELD)
	_require(PARCEL)
	company = _entity(args)
	parcel = parcel_row(as_str(args, "parcel", required=True), company or "")

	rows = frappe.db.get_all(
		FIELD,
		filters={"parcel": parcel["name"]},
		fields=compat.existing_fields(FIELD, _FIELD_FIELDS),
		order_by="field_name asc",
		limit=REGISTER_CAP,
	)
	observed = _observed_spray_dates([row.get("name") for row in rows])
	counties = _parcel_counties([parcel["name"]])
	varieties_by_field = _field_varieties([row.get("name") for row in rows])
	fields = [_describe_field(dict(row), observed, counties, varieties_by_field) for row in rows]

	zones = []
	if compat.doctype_exists(IRRIGATION_ZONE):
		zones = [
			_describe_zone(dict(row))
			for row in frappe.db.get_all(
				IRRIGATION_ZONE,
				filters={"parcel": parcel["name"]},
				fields=compat.existing_fields(IRRIGATION_ZONE, _ZONE_FIELDS),
				limit=REGISTER_CAP * 4,
			)
		]

	acreage = round(sum(row["acreage"] for row in fields), 2)
	parcel_acres = round(float(parcel.get("acreage") or 0), 2)
	planted = [row["planting_year"] for row in fields if row["planting_year"]]
	conditions: dict = {}
	for row in fields:
		key = row["condition"] or "(unrecorded)"
		conditions[key] = conditions.get(key, 0) + 1

	return ToolResult(
		data={
			"parcel": parcel["name"],
			"parcel_name": parcel.get("parcel_name"),
			"abbr": parcel.get("abbr") or parcel_abbr(parcel["name"]),
			"owning_entity": parcel.get("owning_entity"),
			"parcel_acreage": parcel_acres,
			"field_count": len(fields),
			"planted_acreage": acreage,
			"unassigned_acreage": round(parcel_acres - acreage, 2) if parcel_acres else None,
			"average_field_acreage": round(acreage / len(fields), 2) if fields else 0.0,
			"zone_count": len(zones),
			"zoned_acreage": round(sum(zone["area_acres"] for zone in zones), 3),
			"average_zones_per_field": round(len(zones) / len(fields), 2) if fields else 0.0,
			"total_flow_gpm": round(sum(zone["flow_rate_gpm"] or 0 for zone in zones), 2),
			"oldest_planting_year": min(planted) if planted else None,
			"newest_planting_year": max(planted) if planted else None,
			"by_condition": dict(sorted(conditions.items())),
			"by_variety": dict(sorted(_variety_counts(fields).items())),
			"water_rights": sorted({zone["water_right_id"] for zone in zones if zone["water_right_id"]}),
			"food_safety_blocks": [row["name"] for row in fields if row["food_safety_zone"]],
			"blocks_without_hygiene_station": [
				row["name"]
				for row in fields
				if row["food_safety_zone"] and not row["worker_hygiene_station_present"]
			],
			"zones_without_water_test": [zone["name"] for zone in zones if not zone["water_test_last_date"]],
			"fields": [
				{
					"name": row["name"],
					"acreage": row["acreage"],
					"variety": row["variety"],
					"planting_year": row["planting_year"],
					"condition": row["condition"],
					"zone_count": len([zone for zone in zones if zone["field"] == row["name"]]),
				}
				for row in fields
			],
		},
		summary=(
			f"{parcel.get('parcel_name') or parcel['name']}: {len(fields)} fields, {acreage} ac, "
			f"{len(zones)} zones"
		),
	)


# ── 110. import_farm_app_fields ─────────────────────────────────────────────
def import_farm_app_fields(args: dict) -> ToolResult:
	"""Create Fields from a batch of legacy Farm App records, carrying their ids.

	Dry run by default, whole-batch validated before the first insert, and it
	never updates an existing record — see the module docstring.
	"""
	_require(FIELD)
	_require(PARCEL)
	company = _entity(args)
	records = args.get("records")
	if not isinstance(records, list) or not records:
		raise ToolError(
			"records must be a non-empty array of legacy field objects, each with at least "
			"`name`. Recognised keys: " + ", ".join(sorted(IMPORT_KEYS)) + ". Nothing was created."
		)
	if len(records) > IMPORT_CAP:
		raise ToolError(
			f"{len(records)} records is more than this takes in one call ({IMPORT_CAP}). Import "
			"in batches you can actually check. Nothing was created."
		)

	default_parcel = as_str(args, "parcel")
	default_parcel = _resolve_parcel(args) if default_parcel else ""
	apply = as_bool(args, "apply", False)

	# Whole batch first. A half-imported farm is worse than an unimported one:
	# the second run has to work out which half.
	plan, seen_names, seen_uuids = [], set(), set()
	for index, record in enumerate(records, start=1):
		plan.append(_plan_import(record, index, default_parcel, company or "", seen_names, seen_uuids))

	fresh = [entry for entry in plan if entry["action"] == "create"]
	skipped = [entry for entry in plan if entry["action"] != "create"]

	data = {
		"record_count": len(plan),
		"would_create": len(fresh),
		"already_present": len(skipped),
		"applied": False,
		"plan": plan,
		"note": (
			"This is the schema-alignment half of the Farm App migration: it creates ERPNext "
			"Fields carrying `external_farm_app_id` so a later sync engine has something to "
			"match on. It never updates an existing Field and never writes back to the Farm App."
		),
	}
	if not apply:
		return ToolResult(
			data={**data, "dry_run": True},
			summary=f"dry run: {len(fresh)} of {len(plan)} record(s) would be created",
		)

	created = []
	for entry in fresh:
		doc = frappe.new_doc(FIELD)
		doc.field_name = entry["field_name"]
		doc.parcel = entry["parcel"]
		doc.acreage = entry["acreage"]
		doc.variety = entry["variety"]
		doc.planting_year = entry["planting_year"]
		doc.block_number = entry["block_number"]
		doc.external_farm_app_id = entry["farm_app_uuid"]
		doc.crop = "Cherry"
		doc.insert(ignore_permissions=True)
		entry["created_as"] = doc.name
		created.append(doc.name)

	return ToolResult(
		data={**data, "applied": True, "dry_run": False, "created": created},
		summary=f"imported {len(created)} field(s) of {len(plan)} record(s)",
		docstatus_delta="none → 0 (created)" if created else "",
	)


def _plan_import(
	record, index: int, default_parcel: str, company: str, seen_names: set, seen_uuids: set
) -> dict:
	"""Validate one legacy record and say what would happen to it."""
	if not isinstance(record, dict):
		raise ToolError(f"record {index} is not an object. Nothing was created.")
	unknown = sorted(set(record) - IMPORT_KEYS)
	if unknown:
		raise ToolError(
			f"record {index} has key(s) this does not know: {', '.join(unknown)}. Recognised: "
			f"{', '.join(sorted(IMPORT_KEYS))}. A typo silently dropped is a field somebody "
			"thinks they imported. Nothing was created."
		)

	field_name = str(record.get("name") or "").strip()
	if not field_name:
		raise ToolError(f"record {index} has no `name`. Nothing was created.")

	hint = str(record.get("parcel_hint") or "").strip()
	if hint:
		try:
			parcel = str(parcel_row(hint, company)["name"])
		except ToolError as error:
			raise ToolError(
				f"record {index} ({field_name}): {error}. Register the parcel first with "
				"create_parcel, or pass a default `parcel`. Nothing was created."
			) from None
	elif default_parcel:
		parcel = default_parcel
	else:
		raise ToolError(
			f"record {index} ({field_name}) has no `parcel_hint` and no default `parcel` was "
			"given. Nothing was created."
		)

	key = (field_name, parcel)
	if key in seen_names:
		raise ToolError(
			f"record {index} repeats {field_name!r} on {parcel} — the batch contradicts itself. "
			"Nothing was created."
		)
	seen_names.add(key)

	uuid = str(record.get("farm_app_uuid") or "").strip()
	if uuid:
		if uuid in seen_uuids:
			raise ToolError(
				f"record {index} repeats Farm App id {uuid!r} inside this batch. Nothing was created."
			)
		seen_uuids.add(uuid)

	acreage = as_float(record.get("acreage"), f"record {index} acreage")
	if acreage < 0:
		raise ToolError(f"record {index} ({field_name}) has negative acreage. Nothing was created.")
	planting_year = as_int({"planting_year": record.get("planting_year")}, "planting_year")

	entry = {
		"index": index,
		"field_name": field_name,
		"parcel": parcel,
		"acreage": acreage,
		"variety": str(record.get("variety") or "").strip(),
		"planting_year": planting_year,
		"block_number": str(record.get("block_number") or "").strip(),
		"farm_app_uuid": uuid,
		"action": "create",
		"reason": "",
	}

	existing = frappe.db.get_value(FIELD, {"field_name": field_name, "parcel": parcel}, "name")
	if existing:
		entry["action"] = "skip"
		entry["reason"] = f"{existing} already records this block. This tool never updates."
		entry["existing"] = existing
		return entry
	if uuid:
		claimed = frappe.db.get_value(FIELD, {"external_farm_app_id": uuid}, "name")
		if claimed:
			entry["action"] = "skip"
			entry["reason"] = f"{claimed} already carries Farm App id {uuid}."
			entry["existing"] = claimed
	return entry


# ── 117. set_field_boundary ─────────────────────────────────────────────────
def set_field_boundary(args: dict) -> ToolResult:
	"""Give a block its shape on the ground, and derive everything indexable from it."""
	_require(FIELD)
	geo.require()
	company = _entity(args)
	row = field_row(as_str(args, "field", required=True), "", company or "")

	geometry = geo.parse(args.get("boundary_geojson"))
	derived = geo.derive(geometry)
	shape = derived.pop("shape")

	ratio, verdict = geo.area_disagreement(row.get("acreage"), derived["area_computed_acres"])
	entered = round(float(row.get("acreage") or 0), 2)
	if verdict == "refuse":
		raise ToolError(
			f"the boundary encloses {derived['area_computed_acres']} acres and {row['name']} is "
			f"recorded as {entered} — a difference of {round(ratio * 100, 1)}%. That is not a "
			"survey disagreement, it is one of the two figures being about a different piece of "
			"ground. Fix the acreage with update_field, or send the right polygon. Nothing was "
			"changed."
		)

	warnings = list(geo.check_coordinates_look_like_degrees(geometry, "boundary_geojson"))
	if verdict == "warn":
		warnings.append(
			f"The boundary encloses {derived['area_computed_acres']} acres against a recorded "
			f"{entered} — {round(ratio * 100, 1)}%. A deed, a GIS trace and a tape measure "
			"routinely disagree by a few percent; both figures are kept and neither is "
			"overwritten."
		)
	if not entered:
		warnings.append(
			f"No acreage was recorded on this block, so nothing was compared. The polygon says "
			f"{derived['area_computed_acres']} acres — set it with update_field if that is right."
		)

	# v0.32.0. Parcels carry a polygon now, so this is a real check rather than
	# the apology it was from v0.12.0 to v0.31.0 — every call used to end with a
	# line saying a parcel had no boundary and nothing had been checked.
	#
	# REPORTED, NEVER REFUSED, for the same reason a zone outside its field is: a
	# block genuinely does straddle a deed line on plenty of farms, because the
	# planting predates the split. And an unmapped parcel is still said out loud,
	# because "nothing was checked" and "it checked out" are different answers.
	parcel_shape = geo.stored_shape(
		frappe.db.get_value(PARCEL, row.get("parcel"), "boundary_geojson")
		if row.get("parcel") and compat.has_field(PARCEL, "boundary_geojson")
		else None
	)
	if parcel_shape is None:
		inside_parcel = None
		warnings.append(
			f"{row.get('parcel')} has no boundary of its own, so nothing checked that this block "
			"sits inside its parcel. Set it with set_parcel_boundary."
		)
	else:
		inside_parcel = geo.covers_shape(parcel_shape, shape)
		if not inside_parcel:
			warnings.append(
				f"This block is not fully inside {row.get('parcel')}'s boundary. That is allowed and "
				"is sometimes right — a planting that predates a deed split really does straddle the "
				"line — but if it was not deliberate, one of the two polygons is wrong."
			)

	zones_outside = _zones_outside(row["name"], shape)
	if zones_outside:
		warnings.append(
			f"{len(zones_outside)} zone(s) on this block now fall outside its boundary: "
			f"{', '.join(zones_outside)}. That is allowed — a shared line crosses boundaries — "
			"but check it is deliberate."
		)

	dry_run = as_bool(args, "dry_run", False)
	data = {
		"field": row["name"],
		"parcel": row.get("parcel"),
		"acreage_recorded": entered or None,
		"area_computed_acres": derived["area_computed_acres"],
		"area_disagreement_ratio": ratio or None,
		"boundary_centroid": {
			"lat": derived["boundary_centroid_lat"],
			"lon": derived["boundary_centroid_lon"],
		},
		"boundary_bbox_geojson": derived["boundary_bbox_geojson"],
		"h3_cell_counts": {
			resolution: len(cells) for resolution, cells in sorted(json.loads(derived["h3_cells"]).items())
		},
		"h3_resolutions": list(geo.H3_RESOLUTIONS),
		"boundary_contained_in_parcel": inside_parcel,
		"zones_outside_boundary": zones_outside,
		"warnings": warnings,
		"dry_run": bool(dry_run),
		"changed": False,
	}
	if dry_run:
		return ToolResult(
			data=data,
			summary=(f"dry run: would set a {derived['area_computed_acres']}-acre boundary on {row['name']}"),
		)

	doc = frappe.get_doc(FIELD, row["name"])
	for fieldname, value in derived.items():
		doc.set(fieldname, value)
	doc.save(ignore_permissions=True)

	data["changed"] = True
	return ToolResult(
		data=data,
		summary=(
			f"{row['name']}: boundary set, {derived['area_computed_acres']} acres, "
			f"centroid {derived['boundary_centroid_lat']},{derived['boundary_centroid_lon']}"
		),
		docstatus_delta="0 → 0 (updated)",
	)


def _zones_outside(field: str, shape) -> list:
	"""Zones on this block whose own boundary is not covered by the block's."""
	if not compat.doctype_exists(IRRIGATION_ZONE):
		return []
	out = []
	for row in (
		frappe.db.get_all(
			IRRIGATION_ZONE,
			filters={"field": field, "boundary_geojson": ("is", "set")},
			fields=["name", "boundary_geojson"],
			limit=REGISTER_CAP,
		)
		or []
	):
		zone_shape = geo.stored_shape(row.get("boundary_geojson"))
		if zone_shape is not None and not geo.covers_shape(shape, zone_shape):
			out.append(row["name"])
	return sorted(out)


# ── 118. set_zone_boundary ──────────────────────────────────────────────────
def set_zone_boundary(args: dict) -> ToolResult:
	"""Give a zone its shape, and say whether it sits inside the block it waters."""
	_require(IRRIGATION_ZONE)
	geo.require()
	company = _entity(args)
	row = zone_row(as_str(args, "zone", required=True), "", company or "")

	geometry = geo.parse(args.get("boundary_geojson"))
	derived = geo.derive(geometry)
	shape = derived.pop("shape")

	ratio, verdict = geo.area_disagreement(row.get("area_acres"), derived["area_computed_acres"])
	entered = round(float(row.get("area_acres") or 0), 3)
	if verdict == "refuse":
		raise ToolError(
			f"the boundary encloses {derived['area_computed_acres']} acres and {row['name']} is "
			f"recorded as {entered} ({round(float(row.get('area_sq_ft') or 0), 2)} sq ft) — a "
			f"difference of {round(ratio * 100, 1)}%. One of the two is about a different zone. "
			"Fix the area with update_irrigation_zone, or send the right polygon. Nothing was "
			"changed."
		)

	warnings = list(geo.check_coordinates_look_like_degrees(geometry, "boundary_geojson"))
	if verdict == "warn":
		warnings.append(
			f"The boundary encloses {derived['area_computed_acres']} acres against a recorded "
			f"{entered} — {round(ratio * 100, 1)}%."
		)

	# Containment is REPORTED, never enforced. A shared water line crosses a
	# boundary, a pump house sits on the headland, a mainline runs down a road
	# easement. Refusing those would make them unrecordable, which is worse than
	# recording them with a note.
	field_boundary = frappe.db.get_value(FIELD, row.get("field"), "boundary_geojson")
	field_shape = geo.stored_shape(field_boundary)
	if field_shape is None:
		contained = None
		warnings.append(
			f"{row.get('field')} has no boundary of its own, so nothing checked that this zone "
			"sits inside it. Set the block's boundary first with set_field_boundary."
		)
	else:
		contained = geo.covers_shape(field_shape, shape)
		if not contained:
			warnings.append(
				f"This zone is not fully inside {row.get('field')}'s boundary. That is allowed and "
				"is sometimes right — a shared line, a pump house on the headland, a mainline down "
				"an easement — but if it was not deliberate, one of the two polygons is wrong."
			)

	dry_run = as_bool(args, "dry_run", False)
	data = {
		"zone": row["name"],
		"field": row.get("field"),
		"parcel": row.get("parcel"),
		"area_recorded_acres": entered or None,
		"area_computed_acres": derived["area_computed_acres"],
		"area_disagreement_ratio": ratio or None,
		"boundary_centroid": {
			"lat": derived["boundary_centroid_lat"],
			"lon": derived["boundary_centroid_lon"],
		},
		"boundary_bbox_geojson": derived["boundary_bbox_geojson"],
		"h3_cell_counts": {
			resolution: len(cells) for resolution, cells in sorted(json.loads(derived["h3_cells"]).items())
		},
		"boundary_contained_in_field": contained,
		"warnings": warnings,
		"dry_run": bool(dry_run),
		"changed": False,
	}
	if dry_run:
		return ToolResult(
			data=data,
			summary=f"dry run: would set a {derived['area_computed_acres']}-acre boundary on {row['name']}",
		)

	doc = frappe.get_doc(IRRIGATION_ZONE, row["name"])
	for fieldname, value in derived.items():
		doc.set(fieldname, value)
	doc.save(ignore_permissions=True)

	data["changed"] = True
	return ToolResult(
		data=data,
		summary=(
			f"{row['name']}: boundary set, {derived['area_computed_acres']} acres, "
			+ (
				"inside its block"
				if contained
				else ("outside its block" if contained is False else "block has no boundary")
			)
		),
		docstatus_delta="0 → 0 (updated)",
	)


# ── 119. find_fields_containing_point ───────────────────────────────────────
def find_fields_containing_point(args: dict) -> ToolResult:
	"""Which blocks is this GPS fix inside? Bounding box first, then exactly."""
	_require(FIELD)
	geo.require()
	company = _entity(args)
	latitude = as_float(args.get("lat"), "lat")
	longitude = as_float(args.get("lon"), "lon")
	if "lat" not in args or "lon" not in args:
		raise ToolError("lat and lon are both required, in decimal degrees.")
	if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
		raise ToolError(
			f"[{longitude}, {latitude}] is not a point on Earth. lat is -90 to 90 and lon is "
			"-180 to 180 — a latitude past 90 usually means the pair is the wrong way round."
		)

	filters = {"boundary_geojson": ("is", "set")}
	if company:
		filters["owning_entity"] = company
	rows = frappe.db.get_all(
		FIELD,
		filters=filters,
		fields=compat.existing_fields(FIELD, _FIELD_FIELDS),
		limit=REGISTER_CAP,
	)

	# The bounding box is the prefilter, NOT the H3 cells. A bbox is a guaranteed
	# superset of the shape it bounds, so a candidate set built from it cannot
	# miss the right answer; an H3 fill is a set of cells that TOUCH the shape, so
	# a cell can be in the set while the point inside it is outside the polygon —
	# fine for narrowing, and it is the exact test below that settles it either
	# way. What matters is that nothing is dropped before that test.
	candidates, matches = 0, []
	for row in rows or []:
		bounds = geo.bbox_bounds(row.get("boundary_bbox_geojson"))
		if bounds and not (bounds[0] <= longitude <= bounds[2] and bounds[1] <= latitude <= bounds[3]):
			continue
		candidates += 1
		shape = geo.stored_shape(row.get("boundary_geojson"))
		if shape is not None and geo.covers_point(shape, latitude, longitude):
			matches.append(dict(row))

	counties = _parcel_counties([row.get("parcel") for row in matches])
	varieties = _field_varieties([row.get("name") for row in matches])
	described = [_describe_field(row, None, counties, varieties) for row in matches]
	unmapped = frappe.db.count(
		FIELD, {**({"owning_entity": company} if company else {}), "boundary_geojson": ("is", "not set")}
	)
	return ToolResult(
		data={
			"point": {"lat": latitude, "lon": longitude},
			"h3_cells": {
				str(resolution): geo.cell_for_point(latitude, longitude, resolution)
				for resolution in geo.H3_RESOLUTIONS
			},
			"match_count": len(described),
			"fields": described,
			"searched": len(rows or []),
			"candidates_after_bbox": candidates,
			"fields_without_a_boundary": unmapped,
			"note": (
				"Blocks with no boundary cannot be tested and are not in `searched`. "
				f"{unmapped} on this site have none, so an empty result means 'not inside any "
				"MAPPED block' rather than 'not on the farm'."
			)
			if unmapped
			else None,
			"boundary_inclusive": True,
		},
		summary=(
			f"[{latitude}, {longitude}] is inside {len(described)} block(s)"
			+ (f": {', '.join(row['name'] for row in described)}" if described else "")
		),
	)


# ── 120. find_fields_by_h3_cell ─────────────────────────────────────────────
def find_fields_by_h3_cell(args: dict) -> ToolResult:
	"""Which blocks does this H3 cell touch? A spatial index lookup, not a test."""
	_require(FIELD)
	geo.require()
	company = _entity(args)
	cell = as_str(args, "cell", required=True)
	resolution = geo.cell_resolution(cell)

	stored = sorted(geo.H3_RESOLUTIONS)
	if resolution in stored:
		probe, probe_resolution = cell, resolution
	elif resolution > stored[-1]:
		probe, probe_resolution = geo.cell_parent(cell, stored[-1]), stored[-1]
	else:
		probe, probe_resolution = cell, None  # coarser than anything stored; roll up below

	filters = {"h3_cells": ("is", "set")}
	if company:
		filters["owning_entity"] = company
	rows = frappe.db.get_all(
		FIELD,
		filters=filters,
		fields=compat.existing_fields(FIELD, _FIELD_FIELDS),
		limit=REGISTER_CAP,
	)

	matches = []
	for row in rows or []:
		cells = geo.stored_cells(row.get("h3_cells"))
		if probe_resolution is not None:
			if probe in cells.get(str(probe_resolution), set()):
				matches.append(dict(row))
			continue
		# The query is coarser than the coarsest resolution stored, so roll each
		# field's coarsest cells UP to the query's resolution and compare there.
		coarsest = cells.get(str(stored[0]), set())
		if any(geo.cell_parent(entry, resolution) == cell for entry in coarsest):
			matches.append(dict(row))

	counties = _parcel_counties([row.get("parcel") for row in matches])
	varieties = _field_varieties([row.get("name") for row in matches])
	described = [_describe_field(row, None, counties, varieties) for row in matches]
	return ToolResult(
		data={
			"cell": cell,
			"cell_resolution": resolution,
			"matched_at_resolution": probe_resolution if probe_resolution is not None else resolution,
			"probe_cell": probe,
			"stored_resolutions": stored,
			"match_count": len(described),
			"fields": described,
			"searched": len(rows or []),
			"note": (
				"A cell matching a block means the cell TOUCHES it, not that everything in the "
				"cell is inside it. Use find_fields_containing_point when the question is about "
				"a specific position."
			),
		},
		summary=f"H3 cell {cell} (resolution {resolution}) touches {len(described)} block(s)",
	)


# ── 121. import_field_boundary_geojson ──────────────────────────────────────
def import_field_boundary_geojson(args: dict) -> ToolResult:
	"""Set boundaries on existing blocks from a GeoJSON FeatureCollection.

	Per-feature rather than whole-batch, which is the opposite of
	`import_farm_app_fields` and deliberately so: that tool CREATES records, so a
	half-run leaves a farm somebody has to reconcile. This one only sets a field
	on records that already exist, so one bad feature in forty is a bad feature —
	skipping it and naming it is more useful than refusing the other thirty-nine.
	"""
	_require(FIELD)
	geo.require()
	company = _entity(args)

	payload = args.get("feature_collection")
	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except json.JSONDecodeError as error:
			raise ToolError(f"feature_collection is not valid JSON: {error}. Nothing was changed.") from None
	if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
		raise ToolError(
			"feature_collection must be a GeoJSON FeatureCollection. Each Feature's `properties` "
			"needs `field_name`, and `parcel_hint` unless a default `parcel` is given. Nothing "
			"was changed."
		)
	features = payload.get("features")
	if not isinstance(features, list) or not features:
		raise ToolError("the FeatureCollection has no features. Nothing was changed.")
	if len(features) > IMPORT_CAP:
		raise ToolError(
			f"{len(features)} features is more than this takes in one call ({IMPORT_CAP}). "
			"Nothing was changed."
		)

	default_parcel = as_str(args, "parcel")
	default_parcel = _resolve_parcel(args) if default_parcel else ""
	apply = as_bool(args, "apply", False)

	results, seen = [], set()
	for index, feature in enumerate(features, start=1):
		results.append(_plan_boundary(feature, index, default_parcel, company or "", seen))

	ready = [entry for entry in results if entry["action"] == "set"]
	skipped = [entry for entry in results if entry["action"] != "set"]

	data = {
		"feature_count": len(results),
		"would_set": len(ready),
		"skipped": len(skipped),
		"applied": False,
		"dry_run": not apply,
		"results": results,
		"note": (
			"Boundaries are set on blocks that already exist; this never creates a Field. "
			"Register the blocks first with create_field or import_farm_app_fields. Each feature "
			"stands or falls on its own — a malformed one is reported and the rest still apply."
		),
	}
	if not apply:
		return ToolResult(
			data=data,
			summary=f"dry run: {len(ready)} of {len(results)} feature(s) would set a boundary",
		)

	applied, failed = [], []
	for entry in ready:
		try:
			doc = frappe.get_doc(FIELD, entry["field"])
			for fieldname, value in entry.pop("_derived").items():
				doc.set(fieldname, value)
			doc.save(ignore_permissions=True)
			entry["applied"] = True
			applied.append(entry["field"])
		except Exception as error:
			entry["action"] = "error"
			entry["applied"] = False
			entry["reason"] = f"refused on save: {error}"
			failed.append(entry["field"])
	for entry in results:
		entry.pop("_derived", None)

	data.update(
		{"applied": True, "dry_run": False, "set": applied, "failed": failed, "would_set": len(ready)}
	)
	return ToolResult(
		data=data,
		summary=(
			f"set {len(applied)} boundary(ies) of {len(results)} feature(s)"
			+ (f", {len(failed)} refused" if failed else "")
		),
		docstatus_delta="0 → 0 (updated)" if applied else "",
	)


def _plan_boundary(feature, index: int, default_parcel: str, company: str, seen: set) -> dict:
	"""Validate one Feature and say what would happen to it. Never raises."""
	entry = {"index": index, "field_name": None, "field": None, "action": "error", "reason": ""}
	if not isinstance(feature, dict) or feature.get("type") != "Feature":
		entry["reason"] = "not a GeoJSON Feature."
		return entry

	properties = feature.get("properties")
	if not isinstance(properties, dict):
		entry["reason"] = "the Feature has no `properties` object."
		return entry
	field_name = str(properties.get("field_name") or "").strip()
	entry["field_name"] = field_name or None
	if not field_name:
		entry["reason"] = "`properties.field_name` is missing, so there is nothing to match on."
		return entry

	hint = str(properties.get("parcel_hint") or "").strip()
	parcel = default_parcel
	if hint:
		try:
			parcel = str(parcel_row(hint, company)["name"])
		except ToolError as error:
			entry["reason"] = f"parcel_hint {hint!r}: {error}"
			return entry
	if not parcel:
		entry["reason"] = "no `properties.parcel_hint` and no default `parcel` was given."
		return entry
	entry["parcel"] = parcel

	try:
		row = field_row(field_name, parcel, company)
	except ToolError as error:
		entry["action"] = "skip"
		entry["reason"] = f"{error} This tool sets boundaries; it never creates a Field."
		return entry
	entry["field"] = row["name"]

	if row["name"] in seen:
		entry["reason"] = f"{row['name']} appears twice in this collection."
		return entry
	seen.add(row["name"])

	try:
		geometry = geo.parse(feature.get("geometry"), f"feature {index} geometry")
		derived = geo.derive(geometry, f"feature {index} geometry")
	except ToolError as error:
		entry["reason"] = str(error)
		return entry
	derived.pop("shape", None)

	ratio, verdict = geo.area_disagreement(row.get("acreage"), derived["area_computed_acres"])
	entry["area_computed_acres"] = derived["area_computed_acres"]
	entry["acreage_recorded"] = round(float(row.get("acreage") or 0), 2) or None
	entry["area_disagreement_ratio"] = ratio or None
	if verdict == "refuse":
		entry["reason"] = (
			f"the polygon encloses {derived['area_computed_acres']} acres and the block is "
			f"recorded as {entry['acreage_recorded']} — {round(ratio * 100, 1)}%. One of the two "
			"is about a different piece of ground."
		)
		return entry

	entry["action"] = "set"
	entry["reason"] = ""
	entry["replaces_existing"] = bool(str(row.get("boundary_geojson") or "").strip())
	if verdict == "warn":
		entry["warning"] = (
			f"area differs from the recorded acreage by {round(ratio * 100, 1)}%; both figures are kept."
		)
	entry["_derived"] = derived
	return entry
