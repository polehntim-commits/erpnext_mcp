# SPDX-License-Identifier: MIT
"""Piecework and hours — the pay rules, tested as scenarios a foreman would recognise.

`test_payroll.py` tests the engine's functions one at a time and
`test_payroll_integration.py` tests the join from the shift register to the
slip. Neither walks a WHOLE DAY of picking end to end and asserts the dollar
figure, and neither pins the three places where what the code does and what a
reader assumes it does come apart. That is what this file is for.

v0.48.2 wrote this file to pin four places where the code and a reader's
assumption came apart. v0.49.0 closed all four, and every test that asserted a
gap is now the test that asserts it is shut. What follows is what the rules ARE.

────────────────────────────────────────────────────────────────────────────
THE HIGHER-OF RULE IS APPLIED AND THE MAKEUP IS NAMED
────────────────────────────────────────────────────────────────────────────

Ask most people how piecework pay works and they will say "the greater of the
piece total and minimum wage for the hours", and that is now what this app does.
FLSA §6, ORS 653.025 and RCW 49.46.020 all make the minimum wage a floor under
the WAGE: piece rate is a way of measuring pay, never a way to earn less than
the hours were worth. Forty-seven buckets at $1.50 in an eight-hour Oregon day
pays $117.60 — $70.50 earned and $47.10 of `minimum_wage_makeup`.

THE MAKEUP IS ITS OWN FIGURE, and that is the half of the old posture worth
keeping. Through v0.48.2 the shortfall was reported and gross was left alone,
precisely so a rate set below the lawful floor would stay visible. Folding the
top-up silently into gross would have lost that, so it is not folded in: it is a
field on the slip, a column on the stored row, and `topped_up_to_minimum_wage` at
the top of the run summary. `TheHigherOfRuleIsApplied` pins both halves — the
worker is paid, and the rate is still on somebody's screen.

THE FLOOR CARRIES THE OVERTIME PREMIUM. A picker who works fifty hours has a
statutory floor of forty at straight time plus ten at one and a half: $588 plus
$220.50 is $808.50, not the flat $735. Four hundred buckets at $1.50 earns $660
over those fifty hours, so the day pays $808.50. `OvertimeUnderPieceRate` pins
the floor, the $148.50 of makeup, and the fact that omitting the overtime split
from the check is what used to produce the passing verdict.

THE PIECE OVERTIME PREMIUM IS HALF-TIME, NOT TIME AND A HALF. 29 CFR 778.111:
the regular rate is straight-time earnings over hours worked, and the piece
earnings ALREADY paid straight time for the overtime hours — so what is owed on
top is one half of the regular rate. Through v0.48.2 this app paid the full 1.5x
on top, which is more than the law asks and could not be reconciled with the
weighted-average method a mixed day needs.

OVERTIME IS WEEKLY, SO A TEN-HOUR DAY IS NOT OVERTIME. Both states put the ag
threshold at forty hours in a workweek and neither has a daily one. Ten hours on
Tuesday is ten straight-time hours; the premium starts wherever the fortieth hour
of that week falls, which may be Friday morning.

────────────────────────────────────────────────────────────────────────────
AND A WORKER CAN NOW BE PART PIECE-RATE AND PART HOURLY
────────────────────────────────────────────────────────────────────────────

`pay_type` was one field on one salary structure, so a picker who spent the
morning on buckets and the afternoon on irrigation was paid the piece rate for
the afternoon — which is to say nothing, because irrigation produces no buckets.
A shift or a crew row now carries its own `pay_type` and `pay_rate`, blank on the
ordinary day, and the engine pays each stretch its own way and blends ONE regular
rate across them for the premium (29 CFR 778.115). Six hours of picking at $1.50
a bucket and two of irrigation at $16.00 is $167, and the floor is tested on all
eight hours. `MixedPieceworkAndHourlyWork` walks it.

────────────────────────────────────────────────────────────────────────────
EIGHT CLAIMS
────────────────────────────────────────────────────────────────────────────

1. `TheHigherOfRuleIsApplied` — the two scenarios, the boundary between them,
   zero buckets, Washington's higher floor and Portland's, and the makeup named
   on the slip and in the run summary.
2. `OvertimeUnderPieceRate` — a ten-hour day is not overtime; a fifty-hour week
   is; the piece premium is half the effective hourly; and the floor for those
   fifty hours is $808.50, which is what the worker is paid.
3. `BucketsBecomePieceUnits` — accepted Bucket Log Entries, through the real
   loader on the shipped doctype, become the piece units that become the gross.
   Rejected captures do not.
4. `MultiDayPayPeriod` — ten days across two workweeks aggregate into one slip
   with the hours, the units and the overtime each summed the way they are
   counted, not the way they would total.
5. `PieceworkEdgeCases` — zero buckets, a partial shift, an unclosed shift, and
   a paid rest break on a piece-rate day.
6. `MixedPieceworkAndHourlyWork` — the mixed day, end to end, with the rate from
   the shift and from the structure, and the floor over both segments.
7. `TheTwoMinimumWageVerdicts` — the flat check and the per-state check on one
   cross-state slip, and what each says now that Washington's hours are paid up
   to Washington's floor.
8. `TheBucketReconciliation` — `get_piecework_summary` and
   `reconcile_bucket_payroll`, which until v0.48.2 had no test but their name in
   the registry.
"""

from erpnext_mcp import payroll_integration as pi
from erpnext_mcp.payroll_calc import (
	MINIMUM_WAGE_RATES,
	calculate_gross_pay,
	check_minimum_wage,
)

from .fixtures import MAIN
from .harness import STORE, add_field
from .test_payroll_integration import (
	PERIOD_END,
	PERIOD_START,
	PICKER,
	WORKER,
	IntegrationTestCase,
	member,
	shift,
)

#: The rate on the board at the start of the row. Every dollar figure in this
#: file is derived from it and the two state floors, never typed twice.
BUCKET_RATE = 1.50

OR_FLOOR = MINIMUM_WAGE_RATES["OR"]["standard"]  # 14.70
WA_FLOOR = MINIMUM_WAGE_RATES["WA"]["standard"]  # 16.66

#: A day. Both scenarios in the brief are eight hours, so it is a constant.
DAY_HOURS = 8.0

BUCKET_DOCTYPE = "Bucket Log Entry"

#: The bucket tools, on. The payroll switches come from `IntegrationTestCase`.
BUCKET_ON = {
	f"allow_{name}": 1
	for name in (
		"sync_bucket_entries",
		"list_bucket_entries",
		"get_bucket_session",
		"list_bucket_sessions",
		"link_badge_to_employee",
		"link_entries_to_shift",
		"get_piecework_summary",
		"reconcile_bucket_payroll",
	)
}


