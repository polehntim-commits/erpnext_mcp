# SPDX-License-Identifier: MIT
"""Receipt capture — scale tickets, settlement statements, and what a photo is.

v0.67.0. Sprint 2 of the gap-closure plan, and the design principle behind it is
one sentence: **the receipt is the financial atom**. A foreman photographs a piece
of paper. Which piece of paper it is decides which register it lands in, and that
decision is the only branch in an otherwise identical flow — photograph, extract,
file, review. Four kinds of paper, one capture:

    a fuel slip           → Expense Receipt   (v0.31.0, enhanced here)
    a scale ticket        → Scale Ticket      (this release)
    a packout settlement  → Settlement Statement (this release)
    a vendor invoice      → Purchase Invoice  (a later sprint)

`classify_receipt` is the branch. Everything else here is the two new registers.

────────────────────────────────────────────────────────────────────────────
WHY A SCALE TICKET IS ITS OWN DOCTYPE AND NOT A DELIVERY NOTE
────────────────────────────────────────────────────────────────────────────

ERPNext has a Delivery Note, and it is the wrong shape for this. A Delivery Note
is the *seller's* document about goods leaving on the seller's terms, priced,
against a Sales Order, in Items and UOMs the seller controls. A scale ticket is
a third party's weight record: the packer owns the scale, the packer prints the
slip, the variety is whatever the packer's clerk wrote, and there is no price on
it at all — the price arrives months later on a settlement.

Forcing that onto a Delivery Note would mean inventing an Item per variety per
grade before a foreman could photograph a slip at a tailgate, and a capture that
requires master data to exist first is a capture that does not happen. So the
ticket is stored as what it is, and the connection to priced, Item-shaped
documents is made later by the settlement — where the packer has finally said
what the fruit was worth.

────────────────────────────────────────────────────────────────────────────
THE TWO REGISTERS ARE INDEPENDENT ON PURPOSE
────────────────────────────────────────────────────────────────────────────

A Settlement Statement's `gross_delivered_weight` is what the PACKER says
arrived. The sum of the Scale Tickets matched to it is what the GROWER's own
copies say arrived. Nothing in this module reconciles them, overwrites one with
the other, or refuses a settlement whose figure disagrees.

`get_settlement_statement` reports both and names the difference, because the
difference IS the answer — it is the entire reason a grower keeps ticket stubs,
and a tool that quietly agreed them would delete the only audit this document
has.

────────────────────────────────────────────────────────────────────────────
CREATE IS ALWAYS A DRAFT; SUBMIT IS A SEPARATE TOOL AND A SEPARATE SWITCH
────────────────────────────────────────────────────────────────────────────

Both doctypes are submittable, and both `create_*` tools leave the document at
docstatus 0. That is not timidity about writes — it is that submitting a scale
ticket makes a third party's weight record immutable, and an operator who wants
a phone to be able to capture tickets does not necessarily want the same phone
able to freeze them. One switch cannot express that; two can.
"""

from __future__ import annotations

import difflib
import re

import frappe
from frappe.utils import today

from .. import compat
from ..args import as_date, as_float, as_limit, as_str, resolve_account, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import expenses, masters, purchasing

SCALE_TICKET = "Scale Ticket"
SETTLEMENT_STATEMENT = "Settlement Statement"
PURCHASE_INVOICE = "Purchase Invoice"
CUSTOMER = "Customer"
FIELD = "Field"
CUSTOM_FIELD = "Custom Field"
MERCHANT_ALIAS = "Merchant Alias"

#: Scale Ticket statuses, and the Settlement Statement's. Both are computed by
#: their controllers from docstatus; these exist so a filter argument can be
#: refused with the list rather than silently matching nothing.
TICKET_STATUSES = ("Draft", "Submitted", "Matched", "Cancelled")
SETTLEMENT_STATUSES = ("Draft", "Submitted", "Posted", "Cancelled")

#: One unit for all the weights on one document. Mirrored from the two DocType
#: JSONs and asserted equal to them by the tests.
WEIGHT_UOMS = ("Kg", "Lb", "Ton", "Bin", "Box")

DEDUCTION_TYPES = ("Packing", "Cold Storage", "Marketing", "Commission", "Other")

#: What every Scale Ticket read returns. `ticket_image` and `notes` are included
#: because a list of tickets is something a bookkeeper scans for the ones that
#: need a photo opened, exactly as with expense receipts.
_TICKET_FIELDS = (
	"name",
	"ticket_number",
	"date",
	"customer",
	"company",
	"variety",
	"grade",
	"gross_weight",
	"tare_weight",
	"net_weight",
	"weight_uom",
	"field",
	"block",
	"truck_id",
	"driver",
	"destination",
	"status",
	"settlement",
	"ticket_image",
	"notes",
	"docstatus",
	"modified",
)

_SETTLEMENT_FIELDS = (
	"name",
	"statement_number",
	"date",
	"customer",
	"company",
	"period_start",
	"period_end",
	"gross_delivered_weight",
	"packed_weight",
	"packout_pct",
	"cull_weight",
	"cull_pct",
	"weight_uom",
	"total_gross_revenue",
	"total_deductions",
	"net_proceeds",
	"status",
	"posted_journal_entry",
	"statement_image",
	"notes",
	"docstatus",
	"modified",
)

_FLOAT_FIELDS = frozenset(
	{
		"gross_weight",
		"tare_weight",
		"net_weight",
		"gross_delivered_weight",
		"packed_weight",
		"cull_weight",
		"packout_pct",
		"cull_pct",
		"total_gross_revenue",
		"total_deductions",
		"net_proceeds",
	}
)

_DATE_FIELDS = frozenset({"date", "period_start", "period_end", "modified"})


# ── helpers ───────────────────────────────────────────────────────────────


def _row_out(row: dict) -> dict:
	"""One document as JSON: dates as ISO strings, weights and money as numbers."""
	out = {}
	for key, value in dict(row).items():
		if value is None:
			out[key] = None
		elif key in _FLOAT_FIELDS:
			out[key] = float(value or 0)
		elif key == "docstatus":
			out[key] = int(value or 0)
		elif key in _DATE_FIELDS:
			out[key] = str(value)
		else:
			out[key] = value
	return out


def _require(doctype: str, args: dict, *aliases: str) -> str:
	"""The docname this call is about, or a refusal naming why."""
	name = ""
	for key in ("name", *aliases):
		name = as_str(args, key)
		if name:
			break
	if not name:
		raise ToolError(f"name (the {doctype} docname) is required.")
	if not frappe.db.exists(doctype, name):
		raise ToolError(f"no {doctype} called {name!r} on this site.")
	return name


def _customer(value: str, required: bool = False) -> str:
	"""A Customer docname from a docname or a customer_name.

	A phone knows the packer as the name on the ticket; the register knows it as
	a docname, and on a stock ERPNext site those are the same string — until
	somebody enables customer naming by series, at which point they never are
	again. Both are accepted, and neither is guessed at.
	"""
	if not value:
		if required:
			raise ToolError("customer is required — a scale ticket is a delivery TO somebody.")
		return ""
	if frappe.db.exists(CUSTOMER, value):
		return str(value)
	found = frappe.db.get_value(CUSTOMER, {"customer_name": value}, "name")
	if found:
		return str(found)
	raise ToolError(f"no Customer called {value!r} on this site. Create the packer first.")


def _weight_uom(args: dict, default: str = "Kg") -> str:
	value = as_str(args, "weight_uom") or default
	if value not in WEIGHT_UOMS:
		raise ToolError(f"weight_uom must be one of: {', '.join(WEIGHT_UOMS)}.")
	return value


def _status_filter(args: dict, allowed) -> str:
	status = as_str(args, "status")
	if status and status not in allowed:
		raise ToolError(f"status must be one of: {', '.join(allowed)}.")
	return status


def _date_range(args: dict, filters: dict, fieldname: str = "date") -> tuple:
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters[fieldname] = ("between", [from_date, to_date])
	elif from_date:
		filters[fieldname] = (">=", from_date)
	elif to_date:
		filters[fieldname] = ("<=", to_date)
	return from_date, to_date


def _limit(args: dict) -> int:
	# `as_limit` IS this: default 100, clamped to [1, 500], and it keeps an explicit 0
	# as 1 rather than swapping in the default. The `or 100` could not — and a 0 that
	# reached `limit_page_length` would have meant NO LIMIT, not no rows.
	return as_limit(args)


def _optional_field(args: dict) -> str:
	"""The `field` argument, checked against the Field register if there is one.

	A bench without the Field doctype refuses a field argument by name rather
	than writing a dangling link — and a bench WITH it refuses a field nobody
	created, which on a farm is nearly always a typo for one that exists.
	"""
	value = as_str(args, "field")
	if not value:
		return ""
	if not frappe.db.exists("DocType", FIELD):
		raise ToolError(
			"this site has no Field doctype, so a scale ticket cannot name one. "
			"Run `bench migrate`, or omit field and record the origin in block."
		)
	if not frappe.db.exists(FIELD, value):
		raise ToolError(f"no Field called {value!r} on this site.")
	return value


# ── Scale Ticket: reads ───────────────────────────────────────────────────


def list_scale_tickets(args: dict) -> ToolResult:
	"""Scale tickets by customer, company, status and delivery date range."""
	filters = {}

	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)

	customer = as_str(args, "customer")
	if customer:
		filters["customer"] = _customer(customer)

	status = _status_filter(args, TICKET_STATUSES)
	if status:
		filters["status"] = status

	variety = as_str(args, "variety")
	if variety:
		filters["variety"] = variety

	field = as_str(args, "field")
	if field:
		filters["field"] = _optional_field(args)

	settlement = as_str(args, "settlement")
	if settlement:
		filters["settlement"] = settlement

	# `unmatched=true` is the question this register exists to answer: which
	# delivered loads has nobody paid for yet. Expressed as its own argument
	# rather than as `settlement=""`, because an empty string in a filter reads
	# as "no filter" everywhere else in this catalogue.
	if args.get("unmatched") in (True, "true", "True", 1, "1"):
		filters["settlement"] = ("in", ("", None))

	from_date, to_date = _date_range(args, filters)
	limit = _limit(args)

	rows = frappe.db.get_all(
		SCALE_TICKET,
		filters=filters,
		fields=list(_TICKET_FIELDS),
		limit_page_length=limit,
		order_by="date desc, creation desc",
	)
	tickets = [_row_out(row) for row in rows]

	by_status = {}
	for ticket in tickets:
		key = ticket.get("status") or "<none>"
		by_status[key] = by_status.get(key, 0) + 1

	data = {
		"scale_tickets": tickets,
		"count": len(tickets),
		"limit": limit,
		"truncated": len(tickets) == limit,
		"total_net_weight": round(sum(t["net_weight"] for t in tickets), 3),
		"by_status": by_status,
		"by_weight_uom": _by_uom(tickets),
		"filters": {
			"customer": filters.get("customer") or None,
			"company": filters.get("company") or None,
			"status": status or None,
			"from_date": from_date,
			"to_date": to_date,
		},
		"note": (
			"total_net_weight sums net_weight across the rows returned, which is a "
			"partial figure when truncated is true — and a MEANINGLESS one when "
			"by_weight_uom names more than one unit, because kilos and bins do not add."
		),
	}
	return ToolResult(
		data=data,
		summary=f"{len(tickets)} scale ticket(s), {round(sum(t['net_weight'] for t in tickets), 3)} net",
	)


def _by_uom(rows: list) -> dict:
	"""How many rows in each weight unit. The guard on a summed total."""
	counts = {}
	for row in rows:
		key = row.get("weight_uom") or "<none>"
		counts[key] = counts.get(key, 0) + 1
	return counts


def get_scale_ticket(args: dict) -> ToolResult:
	"""One scale ticket in full, with the settlement that claimed it if any."""
	name = _require(SCALE_TICKET, args, "scale_ticket", "ticket")
	doc = frappe.get_doc(SCALE_TICKET, name)

	data = _row_out({field: doc.get(field) for field in _TICKET_FIELDS})
	data["weight_check"] = _weight_check(doc)

	if doc.get("settlement"):
		row = (
			frappe.db.get_value(
				SETTLEMENT_STATEMENT,
				doc.get("settlement"),
				["name", "statement_number", "date", "status", "net_proceeds"],
				as_dict=True,
			)
			or {}
		)
		data["settlement_detail"] = _row_out(row) if row else None

	return ToolResult(
		data=data,
		summary=f"Scale ticket {name} ({doc.get('ticket_number')}): "
		f"{doc.get('net_weight')} {doc.get('weight_uom')} of {doc.get('variety') or 'fruit'} "
		f"to {doc.get('customer')} on {doc.get('date')} — {doc.get('status')}",
	)


def _weight_check(doc) -> dict:
	"""Gross, tare and the subtraction, restated so a reader can see the sum.

	The whole disagreement a scale ticket exists to settle is arithmetic, and a
	tool that returned only the answer would be asking to be trusted about it.
	"""
	gross = float(doc.get("gross_weight") or 0)
	tare = float(doc.get("tare_weight") or 0)
	return {
		"gross_weight": gross,
		"tare_weight": tare,
		"net_weight": float(doc.get("net_weight") or 0),
		"weight_uom": doc.get("weight_uom"),
		"computed_as": f"{gross} - {tare} = {round(gross - tare, 3)}",
	}


