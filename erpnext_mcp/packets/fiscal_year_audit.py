# SPDX-License-Identifier: MIT
"""`fiscal_year_audit_packet` — what an outside CPA asks for on day one.

THE TWO BASES, AND WHY THE PACKET SAYS SO. A trial balance is not one thing.
Balance-sheet accounts (Asset, Liability, Equity) carry forward, so their closing
balance is cumulative from the beginning of the ledger. Profit-and-loss accounts
(Income, Expense) start each year at zero, so theirs is the movement *within* the
year. Mixing the two silently is how a trial balance comes out not balancing and
somebody spends an afternoon on it. Every row here carries its `basis`, and the
debit/credit totals used for the balance check are computed separately, on a
single cumulative basis, where they must agree by construction.

THE ACCOUNTING IDENTITY IS CHECKED. For an unclosed year:

    Assets - (Liabilities + Equity) = Income - Expense

Both sides are computed independently from the same ledger. If they disagree by
more than a cent, something is wrong with the books or with this packet, and
either way it is an ERROR and the packet is not signable. This is the single most
valuable line in the file: it is the check a CPA would do by hand first.

INTERCOMPANY IS FOUND BY LOOKING, NOT BY TRUSTING. A Journal Entry carries a
`company`, but its account lines carry their own — and nothing stops a line
pointing at another company's account. The packet resolves every line's account
to its company and reports any entry that spans more than one, which is the sort
of thing that is invisible until consolidation.
"""

import frappe

from .. import compat
from ..args import as_str, resolve_company
from ..errors import ToolError
from .base import (
	SEVERITY_ERROR,
	SEVERITY_INFO,
	SEVERITY_WARN,
	TOLERANCE,
	Flag,
	PacketResult,
	PacketSpec,
	cap,
	money,
	register,
)

BALANCE_SHEET_ROOTS = ("Asset", "Liability", "Equity")
PROFIT_AND_LOSS_ROOTS = ("Income", "Expense")
CREDIT_ROOTS = ("Liability", "Income", "Equity")

TOP_ENTRIES = 20


def build(filters: dict) -> PacketResult:
	company = resolve_company(as_str(filters, "company", required=True), required=True)
	fiscal_year = as_str(filters, "fiscal_year", required=True)
	year = frappe.db.get_value(
		"Fiscal Year", fiscal_year, ["name", "year_start_date", "year_end_date", "disabled"], as_dict=True
	)
	if not year:
		known = frappe.db.get_all("Fiscal Year", pluck="name", order_by="year_start_date desc", limit=10)
		raise ToolError(
			f"no Fiscal Year named {fiscal_year!r}. Known: {', '.join(known) or '<none>'}. "
			"list_fiscal_years shows which apply to which company."
		)
	start = str(year["year_start_date"])
	end = str(year["year_end_date"])
	flags: list = []

	accounts = _accounts(company)
	cumulative = _aggregate(company, to_date=end)
	in_year = _aggregate(company, from_date=start, to_date=end)

	trial_balance, totals_by_root = _trial_balance(accounts, cumulative, in_year)
	income_statement = _income_statement(totals_by_root)
	balance_sheet = _balance_sheet(totals_by_root)
	raw_totals = _raw_totals(company, end)

	data = {
		"company": company,
		"fiscal_year": fiscal_year,
		"date_range": {"start": start, "end": end, "disabled": bool(year.get("disabled"))},
		"trial_balance": trial_balance,
		"trial_balance_totals": raw_totals,
		"income_statement": income_statement,
		"balance_sheet": balance_sheet,
		"top_20_entries_by_amount": _top_entries(company, start, end),
		"intercompany_activity": _intercompany(company, start, end, flags),
		"document_counts": _document_counts(company, start, end),
		"basis_note": (
			"Balance-sheet accounts (Asset/Liability/Equity) are cumulative to the "
			"year end because they carry forward. Profit-and-loss accounts "
			"(Income/Expense) are movement within the year because they reset. "
			"Each row states its own `basis`. trial_balance_totals is a single "
			"cumulative aggregate over every account, which is where debits and "
			"credits must agree."
		),
		"external_sources": [],
	}

	_check_trial_balance(raw_totals, flags)
	_check_accounting_identity(data, income_statement, balance_sheet, flags)
	_check_documents(data["document_counts"], flags)
	_check_unnatural_balances(trial_balance, flags)
	_check_year_shape(year, company, flags)

	summary = (
		f"fiscal year audit packet for {company} {fiscal_year} ({start}..{end}): "
		f"{len(trial_balance)} account(s), net income "
		f"{income_statement['net_income']}, {len(flags)} flag(s)"
	)
	return PacketResult(data=data, flags=flags, summary=summary)


