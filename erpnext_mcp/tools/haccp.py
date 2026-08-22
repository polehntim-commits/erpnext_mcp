# SPDX-License-Identifier: MIT
"""HACCP / food-safety plan management tools — Cycle 3 of the farm_app retirement.

Eight DocTypes migrated from the Flask sidecar's food-safety module, covering the
full FSMA preventive-controls framework:

    Food Safety Plan          master plan with QI and lifecycle
    Hazard Analysis           per-step hazard identification (risk matrix)
    Preventive Control        CCP definitions, critical limits, monitoring specs
    Monitoring Record         actual measurements against a control
    Corrective Action Record  deviations and what was done about them
    Verification Record       calibration, log review, product testing
    Recall Plan               FDA recall procedures and contacts
    Supplier Verification     supply-chain verification with certificate tracking

THE PLAN IS THE ROOT. Every other record links back to a Food Safety Plan, and
most link to a Preventive Control inside it. The plan is the thing an auditor
asks for; these records are what makes the answer credible.

TOOLS ARE CRUD, NOT WORKFLOW. Unlike the inspection records that branch on
findings, HACCP records are document-management: create, list, read, update. The
compliance value is in the EXISTENCE of the records and their COMPLETENESS, not
in an automated state machine — a qualified individual reviews the plan, not a
tool.

THE DASHBOARD IS THE ANSWER TO "ARE WE READY". It summarises every plan on the
site: how many controls, how many open corrective actions, whether the QI is
current, whether the recall plan has been simulated recently. An auditor asks
that question; this tool answers it without reading eight registers individually.
"""

from __future__ import annotations

import json

import frappe

from .. import compat
from ..args import as_bool, as_date, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

# ── DocType names ────────────────────────────────────────────────────────────

FOOD_SAFETY_PLAN = "Food Safety Plan"
HAZARD_ANALYSIS = "Hazard Analysis"
PREVENTIVE_CONTROL = "Preventive Control"
MONITORING_RECORD = "Monitoring Record"
CORRECTIVE_ACTION_RECORD = "Corrective Action Record"
VERIFICATION_RECORD = "Verification Record"
RECALL_PLAN = "Recall Plan"
SUPPLIER_VERIFICATION = "Supplier Verification"

ALL_HACCP_DOCTYPES = (
	FOOD_SAFETY_PLAN,
	HAZARD_ANALYSIS,
	PREVENTIVE_CONTROL,
	MONITORING_RECORD,
	CORRECTIVE_ACTION_RECORD,
	VERIFICATION_RECORD,
	RECALL_PLAN,
	SUPPLIER_VERIFICATION,
)

RECORD_CAP = 500


# ── shared helpers ───────────────────────────────────────────────────────────


def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _company(args: dict) -> str | None:
	return resolve_company(as_str(args, "company"), required=False)


def _get_one(doctype: str, name: str, fields: tuple) -> dict:
	name = (name or "").strip()
	if not name:
		raise ToolError(f"A {doctype} docname is required.")
	if not frappe.db.exists(doctype, name):
		raise ToolError(f"No {doctype} called {name!r} on this site.")
	return dict(
		frappe.db.get_value(
			doctype,
			name,
			compat.existing_fields(doctype, fields),
			as_dict=True,
		)
		or {}
	)


def _create(doctype: str, values: dict) -> dict:
	doc = frappe.new_doc(doctype)
	for key, val in values.items():
		if val is not None:
			doc.set(key, val)
	doc.insert()
	return {"name": doc.name, "doctype": doctype}


def _update(doctype: str, name: str, values: dict) -> dict:
	name = (name or "").strip()
	if not name:
		raise ToolError(f"A {doctype} docname is required.")
	if not frappe.db.exists(doctype, name):
		raise ToolError(f"No {doctype} called {name!r} on this site.")
	doc = frappe.get_doc(doctype, name)
	for key, val in values.items():
		if val is not None:
			doc.set(key, val)
	doc.save()
	return {"name": doc.name, "doctype": doctype}


def _json_field(args: dict, key: str) -> list | None:
	"""Parse a JSON field that may arrive as a string or a list."""
	raw = args.get(key)
	if raw is None:
		return None
	if isinstance(raw, list):
		return raw
	if isinstance(raw, str):
		raw = raw.strip()
		if not raw:
			return None
		try:
			parsed = json.loads(raw)
			if isinstance(parsed, list):
				return parsed
		except (json.JSONDecodeError, TypeError):
			pass
		raise ToolError(f"{key} must be a JSON array or a list, got: {raw!r}")
	raise ToolError(f"{key} must be a JSON array or a list, got {type(raw).__name__}.")


# ── Food Safety Plan ─────────────────────────────────────────────────────────

_PLAN_FIELDS = (
	"name",
	"plan_name",
	"facility_name",
	"company",
	"scope",
	"status",
	"covered_activities",
	"company_gln",
	"company_address",
	"qualified_individual",
	"qualified_individual_name",
	"qi_certification_expiry",
	"qi_training_description",
	"version_number",
	"effective_date",
	"review_frequency_months",
	"last_review_date",
	"next_review_date",
	"notes",
	"creation",
	"modified",
	"owner",
)


