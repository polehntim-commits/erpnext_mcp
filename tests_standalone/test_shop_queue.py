# SPDX-License-Identifier: MIT
"""The shop queue. v0.273.0 (docs/contracts/shop_queue_v0_273.yaml).

The backlog is open tasks that are shop work (winterize first, then machines, shop templates, training) — never a
block's field work; suggestions are what is due and not yet a task; a weather day is read from the forecast (rain,
wind, a freezing high) or declared; the plan fills the hours; skilled jobs carry a pairing note naming learners;
the evening notice is off until switched on, and once a day; the Today tile; the MCP reads and the write's switch.
"""

import datetime
from unittest import mock

import frappe

from erpnext_mcp import flags, shop_queue, task_templates, tile_queries

from .fixtures import MAIN, V12TestCase
from .harness import STORE

CLEAN = "Clean and organise the shop"
SAWS = "Service chainsaws and pole saws"


def day(date, **weather):
	return {"forecast": {"daily": [{"date": date, **weather}]}}


class ShopCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1)
		task_templates.seed_farm_task_templates()
		STORE.seed("Asset Register", [{"name": "KUBOTA", "asset_type": "Tractor", "company": MAIN}])
		STORE.seed("Field", [{"name": "Block 1", "field_name": "Block 1", "owning_entity": MAIN}])
		for name in (CLEAN, SAWS):
			row = frappe.db.get_value("Farm Task Template", {"template_name": name}, "name")
			frappe.db.set_value("Farm Task Template", row, "enabled", 1)
		STORE.commit()
		patcher = mock.patch.object(frappe.utils, "today", lambda: "2026-11-03")
		patcher.start()
		self.addCleanup(patcher.stop)

	def task(self, **fields):
		doc = frappe.new_doc("Farm Task")
		doc.update({"task_name": fields.pop("task_name", "a task"), "company": MAIN, "state": "Available",
		            "task_type": "Other", "estimated_duration_minutes": 60, "evidence_required": "{\"photos\": true}",
		            **fields})
		doc.insert(ignore_permissions=True)
		STORE.commit()
		return doc.name

	def flag(self, key, kind, value):
		flags.upsert(key, kind, value, company=MAIN, description="test", owner_area="shop_queue", active=True)
		STORE.commit()


class TheBacklog(ShopCase):
	def test_only_shop_work_and_in_order(self):
		clean = shop_queue.add_item(MAIN, CLEAN)["task"]
		saws = shop_queue.add_item(MAIN, SAWS)["task"]
		repair = self.task(task_name="Kubota leaks hydraulic oil", task_type="Repair", asset="KUBOTA", urgency="High")
		winter = self.task(task_name="Winterize — Sprayer — SPRAYER-1", source_workorder="seasonal:fall:2026:S1:W",
		                   due_date="2026-11-10")
		training = self.task(task_name="WPS refresher video", task_type="Training")
		self.task(task_name="Prune Block 1", task_type="Other", location_doctype="Field", location="Block 1")
		done = shop_queue.add_item(MAIN, CLEAN)["task"]
		frappe.db.set_value("Farm Task", done, "state", "Completed")
		STORE.commit()
		order = [row["task"] for row in shop_queue.backlog(MAIN)]
		self.assertEqual(order[:2], [winter, repair], "winterize first, then the machine")
		self.assertEqual(set(order), {winter, repair, clean, saws, training})
		saw_row = next(r for r in shop_queue.backlog(MAIN) if r["task"] == saws)
		self.assertEqual(saw_row["skill"], "Skilled")
		self.assertIn("Chaps", saw_row["safety_note"])

	def test_disabled_starters_cannot_be_raised(self):
		with self.assertRaises(Exception):
			shop_queue.add_item(MAIN, "Repair picking bins")