# ── Scale Ticket: writes ──────────────────────────────────────────────────


def create_scale_ticket(args: dict) -> ToolResult:
	"""Capture one scale ticket as a draft."""
	ticket_number = as_str(args, "ticket_number", required=True)
	date = as_date(args, "date", required=True)
	company = resolve_company(as_str(args, "company"), required=True)
	customer = _customer(as_str(args, "customer"), required=True)
	weight_uom = _weight_uom(args)

	gross = _weight(args, "gross_weight")
	tare = _weight(args, "tare_weight")
	if gross and tare > gross:
		raise ToolError(
			f"tare_weight {tare} is greater than gross_weight {gross}, which would make the "
			f"net weight negative. That is two numbers off different tickets, or a gross and "
			f"a tare in different units. Nothing was created."
		)

	doc = frappe.get_doc(
		{
			"doctype": SCALE_TICKET,
			"ticket_number": ticket_number,
			"date": date,
			"customer": customer,
			"company": company,
			"variety": as_str(args, "variety") or None,
			"grade": as_str(args, "grade") or None,
			"gross_weight": gross,
			"tare_weight": tare,
			"weight_uom": weight_uom,
			"field": _optional_field(args) or None,
			"block": as_str(args, "block") or None,
			"truck_id": as_str(args, "truck_id") or None,
			"driver": as_str(args, "driver") or None,
			"destination": as_str(args, "destination") or None,
			"ticket_image": as_str(args, "ticket_image") or None,
			"notes": as_str(args, "notes") or None,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()

	data = _row_out({field: doc.get(field) for field in _TICKET_FIELDS})
	return ToolResult(
		data=data,
		summary=f"Scale ticket {doc.name} captured as a draft: ticket {ticket_number}, "
		f"{doc.get('net_weight')} {weight_uom} net to {customer} on {date}",
		docstatus_delta="none → draft",
	)


def _weight(args: dict, key: str) -> float:
	"""One weight argument, absent reading as zero and negative refused."""
	raw = args.get(key)
	if raw in (None, ""):
		return 0.0
	value = as_float(raw, key)
	if value < 0:
		raise ToolError(f"{key} cannot be negative — got {value}. Nothing was created.")
	return value


def submit_scale_ticket(args: dict) -> ToolResult:
	"""Submit a draft scale ticket, which makes it immutable."""
	name = _require(SCALE_TICKET, args, "scale_ticket", "ticket")
	doc = frappe.get_doc(SCALE_TICKET, name)

	if int(doc.get("docstatus") or 0) == 1:
		raise ToolError(f"scale ticket {name} is already submitted. Nothing was changed.")
	if int(doc.get("docstatus") or 0) == 2:
		raise ToolError(
			f"scale ticket {name} is cancelled. A cancelled ticket is amended into a new "
			f"one rather than resubmitted. Nothing was changed."
		)

	doc.flags.ignore_permissions = True
	doc.submit()

	return ToolResult(
		data={
			"name": name,
			"ticket_number": doc.get("ticket_number"),
			"status": doc.get("status"),
			"docstatus": int(doc.get("docstatus") or 0),
			"net_weight": float(doc.get("net_weight") or 0),
			"weight_uom": doc.get("weight_uom"),
			"customer": doc.get("customer"),
			"date": str(doc.get("date")),
		},
		summary=f"Scale ticket {name} submitted: {doc.get('net_weight')} "
		f"{doc.get('weight_uom')} net to {doc.get('customer')}",
		docstatus_delta="draft → submitted",
	)


# ── Settlement Statement: reads ───────────────────────────────────────────


def list_settlement_statements(args: dict) -> ToolResult:
	"""Settlement statements by customer, company, status and statement date."""
	filters = {}

	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)

	customer = as_str(args, "customer")
	if customer:
		filters["customer"] = _customer(customer)

	status = _status_filter(args, SETTLEMENT_STATUSES)
	if status:
		filters["status"] = status

	from_date, to_date = _date_range(args, filters)
	limit = _limit(args)

	rows = frappe.db.get_all(
		SETTLEMENT_STATEMENT,
		filters=filters,
		fields=list(_SETTLEMENT_FIELDS),
		limit_page_length=limit,
		order_by="date desc, creation desc",
	)
	statements = [_row_out(row) for row in rows]

	by_status = {}
	for statement in statements:
		key = statement.get("status") or "<none>"
		by_status[key] = by_status.get(key, 0) + 1

	data = {
		"settlement_statements": statements,
		"count": len(statements),
		"limit": limit,
		"truncated": len(statements) == limit,
		"total_net_proceeds": round(sum(s["net_proceeds"] for s in statements), 2),
		"total_deductions": round(sum(s["total_deductions"] for s in statements), 2),
		"by_status": by_status,
		"filters": {
			"customer": filters.get("customer") or None,
			"company": filters.get("company") or None,
			"status": status or None,
			"from_date": from_date,
			"to_date": to_date,
		},
	}
	return ToolResult(
		data=data,
		summary=f"{len(statements)} settlement statement(s), "
		f"{round(sum(s['net_proceeds'] for s in statements), 2)} net proceeds",
	)


def get_settlement_statement(args: dict) -> ToolResult:
	"""One settlement in full: lines, deductions, and the tickets it claims."""
	name = _require(SETTLEMENT_STATEMENT, args, "settlement_statement", "settlement", "statement")
	doc = frappe.get_doc(SETTLEMENT_STATEMENT, name)

	data = _row_out({field: doc.get(field) for field in _SETTLEMENT_FIELDS})
	data["line_items"] = _lines_out(doc)
	data["deductions"] = _deductions_out(doc)
	data["matched_scale_tickets"] = _matched_tickets(name)
	data["delivery_reconciliation"] = _reconciliation(doc, data["matched_scale_tickets"])

	return ToolResult(
		data=data,
		summary=f"Settlement {name} ({doc.get('statement_number')}) from {doc.get('customer')}: "
		f"{doc.get('packout_pct')}% packout, {doc.get('net_proceeds')} net — {doc.get('status')}",
	)


def _lines_out(doc) -> list[dict]:
	lines = []
	for row in doc.get("line_items") or []:
		get = row.get if isinstance(row, dict) else (lambda k, d=None, r=row: getattr(r, k, d))
		lines.append(
			{
				"variety": get("variety"),
				"grade": get("grade"),
				"packed_weight": float(get("packed_weight") or 0),
				"price_per_unit": float(get("price_per_unit") or 0),
				"price_uom": get("price_uom"),
				"gross_amount": float(get("gross_amount") or 0),
			}
		)
	return lines


def _deductions_out(doc) -> list[dict]:
	rows = []
	for row in doc.get("deductions") or []:
		get = row.get if isinstance(row, dict) else (lambda k, d=None, r=row: getattr(r, k, d))
		rows.append(
			{
				"deduction_type": get("deduction_type"),
				"description": get("description"),
				"amount": float(get("amount") or 0),
			}
		)
	return rows


def _matched_tickets(settlement: str) -> list[dict]:
	"""The Scale Tickets pointing at this settlement, newest first."""
	rows = frappe.db.get_all(
		SCALE_TICKET,
		filters={"settlement": settlement},
		fields=[
			"name",
			"ticket_number",
			"date",
			"variety",
			"grade",
			"net_weight",
			"weight_uom",
			"status",
		],
		order_by="date desc",
		limit_page_length=500,
	)
	return [_row_out(row) for row in rows]


def _reconciliation(doc, tickets: list) -> dict:
	"""The packer's delivered weight against the grower's own tickets.

	REPORTED, NEVER ENFORCED. The two figures come from two parties and the
	difference between them is the finding — a settlement that pays for less
	fruit than the tickets say was delivered is the single thing this pair of
	registers exists to make visible. A tool that refused the settlement, or
	overwrote one figure with the other, would delete it.

	Tickets in a unit other than the statement's are counted and excluded rather
	than converted: there is no conversion from bins to kilos that this app
	knows, and a fabricated one would put a fabricated variance on the answer.
	"""
	statement_uom = doc.get("weight_uom")
	comparable = [t for t in tickets if t.get("weight_uom") == statement_uom]
	mismatched = len(tickets) - len(comparable)

	ticket_total = round(sum(float(t.get("net_weight") or 0) for t in comparable), 3)
	packer_total = float(doc.get("gross_delivered_weight") or 0)

	return {
		"packer_gross_delivered_weight": packer_total,
		"matched_ticket_net_weight": ticket_total,
		"weight_uom": statement_uom,
		"variance": round(packer_total - ticket_total, 3),
		"matched_ticket_count": len(tickets),
		"tickets_in_other_units_excluded": mismatched,
		"note": (
			"variance is the PACKER's delivered weight less the sum of the grower's own "
			"matched Scale Tickets. Neither figure is derived from the other and neither "
			"is corrected — a non-zero variance is a finding to take up with the packer, "
			"not an error in this record. Zero matched tickets makes the variance equal "
			"to the packer's figure and means nothing at all."
		),
	}


# ── Settlement Statement: writes ──────────────────────────────────────────


def create_settlement_statement(args: dict) -> ToolResult:
	"""Capture one settlement statement as a draft, with its lines and deductions."""
	statement_number = as_str(args, "statement_number", required=True)
	date = as_date(args, "date", required=True)
	company = resolve_company(as_str(args, "company"), required=True)
	customer = _customer(as_str(args, "customer"), required=True)
	weight_uom = _weight_uom(args)

	period_start = as_date(args, "period_start")
	period_end = as_date(args, "period_end")
	if period_start and period_end and period_start > period_end:
		raise ToolError(f"period_start {period_start} is after period_end {period_end}. Nothing was created.")

	doc = frappe.get_doc(
		{
			"doctype": SETTLEMENT_STATEMENT,
			"statement_number": statement_number,
			"date": date,
			"customer": customer,
			"company": company,
			"period_start": period_start,
			"period_end": period_end,
			"gross_delivered_weight": _weight(args, "gross_delivered_weight"),
			"packed_weight": _weight(args, "packed_weight"),
			"cull_weight": _weight(args, "cull_weight"),
			"weight_uom": weight_uom,
			"statement_image": as_str(args, "statement_image") or None,
			"notes": as_str(args, "notes") or None,
		}
	)
	tickets = _tickets_to_match(args, company=company, customer=customer)

	for row in _read_lines(args):
		doc.append("line_items", row)
	for row in _read_deductions(args):
		doc.append("deductions", row)

	doc.flags.ignore_permissions = True
	doc.insert()

	matched = _match_tickets(doc.name, tickets)

	data = _row_out({field: doc.get(field) for field in _SETTLEMENT_FIELDS})
	data["line_items"] = _lines_out(doc)
	data["deductions"] = _deductions_out(doc)
	data["matched_scale_tickets"] = _matched_tickets(doc.name)
	data["delivery_reconciliation"] = _reconciliation(doc, data["matched_scale_tickets"])

	return ToolResult(
		data=data,
		summary=f"Settlement {doc.name} captured as a draft: {statement_number} from "
		f"{customer}, {doc.get('packout_pct')}% packout, {doc.get('net_proceeds')} net proceeds"
		+ (f", {matched} scale ticket(s) matched" if matched else ""),
		docstatus_delta="none → draft",
	)


def _tickets_to_match(args: dict, *, company: str, customer: str) -> list[str]:
	"""The `scale_tickets` argument, checked before anything is written.

	CHECKED BEFORE THE INSERT, all of it, so a settlement is never created with
	half its tickets claimed. Every refusal here says "nothing was created",
	which is only true because none of it runs after `doc.insert()`.

	Four things are refused, and each is a real way of losing a load:

	  * A ticket that is still a DRAFT. A draft is a record somebody is still
	    editing — its weights can change after the settlement is checked against
	    them, which makes the check meaningless.
	  * A ticket already claimed by a DIFFERENT settlement. Two statements
	    paying for one load is the exact overpayment this register exists to
	    surface, and silently re-pointing the ticket would hide it.
	  * A ticket belonging to another COMPANY. Two entities delivering to one
	    packer under one bench is the ordinary case here.
	  * A ticket from a different CUSTOMER. A packer does not settle another
	    packer's deliveries.
	"""
	raw = args.get("scale_tickets") or args.get("tickets")
	if raw in (None, "", []):
		return []
	if not isinstance(raw, list):
		raise ToolError("scale_tickets must be a list of Scale Ticket docnames.")

	names = []
	for index, value in enumerate(raw, start=1):
		name = str(value or "").strip()
		if not name:
			raise ToolError(f"scale_tickets[{index}] is empty. Nothing was created.")
		row = frappe.db.get_value(
			SCALE_TICKET, name, ["name", "docstatus", "company", "customer", "settlement"], as_dict=True
		)
		if not row:
			raise ToolError(f"no Scale Ticket called {name!r} on this site. Nothing was created.")
		if int(row.get("docstatus") or 0) != 1:
			raise ToolError(
				f"scale ticket {name} is not submitted, so it cannot be matched to a settlement. "
				f"A draft ticket's weights can still change after the settlement is checked "
				f"against them, which makes the check meaningless. Nothing was created."
			)
		if row.get("settlement"):
			raise ToolError(
				f"scale ticket {name} is already matched to settlement {row['settlement']}. "
				f"Two statements paying for one load is the overpayment this register exists to "
				f"surface — it is not resolved by re-pointing the ticket. Nothing was created."
			)
		if str(row.get("company") or "") != company:
			raise ToolError(
				f"scale ticket {name} belongs to {row.get('company')!r}, not to {company!r}. "
				f"Nothing was created."
			)
		if str(row.get("customer") or "") != customer:
			raise ToolError(
				f"scale ticket {name} was delivered to {row.get('customer')!r}, not to "
				f"{customer!r}. A packer does not settle another packer's deliveries. "
				f"Nothing was created."
			)
		names.append(name)
	return names


def _match_tickets(settlement: str, names: list) -> int:
	"""Point each checked ticket at the settlement. Status follows from the link."""
	for name in names:
		frappe.db.set_value(
			SCALE_TICKET, name, {"settlement": settlement, "status": "Matched"}, update_modified=False
		)
	return len(names)


def _read_lines(args: dict) -> list[dict]:
	"""The `line_items` argument, validated into rows the child table will accept.

	A missing `gross_amount` is filled from packed weight times price; one that
	was given is kept. Same rule as an Expense Receipt's line totals and the same
	reason — a packer who applied a promotion to a line is telling the truth and
	the multiplication is not.
	"""
	raw = args.get("line_items") or args.get("lines")
	if raw in (None, "", []):
		return []
	if not isinstance(raw, list):
		raise ToolError(
			"line_items must be a list of objects with variety, grade, packed_weight, "
			"price_per_unit, price_uom and gross_amount."
		)

	rows = []
	for index, item in enumerate(raw, start=1):
		if not isinstance(item, dict):
			raise ToolError(f"line_items[{index}] must be an object, got {type(item).__name__}.")
		packed_weight = as_float(item.get("packed_weight"), f"line_items[{index}].packed_weight")
		price = as_float(item.get("price_per_unit"), f"line_items[{index}].price_per_unit")
		gross_amount = as_float(item.get("gross_amount"), f"line_items[{index}].gross_amount")
		if not gross_amount and packed_weight and price:
			gross_amount = round(packed_weight * price, 2)
		rows.append(
			{
				"variety": as_str(item, "variety") or None,
				"grade": as_str(item, "grade") or None,
				"packed_weight": packed_weight,
				"price_per_unit": price,
				"price_uom": as_str(item, "price_uom") or None,
				"gross_amount": gross_amount,
			}
		)
	return rows


def _read_deductions(args: dict) -> list[dict]:
	"""The `deductions` argument, with every type checked against the Select."""
	raw = args.get("deductions")
	if raw in (None, "", []):
		return []
	if not isinstance(raw, list):
		raise ToolError("deductions must be a list of objects with deduction_type, description and amount.")

	rows = []
	for index, item in enumerate(raw, start=1):
		if not isinstance(item, dict):
			raise ToolError(f"deductions[{index}] must be an object, got {type(item).__name__}.")
		deduction_type = as_str(item, "deduction_type") or "Other"
		if deduction_type not in DEDUCTION_TYPES:
			raise ToolError(
				f"deductions[{index}].deduction_type must be one of: {', '.join(DEDUCTION_TYPES)}."
			)
		amount = as_float(item.get("amount"), f"deductions[{index}].amount")
		if amount < 0:
			raise ToolError(
				f"deductions[{index}].amount cannot be negative — got {amount}. A deduction is "
				f"already a subtraction, so a negative one would ADD to the proceeds. If the "
				f"packer credited something back, that is a line item, not a deduction."
			)
		rows.append(
			{
				"deduction_type": deduction_type,
				"description": as_str(item, "description") or None,
				"amount": amount,
			}
		)
	return rows


def submit_settlement_statement(args: dict) -> ToolResult:
	"""Submit a draft settlement statement, which makes it immutable."""
	name = _require(SETTLEMENT_STATEMENT, args, "settlement_statement", "settlement", "statement")
	doc = frappe.get_doc(SETTLEMENT_STATEMENT, name)

	if int(doc.get("docstatus") or 0) == 1:
		raise ToolError(f"settlement statement {name} is already submitted. Nothing was changed.")
	if int(doc.get("docstatus") or 0) == 2:
		raise ToolError(
			f"settlement statement {name} is cancelled. A cancelled statement is amended into "
			f"a new one rather than resubmitted. Nothing was changed."
		)

	doc.flags.ignore_permissions = True
	doc.submit()

	return ToolResult(
		data={
			"name": name,
			"statement_number": doc.get("statement_number"),
			"status": doc.get("status"),
			"docstatus": int(doc.get("docstatus") or 0),
			"customer": doc.get("customer"),
			"total_gross_revenue": float(doc.get("total_gross_revenue") or 0),
			"total_deductions": float(doc.get("total_deductions") or 0),
			"net_proceeds": float(doc.get("net_proceeds") or 0),
			"packout_pct": float(doc.get("packout_pct") or 0),
		},
		summary=f"Settlement {name} submitted: {doc.get('net_proceeds')} net proceeds from "
		f"{doc.get('customer')}",
		docstatus_delta="draft → submitted",
	)


# ── classify_receipt ──────────────────────────────────────────────────────
#
# WHY THIS IS RULES AND NOT A MODEL. The classifier runs at the moment a foreman
# is standing in front of the thing they photographed, and it is allowed to be
# wrong — the app shows its answer as a pre-selected tab the person can change.
# What it is NOT allowed to be is unaccountable: "why did it file my ticket as an
# expense" has to have an answer, and `matched_signals` is that answer. A
# keyword table is auditable by reading it, ships without weights to host, and
# runs on a bench with no Python beyond what Frappe already has.
#
# IT NEVER RETURNS 1.0. A keyword rule is never certain, and a confidence of 1.0
# is an instruction to the client to stop asking. The ceiling is 0.95.

#: `(keyword, weight)` per receipt type. Weight 2 is a phrase that appears on
#: essentially nothing else; weight 1 is a word that leans. Everything is matched
#: case-insensitively against merchant, description and raw text together.
CLASSIFIER_SIGNALS = {
	"scale_ticket": (
		("scale ticket", 2),
		("weight ticket", 2),
		("weighmaster", 2),
		("gross wt", 2),
		("tare wt", 2),
		("net wt", 2),
		("tare weight", 2),
		("gross weight", 2),
		("net weight", 2),
		("bin count", 2),
		("load ticket", 2),
		("delivery ticket", 2),
		("certified scale", 2),
		("tare", 1),
		("gross", 1),
		("bins", 1),
		("truck", 1),
		("orchard", 1),
		("block", 1),
		("lot", 1),
	),
	"settlement": (
		("settlement", 2),
		("packout", 2),
		("pack out", 2),
		("grower statement", 2),
		("grower return", 2),
		("pool return", 2),
		("packing charge", 2),
		("cold storage", 2),
		("marketing fee", 2),
		("commission", 2),
		("cull", 2),
		("net proceeds", 2),
		("deduction", 1),
		("pool", 1),
		("fob", 1),
		("packed", 1),
		("season", 1),
	),
	"bill": (
		("invoice", 2),
		("invoice no", 2),
		("bill to", 2),
		("remit to", 2),
		("amount due", 2),
		("net 30", 2),
		("purchase order", 2),
		("statement of account", 2),
		("terms", 1),
		("due date", 1),
		("account number", 1),
		("balance forward", 1),
	),
	"expense": (
		("thank you for shopping", 2),
		("change due", 2),
		("cash tendered", 2),
		("card ending", 2),
		("pump", 2),
		("gallons", 2),
		("unleaded", 2),
		("diesel", 2),
		("subtotal", 1),
		("sales tax", 1),
		("cashier", 1),
		("visa", 1),
		("mastercard", 1),
		("debit", 1),
		("receipt", 1),
		("hardware", 1),
		("auto parts", 1),
		("co-op", 1),
	),
}

#: Ties are broken in this order, and the order is an argument. A settlement
#: statement quotes weights, so it will always pick up scale-ticket words; a
#: scale ticket almost never uses the word "packout". The more specific document
#: therefore wins a tie. `expense` is last because it is also the fallback, and a
#: fallback that could win a tie would swallow the two registers this release
#: exists to fill.
CLASSIFIER_PRECEDENCE = ("settlement", "scale_ticket", "bill", "expense")

#: What a type maps to on the other side of the branch, so a client does not have
#: to keep its own table.
CLASSIFIER_TARGETS = {
	"scale_ticket": "Scale Ticket (create_scale_ticket)",
	"settlement": "Settlement Statement (create_settlement_statement)",
	"bill": "Purchase Invoice — NOT YET IMPLEMENTED, capture as an expense for now",
	"expense": "Expense Receipt (submit_expense_receipt)",
}

#: How much evidence counts as enough. Below this, confidence is scaled down in
#: proportion — one weak keyword out of one is not 100% certainty, it is one
#: weak keyword.
CLASSIFIER_SATURATION = 4.0

#: A keyword rule is never certain.
CLASSIFIER_CEILING = 0.95


def classify_receipt(args: dict) -> ToolResult:
	"""Which register a photographed document belongs in, and how sure that is."""
	merchant = as_str(args, "merchant")
	description = as_str(args, "description")
	text = as_str(args, "text") or as_str(args, "ocr_raw_text")
	amount = args.get("amount")

	haystack = " ".join(part for part in (merchant, description, text) if part).lower()
	if not haystack.strip():
		raise ToolError(
			"classify_receipt needs something to read: pass merchant, description or text "
			"(the raw OCR output). Classifying an empty string would return a guess wearing "
			"a confidence."
		)

	scores = {}
	matched = {}
	for receipt_type, signals in CLASSIFIER_SIGNALS.items():
		hits = [(keyword, weight) for keyword, weight in signals if keyword in haystack]
		scores[receipt_type] = sum(weight for _, weight in hits)
		matched[receipt_type] = [keyword for keyword, _ in hits]

	total = sum(scores.values())
	if not total:
		return ToolResult(
			data={
				"receipt_type": "expense",
				"confidence": 0.0,
				"default_applied": True,
				"matched_signals": [],
				"scores": scores,
				"alternatives": [],
				"suggested_tool": CLASSIFIER_TARGETS["expense"],
				"amount": float(amount) if amount not in (None, "") else None,
				"note": (
					"nothing in the text matched any rule, so this is the FALLBACK rather than "
					"a classification — expense is where an unrecognised slip does least harm, "
					"because it is the register a person reviews anyway. Confidence is 0."
				),
			},
			summary="no signal matched — falling back to expense with confidence 0",
		)

	winner = min(
		CLASSIFIER_PRECEDENCE,
		key=lambda kind: (-scores[kind], CLASSIFIER_PRECEDENCE.index(kind)),
	)
	share = scores[winner] / total
	saturation = min(1.0, scores[winner] / CLASSIFIER_SATURATION)
	confidence = round(min(CLASSIFIER_CEILING, share * saturation), 2)

	alternatives = [
		{"receipt_type": kind, "score": scores[kind], "matched_signals": matched[kind]}
		for kind in CLASSIFIER_PRECEDENCE
		if kind != winner and scores[kind]
	]

	data = {
		"receipt_type": winner,
		"confidence": confidence,
		"default_applied": False,
		"matched_signals": matched[winner],
		"scores": scores,
		"alternatives": alternatives,
		"suggested_tool": CLASSIFIER_TARGETS[winner],
		"amount": float(amount) if amount not in (None, "") else None,
		"note": (
			"confidence is the winner's share of all matched evidence, scaled down when there "
			f"was little evidence to share (full weight at a score of {int(CLASSIFIER_SATURATION)}), "
			f"and capped at {CLASSIFIER_CEILING} because a keyword rule is never certain. "
			"matched_signals is the whole reason for the answer — show it rather than the number "
			"when somebody asks why."
		),
	}
	return ToolResult(
		data=data,
		summary=f"{winner} (confidence {confidence}) on "
		f"{len(matched[winner])} signal(s): {', '.join(matched[winner][:5]) or '<none>'}",
	)


# ── merchant → supplier matching ─────────────────────────────────────────────
#
# v0.68.0. `submit_expense_receipt`'s design principle is that NOTHING is ever
# inferred: `merchant` and `supplier` stay independent unless a human sets the
# link. `normalize_merchant` does not break that — it is the SUGGESTION a human
# reviews before they set it, published as its own read-only tool rather than
# folded into capture. `create_purchase_invoice_from_receipt` is the one caller
# in this app that acts on a match without a human in the loop first, and it
# only does so above a high confidence bar, and it says which branch it took.
#
# NO ML. A merchant string and a Supplier's legal name are both short, mostly
# stable strings, and the differences between them are punctuation and a
# corporate suffix — "WILBUR ELLIS CO" against "Wilbur-Ellis Company LLC" is the
# same six letters twice with three extra words neither company chose. Stripping
# the suffix and comparing what is left is the whole algorithm, and it is
# auditable by reading it, which a trained model is not.

#: Corporate-entity words stripped before comparison. Every one of them is a
#: legal-form marker that varies between how a receipt printer abbreviates a
#: vendor and how that vendor is registered, and neither variant is evidence
#: about which company it is.
_CORPORATE_SUFFIXES = frozenset(
	{
		"CO",
		"COMPANY",
		"LLC",
		"LLP",
		"LP",
		"INC",
		"INCORPORATED",
		"CORP",
		"CORPORATION",
		"LTD",
		"LIMITED",
		"PC",
		"PLLC",
		"THE",
	}
)

_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")

#: Below this, a match is noise rather than a suggestion — `normalize_merchant`
#: reports it as `null` rather than as a low-confidence guess a client might
#: render as though it meant something.
_MATCH_FLOOR = 0.35

#: Above this, `create_purchase_invoice_from_receipt` links a Supplier without
#: asking — high enough that "WILBUR ELLIS CO" → "Wilbur-Ellis Company LLC"
#: clears it (it normalizes to an exact match) and "ACE HARDWARE #4102" against
#: an unrelated "ACE RENTALS" does not.
_AUTO_LINK_THRESHOLD = 0.85

#: A similarity algorithm is never certain it has found the same legal entity,
#: only that the text looks alike — same reasoning as `classify_receipt`'s
#: ceiling, and the same number.
_MATCH_CEILING = 0.95


def _normalize_merchant_name(value: str) -> str:
	"""Upper-case, punctuation stripped to spaces, corporate suffixes dropped."""
	text = _NON_ALNUM.sub(" ", str(value or "").upper())
	tokens = [token for token in text.split() if token and token not in _CORPORATE_SUFFIXES]
	return " ".join(tokens)


def _merchant_similarity(a: str, b: str) -> float:
	"""How alike two vendor names are, 0 to 1, after stripping legal-form noise.

	Blends two measures because either alone is fooled differently: a character
	ratio (`SequenceMatcher`) is fooled by word order, a word-set ratio (Jaccard)
	is fooled by a single extra digit run like a store number. Averaging them
	keeps a genuine match from either failure mode alone.
	"""
	norm_a, norm_b = _normalize_merchant_name(a), _normalize_merchant_name(b)
	if not norm_a or not norm_b:
		return 0.0
	char_ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
	tokens_a, tokens_b = set(norm_a.split()), set(norm_b.split())
	word_ratio = len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if (tokens_a | tokens_b) else 0.0
	return round(min(_MATCH_CEILING, 0.5 * char_ratio + 0.5 * word_ratio), 4)


def _candidate_suppliers(limit: int = 3000) -> list[dict]:
	return frappe.db.get_all(
		masters.SUPPLIER, fields=["name", "supplier_name"], limit_page_length=limit, order_by="name asc"
	)


def _ranked_supplier_matches(merchant: str, top: int = 5) -> list[dict]:
	"""Every Supplier on the site, scored against `merchant`, best first."""
	scored = []
	for row in _candidate_suppliers():
		label = row.get("supplier_name") or row.get("name")
		confidence = _merchant_similarity(merchant, label)
		if confidence <= 0:
			continue
		scored.append(
			{"supplier": row["name"], "supplier_name": row.get("supplier_name"), "confidence": confidence}
		)
	scored.sort(key=lambda row: (-row["confidence"], row["supplier"]))
	return scored[:top]


# ── multi-vector receipt intelligence ────────────────────────────────────────
#
# v0.75.0. THE PROBLEM THE NAME MATCHER CANNOT SOLVE. `_merchant_similarity` gets
# from "WILBUR ELLIS CO" to "Wilbur-Ellis Company LLC" because the letters are
# the same letters. It will never get from "SIATAPING" to "Sawyer's Ace
# Hardware", because they are not — that is a point-of-sale terminal's own
# abbreviation of a franchise name, and no amount of string algebra recovers a
# word that is not on the paper.
#
# But the paper is not only the merchant line. The same slip usually prints a
# domain, a phone number, a store number and the last four digits of the card,
# and each of those is a HARDER identifier than the name: a domain is registered
# to one company, a phone number rings in one building, a card's last four picks
# one of two trucks out of a fuel account. So the merchant line stops being the
# only evidence and becomes the weakest of five.
#
# THE CASCADE IS ORDERED BY WHAT THE EVIDENCE IS, NOT BY WHAT IS CONVENIENT:
#
#   1. Alias   — a decision a person already made, replayed. Not a guess at all.
#   2. URL     — a domain is registered to one company.
#   3. Phone   — a number rings in one building.
#   4. OCR     — the existing name similarity. A guess, and scored as one.
#   5. LLM     — the raw text read as prose, for when the first four are silent.
#
# EVERY STEP IS RECORDED, INCLUDING THE ONES THAT FOUND NOTHING. `steps` is the
# same promise `classify_receipt.matched_signals` makes: "why did this receipt
# resolve to that vendor" has an answer that is a list, not a number.
#
# THIS APP DOES NOT CALL A MODEL, AND STEP 5 DOES NOT EITHER. `document_intel`
# established the contract and this follows it exactly: the deterministic half
# runs here, and a judgement arrives from the client that already has a model —
# a phone, or the MCP client itself. So when the first four steps are silent
# this returns `llm_context`: the signals off the paper and the candidate
# Suppliers, packaged as the question. A caller that answers it hands the answer
# back as `resolved_merchant` with `resolution_method="LLM"`. A step that
# secretly dialled an API would put a network call, a key and a bill inside a
# `bench migrate`, and would make an offline capture fail for a reason nobody
# standing at a fuel pump could diagnose.

#: How a merchant was resolved. Mirrored from the Custom Field's Select options
#: and asserted equal to them by the tests. `Manual` is a person setting the
#: supplier link by hand — the only one of the six that is not this app's work.
RESOLUTION_METHODS = ("OCR", "URL", "Phone", "Alias", "LLM", "Manual")

ALIAS_MANUAL = "Manual"
ALIAS_AUTO = "Auto"
ALIAS_LLM = "LLM"

#: How an alias came to be known. Mirrored from the Merchant Alias JSON.
ALIAS_SOURCES = (ALIAS_MANUAL, ALIAS_AUTO, ALIAS_LLM)

#: The seven columns this release adds to Expense Receipt. Four are what the
#: scanner read off the paper besides the name; three are what reading it
#: produced. They are kept apart on purpose — `merchant` stays the raw OCR
#: string for ever, and `resolved_merchant` is the answer — because a pipeline
#: that overwrote the reading with its own conclusion would delete the only
#: evidence anybody could use to find out the conclusion was wrong.
CARD_LAST_FOUR_FIELD = "card_last_four"
MERCHANT_PHONE_FIELD = "merchant_phone"
MERCHANT_URL_FIELD = "merchant_url"
STORE_NUMBER_FIELD = "store_number"
RESOLVED_MERCHANT_FIELD = "resolved_merchant"
RESOLUTION_METHOD_FIELD = "resolution_method"
RESOLUTION_CONFIDENCE_FIELD = "resolution_confidence"

#: What was read off the paper. Captured, never derived from the resolution.
SIGNAL_FIELDS = (
	CARD_LAST_FOUR_FIELD,
	MERCHANT_PHONE_FIELD,
	MERCHANT_URL_FIELD,
	STORE_NUMBER_FIELD,
)

#: What reading it produced. Derived, never typed.
RESOLUTION_FIELDS = (
	RESOLVED_MERCHANT_FIELD,
	RESOLUTION_METHOD_FIELD,
	RESOLUTION_CONFIDENCE_FIELD,
)

INTELLIGENCE_FIELDS = (*SIGNAL_FIELDS, *RESOLUTION_FIELDS)

#: An alias learned from a person's own supplier link is not a guess — it is
#: that person's decision handed back to them, so it resolves at 1.0. Every
#: other step in this module is capped at `_MATCH_CEILING` because every other
#: step is an algorithm's opinion, and this is the one place the distinction is
#: real rather than rhetorical.
_ALIAS_MANUAL_CONFIDENCE = 1.0

#: An alias this app taught itself, or that a model taught it, replays at the
#: same ceiling every other guess in this app lives under. It was a guess when
#: it was written and repetition does not make it a fact.
_ALIAS_LEARNED_CONFIDENCE = _MATCH_CEILING

#: A domain is registered to one company, which makes it the strongest evidence
#: on the slip after a human's own decision — but the receipt might print the
#: franchisor's site rather than the franchisee's, so it is not certainty.
_URL_CONFIDENCE = 0.90

#: A phone number rings in one building. Slightly below a domain because a
#: vendor who moved keeps the number and a corporate switchboard is shared.
_PHONE_CONFIDENCE = 0.88

#: What a domain match against the RECEIPT CORPUS is worth, as against a match
#: against the Supplier's own `website` column. Lower, and deliberately: the
#: corpus is other receipts somebody linked, so it is an alias by another route,
#: and it inherits the alias table's uncertainty rather than the register's.
_CORPUS_PENALTY = 0.05

#: Words whose presence on a line makes that line worth handing to a model. A
#: rewards programme, a survey URL and a "thank you for shopping at" line are
#: the three places a receipt spells out a brand name the merchant line
#: abbreviated — which is exactly what step 5 exists to read.
_LLM_SIGNAL_WORDS = (
	"rewards",
	"loyalty",
	"member",
	"membership",
	"survey",
	"tell us",
	"feedback",
	"thank you for shopping",
	"thank you for choosing",
	"visit us",
	"www.",
	"http",
	".com",
	"store #",
	"franchise",
)

#: How many raw-text lines travel with the question. A receipt is a hundred
#: lines of item codes and eight lines that name the vendor; sending all of it
#: costs a caller tokens to read the item codes again.
_LLM_CONTEXT_LINES = 12

#: A domain, with or without a scheme, in running OCR text. The TLD is bounded
#: at 2–24 letters so a sentence ending in a full stop followed by a word does
#: not read as a hostname.
_URL_RE = re.compile(
	r"\b(?:https?://)?(?:www\.)?((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})\b(?:/\S*)?"
)

#: A North American phone number in any of the shapes a receipt printer uses.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]*)?\(?(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})(?!\d)")

#: The last four of a card, and ONLY in the shapes that say so. A masked run
#: (`************1234`, `XXXX 1234`) or a phrase (`card ending 1234`). A bare
#: four-digit number is never taken: a receipt is full of them — a store number,
#: a time, a total in cents — and a card fingerprint that matched a clock would
#: be worse than no card fingerprint at all.
_CARD_RE = re.compile(
	r"(?:(?:[*x#•]\s*){2,}|"
	r"(?:card|acct|account|visa|mastercard|amex|discover|debit|credit)\D{0,24}?|"
	r"(?:ending(?:\s+in)?|last\s*4|xx)\D{0,6})(\d{4})(?!\d)",
	re.IGNORECASE,
)

#: A store or location number. Anchored on the word or on a `#`, for the same
#: reason the card pattern is anchored: an unanchored number run is a coin toss.
_STORE_RE = re.compile(
	r"(?:store|str|loc|location|shop|unit)\s*(?:no\.?|number|#)?\s*#?\s*(\d{1,6})\b", re.IGNORECASE
)

_BARE_STORE_RE = re.compile(r"#\s?(\d{2,6})\b")

#: Hostnames that identify a payment processor, a survey vendor or a mapping
#: service rather than the merchant. Every one of them appears on receipts from
#: hundreds of unrelated businesses, so a domain match against one of these is a
#: match against the printer's supplier, not against the shop.
_GENERIC_DOMAINS = frozenset(
	{
		"survey.com",
		"surveymonkey.com",
		"talktous.com",
		"tellus.com",
		"google.com",
		"goo.gl",
		"bit.ly",
		"facebook.com",
		"instagram.com",
		"twitter.com",
		"x.com",
		"paypal.com",
		"square.com",
		"squareup.com",
		"clover.com",
		"toasttab.com",
		"verifone.com",
		"ingenico.com",
		"visa.com",
		"mastercard.com",
		"amex.com",
		"americanexpress.com",
		"discover.com",
		"gmail.com",
		"yahoo.com",
		"outlook.com",
		"hotmail.com",
		"example.com",
	}
)


# ── schema: the seven intelligence columns, made to exist ────────────────────


def ensure_receipt_intelligence_fields() -> bool:
	"""Give Expense Receipt the seven columns multi-vector resolution needs.

	CUSTOM FIELDS, and on this app's OWN doctype, which looks odd until you ask
	what the alternative costs. Expense Receipt is a register a site has been
	capturing into since v0.31.0; putting seven more columns in the shipped JSON
	makes every one of them mandatory schema for a bench that will never enable
	receipt intelligence at all, and makes the feature un-droppable — a Custom
	Field is removable in the Desk by the operator who decided they did not want
	it, and a JSON field is removable only by a release. Same mechanism and same
	argument as `anchors.ensure_pairing_fields` and
	`banking_bridge.ensure_categorization_fields`, and the third and fourth of
	those already extend somebody else's doctype for exactly this reason.

	Created on first use as well as at install, so a bench that pulled the code
	without running the installer resolves a merchant the first time somebody
	captures a receipt.

	NEVER RAISES. A site that will not take the columns keeps every existing
	behaviour and loses only the new one: capture still works, the cascade still
	RUNS and still reports its answer in the tool result, and only the writing of
	that answer onto the record is skipped. Every tool says
	`intelligence_fields_installed: false` rather than pretending otherwise.
	"""
	try:
		if _intelligence_fields_present():
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists(expenses.EXPENSE_RECEIPT):
			return False
	except Exception:
		return False

	specification = (
		{
			"fieldname": CARD_LAST_FOUR_FIELD,
			"label": "Card Last Four",
			"fieldtype": "Data",
			"length": 4,
			"insert_after": "ocr_raw_text",
			"read_only": 1,
			"description": (
				"The last four digits of the card this was paid with, as printed on the slip. "
				"Read-only because it is the scanner's reading of the paper, not a field anybody "
				"types. It is what tells two identical fuel receipts on one day apart — two trucks, "
				"two drivers, two cards — which is a distinction no amount, date or vendor name can "
				"make. NEVER a full card number: four digits identify a card within one account and "
				"are useless outside it, which is why this column exists and a longer one does not."
			),
		},
		{
			"fieldname": MERCHANT_PHONE_FIELD,
			"label": "Merchant Phone",
			"fieldtype": "Data",
			"insert_after": CARD_LAST_FOUR_FIELD,
			"read_only": 1,
			"description": (
				"The phone number printed on the receipt. A number rings in one building, which "
				"makes it a harder identifier than the merchant line — a franchise whose till "
				"prints an abbreviation still prints its own phone number underneath it."
			),
		},
		{
			"fieldname": MERCHANT_URL_FIELD,
			"label": "Merchant URL",
			"fieldtype": "Data",
			"insert_after": MERCHANT_PHONE_FIELD,
			"read_only": 1,
			"description": (
				"The domain printed on the receipt. Registered to one company, so it resolves a "
				"vendor the name alone cannot — but a receipt sometimes prints the FRANCHISOR's "
				"site, and a survey or payment-processor domain identifies the till's supplier "
				"rather than the shop, which is why those are ignored by name."
			),
		},
		{
			"fieldname": STORE_NUMBER_FIELD,
			"label": "Store Number",
			"fieldtype": "Data",
			"insert_after": MERCHANT_URL_FIELD,
			"read_only": 1,
			"description": (
				"The store or location number off the slip. It does NOT resolve a vendor on its "
				"own — every chain has a store 14 — and it is kept because it is how a bookkeeper "
				"tells one branch's account from another's when the vendor is the same company."
			),
		},
		{
			"fieldname": RESOLVED_MERCHANT_FIELD,
			"label": "Resolved Merchant",
			"fieldtype": "Data",
			"insert_after": STORE_NUMBER_FIELD,
			"read_only": 1,
			"description": (
				"What the merchant ACTUALLY is, as against `merchant`, which is what the scanner "
				"read. The two are separate columns for ever: overwriting the reading with the "
				"conclusion would delete the only evidence anybody could use to find out the "
				"conclusion was wrong. This is a suggestion — the `supplier` link is still the "
				"human's decision, and only a replayed human decision (an alias) ever sets it."
			),
		},
		{
			"fieldname": RESOLUTION_METHOD_FIELD,
			"label": "Resolution Method",
			"fieldtype": "Select",
			"options": "\n" + "\n".join(RESOLUTION_METHODS),
			"insert_after": RESOLVED_MERCHANT_FIELD,
			"read_only": 1,
			"description": (
				"Which step of the cascade produced the answer, best evidence first: Alias (a "
				"person's own decision, replayed), URL, Phone, OCR (name similarity), LLM (a model "
				"read the raw text), Manual (somebody set the supplier by hand). It is the whole "
				"reason for the answer — show it rather than the confidence when somebody asks why."
			),
		},
		{
			"fieldname": RESOLUTION_CONFIDENCE_FIELD,
			"label": "Resolution Confidence",
			"fieldtype": "Float",
			"precision": "4",
			"insert_after": RESOLUTION_METHOD_FIELD,
			"read_only": 1,
			"description": (
				"0–1. Only ever 1.0 for Alias and Manual, which are records of a person's own "
				"decision rather than guesses about it; every algorithmic step is capped at 0.95 "
				"for the same reason classify_receipt is."
			),
		},
	)

	created = False
	for field in specification:
		try:
			if compat.has_field(expenses.EXPENSE_RECEIPT, field["fieldname"]):
				continue
			if frappe.db.exists(
				CUSTOM_FIELD, {"dt": expenses.EXPENSE_RECEIPT, "fieldname": field["fieldname"]}
			):
				continue
			doc = frappe.new_doc(CUSTOM_FIELD)
			doc.dt = expenses.EXPENSE_RECEIPT
			for key, value in field.items():
				doc.set(key, value)
			doc.insert(ignore_permissions=True)
			created = True
		except Exception:
			frappe.log_error(
				title=f"erpnext_mcp: could not add {field['fieldname']} to Expense Receipt",
				message=compat.traceback_text(),
			)
			return False

	if created:
		try:
			frappe.clear_cache(doctype=expenses.EXPENSE_RECEIPT)
		except Exception:
			pass
	return _intelligence_fields_present()


def _intelligence_fields_present() -> bool:
	try:
		return all(compat.has_field(expenses.EXPENSE_RECEIPT, name) for name in INTELLIGENCE_FIELDS)
	except Exception:
		return False


def intelligence_fields_installed() -> bool:
	"""Whether the seven columns are there, installing them if they are not.

	The public one. Every caller asks this rather than the private check, so a
	bench that never ran the installer gets the columns on first capture instead
	of silently losing the feature until somebody notices.
	"""
	return _intelligence_fields_present() or ensure_receipt_intelligence_fields()


def alias_table_ready() -> bool:
	"""Whether this site has the Merchant Alias register.

	Read through `compat` rather than assumed, because the register ships with
	this app and a bench that pulled the code without migrating has the tools and
	not the table. The cascade drops step 1 rather than raising: an alias is the
	best evidence available, and no evidence is not an error.
	"""
	try:
		return compat.doctype_exists(MERCHANT_ALIAS)
	except Exception:
		return False


# ── reading the paper: the signals besides the name ──────────────────────────


def alias_key(value: str) -> str:
	"""The lookup key for a merchant string — the normalised name, and nothing else.

	Exported under its own name rather than left as `_normalize_merchant_name`
	because the Merchant Alias controller stores rows under it and the resolver
	looks rows up by it, and the day those two use different functions the table
	stops matching with no error anywhere to say so.
	"""
	return _normalize_merchant_name(value)


def normalize_phone(value: str) -> str:
	"""A phone number as its last ten digits, or "" if there are not ten.

	TEN, not eleven and not whatever was printed: a receipt prints (509) 555-1234
	and a Supplier record holds +1 509 555 1234, and those are one number. The
	country code is dropped rather than normalised because this app is a US farm
	app and a leading 1 is punctuation here.
	"""
	digits = re.sub(r"\D", "", str(value or ""))
	if len(digits) == 11 and digits.startswith("1"):
		digits = digits[1:]
	return digits if len(digits) == 10 else ""


def normalize_domain(value: str) -> str:
	"""A URL or hostname as a bare lower-case domain, or "" if it is not one.

	`www.` is dropped and a path, a scheme and a query are discarded — what is
	left is the thing that is registered to a company. Subdomains are KEPT: a
	receipt printing `shop.example.com` is not evidence about `example.com`'s
	owner in a world where a hosting company puts a thousand shops on one domain,
	and the comparison is an exact-or-suffix one below rather than a guess here.
	"""
	text = str(value or "").strip().lower()
	if not text:
		return ""
	text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
	text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
	text = text.split("@")[-1].split(":", 1)[0]
	if text.startswith("www."):
		text = text[4:]
	text = text.strip(".")
	if not text or "." not in text:
		return ""
	if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}", text):
		return ""
	return text


