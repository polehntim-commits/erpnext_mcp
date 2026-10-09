# SPDX-License-Identifier: MIT
"""Daily pay with a weekly overtime settlement. docs/design/daily_pay.md (approved 2026-10-08, §9a).

Built in steps, each its own release:
  v0.287.0  the Pay Policy and Wage Floor (payroll_settings), the opt-in on Employee, `Daily` and `run_kind` on
            Farm Payroll Entry, and who is eligible on a day — this module.
  v0.288.0  the daily run (reviewed punches only) and its NACHA file.
  v0.289.0  the weekly settlement run (overtime premium on the regular rate, deductions, garnishments).
  later     the phone: "Pay me daily", today's pay, the supervisor's tile.

OFF per company until `daily_pay_enabled` is on for it. Payroll stays human: every run, file and payment is
started and approved by a person, exactly as today; nothing here sends money.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat

FLAG = "daily_pay_enabled"
EMPLOYEE = "Employee"
FIELDS = (
	("daily_pay", "Daily Pay", "Check", "",
	 "Paid each working day for that day, with the week's overtime, deductions and garnishments in a weekly "
	 "settlement. Set from the worker's signed request; takes effect at a workweek boundary (Pay Policy)."),
	("daily_pay_since", "Daily Pay Since", "Date", "daily_pay",
	 "The first day of the workweek the opt-in (or opt-out) took effect."),
)


def install_fields() -> list:
	"""Employee.daily_pay and Employee.daily_pay_since. Idempotent; never overwrites a value."""
	made = []
	if not compat.doctype_exists(EMPLOYEE):
		return made
	after = "employment_type"
	for fieldname, label, fieldtype, follows, description in FIELDS:
		if compat.has_field(EMPLOYEE, fieldname):
			continue
		anchor = follows or after
		doc = frappe.get_doc({"doctype": "Custom Field", "dt": EMPLOYEE, "fieldname": fieldname, "label": label,
		                      "fieldtype": fieldtype, "insert_after": anchor if compat.has_field(EMPLOYEE, anchor) else "",
		                      "description": description, "module": "ERPNext MCP", "read_only": 1})
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		made.append(f"{EMPLOYEE}.{fieldname}")
	return made


def enabled(company: str) -> bool:
	from . import flags

	return bool(company) and bool(flags.value(FLAG, company=company, default=False))


def workweek_start(day, policy: dict | None = None) -> datetime.date:
	"""The first day of the workweek holding `day`, per the Pay Policy (Sunday by default)."""
	from . import payroll_settings

	policy = policy or payroll_settings.pay_policy(str(day))
	day = day if isinstance(day, datetime.date) else datetime.date.fromisoformat(str(day)[:10])
	first = payroll_settings.WEEKDAYS.index(policy.get("workweek_start") or "Sunday")  # Sunday = 0
	today = (day.weekday() + 1) % 7  # Monday = 1 … Sunday = 0
	return day - datetime.timedelta(days=(today - first) % 7)


def eligibility(employee: str, day, company: str = "") -> dict:
	"""Whether a worker is on daily pay for `day`, and if not, why — every reason, in words."""
	from . import payroll_settings

	day = str(day)[:10]
	policy = payroll_settings.pay_policy(day)
	fields = compat.existing_fields(EMPLOYEE, ("name", "company", "employment_type", "daily_pay", "daily_pay_since",
	                                          "status"))
	row = dict(frappe.db.get_value(EMPLOYEE, employee, fields, as_dict=True) or {})
	reasons = []
	if not row:
		return {"employee": employee, "day": day, "eligible": False, "reasons": [f"no Employee {employee!r}"]}
	company = company or row.get("company") or ""
	if not enabled(company):
		reasons.append(f"daily pay is off for {company} (`{FLAG}`)")
	if row.get("status") and row["status"] != "Active":
		reasons.append(f"the employee is {row['status']}")
	if not compat.checked(row.get("daily_pay")):
		reasons.append("the worker has not opted in")
	elif row.get("daily_pay_since") and str(row["daily_pay_since"])[:10] > day:
		reasons.append(f"daily pay starts {str(row['daily_pay_since'])[:10]} (the next workweek)")
	types = policy.get("eligible_employment_types") or []
	if types and row.get("employment_type") not in types:
		reasons.append(f"employment type {row.get('employment_type') or '(none)'} is not one the Pay Policy pays daily "
		               f"({', '.join(types)})")
	return {"employee": employee, "day": day, "company": company, "eligible": not reasons, "reasons": reasons,
	        "policy_version": policy.get("version")}


# ── v0.288.0: the daily run ─────────────────────────────────────────────────
PAYROLL_ENTRY = "Farm Payroll Entry"


class DailyPayError(ValueError):
	pass


def has_direct_deposit(employee: str) -> bool:
	"""An Active bank account NACHA will pay (v0.225.0's self-service accounts become Active once verified, approved
	and past the prenote)."""
	from .tools import ach

	try:
		return bool(ach._employee_accounts(employee, active_only=True))
	except Exception:
		return False


def paid_on(employee: str, day: str) -> str:
	"""The Daily run that already paid this worker for `day`, or ""."""
	if not compat.has_field(PAYROLL_ENTRY, "run_kind"):
		return ""
	for row in frappe.db.get_all(PAYROLL_ENTRY, filters={"run_kind": "Daily", "pay_period_start": day,
	                                                      "status": ("!=", "Cancelled")}, fields=["name"], limit=50) or []:
		doc = frappe.get_doc(PAYROLL_ENTRY, row["name"])
		if any((s.get("employee") if isinstance(s, dict) else getattr(s, "employee", None)) == employee
		       for s in doc.get("slips") or []):
			return row["name"]
	return ""


def next_business_day(day) -> datetime.date:
	"""Next weekday after `day` — when a next-business-day ACH lands. Bank holidays are the bank's to add."""
	day = day if isinstance(day, datetime.date) else datetime.date.fromisoformat(str(day)[:10])
	day += datetime.timedelta(days=1)
	while day.weekday() >= 5:
		day += datetime.timedelta(days=1)
	return day


