# SPDX-License-Identifier: MIT
"""`manage_updatable_fields` — update_document's whitelist, managed over MCP.

FIVE CLAIMS.

1. `ItHasItsOwnSwitch` — off by default, separate from allow_update_document,
   and the handler asks again so a direct import cannot skip it.

2. `AddTicksEachPairOnce` — add appends ticked rows, skips a pair already
   ticked, re-ticks one present but unticked, and drops duplicates within one
   call; what it adds is what update_document then accepts.

3. `AddCannotWidenTheFences` — a pair update_document would refuse whatever
   the table said is refused here too, and one refused entry refuses the call.

4. `RemoveDeletesEveryRowForAPair` — and reports what was not there rather
   than failing.

5. `ListShowsWhatWillTakeEffect` — every row, filtered by DocType on request,
   with the rows that can never take effect flagged.
"""

from unittest import mock

from erpnext_mcp.tools import generic_update

from .fixtures import V12TestCase
from .harness import STORE

TASK = "FT-0001"


def rows(*pairs, enabled=1):
	return [{"doctype_name": doctype, "field_name": field, "enabled": enabled} for doctype, field in pairs]


def entries(*pairs):
	return [{"doctype": doctype, "fieldname": field} for doctype, field in pairs]


class ManageTestCase(V12TestCase):
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
					"evidence_required": '{"findings_text": true}',
				}
			],
		)

	def enable(self, whitelist=(), **extra):
		self.configure(allow_manage_updatable_fields=1, update_document_fields=list(whitelist), **extra)

	def stored(self):
		single = STORE.get_raw("ERPNext MCP Settings", "ERPNext MCP Settings") or {}
		return [
			(row["doctype_name"], row["field_name"], int(row.get("enabled") or 0))
			for row in single.get("update_document_fields") or []
		]

	def manage(self, action, *pairs, **extra):
		args = {"action": action, **extra}
		if pairs:
			args["entries"] = entries(*pairs)
		return args


class ItHasItsOwnSwitch(ManageTestCase):
	def test_ships_disabled(self):
		self.configure()
		error = self.tool_error("manage_updatable_fields", self.manage("list"))
		self.assertIn("allow_manage_updatable_fields", error)

	def test_update_documents_switch_does_not_turn_it_on(self):
		self.configure(allow_update_document=1)
		error = self.tool_error("manage_updatable_fields", self.manage("add", ("Farm Task", "notes")))
		self.assertIn("allow_manage_updatable_fields", error)
		self.assertEqual(self.stored(), [])

	def test_the_handler_checks_the_switch_itself(self):
		self.configure()
		with self.assertRaises(generic_update.ToolError):
			generic_update.manage_updatable_fields(self.manage("list"))

	def test_an_unknown_action_is_refused(self):
		self.enable()
		error = self.tool_error("manage_updatable_fields", {"action": "wipe"})
		self.assertIn("must be one of: add, remove, list", error)

	def test_add_and_remove_need_entries(self):
		self.enable()
		for action in ("add", "remove"):
			with self.subTest(action=action):
				error = self.tool_error("manage_updatable_fields", {"action": action})
				self.assertIn("`entries` must be a non-empty list", error)

	def test_a_malformed_entry_is_named(self):
		self.enable()
		error = self.tool_error(
			"manage_updatable_fields",
			{"action": "add", "entries": [{"doctype": "Farm Task"}, "notes"]},
		)
		self.assertIn("entries[0]: `fieldname` is missing", error)
		self.assertIn("entries[1]: expected an object", error)
		self.assertEqual(self.stored(), [])


