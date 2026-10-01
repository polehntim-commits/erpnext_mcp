# SPDX-License-Identifier: MIT
"""v0.22.1 — the four primitives, and the five rules they took declarative.

v0.22.0 shipped six declarative rules, seven built-in scanners and zero
`custom_python`, and its §5 argued each of the seven by naming the primitive
that would move it. This release added those primitives. Five of the seven moved.
Two did not, and saying so is part of the design rather than a backlog entry.

THE ONE CLASS THAT MATTERS MOST IS `TheMigrationOfEachRuleChangesNothing`. Every
other class here checks that a primitive works; that one checks that the rule
built on it says the SAME WORDS as the Python it replaced, per rule, on a fixture
built to make that rule speak. It is the v0.22.0 promise made a second time, and
it is checked the same way: run the sweep with the rule pointed at its shipped
scanner, snapshot every alert row, wipe, point the rule at its declarative
definition, run again, and compare the rows.

THE SHIPPED SCANNERS ARE STILL HERE, WHICH IS WHY THIS IS POSSIBLE. They are the
fallback a site runs before it has migrated the DocType, and they are therefore
also the oracle: the migration is checked against the code it replaces rather
than against a second description of what that code was supposed to do.
"""

import json

import frappe

from erpnext_mcp import compliance_rules, enforcement, records
from erpnext_mcp.alerts import engine

from .fixtures import MAIN
from .harness import STORE
from .test_alerts import ALL_ON, TODAY, days_from_today
from .test_compliance_rule_engine import ALERT_COLUMNS, RULE_TOOLS, RuleEngineTestCase, _plain

RECORD_TOOLS = {
	f"allow_{name}": 1
	for name in (
		"create_housing_inspection",
		"update_housing_inspection",
		"create_detector_test",
		"create_water_test",
		"create_irrigation_zone",
	)
}

#: The five that moved, and the primitive each one paid for.
MIGRATED = (
	("certification_expiring", "regime_heuristics_json"),
	("water_test_stale", "gate_date_field"),
	("housing_detector_test_stale", "date_fields_json"),
	("housing_corrective_action_open", "superseded_by_later_clean_json"),
	("water_test_contamination", "superseded_by_later_clean_json"),
)


class PrimitiveTestCase(RuleEngineTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, **RECORD_TOOLS})

	# ── fixtures the camp and water rules need ──────────────────────────────
	def a_parcel(self) -> None:
		if not STORE.rows("Parcel"):
			self.tool_data(
				"create_parcel",
				{"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43},
			)

	def a_zone(self, zone_name="YC3-Zone2", zone_number=2) -> str:
		self.a_parcel()
		if not STORE.rows("Field"):
			self.tool_data(
				"create_field",
				{"parcel": "Mill Creek", "field_name": "Yellow Camp Block 3", "acreage": 12.5},
			)
		return self.tool_data(
			"create_irrigation_zone",
			{
				"field": "Yellow Camp Block 3",
				"zone_name": zone_name,
				"zone_number": zone_number,
				"water_source": "creek",
			},
		)["name"]

	def an_inspection(self, unit: str, on: str, findings: str = "") -> dict:
		payload = {"unit": unit, "inspection_date": on}
		if findings:
			payload["findings"] = findings
		return self.tool_data("create_housing_inspection", payload)

	def a_detector_test(self, unit: str, on: str, failed: bool = False) -> dict:
		payload = {"unit": unit, "test_date": on}
		if failed:
			payload["co_detector_result"] = "Fail"
		return self.tool_data("create_detector_test", payload)

	def a_water_test(self, source: str, on: str, dirty: bool = False) -> dict:
		payload = {"source": source, "test_date": on}
		payload["coliform_result"] = "Present" if dirty else "Absent"
		return self.tool_data("create_water_test", payload)

	# ── running one rule ────────────────────────────────────────────────────
	def fired(self, rule_id: str) -> list:
		"""What one seeded rule observes right now, through the sweep's own path."""
		return engine.preview(self.rule_named(rule_id), {"today": TODAY, "company": ""})["observations"]

	def as_builtin(self, rule_id: str) -> None:
		"""Point a seeded rule back at the scanner it shipped as, for the parity oracle."""
		name = compliance_rules.resolve(rule_id)
		STORE.get_raw(compliance_rules.DOCTYPE, name)["builtin_scanner"] = rule_id

	def as_declarative(self, rule_id: str) -> None:
		name = compliance_rules.resolve(rule_id)
		STORE.get_raw(compliance_rules.DOCTYPE, name)["builtin_scanner"] = ""

	def edit_rule(self, rule_id: str, **fields) -> dict:
		name = compliance_rules.resolve(rule_id)
		STORE.get_raw(compliance_rules.DOCTYPE, name).update(fields)
		return compliance_rules.rule_row(name)


class CampTestCase(PrimitiveTestCase):
	"""The camp fixtures three classes below share.

	A base class rather than a subclass chain, deliberately: subclassing one test
	class from another re-runs its tests under the second name, which inflates the
	count and reports one failure twice.
	"""

	def a_camp(self, unit_name="MC-Cabin-01") -> str:
		self.a_parcel()
		return self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": unit_name,
				"unit_type": "Cabin",
				"square_footage": 400,
				"capacity": 4,
				"fsma_worker_facility": True,
			},
		)["name"]


