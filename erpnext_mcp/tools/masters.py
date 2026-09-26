# SPDX-License-Identifier: MIT
"""Master data: the records every other document in ERPNext points at.

v0.66.0. An Item, a Supplier, a Customer, a Warehouse and an Item Group are the
nouns a purchase, a sale, a stock movement and a bill are all written in terms
of. Until this module, this app could read order books and age receivables but
could not name a chemical, a chemical's supplier or the shed it is locked in —
so every workflow that ends in a document ended instead at "somebody open the
Desk and create the master first".

THE WORD "COMPANY" MEANS THREE DIFFERENT THINGS HERE, AND EACH TOOL SAYS WHICH.
This is the one thing to understand before reading anything below, because a
`company` argument that quietly means something other than the caller thinks is
how a list comes back looking authoritative and short:

  * **Warehouse really is company-scoped.** `Warehouse.company` is a column, the
    docname carries the company's abbreviation, and filtering on it is exact.
  * **An Item is not.** ERPNext moved per-company defaults out of Item and into
    the `item_defaults` child table in v12. An Item with no default row at all
    is usable by every company on the site, so filtering Items by company does
    not mean "the items this company has" — it means "the items this company has
    set a DEFAULT for", and it hides the rest. `list_items` applies it and says
    so in `company_scope` rather than letting the shorter list speak for itself.
  * **A Supplier and a Customer are neither.** They are site-wide: stock ERPNext
    puts no company column on either. The argument is accepted (a model will
    send it, and refusing the call over it would be obstructive), validated, and
    reported back as not applied — never silently dropped.

NOTHING HERE IS SUBMITTABLE, SO NOTHING HERE IS A DRAFT. None of these doctypes
has a docstatus workflow: an Item is live the moment it is inserted, and there
is no "submit it later" step to hold it back. The nearest thing to a draft is
`disabled`, which is a real field on Item, Supplier, Customer and Warehouse and
which `create_item` will set on request. This is said plainly because "creates as
draft" is what a reader expects from a create tool in this app — every other one
that says it means a docstatus 0 document — and here there is no such state to
promise.

REORDER LEVELS NEED A WAREHOUSE, AND THAT IS ERPNEXT'S RULE RATHER THAN THIS
APP'S. A reorder level lives on an `Item Reorder` row whose primary key includes
the warehouse it applies to; "reorder at 50" with no shed named is not a thing
the doctype can store. So `update_item` takes the level, the quantity and the
warehouse together, falls back to the item's own default warehouse when one is
set, and refuses with that sentence when neither is available — rather than
writing a row against whichever warehouse sorted first.

PRICES ARE NOT A FIELD ON AN ITEM. They are `Item Price` rows: one per item per
price list per UOM, optionally narrowed to one customer or supplier and to a date
window. `set_item_price` therefore has to decide whether it is creating or
updating, and it makes that decision on the whole key rather than on the item and
the list alone — and when the key it was given matches more than one existing
row, it refuses and lists them instead of picking the first.
"""

from __future__ import annotations

import json

import frappe

from .. import compat
from ..args import (
	as_bool,
	as_choice,
	as_date,
	as_float,
	as_limit,
	as_str,
	resolve_company,
)
from ..errors import ToolError
from ..result import ToolResult

ITEM = "Item"
ITEM_GROUP = "Item Group"
ITEM_DEFAULT = "Item Default"
ITEM_PRICE = "Item Price"
PRICE_LIST = "Price List"
SUPPLIER = "Supplier"
CUSTOMER = "Customer"
WAREHOUSE = "Warehouse"
CUSTOM_FIELD = "Custom Field"

#: v0.97.0. The channel a customer buys through, as a Custom Field on ERPNext's
#: own Customer.
#:
#: WHY ON THE CUSTOMER RATHER THAN ON THE INVOICE. The channel is a property of
#: WHO IS BUYING, not of a particular sale: a farm stand is always direct and a
#: packer is never it, and asking the question once per customer beats asking it
#: on every line of every invoice — which is how it would go unanswered.
#:
#: WHAT IT IS FOR. "What percentage of each commodity do you direct-market" is a
#: question no ERPNext install can answer, because nothing classifies a sale.
#: With this column the answer is direct revenue over total revenue per crop,
#: read off Sales Invoice — a computed figure rather than an asserted one.
#: `Crop.pct_direct_marketed` is the fallback for the year the invoice data is
#: not clean, and the two are deliberately not the same number.
#:
#: A CUSTOM FIELD RATHER THAN A DOCTYPE OF OUR OWN, for the reason
#: `anchors.ensure_pairing_fields` gives: a "Customer Sales Channel" register
#: with one row per customer is a shadow of a property the record can hold, and
#: shadows drift.
SALES_CHANNEL_FIELD = "sales_channel"
SALES_CHANNELS = ("Direct", "Wholesale", "Packer", "Processor")

#: What `require_doctype` says when one of these is missing. They ship with
#: ERPNext's Stock, Buying and Selling modules, so the actionable half of the
#: sentence is "install ERPNext", not "run bench migrate".
_HINT = "It ships with ERPNext's Stock, Buying and Selling modules."

#: The root of ERPNext's Item Group tree on a stock install, and the parent
#: `create_item_group` uses when the caller names none.
ALL_ITEM_GROUPS = "All Item Groups"

#: Where `create_item` files a product that arrives with an EPA registration
#: number and no group. The caller should not have to know the name: a number
#: off a pesticide label already says what the product is. The group is made,
#: as a leaf under the root, the first time a site needs it. The name also
#: matches `compliance_fields.CHEMICAL_ITEM_DEPENDS_ON`, so the Desk shows the
#: label fields on everything filed here.
CROP_PROTECTION_GROUP = "Crop Protection Products"

#: The pesticide label columns `compliance_fields._ITEM_FIELDS` installs on
#: Item, as `create_item` / `update_item` accept them: argument name → kind.
#: The argument IS the fieldname, so nothing here renames anything.
PESTICIDE_FIELDS = {
	"epa_registration_number": "text",
	"signal_word": "signal_word",
	"restricted_use": "check",
	"active_ingredients": "ingredients",
	"rei_hours": "whole",
	"phi_days": "whole",
	"phi_crop": "text",
	"application_rate": "text",
	"ppe_requirements": "text",
	"label_scan_validation": "validation",
}
SIGNAL_WORDS = ("Danger", "Warning", "Caution", "None")
_INGREDIENT_KEYS = ("name", "concentration", "unit")

#: The same for the two party trees and the territory tree. Each is only ever a
#: *fallback*: the tool checks the site actually has it before using it, and
#: names what the site does have when it does not.
ALL_SUPPLIER_GROUPS = "All Supplier Groups"
ALL_CUSTOMER_GROUPS = "All Customer Groups"
ALL_TERRITORIES = "All Territories"

_ITEM_LIST_FIELDS = (
	"name",
	"item_code",
	"item_name",
	"item_group",
	"stock_uom",
	"is_stock_item",
	"disabled",
)

#: Everything `get_item` adds on top of the list fields. Filtered through
#: `compat.existing_fields`, so a site missing any of them loses the key rather
#: than the call.
_ITEM_DETAIL_FIELDS = (
	"description",
	"brand",
	"is_fixed_asset",
	"asset_category",
	"is_purchase_item",
	"is_sales_item",
	"has_batch_no",
	"has_serial_no",
	"valuation_rate",
	"standard_rate",
	"weight_per_unit",
	"weight_uom",
	"shelf_life_in_days",
	"modified",
)

_SUPPLIER_FIELDS = (
	"name",
	"supplier_name",
	"supplier_group",
	"supplier_type",
	"country",
	"disabled",
	"tax_id",
	"tax_category",
	"tax_withholding_category",
	"is_transporter",
	"default_currency",
	"payment_terms",
	"modified",
)

_CUSTOMER_FIELDS = (
	"name",
	"customer_name",
	"customer_group",
	"customer_type",
	"territory",
	SALES_CHANNEL_FIELD,
	"disabled",
	"tax_id",
	"tax_category",
	"default_currency",
	"default_price_list",
	"payment_terms",
	"credit_limit",
	"modified",
)

_WAREHOUSE_FIELDS = (
	"name",
	"warehouse_name",
	"company",
	"parent_warehouse",
	"is_group",
	"disabled",
	"warehouse_type",
	"account",
	"city",
	"modified",
)

_PRICE_LIST_FIELDS = ("name", "currency", "enabled", "buying", "selling", "price_not_uom_dependent")

_ITEM_PRICE_FIELDS = (
	"name",
	"item_code",
	"item_name",
	"price_list",
	"price_list_rate",
	"currency",
	"uom",
	"valid_from",
	"valid_upto",
	"customer",
	"supplier",
	"batch_no",
	"buying",
	"selling",
	"modified",
)


# ── shared helpers ──────────────────────────────────────────────────────────


def _require(doctype: str) -> None:
	compat.require_doctype(doctype, _HINT)


def _rows(doctype: str, filters: dict, candidates, order_by: str, limit: int) -> list[dict]:
	"""`get_all` against whichever of `candidates` this site actually has."""
	return frappe.db.get_all(
		doctype,
		filters=filters,
		fields=compat.existing_fields(doctype, list(candidates)),
		order_by=order_by,
		limit=limit,
	)


def _flag(args: dict, key: str, filters: dict, doctype: str, field: str = "") -> bool | None:
	"""Apply a Check-field filter, but only where the site has the field.

	Returns what the caller asked for so the tool can echo it back. Selecting on
	a column a site does not have is a hard SQL error, and a site that has
	customised one of these doctypes down to its bones is still a site this app
	promises not to break.
	"""
	value = as_bool(args, key)
	if value is None:
		return None
	field = field or key
	if compat.has_field(doctype, field):
		filters[field] = 1 if value else 0
	return value


def _like(args: dict, key: str = "search") -> str:
	"""A substring search term, or `""`. Bare — the caller wraps it in `%`."""
	return as_str(args, key)


def _exists(doctype: str, name: str) -> bool:
	return bool(frappe.db.exists(doctype, name))


def _company_abbr(company: str) -> str:
	return frappe.db.get_value("Company", company, "abbr") or ""


def _checked(value) -> bool:
	"""A Check field as a real boolean. See `compat.checked` for why not `bool`."""
	return compat.checked(value)


