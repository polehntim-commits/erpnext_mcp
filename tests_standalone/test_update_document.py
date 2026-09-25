# SPDX-License-Identifier: MIT
"""`update_document` — the generic field writer and its fences.

FIVE CLAIMS.

1. `TheSwitchAndTheWhitelistDecide` — off by default; on, it writes only the
   (DocType, field) pairs ticked in Updatable Fields, and one field not ticked
   refuses the whole call with nothing written.

2. `SomeFieldsAreNeverWritable` — Password fields, system columns, child tables
   and unknown fieldnames are refused even when an operator whitelists them.

3. `OnlyDraftsAreWritten` — submitted and cancelled documents are refused.

4. `SomeDoctypesAreNeverWritable` — the credential stores, the audit log and the
   whitelist itself, whatever the table says.

5. `TheWriteIsOneOrdinarySave` — values are read back after save, unchanged
   values are reported rather than rewritten, and a write DocPerm denial refuses.

6. `EachFieldTypeIsChecked` — Int, Float, Currency, Check, Date, Datetime, Time,
   Link, Dynamic Link, Select and Data each accept their own shape (and the
   obvious coercions) and refuse anything else, naming the field, what was sent
   and what was expected, before anything is written.

7. `TheReplyIsEnoughToTroubleshoot` — every refusal carries the field's metadata
   as JSON, every problem in a call is reported at once, a mistyped fieldname
   gets a suggestion, and a value the save rewrote is flagged.

The Float, Currency, Percent, Check, Time and Duration cases use fields added to
Farm Task's meta for the test (`extra_fields`), because no app doctype has all of
them. The checks are keyed on fieldtype, never on the doctype.
"""

import json
from unittest import mock

import frappe

from erpnext_mcp.tools import generic_update

from .fixtures import MAIN, V12TestCase
from .harness import STORE, Field

TASK = "FT-0001"
#: A valid Farm Task names its evidence; the save below runs that validation.
EVIDENCE = '{"findings_text": true}'


def rows(*pairs, enabled=1):
	return [{"doctype_name": doctype, "field_name": field, "enabled": enabled} for doctype, field in pairs]


class UpdateDocumentTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": TASK,
					"task_name": "Prune block 4",
					"task_type": "Repair",
					"state": "Draft",
					"notes": "old",
					"evidence_required": EVIDENCE,
				},
				{
					"name": "FT-SUB",
					"task_name": "Submitted",
					"task_type": "Repair",
					"state": "Draft",
					"docstatus": 1,
				},
				{
					"name": "FT-CAN",
					"task_name": "Cancelled",
					"task_type": "Repair",
					"state": "Draft",
					"docstatus": 2,
				},
			],
		)

	def enable(self, whitelist):
		self.configure(allow_update_document=1, update_document_fields=whitelist)

	def update(self, updates, docname=TASK, doctype="Farm Task"):
		return {"doctype": doctype, "docname": docname, "updates": updates}


