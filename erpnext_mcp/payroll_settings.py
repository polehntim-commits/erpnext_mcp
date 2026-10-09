# SPDX-License-Identifier: MIT
"""Payroll settings as versioned data. v0.235.0. docs/design/config_lifecycle_and_tool_consolidation.md §4.

Tim's decision 9 (2026-10-04): payroll settings join the shared lifecycle ONLY if payroll
keeps running uninterrupted, and only after a preview diff proves the output identical.
The first kind is `overtime_rule` — until now three constants in code (40 h a workweek,
1.5×, and the half-time premium on piece work that follows from it). It becomes a
Farm Config Version ("Payroll Setting", key `overtime_rule`) whose seed is EXACTLY those
values, so nothing a pay run computes changes on upgrade; with no version published the
readers answer the same built-in numbers.

STRICTER THAN THE PHONE KINDS (§4): a payroll version carries `effective_from`; publishing
is human-only, always (never over MCP, whoever drafted it); and a preview reruns a real pay
period twice — live and staged — and names every employee whose net moves by more than the
flag threshold (decision 10: a percentage, default 2%). Payroll TRANSACTIONS are out of
scope and keep their own tools.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import itertools

import frappe

KIND = "Payroll Setting"
OVERTIME_KEY = "overtime_rule"
#: v0.287.0 (daily pay, docs/design/daily_pay.md §9a). Every pay decision is a policy; the legal floors are a dated,
#: sourced table. Both are keys of this same Payroll Setting kind, so they share its effective dates, human-only
#: publish and preview-a-real-pay-period.
PAY_POLICY_KEY = "pay_policy"
WAGE_FLOOR_KEY = "wage_floor"
#: v0.291.0. Oregon's withholding formula (150-206-436) as a dated setting: a new year's figures are a new version.
OREGON_KEY = "oregon_withholding"
#: v0.292.0. Garnishment: the catch-up policy (Tim's middle path) and the dated, sourced limits it never exceeds.
GARNISHMENT_POLICY_KEY = "garnishment_policy"
GARNISHMENT_LIMITS_KEY = "garnishment_limits"
KEYS = (OVERTIME_KEY, PAY_POLICY_KEY, WAGE_FLOOR_KEY, OREGON_KEY, GARNISHMENT_POLICY_KEY, GARNISHMENT_LIMITS_KEY)
WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
MIN_WAGE_REGIONS = ("standard", "portland_metro", "non_urban")

#: Today's law, as the code had it: OR HB 4002 and WA SB 5172, fully phased.
DEFAULT_OVERTIME = {"weekly_threshold_hours": 40.0, "multiplier": 1.5}

#: Decision 10: flag an employee whose net pay moves by more than this percentage.
DEFAULT_FLAG_PCT = 2.0

_OVERLAY: contextvars.ContextVar = contextvars.ContextVar("erpnext_mcp_payroll_overlay", default=None)
#: v0.287.0. The pay period a run is calculating, so a reader asked without a date answers the setting in force for
#: THAT period (a rerun of April pays April's rule) rather than today's.
_AS_OF: contextvars.ContextVar = contextvars.ContextVar("erpnext_mcp_payroll_as_of", default="")


@contextlib.contextmanager
def period(as_of):
	"""During a pay run: readers default to the settings in force on `as_of` (the period end)."""
	token = _AS_OF.set(str(as_of or "")[:10])
	try:
		yield
	finally:
		_AS_OF.reset(token)


def _day(as_of: str = "") -> str:
	return str(as_of or _AS_OF.get() or frappe.utils.today())[:10]


@contextlib.contextmanager
def overlay(key: str, body: dict):
	"""During a preview only: readers take `body` for `key` instead of the published version."""
	current = dict(_OVERLAY.get() or {})
	current[key] = dict(body or {})
	token = _OVERLAY.set(current)
	try:
		yield
	finally:
		_OVERLAY.reset(token)


def _published(key: str, as_of: str = "") -> tuple[dict, str]:
	"""(body, version name) in force on `as_of`, or ({}, "built-in").

	Among the versions that have ever been published (Published or Superseded), the one
	with the latest `effective_from` on or before the day — so a rerun of April pays
	April's rule, and a version published today for next month changes nothing yet.
	"""
	try:
		from . import phone_config

		if not phone_config.ready():
			return {}, "built-in"
		day = str(as_of or frappe.utils.today())[:10]
		rows = phone_config.rows(KIND, key, (phone_config.PUBLISHED, phone_config.SUPERSEDED))
		live = [int(r.get("version") or 0) for r in rows if r["status"] == phone_config.PUBLISHED]
		if not live:
			return {}, "built-in"
		# A version newer than the published one was rolled back, and never counts.
		ceiling = max(live)
		best = None
		for row in rows:
			if int(row.get("version") or 0) > ceiling:
				continue
			doc = frappe.get_doc(phone_config.DOCTYPE, row["name"])
			body = phone_config.body_of(doc)
			effective = str(body.get("effective_from") or "")[:10]
			if not effective or effective > day:
				continue
			rank = (effective, int(row.get("version") or 0))
			if best is None or rank > best[0]:
				best = (rank, body, doc.name)
		if best:
			return best[1], best[2]
	except Exception:
		pass
	return {}, "built-in"


def _body(key: str, as_of: str = "") -> tuple[dict, str]:
	"""(body, version) for a key: the preview overlay first, else the version in force on the day."""
	staged = (_OVERLAY.get() or {}).get(key)
	if staged is not None:
		return staged, "preview"
	return _published(key, _day(as_of))


def overtime(as_of: str = "", state: str = "") -> dict:
	"""`{weekly_threshold_hours, multiplier, version}` in force on `as_of`.

	v0.287.0: a published Pay Policy's `overtime` wins over the older `overtime_rule` (it folds in). In its
	`never_below_state_floor` mode the threshold is the LOWER of the policy's hours and the state's agricultural
	threshold in force (Wage Floor) — never less generous than the law. With 40 hours and floors of 40–48, that is
	40, exactly what the overtime rule paid.
	"""
	policy, policy_version = _body(PAY_POLICY_KEY, as_of)
	if policy.get("overtime"):
		ot = dict(policy["overtime"])
		out = {"weekly_threshold_hours": float(ot.get("threshold_hours") or DEFAULT_OVERTIME["weekly_threshold_hours"]),
		       "multiplier": float(ot.get("multiplier") or DEFAULT_OVERTIME["multiplier"])}
		if ot.get("mode", "never_below_state_floor") == "never_below_state_floor" and state:
			floor = ag_overtime_threshold(state, as_of)
			if floor is not None:
				out["weekly_threshold_hours"] = min(out["weekly_threshold_hours"], floor)
		out["version"] = policy_version
		return out
	body, version = _body(OVERTIME_KEY, as_of)
	out = dict(DEFAULT_OVERTIME)
	for field in out:
		try:
			if body.get(field) not in (None, ""):
				out[field] = float(body[field])
		except (TypeError, ValueError):
			pass
	out["version"] = version
	return out


# ── the Wage Floor: dated, sourced law (v0.287.0) ─────────────────────────────
def floor_rows(as_of: str = "") -> list:
	body, _v = _body(WAGE_FLOOR_KEY, as_of)
	return list(body.get("rows") or [])


def _in_force(rows: list, day: str) -> dict:
	"""The newest row per (state, kind, region) whose effective_from is on or before `day`."""
	best: dict = {}
	for row in rows:
		effective = str(row.get("effective_from") or "")[:10]
		if not effective or effective > day:
			continue
		key = (str(row.get("state") or "").upper(), row.get("kind"), row.get("region") or "")
		if key not in best or effective > str(best[key].get("effective_from"))[:10]:
			best[key] = row
	return best


def minimum_wages(as_of: str = "") -> dict:
	"""{state: {region: rate}} in force on the day, from the Wage Floor."""
	out: dict = {}
	for (state, kind, region), row in _in_force(floor_rows(as_of), _day(as_of)).items():
		if kind == "minimum_wage":
			out.setdefault(state, {})[region or "standard"] = float(row["rate"])
	return out


def ag_overtime_threshold(state: str, as_of: str = ""):
	row = _in_force(floor_rows(as_of), _day(as_of)).get((str(state or "").upper(), "ag_overtime_threshold", ""))
	return float(row["hours"]) if row else None


def apply_wage_floor(rates: dict, as_of: str = "") -> dict:
	"""The run's minimum-wage table, never below the Wage Floor in force: the higher of each rate."""
	out = {state: dict(regions) for state, regions in (rates or {}).items()}
	for state, regions in minimum_wages(as_of).items():
		for region, rate in regions.items():
			current = out.setdefault(state, {}).get(region)
			if current is None or rate > float(current):
				out[state][region] = rate
	return out


