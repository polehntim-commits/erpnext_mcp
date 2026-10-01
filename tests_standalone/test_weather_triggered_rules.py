# SPDX-License-Identifier: MIT
"""v0.22.5 — a compliance rule that fires on a WEATHER FACT rather than on a date.

Every rule this app has ever shipped fires on a distance from a date: a
certificate forty days from expiry, a cabin four hundred days since its walk, a
finding nobody has superseded. `shift_heat_threshold_crossed` fires on neither.
It fires because the latest row of one shift's weather timeline says 82°F, and it
goes quiet because the next row says 75, or because somebody closed the shift.

THE RUNTIME IS STILL DETERMINISTIC AND THAT IS THE WHOLE POINT. No model runs at
sweep time here either. The DATA fires the rule — a stored reading, a stored
threshold — and the rule, which is itself data, says which column to read and
what number to read it against. What the rule does NOT do is decide anything
about compliance: it raises a task for the FOREMAN, who was standing there, and
the record that answers OAR 437-004-1131 is theirs and carries their signature.

SEVEN CLAIMS.

 1. `TheLatestChildGate` — the primitive on its own: the newest row wins, the
    row goes into the message, a subject with no child row is gated out, `any`
    is an OR and `all` is an AND, and a thermometer reading "warm" does not
    cross a threshold by sorting after "80".

 2. `TheStateRole` — a rule with no clock. `default_severity` is not enough on
    its own and the release says why: an Int column cannot tell "no threshold"
    from "a threshold of zero".

 3. `TheSeededRule` — the fourteenth rule, end to end, on a real shift with a
    real timeline.

 4. `TheAlertGoesQuietByItself` — both ways: the shift closes, and the
    temperature drops. Neither needed a new mechanism.

 5. `ThePerCompanyThreshold` — 76°F fires at an entity whose Weather Settings
    say 75 and is silent at one that left the number at 80. Same reading, same
    minute, two answers, and both are right.

 6. `TheProducerTaskGoesToTheForeman` — by name, not into a skill pool; once and
    not twice; and back to the pool where the expression cannot be honoured.

 7. `TheThirteenAreUntouched` — the backward-compat assertion, extended to
    fourteen.
"""

import json

import frappe

from erpnext_mcp import compliance_fields, compliance_rules, shifts
from erpnext_mcp.alerts import engine, sandbox
from erpnext_mcp.services import weather

from .fixtures import MAIN, OTHER, install_hrms
from .harness import STORE
from .test_alerts import ALL_ON, TODAY
from .test_compliance_rule_engine import ALERT_COLUMNS, RULE_TOOLS, RuleEngineTestCase, _plain

RULE = "shift_heat_threshold_crossed"

FOREMAN = "HR-EMP-00001"  # Ada Orchard, Active, at MAIN
WORKER = "HR-EMP-00002"  # Ben Packhouse, Active, at MAIN
SIGNATURE = "/files/ada-shift-signature.png"
GPS = "45.52,-122.68"

SHIFT_TOOLS = {
	f"allow_{name}": 1
	for name in (
		"start_shift",
		"end_shift",
		"get_shift",
		"list_shifts",
		"generate_tasks_from_compliance_alerts",
		"list_dispatched_tasks",
		"create_farm_task",
	)
}


def at(hour: int, minute: int = 0, day: str = TODAY) -> str:
	return f"{day} {hour:02d}:{minute:02d}:00"


