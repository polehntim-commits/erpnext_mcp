# SPDX-License-Identifier: MIT
"""Fixed assets do not move by accident (v0.214.0).

docs/design/badge_photo_and_fixed_assets.md, Part B.
"""

import datetime

import frappe

from erpnext_mcp import asset_moves, asset_types, registry
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.patches import flag_fixed_asset_types

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase

WM = "40-WM-SE"
TRACTOR = "MC-Tractor-01"
HOME = (45.58125, -121.1849598)
ROAD = (45.5806332, -121.1828221)
ASSET = "Asset Register"
LOG = "Asset State Log"
ON = {
	f"allow_{name}": 1
	for name in (
		"register_asset",
		"update_registered_asset",
		"move_asset",
		"undo_asset_move",
		"update_asset_type",
	)
}


class MoveCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		from .test_api_mobile import ON as BASE

		self.configure(enabled=1, **{**BASE, **ON})
		self.be("Administrator")
		asset_types.seed()
		self.tool_data(
			"register_asset",
			{
				"name": WM,
				"asset_type": "Wind Machine",
				"company": MAIN,
				"gps_latitude": HOME[0],
				"gps_longitude": HOME[1],
			},
		)
		self.tool_data("register_asset", {"name": TRACTOR, "asset_type": "Tractor", "company": MAIN})
		STORE.commit()

	def foreman(self):
		set_roles(WORKER, ["Field Worker", "Foreman"])
		return self.be()

	def picker(self):
		set_roles(WORKER, ["Field Worker"])
		return self.be()

	def position(self, name):
		row = STORE.get_raw(ASSET, name)
		return (round(float(row.get("gps_latitude") or 0), 7), round(float(row.get("gps_longitude") or 0), 7))

	def moves(self, name):
		return [
			r for r in STORE.rows(LOG) if r["asset_name"] == name and r["action"] in ("Moved", "Move undone")
		]


class WhatIsFixed(MoveCase):
	def test_the_shipped_types_are_seeded_fixed_or_mobile(self):
		for name in (
			"Wind Machine",
			"Irrigation Valve",
			"Water Source",
			"Storage",
			"Cabin",
			"House",
			"Block",
		):
			self.assertTrue(asset_moves.is_fixed(name), name)
		for name in ("Tractor", "Sprayer", "Vehicle", "Implement", "General", "Something New"):
			self.assertFalse(asset_moves.is_fixed(name), name)

	def test_the_patch_flags_types_a_site_already_had_once(self):
		frappe.db.set_value("Farm Asset Type", "Wind Machine", "fixed_location", 0)
		self.assertFalse(asset_moves.is_fixed("Wind Machine"))
		self.assertIn("Wind Machine", flag_fixed_asset_types.run())
		self.assertTrue(asset_moves.is_fixed("Wind Machine"))
		self.assertEqual(flag_fixed_asset_types.run(), [])
		flag_fixed_asset_types.execute()

	def test_a_farm_decides_and_the_type_says_so(self):
		out = self.tool_data("update_asset_type", {"name": "General", "fixed_location": True})
		self.assertTrue(out["fixed_location"])
		self.assertTrue(asset_moves.is_fixed("General"))
		types = {t["name"]: t for t in self.tool_data("list_asset_types", {})["asset_types"]}
		self.assertTrue(types["Wind Machine"]["fixed_location"])
		self.assertFalse(types["Tractor"]["fixed_location"])


