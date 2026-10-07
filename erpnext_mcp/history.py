# SPDX-License-Identifier: MIT
"""One history engine for anything with records pointing at it. v0.269.0.

`tools/asset_tags._asset_history` was a list of (doctype, link column, columns) read and merged by time. A block's
history is the same shape with more ways to point (a Dynamic Link, a child-table row, a valve in one of its zones),
so the engine is here and both use it: a SOURCE says what to read and how it points; `collect` reads every source,
merges newest first and pages with `before`. Nothing here writes.
"""

from __future__ import annotations

import frappe

from . import compat

FIELD = "Field"
#: The Farm Task states that end a task — the history shows these; open tasks are on the card.
TASK_DONE = frozenset({"Completed", "Rejected", "Cancelled", "Merged"})

#: Kinds a worker or crew lead never sees (Tim: operational history only — no pay, costs or HR).
SENSITIVE_KINDS = frozenset({"cost"})
#: Roles that read the sensitive kinds.
SENSITIVE_ROLES = frozenset({"Farm Manager", "Accounts Manager", "Accounts User", "HR Manager", "HR User",
                             "System Manager"})


def _text(value) -> str:
	return str(value or "").strip()


def _when(row: dict, date_field: str) -> str:
	return _text(row.get(date_field) or row.get("creation"))[:19]


# ── how a record points at a block ───────────────────────────────────────────
def _link(doctype, column, key, fields, filters=None, limit=200):
	if not compat.doctype_exists(doctype) or not compat.has_field(doctype, column):
		return []
	cols = compat.existing_fields(doctype, list(dict.fromkeys(["name", "creation", *fields])))
	return frappe.db.get_all(doctype, filters={column: key, **(filters or {})}, fields=cols,
	                         order_by="creation desc", limit=limit) or []


def _dynamic(doctype, dt_column, name_column, key, fields, filters=None, limit=200):
	if not compat.doctype_exists(doctype) or not compat.has_field(doctype, name_column):
		return []
	where = {name_column: key, **(filters or {})}
	if compat.has_field(doctype, dt_column):
		where[dt_column] = FIELD
	cols = compat.existing_fields(doctype, list(dict.fromkeys(["name", "creation", *fields])))
	return frappe.db.get_all(doctype, filters=where, fields=cols, order_by="creation desc", limit=limit) or []


def _via_child(doctype, child, child_column, key, fields, limit=200):
	if not compat.doctype_exists(doctype) or not compat.doctype_exists(child):
		return []
	parents = frappe.db.get_all(child, filters={child_column: key, "parenttype": doctype}, pluck="parent",
	                            limit=limit) or []
	if not parents:
		return []
	cols = compat.existing_fields(doctype, list(dict.fromkeys(["name", "creation", *fields])))
	return frappe.db.get_all(doctype, filters={"name": ("in", sorted(set(parents)))}, fields=cols,
	                         order_by="creation desc", limit=limit) or []


