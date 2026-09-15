# SPDX-License-Identifier: MIT
"""County tax lots and lot line adjustments: a reference layer, an agreement, and one button.

v0.169.0. Written for a real deal — a lot line adjustment between Highland LLC
and PFI in Wasco County, Oregon — and shaped by one rule:

  COUNTY DATA IS A READ-ONLY REFERENCE LAYER; THE ADJUSTMENT AND ITS AGREEMENTS
  ARE FULLY EDITABLE; NOTHING FLOWS INTO THE OPERATING BOOKS UNTIL A SURVEY IS
  RECORDED AND SOMEBODY PRESSES A BUTTON.

────────────────────────────────────────────────────────────────────────────
THE REFERENCE LAYER: `County Tax Lot`
────────────────────────────────────────────────────────────────────────────

A tax lot is the county's statement about a piece of ground. It is cached here
so an adjustment can name the lots it changes, and it links out to nothing — not
an Item, an Asset, a Location or a Parcel. The controller refuses every write
that does not carry `flags.county_refresh`, which only `upsert_tax_lot` sets: the
Desk can read a lot and cannot change one.

THE COUNTY CLIENT IS `api/gis.py`'s, NOT A SECOND ONE. That module already knows
Wasco's FeatureServer, the tax lot grammar the layer actually stores
(`canonical_tax_lot`), the bounded fetch, and the ArcGIS error that arrives as an
HTTP 200. This module adds the two query shapes it lacked — an envelope, and the
attributes `parse_features` does not keep — and nothing else.

THE COUNTY SERVER WAS ANSWERING 503 WHEN THIS WAS WRITTEN, so a lot can be SEEDED
BY HAND from a tax statement (`source` = Manual) and refreshed from the county
later. A failed refresh leaves the cached row exactly as it was.

────────────────────────────────────────────────────────────────────────────
THE AGREEMENT: `Lot Line Adjustment`
────────────────────────────────────────────────────────────────────────────

Draft and Under review are ordinary editable drafts. Moving the status to
Submitted to county SUBMITS the document, which fixes the lots, the parties, the
pieces and the easements. Approved and Recorded follow the county in that order.
Withdrawn is allowed until the county records it, and cancels a submitted
document. Recorded is final.

WHAT STAYS EDITABLE AFTER SUBMISSION is read off the DocType JSON's
`allow_on_submit` flags (`after_submit_fields`) rather than listed twice: the
status, the recording details, each piece's surveyed acreage and final
geometry, and the open items. Frappe enforces the same flags on a bench; the
standalone double does not, so the tools enforce them too.

────────────────────────────────────────────────────────────────────────────
THE BUTTON: Record Survey
────────────────────────────────────────────────────────────────────────────

Offered only on a submitted adjustment at status Recorded, to a System Manager.
It is the one path from this module into the operating books, and it reuses the
Parcel tools rather than writing Parcel itself — `create_parcel`,
`update_parcel` and `set_parcel_boundary` keep every check they make, including
the refusal of a boundary that disagrees with the acreage by a quarter.

For each side whose party is a Company on this site:

  * acreage after = the lot's surveyed acreage if the survey states it,
    otherwise the county's GIS acreage less the surveyed pieces given up plus
    the surveyed pieces received (`acreage_basis` says which);
  * geometry after = the lot's polygon, minus the pieces given, plus the pieces
    received, where the lot has a polygon and shapely is installed;
  * the Parcel is the one already on that company carrying the lot's number, or
    a new one called `Tax Lot <number>`.

A Customer or Supplier party's ground is not on this site's books and is
reported, not written. The computation starts from the county's figures every
time, never from the Parcel's current acreage, so pressing the button twice
writes the same answer twice.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlencode

import frappe

from . import compat, geo
from .errors import ToolError

COUNTY_TAX_LOT = "County Tax Lot"
LOT_LINE_ADJUSTMENT = "Lot Line Adjustment"
PIECE = "Lot Line Adjustment Piece"
OPEN_ITEM = "Lot Line Adjustment Open Item"
EASEMENT = "Lot Line Adjustment Easement"
PARCEL = "Parcel"

#: Who may do what. Checked in the tools as well as granted as DocPerms, because
#: the MCP transport acts through one configured account and a DocPerm alone
#: does not bind there.
LAND_REFERENCE_ROLE = "Land Reference"
LAND_AGREEMENTS_ROLE = "Land Agreements"
TAX_LOT_ROLES = (LAND_REFERENCE_ROLE, LAND_AGREEMENTS_ROLE, "System Manager")
AGREEMENT_ROLES = (LAND_AGREEMENTS_ROLE, "System Manager")
RECORD_SURVEY_ROLES = ("System Manager",)

# ── statuses ────────────────────────────────────────────────────────────────
DRAFT = "Draft"
UNDER_REVIEW = "Under review"
SUBMITTED = "Submitted to county"
APPROVED = "Approved"
RECORDED = "Recorded"
WITHDRAWN = "Withdrawn"
STATUSES = (DRAFT, UNDER_REVIEW, SUBMITTED, APPROVED, RECORDED, WITHDRAWN)
DRAFT_STATUSES = (DRAFT, UNDER_REVIEW)

#: Where a submitted adjustment may go next. Backwards is refused: an approval
#: the county gave is not taken back by editing a Select. RECORDED IS FINAL — a
#: recorded adjustment is on the county's books, and undoing it is a new
#: adjustment, not a status.
SUBMITTED_NEXT = {SUBMITTED: (APPROVED, WITHDRAWN), APPROVED: (RECORDED, WITHDRAWN), RECORDED: ()}

PARTY_1 = "Party 1"
PARTY_2 = "Party 2"
SIDES = (PARTY_1, PARTY_2)
PARTY_TYPES = ("Company", "Customer", "Supplier")
CONSIDERATIONS = ("Even swap", "Cash true-up", "Netted")
EASEMENT_TYPES = ("Access", "Utility", "Irrigation")
OPEN_ITEM_STATUSES = ("Open", "In progress", "Resolved", "Dropped")

#: The attributes a county might publish a situs address under. Read the way
#: `gis._property` reads everything else: in order, case-insensitively.
SITUS_FIELDS = ("SitusAddress", "SITUSADDRESS", "Situs", "SiteAddress", "SITEADDR", "PropertyAddress")

#: How big a bounding box may be, in degrees on a side. About three miles at
#: Wasco's latitude: a neighbourhood of lots, not the county.
MAX_BBOX_DEGREES = 0.05

_DOCTYPE_DIR = os.path.join(os.path.dirname(__file__), "erpnext_mcp", "doctype")


# ── small shared helpers ────────────────────────────────────────────────────
def json_value(value):
	"""A JSON column's value as Python, whichever form the database handed back."""
	if value in (None, ""):
		return None
	if isinstance(value, (dict, list)):
		return value
	try:
		return json.loads(str(value))
	except (TypeError, ValueError):
		return None


