# SPDX-License-Identifier: MIT
"""Renderable templates and product labels on the phone (v0.204.0).

docs/design/form_schema_and_labels.md. Tim: any template created through MCP
must render on the iPhone with no code change, and "an applicator can look at
the label of the product they are handling".
"""

import json
import unittest

import frappe

from erpnext_mcp import (
	compliance_fields,
	form_schema,
	product_labels,
	rodent_bait,
	task_forms,
	task_templates,
)
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.patches import rebuild_rodent_templates as rebuild_patch

from .fixtures import MAIN, V12TestCase
from .harness import STORE
from .test_api_mobile import MobileAPITestCase
from .test_rodent_bait import A_SIGNED_PHOTO, answers_for

ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_housing_unit",
		"create_farm_task_template",
		"update_farm_task_template",
		"get_farm_task_template",
		"preview_farm_task_template",
		"create_task_from_template",
		"assign_farm_task",
		"start_farm_task",
		"complete_farm_task",
		"get_farm_task",
		"approve_task_step",
		"list_pest_control_applications",
	)
}

T = lambda en, es="": {"en": en, "es": es}  # noqa: E731


# ── the vocabulary ──────────────────────────────────────────────────────────
class TheVocabulary(unittest.TestCase):
	def test_errors_the_phone_could_not_render(self):
		report = form_schema.validate(
			[
				{"key": "Bad Key", "type": "text", "label": T("x")},
				{"key": "a", "type": "hologram", "label": T("x")},
				{"key": "b", "type": "select", "label": T("x")},
				{"key": "c", "type": "text", "label": T("x"), "show_if": {"field": "nope", "equals": 1}},
				{"key": "d", "type": "measurement", "label": T("x"), "uom": {"from_field": "c"}},
				{"key": "e", "type": "approval", "label": T("x")},
				{"key": "f", "type": "link", "label": T("x"), "link": {"doctype": "Customer"}},
			]
		)
		codes = sorted({row["code"] for row in report["errors"]})
		self.assertEqual(
			codes,
			["bad_condition", "bad_key", "bad_link", "bad_uom", "no_options", "no_role", "unknown_type"],
		)

	def test_the_drafted_interior_templates_faults_are_warned_about(self):
		"""OML's draft: prose branching, a free-text product, one photo per station, a text approval."""
		report = form_schema.validate(
			[
				{"key": "approval", "type": "text", "label": T("Farm Manager approval obtained (name/date)")},
				{"key": "product", "type": "text", "label": T("Product and EPA Reg. No. recorded")},
				{
					"key": "notice",
					"type": "photo",
					"label": T("OCCUPIED tier: notice posted (N/A only if UNOCCUPIED)"),
				},
				{"key": "stations", "type": "photo", "label": T("Photo of EACH station with room visible")},
			]
		)
		self.assertEqual(report["errors"], [])
		codes = {row["code"] for row in report["warnings"]}
		self.assertTrue(
			{"approval_as_text", "free_text_product", "prose_branching", "one_photo_for_many", "english_only"}
			<= codes
		)

	def test_conditions_hide_and_require(self):
		fields = [
			{
				"key": "tier",
				"type": "select",
				"label": T("Tier"),
				"options": [{"value": "o"}, {"value": "u"}],
			},
			{
				"key": "notice",
				"type": "check",
				"label": T("Notice"),
				"required": True,
				"show_if": {"field": "tier", "equals": "o"},
			},
			{
				"key": "note",
				"type": "text",
				"label": T("Note"),
				"required_if": {"context": "occupancy_at_creation", "equals": "Occupied"},
			},
		]
		ok = form_schema.check_answers(
			fields, {"tier": "u", "notice": True}, {"occupancy_at_creation": "Unoccupied"}
		)
		self.assertEqual(ok["problems"], [])
		self.assertNotIn("notice", ok["answers"], "a hidden field is not stored")
		bad = form_schema.check_answers(fields, {"tier": "o"}, {"occupancy_at_creation": "Occupied"})
		self.assertEqual(len(bad["problems"]), 2)

	def test_groups_counts_and_measurements(self):
		fields = [
			{"key": "product", "type": "link", "label": T("P"), "link": {"doctype": "Item"}},
			{
				"key": "stations",
				"type": "group",
				"label": T("S"),
				"min_count": 2,
				"required": True,
				"fields": [
					{
						"key": "count",
						"type": "measurement",
						"label": T("C"),
						"uom": {"from_field": "product"},
						"min": 0,
					}
				],
			},
		]
		self.assertEqual(form_schema.validate(fields)["errors"], [])
		short = form_schema.check_answers(fields, {"stations": [{"count": {"value": 1}}]}, {})
		self.assertIn("needs at least 2", short["problems"][0])
		neg = form_schema.check_answers(
			fields, {"stations": [{"count": {"value": -1}}, {"count": {"value": 2}}]}, {}
		)
		self.assertIn("at least 0", neg["problems"][0])

	def test_a_legacy_checklist_renders_and_answers_become_ticks(self):
		items = [
			{"item_name": "Smoke detector pressed", "required": True, "evidence_type": "None"},
			{"item_name": "Detector count", "required": True, "evidence_type": "Measurement"},
		]
		fields = form_schema.legacy_fields(items)
		self.assertEqual([f["type"] for f in fields], ["check", "measurement"])
		ticks = form_schema.ticks_from_answers(
			fields, {"smoke_detector_pressed": True, "detector_count": {"value": 3, "uom": ""}}, items
		)
		self.assertEqual(
			[t["item_name"] for t in ticks if t["done"]], ["Smoke detector pressed", "Detector count"]
		)

	def test_wizard_fields_share_the_vocabulary(self):
		out = form_schema.from_wizard_fields(
			[
				{
					"fieldname": "severity",
					"type": "select",
					"label": "Severity",
					"options": [{"value": "Minor", "label": "Minor"}],
				},
				{
					"fieldname": "who",
					"type": "employee_select",
					"label": "Who",
					"visible_if": {"field": "severity", "equals": "Minor"},
				},
			],
			[
				{
					"fieldname": "severity",
					"label": "Gravedad",
					"options": [{"value": "Minor", "label": "Menor"}],
				}
			],
		)
		self.assertEqual(out[0]["label"], {"en": "Severity", "es": "Gravedad"})
		self.assertEqual(out[1]["link"], {"doctype": "Employee"})
		self.assertEqual(out[1]["show_if"], {"field": "severity", "equals": "Minor"})

	def test_the_rebuilt_rodent_templates_render_clean(self):
		for spec in rodent_bait.SEED_TASK_TEMPLATES_V204:
			report = form_schema.validate(spec["form_schema"])
			self.assertEqual((report["errors"], report["warnings"]), ([], []), spec["template_name"])
			self.assertEqual(spec["task_type"], "Pest Control")


