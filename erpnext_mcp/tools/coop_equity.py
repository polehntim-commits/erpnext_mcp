# SPDX-License-Identifier: MIT
"""Co-op equity and patronage dividends — captured as receipts, booked as neither expense nor bill.

v0.166.0. A farm that buys into a cooperative pays money that is not a cost: it
is a stake, and it sits on the balance sheet until the co-op retires it. The co-op
then pays patronage back, which is income — partly in cash, often partly retained
as more equity. Both arrive as paper, so both are captured through the receipt
flow, as `Co-op Equity` or `Patronage Dividend`; neither may become a Purchase
Invoice, and neither counts in the expense totals.

THREE TOOLS.

`ensure_coop_accounts` creates the equity account a company needs, idempotently.
`post_coop_receipt` books an Approved co-op receipt as a DRAFT Journal Entry.
`list_coop_equity_summary` totals the positions by company and co-op.

THE ACCOUNT NUMBERS ARE NOT FIXED, BECAUSE THE CHARTS ARE NOT. Written against a
site where Orchard Meadow's `1830` was free and `4230` was Dividend Income; on the
deployed site the same company's `1830` is Accumulated Depreciation - Machinery and
`4230` is Realized Capital Gains, and Polehn Farms has no dividend account at all.
So an account is found BY NAME first. When the equity account has to be created it
goes under the company's `1800` group, at 1830 if that is free and at the next free
number in the group's hundred if it is not, and the answer says which. A company
whose chart has no such group is refused with the argument that names a parent.

v0.166.1: PATRONAGE POSTS TO THE EXISTING `Dividend Income` ACCOUNT, per the design
doc, and no patronage account is created. It is found by NAME rather than as 4230,
because 4230 is Dividend Income on one site and Realized Capital Gains on the other.
A company without a `Dividend Income` ledger (Polehn Farms, today) refuses a
patronage posting by name. v0.166.0 created a separate `Patronage Dividends`
account under 4100; that was reversed before any site ran it.

NOTHING HERE IS SUBMITTED. The Journal Entry is a draft, exactly as
`create_owner_draw`'s is, and posting it is `submit_journal_entry`'s separate switch.
"""

import frappe

from .. import compat
from ..args import as_bool, as_date, as_float, as_str, resolve_account, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import accounts, expenses, governance, mutate

EXPENSE_RECEIPT = expenses.EXPENSE_RECEIPT
COOP_EQUITY_CATEGORY = expenses.COOP_EQUITY_CATEGORY
PATRONAGE_CATEGORY = expenses.PATRONAGE_CATEGORY
COOP_CATEGORIES = expenses.COOP_CATEGORIES

#: The equity account, and where it goes when a company does not have it yet.
#: `parent_arg` is the argument that names a parent on a chart without the group.
EQUITY_ACCOUNT = {
	"key": "equity",
	"account_name": "Co-op Equity Investments",
	"root_type": "Asset",
	"account_type": "",
	"parent_number": "1800",
	"preferred_number": "1830",
	"parent_arg": "equity_parent",
}
#: Where patronage posts: the company's existing Dividend Income ledger, found by
#: name. NEVER CREATED by this module — see the module docstring.
PATRONAGE_ACCOUNT = {
	"key": "patronage",
	"account_name": "Dividend Income",
	"root_type": "Income",
	"design_number": "4230",
}

#: The remark on the line that books retained patronage as equity. The summary reads
#: retained patronage off this marker, so it holds whatever account a site named.
RETAINED_REMARK = "retained as equity"

#: How many co-op receipts one summary reads before saying it stopped.
_SUMMARY_CAP = 5000


# ── accounts ────────────────────────────────────────────────────────────────
def find_account(company: str, spec: dict) -> dict | None:
	"""The company's existing account for `spec`, matched by name, or None."""
	rows = frappe.db.get_all(
		"Account",
		filters={
			"company": company,
			"account_name": spec["account_name"],
			"is_group": 0,
			"root_type": spec["root_type"],
		},
		fields=["name", "account_number", "parent_account", "disabled", "root_type"],
		order_by="name asc",
		limit=5,
	)
	return dict(rows[0]) if rows else None


