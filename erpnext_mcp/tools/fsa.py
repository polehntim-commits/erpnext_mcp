# SPDX-License-Identifier: MIT
"""The county FSA office's copy of the farm's own field boundaries.

`fsa.py` turns what the office hands out — a zipped shapefile, a KML, a GeoJSON
— into features in degrees. These two tools decide what that means for a Field.

────────────────────────────────────────────────────────────────────────────
WHY THE GENERIC IMPORTER COULD NOT DO THIS
────────────────────────────────────────────────────────────────────────────

`import_field_boundary_geojson` has existed since v0.31.0 and takes a
FeatureCollection whose every Feature carries `properties.field_name` and
`properties.parcel_hint`. A CLU carries NEITHER. It carries a farm number, a
tract number, a field number within that tract, a GUID and FSA's own calculated
acreage, and the grower calls the block "the top of Dry Hollow" — so a file that
is entirely correct matches nothing at all through that door, and the report
comes back saying every field is unregistered.

So the matching here is on WHAT A CLU ACTUALLY HAS, in the order of how much the
identifier is worth:

  1. THE CLU GUID, against `fsa_clu_identifier` on a block imported before. This
     is the only identifier FSA guarantees, and it is what makes a re-import an
     UPDATE rather than a second copy of the farm.
  2. TRACT AND FIELD NUMBER, against `fsa_tract_number` + `fsa_clu_number`. What
     the office says out loud, what the acreage report is filed under, and
     stable across the years a GUID sometimes is not.
  3. THE FIELD NUMBER AS THE BLOCK NUMBER, within one parcel. A farm that
     numbered its blocks the way FSA numbers its fields — which is most farms
     that ever filed an acreage report — matches here on the first import,
     before anything has an FSA column at all.
  4. THE BLOCK'S NAME, for a farm that named its blocks `T1234-3` already.

Anything unmatched is REPORTED AND SKIPPED unless `create_missing` is set, for
the same reason `import_field_boundary_geojson` never creates: a boundary landing
on the wrong block is a spray record pointing at the wrong ground, and a farm
that has to reconcile forty invented blocks will not notice the two that were
right to invent.

────────────────────────────────────────────────────────────────────────────
DRY RUN IS THE DEFAULT AND THE PLAN IS THE POINT
────────────────────────────────────────────────────────────────────────────

The interesting output of this tool is not the write. It is the line that says
"tract 1234 field 3 matched Dry Hollow Block 2 by tract/field, 12.4 acres in the
file against 12.1 recorded" — forty of those is a farm reading its own boundaries
back before anything is changed. `apply=true` then does exactly what the plan
said, per feature, and a bad feature in forty is a bad feature rather than a
refused import.

EVERY POLYGON STILL GOES THROUGH `set_field_boundary`'S CHECKS. Self-intersection
is refused, the enclosed area is compared against the block's recorded acreage
and a disagreement past a quarter is refused outright, and every derived column —
centroid, bounding box, H3 coverage, computed acres — is recomputed from the
polygon rather than taken from FSA's `CALCACRES`. FSA's acreage is KEPT, in
`fsa_calc_acres`, because it is the number the payment is made on and it is
worth having both; it is never what the app computes with.
"""

import json

import frappe

from .. import compat, fsa, geo
from ..args import as_bool, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from .farm import FIELD, IMPORT_CAP, PARCEL
from .realestate import parcel_row

#: The columns v0.139.0 adds to Field. Written together and only together: a
#: block carrying a tract number but no CLU identifier is one somebody has to
#: work out by hand next year.
FSA_COLUMNS = (
	"fsa_farm_number",
	"fsa_tract_number",
	"fsa_clu_number",
	"fsa_clu_identifier",
	"fsa_calc_acres",
	"fsa_hel_type",
	"fsa_import_date",
)

#: How a match was made, worst to best, for the report. A caller that sees
#: `block_number` on forty features and `clu_identifier` on none is looking at a
#: first import; the same call next season should say `clu_identifier` forty
#: times, and if it does not, something renamed itself.
MATCH_ORDER = ("clu_identifier", "tract_and_field", "block_number", "field_name")


def _company(args: dict) -> str:
	return resolve_company(as_str(args, "owning_entity") or as_str(args, "company")) or ""


