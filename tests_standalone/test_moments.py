# SPDX-License-Identifier: MIT
"""From hours to moments: extraction config, feature flags, feedback triage.

docs/design/config_flags_triage.md (v0.206.0).
"""

import copy
import json
import unittest

import frappe

from erpnext_mcp import document_intel, extraction_config, flags, triage
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.patches import triage_existing_feedback
from erpnext_mcp.tools import app_feedback

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase

LABEL, _ = extraction_config.builtin("Pesticide Label")

PROWLER = """PROWLER
EPA Reg. No. 12455-97-3240
CAUTION
ACTIVE INGREDIENT: Bromethalin 0.01%
DIRECTIONS FOR USE
Norway rats: 1 place pac per placement
Launder clothing separately
STORAGE AND DISPOSAL
Store in a cool, dry place
16 x 1.5 oz place pacs"""


# ── §1.2 the body ───────────────────────────────────────────────────────────
class TheBodyIsChecked(unittest.TestCase):
	def test_every_builtin_passes_its_own_checks(self):
		for document_type in extraction_config.BUILTINS:
			body, version = extraction_config.builtin(document_type)
			self.assertEqual(extraction_config.problems(body), [], document_type)
			self.assertEqual(version, f"{document_type}@builtin-1")

	def test_the_portable_subset(self):
		for refused in ("(?<=a)b", "(?<!a)b", "(?P<x>a)", "(?<x>a)", "(a)\\1", "a(?i)b", "(?s)a"):
			self.assertTrue(extraction_config.regex_problems(refused), refused)
		for allowed in ("(?i)EPA\\s*Reg", "(?:a|b)+\\d{1,3}", "^[0-9]+-[0-9]+$", "\\\\1"):
			self.assertEqual(extraction_config.regex_problems(allowed), [], allowed)
		self.assertTrue(extraction_config.regex_problems("(unclosed"))

	def test_a_bad_body_says_every_reason(self):
		body = copy.deepcopy(LABEL)
		body["schema_version"] = 2
		body["fields"].append(dict(body["fields"][0]))
		body["fields"][0]["section"] = "nowhere"
		body["rules"].append({"code": "x", "field": "y", "kind": "vibes"})
		body["extractors"].append({"field": "z", "pattern": "(?<=a)b"})
		found = "\n".join(extraction_config.problems(body))
		for words in ("schema_version", "not unique", "'nowhere'", "'vibes'", "look-behind"):
			self.assertIn(words, found)


class SectionsExtractorsRules(unittest.TestCase):
	def test_heading_and_pattern_sections(self):
		carved = extraction_config.carve(PROWLER, LABEL["sections"])
		self.assertIn("Norway rats: 1 place pac per placement", carved["rate"])
		self.assertNotIn("Launder", carved["rate"])
		self.assertNotIn("Store in", carved["rate"])
		self.assertEqual(carved["package"], "16 x 1.5 oz place pacs")

	def test_extractors_first_match_and_section(self):
		values = extraction_config.extract(PROWLER, LABEL)
		self.assertEqual(values["epa_registration_number"], "12455-97-3240")
		self.assertEqual(values["signal_word"], "CAUTION")
		self.assertEqual(values["unit_hint"], "place pacs")

	def test_each_rule_kind(self):
		body = {
			"rules": [
				{"code": "p", "field": "a", "kind": "pattern", "pattern": "^\\d+$"},
				{"code": "r", "field": "b", "kind": "required"},
				{"code": "smax", "field": "c.n", "kind": "sum_max", "max": 100},
				{"code": "smin", "field": "c.n", "kind": "sum_min", "min": 1},
				{"code": "rg", "field": "d", "kind": "range", "min": 1, "max": 5},
				{"code": "o", "field": "e", "kind": "one_of", "values": ["X", "Y"]},
				{"code": "w", "field": "f", "kind": "required", "when": {"field": "g", "equals": "on"}},
			]
		}
		values = {"a": "12x", "b": "", "c": [{"n": 60}, {"n": 50}], "d": 9, "e": "y", "g": "off"}
		ok = {
			r["code"]: (r["ok"], r["skipped"])
			for r in extraction_config.evaluate_rules(body, values)["results"]
		}
		self.assertEqual(
			ok,
			{
				"p": (False, False),
				"r": (False, False),
				"smax": (False, False),
				"smin": (True, False),
				"rg": (False, False),
				"o": (True, False),
				"w": (True, True),
			},
		)

	def test_not_applicable_skips_the_rules_on_those_fields(self):
		result = extraction_config.evaluate_rules(
			LABEL, {"pesticide_use_scope": "Non-crop", "rei_hours": 9000}
		)
		rei = next(r for r in result["results"] if r["code"] == "rei_range")
		self.assertTrue(rei["skipped"])
		self.assertIn("rei_hours", result["not_applicable"])


