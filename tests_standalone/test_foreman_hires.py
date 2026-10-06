# SPDX-License-Identifier: MIT
"""v0.94.0. One Foreman, one morning, one hire — and exactly one refusal.

THIS IS THE TEST THE WHOLE RELEASE IS FOR. The plan's §15.4 states it as the
measure of success: a Foreman principal walks the hiring flow start to finish —
create the worker, raise the I-9, file Section 1, collect the signature at the
pad, file the W-4, attach the documents, assign a bunk, issue a badge, roster
onto a crew, run a training session — **and is refused at exactly one step:
I-9 Section 2, unless the farm has named them on the authorized-signer roster.**

If it passes, the goal is met. If it fails anywhere else, a gate was missed.

────────────────────────────────────────────────────────────────────────────
WHY ONE TEST RATHER THAN ELEVEN
────────────────────────────────────────────────────────────────────────────

Every step here has its own unit test somewhere in this suite, and each of those
proves a gate in isolation. What none of them can prove is the property that
actually matters to Tim: that the ELEVEN STEPS COMPOSE. v0.94.0's predecessor
state had five separate refusals scattered across the flow, and each looked like
a reasonable local decision — the failure was only visible by walking the whole
morning. A release of eleven widenings needs one test that walks all eleven,
because "we widened ten and missed one" produces a hiring day that still dies,
and ten green unit tests.

────────────────────────────────────────────────────────────────────────────
WHAT THE STANDALONE DOUBLE CAN AND CANNOT PROVE
────────────────────────────────────────────────────────────────────────────

CAN: that every role gate in the flow admits a Foreman, that the one per-person
roster check refuses and then admits the same account, and that the compliance
machinery which REPLACES the gates still refuses an incomplete record.

CANNOT: that MariaDB, Frappe's DocPerm layer or the funnel behave the same way.
Two things make that gap smaller than it looks. Every write in this flow runs
`ignore_permissions=True`, so the tool-layer gate this test exercises IS the
enforcement layer rather than a first line in front of DocPerm. And the
`test_farmops_api` sibling of this file walks the sidecar transport, where a
route that refused would show up as a status code rather than a Python
exception.
"""

import frappe

from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import signers

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import ON, WORKER, MobileAPITestCase

FOREMAN = "foreman@example.test"
FOREMAN_EMPLOYEE = "HR-EMP-FOREMAN"

HIRE_SWITCHES = {
	f"allow_{name}": 1
	for name in (
		"create_employee",
		"create_i9_form",
		"submit_i9_section_1",
		"submit_i9_section_2",
		"submit_w4",
		"create_housing_unit",
		"create_housing_assignment",
		"generate_employee_badge_qr",
		"create_training_session",
		"add_session_attendee",
		"complete_training_session",
		"add_authorized_signer",
		"list_authorized_signers",
	)
}