# ── the Pay Policy (v0.287.0) ─────────────────────────────────────────────────
#: Tim's eight decisions (2026-10-08, docs/design/daily_pay.md §9a), as the seeded policy.
DEFAULT_PAY_POLICY = {
	"model": "daily_periods",
	"eligible_employment_types": ["Seasonal", "Piece Rate", "Hourly"],
	"opt_in": "signed_request",
	"switch_effective": "workweek_boundary",
	"workweek_start": "Sunday",
	"daily_cutoff": "18:00",
	"pay_reviewed_punches_only": True,
	"ach_timing": "next_business_day",
	"overtime": {"mode": "never_below_state_floor", "threshold_hours": 40, "multiplier": 1.5},
	"no_direct_deposit": "weekly_only",
	"accept_daily_min_wage_makeup": True,
	"accept_ach_fees": True,
	"weekly_cost_report": True,
}


def pay_policy(as_of: str = "") -> dict:
	body, version = _body(PAY_POLICY_KEY, as_of)
	return {**DEFAULT_PAY_POLICY, **{k: v for k, v in body.items() if k in DEFAULT_PAY_POLICY}, "version": version}


def oregon_withholding(as_of: str = "") -> dict:
	"""Oregon's withholding formula in force on the day (the published version, else the 2026 seed)."""
	from .state_withholding import OR_WITHHOLDING_2026

	body, version = _body(OREGON_KEY, as_of)
	merged = {**OR_WITHHOLDING_2026, **{k: v for k, v in body.items() if k in OR_WITHHOLDING_2026}}
	return {**merged, "version": version}


