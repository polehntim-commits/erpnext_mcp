# SPDX-License-Identifier: MIT
"""Universal Asset Tags: scan it, see its history, log what happened.

Every reportable asset on the farm — a valve, a sprayer, a cabin, a cold storage
unit — gets a durable ID tag (QR and optional NFC) so a worker can scan it and
see what it is, what has happened to it, and what is due. The tag is the
docname, and the docname is the printable ID.

v0.25.0 adds state-change actions: a worker scans an asset and picks an action
(open a valve, fill a sprayer tank, winterize a cabin). The state machine
validates transitions, updates the asset's current_state, and logs the event.

WHY THE HISTORY IS CROSS-DOCTYPE. An irrigation valve's history includes water
tests, inspection sessions, compliance alerts, and farm tasks — all of them
pointing at the same asset name through different link fields. Pulling from one
doctype would be a timeline with gaps; pulling from all of them is the timeline
a worker standing in front of the valve actually needs.
"""

import base64
import json

import frappe

from .. import asset_mirror, compat, timezones
from ..args import as_bool, as_date, as_float, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..render import qr
from ..result import ToolResult

ASSET_REGISTER = "Asset Register"
ASSET_STATE_LOG = "Asset State Log"
IRRIGATION_ZONE = "Irrigation Zone"

REGISTER_CAP = 500

_ASSET_FIELDS = (
	"name",
	"asset_type",
	"company",
	"location",
	"qr_url",
	"nfc_uid",
	"current_state",
	"last_scan_at",
	"last_scan_by",
	"retired_at",
	"description",
	# v0.77.0 — what an insurance schedule asks for. Read on every describe
	# rather than fetched by the export alone, so a scan at the machine shows
	# the same serial an adjuster is holding.
	"serial_number",
	"model",
	"acquired_on",
	"purchase_value",
	"replacement_value",
	"gps_latitude",
	"gps_longitude",
	# v0.78.0 — the service schedule, the hour meter and the zone link. Read on
	# every describe rather than fetched by the maintenance tools alone, so a
	# scan at the machine shows the same figures `check_maintenance_due` and
	# `get_water_usage_report` are computing from.
	"service_interval_hours",
	"service_interval_days",
	"last_service_date",
	"last_service_hours",
	"current_hours",
	"hours_updated_at",
	"irrigation_zone",
	"creation",
	"owner",
)

ASSET_TYPES = (
	"Housing Unit",
	"Irrigation Zone",
	"Irrigation Valve",
	"Sprayer",
	"Tractor",
	"Implement",
	"Vehicle",
	"Block",
	"Water Source",
	"Storage",
	"Cold Storage",
	"General",
)

_HISTORY_DOCTYPES = (
	("Farm Task", "asset_register", "name,task_type,status,priority,assigned_to,creation"),
	("Farm Task", "asset", "name,task_type,state,urgency,assigned_to,origin,creation"),
	("Housing Inspection", "housing_unit", "name,unit,inspector,inspection_date,creation"),
	("Detector Test", "housing_unit", "name,unit,test_type,result,test_date,creation"),
	("Water Test", "water_source", "name,source_name,test_type,result,test_date,creation"),
	("Inspection Session", "location", "name,template,status,creation"),
	("Compliance Alert", "asset_register", "name,alert_type,severity,status,creation"),
	("Asset State Log", "asset_name", "name,action,from_state,to_state,performed_by,performed_at,creation"),
)

ASSET_TYPE_SKILL_MAP: dict[str, str] = {
	"Housing Unit": "camp_maintenance",
	"Irrigation Valve": "irrigation",
	"Irrigation Zone": "irrigation",
	"Sprayer": "equipment_maintenance",
	"Tractor": "equipment_maintenance",
	"Implement": "equipment_maintenance",
	"Vehicle": "equipment_maintenance",
	"Water Source": "water_management",
	"Storage": "facility_maintenance",
	"Cold Storage": "facility_maintenance",
	"Block": "field_operations",
	"General": "general_maintenance",
}


