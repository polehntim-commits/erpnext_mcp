# SPDX-License-Identifier: MIT
"""Garnishment catch-up and carry-forward — Tim's middle path. v0.292.0.

Each period takes the current amount, then as much of the arrears as still fits under the cap; what does not fit is
carried forward and caught up later, never over a cap. Limits are dated, sourced rows (payroll setting
`garnishment_limits`); the policy is a payroll setting too (`garnishment_policy`). Tim asked for five cases by name:
cap hit, carry-forward, multiple orders, a near-minimum-wage week, a share-payout week. The tool tests below run the
same arithmetic through saved runs, the ledger, the register and the deduction record.
"""

from erpnext_mcp import garnishment_ledger, payroll_settings
from erpnext_mcp import payroll_deductions as pd
from erpnext_mcp.payroll_calc import calculate_full_payroll

from .fixtures import MAIN, V12TestCase
from .harness import STORE, frappe
from .test_payroll_deductions import FICA, row
from .test_payroll_integration import ON, PERIOD_END, PERIOD_START, WORKER, IntegrationTestCase, member, shift
from .test_settlement import SettlementCase

FEDERAL = {"federal": payroll_settings.garnishment_limits("2026-08-01")["federal"]}


def by_name(result):
	return {line["deduction"]: line for line in result["lines"]}


class CapHit(V12TestCase):
	def test_a_creditor_order_over_the_cap_takes_the_cap_and_carries_the_rest(self):
		"""$600 weekly disposable: 25% is $150, the amount over 30 × $7.25 is $382.50 — the cap is $150."""
		got = pd.apply_garnishments(600.0, [row(name="WG", deduction_category="Wage Garnishment", amount=500)],
		                            "Weekly", limits=FEDERAL)
		line = by_name(got)["WG"]
		self.assertEqual(line["amount"], 150.0)
		self.assertEqual(line["cap_amount"], 150.0)
		self.assertEqual(line["limit_rule"], "ordinary")
		self.assertEqual(line["carried_forward"], 350.0)
		self.assertEqual(line["arrears_after"], 350.0)
		self.assertIn("carried forward", line["shortfall_reason"])
		self.assertEqual(got["carried_forward"], 350.0)

	def test_never_over_the_cap_even_with_arrears_owed(self):
		got = pd.apply_garnishments(600.0, [row(name="WG", deduction_category="Wage Garnishment", amount=100,
		                                        arrears_balance=5000)], "Weekly", limits=FEDERAL)
		line = by_name(got)["WG"]
		self.assertEqual((line["current_taken"], line["catch_up_taken"], line["amount"]), (100.0, 50.0, 150.0))
		self.assertEqual(line["arrears_after"], 4950.0)


class CarryForward(V12TestCase):
	def test_next_period_catches_up_as_far_as_the_cap_allows(self):
		"""Week two: $1,000 disposable, cap $250. Current $100, then $150 of the $350 carried — $200 left."""
		got = pd.apply_garnishments(1000.0, [row(name="WG", deduction_category="Wage Garnishment", amount=100,
		                                         arrears_balance=350)], "Weekly", limits=FEDERAL)
		line = by_name(got)["WG"]
		self.assertEqual(line["current_taken"], 100.0)
		self.assertEqual(line["catch_up_taken"], 150.0)
		self.assertEqual(line["carried_forward"], 0.0)
		self.assertEqual(line["arrears_before"], 350.0)
		self.assertEqual(line["arrears_after"], 200.0)
		self.assertEqual(got["catch_up_taken"], 150.0)

	def test_the_orders_own_schedule_wins_still_capped(self):
		got = pd.apply_garnishments(1000.0, [row(name="WG", deduction_category="Wage Garnishment", amount=100,
		                                         arrears_balance=350, arrears_per_period=40)], "Weekly", limits=FEDERAL)
		self.assertEqual(by_name(got)["WG"]["catch_up_taken"], 40.0)
		capped = pd.apply_garnishments(500.0, [row(name="WG", deduction_category="Wage Garnishment", amount=100,
		                                           arrears_balance=350, arrears_per_period=90)], "Weekly", limits=FEDERAL)
		self.assertEqual(by_name(capped)["WG"]["amount"], 125.0, "25% of $500 — the order's $90 does not lift the cap")

	def test_the_policy_is_config_not_code(self):
		got = pd.apply_garnishments(1000.0, [row(name="WG", deduction_category="Wage Garnishment", amount=100,
		                                         arrears_balance=350)], "Weekly", limits=FEDERAL,
		                            policy={"catch_up": "none"})
		self.assertEqual(by_name(got)["WG"]["catch_up_taken"], 0.0)

	def test_a_short_settlement_gives_up_the_catch_up_first(self):
		got = pd.apply_garnishments(1000.0, [row(name="WG", deduction_category="Wage Garnishment", amount=100,
		                                         arrears_balance=350)], "Weekly", limits=FEDERAL)
		cut = pd.take_less(by_name(got)["WG"], 80.0, "the settlement's net was short")
		self.assertEqual((cut["current_taken"], cut["catch_up_taken"], cut["carried_forward"]), (80.0, 0.0, 20.0))
		self.assertEqual(cut["arrears_after"], 370.0)


