# SPDX-License-Identifier: MIT
"""A program is a bundle of data: templates, rules, units, contexts, asset-type flags. v0.205.0.

docs/design/programs_and_field_kinds.md A6. Configure, not code: the rodent
program tuned at OML is a document another farm imports, not a release. A
bundle is JSON; `export` builds one from THIS site's records, `import_bundle`
creates what is missing (templates and rules DISABLED and unapproved) and
leaves everything that exists alone.
"""

from __future__ import annotations

import json
import os

import frappe

from . import compat
from .errors import ToolError

PROGRAM_DIR = os.path.join(os.path.dirname(__file__), "programs")
TASK_TEMPLATE = "Farm Task Template"
INSPECTION_TEMPLATE = "Inspection Template"
RULE = "Compliance Rule"
UOM = "UOM"
CONTEXT = "Agricultural UOM Context"
ASSET_TYPE = "Farm Asset Type"
ITEM_GROUP = "Item Group"
PARTS = (
	"task_templates",
	"inspection_templates",
	"compliance_rules",
	"uoms",
	"uom_contexts",
	"asset_types",
	"item_groups",
)

_META = {
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"doctype",
	"version",
	"superseded_by",
	"human_approved_by",
	"human_approved_on",
	"approver_employee",
	"approver_signature",
}


# ── the shipped rodent program, from the same Python the seeders use ────────
def rodent_bundle() -> dict:
	from . import ag_uom, asset_types, rodent_bait

	bait = next(spec for spec in ag_uom.SEED_CONTEXTS if spec["context_name"] == "Bait")
	units = [
		spec
		for spec in ag_uom.SEED_UOMS
		if spec["uom_name"] in ("Block", "Place Pac", "Pouch", "Bait Station")
	]
	aliases = {
		"Place Pac": ["pac", "pacs", "place pacs", "pack", "packs"],
		"Bait Station": ["station", "stations"],
	}
	return {
		"program": "rodent_bait",
		"title": {
			"en": "Rodent bait at housing and buildings",
			"es": "Cebo para roedores en viviendas y edificios",
		},
		"version": 1,
		"requires_app": "0.205.0",
		"task_templates": [dict(spec) for spec in rodent_bait.SEED_TASK_TEMPLATES_V204],
		"inspection_templates": [],
		# The Check rule's producer, always: the seeder only links it once the
		# template exists, and an import drops a link to one that does not.
		"compliance_rules": [
			{
				**spec,
				**(
					{"producer_task_template": rodent_bait.CHECK}
					if spec["rule_id"] == "rodent_bait_check_overdue"
					else {}
				),
			}
			for spec in rodent_bait.rule_seed_specs()
		],
		"uoms": [
			{
				"uom_name": spec["uom_name"],
				"must_be_whole_number": spec["must_be_whole_number"],
				"aliases": aliases.get(spec["uom_name"], []),
			}
			for spec in units
		],
		"uom_contexts": [json.loads(json.dumps(bait, default=list))],
		"asset_types": [
			{
				"type_name": name,
				"people_present": 1,
				"enabled": 0 if name in asset_types.FLAG_ONLY_TYPES else 1,
			}
			for name in (*asset_types.PEOPLE_PRESENT, *asset_types.FLAG_ONLY_TYPES)
		]
		+ [{"type_name": name, "people_present": 0, "enabled": 1} for name in ("Cabin", "House")],
		"item_groups": [rodent_bait.PEST_CONTROL_GROUP],
	}


SHIPPED = {"rodent_bait": rodent_bundle}


def shipped(name: str) -> dict:
	"""A shipped bundle: the JSON file when present, else built from the definitions."""
	path = os.path.join(PROGRAM_DIR, f"{name}.json")
	if os.path.exists(path):
		with open(path) as handle:
			return json.load(handle)
	if name in SHIPPED:
		return SHIPPED[name]()
	raise ToolError(f"no shipped program called {name!r}. Shipped: {', '.join(sorted(SHIPPED))}.")


