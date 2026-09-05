# SPDX-License-Identifier: MIT
"""HR tools — and, first, that they are absent without the hrms app."""

from erpnext_mcp import registry

from .harness import frappe

from .fixtures import LEAVE_BALANCES, HRTestCase, V2TestCase
from .harness import STORE

HR_TOOL_NAMES = ("list_employees", "get_attendance_summary", "get_leave_balance")


class WithoutHRMS(V2TestCase):
	def test_the_tools_are_not_advertised(self):
		self.configure(**{f"allow_{name}": 1 for name in registry.TOOLS}, enabled=1)
		body, _ = self.call("tools/list")
		names = {tool["name"] for tool in body["result"]["tools"]}
		self.assertFalse(names & set(HR_TOOL_NAMES))

	def test_calling_one_explains_what_is_missing(self):
		for name in HR_TOOL_NAMES:
			with self.subTest(tool=name):
				message = self.tool_error(name, {})
				self.assertIn("hrms", message)

	def test_the_refusal_is_audited_as_blocked(self):
		self.tool_error("list_employees")
		row = self.assertAudited("list_employees", status="Blocked")
		self.assertIn("unavailable", row["result_summary"])


class ListEmployees(HRTestCase):
	def test_active_employees_by_default(self):
		data = self.tool_data("list_employees")
		self.assertEqual(data["count"], 2)
		self.assertNotIn("HR-EMP-00003", [row["name"] for row in data["employees"]])

	def test_status_can_be_widened(self):
		data = self.tool_data("list_employees", {"status": ""})
		self.assertEqual(data["count"], 3)

	def test_filters_by_department_and_designation(self):
		data = self.tool_data("list_employees", {"department": "Operations"})
		self.assertEqual(data["count"], 2)
		data = self.tool_data("list_employees", {"designation": "Supervisor"})
		self.assertEqual([row["name"] for row in data["employees"]], ["HR-EMP-00001"])

	def test_counts_by_department(self):
		data = self.tool_data("list_employees", {"status": ""})
		self.assertEqual(data["by_department"], {"Operations": 2, "Administration": 1})

	def test_explains_which_identifier_the_other_tools_want(self):
		data = self.tool_data("list_employees")
		self.assertIn("employee_number", data["note"])
		self.assertIn("HR-EMP", data["note"])


class AttendanceSummary(HRTestCase):
	def test_aggregates_per_employee(self):
		data = self.tool_data("get_attendance_summary", {"from_date": "2026-06-01", "to_date": "2026-06-30"})
		by_employee = {row["employee"]: row for row in data["employees"]}
		self.assertEqual(by_employee["HR-EMP-00001"]["counts"]["Present"], 3)
		self.assertEqual(by_employee["HR-EMP-00001"]["counts"]["On Leave"], 1)
		self.assertEqual(by_employee["HR-EMP-00002"]["counts"]["Absent"], 1)
		self.assertEqual(by_employee["HR-EMP-00002"]["counts"]["Half Day"], 1)

	def test_every_employee_gets_every_status_key(self):
		"""A missing key is ambiguous between zero and untracked."""
		data = self.tool_data("get_attendance_summary", {"from_date": "2026-06-01", "to_date": "2026-06-30"})
		for row in data["employees"]:
			with self.subTest(employee=row["employee"]):
				self.assertEqual(sorted(row["counts"]), data["statuses"])

	def test_drafts_are_not_counted(self):
		"""A draft Attendance row is not evidence anybody turned up."""
		data = self.tool_data("get_attendance_summary", {"from_date": "2026-06-01", "to_date": "2026-06-30"})
		by_employee = {row["employee"]: row for row in data["employees"]}
		self.assertEqual(by_employee["HR-EMP-00002"]["total_marked"], 4)
		self.assertEqual(data["records_counted"], 8)

	def test_totals_across_the_site(self):
		data = self.tool_data("get_attendance_summary", {"from_date": "2026-06-01", "to_date": "2026-06-30"})
		self.assertEqual(data["totals"]["Present"], 5)
		self.assertEqual(data["totals"]["On Leave"], 1)

	def test_scopes_to_one_employee_by_number(self):
		data = self.tool_data(
			"get_attendance_summary",
			{"from_date": "2026-06-01", "to_date": "2026-06-30", "employee": "E-101"},
		)
		self.assertEqual([row["employee"] for row in data["employees"]], ["HR-EMP-00002"])

	def test_scopes_to_a_department(self):
		data = self.tool_data(
			"get_attendance_summary",
			{
				"from_date": "2026-06-01",
				"to_date": "2026-06-30",
				"department": "Administration",
			},
		)
		self.assertEqual(data["employee_count"], 0)

	def test_a_narrow_range_excludes_records(self):
		data = self.tool_data("get_attendance_summary", {"from_date": "2026-06-01", "to_date": "2026-06-03"})
		self.assertEqual(data["records_counted"], 3)

	def test_an_inverted_range_is_refused(self):
		message = self.tool_error(
			"get_attendance_summary", {"from_date": "2026-06-30", "to_date": "2026-06-01"}
		)
		self.assertIn("is after", message)

	def test_an_unknown_employee_points_at_list_employees(self):
		message = self.tool_error(
			"get_attendance_summary",
			{"from_date": "2026-06-01", "to_date": "2026-06-30", "employee": "E-999"},
		)
		self.assertIn("list_employees", message)


