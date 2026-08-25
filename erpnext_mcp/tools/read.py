# SPDX-License-Identifier: MIT
"""The read-only tools. None of these writes anything, ever.

Two conventions run through all ten:

DISCOVER, DON'T ASSUME. Company names, account numbers, fiscal-year labels and
the Bank Transaction schema are all read off the site at call time. There is no
constant in this file that names a real-world company, account or ledger.

EXPLAIN THE SIGN. Accounting sign conventions are where an AI reading a ledger
most reliably goes wrong: a Liability with a $5,000 credit balance is not
"-5000 of something". So every balance is returned twice — `balance` in raw
ledger convention (debit minus credit) and `balance_natural` flipped per the
account's `root_type` so a liability, income or equity balance reads positive
when it is what an accountant would call normal — with `sign_convention` in the
payload naming which is which.
"""

import frappe

from .. import compat
from ..args import (
	MAX_LIMIT,
	as_date,
	as_docstatus,
	as_int,
	as_limit,
	as_str,
	resolve_account,
	resolve_company,
)
from ..errors import ToolError
from ..result import ToolResult

#: Root types whose natural balance is a credit. Everything else (Asset,
#: Expense) is naturally a debit.
_CREDIT_ROOTS = ("Liability", "Income", "Equity")


# ── 1. get_company_topology ─────────────────────────────────────────────────
def get_company_topology(args: dict) -> ToolResult:
	"""The shape of this ERPNext install, in one call.

	Deliberately the first tool in the catalogue: it is what an MCP client should
	call before anything else, because every other tool takes a company, an
	account or a fiscal year that only exists on this particular site.
	"""
	fields = compat.existing_fields(
		"Company",
		[
			"name",
			"abbr",
			"default_currency",
			"country",
			"chart_of_accounts",
			"parent_company",
			"is_group",
			"tax_id",
		],
	)
	companies = frappe.db.get_all("Company", fields=fields, order_by="name asc")
	fiscal_years = _fiscal_years_by_company()

	out = []
	for company in companies:
		name = company["name"]
		roots = frappe.db.get_all(
			"Account",
			filters={"company": name, "parent_account": ("in", ("", None))},
			fields=["name", "account_name", "root_type", "is_group"],
			order_by="root_type asc, name asc",
		)
		out.append(
			{
				**company,
				"default_cost_center": compat.company_default_cost_center(name),
				"fiscal_years": fiscal_years.get(name, []) + fiscal_years["__all__"],
				"root_accounts": roots,
				"root_types": sorted({r["root_type"] for r in roots if r["root_type"]}),
				"account_count": frappe.db.count("Account", {"company": name}),
			}
		)

	data = {
		"companies": out,
		"count": len(out),
		"site": frappe.local.site,
		"optional_doctypes": {
			doctype: compat.doctype_exists(doctype)
			for doctype in ("Bank Transaction", "Bank Statement", "Bank Account", "Bank")
		},
	}
	return ToolResult(data, f"{len(out)} company/companies")


def _fiscal_years_by_company() -> dict:
	"""Fiscal years grouped by the company they are restricted to.

	A Fiscal Year with no rows in its `companies` child table applies to every
	company — that is how ERPNext models "global fiscal year" — so those go into
	the `__all__` bucket and get merged into each company's list.
	"""
	years = frappe.db.get_all(
		"Fiscal Year",
		fields=compat.existing_fields(
			"Fiscal Year", ["name", "year_start_date", "year_end_date", "disabled"]
		),
		order_by="year_start_date desc",
	)
	links = frappe.db.get_all(
		"Fiscal Year Company",
		filters={"parenttype": "Fiscal Year"},
		fields=["parent", "company"],
	)
	by_year = {}
	for link in links:
		by_year.setdefault(link["parent"], []).append(link["company"])

	grouped = {"__all__": []}
	for year in years:
		companies = by_year.get(year["name"], [])
		if not companies:
			grouped["__all__"].append(year)
			continue
		for company in companies:
			grouped.setdefault(company, []).append(year)
	return grouped


# ── 2. get_account_balance ──────────────────────────────────────────────────
def get_account_balance(args: dict) -> ToolResult:
	"""Balance of one account from GL Entry, as of a date.

	Sums the ledger rather than reading a cached figure, so the answer matches
	what ERPNext's own General Ledger report would print — including the
	`is_cancelled` exclusion, which is the single easiest way to compute a wrong
	balance on a site that has ever cancelled a voucher.
	"""
	company = resolve_company(as_str(args, "company"))
	account = resolve_account(as_str(args, "account", required=True), company or "")
	as_of = as_date(args, "as_of") or frappe.utils.today()

	meta = frappe.db.get_value(
		"Account",
		account,
		[
			"account_name",
			"account_number",
			"root_type",
			"account_type",
			"company",
			"account_currency",
			"is_group",
			"freeze_account",
		],
		as_dict=True,
	)

	filters = {"account": account, "posting_date": ("<=", as_of)}
	if compat.has_field("GL Entry", "is_cancelled"):
		filters["is_cancelled"] = 0
	totals = frappe.db.get_all(
		"GL Entry",
		filters=filters,
		fields=["sum(debit) as debit", "sum(credit) as credit", "count(name) as entries"],
	)
	row = (totals or [{}])[0] or {}
	debit = float(row.get("debit") or 0)
	credit = float(row.get("credit") or 0)
	balance = round(debit - credit, 2)
	natural = -balance if (meta or {}).get("root_type") in _CREDIT_ROOTS else balance

	data = {
		"account": account,
		"account_name": (meta or {}).get("account_name"),
		"account_number": (meta or {}).get("account_number"),
		"company": (meta or {}).get("company"),
		"currency": (meta or {}).get("account_currency"),
		"root_type": (meta or {}).get("root_type"),
		"account_type": (meta or {}).get("account_type"),
		"is_group": bool((meta or {}).get("is_group")),
		"as_of": as_of,
		"total_debit": round(debit, 2),
		"total_credit": round(credit, 2),
		"gl_entry_count": int(row.get("entries") or 0),
		"balance": balance,
		"balance_natural": round(natural, 2),
		"sign_convention": (
			"balance = debit - credit (raw ledger). balance_natural flips the sign "
			"for Liability/Income/Equity so a normal balance reads positive."
		),
	}
	if data["is_group"]:
		data["note"] = (
			"This is a group account. GL Entries post to leaf accounts, so this "
			"balance covers only entries booked directly against the group — use "
			"get_chart_of_accounts to walk its children."
		)
	return ToolResult(data, f"{account} balance {balance} as of {as_of} ({data['gl_entry_count']} GL rows)")