def _domains_agree(a: str, b: str) -> bool:
	"""Whether two domains name the same company.

	Equal, or one is a subdomain of the other — `shop.acehardware.com` and
	`acehardware.com` are the same vendor, and `acehardware.com` and
	`notacehardware.com` are not, which is why this is a label-wise suffix test
	and not `endswith`.
	"""
	if not a or not b:
		return False
	if a == b:
		return True
	return a.endswith("." + b) or b.endswith("." + a)


def _generic_domain(domain: str) -> bool:
	"""Whether a domain identifies the till's supplier rather than the shop."""
	if not domain:
		return True
	if domain in _GENERIC_DOMAINS:
		return True
	return any(domain.endswith("." + entry) for entry in _GENERIC_DOMAINS)


def extract_receipt_signals(text: str) -> dict:
	"""Card last four, phone, domain and store number, read out of raw OCR text.

	FOUR REGEXES AND NO MODEL, for the same reason `classify_receipt` is a
	keyword table: this runs while somebody is standing at a fuel pump, it is
	allowed to be wrong, and what it is not allowed to be is unaccountable. Every
	pattern is in the source above and every one of them is anchored — a bare
	four-digit run is never read as a card and never as a store number, because a
	receipt is full of four-digit runs that are times, totals and item codes.

	Returns the four keys always, "" for the ones nothing matched, so a caller
	never has to distinguish "not found" from "not looked for".
	"""
	body = str(text or "")
	found = {name: "" for name in SIGNAL_FIELDS}
	if not body.strip():
		return found

	card = _CARD_RE.search(body)
	if card:
		found[CARD_LAST_FOUR_FIELD] = card.group(1)

	for match in _PHONE_RE.finditer(body):
		phone = normalize_phone("".join(match.groups()))
		if phone:
			found[MERCHANT_PHONE_FIELD] = phone
			break

	for match in _URL_RE.finditer(body):
		domain = normalize_domain(match.group(1))
		if domain and not _generic_domain(domain):
			found[MERCHANT_URL_FIELD] = domain
			break

	store = _STORE_RE.search(body) or _BARE_STORE_RE.search(body)
	if store:
		found[STORE_NUMBER_FIELD] = store.group(1)

	return found


