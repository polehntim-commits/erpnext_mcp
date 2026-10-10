# SPDX-License-Identifier: MIT
"""Sick time, deduction authorizations and separations from the phone. v0.306.0 (app 0.62.0).

* `list_leave_types` lists Oregon sick time and Washington paid sick leave in hours, with the balance and whether it
  is usable yet — for the states the worker has sick time in or works in.
* The phone's `create_leave_request` carries `hours`, so a worker files sick hours from their own phone.
* `list_employee_deductions awaiting_authorization` lists the voluntary deductions a run would skip for want of the
  worker's signature (ORS 652.610(3)); the pad signs them through `submit_form_signature`.
* `record_separation`: last day, reason, seasonal farmworker — and when final pay is due (ORS 652.140 / 652.145).
"""

from erpnext_mcp import final_pay
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import hr as hr_tools

from .fixtures import MAIN, V12TestCase
from .harness import STORE, frappe, set_roles
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase


class SickTimeIsALeaveType(V12TestCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Employee", [{"name": "EMP-1", "employee_name": "Beto", "company": MAIN, "status": "Active",
		                         "date_of_joining": "2024-01-01"}])
		STORE.seed("Farm Sick Time Ledger", [{"name": "L1", "employee": "EMP-1", "company": MAIN,
		                                      "posting_date": "2026-03-01", "kind": "Accrual", "hours": 6}])
		STORE.commit()

	def test_the_worker_sees_the_state_they_have_time_in(self):
		rows = hr_tools.list_leave_types({"employee": "EMP-1", "as_of": "2026-06-01"}).data["leave_types"]
		sick = [r for r in rows if r.get("is_sick_time")]
		self.assertEqual([(r["leave_type"], r["balance"], r["requestable"]) for r in sick],
		                 [("Oregon Sick Time", 6.0, True)])
		self.assertEqual(sick[0]["unit"], "hours")

	def test_without_a_worker_both_are_offered(self):
		rows = hr_tools.list_leave_types({}).data["leave_types"]
		self.assertEqual([r["work_state"] for r in rows if r.get("is_sick_time")], ["OR", "WA"])


class FromThePhone(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		final_pay.install_fields()
		frappe.db.set_value("Employee", WORKER_EMPLOYEE, "date_of_joining", "2024-01-01")
		STORE.seed("Farm Sick Time Ledger", [{"name": "L1", "employee": WORKER_EMPLOYEE, "company": MAIN,
		                                      "posting_date": "2026-03-01", "kind": "Accrual", "hours": 6,
		                                      "work_state": "WA"}])
		STORE.commit()

	def test_a_worker_files_their_own_sick_hours(self):
		self.configure(allow_create_leave_request=1, allow_list_leave_types=1)
		self.be()
		types = mobile_api.list_leave_types()["leave_types"]
		self.assertIn("Washington Paid Sick Leave", [r["leave_type"] for r in types if r.get("is_sick_time")])
		filed = mobile_api.create_leave_request(leave_type="Washington Paid Sick Leave", from_date="2026-06-02",
		                                        hours="3")
		self.assertEqual(list(frappe.db.get_value("Farm Pay Item", filed["pay_item"], ["kind", "hours", "work_state"])),
		                 ["Sick Time", 3.0, "WA"])

	def test_a_separation_needs_hr_and_answers_the_final_pay_deadline(self):
		self.configure(allow_update_employee=1)
		self.be()
		with self.assertRaises(Exception):
			mobile_api.record_separation(employee=WORKER_EMPLOYEE, relieving_date="2026-10-09",
			                             separation_reason="End of season", seasonal_farmworker="1")
		set_roles(WORKER, ["Field Worker", "HR Manager"])
		self.be()
		got = mobile_api.record_separation(employee=WORKER_EMPLOYEE, relieving_date="2026-10-09",
		                                   separation_reason="End of season", seasonal_farmworker="1")
		self.assertEqual(got["final_pay"]["due_date"], "2026-10-09")
		self.assertIn("652.145", got["final_pay"]["rule"])
		row = frappe.db.get_value("Employee", WORKER_EMPLOYEE, ["relieving_date", "separation_reason",
		                                                        "seasonal_farmworker"])
		self.assertEqual((str(row[0]), row[1], int(row[2])), ("2026-10-09", "End of season", 1))

	def test_an_unknown_reason_is_refused(self):
		self.configure(allow_update_employee=1)
		set_roles(WORKER, ["Field Worker", "HR Manager"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.record_separation(employee=WORKER_EMPLOYEE, relieving_date="2026-10-09",
			                             separation_reason="Laid off")
		self.assertIn("End of season", str(caught.exception))


class DeductionsAwaitingAuthorization(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_list_employee_deductions=1)
		STORE.seed("Employee", [{"name": "EMP-1", "employee_name": "Beto", "company": MAIN, "status": "Active"}])
		STORE.seed("Farm Payroll Deduction", [
			{"name": "DED-1", "employee": "EMP-1", "company": MAIN, "deduction_type": "Voluntary",
			 "deduction_category": "Other Voluntary", "status": "Active", "amount_type": "Fixed", "amount": 25,
			 "label": "Housing", "effective_from": "2026-01-01"},
			{"name": "DED-2", "employee": "EMP-1", "company": MAIN, "deduction_type": "Voluntary",
			 "deduction_category": "Other Voluntary", "status": "Active", "amount_type": "Fixed", "amount": 10,
			 "label": "Boots", "effective_from": "2026-01-01", "authorized_on": "2026-01-02"}])
		STORE.commit()

	def test_only_the_unsigned_one_is_listed_and_it_says_why(self):
		data = self.tool_data("list_employee_deductions", {"employee": "EMP-1", "awaiting_authorization": True})
		self.assertEqual([d["name"] for d in data["deductions"]], ["DED-1"])
		self.assertIn("652.610(3)", data["deductions"][0]["authorization_missing"])
		every = self.tool_data("list_employee_deductions", {"employee": "EMP-1"})
		self.assertEqual(len(every["deductions"]), 2)
