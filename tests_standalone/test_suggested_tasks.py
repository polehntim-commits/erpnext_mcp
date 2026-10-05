"""Suggested tasks. v0.242.0 (approved queue item 5; decision 14)."""

import datetime
from unittest import mock

from erpnext_mcp import ccf_providers, go_hold, suggested_tasks

from .harness import STORE
from .test_go_hold import ON, TODAY, GoHoldTestCase, forecast


def windy_tomorrow() -> dict:
	"""Calm today (5 mph), 15 mph tomorrow."""
	values = forecast(wet=False)
	daily = values["forecast"]["daily"]
	for row in daily:
		row["wind_mph"] = 5.0
	daily[1]["wind_mph"] = 15.0
	return values


class SuggestedTasks(GoHoldTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, "allow_list_suggested_tasks": 1})

	def test_the_forecast_is_seen_from_the_day_judged(self):
		values = forecast(wet=False)
		tomorrow = (datetime.date.fromisoformat(TODAY) + datetime.timedelta(days=1)).isoformat()
		shifted = ccf_providers.as_of(values, tomorrow)
		self.assertEqual(shifted["forecast"]["daily"][0]["date"], tomorrow)
		self.assertIs(ccf_providers.as_of(values, TODAY), values)

	def test_go_today_hold_tomorrow_comes_first_and_says_do_it_today(self):
		self.a_rule(rule_id="go_hold_spray_wind")
		self.a_rule()  # pruning
		spray = self.a_task(task_name="Copper, Block 3", task_type="Spray")["name"]
		prune = self.a_task(task_name="Prune Block 7", task_type="Maintenance")["name"]
		with mock.patch.object(ccf_providers, "_weather", return_value=windy_tomorrow()):
			go_hold.day_start()
			data = self.tool_data("list_suggested_tasks", {})
		kinds = {s["task"]: s for s in data["suggestions"]}
		self.assertEqual(data["suggestions"][0]["task"], spray)
		self.assertEqual(kinds[spray]["kind"], suggested_tasks.CLOSING)
		self.assertEqual(kinds[spray]["tomorrow"], "Hold")
		self.assertIn("hazlo hoy", kinds[spray]["why"]["es"])
		self.assertEqual(kinds[prune]["kind"], suggested_tasks.GO)
		self.assertEqual(data["window_closing"], 1)

	def test_a_hold_lifted_this_morning_is_just_cleared_and_a_held_task_is_not_suggested(self):
		self.a_rule()
		prune = self.a_task(task_name="Prune Block 7", task_type="Maintenance")["name"]
		with self.weather(wet=True):
			go_hold.day_start()
			self.assertEqual(self.tool_data("list_suggested_tasks", {})["count"], 0)
		with self.weather(wet=False):
			go_hold.day_start()
			data = self.tool_data("list_suggested_tasks", {})
		self.assertEqual([(s["task"], s["kind"]) for s in data["suggestions"]], [(prune, suggested_tasks.CLEARED)])

	def test_someone_elses_task_is_not_suggested_to_a_worker(self):
		self.a_rule()
		mine = self.claimed(task_name="Prune Block 7", task_type="Maintenance")
		theirs = self.claimed(worker="EMP-002", task_name="Prune Block 8", task_type="Maintenance")
		with self.weather(wet=False):
			go_hold.day_start()
			data = self.tool_data("list_suggested_tasks", {"worker": "EMP-001"})
		tasks = [s["task"] for s in data["suggestions"]]
		self.assertIn(mine, tasks)
		self.assertNotIn(theirs, tasks)
		self.assertTrue(STORE.get_raw("Farm Task", theirs))
