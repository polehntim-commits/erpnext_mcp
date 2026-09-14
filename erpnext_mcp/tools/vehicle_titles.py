# SPDX-License-Identifier: MIT
"""Vehicle titles, MCOs and bills of sale — captured like a receipt, filed on the asset.

v0.165.0. A truck's title is a piece of paper somebody photographs at the kitchen
table, and the phone already has a flow for photographing paper: receipt capture.
So a title is an Expense Receipt in one of two DOCUMENT CATEGORIES, `Title/MCO` and
`Bill of Sale`, carrying three extra columns — `document_subtype`, `vin` and
`linked_asset` — and the Vehicle or Tractor it belongs to carries the other side:
`vin`, `license_plate`, `title_holder`, `lien_holder` and `title_receipt`.

NO NEW DOCTYPE. The register already has the photograph, the OCR text, the date,
the company and the person who captured it, and a Title Document doctype would
have been all of those columns again.

A DOCUMENT CATEGORY IS NOT MONEY SPENT. The amount is optional and defaults to 0,
`create_purchase_invoice_from_receipt` refuses one by name, and
`get_expense_summary` leaves them out of its totals. A bill of sale may print a
price; that price is the asset's `purchase_value`, not a fuel-and-parts expense.

THE VIN FINDS THE ASSET, AND ONLY AN EXACT, SINGLE MATCH LINKS. `match_asset`
normalises both sides (case, spaces, dashes) and looks at `vin` first, then
`serial_number` — `register_asset` documented `serial_number` as "the serial or
VIN off the plate" for eighty releases, so that is where existing vehicles keep
it. Two assets answering to one VIN is reported with both names and links
neither: guessing would file somebody's lien against the wrong truck.

AN AUTOMATIC MATCH NEVER REPLACES A TITLE DOCUMENT A PERSON ALREADY LINKED. A
person naming the asset — `linked_asset` at capture, or `link_title_to_asset`
afterwards — does replace it, and the answer names what it replaced.

ONE LIST OF TITLED TYPES. `TITLED_ASSET_TYPES` is what the Asset Register fields'
`depends_on` shows the section for, what a link is refused outside of, and what
`register_asset` accepts the four title arguments on. A test holds the doctype
JSON to this tuple.
"""

import frappe

from .. import compat
from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult
from . import expenses

ASSET_REGISTER = "Asset Register"
EXPENSE_RECEIPT = expenses.EXPENSE_RECEIPT

#: The asset types a title belongs to. See the module docstring.
TITLED_ASSET_TYPES = ("Vehicle", "Tractor")

TITLE_CATEGORY = expenses.TITLE_CATEGORY
BILL_OF_SALE_CATEGORY = expenses.BILL_OF_SALE_CATEGORY
DOCUMENT_CATEGORIES = expenses.DOCUMENT_CATEGORIES

#: What `document_subtype` may say. Mirrors the doctype's Select options.
DOCUMENT_SUBTYPES = ("Vehicle Title", "MCO", "Bill of Sale", "Registration")

#: The receipt-side columns this release adds.
RECEIPT_FIELDS = ("document_subtype", "vin", "linked_asset")

#: The asset-side columns this release adds, in the order `get_asset_detail` answers them.
ASSET_FIELDS = ("vin", "license_plate", "title_holder", "lien_holder", "title_receipt")

#: The four an asset is registered with. `title_receipt` is set by linking, never typed.
REGISTRABLE_ASSET_FIELDS = ("vin", "license_plate", "title_holder", "lien_holder")

#: Receipt columns `get_asset_detail` reports for each document filed against an asset.
_DOCUMENT_READ_FIELDS = ("name", "category", "document_subtype", "vin", "merchant", "receipt_date", "status")

#: How many assets one VIN lookup reads. A company's vehicles and tractors, not its valves.
_MATCH_SCAN_CAP = 5000


def normalize_vin(value) -> str:
	"""A VIN as it compares: upper case, no spaces, no dashes. Empty stays empty.

	NOT VALIDATED AS SEVENTEEN CHARACTERS. A vehicle built before 1981 has a shorter
	one, and a tractor's title prints a serial number in the same box. Refusing
	those would refuse the paper the farm actually holds.
	"""
	return "".join(str(value or "").split()).replace("-", "").upper()


def receipt_fields_installed() -> bool:
	return all(compat.has_field(EXPENSE_RECEIPT, field) for field in RECEIPT_FIELDS)


def asset_fields_installed() -> bool:
	return all(compat.has_field(ASSET_REGISTER, field) for field in ASSET_FIELDS)


