# SPDX-License-Identifier: MIT
"""County tax lots and lot line adjustments, as tools. v0.169.0.

`erpnext_mcp/land_adjustment.py` is the engine and argues the design; this file
is the twelve doors onto it.

  taxlot_lookup        find a lot at the county (by number, point or box), or seed one by hand
  taxlot_refresh       re-read one cached lot from the county
  lla_list / lla_get   read adjustments
  lla_create           a new draft
  lla_update           header fields and the status, which submits, approves, records or withdraws
  lla_add_piece        add a piece, or update the one of that name
  lla_set_geometry     one piece's polygon — sketch before the survey, final after
  lla_add_open_item    add an open item, or update the one of that name
  lla_add_easement     add an easement
  lla_render_mou       the Memorandum of Understanding, HTML and PDF. READ.
  lla_record_survey    the button: write the adjusted Parcels. System Manager only.
"""

from __future__ import annotations

import frappe

from .. import compat, geo
from .. import land_adjustment as land
from .. import mou_print_format as mou
from ..args import as_bool, as_date, as_float, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import realestate

#: Header fields a caller may set on a draft. Everything the DocType carries
#: except the status (its own path), the child tables (their own tools) and the
#: columns only Record Survey writes.
HEADER_FIELDS = (
	"title",
	"county",
	"state",
	"target_close",
	"lot_1",
	"lot_2",
	"lot_1_acres_surveyed",
	"lot_2_acres_surveyed",
	"party_1_type",
	"party_1",
	"signer_1",
	"signer_1_title",
	"party_2_type",
	"party_2",
	"signer_2",
	"signer_2_title",
	"consideration",
	"true_up_amount",
	"true_up_payer",
	"lender",
	"lender_conditions",
	"recording_number",
	"recorded_on",
	"survey_reference",
	"notes",
)
NUMBER_FIELDS = ("lot_1_acres_surveyed", "lot_2_acres_surveyed", "true_up_amount")
DATE_FIELDS = ("target_close", "recorded_on")
#: Written by Record Survey and nothing else.
SURVEY_STAMPS = ("parcel_1", "parcel_2", "survey_recorded_on", "survey_recorded_by")

PIECE_FIELDS = (
	"piece_name",
	"from_party",
	"to_party",
	"acres_gis",
	"acres_surveyed",
	"gis_sketch_ref",
	"line_notes",
	"improvements",
	"geometry",
)
OPEN_ITEM_FIELDS = ("item", "question", "responsible", "status", "due")
EASEMENT_FIELDS = ("easement_type", "burdened", "benefited", "notes")


# ── gates ───────────────────────────────────────────────────────────────────
def _require_doctypes(*doctypes) -> None:
	for doctype in doctypes:
		compat.require_doctype(
			doctype, "It ships with erpnext_mcp v0.169.0 — run `bench --site <site> migrate` after upgrading."
		)


def _agreement_actor(tail: str) -> str:
	_require_doctypes(land.COUNTY_TAX_LOT, land.LOT_LINE_ADJUSTMENT)
	return land.require_roles(land.AGREEMENT_ROLES, "edit a lot line adjustment", tail)


def _load(args: dict) -> object:
	name = as_str(args, "name") or as_str(args, "lot_line_adjustment")
	if not name:
		raise ToolError("name (the Lot Line Adjustment docname, e.g. LLA-2026-0001) is required.")
	if not frappe.db.exists(land.LOT_LINE_ADJUSTMENT, name):
		raise ToolError(f"no Lot Line Adjustment called {name!r}. lla_list has them.")
	return frappe.get_doc(land.LOT_LINE_ADJUSTMENT, name)


def _checked(doc, tail: str) -> None:
	try:
		land.validate_adjustment(doc)
	except ToolError as exc:
		raise ToolError(f"{exc} {tail}") from None


# ── describing ──────────────────────────────────────────────────────────────
def _piece(row: dict) -> dict:
	out = {key: row.get(key) for key in PIECE_FIELDS}
	out["geometry"] = land.json_value(row.get("geometry"))
	out["row"] = row.get("name")
	return out


