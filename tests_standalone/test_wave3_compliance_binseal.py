# SPDX-License-Identifier: MIT
"""Wave 3 — minors, heat breaks as evidence, cohort training, and bin sealing.

Four items out of `fafo_ios/SERVER_CHANGES.md`, and unlike wave 1 they are not
four instances of one failure. Three of them are places where the SERVER knew
something and threw it away at the moment it mattered; the fourth is a register
that did not exist at all.

FIVE CLAIMS.

1. `WhoIsUnderEighteen` — item 15. `is_minor` is DERIVED from `date_of_birth` on
   every read and stored nowhere, it is THREE-VALUED, and the two age bands have
   different ceilings. The negative controls are the load-bearing tests here: an
   adult is unaffected by every one of these rules, and a worker with no date of
   birth on file reads as `None` rather than as an adult — which is the failure a
   boolean column would have shipped.

2. `WhatAMinorMayNotDo` — item 15 (c) to (f). The hour and clock ceilings refuse
   at `add_worker_to_shift` and merely REPORT at `start_shift`; the prohibited
   task types refuse at `assign_farm_task`; and the weekly ceiling raises a
   compliance alert before the week ends rather than after.

3. `AHeatBreakCarriesTheHeat` — item 17. A break logged as Cool-Down, Water Break
   or Shade Break carries the shift's peak temperature, its peak heat index, the
   moment the threshold was crossed and the provenance of the reading. A Paid
   Rest carries none of it, which is the negative control that proves the stamp
   is keyed on the KIND rather than on the presence of a timeline.

4. `OneAfternoonNotElevenCards` — item 4. Several people lapsing on one
   `group_training` curriculum become ONE Training Session with all of them as
   attendees. One person does not. A curriculum that is not ticked does not.
   Running the sweep twice raises nothing the second time.

5. `TheBinTracesBackToTheCrew` — item 23. The whole register: sealing, the
   retry that must not double a count, the two counts that are allowed to
   disagree, and `trace_bin` — including the reused tag, which is the case a
   uniqueness constraint would have got wrong in the expensive direction.
"""

import datetime as _dt

import frappe

from erpnext_mcp import breaks as breaks_mod
from erpnext_mcp import minors, roles, shifts
from erpnext_mcp.alerts import rules as shipped_rules
from erpnext_mcp.api import rectify
from erpnext_mcp.tools import binseals
from erpnext_mcp.tools import shifts as shift_tools

from .fixtures import MAIN, V12TestCase, install_hrms
from .harness import ROLES, STORE

FOREMAN = "HR-EMP-00001"
WORKER = "HR-EMP-00002"
MINOR = "HR-EMP-00090"
YOUNGER = "HR-EMP-00091"

ON = {
	f"allow_{name}": 1
	for name in (
		"start_shift",
		"add_worker_to_shift",
		"remove_worker_from_shift",
		"end_shift",
		"log_shift_event",
		"log_shift_break",
		"get_break_policy",
		"get_shift",
		"list_shifts",
		"list_employees",
		"get_employee",
		"create_farm_task",
		"assign_farm_task",
		"generate_tasks_from_compliance_alerts",
		"materialize_task_for_alert",
		"list_training_sessions",
		"get_training_session",
		"seal_bin",
		"get_bin_seal",
		"list_bin_seals",
		"trace_bin",
	)
}


def _years_ago(years: int, days: int = 0) -> str:
	"""A date of birth that makes somebody exactly `years` old today, less `days`.

	Computed from today rather than written as a literal, because a literal date
	of birth ages one year every twelve months and the test that asserts "this
	person is fifteen" would start asserting sixteen without anybody editing it.
	"""
	today = _dt.date.fromisoformat(frappe.utils.today())
	try:
		born = today.replace(year=today.year - years)
	except ValueError:  # 29 February
		born = today.replace(year=today.year - years, day=28)
	return (born - _dt.timedelta(days=days)).isoformat()


class Wave3TestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		roles.install_roles()
		STORE.seed(
			"Employee",
			[
				{
					"name": MINOR,
					"employee_name": "Mateo Seventeen",
					"employee_number": "E-190",
					"status": "Active",
					"company": MAIN,
					"date_of_joining": "2026-06-01",
					"date_of_birth": _years_ago(17),
				},
				{
					"name": YOUNGER,
					"employee_name": "Nina Fifteen",
					"employee_number": "E-191",
					"status": "Active",
					"company": MAIN,
					"date_of_joining": "2026-06-01",
					"date_of_birth": _years_ago(15),
				},
			],
		)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	# -- furniture -----------------------------------------------------------
	def a_policy(self, with_minor_rows: bool = True, **overrides):
		row = {
			"name": "LBP-OR-2026",
			"policy_id": "LBP-OR-2026",
			"work_state": "OR",
			"enabled": 1,
			"effective_from": "2026-01-01",
			"human_approved_by": "ada@example.test",
			"regulation_citations": "OAR 437-004-1131",
			"max_hours_without_rest": 4,
			"rest_schedule": [
				{"hours_from": 4, "hours_to": 12, "periods_owed": 1, "minutes_each": 10, "paid": 1}
			],
			"meal_schedule": [
				{"hours_from": 6, "hours_to": 12, "periods_owed": 1, "minutes_each": 30, "paid": 0}
			],
			"heat_schedule": [
				{
					"heat_index_from": 90,
					"heat_index_to": 200,
					"minutes_each": 10,
					"every_hours": 2,
					"concurrent_with_rest": 0,
				}
			],
		}
		if with_minor_rows:
			row["minor_rest_schedule"] = [dict(entry) for entry in minors.MINOR_REST_SCHEDULE]
			row["minor_meal_schedule"] = [dict(entry) for entry in minors.MINOR_MEAL_SCHEDULE]
		row.update(overrides)
		STORE.seed("Labor Break Policy", [row])
		return row["name"]

	def a_shift(self, **overrides):
		payload = {
			"foreman": FOREMAN,
			"location": "Block 7 North",
			"shift_type": "Harvest",
			"start_datetime": f"{frappe.utils.today()} 08:00:00",
			"crew_employees": [WORKER],
		}
		payload.update(overrides)
		return self.tool_data("start_shift", payload)

	def at(self, hour: int, minute: int = 0, day: str = "") -> str:
		return f"{day or frappe.utils.today()} {hour:02d}:{minute:02d}:00"