def _payload(args: dict) -> dict:
	"""The parsed file, whichever of the three ways the caller supplied it."""
	collection = args.get("feature_collection")
	blob = args.get("file_base64")
	if collection and blob:
		raise ToolError(
			"pass either `file_base64` (the file the office gave you) or `feature_collection` "
			"(GeoJSON you already have), not both. Nothing was changed."
		)
	if collection:
		if isinstance(collection, str):
			payload_features, crs = fsa.features_from_geojson(_json(collection))
		else:
			payload_features, crs = fsa.features_from_geojson(collection)
		return {
			"format": "geojson",
			"crs": {"kind": crs.get("kind"), "name": crs.get("name") or "", "datum": "", "unit": "degree"},
			"source_files": [],
			"warnings": list(crs.get("warnings") or []),
			"features": payload_features,
		}
	if not blob:
		raise ToolError(
			"nothing to read. Pass `file_base64` — the .zip the FSA office gave you, or its "
			".kmz, .kml or .geojson — or `feature_collection` if you have converted it already. "
			"Nothing was changed."
		)
	return fsa.read(fsa.decode_upload(blob), as_str(args, "filename"))


def _json(text: str):
	try:
		return json.loads(text)
	except json.JSONDecodeError as error:
		raise ToolError(f"feature_collection is not valid JSON: {error}. Nothing was changed.") from None


def _positions(geometry):
	"""Every `[longitude, latitude]` in a Polygon or a MultiPolygon, flat."""
	if not isinstance(geometry, dict):
		return
	coordinates = geometry.get("coordinates")
	if not isinstance(coordinates, list):
		return
	rings = (
		coordinates
		if geometry.get("type") == "Polygon"
		else [ring for part in coordinates if isinstance(part, list) for ring in part]
	)
	for ring in rings:
		if not isinstance(ring, list):
			continue
		for position in ring:
			if isinstance(position, (list, tuple)) and len(position) >= 2:
				yield position


def _extent(geometry: dict) -> list:
	"""`[west, south, east, north]` of a Polygon or MultiPolygon."""
	longitudes, latitudes = [], []
	for position in _positions(geometry):
		longitudes.append(float(position[0]))
		latitudes.append(float(position[1]))
	if not longitudes:
		return []
	return [
		round(min(longitudes), 6),
		round(min(latitudes), 6),
		round(max(longitudes), 6),
		round(max(latitudes), 6),
	]


def _describe_clu(index: int, feature: dict, include_geometry: bool) -> dict:
	"""One CLU as this app sees it: FSA's own columns, and what the shape says."""
	attributes = fsa.canonical_attributes(feature.get("properties") or {})
	entry = {
		"index": index,
		"clu": fsa.clu_key(attributes),
		"suggested_field_name": fsa.suggested_field_name(attributes),
		**{key: value for key, value in attributes.items() if key not in ("field_name", "parcel_hint")},
		"columns": dict(feature.get("properties") or {}),
	}
	geometry = feature.get("geometry")
	if not geometry:
		entry["geometry_error"] = "this record has no shape at all — the .shp row is a null shape."
		return entry
	try:
		parsed = geo.parse(geometry, f"CLU {entry['clu']}")
	except ToolError as error:
		entry["geometry_error"] = str(error)
		return entry
	entry["geometry_type"] = parsed["type"]
	entry["computed_acres"] = geo.area_acres(parsed)
	entry["bbox"] = _extent(parsed)
	if entry["bbox"]:
		entry["centre"] = {
			"lat": round((entry["bbox"][1] + entry["bbox"][3]) / 2, 6),
			"lon": round((entry["bbox"][0] + entry["bbox"][2]) / 2, 6),
		}
	if attributes.get("calc_acres") and entry.get("computed_acres"):
		ratio, verdict = geo.area_disagreement(attributes["calc_acres"], entry["computed_acres"])
		entry["acres_disagreement_ratio"] = ratio or None
		if verdict != "ok":
			entry["acres_note"] = (
				f"FSA's CALCACRES says {attributes['calc_acres']} and the polygon encloses "
				f"{entry['computed_acres']} — {round(ratio * 100, 1)}% apart."
			)
	if include_geometry:
		entry["geometry"] = parsed
	return entry


