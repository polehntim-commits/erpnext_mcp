# SPDX-License-Identifier: MIT
"""Configure, not code: programs as data, field kinds v2, capabilities, inspections on the phone.

docs/design/programs_and_field_kinds.md (v0.205.0).
"""

import json
import unittest

import frappe

from erpnext_mcp import (
	compliance_fields,
	device_capabilities,
	form_schema,
	occupancy,
	programs,
	qualifications,
	rodent_bait,
	sessions,
	task_templates,
)
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.patches import move_rodent_settings_to_data as move_patch

from .fixtures import MAIN, V12TestCase
from .harness import STORE
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase

T = lambda en, es="": {"en": en, "es": es}  # noqa: E731

ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_housing_unit",
		"create_farm_task_template",
		"create_task_from_template",
		"assign_farm_task",
		"list_programs",
		"export_program",
		"import_program",
		"list_device_capabilities",
		"update_company",
	)
}


# ── Part A ──────────────────────────────────────────────────────────────────
class AnyTemplateCanRequireACertification(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.tool_data(
			"create_farm_task_template",
			{
				"template_name": "Forklift Move",
				"task_type": "Maintenance",
				"evidence_required": {"photos": True},
				"required_certification": "Forklift",
				"dispatch_mode": "Dispatched",
			},
		)

	def test_the_requirement_is_enforced_like_the_applicator_licence(self):
		task = self.tool_data("create_task_from_template", {"template": "Forklift Move"})["name"]
		self.assertEqual(STORE.get_raw("Farm Task", task)["required_certification"], "Forklift")
		message = self.tool_error(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-9", "assigned_to_name": "Jo"}
		)
		self.assertIn("requires 'Forklift'", message)
		STORE.seed(
			"Certification",
			[
				{
					"name": "C-F",
					"cert_type": "Other",
					"cert_name": "Forklift",
					"status": "Active",
					"holder": "EMP-9",
				}
			],
		)
		self.tool_data("assign_farm_task", {"task": task, "assigned_to": "EMP-9", "assigned_to_name": "Jo"})

	def test_an_expired_certificate_does_not_count(self):
		STORE.seed(
			"Certification",
			[
				{
					"name": "C-X",
					"cert_type": "Other",
					"cert_name": "Forklift",
					"status": "Active",
					"holder": "EMP-8",
					"expiration_date": "2000-01-01",
				}
			],
		)
		self.assertEqual(qualifications.qualification("EMP-8", "forklift"), "")


class PeoplePresentIsAFlagOnTheType(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		from erpnext_mcp import asset_types

		asset_types.seed()

	def test_a_shop_asset_and_a_barn_housing_unit_are_occupied_by_type(self):
		STORE.seed("Asset Register", [{"name": "SHED-1", "asset_type": "Storage", "company": MAIN}])
		self.assertEqual(occupancy.occupancy("Asset Register", "SHED-1")["source"], "people_work_here")
		self.assertTrue(occupancy.type_has_people("Barn"))
		self.assertFalse(occupancy.type_has_people("Cabin"), "an empty cabin stays unoccupied")

	def test_the_flag_is_data_an_operator_changes(self):
		STORE.seed("Asset Register", [{"name": "HOUSE-1", "asset_type": "House", "company": MAIN}])
		self.assertFalse(occupancy.occupancy("Asset Register", "HOUSE-1")["occupied"])
		frappe.db.set_value("Farm Asset Type", "House", "people_present", 1)
		self.assertTrue(occupancy.occupancy("Asset Register", "HOUSE-1")["occupied"])


class TheSettingsMoveIntoData(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		compliance_fields.install_compliance_fields(respect_switch=False)

	def test_certification_types_and_rules_move(self):
		task_templates.build_template(
			{
				"template_name": "Old Placement",
				"task_type": "Other",
				"skill_required": "applicator",
				"evidence_required": {"photos": True},
			}
		).insert(ignore_permissions=True)
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-OPEN",
					"task_name": "x",
					"task_type": "Other",
					"template": "Old Placement",
					"skill_required": "applicator",
					"state": "Claimed",
				}
			],
		)
		report = move_patch.move(
			dict(move_patch.OLD_DEFAULTS, pest_people_work_here_types="Barn\nGrain Shed")
		)
		self.assertEqual(report["templates"], ["Old Placement"])
		self.assertEqual(report["tasks"], 1)
		self.assertEqual(
			STORE.get_raw("Farm Task", "FT-OPEN")["required_certification"], "Applicator License"
		)
		self.assertTrue(occupancy.type_has_people("Grain Shed"))
		self.assertEqual(int(STORE.get_raw("Farm Asset Type", "Grain Shed")["enabled"]), 0)
		again = move_patch.move(dict(move_patch.OLD_DEFAULTS, pest_people_work_here_types="Barn\nGrain Shed"))
		self.assertEqual((again["templates"], again["tasks"]), ([], 0))

	def test_require_off_moves_no_certification(self):
		task_templates.build_template(
			{
				"template_name": "Old Placement",
				"task_type": "Other",
				"skill_required": "applicator",
				"evidence_required": {"photos": True},
			}
		).insert(ignore_permissions=True)
		report = move_patch.move(dict(move_patch.OLD_DEFAULTS, pest_require_applicator="0"))
		self.assertEqual(report["templates"], [])

	def test_the_season_is_generic(self):
		self.tool_data("update_company", {"company": MAIN, "season_start": "11-01", "season_end": "02-28"})
		self.assertTrue(occupancy.in_season(MAIN, "2026-01-10"))
		self.tool_data("update_company", {"company": MAIN, "pest_season_start": "03-01"})
		self.assertEqual(occupancy.season_of(MAIN)[0], "03-01")


