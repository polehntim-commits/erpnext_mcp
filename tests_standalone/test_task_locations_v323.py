# SPDX-License-Identifier: MIT
"""v0.323.0 — a Farm Task covers one or more places, with an optional explicit pin.

Tim's Bing removal tasks were filed against the parcel because a task held one location, so the phone pinned them at
the parcel's centre. Now `locations` (a child list) and a Work Spot pin place a task over MC-CP + MC-WM, set through a
validated path rather than the Updatable Fields whitelist.
"""

import frappe

from erpnext_mcp import task_locations
from erpnext_mcp.api import shape
from erpnext_mcp.tools import dispatch

from . import test_dispatch as td
from . import test_field_self_service as tf
from .harness import STORE

ON = {**td.ALL_ON, "allow_record": 1, "allow_create_farm_task": 1}


class BingCase(tf.FieldCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		frappe.db.set_value("Field", tf.CENTER, "block_ticker", "MC-CP")
		frappe.db.set_value("Field", tf.WIND, "block_ticker", "MC-WM")
		STORE.seed("Farm Task", [{"name": "FT-BING", "task_name": "Remove Bing trees", "task_type": "Maintenance",
		                          "state": "Available", "location_doctype": "Parcel", "location": "Mill Creek - MC",
		                          "company": tf.MAIN, "evidence_required": '{"findings_text": true}'}])
		STORE.commit()

	def place(self, **body):
		return self.tool_data("record", {"action": "update", "type": "farm_task", "name": "FT-BING", "body": body})


class SettingTheBlocks(BingCase):
	def test_tickers_set_both_blocks_and_the_pin_moves_off_the_parcel(self):
		data = self.place(locations=["MC-CP", "MC-WM"])
		self.assertEqual(data["via"], "task_locations")
		self.assertEqual([r["location"] for r in data["locations"]], [tf.CENTER, tf.WIND])
		self.assertEqual(data["before"][0]["location"], "Mill Creek - MC")
		row = frappe.db.get_value("Farm Task", "FT-BING", ["location_doctype", "location", "location_count"],
		                          as_dict=True)
		self.assertEqual((row.location_doctype, row.location, row.location_count), ("Field", tf.CENTER, 2))
		phone = shape.task(dispatch._describe_task(dispatch.task_row("FT-BING")))
		lat = task_locations._geometry("Field", tf.CENTER)["boundary_centroid_lat"]
		wind = task_locations._geometry("Field", tf.WIND)["boundary_centroid_lat"]
		self.assertTrue(min(lat, wind) <= phone["latitude"] <= max(lat, wind), "between the two blocks")
		self.assertEqual(len(phone["locations"]), 2)

	def test_the_named_group_brings_both(self):
		data = self.place(locations=["Bing Block"])
		self.assertEqual(sorted(r["location"] for r in data["locations"]), sorted([tf.CENTER, tf.WIND]))

	def test_a_single_task_read_carries_the_union_outline(self):
		self.place(locations=["MC-CP", "MC-WM"])
		task = self.tool_data("get_farm_task", {"task": "FT-BING"})
		self.assertEqual(task["outline_geojson"]["type"], "MultiPolygon")
		self.assertEqual(len(task["outline_geojson"]["coordinates"]), 2)

	def test_an_explicit_pin_wins_and_can_be_cleared(self):
		self.place(locations=["MC-CP", "MC-WM"], work_spot={"latitude": 45.5850, "longitude": -121.2300})
		phone = shape.task(dispatch._describe_task(dispatch.task_row("FT-BING")))
		self.assertAlmostEqual(phone["latitude"], 45.5850)
		self.assertEqual(frappe.db.get_value("Field Pin", {"source": "FT-BING"}, "pin_kind"), "Work Spot")
		self.place(clear_work_spot=True)
		self.assertIsNone(task_locations.work_spot("FT-BING"))

	def test_a_scan_of_either_block_finds_the_task(self):
		self.place(locations=["MC-CP", "MC-WM"])
		self.assertIn("FT-BING", task_locations.tasks_at("Field", tf.WIND))
		self.assertIn("FT-BING", task_locations.tasks_at("Field", tf.CENTER))


class WhatIsRefused(BingCase):
	def test_another_companys_block(self):
		message = self.tool_error("record", {"action": "update", "type": "farm_task", "name": "FT-BING",
		                                     "body": {"locations": ["Pearls - OT"]}})
		self.assertIn("belongs to", message)
		self.assertEqual(frappe.db.get_value("Farm Task", "FT-BING", "location"), "Mill Creek - MC", "nothing changed")

	def test_an_unknown_place_and_a_closed_task(self):
		self.assertIn("nothing called", self.tool_error("record", {"action": "update", "type": "farm_task",
		                                                          "name": "FT-BING", "body": {"locations": ["Atlantis"]}}))
		frappe.db.set_value("Farm Task", "FT-BING", "state", "Completed")
		self.assertIn("history", self.tool_error("record", {"action": "update", "type": "farm_task", "name": "FT-BING",
		                                                   "body": {"locations": ["MC-CP"]}}))

	def test_it_needs_the_create_switch_not_the_whitelist(self):
		self.configure(enabled=1, allow_record=1)
		self.assertIn("allow_create_farm_task", self.tool_error(
			"record", {"action": "update", "type": "farm_task", "name": "FT-BING", "body": {"locations": ["MC-CP"]}}))


class CreatingAMultiBlockTask(BingCase):
	def test_create_farm_task_with_locations(self):
		made = self.tool_data("create_farm_task", {"task_name": "Pull Bing stumps", "task_type": "Maintenance",
		                                           "evidence_required": {"findings_text": True},
		                                           "company": tf.MAIN, "locations": ["MC-CP", "MC-WM"]})
		self.assertEqual(made["location"], tf.CENTER)
		self.assertEqual(frappe.db.get_value("Farm Task", made["name"], "location_count"), 2)

	def test_a_bad_location_creates_nothing(self):
		before = len(STORE.rows("Farm Task"))
		self.tool_error("create_farm_task", {"task_name": "X", "task_type": "Maintenance",
		                                     "evidence_required": {"findings_text": True}, "company": tf.MAIN,
		                                     "locations": ["MC-CP", "Atlantis"]})
		self.assertEqual(len(STORE.rows("Farm Task")), before)


class AOnePlaceTaskIsUnchanged(BingCase):
	def test_no_new_keys_on_an_ordinary_task(self):
		phone = shape.task(dispatch._describe_task(dispatch.task_row("FT-OPEN")))
		self.assertNotIn("locations", phone)
		self.assertNotIn("work_spot", phone)
