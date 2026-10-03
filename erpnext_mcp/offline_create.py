# SPDX-License-Identifier: MIT
"""Records made on a phone with no signal, sent later. v0.226.0.

docs/design/offline_add_asset.md §1.1, §1.4, §1.6, §1.7. Two registers take an
offline create — the Asset Register and Housing Unit — and both go through this
module, so the rules are the same for a pump and a cabin:

* THE TAG IS A UUID THE PHONE MINTED (`tag_uuid`). It is printed or shown before
  the server has heard of the record, so the server keeps it and never changes
  it; `/scan/<uuid>` and `universal_scan` resolve it. A tag another record
  already carries is refused (`tag_in_use`), and a tag a person said was "the
  same one" lives on that record's `tag_aliases`.
* SAME REQUEST, SAME RESULT (`request_id`). A phone whose reply was lost sends
  the request again; the record it already made is answered with
  `replayed: true` and nothing new is written.
* NOTHING IS MERGED BY A MACHINE. A likely duplicate (same serial, or the same
  name in the same place) creates nothing and comes back as
  `possible_duplicate` with the candidates; the person on the phone says Keep as
  new (`confirm_new`) or It's the same one (`link_to_existing`).
* LOGGED. `created_offline`, `device_created_at`, `created_device` on the record,
  one comment on it and one audit row.

Nothing here runs unless a request carries `tag_uuid` or `request_id`: a phone
that does not send them, and every MCP caller, get the old behaviour.
"""

from __future__ import annotations

import re

import frappe

from . import audit, compat, datetimes, timezones
from .errors import ToolError

ASSET_REGISTER = "Asset Register"
HOUSING_UNIT = "Housing Unit"
REGISTERS = (ASSET_REGISTER, HOUSING_UNIT)

#: The columns this release adds to both registers (patch-free: they are in the
#: doctype JSON). `tag_uuid` is unique.
FIELDS = (
	"tag_uuid",
	"tag_aliases",
	"request_id",
	"created_offline",
	"device_created_at",
	"created_device",
	"needs_review",
	"review_note",
)