def dump(value) -> str | None:
	return None if value is None else json.dumps(value, separators=(",", ":"), sort_keys=True)


def side(value, label: str = "party") -> str:
	"""`Party 1` or `Party 2`, from any of the spellings a caller will use."""
	text = str(value or "").strip().lower().replace("_", " ")
	if text in ("party 1", "1", "p1", "party1"):
		return PARTY_1
	if text in ("party 2", "2", "p2", "party2"):
		return PARTY_2
	raise ToolError(f"{label} must be Party 1 or Party 2, got {value!r}.")


def side_of_party(doc, value, label: str) -> str:
	"""A side, from `Party 1`/`Party 2` or from the party's own docname."""
	text = str(value or "").strip()
	if text and text == str(doc.get("party_1") or ""):
		return PARTY_1
	if text and text == str(doc.get("party_2") or ""):
		return PARTY_2
	return side(text, label)


def party_name(doc, which: str) -> str:
	key = "party_1" if which == PARTY_1 else "party_2"
	return str(doc.get(key) or which)


def require_roles(allowed: tuple, action: str, tail: str) -> str:
	"""The principal, once it holds one of `allowed`. Same shape as `governance`'s gate."""
	from . import roles, security

	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(f"this call has no identity to {action} as. {tail}")
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(allowed):
		raise ToolError(
			f"{actor} may not {action}: it holds none of {', '.join(allowed)}. Grant the role in the "
			f"Desk to the account this app acts as (`mcp_system_user` on ERPNext MCP Settings). {tail}"
		)
	return actor


