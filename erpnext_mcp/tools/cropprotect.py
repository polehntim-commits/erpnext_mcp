# SPDX-License-Identifier: MIT
"""Observation → pressure → recommendation: the crop protection pipeline.

THE PIPELINE IS ONE CALL. A scout files what they saw; this module resolves the
threshold that applies, updates the block's running pressure for the season, and
— where the action number was crossed — generates a recommendation with options
ordered least-chemical-first. Nothing downstream is typed by hand, because a
programme that requires three records to be created in the right order is a
programme that gets one record created and abandoned.

────────────────────────────────────────────────────────────────────────────
THRESHOLD RESOLUTION: MOST SPECIFIC WINS, AND A MISS IS AN ANSWER
────────────────────────────────────────────────────────────────────────────

For an observation of (crop, threat, stage) the threshold is chosen:

    1. company + crop + threat + exact stage
    2. company + crop + threat + blank stage   (the season-long fallback)
    3. any company + crop + threat + exact stage
    4. any company + crop + threat + blank stage

Crop, threat and stage are matched CASE-INSENSITIVELY, because they are typed by
a person on a phone at the end of a row and 'codling moth' and 'Codling Moth'
are the same insect.

NO THRESHOLD ON FILE IS NOT AN ERROR. It is the ordinary state of a farm's first
season, and it is recorded on the observation (`evaluation_note`) as a gap to
close. Refusing the observation would mean a farm cannot record what it saw
until it has finished writing its thresholds, which is exactly backwards — the
observations are how you find out what the thresholds should be.

────────────────────────────────────────────────────────────────────────────
THE BENEFICIAL OVERRIDE IS THE POINT OF THE WHOLE MODULE
────────────────────────────────────────────────────────────────────────────

A count over threshold with enough predators eating it does NOT generate a
chemical recommendation. It generates a 'No Action — hold and re-scout'
recommendation and says why. That is not a softening of the threshold; it is
what integrated pest management IS. A mite count over threshold with a healthy
predator population is a block already handling itself, and the spray that
'fixes' it kills the predators and guarantees a worse flare three weeks later.

────────────────────────────────────────────────────────────────────────────
SUSTAINABILITY SCORING LIVES ON THE DOCTYPE CONTROLLER, NOT HERE
────────────────────────────────────────────────────────────────────────────

`ipm_recommendation.score_methods` is the one implementation, imported below.
The controller needs it (to stamp a score whenever the action table changes from
any surface, including the Desk) and `compute_sustainability_score` needs it (to
score a proposed set that has not been saved to anything). A second copy here
would be a second scale, and the first time they disagreed would be the first
time somebody compared a stored score with a freshly computed one.
"""

from __future__ import annotations

import frappe

from .. import compat, timezones
from ..args import as_bool, as_date, as_float, as_int, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.ipm_recommendation.ipm_recommendation import (
	CHEMICAL,
	CONTROL_METHODS,
	METHOD_PRIORITY,
	METHOD_WEIGHTS,
	NO_ACTION,
	UNCLASSIFIED,
	grade_for,
	score_methods,
)
from ..erpnext_mcp.doctype.pest_action_threshold.pest_action_threshold import (
	THREAT_CATEGORIES,
	exceeds,
	rises_with,
)
from ..errors import ToolError
from ..result import ToolResult
from . import spray_rei

OBSERVATION = "Crop Observation"
THRESHOLD = "Pest Action Threshold"
PRESSURE = "Pest Pressure"
RECOMMENDATION = "IPM Recommendation"
PLANTING_SEASON = "Planting Season"

MONITORING = "Monitoring"
WATCH = "Watch"
ACTION = "Action"
CONTROLLED = "Controlled"
CLOSED = "Closed"

#: Block resolution is `spray_rei`'s. An observation, a restriction and a spray
#: on one block must resolve to the same docname in the same register.
_resolve_block = spray_rei._resolve_block

LIST_CAP = 200

_OBSERVATION_FIELDS = (
	"name",
	"company",
	"block_doctype",
	"block",
	"observed_on",
	"observed_at",
	"observer",
	"planting_season",
	"scouting_method",
	"threat_category",
	"threat",
	"crop",
	"crop_stage",
	"growth_stage_code",
	"sample_unit",
	"count_observed",
	"sample_size",
	"percent_affected",
	"severity",
	"beneficials_observed",
	"beneficial_name",
	"threshold",
	"threshold_value",
	"threshold_comparison",
	"threshold_exceeded",
	"warning_exceeded",
	"sample_below_minimum",
	"evaluation_note",
	"pest_pressure",
	"ipm_recommendation",
	"notes",
)

_PRESSURE_FIELDS = (
	"name",
	"status",
	"company",
	"block_doctype",
	"block",
	"threat_category",
	"threat",
	"crop",
	"season_year",
	"planting_season",
	"first_observed_on",
	"last_observed_on",
	"observation_count",
	"latest_value",
	"previous_value",
	"peak_value",
	"peak_on",
	"sample_unit",
	"trend",
	"beneficial_ratio",
	"threshold",
	"threshold_value",
	"threshold_exceeded_count",
	"first_exceeded_on",
	"last_exceeded_on",
	"open_recommendations",
	"last_recommendation",
	"last_spray_application",
	"controlled_on",
	"notes",
)

