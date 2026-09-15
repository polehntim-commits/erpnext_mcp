# SPDX-License-Identifier: MIT
"""County tax lots and lot line adjustments — v0.169.0.

THE FIXTURE IS THE ACTUAL DEAL: a lot line adjustment between PFI and Highland
LLC in Wasco County, Oregon.

    Parcel A  ~2.36 ac  PFI -> Highland   wedge along Dry Hollow Rd south of the packing house
    Parcel B  ~2.46 ac  Highland -> PFI   Hill Place orchard next to the packing house
    Even swap, no cash. Signers Deede Anderson (President, PFI) and Donella Polehn
    (Member, Highland). Lender Columbia Bank, 5.25% RE loan. Close before
    2026-12-31. Five open items, including the map and tax lot numbers.

THE TAX LOT NUMBERS ARE AN OPEN ITEM ON THE REAL DEAL, so the two lots below
carry placeholder numbers and rectangle polygons either side of one line.
Nothing else here is invented.

1. `TheCountyIsAReference`: lookup by number, point or box; manual seeding
   while the county answers 503; refresh; a failed refresh changes nothing;
   the cache links to nothing and the Desk cannot write it.
2. `DraftingTheDeal`: the deal is created, pieces, open items and easements
   are added by party name, and the refusals are named.
3. `SubmissionFixesTheTerms`: the status is the workflow, and what stays
   editable after submission is read off the JSON.
4. `TheMemorandum`: the MOU carries every term, escapes what it prints, and is
   a real PDF. The Print Format is seeded on migrate and never overwritten.
5. `NothingReachesTheBooksUntilTheButton` and `RecordSurvey`: no Parcel exists
   until a System Manager records the survey, which writes the adjusted Parcels
   through the Parcel tools, reuses a Parcel carrying the lot number, skips a
   Customer party, refuses geometry that disagrees with the acreage, and writes
   the same answer twice.
"""

import base64
import copy
import inspect
import io
import json
import math
import unittest
from typing import ClassVar

import frappe

from erpnext_mcp import geo, install
from erpnext_mcp import land_adjustment as land
from erpnext_mcp import mou_print_format as mou
from erpnext_mcp.api import gis
from erpnext_mcp.errors import ToolError

from .fixtures import V12TestCase
from .harness import ROLES, STORE

try:
	from pypdf import PdfReader
except Exception:  # pragma: no cover - the first CI leg has no pypdf
	PdfReader = None

needs_geo = unittest.skipUnless(geo.available(), "needs shapely and h3")

PFI = "PFI"
HIGHLAND = "Highland LLC"

# ── the ground ──────────────────────────────────────────────────────────────
SOUTH, NORTH = 45.5800, 45.58234
MID = (SOUTH + NORTH) / 2
WEST, LINE, EAST = -121.164, -121.160, -121.156
LOT_1 = "1N 13E 3 1000"  # PFI, the packing house (placeholder number)
LOT_2 = "1N 13E 3 1100"  # Highland, Hill Place (placeholder number)
M_PER_DEG_LAT = 111_195.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(MID))
SQ_M_PER_ACRE = 4046.8564224


def box(west, south, east, north):
	return {
		"type": "Polygon",
		"coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
	}


def strip_width(acres, height_deg):
	"""Degrees of longitude a strip `height_deg` tall must be to hold `acres`."""
	return acres * SQ_M_PER_ACRE / (height_deg * M_PER_DEG_LAT) / M_PER_DEG_LON


LOT_1_GEOMETRY = box(WEST, SOUTH, LINE, NORTH)
LOT_2_GEOMETRY = box(LINE, SOUTH, EAST, NORTH)
#: Parcel A: the south half of PFI's lot, along the line — goes to Highland.
PIECE_A = box(LINE - strip_width(2.36, MID - SOUTH), SOUTH, LINE, MID)
#: Parcel B: the north half of Highland's lot, along the line — goes to PFI.
PIECE_B = box(LINE, MID, LINE + strip_width(2.46, NORTH - MID), NORTH)

OPEN_ITEMS = (
	{
		"item": "Barn power",
		"question": "Whose meter serves the barn once the line moves?",
		"owner": "Donella Polehn",
	},
	{
		"item": "Access and irrigation easements",
		"question": "Draft the access and irrigation easements.",
		"owner": "Deede Anderson",
	},
	{
		"item": "Bank written consent",
		"question": "Columbia Bank must consent in writing.",
		"owner": "Donella Polehn",
	},
	{
		"item": "Commercial appraisal",
		"question": "Does the bank need a commercial appraisal?",
		"owner": "Deede Anderson",
	},
	{
		"item": "Map/tax-lot numbers",
		"question": "Confirm both lots' map and tax lot numbers with the assessor.",
		"owner": "Donella Polehn",
	},
)


