# SPDX-License-Identifier: MIT
"""The live budget — plan, request, order, deliver, use, against the budget as it happens. v0.274.0
(docs/contracts/live_budget_v0_274.yaml).

THE PLAN IS CONFIGURATION ("Input Plan", one per company and year), imported from the pro forma a lender saw — the
Growing Budget's lines with the dollars the P&L uses, each line's ERPNext account and cost centre from the CoA
Mapping sheet, the acres by variety, and the spray program ($/acre per application by category, which applications
each variety gets, in which month). Imported as a DRAFT; a person publishes it. The pro forma's numbers are business
data and live in the site, never in this repository.

THE LIVE VIEW, per account and cost centre:
  budget     — the plan's dollars for the year, and phased to date (each line's months, even by default);
  actual     — what the ledger says was spent (GL Entries, not cancelled);
  committed  — what is ordered and not yet received (open Purchase Orders) and asked for and not yet ordered
               (Material Requests, at the Item's last valuation — an estimate, and said so);
  forecast   — actual + committed + the plan for the months still ahead;
and a line at 90% / 100% of its year (actual + committed) raises one alert per level.

ASKING FOR INPUTS: `request_inputs` nets what is asked for against the shed's stock, drafts a Material Request for
the rest, estimates its value, says which budget line it lands on and how much of it is left, and flags it for
approval over the plan's threshold. An ERPNext Budget (action Warn) can be drafted from the plan. Nothing is
submitted, ordered or paid here.

OFF per company until `live_budget_enabled` — Constancy Farms, LLC stays inactive until its go-live.
"""

from __future__ import annotations

import json
import re

import frappe

from . import compat

KIND = "Input Plan"
FLAG = "live_budget_enabled"
MR, MR_ITEM, PO, PO_ITEM, BUDGET, GL = ("Material Request", "Material Request Item", "Purchase Order",
                                        "Purchase Order Item", "Budget", "GL Entry")
CATEGORIES = ("Fungicide", "Insecticide", "Bactericide (copper)", "Growth regulator (GA3)", "Dormant oil")
#: The pro forma's spray timings, the month each falls in (the plan can say otherwise).
TIMING_MONTHS = {"delayed dormant": 3, "bloom": 4, "petal fall": 5, "shuck": 5, "first cover": 5, "straw color": 6,
                 "pre-harvest": 6, "late cover": 7}
DEFAULT_THRESHOLDS = {"warn_pct": 90, "over_pct": 100}


class BudgetError(Exception):
	pass


def enabled(company: str) -> bool:
	from . import flags

	return bool(company) and bool(flags.value(FLAG, company=company, default=False))


def _require_on(company: str) -> None:
	if not enabled(company):
		raise BudgetError(f"the live budget is off for {company} — turn on `{FLAG}` for it when it goes live.")


def plan_key(company: str, year) -> str:
	return f"{re.sub(r'[^a-z0-9]+', '_', str(company).casefold()).strip('_')}_{year}"


# ── the plan (configuration) ─────────────────────────────────────────────────
def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors, warnings = [], []
	if not body.get("company"):
		errors.append("company")
	if not re.fullmatch(r"\d{4}", str(body.get("year") or "")):
		errors.append("year: YYYY")
	lines = body.get("lines") or []
	if not lines:
		errors.append("lines: at least one budget line")
	codes = set()
	for index, line in enumerate(lines, start=1):
		code = line.get("code") or f"line {index}"
		if code in codes:
			errors.append(f"{code}: listed twice")
		codes.add(code)
		try:
			if float(line.get("annual") or 0) < 0:
				errors.append(f"{code}: annual is negative")
		except (TypeError, ValueError):
			errors.append(f"{code}: annual is not a number")
		if not line.get("account"):
			warnings.append(f"{code} ({line.get('line')}): no ERPNext account — it is planned but no actual can reach it")
		elif for_publish and compat.doctype_exists("Account") and not frappe.db.exists("Account", line["account"]):
			warnings.append(f"{code}: no Account {line['account']!r} on this site yet")
		phasing = line.get("months")
		if phasing and (len(phasing) != 12 or abs(sum(float(x or 0) for x in phasing) - 1.0) > 0.01):
			errors.append(f"{code}: months is twelve fractions that add up to 1")
	thresholds = body.get("thresholds") or {}
	for name in ("warn_pct", "over_pct"):
		if name in thresholds and not 0 < float(thresholds[name]) <= 200:
			errors.append(f"thresholds.{name}: 1–200")
	return {"errors": errors, "warnings": warnings}


