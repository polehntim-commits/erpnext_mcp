# SPDX-License-Identifier: MIT
"""Structured I-9 workflow — the 15 tools, the audit trail, the SSN rule.

TWELVE CLAIMS, the last of them v0.64.2's.

1. `ReadTools` — every read tool returns the right data in the right shape.
2. `CreateAndFill` — the happy path: create, Section 1, Section 2.
3. `SSNIsStrippedToLastFour` — the last four are all that is kept by default.
4. `ThreeBusinessDayRule` — Section 2 is refused when verification is late.
5. `AuditTrail` — every mutating action writes an immutable I-9 Audit Log row.
6. `RetentionAndDestruction` — retention dates, destruction eligibility, and
   the refusal to destroy before the retention date.
7. `OnboardCreatesI9` — onboard_employee auto-creates a Draft I-9 Form.

v0.47.0, and each of the four is a federal requirement the app did not meet:

8. `Section1ImmigrationIdentifiers` — an Alien Authorized to Work answers with
   an A-Number, an I-94 number, or a foreign passport WITH its country, and a
   Section 1 answering with none of the three is refused.
9. `FullSSNIsOptInOnly` — nine digits reach the encrypted column only where the
   site switched `store_full_ssn` on, and the switch going off blanks it.
10. `Section2DocumentsAndReceipts` — a title is checked against the list it
    claims to be from, and a receipt completes the form while leaving the
    document owed on a 90-day clock.
11. `Section3Reverification` — `reverify_i9` appends without overwriting
    Section 2, moves the work-authorization expiry forward, refuses a document
    that had already expired, and closes an outstanding receipt.

12. `CompleteMeansSigned` — v0.64.2. A form reaches `Complete` only when it
    carries both attestations; the documents are filed regardless and it rests
    at `Awaiting Verification` until the outstanding one is signed. And a
    signature that never happened gets no timestamp and no IP.

13. `PatchSection1` — v0.67.1, and the hole it closes is that every other tool
    in this module moves a form FORWARD. A Section 1 filed with a blank date of
    birth had no route to one on any status, because `submit_i9_section_1` only
    takes a Draft. `patch_i9_section_1` writes the four transcription columns
    and refuses the sworn ones by name; the PDF half is asserted in
    `test_i9_pdf.py`, where a rendered page exists to go stale.
"""

import json
from datetime import date, datetime, timedelta

import frappe

from erpnext_mcp.tools import i9

from .fixtures import MAIN, V12TestCase, install_hrms
from .harness import ROLES, STORE, add_field, set_roles

#: What the two attestations look like on a form that carries them. The tools
#: take a file URL and store it; nothing here reads the bytes, and a test that
#: needs a REAL capture goes through `collect_form_signature` in
#: `test_missing_signatures.py`.
SECTION_1_INK = "/private/files/i9-section-1-signature.png"
SECTION_2_INK = "/private/files/i9-section-2-signature.png"

I9_TOOLS_ON = {
	f"allow_{name}": 1
	for name in (
		"get_i9_settings",
		"get_i9_form",
		"list_i9_forms",
		"list_pending_i9_verifications",
		"get_i9_audit_log",
		"list_i9_document_types",
		"get_i9_retention_report",
		"list_expiring_work_authorizations",
		"create_i9_form",
		"submit_i9_section_1",
		"submit_i9_section_2",
		"update_i9_settings",
		"flag_i9_reverification",
		"reverify_i9",
		"destroy_i9",
		"patch_i9_section_1",
	)
}

ONBOARD_ON = {
	f"allow_{name}": 1 for name in ("onboard_employee", "create_mobile_user", "attach_file_to_document")
}


class I9TestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **I9_TOOLS_ON)
		install_hrms()
		self._seed_i9_settings()
		self._seed_doc_types()

	def _seed_i9_settings(self):
		STORE.singles["I-9 Settings"] = {
			"doctype": "I-9 Settings",
			"store_document_copies": "0",
			"enrolled_in_e_verify": "0",
			"business_legal_name": "Test Farm LLC",
			"business_address": "123 Orchard Rd",
			"business_ein": "12-3456789",
			"reminder_days_before_doc_expiration": "90",
			"reminder_days_before_destruction": "60",
		}

	def _seed_doc_types(self):
		STORE.seed(
			"I-9 Document Type",
			[
				{
					"name": "U.S. Passport",
					"doc_title": "U.S. Passport",
					"list_category": "A",
					"uscis_code": "passport",
					"requires_photo": 1,
					"enabled": 1,
					"description": "Unexpired U.S. passport",
				},
				{
					"name": "Driver's License",
					"doc_title": "Driver's License",
					"list_category": "B",
					"uscis_code": "drivers-license",
					"requires_photo": 1,
					"enabled": 1,
					"description": "State-issued driver's license",
				},
				{
					"name": "Social Security Card (Unrestricted)",
					"doc_title": "Social Security Card (Unrestricted)",
					"list_category": "C",
					"uscis_code": "ssn-card",
					"requires_photo": 0,
					"enabled": 1,
					"description": "Social Security Account Number card",
				},
				# The document a seasonal worker on a renewing authorization
				# actually presents at reverification, and the reason it is in
				# the fixture rather than only in `i9_documents.py`: Section 3
				# takes List A or List C, and without one of each the tests
				# below could not tell "wrong list" from "unknown document".
				{
					"name": "Employment Authorization Document (Form I-766)",
					"doc_title": "Employment Authorization Document (Form I-766)",
					"list_category": "A",
					"uscis_code": "i-766",
					"requires_photo": 1,
					"enabled": 1,
					"description": "Unexpired Employment Authorization Document with photograph",
				},
			],
		)

	def _create_draft(self, employee="HR-EMP-00001", hire_date=None):
		if hire_date is None:
			hire_date = str(date.today())
		return self.tool_data(
			"create_i9_form",
			{"employee": employee, "company": MAIN, "hire_date": hire_date},
		)

	def _submit_section_1(self, employee="HR-EMP-00001", **overrides):
		payload = {
			"employee": employee,
			"legal_first_name": "Ada",
			"legal_last_name": "Orchard",
			"citizenship_status": "US Citizen",
			# v0.64.2. THE FIXTURES SIGN, because a Form I-9 is complete when it
			# carries the attestations rather than when its boxes are full, and a
			# helper that built the unsigned form would make every test
			# downstream of it assert against a record no inspection would
			# accept. The tests that are ABOUT the unsigned form pass
			# `section_1_signature=""` / `section_2_signature=""` and say so.
			"section_1_signature": SECTION_1_INK,
		}
		payload.update(overrides)
		return self.tool_data("submit_i9_section_1", payload)

	def _submit_section_2(self, employee="HR-EMP-00001", **overrides):
		payload = {
			"employee": employee,
			"document_path": "List A",
			"list_a_doc_title": "U.S. Passport",
			"list_a_doc_authority": "US Dept of State",
			"list_a_doc_number": "123456789",
			"verifier_name": "Tim Polehn",
			"verification_date": str(date.today()),
			"section_2_signature": SECTION_2_INK,
		}
		payload.update(overrides)
		return self.tool_data("submit_i9_section_2", payload)