# ── Garnishment (v0.292.0) ────────────────────────────────────────────────────
_FED = "15 U.S.C. 1673; 29 CFR 870.10-.11; DOL Fact Sheet #30"
_OR = "ORS 18.385 as amended by Or. Laws 2024 ch. 100 (SB 1595) §3"
_WA = "RCW 6.27.150"
#: The limits as dated, sourced rows. A garnishment never takes more than the stricter of federal and state.
SEED_GARNISHMENT_LIMITS = [
	{"scope": "federal", "effective_from": "2009-07-24", "source": _FED + "; FLSA §6 ($7.25 since 2009-07-24)",
	 "federal_minimum_wage": 7.25, "ordinary_rate": 0.25,
	 "period_multipliers": {"Weekly": 30, "Biweekly": 60, "Semimonthly": 65, "Monthly": 130},
	 "support_rates": {"supporting": 0.50, "supporting_arrears": 0.55, "not_supporting": 0.60,
	                   "not_supporting_arrears": 0.65},
	 "student_loan_rate": 0.15},
	*[{"scope": "state", "state": "OR", "effective_from": day, "source": _OR, "ordinary_rate": 0.25,
	   "floors": {"Weekly": w, "Biweekly": b, "Semimonthly": h, "Monthly": m}}
	  for day, w, b, h, m in (("2000-01-01", 254, 509, 545, 1090), ("2025-01-01", 305, 611, 655, 1309),
	                          ("2025-07-01", 338, 675, 737, 1458), ("2026-07-01", 400, 832, 912, 1792))],
	{"scope": "state", "state": "WA", "effective_from": "2026-01-01", "source": _WA + " (1) and (4)",
	 "ordinary_rate": 0.25,
	 # (1) other judgments: exempt the greater of 75% or 35 × the federal minimum wage (35 × 7.25 a week).
	 "floors": {"Weekly": 253.75, "Biweekly": 507.50, "Semimonthly": 549.79, "Monthly": 1099.58},
	 # (4) consumer debt: exempt the greater of 80% or 35 × the state minimum wage ($17.13 in 2026).
	 "consumer": {"rate": 0.20, "floors": {"Weekly": 599.55, "Biweekly": 1199.10, "Semimonthly": 1298.93,
	                                       "Monthly": 2597.85}, "source": _WA + "(4)"}},
]
#: Tim's decision (2026-10-09): the middle path, as policy. Priority as the law orders it — sources below.
DEFAULT_GARNISHMENT_POLICY = {
	"catch_up": "middle_path",
	"carry_forward": True,
	"current_before_arrears": True,
	"priority": ["child_support", "tax_levy", "student_loan", "wage_garnishment"],
	"priority_source": ("Support first: 42 U.S.C. 666(b)(7) and ORS 25.378 (income withholding for support has "
	                    "priority over any other legal process); tax levies next and outside Title III: 29 CFR "
	                    "870.11(b)(2), IRC 6334; creditor orders share what is left of the ordinary pool: 29 CFR "
	                    "870.11(b)(1), ORS 18.385(5); within a kind, first in time."),
	"order_terms_override": True,
}


