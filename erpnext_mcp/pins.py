# SPDX-License-Identifier: MIT
"""Points on the farm map that belong to a piece of work. v0.286.0 (pattern P3;
docs/design/plan_patterns_consolidation_2026_10.md §1).

A job's entrance, delivery spot and loading area, and a pollination season's hive drops, were columns on Contractor
Job and JSON inside its `operation`. They are now rows of one Field Pin table — kind, label, point, block, what it is
for, a key within that, and what it holds — so every map reads them the same way and a new kind of pin is a Select
option, not a schema change.

`layer()` is the ONE map layer: the pins of a job (or of some blocks) together with the valves and hazard markers
inside those blocks. Valves and hazard markers stay assets (Asset Register, `field_card.hazards`); this reads them, it
does not copy them.
"""

from __future__ import annotations

import frappe

PIN = "Field Pin"
KINDS = ("Entrance", "Delivery Spot", "Loading Area", "Hive Drop")
FIELDS = ["name", "pin_kind", "label", "latitude", "longitude", "field", "company", "source_doctype", "source",
          "pin_key", "quantity", "pallets", "notes", "moved_by"]


class PinError(ValueError):
	pass


def _point(lat, lon) -> tuple:
	try:
		lat, lon = float(lat), float(lon)
	except (TypeError, ValueError):
		raise PinError("a pin needs a latitude and a longitude.") from None
	if not (-90 <= lat <= 90 and -180 <= lon <= 180):
		raise PinError(f"{lat}, {lon} is not a point on Earth.")
	return lat, lon


def put(source_doctype: str, source: str, kind: str, lat, lon, *, key: str = "", label: str = "", field: str = "",
        company: str = "", quantity=None, pallets=None, notes: str = "", moved_by: str = "") -> str:
	"""Set the pin of this kind (and key) for `source`, creating it the first time. Returns its name."""
	if kind not in KINDS:
		raise PinError(f"kind is one of: {', '.join(KINDS)}.")
	lat, lon = _point(lat, lon)
	found = frappe.db.get_value(PIN, {"source_doctype": source_doctype, "source": source, "pin_kind": kind,
	                                  "pin_key": key or ""}, "name")
	doc = frappe.get_doc(PIN, found) if found else frappe.new_doc(PIN)
	doc.update({"pin_kind": kind, "latitude": lat, "longitude": lon, "source_doctype": source_doctype, "source": source,
	            "pin_key": key or "", "label": (label or kind)[:140], "field": field or None, "company": company or None,
	            "quantity": None if quantity is None else int(quantity), "pallets": None if pallets is None else int(pallets),
	            "notes": notes or None, "moved_by": moved_by or doc.get("moved_by") or None})
	if found:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
	return doc.name


def of(source_doctype: str, source: str, kind: str = "") -> list:
	"""The pins of one piece of work, in key order."""
	filters = {"source_doctype": source_doctype, "source": source}
	if kind:
		filters["pin_kind"] = kind
	rows = frappe.db.get_all(PIN, filters=filters, fields=FIELDS, order_by="creation asc", limit=5000) or []
	return [dict(r) for r in rows]


def one(source_doctype: str, source: str, kind: str) -> dict | None:
	rows = of(source_doctype, source, kind)
	return rows[0] if rows else None


def clear(source_doctype: str, source: str, kind: str = "") -> int:
	rows = of(source_doctype, source, kind)
	for row in rows:
		frappe.delete_doc(PIN, row["name"], ignore_permissions=True, force=True)
	return len(rows)


def on_blocks(blocks, kinds=()) -> list:
	"""Every pin standing on these blocks, whatever it is for."""
	filters = {"field": ("in", list(blocks) or ["-"])}
	if kinds:
		filters["pin_kind"] = ("in", list(kinds))
	return [dict(r) for r in frappe.db.get_all(PIN, filters=filters, fields=FIELDS, limit=5000) or []]


def layer(*, blocks=(), source_doctype: str = "", source: str = "", include=("pins", "valves", "hazards")) -> list:
	"""The one map layer: [{kind, lat, lon, label, …}] — the work's pins, then the valves and hazard markers (assets)
	standing inside the blocks."""
	from . import field_card

	out = []
	if "pins" in include:
		rows = of(source_doctype, source) if source else on_blocks(blocks)
		out += [{"kind": r["pin_kind"], "lat": r["latitude"], "lon": r["longitude"], "label": r.get("label"),
		         "key": r.get("pin_key") or None, "block": r.get("field"), "quantity": r.get("quantity")} for r in rows]
	seen = set()
	for block in blocks:
		for h in field_card.hazards(block):
			if h["name"] in seen:
				continue
			is_valve = h.get("asset_type") == "Irrigation Valve"
			if (is_valve and "valves" not in include) or (not is_valve and "hazards" not in include):
				continue
			seen.add(h["name"])
			out.append({"kind": "valve" if is_valve else "hazard", "lat": h["latitude"], "lon": h["longitude"],
			            "label": "Valve — protect" if is_valve else (h.get("description") or "Hazard — avoid")[:80],
			            "asset": h["name"], "block": block})
	return out