# ── 1. item 15: who is under eighteen ───────────────────────────────────────
class WhoIsUnderEighteen(Wave3TestCase):
	"""`is_minor` is derived on every read, three-valued, and banded.

	THE FLAG IS NOT STORED AND THERE IS A TEST FOR IT. A fifteen-year-old hired
	in April is sixteen in July, and a column somebody ticked at the hire is
	wrong in the direction that permits more work — which is the only direction
	that matters here.
	"""

	def test_age_is_completed_years_and_a_birthday_today_counts(self):
		today = frappe.utils.today()
		self.assertEqual(minors.age_on(_years_ago(17), today), 17)
		# One day before the eighteenth birthday is still seventeen.
		self.assertEqual(minors.age_on(_years_ago(18, days=-1), today), 17)
		self.assertEqual(minors.age_on(_years_ago(18), today), 18)

	def test_a_leap_day_birth_is_not_divided_by_three_six_five_point_two_five(self):
		"""The arithmetic that is wrong one day in four, and wrong on the birthday.

		29 February 2008 to 28 February 2026 is seventeen years and 364 days;
		days/365.25 rounds it to 17, which happens to agree here — so the case
		that matters is the day AFTER, where the naive figure still reads 17 and
		the (month, day) comparison reads 18.
		"""
		self.assertEqual(minors.age_on("2008-02-29", "2026-02-28"), 17)
		self.assertEqual(minors.age_on("2008-02-29", "2026-03-01"), 18)

	def test_the_two_bands_are_not_one_category(self):
		today = frappe.utils.today()
		self.assertEqual(minors.band(_years_ago(15), today), minors.BAND_UNDER_16)
		self.assertEqual(minors.band(_years_ago(16), today), minors.BAND_16_17)
		self.assertEqual(minors.band(_years_ago(17), today), minors.BAND_16_17)
		self.assertEqual(minors.band(_years_ago(18), today), "")

	def test_the_ceilings_differ_between_the_bands(self):
		"""OREGON'S FIGURES, NOW ASKED FOR BY NAME. This asserted `minors.LIMITS`
		until v0.106.0, when that constant stopped being Oregon's table and became
		the strictest-across-states one — see `test_an_unrecorded_state_gets_the_
		strictest_of_both` below. The four Oregon numbers are unchanged."""
		under = minors.LIMITS_BY_STATE["OR"][minors.BAND_UNDER_16]
		older = minors.LIMITS_BY_STATE["OR"][minors.BAND_16_17]
		self.assertEqual((under["daily_hours"], under["weekly_hours"]), (8.0, 40.0))
		self.assertEqual((older["daily_hours"], older["weekly_hours"]), (10.0, 60.0))
		# The clock applies to the younger band alone IN OREGON, which is FLSA and
		# not an oversight — a seventeen-year-old may lawfully pick at five in the
		# morning there. Washington disagrees, which is the next test.
		self.assertTrue(under["earliest"] and under["latest"])
		self.assertFalse(older["earliest"] or older["latest"])

	def test_washington_binds_tighter_on_the_older_bands_week(self):
		"""S5. THE FIGURE A STATE-BLIND TABLE GOT WRONG. WAC 296-131-120 caps a
		16- or 17-year-old at fifty hours in a non-school week; Oregon allows
		sixty. An app carrying Oregon's number into Washington reports a lawful
		roster for a week that is ten hours over."""
		older = minors.LIMITS_BY_STATE["WA"][minors.BAND_16_17]
		self.assertEqual(older["weekly_hours"], 50.0)
		self.assertEqual(minors.LIMITS_BY_STATE["OR"][minors.BAND_16_17]["weekly_hours"], 60.0)
		# And Washington DOES put a clock on that band, where Oregon does not.
		self.assertEqual((older["earliest"], older["latest"]), ("05:00", "22:00"))

	def test_the_same_week_is_lawful_in_one_state_and_not_the_other(self):
		"""The two tables, exercised through the function that refuses."""
		fifty_five = (minors.BAND_16_17, 9.0, 55.0)
		self.assertIsNone(minors.hours_violation(*fifty_five, "OR"))
		self.assertIn("50-hour", minors.hours_violation(*fifty_five, "WA") or "")

	def test_washingtons_younger_band_may_start_at_five(self):
		"""And Oregon's may not. The clock differs by two hours at the front."""
		six_am = (minors.BAND_UNDER_16, "2026-08-20 06:00:00")
		self.assertIsNone(minors.time_of_day_violation(*six_am, work_state="WA"))
		self.assertIn("before 07:00", minors.time_of_day_violation(*six_am, work_state="OR") or "")

	def test_an_unrecorded_state_gets_the_strictest_of_both(self):
		"""THE SAFE DIRECTION, and the same argument the module makes about a
		missing date of birth: "we do not know" and "they may work" are different
		answers. Defaulting to Oregon would clear a Washington seventeen-year-old
		onto a 60-hour week that state does not allow."""
		older = minors.LIMITS[minors.BAND_16_17]
		self.assertEqual(older["weekly_hours"], 50.0)  # Washington's, the tighter
		self.assertEqual(older["latest"], "22:00")
		under = minors.LIMITS[minors.BAND_UNDER_16]
		self.assertEqual((under["earliest"], under["latest"]), ("07:00", "19:00"))  # Oregon's
		# The narrowest window any state allows — the INTERSECTION, not the union.
		self.assertEqual(under["daily_hours"], 8.0)

	def test_the_strictest_table_keeps_every_states_citation_in_full(self):
		"""It enforces Oregon's 07:00 and Washington's fifty in one row, so no
		single citation is the whole answer. 29 CFR 570.35 is the authority for
		that 07:00 and was being trimmed off at the semicolon."""
		citation = minors.LIMITS[minors.BAND_UNDER_16]["citation"]
		self.assertIn("29 CFR 570.35", citation)
		self.assertIn("WAC 296-131-120", citation)

	def test_an_unknown_state_is_answered_and_never_raised(self):
		"""This is read on the refusal path. A compliance check that threw because
		a column held something unexpected would fail open at the worst moment."""
		self.assertEqual(
			minors.limits_for_band(minors.BAND_16_17, "CA"),
			minors.limits_for_band(minors.BAND_16_17, ""),
		)

	def test_the_state_note_names_the_column_and_only_when_it_is_missing(self):
		self.assertEqual(minors.state_note("OR"), "")
		self.assertEqual(minors.state_note("WA"), "")
		self.assertIn("work_state", minors.state_note(""))

	def test_the_six_day_week_is_washingtons_and_is_never_a_refusal(self):
		"""WAC 296-131-120(4) excepts dairy, livestock, hay harvest and
		irrigation-dependent crop work, and this app cannot tell which a shift is
		— so a hard block would be a false refusal on exactly those operations.
		Oregon carries no figure here; the module says why."""
		self.assertIsNone(minors.days_warning(minors.BAND_16_17, 7, "OR"))
		warned = minors.days_warning(minors.BAND_16_17, 7, "WA")
		self.assertIn("6-day", warned or "")
		self.assertIn("excepted", warned or "")
		self.assertIsNone(minors.days_warning(minors.BAND_16_17, 6, "WA"))

	def test_an_unknown_date_of_birth_is_none_and_not_false(self):
		"""THE FAILURE A BOOLEAN COLUMN WOULD HAVE SHIPPED.

		"we do not know" and "they are an adult" are different answers, and a
		module that collapsed them would clear a minor onto a ten-hour shift
		because a column was empty.
		"""
		self.assertIsNone(minors.is_minor(None, frappe.utils.today()))
		self.assertIsNone(minors.is_minor("", frappe.utils.today()))
		described = minors.describe("", frappe.utils.today())
		self.assertIsNone(described["is_minor"])
		self.assertFalse(described["date_of_birth_recorded"])

	def test_get_employee_derives_it_rather_than_reading_a_column(self):
		data = self.tool_data("get_employee", {"employee": MINOR})
		self.assertTrue(data["is_minor"])
		self.assertEqual(data["age"], 17)
		self.assertEqual(data["minor_band"], minors.BAND_16_17)
		self.assertEqual(data["minor_limits"]["daily_hours"], 10.0)

	def test_the_negative_control_an_adult_reads_false_with_no_band(self):
		data = self.tool_data("get_employee", {"employee": WORKER})
		self.assertIsNone(data["is_minor"])
		self.assertIsNone(data["minor_band"])
		frappe.db.set_value("Employee", WORKER, "date_of_birth", _years_ago(41))
		data = self.tool_data("get_employee", {"employee": WORKER})
		self.assertFalse(data["is_minor"])
		self.assertIsNone(data["minor_band"])

	def test_there_is_no_stored_column_to_read(self):
		"""The claim itself, asserted against the schema rather than the answer.

		A later release that "optimised" this into a stored flag would pass every
		test above on the day it shipped and start lying on the first birthday.
		"""
		from erpnext_mcp import compat

		self.assertFalse(compat.has_field("Employee", "is_minor"))

	def test_list_employees_carries_it_and_counts_them(self):
		data = self.tool_data("list_employees", {"company": MAIN})
		by_name = {row["name"]: row for row in data["employees"]}
		self.assertTrue(by_name[MINOR]["is_minor"])
		self.assertTrue(by_name[YOUNGER]["is_minor"])
		self.assertEqual(by_name[YOUNGER]["minor_band"], minors.BAND_UNDER_16)
		self.assertEqual(data["minors"], 2)

	def test_the_crew_roster_carries_it_so_the_purple_badge_has_something_to_read(self):
		data = self.a_shift(crew_employees=[WORKER, MINOR])
		by_employee = {row["employee"]: row for row in data["crew"]}
		self.assertTrue(by_employee[MINOR]["is_minor"])
		self.assertEqual(by_employee[MINOR]["minor_band"], minors.BAND_16_17)
		self.assertIsNone(by_employee[WORKER]["is_minor"])
		self.assertEqual(data["minors_on_crew"], 1)

	def test_the_roster_answers_as_of_the_shifts_own_day_not_today(self):
		"""A shift read in November is read about the afternoon it happened.

		Somebody who turned eighteen since is not retroactively made an adult on
		last season's crew — `minor_flags` takes the shift's own start.
		"""
		born = frappe.db.get_value("Employee", MINOR, "date_of_birth")
		eighteenth = _dt.date.fromisoformat(str(born)).replace(
			year=_dt.date.fromisoformat(str(born)).year + 18
		)
		day_before = (eighteenth - _dt.timedelta(days=1)).isoformat()
		flags = shifts.minor_flags([MINOR], day_before)
		self.assertTrue(flags[MINOR]["is_minor"])
		self.assertFalse(shifts.minor_flags([MINOR], eighteenth.isoformat())[MINOR]["is_minor"])


# ── 2. item 15 (b): the break schedule a minor counts from ──────────────────
class TheMinorBreakSchedule(Wave3TestCase):
	def test_a_minor_is_counted_off_the_minor_rows(self):
		"""v0.302.0: the minor rows are BOLI's — 15-minute rests at the adult frequency (OAR 839-021-0072 in the
		manner of 839-020-0050), so eight hours owe a minor two rests, not four."""
		policy = dict(frappe.get_doc("Labor Break Policy", self.a_policy()).as_dict())
		adult = breaks_mod.entitlement(8.0, policy, is_minor=False)
		minor = breaks_mod.entitlement(8.0, policy, is_minor=True)
		self.assertEqual(adult["rest_periods"], 1)
		self.assertEqual(minor["rest_periods"], 2)
		self.assertEqual(adult["schedule"], "adult")
		self.assertEqual(minor["schedule"], "minor")

	def test_the_fallback_is_the_adult_table_and_it_is_the_lower_entitlement(self):
		"""A policy with no minor rows does not exempt anybody.

		THE DIRECTION IS THE DESIGN. Falling back to the adult table owes FEWER
		periods, so the gap shows up as a shortfall the moment somebody reads the
		shift. The opposite fallback — no rows, no obligation — would report a
		fifteen-year-old as fully rested on a policy nobody had finished.
		"""
		policy = dict(frappe.get_doc("Labor Break Policy", self.a_policy(with_minor_rows=False)).as_dict())
		minor = breaks_mod.entitlement(8.0, policy, is_minor=True)
		self.assertEqual(minor["rest_periods"], 1)
		self.assertEqual(minor["schedule"], "adult")
		self.assertLess(
			minor["rest_periods"],
			breaks_mod.entitlement(8.0, self._with_minor_rows(), is_minor=True)["rest_periods"],
		)

	def _with_minor_rows(self):
		policy = dict(frappe.get_doc("Labor Break Policy", self.a_policy()).as_dict())
		return policy

	def test_an_unknown_age_is_counted_as_an_adult_rather_than_guessed(self):
		policy = dict(frappe.get_doc("Labor Break Policy", self.a_policy()).as_dict())
		self.assertEqual(breaks_mod.entitlement(8.0, policy, is_minor=None)["schedule"], "adult")

	def test_get_break_policy_returns_both_tables_so_the_handset_can_pick(self):
		self.a_policy()
		data = self.tool_data("get_break_policy", {"company": MAIN})
		self.assertTrue(data["has_minor_schedule"])
		self.assertEqual(len(data["minor_rest_schedule"]), len(minors.MINOR_REST_SCHEDULE))
		self.assertEqual(data["minor_rest_schedule"][0]["minutes_each"], 15)

	def test_a_policy_with_no_minor_rows_says_so_and_hands_back_the_rows_unapproved(self):
		self.a_policy(with_minor_rows=False)
		data = self.tool_data("get_break_policy", {"company": MAIN})
		self.assertFalse(data["has_minor_schedule"])
		self.assertIn("minor_gap", data)
		suggested = data["minor_schedule_suggested"]
		self.assertFalse(suggested["approved"])
		self.assertEqual(suggested["citation"], minors.MINOR_SCHEDULE_CITATION)
		self.assertTrue(suggested["rest_schedule"])

	def test_nothing_was_written_into_the_approved_policy(self):
		"""The suggestion is PUBLISHED, not seeded. A break schedule is a
		statement somebody signed, and an app that filled it in on a read would
		move that statement with nobody's name on it."""
		name = self.a_policy(with_minor_rows=False)
		self.tool_data("get_break_policy", {"company": MAIN})
		self.assertFalse(frappe.get_doc("Labor Break Policy", name).get("minor_rest_schedule"))

	def test_the_crew_reconciliation_uses_the_minor_table_for_the_minor_alone(self):
		policy = dict(frappe.get_doc("Labor Break Policy", self.a_policy()).as_dict())
		shift = {"start_datetime": self.at(6), "end_datetime": self.at(14)}
		crew = [
			{"employee": WORKER, "employee_name": "Ben", "is_minor": False},
			{"employee": MINOR, "employee_name": "Mateo", "is_minor": True},
		]
		result = breaks_mod.crew_reconciliation(shift, crew, [], policy)
		short = {row["employee"]: row for row in result["workers_short"]}
		self.assertEqual(short[WORKER]["rest_owed"], 1)
		self.assertEqual(short[MINOR]["rest_owed"], 2)  # v0.302.0: BOLI's minor rows
		self.assertEqual(short[MINOR]["schedule"], "minor")
		self.assertEqual(result["minors_on_crew"], 1)


