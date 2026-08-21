# SPDX-License-Identifier: MIT
"""The polymorphic pair over the four location registers, and what stops a delete.

Three things these tests are really about.

THE REFERRER TABLES ARE THE WHOLE SAFETY ARGUMENT AND THEY ARE HAND-WRITTEN.
`tools/locations.py` refuses a delete over four categories, and three of those
are driven by constants somebody typed: `CHILD_REGISTERS`, `STATIC_REFERRERS`
and `DYNAMIC_REFERRERS`. A doctype added in a later release with a Link to
`Field` and no entry in those tables is a delete that CUTS A JOIN EDGE AND
REPORTS SUCCESS — a spray record still printing a block name that resolves to
nothing, which is a Worker Protection Standard answer that has quietly stopped
being one. So `TheReferrerTablesAreComplete` walks the shipped DocType JSON and
compares, exactly as `test_realestate` guards `realestate.PARCEL_REFERRERS`, and
it is the most important class in this file.

THE DYNAMIC CHECK IS THE ONE FRAPPE WOULD NOT HAVE MADE. Every static Link in
these tables is one the framework's own link integrity would have refused the
delete over anyway; a Dynamic Link is two plain columns to a database. So
`DeleteRefusesActivity` files a Farm Task against a block through
`location`/`location_doctype` and proves the refusal comes from THIS app — and
`test_the_check_is_specific_to_the_register` proves it does not fire for a task
pointing at a Housing Unit that merely shares a name.

AN ARGUMENT THE NAMED REGISTER DOES NOT HAVE IS REFUSED, NOT DROPPED. `capacity`
on a block and `crop` on a cabin are somebody working from the wrong screen, and
a silent drop is how they come to believe they recorded something they did not.
`UpdateRefusesTheWrongRegistersColumn` holds that in both directions.
"""

import inspect

from erpnext_mcp import locations as location_rows
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import locations as location_tools

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import APP_DOCTYPES, STORE, _load_app_doctype

ALL_ON = {
	"allow_create_parcel": 1,
	"allow_create_field": 1,
	"allow_create_irrigation_zone": 1,
	"allow_create_housing_unit": 1,
	"allow_list_fields": 1,
	"allow_get_field": 1,
	"allow_update_farm_location": 1,
	"allow_delete_farm_location": 1,
	"allow_create_farm_task": 1,
}


class LocationTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def a_parcel(self, parcel_name="Mill Creek", acreage=131.43, company=MAIN, **overrides):
		payload = {
			"owning_entity": company,
			"parcel_name": parcel_name,
			"acreage": acreage,
			"county": "Wasco",
			"state": "OR",
			"use_type": "Orchard",
		}
		payload.update(overrides)
		return self.tool_data("create_parcel", payload)

	def a_field(self, field_name="Yellow Camp Block 3", parcel="Mill Creek", **overrides):
		payload = {
			"parcel": parcel,
			"field_name": field_name,
			"acreage": 12.5,
			"variety": "Bing",
			"condition": "Good",
		}
		payload.update(overrides)
		return self.tool_data("create_field", payload)

	def a_zone(self, zone_name="YC3-Zone2", field="Yellow Camp Block 3", **overrides):
		payload = {
			"field": field,
			"zone_name": zone_name,
			"zone_number": 2,
			"water_source": "well",
			"area_sq_ft": 100000,
		}
		payload.update(overrides)
		return self.tool_data("create_irrigation_zone", payload)

	def a_unit(self, unit_name="MC-Cabin-01", parcel="Mill Creek", **overrides):
		payload = {
			"owning_entity": MAIN,
			"parcel": parcel,
			"unit_name": unit_name,
			"unit_type": "Cabin",
			"capacity": 6,
		}
		payload.update(overrides)
		return self.tool_data("create_housing_unit", payload)


