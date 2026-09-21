# SPDX-License-Identifier: MIT
"""Expense receipt capture — the MCP surface over Expense Receipt.

v0.31.0. The write end of this is a phone. A foreman stands at a fuel pump or a
parts counter, photographs the slip, iOS Vision OCR reads it on-device, and the
app posts the extracted fields, the photograph and the raw OCR text in one call
to `submit_expense_receipt`. The read end is a bookkeeper at a desk asking which
receipts are waiting and which of them the scanner was unsure about.

WHY THE OCR RUNS ON THE PHONE AND NOT HERE. The photograph is the largest thing
in the payload and the extraction is the cheapest part of the job; doing it on
the device means the foreman sees the merchant and the total on screen before
they put the phone away, and can correct them while the paper is still in their
hand. By the time this module sees a receipt, a person has already looked at what
the machine read. That is why `submit_expense_receipt` takes extracted fields
rather than an image to parse, and why it takes `ocr_confidence` as data rather
than computing one.

APPROVAL AND REJECTION ARE SEPARATE TOOLS WITH SEPARATE SWITCHES. They are not
one `review_expense_receipt` with a verdict argument, because an operator who
wants a manager to be able to approve reimbursements does not necessarily want
the same surface able to refuse them, and a single switch cannot express that. It
is also the difference between two settings an operator reads in the form and one
they have to reason about.

A REJECTION REQUIRES A REASON. Not because the schema demands a non-empty string,
but because "rejected" with no sentence beside it is the state that generates the
next three messages asking why — and by the time anybody asks, the person who
refused it has forgotten. It is stored on the record, not in a comment, so
`get_expense_receipt` returns it to the phone that submitted the thing.

────────────────────────────────────────────────────────────────────────────
v0.67.0: THE SUPPLIER AND ITEM LINKS, AND WHY THEY SIT BESIDE THE TEXT
────────────────────────────────────────────────────────────────────────────

Sprint 1 gave this app the ability to create a Supplier and an Item; this
release lets a receipt point at them. `supplier` on the header and `item` on
each line are both OPTIONAL and both ADDITIVE — `merchant` and `description`
keep saying exactly what the paper said.

That is the whole design. A slip printed `VALLEY CO-OP #14` and a Supplier
record called `Valley Co-operative` are the same vendor, and a capture that
replaced the first with the second would lose the evidence in the act of
improving the data. Keeping both is what lets a bookkeeper total a year of fuel
per vendor AND still show an auditor the string the machine read.

NEITHER IS EVER INFERRED. No fuzzy match from merchant to Supplier, no lookup
from an OCR'd line to an Item. `HYD HOSE 1/2` matches four items in a real
catalogue, and a guess would put a fabricated consumption figure somewhere a
person would later read as a measurement. A link gets set when a human — or a
client with a picker in front of a human — says so, or it stays empty.

Both are refused by name on a bench without ERPNext rather than written as
dangling links, because a Link column pointing at a doctype the site does not
have is a record that cannot be opened in the Desk.
"""

from __future__ import annotations

import csv
import datetime
import io

import frappe
from frappe.utils import getdate, today

from .. import compat, security
from ..args import as_bool, as_date, as_float, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

EXPENSE_RECEIPT = "Expense Receipt"
EXPENSE_RECEIPT_ITEM = "Expense Receipt Item"
EMPLOYEE = "Employee"
FARM_TASK = "Farm Task"
SUPPLIER = "Supplier"
ITEM = "Item"
COST_CENTER = "Cost Center"

DRAFT = "Draft"
SUBMITTED = "Submitted"
APPROVED = "Approved"
REJECTED = "Rejected"

#: Every status the doctype declares, in the order it moves through them.
STATUSES = (DRAFT, SUBMITTED, APPROVED, REJECTED)

#: What `submit_expense_receipt` will create. A receipt captured on a phone is
#: Submitted — the foreman pressed the button — but the tool accepts Draft so a
#: client with an offline queue can post something it has not finished with.
CREATABLE_STATUSES = (DRAFT, SUBMITTED)

#: The statuses a review decision may still be taken from. Deciding an already
#: decided receipt would overwrite the name and date of whoever decided it first,
#: which is the one thing an approval record exists to preserve.
REVIEWABLE_STATUSES = (DRAFT, SUBMITTED)

#: The categories the Select ships with. Kept here as well as in the DocType JSON
#: so a bad value is refused with the list rather than with a Frappe traceback,
#: and asserted equal to the JSON's options by the tests.
CATEGORIES = (
	"Fuel",
	"Equipment Parts",
	"Supplies",
	"Hardware",
	"Feed",
	"Seed",
	"Fertilizer",
	"Owner Draw",
	"Title/MCO",
	"Bill of Sale",
	"Co-op Equity",
	"Patronage Dividend",
	"Other",
)

#: v0.68.0. `Owner Draw` is not an expense — it is equity leaving the company —
#: and it does not get a Purchase Invoice. `create_purchase_invoice_from_receipt`
#: refuses a receipt in this category by name and points at `create_owner_draw`
#: instead. Kept as a constant of one rather than a bare string comparison
#: scattered across two modules, so the day a second non-expense category is
#: added, both refusals are one edit.
OWNER_DRAW_CATEGORY = "Owner Draw"

#: v0.165.0. The two categories that record a DOCUMENT rather than money spent: a
#: vehicle's title or MCO, and a bill of sale. Captured through the receipt flow
#: because that is the flow that photographs paper; see `tools/vehicle_titles.py`.
#: The amount is optional on these, `create_purchase_invoice_from_receipt` refuses
#: them, and `get_expense_summary` leaves them out of its totals.
TITLE_CATEGORY = "Title/MCO"
BILL_OF_SALE_CATEGORY = "Bill of Sale"
DOCUMENT_CATEGORIES = (TITLE_CATEGORY, BILL_OF_SALE_CATEGORY)

#: v0.166.0. Money to or from a cooperative that is not an operating cost. A
#: `Co-op Equity` purchase buys a stake (an asset: `is_balance_sheet_item`), and a
#: `Patronage Dividend` is income the co-op pays back. Neither gets a Purchase
#: Invoice; both post through `post_coop_receipt` as a draft Journal Entry, and
#: `get_expense_summary` leaves both out of its totals. See `tools/coop_equity.py`.
COOP_EQUITY_CATEGORY = "Co-op Equity"
PATRONAGE_CATEGORY = "Patronage Dividend"
COOP_CATEGORIES = (COOP_EQUITY_CATEGORY, PATRONAGE_CATEGORY)

#: Every category that is not money spent on running the farm, which the expense
#: totals leave out. One tuple, so the summary and the Purchase Invoice refusal
#: cannot disagree about which categories those are.
NON_EXPENSE_CATEGORIES = (*DOCUMENT_CATEGORIES, *COOP_CATEGORIES)

#: Fields `update_expense_receipt` may change. Deliberately NOT `merchant`,
#: `amount` or `receipt_date` — those are the machine's reading of the paper, and
#: rewriting one through the same door as a recode would let a correction that
#: changes what was spent pass as a correction that changes which bucket it came
#: out of. What IS here is what a bookkeeper adds once the paper is off a truck
#: seat and onto a desk: which vendor it really was, which bucket it is coded to,
#: whether the money went out or came back, and a note.
#:
#: v0.160.0 ADDS `is_return` AND NOT THE OTHER TWO. A tick saying which direction
#: the money went is a fact about the slip that nobody had to read off it — the
#: OCR never claimed it, so changing it corrects nothing and there is nothing to
#: keep an original of. `amount` and `receipt_date` get their own tools, with an
#: audit trail each: see `correct_receipt_amount`.
UPDATABLE_FIELDS = ("cost_center", "supplier", "category", "notes", "is_return")

#: What every read tool returns for a receipt, minus the raw OCR text and the
#: line items — both of which are large and only `get_expense_receipt` returns.
_LIST_FIELDS = (
	"name",
	"merchant",
	"amount",
	"receipt_date",
	"category",
	"status",
	"company",
	"submitted_by",
	"supplier",
	"cost_center",
	"farm_task",
	"ocr_confidence",
	"receipt_image",
	"approved_by",
	"approved_date",
	"rejected_by",
	"rejected_date",
	"rejection_reason",
	"linked_doctype",
	"linked_document",
	"notes",
	"modified",
)

