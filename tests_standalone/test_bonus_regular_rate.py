# SPDX-License-Identifier: MIT
"""Non-discretionary bonuses in the overtime regular rate. v0.296.0 (Tim, 2026-10-09; 29 CFR 778.208-.210).

A bonus earned over several workweeks is allocated to them hour by hour; each week's regular rate rises by the same
amount per hour, and every overtime hour in those weeks is owed the extra half-time — paid with the bonus,
retroactively. Discretionary or not is a policy flag per kind (`pay_item_policy`), defaulting to non-discretionary.
"""

from unittest import mock

from erpnext_mcp import pay_items, payroll_settings

from .fixtures import MAIN, V12TestCase
from .harness import STORE
from .test_payroll_integration import WORKER, IntegrationTestCase, member, shift
from .test_settlement import SettlementCase


def item(name, amount, kind="Production Bonus", pay_on="2025-06-10", **extra):
	return {"name": name, "employee": WORKER, "company": MAIN, "kind": kind, "amount": amount, "pay_on": pay_on,
	        "status": "Pending", "treatment": "Wages", **extra}


class TheArithmetic(V12TestCase):
	def test_allocated_hour_by_hour_and_paid_on_the_overtime_hours(self):
		"""$400 over two weeks of 50 h (10 OT) and 30 h: $5 an hour more on every hour; 10 OT hours x $5 x 0.5 = $25."""
		got = pay_items.regular_rate_premium(400.0, [{"hours": 50, "overtime_hours": 10}, {"hours": 30,
		                                                                                    "overtime_hours": 0}], 1.5)
		self.assertEqual((got["per_hour"], got["premium"]), (5.0, 25.0))
		self.assertEqual([w["bonus_share"] for w in got["weeks"]], [250.0, 150.0])
		self.assertEqual([w["overtime_premium"] for w in got["weeks"]], [25.0, 0.0])

	def test_no_overtime_no_premium(self):
		self.assertEqual(pay_items.regular_rate_premium(400.0, [{"hours": 40, "overtime_hours": 0}], 1.5)["premium"], 0)

	def test_non_discretionary_by_default_and_a_flag_per_kind(self):
		for kind in ("Bonus", "Production Bonus", "Profit Share", "Commission", "Something new"):
			self.assertFalse(payroll_settings.is_discretionary(kind), kind)
		policy = {"kinds": {"Bonus": {"discretionary": True}}, "default_discretionary": False}
		self.assertTrue(payroll_settings.is_discretionary("Bonus", policy=policy))
		self.assertFalse(payroll_settings.is_discretionary("Profit Share", policy=policy))

	def test_the_policy_validates(self):
		bad = payroll_settings.validate({"kinds": {"Bonus": {"discretionary": "maybe"}}, "effective_from": "2026-01-01",
		                                 "source": "x"}, payroll_settings.PAY_ITEM_POLICY_KEY)
		self.assertTrue(bad["errors"])
		warned = payroll_settings.validate({"kinds": {"Bonus": {"discretionary": True}}, "effective_from": "2026-01-01",
		                                    "source": "x"}, payroll_settings.PAY_ITEM_POLICY_KEY)
		self.assertIn("778.211", warned["warnings"][0])


class InARun(IntegrationTestCase):
	"""Five nine-hour days in the week of 2025-06-01: 45 hours, 5 over 40."""

	def setUp(self):
		super().setUp()
		self.structure(WORKER, rate=20.0)
		self.seed_shifts(*[shift(f"B{i}", f"2025-06-0{2 + i}", crew=[member(WORKER)]) for i in range(5)])
		STORE.commit()

	def slip(self, start="2025-06-02", end="2025-06-15"):
		got = self.tool_data("preview_payroll_for_period", {"company": MAIN, "pay_period_start": start,
		                                                    "pay_period_end": end})
		return got, next(s for s in got["slips"] if s["employee"] == WORKER)

	def test_the_bonus_overtime_is_paid_with_it(self):
		STORE.seed("Farm Pay Item", [item("FPI-R1", 450.0, earned_from="2025-06-01", earned_to="2025-06-07")])
		STORE.commit()
		got, slip = self.slip()
		derived = [i for i in slip["pay_items"] if i.get("derived_from") == "FPI-R1"]
		self.assertEqual(len(derived), 1)
		# $450 / 45 h = $10 an hour more; 5 OT hours x $10 x 0.5 = $25.
		self.assertEqual(derived[0]["amount"], 25.0)
		self.assertEqual(slip["pay_items_wages"], 475.0)
		self.assertIn("778.20", got["pay_items"][-1]["regular_rate"]["source"])

	def test_retroactive_for_weeks_in_an_earlier_period(self):
		STORE.seed("Farm Pay Item", [item("FPI-R2", 450.0, pay_on="2025-06-20", earned_from="2025-06-01",
		                                  earned_to="2025-06-07")])
		STORE.commit()
		_got, slip = self.slip("2025-06-16", "2025-06-29")
		derived = [i for i in slip["pay_items"] if i.get("derived_from") == "FPI-R2"]
		self.assertEqual(derived[0]["amount"], 25.0, "paid now, for the overtime worked in June's first week")

	def test_a_discretionary_kind_adds_nothing(self):
		STORE.seed("Farm Pay Item", [item("FPI-R3", 450.0, kind="Bonus", earned_from="2025-06-01",
		                                  earned_to="2025-06-07")])
		STORE.commit()
		policy = {**payroll_settings.DEFAULT_PAY_ITEM_POLICY, "kinds": {"Bonus": {"discretionary": True}}}
		with mock.patch.object(payroll_settings, "pay_item_policy", return_value=policy):
			_got, slip = self.slip()
		self.assertFalse([i for i in slip["pay_items"] if i.get("derived_from")])
		self.assertEqual(slip["pay_items_wages"], 450.0)

	def test_a_crew_share_still_counts(self):
		STORE.seed("Farm Pay Item", [item("FPI-R4", 900.0, kind="Profit Share", treatment="Not subject to withholding",
		                                  earned_from="2025-06-01", earned_to="2025-06-07")])
		STORE.commit()
		_got, slip = self.slip()
		self.assertEqual([i["amount"] for i in slip["pay_items"] if i.get("derived_from") == "FPI-R4"], [50.0])


class InTheSettlement(SettlementCase):
	def test_a_daily_pay_workers_bonus_overtime_is_settled(self):
		STORE.seed("Farm Pay Item", [item("FPI-S9", 450.0, pay_on="2025-06-05", earned_from="2025-06-01",
		                                  earned_to="2025-06-07")])
		STORE.commit()
		slip = self.settle()["slips"][0]
		derived = [i for i in slip["pay_items"] if i.get("derived_from") == "FPI-S9"]
		self.assertEqual(derived[0]["amount"], 25.0, "45 h, 5 over: $10 an hour more, half of it on 5 hours")