# ── templates, tasks, approvals, records ────────────────────────────────────
class FormTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		compliance_fields.install_compliance_fields(respect_switch=False)
		STORE.seed(
			"Item",
			[
				{
					"name": "BAIT-1",
					"item_code": "BAIT-1",
					"item_name": "Bait",
					"item_group": "Pest Control Products",
					"stock_uom": "Nos",
				}
			],
		)
		STORE.seed(
			"Certification",
			[
				{
					"name": "C-1",
					"cert_type": "Applicator License",
					"status": "Active",
					"holder": "EMP-001",
					"cert_name": "x",
				}
			],
		)

	def a_cabin(self):
		self.tool_data("create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 10})
		return self.tool_data(
			"create_housing_unit", {"parcel": "Mill Creek", "unit_name": "C1", "unit_type": "Cabin"}
		)["name"]

	def seeded(self):
		task_templates.seed_farm_task_templates()
		for name in rodent_bait.TEMPLATES:
			frappe.db.set_value("Farm Task Template", name, "enabled", 1)


class TemplatesRefuseWhatThePhoneCannotRender(FormTestCase):
	def payload(self, form):
		return {
			"template_name": "Walk",
			"task_type": "Inspection",
			"evidence_required": {"photos": True},
			"form_schema": form,
		}

	def test_create_refuses_errors_and_reports_warnings(self):
		message = self.tool_error(
			"create_farm_task_template", self.payload([{"key": "x", "type": "hologram", "label": T("x")}])
		)
		self.assertIn("cannot be rendered on the phone", message)
		data = self.tool_data(
			"create_farm_task_template", self.payload([{"key": "x", "type": "check", "label": T("Done")}])
		)
		self.assertTrue(any("Spanish" in line for line in data["warnings"]))
		problems = self.tool_data("get_farm_task_template", {"template": "Walk"})["problems"]
		self.assertTrue(any("no Spanish title" in line for line in problems))

	def test_preview_shows_both_branches(self):
		self.seeded()
		occupied = self.tool_data(
			"preview_farm_task_template",
			{
				"template": rodent_bait.EXTERIOR,
				"language": "es",
				"context": {"occupancy_at_creation": "Occupied"},
			},
		)
		empty = self.tool_data(
			"preview_farm_task_template",
			{"template": rodent_bait.EXTERIOR, "context": {"occupancy_at_creation": "Unoccupied"}},
		)
		self.assertIn("notice_posted", [f["key"] for f in occupied["rendered"]])
		self.assertIn("notice_posted", empty["hidden_by_conditions"])
		self.assertEqual(occupied["rendered"][0]["label"], "Producto")
		self.assertEqual(occupied["problems"], [])

	def test_preview_of_an_unsaved_body(self):
		data = self.tool_data(
			"preview_farm_task_template",
			{
				"template_body": {
					"template_name": "Draft",
					"task_type": "Other",
					"checklist": ["Product and EPA Reg. No. recorded"],
				}
			},
		)
		self.assertTrue(data["form_is_legacy_checklist"])
		self.assertTrue(any("task_type is Other" in line for line in data["problems"]))


class ApprovalsAndRecords(FormTestCase):
	def test_interior_waits_for_the_farm_manager_then_writes_the_record(self):
		self.seeded()
		unit = self.a_cabin()
		task = self.tool_data(
			"create_task_from_template",
			{"template": rodent_bait.INTERIOR, "location_doctype": "Housing Unit", "location": unit},
		)["name"]
		self.tool_data(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-001", "assigned_to_name": "Ana"}
		)
		message = self.tool_error("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("cannot start before it is approved", message)
		from .harness import set_roles

		set_roles("Administrator", ["System Manager", "Farm Manager"])
		self.tool_data("approve_task_step", {"task": task, "key": "manager_approval"})
		self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		fields = json.loads(STORE.get_raw("Farm Task", task)["form_schema"])
		answers = answers_for(fields, {"product": "BAIT-1"})
		answers["stations"] = [answers["stations"][0], answers["stations"][0]]
		self.tool_data(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"evidence_files": list(A_SIGNED_PHOTO),
				"farm_location_gps": "45.1,-122.1",
				"form_answers": answers,
			},
		)
		row = STORE.get_raw("Farm Task", task)
		self.assertEqual(row["bait_product"], "BAIT-1")
		self.assertEqual(int(row["label_available"] or 0), 0)
		records = self.tool_data("list_pest_control_applications", {})["applications"]
		self.assertEqual(records[0]["stations"], 2)
		self.assertEqual(records[0]["quantity"], 2.0)
		self.assertEqual(records[0]["placement"], "Interior")

	def test_a_missing_answer_is_refused_by_name(self):
		self.seeded()
		unit = self.a_cabin()
		task = self.tool_data(
			"create_task_from_template",
			{"template": rodent_bait.NOTICE, "location_doctype": "Housing Unit", "location": unit},
		)["name"]
		self.tool_data(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-001", "assigned_to_name": "Ana"}
		)
		self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"farm_location_gps": "1,1",
				"form_answers": {"product": "BAIT-1"},
			},
		)
		self.assertIn("English notice at each entrance: required", message)


