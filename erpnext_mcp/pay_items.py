# SPDX-License-Identifier: MIT
"""Bonuses, commissions and profit-share payouts paid with a pay run (Farm Pay Item). v0.292.0.

A pay item is Pending until a SAVED run whose period covers its `pay_on` date pays it; the run marks it Paid and
links itself. v0.293.0: an item whose run was CANCELLED (or deleted) is payable again — the next run picks it up and
cancelling a run puts its items back to Pending — and a worker with no salary structure and no hours in the period is
paid the item on its own slip. The engine (payroll_calc) taxes "Wages" items with the period's pay
and adds "Not subject to withholding" items (a fishing-boat crew share, IRC 3121(b)(20)) after the withholding; both
are earnings for the garnishment caps (15 U.S.C. 1672(a)). Treatment per kind is the CPA's call.
"""

from __future__ import annotations

import frappe

from . import compat

PAY_ITEM = "Farm Pay Item"
FIELDS = ("name", "employee", "kind", "amount", "pay_on", "treatment", "reference", "status", "payroll_entry",
          "work_state", "earned_from", "earned_to")
ENTRY = "Farm Payroll Entry"
EMPLOYEE = "Employee"


def ready() -> bool:
	return compat.doctype_exists(PAY_ITEM)


def _released(names: set) -> set:
	"""Of these payroll entries, the ones whose items are payable again: cancelled, or no longer there."""
	if not names:
		return set()
	rows = frappe.db.get_all(ENTRY, filters={"name": ("in", list(names))}, fields=["name", "status"], limit=10000) or []
	live = {r["name"] for r in rows if r.get("status") != "Cancelled"}
	return set(names) - live


def pending(company: str, start: str, end: str, employees: list | None = None) -> dict:
	"""Employee → the pay items payable with a run for the period: Pending ones dated inside it, and Paid ones whose
	run was cancelled since (each marked `released_from`)."""
	if not ready():
		return {}
	filters = {"company": company, "status": ("in", ["Pending", "Paid"]),
	           "pay_on": ("between", [str(start)[:10], str(end)[:10]])}
	if employees:
		filters["employee"] = ("in", list(employees))
	fields = list(compat.existing_fields(PAY_ITEM, FIELDS))
	rows = [dict(r) for r in frappe.db.get_all(PAY_ITEM, filters=filters, fields=fields, order_by="pay_on asc",
	                                           limit=5000) or []]
	released = _released({r.get("payroll_entry") for r in rows if r.get("status") == "Paid" and r.get("payroll_entry")})
	out: dict = {}
	for row in rows:
		if row.get("status") == "Paid":
			if not row.get("payroll_entry") or row["payroll_entry"] not in released:
				continue
			row["released_from"] = row["payroll_entry"]
		out.setdefault(str(row.get("employee") or ""), []).append(row)
	return out


def release(entry: str) -> list:
	"""A cancelled run's items back to Pending. Returns the names."""
	if not ready() or not entry:
		return []
	names = [r["name"] for r in frappe.db.get_all(PAY_ITEM, filters={"payroll_entry": entry, "status": "Paid"},
	                                              fields=["name"], limit=5000) or []]
	for name in names:
		frappe.db.set_value(PAY_ITEM, name, {"status": "Pending", "payroll_entry": None})
	return names


def stand_in(employee: str, company: str) -> dict:
	"""What a settlement reads off a worker's daily slips, for a daily-pay worker paid only an item this week."""
	name = frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
	structure = ""
	if compat.doctype_exists("Farm Salary Structure"):
		structure = frappe.db.get_value("Farm Salary Structure", {"employee": employee, "company": company,
		                                                          "is_active": 1}, "name") or ""
	return {"employee": employee, "employee_name": name, "salary_structure": structure}


def work_state(rows: list | None, employee: str, company: str = "") -> str:
	"""Whose income tax is withheld on an item paid with no hours: the item's own `work_state`, else the state on the
	worker's most recent slip, else blank (no state withholding — reported on the run)."""
	for row in rows or []:
		if str(row.get("work_state") or "").strip():
			return str(row["work_state"]).strip().upper()
	try:
		last = frappe.db.get_all("Farm Payroll Slip", filters={"employee": employee, "work_state": ("!=", "")},
		                         fields=["work_state"], order_by="creation desc", limit=1) or []
	except Exception:  # a site whose slips cannot be read this way just has no fallback
		last = []
	return str((last[0].get("work_state") if last else "") or "").upper()


def mark_paid(entry: str, slips: list) -> list:
	"""Every pay item a saved run's slips carried is Paid, linked to the entry. Returns the names."""
	if not ready():
		return []
	done = []
	for slip in slips or []:
		for item in slip.get("pay_items") or []:
			name = item.get("name")
			if name and frappe.db.exists(PAY_ITEM, name):
				frappe.db.set_value(PAY_ITEM, name, {"status": "Paid", "payroll_entry": entry})
				done.append(name)
	return done


