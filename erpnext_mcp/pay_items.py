# SPDX-License-Identifier: MIT
"""Bonuses, commissions and profit-share payouts paid with a pay run (Farm Pay Item). v0.292.0.

A pay item is Pending until a SAVED run whose period covers its `pay_on` date pays it; the run marks it Paid and
links itself. A cancelled run does not release it automatically — set it back to Pending by hand, which is the
deliberate act a re-run of real money should be. The engine (payroll_calc) taxes "Wages" items with the period's pay
and adds "Not subject to withholding" items (a fishing-boat crew share, IRC 3121(b)(20)) after the withholding; both
are earnings for the garnishment caps (15 U.S.C. 1672(a)). Treatment per kind is the CPA's call.
"""

from __future__ import annotations

import frappe

from . import compat

PAY_ITEM = "Farm Pay Item"
FIELDS = ("name", "employee", "kind", "amount", "pay_on", "treatment", "reference")


def ready() -> bool:
	return compat.doctype_exists(PAY_ITEM)


def pending(company: str, start: str, end: str, employees: list | None = None) -> dict:
	"""Employee → their Pending pay items with `pay_on` inside the period."""
	if not ready():
		return {}
	filters = {"company": company, "status": "Pending", "pay_on": ("between", [str(start)[:10], str(end)[:10]])}
	if employees:
		filters["employee"] = ("in", list(employees))
	out: dict = {}
	for row in frappe.db.get_all(PAY_ITEM, filters=filters, fields=list(FIELDS), order_by="pay_on asc", limit=5000) or []:
		out.setdefault(str(row.get("employee") or ""), []).append(dict(row))
	return out


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