def capture(index, employee=PICKER, day="2025-06-03", verdict="Accepted", at=None, **extra):
	"""One Bucket Log Entry in the shape the shipped doctype stores.

	Deliberately NOT the `picker_id`/`bucket_count` shape
	`test_payroll_integration.install_bucket_log` registers. That one models a
	site whose bucket log came from somewhere else; this one is the doctype this
	app ships, which carries `employee`, `timestamp` and `verdict` and NO count
	column — so one row is one bucket, which is what `_bucket_log_rows` reads it
	as and what the row actually records.
	"""
	minute = index % 60
	hour = 7 + (index // 60)
	row = {
		"name": f"BLE-{index:05d}",
		"entry_uuid": f"{index:08d}-0000-0000-0000-000000000000",
		"session_uuid": f"SESSION-{day}",
		"company": MAIN,
		"employee": employee,
		"timestamp": at or f"{day} {hour:02d}:{minute:02d}:00",
		"verdict": verdict,
		"status": "Pending",
	}
	row.update(extra)
	return row


class PieceworkTestCase(IntegrationTestCase):
	"""`IntegrationTestCase` plus the bucket switches."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **self._payroll_switches(), **BUCKET_ON)

	def _payroll_switches(self):
		from .test_payroll_integration import ON

		return ON

	# ── shorthands ────────────────────────────────────────────────────

	def piece_day(self, hours=DAY_HOURS, units=0.0, state="OR", rate=BUCKET_RATE, breaks=0.0):
		"""One worker, one day, straight through `calculate_gross_pay`.

		Returns (gross_result, minimum_wage_verdict) — what the WORK earned, and
		the floor it is measured against. Not what the worker is paid: since
		v0.49.0 that is the greater of the two and it comes out of the engine, so
		`self.slip(...)` is the function to ask when the question is the paycheck.
		"""
		gross = calculate_gross_pay("Piece Rate", rate, hours, 0.0, units, breaks)
		verdict = pi.check_minimum_wage_by_state(
			{state: hours},
			{state: gross["gross_pay"]},
		)
		return gross, verdict

	def slip(self, shifts, pay_type="Piece Rate", rate=BUCKET_RATE, employee=PICKER, **structure):
		"""One employee's slip for the period, computed the way a run computes it.

		Everything a paycheck question needs and nothing else: no tax
		configuration, because zero withholding on a $117.60 day is still $117.60
		of gross and the deduction stack has its own file.
		"""
		row = {
			"employee": employee,
			"name": "SS-P",
			"pay_type": pay_type,
			"base_rate": rate,
			**structure,
		}
		slips = pi.run_integrated_payroll(
			shifts,
			[row],
			{},
			{},
			{},
			PERIOD_START,
			PERIOD_END,
			company=MAIN,
			include_unworked=False,
		)
		self.assertEqual(len(slips), 1, f"expected one slip, got {len(slips)}")
		return slips[0]

	def one_day(self, units=0.0, hours=DAY_HOURS, state="OR", **crew):
		"""A single Oregon day of `hours`, starting at 06:00, with `units` picked."""
		end_hour = 6 + int(hours)
		minutes = round((hours - int(hours)) * 60)
		return [
			shift(
				"SHIFT-DAY",
				"2025-06-03",
				start="06:00:00",
				end=f"{end_hour:02d}:{minutes:02d}:00",
				state=state,
				crew=[member(PICKER, piece_units=units, **crew)],
			)
		]

	def seed_captures(self, accepted=0, rejected=0, employee=PICKER, day="2025-06-03"):
		"""`accepted` + `rejected` Bucket Log Entries on one day, one worker."""
		rows = [capture(i, employee=employee, day=day) for i in range(accepted)]
		rows += [capture(1000 + i, employee=employee, day=day, verdict="Rejected") for i in range(rejected)]
		STORE.seed(BUCKET_DOCTYPE, rows)
		return rows


# ── Claim 1: the higher-of rule is applied ────────────────────────────


class TheHigherOfRuleIsApplied(PieceworkTestCase):
	"""Gross is the greater of what the work earned and what the hours are owed.

	Both halves, in every test: the worker is paid the floor, AND the difference
	is a figure with a name on it. A top-up that vanished into gross would pay the
	worker correctly and hide a rate set too low to be lawful, which is the reason
	this app reported the shortfall instead of paying it for nineteen releases.
	"""

	# ── what the work earned, before the floor ────────────────────────

	def test_forty_seven_buckets_earn_seventy_dollars_fifty(self):
		"""47 × $1.50. This is the EARNED figure, not the paid one."""
		gross, _verdict = self.piece_day(units=47)
		self.assertEqual(gross["piece_earnings"], 70.50)
		self.assertEqual(gross["gross_pay"], 70.50)

	def test_the_gap_against_oregons_floor_is_priced_to_the_cent(self):
		"""$14.70 × 8 = $117.60 owed against $70.50 earned = $47.10 short."""
		_, verdict = self.piece_day(units=47)
		self.assertFalse(verdict["meets_minimum_wage"])
		self.assertEqual(verdict["states_below_minimum"], ["OR"])
		self.assertEqual(verdict["total_shortfall"], 47.10)
		self.assertEqual(verdict["by_state"]["OR"]["minimum_wage"], OR_FLOOR)
		self.assertEqual(verdict["by_state"]["OR"]["effective_hourly_rate"], 8.81)

	# ── what the worker is paid ───────────────────────────────────────

	def test_forty_seven_buckets_in_eight_hours_pays_oregons_floor(self):
		"""THE SCENARIO. $70.50 of buckets, $117.60 of hours. Paid: $117.60."""
		slip = self.slip(self.one_day(units=47))
		self.assertEqual(slip["total_hours"], DAY_HOURS)
		self.assertEqual(slip["piece_units"], 47)
		self.assertEqual(slip["earned_gross"], 70.50)
		self.assertEqual(slip["minimum_wage_makeup"], 47.10)
		self.assertEqual(slip["gross_pay"], round(OR_FLOOR * DAY_HOURS, 2))
		self.assertEqual(slip["gross_pay"], 117.60)
		self.assertEqual(slip["effective_hourly_rate"], OR_FLOOR)
		self.assertTrue(slip["minimum_wage_check"])

	def test_the_makeup_is_a_line_of_its_own_and_says_which_state(self):
		"""Paid is not the same as forgotten. $47.10, in Oregon, on the slip."""
		slip = self.slip(self.one_day(units=47))
		self.assertEqual(slip["minimum_wage_makeup_by_state"], {"OR": 47.10})
		self.assertEqual(slip["minimum_wage_states_topped_up"], ["OR"])
		row = slip["minimum_wage_by_state"]["OR"]
		self.assertEqual(row["minimum_wage_floor"], 117.60)
		self.assertEqual(row["earned_wages"], 70.50)
		self.assertEqual(row["minimum_wage_makeup"], 47.10)
		self.assertEqual(row["paid_wages"], 117.60)

	def test_one_hundred_and_twenty_buckets_clears_the_floor_and_is_paid_in_full(self):
		"""120 × $1.50 = $180.00 against a $117.60 floor. Paid: $180.00, no makeup.

		The direction that matters most: the rule is HIGHER-of, so a good day is
		not levelled down to the floor and the makeup stays at zero.
		"""
		slip = self.slip(self.one_day(units=120))
		self.assertEqual(slip["earned_gross"], 180.00)
		self.assertEqual(slip["gross_pay"], 180.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)
		self.assertEqual(slip["minimum_wage_makeup_by_state"], {})
		self.assertEqual(slip["effective_hourly_rate"], 22.50)

	def test_the_boundary_is_the_floor_itself_and_not_a_cent_is_added(self):
		"""78.4 buckets is exactly $117.60 — the floor, met and not exceeded."""
		units = round(OR_FLOOR * DAY_HOURS / BUCKET_RATE, 4)  # 78.4
		slip = self.slip(self.one_day(units=units))
		self.assertEqual(slip["earned_gross"], 117.60)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)
		self.assertEqual(slip["gross_pay"], 117.60)

	def test_one_bucket_short_of_the_boundary_is_topped_up_by_sixty_cents(self):
		"""78 buckets is $117.00 — sixty cents under, and sixty cents is owed."""
		slip = self.slip(self.one_day(units=78))
		self.assertEqual(slip["earned_gross"], 117.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.60)
		self.assertEqual(slip["gross_pay"], 117.60)

	def test_zero_buckets_in_a_full_shift_pays_the_floor_and_not_zero(self):
		"""A day that produced nothing is still a day that was worked.

		The rain came in, the block was not ready, the picker was moved and nobody
		wrote it down. Eight hours on the clock are eight hours owed.
		"""
		slip = self.slip(self.one_day(units=0))
		self.assertEqual(slip["earned_gross"], 0.0)
		self.assertEqual(slip["gross_pay"], 117.60)
		self.assertEqual(slip["minimum_wage_makeup"], 117.60)

	def test_the_same_day_in_washington_pays_washingtons_higher_floor(self):
		"""47 buckets against WA's $16.66: $133.28 owed, $62.78 of it makeup."""
		slip = self.slip(self.one_day(units=47, state="WA"))
		self.assertEqual(slip["gross_pay"], round(WA_FLOOR * DAY_HOURS, 2))
		self.assertEqual(slip["gross_pay"], 133.28)
		self.assertEqual(slip["minimum_wage_makeup"], 62.78)
		self.assertEqual(slip["minimum_wage_makeup_by_state"], {"WA": 62.78})

	def test_the_portland_metro_floor_is_used_where_the_structure_says_so(self):
		"""$15.95 × 8 = $127.60. The region is the employer's to declare and it
		reaches the engine, which is the only place it can change a paycheck."""
		slip = self.slip(
			self.one_day(units=47),
			min_wage_regions={"OR": "portland_metro"},
		)
		self.assertEqual(slip["gross_pay"], 127.60)
		self.assertEqual(slip["minimum_wage_makeup"], 57.10)
		self.assertEqual(slip["minimum_wage_by_state"]["OR"]["minimum_wage"], 15.95)

	def test_a_shift_with_no_state_on_it_has_no_floor_and_is_not_topped_up(self):
		"""There is no federal-fallback guess here on purpose. Topping somebody up
		to a rate no legislature named would be inventing the law rather than
		applying it — and the hours are reported either way."""
		slip = self.slip(self.one_day(units=47, state=""))
		self.assertEqual(slip["gross_pay"], 70.50)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)

	def test_a_full_run_pays_the_floor_and_names_the_worker_in_the_summary(self):
		"""End to end: the slip pays $117.60 and the run says whose rate did it."""
		self.structure(PICKER, pay_type="Piece Rate", rate=BUCKET_RATE)
		slip = self.slip(self.one_day(units=47))
		summary = pi.summarize_payroll_run([slip])

		self.assertEqual(summary["total_gross"], 117.60)
		self.assertEqual(summary["total_minimum_wage_makeup"], 47.10)
		# Nobody is below the minimum wage any more — they were paid up to it —
		# so the old list is empty and the new one carries the fact.
		self.assertEqual(summary["below_minimum_wage"], [])
		topped = summary["topped_up_to_minimum_wage"]
		self.assertEqual(len(topped), 1)
		self.assertEqual(topped[0]["employee"], PICKER)
		self.assertEqual(topped[0]["earned_gross"], 70.50)
		self.assertEqual(topped[0]["minimum_wage_makeup"], 47.10)
		self.assertEqual(topped[0]["gross_pay"], 117.60)
		self.assertEqual(topped[0]["states"], ["OR"])

	def test_the_stored_row_carries_both_halves_of_the_gross(self):
		"""Three years later, in an audit, the row has to be able to answer "why
		is this bigger than the buckets times the rate"."""
		from erpnext_mcp.tools import payroll as payroll_tools

		row = payroll_tools._slip_row(self.slip(self.one_day(units=47)))
		self.assertEqual(row["gross_pay"], 117.60)
		self.assertEqual(row["earned_gross"], 70.50)
		self.assertEqual(row["minimum_wage_makeup"], 47.10)
		self.assertEqual(row["minimum_wage_check"], 1)

	def test_a_salary_structure_is_reported_short_and_not_topped_up(self):
		"""THE ONE PLACE THE OLD POSTURE IS KEPT, and it is kept on purpose.

		Whether a salaried employee is exempt from the minimum wage — executive,
		administrative, professional, or one of the agricultural exemptions — is a
		fact about their job that this app does not hold. Raising an exempt
		supervisor's pay because a long harvest week divided their salary below
		$14.70 would be inventing an obligation, so a salaried shortfall is
		computed and REPORTED and somebody who knows the answer decides.

		$100 for an eight-hour day is $12.50 an hour, under Oregon's floor.
		"""
		slip = self.slip(self.one_day(units=0), pay_type="Salary", rate=100.00)
		self.assertEqual(slip["gross_pay"], 100.00)
		self.assertEqual(slip["earned_gross"], 100.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)
		self.assertFalse(slip["minimum_wage_check"])
		self.assertEqual(slip["minimum_wage_detail"]["states_below_minimum"], ["OR"])
		self.assertEqual(slip["minimum_wage_detail"]["total_shortfall"], 17.60)

		summary = pi.summarize_payroll_run([slip])
		self.assertEqual(len(summary["below_minimum_wage"]), 1)
		self.assertEqual(summary["topped_up_to_minimum_wage"], [])

	def test_the_deductions_are_computed_on_the_topped_up_gross(self):
		"""A makeup is wages. Withholding it as though it were not would leave the
		employer owing the tax on money the worker was already paid."""
		slip = self.slip(self.one_day(units=47))
		self.assertEqual(slip["gross_pay"], 117.60)
		self.assertEqual(
			slip["net_pay"],
			round(slip["gross_pay"] - slip["total_deductions"], 2),
		)
		self.assertEqual(slip["social_security"], round(117.60 * 0.062, 2))
		self.assertEqual(slip["medicare"], round(117.60 * 0.0145, 2))


