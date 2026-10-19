# SPDX-License-Identifier: MIT
"""The labor camp: what buildings there are, and who slept where.

Five things these tests are really about.

OVERLAP IS REFUSED, AND THEN ALLOWED ON REQUEST. Two people in one cabin on one
night is a typo most of the time and the point of a bunk room the rest of the
time. Both halves are tested, because a tool that only did the first would make
the barracks unusable and one that only did the second would let a typo become a
bed somebody does not have. The boundary case has its own test: somebody moving
out on the 15th and somebody moving in on the 15th DID share the cabin that
night, and a half-open comparison would say otherwise.

NOTHING DELETES. `end_housing_assignment` writes a date; the row stays. There is
a test counting the rows before and after, because "the record is the audit
trail" is only true if the record is still there.

THE EMPLOYEE LINK IS SOFT UNTIL AN HR APP MAKES IT HARD. On a site with no HR
app the name is stored as text and the tool says so; with Frappe HR installed, an
assignment naming somebody not on file is refused. Both are tested, because a
camp roster that cannot be written until an HR module exists is a camp roster
nobody keeps.

THE OCCUPANCY LIMIT IS COMPUTED ONCE AND THEN RESPECTED. 50 square feet per
occupant gives a default; a number somebody typed for a fixed bunk layout is kept
even when the square footage changes. Both branches have a test.

`WovenNotShadow` PROVES THE COMPLIANCE FIELDS ARE NOT A SHADOW LAYER. Each test
removes one and shows the SAME removal breaks an operational answer and a
regulatory one.
"""

import frappe

from erpnext_mcp import compat, compliance_fields

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import STORE

ALL_ON = {
	"allow_create_parcel": 1,
	"allow_create_housing_unit": 1,
	"allow_update_housing_unit": 1,
	"allow_list_housing_units": 1,
	"allow_get_housing_unit": 1,
	"allow_create_housing_assignment": 1,
	"allow_end_housing_assignment": 1,
	"allow_list_housing_assignments": 1,
	"allow_get_housing_capacity": 1,
	"allow_get_employee_housing_history": 1,
	"allow_create_asset": 1,
}


class HousingTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def a_parcel(self, parcel_name="Mill Creek", acreage=131.43, company=MAIN, **overrides):
		payload = {
			"owning_entity": company,
			"parcel_name": parcel_name,
			"acreage": acreage,
			"use_type": "Labor Housing",
		}
		payload.update(overrides)
		return self.tool_data("create_parcel", payload)

	def a_unit(self, unit_name="MC-Cabin-01", parcel="Mill Creek", **overrides):
		payload = {
			"parcel": parcel,
			"unit_name": unit_name,
			"unit_type": "Cabin",
			"square_footage": 384,
			"capacity": 4,
			"year_built": 1972,
			"condition": "Fair",
		}
		payload.update(overrides)
		return self.tool_data("create_housing_unit", payload)

	def an_assignment(self, unit="MC-Cabin-01", employee="Antony", **overrides):
		payload = {"unit": unit, "employee": employee, "assigned_date": "2026-06-01"}
		payload.update(overrides)
		return self.tool_data("create_housing_assignment", payload)

	def a_camp(self):
		self.a_parcel()
		return self.a_unit()


# ── the two camp backlogs, and the one the register used to hide ────────────
class TheDetectorBacklogIsInTheRegisterToo(HousingTestCase):
	"""v0.93.0. The camp register named the habitability backlog and said nothing
	about the detector backlog, while the compliance calendar raised warnings for
	BOTH. A camp manager reading `list_housing_units` to plan the morning walked
	the cabins it listed and left every detector warning open — twenty overdue
	inspections and seventeen overdue detector tests are two different errands,
	and only one of them was visible where somebody would look.

	The scope is copied from `housing_detector_test_stale` rather than reinvented:
	the flag is what puts a building inside Subpart L, and the register and the
	calendar naming different sets of cabins would be worse than the silence.
	"""

	TODAY = "2026-07-24"

	def a_facility(self, unit_name="MC-Cabin-01", **overrides):
		payload = {"fsma_worker_facility": True}
		payload.update(overrides)
		return self.a_unit(unit_name, **payload)

	def test_a_worker_facility_never_tested_is_named_by_the_register(self):
		self.a_parcel()
		unit = self.a_facility()["name"]
		data = self.tool_data("list_housing_units", {"company": MAIN})
		self.assertIn(unit, data["overdue_detector_tests"])
		row = next(entry for entry in data["units"] if entry["name"] == unit)
		self.assertTrue(row["detector_test_overdue"])
		self.assertEqual(row["detectors_overdue"], ["smoke", "CO"])

	def test_recording_both_dates_takes_it_off_the_list(self):
		self.a_parcel()
		unit = self.a_facility(smoke_detector_last_test=self.TODAY, co_detector_last_test=self.TODAY)["name"]
		data = self.tool_data("list_housing_units", {"company": MAIN})
		self.assertNotIn(unit, data["overdue_detector_tests"])
		row = next(entry for entry in data["units"] if entry["name"] == unit)
		self.assertFalse(row["detector_test_overdue"])
		self.assertEqual(row["detectors_overdue"], [])

	def test_one_stale_detector_names_which_one(self):
		"""An alert saying only 'a detector is overdue' sends somebody to test the
		wrong one, and so would a register column."""
		self.a_parcel()
		unit = self.a_facility(smoke_detector_last_test=self.TODAY, co_detector_last_test="2024-01-01")[
			"name"
		]
		row = next(
			entry
			for entry in self.tool_data("list_housing_units", {"company": MAIN})["units"]
			if entry["name"] == unit
		)
		self.assertEqual(row["detectors_overdue"], ["CO"])
		self.assertFalse(row["smoke_detector_overdue"])
		self.assertTrue(row["co_detector_overdue"])

	def test_a_building_outside_subpart_l_reports_none_rather_than_false(self):
		"""NOT False. A shed on the parcel is never asked for a detector test, and
		False would read as 'tested and fine' — the one wrong answer. Same reason
		the safety rates come back None rather than 0.0 with no hours supplied."""
		self.a_parcel()
		unit = self.a_unit("MC-Shed-01", fsma_worker_facility=False)["name"]
		row = next(
			entry
			for entry in self.tool_data("list_housing_units", {"company": MAIN})["units"]
			if entry["name"] == unit
		)
		self.assertFalse(row["detectors_required"])
		self.assertIsNone(row["detector_test_overdue"])
		self.assertNotIn(
			unit, self.tool_data("list_housing_units", {"company": MAIN})["overdue_detector_tests"]
		)

	def test_an_uninhabitable_facility_is_not_chased_for_a_detector_test(self):
		"""Assignments into it are already refused, so there is nobody to protect
		— the same gate the alert rule applies."""
		self.a_parcel()
		unit = self.a_facility("MC-Cabin-02", condition="Uninhabitable")["name"]
		data = self.tool_data("list_housing_units", {"company": MAIN})
		self.assertNotIn(unit, data["overdue_detector_tests"])

	def test_the_capacity_report_counts_the_two_backlogs_separately(self):
		"""They are different errands with different skills and different
		evidence, and one number covering both is a number nobody can plan from."""
		self.a_parcel()
		self.a_facility("MC-Cabin-01", last_habitability_inspection=self.TODAY)
		self.a_facility(
			"MC-Cabin-02",
			smoke_detector_last_test=self.TODAY,
			co_detector_last_test=self.TODAY,
		)
		data = self.tool_data("get_housing_capacity", {"company": MAIN})
		self.assertEqual(data["overdue_inspection_count"], 1)
		self.assertEqual(data["overdue_detector_test_count"], 1)
		self.assertEqual(data["detector_window_days"], 365)
		self.assertTrue(any("Overdue detector tests: 1" in line for line in data["readout"]))