# ── 1. read_fsa_clu_file ────────────────────────────────────────────────────
def read_fsa_clu_file(args: dict) -> ToolResult:
	"""Say what is in the file the FSA office gave you. Reads nothing else and writes nothing."""
	payload = _payload(args)
	include_geometry = as_bool(args, "include_geometry", False)
	as_feature_collection = as_bool(args, "as_feature_collection", False)
	features = payload.get("features") or []
	if not features:
		raise ToolError(
			"that file parsed, and there is nothing in it — no shapes and no rows. Ask the "
			"office for the CLU layer rather than the tract or farm layer. Nothing was changed."
		)

	clus = [_describe_clu(index, feature, include_geometry) for index, feature in enumerate(features, 1)]
	readable = [entry for entry in clus if "geometry_error" not in entry]

	tracts = {}
	for entry in clus:
		key = entry.get("tract_number") or "(no tract number)"
		bucket = tracts.setdefault(
			key, {"tract_number": entry.get("tract_number"), "fields": 0, "calc_acres": 0.0}
		)
		bucket["fields"] += 1
		if entry.get("calc_acres"):
			bucket["calc_acres"] = round(bucket["calc_acres"] + entry["calc_acres"], 2)

	extents = [entry["bbox"] for entry in readable if entry.get("bbox")]
	bbox = (
		[
			min(box[0] for box in extents),
			min(box[1] for box in extents),
			max(box[2] for box in extents),
			max(box[3] for box in extents),
		]
		if extents
		else []
	)

	data = {
		"format": payload.get("format"),
		"crs": payload.get("crs"),
		"source_files": payload.get("source_files") or [],
		"warnings": payload.get("warnings") or [],
		"clu_count": len(clus),
		"readable": len(readable),
		"unreadable": len(clus) - len(readable),
		"identified_by_guid": len([entry for entry in clus if entry.get("clu_identifier")]),
		"farm_numbers": sorted({entry["farm_number"] for entry in clus if entry.get("farm_number")}),
		"tracts": sorted(tracts.values(), key=lambda row: str(row["tract_number"] or "")),
		"total_calc_acres": round(sum(entry.get("calc_acres") or 0 for entry in clus), 2),
		"total_computed_acres": round(sum(entry.get("computed_acres") or 0 for entry in readable), 2),
		"bbox": bbox,
		"clus": clus,
		"note": (
			"Nothing has been matched against this site and nothing has been written. "
			"import_fsa_clu_boundaries takes the same file and reports, per CLU, which block it "
			"would land on — that is the call to make next, without `apply`."
		),
	}
	if as_feature_collection:
		# THE TRANSLATION, HANDED OVER RATHER THAN KEPT. Every Feature gains the
		# `field_name` and `parcel_hint` that `import_field_boundary_geojson` has
		# matched on since v0.31.0, and keeps its FSA columns under `fsa_` keys —
		# so a caller who would rather use the generic importer, or hand the
		# collection to something else entirely, is not made to reimplement the
		# conversion this module already did.
		data["feature_collection"] = fsa.to_feature_collection(
			payload, parcel=as_str(args, "parcel"), tract_parcels=args.get("tract_parcels")
		)
		data["feature_collection_note"] = (
			"Ready for import_field_boundary_geojson: each Feature carries `field_name` "
			"(T<tract>-<field>) and `parcel_hint`. That tool matches on the name alone and "
			"knows nothing about CLU identifiers, so it will not recognise a block on a "
			"re-import — import_fsa_clu_boundaries is the one that does."
		)
	summary = (
		f"{len(clus)} CLU(s) across {len(tracts)} tract(s), "
		f"{data['total_calc_acres']} FSA acres, read from {payload.get('format')}"
	)
	return ToolResult(data=data, summary=summary)