def _num(value) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return 0.0


def import_pro_forma(content: bytes, company: str, year: int, *, actor: str = "", source: str = "") -> dict:
	"""The pro forma workbook → an Input Plan DRAFT for one company and year."""
	from . import phone_config, xlsx_lite

	book = xlsx_lite.open_workbook(content)
	missing = [s for s in ("Growing Budget", "CoA Mapping") if s not in book]
	if missing:
		raise BudgetError(f"the workbook has no {' or '.join(missing)} sheet — is it the pro forma?")
	rows = book["Growing Budget"]
	head = next((i for i, r in enumerate(rows) if r and str(r[0] or "").strip().casefold() == "code"), None)
	if head is None:
		raise BudgetError("the Growing Budget sheet has no 'Code' header row.")
	header = [str(h or "").strip() for h in rows[head]]
	used = next((i for i, h in enumerate(header) if h.casefold().startswith(f"used {year}")), None) or next(
		(i for i, h in enumerate(header) if h.casefold().startswith("used")), None)
	line_col = next((i for i, h in enumerate(header) if h.casefold() == "budget line"), 1)
	basis_col = next((i for i, h in enumerate(header) if h.casefold() == "basis"), 2)
	if used is None:
		raise BudgetError("the Growing Budget sheet has no 'Used … $/yr' column.")
	mapping = {}
	coa = book["CoA Mapping"]
	coa_head = next((i for i, r in enumerate(coa) if r and str(r[0] or "").strip().casefold() == "sheet"), None)
	if coa_head is not None:
		names = [str(h or "").strip().casefold() for h in coa[coa_head]]
		col = {k: next((i for i, h in enumerate(names) if h.startswith(k)), None)
		       for k in ("model line", "erpnext account", "cost center")}
		for r in coa[coa_head + 1:]:
			if not r or str(r[0] or "").strip() != "Growing Budget" or col["model line"] is None:
				continue
			text = str(r[col["model line"]] or "").strip()
			account = str(r[col["erpnext account"]] or "").strip() if col["erpnext account"] is not None and \
				col["erpnext account"] < len(r) else ""
			centre = str(r[col["cost center"]] or "").strip() if col["cost center"] is not None and \
				col["cost center"] < len(r) else ""
			mapping[text] = (account if re.match(r"^\d{3,5} ", account) else "", centre if re.match(r"^\d{2,4} ", centre)
			                 else "", account if not re.match(r"^\d{3,5} ", account) else "")
	lines, section = [], ""
	for r in rows[head + 1:]:
		if not r or all(v in (None, "") for v in r):
			continue
		code = str(r[0] or "").strip()
		if len([v for v in r if v not in (None, "")]) == 1 and code:
			section = code
			continue
		if not code or used >= len(r) or not isinstance(r[used], (int, float)):
			continue
		text = str(r[line_col] or "").strip()
		account, centre, note = mapping.get(text, ("", "", ""))
		lines.append({"code": code, "line": text, "section": section, "basis": str(r[basis_col] or "").strip(),
		              "annual": round(_num(r[used]), 2), "account": account, "cost_center": centre,
		              **({"mapping_note": note} if note else {})})
	acres = {}
	if "Cost per Acre" in book:
		cpa = book["Cost per Acre"]
		at = next((i for i, r in enumerate(cpa) if r and str(r[0] or "").strip().casefold() == "acres"), None)
		numeric = [i for i, v in enumerate(cpa[at]) if isinstance(v, (int, float))] if at is not None else []
		# The variety names are the nearest row whose cells sit over the acre figures as text.
		names = next((cpa[j] for j in sorted(range(len(cpa)), key=lambda j: abs(j - at)) if at is not None and j != at
		              and numeric and all(i < len(cpa[j]) and isinstance(cpa[j][i], str) for i in numeric)), None)
		if at is not None and names:
			for i, value in enumerate(cpa[at][1:], start=1):
				label = str(names[i] or "").strip() if i < len(names) else ""
				if isinstance(value, (int, float)) and label and not label.casefold().startswith("all"):
					acres[label.replace(" varieties", "")] = value
	program = _spray_program(book.get("Spray Program") or [])
	body = {"company": company, "year": str(year), "source": source or "pro forma", "imported_by": actor or None,
	        "acres": acres, "lines": lines, "spray_program": program, "thresholds": dict(DEFAULT_THRESHOLDS),
	        "approval": {"material_request_over": 2500}, "timing_months": dict(TIMING_MONTHS)}
	report = validate(body)
	if report["errors"]:
		raise BudgetError("; ".join(report["errors"][:8]))
	doc, _ = phone_config.save_draft(KIND, plan_key(company, year), body,
	                                 f"Imported from {source or 'the pro forma'} — check, then publish.", "Operator")
	return {"plan": plan_key(company, year), "version": doc.version, "status": "Draft", "lines": len(lines),
	        "annual_total": round(sum(line["annual"] for line in lines), 2), "acres": acres,
	        "unmapped": [line["code"] for line in lines if not line["account"]], "warnings": report["warnings"],
	        "spray_applications": len(program)}


