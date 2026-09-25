# SPDX-License-Identifier: MIT
"""Purchasing and AP: Purchase Order, Purchase Receipt, Purchase Invoice, Payment Entry.

Sprint 3 of the Gap Closure Plan (v0.68.0). `list_purchase_orders` and
`get_outstanding_invoices` (its Sales Invoice mirror) already existed in
`tools/trade.py` — this module is everything else the purchasing pipeline
needs: creating and submitting the four documents a purchase actually walks
through, and an AP ageing report to close it out.

THE SAME SPLIT AS `mutate.py`. `create_purchase_order` can only ever produce a
draft; `submit_purchase_order` is the separate, separately-switched tool that
moves it to an active state. That is true of all four documents here, for the
reason `mutate.py`'s module docstring gives for Journal Entry: an operator who
enables "propose" without "post" has given a client a scratchpad, not a pen.

EVERY WRITE GOES THROUGH `frappe.get_doc(...).insert()` / `.submit()`. This
module does not compute a grand_total, a stock ledger entry or a GL Entry
anywhere — ERPNext's own controllers do that the moment `.insert()` or
`.submit()` runs, exactly as they would for a human in the Desk. What this
module DOES do is resolve names to docnames, validate a caller's line items
into the shape ERPNext's child tables expect, and read the fields ERPNext
computed back out.

AP AGEING READS GL Entry, NOT Purchase Invoice.outstanding_amount. Sales side
(`get_outstanding_invoices`) takes the shortcut of trusting the doctype's own
maintained column, which is correct for a receivables snapshot. A payables
review is different: a supplier balance can move through a debit note, a
write-off Journal Entry, or a Payment Entry that references nothing — none of
which necessarily updates every Purchase Invoice's outstanding_amount in a way
a per-invoice read would catch consistently. Reading GL Entry against every
account typed Payable, grouped by supplier, is the true ledger balance, which
is what a payables review actually needs.
"""

from __future__ import annotations

import frappe

from .. import compat
from ..args import as_date, as_float, as_limit, as_str, resolve_account, resolve_company, resolve_cost_center
from ..errors import ToolError
from ..result import ToolResult

PURCHASE_ORDER = "Purchase Order"
PURCHASE_ORDER_ITEM = "Purchase Order Item"
PURCHASE_RECEIPT = "Purchase Receipt"
PURCHASE_RECEIPT_ITEM = "Purchase Receipt Item"
PURCHASE_INVOICE = "Purchase Invoice"
PURCHASE_INVOICE_ITEM = "Purchase Invoice Item"
PAYMENT_ENTRY = "Payment Entry"
SUPPLIER = "Supplier"
ITEM = "Item"
WAREHOUSE = "Warehouse"

#: Purchase Invoice reference doctypes a Payment Entry against a Supplier may
#: settle. Just the one, deliberately: this module's Payment Entry tool is
#: `party_type="Supplier"` only (see `create_payment_entry`), so a Sales
#: Invoice reference could never be reached through it and listing one here
#: would advertise a path that always fails validation two lines later.
PAYABLE_REFERENCE_DOCTYPES = ("Purchase Invoice",)

#: Upper bound of each ageing bucket, in days overdue. Same scheme
#: `trade.get_outstanding_invoices` uses, so a reader who knows one AP/AR
#: report knows both.
AGEING_BUCKETS = (("0-30", 30), ("31-60", 60), ("61-90", 90), ("90+", None))


# ── shared resolvers ─────────────────────────────────────────────────────────


def _resolve_supplier(args: dict) -> str:
	given = as_str(args, "supplier") or as_str(args, "name")
	if not given:
		raise ToolError("supplier is required.")
	if frappe.db.exists(SUPPLIER, given):
		return given
	match = frappe.db.get_all(SUPPLIER, filters={"supplier_name": given}, pluck="name", limit=5)
	if len(match) == 1:
		return match[0]
	if len(match) > 1:
		raise ToolError(
			f"{given!r} matches {len(match)} suppliers: {', '.join(sorted(match))}. Pass the docname."
		)
	raise ToolError(f"no Supplier called {given!r} on this site. Call list_suppliers to find it.")


def _resolve_item(value: str, label: str) -> str:
	value = (value or "").strip()
	if not value:
		raise ToolError(f"{label} is required on every line.")
	if frappe.db.exists(ITEM, value):
		return value
	match = frappe.db.get_value(ITEM, {"item_name": value}, "name")
	if match:
		return str(match)
	raise ToolError(f"no Item called {value!r} on this site. Call list_items to find it.")


def _resolve_warehouse(value: str, company: str, label: str) -> str:
	value = (value or "").strip()
	if not value:
		raise ToolError(f"{label} is required on every line.")
	if not frappe.db.exists(WAREHOUSE, value):
		raise ToolError(f"no Warehouse called {value!r} on this site. Call list_warehouses to find it.")
	found_company = frappe.db.get_value(WAREHOUSE, value, "company")
	if company and found_company and found_company != company:
		raise ToolError(f"warehouse {value!r} belongs to company {found_company!r}, not {company!r}")
	return value


def _resolve_payable_account(explicit: str, company: str) -> str:
	"""The Payable account a Purchase Invoice credits or a Payment Entry debits.

	Three tries, most-specific first: what the caller passed, the company's own
	`default_payable_account`, or — on a site that never set one — the sole
	account typed Payable for this company. Ambiguous or absent in all three
	raises with what to do about it, rather than guessing which liability
	account a caller meant.
	"""
	if explicit:
		return resolve_account(explicit, company)
	default = frappe.db.get_value("Company", company, "default_payable_account")
	if default:
		return default
	matches = frappe.db.get_all(
		"Account", filters={"company": company, "account_type": "Payable"}, pluck="name", limit=5
	)
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ToolError(
			f"{company} has no default_payable_account set and {len(matches)} accounts typed "
			f"Payable: {', '.join(sorted(matches))}. Pass credit_to (or paid_to) explicitly."
		)
	raise ToolError(
		f"{company} has no default_payable_account and no account typed Payable. "
		"Pass credit_to (or paid_to) explicitly, or set the company default."
	)


def _resolve_cash_account(explicit: str, company: str, field_hint: str) -> str:
	if explicit:
		return resolve_account(explicit, company)
	for field in ("default_bank_account", "default_cash_account"):
		default = frappe.db.get_value("Company", company, field)
		if default:
			return default
	raise ToolError(
		f"{company} has no default_bank_account or default_cash_account set. Pass {field_hint} explicitly."
	)


# ── Purchase Order ───────────────────────────────────────────────────────────