class TheSwitchAndTheWhitelistDecide(UpdateDocumentTestCase):
	def test_ships_disabled(self):
		error = self.tool_error("update_document", self.update({"notes": "new"}))
		self.assertIn("allow_update_document", error)
		self.assertEqual(STORE.get_raw("Farm Task", TASK)["notes"], "old")

	def test_the_handler_checks_the_switch_itself(self):
		with self.assertRaises(generic_update.ToolError):
			generic_update.update_document(self.update({"notes": "new"}))

	def test_writes_a_whitelisted_field(self):
		self.enable(rows(("Farm Task", "notes"), ("Farm Task", "task_name")))
		data = self.tool_data("update_document", self.update({"notes": "new", "task_name": "Prune block 5"}))
		self.assertEqual(
			data["updated"]["notes"],
			{
				"from": "old",
				"sent": "new",
				"to": "new",
				"took_effect": True,
				"fieldtype": "Text",
				"label": "Instructions",
			},
		)
		self.assertEqual(data["updated"]["task_name"]["to"], "Prune block 5")
		stored = STORE.get_raw("Farm Task", TASK)
		self.assertEqual((stored["notes"], stored["task_name"]), ("new", "Prune block 5"))
		self.assertAudited("update_document", "Success")

	def test_one_unlisted_field_refuses_the_whole_call(self):
		self.enable(rows(("Farm Task", "notes")))
		error = self.tool_error("update_document", self.update({"notes": "new", "task_name": "x"}))
		self.assertIn("task_name: it is not whitelisted for Farm Task", error)
		self.assertNotIn("notes:", error)
		self.assertEqual(STORE.get_raw("Farm Task", TASK)["notes"], "old")

	def test_an_unticked_row_does_not_count(self):
		self.enable(rows(("Farm Task", "notes"), enabled=0))
		error = self.tool_error("update_document", self.update({"notes": "new"}))
		self.assertIn("not whitelisted", error)
		self.assertIn("No field on Farm Task is whitelisted at all", error)

	def test_a_row_for_another_doctype_does_not_count(self):
		self.enable(rows(("Training Session", "notes")))
		error = self.tool_error("update_document", self.update({"notes": "new"}))
		self.assertIn("not whitelisted for Farm Task", error)

	def test_every_refused_field_is_named(self):
		self.enable(rows(("Farm Task", "notes")))
		error = self.tool_error(
			"update_document", self.update({"task_name": "x", "owner": "a@b.c", "nope": 1})
		)
		self.assertIn("refused 3 of 3", error)
		for field in ("task_name:", "owner:", "nope:"):
			self.assertIn(field, error)

	def test_updates_must_be_a_non_empty_object(self):
		self.enable(rows(("Farm Task", "notes")))
		self.assertIn("non-empty object", self.tool_error("update_document", self.update({})))
		# A JSON string of an object is accepted — some clients send one.
		data = self.tool_data("update_document", self.update(json.dumps({"notes": "via string"})))
		self.assertEqual(data["updated"]["notes"]["to"], "via string")


class SomeFieldsAreNeverWritable(UpdateDocumentTestCase):
	def test_system_fields_are_refused_even_when_whitelisted(self):
		system = ("name", "docstatus", "creation", "modified", "modified_by", "owner", "idx", "doctype")
		self.enable(rows(*(("Farm Task", field) for field in system)))
		for field in system:
			with self.subTest(field=field):
				error = self.tool_error("update_document", self.update({field: "x"}))
				self.assertIn(f"{field}: it is a system field", error)
		self.assertEqual(STORE.get_raw("Farm Task", TASK)["docstatus"], 0)

	def test_a_password_field_is_refused_even_when_whitelisted(self):
		STORE.seed("IoT Device", [{"name": "DEV-1"}])
		self.enable(rows(("IoT Device", "auth_token")))
		error = self.tool_error(
			"update_document", self.update({"auth_token": "s3cret"}, "DEV-1", "IoT Device")
		)
		self.assertIn("auth_token: it is a Password field", error)

	def test_a_child_table_field_is_refused_even_when_whitelisted(self):
		self.enable(rows(("Farm Task", "linked_tasks")))
		error = self.tool_error("update_document", self.update({"linked_tasks": []}))
		self.assertIn("linked_tasks: it is a child table", error)

	def test_an_unknown_field_is_named(self):
		self.enable(rows(("Farm Task", "Notes")))
		error = self.tool_error("update_document", self.update({"Notes": "x"}))
		self.assertIn("Farm Task has no field called 'Notes'", error)

	def test_an_object_value_is_refused(self):
		self.enable(rows(("Farm Task", "notes")))
		error = self.tool_error("update_document", self.update({"notes": {"a": 1}}))
		self.assertIn("only a single value", error)


class OnlyDraftsAreWritten(UpdateDocumentTestCase):
	def test_submitted_is_refused(self):
		self.enable(rows(("Farm Task", "notes")))
		error = self.tool_error("update_document", self.update({"notes": "x"}, "FT-SUB"))
		self.assertIn("is submitted", error)
		self.assertNotEqual(STORE.get_raw("Farm Task", "FT-SUB").get("notes"), "x")

	def test_cancelled_is_refused(self):
		self.enable(rows(("Farm Task", "notes")))
		error = self.tool_error("update_document", self.update({"notes": "x"}, "FT-CAN"))
		self.assertIn("is cancelled", error)

	def test_a_missing_document_is_named(self):
		self.enable(rows(("Farm Task", "notes")))
		error = self.tool_error("update_document", self.update({"notes": "x"}, "FT-NOPE"))
		self.assertIn("no Farm Task called 'FT-NOPE'", error)