# ── 3. get_journal_entries ──────────────────────────────────────────────────
def get_journal_entries(args: dict) -> ToolResult:
	"""Journal Entry headers in a date range, newest first.

	Headers only — `get_journal_entry` returns the account lines for one. That
	split is on purpose: a month of JEs with every line expanded is a lot of
	tokens for a question that is usually "which one was it".
	"""
	from_date = as_date(args, "from_date", required=True)
	to_date = as_date(args, "to_date", required=True)
	if from_date > to_date:
		raise ToolError(f"from_date {from_date} is after to_date {to_date}")
	company = resolve_company(as_str(args, "company"))
	account = as_str(args, "account")
	docstatus = as_docstatus(args)
	limit = as_limit(args)

	filters = {"posting_date": ("between", [from_date, to_date])}
	if company:
		filters["company"] = company
	if docstatus is not None:
		filters["docstatus"] = docstatus
	if account:
		resolved = resolve_account(account, company or "")
		names = frappe.db.get_all(
			"Journal Entry Account",
			filters={"account": resolved, "parenttype": "Journal Entry"},
			pluck="parent",
			# One JE can carry the same account on several lines; dedupe below.
			limit=MAX_LIMIT * 10,
		)
		unique = sorted(set(names))
		if not unique:
			return ToolResult(
				{
					"journal_entries": [],
					"count": 0,
					"filters": {"account": resolved, "from_date": from_date, "to_date": to_date},
				},
				f"no Journal Entry touches {resolved}",
			)
		filters["name"] = ("in", unique)

	fields = compat.existing_fields(
		"Journal Entry",
		[
			"name",
			"posting_date",
			"company",
			"voucher_type",
			"total_debit",
			"total_credit",
			"user_remark",
			"cheque_no",
			"cheque_date",
			"bill_no",
			"docstatus",
			"owner",
			"creation",
		],
	)
	rows = frappe.db.get_all(
		"Journal Entry",
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit=limit,
	)
	for row in rows:
		row["docstatus_label"] = _docstatus_label(row.get("docstatus"))

	data = {
		"journal_entries": rows,
		"count": len(rows),
		"limit": limit,
		"truncated": len(rows) == limit,
		"filters": {
			"from_date": from_date,
			"to_date": to_date,
			"company": company,
			"account": account or None,
			"docstatus": docstatus,
		},
	}
	return ToolResult(data, f"{len(rows)} Journal Entry row(s) {from_date}..{to_date}")


# ── 4. get_journal_entry ────────────────────────────────────────────────────
def get_journal_entry(args: dict) -> ToolResult:
	"""One Journal Entry with every account line, party and reference."""
	name = as_str(args, "name", required=True)
	if not frappe.db.exists("Journal Entry", name):
		raise ToolError(f"no Journal Entry named {name!r}")
	doc = frappe.get_doc("Journal Entry", name)

	header_fields = compat.existing_fields(
		"Journal Entry",
		[
			"name",
			"posting_date",
			"company",
			"voucher_type",
			"naming_series",
			"total_debit",
			"total_credit",
			"difference",
			"user_remark",
			"remark",
			"cheque_no",
			"cheque_date",
			"bill_no",
			"bill_date",
			"finance_book",
			"docstatus",
			"owner",
			"creation",
			"modified",
			"modified_by",
			"is_opening",
			"clearance_date",
			"mode_of_payment",
			"multi_currency",
		],
	)
	line_fields = compat.existing_fields(
		"Journal Entry Account",
		[
			"idx",
			"account",
			"account_type",
			"party_type",
			"party",
			"debit",
			"credit",
			"debit_in_account_currency",
			"credit_in_account_currency",
			"account_currency",
			"exchange_rate",
			"against_account",
			"cost_center",
			"project",
			"reference_type",
			"reference_name",
			"reference_due_date",
			"user_remark",
			"is_advance",
		],
	)

	data = {field: doc.get(field) for field in header_fields}
	data["docstatus_label"] = _docstatus_label(doc.get("docstatus"))
	data["accounts"] = [
		{field: line.get(field) for field in line_fields} for line in (doc.get("accounts") or [])
	]
	data["balanced"] = abs(float(doc.get("total_debit") or 0) - float(doc.get("total_credit") or 0)) < 0.005
	return ToolResult(
		data,
		f"{name}: {len(data['accounts'])} line(s), "
		f"debit {doc.get('total_debit')} / credit {doc.get('total_credit')}, "
		f"{data['docstatus_label']}",
	)