class TheRebuildPatch(FormTestCase):
	def test_only_an_unedited_v203_seed_is_rebuilt(self):
		from erpnext_mcp.rodent_bait import SEED_TASK_TEMPLATES

		for spec in SEED_TASK_TEMPLATES[:2]:
			task_templates.build_template(spec).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Farm Task Template",
			SEED_TASK_TEMPLATES[1]["template_name"],
			"description",
			"DRAFT (AI-proposed)",
		)
		report = rebuild_patch.rebuild()
		self.assertEqual(report["rebuilt"], [SEED_TASK_TEMPLATES[0]["template_name"]])
		self.assertEqual(report["left"], [SEED_TASK_TEMPLATES[1]["template_name"]])
		self.assertEqual(rebuild_patch.rebuild()["rebuilt"], [])


# ── labels on the phone ─────────────────────────────────────────────────────
class LabelsOnThePhone(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		compliance_fields.install_compliance_fields(respect_switch=False)
		STORE.seed(
			"Item",
			[
				{
					"name": "PROWLER-PP",
					"item_code": "PROWLER-PP",
					"item_name": "PROWLER Place Pacs",
					"item_group": "Pest Control Products",
					"signal_word": "Caution",
					"interior_use_allowed": "Yes",
				}
			],
		)
		STORE.seed(
			"File",
			[
				{
					"name": "F-PDF",
					"file_name": "EPA label 12455-97 (2024-05-01).pdf",
					"file_url": "/private/files/l.pdf",
					"attached_to_doctype": "Item",
					"attached_to_name": "PROWLER-PP",
					"is_private": 1,
				},
				{
					"name": "F-IMG",
					"file_name": "label-1.jpg",
					"file_url": "/private/files/l1.jpg",
					"attached_to_doctype": "Item",
					"attached_to_name": "PROWLER-PP",
					"is_private": 1,
				},
				{
					"name": "F-OTHER",
					"file_name": "x.jpg",
					"file_url": "/private/files/x.jpg",
					"attached_to_doctype": "Employee",
					"attached_to_name": "E",
					"is_private": 1,
				},
			],
		)

	def test_the_label_is_the_fields_the_pdf_and_the_photos(self):
		self.be()
		data = mobile_api.get_item_label(item_code="PROWLER-PP")
		self.assertEqual([row["kind"] for row in data["files"]], ["epa_label_pdf", "label_photo"])
		self.assertTrue(data["label_available"])
		self.assertEqual(data["signal_word"], "Caution")
		self.assertTrue(product_labels.has_label_pdf("PROWLER-PP"))

	def test_only_that_items_files_are_served(self):
		self.be()
		with self.assertRaises(frappe.ValidationError):
			mobile_api.get_item_label_file(item_code="PROWLER-PP", file="F-OTHER")

	def test_a_view_is_recorded_once_a_day(self):
		self.be()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-X",
					"task_name": "x",
					"task_type": "Pest Control",
					"company": MAIN,
					"state": "Claimed",
				}
			],
		)
		first = task_forms.record_label_viewed("FT-X", "PROWLER-PP", "worker@example.com")
		again = task_forms.record_label_viewed("FT-X", "PROWLER-PP", "worker@example.com")
		self.assertEqual((first["recorded"], again["recorded"]), (True, False))