def garnishment_policy(as_of: str = "") -> dict:
	body, version = _body(GARNISHMENT_POLICY_KEY, as_of)
	return {**DEFAULT_GARNISHMENT_POLICY, **{k: v for k, v in body.items() if k in DEFAULT_GARNISHMENT_POLICY},
	        "version": version}


def garnishment_limits(as_of: str = "", state: str = "") -> dict:
	"""{federal: row, state: row or None} in force on the day — the newest row of each scope on or before it."""
	body, version = _body(GARNISHMENT_LIMITS_KEY, as_of)
	rows = body.get("rows") or SEED_GARNISHMENT_LIMITS
	day = _day(as_of)
	best: dict = {}
	for row in rows:
		effective = str(row.get("effective_from") or "")[:10]
		if not effective or effective > day:
			continue
		key = (row.get("scope"), str(row.get("state") or "").upper())
		if key not in best or effective > str(best[key]["effective_from"]):
			best[key] = row
	federal = best.get(("federal", ""))
	states = {code: dict(row) for (scope, code), row in best.items() if scope == "state" and code}
	return {"federal": dict(federal or {}), "state": states.get(str(state or "").upper()), "states": states,
	        "version": version}


def ot_multiplier(as_of: str = "") -> float:
	return overtime(as_of)["multiplier"]


def ot_premium_multiplier(as_of: str = "") -> float:
	"""What is owed ON TOP where straight time was already paid (29 CFR 778.111 / .115)."""
	return round(overtime(as_of)["multiplier"] - 1.0, 6)


def weekly_threshold(as_of: str = "") -> float:
	return overtime(as_of)["weekly_threshold_hours"]


def validate(body: dict, key: str = "", for_publish: bool = False) -> dict:
	if key == PAY_POLICY_KEY:
		return _validate_policy(body, for_publish)
	if key == WAGE_FLOOR_KEY:
		return _validate_floor(body, for_publish)
	if key == OREGON_KEY:
		return _validate_oregon(body, for_publish)
	if key == GARNISHMENT_POLICY_KEY:
		return _validate_garnishment_policy(body, for_publish)
	if key == GARNISHMENT_LIMITS_KEY:
		return _validate_garnishment_limits(body, for_publish)
	errors, warnings = [], []
	if key != OVERTIME_KEY:
		errors.append(f"a payroll setting is one of: {', '.join(KEYS)}")
		return {"errors": errors, "warnings": warnings}
	try:
		threshold = float(body.get("weekly_threshold_hours"))
		if not 1 <= threshold <= 80:
			errors.append("weekly_threshold_hours is 1–80")
	except (TypeError, ValueError):
		errors.append("weekly_threshold_hours is a number of hours")
	try:
		multiplier = float(body.get("multiplier"))
		if not 1.0 <= multiplier <= 3.0:
			errors.append("multiplier is 1.0–3.0")
	except (TypeError, ValueError):
		errors.append("multiplier is a number, e.g. 1.5")
	effective = str(body.get("effective_from") or "")
	try:
		datetime.date.fromisoformat(effective[:10])
	except ValueError:
		errors.append("effective_from (YYYY-MM-DD) is required: a payroll setting starts on a date")
	if for_publish and not errors:
		submitted = _submitted_after(effective[:10])
		if submitted:
			errors.append(
				f"effective_from {effective[:10]} is inside a submitted pay period ({submitted}); a published "
				"setting never changes a payroll that has been run — choose a date after it"
			)
	return {"errors": errors, "warnings": warnings}


def _effective(body: dict, errors: list) -> str:
	effective = str(body.get("effective_from") or "")[:10]
	try:
		datetime.date.fromisoformat(effective)
	except ValueError:
		errors.append("effective_from (YYYY-MM-DD) is required: a payroll setting starts on a date")
	return effective


def _publish_guard(effective: str, errors: list) -> None:
	submitted = _submitted_after(effective)
	if submitted:
		errors.append(f"effective_from {effective} is inside a submitted pay period ({submitted}); a published "
		              "setting never changes a payroll that has been run — choose a date after it")


