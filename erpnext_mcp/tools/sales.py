# SPDX-License-Identifier: MIT
"""Sales and settlements: what the packer owed, what was invoiced, what was paid.

v0.70.0. Sprint 5 of the gap-closure plan. Sprints 2 and 3 built the two ends of
the grower-packer pipeline — the Scale Ticket that says a load was delivered, and
the Settlement Statement that says what the packer eventually paid for it. This
module is the middle and the end: turning a settlement into revenue in the
general ledger, collecting the money, and reading back what the season actually
did.

    Scale Ticket(s) → Settlement Statement → Sales Invoice → Payment Entry

────────────────────────────────────────────────────────────────────────────
THERE IS NO DELIVERY NOTE, AND THAT IS THE DESIGN
────────────────────────────────────────────────────────────────────────────

ERPNext's Delivery Note is the *seller's* record of goods leaving on the
seller's terms, priced, in Items and UOMs the seller controls. In grower-packer
the seller controls none of that: the packer owns the scale, prints the ticket,
decides the variety and the grade, and states the price months later. The Scale
Ticket IS the delivery evidence, and `receipts.py` argues that at length. Adding
a Delivery Note here would create a second record of one delivery, disagreeing
with the first, with nothing to say which is right.

So this module never writes one, and `get_season_summary` traces deliveries
through Scale Tickets rather than through a Stock ledger.

────────────────────────────────────────────────────────────────────────────
TWO PATHS OUT OF A SETTLEMENT, AND WHY THE INVOICE IS THE DEFAULT
────────────────────────────────────────────────────────────────────────────

`create_sales_invoice_from_settlement` is the ordinary path. It produces an
ERPNext Sales Invoice, which is what makes the proceeds appear in AR ageing, in
payment allocation, and in every standard receivables report an accountant
already knows how to read. `post_settlement_to_gl` is the alternative for
operations that book settlements as Journal Entries — it produces a DRAFT entry
and posts nothing, exactly like `create_journal_entry`, because the split
between "propose" and "post" is the whole permission model of this app.

Both write the same two link fields, so a reader coming from either side can get
back to the other document. Neither will run twice against one settlement: the
second call is refused by name, because two revenue postings for one statement
is precisely the double-count this register exists to make impossible.

────────────────────────────────────────────────────────────────────────────
THE INVOICE TOTAL IS THE SETTLEMENT'S NET PROCEEDS, NOT A RECOMPUTATION
────────────────────────────────────────────────────────────────────────────

A settlement line carries a packed weight, a price and a gross amount, and the
three do not always multiply out — `receipts.py` keeps a stated gross amount
rather than deriving it, because a packer who applied a pool adjustment to one
line is telling the truth and the multiplication is not.

ERPNext's Sales Invoice Item has no such tolerance: it computes `amount` as
`qty × rate` on every validate. So a line whose stated amount disagrees with its
own arithmetic has its RATE adjusted, not its amount — `rate = amount / qty` —
and the line reports `stated_price_per_unit` beside the rate that was used, plus
`rate_differs_from_statement: true`. The invoice therefore totals to exactly
what the packer said they owed, and the adjustment is visible rather than
absorbed.

Deductions become negative "Actual" tax-and-charge rows against an expense
account, which is how ERPNext models a charge the buyer withholds: revenue is
recognised GROSS, the packing and storage charges land in expense, and the
receivable is the net. Netting them into the revenue line instead would hide the
single number a grower most wants a year later — what did storage cost me.
"""

from __future__ import annotations

import re

import frappe

from .. import compat
from ..args import (
	as_bool,
	as_date,
	as_float,
	as_limit,
	as_str,
	resolve_account,
	resolve_company,
	resolve_cost_center,
)
from ..errors import ToolError
from ..result import ToolResult
from . import masters, mutate, receipts

SALES_INVOICE = "Sales Invoice"
SALES_INVOICE_ITEM = "Sales Invoice Item"
SALES_TAXES_AND_CHARGES = "Sales Taxes and Charges"
PAYMENT_ENTRY = "Payment Entry"
SETTLEMENT_STATEMENT = receipts.SETTLEMENT_STATEMENT
SCALE_TICKET = receipts.SCALE_TICKET
CUSTOMER = "Customer"

#: What `require_doctype` says when Sales Invoice or Payment Entry is missing.
_ERPNEXT_HINT = "It ships with ERPNext's Accounts module — install ERPNext to use this tool."

#: What the two link fields are called on each side. The settlement's is declared
#: in this app's own DocType JSON; the invoice's is a Custom Field, because
#: Sales Invoice belongs to ERPNext and this app does not edit somebody else's
#: doctype JSON. `ensure_settlement_link_field` creates it on first use.
SETTLEMENT_LINK_FIELD = "settlement_statement"
SALES_INVOICE_LINK_FIELD = "sales_invoice"

#: Upper bound of each ageing bucket, in days overdue. The same scheme
#: `trade.get_outstanding_invoices` and `purchasing.get_ap_aging` use, so a
#: reader who knows one ageing report in this app knows all three.
AGEING_BUCKETS = (("0-30", 30), ("31-60", 60), ("61-90", 90), ("90+", None))

#: What `get_packout_summary` will group by, and what each one can honestly
#: report. See `_GROUP_BASIS` for the sentence each returns.
PACKOUT_GROUP_BY = ("variety", "grade", "customer", "field", "month")

#: One shared, non-stock Item per variety and grade — never one per settlement.
#: Same reasoning as `receipts._service_item_for_category`: ERPNext requires an
#: `item_code` on every Sales Invoice line, a settlement has no Item behind it,
#: and inventing one per statement would flood the Item master with one-off rows
#: for the same dozen varieties a farm actually grows.
_FRUIT_ITEM_PREFIX = "FRUIT-"

#: Where a settlement line with no variety on it lands.
GENERIC_FRUIT_ITEM = "FRUIT-SALES"

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")

#: Default payment terms when a settlement is invoiced and nobody named a due
#: date. Thirty days is the ordinary term on a packer settlement; it is a
#: DEFAULT and every caller can override it.
DEFAULT_PAYMENT_TERMS_DAYS = 30


# ── ageing helpers ───────────────────────────────────────────────────────────
#
# Deliberate near-copies of `trade._days_overdue` / `trade._bucket` and
# `purchasing`'s pair. Three modules, three copies, one behaviour — asserted
# equal by the tests. Importing a private name across tool modules would couple
# the receivables report to the payables one for four lines of arithmetic.


def _days_overdue(due_date, as_of: str):
	if not due_date:
		return None
	try:
		return (frappe.utils.getdate(as_of) - frappe.utils.getdate(due_date)).days
	except Exception:
		return None


def _bucket(days) -> str:
	if days is None:
		return "unknown"
	if days <= 0:
		return "current"
	for label, upper in AGEING_BUCKETS:
		if upper is None or days <= upper:
			return label
	return "90+"


def _empty_buckets() -> dict:
	buckets = {label: {"count": 0, "outstanding": 0.0} for label, _ in AGEING_BUCKETS}
	buckets["current"] = {"count": 0, "outstanding": 0.0}
	buckets["unknown"] = {"count": 0, "outstanding": 0.0}
	return buckets


def _add_to_bucket(buckets: dict, label: str, outstanding: float) -> None:
	buckets.setdefault(label, {"count": 0, "outstanding": 0.0})
	buckets[label]["count"] += 1
	buckets[label]["outstanding"] = round(buckets[label]["outstanding"] + outstanding, 2)


# ── the two link fields ──────────────────────────────────────────────────────


def ensure_settlement_link_field() -> bool:
	"""Make sure Sales Invoice can point back at a Settlement Statement.

	A Custom Field, which is Frappe's supported way for one app to extend
	another's doctype — the same mechanism `compliance_fields.py` uses and
	defends. Created on first use rather than only at install, so a site that
	upgraded without running the installer still gets a working link the first
	time somebody invoices a settlement.

	NEVER RAISES. A site that will not take the field still gets its Sales
	Invoice; what it loses is the back-link, and every tool here reports
	`back_link_set: false` rather than pretending the pointer exists.
	"""
	try:
		if compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD):
			return True
	except Exception:
		return False
	if not compat.doctype_exists("Custom Field") or not compat.doctype_exists(SETTLEMENT_STATEMENT):
		return False
	try:
		if frappe.db.exists("Custom Field", {"dt": SALES_INVOICE, "fieldname": SETTLEMENT_LINK_FIELD}):
			return True
		doc = frappe.new_doc("Custom Field")
		doc.dt = SALES_INVOICE
		doc.fieldname = SETTLEMENT_LINK_FIELD
		doc.label = "Settlement Statement"
		doc.fieldtype = "Link"
		doc.options = SETTLEMENT_STATEMENT
		doc.read_only = 1
		doc.description = (
			"The packer settlement this invoice recognises as revenue. Set by "
			"create_sales_invoice_from_settlement; read-only because the pair of links has to "
			"stay symmetrical for either document to be trustworthy."
		)
		doc.insert(ignore_permissions=True)
		try:
			frappe.clear_cache(doctype=SALES_INVOICE)
		except Exception:
			pass
		return True
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: could not add the settlement_statement field to Sales Invoice",
			message=compat.traceback_text(),
		)
		return False


def _settlement_can_link() -> bool:
	"""Whether this site's Settlement Statement has the `sales_invoice` column.

	False on a bench that installed v0.70.0 and has not migrated. The invoice is
	still created; the forward link is reported as unset with the reason.
	"""
	try:
		return compat.has_field(SETTLEMENT_STATEMENT, SALES_INVOICE_LINK_FIELD)
	except Exception:
		return False


# ── shared resolvers ─────────────────────────────────────────────────────────


def _require_sales_invoice() -> None:
	compat.require_doctype(SALES_INVOICE, _ERPNEXT_HINT)


def _customer(value: str, required: bool = False) -> str:
	"""A Customer docname from a docname or a customer_name.

	`receipts._customer` already solves this and is the same question — the
	packer named on a ticket is the customer billed on the invoice — so this
	delegates rather than growing a second, subtly different resolver.
	"""
	if not value and required:
		raise ToolError("customer is required — a Sales Invoice is a bill TO somebody.")
	return receipts._customer(value, required=required)


def _settlement_row(args: dict, *, required: bool = True) -> dict:
	"""The Settlement Statement this call is about, read once, or a refusal."""
	name = (
		as_str(args, "settlement_statement")
		or as_str(args, "settlement")
		or as_str(args, "statement")
		or as_str(args, "name")
	)
	if not name:
		if not required:
			return {}
		raise ToolError("settlement_statement (the Settlement Statement docname) is required.")
	compat.require_doctype(
		SETTLEMENT_STATEMENT,
		"It ships with erpnext_mcp — run `bench migrate` after installing v0.67.0.",
	)
	if not frappe.db.exists(SETTLEMENT_STATEMENT, name):
		raise ToolError(f"no Settlement Statement called {name!r} on this site.")
	return {"name": name}


def _resolve_receivable_account(explicit: str, company: str) -> str:
	"""The Receivable account a Sales Invoice debits or a Payment Entry credits.

	Three tries, most-specific first — the mirror of
	`purchasing._resolve_payable_account`, and refusing for the same reason:
	guessing which asset account a caller meant is how a receivable ends up
	somewhere nobody reconciles.
	"""
	if explicit:
		return resolve_account(explicit, company)
	default = frappe.db.get_value("Company", company, "default_receivable_account")
	if default:
		return default
	matches = frappe.db.get_all(
		"Account", filters={"company": company, "account_type": "Receivable"}, pluck="name", limit=5
	)
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ToolError(
			f"{company} has no default_receivable_account set and {len(matches)} accounts typed "
			f"Receivable: {', '.join(sorted(matches))}. Pass debit_to explicitly. Nothing was created."
		)
	raise ToolError(
		f"{company} has no default_receivable_account and no account typed Receivable. "
		"Pass debit_to explicitly, or set the company default. Nothing was created."
	)


def _resolve_income_account(explicit: str, company: str) -> str:
	"""The revenue account every line on this invoice credits."""
	if explicit:
		return resolve_account(explicit, company)
	default = frappe.db.get_value("Company", company, "default_income_account")
	if default:
		return default
	matches = _leaf_accounts(company, "Income")
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ToolError(
			f"{company} has no default_income_account set and {len(matches)} leaf Income accounts: "
			f"{', '.join(sorted(matches))}. Pass income_account explicitly. Nothing was created."
		)
	raise ToolError(
		f"{company} has no default_income_account and no leaf Income account at all. Pass "
		"income_account explicitly, or create one with create_account. Nothing was created."
	)


def _resolve_deduction_account(explicit: str, company: str) -> str:
	"""Where a packer's withheld charges land.

	REFUSED RATHER THAN GUESSED. A settlement's deductions are packing, cold
	storage, marketing and commission — four different expense lines on a real
	chart of accounts, and picking whichever leaf Expense account sorts first
	would put a season of storage charges somewhere nobody looks. There is no
	company default for this in ERPNext, so an operation that wants deductions
	posted names the account once and passes it.
	"""
	if explicit:
		return resolve_account(explicit, company)
	if compat.has_field("Company", "default_expense_account"):
		default = frappe.db.get_value("Company", company, "default_expense_account")
		if default:
			return default
	raise ToolError(
		f"this settlement has deductions and no account to post them to. {company} has no "
		"default_expense_account, and picking a leaf Expense account by name would put a season of "
		"packing and storage charges somewhere nobody chose. Pass deduction_account, or pass "
		"include_deductions: false to invoice the GROSS amount and book the deductions separately. "
		"Nothing was created."
	)