def require_installed(tail: str) -> None:
	"""Refuse on a bench that has the code and not the columns."""
	if not (receipt_fields_installed() and asset_fields_installed()):
		raise ToolError(
			"this site's Expense Receipt and Asset Register do not have the vehicle title "
			f"columns yet. Run `bench --site <site> migrate` after installing v0.165.0. {tail}"
		)


def require_subtype(value: str, tail: str) -> str:
	if value and value not in DOCUMENT_SUBTYPES:
		raise ToolError(f"document_subtype must be one of: {', '.join(DOCUMENT_SUBTYPES)}. {tail}")
	return value


def require_titled_asset(asset: str, company: str, tail: str) -> dict:
	"""The asset a title may be filed on, or a refusal naming why it may not.

	Checked BEFORE anything is written, so a capture naming the wrong asset is
	refused whole rather than filed and left unlinked.
	"""
	if not frappe.db.exists(ASSET_REGISTER, asset):
		raise ToolError(f"no Asset Register record called {asset!r}. list_assets has the register. {tail}")
	row = frappe.db.get_value(
		ASSET_REGISTER, asset, ["name", "asset_type", "company", "vin", "title_receipt"], as_dict=True
	)
	if row.get("asset_type") not in TITLED_ASSET_TYPES:
		raise ToolError(
			f"{asset} is a {row.get('asset_type') or 'untyped asset'}, and a title is filed on a "
			f"{' or a '.join(TITLED_ASSET_TYPES)}. {tail}"
		)
	if company and row.get("company") and row["company"] != company:
		raise ToolError(
			f"{asset} belongs to {row['company']}, and this document was filed for {company}. A "
			f"title is filed on the entity that owns the vehicle. {tail}"
		)
	return dict(row)


def match_asset(vin: str, company: str) -> dict:
	"""The one Vehicle or Tractor in `company` this VIN names, if there is exactly one.

	Returns `asset` (a docname or None), `matched_on` (`vin`, `serial_number` or
	None) and `candidates` — every asset that answered, so an ambiguous match can
	be settled by a person rather than guessed at.
	"""
	wanted = normalize_vin(vin)
	if not wanted:
		return {"asset": None, "matched_on": None, "candidates": []}
	filters = {"asset_type": ("in", list(TITLED_ASSET_TYPES))}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(
		ASSET_REGISTER,
		filters=filters,
		fields=compat.existing_fields(ASSET_REGISTER, ("name", "vin", "serial_number")),
		order_by="name asc",
		limit_page_length=_MATCH_SCAN_CAP,
	)
	for column in ("vin", "serial_number"):
		hits = sorted(str(row["name"]) for row in rows if normalize_vin(row.get(column)) == wanted)
		if hits:
			return {"asset": hits[0] if len(hits) == 1 else None, "matched_on": column, "candidates": hits}
	return {"asset": None, "matched_on": None, "candidates": []}


def link(receipt: str, asset: str, *, replace: bool) -> dict:
	"""File one document on one asset, both ways. The asset was checked by the caller.

	`replace` is whether a person named this asset. When they did, the asset's
	`title_receipt` is set whatever it held; when a VIN match did, a title document
	already on the asset is kept and reported.

	A receipt moving from one asset to another clears the first asset's
	`title_receipt` if it still pointed here, so no asset is left naming a document
	that is filed somewhere else.
	"""
	document = frappe.db.get_value(EXPENSE_RECEIPT, receipt, ["vin", "linked_asset"], as_dict=True) or {}
	target = frappe.db.get_value(ASSET_REGISTER, asset, ["vin", "title_receipt"], as_dict=True) or {}

	previous_asset = str(document.get("linked_asset") or "")
	unlinked_from = None
	if previous_asset and previous_asset != asset:
		if frappe.db.get_value(ASSET_REGISTER, previous_asset, "title_receipt") == receipt:
			frappe.db.set_value(ASSET_REGISTER, previous_asset, "title_receipt", None)
		unlinked_from = previous_asset
	if previous_asset != asset:
		frappe.db.set_value(EXPENSE_RECEIPT, receipt, "linked_asset", asset)

	updates = {}
	held = str(target.get("title_receipt") or "")
	replaced = None
	kept = None
	if held != receipt:
		if not held or replace:
			updates["title_receipt"] = receipt
			replaced = held or None
		else:
			kept = held

	document_vin = normalize_vin(document.get("vin"))
	asset_vin = str(target.get("vin") or "")
	vin_copied = False
	vin_mismatch = None
	if document_vin and not asset_vin:
		updates["vin"] = document_vin
		vin_copied = True
	elif document_vin and normalize_vin(asset_vin) != document_vin:
		# Reported, never overwritten. Which of the two is the misread is a question
		# for somebody holding the paper, and the asset's VIN may be the one a
		# registration was checked against.
		vin_mismatch = {"document": document_vin, "asset": asset_vin}

	if updates:
		frappe.db.set_value(ASSET_REGISTER, asset, updates)

	return {
		"receipt": receipt,
		"asset": asset,
		"title_receipt": receipt if "title_receipt" in updates or held == receipt else kept,
		"title_receipt_replaced": replaced,
		"title_receipt_kept": kept,
		"unlinked_from": unlinked_from,
		"vin_copied": vin_copied,
		"vin_mismatch": vin_mismatch,
	}