def card_last_four(value: str) -> str:
	"""Four digits from whatever a caller sent, or "" if there are not four.

	`****1234`, `xxxx-1234`, `1234` and `Card ending 1234` all mean the same
	thing to a person and arrive as different strings from four different
	scanners. What is refused is a full card number: sixteen digits in this
	column would be a PAN in a database that has no business holding one, and
	silently truncating it would be worse — it would store the fingerprint and
	tell nobody the rest had ever been sent.
	"""
	text = str(value or "").strip()
	if not text:
		return ""
	digits = re.sub(r"\D", "", text)
	if len(digits) > 4:
		raise ToolError(
			f"card_last_four is the LAST FOUR digits of a card and got {len(digits)} digits. "
			"If that is a full card number, do not send it — this app has no column for one and "
			"no reason to hold one. Send the last four only. Nothing was written."
		)
	if len(digits) != 4:
		raise ToolError(f"card_last_four must be exactly 4 digits — got {text!r}. Nothing was written.")
	return digits


def _llm_context_lines(text: str) -> list[str]:
	"""The lines of a receipt that name a brand, for a caller that has a model.

	A receipt is a hundred lines of item codes and eight that say who printed it.
	These are the eight: a rewards programme, a survey URL, a "thank you for
	shopping at" line. Filtering rather than sending the lot is not a token
	economy — it is that the item codes are the part most likely to talk a model
	into a confident wrong answer.
	"""
	lines = []
	for raw in str(text or "").splitlines():
		line = raw.strip()
		if not line or len(line) > 200:
			continue
		lowered = line.lower()
		if any(word in lowered for word in _LLM_SIGNAL_WORDS):
			if line not in lines:
				lines.append(line)
		if len(lines) >= _LLM_CONTEXT_LINES:
			break
	return lines


