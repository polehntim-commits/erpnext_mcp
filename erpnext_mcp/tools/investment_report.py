# SPDX-License-Identifier: MIT
"""`generate_quarterly_investment_report` — the report a manager owes a client.

KAIROS, NOT CHRONOS. A quarterly report is not due on a date; it is due when the
quarter is *actually closed*, and this refuses to produce one before then. Four
things have to be true, and the refusal names every one that is not — all of
them at once, so a single call answers "am I ready?" rather than sending the
caller round the loop four times:

  1. the quarter has ended;
  2. the quarter-end statement has been filed as a Prior Statement in the
     governance archive — a report written before the custodian's own statement
     arrived is a report written from a guess;
  3. no journal entry touching the investment accounts is still a draft, because
     an account that reconciles today and will not once three drafts are posted
     is not reconciled, it is about to not be;
  4. no bank transaction in the period is still unreconciled.

That is the whole design argument for this tool. A report generated on a
calendar date regardless of state is a report whose numbers may be wrong, signed
by somebody who assumed the schedule meant something.

PDF IS THE PRIMARY FORMAT, and that is a requirement rather than a preference: a
`.docx` handed over on 2026-07-29 could not be opened on the machine it was sent
to. `output_format="docx"` exists for a report that has to be edited before it is
signed, and the default is never it.

WHAT THIS DOES NOT INVENT. A benchmark rate. A high-water mark. External
contributions and withdrawals. Every one of those is an input, and when one is
missing the section that depends on it says so in words instead of quietly
substituting a zero — a performance fee computed against an assumed benchmark of
nothing is not conservative, it is wrong in the direction that costs the client.

WHERE THE NUMBERS COME FROM. Assets under management, activity and the cash
clearing balance are read from GL Entry, which exists only for submitted
vouchers. A holdings snapshot cannot be: this app talks to one ERPNext site and
the custodian's positions live somewhere else, so `holdings` is an argument an
operator who *can* reach that feed passes in. When it is passed, the report
reconciles it against the ledger and reports the variance; when it is not, the
holdings section says the ledger is the only source and does not pretend
otherwise.
"""

from __future__ import annotations

import frappe

from .. import __version__, compat
from ..args import as_bool, as_float, as_str, resolve_account, resolve_company
from ..errors import ToolError
from ..render.docx import DocxDocument
from ..render.pdf import PdfDocument
from ..result import ToolResult
from . import artifacts

GOVERNANCE_DOCUMENT = "Governance Document"
PRIOR_STATEMENT = "Prior Statement"

#: Quarter number → (start month/day, end month/day).
QUARTERS = {
	1: ("01-01", "03-31"),
	2: ("04-01", "06-30"),
	3: ("07-01", "09-30"),
	4: ("10-01", "12-31"),
}

#: How an investment account is recognised when the caller does not name one.
#: Matched against the account NAME, case-insensitively, over the company's
#: non-group accounts. Anything matching is listed in the report, so the reader
#: sees exactly what was included rather than trusting a keyword.
_INVESTMENT_KEYWORDS = (
	"marketable securities",
	"marketable security",
	"investment account",
	"investments",
	"investment",
	"brokerage",
	"securities",
)

#: And how the cash clearing account is recognised. Same rule: a shortlist that
#: is reported, never a guess that is hidden.
_CLEARING_KEYWORDS = ("cash clearing", "clearing account", "undeposited")

#: The Investment Management Agreement's split, as actually charged: 1.00% to the
#: custodian and 1.00% to the manager, inside a 2.00% cap. Arguments rather than
#: constants — a different client has a different agreement — but these are the
#: defaults because they are what the agreement in force says.
DEFAULT_MANAGER_FEE_PERCENT = 1.00
DEFAULT_CUSTODY_FEE_PERCENT = 1.00
DEFAULT_PERFORMANCE_FEE_PERCENT = 20.0

#: The cap the agreement puts on the two AUM fees together. Exceeding it is not
#: refused — a later agreement may raise it — but it is flagged, because a fee
#: schedule that has drifted past its own cap is the kind of thing nobody
#: notices until an auditor does.
AUM_FEE_CAP_PERCENT = 2.00

#: Activity rows carried into the report body before it summarises instead.
#: A report is meant to be read; four hundred journal lines is an export.
ACTIVITY_CAP = 250

#: Currency comparisons. Under half a cent is float noise.
TOLERANCE = 0.005


def _money(value) -> float:
	return round(float(value or 0), 2)


def _stated(entry: dict, key: str) -> bool:
	"""True when the caller actually put a value under `key`, zero included.

	Neither coercion in this module can answer this after the fact: `_money`
	turns None, "" and an explicit 0 all into 0.0, and `args.as_float` takes no
	default and does the same. So the question has to be asked of the RAW value,
	before it is coerced. `holdings` is an argument an operator passes rather
	than a stored column — see the module docstring — so "the custodian did not
	report this" and "the custodian reported zero" really are two different
	facts here, and only this check keeps them apart.
	"""
	return entry.get(key) not in (None, "")


def _fmt(value) -> str:
	return f"{float(value or 0):,.2f}"


def _pct(value) -> str:
	return f"{float(value or 0):,.4f}%"