def _clean(row: dict) -> dict:
	"""One row as JSON: Checks as booleans, dates and timestamps as strings."""
	out = {}
	for key, value in dict(row).items():
		if key in _CHECK_FIELDS:
			out[key] = _checked(value)
		elif value is None:
			out[key] = None
		elif key in _DATE_FIELDS:
			out[key] = str(value)
		else:
			out[key] = value
	return out


#: Every Check field any tool in this module returns. Kept as one set rather
#: than per-doctype lists because the names do not collide and a field that
#: reads back as the string "0" is truthy in Python wherever it appears.
_CHECK_FIELDS = frozenset(
	{
		"disabled",
		"enabled",
		"is_stock_item",
		"is_fixed_asset",
		"is_group",
		"is_purchase_item",
		"is_sales_item",
		"has_batch_no",
		"has_serial_no",
		"is_transporter",
		"buying",
		"selling",
		"price_not_uom_dependent",
	}
)

_DATE_FIELDS = frozenset({"valid_from", "valid_upto", "modified", "creation"})


def _tree_parent(doctype: str, parent_field: str, given: str, fallback: str, label: str) -> str:
	"""Resolve the parent node of a tree master, or refuse with the real options.

	`fallback` is ERPNext's stock root — used only when the caller named none AND
	the site actually has it. A site whose tree was renamed on import gets a
	refusal listing its own group nodes rather than a link error from three
	layers down naming a record that never existed here.
	"""
	parent = (given or "").strip()
	if not parent:
		if _exists(doctype, fallback):
			return fallback
		groups = frappe.db.get_all(doctype, filters={"is_group": 1}, pluck="name", limit=25)
		raise ToolError(
			f"this site has no {doctype} called {fallback!r}, so {label} cannot be defaulted. "
			f"Pass {parent_field} explicitly. Group nodes on this site: "
			f"{', '.join(sorted(groups)) or '<none>'}. Nothing was created."
		)
	if not _exists(doctype, parent):
		groups = frappe.db.get_all(doctype, filters={"is_group": 1}, pluck="name", limit=25)
		raise ToolError(
			f"no {doctype} called {parent!r} on this site. Group nodes: "
			f"{', '.join(sorted(groups)) or '<none>'}. Nothing was created."
		)
	if compat.has_field(doctype, "is_group") and not _checked(
		frappe.db.get_value(doctype, parent, "is_group")
	):
		raise ToolError(
			f"{doctype} {parent!r} is a leaf, not a group, so nothing can be filed under it. "
			f"Pass a group node as {parent_field}. Nothing was created."
		)
	return parent


def _group_or_refuse(
	doctype: str, given: str, fallback: str, label: str, *, prefer_leaf: bool = False
) -> str:
	"""A Supplier/Customer Group or Territory: the one given, or a site default.

	Unlike `_tree_parent` this accepts a leaf — a party belongs to a leaf group,
	not to a branch — and it is a required link on both party doctypes, which is
	why a site with no usable default gets the list rather than an insert that
	fails ERPNext's own mandatory pass.

	`prefer_leaf` IS THE v0.67.0 FIX, and it is worth the paragraph. This
	defaulted a Customer to `All Customer Groups` and a Supplier to `All Supplier
	Groups`, both of which are the ROOT of their tree and therefore group nodes.
	ERPNext puts a link filter of `is_group = 0` on both fields, so every
	`create_customer` call that did not name a group was refused by the framework
	— on a stock site, that is every call the tool was designed to make easy.

	The failure was invisible to this app's own suite because a group node is a
	perfectly valid docname and the standalone double does not reproduce
	ERPNext's link filters. It was visible immediately on a bench.

	So where `prefer_leaf` is set and the stock root is a group, the default
	becomes the site's FIRST NON-GROUP node in alphabetical order — `Commercial`
	on a stock ERPNext install. Alphabetical rather than "first created" because
	a default that depends on insertion order is a default that differs between
	two sites nobody can tell apart.

	It is NOT set for Territory: a Territory group is accepted on a Customer, and
	stock ERPNext's own Selling Settings default IS `All Territories`. Changing
	that would be fixing a bug nobody has.
	"""
	value = (given or "").strip()
	if value:
		if not _exists(doctype, value):
			options = frappe.db.get_all(doctype, pluck="name", limit=25)
			raise ToolError(
				f"no {doctype} called {value!r} on this site. Known: "
				f"{', '.join(sorted(options)) or '<none>'}. Nothing was created."
			)
		return value

	if prefer_leaf:
		leaf = _first_leaf(doctype)
		if leaf:
			return leaf
	if _exists(doctype, fallback) and not (prefer_leaf and _is_group(doctype, fallback)):
		return fallback

	options = frappe.db.get_all(doctype, pluck="name", limit=25)
	if len(options) == 1:
		return options[0]
	if prefer_leaf:
		raise ToolError(
			f"{label} is required and this site has no non-group {doctype} to default to — "
			f"ERPNext refuses a party filed under a group node. Create one, or pass {label} "
			f"explicitly. Known: {', '.join(sorted(options)) or '<none>'}. Nothing was created."
		)
	raise ToolError(
		f"{label} is required and this site has no {doctype} called {fallback!r} to default to. "
		f"Known: {', '.join(sorted(options)) or '<none>'}. Nothing was created."
	)


def _is_group(doctype: str, name: str) -> bool:
	"""Is this tree node a branch? A doctype with no `is_group` has no branches."""
	if not compat.has_field(doctype, "is_group"):
		return False
	return _checked(frappe.db.get_value(doctype, name, "is_group"))


def _first_leaf(doctype: str) -> str:
	"""The alphabetically first non-group node, or `""` where there is none."""
	if not compat.has_field(doctype, "is_group"):
		return ""
	rows = frappe.db.get_all(doctype, filters={"is_group": 0}, pluck="name", limit=500)
	return sorted(str(row) for row in rows or [])[0] if rows else ""


def _child_rows(doc, fieldname: str) -> list:
	"""The child rows of `doc`, whether they are dicts or documents."""
	return list(doc.get(fieldname) or [])