# ── 133. investigate_je_gl_link ─────────────────────────────────────────────
def investigate_je_gl_link(args: dict) -> ToolResult:
	"""Every line of a Journal Entry beside every GL Entry row it posted.

	WHAT THIS ANSWERS. "The voucher says X and the ageing report says Y — which
	row is which, and why did the tool that was supposed to keep them in step
	match nothing?" Before this, answering that meant raw SQL against `tabGL
	Entry` on somebody's production box. Now it is one call.

	IT EXISTS BECAUSE OF A REAL ZERO. Sprint 6 verification on 2026-07-30 ran
	`update_journal_entry_party` against ACC-JV-2026-00073, a $10 member
	distribution, and got `gl_entries_matched: 0`. Three explanations were on the
	table: an Equity-account quirk, a Bank Bridge JE-crafting bug, or ordinary
	ERPNext behaviour. It was the third, and none of the guesses would have been
	settled without seeing both tables side by side.

	THE ANSWER, FOR ANYONE WHO NEVER HAS TO RUN THIS. ERPNext does NOT put the
	account line's docname in `GL Entry.voucher_detail_no` for a Journal Entry. It
	fills that column from the line's `reference_detail_no`, which names a payment
	schedule row on an invoice being settled and is empty on every ordinary line.
	`voucher_detail_no` carrying a child-row docname is the Sales Invoice Item /
	Purchase Invoice Item convention, not the Journal Entry one. So a lookup keyed
	on it matches nothing — on Equity, on Expense, on Payable, on anything. That
	is not a defect in the site and it is not specific to any account type.

	`voucher_detail_no_populated` in the result is the field's actual state on
	this voucher, so the explanation above can be checked rather than believed.

	Read-only. It changes nothing, and it is deliberately available on a
	cancelled entry too — a voucher whose rows were reversed is exactly the one
	somebody is trying to understand.
	"""
	# Imported here rather than at module scope purely for locality: the matcher
	# belongs beside the tool that writes through it, and this is the tool that
	# explains what it did.
	from . import mutate

	name = as_str(args, "journal_entry", required=True)
	if not frappe.db.exists("Journal Entry", name):
		raise ToolError(f"no Journal Entry named {name!r}")
	doc = frappe.get_doc("Journal Entry", name)
	lines = list(doc.get("accounts") or [])
	gl_rows = mutate.voucher_gl_rows(name)

	account_facts = _account_facts(str(row.get("account") or "") for row in lines)
	claimed: set = set()
	reports, disagreements = [], []
	for index, line in enumerate(lines, start=1):
		link = mutate.gl_link_for_line(name, lines, index, gl_rows=gl_rows)
		facts = account_facts.get(str(line.get("account") or ""), {})
		matched = []
		for row in link["rows"]:
			claimed.add(row.get("name"))
			disagrees = str(row.get("party_type") or "") != str(line.get("party_type") or "") or str(
				row.get("party") or ""
			) != str(line.get("party") or "")
			if disagrees:
				disagreements.append(index)
			matched.append(
				{
					"gl_entry": row.get("name"),
					"account": row.get("account"),
					"debit": round(float(row.get("debit") or 0), 2),
					"credit": round(float(row.get("credit") or 0), 2),
					"party_type": row.get("party_type") or None,
					"party": row.get("party") or None,
					"voucher_type": row.get("voucher_type"),
					"voucher_no": row.get("voucher_no"),
					"voucher_detail_no": row.get("voucher_detail_no") or None,
					"posting_date": str(row.get("posting_date") or ""),
					"cost_center": row.get("cost_center"),
					"party_disagrees_with_line": disagrees,
				}
			)
		reports.append(
			{
				"line_index": index,
				"line_name": line.get("name"),
				"account": line.get("account"),
				"account_type": facts.get("account_type") or None,
				"root_type": facts.get("root_type") or None,
				"debit": round(float(line.get("debit") or 0), 2),
				"credit": round(float(line.get("credit") or 0), 2),
				"party_type": line.get("party_type") or None,
				"party": line.get("party") or None,
				"reference_detail_no": line.get("reference_detail_no") or None,
				"gl_entries": matched,
				"gl_entries_matched": len(matched),
				"matched_by": link["basis"],
				"match_is_exact": bool(link["exact"]),
				"blocker": link["blocker"] or None,
			}
		)

	orphans = [
		{
			"gl_entry": row.get("name"),
			"account": row.get("account"),
			"debit": round(float(row.get("debit") or 0), 2),
			"credit": round(float(row.get("credit") or 0), 2),
			"party_type": row.get("party_type") or None,
			"party": row.get("party") or None,
			"voucher_detail_no": row.get("voucher_detail_no") or None,
		}
		for row in gl_rows
		if row.get("name") not in claimed
	]
	with_detail_no = [row for row in gl_rows if str(row.get("voucher_detail_no") or "")]
	unmatched_lines = [report["line_index"] for report in reports if not report["gl_entries_matched"]]

	docstatus = int(doc.get("docstatus") or 0)
	data = {
		"journal_entry": name,
		"company": doc.get("company"),
		"posting_date": str(doc.get("posting_date") or ""),
		"voucher_type": doc.get("voucher_type"),
		"docstatus": docstatus,
		"docstatus_label": _docstatus_label(docstatus),
		"lines": reports,
		"unmatched_gl_entries": orphans,
		"summary": {
			"journal_entry_lines": len(lines),
			"gl_entry_rows": len(gl_rows),
			"matched_pairs": len(claimed),
			"unmatched_journal_entry_lines": unmatched_lines,
			"unmatched_gl_entry_rows": len(orphans),
			"lines_whose_party_disagrees_with_the_ledger": sorted(set(disagreements)),
			"voucher_detail_no_populated": len(with_detail_no),
		},
		"finding": _je_gl_finding(docstatus, lines, gl_rows, with_detail_no, unmatched_lines, orphans),
		"sign_convention": (
			"debit and credit are shown as ERPNext stores them, in company currency. "
			"Nothing here is sign-flipped."
		),
	}
	return ToolResult(
		data,
		f"{name}: {len(lines)} line(s), {len(gl_rows)} live GL row(s), {len(claimed)} matched"
		+ (f", lines {unmatched_lines} unmatched" if unmatched_lines else "")
		+ (f", {len(orphans)} GL row(s) unexplained" if orphans else ""),
	)