# ── what is installed ───────────────────────────────────────────────────────
def _exists(doctype: str, name: str) -> bool:
	return compat.doctype_exists(doctype) and bool(frappe.db.exists(doctype, name))


def _rule_exists(rule_id: str) -> bool:
	return compat.doctype_exists(RULE) and bool(frappe.db.exists(RULE, {"rule_id": rule_id}))


def _inspection_exists(name: str) -> bool:
	return compat.doctype_exists(INSPECTION_TEMPLATE) and bool(
		frappe.db.exists(INSPECTION_TEMPLATE, {"template_name": name})
	)


def status(bundle: dict) -> dict:
	"""Which parts of a bundle this site has."""
	have = {
		"task_templates": [
			s["template_name"]
			for s in bundle.get("task_templates") or []
			if _exists(TASK_TEMPLATE, s["template_name"])
		],
		"inspection_templates": [
			s["template_name"]
			for s in bundle.get("inspection_templates") or []
			if _inspection_exists(s["template_name"])
		],
		"compliance_rules": [
			s["rule_id"] for s in bundle.get("compliance_rules") or [] if _rule_exists(s["rule_id"])
		],
		"uoms": [s["uom_name"] for s in bundle.get("uoms") or [] if _exists(UOM, s["uom_name"])],
		"uom_contexts": [
			s["context_name"] for s in bundle.get("uom_contexts") or [] if _exists(CONTEXT, s["context_name"])
		],
		"asset_types": [
			s["type_name"] for s in bundle.get("asset_types") or [] if _exists(ASSET_TYPE, s["type_name"])
		],
		"item_groups": [g for g in bundle.get("item_groups") or [] if _exists(ITEM_GROUP, g)],
	}
	total = sum(len(bundle.get(part) or []) for part in PARTS)
	present = sum(len(values) for values in have.values())
	return {
		"program": bundle.get("program"),
		"title": bundle.get("title"),
		"version": bundle.get("version"),
		"installed": "complete" if present == total and total else "partial" if present else "not installed",
		"present": have,
		"parts": {part: len(bundle.get(part) or []) for part in PARTS},
	}