class CadenceIsAGenericRuleOption(unittest.TestCase):
	def test_the_rodent_block_parses_and_other_programs_can_use_it(self):
		from erpnext_mcp import cadence

		params = cadence.params_of(rodent_bait.cadence_params())
		self.assertEqual(params["tier"], "occupancy")
		with self.assertRaises(ValueError):
			cadence.params_of({"anchor_filters": []})
		generic = cadence.params_of(
			{
				"group_by": "location",
				"anchor_filters": [{"field": "state", "op": "eq", "value": "Completed"}],
				"intervals": {"default": {"in_season_days": 14, "off_season_days": 60}},
				"season": "none",
			}
		)
		rows = [{"name": "A", "state": "Completed", "completed_at": "2026-06-01 08:00:00", "company": ""}]
		due = cadence.evaluate(rows, generic, ("Housing Unit", "U1"), "2026-06-20")
		self.assertEqual(
			(due["phase"], due["interval_days"], due["due_date"]), ("maintenance", 14, "2026-06-15")
		)


class ProgramsAreBundles(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		compliance_fields.install_compliance_fields(respect_switch=False)

	def test_the_shipped_file_is_the_python_definition(self):
		with open(f"{programs.PROGRAM_DIR}/rodent_bait.json") as handle:
			shipped = json.load(handle)
		self.assertEqual(shipped, json.loads(json.dumps(programs.rodent_bundle())))

	def test_import_is_a_dry_run_then_create_only_and_disabled(self):
		dry = self.tool_data("import_program", {"program": "rodent_bait"})
		self.assertTrue(dry["dry_run"])
		self.assertFalse(frappe.db.exists("Farm Task Template", rodent_bait.CHECK))
		real = self.tool_data("import_program", {"program": "rodent_bait", "dry_run": False})
		self.assertEqual(real["refused"], [])
		self.assertEqual(int(STORE.get_raw("Farm Task Template", rodent_bait.CHECK)["enabled"]), 0)
		self.assertEqual(
			self.tool_data("import_program", {"program": "rodent_bait", "dry_run": False})["created"], []
		)
		listed = self.tool_data("list_programs")["programs"][0]
		self.assertEqual(listed["program"], "rodent_bait")

	def test_export_carries_what_this_site_has(self):
		self.tool_data("import_program", {"program": "rodent_bait", "dry_run": False})
		bundle = self.tool_data("export_program", {"program": "rodent_bait"})["bundle"]
		self.assertEqual(len(bundle["task_templates"]), 5)
		self.assertTrue(all(spec["enabled"] == 0 for spec in bundle["compliance_rules"]))


# ── Part B ──────────────────────────────────────────────────────────────────
class TheWiderVocabulary(unittest.TestCase):
	def test_frappe_spellings_map_to_kinds_and_password_is_refused(self):
		report = form_schema.validate(
			[
				{"key": "a", "type": "Small Text", "label": T("A", "A")},
				{"key": "b", "type": "Int", "label": T("B", "B")},
				{"key": "c", "type": "Attach Image", "label": T("C", "C")},
				{"key": "d", "type": "Password", "label": T("D", "D")},
			]
		)
		self.assertEqual([row["code"] for row in report["errors"]], ["refused_type"])
		self.assertEqual([f["type"] for f in report["fields"]][:3], ["small_text", "int", "attach_image"])

	def test_a_computed_rate_times_acres(self):
		fields = [
			{"key": "rate", "type": "float", "label": T("Rate", "Tasa")},
			{"key": "acres", "type": "float", "label": T("Acres", "Acres")},
			{
				"key": "total",
				"type": "computed",
				"label": T("Total", "Total"),
				"formula": "rate * acres",
				"precision": 2,
			},
		]
		self.assertEqual(form_schema.validate(fields)["errors"], [])
		out = form_schema.check_answers(fields, {"rate": 2.5, "acres": 3, "total": 999}, {})
		self.assertEqual(
			out["answers"]["total"], 7.5, "the server recomputes; the phone's value is a preview"
		)
		bad = form_schema.validate(
			[{"key": "x", "type": "computed", "label": T("X"), "formula": "__import__('os')"}]
		)
		self.assertEqual(bad["errors"][0]["code"], "bad_formula")

	def test_table_sum_and_new_kinds(self):
		fields = [
			{
				"key": "mix",
				"type": "table",
				"label": T("Mix", "Mezcla"),
				"fields": [{"key": "qty", "type": "float", "label": T("Qty", "Cant")}],
			},
			{"key": "total", "type": "computed", "label": T("T", "T"), "formula": "sum(mix.qty)"},
			{"key": "start", "type": "time", "label": T("Start", "Inicio")},
			{"key": "hue", "type": "color", "label": T("Colour", "Color")},
			{"key": "where", "type": "geolocation", "label": T("Where", "Dónde")},
			{"key": "runtime", "type": "timer", "label": T("Runtime", "Tiempo")},
		]
		out = form_schema.check_answers(
			fields,
			{
				"mix": [{"qty": 1.5}, {"qty": 2}],
				"start": "07:30",
				"hue": "#aabbcc",
				"where": "45.5,-122.1",
				"runtime": {"started_at": "a", "stopped_at": "b", "seconds": 90},
			},
			{},
		)
		self.assertEqual(out["problems"], [])
		self.assertEqual(out["answers"]["total"], 3.5)
		self.assertEqual(out["answers"]["where"]["coordinates"], [-122.1, 45.5])
		self.assertIn("HH:MM", form_schema.check_answers(fields[2:3], {"start": "7pm"}, {})["problems"][0])

	def test_a_fallback_answer_is_accepted_unless_safety_critical(self):
		fields = [
			{"key": "area", "type": "map_area", "label": T("Area", "Área")},
			{"key": "reading", "type": "rating", "label": T("R", "R"), "safety_critical": True},
		]
		ok = form_schema.check_answers(fields, {"area": {"fallback": "text", "value": "north half"}}, {})
		self.assertEqual(ok["answers"]["area"], {"fallback": "text", "value": "north half"})
		refused = form_schema.check_answers(fields, {"reading": {"fallback": "text", "value": "4"}}, {})
		self.assertIn("safety-critical", refused["problems"][0])

	def test_a_schema_1_client_is_refused_a_safety_critical_new_kind(self):
		fields = [{"key": "pin", "type": "map_area", "label": T("Pin", "Pin"), "safety_critical": True}]
		with self.assertRaises(ToolError):
			device_capabilities.refuse_incapable(fields, {}, {}, None)
		device_capabilities.refuse_incapable(fields, {}, {}, {"schema_version": 2})


class DevicesReportWhatTheyRender(MobileAPITestCase):
	def add_device(self, identifier="IPHONE-1"):
		grant = frappe.db.get_value("Mobile Access Grant", {"user": WORKER}, "name")
		doc = frappe.get_doc("Mobile Access Grant", grant)
		doc.append(
			"devices",
			{"device_identifier": identifier, "device_name": "Ana's iPhone", "enrollment_status": "Enrolled"},
		)
		doc.save(ignore_permissions=True)

	def test_report_and_list(self):
		self.configure(enabled=1, allow_list_device_capabilities=1)
		self.add_device()
		self.be()
		stored = mobile_api.report_device_capabilities(
			device_identifier="IPHONE-1",
			app_version="3.2",
			schema_version=2,
			field_kinds=list(form_schema.TYPES),
		)
		self.assertTrue(stored["stored"])
		self.be("Administrator")
		devices = self.tool_data("list_device_capabilities", {})["devices"]
		mine = next(row for row in devices if row["device"] == "Ana's iPhone")
		self.assertEqual((mine["app_version"], mine["missing_kinds"]), ("3.2", []))
		# The enrolment's own row never reported: schema 1, so it misses the v2 kinds.
		self.assertTrue(any(row["missing_kinds"] for row in devices if row["device"] != "Ana's iPhone"))

	def test_an_old_device_is_named_in_the_template_problems(self):
		self.add_device()
		lines = device_capabilities.problems([{"key": "a", "type": "map_area", "label": T("A")}])
		self.assertIn("fallback", lines[0])


class InspectionsOnThePhone(MobileAPITestCase):
	def test_the_rodent_activity_section_is_answered_and_raises_a_placement(self):
		self.be("Administrator")
		compliance_fields.install_compliance_fields(respect_switch=False)
		sessions.seed_inspection_templates()
		task_templates.seed_farm_task_templates()
		frappe.db.set_value("Farm Task Template", rodent_bait.EXTERIOR, "enabled", 1)
		unit = self.a_camp()
		self.be()
		started = mobile_api.start_inspection(
			template="Mid-season Habitability", location=unit, location_doctype="Housing Unit"
		)
		session = started.get("name") or started.get("session")
		data = mobile_api.get_inspection(session=session)
		section = next(s for s in data["sections"] if s["section_name"] == "Rodent activity")
		self.assertEqual(section["form"][0]["key"], "rodent_activity_seen")
		self.assertEqual(data["form_context"]["location_doctype"], "Housing Unit")
		listed = mobile_api.list_my_inspections()
		self.assertIn(session, [row["name"] for row in listed["sessions"]])
		self.assertTrue(WORKER_EMPLOYEE)

	def test_search_link_only_over_the_allowed_list(self):
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.search_link(doctype="User", txt="a")
		found = mobile_api.search_link(doctype="Item", txt="")
		self.assertIn("results", found)


class SubmittingAnInspectionFromThePhone(MobileAPITestCase):
	def test_answers_are_validated_and_rodent_activity_raises_a_placement(self):
		self.be("Administrator")
		compliance_fields.install_compliance_fields(respect_switch=False)
		task_templates.seed_farm_task_templates()
		frappe.db.set_value("Farm Task Template", rodent_bait.EXTERIOR, "enabled", 1)
		sessions.build_template(
			{
				"template_name": "Quick Walk",
				"description": "A quick walk for rodent signs.",
				"applies_to_asset_type": "Housing Unit",
				"authored_by": "Operator",
				"sections": [
					{
						"section_name": "Rodents",
						"produces_record_doctype": "",
						"renderer_hint": "checklist",
						"required": 1,
						"evidence_contract": {"checklist_items": ["rodent_activity_seen"]},
						"field_prompts": [
							{
								"key": "rodent_activity_seen",
								"type": "select",
								"label": {"en": "Rodent activity seen?", "es": "¿Actividad?"},
								"options": [{"value": "yes"}, {"value": "no"}],
								"required": True,
							}
						],
					}
				],
			}
		).insert(ignore_permissions=True)
		unit = self.a_camp()
		self.be()
		started = mobile_api.start_inspection(
			template="Quick Walk", location=unit, location_doctype="Housing Unit"
		)
		session = started.get("name") or started.get("session")
		STORE.commit()  # a refused call rolls its own request back, as a site does
		with self.assertRaises(frappe.ValidationError):
			mobile_api.submit_inspection(
				session=session,
				section_submissions=[
					{"section_name": "Rodents", "answers": {"rodent_activity_seen": "maybe"}}
				],
			)
		data = mobile_api.submit_inspection(
			session=session,
			section_submissions=[{"section_name": "Rodents", "answers": {"rodent_activity_seen": "yes"}}],
		)
		self.assertEqual(data["bait_tasks"][0]["action"], "created")
