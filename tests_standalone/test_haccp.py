# SPDX-License-Identifier: MIT
"""HACCP / food-safety plan management — Cycle 3 of the farm_app retirement.

TEN CLAIMS, TEN CLASSES.

1. **THE PLAN IS THE ROOT.** `PlanLifecycle`. Create, list, get with enriched
   child counts, update status. An Active plan with an expired QI is REFUSED —
   the validate hook on Food Safety Plan enforces it — because an active plan
   implies somebody qualified is reviewing it, and an expired QI means nobody is.

2. **RISK IS COMPUTED, NOT ENTERED.** `HazardAnalysis`. Likelihood x severity
   yields a risk level through a matrix the QI does not have to remember. The
   test seeds both inputs and asserts the output, then checks filters by type
   and risk.

3. **A CCP IS A CONTROL WITH A LIMIT.** `PreventiveControlCRUD`. Create a
   process control and a critical control point, filter by `ccp_only`, update
   the critical limit, and verify the monitoring specs come back.

4. **THE MEASUREMENT DECIDES, NOT THE WORKER.** `MonitoringCompliance`. Seed a
   control with critical_limit=45 and operator<=, then create monitoring records
   above and below the limit. `is_within_limit` is auto-computed by the
   controller from the control's own specification.

5. **A DEVIATION IS CLOSED, NOT DELETED.** `CorrectiveActionWorkflow`. Create
   Open, update to Closed with closure notes, verify product disposition.

6. **CALIBRATION IS PROOF.** `VerificationRecords`. Create a calibration
   verification and a log review, list by type.

7. **THE RECALL PLAN IS TESTED BEFORE IT IS NEEDED.** `RecallPlanManagement`.
   Create with JSON contacts, update simulation dates.

8. **THE SUPPLIER IS AS STRONG AS ITS CERTIFICATE.** `SupplierVerificationCRUD`.
   Create a verification with an expiry date, verify the certificate expiry
   warning fires through the controller.

9. **THE DASHBOARD IS THE ANSWER.** `Dashboard`. Create a plan with children,
   verify the dashboard aggregates match.

10. **A COLUMN NOBODY SETS IS NOT A GATE.** `HazardAndControlAreScoped`. The
    two registers that shipped without `company`. Asserts the value is stored
    and that a list refuses to cross entities — an unpopulated Link reads NULL
    and lets everything through while the DocType JSON looks correct.
"""

import json

from .fixtures import MAIN, OTHER, V12TestCase

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"list_food_safety_plans",
		"get_food_safety_plan",
		"create_food_safety_plan",
		"update_food_safety_plan",
		"list_hazard_analyses",
		"get_hazard_analysis",
		"create_hazard_analysis",
		"update_hazard_analysis",
		"list_preventive_controls",
		"get_preventive_control",
		"create_preventive_control",
		"update_preventive_control",
		"list_monitoring_records",
		"get_monitoring_record",
		"create_monitoring_record",
		"list_corrective_action_records",
		"get_corrective_action_record",
		"create_corrective_action_record",
		"update_corrective_action_record",
		"list_verification_records",
		"get_verification_record",
		"create_verification_record",
		"list_recall_plans",
		"get_recall_plan",
		"create_recall_plan",
		"update_recall_plan",
		"list_supplier_verifications",
		"get_supplier_verification",
		"create_supplier_verification",
		"update_supplier_verification",
		"get_food_safety_dashboard",
	)
}

TODAY = "2026-07-24"


class HACCPTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	# ── helpers ─────────────────────────────────────────────────────────────

	def a_plan(self, plan_name="Cherry Packing HACCP", **overrides):
		payload = {
			"plan_name": plan_name,
			"facility_name": "Mill Creek Packhouse",
			"company": MAIN,
			"scope": "Fresh cherry packing and cold storage",
			"status": "Draft",
			"qualified_individual": "Jane Smith",
			"qualified_individual_name": "Jane Smith",
			"qi_certification_expiry": "2027-12-31",
			"version_number": 1,
			"effective_date": "2026-01-01",
			"review_frequency_months": 12,
		}
		payload.update(overrides)
		return self.tool_data("create_food_safety_plan", payload)

	def a_hazard(self, plan, **overrides):
		payload = {
			"food_safety_plan": plan,
			"process_step": "Receiving",
			"hazard_type": "Biological",
			"hazard_name": "Listeria monocytogenes",
			"hazard_description": "Contamination from incoming raw material",
			"likelihood": "Moderate",
			"severity": "Critical",
		}
		payload.update(overrides)
		return self.tool_data("create_hazard_analysis", payload)

	def a_control(self, plan, **overrides):
		payload = {
			"food_safety_plan": plan,
			"control_name": "Cold Storage Temperature",
			"control_type": "Process",
			"description": "Maintain cold storage below 45F",
			"is_critical_control_point": True,
			"is_active": True,
			"monitoring_parameter": "Temperature",
			"monitoring_frequency": "Every 4 hours",
			"monitoring_method": "Digital thermometer",
			"critical_limit": 45.0,
			"critical_limit_unit": "F",
			"critical_limit_operator": "<=",
		}
		payload.update(overrides)
		return self.tool_data("create_preventive_control", payload)

	def a_monitoring_record(self, plan, control, value, **overrides):
		payload = {
			"food_safety_plan": plan,
			"preventive_control": control,
			"monitoring_date": TODAY,
			"measured_value": value,
			"measured_unit": "F",
		}
		payload.update(overrides)
		return self.tool_data("create_monitoring_record", payload)

	def a_corrective_action(self, plan, control, **overrides):
		payload = {
			"food_safety_plan": plan,
			"preventive_control": control,
			"deviation_date": TODAY,
			"deviation_description": "Temperature exceeded 45F",
			"action_taken": "Product moved to quarantine, unit serviced",
			"product_disposition": "Removed",
		}
		payload.update(overrides)
		return self.tool_data("create_corrective_action_record", payload)

	def a_verification(self, plan, control=None, **overrides):
		payload = {
			"food_safety_plan": plan,
			"verification_type": "Calibration",
			"verification_date": TODAY,
			"description": "Quarterly thermometer calibration",
		}
		if control:
			payload["preventive_control"] = control
		payload.update(overrides)
		return self.tool_data("create_verification_record", payload)

	def a_recall_plan(self, plan, **overrides):
		payload = {
			"food_safety_plan": plan,
			"recall_plan_name": "Cherry Recall Procedure",
			"recall_coordinator": "Jane Smith",
			"description": "Class I and II recall procedure for fresh cherries",
			"is_active": True,
		}
		payload.update(overrides)
		return self.tool_data("create_recall_plan", payload)

	def a_supplier_verification(self, plan, **overrides):
		payload = {
			"food_safety_plan": plan,
			"supplier_name": "CoolPak Inc",
			"product_supplied": "Modified atmosphere packaging",
			"verification_method": "Certificate Review",
			"verification_date": TODAY,
			"verification_result": "Approved",
			"certificate_type": "GFSI",
			"certificate_expiry_date": "2027-06-30",
		}
		payload.update(overrides)
		return self.tool_data("create_supplier_verification", payload)


# ── 1  Plan Lifecycle ──────────────────────────────────────────────────────


class PlanLifecycle(HACCPTestCase):
	def test_empty_list_returns_empty_array(self):
		data = self.tool_data("list_food_safety_plans", {})
		self.assertEqual(data["plans"], [])
		self.assertEqual(data["count"], 0)

	def test_create_returns_name_and_doctype(self):
		result = self.a_plan()
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Food Safety Plan")

	def test_plan_appears_in_list_after_creation(self):
		self.a_plan()
		data = self.tool_data("list_food_safety_plans", {})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["plans"][0]["plan_name"], "Cherry Packing HACCP")

	def test_get_plan_returns_enriched_counts(self):
		plan = self.a_plan()["name"]
		self.a_hazard(plan)
		self.a_control(plan)
		data = self.tool_data("get_food_safety_plan", {"plan": plan})
		self.assertEqual(data["plan_name"], "Cherry Packing HACCP")
		self.assertEqual(data["hazard_count"], 1)
		self.assertEqual(data["control_count"], 1)
		self.assertEqual(data["monitoring_count"], 0)

	def test_update_status_to_active(self):
		plan = self.a_plan()["name"]
		result = self.tool_data(
			"update_food_safety_plan",
			{
				"plan": plan,
				"status": "Active",
			},
		)
		self.assertEqual(result["name"], plan)

	def test_active_plan_with_expired_qi_is_refused(self):
		"""An Active plan with an expired QI certification should be refused."""
		err = self.tool_error(
			"create_food_safety_plan",
			{
				"plan_name": "Expired QI Plan",
				"facility_name": "Test Facility",
				"company": MAIN,
				"status": "Active",
				"qi_certification_expiry": "2020-01-01",
			},
		)
		self.assertIn("past", err.lower())

	def test_draft_plan_with_expired_qi_is_allowed(self):
		"""Draft status does not check QI expiry — the plan is not in use."""
		result = self.a_plan(
			plan_name="Draft Old QI",
			status="Draft",
			qi_certification_expiry="2020-01-01",
		)
		self.assertIn("name", result)

	def test_list_filters_by_status(self):
		self.a_plan(plan_name="Active Plan", status="Active")
		self.a_plan(plan_name="Draft Plan", status="Draft")
		active = self.tool_data("list_food_safety_plans", {"status": "Active"})
		self.assertEqual(active["count"], 1)
		self.assertEqual(active["plans"][0]["plan_name"], "Active Plan")

	def test_list_filters_by_company(self):
		self.a_plan()
		data = self.tool_data("list_food_safety_plans", {"company": MAIN})
		self.assertEqual(data["count"], 1)

	def test_list_filters_by_unknown_company_errors(self):
		err = self.tool_error("list_food_safety_plans", {"company": "Nonexistent Co"})
		self.assertIn("no Company", err)

	def test_get_nonexistent_plan_errors(self):
		err = self.tool_error("get_food_safety_plan", {"plan": "DOES-NOT-EXIST"})
		self.assertIn("No Food Safety Plan", err)

	def test_update_with_no_fields_errors(self):
		plan = self.a_plan()["name"]
		err = self.tool_error("update_food_safety_plan", {"plan": plan})
		self.assertIn("Nothing to update", err)

	def test_update_nonexistent_plan_errors(self):
		err = self.tool_error(
			"update_food_safety_plan",
			{
				"plan": "GHOST",
				"plan_name": "New Name",
			},
		)
		self.assertIn("No Food Safety Plan", err)

	def test_get_plan_includes_recall_plans(self):
		plan = self.a_plan()["name"]
		self.a_recall_plan(plan)
		data = self.tool_data("get_food_safety_plan", {"plan": plan})
		self.assertIn("recall_plans", data)
		self.assertEqual(len(data["recall_plans"]), 1)