def _parent(company: str, spec: dict, explicit: str) -> str:
	if explicit:
		return accounts._validate_parent(explicit, company, spec["root_type"], spec["parent_arg"])["name"]
	rows = frappe.db.get_all(
		"Account",
		filters={
			"company": company,
			"account_number": spec["parent_number"],
			"is_group": 1,
			"root_type": spec["root_type"],
		},
		pluck="name",
		limit=2,
	)
	if not rows:
		raise ToolError(
			f"{company} has no {spec['root_type']} group numbered {spec['parent_number']} to put "
			f"{spec['account_name']} under. Pass {spec['parent_arg']} naming the group it belongs in."
		)
	return rows[0]


def _free_number(company: str, spec: dict) -> str:
	"""The preferred number if free, else the next free one in the parent's hundred."""
	preferred = int(spec["preferred_number"])
	base = int(spec["parent_number"])
	for number in (preferred, *range(preferred + 1, base + 100), *range(base + 1, preferred)):
		if not accounts._number_holder(company, str(number)):
			return str(number)
	raise ToolError(
		f"every account number from {base + 1} to {base + 99} is taken in {company}. Pass "
		f"{spec['parent_arg']} naming a different group."
	)


def ensure_account(company: str, spec: dict, *, parent: str = "", dry_run: bool = False) -> dict:
	"""Find or create one of the two accounts in one company. Says which it did."""
	existing = find_account(company, spec)
	if existing:
		return {
			"account": existing["name"],
			"action": "existing",
			"account_number": existing.get("account_number") or None,
			"parent_account": existing.get("parent_account"),
			"disabled": bool(existing.get("disabled")),
		}
	parent_name = _parent(company, spec, parent)
	number = _free_number(company, spec)
	moved = number != spec["preferred_number"]
	note = (
		f"{spec['preferred_number']} is taken by {accounts._number_holder(company, spec['preferred_number'])} "
		f"in {company}, so this is {number}."
		if moved
		else None
	)
	if dry_run:
		return {
			"account": None,
			"action": "would_create",
			"account_number": number,
			"parent_account": parent_name,
			"note": note,
		}
	created = accounts.create_account(
		{
			"company": company,
			"account_name": spec["account_name"],
			"account_number": number,
			"root_type": spec["root_type"],
			"parent_account": parent_name,
			**({"account_type": spec["account_type"]} if spec["account_type"] else {}),
		}
	).data
	return {
		"account": created["name"],
		"action": "created",
		"account_number": number,
		"parent_account": parent_name,
		"note": note,
	}


def _named_account(args: dict, key: str, company: str, spec: dict, tail: str) -> tuple:
	"""The account a caller named under `key`, checked, or the default found by name.

	v0.166.1. NAMING THE ACCOUNT IS THE MCP PATHWAY, and the names this module ships
	are only the default. A site whose chart calls its co-op stake something else,
	or books patronage somewhere other than Dividend Income, passes the account and
	nothing in this module has to know its name. Returns `(account, resolved_by)`.
	"""
	named = as_str(args, key)
	if not named:
		return require_account(company, spec), "name match"
	account = resolve_account(named, company)
	row = frappe.db.get_value("Account", account, ["root_type", "is_group", "disabled"], as_dict=True) or {}
	if row.get("root_type") != spec["root_type"]:
		raise ToolError(
			f"{key} {account!r} has root type {row.get('root_type') or 'none'}, and this line needs "
			f"root type {spec['root_type']}. {tail}"
		)
	if int(row.get("is_group") or 0):
		raise ToolError(f"{key} {account!r} is a group, and a group cannot be posted to. {tail}")
	if int(row.get("disabled") or 0):
		raise ToolError(f"{key} {account!r} is disabled. {tail}")
	return account, "argument"