def _leaf_accounts(company: str, root_type: str) -> list[str]:
	rows = frappe.db.get_all(
		"Account",
		filters={"company": company, "root_type": root_type},
		fields=compat.existing_fields("Account", ("name", "is_group", "disabled")),
		limit=500,
	)
	return [
		row["name"] for row in rows if not int(row.get("is_group") or 0) and not int(row.get("disabled") or 0)
	]


def _slug(value: str) -> str:
	return _NON_ALNUM.sub("-", str(value or "").upper()).strip("-")


def _fruit_item(variety: str, grade: str) -> tuple[str, str]:
	"""The shared non-stock Item for one variety and grade, created once.

	Returns `(item_code, how_it_was_resolved)`. A line with no variety on it
	lands on `FRUIT-SALES`, which is a real answer rather than a failure: plenty
	of settlements price a pool without naming what was in it.
	"""
	parts = [part for part in (_slug(variety), _slug(grade)) if part]
	item_code = f"{_FRUIT_ITEM_PREFIX}{'-'.join(parts)}" if parts else GENERIC_FRUIT_ITEM
	if frappe.db.exists(masters.ITEM, item_code):
		return item_code, "existing shared item"
	label = " ".join(part for part in (variety, grade) if part) or "Fruit Sales"
	created = masters.create_item(
		{
			"item_code": item_code,
			"item_name": label,
			"is_stock_item": False,
			"description": (
				f"Non-stock sales item for {label}. Created automatically by "
				"create_sales_invoice the first time a settlement priced this variety and grade. "
				"Shared by every settlement after that — never one Item per statement."
			),
		}
	)
	return created.data["name"], "created from variety and grade"


def _resolve_item(value: str, label: str) -> str:
	value = (value or "").strip()
	if not value:
		raise ToolError(f"{label} is required on every line.")
	if frappe.db.exists(masters.ITEM, value):
		return value
	match = frappe.db.get_value(masters.ITEM, {"item_name": value}, "name")
	if match:
		return str(match)
	raise ToolError(f"no Item called {value!r} on this site. Call list_items to find it.")


# ── reading an invoice back ──────────────────────────────────────────────────

_SI_HEADER_FIELDS = (
	"name",
	"customer",
	"customer_name",
	"company",
	"posting_date",
	"due_date",
	"debit_to",
	"currency",
	"status",
	"docstatus",
	"net_total",
	"total_taxes_and_charges",
	"grand_total",
	"rounded_total",
	"outstanding_amount",
	"is_return",
	"remarks",
	"owner",
)

_SI_LIST_FIELDS = (
	"name",
	"customer",
	"customer_name",
	"posting_date",
	"due_date",
	"grand_total",
	"outstanding_amount",
	"currency",
	"status",
	"docstatus",
	"company",
)


def _docstatus_label(docstatus) -> str:
	return {0: "draft", 1: "submitted", 2: "cancelled"}.get(int(docstatus or 0), "unknown")


def _si_items_out(doc) -> list[dict]:
	rows = []
	for row in doc.get("items") or []:
		rows.append(
			{
				"item_code": row.get("item_code"),
				"item_name": row.get("item_name"),
				"description": row.get("description"),
				"qty": float(row.get("qty") or 0),
				"uom": row.get("uom"),
				"rate": float(row.get("rate") or 0),
				"amount": float(row.get("amount") or 0),
				"income_account": row.get("income_account"),
				"cost_center": row.get("cost_center"),
			}
		)
	return rows


def _si_taxes_out(doc) -> list[dict]:
	rows = []
	for row in doc.get("taxes") or []:
		rows.append(
			{
				"charge_type": row.get("charge_type"),
				"account_head": row.get("account_head"),
				"description": row.get("description"),
				"rate": float(row.get("rate") or 0),
				"tax_amount": float(row.get("tax_amount") or 0),
			}
		)
	return rows


def _si_header_out(doc) -> dict:
	fields = compat.existing_fields(SALES_INVOICE, _SI_HEADER_FIELDS)
	data = {field: doc.get(field) for field in fields}
	for key in ("posting_date", "due_date"):
		if data.get(key) is not None:
			data[key] = str(data[key])
	for key in (
		"net_total",
		"total_taxes_and_charges",
		"grand_total",
		"rounded_total",
		"outstanding_amount",
	):
		if key in data:
			data[key] = float(data.get(key) or 0)
	data["docstatus"] = int(doc.get("docstatus") or 0)
	data["docstatus_label"] = _docstatus_label(doc.get("docstatus"))
	if compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD):
		data[SETTLEMENT_LINK_FIELD] = doc.get(SETTLEMENT_LINK_FIELD)
	return data


def _gl_entries_for(voucher_type: str, voucher_no: str) -> list[dict]:
	"""The GL rows a submit produced, read back rather than computed."""
	if not compat.doctype_exists("GL Entry"):
		return []
	filters = {"voucher_type": voucher_type, "voucher_no": voucher_no}
	if compat.has_field("GL Entry", "is_cancelled"):
		filters["is_cancelled"] = 0
	rows = frappe.db.get_all(
		"GL Entry",
		filters=filters,
		fields=compat.existing_fields(
			"GL Entry", ("account", "debit", "credit", "party_type", "party", "cost_center")
		),
		limit=200,
	)
	return [
		{
			**row,
			"debit": float(row.get("debit") or 0),
			"credit": float(row.get("credit") or 0),
		}
		for row in rows
	]


# ── 1. create_sales_invoice ──────────────────────────────────────────────────


def create_sales_invoice(args: dict) -> ToolResult:
	"""Create a DRAFT Sales Invoice, optionally populated from a settlement.

	Never submits. `submit_sales_invoice` is the separate tool with the separate
	switch, for the reason every create/submit pair in this app is split: an
	operator who enables "draft an invoice" has not thereby enabled "recognise
	revenue in the general ledger".

	FROM A SETTLEMENT, when `settlement_statement` is given: every priced line
	becomes an invoice line against a shared non-stock Item per variety and
	grade, and every deduction becomes a negative Actual charge against
	`deduction_account`. The settlement must be SUBMITTED — a draft statement's
	numbers can still change after the invoice is written against them — and it
	must not already have an invoice or a posted Journal Entry, because two
	revenue postings for one statement is a double count nobody finds until the
	year-end.

	MANUALLY, when `items` is given instead: ordinary line items, resolved and
	validated the way `purchasing.create_purchase_invoice` resolves its own.

	One or the other. Passing both is refused rather than merged.
	"""
	_require_sales_invoice()
	settlement_name = (
		as_str(args, "settlement_statement") or as_str(args, "settlement") or as_str(args, "statement")
	)
	raw_items = args.get("items")
	if settlement_name and raw_items:
		raise ToolError(
			"pass settlement_statement OR items, not both. A settlement already says what the "
			"lines are, and merging hand-written lines into them would produce an invoice whose "
			"total no longer matches the statement it claims to bill. Nothing was created."
		)
	if settlement_name:
		return _invoice_from_settlement(args, settlement_name)
	return _invoice_from_items(args)


def _invoice_from_items(args: dict) -> ToolResult:
	"""The standalone path: lines a caller wrote out."""
	company = resolve_company(as_str(args, "company"), required=True)
	customer = _customer(as_str(args, "customer"), required=True)
	posting_date = as_date(args, "posting_date") or frappe.utils.today()
	due_date = as_date(args, "due_date")
	if due_date and due_date < posting_date:
		raise ToolError(f"due_date {due_date} is before posting_date {posting_date}. Nothing was created.")

	debit_to = _resolve_receivable_account(as_str(args, "debit_to"), company)
	default_income = as_str(args, "income_account")
	cost_center = as_str(args, "cost_center")
	if cost_center:
		cost_center = resolve_cost_center(cost_center, company)

	lines, rate_adjustments = _manual_lines(args.get("items"), company, default_income, cost_center)
	taxes = _manual_taxes(args.get("taxes"), company)

	doc = _insert_invoice(
		company=company,
		customer=customer,
		posting_date=posting_date,
		due_date=due_date,
		debit_to=debit_to,
		lines=lines,
		taxes=taxes,
		remarks=as_str(args, "notes") or as_str(args, "remarks"),
		settlement=None,
	)

	data = _si_header_out(doc)
	data["items"] = _si_items_out(doc)
	data["taxes"] = _si_taxes_out(doc)
	data["settlement_statement"] = None
	# Reported even though it is empty on the ordinary invoice: a caller who stated an
	# amount needs to see that the rate moved to honour it, and a key that only exists
	# when something happened is a key nobody writes a check for.
	data["rate_adjustments"] = rate_adjustments
	data["next_step"] = (
		f"Sales Invoice {doc.name} is a DRAFT and has moved no balance. Submit it in ERPNext, or "
		"with submit_sales_invoice if this site enables that tool."
	)
	return ToolResult(
		data=data,
		summary=f"created draft Sales Invoice {doc.name} for {customer} ({company}): "
		f"{len(lines)} line(s), {data.get('grand_total')}"
		+ (
			f" — {len(rate_adjustments)} line(s) had their RATE adjusted to keep the amount "
			"you stated; see rate_adjustments"
			if rate_adjustments
			else ""
		),
		docstatus_delta="none → 0 (draft)",
	)


def _manual_lines(
	raw, company: str, default_income: str, default_cost_center: str
) -> tuple[list[dict], list[dict]]:
	"""`(lines, rate_adjustments)` from the caller's own `items[]`.

	The second half is the same contract `_settlement_lines_to_invoice_lines`
	returns and exists for the same reason: a stated amount only survives
	validate if the rate produces it, so the rate sometimes has to move, and a
	rate this app changed is not something to change silently.
	"""
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"items must be a non-empty list of objects, each with item_code, qty and rate, e.g. "
			'[{"item_code": "FRUIT-SALES", "qty": 1000, "rate": 0.62}] — or pass '
			"settlement_statement instead and let the packer's own lines fill the invoice."
		)
	income_account = _resolve_income_account(default_income, company)
	lines = []
	adjustments: list[dict] = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"items[{index}] must be an object, got {type(entry).__name__}")
		item_code = _resolve_item(
			as_str(entry, "item_code") or as_str(entry, "item"), f"items[{index}].item_code"
		)
		qty = as_float(entry.get("qty"), f"items[{index}].qty")
		rate = as_float(entry.get("rate"), f"items[{index}].rate")
		if qty <= 0:
			raise ToolError(f"items[{index}].qty must be positive, got {qty}. Nothing was created.")
		if rate < 0:
			raise ToolError(f"items[{index}].rate cannot be negative, got {rate}. Nothing was created.")
		# A STATED AMOUNT WINS, AND THE RATE IS WHAT MOVES TO KEEP IT.
		#
		# Two things had to be got right here, and only one of them is the coercion.
		#
		# First, `as_float` answers 0.0 for an absent value, for "" and for an explicit
		# 0 alike, so `as_float(...) or round(qty * rate, 2)` could not tell "no amount
		# stated" from "this line is comped" and invented a figure for the second. The
		# RAW value still knows the difference, so the branch is on that.
		#
		# Second — and this is what the coercion fix alone did NOT solve — ERPNext
		# recomputes `amount` from qty × rate on every validate, so whatever is written
		# here is overwritten. The proof is that a stated 615.50 was discarded exactly
		# like a stated 0: the `or` was never the mechanism. The only way a stated
		# amount survives is for the RATE to produce it, which is precisely what
		# `_settlement_lines_to_invoice_lines` already does for a packer's stated gross.
		# This path now does the same, and reports the adjustment rather than making it
		# quietly — a rate that differs from the one the caller sent is worth saying out
		# loud, even when it is the caller's own amount that asked for it.
		raw_amount = entry.get("amount")
		stated_rate = rate
		if raw_amount not in (None, ""):
			amount = as_float(raw_amount, f"items[{index}].amount")
			if amount < 0:
				raise ToolError(
					f"items[{index}].amount cannot be negative, got {amount}. Nothing was created."
				)
			# qty is proven positive above, so this cannot divide by zero.
			rate = round(amount / qty, 6)
		else:
			amount = round(qty * rate, 2)
		if abs(rate - stated_rate) > 0.0005:
			adjustments.append(
				{
					"line": index,
					"item_code": item_code,
					"qty": qty,
					"stated_rate": stated_rate,
					"rate": rate,
					"amount": amount,
					"note": (
						f"items[{index}] gave a rate of {stated_rate} and an amount of {amount}, "
						f"which {qty} × {stated_rate} does not produce. ERPNext recomputes a "
						f"line's amount from qty × rate, so the RATE was adjusted to {rate} to "
						f"keep the amount you stated; the rate you sent is kept here beside it."
					),
				}
			)
		line = {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"amount": amount,
			"income_account": (
				resolve_account(as_str(entry, "income_account"), company)
				if as_str(entry, "income_account")
				else income_account
			),
		}
		uom = as_str(entry, "uom")
		if uom:
			line["uom"] = uom
		description = as_str(entry, "description")
		if description:
			line["description"] = description
		line_cost_center = as_str(entry, "cost_center")
		if line_cost_center:
			line["cost_center"] = resolve_cost_center(line_cost_center, company)
		elif default_cost_center:
			line["cost_center"] = default_cost_center
		lines.append(line)
	return lines, adjustments