# ── the quarter ─────────────────────────────────────────────────────────────
def parse_quarter(text: str) -> dict:
	"""`"2026-Q2"` → the year, the number, and the two dates that bound it."""
	raw = str(text or "").strip().upper().replace(" ", "")
	year = number = None
	if "-Q" in raw:
		head, _, tail = raw.partition("-Q")
		if head.isdigit() and tail.isdigit():
			year, number = int(head), int(tail)
	elif raw.startswith("Q") and len(raw) >= 6 and raw[1:2].isdigit():
		# "Q22026" from a caller that dropped the separator. Accepted because the
		# reading is unambiguous, which is the standard the rest of `args` holds.
		number, year = int(raw[1]), int(raw[2:]) if raw[2:].isdigit() else None
	if year is None or number not in QUARTERS:
		raise ToolError(
			f"quarter must look like '2026-Q2' — a four-digit year, a hyphen, Q, and 1 to 4. Got {text!r}."
		)
	if year < 1980 or year > 2200:
		raise ToolError(f"quarter names an implausible year: {year}. Got {text!r}.")
	start_md, end_md = QUARTERS[number]
	return {
		"label": f"{year}-Q{number}",
		"year": year,
		"number": number,
		"start": f"{year}-{start_md}",
		"end": f"{year}-{end_md}",
	}


# ── account discovery ───────────────────────────────────────────────────────
def _leaf_accounts(company: str) -> list[dict]:
	fields = compat.existing_fields(
		"Account", ("name", "account_name", "account_number", "account_type", "root_type", "is_group")
	)
	rows = frappe.db.get_all("Account", filters={"company": company}, fields=fields, limit=2000)
	return [dict(row) for row in rows if not int(row.get("is_group") or 0)]


def _match_accounts(company: str, keywords, what: str, required: bool) -> list[str]:
	matches = []
	for row in _leaf_accounts(company):
		name = str(row.get("account_name") or "").lower()
		if any(keyword in name for keyword in keywords):
			matches.append(row["name"])
	if matches or not required:
		return sorted(matches)
	raise ToolError(
		f"could not find {what} on {company}'s chart: no non-group account has a name containing "
		f"any of {', '.join(keywords)}. Name the accounts explicitly — this tool will not guess "
		"which account holds the portfolio. Nothing was created."
	)


def _resolve_accounts(args: dict, company: str) -> dict:
	requested = args.get("investment_accounts")
	if isinstance(requested, str):
		requested = [part.strip() for part in requested.split(",") if part.strip()]
	if requested:
		accounts = sorted({resolve_account(str(name), company) for name in requested})
		source = "named by the caller"
	else:
		accounts = _match_accounts(company, _INVESTMENT_KEYWORDS, "the investment accounts", True)
		source = "matched by name on this company's chart"

	clearing = as_str(args, "cash_clearing_account")
	if clearing:
		clearing_accounts = [resolve_account(clearing, company)]
	else:
		clearing_accounts = _match_accounts(company, _CLEARING_KEYWORDS, "a cash clearing account", False)
	return {"investment": accounts, "clearing": clearing_accounts, "source": source}


# ── ledger reads ────────────────────────────────────────────────────────────
def _balance(accounts, as_of: str) -> dict:
	if not accounts:
		return {"balance": 0.0, "debit": 0.0, "credit": 0.0, "entries": 0}
	row = (
		frappe.db.get_all(
			"GL Entry",
			filters={"account": ("in", list(accounts)), "is_cancelled": 0, "posting_date": ("<=", as_of)},
			fields=["sum(debit) as debit", "sum(credit) as credit", "count(name) as entries"],
		)
		or [{}]
	)[0] or {}
	debit = _money(row.get("debit"))
	credit = _money(row.get("credit"))
	return {
		"balance": _money(debit - credit),
		"debit": debit,
		"credit": credit,
		"entries": int(row.get("entries") or 0),
	}


def _activity(accounts, start: str, end: str) -> list[dict]:
	if not accounts:
		return []
	fields = compat.existing_fields(
		"GL Entry",
		(
			"name",
			"posting_date",
			"account",
			"debit",
			"credit",
			"voucher_type",
			"voucher_no",
			"cost_center",
			"party",
		),
	)
	rows = frappe.db.get_all(
		"GL Entry",
		filters={
			"account": ("in", list(accounts)),
			"is_cancelled": 0,
			"posting_date": ("between", (start, end)),
		},
		fields=fields,
		order_by="posting_date asc",
		limit=ACTIVITY_CAP + 1,
	)
	return [dict(row) for row in rows]


def _draft_entries(company: str, accounts, start: str, end: str) -> list[dict]:
	"""Draft journal entries in the period that touch the investment accounts.

	GL Entry cannot see a draft — that is the whole reason it is trustworthy for
	balances — so this goes through the Journal Entry Account child table, which
	is the only place a draft's lines exist.
	"""
	if not accounts:
		return []
	drafts = frappe.db.get_all(
		"Journal Entry",
		filters={"company": company, "docstatus": 0, "posting_date": ("between", (start, end))},
		fields=compat.existing_fields(
			"Journal Entry", ("name", "posting_date", "total_debit", "user_remark", "voucher_type")
		),
		order_by="posting_date asc",
		limit=500,
	)
	if not drafts:
		return []
	names = [row["name"] for row in drafts]
	lines = frappe.db.get_all(
		"Journal Entry Account",
		filters={"parent": ("in", names), "account": ("in", list(accounts))},
		fields=compat.existing_fields("Journal Entry Account", ("parent", "account", "debit", "credit")),
		limit=5000,
	)
	touched = {line.get("parent") for line in lines}
	return [dict(row) for row in drafts if row["name"] in touched]


