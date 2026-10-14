# SPDX-License-Identifier: MIT
"""Every Housing Unit has a tag on the Asset Register. v0.322.0.

THE BUG. Tim registered two cabins at Mill Creek from the phone (`mobile:create_housing_unit`, 2026-10-11) and they
never appeared on the phone's map or asset lists. Those read the Asset Register, which is the one register for
anything that carries a tag, a QR and a place on the map; `create_housing_unit` wrote only the Housing Unit. A building
existed for the camp's paperwork and not for anybody walking the camp.

THE FIX IS THE REPEATABLE ASSET PATTERN, NOT A SECOND MAP SOURCE. Teaching the map, the asset list, the offline pack,
the QR sheet and the scan history each to read Housing Units as well would be five readers to keep in step forever.
Instead the Housing Unit controller pairs every unit with one Asset Register row — type "Housing Unit", the same
company, the same GPS, the docname as the printable tag ID (so the QR comes with it) — whichever door created it:
the Desk form, `create_housing_unit` over MCP, or the phone. The unit carries the link in `asset_tag`.

WHAT IT NEVER DOES. It never blocks saving a unit: a failed pairing is logged and retried on the next save and by
the patch, because losing a cabin's registration over its tag would be the wrong way round. It never creates a
second tag for a unit that has one. It adopts an Asset Register row that already carries the unit's name and a
building type, rather than minting a duplicate next to it. And it never copies the unit's NFC `tag_uuid`: one
sticker resolving to two records is how a scan answers with the wrong thing.

WHAT IT KEEPS IN STEP. On every save of the unit: company (from the parcel), GPS (when the unit has a fix), and the
description. Deleting a unit retires its tag rather than deleting it — the tag's scan and task history are records.
"""

from __future__ import annotations

import frappe

from . import compat

HOUSING_UNIT = "Housing Unit"
ASSET_REGISTER = "Asset Register"
ASSET_TYPE = "Housing Unit"
LINK = "asset_tag"
#: An existing Asset Register row under the unit's name is adopted only if it is a building.
ADOPTABLE_TYPES = ("Housing Unit", "Cabin", "House", "Bath House", "Toilet-Shower", "Kitchen", "Barn", "Shop",
                   "Storage")


def ready() -> bool:
	return (
		compat.doctype_exists(HOUSING_UNIT)
		and compat.doctype_exists(ASSET_REGISTER)
		and compat.has_field(HOUSING_UNIT, LINK)
	)


def describe(unit: dict) -> str:
	parts = [str(unit.get("unit_type") or "Housing unit")]
	if unit.get("capacity"):
		parts.append(f"sleeps {int(unit.get('capacity') or 0)}")
	text = f"{', '.join(parts)} — Housing Unit {unit.get('name')}"
	if unit.get("parcel"):
		text += f" on {unit.get('parcel')}"
	return text


def ensure(unit_name: str) -> dict:
	"""Pair one Housing Unit with its Asset Register tag: link, adopt or create; then keep it in step.

	Returns `{"unit", "asset", "outcome": linked|adopted|created|updated|unchanged|skipped, "reason"?}`. Never raises.
	"""
	try:
		return _ensure(unit_name)
	except Exception as exc:  # noqa: BLE001 — a tag must never cost the unit its save
		frappe.log_error(title=f"Housing Unit {unit_name}: asset tag", message=frappe.get_traceback())
		return {"unit": unit_name, "asset": None, "outcome": "skipped", "reason": str(exc)[:200]}


def _ensure(unit_name: str) -> dict:
	if not ready():
		return {"unit": unit_name, "asset": None, "outcome": "skipped", "reason": "not migrated yet"}
	fields = compat.existing_fields(HOUSING_UNIT, ("name", "unit_type", "parcel", "owning_entity", "capacity",
	                                               "gps_latitude", "gps_longitude", LINK, "occupied", "door_label",
	                                               "room_labels"))
	unit = dict(frappe.db.get_value(HOUSING_UNIT, unit_name, fields, as_dict=True) or {})
	if not unit:
		return {"unit": unit_name, "asset": None, "outcome": "skipped", "reason": "no such unit"}

	asset = str(unit.get(LINK) or "")
	outcome = "unchanged"
	if asset and not frappe.db.exists(ASSET_REGISTER, asset):
		asset = ""  # a dangling link (the tag was deleted in the Desk): pair again
	if not asset:
		asset, outcome = _adopt_or_create(unit)
		frappe.db.set_value(HOUSING_UNIT, unit["name"], LINK, asset, update_modified=False)
	changed = _keep_in_step(asset, unit)
	if outcome == "unchanged" and changed:
		outcome = "updated"
	return {"unit": unit["name"], "asset": asset, "outcome": outcome}