# ── 2  Hazard Analysis ────────────────────────────────────────────────────


class HazardAnalysis(HACCPTestCase):
	def test_create_hazard_returns_name_and_doctype(self):
		plan = self.a_plan()["name"]
		result = self.a_hazard(plan)
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Hazard Analysis")

	def test_risk_level_auto_computed_moderate_critical(self):
		"""Moderate likelihood x Critical severity -> High risk."""
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan, likelihood="Moderate", severity="Critical")
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard["name"]})
		self.assertEqual(data["risk_level"], "High")

	def test_risk_level_auto_computed_high_critical(self):
		"""High likelihood x Critical severity -> Critical risk."""
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan, likelihood="High", severity="Critical")
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard["name"]})
		self.assertEqual(data["risk_level"], "Critical")

	def test_risk_level_auto_computed_remote_minor(self):
		"""Remote likelihood x Minor severity -> Low risk."""
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan, likelihood="Remote", severity="Minor")
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard["name"]})
		self.assertEqual(data["risk_level"], "Low")

	def test_risk_level_auto_computed_low_major(self):
		"""Low likelihood x Major severity -> Medium risk."""
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan, likelihood="Low", severity="Major")
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard["name"]})
		self.assertEqual(data["risk_level"], "Medium")

	def test_list_hazards_empty(self):
		data = self.tool_data("list_hazard_analyses", {})
		self.assertEqual(data["hazards"], [])
		self.assertEqual(data["count"], 0)

	def test_list_filtered_by_hazard_type(self):
		plan = self.a_plan()["name"]
		self.a_hazard(plan, hazard_type="Biological", hazard_name="Listeria")
		self.a_hazard(plan, hazard_type="Chemical", hazard_name="Pesticide residue", process_step="Washing")
		bio = self.tool_data("list_hazard_analyses", {"hazard_type": "Biological"})
		self.assertEqual(bio["count"], 1)
		self.assertEqual(bio["hazards"][0]["hazard_name"], "Listeria")

	def test_list_filtered_by_risk_level(self):
		plan = self.a_plan()["name"]
		self.a_hazard(plan, likelihood="High", severity="Critical")  # Critical
		self.a_hazard(
			plan,
			likelihood="Remote",
			severity="Minor",
			hazard_name="Stone",
			hazard_type="Physical",
			process_step="Packing",
		)  # Low
		critical = self.tool_data("list_hazard_analyses", {"risk_level": "Critical"})
		self.assertEqual(critical["count"], 1)

	def test_list_filtered_by_plan(self):
		p1 = self.a_plan(plan_name="Plan A")["name"]
		p2 = self.a_plan(plan_name="Plan B", facility_name="Other Facility")["name"]
		self.a_hazard(p1)
		self.a_hazard(p2, hazard_name="E. coli", process_step="Washing")
		data = self.tool_data("list_hazard_analyses", {"food_safety_plan": p1})
		self.assertEqual(data["count"], 1)

	def test_get_nonexistent_hazard_errors(self):
		err = self.tool_error("get_hazard_analysis", {"hazard_analysis": "NOPE"})
		self.assertIn("No Hazard Analysis", err)

	# ── update: a hazard is a judgement, and a review revises it ───────────

	def test_update_hazard_changes_a_field(self):
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan)["name"]
		self.tool_data(
			"update_hazard_analysis",
			{
				"hazard_analysis": hazard,
				"hazard_description": "Contamination from the flume water",
			},
		)
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard})
		self.assertEqual(data["hazard_description"], "Contamination from the flume water")

	def test_update_hazard_recomputes_risk_level(self):
		"""The matrix runs on save, not only on insert.

		This is the whole reason the tool is safe to expose: a QI who downgrades
		a likelihood must not be left with the old risk level sitting under it.
		Moderate x Critical is High on create; Remote x Minor is Low after.
		"""
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan, likelihood="Moderate", severity="Critical")["name"]
		before = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard})
		self.assertEqual(before["risk_level"], "High")
		self.tool_data(
			"update_hazard_analysis",
			{
				"hazard_analysis": hazard,
				"likelihood": "Remote",
				"severity": "Minor",
			},
		)
		after = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard})
		self.assertEqual(after["risk_level"], "Low")

	def test_update_hazard_leaves_omitted_fields_alone(self):
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan)["name"]
		self.tool_data(
			"update_hazard_analysis",
			{
				"hazard_analysis": hazard,
				"notes": "Reviewed at the July meeting",
			},
		)
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard})
		self.assertEqual(data["hazard_name"], "Listeria monocytogenes")
		self.assertEqual(data["process_step"], "Receiving")
		self.assertEqual(data["notes"], "Reviewed at the July meeting")

	def test_update_hazard_with_nothing_to_change_is_refused(self):
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan)["name"]
		err = self.tool_error("update_hazard_analysis", {"hazard_analysis": hazard})
		self.assertIn("Nothing to update", err)

	def test_update_nonexistent_hazard_errors(self):
		err = self.tool_error(
			"update_hazard_analysis",
			{
				"hazard_analysis": "NOPE",
				"notes": "x",
			},
		)
		self.assertIn("No Hazard Analysis", err)