def the_deal(**overrides):
	deal = {
		"title": "PFI / Highland lot line adjustment — Dry Hollow Rd",
		"county": "Wasco",
		"state": "OR",
		"party_1_type": "Company",
		"party_1": PFI,
		"signer_1": "Deede Anderson",
		"signer_1_title": "President",
		"party_2_type": "Company",
		"party_2": HIGHLAND,
		"signer_2": "Donella Polehn",
		"signer_2_title": "Member",
		"consideration": "Even swap",
		"lender": "Columbia Bank",
		"lender_conditions": "5.25% real estate loan; written consent required before recording.",
		"target_close": "2026-12-31",
		"pieces": [
			{
				"piece_name": "Parcel A",
				"from_party": PFI,
				"to_party": HIGHLAND,
				"acres_gis": 2.36,
				"line_notes": "Wedge along Dry Hollow Rd south of the packing house.",
				"geometry": PIECE_A,
			},
			{
				"piece_name": "Parcel B",
				"from_party": HIGHLAND,
				"to_party": PFI,
				"acres_gis": 2.46,
				"line_notes": "Hill Place orchard next to the packing house.",
				"improvements": "Orchard",
				"geometry": PIECE_B,
			},
		],
		"open_items": [dict(item) for item in OPEN_ITEMS],
		"easements": [
			{
				"type": "access",
				"burdened": HIGHLAND,
				"benefited": PFI,
				"notes": "Truck access to the packing house.",
			},
			{
				"type": "irrigation",
				"burdened": PFI,
				"benefited": HIGHLAND,
				"notes": "Existing line to Hill Place.",
			},
		],
	}
	deal.update(overrides)
	return deal


def county_feature(number=LOT_1, geometry=LOT_1_GEOMETRY, taxpayer="PFI", acres=20.01, account=7503):
	return {
		"type": "Feature",
		"geometry": geometry,
		"properties": {
			"MapTaxlot": number,
			"Taxpayer": taxpayer,
			"CalculatedAcres": acres,
			"AccountNum": account,
			"SitusAddress": "2535 Dry Hollow Rd",
			"MaintArea": "07",
		},
	}


class LandTestCase(V12TestCase):
	SWITCHES: ClassVar[dict] = {
		f"allow_{name}": 1
		for name in (
			"taxlot_lookup",
			"taxlot_refresh",
			"lla_list",
			"lla_get",
			"lla_create",
			"lla_update",
			"lla_add_piece",
			"lla_set_geometry",
			"lla_add_open_item",
			"lla_add_easement",
			"lla_render_mou",
			"lla_record_survey",
			"create_parcel",
			"get_parcel",
		)
	}

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **self.SWITCHES)
		STORE.seed(
			"Company",
			[
				{
					"name": PFI,
					"abbr": "PFI",
					"default_currency": "USD",
					"country": "United States",
					"is_group": 0,
				},
				{
					"name": HIGHLAND,
					"abbr": "HLD",
					"default_currency": "USD",
					"country": "United States",
					"is_group": 0,
				},
			],
		)
		STORE.seed("Customer", [{"name": "Dry Hollow Neighbor", "customer_name": "Dry Hollow Neighbor"}])
		self.requests = []
		self._fetch_before = gis._fetch
		self.addCleanup(setattr, gis, "_fetch", self._fetch_before)
		self.county_answers([county_feature()])
		self._roles_before = copy.deepcopy(ROLES.get("Administrator"))
		self.addCleanup(ROLES.__setitem__, "Administrator", self._roles_before)

	# ── the county ──────────────────────────────────────────────────────────
	def county_answers(self, features):
		def fake(url, params):
			self.requests.append(dict(params))
			return {"type": "FeatureCollection", "features": copy.deepcopy(features)}

		gis._fetch = fake

	def county_down(self):
		def fake(url, params):
			self.requests.append(dict(params))
			raise ToolError(
				"the county GIS server answered HTTP 503. Nothing was changed. That is the county's "
				"server rather than this site — try again, or draw the boundary by hand."
			)

		gis._fetch = fake

	def seed_lots(self):
		for number, geometry, owner, acres in (
			(LOT_1, LOT_1_GEOMETRY, "PFI", geo.area_acres(LOT_1_GEOMETRY)),
			(LOT_2, LOT_2_GEOMETRY, "HIGHLAND LLC", geo.area_acres(LOT_2_GEOMETRY)),
		):
			self.tool_data(
				"taxlot_lookup",
				{
					"map_taxlot": number,
					"manual": {
						"owner_of_record": owner,
						"acres_gis": round(acres, 2),
						"geometry": geometry,
						"situs": "Dry Hollow Rd",
					},
				},
			)

	def as_roles(self, *roles):
		ROLES["Administrator"] = list(roles)

	# ── the deal ────────────────────────────────────────────────────────────
	def create(self, **overrides):
		return self.tool_data("lla_create", the_deal(**overrides))

	def submitted(self):
		self.seed_lots()
		name = self.create(lot_1=LOT_1, lot_2=LOT_2)["name"]
		self.tool_data("lla_update", {"name": name, "fields": {"status": "Submitted to county"}})
		return name

	def recorded(self, surveyed=(2.355, 2.471)):
		name = self.submitted()
		for status in ("Approved", "Recorded"):
			self.tool_data("lla_update", {"name": name, "fields": {"status": status}})
		for piece, acres in zip(("Parcel A", "Parcel B"), surveyed, strict=True):
			self.tool_data("lla_add_piece", {"name": name, "piece_name": piece, "acres_surveyed": acres})
		return name

	def parcels(self):
		return (
			frappe.db.get_all(
				"Parcel", fields=["name", "owning_entity", "acreage", "parcel_id", "boundary_geojson"]
			)
			or []
		)


