# SPDX-License-Identifier: MIT
"""v0.193.0 — the feedback loop goes both ways, and a late training certificate.

TELL_THE_FARM_AUDIT.md F5: a worker could file a note and never learn it was
read. These drive the three new phone routes over the transport a handset uses,
as the worker who filed, a second worker, and a Farm Manager — because every
claim below is a gate, and a test that only drove the manager would assert the
feature and none of the gate.

1. A worker reads their own notes, with every reply, and only their own.
2. A Farm Manager reads the entity's feed and answers it; nobody else does.
3. A reply is a Task Note on the note (`narrative.NARRATIVE_PARENTS`), so the
   MCP `add_task_note` writes one too, and a bench's Document rows read back.
4. A certificate that arrives after the record lands on the record: the File is
   attached to it and `certificate_file` names it.
"""

import base64

import frappe

from erpnext_mcp.tools import narrative

from .fixtures import MAIN, OTHER
from .harness import STORE
from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER_EMPLOYEE
from .test_app_feedback import UUID, AppFeedbackTestCase
from .test_farmops_api import PREFIX

LIST = f"{PREFIX}/mobile/list_app_feedback"
REPLY = f"{PREFIX}/mobile/reply_to_app_feedback"
CERT = f"{PREFIX}/mobile/attach_training_certificate"

MANAGER = "mia@example.test"
MANAGER_EMPLOYEE = "EMP-MIA"
MATE = "cal@example.test"
MATE_EMPLOYEE = "EMP-CAL-FB"
FOREMAN = "flo@example.test"
FOREMAN_EMPLOYEE = "EMP-FLO-FB"

SECOND_UUID = "AAAABBBB-1111-2222-3333-444455556666"


class FeedbackRepliesTestCase(AppFeedbackTestCase):
	def setUp(self):
		super().setUp()
		STORE.seed(
			"Employee",
			[
				{
					"name": MANAGER_EMPLOYEE,
					"employee_name": "Mia Lund",
					"user_id": MANAGER,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": MATE_EMPLOYEE,
					"employee_name": "Cal Reyes",
					"user_id": MATE,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": FOREMAN_EMPLOYEE,
					"employee_name": "Flo Diaz",
					"user_id": FOREMAN,
					"company": MAIN,
					"status": "Active",
				},
			],
		)
		self.manager = self.enrol(email=MANAGER, name="Mia Lund", role="Farm Manager")
		self.mate = self.enrol(email=MATE, name="Cal Reyes")
		self.foreman = self.enrol(email=FOREMAN, name="Flo Diaz", role="Foreman")

	def a_note(self) -> str:
		return self.file()["name"]


# ── 1. the worker's own notes ────────────────────────────────────────────────
class AWorkerReadsTheirOwnNotes(FeedbackRepliesTestCase):
	def test_a_filed_note_comes_back_open_with_no_replies(self):
		name = self.a_note()
		data = self.message(LIST)
		self.assertEqual(data["scope"], "mine")
		self.assertEqual([note["name"] for note in data["app_feedback"]], [name])
		note = data["app_feedback"][0]
		self.assertEqual(note["entry_uuid"], UUID)
		self.assertTrue(note["is_open"])
		self.assertEqual(note["replies"], [])
		self.assertEqual(note["reply_count"], 0)

	def test_a_managers_reply_reaches_the_worker(self):
		name = self.a_note()
		self.message(REPLY, {"name": name, "reply": "Fixed in the next build."}, credential=self.manager)
		note = self.message(LIST)["app_feedback"][0]
		self.assertEqual(note["reply_count"], 1)
		reply = note["replies"][0]
		self.assertEqual(reply["reply"], "Fixed in the next build.")
		self.assertEqual(reply["author"], MANAGER_EMPLOYEE)
		self.assertEqual(reply["author_name"], "Mia Lund")
		self.assertTrue(note["is_open"], "a reply alone does not close the note")

	def test_another_workers_notes_are_not_in_mine(self):
		self.a_note()
		self.assertEqual(self.message(LIST, credential=self.mate)["app_feedback"], [])

	def test_the_status_filter(self):
		first = self.a_note()
		self.file({"entry_uuid": SECOND_UUID, "client_reference": SECOND_UUID})
		self.message(REPLY, {"name": first, "reply": "Done.", "status": "Resolved"}, credential=self.manager)
		self.assertEqual(self.message(LIST, {"status": "open"})["count"], 1)
		resolved = self.message(LIST, {"status": "resolved"})["app_feedback"]
		self.assertEqual([note["name"] for note in resolved], [first])
		self.assertEqual(self.message(LIST, {"status": "all"})["count"], 2)

	def test_resolved_includes_a_refusal(self):
		name = self.a_note()
		self.message(
			REPLY, {"name": name, "reply": "Not this season.", "status": "won't fix"}, credential=self.manager
		)
		note = self.message(LIST, {"status": "resolved"})["app_feedback"][0]
		self.assertEqual(note["status"], "Won't Fix")
		self.assertEqual(note["resolution_note"], "Not this season.")

	def test_an_unknown_status_is_refused_by_name(self):
		_status, body = self.refusal(LIST, {"status": "closed"})
		self.assertIn("open", body["error"])