# ── create_housing_unit ─────────────────────────────────────────────────────
class CreateHousingUnit(HousingTestCase):
	def test_it_registers_a_unit_under_a_docname_carrying_the_parcel(self):
		self.a_parcel()
		data = self.a_unit()
		self.assertEqual(data["name"], "MC-Cabin-01 - MC")
		self.assertEqual(data["unit_name"], "MC-Cabin-01")
		self.assertEqual(data["parcel"], "Mill Creek - ETC")

	def test_the_owning_entity_comes_from_the_parcel(self):
		self.a_parcel()
		self.assertEqual(self.a_unit()["owning_entity"], MAIN)

	def test_every_camp_may_number_its_cabins_from_one(self):
		self.a_parcel("Mill Creek")
		self.a_parcel("Green Camp", acreage=19.87)
		self.assertEqual(self.a_unit("Cabin-01", "Mill Creek")["name"], "Cabin-01 - MC")
		self.assertEqual(self.a_unit("Cabin-01", "Green Camp")["name"], "Cabin-01 - GC")

	def test_a_duplicate_unit_name_on_one_parcel_is_refused(self):
		self.a_camp()
		error = self.tool_error("create_housing_unit", {"parcel": "Mill Creek", "unit_name": "MC-Cabin-01"})
		self.assertIn("already has a unit", error)
		self.assertIn("Nothing was created", error)

	def test_the_lawful_occupancy_is_computed_at_fifty_square_feet_a_head(self):
		self.a_parcel()
		self.assertEqual(self.a_unit(square_footage=384)["max_occupants_per_or_law"], 7)

	def test_it_rounds_down_because_the_last_partial_fifty_is_not_a_person(self):
		self.a_parcel()
		self.assertEqual(self.a_unit(square_footage=149)["max_occupants_per_or_law"], 2)

	def test_a_supplied_occupancy_limit_is_kept_rather_than_recomputed(self):
		"""A cabin with a fixed bunk layout gets the number somebody worked out."""
		self.a_parcel()
		self.assertEqual(
			self.a_unit(square_footage=384, max_occupants_per_or_law=4)["max_occupants_per_or_law"],
			4,
		)

	def test_it_names_the_basis_for_the_computed_limit(self):
		self.a_parcel()
		data = self.a_unit()
		self.assertIn("1910.142", data["lawful_occupancy_basis"])
		self.assertIn("Oregon", data["lawful_occupancy_basis"])

	def test_a_capacity_over_the_lawful_occupancy_is_flagged(self):
		self.a_parcel()
		data = self.a_unit(square_footage=100, capacity=6)
		self.assertTrue(data["capacity_over_lawful_occupancy"])
		self.assertTrue(any("exceeds" in warning for warning in data["warnings"]))

	def test_a_big_capacity_outside_a_multi_unit_building_is_warned_not_refused(self):
		"""A twenty-person cabin is barracks by another name — and some really
		are, so this is recorded rather than refused."""
		self.a_parcel()
		data = self.a_unit(unit_type="Cabin", capacity=24, square_footage=4000)
		self.assertIn("barracks", data["warnings"][0])
		self.assertIn("Multi-Unit Building", data["warnings"][0])
		self.assertTrue(STORE.get_raw("Housing Unit", data["name"]))

	def test_the_same_capacity_in_a_multi_unit_building_is_not_warned_about(self):
		self.a_parcel()
		data = self.a_unit(unit_type="Multi-Unit Building", capacity=24, square_footage=4000)
		self.assertFalse(any("barracks" in warning for warning in data.get("warnings", [])))

	def test_a_missing_square_footage_says_what_it_costs(self):
		self.a_parcel()
		data = self.a_unit(square_footage=0)
		self.assertTrue(any("defensible occupancy limit" in w for w in data["warnings"]))

	def test_or_housing_law_status_defaults_to_unknown_not_to_no(self):
		"""An operator who has not looked should not be recorded as having found
		a violation."""
		self.a_parcel()
		self.assertEqual(self.a_unit()["or_housing_law_compliant"], "Unknown")

	def test_the_fsma_flag_is_stored(self):
		self.a_parcel()
		self.assertTrue(self.a_unit(fsma_worker_facility=True)["fsma_worker_facility"])

	def test_the_fsma_flag_defaults_to_false_not_to_a_truthy_zero_string(self):
		"""`bool("0")` is True, and a Check field really can come back as the
		string. A unit wrongly reported inside the Produce Safety Rule is an
		answer somebody would act on."""
		self.a_parcel()
		self.assertFalse(self.a_unit()["fsma_worker_facility"])

	def test_an_asset_on_another_companys_books_is_refused(self):
		from .fixtures import ASSET_CATEGORY

		self.a_parcel()
		STORE.seed(
			"Asset",
			[
				{
					"name": "ACC-ASS-9001",
					"asset_name": "Other Cabin",
					"company": OTHER,
					"asset_category": ASSET_CATEGORY,
					"gross_purchase_amount": 1000,
					"docstatus": 1,
				}
			],
		)
		error = self.tool_error(
			"create_housing_unit",
			{"parcel": "Mill Creek", "unit_name": "X", "related_asset": "ACC-ASS-9001"},
		)
		self.assertIn("same books", error)

	def test_an_asset_already_carrying_another_unit_is_refused(self):
		from .fixtures import ASSET_CATEGORY

		self.a_parcel()
		STORE.seed(
			"Asset",
			[
				{
					"name": "ACC-ASS-9002",
					"asset_name": "Cabin",
					"company": MAIN,
					"asset_category": ASSET_CATEGORY,
					"gross_purchase_amount": 1000,
					"docstatus": 1,
				}
			],
		)
		self.a_unit("MC-Cabin-01", related_asset="ACC-ASS-9002")
		error = self.tool_error(
			"create_housing_unit",
			{"parcel": "Mill Creek", "unit_name": "MC-Cabin-02", "related_asset": "ACC-ASS-9002"},
		)
		self.assertIn("already carries", error)

	def test_an_unknown_unit_type_is_refused_with_the_list(self):
		self.a_parcel()
		error = self.tool_error(
			"create_housing_unit",
			{"parcel": "Mill Creek", "unit_name": "X", "unit_type": "Yurt"},
		)
		self.assertIn("Manufactured Home", error)

	def test_missing_required_arguments_are_refused_by_name(self):
		self.a_parcel()
		self.assertIn(
			"unit_name is required", self.tool_error("create_housing_unit", {"parcel": "Mill Creek"})
		)
		self.assertIn("parcel is required", self.tool_error("create_housing_unit", {"unit_name": "X"}))

	def test_the_switch_is_off_by_default(self):
		self.a_parcel()
		self.configure(enabled=1, allow_create_parcel=1)
		self.assertIn(
			"switched off",
			self.tool_error("create_housing_unit", {"parcel": "Mill Creek", "unit_name": "X"}),
		)

	def test_it_is_audited(self):
		self.a_camp()
		self.assertAudited("create_housing_unit", "Success")