# ── 1. the county ───────────────────────────────────────────────────────────
class TheCountyIsAReference(LandTestCase):
	def test_a_lookup_by_number_caches_the_lot_with_every_attribute(self):
		data = self.tool_data("taxlot_lookup", {"map_taxlot": "1N13E03 01000"})
		self.assertEqual(data["created"], [LOT_1])
		self.assertEqual(self.requests[0]["where"], f"MapTaxlot='{LOT_1}'")
		lot = data["lots"][0]
		self.assertEqual(lot["owner_of_record"], "PFI")
		self.assertEqual(lot["account"], "7503")
		self.assertEqual(lot["situs"], "2535 Dry Hollow Rd")
		self.assertEqual(lot["acres_gis"], 20.01)
		self.assertEqual(lot["source"], "County GIS")
		self.assertEqual(lot["geometry"], LOT_1_GEOMETRY)
		self.assertEqual(lot["raw_attributes"]["MaintArea"], "07")
		self.assertIn("public.co.wasco.or.us", lot["source_url"])

	def test_a_point_asks_x_longitude_and_y_latitude(self):
		self.tool_data("taxlot_lookup", {"longitude": -121.162, "latitude": 45.581})
		sent = json.loads(self.requests[0]["geometry"])
		self.assertEqual((sent["x"], sent["y"]), (-121.162, 45.581))
		self.assertEqual(self.requests[0]["geometryType"], "esriGeometryPoint")

	def test_a_box_is_an_envelope_and_a_county_sized_box_is_refused_unasked(self):
		self.county_answers([county_feature(), county_feature(LOT_2, LOT_2_GEOMETRY, "HIGHLAND LLC")])
		data = self.tool_data("taxlot_lookup", {"bbox": [WEST, SOUTH, EAST, NORTH]})
		self.assertEqual(sorted(data["created"]), [LOT_1, LOT_2])
		self.assertEqual(self.requests[0]["geometryType"], "esriGeometryEnvelope")
		self.requests.clear()
		self.assertIn("0.05", self.tool_error("taxlot_lookup", {"bbox": [-121.5, 45.3, -120.9, 45.8]}))
		self.assertEqual(self.requests, [])

	def test_two_questions_at_once_are_refused(self):
		error = self.tool_error(
			"taxlot_lookup", {"map_taxlot": LOT_1, "longitude": -121.16, "latitude": 45.58}
		)
		self.assertIn("exactly one", error)

	def test_while_the_county_is_down_a_lot_is_seeded_by_hand(self):
		self.county_down()
		error = self.tool_error("taxlot_lookup", {"map_taxlot": LOT_1})
		self.assertIn("503", error)
		self.assertIn("manual", error)
		self.assertFalse(frappe.db.exists(land.COUNTY_TAX_LOT, LOT_1))
		data = self.tool_data(
			"taxlot_lookup",
			{
				"map_taxlot": "1N-13E-3-1000",
				"manual": {"owner_of_record": "PFI", "acres_gis": 20, "geometry": LOT_1_GEOMETRY},
			},
		)
		self.assertEqual(data["created"], [LOT_1])
		self.assertEqual(data["lots"][0]["source"], "Manual")

	def test_a_manual_seed_is_checked(self):
		self.assertIn(
			"manual does not take",
			self.tool_error("taxlot_lookup", {"map_taxlot": LOT_1, "manual": {"acres": 2}}),
		)
		self.assertIn(
			"Polygon",
			self.tool_error(
				"taxlot_lookup",
				{
					"map_taxlot": LOT_1,
					"manual": {"geometry": {"type": "Point", "coordinates": [-121.16, 45.58]}},
				},
			),
		)
		self.assertIn(
			"tax lot",
			self.tool_error("taxlot_lookup", {"map_taxlot": "Dry Hollow", "manual": {"acres_gis": 2}}),
		)

	def test_refresh_replaces_a_manual_seed_with_the_county_record(self):
		self.tool_data(
			"taxlot_lookup", {"map_taxlot": LOT_1, "manual": {"owner_of_record": "P.F.I.", "acres_gis": 19}}
		)
		data = self.tool_data("taxlot_refresh", {"map_taxlot": LOT_1})
		self.assertTrue(data["refreshed"])
		self.assertEqual(data["lot"]["source"], "County GIS")
		self.assertEqual(data["lot"]["owner_of_record"], "PFI")
		self.assertIn("owner_of_record", data["changed"])

	def test_a_failed_refresh_leaves_the_cached_row_exactly_as_it_was(self):
		self.tool_data(
			"taxlot_lookup", {"map_taxlot": LOT_1, "manual": {"owner_of_record": "PFI", "acres_gis": 20}}
		)
		before = land.tax_lot_row(LOT_1)
		self.county_down()
		self.assertIn("503", self.tool_error("taxlot_refresh", {"map_taxlot": LOT_1}))
		self.assertEqual(land.tax_lot_row(LOT_1), before)
		self.county_answers([])
		data = self.tool_data("taxlot_refresh", {"map_taxlot": LOT_1})
		self.assertFalse(data["refreshed"])
		self.assertEqual(land.tax_lot_row(LOT_1), before)

	def test_refreshing_a_lot_never_looked_up_is_refused(self):
		self.assertIn("taxlot_lookup", self.tool_error("taxlot_refresh", {"map_taxlot": LOT_2}))

	def test_the_cache_links_out_to_nothing_and_every_field_is_read_only(self):
		data = land._doctype_json(land.COUNTY_TAX_LOT)
		kinds = {field["fieldtype"] for field in data["fields"]}
		self.assertFalse(kinds & {"Link", "Dynamic Link", "Table", "Table MultiSelect"})
		for field in data["fields"]:
			if field["fieldtype"] not in ("Section Break", "Column Break"):
				with self.subTest(field=field["fieldname"]):
					self.assertEqual(field.get("read_only"), 1)
		self.assertTrue(all(not perm.get("write") for perm in data["permissions"]))
		self.assertEqual(
			{perm["role"] for perm in data["permissions"]},
			{"System Manager", "Land Reference", "Land Agreements"},
		)

	def test_the_desk_cannot_save_a_tax_lot_and_the_refresh_job_can(self):
		"""Calls the controller directly — the double never runs app controllers — so this
		proves the refusal, not that Frappe invokes it."""
		from erpnext_mcp.erpnext_mcp.doctype.county_tax_lot.county_tax_lot import CountyTaxLot

		doc = CountyTaxLot({"doctype": land.COUNTY_TAX_LOT, "map_taxlot": LOT_1})
		with self.assertRaises(Exception) as caught:
			doc.validate()
		self.assertIn("read-only", str(caught.exception))
		doc.flags.county_refresh = True
		doc.validate()

	def test_the_county_tools_need_a_land_role(self):
		self.as_roles("Accounts User")
		self.assertIn("Land Reference", self.tool_error("taxlot_lookup", {"map_taxlot": LOT_1}))
		self.as_roles("Land Reference")
		self.tool_data("taxlot_lookup", {"map_taxlot": LOT_1})