# ── Claim 2: overtime under piece rate ────────────────────────────────


class OvertimeUnderPieceRate(PieceworkTestCase):
	"""The threshold is weekly, the premium is 1.5x effective, and the floor
	check knows about neither."""

	def week_of(self, day_hours, units_per_day, days=5, state="OR", first="2025-06-02"):
		"""`days` consecutive days from Monday `first`, one crew of one."""
		from datetime import date, timedelta

		start = date.fromisoformat(first)
		end_hour = 6 + int(day_hours)
		rows = []
		for index in range(days):
			when = start + timedelta(days=index)
			rows.append(
				shift(
					f"SHIFT-W-{index}",
					str(when),
					start="06:00:00",
					end=f"{end_hour:02d}:00:00",
					state=state,
					crew=[member(PICKER, piece_units=units_per_day)],
				)
			)
		return rows

	def test_a_ten_hour_day_on_its_own_is_not_overtime(self):
		"""Neither state has a daily threshold. Ten hours Tuesday is ten straight."""
		aggregates = self.aggregate(self.week_of(10, 40, days=1))
		agg = aggregates[PICKER]
		self.assertEqual(agg["total_hours"], 10.0)
		self.assertEqual(agg["overtime_hours"], 0.0)
		self.assertEqual(agg["regular_hours"], 10.0)

	def test_a_ten_hour_day_inside_a_thirty_hour_week_is_still_not_overtime(self):
		"""Three tens is thirty. A daily threshold would have found twenty hours of
		overtime in this week; neither state has one, and there are none."""
		agg = self.aggregate(self.week_of(10, 40, days=3))[PICKER]
		self.assertEqual(agg["total_hours"], 30.0)
		self.assertEqual(agg["overtime_hours"], 0.0)
		self.assertEqual(agg["weeks"][0]["overtime_hours"], 0.0)
		self.assertFalse(agg["weeks"][0]["over_threshold"])

	def test_a_forty_five_hour_week_is_five_hours_of_overtime(self):
		"""Five nines. The premium starts at the fortieth hour, which falls on
		Friday morning, and it is the last five hours that carry it."""
		agg = self.aggregate(self.week_of(9, 40, days=5))[PICKER]
		self.assertEqual(agg["total_hours"], 45.0)
		self.assertEqual(agg["regular_hours"], 40.0)
		self.assertEqual(agg["overtime_hours"], 5.0)
		# The day the fortieth hour fell on is split, not counted whole either way.
		friday = agg["shifts"][-1]
		self.assertEqual(friday["regular_hours"], 4.0)
		self.assertEqual(friday["overtime_hours"], 5.0)

	def test_an_hourly_worker_gets_the_full_time_and_a_half_on_those_five_hours(self):
		"""The other half of the premium rule. An hourly worker's regular pay
		covered forty hours only, so the five overtime ones are owed 1.5x — $30 an
		hour on a $20 rate — and that has not changed."""
		gross = calculate_gross_pay("Hourly", 20.00, 45.0, 5.0, 0)
		self.assertEqual(gross["regular_pay"], 800.00)
		self.assertEqual(gross["overtime_pay"], 150.00)
		self.assertEqual(gross["gross_pay"], 950.00)

	def test_five_ten_hour_days_are_forty_regular_and_ten_overtime(self):
		"""Fifty hours in one workweek. The premium starts at the fortieth."""
		agg = self.aggregate(self.week_of(10, 80))[PICKER]
		self.assertEqual(agg["total_hours"], 50.0)
		self.assertEqual(agg["regular_hours"], 40.0)
		self.assertEqual(agg["overtime_hours"], 10.0)
		self.assertEqual(len(agg["weeks"]), 1)
		self.assertTrue(agg["weeks"][0]["over_threshold"])

	def test_the_premium_is_half_the_effective_piece_hourly(self):
		"""400 buckets over 50 hours: $600 earned, $12.00/hr regular rate, $60 OT.

		29 CFR 778.111. The $600 already paid straight time for all fifty hours,
		including the ten past the threshold, so the ten are owed the OTHER half —
		$6.00 each — and not another $18.00 each. Through v0.48.2 this paid $180
		and grossed $780.
		"""
		result = calculate_gross_pay("Piece Rate", BUCKET_RATE, 50.0, 10.0, 400, 0.0)
		self.assertEqual(result["piece_earnings"], 600.00)
		self.assertEqual(result["effective_hourly_rate"], 12.00)
		self.assertEqual(result["overtime_pay"], 60.00)
		self.assertEqual(result["gross_pay"], 660.00)

	def test_the_floor_for_fifty_hours_is_eight_hundred_and_eight_fifty(self):
		"""THE SCENARIO. 40 × $14.70 + 10 × $22.05, and NOT 50 × $14.70.

		The flat product is $735, which $660 of piece earnings would have failed
		anyway — but $780 of them, which is what v0.48.2 paid for this same day,
		cleared it while being $28.50 short of the law. The premium is in the floor
		now, so neither figure can pass.
		"""
		verdict = pi.check_minimum_wage_by_state(
			{"OR": 50.0},
			{"OR": 660.00},
			overtime_hours_by_state={"OR": 10.0},
		)
		self.assertEqual(verdict["by_state"]["OR"]["minimum_wage_floor"], 808.50)
		self.assertFalse(verdict["meets_minimum_wage"])
		self.assertEqual(verdict["total_shortfall"], 148.50)

		# The same wages against the same hours, with the overtime split withheld:
		# the flat floor of $735, which is the arithmetic that used to pass.
		flat = pi.check_minimum_wage_by_state({"OR": 50.0}, {"OR": 780.00})
		self.assertEqual(flat["by_state"]["OR"]["minimum_wage_floor"], 735.00)
		self.assertTrue(flat["meets_minimum_wage"])

	def test_a_fifty_hour_piecework_week_is_paid_the_floor_with_the_premium_in_it(self):
		"""THE SCENARIO, end to end. Five ten-hour days, 400 buckets, $808.50."""
		slip = self.slip(self.week_of(10, 80))
		self.assertEqual(slip["total_hours"], 50.0)
		self.assertEqual(slip["overtime_hours"], 10.0)
		self.assertEqual(slip["piece_units"], 400.0)
		# v0.298.0. The premium is on the week's regular rate RAISED TO the minimum wage (OAR 839-020-0030(2)(b)):
		# $600 / 50 h = $12 is below $14.70, so 10 h x $14.70 x 0.5 = $73.50, not $60. The makeup fills the rest.
		self.assertEqual(slip["earned_gross"], 673.50)
		self.assertEqual(slip["minimum_wage_makeup"], 135.00)
		self.assertEqual(slip["gross_pay"], 808.50)
		self.assertEqual(slip["minimum_wage_by_state"]["OR"]["minimum_wage_floor"], 808.50)
		self.assertEqual(slip["minimum_wage_by_state"]["OR"]["overtime_hours"], 10.0)
		# And the check on the paid wages finds nothing left to find.
		self.assertTrue(slip["minimum_wage_detail"]["meets_minimum_wage"])
		self.assertEqual(slip["minimum_wage_detail"]["total_shortfall"], 0.0)

	def test_piecework_and_hourly_are_compared_on_the_same_fifty_hours(self):
		"""The brief's third scenario, both ways round, at the same hours.

		At 400 buckets the picker earns $660 and the floor for the week is
		$808.50, so the floor is what they are paid — the same as an hourly worker
		at minimum wage, which is exactly what the higher-of rule promises. At 700
		buckets they earn $1,155 and piecework wins by a distance and is paid in
		full.
		"""
		hourly = calculate_gross_pay("Hourly", OR_FLOOR, 50.0, 10.0, 0)["gross_pay"]
		self.assertEqual(hourly, 808.50)

		lean = calculate_gross_pay("Piece Rate", BUCKET_RATE, 50.0, 10.0, 400, 0.0)["gross_pay"]
		self.assertLess(lean, hourly)
		self.assertEqual(self.slip(self.week_of(10, 80))["gross_pay"], hourly)

		heavy = calculate_gross_pay("Piece Rate", BUCKET_RATE, 50.0, 10.0, 700, 0.0)
		self.assertEqual(heavy["piece_earnings"], 1050.00)
		self.assertEqual(heavy["effective_hourly_rate"], 21.00)
		self.assertEqual(heavy["overtime_pay"], 105.00)
		self.assertEqual(heavy["gross_pay"], 1155.00)
		self.assertGreater(heavy["gross_pay"], hourly)

		paid = self.slip(self.week_of(10, 140))
		self.assertEqual(paid["piece_units"], 700.0)
		self.assertEqual(paid["gross_pay"], 1155.00)
		self.assertEqual(paid["minimum_wage_makeup"], 0.0)

	def test_a_thirty_hour_week_has_no_premium_in_its_floor(self):
		"""Three ten-hour days. Thirty hours at $14.70 is $441 and no hour of it
		is past the threshold, so there is nothing to multiply by one and a half."""
		slip = self.slip(self.week_of(10, 10, days=3))
		self.assertEqual(slip["total_hours"], 30.0)
		self.assertEqual(slip["overtime_hours"], 0.0)
		self.assertEqual(slip["minimum_wage_by_state"]["OR"]["minimum_wage_floor"], 441.00)
		self.assertEqual(slip["gross_pay"], 441.00)  # 30 buckets earned $45

	def test_the_premium_follows_the_hours_into_the_state_they_were_worked_in(self):
		"""Four Oregon tens and one Washington ten: the fortieth hour falls on
		Friday, in Washington, so Washington's floor is the one with the premium
		in it. 40 OR hours at $14.70 is $588; 10 WA hours of which none are
		regular is 10 × $16.66 × 1.5 = $249.90."""
		days = self.week_of(10, 10, days=4)
		days += [
			shift(
				"SHIFT-WA",
				"2025-06-06",
				start="06:00:00",
				end="16:00:00",
				state="WA",
				crew=[member(PICKER, piece_units=10)],
			)
		]
		slip = self.slip(days)
		self.assertEqual(slip["hours_by_state"], {"OR": 40.0, "WA": 10.0})
		self.assertEqual(slip["overtime_hours_by_state"], {"OR": 0.0, "WA": 10.0})
		floors = slip["minimum_wage_by_state"]
		self.assertEqual(floors["OR"]["minimum_wage_floor"], 588.00)
		self.assertEqual(floors["WA"]["minimum_wage_floor"], 249.90)
		self.assertEqual(slip["gross_pay"], 837.90)

	def test_forty_five_then_thirty_five_is_five_of_overtime_across_the_period(self):
		"""Two workweeks are two thresholds. Eighty hours is not the question."""
		week_one = self.week_of(9, 30, days=5, first="2025-06-02")
		week_two = self.week_of(7, 30, days=5, first="2025-06-09")
		for index, row in enumerate(week_two):
			row["name"] = f"SHIFT-W2-{index}"
		agg = self.aggregate(week_one + week_two)[PICKER]
		self.assertEqual(agg["total_hours"], 80.0)
		self.assertEqual(agg["overtime_hours"], 5.0)
		self.assertEqual(len(agg["weeks"]), 2)
		self.assertEqual(agg["weeks"][0]["overtime_hours"], 5.0)
		self.assertEqual(agg["weeks"][1]["overtime_hours"], 0.0)


