# SPDX-License-Identifier: MIT
"""The payroll calendar: deposit due dates and filings, with reminders. v0.224.0.

docs/design/payroll_windows.md §2. Off until `payroll_calendar_enabled`. Rows come
from the same deadline arithmetic `get_tax_deposit_schedule` uses (lookback,
semiweekly/monthly, holiday shifts) and the quarterly / annual filing dates. It
reminds people; it never sends money or files anything.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat, settings
from .tax_remittance_calc import QUARTERS, federal_holidays, next_business_day, quarterly_return_due

ROLES = ("System Manager", "HR Manager", "HR User", "Accounts Manager")
REMINDER_ROLES = ("System Manager", "HR Manager")
HORIZON_DAYS = 60
OVERDUE_DAYS = 90
TAX_FORM = "Tax Form"
ANNUAL = (
	("W-2", "W-2 to workers; W-2 + W-3 to SSA"),
	("940", "Form 940 (FUTA)"),
	("943", "Form 943 — only if this farm files 943 instead of 941"),
	("1099-NEC", "1099-NEC to recipients and the IRS"),
	("OR-WR", "Oregon WR annual withholding reconciliation"),
)


def enabled() -> bool:
	return settings.as_bool(settings._value("payroll_calendar_enabled"))


def reminder_days() -> list:
	out = []
	for part in str(settings._value("payroll_reminder_days") or "7,1").replace(" ", "").split(","):
		try:
			out.append(max(0, int(part)))
		except ValueError:
			continue
	return sorted(set(out), reverse=True) or [7, 1]


def _today() -> datetime.date:
	return datetime.date.fromisoformat(str(frappe.utils.today())[:10])


def _filed(company: str, form: str, year: int, quarter: str = "") -> bool:
	if not compat.doctype_exists(TAX_FORM):
		return False
	filters = {
		"company": company,
		"form_type": form,
		"fiscal_year": year,
		"status": ["in", ["Filed", "Amended"]],
	}
	if quarter:
		filters["quarter"] = quarter
	try:
		return bool(frappe.db.get_all(TAX_FORM, filters=filters, pluck="name", limit=1))
	except Exception:
		return False


def _deposits(company: str, year: int) -> list:
	"""Federal (and Oregon) withholding deposits from the deposit schedule. Never raises."""
	from .tools import tax_remittance

	try:
		data = tax_remittance.get_tax_deposit_schedule({"company": company, "fiscal_year": year}).data
	except Exception:
		return []
	out = []
	for row in data.get("federal_deposits") or []:
		out.append(
			{
				"name": f"deposit:{company}:{row.get('payroll_entry')}",
				"title": f"Federal tax deposit ${float(row.get('deposit_liability') or 0):,.2f} (EFTPS)",
				"subtitle": f"{company} · paid {row.get('payday')} · {data.get('deposit_schedule')} depositor"
				+ " · Oregon withholding deposit on the same date",
				"due_date": str(row.get("due_date") or "")[:10],
				"done": False,
			}
		)
	return out


def _filings(company: str, year: int) -> list:
	holidays = {}
	for one in (year - 1, year, year + 1):
		holidays.update(federal_holidays(one))
	out = []
	for quarter in QUARTERS:
		due = quarterly_return_due(quarter, year, holidays).isoformat()
		out.append(
			{
				"name": f"941:{company}:{year}{quarter}",
				"title": f"Form 941 {quarter} {year} (943 farms: annual instead)",
				"subtitle": company,
				"due_date": due,
				"done": _filed(company, "941", year, quarter),
			}
		)
		out.append(
			{
				"name": f"oq:{company}:{year}{quarter}",
				"title": f"Oregon OQ + Form 132 {quarter} {year}",
				"subtitle": company,
				"due_date": due,
				"done": _filed(company, "OQ", year, quarter),
			}
		)
		out.append(
			{
				"name": f"futa:{company}:{year}{quarter}",
				"title": f"FUTA deposit {quarter} {year} — only if undeposited FUTA is over $500",
				"subtitle": company,
				"due_date": due,
				"done": False,
			}
		)
	annual_due = next_business_day(datetime.date(year + 1, 1, 31), holidays).isoformat()
	for form, title in ANNUAL:
		out.append(
			{
				"name": f"annual:{company}:{year}:{form}",
				"title": f"{title} — {year}",
				"subtitle": company,
				"due_date": annual_due,
				"done": form in ("W-2", "1099-NEC", "OR-WR") and _filed(company, form, year),
			}
		)
	return out


def items(companies: list) -> list:
	"""Every calendar row for these companies, overdue and the next HORIZON_DAYS, soonest first."""
	today = _today()
	lead = max(reminder_days())
	low = (today - datetime.timedelta(days=OVERDUE_DAYS)).isoformat()
	high = (today + datetime.timedelta(days=HORIZON_DAYS)).isoformat()
	out = []
	for company in companies:
		for year in sorted(
			{today.year - 1, today.year, (today + datetime.timedelta(days=HORIZON_DAYS)).year}
		):
			for row in _deposits(company, year) + _filings(company, year):
				due = row["due_date"]
				if not due or not low <= due <= high:
					continue
				if row["done"]:
					state = "done"
				elif due < today.isoformat():
					state = "overdue"
				elif (datetime.date.fromisoformat(due) - today).days <= lead:
					state = "due"
				else:
					state = "upcoming"
				if state == "done" and due < today.isoformat():
					continue
				out.append({**{k: v for k, v in row.items() if k != "done"}, "state": state, "doctype": None})
	seen, unique = set(), []
	for row in sorted(out, key=lambda r: (r["due_date"], r["name"])):
		if row["name"] not in seen:
			seen.add(row["name"])
			unique.append(row)
	return unique


def _companies_of(user: str) -> list:
	from . import roles

	return roles.companies_for(user) or [c for c in frappe.db.get_all("Company", pluck="name", limit=20) if c]


# ── reminders ───────────────────────────────────────────────────────────────
def _recipients() -> list:
	raw = str(settings._value("payroll_reminder_recipients") or "")
	named = [part.strip() for part in raw.replace(";", ",").replace("\n", ",").split(",") if part.strip()]
	if named:
		return named
	users = set()
	if compat.doctype_exists("Has Role"):
		for role in REMINDER_ROLES:
			users |= set(
				frappe.db.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")
			)
	return sorted(
		u for u in users if u not in ("Administrator", "Guest") and frappe.db.get_value("User", u, "enabled")
	)


def send_reminders() -> list:
	"""Daily. One email (and push) per item per lead day. Off unless enabled. Never raises."""
	if not enabled():
		return []
	try:
		today = _today()
		leads = set(reminder_days())
		companies = [c for c in frappe.db.get_all("Company", pluck="name", limit=20) if c]
		sent = []
		for row in items(companies):
			if row["state"] not in ("due", "overdue"):
				continue
			days = (datetime.date.fromisoformat(row["due_date"]) - today).days
			if row["state"] == "due" and days not in leads:
				continue
			key = f"erpnext_mcp_payroll_reminder:{row['name']}:{days if row['state'] == 'due' else 'overdue'}"
			try:
				if frappe.defaults.get_global_default(key):
					continue
				frappe.defaults.set_global_default(key, str(today))
			except Exception:  # pragma: no cover
				pass
			subject = (
				f"Payroll: {row['title']} is OVERDUE (was due {row['due_date']})"
				if row["state"] == "overdue"
				else f"Payroll: {row['title']} is due {row['due_date']} ({days} day(s))"
			)
			recipients = _recipients()
			if recipients:
				frappe.sendmail(
					recipients=recipients,
					subject=subject,
					message=(
						f"<p>{frappe.utils.escape_html(row['title'])} — {frappe.utils.escape_html(row['subtitle'])}.</p>"
						"<p>The figures are in ERPNext (get_tax_deposit_schedule, the tax forms). Paying and filing "
						"are yours: EFTPS, Oregon Revenue Online, or the CPA.</p>"
					),
					now=False,
				)
			_push(recipients, subject)
			sent.append({"item": row["name"], "subject": subject})
		frappe.db.commit()
		return sent
	except Exception:  # pragma: no cover - a scheduled job never raises
		try:
			frappe.log_error(title="erpnext_mcp: payroll_calendar.send_reminders failed")
		except Exception:
			pass
		return []


def _push(users: list, text: str) -> None:
	try:
		from .services import push

		employees = [e for e in (frappe.db.get_value("Employee", {"user_id": u}, "name") for u in users) if e]
		if employees:
			push.send_push_to_employees(
				employees,
				{"aps": {"alert": {"title": "Payroll", "body": text[: push.MAX_BODY]}}},
				priority="5",
			)
	except Exception:  # pragma: no cover - a reminder push never breaks the email
		pass
