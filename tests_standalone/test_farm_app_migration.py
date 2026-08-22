# SPDX-License-Identifier: MIT
"""`farm_app_migration` — the two things worth carrying out of the sidecar.

v0.120.0 shipped a ten-table migration; the owner then said the sidecar's data is
TEST data except for the MRL reference book and the satellite history, so
v0.121.0 narrowed it to those. These run against a REAL SQLite database built in
`setUpClass`, in the farm_app's own column names, so extract and transform are
exercised end to end without a bench. Six claims.

1. **A LIMIT IN THE WRONG UNIT IS NEVER WRITTEN AS ppm.** `TheUnitTrap`. mg/kg
   IS ppm and converts silently; ppb is a thousandth, and a ppb figure in a ppm
   column is a limit a thousand times too loose — the direction that clears a
   shipment it should have held. An unrecognised unit is refused, not guessed.

2. **A LIMIT WITH NO CROP OR NO MARKET IS REFUSED, IN THE DRY RUN.** `TheNameJoin`.
   Both are `reqd=1` on `MRL Record`, so warning and migrating anyway — which an
   earlier draft did — turns a readable plan into a mid-run insert failure after
   part of the batch has landed.

3. **THE NAME JOIN IS EXACT, AND ITS MISSES ARE NAMED.** `TheNameJoin`. No fuzzy
   matching: every "cherries → Cherry" rule is right four times and wrong once,
   and the once is a residue limit on the wrong fruit.

4. **THE WHOLE SATELLITE SERIES CROSSES, NOT THE NEWEST ROW.**
   `TheSatelliteHistory`. v0.120.0 folded only the latest NDVI onto `Field`
   because there was nowhere else to put it. `Satellite Metric` is that
   somewhere.

5. **THE BACKFILL CURSOR IS THE POINT OF THE SATELLITE HALF.**
   `TheSatelliteHistory`. It holds no measurement; losing it means paying the
   provider again for months already bought, and that cost shows up on an
   invoice rather than in the data.

6. **RUNNING IT TWICE CREATES NOTHING THE SECOND TIME.** `RunItTwice`.
"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest

from erpnext_mcp import farm_app_migration as migration

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
import migrate_farm_app as script  # noqa: E402

#: The farm_app's own schema for the four tables this carries, plus the two
#: reference tables the name join reads. Column names copied from the sidecar's
#: `models.py` — a fixture that renamed one would test nothing.
SCHEMA = """
CREATE TABLE field (id INTEGER PRIMARY KEY, name TEXT, ndvi_path TEXT);
CREATE TABLE commodity (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE country (id INTEGER PRIMARY KEY, name TEXT, iso_alpha2 TEXT);
CREATE TABLE maximum_residue (id INTEGER PRIMARY KEY, active_ingredient TEXT, commodity_id INTEGER,
	country_id INTEGER, mrl_value REAL, mrl_unit TEXT, status TEXT, source TEXT,
	source_reference TEXT, effective_date TEXT, review_date TEXT, notes TEXT);
CREATE TABLE mrl_research_session (id INTEGER PRIMARY KEY, active_ingredient TEXT,
	commodity_id INTEGER, country_id INTEGER, research_result TEXT, ai_response_raw TEXT,
	review_notes TEXT, status TEXT);
CREATE TABLE field_satellite_metric (id INTEGER PRIMARY KEY, field_id INTEGER, metric_type TEXT,
	value REAL, indexed_value REAL, timestamp TEXT, source TEXT, h3_index TEXT);
CREATE TABLE satellite_backfill_cursor (id INTEGER PRIMARY KEY, field_id INTEGER,
	oldest_fetched TEXT, newest_fetched TEXT, last_run TEXT, backfill_complete INTEGER);
"""

ROWS = """
INSERT INTO field VALUES (1,'Block A4','/app/instance/ndvi/1.tif');
INSERT INTO field VALUES (2,'Block B4',NULL);
INSERT INTO commodity VALUES (1,'Cherry'),(2,'Pluot');
INSERT INTO country VALUES (1,'European Union','EU'),(2,'Narnia','NA');
INSERT INTO maximum_residue VALUES (1,'spinetoram',1,1,0.5,'mg/kg','registered','EU Pesticides DB',
	'Reg 396/2005','2025-01-01','2027-01-01','checked against the register');
INSERT INTO maximum_residue VALUES (2,'lambda-cyhalothrin',1,1,300,'ppb','restricted','Codex',
	'CXL 2019','2024-01-01',NULL,NULL);
INSERT INTO maximum_residue VALUES (3,'mystery',1,1,1.0,'grains/bushel','registered','x',NULL,NULL,NULL,NULL);
INSERT INTO maximum_residue VALUES (4,'orphan',2,2,1.0,'ppm','registered','x',NULL,NULL,NULL,NULL);
INSERT INTO maximum_residue VALUES (5,'',1,1,1.0,'ppm','registered','x',NULL,NULL,NULL,NULL);
INSERT INTO maximum_residue VALUES (6,'nolimit',1,1,NULL,'ppm','registered','x',NULL,NULL,NULL,NULL);
INSERT INTO mrl_research_session VALUES (1,'fludioxonil',1,1,
	'{"mrl_value":5.0,"source_reference":"EU register","confidence":"high","substance_status":"registered"}',
	'raw model output','looked right','completed');
INSERT INTO mrl_research_session VALUES (2,'nothingol',1,1,'{"mrl_value":"NOT_FOUND"}','raw',NULL,'completed');
INSERT INTO field_satellite_metric VALUES (1,1,'ndvi',0.71,NULL,'2026-08-01 00:00:00','sentinel-2',NULL);
INSERT INTO field_satellite_metric VALUES (2,1,'ndvi',0.65,NULL,'2026-07-25 00:00:00','sentinel-2',NULL);
INSERT INTO field_satellite_metric VALUES (3,1,'moisture',NULL,15.0,'2026-08-20 01:05:08','copernicus',NULL);
INSERT INTO field_satellite_metric VALUES (4,1,'ndvi',87.0,NULL,'2026-08-02 00:00:00','bad-decode',NULL);
INSERT INTO field_satellite_metric VALUES (5,1,'ndvi',0.60,NULL,NULL,'sentinel-2',NULL);
INSERT INTO field_satellite_metric VALUES (6,99,'ndvi',0.60,NULL,'2026-08-03 00:00:00','sentinel-2',NULL);
INSERT INTO satellite_backfill_cursor VALUES (1,1,'2025-01-01 00:00:00','2026-08-01 00:00:00',
	'2026-08-21 03:00:00',0);
INSERT INTO satellite_backfill_cursor VALUES (2,2,NULL,NULL,NULL,0);
"""

#: What the site holds before the run. Blocks come from `import_farm_app_fields`
#: (by external id); the Crop and the Market are matched by NAME.
SITE = {"Crop": {"cherry": "Cherry"}, "Market": {"european union": "EU Fresh"}}


def lookup(doctype: str, name: str):
	"""The injected site query — exact, casefolded, the same rule the real one uses."""
	return SITE.get(doctype, {}).get(str(name or "").strip().casefold())


class MigrationTestCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.directory = tempfile.mkdtemp(prefix="farm-app-migration-")
		cls.path = os.path.join(cls.directory, "farm_app.db")
		connection = sqlite3.connect(cls.path)
		connection.executescript(SCHEMA + ROWS)
		connection.commit()
		connection.close()

	def setUp(self):
		self.connection = migration.open_database(self.path)
		self.addCleanup(self.connection.close)

	def links(self):
		seeds = migration.seed_links_by_name(self.connection, lookup)
		seeds.pop("_unmatched", None)
		return migration.Links({"field": {"1": "Block A4", "2": "Block B4"}, **seeds})

	def run_migration(self, loader=None, context=None, **kwargs):
		loader = loader if loader is not None else migration.DryRunLoader()
		report = migration.migrate(
			self.connection,
			loader,
			self.links(),
			{"company": "Orchard Meadow, LLC", **(context or {})},
			**kwargs,
		)
		return report, loader

	def table(self, report, name) -> dict:
		return next(entry for entry in report["tables"] if entry["table"] == name)

	def documents(self, loader, doctype) -> list:
		return [row["doc"] for row in loader.inserted if row["doctype"] == doctype]

	def why(self, report, table, row_id) -> str:
		refusals = {row["id"]: row["why"] for row in self.table(report, table)["refusals"]}
		self.assertIn(row_id, refusals, f"row {row_id} of {table} was not refused")
		return refusals[row_id]


class WhatItCarries(MigrationTestCase):
	def test_it_carries_exactly_four_tables(self):
		"""The narrowing IS the release. A spec list that grew back would migrate
		test data into the system of record again."""
		self.assertEqual(
			[spec.table for spec in migration.SPECS],
			[
				"maximum_residue",
				"mrl_research_session",
				"field_satellite_metric",
				"satellite_backfill_cursor",
			],
		)

	def test_the_general_tables_are_gone(self):
		for table in (
			"field",
			"iot_device",
			"iot_reading",
			"strategic_plan",
			"objective",
			"market_participant",
			"acquisition_target",
			"competitive_move",
		):
			self.assertNotIn(table, migration.SPEC_BY_TABLE, table)

	def test_a_limit_carries_its_provenance(self):
		_report, loader = self.run_migration(only=["maximum_residue"])
		record = next(d for d in self.documents(loader, "MRL Record") if d["chemical"] == "spinetoram")
		self.assertEqual(record["mrl_ppm"], 0.5)
		self.assertEqual(record["crop"], "Cherry")
		self.assertEqual(record["market"], "EU Fresh")
		self.assertEqual(record["source"], "EU Pesticides DB")
		self.assertEqual(record["research_notes"], "Reg 396/2005")
		self.assertEqual(record["substance_status"], "Registered")
		self.assertEqual(record["expiry_date"], "2027-01-01")

	def test_a_research_session_that_reached_a_limit_still_crosses(self):
		_report, loader = self.run_migration(only=["mrl_research_session"])
		record = self.documents(loader, "MRL Record")[0]
		self.assertEqual(record["chemical"], "fludioxonil")
		self.assertEqual(record["mrl_ppm"], 5.0)
		self.assertEqual(record["research_response"], "raw model output")

	def test_a_session_that_found_nothing_never_becomes_a_limit_of_zero(self):
		"""Zero is a real limit and the strictest one there is."""
		report, loader = self.run_migration(only=["mrl_research_session"])
		self.assertIn("no MRL value", self.why(report, "mrl_research_session", 2))
		for record in self.documents(loader, "MRL Record"):
			self.assertGreater(record["mrl_ppm"], 0)

	def test_a_row_naming_no_substance_or_no_limit_is_refused(self):
		report, _loader = self.run_migration(only=["maximum_residue"])
		self.assertIn("names no substance", self.why(report, "maximum_residue", 5))
		self.assertIn("no limit is not a limit", self.why(report, "maximum_residue", 6))


class TheUnitTrap(MigrationTestCase):
	def crop_and_market(self):
		"""A links map for a row that names commodity 1 and country 1.

		`Links.resolve` answers None for a None id by design, so a fixture row has
		to carry the ids — keying the map on the string "None" resolves nothing
		and the refusal that follows looks like a unit failure.
		"""
		return migration.Links({"commodity": {"1": "Cherry"}, "country": {"1": "EU Fresh"}})

	def test_mg_per_kg_is_ppm_and_converts_silently(self):
		report, loader = self.run_migration(only=["maximum_residue"])
		record = next(d for d in self.documents(loader, "MRL Record") if d["chemical"] == "spinetoram")
		self.assertEqual(record["mrl_ppm"], 0.5)
		warnings = [row["warning"] for row in self.table(report, "maximum_residue")["warnings"]]
		self.assertEqual([text for text in warnings if "mg/kg" in text], [])

	def test_ppb_is_a_thousandth_and_the_conversion_is_announced(self):
		"""300 ppb is 0.3 ppm. Written straight into a ppm column it would be a
		limit a thousand times too loose — the direction that clears a shipment
		it should have held."""
		report, loader = self.run_migration(only=["maximum_residue"])
		record = next(
			d for d in self.documents(loader, "MRL Record") if d["chemical"] == "lambda-cyhalothrin"
		)
		self.assertEqual(record["mrl_ppm"], 0.3)
		warnings = [row["warning"] for row in self.table(report, "maximum_residue")["warnings"]]
		self.assertTrue([text for text in warnings if "converted from ppb" in text])

	def test_a_unit_nobody_recognises_is_refused_and_not_assumed_to_be_ppm(self):
		report, _loader = self.run_migration(only=["maximum_residue"])
		reason = self.why(report, "maximum_residue", 3)
		self.assertIn("grains/bushel", reason)
		self.assertIn("factor of a thousand", reason)

	def test_a_blank_unit_reads_as_ppm_which_is_what_the_column_says(self):
		row = {"active_ingredient": "x", "mrl_value": 2.0, "mrl_unit": "", "commodity_id": 1, "country_id": 1}
		doc = migration.build_maximum_residue(row, self.crop_and_market(), {}, [])
		self.assertEqual(doc["mrl_ppm"], 2.0)

	def test_the_spellings_of_a_unit_that_the_data_actually_holds(self):
		for unit, expected in (("mg/kg", 2.0), ("MG/KG", 2.0), ("ppm", 2.0), ("ug/kg", 0.002)):
			doc = migration.build_maximum_residue(
				{
					"active_ingredient": "x",
					"mrl_value": 2.0,
					"mrl_unit": unit,
					"commodity_id": 1,
					"country_id": 1,
				},
				self.crop_and_market(),
				{},
				[],
			)
			self.assertEqual(doc["mrl_ppm"], expected, unit)


class TheNameJoin(MigrationTestCase):
	def test_the_join_is_exact_and_casefolded(self):
		seeds = migration.seed_links_by_name(self.connection, lookup)
		self.assertEqual(seeds["commodity"], {"1": "Cherry"})
		self.assertEqual(seeds["country"], {"1": "EU Fresh"})

	def test_every_miss_is_named_so_somebody_can_act_on_it(self):
		seeds = migration.seed_links_by_name(self.connection, lookup)
		self.assertEqual(seeds["_unmatched"], {"commodity": ["Pluot"], "country": ["Narnia"]})

	def test_nothing_is_matched_fuzzily(self):
		"""Every 'cherries → Cherry' rule is right four times and wrong once, and
		the once is a residue limit on the wrong fruit."""
		self.assertIsNone(lookup("Crop", "Cherries"))
		self.assertIsNone(lookup("Crop", "Sweet Cherry"))
		self.assertEqual(lookup("Crop", "  CHERRY "), "Cherry")

	def test_a_limit_whose_crop_and_market_are_both_missing_is_refused(self):
		report, _loader = self.run_migration(only=["maximum_residue"])
		reason = self.why(report, "maximum_residue", 4)
		self.assertIn("commodity id 2", reason)
		self.assertIn("country id 2", reason)

	def test_the_refusal_explains_it_would_have_failed_at_insert_anyway(self):
		"""Both links are `reqd=1` on MRL Record, so warning and migrating turns
		a readable dry run into a mid-run failure after part of the batch lands."""
		report, _loader = self.run_migration(only=["maximum_residue"])
		reason = self.why(report, "maximum_residue", 4)
		self.assertIn("requires", reason)
		self.assertIn("refused at insert", reason)
		self.assertIn("re-running picks up only what was missing", reason)

	def test_a_refused_limit_creates_nothing_at_all(self):
		_report, loader = self.run_migration(only=["maximum_residue"])
		self.assertNotIn("orphan", [d["chemical"] for d in self.documents(loader, "MRL Record")])

	def test_a_table_the_export_does_not_have_seeds_nothing_rather_than_raising(self):
		empty = os.path.join(self.directory, "bare.db")
		connection = sqlite3.connect(empty)
		connection.execute("CREATE TABLE field (id INTEGER PRIMARY KEY, name TEXT)")
		connection.commit()
		connection.close()
		seeds = migration.seed_links_by_name(migration.open_database(empty), lookup)
		self.assertEqual(seeds["_unmatched"], {})


class TheSatelliteHistory(MigrationTestCase):
	def setUp(self):
		super().setUp()
		self.report, self.loader = self.run_migration()

	def test_the_whole_series_crosses_and_not_just_the_newest_row(self):
		"""v0.120.0 folded only the latest NDVI onto `Field` because there was
		nowhere else to put it. This doctype is that somewhere."""
		metrics = self.documents(self.loader, "Satellite Metric")
		stamps = sorted(row["timestamp"] for row in metrics)
		self.assertEqual(len(metrics), 3)
		self.assertIn("2026-07-25 00:00:00", stamps)
		self.assertIn("2026-08-01 00:00:00", stamps)

	def test_the_farm_apps_own_index_name_still_resolves(self):
		"""`moisture` is what the sidecar called NDMI, and it is in the rows."""
		row = next(r for r in self.documents(self.loader, "Satellite Metric") if r["source"] == "copernicus")
		self.assertEqual(row["metric_type"], "ndmi")

	def test_a_row_that_stored_only_an_index_is_un_indexed_through_the_same_table(self):
		"""15.0 on the 0-100 scale is -0.7 raw, and the storage side rescales it
		back to 15.0 — so the two never disagree by a rounding step."""
		row = next(r for r in self.documents(self.loader, "Satellite Metric") if r["source"] == "copernicus")
		self.assertAlmostEqual(row["value"], -0.7, places=6)

	def test_a_reading_outside_its_index_range_is_a_bad_decode_and_is_refused(self):
		reason = self.why(self.report, "field_satellite_metric", 4)
		self.assertIn("outside the range", reason)
		self.assertIn("wrong scale", reason)

	def test_a_pass_with_no_timestamp_cannot_be_placed_in_a_series(self):
		self.assertIn("cannot be placed in a series", self.why(self.report, "field_satellite_metric", 5))

	def test_a_metric_whose_block_was_never_migrated_is_refused(self):
		self.assertIn("has not been migrated", self.why(self.report, "field_satellite_metric", 6))

	def test_the_cursor_carries_the_window_already_paid_for(self):
		cursor = self.documents(self.loader, "Satellite Backfill Cursor")[0]
		self.assertEqual(cursor["field"], "Block A4")
		self.assertEqual(cursor["oldest_fetched"], "2025-01-01 00:00:00")
		self.assertEqual(cursor["newest_fetched"], "2026-08-01 00:00:00")
		self.assertEqual(cursor["last_run"], "2026-08-21 03:00:00")

	def test_the_cursor_is_recorded_against_the_index_that_was_actually_fetched(self):
		"""The sidecar kept one cursor per block with no index column and every
		pull it made was NDVI. A cursor claiming to cover indices nobody fetched
		would suppress the walks that have not happened."""
		self.assertEqual(self.documents(self.loader, "Satellite Backfill Cursor")[0]["metric_type"], "ndvi")

	def test_a_cursor_with_neither_end_recorded_says_nothing_was_fetched(self):
		self.assertIn("nothing was fetched", self.why(self.report, "satellite_backfill_cursor", 2))


class TheRasterManifest(MigrationTestCase):
	def test_it_reports_what_the_sidecar_still_points_at(self):
		manifest = migration.raster_manifest(self.connection)
		self.assertEqual(len(manifest["rasters"]), 1)
		self.assertEqual(manifest["rasters"][0]["path"], "/app/instance/ndvi/1.tif")
		self.assertEqual(manifest["rasters"][0]["block"], "Block A4")

	def test_a_block_with_no_cached_raster_is_not_listed(self):
		blocks = {row["block"] for row in migration.raster_manifest(self.connection)["rasters"]}
		self.assertNotIn("Block B4", blocks)

	def test_a_path_that_is_not_readable_from_here_is_counted_as_missing(self):
		"""The path is inside the farm_app container, so from anywhere else it is
		normally absent — and saying so is the whole job."""
		manifest = migration.raster_manifest(self.connection)
		self.assertEqual(manifest["missing"], 1)
		self.assertEqual(manifest["rasters"][0]["readable_at"], "")

	def test_a_root_that_does_hold_the_file_reports_its_size(self):
		root = tempfile.mkdtemp()
		os.makedirs(os.path.join(root, "app/instance/ndvi"))
		with open(os.path.join(root, "app/instance/ndvi/1.tif"), "wb") as handle:
			handle.write(b"\0" * 2048)
		manifest = migration.raster_manifest(self.connection, root)
		self.assertEqual(manifest["missing"], 0)
		self.assertEqual(manifest["total_bytes"], 2048)

	def test_it_never_copies_anything(self):
		"""Moving megabytes across a container boundary is a `docker cp` an
		operator does with their own hands and their own disk."""
		root = tempfile.mkdtemp()
		migration.raster_manifest(self.connection, root)
		self.assertEqual(os.listdir(root), [])


class RunItTwice(MigrationTestCase):
	def test_the_second_run_creates_nothing(self):
		first, loader = self.run_migration()
		existing = {}
		for index, row in enumerate(loader.inserted, start=1):
			existing.setdefault(row["doctype"], []).append(
				{**row["doc"], "name": f"{row['doctype']}-new-{index}"}
			)
		second, again = self.run_migration(loader=migration.DryRunLoader(existing))
		self.assertEqual(again.inserted, [])
		self.assertEqual(second["created"], 0)
		self.assertEqual(second["already_present"], first["created"])

	def test_nothing_is_ever_updated(self):
		"""The sidecar is being retired, not synchronised."""
		first, loader = self.run_migration()
		self.assertEqual(loader.updated, [])
		self.assertEqual(first["updated"], 0)

	def test_an_existing_limit_is_left_exactly_as_it_is(self):
		existing = {
			"MRL Record": [
				{
					"chemical": "spinetoram",
					"crop": "Cherry",
					"market": "EU Fresh",
					"name": "MRL-00001",
					"mrl_ppm": 999,
				}
			]
		}
		report, loader = self.run_migration(loader=migration.DryRunLoader(existing), only=["maximum_residue"])
		self.assertEqual(self.table(report, "maximum_residue")["already_present"], 1)
		self.assertNotIn("spinetoram", [d["chemical"] for d in self.documents(loader, "MRL Record")])

	def test_the_default_loader_writes_nothing(self):
		report, _loader = self.run_migration()
		self.assertFalse(report["applied"])

	def test_the_refusals_are_the_same_both_times(self):
		first, _loader = self.run_migration()
		second, _again = self.run_migration()
		self.assertEqual(first["refused"], second["refused"])


class TheCoercions(unittest.TestCase):
	def test_zero_survives_every_numeric_coercion(self):
		self.assertEqual(migration.number(0), 0.0)
		self.assertEqual(migration.integer(0), 0)

	def test_a_boolean_is_not_a_number(self):
		self.assertIsNone(migration.number(True))

	def test_the_several_spellings_of_a_sqlite_boolean(self):
		for written in (1, "1", True, "true", "yes", "Y"):
			self.assertEqual(migration.flag(written), 1, repr(written))
		for written in (0, "0", False, "false", None, "", "no"):
			self.assertEqual(migration.flag(written), 0, repr(written))

	def test_the_literal_string_none_is_not_a_value(self):
		self.assertEqual(migration.text("None"), "")
		self.assertEqual(migration.text("null"), "")

	def test_a_trailing_z_does_not_eat_a_whole_table(self):
		"""The v0.120.0 bug: `moment()` stripped the offset and left the `Z`, so
		every `isoformat()` timestamp parsed to `""` and every row carrying one
		was refused for having no timestamp."""
		self.assertEqual(migration.moment("2026-08-01T10:30:00Z"), "2026-08-01 10:30:00")
		self.assertEqual(migration.day("2026-08-01T10:30:00Z"), "2026-08-01")

	def test_a_date_column_becomes_an_iso_date_or_nothing(self):
		self.assertEqual(migration.day("2026-08-01 10:00:00"), "2026-08-01")
		self.assertEqual(migration.day("08/01/2026"), "2026-08-01")
		self.assertEqual(migration.day("not a date"), "")

	def test_an_option_map_reads_the_spellings_the_data_holds(self):
		warnings = []
		options = {"not registered": "Not Registered"}
		for written in ("not_registered", "Not-Registered", "NOT REGISTERED"):
			self.assertEqual(migration.option(written, options, warnings, "f"), "Not Registered")
		self.assertEqual(warnings, [])

	def test_an_unknown_option_says_what_it_landed_on_and_not_left_blank(self):
		warnings = []
		migration.option("co-op", {}, warnings, "participant_type", "Competitor")
		self.assertIn("left as 'Competitor'", warnings[0])


class TheCommandLine(unittest.TestCase):
	def documented_flags(self) -> set:
		block = script.__doc__.split("FLAGS, WHICH MATCH THIS DOCSTRING EXACTLY", 1)[1]
		block = block.split("THE NAME JOIN", 1)[0]
		return set(re.findall(r"^\s{4}(--[a-z-]+)", block, re.M))

	def test_the_documented_flags_and_the_registered_flags_are_the_same_set(self):
		self.assertEqual(self.documented_flags(), script.registered_flags())

	def test_the_flags_of_the_dropped_tables_are_gone(self):
		"""`--tickers`, `--parcel` and `--rotate-tokens` belonged to specs this
		release removed. A flag that outlived its spec is one somebody passes and
		watches do nothing."""
		self.assertNotIn("--tickers", script.registered_flags())
		self.assertNotIn("--parcel", script.registered_flags())
		self.assertNotIn("--rotate-tokens", script.registered_flags())
		self.assertFalse(hasattr(script, "load_tickers"))

	def test_database_is_required_and_apply_defaults_to_off(self):
		with self.assertRaises(SystemExit):
			script.parse_args([])
		self.assertFalse(script.parse_args(["--database", "x"]).apply)

	def test_only_the_four_tables_are_accepted(self):
		self.assertEqual(len(script.check_tables(list(migration.SPEC_BY_TABLE))), 4)
		with self.assertRaises(script.PlanError):
			script.check_tables(["iot_device"])

	def test_the_docstring_names_every_table_it_carries(self):
		"""The four-line table in the docstring is what somebody reads before
		running this, and a spec missing from it is a surprise at 6am."""
		for table in migration.SPEC_BY_TABLE:
			self.assertIn(table, script.__doc__)

	def test_the_report_names_each_table_and_its_counts(self):
		text = script.format_report(
			{
				"tables": [
					{
						"table": "maximum_residue",
						"doctype": "MRL Record",
						"read": 6,
						"created": 2,
						"updated": 0,
						"already_present": 0,
						"refused": 4,
						"refusals": [{"id": 3, "why": "unit not recognised"}],
						"warnings": [],
						"truncated": False,
						"note": "",
					}
				],
				"created": 2,
				"updated": 0,
				"already_present": 0,
				"refused": 4,
				"warnings": 0,
			}
		)
		self.assertIn("maximum_residue", text)
		self.assertIn("refused row 3: unit not recognised", text)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