# ── 1. the tables that make the delete safe ─────────────────────────────────
class TheReferrerTablesAreComplete(V12TestCase):
	"""Read off the shipped DocType JSON, never off a list somebody maintains twice.

	The failure this prevents is silent in the worst way: the delete SUCCEEDS and
	the orphan is only visible later, on a record that still prints a name.
	"""

	def links_to(self, target: str) -> set:
		found = set()
		for doctype, folder in APP_DOCTYPES.items():
			for field in _load_app_doctype(folder).get("fields") or []:
				if field.get("fieldtype") == "Link" and field.get("options") == target:
					found.add((doctype, field["fieldname"]))
		return found

	def test_every_plain_link_to_each_register_is_declared_somewhere(self):
		"""Children plus static referrers must together cover the whole JSON.

		Split by what a person can DO about it — a child is re-registered, a
		referrer is repointed — so neither table is complete alone and the union
		has to be.
		"""
		for register in location_rows.REGISTERS:
			with self.subTest(register=register):
				declared = set(location_tools.CHILD_REGISTERS[register]) | set(
					location_tools.STATIC_REFERRERS[register]
				)
				self.assertEqual(
					declared,
					self.links_to(register),
					f"a doctype links to {register} and no table in tools/locations.py names it — "
					"delete_farm_location would leave it pointing at a document that is gone",
				)

	def test_the_two_tables_do_not_overlap(self):
		"""A doctype in both would be reported twice and counted twice."""
		for register in location_rows.REGISTERS:
			with self.subTest(register=register):
				self.assertEqual(
					set(location_tools.CHILD_REGISTERS[register])
					& set(location_tools.STATIC_REFERRERS[register]),
					set(),
				)

	def test_only_the_four_registers_are_children(self):
		"""A child is a register row. Anything else is a referrer, and the
		distinction is what makes the two refusal sentences different."""
		for register, children in location_tools.CHILD_REGISTERS.items():
			with self.subTest(register=register):
				for doctype, _ in children:
					self.assertIn(doctype, location_rows.REGISTERS)

	def test_every_dynamic_link_this_app_ships_is_declared(self):
		"""The check Frappe's own link integrity does not make, so this table is
		the only thing standing between a delete and an orphaned spray record."""
		found = set()
		for doctype, folder in APP_DOCTYPES.items():
			fields = _load_app_doctype(folder).get("fields") or []
			by_name = {field.get("fieldname"): field for field in fields}
			for field in fields:
				if field.get("fieldtype") != "Dynamic Link":
					continue
				source = by_name.get(field.get("options")) or {}
				# Only the genuinely polymorphic ones. A Dynamic Link whose
				# options column is a fixed Select ("Supplier\nCustomer") can
				# never name a register and would be a query that cannot match.
				if source.get("options") == "DocType":
					found.add((doctype, field["fieldname"], field["options"]))
		self.assertEqual(
			found,
			set(location_tools.DYNAMIC_REFERRERS),
			"a dynamic link was added or removed and tools/locations.DYNAMIC_REFERRERS did not "
			"follow — this is the table that stops a delete orphaning a task or a spray record",
		)

	def test_parcels_static_referrers_are_realestates_own_list_minus_the_tree(self):
		"""One fact, split by what a person can do about it rather than copied.

		`test_realestate` already guards `PARCEL_REFERRERS` against the shipped
		JSON, so this transitively guards this module's Parcel row too — belt and
		braces on the register with the most referrers, and what stops the two
		lists drifting the next time a doctype grows a `parcel` column.
		"""
		from erpnext_mcp.tools import realestate

		conveyance = {
			(doctype, fieldname)
			for doctype, fieldname in realestate.PARCEL_REFERRERS
			if doctype not in location_rows.REGISTERS
		}
		self.assertEqual(set(location_tools.STATIC_REFERRERS["Parcel"]), conveyance)

	def test_a_child_table_referrer_is_reported_by_its_parent(self):
		"""`Spray Application Block`'s own docname is a hash nobody can open, so
		an example drawn from it has to name the Spray Application it is a row
		of. A child table added to DYNAMIC_REFERRERS without a CHILD_TABLE_PARENT
		entry would put that hash in a refusal."""
		child_tables = {
			doctype for doctype, folder in APP_DOCTYPES.items() if _load_app_doctype(folder).get("istable")
		}
		declared = {doctype for doctype, _name, _type in location_tools.DYNAMIC_REFERRERS}
		self.assertEqual(
			declared & child_tables,
			set(location_tools.CHILD_TABLE_PARENT),
			"a child table in DYNAMIC_REFERRERS with no CHILD_TABLE_PARENT entry would report a "
			"hash as its example, which names nothing anybody can go and look at",
		)

	def test_the_four_registers_are_the_ones_the_pure_module_names(self):
		"""One vocabulary. A fifth register here and four there is how a picker
		comes to offer something no tool can write."""
		self.assertEqual(tuple(location_tools.REGISTERS), location_rows.REGISTERS)
		self.assertEqual(set(location_tools.CHILD_REGISTERS), set(location_rows.REGISTERS))
		self.assertEqual(set(location_tools.STATIC_REFERRERS), set(location_rows.REGISTERS))


