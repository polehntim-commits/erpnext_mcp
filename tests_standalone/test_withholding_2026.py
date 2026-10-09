# SPDX-License-Identifier: MIT
"""Federal withholding against IRS Publication 15-T (2026). v0.286.1.

Three defects, each of which over-withheld every paycheck, are pinned here to ABSOLUTE dollars — the existing tests
only compared one employee's withholding with another's, which is how they went unseen:

1. The income was annualized and then walked through brackets stored PER PERIOD (the install seeds Weekly,
   Biweekly, … rows divided down from the annual table): $1,000 a week withheld $353.57.
2. Worksheet 1A line 1(g) — $8,600, or $12,900 married filing jointly, subtracted when the Step 2 box is not
   checked — was never applied.
3. The tables were chosen by the year on the W-4 (default 2025), not the year the wages are paid.

Worked by hand from Worksheet 1A and the 2026 annual standard table (page 12):
  $1,000 weekly, single: 52,000 − 8,600 = 43,400 → 1,240 + 12% × (43,400 − 19,900) = 4,060 → ÷ 52 = $78.08.
  $2,000 biweekly, married filing jointly: 52,000 − 12,900 = 39,100 → 10% × (39,100 − 19,300) = 1,980 → ÷ 26 = $76.15.
  $200 a day (daily, 260), head of household: 52,000 − 8,600 = 43,400 → 1,770 + 12% × (43,400 − 33,250) = 2,988 → ÷ 260 = $11.49.
"""

import unittest

from erpnext_mcp import withholding as w

FICA = {"social_security_rate_employee": 6.2, "social_security_wage_base": 184500, "medicare_rate_employee": 1.45,
        "additional_medicare_threshold": 200000, "additional_medicare_rate": 0.9}
W4_STATUS = {"Single": "Single or Married Filing Separately", "Married Filing Jointly": "Married Filing Jointly",
             "Head of Household": "Head of Household"}


def fit(gross, period, status, rows, **w4):
	return w.calculate_federal_withholding(gross, period, {"filing_status": W4_STATUS[status], **w4}, 0, 0, FICA,
	                                       rows)["federal_income_tax"]


def seeded(status, period, year=2026):
	return [r for r in w.seed_brackets(year) if r["filing_status"] == status and r["payroll_period"] == period]


class The2026Table(unittest.TestCase):
	def test_every_row_follows_from_the_one_before(self):
		for status, rows in w.ANNUAL_BRACKETS_2026.items():
			for before, row in zip(rows, rows[1:]):
				with self.subTest(status=status, floor=row["bracket_floor"]):
					self.assertEqual(before["bracket_ceiling"], row["bracket_floor"])
					width = before["bracket_ceiling"] - before["bracket_floor"]
					self.assertAlmostEqual(before["base_tax"] + width * before["marginal_rate"] / 100, row["base_tax"],
					                       places=2)

	def test_both_years_are_seeded(self):
		self.assertEqual(set(w.SEEDED_YEARS), {2025, 2026})
		self.assertTrue(seeded("Single", "Daily"))


class AbsoluteDollars(unittest.TestCase):
	def test_worked_examples(self):
		for gross, period, status, expected in ((1000.0, "Weekly", "Single", 78.08),
		                                        (2000.0, "Biweekly", "Married Filing Jointly", 76.15),
		                                        (200.0, "Daily", "Head of Household", 11.49)):
			with self.subTest(period=period, status=status):
				self.assertEqual(fit(gross, period, status, w.ANNUAL_BRACKETS_2026[status]), expected)
				self.assertAlmostEqual(fit(gross, period, status, seeded(status, period)), expected, delta=0.02,
				                       msg="the seeded per-period rows give the annual answer, to the cent's rounding")

	def test_a_checked_step_2_box_skips_line_1g(self):
		# 52,000 → 1,240 + 12% × 32,100 = 5,092 → ÷ 52 = 97.92 (on the standard table; see the code's 2(c) note).
		self.assertEqual(fit(1000.0, "Weekly", "Single", w.ANNUAL_BRACKETS_2026["Single"], multiple_jobs=True), 97.92)

	def test_step_4b_deductions_come_off_too(self):
		# 52,000 − 5,200 − 8,600 = 38,200 → 1,240 + 12% × 18,300 = 3,436 → ÷ 52 = 66.08.
		self.assertEqual(fit(1000.0, "Weekly", "Single", w.ANNUAL_BRACKETS_2026["Single"], deductions=5200), 66.08)


if __name__ == "__main__":
	unittest.main()
