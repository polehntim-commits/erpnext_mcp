# SPDX-License-Identifier: MIT
"""Oregon sick time, accrued by payroll and used through pay items. v0.301.0 (Tim, 2026-10-10: align with BOLI).

ORS 653.601-.661 and OAR 839-007, as `payroll_settings.sick_time_policy` states them (dated, sourced, published by a
person): one hour per 30 Oregon hours worked, from the first day; capped at 40 accrued a year; up to 40 carried into
the next year; the balance capped at 80; up to 40 used a year; usable from the 91st day; restored on rehire within 180
days. Paid where the employer has 10 or more employees (the policy's `paid`: auto, true or false).

THE LEDGER (Farm Sick Time Ledger) holds every accrual a saved run made and every use it paid. Rows from a cancelled
run never count, so re-running a period cannot double anything — the same rule as the garnishment ledger.

WASHINGTON (v0.305.0). Hours worked in Washington accrue Washington paid sick leave (RCW 49.46.210) into a separate
balance — the ledger's `work_state` — under `sick_time_policy.washington`: one hour per 40, no annual accrual or use cap,
up to 40 carried over, usable from the 90th day, restored on rehire within 12 months, paid at every employer size.

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
#: v0.305.0. The leave types filed through `create_leave_request` / read by `get_leave_balance`, and their state.
LEAVE_TYPES = {"oregon sick time": "OR", "washington paid sick leave": "WA", "washington sick leave": "WA"}
LEAVE_NAMES = {"OR": "Oregon Sick Time", "WA": "Washington Paid Sick Leave"}
STATES = ("OR", "WA")


def state_of_leave(leave_type) -> str:
	"""The state a sick-leave type is under, or "" for an HR leave type."""
	return LEAVE_TYPES.get(str(leave_type or "").strip().lower(), "")


def ready() -> bool:
	return compat.doctype_exists(LEDGER)


def _num(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _date(value) -> datetime.date:
	return value if isinstance(value, datetime.date) else datetime.date.fromisoformat(str(value)[:10])


def _policy(as_of: str = "", state: str = "OR") -> dict:
	"""The policy for one state: the flat Oregon keys, or the `washington` block."""
	from . import payroll_settings

	policy = payroll_settings.sick_time_policy(as_of)
	if state == "WA":
		return {**policy["washington"], "version": policy.get("version"), "states": ["WA"]}
	return policy


def _cap(policy: dict, key: str):
	"""A cap, or None where the law sets none (Washington's accrual, use and balance)."""
	value = policy.get(key)
	return None if value in (None, "") else _num(value)


def _row_state(row) -> str:
	return str(row.get("work_state") or "OR")


def _live_rows(employee: str, until: str = "", state: str = "OR") -> list:
	fields = ["posting_date", "kind", "hours", "hours_worked", "payroll_entry", "pay_item"]
	fields += compat.existing_fields(LEDGER, ("work_state",))
	rows = [dict(r) for r in frappe.db.get_all(
		LEDGER, filters={"employee": employee},
		fields=fields,
		order_by="posting_date asc", limit=10000) or []]
	entries = {r["payroll_entry"] for r in rows if r.get("payroll_entry")}
	cancelled = set()
	if entries:
		cancelled = {r["name"] for r in frappe.db.get_all(ENTRY, filters={"name": ("in", list(entries))},
		                                                     fields=["name", "status"]) or []
		             if r.get("status") == "Cancelled"}
	out = [r for r in rows if not (r.get("payroll_entry") and r["payroll_entry"] in cancelled)]
	return [r for r in out if (not until or str(r["posting_date"])[:10] <= until) and _row_state(r) == state]


def _restarted_on(employee: str, state: str = "OR") -> str:
	"""The date the balance starts from: the hire date, unless a rehire came within the restore window."""
	fields = list(compat.existing_fields(EMPLOYEE, ("date_of_joining", "relieving_date")))
	row = frappe.db.get_value(EMPLOYEE, employee, fields, as_dict=True) or {}
	joined = str(row.get("date_of_joining") or "")[:10]
	left = str(row.get("relieving_date") or "")[:10]
	days = int(_policy("", state).get("rehire_restore_days") or 180)
	if joined and left and left < joined and (_date(joined) - _date(left)).days > days:
		return joined
	return ""


def balance(employee: str, as_of: str = "", state: str = "OR") -> dict:
	"""`{balance, accrued_this_year, used_this_year, carried_in, eligible_from, usable}` on `as_of`.

	Carryover applies at each year boundary (the balance going into a year is capped at `carryover_cap`); the balance
	never exceeds `balance_cap`; a rehire after more than `rehire_restore_days` starts from zero."""
	as_of = str(as_of or frappe.utils.today())[:10]
	policy = _policy(as_of, state)
	start = _restarted_on(employee, state)
	rows = [r for r in _live_rows(employee, as_of, state) if not start or str(r["posting_date"])[:10] >= start]
	carry_cap, balance_cap = _cap(policy, "carryover_cap"), _cap(policy, "balance_cap")
	running, year, carried = 0.0, None, 0.0
	accrued_year = used_year = 0.0
	for row in rows:
		row_year = int(str(row["posting_date"])[:4])
		if year is not None and row_year != year:
			running = running if carry_cap is None else min(running, carry_cap)
			carried = running
			accrued_year = used_year = 0.0
		year = row_year
		hours = _num(row.get("hours"))
		if hours > 0:
			running = running + hours if balance_cap is None else min(running + hours, balance_cap)
			# v0.305.0. An opening balance (Adjustment) is a balance carried in, not this year's accrual.
			if row.get("kind") != "Adjustment":
				accrued_year += hours
		else:
			running += hours
			if row.get("kind") != "Adjustment":
				used_year += -hours
	if year is not None and year != int(as_of[:4]):
		running = running if carry_cap is None else min(running, carry_cap)
		carried, accrued_year, used_year = running, 0.0, 0.0
	joined = str(frappe.db.get_value(EMPLOYEE, employee, "date_of_joining") or "")[:10]
	eligible = (_date(start or joined) + datetime.timedelta(days=int(policy.get("eligible_after_days") or 90))
	            ).isoformat() if (start or joined) else ""
	return {"state": state, "leave_type": LEAVE_NAMES[state],
	        "balance": round(max(running, 0.0), 2), "accrued_this_year": round(accrued_year, 2),
	        "used_this_year": round(used_year, 2), "carried_in": round(carried, 2), "eligible_from": eligible or None,
	        "usable": (not eligible) or as_of >= eligible, "policy": policy.get("version"),
	        "source": policy.get("source")}


def paid(company: str, as_of: str = "", state: str = "OR") -> bool:
	"""Whether sick time is paid at this employer: the policy's `paid`, or (auto) 10+ employees. Washington: always."""
	setting = _policy(as_of, state).get("paid", "auto")
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


def wa_rate(company: str, employee: str, sick_day: str, structure: dict, minimum_wage: float, as_of: str = "") -> tuple:
	"""v0.307.0. Washington paid sick leave's hourly rate and the evidence for it: the greater of the Washington minimum
	wage and the worker's normal hourly compensation (WAC 296-128-670; L&I ES.B.2), by the ONE method the policy names
	for the worker's job class (670(6)).

	recent_similar_workweek: total earnings / total hours in the most recent COMPLETED workweek, before the leave, of
	identical work (the same pay type and the same kind of shift as the worker's latest) or else substantially similar
	work (the same pay type). Earnings are piece, hourly and paid-rest pay plus non-discretionary bonuses earned that
	week; the overtime premium, tips, holiday pay, reimbursements and discretionary bonuses are not. With no such week
	in `rate_lookback_weeks`: the greater of the most recent agricultural workweek's rate and the minimum wage, flagged
	for attorney review."""
	policy = _policy(as_of or sick_day, "WA")
	job_class = str(structure.get("pay_type") or "Hourly")
	method = (policy.get("rate_method") or {}).get(job_class) or "recent_similar_workweek"
	evidence = {"job_class": job_class, "method": method, "minimum_wage": round(minimum_wage, 2),
	            "source": policy.get("rate_source"), "policy": policy.get("version"), "attorney_review": False}
	if method == "hourly_rate":
		hourly = _num(structure.get("base_rate")) if job_class == "Hourly" else _num(structure.get("hourly_rate"))
		rate = round(max(hourly, minimum_wage), 2)
		return rate, {**evidence, "tier": "hourly_rate", "hourly_rate": round(hourly, 4), "rate": rate}
	week = _similar_week(company, employee, str(sick_day)[:10], structure, int(policy.get("rate_lookback_weeks") or 12))
	if not week:
		rate = round(minimum_wage, 2)
		return rate, {**evidence, "tier": "fallback_minimum_wage", "rate": rate, "attorney_review": True,
		              "note": "no agricultural workweek on record before the leave; paid at the Washington minimum wage"}
	normal = (week["straight_time_pay"] + week["bonus"]) / week["hours"] if week["hours"] > 0 else 0.0
	rate = round(max(normal, minimum_wage), 2)
	return rate, {**evidence, **{k: v for k, v in week.items() if k != "aggregate"},
	              "normal_hourly_compensation": round(normal, 4), "rate": rate,
	              "attorney_review": week["tier"] == "fallback_recent_ag_week"}


def _similar_week(company: str, employee: str, sick_day: str, structure: dict, lookback: int) -> dict:
	"""The workweek `wa_rate` reads, newest first: identical → substantially similar → the most recent ag week."""
	from . import daily_pay, payroll_integration
	from .tools import payroll as runs

	this_week = daily_pay.workweek_start(sick_day)
	first = (this_week - datetime.timedelta(days=7 * lookback)).isoformat()
	last = (this_week - datetime.timedelta(days=1)).isoformat()
	try:
		shifts, _ = runs._load_period_shifts(company, first, last, employees=[employee])
	except Exception:  # pragma: no cover - a half-migrated register reads as no history
		shifts = []
	job_class = str(structure.get("pay_type") or "Hourly")

	def starts(shift):
		return str(shift.get("start_datetime") or shift.get("start") or "")[:10]

	def pay_type_of(shift):
		mine = [r for r in shift.get("crew") or [] if r.get("employee") == employee]
		return str((mine[0].get("pay_type") if mine else "") or shift.get("pay_type") or job_class)

	def state_of(shift):
		mine = [r for r in shift.get("crew") or [] if r.get("employee") == employee]
		return str((mine[0].get("work_state") if mine else "") or shift.get("work_state") or "")

	washington = sorted([s for s in shifts or [] if state_of(s) == "WA" and starts(s)], key=starts, reverse=True)
	if not washington:
		return {}
	kind = str(washington[0].get("shift_type") or "")
	weeks: dict = {}
	for shift in washington:
		weeks.setdefault(daily_pay.workweek_start(starts(shift)).isoformat(), []).append(shift)
	ordered = sorted(weeks, reverse=True)
	tiers = (("identical", lambda ws: any(pay_type_of(x) == job_class and str(x.get("shift_type") or "") == kind
	                                      for x in ws)),
	         ("substantially_similar", lambda ws: any(pay_type_of(x) == job_class for x in ws)),
	         ("fallback_recent_ag_week", lambda ws: True))
	for tier, matches in tiers:
		for start in ordered:
			if not matches(weeks[start]):
				continue
			end = (datetime.date.fromisoformat(start) + datetime.timedelta(days=6)).isoformat()
			aggregate = payroll_integration.aggregate_shifts_for_period(
				weeks[start], start, end, overtime_threshold=1e9, workweek_anchor=start).get(employee) or {}
			row = (payroll_integration.weekly_regular_rates(aggregate, structure).get("weeks") or [{}])[0]
			if _num(row.get("hours")) <= 0:
				continue
			bonuses = _bonuses_earned(employee, start, end)
			return {"tier": tier, "week_start": start, "week_end": end, "shift_type": kind or None,
			        "hours": round(_num(row.get("hours")), 2),
			        "straight_time_pay": round(_num(row.get("straight_time_pay")), 2),
			        "bonus": round(sum(b["allocated"] for b in bonuses), 2), "bonuses": bonuses}
	return {}


def _bonuses_earned(employee: str, start: str, end: str) -> list:
	"""Non-discretionary pay items earned in the week, each pro-rated by the days of its earning window inside it."""
	from . import pay_items, payroll_settings

	if not pay_items.ready():
		return []
	fields = ["name", "kind", "amount", "earned_from", "earned_to", "pay_on", "status"]
	out = []
	for row in frappe.db.get_all("Farm Pay Item", filters={"employee": employee}, fields=fields, limit=2000) or []:
		if row.get("status") == "Cancelled" or row.get("kind") == KIND or _num(row.get("amount")) <= 0:
			continue
		if payroll_settings.is_discretionary(row.get("kind"), end):
			continue
		lo = str(row.get("earned_from") or row.get("pay_on") or "")[:10]
		hi = str(row.get("earned_to") or lo)[:10]
		if not lo or hi < start or lo > end:
			continue
		days = (_date(hi) - _date(lo)).days + 1
		inside = (_date(min(hi, end)) - _date(max(lo, start))).days + 1
		out.append({"item": row["name"], "kind": row.get("kind"), "amount": _num(row["amount"]),
		            "allocated": round(_num(row["amount"]) * inside / max(days, 1), 2)})
	return out


def price(company: str, employee: str, rows: list, structure: dict, minimum_wage: float, as_of: str,
          state: str = "OR") -> list:
	"""Each Sick Time pay item priced and limited to what the worker may use; what cannot be paid now waits with a
	reason. Returns the rows to pay (amount set) — the others are left Pending and listed in `waiting`. `rows` are
	all one state's (the caller groups them by `work_state`)."""
	usable = balance(employee, as_of, state)
	policy = _policy(as_of, state)
	use_cap = _cap(policy, "annual_use_cap")
	available = usable["balance"] if use_cap is None else min(
		usable["balance"], max(use_cap - usable["used_this_year"], 0.0))
	is_paid = paid(company, as_of, state)
	hourly = rate(structure, minimum_wage)
	out = []
	for row in rows:
		hours = _num(row.get("hours"))
		if not usable["usable"]:
			row["waiting"] = f"not usable until {usable['eligible_from']} " + (
				"(the 91st day of employment)" if state == "OR" else "(the 90th calendar day after starting)")
			continue
		take = round(min(hours, available), 2)
		if take <= 0:
			row["waiting"] = ("no sick time balance left" if use_cap is None
			                  else f"no sick time balance left (or the {use_cap:g}-hour annual use cap is reached)")
			continue
		available -= take
		# v0.307.0. Washington: normal hourly compensation per WAC 296-128-670, with the evidence kept on the item.
		row_rate, evidence = hourly, {"rule": "OAR 839-007-0000(7)", "rate": hourly}
		if state == "WA":
			row_rate, evidence = wa_rate(company, employee, str(row.get("pay_on") or as_of), structure, minimum_wage,
			                             as_of)
		out.append({**row, "hours": take, "amount": round(take * row_rate, 2) if is_paid else 0.0,
		            "treatment": "Wages", "sick_time_rate": row_rate if is_paid else 0.0,
		            "sick_time_rate_evidence": evidence,
		            "sick_time_paid": is_paid, "work_state": state,
		            "sick_time_note": ("" if take == hours else f"{hours - take:g} h beyond the balance not paid")})
	return out


def record(entry: str, slips: list, company: str, period_end: str) -> list:
	"""After a saved run: an Accrual row per employee and state for hours worked there (Oregon capped at the year's
	accrual cap; Washington uncapped), and a Use row per Sick Time pay item paid, in the item's state."""
	if not ready():
		return []
	oregon = _policy(period_end, "OR")
	accrues_in = {"OR": set(oregon.get("states") or ["OR"]), "WA": {"WA"}}
	written = []
	for slip in slips or []:
		employee = slip.get("employee")
		if not employee:
			continue
		by_state = slip.get("state_hours") or slip.get("hours_by_state") or {}
		for ledger_state in STATES:
			policy = _policy(period_end, ledger_state)
			per = _num(policy.get("accrual_per_hours_worked")) or (30.0 if ledger_state == "OR" else 40.0)
			cap = _cap(policy, "annual_accrual_cap")
			worked = 0.0
			for state, value in by_state.items():
				if state in accrues_in[ledger_state]:
					worked += _num(value.get("hours")) if isinstance(value, dict) else _num(value)
			if worked <= 0:
				continue
			earned = worked / per
			if cap is not None:
				earned = min(earned, max(cap - balance(employee, period_end, ledger_state)["accrued_this_year"], 0.0))
			hours = round(earned, 4)
			if hours > 0:
				written.append(_row(employee, company, period_end, "Accrual", hours, entry, worked=worked,
				                    state=ledger_state))
		for item in slip.get("pay_items") or []:
			if item.get("kind") == KIND and _num(item.get("hours")) > 0 and item.get("name"):
				state = "WA" if str(item.get("work_state") or "") == "WA" else "OR"
				written.append(_row(employee, company, period_end, "Use", -_num(item["hours"]), entry,
				                    pay_item=item["name"], state=state))
	return written


def opening(company: str, posting_date: str, rows: list, note: str) -> list:
	"""v0.305.0. Opening balances carried in from before this ledger (`set_opening_balance kind=sick_time`): one
	Adjustment row per employee and state. Every row is checked before any is written; an employee who already has
	an opening balance for that state is refused (a correction is a further adjustment, not a second opening)."""
	from .errors import ToolError

	if not ready():
		raise ToolError("the Farm Sick Time Ledger is not installed on this site — run `bench migrate`.")
	if not isinstance(rows, list) or not rows:
		raise ToolError('balances is a non-empty list: [{"employee": "HR-EMP-00012", "hours": 12.5, '
		                '"work_state": "OR"}, …]. Nothing was written.')
	checked, seen = [], set()
	for index, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			raise ToolError(f"balances[{index}] is not an object. Nothing was written.")
		unknown = set(row) - {"employee", "hours", "work_state", "note"}
		if unknown:
			raise ToolError(f"balances[{index}] has unknown key(s): {', '.join(sorted(unknown))}. Nothing was written.")
		employee = str(row.get("employee") or "").strip()
		state = str(row.get("work_state") or "OR").strip().upper()
		if state not in STATES:
			raise ToolError(f"balances[{index}]: work_state is OR or WA, not {state!r}. Nothing was written.")
		try:
			hours = float(row.get("hours"))
		except (TypeError, ValueError):
			raise ToolError(f"balances[{index}]: hours is a number. Nothing was written.") from None
		if hours <= 0:
			raise ToolError(f"balances[{index}]: an opening balance is a positive number of hours. Nothing was written.")
		emp = frappe.db.get_value(EMPLOYEE, employee, ["name", "company"], as_dict=True) if employee else None
		if not emp:
			raise ToolError(f"balances[{index}]: no employee {employee!r}. Nothing was written.")
		if emp.get("company") != company:
			raise ToolError(f"balances[{index}]: {employee} works for {emp.get('company')}, not {company}. "
			                "Nothing was written.")
		cap = _cap(_policy(posting_date, state), "balance_cap")
		if cap is not None and hours > cap:
			raise ToolError(f"balances[{index}]: {hours:g} h is over the {cap:g}-hour balance cap for "
			                f"{LEAVE_NAMES[state]}. Nothing was written.")
		if (employee, state) in seen:
			raise ToolError(f"balances[{index}]: {employee} is listed twice for {state}. Nothing was written.")
		seen.add((employee, state))
		if any(str(r.get("note") or "").startswith("Opening balance") for r in _adjustments(employee, state)):
			raise ToolError(f"balances[{index}]: {employee} already has an opening {LEAVE_NAMES[state]} balance. "
			                "Correct it with an adjustment rather than a second opening. Nothing was written.")
		checked.append((employee, state, round(hours, 2), str(row.get("note") or "").strip()))
	return [_row(employee, company, posting_date, "Adjustment", hours, None, state=state,
	             note=f"Opening balance: {note}" + (f" — {row_note}" if row_note else ""))
	        for employee, state, hours, row_note in checked]


def _adjustments(employee: str, state: str) -> list:
	fields = ["kind", "note"] + compat.existing_fields(LEDGER, ("work_state",))
	return [r for r in (frappe.db.get_all(LEDGER, filters={"employee": employee, "kind": "Adjustment"},
	                                      fields=fields, limit=1000) or []) if _row_state(r) == state]


def _row(employee, company, day, kind, hours, entry, worked=None, pay_item=None, state="OR", note=None) -> dict:
	values = {"doctype": LEDGER, "employee": employee, "company": company, "posting_date": day, "kind": kind,
	          "hours": hours, "hours_worked": worked, "payroll_entry": entry, "pay_item": pay_item, "note": note}
	if compat.has_field(LEDGER, "work_state"):
		values["work_state"] = state
	doc = frappe.get_doc(values)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return {"employee": employee, "kind": kind, "hours": hours, "work_state": state}
