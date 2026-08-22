# SPDX-License-Identifier: MIT
"""Strategic plans and the objectives that say whether they worked.

A PLAN AND ITS OBJECTIVES ARE TWO REGISTERS BECAUSE THEY MOVE AT TWO SPEEDS. The
plan is written once and superseded; the objectives under it are touched every
time somebody records a quarter's actual. Held as a child table, every one of
those routine updates would be a write to the strategy document — re-versioning
it, re-stamping it, and putting a numbers entry in the same audit trail as a
change of direction. Held apart, "show me everything overdue across every plan"
is one query instead of a walk through every parent.

SUPERSESSION IS THE WHOLE POINT OF `create_strategic_plan` TAKING A
`previous_version`. A strategy that is edited in place loses the thing that makes
strategic history worth keeping: what the operation used to believe. So a new
plan points back, the controller versions it and retires its predecessor, and the
old wording stays exactly as it was written.

NOTHING HERE GENERATES A PLAN. The farm_app had a tool that asked a model to
write one; that prompt is preserved verbatim in
`prompt_templates.PROMPTS["strategic_plan_draft"]` and is deliberately not wired
to a writer. A generated strategy that lands in the register without somebody
having agreed to it is a document the farm is measured against and nobody chose.
The prompt is available; committing its output is a human act.
"""

import frappe

from .. import compat
from ..args import as_choice, as_date, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.strategic_objective.strategic_objective import SETTLED
from ..erpnext_mcp.doctype.strategic_plan.strategic_plan import HISTORICAL
from ..errors import ToolError
from ..result import ToolResult

PLAN = "Strategic Plan"
OBJECTIVE = "Strategic Objective"

REGISTER_CAP = 500

_PLAN_FIELDS = (
	"name",
	"company",
	"plan_name",
	"crop",
	"status",
	"timeframe",
	"version",
	"previous_version",
	"effective_date",
	"retired_date",
	"description",
	"vision",
	"mission",
	"values_text",
	"swot",
	"porters_five_forces",
	"sustainable_advantage",
	"analogous_games",
	"grand_strategy",
	"business_strategy",
	"command_structure",
	"functional_tactics",
	"validation_control",
	"exit_strategy",
	"notes",
)

_OBJECTIVE_FIELDS = (
	"name",
	"company",
	"strategic_plan",
	"objective",
	"status",
	"due_date",
	"owner_role",
	"kpi_metric",
	"kpi_target",
	"kpi_actual",
	"measured_on",
	"notes",
)

#: The plan sections that carry the actual thinking. Used to report how much of a
#: plan is filled in — a plan with a vision and nothing else is a common and
#: honest state, and the register should say so rather than presenting it as
#: complete.
SUBSTANCE_FIELDS = (
	"vision",
	"mission",
	"swot",
	"porters_five_forces",
	"grand_strategy",
	"business_strategy",
	"sustainable_advantage",
	"command_structure",
	"functional_tactics",
	"validation_control",
	"exit_strategy",
)


#: The blank values. `0` is deliberately not among them — see `_same`.
_BLANK = (None, "")


def _same(before, after) -> bool:
	"""Whether staging `after` over `before` would change nothing.

	NOT `str(before or "") == str(after or "")`, which is the obvious spelling and
	SILENTLY DROPS ZERO: `0 or ""` is `""`, so setting a value of 0 on an empty
	column stages nothing, the write is lost, and the only symptom is a required
	field the caller believes they supplied.

	Zero is a real value in every numeric column these tools write — a non-detect
	residue limit, a cultural fit score of nothing, a battery reading of 0% — so
	blank and zero have to be told apart here rather than collapsed.
	"""
	if before in _BLANK and after in _BLANK:
		return True
	if before in _BLANK or after in _BLANK:
		return False
	return str(before) == str(after)


def _require(doctype: str) -> None:
	if not compat.doctype_exists(doctype):
		raise ToolError(f"{doctype} is not available on this site — run `bench migrate` to install it.")