# ── ledger aggregates ───────────────────────────────────────────────────────
def _accounts(company: str) -> dict:
	rows = frappe.db.get_all(
		"Account",
		filters={"company": company},
		fields=compat.existing_fields(
			"Account",
			["name", "account_name", "account_number", "root_type", "account_type", "is_group"],
		),
	)
	return {row["name"]: row for row in rows}


def _gl_filters(company: str, from_date: str = "", to_date: str = "") -> dict:
	filters = {"company": company}
	if compat.has_field("GL Entry", "is_cancelled"):
		filters["is_cancelled"] = 0
	if from_date and to_date:
		filters["posting_date"] = ("between", [from_date, to_date])
	elif to_date:
		filters["posting_date"] = ("<=", to_date)
	return filters


def _aggregate(company: str, from_date: str = "", to_date: str = "") -> dict:
	"""{account: {debit, credit}} over the requested window."""
	rows = frappe.db.get_all(
		"GL Entry",
		filters=_gl_filters(company, from_date, to_date),
		fields=["account", "sum(debit) as debit", "sum(credit) as credit"],
		group_by="account",
	)
	return {
		row["account"]: {"debit": float(row.get("debit") or 0), "credit": float(row.get("credit") or 0)}
		for row in rows
		if row.get("account")
	}


def _raw_totals(company: str, end: str) -> dict:
	"""One cumulative aggregate over everything — where debits must equal credits."""
	row = (
		frappe.db.get_all(
			"GL Entry",
			filters=_gl_filters(company, to_date=end),
			fields=["sum(debit) as debit", "sum(credit) as credit", "count(name) as entries"],
		)
		or [{}]
	)[0] or {}
	debit = money(row.get("debit"))
	credit = money(row.get("credit"))
	return {
		"debit": debit,
		"credit": credit,
		"difference": money(debit - credit),
		"gl_entry_count": int(row.get("entries") or 0),
		"basis": "cumulative to year end, every account",
	}


def _trial_balance(accounts: dict, cumulative: dict, in_year: dict):
	rows, totals_by_root = [], {}
	for name, account in sorted(accounts.items()):
		root = account.get("root_type") or "<unset>"
		basis = "fiscal_year" if root in PROFIT_AND_LOSS_ROOTS else "cumulative"
		source = in_year if basis == "fiscal_year" else cumulative
		amounts = source.get(name) or {}
		debit = money(amounts.get("debit"))
		credit = money(amounts.get("credit"))
		balance = money(debit - credit)
		if not debit and not credit and account.get("is_group"):
			# A group account with no direct postings adds nothing but noise.
			continue
		natural = money(-balance if root in CREDIT_ROOTS else balance)
		rows.append(
			{
				"account": name,
				"account_name": account.get("account_name"),
				"account_number": account.get("account_number"),
				"root_type": root,
				"account_type": account.get("account_type"),
				"is_group": bool(account.get("is_group")),
				"basis": basis,
				"debit": debit,
				"credit": credit,
				"balance": balance,
				"balance_natural": natural,
			}
		)
		bucket = totals_by_root.setdefault(root, {"debit": 0.0, "credit": 0.0, "accounts": 0})
		# Group accounts would double-count their children's postings only if the
		# ledger posted to both; GL Entry posts to leaves, so summing every row
		# with activity is correct.
		bucket["debit"] += debit
		bucket["credit"] += credit
		bucket["accounts"] += 1

	for root, bucket in totals_by_root.items():
		bucket["debit"] = money(bucket["debit"])
		bucket["credit"] = money(bucket["credit"])
		bucket["balance"] = money(bucket["debit"] - bucket["credit"])
		bucket["balance_natural"] = money(-bucket["balance"] if root in CREDIT_ROOTS else bucket["balance"])

	grouped = {}
	for row in rows:
		grouped.setdefault(row["root_type"], []).append(row)
	return {"by_root_type": grouped, "totals_by_root_type": totals_by_root, "row_count": len(rows)}, (
		totals_by_root
	)


def _income_statement(totals: dict) -> dict:
	income = totals.get("Income", {}).get("balance_natural", 0.0)
	expense = totals.get("Expense", {}).get("balance_natural", 0.0)
	return {
		"basis": "movement within the fiscal year",
		"revenue": money(income),
		"expenses": money(expense),
		"net_income": money(income - expense),
	}


def _balance_sheet(totals: dict) -> dict:
	assets = totals.get("Asset", {}).get("balance_natural", 0.0)
	liabilities = totals.get("Liability", {}).get("balance_natural", 0.0)
	equity = totals.get("Equity", {}).get("balance_natural", 0.0)
	return {
		"basis": "cumulative to year end",
		"assets": money(assets),
		"liabilities": money(liabilities),
		"equity": money(equity),
		"liabilities_plus_equity": money(liabilities + equity),
	}