# ── PRIMITIVE 1: superseded by a later clean record ─────────────────────────
class SupersededByLaterClean(CampTestCase):
	"""The one gate that is a question about OTHER ROWS. It took two rules at once."""

	def test_a_finding_with_nothing_after_it_fires(self):
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain, north wall")
		self.seed_rules()
		fired = self.fired("housing_corrective_action_open")
		self.assertEqual(len(fired), 1)
		self.assertEqual(fired[0]["severity"], "Critical")
		self.assertIn("water stain, north wall", fired[0]["message"])

	def test_a_later_clean_record_for_the_same_unit_supersedes_it(self):
		"""The exit that happens in practice, and requires nobody to remember a field."""
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain, north wall")
		self.an_inspection(unit, "2026-07-20")
		self.seed_rules()
		self.assertEqual(self.fired("housing_corrective_action_open"), [])

	def test_an_earlier_clean_record_does_not_supersede_it(self):
		"""LATER, not merely elsewhere. A clean walk in June says nothing about July."""
		unit = self.a_camp()
		self.an_inspection(unit, "2026-06-01")
		self.an_inspection(unit, "2026-07-01", findings="water stain")
		self.seed_rules()
		self.assertEqual(len(self.fired("housing_corrective_action_open")), 1)

	def test_a_later_clean_record_for_a_DIFFERENT_unit_does_not_supersede_it(self):
		"""Cabin 2 being fine is not evidence about cabin 1, and this is the assertion
		that would catch a supersession index keyed on nothing."""
		one = self.a_camp("MC-Cabin-01")
		two = self.a_camp("MC-Cabin-02")
		self.an_inspection(one, "2026-07-01", findings="water stain")
		self.an_inspection(two, "2026-07-20")
		self.seed_rules()
		fired = self.fired("housing_corrective_action_open")
		self.assertEqual(len(fired), 1)
		self.assertIn(one, fired[0]["message"])
		self.assertNotIn(two, fired[0]["message"])

	def test_closing_the_corrective_action_by_hand_also_silences_it(self):
		unit = self.a_camp()
		created = self.an_inspection(unit, "2026-07-01", findings="water stain")
		self.tool_data(
			"update_housing_inspection",
			{
				"record": created["name"],
				"corrective_action_closed": "2026-07-20",
				"closure_note": "re-flashed the window and repainted",
			},
		)
		self.seed_rules()
		self.assertEqual(self.fired("housing_corrective_action_open"), [])

	def test_an_unreadable_state_does_not_count_as_clean(self):
		"""`unreadable_counts_as_dirty`, and the default is the whole point of it.

		A record whose state nobody can read is not evidence that anything was
		fixed. Treating it as clean is how a compliance file becomes a clean
		record of nothing.
		"""
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain")
		later = self.an_inspection(unit, "2026-07-20")
		STORE.get_raw("Housing Inspection", later["name"])["workflow_state"] = ""
		self.seed_rules()
		self.assertEqual(len(self.fired("housing_corrective_action_open")), 1)

	def test_the_flag_can_be_turned_off_and_then_an_empty_state_supersedes(self):
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain")
		later = self.an_inspection(unit, "2026-07-20")
		STORE.get_raw("Housing Inspection", later["name"])["workflow_state"] = ""
		self.seed_rules()
		row = self.edit_rule(
			"housing_corrective_action_open",
			superseded_by_later_clean_json=json.dumps(
				{
					"subject_field": "unit",
					"clean_state_field": "workflow_state",
					"clean_state_values": [records.RECORDED],
					"unreadable_counts_as_dirty": False,
				}
			),
		)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	# ── the same primitive, the other rule ──────────────────────────────────
	def test_a_contaminated_sample_with_nothing_after_it_fires(self):
		zone = self.a_zone()
		self.a_water_test(zone, "2026-07-01", dirty=True)
		self.seed_rules()
		fired = self.fired("water_test_contamination")
		self.assertEqual(len(fired), 1)
		self.assertEqual(fired[0]["severity"], "Critical")
		self.assertIn("coliform Present", fired[0]["message"])

	def test_a_later_clean_sample_from_the_same_source_supersedes_it(self):
		zone = self.a_zone()
		self.a_water_test(zone, "2026-07-01", dirty=True)
		self.a_water_test(zone, "2026-07-20")
		self.seed_rules()
		self.assertEqual(self.fired("water_test_contamination"), [])

	def test_a_supersession_config_naming_no_subject_is_refused_while_you_are_looking_at_it(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_supersession({"clean_state_field": "workflow_state"})
		self.assertIn("subject_field", str(caught.exception))

	def test_a_supersession_config_with_no_clean_values_is_refused(self):
		"""With none, NOTHING supersedes — which is the behaviour the primitive removes."""
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_supersession(
				{"subject_field": "unit", "clean_state_field": "workflow_state", "clean_state_values": []}
			)
		self.assertIn("clean_state_values", str(caught.exception))


# ── the plural target, which the camp rule needed as well ───────────────────
class OneRuleWalksTwoDoctypes(CampTestCase):
	"""`target_doctypes_json`. A cabin with a water stain and a cabin with a dead CO
	detector are the same conversation with the same person on the same walk."""

	def test_a_failed_detector_raises_the_same_rule_id(self):
		unit = self.a_camp()
		self.a_detector_test(unit, "2026-07-01", failed=True)
		self.seed_rules()
		fired = self.fired("housing_corrective_action_open")
		self.assertEqual(len(fired), 1)
		self.assertEqual(fired[0]["source_doctype"], "Detector Test")

	def test_both_doctypes_raise_under_one_rule_and_one_alert_each(self):
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain")
		self.a_detector_test(unit, "2026-07-02", failed=True)
		self.seed_rules()
		fired = self.fired("housing_corrective_action_open")
		self.assertEqual(
			sorted(entry["source_doctype"] for entry in fired), ["Detector Test", "Housing Inspection"]
		)

	def test_each_target_gets_its_own_label_in_the_message(self):
		"""'the detector test' and 'the habitability inspection' are different errands."""
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain")
		self.a_detector_test(unit, "2026-07-02", failed=True)
		self.seed_rules()
		messages = {
			entry["source_doctype"]: entry["message"]
			for entry in self.fired("housing_corrective_action_open")
		}
		self.assertIn("the habitability inspection", messages["Housing Inspection"])
		self.assertIn("the detector test", messages["Detector Test"])

	def test_each_target_supersedes_on_its_OWN_date_column(self):
		"""A Housing Inspection is dated `inspection_date` and a Detector Test
		`test_date`. A supersession that read one column for both would silently
		stop superseding the other, which is an alert that never goes away."""
		unit = self.a_camp()
		self.a_detector_test(unit, "2026-07-01", failed=True)
		self.a_detector_test(unit, "2026-07-20")
		self.seed_rules()
		self.assertEqual(self.fired("housing_corrective_action_open"), [])

	def test_a_target_doctype_this_site_has_not_got_is_skipped_rather_than_fatal(self):
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain")
		self.seed_rules()
		row = self.edit_rule(
			"housing_corrective_action_open",
			target_doctypes_json=json.dumps(
				[
					{"doctype": "Housing Inspection", "date_field": "inspection_date", "label": "the walk"},
					{"doctype": "Nonexistent Record", "date_field": "test_date", "label": "the other one"},
				]
			),
		)
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertIn("the walk", result["observations"][0]["message"])

	def test_the_primary_target_is_never_scanned_twice(self):
		"""Naming the primary in the list carries its LABEL rather than adding a
		second pass — two passes would raise every alert twice, and the second
		would collide with the first on the docname."""
		unit = self.a_camp()
		self.an_inspection(unit, "2026-07-01", findings="water stain")
		self.seed_rules()
		self.assertEqual(len(self.fired("housing_corrective_action_open")), 1)


# ── a finding's date is not a deadline ──────────────────────────────────────
class TheTimestampRole(CampTestCase):
	"""`date_field_role = Timestamp`, the helper the supersession rules needed.

	Left as a clock, a finding recorded TODAY has zero days remaining, reaches no
	band with these rules' thresholds, and stops firing on exactly the day
	somebody needs to see it.
	"""

	def test_a_finding_recorded_today_still_fires(self):
		unit = self.a_camp()
		self.an_inspection(unit, TODAY, findings="no detector in the back bedroom")
		self.seed_rules()
		self.assertEqual(len(self.fired("housing_corrective_action_open")), 1)

	def test_a_finding_dated_in_the_future_still_fires(self):
		"""The date is written past `create_housing_inspection`, which refuses a
		future walk and is right to. This is about what the ENGINE does with a row
		that has one — a backdated correction, a clock skew, a bad import."""
		unit = self.a_camp()
		created = self.an_inspection(unit, "2026-07-01", findings="loose step")
		STORE.get_raw("Housing Inspection", created["name"])["inspection_date"] = days_from_today(3)
		self.seed_rules()
		self.assertEqual(len(self.fired("housing_corrective_action_open")), 1)

	def test_a_finding_with_no_date_at_all_still_fires_and_says_so(self):
		"""A corrective action nobody dated is still open."""
		unit = self.a_camp()
		created = self.an_inspection(unit, "2026-07-01", findings="loose step")
		STORE.get_raw("Housing Inspection", created["name"])["inspection_date"] = None
		self.seed_rules()
		fired = self.fired("housing_corrective_action_open")
		self.assertEqual(len(fired), 1)
		self.assertIn("on an unrecorded date", fired[0]["message"])
		self.assertNotIn("Open ", fired[0]["message"])

	def test_the_same_rule_as_a_clock_would_have_gone_quiet_which_is_why_the_role_exists(self):
		"""The regression this field prevents, asserted rather than described."""
		unit = self.a_camp()
		self.an_inspection(unit, TODAY, findings="no detector in the back bedroom")
		self.seed_rules()
		row = self.edit_rule(
			"housing_corrective_action_open", date_field_role=compliance_rules.DATE_ROLE_CLOCK
		)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)


