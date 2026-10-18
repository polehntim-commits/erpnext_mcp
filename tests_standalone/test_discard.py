# SPDX-License-Identifier: MIT
"""Discarding a draft: set aside, recoverable, excluded, purged only on purpose. v0.334.0.

SEVEN CLAIMS.

1. `Discarding` — the marker, the refusals (not a draft, a bank match, a submitted
   reference, no reason, a bad duplicate_of), and idempotence.
2. `LeftOut` — bulk submit skips it, submit refuses it, the JE list hides it, and
   restore puts it all back.
3. `Purging` — only discarded drafts, only for System Manager / Accounts Manager,
   each logged before the delete, soft links cleared only where they point at it.
4. `AutoPurge` — off at 0; at N days, only drafts discarded longer ago than that.
5. `TheDesk` — the whitelisted methods the buttons call.
6. `TheClientScripts` — two records on Journal Entry, seeded once, left alone once edited,
   removed on uninstall; and the list script run under node.
7. `TheCard` — the Financial workspace's draft card stops counting discarded drafts,
   unless an operator has changed its filter.
"""

import json
import pathlib
import shutil
import subprocess
import unittest

import frappe

from erpnext_mcp import install, journal_entry_discard_action, registry
from erpnext_mcp.api import discard as desk
from erpnext_mcp.dashboard import card_filters
from erpnext_mcp.tools import discard

from .fixtures import BANK_ACCOUNT, MAIN, OTHER, SeededTestCase, cash, supplies
from .harness import ROLES, STORE, set_roles

JE = "Journal Entry"
ON = {f"allow_{name}": 1 for name in registry.MUTATING_TOOLS} | {
	f"allow_{name}": 1 for name in ("list_discarded_drafts", "get_journal_entries")
}


class DiscardTestCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self._roles = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(lambda: (ROLES.clear(), ROLES.update(self._roles)))
		set_roles("Administrator", ["System Manager"])
		self.assertTrue(discard.ensure_fields(JE))

	def draft(self, amount=100, company=MAIN, remark="Settlement leg"):
		return self.tool_data(
			"create_journal_entry",
			{
				"company": company,
				"posting_date": "2026-03-01",
				"user_remark": remark,
				"accounts": [{"account": supplies(), "debit": amount}, {"account": cash(), "credit": amount}],
			},
		)["name"]

	def discard_it(self, names, **extra):
		return self.tool_data(
			"discard_draft_journal_entries", {"names": names, "reason": "duplicate settlement leg", **extra}
		)

	def raw(self, name):
		return STORE.get_raw(JE, name)


class Discarding(DiscardTestCase):
	def test_the_marker_is_written_and_nothing_is_deleted(self):
		keep, dup = self.draft(), self.draft()
		data = self.discard_it([dup], duplicate_of=keep)
		self.assertEqual(data["discarded"], 1)
		row = self.raw(dup)
		self.assertEqual((row["docstatus"], row["discarded"], row["duplicate_of"]), (0, 1, keep))
		self.assertEqual(row["discard_reason"], "duplicate settlement leg")
		self.assertTrue(row["discarded_by"] and row["discarded_at"])

	def test_again_is_a_skip(self):
		name = self.draft()
		self.discard_it([name])
		self.assertEqual(self.discard_it([name])["already_discarded"], 1)

	def test_refusals_are_per_name_with_the_reason(self):
		submitted, good = self.draft(), self.draft()
		self.tool_data("submit_journal_entry", {"name": submitted})
		data = self.discard_it([submitted, good, "ACC-JV-NOPE"])
		by = {row["name"]: row for row in data["results"]}
		self.assertIn("Only a draft can be discarded", by[submitted]["error"])
		self.assertIn("no Journal Entry", by["ACC-JV-NOPE"]["error"])
		self.assertTrue(by[good]["ok"])
		self.assertEqual(self.raw(submitted)["docstatus"], 1)

	def test_a_bank_match_blocks_it(self):
		name = self.draft()
		bt = frappe.get_doc(
			{
				"doctype": "Bank Transaction",
				"date": "2026-03-01",
				"bank_account": BANK_ACCOUNT,
				"company": MAIN,
				"withdrawal": 100,
				"payment_entries": [{"payment_document": JE, "payment_entry": name, "allocated_amount": 100}],
			}
		).insert()
		data = self.discard_it([name])
		self.assertIn(f"matched to Bank Transaction {bt.name}", data["results"][0]["error"])
		self.assertFalse(self.raw(name).get("discarded"))

	def test_a_submitted_reference_blocks_it(self):
		name = self.draft()
		ref = self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-02",
				"user_remark": "Refers",
				"accounts": [
					{"account": supplies(), "debit": 5, "reference_type": JE, "reference_name": name},
					{"account": cash(), "credit": 5},
				],
			},
		)["name"]
		self.assertIsNone(self.discard_it([name])["results"][0]["error"])  # a DRAFT reference does not block
		self.tool_data("restore_discarded_draft", {"name": name})
		frappe.db.set_value(JE, ref, "docstatus", 1)
		STORE.commit()
		self.assertIn(
			f"submitted Journal Entry {ref} references it", self.discard_it([name])["results"][0]["error"]
		)

	def test_reason_and_duplicate_of_are_checked(self):
		name, other_company = self.draft(), self.draft(20)
		frappe.db.set_value(JE, other_company, "company", OTHER)
		STORE.commit()
		self.assertIn(
			"reason must say why",
			self.tool_error("discard_draft_journal_entries", {"names": [name], "reason": "x"}),
		)
		for dup, words in (
			(name, "itself"),
			("ACC-JV-NOPE", "no Journal Entry"),
			(other_company, "belongs to"),
		):
			with self.subTest(dup=dup):
				self.assertIn(words, self.discard_it([name], duplicate_of=dup)["results"][0]["error"])