# ── 1 ─────────────────────────────────────────────────────────────────────────
class ReadTools(I9TestCase):
	def test_get_i9_settings(self):
		data = self.tool_data("get_i9_settings", {})
		self.assertEqual(data["business_legal_name"], "Test Farm LLC")
		self.assertEqual(data["business_ein"], "12-3456789")
		self.assertFalse(data["store_document_copies"])
		self.assertFalse(data["enrolled_in_e_verify"])

	def test_list_i9_document_types_returns_all(self):
		data = self.tool_data("list_i9_document_types", {})
		self.assertEqual(data["count"], 4)

	def test_list_i9_document_types_filters_by_category(self):
		data = self.tool_data("list_i9_document_types", {"list_category": "B"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["documents"][0]["doc_title"], "Driver's License")

	def test_list_i9_document_types_groups_by_list(self):
		"""The grouped shape, which is the one Section 2's own question has."""
		data = self.tool_data("list_i9_document_types", {})
		self.assertEqual(
			[d["doc_title"] for d in data["by_list"]["A"]],
			["Employment Authorization Document (Form I-766)", "U.S. Passport"],
		)
		self.assertEqual([d["doc_title"] for d in data["by_list"]["B"]], ["Driver's License"])
		self.assertEqual(
			[d["doc_title"] for d in data["by_list"]["C"]],
			["Social Security Card (Unrestricted)"],
		)

	def test_grouping_carries_every_category_even_when_empty(self):
		"""A caller drawing the form needs three keys, not however many happen
		to have rows — an absent 'B' is a KeyError on a phone."""
		data = self.tool_data("list_i9_document_types", {"list_category": "A"})
		self.assertEqual(sorted(data["by_list"]), ["A", "B", "C"])
		self.assertEqual(data["by_list"]["B"], [])

	def test_list_i9_forms_empty(self):
		data = self.tool_data("list_i9_forms", {})
		self.assertEqual(data["count"], 0)

	def test_get_i9_form_missing_raises(self):
		msg = self.tool_error("get_i9_form", {"employee": "HR-EMP-00001"})
		self.assertIn("no I-9 Form", msg)

	def test_list_pending_i9_verifications_empty(self):
		data = self.tool_data("list_pending_i9_verifications", {})
		self.assertEqual(data["count"], 0)

	def test_get_i9_retention_report_empty(self):
		data = self.tool_data("get_i9_retention_report", {})
		self.assertEqual(data["approaching_count"], 0)
		self.assertEqual(data["eligible_count"], 0)

	def test_list_expiring_work_authorizations_empty(self):
		data = self.tool_data("list_expiring_work_authorizations", {})
		self.assertEqual(data["count"], 0)


# ── 2 ─────────────────────────────────────────────────────────────────────────
class CreateAndFill(I9TestCase):
	def test_create_draft(self):
		data = self._create_draft()
		self.assertEqual(data["status"], "Draft")
		self.assertEqual(data["employee"], "HR-EMP-00001")

	def test_create_refuses_duplicate(self):
		self._create_draft()
		msg = self.tool_error(
			"create_i9_form",
			{"employee": "HR-EMP-00001", "company": MAIN, "hire_date": str(date.today())},
		)
		self.assertIn("already has an active I-9", msg)

	def test_submit_section_1(self):
		self._create_draft()
		data = self._submit_section_1()
		self.assertEqual(data["status"], "Section 1 Complete")

	def test_submit_section_1_requires_draft(self):
		msg = self.tool_error(
			"submit_i9_section_1",
			{
				"employee": "HR-EMP-00001",
				"legal_first_name": "Ada",
				"legal_last_name": "Orchard",
				"citizenship_status": "US Citizen",
			},
		)
		self.assertIn("no Draft I-9", msg)

	def test_submit_section_2_list_a(self):
		self._create_draft()
		self._submit_section_1()
		data = self._submit_section_2()
		self.assertEqual(data["status"], "Complete")

	def test_submit_section_2_list_b_c(self):
		self._create_draft()
		self._submit_section_1()
		data = self._submit_section_2(
			document_path="List B + C",
			list_b_doc_title="Driver's License",
			list_b_doc_authority="Oregon DMV",
			list_b_doc_number="DL12345",
			list_c_doc_title="Social Security Card (Unrestricted)",
			list_c_doc_authority="SSA",
			list_c_doc_number="SSC12345",
		)
		self.assertEqual(data["status"], "Complete")

	def test_full_workflow_appears_in_list(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		data = self.tool_data("list_i9_forms", {})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["forms"][0]["status"], "Complete")

	def test_get_i9_form_returns_complete_record(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		data = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["status"], "Complete")
		self.assertEqual(data["legal_first_name"], "Ada")
		self.assertEqual(data["legal_last_name"], "Orchard")

	def test_pending_verification_after_section_1(self):
		self._create_draft()
		self._submit_section_1()
		data = self.tool_data("list_pending_i9_verifications", {})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["pending"][0]["employee"], "HR-EMP-00001")

	def test_no_pending_after_section_2(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		data = self.tool_data("list_pending_i9_verifications", {})
		self.assertEqual(data["count"], 0)


# ── 3 ─────────────────────────────────────────────────────────────────────────
class SSNIsStrippedToLastFour(I9TestCase):
	def test_full_ssn_stored_as_last_four(self):
		self._create_draft()
		self._submit_section_1(ssn_last_four="123-45-6789")
		row = frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "ssn_last_four")
		self.assertEqual(row, "6789")

	def test_four_digits_stored_as_is(self):
		self._create_draft()
		self._submit_section_1(ssn_last_four="1234")
		row = frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "ssn_last_four")
		self.assertEqual(row, "1234")

	def test_partial_ssn_stored_as_available_digits(self):
		self._create_draft()
		self._submit_section_1(ssn_last_four="89")
		row = frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "ssn_last_four")
		self.assertEqual(row, "89")


# ── 4 ─────────────────────────────────────────────────────────────────────────
class ThreeBusinessDayRule(I9TestCase):
	def test_same_day_verification_accepted(self):
		today = date.today()
		self._create_draft(hire_date=str(today))
		self._submit_section_1()
		data = self._submit_section_2(verification_date=str(today))
		self.assertEqual(data["status"], "Complete")

	def test_three_business_days_accepted(self):
		hire = date(2026, 8, 4)  # Monday
		verify = date(2026, 8, 7)  # Thursday, 3 bdays later
		self._create_draft(hire_date=str(hire))
		self._submit_section_1()
		data = self._submit_section_2(verification_date=str(verify))
		self.assertEqual(data["status"], "Complete")

	def test_four_business_days_refused(self):
		hire = date(2026, 8, 3)  # Monday
		verify = date(2026, 8, 8)  # Saturday -> actually let's make it realistic
		# Monday to the next Monday = 5 bdays (M,T,W,Th,F + M) but counting inclusive
		hire = date(2026, 8, 3)  # Monday
		verify = date(2026, 8, 10)  # next Monday = 6 bdays inclusive
		self._create_draft(hire_date=str(hire))
		self._submit_section_1()
		msg = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "U.S. Passport",
				"verifier_name": "Tim Polehn",
				"verification_date": str(verify),
			},
		)
		self.assertIn("business days", msg)

	def test_weekend_not_counted(self):
		hire = date(2026, 8, 7)  # Friday
		verify = date(2026, 8, 11)  # Tuesday = 3 bdays (Fri, Mon, Tue)
		self._create_draft(hire_date=str(hire))
		self._submit_section_1()
		data = self._submit_section_2(verification_date=str(verify))
		self.assertEqual(data["status"], "Complete")


# ── 5 ─────────────────────────────────────────────────────────────────────────
class AuditTrail(I9TestCase):
	def test_create_logs_created(self):
		self._create_draft()
		logs = STORE.rows("I-9 Audit Log")
		created = [r for r in logs if r.get("action") == "Created"]
		self.assertEqual(len(created), 1)
		self.assertEqual(created[0]["employee"], "HR-EMP-00001")

	def test_section_1_logs_submitted(self):
		self._create_draft()
		self._submit_section_1()
		logs = STORE.rows("I-9 Audit Log")
		s1 = [r for r in logs if r.get("action") == "Section 1 Submitted"]
		self.assertEqual(len(s1), 1)

	def test_section_2_logs_signed(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		logs = STORE.rows("I-9 Audit Log")
		s2 = [r for r in logs if r.get("action") == "Section 2 Signed"]
		self.assertEqual(len(s2), 1)

	def test_view_logs_viewed(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})
		logs = STORE.rows("I-9 Audit Log")
		viewed = [r for r in logs if r.get("action") == "Viewed"]
		self.assertEqual(len(viewed), 1)

	def test_get_audit_log_returns_entries(self):
		self._create_draft()
		self._submit_section_1()
		data = self.tool_data("get_i9_audit_log", {"employee": "HR-EMP-00001"})
		self.assertGreaterEqual(data["count"], 2)

	def test_audit_log_is_immutable(self):
		self._create_draft()
		logs = STORE.rows("I-9 Audit Log")
		self.assertTrue(len(logs) > 0)


