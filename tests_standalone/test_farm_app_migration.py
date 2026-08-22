# SPDX-License-Identifier: MIT
"""`farm_app_migration` — moving the sidecar's SQLite rows in, once.

Cycle 2 of the Farm App retirement. This runs against a REAL SQLite database
built in `setUpClass`, in the farm_app's own column names, so the extract and
the transform are exercised end to end without a bench. Six claims.

1. **RUNNING IT TWICE CREATES NOTHING THE SECOND TIME.** `RunItTwice`. The
   property the whole design turns on, asserted the only way that means
   anything: run the migration, feed its own output back as what already
   exists, run it again, and require zero inserts.

2. **A ROW THAT CANNOT BECOME A DOCUMENT IS REFUSED BY NAME.** `Refusals`. A
   device with no hardware id, a reading with no timestamp, a research session
   that found no limit. Each is collected with the source row id, and none of
   them stops the run.

3. **AN MRL OF NOTHING NEVER BECOMES AN MRL OF ZERO.** `TheMrlZero`. The worst
   failure available to this exercise: zero is a real limit, and the strictest
   one there is. A `NOT_FOUND` session is refused rather than migrated.

4. **A TICKER IS NEVER DERIVED FROM A BLOCK NAME.** `NeverInvented`. The field's
   own description says it is the buyer's name for the block and that empty is
   the normal state, so `"Block A4"` does not become `"A4"`.

5. **AN UNRECOGNISED SELECT VALUE IS REPORTED, NOT GUESSED.** `NeverInvented`.
   And the warning says what the field was actually left as, which is not
   always blank.

6. **A FOREIGN KEY THAT POINTS NOWHERE IS A REFUSAL.** `Refusals`. A reading
   with no device is a number with no provenance, and inserting it with an
   empty link would hide that forever.
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

#: The farm_app's own schema for the tables Cycle 2 moves, cut down to the
#: columns the builders read. Column names are copied from the sidecar's
#: `models.py` — a fixture that renamed one would test nothing.
SCHEMA = """
CREATE TABLE field (id INTEGER PRIMARY KEY, name TEXT, polygon TEXT, acres REAL);
CREATE TABLE iot_device (id INTEGER PRIMARY KEY, name TEXT, device_type TEXT, hardware_id TEXT,
	field_id INTEGER, zone TEXT, auth_token TEXT, last_seen TEXT, battery_level REAL,
	signal_strength REAL, is_active INTEGER, config TEXT);
CREATE TABLE iot_reading (id INTEGER PRIMARY KEY, device_id INTEGER, field_id INTEGER,
	reading_type TEXT, value REAL, unit TEXT, timestamp TEXT, quality TEXT);
CREATE TABLE strategic_plan (id INTEGER PRIMARY KEY, name TEXT, vision TEXT, mission TEXT,
	values_json TEXT, swot TEXT, status TEXT, version INTEGER, effective_date TEXT,
	commodity_id INTEGER, parent_id INTEGER, notes TEXT);
CREATE TABLE objective (id INTEGER PRIMARY KEY, strategic_plan_id INTEGER, description TEXT,
	measurable TEXT, target_date TEXT, status TEXT, notes TEXT);
CREATE TABLE market_participant (id INTEGER PRIMARY KEY, name TEXT, participant_type TEXT,
	strategic_plan_id INTEGER, relationship_status TEXT, market_position TEXT, strengths TEXT,
	weaknesses TEXT, employee_count INTEGER, estimated_revenue REAL, contact_id INTEGER);
CREATE TABLE acquisition_target (id INTEGER PRIMARY KEY, participant_id INTEGER,
	strategic_plan_id INTEGER, strategic_fit_score REAL, accretive_score REAL, status TEXT,
	action_level TEXT, identified_date TEXT, recommendation TEXT);
CREATE TABLE competitive_move (id INTEGER PRIMARY KEY, participant_id INTEGER, move_type TEXT,
	severity TEXT, observed_date TEXT, description TEXT, response_urgency TEXT, confidence TEXT,
	strategic_plan_id INTEGER);