# ── the DocType JSON is the one list of what stays editable ─────────────────
def _doctype_json(doctype: str) -> dict:
	folder = doctype.lower().replace(" ", "_")
	with open(os.path.join(_DOCTYPE_DIR, folder, f"{folder}.json"), encoding="utf-8") as handle:
		return json.load(handle)


def after_submit_fields(doctype: str = LOT_LINE_ADJUSTMENT) -> frozenset:
	"""Fieldnames Frappe lets change on a submitted document, from `allow_on_submit`."""
	return frozenset(
		field["fieldname"]
		for field in _doctype_json(doctype)["fields"]
		if field.get("allow_on_submit") and field.get("fieldtype") not in ("Section Break", "Column Break")
	)


# ── County Tax Lot ──────────────────────────────────────────────────────────
TAX_LOT_FIELDS = (
	"map_taxlot",
	"county",
	"account",
	"owner_of_record",
	"situs",
	"acres_gis",
	"source",
	"geometry",
	"source_url",
	"fetched_on",
	"raw_attributes",
)


def canonical_lot(value) -> str:
	"""The county's own spelling of a tax lot, from any spelling `api/gis.py` accepts."""
	from .api import gis

	return gis.canonical_tax_lot(value)


def county_config(county) -> tuple:
	from .api import gis

	return gis._county(county)


def describe_tax_lot(row: dict) -> dict:
	return {
		"map_taxlot": row.get("map_taxlot") or row.get("name"),
		"county": row.get("county") or None,
		"account": row.get("account") or None,
		"owner_of_record": row.get("owner_of_record") or None,
		"situs": row.get("situs") or None,
		"acres_gis": round(float(row["acres_gis"]), 2) if row.get("acres_gis") else None,
		"source": row.get("source") or None,
		"geometry": json_value(row.get("geometry")),
		"source_url": row.get("source_url") or None,
		"fetched_on": str(row.get("fetched_on") or "") or None,
		"raw_attributes": json_value(row.get("raw_attributes")),
	}


def tax_lot_row(name: str) -> dict | None:
	if not name or not frappe.db.exists(COUNTY_TAX_LOT, name):
		return None
	fields = compat.existing_fields(COUNTY_TAX_LOT, ("name", *TAX_LOT_FIELDS))
	return dict(frappe.db.get_value(COUNTY_TAX_LOT, name, fields, as_dict=True) or {})


def upsert_tax_lot(values: dict) -> tuple[str, bool]:
	"""Write one lot into the cache. `(docname, created)`. The only writer there is."""
	name = values["map_taxlot"]
	created = not frappe.db.exists(COUNTY_TAX_LOT, name)
	doc = frappe.new_doc(COUNTY_TAX_LOT) if created else frappe.get_doc(COUNTY_TAX_LOT, name)
	for field in TAX_LOT_FIELDS:
		if field in values:
			value = values[field]
			if field in ("geometry", "raw_attributes") and value is not None and not isinstance(value, str):
				value = dump(value)
			doc.set(field, value)
	doc.flags.county_refresh = True
	doc.flags.ignore_permissions = True
	if created:
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc.name, created


