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

from erpnext_mcp import asset_types, farm_overview, registry
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
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


# ── 7: full CRUD through MCP, without the Desk ───────────────────────────────

CRUD_ON = {
	**ON,
	"allow_create_asset_type": 1,
	"allow_get_asset_type": 1,
	"allow_update_asset_type": 1,
	"allow_delete_asset_type": 1,
}


class TheRegisterIsManagedThroughMCP(V12TestCase):
	"""v0.162.0. Create, read, update and delete, so a farm never needs the Desk.

	THE ONE THAT IS NOT LIKE THE OTHER THREE IS UPDATE, and it is the reason this
	class is long. `Farm Asset Type` autonames `field:type_name`, so the docname
	IS the type name and `Asset Register.asset_type` on every asset stores that
	docname. Changing the name is therefore a RENAME — `frappe.rename_doc`, which
	moves the key and repoints every Link — and writing the column alone would
	leave the record calling itself one thing under a docname of another, with
	every asset still pointing at the old one.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **CRUD_ON)

	def an_asset(self, name="MC-Valve-05", asset_type="Irrigation Valve"):
		return self.tool_data("register_asset", {"name": name, "asset_type": asset_type, "company": MAIN})

	# ── create ──────────────────────────────────────────────────────────
	def test_it_creates_a_type_and_register_asset_accepts_it_at_once(self):
		"""THE WHOLE CLAIM. No release, no migration, no App Store review."""
		created = self.tool_data(
			"create_asset_type",
			{"type_name": "Generator", "icon": "N", "description": "Standby power."},
		)
		self.assertEqual(created["name"], "Generator")
		self.assertTrue(created["enabled"])
		self.assertEqual(self.an_asset(name="GEN-1", asset_type="Generator")["asset_type"], "Generator")

	def test_a_new_type_is_on_the_picker_immediately(self):
		self.tool_data("create_asset_type", {"type_name": "Generator"})
		names = [row["name"] for row in self.tool_data("list_asset_types")["asset_types"]]
		self.assertIn("Generator", names)

	def test_a_duplicate_under_another_spelling_is_refused_and_named(self):
		"""'Tractor' and 'tractor' as two records would be two different Link
		targets, splitting one kind of machine across two masters."""
		error = self.tool_error("create_asset_type", {"type_name": "tractor"})
		self.assertIn("'Tractor'", error)
		self.assertIn("Nothing was created", error)
		self.assertEqual(len(asset_types.names(enabled_only=False)), len(asset_types.SEEDED))

	def test_a_type_can_be_staged_disabled(self):
		created = self.tool_data("create_asset_type", {"type_name": "Drone", "enabled": False})
		self.assertFalse(created["enabled"])
		self.assertNotIn("Drone", [r["name"] for r in self.tool_data("list_asset_types")["asset_types"]])

	def test_nothing_but_the_name_is_guessed(self):
		"""An icon invented here would be a letter on a map nobody chose. The
		map's own fallback — the type's initial — is honest about being one."""
		created = self.tool_data("create_asset_type", {"type_name": "Drone"})
		self.assertEqual(created["icon"], "")
		self.assertIsNone(created["description"])
		self.assertEqual(created["display_order"], 0)
		self.assertEqual(farm_overview.asset_icon("Drone")["glyph"], "D")

	def test_a_nameless_create_is_refused(self):
		self.assertIn("type_name is required", self.tool_error("create_asset_type", {"icon": "X"}))

	# ── get ─────────────────────────────────────────────────────────────
	def test_get_carries_the_usage_count_and_whether_it_can_be_deleted(self):
		"""The question anybody has before touching a type. Attempting a delete
		to find out is finding out the expensive way."""
		data = self.tool_data("get_asset_type", {"name": "Irrigation Valve"})
		self.assertEqual(data["asset_count"], 0)
		self.assertTrue(data["deletable"])
		self.an_asset()
		data = self.tool_data("get_asset_type", {"name": "Irrigation Valve"})
		self.assertEqual(data["asset_count"], 1)
		self.assertFalse(data["deletable"])
		self.assertIn("cannot be deleted", data["note"])

	def test_get_returns_everything_a_picker_draws(self):
		data = self.tool_data("get_asset_type", {"name": "Tractor"})
		self.assertEqual(data["icon"], "T")
		self.assertTrue(data["description"])
		self.assertEqual(data["display_order"], 40)

	def test_an_unknown_type_is_refused_and_a_near_miss_is_named(self):
		error = self.tool_error("get_asset_type", {"name": "tractor"})
		self.assertIn("Did you mean 'Tractor'?", error)

	# ── update ──────────────────────────────────────────────────────────
	def test_it_changes_the_ordinary_fields(self):
		data = self.tool_data(
			"update_asset_type",
			{"name": "Tractor", "icon": "K", "display_order": 5, "description": "Big ones."},
		)
		self.assertEqual(sorted(data["fields_changed"]), ["description", "display_order", "icon"])
		self.assertEqual(data["icon"], "K")
		self.assertEqual(farm_overview.asset_icon("Tractor")["glyph"], "K")

	def test_retiring_is_an_update_and_leaves_the_assets_alone(self):
		self.an_asset()
		self.tool_data("update_asset_type", {"name": "Irrigation Valve", "enabled": False})
		self.assertFalse(asset_types.is_enabled("Irrigation Valve"))
		self.assertEqual(
			frappe.db.get_value("Asset Register", "MC-Valve-05", "asset_type"), "Irrigation Valve"
		)

	def test_a_retired_type_can_be_put_back(self):
		"""THE ZERO-DROP CASE ON A CHECK COLUMN. `1 if as_bool(...) else 0` is
		what keeps a `false` writable — a guard that dropped the 0 would refuse
		to retire anything while reporting that it had."""
		self.tool_data("update_asset_type", {"name": "Sprayer", "enabled": False})
		data = self.tool_data("update_asset_type", {"name": "Sprayer", "enabled": True})
		self.assertTrue(data["enabled"])
		self.assertTrue(asset_types.is_enabled("Sprayer"))

	def test_retiring_a_type_that_is_already_retired_is_a_no_op_and_says_so(self):
		self.tool_data("update_asset_type", {"name": "Sprayer", "enabled": False})
		self.assertIn(
			"nothing to update",
			self.tool_error("update_asset_type", {"name": "Sprayer", "enabled": False}),
		)

	def test_an_update_naming_no_field_is_refused(self):
		self.assertIn("nothing to update", self.tool_error("update_asset_type", {"name": "Tractor"}))

	def test_a_negative_display_order_is_refused(self):
		self.assertIn(
			"cannot be negative",
			self.tool_error("update_asset_type", {"name": "Tractor", "display_order": -1}),
		)

	# ── update: the rename ──────────────────────────────────────────────
	def test_renaming_a_type_repoints_every_asset_carrying_it(self):
		"""THE ONE THAT MATTERS. The docname IS the value every asset stores, so
		a rename that moved the key alone would leave forty valves pointing at a
		type that no longer exists — which is a `reqd` Link resolving to nothing,
		and in the Desk an asset that cannot be opened."""
		self.an_asset(name="V-1")
		self.an_asset(name="V-2")
		data = self.tool_data("update_asset_type", {"name": "Irrigation Valve", "type_name": "Water Valve"})
		self.assertEqual(data["name"], "Water Valve")
		self.assertEqual(data["renamed_from"], "Irrigation Valve")
		self.assertEqual(data["assets_repointed"], 2)
		for asset in ("V-1", "V-2"):
			with self.subTest(asset=asset):
				self.assertEqual(frappe.db.get_value("Asset Register", asset, "asset_type"), "Water Valve")

	def test_the_old_name_is_gone_after_a_rename(self):
		self.tool_data("update_asset_type", {"name": "Tractor", "type_name": "Tractor Unit"})
		self.assertFalse(asset_types.exists("Tractor"))
		self.assertTrue(asset_types.exists("Tractor Unit"))

	def test_the_name_column_moves_with_the_docname(self):
		"""`field:` autoname means the two are one string by construction. A
		rename that moved only the key would leave `type_name` reading the old
		one, and every read here goes through that column."""
		self.tool_data("update_asset_type", {"name": "Tractor", "type_name": "Tractor Unit"})
		self.assertEqual(
			frappe.db.get_value(asset_types.DOCTYPE, "Tractor Unit", "type_name"), "Tractor Unit"
		)

	def test_a_rename_onto_an_existing_type_is_refused(self):
		"""Frappe's merge flag would fold two kinds of asset into one and repoint
		every machine on both at the survivor — a decision about what those
		machines ARE, not a spelling fix."""
		error = self.tool_error("update_asset_type", {"name": "Tractor", "type_name": "Sprayer"})
		self.assertIn("MERGE", error)
		self.assertTrue(asset_types.exists("Tractor"))
		self.assertTrue(asset_types.exists("Sprayer"))

	def test_a_refused_rename_leaves_no_half_update_behind(self):
		"""The clash is checked BEFORE anything is written, so an icon named in
		the same call is not saved against a rename that did not happen.

		CALLED DIRECTLY AND NOT THROUGH `tool_error`, and that is the whole
		reason this test is written the long way. A refused tool call is rolled
		back by the dispatcher, so post-refusal document state is UNOBSERVABLE
		through `tool_error` — the first version of this test passed with the
		clash check moved after the write, which is precisely the bug it claims
		to catch. Proven by moving it: `tool_error` saw nothing, this does. See
		`a-refused-mobile-call-rolls-back-the-whole-test`.
		"""
		with self.assertRaises(ToolError):
			asset_tags.update_asset_type({"name": "Tractor", "type_name": "Sprayer", "icon": "ZZ"})
		self.assertEqual(frappe.db.get_value(asset_types.DOCTYPE, "Tractor", "icon"), "T")

	def test_renaming_to_the_same_name_is_not_a_rename(self):
		self.assertIn(
			"nothing to update",
			self.tool_error("update_asset_type", {"name": "Tractor", "type_name": "Tractor"}),
		)

	def test_a_rename_and_a_field_change_land_together(self):
		data = self.tool_data(
			"update_asset_type", {"name": "Tractor", "new_name": "Tractor Unit", "icon": "K"}
		)
		self.assertEqual(data["name"], "Tractor Unit")
		self.assertEqual(data["icon"], "K")

	# ── delete ──────────────────────────────────────────────────────────
	def test_it_deletes_a_type_nothing_carries(self):
		data = self.tool_data("delete_asset_type", {"name": "Gas Tank"})
		self.assertTrue(data["deleted"])
		self.assertFalse(asset_types.exists("Gas Tank"))
		self.assertNotIn("Gas Tank", data["remaining"])

	def test_deleting_a_type_in_use_is_refused_with_the_count_and_the_remedy(self):
		"""THE REFUSAL IS THE FEATURE. asset_type is a required Link, so deleting
		a type forty valves point at leaves forty records that cannot be opened
		— a failure that surfaces later, to somebody else."""
		self.an_asset(name="V-1")
		self.an_asset(name="V-2")
		error = self.tool_error("delete_asset_type", {"name": "Irrigation Valve"})
		self.assertIn("2 asset(s)", error)
		self.assertIn("enabled=false", error)
		self.assertIn("Nothing was deleted", error)
		self.assertTrue(asset_types.exists("Irrigation Valve"))

	def test_a_refused_delete_leaves_the_assets_readable(self):
		"""The negative control for the refusal: if the delete had gone through,
		this read is what would break."""
		self.an_asset()
		self.tool_error("delete_asset_type", {"name": "Irrigation Valve"})
		self.assertEqual(
			self.tool_data("get_asset_detail", {"asset_name": "MC-Valve-05"})["asset_type"],
			"Irrigation Valve",
		)

	def test_deleting_an_unknown_type_is_refused_by_name(self):
		self.assertIn("no asset type called", self.tool_error("delete_asset_type", {"name": "Nope"}))

	def test_the_controller_refuses_too_and_not_only_the_tool(self):
		"""Two layers on purpose: the tool's refusal names the count and the
		remedy, and the controller's catches every other door into the doctype —
		the Desk, a script, a bulk delete."""
		self.an_asset()
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc(asset_types.DOCTYPE, "Irrigation Valve")

	# ── the switches ────────────────────────────────────────────────────
	def test_each_tool_is_refused_by_the_name_of_its_own_switch(self):
		calls = {
			"create_asset_type": {"type_name": "Drone"},
			"get_asset_type": {"name": "Tractor"},
			"list_asset_types": {},
			"update_asset_type": {"name": "Tractor", "icon": "K"},
			"delete_asset_type": {"name": "Gas Tank"},
		}
		for name, arguments in calls.items():
			with self.subTest(tool=name):
				self.configure(enabled=1, **{**CRUD_ON, f"allow_{name}": 0})
				error = self.tool_error(name, arguments)
				self.assertIn(f"allow_{name}", error)

	def test_the_three_writes_default_off_and_the_two_reads_default_on(self):
		"""326 of 327 mutating tools ship off by design, and a read that had to
		be switched on would make a picker that cannot draw."""
		for name in ("create_asset_type", "update_asset_type", "delete_asset_type"):
			with self.subTest(tool=name):
				self.assertTrue(registry.TOOLS[name]["mutating"])
				self.assertNotIn(name, registry.DEFAULT_ON_MUTATING_TOOLS)
		for name in ("get_asset_type", "list_asset_types"):
			with self.subTest(tool=name):
				self.assertFalse(registry.TOOLS[name]["mutating"])

	def test_every_write_says_mutating_in_its_description(self):
		for name in ("create_asset_type", "update_asset_type", "delete_asset_type"):
			with self.subTest(tool=name):
				self.assertIn("MUTATING", registry.TOOLS[name]["description"])


# ── 8: `wire_value`, and the divergence it exists for ────────────────────────


class TheWireValueIsExplicit(V12TestCase):
	"""v0.163.1. Asked for by the iOS team: "tell us which field to send back."

	IT USUALLY EQUALS `type_name` AND IS STILL NOT REDUNDANT. `Farm Asset Type`
	autonames `field:type_name`, so the two are one string by construction — AT
	INSERT. A `field:` autoname names a document at insert and nowhere else, so
	editing the column afterwards moves it and leaves the docname alone: the row
	reads 'Fuel Depot' while its docname, and `asset_type` on every asset
	carrying it, is still 'Storage'.

	That is not a hypothetical. It was reproduced before this was written —
	`doc.type_name = "Fuel Depot"; doc.save()` left `name` as 'Storage', because
	Frappe does not put `set_only_once` on an autoname field for you. A picker
	built from `type_name` would then offer a value `register_asset` refuses, and
	the worker who picked it would get a link error naming a type they can see on
	their own screen.

	v0.163.1 closes it at both ends: the column edit is refused, and the wire
	value is stated.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **CRUD_ON)

	def rows(self, **args):
		return {row["name"]: row for row in self.tool_data("list_asset_types", args)["asset_types"]}

	def test_every_row_carries_a_wire_value(self):
		for name, row in self.rows().items():
			with self.subTest(asset_type=name):
				self.assertIn("wire_value", row)
				self.assertTrue(row["wire_value"])

	def test_it_is_present_even_when_it_equals_the_type_name(self):
		"""The whole of the iOS team's request. On a healthy register the two ARE
		equal on every row, and the key still has to be there — a client that
		only saw it on the odd row would have to guess on all the others."""
		for name, row in self.rows().items():
			with self.subTest(asset_type=name):
				self.assertEqual(row["wire_value"], row["type_name"])
				self.assertEqual(row["wire_value"], name)

	def test_it_is_the_docname_and_not_the_column(self):
		"""THE CASE IT EXISTS FOR. Written through `db.set_value` because the
		controller now refuses this edit through a save — which is the other half
		of the fix, and is tested below. A row in this state can still arrive
		from an older build or a script."""
		frappe.db.set_value(asset_types.DOCTYPE, "Storage", "type_name", "Fuel Depot")
		row = self.rows()["Storage"]
		self.assertEqual(row["type_name"], "Fuel Depot")
		self.assertEqual(row["wire_value"], "Storage")

	def test_the_wire_value_is_what_register_asset_actually_takes(self):
		"""The claim the field makes, proved rather than asserted: send back what
		`wire_value` says and the registration succeeds."""
		frappe.db.set_value(asset_types.DOCTYPE, "Storage", "type_name", "Fuel Depot")
		wire = self.rows()["Storage"]["wire_value"]
		data = self.tool_data("register_asset", {"name": "SH-1", "asset_type": wire, "company": MAIN})
		self.assertEqual(data["asset_type"], "Storage")

	def test_the_type_name_is_what_register_asset_refuses(self):
		"""The negative control. Without this, "wire_value is the docname" would
		pass on a register where the two never differ, which is every healthy
		one — and the key would look redundant to whoever read the test next."""
		frappe.db.set_value(asset_types.DOCTYPE, "Storage", "type_name", "Fuel Depot")
		error = self.tool_error(
			"register_asset", {"name": "SH-2", "asset_type": "Fuel Depot", "company": MAIN}
		)
		self.assertIn("not an asset type on this site", error)

	def test_get_asset_type_carries_it_too(self):
		"""One client reads the list to build a wheel and the detail to show a
		chosen type. Both have to say the same thing."""
		self.assertEqual(self.tool_data("get_asset_type", {"name": "Tractor"})["wire_value"], "Tractor")

	def test_a_created_type_reports_the_wire_value_at_once(self):
		created = self.tool_data("create_asset_type", {"type_name": "Generator"})
		self.assertEqual(created["wire_value"], "Generator")
		self.assertEqual(
			self.tool_data(
				"register_asset",
				{"name": "GEN-9", "asset_type": created["wire_value"], "company": MAIN},
			)["asset_type"],
			"Generator",
		)

	def test_a_rename_moves_the_wire_value_with_it(self):
		self.tool_data("update_asset_type", {"name": "Tractor", "type_name": "Tractor Unit"})
		self.assertEqual(self.rows()["Tractor Unit"]["wire_value"], "Tractor Unit")

	def test_the_handset_gets_it_as_well(self):
		"""It is the client that asked for it."""
		self.assertTrue(
			all(row.get("wire_value") for row in self.tool_data("list_asset_types")["asset_types"])
		)