# ── 2. drafting ─────────────────────────────────────────────────────────────
class DraftingTheDeal(LandTestCase):
	def test_the_deal_is_created_as_a_draft_with_everything_on_it(self):
		data = self.create()
		self.assertEqual((data["status"], data["docstatus"]), ("Draft", 0))
		pieces = {row["piece_name"]: row for row in data["pieces"]}
		self.assertEqual(
			(pieces["Parcel A"]["from_party"], pieces["Parcel A"]["to_party"]), ("Party 1", "Party 2")
		)
		self.assertEqual(
			(pieces["Parcel B"]["from_party"], pieces["Parcel B"]["to_party"]), ("Party 2", "Party 1")
		)
		self.assertEqual(pieces["Parcel B"]["geometry"], PIECE_B)
		self.assertEqual([row["item"] for row in data["open_items"]], [row["item"] for row in OPEN_ITEMS])
		self.assertEqual({row["status"] for row in data["open_items"]}, {"Open"})
		self.assertEqual([row["easement_type"] for row in data["easements"]], ["Access", "Irrigation"])
		self.assertEqual(data["easements"][0]["burdened"], "Party 2")
		self.assertEqual(data["missing_for_submission"], ["lot_1", "lot_2"])
		self.assertFalse(data["record_survey"]["available"])

	@needs_geo
	def test_the_fixture_pieces_hold_the_acreage_the_deal_names(self):
		self.assertAlmostEqual(geo.area_acres(PIECE_A), 2.36, delta=0.03)
		self.assertAlmostEqual(geo.area_acres(PIECE_B), 2.46, delta=0.03)

	def test_list_and_get_read_it_back(self):
		name = self.create()["name"]
		listed = self.tool_data("lla_list", {"party": HIGHLAND})["adjustments"]
		self.assertEqual([row["name"] for row in listed], [name])
		self.assertEqual((listed[0]["piece_count"], listed[0]["open_item_count"]), (2, 5))
		self.assertEqual(self.tool_data("lla_get", {"name": name})["lender"], "Columbia Bank")

	def test_the_refusals_are_named_and_nothing_is_created(self):
		cases = (
			({"party_2": PFI}, "same"),
			({"consideration": "Even swap", "true_up_amount": 5000}, "Even swap"),
			({"lot_1": LOT_1}, "taxlot_lookup"),
			(
				{"pieces": [{"piece_name": "Loop", "from_party": PFI, "to_party": PFI}]},
				"moves between parties",
			),
			(
				{
					"pieces": [
						{
							"piece_name": "Dot",
							"from_party": PFI,
							"to_party": HIGHLAND,
							"geometry": {"type": "Point", "coordinates": [0, 1]},
						}
					]
				},
				"Polygon",
			),
			({"party_2": "Nobody LLC"}, "no Company"),
			({"status": "Approved"}, "Draft or Under review"),
		)
		for overrides, expected in cases:
			with self.subTest(expected=expected):
				self.assertIn(expected, self.tool_error("lla_create", the_deal(**overrides)))
		self.assertEqual(frappe.db.get_all(land.LOT_LINE_ADJUSTMENT) or [], [])

	def test_pieces_open_items_and_easements_are_added_and_updated_by_name(self):
		name = self.create(pieces=[], open_items=[], easements=[])["name"]
		self.tool_data(
			"lla_add_piece",
			{
				"name": name,
				"piece_name": "Parcel A",
				"from_party": "Party 1",
				"to_party": HIGHLAND,
				"acres_gis": 2.3,
			},
		)
		data = self.tool_data("lla_add_piece", {"name": name, "piece_name": "parcel a", "acres_gis": 2.36})
		self.assertEqual(
			[(row["piece_name"], row["acres_gis"]) for row in data["pieces"]], [("Parcel A", 2.36)]
		)
		data = self.tool_data(
			"lla_set_geometry",
			{"name": name, "piece": "Parcel A", "geojson": {"type": "Feature", "geometry": PIECE_A}},
		)
		self.assertEqual(data["pieces"][0]["geometry"], PIECE_A)
		self.tool_data(
			"lla_add_open_item", {"name": name, "item": "Bank written consent", "owner": "Donella Polehn"}
		)
		data = self.tool_data(
			"lla_add_open_item", {"name": name, "item": "Bank written consent", "status": "In progress"}
		)
		self.assertEqual(
			[(row["item"], row["status"], row["responsible"]) for row in data["open_items"]],
			[("Bank written consent", "In progress", "Donella Polehn")],
		)
		data = self.tool_data(
			"lla_add_easement",
			{
				"name": name,
				"type": "utility",
				"burdened": "Party 1",
				"benefited": "Party 2",
				"notes": "Barn power",
			},
		)
		self.assertEqual(data["easements"][0]["easement_type"], "Utility")

	@needs_geo
	def test_a_geometry_far_from_the_pieces_acreage_is_set_with_a_warning(self):
		name = self.create()["name"]
		data = self.tool_data(
			"lla_set_geometry", {"name": name, "piece": "Parcel A", "geojson": LOT_1_GEOMETRY}
		)
		self.assertTrue(any("set_parcel_boundary" in warning for warning in data["warnings"]))

	def test_the_editable_tools_need_the_land_agreements_role(self):
		self.as_roles("Land Reference")
		self.assertIn("Land Agreements", self.tool_error("lla_create", the_deal()))
		self.as_roles("Land Agreements")
		self.create()


