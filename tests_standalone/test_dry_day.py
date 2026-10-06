"""Probabilistic dry day, per-rule rain threshold, Holds cleared by the forecast, and the rain archive
check. v0.253.0 (Tim's decisions 15, 19, 21)."""

import datetime
import json
from unittest import mock

import frappe

from erpnext_mcp import ccf, ccf_providers, compliance_rules, go_hold, weather_verify
from erpnext_mcp.alerts import engine

from .harness import STORE
from .test_ccf_weather import PAYLOAD
from .test_go_hold import TODAY, GoHoldTestCase, forecast

CANKER = "go_hold_pruning_canker"


def dry_ahead(over=0.05, value=40):
	return {"id": "dry_ahead", "path": "weather.forecast.daily[0..6].rain", "agg": "chance_over", "over": over,
	        "op": "lt", "value": value}


class TheRainModel(GoHoldTestCase):
	def test_a_chance_of_rain_with_no_amount_is_mostly_not_a_wet_day(self):
		# 60% chance of anything, nothing forecast: drizzle at most.
		self.assertAlmostEqual(ccf_providers.p_over(60, 0.0), 0.6 * 2.718281828 ** (-(0.05 - 0.004) / 0.02), places=4)
		self.assertLess(ccf_providers.p_over(60, 0.0), 0.07)

	def test_a_wet_forecast_is_mostly_over_the_threshold_and_less_so_for_a_higher_one(self):
		low, high = ccf_providers.p_over(70, 0.3, 0.05), ccf_providers.p_over(70, 0.3, 0.5)
		self.assertGreater(low, 0.6)
		self.assertLess(high, low)
		self.assertEqual(ccf_providers.p_over(0, 0.0), 0.0)
		self.assertGreater(ccf_providers.p_over(0, 0.4), 0.8, "the model rains while the ensemble is silent")

	def test_the_old_any_rain_risk_is_unchanged_and_the_new_risk_sits_beside_it(self):
		out = ccf_providers.normalise(PAYLOAD, "cell", today="2026-01-15")
		daily = out["forecast"]["daily"]
		self.assertEqual([d["rain_risk_cum_pct"] for d in daily[:3]], [10.0, 28.0, 78.4])
		self.assertEqual(daily[0]["rain"], {"p": 10.0, "in": 0.0})
		wet = [d["wet_risk_cum_pct"] for d in daily]
		self.assertEqual(wet, sorted(wet), "a running chance never falls")
		self.assertLess(wet[1], 5, "two dry-amount days are not wet days")
		self.assertGreater(wet[2], 60, "the 0.3 in day")

	def test_the_threshold_is_the_rules_own(self):
		values = {"weather": ccf_providers.normalise(PAYLOAD, "cell", today="2026-01-15")}
		self.assertFalse(ccf.evaluate(ccf.parse_tree(dry_ahead(0.05)), values)["passed"])
		self.assertTrue(ccf.evaluate(ccf.parse_tree(dry_ahead(1.0)), values)["passed"], "nothing near an inch")

	def test_bad_parameters_are_refused_at_save(self):
		for bad in (0, -1, 6, "0.05", True):
			with self.assertRaisesRegex(ccf.TreeError, "over is a daily rain amount"):
				ccf.parse_tree(dry_ahead(bad))
		with self.assertRaisesRegex(ccf.TreeError, "reduces rain"):
			ccf.parse_tree({"path": "weather.forecast.daily[0..6].precip_in", "agg": "chance_over", "op": "lt", "value": 40})
		with self.assertRaisesRegex(ccf.TreeError, "read through an agg"):
			ccf.parse_tree({"path": "weather.forecast.daily[0].rain", "op": "lt", "value": 40})
		with self.assertRaisesRegex(ccf.TreeError, "the value must be one"):
			ccf.parse_tree({**dry_ahead(), "value": "40"})

	def test_the_field_map_lists_the_aggregation(self):
		data = self.tool_data("get_compliance_field_map", {})
		self.assertIn("chance_over", [a["agg"] for a in data["aggregations"]])
		self.assertIn("weather.forecast.daily[].rain", [c["path"] for c in data["context"]])
		self.assertIn("weather_check.rained", [c["path"] for c in data["context"]])