# ── the alias register ───────────────────────────────────────────────────────


_ALIAS_FIELDS = (
	"name",
	"alias",
	"alias_key",
	"canonical_supplier",
	"source",
	"match_count",
	"last_matched_on",
	"first_learned_from",
)


def find_alias(merchant: str) -> dict | None:
	"""The Merchant Alias row for a merchant string, or None.

	Looked up by the NORMALISED key, which is the docname — so "Valley Co-op
	#14" finds the row taught as "VALLEY CO-OP 14" and a lookup is a primary-key
	read rather than a scan. An alias whose Supplier has since been deleted is
	treated as absent: replaying a decision that points at nothing is not
	replaying a decision.
	"""
	if not alias_table_ready():
		return None
	key = alias_key(merchant)
	if not key:
		return None
	row = frappe.db.get_value(MERCHANT_ALIAS, key, list(_ALIAS_FIELDS), as_dict=True)
	if not row:
		return None
	if not frappe.db.exists(masters.SUPPLIER, row.get("canonical_supplier")):
		return None
	return dict(row)


def learn_merchant_alias(
	merchant: str, supplier: str, *, source: str = ALIAS_MANUAL, receipt: str = ""
) -> dict:
	"""Record that this spelling means this Supplier. Returns what happened, always.

	CALLED WHENEVER A PERSON SETS A SUPPLIER LINK BY HAND, which is what makes
	the table grow without anybody being asked to maintain it — the bookkeeper
	who codes one "SIATAPING" receipt has taught every future one, and was never
	shown a form about aliases.

	FOUR OUTCOMES AND EACH IS NAMED IN `action`, because "did that do anything"
	is the caller's next question:

	  * `created`   — a spelling nothing had seen before.
	  * `repointed` — the key existed against a DIFFERENT Supplier, and a later
	    human decision beat an earlier one. The count is kept, not reset: those
	    receipts really did resolve through this key, and zeroing it would make
	    a long-standing alias look brand new the day somebody corrected it.
	  * `unchanged` — same key, same Supplier. The counter belongs to resolution,
	    not to teaching, so nothing moves.
	  * `skipped`   — the merchant string already normalises to the Supplier's own
	    name, so `_merchant_similarity` would find it anyway and a row here would
	    be a fact stored twice. This is the common case and it is not a failure.

	NEVER RAISES. Learning is a side effect of an edit that has already
	succeeded; a table that will not take a row must not turn a completed supplier
	correction into an error.
	"""
	outcome = {
		"action": "skipped",
		"alias": str(merchant or ""),
		"alias_key": "",
		"supplier": str(supplier or ""),
		"source": source if source in ALIAS_SOURCES else ALIAS_MANUAL,
		"why": "",
	}
	key = alias_key(merchant)
	outcome["alias_key"] = key

	if not key or not supplier:
		outcome["why"] = "there is no merchant string, or no supplier to map it to"
		return outcome
	if not alias_table_ready():
		outcome["why"] = (
			f"this site has no {MERCHANT_ALIAS} doctype — it ships with erpnext_mcp, so run "
			"`bench migrate`. The supplier link itself was still set."
		)
		return outcome

	try:
		label = frappe.db.get_value(masters.SUPPLIER, supplier, "supplier_name") or supplier
		if alias_key(label) == key:
			outcome["why"] = (
				f"{merchant!r} already normalises to {label!r}, so name matching finds it without "
				"an alias. A row here would be the same fact stored twice."
			)
			return outcome

		existing = frappe.db.get_value(MERCHANT_ALIAS, key, ["name", "canonical_supplier"], as_dict=True)
		if existing:
			if str(existing.get("canonical_supplier") or "") == str(supplier):
				outcome["action"] = "unchanged"
				outcome["why"] = "this spelling was already mapped to this Supplier"
				return outcome
			frappe.db.set_value(
				MERCHANT_ALIAS,
				key,
				{"canonical_supplier": supplier, "source": outcome["source"], "alias": str(merchant)},
			)
			outcome["action"] = "repointed"
			outcome["previous_supplier"] = existing.get("canonical_supplier")
			outcome["why"] = (
				f"{key!r} pointed at {existing.get('canonical_supplier')!r} and now points at "
				f"{supplier!r} — a later decision by a person beats an earlier one."
			)
			return outcome

		doc = frappe.get_doc(
			{
				"doctype": MERCHANT_ALIAS,
				"alias": str(merchant),
				"canonical_supplier": supplier,
				"source": outcome["source"],
				"match_count": 1,
				"last_matched_on": today(),
				"first_learned_from": receipt or None,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		outcome["action"] = "created"
		outcome["why"] = f"{merchant!r} had never been seen against a Supplier before"
		return outcome
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: could not learn a merchant alias",
			message=compat.traceback_text(),
		)
		outcome["action"] = "skipped"
		outcome["why"] = "the alias register refused the row; the supplier link itself was still set"
		return outcome


def record_alias_use(name: str) -> None:
	"""Count one receipt against an alias, and stamp the day it fired.

	NEVER RAISES, and `update_modified=False`: a counter is bookkeeping about
	the mapping and not a change to it, and a failed increment must not turn a
	successful capture into an error. The count is the only measure of whether a
	taught mapping was worth teaching.
	"""
	try:
		current = int(frappe.db.get_value(MERCHANT_ALIAS, name, "match_count") or 0)
		frappe.db.set_value(
			MERCHANT_ALIAS,
			name,
			{"match_count": current + 1, "last_matched_on": today()},
			update_modified=False,
		)
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: could not count a merchant alias use",
			message=compat.traceback_text(),
		)


