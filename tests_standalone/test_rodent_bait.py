# SPDX-License-Identifier: MIT
"""Rodent bait at housing and buildings (v0.203.0).

docs/design/rodent_bait_program.md. Tim approved it on 2026-09-27: occupied
buildings in the strictest tier; checks every 7 days in season and monthly off
season, but only for maintenance stations with no activity; a new placement is
always active; only a COMPLETED removal clears interior bait.
"""

import json

import frappe

from erpnext_mcp import (
	agronomy_seed,
	asset_types,
	compliance_fields,
	compliance_rules,
	document_intel,
	enforcement,
	rodent_bait,
	sessions,
	task_templates,
)
from erpnext_mcp.alerts import engine
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.patches import add_rodent_sections_to_inspection_templates as sections_patch
from erpnext_mcp.patches import backfill_farm_task_completed_at as backfill_patch

from .fixtures import MAIN, V12TestCase
from .harness import STORE

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_housing_unit",
		"update_housing_unit",
		"create_housing_assignment",
		"create_task_from_template",
		"claim_farm_task",
		"assign_farm_task",
		"start_farm_task",
		"complete_farm_task",
		"get_farm_task",
		"update_company",
		"update_compliance_rule",
	)
}

A_SIGNED_PHOTO = [
	{"file_url": "/files/station.jpg", "evidence_type": "Photo", "caption": "station locked"},
	{"file_url": "/files/sig.png", "evidence_type": "Signature", "caption": "signed"},
]


def answers_for(fields, overrides=None):
	"""A complete, valid set of answers for a form — every field filled the simplest way."""
	out = {}
	for field in fields:
		kind = field["type"]
		if kind in ("approval", "info"):
			continue
		if kind in ("check", "attestation"):
			out[field["key"]] = True
		elif kind == "select":
			out[field["key"]] = field["options"][-1]["value"]
		elif kind in ("number",):
			out[field["key"]] = 0
		elif kind == "measurement":
			out[field["key"]] = {"value": 1, "uom": "Nos"}
		elif kind == "photo":
			out[field["key"]] = ["/files/station.jpg"]
		elif kind == "date":
			out[field["key"]] = "2026-10-01"
		elif kind == "link":
			out[field["key"]] = "BAIT-1"
		elif kind == "group":
			out[field["key"]] = [answers_for(field["fields"])]
		else:
			out[field["key"]] = "x"
	out.update(overrides or {})
	return out


class BaitTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		compliance_fields.install_compliance_fields(respect_switch=False)
		task_templates.seed_farm_task_templates()
		for name in rodent_bait.TEMPLATES:
			frappe.db.set_value("Farm Task Template", name, "enabled", 1)
		# Every worker in these tests is a licensed applicator unless a test says otherwise.
		self.licence("EMP-001")
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

	# -- fixtures ------------------------------------------------------------
	def licence(self, holder):
		STORE.seed(
			"Certification",
			[
				{
					"name": f"CERT-{holder}",
					"cert_name": "Oregon applicator",
					"cert_type": "Applicator License",
					"status": "Active",
					"holder": holder,
					"expiration_date": "2099-12-31",
				}
			],
		)

	def a_cabin(self, unit_name="MC-Cabin-01", **overrides):
		if not STORE.rows("Parcel"):
			self.tool_data(
				"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
			)
		payload = {"parcel": "Mill Creek", "unit_name": unit_name, "unit_type": "Cabin", "capacity": 4}
		payload.update(overrides)
		return self.tool_data("create_housing_unit", payload)["name"]

	def a_task(self, template, unit, **overrides):
		payload = {"template": template, "location_doctype": "Housing Unit", "location": unit}
		payload.update(overrides)
		return self.tool_data("create_task_from_template", payload)["name"]

	def finish(self, task, worker="EMP-001", **extra):
		"""Assign, start and complete a task with every required checklist item ticked."""
		self.tool_data("assign_farm_task", {"task": task, "assigned_to": worker, "assigned_to_name": "Ana"})
		self.tool_data("start_farm_task", {"task": task, "worker_id": worker})
		payload = {
			"task": task,
			"worker_id": worker,
			"evidence_files": list(A_SIGNED_PHOTO),
			"findings_text": "done",
			"completion_narrative": "done",
			"farm_location_gps": "45.1,-122.1",
			"form_answers": answers_for(
				json.loads(self.task(task).get("form_schema") or "[]"),
				{
					**(
						{"rodent_activity_found": "yes" if extra["bait_activity"] else "no"}
						if "bait_activity" in extra
						else {}
					),
					**extra.pop("answers", {}),
				},
			),
		}
		payload.update(extra)
		return self.tool_data("complete_farm_task", payload)

	def task(self, name):
		return STORE.get_raw("Farm Task", name)


