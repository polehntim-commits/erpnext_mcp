"""Knowledge checks and trainer sign-off. v0.247.0 (approved queue item 7; decisions 42–45)."""

import frappe

from erpnext_mcp import config_lifecycle, phone_config, training_quiz
from erpnext_mcp.tools import phone_configs

from .harness import STORE
from .test_course_videos import COURSE, ON as VIDEO_ON, VideoTestCase

ON = {**VIDEO_ON, "knowledge_checks_enabled": 1,
      **{f"allow_{t}": 1 for t in ("submit_quiz_attempt", "mark_short_answers", "get_quiz_results", "draft_config",
                                   "preview_config", "publish_config", "get_config", "list_configs")}}
KEY = training_quiz.key_for(COURSE)


def t(en, es="—"):
	return {"en": en, "es": es}


QUIZ = {
	"training_type": COURSE,
	"title": t("D-6C walk-around", "Revisión del D-6C"),
	"pass_pct": 80,
	"topics_covered": ["Pre-start walk-around", "Blade and ripper safety"],
	"questions": [
		{"id": "q1", "type": "choice", "prompt": t("First check?", "¿Primera revisión?"),
		 "choices": [{"id": "a", "text": t("Oil", "Aceite")}, {"id": "b", "text": t("Radio", "Radio")}], "answer": "a",
		 "explanation": t("Fluids first.", "Primero los fluidos.")},
		{"id": "q2", "type": "true_false", "prompt": t("Lower the blade to park?", "¿Bajar la hoja para estacionar?"), "answer": True},
		{"id": "q3", "type": "true_false", "prompt": t("Ride on the ripper?", "¿Subirse al ripper?"), "answer": False},
		{"id": "q4", "type": "choice", "prompt": t("Seat belt?", "¿Cinturón?"),
		 "choices": [{"id": "y", "text": t("Always", "Siempre")}, {"id": "n", "text": t("Never", "Nunca")}], "answer": "y"},
		{"id": "q5", "type": "short", "prompt": t("Name one pinch point.", "Nombra un punto de atrapamiento.")},
	],
}
ALL_RIGHT = {"q1": "a", "q2": True, "q3": False, "q4": "y", "q5": "between the track and frame"}