def _row_get(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


# ── Item ────────────────────────────────────────────────────────────────────


def _items_with_defaults_for(company: str) -> list[str]:
	"""Item docnames carrying a per-company default row for `company`.

	The list can legitimately be empty on a site that has never set an item
	default, which is why the caller turns an empty result into a filter that
	matches nothing rather than into a filter it skips: "no items are scoped to
	this company" and "every item is" are different answers.
	"""
	if not compat.doctype_exists(ITEM_DEFAULT):
		return []
	names = frappe.db.get_all(
		ITEM_DEFAULT,
		filters={"company": company, "parenttype": ITEM},
		pluck="parent",
	)
	return sorted({name for name in names if name})


def list_items(args: dict) -> ToolResult:
	"""Items by group, stock flag, disabled flag and company."""
	_require(ITEM)
	filters: dict = {}

	item_group = as_str(args, "item_group")
	if item_group:
		if not _exists(ITEM_GROUP, item_group):
			raise ToolError(
				f"no Item Group called {item_group!r} on this site. Call list_item_groups to see the tree."
			)
		filters["item_group"] = item_group

	is_stock_item = _flag(args, "is_stock_item", filters, ITEM)
	disabled = _flag(args, "disabled", filters, ITEM)

	search = _like(args)
	if search:
		filters["item_name"] = ("like", f"%{search}%")

	company = resolve_company(as_str(args, "company")) if as_str(args, "company") else None
	company_scope = None
	if company:
		scoped = _items_with_defaults_for(company)
		# An empty list has to become a filter that matches nothing. `("in", [])`
		# is not portable across Frappe versions, so the sentinel is a docname no
		# Item can have.
		filters["name"] = ("in", scoped or ["\x00"])
		company_scope = (
			f"company was applied as 'has an Item Default row for {company}'. An ERPNext Item "
			"is NOT company-scoped — an item with no default row at all is usable by every "
			"company on this site, and is not in this list. Omit company to see all items."
		)

	limit = as_limit(args)
	rows = _rows(ITEM, filters, _ITEM_LIST_FIELDS, "item_name asc", limit)
	items = [_clean(row) for row in rows]

	by_group: dict = {}
	for item in items:
		key = item.get("item_group") or "<none>"
		by_group[key] = by_group.get(key, 0) + 1

	data = {
		"items": items,
		"count": len(items),
		"limit": limit,
		"truncated": len(items) == limit,
		"by_item_group": by_group,
		"filters": {
			"item_group": item_group or None,
			"is_stock_item": is_stock_item,
			"disabled": disabled,
			"company": company,
			"search": search or None,
		},
	}
	if company_scope:
		data["company_scope"] = company_scope
	return ToolResult(data, f"{len(items)} item(s)")


def get_item(args: dict) -> ToolResult:
	"""One Item in full, including its per-company defaults and reorder rules."""
	_require(ITEM)
	code = _require_item(args)
	doc = frappe.get_doc(ITEM, code)

	fields = compat.existing_fields(ITEM, list(_ITEM_LIST_FIELDS) + list(_ITEM_DETAIL_FIELDS))
	data = _clean({field: doc.get(field) for field in fields})

	defaults = []
	for row in _child_rows(doc, "item_defaults"):
		defaults.append(
			{
				"company": _row_get(row, "company"),
				"default_warehouse": _row_get(row, "default_warehouse"),
				"default_price_list": _row_get(row, "default_price_list"),
				"buying_cost_center": _row_get(row, "buying_cost_center"),
				"selling_cost_center": _row_get(row, "selling_cost_center"),
				"expense_account": _row_get(row, "expense_account"),
				"income_account": _row_get(row, "income_account"),
			}
		)
	data["item_defaults"] = defaults

	reorder = []
	for row in _child_rows(doc, "reorder_levels"):
		reorder.append(
			{
				"warehouse": _row_get(row, "warehouse"),
				"warehouse_group": _row_get(row, "warehouse_group"),
				"reorder_level": float(_row_get(row, "warehouse_reorder_level") or 0),
				"reorder_qty": float(_row_get(row, "warehouse_reorder_qty") or 0),
				"material_request_type": _row_get(row, "material_request_type"),
			}
		)
	data["reorder_levels"] = reorder

	# The pre-v12 flat fields, reported only where a site still has them, so a
	# reader can tell "this site keeps it on the Item" from "nobody set one".
	flat_warehouse = compat.first_field(ITEM, "default_warehouse")
	if flat_warehouse:
		data["default_warehouse"] = doc.get(flat_warehouse)
	else:
		data["default_warehouse"] = defaults[0]["default_warehouse"] if len(defaults) == 1 else None
		data["default_warehouse_note"] = (
			"this site keeps default warehouses in the item_defaults child table, one per "
			"company. The top-level key is filled only when there is exactly one row; read "
			"item_defaults for the rest."
		)
	for legacy in ("re_order_level", "re_order_qty"):
		if compat.has_field(ITEM, legacy):
			data[legacy] = doc.get(legacy)

	return ToolResult(
		data,
		f"Item {doc.name}: {doc.get('item_name')} ({doc.get('item_group')}, {doc.get('stock_uom')})",
	)


def _require_item(args: dict) -> str:
	"""The Item docname this call is about, from any of the names a model uses."""
	code = as_str(args, "item_code") or as_str(args, "name") or as_str(args, "item")
	if not code:
		raise ToolError("item_code is required.")
	if _exists(ITEM, code):
		return code
	match = frappe.db.get_value(ITEM, {"item_name": code}, "name")
	if match:
		return str(match)
	raise ToolError(f"no Item called {code!r} on this site. Call list_items to find it.")


#: ERPNext's own child table of retail codes on an Item. v0.181.0. A UPC read
#: off a tub of mouse bait at a farm store goes HERE, on the Item that carries
#: the EPA registration and the REI — not on a register of its own. The
#: barcode is how somebody holding the tub finds the record; the registration
#: number is what the record IS.
ITEM_BARCODE = "Item Barcode"

#: The GTIN lengths the check digit is verified on, and the `barcode_type` each
#: is stored as. ERPNext's Select spells the 12-digit US code "UPC-A" and the
#: 13-digit one plain "EAN"; anything else (a Code 128 on a distributor's
#: case) is stored with no type rather than a guessed one.
_GTIN_TYPES = {8: "EAN-8", 12: "UPC-A", 13: "EAN"}


def normalize_barcode(raw) -> tuple[str, str]:
	"""A scanned or typed retail code as `(code, barcode_type)`, check digit verified.

	A UPC-A REACHES A PHONE AS THIRTEEN DIGITS. iOS reports a 12-digit UPC-A
	as the EAN-13 it is a subset of, with a leading zero, and a person typing
	the numbers under the bars types twelve. Stored as either spelling, one
	lookup would miss the other, so a leading-zero EAN-13 is folded to its
	12-digit UPC-A here and `_barcode_spellings` still matches both.

	A GTIN WHOSE CHECK DIGIT FAILS IS REFUSED, not stored. A camera validates
	the digit before it reports a code, so a failure means a typed code with a
	slipped digit — and a wrong code on the Item is a tub that scans to nothing,
	or to somebody else's product.
	"""
	text = str(raw or "").strip()
	compact = "".join(text.split())
	if not compact:
		raise ToolError("barcode is required — the digits under the bars, or a scan of them.")
	if not compact.isdigit():
		return compact, ""
	if len(compact) == 13 and compact.startswith("0"):
		compact = compact[1:]
	kind = _GTIN_TYPES.get(len(compact), "")
	if kind and not _gtin_check_ok(compact):
		raise ToolError(
			f"{compact} is not a valid {kind} — its last digit is the check digit and it does not "
			"match the rest. Check the digits under the bars, or scan it. Nothing was changed."
		)
	return compact, kind


def _gtin_check_ok(digits: str) -> bool:
	"""GS1 mod-10: from the right, excluding the check digit, weights 3,1,3,1…"""
	body, check = digits[:-1], int(digits[-1])
	total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
	return (10 - total % 10) % 10 == check


def _barcode_spellings(code: str) -> list[str]:
	"""Every spelling of one code a site may already hold — see `normalize_barcode`."""
	spellings = [code]
	if code.isdigit() and len(code) == 12:
		spellings.append("0" + code)
	return spellings


def _barcode_owner(code: str) -> str | None:
	"""The Item this code is already on, or None."""
	rows = frappe.db.get_all(
		ITEM_BARCODE,
		filters={"barcode": ("in", _barcode_spellings(code)), "parenttype": ITEM},
		fields=["parent"],
		limit_page_length=1,
	)
	return str(rows[0]["parent"]) if rows else None


def _item_card(code: str) -> dict:
	"""What a phone needs to say which product this is, and what its label closes."""
	fields = compat.existing_fields(ITEM, list(_ITEM_LIST_FIELDS) + list(PESTICIDE_FIELDS))
	row = frappe.db.get_value(ITEM, code, fields, as_dict=True) or {}
	card = _clean(dict(row))
	card["barcodes"] = [
		{"barcode": _row_get(b, "barcode"), "barcode_type": _row_get(b, "barcode_type") or None}
		for b in _child_rows(frappe.get_doc(ITEM, code), "barcodes")
	]
	return card


def find_item_by_barcode(args: dict) -> ToolResult:
	"""The Item a retail barcode is on. Read-only. `found: false` is an answer, not an error.

	NOT FOUND IS THE ORDINARY CASE THE FIRST TIME A PRODUCT IS BOUGHT, and the
	caller's next step — link the code to an Item, or create one — depends on
	knowing that rather than on catching a refusal.
	"""
	_require(ITEM)
	_require(ITEM_BARCODE)
	code, kind = normalize_barcode(args.get("barcode"))
	owner = _barcode_owner(code)
	data = {"barcode": code, "barcode_type": kind or None, "found": bool(owner)}
	if owner:
		data["item"] = _item_card(owner)
	summary = f"{code} is on Item {owner}" if owner else f"no Item carries {code}"
	return ToolResult(data, summary)


def add_item_barcode(args: dict) -> ToolResult:
	"""Put a retail barcode on an existing Item. Idempotent for the Item that already has it.

	A CODE ALREADY ON ANOTHER ITEM IS REFUSED BY NAME. ERPNext keeps barcodes
	unique site-wide, and moving one silently would make yesterday's scan of the
	other product answer with this one.
	"""
	_require(ITEM)
	_require(ITEM_BARCODE)
	item_code = _require_item(args)
	code, kind = normalize_barcode(args.get("barcode"))
	owner = _barcode_owner(code)
	if owner and owner != item_code:
		raise ToolError(
			f"{code} is already on Item {owner}. A barcode can only name one product — remove it "
			f"from {owner} in the Desk first if it was put there by mistake. Nothing was changed."
		)
	if not owner:
		doc = frappe.get_doc(ITEM, item_code)
		doc.append("barcodes", {"barcode": code, "barcode_type": kind})
		doc.save()
	data = {
		"item_code": item_code,
		"barcode": code,
		"barcode_type": kind or None,
		"already_linked": bool(owner),
		"item": _item_card(item_code),
	}
	verb = "was already on" if owner else "added to"
	return ToolResult(data, f"barcode {code} {verb} Item {item_code}")


def create_item(args: dict) -> ToolResult:
	"""Create one Item.

	There is no draft to create it as — see the module docstring. `disabled` is
	the only thing resembling one, and it is a real field the caller may set.
	"""
	_require(ITEM)
	item_code = as_str(args, "item_code", required=True)
	item_name = as_str(args, "item_name") or item_code
	label = _pesticide_values(args, "created")
	stock_uom = _resolve_uom(as_str(args, "stock_uom") or "Nos")
	is_stock_item = as_bool(args, "is_stock_item", True)
	disabled = bool(as_bool(args, "disabled", False))
	description = as_str(args, "description")

	if _exists(ITEM, item_code):
		raise ToolError(
			f"an Item called {item_code!r} already exists on this site. Item codes are the "
			"docname, so they are unique. Use update_item to change it. Nothing was created."
		)

	# v0.181.0. The retail code a product was scanned by, checked BEFORE anything
	# is written for the same reason the group below is resolved late: a refused
	# call leaves nothing behind.
	barcode: tuple[str, str] | None = None
	if args.get("barcode") not in (None, ""):
		_require(ITEM_BARCODE)
		barcode = normalize_barcode(args.get("barcode"))
		owner = _barcode_owner(barcode[0])
		if owner:
			raise ToolError(
				f"barcode {barcode[0]} is already on Item {owner}. That is probably the product "
				f"you are holding — use it, or remove the code from {owner} first. Nothing was created."
			)

	# NOT `_tree_parent`: an Item belongs to a LEAF group, so requiring is_group
	# here would refuse exactly the groups items are supposed to go in. Resolved
	# after every refusal above, because the crop-protection group is created on
	# first use and a refused call should leave nothing behind.
	group_created = False
	if not as_str(args, "item_group") and label.get("epa_registration_number"):
		item_group, group_created = _crop_protection_group()
	else:
		item_group = _item_group_for(as_str(args, "item_group"))

	doc = frappe.new_doc(ITEM)
	doc.item_code = item_code
	doc.item_name = item_name
	doc.item_group = item_group
	doc.stock_uom = stock_uom
	doc.is_stock_item = 1 if is_stock_item else 0
	if description:
		doc.description = description
	if disabled and compat.has_field(ITEM, "disabled"):
		doc.disabled = 1
	for key, value in label.items():
		doc.set(key, value)
	if barcode:
		doc.append("barcodes", {"barcode": barcode[0], "barcode_type": barcode[1]})

	warehouse = as_str(args, "default_warehouse")
	default_note = ""
	if warehouse:
		default_note = _set_default_warehouse(doc, warehouse, as_str(args, "company"))

	doc.insert()

	data = {
		"name": doc.name,
		"item_code": doc.get("item_code"),
		"item_name": item_name,
		"item_group": item_group,
		"stock_uom": stock_uom,
		"is_stock_item": bool(is_stock_item),
		"disabled": disabled,
		"description": description or None,
		"default_warehouse": warehouse or None,
		"submittable": False,
		"note": (
			"An Item has no docstatus and therefore no draft state: this one is live. "
			"Pass disabled to keep it out of transactions."
		),
	}
	if default_note:
		data["default_warehouse_stored_on"] = default_note
	if barcode:
		data["barcode"] = barcode[0]
		data["barcode_type"] = barcode[1] or None
	if label:
		data["pesticide_label"] = {
			key: (json.loads(value) if key == "active_ingredients" and value else value)
			for key, value in label.items()
		}
	if group_created:
		data["item_group_created"] = True
		data["item_group_note"] = (
			f"An EPA registration number with no item_group files the product under "
			f"{CROP_PROTECTION_GROUP!r}. This site had no such group, so it was created "
			f"under {ALL_ITEM_GROUPS!r}."
		)
	return ToolResult(
		data,
		f"created Item {doc.name} ({item_name}) in {item_group}, stocked in {stock_uom}",
		docstatus_delta="none → created",
	)


def _item_group_for(given: str) -> str:
	"""The Item Group a new Item lands in: the one named, or the site's root."""
	group = (given or "").strip()
	if group:
		if not _exists(ITEM_GROUP, group):
			known = frappe.db.get_all(ITEM_GROUP, pluck="name", limit=25)
			raise ToolError(
				f"no Item Group called {group!r} on this site. Known: "
				f"{', '.join(sorted(known)) or '<none>'}. Create it with create_item_group, "
				"or pick one of these. Nothing was created."
			)
		return group
	if _exists(ITEM_GROUP, ALL_ITEM_GROUPS):
		return ALL_ITEM_GROUPS
	leaves = frappe.db.get_all(ITEM_GROUP, filters={"is_group": 0}, pluck="name", limit=25)
	if len(leaves) == 1:
		return leaves[0]
	raise ToolError(
		f"item_group is required: this site has no Item Group called {ALL_ITEM_GROUPS!r} to "
		f"default to. Call list_item_groups to see the tree. Nothing was created."
	)


def _crop_protection_group() -> tuple[str, bool]:
	"""`(group, created)` for an Item that arrived with an EPA number and no group."""
	if _exists(ITEM_GROUP, CROP_PROTECTION_GROUP):
		return CROP_PROTECTION_GROUP, False
	if not _exists(ITEM_GROUP, ALL_ITEM_GROUPS):
		raise ToolError(
			f"an EPA registration number files a product under {CROP_PROTECTION_GROUP!r}, and this "
			f"site has neither that group nor {ALL_ITEM_GROUPS!r} to make it under. Pass item_group, "
			"or create the group with create_item_group. Nothing was created."
		)
	group = frappe.new_doc(ITEM_GROUP)
	group.item_group_name = CROP_PROTECTION_GROUP
	group.parent_item_group = ALL_ITEM_GROUPS
	group.is_group = 0
	group.insert()
	return group.name, True


def _pesticide_values(args: dict, verb: str) -> dict:
	"""The label fields this call sets, validated, as `{fieldname: stored value}`.

	Only keys the caller actually sent are returned, so an update touches nothing
	it was not asked to. An empty string clears a text, Select, JSON or Link field.
	Nothing is written to a column the site does not have: that is refused and
	names the installer, because a label value that silently goes nowhere is
	exactly what an applicator must never be told was recorded.
	"""
	values: dict = {}
	for key, kind in PESTICIDE_FIELDS.items():
		raw = args.get(key)
		if raw is None:
			continue
		if kind == "text":
			values[key] = str(raw).strip()
		elif kind == "check":
			values[key] = 1 if as_bool(args, key) else 0
		elif kind == "whole":
			values[key] = _whole_non_negative(raw, key, verb)
		elif kind == "signal_word":
			values[key] = _signal_word(raw, verb)
		elif kind == "ingredients":
			values[key] = _ingredients(raw, verb)
		elif kind == "validation":
			values[key] = _label_validation(str(raw).strip(), verb)

	for key in values:
		if not compat.has_field(ITEM, key):
			raise ToolError(
				f"this site's Item has no {key!r} column. The pesticide label fields are added by "
				"install_compliance_fields, which also runs on every bench migrate. "
				f"Nothing was {verb}."
			)
	return values


def _whole_non_negative(raw, key: str, verb: str) -> int:
	"""A label interval as a whole number of at least zero. A fraction is refused, not truncated.

	`int(4.5)` is 4, and a four-hour answer to a 4.5-hour REI puts a crew in the
	block half an hour early. The column is an Int, so the caller rounds up and
	knows it did.
	"""
	if isinstance(raw, bool):
		raise ToolError(f"{key} must be a whole number of at least 0, got {raw!r}. Nothing was {verb}.")
	try:
		number = float(raw)
	except (TypeError, ValueError):
		raise ToolError(
			f"{key} must be a whole number of at least 0, got {raw!r}. Nothing was {verb}."
		) from None
	if number != number or number < 0:
		raise ToolError(f"{key} must be at least 0, got {raw!r}. Nothing was {verb}.")
	if number != int(number):
		raise ToolError(
			f"{key} must be a whole number, got {raw!r}. Round a label interval UP, never down. "
			f"Nothing was {verb}."
		)
	return int(number)


def _signal_word(raw, verb: str) -> str:
	"""One of the four signal words in their stored casing, or '' to clear."""
	word = str(raw).strip()
	if not word:
		return ""
	for option in SIGNAL_WORDS:
		if option.lower() == word.lower():
			return option
	raise ToolError(
		f"signal_word must be one of {', '.join(SIGNAL_WORDS)}, got {word!r}. 'None' is the "
		f"answer for a product whose label carries no signal word. Nothing was {verb}."
	)


def _ingredients(raw, verb: str) -> str | None:
	"""`active_ingredients` as the JSON text the column stores, or None to clear.

	Accepts a list or a JSON string of one. Each entry is an object with a
	non-empty `name` and optional `concentration` (a number) and `unit`; any
	other key is refused rather than stored for nothing to read.
	"""
	if isinstance(raw, str):
		if not raw.strip():
			return None
		try:
			raw = json.loads(raw)
		except ValueError:
			raise ToolError(
				"active_ingredients must be valid JSON: a list of "
				f'{{"name", "concentration", "unit"}} objects. Nothing was {verb}.'
			) from None
	if not isinstance(raw, list):
		raise ToolError(
			"active_ingredients must be a list of "
			f'{{"name", "concentration", "unit"}} objects, got {type(raw).__name__}. Nothing was {verb}.'
		)
	if not raw:
		return None
	clean = []
	for position, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"active_ingredients entry {position} must be an object. Nothing was {verb}.")
		unknown = sorted(set(entry) - set(_INGREDIENT_KEYS))
		if unknown:
			raise ToolError(
				f"active_ingredients entry {position} has keys this record does not keep: "
				f"{', '.join(unknown)}. Use name, concentration and unit. Nothing was {verb}."
			)
		name = str(entry.get("name") or "").strip()
		if not name:
			raise ToolError(f"active_ingredients entry {position} needs a name. Nothing was {verb}.")
		row: dict = {"name": name}
		concentration = entry.get("concentration")
		if concentration not in (None, ""):
			if isinstance(concentration, bool):
				concentration = "not a number"
			try:
				row["concentration"] = float(concentration)
			except (TypeError, ValueError):
				raise ToolError(
					f"active_ingredients entry {position} ({name}): concentration must be a number, "
					f"got {concentration!r}. Nothing was {verb}."
				) from None
			if row["concentration"] < 0:
				raise ToolError(
					f"active_ingredients entry {position} ({name}): concentration cannot be negative. "
					f"Nothing was {verb}."
				)
		unit = str(entry.get("unit") or "").strip()
		if unit:
			row["unit"] = unit
		clean.append(row)
	return json.dumps(clean)