# ── Claim 3: buckets become piece units ───────────────────────────────


class BucketsBecomePieceUnits(PieceworkTestCase):
	"""Accepted captures on the shipped doctype are the units that make gross."""

	def test_forty_seven_accepted_captures_are_forty_seven_piece_units(self):
		"""One row is one bucket — the shipped doctype carries no count column."""
		self.seed_captures(accepted=47)
		rows, provenance = self._load_piece_rows()
		self.assertEqual(len(rows), 47)
		self.assertEqual(sum(row["units"] for row in rows), 47.0)
		self.assertIn(BUCKET_DOCTYPE, provenance["sources"])

	def test_rejected_captures_are_not_paid(self):
		"""The model saying the bucket was not filled is the model saying so."""
		self.seed_captures(accepted=47, rejected=13)
		rows, _ = self._load_piece_rows()
		self.assertEqual(len(rows), 47)

	def test_the_captures_reach_the_shift_and_become_the_gross(self):
		"""47 captures + an eight-hour shift + $1.50 = a $70.50 slip."""
		self.seed_captures(accepted=47, rejected=13)
		rows, _ = self._load_piece_rows()
		shifts = [
			shift(
				"SHIFT-BUCKETS",
				"2025-06-03",
				start="06:00:00",
				end="14:00:00",
				crew=[member(PICKER)],
			)
		]
		unmatched = self._attach(shifts, rows)
		self.assertEqual(unmatched, [])

		agg = self.aggregate(shifts)[PICKER]
		self.assertEqual(agg["piece_units"], 47.0)
		self.assertEqual(agg["total_hours"], DAY_HOURS)

		gross = calculate_gross_pay(
			"Piece Rate", BUCKET_RATE, agg["total_hours"], 0.0, agg["piece_units"], 0.0
		)
		self.assertEqual(gross["gross_pay"], 70.50)

	def test_a_capture_with_no_employee_resolved_pays_nobody(self):
		"""An unattributed bucket is not silently credited to whoever is nearest."""
		STORE.seed(BUCKET_DOCTYPE, [capture(1, employee=None), capture(2)])
		rows, _ = self._load_piece_rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["employee"], PICKER)

	def test_buckets_on_a_day_with_no_shift_are_still_units(self):
		"""They are paid — with zero hours, which is what the record says."""
		self.seed_captures(accepted=10, day="2025-06-05")
		rows, _ = self._load_piece_rows()
		shifts = [
			shift("SHIFT-OTHER-DAY", "2025-06-03", crew=[member(PICKER)]),
		]
		unmatched = self._attach(shifts, rows)
		self.assertEqual(len(unmatched), 10)

	# ── plumbing ──────────────────────────────────────────────────────

	def _load_piece_rows(self):
		from erpnext_mcp.tools import payroll as payroll_tools

		return payroll_tools._load_piece_rows(MAIN, PERIOD_START, PERIOD_END, None)

	def _attach(self, shifts, rows):
		from erpnext_mcp.tools import payroll as payroll_tools

		return payroll_tools._attach_piece_rows(shifts, rows)


# ── Claim 4: a multi-day pay period ───────────────────────────────────


class MultiDayPayPeriod(PieceworkTestCase):
	"""Ten picking days across two workweeks, aggregated into one slip."""

	def setUp(self):
		super().setUp()
		self.shifts = self._ten_days()

	def _ten_days(self):
		"""Week one: five ten-hour days, 100 buckets each. Week two: five
		six-hour days, 90 buckets each."""
		from datetime import date, timedelta

		rows = []
		monday = date.fromisoformat(PERIOD_START)
		for index in range(5):
			rows.append(
				shift(
					f"SHIFT-A-{index}",
					str(monday + timedelta(days=index)),
					start="06:00:00",
					end="16:00:00",
					crew=[member(PICKER, piece_units=100)],
				)
			)
		for index in range(5):
			rows.append(
				shift(
					f"SHIFT-B-{index}",
					str(monday + timedelta(days=7 + index)),
					start="06:00:00",
					end="12:00:00",
					crew=[member(PICKER, piece_units=90)],
				)
			)
		return rows

	def test_the_hours_are_the_sum_of_the_days(self):
		agg = self.aggregate(self.shifts)[PICKER]
		self.assertEqual(agg["total_hours"], 80.0)
		self.assertEqual(agg["shift_count"], 10)

	def test_the_units_are_the_sum_of_the_days(self):
		agg = self.aggregate(self.shifts)[PICKER]
		self.assertEqual(agg["piece_units"], 950.0)

	def test_the_overtime_is_week_ones_alone(self):
		"""Fifty hours in week one, thirty in week two. Ten of overtime, not zero
		and not the twenty a daily threshold would have found."""
		agg = self.aggregate(self.shifts)[PICKER]
		self.assertEqual(agg["overtime_hours"], 10.0)
		self.assertEqual(agg["weeks"][0]["overtime_hours"], 10.0)
		self.assertEqual(agg["weeks"][1]["overtime_hours"], 0.0)

	def test_the_period_grosses_the_units_plus_the_premium(self):
		"""950 × $1.50 = $1,425 over 80 hours = $17.8125/hr; ten of those at 0.5x."""
		agg = self.aggregate(self.shifts)[PICKER]
		gross = calculate_gross_pay(
			"Piece Rate",
			BUCKET_RATE,
			agg["total_hours"],
			agg["overtime_hours"],
			agg["piece_units"],
			agg["break_hours"],
		)
		self.assertEqual(gross["piece_earnings"], 1425.00)
		self.assertEqual(gross["effective_hourly_rate"], 17.81)
		self.assertEqual(gross["overtime_pay"], 89.06)
		self.assertEqual(gross["gross_pay"], 1514.06)

	def test_the_period_clears_the_floor_the_two_weeks_add_up_to(self):
		"""Seventy straight hours and ten overtime ones: $1,029 + $220.50.

		The floor is a PERIOD figure built from a WEEKLY split — the ten overtime
		hours are week one's, and week two contributes none — which is the same
		distinction the overtime count itself is made of.

		v0.298.0. And the PREMIUM is on week one's own regular rate (OAR 839-020-0030(2)(a): each workweek stands
		alone): 500 buckets x $1.50 / 50 h = $15, so 10 h x $15 x 0.5 = $75 — not the period's blended $17.81.
		"""
		slip = self.slip(self.shifts)
		self.assertEqual(slip["total_hours"], 80.0)
		self.assertEqual(slip["overtime_hours"], 10.0)
		self.assertEqual(slip["gross_pay"], 1500.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)
		self.assertEqual(slip["minimum_wage_by_state"]["OR"]["minimum_wage_floor"], 1249.50)
		self.assertEqual(slip["effective_hourly_rate"], 18.75)
		weeks = slip["regular_rate_by_week"]["weeks"]
		self.assertEqual([w["regular_rate"] for w in weeks], [15.0, 22.5])

	def test_a_day_outside_the_period_is_not_in_the_total(self):
		"""The eleventh day is the next period's, units and all."""
		extra = shift(
			"SHIFT-NEXT-PERIOD",
			"2025-06-16",
			start="06:00:00",
			end="14:00:00",
			crew=[member(PICKER, piece_units=100)],
		)
		agg = self.aggregate([*self.shifts, extra])[PICKER]
		self.assertEqual(agg["piece_units"], 950.0)
		self.assertEqual(agg["total_hours"], 80.0)
		self.assertEqual(len(agg["shifts_outside_period"]), 1)


