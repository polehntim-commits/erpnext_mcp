# SPDX-License-Identifier: MIT
"""Fields Tim can find and define himself. v0.269.0 (docs/contracts/field_self_service_v0_269.yaml).

Mill Creek as OML has it (2026-10-07): "MC Centerpiece" on the record still called Bing Block - MC, and the
misspelled "Wind Mecine". Lookup by the names people use, the named group "Bing Block", rename keeping the old name,
outline acreage with a logged change, the history engine (and what a worker does not see), the card's hazards and
open tasks, and the contract fixtures the app decodes. Regenerate fixtures with FARM_CONTRACT_REGEN=1.
"""

import json

import frappe

from erpnext_mcp import field_card, field_names, geo, history
from erpnext_mcp.api import mobile as mobile_api

from . import test_contract_v0_262_0 as v262
from .harness import STORE, set_roles
from .test_api_mobile import MAIN, OTHER, WORKER, MobileAPITestCase

HERE = v262.HERE.parent / "v0_269_0"
CENTER = "Bing Block - MC"
WIND = "Wind Mecine - MC"
CENTER_RING = [[-121.23084, 45.586328], [-121.229051, 45.585234], [-121.230893, 45.58415], [-121.232513, 45.584564],
               [-121.232781, 45.585921], [-121.23084, 45.586328]]
WIND_RING = [[-121.230948, 45.584074], [-121.229023, 45.585151], [-121.2267, 45.583757], [-121.228154, 45.582431],
             [-121.230948, 45.584074]]


def mill_creek(case):
	STORE.seed("Parcel", [{"name": "Mill Creek - MC", "parcel_name": "Mill Creek", "owning_entity": MAIN, "acreage": 131.43},
	                      {"name": "Elsewhere - OT", "parcel_name": "Elsewhere", "owning_entity": OTHER, "acreage": 50}])
	STORE.seed("Field", [
		{"name": CENTER, "field_name": "MC Centerpiece", "parcel": "Mill Creek - MC", "owning_entity": MAIN, "acreage": 12.0,
		 "area_computed_acres": 11.3366, "crop": "Cherries", "variety": "Bing (Fallow post 2026 Harvest)",
		 "aliases": "Center Piece\nBing Block",
		 "boundary_geojson": json.dumps({"type": "Polygon", "coordinates": [CENTER_RING]})},
		{"name": WIND, "field_name": "Wind Mecine", "parcel": "Mill Creek - MC", "owning_entity": MAIN, "acreage": 14.0,
		 "area_computed_acres": 13.17, "crop": "Cherries", "variety": "Bing", "aliases": "Wind Machine\nBing Block",
		 "boundary_geojson": json.dumps({"type": "Polygon", "coordinates": [WIND_RING]})},
		{"name": "Pearls - OT", "field_name": "Pearls", "parcel": "Elsewhere - OT", "owning_entity": OTHER, "acreage": 9.0,
		 "aliases": "Center Piece"},
	])
	STORE.seed("Asset Register", [
		{"name": "MC-V1", "asset_type": "Irrigation Valve", "company": MAIN, "gps_latitude": 45.5852, "gps_longitude": -121.2310,
		 "valve_type": "Lateral"},
		{"name": "40-MAIN", "asset_type": "Irrigation Valve", "company": MAIN, "gps_latitude": 45.5824, "gps_longitude": -121.1884},
	])
	STORE.seed("Farm Task", [
		{"name": "FT-DONE", "task_name": "Pull sprinkler risers", "task_type": "Irrigation", "state": "Completed",
		 "location_doctype": "Field", "location": CENTER, "company": MAIN, "completed_at": "2026-10-05 15:00:00"},
		{"name": "FT-OPEN", "task_name": "Mark valves", "task_type": "Irrigation", "state": "Available",
		 "location_doctype": "Field", "location": CENTER, "company": MAIN},
	])
	STORE.seed("Crop Observation", [
		{"name": "OBS-1", "block_doctype": "Field", "block": CENTER, "observation_type": "Pest", "threat": "Spider Mites",
		 "observed_on": "2026-07-01", "count_observed": 12, "percent_affected": 30, "threshold_exceeded": 1},
		{"name": "OBS-2", "block_doctype": "Field", "block": CENTER, "observation_type": "Growth Stage",
		 "growth_stage_code": "85", "observed_on": "2026-06-20"},
	])
	STORE.seed("Scale Ticket", [{"name": "ST-1", "field": CENTER, "date": "2026-07-10", "variety": "Bing",
	                             "net_weight": 18000, "weight_uom": "lb"}])
	STORE.seed("Block Cost Entry", [{"name": "BCE-1", "field": CENTER, "posting_date": "2026-08-01", "amount": 4200,
	                                 "cost_category": "Pruning", "description": "Contract pruning"}])
	STORE.commit()


class FieldCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		mill_creek(self)