def require_account(company: str, spec: dict) -> str:
	"""The account a posting needs, or a refusal pointing at `ensure_coop_accounts`."""
	existing = find_account(company, spec)
	if not existing and spec is PATRONAGE_ACCOUNT:
		raise ToolError(
			f"{company} has no Dividend Income account, which a patronage dividend posts to. Create "
			f"one ({spec['design_number']} under the company's income accounts) and post again. "
			"Nothing was created."
		)
	if not existing:
		raise ToolError(
			f"{company} has no {spec['account_name']} account yet. Run ensure_coop_accounts for "
			f"{company} first. Nothing was created."
		)
	if existing.get("disabled"):
		raise ToolError(f"{existing['name']} is disabled in {company}. Nothing was created.")
	return existing["name"]


# ── ensure_coop_accounts ────────────────────────────────────────────────────
def ensure_coop_accounts(args: dict) -> ToolResult:
	"""Create Co-op Equity Investments where a company lacks it, and report Dividend Income.

	EVERY COMPANY WHEN NONE IS NAMED. Each company is answered on its own: one whose
	chart has no `1800` group is refused in its own row, and the others still get
	their account. `dry_run` reports what would be created and creates nothing.

	THE PATRONAGE ROW IS A CHECK, NOT A CREATE. It says whether the company has the
	Dividend Income ledger patronage posts to (`existing`) or not (`missing`), so a
	company that cannot book patronage is visible before a receipt fails to post.
	"""
	dry_run = bool(as_bool(args, "dry_run", False))
	named = as_str(args, "company")
	if named:
		companies = [resolve_company(named, required=True)]
	else:
		companies = sorted(frappe.db.get_all("Company", pluck="name"))

	results = []
	created = 0
	refused = 0
	missing_income = 0
	for company in companies:
		row = {"company": company}
		try:
			row["equity"] = ensure_account(
				company,
				EQUITY_ACCOUNT,
				parent=as_str(args, "equity_parent") if named else "",
				dry_run=dry_run,
			)
			created += row["equity"]["action"] == "created"
		except ToolError as exc:
			row["equity"] = {"account": None, "action": "refused", "reason": str(exc)}
			refused += 1
		income = find_account(company, PATRONAGE_ACCOUNT)
		row["patronage"] = (
			{
				"account": income["name"],
				"action": "existing",
				"account_number": income.get("account_number") or None,
				"disabled": bool(income.get("disabled")),
			}
			if income
			else {
				"account": None,
				"action": "missing",
				"note": "No Dividend Income ledger, so a patronage dividend cannot be posted here. "
				"Not created: create it in the chart if this company receives patronage.",
			}
		)
		missing_income += 0 if income else 1
		results.append(row)

	return ToolResult(
		data={
			"dry_run": dry_run,
			"companies": results,
			"created_count": created,
			"refused_count": refused,
			"dividend_income_missing_count": missing_income,
			"note": (
				"equity_parent applies only when one company is named. "
				if as_str(args, "equity_parent") and not named
				else ""
			)
			+ ("Nothing was created: dry_run." if dry_run else ""),
		},
		summary=f"co-op accounts across {len(companies)} company(ies): {created} created, {refused} refused"
		+ (" (dry run)" if dry_run else ""),
		docstatus_delta="none" if dry_run or not created else "none → 0 (created)",
	)


