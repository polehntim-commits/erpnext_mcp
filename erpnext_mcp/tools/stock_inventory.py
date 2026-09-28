# SPDX-License-Identifier: MIT
"""Stock and inventory: Stock Entry, Stock Ledger Entry, Bin and reorder levels.

Sprint 4 of the Gap Closure Plan (v0.69.0). `list_warehouses`, `list_items` and
`get_item` (v0.66.0, `tools/masters.py`) can name a shed and a chemical; nothing
until this module could say how much of the chemical is *in* the shed, how it
got there, or when to buy more. That is the whole of what this module adds:
moving stock, reading the balance the movement produced, and the reorder rule
that turns a balance into an alert.

FOUR ERPNEXT DOCTYPES, AND WHICH QUESTION EACH ONE ANSWERS. They are easy to
confuse, and reading the wrong one gives an answer that looks right:

  * **Stock Entry** is the *instruction* — "move 40 lb of Surround from Stores
    to the Shop". It is submittable, and until it is submitted it has moved
    nothing at all.
  * **Stock Ledger Entry** is the *history* — one immutable row per item per
    warehouse per movement, written by ERPNext when a document that touches
    stock is submitted. This is the audit trail; nothing here ever writes one.
  * **Bin** is the *balance* — one row per item per warehouse, carrying
    `actual_qty`, `valuation_rate` and `stock_value` as of now. ERPNext
    maintains it from the ledger.
  * **Item Reorder** is the *rule* — a child row on the Item saying "below this
    many in that warehouse, buy this many more".

THE SAME DRAFT/SUBMIT SPLIT AS `mutate.py` AND `purchasing.py`.
`create_stock_entry` can only ever produce a draft, which moves nothing;
`submit_stock_entry` is the separate, separately-switched tool that actually
moves the stock. The reason is the one `mutate.py` gives for Journal Entry, and
it is sharper here: a submitted Stock Entry writes ledger rows that change every
downstream valuation, and un-doing it is a cancellation somebody has to explain,
not an edit.

EVERY WRITE GOES THROUGH `frappe.get_doc(...).insert()` / `.submit()`. This
module computes no valuation, writes no Stock Ledger Entry and updates no Bin.
ERPNext's own controllers do all three the moment `.submit()` runs, exactly as
they would for a human in the Desk. What this module does is resolve names to
docnames, put each line's warehouse in the *right column* for the entry type
(see below), refuse a UOM it cannot convert, and read back what ERPNext
computed.

ONE `warehouse` ARGUMENT, TWO COLUMNS, AND THE ENTRY TYPE DECIDES WHICH.
`Stock Entry Detail` has `s_warehouse` (out of) and `t_warehouse` (into), and
which one a line's warehouse belongs in is not something a caller should have to
know:

  * **Material Receipt** — stock arriving from outside. `warehouse` is where it
    lands (`t_warehouse`); there is no source.
  * **Material Issue** — stock consumed or written off. `warehouse` is where it
    leaves from (`s_warehouse`); there is no target.
  * **Material Transfer** — `warehouse` is the source and `target_warehouse` is
    required and must differ.

A `target_warehouse` on a Receipt or an Issue is REFUSED rather than ignored,
because the two readings of "I passed both" — "put it in the other column" and
"I misread the entry type" — have opposite consequences, and guessing between
them is how stock lands in the wrong shed.

A UOM THIS SITE CANNOT CONVERT IS A REFUSAL, NOT A GUESS. A caller who says
`qty: 3, uom: "Case"` on an item stocked in Lb means something specific, and
the only place the factor lives is the Item's own `uoms` table. Defaulting the
conversion factor to 1 would post three pounds where thirty-six were meant —
wrong, and wrong quietly, in a column somebody values a season later. So an
unconvertible UOM raises with the item's stock UOM named and nothing written.

SOURCE LINKAGE IS NATIVE WHERE ERPNEXT HAS A FIELD AND A REMARKS MARKER WHERE
IT DOES NOT. ERPNext's Stock Entry carries specific link fields — `work_order`,
`purchase_order`, `delivery_note_no` and a few more — and no generic
(doctype, name) pair. A farm's real sources are often neither: a Farm Task that
consumed the spray, a Scale Ticket that brought the bin in. Rather than install
a custom field for a linkage that is mostly informational, `source_doctype` /
`source_name` writes the native field when one exists for that doctype and
otherwise records `[source: <doctype> <name>]` as the first line of `remarks` —
which `get_stock_entry` reads back, reporting `stored_on` either way so a caller
knows whether the link is queryable or just legible.

AN ITEM WITH NO BIN ROW HAS NEVER MOVED, WHICH IS NOT THE SAME AS ZERO. ERPNext
creates a Bin the first time an item touches a warehouse, so "no row" and "row
saying 0" are different facts and `get_stock_balance` says which it found.
`list_reorder_alerts` deliberately collapses them — an item with a reorder rule
and no Bin at all is at zero against that rule, and it is the single most
important row in the report, not one to omit for want of a record.
"""

from __future__ import annotations

import re

import frappe

from .. import compat, rodent_bait
from ..args import MAX_LIMIT, as_date, as_float, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import masters

STOCK_ENTRY = "Stock Entry"
STOCK_ENTRY_DETAIL = "Stock Entry Detail"
STOCK_ENTRY_TYPE = "Stock Entry Type"
STOCK_LEDGER_ENTRY = "Stock Ledger Entry"
BIN = "Bin"
ITEM = "Item"
ITEM_REORDER = "Item Reorder"
WAREHOUSE = "Warehouse"
BATCH = "Batch"

#: What `require_doctype` says when one of these is missing. All four ship with
#: ERPNext's Stock module, so the actionable half is "install ERPNext", not
#: "run bench migrate".
_HINT = "It ships with ERPNext's Stock module."

#: The three purposes this module writes, and where each one puts a line's
#: warehouse: `(source_column, target_column)`. A purpose absent from here —
#: Manufacture, Repack, Send to Subcontractor — is one whose semantics are not
#: a warehouse pair, and is refused rather than approximated.
ENTRY_TYPE_COLUMNS = {
	"Material Receipt": (None, "t_warehouse"),
	"Material Issue": ("s_warehouse", None),
	"Material Transfer": ("s_warehouse", "t_warehouse"),
}