def _top_entries(company: str, start: str, end: str) -> list:
	rows = frappe.db.get_all(
		"Journal Entry",
		filters={
			"company": company,
			"posting_date": ("between", [start, end]),
			"docstatus": 1,
		},
		fields=compat.existing_fields(
			"Journal Entry",
			["name", "posting_date", "total_debit", "total_credit", "user_remark", "voucher_type", "owner"],
		),
		order_by="total_debit desc",
		limit=TOP_ENTRIES,
	)
	out = []
	for row in rows:
		amount = max(float(row.get("total_debit") or 0), float(row.get("total_credit") or 0))
		out.append({**row, "amount": money(amount)})
	out.sort(key=lambda row: row["amount"], reverse=True)
	return out[:TOP_ENTRIES]


def _intercompany(company: str, start: str, end: str, flags: list) -> list:
	"""Journal Entries whose account lines do not all belong to one company."""
	names = frappe.db.get_all(
		"Journal Entry",
		filters={"company": company, "posting_date": ("between", [start, end]), "docstatus": 1},
		pluck="name",
	)
	if not names:
		return []
	lines = frappe.db.get_all(
		"Journal Entry Account",
		filters={"parent": ("in", sorted(names)), "parenttype": "Journal Entry"},
		fields=["parent", "account", "debit", "credit"],
	)
	account_company = {
		row["name"]: row["company"]
		for row in frappe.db.get_all(
			"Account",
			filters={"name": ("in", sorted({line["account"] for line in lines if line.get("account")}))},
			fields=["name", "company"],
		)
	}
	spans: dict = {}
	for line in lines:
		owner = account_company.get(line.get("account"))
		if not owner:
			continue
		spans.setdefault(line["parent"], set()).add(owner)

	out = [
		{
			"journal_entry": name,
			"companies": sorted(companies),
			"declared_company": company,
		}
		for name, companies in sorted(spans.items())
		if len(companies) > 1 or company not in companies
	]
	if out:
		flags.append(
			Flag(
				code="INTERCOMPANY_ACTIVITY",
				severity=SEVERITY_WARN,
				description=(
					f"{len(out)} submitted Journal Entry/Entries have account lines "
					"belonging to more than one company, or to a company other than "
					"the one on the entry. That is invisible until consolidation."
				),
				detail={"count": len(out), "entries": [row["journal_entry"] for row in out[:20]]},
			)
		)
	return cap(out, flags, "intercompany_activity")


def _document_counts(company: str, start: str, end: str) -> dict:
	def count(doctype: str, extra: dict, date_field: str = "posting_date") -> int | None:
		if not compat.doctype_exists(doctype):
			return None
		filters = {"company": company, date_field: ("between", [start, end]), **extra}
		filters = {k: v for k, v in filters.items() if compat.has_field(doctype, k) or k == date_field}
		try:
			return frappe.db.count(doctype, filters)
		except Exception:
			return None

	return {
		"sales_invoices": count("Sales Invoice", {"docstatus": 1}),
		"purchase_invoices": count("Purchase Invoice", {"docstatus": 1}),
		"journal_entries_submitted": count("Journal Entry", {"docstatus": 1}),
		# v0.334.0. A discarded draft is set aside, not waiting to be posted; counted apart.
		"journal_entries_draft": count("Journal Entry", {"docstatus": 0, "discarded": 0}),
		"journal_entries_discarded": (
			count("Journal Entry", {"docstatus": 0, "discarded": 1}) if _discard_installed() else 0
		),
		"journal_entries_cancelled": count("Journal Entry", {"docstatus": 2}),
		"bank_transactions": count("Bank Transaction", {}, date_field="date"),
		"note": "null means the DocType is not installed on this site.",
	}


def _discard_installed() -> bool:
	from ..tools import discard

	return discard.installed()


# ── anomaly detection ───────────────────────────────────────────────────────
def _check_trial_balance(totals: dict, flags: list) -> None:
	if abs(totals["difference"]) <= TOLERANCE:
		return
	flags.append(
		Flag(
			code="TRIAL_BALANCE_IMBALANCE",
			severity=SEVERITY_ERROR,
			description=(
				f"Cumulative debits ({totals['debit']}) do not equal cumulative "
				f"credits ({totals['credit']}) — a difference of "
				f"{totals['difference']} across {totals['gl_entry_count']} GL rows. "
				"Double entry has been violated somewhere in this company's ledger."
			),
			detail=totals,
		)
	)