def list_food_safety_plans(args: dict) -> ToolResult:
	_require(FOOD_SAFETY_PLAN)
	filters = {}
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		FOOD_SAFETY_PLAN,
		filters=filters,
		fields=compat.existing_fields(FOOD_SAFETY_PLAN, _PLAN_FIELDS),
		order_by="creation desc",
		limit=limit,
	)
	return ToolResult(data={"plans": [dict(r) for r in rows], "count": len(rows)})


def get_food_safety_plan(args: dict) -> ToolResult:
	_require(FOOD_SAFETY_PLAN)
	name = as_str(args, "plan") or as_str(args, "food_safety_plan")
	row = _get_one(FOOD_SAFETY_PLAN, name, _PLAN_FIELDS)
	# Enrich with child counts.
	for child_dt, key in (
		(HAZARD_ANALYSIS, "hazard_count"),
		(PREVENTIVE_CONTROL, "control_count"),
		(MONITORING_RECORD, "monitoring_count"),
		(CORRECTIVE_ACTION_RECORD, "corrective_action_count"),
		(VERIFICATION_RECORD, "verification_count"),
		(SUPPLIER_VERIFICATION, "supplier_verification_count"),
	):
		if compat.doctype_exists(child_dt):
			row[key] = frappe.db.count(child_dt, {"food_safety_plan": name})
		else:
			row[key] = 0
	if compat.doctype_exists(RECALL_PLAN):
		recall = frappe.db.get_all(
			RECALL_PLAN,
			filters={"food_safety_plan": name},
			fields=["name", "recall_plan_name", "is_active"],
			limit=10,
		)
		row["recall_plans"] = [dict(r) for r in recall]
	return ToolResult(data=row)


def create_food_safety_plan(args: dict) -> ToolResult:
	_require(FOOD_SAFETY_PLAN)
	values = {
		"plan_name": as_str(args, "plan_name"),
		"facility_name": as_str(args, "facility_name"),
		"company": _company(args),
		"scope": as_str(args, "scope"),
		"status": as_str(args, "status") or "Draft",
		"covered_activities": _json_field(args, "covered_activities"),
		"company_gln": as_str(args, "company_gln"),
		"company_address": as_str(args, "company_address"),
		"qualified_individual": as_str(args, "qualified_individual"),
		"qualified_individual_name": as_str(args, "qualified_individual_name"),
		"qi_certification_expiry": as_date(args, "qi_certification_expiry"),
		"qi_training_description": as_str(args, "qi_training_description"),
		"version_number": as_int(args, "version_number"),
		"effective_date": as_date(args, "effective_date"),
		"review_frequency_months": as_int(args, "review_frequency_months"),
		"last_review_date": as_date(args, "last_review_date"),
		"next_review_date": as_date(args, "next_review_date"),
		"notes": as_str(args, "notes"),
	}
	result = _create(FOOD_SAFETY_PLAN, values)
	return ToolResult(data=result)


def update_food_safety_plan(args: dict) -> ToolResult:
	_require(FOOD_SAFETY_PLAN)
	name = as_str(args, "plan") or as_str(args, "food_safety_plan")
	values = {}
	for key in (
		"plan_name",
		"facility_name",
		"scope",
		"status",
		"company_gln",
		"company_address",
		"qualified_individual",
		"qualified_individual_name",
		"qi_training_description",
		"notes",
	):
		val = as_str(args, key)
		if val:
			values[key] = val
	for key in ("qi_certification_expiry", "effective_date", "last_review_date", "next_review_date"):
		val = as_date(args, key)
		if val is not None:
			values[key] = val
	for key in ("version_number", "review_frequency_months"):
		val = as_int(args, key)
		if val is not None:
			values[key] = val
	covered = _json_field(args, "covered_activities")
	if covered is not None:
		values["covered_activities"] = covered
	company = _company(args)
	if company:
		values["company"] = company
	if not values:
		raise ToolError("Nothing to update — pass at least one field.")
	result = _update(FOOD_SAFETY_PLAN, name, values)
	return ToolResult(data=result)


# ── Hazard Analysis ──────────────────────────────────────────────────────────

_HAZARD_FIELDS = (
	"name",
	"food_safety_plan",
	"process_step",
	"cte_type",
	"description",
	"hazard_type",
	"hazard_name",
	"hazard_description",
	"likelihood",
	"severity",
	"risk_level",
	"is_preventable",
	"potential_sources",
	"conditions_for_hazard",
	"company",
	"notes",
	"creation",
	"owner",
)


