# SPDX-License-Identifier: MIT
"""Oregon sick time, accrued by payroll and used through pay items. v0.301.0 (Tim, 2026-10-10: align with BOLI).

ORS 653.601-.661 and OAR 839-007, as `payroll_settings.sick_time_policy` states them (dated, sourced, published by a
person): one hour per 30 Oregon hours worked, from the first day; capped at 40 accrued a year; up to 40 carried into
the next year; the balance capped at 80; up to 40 used a year; usable from the 91st day; restored on rehire within 180
days. Paid where the employer has 10 or more employees (the policy's `paid`: auto, true or false).

THE LEDGER (Farm Sick Time Ledger) holds every accrual a saved run made and every use it paid. Rows from a cancelled
run never count, so re-running a period cannot double anything — the same rule as the garnishment ledger.

USE is a Farm Pay Item of kind "Sick Time" with hours — filed through `create_leave_request leave_type="Oregon Sick
Time"` — priced by the run at the rate OAR 839-007-0000(7) sets: the regular hourly rate; for a worker paid only by
the piece, no less than minimum wage; for hourly plus piece, the greater of the hourly rate and minimum wage. It is
pay for time not worked: never in the overtime regular rate, never hours worked.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat

LEDGER = "Farm Sick Time Ledger"
ENTRY = "Farm Payroll Entry"
EMPLOYEE = "Employee"
KIND = "Sick Time"
LEAVE_TYPE = "Oregon Sick Time"


def ready() -> bool:
	return compat.doctype_exists(LEDGER)


def _num(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _date(value) -> datetime.date:
	return value if isinstance(value, datetime.date) else datetime.date.fromisoformat(str(value)[:10])


def _policy(as_of: str = "") -> dict:
	from . import payroll_settings

	return payroll_settings.sick_time_policy(as_of)


def _live_rows(employee: str, until: str = "") -> list:
	rows = [dict(r) for r in frappe.db.get_all(
		LEDGER, filters={"employee": employee},
		fields=["posting_date", "kind", "hours", "hours_worked", "payroll_entry", "pay_item"],
		order_by="posting_date asc", limit=10000) or []]
	entries = {r["payroll_entry"] for r in rows if r.get("payroll_entry")}
	cancelled = set()
	if entries:
		cancelled = {r["name"] for r in frappe.db.get_all(ENTRY, filters={"name": ("in", list(entries))},
		                                                     fields=["name", "status"]) or []
		             if r.get("status") == "Cancelled"}
	out = [r for r in rows if not (r.get("payroll_entry") and r["payroll_entry"] in cancelled)]
	return [r for r in out if not until or str(r["posting_date"])[:10] <= until]


def _restarted_on(employee: str) -> str:
	"""The date the balance starts from: the hire date, unless a rehire came within the restore window."""
	fields = list(compat.existing_fields(EMPLOYEE, ("date_of_joining", "relieving_date")))
	row = frappe.db.get_value(EMPLOYEE, employee, fields, as_dict=True) or {}
	joined = str(row.get("date_of_joining") or "")[:10]
	left = str(row.get("relieving_date") or "")[:10]
	days = int(_policy().get("rehire_restore_days") or 180)
	if joined and left and left < joined and (_date(joined) - _date(left)).days > days:
		return joined
	return ""


def balance(employee: str, as_of: str = "") -> dict:
	"""`{balance, accrued_this_year, used_this_year, carried_in, eligible_from, usable}` on `as_of`.

	Carryover applies at each year boundary (the balance going into a year is capped at `carryover_cap`); the balance
	never exceeds `balance_cap`; a rehire after more than `rehire_restore_days` starts from zero."""
	as_of = str(as_of or frappe.utils.today())[:10]
	policy = _policy(as_of)
	start = _restarted_on(employee)
	rows = [r for r in _live_rows(employee, as_of) if not start or str(r["posting_date"])[:10] >= start]
	running, year, carried = 0.0, None, 0.0
	accrued_year = used_year = 0.0
	for row in rows:
		row_year = int(str(row["posting_date"])[:4])
		if year is not None and row_year != year:
			running = min(running, _num(policy.get("carryover_cap")))
			carried = running
			accrued_year = used_year = 0.0
		year = row_year
		hours = _num(row.get("hours"))
		running = min(running + hours, _num(policy.get("balance_cap")) or 1e9) if hours > 0 else running + hours
		if hours > 0:
			accrued_year += hours
		else:
			used_year += -hours
	if year is not None and year != int(as_of[:4]):
		running = min(running, _num(policy.get("carryover_cap")))
		carried, accrued_year, used_year = running, 0.0, 0.0
	joined = str(frappe.db.get_value(EMPLOYEE, employee, "date_of_joining") or "")[:10]
	eligible = (_date(start or joined) + datetime.timedelta(days=int(policy.get("eligible_after_days") or 90))
	            ).isoformat() if (start or joined) else ""
	return {"balance": round(max(running, 0.0), 2), "accrued_this_year": round(accrued_year, 2),
	        "used_this_year": round(used_year, 2), "carried_in": round(carried, 2), "eligible_from": eligible or None,
	        "usable": (not eligible) or as_of >= eligible, "policy": policy.get("version"),
	        "source": policy.get("source")}


def paid(company: str, as_of: str = "") -> bool:
	"""Whether sick time is paid at this employer: the policy's `paid`, or (auto) 10+ employees."""
	setting = _policy(as_of).get("paid", "auto")
	if isinstance(setting, bool):
		return setting
	if str(setting).lower() in ("true", "false"):
		return str(setting).lower() == "true"
	count = len(frappe.db.get_all(EMPLOYEE, filters={"company": company, "status": "Active"}, pluck="name",
	                              limit=10000) or [])
	return count >= int(_policy(as_of).get("paid_min_employees") or 10)


