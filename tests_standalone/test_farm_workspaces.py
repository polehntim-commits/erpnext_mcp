# SPDX-License-Identifier: MIT
"""The nine farm workspaces built from `workspace_specs/*.json`.

SIX CLAIMS.

1. `TheSpecsNameRealThings` — every DocType a spec names is one this app ships or
   one ERPNext/HRMS ships, and every filter names a real column. The builder drops
   a missing target silently on a site, which is right for a site without HRMS
   and wrong for a typo, so the typo has to fail here.
2. `ThePagesAreBuilt` — all nine, public, in this app's module, after Irrigation.
3. `ThePagesHaveSomethingOnThem` — every child row has the block that renders it.
4. `NothingIsRebuiltOverSomebody` — the reason these are built in code rather
   than shipped as force-synced fixtures.
5. `ItDegradesRatherThanExploding` — this runs inside `bench migrate`.
6. `TheyComeOffWithTheApp` — and a page moved to another module stays.
"""

import glob
import json
import os

import frappe

from erpnext_mcp import dashboard, farm_workspaces
from erpnext_mcp.dashboard import CARD, MODULE, WORKSPACE

from .fixtures import SeededTestCase
from .harness import STORE

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: DocTypes the specs may name that ERPNext or HRMS ship rather than this app.
#: Pinned by hand on purpose: this is the list a reviewer checks against a bench.
FRAMEWORK_DOCTYPES = {
	"Account",
	"Asset",
	"Asset Category",
	"Branch",
	"Customer",
	"Department",
	"Designation",
	"Employee",
	"Employee Grade",
	"Employment Type",
	"Journal Entry",
	"Leave Application",
	"Payment Entry",
	"Purchase Invoice",
	"Sales Invoice",
	"Supplier",
}

#: ERPNext v15's and HRMS's own workspace names. A spec reusing one would
#: overwrite nothing (the guard sees content) but would silently never build.
DEFAULT_WORKSPACES = {
	"Accounting", "Assets", "Build", "Buying", "CRM", "ERPNext Integrations",
	"ERPNext Settings", "Employee Lifecycle", "Expense Claims", "Financial Reports",
	"HR", "Home", "Integrations", "Leaves", "Manufacturing", "Payables", "Payroll",
	"Performance", "Projects", "Quality", "Receivables", "Recruitment", "Salary Payout",
	"Selling", "Shift & Attendance", "Stock", "Support", "Tax & Benefits", "Tools",
	"Users", "Website", "Welcome Workspace",
}  # fmt: skip

EXPECTED = [
	"Farm Operations",
	"Crew & Labor",
	"Compliance",
	"Crop Protection",
	"Assets & Equipment",
	"Land & Parcels",
	"Financial",
	"Market & Sales",
	"Map & Terrain",
]


def app_doctypes() -> dict:
	out = {}
	for path in glob.glob(os.path.join(REPO, "erpnext_mcp", "erpnext_mcp", "doctype", "*", "*.json")):
		if os.path.basename(path)[:-5] != os.path.basename(os.path.dirname(path)):
			continue
		with open(path, encoding="utf-8") as handle:
			doc = json.load(handle)
		if doc.get("doctype") == "DocType":
			out[doc["name"]] = {field["fieldname"] for field in doc.get("fields") or []}
	return out


class FarmWorkspaceTestCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		STORE.rows(WORKSPACE).clear()

	def build(self) -> dict:
		return farm_workspaces.install_farm_workspaces()

	def page(self, name: str) -> dict:
		return dict(STORE.get_raw(WORKSPACE, name) or {})

	def content(self, name: str) -> list:
		raw = self.page(name).get("content")
		return json.loads(raw) if raw else []

	def row(self, report: dict, name: str) -> dict:
		return next(row for row in report["workspaces"] if row["name"] == name)


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheSpecsNameRealThings(FarmWorkspaceTestCase):
	def test_every_spec_file_loads(self):
		self.assertEqual(farm_workspaces.spec_errors(), [])

	def test_the_nine_workspaces_in_sidebar_order(self):
		self.assertEqual(farm_workspaces.workspace_names(), EXPECTED)

	def test_no_spec_reuses_a_default_workspace_name(self):
		for name in farm_workspaces.workspace_names():
			with self.subTest(name=name):
				self.assertNotIn(name, DEFAULT_WORKSPACES)

	def test_every_doctype_named_is_real(self):
		known = set(app_doctypes()) | FRAMEWORK_DOCTYPES
		for spec in farm_workspaces.load_specs():
			targets = [s["link_to"] for s in spec["shortcuts"] if s.get("type", "DocType") == "DocType"]
			targets += [
				link["link_to"]
				for card in spec["link_cards"]
				for link in card["links"]
				if link.get("link_type", "DocType") == "DocType"
			]
			targets += [c["document_type"] for c in spec["number_cards"] if not c.get("shared")]
			for target in targets:
				with self.subTest(workspace=spec["name"], doctype=target):
					self.assertIn(target, known)

	def test_every_filter_names_a_real_column(self):
		fields = app_doctypes()
		for spec in farm_workspaces.load_specs():
			pairs = [(s["link_to"], s.get("stats_filter") or {}) for s in spec["shortcuts"]]
			pairs += [(c["document_type"], c["filters"]) for c in spec["number_cards"] if not c.get("shared")]
			for doctype, filters in pairs:
				if doctype not in fields:
					continue
				for column in filters:
					if column == "docstatus":
						continue
					with self.subTest(workspace=spec["name"], doctype=doctype, column=column):
						self.assertIn(column, fields[doctype])

	def test_every_shared_card_is_defined_somewhere(self):
		known = farm_workspaces._card_specs(farm_workspaces.load_specs())
		for spec in farm_workspaces.load_specs():
			for card in spec["number_cards"]:
				with self.subTest(workspace=spec["name"], card=card["label"]):
					self.assertIn(card["label"], known)

	def test_a_card_label_is_defined_once(self):
		"""A Number Card's docname is its label, so two definitions would be one
		card counting whichever was built first."""
		seen = {spec["label"] for spec in (*dashboard.CARDS, *dashboard.DISPATCH_NUMBER_CARDS)}
		for spec in farm_workspaces.load_specs():
			for card in spec["number_cards"]:
				if card.get("shared"):
					continue
				with self.subTest(card=card["label"]):
					self.assertNotIn(card["label"], seen)
					seen.add(card["label"])

	def test_the_specs_are_not_where_frappe_force_syncs(self):
		"""Not a fixture, not a module `workspace/` folder. Either would be
		imported over an operator's page on every migrate."""
		self.assertFalse(glob.glob(os.path.join(REPO, "erpnext_mcp", "erpnext_mcp", "workspace")))
		self.assertFalse(glob.glob(os.path.join(REPO, "erpnext_mcp", "fixtures")))
		with open(os.path.join(REPO, "erpnext_mcp", "hooks.py"), encoding="utf-8") as handle:
			self.assertNotIn("\nfixtures", handle.read())


# ── 2 ───────────────────────────────────────────────────────────────────────
class ThePagesAreBuilt(FarmWorkspaceTestCase):
	def test_all_nine_are_created(self):
		report = self.build()
		self.assertEqual(report["failed"], [])
		for name in EXPECTED:
			with self.subTest(name=name):
				self.assertTrue(self.row(report, name)["created"])
				self.assertTrue(frappe.db.exists(WORKSPACE, name))

	def test_each_belongs_to_this_app_is_public_and_has_an_icon(self):
		self.build()
		for name in EXPECTED:
			page = self.page(name)
			with self.subTest(name=name):
				self.assertEqual(page.get("module"), MODULE)
				self.assertEqual(page.get("public"), 1)
				self.assertEqual(page.get("is_hidden"), 0)
				self.assertTrue(page.get("icon"))

	def test_they_sort_after_the_irrigation_page(self):
		self.build()
		for name in EXPECTED:
			with self.subTest(name=name):
				self.assertGreater(float(self.page(name).get("sequence_id") or 0), 22.0)

	def test_a_second_migrate_does_not_double_anything(self):
		self.build()
		cards = len(STORE.rows(CARD))
		self.build()
		self.assertEqual(len(STORE.rows(WORKSPACE)), len(EXPECTED))
		self.assertEqual(len(STORE.rows(CARD)), cards)

	def test_the_number_cards_are_created(self):
		self.build()
		for label in ("Active Shifts", "Open Compliance Alerts", "Expense Receipts Awaiting Approval"):
			with self.subTest(card=label):
				self.assertTrue(frappe.db.exists(CARD, label))

	def test_the_block_card_narrows_the_register_to_blocks(self):
		self.build()
		shortcut = next(s for s in self.page("Farm Operations")["shortcuts"] if s["label"] == "Blocks")
		self.assertEqual(shortcut["link_to"], "Asset Register")
		self.assertEqual(json.loads(shortcut["stats_filter"]), {"asset_type": "Block"})

	def test_a_shared_card_is_on_both_pages(self):
		self.build()
		for name in ("Farm Operations", "Crew & Labor"):
			labels = [row["number_card_name"] for row in self.page(name).get("number_cards") or []]
			with self.subTest(name=name):
				self.assertIn("Active Shifts", labels)