def describe(doc) -> dict:
	docstatus = int(doc.get("docstatus") or 0)
	status = doc.get("status") or land.DRAFT
	lots = {}
	for key in ("lot_1", "lot_2"):
		row = land.tax_lot_row(doc.get(key))
		lots[key] = land.describe_tax_lot(row) if row else None
	if docstatus == 0:
		next_statuses = [s for s in land.STATUSES if s not in (status, land.APPROVED, land.RECORDED)]
	elif docstatus == 1:
		next_statuses = list(land.SUBMITTED_NEXT.get(status, ()))
	else:
		next_statuses = []
	refusals = land.survey_refusals(doc)
	return {
		"name": doc.name,
		**{key: doc.get(key) for key in ("title", "status", "county", "state")},
		"docstatus": docstatus,
		"target_close": str(doc.get("target_close") or "") or None,
		**{
			key: doc.get(key)
			for key in HEADER_FIELDS
			if key not in ("title", "county", "state", "target_close")
		},
		"recorded_on": str(doc.get("recorded_on") or "") or None,
		"lots": lots,
		"pieces": [_piece(dict(row)) for row in doc.get("pieces") or []],
		"open_items": [
			{
				**{key: row.get(key) for key in OPEN_ITEM_FIELDS},
				"due": str(row.get("due") or "") or None,
				"row": row.get("name"),
			}
			for row in doc.get("open_items") or []
		],
		"easements": [
			{**{key: row.get(key) for key in EASEMENT_FIELDS}, "row": row.get("name")}
			for row in doc.get("easements") or []
		],
		**{key: doc.get(key) for key in SURVEY_STAMPS},
		"survey_recorded_on": str(doc.get("survey_recorded_on") or "") or None,
		"next_statuses": next_statuses,
		"editable_after_submit": sorted(land.after_submit_fields()),
		"missing_for_submission": land.submission_refusals(doc) if docstatus == 0 else [],
		"record_survey": {
			"available": not refusals and not doc.get("survey_recorded_on"),
			"already_recorded": bool(doc.get("survey_recorded_on")),
			"refusals": refusals,
		},
	}


def _result(doc, summary: str, delta: str = "", **extra) -> ToolResult:
	return ToolResult(data={**describe(doc), **extra}, summary=summary, docstatus_delta=delta)


# ── county tools ────────────────────────────────────────────────────────────
def taxlot_lookup(args: dict) -> ToolResult:
	"""Find tax lots at the county and cache them, or seed one by hand."""
	_require_doctypes(land.COUNTY_TAX_LOT)
	land.require_roles(land.TAX_LOT_ROLES, "look up county tax lots", "Nothing was written.")
	county_key, _config = land.county_config(as_str(args, "county"))
	map_taxlot = as_str(args, "map_taxlot")
	longitude, latitude = args.get("longitude"), args.get("latitude")
	point = None
	if longitude not in (None, "") or latitude not in (None, ""):
		if longitude in (None, "") or latitude in (None, ""):
			raise ToolError("longitude and latitude go together. Nothing was written.")
		point = (longitude, latitude)
	bbox = args.get("bbox") or None
	manual = args.get("manual")

	if manual not in (None, "", {}):
		if not map_taxlot or point or bbox:
			raise ToolError(
				"manual seeding needs map_taxlot and nothing else to search by. Nothing was written."
			)
		values = land.manual_values(map_taxlot, county_key, manual)
		name, created = land.upsert_tax_lot(values)
		row = land.tax_lot_row(name)
		return ToolResult(
			data={
				"county": county_key,
				"lots": [land.describe_tax_lot(row)],
				"created": [name] if created else [],
				"updated": [] if created else [name],
				"warnings": [
					"Seeded by hand (source Manual). Run taxlot_refresh when the county's GIS server "
					"answers, to replace it with the county's own record."
				],
			},
			summary=f"{'seeded' if created else 're-seeded'} County Tax Lot {name} by hand",
			docstatus_delta="none → 0 (created)" if created else "0 → 0 (updated)",
		)

	found, warnings, asked = land.fetch_tax_lots(county_key, map_taxlot or None, point, bbox)
	created, updated = [], []
	for values in found:
		name, was_created = land.upsert_tax_lot(values)
		(created if was_created else updated).append(name)
	if not found:
		warnings.append("The county matched no tax lot. Nothing was written.")
	lots = [land.describe_tax_lot(land.tax_lot_row(name)) for name in (*created, *updated)]
	return ToolResult(
		data={
			"county": county_key,
			"query": asked,
			"lots": lots,
			"created": created,
			"updated": updated,
			"warnings": warnings,
		},
		summary=f"{len(found)} tax lot(s) from the county: {len(created)} new, {len(updated)} refreshed",
		docstatus_delta="none → 0 (created)" if created else ("0 → 0 (updated)" if updated else ""),
	)