# ── 3. item 15 (c)–(f): what a minor may not be scheduled for ───────────────
class WhatAMinorMayNotDo(Wave3TestCase):
	"""The ceilings refuse where refusing costs a name, and report where it would
	cost the record.

	`add_worker_to_shift` REFUSES and `start_shift` REPORTS, and the asymmetry is
	the argument rather than an inconsistency: the first is about one named
	person and the shift goes on existing without them; the second would destroy
	the record of an afternoon for a crew that is standing in the block whatever
	this app says.
	"""

	def a_worked_day(self, employee: str, hours: float, day: str = "", shift: str = "SHIFT-EARLIER"):
		"""A closed shift with `employee` on it for `hours`, written directly.

		Seeded rather than driven through `start_shift`/`end_shift` because the
		close needs a signature file and this test is about the arithmetic over
		the crew rows, which is what `hours_worked_by` reads.
		"""
		day = day or frappe.utils.today()
		# THE CREW GOES INSIDE THE SHIFT ROW, not into a separately seeded child
		# table. `Farm Shift Crew Member` is flattened through its parent in this
		# double (`CHILD_TABLE_SOURCES`), exactly as it is on a bench, so a child
		# table seeded on its own is a table nothing reads — which is the shape
		# that passes a test and answers nothing in an orchard.
		STORE.seed(
			"Farm Shift",
			[
				{
					"name": shift,
					"foreman": FOREMAN,
					"company": MAIN,
					"status": "Closed",
					"start_datetime": f"{day} 06:00:00",
					"end_datetime": f"{day} {6 + int(hours):02d}:00:00",
					"crew": [
						{
							"name": f"{shift}-CREW-1",
							"parent": shift,
							"parenttype": "Farm Shift",
							"parentfield": "crew",
							"employee": employee,
							"joined_at": f"{day} 06:00:00",
							"left_at": f"{day} {6 + int(hours):02d}:00:00",
						}
					],
				}
			],
		)

	def test_the_hours_already_worked_are_counted_off_the_crew_rows(self):
		"""NOT off Attendance, which does not exist until a shift closes — and the
		day being asked about is the one still running."""
		self.a_worked_day(YOUNGER, hours=7)
		worked = shifts.hours_worked_by(YOUNGER, frappe.utils.today())
		self.assertEqual(worked["today"], 7.0)
		self.assertEqual(worked["week"], 7.0)
		self.assertEqual(len(worked["shifts"]), 1)

	def test_adding_a_minor_past_the_daily_ceiling_is_refused_by_name(self):
		self.a_worked_day(YOUNGER, hours=9)
		data = self.a_shift()
		message = self.tool_error(
			"add_worker_to_shift",
			{"shift": data["name"], "employee": YOUNGER, "joined_at": self.at(16)},
		)
		self.assertIn("Nina Fifteen", message)
		self.assertIn("8-hour", message)
		self.assertIn("ORS 653.315", message)
		self.assertIn("Nothing was changed", message)

	def test_the_negative_control_an_adult_with_the_same_nine_hours_is_rostered(self):
		"""THE TEST THAT PROVES THE GATE IS ABOUT AGE.

		Same hours, same shift, same call — an adult joins. Without this the
		refusal above is satisfied by any rule that refuses a nine-hour day.
		"""
		frappe.db.set_value("Employee", WORKER, "date_of_birth", _years_ago(41))
		self.a_worked_day(WORKER, hours=9)
		data = self.a_shift(crew_employees=[])
		added = self.tool_data(
			"add_worker_to_shift",
			{"shift": data["name"], "employee": WORKER, "joined_at": self.at(16)},
		)
		self.assertEqual(added["added"]["employee"], WORKER)

	def test_a_sixteen_to_seventeen_year_old_gets_the_higher_ceiling(self):
		"""Nine hours refuses the younger band and passes the older one. The two
		bands are not one category, and a test that used only 'minor' could not
		tell."""
		self.a_worked_day(MINOR, hours=9)
		data = self.a_shift()
		added = self.tool_data(
			"add_worker_to_shift",
			{"shift": data["name"], "employee": MINOR, "joined_at": self.at(16)},
		)
		self.assertEqual(added["added"]["employee"], MINOR)
		self.assertTrue(added["minor"]["is_minor"])

	def test_the_clock_refuses_the_younger_band_before_seven_in_the_morning(self):
		data = self.a_shift(start_datetime=self.at(5))
		message = self.tool_error(
			"add_worker_to_shift",
			{"shift": data["name"], "employee": YOUNGER, "joined_at": self.at(5, 30)},
		)
		self.assertIn("05:30", message)
		self.assertIn("07:00", message)
		self.assertIn("29 CFR 570.35", message)

	def test_the_clock_does_not_apply_to_the_older_band(self):
		"""There is no time-of-day limit on a 16- or 17-year-old in agriculture,
		and inventing one would be this app writing a rule stricter than the
		regulation."""
		data = self.a_shift(start_datetime=self.at(5))
		added = self.tool_data(
			"add_worker_to_shift",
			{"shift": data["name"], "employee": MINOR, "joined_at": self.at(5, 30)},
		)
		self.assertEqual(added["added"]["employee"], MINOR)

	def test_approaching_the_ceiling_is_a_note_and_not_a_refusal(self):
		self.a_worked_day(YOUNGER, hours=7, day=frappe.utils.add_days(frappe.utils.today(), -1))
		data = self.a_shift()
		added = self.tool_data(
			"add_worker_to_shift",
			{"shift": data["name"], "employee": YOUNGER, "joined_at": self.at(9)},
		)
		self.assertEqual(added["added"]["employee"], YOUNGER)

	def test_a_missing_date_of_birth_is_a_finding_and_never_a_block(self):
		"""Refusing on an empty column would stop a farm rostering its adult crew;
		clearing on it silently is the failure this item exists to close."""
		data = self.a_shift(crew_employees=[])
		added = self.tool_data(
			"add_worker_to_shift",
			{"shift": data["name"], "employee": WORKER, "joined_at": self.at(9)},
		)
		self.assertIn("no date of birth on file", added["date_of_birth_missing"])

	def test_start_shift_reports_the_same_finding_rather_than_refusing(self):
		"""A crew is in the block. A server that would not open a shift produces
		NO record of the afternoon, which is worse evidence than one carrying the
		finding."""
		self.a_worked_day(YOUNGER, hours=9)
		data = self.a_shift(crew_employees=[YOUNGER])
		self.assertTrue(data["name"])
		self.assertEqual(len(data["minor_limits_exceeded"]), 1)
		self.assertIn("Nina Fifteen", data["minor_note"])
		self.assertEqual(data["minors_on_crew"], 1)

	def test_a_minor_may_not_be_dispatched_to_a_spray_task(self):
		task = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Spray block 4",
				"task_type": "Spray",
				"company": MAIN,
				"evidence_required": {"findings_text": True},
			},
		)
		message = self.tool_error("assign_farm_task", {"task": task["name"], "assigned_to": MINOR})
		self.assertIn("40 CFR §170.309(c)", message)
		self.assertIn("Mateo Seventeen", message)

	def test_the_repair_bar_applies_to_the_younger_band_alone(self):
		"""29 CFR §570.71(a) binds under-sixteens; a seventeen-year-old lawfully
		runs a tractor on a farm in Oregon. A single 'minor' category would get
		one of these two wrong."""
		task = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Fix the picking platform",
				"task_type": "Repair",
				"company": MAIN,
				"evidence_required": {"findings_text": True},
			},
		)
		message = self.tool_error("assign_farm_task", {"task": task["name"], "assigned_to": YOUNGER})
		self.assertIn("570.71", message)

		other = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Fix the other platform",
				"task_type": "Repair",
				"company": MAIN,
				"evidence_required": {"findings_text": True},
			},
		)
		assigned = self.tool_data("assign_farm_task", {"task": other["name"], "assigned_to": MINOR})
		self.assertEqual(assigned["assigned_to"], MINOR)
		self.assertTrue(assigned["minor"]["is_minor"])

	def test_the_negative_control_an_adult_may_be_sent_to_a_spray_task(self):
		frappe.db.set_value("Employee", WORKER, "date_of_birth", _years_ago(41))
		task = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Spray block 5",
				"task_type": "Spray",
				"company": MAIN,
				"evidence_required": {"findings_text": True},
			},
		)
		assigned = self.tool_data("assign_farm_task", {"task": task["name"], "assigned_to": WORKER})
		self.assertEqual(assigned["assigned_to"], WORKER)

	def test_a_harvest_task_is_open_to_both_bands(self):
		"""The prohibited list is SHORT on purpose. A list that also carried
		'probably unwise' would be a list a foreman learns to override."""
		self.assertIsNone(minors.prohibited_reason(minors.BAND_UNDER_16, "Harvest"))
		self.assertIsNone(minors.prohibited_reason(minors.BAND_16_17, "Inspection"))
		self.assertIsNone(minors.prohibited_reason("", "Spray"))