def _spray_program(rows: list) -> list:
	head = next((i for i, r in enumerate(rows) if r and str(r[0] or "").strip() == "#"), None)
	if head is None:
		return []
	header = [str(h or "").strip() for h in rows[head]]
	cats = [(i, h) for i, h in enumerate(header) if h in CATEGORIES]
	varieties = [(i, h.replace("-", "")) for i, h in enumerate(header) if i > header.index("Total")] if "Total" in \
		header else []
	out = []
	for r in rows[head + 1:]:
		if not r or not isinstance(r[0], (int, float)):
			break
		timing = str(r[1] or "").strip()
		out.append({"n": int(r[0]), "timing": timing, "target": str(r[2] or "").strip(),
		            "per_acre": {name: _num(r[i]) for i, name in cats if i < len(r)},
		            "varieties": [name for i, name in varieties if i < len(r) and _num(r[i]) == 1]})
	return out


def plan(company: str, year) -> dict:
	from . import phone_config

	doc = phone_config.doc_of(KIND, plan_key(company, year), status=phone_config.PUBLISHED)
	if doc is None:
		raise BudgetError(f"no published Input Plan for {company} {year} — import the pro forma, check it, publish.")
	return phone_config.body_of(doc)


# ── the live view ────────────────────────────────────────────────────────────
def _months(line: dict) -> list:
	phasing = line.get("months")
	return [float(x or 0) for x in phasing] if phasing and len(phasing) == 12 else [1 / 12] * 12


def _fy(year) -> tuple:
	return f"{year}-01-01", f"{year}-12-31"


def _actuals(company: str, year, as_of: str) -> dict:
	if not compat.doctype_exists(GL):
		return {}
	start, _ = _fy(year)
	rows = frappe.db.get_all(GL, filters={"company": company, "is_cancelled": 0,
	                                      "posting_date": ("between", [start, as_of])},
	                         fields=["account", "cost_center", "debit", "credit"], limit=500000) or []
	out: dict = {}
	for r in rows:
		key = (r.get("account") or "", r.get("cost_center") or "")
		out[key] = out.get(key, 0.0) + _num(r.get("debit")) - _num(r.get("credit"))
	return out


