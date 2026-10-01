# SPDX-License-Identifier: MIT
"""Phone config and the compliance loop (v0.207.0).

docs/design/phone_config_and_compliance_loop.md: versioned immutable wizards,
tiles and label profiles; staged rollout; the loop audit, preview and inbox;
label-driven compliance; and the earlier phone items.
"""

import copy
import json
import unittest

import frappe

from erpnext_mcp import (
	compliance_rules,
	flags,
	label_compliance,
	phone_config,
	tiles,
	triage,
	wizard_config,
)
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.patches import reclassify_feature_requests

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase

ON = {
	f"allow_{name}": 1
	for name in (
		"stage_phone_config",
		"publish_phone_config",
		"rollback_phone_config",
		"retire_phone_config",
		"create_wizard_definition",
		"update_wizard_definition",
		"create_tile",
		"update_tile",
		"update_label_profile",
		"approve_label_compliance",
		"reject_label_compliance",
		"create_farm_task_template",
		"update_farm_task_template",
		"approve_compliance_rule",
		"update_compliance_rule",
		"set_feature_flag",
		"import_program",
		"create_mobile_user",
		"create_parcel",
		"create_housing_unit",
	)
}

T = lambda en, es="": {"en": en, "es": es}  # noqa: E731

NEAR_MISS = {
	"title": T("Report a near miss", "Reportar un casi accidente"),
	"description": T("Tell us what almost happened.", "Díganos qué casi pasó."),
	"category": "Safety",
	"icon": "exclamationmark.shield",
	"required_roles": [],
	"steps": [
		{
			"key": "what",
			"title": T("What happened", "Qué pasó"),
			"form": [
				{
					"key": "incident_description",
					"type": "long_text",
					"label": T("What happened", "Qué pasó"),
					"required": True,
				},
				{"key": "occurred_at", "type": "datetime", "label": T("When", "Cuándo"), "required": True},
				{"key": "injury", "type": "check", "label": T("Was anyone hurt?", "¿Alguien se lastimó?")},
			],
			"next": [{"if": {"field": "injury", "equals": True}, "go": "injury"}, {"go": "where"}],
		},
		{
			"key": "injury",
			"title": T("The injury", "La lesión"),
			"form": [
				{
					"key": "body_part",
					"type": "text",
					"label": T("Where on the body", "Dónde en el cuerpo"),
					"required": True,
					"safety_critical": True,
				}
			],
			"next": [{"go": "where"}],
		},
		{
			"key": "where",
			"title": T("Where", "Dónde"),
			"form": [
				{"key": "location_description", "type": "text", "label": T("Where was it", "Dónde fue")}
			],
			"next": [],
		},
	],
	"submit": {"handler": "create_accident_report", "map": {}, "context": {"severity": "Near Miss"}},
	"success": T("Thanks — filed.", "Gracias — enviado."),
}

INBOX_TILE = copy.deepcopy(tiles.SEEDS["compliance_inbox"])


class PhoneConfigCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		# The write limiter is a module-global on the wall clock; each test starts
		# with a clean minute, or the suite's own speed decides which test fails.
		from erpnext_mcp.tools import phone_configs as phone_config_tools

		phone_config_tools._recent.clear()
		self.configure(enabled=1, **ON)
		self.be("Administrator")
		STORE.commit()

	def draft(self, tool, key, body, **extra):
		return self.tool_data(tool, {"key": key, "body": body, "notes": "test", **extra})

	def publish(self, kind, key, version):
		return self.tool_data(
			"publish_phone_config", {"kind": kind, "key": key, "version": str(version), "change_note": "test"}
		)


