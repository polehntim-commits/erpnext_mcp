# SPDX-License-Identifier: MIT
"""The asset-type register, and the Select it replaced. v0.162.0.

`Asset Register.asset_type` was a Select with thirteen options, and the list was
ALSO written out in three Python modules that had drifted apart from each other
and from it:

  * the doctype's own Select — thirteen
  * `tools/asset_tags.ASSET_TYPES` — twelve, never Wind Machine
  * `asset_register.AssetRegister.ASSET_TYPES` — ten, and consulted by nothing
  * `farm_overview.ASSET_ICONS` — four, plus a fallback

So `register_asset` accepted a Wind Machine the tuple beside it did not name,
every Wind Machine task came back with no suggested skill because the skill map
was built from that tuple, and a farm that wanted a Fuel Tank waited for a
release. This release makes it a register.

SIX CLAIMS.

1. `TheRegisterIsTheVocabulary` — one list, read from the doctype, and the three
   Python copies are gone or derived.
2. `TheMigrationIsSafe` — the docname IS the value every asset already stored, so
   nothing is retyped; and the patch seeds what the REGISTER holds as well as
   what this app ships, which is what stops a `reqd` Link from taking assets out
   of service on a site this app has never seen.
3. `CreatingAnAsset` — an unknown type is refused with the list, a retired type
   is refused for a NEW asset and accepted on an existing one, and near-misses
   are named rather than silently resolved.
4. `RetiringAType` — `enabled` governs pickers and never readability; deleting a
   type in use is refused with the count.
5. `TheRead` — `list_asset_types` orders, filters and degrades.
6. `TheMobileRoute` — the handset can fetch the wheel, and the route is scoped
   the way a vocabulary should be.
"""

import json
import pathlib
import unittest

import frappe

from erpnext_mcp import asset_types, farm_overview
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.patches import migrate_asset_types
from erpnext_mcp.tools import asset_tags

from .fixtures import MAIN, V12TestCase
from .harness import INSTALLED_DOCTYPES, STORE
from .test_api_mobile import MobileAPITestCase

ON = {
	"allow_list_asset_types": 1,
	"allow_register_asset": 1,
	"allow_list_assets": 1,
	"allow_update_registered_asset": 1,
}


class AssetTypeTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)

	def an_asset(self, name="MC-Valve-05", asset_type="Irrigation Valve", **extra):
		return self.tool_data(
			"register_asset",
			{"name": name, "asset_type": asset_type, "company": MAIN, **extra},
		)


