# SPDX-License-Identifier: MIT
"""Card print formats for Employee and Asset Register, and their defaults. v0.209.0.

docs/design/card_print_queue.md, Amendment 1 §A6. THE BUG: the Desk's own Print
on an Employee asked for `format=undefined` on A4 and produced a blank page,
because no usable print format was the default. THE FIX: a card print format on
each doctype — a Custom 85.6 × 54 mm page, zero margins — set as the doctype's
default where none is set, so Frappe's Print button and bulk Print produce the
card.

The format lays nothing out itself. It asks the Jinja global
`erpnext_mcp_card_svg` for the SAME drawing `card_art` sends the printer: the
front, then the back turned to landscape so both fit one page size.

The "Print ID Card" / "Print Asset Tag" buttons remain the intended path (they
record a job); this makes the generic button harmless. Records are created only
where absent, so an operator's edits survive a migrate.
"""

from __future__ import annotations

import frappe

from . import card_art, card_print, compat

PRINT_FORMAT = "Print Format"
JINJA_GLOBAL = "erpnext_mcp_card_svg"

#: (doctype, format name, job type)
FORMATS = (
	("Employee", "Employee ID Card (CR80)", "Employee ID"),
	("Asset Register", "Asset Tag Card (CR80)", "Asset Tag"),
)

TEMPLATE = """
<style>
  @page { size: 85.6mm 54mm; margin: 0; }
  html, body { margin: 0 !important; padding: 0 !important; background: #ffffff !important; }
  .print-format { margin: 0 !important; padding: 0 !important; width: 85.6mm !important;
                  min-height: 0 !important; box-shadow: none !important; border: 0 !important; }
  .print-toolbar, .navbar, .no-print, .letter-head, .print-heading, .footer, .page-footer { display: none !important; }
  .cr80 { width: 85.6mm; height: 54mm; overflow: hidden; line-height: 0;
          page-break-after: always; break-after: page; }
  .cr80:last-child { page-break-after: auto; break-after: auto; }
  .cr80-note { font: 9pt Helvetica, Arial, sans-serif; line-height: 1.3; padding: 6mm; color: #b3261e; }
</style>
{%- set card = erpnext_mcp_card_svg(doc.doctype, doc.name) -%}
{%- if card.error %}
<div class="cr80"><div class="cr80-note">{{ card.error }}</div></div>
{%- else %}
<div class="cr80">{{ card.front | safe }}</div>
<div class="cr80">{{ card.back | safe }}</div>
{%- endif %}
"""


def erpnext_mcp_card_svg(doctype: str, name: str) -> dict:
	"""{front, back, error} — the card for one Employee or Asset Register row. Never raises.

	READS ONLY: a first badge is not issued by printing a format; the note says
	to use the Print ID Card button, which is the path that records the card."""
	job_type = next((jt for dt, _n, jt in FORMATS if dt == doctype), "")
	out = {"front": "", "back": "", "error": ""}
	try:
		if not job_type:
			out["error"] = f"no card layout for {doctype}."
			return out
		_name, _title, company, row = (
			card_print._employee if job_type == "Employee ID" else card_print._asset
		)(name)
		sides, _warnings = card_print.card_sides(job_type, row, company, allow_issue=False)
		out["front"] = card_art.to_svg(sides[0], css_width_mm=85.6)
		out["back"] = card_art.to_svg(sides[1], rotation=90, css_width_mm=85.6)
	except Exception as exc:
		out["error"] = str(exc) or type(exc).__name__
	return out


def _page_size_fields() -> dict:
	meta = compat.field_meta(PRINT_FORMAT, "page_size")
	options = [line.strip() for line in str(getattr(meta, "options", "") or "").splitlines()]
	if "Custom" in options:
		return {"page_size": "Custom", "page_width": card_art.CARD_W, "page_height": card_art.CARD_H}
	return {}


def format_fields(doctype: str, name: str) -> dict:
	fields = {
		"doctype": PRINT_FORMAT,
		"name": name,
		"doc_type": doctype,
		"module": "ERPNext MCP",
		"standard": "No",
		"custom_format": 1,
		"print_format_type": "Jinja",
		"print_format_builder": 0,
		"disabled": 0,
		"margin_top": 0,
		"margin_bottom": 0,
		"margin_left": 0,
		"margin_right": 0,
		"html": TEMPLATE,
	}
	fields.update(_page_size_fields())
	return fields


def seed_card_print_formats() -> list:
	"""Create both formats and make each its doctype's default where none is set. Never raises."""
	reports = []
	for doctype, name, _job_type in FORMATS:
		report = {"doctype": doctype, "format": name, "created": False, "default_set": False, "reason": ""}
		reports.append(report)
		try:
			if not frappe.db.exists("DocType", PRINT_FORMAT) or not frappe.db.exists("DocType", doctype):
				report["reason"] = f"this site has no {doctype} doctype"
				continue
			if not frappe.db.exists(PRINT_FORMAT, name):
				doc = frappe.get_doc(format_fields(doctype, name))
				doc.flags.ignore_permissions = True
				doc.insert(ignore_permissions=True)
				report["created"] = True
			report["default_set"] = _set_default(doctype, name)
		except Exception as exc:  # pragma: no cover - a site mid-migrate
			report["reason"] = f"{type(exc).__name__}: {exc}"
	return reports


def _set_default(doctype: str, name: str) -> bool:
	"""A Property Setter for `default_print_format`, only where the doctype has no default."""
	current = ""
	try:
		current = str(frappe.db.get_value("DocType", doctype, "default_print_format") or "")
	except Exception:
		current = ""
	existing = frappe.db.get_value(
		"Property Setter",
		{"doc_type": doctype, "property": "default_print_format"},
		["name", "value"],
		as_dict=True,
	)
	if existing and str(existing.get("value") or "").strip():
		return False
	if current.strip() and not existing:
		return False
	doc = frappe.get_doc(
		{
			"doctype": "Property Setter",
			"doctype_or_field": "DocType",
			"doc_type": doctype,
			"property": "default_print_format",
			"property_type": "Data",
			"value": name,
			"module": "ERPNext MCP",
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	try:
		frappe.clear_cache(doctype=doctype)
	except Exception:
		pass
	return True


def remove_card_print_formats() -> list:
	"""Before uninstall: the Property Setters still pointing at these formats, then the formats."""
	removed = []
	for doctype, name, _job_type in FORMATS:
		try:
			setter = frappe.db.get_value(
				"Property Setter",
				{"doc_type": doctype, "property": "default_print_format", "value": name},
				"name",
			)
			if setter:
				frappe.delete_doc("Property Setter", setter, ignore_permissions=True, force=True)
			if frappe.db.exists(PRINT_FORMAT, name):
				frappe.delete_doc(PRINT_FORMAT, name, ignore_permissions=True, force=True)
				removed.append(name)
		except Exception:  # pragma: no cover
			continue
	return removed