# ── §1 lifecycle ────────────────────────────────────────────────────────────
class TheLifecycle(PhoneConfigCase):
	def test_draft_stage_publish_rollback_retire(self):
		one = self.draft("create_wizard_definition", "near_miss", NEAR_MISS)
		self.assertEqual((one["name"], one["status"]), ("wizard:near_miss@1", "Draft"))
		self.assertIn(
			"already exists",
			self.tool_error(
				"create_wizard_definition", {"key": "near_miss", "body": NEAR_MISS, "notes": "x"}
			),
		)
		self.publish("Wizard", "near_miss", 1)
		changed = copy.deepcopy(NEAR_MISS)
		changed["success"] = T("Filed. Thank you.", "Enviado. Gracias.")
		two = self.draft("update_wizard_definition", "near_miss", changed)
		self.assertEqual(two["name"], "wizard:near_miss@2")
		# Staged to one user: only they are served it.
		self.tool_data(
			"stage_phone_config",
			{
				"kind": "Wizard",
				"key": "near_miss",
				"version": "2",
				"users": [WORKER],
				"change_note": "Tim first",
			},
		)
		self.assertEqual(phone_config.in_force("Wizard", "near_miss", WORKER)[0].version, 2)
		self.assertEqual(phone_config.in_force("Wizard", "near_miss", "someone@else.test")[0].version, 1)
		self.publish("Wizard", "near_miss", 2)
		self.assertEqual(STORE.get_raw(phone_config.DOCTYPE, "wizard:near_miss@1")["status"], "Superseded")
		self.assertFalse(flags.enabled(phone_config.rollout_flag_key("Wizard", "near_miss"), user=WORKER))
		back = self.tool_data(
			"rollback_phone_config", {"kind": "Wizard", "key": "near_miss", "change_note": "bad copy"}
		)
		self.assertEqual(
			(back["name"], back["rolled_back_from"]), ("wizard:near_miss@1", "wizard:near_miss@2")
		)
		self.tool_data(
			"retire_phone_config", {"kind": "Wizard", "key": "near_miss", "change_note": "withdrawn"}
		)
		self.assertIsNone(phone_config.in_force("Wizard", "near_miss", WORKER)[0])
		self.publish("Wizard", "near_miss", 2)
		self.assertEqual(phone_config.in_force("Wizard", "near_miss", WORKER)[0].version, 2)

	def test_published_bodies_are_immutable(self):
		self.draft("create_wizard_definition", "near_miss", NEAR_MISS)
		self.publish("Wizard", "near_miss", 1)
		doc = frappe.get_doc(phone_config.DOCTYPE, "wizard:near_miss@1")
		doc.body_json = json.dumps({**NEAR_MISS, "category": "Other"})
		with self.assertRaises(Exception) as caught:
			doc.save(ignore_permissions=True)
		self.assertIn("immutable", str(caught.exception))
		doc = frappe.get_doc(phone_config.DOCTYPE, "wizard:near_miss@1")
		doc.status = "Retired"
		with self.assertRaises(Exception):
			doc.save(ignore_permissions=True)

	def test_publish_needs_spanish_and_a_change_note(self):
		english = copy.deepcopy(NEAR_MISS)
		english["title"] = {"en": "Report a near miss"}
		self.draft("create_wizard_definition", "near_miss", english)
		message = self.tool_error(
			"publish_phone_config", {"kind": "Wizard", "key": "near_miss", "version": "1", "change_note": "x"}
		)
		self.assertIn("no Spanish", message)
		self.assertIn(
			"change_note",
			self.tool_error("publish_phone_config", {"kind": "Wizard", "key": "near_miss", "version": "1"}),
		)

	def test_writes_need_a_manager(self):
		set_roles("Administrator", ["Accounts User"])
		self.assertIn(
			"Farm Manager",
			self.tool_error("create_tile", {"key": "x_tile", "body": INBOX_TILE, "notes": "x"}),
		)

	def test_size_limit(self):
		body = copy.deepcopy(NEAR_MISS)
		body["description"] = T("x" * 70000, "y")
		self.assertIn(
			"64 KB", self.tool_error("create_wizard_definition", {"key": "big", "body": body, "notes": "x"})
		)


class FlagsTargetUsers(PhoneConfigCase):
	def test_a_user_row_is_most_specific(self):
		flags.upsert("label_capture_v3", "Flag", False, company=MAIN)
		flags.upsert("label_capture_v3", "Flag", True, users=[WORKER])
		self.assertTrue(flags.enabled("label_capture_v3", MAIN, user=WORKER))
		self.assertFalse(flags.enabled("label_capture_v3", MAIN, user="other@x.test"))