def documents_for(asset: str) -> list[dict]:
	"""Every title, MCO, bill of sale and registration filed on one asset, newest first."""
	if not receipt_fields_installed():
		return []
	rows = frappe.db.get_all(
		EXPENSE_RECEIPT,
		filters={"linked_asset": asset},
		fields=list(_DOCUMENT_READ_FIELDS),
		order_by="receipt_date desc, name desc",
		limit_page_length=100,
	)
	return [{**dict(row), "receipt_date": str(row.get("receipt_date") or "") or None} for row in rows]


def asset_title(row: dict) -> dict:
	"""The title columns off one asset, plus the documents filed on it. Empty off a bench without them."""
	if not asset_fields_installed():
		return {}
	values = frappe.db.get_value(ASSET_REGISTER, row["name"], list(ASSET_FIELDS), as_dict=True) or {}
	out = {field: values.get(field) or None for field in ASSET_FIELDS}
	documents = documents_for(row["name"])
	out["title_document"] = next((doc for doc in documents if doc["name"] == out["title_receipt"]), None)
	if out["title_receipt"] and out["title_document"] is None:
		# Set by hand in the Desk to a receipt that is not filed back on this asset.
		one = frappe.db.get_value(
			EXPENSE_RECEIPT, out["title_receipt"], list(_DOCUMENT_READ_FIELDS), as_dict=True
		)
		if one:
			out["title_document"] = {**dict(one), "receipt_date": str(one.get("receipt_date") or "") or None}
	out["title_documents"] = documents
	out["title_document_count"] = len(documents)
	return out


# ── link_title_to_asset ─────────────────────────────────────────────────────
def link_title_to_asset(args: dict) -> ToolResult:
	"""File a captured title, MCO or bill of sale on the Vehicle or Tractor it belongs to.

	BOTH DOCUMENT CATEGORIES, NOT ONLY `Title/MCO`. Capture auto-links a bill of
	sale by its VIN, so refusing one here would leave a mismatched bill of sale that
	could be filed at capture and never corrected afterwards.

	THE PERSON NAMING THE ASSET WINS: the asset's `title_receipt` becomes this
	document whatever it held, and `title_receipt_replaced` says what that was. The
	VIN is copied onto the asset only where the asset has none.
	"""
	tail = "Nothing was linked."
	require_installed(tail)
	receipt = as_str(args, "receipt") or as_str(args, "receipt_name") or as_str(args, "name")
	asset = as_str(args, "asset") or as_str(args, "asset_name")
	if not receipt:
		raise ToolError(f"receipt (the Expense Receipt docname) is required. {tail}")
	if not asset:
		raise ToolError(f"asset (the Asset Register docname) is required. {tail}")
	if not frappe.db.exists(EXPENSE_RECEIPT, receipt):
		raise ToolError(f"no Expense Receipt called {receipt!r} on this site. {tail}")

	document = frappe.db.get_value(EXPENSE_RECEIPT, receipt, ["category", "company"], as_dict=True)
	if document.get("category") not in DOCUMENT_CATEGORIES:
		raise ToolError(
			f"{receipt} is categorised {document.get('category')!r}, and only a "
			f"{' or '.join(DOCUMENT_CATEGORIES)} document is filed on an asset. Recategorise it "
			f"with update_expense_receipt first if it really is a title. {tail}"
		)
	require_titled_asset(asset, str(document.get("company") or ""), tail)

	result = link(receipt, asset, replace=True)
	return ToolResult(
		data=result,
		summary=f"filed {receipt} on {asset}"
		+ (f", replacing {result['title_receipt_replaced']}" if result["title_receipt_replaced"] else "")
		+ (", VIN copied to the asset" if result["vin_copied"] else "")
		+ (" — the VINs disagree" if result["vin_mismatch"] else ""),
		docstatus_delta="0 → 0 (updated)",
	)
