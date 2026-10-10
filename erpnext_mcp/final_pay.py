# SPDX-License-Identifier: MIT
"""When a departing worker's final wages are due. v0.300.0 (Tim, 2026-10-10: align with BOLI).

ORS 652.140 (most workers) and ORS 652.145 (seasonal farmworkers, which overrides it):

* Discharged, or by mutual agreement — by the end of the first business day after.
* Quit with at least 48 hours' (excluding weekends and holidays) notice — immediately, on the last day.
* Quit without that notice — within 5 business days or the next regular payday, whichever is first.
* A SEASONAL FARMWORKER — immediately when employment ends; if they quit without 48 hours' notice, within 48 hours
  or the next payday, whichever is first. (The end-of-harvest exception — noon the next day, for a registered camp
  operator providing free housing until paid — is not assumed; pay immediately.)

The deadline lands on the payroll calendar (overdue / due) until `final_paid_on` is set. Penalty wages run from the
deadline (ORS 652.150), which is why it is a calendar item and not a note. Dates only: paying is a person's.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat

EMPLOYEE = "Employee"
REASONS = ("Discharged", "Mutual agreement", "Quit with 48+ hours notice", "Quit without notice", "End of season")
FIELDS = (
	("separation_reason", "Separation Reason", "Select", "\n" + "\n".join(REASONS),
	 "v0.300.0. Sets when final pay is due (ORS 652.140 / 652.145)."),
	("seasonal_farmworker", "Seasonal Farmworker", "Check", None,
	 "v0.300.0. Temporary farm work producing or harvesting seasonal crops (ORS 652.145): final pay is due "
	 "immediately when employment ends."),
	("final_paid_on", "Final Wages Paid On", "Date", None,
	 "v0.300.0. When the final wages were paid — clears the payroll-calendar deadline."),
)


def install_fields() -> list:
	"""Employee.separation_reason / seasonal_farmworker / final_paid_on. Create-only; never raises."""
	made = []
	try:
		if not compat.doctype_exists(EMPLOYEE):
			return made
		anchor = "relieving_date" if compat.has_field(EMPLOYEE, "relieving_date") else ""
		for fieldname, label, fieldtype, options, description in FIELDS:
			if compat.has_field(EMPLOYEE, fieldname):
				anchor = fieldname
				continue
			doc = frappe.get_doc({"doctype": "Custom Field", "dt": EMPLOYEE, "fieldname": fieldname, "label": label,
			                      "fieldtype": fieldtype, "options": options, "insert_after": anchor,
			                      "description": description, "module": "ERPNext MCP"})
			doc.flags.ignore_permissions = True
			doc.insert(ignore_permissions=True)
			made.append(f"{EMPLOYEE}.{fieldname}")
			anchor = fieldname
	except Exception as exc:  # pragma: no cover
		print(f"erpnext_mcp: final-pay fields were not added — {type(exc).__name__}: {exc}")
	return made


def _business_days_after(day: datetime.date, count: int) -> datetime.date:
	current = day
	while count > 0:
		current += datetime.timedelta(days=1)
		if current.weekday() < 5:
			count -= 1
	return current


def due(last_day, reason: str = "", seasonal: bool = False) -> dict:
	"""Pure. `{due_date, rule, note}` for a worker whose last day was `last_day`."""
	last = last_day if isinstance(last_day, datetime.date) else datetime.date.fromisoformat(str(last_day)[:10])
	reason = str(reason or "")
	if seasonal:
		if reason == "Quit without notice":
			return {"due_date": (last + datetime.timedelta(days=2)).isoformat(),
			        "rule": "ORS 652.145(2): within 48 hours or the next regular payday, whichever is first",
			        "note": "pay sooner if a regular payday falls inside the 48 hours"}
		return {"due_date": last.isoformat(), "rule": "ORS 652.145: immediately, when employment ends", "note": ""}
	if reason == "Quit with 48+ hours notice":
		return {"due_date": last.isoformat(), "rule": "ORS 652.140(2)(a): immediately, on the last day", "note": ""}
	if reason == "Quit without notice":
		return {"due_date": _business_days_after(last, 5).isoformat(),
		        "rule": "ORS 652.140(2)(b): within 5 business days or the next regular payday, whichever is first",
		        "note": "pay sooner if a regular payday comes first"}
	note = "" if reason else "no separation reason recorded — the strictest non-seasonal deadline is used"
	return {"due_date": _business_days_after(last, 1).isoformat(),
	        "rule": "ORS 652.140(1): by the end of the first business day after", "note": note}


def calendar_rows(company: str, low: str, high: str) -> list:
	"""Final-pay deadlines for workers who have left and are not yet marked paid, due between `low` and `high`."""
	if not compat.has_field(EMPLOYEE, "relieving_date"):
		return []
	fields = list(compat.existing_fields(EMPLOYEE, ("name", "employee_name", "relieving_date", "separation_reason",
	                                                "seasonal_farmworker", "final_paid_on")))
	out = []
	for row in frappe.db.get_all(EMPLOYEE, filters={"company": company, "relieving_date": ("is", "set")},
	                             fields=fields, limit=2000) or []:
		if row.get("final_paid_on") or not row.get("relieving_date"):
			continue
		when = due(row["relieving_date"], row.get("separation_reason"), bool(int(row.get("seasonal_farmworker") or 0)))
		if not low <= when["due_date"] <= high:
			continue
		out.append({"name": f"final_pay:{row['name']}", "title": f"Final pay due — {row.get('employee_name') or row['name']}",
		            "subtitle": f"{company} · last day {row['relieving_date']} · {when['rule']}"
		                        + (f" · {when['note']}" if when["note"] else ""),
		            "due_date": when["due_date"], "done": False, "kind": "final_pay", "employee": row["name"]})
	return out