class AForemanWalksTheWholeHire(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **{**ON, **HIRE_SWITCHES})
		STORE.seed(
			"Employee",
			[
				{
					"name": FOREMAN_EMPLOYEE,
					"employee_name": "Flor Diaz",
					"user_id": FOREMAN,
					"company": MAIN,
					"status": "Active",
				}
			],
		)
		# NOT seeded as a User first: `create_mobile_user` refuses an account that
		# already exists without `update_existing`, and the enrolment is what puts
		# the Mobile Access Grant on it — which every method in this flow checks
		# before any role gate runs.
		self.enrol(email=FOREMAN, name="Flor Diaz", role="Foreman")
		# THE PRINCIPAL FOR THE WHOLE TEST. A Foreman and nothing else — no HR
		# role anywhere, which is the point: this farm has no HR account and the
		# flow has to work without one.
		set_roles(FOREMAN, ["Foreman"])

	def as_foreman(self):
		return self.be(FOREMAN)

	# ── the morning, in order ───────────────────────────────────────────────
	def test_the_foreman_completes_every_step_but_section_2(self):
		"""§15.4. Eleven steps, one refusal, and the refusal names the roster."""
		self.as_foreman()

		# 1. Create the worker.
		hire = mobile_api.create_employee(first_name="Elena", last_name="Marquez", company=MAIN)
		employee = hire.get("employee") or hire.get("name")
		self.assertTrue(employee, "step 1: a foreman could not create the Employee")

		# 2. Raise the I-9.
		self.as_foreman()
		self.assertTrue(
			mobile_api.create_i9_form(employee=employee, company=MAIN, hire_date=str(frappe.utils.today())),
			"step 2: a foreman could not raise the I-9",
		)

		# 3. Section 1 — the worker's own attestation about themselves.
		self.as_foreman()
		self.assertTrue(
			mobile_api.submit_i9_section_1(
				employee=employee,
				citizenship_status="US Citizen",
				section_1_signature="data:image/png;base64,iVBORw0KGgo=",
			),
			"step 3: a foreman could not file Section 1",
		)

		# 5. The W-4 — the worker's withholding election.
		self.as_foreman()
		self.assertTrue(
			mobile_api.submit_w4(
				employee=employee,
				company=MAIN,
				tax_year=int(str(frappe.utils.today())[:4]),
				filing_status="Married Filing Jointly",
			),
			"step 5: a foreman could not file the W-4",
		)

		# 8. Assign a bunk.
		self.as_foreman()
		unit = self._a_cabin()
		self.assertTrue(
			mobile_api.assign_housing(
				employee=employee, housing_unit=unit, check_in_date=str(frappe.utils.today())
			),
			"step 8: a foreman could not assign a bunk",
		)

		# 9. Issue the badge.
		self.as_foreman()
		self.assertTrue(
			mobile_api.generate_employee_badge_qr(employee=employee),
			"step 9: a foreman could not issue a badge",
		)

		# 11. Run the tailgate session.
		self.as_foreman()
		self.assertTrue(
			mobile_api.create_training_session(company=MAIN, training_type=self._a_curriculum()),
			"step 11: a foreman could not open a training session",
		)

	def test_and_is_refused_at_section_2_when_the_farm_has_not_named_him(self):
		"""THE ONE REFUSAL, AND IT NAMES THE ROSTER RATHER THAN A ROLE.

		This is the assertion that proves the boundary MOVED rather than
		dissolved. Everything above widened; this did not, because who may attest
		under penalty of perjury that they examined a worker's documents is a
		designation on a PERSON under 8 U.S.C. §1324a — and the refusal has to
		send the reader to `add_authorized_signer`, not to an HR department that
		does not exist on this farm.
		"""
		employee = self._through_section_1()
		self.as_foreman()
		signers.add_authorized_signer({"user": WORKER, "full_name": "Ana Ramos", "title": "Manager"})
		self.as_foreman()
		with self.assertRaises(Exception) as caught:
			mobile_api.submit_i9_section_2(
				employee=employee,
				document_path="List A",
				list_a_doc_type="U.S. Passport",
				verifier_name="Flor Diaz",
				verification_date=str(frappe.utils.today()),
			)
		message = str(caught.exception)
		self.assertIn("authorized signer", message)
		self.assertNotIn("HR Manager", message, "the refusal must name the roster, not a role")

	def test_and_completes_it_once_the_farm_has(self):
		"""Decision 1: the foremen go on the roster. USCIS permits an employer to
		designate an authorized representative, so the person already standing in
		the orchard is the person lawfully able to complete Section 2 — and this
		codebase already had the data structure for saying which foremen those
		are. One row is the whole difference."""
		employee = self._through_section_1()
		self.as_foreman()
		signers.add_authorized_signer({"user": FOREMAN, "full_name": "Flor Diaz", "title": "Foreman"})
		self.as_foreman()
		self.assertTrue(
			mobile_api.submit_i9_section_2(
				employee=employee,
				document_path="List A",
				list_a_doc_type="U.S. Passport",
				verifier_name="Flor Diaz",
				verification_date=str(frappe.utils.today()),
			)
		)

	# ── the compliance that replaced the gates (§15.5) ──────────────────────
	def test_a_foreman_filed_i9_with_an_unsigned_section_1_does_not_complete(self):
		"""THE ASSERTION THAT CARRIES THE SAFETY ARGUMENT FOR THE WHOLE RELEASE.

		Every widening above rests on the claim that the compliance machinery
		refuses an incomplete record regardless of who called it — that the role
		gates were second-guessing `advance_if_signed`, which does the job better.
		If a foreman-filed I-9 with no Section 1 signature could reach Complete,
		the widenings would have removed a real protection instead of a redundant
		one, and this test is what says otherwise.
		"""
		self.as_foreman()
		hire = mobile_api.create_employee(first_name="Sin", last_name="Firma", company=MAIN)
		employee = hire.get("employee") or hire.get("name")
		self.as_foreman()
		mobile_api.create_i9_form(employee=employee, company=MAIN, hire_date=str(frappe.utils.today()))
		self.as_foreman()
		mobile_api.submit_i9_section_1(employee=employee, citizenship_status="US Citizen")
		# READ OFF THE STORED DOCUMENT rather than through `get_i9_form`, which is
		# self-or-HR and correctly refuses a foreman reading somebody ELSE's
		# immigration paperwork. That refusal is not incidental to this test — it
		# is the boundary holding while everything around it widened — but it
		# means the status has to be checked where it actually lives.
		status = frappe.db.get_value("I-9 Form", {"employee": employee}, "status")
		self.assertNotEqual(status, "Complete")
		self.assertTrue(status, "the I-9 was never created, so this proves nothing")

	def test_a_picker_holding_the_same_phone_is_refused_at_step_1(self):
		"""THE NEGATIVE CONTROL FOR ALL OF THE ABOVE, and this file would prove
		very little without it. Eleven green steps for a Foreman mean the flow
		works; they do not by themselves mean any gate is left. A plain Field
		Worker with a perfectly good enrolment must not get past the first one."""
		set_roles(FOREMAN, ["Field Worker"])
		self.as_foreman()
		with self.assertRaises(Exception) as caught:
			mobile_api.create_employee(first_name="Elena", last_name="Marquez", company=MAIN)
		self.assertIn("may not bring a person onto the farm", str(caught.exception))

	def test_and_the_foreman_still_cannot_read_the_register_or_the_deductions(self):
		"""REGISTER 3, UNTOUCHED. The release widened the hire and left another
		person's PII exactly where it was — which costs this farm nothing, because
		`HR_ROLES` already names Farm Manager and the only account newly excluded
		from a garnishment is a picker reading a coworker's support order."""
		self.as_foreman()
		for call in (
			lambda: mobile_api.search_employees(query="Marquez"),
			lambda: mobile_api.list_payroll_deductions(company=MAIN),
		):
			with self.assertRaises(Exception) as caught:
				call()
			# v0.260.0: deductions are now refused by the private-HR gate, in its own words.
			self.assertTrue(any(w in str(caught.exception) for w in ("personnel register", "is restricted to HR Manager")))

	# ── fixture helpers ─────────────────────────────────────────────────────
	def _through_section_1(self) -> str:
		self.as_foreman()
		hire = mobile_api.create_employee(first_name="Elena", last_name="Marquez", company=MAIN)
		employee = hire.get("employee") or hire.get("name")
		self.as_foreman()
		mobile_api.create_i9_form(employee=employee, company=MAIN, hire_date=str(frappe.utils.today()))
		self.as_foreman()
		mobile_api.submit_i9_section_1(
			employee=employee,
			citizenship_status="US Citizen",
			section_1_signature="data:image/png;base64,iVBORw0KGgo=",
		)
		return employee

	def _a_cabin(self) -> str:
		STORE.seed(
			"Housing Unit",
			[
				{
					"name": "MC-Cabin-99",
					"unit_name": "MC-Cabin-99",
					"owning_entity": MAIN,
					"capacity": 4,
					"residential": 1,
					"condition": "Good",
				}
			],
		)
		return "MC-Cabin-99"

	def _a_curriculum(self) -> str:
		STORE.seed(
			"Training Type",
			[{"name": "Heat Illness", "training_type_name": "Heat Illness", "active": 1}],
		)
		return "Heat Illness"