ENTRY_TYPES = tuple(ENTRY_TYPE_COLUMNS)

#: Stock Entry's own link fields, by the doctype each one points at. A
#: `source_doctype` in here is stored queryably; anything else falls back to the
#: remarks marker. `purchase_receipt_no` and `delivery_note_no` are named that
#: way on ERPNext's own doctype — the `_no` suffix is theirs, not a typo here.
SOURCE_LINK_FIELDS = {
	"Work Order": "work_order",
	"Purchase Order": "purchase_order",
	"Purchase Receipt": "purchase_receipt_no",
	"Delivery Note": "delivery_note_no",
	"Sales Invoice": "sales_invoice_no",
	"Pick List": "pick_list",
	"Stock Entry": "outgoing_stock_entry",
}

#: How a non-native source is written into `remarks`, and read back out. The
#: name is the last whitespace-separated token because docnames never contain a
#: space and doctype labels frequently do — "Farm Task FT-2026-0001" splits
#: correctly, "Scale Ticket ST-9" too.
SOURCE_MARKER_RE = re.compile(r"^\[source:\s+(?P<doctype>.+?)\s+(?P<name>[^\s\]]+)\]\s*$")


def _source_marker(doctype: str, name: str) -> str:
	return f"[source: {doctype} {name}]"


# ── shared resolvers ─────────────────────────────────────────────────────────


def _resolve_item(value: str, label: str = "item_code") -> str:
	"""An Item docname from a docname or an item_name."""
	value = (value or "").strip()
	if not value:
		raise ToolError(f"{label} is required.")
	if frappe.db.exists(ITEM, value):
		return value
	matches = frappe.db.get_all(ITEM, filters={"item_name": value}, pluck="name", limit=5)
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ToolError(
			f"{value!r} matches {len(matches)} items: {', '.join(sorted(matches))}. Pass the item_code."
		)
	raise ToolError(f"no Item called {value!r} on this site. Call list_items to find it.")


def _resolve_warehouse(value: str, label: str = "warehouse", company: str = "") -> str:
	"""A Warehouse docname, checked against `company` when one was given.

	Warehouse is the one master in this app that genuinely is company-scoped
	(see `masters.py`'s module docstring), so a mismatch here is a real error
	rather than a filter that quietly returns nothing.
	"""
	value = (value or "").strip()
	if not value:
		raise ToolError(f"{label} is required.")
	if not frappe.db.exists(WAREHOUSE, value):
		raise ToolError(f"no Warehouse called {value!r} on this site. Call list_warehouses to find it.")
	if company:
		found = frappe.db.get_value(WAREHOUSE, value, "company")
		if found and found != company:
			raise ToolError(f"warehouse {value!r} belongs to company {found!r}, not {company!r}")
	return value


def _company_warehouses(company: str) -> list[str]:
	"""Every Warehouse docname belonging to `company`."""
	return list(frappe.db.get_all(WAREHOUSE, filters={"company": company}, pluck="name") or [])


def _warehouse_company(warehouse: str) -> str | None:
	return frappe.db.get_value(WAREHOUSE, warehouse, "company")


def _entry_type(args: dict) -> str:
	given = as_str(args, "entry_type", required=True)
	for known in ENTRY_TYPES:
		if known.lower() == given.lower():
			return known
	raise ToolError(
		f"entry_type must be one of: {', '.join(ENTRY_TYPES)}. Got {given!r}. Nothing was created. "
		"A Manufacture, Repack or Subcontracting entry is not a warehouse-pair movement and is "
		"not something this tool writes — create it in ERPNext."
	)


def _conversion(item_code: str, uom: str, label: str) -> tuple[str, float]:
	"""The UOM to store on a line and the factor from it to the item's stock UOM.

	Returns `(uom, conversion_factor)`. An empty or stock UOM is factor 1. Any
	other UOM is looked up on the Item's own `uoms` table, and REFUSED when the
	site has no conversion for it — see the module docstring for why defaulting
	to 1 is the one outcome worth failing a call to avoid.
	"""
	stock_uom = str(frappe.db.get_value(ITEM, item_code, "stock_uom") or "")
	uom = (uom or "").strip()
	if not uom or (stock_uom and uom.lower() == stock_uom.lower()):
		return (uom or stock_uom, 1.0)
	if compat.has_field(ITEM, "uoms") and compat.doctype_exists("UOM Conversion Detail"):
		rows = frappe.db.get_all(
			"UOM Conversion Detail",
			filters={"parent": item_code, "parenttype": ITEM, "uom": uom},
			fields=["uom", "conversion_factor"],
			limit=1,
		)
		if rows:
			factor = float(rows[0].get("conversion_factor") or 0)
			if factor > 0:
				return (uom, factor)
	raise ToolError(
		f"{label}: Item {item_code} is stocked in {stock_uom or 'an unrecorded UOM'} and this site "
		f"has no conversion from {uom!r}. Send qty in the stock UOM, or add the conversion to the "
		f"Item's UOMs table first. Nothing was created."
	)


# ── Stock Entry ──────────────────────────────────────────────────────────────