def _label_validation(name: str, verb: str) -> str:
	"""A Document Validation docname, or '' to clear the link."""
	if not name:
		return ""
	if not compat.doctype_exists("Document Validation"):
		raise ToolError(f"this site has no Document Validation DocType. Nothing was {verb}.")
	if not _exists("Document Validation", name):
		raise ToolError(
			f"no Document Validation called {name!r} on this site. list_document_validations has "
			f"them. Nothing was {verb}."
		)
	return name


def _stored_label_value(key: str, value):
	"""What a label column holds, in the shape `_pesticide_values` produces, for comparing."""
	kind = PESTICIDE_FIELDS[key]
	if kind == "check":
		return 1 if _checked(value) else 0
	if kind == "whole":
		return int(value or 0)
	if kind == "ingredients":
		if value in (None, "", []):
			return None
		if isinstance(value, str):
			try:
				value = json.loads(value)
			except ValueError:
				return value
		return json.dumps(value)
	return value or ""


def _resolve_uom(uom: str) -> str:
	"""A UOM docname, or a refusal naming what the site actually stocks.

	ERPNext ships around a hundred UOMs and a farm uses six of them. A model
	sending 'Lbs' where the site calls it 'Lb' should get the list, not a link
	error raised from inside the insert.
	"""
	if not compat.doctype_exists("UOM"):
		return uom
	if _exists("UOM", uom):
		return uom
	match = frappe.db.get_all("UOM", filters={"name": ("like", uom)}, pluck="name", limit=5)
	if len(match) == 1:
		return match[0]
	known = frappe.db.get_all("UOM", filters={"enabled": 1}, pluck="name", limit=40)
	raise ToolError(
		f"no UOM called {uom!r} on this site. Known units include: "
		f"{', '.join(sorted(known)[:40]) or '<none>'}. Nothing was created."
	)


def _set_default_warehouse(doc, warehouse: str, company: str) -> str:
	"""Point an Item at a default Warehouse, on whichever field this site uses.

	Returns where it was stored, because the two places are genuinely different
	records and a caller reading the result back should not have to guess which
	one this site has.
	"""
	if not _exists(WAREHOUSE, warehouse):
		raise ToolError(
			f"no Warehouse called {warehouse!r} on this site. Call list_warehouses to see them. "
			"Nothing was created."
		)
	flat = compat.first_field(ITEM, "default_warehouse")
	if flat:
		doc.set(flat, warehouse)
		return "Item.default_warehouse"

	if not compat.has_field(ITEM, "item_defaults"):  # pragma: no cover - no site has neither
		raise ToolError(
			"this site's Item has neither a default_warehouse field nor an item_defaults "
			"table, so a default warehouse cannot be stored. Nothing was created."
		)
	scope = frappe.db.get_value(WAREHOUSE, warehouse, "company") or ""
	company = resolve_company(company or scope, required=True)
	if scope and scope != company:
		raise ToolError(
			f"Warehouse {warehouse!r} belongs to company {scope!r}, not {company!r}. Nothing was created."
		)
	for row in _child_rows(doc, "item_defaults"):
		if _row_get(row, "company") == company:
			if isinstance(row, dict):
				row["default_warehouse"] = warehouse
			else:
				row.default_warehouse = warehouse
			return f"Item Default row for {company}"
	doc.append("item_defaults", {"company": company, "default_warehouse": warehouse})
	return f"Item Default row for {company}"