#: What a caller may put in `charge_type`. ERPNext has more; these are the three
#: that mean anything on a grower's invoice, and refusing the rest by name beats
#: passing through a value whose failure arrives from a controller three layers
#: down.
TAX_CHARGE_TYPES = ("Actual", "On Net Total", "On Previous Row Amount")


def _manual_taxes(raw, company: str) -> list[dict]:
	if raw in (None, "", []):
		return []
	if not isinstance(raw, list):
		raise ToolError(
			"taxes must be a list of objects, each with charge_type and account_head, e.g. "
			'[{"charge_type": "Actual", "account_head": "5100 - Packing", "tax_amount": -6240}]'
		)
	rows = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"taxes[{index}] must be an object, got {type(entry).__name__}")
		charge_type = as_str(entry, "charge_type") or "Actual"
		if charge_type not in TAX_CHARGE_TYPES:
			raise ToolError(
				f"taxes[{index}].charge_type must be one of: {', '.join(TAX_CHARGE_TYPES)}. "
				f"Got {charge_type!r}. Nothing was created."
			)
		account_head = resolve_account(as_str(entry, "account_head", required=True), company)
		row = {
			"charge_type": charge_type,
			"account_head": account_head,
			"description": as_str(entry, "description") or account_head,
			"rate": as_float(entry.get("rate"), f"taxes[{index}].rate"),
			"tax_amount": as_float(entry.get("tax_amount"), f"taxes[{index}].tax_amount"),
		}
		if charge_type == "Actual" and not row["tax_amount"]:
			raise ToolError(
				f"taxes[{index}] is charge_type Actual with no tax_amount. An Actual charge is a "
				"stated figure — a rate cannot produce it. Nothing was created."
			)
		rows.append(row)
	return rows


def _insert_invoice(
	*,
	company: str,
	customer: str,
	posting_date: str,
	due_date,
	debit_to: str,
	lines: list[dict],
	taxes: list[dict],
	remarks: str,
	settlement,
):
	"""The one place this module writes a Sales Invoice. Always a draft."""
	doc = frappe.new_doc(SALES_INVOICE)
	doc.company = company
	doc.customer = customer
	doc.posting_date = posting_date
	doc.debit_to = debit_to
	if due_date:
		doc.due_date = due_date
	if remarks:
		doc.remarks = remarks
	if settlement and ensure_settlement_link_field():
		doc.set(SETTLEMENT_LINK_FIELD, settlement)
	for line in lines:
		doc.append("items", line)
	for row in taxes:
		doc.append("taxes", row)
	doc.flags.ignore_permissions = True
	doc.insert()

	# Belt to the "never submits" braces, exactly as `insert_draft_journal_entry`
	# keeps: a site whose hooks submitted on insert would otherwise have this
	# tool report a draft it did not create.
	if int(doc.get("docstatus") or 0) != 0:
		raise ToolError(
			f"Sales Invoice {doc.name} was created with docstatus {doc.get('docstatus')}, but this "
			"tool only ever produces drafts. Refusing to report success — inspect the site's Sales "
			"Invoice hooks."
		)
	return doc


# ── settlement → invoice ─────────────────────────────────────────────────────


def _settlement_doc(name: str):
	compat.require_doctype(
		SETTLEMENT_STATEMENT,
		"It ships with erpnext_mcp — run `bench migrate` after installing v0.67.0.",
	)
	if not frappe.db.exists(SETTLEMENT_STATEMENT, name):
		raise ToolError(f"no Settlement Statement called {name!r} on this site.")
	return frappe.get_doc(SETTLEMENT_STATEMENT, name)


def _check_settlement_is_invoiceable(doc, *, action: str) -> None:
	"""The four refusals every settlement→ledger path shares.

	Named once and called from both `create_sales_invoice` and
	`post_settlement_to_gl`, so the two paths cannot drift into disagreeing about
	which settlements may be posted — which would let a caller who was refused by
	one route succeed through the other and book the revenue twice.
	"""
	name = doc.name
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 0:
		raise ToolError(
			f"settlement statement {name} is a DRAFT. A draft statement's weights, prices and "
			f"deductions can all still change after {action} is written against them, which makes "
			f"the posting a fiction. Submit it first with submit_settlement_statement. "
			f"Nothing was created."
		)
	if docstatus == 2:
		raise ToolError(
			f"settlement statement {name} is cancelled. A cancelled statement has paid for nothing. "
			f"Nothing was created."
		)
	if _settlement_can_link() and doc.get(SALES_INVOICE_LINK_FIELD):
		raise ToolError(
			f"settlement statement {name} is already invoiced as "
			f"{doc.get(SALES_INVOICE_LINK_FIELD)}. Two revenue postings for one statement is a "
			f"double count that nobody finds until the year end. Cancel that invoice first if it "
			f"was wrong. Nothing was created."
		)
	if doc.get("posted_journal_entry"):
		raise ToolError(
			f"settlement statement {name} is already posted to the ledger as Journal Entry "
			f"{doc.get('posted_journal_entry')}. The Sales Invoice path and the Journal Entry path "
			f"are alternatives, not a sequence — running both would book the proceeds twice. "
			f"Nothing was created."
		)
	if compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD):
		existing = frappe.db.get_all(
			SALES_INVOICE,
			filters={SETTLEMENT_LINK_FIELD: name, "docstatus": ("<", 2)},
			pluck="name",
			limit=5,
		)
		if existing:
			raise ToolError(
				f"settlement statement {name} already has Sales Invoice {existing[0]} pointing at "
				f"it. Nothing was created."
			)


def _settlement_lines_to_invoice_lines(
	doc, *, income_account: str, cost_center: str
) -> tuple[list[dict], list[dict]]:
	"""One invoice line per priced settlement line, and the report of what moved.

	Returns `(lines, report)`. The report is per-line and says which Item was
	used, how it was found, and — the part that matters — whether the rate had to
	be adjusted so the line's amount could stay equal to the packer's own stated
	gross amount. See the module docstring.
	"""
	rows = receipts._lines_out(doc)
	if not rows:
		raise ToolError(
			f"settlement statement {doc.name} has no priced line items, so there is nothing to "
			f"invoice. A settlement with weights but no prices is a packout report, not a bill. "
			f"Nothing was created."
		)

	lines = []
	report = []
	for index, row in enumerate(rows, start=1):
		qty = float(row.get("packed_weight") or 0)
		stated_rate = float(row.get("price_per_unit") or 0)
		amount = float(row.get("gross_amount") or 0) or round(qty * stated_rate, 2)
		if qty <= 0:
			raise ToolError(
				f"settlement line {index} ({row.get('variety') or 'unnamed variety'}) has a packed "
				f"weight of {qty}, which cannot become an invoice line — ERPNext prices a quantity, "
				f"and a quantity of nothing has no rate. Correct the settlement, or invoice it by "
				f"hand with items. Nothing was created."
			)
		if amount <= 0:
			raise ToolError(
				f"settlement line {index} ({row.get('variety') or 'unnamed variety'}) has a gross "
				f"amount of {amount}. An invoice line worth nothing is a line nobody meant to send. "
				f"Nothing was created."
			)
		rate = round(amount / qty, 6)
		item_code, item_resolved_by = _fruit_item(row.get("variety") or "", row.get("grade") or "")
		label = " ".join(part for part in (row.get("variety"), row.get("grade")) if part) or "Fruit"
		uom = row.get("price_uom") or doc.get("weight_uom")

		line = {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"amount": amount,
			"income_account": income_account,
			"description": f"{label} — {qty} {doc.get('weight_uom')} packed, "
			f"settlement {doc.get('statement_number') or doc.name}",
		}
		if uom:
			line["uom"] = uom
		if cost_center:
			line["cost_center"] = cost_center
		lines.append(line)

		adjusted = abs(rate - stated_rate) > 0.0005
		report.append(
			{
				"variety": row.get("variety"),
				"grade": row.get("grade"),
				"item_code": item_code,
				"item_resolved_by": item_resolved_by,
				"qty": qty,
				"uom": uom,
				"rate": rate,
				"stated_price_per_unit": stated_rate,
				"amount": amount,
				"rate_differs_from_statement": adjusted,
				**(
					{
						"rate_note": (
							f"the statement priced this line at {stated_rate} but stated a gross "
							f"amount of {amount}, which {qty} × {stated_rate} does not produce. The "
							f"RATE was adjusted so the invoice totals to what the packer said they "
							f"owed; the stated price is kept here beside it."
						)
					}
					if adjusted
					else {}
				),
			}
		)
	return lines, report


def _settlement_deductions_to_taxes(doc, deduction_account: str) -> list[dict]:
	"""Each deduction as a negative Actual charge. Rows, never one netted figure."""
	rows = []
	for row in receipts._deductions_out(doc):
		amount = float(row.get("amount") or 0)
		if not amount:
			continue
		label = row.get("deduction_type") or "Other"
		description = row.get("description")
		rows.append(
			{
				"charge_type": "Actual",
				"account_head": deduction_account,
				"description": f"{label}: {description}" if description else label,
				"rate": 0.0,
				"tax_amount": -abs(round(amount, 2)),
			}
		)
	return rows


def _invoice_from_settlement(args: dict, settlement_name: str) -> ToolResult:
	doc = _settlement_doc(settlement_name)
	_check_settlement_is_invoiceable(doc, action="an invoice")

	company = resolve_company(as_str(args, "company") or doc.get("company"), required=True)
	if company != doc.get("company"):
		raise ToolError(
			f"settlement statement {doc.name} belongs to company {doc.get('company')!r}, not "
			f"{company!r}. Nothing was created."
		)
	customer = doc.get("customer")
	posting_date = as_date(args, "posting_date") or str(doc.get("date"))
	due_date = as_date(args, "due_date") or frappe.utils.add_days(posting_date, DEFAULT_PAYMENT_TERMS_DAYS)
	due_date = str(due_date)
	if due_date < posting_date:
		raise ToolError(f"due_date {due_date} is before posting_date {posting_date}. Nothing was created.")

	debit_to = _resolve_receivable_account(as_str(args, "debit_to"), company)
	income_account = _resolve_income_account(as_str(args, "income_account"), company)
	cost_center = as_str(args, "cost_center")
	if cost_center:
		cost_center = resolve_cost_center(cost_center, company)

	lines, line_report = _settlement_lines_to_invoice_lines(
		doc, income_account=income_account, cost_center=cost_center
	)

	include_deductions = as_bool(args, "include_deductions", True)
	deductions = receipts._deductions_out(doc)
	taxes: list[dict] = []
	deduction_account = None
	if deductions and include_deductions:
		deduction_account = _resolve_deduction_account(as_str(args, "deduction_account"), company)
		taxes = _settlement_deductions_to_taxes(doc, deduction_account)

	doc_si = _insert_invoice(
		company=company,
		customer=customer,
		posting_date=posting_date,
		due_date=due_date,
		debit_to=debit_to,
		lines=lines,
		taxes=taxes,
		remarks=as_str(args, "notes")
		or f"Packer settlement {doc.get('statement_number') or doc.name} "
		f"({doc.get('period_start') or '?'} to {doc.get('period_end') or '?'})",
		settlement=doc.name,
	)

	forward_linked = _link_settlement_to_invoice(doc.name, doc_si.name)
	back_linked = bool(compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD))

	grand_total = float(doc_si.get("grand_total") or 0)
	net_proceeds = float(doc.get("net_proceeds") or 0)
	expected = (
		net_proceeds if (deductions and include_deductions) else float(doc.get("total_gross_revenue") or 0)
	)
	variance = round(grand_total - expected, 2)

	data = _si_header_out(doc_si)
	data["items"] = _si_items_out(doc_si)
	data["taxes"] = _si_taxes_out(doc_si)
	data["settlement_statement"] = doc.name
	data["settlement"] = {
		"name": doc.name,
		"statement_number": doc.get("statement_number"),
		"date": str(doc.get("date")),
		"total_gross_revenue": float(doc.get("total_gross_revenue") or 0),
		"total_deductions": float(doc.get("total_deductions") or 0),
		"net_proceeds": net_proceeds,
	}
	data["lines_from_settlement"] = line_report
	data["deductions_posted"] = len(taxes)
	data["deduction_account"] = deduction_account
	data["include_deductions"] = bool(include_deductions)
	data["links"] = {
		"settlement_points_at_invoice": forward_linked,
		"invoice_points_at_settlement": back_linked,
		"note": (
			"Both links are set where the site's schema allows. A false here means the column is "
			"missing, not that the documents are unrelated — run `bench migrate` to add the "
			"Settlement Statement's sales_invoice field, and the Sales Invoice's "
			"settlement_statement Custom Field is created on first use."
		),
	}
	data["total_check"] = {
		"invoice_grand_total": grand_total,
		"settlement_expected_total": round(expected, 2),
		"variance": variance,
		"basis": (
			"net_proceeds (gross revenue less deductions)"
			if (deductions and include_deductions)
			else "total_gross_revenue (deductions NOT on this invoice)"
		),
		"note": (
			"A non-zero variance means ERPNext's own totalling disagreed with the settlement — "
			"read the per-line rate adjustments in lines_from_settlement before submitting."
			if abs(variance) > 0.01
			else "The invoice totals to exactly what the settlement said."
		),
	}
	data["next_step"] = (
		f"Sales Invoice {doc_si.name} is a DRAFT and has moved no balance. Submit it in ERPNext, or "
		"with submit_sales_invoice if this site enables that tool."
	)
	if deductions and not include_deductions:
		data["warning"] = (
			f"this settlement has {len(deductions)} deduction(s) totalling "
			f"{float(doc.get('total_deductions') or 0)} which are NOT on this invoice, because "
			"include_deductions was false. The invoice bills the GROSS amount; book the deductions "
			"separately or the receivable will never clear."
		)

	return ToolResult(
		data=data,
		summary=f"created draft Sales Invoice {doc_si.name} for {customer} from settlement "
		f"{doc.name}: {len(lines)} line(s), {len(taxes)} deduction row(s), {grand_total}",
		docstatus_delta="none → 0 (draft)",
	)


