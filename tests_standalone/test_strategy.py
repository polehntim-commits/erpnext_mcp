# SPDX-License-Identifier: MIT
"""What the operation is trying to become, and whether it got there.

1. **PLANS ARE SUPERSEDED, NEVER EDITED INTO THE NEXT ONE.**
   `ThePlanIsSuperseded`. The interesting question about a strategy is what it
   USED to say. Naming a predecessor versions the new plan and retires the old
   one in one call, and the old wording is asserted to be untouched afterwards.

2. **A CIRCULAR CHAIN IS REFUSED.** `ThePlanIsSuperseded`. A loop makes 'what did
   we say before this' unanswerable, which is the question the chain exists for.

3. **AN UNDATED ACTUAL IS REFUSED.** `TheObjectiveMustBeMeasurable`. A figure with
   no date cannot be told from one taken eighteen months ago, so it is either
   believed and wrong or discounted and useless. Achieved-with-no-actual is
   refused for the matching reason.

4. **THE HIT RATE IS COMPUTED OVER SETTLED OBJECTIVES ONLY.**
   `TheHitRateIsHonest`. Counting objectives still in progress flatters every
   plan on the day it is written.

5. **OVERDUE MEANS PAST ITS DATE AND STILL OPEN.** `TheHitRateIsHonest`. A Failed
   objective past its date is settled, and counting it would keep it on the list
   for ever.
"""

from .fixtures import MAIN, V12TestCase, seed_masters
from .harness import frappe

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_strategic_plan",
		"get_strategic_plan",
		"list_strategic_plans",
		"update_strategic_plan",
		"create_strategic_objective",
		"get_strategic_objective",
		"list_strategic_objectives",
		"update_strategic_objective",
		"create_crop",
	)
}


class StrategyTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		self.configure(enabled=1, **ALL_ON)
		# The Crop register is empty in the double until something writes to it,
		# and a plan naming a crop nothing else knows about is refused on purpose.
		self.tool_data("create_crop", {"crop_name": "Sweet Cherry", "crop_type": "Stone Fruit"})

	def a_plan(self, **kw):
		payload = {
			"company": MAIN,
			"plan_name": "Cherry 2026-2031",
			"status": "Developed",
			"timeframe": "2026-2031",
			"vision": "The block that buyers ask for by name.",
			"effective_date": "2026-01-01",
		}
		payload.update(kw)
		return self.tool_data("create_strategic_plan", payload)

	def an_objective(self, plan, **kw):
		payload = {
			"company": MAIN,
			"strategic_plan": plan,
			"objective": "Lift packout to 78% on the Bing blocks.",
			"kpi_metric": "packout",
			"kpi_target": "78%",
			"due_date": "2026-12-31",
		}
		payload.update(kw)
		return self.tool_data("create_strategic_objective", payload)