def rate(structure: dict, minimum_wage: float) -> float:
	"""OAR 839-007-0000(7): the regular hourly rate; piece-only no less than minimum wage; hourly + piece the greater
	of the hourly rate and minimum wage."""
	pay_type = str(structure.get("pay_type") or "Hourly")
	base = _num(structure.get("base_rate"))
	hourly = _num(structure.get("hourly_rate"))
	if pay_type == "Piece Rate":
		return round(max(hourly, minimum_wage), 2)
	if pay_type == "Salary":
		return round(max(hourly, minimum_wage), 2)
	return round(max(base, minimum_wage), 2)


def price(company: str, employee: str, rows: list, structure: dict, minimum_wage: float, as_of: str) -> list:
	"""Each Sick Time pay item priced and limited to what the worker may use; what cannot be paid now waits with a
	reason. Returns the rows to pay (amount set) — the others are left Pending and listed in `waiting`."""
	usable = balance(employee, as_of)
	policy = _policy(as_of)
	available = min(usable["balance"], max(_num(policy.get("annual_use_cap")) - usable["used_this_year"], 0.0))
	is_paid = paid(company, as_of)
	hourly = rate(structure, minimum_wage)
	out = []
	for row in rows:
		hours = _num(row.get("hours"))
		if not usable["usable"]:
			row["waiting"] = f"not usable until {usable['eligible_from']} (the 91st day of employment)"
			continue
		take = round(min(hours, available), 2)
		if take <= 0:
			row["waiting"] = "no sick time balance left (or the 40-hour annual use cap is reached)"
			continue
		available -= take
		out.append({**row, "hours": take, "amount": round(take * hourly, 2) if is_paid else 0.0,
		            "treatment": "Wages", "sick_time_rate": hourly if is_paid else 0.0,
		            "sick_time_paid": is_paid,
		            "sick_time_note": ("" if take == hours else f"{hours - take:g} h beyond the balance not paid")})
	return out


def record(entry: str, slips: list, company: str, period_end: str) -> list:
	"""After a saved run: an Accrual row per employee for Oregon hours worked (capped at the year's accrual cap), and
	a Use row per Sick Time pay item paid."""
	if not ready():
		return []
	policy = _policy(period_end)
	states = set(policy.get("states") or ["OR"])
	per = _num(policy.get("accrual_per_hours_worked")) or 30.0
	cap = _num(policy.get("annual_accrual_cap")) or 40.0
	written = []
	for slip in slips or []:
		employee = slip.get("employee")
		if not employee:
			continue
		worked = 0.0
		for state, value in (slip.get("state_hours") or slip.get("hours_by_state") or {}).items():
			if state in states:
				worked += _num(value.get("hours")) if isinstance(value, dict) else _num(value)
		if worked > 0:
			so_far = balance(employee, period_end)["accrued_this_year"]
			hours = round(min(worked / per, max(cap - so_far, 0.0)), 4)
			if hours > 0:
				written.append(_row(employee, company, period_end, "Accrual", hours, entry, worked=worked))
		for item in slip.get("pay_items") or []:
			if item.get("kind") == KIND and _num(item.get("hours")) > 0 and item.get("name"):
				written.append(_row(employee, company, period_end, "Use", -_num(item["hours"]), entry,
				                    pay_item=item["name"]))
	return written


def _row(employee, company, day, kind, hours, entry, worked=None, pay_item=None) -> dict:
	doc = frappe.get_doc({"doctype": LEDGER, "employee": employee, "company": company, "posting_date": day,
	                      "kind": kind, "hours": hours, "hours_worked": worked, "payroll_entry": entry,
	                      "pay_item": pay_item})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return {"employee": employee, "kind": kind, "hours": hours}