class MultipleOrders(V12TestCase):
	def test_support_then_levy_then_creditor_within_the_combined_cap(self):
		"""$1,000 weekly disposable. Support (50%: $500) takes current $300 and $200 of arrears. The IRS levy, outside
		Title III, takes its $100 out of what is left. The creditor's pool is 25% ($250) less support's $500 — nothing;
		its $200 is carried forward, and the line says support took the pool (29 CFR 870.11(b)(1))."""
		orders = [
			row(name="WG", deduction_category="Wage Garnishment", amount=200, effective_from="2020-01-01"),
			row(name="LV", deduction_category="Tax Levy", amount=100),
			row(name="CS", deduction_category="Child Support", amount=300, arrears_balance=900),
		]
		got = pd.apply_garnishments(1000.0, pd.active_deductions(orders, "2026-08-01"), "Weekly", limits=FEDERAL)
		self.assertEqual([line["deduction"] for line in got["lines"]], ["CS", "LV", "WG"])
		lines = by_name(got)
		self.assertEqual((lines["CS"]["current_taken"], lines["CS"]["catch_up_taken"]), (300.0, 200.0))
		self.assertEqual(lines["CS"]["arrears_after"], 700.0)
		self.assertEqual(lines["LV"]["amount"], 100.0)
		self.assertEqual(lines["WG"]["amount"], 0.0)
		self.assertEqual(lines["WG"]["carried_forward"], 200.0)
		self.assertIn("870.11(b)(1)", lines["WG"]["shortfall_reason"])

	def test_every_current_support_order_before_any_arrears(self):
		orders = [row(name="CS1", deduction_category="Child Support", amount=200, arrears_balance=1000),
		          row(name="CS2", deduction_category="Child Support", amount=200)]
		lines = by_name(pd.apply_garnishments(900.0, pd.active_deductions(orders, "2026-08-01"), "Weekly",
		                                      limits=FEDERAL))
		self.assertEqual((lines["CS1"]["current_taken"], lines["CS2"]["current_taken"]), (200.0, 200.0))
		self.assertEqual(lines["CS1"]["catch_up_taken"], 50.0, "50% of $900 is $450; $400 went to current support")

	def test_the_priority_has_a_source(self):
		policy = payroll_settings.garnishment_policy("2026-08-01")
		self.assertEqual(policy["priority"][0], "child_support")
		self.assertIn("870.11", policy["priority_source"])