# ── occupancy ───────────────────────────────────────────────────────────────
class Occupancy(BaitTestCase):
	def test_an_active_assignment_makes_a_unit_occupied(self):
		unit = self.a_cabin()
		self.assertEqual(rodent_bait.occupancy("Housing Unit", unit)["source"], "none")
		self.tool_data(
			"create_housing_assignment",
			{"unit": unit, "assigned_date": "2020-01-01", "employee_name": "Ana"},
		)
		self.assertEqual(rodent_bait.occupancy("Housing Unit", unit)["source"], "housing_assignment")

	def test_the_manual_flag_is_first_class(self):
		"""Mill Creek: four houses occupied at takeover, no assignment behind any of them."""
		unit = self.a_cabin(occupied=True)
		answer = rodent_bait.occupancy("Housing Unit", unit)
		self.assertEqual((answer["occupied"], answer["source"]), (True, "manual_flag"))

	def test_a_house_asset_marked_occupied_and_a_shop_by_type(self):
		STORE.seed(
			"Asset Register",
			[
				{"name": "MC-House-1", "asset_type": "House", "company": MAIN, "occupied": 1},
				{"name": "MC-Shop", "asset_type": "Storage", "company": MAIN},
				{"name": "MC-Cabin-9", "asset_type": "Cabin", "company": MAIN},
			],
		)
		self.assertEqual(rodent_bait.occupancy("Asset Register", "MC-House-1")["source"], "manual_flag")
		self.assertEqual(rodent_bait.occupancy("Asset Register", "MC-Shop")["source"], "people_work_here")
		self.assertFalse(rodent_bait.occupancy("Asset Register", "MC-Cabin-9")["occupied"])

	def test_the_snapshot_is_stamped_once_and_never_recomputed(self):
		unit = self.a_cabin(occupied=True)
		task = self.a_task(rodent_bait.EXTERIOR, unit)
		row = self.task(task)
		self.assertEqual((row["occupancy_at_creation"], row["occupancy_source"]), ("Occupied", "manual_flag"))
		self.assertEqual(row["bait_placement"], "Exterior")
		self.tool_data("update_housing_unit", {"unit": unit, "occupied": False})
		doc = frappe.get_doc("Farm Task", task)
		doc.notes = "edited"
		doc.save()
		self.assertEqual(self.task(task)["occupancy_at_creation"], "Occupied")

	def test_cabin_and_house_are_seeded_asset_types(self):
		self.assertIn("Cabin", asset_types.SEEDED_NAMES)
		self.assertIn("House", asset_types.SEEDED_NAMES)


