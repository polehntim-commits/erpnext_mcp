"""Least privilege on the phone — the hotfix. v0.260.0 (docs/design/role_data_access_audit.md §3 items 1–6;
Tim, 2026-10-06: HR reads are HR's; workers see their own receipts)."""

from unittest import mock

import frappe

from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import universal_scan as scan_tool

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase

OTHER_EMP = "EMP-BEA"


class HotfixCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Employee", [{"name": OTHER_EMP, "employee_name": "Bea Other", "company": MAIN, "status": "Active"}])

	def as_roles(self, *roles):
		set_roles(WORKER, ["Field Worker", *roles])
		self.be()


class HRReadsAreHRs(HotfixCase):
	def test_a_farm_manager_alone_is_refused_the_private_personnel_reads(self):
		self.as_roles("Farm Manager")
		for call in (lambda: mobile_api.get_i9_form(employee=OTHER_EMP),
		             lambda: mobile_api.list_discipline_history(employee=OTHER_EMP),
		             lambda: mobile_api.get_payroll_register(),
		             lambda: mobile_api.list_payroll_deductions()):
			with self.assertRaisesRegex(frappe.PermissionError, "is restricted to HR Manager, HR User, System Manager"):
				call()

	def test_a_farm_manager_reads_a_coworkers_record_without_the_personal_fields(self):
		STORE.seed("Employee", [{"name": "EMP-CAL", "employee_name": "Cal", "company": MAIN, "status": "Active",
		                         "date_of_birth": "1990-01-01", "cell_number": "5415550100", "gender": "Male"}])
		self.as_roles("Farm Manager")
		detail = mobile_api.get_employee(employee="EMP-CAL")
		self.assertEqual(detail["employee_name"], "Cal")
		self.assertIsNone(detail["date_of_birth"])
		self.assertIsNone(detail["cell_number"])
		self.assertIn("date_of_birth", detail["withheld"])
		self.as_roles("HR Manager")
		self.assertEqual(str(mobile_api.get_employee(employee="EMP-CAL")["date_of_birth"])[:10], "1990-01-01")

	def test_hr_passes_the_gate(self):
		self.as_roles("HR Manager")
		try:
			mobile_api.get_payroll_register()
		except frappe.PermissionError as exc:  # pragma: no cover - the failure this test exists for
			self.fail(f"HR Manager refused: {exc}")
		except Exception:
			pass  # whatever the payroll tool says about an empty register is not this gate

	def test_your_own_record_still_needs_no_hr_role(self):
		self.as_roles()
		try:
			mobile_api.get_employee(employee=WORKER_EMPLOYEE)
		except frappe.PermissionError as exc:
			self.fail(f"own record refused: {exc}")

	def test_office_hr_may_enrol_and_the_owner_profile_carries_hr(self):
		from erpnext_mcp import sidebar

		self.assertTrue({"HR Manager", "HR User"} <= set(guard.FARM_OPS_ROLES))
		self.assertIn("HR Manager", sidebar.PROFILES["Owner"])
		self.assertNotIn("Farm Manager", guard.PRIVATE_HR_ROLES)


class AccidentReports(HotfixCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Accident Report", [
			{"name": "ACC-0001", "company": MAIN, "injured_person": OTHER_EMP, "injured_person_name": "Bea Other"},
			{"name": "ACC-0002", "company": MAIN, "injured_person": WORKER_EMPLOYEE, "injured_person_name": "Ana"}])

	def test_a_worker_cannot_read_someone_elses_but_can_read_their_own(self):
		self.as_roles()
		with self.assertRaisesRegex(frappe.PermissionError, "Reading an accident report"):
			mobile_api.get_accident_report(report="ACC-0001")
		try:
			mobile_api.get_accident_report(report="ACC-0002")
		except frappe.PermissionError as exc:
			self.fail(f"own report refused: {exc}")
		except Exception:
			pass  # the report tool's own shape needs; the gate is what is under test


