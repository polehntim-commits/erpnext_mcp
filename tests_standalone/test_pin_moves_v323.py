# SPDX-License-Identifier: MIT
"""v0.323.0 — move a pin by hand (Desk map, phone, MCP) and record who did it and when.

Tim: "We used to have a way of moving a pin in the Desk." Asset Register kept its draggable pin; Housing Unit, Farm
Task and Field Pin get one. A task's Work Spot overrides its blocks' centre. Every move is stamped.
"""

import frappe

from erpnext_mcp import pin_moves, task_locations
from erpnext_mcp.api import mobile as mobile_api

from . import test_housing as th
from . import test_task_locations_v323 as tl
from .fixtures import MAIN
from .harness import STORE, set_roles

GPS = {"gps_latitude": 45.5852, "gps_longitude": -121.2310}


class AHousingUnitPinIsStamped(th.HousingTestCase):
	def setUp(self):
		super().setUp()
		# frappe.flags lives for one request on a site; in one test process an earlier phone call's mark lingers.
		frappe.flags.pin_source = None

	def test_moving_the_cabin_stamps_it_and_its_tag_follows(self):
		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-1&2", **GPS)
		self.tool_data("update_housing_unit", {"unit": unit["name"], "gps_latitude": 45.5860,
		                                       "gps_longitude": -121.2320})
		row = STORE.get_raw("Housing Unit", unit["name"])
		self.assertTrue(row.get("position_set_by"))
		self.assertTrue(row.get("position_set_at"))
		self.assertIn(row.get("position_source"), ("MCP", "Desk"))
		self.assertAlmostEqual(STORE.get_raw("Asset Register", unit["name"])["gps_latitude"], 45.5860)

	def test_moving_the_cabins_tag_moves_the_cabin(self):
		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-3&4", **GPS)
		set_roles("Administrator", ["System Manager"])
		moved = pin_moves.move("asset", unit["name"], 45.5870, -121.2330, reason="was on the wrong cabin")
		self.assertEqual(moved["latitude"], 45.5870)
		# A plain save that moves a fixed asset is still refused — the drag goes through Move asset.
		with self.assertRaises(Exception):
			pin_moves.move("asset", unit["name"], 45.5880, -121.2340)  # no reason on a placed asset
		self.assertAlmostEqual(STORE.get_raw("Housing Unit", unit["name"])["gps_latitude"], 45.5870)
		self.assertTrue(STORE.get_raw("Asset Register", unit["name"]).get("position_set_at"))

	def test_a_new_unit_is_not_a_move(self):
		self.a_parcel()
		unit = self.a_unit(unit_name="Fresh", **GPS)
		self.assertFalse(STORE.get_raw("Housing Unit", unit["name"]).get("position_set_at"))


class TheTaskWorkSpot(tl.BingCase):
	def test_a_manager_moves_it_and_it_overrides_the_centre(self):
		task_locations.set_locations("FT-BING", ["MC-CP", "MC-WM"])
		centre = task_locations.describe("FT-BING")["map_point"]
		set_roles("Administrator", ["System Manager"])
		moved = pin_moves.move("task", "FT-BING", 45.58, -121.22)
		self.assertEqual((moved["latitude"], moved["longitude"], moved["basis"]), (45.58, -121.22, "pin"))
		self.assertNotEqual(centre["latitude"], 45.58)
		spot = STORE.get_raw("Field Pin", frappe.db.get_value("Field Pin", {"source": "FT-BING"}, "name"))
		self.assertTrue(spot.get("moved_at"))
		cleared = pin_moves.move("task", "FT-BING", None, None, clear=True)
		self.assertEqual(cleared["basis"], "area-weighted centroid")

	def test_a_field_worker_may_not(self):
		set_roles(tl.tf.WORKER, ["Field Worker"])
		with self.assertRaises(pin_moves.MoveError):
			pin_moves.move("task", "FT-BING", 45.58, -121.22, user=tl.tf.WORKER)

	def test_only_a_task_pin_clears(self):
		with self.assertRaises(pin_moves.MoveError):
			pin_moves.move("asset", "nope", None, None, clear=True)

	def test_the_phone_route_for_a_foreman(self):
		set_roles(tl.tf.WORKER, ["Field Worker", "Foreman"])
		self.be()
		data = mobile_api.move_task_pin(task="FT-BING", latitude=45.5851, longitude=-121.2301,
		                                client_request_id="pin-1")
		self.assertEqual(data["basis"], "pin")
		again = mobile_api.move_task_pin(task="FT-BING", latitude=45.5851, longitude=-121.2301,
		                                 client_request_id="pin-1")
		self.assertEqual((again["latitude"], again["longitude"]), (45.5851, -121.2301), "a replay is the same answer")
		self.assertEqual(len(frappe.db.get_all("Field Pin", filters={"source": "FT-BING"})), 1)

	def test_the_phone_route_refuses_a_picker(self):
		set_roles(tl.tf.WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.move_task_pin(task="FT-BING", latitude=45.5851, longitude=-121.2301)


class AFieldPinIsStamped(tl.BingCase):
	def test_moving_it_records_who_and_when(self):
		name = frappe.get_doc({"doctype": "Field Pin", "pin_kind": "Entrance", "latitude": 45.58, "longitude": -121.23,
		                       "field": tl.tf.CENTER, "company": MAIN}).insert(ignore_permissions=True).name
		self.assertFalse(STORE.get_raw("Field Pin", name).get("moved_at"))
		doc = frappe.get_doc("Field Pin", name)
		doc.latitude = 45.581
		doc.save(ignore_permissions=True)
		self.assertTrue(STORE.get_raw("Field Pin", name).get("moved_at"))