# ── update_housing_unit ─────────────────────────────────────────────────────
class UpdateHousingUnit(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_camp()

	def test_it_changes_the_condition(self):
		data = self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "condition": "Needs Repair"})
		self.assertEqual(data["changed"]["condition"], ["Fair", "Needs Repair"])

	def test_inspection_and_detector_dates_move_only_through_their_own_records(self):
		"""v0.337.0. A date on the register always has an inspection or a detector test behind it."""
		for key, record in (
			("last_habitability_inspection", "create_housing_inspection"),
			("smoke_detector_last_test", "create_detector_test"),
			("co_detector_last_test", "create_detector_test"),
		):
			with self.subTest(field=key):
				error = self.tool_error("update_housing_unit", {"unit": "MC-Cabin-01", key: "2026-06-15"})
				self.assertIn(record, error)
				self.assertIn("Nothing was changed", error)

	def test_changing_the_square_footage_recomputes_a_computed_limit(self):
		data = self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "square_footage": 800})
		self.assertEqual(data["max_occupants_per_or_law"], 16)

	def test_changing_the_square_footage_keeps_a_limit_somebody_typed(self):
		"""A considered answer must not be quietly replaced by an arithmetic one."""
		self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "max_occupants_per_or_law": 4})
		data = self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "square_footage": 800})
		self.assertEqual(data["max_occupants_per_or_law"], 4)

	def test_renaming_a_unit_is_refused_because_assignments_point_at_the_docname(self):
		error = self.tool_error("update_housing_unit", {"unit": "MC-Cabin-01", "unit_name": "New"})
		self.assertIn("docname", error)
		self.assertIn("assignment", error)

	def test_moving_a_unit_between_parcels_is_refused_even_for_a_manufactured_home(self):
		self.a_parcel("Green Camp", acreage=19.87)
		error = self.tool_error("update_housing_unit", {"unit": "MC-Cabin-01", "parcel": "Green Camp"})
		self.assertIn("manufactured homes are", error)
		self.assertIn("assignment history", error)

	def test_a_call_that_changes_nothing_is_refused_with_the_options(self):
		error = self.tool_error("update_housing_unit", {"unit": "MC-Cabin-01"})
		self.assertIn("nothing to change", error)
		self.assertIn("new_unit_name", error)
		self.assertNotIn("co_detector_last_test", error, "v0.337.0: refused here, so not offered")

	def test_an_unknown_unit_is_refused(self):
		self.assertIn(
			"no Housing Unit called",
			self.tool_error("update_housing_unit", {"unit": "Nowhere", "capacity": 1}),
		)