def _unreconciled(company: str, start: str, end: str) -> dict:
	"""Bank transactions in the period that are not fully allocated.

	A site with no Bank Transaction doctype has nothing to reconcile, which is a
	pass rather than a failure — the precondition is "nothing is outstanding",
	and on such a site nothing is.
	"""
	if not compat.doctype_exists("Bank Transaction"):
		return {"applicable": False, "outstanding": [], "checked": 0}
	amounts = compat.bank_transaction_amount_fields()
	fields = compat.existing_fields(
		"Bank Transaction",
		(
			"name",
			"date",
			"status",
			"bank_account",
			"description",
			"unallocated_amount",
			"deposit",
			"withdrawal",
			"amount",
		),
	)
	rows = frappe.db.get_all(
		"Bank Transaction",
		filters={"company": company, "date": ("between", (start, end)), "docstatus": ("!=", 2)},
		fields=fields,
		order_by="date asc",
		limit=1000,
	)
	outstanding = []
	for row in rows:
		if str(row.get("status") or "") in ("Reconciled", "Settled"):
			continue
		unallocated = row.get(amounts["unallocated"]) if amounts["unallocated"] else None
		if unallocated is not None:
			if abs(float(unallocated or 0)) <= TOLERANCE:
				continue
		elif str(row.get("status") or "") not in ("Pending", "Unreconciled", ""):  # pragma: no cover
			continue
		outstanding.append(
			{
				"name": row.get("name"),
				"date": str(row.get("date") or ""),
				"status": row.get("status"),
				"unallocated": _money(unallocated) if unallocated is not None else None,
				"amount": _money(compat.signed_amount(row, amounts)),
			}
		)
	return {"applicable": True, "outstanding": outstanding, "checked": len(rows)}


def _statement(company: str, quarter: dict) -> dict:
	"""The Prior Statement filed for this quarter, if one has been."""
	if not compat.doctype_exists(GOVERNANCE_DOCUMENT):
		return {"applicable": False, "document": None}
	fields = compat.existing_fields(
		GOVERNANCE_DOCUMENT, ("name", "title", "effective_date", "execution_date", "attached_file")
	)
	rows = frappe.db.get_all(
		GOVERNANCE_DOCUMENT,
		filters={
			"company": company,
			"category": PRIOR_STATEMENT,
			"effective_date": ("between", (quarter["start"], quarter["end"])),
		},
		fields=fields,
		order_by="effective_date desc",
		limit=25,
	)
	return {"applicable": True, "document": dict(rows[0]) if rows else None, "count": len(rows)}


# ── the kairotic gate ───────────────────────────────────────────────────────
def _preconditions(company: str, quarter: dict, accounts: dict) -> dict:
	today = frappe.utils.today()
	checks = []

	quarter_over = quarter["end"] < today
	checks.append(
		{
			"check": "quarter_closed",
			"met": quarter_over,
			"detail": (
				f"{quarter['label']} ended {quarter['end']}."
				if quarter_over
				else f"quarter not yet closed — {quarter['label']} runs to {quarter['end']} and "
				f"today is {today}. There is no such thing as a report on a quarter that is "
				"still happening."
			),
		}
	)

	statement = _statement(company, quarter)
	filed = bool(statement.get("document"))
	checks.append(
		{
			"check": "statement_filed",
			"met": filed,
			"detail": (
				f"statement {statement['document']['name']} ({statement['document'].get('title')}) "
				f"is filed with effective date {statement['document'].get('effective_date')}."
				if filed
				else "no Prior Statement governance document is filed with an effective date "
				f"inside {quarter['start']}..{quarter['end']}. The custodian's own statement is "
				"what the report is checked against — file it with attach_governance_document "
				f"(category='{PRIOR_STATEMENT}') first."
			),
		}
	)

	drafts = _draft_entries(company, accounts["investment"], quarter["start"], quarter["end"])
	checks.append(
		{
			"check": "activity_submitted",
			"met": not drafts,
			"detail": (
				"every journal entry touching the investment accounts in this quarter is submitted."
				if not drafts
				else f"{len(drafts)} journal entr{'y is' if len(drafts) == 1 else 'ies are'} still "
				f"a draft: {', '.join(row['name'] for row in drafts[:8])}. Submit or delete them "
				"— an account that reconciles today and will not once these post is not "
				"reconciled, it is about to not be."
			),
			"entries": [row["name"] for row in drafts],
		}
	)

	reconciliation = _unreconciled(company, quarter["start"], quarter["end"])
	outstanding = reconciliation["outstanding"]
	checks.append(
		{
			"check": "bank_reconciled",
			"met": not outstanding,
			"detail": (
				(
					f"{reconciliation['checked']} bank transaction(s) in the quarter, all reconciled."
					if reconciliation["applicable"]
					else "this site has no Bank Transaction doctype, so there is nothing "
					"outstanding to reconcile."
				)
				if not outstanding
				else f"{len(outstanding)} bank transaction(s) in the quarter are unreconciled: "
				f"{', '.join(row['name'] for row in outstanding[:8])}. Clear them with "
				"reconcile_bank_transaction."
			),
			"transactions": [row["name"] for row in outstanding],
		}
	)

	return {
		"as_of": today,
		"checks": checks,
		"ready": all(check["met"] for check in checks),
		"statement": statement,
		"drafts": drafts,
		"reconciliation": reconciliation,
	}