class ThePlanRegister(StrategyTestCase):
	def test_a_plan_is_written_and_reports_what_is_still_empty(self):
		created = self.a_plan()
		self.assertEqual(created["version"], 1)
		self.assertIn("vision", created["sections_filled"])
		self.assertIn("exit_strategy", created["sections_empty"])
		self.assertIn("exit_strategy and", created["sections_note"])

	def test_completeness_is_reported_rather_than_hidden(self):
		"""A plan with a vision and nothing else is common and honest."""
		created = self.a_plan()
		self.assertLess(created["completeness"], 0.5)
		full = self.a_plan(
			plan_name="Complete Plan",
			mission="m",
			swot="s",
			porters_five_forces="p",
			grand_strategy="g",
			business_strategy="b",
			sustainable_advantage="a",
			command_structure="c",
			functional_tactics="f",
			validation_control="v",
			exit_strategy="e",
		)
		self.assertEqual(full["completeness"], 1.0)
		self.assertEqual(full["sections_empty"], [])

	def test_a_plan_can_name_a_crop_or_be_farm_wide(self):
		farm_wide = self.a_plan()
		self.assertIsNone(farm_wide["crop"])
		for_crop = self.a_plan(plan_name="Sweet Cherry Plan", crop="Sweet Cherry")
		self.assertEqual(for_crop["crop"], "Sweet Cherry")

	def test_a_plan_for_a_crop_that_does_not_exist_is_refused(self):
		error = self.tool_error(
			"create_strategic_plan",
			{"company": MAIN, "plan_name": "Ghost Crop Plan", "crop": "Dragonfruit"},
		)
		self.assertIn("Dragonfruit", error)

	def test_the_version_cannot_be_typed(self):
		error = self.tool_error(
			"create_strategic_plan",
			{"company": MAIN, "plan_name": "Numbered By Hand", "version": 7},
		)
		self.assertIn("version is derived", error)

	def test_editing_an_analysis_section_says_to_write_a_new_plan_instead(self):
		plan = self.a_plan()
		changed = self.tool_data(
			"update_strategic_plan",
			{"strategic_plan": plan["name"], "grand_strategy": "Grow into the export lane."},
		)
		self.assertIn("CHANGED ITS MIND", changed["note"])

	def test_a_non_analysis_edit_carries_no_such_note(self):
		"""The negative control: the note must not fire on every edit."""
		plan = self.a_plan()
		changed = self.tool_data(
			"update_strategic_plan", {"strategic_plan": plan["name"], "notes": "Reviewed."}
		)
		self.assertNotIn("note", changed)


class ThePlanIsSuperseded(StrategyTestCase):
	def test_naming_a_predecessor_versions_this_plan(self):
		first = self.a_plan()
		second = self.a_plan(plan_name="Cherry 2027-2032", previous_version=first["name"])
		self.assertEqual(second["version"], 2)
		self.assertEqual(second["superseded"], first["name"])

	def test_the_predecessor_is_retired_in_the_same_call(self):
		first = self.a_plan(status="Implemented")
		self.a_plan(plan_name="Cherry 2027-2032", previous_version=first["name"])

		read = self.tool_data("get_strategic_plan", {"strategic_plan": first["name"]})
		self.assertEqual(read["status"], "Historical")
		self.assertTrue(read["retired_date"])

	def test_the_old_wording_is_untouched(self):
		"""The claim superseding exists for."""
		first = self.a_plan(grand_strategy="Hold the fresh lane and do not expand.")
		self.a_plan(
			plan_name="Cherry 2027-2032",
			previous_version=first["name"],
			grand_strategy="Expand into processing.",
		)
		read = self.tool_data("get_strategic_plan", {"strategic_plan": first["name"]})
		self.assertEqual(read["grand_strategy"], "Hold the fresh lane and do not expand.")

	def test_the_chain_is_readable_in_both_directions(self):
		first = self.a_plan()
		second = self.a_plan(plan_name="Cherry 2027-2032", previous_version=first["name"])
		back = self.tool_data("get_strategic_plan", {"strategic_plan": first["name"]})
		self.assertEqual(back["superseded_by"], second["name"])
		forward = self.tool_data("get_strategic_plan", {"strategic_plan": second["name"]})
		self.assertEqual(forward["previous_version"], first["name"])

	def test_a_third_generation_keeps_counting(self):
		first = self.a_plan()
		second = self.a_plan(plan_name="Second", previous_version=first["name"])
		third = self.a_plan(plan_name="Third", previous_version=second["name"])
		self.assertEqual(third["version"], 3)

	def test_a_plan_cannot_supersede_itself(self):
		plan = self.a_plan()
		error = self.tool_error(
			"update_strategic_plan",
			{"strategic_plan": plan["name"], "previous_version": plan["name"]},
		)
		self.assertIn("cannot supersede itself", error)

	def test_a_loop_in_the_chain_is_refused(self):
		first = self.a_plan()
		second = self.a_plan(plan_name="Second", previous_version=first["name"])
		error = self.tool_error(
			"update_strategic_plan",
			{"strategic_plan": first["name"], "previous_version": second["name"]},
		)
		self.assertIn("loop", error.lower())

	def test_retirement_before_the_effective_date_is_refused(self):
		error = self.tool_error(
			"create_strategic_plan",
			{
				"company": MAIN,
				"plan_name": "Backwards",
				"effective_date": "2026-06-01",
				"retired_date": "2026-01-01",
			},
		)
		self.assertIn("retired", error.lower())

	def test_the_register_names_a_break_in_the_chain(self):
		plan = self.a_plan()
		frappe.db.set_value("Strategic Plan", plan["name"], "status", "Historical")
		frappe.db.set_value("Strategic Plan", plan["name"], "retired_date", None)

		listed = self.tool_data("list_strategic_plans", {"company": MAIN})
		self.assertEqual(listed["historical_without_retired_date"], [plan["name"]])
		self.assertEqual(listed["live"], [])