# ── the corpus: what receipts already linked to a Supplier can answer ────────


def _linked_receipt_rows(fieldname: str) -> list[dict]:
	"""Receipts that carry both a supplier link and the signal column asked for.

	The same argument `list_merchant_aliases` makes: the answer to "whose phone
	number is this" is already on the site, sitting on every receipt somebody
	coded by hand, and a second register holding the same fact would be a second
	place for it to be wrong. Empty on a site without the column, which is what
	makes the whole cascade degrade instead of failing.
	"""
	try:
		if not compat.has_field(expenses.EXPENSE_RECEIPT, fieldname):
			return []
		rows = frappe.db.get_all(
			expenses.EXPENSE_RECEIPT,
			filters={"supplier": ("is", "set"), fieldname: ("is", "set")},
			fields=["name", "supplier", fieldname],
			limit_page_length=5000,
		)
		return [dict(row) for row in rows]
	except Exception:
		return []


def _corpus_supplier(fieldname: str, wanted: str, compare) -> dict | None:
	"""The Supplier most receipts with this signal were coded to, and how many.

	MOST, not first: two receipts disagreeing about whose phone number this is
	means one of them was miscoded, and taking whichever sorted first would make
	the answer depend on a docname. A tie is refused — with two suppliers equally
	claimed there is no answer, and inventing one is worse than saying so.
	"""
	counts: dict = {}
	for row in _linked_receipt_rows(fieldname):
		if compare(str(row.get(fieldname) or ""), wanted):
			supplier = str(row.get("supplier") or "")
			if supplier:
				counts[supplier] = counts.get(supplier, 0) + 1
	if not counts:
		return None
	ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
	if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
		return None
	return {"supplier": ranked[0][0], "receipts": ranked[0][1]}


def _supplier_columns(candidates) -> list[str]:
	"""Which of these columns this site's Supplier actually has.

	`website` and the phone columns are all younger than the doctype and some are
	fetched rather than stored on a given version. Asking `compat` rather than
	assuming is what keeps the cascade from raising on a bench with an older
	ERPNext, where the answer should simply be "that step found nothing".
	"""
	out = []
	for name in candidates:
		try:
			if compat.has_field(masters.SUPPLIER, name):
				out.append(name)
		except Exception:
			continue
	return out


def _supplier_by_domain(domain: str) -> dict | None:
	"""A Supplier whose own website column names this domain."""
	columns = _supplier_columns(("website",))
	if not columns or not domain:
		return None
	try:
		rows = frappe.db.get_all(
			masters.SUPPLIER,
			filters={"website": ("is", "set")},
			fields=["name", "supplier_name", "website"],
			limit_page_length=3000,
		)
	except Exception:
		return None
	matches = [dict(row) for row in rows if _domains_agree(normalize_domain(row.get("website")), domain)]
	if len(matches) != 1:
		return None
	return {"supplier": matches[0]["name"], "supplier_name": matches[0].get("supplier_name")}


def _supplier_by_phone(phone: str) -> dict | None:
	"""A Supplier whose own phone column holds this number."""
	columns = _supplier_columns(("mobile_no", "phone", "primary_phone", "contact_no"))
	if not columns or not phone:
		return None
	try:
		rows = frappe.db.get_all(
			masters.SUPPLIER,
			fields=["name", "supplier_name", *columns],
			limit_page_length=3000,
		)
	except Exception:
		return None
	matches = [
		dict(row) for row in rows if any(normalize_phone(row.get(column)) == phone for column in columns)
	]
	if len(matches) != 1:
		return None
	return {"supplier": matches[0]["name"], "supplier_name": matches[0].get("supplier_name")}


def _supplier_label(supplier: str) -> str:
	"""A Supplier's own name, falling back to its docname."""
	try:
		return str(frappe.db.get_value(masters.SUPPLIER, supplier, "supplier_name") or supplier)
	except Exception:
		return str(supplier)


# ── the cascade itself ───────────────────────────────────────────────────────


def _step(name: str, method: str, *, tried: bool, matched=None, why: str = "") -> dict:
	return {
		"step": name,
		"method": method,
		"tried": tried,
		"matched": matched,
		"why": why,
	}


def resolve_merchant(
	merchant: str,
	*,
	merchant_url: str = "",
	merchant_phone: str = "",
	store_number: str = "",
	card_last_four: str = "",
	ocr_raw_text: str = "",
	resolved_merchant: str = "",
	resolution_method: str = "",
	resolution_confidence=None,
) -> dict:
	"""Five ordered steps from a merchant string to a vendor, with every step shown.

	READS ONLY. Nothing here writes anything anywhere — the callers decide what
	to do with the answer, and only one of them (an exact alias hit, which is a
	person's own decision replayed) ever sets a supplier link.

	`resolved_merchant` with `resolution_method` is a CALLER'S OWN ANSWER, and it
	short-circuits the cascade at the top: an MCP client that read the raw text
	with a model, or a person who typed the vendor in, has better evidence than
	any of the five steps and is not asked to prove it. The steps are still run
	and still reported, so the record shows what the deterministic half would have
	said — which is the only way anybody ever finds out the model was wrong.

	Every step that did not fire is in `steps` with `tried` and a reason. A
	cascade that only reported its winner would make "why didn't the URL match"
	unanswerable, and the URL not matching is the interesting half on the day
	somebody's receipts stop resolving.
	"""
	merchant = str(merchant or "")
	read = extract_receipt_signals(ocr_raw_text)
	given = {
		MERCHANT_URL_FIELD: normalize_domain(merchant_url),
		MERCHANT_PHONE_FIELD: normalize_phone(merchant_phone),
		STORE_NUMBER_FIELD: str(store_number or "").strip(),
		CARD_LAST_FOUR_FIELD: str(card_last_four or "").strip(),
	}
	# AN ARGUMENT ALWAYS BEATS THE TEXT, and the provenance is reported rather
	# than inferred: `signals_from_raw_text` names the ones this app read, so a
	# caller can tell "the phone sent me this" from "four regexes found it",
	# which are worth different amounts of trust when one turns out wrong.
	signals = {name: given[name] or read[name] for name in SIGNAL_FIELDS}
	from_text = sorted(name for name in SIGNAL_FIELDS if not given[name] and read[name])
	domain, phone = signals[MERCHANT_URL_FIELD], signals[MERCHANT_PHONE_FIELD]
	store, card = signals[STORE_NUMBER_FIELD], signals[CARD_LAST_FOUR_FIELD]

	out = {
		"merchant": merchant,
		"resolved_merchant": None,
		"supplier": None,
		"supplier_name": None,
		"method": None,
		"confidence": 0.0,
		"steps": [],
		"signals": {
			MERCHANT_URL_FIELD: domain or None,
			MERCHANT_PHONE_FIELD: phone or None,
			STORE_NUMBER_FIELD: store or None,
			CARD_LAST_FOUR_FIELD: card or None,
		},
		"signals_from_raw_text": from_text,
		"alias": None,
		"llm_context": None,
	}

	def settle(step: dict, *, supplier: str, label: str, method: str, confidence: float) -> dict:
		out["steps"].append(step)
		out["supplier"] = supplier or None
		out["supplier_name"] = label or None
		out["resolved_merchant"] = label or supplier or None
		out["method"] = method
		out["confidence"] = round(min(1.0, float(confidence)), 4)
		return out

	# ── step 0: the caller already knows ────────────────────────────────────
	given_method = str(resolution_method or "").strip()
	given_name = str(resolved_merchant or "").strip()
	if given_name or given_method:
		if given_method and given_method not in RESOLUTION_METHODS:
			raise ToolError(f"resolution_method must be one of: {', '.join(RESOLUTION_METHODS)}.")
		if not given_name:
			raise ToolError(
				"resolution_method was given without resolved_merchant. A method with no answer "
				"beside it records how nothing was decided."
			)
		confidence = _MATCH_CEILING
		if resolution_confidence not in (None, ""):
			confidence = as_float(resolution_confidence, "resolution_confidence")
			if confidence < 0 or confidence > 1:
				raise ToolError(
					f"resolution_confidence is a fraction from 0 to 1, not a percentage — got "
					f"{confidence}. A client reporting 87 means 0.87."
				)
		method = given_method or "Manual"
		out["caller_supplied"] = True
		settle(
			_step(
				"caller",
				method,
				tried=True,
				matched=given_name,
				why=(
					"the caller supplied the answer, so the cascade did not decide it. The steps "
					"below are what this app would have said, recorded for comparison."
				),
			),
			supplier="",
			label=given_name,
			method=method,
			confidence=confidence,
		)
		_deterministic_steps(out, merchant, domain, phone, record_only=True)
		return out

	# ── step 1: an alias — a person's own decision, replayed ────────────────
	if not alias_table_ready():
		out["steps"].append(
			_step(
				"alias",
				"Alias",
				tried=False,
				why=f"this site has no {MERCHANT_ALIAS} register — run `bench migrate`",
			)
		)
	else:
		row = find_alias(merchant)
		if row:
			out["alias"] = row
			source = str(row.get("source") or ALIAS_MANUAL)
			confidence = _ALIAS_MANUAL_CONFIDENCE if source == ALIAS_MANUAL else _ALIAS_LEARNED_CONFIDENCE
			label = _supplier_label(row["canonical_supplier"])
			return settle(
				_step(
					"alias",
					"Alias",
					tried=True,
					matched=row["canonical_supplier"],
					why=(
						f"{merchant!r} normalises to {row['alias_key']!r}, which is taught to "
						f"{row['canonical_supplier']!r} (source {source}, used "
						f"{int(row.get('match_count') or 0)} time(s))"
					),
				),
				supplier=row["canonical_supplier"],
				label=label,
				method="Alias",
				confidence=confidence,
			)
		out["steps"].append(
			_step("alias", "Alias", tried=True, why=f"no alias is taught for {alias_key(merchant)!r}")
		)

	# ── steps 2–4: the deterministic evidence, in order ─────────────────────
	winner = _deterministic_steps(out, merchant, domain, phone, record_only=False)
	if winner:
		return settle(
			winner["step"],
			supplier=winner["supplier"],
			label=winner["label"],
			method=winner["method"],
			confidence=winner["confidence"],
		)

	# ── step 5: the question, for a caller that has a model ─────────────────
	context_lines = _llm_context_lines(ocr_raw_text)
	out["steps"].append(
		_step(
			"llm",
			"LLM",
			tried=False,
			why=(
				"nothing deterministic cleared the floor, and this app does not call a model — "
				"llm_context carries the question for a caller that has one"
			),
		)
	)
	out["llm_context"] = {
		"merchant": merchant,
		"signals": out["signals"],
		"raw_text_lines": context_lines,
		"candidate_suppliers": _ranked_supplier_matches(merchant, top=5),
		"question": (
			f"A receipt's merchant line reads {merchant!r}. Read the signals and the raw text "
			"lines and say which vendor this actually is. Answer with the vendor's real name, "
			"and pass it back as resolved_merchant with resolution_method='LLM' and a "
			"resolution_confidence between 0 and 1. If the lines do not identify a vendor, say "
			"so rather than guessing — an unresolved receipt is reviewed by a person, and a "
			"confidently wrong one is not."
		),
		"note": (
			"NOTHING HERE HAS BEEN SENT ANYWHERE. This app makes no model call, ever — the same "
			"contract validate_document_extraction follows. This is the question; the answer "
			"comes back through resolved_merchant."
		),
	}
	return out