# ── create_housing_assignment ───────────────────────────────────────────────
class CreateHousingAssignment(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_camp()

	def test_it_assigns_somebody_to_a_unit(self):
		data = self.an_assignment()
		self.assertEqual(data["unit"], "MC-Cabin-01 - MC")
		self.assertEqual(data["employee_name"], "Antony")
		self.assertEqual(data["assigned_date"], "2026-06-01")
		self.assertTrue(data["current"])

	def test_the_name_carries_the_month_so_intake_sorts_into_seasons(self):
		self.assertEqual(self.an_assignment()["name"], "HA-2026-06-00001")

	def test_the_sequence_runs_within_the_month(self):
		self.a_unit("MC-Cabin-02")
		self.an_assignment("MC-Cabin-01", "Antony")
		self.assertEqual(self.an_assignment("MC-Cabin-02", "Alex")["name"], "HA-2026-06-00002")

	def test_a_different_month_starts_its_own_sequence(self):
		self.a_unit("MC-Cabin-02")
		self.an_assignment("MC-Cabin-01", "Antony", assigned_date="2026-06-01")
		self.assertEqual(
			self.an_assignment("MC-Cabin-02", "Alex", assigned_date="2026-07-01")["name"],
			"HA-2026-07-00001",
		)

	def test_a_blank_end_date_means_currently_assigned(self):
		data = self.an_assignment()
		self.assertIsNone(data["end_date"])
		self.assertEqual(data["status"], "Current")

	def test_an_end_date_given_up_front_makes_it_ended(self):
		data = self.an_assignment(end_date="2026-07-15")
		self.assertFalse(data["current"])
		self.assertEqual(data["status"], "Ended")

	def test_the_parcel_is_carried_from_the_unit(self):
		self.assertEqual(self.an_assignment()["parcel"], "Mill Creek - ETC")

	def test_it_records_the_deposit(self):
		data = self.an_assignment(deposit_paid=250)
		self.assertEqual(data["deposit_paid"], 250.0)
		self.assertEqual(data["deposit_outstanding"], 250.0)

	def test_a_deposit_returned_larger_than_the_one_paid_is_refused(self):
		error = self.tool_error(
			"create_housing_assignment",
			{
				"unit": "MC-Cabin-01",
				"employee": "Antony",
				"assigned_date": "2026-06-01",
				"deposit_paid": 100,
				"deposit_returned": 250,
			},
		)
		self.assertIn("refund of money nobody took", error)

	def test_the_wage_deduction_answer_is_recorded(self):
		data = self.an_assignment(housing_deduction_from_wages="Yes")
		self.assertEqual(data["housing_deduction_from_wages"], "Yes")

	def test_an_unrecorded_wage_deduction_is_warned_about_with_the_statute(self):
		data = self.an_assignment()
		joined = " ".join(data["warnings"])
		self.assertIn("ORS 653", joined)
		self.assertIn("cannot be defended", joined)

	def test_the_section_119_purpose_is_stated_on_the_record(self):
		data = self.an_assignment()
		self.assertIn("Section 119", data["section_119_note"])
		self.assertIn("does not make the determination", data["section_119_note"])

	def test_an_assignment_with_nobody_in_it_is_refused(self):
		error = self.tool_error(
			"create_housing_assignment", {"unit": "MC-Cabin-01", "assigned_date": "2026-06-01"}
		)
		self.assertIn("employee or employee_name is required", error)

	def test_a_missing_assigned_date_is_refused(self):
		self.assertIn(
			"assigned_date is required",
			self.tool_error("create_housing_assignment", {"unit": "MC-Cabin-01", "employee": "Antony"}),
		)

	def test_the_switch_is_off_by_default(self):
		self.configure(enabled=1, allow_create_parcel=1, allow_create_housing_unit=1)
		self.assertIn(
			"switched off",
			self.tool_error(
				"create_housing_assignment",
				{"unit": "MC-Cabin-01", "employee": "A", "assigned_date": "2026-06-01"},
			),
		)


class TheHousingDeductionIsTheEntitysAnswerAndNotTheForemans(HousingTestCase):
	"""v0.94.0, decision 2. A housing deduction is a WAGE deduction.

	It was a three-way per-assignment Select (`Yes` / `No` / `Unknown`) that a
	foreman answered on every bunk — and this farm charges no rent for labor camp
	housing at all, so it was `No` every time and `Unknown` wherever somebody
	skipped it. `Unknown` is not a neutral value here: ORS 653 / OAR 839-015
	require the deduction to be DISCLOSED, and the Housing Assignment row is that
	disclosure, so a column full of `Unknown` is a disclosure nobody made.

	WHAT THE STANDALONE DOUBLE CAN AND CANNOT PROVE. It stores documents whole and
	hands back what it was given, so these tests prove the resolution order and
	that the value is PERSISTED on the row — which is the property that matters.
	They do not prove Frappe's own Select validation would accept the value on the
	bench; `as_choice` against the doctype's options is what does that, and it runs
	on both.
	"""

	def setUp(self):
		super().setUp()
		self.a_camp()

	def _company_default(self, value):
		"""Install the Custom Field the way a migrate would, then answer it.

		BOTH HALVES MATTER. `compat.has_field` reads this site's meta, so setting
		a value on a column the site does not have would leave the resolver
		correctly returning "" and the test passing for the wrong reason — the
		negative-control test below is the one that proves this setUp is doing
		something rather than nothing.
		"""
		self.configure(**{**ALL_ON, "enabled": 1, "allow_install_compliance_fields": 1})
		compliance_fields.install_compliance_fields()
		self.assertTrue(
			compat.has_field("Company", "default_housing_deduction_from_wages"),
			"the Company custom field did not install, so this test would prove nothing",
		)
		frappe.db.set_value("Company", MAIN, "default_housing_deduction_from_wages", value)

	def test_the_entitys_default_is_written_onto_the_row(self):
		"""PF-3, AND IT IS THE WHOLE POINT OF THE CHANGE. `audit_packets` and the
		camp register read the per-assignment COLUMN. Resolving the default when a
		report runs would leave every assignment made after this release reporting
		'Unknown' to an auditor while looking right in the app — so the assertion
		is against the stored row, not against what the tool returned."""
		self._company_default("No")
		created = self.an_assignment()
		self.assertEqual(
			frappe.db.get_value("Housing Assignment", created["name"], "housing_deduction_from_wages"),
			"No",
		)

	def test_an_explicit_answer_still_wins(self):
		"""The arrangement can differ from the entity's norm, which is what the
		Housing Unit doctype's own help text is right about. This supplies an
		answer where the caller sent none; it never overrides one."""
		self._company_default("No")
		created = self.an_assignment(housing_deduction_from_wages="Yes")
		self.assertEqual(
			frappe.db.get_value("Housing Assignment", created["name"], "housing_deduction_from_wages"),
			"Yes",
		)

	def test_a_company_with_no_default_set_behaves_exactly_as_before(self):
		"""THE NEGATIVE CONTROL, and the one that proves this is a default rather
		than a rewrite. A site mid-upgrade, or an entity nobody has answered for,
		falls through to what happened before this existed — the column is left
		unset and the register reports it as Unknown."""
		created = self.an_assignment()
		self.assertEqual(
			frappe.db.get_value("Housing Assignment", created["name"], "housing_deduction_from_wages"),
			"Unknown",
		)
		listed = self.tool_data("list_housing_assignments", {"company": MAIN})
		row = next(entry for entry in listed["assignments"] if entry["name"] == created["name"])
		self.assertEqual(row["housing_deduction_from_wages"], "Unknown")

	def test_the_register_reads_the_stored_answer_back(self):
		"""The read path the compliance packet uses, end to end: a default set
		once on the entity reaches the report as a real disclosure."""
		self._company_default("Yes")
		created = self.an_assignment()
		listed = self.tool_data("list_housing_assignments", {"company": MAIN})
		row = next(entry for entry in listed["assignments"] if entry["name"] == created["name"])
		self.assertEqual(row["housing_deduction_from_wages"], "Yes")
		self.assertEqual(listed["with_wage_deduction"], [created["name"]])


class AssignmentRefusals(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_camp()

	def test_an_overlapping_assignment_is_refused_and_names_the_one_already_there(self):
		self.an_assignment(employee="Antony")
		error = self.tool_error(
			"create_housing_assignment",
			{"unit": "MC-Cabin-01", "employee": "Alex", "assigned_date": "2026-06-15"},
		)
		self.assertIn("HA-2026-06-00001", error)
		self.assertIn("Antony", error)
		self.assertIn("allow_multi_occupancy=true", error)

	def test_multi_occupancy_lets_the_bunk_room_case_through(self):
		self.an_assignment(employee="Antony")
		data = self.tool_data(
			"create_housing_assignment",
			{
				"unit": "MC-Cabin-01",
				"employee": "Alex",
				"assigned_date": "2026-06-15",
				"allow_multi_occupancy": True,
			},
		)
		self.assertTrue(data["multi_occupancy"])
		self.assertEqual(data["occupants_after"], 2)

	def test_a_shared_unit_says_how_many_others_overlap(self):
		self.an_assignment(employee="Antony")
		data = self.tool_data(
			"create_housing_assignment",
			{
				"unit": "MC-Cabin-01",
				"employee": "Alex",
				"assigned_date": "2026-06-15",
				"allow_multi_occupancy": True,
			},
		)
		self.assertTrue(any("Shared occupancy accepted" in w for w in data["warnings"]))

	def test_going_past_the_recorded_capacity_is_warned_about(self):
		self.a_unit("MC-Cabin-02", capacity=1)
		self.an_assignment("MC-Cabin-02", "Antony")
		data = self.tool_data(
			"create_housing_assignment",
			{
				"unit": "MC-Cabin-02",
				"employee": "Alex",
				"assigned_date": "2026-06-15",
				"allow_multi_occupancy": True,
			},
		)
		self.assertTrue(any("recorded capacity of 1" in w for w in data["warnings"]))

	def test_an_assignment_after_the_previous_one_ended_is_not_an_overlap(self):
		self.an_assignment(employee="Antony", end_date="2026-07-15")
		data = self.an_assignment(employee="Alex", assigned_date="2026-07-16")
		self.assertFalse(data["multi_occupancy"])

	def test_moving_out_and_moving_in_on_the_same_day_IS_an_overlap(self):
		"""They shared the cabin that night. A camp manager told otherwise puts
		two people in one bed."""
		self.an_assignment(employee="Antony", end_date="2026-07-15")
		error = self.tool_error(
			"create_housing_assignment",
			{"unit": "MC-Cabin-01", "employee": "Alex", "assigned_date": "2026-07-15"},
		)
		self.assertIn("Nothing was created", error)

	def test_an_assignment_into_a_shower_block_is_refused(self):
		self.a_unit("GC-BathHouse", unit_type="Toilet-Shower", capacity=0)
		error = self.tool_error(
			"create_housing_assignment",
			{"unit": "GC-BathHouse", "employee": "Antony", "assigned_date": "2026-06-01"},
		)
		self.assertIn("shower block", error)
		self.assertIn("its type is wrong", error)

	def test_an_assignment_into_a_shop_is_refused_too(self):
		self.a_unit("MC-Shop", unit_type="Shop")
		self.assertIn(
			"Nothing was created",
			self.tool_error(
				"create_housing_assignment",
				{"unit": "MC-Shop", "employee": "A", "assigned_date": "2026-06-01"},
			),
		)

	def test_an_assignment_into_an_uninhabitable_unit_is_refused(self):
		self.a_unit("MC-Cabin-99", condition="Uninhabitable")
		error = self.tool_error(
			"create_housing_assignment",
			{"unit": "MC-Cabin-99", "employee": "Antony", "assigned_date": "2026-06-01"},
		)
		self.assertIn("Uninhabitable", error)
		self.assertIn("repaired and inspected", error)

	def test_an_end_date_before_the_start_is_refused(self):
		error = self.tool_error(
			"create_housing_assignment",
			{
				"unit": "MC-Cabin-01",
				"employee": "Antony",
				"assigned_date": "2026-06-01",
				"end_date": "2026-05-01",
			},
		)
		self.assertIn("Nobody moved out before they moved in", error)
		self.assertIn("Nothing was created", error)
		self.assertEqual(STORE.rows("Housing Assignment"), [])

	def test_nothing_is_written_when_an_assignment_is_refused(self):
		self.an_assignment(employee="Antony")
		self.tool_error(
			"create_housing_assignment",
			{"unit": "MC-Cabin-01", "employee": "Alex", "assigned_date": "2026-06-15"},
		)
		self.assertEqual(len(STORE.rows("Housing Assignment")), 1)


class AssignmentWithoutAnHRApp(HousingTestCase):
	def test_the_employee_is_stored_as_text_and_the_tool_says_so(self):
		"""A camp roster that cannot be written until an HR module is installed
		is a camp roster nobody keeps."""
		self.a_camp()
		data = self.an_assignment(employee="Antony Sedge")
		self.assertEqual(data["employee"], "Antony Sedge")
		self.assertTrue(any("No HR app" in warning for warning in data["warnings"]))

	def test_a_name_alone_is_enough(self):
		self.a_camp()
		data = self.tool_data(
			"create_housing_assignment",
			{"unit": "MC-Cabin-01", "employee_name": "Antony Sedge", "assigned_date": "2026-06-01"},
		)
		self.assertEqual(data["employee_name"], "Antony Sedge")


class AssignmentWithFrappeHR(HousingTestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		self.a_camp()

	def employee(self):
		return STORE.rows("Employee")[0]

	def test_a_known_employee_is_accepted_and_their_name_filled_in(self):
		row = self.employee()
		data = self.an_assignment(employee=row["name"])
		self.assertEqual(data["employee"], row["name"])
		self.assertEqual(data["employee_name"], row["employee_name"])

	def test_an_employee_named_by_their_own_name_resolves_to_their_id(self):
		row = self.employee()
		self.assertEqual(self.an_assignment(employee=row["employee_name"])["employee"], row["name"])

	def test_an_unknown_employee_is_refused_with_the_fix(self):
		error = self.tool_error(
			"create_housing_assignment",
			{"unit": "MC-Cabin-01", "employee": "Nobody At All", "assigned_date": "2026-06-01"},
		)
		self.assertIn("drifted", error)
		self.assertIn("Create the Employee first", error)

	def test_no_text_only_warning_is_produced_when_the_id_was_checked(self):
		data = self.an_assignment(employee=self.employee()["name"])
		self.assertFalse(any("No HR app" in warning for warning in data["warnings"]))


# ── end_housing_assignment ──────────────────────────────────────────────────
class EndHousingAssignment(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_camp()
		self.assignment = self.an_assignment(deposit_paid=250)["name"]

	def test_it_writes_the_end_date(self):
		data = self.tool_data(
			"end_housing_assignment", {"assignment": self.assignment, "end_date": "2026-07-15"}
		)
		self.assertEqual(data["end_date"], "2026-07-15")
		self.assertEqual(data["status"], "Ended")
		self.assertFalse(data["current"])

	def test_it_never_deletes_the_record(self):
		"""'The record is the audit trail' is only true if the record is there."""
		before = len(STORE.rows("Housing Assignment"))
		self.tool_data("end_housing_assignment", {"assignment": self.assignment, "end_date": "2026-07-15"})
		self.assertEqual(len(STORE.rows("Housing Assignment")), before)
		self.assertTrue(STORE.get_raw("Housing Assignment", self.assignment))

	def test_it_records_the_deposit_returned(self):
		data = self.tool_data(
			"end_housing_assignment",
			{"assignment": self.assignment, "end_date": "2026-07-15", "deposit_returned": 250},
		)
		self.assertEqual(data["deposit_returned"], 250.0)
		self.assertEqual(data["deposit_outstanding"], 0.0)

	def test_a_deposit_still_held_is_reported_so_it_is_refunded_or_explained(self):
		data = self.tool_data(
			"end_housing_assignment", {"assignment": self.assignment, "end_date": "2026-07-15"}
		)
		self.assertTrue(any("still held" in warning for warning in data["warnings"]))

	def test_returning_more_than_was_paid_is_refused(self):
		error = self.tool_error(
			"end_housing_assignment",
			{"assignment": self.assignment, "end_date": "2026-07-15", "deposit_returned": 400},
		)
		self.assertIn("refund of money nobody took", error)

	def test_ending_an_assignment_that_already_ended_is_refused(self):
		self.tool_data("end_housing_assignment", {"assignment": self.assignment, "end_date": "2026-07-15"})
		error = self.tool_error(
			"end_housing_assignment", {"assignment": self.assignment, "end_date": "2026-08-01"}
		)
		self.assertIn("already ended on 2026-07-15", error)
		self.assertIn("correction", error)

	def test_an_end_date_before_the_start_is_refused(self):
		error = self.tool_error(
			"end_housing_assignment", {"assignment": self.assignment, "end_date": "2026-05-01"}
		)
		self.assertIn("before 2026-06-01", error)

	def test_dry_run_writes_nothing(self):
		data = self.tool_data(
			"end_housing_assignment",
			{"assignment": self.assignment, "end_date": "2026-07-15", "dry_run": True},
		)
		self.assertTrue(data["dry_run"])
		self.assertFalse(data["changed"])
		self.assertFalse(STORE.get_raw("Housing Assignment", self.assignment).get("end_date"))

	def test_notes_are_appended_rather_than_replacing_what_is_there(self):
		self.tool_data(
			"end_housing_assignment",
			{"assignment": self.assignment, "end_date": "2026-07-15", "notes": "Left for the season"},
		)
		self.assertIn("Left for the season", STORE.get_raw("Housing Assignment", self.assignment)["notes"])

	def test_an_unknown_assignment_is_refused_with_the_register_named(self):
		self.assertIn(
			"list_housing_assignments",
			self.tool_error("end_housing_assignment", {"assignment": "HA-X", "end_date": "2026-07-15"}),
		)

	def test_the_unit_is_free_again_afterwards(self):
		self.tool_data("end_housing_assignment", {"assignment": self.assignment, "end_date": "2026-07-15"})
		data = self.an_assignment(employee="Alex", assigned_date="2026-07-16")
		self.assertFalse(data["multi_occupancy"])


# ── the registers ───────────────────────────────────────────────────────────
class ListHousingUnits(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_unit("MC-Cabin-01", capacity=4, square_footage=384)
		self.a_unit("MC-Cabin-02", capacity=4, square_footage=384)
		self.a_unit("MC-BathHouse", unit_type="Toilet-Shower", capacity=0, square_footage=200)
		self.an_assignment("MC-Cabin-01", "Antony")

	def test_it_totals_capacity_and_occupancy(self):
		data = self.tool_data("list_housing_units")
		self.assertEqual(data["unit_count"], 3)
		self.assertEqual(data["residential_unit_count"], 2)
		self.assertEqual(data["total_capacity"], 8)
		self.assertEqual(data["currently_assigned"], 1)
		self.assertEqual(data["open_beds"], 7)

	def test_a_shower_block_is_counted_as_a_unit_but_not_as_capacity(self):
		"""Adding its zero capacity to the total would make the register look
		thinner than it is."""
		data = self.tool_data("list_housing_units")
		self.assertEqual(data["by_unit_type"]["Toilet-Shower"], 1)
		self.assertEqual(data["total_capacity"], 8)

	def test_it_names_the_occupants(self):
		unit = next(
			row for row in self.tool_data("list_housing_units")["units"] if row["unit_name"] == "MC-Cabin-01"
		)
		self.assertEqual(unit["occupants"], ["Antony"])

	def test_it_lists_the_overdue_inspections(self):
		"""Never inspected counts as overdue, which is the answer that gets
		somebody to go and look."""
		data = self.tool_data("list_housing_units")
		self.assertEqual(len(data["overdue_inspections"]), 2)

	def test_a_recent_inspection_takes_a_unit_off_that_list(self):
		# v0.337.0. The date moves only through a Housing Inspection; the fixture sets the column directly.
		frappe.db.set_value("Housing Unit", "MC-Cabin-01 - MC", "last_habitability_inspection", "2026-06-15")
		STORE.commit()
		self.assertNotIn("MC-Cabin-01 - MC", self.tool_data("list_housing_units")["overdue_inspections"])

	def test_an_inspection_older_than_a_year_is_overdue_again(self):
		# v0.337.0. The date moves only through a Housing Inspection; the fixture sets the column directly.
		frappe.db.set_value("Housing Unit", "MC-Cabin-01 - MC", "last_habitability_inspection", "2024-01-01")
		STORE.commit()
		self.assertIn("MC-Cabin-01 - MC", self.tool_data("list_housing_units")["overdue_inspections"])

	def test_it_names_the_uninhabitable_units(self):
		self.tool_data("update_housing_unit", {"unit": "MC-Cabin-02", "condition": "Uninhabitable"})
		self.assertEqual(self.tool_data("list_housing_units")["uninhabitable"], ["MC-Cabin-02 - MC"])

	def test_it_filters_by_unit_type(self):
		data = self.tool_data("list_housing_units", {"unit_type": "Cabin"})
		self.assertEqual(data["unit_count"], 2)

	def test_it_scopes_to_one_company(self):
		self.a_parcel("Far Camp", acreage=10, company=OTHER)
		self.a_unit("FC-Cabin-01", "Far Camp")
		names = [row["unit_name"] for row in self.tool_data("list_housing_units", {"company": MAIN})["units"]]
		self.assertNotIn("FC-Cabin-01", names)

	def test_it_is_read_only(self):
		before = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		self.tool_data("list_housing_units")
		after = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		before.pop("MCP Action Log", None)
		after.pop("MCP Action Log", None)
		self.assertEqual(before, after)


class GetHousingUnit(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_camp()

	def test_it_returns_the_whole_assignment_history_not_just_the_current_one(self):
		first = self.an_assignment(employee="Antony")["name"]
		self.tool_data("end_housing_assignment", {"assignment": first, "end_date": "2026-07-15"})
		self.an_assignment(employee="Alex", assigned_date="2026-07-16")
		data = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})
		self.assertEqual(data["assignment_count"], 2)
		self.assertEqual(len(data["current_assignments"]), 1)

	def test_it_reports_the_open_beds(self):
		self.an_assignment()
		self.assertEqual(self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["open_beds"], 3)

	def test_it_names_the_compliance_gaps_in_sentences(self):
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		joined = " ".join(notes)
		self.assertIn("has ever been recorded", joined)
		self.assertIn("smoke detector", joined)
		self.assertIn("propane heater", joined)

	def test_an_uninhabitable_unit_says_assignments_are_refused(self):
		self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "condition": "Uninhabitable"})
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		self.assertTrue(any("refused" in note for note in notes))

	def test_an_fsma_unit_says_what_that_obliges(self):
		self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "fsma_worker_facility": True})
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		self.assertTrue(any("Subpart L" in note for note in notes))

	def test_an_unknown_unit_is_refused_with_the_register_named(self):
		self.assertIn("list_housing_units", self.tool_error("get_housing_unit", {"unit": "Nowhere"}))