# ── 3. submission ───────────────────────────────────────────────────────────
class SubmissionFixesTheTerms(LandTestCase):
	def test_what_stays_editable_is_exactly_the_allow_on_submit_set(self):
		self.assertEqual(
			land.after_submit_fields(),
			{
				"status",
				"lot_1_acres_surveyed",
				"lot_2_acres_surveyed",
				"pieces",
				"open_items",
				"recording_number",
				"recorded_on",
				"survey_reference",
				"parcel_1",
				"parcel_2",
				"survey_recorded_on",
				"survey_recorded_by",
				"notes",
			},
		)
		self.assertEqual(land.after_submit_fields(land.PIECE), {"acres_surveyed", "geometry"})
		self.assertEqual(
			land.after_submit_fields(land.OPEN_ITEM), {"item", "question", "responsible", "status", "due"}
		)
		self.assertEqual(land.after_submit_fields(land.EASEMENT), frozenset())

	def test_it_cannot_go_to_the_county_without_its_lots(self):
		name = self.create()["name"]
		error = self.tool_error("lla_update", {"name": name, "fields": {"status": "Submitted to county"}})
		self.assertIn("lot_1, lot_2", error)
		self.assertEqual(frappe.db.get_value(land.LOT_LINE_ADJUSTMENT, name, "docstatus"), 0)

	def test_submitted_to_county_submits_and_fixes_the_terms(self):
		name = self.submitted()
		data = self.tool_data("lla_get", {"name": name})
		self.assertEqual((data["status"], data["docstatus"]), ("Submitted to county", 1))
		self.assertEqual(data["next_statuses"], ["Approved", "Withdrawn"])
		self.assertIn(
			"fixes consideration",
			self.tool_error("lla_update", {"name": name, "fields": {"consideration": "Netted"}}),
		)
		self.assertIn(
			"no piece can be added",
			self.tool_error(
				"lla_add_piece",
				{"name": name, "piece_name": "Parcel C", "from_party": PFI, "to_party": HIGHLAND},
			),
		)
		self.assertIn(
			"only acres_surveyed and geometry",
			self.tool_error("lla_add_piece", {"name": name, "piece_name": "Parcel A", "acres_gis": 3}),
		)
		self.assertIn("fixed", self.tool_error("lla_add_easement", {"name": name, "type": "access"}))
		self.tool_data("lla_update", {"name": name, "fields": {"recording_number": "2026-004512"}})
		self.tool_data("lla_add_piece", {"name": name, "piece_name": "Parcel A", "acres_surveyed": 2.355})
		self.tool_data("lla_set_geometry", {"name": name, "piece": "Parcel B", "geojson": PIECE_B})
		data = self.tool_data(
			"lla_add_open_item", {"name": name, "item": "Bank written consent", "status": "Resolved"}
		)
		self.assertEqual(
			next(row for row in data["open_items"] if row["item"] == "Bank written consent")["status"],
			"Resolved",
		)

	def test_the_county_steps_run_in_order_and_do_not_run_backwards(self):
		name = self.submitted()
		self.assertIn(
			"Approved or Withdrawn",
			self.tool_error("lla_update", {"name": name, "fields": {"status": "Recorded"}}),
		)
		self.tool_data("lla_update", {"name": name, "fields": {"status": "Approved"}})
		self.assertIn(
			"Recorded or Withdrawn",
			self.tool_error("lla_update", {"name": name, "fields": {"status": "Submitted to county"}}),
		)
		self.assertIn("Draft", self.tool_error("lla_update", {"name": name, "fields": {"status": "Draft"}}))

	def test_withdrawing_a_submitted_adjustment_cancels_it(self):
		name = self.submitted()
		data = self.tool_data("lla_update", {"name": name, "fields": {"status": "Withdrawn"}})
		self.assertEqual((data["status"], data["docstatus"]), ("Withdrawn", 2))
		self.assertIn("cancelled", self.tool_error("lla_update", {"name": name, "fields": {"notes": "x"}}))

	def test_a_draft_skipping_the_county_is_refused(self):
		name = self.create()["name"]
		self.assertIn(
			"goes to Submitted to county",
			self.tool_error("lla_update", {"name": name, "fields": {"status": "Recorded"}}),
		)

	def test_the_survey_stamps_and_the_tables_are_not_lla_update_s(self):
		name = self.create()["name"]
		self.assertIn(
			"lla_record_survey only",
			self.tool_error("lla_update", {"name": name, "fields": {"parcel_1": "X"}}),
		)
		self.assertIn(
			"lla_add_piece", self.tool_error("lla_update", {"name": name, "fields": {"pieces": []}})
		)