def taxlot_refresh(args: dict) -> ToolResult:
	"""Re-read one cached lot from the county. A failed read changes nothing."""
	_require_doctypes(land.COUNTY_TAX_LOT)
	land.require_roles(land.TAX_LOT_ROLES, "refresh county tax lots", "Nothing was written.")
	name = land.canonical_lot(as_str(args, "map_taxlot", required=True))
	before = land.tax_lot_row(name)
	if not before:
		raise ToolError(
			f"{name} is not in the cache. Look it up first with taxlot_lookup. Nothing was written."
		)
	found, warnings, asked = land.fetch_tax_lots(before.get("county") or None, map_taxlot=name)
	match = next((values for values in found if values["map_taxlot"] == name), None)
	if match is None:
		warnings.append(f"The county no longer returns {name}. The cached row is unchanged.")
		return ToolResult(
			data={
				"map_taxlot": name,
				"refreshed": False,
				"lot": land.describe_tax_lot(before),
				"warnings": warnings,
			},
			summary=f"{name}: the county returned nothing; cache unchanged",
		)
	land.upsert_tax_lot(match)
	after = land.describe_tax_lot(land.tax_lot_row(name))
	old = land.describe_tax_lot(before)
	changed = sorted(
		key
		for key in ("account", "owner_of_record", "situs", "acres_gis", "geometry", "source")
		if old.get(key) != after.get(key)
	)
	return ToolResult(
		data={
			"map_taxlot": name,
			"refreshed": True,
			"changed": changed,
			"lot": after,
			"query": asked,
			"warnings": warnings,
		},
		summary=f"{name} refreshed from the county"
		+ (f"; changed {', '.join(changed)}" if changed else "; nothing changed"),
		docstatus_delta="0 → 0 (updated)",
	)


# ── reading adjustments ─────────────────────────────────────────────────────
def lla_list(args: dict) -> ToolResult:
	_require_doctypes(land.LOT_LINE_ADJUSTMENT)
	land.require_roles(land.AGREEMENT_ROLES, "read lot line adjustments", "Nothing was read.")
	filters = {}
	status = as_str(args, "status")
	if status:
		if status not in land.STATUSES:
			raise ToolError(f"status must be one of {', '.join(land.STATUSES)}, got {status!r}.")
		filters["status"] = status
	if as_str(args, "county"):
		filters["county"] = as_str(args, "county")
	rows = (
		frappe.db.get_all(
			land.LOT_LINE_ADJUSTMENT,
			filters=filters,
			fields=[
				"name",
				"title",
				"status",
				"docstatus",
				"county",
				"party_1",
				"party_2",
				"lot_1",
				"lot_2",
				"target_close",
				"survey_recorded_on",
			],
			order_by="modified desc",
			limit=as_limit(args),
		)
		or []
	)
	party = as_str(args, "party")
	out = []
	for row in rows:
		row = dict(row)
		if party and party not in (row.get("party_1"), row.get("party_2")):
			continue
		doc = frappe.get_doc(land.LOT_LINE_ADJUSTMENT, row["name"])
		open_items = [
			item
			for item in doc.get("open_items") or []
			if (item.get("status") or "Open") in ("Open", "In progress")
		]
		out.append(
			{
				**row,
				"target_close": str(row.get("target_close") or "") or None,
				"survey_recorded_on": str(row.get("survey_recorded_on") or "") or None,
				"piece_count": len(doc.get("pieces") or []),
				"open_item_count": len(open_items),
			}
		)
	return ToolResult(
		data={"adjustments": out, "count": len(out)}, summary=f"{len(out)} lot line adjustment(s)"
	)


def lla_get(args: dict) -> ToolResult:
	_require_doctypes(land.LOT_LINE_ADJUSTMENT)
	land.require_roles(land.AGREEMENT_ROLES, "read lot line adjustments", "Nothing was read.")
	doc = _load(args)
	return _result(doc, f"{doc.name}: {doc.get('title')} ({doc.get('status')})")