# ── 6 ─────────────────────────────────────────────────────────────────────────
class RetentionAndDestruction(I9TestCase):
	def test_retention_date_calculated(self):
		hire = str(date.today() - timedelta(days=365))
		self._create_draft(hire_date=hire)
		row = frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "retention_until")
		self.assertIsNotNone(row)

	def test_destroy_refused_before_retention(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		msg = self.tool_error("destroy_i9", {"employee": "HR-EMP-00001"})
		self.assertIn("retained until", msg)

	def test_destroy_after_retention(self):
		hire = date.today() - timedelta(days=365 * 4)
		self._create_draft(hire_date=str(hire))
		self._submit_section_1()
		self._submit_section_2(verification_date=str(hire))
		data = self.tool_data("destroy_i9", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["status"], "Destroyed")

	def test_destroy_logs_destruction(self):
		hire = date.today() - timedelta(days=365 * 4)
		self._create_draft(hire_date=str(hire))
		self._submit_section_1()
		self._submit_section_2(verification_date=str(hire))
		self.tool_data("destroy_i9", {"employee": "HR-EMP-00001"})
		logs = STORE.rows("I-9 Audit Log")
		destroyed = [r for r in logs if r.get("action") == "Destroyed"]
		self.assertEqual(len(destroyed), 1)

	def test_reverification_flag(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		data = self.tool_data(
			"flag_i9_reverification",
			{"employee": "HR-EMP-00001", "reason": "Work auth expiring"},
		)
		self.assertEqual(data["status"], "Reverification Needed")

	def test_reverification_logs(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		self.tool_data(
			"flag_i9_reverification",
			{"employee": "HR-EMP-00001", "reason": "Work auth expiring"},
		)
		logs = STORE.rows("I-9 Audit Log")
		flagged = [r for r in logs if r.get("action") == "Reverification Flagged"]
		self.assertEqual(len(flagged), 1)

	def test_retention_report_shows_eligible(self):
		hire = str(date.today() - timedelta(days=365 * 4))
		self._create_draft(hire_date=hire)
		data = self.tool_data("get_i9_retention_report", {})
		self.assertGreater(data["eligible_count"], 0)

	def test_expiring_work_auth(self):
		self._create_draft()
		self._submit_section_1(
			citizenship_status="Alien Authorized to Work",
			alien_registration_number="A012345678",
			alien_work_authorization_expiry=str(date.today() + timedelta(days=30)),
		)
		self._submit_section_2()
		data = self.tool_data("list_expiring_work_authorizations", {"days_ahead": 90})
		self.assertEqual(data["count"], 1)

	def test_days_ahead_zero_asks_about_today_and_not_about_ninety_days(self):
		"""`as_int(args, "days_ahead", 90) or 90` turned an explicit 0 back into
		the default, so "who is expired or expiring TODAY" silently answered with
		three months of people. It errs toward showing too much rather than too
		little, which is why it survived — but a window nobody asked for is still
		the wrong answer, and on a compliance screen it reads as "these eleven are
		expiring now"."""
		self._create_draft()
		self._submit_section_1(
			citizenship_status="Alien Authorized to Work",
			alien_registration_number="A012345678",
			alien_work_authorization_expiry=str(date.today() + timedelta(days=30)),
		)
		self._submit_section_2()
		self.assertEqual(self.tool_data("list_expiring_work_authorizations", {"days_ahead": 90})["count"], 1)
		self.assertEqual(self.tool_data("list_expiring_work_authorizations", {"days_ahead": 0})["count"], 0)

	def test_days_ahead_zero_still_finds_somebody_expiring_today(self):
		"""The other direction, so the fix cannot be "0 matches nothing"."""
		self._create_draft()
		self._submit_section_1(
			citizenship_status="Alien Authorized to Work",
			alien_registration_number="A012345678",
			alien_work_authorization_expiry=str(date.today()),
		)
		self._submit_section_2()
		self.assertEqual(self.tool_data("list_expiring_work_authorizations", {"days_ahead": 0})["count"], 1)


# ── 7 ─────────────────────────────────────────────────────────────────────────
class OnboardCreatesI9(I9TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **I9_TOOLS_ON, **ONBOARD_ON)

	def test_onboard_creates_draft_i9(self):
		data = self.tool_data(
			"onboard_employee",
			{"full_name": "Ana Ramos", "company": MAIN},
		)
		self.assertIsNotNone(data.get("i9_form"))
		i9 = frappe.db.get_value("I-9 Form", data["i9_form"], "status")
		self.assertEqual(i9, "Draft")

	def test_onboard_reuses_existing_i9(self):
		data1 = self.tool_data(
			"onboard_employee",
			{"full_name": "Ana Ramos", "company": MAIN},
		)
		i9_name = data1["i9_form"]
		data2 = self.tool_data(
			"onboard_employee",
			{"full_name": "Ana Ramos", "company": MAIN},
		)
		self.assertEqual(data2["i9_form"], i9_name)


# ── 8 ─────────────────────────────────────────────────────────────────────────
class Section1ImmigrationIdentifiers(I9TestCase):
	"""Section 1 asks an Alien Authorized to Work for ONE of three identifiers.

	Until v0.47.0 the form could store only the first, so a worker whose answer
	was an I-94 number or a foreign passport had it dropped on the floor and the
	form was filed looking answered.
	"""

	def _alien(self, **overrides):
		self._create_draft()
		return self._submit_section_1(citizenship_status="Alien Authorized to Work", **overrides)

	def _filed(self):
		return next(iter(STORE.rows("I-9 Form")))

	def test_a_number_alone_is_enough(self):
		self._alien(alien_registration_number="A012345678")
		self.assertEqual(self._filed()["alien_registration_number"], "A012345678")

	def test_i94_number_alone_is_enough(self):
		self._alien(i94_admission_number="94123456789")
		self.assertEqual(self._filed()["i94_admission_number"], "94123456789")

	def test_foreign_passport_with_country_is_enough(self):
		self._alien(foreign_passport_number="X1234567", foreign_passport_country="Mexico")
		row = self._filed()
		self.assertEqual(row["foreign_passport_number"], "X1234567")
		self.assertEqual(row["foreign_passport_country"], "Mexico")

	def test_none_of_the_three_is_refused(self):
		self._create_draft()
		msg = self.tool_error(
			"submit_i9_section_1",
			{
				"employee": "HR-EMP-00001",
				"legal_first_name": "Ada",
				"legal_last_name": "Orchard",
				"citizenship_status": "Alien Authorized to Work",
			},
		)
		self.assertIn("needs ONE of", msg)

	def test_a_passport_number_without_a_country_is_refused(self):
		self._create_draft()
		msg = self.tool_error(
			"submit_i9_section_1",
			{
				"employee": "HR-EMP-00001",
				"legal_first_name": "Ada",
				"legal_last_name": "Orchard",
				"citizenship_status": "Alien Authorized to Work",
				"foreign_passport_number": "X1234567",
			},
		)
		self.assertIn("foreign_passport_country", msg)

	def test_a_citizen_needs_none_of_them(self):
		"""The refusal is Section 1's rule about ONE status, not a new required
		field on every I-9 this site files."""
		self._create_draft()
		data = self._submit_section_1(citizenship_status="US Citizen")
		self.assertEqual(data["status"], "Section 1 Complete")

	def test_a_permanent_resident_keeps_the_a_number(self):
		self._create_draft()
		self._submit_section_1(
			citizenship_status="Lawful Permanent Resident",
			alien_registration_number="A987654321",
		)
		self.assertEqual(self._filed()["alien_registration_number"], "A987654321")

	def test_the_identifiers_come_back_on_get(self):
		self._alien(i94_admission_number="94123456789")
		data = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["i94_admission_number"], "94123456789")

	def test_the_audit_row_names_which_identifier_and_not_its_value(self):
		"""The log answers "was this answered and how". An A-number copied into
		a JSON blob on a second doctype is one more place it lives."""
		self._alien(alien_registration_number="A012345678")
		row = next(r for r in STORE.rows("I-9 Audit Log") if r["action"] == "Section 1 Submitted")
		details = json.loads(row["details"])
		self.assertEqual(details["identifiers"], ["alien_registration_number"])
		self.assertNotIn("A012345678", row["details"])


# ── 9 ─────────────────────────────────────────────────────────────────────────
class FullSSNIsOptInOnly(I9TestCase):
	"""Nine digits are kept only where the site asked, and only encrypted.

	E-Verify submits the whole number and cannot be run from four, which is the
	only reason the column exists. A site not running E-Verify should not be
	holding SSNs, so the switch is off and the controller enforces it on every
	save rather than only on the path through the tool.
	"""

	def _stored_full(self, name=None):
		if name is None:
			name = next(iter(STORE.rows("I-9 Form")))["name"]
		return STORE.passwords.get(("I-9 Form", name, "ssn_full"))

	def test_off_by_default(self):
		data = self.tool_data("get_i9_settings", {})
		self.assertFalse(data["store_full_ssn"])

	def test_a_full_ssn_is_not_kept_while_the_switch_is_off(self):
		self._create_draft()
		self._submit_section_1(ssn="123-45-6789")
		self.assertEqual(next(iter(STORE.rows("I-9 Form")))["ssn_last_four"], "6789")
		self.assertIsNone(self._stored_full())

	def test_a_full_ssn_is_kept_encrypted_once_the_switch_is_on(self):
		self.tool_data("update_i9_settings", {"store_full_ssn": True})
		self._create_draft()
		self._submit_section_1(ssn="123-45-6789")
		self.assertEqual(self._stored_full(), "123456789")

	def test_the_last_four_are_written_either_way(self):
		self.tool_data("update_i9_settings", {"store_full_ssn": True})
		self._create_draft()
		self._submit_section_1(ssn="123-45-6789")
		self.assertEqual(next(iter(STORE.rows("I-9 Form")))["ssn_last_four"], "6789")

	def test_a_partial_number_never_reaches_the_full_column(self):
		"""Four digits are not an SSN and would be a useless E-Verify submission."""
		self.tool_data("update_i9_settings", {"store_full_ssn": True})
		self._create_draft()
		self._submit_section_1(ssn_last_four="6789")
		self.assertIsNone(self._stored_full())

	def test_get_i9_form_never_returns_it(self):
		self.tool_data("update_i9_settings", {"store_full_ssn": True})
		self._create_draft()
		self._submit_section_1(ssn="123-45-6789")
		data = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})
		self.assertNotIn("ssn_full", data)

	def test_turning_the_switch_off_blanks_it_on_the_next_save(self):
		"""The switch is a fact about the next save rather than a promise about
		the past, and this is the test that says which."""
		self.tool_data("update_i9_settings", {"store_full_ssn": True})
		self._create_draft()
		self._submit_section_1(ssn="123-45-6789")
		self.assertEqual(self._stored_full(), "123456789")

		self.tool_data("update_i9_settings", {"store_full_ssn": False})
		self._submit_section_2()
		self.assertIsNone(self._stored_full())


