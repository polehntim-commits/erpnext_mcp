# SPDX-License-Identifier: MIT
"""`Satellite Metric` and `Satellite Backfill Cursor` — where the imagery's residue lands.

Cycle 2 closed with the owner's correction: the sidecar's data is test data
except for the MRL book and the satellite history, and the satellite history had
nowhere to go. These two doctypes are that somewhere. Four claims.

1. **THE CONTROLLER'S RANGE TABLE AND `satellite.METRICS` CANNOT DRIFT.**
   `TheyAgreeWithTheModule`. Both hold the physical range of every index, in two
   files, for good reasons — the controller must not import the tools layer on
   every save — and two copies of a constant is exactly the shape that goes
   quietly wrong. SAVI runs -1.5 to 1.5 and everything else -1 to 1, so a
   controller using the wrong table files a legitimate SAVI reading as a bad
   decode.

2. **THE DOCTYPES OFFER EXACTLY THE INDICES THE APP CAN COMPUTE.**
   `TheyAgreeWithTheModule`. A Select option for an index `satellite.evalscript`
   would refuse is a row somebody can create and nothing can ever fill.

3. **THE JSON IS STRUCTURALLY SOUND.** `TheStructure`. `field_order` matching
   the fields it names is a `bench migrate` failure when it does not, and a Link
   whose `options` names no doctype is a field that silently validates nothing.

4. **THE CONTROLLERS DEFEND WHAT THE MIGRATION ALSO DEFENDS.** `TheControllers`.
   Range, duplicates, an inverted window. Note what this file does NOT claim:
   these hooks do not run in the standalone double at all — `STUB_CONTROLLERS`
   covers ERPNext's own doctypes and nothing of this app's — so the tests below
   call `validate` directly against a constructed document. That is real
   coverage of the logic and it is NOT evidence that Frappe will invoke it; only
   a bench run proves that.
"""

import json
import os
import unittest

from erpnext_mcp import satellite

from .harness import STORE, frappe  # noqa: F401 - importing installs the frappe double

DOCTYPE_ROOT = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
	"erpnext_mcp",
	"erpnext_mcp",
	"doctype",
)

METRIC = "Satellite Metric"
CURSOR = "Satellite Backfill Cursor"


def load(folder: str) -> dict:
	with open(os.path.join(DOCTYPE_ROOT, folder, f"{folder}.json")) as handle:
		return json.load(handle)


def fields_of(doc: dict) -> dict:
	return {row["fieldname"]: row for row in doc["fields"]}