class NearMinimumWageWeek(V12TestCase):
	def test_oregons_floor_is_stricter_than_the_federal_one(self):
		"""$450 weekly disposable from 2026-07-01: Oregon exempts $400 (ORS 18.385), so $50 — the federal rule alone
		would allow $112.50."""
		orders = [row(name="WG", deduction_category="Wage Garnishment", amount=200)]
		oregon = payroll_settings.garnishment_limits("2026-08-01", "OR")
		got = pd.apply_garnishments(450.0, orders, "Weekly", limits=oregon)
		self.assertEqual(by_name(got)["WG"]["amount"], 50.0)
		self.assertEqual(by_name(got)["WG"]["carried_forward"], 150.0)
		self.assertIn("OR minimum exemption", got["limits_applied"]["floor_source"])
		federal = pd.apply_garnishments(450.0, orders, "Weekly", limits=FEDERAL)
		self.assertEqual(by_name(federal)["WG"]["amount"], 112.5)

	def test_at_the_federal_floor_nothing_is_taken(self):
		got = pd.apply_garnishments(217.50, [row(name="WG", deduction_category="Wage Garnishment", amount=50,
		                                         arrears_balance=100)], "Weekly", limits=FEDERAL)
		line = by_name(got)["WG"]
		self.assertEqual((line["amount"], line["carried_forward"], line["arrears_after"]), (0.0, 50.0, 150.0))

	def test_the_floor_is_dated(self):
		self.assertEqual(payroll_settings.garnishment_limits("2026-06-30", "OR")["state"]["floors"]["Weekly"], 338)
		self.assertEqual(payroll_settings.garnishment_limits("2026-07-01", "OR")["state"]["floors"]["Weekly"], 400)


class SharePayoutWeek(V12TestCase):
	def slip(self, items=(), deductions=()):
		return calculate_full_payroll(
			{"employee": "E1", "employee_name": "Ana"},
			[{"work_state": "OR", "hours": 40, "overtime_hours": 0, "piece_units": 0, "break_hours": 0}],
			{"name": "S1", "pay_type": "Hourly", "base_rate": 20.0},
			{"w4_data": {"filing_status": "Single"}, "fica_config": FICA, "federal_tax_table": [],
			 "pay_frequency": "Weekly", "pay_items": list(items),
			 "garnishment_limits": payroll_settings.garnishment_limits("2026-08-01")},
			deductions=list(deductions))

	def test_a_crew_share_counts_for_the_cap_and_is_not_withheld_on(self):
		order = [row(name="WG", deduction_category="Wage Garnishment", amount=1000)]
		plain = self.slip(deductions=order)
		share = self.slip([{"name": "FPI-1", "kind": "Profit Share", "amount": 2000,
		                    "treatment": "Not subject to withholding"}], order)
		self.assertEqual(share["gross_pay"], round(plain["gross_pay"] + 2000, 2))
		self.assertEqual(share["statutory_deductions"], plain["statutory_deductions"], "no tax withheld on the share")
		self.assertEqual(share["disposable_earnings"], round(plain["disposable_earnings"] + 2000, 2))
		self.assertEqual(share["garnishment_total"], round(share["disposable_earnings"] * 0.25, 2))
		self.assertGreater(share["garnishment_total"], plain["garnishment_total"])
		self.assertEqual(share["pay_items_not_withheld"], 2000.0)
		self.assertEqual(share["net_pay"], round(share["gross_pay"] - share["total_deductions"], 2))

	def test_a_bonus_is_wages(self):
		plain = self.slip()
		bonus = self.slip([{"name": "FPI-2", "kind": "Bonus", "amount": 500, "treatment": "Wages"}])
		self.assertEqual(bonus["gross_pay"], round(plain["gross_pay"] + 500, 2))
		self.assertGreater(bonus["social_security"], plain["social_security"])
		self.assertEqual(bonus["pay_items_wages"], 500.0)
		self.assertTrue(bonus["minimum_wage_check"])


# ── through the tools: saved runs, the ledger, the register, the records ─────────────────────────────────────────
TOOLS = {**ON, **{f"allow_{n}": 1 for n in ("create_payroll_deduction", "get_payroll_deduction",
                                            "update_payroll_deduction", "get_payroll_register")}}
NEXT = ("2025-06-16", "2025-06-29")