# ── §2 wizards ──────────────────────────────────────────────────────────────
class WizardsAreChecked(unittest.TestCase):
	def errors(self, body):
		return "\n".join(wizard_config.validate(body)["errors"])

	def test_graph_keys_and_handler(self):
		body = copy.deepcopy(NEAR_MISS)
		body["steps"][2]["next"] = [{"go": "what"}]
		self.assertIn("loop back", self.errors(body))
		body = copy.deepcopy(NEAR_MISS)
		body["steps"][0]["next"] = [{"go": "where"}]
		self.assertIn("no path reaches: injury", self.errors(body))
		body = copy.deepcopy(NEAR_MISS)
		body["steps"][2]["form"][0]["key"] = "occurred_at"
		self.assertIn("repeat across steps", self.errors(body))
		body = copy.deepcopy(NEAR_MISS)
		body["submit"] = {"handler": "run_python", "map": {}}
		self.assertIn("submit.handler must be one of", self.errors(body))
		body = copy.deepcopy(NEAR_MISS)
		body["steps"][0]["form"][0]["key"] = "what_happened"
		self.assertIn("needs incident_description", self.errors(body))

	def test_branching_path(self):
		self.assertEqual(wizard_config.path(NEAR_MISS, {"injury": True}), ["what", "injury", "where"])
		self.assertEqual(wizard_config.path(NEAR_MISS, {"injury": False}), ["what", "where"])


