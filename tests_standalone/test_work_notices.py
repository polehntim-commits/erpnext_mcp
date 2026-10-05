"""No-work notices. v0.243.0 (approved queue item 5; decisions 29, 34)."""

import datetime
from unittest import mock

import frappe

from erpnext_mcp import ccf_providers, work_notices

from .harness import STORE
from .test_go_hold import ON, TODAY, GoHoldTestCase
from .test_suggested_tasks import windy_tomorrow

NOTICES_ON = {**ON, "allow_list_work_notices": 1, "allow_send_work_notice": 1, "work_notices_enabled": 1,
              "work_notice_managers": "boss@example.com"}
TOMORROW = (datetime.date.fromisoformat(TODAY) + datetime.timedelta(days=1)).isoformat()


def calm() -> dict:
	values = windy_tomorrow()
	for row in values["forecast"]["daily"]:
		row["wind_mph"] = 5.0
	return values


class NoticeTestCase(GoHoldTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **NOTICES_ON)
		# `preferred_language` is a Custom Field: install it, as a site has it.
		from erpnext_mcp import compliance_fields

		compliance_fields.install_compliance_fields()
		STORE.seed("User", [{"name": u, "enabled": 1} for u in ("foreman@example.com", "boss@example.com")])
		STORE.seed("Employee", [
			{"name": "EMP-001", "employee_name": "Ana", "reports_to": "EMP-009", "preferred_language": "es"},
			{"name": "EMP-009", "employee_name": "Luis", "user_id": "foreman@example.com"},
			{"name": "EMP-010", "employee_name": "Boss", "user_id": "boss@example.com"},
		])
		self.pushes = []
		patcher = mock.patch.object(work_notices, "_push", side_effect=self.record_push)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.reach = True

	def record_push(self, employees, title, body, notice):
		self.pushes.append({"to": list(employees), "title": title, "body": body})
		return {"sent": 1 if self.reach else 0}

	def a_spray(self, enforced=True):
		self.a_rule(rule_id="go_hold_spray_wind", enforced=enforced)
		return self.claimed(task_name="Copper, Block 3", task_type="Spray")

	def drafted(self, values=None):
		with mock.patch.object(ccf_providers, "_weather", return_value=values or windy_tomorrow()):
			made = work_notices.draft()
		frappe.db.commit()  # the scheduler commits after its job
		return made


class Drafting(NoticeTestCase):
	def test_an_enforced_hold_tomorrow_drafts_one_notice_for_the_supervisor(self):
		task = self.a_spray()
		made = self.drafted()
		self.assertEqual(len(made), 1)
		notice = work_notices.describe(made[0])
		self.assertEqual((notice["status"], notice["task"], notice["for_date"]), ("Awaiting Supervisor", task, TOMORROW))
		self.assertEqual(notice["supervisor"], "EMP-009", "decision 29: reports to")
		self.assertEqual(notice["recipients"][0]["language"], "es")
		self.assertFalse(notice["recipients"][0]["sent"])
		self.assertIn("No hay Copper, Block 3", notice["text_es"])
		self.assertEqual(notice["next_possible"], (datetime.date.fromisoformat(TOMORROW) + datetime.timedelta(days=1)).isoformat())
		self.assertEqual(self.pushes[0]["to"], ["EMP-009"])
		self.assertEqual(self.drafted(), [], "one notice per task and day")

	def test_an_advisory_hold_drafts_nothing(self):
		self.a_spray(enforced=False)
		self.assertEqual(self.drafted(), [])

	def test_off_by_default(self):
		self.a_spray()
		self.configure(enabled=1, **{**NOTICES_ON, "work_notices_enabled": 0})
		self.assertEqual(self.drafted(), [])


class NeverSentWithoutAnAnswer(NoticeTestCase):
	def test_unanswered_at_the_cutoff_goes_to_management_and_reaches_no_worker(self):
		self.a_spray()
		name = self.drafted()[0]
		self.pushes.clear()
		self.assertEqual(work_notices.escalate(), [name])
		notice = work_notices.describe(name)
		self.assertEqual(notice["status"], "Escalated")
		self.assertEqual(notice["escalated_to"], ["boss@example.com"])
		self.assertEqual([p["to"] for p in self.pushes], [["EMP-010"]])
		self.assertFalse(any(r["sent"] for r in notice["recipients"]))

	def test_the_hourly_run_drafts_before_the_cutoff_and_escalates_at_it(self):
		self.a_spray()
		at = lambda hhmm: mock.patch.object(work_notices, "_now", return_value=frappe.utils.get_datetime(f"{TODAY} {hhmm}:00"))
		with mock.patch.object(ccf_providers, "_weather", return_value=windy_tomorrow()):
			with at("16:00"):
				work_notices.hourly()
			self.assertEqual(STORE.rows("Work Notice"), [])
			with at("17:05"):
				work_notices.hourly()
			self.assertEqual([r["status"] for r in STORE.rows("Work Notice")], ["Awaiting Supervisor"])
			with at("18:00"):
				work_notices.hourly()
		self.assertEqual([r["status"] for r in STORE.rows("Work Notice")], ["Escalated"])


class Answering(NoticeTestCase):
	def test_send_with_other_work_reaches_each_in_their_language(self):
		self.a_spray()
		name = self.drafted()[0]
		data = self.tool_data("send_work_notice", {"notice": name, "choice": "other_work",
		                                           "alternative": "brush pickup, Block 9, 7 am"})
		self.assertEqual(data["status"], "Other Work Sent")
		self.assertIn("En su lugar: brush pickup", data["text_es"])
		self.assertTrue(data["recipients"][0]["sent"])
		self.assertIn("En su lugar", self.pushes[-1]["body"], "Ana reads Spanish")
		self.assertEqual(data["not_reached"], [])

	def test_someone_not_reached_is_listed_to_call(self):
		self.a_spray()
		name = self.drafted()[0]
		self.reach = False
		data = self.tool_data("send_work_notice", {"notice": name, "choice": "send"})
		self.assertEqual(data["not_reached"], ["Ana"])
		self.assertIn("call", data["call"])

	def test_other_work_needs_the_work_and_only_the_supervisor_or_a_manager_answers(self):
		self.a_spray()
		name = self.drafted()[0]
		self.assertIn("other_work needs", self.tool_error("send_work_notice", {"notice": name, "choice": "other_work"}))
		with self.assertRaisesRegex(PermissionError, "not this notice's supervisor"):
			work_notices.answer(name, "send", "crew@example.com")
		self.assertEqual(work_notices.answer(name, "no_notice", "foreman@example.com")["status"], "No Notice")

	def test_a_lifted_hold_asks_for_a_resume_notice(self):
		self.a_spray()
		name = self.drafted()[0]
		self.tool_data("send_work_notice", {"notice": name, "choice": "send"})
		self.assertEqual(self.drafted(calm()), [name])
		self.assertEqual(work_notices.describe(name)["status"], "Lifted")
		data = self.tool_data("send_work_notice", {"notice": name, "choice": "resume"})
		self.assertEqual(data["status"], "Resume Sent")
		self.assertIn("vuelve a estar en pie", data["text_es"])

	def test_the_list_shows_what_needs_an_answer(self):
		self.a_spray()
		self.drafted()
		data = self.tool_data("list_work_notices", {"status": "open"})
		self.assertEqual((data["count"], data["need_an_answer"]), (1, 1))