def _expense_account(item: str, company: str) -> str:
	rows = frappe.get_doc("Item", item).get("item_defaults") or [] if frappe.db.exists("Item", item) else []
	for row in rows:
		if row.get("company") == company and row.get("expense_account"):
			return row.get("expense_account")
	return ""


def _rate(item: str) -> float:
	return _num(frappe.db.get_value("Item", item, "valuation_rate")) if frappe.db.exists("Item", item) else 0.0


def _committed(company: str) -> tuple:
	"""({(account, cost_center): $}, [notes]) — open POs not yet received, Material Requests not yet ordered."""
	out: dict = {}
	notes = []
	if compat.doctype_exists(PO):
		for po in frappe.db.get_all(PO, filters={"company": company, "docstatus": 1,
		                                          "status": ("not in", ["Closed", "Completed", "Cancelled"])},
		                            fields=["name"], limit=5000) or []:
			for row in frappe.get_doc(PO, po["name"]).get("items") or []:
				left = max(0.0, _num(row.get("qty")) - _num(row.get("received_qty"))) * _num(row.get("rate"))
				if left <= 0:
					continue
				account = row.get("expense_account") or _expense_account(row.get("item_code"), company)
				key = (account or "", row.get("cost_center") or "")
				out[key] = out.get(key, 0.0) + left
	if compat.doctype_exists(MR):
		for mr in frappe.db.get_all(MR, filters={"company": company, "docstatus": ("<", 2),
		                                          "status": ("not in", ["Ordered", "Stopped", "Cancelled", "Received"])},
		                            fields=["name"], limit=5000) or []:
			for row in frappe.get_doc(MR, mr["name"]).get("items") or []:
				left = max(0.0, _num(row.get("qty")) - _num(row.get("ordered_qty"))) * _rate(row.get("item_code"))
				if left <= 0:
					continue
				account = row.get("expense_account") or _expense_account(row.get("item_code"), company)
				key = (account or "", row.get("cost_center") or "")
				out[key] = out.get(key, 0.0) + left
				notes.append(f"{mr['name']} {row.get('item_code')}: at the Item's valuation (an estimate)")
	return out, notes


def live(company: str, year, as_of: str = "") -> dict:
	"""Budget / actual / committed / forecast per account and cost centre, with the alert level of each."""
	body = plan(company, year)
	as_of = as_of or str(frappe.utils.today())
	month = int(as_of[5:7]) if as_of[:4] == str(year) else (12 if as_of[:4] > str(year) else 0)
	thresholds = {**DEFAULT_THRESHOLDS, **(body.get("thresholds") or {})}
	groups: dict = {}
	for line in body.get("lines") or []:
		key = (line.get("account") or "", line.get("cost_center") or "")
		group = groups.setdefault(key, {"account": key[0] or None, "cost_center": key[1] or None, "budget": 0.0,
		                                "budget_to_date": 0.0, "remaining_plan": 0.0, "lines": []})
		months = _months(line)
		annual = _num(line.get("annual"))
		group["budget"] += annual
		group["budget_to_date"] += annual * sum(months[:month])
		group["remaining_plan"] += annual * sum(months[month:])
		group["lines"].append(line.get("code"))
	actuals = _actuals(company, year, as_of)
	committed, notes = _committed(company)
	unplanned = {k for k in set(actuals) | set(committed) if k not in groups and k[0]}
	rows = []
	for key, group in groups.items():
		actual = sum(v for (a, c), v in actuals.items() if a == key[0] and (not key[1] or c == key[1]))
		commit = sum(v for (a, c), v in committed.items() if a == key[0] and (not key[1] or c == key[1] or not c))
		used = actual + commit
		forecast = actual + commit + group["remaining_plan"]
		pct = round(100 * used / group["budget"], 1) if group["budget"] else None
		level = ("over" if pct is not None and pct >= thresholds["over_pct"] else
		         "warn" if pct is not None and pct >= thresholds["warn_pct"] else "ok")
		rows.append({**{k: v for k, v in group.items() if k != "remaining_plan"},
		             "budget": round(group["budget"], 2), "budget_to_date": round(group["budget_to_date"], 2),
		             "actual": round(actual, 2), "committed": round(commit, 2), "forecast": round(forecast, 2),
		             "variance": round(forecast - group["budget"], 2), "used_pct": pct, "level": level})
	rows.sort(key=lambda r: (-{"over": 2, "warn": 1, "ok": 0}[r["level"]], r["account"] or "~"))
	total = {k: round(sum(r[k] for r in rows), 2) for k in ("budget", "budget_to_date", "actual", "committed",
	                                                          "forecast", "variance")}
	return {"company": company, "year": str(year), "as_of": as_of, "thresholds": thresholds, "rows": rows,
	        "total": total, "unplanned": [{"account": a, "cost_center": c or None, "actual": round(actuals.get((a, c), 0), 2),
	                                       "committed": round(committed.get((a, c), 0), 2)} for a, c in sorted(unplanned)],
	        "notes": notes[:20]}


