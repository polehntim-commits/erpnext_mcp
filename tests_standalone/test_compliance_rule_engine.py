# SPDX-License-Identifier: MIT
"""The Configurable Compliance Framework — v0.22.0.

THE ONE TEST THAT MATTERS MOST IS `TheMigrationChangesNothing`. Everything else
here checks a feature; that class checks a PROMISE, and it is the promise the
whole release rests on: an operation upgrading to v0.22.0 gets the same alerts,
with the same docnames, the same severities, the same due dates and the same
words, as it got the night before. A compliance calendar that quietly changed
what it said during an upgrade would be worse than one that stopped working,
because a calendar that stops working is noticed.

It is checked the only way that means anything: build one fixed database, run
the sweep with the SHIPPED Python rules, snapshot every alert row, delete the
alerts, seed the thirteen Compliance Rule records, run the sweep again — now
through the record-driven engine — and compare the two snapshots field by field.
Not counts. Not "an alert of this type exists". The rows.

The rest of the file is the framework: the declarative primitives one at a time,
the sandbox refusing what it is supposed to refuse, versioning by copy, the
approval gate, and the seeder's idempotency.
"""

import json
import unittest

import frappe

from erpnext_mcp import alerts, compliance_rules, enforcement
from erpnext_mcp.alerts import engine, sandbox

from .fixtures import MAIN
from .harness import STORE
from .test_alerts import ALL_ON, TODAY, AlertTestCase, days_from_today

#: The rules that are SWEPT — walked nightly against the site's records. Thirty
#: since v0.69.0 and THIRTY-ONE since v0.98.0, which added
#: `minor_hours_approaching`; the number is spelled out rather than derived
#: because it is the claim these tests exist to make: every rule this app ships
#: became a record, one for one, and a release that drops one should fail here
#: loudly.
SWEPT_RULES = 31

#: v0.80.0. The rules that are GATES — consulted at the moment of a transaction
#: rather than swept. Derived from `enforcement.CONTROL_POINTS` rather than
#: written out, because unlike the thirty these are expected to grow with each
#: control an IPO-readiness phase adds, and a hardcoded total would make every
#: such addition look like a regression in a test about something else.
GATE_RULES = len(enforcement.CONTROL_POINTS)

#: v0.203.0. The four rodent bait rules, seeded DISABLED (Tim switches the program on).
RODENT_RULES = 5

#: What the seeder writes on a fresh site, all told.
ALL_RULES = SWEPT_RULES + GATE_RULES + RODENT_RULES

#: The rows that are LIVE after a fresh seed: the rodent rules and the rodent
#: pre-occupancy gate arrive switched off.
LIVE_RULES = ALL_RULES - RODENT_RULES - len(enforcement._NON_FINANCE)

RULE_TOOLS = {
	f"allow_{name}": 1
	for name in (
		"record_training",
		"list_compliance_rules",
		"get_compliance_rule",
		"create_compliance_rule",
		"approve_compliance_rule",
		"update_compliance_rule",
		"deactivate_compliance_rule",
		"test_compliance_rule",
		"propose_compliance_rule",
	)
}

#: The fields an alert row is compared on. Everything the sweep writes and
#: nothing it does not: `last_refreshed` is a wall-clock stamp and would differ
#: between two runs of an identical sweep, which is the one difference that
#: means nothing.
ALERT_COLUMNS = (
	"name",
	"alert_key",
	"alert_type",
	"severity",
	"category",
	"company",
	"source_doctype",
	"source_docname",
	"alert_message",
	"due_date",
	"first_seen",
	"dismissed",
	"auto_dismissed",
)


class RuleEngineTestCase(AlertTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS})

	# ── the world every parity test is run against ──────────────────────────
	def a_whole_operation(self) -> None:
		"""One fixed database that makes every one of the thirteen rules speak.

		Deliberately built from the SAME helpers `test_alerts.py` uses, so the
		two files cannot drift into testing different fixtures and calling it
		the same rule.
		"""
		self.a_certificate(expires_in_days=45)
		self.a_certificate("Applicator License 2026", cert_type="Applicator License", expires_in_days=-12)
		self.a_policy(review_in_days=-30)
		self.a_block()
		self.a_cabin()
		self.an_employee(i9="Expired", flc_license_expiration=days_from_today(20))
		for row in STORE.rows("Employee"):
			row.setdefault("w4_status", "Active")
		self.a_filing(response_due_in_days=-4)
		self.an_audit(due_in_days=-25)
		self.a_training(expires_in_days=12)

	def a_cabin(self, name="MC-Cabin-01", inspected_days_ago=400, detector_days_ago=400, **overrides):
		"""A cabin on whatever parcel already exists, rather than on a new one.

		`test_alerts.a_cabin` and `test_alerts.a_block` each create the Mill Creek
		parcel, and `create_parcel` refuses a second parcel of one name per entity
		— rightly. No test there calls both; every parity test here calls both,
		because a fixture that fires half the rules proves half the promise.
		"""
		parcels = STORE.rows("Parcel")
		if not parcels:
			return super().a_cabin(name, inspected_days_ago, detector_days_ago, **overrides)
		payload = {
			"parcel": str(parcels[0]["name"]),
			"unit_name": name,
			"unit_type": "Cabin",
			"square_footage": 384,
			"capacity": 4,
			"condition": "Good",
			"fsma_worker_facility": True,
		}
		payload.update(overrides)
		data = self.tool_data("create_housing_unit", payload)
		row = STORE.get_raw("Housing Unit", data["name"])
		row["last_habitability_inspection"] = (
			None if inspected_days_ago is None else days_from_today(-inspected_days_ago)
		)
		for field in ("smoke_detector_last_test", "co_detector_last_test"):
			row[field] = None if detector_days_ago is None else days_from_today(-detector_days_ago)
		return data

	def a_training(self, expires_in_days=12, regimes=("WPS",), **overrides):
		payload = {
			"employee": "HR-EMP-00001",
			"training_type": "WPS Handler Training",
			"completed_date": days_from_today(-350),
			"expires_date": days_from_today(expires_in_days),
			"company": MAIN,
			"regimes": list(regimes),
			"content_topics_covered": (
				"Label comprehension, PPE, decontamination, restricted-entry intervals, emergency response"
			),
			"trainer_name": "M. Ortiz",
			"delivered_language": "Spanish",
		}
		payload.update(overrides)
		return self.tool_data("record_training", payload)

	# ── seeding and snapshotting ────────────────────────────────────────────
	def seed_rules(self) -> dict:
		report = compliance_rules.seed_compliance_rules()
		self.assertEqual(report["failed"], [], f"the seeder could not write every rule: {report}")
		return report

	def alert_snapshot(self) -> dict:
		rows = {}
		for row in STORE.rows("Compliance Alert"):
			rows[str(row["name"])] = {key: _plain(row.get(key)) for key in ALERT_COLUMNS}
		return rows

	def wipe_alerts(self) -> None:
		"""Take the calendar back to empty, so the second sweep starts where the first did."""
		STORE.tables["Compliance Alert"] = {}

	def rule_named(self, rule_id: str) -> dict:
		name = compliance_rules.resolve(rule_id)
		self.assertTrue(name, f"no Compliance Rule for {rule_id!r}")
		return compliance_rules.rule_row(name)