class TheObjectiveMustBeMeasurable(StrategyTestCase):
	def test_an_objective_belongs_to_a_plan(self):
		plan = self.a_plan()
		objective = self.an_objective(plan["name"])
		self.assertEqual(objective["strategic_plan"], plan["name"])
		self.assertEqual(objective["status"], "Pending")

	def test_an_objective_on_no_plan_is_refused(self):
		error = self.tool_error(
			"create_strategic_objective",
			{"company": MAIN, "strategic_plan": "STRP-99999", "objective": "Something."},
		)
		self.assertIn("STRP-99999", error)

	def test_an_objective_with_no_target_says_so(self):
		plan = self.a_plan()
		objective = self.tool_data(
			"create_strategic_objective",
			{"company": MAIN, "strategic_plan": plan["name"], "objective": "Be better."},
		)
		self.assertIn("nothing to measure against", objective["next_step"])

	def test_an_actual_without_a_date_is_dated_today_by_the_tool(self):
		plan = self.a_plan()
		objective = self.an_objective(plan["name"])
		changed = self.tool_data(
			"update_strategic_objective",
			{"strategic_objective": objective["name"], "kpi_actual": "74%"},
		)
		self.assertEqual(changed["kpi_actual"], "74%")
		self.assertEqual(changed["measured_on"], frappe.utils.today())

	def test_an_undated_actual_written_directly_is_refused_by_the_controller(self):
		"""The tool defaults the date; the controller is what makes it a rule."""
		plan = self.a_plan()
		objective = self.an_objective(plan["name"])
		doc = frappe.get_doc("Strategic Objective", objective["name"])
		doc.kpi_actual = "74%"
		doc.measured_on = None
		with self.assertRaises(Exception) as caught:
			doc.save()
		self.assertIn("undated", str(caught.exception).lower())

	def test_achieved_with_an_empty_actual_is_refused(self):
		plan = self.a_plan()
		objective = self.an_objective(plan["name"])
		error = self.tool_error(
			"update_strategic_objective",
			{"strategic_objective": objective["name"], "status": "Achieved"},
		)
		self.assertIn("Achieved", error)

	def test_achieved_with_an_actual_is_allowed(self):
		"""The negative control for the refusal above."""
		plan = self.a_plan()
		objective = self.an_objective(plan["name"])
		changed = self.tool_data(
			"update_strategic_objective",
			{
				"strategic_objective": objective["name"],
				"status": "Achieved",
				"kpi_actual": "79%",
				"measured_on": "2026-11-01",
			},
		)
		self.assertEqual(changed["status"], "Achieved")

	def test_a_target_and_an_actual_may_be_words_rather_than_numbers(self):
		plan = self.a_plan()
		objective = self.an_objective(
			plan["name"],
			objective="House the crew on site.",
			kpi_metric="housing",
			kpi_target="crew housed on site",
		)
		changed = self.tool_data(
			"update_strategic_objective",
			{
				"strategic_objective": objective["name"],
				"kpi_actual": "18 of 24 housed",
				"measured_on": "2026-08-01",
			},
		)
		self.assertEqual(changed["kpi_actual"], "18 of 24 housed")

	def test_an_objective_filed_against_another_entity_plan_is_refused(self):
		plan = self.a_plan()
		error = self.tool_error(
			"create_strategic_objective",
			{
				"company": "Highland Orchards LLC",
				"strategic_plan": plan["name"],
				"objective": "Cross-company objective.",
			},
		)
		self.assertTrue(error)