def daily(as_of: str = "") -> dict:
	"""One alert per budget line and level (90% / 100% of the year), for companies with the live budget on."""
	made = {}
	if not compat.doctype_exists("Compliance Alert"):
		return made
	year = (as_of or str(frappe.utils.today()))[:4]
	for company in [r["name"] for r in frappe.db.get_all("Company", fields=["name"], limit=200) or []]:
		if not enabled(company):
			continue
		try:
			view = live(company, year, as_of)
		except BudgetError:
			continue
		for row in view["rows"]:
			if row["level"] == "ok":
				continue
			made.setdefault(company, []).append(_alert(company, year, row))
	return made


def _alert(company: str, year: str, row: dict) -> str:
	from .alerts import base as alerts

	key = alerts.alert_key(f"budget_{row['level']}", "Account", f"{company}:{year}:{row['account']}:{row['cost_center']}")
	if frappe.db.exists("Compliance Alert", key):
		return key
	alert = frappe.new_doc("Compliance Alert")
	alert.alert_key = key
	alert.alert_type = f"budget_{row['level']}"
	alert.severity = "Warning" if row["level"] == "over" else "Info"
	alert.category = "Other"
	alert.company = company
	alert.source_doctype = "Account" if row["account"] and frappe.db.exists("Account", row["account"]) else "Company"
	alert.source_docname = row["account"] if alert.source_doctype == "Account" else company
	alert.alert_message = (f"{year} budget — {row['account']} / {row['cost_center'] or 'all centres'}: "
	                       f"{row['used_pct']}% used (actual {row['actual']:,.0f} + committed {row['committed']:,.0f} "
	                       f"of {row['budget']:,.0f}); forecast {row['forecast']:,.0f}.")[:1000]
	alert.first_seen = frappe.utils.today()
	alert.last_refreshed = frappe.utils.now()
	alert.insert(ignore_permissions=True)
	return alert.name