# ── 2. update: one door, four writes ────────────────────────────────────────
class UpdateResolvesTheRegister(LocationTestCase):
	def test_it_changes_a_block_through_the_fields_own_tool(self):
		self.a_parcel()
		self.a_field()
		data = self.tool_data(
			"update_farm_location",
			{"doctype": "Field", "name": "Yellow Camp Block 3", "crop": "Cherry", "acres": 13.25},
		)
		self.assertEqual(STORE.get_raw("Field", "Yellow Camp Block 3 - MC")["crop"], "Cherry")
		self.assertEqual(float(STORE.get_raw("Field", "Yellow Camp Block 3 - MC")["acreage"]), 13.25)
		self.assertEqual(data["doctype"], "Field")
		self.assertEqual(data["location"], "Yellow Camp Block 3 - MC")

	def test_the_register_name_is_case_insensitive_and_register_is_an_alias(self):
		self.a_parcel()
		self.a_field()
		self.tool_data(
			"update_farm_location", {"register": "field", "name": "Yellow Camp Block 3", "notes": "ok"}
		)
		self.assertEqual(STORE.get_raw("Field", "Yellow Camp Block 3 - MC")["notes"], "ok")

	def test_the_docname_and_the_typed_name_both_resolve(self):
		self.a_parcel()
		self.a_field()
		self.tool_data(
			"update_farm_location",
			{"doctype": "Field", "name": "Yellow Camp Block 3 - MC", "notes": "by docname"},
		)
		self.assertEqual(STORE.get_raw("Field", "Yellow Camp Block 3 - MC")["notes"], "by docname")

	def test_the_option_is_read_back_rather_than_assembled_from_what_was_sent(self):
		"""A register may derive a column on save. A picker row built from the
		request would show the figure the caller typed, not the stored one."""
		self.a_parcel()
		self.a_field()
		data = self.tool_data(
			"update_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3", "acres": 9.5}
		)
		self.assertEqual(data["option"]["acreage"], 9.5)
		self.assertEqual(data["option"]["doctype"], "Field")

	def test_an_unknown_register_is_refused_naming_all_four(self):
		error = self.tool_error("update_farm_location", {"doctype": "Orchard", "name": "x", "notes": "y"})
		for register in location_rows.REGISTERS:
			self.assertIn(register, error)

	def test_a_missing_register_is_refused_rather_than_guessed(self):
		self.a_parcel()
		self.a_field()
		error = self.tool_error("update_farm_location", {"name": "Yellow Camp Block 3", "notes": "y"})
		self.assertIn("doctype is required", error)