def _adopt_or_create(unit: dict) -> tuple:
	name = str(unit["name"])
	claimed_by = lambda tag: frappe.db.get_value(HOUSING_UNIT, {LINK: tag, "name": ("!=", name)}, "name")  # noqa: E731
	if frappe.db.exists(ASSET_REGISTER, name):
		existing_type = frappe.db.get_value(ASSET_REGISTER, name, "asset_type")
		if existing_type in ADOPTABLE_TYPES and not claimed_by(name):
			return name, "adopted"
		name = _free_name(f"{name} (housing)")
	doc = frappe.new_doc(ASSET_REGISTER)
	doc.__newname = name
	doc.asset_type = _asset_type()
	doc.company = unit.get("owning_entity") or None
	doc.description = describe(unit)
	latitude, longitude = float(unit.get("gps_latitude") or 0), float(unit.get("gps_longitude") or 0)
	if latitude and longitude:
		doc.gps_latitude, doc.gps_longitude = latitude, longitude
	if compat.has_field(ASSET_REGISTER, "occupied"):
		# v0.327.0: the unit's own answer. This used to be "has beds", which is how MC-CABIN-6&6 and 1&2 came to be
		# Occupied on the asset and not on the unit.
		doc.occupied = 1 if int(unit.get("occupied") or 0) else 0
	doc.insert(ignore_permissions=True)
	return doc.name, "created"


def _free_name(base: str) -> str:
	if not frappe.db.exists(ASSET_REGISTER, base):
		return base
	n = 2
	while frappe.db.exists(ASSET_REGISTER, f"{base} {n}"):
		n += 1
	return f"{base} {n}"


def _asset_type() -> str:
	""""Housing Unit" — seeded by asset_types since v0.230; created here if a site lacks the row."""
	from . import asset_types

	try:
		return asset_types.require(ASSET_TYPE, "asset_type", creating=True)
	except Exception:  # noqa: BLE001
		return ASSET_TYPE


def _keep_in_step(asset: str, unit: dict) -> bool:
	row = dict(frappe.db.get_value(ASSET_REGISTER, asset, compat.existing_fields(
		ASSET_REGISTER, ("company", "gps_latitude", "gps_longitude", "description", "retired_at", "occupied",
		                 "door_label", "room_labels")), as_dict=True) or {})
	updates: dict = {}
	# v0.327.0. A unit saved is the unit's answer: occupied, the door label and the rooms follow to its tag.
	for field in ("occupied", "door_label", "room_labels"):
		if field in unit and compat.has_field(ASSET_REGISTER, field):
			mine, theirs = unit.get(field), row.get(field)
			if field == "occupied":
				mine, theirs = int(mine or 0), int(theirs or 0)
			else:
				mine, theirs = mine or None, theirs or None
			if mine != theirs:
				updates[field] = mine
	if unit.get("owning_entity") and row.get("company") != unit.get("owning_entity"):
		updates["company"] = unit["owning_entity"]
	latitude, longitude = float(unit.get("gps_latitude") or 0), float(unit.get("gps_longitude") or 0)
	if latitude and longitude and (float(row.get("gps_latitude") or 0), float(row.get("gps_longitude") or 0)) != (
		latitude, longitude
	):
		updates["gps_latitude"], updates["gps_longitude"] = latitude, longitude
	# The description is refreshed only while it is still the one written here; somebody's own words stand.
	wanted, current = describe(unit), str(row.get("description") or "")
	if (not current or "— Housing Unit " in current) and current != wanted:
		updates["description"] = wanted
	if row.get("retired_at") and compat.has_field(ASSET_REGISTER, "retired_at"):
		updates["retired_at"] = None  # the unit is live again, so is its tag
	if updates:
		frappe.db.set_value(ASSET_REGISTER, asset, updates)
	return bool(updates)


def retire(unit_name: str, asset: str) -> None:
	"""The unit was deleted: retire its tag (kept, with its history), unless another unit still uses it."""
	try:
		if not asset or not frappe.db.exists(ASSET_REGISTER, asset):
			return
		if frappe.db.get_value(HOUSING_UNIT, {LINK: asset, "name": ("!=", unit_name)}, "name"):
			return
		if compat.has_field(ASSET_REGISTER, "retired_at"):
			frappe.db.set_value(ASSET_REGISTER, asset, "retired_at", frappe.utils.today())
	except Exception:  # noqa: BLE001
		frappe.log_error(title=f"Housing Unit {unit_name}: retire asset tag", message=frappe.get_traceback())


