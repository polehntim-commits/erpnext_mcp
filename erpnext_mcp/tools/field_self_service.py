# SPDX-License-Identifier: MIT
"""Fields Tim can find and define himself — the MCP side. v0.269.0 (docs/contracts/field_self_service_v0_269.yaml).

find_fields and get_field_history read; set_field_aliases, rename_field, set_field_acreage and create_task_on_field
write (all OFF until switched on). A name that is more than one block is refused with the candidates, never guessed.
"""

from __future__ import annotations

import frappe

from .. import compat, field_names, history
from ..args import as_bool, as_float, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult

FIELD = "Field"


def _one(query) -> str:
	try:
		return field_names.resolve_one(query)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def find_fields(args: dict) -> ToolResult:
	"""A block (or a named group) by the name people use — candidates when it is ambiguous."""
	query = as_str(args, "name", required=True)
	found = field_names.find(query)
	if found["resolved"]:
		summary = (f"'{query}' is the group {found['group']!r}: " if found["group"] else f"'{query}' is ") + ", ".join(
			found["resolved"])
	elif found["candidates"]:
		summary = f"'{query}' is ambiguous: {len(found['candidates'])} candidates — name one"
	else:
		summary = f"no block answers to '{query}'"
	return ToolResult(data=found, summary=summary)


def get_field_history(args: dict) -> ToolResult:
	"""Everything tied to one block, newest first. Costs are included over MCP (the operator's view)."""
	field = _one(as_str(args, "field", required=True))
	types = [t for t in (as_str(args, "types") or "").split(",") if t.strip()]
	data = history.field_history(
		field, types=types, from_date=as_str(args, "from_date"), to_date=as_str(args, "to_date"),
		season=as_int(args, "season"), limit=as_int(args, "limit", 50), before=as_str(args, "before"),
		include_sensitive=as_bool(args, "include_costs", True),
	)
	return ToolResult(data=data, summary=f"{field}: {data['count']} event(s)" + (" (more)" if data["more"] else ""))


def set_field_aliases(args: dict) -> ToolResult:
	"""Replace, add or remove a block's aliases. The same alias on several blocks makes a named group."""
	field = _one(as_str(args, "field", required=True))
	doc = frappe.get_doc(FIELD, field)
	current = field_names.aliases_of(doc.as_dict())
	if args.get("aliases") is not None:
		new = [a for a in _list(args.get("aliases"))]
	else:
		new = list(current)
		new += [a for a in _list(args.get("add")) if a.casefold() not in {c.casefold() for c in new}]
		drop = {a.casefold() for a in _list(args.get("remove"))}
		new = [a for a in new if a.casefold() not in drop]
	doc.aliases = "\n".join(new)
	doc.save(ignore_permissions=True)
	kept = field_names.aliases_of(doc.as_dict())
	return ToolResult(data={"field": field, "aliases": kept, "was": current},
	                  summary=f"{field}: also known as {', '.join(kept) or 'nothing'}", docstatus_delta="0 → 0 (updated)")


def rename_field(args: dict) -> ToolResult:
	"""A block's name changed (a spelling fixed). The old name is kept as an alias so old references resolve."""
	field = _one(as_str(args, "field", required=True))
	new_name = " ".join(as_str(args, "new_name", required=True).split())
	doc = frappe.get_doc(FIELD, field)
	old_name = doc.field_name
	if new_name == old_name:
		raise ToolError(f"{field} is already called {new_name!r}. Nothing was changed.")
	aliases = field_names.aliases_of(doc.as_dict())
	if old_name and old_name.casefold() not in {a.casefold() for a in aliases}:
		aliases.append(old_name)
	doc.field_name = new_name
	doc.aliases = "\n".join(a for a in aliases if a.casefold() != new_name.casefold())
	doc.save(ignore_permissions=True)
	renamed_to = field
	if as_bool(args, "rename_docname", False):
		from ..abbr import parcel_abbr, suffixed

		target = suffixed(new_name, parcel_abbr(doc.parcel)) if doc.parcel else new_name
		if target != field:
			if frappe.db.exists(FIELD, target):
				raise ToolError(f"{target} already exists; the name was changed but the record id was not.")
			renamed_to = frappe.rename_doc(FIELD, field, target, force=False)
			doc = frappe.get_doc(FIELD, renamed_to)
			aliases = field_names.aliases_of(doc.as_dict())
			if field.casefold() not in {a.casefold() for a in aliases}:
				doc.aliases = "\n".join([*aliases, field])
				doc.save(ignore_permissions=True)
	try:
		doc.add_comment("Info", f"Renamed from {old_name!r} to {new_name!r}" + (f" (record {field} → {renamed_to})" if renamed_to != field else "") + ".")
	except Exception:  # pragma: no cover
		pass
	return ToolResult(
		data={"field": renamed_to, "was": field, "field_name": new_name, "old_name": old_name,
		      "aliases": field_names.aliases_of(frappe.get_doc(FIELD, renamed_to).as_dict())},
		summary=f"{old_name} is now {new_name} (old name kept as an alias)", docstatus_delta="0 → 0 (updated)",
	)


def set_field_acreage(args: dict) -> ToolResult:
	"""From the drawn outline (the default) or a manual figure with a reason. The old value is logged."""
	field = _one(as_str(args, "field", required=True))
	doc = frappe.get_doc(FIELD, field)
	before = round(float(doc.acreage or 0), 2)
	source = (as_str(args, "source") or "Outline").title()
	if source == "Outline":
		if not doc.get("area_computed_acres"):
			raise ToolError(f"{field} has no drawn outline to take acreage from — draw it (set_field_boundary) first, "
			                "or give source=Manual with acres and a reason. Nothing was changed.")
		doc.acreage_source = "Outline"
		doc.acreage = round(float(doc.area_computed_acres), 2)
	elif source == "Manual":
		raw = args.get("acres")
		acres = as_float(raw, "acres") if raw not in (None, "") else None
		reason = as_str(args, "reason")
		if acres is None or acres < 0 or not reason:
			raise ToolError("a manual acreage needs acres (≥ 0) and a reason. Nothing was changed.")
		doc.acreage_source = "Manual"
		doc.acreage_override_reason = reason
		doc.acreage = round(acres, 2)
	else:
		raise ToolError("source is Outline or Manual.")
	doc.save(ignore_permissions=True)
	return ToolResult(
		data={"field": field, "acreage": doc.acreage, "was": before, "source": doc.acreage_source,
		      "area_computed_acres": doc.get("area_computed_acres"), "reason": doc.get("acreage_override_reason")},
		summary=f"{field}: {before} → {doc.acreage} ac ({doc.acreage_source})", docstatus_delta="0 → 0 (updated)",
	)


def create_task_on_field(args: dict) -> ToolResult:
	"""A Farm Task on a block named the way people say it ('a spray check on Center Piece'). Same gates as create_farm_task."""
	from . import dispatch

	field = _one(as_str(args, "field", required=True))
	inner = {k: v for k, v in args.items() if k != "field"}
	inner["location_doctype"] = FIELD
	inner["location"] = field
	if not inner.get("company"):
		inner["company"] = frappe.db.get_value(FIELD, field, "owning_entity")
	result = dispatch.create_farm_task(inner)
	result.data = {**(result.data or {}), "field": field}
	return result


def _list(raw) -> list:
	if raw is None:
		return []
	if isinstance(raw, (list, tuple)):
		values = raw
	else:
		values = str(raw).replace(",", "\n").splitlines()
	return [" ".join(str(v).split()) for v in values if str(v).strip()]