# ── Claim 5: edge cases ───────────────────────────────────────────────


class PieceworkEdgeCases(PieceworkTestCase):
	"""Zero buckets, a partial shift, an unclosed shift, a paid break."""

	def test_zero_buckets_in_a_full_shift_earns_nothing_and_is_owed_the_whole_floor(self):
		"""Not an error, and not a division by zero. A day owed $117.60 and earning
		nothing — which is a day paid $117.60, as `TheHigherOfRuleIsApplied` walks
		end to end. This is the arithmetic underneath it."""
		gross, verdict = self.piece_day(units=0)
		self.assertEqual(gross["piece_earnings"], 0.0)
		self.assertEqual(gross["gross_pay"], 0.0)
		self.assertEqual(gross["effective_hourly_rate"], 0.0)
		self.assertFalse(verdict["meets_minimum_wage"])
		self.assertEqual(verdict["total_shortfall"], round(OR_FLOOR * DAY_HOURS, 2))

	def test_buckets_with_no_hours_recorded_do_not_divide_by_zero(self):
		"""Somebody's buckets landed on a day whose shift was never opened."""
		gross = calculate_gross_pay("Piece Rate", BUCKET_RATE, 0.0, 0.0, 20, 0.0)
		self.assertEqual(gross["piece_earnings"], 30.00)
		self.assertEqual(gross["gross_pay"], 30.00)
		self.assertEqual(gross["effective_hourly_rate"], 0.0)
		# Zero hours means no floor to clear — there is nothing to divide.
		verdict = pi.check_minimum_wage_by_state({"OR": 0.0}, {"OR": 30.00})
		self.assertTrue(verdict["meets_minimum_wage"])
		self.assertEqual(verdict["by_state"], {})

	def test_a_partial_shift_is_paid_for_the_hours_actually_stood(self):
		"""Ana joined at 10:00 on a 06:00–14:00 shift. Four hours, not eight."""
		shifts = [
			shift(
				"SHIFT-PARTIAL",
				"2025-06-03",
				start="06:00:00",
				end="14:00:00",
				crew=[
					member(PICKER, joined="2025-06-03 10:00:00", piece_units=24),
					member(WORKER, piece_units=47),
				],
			)
		]
		aggregates = self.aggregate(shifts)
		self.assertEqual(aggregates[PICKER]["total_hours"], 4.0)
		self.assertEqual(aggregates[WORKER]["total_hours"], 8.0)

		# Half the day and half the buckets is the SAME effective rate — and the
		# same verdict against the floor. The shortfall is halved, not the rate.
		short_day = pi.check_minimum_wage_by_state({"OR": 4.0}, {"OR": 24 * BUCKET_RATE})
		full_day = pi.check_minimum_wage_by_state({"OR": 8.0}, {"OR": 48 * BUCKET_RATE})
		self.assertEqual(short_day["by_state"]["OR"]["effective_hourly_rate"], 9.00)
		self.assertEqual(full_day["by_state"]["OR"]["effective_hourly_rate"], 9.00)
		self.assertEqual(short_day["total_shortfall"], 22.80)
		self.assertEqual(full_day["total_shortfall"], 45.60)

	def test_an_unclosed_shift_counts_no_hours_and_is_named(self):
		"""Buckets with no span to divide by would read as an infinite rate."""
		shifts = [
			shift(
				"SHIFT-OPEN",
				"2025-06-03",
				start="06:00:00",
				end=None,
				crew=[member(PICKER, piece_units=47)],
			)
		]
		agg = self.aggregate(shifts)[PICKER]
		self.assertEqual(agg["total_hours"], 0.0)
		self.assertEqual(agg["piece_units"], 47.0)
		self.assertEqual(agg["open_shifts"], ["SHIFT-OPEN"])

	def test_an_unpaid_meal_comes_off_the_piece_hours_and_raises_the_effective_rate(self):
		"""A half-hour lunch is not hours worked, so it is not in the divisor."""
		shifts = [
			shift(
				"SHIFT-MEAL",
				"2025-06-03",
				start="06:00:00",
				end="14:30:00",
				crew=[member(PICKER, piece_units=47, unpaid_break_hours=0.5)],
			)
		]
		agg = self.aggregate(shifts)[PICKER]
		self.assertEqual(agg["total_hours"], 8.0)
		self.assertEqual(agg["unpaid_break_hours"], 0.5)

	def test_a_paid_rest_break_is_paid_at_the_average_piece_hourly(self):
		"""WAC 296-131-020. Ten minutes of rest is not ten minutes of nothing."""
		gross = calculate_gross_pay("Piece Rate", BUCKET_RATE, DAY_HOURS, 0.0, 120, 0.5)
		# $180 over 7.5 piece-hours = $24.00/hr; half an hour of rest = $12.00.
		self.assertEqual(gross["piece_earnings"], 180.00)
		self.assertEqual(gross["break_pay"], 12.00)
		self.assertEqual(gross["gross_pay"], 192.00)

	def test_a_break_longer_than_the_shift_cannot_outpay_the_shift(self):
		"""The clock is the harder fact; the break is capped at the hours."""
		shifts = [
			shift(
				"SHIFT-ABSURD",
				"2025-06-03",
				start="06:00:00",
				end="08:00:00",
				crew=[member(PICKER, piece_units=10, break_hours=9)],
			)
		]
		agg = self.aggregate(shifts)[PICKER]
		self.assertEqual(agg["total_hours"], 2.0)
		self.assertEqual(agg["break_hours"], 2.0)

	def test_a_negative_bucket_count_does_not_become_negative_pay_silently(self):
		"""It becomes negative pay LOUDLY: the figure is signed and visible.

		Nothing in the engine rejects a negative count — a correction row is a
		real thing — so this pins what one does rather than claiming it cannot
		happen.
		"""
		gross = calculate_gross_pay("Piece Rate", BUCKET_RATE, DAY_HOURS, 0.0, -10, 0.0)
		self.assertEqual(gross["gross_pay"], -15.00)


# ── Claim 6: the mixed worker, as it stands ───────────────────────────