def _check_accounting_identity(data: dict, income: dict, balance: dict, flags: list) -> None:
	"""Assets minus liabilities and equity should equal net income for an open year."""
	left = money(balance["assets"] - balance["liabilities_plus_equity"])
	right = income["net_income"]
	difference = money(left - right)
	data["accounting_identity"] = {
		"assets_less_liabilities_and_equity": left,
		"net_income": right,
		"difference": difference,
		"holds": abs(difference) <= TOLERANCE,
		"note": (
			"For a year that has not been closed to retained earnings, these two "
			"are the same number reached two ways."
		),
	}
	if abs(difference) > TOLERANCE:
		flags.append(
			Flag(
				code="ACCOUNTING_IDENTITY_FAILS",
				severity=SEVERITY_ERROR,
				description=(
					f"Assets minus liabilities and equity is {left}, but net income "
					f"for the year is {right} — a difference of {difference}. For an "
					"unclosed year these are the same figure computed two ways. Either "
					"the year has been closed to retained earnings (in which case "
					"expect this), or the books do not balance."
				),
				detail=data["accounting_identity"],
			)
		)


def _check_documents(counts: dict, flags: list) -> None:
	cancelled = counts.get("journal_entries_cancelled") or 0
	if cancelled:
		flags.append(
			Flag(
				code="CANCELLED_ENTRIES",
				severity=SEVERITY_WARN,
				description=(
					f"{cancelled} Journal Entry/Entries were cancelled during the "
					"fiscal year. Each one was posted and then unposted; an auditor "
					"will want the reason for every one."
				),
				detail={"count": cancelled},
			)
		)
	drafts = counts.get("journal_entries_draft") or 0
	if drafts:
		flags.append(
			Flag(
				code="DRAFT_ENTRIES_AT_YEAR_END",
				severity=SEVERITY_WARN,
				description=(
					f"{drafts} Journal Entry/Entries dated inside the fiscal year are "
					"still drafts. They are in no figure in this packet, and "
					"submitting one after sign-off changes the year."
				),
				detail={"count": drafts},
			)
		)
	if not counts.get("journal_entries_submitted"):
		flags.append(
			Flag(
				code="NO_ACTIVITY",
				severity=SEVERITY_INFO,
				description="No submitted Journal Entries in this fiscal year for this company.",
				detail={},
			)
		)


def _check_unnatural_balances(trial_balance: dict, flags: list) -> None:
	"""Accounts sitting the wrong way round — a bank account in credit, say."""
	offenders = [
		{
			"account": row["account"],
			"root_type": row["root_type"],
			"balance_natural": row["balance_natural"],
		}
		for rows in trial_balance["by_root_type"].values()
		for row in rows
		if not row["is_group"] and row["balance_natural"] < -TOLERANCE
	]
	if not offenders:
		return
	flags.append(
		Flag(
			code="UNNATURAL_BALANCE",
			severity=SEVERITY_WARN,
			description=(
				f"{len(offenders)} account(s) close the year with a balance opposite "
				"to their root type — an asset in credit or a liability in debit. "
				"Sometimes legitimate (an overdrawn bank account, a supplier "
				"prepayment), always worth a sentence in the notes."
			),
			detail={"accounts": offenders[:20], "count": len(offenders)},
		)
	)


def _check_year_shape(year: dict, company: str, flags: list) -> None:
	if year.get("disabled"):
		flags.append(
			Flag(
				code="FISCAL_YEAR_DISABLED",
				severity=SEVERITY_INFO,
				description=f"Fiscal Year {year['name']} is marked disabled on this site.",
				detail={"fiscal_year": year["name"]},
			)
		)
	links = frappe.db.get_all(
		"Fiscal Year Company", filters={"parent": year["name"], "parenttype": "Fiscal Year"}, pluck="company"
	)
	if links and company not in links:
		flags.append(
			Flag(
				code="FISCAL_YEAR_NOT_LINKED",
				severity=SEVERITY_WARN,
				description=(
					f"Fiscal Year {year['name']} is restricted to {', '.join(sorted(links))} "
					f"and does not include {company}. The date range was still applied, "
					"but this may not be that company's fiscal year."
				),
				detail={"fiscal_year": year["name"], "companies": sorted(links)},
			)
		)


register(
	PacketSpec(
		packet_type="fiscal_year_audit_packet",
		title="Fiscal year audit packet",
		purpose=(
			"What an outside CPA asks for to review a year: a trial balance stating "
			"its own basis per account, an income statement and balance sheet, the "
			"twenty largest entries for materiality, any intercompany activity, "
			"document counts, and a check that the accounting identity holds."
		),
		audience="An external accountant or auditor reviewing a completed year.",
		build=build,
		filters={
			"company": {"type": "string", "description": "Company name or abbreviation."},
			"fiscal_year": {
				"type": "string",
				"description": "Fiscal Year docname, e.g. '2026'. See list_fiscal_years.",
			},
		},
		required=("company", "fiscal_year"),
		available=lambda: compat.doctype_exists("Fiscal Year"),
		requires="the Fiscal Year DocType (ERPNext)",
	)
)