def _po_lines(raw, company: str) -> list[dict]:
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"items must be a non-empty list of objects, each with item_code, qty, rate and "
			'warehouse, e.g. [{"item_code": "SPRAY-01", "qty": 10, "rate": 12.5, '
			'"warehouse": "Stores - CO"}]'
		)
	lines = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"items[{index}] must be an object, got {type(entry).__name__}")
		item_code = _resolve_item(
			as_str(entry, "item_code") or as_str(entry, "item"), f"items[{index}].item_code"
		)
		qty = as_float(entry.get("qty"), f"items[{index}].qty")
		rate = as_float(entry.get("rate"), f"items[{index}].rate")
		if qty <= 0:
			raise ToolError(f"items[{index}].qty must be positive, got {qty}")
		if rate < 0:
			raise ToolError(f"items[{index}].rate cannot be negative, got {rate}")
		warehouse = _resolve_warehouse(as_str(entry, "warehouse"), company, f"items[{index}].warehouse")
		line = {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"amount": round(qty * rate, 2),
			"warehouse": warehouse,
			"schedule_date": as_date(entry, "schedule_date"),
		}
		cost_center = as_str(entry, "cost_center")
		if cost_center:
			line["cost_center"] = resolve_cost_center(cost_center, company)
		uom = as_str(entry, "uom")
		if uom:
			line["uom"] = uom
		lines.append(line)
	return lines


def create_purchase_order(args: dict) -> ToolResult:
	"""Create a DRAFT Purchase Order against a Supplier. Never submits."""
	compat.require_doctype(PURCHASE_ORDER, "It ships with ERPNext's Buying module.")
	company = resolve_company(as_str(args, "company"), required=True)
	supplier = _resolve_supplier(args)
	transaction_date = as_date(args, "transaction_date") or frappe.utils.today()
	schedule_date = as_date(args, "schedule_date", required=True)
	items = _po_lines(args.get("items"), company)
	for line in items:
		if not line.get("schedule_date"):
			line["schedule_date"] = schedule_date

	doc = frappe.new_doc(PURCHASE_ORDER)
	doc.company = company
	doc.supplier = supplier
	doc.transaction_date = transaction_date
	doc.schedule_date = schedule_date
	for line in items:
		doc.append("items", line)
	doc.flags.ignore_permissions = True
	doc.insert()

	data = {
		"name": doc.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"supplier": supplier,
		"transaction_date": str(transaction_date),
		"schedule_date": str(schedule_date),
		"grand_total": round(sum(line["amount"] for line in items), 2),
		"item_count": len(items),
		"status": doc.get("status") or "Draft",
		"next_step": (
			"This is a draft against no balance. Submit it in ERPNext, or via "
			"submit_purchase_order if that tool is enabled."
		),
	}
	return ToolResult(
		data,
		f"created draft Purchase Order {doc.name} for {supplier} ({company}): "
		f"{len(items)} line(s), {data['grand_total']}",
		docstatus_delta="none → 0 (draft)",
	)


def _po_items_out(doc) -> list[dict]:
	rows = []
	for row in doc.get("items") or []:
		rows.append(
			{
				"item_code": row.get("item_code"),
				"item_name": row.get("item_name"),
				"qty": float(row.get("qty") or 0),
				"rate": float(row.get("rate") or 0),
				"amount": float(row.get("amount") or 0),
				"received_qty": float(row.get("received_qty") or 0),
				"billed_amt": float(row.get("billed_amt") or 0),
				"warehouse": row.get("warehouse"),
				"schedule_date": row.get("schedule_date"),
				"cost_center": row.get("cost_center"),
			}
		)
	return rows


def get_purchase_order(args: dict) -> ToolResult:
	"""One Purchase Order in full, including its line items."""
	compat.require_doctype(PURCHASE_ORDER, "It ships with ERPNext's Buying module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PURCHASE_ORDER, name):
		raise ToolError(f"no Purchase Order called {name!r} on this site.")
	doc = frappe.get_doc(PURCHASE_ORDER, name)

	fields = compat.existing_fields(
		PURCHASE_ORDER,
		[
			"name",
			"supplier",
			"supplier_name",
			"company",
			"transaction_date",
			"schedule_date",
			"currency",
			"status",
			"docstatus",
			"grand_total",
			"rounded_total",
			"per_received",
			"per_billed",
			"owner",
		],
	)
	data = {field: doc.get(field) for field in fields}
	data["docstatus_label"] = {0: "draft", 1: "submitted", 2: "cancelled"}.get(int(doc.get("docstatus") or 0))
	data["items"] = _po_items_out(doc)
	return ToolResult(
		data,
		f"Purchase Order {doc.name}: {doc.get('supplier')}, {doc.get('grand_total')}, {doc.get('status')}",
	)


def submit_purchase_order(args: dict) -> ToolResult:
	"""Submit a DRAFT Purchase Order (docstatus 0 → 1, status → active).

	This is the tool that commits the company to buying. It takes a name, not a
	document, and cannot create the order it submits — enabling it means "act on
	an order a human or an earlier call already wrote down", never "commit to a
	new purchase right now".
	"""
	compat.require_doctype(PURCHASE_ORDER, "It ships with ERPNext's Buying module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PURCHASE_ORDER, name):
		raise ToolError(f"no Purchase Order called {name!r} on this site.")
	doc = frappe.get_doc(PURCHASE_ORDER, name)
	if int(doc.docstatus or 0) == 1:
		raise ToolError(f"Purchase Order {name} is already submitted")
	if int(doc.docstatus or 0) == 2:
		raise ToolError(f"Purchase Order {name} is cancelled and cannot be submitted")

	doc.submit()
	doc.reload()
	data = {
		"name": doc.name,
		"docstatus": 1,
		"docstatus_label": "submitted",
		"status": doc.get("status"),
		"company": doc.get("company"),
		"supplier": doc.get("supplier"),
		"grand_total": doc.get("grand_total"),
	}
	return ToolResult(
		data,
		f"submitted Purchase Order {doc.name} ({doc.get('supplier')}, {doc.get('grand_total')})",
		docstatus_delta="0 → 1 (submitted)",
	)


# ── Purchase Receipt ─────────────────────────────────────────────────────────


def _pr_lines(raw, company: str) -> list[dict]:
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"items must be a non-empty list of objects, each with item_code, qty and warehouse, "
			'e.g. [{"item_code": "SPRAY-01", "qty": 10, "rate": 12.5, "warehouse": "Stores - CO"}]'
		)
	lines = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"items[{index}] must be an object, got {type(entry).__name__}")
		item_code = _resolve_item(
			as_str(entry, "item_code") or as_str(entry, "item"), f"items[{index}].item_code"
		)
		qty = as_float(entry.get("qty"), f"items[{index}].qty")
		if qty <= 0:
			raise ToolError(f"items[{index}].qty must be positive, got {qty}")
		rate = as_float(entry.get("rate"), f"items[{index}].rate")
		warehouse = _resolve_warehouse(as_str(entry, "warehouse"), company, f"items[{index}].warehouse")
		line = {
			"item_code": item_code,
			"qty": qty,
			"received_qty": qty,
			"rate": rate,
			"amount": round(qty * rate, 2),
			"warehouse": warehouse,
		}
		po = as_str(entry, "purchase_order")
		if po:
			if not frappe.db.exists(PURCHASE_ORDER, po):
				raise ToolError(f"items[{index}].purchase_order {po!r} does not exist.")
			line["purchase_order"] = po
		po_item = as_str(entry, "purchase_order_item")
		if po_item:
			line["purchase_order_item"] = po_item
		cost_center = as_str(entry, "cost_center")
		if cost_center:
			line["cost_center"] = resolve_cost_center(cost_center, company)
		lines.append(line)
	return lines