# ── writing adjustments ─────────────────────────────────────────────────────
def _coerce(key: str, value, doc=None):
	if value in (None, ""):
		return None
	if key in NUMBER_FIELDS:
		number = as_float(value, key)
		if number < 0:
			raise ToolError(f"{key} cannot be negative, got {number:g}.")
		return number
	if key in DATE_FIELDS:
		return as_date({key: value}, key)
	if key == "true_up_payer":
		return (
			land.side_of_party(doc, value, "true_up_payer")
			if doc is not None
			else land.side(value, "true_up_payer")
		)
	if key in ("lot_1", "lot_2"):
		return land.canonical_lot(value)
	return str(value).strip()


def _rows(value, label: str) -> list:
	if value in (None, ""):
		return []
	if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
		raise ToolError(f"{label} must be a list of objects.")
	return value


def _piece_values(doc, row: dict, tail: str) -> dict:
	unknown = sorted(set(row) - set(PIECE_FIELDS) - {"name"})
	if unknown:
		raise ToolError(
			f"a piece does not take {', '.join(unknown)}. It takes {', '.join(PIECE_FIELDS)}. {tail}"
		)
	values = {}
	if "name" in row and "piece_name" not in row:
		row = {**row, "piece_name": row["name"]}
	for key in PIECE_FIELDS:
		if key not in row:
			continue
		value = row[key]
		if key in ("from_party", "to_party"):
			values[key] = land.side_of_party(doc, value, key) if value not in (None, "") else None
		elif key in ("acres_gis", "acres_surveyed"):
			values[key] = None if value in (None, "") else as_float(value, key)
		elif key == "geometry":
			values[key] = land.dump(land.polygon(value, "geometry")) if value not in (None, "") else None
		else:
			values[key] = str(value).strip() if value not in (None, "") else None
	return values


def _open_item_values(row: dict, tail: str) -> dict:
	if "owner" in row and "responsible" not in row:
		row = {**{k: v for k, v in row.items() if k != "owner"}, "responsible": row["owner"]}
	unknown = sorted(set(row) - set(OPEN_ITEM_FIELDS))
	if unknown:
		raise ToolError(
			f"an open item does not take {', '.join(unknown)}. It takes item, question, owner, status, due. {tail}"
		)
	values = {}
	for key in OPEN_ITEM_FIELDS:
		if key not in row:
			continue
		value = row[key]
		if key == "status" and value not in (None, ""):
			if value not in land.OPEN_ITEM_STATUSES:
				raise ToolError(
					f"open item status must be one of {', '.join(land.OPEN_ITEM_STATUSES)}, got {value!r}. {tail}"
				)
		if key == "due":
			values[key] = as_date({"due": value}, "due") if value not in (None, "") else None
		else:
			values[key] = str(value).strip() if value not in (None, "") else None
	return values


def _easement_values(doc, row: dict, tail: str) -> dict:
	if "type" in row and "easement_type" not in row:
		row = {**{k: v for k, v in row.items() if k != "type"}, "easement_type": row["type"]}
	unknown = sorted(set(row) - set(EASEMENT_FIELDS))
	if unknown:
		raise ToolError(
			f"an easement does not take {', '.join(unknown)}. It takes type, burdened, benefited, notes. {tail}"
		)
	kind = str(row.get("easement_type") or "").strip().capitalize()
	if kind not in land.EASEMENT_TYPES:
		raise ToolError(
			f"easement type must be access, utility or irrigation, got {row.get('easement_type')!r}. {tail}"
		)
	values = {"easement_type": kind, "notes": str(row.get("notes") or "").strip() or None}
	for key in ("burdened", "benefited"):
		values[key] = land.side_of_party(doc, row[key], key) if row.get(key) not in (None, "") else None
	if values["burdened"] and values["burdened"] == values["benefited"]:
		raise ToolError(f"an easement's burdened and benefited ground are both {values['burdened']}. {tail}")
	return values