class TheStructure(unittest.TestCase):
	def setUp(self):
		self.metric = load("satellite_metric")
		self.cursor = load("satellite_backfill_cursor")

	def test_field_order_names_exactly_the_fields_that_exist(self):
		"""A mismatch here is a `bench migrate` failure, and it is the one thing
		about a hand-written doctype JSON that nothing else catches."""
		for doc in (self.metric, self.cursor):
			self.assertEqual(doc["field_order"], [row["fieldname"] for row in doc["fields"]], doc["name"])

	def test_they_belong_to_this_app(self):
		for doc in (self.metric, self.cursor):
			self.assertEqual(doc["module"], "ERPNext MCP")
			self.assertEqual(doc["doctype"], "DocType")
			self.assertTrue(doc["permissions"])

	def test_every_link_names_a_doctype(self):
		"""A Link whose `options` is blank validates nothing and accepts any
		string, which is a foreign key that is not one."""
		for doc in (self.metric, self.cursor):
			for row in doc["fields"]:
				if row["fieldtype"] == "Link":
					self.assertTrue(row.get("options"), f"{doc['name']}.{row['fieldname']}")

	def test_the_block_link_is_required_on_both(self):
		"""A satellite row with no block is a number with no ground, and a cursor
		with no block records nothing."""
		for doc in (self.metric, self.cursor):
			self.assertEqual(fields_of(doc)["field"].get("reqd"), 1, doc["name"])
			self.assertEqual(fields_of(doc)["field"]["options"], "Field", doc["name"])

	def test_the_metric_requires_a_reading_and_a_time(self):
		fields = fields_of(self.metric)
		for name in ("value", "timestamp", "metric_type"):
			self.assertEqual(fields[name].get("reqd"), 1, name)

	def test_the_indexed_value_is_read_only_because_it_is_derived(self):
		"""Two fields that can disagree will. The raw value is the input and the
		index is a function of it, so only one of them is typeable."""
		self.assertEqual(fields_of(self.metric)["indexed_value"].get("read_only"), 1)

	def test_the_cursor_holds_no_measurement(self):
		"""Its whole value is bookkeeping about downloads. A reading on it would
		be a second, competing series that nothing updates."""
		suspicious = {"value", "indexed_value", "reading", "mean"}
		self.assertEqual(suspicious & set(fields_of(self.cursor)), set())

	def test_the_json_is_written_the_way_frappe_writes_it(self):
		"""Alphabetical keys with `naming_rule` last — matching the app's other
		doctype files. A file in a different shape is reformatted by the first
		`bench migrate` and the next diff is noise nobody can read."""
		for folder in ("satellite_metric", "satellite_backfill_cursor"):
			path = os.path.join(DOCTYPE_ROOT, folder, f"{folder}.json")
			with open(path) as handle:
				raw = handle.read()
			keys = list(json.loads(raw).keys())
			self.assertEqual(keys[-1], "naming_rule", folder)
			self.assertEqual(keys[:-1], sorted(keys[:-1]), folder)
			self.assertTrue(raw.endswith("\n"), folder)

	def test_the_controller_file_and_the_package_marker_are_both_there(self):
		"""A doctype folder without `__init__.py` is one Frappe cannot import."""
		for folder in ("satellite_metric", "satellite_backfill_cursor"):
			for filename in ("__init__.py", f"{folder}.py", f"{folder}.json"):
				self.assertTrue(os.path.isfile(os.path.join(DOCTYPE_ROOT, folder, filename)), filename)


class TheyAgreeWithTheModule(unittest.TestCase):
	def controller_ranges(self):
		from erpnext_mcp.erpnext_mcp.doctype.satellite_metric.satellite_metric import RAW_RANGES

		return RAW_RANGES

	def test_the_controllers_range_table_matches_satellite_metrics(self):
		"""Two copies of one constant, in two files, for a stated reason. This is
		the guard that keeps them the same: SAVI runs -1.5 to 1.5 and everything
		else -1 to 1, so a controller on the wrong table files a legitimate SAVI
		reading as a bad decode."""
		expected = {key: tuple(value["raw_range"]) for key, value in satellite.METRICS.items()}
		self.assertEqual(self.controller_ranges(), expected)

	def test_both_doctypes_offer_exactly_the_indices_the_app_can_compute(self):
		"""An option for an index `satellite.evalscript` would refuse is a row
		somebody can create and nothing can ever fill."""
		for folder in ("satellite_metric", "satellite_backfill_cursor"):
			options = fields_of(load(folder))["metric_type"]["options"].split("\n")
			self.assertEqual(sorted(options), sorted(satellite.METRICS), folder)

	def test_every_offered_index_actually_builds_an_evalscript(self):
		for option in fields_of(load("satellite_metric"))["metric_type"]["options"].split("\n"):
			self.assertIn("evaluatePixel", satellite.evalscript(option))

	def test_the_controllers_index_arithmetic_matches_the_modules(self):
		"""`indexed_value` on a stored row and `satellite.to_index` on the same
		number have to be the same figure, or a series read through one and
		compared through the other drifts."""
		ranges = self.controller_ranges()
		for metric, (low, high) in ranges.items():
			for raw in (low, 0.0, high, (low + high) / 2):
				derived = round((raw - low) / (high - low) * 100.0, 3)
				self.assertAlmostEqual(
					derived, round(satellite.to_index(raw, metric), 3), places=3, msg=metric
				)

	def test_the_harness_knows_both_doctypes(self):
		"""Without an APP_DOCTYPES entry `compat.doctype_exists` answers False and
		anything guarded on them refuses with 'run bench migrate' — a refusal that
		looks like a registry bug and is not one."""
		from .harness import APP_DOCTYPES

		self.assertEqual(APP_DOCTYPES.get(METRIC), "satellite_metric")
		self.assertEqual(APP_DOCTYPES.get(CURSOR), "satellite_backfill_cursor")

	def test_the_folder_each_doctype_names_is_the_folder_that_exists(self):
		from .harness import APP_DOCTYPES

		for name in (METRIC, CURSOR):
			self.assertTrue(os.path.isdir(os.path.join(DOCTYPE_ROOT, APP_DOCTYPES[name])), name)