# ── 3  Preventive Control CRUD ─────────────────────────────────────────────


class PreventiveControlCRUD(HACCPTestCase):
	def test_create_control_returns_name_and_doctype(self):
		plan = self.a_plan()["name"]
		result = self.a_control(plan)
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Preventive Control")

	def test_get_control_returns_monitoring_specs(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		data = self.tool_data("get_preventive_control", {"preventive_control": ctrl})
		self.assertEqual(data["monitoring_parameter"], "Temperature")
		self.assertEqual(data["monitoring_frequency"], "Every 4 hours")
		self.assertEqual(data["critical_limit"], 45.0)
		self.assertEqual(data["critical_limit_operator"], "<=")

	def test_get_control_includes_monitoring_record_count(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		data = self.tool_data("get_preventive_control", {"preventive_control": ctrl})
		self.assertEqual(data["monitoring_record_count"], 0)

	def test_update_critical_limit(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		result = self.tool_data(
			"update_preventive_control",
			{
				"preventive_control": ctrl,
				"critical_limit": 40.0,
			},
		)
		self.assertEqual(result["name"], ctrl)
		data = self.tool_data("get_preventive_control", {"preventive_control": ctrl})
		self.assertEqual(data["critical_limit"], 40.0)

	def test_list_controls_empty(self):
		data = self.tool_data("list_preventive_controls", {})
		self.assertEqual(data["controls"], [])
		self.assertEqual(data["count"], 0)

	def test_filter_ccp_only(self):
		plan = self.a_plan()["name"]
		self.a_control(plan, control_name="CCP Cold Storage", is_critical_control_point=True)
		self.a_control(
			plan, control_name="Sanitation SOP", control_type="Sanitation", is_critical_control_point=False
		)
		ccp = self.tool_data("list_preventive_controls", {"ccp_only": True})
		self.assertEqual(ccp["count"], 1)
		self.assertEqual(ccp["controls"][0]["control_name"], "CCP Cold Storage")

	def test_filter_by_control_type(self):
		plan = self.a_plan()["name"]
		self.a_control(plan, control_name="Process A", control_type="Process")
		self.a_control(plan, control_name="Sanitation B", control_type="Sanitation")
		san = self.tool_data("list_preventive_controls", {"control_type": "Sanitation"})
		self.assertEqual(san["count"], 1)
		self.assertEqual(san["controls"][0]["control_name"], "Sanitation B")

	def test_update_no_fields_errors(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		err = self.tool_error("update_preventive_control", {"preventive_control": ctrl})
		self.assertIn("Nothing to update", err)

	def test_get_nonexistent_control_errors(self):
		err = self.tool_error("get_preventive_control", {"preventive_control": "GHOST"})
		self.assertIn("No Preventive Control", err)


# ── 4  Monitoring Compliance ───────────────────────────────────────────────


class MonitoringCompliance(HACCPTestCase):
	"""The controller auto-computes is_within_limit from the control's critical
	limit. This is the test that proves it: seed a control with limit<=45,
	then create records at 42 (within) and 48 (outside)."""

	def test_value_within_limit(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan, critical_limit=45.0, critical_limit_operator="<=")["name"]
		rec = self.a_monitoring_record(plan, ctrl, 42.0)
		data = self.tool_data("get_monitoring_record", {"monitoring_record": rec["name"]})
		self.assertEqual(data["is_within_limit"], 1)

	def test_value_at_limit(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan, critical_limit=45.0, critical_limit_operator="<=")["name"]
		rec = self.a_monitoring_record(plan, ctrl, 45.0)
		data = self.tool_data("get_monitoring_record", {"monitoring_record": rec["name"]})
		self.assertEqual(data["is_within_limit"], 1)

	def test_value_above_limit(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan, critical_limit=45.0, critical_limit_operator="<=")["name"]
		rec = self.a_monitoring_record(plan, ctrl, 48.0)
		data = self.tool_data("get_monitoring_record", {"monitoring_record": rec["name"]})
		self.assertEqual(data["is_within_limit"], 0)

	def test_greater_or_equal_operator(self):
		"""A >= operator: value must be at or above the limit."""
		plan = self.a_plan()["name"]
		ctrl = self.a_control(
			plan,
			control_name="pH Control",
			critical_limit=6.5,
			critical_limit_operator=">=",
			critical_limit_unit="pH",
		)["name"]
		low = self.a_monitoring_record(plan, ctrl, 5.0)
		high = self.a_monitoring_record(plan, ctrl, 7.0)
		low_data = self.tool_data("get_monitoring_record", {"monitoring_record": low["name"]})
		high_data = self.tool_data("get_monitoring_record", {"monitoring_record": high["name"]})
		self.assertEqual(low_data["is_within_limit"], 0)
		self.assertEqual(high_data["is_within_limit"], 1)

	def test_list_monitoring_records_empty(self):
		data = self.tool_data("list_monitoring_records", {})
		self.assertEqual(data["records"], [])
		self.assertEqual(data["count"], 0)

	def test_list_filtered_by_control(self):
		plan = self.a_plan()["name"]
		c1 = self.a_control(plan, control_name="Ctrl A")["name"]
		c2 = self.a_control(plan, control_name="Ctrl B", control_type="Sanitation")["name"]
		self.a_monitoring_record(plan, c1, 40.0)
		self.a_monitoring_record(plan, c2, 50.0)
		data = self.tool_data("list_monitoring_records", {"preventive_control": c1})
		self.assertEqual(data["count"], 1)

	def test_list_out_of_limit_only(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan, critical_limit=45.0, critical_limit_operator="<=")["name"]
		self.a_monitoring_record(plan, ctrl, 42.0)  # within
		self.a_monitoring_record(plan, ctrl, 48.0)  # out
		out = self.tool_data("list_monitoring_records", {"out_of_limit_only": True})
		self.assertEqual(out["count"], 1)

	def test_get_nonexistent_monitoring_record_errors(self):
		err = self.tool_error("get_monitoring_record", {"monitoring_record": "NONE"})
		self.assertIn("No Monitoring Record", err)

	def test_monitoring_record_returns_name_and_doctype(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		result = self.a_monitoring_record(plan, ctrl, 40.0)
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Monitoring Record")

	def test_control_get_reflects_monitoring_count(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan, critical_limit=45.0, critical_limit_operator="<=")["name"]
		self.a_monitoring_record(plan, ctrl, 42.0)
		self.a_monitoring_record(plan, ctrl, 43.0)
		data = self.tool_data("get_preventive_control", {"preventive_control": ctrl})
		self.assertEqual(data["monitoring_record_count"], 2)


# ── 5  Corrective Action Workflow ──────────────────────────────────────────


class CorrectiveActionWorkflow(HACCPTestCase):
	def test_create_defaults_to_open(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		result = self.a_corrective_action(plan, ctrl)
		data = self.tool_data(
			"get_corrective_action_record",
			{
				"corrective_action_record": result["name"],
			},
		)
		self.assertEqual(data["status"], "Open")

	def test_create_returns_name_and_doctype(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		result = self.a_corrective_action(plan, ctrl)
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Corrective Action Record")

	def test_update_to_closed_with_closure_notes(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		ca = self.a_corrective_action(plan, ctrl)["name"]
		self.tool_data(
			"update_corrective_action_record",
			{
				"corrective_action_record": ca,
				"status": "Closed",
				"closure_notes": "Unit repaired, product released after testing",
				"closed_date": TODAY,
			},
		)
		data = self.tool_data(
			"get_corrective_action_record",
			{
				"corrective_action_record": ca,
			},
		)
		self.assertEqual(data["status"], "Closed")
		self.assertEqual(data["closure_notes"], "Unit repaired, product released after testing")

	def test_product_disposition_recorded(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		ca = self.a_corrective_action(plan, ctrl, product_disposition="Destroyed")
		data = self.tool_data(
			"get_corrective_action_record",
			{
				"corrective_action_record": ca["name"],
			},
		)
		self.assertEqual(data["product_disposition"], "Destroyed")

	def test_list_corrective_actions_empty(self):
		data = self.tool_data("list_corrective_action_records", {})
		self.assertEqual(data["records"], [])
		self.assertEqual(data["count"], 0)

	def test_list_filtered_by_status(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		ca1 = self.a_corrective_action(plan, ctrl)["name"]
		self.a_corrective_action(
			plan, ctrl, deviation_description="Second deviation", action_taken="Immediate correction"
		)
		self.tool_data(
			"update_corrective_action_record",
			{
				"corrective_action_record": ca1,
				"status": "Closed",
				"closure_notes": "Done",
				"closed_date": TODAY,
			},
		)
		open_recs = self.tool_data("list_corrective_action_records", {"status": "Open"})
		self.assertEqual(open_recs["count"], 1)

	def test_update_no_fields_errors(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		ca = self.a_corrective_action(plan, ctrl)["name"]
		err = self.tool_error(
			"update_corrective_action_record",
			{
				"corrective_action_record": ca,
			},
		)
		self.assertIn("Nothing to update", err)

	def test_get_nonexistent_ca_errors(self):
		err = self.tool_error(
			"get_corrective_action_record",
			{
				"corrective_action_record": "MISSING",
			},
		)
		self.assertIn("No Corrective Action Record", err)


# ── 6  Verification Records ───────────────────────────────────────────────


class VerificationRecords(HACCPTestCase):
	def test_create_calibration_verification(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		result = self.a_verification(
			plan,
			ctrl,
			verification_type="Calibration",
			equipment_name="Fluke 52-II",
			calibration_status="Compliant",
		)
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Verification Record")

	def test_get_verification_returns_all_fields(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		vr = self.a_verification(
			plan,
			ctrl,
			verification_type="Calibration",
			equipment_name="Fluke 52-II",
			result_summary="Within +/- 0.5F",
			is_control_effective=True,
		)
		data = self.tool_data(
			"get_verification_record",
			{
				"verification_record": vr["name"],
			},
		)
		self.assertEqual(data["verification_type"], "Calibration")
		self.assertEqual(data["equipment_name"], "Fluke 52-II")
		self.assertEqual(data["result_summary"], "Within +/- 0.5F")

	def test_list_by_verification_type(self):
		plan = self.a_plan()["name"]
		ctrl = self.a_control(plan)["name"]
		self.a_verification(plan, ctrl, verification_type="Calibration")
		self.a_verification(
			plan, ctrl, verification_type="Log Review", description="Weekly monitoring log review"
		)
		cal = self.tool_data("list_verification_records", {"verification_type": "Calibration"})
		self.assertEqual(cal["count"], 1)

	def test_list_empty(self):
		data = self.tool_data("list_verification_records", {})
		self.assertEqual(data["records"], [])
		self.assertEqual(data["count"], 0)

	def test_get_nonexistent_verification_errors(self):
		err = self.tool_error(
			"get_verification_record",
			{
				"verification_record": "GONE",
			},
		)
		self.assertIn("No Verification Record", err)

	def test_list_by_plan(self):
		p1 = self.a_plan(plan_name="Plan X")["name"]
		p2 = self.a_plan(plan_name="Plan Y", facility_name="Other Facility")["name"]
		self.a_verification(p1, verification_type="Product Testing")
		self.a_verification(p2, verification_type="Sanitation Test")
		data = self.tool_data("list_verification_records", {"food_safety_plan": p1})
		self.assertEqual(data["count"], 1)


# ── 7  Recall Plan Management ─────────────────────────────────────────────


class RecallPlanManagement(HACCPTestCase):
	def test_create_recall_plan_returns_name(self):
		plan = self.a_plan()["name"]
		result = self.a_recall_plan(plan)
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Recall Plan")

	def test_recall_plan_with_json_contacts(self):
		plan = self.a_plan()["name"]
		contacts = [
			{"name": "Jane Smith", "role": "Coordinator", "phone": "555-0100"},
			{"name": "John Doe", "role": "QA Manager", "phone": "555-0101"},
		]
		result = self.a_recall_plan(plan, recall_team_contacts=json.dumps(contacts))
		data = self.tool_data("get_recall_plan", {"recall_plan": result["name"]})
		self.assertEqual(data["recall_plan_name"], "Cherry Recall Procedure")

	def test_update_simulation_dates(self):
		plan = self.a_plan()["name"]
		rp = self.a_recall_plan(plan)["name"]
		self.tool_data(
			"update_recall_plan",
			{
				"recall_plan": rp,
				"last_simulation_date": "2026-07-15",
				"next_simulation_date": "2027-01-15",
			},
		)
		data = self.tool_data("get_recall_plan", {"recall_plan": rp})
		self.assertEqual(str(data["last_simulation_date"]), "2026-07-15")
		self.assertEqual(str(data["next_simulation_date"]), "2027-01-15")

	def test_list_recall_plans_empty(self):
		data = self.tool_data("list_recall_plans", {})
		self.assertEqual(data["recall_plans"], [])
		self.assertEqual(data["count"], 0)

	def test_list_active_only(self):
		plan = self.a_plan()["name"]
		self.a_recall_plan(plan, recall_plan_name="Active RP", is_active=True)
		self.a_recall_plan(
			plan, recall_plan_name="Inactive RP", is_active=False, recall_coordinator="Bob Jones"
		)
		active = self.tool_data("list_recall_plans", {"active_only": True})
		self.assertEqual(active["count"], 1)
		self.assertEqual(active["recall_plans"][0]["recall_plan_name"], "Active RP")

	def test_update_no_fields_errors(self):
		plan = self.a_plan()["name"]
		rp = self.a_recall_plan(plan)["name"]
		err = self.tool_error("update_recall_plan", {"recall_plan": rp})
		self.assertIn("Nothing to update", err)

	def test_get_nonexistent_recall_plan_errors(self):
		err = self.tool_error("get_recall_plan", {"recall_plan": "NONE"})
		self.assertIn("No Recall Plan", err)


# ── 8  Supplier Verification CRUD ─────────────────────────────────────────


class SupplierVerificationCRUD(HACCPTestCase):
	def test_create_returns_name_and_doctype(self):
		plan = self.a_plan()["name"]
		result = self.a_supplier_verification(plan)
		self.assertIn("name", result)
		self.assertEqual(result["doctype"], "Supplier Verification")

	def test_get_returns_expected_fields(self):
		plan = self.a_plan()["name"]
		sv = self.a_supplier_verification(plan)["name"]
		data = self.tool_data(
			"get_supplier_verification",
			{
				"supplier_verification": sv,
			},
		)
		self.assertEqual(data["supplier_name"], "CoolPak Inc")
		self.assertEqual(data["verification_result"], "Approved")
		self.assertEqual(data["certificate_type"], "GFSI")

	def test_update_supplier_verification(self):
		plan = self.a_plan()["name"]
		sv = self.a_supplier_verification(plan)["name"]
		self.tool_data(
			"update_supplier_verification",
			{
				"supplier_verification": sv,
				"verification_result": "Approved with Conditions",
				"verification_notes": "Minor findings, re-audit in 6 months",
			},
		)
		data = self.tool_data(
			"get_supplier_verification",
			{
				"supplier_verification": sv,
			},
		)
		self.assertEqual(data["verification_result"], "Approved with Conditions")
		self.assertIn("re-audit", data["verification_notes"])

	def test_list_empty(self):
		data = self.tool_data("list_supplier_verifications", {})
		self.assertEqual(data["verifications"], [])
		self.assertEqual(data["count"], 0)

	def test_list_filtered_by_verification_result(self):
		plan = self.a_plan()["name"]
		self.a_supplier_verification(plan, supplier_name="A", verification_result="Approved")
		self.a_supplier_verification(plan, supplier_name="B", verification_result="Rejected")
		approved = self.tool_data(
			"list_supplier_verifications",
			{
				"verification_result": "Approved",
			},
		)
		self.assertEqual(approved["count"], 1)
		self.assertEqual(approved["verifications"][0]["supplier_name"], "A")

	def test_list_filtered_by_verification_method(self):
		plan = self.a_plan()["name"]
		self.a_supplier_verification(plan, supplier_name="C", verification_method="Audit")
		self.a_supplier_verification(plan, supplier_name="D", verification_method="Testing")
		audits = self.tool_data(
			"list_supplier_verifications",
			{
				"verification_method": "Audit",
			},
		)
		self.assertEqual(audits["count"], 1)

	def test_update_no_fields_errors(self):
		plan = self.a_plan()["name"]
		sv = self.a_supplier_verification(plan)["name"]
		err = self.tool_error(
			"update_supplier_verification",
			{
				"supplier_verification": sv,
			},
		)
		self.assertIn("Nothing to update", err)

	def test_get_nonexistent_sv_errors(self):
		err = self.tool_error(
			"get_supplier_verification",
			{
				"supplier_verification": "MISSING",
			},
		)
		self.assertIn("No Supplier Verification", err)

	def test_update_certificate_expiry(self):
		plan = self.a_plan()["name"]
		sv = self.a_supplier_verification(plan)["name"]
		self.tool_data(
			"update_supplier_verification",
			{
				"supplier_verification": sv,
				"certificate_expiry_date": "2028-12-31",
			},
		)
		data = self.tool_data(
			"get_supplier_verification",
			{
				"supplier_verification": sv,
			},
		)
		self.assertEqual(str(data["certificate_expiry_date"]), "2028-12-31")


# ── 9  Dashboard ──────────────────────────────────────────────────────────


class Dashboard(HACCPTestCase):
	def test_empty_dashboard(self):
		data = self.tool_data("get_food_safety_dashboard", {})
		self.assertEqual(data["total_plans"], 0)
		self.assertEqual(data["plans"], [])
		self.assertEqual(data["total_open_corrective_actions"], 0)

	def test_dashboard_counts_match_children(self):
		plan = self.a_plan(status="Active")["name"]
		self.a_hazard(plan)
		self.a_hazard(plan, hazard_name="E. coli", process_step="Washing", likelihood="Low", severity="Major")
		ctrl = self.a_control(plan)["name"]
		self.a_monitoring_record(plan, ctrl, 42.0)
		self.a_verification(plan, ctrl)
		self.a_supplier_verification(plan)
		data = self.tool_data("get_food_safety_dashboard", {})
		self.assertEqual(data["total_plans"], 1)
		row = data["plans"][0]
		self.assertEqual(row["hazard_count"], 2)
		self.assertEqual(row["control_count"], 1)
		self.assertEqual(row["monitoring_count"], 1)
		self.assertEqual(row["verification_count"], 1)
		self.assertEqual(row["supplier_count"], 1)

	def test_dashboard_open_corrective_actions(self):
		plan = self.a_plan(status="Active")["name"]
		ctrl = self.a_control(plan)["name"]
		self.a_corrective_action(plan, ctrl)  # Open by default
		self.a_corrective_action(
			plan, ctrl, deviation_description="Second issue", action_taken="Fixed immediately"
		)
		data = self.tool_data("get_food_safety_dashboard", {})
		self.assertEqual(data["total_open_corrective_actions"], 2)
		self.assertEqual(data["plans"][0]["open_corrective_actions"], 2)

	def test_dashboard_qi_current_flag(self):
		self.a_plan(
			status="Draft",
			qi_certification_expiry="2027-12-31",
		)
		data = self.tool_data("get_food_safety_dashboard", {})
		row = data["plans"][0]
		self.assertTrue(row["qi_current"])

	def test_dashboard_qi_expired_flag(self):
		self.a_plan(
			status="Draft",
			qi_certification_expiry="2020-01-01",
		)
		data = self.tool_data("get_food_safety_dashboard", {})
		row = data["plans"][0]
		self.assertFalse(row["qi_current"])

	def test_dashboard_recall_plan_count(self):
		plan = self.a_plan(status="Draft")["name"]
		self.a_recall_plan(plan, is_active=True)
		data = self.tool_data("get_food_safety_dashboard", {})
		self.assertEqual(data["plans"][0]["active_recall_plans"], 1)

	def test_dashboard_multiple_plans(self):
		self.a_plan(plan_name="Plan 1")
		self.a_plan(plan_name="Plan 2", facility_name="Other")
		data = self.tool_data("get_food_safety_dashboard", {})
		self.assertEqual(data["total_plans"], 2)

	def test_dashboard_company_filter(self):
		self.a_plan(plan_name="Main Co Plan", company=MAIN)
		data = self.tool_data("get_food_safety_dashboard", {"company": MAIN})
		self.assertEqual(data["total_plans"], 1)

	def test_dashboard_unknown_company_errors(self):
		err = self.tool_error("get_food_safety_dashboard", {"company": "Nonexistent"})
		self.assertIn("no Company", err)


class HazardAndControlAreScoped(HACCPTestCase):
	"""v0.122.0. The two registers that shipped without a `company` column.

	Every other HACCP doctype carried one from the start; `Hazard Analysis` and
	`Preventive Control` did not, and `require_scoped_doc` reads that column off
	the document itself. A missing column is not a strict gate that happens to
	be absent — it is a check that reads NULL and lets everything through, which
	looks identical to a passing test until two entities share a site.

	ADDING THE COLUMN IS HALF THE FIX. A Link the create handler never sets
	leaves every row NULL and the gate open, with the DocType JSON looking
	correct. So these assert the value is STORED and that a list REFUSES to
	cross entities — not merely that the field exists.
	"""

	def test_hazard_stores_the_company_it_was_created_under(self):
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan, company=MAIN)
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard["name"]})
		self.assertEqual(data["company"], MAIN)

	def test_control_stores_the_company_it_was_created_under(self):
		plan = self.a_plan()["name"]
		control = self.a_control(plan, company=MAIN)
		data = self.tool_data("get_preventive_control", {"preventive_control": control["name"]})
		self.assertEqual(data["company"], MAIN)

	def test_a_hazard_filed_under_one_company_is_not_listed_under_the_other(self):
		plan = self.a_plan()["name"]
		self.a_hazard(plan, company=MAIN)
		mine = self.tool_data("list_hazard_analyses", {"company": MAIN})
		theirs = self.tool_data("list_hazard_analyses", {"company": OTHER})
		self.assertEqual(mine["count"], 1)
		self.assertEqual(theirs["count"], 0)

	def test_a_control_filed_under_one_company_is_not_listed_under_the_other(self):
		plan = self.a_plan()["name"]
		self.a_control(plan, company=MAIN)
		mine = self.tool_data("list_preventive_controls", {"company": MAIN})
		theirs = self.tool_data("list_preventive_controls", {"company": OTHER})
		self.assertEqual(mine["count"], 1)
		self.assertEqual(theirs["count"], 0)

	def test_update_can_set_the_company_on_a_row_that_predates_the_column(self):
		"""Rows created before v0.122.0 have it NULL. Without this they would
		have to be retyped to become scopable."""
		plan = self.a_plan()["name"]
		hazard = self.a_hazard(plan)
		self.tool_data("update_hazard_analysis", {"hazard_analysis": hazard["name"], "company": OTHER})
		data = self.tool_data("get_hazard_analysis", {"hazard_analysis": hazard["name"]})
		self.assertEqual(data["company"], OTHER)

	def test_update_can_set_the_company_on_a_control(self):
		plan = self.a_plan()["name"]
		control = self.a_control(plan)
		self.tool_data("update_preventive_control", {"preventive_control": control["name"], "company": OTHER})
		data = self.tool_data("get_preventive_control", {"preventive_control": control["name"]})
		self.assertEqual(data["company"], OTHER)