def _entry_lines(raw, entry_type: str, company: str) -> list[dict]:
	"""Validate the caller's lines into the shape Stock Entry Detail expects."""
	source_column, target_column = ENTRY_TYPE_COLUMNS[entry_type]
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"items must be a non-empty list of objects, each with item_code, qty and warehouse, "
			'e.g. [{"item_code": "SURROUND-WP", "qty": 40, "warehouse": "Stores - ETC"}]'
		)

	lines = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"items[{index}] must be an object, got {type(entry).__name__}")
		label = f"items[{index}]"
		item_code = _resolve_item(as_str(entry, "item_code") or as_str(entry, "item"), f"{label}.item_code")
		qty = as_float(entry.get("qty"), f"{label}.qty")
		if qty <= 0:
			raise ToolError(
				f"{label}.qty must be positive, got {qty}. A movement out of a warehouse is a "
				f"Material Issue, not a negative Material Receipt."
			)
		warehouse = _resolve_warehouse(as_str(entry, "warehouse"), f"{label}.warehouse", company)
		target = as_str(entry, "target_warehouse")

		line: dict = {"item_code": item_code, "qty": qty}
		uom, factor = _conversion(item_code, as_str(entry, "uom"), label)
		if uom:
			line["uom"] = uom
			line["stock_uom"] = frappe.db.get_value(ITEM, item_code, "stock_uom")
		line["conversion_factor"] = factor
		line["transfer_qty"] = round(qty * factor, 6)

		if entry_type == "Material Transfer":
			if not target:
				raise ToolError(
					f"{label}.target_warehouse is required on a Material Transfer — warehouse is "
					f"where the stock leaves from and target_warehouse is where it lands. "
					f"Nothing was created."
				)
			target = _resolve_warehouse(target, f"{label}.target_warehouse", company)
			if target == warehouse:
				raise ToolError(
					f"{label}: source and target warehouse are both {warehouse!r}. A transfer to "
					f"the same shed moves nothing. Nothing was created."
				)
		elif target:
			raise ToolError(
				f"{label}.target_warehouse is not accepted on a {entry_type} — on a Material "
				f"Receipt `warehouse` is already where the stock lands, and on a Material Issue "
				f"it is where the stock leaves from. Use entry_type 'Material Transfer' to move "
				f"between two warehouses. Nothing was created."
			)

		if source_column:
			line[source_column] = warehouse
		if target_column:
			line[target_column] = target if entry_type == "Material Transfer" else warehouse

		rate = entry.get("basic_rate")
		if rate not in (None, ""):
			rate = as_float(rate, f"{label}.basic_rate")
			if rate < 0:
				raise ToolError(f"{label}.basic_rate cannot be negative, got {rate}")
			line["basic_rate"] = rate
			line["basic_amount"] = round(qty * factor * rate, 2)
		elif entry_type == "Material Receipt" and _unvalued(item_code):
			# v0.201.0. A RECEIPT WITH NO COST, OF A PRODUCT THAT HAS NEVER HAD ONE.
			# PROWLER™ was registered from a store shelf with valuation_rate 0, and
			# a receipt of it with no rate files as a draft that ERPNext then refuses
			# to submit ("Valuation Rate for the Item … is required"). Marked as a
			# zero-valuation receipt instead — a bait block given away by a
			# supplier, or one whose cost is on a bill filed elsewhere, is a real
			# thing — and the answer names it, so nobody reads a zero as a price.
			if compat.has_field(STOCK_ENTRY_DETAIL, "allow_zero_valuation_rate"):
				line["allow_zero_valuation_rate"] = 1
			line["_zero_valued"] = True

		batch_no = as_str(entry, "batch_no")
		if batch_no:
			if compat.doctype_exists(BATCH) and not frappe.db.exists(BATCH, batch_no):
				raise ToolError(f"{label}.batch_no {batch_no!r} is not a Batch on this site.")
			line["batch_no"] = batch_no

		lines.append(line)
	return lines


def _apply_source(doc, args: dict) -> dict | None:
	"""Record where this movement came from, natively where ERPNext has a field.

	Both arguments or neither: a `source_name` with no doctype is unresolvable,
	and a `source_doctype` with no name records nothing. Returns what was stored
	and where, so the tool can report it rather than leaving the caller to guess.
	"""
	source_doctype = as_str(args, "source_doctype")
	source_name = as_str(args, "source_name")
	if not source_doctype and not source_name:
		return None
	if not source_doctype or not source_name:
		raise ToolError(
			"source_doctype and source_name go together — pass both or neither. Nothing was created."
		)
	if not compat.doctype_exists(source_doctype):
		raise ToolError(f"no DocType called {source_doctype!r} on this site. Nothing was created.")
	if not frappe.db.exists(source_doctype, source_name):
		raise ToolError(f"no {source_doctype} called {source_name!r} on this site. Nothing was created.")

	field = SOURCE_LINK_FIELDS.get(source_doctype)
	if field and compat.has_field(STOCK_ENTRY, field):
		doc.set(field, source_name)
		return {"doctype": source_doctype, "name": source_name, "stored_on": f"Stock Entry.{field}"}
	return {"doctype": source_doctype, "name": source_name, "stored_on": "Stock Entry.remarks marker"}


def _read_source(doc) -> dict | None:
	"""The source linkage `_apply_source` wrote, from wherever it landed."""
	for doctype, field in SOURCE_LINK_FIELDS.items():
		if compat.has_field(STOCK_ENTRY, field) and doc.get(field):
			return {"doctype": doctype, "name": doc.get(field), "stored_on": f"Stock Entry.{field}"}
	first_line = str(doc.get("remarks") or "").splitlines()[:1]
	if first_line:
		match = SOURCE_MARKER_RE.match(first_line[0].strip())
		if match:
			return {
				"doctype": match.group("doctype"),
				"name": match.group("name"),
				"stored_on": "Stock Entry.remarks marker",
			}
	return None


def _line_qty(row) -> float:
	"""A line's quantity in the item's own stock UOM.

	`transfer_qty` is the converted figure ERPNext values and ledgers against;
	`qty` is what the caller typed. They differ exactly when a UOM conversion
	applied, and a total built from the wrong one is off by the factor.
	"""
	if row.get("transfer_qty") not in (None, ""):
		return float(row.get("transfer_qty") or 0)
	return float(row.get("qty") or 0)


def _entry_items_out(doc) -> list[dict]:
	rows = []
	for row in doc.get("items") or []:
		rows.append(
			{
				"item_code": row.get("item_code"),
				"item_name": row.get("item_name"),
				"qty": float(row.get("qty") or 0),
				"uom": row.get("uom"),
				"stock_uom": row.get("stock_uom"),
				"conversion_factor": float(row.get("conversion_factor") or 1),
				"stock_qty": _line_qty(row),
				"warehouse": row.get("s_warehouse") or row.get("t_warehouse"),
				"source_warehouse": row.get("s_warehouse"),
				"target_warehouse": row.get("t_warehouse"),
				"batch_no": row.get("batch_no"),
				"basic_rate": float(row.get("basic_rate") or 0),
				"basic_amount": float(row.get("basic_amount") or 0),
			}
		)
	return rows