class LeaveBalance(HRTestCase):
	def test_returns_a_balance_per_allocated_type(self):
		data = self.tool_data("get_leave_balance", {"employee": "HR-EMP-00001"})
		self.assertEqual(
			sorted(row["leave_type"] for row in data["balances"]),
			["Annual Leave", "Sick Leave"],
		)
		self.assertEqual(data["total_balance"], 20.5)

	def test_it_delegates_rather_than_subtracting_itself(self):
		"""Carry-forward and expiry live in HR's function; a subtraction here
		would be confidently wrong on any site with a policy."""
		data = self.tool_data("get_leave_balance", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["computed_via"], "HR get_leave_balance_on")

	def test_only_types_with_a_current_allocation_are_included(self):
		"""Unpaid Leave was allocated for 2025 only — a zero row for it is noise."""
		data = self.tool_data("get_leave_balance", {"employee": "HR-EMP-00001"})
		self.assertNotIn("Unpaid Leave", [row["leave_type"] for row in data["balances"]])

	def test_a_specific_leave_type_can_be_asked_for(self):
		data = self.tool_data("get_leave_balance", {"employee": "HR-EMP-00001", "leave_type": "Sick Leave"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["balances"][0]["balance"], 8.0)

	def test_an_unknown_leave_type_lists_the_allocated_ones(self):
		message = self.tool_error(
			"get_leave_balance", {"employee": "HR-EMP-00001", "leave_type": "Sabbatical"}
		)
		self.assertIn("Annual Leave", message)

	def test_defaults_as_of_to_today(self):
		data = self.tool_data("get_leave_balance", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["as_of"], "2026-07-24")

	def test_resolves_an_employee_by_user_id(self):
		data = self.tool_data("get_leave_balance", {"employee": "approver@example.test"})
		self.assertEqual(data["employee"], "HR-EMP-00001")

	def test_one_broken_leave_type_does_not_lose_the_others(self):
		LEAVE_BALANCES["Sick Leave"] = RuntimeError("misconfigured leave type")
		try:
			data = self.tool_data("get_leave_balance", {"employee": "HR-EMP-00001"})
		finally:
			LEAVE_BALANCES["Sick Leave"] = 8.0
		self.assertEqual([row["leave_type"] for row in data["balances"]], ["Annual Leave"])
		self.assertEqual(data["failed"][0]["leave_type"], "Sick Leave")

	def test_an_employee_with_no_allocations_returns_an_empty_list(self):
		data = self.tool_data("get_leave_balance", {"employee": "HR-EMP-00002"})
		self.assertEqual(data["count"], 0)
		self.assertEqual(data["total_balance"], 0)

	def test_it_refuses_rather_than_guessing_without_the_hr_api(self):
		import sys

		module_path = "hrms.hr.doctype.leave_application.leave_application"
		saved = sys.modules.pop(module_path)
		try:
			message = self.tool_error("get_leave_balance", {"employee": "HR-EMP-00001"})
		finally:
			sys.modules[module_path] = saved
		self.assertIn("does not export get_leave_balance_on", message)
		self.assertIn("run_report", message)


class HRIsAudited(HRTestCase):
	def test_reading_personal_data_leaves_a_row(self):
		self.tool_data("list_employees")
		self.assertAudited("list_employees", status="Success")

	def test_the_ambient_hrms_check_does_not_change_the_other_tools(self):
		"""Installing hrms must not disturb the accounting catalogue."""
		data = self.tool_data("get_company_topology")
		self.assertEqual(data["count"], 2)
		self.assertIn("hrms", STORE.installed_apps)


LEAVE_ON = {
	f"allow_{name}": 1
	for name in (
		"create_leave_request",
		"list_leave_requests",
		"approve_leave_request",
		"reject_leave_request",
		"get_leave_balance",
	)
}

WORKER = "HR-EMP-00001"


class LeaveRequestTestCase(HRTestCase):
	"""v0.157.0. The write behind a read that had shipped without one.

	`get_leave_balance` has told a worker how many days they have left since
	v0.18.1 and there was no way to ask for one of them — a number on a screen
	with no button under it.

	THE SCHEMA HERE WAS READ OFF THE DEPLOYED hrms 15.63.4 rather than guessed.
	`employee`, `leave_type`, `from_date`, `to_date`, `posting_date`, `status`,
	`company` and `naming_series` are all `reqd: 1` on that site, and `company`
	appears in no brief because the Desk form fills it in — which is exactly the
	kind of column a tool omits and only a bench notices.

	EVERY REFUSAL UNDER TEST IS THE TOOL'S OWN. The double does not run hrms's
	controller, so `validate_balance_leaves` and `validate_leave_overlap` never
	fire here; `tools/hr.py` makes both refusals itself, one layer earlier, where
	each can name the argument and the numbers. That is the only version of the
	check this suite can see, and it is the version a worker reads.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **LEAVE_ON)

	def file(self, **overrides):
		payload = {
			"employee": WORKER,
			"leave_type": "Sick Leave",
			"from_date": "2026-06-01",
			"to_date": "2026-06-02",
		}
		payload.update(overrides)
		return self.tool_data("create_leave_request", payload)

	def refuse(self, **overrides):
		payload = {
			"employee": WORKER,
			"leave_type": "Sick Leave",
			"from_date": "2026-06-01",
			"to_date": "2026-06-02",
		}
		payload.update(overrides)
		return self.tool_error("create_leave_request", payload)

	def applications(self):
		return STORE.rows("Leave Application")


class FilingALeaveRequest(LeaveRequestTestCase):
	def test_it_files_a_draft_carrying_the_dates_and_the_reason(self):
		data = self.file(reason="Hospital appointment in The Dalles")
		row = data["request"]
		self.assertEqual(row["employee"], WORKER)
		self.assertEqual(row["leave_type"], "Sick Leave")
		self.assertEqual(str(row["from_date"]), "2026-06-01")
		self.assertEqual(str(row["to_date"]), "2026-06-02")
		self.assertEqual(row["description"], "Hospital appointment in The Dalles")

	def test_it_comes_back_a_draft_and_says_nothing_has_moved(self):
		"""ASKING IS NOT APPROVING. A submitted Leave Application writes Leave
		Ledger Entries; approval is a separate tool with a separate switch."""
		data = self.file()
		self.assertEqual(data["request"]["docstatus"], 0)
		self.assertFalse(data["request"]["submitted"])
		self.assertEqual(data["request"]["status"], "Open")
		self.assertIn("Nothing has been deducted", data["note"])

	def test_the_company_comes_off_the_employee_and_is_not_an_argument(self):
		"""`company` is `reqd: 1` on hrms's doctype and appears in no brief,
		because the Desk form fills it in. A caller who could name a different one
		would file a worker's absence against an entity they do not work for."""
		self.assertEqual(self.file()["request"]["company"], "Example Trading Co")
		self.assertNotIn("company", registry.TOOLS["create_leave_request"]["inputSchema"]["properties"])

	def test_the_days_are_counted_by_hr_rather_than_here(self):
		"""Holiday lists and half days are the whole difficulty; a subtraction
		written in this app would be confidently wrong on any site with a holiday
		list. Same argument `get_leave_balance` makes about not netting itself."""
		data = self.file()
		self.assertEqual(data["days_counted_via"], "HR get_number_of_leave_days")
		self.assertEqual(data["request"]["total_leave_days"], 2.0)

	def test_a_half_day_costs_half_a_day(self):
		data = self.file(from_date="2026-06-01", to_date="2026-06-01", half_day=True)
		self.assertTrue(data["request"]["half_day"])
		self.assertEqual(data["request"]["total_leave_days"], 0.5)
		self.assertEqual(str(data["request"]["half_day_date"]), "2026-06-01")

	def test_the_balance_before_the_request_is_recorded(self):
		data = self.file()
		self.assertEqual(data["balance_before"], LEAVE_BALANCES["Sick Leave"])
		self.assertTrue(data["balance_checked"])


class WhatFilingRefuses(LeaveRequestTestCase):
	def test_a_request_longer_than_the_balance_names_both_numbers(self):
		"""hrms throws "Insufficient leave balance", which says neither what the
		balance was nor what was asked for, so it names nothing to change."""
		message = self.refuse(from_date="2026-06-01", to_date="2026-06-30")
		self.assertIn("8.0 day(s)", message)
		self.assertIn("30.0", message)
		self.assertEqual(self.applications(), [])

	def test_dates_the_wrong_way_round_are_refused(self):
		message = self.refuse(from_date="2026-06-10", to_date="2026-06-01")
		self.assertIn("after", message)
		self.assertEqual(self.applications(), [])

	def test_an_overlapping_request_names_the_one_already_there(self):
		"""Two rows over one absence deduct twice the moment both are approved."""
		first = self.file()["leave_application"]
		message = self.refuse(from_date="2026-06-02", to_date="2026-06-03")
		self.assertIn(first, message)
		self.assertIn("deduct twice", message)
		self.assertEqual(len(self.applications()), 1)

	def test_a_rejected_application_does_not_block_a_new_one(self):
		"""A refusal is an answer, not a booking. Re-filing the same days after
		one is refused is the ordinary next thing somebody does."""
		first = self.file()["leave_application"]
		self.tool_data(
			"reject_leave_request",
			{"leave_application": first, "reason": "cherry harvest that week"},
		)
		self.assertTrue(self.file()["leave_application"])

	def test_a_leave_type_nobody_allocated_says_so_rather_than_reporting_zero(self):
		"""THE ORCHARD MEADOW CASE. That site has five Leave Types and ZERO Leave
		Allocations, so a balance check alone would tell every worker they had
		spent an entitlement they were never given. Those are different facts and
		only one of them is fixed by a Leave Allocation."""
		message = self.refuse(leave_type="Unpaid Leave", from_date="2026-06-01", to_date="2026-06-01")
		self.assertIn("nobody has allocated", message.lower())
		self.assertIn("is_lwp", message)
		self.assertEqual(self.applications(), [])

	def test_an_unpaid_type_needs_no_allocation_at_all(self):
		"""hrms's own rule, and the escape hatch for a farm that has not set up
		Leave Allocations — which is the state Orchard Meadow is in."""
		data = self.file(leave_type="Leave Without Pay", from_date="2026-06-01", to_date="2026-06-03")
		self.assertEqual(data["request"]["leave_type"], "Leave Without Pay")
		self.assertIsNone(data["balance_before"])
		self.assertFalse(data["balance_checked"])
		self.assertIn("unpaid type", data["note"])

	def test_an_unknown_leave_type_lists_what_the_site_has(self):
		message = self.refuse(leave_type="Sabbatical")
		self.assertIn("Sick Leave", message)
		self.assertEqual(self.applications(), [])

	def test_omitting_the_type_with_several_allocations_is_refused_with_the_list(self):
		"""Choosing between them on somebody's behalf is inventing a decision —
		the same three-answer shape `asset_mirror._category` settled on."""
		message = self.tool_error(
			"create_leave_request",
			{"employee": WORKER, "from_date": "2026-06-01", "to_date": "2026-06-02"},
		)
		self.assertIn("Annual Leave", message)
		self.assertIn("Sick Leave", message)

	def test_omitting_the_type_with_exactly_one_allocation_settles_it(self):
		STORE.tables["Leave Allocation"] = {
			name: row
			for name, row in STORE.tables["Leave Allocation"].items()
			if row.get("leave_type") == "Sick Leave"
		}
		data = self.tool_data(
			"create_leave_request",
			{"employee": WORKER, "from_date": "2026-06-01", "to_date": "2026-06-02"},
		)
		self.assertEqual(data["request"]["leave_type"], "Sick Leave")

	def test_a_half_day_over_a_span_has_to_say_which_day(self):
		message = self.refuse(from_date="2026-06-01", to_date="2026-06-05", half_day=True)
		self.assertIn("half_day_date", message)
		self.assertEqual(self.applications(), [])

	def test_a_half_day_date_outside_the_span_is_refused(self):
		message = self.refuse(half_day=True, half_day_date="2026-07-04")
		self.assertIn("outside", message)
		self.assertEqual(self.applications(), [])

	def test_an_unknown_employee_is_refused_by_name(self):
		message = self.refuse(employee="Nobody At All")
		self.assertIn("Nobody At All", message)

	def test_it_is_off_until_an_operator_switches_it_on(self):
		self.configure(enabled=1, allow_create_leave_request=0)
		self.assertIn("allow_create_leave_request", self.refuse())


class ReadingTheQueue(LeaveRequestTestCase):
	def test_it_lists_what_was_filed_and_counts_the_queue(self):
		self.file()
		self.file(leave_type="Annual Leave", from_date="2026-07-01", to_date="2026-07-02")
		data = self.tool_data("list_leave_requests", {})
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["pending_count"], 2)
		self.assertEqual(data["by_status"], {"Open": 2})
		self.assertEqual(data["total_days"], 4.0)

	def test_an_answered_request_is_still_listed(self):
		"""DEFAULTS TO EVERY STATUS. A filter that has to be turned off to see an
		approved day hides the answer to 'did that get approved'."""
		name = self.file()["leave_application"]
		self.tool_data("approve_leave_request", {"leave_application": name})
		data = self.tool_data("list_leave_requests", {})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["by_status"], {"Approved": 1})
		self.assertEqual(data["pending_count"], 0)

	def test_it_narrows_to_one_status(self):
		name = self.file()["leave_application"]
		self.file(leave_type="Annual Leave", from_date="2026-07-01", to_date="2026-07-02")
		self.tool_data("approve_leave_request", {"leave_application": name})
		self.assertEqual(self.tool_data("list_leave_requests", {"status": "Open"})["count"], 1)

	def test_the_window_asks_which_absences_touch_it(self):
		"""Not which were FILED in it. An application that started last week and
		runs into this one is the answer to 'who is off on Tuesday'."""
		# Annual Leave, because ten days is more Sick Leave than this fixture
		# allocates and the balance check refuses it — correctly.
		self.file(leave_type="Annual Leave", from_date="2026-06-01", to_date="2026-06-10")
		found = self.tool_data(
			"list_leave_requests", {"from_date": "2026-06-08", "to_date": "2026-06-09"}
		)
		self.assertEqual(found["count"], 1)

	def test_a_window_that_misses_it_returns_nothing(self):
		self.file(from_date="2026-06-01", to_date="2026-06-02")
		self.assertEqual(
			self.tool_data("list_leave_requests", {"from_date": "2026-08-01"})["count"], 0
		)

	def test_an_unknown_status_is_refused_with_the_real_ones(self):
		message = self.tool_error("list_leave_requests", {"status": "Pending"})
		self.assertIn("Approved", message)

	def test_the_read_is_on_by_default(self):
		"""Reads ship on and writes ship off — the rule the whole switch table
		follows."""
		import json

		spec = json.load(
			open("erpnext_mcp/erpnext_mcp/doctype/erpnext_mcp_settings/erpnext_mcp_settings.json")
		)
		defaults = {f["fieldname"]: f.get("default") for f in spec["fields"]}
		self.assertEqual(defaults["allow_list_leave_requests"], "1")
		for name in ("create", "approve", "reject"):
			with self.subTest(tool=name):
				self.assertEqual(defaults[f"allow_{name}_leave_request"], "0")


