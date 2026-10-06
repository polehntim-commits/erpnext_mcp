# SPDX-License-Identifier: MIT
"""Employee file; login cards never filed permanently. v0.223.0 — docs/design/employee_file.md."""

import json
import re

import frappe

from erpnext_mcp import device_enrollment, employee_file, login_cards

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_farmops_api import FarmOpsAPITestCase
from .test_mobile import WORKER, MobileTestCase

EMP = "HR-EMP-0001"
HR = "hr@example.test"
HRU = "hruser@example.test"
FM = "fm@example.test"
NOBODY = "picker@example.test"


def _in(days: int) -> str:
	return str(frappe.utils.add_days(frappe.utils.today(), days))[:10]


class FileCase(MobileTestCase):
	def setUp(self):
		super().setUp()
		STORE.seed(
			"User",
			[{"name": u, "enabled": 1} for u in (HR, HRU, FM, NOBODY)],
		)
		set_roles(HR, ["HR Manager"])
		set_roles(HRU, ["HR User"])
		set_roles(FM, ["Farm Manager"])
		set_roles(NOBODY, ["Field Worker"])
		STORE.seed(
			"Employee",
			[
				{
					"name": EMP,
					"employee_name": "Ana Ramos",
					"company": MAIN,
					"user_id": WORKER,
					"status": "Active",
					"image": "/files/ana.jpg",
				}
			],
		)

	def seed_records(self):
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-2026-0001",
					"employee": EMP,
					"company": MAIN,
					"status": "Complete",
					"ssn_last_four": "6789",
					"ssn_full": "123456789",
					"list_a_doc_title": "Passport",
					"list_a_doc_number": "X12345678",
					"list_a_doc_expiry": _in(-3),
					"alien_work_authorization_expiry": _in(10),
					"alien_registration_number": "A987654321",
				}
			],
		)
		STORE.seed(
			"W-4 Form",
			[
				{
					"name": "W4-2026-0001",
					"employee": EMP,
					"tax_year": 2026,
					"status": "Active",
					"extra_withholding_per_period": 25,
				}
			],
		)
		STORE.seed(
			"Employee Bank Account",
			[
				{
					"name": "EBA-00001",
					"employee": EMP,
					"routing_number": "123000848",
					"account_number_last_four": "4321",
				}
			],
		)
		STORE.seed(
			"Employee Training Record",
			[
				{
					"name": "ETR-1",
					"employee": EMP,
					"training_type": "WPS",
					"completed_date": _in(-400),
					"expires_date": _in(-35),
				}
			],
		)
		STORE.seed(
			"Farm Incident Record",
			[
				{
					"name": "FIR-1",
					"employee": EMP,
					"discipline_type": "Verbal Warning",
					"issued_on": _in(-20),
					"company": MAIN,
				}
			],
		)
		STORE.seed(
			"Signing Evidence",
			[
				{
					"name": "SE-1",
					"signer": EMP,
					"document_type": "Compliance Policy",
					"document_name": "POL-1",
					"signed_at": frappe.utils.now(),
					"status": "Recorded",
				}
			],
		)
		STORE.seed(
			"Housing Assignment",
			[
				{
					"name": "HA-1",
					"employee": EMP,
					"unit": "Cabin 3",
					"assigned_date": _in(-60),
					"status": "Current",
				}
			],
		)
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-1",
					"task_name": "Badge photo — Ana Ramos",
					"state": "Available",
					"subject_doctype": "Employee",
					"subject_docname": EMP,
					"company": MAIN,
				}
			],
		)
		STORE.seed(
			"Bucket Log Badge Map",
			[{"name": "B-100", "badge_id": "B-100", "employee": EMP, "company": MAIN, "active": 1}],
		)