# ── what the spray program still needs ─────────────────────────────────────────
def forecast_needs(company: str, year, as_of: str = "") -> dict:
	"""Dollars of spray materials still ahead this year, by month and category, from the plan's spray program."""
	body = plan(company, year)
	as_of = as_of or str(frappe.utils.today())
	month_now = int(as_of[5:7]) if as_of[:4] == str(year) else (13 if as_of[:4] > str(year) else 0)
	acres = body.get("acres") or {}
	months = {k.casefold(): v for k, v in (body.get("timing_months") or TIMING_MONTHS).items()}
	ahead = []
	for app in body.get("spray_program") or []:
		timing = str(app.get("timing") or "").casefold()
		month = next((m for name, m in months.items() if name in timing), None)
		if month is None or month <= month_now:
			continue
		area = sum(_num(acres.get(v)) for v in app.get("varieties") or [])
		for category, per_acre in (app.get("per_acre") or {}).items():
			if per_acre:
				ahead.append({"month": f"{year}-{month:02d}", "timing": app.get("timing"), "category": category,
				              "acres": round(area, 2), "per_acre": per_acre, "dollars": round(per_acre * area, 2)})
	by_category: dict = {}
	for row in ahead:
		by_category[row["category"]] = round(by_category.get(row["category"], 0) + row["dollars"], 2)
	return {"company": company, "year": str(year), "as_of": as_of, "applications": ahead, "by_category": by_category,
	        "total": round(sum(r["dollars"] for r in ahead), 2),
	        "note": "Dollars by category from the pro forma's $/acre; products and rates come when the program names them."}


# ── asking for inputs ───────────────────────────────────────────────────────
def request_inputs(company: str, items: list, *, needed_by: str = "", actor: str = "", year=None) -> dict:
	"""A DRAFT Material Request for what the shed does not already hold — valued, placed on the budget, flagged."""
	_require_on(company)
	from . import receiving

	if not compat.doctype_exists(MR):
		raise BudgetError("this site has no Material Request (ERPNext Buying).")
	if not isinstance(items, list) or not items:
		raise BudgetError("items: [{item_code, qty}]")
	warehouse = receiving.storage_warehouse(company)
	year = year or str(frappe.utils.today())[:4]
	try:
		body = plan(company, year)
	except BudgetError:
		body = {}
	view = None
	lines, netted = [], []
	for index, entry in enumerate(items, start=1):
		code = str(entry.get("item_code") or "").strip()
		if not code or not frappe.db.exists("Item", code):
			raise BudgetError(f"items[{index}]: no Item {code!r}")
		qty = _num(entry.get("qty"))
		if qty <= 0:
			raise BudgetError(f"items[{index}]: qty must be more than 0")
		on_hand = _num(frappe.db.get_value("Bin", {"item_code": code, "warehouse": warehouse}, "actual_qty")) \
			if warehouse and compat.doctype_exists("Bin") else 0.0
		need = max(0.0, qty - on_hand)
		netted.append({"item_code": code, "asked": qty, "on_hand": on_hand, "to_order": need})
		if need > 0:
			lines.append({"item_code": code, "qty": need, "rate": _rate(code),
			              "account": _expense_account(code, company)})
	if not lines:
		return {"material_request": None, "netted": netted, "note": "the shed already holds all of it"}
	value = round(sum(ln["qty"] * ln["rate"] for ln in lines), 2)
	limit = _num((body.get("approval") or {}).get("material_request_over") or 0)
	if body:
		try:
			view = live(company, year)
		except BudgetError:
			view = None
	landing = []
	for account in sorted({ln["account"] for ln in lines if ln["account"]}):
		row = next((r for r in (view or {}).get("rows") or [] if r["account"] == account), None)
		asked = round(sum(ln["qty"] * ln["rate"] for ln in lines if ln["account"] == account), 2)
		landing.append({"account": account, "this_request": asked,
		                "left_in_budget": round(row["budget"] - row["actual"] - row["committed"], 2) if row else None,
		                "over_after": bool(row and asked > row["budget"] - row["actual"] - row["committed"])})
	doc = frappe.new_doc(MR)
	doc.company = company
	doc.set("material_request_type", "Purchase")
	doc.transaction_date = frappe.utils.today()
	doc.schedule_date = needed_by or frappe.utils.today()
	for line in lines:
		doc.append("items", {"item_code": line["item_code"], "qty": line["qty"], "schedule_date": needed_by or
		                     frappe.utils.today(), "warehouse": warehouse or None})
	doc.flags.ignore_permissions = True
	doc.insert()
	return {"material_request": doc.name, "docstatus": 0, "estimated_value": value, "netted": netted,
	        "budget": landing, "needs_approval": bool(limit and value > limit), "approval_over": limit or None,
	        "by": actor or None, "next_step": "A person reviews the request and makes the Purchase Order."}