def list_hazard_analyses(args: dict) -> ToolResult:
	_require(HAZARD_ANALYSIS)
	filters = {}
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		filters["food_safety_plan"] = plan
	hazard_type = as_str(args, "hazard_type")
	if hazard_type:
		filters["hazard_type"] = hazard_type
	risk_level = as_str(args, "risk_level")
	if risk_level:
		filters["risk_level"] = risk_level
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		HAZARD_ANALYSIS,
		filters=filters,
		fields=compat.existing_fields(HAZARD_ANALYSIS, _HAZARD_FIELDS),
		order_by="creation desc",
		limit=limit,
	)
	return ToolResult(data={"hazards": [dict(r) for r in rows], "count": len(rows)})


def get_hazard_analysis(args: dict) -> ToolResult:
	_require(HAZARD_ANALYSIS)
	name = as_str(args, "hazard_analysis") or as_str(args, "hazard")
	row = _get_one(HAZARD_ANALYSIS, name, _HAZARD_FIELDS)
	return ToolResult(data=row)


def create_hazard_analysis(args: dict) -> ToolResult:
	_require(HAZARD_ANALYSIS)
	values = {
		"food_safety_plan": as_str(args, "food_safety_plan") or as_str(args, "plan"),
		"process_step": as_str(args, "process_step"),
		"cte_type": as_str(args, "cte_type"),
		"description": as_str(args, "description"),
		"hazard_type": as_str(args, "hazard_type"),
		"hazard_name": as_str(args, "hazard_name"),
		"hazard_description": as_str(args, "hazard_description"),
		"likelihood": as_str(args, "likelihood"),
		"severity": as_str(args, "severity"),
		"is_preventable": as_bool(args, "is_preventable"),
		"potential_sources": as_str(args, "potential_sources"),
		"conditions_for_hazard": as_str(args, "conditions_for_hazard"),
		"company": _company(args),
		"notes": as_str(args, "notes"),
	}
	result = _create(HAZARD_ANALYSIS, values)
	return ToolResult(data=result)


def update_hazard_analysis(args: dict) -> ToolResult:
	"""Revise one hazard row. Unlike a Monitoring or Verification Record — an
	observation with a time on it, which is appended to rather than edited — a
	hazard analysis is a JUDGEMENT, and a judgement is what a plan review
	changes. `farm_app` let one be edited; refusing to here would make a
	mistyped likelihood permanent."""
	_require(HAZARD_ANALYSIS)
	name = as_str(args, "hazard_analysis") or as_str(args, "hazard")
	values = {}
	for key in (
		"process_step",
		"cte_type",
		"description",
		"hazard_type",
		"hazard_name",
		"hazard_description",
		"likelihood",
		"severity",
		"potential_sources",
		"conditions_for_hazard",
		"notes",
	):
		val = as_str(args, key)
		if val:
			values[key] = val
	preventable = as_bool(args, "is_preventable")
	if preventable is not None:
		values["is_preventable"] = preventable
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		values["food_safety_plan"] = plan
	company = _company(args)
	if company:
		values["company"] = company
	if not values:
		raise ToolError("Nothing to update — pass at least one field.")
	result = _update(HAZARD_ANALYSIS, name, values)
	return ToolResult(data=result)


# ── Preventive Control ───────────────────────────────────────────────────────

_CONTROL_FIELDS = (
	"name",
	"food_safety_plan",
	"control_name",
	"description",
	"control_type",
	"is_critical_control_point",
	"is_active",
	"monitoring_parameter",
	"monitoring_frequency",
	"monitoring_method",
	"critical_limit",
	"critical_limit_unit",
	"critical_limit_operator",
	"critical_limit_description",
	"corrective_action_description",
	"corrective_action_responsible",
	"verification_frequency",
	"verification_method",
	"required_training_description",
	"company",
	"notes",
	"creation",
	"owner",
)


def list_preventive_controls(args: dict) -> ToolResult:
	_require(PREVENTIVE_CONTROL)
	filters = {}
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		filters["food_safety_plan"] = plan
	control_type = as_str(args, "control_type")
	if control_type:
		filters["control_type"] = control_type
	ccp_only = as_bool(args, "ccp_only")
	if ccp_only:
		filters["is_critical_control_point"] = 1
	active_only = as_bool(args, "active_only")
	if active_only is not None and active_only:
		filters["is_active"] = 1
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		PREVENTIVE_CONTROL,
		filters=filters,
		fields=compat.existing_fields(PREVENTIVE_CONTROL, _CONTROL_FIELDS),
		order_by="creation desc",
		limit=limit,
	)
	return ToolResult(data={"controls": [dict(r) for r in rows], "count": len(rows)})


def get_preventive_control(args: dict) -> ToolResult:
	_require(PREVENTIVE_CONTROL)
	name = as_str(args, "preventive_control") or as_str(args, "control")
	row = _get_one(PREVENTIVE_CONTROL, name, _CONTROL_FIELDS)
	# Enrich with monitoring record count.
	if compat.doctype_exists(MONITORING_RECORD):
		row["monitoring_record_count"] = frappe.db.count(MONITORING_RECORD, {"preventive_control": name})
	return ToolResult(data=row)