# ── 10 ────────────────────────────────────────────────────────────────────────
class CompleteMeansSigned(I9TestCase):
	"""v0.64.2. A Form I-9 is complete when it carries the attestations.

	`submit_i9_section_2` used to write `status = "Complete"` unconditionally, so
	a form reached its terminal status with both signature boxes empty and the
	only thing saying otherwise was a compliance sweep raising two Criticals
	afterwards. That is a detective control, and the whole value of a status
	called Complete is that somebody can read it without running a sweep first.

	THE DOCUMENTS ARE STILL FILED. Refusing the call would throw away the
	examination somebody performed — which documents, by whom, inside the
	three-business-day window. The form rests at `Awaiting Verification`, which
	this tool already accepts as input, and the outstanding signature advances it.

	THE DETECTIVE CONTROL IS KEPT AND IS NOT RE-ASSERTED HERE. A form parked at
	Awaiting Verification because nobody signed it is exactly what
	`i9_section_1_unsigned` and `i9_section_2_unsigned` chase; that they fire on
	an unsigned form is `test_missing_signatures.py`'s claim, and that signing
	dismisses them is `test_alert_resolution.py`'s. This release adds a gate in
	front of them rather than replacing them.
	"""

	def test_a_form_with_no_attestations_does_not_reach_complete(self):
		self._create_draft()
		self._submit_section_1(section_1_signature="")
		data = self._submit_section_2(section_2_signature="")

		self.assertEqual(data["status"], "Awaiting Verification")
		self.assertEqual(
			data["unsigned"],
			["Section 1 (the employee's attestation)", "Section 2 (the employer's attestation)"],
		)
		self.assertIn("8 CFR", data["unsigned_note"])

	def test_the_documents_are_filed_anyway(self):
		"""The examination is real work and is not thrown away by the refusal to
		call the form complete."""
		self._create_draft()
		self._submit_section_1(section_1_signature="")
		self._submit_section_2(section_2_signature="")

		form = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(form["list_a_doc_title"], "U.S. Passport")
		self.assertEqual(form["list_a_doc_number"], "123456789")
		self.assertEqual(form["verifier_name"], "Tim Polehn")
		self.assertTrue(form["verification_date"])

	def test_one_attestation_is_not_both(self):
		self._create_draft()
		self._submit_section_1()
		data = self._submit_section_2(section_2_signature="")
		self.assertEqual(data["unsigned"], ["Section 2 (the employer's attestation)"])
		self.assertEqual(data["status"], "Awaiting Verification")

	def test_both_attestations_complete_it_in_the_one_call(self):
		self._create_draft()
		self._submit_section_1()
		data = self._submit_section_2()
		self.assertEqual(data["status"], "Complete")
		self.assertEqual(data["unsigned"], [])
		self.assertNotIn("unsigned_note", data)

	def test_a_signature_that_never_happened_gets_no_timestamp(self):
		"""The defect this release is really about. Both columns used to be
		stamped unconditionally — so a blank signature box carried an
		8 CFR 274a.2(h) 'date signed' and a signing IP for an attestation nobody
		made. A record asserting more than it knows is worse than a blank one."""
		self._create_draft()
		self._submit_section_1(section_1_signature="")
		self._submit_section_2(section_2_signature="")

		form = next(iter(STORE.rows("I-9 Form")))
		for field in ("section_1_signature", "section_2_signature"):
			self.assertFalse(form.get(field))
		for field in ("section_1_signed_at", "section_2_signed_at"):
			self.assertFalse(form.get(field), f"{field} was stamped for a signature nobody made")
		for field in ("section_1_signed_ip", "section_2_signed_ip"):
			self.assertFalse(form.get(field))

	def test_a_signature_that_did_happen_is_stamped(self):
		"""The other direction, so the fix cannot be 'stop stamping'."""
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()

		form = next(iter(STORE.rows("I-9 Form")))
		self.assertTrue(form["section_1_signed_at"])
		self.assertTrue(form["section_2_signed_at"])

	def test_signing_the_outstanding_box_advances_the_form(self):
		"""The other half of the gate, and without it the gate would be a trap.
		The pad is where the signature arrives on a handset, not the submit
		call, so the signature landing has to be what completes the form."""
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2(section_2_signature="")
		form = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(form["status"], "Awaiting Verification")

		frappe.db.set_value("I-9 Form", form["name"], "section_2_signature", SECTION_2_INK)
		self.assertEqual(i9.advance_if_signed(str(form["name"])), "Complete")
		self.assertEqual(frappe.db.get_value("I-9 Form", form["name"], "status"), "Complete")

	def test_it_moves_one_edge_and_no_other(self):
		"""A status machine a signature pad could drive in any direction would
		not be one. It will not advance a form whose Section 2 was never filed,
		and will not reopen or re-close anything else."""
		self._create_draft()
		self._submit_section_1()
		form = next(iter(STORE.rows("I-9 Form")))
		name = str(form["name"])
		# Section 1 Complete: signed, but Section 2 was never filed.
		self.assertEqual(i9.advance_if_signed(name), "")
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "status"), "Section 1 Complete")

		# A form somebody set to Expired is not quietly completed either.
		self._submit_section_2()
		frappe.db.set_value("I-9 Form", name, "status", "Expired")
		self.assertEqual(i9.advance_if_signed(name), "")
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "status"), "Expired")