class MixedPieceworkAndHourlyWork(PieceworkTestCase):
	"""A morning of buckets and an afternoon of irrigation, both paid.

	v0.49.0. `pay_type` was one field on one salary structure, so the afternoon
	was paid at a rate per bucket on a task that produces no buckets. A shift or a
	crew row now carries its own `pay_type` and `pay_rate`; blank means "the way
	this worker's structure says", which is every ordinary day.
	"""

	IRRIGATION_RATE = 16.00

	def mixed_day(self, buckets=90, picking_hours=6, irrigation_hours=2, **irrigation):
		"""One day: `picking_hours` on buckets, then `irrigation_hours` hourly."""
		end = 6 + picking_hours
		return [
			shift(
				"SHIFT-PICKING",
				"2025-06-03",
				start="06:00:00",
				end=f"{end:02d}:00:00",
				crew=[member(PICKER, piece_units=buckets)],
			),
			shift(
				"SHIFT-IRRIGATION",
				"2025-06-03",
				start=f"{end:02d}:00:00",
				end=f"{end + irrigation_hours:02d}:00:00",
				pay_type="Hourly",
				crew=[member(PICKER)],
				**irrigation,
			),
		]

	def test_both_halves_of_the_day_are_paid_and_the_day_totals_one_sixty_seven(self):
		"""THE SCENARIO. 90 buckets × $1.50 = $135, plus 2 × $16.00 = $32."""
		slip = self.slip(self.mixed_day(pay_rate=self.IRRIGATION_RATE))
		self.assertEqual(slip["total_hours"], 8.0)
		self.assertEqual(slip["piece_units"], 90.0)
		self.assertEqual(slip["gross_pay"], 167.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)

		detail = slip["gross_detail"]
		self.assertEqual(detail["piece_earnings"], 135.00)
		self.assertEqual(detail["hourly_earnings"], 32.00)
		self.assertEqual(detail["pay_type"], "Mixed")

	def test_the_rate_can_come_from_the_salary_structure_instead_of_the_shift(self):
		"""`hourly_rate` on the structure is the standing answer to "what is an
		hour of this worker's non-piece time worth". The shift only has to say so
		when the day was priced differently from the standing one."""
		slip = self.slip(self.mixed_day(), hourly_rate=self.IRRIGATION_RATE)
		self.assertEqual(slip["gross_pay"], 167.00)

	def test_the_shifts_own_rate_wins_over_the_structures(self):
		"""A day priced on the day is the most specific record there is."""
		slip = self.slip(self.mixed_day(pay_rate=25.00), hourly_rate=self.IRRIGATION_RATE)
		self.assertEqual(slip["gross_pay"], 135.00 + 2 * 25.00)

	def test_the_floor_is_tested_on_the_whole_day_not_on_the_picking_half(self):
		"""Twenty buckets and two hours of irrigation: $30 + $32 over eight hours
		is $62, and eight Oregon hours are owed $117.60. The makeup covers the
		whole day, because the whole day was worked."""
		slip = self.slip(
			self.mixed_day(buckets=20, pay_rate=self.IRRIGATION_RATE),
		)
		self.assertEqual(slip["total_hours"], 8.0)
		self.assertEqual(slip["earned_gross"], 62.00)
		self.assertEqual(slip["minimum_wage_makeup"], 55.60)
		self.assertEqual(slip["gross_pay"], 117.60)

	def test_a_crew_row_can_name_the_pay_type_for_one_worker_on_a_shared_shift(self):
		"""Half the crew picks and one person runs the water. It is one shift."""
		shifts = [
			shift(
				"SHIFT-BOTH",
				"2025-06-03",
				start="06:00:00",
				end="14:00:00",
				crew=[
					member(WORKER, piece_units=100),
					member(PICKER, pay_type="Hourly", pay_rate=self.IRRIGATION_RATE),
				],
			)
		]
		slips = pi.run_integrated_payroll(
			shifts,
			[
				{"employee": WORKER, "name": "SS-W", "pay_type": "Piece Rate", "base_rate": BUCKET_RATE},
				{"employee": PICKER, "name": "SS-P", "pay_type": "Piece Rate", "base_rate": BUCKET_RATE},
			],
			{},
			{},
			{},
			PERIOD_START,
			PERIOD_END,
			company=MAIN,
			include_unworked=False,
		)
		by_employee = {row["employee"]: row for row in slips}
		self.assertEqual(by_employee[WORKER]["gross_pay"], 150.00)  # 100 buckets
		self.assertEqual(by_employee[PICKER]["gross_pay"], 128.00)  # 8 × $16.00

	def test_the_overtime_premium_is_half_of_one_blended_rate_across_both_kinds(self):
		"""29 CFR 778.115. Four ten-hour picking days and one ten-hour irrigation
		day: 40 hours of buckets at 100 a day and 10 hours of water.

		$600 of buckets plus $160 of irrigation is $760 over fifty hours, so the
		regular rate is $15.20 and the ten overtime hours are owed $7.60 each. NOT
		the piece rate for some of them and the hourly rate for the others.
		"""
		shifts = []
		for index in range(4):
			shifts.append(
				shift(
					f"SHIFT-P-{index}",
					f"2025-06-0{2 + index}",
					start="06:00:00",
					end="16:00:00",
					crew=[member(PICKER, piece_units=100)],
				)
			)
		shifts.append(
			shift(
				"SHIFT-I",
				"2025-06-06",
				start="06:00:00",
				end="16:00:00",
				pay_type="Hourly",
				pay_rate=self.IRRIGATION_RATE,
				crew=[member(PICKER)],
			)
		)
		slip = self.slip(shifts)
		self.assertEqual(slip["total_hours"], 50.0)
		self.assertEqual(slip["overtime_hours"], 10.0)
		self.assertEqual(slip["gross_detail"]["straight_time_pay"], 760.00)
		self.assertEqual(slip["gross_detail"]["effective_hourly_rate"], 15.20)
		self.assertEqual(slip["gross_detail"]["overtime_pay"], 76.00)
		self.assertEqual(slip["earned_gross"], 836.00)
		# And the floor for those fifty hours is $808.50, which this clears.
		self.assertEqual(slip["gross_pay"], 836.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)

	def test_a_day_with_no_pay_type_on_it_is_still_the_structures_own_way(self):
		"""The blank field has to keep meaning what it always meant. Sixteen hours
		of picking under a piece structure with no marker anywhere is the v0.30.0
		arithmetic, unchanged — $45 earned, and the floor on top."""
		shifts = [
			shift(
				"SHIFT-PICKING",
				"2025-06-03",
				start="06:00:00",
				end="14:00:00",
				crew=[member(PICKER, piece_units=30)],
			),
			shift(
				"SHIFT-MORE-PICKING",
				"2025-06-04",
				start="06:00:00",
				end="14:00:00",
				crew=[member(PICKER)],
			),
		]
		slip = self.slip(shifts)
		self.assertEqual(slip["total_hours"], 16.0)
		self.assertEqual(slip["earned_gross"], 45.00)
		self.assertEqual(slip["gross_detail"]["pay_type"], "Piece Rate")
		self.assertEqual(slip["minimum_wage_makeup"], round(OR_FLOOR * 16.0 - 45.00, 2))
		self.assertEqual(slip["gross_pay"], round(OR_FLOOR * 16.0, 2))

	def test_an_irrigation_day_with_no_rate_anywhere_is_carried_by_the_floor(self):
		"""Nobody filled in a rate. Those hours earn nothing AT THE RATE — paying
		them $1.50 an hour because that is the bucket rate would look deliberate —
		and the minimum wage makeup pays for them, loudly."""
		slip = self.slip(self.mixed_day(buckets=90))
		self.assertEqual(slip["earned_gross"], 135.00)
		self.assertEqual(slip["gross_pay"], 135.00)  # above the $117.60 floor
		irrigation_only = self.slip(self.mixed_day(buckets=0))
		self.assertEqual(irrigation_only["earned_gross"], 0.0)
		self.assertEqual(irrigation_only["gross_pay"], 117.60)
		self.assertEqual(irrigation_only["minimum_wage_makeup"], 117.60)

	def test_the_same_worker_on_an_hourly_structure_is_paid_for_every_hour(self):
		"""Same sixteen hours, the other structure. The buckets earn nothing."""
		gross = calculate_gross_pay("Hourly", 18.00, 16.0, 0.0, 30, 0.0)
		self.assertEqual(gross["gross_pay"], 288.00)
		self.assertNotIn("piece_earnings", gross)


# ── Claim 7: the two minimum wage verdicts on one slip ────────────────


class TheTwoMinimumWageVerdicts(PieceworkTestCase):
	"""A cross-state slip is checked against each state's own floor, and paid.

	Thirty Oregon hours and ten Washington ones, 400 buckets at $1.50 — $600 for
	forty hours, a flat $15.00/hr in both states. Oregon's floor is $14.70 and
	Washington's is $16.66, so the same rate clears one and not the other.

	Through v0.48.2 that produced two verdicts that disagreed: the flat check
	asked only the state with the most hours and passed, the per-state check named
	Washington and priced $16.60, and `_slip_row` had to resolve them. v0.49.0
	resolves it earlier and differently — Washington's ten hours are PAID up to
	Washington's floor, so both checks pass and the $16.60 is on the slip as
	makeup rather than as a disagreement.
	"""

	def cross_state_shifts(self):
		"""Three Oregon days and one Washington day, 100 buckets each."""
		rows = []
		for index, (day, state) in enumerate(
			(
				("2025-06-02", "OR"),
				("2025-06-03", "OR"),
				("2025-06-04", "OR"),
				("2025-06-05", "WA"),
			)
		):
			rows.append(
				shift(
					f"SHIFT-XS-{index}",
					day,
					start="06:00:00",
					end="16:00:00",
					state=state,
					crew=[member(PICKER, piece_units=100)],
				)
			)
		return rows

	def cross_state_slip(self):
		return pi.run_integrated_payroll(
			self.cross_state_shifts(),
			[{"employee": PICKER, "name": "SS-P", "pay_type": "Piece Rate", "base_rate": BUCKET_RATE}],
			{},
			{},
			{},
			PERIOD_START,
			PERIOD_END,
			company=MAIN,
			include_unworked=False,
		)[0]

	def test_the_period_is_forty_hours_and_six_hundred_dollars_earned_in_two_states(self):
		slip = self.cross_state_slip()
		self.assertEqual(slip["total_hours"], 40.0)
		self.assertEqual(slip["overtime_hours"], 0.0)
		self.assertEqual(slip["piece_units"], 400.0)
		self.assertEqual(slip["earned_gross"], 600.00)
		self.assertEqual(slip["hours_by_state"], {"OR": 30.0, "WA": 10.0})

	def test_only_washingtons_share_is_topped_up(self):
		"""Oregon's thirty hours earned $450 against a $441 floor and are left
		alone. Washington's ten earned $150 against $166.60 and are not."""
		slip = self.cross_state_slip()
		self.assertEqual(slip["minimum_wage_makeup"], 16.60)
		self.assertEqual(slip["minimum_wage_makeup_by_state"], {"WA": 16.60})
		self.assertEqual(slip["gross_pay"], 616.60)

		floors = slip["minimum_wage_by_state"]
		self.assertEqual(floors["OR"]["minimum_wage_floor"], 441.00)
		self.assertEqual(floors["OR"]["earned_wages"], 450.00)
		self.assertEqual(floors["OR"]["minimum_wage_makeup"], 0.0)
		self.assertEqual(floors["WA"]["minimum_wage_floor"], 166.60)
		self.assertEqual(floors["WA"]["earned_wages"], 150.00)
		self.assertEqual(floors["WA"]["minimum_wage_makeup"], 16.60)

	def test_the_makeup_stays_in_the_state_that_was_short(self):
		"""It is not spread across both by hours proportion. Washington's floor is
		Washington's, and the wages it produces are taxed as Washington's."""
		self.assertEqual(self.cross_state_slip()["state_wages"], {"OR": 450.00, "WA": 166.60})

	def test_the_flat_check_would_have_asked_only_oregon(self):
		"""`check_minimum_wage` tests the whole gross against the state with the
		most hours. Thirty of the forty are Oregon's, so Oregon is the question
		asked, and $15.00 clears $14.70 — which is why it was never the verdict
		to trust on a cross-state slip and is no longer the one stored."""
		slip = self.cross_state_slip()
		self.assertEqual(slip["work_state"], "OR")
		flat = check_minimum_wage(600.00, 40.0, "OR")
		self.assertTrue(flat["meets_minimum_wage"])
		self.assertEqual(flat["minimum_wage_floor"], 588.00)
		self.assertEqual(flat["effective_hourly_rate"], 15.00)

	def test_the_per_state_check_passes_now_that_washington_was_paid_its_floor(self):
		"""The check runs on the wages as PAID. $166.60 over ten hours is $16.66."""
		detail = self.cross_state_slip()["minimum_wage_detail"]
		self.assertTrue(detail["meets_minimum_wage"])
		self.assertEqual(detail["states_below_minimum"], [])
		self.assertEqual(detail["total_shortfall"], 0.0)
		self.assertEqual(detail["total_makeup"], 16.60)
		self.assertEqual(detail["by_state"]["WA"]["effective_hourly_rate"], WA_FLOOR)
		self.assertEqual(detail["by_state"]["WA"]["minimum_wage_makeup"], 16.60)

	def test_the_stored_row_agrees_with_both_checks_and_carries_the_makeup(self):
		"""There is no disagreement left for `_slip_row` to resolve — and the
		figure that used to be the only sign of the problem is a column."""
		from erpnext_mcp.tools import payroll as payroll_tools

		slip = self.cross_state_slip()
		self.assertTrue(slip["minimum_wage_check"])
		row = payroll_tools._slip_row(slip)
		self.assertEqual(row["minimum_wage_check"], 1)
		self.assertEqual(row["gross_pay"], 616.60)
		self.assertEqual(row["earned_gross"], 600.00)
		self.assertEqual(row["minimum_wage_makeup"], 16.60)

	def test_the_run_summary_names_washington_as_the_state_that_was_topped_up(self):
		summary = pi.summarize_payroll_run([self.cross_state_slip()])
		self.assertEqual(summary["below_minimum_wage"], [])
		self.assertEqual(len(summary["topped_up_to_minimum_wage"]), 1)
		self.assertEqual(summary["topped_up_to_minimum_wage"][0]["states"], ["WA"])
		self.assertEqual(summary["total_minimum_wage_makeup"], 16.60)