def _opted_in(company: str) -> list:
	if not compat.has_field(EMPLOYEE, "daily_pay"):
		return []
	return [r["name"] for r in frappe.db.get_all(EMPLOYEE, filters={"company": company, "daily_pay": 1},
	                                              fields=["name"], limit=5000) or []]


def day_plan(company: str, day, now=None) -> dict:
	"""Who a daily run for `day` pays, and who waits — with the reason, in words. Raises when the run cannot start."""
	from . import payroll_settings, time_review

	day = str(day)[:10]
	if not enabled(company):
		raise DailyPayError(f"daily pay is off for {company} — turn on `{FLAG}` for it when it goes live.")
	policy = payroll_settings.pay_policy(day)
	now = now or frappe.utils.get_datetime(frappe.utils.now())
	if day == str(now.date()) and now.time() < datetime.time.fromisoformat(str(policy.get("daily_cutoff") or "18:00")):
		raise DailyPayError(f"{day} is not closed yet — today's pay runs after {policy.get('daily_cutoff')} "
		                    "(Pay Policy daily_cutoff), once the day's punches are in and reviewed.")
	if day > str(now.date()):
		raise DailyPayError(f"{day} has not happened yet.")
	pay, held = [], []
	for employee in _opted_in(company):
		reasons = list(eligibility(employee, day, company)["reasons"])
		if not reasons:
			done = paid_on(employee, day)
			if done:
				reasons.append(f"already paid for {day} on {done}")
		if not reasons and policy.get("pay_reviewed_punches_only", True):
			pending = time_review.listing([company], day, day, employee=employee, status="pending")
			if pending:
				reasons.append(f"{len(pending)} punch(es) on {day} not reviewed yet — paid on a daily run after review")
		if not reasons and policy.get("no_direct_deposit") == "weekly_only" and not has_direct_deposit(employee):
			reasons.append("no active direct deposit — paid on the weekly run instead (Pay Policy no_direct_deposit)")
		if reasons:
			held.append({"employee": employee,
			             "employee_name": frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee,
			             "reasons": reasons})
		else:
			pay.append(employee)
	return {"day": day, "company": company, "pay": pay, "held": held, "policy_version": policy.get("version"),
	        "ach_effective_date": str(next_business_day(day)) if policy.get("ach_timing") == "next_business_day"
	        else day}