# ── 4. the memorandum ───────────────────────────────────────────────────────
class TheMemorandum(LandTestCase):
	def test_the_mou_carries_every_term(self):
		self.seed_lots()
		name = self.create(lot_1=LOT_1, lot_2=LOT_2)["name"]
		html = mou.render_html(frappe.get_doc(land.LOT_LINE_ADJUSTMENT, name))
		for expected in (
			"Memorandum of Understanding",
			PFI,
			HIGHLAND,
			"Deede Anderson, President",
			"Donella Polehn, Member",
			"Even swap",
			"No cash changes hands",
			"Parcel A",
			"2.36",
			"Parcel B",
			"2.46",
			"Columbia Bank",
			"5.25%",
			"2026-12-31",
			"Access",
			"Irrigation",
			"Exhibit C",
			"Bank written consent",
			"Commercial appraisal",
			"Map/tax-lot numbers",
			LOT_1,
			"not a deed",
			"&#34;Polygon&#34;",
		):
			with self.subTest(expected=expected):
				self.assertIn(expected, html)
		self.assertIn(f"{PFI}</td><td>{HIGHLAND}", html.replace("\n", "").replace("        ", ""))

	def test_what_it_prints_is_escaped(self):
		name = self.create(title="<script>alert(1)</script>")["name"]
		html = mou.render_html(frappe.get_doc(land.LOT_LINE_ADJUSTMENT, name))
		self.assertNotIn("<script>", html)
		self.assertIn("&lt;script&gt;", html)

	def test_the_tool_returns_a_real_pdf(self):
		name = self.create()["name"]
		data = self.tool_data("lla_render_mou", {"name": name, "include_html": True})
		pdf = base64.b64decode(data["pdf_base64"])
		self.assertTrue(pdf.startswith(b"%PDF"))
		self.assertEqual(data["pdf_bytes"], len(pdf))
		self.assertEqual(data["renderer"], "erpnext_mcp render/pdf.py")
		self.assertIn("Columbia Bank", data["html"])
		if PdfReader is not None:
			text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
			for expected in (
				"Deede Anderson",
				"Donella Polehn",
				"Even swap",
				"Parcel A",
				"Exhibit C",
				"Barn power",
			):
				with self.subTest(expected=expected):
					self.assertIn(expected, text)

	def test_a_cash_true_up_says_who_pays(self):
		name = self.create(consideration="Cash true-up", true_up_amount=12500, true_up_payer=HIGHLAND)["name"]
		html = mou.render_html(frappe.get_doc(land.LOT_LINE_ADJUSTMENT, name))
		self.assertIn(f"{HIGHLAND} pays 12500.00", html)

	def test_the_print_format_is_seeded_once_and_never_overwritten(self):
		STORE.seed("DocType", [{"name": "Print Format"}, {"name": land.LOT_LINE_ADJUSTMENT}])
		report = mou.seed_mou_print_format()
		self.assertTrue(report["created"], report)
		self.assertEqual(
			frappe.db.get_value("Print Format", mou.FORMAT_NAME, "doc_type"), land.LOT_LINE_ADJUSTMENT
		)
		frappe.db.set_value("Print Format", mou.FORMAT_NAME, "html", "operator's own layout")
		self.assertEqual(mou.seed_mou_print_format()["reason"], "already present")
		self.assertEqual(
			frappe.db.get_value("Print Format", mou.FORMAT_NAME, "html"), "operator's own layout"
		)
		for hook in (install.after_install, install.after_migrate):
			self.assertIn("_mou_print_format()", inspect.getsource(hook))