#: v0.75.0. The seven receipt-intelligence columns, read back when the site has
#: them. KEPT OUT OF `_LIST_FIELDS` and filtered through `compat` on every query
#: instead, because they are Custom Fields an operator is allowed to remove —
#: naming them in a `get_all` unconditionally would turn "I deleted a column I
#: never used" into an unreadable receipt register.
_INTELLIGENCE_FIELDS = (
	"card_last_four",
	"merchant_phone",
	"merchant_url",
	"store_number",
	"resolved_merchant",
	"resolution_method",
	"resolution_confidence",
)

#: v0.160.0. The return flag and the correction trail, read back when the site
#: has them. Filtered through `compat` for the same reason as the intelligence
#: block above and one more: these nine columns arrive with THIS release, so a
#: bench that has pulled the code and not yet run `bench migrate` would otherwise
#: have every receipt read fail on a column that is not there. They are NOT in
#: `_LIST_FIELDS`, which stays unconditional on purpose — a missing column there
#: is a missing migration and should say so rather than be quietly dropped.
_CORRECTION_FIELDS = (
	"is_return",
	"original_amount",
	"amount_correction_reason",
	"amount_corrected_by",
	"amount_corrected_at",
	"original_date",
	"date_correction_reason",
	"date_corrected_by",
	"date_corrected_at",
)

#: v0.165.0. The vehicle-document columns, read back when the site has them, for
#: the same pre-migrate reason as the two blocks above.
_TITLE_FIELDS = ("document_subtype", "vin", "linked_asset")

#: v0.166.0. The co-op columns, read back when the site has them.
_COOP_FIELDS = ("coop_name", "is_balance_sheet_item")


# ── helpers ───────────────────────────────────────────────────────────────


def _read_fields() -> list[str]:
	"""The receipt columns to read: the shipped ones, plus whichever extras exist."""
	return [
		*_LIST_FIELDS,
		*compat.existing_fields(EXPENSE_RECEIPT, _INTELLIGENCE_FIELDS),
		*compat.existing_fields(EXPENSE_RECEIPT, _CORRECTION_FIELDS),
		*compat.existing_fields(EXPENSE_RECEIPT, _TITLE_FIELDS),
		*compat.existing_fields(EXPENSE_RECEIPT, _COOP_FIELDS),
	]


def _resolve_employee(value: str, label: str) -> str:
	"""An Employee docname from a docname or an employee_name.

	A phone knows the worker it logged in as; a manager typing into a chat client
	knows a person's name. Both are "the employee" and only one is the key.
	"""
	if not value:
		raise ToolError(f"{label} is required.")
	if frappe.db.exists(EMPLOYEE, value):
		return str(value)
	found = frappe.db.get_value(EMPLOYEE, {"employee_name": value}, "name")
	if found:
		return str(found)
	raise ToolError(f"no Employee called {value!r} on this site.")


def _require_receipt(args: dict) -> str:
	"""The docname of the receipt this call is about, or a refusal naming why."""
	name = as_str(args, "name") or as_str(args, "expense_receipt") or as_str(args, "receipt")
	if not name:
		raise ToolError("name (the Expense Receipt docname) is required.")
	if not frappe.db.exists(EXPENSE_RECEIPT, name):
		raise ToolError(f"no Expense Receipt called {name!r} on this site.")
	return name


def _row_out(row: dict) -> dict:
	"""One receipt as JSON: dates as ISO strings, numbers as numbers.

	v0.160.0. `original_amount` IS SUPPRESSED UNLESS A CORRECTION ACTUALLY
	HAPPENED, and that is not tidiness. `original_amount` is a Currency column,
	which on a real site is `NOT NULL DEFAULT 0` — so every receipt nobody has
	ever corrected reads back 0.00, and a reader shown "original amount: $0.00"
	beside "amount: $13.99" would reasonably conclude the scanner read nothing
	and somebody typed the total in. The database manufactured that zero; the
	timestamp is the only column here that can tell "corrected from nothing"
	apart from "never corrected", because a Datetime is genuinely nullable.

	The same suppression is applied to the reason and the actor for symmetry, so
	the four amount columns are either all present or all absent, and never a
	half-filled trail that has to be interpreted.
	"""
	out = {}
	for key, value in dict(row).items():
		if key == "is_balance_sheet_item":
			# v0.166.0. Always a boolean, because the phone draws the receipt
			# differently on it and an absent Check means an expense.
			out[key] = bool(value)
		elif value is None:
			out[key] = None
		elif key in ("amount", "original_amount", "ocr_confidence", "resolution_confidence"):
			out[key] = float(value or 0)
		elif key in (
			"receipt_date",
			"original_date",
			"approved_date",
			"rejected_date",
			"amount_corrected_at",
			"date_corrected_at",
			"modified",
		):
			out[key] = str(value)
		elif key == "is_return":
			out[key] = bool(value)
		else:
			out[key] = value

	for stamp, columns in (
		("amount_corrected_at", ("original_amount", "amount_correction_reason", "amount_corrected_by")),
		("date_corrected_at", ("original_date", "date_correction_reason", "date_corrected_by")),
	):
		if stamp in out:
			out[stamp.replace("_at", "")] = bool(out[stamp])
			if not out[stamp]:
				out[stamp] = None
				for column in columns:
					if column in out:
						out[column] = None
	return out


def _items_out(doc) -> list[dict]:
	"""The line items off a receipt document, whichever shape the rows are in."""
	items = []
	for row in doc.get("items") or []:
		get = row.get if isinstance(row, dict) else (lambda k, d=None, r=row: getattr(r, k, d))
		items.append(
			{
				"description": get("description"),
				"item": get("item"),
				"quantity": float(get("quantity") or 0),
				"unit_price": float(get("unit_price") or 0),
				"line_total": float(get("line_total") or 0),
			}
		)
	return items


def _linked(doctype: str, value: str, label: str) -> str:
	"""An optional Link argument, proved to exist, or a refusal naming the reason.

	A bench without ERPNext has no Supplier and no Item at all, and the refusal
	says THAT rather than "no Supplier called 'X'" — which would read as a typo
	and send somebody looking for a record that could never be there.
	"""
	if not value:
		return ""
	if not frappe.db.exists("DocType", doctype):
		raise ToolError(
			f"this site has no {doctype} doctype, so {label} cannot be set. {doctype} "
			f"ships with the ERPNext app; the receipt captures fine without it."
		)
	if not frappe.db.exists(doctype, value):
		raise ToolError(f"no {doctype} called {value!r} on this site.")
	return str(value)


def _read_items(args: dict) -> list[dict]:
	"""The `items` argument, validated into rows the child table will accept.

	A missing total is filled from quantity times unit price — OCR reads a bold
	receipt total far more reliably than it reads a column of line arithmetic, so
	the derived number is usually better than the read one. A total the scanner
	DID read is kept: a receipt that charges four at $3 and totals $11.50 after a
	discount is telling the truth, and the multiplication is not.
	"""
	raw = args.get("items")
	if raw in (None, "", []):
		return []
	if not isinstance(raw, list):
		raise ToolError(
			"items must be a list of objects with description, quantity, unit_price and line_total."
		)

	rows = []
	for index, item in enumerate(raw, start=1):
		if not isinstance(item, dict):
			raise ToolError(f"items[{index}] must be an object, got {type(item).__name__}.")
		quantity = as_float(item.get("quantity"), f"items[{index}].quantity")
		unit_price = as_float(item.get("unit_price"), f"items[{index}].unit_price")
		line_total = as_float(item.get("line_total"), f"items[{index}].line_total")
		if not line_total and quantity and unit_price:
			line_total = round(quantity * unit_price, 2)
		rows.append(
			{
				"description": as_str(item, "description") or None,
				"item": _linked(ITEM, as_str(item, "item"), f"items[{index}].item") or None,
				"quantity": quantity,
				"unit_price": unit_price,
				"line_total": line_total,
			}
		)
	return rows


def _confidence(args: dict) -> float | None:
	"""`ocr_confidence` as a fraction, or a refusal saying it is not a percentage."""
	raw = args.get("ocr_confidence")
	if raw in (None, ""):
		return None
	value = as_float(raw, "ocr_confidence")
	if value < 0 or value > 1:
		raise ToolError(
			f"ocr_confidence is a fraction from 0 to 1, not a percentage — got {value}. "
			f"A scanner reporting 87 means 0.87."
		)
	return value


# ── read tools ────────────────────────────────────────────────────────────