# ── 2. import_fsa_clu_boundaries ────────────────────────────────────────────
def import_fsa_clu_boundaries(args: dict) -> ToolResult:
	"""Match a CLU file against the blocks on this site and set their boundaries."""
	compat.require_doctype(
		FIELD, "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."
	)
	compat.require_doctype(
		PARCEL, "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."
	)
	geo.require()
	if not compat.has_field(FIELD, "fsa_clu_identifier"):
		raise ToolError(
			"this site's Field DocType has no FSA columns yet, so an import would set boundaries "
			"with nothing recording which CLU they came from — and next year's file would create "
			"a second copy of the farm. Run `bench --site <site> migrate` and call this again. "
			"Nothing was changed."
		)

	company = _company(args)
	payload = _payload(args)
	features = payload.get("features") or []
	if not features:
		raise ToolError("there is nothing in that file — no shapes and no rows. Nothing was changed.")
	if len(features) > IMPORT_CAP:
		raise ToolError(
			f"{len(features)} features is more than this takes in one call ({IMPORT_CAP}). "
			"Nothing was changed."
		)

	default_parcel = as_str(args, "parcel")
	default_parcel = str(parcel_row(default_parcel, company)["name"]) if default_parcel else ""
	tract_parcels = _tract_parcels(args, company)
	create_missing = as_bool(args, "create_missing", False)
	apply = as_bool(args, "apply", False)

	claimed = {}
	results = []
	for index, feature in enumerate(features, start=1):
		results.append(_plan(feature, index, default_parcel, tract_parcels, company, create_missing, claimed))

	ready = [entry for entry in results if entry["action"] in ("set", "create")]
	data = {
		"format": payload.get("format"),
		"crs": payload.get("crs"),
		"source_files": payload.get("source_files") or [],
		"warnings": payload.get("warnings") or [],
		"feature_count": len(results),
		"would_set": len([entry for entry in ready if entry["action"] == "set"]),
		"would_create": len([entry for entry in ready if entry["action"] == "create"]),
		"skipped": len([entry for entry in results if entry["action"] == "skip"]),
		"errors": len([entry for entry in results if entry["action"] == "error"]),
		"matched_by": {
			how: len([entry for entry in results if entry.get("matched_by") == how]) for how in MATCH_ORDER
		},
		"applied": False,
		"dry_run": not apply,
		"results": results,
		"note": (
			"Each CLU stands or falls on its own: a shape this refuses is named and the rest "
			"still apply. FSA's CALCACRES is kept in fsa_calc_acres and is never what the app "
			"computes with — every derived figure comes from the polygon."
		),
	}
	if not apply:
		for entry in results:
			entry.pop("_derived", None)
			entry.pop("_attributes", None)
		return ToolResult(
			data=data,
			summary=(
				f"dry run: {data['would_set']} boundary(ies) would be set and "
				f"{data['would_create']} block(s) created, of {len(results)} CLU(s)"
			),
		)

	applied, created, failed = [], [], []
	for entry in ready:
		try:
			if entry["action"] == "create":
				name = _create_field(entry)
				created.append(name)
			else:
				name = _update_field(entry)
				applied.append(name)
			entry["field"] = name
			entry["applied"] = True
		except Exception as error:  # reported per feature, never fatal to the batch
			entry["action"] = "error"
			entry["applied"] = False
			entry["reason"] = f"refused on save: {error}"
			failed.append(entry.get("field") or entry["clu"])
	for entry in results:
		entry.pop("_derived", None)
		entry.pop("_attributes", None)

	data.update(
		{
			"applied": True,
			"dry_run": False,
			"set": applied,
			"created": created,
			"failed": failed,
			"errors": len([entry for entry in results if entry["action"] == "error"]),
		}
	)
	return ToolResult(
		data=data,
		summary=(
			f"set {len(applied)} boundary(ies) and created {len(created)} block(s) from "
			f"{len(results)} CLU(s)" + (f", {len(failed)} refused" if failed else "")
		),
		docstatus_delta="0 → 0 (updated)" if applied or created else "",
	)


def _tract_parcels(args: dict, company: str) -> dict:
	"""`{tract number: Parcel}`, resolved once so a bad name fails before any write.

	A FARM'S CLU FILE SPANS SEVERAL PARCELS and that is the normal case, not the
	exception — FSA's tract is a unit of ownership history and a Parcel here is a
	tax lot, and one farm number covers several of both. Without this every
	import would have to be run one parcel at a time with a hand-filtered file.
	"""
	raw = args.get("tract_parcels")
	if raw in (None, ""):
		return {}
	if isinstance(raw, str):
		raw = _json(raw)
	if not isinstance(raw, dict):
		raise ToolError(
			"tract_parcels must be an object mapping tract numbers to parcels, e.g. "
			'{"1234": "Yellow Camp", "1235": "Dry Hollow"}. Nothing was changed.'
		)
	resolved = {}
	for tract, parcel in raw.items():
		key = fsa._identifier(tract)
		if not key:
			continue
		resolved[key] = str(parcel_row(str(parcel), company)["name"])
	return resolved


#: What a match needs to read off a candidate block. `boundary_geojson` is in
#: the list so the plan can say whether this REPLACES a shape somebody already
#: drew, which is the one line of a dry run a person reads twice.
_MATCH_FIELDS = (
	"name",
	"field_name",
	"parcel",
	"owning_entity",
	"acreage",
	"block_number",
	"boundary_geojson",
	"fsa_clu_identifier",
	"fsa_tract_number",
	"fsa_clu_number",
)


def _candidates(filters: dict) -> list:
	return frappe.db.get_all(
		FIELD, filters=filters, fields=compat.existing_fields(FIELD, _MATCH_FIELDS), limit=5
	)