def manual_values(map_taxlot, county_key: str, manual: dict) -> dict:
	"""A lot seeded by hand, checked as hard as one the county sent."""
	if not isinstance(manual, dict):
		raise ToolError("manual must be an object of the lot's fields. Nothing was written.")
	allowed = {"account", "owner_of_record", "situs", "acres_gis", "geometry", "raw_attributes", "source_url"}
	unknown = sorted(set(manual) - allowed)
	if unknown:
		raise ToolError(
			f"manual does not take {', '.join(unknown)}. It takes {', '.join(sorted(allowed))}. "
			"Nothing was written."
		)
	values = {
		"map_taxlot": canonical_lot(map_taxlot),
		"county": county_key,
		"source": "Manual",
		"fetched_on": str(frappe.utils.now()),
	}
	for key in ("account", "owner_of_record", "situs", "source_url"):
		if key in manual:
			values[key] = str(manual.get(key) or "").strip() or None
	if "acres_gis" in manual and manual["acres_gis"] not in (None, ""):
		try:
			acres = float(manual["acres_gis"])
		except (TypeError, ValueError):
			raise ToolError(f"manual.acres_gis must be a number, got {manual['acres_gis']!r}.") from None
		if acres <= 0:
			raise ToolError(f"manual.acres_gis must be more than zero, got {acres:g}. Nothing was written.")
		values["acres_gis"] = round(acres, 4)
	if manual.get("geometry") not in (None, ""):
		values["geometry"] = polygon(manual["geometry"], "manual.geometry")
	if "raw_attributes" in manual:
		raw = json_value(manual["raw_attributes"]) if manual["raw_attributes"] not in (None, "") else None
		if raw is not None and not isinstance(raw, dict):
			raise ToolError("manual.raw_attributes must be an object. Nothing was written.")
		values["raw_attributes"] = raw
	return values


def polygon(value, label: str) -> dict:
	"""A Polygon or MultiPolygon GeoJSON geometry, or a refusal naming `label`."""
	geometry = geo.parse(value, label)
	if geometry.get("type") not in ("Polygon", "MultiPolygon"):
		raise ToolError(f"{label} must be a Polygon or a MultiPolygon, got {geometry.get('type')}.")
	return geometry


def query_params(map_taxlot=None, point=None, bbox=None) -> tuple[dict, dict]:
	"""`(params, asked)` for exactly one of the three questions a county can answer."""
	from .api import gis

	asked = [
		label for label, given in (("map_taxlot", map_taxlot), ("point", point), ("bbox", bbox)) if given
	]
	if len(asked) != 1:
		raise ToolError(
			"pass exactly one of map_taxlot, point (longitude and latitude) or bbox "
			f"([west, south, east, north]); got {len(asked) or 'none'}."
		)
	params = {"outFields": "*", "outSR": 4326, "returnGeometry": "true", "f": "geojson"}
	if map_taxlot:
		canonical = canonical_lot(map_taxlot)
		return {**params, "where": f"{{field}}='{canonical}'"}, {"map_taxlot": canonical}
	if point:
		longitude = gis._degrees(point[0], "longitude", 180.0)
		latitude = gis._degrees(point[1], "latitude", 90.0)
		return {
			**params,
			"geometry": json.dumps({"x": longitude, "y": latitude}),
			"geometryType": "esriGeometryPoint",
			"inSR": 4326,
			"spatialRel": "esriSpatialRelIntersects",
		}, {"point": [longitude, latitude]}
	if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
		raise ToolError("bbox must be four numbers: [west, south, east, north].")
	west = gis._degrees(bbox[0], "bbox west", 180.0)
	south = gis._degrees(bbox[1], "bbox south", 90.0)
	east = gis._degrees(bbox[2], "bbox east", 180.0)
	north = gis._degrees(bbox[3], "bbox north", 90.0)
	if west >= east or south >= north:
		raise ToolError("bbox must be [west, south, east, north] with west < east and south < north.")
	if east - west > MAX_BBOX_DEGREES or north - south > MAX_BBOX_DEGREES:
		raise ToolError(
			f"bbox is {east - west:.3f}° by {north - south:.3f}°; the most is {MAX_BBOX_DEGREES}° a side, "
			"about three miles. A county's lots are looked up a neighbourhood at a time."
		)
	return {
		**params,
		"geometry": json.dumps({"xmin": west, "ymin": south, "xmax": east, "ymax": north}),
		"geometryType": "esriGeometryEnvelope",
		"inSR": 4326,
		"spatialRel": "esriSpatialRelIntersects",
	}, {"bbox": [west, south, east, north]}