def create_preventive_control(args: dict) -> ToolResult:
	_require(PREVENTIVE_CONTROL)
	values = {
		"food_safety_plan": as_str(args, "food_safety_plan") or as_str(args, "plan"),
		"control_name": as_str(args, "control_name"),
		"description": as_str(args, "description"),
		"control_type": as_str(args, "control_type"),
		"is_critical_control_point": as_bool(args, "is_critical_control_point"),
		"is_active": as_bool(args, "is_active"),
		"monitoring_parameter": as_str(args, "monitoring_parameter"),
		"monitoring_frequency": as_str(args, "monitoring_frequency"),
		"monitoring_method": as_str(args, "monitoring_method"),
		"critical_limit": args.get("critical_limit"),
		"critical_limit_unit": as_str(args, "critical_limit_unit"),
		"critical_limit_operator": as_str(args, "critical_limit_operator"),
		"critical_limit_description": as_str(args, "critical_limit_description"),
		"corrective_action_description": as_str(args, "corrective_action_description"),
		"corrective_action_responsible": as_str(args, "corrective_action_responsible"),
		"verification_frequency": as_str(args, "verification_frequency"),
		"verification_method": as_str(args, "verification_method"),
		"required_training_description": as_str(args, "required_training_description"),
		"company": _company(args),
		"notes": as_str(args, "notes"),
	}
	result = _create(PREVENTIVE_CONTROL, values)
	return ToolResult(data=result)


def update_preventive_control(args: dict) -> ToolResult:
	_require(PREVENTIVE_CONTROL)
	name = as_str(args, "preventive_control") or as_str(args, "control")
	values = {}
	for key in (
		"control_name",
		"description",
		"control_type",
		"monitoring_parameter",
		"monitoring_frequency",
		"monitoring_method",
		"critical_limit_unit",
		"critical_limit_operator",
		"critical_limit_description",
		"corrective_action_description",
		"corrective_action_responsible",
		"verification_frequency",
		"verification_method",
		"required_training_description",
		"notes",
	):
		val = as_str(args, key)
		if val:
			values[key] = val
	for key in ("is_critical_control_point", "is_active"):
		val = as_bool(args, key)
		if val is not None:
			values[key] = val
	if "critical_limit" in args:
		values["critical_limit"] = args["critical_limit"]
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		values["food_safety_plan"] = plan
	company = _company(args)
	if company:
		values["company"] = company
	if not values:
		raise ToolError("Nothing to update — pass at least one field.")
	result = _update(PREVENTIVE_CONTROL, name, values)
	return ToolResult(data=result)


# ── Monitoring Record ────────────────────────────────────────────────────────

_MONITORING_FIELDS = (
	"name",
	"food_safety_plan",
	"preventive_control",
	"monitoring_date",
	"monitoring_time",
	"measured_value",
	"measured_unit",
	"is_within_limit",
	"observation_notes",
	"monitored_by",
	"monitored_by_name",
	"block",
	"planting_season",
	"source_task",
	"company",
	"notes",
	"creation",
	"owner",
)


def list_monitoring_records(args: dict) -> ToolResult:
	_require(MONITORING_RECORD)
	filters = {}
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		filters["food_safety_plan"] = plan
	control = as_str(args, "preventive_control") or as_str(args, "control")
	if control:
		filters["preventive_control"] = control
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date:
		filters["monitoring_date"] = (">=", str(from_date))
	if to_date:
		filters["monitoring_date"] = ("<=", str(to_date))
	if from_date and to_date:
		filters["monitoring_date"] = ("between", [str(from_date), str(to_date)])
	out_of_limit_only = as_bool(args, "out_of_limit_only")
	if out_of_limit_only:
		filters["is_within_limit"] = 0
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		MONITORING_RECORD,
		filters=filters,
		fields=compat.existing_fields(MONITORING_RECORD, _MONITORING_FIELDS),
		order_by="monitoring_date desc, creation desc",
		limit=limit,
	)
	return ToolResult(data={"records": [dict(r) for r in rows], "count": len(rows)})


def get_monitoring_record(args: dict) -> ToolResult:
	_require(MONITORING_RECORD)
	name = as_str(args, "monitoring_record") or as_str(args, "record")
	row = _get_one(MONITORING_RECORD, name, _MONITORING_FIELDS)
	return ToolResult(data=row)


def create_monitoring_record(args: dict) -> ToolResult:
	_require(MONITORING_RECORD)
	values = {
		"food_safety_plan": as_str(args, "food_safety_plan") or as_str(args, "plan"),
		"preventive_control": as_str(args, "preventive_control") or as_str(args, "control"),
		"monitoring_date": as_date(args, "monitoring_date") or frappe.utils.today(),
		"monitoring_time": as_str(args, "monitoring_time"),
		"measured_value": args.get("measured_value"),
		"measured_unit": as_str(args, "measured_unit"),
		"observation_notes": as_str(args, "observation_notes"),
		"monitored_by": as_str(args, "monitored_by"),
		"monitored_by_name": as_str(args, "monitored_by_name"),
		"block": as_str(args, "block"),
		"planting_season": as_str(args, "planting_season"),
		"source_task": as_str(args, "source_task"),
		"company": _company(args),
		"notes": as_str(args, "notes"),
	}
	result = _create(MONITORING_RECORD, values)
	return ToolResult(data=result)


