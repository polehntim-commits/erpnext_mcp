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