class LeftOut(DiscardTestCase):
	def test_bulk_submit_skips_it_and_submit_refuses_it(self):
		dead, live = self.draft(), self.draft(20)
		self.discard_it([dead])
		data = self.tool_data("bulk_submit_journal_entries", {"names": [dead, live]})
		self.assertEqual((data["submitted"], data["discarded_skipped"]), (1, 1))
		self.assertEqual(self.raw(dead)["docstatus"], 0)
		self.assertIn("discarded", self.tool_error("submit_journal_entry", {"name": dead}))

	def test_the_list_hides_it_unless_asked(self):
		dead, live = self.draft(), self.draft(20)
		self.discard_it([dead])
		args = {"from_date": "2026-01-01", "to_date": "2026-12-31", "company": MAIN}
		names = [row["name"] for row in self.tool_data("get_journal_entries", args)["journal_entries"]]
		self.assertIn(live, names)
		self.assertNotIn(dead, names)
		rows = self.tool_data("get_journal_entries", {**args, "include_discarded": True})["journal_entries"]
		self.assertEqual(next(r for r in rows if r["name"] == dead)["docstatus_label"], "draft (discarded)")

	def test_restore_puts_it_back(self):
		name = self.draft()
		self.discard_it([name])
		data = self.tool_data("restore_discarded_draft", {"name": name})
		self.assertEqual(data["was"]["discard_reason"], "duplicate settlement leg")
		self.assertFalse(self.raw(name)["discarded"])
		self.tool_data("submit_journal_entry", {"name": name})
		self.assertIn("is not discarded", self.tool_error("restore_discarded_draft", {"name": name}))

	def test_list_discarded_drafts(self):
		a, b = self.draft(100), self.draft(250)
		self.discard_it([a, b])
		data = self.tool_data("list_discarded_drafts", {"company": MAIN})
		self.assertEqual((data["count"], data["total"]), (2, 350.0))


class Purging(DiscardTestCase):
	def test_only_purge_roles(self):
		name = self.draft()
		self.discard_it([name])
		set_roles("Administrator", ["Accounts User"])
		with self.assertRaisesRegex(Exception, "may not purge"):
			discard.purge(JE, user="accounts.user@example.com")
		self.assertTrue(frappe.db.exists(JE, name))

	def test_purge_logs_then_deletes_only_discarded_drafts(self):
		dead, live = self.draft(100, remark="Cash [BB:inv:abc]"), self.draft(20)
		self.discard_it([dead], duplicate_of=live)
		set_roles("Administrator", ["Accounts Manager"])
		out = discard.purge(JE, user="Administrator")
		self.assertEqual((out["purged"], out["total"]), (1, 100.0))
		self.assertFalse(frappe.db.exists(JE, dead))
		self.assertTrue(frappe.db.exists(JE, live))
		log = frappe.get_doc(discard.LOG, out["results"][0]["log"])
		self.assertEqual((log.document_name, log.amount, log.duplicate_of), (dead, 100.0, live))
		self.assertEqual(
			(log.discard_reason, log.purged_by, log.purge_trigger),
			("duplicate settlement leg", "Administrator", "Manual"),
		)
		self.assertIn("[BB:inv:abc]", log.user_remark)
		self.assertEqual(int(log.allow_recreate or 0), 0)
		self.assertIn(dead, log.document_json)

	def test_purge_clears_soft_links_that_point_at_it_only(self):
		dead, other = self.draft(), self.draft(20)
		self.discard_it([dead])
		STORE.seed(
			"Expense Receipt",
			[
				{
					"name": "EXR-1",
					"company": MAIN,
					"linked_doctype": JE,
					"linked_document": dead,
					"amount": 1,
				},
				{
					"name": "EXR-2",
					"company": MAIN,
					"linked_doctype": JE,
					"linked_document": other,
					"amount": 1,
				},
			],
		)
		out = discard.purge(JE, names=[dead], user="Administrator")
		self.assertEqual(out["results"][0]["cleared"], ["Expense Receipt EXR-1"])
		self.assertIsNone(STORE.get_raw("Expense Receipt", "EXR-1").get("linked_document"))
		self.assertEqual(STORE.get_raw("Expense Receipt", "EXR-2")["linked_document"], other)

	def test_a_draft_restored_in_between_survives(self):
		name = self.draft()
		self.discard_it([name])
		self.tool_data("restore_discarded_draft", {"name": name})
		out = discard.purge(JE, names=[name], user="Administrator")
		self.assertEqual(out["purged"], 0)
		self.assertEqual(out["not_discarded"], [name])
		self.assertTrue(frappe.db.exists(JE, name))

	def test_list_shows_what_was_purged(self):
		name = self.draft()
		self.discard_it([name])
		discard.purge(JE, user="Administrator")
		data = self.tool_data("list_discarded_drafts", {"include_purged": True})
		self.assertEqual([row["document_name"] for row in data["purged"]], [name])
		self.assertIn("purged", self.tool_error("restore_discarded_draft", {"name": name}))