def _plain(value):
	return None if value in (None, "") else str(value)


# ── THE PROMISE ─────────────────────────────────────────────────────────────
class TheMigrationChangesNothing(RuleEngineTestCase):
	"""Same database, same night, same alerts — before and after v0.22.0."""

	def test_the_record_driven_sweep_produces_identical_alerts(self):
		self.a_whole_operation()

		self.sweep(today=TODAY)
		legacy = self.alert_snapshot()
		self.assertTrue(legacy, "the fixture raised no alerts at all, so this proves nothing")

		self.wipe_alerts()
		self.seed_rules()
		self.sweep(today=TODAY)
		migrated = self.alert_snapshot()

		self.assertEqual(
			sorted(legacy),
			sorted(migrated),
			"the set of alert DOCNAMES changed across the migration. A docname carries the rule "
			"and the record and nothing that moves daily — if it changed, every snooze and every "
			"human dismissal on this site has been orphaned.",
		)
		for name in sorted(legacy):
			with self.subTest(alert=name):
				self.assertEqual(
					migrated[name],
					legacy[name],
					f"{name} says something different after the migration. The rule definitions "
					f"moved from Python into records; what they SAY was not supposed to move with "
					f"them.",
				)

	def test_every_shipped_rule_actually_fired_in_the_parity_fixture(self):
		"""A parity test over a fixture that fires two rules proves two rules."""
		self.a_whole_operation()
		self.sweep(today=TODAY)
		fired = {row["alert_type"] for row in STORE.rows("Compliance Alert")}
		self.assertGreaterEqual(
			len(fired),
			8,
			f"only {sorted(fired)} fired, so the parity assertion above covers only those. Add a "
			f"fixture for the rest before trusting it.",
		)

	def test_the_declarative_rules_render_the_legacy_message_word_for_word(self):
		"""Per rule, so a failure names WHICH message drifted rather than that one did."""
		self.a_whole_operation()
		self.sweep(today=TODAY)
		legacy = {row["name"]: str(row.get("alert_message") or "") for row in STORE.rows("Compliance Alert")}
		self.wipe_alerts()
		self.seed_rules()
		self.sweep(today=TODAY)

		declarative = {
			spec["rule_id"]
			for spec in compliance_rules.seed_specs()
			if not spec.get("builtin_scanner") and not spec.get("custom_python")
		}
		checked = 0
		for row in STORE.rows("Compliance Alert"):
			if row["alert_type"] not in declarative:
				continue
			checked += 1
			with self.subTest(rule=row["alert_type"], alert=row["name"]):
				self.assertEqual(
					str(row.get("alert_message") or ""),
					legacy.get(row["name"]),
					f"the Jinja template for {row['alert_type']} does not reproduce the Python it replaced.",
				)
		self.assertGreater(checked, 0, "no declarative rule fired, so no template was compared")


