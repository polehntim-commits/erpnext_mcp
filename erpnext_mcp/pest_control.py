# SPDX-License-Identifier: MIT
"""The Pest Control Application record, written by the task that did the work. v0.204.0.

docs/design/form_schema_and_labels.md §5. The non-crop sibling of Spray
Application: who applied which registered product, where, how much, under
which licence, and whether its label was on hand.
"""

from __future__ import annotations

import frappe

from . import compat, form_schema

DOCTYPE = "Pest Control Application"


def _quantity(fields: list, answers: dict) -> tuple:
	"""`(quantity, uom, stations)` from the answers: a `quantity` measurement, else a `stations` group."""
	value = answers.get("quantity")
	if isinstance(value, dict) and value.get("value") not in (None, ""):
		# v0.230.1. "2 blocks" in the number box raised here, inside task
		# completion, and the whole completion came back a 500. The record is
		# still written; the quantity is left for a person, and the notes say so.
		try:
			return float(value["value"]), str(value.get("uom") or ""), None
		except (TypeError, ValueError):
			return None, str(value.get("uom") or ""), None
	for field in fields:
		if field.get("type") != "group":
			continue
		rows = answers.get(field["key"])
		if not isinstance(rows, list):
			continue
		total, unit = 0.0, ""
		for row in rows:
			count = (row or {}).get("count")
			if isinstance(count, dict):
				unit = unit or str(count.get("uom") or "")
				count = count.get("value")
			try:
				total += float(count or 0)
			except (TypeError, ValueError):
				continue
		return (total or None), unit, len(rows)
	return None, "", None


def build_application(task: dict, assignment_doc, answers: dict | None) -> str:
	"""Write the record for a completed task. Returns its name."""
	from . import rodent_bait, task_forms

	fields, _legacy = task_forms.fields_of(task)
	answers = answers or {}
	quantity, uom, stations = _quantity(fields, answers)
	product = task.get("bait_product") or next(iter(form_schema.link_values(fields, answers, "Item")), "")
	if not product:
		product = rodent_bait.product_of(task)
	worker = str(getattr(assignment_doc, "assigned_to", "") or "")
	doc = frappe.new_doc(DOCTYPE)
	doc.company = task.get("company") or None
	doc.source_task = task.get("name")
	doc.applied_at = getattr(assignment_doc, "completed_at", None) or frappe.utils.now()
	doc.location_doctype = task.get("location_doctype") or None
	doc.location = task.get("location") or None
	doc.placement = task.get("bait_placement") or rodent_bait.placement_side(task.get("template")) or None
	doc.occupancy = task.get("occupancy_at_creation") or None
	doc.product = product or None
	doc.quantity = quantity
	unit_note = ""
	if uom:
		# v0.230.1. A unit the worker NAMED is resolved, never replaced: "blocks"
		# used to fall through to the Item's stock unit and the record said
		# "2 Pound" for two blocks of bait.
		from . import uom_resolve

		resolved = uom_resolve.resolve_unit(uom).get("uom")
		doc.uom = resolved or None
		if not resolved:
			unit_note = f"Unit stated as {uom!r}, which is not on this site's unit list — set it by hand."
	elif product:
		doc.uom = frappe.db.get_value("Item", product, "stock_uom") or None
	raw_quantity = (answers.get("quantity") or {}).get("value") if isinstance(answers.get("quantity"), dict) else None
	if quantity is None and raw_quantity not in (None, ""):
		unit_note = (unit_note + " " if unit_note else "") + (
			f"Quantity stated as {raw_quantity!r}, which is not a number — set it by hand."
		)
	doc.stations = stations or 0
	doc.applicator = worker
	doc.applicator_name = str(getattr(assignment_doc, "assigned_to_name", "") or worker)
	from . import qualifications

	requirement = str(task.get("required_certification") or "") or rodent_bait.APPLICATOR_CERTIFICATION
	cert = qualifications.certificate_of(worker, requirement) if worker else ""
	if cert:
		doc.applicator_certification = cert
	available, _snapshot = task_forms.label_state({**task, "bait_product": product})
	doc.label_available = 1 if available else 0
	views = task_forms._json(task.get("label_views"), [])
	doc.label_viewed = 1 if any(row.get("item_code") == product for row in views) else 0
	doc.notes = "\n".join(
		part for part in (str(getattr(assignment_doc, "findings_text", "") or ""), unit_note) if part
	)[:1000]
	doc.insert(ignore_permissions=True)
	return doc.name


def list_applications(company: str = "", location: str = "", limit: int = 100) -> list:
	if not compat.doctype_exists(DOCTYPE):
		return []
	filters = {}
	if company:
		filters["company"] = company
	if location:
		filters["location"] = location
	return [
		dict(row)
		for row in frappe.db.get_all(
			DOCTYPE,
			filters=filters,
			fields=compat.existing_fields(
				DOCTYPE,
				(
					"name",
					"applied_at",
					"company",
					"location_doctype",
					"location",
					"placement",
					"occupancy",
					"product",
					"epa_registration_number",
					"quantity",
					"uom",
					"stations",
					"applicator",
					"applicator_name",
					"applicator_certification",
					"label_available",
					"label_viewed",
					"source_task",
				),
			),
			order_by="applied_at desc",
			limit=limit,
		)
		or []
	]