class AutoPurge(DiscardTestCase):
	def test_off_by_default(self):
		name = self.draft()
		self.discard_it([name])
		self.assertIsNone(discard.auto_purge())
		self.assertTrue(frappe.db.exists(JE, name))

	def test_only_those_discarded_longer_ago(self):
		old, recent = self.draft(), self.draft(20)
		self.discard_it([old, recent])
		frappe.db.set_value(JE, old, discard.AT, "2020-01-01 00:00:00")
		frappe.db.set_value(JE, recent, discard.AT, "2099-01-01 00:00:00")
		self.configure(enabled=1, discard_auto_purge_days=30, **ON)
		out = discard.auto_purge()
		self.assertEqual(out[JE]["purged"], 1)
		self.assertFalse(frappe.db.exists(JE, old))
		self.assertTrue(frappe.db.exists(JE, recent))
		log = frappe.db.get_all(discard.LOG, fields=["purge_trigger", "purged_by"])
		self.assertEqual([(r["purge_trigger"], r["purged_by"]) for r in log], [("Auto", "Administrator")])


class TheDesk(DiscardTestCase):
	def test_the_buttons_methods(self):
		a, b = self.draft(), self.draft(30)
		out = desk.discard(names=json.dumps([a, b]), reason="duplicates from the sync")
		self.assertEqual(out["discarded"], 2)
		self.assertEqual(desk.summary()["count"], 2)
		desk.restore(name=b)
		self.assertEqual(desk.summary()["total"], 100.0)
		self.assertEqual(desk.purge(names=json.dumps([a]))["purged"], 1)
		with self.assertRaises(frappe.ValidationError):
			desk.discard(doctype="Sales Invoice", names=[b], reason="nope nope")


class TheClientScripts(SeededTestCase):
	def scripts(self):
		return frappe.db.get_all(
			"Client Script", filters={"dt": JE}, fields=["name", "view", "script", "enabled"]
		)

	def test_seeded_once_left_alone_once_edited_removed_on_uninstall(self):
		journal_entry_discard_action.seed()
		journal_entry_discard_action.seed()
		rows = self.scripts()
		self.assertEqual(sorted(row["view"] for row in rows), ["Form", "List"])
		form = next(row for row in rows if row["view"] == "Form")
		frappe.db.set_value("Client Script", form["name"], "script", form["script"] + "\n// mine")
		journal_entry_discard_action.seed()
		self.assertIn("// mine", frappe.db.get_value("Client Script", form["name"], "script"))
		# An older copy of this app's own text, unedited, is brought up to date.
		frappe.db.set_value("Client Script", form["name"], "script", "// erpnext_mcp:je-discard-form@r0")
		marker = journal_entry_discard_action.FORM_MARKER
		fingerprint = journal_entry_discard_action._fingerprint("// erpnext_mcp:je-discard-form@r0")
		journal_entry_discard_action.PRIOR_REVISIONS[marker] = {fingerprint: "r0"}
		self.addCleanup(lambda: journal_entry_discard_action.PRIOR_REVISIONS.__setitem__(marker, {}))
		report = {r["name"]: r for r in journal_entry_discard_action.seed()}
		self.assertTrue(any(r["updated"] for r in report.values()))
		self.assertIn("@r1", frappe.db.get_value("Client Script", form["name"], "script"))
		journal_entry_discard_action.remove()
		self.assertEqual(self.scripts(), [])

	def test_the_buttons_call_methods_that_exist(self):
		for name in ("summary", "discard", "restore", "purge"):
			with self.subTest(method=name):
				self.assertIn(
					f'METHOD + ".{name}"',
					journal_entry_discard_action.LIST_SOURCE + journal_entry_discard_action.FORM_SOURCE,
				)
				self.assertTrue(callable(getattr(desk, name)))