# ── 2. who may answer ────────────────────────────────────────────────────────
class OnlyTheFarmAnswers(FeedbackRepliesTestCase):
	def test_scope_all_is_a_farm_managers(self):
		self.a_note()
		status, body = self.refusal(LIST, {"scope": "all"})
		self.assertEqual(status, 403)
		self.assertIn("Farm Manager", body["error"])
		self.assertEqual(self.message(LIST, {"scope": "all"}, credential=self.manager)["count"], 1)

	def test_scope_all_is_bounded_by_the_managers_entities(self):
		self.a_note()
		STORE.get_raw("App Feedback", self.only()["name"])["company"] = OTHER
		self.assertEqual(self.message(LIST, {"scope": "all"}, credential=self.manager)["count"], 0)

	def test_a_foreman_does_not_read_the_whole_feed(self):
		self.a_note()
		status, _body = self.refusal(LIST, {"scope": "all"}, credential=self.foreman)
		self.assertEqual(status, 403)

	def test_the_author_may_follow_up_on_their_own_note(self):
		name = self.a_note()
		data = self.message(REPLY, {"entry_uuid": UUID, "reply": "Still happening today.", "language": "es"})
		self.assertEqual(data["name"], name)
		self.assertEqual(data["reply"]["author"], WORKER_EMPLOYEE)
		self.assertEqual(data["reply"]["language"], "es")

	def test_the_author_may_not_answer_their_own_note(self):
		name = self.a_note()
		status, _body = self.refusal(REPLY, {"name": name, "reply": "sorted", "status": "Resolved"})
		self.assertEqual(status, 403)
		self.assertTrue(self.only().get("status") in (None, "", "Open"))

	def test_a_colleague_may_not_reply(self):
		name = self.a_note()
		status, _body = self.refusal(REPLY, {"name": name, "reply": "me too"}, credential=self.mate)
		self.assertEqual(status, 403)
		self.assertEqual(narrative.describe_notes("App Feedback", name, "replies"), [])

	def test_a_manager_may_not_answer_another_entitys_note(self):
		name = self.a_note()
		STORE.get_raw("App Feedback", name)["company"] = OTHER
		status, _body = self.refusal(REPLY, {"name": name, "reply": "ok"}, credential=self.manager)
		self.assertEqual(status, 403)

	def test_the_author_is_the_login_not_the_body(self):
		name = self.a_note()
		data = self.message(
			REPLY,
			{"name": name, "reply": "Looking.", "author": "EMP-BEN", "author_name": "Ben"},
			credential=self.manager,
		)
		self.assertEqual(data["reply"]["author"], MANAGER_EMPLOYEE)

	def test_an_empty_reply_is_refused(self):
		name = self.a_note()
		_status, body = self.refusal(REPLY, {"name": name, "reply": "  "}, credential=self.manager)
		self.assertIn("reply is required", body["error"])

	def test_an_answered_note_is_not_reopened(self):
		name = self.a_note()
		self.message(REPLY, {"name": name, "reply": "Done.", "status": "Resolved"}, credential=self.manager)
		status, _body = self.refusal(
			REPLY, {"name": name, "reply": "again", "status": "Open"}, credential=self.manager
		)
		self.assertGreaterEqual(status, 400)
		self.assertEqual(STORE.get_raw("App Feedback", name)["status"], "Resolved")


# ── 3. the reply is a narrative entry ────────────────────────────────────────
class TheReplyIsATaskNote(FeedbackRepliesTestCase):
	def test_get_app_feedback_carries_the_thread(self):
		name = self.a_note()
		self.message(REPLY, {"name": name, "reply": "Seen."}, credential=self.manager)
		data = self.tool_data("get_app_feedback", {"name": name})
		self.assertEqual(data["reply_count"], 1)
		self.assertEqual(data["replies"][0]["reply"], "Seen.")

	def test_add_task_note_over_mcp_writes_a_reply(self):
		self.configure(enabled=1, allow_add_task_note=1)
		name = self.a_note()
		self.tool_data(
			"add_task_note",
			{"doctype": "App Feedback", "name": name, "narrative": "From the office.", "author_name": "Tim"},
		)
		note = self.message(LIST)["app_feedback"][0]
		self.assertEqual([reply["reply"] for reply in note["replies"]], ["From the office."])

	def test_the_phones_generic_note_route_stays_closed_to_feedback(self):
		name = self.a_note()
		status, _body = self.refusal(
			f"{PREFIX}/mobile/add_task_note", {"doctype": "App Feedback", "name": name, "narrative": "x"}
		)
		self.assertGreaterEqual(status, 400)

	def test_a_benchs_document_rows_read_back(self):
		"""On a bench a child row is a Document: `as_dict()`, and no mapping
		protocol, so `dict(row)` raises. The double hands back dicts, which is
		why `describe_notes` passed every test while calling `dict(row)`."""

		class Row:
			def __init__(self, data):
				self._data = data

			def as_dict(self):
				return dict(self._data)

		class Doc:
			def get(self, fieldname):
				return [
					Row(
						{"narrative": "on a bench", "author_name": "Mia", "written_at": "2026-07-24 09:00:00"}
					)
				]

		original = frappe.get_doc
		frappe.get_doc = lambda *a, **kw: Doc()
		try:
			notes = narrative.describe_notes("App Feedback", "AFB-X", "replies")
		finally:
			frappe.get_doc = original
		self.assertEqual([note["narrative"] for note in notes], ["on a bench"])