class TheBuiltinReproducesV0202(unittest.TestCase):
	"""§8.3 of v0.202.0, now data: the same three drops, nothing more."""

	def drop(self, entry, deterministic):
		return bool(extraction_config.drop_reason(entry, LABEL, deterministic))

	def test_an_epa_finding_the_rules_did_not_raise_is_dropped(self):
		entry = {"field": "epa_registration_number", "message": "lacks hyphens"}
		self.assertTrue(self.drop(entry, {"issues": []}))
		raised = {
			"issues": [{"field": "epa_registration_number", "severity": "error", "code": "epa_not_in_ocr"}]
		}
		self.assertFalse(self.drop(entry, raised))

	def test_crop_findings_on_a_non_crop_label(self):
		scope = {"pesticide_use_scope": "Non-crop", "issues": []}
		self.assertTrue(self.drop({"field": "rei_hours", "message": "x"}, scope))
		self.assertTrue(self.drop({"field": "", "message": "A mouse bait needs an REI"}, scope))
		self.assertFalse(self.drop({"field": "rei_hours", "message": "x"}, {"pesticide_use_scope": "Crop"}))

	def test_a_not_applicable_rule_drops_findings_on_its_fields(self):
		body = copy.deepcopy(LABEL)
		body["advisory_drop"] = []
		scope = {"pesticide_use_scope": "Non-crop", "issues": []}
		self.assertTrue(extraction_config.drop_reason({"field": "phi_days", "message": "x"}, body, scope))
		self.assertFalse(
			extraction_config.drop_reason(
				{"field": "phi_days", "message": "x"}, body, {"pesticide_use_scope": "Crop"}
			)
		)

	def test_ingredients_totalling_100_percent(self):
		self.assertTrue(
			self.drop({"field": "active_ingredients", "message": "total of 100% is implausible"}, {})
		)
		self.assertFalse(self.drop({"field": "active_ingredients", "message": "a name is misspelt"}, {}))

	def test_merge_still_keeps_the_rest_as_warnings(self):
		deterministic = document_intel.validate_extraction(
			"Pesticide Label", PROWLER, {"epa_registration_number": "12455-97-3240", "signal_word": "Caution"}
		)
		merged = document_intel.merge_llm_assessment(
			deterministic,
			{
				"status": "Flagged",
				"issues": [
					{"field": "epa_registration_number", "severity": "error", "message": "lacks hyphens"},
					{"field": "signal_word", "severity": "error", "message": "x" * 400},
				],
			},
			"apple-foundation-models",
		)
		self.assertEqual(merged["advisory_dropped"], 1)
		kept = [i for i in merged["issues"] if i.get("field") == "signal_word" and len(i["message"]) == 240]
		self.assertEqual(kept[0]["severity"], "warning")
		self.assertEqual(merged["config_version"], "Pesticide Label@builtin-1")


# ── §1.3–1.4 records, tools, the phone ──────────────────────────────────────
ON = {
	f"allow_{name}": 1
	for name in (
		"update_extraction_config",
		"publish_extraction_config",
		"set_feature_flag",
		"propose_triage_fix",
		"approve_triage_proposal",
		"reject_triage_proposal",
		"validate_document_extraction",
	)
}


