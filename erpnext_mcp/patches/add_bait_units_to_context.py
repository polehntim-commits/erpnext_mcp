# SPDX-License-Identifier: MIT
"""Offer Place Pac, Pouch and Bait Station in an existing Bait context.

v0.202.0. PROWLER® Place Pacs are "1 place pac per bait placement", and the
Bait context the v0.197.0 seeder laid down offers Block alone — so the phone's
"Counted in" picker, which lists the context's units, had nothing to offer but
Block. `agronomy_seed` only ever CREATES a context; one that exists keeps its
list, which is right for an operator's edit and wrong for a unit this app now
ships. So this adds the three bait units (`ag_uom.BAIT_UNITS`) that are MISSING
from the Bait context and nothing else: the default is untouched, a unit
somebody already added (Tim added Place Pac on OML by hand) is not doubled, and
a unit somebody removed after this ran is not put back — the patch runs once.

It also installs the UOM `uom_aliases` column (`tools/uoms.ensure_uom_alias_field`)
so "pacs" can be taught to the site without a deploy.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import ag_uom, agronomy_seed, compat

UOM_CONTEXT = "Agricultural UOM Context"
BAIT = "Bait"


def execute() -> None:
	report = add_bait_units_to_context()
	print(
		f"erpnext_mcp: Bait context — added {', '.join(report['added']) or 'nothing'}"
		+ (f" ({report['skipped']})" if report["skipped"] else "")
	)


def add_bait_units_to_context() -> dict:
	report: dict = {"added": [], "skipped": "", "alias_field": False}
	try:
		from erpnext_mcp.tools import uoms

		report["alias_field"] = uoms.ensure_uom_alias_field()
	except Exception:  # pragma: no cover - the field is also created on first use
		pass
	if not compat.doctype_exists("UOM") or not compat.doctype_exists(UOM_CONTEXT):
		report["skipped"] = "no UOM or unit-context doctype"
		return report
	# The units first — after_migrate's seeder runs after patches, and a context
	# row cannot link to a unit that does not exist yet.
	agronomy_seed._seed_uoms({"created": [], "skipped": [], "failed": []})
	if not frappe.db.exists(UOM_CONTEXT, BAIT):
		report["skipped"] = "no Bait context yet — the seeder creates it with these units"
		return report
	doc = frappe.get_doc(UOM_CONTEXT, BAIT)
	listed = {row.get("uom") for row in doc.get("uoms") or []}
	for spec in ag_uom.BAIT_UNITS:
		if spec["uom"] in listed or not frappe.db.exists("UOM", spec["uom"]):
			continue
		doc.append("uoms", dict(spec))
		report["added"].append(spec["uom"])
	if report["added"]:
		doc.save(ignore_permissions=True)
	return report
