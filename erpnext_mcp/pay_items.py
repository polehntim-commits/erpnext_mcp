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
          "work_state")
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