# ── export: this site's records as a bundle ─────────────────────────────────
def export(program: str, title: dict | None, parts: dict) -> dict:
	from . import compliance_rules, sessions, task_templates

	bundle: dict = {
		"program": program,
		"title": title or {"en": program},
		"version": 1,
		"requires_app": "0.205.0",
	}
	templates = []
	for name in parts.get("task_templates") or []:
		row = task_templates.template_row(name)
		if not row:
			raise ToolError(f"no Farm Task Template called {name!r}.")
		described = task_templates.describe(name, with_checklist=True)
		templates.append(
			{
				"template_name": described["template_name"],
				"task_type": described["task_type"],
				"description": described["description"],
				"skill_required": described["skill_required"],
				"required_certification": described.get("required_certification") or "",
				"estimated_duration_minutes": described["estimated_duration_minutes"],
				"dispatch_mode": described["dispatch_mode"],
				"default_urgency": described["default_urgency"],
				"evidence_required": described["evidence_required"],
				"creates_record": described["creates_record"] or "",
				"creates_record_data": described["creates_record_data"],
				"instructions": described["instructions"],
				"instructions_es": described.get("instructions_es") or "",
				"title_es": described.get("title_es") or "",
				"form_schema": described.get("form_schema") or [],
				"applies_to_asset_types": described.get("applies_to_asset_types") or [],
				"regimes": described["compliance_regimes"],
				"checklist": [
					{key: item.get(key) for key in ("item_name", "required", "evidence_type")}
					for item in described.get("checklist") or []
				],
				"enabled": 0,
			}
		)
	bundle["task_templates"] = templates
	inspections = []
	for name in parts.get("inspection_templates") or []:
		docname = sessions.resolve_template(name)
		if not docname:
			raise ToolError(f"no Inspection Template called {name!r}.")
		fields = compat.existing_fields(
			INSPECTION_TEMPLATE,
			(
				"template_name",
				"description",
				"applies_to_asset_type",
				"skill_required",
				"required_certification",
				"estimated_duration_minutes",
				"regulation_citations",
			),
		)
		row = dict(frappe.db.get_value(INSPECTION_TEMPLATE, docname, fields, as_dict=True) or {})
		inspections.append(
			{
				"template_name": row.get("template_name"),
				"description": row.get("description") or "",
				"applies_to_asset_type": row.get("applies_to_asset_type") or "General",
				"skill_required": row.get("skill_required") or "",
				"required_certification": row.get("required_certification") or "",
				"estimated_duration_minutes": int(row.get("estimated_duration_minutes") or 0),
				"regulation_citations": row.get("regulation_citations") or "",
				"sections": [sessions.describe_section(section) for section in sessions.sections_of(docname)],
			}
		)
	bundle["inspection_templates"] = inspections
	rules = []
	for rule_id in parts.get("compliance_rules") or []:
		name = compliance_rules.resolve(rule_id)
		if not name:
			raise ToolError(f"no Compliance Rule called {rule_id!r}.")
		row = compliance_rules.rule_row(name)
		rules.append({key: value for key, value in row.items() if key not in _META} | {"enabled": 0})
	bundle["compliance_rules"] = rules
	uoms = []
	for name in parts.get("uoms") or []:
		if not _exists(UOM, name):
			raise ToolError(f"no UOM called {name!r}.")
		fields = compat.existing_fields(UOM, ("name", "must_be_whole_number", "uom_aliases"))
		row = frappe.db.get_value(UOM, name, fields, as_dict=True) or {}
		from .uom_resolve import parse_aliases

		uoms.append(
			{
				"uom_name": name,
				"must_be_whole_number": int(row.get("must_be_whole_number") or 0),
				"aliases": parse_aliases(row.get("uom_aliases")),
			}
		)
	bundle["uoms"] = uoms
	contexts = []
	for name in parts.get("uom_contexts") or []:
		if not _exists(CONTEXT, name):
			raise ToolError(f"no unit context called {name!r}.")
		doc = frappe.get_doc(CONTEXT, name)
		contexts.append(
			{
				"context_name": name,
				"applies_to": doc.get("applies_to"),
				"description": doc.get("description") or "",
				"uoms": [
					{
						"uom": row.get("uom"),
						"is_default": int(row.get("is_default") or 0),
						"notes": row.get("notes") or "",
					}
					for row in doc.get("uoms") or []
				],
			}
		)
	bundle["uom_contexts"] = contexts
	types = []
	for name in parts.get("asset_types") or []:
		if not _exists(ASSET_TYPE, name):
			raise ToolError(f"no asset type called {name!r}.")
		fields = compat.existing_fields(
			ASSET_TYPE, ("name", "enabled", "people_present", "icon", "description")
		)
		row = frappe.db.get_value(ASSET_TYPE, name, fields, as_dict=True) or {}
		types.append(
			{
				"type_name": name,
				"enabled": int(row.get("enabled") or 0),
				"people_present": int(row.get("people_present") or 0),
				"icon": row.get("icon") or "",
				"description": row.get("description") or "",
			}
		)
	bundle["asset_types"] = types
	bundle["item_groups"] = list(parts.get("item_groups") or [])
	return bundle