# ── 3 ───────────────────────────────────────────────────────────────────────
class ThePagesHaveSomethingOnThem(FarmWorkspaceTestCase):
	def test_every_row_has_a_block_and_every_block_a_row(self):
		self.build()
		for name in EXPECTED:
			page, content = self.page(name), self.content(name)
			with self.subTest(name=name):
				self.assertTrue(content)
				shortcuts = {e["data"]["shortcut_name"] for e in content if e["type"] == "shortcut"}
				self.assertEqual(shortcuts, {row["label"] for row in page.get("shortcuts") or []})
				cards = {e["data"]["number_card_name"] for e in content if e["type"] == "number_card"}
				self.assertEqual(cards, {row["number_card_name"] for row in page.get("number_cards") or []})
				breaks = {e["data"]["card_name"] for e in content if e["type"] == "card"}
				self.assertEqual(
					breaks,
					{row["label"] for row in page.get("links") or [] if row.get("type") == "Card Break"},
				)

	def test_block_ids_are_unique_on_a_page(self):
		self.build()
		for name in EXPECTED:
			ids = [entry["id"] for entry in self.content(name)]
			with self.subTest(name=name):
				self.assertEqual(len(ids), len(set(ids)))

	def test_a_card_break_counts_the_links_under_it(self):
		self.build()
		for name in EXPECTED:
			links = self.page(name).get("links") or []
			for index, row in enumerate(links):
				if row.get("type") != "Card Break":
					continue
				following = 0
				for later in links[index + 1 :]:
					if later.get("type") == "Card Break":
						break
					following += 1
				with self.subTest(name=name, card=row["label"]):
					self.assertEqual(row["link_count"], following)


# ── 4 ───────────────────────────────────────────────────────────────────────
class NothingIsRebuiltOverSomebody(FarmWorkspaceTestCase):
	def test_an_arranged_page_is_left_exactly_as_it_is(self):
		self.build()
		mine = json.dumps([{"id": "mine", "type": "header", "data": {"text": "Mine", "col": 12}}])
		frappe.db.set_value(WORKSPACE, "Compliance", "content", mine)

		report = self.build()
		self.assertTrue(self.row(report, "Compliance")["existed"])
		self.assertEqual(self.page("Compliance")["content"], mine)

	def test_a_blank_page_is_repaired_without_doubling_rows(self):
		self.build()
		before = len(self.page("Financial").get("shortcuts") or [])
		frappe.db.set_value(WORKSPACE, "Financial", "content", "[]")

		report = self.build()
		self.assertTrue(self.row(report, "Financial")["filled"])
		self.assertEqual(len(self.page("Financial").get("shortcuts") or []), before)


# ── 5 ───────────────────────────────────────────────────────────────────────
class ItDegradesRatherThanExploding(FarmWorkspaceTestCase):
	def absent(self, *missing):
		original = farm_workspaces.compat.doctype_exists

		def stub(doctype):
			return False if doctype in missing else original(doctype)

		farm_workspaces.compat.doctype_exists = stub
		try:
			return self.build()
		finally:
			farm_workspaces.compat.doctype_exists = original

	def test_a_site_with_no_workspace_doctype_gets_a_note(self):
		report = self.absent(WORKSPACE)
		self.assertEqual(report["workspaces"], [])
		self.assertIn("Workspace", report["note"])

	def test_a_site_without_hrms_drops_only_the_leave_links(self):
		report = self.absent("Leave Application")
		self.assertEqual(report["failed"], [])
		row = self.row(report, "Crew & Labor")
		self.assertTrue(row["created"])
		self.assertIn("Leave Requests", row["dropped"])
		self.assertNotIn("Leave Application", json.dumps(self.page("Crew & Labor")))

	def test_the_map_page_link_is_dropped_where_the_page_is_absent(self):
		report = self.build()
		self.assertIn("Farm Overview", self.row(report, "Map & Terrain")["dropped"])

	def test_the_map_page_link_is_kept_where_the_page_exists(self):
		original = farm_workspaces._target_exists

		def stub(link_type, link_to):
			return True if link_type == "Page" else original(link_type, link_to)

		farm_workspaces._target_exists = stub
		try:
			self.build()
		finally:
			farm_workspaces._target_exists = original
		shortcut = next(s for s in self.page("Map & Terrain")["shortcuts"] if s["label"] == "Farm Overview")
		self.assertEqual((shortcut["type"], shortcut["link_to"]), ("Page", "farm-overview"))
		self.assertNotIn("doc_view", shortcut)

	def test_an_unreadable_spec_is_reported_not_raised(self):
		path = os.path.join(farm_workspaces.SPEC_DIR, "zz_broken_for_test.json")
		with open(path, "w", encoding="utf-8") as handle:
			handle.write("{not json")
		try:
			report = self.build()
		finally:
			os.remove(path)
		self.assertTrue(any("zz_broken_for_test" in f["name"] for f in report["failed"]))
		self.assertEqual(len(report["workspaces"]), len(EXPECTED))


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheyComeOffWithTheApp(FarmWorkspaceTestCase):
	def test_uninstall_removes_this_apps_pages(self):
		self.build()
		rows = farm_workspaces.remove_farm_workspaces()
		self.assertTrue(all(row["removed"] for row in rows))
		self.assertEqual(len(STORE.rows(WORKSPACE)), 0)

	def test_a_page_moved_to_another_module_stays(self):
		self.build()
		frappe.db.set_value(WORKSPACE, "Financial", "module", "Accounts")
		rows = {row["name"]: row for row in farm_workspaces.remove_farm_workspaces()}
		self.assertFalse(rows["Financial"]["removed"])
		self.assertTrue(frappe.db.exists(WORKSPACE, "Financial"))