def update_item(args: dict) -> ToolResult:
	"""Change one Item's description, group, disabled flag, default warehouse,
	reorder rule or pesticide label fields. Never renames it — the item_code is
	the docname."""
	_require(ITEM)
	code = _require_item(args)
	doc = frappe.get_doc(ITEM, code)

	description = args.get("description")
	item_name = as_str(args, "item_name")
	item_group = as_str(args, "item_group")
	disabled = as_bool(args, "disabled")
	warehouse = as_str(args, "default_warehouse")
	reorder_level = args.get("reorder_level")
	reorder_qty = args.get("reorder_qty")
	reorder_warehouse = as_str(args, "reorder_warehouse")
	label = _pesticide_values(args, "changed")

	touched = (
		description is not None
		or bool(item_name)
		or bool(item_group)
		or disabled is not None
		or bool(warehouse)
		or reorder_level not in (None, "")
		or reorder_qty not in (None, "")
		or bool(label)
	)
	if not touched:
		raise ToolError(
			"nothing to change. Pass at least one of description, item_name, item_group, "
			"disabled, default_warehouse, reorder_level, reorder_qty, or a pesticide label "
			f"field ({', '.join(PESTICIDE_FIELDS)})."
		)

	changes: dict = {}

	if description is not None:
		new = str(description)
		if new != (doc.get("description") or ""):
			changes["description"] = [doc.get("description"), new]
			doc.description = new

	if item_name and item_name != (doc.get("item_name") or ""):
		changes["item_name"] = [doc.get("item_name"), item_name]
		doc.item_name = item_name

	if item_group:
		if not _exists(ITEM_GROUP, item_group):
			raise ToolError(f"no Item Group called {item_group!r} on this site. Nothing was changed.")
		if item_group != (doc.get("item_group") or ""):
			changes["item_group"] = [doc.get("item_group"), item_group]
			doc.item_group = item_group

	if disabled is not None and compat.has_field(ITEM, "disabled"):
		current = _checked(doc.get("disabled"))
		if current != disabled:
			changes["disabled"] = [current, disabled]
			doc.disabled = 1 if disabled else 0

	for key, value in label.items():
		current = _stored_label_value(key, doc.get(key))
		if current != value:
			changes[key] = [current, value]
			doc.set(key, value)

	stored_on = ""
	if warehouse:
		stored_on = _set_default_warehouse(doc, warehouse, as_str(args, "company"))
		changes["default_warehouse"] = [None, warehouse]

	reorder = None
	if reorder_level not in (None, "") or reorder_qty not in (None, ""):
		reorder = _set_reorder(doc, args, reorder_warehouse, reorder_level, reorder_qty)
		changes["reorder"] = [None, reorder]

	if not changes:
		return ToolResult(
			{"name": doc.name, "changed": {}, "note": "every value sent already matched the Item."},
			f"Item {doc.name} already matched; nothing changed",
		)

	doc.save()

	data = {"name": doc.name, "item_code": doc.get("item_code"), "changed": changes}
	if stored_on:
		data["default_warehouse_stored_on"] = stored_on
	if reorder:
		data["reorder"] = reorder
	return ToolResult(
		data,
		f"updated Item {doc.name}: {', '.join(sorted(changes))}",
		docstatus_delta="unchanged (Item has no docstatus)",
	)


def _set_reorder(doc, args: dict, warehouse: str, level, qty) -> dict:
	"""Write a reorder rule onto the Item, against a named warehouse.

	The warehouse is not optional in the data model — see the module docstring —
	so this resolves it from the argument, then from the item's own default, and
	refuses rather than guessing.
	"""
	if not compat.has_field(ITEM, "reorder_levels"):
		# A pre-v12 site keeps them flat, and there they really are warehouseless.
		if compat.has_field(ITEM, "re_order_level"):
			if level not in (None, ""):
				doc.re_order_level = as_float(level, "reorder_level")
			if qty not in (None, ""):
				doc.re_order_qty = as_float(qty, "reorder_qty")
			return {
				"stored_on": "Item.re_order_level / re_order_qty",
				"warehouse": None,
				"reorder_level": doc.get("re_order_level"),
				"reorder_qty": doc.get("re_order_qty"),
			}
		raise ToolError(  # pragma: no cover - no ERPNext version has neither
			"this site's Item has no reorder fields at all. Nothing was changed."
		)

	target = warehouse or _item_default_warehouse(doc)
	if not target:
		raise ToolError(
			"a reorder level belongs to a warehouse — ERPNext stores it on an Item Reorder "
			"row keyed by one — and this Item has no default warehouse to fall back on. "
			"Pass reorder_warehouse. Nothing was changed."
		)
	if not _exists(WAREHOUSE, target):
		raise ToolError(f"no Warehouse called {target!r} on this site. Nothing was changed.")

	existing = None
	for row in _child_rows(doc, "reorder_levels"):
		if _row_get(row, "warehouse") == target:
			existing = row
			break
	if existing is None:
		existing = doc.append("reorder_levels", {"warehouse": target})
		created = True
	else:
		created = False

	def _put(key, value):
		if isinstance(existing, dict):
			existing[key] = value
		else:
			setattr(existing, key, value)

	if level not in (None, ""):
		_put("warehouse_reorder_level", as_float(level, "reorder_level"))
	if qty not in (None, ""):
		_put("warehouse_reorder_qty", as_float(qty, "reorder_qty"))
	if not _row_get(existing, "material_request_type"):
		_put("material_request_type", "Purchase")

	return {
		"stored_on": "Item Reorder row",
		"warehouse": target,
		"created": created,
		"reorder_level": _row_get(existing, "warehouse_reorder_level"),
		"reorder_qty": _row_get(existing, "warehouse_reorder_qty"),
	}


def _item_default_warehouse(doc) -> str:
	"""This Item's default warehouse, from whichever place the site keeps it."""
	flat = compat.first_field(ITEM, "default_warehouse")
	if flat and doc.get(flat):
		return str(doc.get(flat))
	rows = _child_rows(doc, "item_defaults")
	warehouses = {_row_get(row, "default_warehouse") for row in rows if _row_get(row, "default_warehouse")}
	if len(warehouses) == 1:
		return str(next(iter(warehouses)))
	return ""


# ── Item Group ──────────────────────────────────────────────────────────────


def list_item_groups(args: dict) -> ToolResult:
	"""The Item Group tree, flat, with each node's parent."""
	_require(ITEM_GROUP)
	filters: dict = {}

	parent = as_str(args, "parent_item_group")
	if parent:
		if not _exists(ITEM_GROUP, parent):
			raise ToolError(f"no Item Group called {parent!r} on this site.")
		filters["parent_item_group"] = parent
	is_group = _flag(args, "is_group", filters, ITEM_GROUP)

	limit = as_limit(args)
	rows = _rows(
		ITEM_GROUP,
		filters,
		("name", "item_group_name", "parent_item_group", "is_group"),
		"name asc",
		limit,
	)
	groups = [_clean(row) for row in rows]

	data = {
		"item_groups": groups,
		"count": len(groups),
		"limit": limit,
		"truncated": len(groups) == limit,
		"roots": [group["name"] for group in groups if not group.get("parent_item_group")],
		"filters": {"parent_item_group": parent or None, "is_group": is_group},
		"note": "Flat, not nested. Each row names its parent; the roots are listed separately.",
	}
	return ToolResult(data, f"{len(groups)} item group(s)")


def create_item_group(args: dict) -> ToolResult:
	"""Create one Item Group under an existing group node."""
	_require(ITEM_GROUP)
	name = as_str(args, "item_group_name", required=True)
	is_group = bool(as_bool(args, "is_group", False))
	parent = _tree_parent(
		ITEM_GROUP,
		"parent_item_group",
		as_str(args, "parent_item_group"),
		ALL_ITEM_GROUPS,
		"parent_item_group",
	)

	if _exists(ITEM_GROUP, name):
		raise ToolError(
			f"an Item Group called {name!r} already exists on this site. ERPNext names an "
			"Item Group after itself, so the name is the docname. Nothing was created."
		)

	doc = frappe.new_doc(ITEM_GROUP)
	doc.item_group_name = name
	doc.parent_item_group = parent
	doc.is_group = 1 if is_group else 0
	doc.insert()

	return ToolResult(
		{
			"name": doc.name,
			"item_group_name": name,
			"parent_item_group": parent,
			"is_group": is_group,
			"next_step": (
				"A group node holds other groups; file items under a leaf."
				if is_group
				else "Leaf node, ready for items."
			),
		},
		f"created Item Group {doc.name} under {parent}",
		docstatus_delta="none → created",
	)


# ── Supplier ────────────────────────────────────────────────────────────────


def _party_company_note(doctype: str, company: str) -> str | None:
	"""What to say about a `company` argument on a site-wide doctype.

	None when the site really does have a company column (a custom field some
	installs add), in which case the caller's filter was honoured.
	"""
	if compat.has_field(doctype, "company"):
		return None
	return (
		f"company was NOT applied: an ERPNext {doctype} is site-wide and has no company "
		f"column. {company!r} was validated as a real Company and otherwise ignored — the "
		"rows below are every match on this site."
	)


def ensure_sales_channel_field() -> bool:
	"""Give Customer the column that says which channel this buyer is.

	Created at migrate time by `install.py` and here on first use, the same
	arrangement `banking_bridge.ensure_categorization_fields` and
	`anchors.ensure_pairing_fields` are under: a bench that pulled the code
	without running the installer still answers the first call, and running the
	installer means the column is filterable in the Desk before anybody needs it.

	NEVER RAISES. A site that will not take the field loses the channel and keeps
	every other thing a Customer does; the read tools report the column as absent
	rather than reporting every customer as unclassified, which are different
	claims.
	"""
	try:
		if compat.has_field(CUSTOMER, SALES_CHANNEL_FIELD):
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists(CUSTOMER):
			return False
		if frappe.db.exists(CUSTOM_FIELD, {"dt": CUSTOMER, "fieldname": SALES_CHANNEL_FIELD}):
			return True
	except Exception:
		return False

	try:
		doc = frappe.new_doc(CUSTOM_FIELD)
		doc.dt = CUSTOMER
		doc.fieldname = SALES_CHANNEL_FIELD
		doc.label = "Sales Channel"
		doc.fieldtype = "Select"
		doc.options = "\n" + "\n".join(SALES_CHANNELS)
		doc.insert_after = "customer_group"
		doc.module = "ERPNext MCP"
		doc.description = (
			"How this buyer takes the fruit. Direct is the farm stand, the farmers' market, "
			"u-pick and direct-to-retail; Wholesale, Packer and Processor are the three ways "
			"it leaves through somebody else. LEFT BLANK MEANS NOBODY HAS CLASSIFIED THIS "
			"CUSTOMER, which is not the same as not-direct — a report that read a blank as "
			"wholesale would understate the direct share of every farm that has not finished "
			"the exercise."
		)
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title=f"erpnext_mcp: could not add {SALES_CHANNEL_FIELD} to Customer",
			message=compat.traceback_text(),
		)
		return False

	try:
		frappe.clear_cache(doctype=CUSTOMER)
	except Exception:
		pass
	return compat.has_field(CUSTOMER, SALES_CHANNEL_FIELD)