# ── Corrective Action Record ────────────────────────────────────────────────

_CORRECTIVE_FIELDS = (
	"name",
	"food_safety_plan",
	"preventive_control",
	"monitoring_record",
	"status",
	"deviation_date",
	"deviation_description",
	"root_cause",
	"action_taken",
	"action_date",
	"action_taken_by",
	"action_taken_by_name",
	"preventive_measure",
	"preventive_measure_date",
	"affected_product_description",
	"affected_quantity",
	"affected_quantity_unit",
	"product_disposition",
	"recall_determination",
	"recall_initiated",
	"closed_date",
	"closure_notes",
	"company",
	"notes",
	"creation",
	"owner",
)


def list_corrective_action_records(args: dict) -> ToolResult:
	_require(CORRECTIVE_ACTION_RECORD)
	filters = {}
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		filters["food_safety_plan"] = plan
	control = as_str(args, "preventive_control") or as_str(args, "control")
	if control:
		filters["preventive_control"] = control
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date:
		filters["deviation_date"] = (">=", str(from_date))
	if to_date:
		filters["deviation_date"] = ("<=", str(to_date))
	if from_date and to_date:
		filters["deviation_date"] = ("between", [str(from_date), str(to_date)])
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		CORRECTIVE_ACTION_RECORD,
		filters=filters,
		fields=compat.existing_fields(CORRECTIVE_ACTION_RECORD, _CORRECTIVE_FIELDS),
		order_by="deviation_date desc, creation desc",
		limit=limit,
	)
	return ToolResult(data={"records": [dict(r) for r in rows], "count": len(rows)})


def get_corrective_action_record(args: dict) -> ToolResult:
	_require(CORRECTIVE_ACTION_RECORD)
	name = as_str(args, "corrective_action_record") or as_str(args, "record")
	row = _get_one(CORRECTIVE_ACTION_RECORD, name, _CORRECTIVE_FIELDS)
	return ToolResult(data=row)


def create_corrective_action_record(args: dict) -> ToolResult:
	_require(CORRECTIVE_ACTION_RECORD)
	values = {
		"food_safety_plan": as_str(args, "food_safety_plan") or as_str(args, "plan"),
		"preventive_control": as_str(args, "preventive_control") or as_str(args, "control"),
		"monitoring_record": as_str(args, "monitoring_record"),
		"status": as_str(args, "status") or "Open",
		"deviation_date": as_date(args, "deviation_date"),
		"deviation_description": as_str(args, "deviation_description"),
		"root_cause": as_str(args, "root_cause"),
		"action_taken": as_str(args, "action_taken"),
		"action_date": as_date(args, "action_date"),
		"action_taken_by": as_str(args, "action_taken_by"),
		"action_taken_by_name": as_str(args, "action_taken_by_name"),
		"preventive_measure": as_str(args, "preventive_measure"),
		"preventive_measure_date": as_date(args, "preventive_measure_date"),
		"affected_product_description": as_str(args, "affected_product_description"),
		"affected_quantity": args.get("affected_quantity"),
		"affected_quantity_unit": as_str(args, "affected_quantity_unit"),
		"product_disposition": as_str(args, "product_disposition"),
		"recall_determination": as_str(args, "recall_determination"),
		"recall_initiated": as_bool(args, "recall_initiated"),
		"company": _company(args),
		"notes": as_str(args, "notes"),
	}
	result = _create(CORRECTIVE_ACTION_RECORD, values)
	return ToolResult(data=result)


def update_corrective_action_record(args: dict) -> ToolResult:
	_require(CORRECTIVE_ACTION_RECORD)
	name = as_str(args, "corrective_action_record") or as_str(args, "record")
	values = {}
	for key in (
		"status",
		"deviation_description",
		"root_cause",
		"action_taken",
		"action_taken_by",
		"action_taken_by_name",
		"preventive_measure",
		"affected_product_description",
		"affected_quantity_unit",
		"product_disposition",
		"recall_determination",
		"closure_notes",
		"notes",
	):
		val = as_str(args, key)
		if val:
			values[key] = val
	for key in ("deviation_date", "action_date", "preventive_measure_date", "closed_date"):
		val = as_date(args, key)
		if val is not None:
			values[key] = val
	if "affected_quantity" in args:
		values["affected_quantity"] = args["affected_quantity"]
	recall = as_bool(args, "recall_initiated")
	if recall is not None:
		values["recall_initiated"] = recall
	company = _company(args)
	if company:
		values["company"] = company
	if not values:
		raise ToolError("Nothing to update — pass at least one field.")
	result = _update(CORRECTIVE_ACTION_RECORD, name, values)
	return ToolResult(data=result)