def _account_facts(accounts) -> dict:
	"""`account_type` and `root_type` for a set of accounts, in one query."""
	names = sorted({name for name in accounts if name})
	if not names:
		return {}
	fields = compat.existing_fields("Account", ("name", "account_type", "root_type"))
	rows = frappe.db.get_all("Account", filters={"name": ("in", names)}, fields=fields, limit=500)
	return {str(row["name"]): dict(row) for row in rows or []}


def _je_gl_finding(docstatus, lines, gl_rows, with_detail_no, unmatched_lines, orphans) -> str:
	"""One paragraph saying what the numbers above mean, in the reader's terms."""
	if docstatus == 0:
		return (
			"This entry is a DRAFT. A draft posts no GL Entry rows at all, so there is nothing "
			"to match and nothing wrong. It becomes a general ledger question on submit."
		)
	if docstatus == 2:
		return (
			"This entry is CANCELLED. Its live rows are excluded here and only the reversal "
			"remains in the ledger; a cancelled voucher is the record of a posting that was "
			"undone, and its attribution is not something to correct."
		)
	if not gl_rows:
		return (
			"This entry is submitted and has NO live GL Entry rows. That is unusual: a submitted "
			"Journal Entry normally posts one row per line. Either something removed them or the "
			"entry was written by a path that does not post a general ledger."
		)
	detail = (
		f"{len(with_detail_no)} of the {len(gl_rows)} GL row(s) carry a voucher_detail_no"
		if with_detail_no
		else "NOT ONE of the GL rows carries a voucher_detail_no"
	)
	base = (
		f"{detail}. This is ordinary ERPNext behaviour, not a defect and not an account-type "
		"quirk: `JournalEntry.get_gl_entries` fills that column from the line's "
		"`reference_detail_no`, which names a payment schedule row on an invoice being settled "
		"and is empty on every ordinary line. Carrying the child-row docname is the Sales "
		"Invoice Item convention, not the Journal Entry one. Lines are therefore matched to GL "
		"rows on account plus debit plus credit, and that match is refused rather than guessed "
		"wherever two lines of this voucher look alike."
	)
	if unmatched_lines:
		base += (
			f" Line(s) {unmatched_lines} could not be matched to a row at all — read the `blocker` "
			"on each. ERPNext merges lines that share an account, a party and a cost center into "
			"one summed GL row, which is the commonest reason for it."
		)
	if orphans:
		base += (
			f" {len(orphans)} GL row(s) are not explained by any single line, which is what a "
			"merge looks like from the other side."
		)
	return base