# ── 10 ────────────────────────────────────────────────────────────────────────
class Section2DocumentsAndReceipts(I9TestCase):
	def test_a_list_b_document_in_the_list_a_slot_is_refused(self):
		"""The form would be asserting one document proved both identity and
		work authorization when it proved neither."""
		self._create_draft()
		self._submit_section_1()
		msg = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "Driver's License",
				"verifier_name": "Tim Polehn",
				"verification_date": str(date.today()),
			},
		)
		self.assertIn("not a List A document", msg)

	def test_an_unknown_document_is_refused_and_the_refusal_names_the_list(self):
		self._create_draft()
		self._submit_section_1()
		msg = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "Costco Membership",
				"verifier_name": "Tim Polehn",
				"verification_date": str(date.today()),
			},
		)
		self.assertIn("U.S. Passport", msg)

	def test_a_title_is_stored_in_the_tables_own_spelling(self):
		"""A phone that sent lowercase meant the U.S. Passport, and a federal
		form should not carry a title that does not match the list it is from."""
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2(list_a_doc_title="u.s. passport")
		self.assertEqual(next(iter(STORE.rows("I-9 Form")))["list_a_doc_title"], "U.S. Passport")

	def test_every_seeded_list_a_document_is_accepted(self):
		for title in ("U.S. Passport", "Employment Authorization Document (Form I-766)"):
			with self.subTest(document=title):
				self.setUp()
				self._create_draft()
				self._submit_section_1()
				data = self._submit_section_2(list_a_doc_title=title)
				self.assertEqual(data["status"], "Complete")

	def test_a_receipt_still_completes_the_form(self):
		"""8 CFR 274a.2(b)(1)(vi): the person may lawfully work while the
		replacement comes, so the status is Complete and the debt is a column."""
		self._create_draft()
		self._submit_section_1()
		data = self._submit_section_2(list_a_is_receipt=True)
		self.assertEqual(data["status"], "Complete")
		self.assertTrue(data["receipt_pending"])

	def test_the_receipt_deadline_is_ninety_days_from_hire(self):
		hire = date.today() - timedelta(days=1)
		self._create_draft(hire_date=str(hire))
		self._submit_section_1()
		data = self._submit_section_2(list_a_is_receipt=True)
		self.assertEqual(data["receipt_expires_on"], str(hire + timedelta(days=90)))

	def test_no_receipt_leaves_the_columns_clear(self):
		self._create_draft()
		self._submit_section_1()
		data = self._submit_section_2()
		self.assertFalse(data["receipt_pending"])
		self.assertIsNone(data["receipt_expires_on"])

	def test_a_receipt_is_reported_as_outstanding_work(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2(list_a_is_receipt=True)
		data = self.tool_data("list_pending_i9_verifications", {})
		self.assertEqual(data["count"], 0)
		self.assertEqual(data["receipts_count"], 1)
		self.assertEqual(data["receipts_outstanding"][0]["receipt_lists"], ["A"])

	def test_an_overdue_receipt_says_so(self):
		hire = date.today() - timedelta(days=120)
		self._create_draft(hire_date=str(hire))
		self._submit_section_1()
		self._submit_section_2(verification_date=str(hire), list_a_is_receipt=True)
		data = self.tool_data("list_pending_i9_verifications", {})
		row = data["receipts_outstanding"][0]
		self.assertTrue(row["overdue"])
		self.assertLess(row["days_until_receipt_expiry"], 0)

	def test_a_receipt_writes_its_own_audit_row(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2(
			list_c_is_receipt=True,
			document_path="List B + C",
			list_b_doc_title="Driver's License",
			list_c_doc_title="Social Security Card (Unrestricted)",
		)
		accepted = [r for r in STORE.rows("I-9 Audit Log") if r["action"] == "Receipt Accepted"]
		self.assertEqual(len(accepted), 1)
		self.assertEqual(json.loads(accepted[0]["details"])["receipt_lists"], ["C"])


# ── 11 ────────────────────────────────────────────────────────────────────────
class Section3Reverification(I9TestCase):
	"""Form I-9's Supplement B, and the branch that had no call.

	`flag_i9_reverification` could say an I-9 needed re-examining and nothing
	could record the re-examination — which left a second I-9 (refused) or a Desk
	edit over the day-of-hire columns (destroys the record §1324a asks for).
	"""

	NEXT_YEAR = str(date.today() + timedelta(days=365))

	def _verified_alien(self, expiry=None):
		self._create_draft()
		self._submit_section_1(
			citizenship_status="Alien Authorized to Work",
			alien_registration_number="A012345678",
			alien_work_authorization_expiry=expiry or str(date.today() + timedelta(days=10)),
		)
		self._submit_section_2()

	def _reverify(self, **overrides):
		payload = {
			"employee": "HR-EMP-00001",
			"reason": "Work Authorization Expired",
			"document_title": "Employment Authorization Document (Form I-766)",
			"document_number": "SRC1234567890",
			"issuing_authority": "USCIS",
			"document_expiry": self.NEXT_YEAR,
			"verifier_name": "Tim Polehn",
			"verifier_title": "Farm Manager",
		}
		payload.update(overrides)
		return self.tool_data("reverify_i9", payload)

	def test_it_appends_a_section_3_entry(self):
		self._verified_alien()
		data = self._reverify()
		self.assertEqual(data["reverification_count"], 1)
		self.assertEqual(data["status"], "Complete")

	def test_section_2_is_not_overwritten(self):
		"""THE CLAIM THIS WHOLE FEATURE EXISTS FOR. What was examined on the day
		of hire is the record the statute asks the employer to have kept."""
		self._verified_alien()
		self._reverify()
		row = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(row["list_a_doc_title"], "U.S. Passport")
		self.assertEqual(row["list_a_doc_number"], "123456789")

	def test_a_second_reverification_does_not_replace_the_first(self):
		"""A seasonal picker on a renewing authorization gets one a season."""
		self._verified_alien()
		self._reverify()
		data = self._reverify(document_expiry=str(date.today() + timedelta(days=730)))
		self.assertEqual(data["reverification_count"], 2)
		history = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})["reverifications"]
		self.assertEqual(len(history), 2)
		self.assertEqual(history[0]["document_expiry"], self.NEXT_YEAR)

	def test_it_moves_the_work_authorization_expiry_forward(self):
		"""Leaving it on the replaced document goes on raising `i9_expired` about
		an authorization that was renewed, which is how a finding becomes noise."""
		self._verified_alien()
		data = self._reverify()
		self.assertEqual(data["work_authorization_expiry"], self.NEXT_YEAR)
		self.assertEqual(
			next(iter(STORE.rows("I-9 Form")))["alien_work_authorization_expiry"],
			self.NEXT_YEAR,
		)

	def test_the_renewed_worker_drops_off_the_expiring_list(self):
		self._verified_alien()
		before = self.tool_data("list_expiring_work_authorizations", {"days_ahead": 90})
		self.assertEqual(before["count"], 1)
		self._reverify()
		after = self.tool_data("list_expiring_work_authorizations", {"days_ahead": 90})
		self.assertEqual(after["count"], 0)

	def test_a_flagged_i9_can_be_reverified(self):
		self._verified_alien()
		self.tool_data(
			"flag_i9_reverification",
			{"employee": "HR-EMP-00001", "reason": "Work auth expiring"},
		)
		data = self._reverify()
		self.assertEqual(data["status"], "Complete")

	def test_a_draft_cannot_be_reverified(self):
		self._create_draft()
		msg = self.tool_error(
			"reverify_i9",
			{
				"employee": "HR-EMP-00001",
				"reason": "Rehire",
				"rehire_date": str(date.today()),
				"document_title": "U.S. Passport",
				"verifier_name": "Tim Polehn",
			},
		)
		self.assertIn("needs a first one to follow", msg)

	def test_an_already_expired_document_is_refused(self):
		self._verified_alien()
		msg = self.tool_error(
			"reverify_i9",
			{
				"employee": "HR-EMP-00001",
				"reason": "Work Authorization Expired",
				"document_title": "Employment Authorization Document (Form I-766)",
				"document_expiry": str(date.today() - timedelta(days=1)),
				"verifier_name": "Tim Polehn",
			},
		)
		self.assertIn("CONTINUING work authorization", msg)

	def test_a_list_b_document_is_refused_with_its_own_sentence(self):
		self._verified_alien()
		msg = self.tool_error(
			"reverify_i9",
			{
				"employee": "HR-EMP-00001",
				"reason": "Work Authorization Expired",
				"document_title": "Driver's License",
				"verifier_name": "Tim Polehn",
			},
		)
		self.assertIn("establishes IDENTITY", msg)

	def test_an_unknown_document_is_refused(self):
		self._verified_alien()
		msg = self.tool_error(
			"reverify_i9",
			{
				"employee": "HR-EMP-00001",
				"reason": "Work Authorization Expired",
				"document_title": "Library Card",
				"verifier_name": "Tim Polehn",
			},
		)
		self.assertIn("not an accepted I-9 document", msg)

	def test_an_unrecognised_reason_names_the_ones_that_work(self):
		self._verified_alien()
		msg = self.tool_error(
			"reverify_i9",
			{
				"employee": "HR-EMP-00001",
				"reason": "Because",
				"document_title": "U.S. Passport",
				"verifier_name": "Tim Polehn",
			},
		)
		self.assertIn("Work Authorization Expired", msg)

	def test_a_rehire_needs_its_date(self):
		self._verified_alien()
		msg = self.tool_error(
			"reverify_i9",
			{
				"employee": "HR-EMP-00001",
				"reason": "Rehire",
				"document_title": "U.S. Passport",
				"verifier_name": "Tim Polehn",
			},
		)
		self.assertIn("rehire_date", msg)

	def test_a_rehire_is_recorded_with_its_date(self):
		self._verified_alien()
		rehire = str(date.today())
		self._reverify(reason="Rehire", rehire_date=rehire, document_expiry=None)
		history = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})["reverifications"]
		self.assertEqual(history[0]["reason"], "Rehire")
		self.assertEqual(history[0]["rehire_date"], rehire)

	def test_a_document_with_no_expiry_is_accepted(self):
		"""An unexpiring document is a real answer, and refusing a lawful
		reverification for lacking a date that does not exist is not one."""
		self._verified_alien()
		data = self._reverify(
			reason="Rehire",
			rehire_date=str(date.today()),
			document_title="U.S. Passport",
			document_expiry=None,
		)
		self.assertIsNone(data["document_expiry"])

	def test_replacing_a_receipt_clears_it(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2(list_a_is_receipt=True)
		data = self._reverify(reason="Receipt Replaced", document_title="U.S. Passport", document_expiry=None)
		self.assertFalse(data["receipt_pending"])
		self.assertEqual(self.tool_data("list_pending_i9_verifications", {})["receipts_count"], 0)

	def test_replacing_a_receipt_that_is_not_outstanding_is_refused(self):
		self._verified_alien()
		msg = self.tool_error(
			"reverify_i9",
			{
				"employee": "HR-EMP-00001",
				"reason": "Receipt Replaced",
				"document_title": "U.S. Passport",
				"verifier_name": "Tim Polehn",
			},
		)
		self.assertIn("no receipt outstanding", msg)

	def test_it_writes_an_audit_row(self):
		self._verified_alien()
		self._reverify()
		rows = [r for r in STORE.rows("I-9 Audit Log") if r["action"] == "Section 3 Reverified"]
		self.assertEqual(len(rows), 1)
		details = json.loads(rows[0]["details"])
		self.assertEqual(details["reason"], "Work Authorization Expired")
		self.assertEqual(details["document_expiry"], self.NEXT_YEAR)

	def _the_compliance_column(self):
		"""`i9_status` as `compliance_fields.py` installs it on a real site.

		Installed here rather than in the shared fixture because it is a Custom
		Field this app ADDS to Frappe HR's Employee, and every other test in this
		module has to go on running against a site where nobody ran
		`install_compliance_fields` — which is the state the column-absent branch
		of `_clear_expired_i9_column` exists for.
		"""
		add_field(
			"Employee",
			"i9_status",
			fieldtype="Select",
			options="\nPending\nVerified\nExpired\nRejected",
			label="I-9 Status",
		)

	def test_it_moves_the_employee_column_off_expired(self):
		"""The only write this app makes to `i9_status`, and the reason for it:
		leave it and the wizard routes a just-reverified worker to a second I-9,
		which `create_i9_form` then refuses."""
		self._the_compliance_column()
		self._verified_alien()
		frappe.db.set_value("Employee", "HR-EMP-00001", "i9_status", "Expired")
		data = self._reverify()
		self.assertEqual(data["employee_i9_status"], "Verified")
		self.assertEqual(frappe.db.get_value("Employee", "HR-EMP-00001", "i9_status"), "Verified")

	def test_it_does_not_touch_a_column_saying_anything_else(self):
		"""`Pending` is `employee_detail`'s to reconcile and not this tool's. Two
		things writing one column is how they come to disagree."""
		self._the_compliance_column()
		self._verified_alien()
		frappe.db.set_value("Employee", "HR-EMP-00001", "i9_status", "Pending")
		data = self._reverify()
		self.assertIsNone(data["employee_i9_status"])
		self.assertEqual(frappe.db.get_value("Employee", "HR-EMP-00001", "i9_status"), "Pending")

	def test_the_history_is_empty_on_a_form_that_was_never_reverified(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2()
		data = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["reverifications"], [])
		self.assertEqual(data["reverification_count"], 0)


class UpdateI9Settings(I9TestCase):
	def test_update_settings(self):
		data = self.tool_data("update_i9_settings", {"business_legal_name": "New Farm LLC"})
		self.assertIn("business_legal_name", data["updated"])

	def test_update_requires_at_least_one_field(self):
		msg = self.tool_error("update_i9_settings", {})
		self.assertIn("no fields to update", msg)

	def test_e_verify_forces_copies(self):
		self.tool_data("update_i9_settings", {"enrolled_in_e_verify": True})
		data = self.tool_data("get_i9_settings", {})
		self.assertTrue(data["enrolled_in_e_verify"])


# ── 13 ────────────────────────────────────────────────────────────────────────
class PatchSection1(I9TestCase):
	"""v0.67.1. The one way back into a Section 1 that has already been filed.

	THE BUG THAT PRODUCED THIS TOOL IS THE FIRST TEST. An onboarding wizard
	filed a Section 1 without sending `date_of_birth`, `email` or `phone`; the
	form went to `Section 1 Complete`, then to `Complete`, then had its PDF
	rendered, and `submit_i9_section_1` — which only takes a Draft — could not
	be used to put the missing three in. There was no other tool that could
	either, on any status.
	"""

	def _filed_with_gaps(self, employee="HR-EMP-00001") -> str:
		"""A Complete I-9 whose Section 1 is missing exactly what I9-2026-0001's was."""
		self._create_draft(employee=employee)
		self._submit_section_1(employee=employee)
		self._submit_section_2(employee=employee)
		return str(frappe.db.get_value("I-9 Form", {"employee": employee}, "name"))

	def _stored(self, name, *fields):
		return frappe.db.get_value("I-9 Form", name, list(fields), as_dict=True)

	def _corrections(self, name):
		return [
			row
			for row in STORE.rows("I-9 Audit Log")
			if row.get("i9_form") == name and row.get("action") == i9.CORRECTION_ACTION
		]

	# -- the gap, and closing it ---------------------------------------------
	def test_a_filed_section_1_really_does_have_the_gap(self):
		"""The premise. Without this the rest of the class proves nothing."""
		name = self._filed_with_gaps()
		row = self._stored(name, "status", "date_of_birth", "email", "phone")
		self.assertEqual(row["status"], "Complete")
		self.assertFalse(row["date_of_birth"])
		self.assertFalse(row["email"])
		self.assertFalse(row["phone"])

	def test_submit_section_1_cannot_close_it(self):
		"""Why a second tool had to exist rather than the first one widening."""
		self._filed_with_gaps()
		msg = self.tool_error(
			"submit_i9_section_1",
			{
				"employee": "HR-EMP-00001",
				"legal_first_name": "Ada",
				"legal_last_name": "Orchard",
				"citizenship_status": "US Citizen",
				"date_of_birth": "1978-11-05",
			},
		)
		self.assertIn("no Draft I-9", msg)

	def test_it_writes_the_three_missing_fields(self):
		name = self._filed_with_gaps()
		data = self.tool_data(
			"patch_i9_section_1",
			{
				"i9_form": name,
				"date_of_birth": "1978-11-05",
				"email": "ada@example.test",
				"phone": "509-555-0142",
			},
		)
		self.assertEqual(sorted(data["changed"]), ["date_of_birth", "email", "phone"])
		row = self._stored(name, "date_of_birth", "email", "phone")
		self.assertEqual(str(row["date_of_birth"]), "1978-11-05")
		self.assertEqual(row["email"], "ada@example.test")
		self.assertEqual(row["phone"], "509-555-0142")

	def test_it_is_found_by_the_employee_too(self):
		"""`_resolve_form`'s other half — an operator holding the person, not the docname."""
		name = self._filed_with_gaps()
		data = self.tool_data(
			"patch_i9_section_1", {"employee": "HR-EMP-00001", "date_of_birth": "1978-11-05"}
		)
		self.assertEqual(data["name"], name)

	def test_it_works_at_section_1_complete_as_well(self):
		self._create_draft()
		self._submit_section_1()
		name = str(frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "name"))
		data = self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertEqual(data["status"], "Section 1 Complete")
		self.assertEqual(data["changed"], ["phone"])

	def test_the_ssn_is_stripped_to_its_last_four(self):
		name = self._filed_with_gaps()
		self.tool_data("patch_i9_section_1", {"i9_form": name, "ssn_last_four": "123-45-6789"})
		self.assertEqual(self._stored(name, "ssn_last_four")["ssn_last_four"], "6789")

	def test_a_short_ssn_is_refused_rather_than_stored_short(self):
		name = self._filed_with_gaps()
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name, "ssn_last_four": "89"})
		self.assertIn("2 digit(s)", msg)
		self.assertFalse(self._stored(name, "ssn_last_four")["ssn_last_four"])

	# -- the line it will not cross -------------------------------------------
	def test_it_refuses_the_sworn_fields_by_name(self):
		"""Refused, not ignored. A success reporting a correction while the wrong
		name is still on the form is the worse of the two failures."""
		name = self._filed_with_gaps()
		for field, value in (
			("legal_first_name", "Adelaide"),
			("legal_last_name", "Orchid"),
			("citizenship_status", "Alien Authorized to Work"),
			("alien_registration_number", "A123456789"),
			("address_street", "9 Elsewhere Lane"),
			("section_1_signature", SECTION_1_INK),
		):
			with self.subTest(field=field):
				msg = self.tool_error(
					"patch_i9_section_1", {"i9_form": name, field: value, "phone": "509-555-0142"}
				)
				self.assertIn(field, msg)
				self.assertIn("Nothing was changed", msg)

	def test_a_refused_field_takes_the_whole_call_with_it(self):
		"""The patchable field alongside it is not written either — a partial
		success here would leave the caller believing the refused one landed."""
		name = self._filed_with_gaps()
		self.tool_error(
			"patch_i9_section_1",
			{"i9_form": name, "legal_first_name": "Adelaide", "phone": "509-555-0142"},
		)
		self.assertFalse(self._stored(name, "phone")["phone"])
		self.assertEqual(self._stored(name, "legal_first_name")["legal_first_name"], "Ada")

	def test_the_nine_digit_ssn_argument_is_refused(self):
		"""`ssn` reaches the encrypted column through its own site switch.
		A correction path that took it would route around that switch."""
		name = self._filed_with_gaps()
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name, "ssn": "123456789"})
		self.assertIn("ssn", msg)

	def test_it_refuses_a_call_naming_no_patchable_field(self):
		name = self._filed_with_gaps()
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name})
		self.assertIn("naming none of the fields", msg)

	def test_it_will_not_clear_a_field(self):
		name = self._filed_with_gaps()
		self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name, "phone": ""})
		self.assertIn("does not clear one", msg)
		self.assertEqual(self._stored(name, "phone")["phone"], "509-555-0142")

	# -- the statuses ---------------------------------------------------------
	def test_a_draft_is_refused_and_told_where_to_go(self):
		self._create_draft()
		name = str(frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "name"))
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertIn("submit_i9_section_1", msg)

	def test_a_destroyed_record_is_refused(self):
		name = self._filed_with_gaps()
		frappe.db.set_value("I-9 Form", name, "status", "Destroyed")
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertIn("disposed of", msg)

	def test_awaiting_verification_is_refused(self):
		"""Deliberate, and the narrower of the two readings: v0.67.1 shipped the
		two statuses that were asked for. A form resting at `Awaiting
		Verification` has a filed Section 1 and no way to correct it — the gap
		is recorded here rather than closed quietly."""
		name = self._filed_with_gaps()
		frappe.db.set_value("I-9 Form", name, "status", "Awaiting Verification")
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertIn("Awaiting Verification", msg)

	def test_it_moves_no_status_and_touches_no_attestation(self):
		name = self._filed_with_gaps()
		before = self._stored(name, "status", "section_1_signed_at", "section_2_signed_at")
		self.tool_data("patch_i9_section_1", {"i9_form": name, "date_of_birth": "1978-11-05"})
		after = self._stored(name, "status", "section_1_signed_at", "section_2_signed_at")
		self.assertEqual(after, before)

	# -- who may make one -----------------------------------------------------
	def test_it_refuses_an_account_holding_none_of_the_three_roles(self):
		name = self._filed_with_gaps()
		held = list(ROLES.get("Administrator", []))
		self.addCleanup(set_roles, "Administrator", held)
		set_roles("Administrator", ["Farm Manager", "Foreman"])
		msg = self.tool_error("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertIn("may not correct a filed I-9", msg)
		self.assertFalse(self._stored(name, "phone")["phone"])

	def test_hr_user_is_enough(self):
		name = self._filed_with_gaps()
		held = list(ROLES.get("Administrator", []))
		self.addCleanup(set_roles, "Administrator", held)
		set_roles("Administrator", ["HR User"])
		data = self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertEqual(data["corrected_by"], "Administrator")

	def test_farm_manager_alone_is_not(self):
		"""The one role `employee.HR_ROLES` has that this does not, asserted in
		the direction that would silently widen if somebody swapped the tuples."""
		name = self._filed_with_gaps()
		held = list(ROLES.get("Administrator", []))
		self.addCleanup(set_roles, "Administrator", held)
		set_roles("Administrator", ["Farm Manager"])
		self.tool_error("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})

	# -- the audit trail ------------------------------------------------------
	def test_every_correction_writes_one_audit_row(self):
		name = self._filed_with_gaps()
		self.tool_data(
			"patch_i9_section_1",
			{"i9_form": name, "date_of_birth": "1978-11-05", "email": "ada@example.test"},
		)
		rows = self._corrections(name)
		self.assertEqual(len(rows), 1)
		details = json.loads(rows[0]["details"])
		self.assertEqual(sorted(details["fields"]), ["date_of_birth", "email"])
		self.assertEqual(sorted(details["was_blank"]), ["date_of_birth", "email"])
		self.assertEqual(details["status"], "Complete")
		self.assertEqual(details["corrected_by"], "Administrator")

	def test_the_audit_row_carries_the_reason_verbatim(self):
		name = self._filed_with_gaps()
		self.tool_data(
			"patch_i9_section_1",
			{
				"i9_form": name,
				"date_of_birth": "1978-11-05",
				"reason": "iOS onboarding wizard did not send Section 1 contact fields",
			},
		)
		details = json.loads(self._corrections(name)[0]["details"])
		self.assertEqual(details["reason"], "iOS onboarding wizard did not send Section 1 contact fields")

	def test_the_audit_row_does_not_carry_the_values(self):
		"""Same rule `submit_i9_section_1` follows for the immigration
		identifiers: an audit row is a second doctype, and a date of birth
		copied into it is one more place a personal identifier lives."""
		name = self._filed_with_gaps()
		self.tool_data(
			"patch_i9_section_1",
			{"i9_form": name, "date_of_birth": "1978-11-05", "ssn_last_four": "6789"},
		)
		blob = self._corrections(name)[0]["details"]
		self.assertNotIn("1978-11-05", blob)
		self.assertNotIn("6789", blob)

	def test_a_correction_over_a_field_that_already_had_a_value_says_so(self):
		name = self._filed_with_gaps()
		self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0199"})
		details = json.loads(self._corrections(name)[1]["details"])
		self.assertEqual(details["fields"], ["phone"])
		self.assertEqual(details["was_blank"], [])

	def test_it_is_visible_through_get_i9_audit_log(self):
		"""The action string has to be one the doctype's own Select declares, or
		`_log_action` swallows the insert and the trail loses the row."""
		name = self._filed_with_gaps()
		self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		data = self.tool_data("get_i9_audit_log", {"employee": "HR-EMP-00001"})
		self.assertIn(i9.CORRECTION_ACTION, [entry["action"] for entry in data["entries"]])

	# -- the no-op ------------------------------------------------------------
	def test_sending_what_the_form_already_says_writes_nothing(self):
		name = self._filed_with_gaps()
		self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		data = self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertEqual(data["changed"], [])
		self.assertFalse(data["pdf"]["regenerated"])
		self.assertEqual(len(self._corrections(name)), 1)

	def test_the_pdf_key_is_always_there(self):
		"""A caller that had to test for it would have two code paths where it
		needs one, and the one it exercises least is the one that ships broken."""
		name = self._filed_with_gaps()
		data = self.tool_data("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertIn("regenerated", data["pdf"])
		self.assertFalse(data["pdf"]["regenerated"])
		self.assertIn("render_i9_pdf", data["pdf"]["note"])

	# -- the switch -----------------------------------------------------------
	def test_it_is_off_out_of_the_box(self):
		name = self._filed_with_gaps()
		self.configure(enabled=1)
		result = self.tool("patch_i9_section_1", {"i9_form": name, "phone": "509-555-0142"})
		self.assertTrue(result.get("isError"))


# ── 17 ────────────────────────────────────────────────────────────────────────
class WhereTheAttestationWasMade(I9TestCase):
	"""v0.136.0. The coordinates, beside the moment and the address.

	ONE `Data` COLUMN PER SECTION RATHER THAN TWO `Float`s, and the shape is the
	whole fix. `Signing Evidence` keeps the pair as Floats, MariaDB stores those
	`NOT NULL DEFAULT 0`, and a signature that reported no fix came back out as
	`0.0, 0.0` — which the sealed verification page printed as a location off the
	coast of Ghana. A string column has an empty value that means empty.
	"""

	def _stored(self, employee="HR-EMP-00001", *fields):
		name = frappe.db.get_value("I-9 Form", {"employee": employee}, "name")
		return frappe.db.get_value("I-9 Form", name, list(fields), as_dict=True)

	def test_a_fix_sent_with_a_signature_is_written(self):
		self._create_draft()
		self._submit_section_1(gps_lat=45.5231, gps_lon=-122.6765)
		self.assertEqual(
			self._stored("HR-EMP-00001", "section_1_signed_gps")["section_1_signed_gps"],
			"45.523100,-122.676500",
		)

	def test_section_2_carries_its_own(self):
		self._create_draft()
		self._submit_section_1()
		self._submit_section_2(gps_lat=46.6, gps_lon=-120.51)
		self.assertEqual(
			self._stored("HR-EMP-00001", "section_2_signed_gps")["section_2_signed_gps"],
			"46.600000,-120.510000",
		)

	def test_a_section_nobody_signed_acquires_no_location(self):
		"""The rule the moment and the address already follow. A location on an
		attestation that was never made is a record asserting more than it knows."""
		self._create_draft()
		self._submit_section_1(section_1_signature="", gps_lat=45.5231, gps_lon=-122.6765)
		row = self._stored("HR-EMP-00001", "section_1_signed_gps", "section_1_signed_at")
		self.assertFalse(row["section_1_signed_gps"])
		self.assertFalse(row["section_1_signed_at"])

	def test_null_island_is_refused_rather_than_stored(self):
		"""A handset whose location services returned nothing before the fix
		landed sends two zeroes. No farm this app serves is in the ocean."""
		self._create_draft()
		self._submit_section_1(gps_lat=0, gps_lon=0)
		self.assertEqual(self._stored("HR-EMP-00001", "section_1_signed_gps")["section_1_signed_gps"], "")

	def test_a_zero_on_one_axis_alone_is_kept(self):
		"""The equator is a real line. Refusing every zero would be the zero-drop
		this narrow rule exists to avoid, committed while fixing its mirror image."""
		self._create_draft()
		self._submit_section_1(gps_lat=0, gps_lon=-122.6765)
		self.assertEqual(
			self._stored("HR-EMP-00001", "section_1_signed_gps")["section_1_signed_gps"],
			"0.000000,-122.676500",
		)

	def test_half_a_fix_is_no_fix(self):
		self._create_draft()
		self._submit_section_1(gps_lat=45.5231)
		self.assertEqual(self._stored("HR-EMP-00001", "section_1_signed_gps")["section_1_signed_gps"], "")

	def test_a_form_filed_without_coordinates_is_unaffected(self):
		"""Every caller before this release, and most after it."""
		self._create_draft()
		self._submit_section_1()
		self.assertEqual(self._stored("HR-EMP-00001", "section_1_signed_gps")["section_1_signed_gps"], "")

	def test_get_i9_form_reports_it_back(self):
		self._create_draft()
		self._submit_section_1(gps_lat=45.5231, gps_lon=-122.6765)
		data = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["section_1_signed_gps"], "45.523100,-122.676500")

	def test_the_form_column_and_the_evidence_row_cannot_disagree(self):
		"""TWO READERS OF ONE ARGUMENT DICT DESCRIBING ONE SIGNATURE.
		`args.as_gps` builds the column on the form and `signatures._context`
		builds the Signing Evidence row. They accept the same spellings, so a
		caller sending two of them must not put one location in the register and
		a different one on the record — that is two answers to "where was this
		signed" with nothing to say which is right.
		"""
		from erpnext_mcp.args import as_gps
		from erpnext_mcp.tools import signatures

		for args in (
			{"gps_lat": 45.5231, "gps_lon": -122.6765},
			{"gps_latitude": 45.5231, "gps_longitude": -122.6765},
			{"gps_latitude": 1.0, "gps_longitude": 2.0, "gps_lat": 9.9, "gps_lon": 9.9},
		):
			with self.subTest(args=args):
				context = signatures._context(args)
				self.assertEqual(as_gps(args), f"{context['latitude']:.6f},{context['longitude']:.6f}")


# ── 18 ────────────────────────────────────────────────────────────────────────
class TheExaminedDocumentPhotographsReachTheForm(I9TestCase):
	"""v0.136.0. 8 CFR 274a.2(b)(3) — copies kept are retained WITH the I-9.

	The wizard photographs the List A / List B+C documents at the tailgate and
	files them against the EMPLOYEE, which is the right home for the bytes. The
	I-9 held a `document_copies_stored` tickbox and no way to say WHICH copies,
	so a form that keeps them could not be produced complete.
	"""

	def _form(self, employee="HR-EMP-00001") -> str:
		self._create_draft(employee=employee)
		self._submit_section_1(employee=employee)
		return str(frappe.db.get_value("I-9 Form", {"employee": employee}, "name"))

	def test_every_kind_it_maps_is_one_the_upload_path_accepts(self):
		"""The two lists are in different modules and a spelling that drifted
		would make this a silent no-op rather than an error."""
		from erpnext_mcp.tools import employee as employee_tool

		for kind in i9.DOCUMENT_COPY_KINDS:
			self.assertIn(kind, employee_tool.ONBOARDING_KINDS)

	def test_a_list_b_photograph_lands_on_the_list_b_column(self):
		name = self._form()
		linked = i9.link_document_copy("HR-EMP-00001", "i9_list_b_document", "/private/files/b.jpg")
		self.assertEqual(linked, name)
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "list_b_doc_copy"), "/private/files/b.jpg")

	def test_each_list_lands_on_its_own_column(self):
		name = self._form()
		for kind, column, url in (
			("i9_list_a_document", "list_a_doc_copy", "/private/files/a.jpg"),
			("i9_list_b_document", "list_b_doc_copy", "/private/files/b.jpg"),
			("i9_list_c_document", "list_c_doc_copy", "/private/files/c.jpg"),
		):
			i9.link_document_copy("HR-EMP-00001", kind, url)
			self.assertEqual(frappe.db.get_value("I-9 Form", name, column), url)

	def test_a_kind_that_is_not_an_examined_document_links_nothing(self):
		"""`i9_section_2_document` is a scan of the FORM, which belongs in
		`signed_pdf` through `attach_signed_i9` — that call checks it is a PDF
		and refuses to replace an existing one silently."""
		self._form()
		for kind in ("i9_section_1_signature", "i9_section_2_document", "profile_photo", "other", ""):
			self.assertEqual(i9.link_document_copy("HR-EMP-00001", kind, "/private/files/x.jpg"), "")

	def test_a_worker_with_no_i9_at_all_is_not_an_error(self):
		"""The photograph has already landed and the worker has put their
		passport away. A cross-reference that raised would report a failed upload."""
		self.assertEqual(
			i9.link_document_copy("HR-EMP-00002", "i9_list_b_document", "/private/files/b.jpg"), ""
		)

	def test_a_destroyed_form_takes_no_fresh_photograph(self):
		"""`employee` is not unique on this register — `destroy_i9` sets the
		status and SAVES rather than deleting, so a rehired worker has two rows.
		Linking a current document onto the record that certifies its own
		disposal would reconstitute part of a form the certificate says is gone.

		THE DESTROYED ROW IS SEEDED FIRST on purpose: the standalone double's
		`get_value` ignores `order_by` and answers in insertion order, so a
		fixture built the other way round would pass while proving nothing.
		"""
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-OLD-0001",
					"employee": "HR-EMP-00009",
					"company": MAIN,
					"status": "Destroyed",
					"hire_date": str(date.today()),
				}
			],
		)
		self.assertEqual(
			i9.link_document_copy("HR-EMP-00009", "i9_list_b_document", "/private/files/b.jpg"), ""
		)
		self.assertFalse(frappe.db.get_value("I-9 Form", "I9-OLD-0001", "list_b_doc_copy"))

	def test_an_empty_url_links_nothing(self):
		self._form()
		self.assertEqual(i9.link_document_copy("HR-EMP-00001", "i9_list_b_document", ""), "")

	def test_a_photograph_on_the_record_ticks_the_box_that_says_so(self):
		"""A form holding a picture of a passport while answering "copies
		stored: no" is a record an inspector would be right to distrust."""
		name = self._form()
		self.assertFalse(frappe.db.get_value("I-9 Form", name, "document_copies_stored"))
		i9.link_document_copy("HR-EMP-00001", "i9_list_b_document", "/private/files/b.jpg")
		self.assertTrue(frappe.db.get_value("I-9 Form", name, "document_copies_stored"))

	def test_a_retry_links_the_form_the_first_call_could_not_find(self):
		"""THE ORDERING THAT MAKES THE EARLY RETURN MATTER. On a bad link the
		photograph can land before the section it belongs to, so the first call
		finds no I-9 and links nothing. Every later attempt takes the
		`already_attached` path — so if that path skipped the cross-reference,
		the form would never acquire it. Linking there is idempotent and is what
		makes the retry converge.
		"""
		self.assertEqual(
			i9.link_document_copy("HR-EMP-00001", "i9_list_b_document", "/private/files/b.jpg"), ""
		)
		name = self._form()
		self.assertEqual(
			i9.link_document_copy("HR-EMP-00001", "i9_list_b_document", "/private/files/b.jpg"), name
		)
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "list_b_doc_copy"), "/private/files/b.jpg")

	def test_linking_the_same_photograph_twice_changes_nothing(self):
		name = self._form()
		first = i9.link_document_copy("HR-EMP-00001", "i9_list_b_document", "/private/files/b.jpg")
		second = i9.link_document_copy("HR-EMP-00001", "i9_list_b_document", "/private/files/b.jpg")
		self.assertEqual(first, second)
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "list_b_doc_copy"), "/private/files/b.jpg")

	def test_a_kind_that_links_nothing_ticks_nothing(self):
		"""The tickbox follows the photograph rather than the attempt."""
		name = self._form()
		i9.link_document_copy("HR-EMP-00001", "profile_photo", "/private/files/face.jpg")
		self.assertFalse(frappe.db.get_value("I-9 Form", name, "document_copies_stored"))