class ListHousingAssignments(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_unit("MC-Cabin-01")
		self.a_unit("MC-Cabin-02")
		self.ended = self.an_assignment("MC-Cabin-01", "Antony")["name"]
		self.tool_data("end_housing_assignment", {"assignment": self.ended, "end_date": "2026-07-15"})
		self.an_assignment("MC-Cabin-02", "Alex", housing_deduction_from_wages="Yes", deposit_paid=100)

	def test_it_defaults_to_current_assignments_only(self):
		data = self.tool_data("list_housing_assignments")
		self.assertEqual(data["assignment_count"], 1)
		self.assertTrue(data["current_only"])

	def test_current_only_false_gives_the_history(self):
		data = self.tool_data("list_housing_assignments", {"current_only": False})
		self.assertEqual(data["assignment_count"], 2)

	def test_it_names_the_assignments_with_a_wage_deduction(self):
		"""ORS 653 constrains the deduction, and this is where the ones that
		took it are named."""
		data = self.tool_data("list_housing_assignments")
		self.assertEqual(len(data["with_wage_deduction"]), 1)

	def test_it_totals_the_deposits_still_held(self):
		self.assertEqual(self.tool_data("list_housing_assignments")["deposits_outstanding"], 100.0)

	def test_it_filters_by_unit(self):
		data = self.tool_data("list_housing_assignments", {"unit": "MC-Cabin-01", "current_only": False})
		self.assertEqual(data["assignment_count"], 1)

	def test_it_filters_by_employee(self):
		data = self.tool_data("list_housing_assignments", {"employee": "Alex", "current_only": False})
		self.assertEqual(data["assignment_count"], 1)


class GetHousingCapacity(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel("Mill Creek", acreage=131.43)
		self.a_parcel("Green Camp", acreage=19.87)
		for index in range(1, 4):
			self.a_unit(f"MC-Cabin-0{index}", "Mill Creek", capacity=4, square_footage=384)
		self.a_unit("MC-BathHouse", "Mill Creek", unit_type="Bath House", capacity=0)
		self.a_unit("GC-Cabin-01", "Green Camp", capacity=2, square_footage=200)
		self.an_assignment("MC-Cabin-01", "Antony")

	def test_it_reports_the_totals(self):
		data = self.tool_data("get_housing_capacity")
		self.assertEqual(data["unit_count"], 5)
		self.assertEqual(data["total_capacity"], 14)
		self.assertEqual(data["currently_assigned"], 1)
		self.assertEqual(data["open_beds"], 13)

	def test_it_breaks_down_by_parcel(self):
		buckets = {row["parcel"]: row for row in self.tool_data("get_housing_capacity")["by_parcel"]}
		self.assertEqual(buckets["Mill Creek - ETC"]["capacity"], 12)
		self.assertEqual(buckets["Mill Creek - ETC"]["residential_units"], 3)
		self.assertEqual(buckets["Green Camp - ETC"]["capacity"], 2)

	def test_it_reports_the_lawful_capacity_beside_the_used_one(self):
		buckets = {row["parcel"]: row for row in self.tool_data("get_housing_capacity")["by_parcel"]}
		self.assertEqual(buckets["Mill Creek - ETC"]["lawful_capacity"], 21)

	def test_it_counts_the_overdue_inspections(self):
		self.assertEqual(self.tool_data("get_housing_capacity")["overdue_inspection_count"], 4)

	def test_the_readout_reads_like_the_spec_asked_for(self):
		lines = self.tool_data("get_housing_capacity")["readout"]
		line = next(entry for entry in lines if entry.startswith("Mill Creek"))
		self.assertIn("capacity 12", line)
		self.assertIn("currently 1 assigned", line)
		self.assertIn("11 open", line)
		self.assertIn("Overdue habitability inspections: 3", line)

	def test_it_can_be_scoped_to_one_parcel(self):
		data = self.tool_data("get_housing_capacity", {"parcel": "Green Camp"})
		self.assertEqual(data["parcel_count"], 1)
		self.assertEqual(data["total_capacity"], 2)


class GetEmployeeHousingHistory(HousingTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_unit("MC-Cabin-12")
		self.a_unit("MC-Cabin-13")
		self.first = self.an_assignment("MC-Cabin-12", "Antony", deposit_paid=200)["name"]

	def test_it_reads_the_way_the_spec_asked_for(self):
		self.tool_data("end_housing_assignment", {"assignment": self.first, "end_date": "2026-07-15"})
		lines = self.tool_data("get_employee_housing_history", {"employee": "Antony"})["readout"]
		self.assertIn("Antony assigned MC-Cabin-12 - MC 2026-06-01 → 2026-07-15", lines)
		self.assertIn("Antony is currently unassigned.", lines)

	def test_a_current_assignment_reads_as_present(self):
		lines = self.tool_data("get_employee_housing_history", {"employee": "Antony"})["readout"]
		self.assertTrue(any("→ present" in line for line in lines))

	def test_it_lists_every_unit_they_have_lived_in(self):
		self.tool_data("end_housing_assignment", {"assignment": self.first, "end_date": "2026-07-15"})
		self.an_assignment("MC-Cabin-13", "Antony", assigned_date="2026-07-16")
		data = self.tool_data("get_employee_housing_history", {"employee": "Antony"})
		self.assertEqual(data["units_lived_in"], ["MC-Cabin-12 - MC", "MC-Cabin-13 - MC"])
		self.assertEqual(data["assignment_count"], 2)

	def test_it_totals_the_deposits(self):
		data = self.tool_data("get_employee_housing_history", {"employee": "Antony"})
		self.assertEqual(data["deposits_paid"], 200.0)
		self.assertEqual(data["deposits_outstanding"], 200.0)

	def test_it_names_the_assignments_that_took_a_wage_deduction(self):
		self.an_assignment("MC-Cabin-13", "Antony", housing_deduction_from_wages="Yes")
		data = self.tool_data("get_employee_housing_history", {"employee": "Antony"})
		self.assertEqual(len(data["wage_deduction_taken"]), 1)

	def test_it_matches_on_the_name_when_there_is_no_employee_id(self):
		self.tool_data(
			"create_housing_assignment",
			{"unit": "MC-Cabin-13", "employee_name": "Alex Bramwell", "assigned_date": "2026-06-01"},
		)
		data = self.tool_data("get_employee_housing_history", {"employee": "Alex Bramwell"})
		self.assertEqual(data["assignment_count"], 1)

	def test_somebody_never_housed_is_refused_with_the_register_named(self):
		error = self.tool_error("get_employee_housing_history", {"employee": "Nobody"})
		self.assertIn("list_housing_assignments", error)


# ── where the building actually stands ──────────────────────────────────────
class TheUnitHasAPosition(HousingTestCase):
	"""v0.32.0. A camp address is a driveway off a county road and a cabin number
	is paint on a door; neither of them puts an ambulance, an inspector or a crew
	bus at the right building.

	THE PAIR MOVES TOGETHER OR NOT AT ALL, and that is the rule worth testing. A
	unit carrying a corrected longitude beside a stale latitude sits somewhere
	neither reading of the record meant, and it looks exactly as valid as a
	correct one.
	"""

	def setUp(self):
		super().setUp()
		self.a_parcel()

	def test_a_unit_can_be_registered_with_its_coordinates(self):
		data = self.a_unit(gps_latitude=45.6015, gps_longitude=-121.178)
		self.assertEqual(data["gps"], {"lat": 45.6015, "lon": -121.178})

	def test_a_unit_with_no_coordinates_reports_none_rather_than_null_island(self):
		"""[0, 0] is a real place in the Gulf of Guinea and it is what an unset
		Float pair looks like. A map that flies there looks exactly like a map
		showing you where something is."""
		self.assertIsNone(self.a_unit()["gps"])

	def test_the_position_can_be_set_afterwards(self):
		self.a_unit()
		data = self.tool_data(
			"update_housing_unit",
			{"unit": "MC-Cabin-01", "gps_latitude": 45.6015, "gps_longitude": -121.178},
		)
		self.assertEqual(data["gps"], {"lat": 45.6015, "lon": -121.178})
		self.assertEqual(sorted(data["changed"]), ["gps_latitude", "gps_longitude"])

	def test_correcting_one_half_keeps_the_other(self):
		"""The carry that makes a half-call safe rather than refused: passing a new
		latitude on its own is checked against the STORED longitude."""
		self.a_unit(gps_latitude=45.6015, gps_longitude=-121.178)
		data = self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "gps_latitude": 45.6020})
		self.assertEqual(data["gps"], {"lat": 45.602, "lon": -121.178})
		self.assertEqual(list(data["changed"]), ["gps_latitude"])

	def test_half_a_coordinate_on_an_unlocated_unit_is_refused(self):
		"""A unit with a latitude and no longitude sits on a map off the coast of
		Ghana, which is a position and not the one anybody meant."""
		self.a_unit()
		message = self.tool_error("update_housing_unit", {"unit": "MC-Cabin-01", "gps_latitude": 45.6015})
		self.assertIn("have to be set together", message)
		self.assertIn("Nothing was changed", message)

	def test_half_a_coordinate_at_creation_is_refused(self):
		message = self.tool_error(
			"create_housing_unit",
			{"parcel": "Mill Creek", "unit_name": "MC-Cabin-09", "gps_longitude": -121.178},
		)
		self.assertIn("have to be set together", message)

	def test_a_latitude_past_ninety_is_the_pair_the_wrong_way_round(self):
		message = self.tool_error(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": "MC-Cabin-09",
				"gps_latitude": -121.178,
				"gps_longitude": 45.6015,
			},
		)
		self.assertIn("not a point on Earth", message)
		self.assertIn("wrong way round", message)

	def test_a_longitude_off_the_planet_is_refused(self):
		self.a_unit()
		self.assertIn(
			"not a point on Earth",
			self.tool_error(
				"update_housing_unit",
				{"unit": "MC-Cabin-01", "gps_latitude": 45.6, "gps_longitude": 361},
			),
		)

	def test_the_position_can_be_cleared_by_passing_both_empty(self):
		self.a_unit(gps_latitude=45.6015, gps_longitude=-121.178)
		data = self.tool_data(
			"update_housing_unit",
			{"unit": "MC-Cabin-01", "gps_latitude": "", "gps_longitude": ""},
		)
		self.assertIsNone(data["gps"])

	def test_it_comes_back_from_the_single_read_and_the_register(self):
		self.a_unit(gps_latitude=45.6015, gps_longitude=-121.178)
		single = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})
		self.assertEqual(single["gps"], {"lat": 45.6015, "lon": -121.178})
		listed = self.tool_data("list_housing_units", {"owning_entity": MAIN})["units"][0]
		self.assertEqual(listed["gps"], {"lat": 45.6015, "lon": -121.178})

	def test_the_position_is_not_by_itself_a_reason_to_have_called_update(self):
		"""`nothing to change` still fires for a call that passes no argument at
		all — the new fields are added to the sentence rather than to the set of
		things that count as a no-op."""
		self.a_unit()
		message = self.tool_error("update_housing_unit", {"unit": "MC-Cabin-01"})
		self.assertIn("gps_latitude", message)
		self.assertIn("nothing to change", message)