def create_purchase_receipt(args: dict) -> ToolResult:
	"""Create a DRAFT Purchase Receipt: goods received against a Supplier.

	`purchase_order` is optional but, when given, is validated as a real,
	submitted order for the same supplier — a receipt against a draft or
	cancelled order is not something ERPNext's own Buying module allows either.
	"""
	compat.require_doctype(PURCHASE_RECEIPT, "It ships with ERPNext's Stock/Buying module.")
	company = resolve_company(as_str(args, "company"), required=True)
	supplier = _resolve_supplier(args)
	posting_date = as_date(args, "posting_date") or frappe.utils.today()

	purchase_order = as_str(args, "purchase_order")
	if purchase_order:
		if not frappe.db.exists(PURCHASE_ORDER, purchase_order):
			raise ToolError(f"no Purchase Order called {purchase_order!r} on this site.")
		po_row = frappe.db.get_value(PURCHASE_ORDER, purchase_order, ["supplier", "docstatus"], as_dict=True)
		if po_row.docstatus != 1:
			raise ToolError(f"Purchase Order {purchase_order} is not submitted. Nothing was created.")
		if po_row.supplier != supplier:
			raise ToolError(
				f"Purchase Order {purchase_order} is against supplier {po_row.supplier!r}, not {supplier!r}."
			)

	items = _pr_lines(args.get("items"), company)

	doc = frappe.new_doc(PURCHASE_RECEIPT)
	doc.company = company
	doc.supplier = supplier
	doc.posting_date = posting_date
	if purchase_order:
		doc.set("purchase_order", purchase_order)
	for line in items:
		doc.append("items", line)
	doc.flags.ignore_permissions = True
	doc.insert()

	data = {
		"name": doc.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"supplier": supplier,
		"posting_date": str(posting_date),
		"purchase_order": purchase_order or None,
		"grand_total": round(sum(line["amount"] for line in items), 2),
		"item_count": len(items),
		"status": doc.get("status") or "Draft",
		"next_step": (
			"This is a draft and creates no stock ledger entries yet. Submit it in ERPNext, or "
			"via submit_purchase_receipt if that tool is enabled."
		),
	}
	return ToolResult(
		data,
		f"created draft Purchase Receipt {doc.name} for {supplier} ({company}): "
		f"{len(items)} line(s), {data['grand_total']}",
		docstatus_delta="none → 0 (draft)",
	)


def _pr_items_out(doc) -> list[dict]:
	rows = []
	for row in doc.get("items") or []:
		rows.append(
			{
				"item_code": row.get("item_code"),
				"item_name": row.get("item_name"),
				"qty": float(row.get("qty") or 0),
				"received_qty": float(row.get("received_qty") or 0),
				"rate": float(row.get("rate") or 0),
				"amount": float(row.get("amount") or 0),
				"warehouse": row.get("warehouse"),
				"purchase_order": row.get("purchase_order"),
				"purchase_order_item": row.get("purchase_order_item"),
			}
		)
	return rows