# ── PRIMITIVE 2: an ordered lookup on a NAME ────────────────────────────────
class RegimeHeuristics(PrimitiveTestCase):
	"""`regimes_from_field` copies tags off a column. Here there is no column."""

	def regimes_for(self, cert_type: str, cert_name: str = "") -> list:
		self.a_certificate(cert_name or f"{cert_type} 2026", cert_type=cert_type, expires_in_days=10)
		self.seed_rules()
		fired = self.fired("certification_expiring")
		self.assertEqual(len(fired), 1)
		return fired[0]["regimes"]

	def test_an_applicator_licence_is_wps_and_or_osha(self):
		self.assertEqual(self.regimes_for("Applicator License"), ["WPS", "OR-OSHA"])

	def test_a_globalgap_certificate_is_globalgap_and_not_gap(self):
		"""THE ORDERING IS THE WHOLE CONTENT. 'GlobalGAP' contains 'GAP', and a
		USDA GAP packet must not be handed another scheme's certificate."""
		self.assertEqual(self.regimes_for("GlobalGAP"), ["GlobalGAP"])

	def test_a_gap_certificate_is_gap(self):
		self.assertEqual(self.regimes_for("GAP"), ["GAP"])

	def test_a_food_safety_certificate_is_fsma_and_gap(self):
		self.assertEqual(self.regimes_for("Food Safety Training"), ["FSMA", "GAP"])

	def test_a_certificate_nothing_recognises_is_internal_rather_than_untagged(self):
		"""An alert nobody can filter to is worse than one filed under 'ours'."""
		self.assertEqual(self.regimes_for("Other"), ["Internal"])

	def test_the_type_is_read_first_and_the_name_only_as_a_fallback(self):
		"""A name somebody typed on the day must not retag a certificate whose
		TYPE already said something. This is the assertion that pins the engine's
		field order as the OUTER loop rather than the inner one."""
		self.assertEqual(
			self.regimes_for("Food Safety Training", cert_name="WPS refresher 2026"),
			["FSMA", "GAP"],
		)

	def test_the_name_is_consulted_where_the_type_said_nothing(self):
		self.assertEqual(
			self.regimes_for("Other", cert_name="Oregon Tilth organic file 2026"),
			["OTCO"],
		)

	def test_the_rule_still_carries_the_UNION_so_a_one_regime_sweep_runs_it(self):
		"""The heuristics say which regime an ALERT is; the rule's own `regimes` is
		what a `refresh_compliance_alerts(regime=...)` matches on to decide whether
		this rule has to run at all. Both, and they are different questions."""
		self.seed_rules()
		row = self.rule_named("certification_expiring")
		self.assertIn("WPS", row["regimes"])
		self.assertIn("GlobalGAP", row["regimes"])
		self.assertIn("Internal", row["regimes"])

	def test_a_heuristic_entry_with_no_value_is_refused(self):
		"""An entry that matches on nothing matches EVERYTHING that reaches it."""
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_heuristics(
				[{"if_field_contains": {"field": "cert_type"}, "then_regimes": ["WPS"]}],
				"regime_heuristics",
				"regimes",
			)
		self.assertIn("matches on nothing", str(caught.exception))

	def test_a_heuristic_producing_a_regime_nobody_ships_is_refused_by_name(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_heuristics([{"default_regimes": ["SQF"]}], "regime_heuristics", "regimes")
		self.assertIn("SQF", str(caught.exception))

	def test_the_parsed_table_can_be_parsed_again(self):
		"""IT IS PARSED ON SAVE, STORED, AND PARSED AGAIN ON EVERY SWEEP. A parser
		whose own output it cannot read is a rule that validates once and then
		quietly matches nothing."""
		once = compliance_rules.parse_heuristics(
			[
				{
					"if_field_contains": {"field": ["cert_type", "cert_name"], "value": ["wps"]},
					"then_regimes": ["WPS"],
				},
				{"default_regimes": ["Internal"]},
			],
			"regime_heuristics",
			"regimes",
		)
		twice = compliance_rules.parse_heuristics(once, "regime_heuristics", "regimes")
		self.assertEqual(once, twice)
		self.assertEqual(
			compliance_rules.match_heuristics(twice, {"cert_type": "WPS Handler"}, "regimes"), ["WPS"]
		)


class CategoryHeuristics(PrimitiveTestCase):
	"""The same shape, producing the alert's CATEGORY. One rule, eleven kinds of row."""

	def category_for(self, cert_type: str) -> str:
		self.a_certificate(f"{cert_type} 2026", cert_type=cert_type, expires_in_days=10)
		self.seed_rules()
		return self.fired("certification_expiring")[0]["category"]

	def test_an_applicator_licence_is_a_workforce_item(self):
		self.assertEqual(self.category_for("Applicator License"), "Workforce")

	def test_an_flc_licence_is_a_workforce_item(self):
		self.assertEqual(self.category_for("Farm Labor Contractor License"), "Workforce")

	def test_a_globalgap_certificate_is_a_certifications_item(self):
		self.assertEqual(self.category_for("GlobalGAP"), "Certifications")

	def test_a_category_nothing_groups_by_is_refused_at_authoring_time(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_heuristics(
				[{"default_category": "Paperwork"}], "category_heuristics", "category"
			)
		self.assertIn("Paperwork", str(caught.exception))


# ── PRIMITIVE 3: a second date, used only as a gate ─────────────────────────
class TheGateDate(PrimitiveTestCase):
	"""A conjunction over two independent dates. Neither half fires alone."""

	def a_block(self, sprayed_days_ago=60, tested_days_ago=None, **overrides):
		self.a_parcel()
		data = self.tool_data(
			"create_field",
			{"parcel": "Mill Creek", "field_name": "Yellow Camp Block 3", "acreage": 12.5, **overrides},
		)
		row = STORE.get_raw("Field", data["name"])
		row["last_spray_date"] = None if sprayed_days_ago is None else days_from_today(-sprayed_days_ago)
		row["water_test_last_date"] = None if tested_days_ago is None else days_from_today(-tested_days_ago)
		return data["name"]

	def test_sprayed_inside_the_season_with_no_water_test_fires(self):
		self.a_block(sprayed_days_ago=60, tested_days_ago=None)
		self.seed_rules()
		fired = self.fired("water_test_stale")
		self.assertEqual(len(fired), 1)
		self.assertIn("was sprayed 60 day(s) ago", fired[0]["message"])
		self.assertIn("NO agricultural water test on record", fired[0]["message"])

	def test_sprayed_outside_the_season_raises_nothing_however_stale_the_water(self):
		"""Ground nobody is spraying is not unsafe, it is dormant. Subpart E is
		engaged by water contacting a crop and not by a date passing."""
		self.a_block(sprayed_days_ago=200, tested_days_ago=None)
		self.seed_rules()
		self.assertEqual(self.fired("water_test_stale"), [])

	def test_sprayed_inside_the_season_with_a_current_test_raises_nothing(self):
		self.a_block(sprayed_days_ago=60, tested_days_ago=30)
		self.seed_rules()
		self.assertEqual(self.fired("water_test_stale"), [])

	def test_sprayed_inside_the_season_with_a_stale_test_fires_and_names_the_age(self):
		self.a_block(sprayed_days_ago=60, tested_days_ago=118)
		self.seed_rules()
		fired = self.fired("water_test_stale")
		self.assertEqual(len(fired), 1)
		self.assertIn("was last water-tested 118 days ago", fired[0]["message"])

	def test_a_block_with_no_spray_date_at_all_is_gated_OUT(self):
		"""The asymmetry with `missing_date_behaviour`, and both readings are right.

		No inspection ever recorded is the most overdue cabin there is. No spray
		ever recorded is the LEAST urgent block there is.
		"""
		self.a_block(sprayed_days_ago=None, tested_days_ago=None)
		self.seed_rules()
		self.assertEqual(self.fired("water_test_stale"), [])

	def test_fallow_ground_raises_nothing_even_inside_the_season(self):
		self.a_block(sprayed_days_ago=60, tested_days_ago=None, condition="Fallow")
		self.seed_rules()
		self.assertEqual(self.fired("water_test_stale"), [])

	def test_the_gate_interval_is_a_field_somebody_can_move(self):
		self.a_block(sprayed_days_ago=200, tested_days_ago=None)
		self.seed_rules()
		row = self.edit_rule("water_test_stale", gate_within_days=365)
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 1)

	# ── the related-table variant ───────────────────────────────────────────
	def a_related_gate_rule(self, within: int = 120) -> dict:
		"""The same gate, read off ANOTHER doctype's newest row rather than a column.

		Written against Housing Inspection → Housing Unit because those are two
		doctypes this app ships that point at each other; the shape is what
		matters, and it is the shape a site keeping a spray LOG rather than a
		spray COLUMN would use for `water_test_stale`.
		"""
		spec = {
			"rule_id": "cabin_walked_recently_but_unfit",
			"title": "A cabin somebody walked recently is marked unfit",
			"category": "Housing",
			"target_doctype": "Housing Unit",
			"date_field": "",
			"severity_expired": "Warning",
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"due_date_mode": "Today",
			"gate_within_days": within,
			"gate_scope": compliance_rules.GATE_LATEST_RELATED,
			"gate_related_table": {
				"doctype": "Housing Inspection",
				"subject_field": "unit",
				"date_field": "inspection_date",
				"subject_key": "name",
			},
			"kairotic_gate_description": "Fires only on a unit somebody has actually walked lately.",
			"message_template": "{{ name }} was walked {{ gate_days_since }} day(s) ago.",
			"regimes": ["Internal"],
			"enabled": 1,
			"human_approved_by": "Administrator",
			"human_approved_on": frappe.utils.now(),
		}
		doc = compliance_rules.build_rule(spec)
		doc.insert(ignore_permissions=True)
		return compliance_rules.rule_row(doc.name)

	def a_camp(self, unit_name="MC-Cabin-01") -> str:
		self.a_parcel()
		return self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": unit_name,
				"unit_type": "Cabin",
				"square_footage": 400,
				"capacity": 4,
			},
		)["name"]

	def test_a_related_gate_reads_the_newest_row_of_another_doctype(self):
		unit = self.a_camp()
		self.an_inspection(unit, days_from_today(-200))
		self.an_inspection(unit, days_from_today(-10))
		row = self.a_related_gate_rule()
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertIn("walked 10 day(s) ago", result["observations"][0]["message"])

	def test_a_related_gate_with_only_old_rows_gates_the_row_out(self):
		unit = self.a_camp()
		self.an_inspection(unit, days_from_today(-200))
		row = self.a_related_gate_rule()
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	def test_a_related_gate_with_no_rows_at_all_gates_the_row_out(self):
		self.a_camp()
		row = self.a_related_gate_rule()
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	def test_a_related_gate_table_missing_a_key_is_refused_at_authoring_time(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_gate_table({"doctype": "Farm Task", "subject_field": "field"})
		self.assertIn("date_field", str(caught.exception))


# ── PRIMITIVE 4: several anchors of the same kind ───────────────────────────
class PluralDateFields(PrimitiveTestCase):
	"""Either being stale fires, and the message must name WHICH."""

	def a_cabin_with(self, smoke_days_ago, co_days_ago, **overrides) -> str:
		self.a_parcel()
		payload = {
			"parcel": "Mill Creek",
			"unit_name": "MC-Cabin-01",
			"unit_type": "Cabin",
			"square_footage": 400,
			"capacity": 4,
			"fsma_worker_facility": True,
		}
		payload.update(overrides)
		name = self.tool_data("create_housing_unit", payload)["name"]
		row = STORE.get_raw("Housing Unit", name)
		row["smoke_detector_last_test"] = None if smoke_days_ago is None else days_from_today(-smoke_days_ago)
		row["co_detector_last_test"] = None if co_days_ago is None else days_from_today(-co_days_ago)
		return name

	def test_one_stale_detector_produces_one_alert_naming_that_detector(self):
		self.a_cabin_with(smoke_days_ago=400, co_days_ago=30)
		self.seed_rules()
		fired = self.fired("housing_detector_test_stale")
		self.assertEqual(len(fired), 1)
		self.assertIn("the smoke detector was last tested 400 days ago", fired[0]["message"])
		self.assertNotIn("CO detector", fired[0]["message"])

	def test_the_other_stale_detector_names_the_other_one(self):
		self.a_cabin_with(smoke_days_ago=30, co_days_ago=400)
		self.seed_rules()
		fired = self.fired("housing_detector_test_stale")
		self.assertEqual(len(fired), 1)
		self.assertIn("the CO detector was last tested 400 days ago", fired[0]["message"])
		self.assertNotIn("smoke detector", fired[0]["message"])

	def test_both_stale_produce_ONE_alert_naming_both(self):
		"""One cabin is one errand. Two alerts would be two lines on a board for
		one trip up the same set of steps."""
		self.a_cabin_with(smoke_days_ago=400, co_days_ago=500)
		self.seed_rules()
		fired = self.fired("housing_detector_test_stale")
		self.assertEqual(len(fired), 1)
		self.assertIn("the smoke detector was last tested 400 days ago", fired[0]["message"])
		self.assertIn("and the CO detector was last tested 500 days ago", fired[0]["message"])

	def test_a_detector_never_tested_reads_differently_from_one_tested_long_ago(self):
		"""'No CO detector test has ever been recorded' is a different errand from
		'the CO detector was last tested 400 days ago'."""
		self.a_cabin_with(smoke_days_ago=30, co_days_ago=None)
		self.seed_rules()
		fired = self.fired("housing_detector_test_stale")
		self.assertEqual(len(fired), 1)
		self.assertIn("no CO detector test has ever been recorded", fired[0]["message"])

	def test_both_current_raises_nothing(self):
		self.a_cabin_with(smoke_days_ago=30, co_days_ago=30)
		self.seed_rules()
		self.assertEqual(self.fired("housing_detector_test_stale"), [])

	def test_a_building_that_is_not_a_worker_facility_raises_nothing(self):
		"""A shed on the same parcel is not a bunkhouse."""
		self.a_cabin_with(smoke_days_ago=400, co_days_ago=400, fsma_worker_facility=False)
		self.seed_rules()
		self.assertEqual(self.fired("housing_detector_test_stale"), [])

	def test_a_check_box_holding_the_string_zero_is_still_not_ticked(self):
		"""`istrue`, and the reason it exists rather than `isnotnull`. A Check read
		back before the database layer holds the STRING '0', which every
		truthiness test calls true — and would put a shed on the camp's list."""
		name = self.a_cabin_with(smoke_days_ago=400, co_days_ago=400)
		STORE.get_raw("Housing Unit", name)["fsma_worker_facility"] = "0"
		self.seed_rules()
		self.assertEqual(self.fired("housing_detector_test_stale"), [])

	def test_a_date_fields_entry_naming_no_field_is_refused(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_date_fields([{"label": "smoke"}])
		self.assertIn("field", str(caught.exception))


# ── the band reorder the per-row window needed ──────────────────────────────
class TheOuterWindowOutranksTheCriticalBand(PrimitiveTestCase):
	"""v0.22.1 moved the outer-window check ahead of the critical band.

	It is indistinguishable from the legacy scanner until the PER-ROW window is
	narrower than the rule's critical threshold — a certificate whose issuing body
	turns renewals round in ten days. The window is the claim about when the work
	can usefully start, and nothing inside the rule outranks it.
	"""

	def test_a_certificate_outside_its_own_narrow_window_says_nothing(self):
		self.a_certificate("Quick Turnaround Permit", expires_in_days=20, renewal_window_days=10)
		self.seed_rules()
		self.assertEqual(self.fired("certification_expiring"), [])

	def test_the_same_certificate_inside_its_own_window_is_critical(self):
		self.a_certificate("Quick Turnaround Permit", expires_in_days=8, renewal_window_days=10)
		self.seed_rules()
		fired = self.fired("certification_expiring")
		self.assertEqual(len(fired), 1)
		self.assertEqual(fired[0]["severity"], "Critical")

	def test_a_wide_window_still_bands_critical_inside_thirty_days(self):
		self.a_certificate("GlobalGAP 2026", expires_in_days=20, renewal_window_days=90)
		self.seed_rules()
		self.assertEqual(self.fired("certification_expiring")[0]["severity"], "Critical")

	def test_a_wide_window_bands_warning_outside_thirty_days(self):
		self.a_certificate("GlobalGAP 2026", expires_in_days=60, renewal_window_days=90)
		self.seed_rules()
		self.assertEqual(self.fired("certification_expiring")[0]["severity"], "Warning")


# ── THE PROMISE, PER RULE ───────────────────────────────────────────────────
class TheMigrationOfEachRuleChangesNothing(PrimitiveTestCase):
	"""Five rules, five fixtures, five snapshot diffs of exactly zero rows.

	`TheMigrationChangesNothing` proved v0.22.0's thirteen against the Python
	they replaced, on one fixture. This proves v0.22.1's five the same way, one
	rule at a time — because a parity test over a fixture that fires two rules
	proves two rules, and a failure that names WHICH rule drifted is worth more
	than one that says a rule did.
	"""

	def a_world_for(self, rule_id: str) -> None:
		"""A database built to make ONE rule speak, in as many of its shapes as it has."""
		if rule_id == "certification_expiring":
			self.a_certificate("GlobalGAP 2026", cert_type="GlobalGAP", expires_in_days=45)
			self.a_certificate("Applicator License 2026", cert_type="Applicator License", expires_in_days=-12)
			self.a_certificate(
				"Water Test Cert 2026", cert_type="Water Test Certification", expires_in_days=20
			)
			self.a_certificate("Unclassified Permit", cert_type="Other", expires_in_days=5)
			self.a_certificate("Retired One", cert_type="GAP", expires_in_days=3, status="Superseded")
			self.a_certificate("No Body", cert_type="GAP", expires_in_days=40, issuing_body="")
		elif rule_id == "water_test_stale":
			self.a_parcel()
			for name, sprayed, tested in (
				("Block A", 11, 118),
				("Block B", 60, None),
				("Block C", 200, 400),
				("Block D", 30, 30),
			):
				data = self.tool_data(
					"create_field", {"parcel": "Mill Creek", "field_name": name, "acreage": 12.5}
				)
				row = STORE.get_raw("Field", data["name"])
				row["last_spray_date"] = None if sprayed is None else days_from_today(-sprayed)
				row["water_test_last_date"] = None if tested is None else days_from_today(-tested)
		elif rule_id == "housing_detector_test_stale":
			self.a_parcel()
			for name, smoke, co in (
				("MC-Cabin-01", 400, 400),
				("MC-Cabin-02", 400, 30),
				("MC-Cabin-03", 30, None),
				("MC-Cabin-04", 30, 30),
			):
				unit = self.tool_data(
					"create_housing_unit",
					{
						"parcel": "Mill Creek",
						"unit_name": name,
						"unit_type": "Cabin",
						"square_footage": 400,
						"capacity": 4,
						"fsma_worker_facility": True,
					},
				)["name"]
				row = STORE.get_raw("Housing Unit", unit)
				row["smoke_detector_last_test"] = None if smoke is None else days_from_today(-smoke)
				row["co_detector_last_test"] = None if co is None else days_from_today(-co)
		elif rule_id == "housing_corrective_action_open":
			self.a_parcel()
			units = []
			for name in ("MC-Cabin-01", "MC-Cabin-02", "MC-Cabin-03"):
				units.append(
					self.tool_data(
						"create_housing_unit",
						{
							"parcel": "Mill Creek",
							"unit_name": name,
							"unit_type": "Cabin",
							"square_footage": 400,
							"capacity": 4,
							"fsma_worker_facility": True,
						},
					)["name"]
				)
			self.an_inspection(units[0], "2026-07-01", findings="water stain, north wall, spreading")
			self.an_inspection(units[1], "2026-07-02", findings="loose step")
			self.an_inspection(units[1], "2026-07-20")
			self.a_detector_test(units[2], "2026-07-05", failed=True)
			self.a_detector_test(units[0], TODAY, failed=True)
		elif rule_id == "water_test_contamination":
			one = self.a_zone("YC3-Zone2")
			two = self.a_zone("YC3-Zone3", zone_number=3)
			three = self.a_zone("YC3-Zone4", zone_number=4)
			# Still dirty; superseded clean; dirty with an EARLIER clean sample that
			# must not silence it; and a dirty one closed by hand.
			self.a_water_test(one, "2026-07-01", dirty=True)
			self.a_water_test(two, "2026-07-02", dirty=True)
			self.a_water_test(two, "2026-07-20")
			self.a_water_test(three, "2026-06-01")
			self.a_water_test(three, "2026-07-03", dirty=True)

	def snapshot(self, rule_id: str) -> dict:
		self.sweep(today=TODAY)
		rows = {}
		for row in STORE.rows("Compliance Alert"):
			if str(row.get("alert_type")) != rule_id:
				continue
			rows[str(row["name"])] = {key: _plain(row.get(key)) for key in ALERT_COLUMNS}
		return rows

	def assertMigrationChangedNothing(self, rule_id: str, primitive: str) -> None:
		"""Run one rule twice — as its shipped scanner, then as data — and diff the rows.

		Written as a helper called by one test per rule rather than as a loop, so a
		failure names WHICH rule drifted in the test name itself.
		"""
		self.a_world_for(rule_id)
		self.seed_rules()

		self.as_builtin(rule_id)
		legacy = self.snapshot(rule_id)
		self.assertGreaterEqual(
			len(legacy),
			2,
			f"the fixture for {rule_id} raised fewer than two alerts through its shipped scanner, so "
			f"this comparison covers one row and proves almost nothing about {primitive}",
		)

		self.wipe_alerts()
		self.as_declarative(rule_id)
		migrated = self.snapshot(rule_id)

		self.assertEqual(
			sorted(legacy),
			sorted(migrated),
			f"{rule_id} raises a DIFFERENT SET OF ALERT DOCNAMES as a declarative rule. A docname "
			f"carries the rule and the record and nothing that moves daily — if it changed, every "
			f"snooze and every human dismissal on this site is orphaned.",
		)
		for name in sorted(legacy):
			with self.subTest(alert=name):
				self.assertEqual(
					migrated[name],
					legacy[name],
					f"{name} says something different now that {rule_id} is declarative. The "
					f"definition moved out of Python; what it SAYS was not supposed to move with it.",
				)

	def test_certification_expiring_says_exactly_what_its_scanner_said(self):
		self.assertMigrationChangedNothing("certification_expiring", "regime_heuristics_json")

	def test_water_test_stale_says_exactly_what_its_scanner_said(self):
		self.assertMigrationChangedNothing("water_test_stale", "gate_date_field")

	def test_housing_detector_test_stale_says_exactly_what_its_scanner_said(self):
		self.assertMigrationChangedNothing("housing_detector_test_stale", "date_fields_json")

	def test_housing_corrective_action_open_says_exactly_what_its_scanner_said(self):
		self.assertMigrationChangedNothing("housing_corrective_action_open", "superseded_by_later_clean_json")

	def test_water_test_contamination_says_exactly_what_its_scanner_said(self):
		self.assertMigrationChangedNothing("water_test_contamination", "superseded_by_later_clean_json")

	def test_every_rule_this_release_migrated_has_a_parity_test_above(self):
		"""The list and the tests cannot drift into covering different rules."""
		covered = {
			name.removeprefix("test_").removesuffix("_says_exactly_what_its_scanner_said")
			for name in dir(self)
			if name.startswith("test_") and name.endswith("_says_exactly_what_its_scanner_said")
		}
		self.assertEqual(covered, {rule_id for rule_id, _primitive in MIGRATED})

	def test_the_two_permanent_builtins_are_untouched(self):
		"""They kept their scanner, their thresholds and their shape."""
		self.seed_rules()
		for rule_id in ("audit_action_overdue", "supervisor_review_lapsed"):
			with self.subTest(rule=rule_id):
				row = self.rule_named(rule_id)
				self.assertEqual(row["builtin_scanner"], rule_id)
				self.assertEqual(compliance_rules.shape_of(row), compliance_rules.SHAPE_BUILTIN)

	def test_the_six_already_declarative_rules_kept_their_definitions(self):
		"""v0.22.1 touched five rules. These six were not among them."""
		self.seed_rules()
		for rule_id, date_field in (
			("policy_review_overdue", "review_due_date"),
			("housing_inspection_overdue", "last_habitability_inspection"),
			("i9_expired", ""),
			("flc_license_expiring", "flc_license_expiration"),
			("filing_response_due", "response_due_date"),
			("training_expiring", "expires_date"),
		):
			with self.subTest(rule=rule_id):
				row = self.rule_named(rule_id)
				self.assertEqual(row["date_field"], date_field)
				self.assertEqual(row["builtin_scanner"], "")
				self.assertEqual(
					row["date_field_role"],
					compliance_rules.DATE_ROLE_CLOCK,
					"a rule that was not migrated must not have picked up a Timestamp role",
				)


# ── authoring the new primitives through MCP ────────────────────────────────
class TheToolsCarryTheNewPrimitives(PrimitiveTestCase):
	"""An operator authoring a rule through MCP has to be able to reach these, and
	to SEE what they did before approving anything."""

	def test_list_keeps_the_shape_it_had_and_gains_nothing_it_did_not_need(self):
		self.seed_rules()
		entry = {rule["alert_type"]: rule for rule in self.tool_data("list_compliance_rules", {})["rules"]}[
			"certification_expiring"
		]
		for key in (
			"alert_type",
			"title",
			"category",
			"purpose",
			"kairotic_gate",
			"framework",
			"regimes",
			"requires",
			"available",
		):
			self.assertIn(key, entry)
		self.assertEqual(entry["shape"], "declarative")

	def test_get_returns_every_new_primitive_nullable_and_named(self):
		self.seed_rules()
		definition = self.tool_data("get_compliance_rule", {"name": "certification_expiring"})["definition"]
		for key in (
			"target_doctypes",
			"date_fields",
			"date_field_role",
			"superseded_by_later_clean",
			"gate_date_field",
			"gate_within_days",
			"gate_scope",
			"gate_related_table",
			"regime_heuristics",
			"category_heuristics",
		):
			self.assertIn(key, definition, f"{key} is not readable through get_compliance_rule")
		self.assertTrue(definition["regime_heuristics"])
		self.assertEqual(definition["superseded_by_later_clean"], {})

	def test_a_rule_using_a_primitive_the_rule_does_not_use_reports_an_empty_rather_than_a_null(self):
		"""An operator reading two rules side by side should not have to know that a
		blank column and an empty list are one thing."""
		self.seed_rules()
		definition = self.tool_data("get_compliance_rule", {"name": "policy_review_overdue"})["definition"]
		self.assertEqual(definition["target_doctypes"], [])
		self.assertEqual(definition["date_fields"], [])
		self.assertEqual(definition["regime_heuristics"], [])

	def test_a_rule_can_be_authored_with_a_new_primitive_and_dry_run_before_approval(self):
		"""THE READ BETWEEN AUTHORING AND APPROVING. It has to render the primitives,
		or an operator is approving a rule they have only read the fields of."""
		self.a_parcel()
		unit = self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": "MC-Cabin-09",
				"unit_type": "Cabin",
				"square_footage": 400,
				"capacity": 4,
			},
		)["name"]
		self.an_inspection(unit, "2026-07-01", findings="rodent droppings under the sink")

		created = self.tool_data(
			"create_compliance_rule",
			{
				"rule_id": "camp_rodent_finding_open",
				"title": "A camp finding nobody has closed or superseded",
				"category": "Housing",
				"target_doctype": "Housing Inspection",
				"date_field": "inspection_date",
				"date_field_role": "Timestamp",
				"threshold_critical_days": -1,
				"threshold_warning_days": -1,
				"severity_expired": "Critical",
				"due_date_mode": "None",
				"scope_filters": [
					{"field": "workflow_state", "op": "eq", "value": records.CORRECTIVE_ACTION_REQUIRED}
				],
				"superseded_by_later_clean": {
					"subject_field": "unit",
					"clean_state_field": "workflow_state",
					"clean_state_values": [records.RECORDED],
				},
				"kairotic_gate_description": (
					"Fires on a finding still true: nobody closed it and no later clean walk of the "
					"same unit superseded it."
				),
				"message_template": "{{ unit }} — open since {{ anchor }}: {{ findings }}",
				"regimes": ["OR-OSHA"],
			},
		)
		self.assertFalse(created["enabled"], "an authored rule must arrive as a draft")
		self.assertEqual(
			created["definition"]["superseded_by_later_clean"]["subject_field"],
			"unit",
			"the primitive did not survive create_compliance_rule",
		)

		dry = self.tool_data("test_compliance_rule", {"name": "camp_rodent_finding_open"})
		self.assertEqual(dry["observed"], 1)
		self.assertIn("rodent droppings under the sink", dry["observations"][0]["message"])
		self.assertTrue(
			dry["observations"][0]["would_be_alert"].startswith(
				"camp_rodent_finding_open:Housing Inspection:"
			),
			"the dry run must show the docname each alert would take, so an operator can see "
			"before approving that it collides with nothing",
		)
		self.assertIn("draft_note", dry)

	def test_the_dry_run_goes_quiet_once_the_finding_is_superseded(self):
		"""What an operator is really checking: that the rule stops by itself."""
		self.a_parcel()
		unit = self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": "MC-Cabin-09",
				"unit_type": "Cabin",
				"square_footage": 400,
				"capacity": 4,
			},
		)["name"]
		self.an_inspection(unit, "2026-07-01", findings="rodent droppings under the sink")
		self.tool_data(
			"create_compliance_rule",
			{
				"rule_id": "camp_rodent_finding_open",
				"title": "A camp finding nobody has closed or superseded",
				"category": "Housing",
				"target_doctype": "Housing Inspection",
				"date_field": "inspection_date",
				"date_field_role": "Timestamp",
				"threshold_critical_days": -1,
				"threshold_warning_days": -1,
				"severity_expired": "Critical",
				"scope_filters": [
					{"field": "workflow_state", "op": "eq", "value": records.CORRECTIVE_ACTION_REQUIRED}
				],
				"superseded_by_later_clean": {
					"subject_field": "unit",
					"clean_state_field": "workflow_state",
					"clean_state_values": [records.RECORDED],
				},
				"kairotic_gate_description": "Fires on a finding that is still true.",
				"message_template": "{{ unit }} — open since {{ anchor }}",
				"regimes": ["OR-OSHA"],
			},
		)
		self.an_inspection(unit, "2026-07-20")
		dry = self.tool_data("test_compliance_rule", {"name": "camp_rodent_finding_open"})
		self.assertEqual(dry["observed"], 0)

	def test_a_malformed_primitive_is_refused_while_the_author_is_present(self):
		with self.assertRaises(Exception) as caught:
			self.tool_data(
				"create_compliance_rule",
				{
					"rule_id": "broken_supersession",
					"title": "Broken",
					"target_doctype": "Housing Inspection",
					"kairotic_gate_description": "Fires when something is true.",
					"superseded_by_later_clean": {"clean_state_field": "workflow_state"},
					"regimes": ["Internal"],
				},
			)
		self.assertIn("subject_field", str(caught.exception))

	def test_a_new_primitive_can_be_authored_and_the_engine_reads_it(self):
		"""Every primitive is reachable from `create_compliance_rule`, and a rule
		that names one is not silently ignored by the sweep. Checked field by field
		rather than by spot-check, because a primitive the tools accept and the
		engine drops is the exact failure this whole design is written against."""
		self.seed_rules()
		for rule_id, column in (
			("certification_expiring", "regime_heuristics_json"),
			("certification_expiring", "category_heuristics_json"),
			("water_test_stale", "gate_date_field"),
			("housing_detector_test_stale", "date_fields_json"),
			("housing_corrective_action_open", "target_doctypes_json"),
			("housing_corrective_action_open", "superseded_by_later_clean_json"),
			("water_test_contamination", "superseded_by_later_clean_json"),
		):
			with self.subTest(rule=rule_id, primitive=column):
				row = self.rule_named(rule_id)
				self.assertTrue(
					str(row.get(column) or "").strip() not in ("", "[]", "{}"),
					f"{rule_id} is supposed to carry {column} and its column is empty",
				)

	def test_updating_a_primitive_supersedes_and_reports_the_before(self):
		self.seed_rules()
		result = self.tool_data(
			"update_compliance_rule",
			{
				"name": "water_test_stale",
				"gate_within_days": 150,
				"reason": "our spray season runs longer than the shipped default",
			},
		)
		self.assertEqual(result["changes"]["gate_within_days"], {"before": 120, "after": 150})
		self.assertEqual(int(result["version"]), 2)