class Scanning(MoveCase):
	def test_a_scan_never_moves_a_fixed_asset_and_records_where_the_phone_was(self):
		self.foreman()
		answer = mobile_api.scan_asset(asset_name=WM, gps_lat=ROAD[0], gps_lon=ROAD[1])
		self.assertEqual(self.position(WM), HOME)
		self.assertEqual((answer["fixed_location"], answer["position_updated"]), (True, False))
		self.assertAlmostEqual(answer["scan_location"]["latitude"], ROAD[0], places=6)
		row = STORE.get_raw(ASSET, WM)
		self.assertAlmostEqual(row["last_scan_latitude"], ROAD[0], places=6)
		self.assertAlmostEqual(row["last_scan_longitude"], ROAD[1], places=6)
		self.assertEqual(self.moves(WM), [])
		mobile_api.universal_scan(content=WM, gps_lat=ROAD[0], gps_lon=ROAD[1])
		self.assertEqual(self.position(WM), HOME)

	def test_a_fixed_asset_with_no_position_is_not_placed_by_a_scan_either(self):
		self.be("Administrator")
		self.tool_data(
			"register_asset", {"name": "MC-Well-01", "asset_type": "Water Source", "company": MAIN}
		)
		STORE.commit()
		self.foreman()
		mobile_api.scan_asset(asset_name="MC-Well-01", gps_lat=ROAD[0], gps_lon=ROAD[1])
		self.assertEqual(self.position("MC-Well-01"), (0.0, 0.0))

	def test_a_mobile_asset_follows_the_scan_and_a_real_move_is_history(self):
		self.foreman()
		first = mobile_api.scan_asset(asset_name=TRACTOR, gps_lat=HOME[0], gps_lon=HOME[1])
		self.assertTrue(first["position_updated"])
		self.assertEqual(self.moves(TRACTOR), [], "a first sighting is not a move")
		mobile_api.scan_asset(asset_name=TRACTOR, gps_lat=HOME[0] + 0.00001, gps_lon=HOME[1])
		self.assertEqual(self.moves(TRACTOR), [], "a metre is the GPS, not the tractor")
		mobile_api.scan_asset(asset_name=TRACTOR, gps_lat=ROAD[0], gps_lon=ROAD[1])
		self.assertEqual(self.position(TRACTOR), ROAD)
		rows = self.moves(TRACTOR)
		self.assertEqual(len(rows), 1)
		self.assertGreater(rows[0]["distance_m"], 150)
		self.assertAlmostEqual(rows[0]["from_latitude"], HOME[0] + 0.00001, places=6)


