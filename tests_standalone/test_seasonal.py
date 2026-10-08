# SPDX-License-Identifier: MIT
"""Seasonal work lists — Fall winterize first. v0.270.0 (docs/contracts/seasonal_v0_270.yaml).

Drafts until published, disabled checklists raise nothing, the fixed date or the first forecast freeze fires it,
one task per active asset per season (never twice), one farm-level task per company, overdue alerts as the deadline
nears, the Today tile and the two reads.
"""

import datetime
from unittest import mock

import frappe

from erpnext_mcp import config_lifecycle, phone_config, seasonal, task_templates, tile_queries

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import STORE, set_roles

SPRAYER = "Winterize — Sprayer"
TRACTOR = "Winterize — Tractor / machine / vehicle"
FROST = "Frost protection readiness"


def freeze_on(day, tmin=24):
	return {"forecast": {"daily": [{"date": str(day), "tmin_f": tmin}]}}


class SeasonalCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1)
		task_templates.seed_farm_task_templates()
		seasonal.seed()
		STORE.seed("Asset Register", [
			{"name": "SPRAYER-1", "asset_type": "Sprayer", "company": MAIN},
			{"name": "SPRAYER-2", "asset_type": "Sprayer", "company": MAIN},
			{"name": "SPRAYER-OLD", "asset_type": "Sprayer", "company": MAIN, "retired_at": "2025-01-01 00:00:00"},
			{"name": "KUBOTA", "asset_type": "Tractor", "company": MAIN},
			{"name": "OTHER-SPRAYER", "asset_type": "Sprayer", "company": OTHER},
		])
		STORE.commit()
		self.today("2026-10-07")

	def today(self, day):
		patcher = mock.patch.object(frappe.utils, "today", lambda: day)
		patcher.start()
		self.addCleanup(patcher.stop)

	def enable(self, *templates):
		for name in templates:
			row = frappe.db.get_value("Farm Task Template", {"template_name": name}, "name")
			frappe.db.set_value("Farm Task Template", row, "enabled", 1)
		STORE.commit()

	def publish(self, key=seasonal.FALL, **changes):
		body = {**seasonal.SEED_PROGRAMS[key], **changes}
		body["companies"] = [MAIN]
		doc, _report = phone_config.save_draft(seasonal.KIND, key, body, "test", "Operator")
		with config_lifecycle.desk_action():
			phone_config.publish(seasonal.KIND, key, doc.version, "test", "Administrator")
		STORE.commit()

	def tasks(self):
		return [r for r in STORE.rows("Farm Task") if str(r.get("source_workorder") or "").startswith("seasonal:")]


class SeededAsDrafts(SeasonalCase):
	def test_programs_are_drafts_and_checklists_disabled(self):
		self.assertEqual({r["status"] for r in phone_config.rows(seasonal.KIND)}, {"Draft"})
		for spec in seasonal.SEED_TEMPLATES:
			self.assertFalse(frappe.db.get_value("Farm Task Template", {"template_name": spec["template_name"]}, "enabled"),
			                 spec["template_name"])
		self.assertEqual(seasonal.run(lambda c: freeze_on("2026-10-09")), {}, "nothing published, nothing runs")


class WhenItFires(SeasonalCase):
	def test_the_fixed_date_or_the_first_forecast_freeze(self):
		body = seasonal.SEED_PROGRAMS[seasonal.FALL]
		self.assertFalse(seasonal.triggered(body, MAIN, {"forecast": {"daily": []}})["fired"])
		state = seasonal.triggered(body, MAIN, freeze_on("2026-10-12", 26))
		self.assertTrue(state["fired"])
		self.assertEqual(state["deadline"], "2026-10-12")
		self.assertIn("26", state["why"])
		self.assertFalse(seasonal.triggered(body, MAIN, freeze_on("2026-10-30", 26))["fired"], "beyond the 10-day lookahead")
		self.assertFalse(seasonal.triggered(body, MAIN, freeze_on("2026-10-09", 31))["fired"], "not a hard freeze")

	def test_the_fixed_date_fires_without_a_forecast_and_june_never_does(self):
		self.today("2026-10-25")
		state = seasonal.triggered(seasonal.SEED_PROGRAMS[seasonal.FALL], MAIN, {"forecast": {"daily": []}})
		self.assertEqual((state["fired"], state["deadline"]), (True, "2026-10-25"))
		self.today("2026-06-02")
		self.assertFalse(seasonal.triggered(seasonal.SEED_PROGRAMS[seasonal.FALL], MAIN, freeze_on("2026-06-03", 20))["fired"])