# ── the completion time ─────────────────────────────────────────────────────
class CompletedAt(BaitTestCase):
	def test_set_on_completion_and_not_moved_by_an_edit(self):
		unit = self.a_cabin()
		task = self.a_task(rodent_bait.EXTERIOR, unit)
		self.finish(task)
		stamped = self.task(task)["completed_at"]
		self.assertTrue(stamped)
		doc = frappe.get_doc("Farm Task", task)
		doc.notes = "later edit"
		doc.save()
		self.assertEqual(self.task(task)["completed_at"], stamped)

	def test_the_backfill_takes_the_assignment_time(self):
		STORE.seed(
			"Farm Task", [{"name": "FT-OLD", "task_name": "x", "task_type": "Other", "state": "Completed"}]
		)
		STORE.seed(
			"Farm Task Assignment",
			[
				{
					"name": "FTA-OLD",
					"task": "FT-OLD",
					"completed_at": "2026-05-01 10:00:00",
					"state": "Completed",
				}
			],
		)
		report = backfill_patch.backfill_completed_at()
		self.assertEqual(report["from_assignment"], 1)
		self.assertEqual(str(self.task("FT-OLD")["completed_at"]), "2026-05-01 10:00:00")


# ── the notice, and who may do it ───────────────────────────────────────────
class BeforeAPlacementStarts(BaitTestCase):
	def test_an_occupied_placement_waits_for_the_notice(self):
		unit = self.a_cabin(occupied=True)
		task = self.a_task(rodent_bait.EXTERIOR, unit)
		self.tool_data(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-001", "assigned_to_name": "Ana"}
		)
		message = self.tool_error("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("occupant notice", message)
		notice = self.a_task(rodent_bait.NOTICE, unit)
		self.finish(notice)
		self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})

	def test_an_unoccupied_placement_needs_no_notice(self):
		unit = self.a_cabin()
		task = self.a_task(rodent_bait.EXTERIOR, unit)
		self.finish(task)
		self.assertEqual(self.task(task)["state"], "Completed")

	def test_placement_needs_an_applicator_and_a_check_does_not(self):
		unit = self.a_cabin()
		task = self.a_task(rodent_bait.EXTERIOR, unit)
		message = self.tool_error(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-777", "assigned_to_name": "Bo"}
		)
		self.assertIn("requires 'Applicator License'", message)
		self.licence("EMP-777")
		self.tool_data("assign_farm_task", {"task": task, "assigned_to": "EMP-777", "assigned_to_name": "Bo"})
		check = self.a_task(rodent_bait.CHECK, unit)
		self.tool_data(
			"assign_farm_task", {"task": check, "assigned_to": "EMP-888", "assigned_to_name": "Cy"}
		)


