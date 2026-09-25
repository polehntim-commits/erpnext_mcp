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
"""

import json

from erpnext_mcp.tools import generic_update

from .fixtures import V12TestCase
from .harness import STORE

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
		self.assertEqual(data["updated"]["notes"], {"from": "old", "to": "new"})
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
		self.assertEqual(data["unchanged"], ["notes"])
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

	def test_a_boolean_is_written_as_a_check(self):
		self.enable(rows(("Farm Task", "notes")))
		data = self.tool_data("update_document", self.update({"notes": True}))
		self.assertEqual(data["updated"]["notes"]["to"], 1)