CREATE TABLE mrl_research_session (id INTEGER PRIMARY KEY, active_ingredient TEXT,
	commodity_id INTEGER, country_id INTEGER, research_result TEXT, ai_response_raw TEXT,
	review_notes TEXT, status TEXT);
CREATE TABLE field_satellite_metric (id INTEGER PRIMARY KEY, field_id INTEGER, metric_type TEXT,
	value REAL, indexed_value REAL, timestamp TEXT, source TEXT);
"""

ROWS = """
INSERT INTO field VALUES (1,'Block A4','{"type":"Polygon","coordinates":[]}',12.5);
INSERT INTO field VALUES (2,'',NULL,4.0);
INSERT INTO iot_device VALUES (1,'North probe','soil_moisture','AA:BB:CC',1,'zone 2','tok-123',
	'2026-08-01 10:00:00',0.0,-71.0,1,'{"depth_cm":30}');
INSERT INTO iot_device VALUES (2,'Nameless',NULL,'',NULL,NULL,NULL,NULL,NULL,NULL,1,NULL);
INSERT INTO iot_reading VALUES (1,1,1,'soil_moisture_vwc',0.24,'m3/m3','2026-08-01 10:00:00','good');
INSERT INTO iot_reading VALUES (2,1,1,'soil_moisture_vwc',0.0,'m3/m3','2026-08-01 11:00:00','suspect');
INSERT INTO iot_reading VALUES (3,99,1,'soil_moisture_vwc',0.25,'m3/m3','2026-08-01 12:00:00','good');
INSERT INTO iot_reading VALUES (4,1,1,'soil_moisture_vwc',0.26,'m3/m3',NULL,'good');
INSERT INTO strategic_plan VALUES (1,'Cherry 2030','Be the best','Grow well',NULL,
	'{"strengths":["water rights"]}','implemented',2,'2026-01-01',1,NULL,'a note');
INSERT INTO objective VALUES (1,1,'Plant 40 acres','acres','2027-01-01','in_progress',NULL);
INSERT INTO objective VALUES (2,1,'','acres','2027-01-01','pending',NULL);
INSERT INTO objective VALUES (3,404,'Orphaned','acres','2027-01-01','pending',NULL);
INSERT INTO market_participant VALUES (1,'Valley Orchards','competitor',1,'adversarial','leader',
	'["scale","cold storage"]','["debt"]',300,4000000.0,7);
INSERT INTO market_participant VALUES (2,'Co-op Pack','co-op',1,'neutral',NULL,NULL,NULL,10,NULL,NULL);
INSERT INTO acquisition_target VALUES (1,1,1,8.5,7.9,'evaluating','pursue','2026-05-01',
	'{"action":"monitor"}');
INSERT INTO competitive_move VALUES (1,1,'Expansion','high','2026-07-01','Bought 200 acres',
	'immediate','high',1);
INSERT INTO competitive_move VALUES (2,1,'Expansion','high',NULL,'No date','monitor','low',1);
INSERT INTO mrl_research_session VALUES (1,'spinetoram',1,1,
	'{"mrl_value":0.5,"source_reference":"EU 396/2005","confidence":"high",
	  "substance_status":"registered","source_tier":1,"effective_date":"2025-01-01"}',
	'raw model output','looked right','completed');
