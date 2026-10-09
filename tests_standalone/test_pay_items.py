# SPDX-License-Identifier: MIT
"""Pay items are never stuck Pending. v0.293.0.

1. A worker with no salary structure and no hours in the period is paid the item on its own slip; one who WORKED
   without a structure waits with the hours, and the run says why.
2. A daily-pay worker with no daily slip in the week still gets a settlement for an item dated in it.
3. Cancelling a run puts its items back to Pending, and the next run pays them.
"""

from erpnext_mcp import pay_items

from .fixtures import MAIN
from .harness import STORE, frappe
from .test_daily_run import DailyRunCase
from .test_payroll_integration import (
	DRIVER,
	PERIOD_END,
	PERIOD_START,
	WORKER,
	IntegrationTestCase,
	member,
	shift,
)


def item(name, employee, amount=800.0, pay_on="2025-06-10", **extra):
	return {"name": name, "employee": employee, "company": MAIN, "kind": "Bonus", "amount": amount, "pay_on": pay_on,
	        "status": "Pending", "treatment": "Wages", **extra}


class RunCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.structure(WORKER, rate=20.0)
		self.seed_shifts(shift("P1", "2025-06-03", crew=[member(WORKER)]))
		STORE.commit()

	def pay(self):
		got = self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": PERIOD_START,
		                                                "pay_period_end": PERIOD_END})
		STORE.commit()
		return got


class NoStructure(RunCase):
	def test_no_hours_no_structure_is_paid_on_its_own_slip(self):
		STORE.seed("Farm Pay Item", [item("FPI-D1", DRIVER, work_state="OR")])
		got = self.pay()
		slip = next(s for s in got["slips"] if s["employee"] == DRIVER)
		self.assertEqual(slip["gross_pay"], 800.0)
		self.assertEqual(slip["work_state"], "OR")
		self.assertGreater(slip["social_security"], 0, "a bonus is wages")
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-D1", "status"), "Paid")
		self.assertEqual(got["pay_items_waiting"], [])

	def test_hours_without_a_structure_wait_together(self):
		self.seed_shifts(shift("P2", "2025-06-04", crew=[member(DRIVER)]))
		STORE.seed("Farm Pay Item", [item("FPI-D2", DRIVER)])
		STORE.commit()
		got = self.pay()
		self.assertNotIn(DRIVER, [s["employee"] for s in got["slips"]])
		self.assertEqual(got["pay_items_waiting"][0]["item"], "FPI-D2")
		self.assertIn("create_salary_structure", got["pay_items_waiting"][0]["reason"])
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-D2", "status"), "Pending")


class CancelledRun(RunCase):
	def test_cancelling_a_run_releases_its_items(self):
		STORE.seed("Farm Pay Item", [item("FPI-W1", WORKER)])
		first = self.pay()
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-W1", "status"), "Paid")
		doc = frappe.get_doc("Farm Payroll Entry", first["name"])
		doc.status = "Cancelled"
		doc.save(ignore_permissions=True)
		STORE.commit()
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-W1", "status"), "Pending")
		again = self.pay()
		self.assertEqual(again["pay_items_paid"], ["FPI-W1"])

	def test_a_status_written_without_a_save_still_frees_them(self):
		STORE.seed("Farm Pay Item", [item("FPI-W2", WORKER)])
		first = self.pay()
		frappe.db.set_value("Farm Payroll Entry", first["name"], "status", "Cancelled")
		STORE.commit()
		again = self.pay()
		self.assertEqual(again["pay_items"][0]["released_from"], first["name"])
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-W2", "payroll_entry"), again["name"])

	def test_a_live_run_keeps_them(self):
		STORE.seed("Farm Pay Item", [item("FPI-W3", WORKER)])
		self.pay()
		self.assertEqual(pay_items.pending(MAIN, PERIOD_START, PERIOD_END), {})


class DailyPayWorkerWithNoDays(DailyRunCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{f"allow_{n}": 1 for n in ("run_payroll_for_period", "preview_payroll_for_period")})

	def test_a_week_with_no_daily_slips_still_settles_the_item(self):
		STORE.seed("Farm Pay Item", [item("FPI-S1", WORKER, 500.0, pay_on="2025-06-11", work_state="OR")])
		STORE.commit()
		got = self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-08",
		                                                "pay_period_end": "2025-06-14", "run_kind": "Settlement"})
		slip = got["slips"][0]
		self.assertEqual(slip["employee"], WORKER)
		self.assertEqual(slip["gross_pay"], 500.0)
		self.assertEqual(slip["net_pay"], round(slip["gross_pay"] - slip["total_deductions"], 2))
		STORE.commit()
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-S1", "status"), "Paid")
