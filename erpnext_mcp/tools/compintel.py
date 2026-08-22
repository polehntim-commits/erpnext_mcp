# SPDX-License-Identifier: MIT
"""Competitive intelligence: who else is in this market, what they did, and what to buy.

THREE REGISTERS AND ONE ARGUMENT FOR WHY THEY ARE THREE. A participant is a
standing fact about an organisation. A move is a dated event. A target is a
decision in progress with money attached. They change on completely different
cadences — a participant record is revised yearly, a move is written once and
never edited, a target is touched weekly while a deal is live — and collapsing
any two of them means the slow one is rewritten every time the fast one moves.

EVERYTHING HERE IS SOMEBODY'S ESTIMATE AND THE TOOLS SAY SO RATHER THAN
PRETENDING. A competitor's revenue, acreage and market share are reads of a
private business. The tools report them, report that they are estimates, and
never combine them with figures from the ledger — the moment an estimated
competitor revenue is divided by a real one, the result looks like a market share
and is not.

WHAT THIS MODULE WILL NOT DO IS DECIDE. There is no tool here that scores a
target for you, ranks a pipeline into an order to act on, or declares a landscape
assessment. `assess` and `rank` were the farm_app's own framing and they are the
part deliberately not ported: the four fit scores are judgements a person makes,
`accretive_score` is arithmetic on them the controller does in one place, and
what a landscape MEANS is the thing the operator is being paid to work out. What
the tools do instead is put the evidence in front of them — the weakest
dimension named beside the composite, the unanswered moves listed beside the
count. See `erpnext_mcp/erpnext_mcp/prompt_templates.PROMPTS["competitive_landscape"]`
for the prompt the farm_app used, kept as data for a caller that wants it.
"""

import frappe

from .. import compat
from ..args import as_bool, as_choice, as_date, as_float, as_int, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.acquisition_target.acquisition_target import FIT_FIELDS
from ..errors import ToolError
from ..result import ToolResult

PARTICIPANT = "Market Participant"
TARGET = "Acquisition Target"
MOVE = "Competitive Move"

REGISTER_CAP = 500

#: Statuses that mean nobody is working this target any more. Counted separately
#: in the pipeline rollup, because a pipeline of forty targets of which
#: thirty-five are Passed is a pipeline of five.
SETTLED_STATUSES = ("Closed", "Passed")

_PARTICIPANT_FIELDS = (
	"name",
	"company",
	"participant_name",
	"participant_type",
	"customer",
	"strategic_plan",
	"relationship_status",
	"industry_segment",
	"geography",
	"crops",
	"market_position",
	"market_share_pct",
	"employee_count",
	"estimated_revenue",
	"estimated_acreage",
	"strengths",
	"weaknesses",
	"key_assets",
	"vulnerability_windows",
	"notes",
)

_TARGET_FIELDS = (
	"name",
	"company",
	"entity_name",
	"market_participant",
	"strategic_plan",
	"status",
	"action_level",
	"strategic_fit_score",
	"financial_health_score",
	"synergy_score",
	"cultural_fit_score",
	"accretive_score",
	"estimated_value",
	"estimated_acquisition_cost",
	"projected_revenue_uplift",
	"projected_cost_savings",
	"acreage",
	"payback_period_years",
	"irr_estimate",
	"intergenerational_horizon_years",
	"land_value_appreciation",
	"water_rights_value",
	"varietal_ip_value",
	"infrastructure_value",
	"identified_date",
	"target_close_date",
	"actual_close_date",
	"rationale",
	"recommendation",
	"notes",
)