class UpdateRefusesTheWrongRegistersColumn(LocationTestCase):
	"""Both directions, because a silent drop is the failure worth a test."""

	def test_capacity_on_a_block_is_refused_and_names_the_register_that_takes_it(self):
		self.a_parcel()
		self.a_field()
		error = self.tool_error(
			"update_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3", "capacity": 4}
		)
		self.assertIn("Housing Unit", error)
		self.assertIn("Nothing was changed", error)

	def test_crop_on_a_cabin_is_refused(self):
		self.a_parcel()
		self.a_unit()
		error = self.tool_error(
			"update_farm_location", {"doctype": "Housing Unit", "name": "MC-Cabin-01", "crop": "Bing"}
		)
		self.assertIn("Field", error)

	def test_the_refused_call_wrote_nothing(self):
		self.a_parcel()
		self.a_field()
		before = dict(STORE.get_raw("Field", "Yellow Camp Block 3 - MC"))
		self.tool_error(
			"update_farm_location",
			{"doctype": "Field", "name": "Yellow Camp Block 3", "notes": "kept?", "capacity": 4},
		)
		self.assertEqual(STORE.get_raw("Field", "Yellow Camp Block 3 - MC")["notes"], before["notes"])

	def test_a_call_with_nothing_to_change_is_refused_and_lists_what_it_takes(self):
		self.a_parcel()
		self.a_field()
		error = self.tool_error("update_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertIn("nothing to change", error)
		self.assertIn("crop", error)


class UpdateConvertsZoneAcres(LocationTestCase):
	"""The one conversion, and the reason it is not a pass-through."""

	def test_acres_on_a_zone_land_in_square_feet(self):
		self.a_parcel()
		self.a_field()
		self.a_zone()
		self.tool_data(
			"update_farm_location", {"doctype": "Irrigation Zone", "name": "YC3-Zone2", "acres": 2}
		)
		stored = STORE.get_raw("Irrigation Zone", "YC3-Zone2 - MC")
		self.assertEqual(float(stored["area_sq_ft"]), 2 * location_tools.SQ_FT_PER_ACRE)

	def test_a_non_numeric_acreage_is_refused_before_anything_is_written(self):
		self.a_parcel()
		self.a_field()
		self.a_zone()
		error = self.tool_error(
			"update_farm_location", {"doctype": "Irrigation Zone", "name": "YC3-Zone2", "acres": "wide"}
		)
		self.assertIn("acres must be a number", error)

	def test_a_block_keeps_its_acres_as_acres(self):
		"""The conversion is the zone's alone — proving it does not leak."""
		self.a_parcel()
		self.a_field()
		self.tool_data(
			"update_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3", "acres": 11}
		)
		self.assertEqual(float(STORE.get_raw("Field", "Yellow Camp Block 3 - MC")["acreage"]), 11)


class UpdateCannotRename(LocationTestCase):
	def test_the_name_column_is_not_among_the_arguments_this_door_sets(self):
		"""Every register builds its docname from it and everything downstream
		holds that docname."""
		for register in location_rows.REGISTERS:
			with self.subTest(register=register):
				spec = location_tools.REGISTERS[register]
				self.assertNotIn(spec.name_field, spec.editable.values())

	def test_no_register_lets_this_door_move_a_record_to_another_parent(self):
		for register, spec in location_tools.REGISTERS.items():
			with self.subTest(register=register):
				self.assertNotIn("parcel", spec.editable)
				self.assertNotIn("field", spec.editable)


