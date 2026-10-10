# SPDX-License-Identifier: MIT
"""Oregon farm overtime as BOLI computes it. v0.298.0 (Tim, 2026-10-10: align with BOLI; use the legal floor).

* ORS 653.272: 48 hours a workweek in 2025-26, 40 from 2027 (55 in 2023-24); Washington 40 — dated, per state,
  per workweek, through the Pay Policy's `state_floor` mode. A week in two states takes the lower.
* OAR 839-020-0030(2): each workweek stands alone; the regular rate is never below the minimum wage.
* ORS 653.271(4): the employer's fixed workweek (Pay Policy workweek_start), not the pay period's first day.
"""

from erpnext_mcp import install, payroll_settings, phone_config
from erpnext_mcp import payroll_integration as pi

from .fixtures import MAIN
from .harness import STORE
from .test_payroll_integration import PICKER, WORKER, IntegrationTestCase, member, shift

POLICY = {**payroll_settings.DEFAULT_PAY_POLICY, "effective_from": "2020-01-01"}


def days(prefix, first_day, count, hours_end="15:00:00", state="OR", who=WORKER):
	import datetime

	start = datetime.date.fromisoformat(first_day)
	return [shift(f"{prefix}{i}", str(start + datetime.timedelta(days=i)), end=hours_end, state=state,
	              crew=[member(who)]) for i in range(count)]


class TheThreshold(IntegrationTestCase):
	def threshold(self, day, state):
		with payroll_settings.overlay(payroll_settings.PAY_POLICY_KEY, POLICY):
			return payroll_settings.overtime(day, state=state)["weekly_threshold_hours"]

	def test_oregon_is_48_through_2026_and_40_from_2027(self):
		self.assertEqual(self.threshold("2025-06-07", "OR"), 48.0)
		self.assertEqual(self.threshold("2026-12-31", "OR"), 48.0)
		self.assertEqual(self.threshold("2027-01-09", "OR"), 40.0)
		self.assertEqual(self.threshold("2024-06-07", "OR"), 55.0)

	def test_washington_is_40(self):
		self.assertEqual(self.threshold("2025-06-07", "WA"), 40.0)

	def test_the_farms_own_policy_mode_still_works(self):
		farm = {**POLICY, "overtime": {**POLICY["overtime"], "mode": "never_below_state_floor"}}
		with payroll_settings.overlay(payroll_settings.PAY_POLICY_KEY, farm):
			self.assertEqual(payroll_settings.overtime("2025-06-07", state="OR")["weekly_threshold_hours"], 40.0)


class InARun(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.structure(WORKER, rate=20.0)

	def preview(self, start="2025-06-01", end="2025-06-14"):
		with payroll_settings.overlay(payroll_settings.PAY_POLICY_KEY, POLICY):
			got = self.tool_data("preview_payroll_for_period", {"company": MAIN, "pay_period_start": start,
			                                                    "pay_period_end": end})
		return got, next(s for s in got["slips"] if s["employee"] == WORKER)

	def test_45_oregon_hours_in_2025_are_not_overtime(self):
		self.seed_shifts(*days("A", "2025-06-02", 5))  # 5 x 9 h = 45 h
		STORE.commit()
		_got, slip = self.preview()
		self.assertEqual(slip["overtime_hours"], 0.0)

	def test_50_oregon_hours_in_2025_are_two_of_overtime(self):
		self.seed_shifts(*days("B", "2025-06-01", 5, hours_end="16:00:00"))  # 5 x 10 h = 50 h
		STORE.commit()
		_got, slip = self.preview()
		self.assertEqual(slip["overtime_hours"], 2.0)
		self.assertEqual(slip["regular_rate_by_week"]["weeks"][0]["threshold"], 48.0)

	def test_45_washington_hours_are_five_of_overtime(self):
		self.seed_shifts(*days("C", "2025-06-02", 5, state="WA"))
		STORE.commit()
		_got, slip = self.preview()
		self.assertEqual(slip["overtime_hours"], 5.0)

	def test_from_2027_oregon_is_forty(self):
		self.seed_shifts(*days("D", "2027-01-04", 5))
		STORE.commit()
		_got, slip = self.preview("2027-01-03", "2027-01-16")
		self.assertEqual(slip["overtime_hours"], 5.0)

	def test_a_week_in_both_states_takes_the_lower_threshold(self):
		self.seed_shifts(*days("E", "2025-06-02", 3), *days("F", "2025-06-05", 2, state="WA"))
		STORE.commit()
		_got, slip = self.preview()
		self.assertEqual(slip["overtime_hours"], 5.0)

	def test_the_workweek_is_the_employers_not_the_periods(self):
		"""A period starting Wednesday 2025-06-04: the workweek still runs Sunday 2025-06-01 to Saturday 06-07."""
		self.seed_shifts(*days("G", "2025-06-04", 4, hours_end="18:00:00"))  # Wed-Sat, 4 x 12 = 48
		STORE.commit()
		got, slip = self.preview("2025-06-04", "2025-06-17")
		self.assertEqual(got["workweek_anchor"], "2025-06-01")
		self.assertIn("inside the workweek", got["workweek_warning"])
		self.assertEqual(slip["regular_rate_by_week"]["weeks"][0]["week_start"], "2025-06-01")


class TheRegularRateIsWeekly(IntegrationTestCase):
	def test_never_below_the_minimum_wage(self):
		"""A slow piece week: $300 / 50 h = $6 — the regular rate is the floor, not $6 (OAR 839-020-0030(2)(b))."""
		rows = days("H", "2025-06-01", 5, hours_end="16:00:00", who=PICKER)
		for row in rows:
			row["crew"][0]["piece_units"] = 40
		agg = pi.aggregate_shifts_for_period(rows, "2025-06-01", "2025-06-14", overtime_threshold=40.0)[PICKER]
		got = pi.weekly_regular_rates(agg, {"pay_type": "Piece Rate", "base_rate": 1.5},
		                              {"OR": {"standard": 15.05}}, {})
		self.assertEqual(got["weeks"][0]["regular_rate"], 15.05)
		self.assertTrue(got["weeks"][0]["raised_to_minimum_wage"])
		self.assertEqual(got["ot_regular_rate"], 15.05)


class TheMigrateDraftsTheLegalFloor(IntegrationTestCase):
	def test_an_existing_farm_policy_gets_a_draft_to_publish(self):
		old = {**POLICY, "overtime": {**POLICY["overtime"], "mode": "never_below_state_floor"}}
		phone_config.seed(payroll_settings.KIND, payroll_settings.PAY_POLICY_KEY, old, "test")
		install._pay_policy_state_floor_draft()
		rows = phone_config.rows(payroll_settings.KIND, payroll_settings.PAY_POLICY_KEY)
		draft = next(r for r in rows if r["status"] == phone_config.DRAFT)
		self.assertEqual(int(draft["version"]), 2)
		install._pay_policy_state_floor_draft()
		self.assertEqual(len(phone_config.rows(payroll_settings.KIND, payroll_settings.PAY_POLICY_KEY)), 2,
		                 "never a second draft")
