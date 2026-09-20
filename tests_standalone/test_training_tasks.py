# SPDX-License-Identifier: MIT
"""v0.176.0 — a training IS a Farm Task, and the task is the record the phone holds.

TIM'S CALL, AND THE ONE IT REPLACES. The first attempt had Training Sessions
generating Farm Tasks on a nightly sweep: two records, a link between them, and a
handset that had to understand both. The shape shipped here is the other way
round — Farm Task is the atom, `task_type` is "Training", and the class is
something a task REFERENCES for the formal compliance record.

WHAT FARM TASK CANNOT DO, WHICH IS WHY THE REFERENCE STAYS. The doctype has no
date column (`reported_at` and `observed_at` mean other things), no provider and
no instructor, its `location` is a Dynamic Link into a farm register — a cabin, a
block, a zone, never a community college two counties away — and it has no
attendee table. So a Training task cannot CARRY a class's details, and copying
them onto new columns would be a second register of the same afternoon that
disagrees with the first the moment somebody moves a class in the Desk.

SO THE SERVER DENORMALISES AT READ TIME. `_training_details` reads the session
when the task is described and puts the day, the venue, the provider and the head
count on the payload. One record on the phone, one copy of the facts on the
server, no second fetch and no second doctype for iOS to know about.

FOUR CLAIMS.

1. `TheTaskCarriesTheClass` — a Training task naming a session answers with the
   session's day, venue, provider and head count.
2. `EveryOtherTaskIsUnchanged` — no `training` key on anything else, and a
   Training task with no session behind it is legal (the tailgate talk).
3. `TheRefusals` — a session that does not exist, and a class named on a task
   that is not a training.
4. `TheCloseOut` — completing the task files the compliance record into the
   session, and a session nobody signed is named rather than marked Completed.
"""

import base64

import frappe

from erpnext_mcp import roles, training
from erpnext_mcp.api import shape
from erpnext_mcp.tools import dispatch

from .fixtures import MAIN, V12TestCase, install_hrms
from .harness import ROLES, STORE

TRAINEE = "HR-EMP-00002"
SECOND = "HR-EMP-00020"
SECOND_NAME = "Marco Vega"

CURRICULUM = "Heat Illness Prevention"
TOPICS = "Heat index, water, shade, symptoms, reporting, emergency response"

BEN_BADGE = "ETC-0002"
THIRD_BADGE = "ETC-0003"
BADGE_DOCTYPE = "Bucket Log Badge Map"

SIGNATURE = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"signature").decode()

ON = {
	f"allow_{name}": 1
	for name in (
		"create_training_session",
		"add_session_attendee",
		"sign_session_attendance",
		"complete_training_session",
		"get_training_session",
		"record_training",
		"resolve_badge",
		"create_farm_task",
		"claim_farm_task",
		"start_farm_task",
		"complete_farm_task",
		"get_farm_task",
	)
}


class TrainingTaskTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		roles.install_roles()
		training.seed_training_types()
		self.an_employee_at(MAIN, SECOND_NAME, SECOND)
		STORE.seed(
			BADGE_DOCTYPE,
			[
				{"name": BEN_BADGE, "badge_id": BEN_BADGE, "employee": TRAINEE, "company": MAIN, "active": 1},
				{"name": THIRD_BADGE, "badge_id": THIRD_BADGE, "employee": SECOND, "company": MAIN, "active": 1},
			],
		)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	def an_employee_at(self, company: str, name: str, docname: str) -> str:
		STORE.seed(
			"Employee",
			[
				{
					"name": docname,
					"employee_name": name,
					"status": "Active",
					"date_of_joining": "2025-01-01",
					"company": company,
				}
			],
		)
		return docname

	# ── fixtures ────────────────────────────────────────────────────────────
	def a_class(self, **overrides) -> str:
		"""One session, dated today — `record_training` refuses a record dated in
		the future, which is §112.161(a)(2) and not this file's subject."""
		payload = {
			"training_type": CURRICULUM,
			"company": MAIN,
			"session_date": frappe.utils.today(),
			"content_topics_covered": TOPICS,
			"location": "Pine Grove Grange, 2935 Van Horn Dr., Hood River, OR 97031",
			"instructor_name": "Kris Schaedel",
			"provider": "Columbia Gorge Community College",
			"start_time": "08:00:00",
			"end_time": "17:00:00",
		}
		payload.update(overrides)
		return self.tool_data("create_training_session", payload)["name"]

	def signed_up(self, session: str) -> str:
		for badge in (BEN_BADGE, THIRD_BADGE):
			self.tool_data("add_session_attendee", {"session": session, "badge_scan": badge})
		for person in (TRAINEE, SECOND):
			self.tool_data(
				"sign_session_attendance",
				{"session": session, "employee": person, "signature": SIGNATURE},
			)
		return session

	def a_training_task(self, session: str = "", **overrides) -> dict:
		payload = {
			"task_name": "Applicator licence renewal",
			"task_type": "Training",
			"company": MAIN,
			"evidence_required": {"findings_text": True},
			"assigned_to": TRAINEE,
		}
		if session:
			payload["training_session"] = session
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)

	def finish(self, task: str, worker: str = TRAINEE) -> dict:
		self.tool_data("start_farm_task", {"task": task, "worker_id": worker})
		return self.tool_data(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": worker,
				"findings_text": "held it",
				"completion_narrative": "held it",
			},
		)