# ── 4. item 15 (f): the alert that fires before the week ends ───────────────
class TheWeeklyCeilingRaisesAnAlert(Wave3TestCase):
	def a_week(self, employee: str, hours: float):
		day = frappe.utils.today()
		STORE.seed(
			"Farm Shift",
			[
				{
					"name": "SHIFT-WEEK",
					"foreman": FOREMAN,
					"company": MAIN,
					"status": "Closed",
					"start_datetime": f"{day} 06:00:00",
					"end_datetime": f"{day} 07:00:00",
					"crew": [
						{
							"name": "SHIFT-WEEK-CREW",
							"parent": "SHIFT-WEEK",
							"parenttype": "Farm Shift",
							"parentfield": "crew",
							"employee": employee,
							"joined_at": f"{day} 06:00:00",
							"left_at": frappe.utils.add_to_date(f"{day} 06:00:00", hours=hours),
						}
					],
				}
			],
		)

	def observations(self):
		return shipped_rules.SCANNERS["minor_hours_approaching"](
			{"today": frappe.utils.today(), "company": MAIN}
		)

	def test_an_ordinary_week_raises_nothing(self):
		self.a_week(YOUNGER, hours=20)
		self.assertEqual(self.observations(), [])

	def test_within_four_hours_of_the_ceiling_is_a_warning(self):
		self.a_week(YOUNGER, hours=37)
		found = self.observations()
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0].severity, "Warning")
		self.assertIn("Nina Fifteen", found[0].message)
		self.assertIn("40", found[0].message)

	def test_past_the_ceiling_is_critical(self):
		self.a_week(YOUNGER, hours=44)
		found = self.observations()
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0].severity, "Critical")
		self.assertIn("OVER", found[0].message)

	def test_the_negative_control_an_adult_at_forty_four_hours_raises_nothing(self):
		frappe.db.set_value("Employee", WORKER, "date_of_birth", _years_ago(41))
		self.a_week(WORKER, hours=44)
		self.assertEqual(self.observations(), [])

	def test_the_older_band_has_room_the_younger_one_does_not(self):
		self.a_week(MINOR, hours=44)
		self.assertEqual(self.observations(), [])

	def test_it_answers_a_phone_with_a_refusal_rather_than_a_task_nobody_can_close(self):
		"""A Farm Task reading 'do not schedule this person' is a card nobody can
		complete and evidence of nothing — so the rule ships with no recipe and
		an EXPLICIT rectification refusal, which is what stops it falling through
		to the generic task fallback."""
		built = rectify.describe_rectification({"alert_type": "minor_hours_approaching"})
		self.assertFalse(built["can_rectify_mobile"])
		self.assertIn("clears BY ITSELF when the workweek turns", built["explanation"])
		self.assertIn("minor_hours_approaching", rectify._BUILDERS)


# ── 4b. item 15 meets item 14: the countdown a minor actually reads ─────────
class TheCountdownKnowsWhoItIsFor(Wave3TestCase):
	"""`get_break_schedule` shipped in v0.98.0 computing ONE schedule for a crew.

	The whole argument for computing it on the server is that seven phones count
	down to the same second — and that fails immediately if one of the seven is a
	fifteen-year-old whose schedule the server does not know about. iOS has had
	`BreakSchedule.compute(..., isMinor:)` all along; what it had no way to do was
	ASK for it.
	"""

	def schedule_for(self, employee: str = "", **overrides):
		"""The computation, one layer under the handset endpoint.

		`get_break_schedule` is not an MCP tool — v0.98.0 mounted it on the
		mobile surface alone — and standing up that surface's authenticated user
		is `test_wave2_mobile_surface`'s furniture rather than this file's. What
		is new here is the BAND SELECTION, which lives in `_break_schedule_for`;
		the wrapper's own job is `_employee_argument` and a pass-through, and
		`test_the_handset_can_ask` pins that it declares the argument at all.
		"""
		policy = self.a_policy(**overrides)
		# NO CREW ON IT. Two calls in one test would otherwise be two open shifts
		# carrying one person, which `start_shift` refuses on its own account —
		# and rightly: `end_shift` writes one Attendance row per crew row.
		name = self.a_shift(start_datetime=self.at(6), crew_employees=[])["name"]
		frappe.db.set_value("Farm Shift", name, "break_policy", policy)
		row = dict(
			frappe.db.get_value(
				"Farm Shift",
				name,
				[*list(shifts.FIELDS), "break_policy", "work_state"],
				as_dict=True,
			)
		)
		return shift_tools._break_schedule_for(row, planned_hours=8, employee=employee)

	def test_a_minor_gets_more_breaks_at_different_instants(self):
		crew = self.schedule_for()
		minor = self.schedule_for(employee=YOUNGER)
		self.assertGreater(minor["count"], crew["count"])
		self.assertEqual(minor["schedule_band"], "minor")
		self.assertEqual(crew["schedule_band"], "adult")
		self.assertNotEqual(
			[row["due_at"] for row in minor["breaks"]],
			[row["due_at"] for row in crew["breaks"]],
		)

	def test_the_answer_says_whose_schedule_it_is(self):
		"""The badge reads this. A badge that claimed a band the server did not
		use would be worse than no badge."""
		data = self.schedule_for(employee=YOUNGER)
		self.assertEqual(data["employee"], YOUNGER)
		self.assertTrue(data["is_minor"])
		self.assertEqual(data["minor_band"], minors.BAND_UNDER_16)
		self.assertTrue(all(row["schedule_band"] == "minor" for row in data["breaks"]))

	def test_the_negative_control_naming_an_adult_changes_nothing(self):
		frappe.db.set_value("Employee", WORKER, "date_of_birth", _years_ago(41))
		crew = self.schedule_for()
		adult = self.schedule_for(employee=WORKER)
		self.assertEqual(
			[row["due_at"] for row in adult["breaks"]],
			[row["due_at"] for row in crew["breaks"]],
		)
		self.assertFalse(adult["is_minor"])

	def test_a_policy_with_no_minor_rows_counts_the_adult_way_and_says_so(self):
		"""The gap sentence is the TOOL's, so this one goes through the tool."""
		policy = self.a_policy(with_minor_rows=False)
		name = self.a_shift(start_datetime=self.at(6), crew_employees=[])["name"]
		frappe.db.set_value("Farm Shift", name, "break_policy", policy)
		data = shift_tools.get_break_schedule({"shift": name, "planned_hours": 8, "employee": YOUNGER}).data
		self.assertEqual(data["schedule_band"], "adult")
		self.assertTrue(data["is_minor"])
		self.assertIn("ADULT one", data["minor_gap"])

	def test_the_handset_can_ask_and_cannot_ask_about_a_stranger(self):
		from erpnext_mcp.farmops_api import routes as farmops_routes

		route = farmops_routes.BY_PATH["/mobile/get_break_schedule"]
		self.assertIn("employee", farmops_routes.accepted_arguments(route.handler))