# ── 3. delete: four refusals ────────────────────────────────────────────────
class DeleteRemovesACleanRow(LocationTestCase):
	def test_a_block_nothing_has_touched_is_deleted(self):
		self.a_parcel()
		self.a_field()
		data = self.tool_data("delete_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertEqual(data["deleted"], "Yellow Camp Block 3 - MC")
		self.assertNotIn("Yellow Camp Block 3 - MC", STORE.tables.get("Field", {}))

	def test_it_reports_which_checks_passed(self):
		self.a_parcel()
		self.a_field()
		data = self.tool_data("delete_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertEqual(set(data["checks_passed"]), set(location_tools.DELETE_CHECKS))
		self.assertEqual(data["checks_skipped"], [])

	def test_a_leaf_register_says_so_rather_than_claiming_an_empty_check(self):
		self.a_parcel()
		self.a_unit()
		data = self.tool_data("delete_farm_location", {"doctype": "Housing Unit", "name": "MC-Cabin-01"})
		self.assertIn("leaf", data["checks_passed"]["children"])

	def test_the_row_it_removed_is_reported_so_the_answer_is_not_just_a_docname(self):
		self.a_parcel()
		self.a_field()
		data = self.tool_data("delete_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertEqual(data["location_row"]["label"], "Yellow Camp Block 3")
		self.assertEqual(data["location_row"]["doctype"], "Field")


class DeleteRefusesChildren(LocationTestCase):
	def test_a_block_with_a_zone_on_it_is_refused(self):
		self.a_parcel()
		self.a_field()
		self.a_zone()
		error = self.tool_error("delete_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertIn("Irrigation Zone YC3-Zone2 - MC", error)
		self.assertIn("Nothing was deleted", error)

	def test_the_block_is_still_there_afterwards(self):
		self.a_parcel()
		self.a_field()
		self.a_zone()
		self.tool_error("delete_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertIn("Yellow Camp Block 3 - MC", STORE.tables["Field"])

	def test_a_parcel_holding_a_block_a_zone_and_a_cabin_names_all_three(self):
		self.a_parcel()
		self.a_field()
		self.a_zone()
		self.a_unit()
		error = self.tool_error("delete_farm_location", {"doctype": "Parcel", "name": "Mill Creek"})
		self.assertIn("Field", error)
		self.assertIn("Irrigation Zone", error)
		self.assertIn("Housing Unit", error)


class DeleteRefusesActivity(LocationTestCase):
	"""The dynamic-link check — the one nothing else in the stack makes."""

	def a_task_at(self, doctype: str, name: str, docname: str = "FT-0001"):
		"""A Farm Task routed at a place, seeded rather than raised.

		SEEDED ON PURPOSE. `create_farm_task` validates `task_type` against a
		closed Select and would make this fixture about that tool's vocabulary;
		what is under test is the dynamic-link scan, which reads two plain
		columns and does not care how the row got there.
		"""
		STORE.seed(
			"Farm Task",
			[
				{
					"name": docname,
					"task_name": "Thin the top block",
					"company": MAIN,
					"location_doctype": doctype,
					"location": name,
				}
			],
		)
		return docname

	def test_a_block_with_a_task_against_it_is_refused(self):
		self.a_parcel()
		self.a_field()
		self.a_task_at("Field", "Yellow Camp Block 3 - MC")
		error = self.tool_error("delete_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertIn("Farm Task", error)
		self.assertIn("record(s) of WORK", error)

	def test_the_check_is_specific_to_the_register(self):
		"""BOTH columns, always. A task whose location is a Housing Unit that
		happens to share a name must not block a Field — refusing a delete over
		somebody else's record is as wrong as allowing one."""
		self.a_parcel()
		self.a_field("Shared Name")
		self.a_unit("Shared Name")
		self.a_task_at("Housing Unit", "Shared Name - MC")
		data = self.tool_data("delete_farm_location", {"doctype": "Field", "name": "Shared Name"})
		self.assertEqual(data["deleted"], "Shared Name - MC")

	def test_turning_the_activity_check_off_is_the_one_that_really_removes_a_guard(self):
		self.a_parcel()
		self.a_field()
		self.a_task_at("Field", "Yellow Camp Block 3 - MC")
		data = self.tool_data(
			"delete_farm_location",
			{"doctype": "Field", "name": "Yellow Camp Block 3", "force_check_activity": False},
		)
		self.assertEqual(data["deleted"], "Yellow Camp Block 3 - MC")
		self.assertIn("activity", data["checks_skipped"])
		self.assertIn("genuinely removes a protection", data["note"])


class DeleteRefusesAttachments(LocationTestCase):
	def test_a_block_with_a_file_on_it_is_refused(self):
		"""A File names its parent by docname, not by link, so nothing else in
		the stack would have refused and the photograph would simply stop
		resolving."""
		self.a_parcel()
		self.a_field()
		STORE.seed(
			"File",
			[
				{
					"name": "FILE-0001",
					"file_name": "block-3.jpg",
					"attached_to_doctype": "Field",
					"attached_to_name": "Yellow Camp Block 3 - MC",
				}
			],
		)
		error = self.tool_error("delete_farm_location", {"doctype": "Field", "name": "Yellow Camp Block 3"})
		self.assertIn("file(s) are attached", error)
		self.assertIn("File FILE-0001", error)


class DeleteDryRun(LocationTestCase):
	def test_a_dry_run_on_a_clean_row_deletes_nothing_and_says_it_would_succeed(self):
		self.a_parcel()
		self.a_field()
		data = self.tool_data(
			"delete_farm_location",
			{"doctype": "Field", "name": "Yellow Camp Block 3", "dry_run": True},
		)
		self.assertIsNone(data["deleted"])
		self.assertTrue(data["dry_run"])
		self.assertIn("Yellow Camp Block 3 - MC", STORE.tables["Field"])
		self.assertIn("without dry_run would remove it", data["note"])

	def test_a_dry_run_on_a_blocked_row_reports_rather_than_raises(self):
		"""The whole point: 'why can I not remove this' answered without a
		failed write."""
		self.a_parcel()
		self.a_field()
		self.a_zone()
		data = self.tool_data(
			"delete_farm_location",
			{"doctype": "Field", "name": "Yellow Camp Block 3", "dry_run": True},
		)
		self.assertIsNone(data["deleted"])
		self.assertTrue(data["blockers"])
		self.assertIn("Irrigation Zone", str(data["found"]["children"]))


class DeleteIsScopedToTheCallersEntities(LocationTestCase):
	def test_another_entitys_block_is_not_found_rather_than_refused(self):
		self.a_parcel(company=OTHER)
		self.a_field(parcel="Mill Creek")
		error = self.tool_error(
			"delete_farm_location",
			{"doctype": "Field", "name": "Yellow Camp Block 3", "owning_entity": MAIN},
		)
		self.assertIn("Yellow Camp Block 3", error)


# ── 4. the mobile surface ───────────────────────────────────────────────────
class TheMobileWrappersDeclareTheRightArguments(V12TestCase):
	"""`routes.bind` delivers only what a signature names, so an absent argument
	is unreachable rather than merely refused.

	`inspect.signature` reads the live function OBJECT, so unlike
	`inspect.getsource` it cannot go stale while a tree is being edited.
	"""

	def arguments(self, function) -> set:
		return set(inspect.signature(function).parameters) - {"user"}

	def test_no_body_can_turn_off_a_delete_safety_check(self):
		declared = self.arguments(mobile_api.delete_farm_location)
		for check in location_tools.DELETE_CHECKS:
			with self.subTest(check=check):
				self.assertNotIn(f"force_check_{check}", declared)

	def test_the_handset_can_still_ask_for_a_dry_run(self):
		self.assertIn("dry_run", self.arguments(mobile_api.delete_farm_location))

	def test_the_update_wrapper_takes_only_what_the_create_sheet_collects(self):
		self.assertEqual(
			self.arguments(mobile_api.update_farm_location),
			{
				"name",
				"doctype",
				"register",
				"company",
				"acres",
				"crop",
				"variety",
				"block_number",
				"condition",
				"county",
				"state",
				"address",
				"unit_type",
				"capacity",
				"water_source",
				"flow_rate_gpm",
				"notes",
			},
		)

	def test_the_employee_grade_pay_columns_are_absent_from_every_org_signature(self):
		"""One value there reaches everybody on the band, so it is unreachable
		from a handset rather than refused by one."""
		from erpnext_mcp.tools import org as org_tools

		for name in ("create_employee_grade", "update_employee_grade"):
			declared = self.arguments(getattr(mobile_api, name))
			for column in org_tools.GRADE_PAY_FIELDS:
				with self.subTest(method=name, column=column):
					self.assertNotIn(column, declared)

	def test_no_org_write_takes_a_subject_from_the_body_it_should_not(self):
		"""`user` is injected by `guard.endpoint` and dropped by both the
		decorator and `bind`; nothing here may name a different principal."""
		for name in (
			"create_designation",
			"update_designation",
			"create_department",
			"update_department",
			"create_branch",
			"update_branch",
			"create_employment_type",
			"update_employment_type",
			"create_employee_grade",
			"update_employee_grade",
		):
			with self.subTest(method=name):
				self.assertNotIn("employee", self.arguments(getattr(mobile_api, name)))
