"""Go / Hold on Farm Tasks. v0.240.0 (approved queue item 5; decisions 12, 15, 16, 17, 39)."""

import json
from unittest import mock

import frappe

from erpnext_mcp import ccf_providers, compliance_rules, go_hold
from erpnext_mcp.alerts import engine

from .harness import STORE
from .test_ccf_weather import PAYLOAD
from .test_dispatch import ALL_ON, DispatchTestCase

ON = {**ALL_ON, "allow_check_go_hold": 1, "allow_override_hold": 1}
TODAY = str(frappe.utils.today())[:10]


def forecast(wet: bool) -> dict:
	daily = dict(PAYLOAD["daily"])
	if not wet:
		daily.update(precipitation_probability_max=[0] * 7, precipitation_sum=[0] * 7, temperature_2m_min=[35] * 7)
	# Re-date the canned week so "today" is in it.
	import datetime

	first = datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=3)
	daily["time"] = [(first + datetime.timedelta(days=i)).isoformat() for i in range(7)]
	# 72 dry hours behind now, so "hours since rain" is known.
	now = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
	times = [(now - datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(72, 0, -1)]
	hourly = {"time": times, "temperature_2m": [40] * 72, "precipitation": [0] * 72,
	          "precipitation_probability": [0] * 72, "wind_speed_10m": [4] * 72}
	return ccf_providers.normalise({**PAYLOAD, "daily": daily, "hourly": hourly}, "cell", today=TODAY)


class GoHoldTestCase(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)

	def a_rule(self, rule_id="go_hold_pruning_canker", enforced=False, **overrides):
		spec = next(s for s in go_hold.preset_specs() if s["rule_id"] == rule_id)
		spec = {**spec, "enabled": 1, "human_approved_by": "Administrator", "human_approved_on": frappe.utils.now(),
		        **overrides}
		if enforced:
			spec["actions"] = [{"type": "set_go_hold"}, {"type": "block_start"}]
		doc = compliance_rules.build_rule(spec)
		doc.insert(ignore_permissions=True)
		return doc.name

	def weather(self, wet=True):
		return mock.patch.object(ccf_providers, "_weather", return_value=forecast(wet))

	def pruning(self, **overrides):
		return self.claimed(task_name="Prune Block 7", task_type="Maintenance", **overrides)


class NoRuleNoChange(GoHoldTestCase):
	def test_a_task_no_rule_speaks_to_starts_exactly_as_before(self):
		task = self.pruning()
		self.assertEqual(self.tool_data("check_go_hold", {"task": task})["status"], "No rule")
		started = self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertNotIn("go_hold", started)
		self.assertFalse(STORE.get_raw("Farm Task", task).get("go_hold"))


class AdvisoryHold(GoHoldTestCase):
	def test_rain_ahead_holds_pruning_says_why_and_the_work_still_starts(self):
		self.a_rule()
		task = self.pruning()
		with self.weather(wet=True):
			data = self.tool_data("check_go_hold", {"task": task, "language": "es"})
			self.assertEqual(data["status"], "Hold")
			self.assertIn("Lluvia probable", data["reasons"][0])
			self.assertFalse(data["enforced_hold"])
			self.assertFalse(STORE.get_raw("Farm Task", task).get("go_hold"), "check_go_hold writes nothing")
			started = self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertEqual(started["go_hold"]["status"], "Hold")
		self.assertIn("Advisory Hold", started["go_hold"]["note"])
		row = STORE.get_raw("Farm Task", task)
		self.assertEqual(row["go_hold"], "Hold")
		entry = json.loads(row["go_hold_log"])[0]
		self.assertEqual(entry["moment"], "task_start")
		rule = entry["rules"][0]
		self.assertEqual((rule["rule_id"], rule["version"], rule["enforcement"]), ("go_hold_pruning_canker", 1, "Advisory"))
		self.assertIn("weather.forecast.daily[0..6].rain", rule["read"])

	def test_a_task_outside_the_rules_scope_is_not_judged(self):
		self.a_rule()
		task = self.claimed(task_name="Fix the gate", task_type="Repair")
		with self.weather(wet=True):
			self.assertEqual(self.tool_data("check_go_hold", {"task": task})["status"], "No rule")


class EnforcedHold(GoHoldTestCase):
	def test_an_enforced_hold_refuses_the_start_until_a_supervisor_overrides_with_a_reason(self):
		self.a_rule(enforced=True)
		task = self.pruning()
		with self.weather(wet=True):
			error = self.tool_error("start_farm_task", {"task": task, "worker_id": "EMP-001"})
			self.assertIn("is on Hold", error)
			self.assertIn("override_hold", error)
			self.assertIn("Nothing was changed", error)
			self.assertIn("reason", self.tool_error("override_hold", {"task": task, "reason": ""}))
			self.tool_data("override_hold", {"task": task, "reason": "Rain is after 3 pm; we finish by noon"})
			started = self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("Rain is after 3 pm", started["go_hold"]["note"])
		log = json.loads(STORE.get_raw("Farm Task", task)["go_hold_log"])
		self.assertIn("override", [e["moment"] for e in log])

	def test_only_a_supervisor_can_override(self):
		self.a_rule(enforced=True)
		task = self.pruning()
		with self.assertRaisesRegex(PermissionError, "Foreman, Farm Manager or System Manager"):
			go_hold.override(task, "We are fine today", "crew.member@example.com")


class HoldsClearThemselves(GoHoldTestCase):
	def test_the_six_oclock_check_holds_then_clears_when_the_forecast_dries(self):
		self.a_rule()
		task = self.pruning()
		with self.weather(wet=True):
			report = go_hold.day_start()
		self.assertEqual((report["checked"], report["hold"], report["cleared"]), (1, 1, 0))
		with self.weather(wet=False):
			report = go_hold.day_start()
		self.assertEqual((report["hold"], report["cleared"]), (0, 1))
		row = STORE.get_raw("Farm Task", task)
		self.assertEqual(row["go_hold"], "Go")
		self.assertTrue(json.loads(row["go_hold_log"])[0]["cleared"])

	def test_a_task_shows_its_verdict(self):
		self.a_rule()
		task = self.pruning()
		with self.weather(wet=True):
			go_hold.day_start()
		described = self.tool_data("get_farm_task", {"task": task})
		described = described.get("task", described)
		self.assertEqual(described["go_hold"], "Hold")
		self.assertTrue(described["go_hold_reasons"])


class NoStageMeansVerify(GoHoldTestCase):
	def test_missing_stage_is_go_verify_stage_not_a_hold(self):
		self.a_rule(
			rule_id="go_hold_pruning_canker",
			condition_tree={"id": "before_bud_burst", "path": "phenology.bbch", "op": "lt", "value": "51"},
		)
		task = self.pruning()
		STORE.get_raw("Farm Task", task).update(location_doctype="Field", location="B7")
		verdict = go_hold.check(task)
		self.assertEqual(verdict["status"], go_hold.VERIFY)
		self.assertIn("check the stage in the field", verdict["reasons"][0])


class TheSweepLeavesThemAlone(GoHoldTestCase):
	def test_a_go_hold_rule_files_no_alerts_unless_it_asks_to(self):
		self.a_rule()
		rules, _notes = engine.rule_set()
		self.assertNotIn("go_hold_pruning_canker", rules)
		self.a_rule(rule_id="go_hold_spray_wind", actions=[{"type": "set_go_hold"}, {"type": "alert"}])
		rules, _notes = engine.rule_set()
		self.assertIn("go_hold_spray_wind", rules)

	def test_the_presets_are_seeded_off(self):
		made = go_hold.seed()
		self.assertEqual(len(made), 9)  # v0.264.0: + the two pest DD window presets; v0.275.0: + bees out
		rows = [r for r in compliance_rules.rule_rows(include_inactive=True) if r["rule_id"].startswith("go_hold_")]
		self.assertEqual(len(rows), 9)
		self.assertFalse(any(int(r.get("enabled") or 0) for r in rows))
		self.assertEqual(go_hold.seed(), [], "create-only")