class SomeDoctypesAreNeverWritable(UpdateDocumentTestCase):
	def test_refused_doctypes_ignore_the_whitelist(self):
		for doctype in ("User", "ERPNext MCP Settings", "MCP Action Log", "MCP Update Document Field"):
			with self.subTest(doctype=doctype):
				self.enable(rows((doctype, "enabled")))
				error = self.tool_error("update_document", self.update({"enabled": 1}, "x", doctype))
				self.assertIn("cannot be written through update_document", error)

	def test_a_child_doctype_is_refused(self):
		self.enable(rows(("Task Note", "note")))
		error = self.tool_error("update_document", self.update({"note": "x"}, "row-1", "Task Note"))
		self.assertIn("is a child table", error)

	def test_an_unknown_doctype_is_named(self):
		self.enable(rows(("Farm Taks", "notes")))
		error = self.tool_error("update_document", self.update({"notes": "x"}, TASK, "Farm Taks"))
		self.assertIn("no DocType called 'Farm Taks'", error)


class TheWriteIsOneOrdinarySave(UpdateDocumentTestCase):
	def test_an_unchanged_value_is_reported_not_rewritten(self):
		self.enable(rows(("Farm Task", "notes"), ("Farm Task", "task_name")))
		data = self.tool_data("update_document", self.update({"notes": "old", "task_name": "Renamed"}))
		self.assertEqual(
			data["unchanged"], {"notes": {"value": "old", "fieldtype": "Text", "label": "Instructions"}}
		)
		self.assertEqual(list(data["updated"]), ["task_name"])

	def test_nothing_to_change_saves_nothing(self):
		self.enable(rows(("Farm Task", "notes")))
		before = STORE.get_raw("Farm Task", TASK).get("modified")
		data = self.tool_data("update_document", self.update({"notes": "old"}))
		self.assertEqual(data["updated"], {})
		self.assertEqual(STORE.get_raw("Farm Task", TASK).get("modified"), before)

	def test_a_write_permission_denial_refuses(self):
		self.enable(rows(("Farm Task", "notes")))
		STORE.denied_permissions.add(("Farm Task", "write"))
		error = self.tool_error("update_document", self.update({"notes": "new"}))
		self.assertIn("may not write Farm Task", error)
		self.assertEqual(STORE.get_raw("Farm Task", TASK)["notes"], "old")


def details(error: str) -> dict:
	"""The JSON block every field refusal ends with."""
	return json.loads(error.split("Details (JSON): ", 1)[1])


#: Fields no app doctype has together, added to Farm Task's meta for these tests.
EXTRA_FIELDS = (
	("load_kg", "Float"),
	("cost", "Currency"),
	("done_pct", "Percent"),
	("is_urgent", "Check"),
	("start_time", "Time"),
	("run_time", "Duration"),
)


class TypedTestCase(UpdateDocumentTestCase):
	def setUp(self):
		super().setUp()
		meta = frappe.get_meta("Farm Task")
		for fieldname, fieldtype in EXTRA_FIELDS:
			meta.add(
				Field(fieldname=fieldname, fieldtype=fieldtype, label=fieldname.replace("_", " ").title())
			)
		self.enable(
			rows(
				*(("Farm Task", name) for name, _ in EXTRA_FIELDS),
				("Farm Task", "estimated_duration_minutes"),
				("Farm Task", "phi_clears_on"),
				("Farm Task", "reported_at"),
				("Farm Task", "company"),
				("Farm Task", "urgency"),
				("Farm Task", "task_type"),
				("Farm Task", "task_name"),
				("Farm Task", "subject_doctype"),
				("Farm Task", "subject_docname"),
				("Farm Task", "notes"),
			)
		)

	def written(self, field, value):
		data = self.tool_data("update_document", self.update({field: value}))
		return data["updated"][field]["to"] if field in data["updated"] else data["unchanged"][field]["value"]

	def refused(self, field, value):
		error = self.tool_error("update_document", self.update({field: value}))
		self.assertIn(f"- {field}:", error)
		problem = details(error)["rejected"][field]
		self.assertEqual(problem["sent"], value)
		self.assertTrue(problem["expected"], problem)
		self.assertEqual(problem["field"]["fieldname"], field)
		return error, problem


