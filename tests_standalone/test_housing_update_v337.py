# SPDX-License-Identifier: MIT
"""update_housing_unit's three small additions. v0.337.1 (the v0.337.0 rewrite was trimmed back to these, per Tim).

1. `AppendNotes` — a dated line added after what is there, never replacing it.
2. `DryRun` — every check runs, the diff is answered, nothing is written.
3. `RecordDates` — the inspection and detector dates are refused here; they move through their own records.

Neutral fixtures: no names from any live register.
"""

import frappe

from .harness import STORE
from .test_housing import HousingTestCase

PARCEL = "North Camp"
UNIT = "House A"


class Case(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel(PARCEL, acreage=12)
		self.unit = self.a_unit(
			UNIT,
			parcel=PARCEL,
			unit_type="Single-Family House",
			square_footage=1200,
			capacity=6,
			year_built=1990,
			condition="Good",
			notes="First note.",
		)["name"]

	def update(self, **args):
		return self.tool_data("update_housing_unit", {"unit": self.unit, **args})

	def stored(self, field):
		return frappe.db.get_value("Housing Unit", self.unit, field)


class AppendNotes(Case):
	def test_a_dated_line_is_added_after_what_is_there(self):
		self.update(append_notes="Roof patched.")
		self.update(append_notes="Gutters cleared.")
		lines = self.stored("notes").split("\n")
		self.assertEqual(lines[0], "First note.")
		self.assertTrue(lines[1].startswith("[") and lines[1].endswith("] Roof patched."))
		self.assertTrue(lines[2].endswith("] Gutters cleared."))

	def test_on_an_empty_note_it_is_the_first_line(self):
		self.update(notes="")
		self.update(append_notes="Only line.")
		self.assertTrue(self.stored("notes").endswith("] Only line."))
		self.assertNotIn("\n", self.stored("notes"))

	def test_notes_still_replaces(self):
		self.update(notes="Replaced.")
		self.assertEqual(self.stored("notes"), "Replaced.")


class DryRun(Case):
	def test_the_diff_is_answered_and_nothing_is_written(self):
		before = dict(STORE.get_raw("Housing Unit", self.unit))
		data = self.update(year_built=2004, square_footage=1600, append_notes="Appraised.", dry_run=True)
		self.assertTrue(data["dry_run"])
		self.assertEqual(data["diff"]["year_built"], {"before": 1990, "after": 2004})
		self.assertEqual(data["diff"]["square_footage"]["after"], 1600)
		self.assertEqual(data["diff"]["max_occupants_per_or_law"], {"before": 24, "after": 32}, "1600 / 50")
		self.assertTrue(data["diff"]["notes"]["after"].endswith("] Appraised."))
		self.assertEqual(STORE.get_raw("Housing Unit", self.unit), before)

	def test_a_dry_run_still_refuses_what_a_write_would(self):
		error = self.tool_error(
			"update_housing_unit", {"unit": self.unit, "gps_latitude": 45.6, "dry_run": True}
		)
		self.assertIn("gps", error.lower())

	def test_a_value_only_dry_run_writes_nothing(self):
		data = self.update(replacement_value=250000, dry_run=True)
		self.assertEqual(data["diff"]["value"]["after"], {"replacement_value": 250000})

	def test_without_dry_run_it_writes(self):
		self.update(year_built=2004)
		self.assertEqual(self.stored("year_built"), 2004)


class RecordDates(Case):
	def test_each_is_refused_and_nothing_changes(self):
		for key, record in (
			("last_habitability_inspection", "create_housing_inspection"),
			("smoke_detector_last_test", "create_detector_test"),
			("co_detector_last_test", "create_detector_test"),
		):
			with self.subTest(field=key):
				error = self.tool_error(
					"update_housing_unit", {"unit": self.unit, key: "2026-06-15", "capacity": 5}
				)
				self.assertIn(record, error)
				self.assertIn("Nothing was changed", error)
				self.assertEqual(self.stored("capacity"), 6, "the rest of the call is not applied either")

	def test_create_housing_unit_still_takes_them(self):
		data = self.a_unit("House B", parcel=PARCEL, last_habitability_inspection="2026-06-15")
		self.assertEqual(data["last_habitability_inspection"], "2026-06-15")