def _link_settlement_to_invoice(settlement: str, invoice: str) -> bool:
	"""Point the settlement at its invoice. False when the column is not there.

	`frappe.db.set_value` rather than a document save, because the settlement is
	submitted by the time this runs — the same mechanism, and the same reason,
	as `receipts._match_tickets` stamping a submitted Scale Ticket.
	"""
	if not _settlement_can_link():
		return False
	try:
		frappe.db.set_value(
			SETTLEMENT_STATEMENT, settlement, SALES_INVOICE_LINK_FIELD, invoice, update_modified=False
		)
		return True
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: could not link a Settlement Statement to its Sales Invoice",
			message=compat.traceback_text(),
		)
		return False


# ── 2. get_sales_invoice ─────────────────────────────────────────────────────


def get_sales_invoice(args: dict) -> ToolResult:
	"""One Sales Invoice in full: items, taxes, payments, ageing, settlement."""
	_require_sales_invoice()
	name = as_str(args, "sales_invoice") or as_str(args, "invoice") or as_str(args, "name")
	if not name:
		raise ToolError("sales_invoice (the Sales Invoice docname) is required.")
	if not frappe.db.exists(SALES_INVOICE, name):
		raise ToolError(f"no Sales Invoice called {name!r} on this site.")
	doc = frappe.get_doc(SALES_INVOICE, name)
	as_of = as_date(args, "as_of") or frappe.utils.today()

	data = _si_header_out(doc)
	data["items"] = _si_items_out(doc)
	data["taxes"] = _si_taxes_out(doc)
	data["payments"] = _payments_against(name, doc.get("customer"), doc.get("company"))
	data["total_paid"] = round(sum(row["allocated_amount"] for row in data["payments"]), 2)

	days = _days_overdue(doc.get("due_date"), as_of)
	data["ageing"] = {
		"as_of": as_of,
		"days_overdue": days,
		"ageing_bucket": _bucket(days) if int(doc.get("docstatus") or 0) == 1 else None,
		"note": (
			"days_overdue = as_of - due_date; 'current' is not yet due. A draft invoice is not "
			"aged at all — nothing is owed until it is submitted."
		),
	}

	settlement = (
		doc.get(SETTLEMENT_LINK_FIELD) if compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD) else None
	)
	data["linked_settlement"] = _settlement_summary(settlement) if settlement else None

	return ToolResult(
		data=data,
		summary=f"Sales Invoice {name}: {doc.get('customer')}, {data.get('grand_total')} "
		f"({data.get('outstanding_amount')} outstanding) — {doc.get('status')}",
	)


def _payments_against(invoice: str, customer: str, company: str) -> list[dict]:
	"""Every Payment Entry allocated against this invoice, and for how much.

	READ OFF THE ALLOCATION AND NOT OFF THE LEDGER. A payment's GL rows do not
	say which invoice they settled — that lives in the `references` child table
	and nowhere else, which is the same point `purchasing.get_ap_aging` makes
	from the payables side.

	The customer's own Receive entries are walked and their reference rows
	filtered here, rather than querying the child table with a `reference_name`
	filter. It is the customer's payments either way, so the scope is the same;
	reading through the parent means the answer does not depend on a child table
	being separately queryable.

	Cancelled entries are excluded: a cancelled payment did not happen, and
	listing it beside the live ones would make `total_paid` disagree with the
	invoice's own outstanding amount.
	"""
	if not compat.doctype_exists(PAYMENT_ENTRY):
		return []
	filters: dict = {"payment_type": "Receive", "party_type": "Customer", "docstatus": ("<", 2)}
	if customer:
		filters["party"] = customer
	if company:
		filters["company"] = company
	headers = frappe.db.get_all(
		PAYMENT_ENTRY,
		filters=filters,
		fields=compat.existing_fields(
			PAYMENT_ENTRY,
			("name", "posting_date", "paid_amount", "reference_no", "mode_of_payment", "docstatus"),
		),
		order_by="posting_date asc, creation asc",
		limit=500,
	)
	payments = []
	for header in headers:
		doc = frappe.get_doc(PAYMENT_ENTRY, header["name"])
		allocated = 0.0
		matched = False
		for row in doc.get("references") or []:
			get = row.get if isinstance(row, dict) else (lambda k, r=row: getattr(r, k, None))
			if get("reference_doctype") == SALES_INVOICE and get("reference_name") == invoice:
				allocated += float(get("allocated_amount") or 0)
				matched = True
		if not matched:
			continue
		payments.append(
			{
				"payment_entry": header["name"],
				"posting_date": str(header.get("posting_date") or ""),
				"paid_amount": float(header.get("paid_amount") or 0),
				"allocated_amount": round(allocated, 2),
				"reference_no": header.get("reference_no"),
				"mode_of_payment": header.get("mode_of_payment"),
				"docstatus": int(header.get("docstatus") or 0),
				"docstatus_label": _docstatus_label(header.get("docstatus")),
			}
		)
	return payments


def _settlement_summary(name: str) -> dict:
	row = (
		frappe.db.get_value(
			SETTLEMENT_STATEMENT,
			name,
			[
				"name",
				"statement_number",
				"date",
				"customer",
				"period_start",
				"period_end",
				"gross_delivered_weight",
				"packed_weight",
				"packout_pct",
				"weight_uom",
				"total_gross_revenue",
				"total_deductions",
				"net_proceeds",
				"status",
			],
			as_dict=True,
		)
		or {}
	)
	return receipts._row_out(row) if row else {}


# ── 3. list_sales_invoices ───────────────────────────────────────────────────


def list_sales_invoices(args: dict) -> ToolResult:
	"""Sales Invoice headers by customer, company, status and posting date."""
	_require_sales_invoice()
	customer = as_str(args, "customer")
	if customer:
		customer = _customer(customer)
	company = resolve_company(as_str(args, "company"))
	status = as_str(args, "status")
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	outstanding_only = as_bool(args, "outstanding_only", False)
	settlement = as_str(args, "settlement_statement")
	limit = as_limit(args)

	filters: dict = {}
	if customer:
		filters["customer"] = customer
	if company:
		filters["company"] = company
	if status:
		filters["status"] = status
	if outstanding_only:
		filters["outstanding_amount"] = (">", 0.005)
	if settlement:
		if not compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD):
			raise ToolError(
				"this site's Sales Invoice has no settlement_statement field yet, so it cannot be "
				"filtered on. It is created the first time a settlement is invoiced."
			)
		filters[SETTLEMENT_LINK_FIELD] = settlement
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["posting_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["posting_date"] = (">=", from_date)
	elif to_date:
		filters["posting_date"] = ("<=", to_date)

	fields = list(compat.existing_fields(SALES_INVOICE, _SI_LIST_FIELDS))
	if compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD):
		fields.append(SETTLEMENT_LINK_FIELD)
	rows = frappe.db.get_all(
		SALES_INVOICE,
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit=limit,
	)

	by_status: dict = {}
	total_grand = 0.0
	total_outstanding = 0.0
	for row in rows:
		row["docstatus_label"] = _docstatus_label(row.get("docstatus"))
		for key in ("posting_date", "due_date"):
			if row.get(key) is not None:
				row[key] = str(row[key])
		row["grand_total"] = float(row.get("grand_total") or 0)
		row["outstanding_amount"] = float(row.get("outstanding_amount") or 0)
		total_grand += row["grand_total"]
		total_outstanding += row["outstanding_amount"]
		key = row.get("status") or row["docstatus_label"]
		by_status[key] = by_status.get(key, 0) + 1

	data = {
		"invoices": rows,
		"count": len(rows),
		"limit": limit,
		"truncated": len(rows) == limit,
		"total_grand": round(total_grand, 2),
		"total_outstanding": round(total_outstanding, 2),
		"by_status": by_status,
		"filters": {
			"customer": customer or None,
			"company": company,
			"status": status or None,
			"from_date": from_date,
			"to_date": to_date,
			"outstanding_only": bool(outstanding_only),
			"settlement_statement": settlement or None,
		},
		"note": (
			"total_grand and total_outstanding sum the rows RETURNED, which is a partial figure "
			"when truncated is true. Draft and cancelled invoices are included unless a status "
			"filter excludes them — a draft owes nothing, so it inflates total_grand and not "
			"total_outstanding."
		),
	}
	return ToolResult(
		data=data,
		summary=f"{len(rows)} Sales Invoice(s), {round(total_grand, 2)} billed, "
		f"{round(total_outstanding, 2)} outstanding",
	)


# ── 4. submit_sales_invoice ──────────────────────────────────────────────────


def submit_sales_invoice(args: dict) -> ToolResult:
	"""Submit a DRAFT Sales Invoice (docstatus 0 → 1). Creates the GL entries.

	This is the tool that recognises revenue. On a real site `doc.submit()` runs
	ERPNext's own Sales Invoice controller, which debits `debit_to` for the total,
	credits every line's income account, and posts each charge row — exactly what
	a human submitting the same invoice in the Desk would trigger, and nothing
	this tool computes itself. The GL rows are READ BACK rather than derived, so
	what is reported is what the ledger actually says.
	"""
	_require_sales_invoice()
	name = as_str(args, "sales_invoice") or as_str(args, "invoice") or as_str(args, "name")
	if not name:
		raise ToolError("sales_invoice (the Sales Invoice docname) is required.")
	if not frappe.db.exists(SALES_INVOICE, name):
		raise ToolError(f"no Sales Invoice called {name!r} on this site.")
	doc = frappe.get_doc(SALES_INVOICE, name)
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 1:
		raise ToolError(f"Sales Invoice {name} is already submitted. Nothing was changed.")
	if docstatus == 2:
		raise ToolError(
			f"Sales Invoice {name} is cancelled. A cancelled invoice is amended into a new one "
			f"rather than resubmitted. Nothing was changed."
		)

	doc.flags.ignore_permissions = True
	doc.submit()
	doc.reload()

	gl_entries = _gl_entries_for(SALES_INVOICE, name)
	data = _si_header_out(doc)
	data["gl_entries"] = gl_entries
	data["gl_entries_created"] = len(gl_entries)
	data["gl_totals"] = {
		"debit": round(sum(row["debit"] for row in gl_entries), 2),
		"credit": round(sum(row["credit"] for row in gl_entries), 2),
	}
	settlement = (
		doc.get(SETTLEMENT_LINK_FIELD) if compat.has_field(SALES_INVOICE, SETTLEMENT_LINK_FIELD) else None
	)
	data["settlement_statement"] = settlement
	data["note"] = (
		"gl_entries are read back from GL Entry after ERPNext's own controller wrote them; this "
		"tool computes no posting of its own. An empty list on a site with a GL Entry doctype "
		"means the submit posted nothing, which is worth investigating before trusting the invoice."
	)
	return ToolResult(
		data=data,
		summary=f"submitted Sales Invoice {name} ({doc.get('customer')}, "
		f"{data.get('grand_total')}, {len(gl_entries)} GL row(s))",
		docstatus_delta="0 → 1 (submitted)",
	)


# ── 5. create_sales_invoice_from_settlement ──────────────────────────────────


def create_sales_invoice_from_settlement(args: dict) -> ToolResult:
	"""One step: a submitted Settlement Statement → a draft Sales Invoice.

	A thin wrapper over `create_sales_invoice` with `settlement_statement`
	pre-filled — thin ON PURPOSE, so there is exactly one implementation of
	"what does a settlement's line become". The convenience is the argument list,
	not a second code path: a defaulted posting date (the settlement's own date),
	a defaulted due date (thirty days after it), and a name that says what the
	tool is for so a model reaches for it instead of assembling the arguments.
	"""
	settlement = (
		as_str(args, "settlement_statement")
		or as_str(args, "settlement")
		or as_str(args, "statement")
		or as_str(args, "name")
	)
	if not settlement:
		raise ToolError("settlement_statement (the Settlement Statement docname) is required.")
	forwarded = {key: value for key, value in args.items() if key not in ("settlement", "statement", "name")}
	forwarded["settlement_statement"] = settlement
	return create_sales_invoice(forwarded)


# ── 6. receive_payment ───────────────────────────────────────────────────────


