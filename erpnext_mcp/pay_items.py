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
          "work_state", "earned_from", "earned_to", "hours", "terms_posted_on", "terms")
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
				values = {"status": "Paid", "payroll_entry": entry}
				# v0.307.0. How a sick-time rate was set (WAC 296-128-670 workweek and arithmetic), kept on the item.
				if item.get("sick_time_rate_evidence") and compat.has_field(PAY_ITEM, "rate_evidence"):
					import json

					values["rate_evidence"] = json.dumps(item["sick_time_rate_evidence"], sort_keys=True, default=str)
				frappe.db.set_value(PAY_ITEM, name, values)
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
			if row.get("derived_from") or float(row.get("amount") or 0) <= 0 or row.get("kind") == "Sick Time":
				continue  # v0.301.0: sick pay is pay for time not worked — never in the regular rate
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
			if not payroll_settings.bonuses_in_regular_rate(state, policy=policy):
				continue  # v0.298.0: Oregon leaves bonuses out of the regular rate (OAR 839-020-0030(1)).
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


def price_sick_time(company: str, items_by_employee: dict, structures: dict, min_wage_rates: dict, as_of: str) -> list:
	"""v0.301.0. Price each Sick Time item (sick_time.price) in place; returns what waits, with the reason. v0.305.0: per
	state."""
	from . import payroll_calc, sick_time

	waiting = []
	for employee, rows in list(items_by_employee.items()):
		sick = [r for r in rows if r.get("kind") == sick_time.KIND]
		if not sick:
			continue
		structure = structures.get(employee) or {}
		# v0.305.0. Each state's items against that state's balance and minimum wage (Oregon / Washington).
		priced = []
		for state in sick_time.STATES:
			group = [r for r in sick if ("WA" if str(r.get("work_state") or "") == "WA" else "OR") == state]
			if not group:
				continue
			region = ((structure.get("min_wage_regions") or {}).get(state)) or "standard"
			floor = payroll_calc.applicable_minimum_wage(state, region, min_wage_rates)
			priced += sick_time.price(company, employee, group, structure, floor, as_of, state)
		for row in sick:
			if row.get("waiting"):
				waiting.append({"employee": employee, "item": row.get("name"), "hours": row.get("hours"),
				                "reason": row["waiting"]})
		items_by_employee[employee] = [r for r in rows if r.get("kind") != sick_time.KIND] + priced
	return waiting


def unposted_bonus_terms(items_by_employee: dict) -> list:
	"""v0.302.0. OAR 839-020-0013: a grower offering a harvest bonus must post its terms — how and when it is earned
	— where the crew can read them, in their language. Bonuses with no posting date recorded are listed."""
	from . import payroll_settings

	kinds = payroll_settings.pay_item_policy().get("kinds") or {}
	out = []
	for employee, rows in (items_by_employee or {}).items():
		for row in rows:
			if row.get("kind") not in ("Bonus", "Production Bonus") or row.get("derived_from"):
				continue
			if row.get("terms_posted_on"):
				continue
			# v0.309.0. A disclosure record on the bonus KIND covers every item of it earned after it was posted.
			disclosure = (kinds.get(row.get("kind")) or {}).get("disclosure") or {}
			posted = str(disclosure.get("posted_on") or "")[:10]
			earned = str(row.get("earned_from") or row.get("pay_on") or "")[:10]
			if posted and (not earned or posted <= earned):
				continue
			out.append({"employee": employee, "item": row.get("name"), "kind": row.get("kind"),
			            "reason": "no record that the bonus terms were posted or given before it was earned (OAR "
			                      "839-020-0013) — conditions that were not disclosed cannot be used to deny or reduce "
			                      "it. Record them on the item, or as the bonus kind's disclosure (pay_item_policy)."})
	return out