class LoginCards(FileCase):
	def card(self, **overrides):
		return self.tool_data("generate_mobile_login_qr", {"user": WORKER, **overrides})

	def test_archive_is_refused_by_default_and_with_device_keys(self):
		self.make()
		before = len(STORE.rows("Governance Document"))
		self.assertIn(
			"filing it is off", self.tool_error("generate_mobile_login_qr", {"user": WORKER, "archive": True})
		)
		STORE.singles["ERPNext MCP Settings"]["login_card_archive_enabled"] = 1
		STORE.singles["ERPNext MCP Settings"]["device_keys_enabled"] = 1
		self.assertIn(
			"issue_enrollment_link",
			self.tool_error("generate_mobile_login_qr", {"user": WORKER, "archive": True}),
		)
		self.assertEqual(len(STORE.rows("Governance Document")), before)

	def test_a_filed_card_deletes_itself_on_first_sign_in(self):
		self.make()
		STORE.singles["ERPNext MCP Settings"]["login_card_archive_enabled"] = 1
		data = self.card(archive=True, company=MAIN)
		filed = data["archive"]["governance_document"]
		self.assertEqual(frappe.db.get_value("Mobile Access Grant", WORKER, "qr_document"), filed)
		self.assertEqual(
			device_enrollment.verify(data["payload"]["api_key"], data["payload"]["api_secret"]), WORKER
		)
		self.assertFalse(frappe.db.exists("Governance Document", filed))
		self.assertFalse(frappe.db.get_value("Mobile Access Grant", WORKER, "qr_document"))
		self.assertFalse([f for f in STORE.rows("File") if f.get("attached_to_name") == filed])
		self.assertTrue(
			[r for r in STORE.rows("MCP Action Log") if r.get("tool_name") == "login_card:purged"]
		)

	def test_an_expired_card_is_purged_and_other_documents_are_not(self):
		self.make()
		STORE.singles["ERPNext MCP Settings"]["login_card_archive_enabled"] = 1
		filed = self.card(archive=True, company=MAIN)["archive"]["governance_document"]
		STORE.seed(
			"Governance Document", [{"name": "GOV-OA", "title": "Operating Agreement", "company": MAIN}]
		)
		STORE.seed(
			"Governance Document",
			[{"name": "GOV-ODD", "title": login_cards.TITLE_PREFIX + "not really", "company": MAIN}],
		)
		STORE.seed(
			"File",
			[
				{
					"name": "F-ODD",
					"file_name": "contract.pdf",
					"attached_to_doctype": "Governance Document",
					"attached_to_name": "GOV-ODD",
				}
			],
		)
		frappe.db.set_value("Mobile Access Grant", WORKER, "qr_expires_at", "2020-01-01 00:00:00")
		done = login_cards.purge_due()
		self.assertEqual([card for card, _ in done], [filed])
		self.assertTrue(frappe.db.exists("Governance Document", "GOV-OA"))
		self.assertTrue(frappe.db.exists("Governance Document", "GOV-ODD"))

	def test_the_grant_links_the_employee(self):
		self.make()
		self.card()
		self.assertEqual(frappe.db.get_value("Mobile Access Grant", WORKER, "employee"), EMP)
		frappe.db.set_value("Mobile Access Grant", WORKER, "employee", None)
		self.assertEqual(login_cards.backfill_grant_employees(), 1)


class WhoSeesWhat(FileCase):
	def test_self_hr_hr_user_farm_manager_and_nobody(self):
		self.seed_records()
		mine = employee_file.build(EMP, WORKER)
		self.assertTrue(mine["own_file"])
		self.assertTrue(all(s.get("available") for s in mine["sections"].values()))
		self.assertTrue(employee_file.build(EMP, HR)["sections"]["identity"]["available"])
		hr_user = employee_file.build(EMP, HRU)
		self.assertEqual(hr_user["sections"]["signed_documents"]["discipline"]["available"], False)
		farm = employee_file.build(EMP, FM)
		self.assertFalse(farm["sections"]["identity"]["available"])
		self.assertFalse(farm["sections"]["signed_documents"]["available"])
		self.assertNotIn("roles", farm["sections"]["access"])
		with self.assertRaises(employee_file.NotAllowed):
			employee_file.build(EMP, NOBODY)