# ── 5. item 17: a heat break carries the heat ───────────────────────────────
class AHeatBreakCarriesTheHeat(Wave3TestCase):
	"""The break row answers "was relief provided in time" without a join.

	The Farm Shift controller has stamped every event with the reading current at
	its own instant since v0.19.4. That is the conditions the foreman was
	STANDING IN. What the record could not say is the conditions the break was
	CALLED ABOUT — the shift's peak, and the moment the index crossed the
	threshold — which is what OAR 437-004-1131 attaches its obligations to. A
	cool-down at 16:10 after a 97 °F afternoon sits on a row whose snapshot reads
	88, and reconstructing the rest is a join over ninety readings that nobody
	performs.
	"""

	def a_hot_shift(self):
		data = self.a_shift(start_datetime=self.at(6))
		doc = frappe.get_doc("Farm Shift", data["name"])
		for hour, temp, index in ((8, 78.0, 79.0), (11, 92.0, 97.0), (15, 86.0, 88.0)):
			doc.append(
				"weather_timeline",
				{
					"reading_datetime": self.at(hour),
					"temp_f": temp,
					"heat_index_f": index,
					"source": "Open-Meteo forecast API",
				},
			)
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		return data["name"]

	def the_break(self, shift: str, kind: str):
		return next(row for row in shifts.events_of(shift) if row.get("break_kind") == kind)

	def test_a_water_break_carries_the_peak_the_crossing_and_the_source(self):
		shift = self.a_hot_shift()
		self.tool_data(
			"log_shift_break",
			{"shift": shift, "break_kind": "Water Break", "started_at": self.at(16), "duration_minutes": 10},
		)
		row = self.the_break(shift, "Water Break")
		self.assertEqual(row["peak_heat_index_f"], 97.0)
		self.assertEqual(row["peak_temp_f"], 92.0)
		# 08:00 read 79 °F — BELOW the 80 °F threshold — so the crossing is the
		# 11:00 reading. The point of asserting it rather than the first reading:
		# a stamp that recorded "the first weather we have" would be a different
		# and useless claim, and it would pass a test written against 08:00.
		self.assertEqual(str(row["threshold_crossed_at"]), self.at(11))
		self.assertEqual(row["weather_source"], "Open-Meteo forecast API")
		self.assertTrue(row["heat_obligation"])

	def test_the_peak_is_not_the_snapshot_and_the_two_are_both_kept(self):
		"""THE WHOLE POINT OF THE ITEM, in one assertion. The snapshot is 88 —
		what it was at four o'clock — and the peak is 97, which is the afternoon
		the break was called about."""
		shift = self.a_hot_shift()
		self.tool_data(
			"log_shift_break",
			{
				"shift": shift,
				"break_kind": "Cool-Down",
				"started_at": self.at(15, 10),
				"duration_minutes": 10,
			},
		)
		row = self.the_break(shift, "Cool-Down")
		self.assertEqual(row["weather_snapshot_heat_index_f"], 88.0)
		self.assertEqual(row["peak_heat_index_f"], 97.0)
		self.assertNotEqual(row["weather_snapshot_heat_index_f"], row["peak_heat_index_f"])

	def test_the_negative_control_a_paid_rest_carries_none_of_it(self):
		"""Keyed on the KIND and not on the presence of a timeline. Without this
		the test above passes for an implementation that stamps every break."""
		shift = self.a_hot_shift()
		self.tool_data(
			"log_shift_break",
			{"shift": shift, "break_kind": "Paid Rest", "started_at": self.at(16), "duration_minutes": 10},
		)
		row = self.the_break(shift, "Paid Rest")
		self.assertFalse(row["heat_obligation"])
		self.assertIn(row.get("peak_heat_index_f"), (None, "", 0))
		self.assertIn(row.get("threshold_crossed_at"), (None, ""))

	def test_all_three_heat_kinds_count_and_they_stay_three_records(self):
		shift = self.a_hot_shift()
		for hour, kind in ((10, "Water Break"), (12, "Shade Break"), (14, "Cool-Down")):
			self.tool_data(
				"log_shift_break",
				{"shift": shift, "break_kind": kind, "started_at": self.at(hour), "duration_minutes": 10},
			)
		rows = [row for row in shifts.events_of(shift) if row.get("break_kind")]
		self.assertEqual(len(rows), 3)
		self.assertTrue(all(row["heat_obligation"] for row in rows))
		self.assertEqual({row["event_type"] for row in rows}, {"Water Break", "Shade Break", "Cool-Down"})

	def test_a_shift_with_no_timeline_leaves_the_columns_blank_and_says_so(self):
		"""Blank rather than zero: nobody measured, and that is not a
		temperature."""
		data = self.a_shift(start_datetime=self.at(6))
		answer = self.tool_data(
			"log_shift_break",
			{
				"shift": data["name"],
				"break_kind": "Water Break",
				"started_at": self.at(10),
				"duration_minutes": 10,
			},
		)
		row = self.the_break(data["name"], "Water Break")
		self.assertTrue(row["heat_obligation"])
		self.assertIn(row.get("peak_heat_index_f"), (None, "", 0))
		self.assertIn("no weather reading", answer["heat_note"])

	def test_nothing_is_copied_from_a_reading_that_did_not_exist_yet(self):
		"""At-or-before, never after. Reaching forward would stamp a break with a
		measurement taken after it was called."""
		shift = self.a_hot_shift()
		self.tool_data(
			"log_shift_break",
			{"shift": shift, "break_kind": "Water Break", "started_at": self.at(9), "duration_minutes": 10},
		)
		row = self.the_break(shift, "Water Break")
		self.assertEqual(row["peak_heat_index_f"], 79.0)
		self.assertNotEqual(row["peak_heat_index_f"], 97.0)

	def test_the_read_side_carries_both_spellings_of_the_temperature(self):
		"""`temp_f` is what every existing reader uses and `ambient_temp_f` is
		what the handset's heat-break payload calls it. One column, two keys, so
		they cannot drift — which is the class of failure wave 1 was seven
		instances of."""
		shift = self.a_hot_shift()
		self.tool_data(
			"log_shift_break",
			{
				"shift": shift,
				"break_kind": "Shade Break",
				"started_at": self.at(15, 10),
				"duration_minutes": 10,
			},
		)
		described = self.tool_data("get_shift", {"shift": shift})
		event = next(row for row in described["compliance_events"] if row.get("break_kind"))
		self.assertEqual(event["temp_f"], event["ambient_temp_f"])
		self.assertEqual(event["heat_index_f"], 88.0)
		self.assertEqual(event["peak_heat_index_f"], 97.0)
		self.assertTrue(event["heat_obligation"])
		self.assertEqual(event["weather_source"], "Open-Meteo forecast API")

	def test_the_threshold_is_the_apps_own_and_not_a_number_typed_here(self):
		self.assertEqual(shifts.HEAT_THRESHOLD_F, 80.0)
		self.assertEqual(
			shifts.heat_conditions(
				[{"reading_datetime": self.at(8), "temp_f": 70.0, "heat_index_f": 71.0}], self.at(9)
			)["threshold_crossed_at"],
			None,
		)

	# ── item 9, the half of it that was left open ───────────────────────────
	#
	# v0.96.0 widened `BREAK_KINDS` to `Water Break` and `Shade Break` and the
	# item was called closed. It was not: the app's enum spells them `Water` and
	# `Shade`, its recovery path matches a farm's list on letters and digits
	# alone, and `water` never matches `waterbreak` — so a phone that stopped
	# folding onto Cool-Down would have been refused, and every water and shade
	# break would have gone back to living on the handset. These pin the
	# spellings a phone actually sends.

	def test_the_spelling_the_handset_uses_is_taken_and_stored_canonical(self):
		"""`Water` is `BreakEvent.Kind.water.rawValue`, verbatim."""
		shift = self.a_hot_shift()
		self.tool_data(
			"log_shift_break",
			{"shift": shift, "break_kind": "Water", "started_at": self.at(16), "duration_minutes": 10},
		)
		# STORED AS THE SELECT'S OWN WORDING, not as what arrived: every reader in
		# `breaks.py` compares against these by name, so a row holding `Water`
		# would be a heat break missing from the heat counts.
		row = self.the_break(shift, "Water Break")
		self.assertEqual(row["break_kind"], "Water Break")
		self.assertEqual(row["event_type"], "Water Break")
		self.assertTrue(row["heat_obligation"])
		self.assertIn(row["break_kind"], breaks_mod.HEAT_RELIEF_KINDS)

	def test_shade_too_and_it_is_not_folded_onto_a_cool_down(self):
		"""The distinction is the regulation's. An inspector after a heat event
		asks whether SHADE was provided, not whether a cool-down was."""
		shift = self.a_hot_shift()
		self.tool_data(
			"log_shift_break",
			{"shift": shift, "break_kind": "Shade", "started_at": self.at(16), "duration_minutes": 10},
		)
		kinds = {row["break_kind"] for row in shifts.events_of(shift) if row.get("break_kind")}
		self.assertEqual(kinds, {"Shade Break"})
		self.assertNotIn("Cool-Down", kinds)

	def test_a_retyped_select_is_still_the_same_column(self):
		"""An administrator who dropped the hyphen broke every heat break on the
		farm and would never have connected the two.

		ASSERTED ON THE REGISTER AND NOT ONLY ON THE HELPER. What this claim is
		about is breaks that failed to get logged, so the assertion that means
		anything is on the rows the shift ends up holding.
		"""
		shift = self.a_hot_shift()
		retyped = {
			"Cool Down": "Cool-Down",
			"cooldown": "Cool-Down",
			"COOL_DOWN": "Cool-Down",
			"water break": "Water Break",
		}
		for index, (sent, stored) in enumerate(retyped.items()):
			with self.subTest(sent=sent):
				self.assertEqual(shift_tools.canonical_break_kind(sent), stored)
				self.tool_data(
					"log_shift_break",
					{
						"shift": shift,
						"break_kind": sent,
						"started_at": self.at(9 + index),
						"duration_minutes": 10,
					},
				)
		rows = [row for row in shifts.events_of(shift) if row.get("break_kind")]
		# FOUR SPELLINGS WENT UP AND THE REGISTER HOLDS TWO WORDS, both of them
		# the Select's own. Four rows rather than three: the spelling is
		# normalised, the break is not deduplicated.
		self.assertEqual(len(rows), 4)
		self.assertEqual({row["break_kind"] for row in rows}, {"Cool-Down", "Water Break"})
		self.assertTrue(all(row["heat_obligation"] for row in rows))

	def test_nothing_resolves_across_meanings(self):
		"""THE ONE THAT MATTERS MOST. An unpaid meal must never become a paid
		rest to make a write succeed — that is a wage claim with this server's
		fingerprints on it — and a word that is not a break kind is still a
		refusal rather than a guess."""
		self.assertEqual(shift_tools.canonical_break_kind("Rest"), "")
		self.assertEqual(shift_tools.canonical_break_kind("Meal"), "")
		self.assertEqual(shift_tools.canonical_break_kind("Lunch"), "")
		self.assertEqual(shift_tools.canonical_break_kind(""), "")
		for alias, canonical in shift_tools.BREAK_KIND_ALIASES.items():
			with self.subTest(alias=alias):
				# An alias is the same provision spelled shorter. Both names have
				# to be about the same break, and `paid` is where a substitution
				# across meanings would show up first.
				self.assertIn(canonical, shift_tools.BREAK_KINDS)
				self.assertIn(alias.lower(), canonical.lower())

	def test_the_refusal_still_enumerates_the_canonical_list(self):
		"""It is parsed by a machine as well as read by a person: the handset
		reads the accepted values out of this sentence and builds its retry from
		them, so it must name what the column HOLDS and not the aliases."""
		shift = self.a_hot_shift()
		message = self.tool_error(
			"log_shift_break",
			{"shift": shift, "break_kind": "Lunch", "started_at": self.at(16)},
		)
		self.assertIn("break_kind must be one of", message)
		self.assertIn("Water Break", message)
		self.assertIn("Shade Break", message)
		self.assertIn("Got 'Lunch'", message)
		self.assertNotIn("'Water',", message)


