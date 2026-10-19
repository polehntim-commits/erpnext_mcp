# SPDX-License-Identifier: MIT
"""update_housing_unit to Tim's spec: only what is passed, a rename, a dry run, a diff, refusals with reasons. v0.337.0.

FOUR CLAIMS.

1. `RenameAndChange` — MC-House-03 → MC-4409 with year_built / square footage / notes in one call: old_names kept,
   assignments follow, other units untouched, the lawful occupancy recomputed, the diff reported.
2. `DryRun` — everything checked, nothing written.
3. `Occupancy` — a typed override survives a square-footage change; the two warnings.
4. `Refusals` — each refused case answers a sentence naming why, and writes nothing.
"""

import frappe

from erpnext_mcp import renames

from .fixtures import ASSET_CATEGORY, MAIN, OTHER
from .harness import STORE
from .test_housing import HousingTestCase

HOUSE = "MC-House-03"


class Case(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.house = self.a_unit(
			HOUSE,
			unit_type="Single-Family House",
			square_footage=1200,
			capacity=6,
			year_built=1998,
			condition="Good",
			notes="old notes",
		)["name"]
		self.cabin = self.a_unit("MC-Cabin-01")["name"]

	def update(self, **args):
		return self.tool_data("update_housing_unit", {"unit": HOUSE, **args})

	def refused(self, **args):
		before = {doctype: dict(rows) for doctype, rows in STORE.tables.items() if doctype == "Housing Unit"}
		error = self.tool_error("update_housing_unit", {"unit": HOUSE, **args})
		self.assertIn("Nothing was changed", error)
		after = {doctype: dict(rows) for doctype, rows in STORE.tables.items() if doctype == "Housing Unit"}
		self.assertEqual(before.keys(), after.keys())
		return error


class RenameAndChange(Case):
	def test_the_spec_example(self):
		mine = self.an_assignment(unit=HOUSE, employee="Antony")["name"]
		theirs = self.an_assignment(unit="MC-Cabin-01", employee="Alex")["name"]
		data = self.update(new_unit_name="MC-4409", year_built=2004, square_footage=1600, notes="3-bd/1-ba")
		new = data["name"]
		self.assertEqual(new, "MC-4409 - MC")
		self.assertEqual(data["unit_name"], "MC-4409")
		self.assertEqual((data["year_built"], data["square_footage"]), (2004, 1600))
		self.assertEqual(data["lawful_occupancy"], 32, "1600 sq ft / 50")
		self.assertEqual(data["diff"]["year_built"], {"before": 1998, "after": 2004})
		self.assertEqual(data["diff"]["name"], {"before": self.house, "after": new})
		self.assertIn(self.house, frappe.db.get_value("Housing Unit", new, "old_names"))
		self.assertEqual(
			renames.current_name(self.house), ("Housing Unit", new), "the old name still resolves"
		)
		self.assertEqual(
			frappe.db.get_value("Housing Assignment", mine, "unit"), new, "its assignment followed"
		)
		self.assertEqual(
			frappe.db.get_value("Housing Assignment", theirs, "unit"), self.cabin, "others untouched"
		)
		self.assertFalse(frappe.db.exists("Housing Unit", self.house))

	def test_only_the_fields_passed_change(self):
		data = self.update(year_built=2004)
		self.assertEqual(set(data["diff"]), {"year_built"})
		row = frappe.db.get_value(
			"Housing Unit", self.house, ["capacity", "condition", "notes"], as_dict=True
		)
		self.assertEqual((row.capacity, row.condition, row.notes), (6, "Good", "old notes"))

	def test_append_notes_keeps_what_was_there(self):
		self.update(append_notes="Roof patched")
		notes = frappe.db.get_value("Housing Unit", self.house, "notes")
		self.assertTrue(notes.startswith("old notes\n["))
		self.assertTrue(notes.endswith("] Roof patched"))

	def test_the_diff_lands_on_the_timeline_and_in_the_log_summary(self):
		self.update(capacity=5)
		comments = [c for c in STORE.comments if c["name"] == self.house]
		self.assertTrue(any("capacity: 6 → 5" in c["text"] for c in comments))
		self.assertTrue(
			any("capacity: 6 → 5" in (r.get("result_summary") or "") for r in STORE.rows("MCP Action Log"))
		)


class DryRun(Case):
	def test_nothing_is_written(self):
		before = STORE.get_raw("Housing Unit", self.house).copy()
		data = self.update(new_unit_name="MC-4409", year_built=2004, square_footage=1600, dry_run=True)
		self.assertTrue(data["dry_run"])
		self.assertEqual(data["diff"]["year_built"], {"before": 1998, "after": 2004})
		self.assertEqual(data["rename"]["to"], "MC-4409 - MC")
		self.assertEqual(data["lawful_occupancy"], 32)
		self.assertEqual(STORE.get_raw("Housing Unit", self.house), before)
		self.assertFalse(frappe.db.exists("Housing Unit", "MC-4409 - MC"))


class Occupancy(Case):
	def test_a_typed_override_survives_a_square_footage_change(self):
		self.update(max_occupants_per_or_law=4)
		data = self.update(square_footage=1600)
		self.assertEqual(data["lawful_occupancy"], 4)
		self.assertNotIn("max_occupants_per_or_law", data["diff"])

	def test_a_computed_limit_is_recomputed(self):
		data = self.update(square_footage=1600)
		self.assertEqual(data["diff"]["max_occupants_per_or_law"], {"before": 24, "after": 32})

	def test_capacity_warnings(self):
		data = self.update(capacity=25)
		self.assertTrue(any("barracks" in w for w in data["warnings"]))
		self.assertTrue(any("above the lawful occupancy of 24" in w for w in data["warnings"]))


class Refusals(Case):
	def test_an_unknown_unit_lists_the_parcels_units(self):
		error = self.tool_error(
			"update_housing_unit", {"unit": "MC-House-99", "parcel": "Mill Creek", "capacity": 1}
		)
		self.assertIn("no Housing Unit called 'MC-House-99'", error)
		self.assertIn(HOUSE, error)
		self.assertIn("MC-Cabin-01", error)

	def test_a_name_already_on_the_parcel(self):
		self.assertIn("already has a unit called 'MC-Cabin-01'", self.refused(new_unit_name="MC-Cabin-01"))

	def test_a_type_nobody_sleeps_in_while_lived_in(self):
		self.an_assignment(unit=HOUSE, employee="Antony")
		for unit_type in ("Toilet-Shower", "Kitchen", "Bath House", "Barn", "Shop"):
			with self.subTest(unit_type=unit_type):
				error = self.refused(unit_type=unit_type)
				self.assertIn("current assignment", error)
				self.assertIn("Antony", error)
		self.assertEqual(self.update(unit_type="Cabin")["unit_type"], "Cabin", "a dwelling type is fine")

	def test_uninhabitable_while_lived_in_needs_force_and_then_lists_them(self):
		self.an_assignment(unit=HOUSE, employee="Antony")
		self.assertIn("force_uninhabitable", self.refused(condition="Uninhabitable"))
		data = self.update(condition="Uninhabitable", force_uninhabitable=True)
		self.assertEqual(data["condition"], "Uninhabitable")
		self.assertTrue(any("Antony" in w for w in data["warnings"]))

	def test_uninhabitable_when_empty_is_fine(self):
		self.assertEqual(self.update(condition="Uninhabitable")["condition"], "Uninhabitable")

	def test_an_asset_on_another_companys_books_or_already_used(self):
		STORE.seed(
			"Asset",
			[
				{
					"name": "ACC-ASS-7001",
					"asset_name": "Other",
					"company": OTHER,
					"asset_category": ASSET_CATEGORY,
					"gross_purchase_amount": 1,
					"docstatus": 1,
				},
				{
					"name": "ACC-ASS-7002",
					"asset_name": "Cabin",
					"company": MAIN,
					"asset_category": ASSET_CATEGORY,
					"gross_purchase_amount": 1,
					"docstatus": 1,
				},
			],
		)
		STORE.commit()
		self.assertIn("same books", self.refused(related_asset="ACC-ASS-7001"))
		self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "related_asset": "ACC-ASS-7002"})
		self.assertIn("already carries", self.refused(related_asset="ACC-ASS-7002"))

	def test_half_a_gps_pair(self):
		self.assertIn("gps", self.refused(gps_latitude=45.6).lower())

	def test_inspection_and_detector_dates(self):
		for key in ("last_habitability_inspection", "smoke_detector_last_test", "co_detector_last_test"):
			with self.subTest(field=key):
				self.assertIn("moves only when", self.refused(**{key: "2026-06-15"}))

	def test_a_different_parcel(self):
		self.a_parcel("Green Camp", acreage=19.87)
		self.assertIn("cannot move between parcels", self.refused(parcel="Green Camp", capacity=5))
		self.assertEqual(
			self.update(parcel="Mill Creek", capacity=5)["capacity"], 5, "its own parcel narrows only"
		)

	def test_unit_name_points_at_new_unit_name(self):
		self.assertIn("new_unit_name", self.refused(unit_name="X"))