def _entry_value(doc, items: list[dict]) -> float:
	"""What the movement is worth, from ERPNext's own total where it has one.

	A draft Material Issue or Transfer usually carries no rate at all — ERPNext
	values those from the item's existing valuation at submit, not from anything
	a caller typed — so a zero here means "not yet valued", not "worthless".
	"""
	if compat.has_field(STOCK_ENTRY, "total_amount") and doc.get("total_amount") not in (None, ""):
		return round(float(doc.get("total_amount") or 0), 2)
	return round(sum(float(row.get("basic_amount") or 0) for row in items), 2)


def _unvalued(item_code: str) -> bool:
	"""True when the Item has no valuation of its own to fall back on."""
	if not compat.has_field(ITEM, "valuation_rate"):
		return False
	try:
		return float(frappe.db.get_value(ITEM, item_code, "valuation_rate") or 0) <= 0
	except (TypeError, ValueError):
		return True


#: The purposes that post a DIFFERENCE — stock arriving from, or leaving to,
#: nowhere in the stock ledger. A transfer moves value between two warehouses
#: and has nothing to post against.
_DIFFERENCE_PURPOSES = ("Material Receipt", "Material Issue")


def _account_preflight(company: str, entry_type: str, lines: list[dict]) -> None:
	"""Refuse, in a sentence, an entry the company's books cannot take.

	v0.200.0, AFB-2026-00025. A stock receipt on Orchard Meadow came back from
	ERPNext as "Please enter <b>Difference Account</b> or set default <b>Stock
	Adjustment Account</b> for company <strong>Orchard Meadow, LLC</strong>" —
	raised from inside the insert, HTML and all, and read on the phone as a
	problem with the shed that was chosen. It is a problem with the chart: under
	perpetual inventory every movement posts to the ledger, so a receipt needs an
	account for the difference and every warehouse needs an inventory account.
	Asked here, both gaps are named at once, with who fixes them and how, before
	anything is written.

	ONLY UNDER PERPETUAL INVENTORY, which is the only time ERPNext asks. A site
	without the columns this reads is not checked; ERPNext still has the last word.
	"""
	if not compat.doctype_exists("Company") or not compat.has_field("Company", "enable_perpetual_inventory"):
		return
	fields = compat.existing_fields(
		"Company", ("enable_perpetual_inventory", "stock_adjustment_account", "default_inventory_account")
	)
	row = frappe.db.get_value("Company", company, fields, as_dict=True) or {}
	if not compat.checked(row.get("enable_perpetual_inventory")):
		return

	problems = []
	if (
		entry_type in _DIFFERENCE_PURPOSES
		and compat.has_field("Company", "stock_adjustment_account")
		and not row.get("stock_adjustment_account")
		and not any(line.get("expense_account") for line in lines)
	):
		problems.append(
			f"{company} has no Stock Adjustment Account, so a {entry_type.lower()} has no account "
			"to post the difference to. An accountant sets it once, with set_company_defaults "
			"(stock_adjustment_account) or on the Company in the Desk."
		)

	if compat.has_field("Company", "default_inventory_account") and not row.get("default_inventory_account"):
		warehouses = sorted(
			{
				str(line.get(column))
				for line in lines
				for column in ("s_warehouse", "t_warehouse")
				if line.get(column)
			}
		)
		unaccounted = [name for name in warehouses if not _warehouse_account(name)]
		if unaccounted:
			problems.append(
				f"{', '.join(unaccounted)} {'has' if len(unaccounted) == 1 else 'have'} no inventory "
				f"account, and {company} has no Default Inventory Account to fall back on — under "
				"perpetual inventory every warehouse's stock is valued into one. Set it once with "
				"set_company_defaults (default_inventory_account), or on the warehouse in the Desk."
			)

	if problems:
		raise ToolError(" ".join(problems) + " The warehouse chosen is not the problem. Nothing was filed.")


def _warehouse_account(warehouse: str) -> str:
	"""The inventory account a warehouse values into: its own, or the nearest parent's. "" for none."""
	if not compat.has_field(WAREHOUSE, "account"):
		return "unchecked"
	seen: set = set()
	current = warehouse
	while current and current not in seen:
		seen.add(current)
		fields = compat.existing_fields(WAREHOUSE, ("account", "parent_warehouse"))
		row = frappe.db.get_value(WAREHOUSE, current, fields, as_dict=True) or {}
		if row.get("account"):
			return str(row["account"])
		current = str(row.get("parent_warehouse") or "")
	return ""


def create_stock_entry(args: dict) -> ToolResult:
	"""Create a DRAFT Stock Entry. Moves nothing; never submits."""
	compat.require_doctype(STOCK_ENTRY, _HINT)
	entry_type = _entry_type(args)
	company = resolve_company(as_str(args, "company"), required=True)
	posting_date = as_date(args, "posting_date") or frappe.utils.today()
	lines = _entry_lines(args.get("items"), entry_type, company)
	_account_preflight(company, entry_type, lines)
	# v0.203.0. Where a Pest Control product is going, checked before anything
	# is written (docs/design/rodent_bait_program.md §4.1).
	bait = rodent_bait.check_stock_bait_args(args)

	doc = frappe.new_doc(STOCK_ENTRY)
	doc.company = company
	doc.posting_date = posting_date
	# Modern ERPNext keys the movement off `stock_entry_type` (a Link) and
	# derives `purpose` from it; v12 and earlier had only `purpose`. Both are
	# set where the site has them, so neither vintage ends up with a Stock Entry
	# whose purpose is blank.
	if compat.has_field(STOCK_ENTRY, "stock_entry_type"):
		if compat.doctype_exists(STOCK_ENTRY_TYPE) and not frappe.db.exists(STOCK_ENTRY_TYPE, entry_type):
			raise ToolError(
				f"this site has no Stock Entry Type called {entry_type!r}. ERPNext ships one per "
				f"purpose; a site that renamed or deleted it needs the entry created in the Desk. "
				f"Nothing was created."
			)
		doc.stock_entry_type = entry_type
	if compat.has_field(STOCK_ENTRY, "purpose"):
		doc.purpose = entry_type

	source = _apply_source(doc, args)
	remarks = as_str(args, "remarks")
	if source and source["stored_on"].endswith("remarks marker"):
		marker = _source_marker(source["doctype"], source["name"])
		remarks = f"{marker}\n{remarks}" if remarks else marker
	if remarks:
		doc.set("remarks", remarks)

	zero_valued = []
	for line in lines:
		if line.pop("_zero_valued", False):
			zero_valued.append(line["item_code"])
		doc.append("items", line)
	doc.flags.ignore_permissions = True
	doc.insert()

	items = _entry_items_out(doc)
	data = {
		"name": doc.name,
		"entry_type": entry_type,
		"company": company,
		"posting_date": str(posting_date),
		"docstatus": 0,
		"status": "Draft",
		"items": items,
		"item_count": len(items),
		"total_qty": round(sum(row["stock_qty"] for row in items), 6),
		"total_value": _entry_value(doc, items),
		"source": source,
		"remarks": doc.get("remarks"),
		# v0.201.0. Always present: the items received at zero value because no
		# cost was sent and the product has none on file.
		"zero_valued_items": zero_valued,
		"zero_valued_note": (
			f"{', '.join(zero_valued)} {'was' if len(zero_valued) == 1 else 'were'} received at zero "
			"value: no cost was sent and the product has none on file. Send basic_rate to value it."
		)
		if zero_valued
		else None,
		"next_step": (
			"This is a draft: no Stock Ledger Entry was written and no balance moved. Submit it "
			"in ERPNext, or via submit_stock_entry if that tool is enabled."
		),
	}
	# v0.203.0. Rodent bait going to a cabin or a building raises its placement
	# task. Reported per location; never fails the entry.
	bait_tasks = rodent_bait.from_stock_entry(
		[dict(row.as_dict() if hasattr(row, "as_dict") else row) for row in doc.get("items") or []],
		bait,
		company,
		doc.name,
	)
	if bait_tasks is not None:
		data["bait_tasks"] = bait_tasks
	return ToolResult(
		data,
		f"created draft {entry_type} {doc.name} ({company}): {len(items)} line(s), {data['total_qty']} units",
		docstatus_delta="none → 0 (draft)",
	)