# ── 6. item 4: one afternoon, not eleven cards ──────────────────────────────
class OneAfternoonNotElevenCards(Wave3TestCase):
	"""A cohort retraining is one delivery whether three people need it or eleven.

	`training_expiring` raises one alert per Employee Training Record, so a crew
	whose heat-illness training all lapses in the same fortnight produced N
	Compliance-Audit tasks each reading "arrange and deliver the retraining" —
	N cards for one afternoon, no cohort anywhere on the record, and an
	attendance sheet that has to be assembled afterwards from N separately closed
	tasks. iOS has had `TrainingSessionRunner` and `AttendanceSignatureView`
	since before this release with nothing raised for them to run.
	"""

	CURRICULUM = "Heat Illness Prevention"
	SOLO = "Applicator Licence Refresher"

	def a_curriculum(self, name: str, group: bool):
		STORE.seed(
			"Training Type",
			[{"name": name, "training_type_name": name, "active": 1, "group_training": 1 if group else 0}],
		)
		return name

	def lapsing(self, employee: str, curriculum: str, index: int):
		STORE.seed(
			"Employee Training Record",
			[
				{
					"name": f"ETR-{index:04d}",
					"employee": employee,
					"employee_name": frappe.db.get_value("Employee", employee, "employee_name"),
					"training_type": curriculum,
					"company": MAIN,
					"completed_date": frappe.utils.add_days(frappe.utils.today(), -350),
					"expires_date": frappe.utils.add_days(frappe.utils.today(), 10),
				}
			],
		)
		STORE.seed(
			"Compliance Alert",
			[
				{
					"name": f"CA-TRAIN-{index:04d}",
					"alert_key": f"CA-TRAIN-{index:04d}",
					"alert_type": "training_expiring",
					"severity": "Warning",
					"category": "Workforce",
					"company": MAIN,
					"source_doctype": "Employee Training Record",
					"source_docname": f"ETR-{index:04d}",
					"alert_message": f"{curriculum} expires in 10 days.",
					"due_date": frappe.utils.add_days(frappe.utils.today(), 10),
					"dismissed": 0,
				}
			],
		)

	def sweep(self, **overrides):
		payload = {"company": MAIN, "alert_types": ["training_expiring"]}
		payload.update(overrides)
		return self.tool_data("generate_tasks_from_compliance_alerts", payload)

	def test_two_people_on_a_group_curriculum_become_one_session(self):
		self.a_curriculum(self.CURRICULUM, group=True)
		self.lapsing(WORKER, self.CURRICULUM, 1)
		self.lapsing(MINOR, self.CURRICULUM, 2)
		report = self.sweep()
		self.assertEqual(report["training_session_count"], 1)
		self.assertEqual(report["alerts_bundled_into_training_sessions"], 2)
		self.assertEqual(report["created_count"], 0)

	def test_everybody_is_on_the_attendance_sheet_and_nobody_is_marked_present(self):
		"""Listing somebody who is going to be retrained is a roster. Ticking
		`attended` would be a claim that they turned up, made before the
		afternoon happened."""
		self.a_curriculum(self.CURRICULUM, group=True)
		self.lapsing(WORKER, self.CURRICULUM, 1)
		self.lapsing(MINOR, self.CURRICULUM, 2)
		name = self.sweep()["training_sessions"][0]["training_session"]
		doc = frappe.get_doc("Training Session", name)
		self.assertEqual(doc.status, "Scheduled")
		self.assertEqual({row.employee for row in doc.attendees}, {WORKER, MINOR})
		self.assertFalse(any(row.attended for row in doc.attendees))

	def test_the_session_records_which_alerts_it_answers(self):
		self.a_curriculum(self.CURRICULUM, group=True)
		self.lapsing(WORKER, self.CURRICULUM, 1)
		self.lapsing(MINOR, self.CURRICULUM, 2)
		name = self.sweep()["training_sessions"][0]["training_session"]
		answered = frappe.get_doc("Training Session", name).source_alerts
		self.assertEqual(set(answered.split("\n")), {"CA-TRAIN-0001", "CA-TRAIN-0002"})

	def test_running_the_sweep_twice_raises_nothing_the_second_time(self):
		"""IDEMPOTENT, and by the session rather than by the task: a Farm Task
		carries one `source_alert` and a session answers several, so the session
		is what the next sweep has to read."""
		self.a_curriculum(self.CURRICULUM, group=True)
		self.lapsing(WORKER, self.CURRICULUM, 1)
		self.lapsing(MINOR, self.CURRICULUM, 2)
		self.sweep()
		again = self.sweep()
		self.assertEqual(again["training_session_count"], 0)
		self.assertEqual(again["created_count"], 0)
		self.assertEqual(len(again["skipped_already_answered"]), 2)
		self.assertEqual(len(frappe.db.get_all("Training Session")), 1)

	def test_the_negative_control_an_unticked_curriculum_still_raises_one_task_each(self):
		"""THE TEST THAT PROVES THE FLAG DOES THE WORK. An applicator licence is a
		one-to-one item and the per-alert path is right for it; without this the
		bundling above is satisfied by an implementation that bundles everything.
		"""
		self.a_curriculum(self.SOLO, group=False)
		self.lapsing(WORKER, self.SOLO, 3)
		self.lapsing(MINOR, self.SOLO, 4)
		report = self.sweep()
		self.assertEqual(report["training_session_count"], 0)
		self.assertEqual(report["created_count"], 2)

	def test_one_person_alone_is_a_task_and_not_a_session(self):
		"""A session opened for a single attendee is a heavier document answering
		a lighter question, and the per-alert path already handles it well."""
		self.a_curriculum(self.CURRICULUM, group=True)
		self.lapsing(WORKER, self.CURRICULUM, 5)
		report = self.sweep()
		self.assertEqual(report["training_session_count"], 0)
		self.assertEqual(report["created_count"], 1)

	def test_a_single_tap_on_a_phone_gets_its_task_and_is_told_about_the_cohort(self):
		"""`materialize_task_for_alert` answers ONE docname by design — a tap
		names the alert somebody is looking at, not a filter that might catch a
		coworker's. What it owes them is the sentence."""
		self.a_curriculum(self.CURRICULUM, group=True)
		self.lapsing(WORKER, self.CURRICULUM, 1)
		self.lapsing(MINOR, self.CURRICULUM, 2)
		data = self.tool_data("materialize_task_for_alert", {"alert": "CA-TRAIN-0001"})
		self.assertTrue(data["task"])
		self.assertIn("GROUP session", data["cohort_note"])
		self.assertIn("generate_tasks_from_compliance_alerts", data["cohort_note"])

	def test_a_curriculum_flag_is_a_row_and_not_a_release(self):
		"""The flag lives on the Training Type, so making a curriculum a cohort
		delivery is an operator's edit rather than a code change."""
		from erpnext_mcp import compat

		self.assertTrue(compat.has_field("Training Type", "group_training"))
		self.assertTrue(compat.has_field("Farm Task Template", "group_training"))


