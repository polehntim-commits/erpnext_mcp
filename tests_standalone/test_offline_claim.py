# SPDX-License-Identifier: MIT
"""A claim made with no signal. v0.261.0 (Tim, 2026-10-06). See `erpnext_mcp/offline_claims.py`.

THE FOUR CASES TIM NAMED, AND EACH CLASS BELOW IS ONE:

1. `TwoOfflineClaims` — two phones take the same task offline. The first to reach the
   server holds it; the second is a second claim, flagged, never refused.
2. `SyncOrder` — what decides is the order claims REACH the server, not the phones'
   clocks: a claim tapped earlier but synced later is still the second.
3. `CompleteBeforeTheClaimSyncs` — the winner finished before the loser's queue drained
   (claim, start, complete in order). The loser's work lands on their own assignment and
   the finished task is not reopened.
4. `PayMinutesForBoth` — both assignments carry their minutes, and the cost split that
   payroll reads counts both.
"""

from .fixtures import MAIN, V12TestCase
from .harness import STORE

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_farm_task",
		"claim_farm_task",
		"start_farm_task",
		"pause_farm_task",
		"resume_farm_task",
		"complete_farm_task",
		"get_farm_task",
		"review_claim_conflict",
	)
}

ANA = "HR-EMP-00001"
BETO = "HR-EMP-00002"
PHOTO = [{"file_url": "/files/a.jpg", "evidence_type": "Photo"}]


class OfflineClaimCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		STORE.seed(
			"Employee",
			[
				{"name": ANA, "employee_name": "Ana Ramos", "company": MAIN, "status": "Active"},
				{"name": BETO, "employee_name": "Beto Cruz", "company": MAIN, "status": "Active"},
			],
		)
		self.task = self.tool_data(
			"create_farm_task",
			{"task_name": "Prune row 4", "task_type": "Other", "evidence_required": {"photos": True}, "company": MAIN},
		)["name"]

	def claim(self, worker, at="2026-07-24 07:00:00"):
		return self.tool_data(
			"claim_farm_task", {"task": self.task, "worker_id": worker, "offline": True, "claimed_at": at}
		)

	def mine(self, worker):
		return next(r for r in STORE.rows("Farm Task Assignment") if r["task"] == self.task and r["assigned_to"] == worker)

	def work(self, worker, minutes):
		"""Start and complete through the tools, then set the minutes (the harness clock ticks 1 s a call)."""
		self.tool_data("start_farm_task", {"task": self.task, "worker_id": worker})
		self.tool_data("complete_farm_task", {"task": self.task, "worker_id": worker, "evidence_files": PHOTO})
		self.mine(worker)["actual_duration_minutes"] = minutes


class TwoOfflineClaims(OfflineClaimCase):
	def test_the_first_holds_and_the_second_is_kept_not_refused(self):
		first = self.claim(ANA)
		second = self.claim(BETO)
		self.assertEqual(first["claim_outcome"], "held")
		self.assertEqual(second["claim_outcome"], "second")
		self.assertIn("Claimed offline by two people", second["claim_conflict"])
		self.assertIn("Ana Ramos", second["claim_conflict"])
		task = STORE.get_raw("Farm Task", self.task)
		self.assertEqual((task["state"], task["assigned_to"]), ("Claimed", ANA))
		self.assertTrue(self.mine(BETO)["second_claim"])
		self.assertIn("Beto Cruz", self.mine(ANA)["claim_conflict"])

	def test_an_online_claim_on_a_held_task_is_still_refused(self):
		self.claim(ANA)
		text = self.tool_error("claim_farm_task", {"task": self.task, "worker_id": BETO})
		self.assertIn("held by", text)

	def test_a_resent_offline_claim_is_one_second_claim(self):
		self.claim(ANA)
		self.claim(BETO)
		self.claim(BETO)
		self.assertEqual(sum(1 for r in STORE.rows("Farm Task Assignment") if r["assigned_to"] == BETO), 1)

	def test_the_flag_is_a_compliance_item_until_a_supervisor_reviews_it(self):
		from erpnext_mcp import offline_claims

		self.claim(ANA)
		self.claim(BETO)
		self.assertEqual(offline_claims.claim_values({"name": self.task}, {})["offline_conflicts"], 2)
		self.tool_data("review_claim_conflict", {"task": self.task, "note": "Beto did rows 4–6"})
		self.assertEqual(offline_claims.claim_values({"name": self.task}, {})["offline_conflicts"], 0)
		self.assertIn("Beto did rows", self.mine(BETO)["claim_conflict"])


class SyncOrder(OfflineClaimCase):
	def test_reaching_the_server_first_wins_whatever_the_phone_clocks_say(self):
		self.claim(BETO, at="2026-07-24 07:30:00")
		late = self.claim(ANA, at="2026-07-24 07:00:00")
		self.assertEqual(late["claim_outcome"], "second")
		self.assertEqual(STORE.get_raw("Farm Task", self.task)["assigned_to"], BETO)
		self.assertEqual(str(self.mine(ANA)["device_claimed_at"])[:16], "2026-07-24 07:00")

	def test_the_second_worker_starts_pauses_and_resumes_without_moving_the_task(self):
		self.claim(ANA)
		self.claim(BETO)
		self.tool_data("start_farm_task", {"task": self.task, "worker_id": BETO})
		self.assertEqual(self.mine(BETO)["state"], "In-Progress")
		self.assertEqual(STORE.get_raw("Farm Task", self.task)["state"], "Claimed")
		self.tool_data("pause_farm_task", {"task": self.task, "worker_id": BETO})
		self.tool_data("resume_farm_task", {"task": self.task, "worker_id": BETO})
		self.assertEqual(self.mine(BETO)["state"], "In-Progress")
		self.assertEqual(self.mine(ANA)["state"], "Claimed")
		self.assertEqual(STORE.get_raw("Farm Task", self.task)["state"], "Claimed")