class EachFieldTypeIsChecked(TypedTestCase):
	def test_int(self):
		self.assertEqual(self.written("estimated_duration_minutes", 45), 45)
		self.assertEqual(self.written("estimated_duration_minutes", "60"), 60)
		self.assertEqual(self.written("estimated_duration_minutes", 90.0), 90)
		for bad in ("ninety", 12.5, True):
			with self.subTest(bad=bad):
				_, problem = self.refused("estimated_duration_minutes", bad)
				self.assertEqual(problem["field"]["fieldtype"], "Int")
				self.assertIn("a whole number", problem["expected"])

	def test_float_currency_percent(self):
		self.assertEqual(self.written("load_kg", 12.5), 12.5)
		self.assertEqual(self.written("load_kg", "7.25"), 7.25)
		self.assertEqual(self.written("done_pct", 40), 40)
		self.assertEqual(self.written("cost", "1200.50"), 1200.5)
		error, _ = self.refused("cost", "$1,200")
		self.assertIn("thousands separator", error)
		self.assertIn("currency symbol", error)
		self.refused("load_kg", "heavy")
		self.refused("load_kg", False)

	def test_check(self):
		for sent, stored in ((True, 1), (False, 0), (1, 1), ("yes", 1), ("false", 0)):
			with self.subTest(sent=sent):
				self.assertEqual(self.written("is_urgent", sent), stored)
		_, problem = self.refused("is_urgent", "maybe")
		self.assertEqual(problem["expected"], "true/false or 1/0")
		self.refused("is_urgent", 2)

	def test_date(self):
		self.assertEqual(self.written("phi_clears_on", "2026-10-28"), "2026-10-28")
		for bad in (
			"10/28/2026",
			"2026-02-30",
			"tomorrow",
			"2026-10-28 07:00:00",
			20261028,
			"20261028",
			"2026-W43-3",
		):
			with self.subTest(bad=bad):
				_, problem = self.refused("phi_clears_on", bad)
				self.assertIn("YYYY-MM-DD", problem["expected"])

	def test_datetime(self):
		self.assertEqual(self.written("reported_at", "2026-07-20 07:30"), "2026-07-20 07:30:00")
		self.assertEqual(self.written("reported_at", "2026-07-20T08:15:30"), "2026-07-20 08:15:30")
		for bad in ("2026-07-20", "07:30", "2026-13-01 07:30"):
			with self.subTest(bad=bad):
				_, problem = self.refused("reported_at", bad)
				self.assertIn("HH:MM:SS", problem["expected"])

	def test_time_and_duration(self):
		self.assertEqual(self.written("start_time", "07:30"), "07:30:00")
		self.assertEqual(self.written("start_time", "23:59:59"), "23:59:59")
		self.refused("start_time", "25:00")
		self.refused("start_time", "7am")
		self.assertEqual(self.written("run_time", 3600), 3600)
		self.refused("run_time", -5)

	def test_link(self):
		self.assertEqual(self.written("company", MAIN), MAIN)
		error, problem = self.refused("company", "Example Trading")
		self.assertIn("company: linked document 'Example Trading' does not exist in doctype 'Company'", error)
		self.assertEqual(problem["field"]["links_to"], "Company")
		self.assertEqual(problem["links_to"], "Company")
		self.assertIn(MAIN, problem["did_you_mean"])
		self.assertIn("[Link → Company]", error)

	def test_dynamic_link(self):
		# The doctype comes from the controlling field, sent in the same call.
		data = self.tool_data(
			"update_document", self.update({"subject_doctype": "Company", "subject_docname": MAIN})
		)
		self.assertEqual(data["updated"]["subject_docname"]["to"], MAIN)
		error, _ = self.refused("subject_docname", "Nobody Ltd")
		self.assertIn("linked document 'Nobody Ltd' does not exist in doctype 'Company'", error)

	def test_dynamic_link_with_no_doctype_says_which_field_to_send(self):
		error, problem = self.refused("subject_docname", MAIN)
		self.assertIn("'subject_doctype' is empty", error)
		self.assertEqual(problem["field"]["doctype_from_field"], "subject_doctype")

	def test_select(self):
		self.assertEqual(self.written("urgency", "High"), "High")
		error, problem = self.refused("urgency", "high")
		self.assertEqual(problem["field"]["options"], ["Low", "Normal", "High", "Critical"])
		self.assertEqual(problem["did_you_mean"], ["High"])
		self.assertIn("one of: Low, Normal, High, Critical", error)
		self.refused("urgency", "Whenever")

	def test_mandatory_fields_cannot_be_cleared(self):
		for field in ("task_type", "task_name"):
			with self.subTest(field=field):
				error, problem = self.refused(field, "")
				self.assertIn("is mandatory on Farm Task", error)
				self.assertTrue(problem["field"]["reqd"])

	def test_an_optional_field_can_be_cleared(self):
		self.assertIsNone(self.written("urgency", None))

	def test_text(self):
		self.assertEqual(self.written("task_name", 42), "42")
		_, problem = self.refused("task_name", "x" * 141)
		self.assertIn("at most 140 characters", problem["expected"])
		self.refused("notes", True)