def _refuse(quarter: dict, preconditions: dict) -> None:
	unmet = [check for check in preconditions["checks"] if not check["met"]]
	if not unmet:
		return
	# The quarter-not-closed case leads, because it is the one a caller is most
	# likely to have got wrong and the only one they cannot fix by doing work.
	unmet.sort(key=lambda check: 0 if check["check"] == "quarter_closed" else 1)
	lines = "; ".join(check["detail"] for check in unmet)
	raise ToolError(
		f"{quarter['label']} is not ready to report on. {lines} All {len(unmet)} of these are "
		"listed at once rather than one per call, so this answers 'what is left' in a single "
		"reply. Nothing was created."
	)


# ── the figures ─────────────────────────────────────────────────────────────
def _fees(opening: float, closing: float, args: dict) -> dict:
	manager_percent = _percent_arg(args, "manager_fee_percent", DEFAULT_MANAGER_FEE_PERCENT)
	custody_percent = _percent_arg(args, "custody_fee_percent", DEFAULT_CUSTODY_FEE_PERCENT)
	average = _money((opening + closing) / 2)
	manager = _money(average * manager_percent / 100 / 4)
	custody = _money(average * custody_percent / 100 / 4)
	total_percent = round(manager_percent + custody_percent, 6)
	return {
		"basis": "average of opening and closing assets under management, charged quarterly",
		"average_aum": average,
		"manager_fee_percent": manager_percent,
		"custody_fee_percent": custody_percent,
		"combined_percent": total_percent,
		"manager_fee": manager,
		"custody_fee": custody,
		"total_fee": _money(manager + custody),
		"annualised_total": _money(average * total_percent / 100),
		"over_cap": total_percent > AUM_FEE_CAP_PERCENT + 1e-9,
		"cap_percent": AUM_FEE_CAP_PERCENT,
		"note": (
			"An accrual, not a posting. Nothing here debits a fee account or moves money — the "
			"figure is what the agreement says is owed for the quarter, and booking it is a "
			"separate, separately-enabled act."
		),
	}


def _percent_arg(args: dict, key: str, default: float) -> float:
	raw = args.get(key)
	value = default if raw in (None, "") else as_float(raw, key)
	if value < 0 or value > 100:
		raise ToolError(f"{key} must be between 0 and 100, got {value}. Nothing was created.")
	return round(value, 6)


def _performance(opening: float, closing: float, args: dict) -> dict:
	net_contributions = _money(as_float(args.get("net_contributions"), "net_contributions"))
	gain = _money(closing - opening - net_contributions)
	gross_return = round(gain / opening * 100, 6) if opening else None

	# `note` is on BOTH branches of this function, and on purpose: the renderer
	# prints it unconditionally, and a key that exists only when a figure was
	# computed is a KeyError waiting for the first report generated without a
	# benchmark — which is the ordinary case.
	contribution_note = "Return is measured as closing minus opening minus net contributions. " + (
		"net_contributions was not supplied and is treated as zero, which is right only if "
		"no money entered or left the account this quarter — check that before relying on "
		"the return figure."
		if not net_contributions
		else "net_contributions was supplied by the caller."
	)

	raw_benchmark = args.get("benchmark_rate_percent")
	if raw_benchmark in (None, ""):
		return {
			"computed": False,
			"net_contributions": net_contributions,
			"period_gain": gain,
			"gross_return_percent": gross_return,
			"performance_fee": None,
			"note": contribution_note,
			"reason": (
				"No benchmark_rate_percent was supplied, so the return over benchmark and the "
				"performance fee are NOT computed. They are not zero and they are not estimated: "
				"the 10-year Treasury yield is a market fact this site does not hold, and a "
				"performance fee computed against an assumed benchmark of nothing would overstate "
				"what the manager is owed."
			),
		}

	benchmark_annual = _percent_arg(args, "benchmark_rate_percent", 0.0)
	benchmark_quarter = round(benchmark_annual / 4, 6)
	benchmark_gain = _money(opening * benchmark_quarter / 100)
	excess = _money(gain - benchmark_gain)

	performance_percent = _percent_arg(args, "performance_fee_percent", DEFAULT_PERFORMANCE_FEE_PERCENT)
	raw_hwm = args.get("high_water_mark")
	high_water_mark = None if raw_hwm in (None, "") else _money(as_float(raw_hwm, "high_water_mark"))

	eligible = max(0.0, excess)
	hwm_note = "No high-water mark was supplied, so the fee is computed on the whole excess return."
	if high_water_mark is not None:
		headroom = _money(closing - high_water_mark)
		if headroom <= 0:
			eligible = 0.0
			hwm_note = (
				f"Closing assets of {_fmt(closing)} are at or below the high-water mark of "
				f"{_fmt(high_water_mark)}, so no performance fee is earned this quarter however "
				"the quarter itself went. That is what a high-water mark is for."
			)
		else:
			eligible = _money(min(eligible, headroom))
			hwm_note = (
				f"Closing assets exceed the high-water mark of {_fmt(high_water_mark)} by "
				f"{_fmt(headroom)}, which caps the fee-eligible gain."
			)

	return {
		"computed": True,
		"net_contributions": net_contributions,
		"period_gain": gain,
		"gross_return_percent": gross_return,
		"benchmark_annual_percent": benchmark_annual,
		"benchmark_quarter_percent": benchmark_quarter,
		"benchmark_gain": benchmark_gain,
		"gain_over_benchmark": excess,
		"high_water_mark": high_water_mark,
		"fee_eligible_gain": eligible,
		"performance_fee_percent": performance_percent,
		"performance_fee": _money(eligible * performance_percent / 100),
		"high_water_mark_note": hwm_note,
		"note": contribution_note,
	}