def _date(value):
	return str(value) if value else None


def _describe_plan(row: dict) -> dict:
	described = {
		key: row.get(key) or None
		for key in _PLAN_FIELDS
		if key not in ("version", "effective_date", "retired_date")
	}
	described["version"] = int(row.get("version") or 1)
	described["effective_date"] = _date(row.get("effective_date"))
	described["retired_date"] = _date(row.get("retired_date"))
	filled = [key for key in SUBSTANCE_FIELDS if str(row.get(key) or "").strip()]
	described["sections_filled"] = filled
	described["sections_empty"] = [key for key in SUBSTANCE_FIELDS if key not in filled]
	described["completeness"] = round(len(filled) / len(SUBSTANCE_FIELDS), 2)
	return described


def _describe_objective(row: dict) -> dict:
	due = _date(row.get("due_date"))
	status = row.get("status")
	return {
		"name": row.get("name"),
		"company": row.get("company"),
		"strategic_plan": row.get("strategic_plan"),
		"objective": row.get("objective"),
		"status": status,
		"due_date": due,
		# Overdue means "past its date and still open". A Failed objective past
		# its date is not overdue, it is settled — and counting it as overdue
		# would keep it on the list for ever.
		"overdue": bool(due and due < frappe.utils.today() and status not in SETTLED),
		"owner_role": row.get("owner_role") or None,
		"kpi_metric": row.get("kpi_metric") or None,
		"kpi_target": row.get("kpi_target") or None,
		"kpi_actual": row.get("kpi_actual") or None,
		"measured_on": _date(row.get("measured_on")),
		"notes": row.get("notes") or None,
	}


def _write_plan(doc, args: dict, creating: bool) -> dict:
	changed = {}

	def stage(key, value):
		before = doc.get(key)
		if not _same(before, value):
			changed[key] = [before, value]
			doc.set(key, value)

	if creating or "plan_name" in args:
		stage("plan_name", as_str(args, "plan_name", required=creating))
	if "crop" in args:
		crop = as_str(args, "crop")
		if crop and not frappe.db.exists("Crop", crop):
			raise ToolError(f"Crop {crop!r} does not exist.")
		stage("crop", crop)
	if "status" in args:
		value = as_str(args, "status")
		stage("status", as_choice(PLAN, "status", value, "status") if value else "")
	if "previous_version" in args:
		previous = as_str(args, "previous_version")
		if previous and not frappe.db.exists(PLAN, previous):
			raise ToolError(f"Strategic Plan {previous!r} does not exist.")
		stage("previous_version", previous)
	for key in (
		"timeframe",
		"description",
		"vision",
		"mission",
		"values_text",
		"swot",
		"porters_five_forces",
		"sustainable_advantage",
		"analogous_games",
		"grand_strategy",
		"business_strategy",
		"command_structure",
		"functional_tactics",
		"validation_control",
		"exit_strategy",
		"notes",
	):
		if key in args:
			stage(key, as_str(args, key))
	for key in ("effective_date", "retired_date"):
		if key in args:
			stage(key, as_date(args, key) or "")
	if "version" in args:
		raise ToolError(
			"version is derived from previous_version on every save and cannot be set. A number "
			"somebody types is one that will eventually be typed twice, and two plans both "
			"calling themselves v3 make the chain unorderable. Nothing was changed."
		)
	return changed