# ── 1. the task carries the class ───────────────────────────────────────────
class TheTaskCarriesTheClass(TrainingTaskTestCase):
	def test_the_payload_answers_with_the_day_the_venue_and_the_provider(self):
		"""**THE WHOLE POINT OF THE SHAPE.** Farm Task has no date column, no
		venue that can hold an address and no provider — so without this block a
		Training task on a phone says only that a training exists."""
		session = self.signed_up(self.a_class())
		task = self.a_training_task(session)
		described = self.tool_data("get_farm_task", {"task": task["name"]})
		payload = described.get("task", described)

		self.assertEqual(payload["training"]["session"], session)
		self.assertEqual(payload["training"]["session_date"], frappe.utils.today())
		self.assertIn("Pine Grove Grange", payload["training"]["venue"])
		self.assertEqual(payload["training"]["provider"], "Columbia Gorge Community College")
		self.assertEqual(payload["training"]["instructor"], "Kris Schaedel")
		self.assertEqual(payload["training"]["attendee_count"], 2)
		self.assertEqual(payload["training"]["signed_count"], 2)

	def test_the_reference_is_the_subject_pair_and_not_a_new_column(self):
		"""No schema change: the task uses the link the doctype already had for
		'the record this task is about'."""
		session = self.a_class()
		task = self.a_training_task(session)
		row = frappe.db.get_value(
			"Farm Task", task["name"], ["subject_doctype", "subject_docname"], as_dict=True
		)
		self.assertEqual(row["subject_doctype"], "Training Session")
		self.assertEqual(row["subject_docname"], session)

	def test_the_handset_shape_carries_it_too(self):
		"""`shape.task` is what a phone decodes, and it is a strict projection —
		a key that is not listed there does not reach the app however correct the
		tool's own payload is."""
		session = self.signed_up(self.a_class())
		task = self.a_training_task(session)
		row = dispatch.task_row(task["name"])
		row["training"] = dispatch._training_details(row)

		projected = shape.task(row)
		self.assertEqual(projected["training"]["session"], session)
		self.assertEqual(projected["subject_docname"], session)

	def test_what_still_stands_between_the_class_and_its_records(self):
		"""A foreman learns it while everybody is still in the room, rather than
		from a refusal afterwards."""
		session = self.a_class()
		self.tool_data("add_session_attendee", {"session": session, "badge_scan": BEN_BADGE})
		task = self.a_training_task(session)
		described = self.tool_data("get_farm_task", {"task": task["name"]})
		payload = described.get("task", described)

		blockers = payload["training"]["completion_blockers"]
		self.assertTrue(any("no attendee is ready" in text for text in blockers), blockers)

	def test_a_class_that_was_deleted_does_not_take_the_task_down(self):
		"""A dispatch board that will not draw because a training register is
		missing is a worse failure than a task with no details on it."""
		session = self.a_class()
		task = self.a_training_task(session)
		frappe.delete_doc("Training Session", session, force=True)

		described = self.tool_data("get_farm_task", {"task": task["name"]})
		payload = described.get("task", described)
		self.assertIsNone(payload.get("training"))
		self.assertEqual(payload["name"], task["name"])