class AddTicksEachPairOnce(ManageTestCase):
	def test_adds_ticked_rows(self):
		self.enable()
		data = self.tool_data(
			"manage_updatable_fields", self.manage("add", ("Farm Task", "notes"), ("Farm Task", "task_name"))
		)
		self.assertEqual([e["fieldname"] for e in data["added"]], ["notes", "task_name"])
		self.assertEqual(data["added"][0]["fieldtype"], "Text")
		self.assertEqual(self.stored(), [("Farm Task", "notes", 1), ("Farm Task", "task_name", 1)])
		self.assertAudited("manage_updatable_fields", "Success")

	def test_what_it_adds_update_document_accepts(self):
		"""The point of the tool: before, refused; after, written."""
		self.enable(allow_update_document=1)
		update = {"doctype": "Farm Task", "docname": TASK, "updates": {"notes": "new"}}
		self.assertIn("not whitelisted", self.tool_error("update_document", update))
		self.tool_data("manage_updatable_fields", self.manage("add", ("Farm Task", "notes")))
		self.tool_data("update_document", update)
		self.assertEqual(STORE.get_raw("Farm Task", TASK)["notes"], "new")

	def test_a_ticked_pair_is_skipped(self):
		self.enable(rows(("Farm Task", "notes")))
		data = self.tool_data(
			"manage_updatable_fields", self.manage("add", ("Farm Task", "notes"), ("Farm Task", "task_name"))
		)
		self.assertEqual([e["fieldname"] for e in data["already_present"]], ["notes"])
		self.assertEqual([e["fieldname"] for e in data["added"]], ["task_name"])
		self.assertEqual(self.stored(), [("Farm Task", "notes", 1), ("Farm Task", "task_name", 1)])

	def test_an_unticked_pair_is_ticked_not_duplicated(self):
		self.enable(rows(("Farm Task", "notes"), enabled=0))
		data = self.tool_data("manage_updatable_fields", self.manage("add", ("Farm Task", "notes")))
		self.assertEqual([e["fieldname"] for e in data["re_enabled"]], ["notes"])
		self.assertEqual(data["added"], [])
		self.assertEqual(self.stored(), [("Farm Task", "notes", 1)])

	def test_a_pair_repeated_in_one_call_is_added_once(self):
		self.enable()
		data = self.tool_data(
			"manage_updatable_fields", self.manage("add", ("Farm Task", "notes"), ("Farm Task", "notes"))
		)
		self.assertEqual(len(data["added"]), 1)
		self.assertEqual(self.stored(), [("Farm Task", "notes", 1)])

	def test_nothing_new_saves_nothing(self):
		self.enable(rows(("Farm Task", "notes")))
		with mock.patch.object(generic_update, "_save_settings") as save:
			data = self.tool_data("manage_updatable_fields", self.manage("add", ("Farm Task", "notes")))
		save.assert_not_called()
		self.assertEqual(data["added"], [])

	def test_entries_may_arrive_as_a_json_string(self):
		self.enable()
		self.tool_data(
			"manage_updatable_fields",
			{"action": "add", "entries": '[{"doctype": "Farm Task", "fieldname": "notes"}]'},
		)
		self.assertEqual(self.stored(), [("Farm Task", "notes", 1)])

	def test_a_settings_write_denial_refuses(self):
		self.enable()
		STORE.denied_permissions.add(("ERPNext MCP Settings", "write"))
		error = self.tool_error("manage_updatable_fields", self.manage("add", ("Farm Task", "notes")))
		self.assertIn("may not write ERPNext MCP Settings", error)
		self.assertEqual(self.stored(), [])