# ── 7. item 23: the bin traces back to the crew ─────────────────────────────
class TheBinTracesBackToTheCrew(Wave3TestCase):
	"""A bin leaves the orchard carrying a tag and nothing else.

	Every question asked of it afterwards — whose fruit, which block, which
	shift, and therefore which spray record and which weather timeline — is a
	join from that tag back to an hour nobody wrote down. This register is what
	writes it down, at the moment the checker seals, which is the last instant
	anybody knows.
	"""

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Bucket Log Badge Map",
			[
				{"name": "B-0117", "badge_id": "B-0117", "company": MAIN, "employee": WORKER, "active": 1},
				{"name": "B-0119", "badge_id": "B-0119", "company": MAIN, "employee": MINOR, "active": 1},
			],
		)

	def a_seal(self, **overrides):
		payload = {
			"bin_tag": "OML-4471",
			"bucket_count": 42,
			"contributors": [
				{"badge_id": "B-0117", "buckets_contributed": 18},
				{"employee": MINOR, "buckets_contributed": 21},
			],
			"sealed_by": FOREMAN,
			"company": MAIN,
			"sealed_at": self.at(11, 30),
		}
		payload.update(overrides)
		return self.tool_data("seal_bin", payload)

	# -- writing -------------------------------------------------------------
	def test_a_bin_is_sealed_with_the_names_of_the_people_who_filled_it(self):
		data = self.a_seal()
		self.assertTrue(data["name"])
		self.assertEqual(data["bucket_count"], 42)
		self.assertEqual(data["contributor_count"], 2)
		self.assertEqual({row["employee"] for row in data["contributors"]}, {WORKER, MINOR})
		self.assertFalse(data["already_sealed"])

	def test_a_badge_resolves_to_the_person_and_the_badge_is_kept_beside_them(self):
		"""Both, and not one instead of the other: the badge is what the phone
		saw and the link is what it resolved to, and a dispute about a mis-scan
		needs both."""
		row = next(entry for entry in self.a_seal()["contributors"] if entry["employee"] == WORKER)
		self.assertEqual(row["badge_id"], "B-0117")
		self.assertEqual(row["employee_name"], "Ben Packhouse")

	def test_the_two_counts_are_allowed_to_disagree_and_the_gap_is_named(self):
		"""NEVER RECONCILED. A bucket tipped by somebody whose badge did not scan
		is in the bin and not in the rows, and that is the fact a piece-rate
		dispute turns on. Balancing them would delete it."""
		data = self.a_seal()
		self.assertEqual(data["buckets_attributed"], 39)
		self.assertEqual(data["unattributed_buckets"], 3)
		read = self.tool_data("get_bin_seal", {"name": data["name"]})
		self.assertIn("attributed to nobody", read["attribution_note"])

	def test_a_retry_returns_the_same_seal_rather_than_doubling_the_count(self):
		"""A phone that sealed a bin and did not hear the answer sends the same
		call again. Two records of one bin is a doubled count at the pack line
		and a doubled piece rate on somebody's cheque."""
		first = self.a_seal(client_event_id="evt-8f2c")
		second = self.a_seal(client_event_id="evt-8f2c")
		self.assertTrue(second["already_sealed"])
		self.assertEqual(second["name"], first["name"])
		self.assertEqual(len(frappe.db.get_all("Bin Seal")), 1)

	def test_the_negative_control_a_second_seal_with_no_event_id_is_a_second_bin(self):
		"""Two bins really can carry one tag on one afternoon — a checker
		re-using a sticker, a tag read twice off two bins. The idempotency is on
		the EVENT, never on the tag, and this is what says so."""
		self.a_seal()
		self.a_seal()
		self.assertEqual(len(frappe.db.get_all("Bin Seal")), 2)

	def test_one_worker_who_came_back_is_one_row_with_the_buckets_added_up(self):
		"""Merged rather than refused. The controller's duplicate check would
		otherwise refuse the whole seal over somebody's second bucket."""
		data = self.a_seal(
			contributors=[
				{"badge_id": "B-0117", "buckets_contributed": 8, "scanned_at": self.at(9)},
				{"employee": WORKER, "buckets_contributed": 10, "scanned_at": self.at(11)},
			]
		)
		self.assertEqual(data["contributor_count"], 1)
		row = data["contributors"][0]
		self.assertEqual(row["buckets_contributed"], 18)
		self.assertEqual(row["first_scan_at"], self.at(9))
		self.assertEqual(row["last_scan_at"], self.at(11))

	def test_an_unregistered_badge_is_reported_and_the_bin_is_still_sealed(self):
		"""A bin refused over a card nobody registered is a bin nothing can trace
		at all — a record with a gap in it is worth more."""
		data = self.a_seal(contributors=[{"badge_id": "B-0117", "buckets_contributed": 18}, "B-9999"])
		self.assertEqual(data["unresolved_badges"], ["B-9999"])
		self.assertEqual(data["contributor_count"], 1)
		self.assertTrue(data["name"])

	def test_a_bin_with_nobody_on_it_says_what_that_costs(self):
		data = self.a_seal(contributors=[])
		self.assertEqual(data["contributor_count"], 0)
		self.assertIn("names nobody", data["no_contributors_note"])

	def test_the_coordinates_are_stored_and_indexed(self):
		data = self.a_seal(gps_lat=45.9327, gps_lon=-118.3877)
		self.assertEqual(data["gps_lat"], 45.9327)
		# `h3_hex` is derived where the site has the library and blank where it
		# does not — the coordinates are the record and the cell is the index.
		self.assertIn("h3_hex", data)

	def test_a_transposed_coordinate_is_refused_rather_than_filed(self):
		"""[45.6, -121.2] and [-121.2, 45.6] are the same two numbers and only
		one of them is in Oregon."""
		message = self.tool_error(
			"seal_bin",
			{"bin_tag": "OML-9", "bucket_count": 1, "gps_lat": -118.3877, "gps_lon": 45.9327},
		)
		self.assertIn("not a point on Earth", message)

	def test_a_negative_bucket_count_is_refused(self):
		message = self.tool_error("seal_bin", {"bin_tag": "OML-9", "bucket_count": -3})
		self.assertIn("piece-rate", message)

	def test_a_bucket_count_of_zero_is_a_real_answer_and_omitting_it_is_not(self):
		self.assertEqual(self.a_seal(bucket_count=0)["bucket_count"], 0)
		self.assertIn("bucket_count is required", self.tool_error("seal_bin", {"bin_tag": "OML-9"}))

	def test_a_manual_tag_is_recognisable_as_one(self):
		data = self.a_seal(bin_tag="MANUAL-0042")
		self.assertTrue(data["manual_tag"])
		self.assertFalse(self.a_seal(bin_tag="OML-0001")["manual_tag"])

	def test_a_field_that_is_not_on_the_register_is_refused_by_name(self):
		message = self.tool_error(
			"seal_bin", {"bin_tag": "OML-9", "bucket_count": 1, "field": "Nowhere Block"}
		)
		self.assertIn("Nowhere Block", message)
		self.assertIn("block", message)

	def test_the_shift_carries_the_company_onto_the_seal(self):
		shift = self.a_shift()["name"]
		data = self.a_seal(shift=shift, company=None)
		self.assertEqual(data["shift"], shift)
		self.assertEqual(data["company"], MAIN)

	# -- reading -------------------------------------------------------------
	def test_the_register_filters_by_shift_and_by_tag(self):
		shift = self.a_shift()["name"]
		self.a_seal(bin_tag="OML-1", shift=shift)
		self.a_seal(bin_tag="OML-2", shift=shift)
		self.a_seal(bin_tag="OML-3")
		self.assertEqual(self.tool_data("list_bin_seals", {"shift": shift})["count"], 2)
		self.assertEqual(self.tool_data("list_bin_seals", {"bin_tag": "OML-3"})["count"], 1)
		self.assertEqual(self.tool_data("list_bin_seals", {})["total_buckets"], 126)

	def test_the_register_does_not_carry_the_contributors(self):
		"""One bin's crew is a child table; forty bins would be forty reads of
		it. `get_bin_seal` is the one that has them."""
		self.a_seal()
		row = self.tool_data("list_bin_seals", {})["seals"][0]
		self.assertNotIn("contributors", row)
		self.assertIn("contributors", self.tool_data("get_bin_seal", {"name": row["name"]}))

	# -- the read the whole feature exists for --------------------------------
	def test_a_tag_at_the_packing_house_answers_with_the_crew(self):
		shift = self.a_shift()["name"]
		self.a_seal(shift=shift)
		traced = self.tool_data("trace_bin", {"bin_tag": "OML-4471"})
		self.assertEqual(traced["matches"], 1)
		self.assertEqual(traced["bucket_count"], 42)
		self.assertEqual(traced["shift"], shift)
		self.assertEqual(traced["sealed_by_name"], "Ada Orchard")
		self.assertEqual({row["employee"] for row in traced["contributors"]}, {WORKER, MINOR})
		self.assertIn("Ada Orchard", traced["note"])

	def test_a_reused_tag_answers_with_all_of_them_rather_than_confidently_with_one(self):
		"""THE CASE A UNIQUENESS CONSTRAINT WOULD HAVE GOT WRONG IN THE EXPENSIVE
		DIRECTION. Bin tags are reused between seasons and between growers;
		refusing the second seal would throw away a true record of a bin that was
		really closed, and answering with one of two would be a confident answer
		about possibly the wrong afternoon."""
		self.a_seal(sealed_at=self.at(9))
		self.a_seal(sealed_at=self.at(15), bucket_count=30)
		traced = self.tool_data("trace_bin", {"bin_tag": "OML-4471"})
		self.assertEqual(traced["matches"], 2)
		self.assertEqual(traced["bucket_count"], 30)  # the newest
		self.assertEqual(len(traced["ambiguous"]), 2)
		self.assertIn("reused between seasons", traced["ambiguity_note"])

	def test_a_tag_with_no_seal_is_a_break_in_the_chain_and_says_so(self):
		message = self.tool_error("trace_bin", {"bin_tag": "OML-NOTHING"})
		self.assertIn("break in the chain", message)
		self.assertIn("list_bin_seals", message)

	def test_the_aliases_the_pack_line_might_use_all_reach_it(self):
		self.a_seal()
		for key in ("bin_tag", "tag", "bin"):
			self.assertEqual(self.tool_data("trace_bin", {key: "OML-4471"})["matches"], 1)

	# -- the mobile door ------------------------------------------------------
	def test_the_docname_is_a_dated_series(self):
		"""Asserted against the DOCTYPE rather than the docname, because this
		double names records by its own fallback rule and a bench names them off
		the series — so a test on the string would be asserting the double."""
		from erpnext_mcp import compat

		meta = compat.field_meta("Bin Seal", "naming_series")
		self.assertIn("BIN-.YYYY.", str(getattr(meta, "options", "") or ""))

	def test_the_handset_route_exists_and_cannot_name_a_company(self):
		"""`company` and `source` are absent from the wrapper's signature, so the
		route table's argument filter makes them unreachable rather than merely
		refused."""
		from erpnext_mcp.farmops_api import routes as farmops_routes

		route = farmops_routes.BY_PATH["/mobile/seal_bin"]
		accepted = farmops_routes.accepted_arguments(route.handler)
		self.assertIn("bin_tag", accepted)
		self.assertIn("contributors", accepted)
		self.assertNotIn("company", accepted)
		self.assertNotIn("source", accepted)
		self.assertTrue(route.mutating)

	def test_the_write_ships_off_and_the_three_reads_ship_on(self):
		from erpnext_mcp import registry

		self.assertTrue(registry.TOOLS["seal_bin"]["mutating"])
		for name in ("get_bin_seal", "list_bin_seals", "trace_bin"):
			self.assertFalse(registry.TOOLS[name]["mutating"])

	def test_the_contributors_are_read_through_the_parent(self):
		"""Filtering the child doctype on `parent` works on a bench and answers
		nothing in this double — a tool written that way is a tool whose whole
		answer is untested. `describe` reads the parent document."""
		name = self.a_seal()["name"]
		self.assertEqual(len(binseals.describe(name, with_contributors=True)["contributors"]), 2)