class TheReplyIsEnoughToTroubleshoot(TypedTestCase):
	def test_every_problem_in_the_call_is_reported_at_once(self):
		error = self.tool_error(
			"update_document",
			self.update(
				{
					"phi_clears_on": "Oct 28",
					"company": "Nope Inc",
					"urgency": "Soon",
					"estimated_duration_minutes": 30,
				}
			),
		)
		self.assertIn("refused 3 of 4 field(s)", error)
		found = details(error)
		self.assertEqual(set(found["rejected"]), {"phi_clears_on", "company", "urgency"})
		self.assertEqual(found["accepted"]["estimated_duration_minutes"]["fieldtype"], "Int")
		self.assertIsNone(STORE.get_raw("Farm Task", TASK).get("urgency"))

	def test_every_rejection_carries_the_field_metadata(self):
		_, problem = self.refused("urgency", "Soon")
		self.assertEqual(
			{key: problem["field"][key] for key in ("fieldname", "label", "fieldtype", "reqd")},
			{"fieldname": "urgency", "label": "Urgency", "fieldtype": "Select", "reqd": False},
		)

	def test_a_label_or_typo_gets_a_fieldname_suggestion(self):
		error = self.tool_error("update_document", self.update({"Urgncy": "High"}))
		self.assertIn("urgency", details(error)["rejected"]["Urgncy"]["did_you_mean"])
		self.assertIn("Did you mean: 'urgency'?", error)

	def test_success_echoes_before_sent_after_and_type(self):
		data = self.tool_data("update_document", self.update({"estimated_duration_minutes": "75"}))
		entry = data["updated"]["estimated_duration_minutes"]
		self.assertEqual((entry["from"], entry["sent"], entry["to"]), (None, "75", 75))
		self.assertEqual(entry["fieldtype"], "Int")
		self.assertTrue(entry["took_effect"])
		self.assertNotIn("warnings", data)

	def test_a_value_the_save_rewrote_is_flagged(self):
		controller = type(frappe.get_doc("Farm Task", TASK))
		original = controller.validate

		def rewrite(doc):
			original(doc)
			doc.urgency = "Normal"

		with mock.patch.object(controller, "validate", rewrite):
			data = self.tool_data("update_document", self.update({"urgency": "High"}))
		entry = data["updated"]["urgency"]
		self.assertEqual((entry["sent"], entry["to"], entry["took_effect"]), ("High", "Normal", False))
		self.assertIn("urgency: sent 'High' but the saved value is 'Normal'", data["warnings"][0])

	def test_the_documents_own_validation_is_named_with_the_attempt(self):
		controller = type(frappe.get_doc("Farm Task", TASK))

		def refuse(doc):
			frappe.throw("Urgency High needs a foreman")

		with mock.patch.object(controller, "validate", refuse):
			error = self.tool_error("update_document", self.update({"urgency": "High"}))
		self.assertIn("refused the save", error)
		self.assertIn("Urgency High needs a foreman", error)
		self.assertEqual(details(error)["attempted"]["urgency"]["sent"], "High")