# ── import: create what is missing ──────────────────────────────────────────
def import_bundle(bundle: dict, dry_run: bool = True) -> dict:
	"""Create-only. Templates and rules arrive disabled and unapproved."""
	from . import compliance_rules, sessions, task_templates

	if not isinstance(bundle, dict) or not bundle.get("program"):
		raise ToolError("bundle must be an object with a `program` name.")
	report = {
		"program": bundle["program"],
		"dry_run": bool(dry_run),
		"created": [],
		"present": [],
		"refused": [],
	}

	def step(label: str, exists: bool, create) -> None:
		if exists:
			report["present"].append(label)
			return
		if dry_run:
			report["created"].append(f"{label} (would create)")
			return
		try:
			create()
			report["created"].append(label)
		except Exception as exc:
			report["refused"].append({"part": label, "reason": f"{type(exc).__name__}: {exc}"})

	for group in bundle.get("item_groups") or []:

		def make_group(group=group):
			parent = "All Item Groups" if frappe.db.exists(ITEM_GROUP, "All Item Groups") else None
			if not parent:
				raise ToolError("no 'All Item Groups' to put it under")
			doc = frappe.new_doc(ITEM_GROUP)
			doc.item_group_name = group
			doc.parent_item_group = parent
			doc.is_group = 0
			doc.insert(ignore_permissions=True)

		step(f"Item Group {group}", _exists(ITEM_GROUP, group), make_group)

	for spec in bundle.get("uoms") or []:

		def make_uom(spec=spec):
			doc = frappe.new_doc(UOM)
			doc.uom_name = spec["uom_name"]
			doc.must_be_whole_number = int(spec.get("must_be_whole_number") or 0)
			if spec.get("aliases") and compat.has_field(UOM, "uom_aliases"):
				doc.uom_aliases = "\n".join(spec["aliases"])
			doc.insert(ignore_permissions=True)

		step(f"UOM {spec['uom_name']}", _exists(UOM, spec["uom_name"]), make_uom)

	for spec in bundle.get("asset_types") or []:

		def make_type(spec=spec):
			doc = frappe.new_doc(ASSET_TYPE)
			doc.type_name = spec["type_name"]
			doc.icon = (spec.get("icon") or spec["type_name"][:1]).upper()[:1]
			doc.display_order = 900
			doc.description = spec.get("description") or ""
			doc.enabled = int(spec.get("enabled", 1))
			if compat.has_field(ASSET_TYPE, "people_present"):
				doc.people_present = int(spec.get("people_present") or 0)
			doc.insert(ignore_permissions=True)

		step(f"Asset type {spec['type_name']}", _exists(ASSET_TYPE, spec["type_name"]), make_type)

	for spec in bundle.get("uom_contexts") or []:

		def make_context(spec=spec):
			doc = frappe.new_doc(CONTEXT)
			doc.context_name = spec["context_name"]
			doc.applies_to = spec.get("applies_to")
			doc.description = spec.get("description") or ""
			doc.is_active = 1
			for row in spec.get("uoms") or []:
				if frappe.db.exists(UOM, row["uom"]):
					doc.append("uoms", dict(row))
			doc.insert(ignore_permissions=True)

		step(f"Unit context {spec['context_name']}", _exists(CONTEXT, spec["context_name"]), make_context)

	for spec in bundle.get("task_templates") or []:
		step(
			f"Farm Task Template {spec['template_name']}",
			_exists(TASK_TEMPLATE, spec["template_name"]),
			lambda spec=spec: task_templates.build_template({**spec, "enabled": 0}).insert(
				ignore_permissions=True
			),
		)

	for spec in bundle.get("inspection_templates") or []:
		step(
			f"Inspection Template {spec['template_name']}",
			_inspection_exists(spec["template_name"]),
			lambda spec=spec: sessions.build_template({**spec, "authored_by": "Operator"}).insert(
				ignore_permissions=True
			),
		)

	for spec in bundle.get("compliance_rules") or []:
		clean = {key: value for key, value in spec.items() if key not in _META}
		clean["enabled"] = 0
		if clean.get("producer_task_template") and not _exists(
			TASK_TEMPLATE, clean["producer_task_template"]
		):
			clean.pop("producer_task_template")
		step(
			f"Compliance Rule {spec['rule_id']}",
			_rule_exists(spec["rule_id"]),
			lambda clean=clean: compliance_rules.build_rule(clean).insert(ignore_permissions=True),
		)
	return report