def lla_create(args: dict) -> ToolResult:
	tail = "Nothing was created."
	_agreement_actor(tail)
	status = as_str(args, "status") or land.DRAFT
	if status not in land.DRAFT_STATUSES:
		raise ToolError(
			f"a new adjustment starts as Draft or Under review, not {status!r}; submit it with lla_update. {tail}"
		)
	unknown = sorted(set(args) - set(HEADER_FIELDS) - {"status", "pieces", "open_items", "easements"})
	if unknown:
		raise ToolError(f"lla_create does not take {', '.join(unknown)}. {tail}")
	doc = frappe.new_doc(land.LOT_LINE_ADJUSTMENT)
	doc.status = status
	doc.county = "Wasco"
	doc.state = "OR"
	doc.consideration = "Even swap"
	doc.party_1_type = "Company"
	doc.party_2_type = "Company"
	for key in HEADER_FIELDS:
		if key in args and key != "true_up_payer":
			doc.set(key, _coerce(key, args[key]))
	if "true_up_payer" in args:
		doc.set("true_up_payer", _coerce("true_up_payer", args["true_up_payer"], doc))
	if not doc.get("title"):
		raise ToolError(f"title is required. {tail}")
	# The parties first: every piece and easement names its side by them.
	try:
		land.check_parties(doc)
	except ToolError as exc:
		raise ToolError(f"{exc} {tail}") from None
	for row in _rows(args.get("pieces"), "pieces"):
		doc.append("pieces", _piece_values(doc, row, tail))
	for row in _rows(args.get("open_items"), "open_items"):
		values = _open_item_values(row, tail)
		values.setdefault("status", "Open")
		doc.append("open_items", values)
	for row in _rows(args.get("easements"), "easements"):
		doc.append("easements", _easement_values(doc, row, tail))
	for index, row in enumerate(doc.get("pieces") or [], start=1):
		if not row.get("piece_name"):
			raise ToolError(f"piece {index} has no name. {tail}")
	for row in doc.get("open_items") or []:
		if not row.get("item"):
			raise ToolError(f"every open item needs item. {tail}")
	_checked(doc, tail)
	doc.insert(ignore_permissions=True)
	return _result(doc, f"created {doc.name}: {doc.get('title')}", "none → 0 (created)")


def _transition(doc, status: str, tail: str) -> str:
	"""Apply a status change. Returns the docstatus delta it caused."""
	current = doc.get("status") or land.DRAFT
	docstatus = int(doc.get("docstatus") or 0)
	if status not in land.STATUSES:
		raise ToolError(f"status must be one of {', '.join(land.STATUSES)}, got {status!r}. {tail}")
	if status == current:
		return ""
	if docstatus == 2:
		raise ToolError(f"{doc.name} is cancelled ({current}). Amend it into a new adjustment. {tail}")
	if docstatus == 0:
		if status in land.DRAFT_STATUSES:
			doc.status = status
			return ""
		if status == land.WITHDRAWN:
			doc.status = status
			return ""
		if status == land.SUBMITTED:
			missing = land.submission_refusals(doc)
			if missing:
				raise ToolError(
					f"{doc.name} cannot go to the county yet: it has no {', '.join(missing)}. {tail}"
				)
			doc.status = status
			return "submit"
		raise ToolError(
			f"{doc.name} is a draft; it goes to {land.SUBMITTED} before it can be {status}. {tail}"
		)
	allowed = land.SUBMITTED_NEXT.get(current, ())
	if status not in allowed:
		raise ToolError(
			f"{doc.name} is {current}; from there it can go to {' or '.join(allowed) or 'nothing further'}, not {status}. {tail}"
		)
	if status == land.WITHDRAWN:
		doc.status = status
		return "cancel"
	doc.status = status
	return ""