class ThroughTheRuns(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **TOOLS)
		self.structure(WORKER, rate=20.0)
		days = ("2025-06-02", "2025-06-03", "2025-06-16", "2025-06-17")
		self.seed_shifts(*[shift(f"G{i}", day, crew=[member(WORKER)]) for i, day in enumerate(days)])
		order = self.tool_data("create_payroll_deduction", {
			"employee": WORKER, "company": MAIN, "deduction_category": "Wage Garnishment", "amount": 300,
			"effective_from": "2025-01-01", "reference": "CASE-9", "arrears_opening": 100,
			"debt_type": "Other judgment"})
		self.order = order["deduction"]["name"]
		STORE.commit()

	def pay(self, period=(PERIOD_START, PERIOD_END)):
		got = self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": period[0],
		                                                "pay_period_end": period[1]})
		STORE.commit()
		return got

	def line(self, slip):
		return next(line for line in slip["deduction_lines"] if line["deduction"] == self.order)

	def test_each_saved_run_is_in_the_ledger_and_the_balance_follows(self):
		first = self.pay()
		taken = self.line(first["slips"][0])
		self.assertEqual(taken["arrears_before"], 100.0)
		self.assertEqual(first["garnishment_ledger"][0]["deduction"], self.order)
		self.assertEqual(garnishment_ledger.balance(self.order), taken["arrears_after"])
		self.assertEqual(frappe.db.get_value("Farm Payroll Deduction", self.order, "arrears_balance"),
		                 taken["arrears_after"])
		second = self.line(self.pay(NEXT)["slips"][0])
		self.assertEqual(second["arrears_before"], taken["arrears_after"])

		record = self.tool_data("get_payroll_deduction", {"deduction": self.order})
		self.assertEqual(len(record["arrears"]["ledger"]), 2)
		self.assertEqual(record["arrears"]["balance"], second["arrears_after"])

		register = self.tool_data("get_payroll_register", {"company": MAIN, "date_from": PERIOD_START,
		                                                   "date_to": NEXT[1]})
		rows = register["garnishments"]["rows"]
		self.assertEqual([r["order"] for r in rows], [self.order, self.order])
		self.assertEqual(rows[0]["cap"], taken["cap_amount"])
		self.assertEqual(rows[-1]["arrears_after"], second["arrears_after"])

	def test_a_cancelled_run_does_not_count(self):
		first = self.pay()
		frappe.db.set_value("Farm Payroll Entry", first["name"], "status", "Cancelled")
		STORE.commit()
		self.assertEqual(garnishment_ledger.balance(self.order), 100.0)

	def test_a_pay_item_is_paid_once_and_counts_for_the_cap(self):
		STORE.seed("Farm Pay Item", [{"name": "FPI-T1", "employee": WORKER, "company": MAIN, "kind": "Profit Share",
		                              "amount": 1500, "pay_on": "2025-06-10", "status": "Pending",
		                              "treatment": "Not subject to withholding"}])
		first = self.pay()
		slip = first["slips"][0]
		self.assertEqual(slip["pay_items_not_withheld"], 1500.0)
		self.assertEqual(first["pay_items_paid"], ["FPI-T1"])
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-T1", "status"), "Paid")
		self.assertEqual(frappe.db.get_value("Farm Pay Item", "FPI-T1", "payroll_entry"), first["name"])
		self.assertEqual(self.pay(NEXT)["pay_items"], [], "not paid twice")


class TheSettlementCarriesForward(SettlementCase):
	def test_a_short_settlement_is_carried_into_the_ledger(self):
		order = self.tool_data("create_payroll_deduction", {"employee": WORKER, "company": MAIN,
		                                                    "deduction_category": "Child Support", "amount": 400,
		                                                    "effective_from": "2025-01-01", "reference": "CASE-S"})
		STORE.commit()
		got = self.settle(run=True)
		STORE.commit()
		line = got["slips"][0]["deduction_lines"][0]
		self.assertGreater(line["carried_forward"], 0)
		self.assertEqual(got["garnishment_ledger"][0]["run_kind"], "Settlement")
		self.assertEqual(garnishment_ledger.balance(order["deduction"]["name"]), line["arrears_after"])