# ── the bait state and the next check ───────────────────────────────────────
class TheIntervalLogic(BaitTestCase):
	def seed(self, unit, *rows, company=MAIN):
		STORE.seed(
			"Farm Task",
			[
				{
					"name": name,
					"task_name": name,
					"task_type": "Other",
					"template": template,
					"state": "Completed",
					"completed_at": f"{day} 09:00:00",
					"location_doctype": "Housing Unit",
					"location": unit,
					"company": company,
					"bait_activity": activity,
				}
				for name, template, day, activity in rows
			],
		)

	def test_the_location_state_follows_the_completions(self):
		unit = self.a_cabin()
		placement = self.a_task(rodent_bait.EXTERIOR, unit)
		self.finish(placement)
		self.assertEqual(STORE.get_raw("Housing Unit", unit)["rodent_bait_state"], "Active")
		check = self.a_task(rodent_bait.CHECK, unit)
		self.finish(check, bait_activity=False)
		self.assertEqual(self.task(check)["bait_activity"], "No activity")
		self.assertEqual(STORE.get_raw("Housing Unit", unit)["rodent_bait_state"], "Maintenance")
		check2 = self.a_task(rodent_bait.CHECK, unit)
		self.finish(check2, bait_activity=True)
		self.assertEqual(STORE.get_raw("Housing Unit", unit)["rodent_bait_state"], "Active")
		removal = self.a_task(rodent_bait.REMOVAL, unit)
		self.finish(removal)
		self.assertEqual(STORE.get_raw("Housing Unit", unit)["rodent_bait_state"], "Cleared")

	def test_knockdown_keeps_seven_days_even_off_season(self):
		unit = self.a_cabin()
		self.seed(unit, ("FT-P", rodent_bait.EXTERIOR, "2026-12-01", ""))
		due = rodent_bait.next_check("Housing Unit", unit, today="2026-12-02")
		self.assertEqual((due["phase"], due["interval_days"], due["in_season"]), ("knockdown", 7, False))

	def test_a_quiet_maintenance_station_goes_monthly_off_season_only(self):
		unit = self.a_cabin()
		self.seed(
			unit,
			("FT-P", rodent_bait.EXTERIOR, "2026-11-01", ""),
			("FT-C", rodent_bait.CHECK, "2026-11-20", "No activity"),
		)
		frappe.db.set_value("Housing Unit", unit, "rodent_bait_state", "Maintenance")
		off = rodent_bait.next_check("Housing Unit", unit, today="2026-11-21")
		self.assertEqual((off["phase"], off["interval_days"]), ("maintenance", 30))
		self.assertEqual(off["due_date"], "2026-12-20")
		on = rodent_bait.next_check("Housing Unit", unit, today="2026-06-01")
		self.assertEqual(on["interval_days"], 7)

	def test_activity_keeps_it_weekly_off_season(self):
		unit = self.a_cabin()
		self.seed(
			unit,
			("FT-P", rodent_bait.EXTERIOR, "2026-11-01", ""),
			("FT-C", rodent_bait.CHECK, "2026-11-20", "Activity"),
		)
		frappe.db.set_value("Housing Unit", unit, "rodent_bait_state", "Active")
		due = rodent_bait.next_check("Housing Unit", unit, today="2026-11-21")
		self.assertEqual((due["phase"], due["interval_days"]), ("active", 7))

	def test_a_completed_removal_ends_the_round(self):
		unit = self.a_cabin()
		self.seed(
			unit,
			("FT-P", rodent_bait.EXTERIOR, "2026-11-01", ""),
			("FT-R", rodent_bait.REMOVAL, "2026-11-10", ""),
		)
		self.assertIsNone(rodent_bait.next_check("Housing Unit", unit, today="2026-11-21"))

	def test_the_scanner_raises_an_overdue_check(self):
		unit = self.a_cabin()
		self.seed(unit, ("FT-P", rodent_bait.EXTERIOR, "2026-06-01", ""))
		found = rodent_bait.scan_check_overdue({"today": "2026-06-20", "company": MAIN})
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0].severity, "Critical")
		self.assertIn("12 day(s) overdue", found[0].message)

	def test_activity_is_read_from_the_checklist_when_not_said(self):
		self.assertTrue(rodent_bait.activity_of(None, []))
		self.assertFalse(
			rodent_bait.activity_of(
				None, [{"item_name": "Consumption per station (none / partial / all)", "note": "none"}]
			)
		)
		self.assertTrue(
			rodent_bait.activity_of(None, [{"item_name": "Carcasses collected (count)", "note": "2"}])
		)

	def test_the_season_wraps_the_year(self):
		self.tool_data(
			"update_company", {"company": MAIN, "pest_season_start": "11-01", "pest_season_end": "2-28"}
		)
		self.assertTrue(rodent_bait.in_season(MAIN, "2026-01-15"))
		self.assertFalse(rodent_bait.in_season(MAIN, "2026-06-15"))
		self.assertIn(
			"MM-DD", self.tool_error("update_company", {"company": MAIN, "pest_season_start": "March"})
		)