# ── compliance is woven, not a shadow layer ─────────────────────────────────
class WovenNotShadow(HousingTestCase):
	"""The same test as in test_farm: remove a compliance field and show the
	SAME removal breaks an operational answer AND a regulatory one.

	If only the regulatory half broke, the field would belong in a separate
	compliance register and this design would be wrong.
	"""

	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_unit(
			"MC-Cabin-01",
			capacity=4,
			square_footage=384,
			condition="Good",
			fsma_worker_facility=True,
			last_habitability_inspection="2026-06-15",
			smoke_detector_last_test="2026-06-15",
			co_detector_last_test="2026-06-15",
		)

	def test_the_condition_gates_dispatch_and_a_habitability_finding(self):
		# OPERATIONAL: somebody can be housed here.
		self.an_assignment("MC-Cabin-01", "Antony")
		STORE.tables["Housing Unit"]["MC-Cabin-01 - MC"]["condition"] = "Uninhabitable"
		# OPERATIONAL: the next assignment is refused outright.
		self.assertIn(
			"Uninhabitable",
			self.tool_error(
				"create_housing_assignment",
				{
					"unit": "MC-Cabin-01",
					"employee": "Alex",
					"assigned_date": "2026-08-01",
					"allow_multi_occupancy": True,
				},
			),
		)
		# REGULATORY: the unit appears on the register's uninhabitable list.
		self.assertEqual(self.tool_data("list_housing_units")["uninhabitable"], ["MC-Cabin-01 - MC"])

	def test_the_habitability_inspection_gates_a_walk_and_an_audit_line(self):
		self.assertEqual(self.tool_data("get_housing_capacity")["overdue_inspection_count"], 0)
		STORE.tables["Housing Unit"]["MC-Cabin-01 - MC"]["last_habitability_inspection"] = None
		# REGULATORY: the count an inspector asks for goes up.
		self.assertEqual(self.tool_data("get_housing_capacity")["overdue_inspection_count"], 1)
		# OPERATIONAL: the unit's own detail says go and look at it.
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		self.assertTrue(any("has ever been recorded" in note for note in notes))

	def test_the_co_detector_date_is_a_life_safety_fact_before_it_is_a_record(self):
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		self.assertFalse(any("CO detector" in note for note in notes))
		STORE.tables["Housing Unit"]["MC-Cabin-01 - MC"]["co_detector_last_test"] = None
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		# Both halves at once: the sentence names the appliance (operational)
		# and the requirement (regulatory), because they are the same fact.
		self.assertTrue(any("propane heater" in note for note in notes))
		self.assertTrue(any("fuel-burning appliance" in note for note in notes))

	def test_the_lawful_occupancy_limits_a_bed_count_and_defends_a_filing(self):
		self.assertFalse(
			self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["capacity_over_lawful_occupancy"]
		)
		self.tool_data("update_housing_unit", {"unit": "MC-Cabin-01", "capacity": 12})
		data = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})
		# REGULATORY: the unit lands on the over-occupancy list.
		self.assertTrue(data["capacity_over_lawful_occupancy"])
		self.assertIn("MC-Cabin-01 - MC", self.tool_data("list_housing_units")["over_lawful_occupancy"])
		# OPERATIONAL: the same figure is the one a camp manager fills beds to.
		self.assertTrue(any("over-filled" in note for note in data["compliance_notes"]))

	def test_the_assignment_record_is_both_the_roster_and_the_section_119_evidence(self):
		"""One record, two uses. That is what woven means: delete it and the camp
		manager loses today's roster at the same moment the tax position loses
		its defence."""
		self.an_assignment("MC-Cabin-01", "Antony")
		# OPERATIONAL: who is in the cabin tonight.
		self.assertEqual(self.tool_data("list_housing_units")["units"][0]["occupants"], ["Antony"])
		# REGULATORY: the same row is what a Section 119 exclusion is defended
		# with, and it survives the person leaving.
		name = self.tool_data("list_housing_assignments")["assignments"][0]["name"]
		self.tool_data("end_housing_assignment", {"assignment": name, "end_date": "2026-07-15"})
		self.assertEqual(self.tool_data("list_housing_units")["units"][0]["occupants"], [])
		history = self.tool_data("get_employee_housing_history", {"employee": "Antony"})
		self.assertEqual(history["assignment_count"], 1)

	def test_the_fsma_flag_changes_what_the_other_facts_oblige(self):
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		self.assertTrue(any("Subpart L" in note for note in notes))
		STORE.tables["Housing Unit"]["MC-Cabin-01 - MC"]["fsma_worker_facility"] = 0
		self.assertEqual(self.tool_data("list_housing_units")["fsma_worker_facilities"], [])
		notes = self.tool_data("get_housing_unit", {"unit": "MC-Cabin-01"})["compliance_notes"]
		self.assertFalse(any("Subpart L" in note for note in notes))