def _validate_policy(body: dict, for_publish: bool) -> dict:
	errors, warnings = [], []
	choices = {"model": ("daily_periods",), "opt_in": ("signed_request",), "switch_effective": ("workweek_boundary",),
	           "ach_timing": ("next_business_day", "same_day"), "no_direct_deposit": ("weekly_only", "daily_check")}
	for field, allowed in choices.items():
		if body.get(field, DEFAULT_PAY_POLICY[field]) not in allowed:
			hint = " (weekly pay with daily advances — option B — is not built)" if field == "model" else ""
			errors.append(f"{field} is one of: {', '.join(allowed)}{hint}")
	if body.get("workweek_start", "Sunday") not in WEEKDAYS:
		errors.append(f"workweek_start is one of: {', '.join(WEEKDAYS)}")
	cutoff = str(body.get("daily_cutoff", "18:00"))
	try:
		datetime.time.fromisoformat(cutoff)
	except ValueError:
		errors.append("daily_cutoff is a time, HH:MM — e.g. 18:00")
	types = body.get("eligible_employment_types", DEFAULT_PAY_POLICY["eligible_employment_types"])
	if not isinstance(types, list) or not all(isinstance(t, str) and t.strip() for t in types):
		errors.append("eligible_employment_types is a list of Employment Type names")
	ot = body.get("overtime") or {}
	mode = ot.get("mode", "never_below_state_floor")
	if mode not in ("never_below_state_floor", "farm_threshold"):
		errors.append("overtime.mode is never_below_state_floor or farm_threshold")
	try:
		hours = float(ot.get("threshold_hours", 40))
		if not 1 <= hours <= 80:
			errors.append("overtime.threshold_hours is 1–80")
	except (TypeError, ValueError):
		errors.append("overtime.threshold_hours is a number of hours")
		hours = None
	try:
		multiplier = float(ot.get("multiplier", 1.5))
		if not 1.5 <= multiplier <= 3.0:
			errors.append("overtime.multiplier is 1.5–3.0 (the law's floor is time and a half)")
	except (TypeError, ValueError):
		errors.append("overtime.multiplier is a number, e.g. 1.5")
	for flag in ("pay_reviewed_punches_only", "accept_daily_min_wage_makeup", "accept_ach_fees", "weekly_cost_report"):
		if flag in body and not isinstance(body[flag], bool):
			errors.append(f"{flag} is true or false")
	if body.get("pay_reviewed_punches_only") is False:
		warnings.append("pay_reviewed_punches_only is off — a daily run would pay punches nobody has reviewed")
	effective = _effective(body, errors)
	if mode == "farm_threshold" and hours is not None and not errors:
		# A farm threshold may never be less generous than the law on any date from effective_from on: the floor in
		# force that day, and every later floor.
		floors = [r for r in floor_rows() if r.get("kind") == "ag_overtime_threshold"]
		binding = list(_in_force(floors, effective).values())
		binding += [r for r in floors if str(r.get("effective_from") or "")[:10] > effective]
		for row in binding:
			if hours > float(row.get("hours") or 0):
				errors.append(f"overtime after {hours:g} hours is below the floor: {row.get('state')} requires "
				              f"overtime after {float(row['hours']):g} hours from {row.get('effective_from')} "
				              f"({row.get('source')}). Use never_below_state_floor, or a lower threshold")
	if for_publish and not errors:
		_publish_guard(effective, errors)
	return {"errors": errors, "warnings": warnings}


def _validate_oregon(body: dict, for_publish: bool) -> dict:
	"""Shape and continuity: every schedule row's constant follows from the one before (to a dollar, as printed)."""
	from .state_withholding import OR_WITHHOLDING_2026, oregon_annual_withholding

	errors, warnings = [], []
	for field in OR_WITHHOLDING_2026:
		if field not in body:
			errors.append(f"{field} is required (see the seed for its shape)")
	if not str(body.get("source") or "").strip():
		errors.append("source: the DOR publication and revision these figures come from")
	for name, rows in (body.get("schedules") or {}).items():
		for before, row in itertools.pairwise(rows):
			try:
				expected = float(before[2]) + (float(row[1]) - float(before[1])) * float(before[3]) / 100
				if abs(expected - float(row[2])) > 1.0:
					errors.append(f"schedules.{name}: the row at {row[0]} should start near {expected:,.0f}, not {row[2]}")
			except (TypeError, ValueError, IndexError):
				errors.append(f"schedules.{name}: rows are [from, minus, constant, rate]")
	if not errors:
		try:
			oregon_annual_withholding(25000, 1000, {"filing_status": "Single", "allowances": 0},
			                          {**OR_WITHHOLDING_2026, **body})
		except Exception as exc:  # the shape is wrong in a way the checks above missed
			errors.append(f"the formula does not run on this body: {exc}")
	effective = _effective(body, errors)
	if for_publish and not errors:
		_publish_guard(effective, errors)
	return {"errors": errors, "warnings": warnings}