INSERT INTO mrl_research_session VALUES (2,'nothingol',1,1,'{"mrl_value":"NOT_FOUND"}','raw',NULL,'completed');
INSERT INTO mrl_research_session VALUES (3,'',1,1,'{"mrl_value":1.0}','raw',NULL,'completed');
INSERT INTO field_satellite_metric VALUES (1,1,'ndvi',0.71,NULL,'2026-08-01 00:00:00','sentinel-2');
INSERT INTO field_satellite_metric VALUES (2,1,'ndvi',0.65,NULL,'2026-07-25 00:00:00','sentinel-2');
INSERT INTO field_satellite_metric VALUES (3,1,'moisture',0.30,NULL,'2026-08-01 00:00:00','sentinel-2');
"""

#: What the site already knows before the run: the blocks migrated in an earlier
#: wave, and the Crop and Market the strategic and MRL rows point at.
SEED = {
	"field": {"1": "Block A4"},
	"commodity": {"1": "Cherry"},
	"country": {"1": "European Union"},
}


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

	def run_migration(self, loader=None, context=None, **kwargs):
		# The default loader models a site where the earlier wave already ran:
		# block 1 is there and carries its farm_app id, which is what
		# `seed_links_from_site` reads and what `SEED` mirrors. Without it the
		# `field` spec would insert a second copy of a block that exists, and
		# every child row would link to the copy.
		loader = (
			loader
			if loader is not None
			else migration.DryRunLoader({"Field": [{"external_farm_app_id": "1", "name": "Block A4"}]})
		)
		links = migration.Links(SEED)
		report = migration.migrate(
			self.connection,
			loader,
			links,
			{"company": "Orchard Meadow, LLC", **(context or {})},
			**kwargs,
		)
		return report, loader

	def table(self, report, name) -> dict:
		return next(entry for entry in report["tables"] if entry["table"] == name)

	def documents(self, loader, doctype) -> list:
		return [row["doc"] for row in loader.inserted if row["doctype"] == doctype]


class ReadingTheSource(MigrationTestCase):
	def test_the_database_is_opened_read_only(self):
		"""This runs against a copy somebody may have taken while the Flask app
		was still writing. A migration that could modify its own source is one
		whose second run cannot be trusted."""
		with self.assertRaises(sqlite3.OperationalError):
			self.connection.execute("INSERT INTO field (name) VALUES ('written by the migration')")

	def test_a_database_that_is_not_there_is_refused_before_anything_starts(self):
		with self.assertRaises(migration.MigrationError):
			migration.open_database(os.path.join(self.directory, "no-such.db"))

	def test_a_table_the_export_does_not_have_is_a_warning_and_not_a_crash(self):
		"""And its warning is a dict like every other one — an earlier draft
		appended a bare string here and every reader crashed on exactly the
		table a partial export is most likely to be missing."""
		empty = os.path.join(self.directory, "empty.db")
		connection = sqlite3.connect(empty)
		connection.execute("CREATE TABLE field (id INTEGER PRIMARY KEY, name TEXT)")
		connection.commit()
		connection.close()
		report = migration.migrate(migration.open_database(empty))
		for entry in report["tables"]:
			for warning in entry["warnings"]:
				self.assertEqual(set(warning), {"id", "warning"})
		self.assertIn("not in this database", self.table(report, "iot_device")["warnings"][0]["warning"])

	def test_rows_come_back_oldest_first(self):
		rows = list(migration.read_rows(self.connection, "iot_reading"))
		self.assertEqual([row["id"] for row in rows], sorted(row["id"] for row in rows))

	def test_the_row_limit_is_reported_rather_than_silently_truncating(self):
		"""A silently truncated migration reads exactly like a complete one."""
		report, _loader = self.run_migration(limit=1, only=["iot_reading"])
		entry = self.table(report, "iot_reading")
		self.assertTrue(entry["truncated"])
		self.assertIn("row limit", entry["warnings"][-1]["warning"])

	def test_an_unknown_table_is_refused_by_name(self):
		with self.assertRaises(migration.MigrationError):
			migration.migrate(self.connection, only=["nostr_identity"])


class WhatTheDocumentsSay(MigrationTestCase):
	def setUp(self):
		super().setUp()
		self.report, self.loader = self.run_migration()

	def test_a_device_carries_its_hardware_id_zone_and_config(self):
		device = self.documents(self.loader, "IoT Device")[0]
		self.assertEqual(device["hardware_id"], "AA:BB:CC")
		self.assertEqual(device["device_type"], "Soil Moisture")
		self.assertEqual(device["zone"], "zone 2")
		self.assertIn("depth_cm", device["device_config"])
		self.assertEqual(device["field"], "Block A4")
		self.assertEqual(device["company"], "Orchard Meadow, LLC")

	def test_a_battery_reading_of_zero_survives(self):
		"""`str(x or "")` refuses to write a 0, and a dead battery is exactly
		the reading somebody needs to see."""
		self.assertEqual(self.documents(self.loader, "IoT Device")[0]["battery_level"], 0.0)

	def test_a_reading_of_zero_survives_too(self):
		values = [doc["value"] for doc in self.documents(self.loader, "IoT Reading")]
		self.assertIn(0.0, values)

	def test_the_device_auth_token_crosses_by_default(self):
		"""A device in an orchard post has this burned into its firmware and no
		way to be told a new one short of a truck and a laptop."""
		self.assertEqual(self.documents(self.loader, "IoT Device")[0]["auth_token"], "tok-123")

	def test_rotate_tokens_leaves_it_behind(self):
		_report, loader = self.run_migration(context={"rotate_tokens": True})
		self.assertNotIn("auth_token", self.documents(loader, "IoT Device")[0])

	def test_a_json_column_becomes_readable_text_and_not_one_line(self):
		"""These land in fields an operator OPENS. A single line of JSON in a
		textarea is a document nobody will read again."""
		plan = self.documents(self.loader, "Strategic Plan")[0]
		self.assertIn("water rights", plan["swot"])
		self.assertIn("\n", plan["swot"])

	def test_a_json_list_becomes_one_item_per_line_for_the_small_text_fields(self):
		participant = self.documents(self.loader, "Market Participant")[0]
		self.assertEqual(participant["strengths"], "scale\ncold storage")

	def test_the_foreign_keys_become_docnames(self):
		objective = self.documents(self.loader, "Strategic Objective")[0]
		plan_row = next(row for row in self.loader.inserted if row["doctype"] == "Strategic Plan")
		self.assertEqual(plan_row["doc"]["plan_name"], "Cherry 2030")
		# The objective points at the docname the loader returned for the plan,
		# not at the SQLite id it carried.
		self.assertEqual(objective["strategic_plan"], "Strategic Plan-new-1")
		self.assertNotIn(objective["strategic_plan"], ("1", 1))

	def test_a_crop_and_a_market_resolve_through_the_seeded_map(self):
		self.assertEqual(self.documents(self.loader, "Strategic Plan")[0]["crop"], "Cherry")
		self.assertEqual(self.documents(self.loader, "MRL Record")[0]["market"], "European Union")

	def test_the_statuses_land_on_the_doctypes_own_options(self):
		self.assertEqual(self.documents(self.loader, "Strategic Plan")[0]["status"], "Implemented")
		self.assertEqual(self.documents(self.loader, "Strategic Objective")[0]["status"], "In Progress")
		self.assertEqual(self.documents(self.loader, "Acquisition Target")[0]["action_level"], "Pursue")
		self.assertEqual(self.documents(self.loader, "Competitive Move")[0]["response_urgency"], "Urgent")

	def test_a_date_column_becomes_an_iso_date_and_a_timestamp_a_datetime(self):
		self.assertEqual(self.documents(self.loader, "Strategic Plan")[0]["effective_date"], "2026-01-01")
		self.assertEqual(self.documents(self.loader, "IoT Reading")[0]["timestamp"], "2026-08-01 10:00:00")

	def test_the_mrl_record_carries_the_limit_and_its_provenance(self):
		record = self.documents(self.loader, "MRL Record")[0]
		self.assertEqual(record["chemical"], "spinetoram")
		self.assertEqual(record["mrl_ppm"], 0.5)
		self.assertEqual(record["source"], "EU 396/2005")
		self.assertEqual(record["confidence"], "High")
		self.assertEqual(record["substance_status"], "Registered")
		self.assertEqual(record["research_response"], "raw model output")


class Refusals(MigrationTestCase):
	def setUp(self):
		super().setUp()
		self.report, self.loader = self.run_migration()

	def why(self, table, row_id) -> str:
		refusals = {row["id"]: row["why"] for row in self.table(self.report, table)["refusals"]}
		self.assertIn(row_id, refusals, f"row {row_id} of {table} was not refused")
		return refusals[row_id]

	def test_a_device_with_no_hardware_id_has_no_natural_key(self):
		"""Two of them would migrate as one, and neither could be matched to the
		thing on the post."""
		self.assertIn("natural key", self.why("iot_device", 2))

	def test_a_reading_with_no_timestamp_has_no_place_in_a_series(self):
		self.assertIn("no place in a series", self.why("iot_reading", 4))

	def test_a_reading_whose_device_was_not_migrated_is_refused(self):
		"""Inserting it with an empty link would hide the missing provenance
		forever."""
		self.assertIn("has not been migrated", self.why("iot_reading", 3))

	def test_an_orphaned_objective_is_refused(self):
		self.assertIn("strategic_plan id 404", self.why("objective", 3))

	def test_an_objective_with_nothing_to_achieve_is_refused(self):
		self.assertIn("states nothing to achieve", self.why("objective", 2))

	def test_a_move_with_no_observed_date_cannot_be_placed_in_the_timeline(self):
		self.assertIn("competitive timeline", self.why("competitive_move", 2))

	def test_a_refusal_never_stops_the_run(self):
		"""Four tables carry a refused row and every one of them still migrated
		its good rows."""
		self.assertGreater(self.report["refused"], 0)
		self.assertGreater(self.report["created"], 0)
		self.assertEqual(self.table(self.report, "iot_reading")["created"], 2)

	def test_every_refusal_carries_the_source_row_id(self):
		"""A migration that refused eleven rows has to say which eleven."""
		for entry in self.report["tables"]:
			for refusal in entry["refusals"]:
				self.assertIsNotNone(refusal["id"])
				self.assertTrue(refusal["why"])


class TheMrlZero(MigrationTestCase):
	def setUp(self):
		super().setUp()
		self.report, self.loader = self.run_migration()

	def test_a_session_that_found_no_limit_is_refused(self):
		refusals = {
			row["id"]: row["why"] for row in self.table(self.report, "mrl_research_session")["refusals"]
		}
		self.assertIn("no MRL value", refusals[2])

	def test_the_refusal_says_why_zero_would_be_the_worst_outcome(self):
		"""Zero is a real limit and the strictest one there is — a shipment
		decided against it would be held for a residue nobody exceeded."""
		refusals = {
			row["id"]: row["why"] for row in self.table(self.report, "mrl_research_session")["refusals"]
		}
		self.assertIn("strictest limit", refusals[2])

	def test_no_mrl_record_was_created_with_a_zero_limit(self):
		"""The claim itself, asserted over what was actually built rather than
		over the refusal message."""
		for record in self.documents(self.loader, "MRL Record"):
			self.assertGreater(record["mrl_ppm"], 0)

	def test_a_session_naming_no_substance_is_refused(self):
		refusals = {row["id"] for row in self.table(self.report, "mrl_research_session")["refusals"]}
		self.assertIn(3, refusals)


class NeverInvented(MigrationTestCase):
	def fresh_site(self, context=None):
		"""A run against a site with no blocks yet — the only case in which the
		`field` spec creates anything, and so the only one where a ticker
		decision is visible."""
		return self.run_migration(loader=migration.DryRunLoader(), context=context, only=["field"])

	def test_a_ticker_is_not_derived_from_a_block_name(self):
		"""`block_ticker` is the buyer's name for the block, unique across the
		company, promised to somebody outside the business. Deriving `"A4"` from
		`"Block A4"` would manufacture that promise on every block at once."""
		_report, loader = self.fresh_site()
		block = self.documents(loader, "Field")[0]
		self.assertEqual(block["field_name"], "Block A4")
		self.assertNotIn("block_ticker", block)

	def test_a_ticker_the_operator_states_is_carried_and_upper_cased(self):
		_report, loader = self.fresh_site({"tickers": {"1": "yc-3"}})
		self.assertEqual(self.documents(loader, "Field")[0]["block_ticker"], "YC-3")

	def test_a_ticker_may_be_keyed_by_block_name_as_well_as_id(self):
		_report, loader = self.fresh_site({"tickers": {"Block A4": "OM-A4"}})
		self.assertEqual(self.documents(loader, "Field")[0]["block_ticker"], "OM-A4")

	def test_a_ticker_too_long_for_the_field_is_warned_and_dropped(self):
		report, loader = self.fresh_site({"tickers": {"1": "AN-EXTREMELY-LONG-ONE"}})
		self.assertNotIn("block_ticker", self.documents(loader, "Field")[0])
		self.assertIn("longer than", self.table(report, "field")["warnings"][0]["warning"])

	def test_the_block_carries_its_farm_app_id_for_the_next_wave_to_match_on(self):
		_report, loader = self.fresh_site()
		self.assertEqual(self.documents(loader, "Field")[0]["external_farm_app_id"], "1")

	def test_an_unrecognised_select_value_is_warned_and_says_what_it_landed_as(self):
		"""A warning that said "left blank" while a value was written would send
		an operator looking for an empty field they would never find."""
		report, _loader = self.run_migration()
		warnings = [row["warning"] for row in self.table(report, "market_participant")["warnings"]]
		matching = [text for text in warnings if "co-op" in text]
		self.assertEqual(len(matching), 1)
		self.assertIn("left as 'Competitor'", matching[0])

	def test_a_contact_is_not_guessed_into_a_customer_or_a_supplier(self):
		report, _loader = self.run_migration()
		warnings = [row["warning"] for row in self.table(report, "market_participant")["warnings"]]
		self.assertTrue(any("Customer or a Supplier" in text for text in warnings))

	def test_the_satellite_series_loss_is_stated_rather_than_left_to_be_noticed(self):
		report, loader = self.run_migration()
		entry = self.table(report, "field_satellite_metric")
		self.assertEqual(entry["updated"], 1)
		self.assertIn("Satellite Metric` has not shipped", entry["warnings"][0]["warning"])
		self.assertEqual(loader.updated[0]["changes"]["last_ndvi_mean"], 0.71)

	def test_the_newest_ndvi_wins_and_a_non_ndvi_metric_does_not_land_on_field(self):
		"""0.71 is the 1 August reading and 0.65 the 25 July one; the moisture
		row has nowhere to go at all."""
		_report, loader = self.run_migration()
		self.assertEqual(len(loader.updated), 1)
		self.assertEqual(loader.updated[0]["changes"]["last_ndvi_pull_date"], "2026-08-01")


class RunItTwice(MigrationTestCase):
	def test_the_second_run_creates_nothing(self):
		"""The property the whole design turns on. The second run is fed the
		first run's own output as what already exists, including the docnames it
		returned — which is what a real Frappe loader would have written into
		the links map."""
		first, loader = self.run_migration()
		existing = {"Field": [{"external_farm_app_id": "1", "name": "Block A4"}]}
		for index, row in enumerate(loader.inserted, start=1):
			name = f"{row['doctype']}-new-{index}"
			existing.setdefault(row["doctype"], []).append({**row["doc"], "name": name})

		second, again = self.run_migration(loader=migration.DryRunLoader(existing))
		self.assertEqual(again.inserted, [])
		self.assertEqual(second["created"], 0)
		# Everything the first run created OR found is found by the second. The
		# `+ already_present` half matters: block 1 was there before either run,
		# and an assertion that ignored it would drift the moment the fixture
		# gained a second pre-existing document.
		self.assertEqual(second["already_present"], first["created"] + first["already_present"])

	def test_the_refusals_are_the_same_both_times(self):
		first, _loader = self.run_migration()
		second, _again = self.run_migration()
		self.assertEqual(first["refused"], second["refused"])

	def test_an_existing_document_is_left_exactly_as_it_is(self):
		"""It never updates: after the cutover ERPNext is the system of record,
		and reaching back to overwrite an operator's correction with a stale
		sidecar value would silently undo their work."""
		existing = {
			"IoT Device": [{"hardware_id": "AA:BB:CC", "name": "IOTD-00001", "zone": "somewhere else"}]
		}
		report, loader = self.run_migration(loader=migration.DryRunLoader(existing), only=["iot_device"])
		self.assertEqual(self.documents(loader, "IoT Device"), [])
		self.assertEqual(self.table(report, "iot_device")["already_present"], 1)

	def test_the_links_map_remembers_a_document_that_was_already_there(self):
		"""Otherwise a second run would refuse every child row, because their
		parents were "not migrated" — by this run."""
		existing = {"IoT Device": [{"hardware_id": "AA:BB:CC", "name": "IOTD-00001"}]}
		_report, loader = self.run_migration(loader=migration.DryRunLoader(existing))
		self.assertEqual(self.documents(loader, "IoT Reading")[0]["device"], "IOTD-00001")

	def test_the_default_loader_writes_nothing(self):
		report, _loader = self.run_migration()
		self.assertFalse(report["applied"])

	def test_updates_are_counted_apart_from_creations(self):
		"""The satellite fold updates on every run by design. Folded into
		`created`, a fully idempotent second run would report that it had
		created something — which is the one number an operator checks."""
		first, loader = self.run_migration()
		existing = {"Field": [{"external_farm_app_id": "1", "name": "Block A4"}]}
		for index, row in enumerate(loader.inserted, start=1):
			existing.setdefault(row["doctype"], []).append(
				{**row["doc"], "name": f"{row['doctype']}-new-{index}"}
			)
		second, _again = self.run_migration(loader=migration.DryRunLoader(existing))
		self.assertEqual(first["updated"], second["updated"])
		self.assertEqual(second["created"], 0)


class TheCoercions(unittest.TestCase):
	def test_zero_survives_every_numeric_coercion(self):
		self.assertEqual(migration.number(0), 0.0)
		self.assertEqual(migration.integer(0), 0)
		self.assertEqual(migration.number("0"), 0.0)

	def test_a_boolean_is_not_a_number(self):
		self.assertIsNone(migration.number(True))

	def test_the_several_spellings_of_a_sqlite_boolean(self):
		for written in (1, "1", True, "true", "yes", "Y"):
			self.assertEqual(migration.flag(written), 1, repr(written))
		for written in (0, "0", False, "false", None, "", "no"):
			self.assertEqual(migration.flag(written), 0, repr(written))

	def test_the_literal_string_none_is_not_a_value(self):
		"""SQLite exports written by a naive `str()` carry it, and a field
		holding the word "None" is worse than an empty one."""
		self.assertEqual(migration.text("None"), "")
		self.assertEqual(migration.text("null"), "")

	def test_a_date_column_becomes_an_iso_date_or_nothing(self):
		self.assertEqual(migration.day("2026-08-01 10:00:00"), "2026-08-01")
		self.assertEqual(migration.day("2026-08-01"), "2026-08-01")
		self.assertEqual(migration.day("08/01/2026"), "2026-08-01")
		self.assertEqual(migration.day("not a date"), "")

	def test_a_datetime_keeps_its_time(self):
		self.assertEqual(migration.moment("2026-08-01T10:30:00Z"), "2026-08-01 10:30:00")
		self.assertEqual(migration.moment("2026-08-01"), "2026-08-01 00:00:00")

	def test_json_that_is_not_json_survives_as_the_text_it_is(self):
		"""A column somebody typed prose into is still worth keeping."""
		self.assertEqual(migration.json_text("just some notes"), "just some notes")

	def test_a_json_dict_becomes_key_and_value_lines(self):
		self.assertEqual(migration.lines('{"a": 1, "b": 2}'), "a: 1\nb: 2")

	def test_an_option_map_reads_the_spellings_the_data_holds(self):
		warnings = []
		options = {"acquisition target": "Acquisition Target"}
		for written in ("acquisition_target", "Acquisition-Target", "ACQUISITION TARGET"):
			self.assertEqual(migration.option(written, options, warnings, "f"), "Acquisition Target")
		self.assertEqual(warnings, [])

	def test_a_blank_option_takes_the_default_without_a_warning(self):
		warnings = []
		self.assertEqual(migration.option("", {}, warnings, "f", "Medium"), "Medium")
		self.assertEqual(warnings, [])


class TheCommandLine(unittest.TestCase):
	def documented_flags(self) -> set:
		block = script.__doc__.split("FLAGS, WHICH MATCH THIS DOCSTRING EXACTLY", 1)[1]
		block = block.split("THE TICKER FILE", 1)[0]
		return set(re.findall(r"^\s{4}(--[a-z-]+)", block, re.M))

	def test_the_documented_flags_and_the_registered_flags_are_the_same_set(self):
		self.assertEqual(self.documented_flags(), script.registered_flags())

	def test_database_is_required(self):
		with self.assertRaises(SystemExit):
			script.parse_args([])

	def test_apply_defaults_to_off(self):
		"""A migration is not something to run for real by accident."""
		self.assertFalse(script.parse_args(["--database", "x"]).apply)

	def test_table_is_repeatable(self):
		parsed = script.parse_args(["--database", "x", "--table", "iot_device", "--table", "iot_reading"])
		self.assertEqual(parsed.table, ["iot_device", "iot_reading"])

	def test_an_unknown_table_is_refused_before_frappe_is_started(self):
		with self.assertRaises(script.PlanError):
			script.check_tables(["nostr_identity"])

	def test_every_spec_table_is_accepted(self):
		self.assertEqual(len(script.check_tables(list(migration.SPEC_BY_TABLE))), len(migration.SPECS))

	def test_a_ticker_too_long_for_the_field_is_refused_rather_than_trimmed(self):
		"""A silently truncated ticker is a different promise from the one the
		farm made."""
		directory = tempfile.mkdtemp()
		path = os.path.join(directory, "tickers.json")
		with open(path, "w") as handle:
			handle.write('{"1": "AN-EXTREMELY-LONG-TICKER"}')
		with self.assertRaises(script.PlanError) as caught:
			script.load_tickers(path)
		self.assertIn("the field holds 10", str(caught.exception))

	def test_a_ticker_file_is_upper_cased_and_blanks_are_dropped(self):
		directory = tempfile.mkdtemp()
		path = os.path.join(directory, "tickers.json")
		with open(path, "w") as handle:
			handle.write('{"1": "yc-3", "2": "", "3": null}')
		self.assertEqual(script.load_tickers(path), {"1": "YC-3"})

	def test_no_ticker_file_is_an_empty_map_and_not_an_error(self):
		self.assertEqual(script.load_tickers(""), {})

	def test_a_ticker_file_that_is_not_json_is_refused_by_name(self):
		directory = tempfile.mkdtemp()
		path = os.path.join(directory, "tickers.json")
		with open(path, "w") as handle:
			handle.write("not json")
		with self.assertRaises(script.PlanError):
			script.load_tickers(path)

	def test_the_report_names_every_table_and_its_counts(self):
		text = script.format_report(
			{
				"tables": [
					{
						"table": "iot_device",
						"doctype": "IoT Device",
						"read": 2,
						"created": 1,
						"updated": 0,
						"already_present": 0,
						"refused": 1,
						"refusals": [{"id": 2, "why": "no hardware id"}],
						"warnings": [],
						"truncated": False,
						"note": "",
					}
				],
				"created": 1,
				"updated": 0,
				"already_present": 0,
				"refused": 1,
				"warnings": 0,
			}
		)
		self.assertIn("iot_device", text)
		self.assertIn("refused row 2: no hardware id", text)
		self.assertIn("1 to create", text)

	def test_the_specs_are_in_dependency_order(self):
		"""Reordering `SPECS` breaks the foreign keys — a child cannot resolve a
		parent that has not run — so the order is the spec and not a
		presentation choice."""
		seen = set()
		for spec in migration.SPECS:
			for parent in spec.depends_on:
				self.assertIn(parent, seen, f"{spec.table} runs before {parent}")
			seen.add(spec.table)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