def _holdings(args: dict, closing: float) -> dict:
	raw = args.get("holdings")
	if not raw:
		return {
			"supplied": False,
			"source": "general ledger only",
			"positions": [],
			"note": (
				"No holdings snapshot was passed. This app reads one ERPNext site and the "
				"custodian's positions are not on it, so assets under management here are the "
				"ledger balance of the investment accounts and nothing else. An operator who can "
				"reach the custodian's feed should pass `holdings` — the report then reconciles "
				"the two and reports the variance."
			),
		}
	if not isinstance(raw, list):
		raise ToolError("holdings must be a list of position objects. Nothing was created.")

	positions = []
	total = 0.0
	for index, item in enumerate(raw, start=1):
		if not isinstance(item, dict):
			raise ToolError(f"holdings[{index}] is not an object. Nothing was created.")
		market_value = _money(item.get("market_value"))
		if not market_value and _stated(item, "quantity") and _stated(item, "price"):
			market_value = _money(
				as_float(item.get("quantity"), "quantity") * as_float(item.get("price"), "price")
			)
		total += market_value
		# Not `... or None` on these three: a custodian who reports a position at
		# zero — a lot sold out during the quarter, a right that expired worthless,
		# a holding written down to nothing — SAID zero, and `or None` files that
		# under "not reported". The coercions cannot tell the two apart once they
		# have run, so `_stated` asks the raw value first.
		positions.append(
			{
				"symbol": str(item.get("symbol") or "").strip() or None,
				"description": str(item.get("description") or "").strip() or None,
				"quantity": (
					as_float(item.get("quantity"), "quantity") if _stated(item, "quantity") else None
				),
				"price": _money(item.get("price")) if _stated(item, "price") else None,
				"market_value": market_value,
				"cost_basis": _money(item.get("cost_basis")) if _stated(item, "cost_basis") else None,
			}
		)
	total = _money(total)
	variance = _money(total - closing)
	return {
		"supplied": True,
		"source": "custodian snapshot passed by the caller",
		"positions": positions,
		"position_count": len(positions),
		"market_value": total,
		"ledger_balance": closing,
		"variance": variance,
		"reconciles": abs(variance) <= TOLERANCE,
		"note": (
			"The snapshot and the ledger agree."
			if abs(variance) <= TOLERANCE
			else f"The snapshot totals {_fmt(total)} against a ledger balance of {_fmt(closing)} — "
			f"a difference of {_fmt(variance)}. That is a mark-to-market that has not been "
			"posted, a trade that has not been booked, or a snapshot from the wrong date. It is "
			"reported rather than reconciled away."
		),
	}