# ── the upgrade a v0.22.0 site actually gets ────────────────────────────────
class TheUpgradeFromV0220(PrimitiveTestCase):
	"""`patches/migrate_declarative_rules.py`, which exists because the SEEDER
	CANNOT DO THIS.

	`seed_compliance_rules` leaves alone anything already on the site — the
	property that stops an operator's raised threshold being corrected back on
	every upgrade, and therefore the reason it can never move a rule a previous
	version already wrote. A site that installed v0.22.0 has a
	`certification_expiring` row naming a built-in scanner, and without this patch
	it would keep running that scanner for ever.
	"""

	def a_v0220_site(self, **edits) -> None:
		"""The five rules as v0.22.0 left them: declarative fields cleared, scanner named."""
		self.seed_rules()
		for rule_id, _primitive in MIGRATED:
			name = compliance_rules.resolve(rule_id)
			row = STORE.get_raw(compliance_rules.DOCTYPE, name)
			row["builtin_scanner"] = rule_id
			for column in (
				"target_doctypes_json",
				"date_fields_json",
				"superseded_by_later_clean_json",
				"gate_related_table_json",
				"regime_heuristics_json",
				"category_heuristics_json",
			):
				row[column] = "[]" if column.endswith("s_json") else "{}"
			row["gate_date_field"] = ""
			row["gate_within_days"] = 0
			row["date_field_role"] = compliance_rules.DATE_ROLE_CLOCK
			row["message_template"] = ""
			row["scope_filters_json"] = "[]"
			row.update(edits.get(rule_id) or {})

	def run_the_patch(self) -> dict:
		from erpnext_mcp.patches import migrate_declarative_rules

		return migrate_declarative_rules.migrate_declarative_rules()

	def test_it_migrates_the_five_and_touches_nothing_else(self):
		self.a_v0220_site()
		report = self.run_the_patch()
		self.assertEqual(sorted(report["migrated"]), sorted(rule for rule, _p in MIGRATED))
		self.assertEqual(report["failed"], [])
		for rule_id in ("audit_action_overdue", "supervisor_review_lapsed"):
			with self.subTest(rule=rule_id):
				self.assertEqual(self.rule_named(rule_id)["builtin_scanner"], rule_id)

	def test_the_migrated_rules_are_declarative_afterwards(self):
		self.a_v0220_site()
		self.run_the_patch()
		for rule_id, _primitive in MIGRATED:
			with self.subTest(rule=rule_id):
				row = self.rule_named(rule_id)
				self.assertEqual(row["builtin_scanner"], "")
				self.assertEqual(compliance_rules.shape_of(row), compliance_rules.SHAPE_DECLARATIVE)
				self.assertEqual(int(row["version"]), 2)

	def test_it_supersedes_rather_than_edits_so_april_stays_readable(self):
		"""The old row is still on the site in full, disabled, pointing at the new
		one — so an alert raised under it can still be read against the definition
		that raised it."""
		self.a_v0220_site()
		before = compliance_rules.resolve("water_test_stale")
		self.run_the_patch()
		old = compliance_rules.rule_row(before)
		self.assertEqual(old["builtin_scanner"], "water_test_stale")
		self.assertFalse(old["enabled"])
		self.assertTrue(old["superseded_by"])
		self.assertNotEqual(old["superseded_by"], before)

	def test_it_is_a_no_op_the_second_time(self):
		self.a_v0220_site()
		self.run_the_patch()
		self.assertEqual(self.run_the_patch()["migrated"], [])
		# Twenty since v0.28.0 — the three I-9 and two W-4 rules are declarative
		# records the patch has nothing to say about and leaves exactly alone.
		# Twenty-one since v0.39.0 (financial_kpi_threshold_breach) and
		# twenty-two since v0.42.0 (budget_variance_breach) — both seeded
		# fresh rather than migrated, and both equally untouched by this patch.
		# Twenty-six since v0.55.0, on the same reading: the four
		# missing-signature rules postdate the v0.22.0 the patch migrates FROM,
		# so there is nothing of theirs for it to have migrated. Twenty-seven
		# since v0.56.0 added the fifth, on the employer's own tax returns.
		# Thirty since v0.69.0, on the same reading again: the two spray-interval
		# rules and the reorder rule were authored long after v0.22.0 and are
		# seeded fresh, so the patch has nothing of theirs to migrate either.
		#
		# v0.80.0 adds the IPO-readiness GATES on exactly the same reading a third
		# time, and they are counted apart from the thirty rather than folded into
		# the number. This test is about what the v0.22.0 patch MIGRATES, and a
		# gate is not something it could ever have migrated: control points did not
		# exist until this release, so there is no v0.22.0 site with one to carry
		# forward. Deriving the addend from `enforcement.CONTROL_POINTS` keeps the
		# sentence above true — the patch still leaves exactly thirty swept rules —
		# without this assertion breaking every time a phase adds a control.
		swept = [row for row in compliance_rules.rule_rows() if not row.get("control_point")]
		self.assertEqual(len(swept), 32)
		# v0.203.0. The pre-occupancy bait gate seeds OFF (Tim switches the rodent
		# program on), so it is not a live row.
		self.assertEqual(
			len(compliance_rules.rule_rows()),
			32 + len(enforcement.CONTROL_POINTS) - len(enforcement._NON_FINANCE),
		)

	def test_an_operator_edited_threshold_survives_the_migration(self):
		"""The question the patch exists to answer well. A site that contracted its
		annual detector cycle at ten months still has ten months afterwards."""
		self.a_v0220_site(housing_detector_test_stale={"cadence_days": 300})
		self.run_the_patch()
		self.assertEqual(int(self.rule_named("housing_detector_test_stale")["cadence_days"]), 300)

	def test_an_operator_added_scope_filter_survives_and_is_ANDED_with_the_shipped_one(self):
		"""In v0.22.0 a built-in rule seeded with an EMPTY filter list, so anything
		in that column was added by an operator on top of the scanner's own scoping.
		Replacing the list would silently widen a rule somebody narrowed."""
		self.a_v0220_site(
			housing_detector_test_stale={
				"scope_filters_json": json.dumps([{"field": "unit_name", "op": "contains", "value": "Cabin"}])
			}
		)
		self.run_the_patch()
		filters = compliance_rules.parse_filters(
			self.rule_named("housing_detector_test_stale")["scope_filters_json"]
		)
		fields = [entry["field"] for entry in filters]
		self.assertIn("unit_name", fields)
		self.assertIn("fsma_worker_facility", fields)

	def test_a_tuned_spray_season_is_read_across_into_the_new_gate(self):
		"""It lived in `extra_parameters` because v0.22.0 had no column for it. A
		site that tuned it would otherwise have that tuning quietly ignored."""
		self.a_v0220_site(water_test_stale={"extra_parameters_json": json.dumps({"spray_season_days": 200})})
		self.run_the_patch()
		row = self.rule_named("water_test_stale")
		self.assertEqual(int(row["gate_within_days"]), 200)
		self.assertEqual(
			compliance_rules.as_object(row["extra_parameters_json"])["spray_season_days"],
			200,
			"the key stays in extra_parameters too, so anything reading it by name still works",
		)

	def test_a_rule_somebody_switched_off_stays_off(self):
		self.a_v0220_site(water_test_contamination={"enabled": 0, "active_row_flag": 0})
		self.run_the_patch()
		self.assertFalse(self.rule_named("water_test_contamination")["enabled"])

	def test_a_rule_that_is_already_declarative_is_left_alone(self):
		"""A site installing fresh at v0.22.1 gets the new shape from the seeder,
		and this patch must not then write a version 2 of every rule on it."""
		self.seed_rules()
		report = self.run_the_patch()
		self.assertEqual(sorted(report["already"]), sorted(rule for rule, _p in MIGRATED))
		self.assertEqual(report["migrated"], [])

	def test_a_site_with_no_rules_at_all_is_not_an_error(self):
		report = self.run_the_patch()
		self.assertEqual(report["absent"], [rule for rule, _p in MIGRATED])
		self.assertEqual(report["failed"], [])

	def test_the_migrated_rules_say_the_same_thing_the_scanners_did(self):
		"""The end of the whole chain: an upgraded site's calendar is unchanged.

		Same fixture, same night. Snapshot the calendar with the site as v0.22.0
		left it, run the patch, sweep again, and diff the rows.
		"""
		self.a_parcel()
		unit = self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": "MC-Cabin-01",
				"unit_type": "Cabin",
				"square_footage": 400,
				"capacity": 4,
				"fsma_worker_facility": True,
			},
		)["name"]
		housing = STORE.get_raw("Housing Unit", unit)
		housing["smoke_detector_last_test"] = days_from_today(-400)
		housing["co_detector_last_test"] = days_from_today(-30)
		self.an_inspection(unit, "2026-07-01", findings="water stain, north wall")
		self.a_certificate("GlobalGAP 2026", cert_type="GlobalGAP", expires_in_days=45)
		self.a_certificate("Applicator License 2026", cert_type="Applicator License", expires_in_days=-12)
		zone = self.a_zone()
		self.a_water_test(zone, "2026-07-02", dirty=True)
		block = self.tool_data(
			"create_field", {"parcel": "Mill Creek", "field_name": "Block A", "acreage": 12.5}
		)["name"]
		field_row = STORE.get_raw("Field", block)
		field_row["last_spray_date"] = days_from_today(-11)
		field_row["water_test_last_date"] = days_from_today(-118)

		self.a_v0220_site()
		legacy = self.snapshot_all()

		self.wipe_alerts()
		self.run_the_patch()
		migrated = self.snapshot_all()

		self.assertGreaterEqual(len(legacy), 5, "the fixture did not exercise enough rules")
		self.assertEqual(sorted(legacy), sorted(migrated))
		for name in sorted(legacy):
			with self.subTest(alert=name):
				self.assertEqual(migrated[name], legacy[name])

	def snapshot_all(self) -> dict:
		self.sweep(today=TODAY)
		return {
			str(row["name"]): {key: _plain(row.get(key)) for key in ALERT_COLUMNS}
			for row in STORE.rows("Compliance Alert")
		}