class ExtractionConfigRecords(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.be("Administrator")
		extraction_config.seed()
		STORE.commit()

	def test_seed_is_create_only(self):
		self.assertEqual(
			sorted(r["name"] for r in extraction_config.rows()),
			["I-9 Document@1", "Pesticide Label@1", "Receipt@1"],
		)
		self.assertEqual(extraction_config.seed(), [])
		self.assertEqual(extraction_config.active("Receipt")[1], "Receipt@1")

	def draft(self, **change):
		body = copy.deepcopy(LABEL)
		body.update(change)
		return self.tool_data(
			"update_extraction_config",
			{"document_type": "pesticide label", "config": body, "notes": "AFB-2026-00031"},
		)

	def test_update_drafts_and_publish_supersedes(self):
		drafted = self.draft(advisory_drop=[])
		self.assertEqual((drafted["name"], drafted["status"]), ("Pesticide Label@2", "Draft"))
		self.assertEqual(extraction_config.active("Pesticide Label")[1], "Pesticide Label@1")
		published = self.tool_data(
			"publish_extraction_config", {"document_type": "Pesticide Label", "version": "2"}
		)
		self.assertEqual(published["superseded"], "Pesticide Label@1")
		self.assertEqual(STORE.get_raw("Extraction Config", "Pesticide Label@1")["status"], "Superseded")
		self.assertIn(
			"only a Draft",
			self.tool_error(
				"publish_extraction_config", {"document_type": "Pesticide Label", "version": "2"}
			),
		)
		# The phone gets the new one, and a cheap answer when it already has it.
		self.be()
		answer = mobile_api.get_extraction_config(document_type="Pesticide Label")
		self.assertEqual(
			(answer["config_version"], answer["config"]["advisory_drop"]), ("Pesticide Label@2", [])
		)
		same = mobile_api.get_extraction_config(
			document_type="Pesticide Label", known_version="Pesticide Label@2"
		)
		self.assertTrue(same["not_modified"])
		# And the server's advisory filter follows it: nothing is dropped now.
		deterministic = document_intel.validate_extraction(
			"Pesticide Label", PROWLER, {"epa_registration_number": "12455-97-3240"}
		)
		merged = document_intel.merge_llm_assessment(
			deterministic,
			{
				"issues": [
					{"field": "epa_registration_number", "severity": "error", "message": "lacks hyphens"}
				]
			},
			"apple-foundation-models",
		)
		self.assertEqual(merged["advisory_dropped"], 0)

	def test_a_config_the_phone_would_refuse_is_not_written(self):
		message = self.tool_error(
			"update_extraction_config",
			{"document_type": "Pesticide Label", "config": {"schema_version": 1}, "notes": "x"},
		)
		self.assertIn("instructions.en", message)
		self.assertEqual(extraction_config.next_version("Pesticide Label"), 2)

	def test_writes_need_a_manager(self):
		set_roles("Administrator", ["Accounts User"])
		self.assertIn(
			"Farm Manager",
			self.tool_error("publish_extraction_config", {"document_type": "Receipt", "version": "1"}),
		)
		set_roles("Administrator", ["System Manager"])

	def test_validation_records_the_config_and_flags(self):
		data = self.tool_data(
			"validate_document_extraction",
			{
				"document_type": "Pesticide Label",
				"ocr_text": PROWLER,
				"extracted_fields": {"epa_registration_number": "12455-97-3240", "signal_word": "Caution"},
				"company": MAIN,
				"feature_flags": {"label_capture_v2": True, "Bad Key": 1},
			},
		)
		self.assertEqual(data["config_version"], "Pesticide Label@1")
		row = STORE.get_raw("Document Validation", data["validation_id"])
		self.assertEqual(row["config_version"], "Pesticide Label@1")
		self.assertEqual(json.loads(row["feature_flags"]), {"label_capture_v2": True})
		sent = self.tool_data(
			"validate_document_extraction",
			{
				"document_type": "Pesticide Label",
				"ocr_text": PROWLER,
				"extracted_fields": {"epa_registration_number": "12455-97-3240"},
				"company": MAIN,
				"config_version": "Pesticide Label@builtin-1",
			},
		)
		self.assertEqual(
			STORE.get_raw("Document Validation", sent["validation_id"])["config_version"],
			"Pesticide Label@builtin-1",
		)

	def test_preview_is_a_dry_run_against_a_stored_validation(self):
		data = self.tool_data(
			"validate_document_extraction",
			{
				"document_type": "Pesticide Label",
				"ocr_text": PROWLER,
				"extracted_fields": {"epa_registration_number": "12455-97-3240", "signal_word": "Caution"},
				"company": MAIN,
				"llm_model": "apple-foundation-models",
				"llm_assessment": {
					"status": "Flagged",
					"issues": [{"field": "active_ingredients", "severity": "error", "message": "100% total"}],
				},
			},
		)
		before = len(STORE.rows("Extraction Config"))
		preview = self.tool_data("preview_extraction_config", {"validation": data["validation_id"]})
		self.assertEqual(preview["config_version"], "Pesticide Label@1")
		self.assertEqual(preview["extracted"]["unit_hint"], "place pacs")
		self.assertEqual([d["field"] for d in preview["advisory_dropped"]], ["active_ingredients"])
		self.assertIn({"field": "signal_word", "recorded": "Caution", "config": "CAUTION"}, preview["diff"])
		self.assertEqual(len(STORE.rows("Extraction Config")), before)


# ── §2 flags ────────────────────────────────────────────────────────────────
class FlagsResolve(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.be("Administrator")

	def set(self, **args):
		return self.tool_data("set_feature_flag", args)

	def test_no_row_is_the_callers_default(self):
		self.assertIs(flags.value("label_capture_v2", MAIN, (), "1.0", default=False), False)
		self.assertEqual(flags.value("bait_check_active_days", MAIN, default=14), 14)

	def test_the_most_specific_row_wins(self):
		self.set(flag_key="bait_days", kind="Threshold", value=14)
		self.assertEqual(flags.value("bait_days", MAIN), 14.0)
		self.set(flag_key="bait_days", kind="Threshold", value=10, roles=["Farm Worker"])
		self.assertEqual(flags.value("bait_days", MAIN, ["Farm Worker"]), 10.0)
		self.set(flag_key="bait_days", kind="Threshold", value=7, company=MAIN)
		self.assertEqual(flags.value("bait_days", MAIN, ["Farm Worker"]), 7.0)
		self.set(flag_key="bait_days", kind="Threshold", value=5, company=MAIN, roles=["Farm Worker"])
		self.assertEqual(flags.value("bait_days", MAIN, ["Farm Worker"]), 5.0)
		self.assertEqual(flags.value("bait_days", "Other Co"), 14.0)

	def test_version_range_and_active(self):
		self.set(flag_key="label_capture_v2", value=True, min_app_version="1.42")
		self.assertTrue(flags.enabled("label_capture_v2", MAIN, (), "1.42.1"))
		self.assertFalse(flags.enabled("label_capture_v2", MAIN, (), "1.41"))
		self.assertFalse(flags.enabled("label_capture_v2", MAIN, (), None))
		self.set(flag_key="label_capture_v2", value=True, min_app_version="1.42", active=False)
		self.assertFalse(flags.enabled("label_capture_v2", MAIN, (), "2.0"))
		self.assertEqual(len(flags.rows("label_capture_v2")), 1, "same scope is an update, not a second row")

	def test_refusals(self):
		self.assertIn(
			"lower_snake_case", self.tool_error("set_feature_flag", {"flag_key": "Label V2", "value": True})
		)
		self.assertIn(
			"number",
			self.tool_error("set_feature_flag", {"flag_key": "x_days", "kind": "Threshold", "value": "soon"}),
		)

	def test_the_phone_reads_its_resolved_map(self):
		self.set(flag_key="label_capture_v2", value=True)
		self.set(flag_key="triage_banner", kind="Text", value="hola", company=MAIN)
		self.be()
		answer = mobile_api.get_feature_flags(app_version="1.50")
		self.assertEqual(answer["flags"], {"label_capture_v2": True, "triage_banner": "hola"})
		self.assertTrue(answer["evaluated_at"])

	def test_stamp_records_on_the_record(self):
		doc = frappe.new_doc("Document Validation")
		flags.stamp(doc, "label_capture_v2", True)
		flags.stamp_all(doc, {"bait_days": 7})
		self.assertEqual(json.loads(doc.feature_flags), {"bait_days": 7, "label_capture_v2": True})


# ── §3 triage ───────────────────────────────────────────────────────────────
class TellTheFarmTriage(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.be("Administrator")
		self.counter = 0

	def note(self, text, **extra):
		self.counter += 1
		data = app_feedback.submit_app_feedback(
			{
				"entry_uuid": f"00000000-0000-0000-0000-{self.counter:012d}",
				"comment": text,
				"screen": extra.pop("screen", "label_capture"),
				"company": MAIN,
				"caller_user": WORKER,
				**extra,
			}
		).data
		STORE.commit()
		return data["app_feedback"]["name"]

	def test_classified_on_arrival_with_evidence(self):
		name = self.note("The app crashes when I open the label")
		row = STORE.get_raw("App Feedback", name)
		self.assertEqual((row["triage_class"], row["triage_state"]), ("Code bug", "Auto-classified"))
		other = self.note(f"The rate on {name} is wrong", screen="product")
		row = STORE.get_raw("App Feedback", other)
		self.assertEqual(row["triage_class"], "Data or config")
		self.assertIn(
			{"doctype": "App Feedback", "name": name}, json.loads(row["evidence_json"])["linked_records"]
		)
		self.assertEqual(
			STORE.get_raw("App Feedback", self.note("Could we add a Spanish voice option?"))["triage_class"],
			"Feature request",
		)
		self.assertEqual(
			STORE.get_raw("App Feedback", self.note("Where do I see my hours?", screen="x"))["triage_class"],
			"Question",
		)
		again = self.note("The app crashes when I open the label")
		self.assertEqual(STORE.get_raw("App Feedback", again)["triage_class"], "Duplicate")

	def test_a_proposal_is_checked_and_never_applied(self):
		name = self.note("Place Pac should be counted in pacs")
		bad = self.tool_error(
			"propose_triage_fix",
			{
				"feedback": name,
				"triage_class": "Data or config",
				"summary": "x",
				"calls": [
					{"tool": "list_uoms", "arguments": {}, "why": "look"},
					{"tool": "no_such_tool", "arguments": {}, "why": "x"},
					{"tool": "set_uom_aliases", "arguments": {"uom": 5, "surprise": 1}, "why": "x"},
				],
			},
		)
		for words in ("read tool", "not a tool", "'surprise'", "'uom' should be string"):
			self.assertIn(words, bad)
		self.assertIn(
			"ticket",
			self.tool_error(
				"propose_triage_fix",
				{
					"feedback": name,
					"triage_class": "Code bug",
					"summary": "x",
					"calls": [{"tool": "set_uom_aliases"}],
				},
			),
		)
		self.propose(name)
		row = STORE.get_raw("App Feedback", name)
		self.assertEqual((row["triage_state"], row.get("status") or "Open"), ("Proposed", "Open"))
		self.assertEqual(json.loads(row["proposal_json"])["calls"][0]["tool"], "set_feature_flag")

	def propose(self, name):
		return self.tool_data(
			"propose_triage_fix",
			{
				"feedback": name,
				"triage_class": "Data or config",
				"summary": "Count Place Pac in pacs",
				"calls": [
					{
						"tool": "set_feature_flag",
						"arguments": {"flag_key": "place_pac_count", "value": True},
						"why": "turns on counting in pacs",
					}
				],
			},
		)

	def test_approve_runs_through_the_dispatcher_then_replies_and_resolves(self):
		name = self.note("Place Pac should be counted in pacs")
		self.propose(name)
		STORE.commit()
		self.configure(enabled=1, **{**ON, "allow_set_feature_flag": 0})
		stopped = self.tool_data("approve_triage_proposal", {"feedback": name})
		self.assertFalse(stopped["ok"])
		self.assertIn("switched off", stopped["message"])
		self.assertEqual(STORE.get_raw("App Feedback", name)["triage_state"], "Approved")
		self.configure(enabled=1, **ON)
		done = self.tool_data("approve_triage_proposal", {"feedback": name, "note": "Thanks Ana"})
		self.assertTrue(done["ok"])
		row = STORE.get_raw("App Feedback", name)
		self.assertEqual((row["triage_state"], row["status"]), ("Applied", "Resolved"))
		applied = json.loads(row["applied_json"])
		self.assertEqual([r["ok"] for r in applied["results"]], [True])
		self.assertTrue(flags.enabled("place_pac_count", MAIN))
		replies = app_feedback.replies_of(name)
		self.assertTrue(replies[-1]["reply"].startswith("Fixed: Count Place Pac in pacs"))
		self.assertIn("Thanks Ana", replies[-1]["reply"])

	def test_only_a_manager_approves_and_reject_keeps_the_note_open(self):
		name = self.note("Place Pac should be counted in pacs")
		self.propose(name)
		STORE.commit()
		set_roles("Administrator", ["Accounts User"])
		self.assertIn("Farm Manager", self.tool_error("approve_triage_proposal", {"feedback": name}))
		set_roles("Administrator", ["Farm Manager"])
		self.assertIn("reason", self.tool_error("reject_triage_proposal", {"feedback": name, "reason": ""}))
		self.tool_data("reject_triage_proposal", {"feedback": name, "reason": "Pacs is already an alias"})
		row = STORE.get_raw("App Feedback", name)
		self.assertEqual((row["triage_state"], row.get("status") or "Open"), ("Rejected", "Open"))
		self.assertFalse(flags.enabled("place_pac_count", MAIN))
		set_roles("Administrator", ["System Manager"])

	def test_a_ticket_and_the_queue(self):
		name = self.note("The scan screen freezes on an iPad")
		self.tool_data(
			"propose_triage_fix",
			{
				"feedback": name,
				"triage_class": "Code bug",
				"summary": "Scanner freezes on iPad",
				"ticket": {
					"title": "Scanner freeze",
					"expected": "scans",
					"actual": "freezes",
					"severity": "high",
				},
			},
		)
		self.assertIn("code change", self.tool_error("approve_triage_proposal", {"feedback": name}))
		queue = self.tool_data("list_triage_queue", {"state": "Ticketed"})["queue"]
		self.assertEqual([(q["name"], q["ticket"]["title"]) for q in queue], [(name, "Scanner freeze")])

	def test_the_desk_button_needs_the_switch(self):
		name = self.note("Place Pac should be counted in pacs")
		self.propose(name)
		STORE.commit()
		self.configure(enabled=1, **{**ON, "allow_approve_triage_proposal": 0})
		doc = frappe.get_doc("App Feedback", name)
		with self.assertRaises(Exception) as caught:
			doc.approve_triage_proposal()
		self.assertIn("allow_approve_triage_proposal", str(caught.exception))

	def test_the_patch_classifies_the_open_backlog(self):
		name = self.note("The unit on the bait is wrong")
		frappe.db.set_value("App Feedback", name, {"triage_state": "", "triage_class": ""})
		self.assertEqual(triage_existing_feedback.run(), 1)
		self.assertEqual(STORE.get_raw("App Feedback", name)["triage_state"], "Auto-classified")
		self.assertEqual(triage_existing_feedback.run(), 0)

	def test_linked_records_must_exist(self):
		self.assertEqual(triage.linked_records("see FT-2026-09-0001 and DVAL-2026-0001"), [])
