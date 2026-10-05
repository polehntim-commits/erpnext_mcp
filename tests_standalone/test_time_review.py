"""Punch review as a compliance item. v0.251.0 (AFB-2026-00032)."""

from unittest import mock

import frappe

from erpnext_mcp import audit_packets, punch_times, shifts, time_review
from erpnext_mcp.alerts import base
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER
from .test_punch_times import stamp
from .test_work_actions import CAL, DEE, WorkActionsCase


def live(alert_type=None, docname=None):
	return [a for a in STORE.rows("Compliance Alert") if not int(a.get("dismissed") or 0)
	        and (alert_type is None or a.get("alert_type") == alert_type)
	        and (docname is None or a.get("source_docname") == docname)]


class PunchCase(WorkActionsCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net",
		               **{"allow_create_mobile_user": 1, "allow_list_time_reviews": 1, "allow_review_punches": 1})
		time_review.seed()

	def a_shift(self, tapped_hours=1.0, people=(CAL,)):
		self.foreman()
		shift = mobile_api.clock_in_crew(employees=list(people), location="Block 7 North", tapped_at=stamp(tapped_hours))["shift"]
		frappe.local.session.user = "Administrator"
		return shift

	def row(self, shift, person=CAL):
		return next(r for r in frappe.get_doc("Farm Shift", shift).as_dict()["crew"] if r["employee"] == person)

	def flags(self, shift, person=CAL):
		return dict((r["employee"], f) for r, f in time_review.judge_shift(shift))[person]

	def close(self, shift, out_hours=0.5):
		STORE.get_raw("Farm Shift", shift).update(end_datetime=stamp(out_hours), status="Closed")


class Flags(PunchCase):
	def test_a_late_offline_punch_and_a_missing_clock_out_are_flagged(self):
		shift = self.a_shift(tapped_hours=20)
		self.assertEqual(self.flags(shift), ["runaway", "offline"], "20 hours open is a runaway")
		self.close(shift)
		self.assertEqual(self.flags(shift), ["missing_clock_out", "offline"])

	def test_gps_outside_the_block_and_short_breaks(self):
		shift = self.a_shift()
		STORE.get_raw("Farm Shift", shift)["farm_location_gps"] = "45.6000,-121.2000"
		STORE.seed("Shift Location Log", [{"name": "SLL-1", "shift": shift, "employee": CAL, "latitude": 45.62,
		                                    "longitude": -121.2, "accuracy_meters": 10}])
		self.assertIn("outside_block", self.flags(shift))
		STORE.get_raw("Farm Shift", shift)["break_policy"] = "OR-AG"
		with mock.patch.object(shifts, "_break_summary", return_value={"workers_short": [{"employee": CAL}]}):
			self.assertIn("breaks_short", self.flags(shift))


class AsCompliance(PunchCase):
	def test_an_unreviewed_flag_raises_an_alert_for_the_company_and_approval_clears_it(self):
		shift = self.a_shift(tapped_hours=15)
		self.foreman()
		mobile_api.clock_out_worker(shift=shift, employee=CAL, left_at=stamp(14.5))  # both sent late
		frappe.local.session.user = "Administrator"
		self.assertEqual(self.flags(shift), ["offline"])
		base.refresh_compliance_alerts(company=MAIN, alert_types=["punch_offline_window"])
		alerts = live("punch_offline_window")
		self.assertEqual([(a["source_doctype"], a["source_docname"], a["company"]) for a in alerts], [("Farm Shift", shift, MAIN)])
		self.assertIn("sent late", alerts[0]["alert_message"])
		row = self.row(shift)["name"]
		refused = time_review.review("Administrator", "approve", [row])
		self.assertIn("approve with a reason", refused["refused"][0]["why"])
		done = time_review.review("Administrator", "approve", [row], "Phone was in the truck all day; times match the crew sheet")
		self.assertEqual(len(done["done"]), 1)
		self.assertEqual(live("punch_offline_window"), [], "the approval cleared it")

	def test_overtime_approaching_and_exceeded_per_person(self):
		hours = {"week": 37.0}
		with mock.patch.object(shifts, "hours_worked_by", side_effect=lambda *a, **k: hours):
			base.refresh_compliance_alerts(company=MAIN, alert_types=["overtime_week_approaching", "overtime_week_exceeded"])
			kinds = {a["alert_type"] for a in live(docname=CAL)}
			self.assertEqual(kinds, {"overtime_week_approaching"})
			hours["week"] = 41.5
			base.refresh_compliance_alerts(company=MAIN, alert_types=["overtime_week_approaching", "overtime_week_exceeded"])
			kinds = {a["alert_type"] for a in live(docname=CAL)}
			self.assertEqual(kinds, {"overtime_week_exceeded"})