class TheWeatherDay(ShopCase):
	def test_rain_wind_cold_or_declared(self):
		self.assertTrue(shop_queue.weather_day(MAIN, "2026-11-03", day("2026-11-03", precip_prob_pct=70))["shop_day"])
		self.assertIn("rain 0.25 in", shop_queue.weather_day(MAIN, "2026-11-03",
		                                                     day("2026-11-03", precip_in=0.25))["reasons"])
		self.assertTrue(shop_queue.weather_day(MAIN, "2026-11-03", day("2026-11-03", wind_mph=24))["shop_day"])
		self.assertTrue(shop_queue.weather_day(MAIN, "2026-11-03", day("2026-11-03", tmax_f=29))["shop_day"])
		calm = day("2026-11-03", precip_prob_pct=20, precip_in=0.0, wind_mph=6, tmax_f=55)
		self.assertFalse(shop_queue.weather_day(MAIN, "2026-11-03", calm)["shop_day"])
		self.flag("shop_day_rain_pct", "Threshold", 15)
		self.assertTrue(shop_queue.weather_day(MAIN, "2026-11-03", calm)["shop_day"], "the farm's own threshold")
		self.flag("shop_day_rain_pct", "Threshold", 60)
		self.flag(shop_queue.DECLARED, "Text", "2026-11-03 smoke")
		self.assertIn("declared", shop_queue.weather_day(MAIN, "2026-11-03", calm)["reasons"][0])

	def test_the_plan_fills_the_hours_and_pairs_learners(self):
		for _ in range(6):
			shop_queue.add_item(MAIN, SAWS)
		STORE.seed("Employee", [{"name": "EMP-ANT", "employee_name": "Antony", "company": MAIN, "status": "Active",
		                         "farm_skills": "shop_learner, pruning"}]) if frappe.db.exists("DocType", "Employee") else None
		plan = shop_queue.day_plan(MAIN, "2026-11-03", forecast=day("2026-11-03", precip_prob_pct=80))
		self.assertTrue(plan["shop_day"])
		self.assertEqual(len(plan["items"]), 4, "8 h of 2-h jobs")
		self.assertEqual(plan["minutes_planned"], 480)
		self.assertIn("Pair a learner", plan["items"][0]["pairing"])
		dry = shop_queue.day_plan(MAIN, "2026-11-03", forecast=day("2026-11-03", precip_prob_pct=5))
		self.assertEqual(dry["items"], [], "nothing on a working day")
		self.assertEqual(len(dry["would_do"]), 4)
		two = shop_queue.day_plan(MAIN, "2026-11-03", people=2, forecast=day("2026-11-03", precip_prob_pct=80))
		self.assertEqual(len(two["items"]), 6)


class TheEveningNotice(ShopCase):
	def test_off_until_on_then_once_at_the_hour(self):
		shop_queue.add_item(MAIN, CLEAN)
		evening = datetime.datetime(2026, 11, 3, 17, 5)
		wet = lambda company: day("2026-11-04", precip_prob_pct=90)  # noqa: E731
		self.assertEqual(shop_queue.hourly(evening, wet), {})
		self.flag(shop_queue.EVENING, "Flag", True)
		self.assertEqual(shop_queue.hourly(datetime.datetime(2026, 11, 3, 9, 0), wet), {}, "not at 9")
		made = shop_queue.hourly(evening, wet)
		self.assertIn(MAIN, made)
		shop_queue.hourly(evening, wet)
		alerts = [a for a in STORE.rows("Compliance Alert") if a.get("alert_type") == "shop_day_tomorrow"]
		self.assertEqual(len(alerts), 1)
		self.assertIn("rain likely", alerts[0]["alert_message"])


class TheTileAndTools(ShopCase):
	def test_tile_reads_and_the_write_switch(self):
		shop_queue.add_item(MAIN, CLEAN)
		with mock.patch.object(shop_queue, "_forecast", lambda company: day("2026-11-03", precip_prob_pct=90)):
			tile = tile_queries.shop_today("Administrator", MAIN, {})
			self.assertEqual(tile["count"], 1)
			plan = self.tool_data("get_weather_day_plan", {"company": MAIN})
			self.assertTrue(plan["shop_day"])
		listed = self.tool_data("list_shop_backlog", {"company": MAIN})
		self.assertEqual(listed["count"], 1)
		self.assertIn("allow_add_shop_item", self.tool_error("add_shop_item", {"company": MAIN, "template": CLEAN}))
		self.configure(allow_add_shop_item=1)
		self.assertTrue(self.tool_data("add_shop_item", {"company": MAIN, "template": CLEAN})["task"])