def lla_update(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	_agreement_actor(tail)
	doc = _load(args)
	fields = args.get("fields")
	if not isinstance(fields, dict) or not fields:
		raise ToolError(f"fields must be an object of the header fields to change. {tail}")
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 2:
		raise ToolError(f"{doc.name} is cancelled and cannot be edited. {tail}")
	tables = sorted({"pieces", "open_items", "easements"} & set(fields))
	if tables:
		raise ToolError(
			f"lla_update does not edit {', '.join(tables)}: use lla_add_piece, lla_set_geometry, "
			f"lla_add_open_item and lla_add_easement. {tail}"
		)
	stamps = sorted(set(SURVEY_STAMPS) & set(fields))
	if stamps:
		raise ToolError(f"{', '.join(stamps)} are written by lla_record_survey only. {tail}")
	unknown = sorted(set(fields) - set(HEADER_FIELDS) - {"status"})
	if unknown:
		raise ToolError(f"{', '.join(unknown)} are not Lot Line Adjustment fields this tool sets. {tail}")
	if docstatus == 1:
		locked = sorted(key for key in fields if key not in land.after_submit_fields())
		if locked:
			raise ToolError(
				f"{doc.name} is submitted, which fixes {', '.join(locked)}. After submission only "
				f"{', '.join(sorted(land.after_submit_fields() - {'pieces', 'open_items'} - set(SURVEY_STAMPS)))} "
				f"change here. Withdraw it and amend a new draft to change the terms. {tail}"
			)
	changes = {}
	for key, value in fields.items():
		if key == "status":
			continue
		wanted = _coerce(key, value, doc)
		before = doc.get(key)
		if str(before if before is not None else "") != str(wanted if wanted is not None else ""):
			changes[key] = [before, wanted]
			doc.set(key, wanted)
	action = ""
	if "status" in fields:
		before = doc.get("status")
		action = _transition(doc, str(fields["status"] or "").strip(), tail)
		if doc.get("status") != before:
			changes["status"] = [before, doc.get("status")]
	if not changes:
		raise ToolError(f"nothing to change on {doc.name}: every value given is already set. {tail}")
	_checked(doc, tail)
	if action == "submit":
		doc.submit()
		delta = "0 → 1 (submitted)"
	elif action == "cancel":
		doc.cancel()
		delta = "1 → 2 (cancelled)"
	else:
		doc.save(ignore_permissions=True)
		delta = f"{docstatus} → {docstatus} (updated)"
	return _result(
		doc,
		f"{doc.name}: " + ", ".join(f"{k} → {v[1]!r}" for k, v in changes.items()),
		delta,
		changed=changes,
	)


def _find_row(doc, table: str, key: str, value: str):
	for row in doc.get(table) or []:
		if str(row.get(key) or "").strip().lower() == str(value or "").strip().lower():
			return row
	return None


def lla_add_piece(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	_agreement_actor(tail)
	doc = _load(args)
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 2:
		raise ToolError(f"{doc.name} is cancelled. {tail}")
	payload = {key: args[key] for key in PIECE_FIELDS if key in args}
	if "piece" in args and "piece_name" not in payload:
		payload["piece_name"] = args["piece"]
	values = _piece_values(doc, payload, tail)
	if not values.get("piece_name"):
		raise ToolError(f"piece_name is required. {tail}")
	existing = _find_row(doc, "pieces", "piece_name", values["piece_name"])
	if docstatus == 1:
		if existing is None:
			raise ToolError(f"{doc.name} is submitted; no piece can be added to it. {tail}")
		locked = sorted(key for key in values if key not in ("piece_name", "acres_surveyed", "geometry"))
		if locked:
			raise ToolError(
				f"{doc.name} is submitted; only acres_surveyed and geometry change on a piece, not {', '.join(locked)}. {tail}"
			)
	if existing is None:
		for key in ("from_party", "to_party"):
			if not values.get(key):
				raise ToolError(f"a new piece needs {key} (Party 1, Party 2, or the party's name). {tail}")
		doc.append("pieces", values)
		verb = "added"
	else:
		for key, value in values.items():
			if key != "piece_name":
				existing[key] = value
		verb = "updated"
	_checked(doc, tail)
	doc.save(ignore_permissions=True)
	return _result(
		doc, f"{doc.name}: {verb} piece {values['piece_name']}", f"{docstatus} → {docstatus} (updated)"
	)


def lla_set_geometry(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	_agreement_actor(tail)
	doc = _load(args)
	if int(doc.get("docstatus") or 0) == 2:
		raise ToolError(f"{doc.name} is cancelled. {tail}")
	piece = as_str(args, "piece", required=True)
	row = _find_row(doc, "pieces", "piece_name", piece)
	if row is None:
		names = ", ".join(str(r.get("piece_name")) for r in doc.get("pieces") or []) or "none"
		raise ToolError(f"{doc.name} has no piece called {piece!r} (pieces: {names}). {tail}")
	geometry = land.polygon(args.get("geojson"), "geojson")
	row["geometry"] = land.dump(geometry)
	computed = geo.area_acres(geometry)
	# Stored Float columns: an unset one reads 0, so 0 here means "no figure yet".
	surveyed = float(row.get("acres_surveyed") or 0)
	stated = surveyed if surveyed > 0 else float(row.get("acres_gis") or 0)
	warnings = list(geo.check_coordinates_look_like_degrees(geometry, "geojson"))
	if stated:
		ratio, verdict = geo.area_disagreement(stated, computed)
		if verdict:
			warnings.append(
				f"The polygon encloses {computed} acres against {stated:g} acres on the piece — {round(ratio * 100, 1)}%. "
				"Check the geometry before the survey is recorded; Record Survey hands this shape to set_parcel_boundary, which refuses a quarter's disagreement."
			)
	doc.save(ignore_permissions=True)
	docstatus = int(doc.get("docstatus") or 0)
	return _result(
		doc,
		f"{doc.name}: set the geometry of {row.get('piece_name')} ({computed} ac computed)",
		f"{docstatus} → {docstatus} (updated)",
		geometry_area_computed_acres=computed,
		warnings=warnings,
	)


def lla_add_open_item(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	_agreement_actor(tail)
	doc = _load(args)
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 2:
		raise ToolError(f"{doc.name} is cancelled. {tail}")
	payload = {key: args[key] for key in (*OPEN_ITEM_FIELDS, "owner") if key in args}
	values = _open_item_values(payload, tail)
	if not values.get("item"):
		raise ToolError(f"item is required. {tail}")
	existing = _find_row(doc, "open_items", "item", values["item"])
	if existing is None:
		values.setdefault("status", "Open")
		doc.append("open_items", values)
		verb = "added"
	else:
		for key, value in values.items():
			if key != "item":
				existing[key] = value
		verb = "updated"
	doc.save(ignore_permissions=True)
	return _result(
		doc, f"{doc.name}: {verb} open item {values['item']}", f"{docstatus} → {docstatus} (updated)"
	)


def lla_add_easement(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	_agreement_actor(tail)
	doc = _load(args)
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus != 0:
		raise ToolError(
			f"{doc.name} is {'submitted' if docstatus == 1 else 'cancelled'}; easements are part of the terms and are fixed. {tail}"
		)
	payload = {key: args[key] for key in (*EASEMENT_FIELDS, "type") if key in args}
	doc.append("easements", _easement_values(doc, payload, tail))
	doc.save(ignore_permissions=True)
	return _result(
		doc,
		f"{doc.name}: added a {payload.get('type') or payload.get('easement_type')} easement",
		"0 → 0 (updated)",
	)


def lla_render_mou(args: dict) -> ToolResult:
	_require_doctypes(land.LOT_LINE_ADJUSTMENT)
	land.require_roles(land.AGREEMENT_ROLES, "print a memorandum of understanding", "Nothing was rendered.")
	doc = _load(args)
	rendered = mou.render(doc)
	data = {
		"name": doc.name,
		"title": doc.get("title"),
		"print_format": mou.FORMAT_NAME,
		"renderer": rendered["renderer"],
		"file_name": f"{doc.name} Memorandum of Understanding.pdf",
		"pdf_bytes": len(rendered["pdf"]),
		"pdf_base64": mou.pdf_base64(rendered["pdf"]),
		"html": rendered["html"] if as_bool(args, "include_html", False) else None,
		"note": rendered["note"] or None,
	}
	return ToolResult(
		data=data, summary=f"Memorandum of Understanding for {doc.name}: {len(rendered['pdf'])} byte PDF"
	)


# ── the button ──────────────────────────────────────────────────────────────
def lla_record_survey(args: dict) -> ToolResult:
	"""Write the adjusted Parcels from the recorded survey. The one path into the books."""
	tail = "Nothing was changed."
	_require_doctypes(land.LOT_LINE_ADJUSTMENT, land.PARCEL)
	actor = land.require_roles(land.RECORD_SURVEY_ROLES, "record a lot line adjustment's survey", tail)
	doc = _load(args)
	refusals = land.survey_refusals(doc)
	if refusals:
		raise ToolError(f"Record Survey cannot run on {doc.name}: {'; '.join(refusals)}. {tail}")

	plans = [land.side_plan(doc, which) for which in land.SIDES]
	on_books = [plan for plan in plans if plan["party_type"] == "Company"]
	if not on_books:
		raise ToolError(
			f"neither party on {doc.name} is a Company on this site, so there is no Parcel here to write. {tail}"
		)
	for plan in on_books:
		if plan["acres_after"] is None:
			index = 1 if plan["side"] == land.PARTY_1 else 2
			raise ToolError(
				f"{plan['side']}'s lot {plan['lot']} has no county acreage and lot_{index}_acres_surveyed is blank, "
				f"so the adjusted acreage is unknown. Set lot_{index}_acres_surveyed from the survey. {tail}"
			)
		if plan["acres_after"] <= 0:
			raise ToolError(
				f"{plan['side']}'s adjusted acreage comes to {plan['acres_after']:g}. Check the surveyed pieces. {tail}"
			)
		if plan.get("geometry_after"):
			ratio, verdict = geo.area_disagreement(plan["acres_after"], plan["area_after_computed_acres"])
			if verdict == "refuse":
				raise ToolError(
					f"{plan['side']}'s adjusted polygon encloses {plan['area_after_computed_acres']} acres against "
					f"{plan['acres_after']:g} acres after the survey — {round(ratio * 100, 1)}%. One of the piece "
					f"geometries or the acreages is wrong. {tail}"
				)

	written = []
	for plan in plans:
		index = 1 if plan["side"] == land.PARTY_1 else 2
		if plan["party_type"] != "Company":
			written.append(
				{
					"side": plan["side"],
					"party": plan["party"],
					"parcel": None,
					"skipped": f"{plan['party']} is a {plan['party_type']}, not a Company on this site; its ground is not on these books.",
					"acres_after": plan["acres_after"],
					"acreage_basis": plan["acreage_basis"],
				}
			)
			continue
		company = plan["party"]
		lot = land.tax_lot_row(plan["lot"]) or {}
		parcel = doc.get(f"parcel_{index}")
		if parcel and not frappe.db.exists(land.PARCEL, parcel):
			parcel = None
		parcel = parcel or land.matching_parcel(company, plan["lot"])
		action = "updated"
		if not parcel:
			created = realestate.create_parcel(
				{
					"company": company,
					"parcel_name": f"Tax Lot {plan['lot']}",
					"parcel_id": plan["lot"],
					"county": doc.get("county") or "",
					"state": doc.get("state") or "",
					"address": lot.get("situs") or "",
					"acreage": plan["acres_after"],
				}
			)
			parcel = created.data["name"]
			action = "created"
		else:
			current = realestate.parcel_row(parcel, company)
			update = {"parcel": parcel}
			if abs(float(current.get("acreage") or 0) - plan["acres_after"]) > 1e-9:
				update["acreage"] = plan["acres_after"]
			if not current.get("parcel_id"):
				update["parcel_id"] = plan["lot"]
			if len(update) > 1:
				realestate.update_parcel(update)
			else:
				action = "unchanged"
		boundary = None
		if plan.get("geometry_after"):
			boundary = realestate.set_parcel_boundary(
				{"parcel": parcel, "boundary_geojson": plan["geometry_after"]}
			).data
			if action == "unchanged":
				action = "boundary set"
		doc.set(f"parcel_{index}", parcel)
		written.append(
			{
				"side": plan["side"],
				"party": company,
				"parcel": parcel,
				"action": action,
				"acres_after": plan["acres_after"],
				"acreage_basis": plan["acreage_basis"],
				"pieces_given": plan["pieces_given"],
				"pieces_received": plan["pieces_received"],
				"boundary_area_computed_acres": (boundary or {}).get("area_computed_acres"),
				"warnings": plan["warnings"] + list((boundary or {}).get("warnings") or []),
			}
		)

	doc.survey_recorded_on = str(frappe.utils.now())
	doc.survey_recorded_by = actor if frappe.db.exists("User", actor) else None
	doc.save(ignore_permissions=True)
	parcels = ", ".join(
		f"{entry['parcel']} ({entry['acres_after']:g} ac)" for entry in written if entry.get("parcel")
	)
	return _result(
		doc,
		f"{doc.name}: survey recorded onto {parcels}",
		"1 → 1 (survey recorded)",
		survey=written,
	)