# ── 1 ────────────────────────────────────────────────────────────────────────
class TheRegisterIsTheVocabulary(AssetTypeTestCase):
	def test_the_seeded_register_is_on_the_site(self):
		"""`install.after_migrate` seeds it, and so does the harness, because
		`asset_type` is a `reqd` Link and a site without the masters cannot
		register or even open an asset."""
		self.assertEqual(sorted(asset_types.names()), sorted(asset_types.SEEDED_NAMES))

	def test_every_type_the_old_select_offered_is_still_a_type(self):
		"""THE ONE THAT WOULD HAVE BROKEN A FARM. Eight of the thirteen were not
		in the seed list this release was briefed with; an asset of a type with
		no master is, in the Desk, a record that cannot be opened or saved."""
		old_select = (
			"Housing Unit",
			"Irrigation Zone",
			"Irrigation Valve",
			"Sprayer",
			"Tractor",
			"Implement",
			"Vehicle",
			"Wind Machine",
			"Block",
			"Water Source",
			"Storage",
			"Cold Storage",
			"General",
		)
		for asset_type in old_select:
			with self.subTest(asset_type=asset_type):
				self.assertIn(asset_type, asset_types.SEEDED_NAMES)

	def test_the_two_new_types_are_there(self):
		for asset_type in ("Fuel Tank", "Gas Tank"):
			with self.subTest(asset_type=asset_type):
				self.assertTrue(asset_types.exists(asset_type))

	def test_the_asset_tags_tuple_is_derived_and_not_a_second_copy(self):
		"""It had drifted by one entry for six releases. Now it IS the seed list."""
		self.assertIs(asset_tags.ASSET_TYPES, asset_types.SEEDED_NAMES)

	def test_the_controller_no_longer_carries_its_own_list(self):
		"""The third copy named ten types and gated nothing at all."""
		from erpnext_mcp.erpnext_mcp.doctype.asset_register import asset_register

		self.assertFalse(hasattr(asset_register, "ASSET_TYPES"))

	def test_the_field_is_a_link_to_the_register(self):
		payload = json.loads(
			(
				pathlib.Path(__file__).resolve().parent.parent
				/ "erpnext_mcp"
				/ "erpnext_mcp"
				/ "doctype"
				/ "asset_register"
				/ "asset_register.json"
			).read_text(encoding="utf-8")
		)
		field = next(row for row in payload["fields"] if row["fieldname"] == "asset_type")
		self.assertEqual(field["fieldtype"], "Link")
		self.assertEqual(field["options"], asset_types.DOCTYPE)
		self.assertTrue(field["reqd"])

	def test_every_shipped_type_has_a_skill_and_a_mirror_category(self):
		"""The two tables that are keyed on these strings. Wind Machine was
		missing from the skill map and had been since v0.25.0 — deriving
		`ASSET_TYPES` from the register is what surfaced it."""
		for name in asset_types.SEEDED_NAMES:
			with self.subTest(asset_type=name):
				self.assertIn(name, asset_tags.ASSET_TYPE_SKILL_MAP)


# ── 2 ────────────────────────────────────────────────────────────────────────
class TheMigrationIsSafe(AssetTypeTestCase):
	def test_the_docname_is_the_value_an_asset_already_stored(self):
		"""THE DECISION THE WHOLE MIGRATION RESTS ON. `field:type_name` means a
		Select holding 'Irrigation Valve' becomes a Link holding 'Irrigation
		Valve' — so not one asset row is rewritten. A series autoname would have
		meant a migration that rewrote every asset on the site, and these
		docnames are printed on zip-tied tags in an orchard."""
		self.assertEqual(frappe.db.get_value(asset_types.DOCTYPE, "Tractor", "name"), "Tractor")
		name = self.an_asset(asset_type="Tractor")["name"]
		self.assertEqual(frappe.db.get_value("Asset Register", name, "asset_type"), "Tractor")

	def test_the_patch_seeds_a_type_the_register_holds_but_this_app_does_not_ship(self):
		"""THE HALF THAT MAKES IT SAFE ON A SITE THIS APP HAS NEVER SEEN. A
		`reqd` Link whose target does not exist is an asset that cannot be opened
		or saved, so the patch reads the DISTINCT values off the column and seeds
		those too — however they got there."""
		STORE.seed(
			"Asset Register",
			[{"name": "ODD-1", "asset_type": "Cider Press", "company": MAIN}],
		)
		report = migrate_asset_types.migrate_asset_types()
		self.assertIn("Cider Press", report["inherited"])
		self.assertIn("Cider Press", report["created"])
		self.assertTrue(asset_types.exists("Cider Press"))

	def test_an_inherited_type_is_named_on_the_console(self):
		STORE.seed(
			"Asset Register",
			[{"name": "ODD-1", "asset_type": "Cider Press", "company": MAIN}],
		)
		lines = migrate_asset_types.report_lines(migrate_asset_types.migrate_asset_types())
		self.assertTrue(any("Cider Press" in line for line in lines))

	def test_the_patch_retypes_nothing(self):
		"""`MC-FUEL-TANK` on the live site is registered as Storage, and Fuel
		Tank now exists — but retyping it is a judgement about a physical object
		and belongs to whoever walks past it."""
		STORE.seed(
			"Asset Register",
			[{"name": "MC-FUEL-TANK", "asset_type": "Storage", "company": MAIN}],
		)
		migrate_asset_types.migrate_asset_types()
		self.assertEqual(frappe.db.get_value("Asset Register", "MC-FUEL-TANK", "asset_type"), "Storage")

	def test_running_it_twice_creates_nothing_the_second_time(self):
		"""It is in patches.txt AND called from after_migrate, so it runs at
		least twice on any real bench."""
		migrate_asset_types.migrate_asset_types()
		second = migrate_asset_types.migrate_asset_types()
		self.assertEqual(second["created"], [])

	def test_an_operators_edit_survives_every_later_migrate(self):
		"""The contract `_employment_types` and `_i9_document_types` keep. Only
		ever creates what is absent, by docname."""
		frappe.db.set_value(asset_types.DOCTYPE, "Tractor", "icon", "ZZ")
		frappe.db.set_value(asset_types.DOCTYPE, "Tractor", "display_order", 999)
		migrate_asset_types.migrate_asset_types()
		self.assertEqual(frappe.db.get_value(asset_types.DOCTYPE, "Tractor", "icon"), "ZZ")
		self.assertEqual(frappe.db.get_value(asset_types.DOCTYPE, "Tractor", "display_order"), 999)

	def test_a_retired_type_is_not_resurrected_by_a_migrate(self):
		frappe.db.set_value(asset_types.DOCTYPE, "Sprayer", "enabled", 0)
		migrate_asset_types.migrate_asset_types()
		self.assertFalse(asset_types.is_enabled("Sprayer"))

	def test_a_site_with_no_register_says_so_rather_than_seeding_nothing(self):
		INSTALLED_DOCTYPES.discard(asset_types.DOCTYPE)
		self.addCleanup(INSTALLED_DOCTYPES.add, asset_types.DOCTYPE)
		report = migrate_asset_types.migrate_asset_types()
		self.assertIn("bench", report["skipped"])