def fetch_tax_lots(county=None, map_taxlot=None, point=None, bbox=None) -> tuple[list, list, dict]:
	"""`(values_per_lot, warnings, asked)` from the county's live layer. Writes nothing."""
	from .api import gis

	key, config = county_config(county)
	params, asked = query_params(map_taxlot, point, bbox)
	if "where" in params:
		params["where"] = params["where"].replace("{field}", config["tax_lot_field"])
	try:
		payload = gis._fetch(config["url"], params)
	except ToolError as exc:
		raise ToolError(
			f"{exc} {config['label']}'s GIS server is the county's, not this site's. While it is "
			"down, seed the lot by hand: taxlot_lookup with map_taxlot and manual={owner_of_record, "
			"account, situs, acres_gis, geometry} from the tax statement, then taxlot_refresh once "
			"the server answers."
		) from None
	_features, warnings = gis.parse_features(payload, config)
	names = config.get("properties") or {}
	source_url = f"{config['url']}?{urlencode(params)}"
	now = str(frappe.utils.now())
	out = []
	for entry in payload.get("features") or []:
		if not isinstance(entry, dict):
			continue
		geometry = entry.get("geometry")
		if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
			continue
		properties = entry.get("properties") or entry.get("attributes") or {}
		number = gis._text(gis._property(properties, names.get("tax_lot", ())))
		try:
			canonical = canonical_lot(number)
		except ToolError:
			warnings.append(
				f"The county returned a lot numbered {number!r}, which is not a tax lot; skipped."
			)
			continue
		account = gis._property(properties, names.get("account", ()))
		out.append(
			{
				"map_taxlot": canonical,
				"county": key,
				"account": gis._text(account),
				"owner_of_record": gis._text(gis._property(properties, names.get("taxpayer", ()))),
				"situs": gis._text(gis._property(properties, SITUS_FIELDS)),
				"acres_gis": gis._acres(gis._property(properties, names.get("acres", ()))),
				"source": "County GIS",
				"geometry": geometry,
				"source_url": source_url,
				"fetched_on": now,
				"raw_attributes": properties if isinstance(properties, dict) else None,
			}
		)
		if len(out) >= gis._MAX_FEATURES:
			break
	return out, warnings, {"county": key, **asked}


# ── Lot Line Adjustment ─────────────────────────────────────────────────────
def piece_rows(doc) -> list:
	return [dict(row) for row in (doc.get("pieces") or [])]


def check_parties(doc) -> None:
	"""The two parties are real, and are two."""
	for index in (1, 2):
		party_type = str(doc.get(f"party_{index}_type") or "")
		party = str(doc.get(f"party_{index}") or "")
		if party_type and party_type not in PARTY_TYPES:
			raise ToolError(
				f"party_{index}_type must be one of {', '.join(PARTY_TYPES)}, got {party_type!r}."
			)
		if party and not party_type:
			raise ToolError(
				f"party_{index} is set with no party_{index}_type (Company, Customer or Supplier)."
			)
		if party and not frappe.db.exists(party_type, party):
			raise ToolError(f"no {party_type} called {party!r} on this site (party_{index}).")
	if doc.get("party_1") and (doc.get("party_1"), doc.get("party_1_type")) == (
		doc.get("party_2"),
		doc.get("party_2_type"),
	):
		raise ToolError("party_1 and party_2 are the same; a lot line adjustment is between two owners.")