# ── 92. generate_quarterly_investment_report ────────────────────────────────
def generate_quarterly_investment_report(args: dict) -> ToolResult:
	"""Build the quarter's report — but only once the quarter is genuinely closed."""
	company = resolve_company(as_str(args, "company"), required=True)
	quarter = parse_quarter(as_str(args, "quarter", required=True))
	output_format = (as_str(args, "output_format") or "pdf").strip().lower()
	if output_format not in ("pdf", "docx"):
		raise ToolError(
			f"output_format must be 'pdf' or 'docx', got {output_format!r}. PDF is the default "
			"and the one to use: a .docx is a file the recipient may not be able to open, which "
			"is exactly what happened on 2026-07-29. Nothing was created."
		)

	accounts = _resolve_accounts(args, company)
	preconditions = _preconditions(company, quarter, accounts)
	_refuse(quarter, preconditions)

	day_before = frappe.utils.add_days(frappe.utils.getdate(quarter["start"]), -1).isoformat()
	opening = _balance(accounts["investment"], day_before)
	closing = _balance(accounts["investment"], quarter["end"])
	activity = _activity(accounts["investment"], quarter["start"], quarter["end"])
	truncated = len(activity) > ACTIVITY_CAP
	if truncated:
		activity = activity[:ACTIVITY_CAP]

	clearing_balance = _balance(accounts["clearing"], quarter["end"])
	fees = _fees(opening["balance"], closing["balance"], args)
	performance = _performance(opening["balance"], closing["balance"], args)
	holdings = _holdings(args, closing["balance"])

	report = {
		"company": company,
		"quarter": quarter,
		"generated_at": str(frappe.utils.now()),
		"generated_by": str(frappe.session.user),
		"site": str(frappe.local.site),
		"generator_version": __version__,
		"accounts": {
			"investment": accounts["investment"],
			"cash_clearing": accounts["clearing"],
			"discovery": accounts["source"],
		},
		"aum": {
			"opening": opening["balance"],
			"opening_as_of": day_before,
			"closing": closing["balance"],
			"closing_as_of": quarter["end"],
			"change": _money(closing["balance"] - opening["balance"]),
			"gl_entries_to_date": closing["entries"],
		},
		"activity": {
			"rows": [
				{
					"posting_date": str(row.get("posting_date") or ""),
					"account": row.get("account"),
					"voucher_type": row.get("voucher_type"),
					"voucher_no": row.get("voucher_no"),
					"cost_center": row.get("cost_center"),
					"debit": _money(row.get("debit")),
					"credit": _money(row.get("credit")),
				}
				for row in activity
			],
			"count": len(activity),
			"truncated": truncated,
			"debit_total": _money(sum(float(row.get("debit") or 0) for row in activity)),
			"credit_total": _money(sum(float(row.get("credit") or 0) for row in activity)),
		},
		"fees": fees,
		"performance": performance,
		"holdings": holdings,
		"cash_clearing": {
			"accounts": accounts["clearing"],
			"balance": clearing_balance["balance"],
			"as_of": quarter["end"],
			"clear": abs(clearing_balance["balance"]) <= TOLERANCE,
			"note": (
				(
					"Clear at quarter end: nothing was in transit across the boundary."
					if abs(clearing_balance["balance"]) <= TOLERANCE
					else "A cash clearing account holds money in transit, and this one does not "
					"close at zero — something left one side of a transfer and has not arrived "
					"at the other."
				)
				if accounts["clearing"]
				else "No cash clearing account was found or named on this company's chart, so "
				"there is nothing in transit to report."
			),
		},
		"reconciliation": {
			"statement": preconditions["statement"].get("document"),
			"bank_transactions_checked": preconditions["reconciliation"]["checked"],
			"bank_transactions_outstanding": len(preconditions["reconciliation"]["outstanding"]),
			"draft_entries": len(preconditions["drafts"]),
			"note": (
				"Every one of these was a precondition of producing this report at all. They are "
				"restated here because a reader six months from now should not have to take on "
				"trust that they were checked."
			),
		},
		"preconditions": preconditions["checks"],
	}

	warnings = []
	if truncated:
		warnings.append(
			f"More than {ACTIVITY_CAP} ledger rows fell in this quarter; the activity table shows "
			f"the first {ACTIVITY_CAP} and the totals beneath it cover only those. Run the "
			"reconciliation packet for the full detail."
		)
	if fees["over_cap"]:
		warnings.append(
			f"Manager and custody fees total {fees['combined_percent']}% of assets, above the "
			f"{AUM_FEE_CAP_PERCENT}% cap the agreement in force sets. Not refused — a later "
			"agreement may raise it — but check which agreement is being applied."
		)
	if holdings["supplied"] and not holdings["reconciles"]:
		warnings.append(holdings["note"])
	if not report["cash_clearing"]["clear"] and accounts["clearing"]:
		warnings.append(
			f"Cash clearing closes at {_fmt(report['cash_clearing']['balance'])} rather than zero. "
			"Something is in transit across the quarter boundary."
		)
	if warnings:
		report["warnings"] = warnings

	if as_bool(args, "dry_run", False):
		report["dry_run"] = True
		report["would_produce"] = {
			"file": _file_name(company, quarter, output_format),
			"governance_document": _archive_title(company, quarter),
			"category": PRIOR_STATEMENT,
		}
		report["note"] = (
			"Nothing was written. Every precondition passed and the figures above are the ones "
			"the document would carry."
		)
		report["mcp_action_log_id"] = None
		return ToolResult(
			report,
			f"dry run: {quarter['label']} report for {company} is ready to generate — "
			f"AUM {_fmt(closing['balance'])}, manager fee {_fmt(fees['manager_fee'])}",
		)

	# Resolved before anything is created, so a bad path refuses the whole run
	# rather than leaving an archive entry behind.
	target = artifacts.resolve_output_path(
		as_str(args, "output_path"), _file_name(company, quarter, output_format)
	)

	payload = _render_pdf(report) if output_format == "pdf" else _render_docx(report)
	archive = _archive(company, quarter, args, closing["balance"])
	file_name = _file_name(company, quarter, output_format)
	attachment = artifacts.attach_bytes(
		GOVERNANCE_DOCUMENT, archive.name, file_name, payload, field="attached_file"
	)

	report["governance_document"] = archive.name
	report["document"] = artifacts.describe_attachment(attachment, payload)
	report["output_format"] = output_format
	report["written_to_disk"] = (
		[artifacts.write_output(target, payload, as_bool(args, "overwrite", False))] if target else []
	)
	report["mcp_action_log_id"] = None
	report["note"] = (
		f"Filed as a {PRIOR_STATEMENT} in the governance archive, with the {output_format.upper()} "
		"attached as a private File. Read it back with get_governance_document_content. The "
		"MCP Action Log row for this call is in this result rather than on the page — it does "
		"not exist until the call returns."
	)
	report["next_step"] = (
		"The manager fee above is an ACCRUAL and nothing posted it. Book it with "
		"create_journal_entry when it is charged, against the investment cost center."
	)

	return ToolResult(
		report,
		f"generated the {quarter['label']} investment report for {company} as {archive.name}: "
		f"AUM {_fmt(closing['balance'])}, manager fee {_fmt(fees['manager_fee'])}, "
		f"{len(activity)} ledger row(s)",
		docstatus_delta="none → 0 (created)",
	)


def _file_name(company: str, quarter: dict, output_format: str) -> str:
	slug = "".join(char if (char.isalnum() or char in "-_") else "-" for char in company).strip("-")
	while "--" in slug:
		slug = slug.replace("--", "-")
	return f"investment-report-{quarter['label']}-{slug or 'company'}.{output_format}"


def _archive_title(company: str, quarter: dict) -> str:
	return f"Quarterly Investment Report {quarter['label']} — {company}"