def receive_payment(args: dict) -> ToolResult:
	"""Record money received from a customer as a DRAFT Payment Entry.

	`payment_type` is always "Receive" and `party_type` always "Customer" — the
	AR side only, deliberately. `purchasing.create_payment_entry` is the Pay /
	Supplier mirror, and keeping them apart means a tool that can collect money
	cannot be talked into spending it.

	ALLOCATION, IN TWO MODES. Pass `invoices` to say exactly which invoices this
	cheque settles and for how much. Pass nothing and the payment is allocated
	OLDEST FIRST across the customer's submitted, outstanding invoices — by due
	date, then posting date — which is what a remittance with no advice attached
	means everywhere in accounts receivable. Whichever ran is reported as
	`allocation_method`, and every allocated invoice is listed with the amount, so
	an automatic allocation is auditable rather than merely convenient.

	MONEY LEFT OVER IS LEFT OVER. A payment larger than everything outstanding is
	not refused and is not spread onto invoices that do not exist: the remainder
	comes back as `unallocated_amount`, which is a real on-account balance and the
	honest answer to "the packer overpaid".

	ALWAYS A DRAFT. Submitting is what actually moves the bank balance and clears
	the receivable; that is `submit_payment_entry`, with its own switch.
	"""
	compat.require_doctype(PAYMENT_ENTRY, _ERPNEXT_HINT)
	_require_sales_invoice()
	company = resolve_company(as_str(args, "company"), required=True)
	customer = _customer(as_str(args, "customer"), required=True)
	posting_date = as_date(args, "posting_date") or frappe.utils.today()
	paid_amount = as_float(args.get("paid_amount"), "paid_amount")
	if paid_amount <= 0:
		raise ToolError("paid_amount must be positive. Nothing was created.")

	paid_from = _resolve_receivable_account(as_str(args, "paid_from") or as_str(args, "debit_to"), company)
	paid_to = _resolve_cash_account(as_str(args, "paid_to"), company)

	raw_invoices = args.get("invoices") or args.get("references")
	if raw_invoices in (None, "", []):
		references, method = _auto_allocate(customer, company, paid_amount, posting_date)
	else:
		references = _explicit_allocation(raw_invoices, customer, company)
		method = "explicit"

	total_allocated = round(sum(row["allocated_amount"] for row in references), 2)
	if total_allocated - paid_amount > 0.005:
		raise ToolError(
			f"the invoices allocate {total_allocated} but paid_amount is {paid_amount}. Nothing was created."
		)

	doc = frappe.new_doc(PAYMENT_ENTRY)
	doc.payment_type = "Receive"
	doc.party_type = "Customer"
	doc.party = customer
	doc.company = company
	doc.posting_date = posting_date
	doc.paid_amount = paid_amount
	doc.received_amount = paid_amount
	doc.paid_from = paid_from
	doc.paid_to = paid_to
	reference_no = as_str(args, "reference_no")
	if reference_no:
		doc.reference_no = reference_no
		doc.reference_date = as_date(args, "reference_date") or posting_date
	mode_of_payment = as_str(args, "mode_of_payment")
	if mode_of_payment:
		doc.mode_of_payment = mode_of_payment
	for row in references:
		doc.append("references", row)
	doc.flags.ignore_permissions = True
	doc.insert()

	if int(doc.get("docstatus") or 0) != 0:
		raise ToolError(
			f"Payment Entry {doc.name} was created with docstatus {doc.get('docstatus')}, but this "
			"tool only ever produces drafts. Refusing to report success."
		)

	data = {
		"payment_entry": doc.name,
		"name": doc.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"customer": customer,
		"posting_date": str(posting_date),
		"paid_amount": paid_amount,
		"paid_from": paid_from,
		"paid_to": paid_to,
		"reference_no": reference_no or None,
		"mode_of_payment": mode_of_payment or None,
		"allocation_method": method,
		"allocated_total": total_allocated,
		"unallocated_amount": round(paid_amount - total_allocated, 2),
		"allocated_invoices": [
			{
				"sales_invoice": row["reference_name"],
				"due_date": str(row["due_date"]) if row.get("due_date") else None,
				"outstanding_before": row["outstanding_amount"],
				"allocated_amount": row["allocated_amount"],
			}
			for row in references
		],
		"next_step": (
			f"Payment Entry {doc.name} is a DRAFT: no bank balance has moved and no invoice's "
			"outstanding amount has changed yet. Submit it in ERPNext, or with submit_payment_entry "
			"if this site enables that tool."
		),
		"note": (
			"allocation_method says how the invoices were chosen. 'oldest first' walks the "
			"customer's submitted, outstanding invoices by due date and then posting date, which "
			"is what a remittance with no advice attached means. unallocated_amount is a real "
			"on-account balance, not an error."
		),
	}
	return ToolResult(
		data=data,
		summary=f"created draft Payment Entry {doc.name}: {paid_amount} from {customer}, "
		f"{len(references)} invoice(s) allocated {total_allocated}",
		docstatus_delta="none → 0 (draft)",
	)


def _resolve_cash_account(explicit: str, company: str) -> str:
	if explicit:
		return resolve_account(explicit, company)
	for field in ("default_bank_account", "default_cash_account"):
		default = frappe.db.get_value("Company", company, field)
		if default:
			return default
	raise ToolError(
		f"{company} has no default_bank_account or default_cash_account set. Pass paid_to "
		"explicitly — the account the money landed in is not something this tool will guess. "
		"Nothing was created."
	)


def _open_invoices(customer: str, company: str) -> list[dict]:
	"""This customer's submitted, still-owed invoices, oldest first."""
	rows = frappe.db.get_all(
		SALES_INVOICE,
		filters={
			"customer": customer,
			"company": company,
			"docstatus": 1,
			"outstanding_amount": (">", 0.005),
		},
		fields=compat.existing_fields(
			SALES_INVOICE, ("name", "due_date", "posting_date", "outstanding_amount", "grand_total")
		),
		limit=500,
	)

	# Sorted here rather than in SQL: an invoice with no due date has to sort
	# beside its posting date rather than to the front (a NULL sorts first in
	# most databases), because an invoice nobody put terms on is not thereby the
	# most overdue thing on the ledger.
	def key(row):
		due = str(row.get("due_date") or row.get("posting_date") or "")
		return (due, str(row.get("posting_date") or ""), str(row.get("name") or ""))

	return sorted(rows, key=key)


def _auto_allocate(customer: str, company: str, paid_amount: float, posting_date: str):
	remaining = round(paid_amount, 2)
	references = []
	for row in _open_invoices(customer, company):
		if remaining <= 0.005:
			break
		outstanding = round(float(row.get("outstanding_amount") or 0), 2)
		allocated = min(outstanding, remaining)
		if allocated <= 0.005:
			continue
		references.append(
			{
				"reference_doctype": SALES_INVOICE,
				"reference_name": row["name"],
				"due_date": row.get("due_date"),
				"total_amount": float(row.get("grand_total") or outstanding),
				"outstanding_amount": outstanding,
				"allocated_amount": round(allocated, 2),
			}
		)
		remaining = round(remaining - allocated, 2)
	return references, "oldest first"


def _explicit_allocation(raw, customer: str, company: str) -> list[dict]:
	if not isinstance(raw, list):
		raise ToolError(
			"invoices must be a list of objects, each with sales_invoice and allocated_amount, "
			'e.g. [{"sales_invoice": "ACC-SINV-2026-00001", "allocated_amount": 5000}]'
		)
	references = []
	seen = set()
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"invoices[{index}] must be an object, got {type(entry).__name__}")
		name = as_str(entry, "sales_invoice") or as_str(entry, "reference_name") or as_str(entry, "invoice")
		if not name:
			raise ToolError(f"invoices[{index}].sales_invoice is required. Nothing was created.")
		if name in seen:
			raise ToolError(
				f"invoices[{index}]: {name} appears twice. Allocate the whole amount on one row. "
				f"Nothing was created."
			)
		seen.add(name)
		if not frappe.db.exists(SALES_INVOICE, name):
			raise ToolError(f"invoices[{index}]: no Sales Invoice called {name!r}. Nothing was created.")
		invoice = frappe.db.get_value(
			SALES_INVOICE,
			name,
			["customer", "company", "docstatus", "outstanding_amount", "due_date", "grand_total"],
			as_dict=True,
		)
		if int(invoice.get("docstatus") or 0) != 1:
			raise ToolError(
				f"invoices[{index}]: {name} is not submitted, so nothing is owed on it yet. "
				f"Nothing was created."
			)
		if invoice.get("customer") != customer:
			raise ToolError(
				f"invoices[{index}]: {name} is billed to {invoice.get('customer')!r}, not "
				f"{customer!r}. Nothing was created."
			)
		if invoice.get("company") != company:
			raise ToolError(
				f"invoices[{index}]: {name} belongs to company {invoice.get('company')!r}, not "
				f"{company!r}. Nothing was created."
			)
		outstanding = round(float(invoice.get("outstanding_amount") or 0), 2)
		allocated = as_float(entry.get("allocated_amount"), f"invoices[{index}].allocated_amount")
		if not allocated:
			allocated = outstanding
		if allocated <= 0:
			raise ToolError(
				f"invoices[{index}].allocated_amount must be positive, got {allocated}. Nothing was created."
			)
		if allocated - outstanding > 0.005:
			raise ToolError(
				f"invoices[{index}]: allocated_amount {allocated} exceeds {name}'s outstanding "
				f"amount {outstanding}. Nothing was created."
			)
		references.append(
			{
				"reference_doctype": SALES_INVOICE,
				"reference_name": name,
				"due_date": invoice.get("due_date"),
				"total_amount": float(invoice.get("grand_total") or outstanding),
				"outstanding_amount": outstanding,
				"allocated_amount": round(allocated, 2),
			}
		)
	return references


# ── 7. get_settlement_shrink ─────────────────────────────────────────────────


def get_settlement_shrink(args: dict) -> ToolResult:
	"""Delivered against packed against culled, for one settlement.

	SHRINK IS WHAT DID NOT COME OUT THE OTHER END: delivered minus packed. Cull
	is the part of it the packer reported as culled, and the rest — juice, storage
	loss, fruit not yet run, and any weight the packer simply never accounted for
	— is `unexplained_weight`. That third number is reported SEPARATELY rather
	than folded into the cull, because a cull percentage is what a grower
	renegotiates a contract over and an unexplained percentage is what a grower
	asks a question about. They are not the same finding.

	PER VARIETY AND GRADE where the evidence allows it. Packed weight per variety
	comes from the settlement's own priced lines; delivered weight per variety
	comes from the grower's matched Scale Tickets. A variety that appears on one
	side and not the other is REPORTED AS SUCH rather than dropped — the packer
	grading a load differently from the ticket is a real thing that happens, and
	it is the kind of thing this pair of registers exists to make visible.
	"""
	row = _settlement_row(args)
	doc = _settlement_doc(row["name"])

	delivered = float(doc.get("gross_delivered_weight") or 0)
	packed = float(doc.get("packed_weight") or 0)
	cull = float(doc.get("cull_weight") or 0)
	uom = doc.get("weight_uom")

	shrink = round(delivered - packed, 3)
	unexplained = round(shrink - cull, 3)
	tickets = receipts._matched_tickets(doc.name)

	data = {
		"settlement_statement": doc.name,
		"statement_number": doc.get("statement_number"),
		"customer": doc.get("customer"),
		"company": doc.get("company"),
		"date": str(doc.get("date")),
		"period_start": str(doc.get("period_start")) if doc.get("period_start") else None,
		"period_end": str(doc.get("period_end")) if doc.get("period_end") else None,
		"weight_uom": uom,
		"gross_delivered_weight": delivered,
		"packed_weight": packed,
		"cull_weight": cull,
		"shrink_weight": shrink,
		"unexplained_weight": unexplained,
		"packout_pct": _pct(packed, delivered),
		"shrink_pct": _pct(shrink, delivered),
		"cull_pct": _pct(cull, delivered),
		"unexplained_pct": _pct(unexplained, delivered),
		"by_variety_grade": _shrink_by_variety(doc, tickets),
		"ticket_reconciliation": receipts._reconciliation(doc, tickets),
		"note": (
			"packout_pct is packed over DELIVERED, and shrink_pct is its complement — the two add "
			"to 100 by construction. cull_pct is the part of the shrink the packer reported as "
			"culled; unexplained_pct is the rest, and it is the number to ask about. A negative "
			"unexplained weight means the packer's own cull exceeds delivered minus packed, which "
			"is arithmetically impossible on one pool and usually means the statement covers fruit "
			"delivered before this period. Zero delivered weight makes every percentage zero and "
			"none of them meaningful."
		),
	}
	if delivered <= 0:
		data["warning"] = (
			"this settlement states no gross delivered weight, so every percentage here is zero by "
			"convention rather than by measurement. The matched Scale Tickets in "
			"ticket_reconciliation are the only delivered figure available."
		)
	return ToolResult(
		data=data,
		summary=f"Settlement {doc.name}: {data['packout_pct']}% packout, {data['shrink_pct']}% "
		f"shrink ({data['cull_pct']}% culled, {data['unexplained_pct']}% unexplained)",
	)


def _pct(part, whole) -> float:
	whole = float(whole or 0)
	if not whole:
		return 0.0
	return round(float(part or 0) / whole * 100, 2)