# ── v0.296.0: bonuses in the overtime regular rate (29 CFR 778.208-.210) ─────────────────────────────────────────
def _workweeks(start: str, end: str) -> tuple:
	"""The earning window snapped out to whole workweeks (Pay Policy workweek_start)."""
	import datetime

	from . import daily_pay

	first = daily_pay.workweek_start(start)
	last = daily_pay.workweek_start(end) + datetime.timedelta(days=6)
	return first.isoformat(), last.isoformat()


def regular_rate_premium(amount: float, weeks: list, multiplier: float) -> dict:
	"""Pure. The overtime premium a bonus adds when allocated hour by hour over the workweeks it was earned in
	(778.209(b)): each week's share is amount x week hours / all hours, raising that week's regular rate by
	share / week hours — the same amount per hour in every week — and each overtime hour is owed (multiplier - 1)
	times that increase."""
	total_hours = sum(float(w.get("hours") or 0) for w in weeks)
	if amount <= 0 or total_hours <= 0:
		return {"premium": 0.0, "per_hour": 0.0, "weeks": [], "total_hours": round(total_hours, 2)}
	per_hour = amount / total_hours
	rows, premium = [], 0.0
	for week in weeks:
		hours = float(week.get("hours") or 0)
		overtime = float(week.get("overtime_hours") or 0)
		owed = per_hour * (multiplier - 1.0) * overtime
		premium += owed
		rows.append({"week_start": week.get("week_start"), "week_end": week.get("week_end"), "hours": round(hours, 2),
		             "overtime_hours": round(overtime, 2), "bonus_share": round(per_hour * hours, 2),
		             "regular_rate_increase": round(per_hour, 4), "overtime_premium": round(owed, 2)})
	return {"premium": round(premium, 2), "per_hour": round(per_hour, 4), "weeks": rows,
	        "total_hours": round(total_hours, 2)}


def regular_rate_items(company: str, items_by_employee: dict, period_start: str, period_end: str) -> dict:
	"""For each non-discretionary pay item, the overtime it adds to the regular rate, as a derived WAGES item paid
	with it. Employee → [derived items]. The hours come off the shift register for the weeks the bonus was earned in,
	with the overtime threshold in force (Pay Policy, never below the state's agricultural threshold)."""
	from . import payroll_integration, payroll_settings
	from .tools import payroll as runs

	policy = payroll_settings.pay_item_policy(period_end)
	out: dict = {}
	for employee, rows in (items_by_employee or {}).items():
		for row in rows:
			if row.get("derived_from") or float(row.get("amount") or 0) <= 0:
				continue
			if payroll_settings.is_discretionary(row.get("kind"), policy=policy):
				continue
			start = str(row.get("earned_from") or period_start)[:10]
			end = str(row.get("earned_to") or row.get("earned_from") or period_end)[:10]
			if end < start:
				start, end = end, start
			first, last = _workweeks(start, end)
			shifts, _provenance = runs._load_period_shifts(company, first, last, employees=[employee])
			state = str(row.get("work_state") or "")
			rule = payroll_settings.overtime(last, state=state)
			aggregate = payroll_integration.aggregate_shifts_for_period(
				shifts, first, last, overtime_threshold=float(rule["weekly_threshold_hours"]),
				workweek_anchor=first).get(employee) or {}
			if not state and aggregate.get("hours_by_state"):
				state = max(aggregate["hours_by_state"], key=aggregate["hours_by_state"].get)
				rule = payroll_settings.overtime(last, state=state)
				aggregate = payroll_integration.aggregate_shifts_for_period(
					shifts, first, last, overtime_threshold=float(rule["weekly_threshold_hours"]),
					workweek_anchor=first).get(employee) or {}
			result = regular_rate_premium(float(row["amount"]), aggregate.get("weeks") or [], float(rule["multiplier"]))
			if result["premium"] <= 0:
				continue
			out.setdefault(employee, []).append({
				"name": None, "derived_from": row.get("name"), "kind": "Overtime on bonus (regular rate)",
				"amount": result["premium"], "treatment": "Wages", "work_state": row.get("work_state") or state,
				"pay_on": row.get("pay_on"),
				"regular_rate": {**result, "bonus": float(row["amount"]), "bonus_kind": row.get("kind"),
				                 "earned": [first, last], "threshold_hours": rule["weekly_threshold_hours"],
				                 "multiplier": rule["multiplier"], "policy": policy.get("version"),
				                 "source": policy.get("source")}})
	return out