def paid_daily(employee: str, day, company: str = "") -> bool:
	"""On daily pay that day. A worker without direct deposit stays on the weekly run when the Pay Policy says
	weekly_only — so the two runs never both skip (or both pay) the same person."""
	from . import payroll_settings

	if not eligibility(employee, day, company)["eligible"]:
		return False
	if payroll_settings.pay_policy(str(day)[:10]).get("no_direct_deposit") == "weekly_only":
		return has_direct_deposit(employee)
	return True


def on_daily_pay(company: str, start, end) -> tuple:
	"""(paid daily for the whole period — leave them out of a Regular run, switched during it — hold them)."""
	if not enabled(company):
		return [], []
	whole, switched = [], []
	for employee in _opted_in(company):
		first = paid_daily(employee, str(start)[:10], company)
		last = paid_daily(employee, str(end)[:10], company)
		if first and last:
			whole.append(employee)
		elif first or last:
			switched.append(employee)
	return whole, switched


# ── v0.289.0: the weekly settlement (§4, §5) ─────────────────────────────────
def _row_get(row, key, default=None):
	return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _num(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _entries(company: str, start: str, end: str, run_kind: str) -> list:
	if not compat.has_field(PAYROLL_ENTRY, "run_kind"):
		return []
	return frappe.db.get_all(PAYROLL_ENTRY, filters={"company": company, "run_kind": run_kind,
	                                                  "pay_period_start": ("between", [start, end]),
	                                                  "status": ("!=", "Cancelled")},
	                         fields=["name", "pay_period_start"], order_by="pay_period_start asc", limit=500) or []


def week_daily_slips(company: str, start: str, end: str) -> dict:
	"""{employee: [the week's daily slips, oldest first]}, each with the entry it came from."""
	out: dict = {}
	for entry in _entries(company, start, end, "Daily"):
		for slip in frappe.get_doc(PAYROLL_ENTRY, entry["name"]).get("slips") or []:
			employee = _row_get(slip, "employee")
			if employee:
				out.setdefault(employee, []).append({**{k: _row_get(slip, k) for k in (
					"employee", "employee_name", "salary_structure", "work_state", "total_hours", "gross_pay",
					"statutory_deductions", "social_security", "earned_gross", "minimum_wage_makeup",
					"federal_withholding", "state_withholding")},
					"entry": entry["name"], "day": str(entry["pay_period_start"])[:10]})
	return out


def settled_on(employee: str, company: str, start: str) -> str:
	for entry in _entries(company, start, start, "Settlement"):
		if any(_row_get(s, "employee") == employee for s in frappe.get_doc(PAYROLL_ENTRY, entry["name"]).get("slips") or []):
			return entry["name"]
	return ""


def _open_shortfalls(employee: str, company: str, start: str) -> list:
	"""Last week's settlement shortfalls for this worker — shown again, never collected automatically."""
	import json

	prior = (datetime.date.fromisoformat(start) - datetime.timedelta(days=7)).isoformat()
	for entry in _entries(company, prior, prior, "Settlement"):
		for slip in frappe.get_doc(PAYROLL_ENTRY, entry["name"]).get("slips") or []:
			if _row_get(slip, "employee") != employee:
				continue
			try:
				detail = json.loads(_row_get(slip, "state_taxes_detail") or "{}")
			except ValueError:
				detail = {}
			return [{**row, "from": entry["name"]} for row in detail.get("_deduction_shortfalls") or []]
	return []


def settlement(company: str, start, end) -> tuple:
	"""(slips, held, notes) for the workweek `start`..`end`: per daily-pay worker, the week's overtime premium on
	the regular rate, and the week's deductions and garnishments.

	§4. Overtime hours are the week's hours over the threshold in force (Pay Policy, never less generous than the
	state). The REGULAR RATE is the week's straight-time earnings — hourly, piece, paid rest and minimum-wage makeup,
	everything the daily runs paid — over the week's hours (29 CFR 778.109 / .111 / .115). The daily runs already paid
	straight time for every hour, so what is owed is the premium: overtime hours × regular rate × (multiplier − 1).
	Withheld on the WEEKLY tables, as its own payment of a weekly figure.

	§5. Deductions and garnishments are taken here, against the WEEK's disposable earnings (the CCPA's ceilings are
	weekly), out of this settlement's net only — a daily payment is never clawed back and never made negative. What
	the settlement cannot cover is reported as a shortfall, and shown again on next week's settlement; it is not
	collected automatically, because arrears must stay inside the same ceilings and that is a person's call.
	"""
	from . import payroll_calc, payroll_deductions, payroll_settings
	from .tools import payroll as runs

	start, end = str(start)[:10], str(end)[:10]
	if workweek_start(start).isoformat() != start or (
			datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days != 6:
		raise DailyPayError(f"a settlement is one workweek: {workweek_start(start)} to "
		                    f"{workweek_start(start) + datetime.timedelta(days=6)} (Pay Policy workweek_start).")
	if not enabled(company):
		raise DailyPayError(f"daily pay is off for {company} — turn on `{FLAG}` for it when it goes live.")
	by_employee = week_daily_slips(company, start, end)
	deductions_by_employee, _sources = runs._load_deductions(company, end, list(by_employee))
	ytd_by_employee = runs._load_ytd(company, start, list(by_employee))
	slips, held, notes = [], [], []
	for employee, days in sorted(by_employee.items()):
		done = settled_on(employee, company, start)
		if done:
			held.append({"employee": employee, "employee_name": days[0].get("employee_name"),
			             "reasons": [f"this week is already settled on {done}"]})
			continue
		hours = round(sum(_num(d["total_hours"]) for d in days), 2)
		straight = round(sum(_num(d["gross_pay"]) for d in days), 2)
		daily_taxes = round(sum(_num(d["statutory_deductions"]) for d in days), 2)
		by_state: dict = {}
		for d in days:
			by_state[d.get("work_state") or ""] = by_state.get(d.get("work_state") or "", 0.0) + _num(d["total_hours"])
		state = max(by_state, key=by_state.get) if by_state else ""
		rule = payroll_settings.overtime(end, state=state)
		threshold, multiplier = float(rule["weekly_threshold_hours"]), float(rule["multiplier"])
		overtime_hours = round(max(hours - threshold, 0.0), 2)
		regular_rate = round(straight / hours, 4) if hours else 0.0
		premium = round(overtime_hours * regular_rate * (multiplier - 1.0), 2)

		tax_config = runs._build_tax_config(employee, "Weekly", company, end)
		tax_config["pay_frequency"] = "Weekly"
		ytd = ytd_by_employee.get(employee) or {}
		tax_config["ytd_gross"] = round(_num(ytd.get("ytd_gross")) + straight, 2)
		tax_config["ytd_ss_withheld"] = round(_num(ytd.get("ytd_ss_withheld")) + sum(_num(d["social_security"])
		                                                                               for d in days), 2)
		who = {"employee": employee, "employee_name": days[0].get("employee_name") or employee}
		where = [{"work_state": state, "hours": 0.0, "overtime_hours": 0.0, "piece_units": 0, "break_hours": 0.0}]
		structure = days[0].get("salary_structure") or ""
		slip = payroll_calc.calculate_full_payroll(
			who, where, {"pay_type": "Salary", "base_rate": premium, "name": structure}, tax_config, deductions=None)
		# INCOME TAX BY THE AGGREGATE METHOD (IRS Pub 15 §7, supplemental wages paid separately): the tax on the
		# week's whole pay on the weekly table, less what the daily runs withheld — not the weekly table applied to
		# the premium alone, which would treat $50 as a whole week's pay and under-withhold. FICA is flat; unchanged.
		whole = payroll_calc.calculate_full_payroll(
			who, where, {"pay_type": "Salary", "base_rate": round(straight + premium, 2), "name": structure},
			{**tax_config, "ytd_gross": _num(ytd.get("ytd_gross")),
			 "ytd_ss_withheld": _num(ytd.get("ytd_ss_withheld"))}, deductions=None)
		for field in ("federal_withholding", "state_withholding"):
			owed = round(max(_num(whole.get(field)) - sum(_num(d.get(field)) for d in days), 0.0), 2)
			slip["statutory_deductions"] = round(_num(slip.get("statutory_deductions")) - _num(slip.get(field)) + owed, 2)
			slip[field] = owed

		rows = payroll_deductions.active_deductions(deductions_by_employee.get(employee) or [], end)
		skipped = [r for r in rows if payroll_deductions.row_is_pre_tax(r)]
		rows = [r for r in rows if not payroll_deductions.row_is_pre_tax(r)]
		for row in skipped:
			notes.append(f"{days[0].get('employee_name') or employee}: the pre-tax election "
			             f"{payroll_deductions.row_label(row)} is not taken for a daily-pay worker (it would have to "
			             "change wages already paid) — keep this worker weekly, or make it after-tax")
		statutory = _num(slip.get("statutory_deductions"))
		week_gross = round(straight + premium, 2)
		disposable = payroll_deductions.disposable_earnings(week_gross, round(daily_taxes + statutory, 2))
		garnish = payroll_deductions.apply_garnishments(disposable, rows, pay_frequency="Weekly",
		                                                state_cap_rate=tax_config.get("garnishment_state_cap_rate"))
		cash = round(max(premium - statutory, 0.0), 2)
		garnished = 0.0
		lines, shortfalls = [], list(garnish.get("shortfalls") or [])
		for line in garnish["lines"]:
			take = round(min(_num(line["amount"]), cash), 2)
			if take < _num(line["amount"]):
				short = {**line, "amount": take, "shortfall": round(_num(line["amount"]) - take + _num(line.get("shortfall")), 2),
				         "shortfall_reason": "the settlement's net was short — daily wages already paid are never taken back"}
				shortfalls.append(short)
				line = short
			cash = round(cash - take, 2)
			garnished += take
			lines.append(line)
		post = payroll_deductions.apply_post_tax_deductions(cash, rows, week_gross, disposable)
		lines += post["lines"]
		shortfalls += post.get("shortfalls") or []
		summary = payroll_deductions.summarize_deductions(lines)
		withholdings = round(_num(summary.get("total")), 2)
		slip.update({
			"total_hours": hours, "regular_hours": round(hours - overtime_hours, 2), "overtime_hours": overtime_hours,
			"earned_gross": premium, "minimum_wage_makeup": 0, "gross_pay": premium,
			"deduction_lines": lines, "deduction_shortfalls": shortfalls,
			"garnishment_total": round(garnished, 2),
			"post_tax_deductions": round(_num(post.get("total")), 2), "pre_tax_deductions": 0,
			"total_deduction_withholdings": withholdings, "disposable_earnings": disposable,
			"total_deductions": round(statutory + withholdings, 2), "net_pay": round(premium - statutory - withholdings, 2),
			"settlement": {"week": [start, end], "days_paid": [d["day"] for d in days],
			               "daily_entries": sorted({d["entry"] for d in days}), "week_hours": hours,
			               "straight_time_earnings": straight, "regular_rate": regular_rate,
			               "overtime_threshold": threshold, "multiplier": multiplier, "premium": premium,
			               "week_gross": week_gross, "week_disposable_earnings": disposable,
			               "open_from_last_week": _open_shortfalls(employee, company, start),
			               "rule": rule.get("version")},
		})
		slips.append(slip)
	return slips, held, notes