# ── 5. the button ───────────────────────────────────────────────────────────
@needs_geo
class NothingReachesTheBooksUntilTheButton(LandTestCase):
	def test_no_parcel_exists_at_any_step_before_record_survey(self):
		self.assertEqual(self.parcels(), [])
		name = self.recorded()
		self.assertEqual(self.parcels(), [])
		data = self.tool_data("lla_get", {"name": name})
		self.assertTrue(data["record_survey"]["available"], data["record_survey"])

	def test_it_waits_for_recorded(self):
		name = self.submitted()
		self.tool_data("lla_add_piece", {"name": name, "piece_name": "Parcel A", "acres_surveyed": 2.355})
		error = self.tool_error("lla_record_survey", {"name": name})
		self.assertIn("waits for Recorded", error)
		self.assertIn("Parcel B has no acres_surveyed", error)
		self.assertEqual(self.parcels(), [])

	def test_it_is_a_system_manager_s_button(self):
		name = self.recorded()
		self.as_roles("Land Agreements")
		self.assertIn("System Manager", self.tool_error("lla_record_survey", {"name": name}))
		self.assertEqual(self.parcels(), [])


@needs_geo
class RecordSurvey(LandTestCase):
	def test_the_adjusted_parcels_are_written_from_the_survey(self):
		name = self.recorded()
		data = self.tool_data("lla_record_survey", {"name": name})
		survey = {entry["side"]: entry for entry in data["survey"]}
		lot_1_acres = round(geo.area_acres(LOT_1_GEOMETRY), 2)
		lot_2_acres = round(geo.area_acres(LOT_2_GEOMETRY), 2)
		self.assertAlmostEqual(survey["Party 1"]["acres_after"], lot_1_acres - 2.355 + 2.471, places=3)
		self.assertAlmostEqual(survey["Party 2"]["acres_after"], lot_2_acres - 2.471 + 2.355, places=3)
		self.assertIn("county GIS", survey["Party 1"]["acreage_basis"])
		self.assertEqual(survey["Party 1"]["action"], "created")

		parcels = {row["owning_entity"]: row for row in self.parcels()}
		self.assertEqual(set(parcels), {PFI, HIGHLAND})
		pfi = parcels[PFI]
		self.assertEqual(pfi["parcel_id"], LOT_1)
		self.assertAlmostEqual(float(pfi["acreage"]), survey["Party 1"]["acres_after"], places=3)
		boundary = json.loads(pfi["boundary_geojson"])
		from shapely.geometry import Point, shape

		shape_ = shape(boundary)
		# PFI gave up Parcel A and received Parcel B.
		self.assertFalse(shape_.contains(Point(LINE - 0.0001, (SOUTH + MID) / 2)))
		self.assertTrue(shape_.contains(Point(LINE + 0.0001, (MID + NORTH) / 2)))
		self.assertAlmostEqual(geo.area_acres(boundary), survey["Party 1"]["acres_after"], delta=0.05)

		self.assertEqual((data["parcel_1"], data["parcel_2"]), (pfi["name"], parcels[HIGHLAND]["name"]))
		self.assertTrue(data["survey_recorded_on"])
		self.assertTrue(data["record_survey"]["already_recorded"])

	def test_pressing_it_twice_writes_the_same_answer(self):
		name = self.recorded()
		first = self.tool_data("lla_record_survey", {"name": name})
		before = sorted((row["name"], float(row["acreage"])) for row in self.parcels())
		second = self.tool_data("lla_record_survey", {"name": name})
		self.assertEqual(sorted((row["name"], float(row["acreage"])) for row in self.parcels()), before)
		self.assertEqual((first["parcel_1"], first["parcel_2"]), (second["parcel_1"], second["parcel_2"]))
		self.assertEqual({entry["action"] for entry in second["survey"]}, {"boundary set"})

	def test_a_parcel_already_carrying_the_lot_number_is_updated_not_duplicated(self):
		existing = self.tool_data(
			"create_parcel",
			{
				"company": PFI,
				"parcel_name": "Packing House",
				"parcel_id": "1N13E03 01000",
				"acreage": 19.5,
				"county": "Wasco",
			},
		)["name"]
		name = self.recorded()
		data = self.tool_data("lla_record_survey", {"name": name})
		self.assertEqual(data["parcel_1"], existing)
		self.assertEqual(len([row for row in self.parcels() if row["owning_entity"] == PFI]), 1)
		self.assertEqual(next(e for e in data["survey"] if e["side"] == "Party 1")["action"], "updated")

	def test_the_survey_s_own_lot_acreage_wins_when_it_is_stated(self):
		name = self.recorded()
		stated = round(geo.area_acres(LOT_1_GEOMETRY) + 0.1, 3)
		self.tool_data("lla_update", {"name": name, "fields": {"lot_1_acres_surveyed": stated}})
		data = self.tool_data("lla_record_survey", {"name": name})
		party_1 = next(entry for entry in data["survey"] if entry["side"] == "Party 1")
		self.assertEqual(party_1["acres_after"], stated)
		self.assertIn("recorded survey", party_1["acreage_basis"])

	def test_a_customer_party_is_reported_and_not_written(self):
		self.seed_lots()
		deal = the_deal(lot_1=LOT_1, lot_2=LOT_2, party_2_type="Customer", party_2="Dry Hollow Neighbor")
		for piece in deal["pieces"]:
			for key in ("from_party", "to_party"):
				if piece[key] == HIGHLAND:
					piece[key] = "Party 2"
		for easement in deal["easements"]:
			for key in ("burdened", "benefited"):
				if easement[key] == HIGHLAND:
					easement[key] = "Party 2"
		name = self.tool_data("lla_create", deal)["name"]
		for status in ("Submitted to county", "Approved", "Recorded"):
			self.tool_data("lla_update", {"name": name, "fields": {"status": status}})
		for piece, acres in (("Parcel A", 2.355), ("Parcel B", 2.471)):
			self.tool_data("lla_add_piece", {"name": name, "piece_name": piece, "acres_surveyed": acres})
		data = self.tool_data("lla_record_survey", {"name": name})
		party_2 = next(entry for entry in data["survey"] if entry["side"] == "Party 2")
		self.assertIsNone(party_2["parcel"])
		self.assertIn("not a Company", party_2["skipped"])
		self.assertEqual({row["owning_entity"] for row in self.parcels()}, {PFI})

	def test_geometry_that_disagrees_with_the_acreage_is_refused_before_anything_is_written(self):
		name = self.recorded()
		# Parcel B redrawn as the whole of Highland's lot: PFI would gain twenty acres the survey does not show.
		self.tool_data("lla_set_geometry", {"name": name, "piece": "Parcel B", "geojson": LOT_2_GEOMETRY})
		error = self.tool_error("lla_record_survey", {"name": name})
		self.assertIn("adjusted polygon encloses", error)
		self.assertEqual(self.parcels(), [])
		self.assertFalse(frappe.db.get_value(land.LOT_LINE_ADJUSTMENT, name, "survey_recorded_on"))

	def test_a_recorded_adjustment_is_final(self):
		name = self.recorded()
		self.assertEqual(self.tool_data("lla_get", {"name": name})["next_statuses"], [])
		self.assertIn(
			"nothing further",
			self.tool_error("lla_update", {"name": name, "fields": {"status": "Withdrawn"}}),
		)

	def test_the_parcels_it_writes_follow_a_conveyance(self):
		from erpnext_mcp.tools import realestate

		self.assertIn(("Lot Line Adjustment", "parcel_1"), realestate.PARCEL_REFERRERS)
		self.assertIn(("Lot Line Adjustment", "parcel_2"), realestate.PARCEL_REFERRERS)