class CompleteBeforeTheClaimSyncs(OfflineClaimCase):
	def test_the_late_queue_lands_on_its_own_row_and_the_task_stays_finished(self):
		self.claim(ANA)
		self.work(ANA, 40)
		self.assertEqual(STORE.get_raw("Farm Task", self.task)["state"], "Completed")
		# Beto's queue drains: claim, start, complete — in that order.
		self.assertEqual(self.claim(BETO)["claim_outcome"], "second")
		self.tool_data("start_farm_task", {"task": self.task, "worker_id": BETO})
		done = self.tool_data(
			"complete_farm_task",
			{"task": self.task, "worker_id": BETO, "evidence_files": PHOTO, "completion_narrative": "Rows 4–6"},
		)
		self.assertEqual(done["claim_outcome"], "second")
		self.assertEqual(STORE.get_raw("Farm Task", self.task)["state"], "Completed")
		beto = self.mine(BETO)
		self.assertEqual(beto["state"], "Completed")
		self.assertEqual(beto["completion_narrative"], "Rows 4–6")
		self.assertEqual(len(beto.get("evidence_files") or []), 1)

	def test_naming_the_holders_assignment_still_finds_your_own(self):
		self.claim(ANA)
		self.claim(BETO)
		holder = self.mine(ANA)["name"]
		self.tool_data("start_farm_task", {"assignment": holder, "worker_id": BETO})
		self.assertEqual(self.mine(BETO)["state"], "In-Progress")
		self.assertEqual(self.mine(ANA)["state"], "Claimed")

	def test_a_resent_completion_changes_nothing(self):
		self.claim(ANA)
		self.claim(BETO)
		self.work(BETO, 25)
		again = self.tool_data("complete_farm_task", {"task": self.task, "worker_id": BETO, "evidence_files": PHOTO})
		self.assertEqual(again["assignment"]["actual_duration_minutes"], 25)


class PayMinutesForBoth(OfflineClaimCase):
	def test_both_workers_keep_their_minutes(self):
		self.claim(ANA)
		self.claim(BETO)
		self.work(ANA, 40)
		self.work(BETO, 25)
		self.assertEqual(self.mine(ANA)["actual_duration_minutes"], 40)
		self.assertEqual(self.mine(BETO)["actual_duration_minutes"], 25)
		states = {r["assigned_to"]: r["state"] for r in STORE.rows("Farm Task Assignment") if r["task"] == self.task}
		self.assertEqual(states, {ANA: "Completed", BETO: "Completed"})
		# payroll_gl's labour split reads every assignment's minutes and excludes no state.
		from erpnext_mcp.tools import payroll_gl

		self.assertIn("actual_duration_minutes", payroll_gl._ASSIGNMENT_FIELDS)

	def test_a_rule_refusal_offline_is_kept_too(self):
		STORE.get_raw("Farm Task", self.task)["state"] = "Draft"
		out = self.claim(ANA)
		self.assertEqual(out["claim_outcome"], "second")
		self.assertIn("Draft", out["claim_conflict"])
		self.assertEqual(STORE.get_raw("Farm Task", self.task)["state"], "Draft")


from erpnext_mcp.api import mobile as mobile_api  # noqa: E402

from .test_api_mobile import MobileAPITestCase  # noqa: E402


class ThePhoneRoute(MobileAPITestCase):
	"""`claim_task` with `offline=1`: the answer says who holds it, and a resend is the same claim."""

	def test_a_late_offline_claim_comes_back_as_second_and_replays(self):
		STORE.seed("Employee", [{"name": BETO, "employee_name": "Beto Cruz", "company": MAIN, "status": "Active"}])
		task = self.a_task(task_type="Other", skill_required="", evidence_required={"photos": True})
		self.tool_data("claim_farm_task", {"task": task, "worker_id": BETO})
		self.be()
		out = mobile_api.claim_task(task=task, offline=1, claimed_at="2026-07-24 07:00:00", client_request_id="c-1")
		self.assertEqual(out["claim_outcome"], "second")
		self.assertTrue(out["second_claim"])
		self.assertEqual(out["state"], "Claimed")
		self.assertIn("Beto Cruz", out["claim_conflict"])
		self.assertEqual(out["claim_holder_name"], "Beto Cruz")
		again = mobile_api.claim_task(task=task, offline=1, claimed_at="2026-07-24 07:00:00", client_request_id="c-1")
		self.assertTrue(again["replayed"])

	def test_an_offline_claim_on_a_free_task_holds_it(self):
		task = self.a_task(task_type="Other", skill_required="", evidence_required={"photos": True})
		self.be()
		out = mobile_api.claim_task(task=task, offline=1, claimed_at="2026-07-24 07:00:00")
		self.assertEqual(out["claim_outcome"], "held")
		self.assertFalse(out["second_claim"])