def draft_erpnext_budget(company: str, year, *, actor: str = "") -> dict:
	"""ERPNext Budget documents (one per cost centre, action Warn) from the published plan — DRAFTS."""
	_require_on(company)
	if not compat.doctype_exists(BUDGET):
		raise BudgetError("this site has no Budget doctype (ERPNext Accounts).")
	body = plan(company, year)
	by_centre: dict = {}
	for line in body.get("lines") or []:
		if not line.get("account") or not line.get("cost_center"):
			continue
		accounts = by_centre.setdefault(line["cost_center"], {})
		accounts[line["account"]] = accounts.get(line["account"], 0.0) + _num(line.get("annual"))
	made, skipped = [], []
	for centre, accounts in sorted(by_centre.items()):
		if not frappe.db.exists("Cost Center", centre):
			skipped.append(f"{centre}: no such Cost Center yet")
			continue
		missing = [a for a in accounts if not frappe.db.exists("Account", a)]
		if missing:
			skipped.append(f"{centre}: no Account {', '.join(missing[:3])}")
			continue
		doc = frappe.new_doc(BUDGET)
		doc.company = company
		doc.set("budget_against", "Cost Center")
		doc.set("cost_center", centre)
		doc.set("fiscal_year", str(year))
		for name in ("action_if_annual_budget_exceeded", "action_if_accumulated_monthly_budget_exceeded"):
			doc.set(name, "Warn")
		for account, amount in sorted(accounts.items()):
			doc.append("accounts", {"account": account, "budget_amount": round(amount, 2)})
		doc.flags.ignore_permissions = True
		doc.insert()
		made.append(doc.name)
	return {"budgets": made, "skipped": skipped, "docstatus": 0, "action": "Warn",
	        "next_step": "Submit each Budget in ERPNext when the plan is final; it warns, it does not stop."}


# ── usage → cost per block ────────────────────────────────────────────────────
def input_cost_by_block(company: str, year) -> dict:
	"""Spray materials put on each block this year, at each Item's valuation: rate/acre × block acres × $/unit."""
	if not compat.doctype_exists("Spray Application"):
		return {"blocks": [], "total": 0}
	start, end = _fy(year)
	out: dict = {}
	fields = compat.existing_fields("Spray Application", ("name", "company", "completed_at", "started_at",
	                                                     "products_applied", "status"))
	for row in frappe.db.get_all("Spray Application", filters={"company": company}, fields=fields, limit=20000) or []:
		day = str(row.get("completed_at") or row.get("started_at") or "")[:10]
		if not (start <= day <= end) or str(row.get("status") or "") == "Cancelled":
			continue
		try:
			products = json.loads(row.get("products_applied") or "[]")
		except ValueError:
			products = []
		for block in frappe.get_doc("Spray Application", row["name"]).get("blocks") or []:
			acres = _num(block.get("acres"))
			for product in products:
				cost = _num(product.get("rate_per_acre")) * acres * _rate(product.get("item"))
				entry = out.setdefault(block.get("block"), {"block": block.get("block"), "acres": acres, "cost": 0.0,
				                                           "applications": set()})
				entry["cost"] += cost
				entry["applications"].add(row["name"])
	blocks = [{**e, "cost": round(e["cost"], 2), "applications": len(e["applications"]),
	           "per_acre": round(e["cost"] / e["acres"], 2) if e["acres"] else None} for e in out.values()]
	blocks.sort(key=lambda b: -b["cost"])
	return {"company": company, "year": str(year), "blocks": blocks, "total": round(sum(b["cost"] for b in blocks), 2),
	        "note": "At each Item's valuation rate; a block's acres as recorded on the application."}