# ── the rules ───────────────────────────────────────────────────────────────
class TheRules(BaitTestCase):
	def test_only_a_completed_removal_clears_interior_bait(self):
		unit = self.a_cabin()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-I",
					"task_name": "i",
					"task_type": "Other",
					"template": rodent_bait.INTERIOR,
					"state": "Completed",
					"completed_at": "2026-06-01 09:00:00",
					"location_doctype": "Housing Unit",
					"location": unit,
				},
				{
					"name": "FT-R",
					"task_name": "r",
					"task_type": "Housing-Cleanup",
					"template": rodent_bait.REMOVAL,
					"state": "In-Progress",
					"location_doctype": "Housing Unit",
					"location": unit,
				},
			],
		)
		spec = next(
			row for row in rodent_bait.rule_seed_specs() if row["rule_id"] == "rodent_bait_interior_placement"
		)
		config = compliance_rules.parse_supersession(json.dumps(spec["superseded_by_later_clean"]))
		candidate = self.task("FT-I")
		clean = engine._clean_index("Farm Task", "completed_at", config, "", [])
		self.assertFalse(engine._superseded(candidate, config, clean, "completed_at"))
		self.assertEqual(rodent_bait.uncleared_interior("Housing Unit", unit), ["FT-I"])
		frappe.db.set_value("Farm Task", "FT-R", "state", "Completed")
		frappe.db.set_value("Farm Task", "FT-R", "completed_at", "2026-06-10 09:00:00")
		clean = engine._clean_index("Farm Task", "completed_at", config, "", [])
		self.assertTrue(engine._superseded(candidate, config, clean, "completed_at"))
		self.assertEqual(rodent_bait.uncleared_interior("Housing Unit", unit), [])

	def test_severity_follows_the_tier(self):
		row = {
			"extra_parameters_json": json.dumps(
				{"severity_by_field": {"field": "occupancy_at_creation", "map": {"Unoccupied": "Warning"}}}
			)
		}
		self.assertEqual(
			engine._severity_by_field(row, {"occupancy_at_creation": "Unoccupied"}, "Critical"), "Warning"
		)
		self.assertEqual(
			engine._severity_by_field(row, {"occupancy_at_creation": "Occupied"}, "Critical"), "Critical"
		)

	def test_seeded_rules_are_disabled_and_unapproved(self):
		compliance_rules.seed_compliance_rules()
		for rule_id in (
			"rodent_bait_interior_placement",
			"rodent_bait_check_overdue",
			"pest_control_label_fields_missing",
			"rodent_bait_label_conformance",
			"control_housing_preoccupancy_bait_clearance",
		):
			row = STORE.get_raw("Compliance Rule", compliance_rules.resolve(rule_id))
			self.assertEqual(int(row.get("enabled") or 0), 0, rule_id)
			self.assertFalse(row.get("human_approved_by"), rule_id)
		extra = json.loads(
			STORE.get_raw("Compliance Rule", compliance_rules.resolve("rodent_bait_check_overdue"))[
				"extra_parameters_json"
			]
		)
		self.assertEqual(extra["occupied"], {"in_season_days": 7, "off_season_days": 30})
		self.assertEqual(extra["notify_roles"], ["Farm Manager"])

	def test_seeded_templates_are_disabled_with_the_right_skills(self):
		for name, skill in (
			(rodent_bait.EXTERIOR, "applicator"),
			(rodent_bait.REMOVAL, "applicator"),
			(rodent_bait.CHECK, "camp_maintenance"),
			(rodent_bait.NOTICE, "camp_maintenance"),
		):
			spec = next(row for row in rodent_bait.SEED_TASK_TEMPLATES if row["template_name"] == name)
			self.assertEqual((spec["enabled"], spec["skill_required"]), (0, skill))

	def test_an_interior_placement_of_an_outdoor_product_is_critical(self):
		unit = self.a_cabin()
		STORE.seed(
			"Item",
			[
				{
					"name": "OUTDOOR-BAIT",
					"item_name": "Outdoor Bait",
					"item_group": "Pest Control Products",
					"interior_use_allowed": "No",
				}
			],
		)
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-I",
					"task_name": "i",
					"task_type": "Other",
					"template": rodent_bait.INTERIOR,
					"state": "Completed",
					"completed_at": "2026-06-01 09:00:00",
					"location_doctype": "Housing Unit",
					"location": unit,
					"bait_product": "OUTDOOR-BAIT",
				}
			],
		)
		found = rodent_bait.scan_label_conformance({"company": ""})
		self.assertEqual([row.severity for row in found], ["Critical"])


