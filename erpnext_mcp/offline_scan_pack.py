# SPDX-License-Identifier: MIT
"""What a phone needs to answer a scan with no signal. v0.228.0.

docs/design/offline_mill_creek.md §3. One read, made by Prepare for offline:
every tagged asset and housing unit in the caller's entities — with the tag
UUIDs and aliases that resolve to it — and the live Farm Tasks on each. A scan in
a cabin with no signal then shows what the thing is and what is due on it, "as of"
the time this was taken. The phone also keeps every full scan answer it gets while
online (history included), so a tag scanned once before reads in full.
"""

from __future__ import annotations

import frappe

from . import compat
from .tools import universal_scan

ASSET = "Asset Register"
HOUSING = "Housing Unit"
CAP = 3000


def _rows(doctype: str, fields: tuple, filters: dict) -> list:
	if not compat.doctype_exists(doctype):
		return []
	return [
		dict(r)
		for r in frappe.db.get_all(
			doctype, filters=filters, fields=compat.existing_fields(doctype, fields), limit=CAP
		)
		or []
	]


def _aliases(value) -> list:
	return [line.strip() for line in str(value or "").replace(",", "\n").splitlines() if line.strip()]


def build(companies) -> dict:
	companies = list(companies or [])
	asset_filters = {"company": ("in", companies)} if companies else {}
	assets = _rows(
		ASSET,
		("name", "asset_type", "description", "location", "company", "tag_uuid", "tag_aliases", "retired_at"),
		asset_filters,
	)
	unit_filters = {"owning_entity": ("in", companies)} if companies else {}
	units = _rows(
		HOUSING,
		("name", "unit_name", "unit_type", "parcel", "capacity", "owning_entity", "tag_uuid", "tag_aliases"),
		unit_filters,
	)

	tasks: dict = {}
	if compat.doctype_exists(universal_scan.FARM_TASK):
		fields = compat.existing_fields(universal_scan.FARM_TASK, universal_scan._TASK_FIELDS)
		filters = {"state": ("in", list(universal_scan.LIVE_STATES))}
		if companies and compat.has_field(universal_scan.FARM_TASK, "company"):
			filters["company"] = ("in", companies)
		rows = [
			dict(r)
			for r in frappe.db.get_all(universal_scan.FARM_TASK, filters=filters, fields=fields, limit=CAP)
			or []
		]
		due = universal_scan._alert_due_dates(rows)
		today = universal_scan._today()
		for row in rows:
			keys = set()
			if row.get("location_doctype") in (ASSET, HOUSING) and row.get("location"):
				keys.add(f"{row['location_doctype']}|{row['location']}")
			if row.get("asset"):
				keys.add(f"{ASSET}|{row['asset']}")
			for key in keys:
				tasks.setdefault(key, []).append(universal_scan._describe_task(row, due, today))

	def entry(doctype: str, row: dict, title: str, subtitle: str) -> dict:
		key = f"{doctype}|{row['name']}"
		pending = tasks.get(key, [])
		return {
			"doctype": doctype,
			"name": row["name"],
			"title": title,
			"subtitle": subtitle,
			"tags": [t for t in [str(row.get("tag_uuid") or "").lower()] if t]
			+ [a.lower() for a in _aliases(row.get("tag_aliases"))],
			"retired": bool(row.get("retired_at")),
			"pending_tasks": pending,
			"overdue_task_count": sum(1 for t in pending if t.get("overdue")),
		}

	entities = [
		entry(ASSET, r, r["name"], " · ".join(x for x in (r.get("asset_type"), r.get("location")) if x))
		for r in assets
	] + [
		entry(
			HOUSING,
			r,
			r.get("unit_name") or r["name"],
			" · ".join(
				x for x in (r.get("unit_type"), r.get("parcel"), f"{int(r.get('capacity') or 0)} beds") if x
			),
		)
		for r in units
	]
	return {
		"generated_at": str(frappe.utils.now())[:19],
		"entities": entities,
		"count": len(entities),
		"task_count": sum(len(v) for v in tasks.values()),
	}