class ReviewAndLock(PunchCase):
	def test_a_missing_clock_out_is_fixed_with_a_reason_and_then_locked(self):
		shift = self.a_shift(tapped_hours=9)
		self.close(shift)
		row = self.row(shift)["name"]
		self.assertIn("fix it", time_review.review("Administrator", "approve", [row], "ok")["refused"][0]["why"])
		with self.assertRaisesRegex(ToolError, "needs a reason"):
			time_review.review("Administrator", "fix", [row], "", {row: {"out": stamp(1)}})
		done = time_review.review("Administrator", "fix", [row], "Left at 4 per the crew sheet", {row: {"out": stamp(1)}})
		self.assertEqual(done["done"][0]["row"], row)
		saved = self.row(shift)
		self.assertEqual((saved["time_review_status"], str(saved["left_at"])[:16]), ("Fixed", stamp(1)[:16]))
		with self.assertRaisesRegex(ToolError, "locked for payroll"):
			punch_times.resolve("Administrator", row, "Phone time stands")
		doc = frappe.get_doc("Farm Shift", shift)
		next(r for r in doc.crew if r.get("name") == row).set("left_at", stamp(0.8))
		with self.assertRaisesRegex(frappe.ValidationError, "locked for payroll"):
			doc.save()

	def test_only_a_manager_reopens_with_a_reason(self):
		shift = self.a_shift()
		row = self.row(shift)["name"]
		time_review.review("Administrator", "approve", [row])
		set_roles("crewboss@example.com", ["Foreman"])
		with self.assertRaisesRegex(ToolError, "restricted to"):
			time_review.review("crewboss@example.com", "reopen", [row], "wrong day")
		time_review.review("Administrator", "reopen", [row], "Wrong day keyed")
		self.assertFalse(self.row(shift)["time_review_status"])

	def test_a_whole_period_is_approved_in_one_go_over_mcp(self):
		self.a_shift(people=(CAL, DEE))
		today = str(frappe.utils.today())[:10]
		data = self.tool_data("review_punches", {"action": "approve", "from": today, "to": today})
		self.assertEqual(len(data["done"]), 2)
		self.assertEqual(self.tool_data("list_time_reviews", {"from": today, "to": today})["count"], 0)


class PayrollAndAudit(PunchCase):
	def test_payroll_warns_on_unreviewed_punches_and_the_packet_shows_each_review(self):
		shift = self.a_shift(people=(CAL, DEE))
		today = str(frappe.utils.today())[:10]
		warning = time_review.payroll_warning(MAIN, today, today)
		self.assertEqual(warning["punches_not_reviewed"], 2)
		time_review.review("Administrator", "approve", [self.row(shift, CAL)["name"]])
		self.assertEqual(time_review.payroll_warning(MAIN, today, today)["punches_not_reviewed"], 1)
		section = audit_packets._BUILDERS["time_records"](audit_packets.TYPES["DOL"], MAIN, today, today)
		statuses = sorted(r["status"] for r in section["rows"])
		self.assertEqual(statuses, ["Approved", "Not reviewed"])
		self.assertIn("time_records", audit_packets.TYPES["DOL"].sections)