# ── a block's sources ────────────────────────────────────────────────────────
def _field_events(field: str, limit: int) -> list:
	ev: list = []

	def add(kind, row, date_field, title, detail, doctype):
		ev.append({"kind": kind, "when": _when(row, date_field), "title": title, "detail": detail,
		           "doctype": doctype, "docname": row.get("name")})

	for r in _dynamic("Farm Task", "location_doctype", "location", field,
	                  ["task_name", "task_type", "state", "completed_at", "assigned_to_name"], limit=limit):
		if str(r.get("state") or "") in TASK_DONE:
			add("task", r, "completed_at", f"{r.get('task_type') or 'Task'}: {r.get('task_name') or r['name']}",
			    f"{r.get('state')}" + (f" — {r.get('assigned_to_name')}" if r.get("assigned_to_name") else ""), "Farm Task")
	for r in _via_child("Spray Application", "Spray Application Block", "block", field,
	                    ["status", "tank_mix", "gallons_per_acre", "rei_hours", "phi_days", "completed_at", "started_at"],
	                    limit=limit):
		add("spray", r, "completed_at", f"Spray: {r.get('tank_mix') or r['name']}",
		    f"{r.get('status') or ''}; {r.get('gallons_per_acre') or '?'} gal/ac; REI {r.get('rei_hours') or '?'} h; "
		    f"PHI {r.get('phi_days') or '?'} d", "Spray Application")
	for r in _dynamic("Crop Observation", "block_doctype", "block", field,
	                  ["observation_type", "observed_on", "threat", "crop_stage", "growth_stage_code", "count_observed",
	                   "percent_affected", "threshold_exceeded", "beneficial_name", "notes"], limit=limit):
		if r.get("crop_stage") or r.get("growth_stage_code"):
			add("phenology", r, "observed_on", f"Stage: {r.get('crop_stage') or r.get('growth_stage_code')}",
			    _text(r.get("notes"))[:140], "Crop Observation")
		else:
			hit = " — THRESHOLD EXCEEDED" if r.get("threshold_exceeded") else ""
			add("ipm", r, "observed_on", f"{r.get('observation_type') or 'Observation'}: "
			    f"{r.get('threat') or r.get('beneficial_name') or ''}".strip(),
			    f"count {r.get('count_observed') or '?'}, {r.get('percent_affected') or 0}% affected{hit}",
			    "Crop Observation")
	for r in _dynamic("Pest Pressure", "block_doctype", "block", field,
	                  ["threat", "status", "last_observed_on", "threshold_exceeded_count", "trend"], limit=limit):
		add("ipm", r, "last_observed_on", f"Pest pressure: {r.get('threat')}",
		    f"{r.get('status') or ''}; trend {r.get('trend') or '?'}; threshold hit {r.get('threshold_exceeded_count') or 0}×",
		    "Pest Pressure")
	for r in _dynamic("Inspection Session", "location_doctype", "location", field,
	                  ["template", "state", "submitted_at", "worker_name"], limit=limit):
		add("inspection", r, "submitted_at", f"Inspection: {r.get('template') or r['name']}",
		    f"{r.get('state') or ''}" + (f" — {r.get('worker_name')}" if r.get("worker_name") else ""), "Inspection Session")
	for r in _link("Scale Ticket", "field", field, ["date", "variety", "net_weight", "weight_uom", "grade"], limit=limit):
		add("harvest", r, "date", f"Scale ticket {r['name']}",
		    f"{r.get('variety') or ''} {r.get('grade') or ''}: {r.get('net_weight') or '?'} {r.get('weight_uom') or ''}".strip(),
		    "Scale Ticket")
	for r in _link("Traceability Lot Code", "field", field, ["harvest_date", "quantity", "quantity_uom", "variety"],
	               limit=limit):
		add("harvest", r, "harvest_date", f"Harvest lot {r['name']}",
		    f"{r.get('variety') or ''} {r.get('quantity') or '?'} {r.get('quantity_uom') or ''}".strip(), "Traceability Lot Code")
	for r in _link("Planting Season", "field", field, ["season_label", "status", "variety", "plant_year", "productive_from",
	                                                    "removed_on", "removal_reason", "actual_yield", "yield_uom"],
	               limit=limit):
		if r.get("removed_on"):
			add("planting", r, "removed_on", f"Removed: {r.get('variety') or r.get('season_label') or ''}",
			    _text(r.get("removal_reason")), "Planting Season")
		add("planting", r, "productive_from", f"Planting: {r.get('variety') or ''} ({r.get('plant_year') or '?'})",
		    f"{r.get('status') or ''}" + (f"; yield {r.get('actual_yield')} {r.get('yield_uom') or ''}" if r.get("actual_yield") else ""),
		    "Planting Season")
	for doctype, date_field, title in (("Water Test", "test_date", "Water test"), ("Monitoring Record", "monitoring_date",
	                                                                              "Food-safety check")):
		for r in _link(doctype, "block", field, [date_field, "notes"], limit=limit):
			add("food_safety", r, date_field, f"{title} {r['name']}", _text(r.get("notes"))[:140], doctype)
	# Irrigation: a valve opened or shut in one of this block's zones.
	if compat.doctype_exists("Irrigation Zone"):
		zones = frappe.db.get_all("Irrigation Zone", filters={"field": field}, pluck="name", limit=200) or []
		valves = (frappe.db.get_all("Asset Register", filters={"irrigation_zone": ("in", zones)}, pluck="name", limit=500)
		          if zones and compat.has_field("Asset Register", "irrigation_zone") else []) or []
		for valve in valves:
			for r in _link("Asset State Log", "asset_name", valve, ["action", "to_state", "performed_at", "performed_by"],
			               limit=limit):
				add("irrigation", r, "performed_at", f"Valve {valve}: {r.get('to_state') or r.get('action')}",
				    _text(r.get("performed_by")), "Asset State Log")
	# The block itself: comments (incl. the acreage change log) and attached photos.
	for r in _link("Comment", "reference_name", field, ["content", "comment_type", "comment_email"],
	               filters={"reference_doctype": FIELD}, limit=limit):
		kind = "record" if str(r.get("comment_type")) == "Info" else "note"
		add(kind, r, "creation", "Change to the block" if kind == "record" else "Note",
		    _text(frappe.utils.strip_html(r.get("content") or "") if hasattr(frappe.utils, "strip_html") else r.get("content"))[:300],
		    "Comment")
	for r in _link("File", "attached_to_name", field, ["file_name", "file_url"], filters={"attached_to_doctype": FIELD},
	               limit=limit):
		add("note", r, "creation", f"Photo / file: {r.get('file_name') or r['name']}", _text(r.get("file_url")), "File")
	for doctype, title in (("Block Cost Entry", "Cost"), ("Block Revenue Entry", "Revenue")):
		for r in _link(doctype, "field", field, ["posting_date", "amount", "cost_category", "description"], limit=limit):
			add("cost", r, "posting_date", f"{title}: {r.get('cost_category') or ''}".strip(),
			    f"{r.get('amount') or 0} — {_text(r.get('description'))[:100]}", doctype)
	return ev


def field_history(field: str, *, types=None, from_date="", to_date="", season=None, limit=50, before="",
                  include_sensitive=False) -> dict:
	"""A block's events, newest first. `before` (an ISO time) pages back; `season` = that calendar year."""
	limit = max(1, min(int(limit or 50), 500))
	wanted = {t.strip() for t in (types or []) if t and t.strip()}
	events = _field_events(field, limit=max(limit * 2, 200))
	if season:
		from_date, to_date = f"{int(season)}-01-01", f"{int(season)}-12-31"
	out = []
	for e in events:
		if not include_sensitive and e["kind"] in SENSITIVE_KINDS:
			continue
		if wanted and e["kind"] not in wanted:
			continue
		day = e["when"][:10]
		if from_date and day < str(from_date)[:10]:
			continue
		if to_date and day > str(to_date)[:10]:
			continue
		if before and e["when"] >= str(before):
			continue
		out.append(e)
	out.sort(key=lambda e: (e["when"], e["docname"] or ""), reverse=True)
	page = out[:limit]
	return {
		"field": field,
		"events": page,
		"count": len(page),
		"more": len(out) > limit,
		"before": page[-1]["when"] if len(out) > limit and page else None,
		"kinds": sorted({e["kind"] for e in page}),
		"sensitive_included": bool(include_sensitive),
	}


def may_see_sensitive(roles) -> bool:
	return bool(set(roles or ()) & SENSITIVE_ROLES)