def _shrink_by_variety(doc, tickets: list) -> list[dict]:
	"""Packed per variety from the lines, delivered per variety from the tickets.

	Tickets in a weight unit other than the statement's are EXCLUDED rather than
	converted, exactly as `receipts._reconciliation` excludes them, and for the
	same reason: there is no bins-to-kilos conversion this app knows, and a
	fabricated one would put a fabricated shrink on the answer.
	"""
	statement_uom = doc.get("weight_uom")
	groups: dict = {}

	def entry(variety, grade):
		key = (str(variety or ""), str(grade or ""))
		return groups.setdefault(
			key,
			{
				"variety": variety or None,
				"grade": grade or None,
				"delivered_weight": 0.0,
				"packed_weight": 0.0,
				"ticket_count": 0,
				"seen_on": [],
			},
		)

	for line in receipts._lines_out(doc):
		row = entry(line.get("variety"), line.get("grade"))
		row["packed_weight"] = round(row["packed_weight"] + float(line.get("packed_weight") or 0), 3)
		if "settlement lines" not in row["seen_on"]:
			row["seen_on"].append("settlement lines")

	excluded = 0
	for ticket in tickets:
		if ticket.get("weight_uom") != statement_uom:
			excluded += 1
			continue
		row = entry(ticket.get("variety"), ticket.get("grade"))
		row["delivered_weight"] = round(row["delivered_weight"] + float(ticket.get("net_weight") or 0), 3)
		row["ticket_count"] += 1
		if "scale tickets" not in row["seen_on"]:
			row["seen_on"].append("scale tickets")

	out = []
	for row in groups.values():
		delivered = row["delivered_weight"]
		packed = row["packed_weight"]
		both = len(row["seen_on"]) == 2
		out.append(
			{
				**row,
				"weight_uom": statement_uom,
				"shrink_weight": round(delivered - packed, 3) if both else None,
				"packout_pct": _pct(packed, delivered) if both else None,
				"shrink_pct": _pct(delivered - packed, delivered) if both else None,
				"comparable": both,
				"note": (
					None
					if both
					else (
						"this variety and grade appears only on the "
						f"{row['seen_on'][0]}, so there is nothing to compare it with. A packer who "
						"regrades a load produces exactly this: the ticket says one grade and the "
						"statement prices another."
					)
				),
			}
		)
	out.sort(key=lambda r: (-(r["packed_weight"] or 0), -(r["delivered_weight"] or 0), str(r["variety"])))
	if excluded:
		out.append(
			{
				"variety": None,
				"grade": None,
				"comparable": False,
				"tickets_in_other_units_excluded": excluded,
				"note": (
					f"{excluded} matched Scale Ticket(s) are in a weight unit other than the "
					f"statement's {statement_uom} and were excluded rather than converted."
				),
			}
		)
	return out


# ── 8. get_packout_summary ───────────────────────────────────────────────────

#: What each grouping can honestly report, and where each number came from. The
#: interesting entries are `variety`/`grade` and `field`, where one side of the
#: ratio is not attributable and this says so rather than allocating it.
_GROUP_BASIS = {
	"customer": (
		"Every figure comes straight off the settlement headers, so delivered, packed and culled "
		"are all exact."
	),
	"month": (
		"Every figure comes straight off the settlement headers, grouped by the statement date's "
		"month, so delivered, packed and culled are all exact."
	),
	"variety": (
		"packed comes from the settlements' own priced lines; delivered comes from the Scale "
		"Tickets matched to those settlements. culled is null — a packer states ONE cull weight "
		"per statement and it cannot be split across varieties without inventing the split."
	),
	"grade": (
		"packed comes from the settlements' own priced lines; delivered comes from the Scale "
		"Tickets matched to those settlements. culled is null — a packer states ONE cull weight "
		"per statement and it cannot be split across grades without inventing the split."
	),
	"field": (
		"delivered comes from the Scale Tickets, which are the only records that name a field. "
		"packed and culled are attributed only where EVERY ticket on a settlement names the same "
		"field, because a statement that pools two fields cannot be split back into them; the "
		"rest is counted in `unattributed` rather than allocated."
	),
}


def get_packout_summary(args: dict) -> ToolResult:
	"""Packout across settlements for a period, grouped the way you ask.

	PACKOUT IS PACKED OVER DELIVERED. The overall figures come from the
	settlement headers and are always exact. The per-group figures come from
	whatever evidence actually attributes to that group — read `basis` before
	reading the groups, because for two of the five groupings one side of the
	ratio genuinely cannot be attributed and is returned as null rather than
	allocated pro-rata. A pro-rata packout by field is a made-up number that
	looks exactly like a measured one.
	"""
	compat.require_doctype(
		SETTLEMENT_STATEMENT,
		"It ships with erpnext_mcp — run `bench migrate` after installing v0.67.0.",
	)
	group_by = (as_str(args, "group_by") or "variety").strip().lower()
	if group_by not in PACKOUT_GROUP_BY:
		raise ToolError(f"group_by must be one of: {', '.join(PACKOUT_GROUP_BY)}. Got {group_by!r}.")

	filters: dict = {"docstatus": 1}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	customer = as_str(args, "customer")
	if customer:
		filters["customer"] = _customer(customer)
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["date"] = (">=", from_date)
	elif to_date:
		filters["date"] = ("<=", to_date)

	settlements = frappe.db.get_all(
		SETTLEMENT_STATEMENT,
		filters=filters,
		fields=[
			"name",
			"statement_number",
			"date",
			"customer",
			"company",
			"gross_delivered_weight",
			"packed_weight",
			"cull_weight",
			"weight_uom",
		],
		order_by="date asc",
		limit_page_length=500,
	)

	total_delivered = round(sum(float(s.get("gross_delivered_weight") or 0) for s in settlements), 3)
	total_packed = round(sum(float(s.get("packed_weight") or 0) for s in settlements), 3)
	total_culled = round(sum(float(s.get("cull_weight") or 0) for s in settlements), 3)
	uoms: dict = {}
	for s in settlements:
		key = s.get("weight_uom") or "<none>"
		uoms[key] = uoms.get(key, 0) + 1

	if group_by in ("customer", "month"):
		groups, unattributed = _groups_from_headers(settlements, group_by)
	elif group_by in ("variety", "grade"):
		groups, unattributed = _groups_from_lines_and_tickets(settlements, group_by)
	else:
		groups, unattributed = _groups_by_field(settlements)

	data = {
		"summary": {
			"total_delivered": total_delivered,
			"total_packed": total_packed,
			"total_culled": total_culled,
			"overall_packout_pct": _pct(total_packed, total_delivered),
			"overall_shrink_pct": _pct(total_delivered - total_packed, total_delivered),
			"overall_cull_pct": _pct(total_culled, total_delivered),
			"settlement_count": len(settlements),
		},
		"groups": groups,
		"group_by": group_by,
		"unattributed": unattributed,
		"period": {"from_date": from_date, "to_date": to_date},
		"filters": {"company": filters.get("company") or None, "customer": filters.get("customer") or None},
		"by_weight_uom": uoms,
		"basis": _GROUP_BASIS[group_by],
		"note": (
			"packout = packed / delivered as a percentage; shrink is its complement. Only SUBMITTED "
			"settlements are counted — a draft statement is a document somebody is still typing. "
			"A by_weight_uom naming more than one unit makes every total here MEANINGLESS: kilos "
			"and bins do not add, and this tool will not convert them."
		),
	}
	if len(uoms) > 1:
		data["warning"] = (
			f"these settlements are stated in {len(uoms)} different weight units "
			f"({', '.join(sorted(uoms))}). The totals add numbers that are not comparable. Filter "
			"to one customer, or to a period where the unit was consistent."
		)
	return ToolResult(
		data=data,
		summary=f"{len(settlements)} settlement(s), {data['summary']['overall_packout_pct']}% overall "
		f"packout across {len(groups)} {group_by} group(s)",
	)


def _new_group(key, *, packed_known: bool = True, culled_known: bool = True) -> dict:
	"""One group, carrying whether each side of the ratio is knowable for it.

	The two flags are PER GROUP rather than per grouping, because the field
	grouping is per group: a settlement whose tickets all name one field
	attributes its packed weight exactly, and a settlement pooling two fields
	attributes it to neither. A group that got no attributed settlement must
	come back with `packed: null`, not with a zero — a zero packout reads as
	"nothing packed out of that field", which is a finding, and this is the
	absence of one.
	"""
	return {
		"group_key": key,
		"delivered": 0.0,
		"packed": 0.0,
		"culled": 0.0,
		"settlement_count": 0,
		"settlements": [],
		"_packed_known": packed_known,
		"_culled_known": culled_known,
	}


def _finish_groups(groups: dict) -> list[dict]:
	out = []
	for row in groups.values():
		packed_known = row.pop("_packed_known", True)
		culled_known = row.pop("_culled_known", True)
		delivered = round(row["delivered"], 3)
		packed = round(row["packed"], 3) if packed_known else None
		row["delivered"] = delivered
		row["packed"] = packed
		row["culled"] = round(row["culled"], 3) if culled_known else None
		row["settlement_count"] = len(set(row["settlements"]))
		row["packout_pct"] = _pct(packed, delivered) if (packed is not None and delivered) else None
		row["shrink_pct"] = (
			_pct(delivered - packed, delivered) if (packed is not None and delivered) else None
		)
		row["cull_pct"] = _pct(row["culled"], delivered) if (culled_known and delivered) else None
		row.pop("settlements", None)
		out.append(row)
	out.sort(key=lambda r: (-(r["delivered"] or 0), -(r["packed"] or 0), str(r["group_key"])))
	return out


def _groups_from_headers(settlements: list, group_by: str):
	groups: dict = {}
	for s in settlements:
		key = s.get("customer") if group_by == "customer" else str(s.get("date") or "")[:7]
		row = groups.setdefault(key or "<none>", _new_group(key or "<none>"))
		row["delivered"] += float(s.get("gross_delivered_weight") or 0)
		row["packed"] += float(s.get("packed_weight") or 0)
		row["culled"] += float(s.get("cull_weight") or 0)
		row["settlements"].append(s["name"])
	return _finish_groups(groups), None


def _settlement_tickets(names: list) -> list[dict]:
	if not names:
		return []
	return frappe.db.get_all(
		SCALE_TICKET,
		filters={"settlement": ("in", names)},
		fields=["name", "settlement", "variety", "grade", "field", "net_weight", "weight_uom"],
		limit_page_length=5000,
	)


def _groups_from_lines_and_tickets(settlements: list, group_by: str):
	"""packed from the priced lines, delivered from the matched tickets."""
	names = [s["name"] for s in settlements]
	uom_by_settlement = {s["name"]: s.get("weight_uom") for s in settlements}
	groups: dict = {}

	# The priced lines are read through the parent document rather than off the
	# child table directly. A `Settlement Line Item` query would be cheaper, and
	# it would also be the one place in this module that depends on the child
	# table being separately queryable — which is true of a bench and is not a
	# property worth relying on for a report that is already capped at 500
	# statements.
	def group(key: str) -> dict:
		return groups.setdefault(key, _new_group(key, culled_known=False))

	for name in names:
		for line in receipts._lines_out(frappe.get_doc(SETTLEMENT_STATEMENT, name)):
			row = group((line.get(group_by) or "<unnamed>").strip() or "<unnamed>")
			row["packed"] += float(line.get("packed_weight") or 0)
			row["settlements"].append(name)

	excluded_tickets = 0
	for ticket in _settlement_tickets(names):
		if ticket.get("weight_uom") != uom_by_settlement.get(ticket.get("settlement")):
			excluded_tickets += 1
			continue
		row = group((ticket.get(group_by) or "<unnamed>").strip() or "<unnamed>")
		row["delivered"] += float(ticket.get("net_weight") or 0)
		row["settlements"].append(ticket["settlement"])

	unattributed = {
		"culled_weight": round(sum(float(s.get("cull_weight") or 0) for s in settlements), 3),
		"tickets_in_other_units_excluded": excluded_tickets,
		"note": (
			f"a packer states ONE cull weight per statement, so no part of it attributes to a "
			f"single {group_by}. It is reported here in full rather than split."
		),
	}
	return _finish_groups(groups), unattributed


def _groups_by_field(settlements: list):
	"""delivered from the tickets; packed and culled only where one field owns a settlement."""
	names = [s["name"] for s in settlements]
	uom_by_settlement = {s["name"]: s.get("weight_uom") for s in settlements}
	tickets = _settlement_tickets(names)

	fields_per_settlement: dict = {}
	groups: dict = {}
	excluded_tickets = 0
	for ticket in tickets:
		settlement = ticket.get("settlement")
		field = (ticket.get("field") or "").strip()
		fields_per_settlement.setdefault(settlement, set()).add(field or "<none>")
		if ticket.get("weight_uom") != uom_by_settlement.get(settlement):
			excluded_tickets += 1
			continue
		key = field or "<no field on the ticket>"
		# packed and culled start UNKNOWN for a field group and become known only
		# if a settlement turns out to be owned by this field alone, below.
		row = groups.setdefault(key, _new_group(key, packed_known=False, culled_known=False))
		row["delivered"] += float(ticket.get("net_weight") or 0)
		row["settlements"].append(settlement)

	unattributed_packed = 0.0
	unattributed_culled = 0.0
	unattributed_settlements = []
	for s in settlements:
		seen = fields_per_settlement.get(s["name"]) or set()
		single = next(iter(seen)) if len(seen) == 1 else None
		packed = float(s.get("packed_weight") or 0)
		culled = float(s.get("cull_weight") or 0)
		if single and single != "<none>":
			row = groups.setdefault(single, _new_group(single, packed_known=False, culled_known=False))
			row["_packed_known"] = True
			row["_culled_known"] = True
			row["packed"] += packed
			row["culled"] += culled
			row["settlements"].append(s["name"])
		else:
			unattributed_packed += packed
			unattributed_culled += culled
			unattributed_settlements.append(s["name"])

	unattributed = {
		"packed_weight": round(unattributed_packed, 3),
		"culled_weight": round(unattributed_culled, 3),
		"settlement_count": len(unattributed_settlements),
		"settlements": sorted(unattributed_settlements),
		"tickets_in_other_units_excluded": excluded_tickets,
		"note": (
			"these settlements pool fruit from more than one field, or from tickets that name no "
			"field at all, so their packed and culled weights attribute to no single field. They "
			"are reported here in full rather than allocated pro-rata — a pro-rata packout by "
			"field is a made-up number that looks exactly like a measured one."
		),
	}
	return _finish_groups(groups), unattributed