class NothingSecret(FileCase):
	def test_last_four_only_and_no_forbidden_keys(self):
		self.seed_records()
		self.make()
		self.tool_data("generate_mobile_login_qr", {"user": WORKER})
		data = employee_file.build(EMP, HR)
		text = json.dumps(data, default=str)
		i9 = data["sections"]["identity"]["i9"]
		self.assertEqual(i9["ssn"], "•••• 6789")
		self.assertEqual(i9["documents"][0]["number"], "•••• 5678")
		for forbidden in (
			"ssn_full",
			"api_secret",
			"enrollment_token",
			"routing_number",
			"account_number",
			"token_hash",
			"api_key",
			"123456789",
			"X12345678",
			"A987654321",
			"123000848",
		):
			self.assertNotIn(forbidden, text, forbidden)
		# Standing alone, not the tail of a random hex docname ("1a4d699901001" made this flaky).
		self.assertFalse(re.search(r"(?<![0-9A-Za-z])\d{9}(?![0-9A-Za-z])", text), "an SSN-shaped number")
		self.assertNotIn("extra_withholding", text)


class Expiry(FileCase):
	def test_flags_worst_first_and_windows(self):
		self.seed_records()
		data = employee_file.build(EMP, HR)
		states = [(f["item"], f["state"]) for f in data["flags"]]
		self.assertEqual(states[0][1], "expired")
		self.assertIn(("Work authorization", "expiring"), states)
		self.assertIn(("Training: WPS", "expired"), states)
		self.assertEqual(data["summary"]["expired"], 2)
		quiet = employee_file.build(EMP, HR, warning_days=5)
		self.assertNotIn(
			("Work authorization", "expiring"), [(f["item"], f["state"]) for f in quiet["flags"]]
		)

	def test_the_badge_photo_task_and_the_rest_are_there(self):
		self.seed_records()
		data = employee_file.build(EMP, HR)
		self.assertEqual(data["sections"]["tasks"]["tasks"][0]["relation"], "about this person")
		self.assertEqual(data["sections"]["badge"]["badges"][0]["badge_id"], "B-100")
		self.assertEqual(data["sections"]["housing"]["assignments"][0]["unit"], "Cabin 3")
		self.assertEqual(
			data["sections"]["signed_documents"]["policy_acknowledgments"][0]["document"], "POL-1"
		)


class Packet(FileCase):
	def test_one_private_pdf_on_the_employee_and_no_i9(self):
		self.seed_records()
		answer = employee_file.packet(EMP, HR)
		self.assertTrue(answer["file_name"].endswith(".pdf"))
		self.assertGreater(answer["bytes"], 100)
		row = next(f for f in STORE.rows("File") if f.get("attached_to_name") == EMP)
		self.assertEqual(int(row["is_private"]), 1)
		self.assertNotIn("I-9", " ".join(answer["appended"] + answer["not_appended"]))

	def test_the_tool_ships_off_and_needs_hr_manager(self):
		from .harness import META

		fields = {f["fieldname"]: f for f in META["ERPNext MCP Settings"].fields}
		self.assertEqual(fields["allow_export_employee_file_packet"]["default"], "0")
		self.assertEqual(fields["allow_get_employee_file"]["default"], "1")
		self.assertEqual(fields["login_card_archive_enabled"]["default"], "0")


class TheDesk(FileCase):
	def test_button_and_connections_seed_once(self):
		first = employee_file.seed_connections()
		self.assertIn("I-9 Form", first)
		self.assertEqual(employee_file.seed_connections(), [])
		self.assertTrue(employee_file.seed_desk_button()["created"])
		self.assertEqual(employee_file.seed_desk_button()["reason"], "already present")

	def test_the_desk_reads_as_the_session_user(self):
		from erpnext_mcp.api import employee_file as api

		self.seed_records()
		frappe.local.session.user = FM
		answer = api.get(EMP)
		self.assertIn("Employee file", answer["html"])
		self.assertFalse(answer["file"]["sections"]["identity"]["available"])


class OnThePhone(FarmOpsAPITestCase):
	"""The worker is already enrolled and has an Employee (WORKER_EMPLOYEE) in setUp."""

	def test_my_own_file_and_not_somebody_elses(self):
		from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER_EMPLOYEE

		mine = self.post("/farmops/api/mobile/get_employee_file", {})
		self.assertEqual(mine.status_code, 200, mine.get_data(as_text=True))
		self.assertEqual(self.payload(mine)["message"]["employee"], WORKER_EMPLOYEE)
		theirs = self.post("/farmops/api/mobile/get_employee_file", {"employee": OUTSIDER_EMPLOYEE})
		self.assertNotEqual(theirs.status_code, 200)