# ── Verification Record ─────────────────────────────────────────────────────

_VERIFICATION_FIELDS = (
	"name",
	"food_safety_plan",
	"preventive_control",
	"verification_type",
	"description",
	"verification_date",
	"verification_time",
	"equipment_name",
	"equipment_calibrated_date",
	"equipment_calibration_due_date",
	"calibration_status",
	"result_summary",
	"is_control_effective",
	"findings",
	"corrective_actions_triggered",
	"verified_by",
	"verified_by_name",
	"company",
	"notes",
	"creation",
	"owner",
)


def list_verification_records(args: dict) -> ToolResult:
	_require(VERIFICATION_RECORD)
	filters = {}
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		filters["food_safety_plan"] = plan
	control = as_str(args, "preventive_control") or as_str(args, "control")
	if control:
		filters["preventive_control"] = control
	vtype = as_str(args, "verification_type")
	if vtype:
		filters["verification_type"] = vtype
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date:
		filters["verification_date"] = (">=", str(from_date))
	if to_date:
		filters["verification_date"] = ("<=", str(to_date))
	if from_date and to_date:
		filters["verification_date"] = ("between", [str(from_date), str(to_date)])
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		VERIFICATION_RECORD,
		filters=filters,
		fields=compat.existing_fields(VERIFICATION_RECORD, _VERIFICATION_FIELDS),
		order_by="verification_date desc, creation desc",
		limit=limit,
	)
	return ToolResult(data={"records": [dict(r) for r in rows], "count": len(rows)})


def get_verification_record(args: dict) -> ToolResult:
	_require(VERIFICATION_RECORD)
	name = as_str(args, "verification_record") or as_str(args, "record")
	row = _get_one(VERIFICATION_RECORD, name, _VERIFICATION_FIELDS)
	return ToolResult(data=row)


def create_verification_record(args: dict) -> ToolResult:
	_require(VERIFICATION_RECORD)
	values = {
		"food_safety_plan": as_str(args, "food_safety_plan") or as_str(args, "plan"),
		"preventive_control": as_str(args, "preventive_control") or as_str(args, "control"),
		"verification_type": as_str(args, "verification_type"),
		"description": as_str(args, "description"),
		"verification_date": as_date(args, "verification_date") or frappe.utils.today(),
		"verification_time": as_str(args, "verification_time"),
		"equipment_name": as_str(args, "equipment_name"),
		"equipment_calibrated_date": as_date(args, "equipment_calibrated_date"),
		"equipment_calibration_due_date": as_date(args, "equipment_calibration_due_date"),
		"calibration_status": as_str(args, "calibration_status"),
		"result_summary": as_str(args, "result_summary"),
		"is_control_effective": as_bool(args, "is_control_effective"),
		"findings": as_str(args, "findings"),
		"corrective_actions_triggered": as_bool(args, "corrective_actions_triggered"),
		"verified_by": as_str(args, "verified_by"),
		"verified_by_name": as_str(args, "verified_by_name"),
		"company": _company(args),
		"notes": as_str(args, "notes"),
	}
	result = _create(VERIFICATION_RECORD, values)
	return ToolResult(data=result)


# ── Recall Plan ──────────────────────────────────────────────────────────────

_RECALL_FIELDS = (
	"name",
	"food_safety_plan",
	"recall_plan_name",
	"description",
	"is_active",
	"recall_coordinator",
	"recall_coordinator_name",
	"recall_coordinator_backup",
	"recall_coordinator_backup_name",
	"recall_team_contacts",
	"customers_list",
	"product_identification",
	"fda_notification_required",
	"fda_notification_procedure",
	"last_simulation_date",
	"next_simulation_date",
	"company",
	"notes",
	"creation",
	"owner",
)


def list_recall_plans(args: dict) -> ToolResult:
	_require(RECALL_PLAN)
	filters = {}
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		filters["food_safety_plan"] = plan
	active_only = as_bool(args, "active_only")
	if active_only:
		filters["is_active"] = 1
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		RECALL_PLAN,
		filters=filters,
		fields=compat.existing_fields(RECALL_PLAN, _RECALL_FIELDS),
		order_by="creation desc",
		limit=limit,
	)
	return ToolResult(data={"recall_plans": [dict(r) for r in rows], "count": len(rows)})


def get_recall_plan(args: dict) -> ToolResult:
	_require(RECALL_PLAN)
	name = as_str(args, "recall_plan") or as_str(args, "plan_name")
	row = _get_one(RECALL_PLAN, name, _RECALL_FIELDS)
	return ToolResult(data=row)