# ── 9. get_ar_aging ──────────────────────────────────────────────────────────


def get_ar_aging(args: dict) -> ToolResult:
	"""Accounts Receivable ageing, grouped by customer.

	THE MIRROR OF `get_ap_aging`, down to the buckets and the cross-check, and
	the complement of `get_outstanding_invoices` — that one lists invoices, this
	one lists CUSTOMERS, which is the shape a dashboard or a collections call
	needs. Both coexist on purpose.

	THE TOTAL comes from GL Entry against every account typed Receivable for the
	company — `debit - credit` summed per customer, which is the true ledger
	balance regardless of what wrote it.

	THE PER-INVOICE BUCKETS come from Sales Invoice.outstanding_amount and
	due_date. GL Entry cannot supply this half on its own: a Payment Entry's GL
	rows do not say which invoice they settled, so there is no way to net a
	payment against one specific invoice by reading the general ledger alone.

	THE TWO ARE CROSS-CHECKED per customer and a mismatch is REPORTED, because a
	real one usually means something posted to the Receivable account outside the
	Sales Invoice → Payment Entry flow — a manual Journal Entry against it is the
	usual cause, and a settlement posted with `post_settlement_to_gl` is the
	usual cause on THIS app's sites.
	"""
	_require_sales_invoice()
	compat.require_doctype("GL Entry", _ERPNEXT_HINT)
	company = resolve_company(as_str(args, "company"), required=True)
	customer = as_str(args, "customer")
	if customer:
		customer = _customer(customer)
	as_of = as_date(args, "as_of") or frappe.utils.today()
	limit = as_limit(args)

	receivable_accounts = frappe.db.get_all(
		"Account", filters={"company": company, "account_type": "Receivable"}, pluck="name"
	)
	if not receivable_accounts:
		data = {
			"customers": [],
			"count": 0,
			"as_of": as_of,
			"company": company,
			"total_outstanding": 0.0,
			"gl_total_outstanding": 0.0,
			"buckets": {},
			"invoice_count": 0,
			"note": f"{company} has no Account typed Receivable, so there is nothing to age.",
		}
		return ToolResult(data, f"no Receivable accounts for {company}")

	gl_filters: dict = {
		"company": company,
		"account": ("in", receivable_accounts),
		"party_type": "Customer",
	}
	if compat.has_field("GL Entry", "is_cancelled"):
		gl_filters["is_cancelled"] = 0
	if customer:
		gl_filters["party"] = customer
	gl_rows = frappe.db.get_all(
		"GL Entry", filters=gl_filters, fields=["party", "debit", "credit"], limit=5000
	)
	gl_balance: dict = {}
	for row in gl_rows:
		party = row["party"]
		gl_balance[party] = round(
			gl_balance.get(party, 0.0) + float(row.get("debit") or 0) - float(row.get("credit") or 0), 2
		)

	si_filters: dict = {"company": company, "docstatus": 1, "outstanding_amount": (">", 0.005)}
	if customer:
		si_filters["customer"] = customer
	invoices = frappe.db.get_all(
		SALES_INVOICE,
		filters=si_filters,
		fields=compat.existing_fields(
			SALES_INVOICE,
			("name", "customer", "customer_name", "posting_date", "due_date", "outstanding_amount"),
		),
		order_by="due_date asc",
		limit=limit,
	)

	by_customer: dict = {}
	buckets_total = _empty_buckets()
	subledger_total = 0.0

	for invoice in invoices:
		party = invoice["customer"]
		outstanding = round(float(invoice.get("outstanding_amount") or 0), 2)
		days = _days_overdue(invoice.get("due_date"), as_of)
		label = _bucket(days)

		entry = by_customer.setdefault(
			party,
			{
				"customer": party,
				"customer_name": invoice.get("customer_name"),
				"total_outstanding": 0.0,
				"invoices": [],
				"buckets": _empty_buckets(),
			},
		)
		entry["total_outstanding"] = round(entry["total_outstanding"] + outstanding, 2)
		entry["invoices"].append(
			{
				"name": invoice["name"],
				"posting_date": str(invoice.get("posting_date") or "") or None,
				"due_date": str(invoice.get("due_date") or "") or None,
				"outstanding_amount": outstanding,
				"days_overdue": days,
				"ageing_bucket": label,
			}
		)
		_add_to_bucket(entry["buckets"], label, outstanding)
		_add_to_bucket(buckets_total, label, outstanding)
		subledger_total += outstanding

	for party, entry in by_customer.items():
		ledger = gl_balance.get(party, 0.0)
		entry["gl_balance"] = ledger
		drift = round(ledger - entry["total_outstanding"], 2)
		if abs(drift) > 0.005:
			entry["drift"] = drift
			entry["drift_note"] = (
				f"GL Entry shows {ledger} owed by {party} but open Sales Invoices only account for "
				f"{entry['total_outstanding']}. The {drift} difference is something posted to the "
				"Receivable account outside the Sales Invoice → Payment Entry flow — a manual "
				"Journal Entry against it, or a settlement booked with post_settlement_to_gl, are "
				"the two usual causes."
			)

	customers = sorted(by_customer.values(), key=lambda row: row["total_outstanding"], reverse=True)

	data = {
		"customers": customers,
		"count": len(customers),
		"as_of": as_of,
		"company": company,
		"invoice_count": len(invoices),
		"truncated": len(invoices) == limit,
		"totals": {
			"total_outstanding": round(subledger_total, 2),
			"gl_total_outstanding": round(sum(gl_balance.values()), 2),
			"buckets": buckets_total,
		},
		# Flat aliases, so a caller that knows get_ap_aging's shape reads this one
		# without a second lookup.
		"total_outstanding": round(subledger_total, 2),
		"gl_total_outstanding": round(sum(gl_balance.values()), 2),
		"buckets": buckets_total,
		"filters": {"customer": customer or None, "company": company},
		"bucket_definition": (
			"days_overdue = as_of - due_date. 'current' is not yet due (days_overdue <= 0); "
			"'0-30', '31-60', '61-90' and '90+' are days past due; 'unknown' is an invoice with no "
			"due_date. total_outstanding sums open Sales Invoices' own outstanding_amount; "
			"gl_total_outstanding is the true ledger balance from GL Entry against every account "
			"typed Receivable. A per-customer 'drift' field means the two disagree for that "
			"customer."
		),
	}
	return ToolResult(
		data,
		f"{len(customers)} customer(s) owing {round(subledger_total, 2)} total as of {as_of}",
	)


# ── 10. get_season_summary ───────────────────────────────────────────────────


def get_season_summary(args: dict) -> ToolResult:
	"""The whole pipeline for a season: tickets → settlements → invoices → payments.

	THE GAPS ARE THE POINT. Any one of these registers read alone looks fine; it
	is the joins between them that go wrong, and always in the same three places:
	fruit delivered that no settlement ever claimed, settlements nobody invoiced,
	and invoices nobody chased. Each is counted, weighed or valued here, and
	`pipeline_health` is `complete` only when all three are empty.

	Nothing here is corrected and nothing is inferred. A gap is a list of
	docnames a person can go and look at.
	"""
	compat.require_doctype(
		SETTLEMENT_STATEMENT,
		"It ships with erpnext_mcp — run `bench migrate` after installing v0.67.0.",
	)
	company = resolve_company(as_str(args, "company"), required=True)
	customer = as_str(args, "customer")
	if customer:
		customer = _customer(customer)
	from_date = as_date(args, "from_date", required=True)
	to_date = as_date(args, "to_date", required=True)
	if from_date > to_date:
		raise ToolError(f"from_date {from_date} is after to_date {to_date}")

	ticket_filters: dict = {
		"company": company,
		"docstatus": ("<", 2),
		"date": ("between", [from_date, to_date]),
	}
	if customer:
		ticket_filters["customer"] = customer
	tickets = frappe.db.get_all(
		SCALE_TICKET,
		filters=ticket_filters,
		fields=[
			"name",
			"date",
			"customer",
			"variety",
			"net_weight",
			"weight_uom",
			"settlement",
			"docstatus",
			"status",
		],
		limit_page_length=5000,
	)

	settlement_filters: dict = {
		"company": company,
		"docstatus": 1,
		"date": ("between", [from_date, to_date]),
	}
	if customer:
		settlement_filters["customer"] = customer
	settlements = frappe.db.get_all(
		SETTLEMENT_STATEMENT,
		filters=settlement_filters,
		fields=[
			"name",
			"statement_number",
			"date",
			"customer",
			"gross_delivered_weight",
			"packed_weight",
			"cull_weight",
			"weight_uom",
			"total_gross_revenue",
			"total_deductions",
			"net_proceeds",
			"packout_pct",
			"posted_journal_entry",
		]
		+ ([SALES_INVOICE_LINK_FIELD] if _settlement_can_link() else []),
		order_by="date asc",
		limit_page_length=500,
	)

	deliveries = _delivery_rollup(tickets)
	settlement_block = _settlement_rollup(settlements)
	invoicing = _invoicing_rollup(company, customer, from_date, to_date)
	unmatched = _unmatched_tickets(tickets)
	uninvoiced = _uninvoiced_settlements(settlements)

	gaps = []
	if unmatched["count"]:
		gaps.append(
			f"{unmatched['count']} submitted Scale Ticket(s) totalling {unmatched['total_weight']} "
			f"are claimed by no settlement — delivered fruit nobody has been paid for."
		)
	if uninvoiced["count"]:
		gaps.append(
			f"{uninvoiced['count']} submitted settlement(s) worth {uninvoiced['total_net_proceeds']} "
			f"have neither a Sales Invoice nor a posted Journal Entry — revenue that is not in the "
			f"ledger."
		)
	if invoicing["total_outstanding"] > 0.005:
		gaps.append(
			f"{invoicing['outstanding_invoice_count']} submitted invoice(s) still owe "
			f"{invoicing['total_outstanding']} — billed and not collected."
		)

	data = {
		"company": company,
		"customer": customer or None,
		"period": {"from_date": from_date, "to_date": to_date},
		"deliveries": deliveries,
		"settlements": settlement_block,
		"invoicing": invoicing,
		"unmatched_tickets": unmatched,
		"unsettled_deliveries": unmatched,
		"uninvoiced_settlements": uninvoiced,
		"pipeline_health": "complete" if not gaps else "has_gaps",
		"gaps": gaps,
		"note": (
			"Every stage is filtered by ITS OWN date, so a delivery in November settled in January "
			"is in this window and its settlement is not. That is the honest reading of a date "
			"range and it is also why unmatched_tickets near the end of a season is normal rather "
			"than alarming — widen to_date before treating it as a finding. Scale tickets still in "
			"DRAFT are counted in the delivery totals and excluded from the unmatched list: a "
			"draft ticket is not yet evidence of anything. unsettled_deliveries is the same list "
			"as unmatched_tickets under the name the executive summary uses for it."
		),
	}
	return ToolResult(
		data=data,
		summary=f"{from_date} to {to_date} for {company}: {deliveries['ticket_count']} ticket(s), "
		f"{settlement_block['count']} settlement(s), {invoicing['invoice_count']} invoice(s) — "
		f"{data['pipeline_health']}",
	)


def _delivery_rollup(tickets: list) -> dict:
	by_variety: dict = {}
	uoms: dict = {}
	total = 0.0
	for ticket in tickets:
		weight = float(ticket.get("net_weight") or 0)
		total += weight
		key = ticket.get("variety") or "<unnamed>"
		row = by_variety.setdefault(key, {"variety": key, "net_weight": 0.0, "ticket_count": 0})
		row["net_weight"] = round(row["net_weight"] + weight, 3)
		row["ticket_count"] += 1
		uom = ticket.get("weight_uom") or "<none>"
		uoms[uom] = uoms.get(uom, 0) + 1
	return {
		"ticket_count": len(tickets),
		"total_net_weight": round(total, 3),
		"weight_uom": next(iter(uoms)) if len(uoms) == 1 else None,
		"by_weight_uom": uoms,
		"by_variety": sorted(by_variety.values(), key=lambda r: -r["net_weight"]),
		"note": (
			"total_net_weight is MEANINGLESS when by_weight_uom names more than one unit — bins "
			"and pounds do not add, and nothing here converts them."
		),
	}