def _one(filters: dict, how: str):
	"""The single block matching, or a refusal naming the several that do."""
	rows = _candidates(filters)
	if not rows:
		return None
	if len(rows) > 1:
		names = ", ".join(sorted(str(row.get("name")) for row in rows))
		raise ToolError(
			f"{len(rows)} blocks match this CLU by {how}: {names}. Two blocks answering to one "
			"CLU is something to fix on the site before importing — this will not guess which."
		)
	return dict(rows[0])


def _match(attributes: dict, parcel: str, company: str) -> tuple:
	"""The block this CLU belongs to, and how it was recognised.

	The order is the order of how much the identifier is worth, and the first
	hit wins. Every step is scoped to the company when there is one, because two
	entities on one bench are two businesses and FSA numbers restart for each.
	"""
	scope = {"owning_entity": company} if company else {}

	identifier = attributes.get("clu_identifier")
	if identifier and compat.has_field(FIELD, "fsa_clu_identifier"):
		row = _one({**scope, "fsa_clu_identifier": identifier}, "CLU identifier")
		if row:
			return row, "clu_identifier"

	tract = attributes.get("tract_number")
	number = attributes.get("clu_number")
	if tract and number and compat.has_field(FIELD, "fsa_tract_number"):
		row = _one({**scope, "fsa_tract_number": tract, "fsa_clu_number": number}, "tract and field number")
		if row:
			return row, "tract_and_field"

	if number and parcel:
		# COMPARED AFTER NORMALISING, not in the query. A block numbered "03" on
		# this site and field 3 in the file are the same block, and a farm that
		# zero-padded its block numbers once would otherwise match nothing and be
		# told to create forty duplicates of what it already has.
		rows = [
			row
			for row in _candidates({"parcel": parcel, "block_number": ("is", "set")})
			if fsa._identifier(row.get("block_number")) == number
		]
		if len(rows) > 1:
			names = ", ".join(sorted(str(row.get("name")) for row in rows))
			raise ToolError(
				f"{len(rows)} blocks on {parcel} carry block number {number}: {names}. This will "
				"not guess which one the CLU is."
			)
		if rows:
			return dict(rows[0]), "block_number"

	name = fsa.suggested_field_name(attributes)
	if name and parcel:
		row = _one({"parcel": parcel, "field_name": name}, "block name")
		if row:
			return row, "field_name"

	return None, None