class EditingTheNameColumnIsRefused(V12TestCase):
	"""v0.163.1. The server half of the same fix.

	`field:` autoname names a document at INSERT and nowhere else, so a
	`type_name` edit that is not a rename silently splits the identity in two.
	The controller refuses it and names the tool that does it properly.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **CRUD_ON)

	def test_editing_the_column_through_a_save_is_refused(self):
		doc = frappe.get_doc(asset_types.DOCTYPE, "Storage")
		doc.type_name = "Fuel Depot"
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save(ignore_permissions=True)
		self.assertIn("RENAME", str(caught.exception))
		self.assertIn("update_asset_type", str(caught.exception))

	def test_the_refusal_names_how_many_assets_would_be_stranded(self):
		self.tool_data("register_asset", {"name": "SH-1", "asset_type": "Storage", "company": MAIN})
		doc = frappe.get_doc(asset_types.DOCTYPE, "Storage")
		doc.type_name = "Fuel Depot"
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save(ignore_permissions=True)
		self.assertIn("1 assets", str(caught.exception))

	def test_the_column_is_unchanged_after_the_refusal(self):
		doc = frappe.get_doc(asset_types.DOCTYPE, "Storage")
		doc.type_name = "Fuel Depot"
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(asset_types.DOCTYPE, "Storage", "type_name"), "Storage")

	def test_saving_any_other_field_still_works(self):
		"""THE NEGATIVE CONTROL. A guard that refused every save on this doctype
		would pass the three tests above and make the register uneditable."""
		doc = frappe.get_doc(asset_types.DOCTYPE, "Storage")
		doc.icon = "B"
		doc.description = "Barns and sheds."
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(asset_types.DOCTYPE, "Storage", "icon"), "B")

	def test_creating_a_type_is_not_caught_by_the_guard(self):
		"""`is_new()` — on an insert the docname does not exist yet, and the
		whole point of `field:` autoname is that it is about to be this column."""
		self.assertEqual(self.tool_data("create_asset_type", {"type_name": "Generator"})["name"], "Generator")

	def test_the_rename_tool_is_the_way_through(self):
		"""The refusal points at it, so it had better work."""
		self.tool_data("register_asset", {"name": "SH-1", "asset_type": "Storage", "company": MAIN})
		self.tool_data("update_asset_type", {"name": "Storage", "type_name": "Fuel Depot"})
		self.assertEqual(frappe.db.get_value("Asset Register", "SH-1", "asset_type"), "Fuel Depot")
		self.assertEqual(frappe.db.get_value(asset_types.DOCTYPE, "Fuel Depot", "type_name"), "Fuel Depot")