# ── post_coop_receipt ───────────────────────────────────────────────────────
def post_coop_receipt(args: dict) -> ToolResult:
	"""Book one Approved co-op receipt as a DRAFT Journal Entry.

	CO-OP EQUITY: Dr Co-op Equity Investments, Cr bank. A receipt marked `is_return`
	is equity the co-op retired and paid out, and posts the other way round.

	PATRONAGE DIVIDEND: Cr Dividend Income for the whole amount; Dr bank for the
	cash, and Dr Co-op Equity Investments for `retained_amount`, the part the co-op
	kept as equity. The income line carries the company's default cost center,
	because ERPNext refuses a Profit and Loss line without one at submit.
	"""
	tail = "Nothing was created."
	receipt = as_str(args, "receipt") or as_str(args, "expense_receipt") or as_str(args, "name")
	if not receipt:
		raise ToolError(f"receipt (the Expense Receipt docname) is required. {tail}")
	if not frappe.db.exists(EXPENSE_RECEIPT, receipt):
		raise ToolError(f"no Expense Receipt called {receipt!r} on this site. {tail}")
	row = frappe.db.get_value(
		EXPENSE_RECEIPT,
		receipt,
		[
			"category",
			"status",
			"amount",
			"company",
			"merchant",
			"receipt_date",
			"linked_doctype",
			"linked_document",
			*compat.existing_fields(EXPENSE_RECEIPT, ("is_return", "coop_name")),
		],
		as_dict=True,
	)
	category = row.get("category")
	if category not in COOP_CATEGORIES:
		raise ToolError(
			f"{receipt} is categorised {category!r}, and this tool books only a "
			f"{' or '.join(COOP_CATEGORIES)} receipt. {tail}"
		)
	if row.get("status") != expenses.APPROVED:
		raise ToolError(
			f"{receipt} is {row.get('status')!r}, not Approved. Approve it with "
			f"approve_expense_receipt first. {tail}"
		)
	if row.get("linked_document"):
		raise ToolError(
			f"{receipt} is already linked to {row['linked_doctype']} {row['linked_document']}. {tail}"
		)
	amount = float(row.get("amount") or 0)
	if amount <= 0:
		raise ToolError(f"{receipt} has amount {amount}; there is nothing to book. {tail}")

	company = row["company"]
	is_return = bool(row.get("is_return"))
	coop_name = row.get("coop_name") or row.get("merchant")
	equity, equity_resolved_by = _named_account(args, "equity_account", company, EQUITY_ACCOUNT, tail)
	counter = governance._counter_account(args, company)
	posting_date = as_date(args, "posting_date") or str(row.get("receipt_date"))

	retained_given = args.get("retained_amount") not in (None, "")
	retained = as_float(args.get("retained_amount"), "retained_amount")
	remark_head = f"{category} — {coop_name} ({receipt})"

	if category == COOP_EQUITY_CATEGORY:
		if retained_given:
			raise ToolError(f"retained_amount applies to a patronage dividend, not to {category}. {tail}")
		if as_str(args, "income_account"):
			raise ToolError(f"income_account applies to a patronage dividend, not to {category}. {tail}")
		income_resolved_by = None
		money_in, money_out = (counter, equity) if is_return else (equity, counter)
		raw = [
			{"account": money_in, "debit": amount, "user_remark": remark_head},
			{"account": money_out, "credit": amount, "user_remark": remark_head},
		]
		income_account = None
		cost_center = None
	else:
		if is_return:
			raise ToolError(
				f"{receipt} is a patronage dividend marked as a return. Patronage paid back to a co-op "
				f"is not a case this tool books; post it with create_journal_entry. {tail}"
			)
		if retained < 0 or retained > amount:
			raise ToolError(f"retained_amount must be between 0 and {amount} — got {retained}. {tail}")
		income_account, income_resolved_by = _named_account(
			args, "income_account", company, PATRONAGE_ACCOUNT, tail
		)
		cost_center = as_str(args, "cost_center") or frappe.db.get_value("Company", company, "cost_center")
		if not cost_center:
			raise ToolError(
				f"{company} has no default cost center, and ERPNext refuses an income line without "
				f"one. Pass cost_center. {tail}"
			)
		cash = round(amount - retained, 2)
		raw = []
		if cash > 0:
			raw.append({"account": counter, "debit": cash, "user_remark": f"{remark_head}: cash"})
		if retained > 0:
			raw.append(
				{"account": equity, "debit": retained, "user_remark": f"{remark_head}: {RETAINED_REMARK}"}
			)
		raw.append(
			{
				"account": income_account,
				"credit": amount,
				"cost_center": cost_center,
				"user_remark": remark_head,
			}
		)

	lines = mutate.validated_journal_lines(raw, company)
	doc = mutate.insert_draft_journal_entry(company, posting_date, lines, remark_head)
	frappe.db.set_value(
		EXPENSE_RECEIPT, receipt, {"linked_doctype": "Journal Entry", "linked_document": doc.name}
	)

	return ToolResult(
		data={
			"journal_entry": doc.name,
			"docstatus": 0,
			"receipt": receipt,
			"company": company,
			"category": category,
			"coop_name": coop_name,
			"amount": amount,
			"is_return": is_return,
			"posting_date": posting_date,
			"equity_account": equity,
			"equity_account_resolved_by": equity_resolved_by,
			"income_account": income_account,
			"income_account_resolved_by": income_resolved_by,
			"counter_account": counter,
			"cost_center": cost_center,
			"retained_amount": retained if category == PATRONAGE_CATEGORY else None,
			"lines": [
				{
					"account": line["account"],
					"debit": line.get("debit") or 0,
					"credit": line.get("credit") or 0,
				}
				for line in raw
			],
		},
		summary=f"draft Journal Entry {doc.name} for {receipt}: {category} {amount} with {coop_name}",
		docstatus_delta="none → 0 (draft Journal Entry)",
	)