class WizardsOnThePhone(PhoneConfigCase):
	def setUp(self):
		super().setUp()
		self.draft("create_wizard_definition", "near_miss", NEAR_MISS)
		self.publish("Wizard", "near_miss", 1)
		STORE.commit()

	def test_the_spec_carries_both_shapes_and_the_version(self):
		self.be()
		spec = mobile_api.get_wizard_definition(wizard="near_miss")
		self.assertEqual(spec["config_version"], "wizard:near_miss@1")
		self.assertEqual(spec["steps"][0]["form"][0]["key"], "incident_description")
		self.assertEqual(spec["steps"][0]["fields"][0]["type"], "text")
		self.assertEqual(spec["steps"][0]["next"][0]["go"], "injury")
		self.assertEqual(spec["submit_endpoint"], "farmops/api/mobile/submit_wizard_via_mobile")
		listed = mobile_api.list_wizard_definitions()
		self.assertIn("wizard:near_miss@1", [w.get("config_version") for w in listed["wizards"]])

	def test_submit_checks_the_path_and_is_idempotent(self):
		self.be()
		answers = {
			"incident_description": "Ladder slipped",
			"occurred_at": "2026-07-23 08:00:00",
			"injury": True,
		}
		with self.assertRaises(Exception) as caught:
			mobile_api.submit_wizard_via_mobile(
				wizard="near_miss", answers=answers, config_version="wizard:near_miss@1"
			)
		self.assertIn("Where on the body", str(caught.exception))
		answers["body_part"] = "hand"
		first = mobile_api.submit_wizard_via_mobile(
			wizard="near_miss",
			answers=answers,
			config_version="wizard:near_miss@1",
			client_reference="11111111-aaaa",
		)
		self.assertTrue(first["filed"])
		self.assertEqual(first["path"], ["what", "injury", "where"])
		self.assertEqual(first["submit_method"], "create_accident_report")
		again = mobile_api.submit_wizard_via_mobile(
			wizard="near_miss",
			answers=answers,
			config_version="wizard:near_miss@1",
			client_reference="11111111-aaaa",
		)
		self.assertTrue(again["duplicate"])

	def test_a_retired_wizard_is_refused(self):
		self.be("Administrator")
		self.tool_data("retire_phone_config", {"kind": "Wizard", "key": "near_miss", "change_note": "gone"})
		STORE.commit()
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.submit_wizard_via_mobile(wizard="near_miss", answers={"incident_description": "x"})
		self.assertIn("withdrawn", str(caught.exception))

	def test_preview_traces_the_path_and_the_handler_call(self):
		data = self.tool_data(
			"preview_wizard",
			{
				"key": "near_miss",
				"answers": {"incident_description": "x", "occurred_at": "2026-09-28", "injury": False},
			},
		)
		self.assertEqual(data["path"], ["what", "where"])
		self.assertEqual(data["handler_would_receive"]["severity"], "Near Miss")
		self.assertEqual(data["ignored_answers"], ["injury"])
		self.assertEqual(data["steps"][0]["form"]["es"][0]["label"], "Qué pasó")

	def test_the_legacy_wizards_convert(self):
		from erpnext_mcp.tools import wizards as wizard_tools

		wizard_tools.install_wizard_definitions(overwrite=False)
		made = wizard_config.seed_from_legacy()
		self.assertTrue(any(name.startswith("wizard:accident_investigation@") for name in made))
		self.assertEqual(wizard_config.seed_from_legacy(), [])

	def test_report_an_accident_gains_a_map_pin_as_a_published_version(self):
		"""v0.211.0, quick_wins_2026_10.md §1: a config change, not a code one."""
		from erpnext_mcp.tools import wizards as wizard_tools

		wizard_tools.install_wizard_definitions(overwrite=False)
		wizard_config.seed_from_legacy()
		name = wizard_config.add_accident_location()
		self.assertEqual(name, "wizard:accident_investigation@2")
		two = phone_config.doc_of("Wizard", "accident_investigation", 2)
		self.assertEqual(two.status, "Published")
		self.assertEqual(phone_config.doc_of("Wizard", "accident_investigation", 1).status, "Superseded")
		body = phone_config.body_of(two)
		where = next(step for step in body["steps"] if step["key"] == "where")
		pin = where["form"][-1]
		self.assertEqual((pin["key"], pin["type"], pin["required"]), ("location_point", "geolocation", False))
		self.assertTrue(pin["label"]["es"])
		# The answer reaches the route as an argument, with no submit map needed.
		arguments, ignored = wizard_config.handler_arguments(
			body, {"location_point": {"type": "Point", "coordinates": [-121.18, 45.6]}}, {}
		)
		self.assertEqual(arguments["location_point"]["coordinates"], [-121.18, 45.6])
		self.assertEqual(ignored, [])
		# Once, and never over a version somebody else wrote.
		self.assertEqual(wizard_config.add_accident_location(), "")
		self.assertEqual(len(phone_config.rows("Wizard", "accident_investigation")), 2)


# ── §3 tiles ────────────────────────────────────────────────────────────────
class TilesAreChecked(PhoneConfigCase):
	def test_every_part_of_a_tile_is_checked(self):
		bad = copy.deepcopy(INBOX_TILE)
		bad.update(
			{
				"icon": "flame.circle.rocket",
				"target": {"kind": "report", "report": "payroll"},
				"audience": {"roles": ["Astronaut"]},
				"badge": {"query": "triage_queue", "params": {}},
				"show_if": {"occupancy": "Occupied"},
			}
		)
		found = "\n".join(phone_config.validate("Tile", "bad", bad)["errors"])
		for words in (
			"allowlist",
			"report must be one of",
			"resolves to nobody",
			"triage_queue is for",
			"asset_scan surface only",
		):
			self.assertIn(words, found)

	def test_the_worker_gets_the_inbox_tile_with_a_badge(self):
		tiles.seed()
		STORE.commit()
		self.be()
		answer = mobile_api.get_tiles(surface="today", app_version="0.21.0")
		keys = [t["key"] for t in answer["tiles"]]
		self.assertIn("compliance_inbox", keys)
		inbox = next(t for t in answer["tiles"] if t["key"] == "compliance_inbox")
		self.assertEqual(inbox["badge"], {"count": 0, "tone": "neutral"})
		self.assertEqual(mobile_api.get_tiles(surface="today", app_version="0.20.1")["tiles"], [])

	def test_preview_explains_hidden_tiles(self):
		body = copy.deepcopy(INBOX_TILE)
		body["show_if"] = {"flag": "inbox_beta"}
		self.draft("create_tile", "inbox_beta", body)
		self.publish("Tile", "inbox_beta", 1)
		data = self.tool_data(
			"preview_tiles", {"surface": "today", "as_user": WORKER, "app_version": "0.21.0"}
		)
		self.assertIn(
			{"key": "inbox_beta", "config_version": "tile:inbox_beta@1", "hidden": "flag inbox_beta is off"},
			data["hidden"],
		)