def list_expense_receipts(args: dict) -> ToolResult:
	"""Receipts, filtered by status, employee, company and date range."""
	filters = {}

	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)

	status = as_str(args, "status")
	if status:
		if status not in STATUSES:
			raise ToolError(f"status must be one of: {', '.join(STATUSES)}.")
		filters["status"] = status

	employee = as_str(args, "employee") or as_str(args, "submitted_by")
	if employee:
		filters["submitted_by"] = _resolve_employee(employee, "employee")

	category = as_str(args, "category")
	if category:
		if category not in CATEGORIES:
			raise ToolError(f"category must be one of: {', '.join(CATEGORIES)}.")
		filters["category"] = category

	farm_task = as_str(args, "farm_task")
	if farm_task:
		filters["farm_task"] = farm_task

	supplier = as_str(args, "supplier")
	if supplier:
		filters["supplier"] = _linked(SUPPLIER, supplier, "supplier")

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["receipt_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["receipt_date"] = (">=", from_date)
	elif to_date:
		filters["receipt_date"] = ("<=", to_date)

	# `as_limit` IS this: default 100, clamped to [1, 500]. It also clamps an explicit
	# 0 to 1 instead of discarding it, which the hand-rolled version could not do —
	# a 0 reached `limit_page_length`, where Frappe reads it as NO LIMIT.
	limit = as_limit(args)

	# **TWO ORDERS, BECAUSE THIS LIST ANSWERS TWO DIFFERENT QUESTIONS.**
	#
	# Lowest confidence first is a REVIEW QUEUE: the receipts somebody most needs
	# to open the photograph for are at the top of the page rather than the end
	# of it, which is what a bookkeeper at a desk wants and is the default.
	#
	# `newest_first` is a DIARY, and it is what a handset asks for — "the last
	# ten I filed", and then the ten before those. v0.176.3, and it is not a
	# preference: with the review order a `limit` of ten answers the ten LEAST
	# confident receipts in the whole register, which is neither the last ten nor
	# a page of anything. Paging by date on top of that order would return
	# overlapping, gap-ridden sets.
	newest_first = as_bool(args, "newest_first", False)
	rows = frappe.db.get_all(
		EXPENSE_RECEIPT,
		filters=filters,
		fields=_read_fields(),
		limit_page_length=limit,
		order_by=(
			"receipt_date desc, creation desc"
			if newest_first
			else "ocr_confidence asc, receipt_date desc"
		),
	)
	receipts = [_row_out(row) for row in rows]
	total = sum(receipt["amount"] for receipt in receipts)

	data = {
		"receipts": receipts,
		"count": len(receipts),
		"total_amount": round(total, 2),
	}
	return ToolResult(
		data=data,
		summary=f"{len(receipts)} expense receipt(s) totalling {round(total, 2)}",
	)


def get_expense_receipt(args: dict) -> ToolResult:
	"""One receipt in full, including the line items and the raw OCR text."""
	name = _require_receipt(args)
	doc = frappe.get_doc(EXPENSE_RECEIPT, name)

	data = _row_out({field: doc.get(field) for field in _read_fields()})
	data["ocr_raw_text"] = doc.get("ocr_raw_text")
	data["items"] = _items_out(doc)
	data["items_total"] = round(sum(item["line_total"] for item in data["items"]), 2)

	return ToolResult(
		data=data,
		summary=f"Expense receipt {name}: {doc.get('merchant')} {doc.get('amount')} "
		f"on {doc.get('receipt_date')} — {doc.get('status')}",
	)


# ── mutating tools ────────────────────────────────────────────────────────


def submit_expense_receipt(args: dict) -> ToolResult:
	"""Create a receipt from a photograph and what the scanner read off it."""
	merchant = as_str(args, "merchant", required=True)
	category = as_str(args, "category") or "Other"
	if category not in CATEGORIES:
		raise ToolError(f"category must be one of: {', '.join(CATEGORIES)}.")
	amount = as_float(args.get("amount"), "amount")
	# v0.165.0. A title or a bill of sale is a document, not a purchase, so the
	# amount may be left out and reads 0. An explicit 0 or a printed price is
	# kept as sent.
	if args.get("amount") in (None, "") and category not in DOCUMENT_CATEGORIES:
		raise ToolError("amount is required.")
	if amount < 0:
		raise ToolError(
			"amount cannot be negative. A slip whose money came BACK — a part returned to the "
			"counter, a core charge refunded — is captured at its POSITIVE amount with "
			"is_return: true, which is what makes the bank matcher look at credits instead of "
			"withdrawals. A minus sign here would net against the wrong side of the statement."
		)

	# v0.160.0. `compat`-guarded because the column ships with this release: a
	# bench running the new code before `bench migrate` captures the receipt
	# rather than refusing it, and the flag can be set later from a desk.
	is_return = 1 if as_bool(args, "is_return", False) else 0

	receipt_date = as_date(args, "receipt_date", required=True)
	company = resolve_company(as_str(args, "company"), required=True)
	submitted_by = _resolve_employee(as_str(args, "submitted_by") or as_str(args, "employee"), "submitted_by")

	from . import vehicle_titles  # imports this module; see `receipts` below

	title = _title_arguments(args, category, company)
	coop = _coop_arguments(args, category, merchant, amount)

	status = as_str(args, "status") or SUBMITTED
	if status not in CREATABLE_STATUSES:
		raise ToolError(
			f"status must be {' or '.join(CREATABLE_STATUSES)} on submission. "
			f"Approval and rejection are separate tools."
		)

	farm_task = as_str(args, "farm_task")
	if farm_task and not frappe.db.exists(FARM_TASK, farm_task):
		raise ToolError(f"no Farm Task called {farm_task!r} on this site.")

	supplier = _linked(SUPPLIER, as_str(args, "supplier"), "supplier")

	cost_center = as_str(args, "cost_center")
	if cost_center and category in COOP_CATEGORIES:
		# v0.166.0. An equity stake and a dividend are not an operating cost, so there
		# is nothing for a cost center to allocate. Refused rather than dropped, so a
		# client that always sends one hears about it.
		raise ToolError(
			f"cost_center does not apply to a {category} receipt: it is not an operating cost. "
			"Leave it out. Nothing was created."
		)
	if cost_center:
		_linked(COST_CENTER, cost_center, "cost_center")

	# IMPORTED IN THE FUNCTION, not at the top. `tools/receipts.py` imports this
	# module for the Expense Receipt name and its statuses, so a module-level
	# import here would be a cycle. Same shape and same reason as `read.py`'s
	# local import of `mutate`.
	from . import receipts

	ocr_raw_text = as_str(args, "ocr_raw_text")

	# v0.75.0. The cascade runs BEFORE the insert and writes nothing itself —
	# what it returns is folded into the same document, so a receipt is never
	# briefly on the site without its own resolution. See `resolve_merchant`.
	intelligence = _receipt_intelligence(args, merchant, ocr_raw_text)
	resolution = intelligence["resolution"]

	# THE ONE PLACE A CAPTURE MAY SET A SUPPLIER IT WAS NOT GIVEN, and it is not
	# an inference: an exact alias hit is a person's own earlier decision about
	# this exact spelling, replayed. Every other step of the cascade — a domain,
	# a phone number, a name score — stops at `resolved_merchant` and leaves the
	# link to a human, which is the rule this tool has had since v0.31.0.
	supplier_resolved_by = "argument" if supplier else ""
	if not supplier and resolution.get("method") == "Alias" and resolution.get("supplier"):
		supplier = resolution["supplier"]
		supplier_resolved_by = (
			f"alias {resolution['alias']['alias_key']!r} taught from a person's own supplier link"
		)

	payload = {
		"doctype": EXPENSE_RECEIPT,
		"merchant": merchant,
		"amount": amount,
		"receipt_date": receipt_date,
		"category": category,
		"company": company,
		"submitted_by": submitted_by,
		"supplier": supplier or None,
		"cost_center": cost_center or None,
		"farm_task": farm_task or None,
		"status": status,
		"receipt_image": as_str(args, "receipt_image") or None,
		"ocr_raw_text": ocr_raw_text or None,
		"ocr_confidence": _confidence(args),
		"notes": as_str(args, "notes") or None,
	}
	if compat.has_field(EXPENSE_RECEIPT, "is_return"):
		payload["is_return"] = is_return
	payload.update(intelligence["columns"])
	payload.update(title["columns"])
	payload.update(coop)

	doc = frappe.get_doc(payload)
	for row in _read_items(args):
		doc.append("items", row)

	doc.flags.ignore_permissions = True
	doc.insert()

	if resolution.get("method") == "Alias" and (resolution.get("alias") or {}).get("name"):
		receipts.record_alias_use(resolution["alias"]["name"])

	# v0.165.0. After the insert, because the asset's back-link names this docname.
	# Every refusal a link can raise was raised in `_title_arguments`, before it.
	title_reported = None
	if category in DOCUMENT_CATEGORIES:
		title_reported = {"linked_asset": None, "matched_by": None, "candidates": []}
		asset = title["linked_asset"]
		if asset:
			title_reported.update(vehicle_titles.link(doc.name, asset, replace=True))
			title_reported["matched_by"] = "argument"
		elif title["columns"].get("vin"):
			match = vehicle_titles.match_asset(title["columns"]["vin"], company)
			title_reported["candidates"] = match["candidates"]
			if match["asset"]:
				title_reported.update(vehicle_titles.link(doc.name, match["asset"], replace=False))
				title_reported["matched_by"] = match["matched_on"]
		title_reported["linked_asset"] = title_reported.pop("asset", None)
		title_reported.pop("receipt", None)

	return ToolResult(
		data={
			"name": doc.name,
			"merchant": merchant,
			"amount": amount,
			"receipt_date": str(receipt_date),
			"category": category,
			"status": status,
			"company": company,
			"submitted_by": submitted_by,
			"supplier": supplier or None,
			"supplier_resolved_by": supplier_resolved_by or None,
			"cost_center": cost_center or None,
			"farm_task": farm_task or None,
			"ocr_confidence": doc.get("ocr_confidence"),
			"receipt_image": doc.get("receipt_image"),
			"is_return": bool(is_return),
			"items": _items_out(doc),
			**intelligence["reported"],
			**(
				{
					"coop_name": coop["coop_name"],
					"is_balance_sheet_item": bool(coop["is_balance_sheet_item"]),
				}
				if coop
				else {}
			),
			**(
				{
					"document_subtype": title["columns"].get("document_subtype"),
					"vin": title["columns"].get("vin"),
					"title": title_reported,
				}
				if title_reported is not None
				else {}
			),
		},
		summary=f"Expense receipt {doc.name} captured: {merchant} {amount} on {receipt_date} "
		f"({category}{', RETURN — money back' if is_return else ''}) by {submitted_by}"
		+ (
			f" — resolved to {resolution['resolved_merchant']} by {resolution['method']} "
			f"(confidence {resolution['confidence']})"
			if resolution.get("resolved_merchant")
			else ""
		),
		docstatus_delta=f"none → {status}",
	)


def _coop_arguments(args: dict, category: str, merchant: str, amount: float) -> dict:
	"""`coop_name` and `is_balance_sheet_item`, checked before anything is written.

	v0.166.0. ON THE TWO CO-OP CATEGORIES ONLY. `coop_name` defaults to the
	merchant line, because that is where the co-op's name is printed and a notice
	nobody retyped should still total under somebody. `is_balance_sheet_item` is
	never an argument: it is 1 exactly when the category is `Co-op Equity`.

	THE AMOUNT MUST BE POSITIVE. A co-op receipt is only ever a sum of money moving;
	a zero one would be a row the equity summary counts as a transaction.
	"""
	tail = "Nothing was created."
	coop_name = as_str(args, "coop_name")
	if category not in COOP_CATEGORIES:
		if coop_name:
			raise ToolError(
				f"coop_name belongs to a {' or '.join(COOP_CATEGORIES)} receipt, and this receipt is "
				f"categorised {category!r}. {tail}"
			)
		return {}
	if not all(compat.has_field(EXPENSE_RECEIPT, field) for field in _COOP_FIELDS):
		raise ToolError(
			"this site's Expense Receipt does not have the co-op columns yet. Run `bench --site "
			f"<site> migrate` after installing v0.166.0. {tail}"
		)
	if amount <= 0:
		raise ToolError(f"a {category} receipt needs a positive amount — got {amount}. {tail}")
	return {
		"coop_name": coop_name or merchant,
		"is_balance_sheet_item": 1 if category == COOP_EQUITY_CATEGORY else 0,
	}


def _title_arguments(args: dict, category: str, company: str) -> dict:
	"""`document_subtype`, `vin` and `linked_asset`, checked before anything is written.

	v0.165.0. THEY BELONG TO THE TWO DOCUMENT CATEGORIES AND ARE REFUSED ON ANY
	OTHER. A fuel slip carrying a VIN would link itself to a truck's title slot
	on the strength of a number nobody meant as one.

	`linked_asset` IS PROVED LINKABLE HERE — it exists, it is a Vehicle or a
	Tractor, it is this company's — so the receipt is never inserted and then
	left unlinked by a refusal that could have come first.
	"""
	from . import vehicle_titles

	tail = "Nothing was created."
	subtype = as_str(args, "document_subtype")
	vin = vehicle_titles.normalize_vin(as_str(args, "vin"))
	asset = as_str(args, "linked_asset")
	if category not in DOCUMENT_CATEGORIES:
		sent = [
			key
			for key, value in (("document_subtype", subtype), ("vin", vin), ("linked_asset", asset))
			if value
		]
		if sent:
			raise ToolError(
				f"{', '.join(sent)} belong to a {' or '.join(DOCUMENT_CATEGORIES)} document, and this "
				f"receipt is categorised {category!r}. {tail}"
			)
		return {"columns": {}, "linked_asset": ""}

	vehicle_titles.require_installed(tail)
	vehicle_titles.require_subtype(subtype, tail)
	if asset:
		vehicle_titles.require_titled_asset(asset, company, tail)
	columns = {
		key: value
		for key, value in (("document_subtype", subtype), ("vin", vin), ("linked_asset", asset))
		if value
	}
	return {"columns": columns, "linked_asset": asset}


def _receipt_intelligence(args: dict, merchant: str, ocr_raw_text: str) -> dict:
	"""What the paper says besides the name, and what it resolves to.

	THREE THINGS COME BACK, and they are separated because they go to three
	different places: `columns` are written onto the document (and only the ones
	this site actually has), `resolution` is the cascade in full for the tool
	layer to act on, and `reported` is what the caller is told — which includes
	the cascade's own step list, because a suggestion with no evidence beside it
	is a number a person can only accept or reject.

	AN ARGUMENT ALWAYS BEATS THE TEXT. A client that ran its own extraction over
	a higher-resolution image than the raw text it sent knows better than four
	regexes; what is read off the text fills in the fields it did not send, and
	`signals_from_raw_text` says which those were.
	"""
	from . import receipts  # see submit_expense_receipt on why this is local

	card = receipts.card_last_four(as_str(args, "card_last_four"))
	phone = receipts.normalize_phone(as_str(args, "merchant_phone"))
	if as_str(args, "merchant_phone") and not phone:
		raise ToolError(
			f"merchant_phone {as_str(args, 'merchant_phone')!r} is not a ten-digit phone number. "
			"Nothing was created."
		)
	url = as_str(args, "merchant_url")
	domain = receipts.normalize_domain(url)
	if url and not domain:
		raise ToolError(f"merchant_url {url!r} is not a domain or a URL. Nothing was created.")

	resolution = receipts.resolve_merchant(
		merchant,
		merchant_url=url,
		merchant_phone=phone,
		store_number=as_str(args, "store_number"),
		card_last_four=card,
		ocr_raw_text=ocr_raw_text,
		resolved_merchant=as_str(args, "resolved_merchant"),
		resolution_method=as_str(args, "resolution_method"),
		resolution_confidence=args.get("resolution_confidence"),
	)

	installed = receipts.intelligence_fields_installed()
	values = {
		receipts.CARD_LAST_FOUR_FIELD: resolution["signals"][receipts.CARD_LAST_FOUR_FIELD],
		receipts.MERCHANT_PHONE_FIELD: resolution["signals"][receipts.MERCHANT_PHONE_FIELD],
		receipts.MERCHANT_URL_FIELD: resolution["signals"][receipts.MERCHANT_URL_FIELD],
		receipts.STORE_NUMBER_FIELD: resolution["signals"][receipts.STORE_NUMBER_FIELD],
		receipts.RESOLVED_MERCHANT_FIELD: resolution.get("resolved_merchant"),
		receipts.RESOLUTION_METHOD_FIELD: resolution.get("method"),
		receipts.RESOLUTION_CONFIDENCE_FIELD: resolution.get("confidence") or None,
	}
	columns = {}
	if installed:
		columns = {key: value for key, value in values.items() if value not in (None, "")}

	return {
		"columns": columns,
		"resolution": resolution,
		"reported": {
			"signals": resolution["signals"],
			"signals_from_raw_text": resolution["signals_from_raw_text"],
			"resolved_merchant": resolution.get("resolved_merchant"),
			"resolution_method": resolution.get("method"),
			"resolution_confidence": resolution.get("confidence"),
			"resolution_steps": resolution["steps"],
			"llm_context": resolution.get("llm_context"),
			"intelligence_fields_installed": installed,
			**(
				{}
				if installed
				else {
					"intelligence_note": (
						"this site's Expense Receipt does not have the seven receipt-intelligence "
						"columns, so the resolution above was computed and reported but NOT stored "
						"on the record. Run `bench migrate`; capture is otherwise unaffected."
					)
				}
			),
		},
	}


def approve_expense_receipt(args: dict) -> ToolResult:
	"""Approve a receipt, recording who approved it and when."""
	name = _require_receipt(args)
	status = frappe.db.get_value(EXPENSE_RECEIPT, name, "status")
	if status not in REVIEWABLE_STATUSES:
		raise ToolError(
			f"expense receipt {name} is already {status!r}. "
			f"Only {' or '.join(REVIEWABLE_STATUSES)} receipts can be approved."
		)

	approved_by = _resolve_employee(as_str(args, "approved_by") or as_str(args, "employee"), "approved_by")
	approved_date = as_date(args, "approved_date") or today()

	frappe.db.set_value(
		EXPENSE_RECEIPT,
		name,
		{
			"status": APPROVED,
			"approved_by": approved_by,
			"approved_date": approved_date,
		},
	)

	merchant, amount = frappe.db.get_value(EXPENSE_RECEIPT, name, ["merchant", "amount"])
	return ToolResult(
		data={
			"name": name,
			"status": APPROVED,
			"approved_by": approved_by,
			"approved_date": str(approved_date),
		},
		summary=f"Expense receipt {name} ({merchant} {amount}) approved by {approved_by} on {approved_date}",
		docstatus_delta=f"{status} → {APPROVED}",
	)


def reject_expense_receipt(args: dict) -> ToolResult:
	"""Reject a receipt with a reason, recording who rejected it and when."""
	name = _require_receipt(args)
	status = frappe.db.get_value(EXPENSE_RECEIPT, name, "status")
	if status not in REVIEWABLE_STATUSES:
		raise ToolError(
			f"expense receipt {name} is already {status!r}. "
			f"Only {' or '.join(REVIEWABLE_STATUSES)} receipts can be rejected."
		)

	reason = as_str(args, "reason") or as_str(args, "rejection_reason")
	if not reason:
		raise ToolError(
			"reason is required to reject a receipt. A rejection with no sentence beside it "
			"is the state that generates the next three messages asking why."
		)

	rejected_by = _resolve_employee(as_str(args, "rejected_by") or as_str(args, "employee"), "rejected_by")
	rejected_date = as_date(args, "rejected_date") or today()

	frappe.db.set_value(
		EXPENSE_RECEIPT,
		name,
		{
			"status": REJECTED,
			"rejected_by": rejected_by,
			"rejected_date": rejected_date,
			"rejection_reason": reason,
		},
	)

	merchant, amount = frappe.db.get_value(EXPENSE_RECEIPT, name, ["merchant", "amount"])
	return ToolResult(
		data={
			"name": name,
			"status": REJECTED,
			"rejected_by": rejected_by,
			"rejected_date": str(rejected_date),
			"rejection_reason": reason,
		},
		summary=f"Expense receipt {name} ({merchant} {amount}) rejected by {rejected_by} "
		f"on {rejected_date}: {reason}",
		docstatus_delta=f"{status} → {REJECTED}",
	)


# ── update_expense_receipt ───────────────────────────────────────────────────


def update_expense_receipt(args: dict) -> ToolResult:
	"""Correct cost_center, supplier, category or notes on a receipt already captured.

	WHY THIS EXISTS. A receipt is captured fast, at a fuel pump or a parts
	counter, by whoever has the phone — and coded properly later, at a desk, by
	whoever reconciles the books. `submit_expense_receipt` is the first act;
	this is the second. It runs on a receipt in ANY status, including one already
	Approved or Rejected, because none of the four fields it touches is the thing
	that was approved or rejected — the merchant, the amount and the date are.
	Recoding what bucket a $184 fuel stop landed in does not reopen the question
	of whether $184 was a reasonable fuel stop.

	WHAT IT WILL NOT TOUCH. `merchant`, `amount`, `receipt_date`, `receipt_image`,
	`ocr_raw_text`, `ocr_confidence`, `farm_task`, and every review field. Those
	are either the machine's reading of the paper (correcting one is a fresh
	capture, not an edit) or the record of a decision this tool has no business
	rewriting.

	AT LEAST ONE FIELD, AND AT LEAST ONE REAL CHANGE. A call that touches none of
	`cost_center`, `supplier`, `category`, `notes` has nothing to do, and a call
	whose values already match the record has nothing to change — both are
	refused rather than silently accepted as a no-op write, because the caller's
	next question after either is "did that do anything?" and the answer should
	not require a second call to find out.

	v0.75.0. SETTING A SUPPLIER TEACHES AN ALIAS, and that is the one place this
	app's merchant resolution learns anything. A bookkeeper who codes a
	"SIATAPING" slip to Sawyer's Ace Hardware has answered a question no
	algorithm could — those letters are not in that name — and the answer is
	worth keeping, because the same till will print the same string every week
	for years. So the mapping is recorded, and the NEXT such receipt resolves
	itself at capture. Nothing is asked of the bookkeeper: they were never shown
	a form about aliases, and the register grows out of work they were doing
	anyway.

	IT IS SKIPPED WHEN IT WOULD BE REDUNDANT — a merchant string that already
	normalises to the Supplier's own name is found by name matching, and a row
	teaching that would be one fact stored twice. `alias_learned.action` says
	which of created / repointed / unchanged / skipped happened, and why.

	AND IT NEVER FAILS THE UPDATE. Learning is a side effect of a write that has
	already succeeded; an alias register that refuses a row must not turn a
	completed supplier correction into an error the caller has to interpret.
	"""
	from . import receipts  # see submit_expense_receipt on why this is local

	name = _require_receipt(args)
	present = [key for key in UPDATABLE_FIELDS if key in args]
	if not present:
		raise ToolError(
			f"nothing to update — pass at least one of: {', '.join(UPDATABLE_FIELDS)}. Nothing was changed."
		)

	doc = frappe.get_doc(EXPENSE_RECEIPT, name)
	before = {}
	after = {}

	for key in present:
		if key == "cost_center":
			value = as_str(args, "cost_center")
			if value and not frappe.db.exists(COST_CENTER, value):
				raise ToolError(f"no Cost Center called {value!r} on this site. Nothing was changed.")
		elif key == "supplier":
			value = _linked(SUPPLIER, as_str(args, "supplier"), "supplier")
		elif key == "category":
			value = as_str(args, "category")
			if not value or value not in CATEGORIES:
				raise ToolError(f"category must be one of: {', '.join(CATEGORIES)}.")
		elif key == "is_return":
			if not compat.has_field(EXPENSE_RECEIPT, "is_return"):
				raise ToolError(
					"this site has no is_return column on Expense Receipt yet. Run `bench migrate` "
					"after installing v0.160.0. Nothing was changed."
				)
			# 0 and 1 rather than False and True: the column is a Frappe Check,
			# which is an Int, and the comparison below is on the string form.
			value = 1 if as_bool(args, "is_return", False) else 0
		else:  # notes
			value = as_str(args, "notes")

		current = doc.get(key)
		if key == "is_return":
			current = 1 if current else 0
		current = "" if current is None else current
		if str(value) == str(current):
			continue
		# NOT `value or None`. `is_return` is a Check, and 0 is the value that
		# says the money went out — a guard that dropped it would refuse to
		# untick a box that had been ticked in error, and would say the field
		# was missing while reporting the tick it was asked to remove.
		before[key] = current if key == "is_return" else (current or None)
		after[key] = value if key == "is_return" else (value or None)

	if not before:
		raise ToolError(
			f"expense receipt {name} already reads what was asked for on every field named "
			f"({', '.join(present)}). Nothing to change, and nothing was changed."
		)

	if "category" in after:
		_follow_category(doc, before, after)

	frappe.db.set_value(EXPENSE_RECEIPT, name, after)

	alias_learned = None
	if after.get("supplier"):
		alias_learned = receipts.learn_merchant_alias(
			doc.get("merchant") or "",
			after["supplier"],
			source=receipts.ALIAS_MANUAL,
			receipt=name,
		)
		# The receipt's own resolution follows the human, whatever the cascade
		# had decided at capture. `Manual` at 1.0 is not this app being certain —
		# it is this app recording that it was not asked.
		if receipts.intelligence_fields_installed():
			frappe.db.set_value(
				EXPENSE_RECEIPT,
				name,
				{
					receipts.RESOLVED_MERCHANT_FIELD: receipts._supplier_label(after["supplier"]),
					receipts.RESOLUTION_METHOD_FIELD: "Manual",
					receipts.RESOLUTION_CONFIDENCE_FIELD: 1.0,
				},
				update_modified=False,
			)

	diff = "; ".join(f"{key}: {before[key]!r} → {after[key]!r}" for key in before)
	try:
		doc.add_comment("Comment", f"Updated via MCP (erpnext_mcp): {diff}.")
	except Exception:
		# The write itself already succeeded and is the operation the caller
		# asked for; a failed comment must not report it as a failure. The
		# MCP Action Log carries the same diff regardless.
		frappe.log_error(
			title="erpnext_mcp: could not attach update comment to Expense Receipt",
			message=compat.traceback_text(),
		)

	merchant, amount = frappe.db.get_value(EXPENSE_RECEIPT, name, ["merchant", "amount"])
	return ToolResult(
		data={
			"name": name,
			"merchant": merchant,
			"amount": float(amount or 0),
			"fields_changed": sorted(before),
			"before": before,
			"after": after,
			"alias_learned": alias_learned,
		},
		summary=f"Expense receipt {name} ({merchant} {amount}) updated: {diff}"
		+ (
			f" — alias {alias_learned['alias_key']!r} → {alias_learned['supplier']} "
			f"({alias_learned['action']})"
			if alias_learned and alias_learned["action"] in ("created", "repointed")
			else ""
		),
		docstatus_delta="",
	)


def _follow_category(doc, before: dict, after: dict) -> None:
	"""The co-op columns a recategorisation carries with it. Added to the same write.

	v0.166.0. `is_balance_sheet_item` IS DERIVED FROM THE CATEGORY, so moving a
	receipt into or out of `Co-op Equity` moves the flag; left alone, a receipt
	recoded to Fuel would still say it bought an asset. Moving INTO a co-op
	category fills a blank `coop_name` from the merchant, and is refused while the
	receipt carries a cost center, which those categories do not have.
	"""
	category = after["category"]
	if not all(compat.has_field(EXPENSE_RECEIPT, field) for field in _COOP_FIELDS):
		if category in COOP_CATEGORIES:
			raise ToolError(
				"this site's Expense Receipt does not have the co-op columns yet. Run `bench --site "
				"<site> migrate` after installing v0.166.0. Nothing was changed."
			)
		return
	if category in COOP_CATEGORIES:
		cost_center = after.get("cost_center", doc.get("cost_center"))
		if cost_center:
			raise ToolError(
				f"a {category} receipt has no cost center, and this one carries {cost_center!r}. Pass "
				"cost_center as '' in the same call. Nothing was changed."
			)
		if not doc.get("coop_name"):
			before["coop_name"] = None
			after["coop_name"] = doc.get("merchant")
	flag = 1 if category == COOP_EQUITY_CATEGORY else 0
	if int(doc.get("is_balance_sheet_item") or 0) != flag:
		# 0 and 1, not None: the column is a Check, and 0 is the value that says
		# the receipt is an expense again.
		before["is_balance_sheet_item"] = int(doc.get("is_balance_sheet_item") or 0)
		after["is_balance_sheet_item"] = flag


# ── correct_receipt_amount / correct_receipt_date ────────────────────────────

#: The two columns a correction may rewrite, and the trail each one leaves.
#: A dict rather than two hand-written functions because the ONLY thing that
#: differs between correcting a number and correcting a date is how the new
#: value is read and rendered — every rule around it (the reason, the once-only
#: original, the actor from the session, the no-op refusal) is the same rule,
#: and two copies of it would drift the first time one of them was fixed.
CORRECTIONS = {
	"amount": {
		"original": "original_amount",
		"reason": "amount_correction_reason",
		"actor": "amount_corrected_by",
		"stamp": "amount_corrected_at",
		"noun": "amount",
	},
	"receipt_date": {
		"original": "original_date",
		"reason": "date_correction_reason",
		"actor": "date_corrected_by",
		"stamp": "date_corrected_at",
		"noun": "receipt date",
	},
}

#: Arguments both correction tools accept, in the order the schema declares
#: them. Named here so `test_expense_reads` can hold the registry entry and the
#: handler to the same list.
CORRECTION_ARGUMENTS = ("name", "expense_receipt", "receipt", "reason", "correction_reason")


def _correction_reason(args: dict, noun: str) -> str:
	"""The sentence that has to come with a correction, or a refusal saying why.

	Required, on the same reasoning as `reject_expense_receipt`'s. A number
	quietly different from the one on the photograph still stapled beside it is
	the state that generates the next three messages asking which is right, and
	by then the person who changed it has forgotten. "OCR read the expiry date"
	takes four seconds to type and answers the question forever.
	"""
	reason = as_str(args, "reason") or as_str(args, "correction_reason")
	if not reason:
		raise ToolError(
			f"reason is required to correct the {noun}. The photograph stays on the record and "
			f"will not agree with the corrected value; without a sentence saying why, the next "
			f"person to open this receipt cannot tell a fixed OCR error from a typo. "
			f"Nothing was changed."
		)
	return reason


def _correction_trail_installed() -> bool:
	"""Whether this site has been migrated far enough to record a correction."""
	return all(compat.has_field(EXPENSE_RECEIPT, column) for column in _CORRECTION_FIELDS)


def _correct(args: dict, *, field: str, value, rendered: str) -> ToolResult:
	"""Rewrite one captured field, keeping what it said before. Shared by both tools.

	THE ORIGINAL IS WRITTEN ONCE, BY THE FIRST CORRECTION. A second correction
	leaves it alone. This is the whole reason the trail is worth having: the
	question anybody asks later is "what did the scanner read off the paper",
	not "what did the last-but-one person think it read", and a column that
	tracked the previous value would answer the second question while looking
	like it answered the first.

	THE ACTOR COMES FROM THE SESSION AND IS NOT AN ARGUMENT, exactly as
	`resolve_app_feedback`'s does. An audit trail whose "who" is supplied by the
	caller records a claim, not a fact.

	THE STATUS IS NOT TOUCHED. A correction is a bookkeeper fixing what the
	machine read, not a re-review — but where a decision has ALREADY been taken
	on the old number, the response says so, because an approval is a statement
	about an amount and the amount has just changed underneath it. It is
	reported rather than refused: the receipt this exists for is precisely the
	one that got approved at the wrong total and then would not reconcile.
	"""
	spec = CORRECTIONS[field]
	name = _require_receipt(args)
	reason = _correction_reason(args, spec["noun"])

	if not _correction_trail_installed():
		raise ToolError(
			f"this site has no correction columns on {EXPENSE_RECEIPT} yet, so the {spec['noun']} "
			f"cannot be corrected with a record of what it said before — and correcting it "
			f"without one is the thing this tool exists to prevent. Run `bench migrate` after "
			f"installing v0.160.0. Nothing was changed."
		)

	row = dict(
		frappe.db.get_value(
			EXPENSE_RECEIPT,
			name,
			[
				"merchant",
				"amount",
				"receipt_date",
				"status",
				spec["original"],
				spec["stamp"],
				*compat.existing_fields(EXPENSE_RECEIPT, ("bank_transaction",)),
			],
			as_dict=True,
		)
		or {}
	)

	current = row.get(field)
	if str(current or "") == str(value or ""):
		raise ToolError(
			f"expense receipt {name} already reads {rendered} for its {spec['noun']}. Nothing to "
			f"correct, and nothing was changed."
		)

	already = bool(row.get(spec["stamp"]))
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	stamped = frappe.utils.now()

	payload = {
		field: value,
		spec["reason"]: reason,
		spec["actor"]: actor,
		spec["stamp"]: stamped,
	}
	# ONLY ON THE FIRST CORRECTION. See the docstring: the column holds what the
	# scanner read, not what the previous corrector thought.
	if not already:
		payload[spec["original"]] = current
	frappe.db.set_value(EXPENSE_RECEIPT, name, payload)

	was = _rendered(field, row.get(spec["original"]) if already else current)
	diff = f"{spec['noun']}: {_rendered(field, current)} → {rendered}"
	try:
		frappe.get_doc(EXPENSE_RECEIPT, name).add_comment(
			"Comment", f"Corrected via MCP (erpnext_mcp): {diff}. Reason: {reason}"
		)
	except Exception:
		# The correction itself already succeeded and is what the caller asked
		# for; a failed comment must not report it as a failure. Same shape and
		# same reason as `update_expense_receipt`'s.
		frappe.log_error(
			title="erpnext_mcp: could not attach correction comment to Expense Receipt",
			message=compat.traceback_text(),
		)

	data = {
		"name": name,
		"merchant": row.get("merchant"),
		"field": field,
		"previous_value": _rendered(field, current),
		"corrected_to": rendered,
		"as_captured": was,
		"first_correction": not already,
		"correction_reason": reason,
		"corrected_by": actor,
		"corrected_at": stamped,
		"status": row.get("status"),
		"note": (
			f"The status is unchanged — {name} is still {row.get('status')!r}. A correction is "
			f"what the machine read being fixed, not a decision being retaken, and the "
			f"photograph and the raw OCR text are both untouched so the paper can still be "
			f"checked against the number."
		),
	}
	if already:
		data["note"] += (
			f" This is not the first correction to this {spec['noun']}: `as_captured` is still "
			f"what was originally captured, which is the question anybody asks later."
		)
	if row.get("status") in (APPROVED, REJECTED):
		data["decided_on_the_old_value"] = (
			f"{name} was already {row.get('status')} when the {spec['noun']} said "
			f"{_rendered(field, current)}. That decision was about the old value and has not "
			f"been reopened. If the difference matters to it, that is a person's call."
		)
	if row.get("bank_transaction"):
		data["match_is_now_stale"] = (
			f"{name} is matched to bank transaction {row['bank_transaction']}, and the stored "
			f"match confidence was scored against the old {spec['noun']}. The link was not "
			f"broken — a person made it and a person unmakes it — but it is worth re-checking "
			f"with match_receipt_to_bank_transaction."
		)

	return ToolResult(
		data=data,
		summary=f"Expense receipt {name} ({row.get('merchant')}) corrected: {diff} — {reason}",
		docstatus_delta="",
	)


def _rendered(field: str, value) -> str:
	"""One correctable value as the string a person reads in a message."""
	if value in (None, ""):
		return "(empty)"
	return f"{float(value):.2f}" if field == "amount" else str(value)


def correct_receipt_amount(args: dict) -> ToolResult:
	"""Correct the amount on a captured receipt, keeping what the scanner read.

	WHY THIS IS NOT `update_expense_receipt`. That tool recodes a receipt — which
	bucket, which vendor, which cost center — and deliberately refuses to touch
	the money, because a correction that changes what was spent is a different
	act from one that changes what it was spent on, and one door for both means
	no audit trail can tell them apart afterwards. This tool changes the money
	and leaves a trail; that one changes the coding and leaves a comment.

	WHY IT EXISTS AT ALL. On-device OCR reads a NAPA slip's `$13.99` as `$18.18`
	and there is no way back: the amount is fixed at capture, the receipt never
	matches its bank line, and it sits in `list_unmatched_receipts` forever with
	a photograph beside it that plainly shows the right number. Re-capturing is
	the wrong remedy — it makes a second document for one purchase, and the
	first one still has to be dealt with.

	A REASON IS REQUIRED. The photograph stays on the record and will not agree
	with the corrected number; without a sentence, the next person cannot tell a
	fixed OCR error from a typo.

	NEGATIVE IS STILL REFUSED. A refund is not an expense with a minus sign in
	front of it — it is a slip whose money went the other way, which is what
	`is_return` on the receipt says. The amount stays the magnitude the paper
	printed.
	"""
	raw = args.get("amount")
	if raw in (None, ""):
		raise ToolError("amount is required — what should this receipt say instead?")
	value = as_float(raw, "amount")
	if value < 0:
		raise ToolError(
			"amount cannot be negative. A receipt for money coming BACK is captured at its "
			"positive amount with is_return set, which is what makes the bank matcher look at "
			"credits instead of withdrawals. Nothing was changed."
		)
	return _correct(args, field="amount", value=value, rendered=f"{value:.2f}")


def correct_receipt_date(args: dict) -> ToolResult:
	"""Correct the receipt date on a captured receipt, keeping what the scanner read.

	THE SAME ACT AS `correct_receipt_amount`, on the other column the matcher
	depends on, and the one OCR gets wrong in the most spectacular way: a slip
	printed `08/30/26` next to a card's `EXP 01/29` comes back dated 2099-01-08,
	which is outside every date window any matcher would search and inside no
	fiscal year anybody has open.

	`receipt_date` IS THE DOCTYPE'S OWN COLUMN and stays a date. There is no
	partial correction — a receipt whose date the scanner could not read at all
	is a receipt somebody types a date onto, and that is this call.
	"""
	value = as_date(args, "receipt_date") or as_date(args, "date")
	if not value:
		raise ToolError("receipt_date is required — what date should this receipt say instead? YYYY-MM-DD.")
	return _correct(args, field="receipt_date", value=value, rendered=str(value))


# ── get_expense_summary / get_expense_report ─────────────────────────────────

#: Aggregation buckets `get_expense_summary`'s trend series can be sliced into.
PERIODS = ("week", "month", "quarter")

#: Hard ceiling on how many receipts a summary or a report will scan in one
#: call. Not a page size — both tools aggregate or export everything that
#: matches, so this exists purely to keep one request from walking an unbounded
#: table. `truncated` in the response says when it bit.
_SCAN_CAP = 5000


def _period_bucket(date_value, period: str) -> tuple[str, str, str, str]:
	"""One receipt_date's bucket: a sort key, a label, and the span it covers."""
	date = getdate(date_value)
	if period == "week":
		start = date - datetime.timedelta(days=date.weekday())
		end = start + datetime.timedelta(days=6)
		key = start.isoformat()
		label = f"Week of {start.isoformat()}"
	elif period == "quarter":
		quarter = (date.month - 1) // 3 + 1
		start_month = (quarter - 1) * 3 + 1
		end_month = start_month + 2
		start = date.replace(month=start_month, day=1)
		end = _last_day_of_month(date.replace(month=end_month, day=1))
		key = f"{date.year}-Q{quarter}"
		label = key
	else:  # month
		start = date.replace(day=1)
		end = _last_day_of_month(start)
		key = f"{date.year:04d}-{date.month:02d}"
		label = key
	return key, label, start.isoformat(), end.isoformat()


def _last_day_of_month(first_of_month: datetime.date) -> datetime.date:
	next_month = first_of_month.replace(day=28) + datetime.timedelta(days=4)
	return next_month.replace(day=1) - datetime.timedelta(days=1)


def _summary_date_range(args: dict, filters: dict) -> tuple:
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["receipt_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["receipt_date"] = (">=", from_date)
	elif to_date:
		filters["receipt_date"] = ("<=", to_date)
	return from_date, to_date


def get_expense_summary(args: dict) -> ToolResult:
	"""Expense receipts totalled by category and by period — what the dashboard reads.

	`status` is left OFF the filter by default and Rejected receipts are
	excluded instead: a rejected receipt was decided not to be a real expense,
	and a dashboard total that included it would overstate spend by exactly the
	amount somebody already refused. Passing `status` explicitly asks for one
	status only, Rejected included, and the exclusion note does not apply.
	"""
	filters: dict = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	from_date, to_date = _summary_date_range(args, filters)

	period = as_str(args, "period") or "month"
	if period not in PERIODS:
		raise ToolError(f"period must be one of: {', '.join(PERIODS)}.")

	group_by = as_str(args, "group_by")
	if group_by and group_by not in ("merchant", "supplier"):
		raise ToolError("group_by must be 'merchant' or 'supplier'.")

	explicit_status = as_str(args, "status")
	rejected_excluded = 0
	if explicit_status:
		if explicit_status not in STATUSES:
			raise ToolError(f"status must be one of: {', '.join(STATUSES)}.")
		filters["status"] = explicit_status
	else:
		filters["status"] = ("!=", REJECTED)
		rejected_excluded = frappe.db.count(EXPENSE_RECEIPT, {**filters, "status": REJECTED})

	rows = frappe.db.get_all(
		EXPENSE_RECEIPT,
		filters=filters,
		fields=[
			"merchant",
			"amount",
			"receipt_date",
			"category",
			"supplier",
			*compat.existing_fields(EXPENSE_RECEIPT, ("is_return",)),
		],
		order_by="receipt_date asc",
		limit_page_length=_SCAN_CAP,
	)
	truncated = len(rows) == _SCAN_CAP

	by_category: dict = {}
	by_group: dict = {}
	buckets: dict = {}
	total = 0.0
	returns_count = 0
	returns_amount = 0.0
	documents_excluded = 0
	coop_excluded = 0
	counted = 0
	for row in rows:
		# v0.165.0. A title or a bill of sale is a document, not spend. Counted
		# separately and reported, never added.
		if row.get("category") in DOCUMENT_CATEGORIES:
			documents_excluded += 1
			continue
		# v0.166.0. A co-op equity purchase is an asset and a patronage dividend is
		# income. Neither is spend.
		if row.get("category") in COOP_CATEGORIES:
			coop_excluded += 1
			continue
		counted += 1
		# v0.160.0. A RETURN SUBTRACTS. `is_return` says the money came back, and
		# the amount is stored as the magnitude the paper printed — so adding it
		# to a spend total would overstate the category by twice the refund: once
		# for the purchase that is also in here, once for getting it back. The
		# count still counts it, because a return IS a receipt somebody captured
		# and a bucket showing 11 receipts and 10 rows would be the next bug.
		amount = float(row.get("amount") or 0)
		if row.get("is_return"):
			returns_count += 1
			returns_amount = round(returns_amount + amount, 2)
			amount = -amount
		total += amount

		category = row.get("category") or "Other"
		cat = by_category.setdefault(category, {"count": 0, "total": 0.0})
		cat["count"] += 1
		cat["total"] = round(cat["total"] + amount, 2)

		if group_by:
			key = row.get(group_by) or f"<no {group_by}>"
			grp = by_group.setdefault(key, {"count": 0, "total": 0.0})
			grp["count"] += 1
			grp["total"] = round(grp["total"] + amount, 2)

		if row.get("receipt_date"):
			key, label, start, end = _period_bucket(row["receipt_date"], period)
			bucket = buckets.setdefault(
				key, {"period": label, "period_start": start, "period_end": end, "count": 0, "total": 0.0}
			)
			bucket["count"] += 1
			bucket["total"] = round(bucket["total"] + amount, 2)

	trend = [buckets[key] for key in sorted(buckets)]

	data = {
		"count": counted,
		"total_amount": round(total, 2),
		"documents_excluded": documents_excluded,
		"coop_excluded": coop_excluded,
		"returns_count": returns_count,
		"returns_amount": returns_amount,
		"by_category": by_category,
		"trend": trend,
		"period": period,
		"truncated": truncated,
		"filters": {
			"company": filters.get("company") or None,
			"status": explicit_status or None,
			"from_date": from_date,
			"to_date": to_date,
			"group_by": group_by or None,
		},
		"note": (
			f"{rejected_excluded} Rejected receipt(s) excluded from these totals; pass "
			"status='Rejected' explicitly to see them on their own. "
			if not explicit_status
			else ""
		)
		+ (
			f"{returns_count} receipt(s) totalling {returns_amount} are RETURNS and were "
			"SUBTRACTED, not added — the money came back. `total_amount` is net spend. "
			if returns_count
			else ""
		)
		+ (
			f"{documents_excluded} {' / '.join(DOCUMENT_CATEGORIES)} document(s) left out: they "
			"record a vehicle's paperwork, not money spent. "
			if documents_excluded
			else ""
		)
		+ (
			f"{coop_excluded} {' / '.join(COOP_CATEGORIES)} receipt(s) left out: co-op equity is an "
			"asset and patronage is income — list_coop_equity_summary totals them. "
			if coop_excluded
			else ""
		)
		+ (
			f"Scanned the {_SCAN_CAP} most recent matching receipts and stopped; these totals "
			"are a PARTIAL figure. Narrow from_date/to_date to see everything in a window."
			if truncated
			else ""
		),
	}
	if group_by:
		data[f"by_{group_by}"] = by_group
	return ToolResult(
		data,
		f"{counted} expense receipt(s) totalling {round(total, 2)} across "
		f"{len(by_category)} categor{'y' if len(by_category) == 1 else 'ies'}",
	)


#: Columns `get_expense_report` lists per receipt, and the CSV export mirrors.
_REPORT_FIELDS = (
	"name",
	"receipt_date",
	"merchant",
	"category",
	"amount",
	"supplier",
	"cost_center",
	"status",
	"company",
	# v0.160.0. On the export rather than netted into a total, because this tool
	# has no category totals to net — it is one row per receipt, and a bookkeeper
	# reading a $13.99 line needs to see which way it went. Filtered through
	# `compat` at the query, so a pre-migrate bench exports the columns it has.
	"is_return",
)


def get_expense_report(args: dict) -> ToolResult:
	"""Every expense receipt in a window, one row each, for a bookkeeper's export.

	Unlike `get_expense_summary`, nothing is excluded by default — Draft,
	Submitted, Approved and Rejected all appear, because a detailed export is
	where somebody checks what happened to a specific receipt, and a rejected
	one that silently disappeared would look like it was never captured at all.
	Filter to one `status` to narrow it.
	"""
	filters: dict = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	from_date, to_date = _summary_date_range(args, filters)

	status = as_str(args, "status")
	if status:
		if status not in STATUSES:
			raise ToolError(f"status must be one of: {', '.join(STATUSES)}.")
		filters["status"] = status

	category = as_str(args, "category")
	if category:
		if category not in CATEGORIES:
			raise ToolError(f"category must be one of: {', '.join(CATEGORIES)}.")
		filters["category"] = category

	# `as_limit` IS this: default 100, clamped to [1, 500]. It also clamps an explicit
	# 0 to 1 instead of discarding it, which the hand-rolled version could not do —
	# a 0 reached `limit_page_length`, where Frappe reads it as NO LIMIT.
	limit = as_limit(args)

	rows = frappe.db.get_all(
		EXPENSE_RECEIPT,
		filters=filters,
		fields=compat.existing_fields(EXPENSE_RECEIPT, _REPORT_FIELDS),
		order_by="receipt_date asc, name asc",
		limit_page_length=limit,
	)
	receipts = [_row_out(row) for row in rows]
	# Signed for the same reason `get_expense_summary`'s is: a return is money
	# back, and a total that added it would be wrong by twice the refund.
	total = round(sum(-r["amount"] if r.get("is_return") else r["amount"] for r in receipts), 2)
	returns_count = sum(1 for r in receipts if r.get("is_return"))

	data = {
		"receipts": receipts,
		"count": len(receipts),
		"total_amount": total,
		"returns_count": returns_count,
		"limit": limit,
		"truncated": len(receipts) == limit,
		"filters": {
			"company": filters.get("company") or None,
			"status": status or None,
			"category": category or None,
			"from_date": from_date,
			"to_date": to_date,
		},
	}
	if as_bool(args, "csv", False):
		data["csv"] = _csv_export(receipts)
	return ToolResult(data, f"{len(receipts)} expense receipt(s) totalling {total}")


def _csv_export(receipts: list[dict]) -> str:
	buffer = io.StringIO()
	# `restval` because `_REPORT_FIELDS` is now compat-filtered at the query: a
	# bench that has not migrated has no `is_return` key on the row, and
	# DictWriter raises on a missing field rather than leaving the cell empty.
	writer = csv.DictWriter(buffer, fieldnames=list(_REPORT_FIELDS), extrasaction="ignore", restval="")
	writer.writeheader()
	for row in receipts:
		writer.writerow(row)
	return buffer.getvalue()