class ComplianceAlerts(HotfixCase):
	def test_a_field_worker_neither_reads_nor_dismisses_the_calendar(self):
		self.as_roles()
		with self.assertRaisesRegex(frappe.PermissionError, "compliance calendar"):
			mobile_api.list_compliance_alerts()
		with self.assertRaisesRegex(frappe.PermissionError, "Dismissing a compliance alert"):
			mobile_api.dismiss_compliance_alert(alert="ALERT-1", reason="not mine")

	def test_a_foreman_reads_it(self):
		self.as_roles("Foreman")
		self.assertIn("alerts", mobile_api.list_compliance_alerts() | {"alerts": []})


class ReceiptsAreYourOwn(HotfixCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Expense Receipt", [
			{"name": "EXR-MINE", "company": MAIN, "submitted_by": WORKER_EMPLOYEE, "merchant": "Fuel", "amount": 20.0,
			 "status": "Submitted", "receipt_date": "2026-10-01"},
			{"name": "EXR-THEIRS", "company": MAIN, "submitted_by": OTHER_EMP, "merchant": "Hardware", "amount": 80.0,
			 "status": "Submitted", "receipt_date": "2026-10-01"}])

	def test_a_worker_lists_and_opens_only_their_own(self):
		self.as_roles()
		self.assertEqual([r["name"] for r in mobile_api.list_expense_receipts()["receipts"]], ["EXR-MINE"])
		with self.assertRaisesRegex(frappe.DoesNotExistError, "not found"):
			mobile_api.get_expense_receipt(receipt="EXR-THEIRS")
		with self.assertRaisesRegex(frappe.DoesNotExistError, "not found"):
			mobile_api.update_expense_receipt(receipt_name="EXR-THEIRS", notes="mine now")
		self.assertEqual(mobile_api.get_expense_receipt(receipt="EXR-MINE")["name"], "EXR-MINE")

	def test_a_reviewer_sees_everyones(self):
		self.as_roles("Farm Manager")
		self.assertEqual(sorted(r["name"] for r in mobile_api.list_expense_receipts()["receipts"]),
		                 ["EXR-MINE", "EXR-THEIRS"])


class HousingScan(HotfixCase):
	ANSWER = {"entity_type": scan_tool.HOUSING, "entity": {"name": "CABIN-1", "capacity": 4, "currently_assigned": 2,
	          "current_assignments": [{"employee_name": "Bea Other", "deposit_paid": 100}],
	          "assignment_history": [{"employee_name": "Old Tenant"}]},
	          "pending_tasks": [], "overdue_tasks": [], "due_compliance": []}

	def scan(self):
		with mock.patch.object(scan_tool, "universal_scan",
		                       return_value=mock.Mock(data={**self.ANSWER, "entity": dict(self.ANSWER["entity"])})):
			return mobile_api.universal_scan(content="CABIN-1")

	def test_a_worker_sees_the_unit_not_who_lives_there(self):
		self.as_roles()
		entity = self.scan()["entity"]
		self.assertEqual((entity["capacity"], entity["currently_assigned"]), (4, 2))
		self.assertNotIn("current_assignments", entity)
		self.assertNotIn("assignment_history", entity)

	def test_housing_staff_see_the_occupants(self):
		self.as_roles("Farm Manager")
		self.assertIn("current_assignments", self.scan()["entity"])


class SearchLinkIsNotAnOracle(HotfixCase):
	def test_a_date_or_free_text_column_cannot_be_probed(self):
		self.as_roles()
		with mock.patch("erpnext_mcp.form_schema.phone_link_doctypes", return_value=["Employee"]):
			with self.assertRaisesRegex(frappe.PermissionError, "not allowed here"):
				mobile_api.search_link(doctype="Employee", filters={"date_of_birth": "1990-01-01"})
			with self.assertRaisesRegex(frappe.PermissionError, "not allowed here"):
				mobile_api.search_link(doctype="Employee", filters={"cell_number": "5415550100"})
			mobile_api.search_link(doctype="Employee", filters={"status": "Active"})