# ── 2. everything else is untouched ─────────────────────────────────────────
class EveryOtherTaskIsUnchanged(TrainingTaskTestCase):
	def test_an_ordinary_task_carries_no_training_key(self):
		plain = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Walk cabin 3",
				"task_type": "Inspection",
				"company": MAIN,
				"evidence_required": {"photos": True},
			},
		)
		self.assertNotIn("training", plain)
		self.assertNotIn("subject_doctype", plain)

	def test_a_tailgate_talk_is_a_training_task_with_no_session(self):
		"""**LEGAL, AND NOT AN OVERSIGHT.** The five-minute talk a foreman gives
		at the end of a row is a Training task that will never have a formal
		session behind it. Refusing it would push that work off the board it was
		put on."""
		task = self.a_training_task()
		self.assertEqual(task["task_type"], "Training")
		self.assertNotIn("training", task)

	def test_finishing_a_tailgate_talk_closes_nothing_and_says_nothing(self):
		task = self.a_training_task()
		answer = self.finish(task["name"])
		self.assertIsNone(answer.get("training_session"))
		self.assertNotIn("training_session", shape.completion(answer))


# ── 3. what it refuses ──────────────────────────────────────────────────────
class TheRefusals(TrainingTaskTestCase):
	def test_a_session_nobody_has_heard_of_is_refused(self):
		error = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Applicator licence renewal",
				"task_type": "Training",
				"company": MAIN,
				"evidence_required": {"findings_text": True},
				"training_session": "TRNS-2026-9999",
			},
		)
		self.assertIn("no Training Session", error)
		self.assertIn("Nothing was created", error)

	def test_a_class_named_on_work_that_is_not_a_training_is_refused(self):
		"""Somebody meaning something the record cannot hold — an Inspection with
		a class on it would carry details no screen reads and no completion
		closes."""
		session = self.a_class()
		error = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Walk cabin 3",
				"task_type": "Inspection",
				"company": MAIN,
				"evidence_required": {"photos": True},
				"training_session": session,
			},
		)
		self.assertIn("belongs to a 'Training' task", error)


# ── 4. the close-out ────────────────────────────────────────────────────────
class TheCloseOut(TrainingTaskTestCase):
	def test_completing_the_task_files_the_training_records(self):
		"""The formal compliance record is the Training Session's, and this is
		what writes it: one Employee Training Record per provable attendance."""
		session = self.signed_up(self.a_class())
		task = self.a_training_task(session)

		answer = self.finish(task["name"])
		self.assertTrue(answer["training_session"]["completed"])
		self.assertEqual(answer["training_session"]["records_created"], 2)
		self.assertEqual(frappe.db.get_value("Training Session", session, "status"), "Completed")

	def test_the_phone_is_told_what_the_class_filed(self):
		session = self.signed_up(self.a_class())
		task = self.a_training_task(session)
		self.tool_data("start_farm_task", {"task": task["name"], "worker_id": TRAINEE})
		raw = dispatch.complete_farm_task(
			{
				"task": task["name"],
				"worker_id": TRAINEE,
				"findings_text": "held it",
				"completion_narrative": "held it",
			}
		)

		projected = shape.completion(raw.data)
		self.assertEqual(projected["training_session"], session)
		self.assertTrue(projected["training_session_completed"])

	def test_a_class_nobody_signed_is_named_rather_than_marked_completed(self):
		"""**THE CLAIM THAT KEEPS THIS HONEST.** A task completion is not an
		attendance record. Closing the class regardless would mark an afternoon
		Completed with NOTHING under it — a training the compliance matrix
		believes happened, which is worse than one it knows is outstanding."""
		session = self.a_class()
		self.tool_data("add_session_attendee", {"session": session, "badge_scan": BEN_BADGE})
		task = self.a_training_task(session)

		answer = self.finish(task["name"])
		self.assertFalse(answer["training_session"]["completed"])
		self.assertTrue(answer["training_session"]["blocked"])
		self.assertIn("no attendee is ready", answer["training_session"]["note"])
		self.assertEqual(frappe.db.get_value("Training Session", session, "status"), "Scheduled")

	def test_the_task_itself_still_completes(self):
		"""Taking the completion away from somebody because the paperwork is
		short teaches them not to file it. The work of holding the class was
		done."""
		session = self.a_class()
		self.tool_data("add_session_attendee", {"session": session, "badge_scan": BEN_BADGE})
		task = self.a_training_task(session)

		self.assertEqual(self.finish(task["name"])["final_state"], "Completed")