# ── 3 ────────────────────────────────────────────────────────────────────────
class CreatingAnAsset(AssetTypeTestCase):
	def test_a_known_type_registers(self):
		self.assertEqual(self.an_asset()["asset_type"], "Irrigation Valve")

	def test_a_type_this_site_has_not_got_is_refused_with_the_list(self):
		error = self.tool_error("register_asset", {"name": "X-1", "asset_type": "Submarine", "company": MAIN})
		self.assertIn("Submarine", error)
		self.assertIn("not an asset type on this site", error)
		self.assertIn("Irrigation Valve", error)

	def test_a_near_miss_is_named_and_not_silently_accepted(self):
		"""'tractor' resolving to 'Tractor' on its own would be this app deciding
		what somebody meant, and the docname is what every asset stores."""
		error = self.tool_error("register_asset", {"name": "X-1", "asset_type": "tractor", "company": MAIN})
		self.assertIn("Did you mean 'Tractor'?", error)
		self.assertFalse(frappe.db.exists("Asset Register", "X-1"))

	def test_a_type_an_operator_added_registers_with_no_release(self):
		"""THE WHOLE POINT OF THE REGISTER."""
		doc = frappe.new_doc(asset_types.DOCTYPE)
		doc.type_name = "Generator"
		doc.icon = "N"
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		self.assertEqual(self.an_asset(name="GEN-1", asset_type="Generator")["asset_type"], "Generator")


