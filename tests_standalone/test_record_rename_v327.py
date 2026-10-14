# SPDX-License-Identifier: MIT
"""v0.327.0 — rename through MCP (record action=rename), the cabin's tag with it; occupied kept in step; duplex rooms.

Tim, 2026-10-13: "Code tool please, via MCP all the way." The Mill Creek cabins were registered as MC-CABIN-6&6,
16&17, 18&19; the doors say 5&6, 16&16, 18&18. And MC-CABIN-6&6 / 1&2 were Occupied on the tag and not on the unit.
"""

import frappe

from erpnext_mcp import housing_assets, renames
from erpnext_mcp.tools import record_tool

from . import test_housing as th
from .harness import STORE

ON = {**th.ALL_ON, "allow_record": 1, "allow_update_document": 1, "allow_universal_scan": 1}


class RenameCase(th.HousingTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.a_parcel()

	def cabin(self, unit_name, **extra):
		return self.a_unit(unit_name=unit_name, **extra)["name"]

	def rename(self, **args):
		return self.tool_data("record", {"action": "rename", "type": "housing_unit", **args})


class ADryRunChangesNothing(RenameCase):
	def test_dry_run_is_the_default_and_names_the_pair(self):
		old = self.cabin("MC-CABIN-6&6")
		data = self.rename(name=old, new_name="MC-CABIN-5&6")
		self.assertTrue(data["dry_run"])
		step = data["steps"][0]
		self.assertEqual(step["new_name"], old.replace("6&6", "5&6"))
		self.assertEqual(step["partner"]["doctype"], "Asset Register")
		self.assertEqual(step["partner"]["new_name"], step["new_name"])
		self.assertTrue(frappe.db.exists("Housing Unit", old))

	def test_a_dry_run_needs_no_switch(self):
		self.configure(enabled=1, **{**ON, "allow_update_housing_unit": 0})
		old = self.cabin("MC-CABIN-6&6")
		self.assertTrue(self.rename(name=old, new_name="MC-CABIN-5&6")["dry_run"])
		self.assertIn("allow_update_housing_unit", self.tool_error(
			"record", {"action": "rename", "type": "housing_unit", "name": old, "new_name": "X", "dry_run": False}))


class TheUnitAndItsTagMoveTogether(RenameCase):
	def test_rename_moves_both_keeps_the_pair_and_the_old_name(self):
		old = self.cabin("MC-CABIN-6&6")
		self.tool_data("create_housing_assignment", {"unit": old, "employee_name": "Ana", "assigned_date": "2026-10-01"})
		data = self.rename(name=old, new_name="MC-CABIN-5&6", dry_run=False)
		new = data["renamed"][0]["to"]
		self.assertFalse(frappe.db.exists("Housing Unit", old))
		self.assertEqual(STORE.get_raw("Housing Unit", new)["unit_name"], "MC-CABIN-5&6")
		self.assertTrue(frappe.db.exists("Asset Register", new))
		self.assertFalse(frappe.db.exists("Asset Register", old))
		self.assertEqual(frappe.db.get_value("Housing Unit", new, "asset_tag"), new)
		self.assertIn(old, STORE.get_raw("Housing Unit", new)["old_names"])
		self.assertIn(old, STORE.get_raw("Asset Register", new)["old_names"])
		self.assertEqual(STORE.rows("Housing Assignment")[0]["unit"], new, "the people in it follow")
		self.assertEqual(renames.current_name(old), ("Housing Unit", new))

	def test_naming_the_tag_renames_the_unit_too(self):
		old = self.cabin("MC-CABIN-18&19")
		data = self.tool_data("record", {"action": "rename", "type": "asset_register", "name": old,
		                                 "new_name": old.replace("18&19", "18&18"), "dry_run": False})
		new = data["renamed"][0]["to"]
		self.assertTrue(frappe.db.exists("Housing Unit", new))
		self.assertEqual(STORE.get_raw("Housing Unit", new)["unit_name"], "MC-CABIN-18&18")

	def test_door_label_and_rooms_ride_along(self):
		old = self.cabin("MC-CABIN-16&17")
		data = self.rename(name=old, new_name="MC-CABIN-16&16", door_label="16&16", room_labels="16a, 16b",
		                   dry_run=False)
		new = data["renamed"][0]["to"]
		self.assertEqual(STORE.get_raw("Housing Unit", new)["room_labels"], "16a, 16b")
		self.assertEqual(STORE.get_raw("Asset Register", new)["door_label"], "16&16")


class NothingCollides(RenameCase):
	def test_a_taken_name_is_refused(self):
		a, b = self.cabin("MC-CABIN-1&2"), self.cabin("MC-CABIN-3&4")
		self.assertIn("already exists", self.tool_error(
			"record", {"action": "rename", "type": "housing_unit", "name": a, "new_name": "MC-CABIN-3&4"}))
		self.assertTrue(frappe.db.exists("Housing Unit", b))

	def test_a_chain_is_ordered_top_down(self):
		a, b = self.cabin("MC-CABIN-15&16"), self.cabin("MC-CABIN-16&17")
		data = self.rename(renames=[{"name": a, "new_name": "MC-CABIN-16&17"},
		                            {"name": b, "new_name": "MC-CABIN-16&18"}], dry_run=False)
		self.assertEqual([r["from"] for r in data["renamed"]], [b, a])
		self.assertEqual(STORE.get_raw("Housing Unit", a.replace("15&16", "16&17"))["unit_name"], "MC-CABIN-16&17")

	def test_a_swap_goes_through_a_temporary_name(self):
		a, b = self.cabin("MC-CABIN-1&2"), self.cabin("MC-CABIN-3&4")
		self.rename(renames=[{"name": a, "new_name": "MC-CABIN-3&4"}, {"name": b, "new_name": "MC-CABIN-1&2"}],
		            dry_run=False)
		self.assertEqual(STORE.get_raw("Housing Unit", a)["old_names"].splitlines(), [b])
		self.assertEqual(frappe.db.get_value("Housing Unit", a, "asset_tag"), a)
		self.assertFalse([n for n in STORE.tables.get("Housing Unit", {}) if "~renaming" in n])

	def test_money_and_hr_are_not_renamed_here(self):
		with self.assertRaises(renames.RenameError):
			renames.plan("Employee", [{"name": "EMP-001", "new_name": "x"}])


class APrintedTagIsFlagged(RenameCase):
	def test_needs_new_tag(self):
		old = self.cabin("MC-CABIN-9&10")
		STORE.seed("Card Print Job", [{"name": "CPJ-1", "job_type": "Asset Tag", "reference_doctype": "Asset Register",
		                               "reference_name": old, "status": "Printed", "printed_at": "2026-10-12 10:00:00"}])
		STORE.commit()
		data = self.rename(name=old, new_name="MC-CABIN-9&11")
		self.assertEqual(data["needs_new_tag"][0]["printed_tag"]["printed_at"], "2026-10-12")


class OccupiedAgrees(RenameCase):
	def test_a_new_tag_takes_the_units_answer_not_its_beds(self):
		unit = self.cabin("MC-CABIN-1&2", capacity=4)
		self.assertEqual(int(STORE.get_raw("Asset Register", unit).get("occupied") or 0), 0)

	def test_mismatch_is_reported_and_synced_from_a_chosen_source(self):
		unit = self.cabin("MC-CABIN-6&6")
		frappe.db.set_value("Asset Register", unit, "occupied", 1)
		STORE.commit()
		rows = self.tool_data("list_housing_units", {"mismatches_only": True})["units"]
		self.assertEqual([r["name"] for r in rows], [unit])
		dry = self.tool_data("update_housing_unit", {"unit": "*", "sync_occupancy_from": "unit"})
		self.assertEqual(dry["changed"], 1)
		self.assertEqual(int(STORE.get_raw("Asset Register", unit)["occupied"]), 1, "a dry run writes nothing")
		self.tool_data("update_housing_unit", {"unit": unit, "sync_occupancy_from": "unit", "dry_run": False})
		self.assertEqual(int(STORE.get_raw("Asset Register", unit)["occupied"]), 0)

	def test_saving_either_side_moves_the_other(self):
		unit = self.cabin("MC-CABIN-7&8")
		self.tool_data("update_housing_unit", {"unit": unit, "occupied": True})
		self.assertEqual(int(STORE.get_raw("Asset Register", unit)["occupied"]), 1)
		doc = frappe.get_doc("Asset Register", unit)
		doc.occupied = 0
		doc.save(ignore_permissions=True)
		self.assertEqual(int(STORE.get_raw("Housing Unit", unit)["occupied"]), 0)

	def test_assignments_as_the_source(self):
		unit = self.cabin("MC-CABIN-3&4")
		self.tool_data("create_housing_assignment", {"unit": unit, "employee_name": "Ana", "assigned_date": "2026-10-01"})
		frappe.db.set_value("Housing Unit", unit, "occupied", 0)
		frappe.db.set_value("Asset Register", unit, "occupied", 0)
		STORE.commit()
		result = housing_assets.sync_occupancy([unit], "assignments", dry_run=False)
		self.assertEqual(result["changes"][0]["after"], True)
		self.assertEqual(int(STORE.get_raw("Asset Register", unit)["occupied"]), 1)


class DuplexRooms(RenameCase):
	def test_an_assignment_names_one_of_the_rooms(self):
		unit = self.cabin("MC-CABIN-18&18")
		self.tool_data("update_housing_unit", {"unit": unit, "room_labels": "18a, 18b", "door_label": "18&18"})
		self.tool_data("create_housing_assignment", {"unit": unit, "employee_name": "Ana", "assigned_date": "2026-10-01",
		                                             "room": "18b"})
		self.assertIn("not one of them", self.tool_error("create_housing_assignment", {
			"unit": unit, "employee_name": "Luis", "assigned_date": "2026-10-01", "room": "18c",
			"allow_multi_occupancy": True}))
		got = self.tool_data("get_housing_unit", {"unit": unit})
		self.assertEqual(got.get("room_labels"), ["18a", "18b"])


class TheOldNameStillScans(RenameCase):
	def test_scanning_the_old_name_lands_on_the_renamed_cabin(self):
		old = self.cabin("MC-CABIN-6&6")
		new = self.rename(name=old, new_name="MC-CABIN-5&6", dry_run=False)["renamed"][0]["to"]
		data = self.tool_data("universal_scan", {"content": old})
		self.assertEqual(data["entity_name"], new)
		self.assertEqual(data["renamed_from"], old)