def _validate_garnishment_policy(body: dict, for_publish: bool) -> dict:
	errors = []
	if body.get("catch_up", "middle_path") not in ("middle_path", "none"):
		errors.append("catch_up is middle_path (current amount, then arrears up to the cap) or none")
	priority = list(body.get("priority") or DEFAULT_GARNISHMENT_POLICY["priority"])
	if priority[:1] != ["child_support"]:
		errors.append("priority must begin with child_support — income withholding for support has priority over "
		              "any other legal process (42 U.S.C. 666(b)(7))")
	if "tax_levy" in priority and "wage_garnishment" in priority and \
			priority.index("tax_levy") > priority.index("wage_garnishment"):
		errors.append("priority: tax levies come before creditor orders")
	if not str(body.get("priority_source") or DEFAULT_GARNISHMENT_POLICY["priority_source"]).strip():
		errors.append("priority_source: where the ordering comes from")
	effective = _effective(body, errors)
	if for_publish and not errors:
		_publish_guard(effective, errors)
	return {"errors": errors, "warnings": []}


def _validate_garnishment_limits(body: dict, for_publish: bool) -> dict:
	errors = []
	rows = body.get("rows")
	if not isinstance(rows, list) or not rows:
		errors.append("rows: the limits, one per scope (federal / state) and date")
		rows = []
	if not any(r.get("scope") == "federal" for r in rows):
		errors.append("a federal row is required — Title III applies everywhere")
	for index, row in enumerate(rows):
		where = f"rows[{index}]"
		if not str(row.get("source") or "").strip():
			errors.append(f"{where}: source — every limit names where it comes from")
		try:
			datetime.date.fromisoformat(str(row.get("effective_from") or "")[:10])
		except ValueError:
			errors.append(f"{where}: effective_from is YYYY-MM-DD")
		rate = row.get("ordinary_rate", 0.25)
		try:
			if not 0 <= float(rate) <= 0.25:
				errors.append(f"{where}: ordinary_rate is 0–0.25 (a state may be stricter than Title III, never looser)")
		except (TypeError, ValueError):
			errors.append(f"{where}: ordinary_rate is a fraction, e.g. 0.25")
		if row.get("scope") == "federal":
			try:
				if float(row.get("federal_minimum_wage") or 0) <= 0:
					errors.append(f"{where}: federal_minimum_wage — the 30× floor is built on it")
			except (TypeError, ValueError):
				errors.append(f"{where}: federal_minimum_wage is a dollar amount, e.g. 7.25")
		if row.get("scope") == "state" and len(str(row.get("state") or "")) != 2:
			errors.append(f"{where}: state is a two-letter code")
	effective = _effective(body, errors)
	if for_publish and not errors:
		_publish_guard(effective, errors)
	return {"errors": errors, "warnings": []}


def _validate_floor(body: dict, for_publish: bool) -> dict:
	errors, warnings = [], []
	rows = body.get("rows")
	if not isinstance(rows, list) or not rows:
		errors.append("rows: the floors, one per state, kind, region and date")
		rows = []
	for index, row in enumerate(rows):
		where = f"rows[{index}]"
		state = str(row.get("state") or "")
		if len(state) != 2 or not state.isalpha():
			errors.append(f"{where}: state is a two-letter code")
		try:
			datetime.date.fromisoformat(str(row.get("effective_from") or "")[:10])
		except ValueError:
			errors.append(f"{where}: effective_from is YYYY-MM-DD")
		if not str(row.get("source") or "").strip():
			errors.append(f"{where}: source — every floor names where it comes from")
		kind = row.get("kind")
		if kind == "minimum_wage":
			if (row.get("region") or "standard") not in MIN_WAGE_REGIONS:
				errors.append(f"{where}: region is one of {', '.join(MIN_WAGE_REGIONS)}")
			try:
				if float(row.get("rate")) <= 0:
					errors.append(f"{where}: rate is above zero")
			except (TypeError, ValueError):
				errors.append(f"{where}: rate is dollars an hour")
		elif kind == "ag_overtime_threshold":
			try:
				if not 1 <= float(row.get("hours")) <= 80:
					errors.append(f"{where}: hours is 1–80")
			except (TypeError, ValueError):
				errors.append(f"{where}: hours is a number of hours")
		else:
			errors.append(f"{where}: kind is minimum_wage or ag_overtime_threshold")
	effective = _effective(body, errors)
	if for_publish and not errors:
		_publish_guard(effective, errors)
	return {"errors": errors, "warnings": warnings}