def create_strategic_plan(args: dict) -> ToolResult:
	"""Write a plan. Naming a predecessor versions this one and retires that one."""
	_require(PLAN)
	doc = frappe.new_doc(PLAN)
	doc.company = resolve_company(as_str(args, "company"), required=True)
	_write_plan(doc, args, creating=True)
	doc.insert(ignore_permissions=True)

	described = _describe_plan(dict(doc.as_dict()))
	data = {**described}
	if doc.previous_version:
		data["superseded"] = doc.previous_version
		data["next_step"] = (
			f"{doc.previous_version} is now Historical and carries a retired date. Its wording is "
			"unchanged — that is the point of superseding rather than editing."
		)
	if described["sections_empty"]:
		data.setdefault("next_step", "")
		data["sections_note"] = (
			f"{len(described['sections_empty'])} of {len(SUBSTANCE_FIELDS)} sections are empty. "
			"That is an ordinary state for a plan in Developing; exit_strategy and "
			"validation_control are the two most often left blank and the two most expensive to."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {described['plan_name']} v{described['version']} ({described['status']})",
		docstatus_delta="none → 0 (created)",
	)


def get_strategic_plan(args: dict) -> ToolResult:
	"""One plan in full, with its objectives and the chain it sits in."""
	_require(PLAN)
	name = as_str(args, "strategic_plan", required=True)
	row = frappe.db.get_value(PLAN, name, compat.existing_fields(PLAN, _PLAN_FIELDS), as_dict=True)
	if not row:
		raise ToolError(f"Strategic Plan {name!r} does not exist.")
	described = _describe_plan(dict(row))

	objectives = []
	if compat.doctype_exists(OBJECTIVE):
		objectives = [
			_describe_objective(dict(objective))
			for objective in frappe.db.get_all(
				OBJECTIVE,
				filters={"strategic_plan": described["name"]},
				fields=compat.existing_fields(OBJECTIVE, _OBJECTIVE_FIELDS),
				order_by="due_date asc, objective asc",
				limit=REGISTER_CAP,
			)
		]

	successor = frappe.db.get_value(PLAN, {"previous_version": described["name"]}, "name")
	by_status: dict = {}
	for objective in objectives:
		by_status[objective["status"]] = by_status.get(objective["status"], 0) + 1

	return ToolResult(
		data={
			**described,
			"superseded_by": successor or None,
			"objective_count": len(objectives),
			"objectives_by_status": dict(sorted(by_status.items())),
			"objectives_overdue": [row["name"] for row in objectives if row["overdue"]],
			"objectives_unmeasured": [
				row["name"] for row in objectives if row["kpi_target"] and not row["kpi_actual"]
			],
			"objectives": objectives,
		},
		summary=(
			f"{described['plan_name']} v{described['version']}: {described['status']}, "
			f"{len(objectives)} objective(s)"
		),
	)