class TheControllers(unittest.TestCase):
	"""Direct calls into `validate`. See the module docstring: these hooks do NOT
	run in the standalone double, so this is coverage of the logic and not proof
	that Frappe invokes it."""

	#: The double's clock is pinned at 2026-07-24, so a fixture stamped later than
	#: that trips the future-acquisition guard and every test in this class fails
	#: for a reason that has nothing to do with what it is testing.
	PAST = "2026-07-01 00:00:00"
	FUTURE = "2026-08-01 00:00:00"

	def metric(self, **kw):
		from erpnext_mcp.erpnext_mcp.doctype.satellite_metric.satellite_metric import SatelliteMetric

		return SatelliteMetric(
			{
				"doctype": METRIC,
				"field": "Block A4",
				"metric_type": "ndvi",
				"timestamp": self.PAST,
				"value": 0.71,
				**kw,
			}
		)

	def cursor(self, **kw):
		from erpnext_mcp.erpnext_mcp.doctype.satellite_backfill_cursor.satellite_backfill_cursor import (
			SatelliteBackfillCursor,
		)

		return SatelliteBackfillCursor({"doctype": CURSOR, "field": "Block A4", "metric_type": "ndvi", **kw})

	def test_a_good_reading_derives_its_index(self):
		doc = self.metric()
		doc.validate()
		self.assertAlmostEqual(doc.indexed_value, 85.5, places=3)

	def test_a_reading_outside_the_range_is_refused_rather_than_clamped(self):
		"""87 in a column that runs -1 to 1 is a decode with the wrong scale.
		Clamped, it would file as a very healthy block."""
		with self.assertRaises(Exception) as caught:
			self.metric(value=87.0).validate()
		self.assertIn("outside the range", str(caught.exception))

	def test_savi_keeps_its_own_wider_range(self):
		doc = self.metric(metric_type="savi", value=1.2)
		doc.validate()
		self.assertAlmostEqual(doc.indexed_value, 90.0, places=3)

	def test_a_savi_reading_that_would_be_illegal_ndvi_is_accepted(self):
		"""The negative control for the range table: 1.2 is a bad decode as NDVI
		and an ordinary reading as SAVI."""
		with self.assertRaises(Exception):
			self.metric(metric_type="ndvi", value=1.2).validate()

	def test_a_reading_with_no_block_or_no_time_or_no_value_is_refused(self):
		for missing in ("field", "timestamp", "value"):
			with self.assertRaises(Exception) as caught:
				self.metric(**{missing: None}).validate()
			self.assertTrue(str(caught.exception), missing)

	def test_a_pass_dated_after_the_server_is_refused(self):
		"""A satellite cannot have flown over yet, and the row would sort above
		every real reading for ever."""
		with self.assertRaises(Exception) as caught:
			self.metric(timestamp=self.FUTURE).validate()
		self.assertIn("ahead of the server", str(caught.exception))

	def test_an_inverted_cursor_window_is_refused(self):
		"""A scheduler reading it fetches either nothing for ever or everything
		every night, and neither failure announces itself."""
		with self.assertRaises(Exception) as caught:
			self.cursor(oldest_fetched="2026-08-01 00:00:00", newest_fetched="2025-01-01 00:00:00").validate()
		self.assertIn("cannot exist", str(caught.exception))

	def test_a_cursor_window_in_the_right_order_passes(self):
		doc = self.cursor(oldest_fetched="2025-01-01 00:00:00", newest_fetched="2026-07-01 00:00:00")
		doc.validate()
		self.assertEqual(doc.metric_type, "ndvi")

	def test_a_half_open_cursor_is_fine(self):
		"""A block whose backfill has started but never reached the archive's end
		has one bound and not the other."""
		self.cursor(newest_fetched="2026-07-01 00:00:00").validate()
		self.cursor(oldest_fetched="2025-01-01 00:00:00").validate()

	def test_a_cursor_with_no_block_is_refused(self):
		with self.assertRaises(Exception):
			self.cursor(field=None).validate()


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