# ── 5. list_bank_transactions ───────────────────────────────────────────────
def list_bank_transactions(args: dict) -> ToolResult:
	"""Bank Transactions, filtered the way a reconciliation actually asks.

	Amounts are normalised to one signed `amount` (positive in, negative out)
	whichever way this ERPNext version stores them — see `compat`.
	"""
	compat.require_doctype("Bank Transaction", "It ships with ERPNext's Accounts module.")
	bank_account = as_str(args, "bank_account")
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	status = as_str(args, "status")
	limit = as_limit(args)

	money = compat.bank_transaction_amount_fields()
	filters = {}
	if bank_account:
		filters["bank_account"] = _resolve_bank_account(bank_account)
	if from_date and to_date:
		filters["date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["date"] = (">=", from_date)
	elif to_date:
		filters["date"] = ("<=", to_date)
	if status:
		filters["status"] = status

	fields = compat.existing_fields(
		"Bank Transaction",
		[
			"name",
			"date",
			"bank_account",
			"company",
			"description",
			"status",
			"reference_number",
			"currency",
			"party_type",
			"party",
			"bank_party_name",
			"docstatus",
			"deposit",
			"withdrawal",
			"amount",
			"allocated_amount",
			"unallocated_amount",
		],
	)
	rows = frappe.db.get_all(
		"Bank Transaction",
		filters=filters,
		fields=fields,
		order_by="date desc, creation desc",
		limit=limit,
	)
	for row in rows:
		row["amount_signed"] = round(compat.signed_amount(row, money), 2)

	data = {
		"bank_transactions": rows,
		"count": len(rows),
		"limit": limit,
		"truncated": len(rows) == limit,
		"amount_layout": money["style"],
		"sign_convention": "amount_signed is positive for money in, negative for money out.",
		"filters": {
			"bank_account": filters.get("bank_account"),
			"from_date": from_date,
			"to_date": to_date,
			"status": status or None,
		},
	}
	return ToolResult(data, f"{len(rows)} Bank Transaction row(s)")


# ── 6. get_bank_statement ───────────────────────────────────────────────────
def get_bank_statement(args: dict) -> ToolResult:
	"""One Bank Statement, on the versions of ERPNext that have the doctype.

	Bank Statement arrived later than Bank Transaction, so this is the one tool
	that can legitimately be unavailable on a supported site. It says so in
	words rather than raising a schema error, and `get_company_topology`
	reports the doctype's presence up front so a client need not find out here.
	"""
	compat.require_doctype(
		"Bank Statement",
		"It is only present on ERPNext versions that ship the Bank Statement "
		"doctype; get_company_topology reports whether this site has it.",
	)
	name = as_str(args, "name", required=True)
	if not frappe.db.exists("Bank Statement", name):
		raise ToolError(f"no Bank Statement named {name!r}")

	doc = frappe.get_doc("Bank Statement", name)
	# The field set has changed across versions, so mirror whatever is there
	# rather than naming columns — minus the framework's own bookkeeping.
	skip = {
		"doctype",
		"parent",
		"parentfield",
		"parenttype",
		"idx",
		"_user_tags",
		"_comments",
		"_assign",
		"_liked_by",
	}
	data = {
		key: value
		for key, value in doc.as_dict(no_nulls=False).items()
		if key not in skip and not isinstance(value, list)
	}
	data["child_tables"] = {
		key: [row.as_dict() for row in value]
		for key, value in doc.as_dict().items()
		if isinstance(value, list) and value
	}
	return ToolResult(data, f"Bank Statement {name}")


# ── 7. list_fiscal_years ────────────────────────────────────────────────────
def list_fiscal_years(args: dict) -> ToolResult:
	"""Every Fiscal Year and the companies it applies to.

	Needed because ERPNext will refuse a posting_date that falls outside a
	fiscal year, which is otherwise a confusing failure for a client picking
	dates on its own.
	"""
	company = resolve_company(as_str(args, "company"))
	grouped = _fiscal_years_by_company()
	if company:
		years = grouped.get(company, []) + grouped["__all__"]
		scope = f"company {company}"
	else:
		years = [year for key, value in grouped.items() if key != "__all__" for year in value]
		years += grouped["__all__"]
		scope = "all companies"

	seen, unique = set(), []
	for year in sorted(years, key=lambda y: str(y.get("year_start_date") or ""), reverse=True):
		if year["name"] in seen:
			continue
		seen.add(year["name"])
		unique.append(year)

	data = {
		"fiscal_years": unique,
		"count": len(unique),
		"company": company,
		"company_agnostic_years": [y["name"] for y in grouped["__all__"]],
		"note": (
			"A Fiscal Year with no company links applies to every company; those "
			"are listed in company_agnostic_years."
		),
	}
	return ToolResult(data, f"{len(unique)} fiscal year(s) for {scope}")


# ── 8. get_chart_of_accounts ────────────────────────────────────────────────
def get_chart_of_accounts(args: dict) -> ToolResult:
	"""The company's chart of accounts as a nested tree.

	Built in one query and assembled in Python: walking `parent_account` with a
	query per node is the obvious implementation and is also how you make a
	2,000-account chart take thirty seconds.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	root_type = as_str(args, "root_type")

	filters = {"company": company}
	if root_type:
		valid = ("Asset", "Liability", "Income", "Expense", "Equity")
		if root_type not in valid:
			raise ToolError(f"root_type must be one of {', '.join(valid)}, got {root_type!r}")
		filters["root_type"] = root_type

	fields = compat.existing_fields(
		"Account",
		[
			"name",
			"account_name",
			"account_number",
			"parent_account",
			"is_group",
			"root_type",
			"account_type",
			"account_currency",
			"disabled",
			"freeze_account",
			"lft",
			"rgt",
		],
	)
	# `lft` is the nested-set left bound: ordering by it yields parents before
	# children, so the tree assembles in one pass. Sites where the nested set
	# is absent fall back to name order, which still assembles correctly
	# because every node is created before it is linked.
	order_by = "lft asc" if compat.has_field("Account", "lft") else "name asc"
	accounts = frappe.db.get_all("Account", filters=filters, fields=fields, order_by=order_by)

	nodes = {row["name"]: {**row, "children": []} for row in accounts}
	roots = []
	for row in accounts:
		parent = row.get("parent_account")
		if parent and parent in nodes:
			nodes[parent]["children"].append(nodes[row["name"]])
		else:
			# Either a real root, or a node whose parent was filtered out by
			# root_type — both belong at the top of *this* response.
			roots.append(nodes[row["name"]])

	data = {
		"company": company,
		"root_type": root_type or None,
		"accounts": roots,
		"flat_count": len(accounts),
		"note": "children[] is nested; flat_count is every account in the response.",
	}
	return ToolResult(data, f"{len(accounts)} account(s) for {company}")


# ── 9. list_unreconciled_bank_transactions ──────────────────────────────────
def list_unreconciled_bank_transactions(args: dict) -> ToolResult:
	"""Bank Transactions with money still unallocated — the reconciliation worklist.

	Prefers this site's `unallocated_amount` when it has one, and otherwise
	computes `gross - allocated` itself, so the answer is the same on either
	schema.
	"""
	compat.require_doctype("Bank Transaction", "It ships with ERPNext's Accounts module.")
	bank_account = _resolve_bank_account(as_str(args, "bank_account", required=True))
	limit = as_limit(args)
	money = compat.bank_transaction_amount_fields()

	filters = {"bank_account": bank_account, "docstatus": ("<", 2)}
	if money["unallocated"]:
		filters[money["unallocated"]] = (">", 0)

	fields = compat.existing_fields(
		"Bank Transaction",
		[
			"name",
			"date",
			"bank_account",
			"company",
			"description",
			"status",
			"reference_number",
			"currency",
			"party_type",
			"party",
			"docstatus",
			"deposit",
			"withdrawal",
			"amount",
			"allocated_amount",
			"unallocated_amount",
		],
	)
	rows = frappe.db.get_all(
		"Bank Transaction",
		filters=filters,
		fields=fields,
		order_by="date asc",
		# Without an unallocated_amount column the filter cannot be pushed into
		# SQL, so over-fetch and cut in Python.
		limit=limit if money["unallocated"] else MAX_LIMIT * 4,
	)

	out = []
	for row in rows:
		gross = round(compat.gross_amount(row, money), 2)
		allocated = round(float(row.get(money["allocated"]) or 0), 2) if money["allocated"] else 0.0
		unallocated = (
			round(float(row.get(money["unallocated"]) or 0), 2)
			if money["unallocated"]
			else round(gross - allocated, 2)
		)
		if unallocated <= 0:
			continue
		out.append(
			{
				**row,
				"amount_signed": round(compat.signed_amount(row, money), 2),
				"gross_amount": gross,
				"allocated_amount_effective": allocated,
				"unallocated_amount_effective": unallocated,
			}
		)
		if len(out) >= limit:
			break

	data = {
		"unreconciled": out,
		"count": len(out),
		"bank_account": bank_account,
		"limit": limit,
		"truncated": len(out) == limit,
		"amount_layout": money["style"],
		"unallocated_source": "column" if money["unallocated"] else "computed (gross - allocated)",
	}
	return ToolResult(data, f"{bank_account}: {len(out)} unreconciled transaction(s)")


# ── 10. search_accounts ─────────────────────────────────────────────────────
def search_accounts(args: dict) -> ToolResult:
	"""Fuzzy account lookup — the tool that turns "Cash Clearing" into a docname.

	Exists so a client never has to guess ERPNext's `"<name> - <abbr>"` primary
	key. Results are ranked (exact number, exact name, prefix, then substring)
	because the top hit being right is what saves the follow-up call.
	"""
	query = as_str(args, "query", required=True)
	company = resolve_company(as_str(args, "company"))
	limit = as_limit(args)

	base = {"company": company} if company else {}
	fields = compat.existing_fields(
		"Account",
		[
			"name",
			"account_name",
			"account_number",
			"company",
			"root_type",
			"account_type",
			"is_group",
			"disabled",
			"parent_account",
			"account_currency",
		],
	)
	pattern = f"%{query}%"
	found = {}
	for filters in (
		{**base, "account_number": query},
		{**base, "account_name": query},
		{**base, "account_number": ("like", pattern)},
		{**base, "account_name": ("like", pattern)},
		{**base, "name": ("like", pattern)},
	):
		for row in frappe.db.get_all("Account", filters=filters, fields=fields, limit=limit * 4):
			found.setdefault(row["name"], row)

	needle = query.lower()

	def rank(row):
		number = str(row.get("account_number") or "").lower()
		account_name = str(row.get("account_name") or "").lower()
		if number and number == needle:
			return (0, account_name)
		if account_name == needle:
			return (1, account_name)
		if account_name.startswith(needle) or number.startswith(needle):
			return (2, account_name)
		if needle in account_name or needle in number:
			return (3, account_name)
		return (4, account_name)

	ranked = sorted(found.values(), key=rank)[:limit]
	data = {
		"query": query,
		"company": company,
		"matches": ranked,
		"count": len(ranked),
		"total_before_limit": len(found),
		"note": "Ranked best-first: exact number, exact name, prefix, substring.",
	}
	return ToolResult(data, f"search {query!r}: {len(ranked)} of {len(found)} match(es)")


# ── shared ──────────────────────────────────────────────────────────────────
def _resolve_bank_account(value: str) -> str:
	"""A Bank Account docname from a docname or an account_name.

	ERPNext's Bank Account primary key is `"<label> - <bank>"`, so the same
	problem as Account, solved the same way.
	"""
	value = (value or "").strip()
	if not value:
		return ""
	if frappe.db.exists("Bank Account", value):
		return value
	matches = frappe.db.get_all("Bank Account", filters={"account_name": value}, pluck="name")
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ToolError(f"{value!r} matches {len(matches)} Bank Accounts: {', '.join(sorted(matches)[:10])}")
	known = frappe.db.get_all("Bank Account", pluck="name", limit=25)
	raise ToolError(
		f"no Bank Account matching {value!r}. Known bank accounts: {', '.join(sorted(known)) or '<none>'}"
	)


def _docstatus_label(docstatus) -> str:
	return {0: "draft", 1: "submitted", 2: "cancelled"}.get(int(docstatus or 0), "unknown")


# ── 149. find_drifted_je_attributions ───────────────────────────────────────
#: When v0.13.0 shipped the broken `update_journal_entry_party`, and when v0.14.0
#: fixed it. Upstream dates: a site that upgraded later ran the broken tool for
#: longer, which is why both ends are arguments with these as defaults rather
#: than constants the report asserts.
V013_RELEASED = "2026-07-30"
V014_RELEASED = "2026-07-31"

#: Most Journal Entries one scan will read. The scan is three queries whatever
#: this is, but the matcher runs per line and a caller asking for the whole
#: ledger wants a report they can act on rather than one they have to page
#: through.
DRIFT_SCAN_CAP = 500


def find_drifted_je_attributions(args: dict) -> ToolResult:
	"""Every submitted Journal Entry whose voucher and ledger disagree about a party.

	WHAT DRIFT IS, AND WHERE IT CAME FROM. A Journal Entry line carries
	`party_type` and `party`; so does each GL Entry row it posted. The voucher is
	what the entry shows and the GL is what every ageing report, party ledger and
	statement of account reads. They are supposed to say the same thing.

	v0.13.0's `update_journal_entry_party` looked its GL rows up by
	`voucher_detail_no == line.name`, which is the Sales Invoice Item convention
	and NOT the Journal Entry one — ERPNext fills that column from the line's
	`reference_detail_no`, empty on every ordinary line. So every call against a
	submitted entry matched zero GL rows, wrote the voucher, and returned a
	warning blaming the site. The result is a specific and silent damage class:
	entries whose voucher says one party and whose ledger says another, where
	nothing in either table admits to the disagreement. This tool finds them.

	IT IS NOT LIMITED TO THAT DAMAGE CLASS. Drift can also arrive from a direct
	database edit, a restored backup, or a migration that moved parties on one
	table and not the other. The scan reads the current state of both tables and
	does not care what caused it, which is why the vintage grouping is reported
	beside the finding rather than used to filter it.

	READ-ONLY, AND THREE QUERIES WHATEVER THE RANGE. Every candidate entry's lines
	and every candidate entry's GL rows are read in one query each, then matched in
	memory by the same `gl_link_for_line` the repair writes through — so a line
	this reports as drifted is a line the repair can actually match, and a line
	whose GL rows are ambiguous is reported as ambiguous rather than as clean.
	"""
	from . import mutate

	company = resolve_company(as_str(args, "company"), required=False)
	from_date = as_date(args, "from_date", required=True)
	to_date = as_date(args, "to_date", required=True)
	if to_date < from_date:
		raise ToolError(f"to_date {to_date} is before from_date {from_date}.")
	# max(1, ...) not `or 500`: a 0 survived `min` and reached Frappe as NO LIMIT.
	limit = max(1, min(DRIFT_SCAN_CAP, as_int(args, "limit", 500)))
	vintage_from = as_date(args, "vintage_from") or V013_RELEASED
	vintage_to = as_date(args, "vintage_to") or V014_RELEASED

	filters = {"docstatus": 1, "posting_date": ("between", (from_date, to_date))}
	if company:
		filters["company"] = company
	entries = frappe.db.get_all(
		"Journal Entry",
		filters=filters,
		fields=compat.existing_fields(
			"Journal Entry", ("name", "company", "posting_date", "modified", "voucher_type", "user_remark")
		),
		order_by="posting_date asc, name asc",
		limit=limit,
	)
	if not entries:
		return ToolResult(
			data={
				"company": company,
				"from_date": from_date,
				"to_date": to_date,
				"entries_scanned": 0,
				"drifted_entry_count": 0,
				"drifted_line_count": 0,
				"drifted": [],
				"note": "No submitted Journal Entry falls in that range, so there is nothing to check.",
			},
			summary=f"0 submitted entries between {from_date} and {to_date}",
		)

	names = [str(entry["name"]) for entry in entries]
	lines_by_entry = _lines_by_entry(names)
	gl_by_entry = _gl_by_entry(names)

	drifted, ambiguous = [], []
	for entry in entries:
		name = str(entry["name"])
		lines = lines_by_entry.get(name) or []
		gl_rows = gl_by_entry.get(name) or []
		if not lines or not gl_rows:
			continue
		for index, line in enumerate(lines, start=1):
			link = mutate.gl_link_for_line(name, lines, index, gl_rows=gl_rows)
			if link["blocker"]:
				if _line_has_party(line) or any(_line_has_party(row) for row in gl_rows):
					ambiguous.append(
						{
							"journal_entry": name,
							"line_index": index,
							"account": line.get("account"),
							"why": link["blocker"],
						}
					)
				continue
			for row in link["rows"]:
				if not _party_differs(line, row):
					continue
				drifted.append(
					{
						"journal_entry": name,
						"company": entry.get("company"),
						"posting_date": str(entry.get("posting_date") or ""),
						"modified": str(entry.get("modified") or ""),
						"line_index": index,
						"line_name": line.get("name"),
						"account": line.get("account"),
						"debit": round(float(line.get("debit") or 0), 2),
						"credit": round(float(line.get("credit") or 0), 2),
						"jea_party_type": str(line.get("party_type") or "") or None,
						"jea_party": str(line.get("party") or "") or None,
						"gle_party_type": str(row.get("party_type") or "") or None,
						"gle_party": str(row.get("party") or "") or None,
						"gl_entry": row.get("name"),
						"matched_by": link["basis"],
						"match_is_exact": bool(link["exact"]),
						"vintage": _vintage(entry.get("modified"), vintage_from, vintage_to),
						"repair": {
							"journal_entry": name,
							"line_index": index,
							"party_type": str(line.get("party_type") or ""),
							"party": str(line.get("party") or ""),
						},
					}
				)

	by_vintage: dict = {}
	for row in drifted:
		by_vintage[row["vintage"]] = by_vintage.get(row["vintage"], 0) + 1
	by_entry: dict = {}
	for row in drifted:
		by_entry.setdefault(row["journal_entry"], []).append(row["line_index"])

	return ToolResult(
		data={
			"company": company,
			"from_date": from_date,
			"to_date": to_date,
			"entries_scanned": len(entries),
			"scan_capped": len(entries) >= limit,
			"drifted_entry_count": len(by_entry),
			"drifted_line_count": len(drifted),
			"drifted_entries": sorted(by_entry),
			"by_vintage": dict(sorted(by_vintage.items())),
			"vintage_window": {"from": vintage_from, "to": vintage_to},
			"drifted": drifted,
			"ambiguous": ambiguous,
			"repair_input": [row["repair"] for row in drifted],
			"note": (
				"`repair_input` is the list repair_drifted_je_attributions takes verbatim. Every "
				"entry in it brings the LEDGER into line with the VOUCHER, which is the right "
				"direction for the v0.13.0 damage class — that tool wrote the voucher and failed "
				"to write the ledger, so the voucher holds the attribution somebody intended. "
				"If a particular line drifted for some other reason and the LEDGER is right, "
				"call update_journal_entry_party on it directly with the party you want on both."
			),
			"vintage_note": (
				f"`by_vintage` groups on the entry's modification date against the window "
				f"{vintage_from} to {vintage_to}, when the broken tool was live upstream. A site "
				"that upgraded later ran it for longer — pass vintage_from and vintage_to to "
				"match when YOUR site was on v0.13.0. The grouping is reported beside the "
				"finding and never used to filter it: drift from a restored backup or a direct "
				"database edit is just as real and lands outside the window."
			),
			"ambiguous_note": (
				f"{len(ambiguous)} line(s) carry a party somewhere but could not be matched to "
				"their GL rows with certainty — usually two lines of one voucher posting the same "
				"amount to the same account, which the ledger cannot tell apart. They are NOT "
				"counted as drifted and NOT in `repair_input`: reporting a coin toss as a finding "
				"would be worse than reporting nothing. investigate_je_gl_link shows each one."
				if ambiguous
				else "Every line with a party matched its GL rows unambiguously."
			),
		},
		summary=(
			f"{len(drifted)} drifted line(s) across {len(by_entry)} of {len(entries)} submitted "
			f"entr(y/ies) between {from_date} and {to_date}"
			+ (f", {len(ambiguous)} ambiguous" if ambiguous else "")
		),
	)


def _lines_by_entry(names: list) -> dict:
	"""Every account line of every candidate entry, in one query, in `idx` order.

	Order matters: `line_index` is the position ERPNext numbers a line by, and a
	report that indexed them in whatever order the database returned would name
	the wrong line in a repair instruction.
	"""
	rows = frappe.db.get_all(
		"Journal Entry Account",
		filters={"parent": ("in", names)},
		fields=compat.existing_fields(
			"Journal Entry Account",
			(
				"name",
				"parent",
				"idx",
				"account",
				"debit",
				"credit",
				"party_type",
				"party",
				"reference_detail_no",
			),
		),
		order_by="parent asc, idx asc",
		limit=DRIFT_SCAN_CAP * 20,
	)
	out: dict = {}
	for row in rows or []:
		out.setdefault(str(row.get("parent")), []).append(dict(row))
	return out


def _gl_by_entry(names: list) -> dict:
	"""Every live GL Entry row of every candidate entry, in one query."""
	if not compat.doctype_exists("GL Entry"):  # pragma: no cover - ERPNext always ships it
		return {}
	filters = {"voucher_type": "Journal Entry", "voucher_no": ("in", names)}
	if compat.has_field("GL Entry", "is_cancelled"):
		filters["is_cancelled"] = 0
	rows = frappe.db.get_all(
		"GL Entry",
		filters=filters,
		fields=compat.existing_fields(
			"GL Entry",
			(
				"name",
				"voucher_no",
				"account",
				"debit",
				"credit",
				"party_type",
				"party",
				"voucher_detail_no",
				"posting_date",
				"cost_center",
			),
		),
		order_by="voucher_no asc, name asc",
		limit=DRIFT_SCAN_CAP * 20,
	)
	out: dict = {}
	for row in rows or []:
		out.setdefault(str(row.get("voucher_no")), []).append(dict(row))
	return out


def _line_has_party(row) -> bool:
	return bool(str(row.get("party_type") or "") or str(row.get("party") or ""))


def _party_differs(line, row) -> bool:
	return str(line.get("party_type") or "") != str(row.get("party_type") or "") or str(
		line.get("party") or ""
	) != str(row.get("party") or "")


def _vintage(modified, vintage_from: str, vintage_to: str) -> str:
	"""Which side of the v0.13.0 window an entry was last modified on."""
	stamp = str(modified or "")[:10]
	if not stamp:
		return "unknown (no modification date)"
	if stamp < vintage_from:
		return f"before {vintage_from} (predates the broken tool)"
	if stamp > vintage_to:
		return f"after {vintage_to} (postdates the fix — drift from another cause)"
	return f"{vintage_from} to {vintage_to} (the v0.13.0 window)"