# ── §4 the loop ─────────────────────────────────────────────────────────────
def build_loop_site(case):
	"""A rule wired to a phone-renderable template and one overdue alert for Ana."""
	compliance_rules.seed_compliance_rules()
	case.rule = compliance_rules.resolve("housing_detector_test_stale")
	case.tool_data(
		"create_farm_task_template",
		{
			"template_name": "Detector Test",
			"task_type": "Inspection",
			"evidence_required": {"photos": True},
			"form_schema": [
				{"key": "tested", "type": "check", "label": T("Tested", "Probado"), "required": True}
			],
		},
	)
	frappe.db.set_value("Compliance Rule", case.rule, "producer_task_template", "Detector Test")
	STORE.seed(
		"Compliance Alert",
		[
			{
				"name": "ALERT-1",
				"alert_key": "ALERT-1",
				"alert_type": "housing_detector_test_stale",
				"severity": "Warning",
				"company": MAIN,
				"subject_employee": WORKER_EMPLOYEE,
				"alert_message": "Detector test is stale at MC-Cabin-01",
				"due_date": "2020-01-01",
				"dismissed": 0,
			}
		],
	)
	tiles.seed()
	STORE.commit()


class TheComplianceLoop(PhoneConfigCase):
	def setUp(self):
		super().setUp()
		build_loop_site(self)

	def test_audit_names_the_path_and_the_gaps(self):
		data = self.tool_data("audit_compliance_loop", {"rule": "housing_detector_test_stale"})
		row = data["rules"][0]
		self.assertEqual(row["path"]["kind"], "task_template")
		self.assertTrue(row["renderable"])
		self.assertIn("Work: My tasks / Available", row["entry_points"])
		self.assertEqual(row["dismissal"]["mode"], "auto")
		frappe.db.set_value("Farm Task Template", "Detector Test", "enabled", 0)
		gaps = self.tool_data("audit_compliance_loop", {"rule": "housing_detector_test_stale"})["rules"][0][
			"gaps"
		]
		self.assertIn("Farm Task Template 'Detector Test' is disabled", gaps)

	def test_enabling_a_rule_with_a_gap_needs_a_reason(self):
		frappe.db.set_value("Farm Task Template", "Detector Test", "enabled", 0)
		frappe.db.set_value("Compliance Rule", self.rule, "enabled", 0)
		STORE.commit()
		self.assertIn("accept_loop_gap", self.tool_error("approve_compliance_rule", {"name": self.rule}))
		self.tool_data(
			"approve_compliance_rule", {"name": self.rule, "accept_loop_gap": "template being rewritten"}
		)
		self.assertIn(
			"template being rewritten", STORE.get_raw("Compliance Rule", self.rule)["loop_gap_accepted"]
		)

	def test_the_inbox_and_starting_the_work(self):
		self.be()
		box = mobile_api.get_compliance_inbox()
		self.assertEqual(box["counts"]["overdue"], 1)
		item = box["overdue"][0]
		self.assertEqual(
			item["action"],
			{"kind": "task_template", "ref": "Detector Test", "context": {"source_alert": "ALERT-1"}},
		)
		started = mobile_api.start_template_task(
			template="Detector Test", source_alert="ALERT-1", client_reference="ref-1"
		)
		task = started["task"]["name"]
		self.assertEqual(STORE.get_raw("Farm Task", task)["source_alert"], "ALERT-1")
		self.assertEqual(STORE.get_raw("Farm Task", task)["template_version"], 1)
		again = mobile_api.start_template_task(
			template="Detector Test", source_alert="ALERT-1", client_reference="ref-1"
		)
		self.assertTrue(again["duplicate"])
		box = mobile_api.get_compliance_inbox()
		self.assertEqual(box["overdue"][0]["action"]["kind"], "task")

	def test_a_missing_certification_blocks_the_item(self):
		frappe.db.set_value("Farm Task Template", "Detector Test", "required_certification", "Electrician")
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-X",
					"task_name": "Detector",
					"task_type": "Inspection",
					"state": "Available",
					"company": MAIN,
					"source_alert": "ALERT-1",
					"required_certification": "Electrician",
					"dispatch_mode": "Open",
				}
			],
		)
		STORE.commit()
		self.be()
		box = mobile_api.get_compliance_inbox()
		self.assertEqual(box["counts"]["blocked"], 1)
		self.assertIn("Electrician", box["blocked"][0]["blocked_reason"]["en"])

	def test_preview_is_end_to_end_and_writes_nothing(self):
		before = len(STORE.rows("Farm Task"))
		data = self.tool_data(
			"preview_compliance_loop", {"rule": "housing_detector_test_stale", "as_user": WORKER}
		)
		stages = [s["stage"] for s in data["stages"]]
		self.assertEqual(stages, ["alert", "work", "phone", "who", "completion", "dismissal"])
		self.assertEqual(data["stages"][0]["alert"]["name"], "ALERT-1")
		self.assertEqual(data["stages"][2]["form"]["es"][0]["label"], "Probado")
		self.assertTrue(data["stages"][3]["viewer"]["in_inbox"])
		self.assertEqual(len(STORE.rows("Farm Task")), before)

	def test_the_template_version_bumps_when_the_form_changes(self):
		self.tool_data(
			"update_farm_task_template",
			{
				"template": "Detector Test",
				"form_schema": [
					{"key": "tested", "type": "check", "label": T("Tested twice", "Probado dos veces")}
				],
			},
		)
		self.assertEqual(STORE.get_raw("Farm Task Template", "Detector Test")["version"], 2)