# ── 4 ────────────────────────────────────────────────────────────────────────
class RetiringAType(AssetTypeTestCase):
	def retire(self, name="Sprayer"):
		frappe.db.set_value(asset_types.DOCTYPE, name, "enabled", 0)

	def test_a_retired_type_is_off_the_picker(self):
		self.retire()
		self.assertNotIn("Sprayer", asset_types.names())

	def test_a_retired_type_is_refused_for_a_new_asset(self):
		self.retire()
		error = self.tool_error("register_asset", {"name": "X-1", "asset_type": "Sprayer", "company": MAIN})
		self.assertIn("retired asset type", error)

	def test_an_asset_already_carrying_a_retired_type_still_reads(self):
		"""ENABLED GOVERNS PICKERS AND NEVER READABILITY. A farm that sold its
		sprayers still has last season's spray records pointing at one."""
		name = self.an_asset(name="SPR-1", asset_type="Sprayer")["name"]
		self.retire()
		self.assertEqual(self.tool_data("get_asset_detail", {"asset_name": name})["asset_type"], "Sprayer")

	def test_an_existing_asset_can_still_be_retyped_into_a_retired_type(self):
		"""A correction — "this was always a Sprayer" — and a register that
		refused it would leave the record wrong on purpose."""
		self.an_asset()
		self.retire()
		data = self.tool_data(
			"update_registered_asset", {"asset_name": "MC-Valve-05", "asset_type": "Sprayer"}
		)
		self.assertEqual(data["changed"]["asset_type"], ["Irrigation Valve", "Sprayer"])

	def test_deleting_a_type_in_use_is_refused_with_the_count(self):
		"""Deleting would leave those assets pointing at nothing — which is the
		whole reason `enabled` is a flag rather than a delete."""
		self.an_asset()
		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.delete_doc(asset_types.DOCTYPE, "Irrigation Valve")
		self.assertIn("1 asset", str(caught.exception))
		self.assertIn("Untick Enabled instead", str(caught.exception))

	def test_deleting_an_unused_type_is_allowed(self):
		"""The negative control: the refusal above must be about USE, not about
		the doctype being undeletable."""
		frappe.delete_doc(asset_types.DOCTYPE, "Gas Tank")
		self.assertFalse(asset_types.exists("Gas Tank"))


# ── 5 ────────────────────────────────────────────────────────────────────────
class TheRead(AssetTypeTestCase):
	def test_it_lists_the_enabled_types_in_picker_order(self):
		data = self.tool_data("list_asset_types")
		self.assertEqual([row["name"] for row in data["asset_types"]], list(asset_types.SEEDED_NAMES))
		self.assertTrue(data["available"])

	def test_a_retired_type_is_off_the_list_by_default(self):
		frappe.db.set_value(asset_types.DOCTYPE, "Sprayer", "enabled", 0)
		names = [row["name"] for row in self.tool_data("list_asset_types")["asset_types"]]
		self.assertNotIn("Sprayer", names)

	def test_a_retired_type_can_be_asked_for_and_is_flagged(self):
		"""A client showing an EXISTING asset still has to render the type it
		actually carries."""
		frappe.db.set_value(asset_types.DOCTYPE, "Sprayer", "enabled", 0)
		data = self.tool_data("list_asset_types", {"include_disabled": True})
		row = next(row for row in data["asset_types"] if row["name"] == "Sprayer")
		self.assertFalse(row["enabled"])

	def test_the_order_falls_back_to_the_name_and_not_to_creation_order(self):
		"""A Frappe Int column is NOT NULL DEFAULT 0, so a register nobody has
		ordered has fifteen rows all claiming to be first."""
		for name in asset_types.SEEDED_NAMES:
			frappe.db.set_value(asset_types.DOCTYPE, name, "display_order", 0)
		names = [row["name"] for row in self.tool_data("list_asset_types")["asset_types"]]
		self.assertEqual(names, sorted(names))

	def test_each_row_carries_what_a_picker_draws(self):
		row = next(
			row
			for row in self.tool_data("list_asset_types")["asset_types"]
			if row["name"] == "Irrigation Valve"
		)
		self.assertEqual(row["icon"], "V")
		self.assertTrue(row["description"])
		self.assertTrue(row["enabled"])

	def test_a_bench_that_has_not_migrated_answers_the_shipped_list_and_says_so(self):
		"""AN EMPTY WHEEL READS AS A BROKEN APP. Fifteen types and a flag for the
		operator does not."""
		INSTALLED_DOCTYPES.discard(asset_types.DOCTYPE)
		self.addCleanup(INSTALLED_DOCTYPES.add, asset_types.DOCTYPE)
		data = self.tool_data("list_asset_types")
		self.assertFalse(data["available"])
		self.assertEqual(data["count"], len(asset_types.SEEDED))
		self.assertIn("bench", data["note"])


