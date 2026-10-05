"""Each person's own Start / End of Day. v0.252.0 (approved queue item 3, part 2; decisions 27, 33)."""

import json

import frappe

from erpnext_mcp import daily_checks

from .harness import STORE
from .test_dispatch import ALL_ON, DispatchTestCase

ON = {**ALL_ON, "allow_create_task_from_template": 1, "personal_day_checks_enabled": 1}
PROOF = {"evidence_files": [{"file_url": "/files/x.jpg", "evidence_type": "Photo"}], "signature_file": "/files/s.png",
         "findings_text": "ok", "completion_narrative": "done"}


class DayChecks(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		daily_checks.seed_personal()
		STORE.seed("Asset Register", [{"name": "MC-Pump-01", "asset_type": "Pump", "company": None}])
		STORE.seed("Field", [{"name": "B7", "field_name": "B7"}])

	def start(self, **task):
		work = self.claimed(**task)
		return work, self.tool_data("start_farm_task", {"task": work, "worker_id": "EMP-001"})


class StartOfDay(DayChecks):
	def test_the_first_start_of_the_day_raises_it_once(self):
		_work, started = self.start(task_name="Walk the line")
		check = started["start_of_day"]
		self.assertTrue(check["created"])
		row = STORE.get_raw("Farm Task", check["task"])
		self.assertEqual(row["assigned_to"], "EMP-001")
		_again, second = self.start(task_name="Fix the gate")
		self.assertNotIn("start_of_day", second, "once per person per day")

	def test_off_until_switched_on(self):
		self.configure(enabled=1, **{**ON, "personal_day_checks_enabled": 0})
		_work, started = self.start(task_name="Walk the line")
		self.assertNotIn("start_of_day", started)


class EndOfDay(DayChecks):
	def test_hour_meters_and_stale_stages_are_asked_and_filed_on_completion(self):
		self.start(task_name="Run the pump", asset="MC-Pump-01")
		thin = self.claimed(task_name="Thin B7")
		STORE.get_raw("Farm Task", thin).update(location_doctype="Field", location="B7")
		self.tool_data("start_farm_task", {"task": thin, "worker_id": "EMP-001"})
		check = daily_checks.ensure_personal_check("EMP-001", daily_checks.PERSON_END)
		self.assertEqual((check["readings"], check["stages"]), (1, 1))
		fields = {f["key"]: f for f in json.loads(STORE.get_raw("Farm Task", check["task"])["form_schema"])}
		self.assertIn("hours_mc_pump_01", fields)
		self.assertIn("stage_b7", fields)
		self.assertIn("B7", fields["stage_b7"]["label"]["en"])
		answers = {key: True for key, f in fields.items() if f["type"] == "check"}
		answers.update(hours_mc_pump_01=1234.5, stage_b7=55)
		self.tool_data("start_farm_task", {"task": check["task"], "worker_id": "EMP-001"})
		done = self.tool_data("complete_farm_task", {"task": check["task"], "worker_id": "EMP-001", "form_answers": answers, **PROOF})
		self.assertTrue(done["end_of_day"]["readings"][0]["recorded"])
		self.assertEqual(done["end_of_day"]["stages"][0]["bbch"], "55")
		logs = [r for r in STORE.rows("Asset State Log") if r.get("asset_name") == "MC-Pump-01"]
		self.assertEqual(float(logs[-1]["engine_hours"]), 1234.5)


from .test_shifts import ON as SHIFT_ON, WORKER, ShiftTestCase, at  # noqa: E402


class ClockingOut(ShiftTestCase):
	def test_clocking_out_raises_the_end_of_day_once(self):
		self.configure(enabled=1, **{**SHIFT_ON, "personal_day_checks_enabled": 1, "allow_create_task_from_template": 1})
		daily_checks.seed_personal()
		shift = self.start(crew_employees=[WORKER])["name"]
		data = self.tool_data("remove_worker_from_shift", {"shift": shift, "employee": WORKER, "left_at": at(11)})
		self.assertTrue(data["end_of_day"]["created"])
		self.assertEqual(STORE.get_raw("Farm Task", data["end_of_day"]["task"])["assigned_to"], WORKER)
		self.assertEqual(daily_checks.ensure_personal_check(WORKER, daily_checks.PERSON_END)["created"], False)