def _submitted_after(day: str) -> str:
	"""A submitted Farm Payroll Entry whose period ends on or after `day`, or "".

	v0.287.0: Farm Payroll Entry is not a submittable doctype — "Submitted" is its `status` — so the v0.235.0 filter
	on docstatus never matched and this guard never fired.
	"""
	try:
		rows = frappe.db.get_all(
			"Farm Payroll Entry",
			filters={"status": "Submitted", "pay_period_end": (">=", day)},
			fields=["name"],
			limit=1,
		)
	except Exception:
		return ""
	return rows[0]["name"] if rows else ""


def seed_body(key: str = OVERTIME_KEY) -> dict:
	if key == PAY_POLICY_KEY:
		return {"schema_version": 1, "key": key, **DEFAULT_PAY_POLICY, "effective_from": "2026-10-08",
		        "basis": "Tim's eight decisions, 2026-10-08 (docs/design/daily_pay.md §9a)."}
	if key == WAGE_FLOOR_KEY:
		return {"schema_version": 1, "key": key, "effective_from": "2023-01-01", "rows": [dict(r) for r in SEED_FLOORS]}
	if key == OREGON_KEY:
		from .state_withholding import OR_WITHHOLDING_2026

		return {"schema_version": 1, "key": key, **OR_WITHHOLDING_2026}
	if key == GARNISHMENT_POLICY_KEY:
		return {"schema_version": 1, "key": key, **DEFAULT_GARNISHMENT_POLICY, "effective_from": "2026-10-09",
		        "basis": "Tim's decision 2026-10-09: the middle path, as a policy (not hard-coded)."}
	if key == GARNISHMENT_LIMITS_KEY:
		return {"schema_version": 1, "key": key, "effective_from": "2009-07-24",
		        "rows": [dict(r) for r in SEED_GARNISHMENT_LIMITS]}
	return {"schema_version": 1, "key": OVERTIME_KEY, **DEFAULT_OVERTIME, "effective_from": "2020-01-01",
	        "basis": "OR HB 4002 and WA SB 5172, fully phased: 40 hours a workweek, 1.5× the regular rate."}


_BOLI = "Oregon BOLI minimum wage schedule, oregon.gov/boli/workers/pages/minimum-wage-schedule.aspx"
_BOLI_AG = "Oregon HB 4002 (2022); BOLI, oregon.gov/boli/employers/Pages/minimum-wage-and-overtime-in-agriculture.aspx"
_LNI = "Washington L&I, lni.wa.gov (2026 rate announced 2025-09-30)"
#: v0.287.0. The floors as of 2026-10-09, each with its source. A new legal rate is a new row (a new version of this
#: setting, published by a person) — never a release.
SEED_FLOORS = (
	{"state": "OR", "kind": "minimum_wage", "region": "standard", "rate": 15.05, "effective_from": "2025-07-01", "source": _BOLI},
	{"state": "OR", "kind": "minimum_wage", "region": "portland_metro", "rate": 16.30, "effective_from": "2025-07-01", "source": _BOLI},
	{"state": "OR", "kind": "minimum_wage", "region": "non_urban", "rate": 14.05, "effective_from": "2025-07-01", "source": _BOLI},
	{"state": "OR", "kind": "minimum_wage", "region": "standard", "rate": 15.55, "effective_from": "2026-07-01", "source": _BOLI},
	{"state": "OR", "kind": "minimum_wage", "region": "portland_metro", "rate": 16.80, "effective_from": "2026-07-01", "source": _BOLI},
	{"state": "OR", "kind": "minimum_wage", "region": "non_urban", "rate": 14.55, "effective_from": "2026-07-01", "source": _BOLI},
	{"state": "OR", "kind": "ag_overtime_threshold", "hours": 55, "effective_from": "2023-01-01", "source": _BOLI_AG},
	{"state": "OR", "kind": "ag_overtime_threshold", "hours": 48, "effective_from": "2025-01-01", "source": _BOLI_AG},
	{"state": "OR", "kind": "ag_overtime_threshold", "hours": 40, "effective_from": "2027-01-01", "source": _BOLI_AG},
	{"state": "WA", "kind": "minimum_wage", "region": "standard", "rate": 16.66, "effective_from": "2025-01-01", "source": _LNI},
	{"state": "WA", "kind": "minimum_wage", "region": "standard", "rate": 17.13, "effective_from": "2026-01-01", "source": _LNI},
	{"state": "WA", "kind": "ag_overtime_threshold", "hours": 40, "effective_from": "2024-01-01",
	 "source": "Washington SB 5172 (2021), fully phased 2024-01-01"},
)