def _require() -> None:
	compat.require_doctype(
		ASSET_REGISTER,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _company(args: dict, required: bool = False) -> str | None:
	requested = as_str(args, "company")
	return resolve_company(requested, required=required)


def _parse_state(value) -> dict | None:
	if not value:
		return None
	if isinstance(value, dict):
		return value
	try:
		parsed = json.loads(str(value))
		return parsed if isinstance(parsed, dict) else None
	except (json.JSONDecodeError, ValueError, TypeError):
		return None


def _money(value) -> float | None:
	"""A Currency column as a number, or None where the column is empty.

	NOT `float(x) or None`. Zero is an answer — "this was valued and it is worth
	nothing" — and an unset column is the absence of one, and an insurance
	schedule that cannot tell them apart reports a valued write-off and an
	unvalued tractor in the same line.
	"""
	if value in (None, ""):
		return None
	try:
		return round(float(value), 2)
	except (TypeError, ValueError):
		return None


def _describe_asset(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"asset_type": row.get("asset_type") or None,
		"company": row.get("company") or None,
		"location": row.get("location") or None,
		# BOTH SPELLINGS, and `location` is the one that stays. The column has
		# been called that since v0.25.0 and its label reads "Location (Parent
		# Asset)", which is exactly the confusion the second key removes: a
		# valve's parent is a zone, not a place, and a caller setting the
		# hierarchy should not have to know the column was named before the
		# tree was. Renaming the column would break every stored filter and
		# every client reading it.
		"parent_asset": row.get("location") or None,
		"qr_url": row.get("qr_url") or None,
		"nfc_uid": row.get("nfc_uid") or None,
		"current_state": _parse_state(row.get("current_state")),
		"last_scan_at": str(row.get("last_scan_at") or "") or None,
		"last_scan_by": row.get("last_scan_by") or None,
		"retired": bool(row.get("retired_at")),
		"retired_at": str(row.get("retired_at") or "") or None,
		"description": row.get("description") or None,
		"serial_number": row.get("serial_number") or None,
		"model": row.get("model") or None,
		"acquired_on": str(row.get("acquired_on") or "") or None,
		# Zero and absent are kept apart. A tractor entered with no replacement
		# value has not been valued; one entered as 0 has been, by somebody who
		# meant it. `or None` on a Currency column would have merged the two and
		# the insurance schedule would report both as "no cover stated".
		"purchase_value": _money(row.get("purchase_value")),
		"replacement_value": _money(row.get("replacement_value")),
		"gps_latitude": round(float(row.get("gps_latitude") or 0), 7) or None,
		"gps_longitude": round(float(row.get("gps_longitude") or 0), 7) or None,
		# The schedule and the meter, each kept apart from "not set" the same way
		# the currency columns above are: a machine with a zero-hour interval is
		# not on an hours schedule, and one reading 0.0 is straight off the lot.
		"service_interval_hours": _money(row.get("service_interval_hours")),
		"service_interval_days": (
			int(row["service_interval_days"]) if row.get("service_interval_days") else None
		),
		"last_service_date": str(row.get("last_service_date") or "") or None,
		"last_service_hours": _money(row.get("last_service_hours")),
		"current_hours": _money(row.get("current_hours")),
		"hours_updated_at": str(row.get("hours_updated_at") or "") or None,
		"irrigation_zone": row.get("irrigation_zone") or None,
	}


def asset_row(asset_name: str, company: str = "") -> dict:
	"""One Asset Register record as a dict, from its docname."""
	asset_name = (asset_name or "").strip()
	if not asset_name:
		raise ToolError("asset_name is required (an Asset Register docname, e.g. 'MC-Valve-05')")
	fields = compat.existing_fields(ASSET_REGISTER, _ASSET_FIELDS)

	if frappe.db.exists(ASSET_REGISTER, asset_name):
		row = dict(frappe.db.get_value(ASSET_REGISTER, asset_name, fields, as_dict=True) or {})
		if company and row.get("company") and row["company"] != company:
			raise ToolError(f"Asset {asset_name!r} belongs to {row['company']!r}, not {company!r}")
		return row

	filters = {"name": ("like", f"%{asset_name}%")}
	if company:
		filters["company"] = company
	matches = frappe.db.get_all(ASSET_REGISTER, filters=filters, fields=fields, limit=25)
	if len(matches) == 1:
		return dict(matches[0])
	if len(matches) > 1:
		names = ", ".join(sorted(str(m.get("name")) for m in matches))
		raise ToolError(f"{asset_name!r} matches {len(matches)} assets: {names}. Pass the exact docname.")
	raise ToolError(f"no Asset Register record called {asset_name!r}. list_assets has the register.")


def _asset_history(asset_name: str, limit: int = 50) -> list:
	"""Chronological history from all doctypes that reference this asset."""
	events = []
	for doctype, link_field, field_str in _HISTORY_DOCTYPES:
		if not compat.doctype_exists(doctype):
			continue
		if not compat.has_field(doctype, link_field):
			continue
		fields_wanted = [f.strip() for f in field_str.split(",")]
		fields_available = compat.existing_fields(doctype, fields_wanted)
		if not fields_available:
			continue
		try:
			rows = frappe.db.get_all(
				doctype,
				filters={link_field: asset_name},
				fields=fields_available,
				order_by="creation desc",
				limit=limit,
			)
		except Exception:
			continue
		for row in rows or []:
			event = {"doctype": doctype, "docname": row.get("name")}
			for field in fields_available:
				if field != "name":
					event[field] = str(row.get(field) or "") or None
			event["timestamp"] = str(row.get("creation") or "")
			events.append(event)

	events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
	return events[:limit]


# ── list_assets ────────────────────────────────────────────────────────────
def list_assets(args: dict) -> ToolResult:
	"""The asset register: every tagged asset with its type, location and scan status."""
	_require()
	company = _company(args)
	limit = as_limit(args)

	filters = {}
	if company:
		filters["company"] = company
	asset_type = as_str(args, "asset_type")
	if asset_type:
		filters["asset_type"] = asset_type
	location = as_str(args, "location")
	if location:
		filters["location"] = location

	retired = as_bool(args, "retired")
	if retired is True:
		filters["retired_at"] = ("is", "set")
	elif retired is False or retired is None:
		filters["retired_at"] = ("is", "not set")

	rows = frappe.db.get_all(
		ASSET_REGISTER,
		filters=filters,
		fields=compat.existing_fields(ASSET_REGISTER, _ASSET_FIELDS),
		order_by="asset_type asc, name asc",
		limit=min(limit, REGISTER_CAP),
	)
	assets = [_describe_asset(dict(row)) for row in rows]

	# v0.148.0. Which of these are also on the books, in ONE query rather than
	# one per row — this returns up to five hundred, and a per-row lookup to fill
	# a column is five hundred round trips.
	mirrors = asset_mirror.mirrors_for([asset["name"] for asset in assets])
	for asset in assets:
		asset["erpnext_asset"] = mirrors.get(asset["name"])

	by_type: dict = {}
	for asset in assets:
		key = asset["asset_type"] or "(unrecorded)"
		by_type[key] = by_type.get(key, 0) + 1

	return ToolResult(
		data={
			"company": company,
			"asset_count": len(assets),
			"by_asset_type": dict(sorted(by_type.items())),
			# The gap, stated as a number. "How much of the tag register is on
			# the books" is the question this feature exists to make answerable,
			# and counting `erpnext_asset` by hand across five hundred rows is
			# not answering it.
			"on_the_books_count": sum(1 for asset in assets if asset["erpnext_asset"]),
			"assets": assets,
		},
		summary=f"{len(assets)} asset(s)" + (f" for {company}" if company else ""),
	)


# ── get_asset_detail ───────────────────────────────────────────────────────
def get_asset_detail(args: dict) -> ToolResult:
	"""One asset in full: current state, open tasks, history timeline."""
	_require()
	company = _company(args)
	name = as_str(args, "asset_name", required=True)
	row = asset_row(name, company or "")
	described = _describe_asset(row)

	history = _asset_history(row["name"], limit=as_limit(args))

	open_tasks = []
	if compat.doctype_exists("Farm Task"):
		task_fields = compat.existing_fields(
			"Farm Task", ["name", "task_type", "state", "urgency", "assigned_to", "origin", "creation"]
		)
		seen = set()
		for link_field in ("asset", "asset_register"):
			if not compat.has_field("Farm Task", link_field):
				continue
			live_states = (
				"Draft",
				"Available",
				"Claimed",
				"In-Progress",
				"Awaiting-Review",
				"Open",
				"In Progress",
				"Dispatched",
			)
			try:
				tasks = frappe.db.get_all(
					"Farm Task",
					filters={link_field: row["name"], "state": ("in", list(live_states))},
					fields=task_fields,
					order_by="creation desc",
					limit=50,
				)
				for t in tasks or []:
					if t.get("name") not in seen:
						seen.add(t["name"])
						open_tasks.append(dict(t))
			except Exception:
				pass

	children = frappe.db.get_all(
		ASSET_REGISTER,
		filters={"location": row["name"]},
		fields=compat.existing_fields(ASSET_REGISTER, ("name", "asset_type", "retired_at")),
		order_by="name asc",
		limit=REGISTER_CAP,
	)
	child_list = [
		{"name": c.get("name"), "asset_type": c.get("asset_type"), "retired": bool(c.get("retired_at"))}
		for c in children or []
	]

	return ToolResult(
		data={
			**described,
			"erpnext_asset": asset_mirror.mirror_of(row["name"]) or None,
			"open_tasks": open_tasks,
			"open_task_count": len(open_tasks),
			"children": child_list,
			"child_count": len(child_list),
			"history": history,
			"history_count": len(history),
		},
		summary=f"{row['name']}: {described['asset_type'] or 'untyped'}"
		+ (f", {len(open_tasks)} open task(s)" if open_tasks else ""),
	)


# ── get_asset_history ──────────────────────────────────────────────────────
def get_asset_history(args: dict) -> ToolResult:
	"""Chronological history of all events/tasks/inspections for one asset."""
	_require()
	company = _company(args)
	name = as_str(args, "asset_name", required=True)
	row = asset_row(name, company or "")
	limit = as_limit(args)

	history = _asset_history(row["name"], limit=limit)

	return ToolResult(
		data={
			"asset_name": row["name"],
			"asset_type": row.get("asset_type"),
			"event_count": len(history),
			"events": history,
		},
		summary=f"{row['name']}: {len(history)} event(s)",
	)


# ── scan_asset ─────────────────────────────────────────────────────────────
def scan_asset(args: dict) -> ToolResult:
	"""Record a scan event, return asset detail + open tasks + due compliance items.

	Updates last_scan_at and last_scan_by on the asset record. If GPS coordinates
	are provided, updates the asset's position too.
	"""
	_require()
	name = as_str(args, "asset_name", required=True)
	scanned_by = as_str(args, "scanned_by")

	row = asset_row(name)
	doc = frappe.get_doc(ASSET_REGISTER, row["name"])

	doc.last_scan_at = frappe.utils.now()
	if scanned_by:
		doc.last_scan_by = scanned_by

	gps_lat = args.get("gps_lat") or args.get("gps_latitude")
	gps_lon = args.get("gps_lon") or args.get("gps_longitude")
	if gps_lat is not None and gps_lon is not None:
		try:
			doc.gps_latitude = float(gps_lat)
			doc.gps_longitude = float(gps_lon)
		except (TypeError, ValueError):
			pass

	doc.save(ignore_permissions=True)
	described = _describe_asset(dict(doc.as_dict()))

	open_tasks = []
	if compat.doctype_exists("Farm Task"):
		task_fields = compat.existing_fields(
			"Farm Task", ["name", "task_type", "state", "urgency", "assigned_to", "origin", "creation"]
		)
		seen = set()
		for link_field in ("asset", "asset_register"):
			if not compat.has_field("Farm Task", link_field):
				continue
			live_states = (
				"Draft",
				"Available",
				"Claimed",
				"In-Progress",
				"Awaiting-Review",
				"Open",
				"In Progress",
				"Dispatched",
			)
			try:
				tasks = frappe.db.get_all(
					"Farm Task",
					filters={link_field: doc.name, "state": ("in", list(live_states))},
					fields=task_fields,
					order_by="creation desc",
					limit=20,
				)
				for t in tasks or []:
					if t.get("name") not in seen:
						seen.add(t["name"])
						open_tasks.append(dict(t))
			except Exception:
				pass

	due_compliance = []
	if compat.doctype_exists("Compliance Alert") and compat.has_field("Compliance Alert", "asset_register"):
		try:
			alerts = frappe.db.get_all(
				"Compliance Alert",
				filters={
					"asset_register": doc.name,
					"status": ("in", ["Open", "Overdue"]),
				},
				fields=compat.existing_fields(
					"Compliance Alert", ["name", "alert_type", "severity", "status", "creation"]
				),
				order_by="creation desc",
				limit=20,
			)
			due_compliance = [dict(a) for a in alerts or []]
		except Exception:
			pass

	asset_type = described.get("asset_type") or "General"
	can_report = compat.doctype_exists("Farm Task") and compat.has_field("Farm Task", "asset")
	suggested_skill = ASSET_TYPE_SKILL_MAP.get(asset_type, "general_maintenance")

	# v0.77.0. THE SAME MENU `universal_scan` RETURNS, because this is the same
	# moment: a worker is standing at the machine with the tag on their screen.
	# The two scan routes answering different questions is how a handset ends up
	# with two code paths for one card.
	from . import asset_actions, asset_status

	defn = _STATE_DEFINITIONS.get(asset_type)
	effective = _current_state_value(described.get("current_state")) or (defn["default"] if defn else "")

	# v0.78.0. THE SAME ARGUMENT ONE STEP FURTHER. The menu said what a worker
	# may DO; the status report says what they need to KNOW before they do it —
	# what it is due, what it has run, what is outstanding on it, and whether the
	# ground under it is closed to entry. Composed once in `asset_status` and
	# returned by both scan routes, because seven round trips on a rural cell is
	# a screen nobody waits for. Every section degrades to empty with a reason;
	# a scan never fails over a section.
	report = asset_status.status_report(dict(doc.as_dict()), args)

	return ToolResult(
		data={
			**described,
			"scan_recorded": True,
			"open_tasks": open_tasks,
			"open_task_count": len(open_tasks),
			"due_compliance": due_compliance,
			"due_compliance_count": len(due_compliance),
			"can_report": can_report,
			"suggested_skill": suggested_skill,
			"state": effective or None,
			"state_actions": _actions_for(asset_type, effective) if effective else [],
			"action_menu": asset_actions.menu_for(asset_type, effective),
			# SPREAD LAST AND DELIBERATELY. `status_report` re-answers `state`
			# and `open_tasks` in richer shapes under keys of its own
			# (`status.state`, `status.open_tasks`), and it must not overwrite
			# the flat keys above that every shipped handset already decodes —
			# so it lands under one key and the old surface is untouched.
			"status": report,
			# Hoisted out of `status` because they are the two facts a screen
			# colours the whole card on, and a client should not have to reach
			# into a nested object to find out whether to show a red banner.
			"needs_attention": report["needs_attention"],
			"warnings": report["warnings"],
		},
		summary=(
			f"scanned {doc.name}"
			+ (f" by {scanned_by}" if scanned_by else "")
			+ (f" — {report['warnings'][0]['message']}" if report["warnings"] else "")
		),
		docstatus_delta="0 → 0 (updated)",
	)


# ── create_asset ───────────────────────────────────────────────────────────
def _parent(args: dict, verb: str) -> str:
	"""The parent asset, under either name, checked to exist.

	`parent_asset` AND `location` BOTH WORK, and neither is deprecated. The
	column has been `location` since v0.25.0 and its own label reads "Location
	(Parent Asset)" — which is the confusion, not the fix: a valve's parent is a
	zone, not a place. Renaming it would break every stored filter, every report
	column and every client already sending it, so the second spelling is
	accepted at the door instead and `_describe_asset` answers with both.

	Passing the two with DIFFERENT values is refused rather than resolved by
	precedence. There is no reading of that call which is not somebody's mistake,
	and picking a winner silently would put a valve under the wrong turnout —
	which is what the closing cascade walks.
	"""
	parent = as_str(args, "parent_asset")
	location = as_str(args, "location")
	if parent and location and parent != location:
		raise ToolError(
			f"parent_asset {parent!r} and location {location!r} are two names for one column "
			f"and they disagree. Send one. Nothing was {verb}."
		)
	parent = parent or location
	if parent and not frappe.db.exists(ASSET_REGISTER, parent):
		raise ToolError(
			f"Parent asset {parent!r} does not exist in Asset Register. The parent must be "
			f"registered first — a turnout before the valves under it. Nothing was {verb}."
		)
	return parent


def _capital_fields(doc, args: dict) -> None:
	"""The insurance columns, set from whichever of them the caller sent."""
	for key in ("serial_number", "model"):
		value = as_str(args, key)
		if value:
			doc.set(key, value)
	acquired = as_date(args, "acquired_on")
	if acquired:
		doc.acquired_on = acquired
	for key in ("purchase_value", "replacement_value"):
		if args.get(key) is not None:
			doc.set(key, as_float(args.get(key), key))


def _service_fields(doc, args: dict) -> None:
	"""The service schedule and the zone link, set at registration.

	ACCEPTED AT REGISTRATION rather than only on a later update, because the one
	moment somebody has the manual open is the moment they are entering the
	machine — and a schedule nobody sets is a machine that never comes up as
	due. `current_hours` is deliberately absent: the meter is a series written by
	a metered check-out, not a number typed onto a new record.
	"""
	for key in ("service_interval_hours", "last_service_hours"):
		if args.get(key) is not None:
			doc.set(key, as_float(args.get(key), key))
	if args.get("service_interval_days") is not None:
		doc.service_interval_days = as_int(args, "service_interval_days")
	serviced = as_date(args, "last_service_date")
	if serviced:
		doc.last_service_date = serviced
	zone = as_str(args, "irrigation_zone")
	if zone:
		if not frappe.db.exists(IRRIGATION_ZONE, zone):
			raise ToolError(
				f"no Irrigation Zone called {zone!r} on this site. list_irrigation_zones has the "
				"register. Nothing was created."
			)
		doc.irrigation_zone = zone


def _attach_photo(asset: str, args: dict) -> str:
	"""Point an already-uploaded File at this asset, and say whether it stuck.

	THE BYTES DO NOT COME THROUGH HERE. The upload path this app has is
	`stage_file_chunk` → `finalize_staged_file`, which verifies a SHA-256 before
	anything is written, and re-implementing it inline would be a second way to
	put a file on this site with one fewer check on it. So registration takes the
	File docname that path returns and re-points it, which is what
	`attach_file_to_document` does for every other doctype.

	A FAILURE HERE DOES NOT UNDO THE REGISTRATION. The asset is the record; the
	photograph is evidence about it. Losing a tractor's registration because its
	photo could not be attached would be the wrong trade, so the reason comes
	back in `photo_attached` / `photo_error` and the caller can retry the attach
	alone.
	"""
	token = as_str(args, "photo_file_token") or as_str(args, "photo")
	if not token:
		return ""
	if not frappe.db.exists("File", token):
		raise ToolError(
			f"no File called {token!r} on this site. Upload the photograph with "
			"stage_file_chunk and finalize_staged_file first, then pass the docname that "
			"returns as photo_file_token."
		)
	frappe.db.set_value(
		"File",
		token,
		{"attached_to_doctype": ASSET_REGISTER, "attached_to_name": asset},
		update_modified=False,
	)
	return token


def _with_mirror(described: dict, verdict: dict) -> dict:
	"""The mirror's verdict, folded into an answer the caller already had.

	ADDITIVE AND NEVER SUBTRACTIVE. Every key the handset reads today is still
	here and still means the same thing; these three are new, and a client that
	does not know about them ignores them. That is the whole reason the mobile
	contract did not have to change for this feature.

	`erpnext_asset_note` IS PRESENT ONLY WHEN THERE IS SOMETHING TO SAY. A key
	that is always there and usually empty gets read as "no problem" by a person
	skimming, which is the one reading it must never support: the interesting
	case is the tag that did NOT reach the books, and it should look different
	from the one that did.
	"""
	described["erpnext_asset"] = verdict.get("asset")
	described["erpnext_asset_created"] = bool(verdict.get("created"))
	# v0.149.0. How many photographs this call put onto the ERPNext Asset. A
	# COUNT AND NOT A SILENCE: the whole complaint this feature answers is
	# "I photographed a wind machine and the picture went nowhere", and an
	# answer that does not say what happened to the photograph cannot settle it.
	# Zero on a mirrored asset means the tag had none to copy.
	if verdict.get("asset"):
		described["erpnext_asset_photos"] = len(verdict.get("photos") or [])
	if verdict.get("reason"):
		described["erpnext_asset_note"] = verdict["reason"]
	return described


def register_asset(args: dict) -> ToolResult:
	"""Register a new asset with its tag ID, type, parent and insurance detail.

	THE PHOTOGRAPH IS A TWO-STEP FLOW AND IS DOCUMENTED AS ONE. A handset that
	has just OCR'd a VIN off a plate has the photograph in hand, and the natural
	thing to want is one call. It is still two: `stage_file_chunk` /
	`finalize_staged_file` move the bytes and verify the hash, and the docname
	that returns goes in `photo_file_token` here. What this saves over calling
	`attach_file_to_document` afterwards is the round trip and the failure mode
	where the asset exists and nobody remembered to attach anything —
	`export_insurance_schedule` reads exactly these attachments.
	"""
	_require()
	company = _company(args, required=True)
	name = as_str(args, "name", required=True)

	if frappe.db.exists(ASSET_REGISTER, name):
		raise ToolError(
			f"Asset Register already has a record called {name!r}. The docname IS the "
			"printable tag ID, and two tags with the same string is two tags that resolve "
			"to the same record. Nothing was created."
		)

	asset_type = as_str(args, "asset_type", required=True)
	if asset_type not in ASSET_TYPES:
		from ..args import as_choice

		asset_type = as_choice(ASSET_REGISTER, "asset_type", asset_type, "asset_type")

	location = _parent(args, "created")

	doc = frappe.new_doc(ASSET_REGISTER)
	doc.__newname = name
	doc.asset_type = asset_type
	doc.company = company
	doc.location = location or None
	doc.description = as_str(args, "description")
	doc.nfc_uid = as_str(args, "nfc_uid")
	_capital_fields(doc, args)
	_service_fields(doc, args)

	lat = args.get("gps_latitude")
	lon = args.get("gps_longitude")
	if lat is not None:
		doc.gps_latitude = as_float(lat, "gps_latitude")
	if lon is not None:
		doc.gps_longitude = as_float(lon, "gps_longitude")

	doc.insert(ignore_permissions=True)

	photo, photo_error = "", ""
	try:
		photo = _attach_photo(doc.name, args)
	except ToolError as exc:
		photo_error = str(exc)

	described = _describe_asset(dict(doc.as_dict()))
	described["photo_attached"] = photo or None
	if photo_error:
		described["photo_error"] = (
			f"{photo_error} The asset itself WAS registered as {doc.name} — attach the "
			"photograph with attach_file_to_document rather than registering it again."
		)

	# v0.148.0. The same machine on the books, when the switch is on and the tag
	# carries what ERPNext's Asset demands. AFTER the insert and after the photo,
	# and it raises nothing: `asset_mirror.sync` returns its verdict rather than
	# throwing, for the reason `_attach_photo` gives — the tag is the record, and
	# losing a registration because a second copy of it could not be built would
	# be the wrong way round.
	verdict = asset_mirror.sync(
		dict(doc.as_dict()), location=as_str(args, "asset_location"), photo_file=photo
	)
	described = _with_mirror(described, verdict)

	return ToolResult(
		data=described,
		summary=f"registered asset {doc.name} ({asset_type})"
		+ (f", parent {location}" if location else "")
		+ (", photo attached" if photo else "")
		+ (f", ERPNext Asset {verdict['asset']}" if verdict.get("created") else ""),
		docstatus_delta="none → 0 (created)",
	)


# ── update_asset ───────────────────────────────────────────────────────────
def update_registered_asset(args: dict) -> ToolResult:
	"""Update asset fields. Cannot rename — the docname is the tag ID."""
	_require()
	company = _company(args)
	name = as_str(args, "asset_name", required=True)
	row = asset_row(name, company or "")

	doc = frappe.get_doc(ASSET_REGISTER, row["name"])
	changes = {}

	for key in ("description", "nfc_uid", "serial_number", "model"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	if "acquired_on" in args:
		_stage(changes, doc, "acquired_on", as_date(args, "acquired_on"))
	for key in ("purchase_value", "replacement_value"):
		if key in args:
			value = args.get(key)
			_stage(changes, doc, key, as_float(value, key) if value is not None else None)
	if "asset_type" in args:
		value = as_str(args, "asset_type")
		if value:
			from ..args import as_choice

			value = as_choice(ASSET_REGISTER, "asset_type", value, "asset_type")
		_stage(changes, doc, "asset_type", value)
	if "location" in args or "parent_asset" in args:
		location = _parent(args, "changed")
		if location and location == row["name"]:
			raise ToolError("An asset cannot be its own parent. Nothing was changed.")
		_stage(changes, doc, "location", location or None)
	for key in ("gps_latitude", "gps_longitude"):
		if key in args:
			_stage(changes, doc, key, as_float(args.get(key), key))
	# v0.78.0. The service schedule and the zone link. `current_hours` is NOT
	# settable here on purpose: it is a cache of the Asset State Log's own
	# series, written by a metered check-out or check-in, and a column somebody
	# could type over would make "hours since service" mean whatever was entered
	# last. `last_service_hours` IS settable, because closing out a service is a
	# statement about the past that record_service also makes.
	for key in ("service_interval_hours", "last_service_hours"):
		if key in args:
			value = args.get(key)
			_stage(changes, doc, key, as_float(value, key) if value is not None else None)
	if "service_interval_days" in args:
		value = args.get("service_interval_days")
		_stage(
			changes,
			doc,
			"service_interval_days",
			as_int(args, "service_interval_days") if value is not None else None,
		)
	if "last_service_date" in args:
		_stage(changes, doc, "last_service_date", as_date(args, "last_service_date"))
	if "irrigation_zone" in args:
		zone = as_str(args, "irrigation_zone")
		if zone and not frappe.db.exists(IRRIGATION_ZONE, zone):
			raise ToolError(
				f"no Irrigation Zone called {zone!r} on this site. list_irrigation_zones has the "
				"register, and the link is what lets get_water_usage_report price this valve's "
				"minutes as gallons. Nothing was changed."
			)
		_stage(changes, doc, "irrigation_zone", zone or None)
	if "current_state" in args:
		state = args.get("current_state")
		if state and isinstance(state, str):
			try:
				json.loads(state)
			except (json.JSONDecodeError, ValueError):
				raise ToolError("current_state must be valid JSON. Nothing was changed.") from None
		elif state and isinstance(state, dict):
			state = json.dumps(state)
		_stage(changes, doc, "current_state", state or None)

	# v0.148.0. `asset_location` changes NOTHING on the register — it names the
	# ERPNext Location the mirrored Asset is filed under — so it is not staged as
	# a change and it does not, on its own, make this a no-op. A site with more
	# than one Location gets a mirror refusal naming the argument, and the retry
	# that follows carries only that argument; refusing it as "nothing to change"
	# would make the refusal unactionable through the door that raised it.
	mirror_location = as_str(args, "asset_location")
	if not changes and not mirror_location:
		raise ToolError(
			"nothing to change. Pass at least one of: asset_type, parent_asset (or location), "
			"description, nfc_uid, serial_number, model, acquired_on, purchase_value, "
			"replacement_value, gps_latitude, gps_longitude, current_state — or "
			"asset_location, which files this asset's ERPNext Asset under an ERPNext "
			"Location and leaves the register row alone."
		)

	if changes:
		doc.save(ignore_permissions=True)
	described = _describe_asset(dict(doc.as_dict()))

	# v0.148.0. An edit that supplies purchase_value and acquired_on is the edit
	# that makes a tag mirrorable, so this is where a valve somebody has finally
	# costed reaches the books. On an asset ALREADY on the books this refreshes
	# identity only — never the money. `asset_mirror._refresh` says why.
	verdict = asset_mirror.sync(dict(doc.as_dict()), location=mirror_location)
	described = _with_mirror(described, verdict)

	return ToolResult(
		data={
			**described,
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed"
		+ (f", ERPNext Asset {verdict['asset']} created" if verdict.get("created") else ""),
		docstatus_delta="0 → 0 (updated)",
	)


def _stage(changes: dict, doc, field: str, wanted) -> None:
	before = doc.get(field)
	before = "" if before is None else before
	if str(before) == str(wanted if wanted is not None else ""):
		return
	changes[field] = [before or None, wanted or None]
	doc.set(field, wanted if wanted != "" else None)


# ── retire_asset ───────────────────────────────────────────────────────────
def retire_asset(args: dict) -> ToolResult:
	"""Soft-retire an asset: set retired_at, preserve all history."""
	_require()
	company = _company(args)
	name = as_str(args, "asset_name", required=True)
	row = asset_row(name, company or "")

	if row.get("retired_at"):
		raise ToolError(f"{row['name']} was already retired on {row['retired_at']}. Nothing was changed.")

	doc = frappe.get_doc(ASSET_REGISTER, row["name"])
	doc.retired_at = as_date(args, "retired_at") or frappe.utils.today()
	reason = as_str(args, "reason")
	if reason:
		existing_desc = doc.description or ""
		doc.description = f"{existing_desc}\n\nRetired: {reason}".strip()
	doc.save(ignore_permissions=True)

	described = _describe_asset(dict(doc.as_dict()))

	# v0.148.0. THE MIRROR IS REPORTED AND IS NOT DISPOSED OF. ERPNext retires an
	# asset through a scrap or sale journal that posts to the general ledger;
	# this writes a date on a register row. They are not the same act, and
	# turning one into the other from here would move a balance nobody asked to
	# move. What the caller gets is the Asset's name and a sentence saying so.
	mirror = asset_mirror.mirror_of(doc.name)
	described["erpnext_asset"] = mirror or None
	if mirror:
		described["erpnext_asset_note"] = (
			f"the tag is retired. ERPNext Asset {mirror} is UNCHANGED and still on the books "
			"— disposing of a fixed asset posts a scrap or sale journal to the general "
			"ledger, which is a decision made in the Desk and not a consequence of retiring "
			"a tag."
		)

	return ToolResult(
		data={**described, "reason": reason or None},
		summary=f"retired {doc.name}" + (f": {reason}" if reason else ""),
		docstatus_delta="0 → 0 (updated)",
	)


# ── bulk_create_assets ─────────────────────────────────────────────────────
def bulk_create_assets(args: dict) -> ToolResult:
	"""Bulk registration for initial rollout. Each item needs name, asset_type."""
	_require()
	company = _company(args, required=True)
	assets_list = args.get("assets")
	if not assets_list or not isinstance(assets_list, list):
		raise ToolError("assets is required and must be a list of asset objects.")
	if len(assets_list) > REGISTER_CAP:
		raise ToolError(f"bulk_create_assets accepts at most {REGISTER_CAP} assets per call.")

	place = as_str(args, "asset_location")

	created = []
	errors = []
	for index, item in enumerate(assets_list):
		if not isinstance(item, dict):
			errors.append({"index": index, "error": "each asset must be an object"})
			continue
		name = str(item.get("name") or "").strip()
		asset_type = str(item.get("asset_type") or "").strip()
		if not name or not asset_type:
			errors.append({"index": index, "name": name, "error": "name and asset_type are required"})
			continue
		if frappe.db.exists(ASSET_REGISTER, name):
			errors.append({"index": index, "name": name, "error": f"{name} already exists"})
			continue

		doc = frappe.new_doc(ASSET_REGISTER)
		doc.__newname = name
		doc.asset_type = asset_type
		doc.company = company
		doc.location = str(item.get("location") or "").strip() or None
		doc.description = str(item.get("description") or "").strip() or None
		doc.nfc_uid = str(item.get("nfc_uid") or "").strip() or None
		if item.get("gps_latitude") is not None:
			try:
				doc.gps_latitude = float(item["gps_latitude"])
			except (TypeError, ValueError):
				pass
		if item.get("gps_longitude") is not None:
			try:
				doc.gps_longitude = float(item["gps_longitude"])
			except (TypeError, ValueError):
				pass
		# v0.148.0. THE TWO FACTS THAT DECIDE WHETHER A ROW REACHES THE BOOKS,
		# accepted here rather than left to five hundred follow-up edits. Both are
		# optional and both were absent before this release, so a caller sending
		# the shape this tool has always taken gets exactly what it always got —
		# a tag, and no ERPNext Asset. A bad figure is skipped rather than
		# refused: a rollout of five hundred valves should not be lost to one
		# mistyped price, and a row that mirrors nothing says so in `created`.
		for key in ("purchase_value", "replacement_value"):
			if item.get(key) is not None:
				try:
					doc.set(key, float(item[key]))
				except (TypeError, ValueError):
					pass
		if item.get("acquired_on"):
			doc.acquired_on = str(item["acquired_on"]).strip() or None
		try:
			doc.insert(ignore_permissions=True)
		except Exception as exc:
			errors.append({"index": index, "name": name, "error": str(exc)})
			continue
		# OUTSIDE the try, deliberately. `sync` catches its own failures and
		# returns a verdict, but a row that WAS inserted must not be able to
		# reach the error list through this branch whatever happens after — that
		# would report a registered asset as one that failed to register.
		stored = dict(doc.as_dict())
		created.append(_with_mirror(_describe_asset(stored), asset_mirror.sync(stored, location=place)))

	mirrored = [row["name"] for row in created if row.get("erpnext_asset")]
	return ToolResult(
		data={
			"company": company,
			"created_count": len(created),
			"error_count": len(errors),
			# The count rather than a re-listing: `created` already carries each
			# row's own `erpnext_asset` and its reason, and a rollout of five
			# hundred wants the headline before it wants the five hundred.
			"mirrored_count": len(mirrored),
			"created": created,
			"errors": errors,
		},
		summary=f"bulk created {len(created)} asset(s), {len(errors)} error(s)"
		+ (f", {len(mirrored)} on the books" if mirrored else ""),
		docstatus_delta="none → 0 (created)" if created else "",
	)


# ── generate_asset_qr ─────────────────────────────────────────────────────
def generate_asset_qr(args: dict) -> ToolResult:
	"""Generate a QR code for one asset's tag ID."""
	_require()
	name = as_str(args, "asset_name", required=True)
	row = asset_row(name)
	url = row.get("qr_url") or f"/scan/{row['name']}"

	fmt = as_str(args, "format") or "png"
	if fmt not in ("png", "matrix"):
		raise ToolError("format must be 'png' or 'matrix'.")

	rendered = qr.render(url)
	data = {
		"asset_name": row["name"],
		"qr_url": url,
		"modules": rendered["modules"],
		"pixels": rendered["pixels"],
		"scale": rendered["scale"],
		"border": rendered["border"],
		"error_correction": rendered["error_correction"],
		"encoder": rendered["encoder"],
	}
	if fmt == "png":
		data["png_base64"] = base64.b64encode(rendered["png"]).decode("ascii")
		data["png_bytes"] = len(rendered["png"])
	else:
		data["matrix"] = rendered["matrix"]

	return ToolResult(
		data=data,
		summary=f"QR code for {row['name']} ({rendered['modules']}×{rendered['modules']} modules)",
	)


# ── generate_asset_qr_sheet ───────────────────────────────────────────────
def generate_asset_qr_sheet(args: dict) -> ToolResult:
	"""Bulk QR sheet: one QR per asset, for printing on Avery labels."""
	_require()
	names = args.get("asset_names")
	if not names or not isinstance(names, list):
		raise ToolError("asset_names is required and must be a list of asset docnames.")
	if len(names) > 100:
		raise ToolError("generate_asset_qr_sheet accepts at most 100 assets per call.")

	template = as_str(args, "template") or "avery_5160"

	labels = []
	errors = []
	for name in names:
		name = str(name or "").strip()
		if not name:
			continue
		if not frappe.db.exists(ASSET_REGISTER, name):
			errors.append({"asset_name": name, "error": "not found"})
			continue
		url_val = frappe.db.get_value(ASSET_REGISTER, name, "qr_url") or f"/scan/{name}"
		try:
			rendered = qr.render(url_val)
			labels.append(
				{
					"asset_name": name,
					"qr_url": url_val,
					"png_base64": base64.b64encode(rendered["png"]).decode("ascii"),
					"modules": rendered["modules"],
				}
			)
		except Exception as exc:
			errors.append({"asset_name": name, "error": str(exc)})

	return ToolResult(
		data={
			"template": template,
			"label_count": len(labels),
			"error_count": len(errors),
			"labels": labels,
			"errors": errors,
		},
		summary=f"{len(labels)} label(s) generated for {template}",
	)


# ══════════════════════════════════════════════════════════════════════════════
# v0.26.0 — field-initiated task creation from asset scan
# ══════════════════════════════════════════════════════════════════════════════


def report_asset_issue(args: dict) -> ToolResult:
	"""Convenience wrapper: report a problem on a specific asset.

	Looks up the asset, auto-fills location and skill_required from it, then
	delegates to dispatch.report_field_task with the asset link pre-filled.
	"""
	_require()
	compat.require_doctype(
		"Farm Task",
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)

	asset_name = as_str(args, "asset_name", required=True)
	row = asset_row(asset_name)
	asset_type = row.get("asset_type") or "General"

	inner = {
		"reported_by": as_str(args, "reported_by", required=True),
		"photo_file_token": as_str(args, "photo_file_token", required=True),
		"asset": row["name"],
		"task_type": as_str(args, "task_type") or "Repair",
		"skill_required": as_str(args, "skill_required")
		or ASSET_TYPE_SKILL_MAP.get(asset_type, "general_maintenance"),
		"urgency": as_str(args, "urgency") or "Normal",
	}
	description = as_str(args, "description")
	if description:
		inner["description"] = description

	company = as_str(args, "company") or row.get("company")
	if company:
		inner["company"] = company

	gps_lat = args.get("gps_lat") or args.get("gps_latitude")
	gps_lon = args.get("gps_lon") or args.get("gps_longitude")
	if gps_lat is not None and gps_lon is not None:
		inner["gps_lat"] = gps_lat
		inner["gps_lon"] = gps_lon

	from . import dispatch

	result = dispatch.report_field_task(inner)
	data = result.data or {}
	data["asset"] = row["name"]
	data["asset_type"] = asset_type
	data["skill_auto_mapped"] = "skill_required" not in args or not args.get("skill_required")

	return ToolResult(
		data=data,
		summary=(
			f"field report {data.get('name', '?')} on {row['name']} ({asset_type}), "
			f"skill={inner['skill_required']}"
		),
		docstatus_delta="none → 0 (created)",
	)


# ══════════════════════════════════════════════════════════════════════════════
# v0.25.0 — state-change actions
# ══════════════════════════════════════════════════════════════════════════════

_STATE_DEFINITIONS: dict[str, dict] = {
	"Irrigation Valve": {
		"default": "closed",
		"actions": {
			"open_valve": {"from": ["closed"], "to": "open"},
			"close_valve": {"from": ["open"], "to": "closed"},
			"winterize": {"from": ["closed"], "to": "winterized"},
			"reopen": {"from": ["winterized"], "to": "closed"},
		},
	},
	"Housing Unit": {
		"default": "vacant",
		"actions": {
			"mark_occupied": {"from": ["vacant"], "to": "occupied"},
			"mark_vacant": {"from": ["occupied", "uninhabitable"], "to": "vacant"},
			"mark_uninhabitable": {"from": ["vacant", "occupied"], "to": "uninhabitable"},
			"winterize": {"from": ["vacant"], "to": "winterized"},
			"reopen": {"from": ["winterized"], "to": "vacant"},
		},
	},
	"Sprayer": {
		"default": "empty",
		"actions": {
			"fill_tank": {"from": ["empty", "cleaned"], "to": "loaded"},
			"start_spray": {"from": ["loaded"], "to": "in_use"},
			"end_spray": {"from": ["in_use"], "to": "empty"},
			"clean_tank": {"from": ["empty"], "to": "cleaned"},
		},
	},
	# v0.77.0 adds check-out/check-in. Who has the tractor is a different
	# question from whether it runs, and the register could previously only
	# answer the second — so a machine somebody had driven to the far block read
	# `in_service`, which is true and not the answer anybody was asking for.
	# `start_maintenance` reaches from `checked_out` because that is where a
	# breakdown happens; `put_in_service` does not, because a tractor coming
	# back from the field is checked IN, by the person who has it.
	"Tractor": {
		"default": "in_service",
		"actions": {
			"put_in_service": {"from": ["out_of_service", "maintenance"], "to": "in_service"},
			"take_out_of_service": {"from": ["in_service", "checked_out"], "to": "out_of_service"},
			"start_maintenance": {
				"from": ["in_service", "out_of_service", "checked_out"],
				"to": "maintenance",
			},
			"end_maintenance": {"from": ["maintenance"], "to": "in_service"},
			"check_out": {"from": ["in_service"], "to": "checked_out"},
			"check_in": {"from": ["checked_out"], "to": "in_service"},
		},
	},
	# A pickup and a tractor are the same question — is it running, and who has
	# it — so Vehicle shares the machine rather than getting a near-copy that
	# would drift. Defined as its own entry rather than an alias so an operator
	# reading `_STATE_DEFINITIONS` sees what a Vehicle does without following a
	# reference.
	"Vehicle": {
		"default": "in_service",
		"actions": {
			"put_in_service": {"from": ["out_of_service", "maintenance"], "to": "in_service"},
			"take_out_of_service": {"from": ["in_service", "checked_out"], "to": "out_of_service"},
			"start_maintenance": {
				"from": ["in_service", "out_of_service", "checked_out"],
				"to": "maintenance",
			},
			"end_maintenance": {"from": ["maintenance"], "to": "in_service"},
			"check_out": {"from": ["in_service"], "to": "checked_out"},
			"check_in": {"from": ["checked_out"], "to": "in_service"},
		},
	},
	# An implement is attached or it is not, and WHICH tractor it is attached to
	# is the register tree rather than a state: `parent_asset` already points one
	# asset at another, and duplicating the link inside a state blob would give
	# the same fact two homes. See `asset_actions._MENU`, which says so in the
	# entry a handset draws.
	"Implement": {
		"default": "detached",
		"actions": {
			"attach": {"from": ["detached"], "to": "attached"},
			"detach": {"from": ["attached"], "to": "detached"},
			"flag_repair": {"from": ["attached", "detached"], "to": "needs_repair"},
			"clear_repair": {"from": ["needs_repair"], "to": "detached"},
		},
	},
	"Water Source": {
		"default": "active",
		"actions": {
			"activate": {"from": ["inactive"], "to": "active"},
			"deactivate": {"from": ["active", "treated"], "to": "inactive"},
			"log_treatment": {"from": ["active", "contaminated"], "to": "treated"},
			"mark_contaminated": {"from": ["active", "treated"], "to": "contaminated"},
			"clear_contamination": {"from": ["contaminated"], "to": "active"},
		},
	},
	"Storage": {
		"default": "closed",
		"actions": {
			"open_for_season": {"from": ["closed", "off_season"], "to": "open"},
			"close_for_season": {"from": ["open", "active_season"], "to": "off_season"},
			"log_temperature": {"from": ["open", "active_season"], "to": "active_season"},
		},
	},
	"Cold Storage": {
		"default": "closed",
		"actions": {
			"open_for_season": {"from": ["closed", "off_season"], "to": "open"},
			"close_for_season": {"from": ["open", "active_season"], "to": "off_season"},
			"log_temperature": {"from": ["open", "active_season"], "to": "active_season"},
		},
	},
	"Block": {
		"default": "active",
		"actions": {
			"activate": {"from": ["dormant", "fallow"], "to": "active"},
			"set_dormant": {"from": ["active"], "to": "dormant"},
			"set_fallow": {"from": ["active", "dormant"], "to": "fallow"},
		},
	},
	"Irrigation Zone": {
		"default": "active",
		"actions": {
			"activate": {"from": ["winterized", "offline"], "to": "active"},
			"winterize": {"from": ["active"], "to": "winterized"},
			"take_offline": {"from": ["active", "winterized"], "to": "offline"},
		},
	},
	"General": {
		"default": "active",
		"actions": {
			"activate": {"from": ["inactive", "needs_repair"], "to": "active"},
			"deactivate": {"from": ["active"], "to": "inactive"},
			"flag_repair": {"from": ["active", "inactive"], "to": "needs_repair"},
			"clear_repair": {"from": ["needs_repair"], "to": "active"},
		},
	},
}


def _current_state_value(state_json) -> str:
	parsed = _parse_state(state_json)
	if parsed and isinstance(parsed, dict):
		return str(parsed.get("state") or "")
	return ""


def _actions_for(asset_type: str, current: str) -> list[dict]:
	defn = _STATE_DEFINITIONS.get(asset_type)
	if not defn:
		return []
	result = []
	effective = current or defn["default"]
	for action_name, rule in defn["actions"].items():
		if effective in rule["from"]:
			result.append(
				{
					"action": action_name,
					"from_state": effective,
					"to_state": rule["to"],
				}
			)
	return result


# ══════════════════════════════════════════════════════════════════════════════
# The cascade: a state change that carries downhill
# ══════════════════════════════════════════════════════════════════════════════

#: Which actions carry to an asset's descendants, per asset type. Everything not
#: named here changes exactly one record, which is what a state change was until
#: now and what it stays for every type but one.
#:
#: WHY A VALVE IS THE EXCEPTION. A main valve at an irrigation turnout is
#: physically upstream of every valve on its line: shut it and the laterals below
#: it are dry, whether or not anybody walked out to them. Without the cascade the
#: register says those valves are `open` while no water can reach them — and that
#: is the reading a worker gets when they are sent out on a line break, which is
#: exactly the moment the main gets shut. The rule is not a convenience; it is
#: the register agreeing with the pipe.
#:
#: OPENING DOES NOT CASCADE, and the asymmetry is the physical one rather than a
#: half-finished implementation. Closing upstream stops the water for certain;
#: opening upstream only makes water AVAILABLE to whatever is below, each of
#: which is open or shut on its own account. A cascade in that direction would
#: report a whole line as running because somebody turned on the main — and
#: `get_irrigation_runtime` sums exactly these events into the minutes that feed
#: water usage, so the wrong `open` is not a cosmetic error on a screen, it is a
#: billing figure.
_CASCADING_ACTIONS: dict[str, tuple[str, ...]] = {
	"Irrigation Valve": ("close_valve",),
}

#: How far down the `location` tree a cascade walks, and how many records it may
#: touch. A turnout with more than five hundred valves under it, or a line nested
#: twelve deep, is a data problem rather than a farm — and a worker shutting one
#: valve is not where somebody should discover it. Both bounds are REPORTED when
#: they bite (`cascade_truncated`), because a silent cap on this particular
#: operation reads as "everything downstream is shut" when it is not.
CASCADE_DEPTH = 12
CASCADE_CAP = 500

_CHILD_FIELDS = ("name", "asset_type", "company", "location", "current_state", "retired_at")


def _descendants(asset_name: str) -> tuple[list[dict], bool]:
	"""Every asset below this one in the `location` tree, nearest first.

	Breadth-first and CYCLE-SAFE. `Asset Register.validate` refuses an asset that
	is its own parent and nothing refuses A → B → A, so a walk that trusted the
	tree to be a tree would spin on a register somebody has miskeyed. `seen`
	makes the second visit a no-op instead.

	Returns the rows and whether either bound above stopped the walk early.
	"""
	fields = compat.existing_fields(ASSET_REGISTER, _CHILD_FIELDS)
	out: list[dict] = []
	seen = {asset_name}
	frontier = [asset_name]
	truncated = False

	for _ in range(CASCADE_DEPTH):
		if not frontier:
			break
		rows = frappe.db.get_all(
			ASSET_REGISTER,
			filters={"location": ("in", frontier)},
			fields=fields,
			order_by="name asc",
			limit=CASCADE_CAP + 1,
		)
		frontier = []
		for row in rows or []:
			name = str(row.get("name"))
			if name in seen:
				continue
			seen.add(name)
			if len(out) >= CASCADE_CAP:
				truncated = True
				break
			out.append(dict(row))
			frontier.append(name)
		if truncated:
			break
	else:
		truncated = truncated or bool(frontier)

	return out, truncated


def _write_state_change(
	row: dict,
	asset_type: str,
	action: str,
	from_state: str,
	to_state: str,
	args: dict,
	cascaded_from: str = "",
) -> str:
	"""Move one asset to `to_state` and file the log row that says so.

	The primary action and every record the cascade reaches go through here, so a
	valve shut by hand and a valve shut because the main above it closed leave
	the same shape of evidence — same action name, same from/to pair, same
	timestamp column. `get_irrigation_runtime` pairs on that shape, and a cascade
	that filed a differently-named action would drop the closing half of every
	run it touched and report the water still running.

	WHAT SEPARATES THE TWO IS `cascaded_from`, a column rather than a wording:
	the row names the asset whose closure caused it. The sentence goes in `notes`
	as well, because a site that has not migrated the column would otherwise have
	no trace at all of why a valve nobody visited changed state.

	THE METER READING IS VALIDATED BEFORE THE STATE MOVES, and that ordering is
	the point of doing it here at all. A reading below the one already on record
	is a typo or a swapped instrument, and refusing it AFTER writing the state
	change would leave a tractor checked in with no hours on it and nothing to
	say why. See `engine_hours.apply_reading`.

	A CASCADED ROW NEVER CARRIES A READING, for the reason it carries no GPS fix
	and no photograph: nobody was standing at that machine.
	"""
	from . import engine_hours

	meter = {} if cascaded_from else engine_hours.apply_reading(row, asset_type, action, args)

	# ONE TIMESTAMP FOR BOTH WRITES, read once. The register's `last_state_change`
	# says when the state it is showing was reached and the log row says the same
	# thing about the same event; two calls to `now()` would put them a fraction
	# apart, and a screen that reconciles the cached column against the log it
	# caches would report a valve whose register disagrees with its own history.
	stamp = frappe.utils.now()

	doc = frappe.get_doc(ASSET_REGISTER, row["name"])
	doc.current_state = json.dumps({"state": to_state})
	# A CACHE OF THE LOG, NOT A SECOND ACCOUNT OF IT — the same relationship
	# `current_state` beside it has, and the same one `engine_hours` describes at
	# length for the hour meter. Runtime is still summed from the Asset State Log;
	# this column exists so a list of forty valves can say "open since 06:12"
	# without forty queries behind a screen a worker is waiting on. Written only
	# where the site has migrated the column.
	if compat.has_field(ASSET_REGISTER, "last_state_change"):
		doc.last_state_change = stamp
	doc.save(ignore_permissions=True)

	log = frappe.new_doc(ASSET_STATE_LOG)
	log.asset_name = row["name"]
	log.asset_type = asset_type
	log.action = action
	log.from_state = from_state
	log.to_state = to_state
	log.performed_by = as_str(args, "performed_by") or (
		frappe.session.user if hasattr(frappe, "session") else None
	)
	log.performed_at = stamp

	notes = as_str(args, "notes")
	if cascaded_from:
		origin = (
			f"Closed automatically when {cascaded_from} was closed — this valve is downstream "
			f"of it and no water can reach it."
		)
		notes = f"{origin} {notes}".strip() if notes else origin
		if compat.has_field(ASSET_STATE_LOG, "cascaded_from"):
			log.cascaded_from = cascaded_from
	log.notes = notes

	# GPS AND THE PHOTO STAY ON THE RECORD THE WORKER WAS STANDING AT. A fix
	# taken at the turnout is not where the lateral three hundred yards away is,
	# and a photograph of the main is not a photograph of it either. A cascaded
	# row carries the time, the transition and its cause; it does not claim
	# somebody was there.
	if not cascaded_from:
		gps_lat = args.get("gps_lat") or args.get("gps_latitude")
		gps_lon = args.get("gps_lon") or args.get("gps_longitude")
		if gps_lat is not None:
			try:
				log.gps_latitude = float(gps_lat)
			except (TypeError, ValueError):
				pass
		if gps_lon is not None:
			try:
				log.gps_longitude = float(gps_lon)
			except (TypeError, ValueError):
				pass

		photo = as_str(args, "photo_file_token")
		if photo:
			log.photo = photo

	if meter:
		log.engine_hours = meter["engine_hours"]
		if meter["hours_used"] is not None and compat.has_field(ASSET_STATE_LOG, "hours_used"):
			log.hours_used = meter["hours_used"]
		if meter["meter_note"]:
			log.notes = f"{log.notes} {meter['meter_note']}".strip() if log.notes else meter["meter_note"]

	log.insert(ignore_permissions=True)

	# The register's cached reading follows the log, never the other way round —
	# see `engine_hours` on why the series is the record and the column is a
	# screen. Written after the insert so a cache can never be ahead of the row
	# it caches.
	if meter:
		engine_hours.cache_reading(row["name"], meter["engine_hours"], reset=meter["meter_reset"])

	return log.name


def _cascade(root: dict, action: str, args: dict) -> dict:
	"""Apply `action` to every descendant of `root` that can take it.

	EVERY DESCENDANT IS ACCOUNTED FOR, applied or skipped with the reason. A
	cascade that reported only what it changed would leave the interesting case
	invisible: the valve that was already `winterized`, or retired, or is a
	pump rather than a valve, is precisely the one somebody needs to know the
	main did not shut — and "12 valves closed" out of fourteen children says
	nothing about the other two.
	"""
	descendants, truncated = _descendants(root["name"])
	applied, skipped = [], []

	for child in descendants:
		child_type = child.get("asset_type") or "General"
		entry = {"asset_name": child.get("name"), "asset_type": child_type}

		if child.get("retired_at"):
			skipped.append({**entry, "reason": "retired"})
			continue

		defn = _STATE_DEFINITIONS.get(child_type)
		rule = (defn or {}).get("actions", {}).get(action)
		if not rule:
			skipped.append({**entry, "reason": f"{child_type!r} has no {action!r} action"})
			continue

		current = _current_state_value(child.get("current_state")) or defn["default"]
		if current not in rule["from"]:
			skipped.append({**entry, "reason": f"already {current!r}"})
			continue

		log_name = _write_state_change(
			child, child_type, action, current, rule["to"], args, cascaded_from=root["name"]
		)
		applied.append(
			{
				**entry,
				"from_state": current,
				"to_state": rule["to"],
				"log_name": log_name,
			}
		)

	return {
		"cascaded": applied,
		"cascaded_count": len(applied),
		"cascade_skipped": skipped,
		"cascade_truncated": truncated,
	}


# ── get_available_actions ─────────────────────────────────────────────────
def get_available_actions(args: dict) -> ToolResult:
	"""What can be done to this asset right now, given its type and current state."""
	_require()
	name = as_str(args, "asset_name", required=True)
	row = asset_row(name)
	asset_type = row.get("asset_type") or "General"
	current = _current_state_value(row.get("current_state"))

	defn = _STATE_DEFINITIONS.get(asset_type, _STATE_DEFINITIONS["General"])
	effective = current or defn["default"]
	actions = _actions_for(asset_type, effective)

	all_states = set()
	for rule in defn["actions"].values():
		all_states.update(rule["from"])
		all_states.add(rule["to"])

	from . import asset_actions

	menu = asset_actions.menu_for(asset_type, effective)
	return ToolResult(
		data={
			"asset_name": row["name"],
			"asset_type": asset_type,
			"current_state": effective,
			"available_actions": actions,
			"all_states": sorted(all_states),
			# v0.77.0. The state machine answers "what transitions are legal";
			# this answers "what does a worker standing at this machine do",
			# which is a longer list — an inspection and a calibration are
			# neither transitions nor absent from the job.
			"action_menu": menu,
		},
		summary=f"{row['name']}: {len(actions)} available action(s) from {effective!r}",
	)


# ── log_asset_state_change ───────────────────────────────────────────────
def log_asset_state_change(args: dict) -> ToolResult:
	"""Perform a state-change action on an asset.

	Validates the transition, updates current_state on the Asset Register record,
	and writes an Asset State Log entry.

	SOME ACTIONS REACH FURTHER THAN THE ASSET SCANNED. Closing an irrigation
	valve closes every valve below it on the line, because that is what shutting
	an upstream valve does to the water — see `_CASCADING_ACTIONS` for why this
	one direction and no other. The cascade is reported in full (`cascaded`,
	`cascade_skipped`) rather than summarised: a worker who shut a main to fix a
	line break is owed the list of what went dry with it.

	THE PRIMARY TRANSITION IS VALIDATED BEFORE ANYTHING IS WRITTEN, cascade
	included. A refused action leaves the register exactly as it found it.
	"""
	_require()
	compat.require_doctype(
		ASSET_STATE_LOG,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)

	name = as_str(args, "asset_name", required=True)
	action = as_str(args, "action", required=True)
	row = asset_row(name)
	asset_type = row.get("asset_type") or "General"
	current = _current_state_value(row.get("current_state"))

	defn = _STATE_DEFINITIONS.get(asset_type, _STATE_DEFINITIONS["General"])
	effective = current or defn["default"]

	action_def = defn["actions"].get(action)
	if not action_def:
		valid = sorted(defn["actions"].keys())
		raise ToolError(
			f"{action!r} is not a valid action for asset type {asset_type!r}. "
			f"Valid actions: {', '.join(valid)}."
		)

	if effective not in action_def["from"]:
		available = _actions_for(asset_type, effective)
		available_names = [a["action"] for a in available]
		raise ToolError(
			f"Cannot {action!r} from state {effective!r}. "
			f"Available actions in this state: {', '.join(available_names) or 'none'}."
		)

	to_state = action_def["to"]

	log_name = _write_state_change(row, asset_type, action, effective, to_state, args)
	log_row = (
		frappe.db.get_value(
			ASSET_STATE_LOG,
			log_name,
			compat.existing_fields(
				ASSET_STATE_LOG, ("performed_by", "performed_at", "engine_hours", "hours_used")
			),
			as_dict=True,
		)
		or {}
	)

	cascade = (
		_cascade(row, action, args)
		if action in _CASCADING_ACTIONS.get(asset_type, ())
		else {"cascaded": [], "cascaded_count": 0, "cascade_skipped": [], "cascade_truncated": False}
	)

	summary = f"{row['name']}: {action} ({effective} → {to_state})"
	if cascade["cascaded_count"]:
		summary += f", cascaded to {cascade['cascaded_count']} downstream asset(s)"

	# WHICH six o'clock. The stored column is naive and always has been; the
	# `_local` twin beside it carries the offset, so "the valve went off at 18:12"
	# is a sentence a worker can check against their own watch. See
	# `erpnext_mcp/timezones.py` for why the stored value is left alone.
	clock = timezones.Renderer(args)
	data = {
		"asset_name": row["name"],
		"asset_type": asset_type,
		"action": action,
		"from_state": effective,
		"to_state": to_state,
		"log_name": log_name,
		"performed_by": log_row.get("performed_by"),
		"performed_at": str(log_row.get("performed_at") or ""),
		# The meter, where one was read. Present as explicit nulls rather than
		# absent keys so a handset renders one card for every state change
		# instead of testing which shape arrived.
		"engine_hours": (
			round(float(log_row["engine_hours"]), 1) if log_row.get("engine_hours") is not None else None
		),
		"hours_used": (round(float(log_row["hours_used"]), 1) if log_row.get("hours_used") else None),
		**cascade,
		**clock.block(),
	}
	clock.add(data, "performed_at")
	if data["engine_hours"] is not None:
		summary += f", meter {data['engine_hours']} h"
	if data["hours_used"] is not None:
		summary += f" ({data['hours_used']} h this session)"

	return ToolResult(
		data=data,
		summary=summary,
		docstatus_delta="0 → 0 (updated)",
	)


# ── list_asset_state_history ─────────────────────────────────────────────
def list_asset_state_history(args: dict) -> ToolResult:
	"""Chronological state-change log for one asset."""
	_require()
	name = as_str(args, "asset_name", required=True)
	row = asset_row(name)
	limit = as_limit(args)

	if not compat.doctype_exists(ASSET_STATE_LOG):
		return ToolResult(
			data={"asset_name": row["name"], "event_count": 0, "events": []},
			summary=f"{row['name']}: no state log (Asset State Log doctype not installed)",
		)

	fields = compat.existing_fields(
		ASSET_STATE_LOG,
		(
			"name",
			"action",
			"from_state",
			"to_state",
			"performed_by",
			"performed_at",
			"notes",
			"cascaded_from",
			"gps_latitude",
			"gps_longitude",
			"photo",
			"asset_type",
			"creation",
		),
	)

	rows = frappe.db.get_all(
		ASSET_STATE_LOG,
		filters={"asset_name": row["name"]},
		fields=fields,
		order_by="creation desc",
		limit=limit,
	)

	clock = timezones.Renderer(args)
	events = []
	for r in rows or []:
		events.append(
			{
				"log_name": r.get("name"),
				"action": r.get("action"),
				"from_state": r.get("from_state"),
				"to_state": r.get("to_state"),
				"performed_by": r.get("performed_by") or None,
				"performed_at": str(r.get("performed_at") or r.get("creation") or ""),
				"notes": r.get("notes") or None,
				# Present on every event rather than only the cascaded ones: a reader
				# working out whether somebody was actually at this valve needs the
				# answer on each row, not the absence of a key on most of them.
				"cascaded_from": r.get("cascaded_from") or None,
				"cascaded": bool(r.get("cascaded_from")),
				"gps_latitude": round(float(r.get("gps_latitude") or 0), 7) or None,
				"gps_longitude": round(float(r.get("gps_longitude") or 0), 7) or None,
				"photo": r.get("photo") or None,
			}
		)
		clock.add(events[-1], "performed_at")

	return ToolResult(
		data={
			"asset_name": row["name"],
			"asset_type": row.get("asset_type"),
			"event_count": len(events),
			"events": events,
			**clock.block(),
		},
		summary=f"{row['name']}: {len(events)} state change(s)",
	)
