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

from .. import compat
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
	"Other",
)

#: v0.68.0. `Owner Draw` is not an expense — it is equity leaving the company —
#: and it does not get a Purchase Invoice. `create_purchase_invoice_from_receipt`
#: refuses a receipt in this category by name and points at `create_owner_draw`
#: instead. Kept as a constant of one rather than a bare string comparison
#: scattered across two modules, so the day a second non-expense category is
#: added, both refusals are one edit.
OWNER_DRAW_CATEGORY = "Owner Draw"

#: Fields `update_expense_receipt` may change. Deliberately NOT `merchant`,
#: `amount` or `receipt_date` — those are the machine's reading of the paper and
#: correcting them is `submit_expense_receipt` capturing a fresh photograph, not
#: an edit to this one. What IS here is exactly what a bookkeeper adds once the
#: paper is off a truck seat and onto a desk: which vendor it really was, which
#: bucket it is coded to, and a note. None of the four affects `amount`, so
#: nothing here can turn one receipt into a different expense.
UPDATABLE_FIELDS = ("cost_center", "supplier", "category", "notes")

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


# ── helpers ───────────────────────────────────────────────────────────────


def _read_fields() -> list[str]:
	"""The receipt columns to read: the shipped ones, plus whichever extras exist."""
	return [*_LIST_FIELDS, *compat.existing_fields(EXPENSE_RECEIPT, _INTELLIGENCE_FIELDS)]


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
	"""One receipt as JSON: dates as ISO strings, numbers as numbers."""
	out = {}
	for key, value in dict(row).items():
		if value is None:
			out[key] = None
		elif key in ("amount", "ocr_confidence", "resolution_confidence"):
			out[key] = float(value or 0)
		elif key in ("receipt_date", "approved_date", "rejected_date", "modified"):
			out[key] = str(value)
		else:
			out[key] = value
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

	# Lowest confidence first, so the receipts a person most needs to open the
	# photo for are the ones at the top of the page rather than at the end of it.
	rows = frappe.db.get_all(
		EXPENSE_RECEIPT,
		filters=filters,
		fields=_read_fields(),
		limit_page_length=limit,
		order_by="ocr_confidence asc, receipt_date desc",
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
	amount = as_float(args.get("amount"), "amount")
	if args.get("amount") in (None, ""):
		raise ToolError("amount is required.")
	if amount < 0:
		raise ToolError("amount cannot be negative. A refund is a credit note, not an expense receipt.")

	receipt_date = as_date(args, "receipt_date", required=True)
	company = resolve_company(as_str(args, "company"), required=True)
	submitted_by = _resolve_employee(as_str(args, "submitted_by") or as_str(args, "employee"), "submitted_by")

	category = as_str(args, "category") or "Other"
	if category not in CATEGORIES:
		raise ToolError(f"category must be one of: {', '.join(CATEGORIES)}.")

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
	payload.update(intelligence["columns"])

	doc = frappe.get_doc(payload)
	for row in _read_items(args):
		doc.append("items", row)

	doc.flags.ignore_permissions = True
	doc.insert()

	if resolution.get("method") == "Alias" and (resolution.get("alias") or {}).get("name"):
		receipts.record_alias_use(resolution["alias"]["name"])

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
			"items": _items_out(doc),
			**intelligence["reported"],
		},
		summary=f"Expense receipt {doc.name} captured: {merchant} {amount} on {receipt_date} "
		f"({category}) by {submitted_by}"
		+ (
			f" — resolved to {resolution['resolved_merchant']} by {resolution['method']} "
			f"(confidence {resolution['confidence']})"
			if resolution.get("resolved_merchant")
			else ""
		),
		docstatus_delta=f"none → {status}",
	)


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
		else:  # notes
			value = as_str(args, "notes")

		current = doc.get(key)
		current = "" if current is None else current
		if str(value) == str(current):
			continue
		before[key] = current or None
		after[key] = value or None

	if not before:
		raise ToolError(
			f"expense receipt {name} already reads what was asked for on every field named "
			f"({', '.join(present)}). Nothing to change, and nothing was changed."
		)

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
		fields=["merchant", "amount", "receipt_date", "category", "supplier"],
		order_by="receipt_date asc",
		limit_page_length=_SCAN_CAP,
	)
	truncated = len(rows) == _SCAN_CAP

	by_category: dict = {}
	by_group: dict = {}
	buckets: dict = {}
	total = 0.0
	for row in rows:
		amount = float(row.get("amount") or 0)
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
		"count": len(rows),
		"total_amount": round(total, 2),
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
		f"{len(rows)} expense receipt(s) totalling {round(total, 2)} across "
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
		fields=list(_REPORT_FIELDS),
		order_by="receipt_date asc, name asc",
		limit_page_length=limit,
	)
	receipts = [_row_out(row) for row in rows]
	total = round(sum(r["amount"] for r in receipts), 2)

	data = {
		"receipts": receipts,
		"count": len(receipts),
		"total_amount": total,
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
	writer = csv.DictWriter(buffer, fieldnames=list(_REPORT_FIELDS), extrasaction="ignore")
	writer.writeheader()
	for row in receipts:
		writer.writerow(row)
	return buffer.getvalue()