def check_terms(doc) -> None:
	consideration = str(doc.get("consideration") or "Even swap")
	if consideration not in CONSIDERATIONS:
		raise ToolError(f"consideration must be one of {', '.join(CONSIDERATIONS)}, got {consideration!r}.")
	amount = float(doc.get("true_up_amount") or 0)
	if amount < 0:
		raise ToolError("true_up_amount cannot be negative; say who pays with true_up_payer.")
	if consideration == "Even swap" and amount:
		raise ToolError(
			f"consideration is Even swap and true_up_amount is {amount:,.2f}. An even swap moves no cash — "
			"set consideration to Cash true-up or Netted, or clear the amount."
		)
	if consideration == "Cash true-up" and amount and not doc.get("true_up_payer"):
		raise ToolError("a Cash true-up needs true_up_payer: Party 1 or Party 2.")


def check_piece(row: dict, label: str) -> None:
	for key in ("from_party", "to_party"):
		if row.get(key) not in SIDES:
			raise ToolError(f"{label}: {key} must be Party 1 or Party 2.")
	if row["from_party"] == row["to_party"]:
		raise ToolError(
			f"{label}: from_party and to_party are both {row['from_party']}; a piece moves between parties."
		)
	for key in ("acres_gis", "acres_surveyed"):
		if row.get(key) not in (None, "") and float(row[key]) < 0:
			raise ToolError(f"{label}: {key} cannot be negative.")
	if row.get("geometry") not in (None, ""):
		polygon(json_value(row["geometry"]) or row["geometry"], f"{label} geometry")


def validate_adjustment(doc) -> None:
	"""Everything that must hold on any save. Used by the controller and the tools."""
	status = str(doc.get("status") or DRAFT)
	if status not in STATUSES:
		raise ToolError(f"status must be one of {', '.join(STATUSES)}, got {status!r}.")
	for key in ("lot_1", "lot_2"):
		if doc.get(key) and not frappe.db.exists(COUNTY_TAX_LOT, doc.get(key)):
			raise ToolError(
				f"{key} {doc.get(key)!r} is not a County Tax Lot here. Look it up with taxlot_lookup first."
			)
	if doc.get("lot_1") and doc.get("lot_1") == doc.get("lot_2"):
		raise ToolError("lot_1 and lot_2 are the same tax lot; an adjustment moves a line between two lots.")
	check_parties(doc)
	check_terms(doc)
	for index, row in enumerate(piece_rows(doc), start=1):
		check_piece(row, f"piece {index} ({row.get('piece_name') or 'unnamed'})")


def submission_refusals(doc) -> list:
	"""What is missing before an adjustment can go to the county."""
	missing = []
	for key, what in (
		("lot_1", "lot_1"),
		("lot_2", "lot_2"),
		("party_1", "party_1"),
		("party_2", "party_2"),
		("signer_1", "signer_1"),
		("signer_2", "signer_2"),
	):
		if not doc.get(key):
			missing.append(what)
	if not piece_rows(doc):
		missing.append("at least one piece")
	return missing


# ── Record Survey ───────────────────────────────────────────────────────────
def survey_refusals(doc) -> list:
	"""Why Record Survey cannot run on this document, in words. Empty means it can."""
	refusals = []
	if int(doc.get("docstatus") or 0) != 1:
		refusals.append("the adjustment is not submitted (docstatus must be 1)")
	if doc.get("status") != RECORDED:
		refusals.append(f"its status is {doc.get('status')!r}, and Record Survey waits for Recorded")
	for index, row in enumerate(piece_rows(doc), start=1):
		label = row.get("piece_name") or f"piece {index}"
		if not float(row.get("acres_surveyed") or 0) > 0:
			refusals.append(f"{label} has no acres_surveyed")
		if json_value(row.get("geometry")) is None:
			refusals.append(f"{label} has no final geometry")
	if not piece_rows(doc):
		refusals.append("it has no pieces")
	return refusals