class AddCannotWidenTheFences(ManageTestCase):
	def refused(self, doctype, fieldname, expected):
		self.enable()
		error = self.tool_error("manage_updatable_fields", self.manage("add", (doctype, fieldname)))
		self.assertIn(expected, error)
		self.assertIn("Nothing was changed", error)
		self.assertEqual(self.stored(), [])

	def test_credential_stores_the_audit_log_and_the_whitelist_itself(self):
		for doctype in ("User", "ERPNext MCP Settings", "MCP Action Log", "MCP Update Document Field"):
			with self.subTest(doctype=doctype):
				self.refused(doctype, "enabled", "is never writable through update_document")

	def test_a_system_field(self):
		for field in ("name", "docstatus", "owner", "modified"):
			with self.subTest(field=field):
				self.refused("Farm Task", field, "is a system field")

	def test_a_password_field(self):
		self.refused("IoT Device", "auth_token", "it is a Password field")

	def test_a_child_table_field(self):
		self.refused("Farm Task", "linked_tasks", "it is a child table")

	def test_a_child_doctype(self):
		self.refused("Task Note", "note", "Task Note is a child table")

	def test_an_unknown_doctype(self):
		self.refused("Farm Taks", "notes", "no DocType called 'Farm Taks'")

	def test_an_unknown_field_gets_a_suggestion(self):
		self.refused("Farm Task", "Instructions", "Did you mean: 'notes'")

	def test_one_refused_entry_refuses_the_whole_call(self):
		self.enable()
		error = self.tool_error(
			"manage_updatable_fields",
			self.manage("add", ("Farm Task", "notes"), ("Farm Task", "owner"), ("Farm Taks", "notes")),
		)
		self.assertIn("refused 2 of 3 entries", error)
		self.assertIn("Farm Task.owner", error)
		self.assertIn("Farm Taks.notes", error)
		self.assertNotIn("Farm Task.notes:", error)
		self.assertEqual(self.stored(), [])


class RemoveDeletesEveryRowForAPair(ManageTestCase):
	def test_removes_and_keeps_the_rest(self):
		self.enable(rows(("Farm Task", "notes"), ("Farm Task", "task_name"), ("Farm Task", "notes")))
		data = self.tool_data("manage_updatable_fields", self.manage("remove", ("Farm Task", "notes")))
		self.assertEqual(data["removed"], entries(("Farm Task", "notes")))
		self.assertEqual(self.stored(), [("Farm Task", "task_name", 1)])
		self.assertAudited("manage_updatable_fields", "Success")

	def test_removed_is_refused_by_update_document(self):
		self.enable(rows(("Farm Task", "notes")), allow_update_document=1)
		self.tool_data("manage_updatable_fields", self.manage("remove", ("Farm Task", "notes")))
		error = self.tool_error(
			"update_document", {"doctype": "Farm Task", "docname": TASK, "updates": {"notes": "x"}}
		)
		self.assertIn("not whitelisted", error)

	def test_an_absent_pair_is_reported_not_refused(self):
		self.enable(rows(("Farm Task", "notes")))
		data = self.tool_data(
			"manage_updatable_fields",
			self.manage("remove", ("Farm Task", "task_name"), ("Gone Doctype", "x")),
		)
		self.assertEqual(data["removed"], [])
		self.assertEqual(data["not_found"], entries(("Farm Task", "task_name"), ("Gone Doctype", "x")))
		self.assertEqual(self.stored(), [("Farm Task", "notes", 1)])

	def test_a_row_for_a_vanished_doctype_can_still_be_removed(self):
		self.enable(rows(("Gone Doctype", "x")))
		data = self.tool_data("manage_updatable_fields", self.manage("remove", ("Gone Doctype", "x")))
		self.assertEqual(len(data["removed"]), 1)
		self.assertEqual(self.stored(), [])


class ListShowsWhatWillTakeEffect(ManageTestCase):
	def test_lists_every_row(self):
		self.enable(
			rows(("Farm Task", "task_name"), ("Farm Task", "notes"))
			+ rows(("Farm Task", "state"), enabled=0)
			+ rows(("Farm Task", "owner"), ("Gone Doctype", "x"))
		)
		data = self.tool_data("manage_updatable_fields", self.manage("list"))
		self.assertEqual(data["count"], 5)
		by_key = {(e["doctype"], e["fieldname"]): e for e in data["entries"]}
		self.assertEqual(by_key[("Farm Task", "notes")]["fieldtype"], "Text")
		self.assertIs(by_key[("Farm Task", "state")]["enabled"], False)
		self.assertIn("system field", by_key[("Farm Task", "owner")]["problem"])
		self.assertIn("no DocType", by_key[("Gone Doctype", "x")]["problem"])
		self.assertNotIn("problem", by_key[("Farm Task", "notes")])
		self.assertEqual(data["enabled_by_doctype"], {"Farm Task": ["notes", "task_name"]})

	def test_filters_by_doctype(self):
		self.enable(rows(("Farm Task", "notes"), ("Warehouse", "warehouse_name")))
		data = self.tool_data("manage_updatable_fields", self.manage("list", doctype="Warehouse"))
		self.assertEqual(
			[(e["doctype"], e["fieldname"]) for e in data["entries"]], [("Warehouse", "warehouse_name")]
		)
		self.assertEqual(data["doctype_filter"], "Warehouse")

	def test_an_empty_table(self):
		self.enable()
		data = self.tool_data("manage_updatable_fields", self.manage("list"))
		self.assertEqual((data["count"], data["entries"], data["enabled_by_doctype"]), (0, [], {}))