def _sales_channel(value: str, verb: str) -> str:
	"""One of the four channels, or a refusal naming all four.

	Read through `as_choice` off the site's own meta where the column exists, so
	an operator who has added a fifth channel gets their own list back rather than
	this app's idea of it.
	"""
	if not value:
		return ""
	if compat.has_field(CUSTOMER, SALES_CHANNEL_FIELD):
		return as_choice(CUSTOMER, SALES_CHANNEL_FIELD, value, SALES_CHANNEL_FIELD)
	raise ToolError(
		f"this site's Customer has no {SALES_CHANNEL_FIELD} column yet — it is a Custom Field "
		"this app installs, so run `bench --site <site> migrate`. Nothing was " + verb + "."
	)


def list_suppliers(args: dict) -> ToolResult:
	"""Suppliers by group and disabled flag."""
	return _list_party(
		args,
		doctype=SUPPLIER,
		group_arg="supplier_group",
		group_doctype="Supplier Group",
		name_field="supplier_name",
		fields=_SUPPLIER_FIELDS,
		key="suppliers",
	)


def list_customers(args: dict) -> ToolResult:
	"""Customers by group, territory and disabled flag."""
	return _list_party(
		args,
		doctype=CUSTOMER,
		group_arg="customer_group",
		group_doctype="Customer Group",
		name_field="customer_name",
		fields=_CUSTOMER_FIELDS,
		key="customers",
		territory=True,
		channel=True,
	)


def _list_party(
	args: dict,
	*,
	doctype: str,
	group_arg: str,
	group_doctype: str,
	name_field: str,
	fields,
	key: str,
	territory: bool = False,
	channel: bool = False,
) -> ToolResult:
	"""Supplier and Customer are the same listing with the nouns swapped.

	One function rather than two near-copies, for the reason `trade._order_book`
	is one function: a fix to the company reporting or the truncation must not be
	able to land on one side only.
	"""
	_require(doctype)
	filters: dict = {}

	group = as_str(args, group_arg)
	if group:
		if compat.doctype_exists(group_doctype) and not _exists(group_doctype, group):
			known = frappe.db.get_all(group_doctype, pluck="name", limit=25)
			raise ToolError(
				f"no {group_doctype} called {group!r} on this site. Known: "
				f"{', '.join(sorted(known)) or '<none>'}."
			)
		filters[group_arg] = group

	territory_value = ""
	if territory:
		territory_value = as_str(args, "territory")
		if territory_value:
			if compat.doctype_exists("Territory") and not _exists("Territory", territory_value):
				known = frappe.db.get_all("Territory", pluck="name", limit=25)
				raise ToolError(
					f"no Territory called {territory_value!r} on this site. Known: "
					f"{', '.join(sorted(known)) or '<none>'}."
				)
			filters["territory"] = territory_value

	channel_value = ""
	if channel:
		channel_value = _sales_channel(as_str(args, SALES_CHANNEL_FIELD), "listed")
		if channel_value:
			filters[SALES_CHANNEL_FIELD] = channel_value

	disabled = _flag(args, "disabled", filters, doctype)

	search = _like(args)
	if search:
		filters[name_field] = ("like", f"%{search}%")

	company = None
	company_note = None
	if as_str(args, "company"):
		company = resolve_company(as_str(args, "company"))
		company_note = _party_company_note(doctype, company or "")
		if company_note is None:
			filters["company"] = company

	limit = as_limit(args)
	rows = _rows(doctype, filters, fields, f"{name_field} asc", limit)
	parties = [_clean(row) for row in rows]

	data = {
		key: parties,
		"count": len(parties),
		"limit": limit,
		"truncated": len(parties) == limit,
		"filters": {
			group_arg: group or None,
			"disabled": disabled,
			"company": company,
			"search": search or None,
			**({"territory": territory_value or None} if territory else {}),
			**({SALES_CHANNEL_FIELD: channel_value or None} if channel else {}),
		},
	}
	if channel:
		data.update(_channel_rollup(parties))
	if company_note:
		data["company_scope"] = company_note
	return ToolResult(data, f"{len(parties)} {doctype.lower()}(s)")


def _channel_rollup(parties: list) -> dict:
	"""How many customers sit in each channel, and who is in none of them.

	The unclassified list is the useful half. A direct-marketed percentage
	computed over invoices is only as complete as this classification, and a
	report that showed the percentage without showing how many customers were
	never classified would present a partial answer as a whole one.
	"""
	if not compat.has_field(CUSTOMER, SALES_CHANNEL_FIELD):
		return {
			"sales_channel_installed": False,
			"sales_channel_note": (
				f"Customer has no {SALES_CHANNEL_FIELD} column on this site, so no customer is "
				"classified either way. It is a Custom Field this app installs — run `bench "
				"--site <site> migrate`."
			),
		}
	counts: dict = {}
	for party in parties:
		key = party.get(SALES_CHANNEL_FIELD) or "(unclassified)"
		counts[key] = counts.get(key, 0) + 1
	return {
		"sales_channel_installed": True,
		"by_sales_channel": dict(sorted(counts.items())),
		"without_sales_channel": [
			party.get("name") for party in parties if not party.get(SALES_CHANNEL_FIELD)
		],
	}


def get_supplier(args: dict) -> ToolResult:
	"""One Supplier in full, with its per-company account defaults."""
	return _get_party(
		args,
		doctype=SUPPLIER,
		name_field="supplier_name",
		fields=_SUPPLIER_FIELDS,
		account_field="default_payable_account",
	)


def get_customer(args: dict) -> ToolResult:
	"""One Customer in full, with its per-company account defaults."""
	return _get_party(
		args,
		doctype=CUSTOMER,
		name_field="customer_name",
		fields=_CUSTOMER_FIELDS,
		account_field="default_receivable_account",
	)


def _get_party(args: dict, *, doctype: str, name_field: str, fields, account_field: str) -> ToolResult:
	_require(doctype)
	name = _resolve_party(args, doctype, name_field)
	doc = frappe.get_doc(doctype, name)

	data = _clean({field: doc.get(field) for field in compat.existing_fields(doctype, list(fields))})

	accounts = []
	for row in _child_rows(doc, "accounts"):
		accounts.append(
			{
				"company": _row_get(row, "company"),
				"account": _row_get(row, "account"),
			}
		)
	data["company_accounts"] = accounts
	data["company_accounts_note"] = (
		f"Per-company {account_field} overrides. Empty means this party posts to each "
		"company's own default control account, which is the normal case."
	)

	addresses = []
	if compat.doctype_exists("Dynamic Link"):
		addresses = frappe.db.get_all(
			"Dynamic Link",
			filters={"link_doctype": doctype, "link_name": name, "parenttype": "Address"},
			pluck="parent",
			limit=10,
		)
	data["addresses"] = sorted(addresses)

	label = doc.get(name_field) or name
	return ToolResult(data, f"{doctype} {name}: {label}")


def _resolve_party(args: dict, doctype: str, name_field: str) -> str:
	"""A party docname from a docname or from its display name."""
	given = as_str(args, "name") or as_str(args, doctype.lower()) or as_str(args, name_field)
	if not given:
		raise ToolError(f"name (the {doctype} docname or {name_field}) is required.")
	if _exists(doctype, given):
		return given
	match = frappe.db.get_all(doctype, filters={name_field: given}, pluck="name", limit=5)
	if len(match) == 1:
		return match[0]
	if len(match) > 1:
		raise ToolError(
			f"{given!r} matches {len(match)} {doctype} records: {', '.join(sorted(match))}. Pass the docname."
		)
	raise ToolError(f"no {doctype} called {given!r} on this site.")


def create_supplier(args: dict) -> ToolResult:
	"""Create one Supplier."""
	_require(SUPPLIER)
	name = as_str(args, "supplier_name", required=True)
	group = _group_or_refuse(
		"Supplier Group",
		as_str(args, "supplier_group"),
		ALL_SUPPLIER_GROUPS,
		"supplier_group",
		prefer_leaf=True,
	)
	supplier_type = _party_type(SUPPLIER, "supplier_type", as_str(args, "supplier_type"))
	company, company_note = _party_company(SUPPLIER, as_str(args, "company"))

	if _exists(SUPPLIER, name):
		raise ToolError(f"a Supplier called {name!r} already exists on this site. Nothing was created.")

	doc = frappe.new_doc(SUPPLIER)
	doc.supplier_name = name
	doc.supplier_group = group
	if supplier_type:
		doc.supplier_type = supplier_type
	_optional(doc, SUPPLIER, args, ("country", "tax_id", "tax_category", "tax_withholding_category"))
	if as_bool(args, "is_transporter") and compat.has_field(SUPPLIER, "is_transporter"):
		doc.is_transporter = 1
	doc.insert()

	data = {
		"name": doc.name,
		"supplier_name": name,
		"supplier_group": group,
		"supplier_type": supplier_type or None,
		"company": company,
		"submittable": False,
	}
	if company_note:
		data["company_scope"] = company_note
	return ToolResult(
		data,
		f"created Supplier {doc.name} in group {group}",
		docstatus_delta="none → created",
	)


