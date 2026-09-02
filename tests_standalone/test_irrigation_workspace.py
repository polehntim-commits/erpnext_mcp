# SPDX-License-Identifier: MIT
"""The Irrigation landing page.

WHAT THIS RELEASE IS NOT. It is not an `Irrigation Valve` doctype. The valves
have been Frappe documents since v0.25.0 — `Asset Register` rows whose
`asset_type` is "Irrigation Valve" — and `tools/valves.py` opens with the
argument for why a second register would be a second account of the same pipe.
`TheValvesAreStillAssetRegisterRecords` below is that claim written as a test, so
a later release cannot quietly add the duplicate this one refused to.

WHAT WAS MISSING WAS THE DOOR. Reaching the valves meant knowing to search
"Asset Register" and then setting a filter by hand.

FIVE CLAIMS.

1. `ThePageIsBuilt` — created, carrying the app's module, at a derived route.
2. `TheValveCardCarriesItsFilter` — THE RELEASE. `stats_filter` is read by
   Frappe's shortcut widget for the count badge AND for `frappe.route_options` on
   click, so the card counts the valves and lands on a list narrowed to them. A
   shortcut without it would be a shortcut to the register somebody already could
   not find the valves in — which is why this is asserted rather than assumed.
3. `ThePageHasSomethingOnIt` — the v0.16.0 bug: a Workspace renders ONLY what its
   `content` block list names.
4. `NothingIsRebuiltOverSomebody` — the safety property, and the reason this is
   built in code rather than shipped as a force-synced `workspace/*.json`.
5. `ItDegradesRatherThanExploding` — this runs inside `bench migrate`.
"""

import json

import frappe

from erpnext_mcp import irrigation_workspace
from erpnext_mcp.dashboard import MODULE, WORKSPACE

from .fixtures import SeededTestCase
from .harness import STORE


class IrrigationPageTestCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		STORE.rows(WORKSPACE).clear()

	def build(self) -> dict:
		return irrigation_workspace.install_irrigation_workspace()

	def page(self) -> dict:
		return dict(STORE.get_raw(WORKSPACE, irrigation_workspace.WORKSPACE_NAME) or {})

	def content(self) -> list:
		raw = self.page().get("content")
		return json.loads(raw) if raw else []

	def shortcut(self, label: str) -> dict:
		return next(row for row in self.page().get("shortcuts") or [] if row["label"] == label)


# ── 1 ───────────────────────────────────────────────────────────────────────
class ThePageIsBuilt(IrrigationPageTestCase):
	def test_it_creates_the_page(self):
		report = self.build()
		self.assertTrue(report["created"])
		self.assertTrue(frappe.db.exists(WORKSPACE, irrigation_workspace.WORKSPACE_NAME))

	def test_it_belongs_to_this_app(self):
		"""`remove_irrigation_workspace` refuses to delete a page moved to another
		module, so the module is the ownership test an uninstall depends on."""
		self.build()
		self.assertEqual(self.page().get("module"), MODULE)

	def test_it_is_public_rather_than_somebodys_private_page(self):
		self.build()
		self.assertEqual(self.page().get("public"), 1)
		self.assertEqual(self.page().get("is_hidden"), 0)

	def test_a_second_migrate_does_not_double_the_rows(self):
		self.build()
		self.build()
		self.assertEqual(len(STORE.rows(WORKSPACE)), 1)

	def test_it_sorts_after_the_onboarding_page(self):
		"""Irrigation is opened several times a week in season; onboarding is
		opened when somebody starts."""
		self.build()
		self.assertGreater(float(self.page().get("sequence_id") or 0), 21.0)


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheValveCardCarriesItsFilter(IrrigationPageTestCase):
	"""THE RELEASE, AND THE ONE THING WORTH ASSERTING TWICE. Without
	`stats_filter` this page would be four shortcuts to whole registers, and the
	valve one would land on the same unfiltered Asset Register list that made the
	valves hard to find in the first place."""

	def test_the_valve_shortcut_narrows_to_valves(self):
		self.build()
		stored = json.loads(self.shortcut("Irrigation Valves")["stats_filter"])
		self.assertEqual(stored, {"asset_type": "Irrigation Valve"})

	def test_the_filter_is_json_frappe_can_read_back(self):
		"""`frappe.utils.get_filter_from_json` parses this string on every click
		and on every badge refresh. A dict written straight into the column would
		be a card that silently stops filtering."""
		self.build()
		raw = self.shortcut("Irrigation Valves")["stats_filter"]
		self.assertIsInstance(raw, str)
		self.assertIsInstance(json.loads(raw), dict)

	def test_it_points_at_the_asset_register_and_not_a_valve_doctype(self):
		self.build()
		self.assertEqual(self.shortcut("Irrigation Valves")["link_to"], "Asset Register")

	def test_it_opens_a_list_rather_than_a_new_record(self):
		"""A `New` view here would offer to create an untyped asset, which is the
		one thing somebody looking for the valve register does not want."""
		self.build()
		self.assertEqual(self.shortcut("Irrigation Valves")["doc_view"], "List")

	def test_the_valves_come_first(self):
		labels = [row["label"] for row in (self.build(), self.page())[1].get("shortcuts") or []]
		self.assertEqual(labels[0], "Irrigation Valves")

	def test_no_card_filters_on_the_state_column(self):
		"""`current_state` is JSON and a valve never toggled is CLOSED with the
		column EMPTY — see `tools.valves._state_of` — so a state filter would both
		miscount and mislead. This is the negative control on the card set."""
		self.build()
		for row in self.page().get("shortcuts") or []:
			with self.subTest(shortcut=row["label"]):
				self.assertNotIn("current_state", row.get("stats_filter") or "")