def _shape(geometry):
	from shapely.geometry import shape

	return shape(geometry).buffer(0)


def _geojson(shape_) -> dict:
	from shapely.geometry import mapping

	return json.loads(json.dumps(mapping(shape_)))


def side_plan(doc, which: str) -> dict:
	"""What Record Survey will write for one side, computed from the record alone."""
	index = 1 if which == PARTY_1 else 2
	lot_name = doc.get(f"lot_{index}")
	lot = tax_lot_row(lot_name) or {}
	given = [row for row in piece_rows(doc) if row.get("from_party") == which]
	received = [row for row in piece_rows(doc) if row.get("to_party") == which]
	given_acres = round(sum(float(row.get("acres_surveyed") or 0) for row in given), 4)
	received_acres = round(sum(float(row.get("acres_surveyed") or 0) for row in received), 4)
	plan = {
		"side": which,
		"party_type": doc.get(f"party_{index}_type"),
		"party": doc.get(f"party_{index}"),
		"lot": lot_name,
		"pieces_given": [row.get("piece_name") for row in given],
		"pieces_received": [row.get("piece_name") for row in received],
		"acres_given_surveyed": given_acres,
		"acres_received_surveyed": received_acres,
		"warnings": [],
	}
	stated = float(doc.get(f"lot_{index}_acres_surveyed") or 0)
	if stated > 0:
		plan["acres_after"] = round(stated, 3)
		plan["acreage_basis"] = "the recorded survey's figure for the adjusted lot"
	elif lot.get("acres_gis"):
		plan["acres_after"] = round(float(lot["acres_gis"]) - given_acres + received_acres, 3)
		plan["acreage_basis"] = (
			f"county GIS {float(lot['acres_gis']):g} ac − {given_acres:g} ac given + "
			f"{received_acres:g} ac received (surveyed). Set lot_{index}_acres_surveyed to use the survey's own figure."
		)
	else:
		plan["acres_after"] = None
		plan["acreage_basis"] = None

	lot_geometry = json_value(lot.get("geometry"))
	plan["geometry_after"] = None
	if lot_geometry is None:
		plan["warnings"].append(
			f"{lot_name} has no county polygon, so the Parcel's boundary is left as it is."
		)
	elif not geo.available():
		plan["warnings"].append(
			f"this site is missing {geo.requires_sentence()}, so no boundary was computed."
		)
	else:
		from shapely.ops import unary_union

		result = _shape(lot_geometry)
		if given:
			result = result.difference(unary_union([_shape(json_value(row["geometry"])) for row in given]))
		if received:
			result = unary_union([result, *[_shape(json_value(row["geometry"])) for row in received]])
		result = result.buffer(0)
		if result.is_empty or result.geom_type not in ("Polygon", "MultiPolygon"):
			plan["warnings"].append(
				f"{lot_name} less the pieces given leaves no area; check the piece geometry. No boundary was computed."
			)
		else:
			plan["geometry_after"] = _geojson(result)
			plan["area_after_computed_acres"] = geo.area_acres(plan["geometry_after"])
	return plan


def matching_parcel(company: str, map_taxlot: str) -> str | None:
	"""The Parcel on `company` whose parcel_id is this lot, however it was spelled."""
	rows = (
		frappe.db.get_all(
			PARCEL, filters={"owning_entity": company}, fields=["name", "parcel_id"], limit=5000
		)
		or []
	)
	for row in rows:
		number = str(row.get("parcel_id") or "").strip()
		if not number:
			continue
		try:
			if canonical_lot(number) == map_taxlot:
				return row["name"]
		except ToolError:
			continue
	return None