# ── the pre-occupancy gate ──────────────────────────────────────────────────
class ThePreOccupancyGate(BaitTestCase):
	def interior_bait_at(self, unit):
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-I",
					"task_name": "i",
					"task_type": "Other",
					"template": rodent_bait.INTERIOR,
					"state": "Completed",
					"completed_at": "2026-06-01 09:00:00",
					"location_doctype": "Housing Unit",
					"location": unit,
				}
			],
		)

	def assign(self, unit):
		return {"unit": unit, "assigned_date": "2026-07-01", "employee_name": "Ana"}

	def test_off_by_default_so_the_assignment_goes_through(self):
		compliance_rules.seed_compliance_rules()
		unit = self.a_cabin()
		self.interior_bait_at(unit)
		data = self.tool_data("create_housing_assignment", self.assign(unit))
		self.assertEqual(data["bait_clearance"]["mode"], "Off")

	def test_enforced_refuses_and_names_the_removal(self):
		compliance_rules.seed_compliance_rules()
		name = compliance_rules.resolve("control_housing_preoccupancy_bait_clearance")
		frappe.db.set_value("Compliance Rule", name, "enabled", 1)
		frappe.db.set_value("Compliance Rule", name, "enforcement_mode", "Enforced")
		unit = self.a_cabin()
		self.interior_bait_at(unit)
		message = self.tool_error("create_housing_assignment", self.assign(unit))
		self.assertIn("Removal and Clearance", message)
		self.assertIn("housing_preoccupancy_bait_clearance", enforcement.CONTROL_POINTS)


# ── the triggers ────────────────────────────────────────────────────────────
class TheTriggers(BaitTestCase):
	def setUp(self):
		super().setUp()
		STORE.seed(
			"Item",
			[
				{
					"name": "PROWLER-PP",
					"item_name": "PROWLER Place Pacs",
					"item_group": "Pest Control Products",
					"tamper_resistant_station_required": 1,
					"max_distance_from_structure_ft": 100,
					"burrow_baiting_allowed": "No",
					"min_bait_days": 7,
					"interior_use_allowed": "Yes",
				},
				{
					"name": "OUTDOOR-BAIT",
					"item_name": "Outdoor Bait",
					"item_group": "Pest Control Products",
					"interior_use_allowed": "No",
				},
				{"name": "GLOVES", "item_name": "Gloves", "item_group": "Consumable"},
			],
		)

	def test_a_bait_issue_to_a_cabin_raises_the_placement_and_the_notice(self):
		unit = self.a_cabin(occupied=True)
		bait = rodent_bait.check_stock_bait_args({"bait_location": unit})
		out = rodent_bait.from_stock_entry(
			[{"item_code": "PROWLER-PP", "qty": 4, "uom": "Place Pac"}], bait, MAIN, "MAT-1"
		)
		self.assertEqual(out[0]["action"], "created")
		row = self.task(out[0]["task"])
		self.assertEqual(
			(row["origin"], row["bait_product"], row["template"]),
			("field_reported", "PROWLER-PP", rodent_bait.EXTERIOR),
		)
		self.assertIn("no burrow baiting", row["notes"])
		self.assertTrue(out[0].get("notice"))
		again = rodent_bait.from_stock_entry([{"item_code": "PROWLER-PP", "qty": 2}], bait, MAIN, "MAT-2")
		self.assertEqual(again[0]["action"], "linked")

	def test_interior_of_an_outdoor_product_is_refused(self):
		unit = self.a_cabin()
		bait = rodent_bait.check_stock_bait_args({"bait_location": unit, "bait_placement": "Interior"})
		out = rodent_bait.from_stock_entry([{"item_code": "OUTDOOR-BAIT", "qty": 1}], bait, MAIN, "MAT-1")
		self.assertEqual(out[0]["action"], "refused")

	def test_nothing_happens_for_other_items_or_a_disabled_template(self):
		unit = self.a_cabin()
		self.assertIsNone(
			rodent_bait.from_stock_entry([{"item_code": "GLOVES", "qty": 1}], ("", "", ""), MAIN, "M")
		)
		frappe.db.set_value("Farm Task Template", rodent_bait.EXTERIOR, "enabled", 0)
		bait = rodent_bait.check_stock_bait_args({"bait_location": unit})
		out = rodent_bait.from_stock_entry([{"item_code": "PROWLER-PP", "qty": 1}], bait, MAIN, "MAT-1")
		self.assertEqual(out[0]["action"], "skipped")
		self.assertIn("disabled", out[0]["message"])

	def test_a_bad_location_is_refused_before_anything_is_written(self):
		with self.assertRaises(ToolError):
			rodent_bait.check_stock_bait_args({"bait_location": "No-Such-Cabin"})
		with self.assertRaises(ToolError):
			rodent_bait.check_stock_bait_args({"bait_location": self.a_cabin(), "bait_placement": "Roof"})

	def test_rodent_activity_on_an_inspection_raises_a_placement(self):
		unit = self.a_cabin()
		session = {"name": "INS-1", "location_doctype": "Housing Unit", "location": unit, "company": MAIN}
		out = rodent_bait.activity_tasks(
			session, {"Rodent activity": {"checklist_items": {"rodent_activity_seen": True}}}
		)
		self.assertEqual(out[0]["action"], "created")
		self.assertEqual(
			rodent_bait.activity_tasks(
				session, {"Rodent activity": {"checklist_items": {"rodent_activity_seen": False}}}
			),
			[],
		)

	def test_bait_cleared_fails_while_interior_bait_is_out(self):
		unit = self.a_cabin()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-I",
					"task_name": "i",
					"task_type": "Other",
					"template": rodent_bait.INTERIOR,
					"state": "Completed",
					"completed_at": "2026-06-01 09:00:00",
					"location_doctype": "Housing Unit",
					"location": unit,
				}
			],
		)
		submitted = {"Rodent bait cleared": {"checklist_items": {"rodent_bait_cleared": True}, "notes": ""}}
		found = rodent_bait.review_clearance(
			{"location_doctype": "Housing Unit", "location": unit}, submitted
		)
		self.assertEqual(found[0]["placements"], ["FT-I"])
		self.assertFalse(submitted["Rodent bait cleared"]["checklist_items"]["rodent_bait_cleared"])
		self.assertIn("NOT cleared", submitted["Rodent bait cleared"]["notes"])