class ByTheNamesPeopleUse(FieldCase):
	def test_normalise(self):
		self.assertEqual(field_names.normalise("Center Piece"), field_names.normalise("CENTER-PIECE"))
		self.assertEqual(field_names.normalise("centerpiece"), "centerpiece")

	def test_an_alias_and_a_name_resolve_and_case_and_spaces_do_not_matter(self):
		self.assertEqual(field_names.find("wind machine", [MAIN])["resolved"], [WIND])
		self.assertEqual(field_names.find("mc centerpiece", [MAIN])["resolved"], [CENTER])
		self.assertEqual(field_names.find("CENTER PIECE", [MAIN])["resolved"], [CENTER])

	def test_bing_block_is_a_named_group_of_both(self):
		found = field_names.find("bing block", [MAIN])
		self.assertEqual((found["resolved"], found["group"]), (sorted([CENTER, WIND]), "Bing Block"))

	def test_ambiguity_is_candidates_never_a_pick(self):
		found = field_names.find("Center Piece")  # MCP: both companies — two different blocks
		self.assertEqual(found["resolved"], [])
		self.assertEqual({c["field"] for c in found["candidates"]}, {CENTER, "Pearls - OT"})
		with self.assertRaises(ValueError):
			field_names.resolve_one("Bing Block", [MAIN])  # a group is not one block

	def test_the_phone_sees_only_its_companies(self):
		self.be()
		self.assertEqual(mobile_api.find_fields(query="Pearls")["resolved"], [])
		self.assertEqual(mobile_api.find_fields(query="center piece")["resolved"], [CENTER])


class DefineWithoutCode(FieldCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_rename_field=1, allow_set_field_aliases=1, allow_set_field_acreage=1,
		               allow_create_task_on_field=1, allow_create_farm_task=1)

	def test_rename_keeps_the_old_name_as_an_alias(self):
		data = self.tool_data("rename_field", {"field": "Wind Mecine", "new_name": "Wind Machine"})
		self.assertEqual(data["field_name"], "Wind Machine")
		self.assertIn("Wind Mecine", data["aliases"])
		self.assertEqual(field_names.find("wind mecine", [MAIN])["resolved"], [WIND])

	def test_aliases_add_and_remove(self):
		data = self.tool_data("set_field_aliases", {"field": CENTER, "add": "Centerpiece", "remove": "Bing Block"})
		self.assertEqual(data["aliases"], ["Center Piece", "Centerpiece"])

	def test_acreage_from_the_outline_is_logged_and_manual_needs_a_reason(self):
		data = self.tool_data("set_field_acreage", {"field": "mc centerpiece"})
		# The acreage IS the measured outline (re-measured on save where shapely is installed).
		self.assertEqual((data["was"], data["source"]), (12.0, "Outline"))
		self.assertEqual(data["acreage"], round(float(data["area_computed_acres"]), 2))
		notes = [r["content"] for r in STORE.rows("Comment") if r.get("reference_name") == CENTER]
		self.assertTrue(any(f"12.0 to {data['acreage']}" in n for n in notes), notes)
		self.assertIn("reason", self.tool_error("set_field_acreage", {"field": WIND, "source": "Manual", "acres": 14}))
		data = self.tool_data("set_field_acreage", {"field": WIND, "source": "Manual", "acres": 14,
		                                            "reason": "Lease schedule acreage"})
		self.assertEqual((data["acreage"], data["source"]), (14.0, "Manual"))

	def test_a_task_on_a_field_by_name_and_an_ambiguous_one_refused(self):
		self.assertIn("more than one", self.tool_error("create_task_on_field", {"field": "Bing Block", "task_name": "x"}))


class History(FieldCase):
	def test_everything_tied_to_the_block_newest_first(self):
		data = history.field_history(CENTER, include_sensitive=True, limit=50)
		kinds = [e["kind"] for e in data["events"]]
		self.assertTrue({"task", "ipm", "phenology", "harvest", "cost"} <= set(kinds), kinds)
		self.assertNotIn("FT-OPEN", [e["docname"] for e in data["events"]], "open tasks are on the card")
		whens = [e["when"] for e in data["events"]]
		self.assertEqual(whens, sorted(whens, reverse=True))
		hit = next(e for e in data["events"] if e["docname"] == "OBS-1")
		self.assertIn("THRESHOLD EXCEEDED", hit["detail"])

	def test_a_worker_never_sees_costs_and_filters_and_paging_work(self):
		self.be()
		data = mobile_api.get_field_history(field="center piece")
		self.assertNotIn("cost", data["kinds"])
		self.assertFalse(data["sensitive_included"])
		only = mobile_api.get_field_history(field="center piece", types="harvest")
		self.assertEqual({e["kind"] for e in only["events"]}, {"harvest"})
		page = history.field_history(CENTER, limit=2, include_sensitive=True)
		self.assertTrue(page["more"])
		older = history.field_history(CENTER, limit=50, before=page["before"], include_sensitive=True)
		self.assertTrue(all(e["when"] < page["before"] for e in older["events"]))
		self.assertEqual(history.field_history(CENTER, season=2025)["events"], [])

	def test_a_manager_sees_costs(self):
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		self.assertIn("cost", mobile_api.get_field_history(field=CENTER)["kinds"])