class WeatherRuleTestCase(RuleEngineTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, **SHIFT_TOOLS})
		install_hrms()
		compliance_fields.install_compliance_fields(respect_switch=False)

	# ── the world ───────────────────────────────────────────────────────────
	def a_shift(self, **overrides) -> str:
		payload = {
			"foreman": FOREMAN,
			"location": "Block 7 North",
			"shift_type": "Harvest",
			"farm_location_gps": GPS,
			"start_datetime": at(6),
			"crew_employees": [WORKER],
		}
		payload.update(overrides)
		return str(self.tool_data("start_shift", payload)["name"])

	def close(self, shift: str, **overrides) -> dict:
		payload = {
			"shift": shift,
			"end_datetime": at(15),
			"supervisor_signature_file_token": SIGNATURE,
		}
		payload.update(overrides)
		return self.tool_data("end_shift", payload)

	def reading(self, hour: int, temp: float, minute: int = 0, humidity: float = 40.0) -> dict:
		return {
			"reading_datetime": at(hour, minute),
			"temp_f": temp,
			# THE HEAT INDEX IS COMPUTED, never typed, exactly as v0.19.4's service
			# computes it — so a fixture cannot accidentally make the second
			# condition of the gate true while pretending to test the first.
			"heat_index_f": weather.heat_index_f(temp, humidity),
			"humidity_pct": humidity,
			"wind_speed_mph": 3.0,
			"source": weather.SOURCE_CURRENT,
			"fetched_at": frappe.utils.now(),
		}

	def append(self, shift: str, *readings) -> dict:
		return weather.append_readings(shift, list(readings))

	def override(self, company: str, **values) -> None:
		single = dict(STORE.singles.get(weather.SETTINGS_DOCTYPE) or {})
		single.setdefault("doctype", weather.SETTINGS_DOCTYPE)
		single["per_company_overrides"] = [
			*(single.get("per_company_overrides") or []),
			{"company": company, **values},
		]
		STORE.singles[weather.SETTINGS_DOCTYPE] = single

	# ── running it ──────────────────────────────────────────────────────────
	def fired(self, rule_id: str = RULE, company: str = "") -> list:
		"""What the rule observes right now, through the sweep's own code path."""
		return engine.preview(self.rule_named(rule_id), {"today": TODAY, "company": company})["observations"]

	def heat_alerts(self) -> list:
		return [
			dict(row)
			for row in STORE.rows("Compliance Alert")
			if row.get("alert_type") == RULE and not frappe.utils.cint(row.get("dismissed"))
		]

	def heat_alert_rows(self) -> list:
		return [dict(row) for row in STORE.rows("Compliance Alert") if row.get("alert_type") == RULE]

	def tasks(self) -> list:
		return [dict(row) for row in STORE.rows("Farm Task")]


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheLatestChildGate(WeatherRuleTestCase):
	"""The primitive on its own, before any rule is wrapped around it."""

	def a_gate(self, **overrides) -> dict:
		config = {
			"child_doctype": "Farm Shift Weather Reading",
			"parent_field": "parent",
			"parentfield": "weather_timeline",
			"order_by": "reading_datetime",
			"context_key": "latest_weather",
			"conditions": [{"field": "temp_f", "op": "gte", "threshold": 80}],
		}
		config.update(overrides)
		return compliance_rules.parse_latest_child_threshold(config)

	def index(self, config: dict) -> dict:
		return engine._latest_child_index(config, "", [])

	def test_the_newest_row_wins_and_not_the_hottest(self):
		"""The whole reason this is not `gate_related_table` with an extra column.

		A maximum over `reading_datetime` says WHEN the last reading was. A maximum
		over `temp_f` says how hot it got. Neither is the question: what matters is
		the temperature ON the latest row, and folding to a row is the only way to
		have both numbers come off the same reading.
		"""
		shift = self.a_shift()
		self.append(shift, self.reading(9, 88.0), self.reading(13, 72.0))
		latest = self.index(self.a_gate())[shift]
		self.assertEqual(str(latest["reading_datetime"]), at(13))
		self.assertEqual(float(latest["temp_f"]), 72.0)

	def test_a_subject_with_no_child_row_is_gated_out(self):
		shift = self.a_shift()
		row, crossed, passes = engine._child_gate(
			{"name": shift}, self.a_gate(), self.index(self.a_gate()), "", {}
		)
		self.assertFalse(passes)
		self.assertEqual(row, {})
		self.assertEqual(crossed, [])

	def test_any_is_an_or_and_all_is_an_and(self):
		shift = self.a_shift()
		# 79°F at 90% humidity: below the temperature threshold, above the heat
		# index one. The pair is the case the two match modes disagree on.
		self.append(shift, self.reading(11, 79.0, humidity=90.0))
		conditions = [
			{"field": "temp_f", "op": "gte", "threshold": 80},
			{"field": "heat_index_f", "op": "gte", "threshold": 80},
		]
		any_gate = self.a_gate(conditions=conditions, match="any")
		all_gate = self.a_gate(conditions=conditions, match="all")
		candidate = {"name": shift}
		self.assertTrue(engine._child_gate(candidate, any_gate, self.index(any_gate), "", {})[2])
		self.assertFalse(engine._child_gate(candidate, all_gate, self.index(all_gate), "", {})[2])

	def test_a_thermometer_holding_text_does_not_cross_a_threshold(self):
		"""`passes_threshold` is numeric-only, and this is why it is not `_passes`.

		`_passes` compares numerically where BOTH sides are numbers and falls back
		to a LEXICAL comparison otherwise, which is right for a scope filter: the
		values reaching an ordering comparison there are overwhelmingly ISO dates,
		and ISO dates sort correctly as strings. It is exactly wrong on a
		thermometer. A reading whose column somebody filled in as "warm" is
		lexically greater than "80" — and would raise a heat alert off a word.
		"""
		self.assertTrue(compliance_rules._passes("warm", "gte", "80"), "the filter engine still falls back")
		self.assertFalse(compliance_rules.passes_threshold("warm", "gte", 80))
		self.assertTrue(compliance_rules.passes_threshold("82", "gte", 80))
		self.assertTrue(compliance_rules.passes_threshold(80.0, "gte", 80))
		self.assertFalse(compliance_rules.passes_threshold(None, "gte", 80))

	def test_a_reading_with_no_temperature_is_not_a_cool_reading(self):
		shift = self.a_shift()
		self.append(shift, {**self.reading(11, 95.0), "temp_f": None, "heat_index_f": None})
		gate = self.a_gate()
		self.assertFalse(engine._child_gate({"name": shift}, gate, self.index(gate), "", {})[2])

	def test_parentfield_narrows_the_read_to_the_right_table(self):
		"""A child DOCTYPE can hang off more than one table on more than one parent.

		Without this filter the gate would read another table's rows as if they
		were this one's — which is how a rule about a weather timeline starts
		answering questions about a crew list.
		"""
		shift = self.a_shift()
		self.append(shift, self.reading(11, 95.0))
		self.assertIn(shift, self.index(self.a_gate(parentfield="weather_timeline")))
		self.assertEqual(self.index(self.a_gate(parentfield="crew")), {})

	def test_scope_filters_narrow_which_child_rows_count(self):
		shift = self.a_shift()
		self.append(shift, self.reading(9, 95.0), {**self.reading(13, 70.0), "source": "manual"})
		gate = self.a_gate(scope_filters=[{"field": "source", "op": "eq", "value": weather.SOURCE_CURRENT}])
		self.assertEqual(float(self.index(gate)[shift]["temp_f"]), 95.0)

	# ── refusals, at authoring time ─────────────────────────────────────────
	def test_it_refuses_a_gate_with_no_order_by(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_latest_child_threshold(
				{"child_doctype": "Farm Shift Weather Reading", "conditions": [{"field": "temp_f"}]}
			)
		self.assertIn("order_by", str(caught.exception))

	def test_it_refuses_a_gate_with_no_conditions(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_latest_child_threshold(
				{"child_doctype": "Farm Shift Weather Reading", "order_by": "reading_datetime"}
			)
		self.assertIn("gates NOTHING IN", str(caught.exception))

	def test_it_refuses_a_condition_with_no_number(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_latest_child_threshold(
				{
					"child_doctype": "Farm Shift Weather Reading",
					"order_by": "reading_datetime",
					"conditions": [{"field": "temp_f", "op": "gte"}],
				}
			)
		self.assertIn("no number to compare against", str(caught.exception))

	def test_it_refuses_an_operator_that_is_not_a_comparison(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_latest_child_threshold(
				{
					"child_doctype": "Farm Shift Weather Reading",
					"order_by": "reading_datetime",
					"conditions": [{"field": "temp_f", "op": "contains", "threshold": 80}],
				}
			)
		self.assertIn("number read against a number", str(caught.exception))

	def test_it_refuses_a_threshold_source_this_app_does_not_resolve(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_latest_child_threshold(
				{
					"child_doctype": "Farm Shift Weather Reading",
					"order_by": "reading_datetime",
					"conditions": [
						{"field": "temp_f", "op": "gte", "threshold_source": "weather.whatever_i_like"}
					],
				}
			)
		self.assertIn("not a setting this app resolves", str(caught.exception))

	def test_the_parser_accepts_its_own_output(self):
		"""Parsed on save, stored, parsed again on every sweep. A parser whose
		output it could not read is a rule that validates once and then quietly
		matches nothing, which is the worst failure available to a rule."""
		once = self.a_gate()
		self.assertEqual(compliance_rules.parse_latest_child_threshold(json.dumps(once)), once)

	def test_the_flat_single_condition_shape_is_accepted(self):
		flat = compliance_rules.parse_latest_child_threshold(
			{
				"child_doctype": "Farm Shift Weather Reading",
				"order_by": "reading_datetime",
				"field": "temp_f",
				"op": "gte",
				"threshold": 80,
			}
		)
		self.assertEqual(len(flat["conditions"]), 1)
		self.assertEqual(flat["conditions"][0]["field"], "temp_f")


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheStateRole(WeatherRuleTestCase):
	"""A rule with no clock, and why `default_severity` alone was not enough."""

	def test_a_state_rule_raises_at_its_default_severity_and_bands_nothing(self):
		for severity in ("Info", "Warning", "Critical"):
			with self.subTest(severity=severity):
				self.assertEqual(
					engine._band(
						None,
						{},
						"",
						critical_days=30,
						warning_days=90,
						severity_critical="Critical",
						severity_warning="Warning",
						severity_expired="Critical",
						state_severity=severity,
					),
					(severity, "state", 0),
				)

	def test_the_state_severity_outranks_even_the_per_row_window(self):
		"""The window is the one thing that outranks everything on a CLOCK. A
		state-driven rule has no clock for it to outrank, so it must not be able to
		silence one — a shift is not less hot for having a short lead time."""
		severity, band, _window = engine._band(
			400,
			{"renewal_window_days": 1},
			"renewal_window_days",
			critical_days=30,
			warning_days=90,
			severity_critical="Critical",
			severity_warning="Warning",
			severity_expired="Critical",
			state_severity="Warning",
		)
		self.assertEqual((severity, band), ("Warning", "state"))

	def test_without_the_role_the_same_thresholds_would_have_said_critical(self):
		"""The argument for a third `date_field_role` value, as a test.

		`threshold_*_days` are Int columns, so "no threshold" and "a threshold of
		zero" are ONE VALUE in the database — and zero is a real setting meaning
		"fire on the due date itself". A shift that started this morning is zero
		days from its own start, so a rule read as a clock says Critical about a
		crew who are merely at work. Nothing the engine can read off the numbers
		distinguishes the two cases; the rule has to say which it is.
		"""
		as_a_clock = engine._band(
			0,
			{},
			"",
			critical_days=0,
			warning_days=0,
			severity_critical="Critical",
			severity_warning="Warning",
			severity_expired="Critical",
		)
		self.assertEqual(as_a_clock[0], "Critical")

	def test_a_state_rule_with_no_date_on_the_row_still_raises(self):
		"""`missing_date_behaviour` is about a deadline. A state rule has none, so
		a row with an empty anchor is not a row that has nothing to say."""
		shift = self.a_shift()
		self.append(shift, self.reading(11, 92.0))
		# Nulled AFTER the readings are on, because the shift will not save without
		# a start — which is itself the reason this row can only arrive by import.
		STORE.get_raw(shifts.DOCTYPE, shift)["start_datetime"] = None
		self.seed_rules()
		self.assertEqual(len(self.fired()), 1)

	def test_the_seeded_rule_reports_itself_as_state_driven(self):
		self.seed_rules()
		described = self.tool_data("get_compliance_rule", {"name": RULE})
		self.assertTrue(described["state_driven"])
		self.assertEqual(described["default_severity"], "Warning")
		self.assertEqual(described["definition"]["date_field_role"], compliance_rules.DATE_ROLE_STATE)
		self.assertEqual(described["shape"], compliance_rules.SHAPE_DECLARATIVE)

	def test_list_compliance_rules_carries_the_new_fields(self):
		self.seed_rules()
		rules = {row["alert_type"]: row for row in self.tool_data("list_compliance_rules", {})["rules"]}
		self.assertTrue(rules[RULE]["state_driven"])
		self.assertEqual(rules[RULE]["producer_assigned_to_expression"], "row.foreman")
		# Additive: the twelve that are not state-driven say so rather than
		# omitting the key, so a client can read one shape for every rule.
		self.assertFalse(rules["training_expiring"]["state_driven"])
		self.assertIsNone(rules["training_expiring"]["producer_assigned_to_expression"])


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheSeededRule(WeatherRuleTestCase):
	def test_an_open_shift_at_eighty_two_degrees_fires_warning(self):
		shift = self.a_shift()
		self.append(shift, self.reading(11, 82.0))
		self.seed_rules()

		observations = self.fired()
		self.assertEqual(len(observations), 1)
		self.assertEqual(observations[0]["severity"], "Warning")
		self.assertEqual(observations[0]["source_doctype"], shifts.DOCTYPE)
		self.assertEqual(observations[0]["source_docname"], shift)
		self.assertEqual(observations[0]["category"], "Workforce")
		self.assertEqual(observations[0]["regimes"], ["OR-OSHA"])
		# NO DUE DATE. There is no deadline — the obligation is now, and a date on
		# the calendar would put this in a queue with things that are due next week.
		self.assertIsNone(observations[0]["due_date"])

	def test_the_message_names_the_reading_that_fired_it(self):
		"""An alert that says a threshold was crossed and cannot say WHAT the
		reading was is an alert somebody has to go and look up before acting."""
		shift = self.a_shift()
		self.append(shift, self.reading(11, 82.0, humidity=55.0))
		self.seed_rules()
		message = self.fired()[0]["message"]
		self.assertIn(shift, message)
		self.assertIn("Block 7 North", message)
		self.assertIn("82.0°F", message)
		self.assertIn(at(11), message)
		self.assertIn("OAR 437-004-1131", message)

	def test_a_shift_with_no_readings_does_not_fire(self):
		"""A shift with an empty timeline is not a cool shift. It is a shift nobody
		has a reading for, and raising off no reading would be this app asserting a
		fact it does not have."""
		self.a_shift()
		self.seed_rules()
		self.assertEqual(self.fired(), [])

	def test_a_cool_shift_does_not_fire(self):
		shift = self.a_shift()
		self.append(shift, self.reading(11, 71.0))
		self.seed_rules()
		self.assertEqual(self.fired(), [])

	def test_a_closed_shift_does_not_fire_however_hot_it_was(self):
		shift = self.a_shift()
		self.append(shift, self.reading(11, 99.0))
		self.close(shift)
		self.seed_rules()
		self.assertEqual(self.fired(), [])

	def test_the_dry_run_reports_what_would_be_observed_and_writes_nothing(self):
		shift = self.a_shift()
		self.append(shift, self.reading(11, 84.0))
		self.seed_rules()

		data = self.tool_data("test_compliance_rule", {"name": RULE, "dry_run": True})
		self.assertEqual(data["observed"], 1)
		self.assertEqual(data["observations"][0]["source_docname"], shift)
		self.assertEqual(data["observations"][0]["severity"], "Warning")
		self.assertIn("NOTHING WAS WRITTEN", data["note"])
		self.assertEqual(STORE.rows("Compliance Alert"), [])

	def test_the_kairotic_gate_and_the_citation_are_on_the_record(self):
		"""The two fields an auditor reads, and the reason the record is the answer
		to "why did this fire" rather than a git history nobody has access to."""
		self.seed_rules()
		row = self.rule_named(RULE)
		self.assertIn("Fires on a WEATHER FACT, not a date", row["kairotic_gate_description"])
		self.assertEqual(row["regulation_citations"], "OAR 437-004-1131 heat illness prevention")
		self.assertEqual(int(row["retention_years"]), 3)
		self.assertEqual(row["authored_by"], compliance_rules.AUTHOR_SYSTEM)
		self.assertEqual(row["human_approved_by"], "Administrator")

	def test_the_sweep_raises_and_refreshes_one_alert_and_not_two(self):
		shift = self.a_shift()
		self.append(shift, self.reading(11, 82.0))
		self.seed_rules()

		self.sweep(today=TODAY)
		self.assertEqual(len(self.heat_alerts()), 1)
		first = self.heat_alerts()[0]["name"]

		# A second reading, still hot. The docname carries the rule and the record
		# and nothing that moves, so the same alert is refreshed rather than a
		# second one raised beside it.
		self.append(shift, self.reading(11, 83.0, minute=15))
		self.sweep(today=TODAY)
		self.assertEqual([row["name"] for row in self.heat_alerts()], [first])


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheAlertGoesQuietByItself(WeatherRuleTestCase):
	"""Both directions, and NEITHER needed a new mechanism.

	The sweep auto-dismisses every alert of a rule whose key the rule did not
	observe on this run. A shift that closed falls out of the scope filters; a
	shift that cooled falls out of the gate. In both cases the rule simply does
	not observe it, and the standard path does the rest.
	"""

	def a_hot_shift(self) -> str:
		shift = self.a_shift()
		self.append(shift, self.reading(11, 82.0))
		self.seed_rules()
		self.sweep(today=TODAY)
		self.assertEqual(len(self.heat_alerts()), 1)
		return shift

	def test_it_dismisses_itself_when_the_temperature_drops(self):
		shift = self.a_hot_shift()
		self.append(shift, self.reading(15, 75.0))
		report = self.sweep(today=TODAY)

		self.assertEqual(self.heat_alerts(), [])
		dismissed = self.heat_alert_rows()[0]
		self.assertTrue(frappe.utils.cint(dismissed["auto_dismissed"]))
		self.assertIsNone(dismissed.get("dismissed_reason"))
		self.assertGreaterEqual(report["auto_dismissed"], 1)

	def test_it_dismisses_itself_when_the_shift_closes(self):
		shift = self.a_hot_shift()
		self.close(shift)
		self.sweep(today=TODAY)
		self.assertEqual(self.heat_alerts(), [])
		self.assertTrue(frappe.utils.cint(self.heat_alert_rows()[0]["auto_dismissed"]))

	def test_it_comes_back_on_the_same_key_when_the_heat_does(self):
		"""An alert is a statement about the present, not a task somebody closed.
		The afternoon warms up again and the SAME row is un-dismissed — so the four
		hours it has already been open are still visible on it."""
		shift = self.a_hot_shift()
		key = self.heat_alerts()[0]["name"]
		first_seen = self.heat_alerts()[0]["first_seen"]

		self.append(shift, self.reading(15, 75.0))
		self.sweep(today=TODAY)
		self.assertEqual(self.heat_alerts(), [])

		self.append(shift, self.reading(16, 88.0))
		self.sweep(today=TODAY)
		back = self.heat_alerts()
		self.assertEqual([row["name"] for row in back], [key])
		self.assertEqual(back[0]["first_seen"], first_seen)


# ── 5 ───────────────────────────────────────────────────────────────────────
class ThePerCompanyThreshold(WeatherRuleTestCase):
	"""One reading, two entities, two right answers.

	The number is read from Weather Settings — the same place the v0.19.4 shift
	sweep reads it — and NOT from a literal on the rule. A literal would make the
	alert layer and the operational layer disagree about what hot means on the
	same afternoon, on the same shift, and the disagreement would be invisible
	until somebody compared two records.
	"""

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Employee",
			[
				{
					"name": "HR-EMP-09001",
					"employee_name": "Other Foreman",
					"status": "Active",
					"date_of_joining": "2026-06-01",
					"company": OTHER,
				}
			],
		)

	def test_an_entity_that_lowered_its_threshold_fires_at_seventy_six(self):
		self.override(MAIN, heat_threshold_temp_f=75.0, heat_threshold_heat_index_f=75.0)
		shift = self.a_shift()
		self.append(shift, self.reading(11, 76.0))
		self.seed_rules()
		self.assertEqual(len(self.fired()), 1)

	def test_the_same_reading_at_an_entity_on_the_default_is_silent(self):
		self.override(MAIN, heat_threshold_temp_f=75.0, heat_threshold_heat_index_f=75.0)
		mine = self.a_shift()
		theirs = self.a_shift(foreman="HR-EMP-09001", company=OTHER, crew_employees=[])
		self.append(mine, self.reading(11, 76.0))
		self.append(theirs, self.reading(11, 76.0))
		self.seed_rules()

		fired = {row["source_docname"] for row in self.fired()}
		self.assertEqual(fired, {mine})

	def test_the_literal_on_the_rule_is_the_floor_the_setting_falls_back_to(self):
		"""A site whose Weather Settings have not migrated gets the REGULATION's
		number rather than nothing. That is the difference between a rule that is
		conservative and a rule that is silent."""
		self.assertIsNone(compliance_rules.threshold_from_source("weather.nonsense", MAIN))
		config = compliance_rules.parse_latest_child_threshold(
			{
				"child_doctype": "Farm Shift Weather Reading",
				"order_by": "reading_datetime",
				"conditions": [
					{
						"field": "temp_f",
						"op": "gte",
						"threshold": 80,
						"threshold_source": "weather.heat_threshold_temp_f",
					}
				],
			}
		)
		shift = self.a_shift()
		self.append(shift, self.reading(11, 82.0))
		index = engine._latest_child_index(config, "", [])
		# `cache` pre-populated with an unreadable setting, which is what a site
		# mid-migrate hands back.
		_row, crossed, passes = engine._child_gate(
			{"name": shift, "company": MAIN},
			config,
			index,
			"company",
			{MAIN: {"weather.heat_threshold_temp_f": None}},
		)
		self.assertTrue(passes)
		self.assertEqual(crossed[0]["threshold"], 80)

	def test_the_thresholds_are_resolved_once_per_company_and_not_once_per_row(self):
		self.override(MAIN, heat_threshold_temp_f=75.0)
		limits = weather.thresholds_for(MAIN)
		self.assertEqual(limits["heat_threshold_temp_f"], 75.0)
		self.assertEqual(weather.thresholds_for(OTHER)["heat_threshold_temp_f"], 80.0)
		self.assertEqual(compliance_rules.threshold_from_source("weather.heat_threshold_temp_f", MAIN), 75.0)
		self.assertEqual(compliance_rules.threshold_from_source("weather.heat_threshold_temp_f", OTHER), 80.0)


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheProducerTaskGoesToTheForeman(WeatherRuleTestCase):
	def a_fired_alert(self) -> str:
		shift = self.a_shift()
		self.append(shift, self.reading(11, 84.0))
		self.seed_rules()
		self.sweep(today=TODAY)
		self.assertEqual(len(self.heat_alerts()), 1)
		return shift

	def generate(self, **overrides) -> dict:
		payload = {"alert_types": [RULE]}
		payload.update(overrides)
		return self.tool_data("generate_tasks_from_compliance_alerts", payload)

	def test_the_task_is_assigned_to_the_shifts_own_foreman(self):
		shift = self.a_fired_alert()
		report = self.generate()

		self.assertEqual(len(report["created"]), 1)
		entry = report["created"][0]
		self.assertEqual(entry["assigned_to"], FOREMAN)
		self.assertEqual(entry["dispatch_mode"], "Dispatched")
		# EITHER/OR. A skill is a POOL and an assignee is a PERSON; a task carrying
		# both would show up in the pool listing beside the person already holding it.
		self.assertEqual(entry["skill_required"], "")

		task = frappe.db.get_value(
			"Farm Task",
			entry["task"],
			["assigned_to", "state", "dispatch_mode", "skill_required"],
			as_dict=True,
		)
		self.assertEqual(task["assigned_to"], FOREMAN)
		self.assertEqual(task["state"], "Claimed")
		self.assertEqual(str(task["skill_required"] or ""), "")
		self.assertIn(shift, str(entry["task_name"]))

	def test_it_lands_on_that_foremans_own_list_and_not_in_the_pool(self):
		self.a_fired_alert()
		self.generate()
		mine = self.tool_data("list_dispatched_tasks", {"assigned_to": FOREMAN})
		self.assertEqual(len(mine["assignments"]), 1)
		pool = self.tool_data("list_available_tasks", {})
		self.assertEqual(pool["tasks"], [], "a task with a named holder must not also sit in the pool")

	def test_the_errand_is_a_sentence_and_not_the_rules_title(self):
		"""The rule's title is a STATEMENT — "an open shift's latest reading has
		crossed the threshold". A task name has to be an errand somebody can do."""
		self.a_fired_alert()
		entry = self.generate()["created"][0]
		self.assertIn("Document the water, shade and rest cycle", entry["task_name"])

	def test_the_evidence_contract_asks_for_the_foremans_own_signature(self):
		self.a_fired_alert()
		entry = self.generate()["created"][0]
		self.assertEqual(entry["evidence_required"], {"findings_text": True, "signature": True})

	def test_a_second_run_raises_no_second_task(self):
		self.a_fired_alert()
		first = self.generate()
		second = self.generate()
		self.assertEqual(len(first["created"]), 1)
		self.assertEqual(second["created"], [])
		self.assertEqual(len(second["skipped_already_answered"]), 1)
		self.assertEqual(len(self.tasks()), 1)

	def test_a_dry_run_says_who_it_would_go_to_and_writes_nothing(self):
		self.a_fired_alert()
		report = self.generate(dry_run=True)
		self.assertEqual(report["created"][0]["assigned_to"], FOREMAN)
		self.assertEqual(self.tasks(), [])

	def test_an_expression_naming_nobody_falls_back_to_the_pool_and_says_so(self):
		"""NEVER `Dispatched` WITH NOBODY ON IT. That combination is a task sitting
		in Available that no worker is allowed to claim — visible, urgent and
		unreachable, which is the worst of the three ways for dispatch to fail."""
		shift = self.a_fired_alert()
		STORE.get_raw(shifts.DOCTYPE, shift)["foreman"] = ""
		entry = self.generate()["created"][0]
		self.assertIsNone(entry["assigned_to"])
		self.assertNotEqual(entry["dispatch_mode"], "Dispatched")
		self.assertTrue(entry["routing_notes"])
		self.assertIn("routes by skill", entry["routing_notes"][0])

	def test_an_expression_naming_somebody_payroll_has_never_heard_of_falls_back(self):
		shift = self.a_fired_alert()
		STORE.get_raw(shifts.DOCTYPE, shift)["foreman"] = "HR-EMP-99999"
		entry = self.generate()["created"][0]
		self.assertIsNone(entry["assigned_to"])
		self.assertIn("not an Employee on this site", " ".join(entry["routing_notes"]))

	def test_the_thirteen_shipped_rules_still_route_out_of_the_reviewed_table(self):
		"""`ALERT_TASK_MAP` stays FIRST, and that ordering is the backward-compat
		guarantee: the shipped rules produce exactly the tasks they produced in
		v0.22.1 whatever a site has since edited onto their records."""
		from erpnext_mcp.tools.dispatch import ALERT_TASK_MAP, _recipe_for

		self.seed_rules()
		for alert_type, recipe in sorted(ALERT_TASK_MAP.items()):
			with self.subTest(rule=alert_type):
				self.assertIs(_recipe_for(alert_type), recipe)

	def test_a_rule_with_no_recipe_anywhere_is_still_reported_by_name(self):
		"""None still means "reported by name, not turned into a generic task".

		A task with a made-up evidence contract is worse than no task: it produces
		a compliance record nobody can rely on. So BOTH halves — a task type and an
		evidence contract — are required before an alert becomes work, whether they
		come from the table or from the record.
		"""
		from erpnext_mcp.tools.dispatch import _recipe_for

		self.seed_rules()
		self.assertIsNone(_recipe_for("no_such_rule"))

		name = compliance_rules.resolve(RULE)
		STORE.get_raw(compliance_rules.DOCTYPE, name)["evidence_contract_json"] = "{}"
		self.assertIsNone(_recipe_for(RULE))
		STORE.get_raw(compliance_rules.DOCTYPE, name)["evidence_contract_json"] = json.dumps(
			{"findings_text": True}
		)
		STORE.get_raw(compliance_rules.DOCTYPE, name)["producer_farm_task_type"] = ""
		self.assertIsNone(_recipe_for(RULE))

	# ── the expression itself ───────────────────────────────────────────────
	def test_the_expression_runs_in_the_same_sandbox_as_custom_python(self):
		self.assertEqual(sandbox.evaluate("row.foreman", {"row": {"foreman": FOREMAN}}), FOREMAN)
		self.assertEqual(sandbox.evaluate("row.foreman or row.owner", {"row": {"owner": "x"}}), "x")
		self.assertIsNone(sandbox.evaluate("", {"row": {}}))
		with self.assertRaises(sandbox.SandboxError):
			sandbox.evaluate("__import__('os').system('ls')", {"row": {}})
		with self.assertRaises(sandbox.SandboxError):
			sandbox.evaluate("row.__class__", {"row": {}})

	def test_a_rule_naming_both_a_skill_and_an_assignee_is_refused_at_authoring_time(self):
		self.seed_rules()
		message = self.tool_error(
			"update_compliance_rule",
			{
				"name": RULE,
				"producer_skill_required": "hr_admin",
				"reason": "trying to have it both ways",
			},
		)
		self.assertIn("A skill is a POOL and an assignee is a PERSON", message)

	def test_an_assignee_expression_the_sandbox_refuses_is_refused_at_authoring_time(self):
		self.seed_rules()
		message = self.tool_error(
			"update_compliance_rule",
			{
				"name": RULE,
				"producer_assigned_to_expression": "row.__class__",
				"reason": "reaching out of the sandbox",
			},
		)
		self.assertIn("sandbox refused", message)

	def test_a_producer_task_type_no_farm_task_will_accept_is_refused(self):
		self.seed_rules()
		message = self.tool_error(
			"update_compliance_rule",
			{
				"name": RULE,
				"producer_farm_task_type": "Document Heat Response",
				"reason": "a sentence where a select value goes",
			},
		)
		self.assertIn("not one of the Farm Task types", message)


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheThirteenAreUntouched(WeatherRuleTestCase):
	"""The v0.22.0 and v0.22.1 promise, made a third time.

	An operation upgrading to v0.22.5 gets the same alerts, with the same
	docnames, severities, due dates and words, as it got the night before — plus
	one new rule that fires on records the other thirteen never looked at.
	"""

	def test_the_whole_calendar_is_identical_with_the_fourteenth_rule_switched_off(self):
		self.a_whole_operation()
		self.seed_rules()
		self.sweep(today=TODAY)
		with_it = self.alert_snapshot()

		self.wipe_alerts()
		self.tool_data(
			"deactivate_compliance_rule",
			{"name": RULE, "reason": "asserting the other thirteen do not depend on it"},
		)
		self.sweep(today=TODAY)
		without_it = self.alert_snapshot()

		self.assertEqual(sorted(with_it), sorted(without_it))
		for name in sorted(with_it):
			with self.subTest(alert=name):
				self.assertEqual(with_it[name], without_it[name])

	def test_the_fourteenth_rule_joins_the_snapshot_field_by_field(self):
		"""The parity classes above compare thirteen rules against the Python they
		replaced. This one has no Python to be compared against — it never had a
		scanner — so what is pinned is the row itself, every column of it."""
		shift = self.a_shift()
		self.append(shift, self.reading(11, 82.0, humidity=55.0))
		self.seed_rules()
		self.sweep(today=TODAY)

		row = next(row for row in STORE.rows("Compliance Alert") if row["alert_type"] == RULE)
		snapshot = {key: _plain(row.get(key)) for key in ALERT_COLUMNS}
		self.assertEqual(
			snapshot,
			{
				"name": f"{RULE}:{shifts.DOCTYPE}:{shift}",
				"alert_key": f"{RULE}:{shifts.DOCTYPE}:{shift}",
				"alert_type": RULE,
				"severity": "Warning",
				"category": "Workforce",
				"company": MAIN,
				"source_doctype": shifts.DOCTYPE,
				"source_docname": shift,
				"alert_message": (
					f"Heat threshold crossed on {shift} at Block 7 North — latest reading 82.0°F "
					f"(83.6°F heat index) at {at(11)}. Document water/shade/rest breaks per "
					f"OAR 437-004-1131."
				),
				"due_date": None,
				"first_seen": TODAY,
				"dismissed": "0",
				"auto_dismissed": "0",
			},
		)

	def test_the_v0194_weather_sweep_still_logs_its_own_threshold_event(self):
		"""TWO SYSTEMS OBSERVING THE SAME FACT AT DIFFERENT CADENCES, and neither
		is the other's deduplication. The weather sweep writes an operational event
		ON THE SHIFT, keyed on the shift; the rule sweep raises an ALERT, keyed on
		the rule and the shift. Both are idempotent by their own key, so running
		them in either order, or twice, produces one of each.
		"""
		shift = self.a_shift()
		reading = self.reading(11, 92.0)
		self.append(shift, reading)
		row = dict(STORE.get_raw(shifts.DOCTYPE, shift))
		weather.evaluate_thresholds(row, reading)
		weather.evaluate_thresholds(dict(STORE.get_raw(shifts.DOCTYPE, shift)), reading)

		events = [
			event
			for event in (STORE.get_raw(shifts.DOCTYPE, shift).get("compliance_events") or [])
			if event.get("event_type") == weather.THRESHOLD_EVENT
		]
		self.assertEqual(
			len(events), 1, "the shift's own event deduped on the shift, as it has since v0.19.4"
		)

		self.seed_rules()
		self.sweep(today=TODAY)
		self.sweep(today=TODAY)
		self.assertEqual(len(self.heat_alerts()), 1, "the alert deduped on rule_id + shift docname")

	def test_a_re_migrate_writes_no_duplicate_and_keeps_an_operator_edit(self):
		"""The seeder's contract, checked on the one rule it has never seen before.

		A Frappe fixture would correct an operator's edit back on every upgrade,
		which is why this app has never shipped one. The check is "does ANY row
		hold this rule_id" — not "does a live row" — so a rule somebody superseded
		with their own v2 does not get v1 seeded back beside it every migrate.
		"""
		self.seed_rules()
		self.tool_data(
			"update_compliance_rule",
			{
				"name": RULE,
				"default_severity": "Critical",
				"reason": "this operation treats any crossing as a stop-work",
			},
		)
		again = compliance_rules.seed_compliance_rules()
		self.assertEqual(again["created"], [])
		self.assertIn(RULE, again["present"])

		live = [row for row in compliance_rules.rule_rows() if row["rule_id"] == RULE]
		self.assertEqual(len(live), 1)
		self.assertEqual(live[0]["default_severity"], "Critical")
		self.assertEqual(int(live[0]["version"]), 2)

	def test_the_split_is_twentyfour_declarative_six_builtin_and_no_custom_python(self):
		self.seed_rules()
		shapes: dict = {}
		for row in compliance_rules.rule_rows():
			# v0.80.0. GATES ARE NOT ONE OF THE THREE SHAPES, and this is the
			# second place that has to say so. `shape_of` reads "declarative" off a
			# gate rule because a gate carries no scanner — but the shapes describe
			# how the SWEEP runs a rule, and a rule with a `control_point` is never
			# swept: it is consulted at the moment of a transaction. Counting the
			# IPO-readiness controls here would quietly restate this test's claim
			# about the seeded population as something it does not mean.
			if row.get("control_point"):
				continue
			shapes.setdefault(compliance_rules.shape_of(row), []).append(row["rule_id"])
		# Twenty-one since v0.55.0: the three record-only missing-signature rules
		# each ask one question of one signature column on one row, which is the
		# declarative vocabulary working as intended rather than being stretched.
		# Twenty-two since v0.56.0: `tax_form_signature_missing` is one more
		# question about one more signature column, on a fourth doctype.
		# Twenty-four since v0.69.0: `rei_active_block_entry` and
		# `phi_harvest_window` each ask whether one stamped window is still open,
		# which needed a datetime template variable rather than a new shape.
		self.assertEqual(len(shapes[compliance_rules.SHAPE_DECLARATIVE]), 24)
		# Three since v0.39.0: `financial_kpi_threshold_breach` joined the two
		# permanent built-ins, and it is permanent for a reason neither of those
		# has — its thresholds are not on its own row, they are on each
		# Financial KPI Definition, per KPI and per unit. Four since v0.42.0:
		# `budget_variance_breach` is permanent for the same class of reason —
		# its thresholds are on each Budget Line Item and Budget KPI Target,
		# and the comparison is against every child row on a budget at once.
		# Five since v0.55.0: `i9_supplement_b_unsigned` folds an I-9's
		# reverification CHILD TABLE to a count and a newest date, and the one
		# primitive that folds a child table compares numbers by design.
		# Six since v0.69.0: `item_below_reorder` reads a balance on Bin against a
		# level on Item's child table, which is a join rather than a filter.
		# Eight since v0.212.0: `training_day_missed` joins an attendee's registration
		# to that person's rows on every day of the course.
		self.assertEqual(len(shapes[compliance_rules.SHAPE_BUILTIN]), 8)
		self.assertEqual(shapes.get(compliance_rules.SHAPE_CUSTOM, []), [])