class ThePruningDefaults(GoHoldTestCase):
	def test_seven_days_ahead_over_a_twentieth_of_an_inch_and_forty_eight_hours_dry(self):
		spec = next(s for s in go_hold.preset_specs() if s["rule_id"] == CANKER)
		leaves = {leaf["id"]: leaf for leaf in spec["condition_tree"]["all"]}
		self.assertEqual(leaves["dry_ahead"]["path"], "weather.forecast.daily[0..6].rain")
		self.assertEqual((leaves["dry_ahead"]["over"], leaves["dry_ahead"]["value"]), (0.05, 40))
		self.assertEqual((leaves["dry_since"]["path"], leaves["dry_since"]["value"]), ("weather.recent.hours_since_rain", 48))
		ccf.parse_tree(spec["condition_tree"])

	def test_rain_yesterday_holds_pruning_for_forty_eight_hours(self):
		self.a_rule()
		task = self.pruning()
		wet_yesterday = forecast(False)
		wet_yesterday["recent"]["hours_since_rain"] = 30.0
		with mock.patch.object(ccf_providers, "_weather", return_value=wet_yesterday):
			data = self.tool_data("check_go_hold", {"task": task})
		self.assertEqual(data["status"], "Hold")
		self.assertIn("48 hours", data["reasons"][0])

	def test_the_start_records_the_rain_it_bet_against(self):
		self.a_rule()
		task = self.pruning()
		with self.weather(wet=False):
			self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		rule = json.loads(STORE.get_raw("Farm Task", task)["go_hold_log"])[0]["rules"][0]
		bet = rule["rain_forecast"]
		self.assertEqual((bet["from"], bet["days"], bet["over_in"], bet["check"]), (TODAY, 4, 0.05, "dry_ahead"))
		self.assertEqual(bet["risk_pct"], 0.0)


class TheForecastClearsAHold(GoHoldTestCase):
	def setUp(self):
		super().setUp()
		self.on = mock.patch.object(ccf_providers, "forecast_enabled", return_value=True)
		self.on.start()
		self.addCleanup(self.on.stop)

	def test_a_hold_clears_on_the_next_forecast_refresh_not_tomorrow_morning(self):
		self.a_rule()
		task = self.pruning()
		with self.weather(wet=True):
			go_hold.day_start()
			again = go_hold.forecast_refresh()
		self.assertEqual((again["checked"], again["changed"]), (1, 0))
		log = json.loads(STORE.get_raw("Farm Task", task)["go_hold_log"])
		self.assertEqual(len(log), 1, "an unchanged verdict writes nothing")
		with self.weather(wet=False):
			report = go_hold.forecast_refresh()
		self.assertEqual((report["cleared"], report["changed"]), (1, 1))
		row = STORE.get_raw("Farm Task", task)
		self.assertEqual(row["go_hold"], "Go")
		entry = json.loads(row["go_hold_log"])[0]
		self.assertEqual((entry["moment"], entry["cleared"]), ("forecast_refresh", True))

	def test_nothing_runs_without_a_weather_rule_or_with_forecasts_off(self):
		self.pruning()
		self.assertEqual(go_hold.forecast_refresh()["checked"], 0)
		self.a_rule()
		with mock.patch.object(ccf_providers, "forecast_enabled", return_value=False):
			self.assertEqual(go_hold.forecast_refresh()["checked"], 0)

	def test_it_is_scheduled_hourly(self):
		from erpnext_mcp import hooks

		self.assertIn("erpnext_mcp.go_hold.scheduled_forecast_refresh", hooks.scheduler_events["hourly"])
		self.assertIn("erpnext_mcp.weather_verify.daily", hooks.scheduler_events["cron"]["45 4 * * *"])
		self.assertIn("erpnext_mcp.tools.maintenance.sweep_due_maintenance", hooks.scheduler_events["cron"]["30 4 * * *"])


