"""The approved queue on the phone: v0.250.0 mobile routes (decisions 14, 17, 25, 28, 34, 40, 45)."""

import frappe

from erpnext_mcp import compliance_rules, config_lifecycle, go_hold, sop, training_quiz, work_notices
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import phone_configs

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase
from .test_quiz import QUIZ, ALL_RIGHT, KEY
from .test_course_videos import COURSE, YT

EXTRA = {f"allow_{t}": 1 for t in ("draft_config",)} | {"knowledge_checks_enabled": 1, "work_notices_enabled": 1,
                                                       "sop_approver_rules": f"* = {WORKER}"}


class PhoneRoutes(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net",
		               **{**{k: 1 for k in ("allow_create_mobile_user", "allow_create_farm_task", "allow_claim_farm_task")}, **EXTRA})
		STORE.seed("Field", [{"name": "B7", "field_name": "B7", "company": MAIN}])

	def phone(self, fn, **kw):
		self.be()
		try:
			return fn(**kw)
		finally:
			frappe.local.session.user = "Administrator"


class Stage(PhoneRoutes):
	def test_the_phone_must_pick_a_code_and_a_retry_files_once(self):
		with self.assertRaises(frappe.ValidationError):
			self.phone(mobile_api.record_block_stage, block="B7", bbch="")
		first = self.phone(mobile_api.record_block_stage, block="B7", bbch="55", client_request_id="s1")
		again = self.phone(mobile_api.record_block_stage, block="B7", bbch="55", client_request_id="s1")
		self.assertEqual((again["observation"], again["duplicate"]), (first["observation"], True))
		timeline = self.phone(mobile_api.get_block_stages, block="B7")
		self.assertEqual(timeline["current"]["bbch"], "55")


class Overrides(PhoneRoutes):
	def test_a_foreman_overrides_a_hold_from_the_phone(self):
		task = self.a_task(task_name="Prune B7", task_type="Maintenance")
		set_roles(WORKER, ["Field Worker", "Foreman"])
		data = self.phone(mobile_api.override_task_hold, task=task, reason="Rain holds off until 3 pm")
		self.assertEqual(data["override"]["by"], WORKER)
		self.assertTrue(go_hold.override_today(dict(frappe.get_doc("Farm Task", task).as_dict())))


class Notices(PhoneRoutes):
	def test_a_sent_notice_reaches_my_today_card_in_my_language(self):
		task = self.a_task(task_name="Copper, Block 3", task_type="Spray")
		doc = frappe.get_doc({"doctype": "Work Notice", "company": MAIN, "for_date": str(frappe.utils.add_days(frappe.utils.today(), 1))[:10],
		                      "status": "Sent", "task": task, "task_name": "Copper, Block 3",
		                      "text_en": "No Copper tomorrow.", "text_es": "Mañana no hay Copper.",
		                      "recipients": [{"employee": WORKER_EMPLOYEE, "employee_name": "Ana Ramos", "language": "es", "sent": 1}]})
		doc.insert(ignore_permissions=True)
		data = self.phone(mobile_api.list_my_work_notices)
		self.assertEqual([n["text"] for n in data["for_me"]], ["Mañana no hay Copper."])
		self.assertEqual(data["to_decide"], [])


class SopOnThePhone(PhoneRoutes):
	def test_an_approver_sees_it_and_approves_it_with_face_id(self):
		frappe.set_user("Administrator")
		policy = frappe.get_doc({"doctype": "Compliance Policy", "policy_name": "Ladder SOP", "category": "Worker Safety",
		                         "status": "Draft", "covers_task_types": "Maintenance", "company": MAIN}).insert(ignore_permissions=True)
		sop.submit(policy.name, "Administrator")
		reviews = self.phone(mobile_api.list_my_sop_reviews)["reviews"]
		self.assertEqual([r["name"] for r in reviews], [policy.name])
		done = self.phone(mobile_api.review_sop, policy=policy.name, action="approve", face_id="true")
		self.assertEqual(done["status"], "Approved")
		methods = [r["verification_method"] for r in STORE.rows("Signing Evidence") if r.get("document_name") == policy.name]
		self.assertEqual(methods, ["Face ID (device key)"])
		task = self.a_task(task_name="Ladder work", task_type="Maintenance")
		sops = self.phone(mobile_api.get_task_sops, task=task)["sops"]
		self.assertEqual([(s["policy"], s["approved"]) for s in sops], [(policy.name, True)])


class CoursePlayer(PhoneRoutes):
	def test_my_course_my_attempt_and_a_managers_sign_off(self):
		STORE.seed("Training Type", [{"name": COURSE, "training_type_name": COURSE, "active": 1, "video_url": YT}])
		frappe.set_user("Administrator")
		self.tool_data("draft_config", {"kind": "quiz", "key": KEY, "body": QUIZ})
		with config_lifecycle.desk_action():
			phone_configs.publish_phone_config({"kind": "quiz", "key": KEY, "version": 1, "change_note": "Tim read it"})
		course = self.phone(mobile_api.get_my_course, training_type=COURSE)
		self.assertEqual(course["quiz"]["version"], 1)
		attempt = self.phone(mobile_api.submit_my_quiz_attempt, training_type=COURSE, answers=ALL_RIGHT)
		self.assertEqual(attempt["employee"], WORKER_EMPLOYEE)
		training_quiz.mark(attempt["evidence"], {"q5": True}, "Administrator")
		frappe.db.commit()  # each phone request commits; the refusal below rolls back only itself
		with self.assertRaises(frappe.PermissionError):
			self.phone(mobile_api.list_training_signoffs)
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.assertEqual(len(self.phone(mobile_api.list_training_signoffs)["ready"]), 1)
		done = self.phone(mobile_api.sign_off_training, attempt=attempt["evidence"])
		self.assertEqual(done["signoff_status"], "Signed off")


class TheStartTellsThePhone(PhoneRoutes):
	def test_a_held_task_carries_its_verdict_and_the_start_carries_the_stage_question(self):
		from unittest import mock

		from erpnext_mcp import ccf_providers
		from .test_go_hold import forecast

		spec = next(x for x in go_hold.preset_specs() if x["rule_id"] == "go_hold_pruning_canker")
		compliance_rules.build_rule({**spec, "enabled": 1, "human_approved_by": "Administrator",
		                             "human_approved_on": frappe.utils.now()}).insert(ignore_permissions=True)
		task = self.a_task(task_name="Prune B7", task_type="Maintenance", location_doctype="Field", location="B7")
		self.tool_data("claim_farm_task", {"task": task, "worker_id": WORKER_EMPLOYEE, "worker_name": "Ana Ramos"})
		frappe.db.commit()
		with mock.patch.object(ccf_providers, "_weather", return_value=forecast(wet=True)):
			started = self.phone(mobile_api.start_task, task=task)
		self.assertEqual(started["go_hold"], "Hold")
		self.assertTrue(started["go_hold_reasons"])
		self.assertIn("Advisory Hold", started["go_hold_at_start"]["note"])
		self.assertIn("What stage is B7 at", started["stage_prompt"]["question"]["en"])