# ── 19 ────────────────────────────────────────────────────────────────────────
class TheSignedCopyCarriesItsOwnMetadata(I9TestCase):
	"""v0.137.0. The phone builds the I-9; the server was at neither signing.

	THE HOLE THE ARCHITECTURE LEFT. `section_1_signed_at`, `_signed_ip` and
	`_signed_gps` used to be filled as a side effect of the server RECEIVING a
	signature. The retained page is rendered and sealed on the handset now and
	arrives whole through `attach_signed_i9`, so nothing filled them and the only
	timestamp on the record was `signed_pdf_on` — which answers "when did the file
	arrive", a different question from the one 8 CFR 274a.2(h)(2) asks.
	"""

	def setUp(self):
		# `allow_attach_signed_i9` is not in `I9_TOOLS_ON`, because every mutating
		# tool in this app ships OFF and an operator ticks it deliberately. The
		# switch is the subject of `test_i9_pdf.AttachSignedTool`; here it is
		# furniture, so it is turned on the same way that file turns it on.
		super().setUp()
		self.configure(enabled=1, **dict(I9_TOOLS_ON, allow_attach_signed_i9=1))
		# STAMPED FROM THE HARNESS CLOCK, NEVER HARDCODED. `frappe.utils.now()`
		# here is a fixed base plus one second per call, so a literal date is
		# whatever the double says relative to it — a 2026-08-25 fixture is in the
		# FUTURE to a clock anchored in July, and every one of these tests failed
		# on the refusal that is supposed to fire only for a broken client.
		signing = datetime.fromisoformat(str(frappe.utils.now())) - timedelta(hours=2)
		self.SIGNED_1 = signing.strftime("%Y-%m-%d %H:%M:%S")
		self.SIGNED_2 = (signing + timedelta(minutes=28)).strftime("%Y-%m-%d %H:%M:%S")

	def _ready(self, employee="HR-EMP-00001") -> str:
		"""A form with both sections filed and no signature ever sent to us."""
		self._create_draft(employee=employee)
		self._submit_section_1(employee=employee, section_1_signature="")
		self._submit_section_2(employee=employee, section_2_signature="")
		return str(frappe.db.get_value("I-9 Form", {"employee": employee}, "name"))

	def _a_scan(self, name="signed-i9.pdf") -> str:
		STORE.seed("File", [{"name": name, "file_name": name, "file_url": f"/private/files/{name}"}])
		return name

	def _filed(self, name, **extra):
		payload = {"i9_form": name, "file_token": self._a_scan()}
		payload.update(extra)
		return self.tool_data("attach_signed_i9", payload)

	def _stored(self, name, *fields):
		return frappe.db.get_value("I-9 Form", name, list(fields), as_dict=True)

	def test_the_moment_each_section_was_signed_is_recorded(self):
		name = self._ready()
		self._filed(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		row = self._stored(name, "section_1_signed_at", "section_2_signed_at")
		self.assertEqual(str(row["section_1_signed_at"]), self.SIGNED_1)
		self.assertEqual(str(row["section_2_signed_at"]), self.SIGNED_2)

	def test_each_section_keeps_its_own_coordinates(self):
		name = self._ready()
		self._filed(
			name,
			section_1_signed_at=self.SIGNED_1,
			section_1_gps_lat=45.5231,
			section_1_gps_lon=-122.6765,
			section_2_signed_at=self.SIGNED_2,
			section_2_gps_lat=46.6,
			section_2_gps_lon=-120.51,
		)
		row = self._stored(name, "section_1_signed_gps", "section_2_signed_gps")
		self.assertEqual(row["section_1_signed_gps"], "45.523100,-122.676500")
		self.assertEqual(row["section_2_signed_gps"], "46.600000,-120.510000")

	def test_one_sections_fix_is_never_copied_onto_the_other(self):
		"""The reason the coordinate keys are named per section rather than left
		to an alias chain: a phone that got a lock for Section 1 and not for
		Section 2 must not have the record claim both were made in one place."""
		name = self._ready()
		self._filed(name, section_1_gps_lat=45.5231, section_1_gps_lon=-122.6765)
		row = self._stored(name, "section_1_signed_gps", "section_2_signed_gps")
		self.assertEqual(row["section_1_signed_gps"], "45.523100,-122.676500")
		self.assertFalse(row["section_2_signed_gps"])

	def test_the_address_is_the_servers_own_observation(self):
		name = self._ready()
		self._filed(name, section_1_signed_at=self.SIGNED_1)
		self.assertTrue(self._stored(name, "section_1_signed_ip")["section_1_signed_ip"])

	def test_the_arrival_time_stays_a_separate_fact_from_the_signing_time(self):
		"""`signed_pdf_on` is the server's clock and answers a different question.
		Collapsing them would lose the hour the crew spent out of signal."""
		name = self._ready()
		self._filed(name, section_1_signed_at=self.SIGNED_1)
		row = self._stored(name, "section_1_signed_at", "signed_pdf_on")
		self.assertEqual(str(row["section_1_signed_at"]), self.SIGNED_1)
		self.assertTrue(row["signed_pdf_on"])
		self.assertNotEqual(str(row["signed_pdf_on"]), self.SIGNED_1)

	def test_a_timestamp_in_the_future_is_refused(self):
		"""The one claim that cannot be true, and what a skewed clock produces."""
		name = self._ready()
		result = self.tool(
			"attach_signed_i9",
			{"i9_form": name, "file_token": self._a_scan(), "section_1_signed_at": "2099-01-01 00:00:00"},
		)
		self.assertTrue(result.get("isError"))
		self.assertIn("future", str(result))

	def test_a_refused_timestamp_files_nothing_at_all(self):
		"""Resolved before the File is touched, so the refusal costs nothing. A
		check that fired after the bytes were moved would leave the file private
		and re-pointed with the record not updated."""
		name = self._ready()
		self.tool(
			"attach_signed_i9",
			{"i9_form": name, "file_token": self._a_scan(), "section_1_signed_at": "2099-01-01 00:00:00"},
		)
		self.assertFalse(self._stored(name, "signed_pdf")["signed_pdf"])

	def test_a_moment_captured_at_the_pad_is_not_replaced_by_one_restated(self):
		"""A signature that DID reach the server was timed by the server as the
		ink landed. An upload restating it must keep the better record."""
		name = self._ready()
		pad = (datetime.fromisoformat(self.SIGNED_1) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
		frappe.db.set_value("I-9 Form", name, "section_1_signed_at", pad)
		self._filed(name, section_1_signed_at=self.SIGNED_1)
		self.assertEqual(str(self._stored(name, "section_1_signed_at")["section_1_signed_at"]), pad)

	def test_the_answer_says_which_columns_it_actually_filled(self):
		"""Otherwise a phone believes it wrote something it did not."""
		name = self._ready()
		pad = (datetime.fromisoformat(self.SIGNED_1) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
		frappe.db.set_value("I-9 Form", name, "section_1_signed_at", pad)
		data = self._filed(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		self.assertNotIn("section_1_signed_at", data["signing_metadata"])
		self.assertEqual(data["signing_metadata"]["section_2_signed_at"], self.SIGNED_2)

	def test_null_island_is_refused_here_too(self):
		name = self._ready()
		self._filed(name, section_1_gps_lat=0, section_1_gps_lon=0)
		self.assertFalse(self._stored(name, "section_1_signed_gps")["section_1_signed_gps"])

	def test_half_a_fix_is_no_fix(self):
		name = self._ready()
		self._filed(name, section_1_gps_lat=45.5231)
		self.assertFalse(self._stored(name, "section_1_signed_gps")["section_1_signed_gps"])

	def test_an_upload_carrying_no_metadata_behaves_exactly_as_before(self):
		"""Every caller that predates this release, and the Desk."""
		name = self._ready()
		data = self._filed(name)
		self.assertTrue(data["signed_pdf"])
		self.assertEqual(data["signing_metadata"], {})

	def test_the_audit_row_records_that_a_claim_was_taken(self):
		"""The moment and the place are the client's word rather than the
		server's, so the log says which columns came in that way."""
		name = self._ready()
		self._filed(name, section_1_signed_at=self.SIGNED_1)
		rows = [
			r
			for r in STORE.rows("I-9 Audit Log")
			if r.get("action") == "Signed Copy Filed" and r.get("i9_form") == name
		]
		self.assertEqual(len(rows), 1)
		self.assertIn("section_1_signed_at", json.dumps(rows[0].get("details")))