# ── 6 ────────────────────────────────────────────────────────────────────────
class TheMobileRoute(MobileAPITestCase):
	"""ON `MobileAPITestCase` RATHER THAN A LOCAL FIXTURE, because the route is
	behind `guard.require_scope` and enrolling a handset by hand here would be a
	second implementation of the setUp that already exists — and one that could
	drift from what the surface actually demands."""

	def test_a_handset_can_fetch_the_wheel(self):
		self.be()
		data = mobile_api.list_asset_types()
		self.assertEqual([row["name"] for row in data["asset_types"]], list(asset_types.SEEDED_NAMES))

	def test_an_unenrolled_handset_gets_nothing(self):
		"""A vocabulary is still behind the enrolment gate: this surface answers
		nobody it has not enrolled, and a read with no scope is still a read."""
		self.be("nobody@example.com")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_asset_types()

	def test_a_retired_type_is_off_the_handsets_wheel_too(self):
		self.be()
		frappe.db.set_value(asset_types.DOCTYPE, "Sprayer", "enabled", 0)
		names = [row["name"] for row in mobile_api.list_asset_types()["asset_types"]]
		self.assertNotIn("Sprayer", names)

	def test_it_is_on_the_route_table(self):
		from erpnext_mcp.farmops_api import routes

		self.assertIn("/mobile/list_asset_types", {route.path for route in routes.ROUTES})

	def test_it_takes_no_company_because_it_is_a_vocabulary(self):
		"""The register says what KINDS of thing exist. It holds no company
		column and names no asset, so there is nothing for a scope to narrow —
		and a worker who may register an asset has to see the type list."""
		import inspect

		self.assertNotIn("company", inspect.signature(mobile_api.list_asset_types).parameters)


# ── the map ──────────────────────────────────────────────────────────────────
class TheMapReadsTheRegister(AssetTypeTestCase):
	def test_the_glyph_comes_off_the_record(self):
		self.assertEqual(farm_overview.asset_icon("Tractor")["glyph"], "T")

	def test_editing_the_record_changes_the_glyph(self):
		frappe.db.set_value(asset_types.DOCTYPE, "Tractor", "icon", "K")
		self.assertEqual(farm_overview.asset_icon("Tractor")["glyph"], "K")

	def test_a_type_with_no_icon_falls_back_to_its_own_initial(self):
		frappe.db.set_value(asset_types.DOCTYPE, "Tractor", "icon", "")
		self.assertEqual(farm_overview.asset_icon("Tractor")["glyph"], "T")

	def test_a_type_the_map_does_not_colour_still_gets_a_pin(self):
		badge = farm_overview.asset_icon("Cider Press")
		self.assertEqual(badge["glyph"], "C")
		self.assertEqual(badge["colour"], farm_overview.ASSET_ICON_DEFAULT["colour"])


class TheSeedIsWellFormed(unittest.TestCase):
	def test_every_shipped_type_is_distinct(self):
		self.assertEqual(len(asset_types.SEEDED_NAMES), len(set(asset_types.SEEDED_NAMES)))

	def test_every_shipped_type_has_a_glyph_a_description_and_an_order(self):
		for name, icon, order, detail in asset_types.SEEDED:
			with self.subTest(asset_type=name):
				self.assertTrue(icon, f"{name} has no icon")
				self.assertTrue(detail, f"{name} has no description")
				self.assertGreater(order, 0, f"{name} has no display order")

	def test_general_sorts_last(self):
		"""It is what somebody picks when none of the others fit."""
		orders = {name: order for name, _icon, order, _detail in asset_types.SEEDED}
		self.assertEqual(max(orders, key=orders.get), "General")