def submit_stock_entry(args: dict) -> ToolResult:
	"""Submit a DRAFT Stock Entry (docstatus 0 → 1). This is what moves the stock.

	Takes a name, not a document, and cannot create the entry it submits — for
	the reason `purchasing.submit_purchase_order` gives, with the extra edge that
	what this one commits is not a promise to a supplier but ledger rows every
	later valuation is computed from.
	"""
	compat.require_doctype(STOCK_ENTRY, _HINT)
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(STOCK_ENTRY, name):
		raise ToolError(f"no Stock Entry called {name!r} on this site.")
	doc = frappe.get_doc(STOCK_ENTRY, name)
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 1:
		raise ToolError(f"Stock Entry {name} is already submitted")
	if docstatus == 2:
		raise ToolError(f"Stock Entry {name} is cancelled and cannot be submitted")

	doc.submit()
	doc.reload()
	items = _entry_items_out(doc)
	data = {
		"name": doc.name,
		"status": "Submitted",
		"docstatus": 1,
		"entry_type": doc.get("stock_entry_type") or doc.get("purpose"),
		"company": doc.get("company"),
		"posting_date": str(doc.get("posting_date") or ""),
		"total_qty": round(sum(row["stock_qty"] for row in items), 6),
		"total_value": _entry_value(doc, items),
		"next_step": (
			"ERPNext has written the Stock Ledger Entries and updated every affected Bin. "
			"Read the result with get_stock_balance or get_stock_ledger."
		),
	}
	return ToolResult(
		data,
		f"submitted Stock Entry {doc.name} ({data['entry_type']}, {data['total_qty']} units)",
		docstatus_delta="0 → 1 (submitted)",
	)