# ── S5 / S12: the state, and the hours that were actually worked ────────────
class TheShiftKnowsWhichStateItIsIn(Wave3TestCase):
	"""S5. `work_state` has been on Farm Shift since v0.58.0 and nothing wrote it.

	Three readers consult that column — `get_break_schedule`'s policy fallback,
	`list_employees_by_work_state`, and the minor hour ceilings — and every shift
	this app had ever created carried an empty one, so all three were answering
	from a blank. `start_shift` now takes it.
	"""

	def test_start_shift_records_the_state_it_was_given(self):
		data = self.a_shift(work_state="WA")
		self.assertEqual(STORE.get_raw("Farm Shift", data["name"])["work_state"], "WA")

	def test_it_is_optional_and_a_shift_without_one_still_opens(self):
		"""A foreman opening a shift at five in the morning is not stopped by a
		field. The readers each say what they did without it."""
		data = self.a_shift()
		self.assertTrue(data["name"])
		self.assertFalse(STORE.get_raw("Farm Shift", data["name"]).get("work_state"))

	def test_a_state_this_app_does_not_know_is_refused_rather_than_dropped(self):
		"""The column is a Select with two options, so Frappe would drop a third
		silently — leaving a shift reporting NO state after a call that named one,
		and the minor checks quietly back on the strictest table."""
		message = self.tool_error(
			"start_shift",
			{
				"foreman": FOREMAN,
				"location": "Block 7 North",
				"shift_type": "Harvest",
				"start_datetime": self.at(8),
				"crew_employees": [WORKER],
				"work_state": "CA",
			},
		)
		self.assertIn("OR, WA", message)
		self.assertIn("Nothing was created", message)

	def a_fifty_one_hour_week(self):
		"""Forty-two hours over the week's earlier days, plus nine today.

		THE FIGURE IS CHOSEN TO SIT BETWEEN THE TWO STATES: Washington's ceiling
		for this band is fifty and Oregon's is sixty, so fifty-one is a breach in
		one state and lawful in the other, and the three tests below differ only
		in which state the shift says it is in. The daily total stays at nine,
		under both states' ten, so nothing here can pass or fail on the DAILY
		limit by accident.

		THE DAYS ARE READ OFF `shifts.week_bounds` RATHER THAN COUNTED BACKWARDS
		FROM TODAY. The workweek starts on a Monday, so a fixed `-5` lands in the
		PREVIOUS week and its ten hours are not counted at all — which is how the
		first draft of this helper built a "fifty-nine hour week" that the code
		correctly read as forty-nine. The assertion below is deliberate: if the
		fixture's frozen date ever moves to early in a week there are not enough
		earlier days to build this total, and that should fail loudly here rather
		than as a puzzling pass somewhere else.
		"""
		today = frappe.utils.today()
		week_start, _ = shifts.week_bounds(today)
		earlier = []
		offset = -1
		while str(frappe.utils.add_days(today, offset)) >= str(week_start):
			earlier.append(offset)
			offset -= 1
		self.assertGreaterEqual(
			len(earlier),
			4,
			"this fixture's frozen date is too early in the week to build a 51-hour week",
		)
		for day_offset in earlier[:4]:
			self.a_worked_hours(MINOR, 10.5, day_offset)
		self.a_worked_hours(MINOR, 9.0, 0)

	def test_oregon_allows_the_fifty_nine_hour_week(self):
		"""THE NEGATIVE HALF OF S5, and it has to be asserted or the refusal below
		is satisfied by any rule that dislikes long weeks."""
		self.a_fifty_one_hour_week()
		oregon = self.a_shift(work_state="OR", crew_employees=[], start_datetime=self.at(8))
		added = self.tool_data(
			"add_worker_to_shift",
			{"shift": oregon["name"], "employee": MINOR, "joined_at": self.at(9)},
		)
		self.assertEqual(added["added"]["employee"], MINOR)

	def test_washington_refuses_the_same_week_by_its_own_citation(self):
		"""THE POINT OF S5. Same person, same hours, same call — a different
		state, and this one is nine hours over."""
		self.a_fifty_one_hour_week()
		washington = self.a_shift(work_state="WA", crew_employees=[], start_datetime=self.at(8))
		message = self.tool_error(
			"add_worker_to_shift",
			{"shift": washington["name"], "employee": MINOR, "joined_at": self.at(9)},
		)
		self.assertIn("50-hour", message)
		self.assertIn("WAC 296-131-120", message)

	def test_with_no_state_the_stricter_figure_binds_and_the_refusal_says_so(self):
		"""The safe direction, plus the one sentence that makes it actionable —
		an unactionable refusal is one a foreman routes around."""
		self.a_fifty_one_hour_week()
		shift = self.a_shift(crew_employees=[], start_datetime=self.at(8))
		message = self.tool_error(
			"add_worker_to_shift",
			{"shift": shift["name"], "employee": MINOR, "joined_at": self.at(9)},
		)
		self.assertIn("50-hour", message)
		self.assertIn("NO work_state IS RECORDED", message)
		self.assertIn("set work_state on the shift", message)

	def a_worked_hours(self, employee: str, hours: float, day_offset: int = 0):
		"""One closed shift of `hours` on the day `day_offset` days from today.

		MINUTE-ACCURATE, because the totals these tests turn on are half-hours —
		an `int(hours)` end time silently rounded 10.5 down to 10 and put the
		week four hours under the figure the test name claimed.
		"""
		day = frappe.utils.add_days(frappe.utils.today(), day_offset)
		name = f"SHIFT-{employee}-{day_offset}"
		finish = 6 * 60 + round(hours * 60)
		ends = f"{day} {finish // 60:02d}:{finish % 60:02d}:00"
		STORE.seed(
			"Farm Shift",
			[
				{
					"name": name,
					"foreman": FOREMAN,
					"company": MAIN,
					"status": "Closed",
					"start_datetime": f"{day} 06:00:00",
					"end_datetime": ends,
					"crew": [
						{
							"name": f"{name}-CREW-1",
							"parent": name,
							"parenttype": "Farm Shift",
							"parentfield": "crew",
							"employee": employee,
							"joined_at": f"{day} 06:00:00",
							"left_at": ends,
						}
					],
				}
			],
		)
		return name


class TheCloseChecksTheHoursThatWereWorked(Wave3TestCase):
	"""S12. The gap between "may be scheduled" and "was worked".

	`add_worker_to_shift` refuses a roster that WOULD breach a ceiling and
	`start_shift` reports one — and both run against a shift with no end time, so
	the span it will add is unknown and nothing is projected. That leaves the
	ordinary case unchecked by anything: ONE SHIFT THAT SIMPLY RAN LONG. A
	fifteen-year-old put on an empty roster at 07:00 and clocked out at 19:30
	passed every check this app made.

	REPORTED, NOT REFUSED, and here the argument is stronger than at
	`start_shift`: the hours are already worked, and refusing the close would
	leave the shift open, unsigned and with no Attendance — destroying the one
	document an investigator asks for, as a punishment for something the refusal
	cannot undo.
	"""

	SIGNATURE = "/files/ada-shift-signature.png"

	def close(self, shift: str, **overrides):
		payload = {
			"shift": shift,
			"end_datetime": self.at(15),
			"supervisor_signature_file_token": self.SIGNATURE,
		}
		payload.update(overrides)
		return self.tool_data("end_shift", payload)

	def test_a_twelve_hour_day_on_a_fifteen_year_old_is_caught_at_the_close(self):
		"""THE CASE NOTHING CAUGHT. One shift, no other hours anywhere, inside the
		clock at both ends — and four hours over the daily ceiling."""
		opened = self.a_shift(crew_employees=[YOUNGER], start_datetime=self.at(7), work_state="OR")
		closed = self.close(opened["name"], end_datetime=self.at(19))
		self.assertEqual(len(closed["minor_limits_exceeded"]), 1)
		self.assertIn("Nina Fifteen", closed["minor_note"])
		self.assertIn("8-hour", closed["minor_note"])

	def test_the_close_still_happens_and_still_writes_its_attendance(self):
		"""The finding does not cost the record. This is the whole design."""
		opened = self.a_shift(crew_employees=[YOUNGER], start_datetime=self.at(7), work_state="OR")
		closed = self.close(opened["name"], end_datetime=self.at(19))
		self.assertIn("minor_limits_exceeded", closed)
		self.assertEqual(STORE.get_raw("Farm Shift", opened["name"])["end_datetime"], self.at(19))
		self.assertTrue(STORE.get_raw("Farm Shift", opened["name"])["supervisor_review_signature"])
		self.assertTrue(closed["attendance_created"])

	def test_the_negative_control_an_adult_works_the_same_twelve_hours(self):
		"""Without this, the finding above is satisfied by any rule about long
		days rather than by one about age."""
		frappe.db.set_value("Employee", WORKER, "date_of_birth", _years_ago(41))
		opened = self.a_shift(crew_employees=[WORKER], start_datetime=self.at(7), work_state="OR")
		closed = self.close(opened["name"], end_datetime=self.at(19))
		self.assertNotIn("minor_limits_exceeded", closed)
		self.assertNotIn("minor_crew_findings", closed)

	def test_a_lawful_eight_hour_day_closes_clean(self):
		opened = self.a_shift(crew_employees=[YOUNGER], start_datetime=self.at(8), work_state="OR")
		closed = self.close(opened["name"], end_datetime=self.at(16))
		self.assertNotIn("minor_limits_exceeded", closed)
		# Still REPORTED as a minor on the crew — the badge, not the breach.
		self.assertEqual(len(closed["minor_crew_findings"]), 1)

	def test_closing_after_the_curfew_is_caught_even_when_the_hours_are_fine(self):
		"""Two and a half hours' work, ending at 21:30. Nowhere near a daily
		ceiling and squarely outside the clock — the two checks are independent
		and a test that only moved the hours could not tell."""
		opened = self.a_shift(crew_employees=[YOUNGER], start_datetime=self.at(19), work_state="OR")
		closed = self.close(opened["name"], end_datetime=self.at(21, 30))
		reasons = " ".join(closed["minor_limits_exceeded"][0]["reasons"])
		self.assertIn("21:30", reasons)
		self.assertIn("19:00", reasons)

	def test_the_state_decides_the_close_too(self):
		"""A 21:30 finish for a fifteen-year-old is outside Oregon's 19:00 and
		inside Washington's 21:00... by half an hour, which is the point: the
		curfews differ and the close reads the shift's own state."""
		oregon = self.a_shift(crew_employees=[YOUNGER], start_datetime=self.at(19), work_state="OR")
		self.assertIn("minor_limits_exceeded", self.close(oregon["name"], end_datetime=self.at(20, 30)))

		washington = self.a_shift(crew_employees=[YOUNGER], start_datetime=self.at(19), work_state="WA")
		closed = self.close(washington["name"], end_datetime=self.at(20, 30))
		self.assertNotIn("minor_limits_exceeded", closed)
