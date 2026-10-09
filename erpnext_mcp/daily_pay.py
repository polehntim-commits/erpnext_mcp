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
