# SPDX-License-Identifier: MIT
"""Move the "Rodent Bait Program" settings into data. v0.205.0.

docs/design/programs_and_field_kinds.md A1–A7. v0.203.0 kept the program's
open decisions in ERPNext MCP Settings; configure-not-code moves each onto the
record it belongs to, and the rodent program behaves exactly as before:

  pest_applicator_certification  → `required_certification` on every Farm Task
  (+ pest_require_applicator,       Template whose skill was the applicator skill,
     pest_applicator_skill)         and on their open tasks
  pest_people_work_here_types    → `people_present` on the Farm Asset Types of
                                   those names (created disabled where missing)
  pest_alert_role                → already on each rule's notify_roles; dropped
  pest_crew_skill                → already on the templates; dropped
  Company pest_season_*          → Company season_start / season_end
  a System-authored `rodent_bait_check_overdue` naming the builtin scanner
                                 → declarative, with `grouped_cadence`

The settings values are read from `tabSingles` FIRST — the fields are gone from
the DocType once this runs. Idempotent: every step fills a blank or sets a flag.
"""

from __future__ import annotations

import json

import frappe

from erpnext_mcp import compat, rodent_bait

SETTINGS = "ERPNext MCP Settings"
OLD_DEFAULTS = {
	"pest_applicator_skill": "applicator",
	"pest_applicator_certification": "Applicator License",
	"pest_require_applicator": "1",
	"pest_people_work_here_types": "Barn\nShop\nKitchen\nBath House\nToilet-Shower\nStorage\nCold Storage",
}
TERMINAL = ("Completed", "Rejected", "Cancelled", "Merged")


def execute() -> None:
	report = move(old_values())
	print(f"erpnext_mcp: rodent settings moved to data — {json.dumps(report, default=str)[:400]}")


def old_values() -> dict:
	"""The stored v0.203.0 values, from tabSingles, defaults where never set."""
	values = dict(OLD_DEFAULTS)
	try:
		rows = frappe.db.sql(
			"select field, value from `tabSingles` where doctype=%s and field like %s",
			(SETTINGS, "pest\\_%"),
			as_dict=True,
		)
		for row in rows or []:
			if row.get("value") not in (None, ""):
				values[str(row["field"])] = str(row["value"])
	except Exception:
		pass
	return values


def move(old: dict) -> dict:
	report: dict = {"templates": [], "tasks": 0, "asset_types": [], "companies": [], "rules": []}
	_certifications(old, report)
	_asset_types(old, report)
	_seasons(report)
	_rules(report)
	return report


def _certifications(old: dict, report: dict) -> None:
	if str(old.get("pest_require_applicator") or "0").strip() in ("", "0", "false"):
		return
	skill = str(old.get("pest_applicator_skill") or "").strip()
	cert = str(old.get("pest_applicator_certification") or "").strip()
	template = "Farm Task Template"
	if not (skill and cert) or not compat.has_field(template, "required_certification"):
		return
	names = []
	for row in frappe.db.get_all(
		template, filters={"skill_required": skill}, fields=["name", "required_certification"]
	):
		names.append(row["name"])
		if not str(row.get("required_certification") or "").strip():
			frappe.db.set_value(template, row["name"], "required_certification", cert, update_modified=False)
			report["templates"].append(row["name"])
	if names and compat.has_field("Farm Task", "required_certification"):
		for row in frappe.db.get_all(
			"Farm Task",
			filters={"template": ("in", names)},
			fields=["name", "state", "required_certification", "skill_required"],
			limit=100000,
		):
			if row.get("state") in TERMINAL or str(row.get("required_certification") or "").strip():
				continue
			if str(row.get("skill_required") or "") != skill:
				continue
			frappe.db.set_value(
				"Farm Task", row["name"], "required_certification", cert, update_modified=False
			)
			report["tasks"] += 1


def _asset_types(old: dict, report: dict) -> None:
	doctype = "Farm Asset Type"
	if not compat.has_field(doctype, "people_present"):
		return
	raw = str(old.get("pest_people_work_here_types") or "")
	for name in [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]:
		if frappe.db.exists(doctype, name):
			if not compat.checked(frappe.db.get_value(doctype, name, "people_present")):
				frappe.db.set_value(doctype, name, "people_present", 1, update_modified=False)
				report["asset_types"].append(name)
			continue
		doc = frappe.new_doc(doctype)
		doc.type_name = name
		doc.icon = name[:1].upper()
		doc.display_order = 900
		doc.description = "A Housing Unit type people work in (flag only)."
		doc.enabled = 0
		doc.people_present = 1
		doc.insert(ignore_permissions=True)
		report["asset_types"].append(f"{name} (created, disabled)")


def _seasons(report: dict) -> None:
	if not compat.doctype_exists("Company") or not compat.has_field("Company", "season_start"):
		return
	old = compat.existing_fields("Company", ("name", "pest_season_start", "pest_season_end"))
	if "pest_season_start" in old:
		for row in frappe.db.get_all("Company", fields=[*old, "season_start", "season_end"]):
			for new, was in (("season_start", "pest_season_start"), ("season_end", "pest_season_end")):
				if row.get(was) and not row.get(new):
					frappe.db.set_value("Company", row["name"], new, row[was], update_modified=False)
					report["companies"].append(f"{row['name']}.{new}")
	for fieldname in ("pest_season_start", "pest_season_end"):
		existing = frappe.db.get_value("Custom Field", {"dt": "Company", "fieldname": fieldname}, "name")
		if existing:
			frappe.delete_doc("Custom Field", existing, ignore_permissions=True, force=True)
	try:
		frappe.clear_cache(doctype="Company")
	except Exception:
		pass


def _rules(report: dict) -> None:
	doctype = "Compliance Rule"
	if not compat.doctype_exists(doctype):
		return
	fields = compat.existing_fields(
		doctype,
		("name", "rule_id", "builtin_scanner", "authored_by", "extra_parameters_json", "message_template"),
	)
	for row in frappe.db.get_all(doctype, filters={"rule_id": "rodent_bait_check_overdue"}, fields=fields):
		if str(row.get("builtin_scanner") or "") != "rodent_bait_check_overdue":
			continue
		if str(row.get("authored_by") or "") != "System":
			continue
		try:
			extra = json.loads(row.get("extra_parameters_json") or "{}")
		except ValueError:
			extra = {}
		extra["grouped_cadence"] = rodent_bait.cadence_params(extra)
		frappe.db.set_value(
			doctype,
			row["name"],
			{
				"builtin_scanner": "",
				"extra_parameters_json": json.dumps(extra),
				"message_template": row.get("message_template") or rodent_bait.CHECK_MESSAGE,
			},
			update_modified=False,
		)
		report["rules"].append(row["name"])