class DidItActuallyRain(GoHoldTestCase):
	"""Decision 21: the work's forecast, scored against what fell."""

	def setUp(self):
		super().setUp()
		for patcher in (mock.patch.object(ccf_providers, "forecast_enabled", return_value=True),
		                mock.patch.object(ccf_providers, "block_point", return_value=(45.6, -121.2, "B7"))):
			patcher.start()
			self.addCleanup(patcher.stop)

	def pruned(self, days_ago=10, risk=20.0):
		self.a_rule()
		task = self.pruning()
		start = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=days_ago)).isoformat()
		end = (datetime.date.fromisoformat(start) + datetime.timedelta(days=6)).isoformat()
		log = [{"at": f"{start} 07:00:00", "moment": "task_start", "status": "Go",
		        "rules": [{"rule_id": CANKER, "version": 1, "verdict": "Go",
		                   "rain_forecast": {"from": start, "to": end, "days": 7, "over_in": 0.05, "risk_pct": risk,
		                                     "check": "dry_ahead"}}]}]
		frappe.db.set_value("Farm Task", task, {"state": "Completed", "completed_at": f"{start} 15:00:00",
		                                         "go_hold_log": json.dumps(log)})
		return task, start, end

	def fell(self, start, amounts):
		day = datetime.date.fromisoformat(start)
		return {(day + datetime.timedelta(days=i)).isoformat(): a for i, a in enumerate(amounts)}

	def test_rain_two_days_after_is_recorded_on_the_task_and_alerts_through_the_rule(self):
		task, start, _ = self.pruned()
		with mock.patch.object(weather_verify, "actual_daily",
		                       return_value=(self.fell(start, [0, 0, 0.32, 0.01, 0, 0, 0]), "Open-Meteo archive", True)):
			report = weather_verify.run()
		self.assertEqual((report["checked"], report["rained"], report["provisional"]), (1, 1, 0))
		entry = json.loads(STORE.get_raw("Farm Task", task)["go_hold_log"])[0]
		self.assertEqual(entry["moment"], "archive_check")
		self.assertTrue(entry["final"] and entry["rained"])
		self.assertEqual((entry["max_in"], entry["forecast_risk_pct"], entry["brier"]), (0.32, 20.0, 0.64))
		self.assertEqual(len(entry["wet_days"]), 1)

		weather_verify.seed()
		row = next(r for r in compliance_rules.rule_rows(include_inactive=True) if r["rule_id"] == weather_verify.RULE_ID)
		self.assertFalse(int(row.get("enabled") or 0), "seeded OFF")
		frappe.db.set_value(compliance_rules.DOCTYPE, row["name"], {"enabled": 1, "human_approved_by": "Administrator",
		                                                           "human_approved_on": frappe.utils.now()})
		rules, _ = engine.rule_set()
		found = rules[weather_verify.RULE_ID].scan({"today": TODAY, "company": ""})
		self.assertEqual([o.source_docname for o in found], [task])
		self.assertIn("Check the cuts", found[0].message)

	def test_a_dry_week_raises_nothing_and_scores_well(self):
		task, start, _ = self.pruned(risk=20.0)
		with mock.patch.object(weather_verify, "actual_daily", return_value=(self.fell(start, [0] * 7), "Open-Meteo archive", True)):
			weather_verify.run()
		weather_verify.seed()
		row = next(r for r in compliance_rules.rule_rows(include_inactive=True) if r["rule_id"] == weather_verify.RULE_ID)
		frappe.db.set_value(compliance_rules.DOCTYPE, row["name"], {"enabled": 1, "human_approved_by": "Administrator",
		                                                           "human_approved_on": frappe.utils.now()})
		rules, _ = engine.rule_set()
		self.assertEqual(rules[weather_verify.RULE_ID].scan({"today": TODAY, "company": ""}), [])
		data = self.tool_data("get_forecast_verification", {"days": 30})
		self.assertEqual((data["tasks_checked"], data["rained"], data["brier"]), (1, 0, 0.04))
		self.assertEqual(data["checks"][0]["task"], task)
		self.assertEqual(data["reliability"], [{"forecast_pct": "20–40", "tasks": 1, "mean_forecast_pct": 20.0, "rained_pct": 0.0}])

	def test_provisional_until_the_archive_catches_up_then_final(self):
		task, start, _ = self.pruned(days_ago=8)
		with mock.patch.object(weather_verify, "actual_daily",
		                       return_value=(self.fell(start, [0] * 7), "Open-Meteo forecast API, past days (provisional)", False)):
			self.assertEqual(weather_verify.run()["provisional"], 1)
		with mock.patch.object(weather_verify, "actual_daily",
		                       return_value=(self.fell(start, [0, 0.1, 0, 0, 0, 0, 0]), "Open-Meteo archive", True)):
			weather_verify.run()
			self.assertEqual(weather_verify.run()["checked"], 0, "a final check is not repeated")
		log = json.loads(STORE.get_raw("Farm Task", task)["go_hold_log"])
		checks = [e for e in log if e["moment"] == "archive_check"]
		self.assertEqual(len(checks), 1, "the archive replaces the provisional check")
		self.assertTrue(checks[0]["final"] and checks[0]["rained"])

	def test_a_window_not_yet_over_is_not_judged(self):
		task, _start, _ = self.pruned(days_ago=3)
		with mock.patch.object(weather_verify, "actual_daily") as fetch:
			self.assertEqual(weather_verify.run()["checked"], 0)
		fetch.assert_not_called()
		self.assertNotIn("archive_check", STORE.get_raw("Farm Task", task)["go_hold_log"])

	def test_nothing_checked_says_how_it_will_be(self):
		data = self.tool_data("get_forecast_verification", {})
		self.assertEqual(data["tasks_checked"], 0)
		self.assertIn("04:45", data["note"])
		self.assertIn("730", self.tool_error("get_forecast_verification", {"days": 0}))