_MOVE_FIELDS = (
	"name",
	"company",
	"market_participant",
	"move_type",
	"strategic_plan",
	"severity",
	"observed_date",
	"description",
	"source",
	"confidence",
	"impact_assessment",
	"market_impact_pct",
	"revenue_impact",
	"response_urgency",
	"recommended_response",
	"actual_response",
	"response_date",
	"outcome",
	"notes",
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


def _num(value):
	return float(value) if value not in (None, "") else None


def _lines(value) -> list:
	return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _date(value):
	return str(value) if value else None


# ══════════════════════════════════════════════════════════════════════════
# MARKET PARTICIPANT
# ══════════════════════════════════════════════════════════════════════════


def _describe_participant(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"company": row.get("company"),
		"participant_name": row.get("participant_name"),
		"participant_type": row.get("participant_type"),
		"customer": row.get("customer") or None,
		"strategic_plan": row.get("strategic_plan") or None,
		"relationship_status": row.get("relationship_status") or None,
		"industry_segment": row.get("industry_segment") or None,
		"geography": row.get("geography") or None,
		"crops": row.get("crops") or None,
		"market_position": row.get("market_position") or None,
		# Every one of these four is a read of a private business. The key names
		# say `estimated` so a caller cannot lose that on the way through.
		"market_share_pct": _num(row.get("market_share_pct")),
		"employee_count": int(row.get("employee_count") or 0) or None,
		"estimated_revenue": _num(row.get("estimated_revenue")),
		"estimated_acreage": _num(row.get("estimated_acreage")),
		"strengths": _lines(row.get("strengths")),
		"weaknesses": _lines(row.get("weaknesses")),
		"key_assets": _lines(row.get("key_assets")),
		"vulnerability_windows": _lines(row.get("vulnerability_windows")),
		"notes": row.get("notes") or None,
	}


ESTIMATE_CAVEAT = (
	"Revenue, acreage, headcount and market share on a participant record are somebody's "
	"read of a private business, not filed figures. They are worth acting on and are not "
	"worth combining with numbers from the ledger."
)


def _write_participant(doc, args: dict, creating: bool) -> dict:
	changed = {}

	def stage(key, value):
		before = doc.get(key)
		if not _same(before, value):
			changed[key] = [before, value]
			doc.set(key, value)

	if creating or "participant_name" in args:
		stage("participant_name", as_str(args, "participant_name", required=creating))
	if creating or "participant_type" in args:
		stage(
			"participant_type",
			as_choice(
				PARTICIPANT,
				"participant_type",
				as_str(args, "participant_type", required=creating),
				"participant_type",
			),
		)
	for key in (
		"industry_segment",
		"geography",
		"crops",
		"strengths",
		"weaknesses",
		"key_assets",
		"vulnerability_windows",
		"notes",
	):
		if key in args:
			stage(key, as_str(args, key))
	for key, field in (
		("market_position", "market_position"),
		("relationship_status", "relationship_status"),
	):
		if key in args:
			value = as_str(args, key)
			stage(field, as_choice(PARTICIPANT, field, value, field) if value else "")
	for key in ("market_share_pct", "estimated_revenue", "estimated_acreage"):
		if key in args:
			stage(key, as_float(args.get(key), key))
	if "employee_count" in args:
		stage("employee_count", as_int(args, "employee_count") or 0)
	for key, doctype in (("customer", "Customer"), ("strategic_plan", "Strategic Plan")):
		if key in args:
			value = as_str(args, key)
			if value and not frappe.db.exists(doctype, value):
				raise ToolError(f"{doctype} {value!r} does not exist.")
			stage(key, value)
	return changed


def create_market_participant(args: dict) -> ToolResult:
	"""Open a record for another organisation in this market."""
	_require(PARTICIPANT)
	doc = frappe.new_doc(PARTICIPANT)
	doc.company = resolve_company(as_str(args, "company"), required=True)
	_write_participant(doc, args, creating=True)
	doc.insert(ignore_permissions=True)

	described = _describe_participant(dict(doc.as_dict()))
	return ToolResult(
		data={**described, "caveat": ESTIMATE_CAVEAT},
		summary=f"{doc.name}: {described['participant_name']} ({described['participant_type']})",
		docstatus_delta="none → 0 (created)",
	)


def get_market_participant(args: dict) -> ToolResult:
	"""One participant, with the moves observed against them and any target on them."""
	_require(PARTICIPANT)
	name = as_str(args, "market_participant", required=True)
	row = frappe.db.get_value(
		PARTICIPANT, name, compat.existing_fields(PARTICIPANT, _PARTICIPANT_FIELDS), as_dict=True
	)
	if not row:
		raise ToolError(f"Market Participant {name!r} does not exist.")
	described = _describe_participant(dict(row))

	moves = []
	if compat.doctype_exists(MOVE):
		moves = [
			_describe_move(dict(move))
			for move in frappe.db.get_all(
				MOVE,
				filters={"market_participant": described["name"]},
				fields=compat.existing_fields(MOVE, _MOVE_FIELDS),
				order_by="observed_date desc",
				limit=REGISTER_CAP,
			)
		]
	targets = []
	if compat.doctype_exists(TARGET):
		targets = [
			_describe_target(dict(target))
			for target in frappe.db.get_all(
				TARGET,
				filters={"market_participant": described["name"]},
				fields=compat.existing_fields(TARGET, _TARGET_FIELDS),
				order_by="modified desc",
				limit=REGISTER_CAP,
			)
		]

	return ToolResult(
		data={
			**described,
			"caveat": ESTIMATE_CAVEAT,
			"move_count": len(moves),
			# The gap between what was recommended and what was done. Reported here
			# because a participant with nine observed moves and no responses is a
			# competitor being watched rather than answered.
			"moves_without_response": [move["name"] for move in moves if not move["actual_response"]],
			"moves": moves,
			"acquisition_targets": targets,
		},
		summary=f"{described['participant_name']}: {len(moves)} move(s) observed",
	)


def list_market_participants(args: dict) -> ToolResult:
	"""The participant register, counted by type and by position."""
	_require(PARTICIPANT)
	company = resolve_company(as_str(args, "company"))
	limit = as_limit(args)

	filters = {}
	if company:
		filters["company"] = company
	for key in ("participant_type", "market_position", "relationship_status"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(PARTICIPANT, key, value, key)
	for key in ("geography", "industry_segment", "strategic_plan"):
		value = as_str(args, key)
		if value:
			filters[key] = value

	rows = frappe.db.get_all(
		PARTICIPANT,
		filters=filters,
		fields=compat.existing_fields(PARTICIPANT, _PARTICIPANT_FIELDS),
		order_by="participant_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	participants = [_describe_participant(dict(row)) for row in rows]

	by_type: dict = {}
	for row in participants:
		by_type[row["participant_type"]] = by_type.get(row["participant_type"], 0) + 1

	acreage = [row["estimated_acreage"] for row in participants if row["estimated_acreage"]]
	return ToolResult(
		data={
			"company": company,
			"participant_count": len(participants),
			"by_type": dict(sorted(by_type.items())),
			"estimated_acreage_total": round(sum(acreage), 2) if acreage else None,
			"without_assessment": [
				row["name"] for row in participants if not row["strengths"] and not row["weaknesses"]
			],
			"caveat": ESTIMATE_CAVEAT,
			"participants": participants,
		},
		summary=f"{len(participants)} participant(s)",
	)


def update_market_participant(args: dict) -> ToolResult:
	"""Revise a participant record."""
	_require(PARTICIPANT)
	name = as_str(args, "market_participant", required=True)
	if not frappe.db.exists(PARTICIPANT, name):
		raise ToolError(f"Market Participant {name!r} does not exist. Nothing was changed.")

	doc = frappe.get_doc(PARTICIPANT, name)
	changed = _write_participant(doc, args, creating=False)
	if not changed:
		raise ToolError(
			"nothing to change. Pass at least one of: participant_name, participant_type, "
			"customer, strategic_plan, relationship_status, industry_segment, geography, crops, "
			"market_position, market_share_pct, employee_count, estimated_revenue, "
			"estimated_acreage, strengths, weaknesses, key_assets, vulnerability_windows, notes."
		)
	doc.save(ignore_permissions=True)
	return ToolResult(
		data={**_describe_participant(dict(doc.as_dict())), "changed": changed},
		summary=f"{doc.name}: {len(changed)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ══════════════════════════════════════════════════════════════════════════
# COMPETITIVE MOVE
# ══════════════════════════════════════════════════════════════════════════


def _describe_move(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"company": row.get("company"),
		"market_participant": row.get("market_participant"),
		"move_type": row.get("move_type"),
		"strategic_plan": row.get("strategic_plan") or None,
		"severity": row.get("severity") or None,
		"observed_date": _date(row.get("observed_date")),
		"description": row.get("description") or None,
		"source": row.get("source") or None,
		"confidence": row.get("confidence") or None,
		"impact_assessment": row.get("impact_assessment") or None,
		"market_impact_pct": _num(row.get("market_impact_pct")),
		"revenue_impact": _num(row.get("revenue_impact")),
		"response_urgency": row.get("response_urgency") or None,
		"recommended_response": row.get("recommended_response") or None,
		"actual_response": row.get("actual_response") or None,
		"response_date": _date(row.get("response_date")),
		"outcome": row.get("outcome") or None,
		"notes": row.get("notes") or None,
	}


def _write_move(doc, args: dict, creating: bool) -> dict:
	changed = {}

	def stage(key, value):
		before = doc.get(key)
		if not _same(before, value):
			changed[key] = [before, value]
			doc.set(key, value)

	if creating or "market_participant" in args:
		participant = as_str(args, "market_participant", required=creating)
		if participant and not frappe.db.exists(PARTICIPANT, participant):
			raise ToolError(f"Market Participant {participant!r} does not exist.")
		stage("market_participant", participant)
	if creating or "move_type" in args:
		stage(
			"move_type",
			as_choice(MOVE, "move_type", as_str(args, "move_type", required=creating), "move_type"),
		)
	if creating or "description" in args:
		stage("description", as_str(args, "description", required=creating))
	if creating or "observed_date" in args:
		stage("observed_date", as_date(args, "observed_date") or frappe.utils.today())
	for key in ("severity", "confidence", "response_urgency"):
		if key in args:
			value = as_str(args, key)
			stage(key, as_choice(MOVE, key, value, key) if value else "")
	for key in ("source", "impact_assessment", "recommended_response", "actual_response", "outcome", "notes"):
		if key in args:
			stage(key, as_str(args, key))
	for key in ("market_impact_pct", "revenue_impact"):
		if key in args:
			stage(key, as_float(args.get(key), key))
	if "response_date" in args:
		stage("response_date", as_date(args, "response_date") or "")
	if "strategic_plan" in args:
		plan = as_str(args, "strategic_plan")
		if plan and not frappe.db.exists("Strategic Plan", plan):
			raise ToolError(f"Strategic Plan {plan!r} does not exist.")
		stage("strategic_plan", plan)
	return changed


def create_competitive_move(args: dict) -> ToolResult:
	"""Record something a competitor did, on the day somebody noticed it."""
	_require(MOVE)
	doc = frappe.new_doc(MOVE)
	doc.company = resolve_company(as_str(args, "company"), required=True)
	_write_move(doc, args, creating=True)
	doc.insert(ignore_permissions=True)

	described = _describe_move(dict(doc.as_dict()))
	data = {**described}
	if not described["actual_response"]:
		data["next_step"] = (
			"Nothing has been done about this yet. Record what was actually done — or that "
			"nothing was — with update_competitive_move; the gap between the recommendation and "
			"the response is the part of this register worth reading back."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {described['move_type']} by {described['market_participant']}",
		docstatus_delta="none → 0 (created)",
	)


def get_competitive_move(args: dict) -> ToolResult:
	"""One move in full, with the participant it belongs to."""
	_require(MOVE)
	name = as_str(args, "competitive_move", required=True)
	row = frappe.db.get_value(MOVE, name, compat.existing_fields(MOVE, _MOVE_FIELDS), as_dict=True)
	if not row:
		raise ToolError(f"Competitive Move {name!r} does not exist.")
	described = _describe_move(dict(row))

	participant = {}
	if described["market_participant"] and compat.doctype_exists(PARTICIPANT):
		participant = dict(
			frappe.db.get_value(
				PARTICIPANT,
				described["market_participant"],
				["name", "participant_name", "participant_type", "market_position"],
				as_dict=True,
			)
			or {}
		)
	return ToolResult(
		data={**described, "participant_detail": participant or None},
		summary=f"{described['name']}: {described['move_type']} on {described['observed_date']}",
	)


def list_competitive_moves(args: dict) -> ToolResult:
	"""Moves observed, newest first, with the unanswered ones named."""
	_require(MOVE)
	company = resolve_company(as_str(args, "company"))
	limit = as_limit(args)

	filters = {}
	if company:
		filters["company"] = company
	for key in ("market_participant", "strategic_plan"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	for key in ("move_type", "severity", "confidence", "response_urgency"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(MOVE, key, value, key)
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["observed_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["observed_date"] = (">=", from_date)
	elif to_date:
		filters["observed_date"] = ("<=", to_date)

	rows = frappe.db.get_all(
		MOVE,
		filters=filters,
		fields=compat.existing_fields(MOVE, _MOVE_FIELDS),
		order_by="observed_date desc",
		limit=min(limit, REGISTER_CAP),
	)
	moves = [_describe_move(dict(row)) for row in rows]

	unanswered = as_bool(args, "unanswered")
	if unanswered is not None:
		moves = [row for row in moves if bool(row["actual_response"]) is not bool(unanswered)]

	by_type: dict = {}
	by_participant: dict = {}
	for row in moves:
		by_type[row["move_type"]] = by_type.get(row["move_type"], 0) + 1
		key = row["market_participant"]
		by_participant[key] = by_participant.get(key, 0) + 1

	return ToolResult(
		data={
			"company": company,
			"move_count": len(moves),
			"by_type": dict(sorted(by_type.items())),
			"by_participant": dict(sorted(by_participant.items())),
			"unanswered": [row["name"] for row in moves if not row["actual_response"]],
			"urgent_unanswered": [
				row["name"]
				for row in moves
				if not row["actual_response"] and row["response_urgency"] in ("Respond", "Urgent")
			],
			"low_confidence": [row["name"] for row in moves if row["confidence"] == "Low"],
			"moves": moves,
		},
		summary=f"{len(moves)} move(s), {sum(1 for row in moves if not row['actual_response'])} unanswered",
	)


def update_competitive_move(args: dict) -> ToolResult:
	"""Revise a move, or record what was actually done about it."""
	_require(MOVE)
	name = as_str(args, "competitive_move", required=True)
	if not frappe.db.exists(MOVE, name):
		raise ToolError(f"Competitive Move {name!r} does not exist. Nothing was changed.")

	doc = frappe.get_doc(MOVE, name)
	changed = _write_move(doc, args, creating=False)
	if not changed:
		raise ToolError(
			"nothing to change. Pass at least one of: market_participant, move_type, "
			"strategic_plan, severity, observed_date, description, source, confidence, "
			"impact_assessment, market_impact_pct, revenue_impact, response_urgency, "
			"recommended_response, actual_response, response_date, outcome, notes."
		)
	doc.save(ignore_permissions=True)
	return ToolResult(
		data={**_describe_move(dict(doc.as_dict())), "changed": changed},
		summary=f"{doc.name}: {len(changed)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ══════════════════════════════════════════════════════════════════════════
# ACQUISITION TARGET
# ══════════════════════════════════════════════════════════════════════════


def _describe_target(row: dict) -> dict:
	scores = {field: _num(row.get(field)) for field in FIT_FIELDS}
	present = {field: value for field, value in scores.items() if value is not None}
	described = {
		"name": row.get("name"),
		"company": row.get("company"),
		"entity_name": row.get("entity_name"),
		"market_participant": row.get("market_participant") or None,
		"strategic_plan": row.get("strategic_plan") or None,
		"status": row.get("status"),
		"action_level": row.get("action_level") or None,
		**scores,
		"accretive_score": _num(row.get("accretive_score")),
		# The composite is what a list sorts on and the weakest input is what
		# kills a deal. Naming it beside the mean is the whole point of reporting
		# four scores rather than one.
		"weakest_dimension": min(present, key=present.get) if present else None,
		"scores_recorded": len(present),
		"estimated_value": _num(row.get("estimated_value")),
		"estimated_acquisition_cost": _num(row.get("estimated_acquisition_cost")),
		"projected_revenue_uplift": _num(row.get("projected_revenue_uplift")),
		"projected_cost_savings": _num(row.get("projected_cost_savings")),
		"acreage": _num(row.get("acreage")),
		"payback_period_years": _num(row.get("payback_period_years")),
		"irr_estimate": _num(row.get("irr_estimate")),
		"intergenerational_horizon_years": int(row.get("intergenerational_horizon_years") or 0) or None,
		"land_value_appreciation": _num(row.get("land_value_appreciation")),
		"water_rights_value": _num(row.get("water_rights_value")),
		"varietal_ip_value": _num(row.get("varietal_ip_value")),
		"infrastructure_value": _num(row.get("infrastructure_value")),
		"identified_date": _date(row.get("identified_date")),
		"target_close_date": _date(row.get("target_close_date")),
		"actual_close_date": _date(row.get("actual_close_date")),
		"rationale": row.get("rationale") or None,
		"recommendation": row.get("recommendation") or None,
		"notes": row.get("notes") or None,
	}
	described["asset_value_total"] = _asset_total(described)
	return described


def _asset_total(target: dict):
	"""Land, water, varietal IP and infrastructure added up, where any are known.

	Reported beside the estimated value because the two disagreeing is the
	interesting case: an asset total well above the going-concern value is a
	target worth buying for the ground, and one well below is a target whose
	value is in the business rather than the dirt.
	"""
	parts = [
		target[key]
		for key in (
			"land_value_appreciation",
			"water_rights_value",
			"varietal_ip_value",
			"infrastructure_value",
		)
		if target[key] is not None
	]
	return round(sum(parts), 2) if parts else None


def _write_target(doc, args: dict, creating: bool) -> dict:
	changed = {}

	def stage(key, value):
		before = doc.get(key)
		if not _same(before, value):
			changed[key] = [before, value]
			doc.set(key, value)

	if creating or "entity_name" in args:
		stage("entity_name", as_str(args, "entity_name", required=creating))
	if "market_participant" in args:
		participant = as_str(args, "market_participant")
		if participant and not frappe.db.exists(PARTICIPANT, participant):
			raise ToolError(f"Market Participant {participant!r} does not exist.")
		stage("market_participant", participant)
	if "strategic_plan" in args:
		plan = as_str(args, "strategic_plan")
		if plan and not frappe.db.exists("Strategic Plan", plan):
			raise ToolError(f"Strategic Plan {plan!r} does not exist.")
		stage("strategic_plan", plan)
	for key in ("status", "action_level"):
		if key in args:
			value = as_str(args, key)
			stage(key, as_choice(TARGET, key, value, key) if value else "")
	for key in FIT_FIELDS:
		if key in args:
			stage(key, as_float(args.get(key), key))
	for key in (
		"estimated_value",
		"estimated_acquisition_cost",
		"projected_revenue_uplift",
		"projected_cost_savings",
		"acreage",
		"payback_period_years",
		"irr_estimate",
		"land_value_appreciation",
		"water_rights_value",
		"varietal_ip_value",
		"infrastructure_value",
	):
		if key in args:
			stage(key, as_float(args.get(key), key))
	if "intergenerational_horizon_years" in args:
		stage("intergenerational_horizon_years", as_int(args, "intergenerational_horizon_years") or 0)
	for key in ("identified_date", "target_close_date", "actual_close_date"):
		if key in args:
			stage(key, as_date(args, key) or "")
	for key in ("rationale", "recommendation", "notes"):
		if key in args:
			stage(key, as_str(args, key))
	if "accretive_score" in args:
		raise ToolError(
			"accretive_score is derived from the four fit scores on every save and cannot be "
			"set. A composite somebody can edit independently of its inputs will disagree with "
			"them, and the disagreement is found by whoever is reading it to decide. Nothing "
			"was changed."
		)
	return changed


def create_acquisition_target(args: dict) -> ToolResult:
	"""Open a file on a farm somebody is considering buying."""
	_require(TARGET)
	doc = frappe.new_doc(TARGET)
	doc.company = resolve_company(as_str(args, "company"), required=True)
	if not as_date(args, "identified_date"):
		doc.identified_date = frappe.utils.today()
	_write_target(doc, args, creating=True)
	doc.insert(ignore_permissions=True)

	described = _describe_target(dict(doc.as_dict()))
	data = {**described}
	if described["scores_recorded"] < len(FIT_FIELDS):
		missing = [field for field in FIT_FIELDS if described[field] is None]
		data["next_step"] = (
			f"{len(missing)} of the four fit scores are unrecorded ({', '.join(missing)}), so "
			"the accretive score is a mean of the rest and is not comparable with a fully "
			"scored target. Cultural fit is the one most often skipped and most often decisive."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {described['entity_name']} ({described['status']})",
		docstatus_delta="none → 0 (created)",
	)


def get_acquisition_target(args: dict) -> ToolResult:
	"""One target in full, with its scores, its asset breakdown and its participant."""
	_require(TARGET)
	name = as_str(args, "acquisition_target", required=True)
	row = frappe.db.get_value(TARGET, name, compat.existing_fields(TARGET, _TARGET_FIELDS), as_dict=True)
	if not row:
		raise ToolError(f"Acquisition Target {name!r} does not exist.")
	described = _describe_target(dict(row))

	participant = {}
	if described["market_participant"] and compat.doctype_exists(PARTICIPANT):
		participant = _describe_participant(
			dict(
				frappe.db.get_value(
					PARTICIPANT,
					described["market_participant"],
					compat.existing_fields(PARTICIPANT, _PARTICIPANT_FIELDS),
					as_dict=True,
				)
				or {}
			)
		)

	notes = []
	if described["asset_value_total"] and described["estimated_value"]:
		if described["asset_value_total"] > described["estimated_value"]:
			notes.append(
				"The named assets total more than the estimated value of the whole. That is a "
				"target worth buying for the ground and the water rather than for the business."
			)
	if described["weakest_dimension"] and described[described["weakest_dimension"]] is not None:
		if described[described["weakest_dimension"]] < 0.4:
			notes.append(
				f"{described['weakest_dimension']} is {described[described['weakest_dimension']]}. "
				"The mean does not show this, and a deal fails on its weakest dimension rather "
				"than on its average."
			)
	return ToolResult(
		data={**described, "participant_detail": participant or None, "notes_on_scoring": notes},
		summary=f"{described['entity_name']}: {described['status']}, score {described['accretive_score']}",
	)


def list_acquisition_targets(args: dict) -> ToolResult:
	"""The pipeline, best-scored first, with the live ones separated from the settled."""
	_require(TARGET)
	company = resolve_company(as_str(args, "company"))
	limit = as_limit(args)

	filters = {}
	if company:
		filters["company"] = company
	for key in ("status", "action_level"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(TARGET, key, value, key)
	for key in ("market_participant", "strategic_plan"):
		value = as_str(args, key)
		if value:
			filters[key] = value

	rows = frappe.db.get_all(
		TARGET,
		filters=filters,
		fields=compat.existing_fields(TARGET, _TARGET_FIELDS),
		order_by="accretive_score desc, entity_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	targets = [_describe_target(dict(row)) for row in rows]

	live = [row for row in targets if row["status"] not in SETTLED_STATUSES]
	by_status: dict = {}
	for row in targets:
		by_status[row["status"]] = by_status.get(row["status"], 0) + 1

	acreage = [row["acreage"] for row in live if row["acreage"]]
	return ToolResult(
		data={
			"company": company,
			"target_count": len(targets),
			# A pipeline of forty of which thirty-five are settled is a pipeline of
			# five, and the count that gets quoted is the wrong one by default.
			"live_count": len(live),
			"by_status": dict(sorted(by_status.items())),
			"live_acreage": round(sum(acreage), 2) if acreage else None,
			"unscored": [row["name"] for row in targets if row["scores_recorded"] == 0],
			"partially_scored": [
				row["name"] for row in targets if 0 < row["scores_recorded"] < len(FIT_FIELDS)
			],
			"targets": targets,
		},
		summary=f"{len(targets)} target(s), {len(live)} live",
	)


def update_acquisition_target(args: dict) -> ToolResult:
	"""Move a target through the pipeline, or revise its scores."""
	_require(TARGET)
	name = as_str(args, "acquisition_target", required=True)
	if not frappe.db.exists(TARGET, name):
		raise ToolError(f"Acquisition Target {name!r} does not exist. Nothing was changed.")

	doc = frappe.get_doc(TARGET, name)
	changed = _write_target(doc, args, creating=False)
	if not changed:
		raise ToolError(
			"nothing to change. Pass at least one of: entity_name, market_participant, "
			"strategic_plan, status, action_level, the four fit scores, estimated_value, "
			"estimated_acquisition_cost, projected_revenue_uplift, projected_cost_savings, "
			"acreage, payback_period_years, irr_estimate, intergenerational_horizon_years, "
			"land_value_appreciation, water_rights_value, varietal_ip_value, "
			"infrastructure_value, identified_date, target_close_date, actual_close_date, "
			"rationale, recommendation, notes."
		)
	doc.save(ignore_permissions=True)
	described = _describe_target(dict(doc.as_dict()))
	return ToolResult(
		data={**described, "changed": changed},
		summary=f"{doc.name}: {len(changed)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)
