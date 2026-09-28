# SPDX-License-Identifier: MIT
"""Rebuild the five rodent bait templates on the form schema. v0.204.0.

docs/design/form_schema_and_labels.md §7. v0.203.0 seeded the templates with a
flat checklist; v0.204.0 ships them as forms the phone renders (EN/ES, the
product as a link that opens its label, quantities in the product's own unit,
occupied-only branches, a station group, the Farm Manager's approval, safety
attestations, and a Pest Control Application record).

ONLY A TEMPLATE THIS APP SEEDED AND NOBODY EDITED is rebuilt: its description
must still be v0.203.0's word for word, and no task may have been raised from
it. OML's own drafts (authored by another session, different words) are never
touched; `preview_farm_task_template` shows what the rebuilt shape looks like.
Idempotent — a rebuilt template no longer carries the v0.203.0 description.
"""

from __future__ import annotations

import json

import frappe

from erpnext_mcp import compat, rodent_bait

TEMPLATE = "Farm Task Template"


def execute() -> None:
	report = rebuild()
	print(f"erpnext_mcp: rodent templates rebuilt: {', '.join(report['rebuilt']) or 'none'}")


def rebuild() -> dict:
	report: dict = {"rebuilt": [], "left": []}
	if not compat.doctype_exists(TEMPLATE) or not compat.has_field(TEMPLATE, "form_schema"):
		return report
	for spec in rodent_bait.SEED_TASK_TEMPLATES_V204:
		name = spec["template_name"]
		if not frappe.db.exists(TEMPLATE, name):
			continue
		doc = frappe.get_doc(TEMPLATE, name)
		raised = frappe.db.count("Farm Task", {"template": name}) if compat.doctype_exists("Farm Task") else 0
		if str(doc.description or "") != rodent_bait.V203_DESCRIPTIONS.get(name) or raised:
			report["left"].append(name)
			continue
		for key in (
			"description",
			"task_type",
			"creates_record",
			"instructions",
			"title_es",
			"instructions_es",
			"skill_required",
		):
			doc.set(key, spec.get(key) or "")
		doc.evidence_required = json.dumps(spec["evidence_required"])
		doc.form_schema = json.dumps(spec["form_schema"])
		doc.applies_to_asset_types = "\n".join(spec["applies_to_asset_types"])
		doc.set("checklist", [])
		doc.save(ignore_permissions=True)
		report["rebuilt"].append(name)
	return report