def _archive(company: str, quarter: dict, args: dict, closing: float):
	compat.require_doctype(GOVERNANCE_DOCUMENT, "It ships with erpnext_mcp.")
	title = as_str(args, "title") or _archive_title(company, quarter)
	existing = frappe.db.get_value(GOVERNANCE_DOCUMENT, {"company": company, "title": title}, "name")
	if existing:
		raise ToolError(
			f"{company} already has a governance document titled {title!r} ({existing}) — this "
			f"quarter has been reported on before. A second document under the same title would "
			"leave two claiming to be the report of record. Pass a `title` that distinguishes the "
			"re-run, or read the existing one back with get_governance_document_content. Nothing "
			"was created."
		)
	doc = frappe.new_doc(GOVERNANCE_DOCUMENT)
	doc.title = title
	doc.category = PRIOR_STATEMENT
	doc.company = company
	doc.effective_date = quarter["end"]
	doc.parties = f"Client: {company}. Report prepared by the Investment Manager."
	doc.notes = (
		f"Assets under management at {quarter['end']}: {_fmt(closing)}. Generated by erpnext_mcp "
		f"{__version__} from GL Entry; produced only after the quarter closed, the statement was "
		"filed, every investment journal entry was submitted and every bank transaction in the "
		"period was reconciled."
	)
	doc.insert()
	return doc


# ── rendering ───────────────────────────────────────────────────────────────
def _sections(report: dict):
	"""The report's content, as (kind, payload) pairs both renderers walk.

	One description of the document, two output formats. A PDF and a DOCX that
	were written separately would drift, and the first anybody would know is a
	client comparing the copy they were emailed with the copy in the archive.
	"""
	quarter = report["quarter"]
	aum = report["aum"]
	fees = report["fees"]
	performance = report["performance"]
	holdings = report["holdings"]

	yield (
		"title",
		(
			"QUARTERLY INVESTMENT REPORT",
			[report["company"], f"{quarter['label']} — quarter ended {quarter['end']}"],
		),
	)

	yield ("heading", "Executive summary")
	yield (
		"key_values",
		[
			("Assets under management", _fmt(aum["closing"])),
			("Opening assets", _fmt(aum["opening"])),
			("Change over the quarter", _fmt(aum["change"])),
			("Manager fee accrued", _fmt(fees["manager_fee"])),
			("Custody fee accrued", _fmt(fees["custody_fee"])),
			(
				"Performance fee",
				_fmt(performance["performance_fee"]) if performance["computed"] else "not computed",
			),
		],
	)

	yield ("heading", "Assets under management")
	yield (
		"key_values",
		[
			(f"Opening ({aum['opening_as_of']})", _fmt(aum["opening"])),
			(f"Closing ({aum['closing_as_of']})", _fmt(aum["closing"])),
			("Change", _fmt(aum["change"])),
			("Accounts included", ", ".join(report["accounts"]["investment"]) or "none"),
			("How those were chosen", report["accounts"]["discovery"]),
		],
	)
	yield (
		"paragraph",
		"Balances are summed from GL Entry, which exists only for submitted vouchers. Cancelled "
		"entries carry no GL row and are therefore absent rather than filtered out.",
	)

	yield ("heading", "Holdings")
	if holdings["supplied"]:
		yield (
			"table",
			(
				["Symbol", "Description", "Quantity", "Price", "Market value", "Cost basis"],
				[
					[
						position["symbol"] or "",
						position["description"] or "",
						# `is not None`, not truthiness: the payload above now keeps a
						# stated zero, and a blank cell on the page would throw it away
						# again one layer further out.
						f"{position['quantity']:,.4f}" if position["quantity"] is not None else "",
						_fmt(position["price"]) if position["price"] is not None else "",
						_fmt(position["market_value"]),
						_fmt(position["cost_basis"]) if position["cost_basis"] is not None else "",
					]
					for position in holdings["positions"]
				],
				("l", "l", "r", "r", "r", "r"),
			),
		)
		yield (
			"key_values",
			[
				("Snapshot market value", _fmt(holdings["market_value"])),
				("Ledger balance", _fmt(holdings["ledger_balance"])),
				("Variance", _fmt(holdings["variance"])),
			],
		)
	yield ("paragraph", holdings["note"])

	yield ("heading", "Activity")
	activity = report["activity"]
	if activity["rows"]:
		yield (
			"table",
			(
				["Date", "Voucher", "Account", "Cost centre", "Debit", "Credit"],
				[
					[
						row["posting_date"],
						row["voucher_no"] or "",
						row["account"] or "",
						row["cost_center"] or "",
						_fmt(row["debit"]),
						_fmt(row["credit"]),
					]
					for row in activity["rows"]
				],
				("l", "l", "l", "l", "r", "r"),
			),
		)
		yield (
			"key_values",
			[
				("Rows", str(activity["count"])),
				("Total debits", _fmt(activity["debit_total"])),
				("Total credits", _fmt(activity["credit_total"])),
			],
		)
	else:
		yield ("paragraph", "No ledger activity in the investment accounts this quarter.")
	if activity["truncated"]:
		yield (
			"paragraph",
			f"TRUNCATED: more than {ACTIVITY_CAP} rows fell in this quarter and the table above "
			"shows the first of them. The totals beneath it cover only the rows shown.",
		)

	yield ("heading", "Management and custody fees")
	yield (
		"key_values",
		[
			("Average assets under management", _fmt(fees["average_aum"])),
			("Manager fee rate (annual)", _pct(fees["manager_fee_percent"])),
			("Custody fee rate (annual)", _pct(fees["custody_fee_percent"])),
			("Combined rate", _pct(fees["combined_percent"])),
			("Manager fee this quarter", _fmt(fees["manager_fee"])),
			("Custody fee this quarter", _fmt(fees["custody_fee"])),
			("Total fee this quarter", _fmt(fees["total_fee"])),
		],
	)
	yield ("paragraph", f"Basis: {fees['basis']}. {fees['note']}")
	if fees["over_cap"]:
		yield (
			"paragraph",
			f"NOTE: the combined rate is above the {AUM_FEE_CAP_PERCENT}% cap in the agreement in "
			"force. Check which agreement is being applied.",
		)

	yield ("heading", "Performance")
	if performance["computed"]:
		yield (
			"key_values",
			[
				("Net contributions", _fmt(performance["net_contributions"])),
				("Gain over the quarter", _fmt(performance["period_gain"])),
				(
					"Gross return",
					_pct(performance["gross_return_percent"])
					if performance["gross_return_percent"] is not None
					else "not computable from a zero opening balance",
				),
				("Benchmark (annual)", _pct(performance["benchmark_annual_percent"])),
				("Benchmark (quarter)", _pct(performance["benchmark_quarter_percent"])),
				("Benchmark gain", _fmt(performance["benchmark_gain"])),
				("Gain over benchmark", _fmt(performance["gain_over_benchmark"])),
				(
					"High-water mark",
					_fmt(performance["high_water_mark"])
					if performance["high_water_mark"] is not None
					else "none supplied",
				),
				("Fee-eligible gain", _fmt(performance["fee_eligible_gain"])),
				("Performance fee rate", _pct(performance["performance_fee_percent"])),
				("Performance fee", _fmt(performance["performance_fee"])),
			],
		)
		yield ("paragraph", performance["high_water_mark_note"])
	else:
		yield ("paragraph", performance["reason"])
		yield (
			"key_values",
			[
				("Net contributions", _fmt(performance["net_contributions"])),
				("Gain over the quarter", _fmt(performance["period_gain"])),
				(
					"Gross return",
					_pct(performance["gross_return_percent"])
					if performance["gross_return_percent"] is not None
					else "not computable from a zero opening balance",
				),
			],
		)
	yield ("paragraph", performance["note"])

	yield ("heading", "Cash clearing")
	clearing = report["cash_clearing"]
	yield (
		"key_values",
		[
			("Accounts", ", ".join(clearing["accounts"]) or "none found"),
			(f"Balance at {clearing['as_of']}", _fmt(clearing["balance"])),
			("Clear", "yes" if clearing["clear"] else "NO — money is in transit"),
		],
	)
	yield ("paragraph", clearing["note"])

	yield ("heading", "Reconciliation and readiness")
	yield (
		"table",
		(
			["Precondition", "Met", "Detail"],
			[
				[check["check"], "yes" if check["met"] else "NO", check["detail"]]
				for check in report["preconditions"]
			],
			("l", "c", "l"),
		),
	)
	statement = report["reconciliation"]["statement"] or {}
	yield (
		"key_values",
		[
			("Statement of record", statement.get("name") or "none"),
			("Statement title", statement.get("title") or ""),
			("Statement effective date", str(statement.get("effective_date") or "")),
			("Bank transactions checked", str(report["reconciliation"]["bank_transactions_checked"])),
			("Bank transactions outstanding", str(report["reconciliation"]["bank_transactions_outstanding"])),
			("Draft journal entries", str(report["reconciliation"]["draft_entries"])),
		],
	)

	yield ("heading", "Provenance")
	yield (
		"key_values",
		[
			("Generated at", report["generated_at"]),
			("Generated by", report["generated_by"]),
			("Site", report["site"]),
			("Generator", f"erpnext_mcp {report['generator_version']}"),
			("Source", "GL Entry (submitted vouchers only)"),
		],
	)
	yield (
		"paragraph",
		"This report was produced only after the quarter closed, the custodian's statement was "
		"filed in the governance archive, every journal entry touching the investment accounts "
		"was submitted, and every bank transaction in the period was reconciled. A report on a "
		"quarter that was not yet closed is refused rather than caveated.",
	)
	for warning in report.get("warnings", ()):
		yield ("paragraph", f"WARNING: {warning}")