class OneTaskPerAsset(SeasonalCase):
	def test_published_and_enabled_raises_one_per_active_asset_once(self):
		self.enable(SPRAYER, TRACTOR, FROST)
		self.publish()
		seasonal.run(lambda c: freeze_on("2026-10-12"))
		mine = {(r.get("asset"), r["company"]) for r in self.tasks()}
		self.assertEqual(mine, {("SPRAYER-1", MAIN), ("SPRAYER-2", MAIN), ("KUBOTA", MAIN), (None, MAIN)})
		self.assertTrue(all(str(r.get("due_date")) == "2026-10-12" for r in self.tasks()))
		count = len(self.tasks())
		seasonal.run(lambda c: freeze_on("2026-10-12"))
		self.assertEqual(len(self.tasks()), count, "never twice in a season")

	def test_a_disabled_checklist_raises_nothing(self):
		self.enable(TRACTOR)
		self.publish()
		out = seasonal.run(lambda c: freeze_on("2026-10-12"))
		self.assertEqual({r.get("asset") for r in self.tasks()}, {"KUBOTA"})
		self.assertIn(SPRAYER, out[f"{seasonal.FALL}:{MAIN}"]["raised"]["skipped_disabled"])


class Overdue(SeasonalCase):
	def test_open_tasks_alert_as_the_freeze_nears_and_done_ones_do_not(self):
		self.enable(SPRAYER)
		self.publish()
		self.today("2026-10-12")
		seasonal.run(lambda c: freeze_on("2026-10-20"))
		self.assertEqual(len(self.tasks()), 2, "8 days out: raised")
		self.assertEqual(STORE.rows("Compliance Alert"), [], "8 days out: no alert yet")
		done = self.tasks()[0]["name"]
		frappe.db.set_value("Farm Task", done, "state", "Completed")
		STORE.commit()
		self.today("2026-10-17")
		seasonal.run(lambda c: freeze_on("2026-10-20"))
		alerts = [a for a in STORE.rows("Compliance Alert") if a.get("alert_type") == f"seasonal_{seasonal.FALL}"]
		self.assertEqual(len(alerts), 1)
		self.assertNotEqual(alerts[0]["source_docname"], done)
		self.assertIn("2026-10-20", alerts[0]["alert_message"])


class TheTileAndTheReads(SeasonalCase):
	def test_tile_status_and_list(self):
		self.enable(SPRAYER, FROST)
		self.publish()
		seasonal.run(lambda c: freeze_on("2026-10-12"))
		set_roles("Administrator", ["Farm Manager"])
		tile = tile_queries.seasonal_open("Administrator", MAIN, {})
		self.assertEqual(tile["count"], 3)
		status = self.tool_data("get_winterize_status", {"company": MAIN})
		self.assertEqual((status["total"], status["open"], status["done"]), (3, 3, 0))
		listed = self.tool_data("list_seasonal_work", {"company": MAIN})
		fall = next(p for p in listed["programs"] if p["program"] == seasonal.FALL)
		self.assertTrue(fall["published"])
		self.assertEqual({c["template"]: c["enabled"] for c in fall["checklists"]}[SPRAYER], True)
		spring = next(p for p in listed["programs"] if p["program"] == seasonal.SPRING)
		self.assertFalse(spring["published"])

	def test_validation(self):
		self.assertTrue(seasonal.validate({"trigger": {}, "items": []})["errors"])
		self.assertTrue(any("MM-DD" in e for e in seasonal.validate(
			{"trigger": {"fixed_mmdd": "25-10"}, "items": [{"template": SPRAYER, "asset_types": ["Sprayer"]}]})["errors"]))
		self.assertEqual(seasonal.validate(seasonal.SEED_PROGRAMS[seasonal.FALL])["errors"], [])
		datetime.date.fromisoformat("2026-10-25")