class QuizTestCase(VideoTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		STORE.get_raw("Employee", "EMP-001").update(company=self.company(), status="Active")

	def company(self):
		rows = STORE.rows("Company")
		return rows[0]["name"] if rows else None

	def published(self, body=None, version=1):
		self.tool_data("draft_config", {"kind": "quiz", "key": KEY, "body": body or QUIZ})
		with config_lifecycle.desk_action():
			phone_configs.publish_phone_config({"kind": "quiz", "key": KEY, "version": version, "change_note": "Tim read it"})

	def attempt(self, answers, **kw):
		return self.tool_data("submit_quiz_attempt", {"employee": "EMP-001", "training_type": COURSE, "answers": answers, **kw})


class Authoring(QuizTestCase):
	def test_a_bad_quiz_says_what_blocks_publishing(self):
		bad = {**QUIZ, "questions": [{"id": "q1", "type": "choice", "prompt": t("x", ""), "choices": [{"id": "a", "text": t("A")}], "answer": "z"}]}
		data = self.tool_data("preview_config", {"kind": "quiz", "key": KEY, "body": bad})["preview"]
		joined = " ".join(data["publish_blockers"])
		self.assertIn("two or more", joined)
		self.assertIn("one of its choice ids", joined)
		self.assertIn("no Spanish", joined)

	def test_mcp_drafts_and_a_person_publishes(self):
		self.tool_data("draft_config", {"kind": "quiz", "key": KEY, "body": QUIZ})
		self.assertIn("Desk", self.tool_error("publish_config", {"kind": "quiz", "key": KEY, "version": 1, "change_note": "x"}))
		self.published()
		doc, body = training_quiz.body_for(COURSE)
		self.assertEqual((int(doc.version), body["pass_pct"]), (1, 80))


class Attempts(QuizTestCase):
	def test_short_answers_wait_then_a_pass_is_ready_for_sign_off(self):
		self.published()
		data = self.attempt(ALL_RIGHT, client_request_id="r1")
		self.assertEqual((data["result"], data["short_pending"]), ("Awaiting marking", 1))
		self.assertTrue(self.attempt(ALL_RIGHT, client_request_id="r1")["duplicate"])
		marked = self.tool_data("mark_short_answers", {"attempt": data["evidence"], "marks": {"q5": True}})
		self.assertEqual((marked["result"], marked["score_pct"], marked["signoff_status"]), ("Passed", 100.0, "Ready for sign-off"))
		self.assertEqual(self.tool_data("get_quiz_results", {"status": "ready"})["ready_for_signoff"], 1)

	def test_a_fail_shows_the_explanation_and_is_not_ready(self):
		self.published()
		data = self.attempt({**ALL_RIGHT, "q1": "b", "q2": False})
		data = self.tool_data("mark_short_answers", {"attempt": data["evidence"], "marks": {"q5": True}})
		self.assertEqual((data["result"], data["score_pct"]), ("Failed", 60.0))
		self.assertIsNone(data["signoff_status"])
		missed = {q["id"]: q for q in data["questions"] if q["result"] == "wrong"}
		self.assertEqual(missed["q1"]["explanation"]["en"], "Fluids first.")

	def test_an_attempt_is_graded_on_the_version_it_was_taken_on(self):
		self.published()
		harder = {**QUIZ, "pass_pct": 100, "questions": QUIZ["questions"][:4] + [{**QUIZ["questions"][0], "id": "q6", "answer": "b"}]}
		self.published(harder, version=2)
		data = self.attempt({**ALL_RIGHT}, version=1)
		self.assertEqual(data["quiz_version"], 1)

	def test_off_until_switched_on(self):
		self.published()
		self.configure(enabled=1, **{**ON, "knowledge_checks_enabled": 0})
		self.assertIn("switched off", self.tool_error("submit_quiz_attempt", {"employee": "EMP-001", "training_type": COURSE, "answers": {}}))


class SignOff(QuizTestCase):
	def passed(self):
		self.published()
		data = self.attempt(ALL_RIGHT)
		return self.tool_data("mark_short_answers", {"attempt": data["evidence"], "marks": {"q5": True}})["evidence"]

	def test_a_person_signs_off_and_the_training_record_is_filed(self):
		name = self.passed()
		with self.assertRaisesRegex(PermissionError, "cannot sign off"):
			training_quiz.sign_off(name, "crew@example.com")
		done = training_quiz.sign_off(name, "Administrator")
		self.assertEqual(done["signoff_status"], "Signed off")
		record = STORE.get_raw("Employee Training Record", done["training_record"])
		self.assertEqual((record["employee"], record["training_type"]), ("EMP-001", COURSE))
		self.assertEqual(record["supervisor_reviewed_by"], "Administrator")
		roles = [r["signature_role"] for r in STORE.rows("Signing Evidence") if r.get("document_name") == name]
		self.assertEqual(roles, ["Trainer"])
		with self.assertRaisesRegex(ValueError, "not ready"):
			training_quiz.sign_off(name, "Administrator")

	def test_below_the_video_minimum_needs_a_reason(self):
		STORE.get_raw("Training Type", COURSE)["video_min_pct"] = 90
		self.a_video()
		name = self.passed()
		self.assertEqual(training_quiz.describe(name)["video_minimum"], "Not met")
		with self.assertRaisesRegex(ValueError, "give a reason"):
			training_quiz.sign_off(name, "Administrator")
		done = training_quiz.sign_off(name, "Administrator", reason="Watched it with me in the shop")
		self.assertEqual(done["signoff_status"], "Signed off")