_DRIVER = r"""
const vm = require("vm");
const added = [], buttons = [], actions = [];
let filters = [];
const listview = {
  filter_area: {
    exists: (f) => filters.some((g) => g[1] === f[1] && g[3] === f[3]),
    add: (fs) => { filters = filters.concat(fs); added.push(fs); },
    remove: (field) => { filters = filters.filter((g) => g[1] !== field); },
  },
  page: { add_inner_button: (l, fn) => buttons.push([l, fn]), add_actions_menu_item: (l) => actions.push(l) },
  get_checked_items: () => [], refresh: () => {},
};
let roles = ["Accounts Manager"];
const ctx = {
  __: (s, a) => (a ? s.replace(/\{(\d)\}/g, (_, i) => a[i]) : s), cint: (v) => (v ? 1 : 0),
  frappe: {
    listview_settings: { "Journal Entry": { get_indicator: (doc) => ["Draft", "red", "docstatus,=,0"], onload: () => added.push("erpnext") } },
    user: { has_role: (r) => roles.includes(r) }, utils: { escape_html: (s) => s }, msgprint: () => {}, call: () => Promise.resolve({}),
  },
};
vm.createContext(ctx);
vm.runInContext(process.argv[1], ctx);
const s = ctx.frappe.listview_settings["Journal Entry"];
s.onload(listview);
const before = filters.length;
buttons.find((b) => b[0] === "Show / hide discarded")[1]();
const shown = filters.length;
console.log(JSON.stringify({
  erpnext_onload_kept: added[0] === "erpnext",
  default_filter: added[1],
  toggled: [before, shown],
  indicator: s.get_indicator({ docstatus: 0, discarded: 1 }),
  plain: s.get_indicator({ docstatus: 0, discarded: 0 }),
  buttons: buttons.map((b) => b[0]), actions,
  purge_row: s.button.show({ docstatus: 0, discarded: 1 }),
  fields: s.add_fields,
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed on this machine")
class TheListScriptRuns(unittest.TestCase):
	def test_hidden_by_default_toggled_and_erpnexts_settings_kept(self):
		out = subprocess.run(
			["node", "-e", _DRIVER, journal_entry_discard_action.LIST_SOURCE],
			capture_output=True,
			text=True,
			check=True,
			timeout=30,
		)
		got = json.loads(out.stdout.strip().splitlines()[-1])
		self.assertTrue(got["erpnext_onload_kept"])
		self.assertEqual(got["default_filter"], [["Journal Entry", "discarded", "=", 0]])
		self.assertEqual(got["toggled"], [1, 0])
		self.assertEqual(got["indicator"], ["Discarded", "gray", "discarded,=,1"])
		self.assertEqual(got["plain"], ["Draft", "red", "docstatus,=,0"])
		self.assertEqual(got["buttons"], ["Show / hide discarded", "Purge discarded"])
		self.assertEqual(got["actions"], ["Discard"])
		self.assertEqual(got["purge_row"], 1)
		self.assertIn("discarded", got["fields"])


class TheCard(SeededTestCase):
	def test_the_draft_card_follows_unless_edited(self):
		old = card_filters(JE, {"docstatus": 0})
		STORE.seed(
			"Number Card",
			[
				{
					"name": "Draft Journal Entries",
					"label": "Draft Journal Entries",
					"document_type": JE,
					"filters_json": old,
				}
			],
		)
		install._discard_fields()
		self.assertEqual(
			frappe.db.get_value("Number Card", "Draft Journal Entries", "filters_json"),
			card_filters(JE, {"docstatus": 0, "discarded": 0}),
		)
		frappe.db.set_value("Number Card", "Draft Journal Entries", "filters_json", "[]")
		install._discard_fields()
		self.assertEqual(frappe.db.get_value("Number Card", "Draft Journal Entries", "filters_json"), "[]")

	def test_the_spec(self):
		spec = json.loads(
			(pathlib.Path(install.__file__).parent / "workspace_specs" / "financial.json").read_text()
		)
		card = next(c for c in spec["number_cards"] if c["label"] == "Draft Journal Entries")
		self.assertEqual(card["filters"], {"docstatus": 0, "discarded": 0})