# ── the split, stated as a test ─────────────────────────────────────────────
class TheThirteenMigrateInThreeShapes(RuleEngineTestCase):
	def test_the_shipped_rules_become_records_one_for_one(self):
		"""Thirteen migrated from Python, and since v0.22.5 one that never was.

		`shift_heat_threshold_crossed` has no shipped scanner and never had one:
		it was authored as a record, in the vocabulary, and there is nothing to
		fall back to. It is the first rule this app ships that is ONLY data.

		v0.27.0 added three I-9 rules: `i9_verification_overdue`,
		`work_authorization_expiring`, `i9_retention_destruction_eligible`.

		v0.39.0 added `financial_kpi_threshold_breach`, and it is the first rule
		here that is about money rather than about a regulator. It lands on this
		calendar rather than on a finance board of its own because an operation
		with two alerting systems reads neither.

		v0.42.0 added `budget_variance_breach`, the second finance rule, on the
		same argument.

		v0.55.0 added four MISSING-SIGNATURE rules — three record-only
		(`i9_section_1_unsigned`, `i9_section_2_unsigned`, `w4_signature_missing`)
		and one built-in (`i9_supplement_b_unsigned`, which folds a child table).
		They are the first rules here that watch a BOX rather than a clock: a
		form can be filed on time, in full, and attest to nothing.

		v0.56.0 added a fifth, `tax_form_signature_missing`, for the employer's
		OWN returns — and its scope is the interesting part: 941, OR-WR, OQ and
		WA-ESD carry a penalties-of-perjury declaration and W-2 and 1099-NEC have
		no signature line at all, so the rule names four form types rather than
		firing on every Tax Form on the site.

		v0.69.0 added three that come out of the stock hooks: two spray intervals
		(`rei_active_block_entry`, `phi_harvest_window`), which are the first
		rules here whose auto-dismiss is measured in HOURS rather than days, and
		`item_below_reorder`, which is built in because the balance and the level
		it is read against live on two different doctypes.
		"""
		report = self.seed_rules()
		self.assertEqual(len(report["created"]), ALL_RULES)
		self.assertEqual(len(compliance_rules.rule_rows()), LIVE_RULES)
		# v0.80.0. The thirty are still thirty. What grew is the gate register
		# beside them, and the two are counted separately here so that adding a
		# control point can never be mistaken for losing a swept rule.
		swept = [row for row in compliance_rules.rule_rows() if not row.get("control_point")]
		self.assertEqual(len(swept), SWEPT_RULES)
		self.assertIn("shift_heat_threshold_crossed", report["created"])
		self.assertNotIn("shift_heat_threshold_crossed", alerts.RULES)

	def test_the_shapes_are_twenty_four_declarative_six_builtin_and_no_custom_python(self):
		"""The split is a claim the release makes, so it is asserted rather than described.

		v0.22.0 shipped 6/7/0 and named the four primitives that would move five of
		the seven. v0.22.1 added them, and this is the number that says so.

		`custom_python` shipping UNUSED is the deliberate part, and it is more
		pointed at 11/2/0 than it was at 6/7/0: a framework that needed a program
		for eleven of its own thirteen rules would be a framework whose primitives
		do not reach its own problem domain.

		v0.27.0 added three I-9 declarative rules.
		v0.28.0 added two W-4 declarative rules.
		v0.55.0 added three more — two I-9 and one W-4 — each of which asks one
		question of one signature column on one row, which is exactly what the
		declarative vocabulary is for. Its fourth sibling is built in, below.
		"""
		self.seed_rules()
		shapes = {}
		for row in compliance_rules.rule_rows():
			# v0.80.0. GATES ARE NOT ONE OF THE THREE SHAPES. `shape_of` reads
			# "declarative" off a gate rule because a gate carries no scanner — but
			# the shapes are a statement about how the SWEEP runs a rule, and a gate
			# is never swept. Counting them here would silently restate this test's
			# claim as something it does not mean.
			if row.get("control_point"):
				continue
			shapes.setdefault(compliance_rules.shape_of(row), []).append(row["rule_id"])
		self.assertEqual(
			sorted(shapes.get(compliance_rules.SHAPE_DECLARATIVE, [])),
			[
				"certification_expiring",
				"employee_missing_w4",
				"field_flag_awaiting_dispatch",
				"filing_response_due",
				"flc_license_expiring",
				"housing_corrective_action_open",
				"housing_detector_test_stale",
				"housing_inspection_overdue",
				"i9_expired",
				"i9_retention_destruction_eligible",
				"i9_section_1_unsigned",
				"i9_section_2_unsigned",
				"i9_verification_overdue",
				# v0.69.0. Both read a column a spray task's completion stamped —
				# an expiry to the hour and a harvest date — against
				# `{{current_datetime}}` and `{{current_date}}`. A rule whose
				# whole condition is "is this window still open" is exactly what
				# the declarative vocabulary is for; what was missing was the
				# datetime template variable, which is one entry in a closed
				# registry rather than a new shape of rule.
				"phi_harvest_window",
				"policy_review_overdue",
				"rei_active_block_entry",
				"shift_heat_threshold_crossed",
				"tax_form_signature_missing",
				"training_expiring",
				"w4_signature_missing",
				"w4_tax_year_outdated",
				"water_test_contamination",
				"water_test_stale",
				"work_authorization_expiring",
			],
		)
		self.assertEqual(shapes.get(compliance_rules.SHAPE_CUSTOM, []), [])

	def test_the_six_that_stay_built_in_are_named_and_argued(self):
		"""PERMANENT, not a backlog. Both are a different SHAPE of question.

		`audit_action_overdue` walks a child table, keeps the overdue rows, picks
		the worst and raises ONE alert per audit — an aggregation, and an
		aggregation is not a filter. `supervisor_review_lapsed` walks a TABLE of
		doctypes, needs an OR of two nulls, runs its clock on `creation`, and its
		thresholds mean days ELAPSED rather than days remaining — a number that
		means the opposite of what the same number means on the other twelve.

		Saying "these two do not belong in a rule record" is part of the design,
		so it is asserted rather than left as prose somebody can quietly widen.
		"""
		self.seed_rules()
		builtin = sorted(
			row["rule_id"]
			for row in compliance_rules.rule_rows()
			if compliance_rules.shape_of(row) == compliance_rules.SHAPE_BUILTIN
		)
		self.assertEqual(
			builtin,
			[
				"audit_action_overdue",
				# v0.42.0, and the fourth permanent built-in, for the same reason
				# the third is: its thresholds are not on its own row — they live
				# on each Budget Line Item and Budget KPI Target, per-line rather
				# than per-rule — and the comparison is against DERIVED,
				# MULTI-ROW state: every child row on an Active budget, checked
				# at once by `budget_engine.check_budget_variances`. That is
				# arithmetic across a table, not a filter on one row.
				"budget_variance_breach",
				# v0.39.0, and the third permanent built-in. Its thresholds are
				# not on its own row at all: they live on each Financial KPI
				# Definition, because a ratio's warning line and a dollar
				# figure's warning line are not values that can share a column.
				# And the comparison is against a DERIVED value — the newest
				# cached snapshot for a (KPI, company, window) tuple, against
				# four nullable bounds whose meaning depends on which are set.
				# That is arithmetic, not a filter.
				"financial_kpi_threshold_breach",
				# v0.55.0, and the fifth permanent built-in. Supplement B lives
				# in a CHILD TABLE — one reverification per row, each with its
				# own attestation — so "does this form have an unsigned
				# reverification" folds a set of rows down to a count and a
				# newest date, which is the same aggregation the first built-in
				# does. The nearest declarative primitive,
				# `latest_child_field_threshold`, compares NUMBERS by design and
				# an empty Attach field is not one.
				"i9_supplement_b_unsigned",
				# v0.69.0, and the sixth permanent built-in. The balance is
				# `Bin.actual_qty` and the level is a reorder rule that lives on
				# `Item Reorder` on a modern site and on `Item.re_order_level` on
				# an old one — two doctypes with no column joining them, plus a
				# vintage decision `stock_inventory._reorder_rules` already owns.
				# The declarative engine walks the rows of ONE doctype; there is
				# no question you can ask of a Bin whose answer is on an Item's
				# child table.
				"item_below_reorder",
				# v0.98.0, and the seventh permanent built-in. The condition is a
				# SUM over the crew rows of every shift a person is on, bounded
				# by a workweek, compared against a ceiling that depends on their
				# AGE ON THE DAY — three things the declarative vocabulary has
				# none of. It also has to count the shift that is still running,
				# which is the whole point of the rule and which no stored column
				# holds until the shift closes.
				"minor_hours_approaching",
				"supervisor_review_lapsed",
			],
		)

	def test_every_migrated_rule_keeps_its_kairotic_gate_and_its_citation_verbatim(self):
		"""The two fields an auditor reads. Neither may be paraphrased by a migration."""
		self.seed_rules()
		for key, rule in alerts.RULES.items():
			with self.subTest(rule=key):
				row = self.rule_named(key)
				self.assertEqual(row["kairotic_gate_description"], rule.kairotic_gate)
				self.assertEqual(row["regulation_citations"], rule.framework)
				self.assertEqual(row["purpose"], rule.purpose)
				self.assertEqual(sorted(row["regimes"]), sorted(rule.regimes))

	def test_every_migrated_rule_is_attributed_to_the_system_and_approved(self):
		self.seed_rules()
		for row in compliance_rules.rule_rows():
			with self.subTest(rule=row["rule_id"]):
				self.assertEqual(row["authored_by"], compliance_rules.AUTHOR_SYSTEM)
				self.assertTrue(row["human_approved_by"])
				self.assertTrue(row["human_approved_on"])
				self.assertTrue(row["enabled"])

	def test_a_built_in_rule_still_carries_its_thresholds_on_the_record(self):
		"""'Built-in' means the SHAPE is code, not that the numbers are."""
		self.seed_rules()
		row = self.rule_named("supervisor_review_lapsed")
		self.assertEqual(row["builtin_scanner"], "supervisor_review_lapsed")
		self.assertEqual(int(row["threshold_critical_days"]), 30)
		self.assertEqual(int(row["threshold_warning_days"]), 14)

	def test_the_rule_that_was_built_in_yesterday_keeps_every_number_it_had(self):
		"""Migrating a rule to declarative must not move a single tunable.

		`certification_expiring` was the built-in the v0.22.0 notes argued hardest
		about. It is data now, and the numbers on the record are the ones the
		scanner read the night before — same critical threshold, same default
		window, same per-row window field.
		"""
		self.seed_rules()
		row = self.rule_named("certification_expiring")
		self.assertEqual(row["builtin_scanner"], "")
		self.assertEqual(int(row["threshold_critical_days"]), 30)
		self.assertEqual(int(row["threshold_warning_days"]), 90)
		self.assertEqual(row["window_field"], "renewal_window_days")

	def test_the_producer_recipe_comes_off_the_dispatch_map_rather_than_being_retyped(self):
		from erpnext_mcp.tools.dispatch import ALERT_TASK_MAP

		self.seed_rules()
		row = self.rule_named("water_test_stale")
		self.assertEqual(row["producer_farm_task_type"], ALERT_TASK_MAP["water_test_stale"]["task_type"])
		self.assertEqual(row["producer_skill_required"], ALERT_TASK_MAP["water_test_stale"]["skill"])