def _load_seed_script():
	import importlib.util
	import pathlib

	path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "seed_updatable_fields.py"
	spec = importlib.util.spec_from_file_location("seed_updatable_fields", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TheSeedScript(ManageTestCase):
	"""`scripts/seed_updatable_fields.py`, run against the double's meta."""

	def setUp(self):
		super().setUp()
		self.script = _load_seed_script()

	def seed(self, pairs=None):
		with mock.patch("builtins.print"):
			return self.script.seed(pairs, commit=False)

	def test_it_carries_the_three_registers_it_was_asked_for(self):
		starter = self.script.STARTER
		self.assertEqual(
			set(starter["Training Session"]),
			{
				"expires_date",
				"location",
				"end_time",
				"start_time",
				"duration_minutes",
				"instructor_name",
				"provider",
				"training_source",
				"delivery_method",
				"notes",
				"session_date",
			},
		)
		self.assertLessEqual(
			{"warehouse_name", "warehouse_type", "disabled", "city"}, set(starter["Warehouse"])
		)
		self.assertEqual(set(starter["ToDo"]), {"status", "description", "date", "priority"})

	def test_no_pair_is_listed_twice(self):
		pairs = self.script.starter_pairs()
		self.assertEqual(len(pairs), len(set(pairs)))

	def test_every_pair_on_an_app_doctype_is_a_real_writable_field(self):
		"""The app ships these doctypes' JSON, so a typo here is caught here. The
		ERPNext doctypes (Warehouse, ToDo, the invoices) are only partly modelled by
		the double; their fields were read off the umbrel image, and on a site that
		lacks one the script skips it by name rather than failing."""
		import json
		import pathlib

		root = pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp"
		app = {json.loads(p.read_text())["name"] for p in root.glob("**/doctype/*/*.json")}
		listed = set(self.script.STARTER) & app
		self.assertEqual(len(listed), 19)
		self.enable()
		result = self.seed()
		self.maxDiff = None
		self.assertEqual([(d, f, why) for d, f, why in result["skipped"] if d in app], [])
		self.assertEqual({d for d, _ in result["added"]} & app, listed)
		self.assertIn(("Training Session", "expires_date"), result["added"])
		self.assertIn(("Farm Task", "notes"), result["added"])

	def test_it_is_idempotent(self):
		self.enable()
		first = self.seed()
		stored = self.stored()
		second = self.seed()
		self.assertEqual(second["added"], [])
		self.assertEqual(len(second["existing"]), len(first["added"]))
		self.assertEqual(self.stored(), stored)

	def test_an_unticked_row_is_left_unticked(self):
		self.enable(rows(("Farm Task", "notes"), enabled=0))
		result = self.seed([("Farm Task", "notes"), ("Farm Task", "task_name")])
		self.assertEqual(result["added"], [("Farm Task", "task_name")])
		self.assertEqual(self.stored(), [("Farm Task", "notes", 0), ("Farm Task", "task_name", 1)])

	def test_an_unwritable_pair_is_skipped_not_written(self):
		self.enable()
		result = self.seed([("Farm Task", "owner"), ("User", "enabled"), ("Farm Task", "notes")])
		self.assertEqual(result["added"], [("Farm Task", "notes")])
		self.assertEqual(
			[(d, f) for d, f, _ in result["skipped"]], [("Farm Task", "owner"), ("User", "enabled")]
		)
		self.assertEqual(self.stored(), [("Farm Task", "notes", 1)])