# ── Claim 7b: the mixed day off the real doctypes ─────────────────────


class TheMixedDayThroughTheTools(PieceworkTestCase):
	"""The same day, seeded on the site and read the way a payroll run reads it.

	`MixedPieceworkAndHourlyWork` hands `run_integrated_payroll` shift dicts built
	by hand, which tests the arithmetic and nothing about whether the columns
	exist. This one seeds Farm Shift rows with `pay_type` and `pay_rate` on them
	and goes through `preview_payroll_for_period` — the loader, the doctype
	fields, the salary structure, the engine and the tool's own view of a slip.
	"""

	def setUp(self):
		super().setUp()
		# Farm Shift Crew Member records who and when, not how much — a per-worker
		# bucket count is a column a site adds. Added here so these tests can put
		# the units on the crew row instead of standing the bucket bridge up, the
		# same way `test_payroll_integration` does for the same reason.
		add_field("Farm Shift Crew Member", "piece_units", "Float")

	def preview_slip(self, employee=PICKER):
		data = self.tool_data(
			"preview_payroll_for_period",
			{
				"company": MAIN,
				"pay_period_start": PERIOD_START,
				"pay_period_end": PERIOD_END,
			},
		)
		slip = next(row for row in data["slips"] if row["employee"] == employee)
		return slip, data

	def test_the_shipped_doctypes_carry_the_columns_this_needs(self):
		"""Asserted rather than assumed. A field that is only in a fixture is a
		field a real site does not have, and the whole feature is a column."""
		from erpnext_mcp import compat

		self.assertTrue(compat.has_field("Farm Shift", "pay_type"))
		self.assertTrue(compat.has_field("Farm Shift", "pay_rate"))
		self.assertTrue(compat.has_field("Farm Shift Crew Member", "pay_type"))
		self.assertTrue(compat.has_field("Farm Shift Crew Member", "pay_rate"))
		self.assertTrue(compat.has_field("Farm Salary Structure", "hourly_rate"))
		self.assertTrue(compat.has_field("Farm Payroll Slip", "minimum_wage_makeup"))
		self.assertTrue(compat.has_field("Farm Payroll Slip", "earned_gross"))

	def test_a_piece_morning_and_an_hourly_afternoon_seeded_on_the_site(self):
		"""90 buckets in six hours, then two hours of irrigation at $16.00."""
		self.structure(PICKER, pay_type="Piece Rate", rate=BUCKET_RATE)
		self.seed_shifts(
			shift(
				"SHIFT-PICK",
				"2025-06-03",
				start="06:00:00",
				end="12:00:00",
				crew=[member(PICKER, piece_units=90)],
			),
			shift(
				"SHIFT-WATER",
				"2025-06-03",
				start="12:00:00",
				end="14:00:00",
				pay_type="Hourly",
				pay_rate=16.00,
				crew=[member(PICKER)],
			),
		)
		slip, _ = self.preview_slip()
		self.assertEqual(slip["total_hours"], 8.0)
		self.assertEqual(slip["piece_units"], 90.0)
		self.assertEqual(slip["gross_pay"], 167.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)

	def test_the_structures_hourly_rate_is_read_off_the_record(self):
		"""`create_salary_structure` takes it, the loader reads it back, and the
		engine pays the irrigation hours with it."""
		created = self.tool_data(
			"create_salary_structure",
			{
				"employee": PICKER,
				"company": MAIN,
				"pay_type": "Piece Rate",
				"base_rate": BUCKET_RATE,
				"hourly_rate": 16.00,
				"effective_from": "2025-01-01",
			},
		)
		self.assertEqual(created["hourly_rate"], 16.00)
		self.seed_shifts(
			shift(
				"SHIFT-PICK",
				"2025-06-03",
				start="06:00:00",
				end="12:00:00",
				crew=[member(PICKER, piece_units=90)],
			),
			shift(
				"SHIFT-WATER",
				"2025-06-03",
				start="12:00:00",
				end="14:00:00",
				pay_type="Hourly",
				crew=[member(PICKER)],
			),
		)
		slip, _ = self.preview_slip()
		self.assertEqual(slip["gross_pay"], 167.00)

	def test_the_makeup_reaches_the_stored_run_as_a_column(self):
		"""47 buckets, eight hours, written down. `run_payroll_for_period` is the
		tool that persists a slip, and the two figures have to survive it."""
		self.structure(PICKER, pay_type="Piece Rate", rate=BUCKET_RATE)
		self.seed_shifts(
			shift(
				"SHIFT-47",
				"2025-06-03",
				start="06:00:00",
				end="14:00:00",
				crew=[member(PICKER, piece_units=47)],
			),
		)
		data = self.tool_data(
			"run_payroll_for_period",
			{
				"company": MAIN,
				"pay_period_start": PERIOD_START,
				"pay_period_end": PERIOD_END,
			},
		)
		entry = self.tool_data("get_payroll_entry", {"name": data["name"]})
		row = next(r for r in entry["slips"] if r["employee"] == PICKER)
		self.assertEqual(row["gross_pay"], 117.60)
		self.assertEqual(row["earned_gross"], 70.50)
		self.assertEqual(row["minimum_wage_makeup"], 47.10)
		self.assertEqual(row["minimum_wage_check"], 1)

	def test_a_run_with_no_pay_type_anywhere_is_the_run_it_always_was(self):
		"""The blank column changes nothing. Eight hours of picking that clears
		the floor grosses the piece total and nothing else happens."""
		self.structure(PICKER, pay_type="Piece Rate", rate=BUCKET_RATE)
		self.seed_shifts(
			shift(
				"SHIFT-PICK",
				"2025-06-03",
				start="06:00:00",
				end="14:00:00",
				crew=[member(PICKER, piece_units=120)],
			),
		)
		slip, _ = self.preview_slip()
		self.assertEqual(slip["gross_pay"], 180.00)
		self.assertEqual(slip["earned_gross"], 180.00)
		self.assertEqual(slip["minimum_wage_makeup"], 0.0)


# ── Claim 8: the bucket-to-payroll reconciliation ─────────────────────