def create_recall_plan(args: dict) -> ToolResult:
	_require(RECALL_PLAN)
	values = {
		"food_safety_plan": as_str(args, "food_safety_plan"),
		"recall_plan_name": as_str(args, "recall_plan_name"),
		"description": as_str(args, "description"),
		"is_active": as_bool(args, "is_active"),
		"recall_coordinator": as_str(args, "recall_coordinator"),
		"recall_coordinator_name": as_str(args, "recall_coordinator_name"),
		"recall_coordinator_backup": as_str(args, "recall_coordinator_backup"),
		"recall_coordinator_backup_name": as_str(args, "recall_coordinator_backup_name"),
		"recall_team_contacts": _json_field(args, "recall_team_contacts"),
		"customers_list": _json_field(args, "customers_list"),
		"product_identification": as_str(args, "product_identification"),
		"fda_notification_required": as_bool(args, "fda_notification_required"),
		"fda_notification_procedure": as_str(args, "fda_notification_procedure"),
		"last_simulation_date": as_date(args, "last_simulation_date"),
		"next_simulation_date": as_date(args, "next_simulation_date"),
		"company": _company(args),
		"notes": as_str(args, "notes"),
	}
	result = _create(RECALL_PLAN, values)
	return ToolResult(data=result)


def update_recall_plan(args: dict) -> ToolResult:
	_require(RECALL_PLAN)
	name = as_str(args, "recall_plan") or as_str(args, "plan_name")
	values = {}
	for key in (
		"recall_plan_name",
		"description",
		"recall_coordinator",
		"recall_coordinator_name",
		"recall_coordinator_backup",
		"recall_coordinator_backup_name",
		"product_identification",
		"fda_notification_procedure",
		"notes",
	):
		val = as_str(args, key)
		if val:
			values[key] = val
	for key in ("is_active", "fda_notification_required"):
		val = as_bool(args, key)
		if val is not None:
			values[key] = val
	for key in ("last_simulation_date", "next_simulation_date"):
		val = as_date(args, key)
		if val is not None:
			values[key] = val
	for json_key in ("recall_team_contacts", "customers_list"):
		val = _json_field(args, json_key)
		if val is not None:
			values[json_key] = val
	plan = as_str(args, "food_safety_plan")
	if plan:
		values["food_safety_plan"] = plan
	company = _company(args)
	if company:
		values["company"] = company
	if not values:
		raise ToolError("Nothing to update — pass at least one field.")
	result = _update(RECALL_PLAN, name, values)
	return ToolResult(data=result)


# ── Supplier Verification ───────────────────────────────────────────────────

_SUPPLIER_FIELDS = (
	"name",
	"food_safety_plan",
	"supplier_name",
	"supplier_gln",
	"supplier_address",
	"supplier_contact_name",
	"supplier_contact_phone",
	"supplier_contact_email",
	"product_supplied",
	"product_description",
	"hazards_controlled_by_supplier",
	"verification_method",
	"verification_date",
	"verification_result",
	"verification_notes",
	"certificate_type",
	"certificate_expiry_date",
	"next_verification_date",
	"company",
	"notes",
	"creation",
	"owner",
)


def list_supplier_verifications(args: dict) -> ToolResult:
	_require(SUPPLIER_VERIFICATION)
	filters = {}
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		filters["food_safety_plan"] = plan
	result_filter = as_str(args, "verification_result")
	if result_filter:
		filters["verification_result"] = result_filter
	method = as_str(args, "verification_method")
	if method:
		filters["verification_method"] = method
	company = _company(args)
	if company:
		filters["company"] = company
	limit = as_limit(args)
	rows = frappe.db.get_all(
		SUPPLIER_VERIFICATION,
		filters=filters,
		fields=compat.existing_fields(SUPPLIER_VERIFICATION, _SUPPLIER_FIELDS),
		order_by="verification_date desc, creation desc",
		limit=limit,
	)
	return ToolResult(data={"verifications": [dict(r) for r in rows], "count": len(rows)})


def get_supplier_verification(args: dict) -> ToolResult:
	_require(SUPPLIER_VERIFICATION)
	name = as_str(args, "supplier_verification") or as_str(args, "verification")
	row = _get_one(SUPPLIER_VERIFICATION, name, _SUPPLIER_FIELDS)
	return ToolResult(data=row)


def create_supplier_verification(args: dict) -> ToolResult:
	_require(SUPPLIER_VERIFICATION)
	values = {
		"food_safety_plan": as_str(args, "food_safety_plan") or as_str(args, "plan"),
		"supplier_name": as_str(args, "supplier_name"),
		"supplier_gln": as_str(args, "supplier_gln"),
		"supplier_address": as_str(args, "supplier_address"),
		"supplier_contact_name": as_str(args, "supplier_contact_name"),
		"supplier_contact_phone": as_str(args, "supplier_contact_phone"),
		"supplier_contact_email": as_str(args, "supplier_contact_email"),
		"product_supplied": as_str(args, "product_supplied"),
		"product_description": as_str(args, "product_description"),
		"hazards_controlled_by_supplier": _json_field(args, "hazards_controlled_by_supplier"),
		"verification_method": as_str(args, "verification_method"),
		"verification_date": as_date(args, "verification_date"),
		"verification_result": as_str(args, "verification_result"),
		"verification_notes": as_str(args, "verification_notes"),
		"certificate_type": as_str(args, "certificate_type"),
		"certificate_expiry_date": as_date(args, "certificate_expiry_date"),
		"next_verification_date": as_date(args, "next_verification_date"),
		"company": _company(args),
		"notes": as_str(args, "notes"),
	}
	result = _create(SUPPLIER_VERIFICATION, values)
	return ToolResult(data=result)