# ── §5 labels ──────────────────────────────────────────────────────────────
class LabelDrivenCompliance(PhoneConfigCase):
	def setUp(self):
		super().setUp()
		from erpnext_mcp import compliance_fields

		compliance_fields.install_compliance_fields(respect_switch=False)
		label_compliance.seed()
		STORE.seed(
			"Item",
			[
				{
					"name": "RESTRICTO",
					"item_code": "RESTRICTO",
					"item_name": "Restricto",
					"restricted_use": 1,
					"label_scan_validation": "DVAL-1",
				},
				{"name": "PLAIN", "item_code": "PLAIN", "item_name": "Plain", "restricted_use": 0},
				{
					"name": "BAIT",
					"item_code": "BAIT",
					"item_name": "Bait",
					"pesticide_use_scope": "Non-crop",
					"tamper_resistant_station_required": 1,
				},
			],
		)
		STORE.commit()

	def test_active_when_everything_is_live(self):
		result = label_compliance.attach("RESTRICTO")
		self.assertEqual(result["state"], "Active")
		self.assertEqual(label_compliance.requirements("RESTRICTO")["certifications"], ["Applicator License"])
		self.assertEqual(label_compliance.evaluate("PLAIN")["matched"], [])

	def test_a_new_program_is_a_proposal(self):
		result = label_compliance.attach("BAIT")
		self.assertEqual(result["state"], "Proposed")
		self.assertEqual(result["proposed_calls"][0]["tool"], "import_program")
		listed = self.tool_data("list_label_compliance", {"item": "BAIT"})["items"][0]
		self.assertEqual(listed["state"], "Proposed")
		STORE.commit()
		self.tool_data("reject_label_compliance", {"item": "BAIT", "reason": "not ours"})
		self.assertEqual(STORE.get_raw("Item", "BAIT")["compliance_state"], "Rejected")

	def test_the_requirement_gates_the_work(self):
		from erpnext_mcp import qualifications

		label_compliance.attach("RESTRICTO")
		with self.assertRaises(Exception) as caught:
			qualifications.refuse_unqualified(
				{"bait_product": "RESTRICTO", "task_name": "Spray"}, WORKER_EMPLOYEE, "claimed"
			)
		self.assertIn("Applicator License", str(caught.exception))

	def test_preview_lists_matches(self):
		data = self.tool_data("preview_label_profile", {"key": "restricted_use_pesticide"})
		self.assertEqual([m["item"] for m in data["matching_items"]], ["RESTRICTO"])