_THRESHOLD_FIELDS = (
	"name",
	"company",
	"crop",
	"threat_category",
	"threat",
	"crop_stage",
	"sample_unit",
	"comparison",
	"action_threshold",
	"warning_threshold",
	"beneficial_ratio_min",
	"min_sample_size",
	"recommended_methods",
	"source",
	"effective_from",
	"disabled",
	"notes",
)


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _number(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _category(args: dict, key: str = "threat_category", required: bool = True) -> str:
	value = as_str(args, key, required=required)
	if not value:
		return ""
	for option in THREAT_CATEGORIES:
		if option.lower() == value.lower():
			return option
	raise ToolError(
		f"{key} must be one of: {', '.join(THREAT_CATEGORIES)}. Got {value!r}. The six are fixed "
		"because they are what an IPM programme reports against — a seventh invented on one farm "
		"drops out of every roll-up. Nothing was written."
	)


# ── set_pest_action_threshold ───────────────────────────────────────────────
def set_pest_action_threshold(args: dict) -> ToolResult:
	"""Write or revise the number that turns a count into a decision."""
	_require(THRESHOLD)
	company = resolve_company(as_str(args, "company"))
	crop = as_str(args, "crop", required=True)
	threat = as_str(args, "threat", required=True)
	category = _category(args)
	stage = as_str(args, "crop_stage")

	action = args.get("action_threshold")
	if action in (None, ""):
		raise ToolError("action_threshold is the number this record exists to hold. Nothing was written.")
	action = as_float(action, "action_threshold")

	comparison = as_str(args, "comparison") or "Greater Than"
	valid = ("Greater Than", "Greater Than Or Equal", "Less Than", "Less Than Or Equal")
	match = [option for option in valid if option.lower() == comparison.lower()]
	if not match:
		raise ToolError(
			f"comparison must be one of: {', '.join(valid)}. Got {comparison!r}. Use a 'Less Than' "
			"comparison for a nutrient or moisture FLOOR, where the finding is that the number has "
			"fallen too far. Nothing was written."
		)
	comparison = match[0]

	warning = args.get("warning_threshold")
	warning = as_float(warning, "warning_threshold") if warning not in (None, "") else None
	if warning is not None:
		upward = rises_with(comparison)
		if upward and warning > action:
			raise ToolError(
				f"warning_threshold ({warning:g}) is above action_threshold ({action:g}) on a "
				f"'{comparison}' comparison. The warning is meant to arrive FIRST — on the way up "
				"that means below the action number. As written it would warn only after it had "
				"already said to act, which is indistinguishable from having no warning until the "
				"week it matters. Nothing was written."
			)
		if not upward and warning < action:
			raise ToolError(
				f"warning_threshold ({warning:g}) is below action_threshold ({action:g}) on a "
				f"'{comparison}' comparison. This threshold fires as a number FALLS, so the "
				"warning has to sit above the action number to arrive first. Nothing was written."
			)

	existing = _find_threshold(company or "", crop, threat, stage, exact_stage_only=True)
	replaced = None
	if existing:
		# A REVISION RETIRES THE OLD ROW RATHER THAN EDITING IT. Every observation
		# already evaluated points at the row that evaluated it, and a threshold
		# somebody edited in July would silently rewrite what June's scouting was
		# measured against. Same posture as the versioned Compliance Rule.
		replaced = existing["name"]
		old = frappe.get_doc(THRESHOLD, replaced)
		old.disabled = 1
		old.save(ignore_permissions=True)

	doc = frappe.new_doc(THRESHOLD)
	doc.company = company or None
	doc.crop = crop
	doc.threat = threat
	doc.threat_category = category
	doc.crop_stage = stage or None
	doc.comparison = comparison
	doc.action_threshold = action
	doc.warning_threshold = warning
	doc.sample_unit = as_str(args, "sample_unit") or None
	doc.beneficial_ratio_min = as_float(args.get("beneficial_ratio_min"), "beneficial_ratio_min")
	doc.min_sample_size = as_int(args, "min_sample_size") or 0
	doc.recommended_methods = as_str(args, "recommended_methods") or None
	doc.source = as_str(args, "source") or None
	doc.effective_from = as_date(args, "effective_from") or frappe.utils.nowdate()
	doc.notes = as_str(args, "notes") or None
	doc.insert(ignore_permissions=True)

	warnings = []
	if not doc.sample_unit:
		warnings.append(
			"No sample unit on this threshold. An observation and a threshold that disagree about "
			"the unit are two numbers nobody should compare — 'two per trap' against 'two percent "
			"infested' is the kind of mismatch that reads as fine and decides a spray."
		)
	if not doc.recommended_methods:
		warnings.append(
			"No recommended methods. Crossing this threshold will generate a recommendation with "
			"a single generic option, which is an alarm rather than something a crew can act on. "
			"One action per line, each prefixed with a control method — 'Biological: release "
			"predatory mites'."
		)
	if not doc.beneficial_ratio_min and category == "Insect":
		warnings.append(
			"No beneficial ratio set on an insect threshold, so a count over threshold will "
			"recommend action even where predators are present in numbers that argue for waiting."
		)

	return ToolResult(
		data={
			**_describe_threshold(dict(doc.as_dict())),
			"replaced": replaced,
			"warnings": warnings,
		},
		summary=(
			f"threshold for {threat} on {crop}"
			+ (f" at {stage}" if stage else "")
			+ f": {comparison.lower()} {action:g}"
			+ (f" (replaced {replaced})" if replaced else "")
		),
		docstatus_delta="none → 0 (created)",
	)


def _describe_threshold(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"company": row.get("company") or None,
		"crop": row.get("crop"),
		"threat_category": row.get("threat_category"),
		"threat": row.get("threat"),
		"crop_stage": row.get("crop_stage") or None,
		"sample_unit": row.get("sample_unit") or None,
		"comparison": row.get("comparison"),
		"action_threshold": round(_number(row.get("action_threshold")), 4),
		"warning_threshold": (
			round(_number(row.get("warning_threshold")), 4)
			if row.get("warning_threshold") not in (None, "")
			else None
		),
		"beneficial_ratio_min": round(_number(row.get("beneficial_ratio_min")), 3) or None,
		"min_sample_size": int(row.get("min_sample_size") or 0) or None,
		"recommended_methods": row.get("recommended_methods") or None,
		"source": row.get("source") or None,
		"effective_from": str(row.get("effective_from") or "") or None,
		"disabled": compat.checked(row.get("disabled")),
		"notes": row.get("notes") or None,
	}


def _find_threshold(
	company: str, crop: str, threat: str, stage: str, exact_stage_only: bool = False
) -> dict | None:
	"""The threshold that applies, most specific first. See the module docstring.

	Matching is case-insensitive on crop, threat and stage, because those three
	are typed by a person on a phone at the end of a row.
	"""
	if not compat.doctype_exists(THRESHOLD):
		return None
	try:
		rows = frappe.db.get_all(
			THRESHOLD,
			filters={"disabled": 0},
			fields=compat.existing_fields(THRESHOLD, _THRESHOLD_FIELDS),
			order_by="effective_from desc",
			limit=500,
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return None

	crop_key, threat_key, stage_key = crop.lower(), threat.lower(), (stage or "").lower()
	candidates = [
		dict(row)
		for row in rows or []
		if str(row.get("crop") or "").lower() == crop_key
		and str(row.get("threat") or "").lower() == threat_key
	]
	if not candidates:
		return None

	def pick(rows_in, want_company, want_stage):
		for row in rows_in:
			row_company = str(row.get("company") or "")
			if want_company and row_company != company:
				continue
			if not want_company and row_company and company and row_company != company:
				# A threshold belonging to ANOTHER company is never a fallback for
				# this one. A blank company is the site-wide default and is.
				continue
			if str(row.get("crop_stage") or "").lower() != want_stage:
				continue
			return row
		return None

	order = [(True, stage_key)]
	if not exact_stage_only:
		order += [(True, ""), (False, stage_key), (False, "")]
	for want_company, want_stage in order:
		if want_company and not company:
			continue
		found = pick(candidates, want_company, want_stage)
		if found:
			return found
	return None


# ── list_pest_action_thresholds ─────────────────────────────────────────────
def list_pest_action_thresholds(args: dict) -> ToolResult:
	"""Every threshold on file."""
	_require(THRESHOLD)
	company = resolve_company(as_str(args, "company"))

	filters: dict = {}
	if company:
		filters["company"] = company
	if as_str(args, "crop"):
		filters["crop"] = ("like", as_str(args, "crop"))
	if as_str(args, "threat"):
		filters["threat"] = ("like", as_str(args, "threat"))
	category = _category(args, required=False)
	if category:
		filters["threat_category"] = category
	if not as_bool(args, "include_disabled", False):
		filters["disabled"] = 0

	rows = frappe.db.get_all(
		THRESHOLD,
		filters=filters,
		fields=compat.existing_fields(THRESHOLD, _THRESHOLD_FIELDS),
		order_by="crop asc, threat asc, crop_stage asc",
		limit=min(as_limit(args), LIST_CAP),
	)
	thresholds = [_describe_threshold(dict(row)) for row in rows or []]
	no_methods = [t["name"] for t in thresholds if not t["recommended_methods"]]
	return ToolResult(
		data={
			"count": len(thresholds),
			"thresholds": thresholds,
			"thresholds_without_recommended_methods": no_methods,
		},
		summary=f"{len(thresholds)} action threshold(s)",
	)


# ── create_crop_observation ─────────────────────────────────────────────────
def create_crop_observation(args: dict) -> ToolResult:
	"""File what a scout saw, and run the threshold engine on it."""
	_require(OBSERVATION)
	company = resolve_company(as_str(args, "company"))
	block, block_doctype = _resolve_block(
		as_str(args, "block", required=True), as_str(args, "block_doctype"), "recorded"
	)
	category = _category(args)
	threat = as_str(args, "threat", required=True)
	crop = as_str(args, "crop")
	stage = as_str(args, "crop_stage")

	count = args.get("count_observed")
	if count in (None, ""):
		raise ToolError(
			"count_observed is required. Zero is a real and useful observation — it is how a block "
			"is shown to have been walked and found clean. Nothing was recorded."
		)
	count = as_float(count, "count_observed")

	observed_on = as_date(args, "observed_on") or frappe.utils.nowdate()
	if str(observed_on) > str(frappe.utils.nowdate()):
		raise ToolError(
			f"observed_on is {observed_on}, which is in the future. A scouting round is filed after "
			"it is walked. Nothing was recorded."
		)
	season_year = as_int(args, "season_year") or int(str(observed_on)[:4])

	planting = as_str(args, "planting_season")
	if planting and not frappe.db.exists(PLANTING_SEASON, planting):
		raise ToolError(f"no Planting Season called {planting!r} on this site. Nothing was recorded.")
	if not planting:
		planting = _planting_for(block, block_doctype, season_year, company or "")
	if planting and not crop:
		crop = str(frappe.db.get_value(PLANTING_SEASON, planting, "crop") or "")

	sample_size = as_int(args, "sample_size") or 0
	beneficials = as_float(args.get("beneficials_observed"), "beneficials_observed")
	sample_unit = as_str(args, "sample_unit")

	# ── the threshold ───────────────────────────────────────────────────────
	threshold, evaluation = evaluate_against_threshold(
		company or "", crop, threat, stage, count, sample_size, beneficials, sample_unit
	)

	doc = frappe.new_doc(OBSERVATION)
	doc.company = company or None
	doc.block_doctype = block_doctype
	doc.block = block
	doc.observed_on = observed_on
	doc.observed_at = as_str(args, "observed_at") or None
	doc.observer = (
		as_str(args, "observer") or (frappe.session.user if hasattr(frappe, "session") else "") or None
	)
	doc.planting_season = planting or None
	doc.threat_category = category
	doc.threat = threat
	doc.crop = crop or None
	doc.crop_stage = stage or None
	doc.growth_stage_code = as_str(args, "growth_stage_code") or None
	doc.sample_unit = sample_unit or (threshold or {}).get("sample_unit") or None
	doc.count_observed = count
	doc.sample_size = sample_size or 0
	doc.percent_affected = args.get("percent_affected")
	doc.beneficials_observed = beneficials
	doc.beneficial_name = as_str(args, "beneficial_name") or None
	doc.notes = as_str(args, "notes") or None
	doc.photo = as_str(args, "photo") or None
	severity = as_str(args, "severity")
	if severity:
		doc.severity = severity
	scouting = as_str(args, "scouting_method")
	if scouting:
		doc.scouting_method = scouting

	stamp_evaluation(doc, threshold, evaluation)
	doc.insert(ignore_permissions=True)

	# ── the pressure, then the recommendation ───────────────────────────────
	downstream = run_downstream(doc, threshold, evaluation, season_year)
	pressure = downstream["pest_pressure"]
	recommendation = downstream["ipm_recommendation"]

	clock = timezones.Renderer(args)
	described = _describe_observation(dict(doc.as_dict()))
	clock.add(described, "observed_at")

	return ToolResult(
		data={
			**described,
			"threshold_detail": _describe_threshold(threshold) if threshold else None,
			"pest_pressure_detail": pressure,
			"ipm_recommendation_detail": recommendation,
			"evaluation": evaluation,
		},
		summary=(
			f"{threat} on {block}: {count:g}"
			+ (f" vs threshold {evaluation['threshold_value']:g}" if threshold else " (no threshold on file)")
			+ (" — OVER, recommendation generated" if recommendation else "")
		),
		docstatus_delta="none → 0 (created)",
	)


# ── the pipeline, in three pieces both doors go through ─────────────────────
#
# v0.115.0. THERE ARE NOW TWO DOORS ONTO THIS PIPELINE and there must be exactly
# one pipeline behind them. `create_crop_observation` is somebody filing a round
# they walked; `index_scouting_observations` is the sweep turning a completed
# scouting task into the same record. If the sweep re-implemented the threshold
# lookup, an observation would be evaluated differently depending on which door
# it came through — and the two would drift silently, because both produce a
# well-formed record either way.
#
# NOTHING HERE IS NEW BEHAVIOUR. These are the exact lines
# `create_crop_observation` has run since v0.100.0, lifted out unchanged.


def evaluate_against_threshold(
	company: str, crop: str, threat: str, stage: str, count, sample_size, beneficials, sample_unit
) -> tuple:
	"""(threshold, evaluation) for one measurement. No threshold without a crop.

	A threshold is keyed on the crop as well as the threat, so an observation
	that names no crop cannot be matched to one — and guessing at the farm's
	only crop would evaluate a block of pears against a cherry number.
	"""
	threshold = _find_threshold(company or "", crop, threat, stage) if crop else None
	return threshold, _evaluate(threshold, count, sample_size, beneficials, sample_unit)


def stamp_evaluation(doc, threshold, evaluation) -> None:
	"""Copy the decision onto the observation, BEFORE it is inserted.

	The number is copied rather than joined to for the reason the DocType gives:
	the decision has to be re-readable years later against the threshold that
	actually made it, and a threshold revised next March must not silently
	rewrite what last August decided.
	"""
	doc.threshold = (threshold or {}).get("name")
	doc.threshold_value = evaluation["threshold_value"]
	doc.threshold_comparison = evaluation["comparison"]
	doc.threshold_exceeded = 1 if evaluation["action_exceeded"] else 0
	doc.warning_exceeded = 1 if evaluation["warning_exceeded"] else 0
	doc.sample_below_minimum = 1 if evaluation["sample_below_minimum"] else 0
	doc.evaluation_note = evaluation["note"]


def run_downstream(doc, threshold, evaluation, season_year: int) -> dict:
	"""Move the block's pressure, generate where the threshold said to, link both.

	Called AFTER the insert, because both writes point back at the observation
	by docname. Returns the two records it wrote, either of which may be None —
	a block with no threshold on file still moves its pressure and still
	generates nothing, and those are two different silences.
	"""
	pressure = _upsert_pressure(doc, threshold, evaluation, season_year)
	recommendation = None
	if evaluation["generate"]:
		recommendation = _generate_recommendation(doc, threshold, evaluation, pressure, season_year)

	updates = {}
	if pressure:
		updates["pest_pressure"] = pressure["name"]
	if recommendation:
		updates["ipm_recommendation"] = recommendation["name"]
	if updates:
		frappe.db.set_value(OBSERVATION, doc.name, updates)
		for key, value in updates.items():
			doc.set(key, value)

	return {"pest_pressure": pressure, "ipm_recommendation": recommendation}


def _planting_for(block: str, block_doctype: str, season_year: int, company: str) -> str:
	"""The planting on this block for this season, where exactly one is on file.

	AMBIGUITY IS ANSWERED WITH NOTHING RATHER THAN WITH A GUESS. A field worked
	as two plantings — two varieties in one legal block — has two candidates, and
	attaching a scouting round to the wrong one silently misattributes a season's
	pest history. The observation is still recorded; it just is not linked, and
	the caller can pass `planting_season` to say which.
	"""
	if block_doctype != "Field" or not compat.doctype_exists(PLANTING_SEASON):
		return ""
	filters: dict = {"field": block, "status": ("!=", "Removed")}
	if company:
		filters["company"] = company
	try:
		rows = frappe.db.get_all(PLANTING_SEASON, filters=filters, pluck="name", limit=5)
	except Exception:  # pragma: no cover
		return ""
	return rows[0] if len(rows or []) == 1 else ""


def _evaluate(threshold, count, sample_size, beneficials, sample_unit) -> dict:
	"""Measure the count against the threshold, and decide whether to generate.

	The four ways this can come out, in the order they are checked:

	  * NO THRESHOLD — recorded, nothing generated, gap named.
	  * UNIT MISMATCH — recorded, nothing generated. Comparing 'per trap' against
	    'percent infested' produces a number that is arithmetically fine and
	    means nothing, and acting on it is worse than not acting.
	  * SAMPLE TOO SMALL — recorded, pressure still moves, nothing generated.
	  * BENEFICIALS HOLDING IT — over threshold, and the recommendation generated
	    is 'hold and re-scout' rather than a control. See the module docstring.
	"""
	out = {
		"threshold_value": 0.0,
		"comparison": "",
		"action_exceeded": False,
		"warning_exceeded": False,
		"sample_below_minimum": False,
		"beneficials_holding": False,
		"beneficial_ratio": None,
		"generate": False,
		"note": "",
	}
	if not threshold:
		out["note"] = (
			"No action threshold on file for this crop and threat, so this observation was "
			"recorded but not evaluated. That is the ordinary state of a first season and it is a "
			"gap to close — set_pest_action_threshold writes one, and the observations already on "
			"file are how you find out what the number should be."
		)
		return out

	comparison = str(threshold.get("comparison") or "Greater Than")
	action = _number(threshold.get("action_threshold"))
	out["threshold_value"] = action
	out["comparison"] = comparison

	threshold_unit = str(threshold.get("sample_unit") or "")
	if threshold_unit and sample_unit and threshold_unit != sample_unit:
		out["note"] = (
			f"This observation is in {sample_unit!r} and threshold {threshold['name']} is in "
			f"{threshold_unit!r}. The two are not comparable, so no evaluation was made — a count "
			"measured against a threshold in a different unit produces a number that is "
			"arithmetically fine and means nothing. Record the observation in the threshold's unit, "
			"or set a threshold in this one."
		)
		return out

	out["action_exceeded"] = exceeds(count, action, comparison)
	warning = threshold.get("warning_threshold")
	if warning not in (None, "") and not out["action_exceeded"]:
		out["warning_exceeded"] = exceeds(count, _number(warning), comparison)

	minimum = int(threshold.get("min_sample_size") or 0)
	if minimum and sample_size and sample_size < minimum:
		out["sample_below_minimum"] = True
	elif minimum and not sample_size:
		out["sample_below_minimum"] = True

	ratio_min = _number(threshold.get("beneficial_ratio_min"))
	if ratio_min and count > 0:
		ratio = round(beneficials / count, 3)
		out["beneficial_ratio"] = ratio
		if ratio >= ratio_min:
			out["beneficials_holding"] = True

	if not out["action_exceeded"]:
		out["note"] = f"{count:g} against an action threshold of {action:g} ({comparison.lower()}) — " + (
			"early warning number crossed." if out["warning_exceeded"] else "under threshold."
		)
		return out

	if out["sample_below_minimum"]:
		out["note"] = (
			f"{count:g} is over the action threshold of {action:g}, but the sample "
			f"({sample_size or 'none recorded'}) is below the {minimum} this threshold requires. "
			"The observation is on the record and the block's pressure has moved; no "
			"recommendation was generated, because acting on a sample too small to say is how a "
			"programme loses a crew's confidence. Re-scout with a fuller sample."
		)
		return out

	if out["beneficials_holding"]:
		out["generate"] = True
		out["note"] = (
			f"{count:g} is over the action threshold of {action:g}, BUT beneficials are present at "
			f"a ratio of {out['beneficial_ratio']:g} against a minimum of {ratio_min:g}. This block "
			"is handling itself, and a control applied now would kill the predators and guarantee a "
			"worse flare in three weeks. The recommendation generated is to hold and re-scout."
		)
		return out

	out["generate"] = True
	out["note"] = f"{count:g} is over the action threshold of {action:g} ({comparison.lower()})."
	return out


def _describe_observation(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"company": row.get("company") or None,
		"block": row.get("block"),
		"block_doctype": row.get("block_doctype") or None,
		"observed_on": str(row.get("observed_on") or "") or None,
		"observed_at": str(row.get("observed_at") or "") or None,
		"observer": row.get("observer") or None,
		"planting_season": row.get("planting_season") or None,
		"scouting_method": row.get("scouting_method") or None,
		"threat_category": row.get("threat_category"),
		"threat": row.get("threat"),
		"crop": row.get("crop") or None,
		"crop_stage": row.get("crop_stage") or None,
		"growth_stage_code": row.get("growth_stage_code") or None,
		"sample_unit": row.get("sample_unit") or None,
		"count_observed": round(_number(row.get("count_observed")), 4),
		"sample_size": int(row.get("sample_size") or 0) or None,
		"percent_affected": (
			round(_number(row.get("percent_affected")), 2)
			if row.get("percent_affected") not in (None, "")
			else None
		),
		"severity": row.get("severity") or None,
		"beneficials_observed": round(_number(row.get("beneficials_observed")), 4) or None,
		"beneficial_name": row.get("beneficial_name") or None,
		"threshold": row.get("threshold") or None,
		"threshold_value": round(_number(row.get("threshold_value")), 4) or None,
		"threshold_comparison": row.get("threshold_comparison") or None,
		"threshold_exceeded": compat.checked(row.get("threshold_exceeded")),
		"warning_exceeded": compat.checked(row.get("warning_exceeded")),
		"sample_below_minimum": compat.checked(row.get("sample_below_minimum")),
		"evaluation_note": row.get("evaluation_note") or None,
		"pest_pressure": row.get("pest_pressure") or None,
		"ipm_recommendation": row.get("ipm_recommendation") or None,
		"notes": row.get("notes") or None,
	}


# ── the pressure roll-up ────────────────────────────────────────────────────
def _upsert_pressure(observation, threshold, evaluation, season_year: int) -> dict | None:
	"""One row per block, threat and season, moved by this observation.

	NEVER RAISES. An observation that is already on the record must not be lost
	because its roll-up failed; the failure comes back as a null and the caller
	sees the observation without a pressure attached, which is visible and
	recoverable. Losing the observation is neither.
	"""
	if not compat.doctype_exists(PRESSURE):
		return None
	filters = {
		"block": observation.block,
		"block_doctype": observation.block_doctype,
		"threat": observation.threat,
		"season_year": season_year,
	}
	if observation.company:
		filters["company"] = observation.company
	try:
		existing = frappe.db.get_all(PRESSURE, filters=filters, pluck="name", limit=1)
		doc = frappe.get_doc(PRESSURE, existing[0]) if existing else frappe.new_doc(PRESSURE)

		if not existing:
			doc.company = observation.company or None
			doc.block_doctype = observation.block_doctype
			doc.block = observation.block
			doc.threat_category = observation.threat_category
			doc.threat = observation.threat
			doc.season_year = season_year
			doc.status = MONITORING
			doc.first_observed_on = observation.observed_on
			doc.observation_count = 0
		doc.crop = observation.crop or doc.crop
		doc.planting_season = observation.planting_season or doc.planting_season
		doc.sample_unit = observation.sample_unit or doc.sample_unit

		count = _number(observation.count_observed)
		doc.previous_value = _number(doc.latest_value) if doc.observation_count else None
		doc.latest_value = count
		doc.last_observed_on = observation.observed_on
		doc.observation_count = int(doc.observation_count or 0) + 1
		doc.beneficial_ratio = evaluation.get("beneficial_ratio")

		if threshold:
			doc.threshold = threshold.get("name")
			doc.threshold_value = _number(threshold.get("action_threshold"))
		doc.trend = _trend(doc.previous_value, count, str((threshold or {}).get("comparison") or ""))

		if evaluation["action_exceeded"]:
			doc.threshold_exceeded_count = int(doc.threshold_exceeded_count or 0) + 1
			doc.first_exceeded_on = doc.first_exceeded_on or observation.observed_on
			doc.last_exceeded_on = observation.observed_on

		doc.status = _pressure_status(doc, evaluation)
		doc.save(ignore_permissions=True)
		return _describe_pressure(dict(doc.as_dict()))
	except Exception:  # pragma: no cover - reported as absent, never raised
		return None


def _trend(previous, current, comparison: str) -> str:
	"""Rising means DETERIORATING, not numerically larger.

	On a Nutrient threshold where the finding is a number falling below a floor,
	a dropping tissue reading is a RISING pressure. A manager scanning the board
	is asking which blocks are getting worse, and a column that answered "which
	digits got bigger" would put a recovering block and a failing one in the same
	bucket half the time.
	"""
	if previous in (None, ""):
		return "Unknown"
	previous, current = _number(previous), _number(current)
	if previous == current:
		return "Flat"
	worse_when_higher = rises_with(comparison) if comparison else True
	got_bigger = current > previous
	return "Rising" if got_bigger == worse_when_higher else "Falling"


def _pressure_status(doc, evaluation) -> str:
	"""Where this pressure now stands. Never walks back a Closed record.

	A pressure somebody closed by hand — the season is over, the block was pulled
	— stays closed. Reopening it from a stray late observation would resurrect a
	story a person had deliberately ended, and the observation is on the record
	either way.
	"""
	current = str(doc.status or MONITORING)
	if current == CLOSED:
		return CLOSED
	if evaluation["action_exceeded"]:
		return ACTION
	if evaluation["warning_exceeded"]:
		return WATCH
	# Falling back under threshold after having crossed it is Controlled, not
	# Monitoring: something was done, or the pest moved on, and the distinction
	# is what a season review is read off.
	if int(doc.threshold_exceeded_count or 0) > 0:
		return CONTROLLED
	return MONITORING


def _describe_pressure(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"status": row.get("status"),
		"company": row.get("company") or None,
		"block": row.get("block"),
		"block_doctype": row.get("block_doctype") or None,
		"threat_category": row.get("threat_category"),
		"threat": row.get("threat"),
		"crop": row.get("crop") or None,
		"season_year": int(row.get("season_year") or 0) or None,
		"planting_season": row.get("planting_season") or None,
		"first_observed_on": str(row.get("first_observed_on") or "") or None,
		"last_observed_on": str(row.get("last_observed_on") or "") or None,
		"observation_count": int(row.get("observation_count") or 0),
		"latest_value": round(_number(row.get("latest_value")), 4),
		"previous_value": (
			round(_number(row.get("previous_value")), 4)
			if row.get("previous_value") not in (None, "")
			else None
		),
		"peak_value": round(_number(row.get("peak_value")), 4),
		"peak_on": str(row.get("peak_on") or "") or None,
		"sample_unit": row.get("sample_unit") or None,
		"trend": row.get("trend") or "Unknown",
		"beneficial_ratio": (
			round(_number(row.get("beneficial_ratio")), 3)
			if row.get("beneficial_ratio") not in (None, "")
			else None
		),
		"threshold": row.get("threshold") or None,
		"threshold_value": round(_number(row.get("threshold_value")), 4) or None,
		"threshold_exceeded_count": int(row.get("threshold_exceeded_count") or 0),
		"first_exceeded_on": str(row.get("first_exceeded_on") or "") or None,
		"last_exceeded_on": str(row.get("last_exceeded_on") or "") or None,
		"open_recommendations": int(row.get("open_recommendations") or 0),
		"last_recommendation": row.get("last_recommendation") or None,
		"last_spray_application": row.get("last_spray_application") or None,
		"controlled_on": str(row.get("controlled_on") or "") or None,
		"notes": row.get("notes") or None,
	}


# ── the recommendation ──────────────────────────────────────────────────────
def _parse_methods(raw: str) -> list[dict]:
	"""'Biological: release predatory mites' → one scored, ordered action.

	A line with no recognised prefix is Unclassified, which scores low on
	purpose — see the scale in `ipm_recommendation.py`. It is not silently
	promoted to Cultural to flatter the number.
	"""
	actions = []
	for line in str(raw or "").split("\n"):
		line = line.strip()
		if not line:
			continue
		method, _, rest = line.partition(":")
		candidate = method.strip().title()
		if candidate in CONTROL_METHODS and rest.strip():
			actions.append({"control_method": candidate, "action": rest.strip()})
		else:
			actions.append({"control_method": UNCLASSIFIED, "action": line})
	return actions


def _generate_recommendation(observation, threshold, evaluation, pressure, season_year: int):
	"""Build the recommendation a crossed threshold earns. Never raises.

	THE OPTIONS ARE ORDERED LEAST-CHEMICAL-FIRST, which is the order an IPM
	programme is meant to consider them in. That ordering is not cosmetic: the
	first row is what a person reads, and a list that opened with the spray would
	make the ladder decorative.
	"""
	if not compat.doctype_exists(RECOMMENDATION):
		return None
	try:
		actions = _parse_methods((threshold or {}).get("recommended_methods") or "")
		if evaluation["beneficials_holding"]:
			# The beneficial override REPLACES the control options rather than
			# joining them. Presenting 'hold and re-scout' alongside 'cover spray'
			# is presenting no recommendation at all.
			actions = [
				{
					"control_method": NO_ACTION,
					"action": (
						"Hold and re-scout in 5-7 days. Beneficials are present at a ratio of "
						f"{evaluation['beneficial_ratio']:g}, above the "
						f"{_number((threshold or {}).get('beneficial_ratio_min')):g} this threshold "
						"requires. A control applied now kills the predators that are already "
						"working and generally produces a worse flare within three weeks."
					),
				}
			]
		elif not actions:
			actions = [
				{
					"control_method": UNCLASSIFIED,
					"action": (
						f"{observation.threat} is over its action threshold on {observation.block} "
						"and no recommended methods are set on the threshold. Decide the response "
						"and record it here; then add the methods to the threshold so the next "
						"crossing generates something actionable."
					),
				}
			]
		actions.sort(key=lambda row: METHOD_PRIORITY.index(row["control_method"]))
		for index, row in enumerate(actions, start=1):
			row["priority"] = index

		doc = frappe.new_doc(RECOMMENDATION)
		doc.status = "Open"
		doc.company = observation.company or None
		doc.block_doctype = observation.block_doctype
		doc.block = observation.block
		doc.threat_category = observation.threat_category
		doc.threat = observation.threat
		doc.crop = observation.crop or None
		doc.crop_stage = observation.crop_stage or None
		doc.season_year = season_year
		doc.planting_season = observation.planting_season or None
		doc.pest_pressure = (pressure or {}).get("name")
		doc.triggered_by = observation.name
		doc.threshold = (threshold or {}).get("name")
		doc.observed_value = _number(observation.count_observed)
		doc.threshold_value = evaluation["threshold_value"]
		doc.urgency = _urgency(evaluation, pressure)
		doc.generated_automatically = 1
		doc.rationale = evaluation["note"]
		for row in actions:
			doc.append("actions", row)
		doc.insert(ignore_permissions=True)

		if pressure:
			_bump_pressure_response(pressure["name"], doc.name)
		return _describe_recommendation(dict(doc.as_dict()), include_actions=True)
	except Exception:  # pragma: no cover - reported as absent, never raised
		return None


def _urgency(evaluation, pressure) -> str:
	"""Routine, Elevated or Urgent — set at generation, freely overridden after.

	A person standing in the block outranks this. What it is for is the board: a
	manager reading twenty open recommendations needs the three that are running
	away from them at the top, and 'crossed three times running and still rising'
	is a different problem from 'crossed once yesterday'.
	"""
	if evaluation.get("beneficials_holding"):
		return "Routine"
	crossings = int((pressure or {}).get("threshold_exceeded_count") or 0)
	trend = str((pressure or {}).get("trend") or "")
	if crossings >= 3 and trend == "Rising":
		return "Urgent"
	if crossings >= 2 or trend == "Rising":
		return "Elevated"
	return "Routine"


def _bump_pressure_response(pressure_name: str, recommendation: str) -> None:
	"""Point the pressure at its newest recommendation and count the open ones."""
	try:
		open_count = len(
			frappe.db.get_all(
				RECOMMENDATION,
				filters={"pest_pressure": pressure_name, "status": "Open"},
				pluck="name",
				limit=100,
			)
			or []
		)
		frappe.db.set_value(
			PRESSURE,
			pressure_name,
			{"last_recommendation": recommendation, "open_recommendations": open_count},
		)
	except Exception:  # pragma: no cover
		return


def _describe_recommendation(row: dict, include_actions: bool = False) -> dict:
	out = {
		"name": row.get("name"),
		"status": row.get("status"),
		"company": row.get("company") or None,
		"block": row.get("block"),
		"block_doctype": row.get("block_doctype") or None,
		"threat_category": row.get("threat_category"),
		"threat": row.get("threat"),
		"crop": row.get("crop") or None,
		"crop_stage": row.get("crop_stage") or None,
		"season_year": int(row.get("season_year") or 0) or None,
		"planting_season": row.get("planting_season") or None,
		"pest_pressure": row.get("pest_pressure") or None,
		"triggered_by": row.get("triggered_by") or None,
		"threshold": row.get("threshold") or None,
		"observed_value": round(_number(row.get("observed_value")), 4),
		"threshold_value": round(_number(row.get("threshold_value")), 4),
		"urgency": row.get("urgency") or None,
		"generated_automatically": compat.checked(row.get("generated_automatically")),
		"sustainability_score": round(_number(row.get("sustainability_score")), 1),
		"sustainability_grade": row.get("sustainability_grade") or None,
		"chemical_actions": int(row.get("chemical_actions") or 0),
		"biological_actions": int(row.get("biological_actions") or 0),
		"cultural_actions": int(row.get("cultural_actions") or 0),
		"other_actions": int(row.get("other_actions") or 0),
		"rationale": row.get("rationale") or None,
		"decided_by": row.get("decided_by") or None,
		"decided_on": str(row.get("decided_on") or "") or None,
		"resulting_spray_application": row.get("resulting_spray_application") or None,
		"closed_on": str(row.get("closed_on") or "") or None,
		"notes": row.get("notes") or None,
	}
	if include_actions:
		out["actions"] = [
			{
				"control_method": dict(line).get("control_method"),
				"action": dict(line).get("action"),
				"status": dict(line).get("status") or "Proposed",
				"priority": int(dict(line).get("priority") or 0),
				"product": dict(line).get("product") or None,
				"rate_per_acre": round(_number(dict(line).get("rate_per_acre")), 4) or None,
				"rate_uom": dict(line).get("rate_uom") or None,
				"due_by": str(dict(line).get("due_by") or "") or None,
				"farm_task": dict(line).get("farm_task") or None,
				"completed_on": str(dict(line).get("completed_on") or "") or None,
				"notes": dict(line).get("notes") or None,
			}
			for line in row.get("actions") or []
		]
	return out


# ── list_crop_observations ──────────────────────────────────────────────────
def list_crop_observations(args: dict) -> ToolResult:
	"""Scouting records over a window."""
	_require(OBSERVATION)
	company = resolve_company(as_str(args, "company"))

	filters: dict = {}
	if company:
		filters["company"] = company
	if as_str(args, "block"):
		block, block_doctype = _resolve_block(as_str(args, "block"), as_str(args, "block_doctype"), "read")
		filters["block"] = block
		filters["block_doctype"] = block_doctype
	if as_str(args, "threat"):
		filters["threat"] = ("like", as_str(args, "threat"))
	category = _category(args, required=False)
	if category:
		filters["threat_category"] = category
	if as_str(args, "planting_season"):
		filters["planting_season"] = as_str(args, "planting_season")
	if as_bool(args, "only_exceeded", False):
		filters["threshold_exceeded"] = 1

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["observed_on"] = ("between", [from_date, to_date])
	elif from_date:
		filters["observed_on"] = (">=", from_date)
	elif to_date:
		filters["observed_on"] = ("<=", to_date)

	rows = frappe.db.get_all(
		OBSERVATION,
		filters=filters,
		fields=compat.existing_fields(OBSERVATION, _OBSERVATION_FIELDS),
		order_by="observed_on desc",
		limit=min(as_limit(args), LIST_CAP),
	)
	observations = [_describe_observation(dict(row)) for row in rows or []]

	by_category = {}
	for observation in observations:
		by_category[observation["threat_category"]] = by_category.get(observation["threat_category"], 0) + 1
	unevaluated = [o["name"] for o in observations if not o["threshold"]]

	return ToolResult(
		data={
			"count": len(observations),
			"by_threat_category": by_category,
			"exceeded_count": sum(1 for o in observations if o["threshold_exceeded"]),
			"observations_with_no_threshold_on_file": unevaluated,
			"observations": observations,
		},
		summary=(
			f"{len(observations)} observation(s), "
			f"{sum(1 for o in observations if o['threshold_exceeded'])} over threshold"
		),
	)


# ── get_pest_pressure / list_pest_pressures ─────────────────────────────────
def get_pest_pressure(args: dict) -> ToolResult:
	"""One tracked threat in full, with its scouting history and open options."""
	_require(PRESSURE)
	name = as_str(args, "pest_pressure", required=True)
	if not frappe.db.exists(PRESSURE, name):
		raise ToolError(
			f"no Pest Pressure called {name!r} on this site. list_pest_pressures has the register."
		)
	row = frappe.db.get_value(
		PRESSURE, name, compat.existing_fields(PRESSURE, _PRESSURE_FIELDS), as_dict=True
	)
	described = _describe_pressure(dict(row))

	observations = frappe.db.get_all(
		OBSERVATION,
		filters={"pest_pressure": name},
		fields=compat.existing_fields(OBSERVATION, _OBSERVATION_FIELDS),
		order_by="observed_on desc",
		limit=min(as_limit(args), LIST_CAP),
	)
	recommendations = frappe.db.get_all(
		RECOMMENDATION,
		filters={"pest_pressure": name},
		fields=["name", "status", "urgency", "sustainability_score", "sustainability_grade", "creation"],
		order_by="creation desc",
		limit=50,
	)

	return ToolResult(
		data={
			**described,
			"threshold_detail": (
				_describe_threshold(
					dict(
						frappe.db.get_value(
							THRESHOLD,
							described["threshold"],
							compat.existing_fields(THRESHOLD, _THRESHOLD_FIELDS),
							as_dict=True,
						)
					)
				)
				if described.get("threshold") and frappe.db.exists(THRESHOLD, described["threshold"])
				else None
			),
			"observations": [_describe_observation(dict(o)) for o in observations or []],
			"recommendations": [dict(r) for r in recommendations or []],
		},
		summary=(
			f"{described['threat']} on {described['block']} ({described['season_year']}): "
			f"{described['status']}, latest {described['latest_value']:g}, "
			f"peak {described['peak_value']:g}, {described['trend'].lower()}"
		),
	)


def list_pest_pressures(args: dict) -> ToolResult:
	"""The board: every tracked threat, worst first."""
	_require(PRESSURE)
	company = resolve_company(as_str(args, "company"))

	filters: dict = {}
	if company:
		filters["company"] = company
	if as_str(args, "block"):
		block, block_doctype = _resolve_block(as_str(args, "block"), as_str(args, "block_doctype"), "read")
		filters["block"] = block
		filters["block_doctype"] = block_doctype
	if as_str(args, "threat"):
		filters["threat"] = ("like", as_str(args, "threat"))
	category = _category(args, required=False)
	if category:
		filters["threat_category"] = category
	if as_int(args, "season_year"):
		filters["season_year"] = as_int(args, "season_year")
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	elif not as_bool(args, "include_closed", False):
		filters["status"] = ("!=", CLOSED)

	rows = frappe.db.get_all(
		PRESSURE,
		filters=filters,
		fields=compat.existing_fields(PRESSURE, _PRESSURE_FIELDS),
		order_by="last_observed_on desc",
		limit=min(as_limit(args), LIST_CAP),
	)
	pressures = [_describe_pressure(dict(row)) for row in rows or []]

	# WORST FIRST, and "worst" is the status ladder rather than the raw number:
	# the numbers are in different units across threats and cannot be ranked
	# against each other. Action beats Watch beats everything else, and a rising
	# trend beats a falling one inside each band.
	ladder = {ACTION: 0, WATCH: 1, MONITORING: 2, CONTROLLED: 3, CLOSED: 4}
	trend_rank = {"Rising": 0, "Unknown": 1, "Flat": 2, "Falling": 3}
	pressures.sort(
		key=lambda p: (
			ladder.get(p["status"], 9),
			trend_rank.get(p["trend"], 9),
			-(p["threshold_exceeded_count"] or 0),
		)
	)

	counts = {}
	for pressure in pressures:
		counts[pressure["status"]] = counts.get(pressure["status"], 0) + 1
	return ToolResult(
		data={
			"count": len(pressures),
			"by_status": counts,
			"needing_action": [p["name"] for p in pressures if p["status"] == ACTION],
			"pest_pressures": pressures,
		},
		summary=(
			f"{len(pressures)} tracked threat(s), "
			f"{counts.get(ACTION, 0)} at Action, {counts.get(WATCH, 0)} at Watch"
		),
	)


# ── get_ipm_recommendation ──────────────────────────────────────────────────
def get_ipm_recommendation(args: dict) -> ToolResult:
	"""One recommendation in full, with its options and what they score."""
	_require(RECOMMENDATION)
	name = as_str(args, "recommendation", required=True)
	if not frappe.db.exists(RECOMMENDATION, name):
		raise ToolError(f"no IPM Recommendation called {name!r} on this site.")
	doc = frappe.get_doc(RECOMMENDATION, name)
	described = _describe_recommendation(dict(doc.as_dict()), include_actions=True)

	observation = None
	if described.get("triggered_by") and frappe.db.exists(OBSERVATION, described["triggered_by"]):
		observation = _describe_observation(
			dict(
				frappe.db.get_value(
					OBSERVATION,
					described["triggered_by"],
					compat.existing_fields(OBSERVATION, _OBSERVATION_FIELDS),
					as_dict=True,
				)
			)
		)

	clock = timezones.Renderer(args)
	clock.add(described, "decided_on", "closed_on")

	return ToolResult(
		data={
			**described,
			"triggering_observation": observation,
			"scale": {
				"weights": METHOD_WEIGHTS,
				"note": (
					"This app's own 0-100 scale, not a certification. It is documented in full at "
					"the top of ipm_recommendation.py. Chemical is 0.20 rather than 0 because a "
					"correctly timed threshold-driven spray IS integrated pest management; "
					"Unclassified is 0.30 because an action nobody categorised cannot be shown to "
					"have been anything else."
				),
			},
			"timezone": clock.block(),
		},
		summary=(
			f"{name}: {described['threat']} on {described['block']}, {described['status']}, "
			f"score {described['sustainability_score']:g} ({described['sustainability_grade']})"
		),
	)


# ── compute_sustainability_score ────────────────────────────────────────────
def compute_sustainability_score(args: dict) -> ToolResult:
	"""Score a set of control methods — a proposal, a block, or a whole season.

	Three shapes of question, one scale. `methods` scores a hypothetical set
	before anything is written. `recommendation` scores one record. A block, a
	season or a company with no other argument scores every recommendation in
	scope, which is the season-over-season number the scale exists to produce.
	"""
	company = resolve_company(as_str(args, "company"))

	raw = args.get("methods")
	if raw not in (None, ""):
		if isinstance(raw, str):
			raw = [raw]
		if not isinstance(raw, list):
			raise ToolError("methods must be a list of control-method names.")
		unknown = [
			str(name)
			for name in raw
			if str(name).strip().title() not in CONTROL_METHODS and str(name).strip()
		]
		result = score_methods([str(name).strip().title() for name in raw])
		return ToolResult(
			data={
				"scope": "methods",
				"methods": [str(name) for name in raw],
				"unrecognised_methods_scored_as_unclassified": unknown,
				**result,
				"weights": METHOD_WEIGHTS,
			},
			summary=f"{result['score']:g} ({result['grade']}) over {result['scored_count']} method(s)",
		)

	_require(RECOMMENDATION)
	name = as_str(args, "recommendation")
	if name:
		if not frappe.db.exists(RECOMMENDATION, name):
			raise ToolError(f"no IPM Recommendation called {name!r} on this site.")
		doc = frappe.get_doc(RECOMMENDATION, name)
		described = _describe_recommendation(dict(doc.as_dict()), include_actions=True)
		return ToolResult(
			data={
				"scope": "recommendation",
				"recommendation": name,
				"score": described["sustainability_score"],
				"grade": described["sustainability_grade"],
				"counts": {
					CHEMICAL: described["chemical_actions"],
					"Biological": described["biological_actions"],
					"Cultural": described["cultural_actions"],
					"Other": described["other_actions"],
				},
				"actions": described["actions"],
				"weights": METHOD_WEIGHTS,
			},
			summary=f"{name}: {described['sustainability_score']:g} ({described['sustainability_grade']})",
		)

	filters: dict = {}
	if company:
		filters["company"] = company
	if as_str(args, "block"):
		block, block_doctype = _resolve_block(as_str(args, "block"), as_str(args, "block_doctype"), "read")
		filters["block"] = block
		filters["block_doctype"] = block_doctype
	if as_int(args, "season_year"):
		filters["season_year"] = as_int(args, "season_year")
	category = _category(args, required=False)
	if category:
		filters["threat_category"] = category

	rows = frappe.db.get_all(
		RECOMMENDATION,
		filters=filters,
		fields=[
			"name",
			"status",
			"block",
			"threat",
			"threat_category",
			"season_year",
			"sustainability_score",
			"sustainability_grade",
			"chemical_actions",
			"biological_actions",
			"cultural_actions",
			"other_actions",
		],
		order_by="creation desc",
		limit=LIST_CAP,
	)
	records = [dict(row) for row in rows or []]
	if not records:
		return ToolResult(
			data={
				"scope": "portfolio",
				"count": 0,
				"score": None,
				"grade": None,
				"note": (
					"No IPM recommendations in scope, so there is nothing to score. A score of 0 "
					"would read as a bad programme rather than as an empty one, which is why this "
					"is null."
				),
				"weights": METHOD_WEIGHTS,
			},
			summary="no recommendations in scope",
		)

	# THE PORTFOLIO SCORE IS THE MEAN OF THE RECORD SCORES, not a re-score of
	# every action pooled together. Pooling would let one recommendation with
	# nine cultural rows outvote nine recommendations that each chose a spray,
	# and the question being asked is "how did this programme decide", which is
	# one vote per decision.
	total = round(sum(_number(row.get("sustainability_score")) for row in records) / len(records), 1)
	chemical = sum(int(row.get("chemical_actions") or 0) for row in records)
	biological = sum(int(row.get("biological_actions") or 0) for row in records)
	cultural = sum(int(row.get("cultural_actions") or 0) for row in records)
	other = sum(int(row.get("other_actions") or 0) for row in records)

	return ToolResult(
		data={
			"scope": "portfolio",
			"count": len(records),
			"score": total,
			"grade": grade_for(total),
			"method": "mean of per-recommendation scores, one vote per decision",
			"counts": {
				CHEMICAL: chemical,
				"Biological": biological,
				"Cultural": cultural,
				"Other": other,
			},
			"recommendations": records,
			"weights": METHOD_WEIGHTS,
		},
		summary=f"{total:g} ({grade_for(total)}) over {len(records)} recommendation(s)",
	)