def update_supplier_verification(args: dict) -> ToolResult:
	_require(SUPPLIER_VERIFICATION)
	name = as_str(args, "supplier_verification") or as_str(args, "verification")
	values = {}
	for key in (
		"supplier_name",
		"supplier_gln",
		"supplier_address",
		"supplier_contact_name",
		"supplier_contact_phone",
		"supplier_contact_email",
		"product_supplied",
		"product_description",
		"verification_method",
		"verification_result",
		"verification_notes",
		"certificate_type",
		"notes",
	):
		val = as_str(args, key)
		if val:
			values[key] = val
	for key in ("verification_date", "certificate_expiry_date", "next_verification_date"):
		val = as_date(args, key)
		if val is not None:
			values[key] = val
	hazards = _json_field(args, "hazards_controlled_by_supplier")
	if hazards is not None:
		values["hazards_controlled_by_supplier"] = hazards
	plan = as_str(args, "food_safety_plan") or as_str(args, "plan")
	if plan:
		values["food_safety_plan"] = plan
	company = _company(args)
	if company:
		values["company"] = company
	if not values:
		raise ToolError("Nothing to update — pass at least one field.")
	result = _update(SUPPLIER_VERIFICATION, name, values)
	return ToolResult(data=result)


# ── Dashboard ────────────────────────────────────────────────────────────────


def get_food_safety_dashboard(args: dict) -> ToolResult:
	"""Summary of every food safety plan on the site, designed for the question
	'are we audit-ready': QI status, control counts, open corrective actions,
	recall plan currency, supplier certificate expiry."""
	_require(FOOD_SAFETY_PLAN)
	company = _company(args)
	filters = {}
	if company:
		filters["company"] = company

	plans = frappe.db.get_all(
		FOOD_SAFETY_PLAN,
		filters=filters,
		fields=compat.existing_fields(
			FOOD_SAFETY_PLAN,
			(
				"name",
				"plan_name",
				"facility_name",
				"status",
				"qualified_individual_name",
				"qi_certification_expiry",
				"next_review_date",
			),
		),
		order_by="creation desc",
		limit=RECORD_CAP,
	)

	today = frappe.utils.today()
	summary = []
	total_open_cas = 0

	for plan in plans:
		plan_name = plan.get("name")
		row = dict(plan)

		# QI status.
		qi_expiry = str(plan.get("qi_certification_expiry") or "")
		row["qi_current"] = bool(qi_expiry and qi_expiry >= today)

		# Review status.
		next_review = str(plan.get("next_review_date") or "")
		row["review_overdue"] = bool(next_review and next_review < today)

		# Child counts.
		for child_dt, key in (
			(HAZARD_ANALYSIS, "hazard_count"),
			(PREVENTIVE_CONTROL, "control_count"),
			(MONITORING_RECORD, "monitoring_count"),
			(VERIFICATION_RECORD, "verification_count"),
			(SUPPLIER_VERIFICATION, "supplier_count"),
		):
			if compat.doctype_exists(child_dt):
				row[key] = frappe.db.count(child_dt, {"food_safety_plan": plan_name})
			else:
				row[key] = 0

		# Open corrective actions.
		if compat.doctype_exists(CORRECTIVE_ACTION_RECORD):
			open_cas = frappe.db.count(
				CORRECTIVE_ACTION_RECORD,
				{"food_safety_plan": plan_name, "status": "Open"},
			)
			row["open_corrective_actions"] = open_cas
			total_open_cas += open_cas

		# Recall plan.
		if compat.doctype_exists(RECALL_PLAN):
			recall = frappe.db.get_all(
				RECALL_PLAN,
				filters={"food_safety_plan": plan_name, "is_active": 1},
				fields=["name", "recall_plan_name", "last_simulation_date", "next_simulation_date"],
				limit=5,
			)
			row["active_recall_plans"] = len(recall)
			if recall:
				last_sim = max(str(r.get("last_simulation_date") or "") for r in recall)
				row["last_recall_simulation"] = last_sim or None
		else:
			row["active_recall_plans"] = 0

		# Expired supplier certs.
		if compat.doctype_exists(SUPPLIER_VERIFICATION):
			expired = frappe.db.count(
				SUPPLIER_VERIFICATION,
				{
					"food_safety_plan": plan_name,
					"certificate_expiry_date": ("<", today),
				},
			)
			row["expired_supplier_certificates"] = expired

		summary.append(row)

	return ToolResult(
		data={
			"plans": summary,
			"total_plans": len(summary),
			"total_open_corrective_actions": total_open_cas,
		}
	)
