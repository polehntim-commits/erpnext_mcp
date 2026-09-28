# SPDX-License-Identifier: MIT
"""A product's label, for the phone. v0.204.0.

Tim: "So an applicator can look at the label of the product they are handling.
This would allso work when we are spraying as well." docs/design/
form_schema_and_labels.md §4. The label is the EPA PDF `epa_ppls` attached to
the Item, the photographs `register_product_label` filed against it, and the
label facts the Item carries — one answer, so the phone has one screen.

AN ITEM IS NOT A PERSONNEL RECORD AND NOT ENTITY-SCOPED: every worker who may
handle a product may read its label, which is the whole point of FIFRA's "the
label is the law". The files served are only those attached to that Item.
"""

from __future__ import annotations

import frappe

from . import compat
from .errors import ToolError

ITEM = "Item"
FILE = "File"
_IMAGE = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp")

#: The label facts, in the order a worker reads them.
KEY_FIELDS = (
	"epa_registration_number",
	"signal_word",
	"restricted_use",
	"active_ingredients",
	"ppe_requirements",
	"rei_hours",
	"phi_days",
	"phi_crop",
	"application_rate",
	"application_rate_uom",
	"pesticide_use_scope",
	"storage_disposal",
	"product_form",
	"package_size",
	"tamper_resistant_station_required",
	"max_distance_from_structure_ft",
	"burrow_baiting_allowed",
	"min_bait_days",
	"interior_use_allowed",
)


def _kind(file_name: str) -> str:
	name = str(file_name or "")
	lower = name.lower()
	if lower.endswith(".pdf") and lower.startswith("epa label"):
		return "epa_label_pdf"
	if lower.endswith(_IMAGE):
		return "label_photo"
	return "other"


def label_files(item_code: str) -> list:
	"""Every file on the Item, classified, EPA PDF first then photos, newest first."""
	if not item_code or not compat.doctype_exists(FILE):
		return []
	rows = frappe.db.get_all(
		FILE,
		filters={"attached_to_doctype": ITEM, "attached_to_name": item_code},
		fields=compat.existing_fields(
			FILE, ("name", "file_name", "file_url", "file_size", "modified", "is_folder")
		),
		limit=200,
	)
	out = []
	for row in rows or []:
		if compat.checked(row.get("is_folder")):
			continue
		kind = _kind(row.get("file_name"))
		name = str(row.get("file_name") or "")
		out.append(
			{
				"file": row["name"],
				"file_name": name,
				"kind": kind,
				"content_type": "application/pdf" if name.lower().endswith(".pdf") else _image_type(name),
				"file_size": int(row.get("file_size") or 0) or None,
				"modified": str(row.get("modified") or "") or None,
			}
		)
	order = {"epa_label_pdf": 0, "label_photo": 1, "other": 2}
	out.sort(key=lambda row: (order[row["kind"]], "" if not row["modified"] else _neg(row["modified"])))
	return out


def _neg(stamp: str) -> str:
	# Newest first inside a kind, without parsing: invert each digit.
	return "".join(chr(ord("9") - ord(ch) + ord("0")) if ch.isdigit() else ch for ch in stamp)


def _image_type(name: str) -> str:
	lower = name.lower()
	for suffix, kind in (
		(".png", "image/png"),
		(".heic", "image/heic"),
		(".heif", "image/heif"),
		(".gif", "image/gif"),
		(".webp", "image/webp"),
	):
		if lower.endswith(suffix):
			return kind
	return "image/jpeg" if lower.endswith((".jpg", ".jpeg")) else "application/octet-stream"


def has_label_pdf(item_code: str) -> bool:
	return any(row["kind"] == "epa_label_pdf" for row in label_files(item_code))


def item_label(item_code: str) -> dict:
	"""§4 `get_item_label`: the key fields and the files."""
	code = str(item_code or "").strip()
	if not code or not frappe.db.exists(ITEM, code):
		raise ToolError(f"no Item called {code or '(none)'!r} on this site.")
	fields = compat.existing_fields(ITEM, ("name", "item_name", "item_group", "stock_uom", *KEY_FIELDS))
	row = dict(frappe.db.get_value(ITEM, code, fields, as_dict=True) or {})
	files = label_files(code)
	facts = {}
	for key in KEY_FIELDS:
		if key not in row:
			continue
		value = row.get(key)
		if key == "active_ingredients" and isinstance(value, str) and value.strip():
			try:
				import json

				value = json.loads(value)
			except ValueError:
				pass
		if key in ("restricted_use", "tamper_resistant_station_required"):
			value = compat.checked(value)
		facts[key] = value if value not in ("",) else None
	return {
		"item_code": code,
		"item_name": row.get("item_name") or code,
		"item_group": row.get("item_group") or None,
		"stock_uom": row.get("stock_uom") or None,
		**facts,
		"files": files,
		"label_available": any(row["kind"] == "epa_label_pdf" for row in files),
	}


def item_label_file(item_code: str, file: str, max_bytes=None) -> dict:
	"""§4 `get_item_label_file`: one of THAT Item's files, base64. Refuses any other file."""
	from .tools import files as file_tools

	code = str(item_code or "").strip()
	wanted = str(file or "").strip()
	if not code or not frappe.db.exists(ITEM, code):
		raise ToolError(f"no Item called {code or '(none)'!r} on this site.")
	if wanted not in {row["file"] for row in label_files(code)}:
		raise ToolError(f"{wanted!r} is not one of {code}'s label files. Nothing was read.")
	return file_tools.attachment_content_on_authorized_parent(ITEM, code, wanted, max_bytes).data