class TheHitRateIsHonest(StrategyTestCase):
	def settled_plan(self):
		plan = self.a_plan()
		self.an_objective(
			plan["name"],
			objective="Achieved one.",
			status="Achieved",
			kpi_actual="79%",
			measured_on="2026-11-01",
		)
		self.an_objective(plan["name"], objective="Failed one.", status="Failed")
		self.an_objective(plan["name"], objective="Still going.", status="In Progress")
		return plan

	def test_the_rate_counts_only_settled_objectives(self):
		self.settled_plan()
		listed = self.tool_data("list_strategic_objectives", {"company": MAIN})
		self.assertEqual(listed["objective_count"], 3)
		self.assertEqual(listed["settled_count"], 2)
		self.assertEqual(listed["achieved_rate"], 0.5)

	def test_a_plan_with_nothing_settled_has_no_rate_rather_than_a_zero(self):
		plan = self.a_plan()
		self.an_objective(plan["name"])
		listed = self.tool_data("list_strategic_objectives", {"strategic_plan": plan["name"]})
		self.assertIsNone(listed["achieved_rate"])

	def test_overdue_means_past_its_date_and_still_open(self):
		plan = self.a_plan()
		open_late = self.an_objective(plan["name"], objective="Late and open.", due_date="2020-01-01")
		self.an_objective(plan["name"], objective="Late and failed.", due_date="2020-01-01", status="Failed")
		listed = self.tool_data("list_strategic_objectives", {"strategic_plan": plan["name"]})
		self.assertEqual(listed["overdue"], [open_late["name"]])

	def test_the_plan_read_names_its_own_overdue_and_unmeasured(self):
		plan = self.a_plan()
		self.an_objective(plan["name"], objective="Late.", due_date="2020-01-01")
		read = self.tool_data("get_strategic_plan", {"strategic_plan": plan["name"]})
		self.assertEqual(read["objective_count"], 1)
		self.assertEqual(len(read["objectives_overdue"]), 1)
		self.assertEqual(len(read["objectives_unmeasured"]), 1)

	def test_objectives_are_listable_across_every_plan(self):
		"""The reason this is not a child table."""
		first = self.a_plan()
		second = self.a_plan(plan_name="Second Plan")
		self.an_objective(first["name"], objective="One.", due_date="2020-01-01")
		self.an_objective(second["name"], objective="Two.", due_date="2020-06-01")

		everything = self.tool_data("list_strategic_objectives", {"company": MAIN})
		self.assertEqual(everything["objective_count"], 2)
		self.assertEqual(len(everything["overdue"]), 2)

	def test_the_due_date_window_narrows_the_list(self):
		plan = self.a_plan()
		self.an_objective(plan["name"], objective="Early.", due_date="2026-03-31")
		self.an_objective(plan["name"], objective="Late.", due_date="2026-12-31")
		window = self.tool_data(
			"list_strategic_objectives",
			{"company": MAIN, "due_from": "2026-01-01", "due_to": "2026-06-30"},
		)
		self.assertEqual(window["objective_count"], 1)

	def test_an_objective_reads_back_with_its_plan(self):
		plan = self.a_plan()
		objective = self.an_objective(plan["name"])
		read = self.tool_data("get_strategic_objective", {"strategic_objective": objective["name"]})
		self.assertEqual(read["plan_detail"]["plan_name"], "Cherry 2026-2031")