def create_customer(args: dict) -> ToolResult:
	"""Create one Customer."""
	_require(CUSTOMER)
	name = as_str(args, "customer_name", required=True)
	group = _group_or_refuse(
		"Customer Group",
		as_str(args, "customer_group"),
		ALL_CUSTOMER_GROUPS,
		"customer_group",
		prefer_leaf=True,
	)
	territory = _group_or_refuse("Territory", as_str(args, "territory"), ALL_TERRITORIES, "territory")
	customer_type = _party_type(CUSTOMER, "customer_type", as_str(args, "customer_type"))
	company, company_note = _party_company(CUSTOMER, as_str(args, "company"))

	if _exists(CUSTOMER, name):
		raise ToolError(f"a Customer called {name!r} already exists on this site. Nothing was created.")

	doc = frappe.new_doc(CUSTOMER)
	doc.customer_name = name
	doc.customer_group = group
	doc.territory = territory
	if customer_type:
		doc.customer_type = customer_type
	_optional(doc, CUSTOMER, args, ("tax_id", "tax_category", "default_currency", "default_price_list"))
	# v0.97.0. Asked at creation because it is asked once and answered forever,
	# and because a customer register classified retroactively is a register
	# somebody has to go back through.
	channel = ""
	if as_str(args, SALES_CHANNEL_FIELD):
		ensure_sales_channel_field()
		channel = _sales_channel(as_str(args, SALES_CHANNEL_FIELD), "created")
		doc.set(SALES_CHANNEL_FIELD, channel)
	doc.insert()

	data = {
		"name": doc.name,
		"customer_name": name,
		"customer_group": group,
		"territory": territory,
		"customer_type": customer_type or None,
		SALES_CHANNEL_FIELD: channel or None,
		"company": company,
		"submittable": False,
	}
	if company_note:
		data["company_scope"] = company_note
	return ToolResult(
		data,
		f"created Customer {doc.name} in group {group} ({territory})",
		docstatus_delta="none → created",
	)


def _party_type(doctype: str, fieldname: str, value: str) -> str:
	"""Company or Individual, in the doctype's own casing, or `""`.

	Read through `as_choice` off the site's meta rather than compared to a
	hardcoded pair, so a site that has customised the Select gets its own options
	listed back rather than this app's idea of them.
	"""
	if not value:
		return ""
	if not compat.has_field(doctype, fieldname):
		raise ToolError(f"this site's {doctype} has no {fieldname} field. Nothing was created.")
	return as_choice(doctype, fieldname, value, fieldname)


def _party_company(doctype: str, given: str):
	"""Validate a `company` argument on a site-wide party doctype.

	Returns `(company, note)`. The company is validated even when it cannot be
	stored, because a caller who named a company that does not exist has a
	mistake worth hearing about whether or not this record could hold it.
	"""
	if not given:
		return None, None
	company = resolve_company(given)
	if compat.has_field(doctype, "company"):
		return company, None
	return company, (
		f"company was validated but NOT stored: an ERPNext {doctype} is site-wide and has no "
		"company column. Company-specific behaviour for a party lives on its per-company "
		"account rows, which this tool does not write."
	)


def _optional(doc, doctype: str, args: dict, fields) -> None:
	"""Copy the optional string arguments a site actually has fields for."""
	for field in fields:
		value = as_str(args, field)
		if value and compat.has_field(doctype, field):
			doc.set(field, value)


def update_supplier(args: dict) -> ToolResult:
	"""Change one Supplier's group, type, disabled flag or tax identifiers."""
	return _update_party(
		args,
		doctype=SUPPLIER,
		name_field="supplier_name",
		group_arg="supplier_group",
		group_doctype="Supplier Group",
		type_arg="supplier_type",
		extra=("country", "tax_id", "tax_category", "tax_withholding_category"),
	)


def update_customer(args: dict) -> ToolResult:
	"""Change one Customer's group, territory, type, disabled flag or tax id."""
	return _update_party(
		args,
		doctype=CUSTOMER,
		name_field="customer_name",
		group_arg="customer_group",
		group_doctype="Customer Group",
		type_arg="customer_type",
		extra=("tax_id", "tax_category", "default_currency", "default_price_list"),
		territory=True,
		channel=True,
	)


def _update_party(
	args: dict,
	*,
	doctype: str,
	name_field: str,
	group_arg: str,
	group_doctype: str,
	type_arg: str,
	extra,
	territory: bool = False,
	channel: bool = False,
) -> ToolResult:
	"""Update a party in place. Never renames it: the display name is the docname
	on a stock install, and renaming is a different operation with different
	consequences for every document already pointing at it."""
	_require(doctype)
	name = _resolve_party(args, doctype, name_field)
	doc = frappe.get_doc(doctype, name)

	changes: dict = {}

	group = as_str(args, group_arg)
	if group:
		if compat.doctype_exists(group_doctype) and not _exists(group_doctype, group):
			known = frappe.db.get_all(group_doctype, pluck="name", limit=25)
			raise ToolError(
				f"no {group_doctype} called {group!r} on this site. Known: "
				f"{', '.join(sorted(known)) or '<none>'}. Nothing was changed."
			)
		if group != (doc.get(group_arg) or ""):
			changes[group_arg] = [doc.get(group_arg), group]
			doc.set(group_arg, group)

	if territory:
		value = as_str(args, "territory")
		if value:
			if compat.doctype_exists("Territory") and not _exists("Territory", value):
				raise ToolError(f"no Territory called {value!r} on this site. Nothing was changed.")
			if value != (doc.get("territory") or ""):
				changes["territory"] = [doc.get("territory"), value]
				doc.territory = value

	if channel and SALES_CHANNEL_FIELD in args:
		# Installed on demand rather than refused, so classifying the customer
		# register is one call per customer on a site that upgraded without
		# running the installer.
		ensure_sales_channel_field()
		wanted = _sales_channel(as_str(args, SALES_CHANNEL_FIELD), "changed")
		if wanted != (doc.get(SALES_CHANNEL_FIELD) or ""):
			changes[SALES_CHANNEL_FIELD] = [doc.get(SALES_CHANNEL_FIELD), wanted]
			doc.set(SALES_CHANNEL_FIELD, wanted or None)

	party_type = as_str(args, type_arg)
	if party_type:
		resolved = _party_type(doctype, type_arg, party_type)
		if resolved != (doc.get(type_arg) or ""):
			changes[type_arg] = [doc.get(type_arg), resolved]
			doc.set(type_arg, resolved)

	disabled = as_bool(args, "disabled")
	if disabled is not None and compat.has_field(doctype, "disabled"):
		current = _checked(doc.get("disabled"))
		if current != disabled:
			changes["disabled"] = [current, disabled]
			doc.disabled = 1 if disabled else 0

	for field in extra:
		if field not in args or args.get(field) is None:
			continue
		if not compat.has_field(doctype, field):
			continue
		value = str(args[field]).strip()
		if value != (doc.get(field) or ""):
			changes[field] = [doc.get(field), value]
			doc.set(field, value)

	if not changes:
		raise ToolError(
			f"nothing to change on {doctype} {name}. Pass at least one of {group_arg}, "
			f"{type_arg}, disabled{', territory' if territory else ''}"
			f"{', ' + SALES_CHANNEL_FIELD if channel else ''}, "
			f"{', '.join(extra)} — with a value that differs from what is stored."
		)

	doc.save()
	return ToolResult(
		{"name": name, "changed": changes},
		f"updated {doctype} {name}: {', '.join(sorted(changes))}",
		docstatus_delta=f"unchanged ({doctype} has no docstatus)",
	)


# ── Warehouse ───────────────────────────────────────────────────────────────


def list_warehouses(args: dict) -> ToolResult:
	"""Warehouses by company, group flag and disabled flag."""
	_require(WAREHOUSE)
	filters: dict = {}

	company = resolve_company(as_str(args, "company")) if as_str(args, "company") else None
	if company:
		filters["company"] = company

	is_group = _flag(args, "is_group", filters, WAREHOUSE)
	disabled = _flag(args, "disabled", filters, WAREHOUSE)

	parent = as_str(args, "parent_warehouse")
	if parent:
		if not _exists(WAREHOUSE, parent):
			raise ToolError(f"no Warehouse called {parent!r} on this site.")
		filters["parent_warehouse"] = parent

	limit = as_limit(args)
	rows = _rows(WAREHOUSE, filters, _WAREHOUSE_FIELDS, "name asc", limit)
	warehouses = [_clean(row) for row in rows]

	data = {
		"warehouses": warehouses,
		"count": len(warehouses),
		"limit": limit,
		"truncated": len(warehouses) == limit,
		"roots": [w["name"] for w in warehouses if not w.get("parent_warehouse")],
		"filters": {
			"company": company,
			"is_group": is_group,
			"disabled": disabled,
			"parent_warehouse": parent or None,
		},
		"note": (
			"Flat, not nested — each row names its parent. A group warehouse holds other "
			"warehouses and cannot itself hold stock."
		),
	}
	return ToolResult(data, f"{len(warehouses)} warehouse(s)")


def create_warehouse(args: dict) -> ToolResult:
	"""Create one Warehouse under a company's warehouse tree.

	ERPNext names a Warehouse `"<warehouse_name> - <company abbr>"`, so the
	docname a caller will store is predictable — and it is predicted here, before
	anything is written, so a collision is a sentence rather than a duplicate-name
	error out of the framework.
	"""
	_require(WAREHOUSE)
	warehouse_name = as_str(args, "warehouse_name", required=True)
	company = resolve_company(as_str(args, "company"), required=True)
	is_group = bool(as_bool(args, "is_group", False))

	abbr = _company_abbr(company)
	predicted = f"{warehouse_name} - {abbr}" if abbr else warehouse_name
	if _exists(WAREHOUSE, predicted):
		raise ToolError(
			f"a Warehouse named {predicted!r} already exists. ERPNext names warehouses "
			"'<name> - <company abbr>', so this name is taken in this company. "
			"Nothing was created."
		)

	parent = _parent_warehouse(as_str(args, "parent_warehouse"), company)

	warehouse_type = as_str(args, "warehouse_type")
	if warehouse_type:
		if not compat.has_field(WAREHOUSE, "warehouse_type"):
			raise ToolError("this site's Warehouse has no warehouse_type field. Nothing was created.")
		if compat.doctype_exists("Warehouse Type") and not _exists("Warehouse Type", warehouse_type):
			known = frappe.db.get_all("Warehouse Type", pluck="name", limit=25)
			raise ToolError(
				f"no Warehouse Type called {warehouse_type!r} on this site. Known: "
				f"{', '.join(sorted(known)) or '<none>'}. Nothing was created."
			)

	doc = frappe.new_doc(WAREHOUSE)
	doc.warehouse_name = warehouse_name
	doc.company = company
	doc.is_group = 1 if is_group else 0
	if parent:
		doc.parent_warehouse = parent
	if warehouse_type:
		doc.warehouse_type = warehouse_type
	_optional(doc, WAREHOUSE, args, ("city", "phone_no", "address_line_1"))
	doc.insert()

	return ToolResult(
		{
			"name": doc.name,
			"warehouse_name": warehouse_name,
			"company": company,
			"parent_warehouse": parent or None,
			"is_group": is_group,
			"warehouse_type": warehouse_type or None,
			"submittable": False,
			"next_step": (
				"A group warehouse holds other warehouses and cannot hold stock."
				if is_group
				else "Leaf warehouse, ready to receive stock."
			),
		},
		f"created Warehouse {doc.name} in {company}" + (f" under {parent}" if parent else " as a root"),
		docstatus_delta="none → created",
	)