class TheBucketReconciliation(PieceworkTestCase):
	"""`get_piecework_summary` and `reconcile_bucket_payroll`, which shipped in
	v0.44.0 with a registration test and nothing else."""

	FROM = "2025-06-01"
	TO = "2025-06-30"

	def summary(self, employee=PICKER, **extra):
		return self.tool_data(
			"get_piecework_summary",
			{
				"employee": employee,
				"company": MAIN,
				"from_date": self.FROM,
				"to_date": self.TO,
				**extra,
			},
		)

	def reconcile(self, **extra):
		return self.tool_data(
			"reconcile_bucket_payroll",
			{"company": MAIN, "from_date": self.FROM, "to_date": self.TO, **extra},
		)

	# ── get_piecework_summary ─────────────────────────────────────────

	def test_the_summary_counts_accepted_buckets_as_the_piece_units(self):
		"""47 accepted and 13 rejected is 47 units and a 78% acceptance rate."""
		self.seed_captures(accepted=47, rejected=13)
		data = self.summary()
		self.assertEqual(data["total_accepted"], 47)
		self.assertEqual(data["total_rejected"], 13)
		self.assertEqual(data["piece_units"], 47)
		self.assertAlmostEqual(data["acceptance_rate"], 47 / 60, places=4)

	def test_the_summary_is_scoped_to_the_employee_asked_about(self):
		self.seed_captures(accepted=47, employee=PICKER)
		STORE.seed(
			BUCKET_DOCTYPE,
			[capture(2000 + i, employee=WORKER) for i in range(5)],
		)
		self.assertEqual(self.summary(PICKER)["total_accepted"], 47)
		self.assertEqual(self.summary(WORKER)["total_accepted"], 5)

	def test_the_summary_is_scoped_to_the_dates_asked_about(self):
		"""A bucket outside the window is another period's piece unit."""
		self.seed_captures(accepted=10, day="2025-06-03")
		STORE.seed(
			BUCKET_DOCTYPE,
			[capture(3000 + i, day="2025-07-03") for i in range(4)],
		)
		self.assertEqual(self.summary()["total_accepted"], 10)
		self.assertEqual(self.summary(to_date="2025-07-31")["total_accepted"], 14)

	def test_the_summary_counts_the_sessions_the_buckets_came_from(self):
		self.seed_captures(accepted=10, day="2025-06-03")
		STORE.seed(
			BUCKET_DOCTYPE,
			[capture(4000 + i, day="2025-06-04") for i in range(6)],
		)
		data = self.summary()
		self.assertEqual(data["session_count"], 2)
		self.assertEqual(data["total_accepted"], 16)

	def test_a_worker_with_no_buckets_gets_zeroes_rather_than_an_error(self):
		data = self.summary()
		self.assertEqual(data["total_accepted"], 0)
		self.assertEqual(data["piece_units"], 0)
		self.assertEqual(data["acceptance_rate"], 0.0)
		self.assertEqual(data["session_count"], 0)

	def test_backwards_dates_are_refused(self):
		message = self.tool_error(
			"get_piecework_summary",
			{"employee": PICKER, "company": MAIN, "from_date": self.TO, "to_date": self.FROM},
		)
		self.assertIn("after", message)

	def test_the_summary_is_a_read_and_is_on_by_default(self):
		"""Both of these are reads, and reads ship on. Asserted rather than
		assumed, because "the payroll figure could not be checked" is a bad
		default and this is the file that would notice it changing."""
		self.configure(enabled=1)  # nothing but the kill switch — shipped defaults
		self.seed_captures(accepted=3)
		data = self.summary()
		self.assertEqual(data["total_accepted"], 3)

	def test_the_kill_switch_stops_the_summary(self):
		"""Disabled is 404 at the transport, not a tool error inside a 200."""
		self.configure(enabled=0, **BUCKET_ON)
		_body, status = self.call(
			"tools/call",
			{
				"name": "get_piecework_summary",
				"arguments": {
					"employee": PICKER,
					"company": MAIN,
					"from_date": self.FROM,
					"to_date": self.TO,
				},
			},
		)
		self.assertEqual(status, 404)

	def test_the_summary_is_refused_when_its_own_switch_is_turned_off(self):
		self.configure(enabled=1, allow_get_piecework_summary=0)
		message = self.tool_error(
			"get_piecework_summary",
			{"employee": PICKER, "company": MAIN, "from_date": self.FROM, "to_date": self.TO},
		)
		self.assertIn("get_piecework_summary", message)

	# ── reconcile_bucket_payroll ──────────────────────────────────────

	def test_buckets_with_no_payroll_yet_are_reported_as_not_yet_paid(self):
		"""Not an error — a bucket nobody has run payroll for is simply unpaid."""
		self.seed_captures(accepted=47)
		data = self.reconcile()
		row = self._row(data, PICKER)
		self.assertEqual(row["accepted_bucket_entries"], 47)
		self.assertEqual(row["payroll_piece_units"], 0.0)
		self.assertEqual(row["discrepancy"], 47)
		self.assertEqual(row["status"], "bucket_units_not_yet_paid")
		self.assertEqual(data["unpaid_pending_bucket_entries"], 47)

	def test_a_slip_that_paid_the_same_count_matches(self):
		self.seed_captures(accepted=47)
		self.seed_payroll_run({PICKER: 47})
		row = self._row(self.reconcile(), PICKER)
		self.assertEqual(row["status"], "matches")
		self.assertEqual(row["discrepancy"], 0)
		self.assertEqual(self.reconcile()["mismatch_count"], 0)

	def test_a_slip_that_paid_more_units_than_the_bucket_log_holds_is_flagged(self):
		"""The direction that matters most — payroll ahead of the evidence."""
		self.seed_captures(accepted=47)
		self.seed_payroll_run({PICKER: 60})
		row = self._row(self.reconcile(), PICKER)
		self.assertEqual(row["status"], "payroll_units_exceed_bucket_log")
		self.assertEqual(row["discrepancy"], -13)

	def test_a_slip_that_paid_fewer_units_than_the_bucket_log_holds_is_flagged(self):
		self.seed_captures(accepted=47)
		self.seed_payroll_run({PICKER: 40})
		row = self._row(self.reconcile(), PICKER)
		self.assertEqual(row["status"], "bucket_units_not_yet_paid")
		self.assertEqual(row["discrepancy"], 7)

	def test_rejected_captures_are_not_expected_to_have_been_paid(self):
		"""Only Accepted entries are the yardstick — a rejected one is not a bucket."""
		self.seed_captures(accepted=47, rejected=13)
		self.seed_payroll_run({PICKER: 47})
		self.assertEqual(self.reconcile()["mismatch_count"], 0)

	def test_a_bucket_with_no_employee_is_counted_as_unattributed_not_dropped(self):
		STORE.seed(BUCKET_DOCTYPE, [capture(1, employee=None), capture(2, employee=None), capture(3)])
		data = self.reconcile()
		self.assertEqual(data["unattributed_bucket_entries"], 2)
		self.assertEqual(self._row(data, PICKER)["accepted_bucket_entries"], 1)

	def test_a_cancelled_payroll_run_does_not_count_as_having_paid_anything(self):
		self.seed_captures(accepted=47)
		self.seed_payroll_run({PICKER: 47}, status="Cancelled")
		row = self._row(self.reconcile(), PICKER)
		self.assertEqual(row["payroll_piece_units"], 0.0)
		self.assertEqual(row["status"], "bucket_units_not_yet_paid")

	def test_the_reconciliation_can_be_scoped_to_one_employee(self):
		self.seed_captures(accepted=47, employee=PICKER)
		STORE.seed(
			BUCKET_DOCTYPE,
			[capture(5000 + i, employee=WORKER) for i in range(9)],
		)
		data = self.reconcile(employee=WORKER)
		self.assertEqual([row["employee"] for row in data["employees"]], [WORKER])
		self.assertEqual(data["employees"][0]["accepted_bucket_entries"], 9)

	def test_backwards_dates_are_refused_before_anything_is_compared(self):
		message = self.tool_error(
			"reconcile_bucket_payroll",
			{"company": MAIN, "from_date": self.TO, "to_date": self.FROM},
		)
		self.assertIn("after", message)

	def test_the_reconciliation_is_refused_when_its_own_switch_is_turned_off(self):
		self.configure(enabled=1, allow_reconcile_bucket_payroll=0)
		message = self.tool_error(
			"reconcile_bucket_payroll",
			{"company": MAIN, "from_date": self.FROM, "to_date": self.TO},
		)
		self.assertIn("reconcile_bucket_payroll", message)

	def test_the_kill_switch_stops_the_reconciliation(self):
		self.configure(enabled=0, **BUCKET_ON)
		_body, status = self.call(
			"tools/call",
			{
				"name": "reconcile_bucket_payroll",
				"arguments": {"company": MAIN, "from_date": self.FROM, "to_date": self.TO},
			},
		)
		self.assertEqual(status, 404)

	# ── plumbing ──────────────────────────────────────────────────────

	def seed_payroll_run(self, units_by_employee, status="Calculated", name="PAY-2025-00001"):
		"""A Farm Payroll Entry whose slips carry the piece units given."""
		slips = []
		for index, (employee, units) in enumerate(sorted(units_by_employee.items()), start=1):
			slips.append(
				{
					"name": f"{name}-slip-{index}",
					"parent": name,
					"parenttype": "Farm Payroll Entry",
					"parentfield": "slips",
					"employee": employee,
					"pay_type": "Piece Rate",
					"piece_units": units,
					"piece_rate": BUCKET_RATE,
					"gross_pay": round(units * BUCKET_RATE, 2),
					"minimum_wage_check": 0,
				}
			)
		# BOTH, deliberately. The tool reads the parent to find the period and
		# `frappe.db.get_all("Farm Payroll Slip", ...)` to find the units, and a
		# fixture that only nested them would test neither query honestly.
		STORE.seed("Farm Payroll Slip", slips)
		STORE.seed(
			"Farm Payroll Entry",
			[
				{
					"name": name,
					"company": MAIN,
					"pay_period_start": self.FROM,
					"pay_period_end": self.TO,
					"pay_frequency": "Biweekly",
					"status": status,
					"employee_count": len(slips),
					"slips": slips,
					"gl_postings": [],
				}
			],
		)
		return name

	def _row(self, data, employee):
		for row in data["employees"]:
			if row["employee"] == employee:
				return row
		self.fail(f"{employee} not in the reconciliation: {data['employees']}")