#: The arguments a create route accepts for this, forwarded unchanged to the tool.
ARGUMENTS = (
	"tag_uuid",
	"request_id",
	"device_created_at",
	"offline",
	"created_device",
	"review_note",
	"confirm_new",
	"link_to_existing",
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

POSSIBLE_DUPLICATE = "possible_duplicate"


def is_uuid(value) -> bool:
	return bool(_UUID.match(str(value or "").strip().lower()))


def _truthy(value) -> bool:
	return str(value).strip().lower() in ("1", "true", "yes", "on") if value is not None else False


def options(args: dict) -> dict:
	"""The offline arguments of one request, checked. `active` is False when none were sent."""
	tag = str(args.get("tag_uuid") or "").strip().lower()
	if tag and not is_uuid(tag):
		raise ToolError(
			f"tag_uuid {tag[:40]!r} is not a UUID. The phone mints one per new record "
			"(xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx). Nothing was created.",
			"error.asset.bad_tag",
		)
	request_id = str(args.get("request_id") or "").strip()
	if request_id and not 8 <= len(request_id) <= 64:
		raise ToolError("request_id is 8 to 64 characters (a UUID). Nothing was created.")
	stamp = ""
	raw_time = args.get("device_created_at")
	if raw_time not in (None, ""):
		stamp = datetimes.as_site_datetime(raw_time, timezones.site_timezone()[0])
	return {
		"active": bool(tag or request_id),
		"tag_uuid": tag,
		"request_id": request_id,
		"device_created_at": stamp,
		"offline": _truthy(args.get("offline")),
		"created_device": str(args.get("created_device") or "").strip()[:140],
		"review_note": str(args.get("review_note") or "").strip()[:500],
		"confirm_new": _truthy(args.get("confirm_new")),
		"link_to_existing": str(args.get("link_to_existing") or "").strip(),
	}


def ready(doctype: str) -> bool:
	return compat.has_field(doctype, "tag_uuid")


def require_ready(doctype: str) -> None:
	if not ready(doctype):
		raise ToolError(
			f"{doctype} has no tag_uuid column on this site yet — it ships with erpnext_mcp v0.226.0; "
			"run `bench migrate`. Nothing was created."
		)


# ── lookups ─────────────────────────────────────────────────────────────────
def replayed(doctype: str, opts: dict) -> str:
	"""The record this request already made, or ""."""
	if not opts.get("request_id") or not compat.has_field(doctype, "request_id"):
		return ""
	return str(frappe.db.get_value(doctype, {"request_id": opts["request_id"]}, "name") or "")


def _aliases(value) -> list:
	return [line.strip().lower() for line in str(value or "").replace(",", "\n").splitlines() if line.strip()]


def resolve(tag) -> tuple:
	"""`(doctype, docname)` for a tag UUID — its own, or an alias — else `("", "")`."""
	text = str(tag or "").strip().lower()
	if not is_uuid(text):
		return "", ""
	for doctype in REGISTERS:
		if not ready(doctype):
			continue
		name = frappe.db.get_value(doctype, {"tag_uuid": text}, "name")
		if name:
			return doctype, str(name)
	for doctype in REGISTERS:
		if not compat.has_field(doctype, "tag_aliases"):
			continue
		rows = frappe.db.get_all(
			doctype, filters={"tag_aliases": ("like", f"%{text}%")}, fields=["name", "tag_aliases"], limit=5
		)
		for row in rows or []:
			if text in _aliases(row.get("tag_aliases")):
				return doctype, str(row["name"])
	return "", ""


def claim_tag(opts: dict) -> None:
	"""Refuse a tag another record already carries (the replay was answered before this)."""
	tag = opts.get("tag_uuid")
	if not tag:
		return
	doctype, name = resolve(tag)
	if name:
		raise ToolError(
			f"tag {tag} is already on {doctype} {name}. A tag is one record's for good — the phone "
			"should mint a new one for a new record. Nothing was created.",
			"error.asset.tag_in_use",
		)


def candidates(doctype: str, rows: list) -> dict:
	return {
		"created": False,
		"outcome": POSSIBLE_DUPLICATE,
		"doctype": doctype,
		"candidates": rows,
		"note": (
			"This looks like something already on the register. Nothing was created. Choose Keep as new "
			"(send again with confirm_new) or It's the same one (send link_to_existing with the record's "
			"name — the tag is added to it and the photos go to it)."
		),
	}


def asset_duplicates(name: str, location: str, serial: str, company: str) -> list:
	"""Asset Register rows with the same serial, or the same name in the same place."""
	fields = compat.existing_fields(
		ASSET_REGISTER, ("name", "asset_type", "location", "serial_number", "company", "description")
	)
	found: dict = {}
	if serial and compat.has_field(ASSET_REGISTER, "serial_number"):
		filters = {"serial_number": serial}
		if company:
			filters["company"] = company
		for row in frappe.db.get_all(ASSET_REGISTER, filters=filters, fields=fields, limit=10) or []:
			found[row["name"]] = {**dict(row), "why": "same serial number"}
	if name and frappe.db.exists(ASSET_REGISTER, name):
		row = dict(frappe.db.get_value(ASSET_REGISTER, name, fields, as_dict=True) or {})
		row["name"] = name
		if str(row.get("location") or "") == str(location or ""):
			found.setdefault(name, {**row, "why": "same name in the same place"})
	return list(found.values())


# ── writes ──────────────────────────────────────────────────────────────────
def stamp(doc, doctype: str, opts: dict, needs_review: str = "") -> None:
	"""The offline columns on a new record, before insert."""
	if not opts.get("active"):
		return
	values = {
		"tag_uuid": opts.get("tag_uuid") or None,
		"request_id": opts.get("request_id") or None,
		"created_offline": 1 if opts.get("offline") else 0,
		"device_created_at": opts.get("device_created_at") or None,
		"created_device": opts.get("created_device") or None,
	}
	notes = [part for part in (opts.get("review_note"), needs_review) if part]
	if notes:
		values["needs_review"] = 1
		values["review_note"] = "\n".join(notes)[:1000]
	for key, value in values.items():
		if compat.has_field(doctype, key):
			doc.set(key, value)


def record(doctype: str, name: str, opts: dict, verb: str = "Created") -> None:
	"""One comment on the record and one audit row. Never raises."""
	if not opts.get("active"):
		return
	when = opts.get("device_created_at") or "an unknown time"
	device = opts.get("created_device") or "a phone"
	text = (
		f"{verb} {'offline ' if opts.get('offline') else ''}on {device} at {when} (phone time); "
		f"tag {opts.get('tag_uuid') or '—'}, request {opts.get('request_id') or '—'}."
	)
	try:
		frappe.get_doc(doctype, name).add_comment("Comment", text)
	except Exception:  # pragma: no cover - a comment is not worth losing the record over
		pass
	audit.record(
		"offline_create",
		{
			"doctype": doctype,
			"name": name,
			"tag_uuid": opts.get("tag_uuid"),
			"request_id": opts.get("request_id"),
		},
		audit.STATUS_SUCCESS,
		text[:500],
		commit=False,
	)


def link(doctype: str, target: str, opts: dict, company: str = "") -> dict:
	"""It's the same one: this tag becomes an alias of `target`. Idempotent."""
	require_ready(doctype)
	if not frappe.db.exists(doctype, target):
		raise ToolError(f"no {doctype} called {target!r} to link this tag to. Nothing was changed.")
	if company and compat.has_field(doctype, "company"):
		owner = frappe.db.get_value(doctype, target, "company")
		if owner and owner != company:
			raise ToolError(f"no {doctype} called {target!r} to link this tag to. Nothing was changed.")
	tag = opts.get("tag_uuid")
	if not tag:
		raise ToolError("link_to_existing needs the tag_uuid being linked. Nothing was changed.")
	held_doctype, held = resolve(tag)
	if held and (held_doctype, held) != (doctype, target):
		raise ToolError(
			f"tag {tag} is already on {held_doctype} {held}. Nothing was changed.", "error.asset.tag_in_use"
		)
	already = bool(held)
	if not already:
		current = _aliases(frappe.db.get_value(doctype, target, "tag_aliases"))
		current.append(tag)
		frappe.db.set_value(doctype, target, "tag_aliases", "\n".join(current), update_modified=False)
		record(doctype, target, opts, verb="Tag linked")
	return {
		"created": False,
		"outcome": "linked",
		"replayed": already,
		"doctype": doctype,
		"name": target,
		"tag_uuid": tag,
	}