class TheCard(FieldCase):
	def test_hazards_inside_the_outline_and_open_tasks(self):
		self.be()
		card = mobile_api.get_field_card(field="center piece")
		self.assertEqual([h["name"] for h in card["hazards"]], ["MC-V1"])
		self.assertEqual([t["name"] for t in card["open_tasks"]], ["FT-OPEN"])
		self.assertEqual(card["aliases"], ["Center Piece", "Bing Block"])
		self.assertTrue(card["has_boundary"])

	def test_point_in_polygon(self):
		shape = json.dumps({"type": "Polygon", "coordinates": [CENTER_RING]})
		self.assertTrue(field_card.contains(shape, 45.5852, -121.2310))
		self.assertFalse(field_card.contains(shape, 45.5824, -121.1884))


class Contract(FieldCase):
	def setUp(self):
		super().setUp()
		from unittest import mock

		patcher = mock.patch.object(frappe.utils, "today", lambda: "2026-10-07")  # the card's season window
		patcher.start()
		self.addCleanup(patcher.stop)

	def check(self, name, value):
		before = v262.HERE
		v262.HERE = HERE
		try:
			v262.check(self, name, value)
		finally:
			v262.HERE = before

	def test_the_three_answers(self):
		self.be()
		self.check("find_fields", mobile_api.find_fields(query="bing block"))
		self.check("get_field_card", mobile_api.get_field_card(field="center piece"))
		self.check("get_field_history", mobile_api.get_field_history(field="center piece"))


class OutlineByDefault(FieldCase):
	def test_a_new_block_without_acreage_follows_its_outline_and_one_with_a_figure_keeps_it(self):
		if not geo.available():
			self.skipTest("shapely / h3 not installed — the outline is stored, not measured")
		ring = json.dumps({"type": "Polygon", "coordinates": [CENTER_RING]})
		doc = frappe.get_doc({"doctype": "Field", "field_name": "New Block", "parcel": "Mill Creek - MC",
		                      "boundary_geojson": ring})
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.acreage_source, "Outline")
		self.assertAlmostEqual(float(doc.acreage), round(float(doc.area_computed_acres), 2))
		typed = frappe.get_doc({"doctype": "Field", "field_name": "FSA Block", "parcel": "Mill Creek - MC", "acreage": 11.5})
		typed.insert(ignore_permissions=True)
		self.assertEqual((typed.acreage_source, float(typed.acreage)), ("Manual", 11.5))
		self.assertIn("created", typed.acreage_override_reason)

	def test_a_gross_redraw_on_an_outline_block_is_refused_unless_meant(self):
		if not geo.available():
			self.skipTest("shapely / h3 not installed")
		self.configure(enabled=1, allow_set_field_boundary=1, allow_set_field_acreage=1)
		self.tool_data("set_field_acreage", {"field": "mc centerpiece"})  # now Outline, ~11 ac
		tiny = json.dumps({"type": "Polygon", "coordinates": [[[-121.2308, 45.5850], [-121.2306, 45.5850],
		                                                      [-121.2306, 45.5852], [-121.2308, 45.5852],
		                                                      [-121.2308, 45.5850]]]})
		self.assertIn("different piece of ground", self.tool_error("set_field_boundary", {"field": CENTER, "boundary_geojson": tiny}))
		data = self.tool_data("set_field_boundary", {"field": CENTER, "boundary_geojson": tiny, "replace_acreage": True})
		self.assertTrue(data["changed"])
		self.assertLess(float(frappe.db.get_value("Field", CENTER, "acreage")), 1)


class AddTaskHereOffline(FieldCase):
	def test_a_retried_template_task_is_not_raised_twice(self):
		"""App 0.54.0 queues "Add task here" offline; the route takes client_request_id through request_receipts
		(the decorator claim / start / complete already use and test), so a retry replays the first answer."""
		import inspect

		from erpnext_mcp import request_receipts

		source = inspect.getsource(mobile_api)
		self.assertIn('@request_receipts.idempotent("create_task_from_template")', source)
		self.assertIn("client_request_id", inspect.signature(inspect.unwrap(mobile_api.create_task_from_template)).parameters)
		calls = []

		@request_receipts.idempotent("create_task_from_template")
		def fake(user, client_request_id=None):
			calls.append(1)
			return {"name": "FT-NEW"}

		first = fake(WORKER, client_request_id="op-1")
		STORE.commit()
		again = fake(WORKER, client_request_id="op-1")
		self.assertEqual((len(calls), again.get("replayed"), again["name"]), (1, True, first["name"]))