def _plan(
	feature: dict,
	index: int,
	default_parcel: str,
	tract_parcels: dict,
	company: str,
	create_missing: bool,
	claimed: dict,
) -> dict:
	"""What would happen to one CLU. Never raises — a bad feature is a bad feature."""
	attributes = fsa.canonical_attributes(feature.get("properties") or {})
	entry = {
		"index": index,
		"clu": fsa.clu_key(attributes),
		"farm_number": attributes.get("farm_number"),
		"tract_number": attributes.get("tract_number"),
		"clu_number": attributes.get("clu_number"),
		"clu_identifier": attributes.get("clu_identifier"),
		"calc_acres": attributes.get("calc_acres"),
		"field": None,
		"matched_by": None,
		"action": "error",
		"reason": "",
	}

	parcel = default_parcel
	hint = attributes.get("parcel_hint")
	if hint:
		try:
			parcel = str(parcel_row(str(hint), company)["name"])
		except ToolError as error:
			entry["reason"] = f"parcel_hint {hint!r}: {error}"
			return entry
	elif attributes.get("tract_number") in tract_parcels:
		parcel = tract_parcels[attributes["tract_number"]]
	entry["parcel"] = parcel or None

	geometry = feature.get("geometry")
	if not geometry:
		entry["reason"] = "this CLU has no shape — the row is a null shape in the .shp."
		return entry
	try:
		parsed = geo.parse(geometry, f"CLU {entry['clu']}")
		derived = geo.derive(parsed, f"CLU {entry['clu']}")
	except ToolError as error:
		entry["reason"] = str(error)
		return entry
	derived.pop("shape", None)
	entry["computed_acres"] = derived["area_computed_acres"]
	if attributes.get("calc_acres") is not None:
		ratio, verdict = geo.area_disagreement(attributes["calc_acres"], entry["computed_acres"])
		if verdict != "ok":
			entry["fsa_acres_disagreement_ratio"] = ratio

	try:
		row, how = _match(attributes, parcel, company)
	except ToolError as error:
		entry["reason"] = str(error)
		return entry

	if row:
		entry["field"] = row["name"]
		entry["matched_by"] = how
		if row["name"] in claimed:
			entry["reason"] = (
				f"{row['name']} was already matched by {claimed[row['name']]} earlier in this "
				"file. Two CLUs cannot both be one block."
			)
			entry["field"] = None
			return entry
		claimed[row["name"]] = entry["clu"]
		entry["acreage_recorded"] = round(float(row.get("acreage") or 0), 2) or None
		ratio, verdict = geo.area_disagreement(row.get("acreage"), entry["computed_acres"])
		entry["area_disagreement_ratio"] = ratio or None
		if verdict == "refuse":
			entry["reason"] = (
				f"the CLU encloses {entry['computed_acres']} acres and {row['name']} is recorded "
				f"as {entry['acreage_recorded']} — {round(ratio * 100, 1)}%. One of the two is "
				"about a different piece of ground."
			)
			return entry
		if verdict == "warn":
			entry["warning"] = (
				f"the CLU encloses {entry['computed_acres']} acres against a recorded "
				f"{entry['acreage_recorded']}; both figures are kept."
			)
		entry["replaces_existing"] = bool(str(row.get("boundary_geojson") or "").strip())
		entry["action"] = "set"
	else:
		if not create_missing:
			entry["action"] = "skip"
			entry["reason"] = (
				"no block on this site matches this CLU. Set create_missing to register it as "
				f"{fsa.suggested_field_name(attributes)!r}, or put its tract and field number on "
				"the block it belongs to and run this again."
			)
			return entry
		if not parcel:
			entry["action"] = "skip"
			entry["reason"] = (
				"nothing matched and there is no parcel to create it under. Pass `parcel`, or "
				"`tract_parcels` mapping this CLU's tract to one."
			)
			return entry
		field_name = fsa.suggested_field_name(attributes)
		if not field_name:
			entry["action"] = "skip"
			entry["reason"] = (
				"nothing matched and this CLU has no tract or field number to name a block "
				"after. Register the block first with create_field."
			)
			return entry
		entry["action"] = "create"
		entry["field_name"] = field_name
		# FSA's OWN ACREAGE IS WHAT A NEW BLOCK IS RECORDED AS, not the polygon's.
		# The two are within a percent of each other and one of them is the number
		# the payment is made on; a farm's acreage column agreeing with its acreage
		# report is worth more than it agreeing with this app's own arithmetic.
		entry["acreage"] = (
			attributes["calc_acres"] if attributes.get("calc_acres") is not None else entry["computed_acres"]
		)

	entry["reason"] = ""
	entry["_derived"] = derived
	entry["_attributes"] = attributes
	return entry


def _stamp(doc, attributes: dict) -> None:
	"""Write FSA's identifiers onto the block, so next year's file recognises it.

	A VALUE THE FILE DOES NOT CARRY IS LEFT ALONE rather than blanked. An export
	that dropped the HEL column is an export with one fewer column in it, not a
	farm whose ground stopped being highly erodible — and a re-import that
	cleared what the last one wrote would lose the CLU identifier the whole
	matching order rests on.
	"""
	values = {
		"fsa_farm_number": attributes.get("farm_number"),
		"fsa_tract_number": attributes.get("tract_number"),
		"fsa_clu_number": attributes.get("clu_number"),
		"fsa_clu_identifier": attributes.get("clu_identifier"),
		"fsa_calc_acres": attributes.get("calc_acres"),
		"fsa_hel_type": attributes.get("hel_type"),
		"fsa_import_date": frappe.utils.today(),
	}
	for fieldname, value in values.items():
		if value is None or not compat.has_field(FIELD, fieldname):
			continue
		doc.set(fieldname, value)


def _update_field(entry: dict) -> str:
	"""Set the boundary on a block that already exists, through its own controller."""
	doc = frappe.get_doc(FIELD, entry["field"])
	for fieldname, value in entry["_derived"].items():
		doc.set(fieldname, value)
	_stamp(doc, entry["_attributes"])
	doc.save(ignore_permissions=True)
	return doc.name


def _create_field(entry: dict) -> str:
	"""Register a block this farm has never had a record of, from its CLU."""
	doc = frappe.new_doc(FIELD)
	doc.field_name = entry["field_name"]
	doc.parcel = entry["parcel"]
	doc.acreage = entry["acreage"]
	number = entry["_attributes"].get("clu_number")
	if number:
		doc.block_number = number
	for fieldname, value in entry["_derived"].items():
		doc.set(fieldname, value)
	_stamp(doc, entry["_attributes"])
	doc.insert(ignore_permissions=True)
	return doc.name