class AnUntouchedPresetTakesTheNewDefaults(GoHoldTestCase):
	"""v0.255.0. OML seeded the pruning preset at v0.240.0; seeding is create-only."""

	def an_old_preset(self, **overrides):
		spec = next(s for s in go_hold.preset_specs() if s["rule_id"] == CANKER)
		spec = {**spec, "condition_tree": go_hold.SUPERSEDED_TREES[CANKER][0], **overrides}
		return compliance_rules.build_rule(spec).insert(ignore_permissions=True).name

	def tree(self, name):
		return json.loads(frappe.db.get_value(compliance_rules.DOCTYPE, name, "condition_tree_json"))

	def test_the_old_tree_is_replaced_and_the_rule_stays_off(self):
		name = self.an_old_preset()
		self.assertEqual(go_hold.refresh_presets(), [CANKER])
		leaves = {leaf["id"]: leaf for leaf in self.tree(name)["all"]}
		self.assertEqual((leaves["dry_ahead"]["agg"], leaves["dry_since"]["value"]), ("chance_over", 48))
		self.assertFalse(int(frappe.db.get_value(compliance_rules.DOCTYPE, name, "enabled") or 0))
		self.assertEqual(go_hold.refresh_presets(), [], "once")

	def assert_left_alone(self, **overrides):
		name = self.an_old_preset(**overrides)
		before = self.tree(name)
		self.assertEqual(go_hold.refresh_presets(), [])
		self.assertEqual(self.tree(name), before)

	def test_an_enabled_approved_preset_is_left_alone(self):
		self.assert_left_alone(enabled=1, human_approved_by="Administrator", human_approved_on=frappe.utils.now())

	def test_an_edited_tree_is_left_alone(self):
		edited = go_hold.SUPERSEDED_TREES[CANKER][0]
		self.assert_left_alone(condition_tree={"all": [dict(edited["all"][0], value=30)] + edited["all"][1:]})