def list_strategic_plans(args: dict) -> ToolResult:
	"""The plan register, newest version first, with the live ones named."""
	_require(PLAN)
	company = resolve_company(as_str(args, "company"))
	limit = as_limit(args)

	filters = {}
	if company:
		filters["company"] = company
	status = as_str(args, "status")
	if status:
		filters["status"] = as_choice(PLAN, "status", status, "status")
	crop = as_str(args, "crop")
	if crop:
		filters["crop"] = crop

	rows = frappe.db.get_all(
		PLAN,
		filters=filters,
		fields=compat.existing_fields(PLAN, _PLAN_FIELDS),
		order_by="version desc, plan_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	plans = [_describe_plan(dict(row)) for row in rows]

	by_status: dict = {}
	for row in plans:
		by_status[row["status"]] = by_status.get(row["status"], 0) + 1

	return ToolResult(
		data={
			"company": company,
			"plan_count": len(plans),
			"by_status": dict(sorted(by_status.items())),
			"live": [row["name"] for row in plans if row["status"] != HISTORICAL],
			# A Historical plan with no retired date is a break in the chain: it
			# says the plan stopped applying and not when, so nothing can be dated
			# against it.
			"historical_without_retired_date": [
				row["name"] for row in plans if row["status"] == HISTORICAL and not row["retired_date"]
			],
			"plans": plans,
		},
		summary=f"{len(plans)} plan(s)",
	)


def update_strategic_plan(args: dict) -> ToolResult:
	"""Revise a plan in place. To change direction, write a new one instead."""
	_require(PLAN)
	name = as_str(args, "strategic_plan", required=True)
	if not frappe.db.exists(PLAN, name):
		raise ToolError(f"Strategic Plan {name!r} does not exist. Nothing was changed.")

	doc = frappe.get_doc(PLAN, name)
	changed = _write_plan(doc, args, creating=False)
	if not changed:
		raise ToolError(
			"nothing to change. Pass at least one of: plan_name, crop, status, timeframe, "
			"previous_version, effective_date, retired_date, description, vision, mission, "
			"values_text, swot, porters_five_forces, sustainable_advantage, analogous_games, "
			"grand_strategy, business_strategy, command_structure, functional_tactics, "
			"validation_control, exit_strategy, notes."
		)
	doc.save(ignore_permissions=True)
	described = _describe_plan(dict(doc.as_dict()))
	data = {**described, "changed": changed}
	if set(changed) & set(SUBSTANCE_FIELDS):
		data["note"] = (
			"An analysis section was edited in place. That is right for filling a gap or fixing "
			"wording; if the operation has CHANGED ITS MIND, write a new plan naming this one as "
			"previous_version instead — an edited plan loses what it used to say, which is the "
			"question strategic history exists to answer."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {len(changed)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ══════════════════════════════════════════════════════════════════════════
# OBJECTIVES
# ══════════════════════════════════════════════════════════════════════════


def _write_objective(doc, args: dict, creating: bool) -> dict:
	changed = {}

	def stage(key, value):
		before = doc.get(key)
		if not _same(before, value):
			changed[key] = [before, value]
			doc.set(key, value)

	if creating or "strategic_plan" in args:
		plan = as_str(args, "strategic_plan", required=creating)
		if plan and not frappe.db.exists(PLAN, plan):
			raise ToolError(f"Strategic Plan {plan!r} does not exist.")
		stage("strategic_plan", plan)
	if creating or "objective" in args:
		stage("objective", as_str(args, "objective", required=creating))
	if "status" in args:
		value = as_str(args, "status")
		stage("status", as_choice(OBJECTIVE, "status", value, "status") if value else "")
	for key in ("owner_role", "kpi_metric", "kpi_target", "kpi_actual", "notes"):
		if key in args:
			stage(key, as_str(args, key))
	for key in ("due_date", "measured_on"):
		if key in args:
			stage(key, as_date(args, key) or "")
	return changed


def create_strategic_objective(args: dict) -> ToolResult:
	"""Add a measurable promise to a plan."""
	_require(OBJECTIVE)
	_require(PLAN)
	doc = frappe.new_doc(OBJECTIVE)

	plan = as_str(args, "strategic_plan", required=True)
	plan_company = frappe.db.get_value(PLAN, plan, "company")
	if not plan_company:
		raise ToolError(f"Strategic Plan {plan!r} does not exist. Nothing was created.")
	doc.company = resolve_company(as_str(args, "company")) or plan_company

	# An actual with no measurement date is refused by the controller. Where a
	# caller supplied one and no date, today is the honest default — they have
	# the number in front of them now.
	if as_str(args, "kpi_actual") and not as_date(args, "measured_on"):
		doc.measured_on = frappe.utils.today()

	_write_objective(doc, args, creating=True)
	doc.insert(ignore_permissions=True)

	described = _describe_objective(dict(doc.as_dict()))
	data = {**described}
	if not described["kpi_target"]:
		data["next_step"] = (
			"This objective has no target. An objective with nothing to measure against cannot "
			"be reported as achieved or failed, only as done — which is what makes a plan look "
			"like it worked."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {described['objective'][:60]}",
		docstatus_delta="none → 0 (created)",
	)


def get_strategic_objective(args: dict) -> ToolResult:
	"""One objective, with the plan it belongs to."""
	_require(OBJECTIVE)
	name = as_str(args, "strategic_objective", required=True)
	row = frappe.db.get_value(
		OBJECTIVE, name, compat.existing_fields(OBJECTIVE, _OBJECTIVE_FIELDS), as_dict=True
	)
	if not row:
		raise ToolError(f"Strategic Objective {name!r} does not exist.")
	described = _describe_objective(dict(row))

	plan = {}
	if compat.doctype_exists(PLAN) and described["strategic_plan"]:
		plan = dict(
			frappe.db.get_value(
				PLAN,
				described["strategic_plan"],
				["name", "plan_name", "status", "version", "crop"],
				as_dict=True,
			)
			or {}
		)
	return ToolResult(
		data={**described, "plan_detail": plan or None},
		summary=f"{described['name']}: {described['status']}",
	)


def list_strategic_objectives(args: dict) -> ToolResult:
	"""Objectives across every plan, or one, with the overdue ones named."""
	_require(OBJECTIVE)
	company = resolve_company(as_str(args, "company"))
	limit = as_limit(args)

	filters = {}
	if company:
		filters["company"] = company
	plan = as_str(args, "strategic_plan")
	if plan:
		filters["strategic_plan"] = plan
	status = as_str(args, "status")
	if status:
		filters["status"] = as_choice(OBJECTIVE, "status", status, "status")
	from_date = as_date(args, "due_from")
	to_date = as_date(args, "due_to")
	if from_date and to_date:
		filters["due_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["due_date"] = (">=", from_date)
	elif to_date:
		filters["due_date"] = ("<=", to_date)

	rows = frappe.db.get_all(
		OBJECTIVE,
		filters=filters,
		fields=compat.existing_fields(OBJECTIVE, _OBJECTIVE_FIELDS),
		order_by="due_date asc, objective asc",
		limit=min(limit, REGISTER_CAP),
	)
	objectives = [_describe_objective(dict(row)) for row in rows]

	by_status: dict = {}
	for row in objectives:
		by_status[row["status"]] = by_status.get(row["status"], 0) + 1
	settled = [row for row in objectives if row["status"] in SETTLED]
	achieved = [row for row in settled if row["status"] == "Achieved"]

	return ToolResult(
		data={
			"company": company,
			"strategic_plan": plan or None,
			"objective_count": len(objectives),
			"by_status": dict(sorted(by_status.items())),
			"overdue": [row["name"] for row in objectives if row["overdue"]],
			"unmeasured": [row["name"] for row in objectives if row["kpi_target"] and not row["kpi_actual"]],
			"without_target": [row["name"] for row in objectives if not row["kpi_target"]],
			# Of the ones somebody actually finished with — which is the only
			# population this rate means anything over. A hit rate computed across
			# objectives still in progress flatters every plan on day one.
			"settled_count": len(settled),
			"achieved_rate": round(len(achieved) / len(settled), 2) if settled else None,
			"objectives": objectives,
		},
		summary=f"{len(objectives)} objective(s), {sum(1 for row in objectives if row['overdue'])} overdue",
	)


def update_strategic_objective(args: dict) -> ToolResult:
	"""Record progress against an objective, or revise it."""
	_require(OBJECTIVE)
	name = as_str(args, "strategic_objective", required=True)
	if not frappe.db.exists(OBJECTIVE, name):
		raise ToolError(f"Strategic Objective {name!r} does not exist. Nothing was changed.")

	doc = frappe.get_doc(OBJECTIVE, name)
	if "kpi_actual" in args and "measured_on" not in args and as_str(args, "kpi_actual"):
		doc.measured_on = frappe.utils.today()
	changed = _write_objective(doc, args, creating=False)
	if not changed:
		raise ToolError(
			"nothing to change. Pass at least one of: strategic_plan, objective, status, "
			"due_date, owner_role, kpi_metric, kpi_target, kpi_actual, measured_on, notes."
		)
	doc.save(ignore_permissions=True)
	return ToolResult(
		data={**_describe_objective(dict(doc.as_dict())), "changed": changed},
		summary=f"{doc.name}: {len(changed)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)