# ── camp maintenance filing, inspection sections, label facts ──────────────
class CampMaintenance(BaitTestCase):
	def test_the_seeded_inspection_templates_carry_the_rodent_sections(self):
		names = {
			spec["template_name"]: [section["section_name"] for section in spec["sections"]]
			for spec in sessions.SEED_TEMPLATES
		}
		self.assertIn("Rodent bait cleared", names["Pre-season Cabin Opening"])
		self.assertIn("Rodent activity", names["Mid-season Habitability"])

	def test_the_patch_adds_the_sections_to_a_system_template_once(self):
		spec = dict(
			next(s for s in sessions.SEED_TEMPLATES if s["template_name"] == "Mid-season Habitability")
		)
		spec["sections"] = tuple(s for s in spec["sections"] if s["section_name"] != "Rodent activity")
		sessions.build_template({**spec, "authored_by": "System"}).insert(ignore_permissions=True)
		report = sections_patch.add_rodent_sections()
		self.assertEqual(len(report["updated"]), 1)
		self.assertEqual(sections_patch.add_rodent_sections()["updated"], [])

	def test_the_calendar_and_packet_file_bait_under_housing(self):
		unit = self.a_cabin()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-P",
					"task_name": "p",
					"task_type": "Other",
					"template": rodent_bait.EXTERIOR,
					"state": "Completed",
					"completed_at": "2026-06-01 09:00:00",
					"location_doctype": "Housing Unit",
					"location": unit,
					"company": MAIN,
				}
			],
		)
		block = rodent_bait.camp_maintenance(MAIN, "2026-06-03", [])
		self.assertEqual(block["next_bait_checks"][0]["due_date"], "2026-06-08")
		rows = rodent_bait.audit_rows(MAIN, "2026-01-01", "2026-12-31")
		self.assertEqual(rows[0]["tasks"][0]["task"], "FT-P")

	def test_label_facts_are_read_from_the_ocr(self):
		facts = document_intel.bait_label_facts(
			"Tamper-resistant bait stations must be used where children or pets may be exposed. "
			"Use in and within 100 feet of man-made structures. Do not place place pacs in burrows. "
			"Maintain fresh bait for at least 1 week. For indoor and outdoor use."
		)
		self.assertEqual(
			facts,
			{
				"tamper_resistant_station_required": 1,
				"max_distance_from_structure_ft": 100.0,
				"burrow_baiting_allowed": "No",
				"min_bait_days": 7,
				"interior_use_allowed": "Yes",
			},
		)
		self.assertEqual(document_intel.bait_label_facts("Outdoor use only."), {"interior_use_allowed": "No"})

	def test_the_bait_units_seed_still_imports(self):
		self.assertTrue(callable(agronomy_seed.seed_agricultural_masters))
		self.assertTrue(callable(mobile_api.set_building_occupancy))