# ── 4. a late certificate ────────────────────────────────────────────────────
PDF = b"%PDF-1.4\n" + b"0" * 64


class ALateCertificateLandsOnTheRecord(FeedbackRepliesTestCase):
	RECORD = "ETR-2026-0001"

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Training Type", [{"name": "Pesticide Handler", "training_type_name": "Pesticide Handler"}]
		)
		STORE.seed(
			"Employee Training Record",
			[
				{
					"name": self.RECORD,
					"employee": WORKER_EMPLOYEE,
					"employee_name": "Ana Ramos",
					"training_type": "Pesticide Handler",
					"company": MAIN,
					"completed_date": "2026-07-01",
					"training_source": "Internal",
				}
			],
		)

	def record(self):
		return STORE.get_raw("Employee Training Record", self.RECORD)

	def inline(self, **extra):
		body = {
			"training_record": self.RECORD,
			"file_name": "handler-card.pdf",
			"file_content": base64.b64encode(PDF).decode(),
		}
		body.update(extra)
		return body

	def test_the_worker_files_their_own_card(self):
		data = self.message(CERT, self.inline(training_source="External", provider="Columbia Gorge CC"))
		row = self.record()
		self.assertEqual(row["certificate_file"], data["certificate_file"])
		self.assertTrue(data["certificate_file"])
		self.assertEqual(row["training_source"], "External")
		self.assertEqual(row["provider"], "Columbia Gorge CC")
		handle = STORE.get_raw("File", data["file"])
		self.assertEqual(handle["attached_to_doctype"], "Employee Training Record")
		self.assertEqual(handle["attached_to_name"], self.RECORD)

	def test_an_uploaded_file_is_re_pointed_onto_the_record(self):
		STORE.seed(
			"File",
			[
				{
					"name": "FILE-CARD",
					"file_name": "card.jpg",
					"file_url": "/private/files/card.jpg",
					"is_private": 1,
				}
			],
		)
		data = self.message(CERT, {"training_record": self.RECORD, "file": "FILE-CARD"})
		self.assertEqual(self.record()["certificate_file"], "/private/files/card.jpg")
		self.assertEqual(STORE.get_raw("File", "FILE-CARD")["attached_to_name"], self.RECORD)
		self.assertIsNone(data["replaced_certificate_file"])

	def test_a_second_card_replaces_the_first_and_says_so(self):
		self.record()["certificate_file"] = "/private/files/old.pdf"
		data = self.message(CERT, self.inline())
		self.assertEqual(data["replaced_certificate_file"], "/private/files/old.pdf")

	def test_a_colleague_may_not_file_a_card_on_your_record(self):
		status, _body = self.refusal(CERT, self.inline(), credential=self.mate)
		self.assertGreaterEqual(status, 400)
		self.assertFalse(self.record().get("certificate_file"))

	def test_a_foreman_may(self):
		self.message(CERT, self.inline(), credential=self.foreman)
		self.assertTrue(self.record()["certificate_file"])

	def test_another_entitys_record_reads_as_not_found(self):
		self.record()["company"] = OTHER
		self.record()["employee"] = OUTSIDER_EMPLOYEE
		status, _body = self.refusal(CERT, self.inline(), credential=self.foreman)
		self.assertGreaterEqual(status, 400)
		self.assertFalse(self.record().get("certificate_file"))

	def test_exactly_one_source(self):
		_status, body = self.refusal(CERT, self.inline(file="FILE-CARD"))
		self.assertIn("exactly one", body["error"])
		_status, body = self.refusal(CERT, {"training_record": self.RECORD})
		self.assertIn("exactly one", body["error"])

	def test_a_file_that_is_not_on_the_site_is_refused(self):
		_status, body = self.refusal(CERT, {"training_record": self.RECORD, "file": "FILE-NOPE"})
		self.assertIn("no File", body["error"])