def backfill() -> dict:
	"""Pair every Housing Unit that has no tag (or a dangling one). Idempotent: a second run changes nothing."""
	report = {"units": 0, "created": [], "adopted": [], "skipped": []}
	if not ready():
		return report
	for name in frappe.db.get_all(HOUSING_UNIT, pluck="name", limit=100000) or []:
		report["units"] += 1
		result = ensure(str(name))
		if result["outcome"] in ("created", "adopted"):
			report[result["outcome"]].append({"unit": result["unit"], "asset": result["asset"]})
		elif result["outcome"] == "skipped":
			report["skipped"].append({"unit": result["unit"], "reason": result.get("reason")})
	return report


# ── v0.327.0: occupied, kept in step and checked ───────────────────────────
def follow_asset(asset_doc) -> None:
	"""Asset Register on_update: a cabin's tag marked occupied (or not) marks its unit the same. Never raises."""
	try:
		if not ready() or not compat.has_field(ASSET_REGISTER, "occupied"):
			return
		unit = frappe.db.get_value(HOUSING_UNIT, {LINK: asset_doc.name}, ["name", "occupied"], as_dict=True)
		if unit and int(unit.get("occupied") or 0) != int(asset_doc.get("occupied") or 0):
			frappe.db.set_value(HOUSING_UNIT, unit["name"], "occupied", int(asset_doc.get("occupied") or 0),
			                    update_modified=False)
	except Exception:  # noqa: BLE001
		frappe.log_error(title=f"Asset {asset_doc.name}: occupied → housing unit", message=frappe.get_traceback())


def _assigned(units: list) -> dict:
	if not units or not compat.doctype_exists("Housing Assignment"):
		return {}
	out: dict = {}
	for row in frappe.db.get_all("Housing Assignment", filters={"unit": ("in", units), "status": "Current"},
	                             fields=["unit"], limit=100000) or []:
		out[row["unit"]] = out.get(row["unit"], 0) + 1
	return out


def occupancy(units: list | None = None, company: str = "") -> list:
	"""Each unit's occupied, its tag's occupied and its active assignments, with whether they disagree."""
	filters: dict = {}
	if units:
		filters["name"] = ("in", list(units))
	if company:
		filters["owning_entity"] = company
	rows = frappe.db.get_all(HOUSING_UNIT, filters=filters, fields=["name", "unit_name", "occupied", LINK],
	                         order_by="unit_name asc", limit=100000) or []
	tags = {r[LINK] for r in rows if r.get(LINK)}
	asset_occupied = {a["name"]: int(a.get("occupied") or 0) for a in frappe.db.get_all(
		ASSET_REGISTER, filters={"name": ("in", list(tags))}, fields=["name", "occupied"], limit=100000) or []} \
		if tags and compat.has_field(ASSET_REGISTER, "occupied") else {}
	assigned = _assigned([r["name"] for r in rows])
	out = []
	for r in rows:
		unit_occ = int(r.get("occupied") or 0)
		tag_occ = asset_occupied.get(r.get(LINK)) if r.get(LINK) else None
		people = assigned.get(r["name"], 0)
		out.append({"unit": r["name"], "unit_name": r.get("unit_name"), "asset": r.get(LINK) or None,
		            "unit_occupied": bool(unit_occ), "asset_occupied": None if tag_occ is None else bool(tag_occ),
		            "active_assignments": people,
		            "mismatch": (tag_occ is not None and tag_occ != unit_occ)})
	return out


SOURCES = ("unit", "asset", "assignments")


def sync_occupancy(units: list | None, source: str, *, dry_run: bool = True, company: str = "") -> dict:
	"""Make each unit and its tag agree, taking `source` as the truth. Returns before / after per unit."""
	if source not in SOURCES:
		raise ValueError(f"sync_occupancy_from is one of: {', '.join(SOURCES)}.")
	changes = []
	for row in occupancy(units, company):
		if source == "unit":
			want = row["unit_occupied"]
		elif source == "asset":
			want = row["asset_occupied"] if row["asset_occupied"] is not None else row["unit_occupied"]
		else:
			want = row["active_assignments"] > 0
		before = {"unit": row["unit_occupied"], "asset": row["asset_occupied"]}
		if before["unit"] == want and before["asset"] in (want, None):
			continue
		changes.append({"unit": row["unit"], "asset": row["asset"], "before": before, "after": want,
		                "active_assignments": row["active_assignments"]})
		if not dry_run:
			frappe.db.set_value(HOUSING_UNIT, row["unit"], "occupied", int(want), update_modified=False)
			if row["asset"]:
				frappe.db.set_value(ASSET_REGISTER, row["asset"], "occupied", int(want), update_modified=False)
	return {"source": source, "dry_run": dry_run, "changes": changes, "changed": len(changes)}
