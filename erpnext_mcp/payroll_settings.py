# SPDX-License-Identifier: MIT
"""Payroll settings as versioned data. v0.235.0. docs/design/config_lifecycle_and_tool_consolidation.md §4.

Tim's decision 9 (2026-10-04): payroll settings join the shared lifecycle ONLY if payroll
keeps running uninterrupted, and only after a preview diff proves the output identical.
The first kind is `overtime_rule` — until now three constants in code (40 h a workweek,
1.5×, and the half-time premium on piece work that follows from it). It becomes a
Farm Config Version ("Payroll Setting", key `overtime_rule`) whose seed is EXACTLY those
values, so nothing a pay run computes changes on upgrade; with no version published the
readers answer the same built-in numbers.

STRICTER THAN THE PHONE KINDS (§4): a payroll version carries `effective_from`; publishing
is human-only, always (never over MCP, whoever drafted it); and a preview reruns a real pay
period twice — live and staged — and names every employee whose net moves by more than the
flag threshold (decision 10: a percentage, default 2%). Payroll TRANSACTIONS are out of
scope and keep their own tools.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime

import frappe

KIND = "Payroll Setting"
OVERTIME_KEY = "overtime_rule"

#: Today's law, as the code had it: OR HB 4002 and WA SB 5172, fully phased.
DEFAULT_OVERTIME = {"weekly_threshold_hours": 40.0, "multiplier": 1.5}

#: Decision 10: flag an employee whose net pay moves by more than this percentage.
DEFAULT_FLAG_PCT = 2.0

_OVERLAY: contextvars.ContextVar = contextvars.ContextVar("erpnext_mcp_payroll_overlay", default=None)


@contextlib.contextmanager
def overlay(key: str, body: dict):
	"""During a preview only: readers take `body` for `key` instead of the published version."""
	current = dict(_OVERLAY.get() or {})
	current[key] = dict(body or {})
	token = _OVERLAY.set(current)
	try:
		yield
	finally:
		_OVERLAY.reset(token)


def _published(key: str, as_of: str = "") -> tuple[dict, str]:
	"""(body, version name) in force on `as_of`, or ({}, "built-in").

	Among the versions that have ever been published (Published or Superseded), the one
	with the latest `effective_from` on or before the day — so a rerun of April pays
	April's rule, and a version published today for next month changes nothing yet.
	"""
	try:
		from . import phone_config

		if not phone_config.ready():
			return {}, "built-in"
		day = str(as_of or frappe.utils.today())[:10]
		rows = phone_config.rows(KIND, key, (phone_config.PUBLISHED, phone_config.SUPERSEDED))
		live = [int(r.get("version") or 0) for r in rows if r["status"] == phone_config.PUBLISHED]
		if not live:
			return {}, "built-in"
		# A version newer than the published one was rolled back, and never counts.
		ceiling = max(live)
		best = None
		for row in rows:
			if int(row.get("version") or 0) > ceiling:
				continue
			doc = frappe.get_doc(phone_config.DOCTYPE, row["name"])
			body = phone_config.body_of(doc)
			effective = str(body.get("effective_from") or "")[:10]
			if not effective or effective > day:
				continue
			rank = (effective, int(row.get("version") or 0))
			if best is None or rank > best[0]:
				best = (rank, body, doc.name)
		if best:
			return best[1], best[2]
	except Exception:
		pass
	return {}, "built-in"


def overtime(as_of: str = "") -> dict:
	"""`{weekly_threshold_hours, multiplier, version}` in force on `as_of`."""
	staged = (_OVERLAY.get() or {}).get(OVERTIME_KEY)
	if staged is not None:
		body, version = staged, "preview"
	else:
		body, version = _published(OVERTIME_KEY, as_of)
	out = dict(DEFAULT_OVERTIME)
	for field in out:
		try:
			if body.get(field) not in (None, ""):
				out[field] = float(body[field])
		except (TypeError, ValueError):
			pass
	out["version"] = version
	return out


def ot_multiplier(as_of: str = "") -> float:
	return overtime(as_of)["multiplier"]


def ot_premium_multiplier(as_of: str = "") -> float:
	"""What is owed ON TOP where straight time was already paid (29 CFR 778.111 / .115)."""
	return round(overtime(as_of)["multiplier"] - 1.0, 6)


def weekly_threshold(as_of: str = "") -> float:
	return overtime(as_of)["weekly_threshold_hours"]


def validate(body: dict, key: str = "", for_publish: bool = False) -> dict:
	errors, warnings = [], []
	if key != OVERTIME_KEY:
		errors.append(f"the only payroll setting so far is {OVERTIME_KEY!r}")
		return {"errors": errors, "warnings": warnings}
	try:
		threshold = float(body.get("weekly_threshold_hours"))
		if not 1 <= threshold <= 80:
			errors.append("weekly_threshold_hours is 1–80")
	except (TypeError, ValueError):
		errors.append("weekly_threshold_hours is a number of hours")
	try:
		multiplier = float(body.get("multiplier"))
		if not 1.0 <= multiplier <= 3.0:
			errors.append("multiplier is 1.0–3.0")
	except (TypeError, ValueError):
		errors.append("multiplier is a number, e.g. 1.5")
	effective = str(body.get("effective_from") or "")
	try:
		datetime.date.fromisoformat(effective[:10])
	except ValueError:
		errors.append("effective_from (YYYY-MM-DD) is required: a payroll setting starts on a date")
	if for_publish and not errors:
		submitted = _submitted_after(effective[:10])
		if submitted:
			errors.append(
				f"effective_from {effective[:10]} is inside a submitted pay period ({submitted}); a published "
				"setting never changes a payroll that has been run — choose a date after it"
			)
	return {"errors": errors, "warnings": warnings}


def _submitted_after(day: str) -> str:
	"""A submitted Farm Payroll Entry whose period ends on or after `day`, or ""."""
	try:
		rows = frappe.db.get_all(
			"Farm Payroll Entry",
			filters={"docstatus": 1, "pay_period_end": (">=", day)},
			fields=["name"],
			limit=1,
		)
	except Exception:
		return ""
	return rows[0]["name"] if rows else ""


def seed_body() -> dict:
	return {"schema_version": 1, "key": OVERTIME_KEY, **DEFAULT_OVERTIME, "effective_from": "2020-01-01",
	        "basis": "OR HB 4002 and WA SB 5172, fully phased: 40 hours a workweek, 1.5× the regular rate."}


def flag_pct() -> float:
	try:
		from . import settings

		value = float(settings.get_settings().get("payroll_preview_flag_pct") or 0)
	except Exception:
		value = 0
	return value if value > 0 else DEFAULT_FLAG_PCT


# ── the preview: rerun a pay period live and staged ─────────────────────────
_COMPARED = ("gross_pay", "net_pay", "total_deductions", "overtime_pay", "regular_hours", "overtime_hours",
             "federal_withholding", "state_withholding", "social_security", "medicare", "employer_taxes")


def latest_period(company: str = "") -> dict:
	filters = {"docstatus": ("in", [0, 1])}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(
		"Farm Payroll Entry",
		filters=filters,
		fields=["name", "company", "pay_period_start", "pay_period_end", "pay_frequency"],
		order_by="pay_period_end desc",
		limit=1,
	)
	return dict(rows[0]) if rows else {}


def preview(key: str, body: dict, args: dict) -> dict:
	"""Rerun one pay period with the live setting and with `body`; per-employee differences."""
	from .tools import payroll

	period = {k: args.get(k) for k in ("company", "pay_period_start", "pay_period_end", "pay_frequency") if args.get(k)}
	if not (period.get("pay_period_start") and period.get("pay_period_end")):
		last = latest_period(str(args.get("company") or ""))
		if not last:
			raise ValueError("no pay period to rerun: pass company, pay_period_start and pay_period_end")
		period = {"company": last["company"], "pay_period_start": str(last["pay_period_start"]),
		          "pay_period_end": str(last["pay_period_end"]), "pay_frequency": last.get("pay_frequency") or "Biweekly"}
	report = validate({**body}, key)
	if report["errors"]:
		raise ValueError("; ".join(report["errors"]))
	run_args = dict(period)
	_c, live_slips, live_totals = payroll._period_run(dict(run_args), creating=False)
	with overlay(key, body):
		_c2, staged_slips, staged_totals = payroll._period_run(dict(run_args), creating=False)
	live_by = {s.get("employee"): s for s in live_slips}
	staged_by = {s.get("employee"): s for s in staged_slips}
	threshold = flag_pct()
	employees, flagged = [], []
	for employee in sorted(set(live_by) | set(staged_by)):
		a, b = live_by.get(employee) or {}, staged_by.get(employee) or {}
		changes = {}
		for field in _COMPARED:
			x, y = _num(a.get(field)), _num(b.get(field))
			if x is None and y is None:
				continue
			if round((y or 0) - (x or 0), 2) != 0:
				changes[field] = {"live": x, "staged": y, "difference": round((y or 0) - (x or 0), 2)}
		net_live = _num(a.get("net_pay")) or 0.0
		net_diff = (changes.get("net_pay") or {}).get("difference", 0.0)
		pct = round(abs(net_diff) / net_live * 100, 2) if net_live else (100.0 if net_diff else 0.0)
		entry = {"employee": employee, "employee_name": a.get("employee_name") or b.get("employee_name"),
		         "changes": changes, "net_change_pct": pct, "flagged": pct > threshold}
		employees.append(entry)
		if entry["flagged"]:
			flagged.append(employee)
	totals = {}
	for field in ("total_gross", "total_net", "total_deductions"):
		x, y = _num(live_totals.get(field)), _num(staged_totals.get(field))
		totals[field] = {"live": x, "staged": y, "difference": round((y or 0) - (x or 0), 2)}
	identical = not any(e["changes"] for e in employees)
	return {
		"period": period,
		"identical": identical,
		"employees": employees,
		"changed": sum(1 for e in employees if e["changes"]),
		"flagged": flagged,
		"flag_threshold_pct": threshold,
		"totals": totals,
		"written": False,
		"note": "NOTHING WAS WRITTEN. The period was calculated twice, read-only; no slip, entry or GL row exists from it.",
	}


def _num(value):
	try:
		return round(float(value), 2) if value not in (None, "") else None
	except (TypeError, ValueError):
		return None