def _render_pdf(report: dict) -> bytes:
	quarter = report["quarter"]
	document = PdfDocument(
		title=_archive_title(report["company"], quarter),
		author="erpnext_mcp",
		subject=f"Investment report {quarter['label']}",
		footer=f"{report['company']} — {quarter['label']} — erpnext_mcp {report['generator_version']}",
	)
	for kind, payload in _sections(report):
		if kind == "title":
			document.title_block(payload[0], *payload[1])
		elif kind == "heading":
			document.heading(payload)
		elif kind == "paragraph":
			document.paragraph(payload)
			document.spacer(3)
		elif kind == "key_values":
			document.key_values(payload)
			document.spacer(3)
		elif kind == "table":
			headers, rows, align = payload
			document.table(headers, rows, align=align)
			document.spacer(3)
	return document.render()


def _render_docx(report: dict) -> bytes:
	quarter = report["quarter"]
	document = DocxDocument(
		title=_archive_title(report["company"], quarter),
		subject=f"Investment report {quarter['label']}",
	)
	for kind, payload in _sections(report):
		if kind == "title":
			document.title_block(payload[0], *payload[1])
		elif kind == "heading":
			document.heading(payload)
		elif kind == "paragraph":
			document.paragraph(payload)
		elif kind == "key_values":
			document.key_values(payload)
			document.spacer()
		elif kind == "table":
			headers, rows, _align = payload
			document.table(headers, rows)
			document.spacer()
	return document.render()