def get_purchase_receipt(args: dict) -> ToolResult:
	"""One Purchase Receipt in full, including its line items."""
	compat.require_doctype(PURCHASE_RECEIPT, "It ships with ERPNext's Stock/Buying module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PURCHASE_RECEIPT, name):
		raise ToolError(f"no Purchase Receipt called {name!r} on this site.")
	doc = frappe.get_doc(PURCHASE_RECEIPT, name)

	fields = compat.existing_fields(
		PURCHASE_RECEIPT,
		[
			"name",
			"supplier",
			"supplier_name",
			"company",
			"posting_date",
			"purchase_order",
			"currency",
			"status",
			"docstatus",
			"grand_total",
			"per_billed",
			"owner",
		],
	)
	data = {field: doc.get(field) for field in fields}
	data["docstatus_label"] = {0: "draft", 1: "submitted", 2: "cancelled"}.get(int(doc.get("docstatus") or 0))
	data["items"] = _pr_items_out(doc)
	return ToolResult(
		data,
		f"Purchase Receipt {doc.name}: {doc.get('supplier')}, {doc.get('grand_total')}, {doc.get('status')}",
	)


def list_purchase_receipts(args: dict) -> ToolResult:
	"""Purchase Receipt headers by supplier, status, company and date range."""
	compat.require_doctype(PURCHASE_RECEIPT, "It ships with ERPNext's Stock/Buying module.")
	status = as_str(args, "status")
	supplier = as_str(args, "supplier")
	purchase_order = as_str(args, "purchase_order")
	company = resolve_company(as_str(args, "company"))
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	limit = as_limit(args)

	filters: dict = {}
	if status:
		filters["status"] = status
	if supplier:
		filters["supplier"] = supplier
	if purchase_order:
		filters["purchase_order"] = purchase_order
	if company:
		filters["company"] = company
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["posting_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["posting_date"] = (">=", from_date)
	elif to_date:
		filters["posting_date"] = ("<=", to_date)

	fields = compat.existing_fields(
		PURCHASE_RECEIPT,
		[
			"name",
			"supplier",
			"supplier_name",
			"posting_date",
			"purchase_order",
			"grand_total",
			"currency",
			"status",
			"per_billed",
			"docstatus",
			"company",
			"owner",
		],
	)
	rows = frappe.db.get_all(
		PURCHASE_RECEIPT,
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit=limit,
	)
	for row in rows:
		row["docstatus_label"] = {0: "draft", 1: "submitted", 2: "cancelled"}.get(
			int(row.get("docstatus") or 0)
		)

	data = {
		"receipts": rows,
		"count": len(rows),
		"limit": limit,
		"truncated": len(rows) == limit,
		"total_value": round(sum(float(row.get("grand_total") or 0) for row in rows), 2),
		"filters": {
			"status": status or None,
			"supplier": supplier or None,
			"purchase_order": purchase_order or None,
			"company": company,
			"from_date": from_date,
			"to_date": to_date,
		},
	}
	return ToolResult(data, f"{len(rows)} Purchase Receipt(s)")


def submit_purchase_receipt(args: dict) -> ToolResult:
	"""Submit a DRAFT Purchase Receipt (docstatus 0 → 1).

	On a real ERPNext site this is what creates the Stock Ledger Entries that
	move the received quantity into the warehouse — a side effect of
	`doc.submit()` running ERPNext's own Stock module hooks, not anything this
	tool computes itself.
	"""
	compat.require_doctype(PURCHASE_RECEIPT, "It ships with ERPNext's Stock/Buying module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PURCHASE_RECEIPT, name):
		raise ToolError(f"no Purchase Receipt called {name!r} on this site.")
	doc = frappe.get_doc(PURCHASE_RECEIPT, name)
	if int(doc.docstatus or 0) == 1:
		raise ToolError(f"Purchase Receipt {name} is already submitted")
	if int(doc.docstatus or 0) == 2:
		raise ToolError(f"Purchase Receipt {name} is cancelled and cannot be submitted")

	doc.submit()
	doc.reload()
	data = {
		"name": doc.name,
		"docstatus": 1,
		"docstatus_label": "submitted",
		"status": doc.get("status"),
		"company": doc.get("company"),
		"supplier": doc.get("supplier"),
		"grand_total": doc.get("grand_total"),
		# v0.69.0. ALWAYS PRESENT. What happened to the stock, including — and
		# especially — the case where this tool wrote nothing because ERPNext had
		# already done it.
		"inbound_stock": _inbound_stock(doc),
	}
	return ToolResult(
		data,
		f"submitted Purchase Receipt {doc.name} ({doc.get('supplier')}, {doc.get('grand_total')})",
		docstatus_delta="0 → 1 (submitted)",
	)


def _inbound_stock(doc) -> dict:
	"""Put what arrived into the warehouse, unless submitting already did. Never raises.

	v0.69.0, AND THE GUARD IS THE INTERESTING HALF. On a site with ERPNext's
	Stock module, submitting a Purchase Receipt POSTS ITS OWN STOCK LEDGER
	ENTRIES — that is what submission means for this doctype, and the docstring
	above has said so since v0.68.0. Writing a Material Receipt on top of it
	would put every delivery into the warehouse twice, which is a worse inventory
	than no automation at all and is exactly the kind of error nobody finds until
	a count.

	So the mirror entry is written only where the receipt did NOT post: a site
	without the stock ledger, a receipt of non-stock items, an install where
	Purchase Receipt records what arrived rather than moving it. `stock_bridge.
	receipt_already_posted` decides, off the LEDGER rather than off a version
	number, and it answers "already posted" for anything it cannot determine —
	erring towards writing nothing, because a receipt somebody enters by hand is
	recoverable and a doubled balance is a physical count.

	EITHER WAY THE ANSWER IS REPORTED. "ERPNext moved it" and "we moved it" are
	both good outcomes and they are different facts; a caller that could not tell
	them apart would have to guess which of two shapes of ledger it is reading.
	"""
	from .. import stock_bridge

	name = str(doc.name)
	try:
		if stock_bridge.receipt_already_posted(name):
			return {
				"created": [],
				"warnings": [],
				"posted_by": "purchase_receipt",
				"note": (
					"Submitting this receipt posted its own Stock Ledger Entries, so the goods are "
					"already in the warehouse and nothing further was written. A second Material "
					"Receipt on top of that would count every delivery twice."
				),
			}
		lines = stock_bridge.receipt_lines(doc)
		moved = stock_bridge.receive_materials(
			lines,
			company=str(doc.get("company") or ""),
			source_doctype=PURCHASE_RECEIPT,
			source_name=name,
			posting_datetime=str(doc.get("posting_date") or ""),
		)
	except Exception as exc:  # a submitted receipt is never unsubmitted over this
		return {
			"created": [],
			"warnings": [
				f"the inbound stock movement did not run: {type(exc).__name__}: {exc}. THE RECEIPT "
				"IS SUBMITTED — this is about the warehouse, not about the document."
			],
			"posted_by": "nobody",
			"note": "Move the received quantities into the warehouse by hand.",
		}
	return {
		"created": moved["stock_entries"],
		"warnings": moved["warnings"],
		"posted_by": "erpnext_mcp" if moved["moved"] else "nobody",
		"note": (
			f"This site's Purchase Receipt posts no stock ledger entry of its own, so "
			f"{moved['moved']} of {moved['requested']} line(s) were put away as Material Receipts "
			f"tagged back to {name}."
			if moved["requested"]
			else "This receipt has no stock lines to put away."
		),
	}


# ── Purchase Invoice ─────────────────────────────────────────────────────────


def _pi_lines(raw, company: str) -> list[dict]:
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"items must be a non-empty list of objects, each with item_code, qty, rate and "
			'expense_account, e.g. [{"item_code": "SPRAY-01", "qty": 10, "rate": 12.5, '
			'"expense_account": "5100 - Office Supplies - CO"}]'
		)
	lines = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"items[{index}] must be an object, got {type(entry).__name__}")
		item_code = _resolve_item(
			as_str(entry, "item_code") or as_str(entry, "item"), f"items[{index}].item_code"
		)
		qty = as_float(entry.get("qty"), f"items[{index}].qty")
		rate = as_float(entry.get("rate"), f"items[{index}].rate")
		if qty <= 0:
			raise ToolError(f"items[{index}].qty must be positive, got {qty}")
		if rate < 0:
			raise ToolError(f"items[{index}].rate cannot be negative, got {rate}")
		expense_account = as_str(entry, "expense_account")
		if not expense_account:
			raise ToolError(f"items[{index}].expense_account is required.")
		line = {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"amount": round(qty * rate, 2),
			"expense_account": resolve_account(expense_account, company),
		}
		cost_center = as_str(entry, "cost_center")
		if cost_center:
			line["cost_center"] = resolve_cost_center(cost_center, company)
		warehouse = as_str(entry, "warehouse")
		if warehouse:
			line["warehouse"] = _resolve_warehouse(warehouse, company, f"items[{index}].warehouse")
		for ref_field, ref_doctype in (
			("purchase_order", PURCHASE_ORDER),
			("purchase_receipt", PURCHASE_RECEIPT),
		):
			value = as_str(entry, ref_field)
			if value:
				if not frappe.db.exists(ref_doctype, value):
					raise ToolError(f"items[{index}].{ref_field} {value!r} does not exist.")
				line[ref_field] = value
		lines.append(line)
	return lines


def create_purchase_invoice(args: dict) -> ToolResult:
	"""Create a DRAFT Purchase Invoice against a Supplier. Never submits.

	Optionally linked to `purchase_order` and/or `purchase_receipt` for
	provenance — ERPNext does not require either, and this tool does not either;
	a utility bill has no receipt behind it at all.
	"""
	compat.require_doctype(PURCHASE_INVOICE, "It ships with ERPNext's Accounts module.")
	company = resolve_company(as_str(args, "company"), required=True)
	supplier = _resolve_supplier(args)
	posting_date = as_date(args, "posting_date") or frappe.utils.today()
	due_date = as_date(args, "due_date")
	bill_no = as_str(args, "bill_no")
	bill_date = as_date(args, "bill_date")

	for ref_field, ref_doctype in (
		("purchase_order", PURCHASE_ORDER),
		("purchase_receipt", PURCHASE_RECEIPT),
	):
		value = as_str(args, ref_field)
		if value and not frappe.db.exists(ref_doctype, value):
			raise ToolError(f"no {ref_doctype} called {value!r} on this site.")

	credit_to = _resolve_payable_account(as_str(args, "credit_to"), company)
	items = _pi_lines(args.get("items"), company)

	doc = frappe.new_doc(PURCHASE_INVOICE)
	doc.company = company
	doc.supplier = supplier
	doc.posting_date = posting_date
	doc.credit_to = credit_to
	if due_date:
		doc.due_date = due_date
	if bill_no:
		doc.bill_no = bill_no
	if bill_date:
		doc.bill_date = bill_date
	if as_str(args, "purchase_order"):
		doc.set("purchase_order", as_str(args, "purchase_order"))
	if as_str(args, "purchase_receipt"):
		doc.set("purchase_receipt", as_str(args, "purchase_receipt"))
	for line in items:
		doc.append("items", line)
	doc.flags.ignore_permissions = True
	doc.insert()

	grand_total = round(sum(line["amount"] for line in items), 2)
	data = {
		"name": doc.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"supplier": supplier,
		"posting_date": str(posting_date),
		"due_date": str(due_date) if due_date else None,
		"credit_to": credit_to,
		"grand_total": grand_total,
		"outstanding_amount": doc.get("outstanding_amount", grand_total),
		"item_count": len(items),
		"status": doc.get("status") or "Draft",
		"next_step": (
			"This is a draft and affects no balance. Submit it in ERPNext, or via "
			"submit_purchase_invoice if that tool is enabled."
		),
	}
	return ToolResult(
		data,
		f"created draft Purchase Invoice {doc.name} for {supplier} ({company}): "
		f"{len(items)} line(s), {grand_total}",
		docstatus_delta="none → 0 (draft)",
	)


def _pi_items_out(doc) -> list[dict]:
	rows = []
	for row in doc.get("items") or []:
		rows.append(
			{
				"item_code": row.get("item_code"),
				"item_name": row.get("item_name"),
				"qty": float(row.get("qty") or 0),
				"rate": float(row.get("rate") or 0),
				"amount": float(row.get("amount") or 0),
				"expense_account": row.get("expense_account"),
				"cost_center": row.get("cost_center"),
				"warehouse": row.get("warehouse"),
				"purchase_order": row.get("purchase_order"),
				"purchase_receipt": row.get("purchase_receipt"),
			}
		)
	return rows


def get_purchase_invoice(args: dict) -> ToolResult:
	"""One Purchase Invoice in full, including its line items."""
	compat.require_doctype(PURCHASE_INVOICE, "It ships with ERPNext's Accounts module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PURCHASE_INVOICE, name):
		raise ToolError(f"no Purchase Invoice called {name!r} on this site.")
	doc = frappe.get_doc(PURCHASE_INVOICE, name)

	fields = compat.existing_fields(
		PURCHASE_INVOICE,
		[
			"name",
			"supplier",
			"supplier_name",
			"company",
			"posting_date",
			"due_date",
			"bill_no",
			"bill_date",
			"credit_to",
			"currency",
			"status",
			"docstatus",
			"grand_total",
			"outstanding_amount",
			"purchase_order",
			"purchase_receipt",
			"owner",
		],
	)
	data = {field: doc.get(field) for field in fields}
	data["docstatus_label"] = {0: "draft", 1: "submitted", 2: "cancelled"}.get(int(doc.get("docstatus") or 0))
	data["items"] = _pi_items_out(doc)
	return ToolResult(
		data,
		f"Purchase Invoice {doc.name}: {doc.get('supplier')}, {doc.get('grand_total')}, {doc.get('status')}",
	)


def list_purchase_invoices(args: dict) -> ToolResult:
	"""Purchase Invoice headers by supplier, status, company, date range and outstanding amount."""
	compat.require_doctype(PURCHASE_INVOICE, "It ships with ERPNext's Accounts module.")
	status = as_str(args, "status")
	supplier = as_str(args, "supplier")
	company = resolve_company(as_str(args, "company"))
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	outstanding_only = args.get("outstanding_only")
	limit = as_limit(args)

	filters: dict = {}
	if status:
		filters["status"] = status
	if supplier:
		filters["supplier"] = supplier
	if company:
		filters["company"] = company
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["posting_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["posting_date"] = (">=", from_date)
	elif to_date:
		filters["posting_date"] = ("<=", to_date)
	if outstanding_only:
		filters["outstanding_amount"] = (">", 0)

	fields = compat.existing_fields(
		PURCHASE_INVOICE,
		[
			"name",
			"supplier",
			"supplier_name",
			"posting_date",
			"due_date",
			"grand_total",
			"outstanding_amount",
			"currency",
			"status",
			"docstatus",
			"company",
			"owner",
		],
	)
	rows = frappe.db.get_all(
		PURCHASE_INVOICE,
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit=limit,
	)
	for row in rows:
		row["docstatus_label"] = {0: "draft", 1: "submitted", 2: "cancelled"}.get(
			int(row.get("docstatus") or 0)
		)

	data = {
		"invoices": rows,
		"count": len(rows),
		"limit": limit,
		"truncated": len(rows) == limit,
		"total_value": round(sum(float(row.get("grand_total") or 0) for row in rows), 2),
		"total_outstanding": round(sum(float(row.get("outstanding_amount") or 0) for row in rows), 2),
		"filters": {
			"status": status or None,
			"supplier": supplier or None,
			"company": company,
			"from_date": from_date,
			"to_date": to_date,
			"outstanding_only": bool(outstanding_only),
		},
	}
	return ToolResult(data, f"{len(rows)} Purchase Invoice(s)")


def submit_purchase_invoice(args: dict) -> ToolResult:
	"""Submit a DRAFT Purchase Invoice (docstatus 0 → 1). Creates GL Entries for AP.

	This is the tool that moves a balance: on a real site, `doc.submit()` runs
	ERPNext's own Purchase Invoice controller, which books the expense side of
	every line and credits `credit_to` for the total — exactly what a human
	submitting the same invoice in the Desk would trigger, and nothing this tool
	computes itself.
	"""
	compat.require_doctype(PURCHASE_INVOICE, "It ships with ERPNext's Accounts module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PURCHASE_INVOICE, name):
		raise ToolError(f"no Purchase Invoice called {name!r} on this site.")
	doc = frappe.get_doc(PURCHASE_INVOICE, name)
	if int(doc.docstatus or 0) == 1:
		raise ToolError(f"Purchase Invoice {name} is already submitted")
	if int(doc.docstatus or 0) == 2:
		raise ToolError(f"Purchase Invoice {name} is cancelled and cannot be submitted")

	doc.submit()
	doc.reload()
	data = {
		"name": doc.name,
		"docstatus": 1,
		"docstatus_label": "submitted",
		"status": doc.get("status"),
		"company": doc.get("company"),
		"supplier": doc.get("supplier"),
		"grand_total": doc.get("grand_total"),
		"outstanding_amount": doc.get("outstanding_amount"),
		"gl_entries_created": frappe.db.count(
			"GL Entry", {"voucher_type": "Purchase Invoice", "voucher_no": doc.name}
		),
	}
	return ToolResult(
		data,
		f"submitted Purchase Invoice {doc.name} ({doc.get('supplier')}, {doc.get('grand_total')})",
		docstatus_delta="0 → 1 (submitted)",
	)


#: The register `create_purchase_invoice_from_receipt` links back from. Named
#: here rather than imported, because `receipts` imports this module.
EXPENSE_RECEIPT = "Expense Receipt"


def delete_draft_purchase_invoice(args: dict) -> ToolResult:
	"""Delete a DRAFT Purchase Invoice outright. Drafts only, and it says what it deleted.

	THE GAP THIS FILLS. An OCR misread makes `create_purchase_invoice_from_receipt`
	put a draft against the wrong Supplier, and there was no MCP path to withdraw
	it — only the Desk. `delete_draft_journal_entry` closed the same gap for
	journal entries and this is that tool for invoices: drafts, and only drafts.
	A submitted invoice has posted to AP and gets cancelled, never deleted.

	THE RECEIPT IS RELEASED FIRST, AND IT HAS TO BE. `Expense Receipt.linked_document`
	is a Dynamic Link, so Frappe's `delete_doc` refuses the delete while a receipt
	still points here — and `create_purchase_invoice_from_receipt` refuses a
	receipt that has one. Clearing `linked_doctype`/`linked_document` BEFORE the
	delete is what makes both work; the dispatcher rolls the whole call back if
	the delete then fails, so a receipt is never left released from an invoice
	that still exists.

	THE RECEIPT'S `supplier` IS NOT TOUCHED. The pipeline wrote the Supplier it
	matched onto the receipt, and it prefers that link over its own `supplier`
	argument on the next run. Whether the link is the misread or a person's
	deliberate choice is not something this tool can tell, so it reports the
	link on every released receipt and `next_step` says how to change it.

	IT IS A REAL DELETE, so `reason` is mandatory and the response carries the
	company, supplier, date, total and every line: once this returns, the MCP
	Action Log row is the only description of the document that ever existed.
	"""
	compat.require_doctype(PURCHASE_INVOICE, "It ships with ERPNext's Accounts module.")
	name = as_str(args, "name", required=True)
	reason = as_str(args, "reason", required=True)
	if len(reason) < 4:
		raise ToolError("reason must be a real explanation, not a placeholder. Nothing was deleted.")
	if not frappe.db.exists(PURCHASE_INVOICE, name):
		raise ToolError(f"no Purchase Invoice called {name!r} on this site. Nothing was deleted.")
	doc = frappe.get_doc(PURCHASE_INVOICE, name)
	docstatus = int(doc.docstatus or 0)
	if docstatus == 1:
		raise ToolError(
			f"Purchase Invoice {name} is submitted: it has posted the expense and the payable, "
			"and deleting it would take those balances with it and leave nothing saying why. "
			"Cancel it in ERPNext, which writes the reversing entries and keeps the record. "
			"Nothing was deleted."
		)
	if docstatus == 2:
		raise ToolError(
			f"Purchase Invoice {name} is cancelled. ERPNext keeps cancelled invoices and their "
			"reversing GL rows on purpose — the pair is the evidence that a posting was made "
			"and undone. Deleting one leaves an audit trail with a hole in it. Nothing was "
			"deleted."
		)

	# Read everything worth keeping BEFORE the document stops existing.
	items = doc.get("items") or []
	deleted = {
		"name": doc.name,
		"company": doc.get("company"),
		"supplier": doc.get("supplier"),
		"supplier_name": doc.get("supplier_name"),
		"posting_date": str(doc.get("posting_date") or ""),
		"bill_no": doc.get("bill_no"),
		"grand_total": float(doc.get("grand_total") or 0),
		"line_count": len(items),
		"items": [
			{
				"item_code": row.get("item_code"),
				"qty": float(row.get("qty") or 0),
				"rate": float(row.get("rate") or 0),
				"amount": float(row.get("amount") or 0),
				"expense_account": row.get("expense_account"),
				"cost_center": row.get("cost_center"),
			}
			for row in items
		],
	}

	released = []
	if compat.doctype_exists(EXPENSE_RECEIPT):
		released = frappe.db.get_all(
			EXPENSE_RECEIPT,
			filters={"linked_doctype": PURCHASE_INVOICE, "linked_document": name},
			fields=["name", "merchant", "amount", "supplier"],
			order_by="name asc",
		)
		for row in released:
			frappe.db.set_value(EXPENSE_RECEIPT, row["name"], {"linked_doctype": "", "linked_document": ""})

	frappe.delete_doc(PURCHASE_INVOICE, name, ignore_permissions=False)

	receipts = [
		{
			"name": row["name"],
			"merchant": row.get("merchant"),
			"amount": float(row.get("amount") or 0),
			"supplier": row.get("supplier"),
		}
		for row in released
	]
	data = {
		"deleted": deleted,
		"reason": reason,
		"gl_entries_removed": 0,
		"receipts_released": receipts,
		"note": (
			"A draft writes no GL Entries, so no balance changed when this was created and none "
			"changed when it was deleted. The MCP Action Log row for this call is now the only "
			"record that the invoice existed; it carries the lines above and this reason."
		),
		"next_step": (
			"The receipt(s) above no longer name an invoice, so create_purchase_invoice_from_receipt "
			"will take them again. It uses each receipt's own `supplier` link ahead of its "
			"`supplier` argument: if that link is the misread, correct it with "
			"update_expense_receipt(name=…, supplier=…) first."
			if receipts
			else "No Expense Receipt was linked to this invoice, so nothing else changed."
		),
	}
	return ToolResult(
		data,
		f"deleted draft Purchase Invoice {name} ({deleted['company']}, {deleted['supplier']}, "
		f"{deleted['posting_date']}, {deleted['grand_total']}, {deleted['line_count']} line(s); "
		f"{len(receipts)} receipt(s) released) — {reason}",
		docstatus_delta="0 (draft) → deleted",
	)


# ── Payment Entry ────────────────────────────────────────────────────────────


def _pe_references(raw, supplier: str) -> list[dict]:
	if raw in (None, "", []):
		return []
	if not isinstance(raw, list):
		raise ToolError(
			"references must be a list of objects, each with reference_name and "
			'allocated_amount, e.g. [{"reference_name": "ACC-PINV-2026-00001", '
			'"allocated_amount": 500}]'
		)
	rows = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"references[{index}] must be an object, got {type(entry).__name__}")
		reference_doctype = as_str(entry, "reference_doctype") or "Purchase Invoice"
		if reference_doctype not in PAYABLE_REFERENCE_DOCTYPES:
			raise ToolError(
				f"references[{index}].reference_doctype must be {PAYABLE_REFERENCE_DOCTYPES[0]!r}, "
				f"got {reference_doctype!r}."
			)
		reference_name = as_str(entry, "reference_name", required=True)
		if not frappe.db.exists(reference_doctype, reference_name):
			raise ToolError(f"references[{index}]: no {reference_doctype} called {reference_name!r}.")
		invoice = frappe.db.get_value(
			reference_doctype,
			reference_name,
			["supplier", "docstatus", "outstanding_amount", "due_date"],
			as_dict=True,
		)
		if invoice.docstatus != 1:
			raise ToolError(f"references[{index}]: {reference_name} is not submitted. Nothing was created.")
		if invoice.supplier != supplier:
			raise ToolError(
				f"references[{index}]: {reference_name} is billed to {invoice.supplier!r}, not {supplier!r}."
			)
		allocated = as_float(entry.get("allocated_amount"), f"references[{index}].allocated_amount")
		if allocated <= 0:
			raise ToolError(f"references[{index}].allocated_amount must be positive, got {allocated}")
		outstanding = float(invoice.outstanding_amount or 0)
		if allocated - outstanding > 0.005:
			raise ToolError(
				f"references[{index}]: allocated_amount {allocated} exceeds {reference_name}'s "
				f"outstanding_amount {outstanding}. Nothing was created."
			)
		rows.append(
			{
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"due_date": invoice.due_date,
				"total_amount": outstanding,
				"outstanding_amount": outstanding,
				"allocated_amount": round(allocated, 2),
			}
		)
	return rows


def create_payment_entry(args: dict) -> ToolResult:
	"""Create a DRAFT Payment Entry paying a Supplier. Never submits.

	`payment_type` is always "Pay" and `party_type` is always "Supplier" — this
	tool is the AP side only, on purpose: a Receive-type Payment Entry settles a
	Sales Invoice and belongs with the receivables tools, not here.

	`references` allocates the payment across one or more submitted Purchase
	Invoices for the same supplier; a caller may allocate less than the full
	outstanding amount on any of them (a partial payment), and less than
	`paid_amount` in total (an on-account payment with no reference at all).
	"""
	compat.require_doctype(PAYMENT_ENTRY, "It ships with ERPNext's Accounts module.")
	company = resolve_company(as_str(args, "company"), required=True)
	supplier = _resolve_supplier(args)
	posting_date = as_date(args, "posting_date") or frappe.utils.today()
	paid_amount = as_float(args.get("paid_amount"), "paid_amount")
	if paid_amount <= 0:
		raise ToolError("paid_amount must be positive.")

	paid_to = _resolve_payable_account(as_str(args, "paid_to") or as_str(args, "credit_to"), company)
	paid_from = _resolve_cash_account(as_str(args, "paid_from"), company, "paid_from")
	references = _pe_references(args.get("references"), supplier)

	total_allocated = round(sum(row["allocated_amount"] for row in references), 2)
	if total_allocated - paid_amount > 0.005:
		raise ToolError(
			f"references allocate {total_allocated} but paid_amount is {paid_amount}. Nothing was created."
		)

	doc = frappe.new_doc(PAYMENT_ENTRY)
	doc.payment_type = "Pay"
	doc.party_type = "Supplier"
	doc.party = supplier
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

	data = {
		"name": doc.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"supplier": supplier,
		"posting_date": str(posting_date),
		"paid_amount": paid_amount,
		"paid_from": paid_from,
		"paid_to": paid_to,
		"allocated_total": total_allocated,
		"unallocated_amount": round(paid_amount - total_allocated, 2),
		"references": [
			{"reference_name": r["reference_name"], "allocated_amount": r["allocated_amount"]}
			for r in references
		],
		"next_step": (
			"This is a draft and affects no balance. Submit it in ERPNext, or via "
			"submit_payment_entry if that tool is enabled."
		),
	}
	return ToolResult(
		data,
		f"created draft Payment Entry {doc.name}: {paid_amount} to {supplier} ({company}), "
		f"{len(references)} reference(s) allocated {total_allocated}",
		docstatus_delta="none → 0 (draft)",
	)


def _pe_out(doc) -> dict:
	fields = compat.existing_fields(
		PAYMENT_ENTRY,
		[
			"name",
			"payment_type",
			"party_type",
			"party",
			"party_name",
			"company",
			"posting_date",
			"paid_amount",
			"paid_from",
			"paid_to",
			"reference_no",
			"reference_date",
			"mode_of_payment",
			"docstatus",
		],
	)
	data = {field: doc.get(field) for field in fields}
	data["docstatus_label"] = {0: "draft", 1: "submitted", 2: "cancelled"}.get(int(doc.get("docstatus") or 0))
	data["references"] = [
		{
			"reference_doctype": row.get("reference_doctype"),
			"reference_name": row.get("reference_name"),
			"allocated_amount": float(row.get("allocated_amount") or 0),
			"outstanding_amount": float(row.get("outstanding_amount") or 0),
		}
		for row in doc.get("references") or []
	]
	return data


def get_payment_entry(args: dict) -> ToolResult:
	"""One Payment Entry in full, including its invoice references."""
	compat.require_doctype(PAYMENT_ENTRY, "It ships with ERPNext's Accounts module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PAYMENT_ENTRY, name):
		raise ToolError(f"no Payment Entry called {name!r} on this site.")
	doc = frappe.get_doc(PAYMENT_ENTRY, name)
	data = _pe_out(doc)
	return ToolResult(data, f"Payment Entry {doc.name}: {doc.get('paid_amount')} to {doc.get('party')}")


def list_payment_entries(args: dict) -> ToolResult:
	"""Payment Entry headers by supplier, company, status and date range. Pay/Supplier only."""
	compat.require_doctype(PAYMENT_ENTRY, "It ships with ERPNext's Accounts module.")
	supplier = as_str(args, "supplier")
	company = resolve_company(as_str(args, "company"))
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	docstatus = args.get("docstatus")
	limit = as_limit(args)

	filters: dict = {"payment_type": "Pay", "party_type": "Supplier"}
	if supplier:
		filters["party"] = supplier
	if company:
		filters["company"] = company
	if docstatus is not None and docstatus != "":
		filters["docstatus"] = int(docstatus)
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["posting_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["posting_date"] = (">=", from_date)
	elif to_date:
		filters["posting_date"] = ("<=", to_date)

	fields = compat.existing_fields(
		PAYMENT_ENTRY,
		[
			"name",
			"party",
			"party_name",
			"posting_date",
			"paid_amount",
			"paid_from",
			"paid_to",
			"reference_no",
			"docstatus",
			"company",
		],
	)
	rows = frappe.db.get_all(
		PAYMENT_ENTRY,
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit=limit,
	)
	for row in rows:
		row["docstatus_label"] = {0: "draft", 1: "submitted", 2: "cancelled"}.get(
			int(row.get("docstatus") or 0)
		)

	data = {
		"payments": rows,
		"count": len(rows),
		"limit": limit,
		"truncated": len(rows) == limit,
		"total_paid": round(sum(float(row.get("paid_amount") or 0) for row in rows), 2),
		"filters": {
			"supplier": supplier or None,
			"company": company,
			"from_date": from_date,
			"to_date": to_date,
		},
	}
	return ToolResult(data, f"{len(rows)} Payment Entry(ies)")


def submit_payment_entry(args: dict) -> ToolResult:
	"""Submit a DRAFT Payment Entry (docstatus 0 → 1). Creates GL Entries.

	On a real site `doc.submit()` runs ERPNext's own Payment Entry controller,
	which debits `paid_to`, credits `paid_from`, and updates every referenced
	Purchase Invoice's outstanding_amount — this tool triggers that and reports
	what happened; it does not compute any of it.
	"""
	compat.require_doctype(PAYMENT_ENTRY, "It ships with ERPNext's Accounts module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(PAYMENT_ENTRY, name):
		raise ToolError(f"no Payment Entry called {name!r} on this site.")
	doc = frappe.get_doc(PAYMENT_ENTRY, name)
	if int(doc.docstatus or 0) == 1:
		raise ToolError(f"Payment Entry {name} is already submitted")
	if int(doc.docstatus or 0) == 2:
		raise ToolError(f"Payment Entry {name} is cancelled and cannot be submitted")

	doc.submit()
	doc.reload()
	data = _pe_out(doc)
	data["gl_entries_created"] = frappe.db.count(
		"GL Entry", {"voucher_type": "Payment Entry", "voucher_no": doc.name}
	)
	return ToolResult(
		data,
		f"submitted Payment Entry {doc.name} ({doc.get('paid_amount')} to {doc.get('party')})",
		docstatus_delta="0 → 1 (submitted)",
	)


# ── AP ageing ────────────────────────────────────────────────────────────────


def get_ap_aging(args: dict) -> ToolResult:
	"""Accounts Payable ageing, grouped by supplier.

	THE TOTAL comes from GL Entry against every account typed Payable for the
	company — `credit - debit` summed per supplier, which is the true ledger
	balance regardless of what wrote it: a Payment Entry, a Journal Entry
	write-off, anything. That is the number a payables review actually has to
	trust.

	THE PER-INVOICE BUCKETS come from Purchase Invoice.outstanding_amount and
	due_date, the same approach `get_outstanding_invoices` takes on the
	receivables side. GL Entry cannot supply this half on its own: a Payment
	Entry's own GL rows do not say which invoice they paid (that allocation
	lives in its `references` child table, not in the general ledger), so there
	is no reliable way to net a payment's debit against one specific invoice's
	credit by reading GL Entry alone. Trusting the invoice's own
	ERPNext-maintained column for "how much of THIS invoice is still owed" is
	correct where the ledger-wide net is not.

	THE TWO ARE CROSS-CHECKED per supplier, and a mismatch is reported rather
	than hidden — because a real one usually means an entry outside the normal
	Purchase Invoice → Payment Entry flow (a manual Journal Entry against the
	Payable account) that the invoice-level view cannot see.
	"""
	compat.require_doctype("Purchase Invoice", "It ships with ERPNext's Accounts module.")
	compat.require_doctype("GL Entry", "It ships with ERPNext's Accounts module.")
	company = resolve_company(as_str(args, "company"), required=True)
	supplier = as_str(args, "supplier")
	as_of = as_date(args, "as_of") or frappe.utils.today()
	limit = as_limit(args)

	payable_accounts = frappe.db.get_all(
		"Account", filters={"company": company, "account_type": "Payable"}, pluck="name"
	)
	if not payable_accounts:
		data = {
			"suppliers": [],
			"count": 0,
			"as_of": as_of,
			"company": company,
			"total_outstanding": 0.0,
			"buckets": {},
			"note": f"{company} has no Account typed Payable, so there is nothing to age.",
		}
		return ToolResult(data, f"no Payable accounts for {company}")

	gl_filters: dict = {"company": company, "account": ("in", payable_accounts), "party_type": "Supplier"}
	if compat.has_field("GL Entry", "is_cancelled"):
		gl_filters["is_cancelled"] = 0
	if supplier:
		gl_filters["party"] = supplier
	gl_rows = frappe.db.get_all(
		"GL Entry", filters=gl_filters, fields=["party", "debit", "credit"], limit=5000
	)

	gl_balance: dict[str, float] = {}
	for row in gl_rows:
		party = row["party"]
		gl_balance[party] = round(
			gl_balance.get(party, 0.0) + float(row.get("credit") or 0) - float(row.get("debit") or 0), 2
		)

	pi_filters: dict = {"company": company, "docstatus": 1, "outstanding_amount": (">", 0.005)}
	if supplier:
		pi_filters["supplier"] = supplier
	invoices = frappe.db.get_all(
		"Purchase Invoice",
		filters=pi_filters,
		fields=["name", "supplier", "due_date", "outstanding_amount"],
		order_by="due_date asc",
		limit=limit,
	)

	by_supplier: dict[str, dict] = {}
	buckets_total = {label: {"count": 0, "outstanding": 0.0} for label, _ in AGEING_BUCKETS}
	buckets_total["current"] = {"count": 0, "outstanding": 0.0}
	buckets_total["unknown"] = {"count": 0, "outstanding": 0.0}
	subledger_total = 0.0

	for invoice in invoices:
		party = invoice["supplier"]
		outstanding = round(float(invoice.get("outstanding_amount") or 0), 2)
		days = _days_overdue(invoice.get("due_date"), as_of)
		label = _bucket(days)

		entry = by_supplier.setdefault(
			party, {"supplier": party, "outstanding": 0.0, "invoices": [], "buckets": {}}
		)
		entry["outstanding"] = round(entry["outstanding"] + outstanding, 2)
		entry["invoices"].append(
			{
				"name": invoice["name"],
				"due_date": invoice.get("due_date"),
				"outstanding_amount": outstanding,
				"days_overdue": days,
				"ageing_bucket": label,
			}
		)
		entry["buckets"].setdefault(label, {"count": 0, "outstanding": 0.0})
		entry["buckets"][label]["count"] += 1
		entry["buckets"][label]["outstanding"] = round(
			entry["buckets"][label]["outstanding"] + outstanding, 2
		)

		buckets_total[label]["count"] += 1
		buckets_total[label]["outstanding"] = round(buckets_total[label]["outstanding"] + outstanding, 2)
		subledger_total += outstanding

	for party, entry in by_supplier.items():
		ledger = gl_balance.get(party, 0.0)
		entry["gl_balance"] = ledger
		drift = round(ledger - entry["outstanding"], 2)
		if abs(drift) > 0.005:
			entry["drift"] = drift
			entry["drift_note"] = (
				f"GL Entry shows {ledger} owed to {party} but open Purchase Invoices only "
				f"account for {entry['outstanding']}. The {drift} difference is something posted "
				"to the Payable account outside the normal Purchase Invoice → Payment Entry flow — "
				"a manual Journal Entry against it is the usual cause."
			)

	suppliers = sorted(by_supplier.values(), key=lambda row: row["outstanding"], reverse=True)
	truncated = len(invoices) == limit

	data = {
		"suppliers": suppliers,
		"count": len(suppliers),
		"as_of": as_of,
		"company": company,
		"total_outstanding": round(subledger_total, 2),
		"gl_total_outstanding": round(sum(gl_balance.values()), 2),
		"buckets": buckets_total,
		"truncated": truncated,
		"filters": {"supplier": supplier or None, "company": company},
		"bucket_definition": (
			"days_overdue = as_of - due_date. 'current' is not yet due (days_overdue <= 0); "
			"'0-30', '31-60', '61-90' and '90+' are days past due; 'unknown' is an invoice with no "
			"due_date. total_outstanding sums open Purchase Invoices' own outstanding_amount; "
			"gl_total_outstanding is the true ledger balance from GL Entry against every account "
			"typed Payable. A per-supplier 'drift' field means the two disagree for that supplier."
		),
	}
	return ToolResult(
		data, f"{len(suppliers)} supplier(s) owed {round(subledger_total, 2)} total as of {as_of}"
	)


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