def get_stock_entry(args: dict) -> ToolResult:
	"""One Stock Entry in full: its lines, its status and where it came from."""
	compat.require_doctype(STOCK_ENTRY, _HINT)
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(STOCK_ENTRY, name):
		raise ToolError(f"no Stock Entry called {name!r} on this site.")
	doc = frappe.get_doc(STOCK_ENTRY, name)

	fields = compat.existing_fields(
		STOCK_ENTRY,
		[
			"name",
			"company",
			"posting_date",
			"posting_time",
			"stock_entry_type",
			"purpose",
			"docstatus",
			"remarks",
			"total_amount",
			"total_outgoing_value",
			"total_incoming_value",
			"owner",
		],
	)
	data = {field: doc.get(field) for field in fields}
	docstatus = int(doc.get("docstatus") or 0)
	items = _entry_items_out(doc)
	data.update(
		{
			"entry_type": doc.get("stock_entry_type") or doc.get("purpose"),
			"status": {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(docstatus),
			"docstatus_label": {0: "draft", 1: "submitted", 2: "cancelled"}.get(docstatus),
			"items": items,
			"item_count": len(items),
			"total_qty": round(sum(row["stock_qty"] for row in items), 6),
			"total_value": _entry_value(doc, items),
			"source": _read_source(doc),
		}
	)
	return ToolResult(
		data,
		f"Stock Entry {doc.name}: {data['entry_type']}, {len(items)} line(s), {data['status']}",
	)


def _entries_touching(item_code: str, warehouse: str) -> set[str] | None:
	"""Stock Entry docnames whose lines match an item and/or a warehouse.

	`None` means "no line filter was asked for", which is not the same as the
	empty set — an empty set is a filter that legitimately matched nothing, and
	collapsing the two would turn "no entry moved that item" into "every entry".
	"""
	if not item_code and not warehouse:
		return None
	filters: dict = {"parenttype": STOCK_ENTRY}
	if item_code:
		filters["item_code"] = item_code
	rows = frappe.db.get_all(
		STOCK_ENTRY_DETAIL, filters=filters, fields=["parent", "s_warehouse", "t_warehouse"]
	)
	names = set()
	for row in rows:
		if warehouse and warehouse not in (row.get("s_warehouse"), row.get("t_warehouse")):
			continue
		if row.get("parent"):
			names.add(row["parent"])
	return names


def list_stock_entries(args: dict) -> ToolResult:
	"""Stock Entry headers by type, warehouse, item and date range, newest first."""
	compat.require_doctype(STOCK_ENTRY, _HINT)
	company = resolve_company(as_str(args, "company"), required=True)
	entry_type = as_str(args, "entry_type")
	if entry_type:
		entry_type = _entry_type({"entry_type": entry_type})
	warehouse = as_str(args, "warehouse")
	if warehouse:
		warehouse = _resolve_warehouse(warehouse)
	item_code = as_str(args, "item_code")
	if item_code:
		item_code = _resolve_item(item_code)
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	limit = as_limit(args)

	filters: dict = {"company": company}
	if entry_type:
		type_field = "stock_entry_type" if compat.has_field(STOCK_ENTRY, "stock_entry_type") else "purpose"
		filters[type_field] = entry_type
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["posting_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["posting_date"] = (">=", from_date)
	elif to_date:
		filters["posting_date"] = ("<=", to_date)

	names = _entries_touching(item_code, warehouse)
	if names is not None:
		if not names:
			return ToolResult(
				{
					"entries": [],
					"count": 0,
					"company": company,
					"truncated": False,
					"note": (
						"no Stock Entry line matches that item and/or warehouse — the filter is "
						"applied against Stock Entry Detail, so this is an empty result rather "
						"than an unfiltered one."
					),
				},
				"no stock entries match",
			)
		filters["name"] = ("in", sorted(names))

	fields = compat.existing_fields(
		STOCK_ENTRY,
		[
			"name",
			"company",
			"posting_date",
			"stock_entry_type",
			"purpose",
			"docstatus",
			"total_amount",
			"remarks",
			"owner",
		],
	)
	rows = frappe.db.get_all(
		STOCK_ENTRY,
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit=limit + 1,
	)
	truncated = len(rows) > limit
	rows = rows[:limit]

	entries = []
	for row in rows:
		docstatus = int(row.get("docstatus") or 0)
		entries.append(
			{
				**row,
				"entry_type": row.get("stock_entry_type") or row.get("purpose"),
				"status": {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(docstatus),
				"docstatus_label": {0: "draft", 1: "submitted", 2: "cancelled"}.get(docstatus),
			}
		)

	data = {
		"entries": entries,
		"count": len(entries),
		"company": company,
		"entry_type": entry_type or None,
		"warehouse": warehouse or None,
		"item_code": item_code or None,
		"total_value": round(sum(float(row.get("total_amount") or 0) for row in entries), 2),
		"truncated": truncated,
	}
	return ToolResult(data, f"{len(entries)} stock entr{'y' if len(entries) == 1 else 'ies'} for {company}")


# ── balances, ledger and warehouse summary ──────────────────────────────────


def _bin_rows(filters: dict) -> list[dict]:
	fields = compat.existing_fields(
		BIN,
		[
			"name",
			"item_code",
			"warehouse",
			"actual_qty",
			"valuation_rate",
			"stock_value",
			"reserved_qty",
			"ordered_qty",
			"projected_qty",
			"stock_uom",
		],
	)
	return list(frappe.db.get_all(BIN, filters=filters, fields=fields, limit=MAX_LIMIT) or [])


def _stock_value(row) -> float:
	"""A Bin's value, computed where the site does not carry the column."""
	if row.get("stock_value") not in (None, ""):
		return round(float(row.get("stock_value") or 0), 2)
	return round(float(row.get("actual_qty") or 0) * float(row.get("valuation_rate") or 0), 2)


def get_stock_balance(args: dict) -> ToolResult:
	"""On-hand quantity and value for one item, per warehouse."""
	compat.require_doctype(BIN, _HINT)
	item_code = _resolve_item(as_str(args, "item_code", required=True))
	warehouse = as_str(args, "warehouse")
	company = resolve_company(as_str(args, "company"))
	if warehouse:
		warehouse = _resolve_warehouse(warehouse, "warehouse", company or "")

	filters: dict = {"item_code": item_code}
	if warehouse:
		filters["warehouse"] = warehouse
	elif company:
		scoped = _company_warehouses(company)
		if not scoped:
			raise ToolError(f"company {company!r} has no warehouses on this site.")
		filters["warehouse"] = ("in", scoped)

	rows = _bin_rows(filters)
	item = frappe.db.get_value(ITEM, item_code, ["item_name", "stock_uom"], as_dict=True) or {}

	balances = []
	for row in sorted(rows, key=lambda r: str(r.get("warehouse") or "")):
		balances.append(
			{
				"warehouse": row.get("warehouse"),
				"company": _warehouse_company(row.get("warehouse")),
				"qty": float(row.get("actual_qty") or 0),
				"valuation_rate": float(row.get("valuation_rate") or 0),
				"stock_value": _stock_value(row),
				"reserved_qty": float(row.get("reserved_qty") or 0),
				"projected_qty": float(row.get("projected_qty") or 0),
			}
		)

	data = {
		"item_code": item_code,
		"item_name": item.get("item_name"),
		"uom": item.get("stock_uom"),
		"warehouse": warehouse or None,
		"company": company,
		"balances": balances,
		"warehouse_count": len(balances),
		"total_qty": round(sum(row["qty"] for row in balances), 6),
		"total_value": round(sum(row["stock_value"] for row in balances), 2),
	}
	if not balances:
		# See the module docstring: ERPNext creates a Bin the first time an item
		# touches a warehouse, so this is "never moved here", which a caller
		# reading a bare 0 would mistake for "counted, and none left".
		data["note"] = (
			f"{item_code} has no Bin row for the requested scope, which means it has never moved "
			f"in or out of it — not that it was counted and found empty."
		)
	return ToolResult(
		data,
		f"{item_code}: {data['total_qty']} {data['uom'] or ''} across "
		f"{len(balances)} warehouse(s), value {data['total_value']}",
	)


def get_stock_ledger(args: dict) -> ToolResult:
	"""Movement history from Stock Ledger Entry, newest first.

	This is the audit trail, not a balance: each row is one movement with the
	running `balance_qty` ERPNext computed at the time. Cancelled rows are
	excluded where the site marks them, because a cancelled movement did not
	happen and including it would double every total built off this list.
	"""
	compat.require_doctype(STOCK_LEDGER_ENTRY, _HINT)
	item_code = as_str(args, "item_code")
	if item_code:
		item_code = _resolve_item(item_code)
	warehouse = as_str(args, "warehouse")
	if warehouse:
		warehouse = _resolve_warehouse(warehouse)
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	limit = as_limit(args)

	filters: dict = {}
	if item_code:
		filters["item_code"] = item_code
	if warehouse:
		filters["warehouse"] = warehouse
	if from_date and to_date:
		if from_date > to_date:
			raise ToolError(f"from_date {from_date} is after to_date {to_date}")
		filters["posting_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["posting_date"] = (">=", from_date)
	elif to_date:
		filters["posting_date"] = ("<=", to_date)
	if compat.has_field(STOCK_LEDGER_ENTRY, "is_cancelled"):
		filters["is_cancelled"] = 0

	fields = compat.existing_fields(
		STOCK_LEDGER_ENTRY,
		[
			"name",
			"posting_date",
			"posting_time",
			"item_code",
			"warehouse",
			"actual_qty",
			"qty_after_transaction",
			"valuation_rate",
			"stock_value",
			"stock_value_difference",
			"voucher_type",
			"voucher_no",
			"company",
		],
	)
	order = "posting_date desc, creation desc"
	if compat.has_field(STOCK_LEDGER_ENTRY, "posting_time"):
		order = "posting_date desc, posting_time desc, creation desc"
	rows = frappe.db.get_all(
		STOCK_LEDGER_ENTRY, filters=filters, fields=fields, order_by=order, limit=limit + 1
	)
	truncated = len(rows) > limit
	rows = rows[:limit]

	movements = []
	for row in rows:
		movements.append(
			{
				"posting_date": row.get("posting_date"),
				"posting_time": row.get("posting_time"),
				"item_code": row.get("item_code"),
				"warehouse": row.get("warehouse"),
				"qty_change": float(row.get("actual_qty") or 0),
				"balance_qty": float(row.get("qty_after_transaction") or 0),
				"valuation_rate": float(row.get("valuation_rate") or 0),
				"value_change": float(row.get("stock_value_difference") or 0),
				"voucher_type": row.get("voucher_type"),
				"voucher_no": row.get("voucher_no"),
			}
		)

	data = {
		"movements": movements,
		"count": len(movements),
		"item_code": item_code or None,
		"warehouse": warehouse or None,
		"from_date": from_date,
		"to_date": to_date,
		"net_qty_change": round(sum(row["qty_change"] for row in movements), 6),
		"truncated": truncated,
	}
	if truncated:
		data["note"] = (
			"net_qty_change covers the rows returned, not the whole period — narrow the date "
			"range or raise limit before treating it as a period total."
		)
	return ToolResult(data, f"{len(movements)} stock movement(s), net {data['net_qty_change']}")


def _reorder_rules(item_codes=None, warehouse: str = "") -> dict[tuple[str, str], dict]:
	"""Reorder rules keyed by `(item_code, warehouse)`, from wherever they live.

	A pre-v12 site keeps a single warehouseless level on the Item itself; from
	v12 they are `Item Reorder` rows keyed by warehouse. Both are read, and the
	flat one is keyed against `""` so a caller can tell the two apart rather
	than seeing a rule attributed to a warehouse it was never scoped to.
	"""
	rules: dict[tuple[str, str], dict] = {}
	if compat.doctype_exists(ITEM_REORDER) and compat.has_field(ITEM, "reorder_levels"):
		filters: dict = {"parenttype": ITEM}
		if warehouse:
			filters["warehouse"] = warehouse
		if item_codes is not None:
			if not item_codes:
				return rules
			filters["parent"] = ("in", sorted(item_codes))
		rows = frappe.db.get_all(
			ITEM_REORDER,
			filters=filters,
			fields=["parent", "warehouse", "warehouse_reorder_level", "warehouse_reorder_qty"],
			limit=MAX_LIMIT,
		)
		for row in rows:
			key = (str(row.get("parent") or ""), str(row.get("warehouse") or ""))
			rules[key] = {
				"reorder_level": float(row.get("warehouse_reorder_level") or 0),
				"reorder_qty": float(row.get("warehouse_reorder_qty") or 0),
				"stored_on": "Item Reorder row",
			}
		return rules

	if compat.has_field(ITEM, "re_order_level"):
		filters = {}
		if item_codes is not None:
			if not item_codes:
				return rules
			filters["name"] = ("in", sorted(item_codes))
		for row in frappe.db.get_all(
			ITEM, filters=filters, fields=["name", "re_order_level", "re_order_qty"], limit=MAX_LIMIT
		):
			if not float(row.get("re_order_level") or 0):
				continue
			rules[(str(row.get("name")), "")] = {
				"reorder_level": float(row.get("re_order_level") or 0),
				"reorder_qty": float(row.get("re_order_qty") or 0),
				"stored_on": "Item.re_order_level",
			}
	return rules


def get_warehouse_summary(args: dict) -> ToolResult:
	"""Everything on hand in one warehouse, with its reorder rules."""
	compat.require_doctype(BIN, _HINT)
	company = resolve_company(as_str(args, "company"))
	warehouse = _resolve_warehouse(as_str(args, "warehouse", required=True), "warehouse", company or "")
	owner_company = _warehouse_company(warehouse)

	rows = _bin_rows({"warehouse": warehouse})
	item_codes = {str(row.get("item_code")) for row in rows if row.get("item_code")}
	rules = _reorder_rules(item_codes, warehouse)
	names = {}
	if item_codes:
		for item in frappe.db.get_all(
			ITEM,
			filters={"name": ("in", sorted(item_codes))},
			fields=["name", "item_name", "stock_uom"],
			limit=MAX_LIMIT,
		):
			names[item["name"]] = item

	items = []
	for row in sorted(rows, key=lambda r: str(r.get("item_code") or "")):
		item_code = str(row.get("item_code") or "")
		rule = rules.get((item_code, warehouse)) or rules.get((item_code, "")) or {}
		qty = float(row.get("actual_qty") or 0)
		level = float(rule.get("reorder_level") or 0)
		items.append(
			{
				"item_code": item_code,
				"item_name": (names.get(item_code) or {}).get("item_name"),
				"uom": (names.get(item_code) or {}).get("stock_uom") or row.get("stock_uom"),
				"qty": qty,
				"valuation_rate": float(row.get("valuation_rate") or 0),
				"stock_value": _stock_value(row),
				"reorder_level": level or None,
				"reorder_qty": float(rule.get("reorder_qty") or 0) or None,
				"below_reorder": bool(level) and qty < level,
			}
		)

	# `_bin_rows` caps at MAX_LIMIT and this tool takes no limit argument, so a
	# warehouse holding more lines than that says so rather than presenting a
	# partial count as a total. Silent truncation here would understate a
	# valuation, which is the number this summary exists to give.
	truncated = len(rows) >= MAX_LIMIT
	data = {
		"warehouse": warehouse,
		"company": owner_company,
		"items": items,
		"item_count": len(items),
		"total_qty": round(sum(row["qty"] for row in items), 6),
		"total_value": round(sum(row["stock_value"] for row in items), 2),
		"below_reorder_count": sum(1 for row in items if row["below_reorder"]),
		"truncated": truncated,
	}
	if truncated:
		data["note"] = (
			f"this warehouse holds at least {MAX_LIMIT} stocked items and the totals cover only "
			f"the first {MAX_LIMIT}. Read the rest per item with get_stock_balance."
		)
	return ToolResult(
		data,
		f"{warehouse}: {len(items)} item(s), {data['total_qty']} units, value {data['total_value']}, "
		f"{data['below_reorder_count']} below reorder",
	)


# ── reorder rules ────────────────────────────────────────────────────────────


def set_reorder_level(args: dict) -> ToolResult:
	"""Set the reorder level and quantity for one item in one warehouse.

	The write itself goes through `masters._set_reorder`, which is the one place
	in this app that knows where a reorder rule lives on a given ERPNext vintage
	— an `Item Reorder` child row from v12, a flat pair of fields before that.
	Duplicating that decision here would mean two answers to one schema question.
	"""
	compat.require_doctype(ITEM, _HINT)
	item_code = _resolve_item(as_str(args, "item_code", required=True))
	warehouse = _resolve_warehouse(as_str(args, "warehouse", required=True))
	if args.get("reorder_level") in (None, ""):
		raise ToolError("reorder_level is required. Nothing was changed.")
	if args.get("reorder_qty") in (None, ""):
		raise ToolError("reorder_qty is required. Nothing was changed.")
	level = as_float(args.get("reorder_level"), "reorder_level")
	qty = as_float(args.get("reorder_qty"), "reorder_qty")
	if level < 0:
		raise ToolError(f"reorder_level cannot be negative, got {level}. Nothing was changed.")
	if qty <= 0:
		raise ToolError(
			f"reorder_qty must be positive, got {qty} — it is how much to buy when the level is "
			f"hit, and buying nothing is what leaving the rule unset already does. "
			f"Nothing was changed."
		)

	doc = frappe.get_doc(ITEM, item_code)
	written = masters._set_reorder(doc, args, warehouse, level, qty)
	doc.flags.ignore_permissions = True
	doc.save()

	data = {
		"item_code": item_code,
		"warehouse": written.get("warehouse") or warehouse,
		"reorder_level": float(written.get("reorder_level") or 0),
		"reorder_qty": float(written.get("reorder_qty") or 0),
		"created": written.get("created"),
		"stored_on": written.get("stored_on"),
	}
	return ToolResult(
		data,
		f"reorder rule for {item_code} at {data['warehouse']}: level {data['reorder_level']}, "
		f"order {data['reorder_qty']}",
		docstatus_delta="unchanged (Item has no docstatus)",
	)


def list_reorder_alerts(args: dict) -> ToolResult:
	"""Every item currently below its reorder level, worst shortfall first.

	An item with a rule and NO Bin row is reported at zero rather than skipped —
	see the module docstring. That is the opposite of `get_stock_balance`'s
	treatment of the same absence, deliberately: there the question is "what is
	on hand", and "never moved" is the honest answer; here the question is "what
	must be bought", and never having arrived is the strongest possible yes.
	"""
	compat.require_doctype(ITEM, _HINT)
	company = resolve_company(as_str(args, "company"))
	warehouse = as_str(args, "warehouse")
	if warehouse:
		warehouse = _resolve_warehouse(warehouse, "warehouse", company or "")

	rules = _reorder_rules(None, warehouse)
	if company and not warehouse:
		scoped = set(_company_warehouses(company))
		rules = {key: rule for key, rule in rules.items() if not key[1] or key[1] in scoped}

	item_codes = {key[0] for key in rules}
	names = {}
	if item_codes:
		for item in frappe.db.get_all(
			ITEM,
			filters={"name": ("in", sorted(item_codes))},
			fields=["name", "item_name", "stock_uom", "disabled"],
			limit=MAX_LIMIT,
		):
			names[item["name"]] = item

	alerts = []
	for (item_code, rule_warehouse), rule in rules.items():
		item = names.get(item_code) or {}
		if item.get("disabled"):
			# A disabled item is one nobody is allowed to buy or consume. An
			# alert to reorder it is noise at best and a purchase order somebody
			# has to cancel at worst.
			continue
		level = float(rule.get("reorder_level") or 0)
		if level <= 0:
			continue
		bin_filters: dict = {"item_code": item_code}
		if rule_warehouse:
			bin_filters["warehouse"] = rule_warehouse
		elif company:
			scoped = _company_warehouses(company)
			if not scoped:
				continue
			bin_filters["warehouse"] = ("in", scoped)
		current = round(sum(float(row.get("actual_qty") or 0) for row in _bin_rows(bin_filters)), 6)
		if current >= level:
			continue
		alerts.append(
			{
				"item_code": item_code,
				"item_name": item.get("item_name"),
				"uom": item.get("stock_uom"),
				"warehouse": rule_warehouse or None,
				"current_qty": current,
				"reorder_level": level,
				"reorder_qty": float(rule.get("reorder_qty") or 0),
				"shortfall": round(level - current, 6),
				"stored_on": rule.get("stored_on"),
			}
		)

	alerts.sort(key=lambda row: (-row["shortfall"], row["item_code"], row["warehouse"] or ""))
	data = {
		"alerts": alerts[:MAX_LIMIT],
		"count": min(len(alerts), MAX_LIMIT),
		"company": company,
		"warehouse": warehouse or None,
		"rules_checked": len(rules),
		"truncated": len(alerts) > MAX_LIMIT,
	}
	return ToolResult(
		data,
		f"{data['count']} item(s) below reorder level out of {len(rules)} rule(s) checked",
	)