# ── list_coop_equity_summary ────────────────────────────────────────────────
def list_coop_equity_summary(args: dict) -> ToolResult:
	"""Co-op positions by company and co-op: invested, redeemed, patronage, and the dates.

	`net_equity` is invested minus redeemed PLUS patronage retained as equity, which
	is read off the draft or posted Journal Entry `post_coop_receipt` made — a
	retained allocation is equity the farm holds, and leaving it out would understate
	the position by exactly the part the co-op did not pay in cash. Rejected receipts
	are left out unless `status` asks for them.
	"""
	if not compat.has_field(EXPENSE_RECEIPT, "coop_name"):
		raise ToolError(
			"this site's Expense Receipt does not have the co-op columns yet. Run `bench --site "
			"<site> migrate` after installing v0.166.0."
		)
	filters = {"category": ("in", list(COOP_CATEGORIES))}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	status = as_str(args, "status")
	if status:
		if status not in expenses.STATUSES:
			raise ToolError(f"status must be one of: {', '.join(expenses.STATUSES)}.")
		filters["status"] = status
	else:
		filters["status"] = ("!=", expenses.REJECTED)
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["receipt_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["receipt_date"] = (">=", from_date)
	elif to_date:
		filters["receipt_date"] = ("<=", to_date)
	wanted_coop = as_str(args, "coop_name").lower()

	rows = frappe.db.get_all(
		EXPENSE_RECEIPT,
		filters=filters,
		fields=[
			"name",
			"company",
			"category",
			"amount",
			"receipt_date",
			"merchant",
			"coop_name",
			"linked_doctype",
			"linked_document",
			*compat.existing_fields(EXPENSE_RECEIPT, ("is_return",)),
		],
		order_by="receipt_date asc, name asc",
		limit_page_length=_SUMMARY_CAP,
	)

	equity_accounts: dict = {}
	positions: dict = {}
	for row in rows:
		coop = str(row.get("coop_name") or row.get("merchant") or "").strip() or "(unnamed)"
		if wanted_coop and wanted_coop not in coop.lower():
			continue
		key = (row["company"], coop)
		position = positions.setdefault(
			key,
			{
				"company": row["company"],
				"coop_name": coop,
				"equity_invested": 0.0,
				"equity_redeemed": 0.0,
				"patronage_received": 0.0,
				"patronage_retained": 0.0,
				"equity_transactions": 0,
				"patronage_transactions": 0,
				"transaction_count": 0,
				"unposted_count": 0,
				"first_date": None,
				"last_date": None,
			},
		)
		amount = float(row.get("amount") or 0)
		date = str(row.get("receipt_date") or "") or None
		if row["category"] == COOP_EQUITY_CATEGORY:
			position["equity_transactions"] += 1
			bucket = "equity_redeemed" if row.get("is_return") else "equity_invested"
			position[bucket] = round(position[bucket] + amount, 2)
		else:
			position["patronage_transactions"] += 1
			position["patronage_received"] = round(position["patronage_received"] + amount, 2)
			if row.get("linked_doctype") == "Journal Entry" and row.get("linked_document"):
				position["patronage_retained"] = round(
					position["patronage_retained"] + _retained(row, equity_accounts), 2
				)
		position["transaction_count"] += 1
		position["unposted_count"] += 0 if row.get("linked_document") else 1
		if date:
			position["first_date"] = min(filter(None, (position["first_date"], date)))
			position["last_date"] = max(filter(None, (position["last_date"], date)))

	out = []
	for position in sorted(positions.values(), key=lambda p: (p["company"], p["coop_name"].lower())):
		position["net_equity"] = round(
			position["equity_invested"] - position["equity_redeemed"] + position["patronage_retained"], 2
		)
		out.append(position)

	by_company: dict = {}
	for position in out:
		total = by_company.setdefault(
			position["company"],
			{
				"company": position["company"],
				"coop_count": 0,
				"net_equity": 0.0,
				"patronage_received": 0.0,
				"transaction_count": 0,
			},
		)
		total["coop_count"] += 1
		total["net_equity"] = round(total["net_equity"] + position["net_equity"], 2)
		total["patronage_received"] = round(total["patronage_received"] + position["patronage_received"], 2)
		total["transaction_count"] += position["transaction_count"]

	truncated = len(rows) == _SUMMARY_CAP
	return ToolResult(
		data={
			"positions": out,
			"position_count": len(out),
			"companies": [by_company[name] for name in sorted(by_company)],
			"truncated": truncated,
			"filters": {
				"company": filters.get("company") or None,
				"coop_name": as_str(args, "coop_name") or None,
				"status": status or None,
				"from_date": from_date,
				"to_date": to_date,
			},
			"note": (
				"Totals come from the receipts. unposted_count is receipts with no Journal Entry yet; "
				"get_account_balance on Co-op Equity Investments is the ledger's own figure. "
			)
			+ (f"Stopped after {_SUMMARY_CAP} receipts; narrow the dates." if truncated else ""),
		},
		summary=f"{len(out)} co-op position(s) across {len(by_company)} company(ies)",
	)


def _retained(row: dict, equity_accounts: dict) -> float:
	"""The patronage a co-op kept as equity, read off the Journal Entry that booked it.

	A line counts when it carries `post_coop_receipt`'s own retained-equity remark, so
	an equity account the caller named is counted, or when it debits the company's
	default Co-op Equity Investments account, for an entry whose remark was edited.
	"""
	company = row["company"]
	if company not in equity_accounts:
		existing = find_account(company, EQUITY_ACCOUNT)
		equity_accounts[company] = existing["name"] if existing else ""
	account = equity_accounts[company]
	if not frappe.db.exists("Journal Entry", row["linked_document"]):
		return 0.0
	entry = frappe.get_doc("Journal Entry", row["linked_document"])
	if int(entry.get("docstatus") or 0) == 2:
		return 0.0
	total = 0.0
	for line in entry.get("accounts") or []:
		get = line.get if isinstance(line, dict) else (lambda key, line=line: getattr(line, key, None))
		marked = str(get("user_remark") or "").endswith(RETAINED_REMARK)
		if marked or (account and get("account") == account):
			total += float(get("debit_in_account_currency") or get("debit") or 0)
	return total