# ── the declarative primitives, one at a time ───────────────────────────────
class TheDeclarativeEngine(RuleEngineTestCase):
	def a_rule(self, **overrides) -> dict:
		spec = {
			"rule_id": "widget_expiring",
			"title": "A widget is expiring",
			"category": "Records",
			"target_doctype": "Compliance Policy",
			"date_field": "review_due_date",
			"cadence_days": 0,
			"threshold_critical_days": 30,
			"threshold_warning_days": 90,
			"severity_expired": "Critical",
			"kairotic_gate_description": "Fires on the record's own date.",
			"message_template": "{{ name }} in {{ days_remaining }} day(s).",
			"regimes": ["Internal"],
			"enabled": 1,
			"human_approved_by": "Administrator",
			"human_approved_on": frappe.utils.now(),
		}
		spec.update(overrides)
		doc = compliance_rules.build_rule(spec)
		doc.insert(ignore_permissions=True)
		return compliance_rules.rule_row(doc.name)

		# -- bands ---------------------------------------------------------------

	def test_a_date_inside_the_warning_band_raises_warning(self):
		self.a_policy(review_in_days=60)
		row = self.a_rule()
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual([entry["severity"] for entry in result["observations"]], ["Warning"])
		self.assertIn("in 60 day(s)", result["observations"][0]["message"])

	def test_a_date_inside_the_critical_band_raises_critical(self):
		self.a_policy(review_in_days=10)
		result = engine.preview(self.a_rule(), {"today": TODAY, "company": ""})
		self.assertEqual([entry["severity"] for entry in result["observations"]], ["Critical"])

	def test_a_date_outside_every_band_raises_nothing(self):
		self.a_policy(review_in_days=200)
		self.assertEqual(engine.preview(self.a_rule(), {"today": TODAY, "company": ""})["observed"], 0)

	def test_a_negative_threshold_means_the_band_never_fires(self):
		"""How a rule says 'I have nothing to say until the date has passed'."""
		self.a_policy(review_in_days=5)
		row = self.a_rule(threshold_critical_days=-1, threshold_warning_days=-1)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	def test_severity_expired_is_what_a_passed_date_raises(self):
		self.a_policy(review_in_days=-3)
		row = self.a_rule(severity_expired="Warning", threshold_critical_days=-1, threshold_warning_days=-1)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual([entry["severity"] for entry in result["observations"]], ["Warning"])

	# -- cadence -------------------------------------------------------------
	def test_a_cadence_counts_from_the_anchor_rather_than_to_it(self):
		"""365 days on a last-inspection date is the annual walk, not an expiry."""
		self.a_cabin(inspected_days_ago=400)
		row = self.a_rule(
			rule_id="walk_overdue",
			target_doctype="Housing Unit",
			date_field="last_habitability_inspection",
			cadence_days=365,
			threshold_critical_days=-1,
			threshold_warning_days=-1,
			severity_expired="Warning",
			message_template="{{ name }} is {{ days_overdue }} day(s) past its walk.",
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertIn("35 day(s) past its walk", result["observations"][0]["message"])

	def test_a_row_inside_its_cadence_raises_nothing(self):
		self.a_cabin(inspected_days_ago=100)
		row = self.a_rule(
			rule_id="walk_overdue",
			target_doctype="Housing Unit",
			date_field="last_habitability_inspection",
			cadence_days=365,
			threshold_critical_days=-1,
			threshold_warning_days=-1,
			severity_expired="Warning",
		)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	# -- the missing anchor --------------------------------------------------
	def test_a_missing_date_is_skipped_where_the_rule_says_skip(self):
		"""A training with no expiry does not lapse."""
		self.a_policy(review_in_days=-3)
		STORE.get_raw("Compliance Policy", "Harvest Hygiene SOP")["review_due_date"] = None
		row = self.a_rule(missing_date_behaviour="Skip")
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	def test_a_missing_date_raises_where_the_rule_says_raise(self):
		"""A cabin nobody has ever inspected is the most overdue cabin there is."""
		self.a_policy(review_in_days=-3)
		STORE.get_raw("Compliance Policy", "Harvest Hygiene SOP")["review_due_date"] = None
		row = self.a_rule(missing_date_behaviour="Raise", severity_expired="Warning")
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual([entry["severity"] for entry in result["observations"]], ["Warning"])
		self.assertIsNone(result["observations"][0]["due_date"])

	# -- scope filters -------------------------------------------------------
	def test_a_scope_filter_narrows_the_scan(self):
		self.a_policy("Kept SOP", review_in_days=-3)
		self.a_policy("Dropped SOP", review_in_days=-3, category="Water Testing")
		row = self.a_rule(
			scope_filters=[{"field": "policy_name", "op": "eq", "value": "Kept SOP"}],
			threshold_warning_days=0,
			threshold_critical_days=-1,
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual([entry["source_docname"] for entry in result["observations"]], ["Kept SOP"])

	def test_a_company_filter_scopes_a_rule_to_one_entity(self):
		self.a_policy("Main SOP", review_in_days=-3, company=MAIN)
		row = self.a_rule(
			scope_filters=[{"field": "company", "op": "eq", "value": MAIN}],
			threshold_warning_days=0,
			threshold_critical_days=-1,
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertTrue(result["observed"])
		for entry in result["observations"]:
			self.assertEqual(entry["company"], MAIN)

	def test_a_filter_default_is_what_an_empty_column_is_read_as(self):
		"""The difference between this and SQL, and it is the whole reason for it.

		`status != 'Active'` in SQL excludes every row whose status was never
		set. Read against a default, those rows pass — which is what the legacy
		`str(row.get("status") or "Active")` did, and what the shipped policy
		rule still needs.
		"""
		self.a_policy(review_in_days=-3)
		STORE.get_raw("Compliance Policy", "Harvest Hygiene SOP")["status"] = None
		row = self.a_rule(
			scope_filters=[{"field": "status", "op": "eq", "value": "Active", "default": "Active"}],
			threshold_warning_days=0,
			threshold_critical_days=-1,
		)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 1)

		bare = self.a_rule(
			rule_id="widget_bare",
			scope_filters=[{"field": "status", "op": "eq", "value": "Active"}],
			threshold_warning_days=0,
			threshold_critical_days=-1,
		)
		self.assertEqual(engine.preview(bare, {"today": TODAY, "company": ""})["observed"], 0)

	def test_a_filter_on_a_column_this_site_has_not_got_is_reported_not_fatal(self):
		self.a_policy(review_in_days=-3)
		row = self.a_rule(
			scope_filters=[{"field": "a_column_nobody_has", "op": "eq", "value": "x"}],
			threshold_warning_days=0,
			threshold_critical_days=-1,
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertTrue(any("a_column_nobody_has" in note for note in result["computation_warnings"]))

	# -- the message ---------------------------------------------------------
	def test_the_message_template_renders_the_row_and_the_computed_fields(self):
		self.a_policy(review_in_days=-3)
		row = self.a_rule(
			threshold_warning_days=0,
			threshold_critical_days=-1,
			message_template=(
				"{{ policy_name }} ({{ version }}) is {{ days_overdue }} day(s) overdue as of "
				"{{ today }}; due {{ due_date }}."
			),
		)
		message = engine.preview(row, {"today": TODAY, "company": ""})["observations"][0]["message"]
		self.assertEqual(
			message,
			f"Harvest Hygiene SOP (v3) is 3 day(s) overdue as of {TODAY}; due {days_from_today(-3)}.",
		)

	def test_a_template_that_cannot_render_produces_a_plain_message_rather_than_no_alert(self):
		"""An ugly alert is a problem somebody fixes. A missing one is not."""
		self.a_policy(review_in_days=-3)
		row = self.a_rule(
			threshold_warning_days=0,
			threshold_critical_days=-1,
			message_template="{{ oops.deeper.still }}",
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertIn("past due", result["observations"][0]["message"])
		self.assertTrue(any("template" in note for note in result["computation_warnings"]))

	def test_a_message_template_cannot_reach_frappe(self):
		"""The Jinja environment is sandboxed and has no framework in it.

		Deliberately not `frappe.render_template`: that environment carries
		`frappe` in its globals, and a message template able to call
		`frappe.db.set_value` would be a second, undocumented escape hatch
		sitting beside the one this release spent a module sandboxing.
		"""
		self.a_policy(review_in_days=-3)
		row = self.a_rule(
			threshold_warning_days=0,
			threshold_critical_days=-1,
			message_template="{{ frappe.db.get_value('Compliance Policy', 'x', 'name') }}",
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertNotIn("Compliance Policy", result["observations"][0]["message"])

	# -- per-row regimes -----------------------------------------------------
	def test_regimes_from_field_copies_the_tags_off_the_row(self):
		self.an_employee()
		self.a_training(expires_in_days=12, regimes=("WPS", "OR-OSHA"))
		row = self.a_rule(
			rule_id="training_widget",
			target_doctype="Employee Training Record",
			date_field="expires_date",
			regimes_from_field="regimes",
			regimes=["Internal"],
			message_template="{{ regimes|join('/') }}",
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(sorted(result["observations"][0]["regimes"]), ["OR-OSHA", "WPS"])

	# -- no clock at all -----------------------------------------------------
	def test_a_rule_with_no_date_field_fires_on_the_filters_alone(self):
		self.an_employee(i9="Expired")
		row = self.a_rule(
			rule_id="i9_widget",
			target_doctype="Employee",
			date_field="",
			due_date_mode="Today",
			scope_filters=[{"field": "i9_status", "op": "eq", "value": "Expired"}],
			message_template="{{ employee_name or name }} has an expired I-9.",
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertEqual(result["observations"][0]["severity"], "Critical")
		self.assertEqual(result["observations"][0]["due_date"], TODAY)

	def test_a_required_field_this_site_lacks_makes_the_rule_scan_nothing(self):
		"""An EMPTY SCAN, not a failure — the difference is auto-dismissal.

		A rule that failed would dismiss nothing, leaving yesterday's alerts
		standing on a site that can no longer evaluate them. A rule that scans
		and finds nothing takes them off, which is the honest answer when the
		column they were computed from is gone.
		"""
		self.an_employee(i9="Expired")
		row = self.a_rule(
			rule_id="i9_widget",
			target_doctype="Employee",
			date_field="",
			requires_fields="a_field_nobody_installed",
			scope_filters=[{"field": "i9_status", "op": "eq", "value": "Expired"}],
		)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	# -- template variables (v0.68.0) -----------------------------------------
	def test_current_date_narrows_by_the_records_own_clock_at_sweep_time(self):
		"""`{{current_date}}` is resolved when the sweep runs, not when the rule was
		saved — this is what makes it different from hardcoding `TODAY` into the
		filter, and the whole reason the feature exists. Bands are opened wide so
		both records would otherwise be observed; the scope filter is the only
		thing excluding the one whose review is not yet due."""
		self.a_policy("Overdue SOP", review_in_days=-3)
		self.a_policy("Future SOP", review_in_days=10)
		row = self.a_rule(
			scope_filters=[{"field": "review_due_date", "op": "lt", "value": "{{current_date}}"}],
			threshold_warning_days=900,
			threshold_critical_days=900,
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual([entry["source_docname"] for entry in result["observations"]], ["Overdue SOP"])


# ── scope-filter template variables, as pure functions ──────────────────────
class TheScopeFilterTemplateVariables(unittest.TestCase):
	"""v0.68.0. `w4_tax_year_outdated` needed `tax_year < current_year` and, until
	this release, had no way to say "current year" without hardcoding it — which
	is a rule that stops being true every January. These test the resolution
	mechanism directly, against `TODAY = "2026-07-24"` (harness.py), rather than
	through a full rule sweep — `TheDeclarativeEngine` above covers that end of it
	for `{{current_date}}`, and `test_missing_signatures.py` covers the rule this
	was built for.
	"""

	def test_current_year_and_current_month_resolve_at_evaluation_time(self):
		matched, _warnings = compliance_rules.row_matches(
			{"tax_year": 2025}, [{"field": "tax_year", "op": "lt", "value": "{{current_year}}"}]
		)
		self.assertTrue(matched)

		matched, _warnings = compliance_rules.row_matches(
			{"tax_year": 2026}, [{"field": "tax_year", "op": "lt", "value": "{{current_year}}"}]
		)
		self.assertFalse(matched, "2026 is not less than current_year (2026 on TODAY)")

		matched, _warnings = compliance_rules.row_matches(
			{"review_month": 7}, [{"field": "review_month", "op": "eq", "value": "{{current_month}}"}]
		)
		self.assertTrue(matched)

	def test_current_date_resolves_to_todays_iso_string(self):
		self.assertEqual(compliance_rules.resolve_template("{{current_date}}"), frappe.utils.today())

	def test_a_template_inside_a_list_op_resolves_element_wise(self):
		matched, _warnings = compliance_rules.row_matches(
			{"tax_year": 2026}, [{"field": "tax_year", "op": "in", "value": ["2024", "{{current_year}}"]}]
		)
		self.assertTrue(matched)

	def test_an_unknown_template_variable_is_refused_at_authoring_time(self):
		with self.assertRaises(ValueError) as ctx:
			compliance_rules.parse_filters(
				json.dumps([{"field": "tax_year", "op": "lt", "value": "{{current_decade}}"}])
			)
		self.assertIn("current_decade", str(ctx.exception))

	def test_a_template_embedded_in_other_text_is_refused_rather_than_ignored(self):
		"""A filter value is compared whole, as a number or as exact text — a
		template glued into a sentence would silently compare against the literal
		string `"before {{current_year}}"` forever, which is a quieter failure than
		refusing it up front."""
		with self.assertRaises(ValueError):
			compliance_rules.parse_filters(
				json.dumps([{"field": "tax_year", "op": "lt", "value": "before {{current_year}}"}])
			)

	def test_a_plain_value_that_is_not_shaped_like_a_template_is_untouched(self):
		parsed = compliance_rules.parse_filters(
			json.dumps([{"field": "status", "op": "eq", "value": "Active"}])
		)
		self.assertEqual(parsed[0]["value"], "Active")


# ── the sandbox ─────────────────────────────────────────────────────────────
class TheCustomPythonSandbox(RuleEngineTestCase):
	def a_program_rule(self, program: str, **overrides) -> dict:
		spec = {
			"rule_id": "programmed",
			"title": "A rule written as a program",
			"category": "Records",
			"target_doctype": "Compliance Policy",
			"custom_python": program,
			"kairotic_gate_description": "Fires on whatever the program says.",
			"regimes": ["Internal"],
			"enabled": 1,
			"human_approved_by": "Administrator",
			"human_approved_on": frappe.utils.now(),
		}
		spec.update(overrides)
		doc = compliance_rules.build_rule(spec)
		doc.insert(ignore_permissions=True)
		return compliance_rules.rule_row(doc.name)

	def test_a_program_can_read_rows_and_return_observations(self):
		self.a_policy(review_in_days=-30)
		row = self.a_program_rule(
			"rows = frappe.get_all('Compliance Policy', fields=['name', 'review_due_date'])\n"
			"out = []\n"
			"for policy in rows:\n"
			"    late = days_since(today, policy['review_due_date'])\n"
			"    if late is not None and late > 7:\n"
			"        out.append(observation('Compliance Policy', policy['name'],\n"
			"            f'{policy[\"name\"]} is {late} days overdue', SEVERITY_WARNING))\n"
			"return out\n"
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertEqual(result["observations"][0]["severity"], "Warning")
		self.assertIn("30 days overdue", result["observations"][0]["message"])

	def test_a_program_that_falls_off_the_end_returns_its_observations_list(self):
		self.a_policy(review_in_days=-30)
		row = self.a_program_rule(
			"for policy in frappe.get_all('Compliance Policy', fields=['name']):\n"
			"    observations.append(observation('Compliance Policy', policy['name'], 'late'))\n"
		)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 1)

	# -- what it refuses -----------------------------------------------------
	def test_import_is_refused(self):
		with self.assertRaises(sandbox.SandboxError) as caught:
			sandbox.check("import os\nreturn []\n")
		self.assertIn("`import` is refused", str(caught.exception))
		self.assertIn("os", str(caught.exception))

	def test_from_import_is_refused(self):
		with self.assertRaises(sandbox.SandboxError):
			sandbox.check("from os import path\nreturn []\n")

	def test_exec_and_eval_are_refused(self):
		for program in ("exec('x = 1')", "eval('1 + 1')", "compile('x', 'y', 'exec')"):
			with self.subTest(program=program):
				with self.assertRaises(sandbox.SandboxError):
					sandbox.check(f"{program}\nreturn []\n")

	def test_open_is_refused(self):
		with self.assertRaises(sandbox.SandboxError):
			sandbox.check("open('/etc/passwd')\nreturn []\n")

	def test_dunder_attributes_are_refused(self):
		"""`x.__class__.__bases__[0].__subclasses__()` needs no import at all."""
		with self.assertRaises(sandbox.SandboxError) as caught:
			sandbox.check("return [].__class__.__bases__\n")
		self.assertIn("underscore", str(caught.exception))

	def test_while_is_refused_because_it_is_unbounded(self):
		with self.assertRaises(sandbox.SandboxError) as caught:
			sandbox.check("while True:\n    pass\n")
		self.assertIn("unbounded", str(caught.exception))

	def test_defining_a_function_or_a_class_or_a_lambda_is_refused(self):
		for program in ("def f():\n    return 1\n", "class C:\n    pass\n", "f = lambda: 1\n"):
			with self.subTest(program=program.split("\n")[0]):
				with self.assertRaises(sandbox.SandboxError):
					sandbox.check(program)

	def test_try_except_is_refused_because_a_quiet_rule_is_the_failure_mode(self):
		with self.assertRaises(sandbox.SandboxError) as caught:
			sandbox.check("try:\n    x = 1\nexcept Exception:\n    pass\n")
		self.assertIn("goes quiet", str(caught.exception))

	def test_a_refused_program_is_reported_on_the_rule_rather_than_killing_the_sweep(self):
		"""And it says the condition is now UNWATCHED, which is the fact that matters."""
		row = self.a_program_rule("x = 1\nreturn []\n")
		frappe.db.set_value(compliance_rules.DOCTYPE, row["name"], "custom_python", "import os\nreturn []\n")
		refreshed = compliance_rules.rule_row(row["name"])
		result = engine.preview(refreshed, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertIn("UNWATCHED", result["observations"][0]["message"])
		self.assertEqual(result["observations"][0]["severity"], "Warning")

	def test_the_controller_refuses_a_program_the_sandbox_would_not_run(self):
		"""Refused while the person who typed it is present, not at 2am."""
		with self.assertRaises(Exception) as caught:
			self.a_program_rule("import subprocess\nreturn []\n")
		self.assertIn("sandbox", str(caught.exception).lower())

	def test_a_runaway_loop_is_stopped_by_the_step_budget(self):
		with self.assertRaises(sandbox.SandboxError) as caught:
			sandbox.run(
				"out = 0\nfor i in range(10000000):\n    out = out + 1\nreturn out\n", {"range": range}
			)
		self.assertIn("steps", str(caught.exception))

	def test_a_name_that_was_not_given_is_a_refusal_naming_what_is_in_scope(self):
		with self.assertRaises(sandbox.SandboxError) as caught:
			sandbox.run("return mystery\n", {"today": "2026-01-01"})
		self.assertIn("today", str(caught.exception))

	def test_get_doc_hands_back_a_dict_so_nothing_can_be_saved_through_it(self):
		self.a_policy(review_in_days=-30)
		row = self.a_program_rule(
			"doc = frappe.get_doc('Compliance Policy', 'Harvest Hygiene SOP')\n"
			"return [observation('Compliance Policy', 'Harvest Hygiene SOP', str(doc['policy_name']))]\n"
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observations"][0]["message"], "Harvest Hygiene SOP")

		saving = self.a_program_rule(
			"doc = frappe.get_doc('Compliance Policy', 'Harvest Hygiene SOP')\ndoc.save()\nreturn []\n",
			rule_id="programmed_saver",
		)
		refused = engine.preview(saving, {"today": TODAY, "company": ""})
		self.assertIn("did not run", refused["observations"][0]["message"])


# ── versioning, approval and the switch ─────────────────────────────────────
class RulesAreVersionedByCopy(RuleEngineTestCase):
	def test_an_update_writes_a_new_row_and_supersedes_the_old_one(self):
		self.seed_rules()
		before = self.rule_named("housing_inspection_overdue")
		updated = self.tool_data(
			"update_compliance_rule",
			{"name": "housing_inspection_overdue", "cadence_days": 300, "reason": "contracted at ten months"},
		)
		self.assertEqual(updated["version"], 2)
		self.assertEqual(updated["supersedes"], before["name"])

		old = compliance_rules.rule_row(before["name"])
		self.assertEqual(old["superseded_by"], updated["name"])
		self.assertFalse(old["enabled"])
		self.assertEqual(int(old["cadence_days"]), 365, "the old row was EDITED rather than superseded")

	def test_only_one_row_is_live_after_an_update(self):
		self.seed_rules()
		self.tool_data(
			"update_compliance_rule",
			{"name": "policy_review_overdue", "threshold_warning_days": 7, "reason": "a week of grace"},
		)
		live = [row for row in compliance_rules.rule_rows() if row["rule_id"] == "policy_review_overdue"]
		self.assertEqual(len(live), 1)
		self.assertEqual(int(live[0]["version"]), 2)

	def test_a_second_live_row_for_one_rule_id_is_refused(self):
		self.seed_rules()
		with self.assertRaises(Exception) as caught:
			compliance_rules.build_rule(
				{
					"rule_id": "policy_review_overdue",
					"title": "A duplicate",
					"target_doctype": "Compliance Policy",
					"kairotic_gate_description": "duplicate",
					"enabled": 1,
					"human_approved_by": "Administrator",
					"human_approved_on": frappe.utils.now(),
				}
			).insert(ignore_permissions=True)
		self.assertIn("already live", str(caught.exception))

	def test_the_sweep_sees_exactly_one_definition_per_rule_id(self):
		self.seed_rules()
		self.tool_data(
			"update_compliance_rule",
			{"name": "policy_review_overdue", "threshold_warning_days": 7, "reason": "a week of grace"},
		)
		rules = alerts.rule_map()
		self.assertEqual(len([key for key in rules if key == "policy_review_overdue"]), 1)
		self.assertEqual(rules["policy_review_overdue"].version, 2)

	def test_a_superseded_row_stays_readable(self):
		"""The definition an alert was raised under has to still be on the site."""
		self.seed_rules()
		first = self.rule_named("policy_review_overdue")["name"]
		self.tool_data(
			"update_compliance_rule",
			{"name": "policy_review_overdue", "threshold_warning_days": 7, "reason": "a week of grace"},
		)
		data = self.tool_data("get_compliance_rule", {"name": first})
		self.assertEqual(data["version"], 1)
		self.assertTrue(data["superseded_by"])
		self.assertEqual(data["kairotic_gate"], alerts.RULES["policy_review_overdue"].kairotic_gate)


class TheApprovalGate(RuleEngineTestCase):
	def test_a_rule_cannot_be_enabled_without_an_approver(self):
		with self.assertRaises(Exception) as caught:
			compliance_rules.build_rule(
				{
					"rule_id": "unapproved",
					"title": "Nobody read this",
					"target_doctype": "Compliance Policy",
					"kairotic_gate_description": "gate",
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
		self.assertIn("approver", str(caught.exception).lower())

	def test_create_leaves_a_rule_in_draft(self):
		data = self.tool_data(
			"create_compliance_rule",
			{
				"rule_id": "new_widget",
				"title": "A widget rule",
				"category": "Records",
				"target_doctype": "Compliance Policy",
				"date_field": "review_due_date",
				"kairotic_gate_description": "Fires on the policy's own review date.",
				"message_template": "{{ name }}",
			},
		)
		self.assertFalse(data["enabled"])
		self.assertNotIn("new_widget", alerts.rule_map())

	def test_approving_turns_it_on_and_records_who_and_when(self):
		self.tool_data(
			"create_compliance_rule",
			{
				"rule_id": "new_widget",
				"title": "A widget rule",
				"category": "Records",
				"target_doctype": "Compliance Policy",
				"date_field": "review_due_date",
				"kairotic_gate_description": "Fires on the policy's own review date.",
				"message_template": "{{ name }}",
			},
		)
		data = self.tool_data("approve_compliance_rule", {"name": "new_widget"})
		self.assertTrue(data["enabled"])
		self.assertTrue(data["human_approved_by"])
		self.assertTrue(data["human_approved_on"])
		self.assertIn("new_widget", alerts.rule_map())

	def test_a_deactivated_rule_is_skipped_and_a_reactivated_one_comes_back(self):
		self.seed_rules()
		self.assertIn("policy_review_overdue", alerts.rule_map())
		self.tool_data(
			"deactivate_compliance_rule",
			{"name": "policy_review_overdue", "reason": "we do not run an SOP review cycle this season"},
		)
		self.assertNotIn("policy_review_overdue", alerts.rule_map())
		self.tool_data("approve_compliance_rule", {"name": "policy_review_overdue"})
		self.assertIn("policy_review_overdue", alerts.rule_map())

	def test_a_deactivated_rule_dismisses_nothing(self):
		"""Off is not deleted. The alerts it owns stay exactly as they were."""
		self.a_policy(review_in_days=-30)
		self.seed_rules()
		self.sweep(today=TODAY)
		before = len(
			[row for row in STORE.rows("Compliance Alert") if row["alert_type"] == "policy_review_overdue"]
		)
		self.assertEqual(before, 1)
		self.tool_data(
			"deactivate_compliance_rule",
			{"name": "policy_review_overdue", "reason": "not this season, and the alerts stay put"},
		)
		self.sweep(today=TODAY)
		after = [
			row for row in STORE.rows("Compliance Alert") if row["alert_type"] == "policy_review_overdue"
		]
		self.assertEqual(len(after), 1)
		self.assertFalse(frappe.utils.cint(after[0].get("dismissed")))


class TheSeederIsIdempotent(RuleEngineTestCase):
	def test_seeding_twice_writes_thirty_rules_once(self):
		self.assertEqual(len(self.seed_rules()["created"]), ALL_RULES)
		again = compliance_rules.seed_compliance_rules()
		self.assertEqual(again["created"], [])
		self.assertEqual(len(again["present"]), ALL_RULES)
		self.assertEqual(len(compliance_rules.rule_rows()), LIVE_RULES)

	def test_an_operator_edit_is_not_overwritten_on_the_next_migrate(self):
		"""The difference between a seeder and a Frappe fixture, and the reason
		this app has never shipped one."""
		self.seed_rules()
		self.tool_data(
			"update_compliance_rule",
			{"name": "housing_inspection_overdue", "cadence_days": 300, "reason": "contracted at ten months"},
		)
		compliance_rules.seed_compliance_rules()
		live = [row for row in compliance_rules.rule_rows() if row["rule_id"] == "housing_inspection_overdue"]
		self.assertEqual(len(live), 1)
		self.assertEqual(int(live[0]["cadence_days"]), 300)

	def test_a_disabled_rule_is_not_seeded_back_on(self):
		self.seed_rules()
		self.tool_data(
			"deactivate_compliance_rule",
			{"name": "filing_response_due", "reason": "this operation files nothing that gets a response"},
		)
		compliance_rules.seed_compliance_rules()
		self.assertNotIn("filing_response_due", alerts.rule_map())


class TheSweepFallsBackWithoutRecords(RuleEngineTestCase):
	def test_an_unseeded_site_runs_the_shipped_definitions_and_says_so(self):
		"""A calendar that emptied itself during a migrate would be the worst
		possible failure of this release."""
		self.a_policy(review_in_days=-30)
		report = self.sweep(today=TODAY)
		self.assertEqual(report["created"], 1)
		self.assertTrue(report.get("engine_notes"))
		self.assertIn("shipped", " ".join(report["engine_notes"]))

	def test_a_seeded_site_says_nothing_about_a_fallback(self):
		self.a_policy(review_in_days=-30)
		self.seed_rules()
		report = self.sweep(today=TODAY)
		self.assertFalse(report.get("engine_notes"))


# ── the tools ───────────────────────────────────────────────────────────────
class TheRuleTools(RuleEngineTestCase):
	def test_list_keeps_the_shape_it_had_before_v0_22_0(self):
		"""Clients read this. Additive is fine; renamed is a breaking change."""
		self.seed_rules()
		data = self.tool_data("list_compliance_rules", {})
		self.assertEqual(data["rule_count"], ALL_RULES)
		for rule in data["rules"]:
			for key in ("alert_type", "title", "category", "purpose", "kairotic_gate", "framework"):
				self.assertIn(key, rule)
		self.assertIn("not evidence that anybody did the work", data["note"])

	def test_list_filters_by_regime_category_target_and_active(self):
		self.seed_rules()
		housing = self.tool_data("list_compliance_rules", {"category": "Housing", "active": True})
		self.assertEqual(
			sorted(rule["alert_type"] for rule in housing["rules"]),
			["housing_corrective_action_open", "housing_detector_test_stale", "housing_inspection_overdue"],
		)
		fsma = self.tool_data("list_compliance_rules", {"regime": "FSMA"})
		self.assertIn("water_test_contamination", [rule["alert_type"] for rule in fsma["rules"]])
		self.assertNotIn("i9_expired", [rule["alert_type"] for rule in fsma["rules"]])
		employees = self.tool_data("list_compliance_rules", {"target_doctype": "Employee"})
		self.assertEqual(
			sorted(rule["alert_type"] for rule in employees["rules"]),
			[
				"employee_missing_w4",
				"flc_license_expiring",
				"i9_expired",
				# v0.98.0. The fourth rule whose subject is the person rather
				# than a document about them.
				"minor_hours_approaching",
			],
		)

	def test_a_regime_that_is_not_in_the_vocabulary_is_refused_by_name(self):
		self.seed_rules()
		self.assertIn("OSHA-ish", self.tool_error("list_compliance_rules", {"regime": "OSHA-ish"}))

	def test_get_returns_the_whole_definition(self):
		self.seed_rules()
		data = self.tool_data("get_compliance_rule", {"name": "training_expiring"})
		self.assertEqual(data["shape"], compliance_rules.SHAPE_DECLARATIVE)
		self.assertEqual(data["definition"]["date_field"], "expires_date")
		self.assertEqual(data["definition"]["regimes_from_field"], "regimes")
		self.assertTrue(data["definition"]["message_template"])

	def test_test_compliance_rule_reports_what_would_fire_and_writes_nothing(self):
		self.a_policy(review_in_days=-30)
		self.seed_rules()
		before = len(STORE.rows("Compliance Alert"))
		data = self.tool_data("test_compliance_rule", {"name": "policy_review_overdue"})
		self.assertEqual(data["observed"], 1)
		self.assertEqual(
			data["observations"][0]["would_be_alert"],
			"policy_review_overdue:Compliance Policy:Harvest Hygiene SOP",
		)
		self.assertEqual(len(STORE.rows("Compliance Alert")), before)

	def test_the_ai_proposer_no_longer_refuses_but_still_will_not_author_alone(self):
		"""v0.22.0 declared this surface and left it refusing; v0.37.0 wired it.

		What is asserted here is the ONE property this file is responsible for —
		that a proposal is still a draft, still off, and still not a rule the
		sweep runs. `test_ai_proposals.py` is where the rails are taken apart one
		at a time; this is the line that would fail if the CCF's approval gate
		were ever quietly moved out of the way by the release that filled it.
		"""
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, "allow_propose_compliance_rule": 1})
		data = self.tool_data(
			"propose_compliance_rule",
			{
				"rule_id": "a_model_wrote_this",
				"title": "A rule a model drafted",
				"target_doctype": "Compliance Policy",
				"kairotic_gate_description": "Ripe when a policy is past its review date.",
				"regulation_section": "OAR 437-004-1131",
			},
		)
		self.assertFalse(data["enabled"])
		self.assertEqual(data["authored_by"], compliance_rules.AUTHOR_AI)
		self.assertNotIn("a_model_wrote_this", alerts.rule_map())

	def test_every_rule_change_lands_in_the_mcp_action_log(self):
		"""An auditor asking 'who changed this rule and when' gets an answer
		without leaving the app."""
		self.seed_rules()
		self.tool_data(
			"update_compliance_rule",
			{"name": "policy_review_overdue", "threshold_warning_days": 7, "reason": "a week of grace"},
		)
		logged = [
			row for row in STORE.rows("MCP Action Log") if row.get("tool_name") == "update_compliance_rule"
		]
		self.assertTrue(logged)
		self.assertIn("policy_review_overdue", json.dumps(logged[-1].get("arguments_json") or ""))