# ── §6 the earlier phone items ──────────────────────────────────────────────
class TheEarlierPhoneItems(PhoneConfigCase):
	def test_the_classifier_reads_desire_as_a_request(self):
		cases = {
			"Can the app show my hours?": "Feature request",
			"Is there a way to sort by block?": "Feature request",
			"¿Se puede ver el mapa?": "Feature request",
			"Show the PPE on the task screen": "Feature request",
			"How do I see my hours?": "Question",
			"¿Cómo veo mis horas?": "Question",
			"The app crashes on the label": "Code bug",
			"The rate on the label is wrong": "Data or config",
		}
		for text, wanted in cases.items():
			doc = frappe._dict(name="AFB-X", feedback_text=text, screen_name="s")
			self.assertEqual(triage.classify(doc)["triage_class"], wanted, text)

	def test_the_reclassify_patch_only_touches_auto_questions(self):
		STORE.seed(
			"App Feedback",
			[
				{
					"name": "AFB-1",
					"entry_uuid": "u1",
					"feedback_text": "Can the app show my hours?",
					"triage_state": "Auto-classified",
					"triage_class": "Question",
					"screen_name": "a",
				},
				{
					"name": "AFB-2",
					"entry_uuid": "u2",
					"feedback_text": "Can the app show my hours?",
					"triage_state": "Proposed",
					"triage_class": "Question",
					"screen_name": "b",
				},
			],
		)
		self.assertEqual(reclassify_feature_requests.run(), 1)
		self.assertEqual(STORE.get_raw("App Feedback", "AFB-1")["triage_class"], "Feature request")
		self.assertEqual(STORE.get_raw("App Feedback", "AFB-2")["triage_class"], "Question")


class InspectionsFromTheScan(PhoneConfigCase):
	def setUp(self):
		super().setUp()
		from erpnext_mcp import compliance_fields, sessions

		compliance_fields.install_compliance_fields(respect_switch=False)
		sessions.build_template(
			{
				"template_name": "Quick Walk",
				"description": "A quick walk.",
				"applies_to_asset_type": "Housing Unit",
				"authored_by": "Operator",
				"sections": [
					{
						"section_name": "Walk",
						"produces_record_doctype": "",
						"renderer_hint": "checklist",
						"field_prompts": [{"key": "clean", "type": "check", "label": T("Clean", "Limpio")}],
					}
				],
			}
		).insert(ignore_permissions=True)
		self.unit = self.a_camp()
		STORE.commit()

	def test_startable_then_submit_twice(self):
		self.be()
		listed = mobile_api.list_startable_inspections(location_doctype="Housing Unit", location=self.unit)
		self.assertIn("Quick Walk", [t["title"]["en"] for t in listed["templates"]])
		started = mobile_api.start_inspection(
			template="Quick Walk", location=self.unit, location_doctype="Housing Unit"
		)
		session = started.get("name") or started.get("session")
		body = [{"section_name": "Walk", "answers": {"clean": True}}]
		first = mobile_api.submit_inspection(
			session=session, section_submissions=body, client_reference="ref-9"
		)
		self.assertNotIn("duplicate", first)
		again = mobile_api.submit_inspection(
			session=session, section_submissions=body, client_reference="ref-9"
		)
		self.assertTrue(again["duplicate"])
		row = next(r for r in mobile_api.list_my_inspections()["sessions"] if r["name"] == session)
		self.assertIn("farm_task", row)