class AnOlderPhoneStillGetsJudged(BaitTestCase):
	def test_clearance_is_judged_when_the_section_was_not_sent(self):
		unit = self.a_cabin()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-I",
					"task_name": "i",
					"task_type": "Other",
					"template": rodent_bait.INTERIOR,
					"state": "In-Progress",
					"location_doctype": "Housing Unit",
					"location": unit,
				}
			],
		)
		template = {
			"Habitability walk": {"produces_record_doctype": "Housing Inspection", "evidence_contract": {}},
			"Rodent bait cleared": {
				"produces_record_doctype": "Housing Inspection",
				"evidence_contract": {"checklist_items": ["rodent_bait_cleared"]},
			},
		}
		submitted = {"Habitability walk": {"checklist_items": {}, "notes": ""}}
		found = rodent_bait.review_clearance(
			{"location_doctype": "Housing Unit", "location": unit}, submitted, template
		)
		self.assertEqual(found[0]["section"], "Habitability walk")
		self.assertIn("NOT cleared", submitted["Habitability walk"]["notes"])


class TheBaitQuantityIsWhatWasSaid(BaitTestCase):
	"""v0.230.1. "2 blocks" in the quantity box crashed task completion (500), and a
	unit the site did not know was replaced by the Item's stock unit ("2 Pound")."""

	def build(self, quantity):
		import types

		from erpnext_mcp import pest_control

		if not STORE.get_raw("Farm Task", "FT-BAIT-1"):
			STORE.seed("Farm Task", [{"name": "FT-BAIT-1", "task_name": "Bait", "task_type": "Inspection", "company": MAIN}])
		task = {"name": "FT-BAIT-1", "company": MAIN, "bait_product": "BAIT-1"}
		done = types.SimpleNamespace(assigned_to="EMP-001", assigned_to_name="Ana", findings_text="")
		name = pest_control.build_application(task, done, {"quantity": quantity})
		return STORE.get_raw("Pest Control Application", name)

	def test_text_in_the_number_box_is_kept_for_a_person_not_a_crash(self):
		row = self.build({"value": "2 blocks", "uom": ""})
		self.assertIsNone(row.get("quantity"))
		self.assertIn("'2 blocks', which is not a number", row.get("notes") or "")

	def test_an_unknown_unit_is_left_blank_not_replaced(self):
		row = self.build({"value": 2, "uom": "zorbles"})
		self.assertEqual(row.get("quantity"), 2.0)
		self.assertFalse(row.get("uom"))
		self.assertIn("'zorbles'", row.get("notes") or "")

	def test_no_unit_named_still_falls_back_to_the_stock_unit(self):
		row = self.build({"value": 3})
		self.assertEqual(row.get("uom"), "Nos")