def _deterministic_steps(out: dict, merchant: str, domain: str, phone: str, *, record_only: bool):
	"""Steps 2, 3 and 4 — URL, phone and name — appended to `out["steps"]`.

	Returns the first one that cleared its bar, or None. `record_only` runs every
	step and returns nothing: it is the path taken when the caller supplied the
	answer, where the steps are kept as the record of what this app would have
	said and are explicitly not allowed to overrule it.
	"""
	winner = None

	# ── step 2: a domain is registered to one company ───────────────────────
	if not domain:
		out["steps"].append(
			_step("url", "URL", tried=False, why="no domain was given and none was read off the text")
		)
	elif _generic_domain(domain):
		out["steps"].append(
			_step(
				"url",
				"URL",
				tried=True,
				why=(
					f"{domain!r} is a payment processor, a survey vendor or a social site — it "
					"identifies the till's supplier, not the shop"
				),
			)
		)
	else:
		hit = _supplier_by_domain(domain)
		corpus = None if hit else _corpus_supplier(MERCHANT_URL_FIELD, domain, _url_matches)
		if hit:
			out["steps"].append(
				_step(
					"url",
					"URL",
					tried=True,
					matched=hit["supplier"],
					why=f"{domain!r} is on that Supplier's website",
				)
			)
			winner = winner or {
				"step": out["steps"][-1],
				"supplier": hit["supplier"],
				"label": hit.get("supplier_name") or hit["supplier"],
				"method": "URL",
				"confidence": _URL_CONFIDENCE,
			}
		elif corpus:
			out["steps"].append(
				_step(
					"url",
					"URL",
					tried=True,
					matched=corpus["supplier"],
					why=(
						f"{corpus['receipts']} receipt(s) carrying {domain!r} are already coded to "
						f"{corpus['supplier']!r}"
					),
				)
			)
			winner = winner or {
				"step": out["steps"][-1],
				"supplier": corpus["supplier"],
				"label": _supplier_label(corpus["supplier"]),
				"method": "URL",
				"confidence": round(_URL_CONFIDENCE - _CORPUS_PENALTY, 4),
			}
		else:
			out["steps"].append(
				_step("url", "URL", tried=True, why=f"no Supplier and no coded receipt names {domain!r}")
			)

	# ── step 3: a phone number rings in one building ────────────────────────
	if not phone:
		out["steps"].append(
			_step(
				"phone", "Phone", tried=False, why="no phone number was given and none was read off the text"
			)
		)
	else:
		hit = _supplier_by_phone(phone)
		corpus = None if hit else _corpus_supplier(MERCHANT_PHONE_FIELD, phone, _phone_matches)
		if hit:
			out["steps"].append(
				_step(
					"phone",
					"Phone",
					tried=True,
					matched=hit["supplier"],
					why=f"{phone} is that Supplier's number",
				)
			)
			if winner is None:
				winner = {
					"step": out["steps"][-1],
					"supplier": hit["supplier"],
					"label": hit.get("supplier_name") or hit["supplier"],
					"method": "Phone",
					"confidence": _PHONE_CONFIDENCE,
				}
		elif corpus:
			out["steps"].append(
				_step(
					"phone",
					"Phone",
					tried=True,
					matched=corpus["supplier"],
					why=(
						f"{corpus['receipts']} receipt(s) carrying {phone} are already coded to "
						f"{corpus['supplier']!r}"
					),
				)
			)
			if winner is None:
				winner = {
					"step": out["steps"][-1],
					"supplier": corpus["supplier"],
					"label": _supplier_label(corpus["supplier"]),
					"method": "Phone",
					"confidence": round(_PHONE_CONFIDENCE - _CORPUS_PENALTY, 4),
				}
		else:
			out["steps"].append(
				_step("phone", "Phone", tried=True, why=f"no Supplier and no coded receipt carries {phone}")
			)

	# ── step 4: the name, compared the way it always was ────────────────────
	ranked = _ranked_supplier_matches(merchant, top=5) if merchant else []
	out["ranked_name_matches"] = ranked
	best = ranked[0] if ranked and ranked[0]["confidence"] >= _MATCH_FLOOR else None
	if not merchant:
		out["steps"].append(_step("ocr", "OCR", tried=False, why="there is no merchant string to compare"))
	elif best:
		out["steps"].append(
			_step(
				"ocr",
				"OCR",
				tried=True,
				matched=best["supplier"],
				why=f"the name is {best['confidence']} similar to {best['supplier']!r} after stripping legal-form words",
			)
		)
		if winner is None:
			winner = {
				"step": out["steps"][-1],
				"supplier": best["supplier"],
				"label": best.get("supplier_name") or best["supplier"],
				"method": "OCR",
				"confidence": best["confidence"],
			}
	else:
		top = ranked[0]["confidence"] if ranked else 0.0
		out["steps"].append(
			_step(
				"ocr",
				"OCR",
				tried=True,
				why=f"the best name similarity was {top}, below the {_MATCH_FLOOR} floor",
			)
		)

	return None if record_only else winner


def _url_matches(stored: str, wanted: str) -> bool:
	return _domains_agree(normalize_domain(stored), wanted)


def _phone_matches(stored: str, wanted: str) -> bool:
	return normalize_phone(stored) == wanted


def normalize_merchant(args: dict) -> ToolResult:
	"""The best-matching Supplier for a merchant string, and how confident that is.

	Read-only, and touches nothing: it exists so a client can show a picker
	"did you mean Wilbur-Ellis Company LLC?" and let a person confirm it, the
	same way `classify_receipt` proposes a register and shows its evidence. The
	match is never written anywhere by this call — `update_expense_receipt` or
	`submit_expense_receipt`'s own `supplier` argument is how a human accepts it.

	v0.75.0. THE NAME IS NO LONGER THE ONLY EVIDENCE. Pass `merchant_url`,
	`merchant_phone` or the whole `ocr_raw_text` and the five-step cascade runs:
	a taught alias, then a domain, then a phone number, then the name similarity
	this tool has always done, and finally the question for a caller that has a
	model. `match` and `alternatives` are unchanged and still mean what they
	always meant — the NAME match specifically — so a client written against the
	old shape keeps working; `resolution` is the cascade's own answer beside it.
	"""
	compat.require_doctype(masters.SUPPLIER, masters._HINT)
	merchant = as_str(args, "merchant", required=True)

	resolution = resolve_merchant(
		merchant,
		merchant_url=as_str(args, "merchant_url"),
		merchant_phone=as_str(args, "merchant_phone"),
		store_number=as_str(args, "store_number"),
		# Checked here too, though this tool writes nothing: a caller that sent a
		# full card number to the read tool would get no refusal, learn nothing,
		# and send it again to the one that stores things.
		card_last_four=card_last_four(as_str(args, "card_last_four")),
		ocr_raw_text=as_str(args, "ocr_raw_text") or as_str(args, "text"),
	)

	ranked = resolution.get("ranked_name_matches") or _ranked_supplier_matches(merchant, top=5)
	best = ranked[0] if ranked and ranked[0]["confidence"] >= _MATCH_FLOOR else None
	alternatives = [row for row in ranked if row is not best]

	data = {
		"merchant": merchant,
		"match": best,
		"alternatives": alternatives,
		"threshold": _MATCH_FLOOR,
		"suppliers_considered": len(_candidate_suppliers()),
		"resolution": resolution,
		"note": (
			"`match` is the NAME similarity alone, after stripping punctuation and legal-form "
			"words (Co, LLC, Inc, Corp, Ltd …) from both sides — never a fitted model, so a low "
			"score does not mean the two are unrelated, only that their names read differently. "
			"`resolution` is the full cascade: a taught alias first, then a domain, then a phone "
			"number, then that name score, and `resolution.steps` says what every step found and "
			"what it did not. This suggests a link; it does not set one."
		),
	}
	if resolution.get("method") and resolution["method"] != "OCR":
		summary = (
			f"{merchant!r} resolves to {resolution['resolved_merchant']} by "
			f"{resolution['method']} (confidence {resolution['confidence']})"
		)
	elif best:
		summary = (
			f"best match for {merchant!r}: {best['supplier_name'] or best['supplier']} "
			f"(confidence {best['confidence']})"
		)
	else:
		summary = f"no Supplier scored above {_MATCH_FLOOR} for {merchant!r}"
	return ToolResult(data=data, summary=summary)


def list_merchant_aliases(args: dict) -> ToolResult:
	"""Merchant strings already linked to a Supplier, grouped by which Supplier.

	TWO ANSWERS, AND THEY ARE DIFFERENT QUESTIONS. `aliases` is derived: every
	Expense Receipt whose `supplier` was set, read back grouped by that Supplier.
	It is the whole history of what has been coded to whom, it costs no table,
	and the day a receipt's supplier link changes it changes with it for free.

	`taught` is the Merchant Alias register — the subset of that history somebody
	turned into a RULE, which the resolver reads at step 1 of its cascade. A
	spelling can be in the first and not the second, and that is not a gap: a
	merchant string that already normalises to its Supplier's own name needs no
	rule, because name matching finds it. The `match_count` on a taught row is
	the only figure here that says whether teaching it was worth anything.
	"""
	filters: dict = {"supplier": ("is", "set")}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	supplier = as_str(args, "supplier")
	if supplier:
		if not frappe.db.exists(masters.SUPPLIER, supplier):
			raise ToolError(f"no Supplier called {supplier!r} on this site.")
		filters["supplier"] = supplier

	rows = frappe.db.get_all(
		expenses.EXPENSE_RECEIPT,
		filters=filters,
		fields=["merchant", "supplier"],
		limit_page_length=5000,
	)

	by_supplier: dict = {}
	for row in rows:
		entry = by_supplier.setdefault(row["supplier"], {})
		merchant = row.get("merchant") or "<blank>"
		entry[merchant] = entry.get(merchant, 0) + 1

	names = sorted(by_supplier)
	labels = (
		{
			r["name"]: r.get("supplier_name")
			for r in frappe.db.get_all(
				masters.SUPPLIER, filters={"name": ("in", names)}, fields=["name", "supplier_name"]
			)
		}
		if names
		else {}
	)

	aliases = []
	for name in names:
		merchants = by_supplier[name]
		receipt_count = sum(merchants.values())
		aliases.append(
			{
				"supplier": name,
				"supplier_name": labels.get(name),
				"receipt_count": receipt_count,
				"merchant_strings": [
					{"merchant": merchant, "receipt_count": count}
					for merchant, count in sorted(merchants.items(), key=lambda kv: (-kv[1], kv[0]))
				],
			}
		)
	aliases.sort(key=lambda row: (-row["receipt_count"], row["supplier"]))

	taught = _taught_aliases(supplier)

	data = {
		"aliases": aliases,
		"count": len(aliases),
		"taught": taught,
		"taught_count": len(taught),
		"alias_register_installed": alias_table_ready(),
		"filters": {"company": filters.get("company") or None, "supplier": supplier or None},
		"note": (
			"`aliases` is built from Expense Receipt rows whose supplier is already set — the whole "
			"history of what has been coded to whom, costing no table. A merchant string appears "
			"once per distinct spelling a receipt was captured with, so 'WILBUR ELLIS CO' and "
			"'Wilbur Ellis #14' both list under the same Supplier once each has been linked at "
			"least once.\n\n"
			"`taught` is the Merchant Alias register: the subset of that history somebody turned "
			"into a rule the resolver reads first. A spelling missing from it is usually not a gap "
			"— one that already normalises to its Supplier's own name needs no rule, because name "
			"matching finds it. `match_count` is how many receipts each rule has resolved."
		),
	}
	return ToolResult(
		data=data,
		summary=f"{len(aliases)} supplier(s) with a known merchant alias, {len(taught)} taught rule(s)",
	)


