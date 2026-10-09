# SPDX-License-Identifier: MIT
"""Every garnishment catch-up and carry-forward, written down. v0.292.0 (Tim's middle path, 2026-10-09).

`payroll_deductions.apply_garnishments` stays pure: it is handed each order's `arrears_balance` and reports, per
line, the current amount ordered and taken, the arrears caught up, what was carried forward, the cap and the rule
that set it. This module keeps the books: the balance is the arrears stated on the order plus every carry-forward
less every catch-up in the order's ledger — rows from a CANCELLED payroll entry never count, so re-running a period
cannot double anything — and each saved run appends one ledger row per order. The same rows are the weekly report:
who, which order, what was taken, the cap, the arrears left — what is explained to the worker and to the agency.
"""

from __future__ import annotations

import frappe

from . import compat

DEDUCTION = "Farm Payroll Deduction"
LEDGER = "Farm Payroll Deduction Ledger"
ENTRY = "Farm Payroll Entry"


def _num(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _get(row, key, default=None):
	return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def ready() -> bool:
	return compat.doctype_exists(LEDGER) and compat.has_field(DEDUCTION, "ledger")


def _live_entries(names: set) -> set:
	if not names:
		return set()
	rows = frappe.db.get_all(ENTRY, filters={"name": ("in", list(names))}, fields=["name", "status"], limit=10000) or []
	return {r["name"] for r in rows if r.get("status") != "Cancelled"}


def balance(deduction: str, opening=None) -> float:
	"""Arrears on the order now: stated arrears + carried forward − caught up, cancelled runs excluded."""
	if opening is None:
		opening = frappe.db.get_value(DEDUCTION, deduction, "arrears_opening") if compat.has_field(
			DEDUCTION, "arrears_opening") else 0
	total = _num(opening)
	if not ready():
		return round(total, 2)
	doc = frappe.get_doc(DEDUCTION, deduction)
	rows = list(doc.get("ledger") or [])
	live = _live_entries({_get(r, "payroll_entry") for r in rows if _get(r, "payroll_entry")})
	for r in rows:
		if _get(r, "payroll_entry") and _get(r, "payroll_entry") not in live:
			continue
		total += _num(_get(r, "carried_forward")) - _num(_get(r, "catch_up_taken"))
	return round(max(total, 0.0), 2)


def with_balances(rows: list) -> list:
	"""Each garnishment row given its `arrears_balance` (what `apply_garnishments` reads)."""
	from . import payroll_deductions

	for row in rows:
		if payroll_deductions.row_type(row) == "garnishment" and row.get("name"):
			row["arrears_balance"] = balance(row["name"], row.get("arrears_opening"))
	return rows


def record(entry: str, slips: list, run_kind: str = "Regular", period_end: str = "") -> list:
	"""Append the period's line to each order's ledger and refresh its balance. Returns what was written."""
	if not ready():
		return []
	written = []
	for slip in slips or []:
		for line in slip.get("deduction_lines") or []:
			if line.get("deduction_type") != "garnishment" or not line.get("deduction"):
				continue
			if not frappe.db.exists(DEDUCTION, line["deduction"]):
				continue
			doc = frappe.get_doc(DEDUCTION, line["deduction"])
			row = {"payroll_entry": entry, "pay_period_end": str(period_end or "")[:10] or None, "run_kind": run_kind,
			       "current_requested": line.get("current_requested", line.get("requested")),
			       "current_taken": line.get("current_taken", line.get("amount")),
			       "catch_up_taken": line.get("catch_up_taken", 0), "carried_forward": line.get("carried_forward", 0),
			       "cap_amount": line.get("cap_amount", line.get("limit_ceiling")), "cap_rule": line.get("limit_rule"),
			       "arrears_before": line.get("arrears_before", 0), "arrears_after": line.get("arrears_after", 0),
			       "note": (line.get("shortfall_reason") or "")[:500] or None}
			doc.append("ledger", row)
			doc.flags.ignore_permissions = True
			doc.save(ignore_permissions=True)
			frappe.db.set_value(DEDUCTION, doc.name, "arrears_balance", balance(doc.name))
			written.append({"deduction": doc.name, "employee": doc.employee, **row})
	return written


def report(company: str = "", start: str = "", end: str = "", employee: str = "", deduction: str = "") -> list:
	"""Who, which order, amount taken, cap, remaining arrears — every saved period in the window (cancelled excluded)."""
	if not ready():
		return []
	filters = {"deduction_type": "Garnishment"}
	if company:
		filters["company"] = company
	if employee:
		filters["employee"] = employee
	if deduction:
		filters["name"] = deduction
	out = []
	for d in frappe.db.get_all(DEDUCTION, filters=filters, fields=["name"], limit=5000) or []:
		doc = frappe.get_doc(DEDUCTION, d["name"])
		rows = list(doc.get("ledger") or [])
		live = _live_entries({_get(r, "payroll_entry") for r in rows if _get(r, "payroll_entry")})
		for r in rows:
			day = str(_get(r, "pay_period_end") or "")[:10]
			if (start and day and day < start) or (end and day and day > end):
				continue
			if _get(r, "payroll_entry") and _get(r, "payroll_entry") not in live:
				continue
			out.append({"employee": doc.employee, "employee_name": doc.get("employee_name"), "order": doc.name,
			            "category": doc.get("deduction_category"), "reference": doc.get("reference"),
			            "period_end": day, "entry": _get(r, "payroll_entry"), "run_kind": _get(r, "run_kind"),
			            "current_ordered": _num(_get(r, "current_requested")),
			            "current_taken": _num(_get(r, "current_taken")),
			            "arrears_caught_up": _num(_get(r, "catch_up_taken")),
			            "carried_forward": _num(_get(r, "carried_forward")),
			            "cap": _num(_get(r, "cap_amount")), "cap_rule": _get(r, "cap_rule"),
			            "arrears_after": _num(_get(r, "arrears_after")), "why": _get(r, "note")})
	return sorted(out, key=lambda r: (r["period_end"], r["employee"] or "", r["order"]))