def _parent_warehouse(given: str, company: str) -> str:
	"""The group warehouse a new one hangs off, or `""` for a root.

	When the caller names none, this looks for the company's own root group —
	ERPNext creates `"All Warehouses - <abbr>"` with the company — and returns
	`""` rather than raising when there is none, because a site whose tree was
	built by hand can legitimately have a warehouse with no parent.
	"""
	if given:
		if not _exists(WAREHOUSE, given):
			raise ToolError(
				f"no Warehouse called {given!r} on this site. Call list_warehouses to see the "
				"tree. Nothing was created."
			)
		row = frappe.db.get_value(WAREHOUSE, given, ["company", "is_group"], as_dict=True) or {}
		if row.get("company") and row["company"] != company:
			raise ToolError(
				f"parent warehouse {given!r} belongs to company {row['company']!r}, not "
				f"{company!r}. Nothing was created."
			)
		if not _checked(row.get("is_group")):
			raise ToolError(
				f"warehouse {given!r} is a leaf, not a group, so nothing can be filed under it. "
				"Nothing was created."
			)
		return given

	roots = frappe.db.get_all(
		WAREHOUSE,
		filters={"company": company, "is_group": 1},
		fields=["name", "parent_warehouse"],
		limit=25,
	)
	for row in roots:
		if not row.get("parent_warehouse"):
			return str(row["name"])
	if len(roots) == 1:
		return str(roots[0]["name"])
	return ""


# ── prices ──────────────────────────────────────────────────────────────────


def list_price_lists(args: dict) -> ToolResult:
	"""Price Lists on this site, with their currency and buying/selling flags."""
	_require(PRICE_LIST)
	filters: dict = {}
	enabled = _flag(args, "enabled", filters, PRICE_LIST)
	buying = _flag(args, "buying", filters, PRICE_LIST)
	selling = _flag(args, "selling", filters, PRICE_LIST)

	limit = as_limit(args)
	rows = _rows(PRICE_LIST, filters, _PRICE_LIST_FIELDS, "name asc", limit)
	price_lists = [_clean(row) for row in rows]

	data = {
		"price_lists": price_lists,
		"count": len(price_lists),
		"limit": limit,
		"truncated": len(price_lists) == limit,
		"filters": {"enabled": enabled, "buying": buying, "selling": selling},
		"note": (
			"A Price List holds no rates itself — the rates are Item Price rows pointing at "
			"it. Use get_item_price to read one and set_item_price to write one."
		),
	}
	return ToolResult(data, f"{len(price_lists)} price list(s)")


def get_item_price(args: dict) -> ToolResult:
	"""Every Item Price for one item, optionally narrowed to one price list."""
	_require(ITEM_PRICE)
	code = _require_item(args)

	filters: dict = {"item_code": code}
	price_list = as_str(args, "price_list")
	if price_list:
		if compat.doctype_exists(PRICE_LIST) and not _exists(PRICE_LIST, price_list):
			known = frappe.db.get_all(PRICE_LIST, pluck="name", limit=25)
			raise ToolError(
				f"no Price List called {price_list!r} on this site. Known: "
				f"{', '.join(sorted(known)) or '<none>'}."
			)
		filters["price_list"] = price_list

	uom = as_str(args, "uom")
	if uom and compat.has_field(ITEM_PRICE, "uom"):
		filters["uom"] = uom
	for party_field in ("customer", "supplier"):
		value = as_str(args, party_field)
		if value and compat.has_field(ITEM_PRICE, party_field):
			filters[party_field] = value

	limit = as_limit(args)
	rows = _rows(ITEM_PRICE, filters, _ITEM_PRICE_FIELDS, "price_list asc, valid_from desc", limit)
	prices = [_clean(row) for row in rows]

	# A date narrows the answer without hiding the rest: `applicable` is the
	# subset whose window covers it, and the full list stays there, because "the
	# price yesterday" and "what prices exist" are both questions somebody asks.
	as_of = as_date(args, "as_of")
	applicable = [price for price in prices if _covers(price, as_of)] if as_of else prices

	rate = None
	if len(applicable) == 1:
		rate = float(applicable[0].get("price_list_rate") or 0)

	data = {
		"item_code": code,
		"prices": prices,
		"count": len(prices),
		"limit": limit,
		"truncated": len(prices) == limit,
		"applicable": applicable if as_of else None,
		"as_of": as_of,
		"price_list_rate": rate,
		"filters": {
			"price_list": price_list or None,
			"uom": uom or None,
			"customer": as_str(args, "customer") or None,
			"supplier": as_str(args, "supplier") or None,
		},
		"note": (
			"price_list_rate is filled ONLY when exactly one row applies. More than one and "
			"the choice is ERPNext's pricing rules to make, not this tool's."
		),
	}
	return ToolResult(
		data,
		f"{len(prices)} price(s) for {code}" + (f" on {price_list}" if price_list else ""),
	)


def _covers(price: dict, as_of: str) -> bool:
	"""Whether an Item Price's validity window contains `as_of`.

	An open end is open: a row with no `valid_upto` is still in force, which is
	how most rows on a real site look.
	"""
	start = price.get("valid_from")
	end = price.get("valid_upto")
	if start and str(start) > as_of:
		return False
	if end and str(end) < as_of:
		return False
	return True


def set_item_price(args: dict) -> ToolResult:
	"""Create or update one Item Price.

	The key is the whole of (item, price list, UOM, customer, supplier,
	valid_from), not just the item and the list — that is what ERPNext's own
	duplicate check uses, and matching on less would overwrite a customer's
	negotiated rate with the list rate. When the key matches more than one row,
	this refuses and names them.
	"""
	_require(ITEM_PRICE)
	code = _require_item(args)

	price_list = as_str(args, "price_list", required=True)
	if compat.doctype_exists(PRICE_LIST):
		if not _exists(PRICE_LIST, price_list):
			known = frappe.db.get_all(PRICE_LIST, pluck="name", limit=25)
			raise ToolError(
				f"no Price List called {price_list!r} on this site. Known: "
				f"{', '.join(sorted(known)) or '<none>'}. Nothing was written."
			)

	if args.get("rate") in (None, "") and args.get("price_list_rate") in (None, ""):
		raise ToolError("rate is required.")
	rate = as_float(
		args.get("rate") if args.get("rate") not in (None, "") else args.get("price_list_rate"),
		"rate",
	)
	if rate < 0:
		raise ToolError("rate cannot be negative. Nothing was written.")

	uom = as_str(args, "uom")
	customer = as_str(args, "customer")
	supplier = as_str(args, "supplier")
	if customer and supplier:
		raise ToolError(
			"an Item Price is either a customer's price or a supplier's, not both. Nothing was written."
		)
	if customer and not _exists(CUSTOMER, customer):
		raise ToolError(f"no Customer called {customer!r} on this site. Nothing was written.")
	if supplier and not _exists(SUPPLIER, supplier):
		raise ToolError(f"no Supplier called {supplier!r} on this site. Nothing was written.")

	valid_from = as_date(args, "valid_from")
	valid_upto = as_date(args, "valid_upto")
	if valid_from and valid_upto and valid_from > valid_upto:
		raise ToolError(f"valid_from {valid_from} is after valid_upto {valid_upto}. Nothing was written.")

	key: dict = {"item_code": code, "price_list": price_list}
	for field, value in (("uom", uom), ("customer", customer), ("supplier", supplier)):
		if compat.has_field(ITEM_PRICE, field):
			key[field] = value or ""
	if valid_from and compat.has_field(ITEM_PRICE, "valid_from"):
		key["valid_from"] = valid_from

	matches = frappe.db.get_all(ITEM_PRICE, filters=key, pluck="name", limit=10)
	if len(matches) > 1:
		raise ToolError(
			f"{len(matches)} Item Price rows already match that key: {', '.join(sorted(matches))}. "
			"This site has duplicates ERPNext's own check would have refused; clean them up in "
			"the Desk rather than having this tool pick one. Nothing was written."
		)

	currency = as_str(args, "currency") or (
		frappe.db.get_value(PRICE_LIST, price_list, "currency") if compat.doctype_exists(PRICE_LIST) else ""
	)

	if matches:
		doc = frappe.get_doc(ITEM_PRICE, matches[0])
		before = float(doc.get("price_list_rate") or 0)
		doc.price_list_rate = rate
		if valid_upto and compat.has_field(ITEM_PRICE, "valid_upto"):
			doc.valid_upto = valid_upto
		if currency and compat.has_field(ITEM_PRICE, "currency"):
			doc.currency = currency
		doc.save()
		return ToolResult(
			{
				"name": doc.name,
				"created": False,
				"item_code": code,
				"price_list": price_list,
				"price_list_rate": rate,
				"previous_rate": before,
				"currency": currency or None,
				"uom": uom or None,
				"customer": customer or None,
				"supplier": supplier or None,
				"valid_from": valid_from,
				"valid_upto": valid_upto,
			},
			f"updated Item Price {doc.name}: {code} on {price_list} {before} → {rate}",
			docstatus_delta=f"{before} → {rate}",
		)

	doc = frappe.new_doc(ITEM_PRICE)
	doc.item_code = code
	doc.price_list = price_list
	doc.price_list_rate = rate
	for field, value in (
		("uom", uom),
		("customer", customer),
		("supplier", supplier),
		("currency", currency),
		("valid_from", valid_from),
		("valid_upto", valid_upto),
	):
		if value and compat.has_field(ITEM_PRICE, field):
			doc.set(field, value)
	doc.insert()

	return ToolResult(
		{
			"name": doc.name,
			"created": True,
			"item_code": code,
			"price_list": price_list,
			"price_list_rate": rate,
			"previous_rate": None,
			"currency": currency or None,
			"uom": uom or None,
			"customer": customer or None,
			"supplier": supplier or None,
			"valid_from": valid_from,
			"valid_upto": valid_upto,
		},
		f"created Item Price {doc.name}: {code} on {price_list} at {rate}",
		docstatus_delta="none → created",
	)