def flag_pct() -> float:
	try:
		from . import settings

		value = float(settings.get_settings().get("payroll_preview_flag_pct") or 0)
	except Exception:
		value = 0
	return value if value > 0 else DEFAULT_FLAG_PCT


# ── the preview: rerun a pay period live and staged ─────────────────────────
_COMPARED = ("gross_pay", "net_pay", "total_deductions", "overtime_pay", "regular_hours", "overtime_hours",
             "federal_withholding", "state_withholding", "social_security", "medicare", "employer_taxes")


def latest_period(company: str = "") -> dict:
	filters = {"docstatus": ("in", [0, 1])}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(
		"Farm Payroll Entry",
		filters=filters,
		fields=["name", "company", "pay_period_start", "pay_period_end", "pay_frequency"],
		order_by="pay_period_end desc",
		limit=1,
	)
	return dict(rows[0]) if rows else {}


def preview(key: str, body: dict, args: dict) -> dict:
	"""Rerun one pay period with the live setting and with `body`; per-employee differences."""
	from .tools import payroll

	period = {k: args.get(k) for k in ("company", "pay_period_start", "pay_period_end", "pay_frequency") if args.get(k)}
	if not (period.get("pay_period_start") and period.get("pay_period_end")):
		last = latest_period(str(args.get("company") or ""))
		if not last:
			raise ValueError("no pay period to rerun: pass company, pay_period_start and pay_period_end")
		period = {"company": last["company"], "pay_period_start": str(last["pay_period_start"]),
		          "pay_period_end": str(last["pay_period_end"]), "pay_frequency": last.get("pay_frequency") or "Biweekly"}
	report = validate({**body}, key)
	if report["errors"]:
		raise ValueError("; ".join(report["errors"]))
	run_args = dict(period)
	_c, live_slips, live_totals = payroll._period_run(dict(run_args), creating=False)
	with overlay(key, body):
		_c2, staged_slips, staged_totals = payroll._period_run(dict(run_args), creating=False)
	live_by = {s.get("employee"): s for s in live_slips}
	staged_by = {s.get("employee"): s for s in staged_slips}
	threshold = flag_pct()
	employees, flagged = [], []
	for employee in sorted(set(live_by) | set(staged_by)):
		a, b = live_by.get(employee) or {}, staged_by.get(employee) or {}
		changes = {}
		for field in _COMPARED:
			x, y = _num(a.get(field)), _num(b.get(field))
			if x is None and y is None:
				continue
			if round((y or 0) - (x or 0), 2) != 0:
				changes[field] = {"live": x, "staged": y, "difference": round((y or 0) - (x or 0), 2)}
		net_live = _num(a.get("net_pay")) or 0.0
		net_diff = (changes.get("net_pay") or {}).get("difference", 0.0)
		pct = round(abs(net_diff) / net_live * 100, 2) if net_live else (100.0 if net_diff else 0.0)
		entry = {"employee": employee, "employee_name": a.get("employee_name") or b.get("employee_name"),
		         "changes": changes, "net_change_pct": pct, "flagged": pct > threshold}
		employees.append(entry)
		if entry["flagged"]:
			flagged.append(employee)
	totals = {}
	for field in ("total_gross", "total_net", "total_deductions"):
		x, y = _num(live_totals.get(field)), _num(staged_totals.get(field))
		totals[field] = {"live": x, "staged": y, "difference": round((y or 0) - (x or 0), 2)}
	identical = not any(e["changes"] for e in employees)
	return {
		"period": period,
		"identical": identical,
		"employees": employees,
		"changed": sum(1 for e in employees if e["changes"]),
		"flagged": flagged,
		"flag_threshold_pct": threshold,
		"totals": totals,
		"written": False,
		"note": "NOTHING WAS WRITTEN. The period was calculated twice, read-only; no slip, entry or GL row exists from it.",
	}


def _num(value):
	try:
		return round(float(value), 2) if value not in (None, "") else None
	except (TypeError, ValueError):
		return None