class TheControllers(unittest.TestCase):
	"""Called on constructed instances: the double never runs app controllers."""

	def test_cancelling_after_the_survey_is_recorded_is_refused(self):
		from erpnext_mcp.erpnext_mcp.doctype.lot_line_adjustment.lot_line_adjustment import LotLineAdjustment

		doc = LotLineAdjustment({"doctype": land.LOT_LINE_ADJUSTMENT, "status": "Recorded"})
		with self.assertRaises(Exception) as caught:
			doc.before_cancel()
		self.assertIn("cannot be cancelled", str(caught.exception))

	def test_submitting_from_the_desk_names_what_is_missing(self):
		from erpnext_mcp.erpnext_mcp.doctype.lot_line_adjustment.lot_line_adjustment import LotLineAdjustment

		doc = LotLineAdjustment({"doctype": land.LOT_LINE_ADJUSTMENT, "status": "Draft"})
		with self.assertRaises(Exception) as caught:
			doc.before_submit()
		self.assertIn("lot_1", str(caught.exception))

	def test_the_form_button_is_offered_exactly_where_the_server_allows_it(self):
		import os

		path = os.path.join(land._DOCTYPE_DIR, "lot_line_adjustment", "lot_line_adjustment.js")
		script = open(path, encoding="utf-8").read()
		for clause in (
			"frm.doc.docstatus === 1",
			'frm.doc.status === "Recorded"',
			"!frm.doc.survey_recorded_on",
			'has_role("System Manager")',
			'frm.call("record_survey")',
		):
			with self.subTest(clause=clause):
				self.assertIn(clause, script)