def _settlement_rollup(settlements: list) -> dict:
	gross = round(sum(float(s.get("total_gross_revenue") or 0) for s in settlements), 2)
	deductions = round(sum(float(s.get("total_deductions") or 0) for s in settlements), 2)
	net = round(sum(float(s.get("net_proceeds") or 0) for s in settlements), 2)
	delivered = round(sum(float(s.get("gross_delivered_weight") or 0) for s in settlements), 3)
	packed = round(sum(float(s.get("packed_weight") or 0) for s in settlements), 3)
	return {
		"count": len(settlements),
		"total_gross_revenue": gross,
		"total_deductions": deductions,
		"total_net_proceeds": net,
		"total_delivered_weight": delivered,
		"total_packed_weight": packed,
		"avg_packout_pct": _pct(packed, delivered),
		"note": (
			"avg_packout_pct is the WEIGHTED packout — total packed over total delivered — not the "
			"mean of each statement's own percentage. A mean of percentages lets a two-bin "
			"statement count as much as a two-hundred-bin one."
		),
	}


def _invoicing_rollup(company: str, customer: str, from_date: str, to_date: str) -> dict:
	if not compat.doctype_exists(SALES_INVOICE):
		return {
			"invoice_count": 0,
			"total_invoiced": 0.0,
			"total_outstanding": 0.0,
			"total_paid": 0.0,
			"outstanding_invoice_count": 0,
			"note": "this site has no Sales Invoice doctype, so nothing was invoiced through it.",
		}
	filters: dict = {
		"company": company,
		"docstatus": 1,
		"posting_date": ("between", [from_date, to_date]),
	}
	if customer:
		filters["customer"] = customer
	rows = frappe.db.get_all(
		SALES_INVOICE,
		filters=filters,
		fields=compat.existing_fields(SALES_INVOICE, ("name", "grand_total", "outstanding_amount", "status")),
		limit=500,
	)
	invoiced = round(sum(float(r.get("grand_total") or 0) for r in rows), 2)
	outstanding = round(sum(float(r.get("outstanding_amount") or 0) for r in rows), 2)
	return {
		"invoice_count": len(rows),
		"total_invoiced": invoiced,
		"total_outstanding": outstanding,
		"total_paid": round(invoiced - outstanding, 2),
		"outstanding_invoice_count": sum(1 for r in rows if float(r.get("outstanding_amount") or 0) > 0.005),
		"note": (
			"total_paid is invoiced less outstanding, which counts a credit note or a write-off as "
			"paid. It is the collected-versus-billed figure, not a sum of Payment Entries."
		),
	}


def _unmatched_tickets(tickets: list) -> dict:
	rows = [t for t in tickets if not t.get("settlement") and int(t.get("docstatus") or 0) == 1]
	uoms: dict = {}
	for t in rows:
		uom = t.get("weight_uom") or "<none>"
		uoms[uom] = uoms.get(uom, 0) + 1
	return {
		"count": len(rows),
		"total_weight": round(sum(float(t.get("net_weight") or 0) for t in rows), 3),
		"by_weight_uom": uoms,
		"scale_tickets": sorted(t["name"] for t in rows)[:100],
		"note": (
			"Submitted Scale Tickets no settlement has claimed: fruit delivered that nobody has "
			"paid for yet. Draft tickets are excluded — a draft is not yet evidence. At most 100 "
			"docnames are listed; count is the whole figure."
		),
	}


def _uninvoiced_settlements(settlements: list) -> dict:
	rows = [
		s for s in settlements if not s.get(SALES_INVOICE_LINK_FIELD) and not s.get("posted_journal_entry")
	]
	return {
		"count": len(rows),
		"total_net_proceeds": round(sum(float(s.get("net_proceeds") or 0) for s in rows), 2),
		"settlement_statements": sorted(s["name"] for s in rows)[:100],
		"note": (
			"Submitted settlements with neither a Sales Invoice nor a posted Journal Entry behind "
			"them: revenue the packer has agreed to and the ledger has never heard of. On a site "
			"that has not migrated to v0.70.0 the settlement has no sales_invoice column, so every "
			"settlement appears here — run `bench migrate` before reading this as a finding."
		),
	}


# ── 11. post_settlement_to_gl ────────────────────────────────────────────────


def post_settlement_to_gl(args: dict) -> ToolResult:
	"""Book a submitted settlement as a DRAFT Journal Entry. The alternative path.

	THE OTHER WAY OUT OF A SETTLEMENT, and the one to choose only deliberately.
	`create_sales_invoice_from_settlement` produces a Sales Invoice, which is
	what gives AR ageing, payment allocation and every standard receivables
	report something to work with. This produces a Journal Entry, which gives the
	same three GL movements and none of the subledger — the receivable exists as
	a balance against a party and not as a document anybody can age. Operations
	that reconcile settlements against a bank deposit rather than against an
	invoice want this; most operations want the invoice.

	THE ENTRY, in three parts and always balanced:

	    debit   receivable_account   net proceeds     (party: the packer)
	    debit   deduction_account    total deductions
	    credit  income_account       total gross revenue

	ALWAYS A DRAFT, because `mutate.insert_draft_journal_entry` is the one place
	this app writes a Journal Entry and it cannot be talked into submitting.
	`submit_journal_entry` is the separate tool with the separate switch, and
	until it runs this has moved no balance — which is also why the settlement's
	`posted_journal_entry` is stamped now rather than at submit: the point of the
	column is that a SECOND posting is refused, and a draft that nobody notices
	is exactly how a second one gets written.
	"""
	row = _settlement_row(args)
	doc = _settlement_doc(row["name"])
	_check_settlement_is_invoiceable(doc, action="a journal entry")

	company = doc.get("company")
	gross = round(float(doc.get("total_gross_revenue") or 0), 2)
	deductions = round(float(doc.get("total_deductions") or 0), 2)
	net = round(float(doc.get("net_proceeds") or 0), 2)
	if gross <= 0:
		raise ToolError(
			f"settlement statement {doc.name} has a gross revenue of {gross}, so there is nothing "
			f"to post. A settlement with weights but no priced lines is a packout report, not a "
			f"bill. Nothing was created."
		)

	income_account = _resolve_income_account(as_str(args, "income_account"), company)
	receivable_account = _resolve_receivable_account(
		as_str(args, "receivable_account") or as_str(args, "debit_to"), company
	)
	deduction_account = (
		_resolve_deduction_account(as_str(args, "deduction_account"), company) if deductions else None
	)
	cost_center = as_str(args, "cost_center")
	if cost_center:
		cost_center = resolve_cost_center(cost_center, company)
	posting_date = as_date(args, "posting_date") or str(doc.get("date"))

	lines = [
		{
			"account": receivable_account,
			"debit": net,
			"party_type": "Customer",
			"party": doc.get("customer"),
			**({"cost_center": cost_center} if cost_center else {}),
		}
	]
	if deductions:
		lines.append(
			{
				"account": deduction_account,
				"debit": deductions,
				**({"cost_center": cost_center} if cost_center else {}),
			}
		)
	lines.append(
		{
			"account": income_account,
			"credit": gross,
			**({"cost_center": cost_center} if cost_center else {}),
		}
	)

	remark = (
		f"Packer settlement {doc.get('statement_number') or doc.name} from {doc.get('customer')} "
		f"({doc.get('period_start') or '?'} to {doc.get('period_end') or '?'}): "
		f"{gross} gross less {deductions} deductions = {net} net proceeds."
	)
	validated = mutate.validated_journal_lines(lines, company)
	entry = mutate.insert_draft_journal_entry(
		company, posting_date, validated, remark, {"voucher_type": "Journal Entry"}
	)

	linked = _link_settlement_to_journal_entry(doc.name, entry.name)

	data = {
		"journal_entry": entry.name,
		"name": entry.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"posting_date": posting_date,
		"settlement_statement": doc.name,
		"customer": doc.get("customer"),
		"debit_total": round(net + deductions, 2),
		"credit_total": gross,
		"line_count": len(validated),
		"accounts": {
			"receivable_account": receivable_account,
			"deduction_account": deduction_account,
			"income_account": income_account,
			"cost_center": cost_center or None,
		},
		"amounts": {
			"total_gross_revenue": gross,
			"total_deductions": deductions,
			"net_proceeds": net,
		},
		"settlement_linked": linked,
		"user_remark": remark,
		"next_step": (
			f"Journal Entry {entry.name} is a DRAFT and has moved no balance. Review it in ERPNext, "
			"or submit it with submit_journal_entry if this site enables that tool. The settlement "
			"is already stamped with it, so a second posting through either path is now refused."
		),
		"note": (
			"This path produces NO Sales Invoice, so the receivable exists as a party balance and "
			"not as a document get_outstanding_invoices can age. get_ar_aging will show it as "
			"per-customer drift between the ledger and the open invoices, which is the correct "
			"reading rather than an error."
		),
	}
	return ToolResult(
		data=data,
		summary=f"created draft Journal Entry {entry.name} from settlement {doc.name}: "
		f"{round(net + deductions, 2)} debit = {gross} credit",
		docstatus_delta="none → 0 (draft)",
	)


def _link_settlement_to_journal_entry(settlement: str, entry: str) -> bool:
	"""Stamp `posted_journal_entry`, which also flips the settlement to Posted.

	The status column is derived by the controller from docstatus and this field
	(`settlement_statement.status_for`), so the write has to set both — a
	`db.set_value` does not run `validate`, and a settlement carrying a journal
	entry while still reading `Submitted` would be a document that contradicts
	itself.
	"""
	try:
		frappe.db.set_value(
			SETTLEMENT_STATEMENT,
			settlement,
			{"posted_journal_entry": entry, "status": "Posted"},
			update_modified=False,
		)
		return True
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: could not link a Settlement Statement to its Journal Entry",
			message=compat.traceback_text(),
		)
		return False


# ── 12. reconcile_settlement_to_tickets ──────────────────────────────────────


def reconcile_settlement_to_tickets(args: dict) -> ToolResult:
	"""Match late-arriving Scale Tickets to a settlement that already exists.

	`create_settlement_statement` matches tickets at capture. Tickets do not
	always arrive by then — a driver's stub found in a truck in December is an
	ordinary event, and the settlement it belongs to was filed in November. This
	is the only way to attach one afterwards.

	THE SAME FOUR CHECKS, run BEFORE anything is written, so a settlement is
	never left with half its tickets claimed: a ticket still in draft is refused
	(its weights can still change), one already matched to another settlement is
	refused (two statements paying for one load is the overpayment this register
	exists to surface), and so is one from another company or another packer.
	`receipts._tickets_to_match` is that check, called here rather than copied.

	THE VARIANCE MOVES AND THE REPORT SAYS BY HOW MUCH. Adding a ticket does not
	change the packer's delivered weight and does not change the settlement's
	money — it changes what the grower's own records say arrived, which is the
	other half of the comparison. `variance_change` is the whole point of the
	call: it is how much the disagreement with the packer moved, and which
	direction.
	"""
	row = _settlement_row(args)
	doc = _settlement_doc(row["name"])

	if int(doc.get("docstatus") or 0) == 2:
		raise ToolError(
			f"settlement statement {doc.name} is cancelled, and a cancelled statement claims "
			f"nothing. Nothing was changed."
		)

	raw = args.get("scale_tickets") or args.get("tickets")
	if raw in (None, "", []):
		raise ToolError(
			"scale_tickets is required — a list of Scale Ticket docnames to match to this "
			"settlement. Call list_scale_tickets with unmatched: true to find the candidates."
		)

	before = receipts._matched_tickets(doc.name)
	reconciliation_before = receipts._reconciliation(doc, before)

	names = receipts._tickets_to_match(
		{"scale_tickets": raw}, company=doc.get("company"), customer=doc.get("customer")
	)
	matched = receipts._match_tickets(doc.name, names)

	after = receipts._matched_tickets(doc.name)
	reconciliation_after = receipts._reconciliation(doc, after)
	variance_change = round(reconciliation_after["variance"] - reconciliation_before["variance"], 3)

	data = {
		"settlement_statement": doc.name,
		"statement_number": doc.get("statement_number"),
		"customer": doc.get("customer"),
		"company": doc.get("company"),
		"matched_count": matched,
		"matched_scale_tickets": names,
		"ticket_count_before": len(before),
		"ticket_count_after": len(after),
		"reconciliation_before": reconciliation_before,
		"updated_reconciliation": reconciliation_after,
		"delivery_reconciliation": reconciliation_after,
		"variance_change": variance_change,
		"note": (
			"variance is the PACKER's delivered weight less the sum of the grower's matched "
			"tickets, so matching a ticket makes it SMALLER by that ticket's net weight — a "
			"negative variance_change is the expected direction. Nothing about the settlement's "
			"weights, prices or money was touched: the packer's figures are the packer's, and "
			"agreeing them would delete the comparison. A ticket in a different weight unit is "
			"still matched and still excluded from the arithmetic, and says so."
		),
	}
	return ToolResult(
		data=data,
		summary=f"matched {matched} scale ticket(s) to settlement {doc.name}: variance moved "
		f"{variance_change} to {reconciliation_after['variance']} "
		f"{reconciliation_after['weight_uom']}",
		docstatus_delta="none" if not matched else f"{matched} ticket(s) → Matched",
	)