def _taught_aliases(supplier: str = "") -> list[dict]:
	"""The Merchant Alias register, most-used first, or [] on a site without it."""
	if not alias_table_ready():
		return []
	filters = {"canonical_supplier": supplier} if supplier else {}
	rows = frappe.db.get_all(
		MERCHANT_ALIAS,
		filters=filters,
		fields=list(_ALIAS_FIELDS),
		limit_page_length=5000,
		order_by="match_count desc, name asc",
	)
	out = []
	for row in rows:
		entry = dict(row)
		entry["match_count"] = int(entry.get("match_count") or 0)
		entry["last_matched_on"] = str(entry["last_matched_on"]) if entry.get("last_matched_on") else None
		entry["supplier_name"] = _supplier_label(entry.get("canonical_supplier") or "")
		out.append(entry)
	return out


# ── create_purchase_invoice_from_receipt ─────────────────────────────────────

#: Keyword groups an Expense Receipt category is matched against a company's
#: leaf Expense accounts with. Same shape as governance._DISTRIBUTION_KEYWORDS:
#: a short, readable, auditable table rather than a fitted mapping.
_CATEGORY_ACCOUNT_KEYWORDS = {
	"Fuel": ("fuel",),
	"Equipment Parts": ("equipment", "repair", "maintenance", "parts"),
	"Supplies": ("supplies",),
	"Hardware": ("hardware",),
	"Feed": ("feed",),
	"Seed": ("seed",),
	"Fertilizer": ("fertilizer", "chemical", "spray"),
	"Other": ("miscellaneous", "sundry", "other", "general"),
}


def _leaf_expense_accounts(company: str) -> list[dict]:
	fields = compat.existing_fields(
		"Account", ("name", "account_name", "account_number", "is_group", "disabled", "root_type")
	)
	rows = frappe.db.get_all(
		"Account",
		filters={"company": company, "root_type": "Expense"},
		fields=fields,
		order_by="name asc",
		limit=500,
	)
	return [
		dict(row) for row in rows if not int(row.get("is_group") or 0) and not int(row.get("disabled") or 0)
	]


def _match_expense_account(company: str, category: str) -> str:
	"""One leaf Expense account whose name matches a receipt category.

	Same shape and same reasoning as `governance._match_equity_account`:
	refuses on zero matches and on more than one rather than picking the first
	one that sorts first, because either silent choice puts a receipt on a P&L
	line nobody chose and nobody notices until the category total is wrong.
	"""
	keywords = _CATEGORY_ACCOUNT_KEYWORDS.get(category, (category.lower(),))
	candidates = _leaf_expense_accounts(company)
	matches = [
		row
		for row in candidates
		if any(keyword in str(row.get("account_name") or "").lower() for keyword in keywords)
	]
	if len(matches) == 1:
		return matches[0]["name"]
	listing = ", ".join(row["name"] for row in candidates) or "<none>"
	if not matches:
		raise ToolError(
			f"could not find an expense account for category {category!r} on {company}: no leaf "
			f"Expense account is named after {', '.join(keywords)}. Name it explicitly with "
			f"expense_account, or create one with create_account. Leaf expense accounts on this "
			f"company: {listing}. Nothing was created."
		)
	raise ToolError(
		f"{len(matches)} leaf Expense accounts could be the {category} account for {company}: "
		f"{', '.join(row['name'] for row in matches)}. Name it explicitly with expense_account. "
		"Nothing was created."
	)


_RECEIPT_FIELDS_FOR_PI = (
	"merchant",
	"amount",
	"receipt_date",
	"category",
	"status",
	"company",
	"supplier",
	"cost_center",
	"linked_doctype",
	"linked_document",
	"notes",
)


#: One non-stock Item per category, found or created — never one per receipt.
#: `purchasing.create_purchase_invoice` requires an `item_code` on every line
#: (it is the general purchasing pipeline's tool, and ERPNext's own Purchase
#: Invoice Item makes one mandatory there); a receipt has no Item behind it at
#: all. Rather than invent a fresh Item per receipt — which would flood the
#: Item master with one-off rows for the same eight categories this app
#: already has a closed list of — this resolves to a stable, shared,
#: non-stock Item per category, created once and reused by every receipt in
#: it after that.
_CATEGORY_ITEM_PREFIX = "EXP-"


def _service_item_for_category(category: str) -> str:
	item_code = f"{_CATEGORY_ITEM_PREFIX}{category.upper().replace(' ', '-')}"
	if frappe.db.exists(masters.ITEM, item_code):
		return item_code
	created = masters.create_item(
		{
			"item_code": item_code,
			"item_name": f"{category} (Expense)",
			"is_stock_item": False,
			"description": (
				f"Non-stock service item for {category} expense receipts. Created automatically by "
				"create_purchase_invoice_from_receipt the first time this category was billed."
			),
		}
	)
	return created.data["name"]


def create_purchase_invoice_from_receipt(args: dict) -> ToolResult:
	"""Turn one APPROVED Expense Receipt into a DRAFT Purchase Invoice.

	BUILDS ON `purchasing.create_purchase_invoice` rather than writing the
	document itself — this tool's whole job is deciding what to hand that one:
	which Supplier, which expense account, which Item. The Purchase Invoice
	itself is whatever the general purchasing pipeline would produce for a
	human who typed the same choices in by hand.

	APPROVED ONLY. A Submitted receipt has not yet been looked at by anyone but
	the foreman who photographed it; billing it would put an unreviewed number
	into the payables ledger. `approve_expense_receipt` is the gate.

	OWNER DRAW IS REFUSED BY NAME. `create_owner_draw` exists precisely because
	an owner draw is equity leaving the company, not a bill from a vendor, and
	this tool would otherwise happily invent a Supplier called after the
	owner's own name and post it as a payable.

	THE SUPPLIER, IN ORDER: the receipt's own `supplier` link if one is already
	set; the `supplier` argument if given; otherwise the best match
	`normalize_merchant` would return, used automatically ONLY above a high
	confidence bar; and failing all three, a brand-new Supplier created from
	the merchant string, exactly as the merchant printed it. Whichever branch
	ran is reported as `supplier_resolved_by` — this is the one tool in the app
	that links or creates a Supplier without a human confirming the match
	first, and it says so rather than leaving the caller to guess.

	THE EXPENSE ACCOUNT is matched from the receipt's category the same way a
	member distribution's equity account is matched from event type — a short
	keyword table against the company's own leaf Expense accounts — or named
	explicitly with `expense_account`.

	ONE LINE, AGAINST A SHARED NON-STOCK ITEM per category (`item` overrides
	this with a real stock Item). The merchant string, the notes and the
	photograph stay on the Expense Receipt — `linked_document` is how a reader
	gets from the invoice back to them.

	ALWAYS A DRAFT. `purchasing.create_purchase_invoice` never submits — review
	it and post it in ERPNext, or with `submit_purchase_invoice` if this site
	enables it.
	"""
	compat.require_doctype(
		PURCHASE_INVOICE, "It ships with ERPNext's Buying module — install ERPNext to use this tool."
	)
	receipt_name = as_str(args, "receipt") or as_str(args, "expense_receipt") or as_str(args, "name")
	if not receipt_name:
		raise ToolError("receipt (the Expense Receipt docname) is required.")
	if not frappe.db.exists(expenses.EXPENSE_RECEIPT, receipt_name):
		raise ToolError(f"no Expense Receipt called {receipt_name!r} on this site.")

	receipt = frappe.db.get_value(
		expenses.EXPENSE_RECEIPT, receipt_name, list(_RECEIPT_FIELDS_FOR_PI), as_dict=True
	)

	if receipt.get("status") != expenses.APPROVED:
		raise ToolError(
			f"expense receipt {receipt_name} is {receipt.get('status')!r}, not Approved. Billing an "
			f"unreviewed receipt would put a number nobody has checked into the payables ledger — "
			f"approve it first with approve_expense_receipt. Nothing was created."
		)
	if receipt.get("category") == expenses.OWNER_DRAW_CATEGORY:
		raise ToolError(
			f"expense receipt {receipt_name} is categorised {expenses.OWNER_DRAW_CATEGORY!r}, which "
			"is equity leaving the company, not a bill from a vendor. Use create_owner_draw instead. "
			"Nothing was created."
		)
	if receipt.get("linked_document"):
		raise ToolError(
			f"expense receipt {receipt_name} is already linked to "
			f"{receipt.get('linked_doctype')} {receipt.get('linked_document')}. Nothing was created."
		)
	amount = float(receipt.get("amount") or 0)
	if amount <= 0:
		raise ToolError(f"expense receipt {receipt_name} has amount {amount} — nothing to bill.")

	company = receipt.get("company")
	merchant = receipt.get("merchant") or ""
	category = receipt.get("category") or "Other"

	supplier = receipt.get("supplier")
	supplier_resolved_by = "receipt link"
	persist_supplier = False
	if not supplier:
		explicit_supplier = as_str(args, "supplier")
		if explicit_supplier:
			if not frappe.db.exists(masters.SUPPLIER, explicit_supplier):
				raise ToolError(
					f"no Supplier called {explicit_supplier!r} on this site. Nothing was created."
				)
			supplier = explicit_supplier
			supplier_resolved_by = "argument"
			persist_supplier = True
		else:
			best = _ranked_supplier_matches(merchant, top=1)
			if best and best[0]["confidence"] >= _AUTO_LINK_THRESHOLD:
				supplier = best[0]["supplier"]
				supplier_resolved_by = f"matched to merchant name (confidence {best[0]['confidence']})"
				persist_supplier = True
			else:
				created = masters.create_supplier({"supplier_name": merchant})
				supplier = created.data["name"]
				supplier_resolved_by = "created from merchant name — no confident match found"
				persist_supplier = True

	expense_account = as_str(args, "expense_account")
	if expense_account:
		expense_account = resolve_account(expense_account, company)
		account_resolved_by = "argument"
	else:
		expense_account = _match_expense_account(company, category)
		account_resolved_by = "category match"

	cost_center = as_str(args, "cost_center") or receipt.get("cost_center") or None
	posting_date = as_date(args, "posting_date") or str(receipt.get("receipt_date") or "")
	item = as_str(args, "item")
	if item:
		if not frappe.db.exists(masters.ITEM, item):
			raise ToolError(f"no Item called {item!r} on this site. Nothing was created.")
		item_resolved_by = "argument"
	else:
		item = _service_item_for_category(category)
		item_resolved_by = "shared category item"

	pi_args = {
		"company": company,
		"supplier": supplier,
		"posting_date": posting_date,
		"items": [
			{
				"item_code": item,
				"qty": 1,
				"rate": amount,
				"expense_account": expense_account,
				**({"cost_center": cost_center} if cost_center else {}),
			}
		],
	}
	bill_no = as_str(args, "bill_no")
	if bill_no:
		pi_args["bill_no"] = bill_no
	due_date = as_date(args, "due_date")
	if due_date:
		pi_args["due_date"] = due_date
	credit_to = as_str(args, "credit_to")
	if credit_to:
		pi_args["credit_to"] = credit_to

	result = purchasing.create_purchase_invoice(pi_args)
	pi_name = result.data["name"]

	updates = {"linked_doctype": PURCHASE_INVOICE, "linked_document": pi_name}
	if persist_supplier:
		updates["supplier"] = supplier
	frappe.db.set_value(expenses.EXPENSE_RECEIPT, receipt_name, updates)

	data = {
		"purchase_invoice": pi_name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"expense_receipt": receipt_name,
		"supplier": supplier,
		"supplier_resolved_by": supplier_resolved_by,
		"company": company,
		"amount": amount,
		"expense_account": expense_account,
		"expense_account_resolved_by": account_resolved_by,
		"item": item,
		"item_resolved_by": item_resolved_by,
		"cost_center": cost_center,
		"credit_to": result.data.get("credit_to"),
		"posting_date": posting_date,
		"next_step": (
			f"Purchase Invoice {pi_name} is a DRAFT and has moved no balance. Review it in ERPNext, "
			"or submit it with submit_purchase_invoice if this site enables that tool."
		),
	}
	return ToolResult(
		data=data,
		summary=f"created draft Purchase Invoice {pi_name} for {supplier} ({amount}) from "
		f"expense receipt {receipt_name}",
		docstatus_delta="none → 0 (draft)",
	)