class Moving(MoveCase):
	def test_a_move_needs_a_reason_and_records_where_it_was(self):
		self.foreman()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.move_asset(asset_name=WM, gps_latitude=ROAD[0], gps_longitude=ROAD[1])
		self.assertIn("reason is required", str(caught.exception))
		self.assertEqual(self.position(WM), HOME)

		answer = mobile_api.move_asset(
			asset_name=WM,
			gps_latitude=ROAD[0],
			gps_longitude=ROAD[1],
			reason="Reinstalled on the new pad",
			accuracy_m=4,
			client_request_id="m-1",
		)
		self.assertTrue(answer["moved"])
		self.assertTrue(170 < answer["distance_m"] < 190, answer["distance_m"])
		self.assertEqual(self.position(WM), ROAD)
		self.assertTrue(answer["undo_until"])
		row = self.moves(WM)[0]
		self.assertEqual(
			(row["action"], row["performed_by"], row["client_request_id"]), ("Moved", WORKER, "m-1")
		)
		self.assertAlmostEqual(row["from_latitude"], HOME[0], places=6)
		self.assertAlmostEqual(row["gps_latitude"], ROAD[0], places=6)
		self.assertIn("Reinstalled on the new pad", row["notes"])
		self.assertEqual(row["from_state"], row["to_state"], "a move is not a state change")

		# The same request again changes nothing and is an answer.
		again = mobile_api.move_asset(
			asset_name=WM, gps_latitude=ROAD[0], gps_longitude=ROAD[1], reason="Reinstalled on the new pad"
		)
		self.assertFalse(again["moved"])
		self.assertEqual(len(self.moves(WM)), 1)

		self.be("Administrator")
		history = self.tool_data("list_asset_state_history", {"asset_name": WM})["events"]
		moved = next(e for e in history if e["action"] == "Moved")
		self.assertAlmostEqual(moved["from_latitude"], HOME[0], places=6)
		self.assertTrue(170 < moved["distance_m"] < 190)

	def test_a_first_placement_needs_no_reason(self):
		self.foreman()
		answer = mobile_api.move_asset(asset_name=TRACTOR, gps_latitude=HOME[0], gps_longitude=HOME[1])
		self.assertEqual((answer["moved"], answer["first_placement"]), (True, True))
		self.assertIsNone(answer["undo_until"], "there is nowhere to put it back to")

	def test_a_picker_may_not_move_or_undo(self):
		self.picker()
		for call in (
			lambda: mobile_api.move_asset(
				asset_name=WM, gps_latitude=ROAD[0], gps_longitude=ROAD[1], reason="x"
			),
			lambda: mobile_api.undo_asset_move(asset_name=WM),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.assertEqual(self.position(WM), HOME)

	def test_nonsense_coordinates_and_another_entitys_asset_are_refused(self):
		self.foreman()
		with self.assertRaises(frappe.ValidationError):
			mobile_api.move_asset(asset_name=WM, gps_latitude=0, gps_longitude=0, reason="x")
		with self.assertRaises(frappe.ValidationError):
			mobile_api.move_asset(asset_name=WM, gps_latitude=95, gps_longitude=-121, reason="x")
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.move_asset(asset_name="NOWHERE", gps_latitude=45, gps_longitude=-121, reason="x")


class Undoing(MoveCase):
	def moved(self):
		self.foreman()
		mobile_api.move_asset(asset_name=WM, gps_latitude=ROAD[0], gps_longitude=ROAD[1], reason="Mistake")
		STORE.commit()

	def test_undo_puts_it_back_once_and_says_so_in_the_history(self):
		self.moved()
		detail = mobile_api.get_asset_detail(asset_name=WM)
		self.assertTrue(detail["last_move"]["can_undo"])
		answer = mobile_api.undo_asset_move(asset_name=WM)
		self.assertEqual(self.position(WM), HOME)
		self.assertEqual([r["action"] for r in self.moves(WM)].count("Move undone"), 1)
		self.assertEqual(answer["last_move"]["action"], "Move undone")
		STORE.commit()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.undo_asset_move(asset_name=WM)
		self.assertIn("already undone", str(caught.exception))

	def test_a_move_older_than_a_day_is_not_undone(self):
		self.moved()
		row = self.moves(WM)[0]
		old = str(frappe.utils.get_datetime(str(row["performed_at"])) - datetime.timedelta(hours=25))
		frappe.db.set_value(LOG, row["name"], "performed_at", old)
		STORE.commit()
		self.assertFalse(mobile_api.get_asset_detail(asset_name=WM)["last_move"]["can_undo"])
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.undo_asset_move(asset_name=WM)
		self.assertIn("more than 24 hours", str(caught.exception))
		self.assertEqual(self.position(WM), ROAD)


class TheServerRefusesEverythingElse(MoveCase):
	def test_saving_the_document_with_a_new_position_is_refused(self):
		doc = frappe.get_doc(ASSET, WM)
		doc.gps_latitude, doc.gps_longitude = ROAD
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save(ignore_permissions=True)
		self.assertIn("fixed location", str(caught.exception))
		# Anything else about it still saves, and so does a mobile asset's position.
		doc = frappe.get_doc(ASSET, WM)
		doc.description = "South-east wind machine"
		doc.save(ignore_permissions=True)
		tractor = frappe.get_doc(ASSET, TRACTOR)
		tractor.gps_latitude, tractor.gps_longitude = ROAD
		tractor.save(ignore_permissions=True)

	def test_update_registered_asset_needs_a_reason_and_writes_history(self):
		error = self.tool_error(
			"update_registered_asset", {"asset_name": WM, "gps_latitude": ROAD[0], "gps_longitude": ROAD[1]}
		)
		self.assertIn("Pass reason", error)
		self.assertEqual(self.position(WM), HOME)
		self.tool_data(
			"update_registered_asset",
			{
				"asset_name": WM,
				"gps_latitude": ROAD[0],
				"gps_longitude": ROAD[1],
				"reason": "Surveyed position",
			},
		)
		self.assertEqual(self.position(WM), ROAD)
		self.assertEqual(self.moves(WM)[0]["notes"], "Surveyed position")
		# A change that is not the position asks for nothing.
		self.tool_data("update_registered_asset", {"asset_name": WM, "description": "Wind machine, SE"})

	def test_the_tools_are_off_by_default_and_gated_over_mcp_too(self):
		self.assertIn("move_asset", registry.MUTATING_TOOLS)
		self.assertIn("undo_asset_move", registry.MUTATING_TOOLS)
		moved = self.tool_data(
			"move_asset",
			{"asset_name": WM, "gps_latitude": ROAD[0], "gps_longitude": ROAD[1], "reason": "Pad"},
		)
		self.assertTrue(moved["moved"])
		self.assertEqual(self.tool_data("undo_asset_move", {"asset_name": WM})["to"]["latitude"], HOME[0])