class AnsweringARequest(LeaveRequestTestCase):
	def test_approving_submits_it(self):
		"""THE CALL THAT SPENDS THE ENTITLEMENT. hrms writes the Leave Ledger
		Entry on submit, which is why this is a separate tool and a separate
		switch from filing."""
		name = self.file()["leave_application"]
		data = self.tool_data("approve_leave_request", {"leave_application": name})
		self.assertEqual(data["status"], "Approved")
		self.assertEqual(data["request"]["docstatus"], 1)
		self.assertTrue(data["request"]["submitted"])
		self.assertIn("balance has moved", data["note"])

	def test_rejecting_submits_it_too_and_draws_nothing_down(self):
		"""Leaving an answered request as a draft leaves it looking unanswered."""
		name = self.file()["leave_application"]
		data = self.tool_data(
			"reject_leave_request",
			{"leave_application": name, "reason": "two of the crew are already off that week"},
		)
		self.assertEqual(data["status"], "Rejected")
		self.assertEqual(data["request"]["docstatus"], 1)
		self.assertIn("draws down no balance", data["note"])

	def test_the_refusal_reason_is_written_onto_the_application(self):
		"""So the worker reads the answer on the record rather than hearing it
		second-hand."""
		name = self.file(reason="Hospital appointment")["leave_application"]
		self.tool_data(
			"reject_leave_request",
			{"leave_application": name, "reason": "cherry harvest that week"},
		)
		description = frappe.db.get_value("Leave Application", name, "description")
		self.assertIn("Hospital appointment", description)
		self.assertIn("cherry harvest that week", description)

	def test_rejecting_without_a_reason_is_refused(self):
		"""An approval explains itself and a refusal does not. The asymmetry is
		the point."""
		name = self.file()["leave_application"]
		self.assertIn("reason", self.tool_error("reject_leave_request", {"leave_application": name}))
		self.assertIn(
			"real explanation",
			self.tool_error("reject_leave_request", {"leave_application": name, "reason": "no"}),
		)

	def test_approving_needs_no_reason(self):
		name = self.file()["leave_application"]
		self.assertEqual(
			self.tool_data("approve_leave_request", {"leave_application": name})["status"],
			"Approved",
		)

	def test_an_already_answered_request_is_not_re_answered(self):
		"""Changing an answer that has moved a balance is a cancellation and an
		amendment, and both are Desk work."""
		name = self.file()["leave_application"]
		self.tool_data("approve_leave_request", {"leave_application": name})
		message = self.tool_error(
			"reject_leave_request", {"leave_application": name, "reason": "changed my mind"}
		)
		self.assertIn("already answered", message)
		self.assertEqual(frappe.db.get_value("Leave Application", name, "status"), "Approved")

	def test_an_unknown_application_is_refused_by_name(self):
		message = self.tool_error("approve_leave_request", {"leave_application": "HR-LAP-NOPE"})
		self.assertIn("HR-LAP-NOPE", message)

	def test_both_are_off_until_an_operator_switches_them_on(self):
		name = self.file()["leave_application"]
		self.configure(enabled=1, allow_create_leave_request=1, allow_approve_leave_request=0)
		self.assertIn(
			"allow_approve_leave_request",
			self.tool_error("approve_leave_request", {"leave_application": name}),
		)