# ── 3 ───────────────────────────────────────────────────────────────────────
class ThePageHasSomethingOnIt(IrrigationPageTestCase):
	"""THE v0.16.0 BUG, WRITTEN AS A TEST: child rows written, `content` left
	empty, and a page with a title and nothing else."""

	def test_the_content_block_list_is_not_empty(self):
		self.build()
		self.assertTrue(self.content())

	def test_every_shortcut_row_has_a_block_that_renders_it(self):
		self.build()
		rendered = {
			entry["data"]["shortcut_name"] for entry in self.content() if entry.get("type") == "shortcut"
		}
		for row in self.page().get("shortcuts") or []:
			with self.subTest(shortcut=row["label"]):
				self.assertIn(row["label"], rendered)

	def test_every_block_names_a_row_that_exists(self):
		self.build()
		labels = {row["label"] for row in self.page().get("shortcuts") or []}
		for entry in self.content():
			if entry.get("type") != "shortcut":
				continue
			with self.subTest(block=entry["id"]):
				self.assertIn(entry["data"]["shortcut_name"], labels)

	def test_a_link_card_names_only_doctypes_this_site_has(self):
		self.build()
		for row in self.page().get("links") or []:
			if row.get("type") == "Card Break":
				continue
			with self.subTest(link=row["link_to"]):
				self.assertTrue(frappe.db.exists("DocType", row["link_to"]))

	def test_the_page_says_where_the_valves_actually_live(self):
		"""The first thing somebody does on finding this page is wonder why there
		is no Irrigation Valve list. The answer belongs on the page."""
		self.build()
		text = " ".join(
			entry["data"].get("text", "") for entry in self.content() if entry.get("type") == "paragraph"
		)
		self.assertIn("Asset Register", text)


# ── 4 ───────────────────────────────────────────────────────────────────────
class NothingIsRebuiltOverSomebody(IrrigationPageTestCase):
	"""THE REASON THIS IS CODE AND NOT A `workspace/*.json`. A JSON in the module
	folder is in Frappe's `IMPORTABLE_DOCTYPES` and is force-synced by every
	`bench migrate`, which would overwrite an operator's own arrangement on every
	upgrade."""

	def test_an_arranged_page_is_left_exactly_as_it_is(self):
		self.build()
		mine = json.dumps([{"id": "mine", "type": "header", "data": {"text": "Mine", "col": 12}}])
		frappe.db.set_value(WORKSPACE, irrigation_workspace.WORKSPACE_NAME, "content", mine)

		report = self.build()
		self.assertTrue(report["existed"])
		self.assertFalse(report["created"])
		self.assertEqual(self.page()["content"], mine)

	def test_a_page_left_empty_by_a_bad_release_is_repaired(self):
		self.build()
		frappe.db.set_value(WORKSPACE, irrigation_workspace.WORKSPACE_NAME, "content", "[]")

		report = self.build()
		self.assertTrue(report["filled"])
		self.assertTrue(self.content())

	def test_repairing_does_not_double_the_child_rows(self):
		self.build()
		before = len(self.page().get("shortcuts") or [])
		frappe.db.set_value(WORKSPACE, irrigation_workspace.WORKSPACE_NAME, "content", "[]")
		self.build()
		self.assertEqual(len(self.page().get("shortcuts") or []), before)


# ── 5 ───────────────────────────────────────────────────────────────────────
class ItDegradesRatherThanExploding(IrrigationPageTestCase):
	"""THIS RUNS INSIDE `bench migrate`. Anything raised here takes a real site's
	migration down for every app on the bench."""

	def absent(self, missing: str):
		original = irrigation_workspace.compat.doctype_exists

		def stub(doctype):
			return False if doctype == missing else original(doctype)

		irrigation_workspace.compat.doctype_exists = stub
		try:
			return self.build()
		finally:
			irrigation_workspace.compat.doctype_exists = original

	def test_a_site_with_no_workspace_doctype_gets_a_note(self):
		report = self.absent(WORKSPACE)
		self.assertFalse(report["created"])
		self.assertIn("Workspace", report["note"])

	def test_a_site_with_no_asset_register_gets_a_note_not_a_page_of_dead_links(self):
		report = self.absent("Asset Register")
		self.assertFalse(report["created"])
		self.assertIn("Asset Register", report["note"])

	def test_a_missing_optional_register_drops_only_its_own_card(self):
		"""Water Test is not what this page is for. A site without it still gets
		the valves."""
		report = self.absent("Water Test")
		self.assertTrue(report["created"])
		labels = [row["label"] for row in self.page().get("shortcuts") or []]
		self.assertIn("Irrigation Valves", labels)
		self.assertNotIn("Water Tests", labels)


# ── the claim this release is built on ──────────────────────────────────────
class TheValvesAreStillAssetRegisterRecords(IrrigationPageTestCase):
	"""THE PREMISE, PINNED. This page exists BECAUSE there is no Irrigation Valve
	doctype and there is not meant to be one: a second register would be a second
	state for one gate, and the tag, the QR payload, the closing cascade, the
	Asset State Log and the v0.149.0 ERPNext Asset mirror are all keyed on the
	Asset Register docname. A later release that adds the duplicate fails here."""

	def test_this_app_ships_no_irrigation_valve_doctype(self):
		self.assertFalse(frappe.db.exists("DocType", "Irrigation Valve"))

	def test_the_valve_type_is_a_column_on_the_asset_register(self):
		from .harness import META

		fieldnames = {field.fieldname for field in META.get("Asset Register").fields}
		for column in ("asset_type", "valve_type", "gps_latitude", "gps_longitude", "irrigation_zone"):
			with self.subTest(column=column):
				self.assertIn(column, fieldnames)
