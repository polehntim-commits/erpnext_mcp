"""The irrigation schedule. v0.248.0 (approved queue item 9; decision 18)."""

import datetime

from erpnext_mcp import irrigation_schedule

from .fixtures import MAIN
from .harness import STORE
from .test_irrigation_runtime import ALL_ON as RUNTIME_ON, RuntimeTestCase

ON = {**RUNTIME_ON, "allow_set_irrigation_schedule": 1, "allow_get_irrigation_schedule": 1, "irrigation_schedule_enabled": 1}
MON = datetime.date(2026, 7, 6)  # a Monday


class ScheduleTestCase(RuntimeTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.a_valve()
		STORE.seed("Field", [{"name": "B7", "field_name": "B7"}])
		STORE.seed("Irrigation Zone", [{"name": "Z3", "zone_name": "Zone 3", "field": "B7", "owning_entity": MAIN}])

	def schedule(self, *rows):
		return self.tool_data("set_irrigation_schedule", {"zone": "Z3", "schedule": list(rows)})


class TheSchedule(ScheduleTestCase):
	def test_days_read_three_ways_and_a_bad_one_is_refused(self):
		self.assertEqual(irrigation_schedule.parse_days("Mon, Wed Fri"), {"weekdays": [0, 2, 4]})
		self.assertEqual(irrigation_schedule.parse_days("daily"), {"weekdays": list(range(7))})
		self.assertEqual(irrigation_schedule.parse_days("every 3"), {"every": 3})
		self.assertIn("Mon Tue", self.tool_error("set_irrigation_schedule", {"zone": "Z3", "schedule": [
			{"valve": "MC-Valve-05", "days": "Moonday", "start_time": "06:00", "minutes": 120}]}))
		self.assertIn("no Asset Register", self.tool_error("set_irrigation_schedule", {"zone": "Z3", "schedule": [
			{"valve": "Nope", "days": "daily", "start_time": "06:00", "minutes": 120}]}))

	def test_the_plan_for_a_week_respects_days_and_season(self):
		self.schedule({"valve": "MC-Valve-05", "days": "Mon Thu", "start_time": "6:00", "minutes": 120,
		               "season_start": "2026-07-08"})
		plan = irrigation_schedule.planned("Z3", MON, MON + datetime.timedelta(days=6))
		self.assertEqual([(p["date"], p["start_time"], p["minutes"]) for p in plan], [("2026-07-09", "06:00", 120)])


class RaisedAsWork(ScheduleTestCase):
	def test_each_set_becomes_one_irrigation_task_on_the_block(self):
		self.schedule({"valve": "MC-Valve-05", "days": "daily", "start_time": "05:30", "minutes": 90})
		made = irrigation_schedule.raise_for(MON)
		self.assertEqual(len(made), 1)
		task = STORE.get_raw("Farm Task", made[0])
		self.assertEqual((task["task_type"], task["location"], task["asset"], task["start_date"]),
		                 ("Irrigation", "B7", "MC-Valve-05", "2026-07-06"))
		self.assertIn("05:30, 90 min", task["task_name"])
		self.assertEqual(irrigation_schedule.raise_for(MON), [], "once per set and day")

	def test_off_until_switched_on(self):
		self.configure(enabled=1, **{**ON, "irrigation_schedule_enabled": 0})
		self.schedule({"valve": "MC-Valve-05", "days": "daily", "start_time": "05:30", "minutes": 90})
		self.assertEqual(irrigation_schedule.raise_for(MON), [])


class PlannedVersusRan(ScheduleTestCase):
	def test_done_short_missed_and_extra_from_the_valve_log(self):
		self.schedule({"valve": "MC-Valve-05", "days": "Mon Tue Wed", "start_time": "06:00", "minutes": 120})
		self.event("MC-Valve-05", "open", "2026-07-06 06:00:00")
		self.event("MC-Valve-05", "closed", "2026-07-06 07:55:00")   # 115 of 120: done (90%+)
		self.event("MC-Valve-05", "open", "2026-07-07 06:00:00")
		self.event("MC-Valve-05", "closed", "2026-07-07 07:00:00")   # 60 of 120: short
		self.event("MC-Valve-05", "open", "2026-07-09 06:00:00")
		self.event("MC-Valve-05", "closed", "2026-07-09 06:30:00")   # Thursday: extra
		data = self.tool_data("get_irrigation_schedule", {"zone": "Z3", "from": "2026-07-06", "to": "2026-07-12"})
		got = {d["date"]: d["status"] for d in data["days"]}
		self.assertEqual(got, {"2026-07-06": "done", "2026-07-07": "short", "2026-07-08": "missed", "2026-07-09": "extra"})
		self.assertEqual((data["planned_minutes"], data["ran_minutes"]), (360, 205.0))
