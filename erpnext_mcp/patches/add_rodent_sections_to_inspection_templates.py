# SPDX-License-Identifier: MIT
"""Add the two rodent bait sections to the seeded housing inspection templates.

v0.203.0 (docs/design/rodent_bait_program.md §4.4). Rodent bait is part of camp
maintenance: the pre-occupancy walk gets "Rodent bait cleared" (the server
passes it only when every interior bait round ended in a completed Removal and
Clearance) and the routine walk gets "Rodent activity" (yes raises a placement
task). The seeder only ever CREATES a template, so a site seeded before this
release needs the sections added to the live version it already has.

Only the LIVE version of a template THIS APP authored (`authored_by = System`)
is touched, and only when it does not already carry the section. A section
added changes nothing already recorded, which is why this edits in place
rather than versioning. An operator's own template is left alone. Idempotent.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import compat, proposals, sessions

TEMPLATE = "Inspection Template"


def execute() -> None:
	report = add_rodent_sections()
	print(f"erpnext_mcp: rodent bait sections added to {', '.join(report['updated']) or 'no template'}")


def add_rodent_sections() -> dict:
	report: dict = {"updated": [], "skipped": []}
	if not compat.doctype_exists(TEMPLATE):
		return report
	for template_name, section in sessions.RODENT_SECTIONS.items():
		rows = frappe.db.get_all(
			TEMPLATE,
			filters={"template_name": template_name, "active": 1, "superseded_by": ("in", ("", None))},
			fields=["name", "authored_by"],
			limit=5,
		)
		for row in rows or []:
			if str(row.get("authored_by") or "") != proposals.AUTHOR_SYSTEM:
				report["skipped"].append(f"{row['name']} (not authored by this app)")
				continue
			doc = frappe.get_doc(TEMPLATE, row["name"])
			if any(
				str(existing.get("section_name") or "") == section["section_name"]
				for existing in doc.get("sections") or []
			):
				continue
			order = max(
				[int(existing.get("order_index") or 0) for existing in doc.get("sections") or []] or [0]
			)
			doc.append("sections", sessions._section_row({**section, "order_index": order + 1}, order))
			doc.save(ignore_permissions=True)
			report["updated"].append(f"{row['name']} ({template_name})")
	return report
