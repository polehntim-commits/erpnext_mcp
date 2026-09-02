# SPDX-License-Identifier: MIT
"""Every patch, against a site that has never seen this app.

WHY THIS MODULE EXISTS. v0.12.0's `register_custom_party_types` patch raised a
`LinkValidationError` on a real bench and took the whole `bench migrate` down
with it — and this suite passed, because the test double had no link validation
and inserted the impossible row happily. Two things came out of that, and both
live here:

  1. A patch is not "code that happens to run once". It runs inside somebody's
     upgrade, on a site whose state this app does not control, and an exception
     it lets out stops every other app's migration too. So every patch gets a
     test that runs it against an empty store and asserts it survives.
  2. The failure was a **link** failure, and the double now models links. See
     `Document._validate_links` in `harness.py` — the tests below would pass
     vacuously without it, which is what happened last time.

WHAT A "FRESH SITE" MEANS HERE. `STORE.reset()` plus the DocType rows the app
ships, and nothing else: no companies, no accounts, no party types, no Family
register. That is a harsher site than a real one — a real `bench migrate` has
already synced the DocTypes and run ERPNext's own fixtures — and a patch that
survives it survives anything.
"""

import re
import unittest

from erpnext_mcp import install, settings
from erpnext_mcp.patches import (
	backfill_alert_subject_employee,
	backfill_completion_signatures,
	backfill_field_varieties,
	backfill_observation_type,
	backfill_planting_rootstock,
	fix_literal_newlines_in_instructions,
	migrate_declarative_rules,
	migrate_incident_tool_switches,
	migrate_training_types,
	recompute_2026_dependents_credit,
	register_custom_party_types,
	rename_discipline_record,
	repoint_producer_task_template,
	set_default_tool_switches,
	widen_i9_attestation_filters,
)
from erpnext_mcp.tools import company

from .harness import APP_DOCTYPES, INSTALLED_DOCTYPES, META, STORE, MCPTestCase, frappe

#: Every patch this app ships, as `(dotted name, module)`. The test that reads
#: `patches.txt` keeps this list honest, so a patch added without a test here
#: fails the build rather than shipping unexercised.
PATCHES = (
	("erpnext_mcp.patches.set_default_tool_switches", set_default_tool_switches),
	("erpnext_mcp.patches.register_custom_party_types", register_custom_party_types),
	("erpnext_mcp.patches.migrate_training_types", migrate_training_types),
	("erpnext_mcp.patches.backfill_completion_signatures", backfill_completion_signatures),
	("erpnext_mcp.patches.migrate_declarative_rules", migrate_declarative_rules),
	("erpnext_mcp.patches.repoint_producer_task_template", repoint_producer_task_template),
	("erpnext_mcp.patches.recompute_2026_dependents_credit", recompute_2026_dependents_credit),
	(
		"erpnext_mcp.patches.fix_literal_newlines_in_instructions",
		fix_literal_newlines_in_instructions,
	),
	("erpnext_mcp.patches.rename_discipline_record", rename_discipline_record),
	(
		"erpnext_mcp.patches.backfill_alert_subject_employee",
		backfill_alert_subject_employee,
	),
	("erpnext_mcp.patches.migrate_incident_tool_switches", migrate_incident_tool_switches),
	("erpnext_mcp.patches.backfill_planting_rootstock", backfill_planting_rootstock),
	("erpnext_mcp.patches.backfill_observation_type", backfill_observation_type),
	("erpnext_mcp.patches.widen_i9_attestation_filters", widen_i9_attestation_filters),
	("erpnext_mcp.patches.backfill_field_varieties", backfill_field_varieties),
)


def patches_txt() -> dict:
	"""`patches.txt` as `{section: [dotted names]}`."""
	import os

	path = os.path.join(
		os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "erpnext_mcp", "patches.txt"
	)
	with open(path) as handle:
		body = handle.read()
	sections, current = {}, None
	for line in body.splitlines():
		line = line.strip()
		if not line or line.startswith("#"):
			continue
		match = re.fullmatch(r"\[(\w+)\]", line)
		if match:
			current = match.group(1)
			sections[current] = []
		elif current:
			sections[current].append(line)
	return sections


class FreshSite(MCPTestCase):
	"""A site with the app's DocTypes and nothing in them."""

	def setUp(self):
		super().setUp()
		STORE.reset()


class PatchesTxt(unittest.TestCase):
	def test_every_patch_in_patches_txt_has_a_test_in_this_module(self):
		"""The list above is the thing keeping this honest."""
		listed = {name for names in patches_txt().values() for name in names}
		self.assertEqual(listed, {name for name, _module in PATCHES})

	def test_every_patch_module_has_an_execute(self):
		for name, module in PATCHES:
			with self.subTest(patch=name):
				self.assertTrue(callable(getattr(module, "execute", None)))

	def test_the_party_type_patch_runs_after_doctypes_are_synced(self):
		"""Load-bearing, not stylistic. A Party Type's name has to be a real
		DocType, and `Family` is one this app ships — so the patch has to run
		after `bench migrate` has created it. In `pre_model_sync` this patch
		would fail exactly the way v0.12.0 did."""
		sections = patches_txt()
		self.assertIn("erpnext_mcp.patches.register_custom_party_types", sections.get("post_model_sync", []))
		self.assertNotIn(
			"erpnext_mcp.patches.register_custom_party_types", sections.get("pre_model_sync", [])
		)


class PatchesOnAFreshSite(FreshSite):
	def test_every_patch_runs_without_raising(self):
		"""The whole point. An exception here is a `bench migrate` that aborts
		for every app on the bench, not just this one."""
		for name, module in PATCHES:
			with self.subTest(patch=name):
				module.execute()

	def test_every_patch_is_a_no_op_the_second_time(self):
		for name, module in PATCHES:
			with self.subTest(patch=name):
				module.execute()
				module.execute()

	def test_running_them_in_patches_txt_order_works(self):
		for _name, module in PATCHES:
			module.execute()
		self.assertTrue(frappe.db.exists("Party Type", "Family"))
		self.assertTrue(settings.tool_enabled("get_company_topology"))

	def test_the_tool_switches_are_seeded(self):
		set_default_tool_switches.execute()
		stored = STORE.singles.get(settings.SETTINGS_DOCTYPE) or {}
		self.assertEqual(stored.get("allow_list_companies"), "1")
		self.assertEqual(stored.get("allow_create_company"), "0")

	def test_the_party_types_are_registered(self):
		register_custom_party_types.execute()
		self.assertTrue(frappe.db.exists("Party Type", "Family"))
		self.assertTrue(frappe.db.exists("Party Type", "Contact"))

	def test_after_install_runs_clean(self):
		install.after_install()
		self.assertTrue(frappe.db.exists("Party Type", "Family"))

	def test_after_migrate_runs_clean(self):
		install.after_migrate()
		self.assertTrue(frappe.db.exists("Party Type", "Family"))

	def test_after_migrate_twice_runs_clean(self):
		install.after_migrate()
		install.after_migrate()
		self.assertEqual(len(STORE.rows("Party Type")), 2)


class TheRegressionItself(FreshSite):
	"""v0.12.0's exact failure, from both directions.

	`Party Type` names itself `field:party_type`, and that field is a **Link to
	DocType**. So a party type called "Family" needs a DocType called "Family",
	and there was none — `_validate_links()` refused the insert and the patch
	took the migration with it. `Contact` registered fine in the same loop,
	because Frappe ships a Contact DocType; that asymmetry is why the failure
	looked like a self-link and was not one.
	"""

	def without_the_family_doctype(self):
		INSTALLED_DOCTYPES.discard("Family")

	def test_inserting_a_party_type_with_no_matching_doctype_really_does_raise(self):
		"""The double has to reproduce the production failure, or every test
		below it is theatre. This is that assertion."""
		self.without_the_family_doctype()
		doc = frappe.new_doc("Party Type")
		doc.party_type = "Family"
		doc.account_type = "Payable"
		with self.assertRaises(frappe.LinkValidationError) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("Family", str(caught.exception))

	def test_the_seeder_skips_it_instead_of_raising(self):
		self.without_the_family_doctype()
		result = company.ensure_party_types()
		self.assertIn("Family", result["skipped"])
		self.assertNotIn("Family", result["created"])

	def test_contact_still_registers_when_family_cannot(self):
		"""One unregistrable party type must not cost the other one. That is the
		difference between a skip and an abort."""
		self.without_the_family_doctype()
		result = company.ensure_party_types()
		self.assertEqual(result["created"], ["Contact"])
		self.assertTrue(frappe.db.exists("Party Type", "Contact"))

	def test_the_patch_survives_it_and_says_why(self):
		self.without_the_family_doctype()
		register_custom_party_types.execute()  # must not raise
		self.assertFalse(frappe.db.exists("Party Type", "Family"))

	def test_the_skip_reason_names_the_doctype_rule(self):
		self.without_the_family_doctype()
		reason = company.ensure_party_types()["skipped"]["Family"]
		self.assertIn("ships with erpnext_mcp", reason)

	def test_a_party_type_pointing_at_a_doctype_nobody_ships_says_so_differently(self):
		"""Two different situations and two different sentences: 'ours, not
		migrated yet' is a retry, 'nobody has this' is a dead end."""
		reason = company.party_type_blocker("Wombat", {"doctype": "Wombat"})
		self.assertIn("no DocType called", reason)
		self.assertIn("Dynamic Link", reason)

	def test_after_migrate_survives_it_too(self):
		self.without_the_family_doctype()
		install.after_migrate()
		self.assertTrue(settings.tool_enabled("get_company_topology"))

	def test_a_migrate_that_cannot_register_a_party_type_still_seeds_the_switches(self):
		"""The consequential half of the v0.12.0 failure. The abort meant
		`after_migrate` never ran, so the release's new tool switches were never
		written — an operator got a traceback AND a half-configured app."""
		self.without_the_family_doctype()
		for _name, module in PATCHES:
			module.execute()
		install.after_migrate()
		stored = STORE.singles.get(settings.SETTINGS_DOCTYPE) or {}
		self.assertEqual(stored.get("allow_list_fields"), "1")
		self.assertEqual(stored.get("allow_set_field_boundary"), "0")


class TheLiteralNewlineFix(FreshSite):
	"""A worker reading instructions on the phone saw `Step 1\\nStep 2` instead
	of two lines, because whatever wrote the template sent the two-character
	escape sequence instead of a real line break. This is the fix, exercised
	against both the template and a task already snapshotted from a bad one."""

	def test_a_literal_backslash_n_in_template_instructions_becomes_a_real_newline(self):
		STORE.seed(
			"Farm Task Template",
			[
				{
					"name": "Bad Template",
					"template_name": "Bad Template",
					"task_type": "Harvest",
					"instructions": "Step 1\\nStep 2",
					"evidence_required": "{}",
				}
			],
		)
		fix_literal_newlines_in_instructions.execute()
		self.assertEqual(
			frappe.db.get_value("Farm Task Template", "Bad Template", "instructions"),
			"Step 1\nStep 2",
		)

	def test_a_literal_backslash_n_in_task_notes_becomes_a_real_newline(self):
		"""A task already snapshotted from a bad template needs fixing too — a
		worker looking at THIS task right now cannot wait for a re-snapshot that
		may never happen."""
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "Bad Task",
					"task_name": "Bad Task",
					"task_type": "Harvest",
					"state": "In-Progress",
					"notes": "Step 1\\nStep 2",
					"evidence_required": "{}",
				}
			],
		)
		fix_literal_newlines_in_instructions.execute()
		self.assertEqual(frappe.db.get_value("Farm Task", "Bad Task", "notes"), "Step 1\nStep 2")

	def test_a_real_newline_is_left_alone(self):
		"""Nothing to fix here — a value with no literal escape sequence must
		not be touched, or every already-correct template gets rewritten for
		no reason on every migrate."""
		STORE.seed(
			"Farm Task Template",
			[
				{
					"name": "Good Template",
					"template_name": "Good Template",
					"task_type": "Harvest",
					"instructions": "Step 1\nStep 2",
					"evidence_required": "{}",
				}
			],
		)
		fix_literal_newlines_in_instructions.execute()
		self.assertEqual(
			frappe.db.get_value("Farm Task Template", "Good Template", "instructions"),
			"Step 1\nStep 2",
		)

	def test_an_empty_instructions_field_is_skipped_without_raising(self):
		STORE.seed(
			"Farm Task Template",
			[
				{
					"name": "Empty Template",
					"template_name": "Empty Template",
					"task_type": "Harvest",
					"instructions": "",
					"evidence_required": "{}",
				}
			],
		)
		fix_literal_newlines_in_instructions.execute()  # must not raise
		self.assertEqual(frappe.db.get_value("Farm Task Template", "Empty Template", "instructions"), "")

	def test_running_it_twice_only_reports_the_row_once(self):
		STORE.seed(
			"Farm Task Template",
			[
				{
					"name": "Bad Template",
					"template_name": "Bad Template",
					"task_type": "Harvest",
					"instructions": "Step 1\\nStep 2",
					"evidence_required": "{}",
				}
			],
		)
		first = fix_literal_newlines_in_instructions.unescape_literal_newlines()
		second = fix_literal_newlines_in_instructions.unescape_literal_newlines()
		self.assertEqual(len(first["fixed"]), 1)
		self.assertEqual(len(second["fixed"]), 0)


class EveryLinkTargetExists(unittest.TestCase):
	"""Every Link and Dynamic Link this app declares points somewhere real.

	The schema-level version of the same check. A DocType JSON whose Link names a
	doctype nothing ships is a field that cannot be filled in on any site, and it
	fails at `bench migrate` or at first use rather than here — which is the
	expensive order to find out.
	"""

	def test_every_link_option_names_a_doctype_that_exists(self):
		for doctype, folder in sorted(APP_DOCTYPES.items()):
			meta = META.get(doctype)
			for field in meta.fields if meta else []:
				if field.get("fieldtype") != "Link":
					continue
				target = str(field.get("options") or "")
				with self.subTest(doctype=doctype, field=field.get("fieldname")):
					self.assertTrue(target, f"{folder}: Link with no options")
					self.assertIn(
						target,
						INSTALLED_DOCTYPES,
						f"{doctype}.{field.get('fieldname')} links to {target!r}, which nothing ships",
					)

	def test_every_dynamic_link_resolves_through_a_field_on_the_same_doctype(self):
		for doctype in sorted(APP_DOCTYPES):
			meta = META.get(doctype)
			fieldnames = {field.get("fieldname") for field in (meta.fields if meta else [])}
			for field in meta.fields if meta else []:
				if field.get("fieldtype") != "Dynamic Link":
					continue
				with self.subTest(doctype=doctype, field=field.get("fieldname")):
					self.assertIn(str(field.get("options") or ""), fieldnames)

	def test_every_party_type_this_app_registers_resolves_to_a_real_doctype(self):
		"""The rule the release broke, asserted directly against the catalogue."""
		for name, spec in sorted(company.CUSTOM_PARTY_TYPES.items()):
			with self.subTest(party_type=name):
				target = spec.get("doctype") or name
				self.assertIn(
					target,
					INSTALLED_DOCTYPES,
					f"party type {name!r} resolves to {target!r}, which is not a DocType",
				)

	def test_the_ones_this_app_ships_are_flagged_as_such(self):
		"""`Family` is ours and `Contact` is Frappe's, and the difference decides
		which skip message an operator gets."""
		self.assertTrue(company.CUSTOM_PARTY_TYPES["Family"]["ships_with_this_app"])
		self.assertFalse(company.CUSTOM_PARTY_TYPES["Contact"]["ships_with_this_app"])
		self.assertIn("Family", APP_DOCTYPES)
		self.assertNotIn("Contact", APP_DOCTYPES)


class DoctypesThisReleaseAdded(unittest.TestCase):
	"""All six new DocTypes are loadable, named, and in this app's module.

	Tim's migrate aborted in `post_model_sync`, which runs after the DocType sync
	— so the tables were already created. This asserts the schema they were
	created from is well-formed, which is the part a failed patch could not have
	affected but which is cheap to be sure of.
	"""

	NEW = ("Field", "Irrigation Zone", "Housing Unit", "Housing Assignment", "Family")

	def test_each_one_is_registered_and_has_fields(self):
		for doctype in self.NEW:
			with self.subTest(doctype=doctype):
				self.assertIn(doctype, APP_DOCTYPES)
				self.assertIn(doctype, INSTALLED_DOCTYPES)
				self.assertTrue(META[doctype].fields)

	def test_each_one_can_be_instantiated(self):
		"""Which means its controller module imports — the failure v0.7.1 shipped,
		where a DocType JSON had no Python module beside it and `bench migrate`
		died with ModuleNotFoundError."""
		for doctype in self.NEW:
			with self.subTest(doctype=doctype):
				self.assertIsNotNone(frappe.new_doc(doctype))

	def test_none_of_them_is_a_single_or_a_child_table(self):
		for doctype in self.NEW:
			with self.subTest(doctype=doctype):
				row = STORE.get_raw("DocType", doctype)
				self.assertEqual(int(row["issingle"]), 0)
				self.assertEqual(int(row["istable"]), 0)

	def test_the_new_registers_are_named_by_a_field_not_by_a_hash(self):
		"""A docname somebody can read is the difference between a register and a
		table of serial numbers. Housing Assignment is the exception on purpose —
		it is a dated event, and `HA-2026-06-00001` is its own sort order."""
		self.assertEqual(META["Family"].autoname, "field:family_member_name")


class TheDoubleActuallyValidates(MCPTestCase):
	"""Guard on the guard.

	If `_validate_links` is ever removed or short-circuited, every link test in
	the suite starts passing for the wrong reason and nothing says so. These fail
	loudly if the validation stops happening.
	"""

	def test_a_link_to_a_missing_record_is_refused(self):
		doc = frappe.new_doc("Family")
		doc.family_member_name = "Nobody's Cousin"
		doc.related_party = "RP-does-not-exist"
		with self.assertRaises(frappe.LinkValidationError):
			doc.insert(ignore_permissions=True)

	def test_a_link_to_a_record_that_exists_is_accepted(self):
		STORE.seed(
			"Related Party",
			[{"name": "RP-0001", "party_name": "Alex Bramwell", "company": "Example Trading Co"}],
		)
		doc = frappe.new_doc("Family")
		doc.family_member_name = "Alex Bramwell"
		doc.related_party = "RP-0001"
		doc.insert(ignore_permissions=True)
		self.assertTrue(STORE.get_raw("Family", "Alex Bramwell"))

	def test_ignore_links_bypasses_it_the_way_frappe_does(self):
		doc = frappe.new_doc("Family")
		doc.family_member_name = "Bypassed"
		doc.related_party = "RP-does-not-exist"
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		self.assertTrue(STORE.get_raw("Family", "Bypassed"))

	def test_an_empty_link_is_not_validated(self):
		doc = frappe.new_doc("Family")
		doc.family_member_name = "No Related Party"
		doc.insert(ignore_permissions=True)
		self.assertTrue(STORE.get_raw("Family", "No Related Party"))


class AFamilyPostingActuallyWorks(MCPTestCase):
	"""End to end, which is what v0.12.0 claimed and could not do.

	The release registered a `Family` party type and had tests asserting a
	Family-party posting "posts cleanly" — against a double that modelled
	`party` as free text. On a real site `party` is a Dynamic Link resolved
	through `party_type`, so the posting would have been refused even if the
	party type had registered. Both halves are checked here: the good posting
	goes through, and a party that is not on the register is refused the way
	Frappe refuses it.
	"""

	def setUp(self):
		super().setUp()
		from .fixtures import MAIN, cash, seed_site, seed_v12, supplies

		seed_site()
		seed_v12()
		self.MAIN, self.cash, self.supplies = MAIN, cash, supplies
		self.configure(enabled=1, allow_create_journal_entry=1)
		company.ensure_party_types()

	def a_journal_entry(self, party_type, party):
		return self.tool(
			"create_journal_entry",
			{
				"company": self.MAIN,
				"posting_date": "2026-03-01",
				"user_remark": f"transfer to {party}",
				"accounts": [
					{
						"account": self.supplies(),
						"debit": 500,
						"party_type": party_type,
						"party": party,
					},
					{"account": self.cash(), "credit": 500},
				],
			},
		)

	def test_a_family_party_posts_cleanly(self):
		from .fixtures import ALEX

		result = self.a_journal_entry("Family", ALEX)
		self.assertFalse(result.get("isError"), result["content"][0]["text"])

	def test_a_contact_party_posts_cleanly(self):
		from .fixtures import ANTONY

		result = self.a_journal_entry("Contact", ANTONY)
		self.assertFalse(result.get("isError"), result["content"][0]["text"])

	def test_a_family_party_who_is_not_on_the_register_is_refused(self):
		"""The Dynamic Link doing its job. Without the register entry there is
		nothing for the posting to point at, and a ledger that accepted it would
		be naming somebody who does not exist."""
		result = self.a_journal_entry("Family", "Somebody Nobody Added")
		self.assertTrue(result.get("isError"))
		self.assertIn("Somebody Nobody Added", result["content"][0]["text"])

	def test_a_party_type_that_is_not_a_doctype_is_refused(self):
		"""The same rule one level up: `party_type` is a Link to DocType, so a
		posting cannot invent a party type any more than it can invent a party."""
		result = self.a_journal_entry("Cousin", "Alex Bramwell")
		self.assertTrue(result.get("isError"))

	def test_the_family_register_is_what_makes_the_difference(self):
		"""Add the person, and the identical posting that was refused goes
		through — which is the whole reason this release ships a Family DocType."""
		refused = self.a_journal_entry("Family", "Marguerite Bramwell")
		self.assertTrue(refused.get("isError"))

		doc = frappe.new_doc("Family")
		doc.family_member_name = "Marguerite Bramwell"
		doc.relationship = "Parent"
		doc.insert(ignore_permissions=True)

		accepted = self.a_journal_entry("Family", "Marguerite Bramwell")
		self.assertFalse(accepted.get("isError"), accepted["content"][0]["text"])


class TheDisciplineRecordRename(FreshSite):
	"""v0.94.0. `Discipline Record` → `Farm Incident Record`, and what it leaves alone.

	THE RENAME IS THE SCHEMA ADMITTING WHAT THE TABLE ALREADY WAS: both voices
	were on the record from the start — `employee_statement` beside
	`manager_signature` — and the only thing it could not express was the WORKER
	being the one who opens it. Once `report_direction` exists, "Discipline
	Record" is the wrong name for half the rows.

	WHAT THE STANDALONE DOUBLE CAN PROVE HERE IS LIMITED AND WORTH STATING. The
	harness has no `rename_doc` and no real table, so these tests exercise the
	patch's CONTROL FLOW — that every branch reports rather than raising, and that
	a site already renamed is a no-op. They do not prove MariaDB renamed the
	table. What they do prove is the property that a `bench migrate` depends on:
	no branch of this patch throws, because an exception here aborts the migration
	for every app on the bench.
	"""

	def test_a_site_with_neither_name_is_a_no_op(self):
		"""The fresh-install case, and the one that runs on most sites."""
		rename_discipline_record.execute()

	def test_it_never_raises_whatever_the_site_looks_like(self):
		"""The property `bench migrate` actually depends on. Run twice, because
		idempotence is the other half of the same requirement."""
		rename_discipline_record.execute()
		rename_discipline_record.execute()

	def test_the_old_and_new_names_are_what_the_code_says_they_are(self):
		"""Cheap, and it catches the typo that would make the patch a silent
		no-op on every site forever."""
		self.assertEqual(rename_discipline_record.OLD, "Discipline Record")
		self.assertEqual(rename_discipline_record.NEW, "Farm Incident Record")
		self.assertEqual(rename_discipline_record.SUPERVISOR, "Supervisor Report")

	def test_the_supervisor_constant_matches_the_doctypes_own(self):
		"""THE BACKFILL WRITES THIS STRING INTO EVERY EXISTING ROW, so a drift
		between the patch's spelling and the controller's would put a value in the
		column that `chain_for` then filters OUT — silently hiding the entire
		existing discipline history of every worker on the farm."""
		from erpnext_mcp.erpnext_mcp.doctype.farm_incident_record.farm_incident_record import (
			SUPERVISOR_REPORT,
		)

		self.assertEqual(rename_discipline_record.SUPERVISOR, SUPERVISOR_REPORT)

	def test_the_tools_are_renamed_and_their_switches_migrate_separately(self):
		"""v0.95.0 renamed the six tools this patch's own docstring warned about.

		`registry.TOOLS` carries the new names now — `create_incident_record` and
		its five siblings — and the old names are gone from it. What carries the
		OLD-switch-stays-ON promise across the rename is a separate patch,
		`migrate_incident_tool_switches`, tested in `TheIncidentToolSwitchMigration`
		below. This test only pins that the rename itself happened.
		"""
		from erpnext_mcp import registry

		for name in (
			"create_incident_record",
			"acknowledge_incident_record",
			"get_incident_record",
			"list_incident_history",
			"get_incident_report",
			"expire_incident_record",
		):
			with self.subTest(tool=name):
				self.assertIn(name, registry.TOOLS)

		for name in (
			"create_discipline_record",
			"acknowledge_discipline_record",
			"get_discipline_record",
			"list_discipline_history",
			"get_discipline_report",
			"expire_discipline_record",
		):
			with self.subTest(old_tool=name):
				self.assertNotIn(name, registry.TOOLS)


class TheIncidentToolSwitchMigration(FreshSite):
	"""v0.95.0. `migrate_incident_tool_switches`: carrying `allow_<old>` to `allow_<new>`.

	THE PROPERTY `rename_discipline_record` DEMANDED OF WHOEVER DID THIS: "a site
	with the old switch ON ends with the new switch ON, and a site with it OFF
	stays OFF." Both directions are tested below, plus the two failure shapes
	that would silently break an operator's configuration: overwriting a value
	they already set on the new field, and running after `set_default_tool_switches`
	has already seeded one.
	"""

	def _stored(self):
		return STORE.singles.get(settings.SETTINGS_DOCTYPE) or {}

	def test_a_site_with_the_old_switch_on_ends_with_the_new_switch_on(self):
		STORE.singles[settings.SETTINGS_DOCTYPE] = {"allow_create_discipline_record": "1"}
		migrate_incident_tool_switches.execute()
		self.assertEqual(self._stored().get("allow_create_incident_record"), "1")

	def test_a_site_with_the_old_switch_off_stays_off(self):
		STORE.singles[settings.SETTINGS_DOCTYPE] = {"allow_expire_discipline_record": "0"}
		migrate_incident_tool_switches.execute()
		self.assertEqual(self._stored().get("allow_expire_incident_record"), "0")

	def test_a_site_that_never_stored_the_old_switch_gets_nothing_carried(self):
		"""A fresh install. There is nothing on the old key, so nothing is
		written to the new one — `set_default_tool_switches` is what seeds it."""
		STORE.singles[settings.SETTINGS_DOCTYPE] = {}
		migrate_incident_tool_switches.execute()
		self.assertNotIn("allow_create_incident_record", self._stored())

	def test_an_explicit_new_value_is_never_overwritten(self):
		"""A site re-running `bench migrate`, or one where an operator already
		flipped the new switch by hand. Either way the old value does not win."""
		STORE.singles[settings.SETTINGS_DOCTYPE] = {
			"allow_create_discipline_record": "1",
			"allow_create_incident_record": "0",
		}
		migrate_incident_tool_switches.execute()
		self.assertEqual(self._stored().get("allow_create_incident_record"), "0")

	def test_it_is_a_no_op_the_second_time(self):
		STORE.singles[settings.SETTINGS_DOCTYPE] = {"allow_create_discipline_record": "1"}
		migrate_incident_tool_switches.execute()
		migrate_incident_tool_switches.execute()
		self.assertEqual(self._stored().get("allow_create_incident_record"), "1")

	def test_it_carries_all_six_pairs(self):
		STORE.singles[settings.SETTINGS_DOCTYPE] = {
			"allow_create_discipline_record": "1",
			"allow_acknowledge_discipline_record": "1",
			"allow_get_discipline_record": "0",
			"allow_list_discipline_history": "0",
			"allow_get_discipline_report": "1",
			"allow_expire_discipline_record": "0",
		}
		migrate_incident_tool_switches.execute()
		stored = self._stored()
		self.assertEqual(stored.get("allow_create_incident_record"), "1")
		self.assertEqual(stored.get("allow_acknowledge_incident_record"), "1")
		self.assertEqual(stored.get("allow_get_incident_record"), "0")
		self.assertEqual(stored.get("allow_list_incident_history"), "0")
		self.assertEqual(stored.get("allow_get_incident_report"), "1")
		self.assertEqual(stored.get("allow_expire_incident_record"), "0")

	def test_running_before_set_default_tool_switches_matters(self):
		"""THE ORDERING `patches.txt` ENCODES. If the seed patch ran first on a
		fresh `allow_create_incident_record`, it would stamp the field's default
		(`0`) as a "stored" value — and this patch's no-clobber rule would then
		refuse to carry the operator's `1` over it, reading the seed as if it
		were the operator's own choice. Running this patch first is what keeps
		that from happening."""
		STORE.singles[settings.SETTINGS_DOCTYPE] = {"allow_create_discipline_record": "1"}
		migrate_incident_tool_switches.execute()
		set_default_tool_switches.execute()
		self.assertEqual(self._stored().get("allow_create_incident_record"), "1")


class BackfillAlertSubjectEmployee(FreshSite):
	"""Naming the person an alert is about, on the alerts a site already has.

	THE COLUMN IS WHAT KEEPS A HANDSET FROM READING A NAME OUT OF PROSE. The
	compliance-to-task picker removes the subject of an alert from the list of
	people it may be handed to — nobody signs off their own gap — and until
	v0.106.0 the only way to identify them was to search the alert's message for
	a candidate's full name. The sweep would fill the column overnight; this
	fills it now, because a control that is off until tomorrow was off when
	somebody used it this afternoon.
	"""

	ALERT = "Compliance Alert"

	def an_alert(self, name, doctype, docname, subject=""):
		row = {
			"name": name,
			"alert_key": name,
			"alert_type": "certification_expiring",
			"severity": "Warning",
			"category": "Certifications",
			"company": "Test Farm LLC",
			"source_doctype": doctype,
			"source_docname": docname,
			"alert_message": f"{docname} is inside its renewal window.",
			"dismissed": 0,
		}
		if subject:
			row["subject_employee"] = subject
		STORE.seed(self.ALERT, [row])
		return name

	def a_person(self, name, full_name, status="Active"):
		STORE.seed(
			"Employee",
			[
				{
					"name": name,
					"employee_name": full_name,
					"company": "Test Farm LLC",
					"status": status,
				}
			],
		)
		return name

	def stored(self, alert):
		return frappe.db.get_value(self.ALERT, alert, "subject_employee")

	def test_an_alert_pointing_at_an_employee_is_about_that_employee(self):
		self.a_person("HR-EMP-0001", "Ana Ruiz")
		alert = self.an_alert("CA-EMP-0001", "Employee", "HR-EMP-0001")
		backfill_alert_subject_employee.execute()
		self.assertEqual(self.stored(alert), "HR-EMP-0001")

	def test_an_alert_about_a_cabin_is_about_nobody_and_that_is_the_answer(self):
		"""EMPTY IS A REAL ANSWER AND IS THE COMMON ONE. A stale water test, an
		uninspected cabin and an overdue filing are about the OPERATION, and a
		patch that invented a person for them would put somebody's name on a gap
		that is not theirs."""
		STORE.seed("Housing Unit", [{"name": "Cabin 1", "company": "Test Farm LLC"}])
		alert = self.an_alert("CA-CABIN-0001", "Housing Unit", "Cabin 1")
		report = backfill_alert_subject_employee.backfill_alert_subject_employee()
		self.assertFalse(self.stored(alert))
		self.assertEqual(report["no_subject"], 1)
		self.assertEqual(report["filled"], 0)

	def test_a_certificate_naming_one_holder_by_name_resolves_to_them(self):
		"""`Certification.holder` IS FREE TEXT — the register holds licences
		issued to the operation as well as to people — so the applicator-licence
		alert, which is the one this whole mechanism exists for, has to come
		through a name match."""
		self.a_person("HR-EMP-0002", "Timothy Polehn")
		STORE.seed(
			"Certification",
			[
				{
					"name": "Applicator License 2025",
					"cert_name": "Applicator License 2025",
					"cert_type": "Applicator License",
					"company": "Test Farm LLC",
					"holder": "Timothy Polehn",
				}
			],
		)
		alert = self.an_alert("CA-CERT-0001", "Certification", "Applicator License 2025")
		backfill_alert_subject_employee.execute()
		self.assertEqual(self.stored(alert), "HR-EMP-0002")

	def test_two_people_of_the_same_name_resolve_to_neither(self):
		"""AMBIGUOUS IS NOT A SUBJECT, IT IS TWO PEOPLE. Picking the first would
		remove the wrong person from the picker — which on this field hides the
		only worker qualified to do the job."""
		self.a_person("HR-EMP-0003", "Juan Garcia")
		self.a_person("HR-EMP-0004", "Juan Garcia")
		STORE.seed(
			"Certification",
			[
				{
					"name": "CDL 2025",
					"cert_name": "CDL 2025",
					"cert_type": "Commercial Driver License",
					"company": "Test Farm LLC",
					"holder": "Juan Garcia",
				}
			],
		)
		alert = self.an_alert("CA-CERT-0002", "Certification", "CDL 2025")
		backfill_alert_subject_employee.execute()
		self.assertFalse(self.stored(alert))

	def test_a_subject_already_stored_is_not_rewritten(self):
		self.a_person("HR-EMP-0005", "Rosa Delgado")
		self.a_person("HR-EMP-0006", "Marco Vega")
		alert = self.an_alert("CA-EMP-0002", "Employee", "HR-EMP-0005", subject="HR-EMP-0006")
		report = backfill_alert_subject_employee.backfill_alert_subject_employee()
		self.assertEqual(self.stored(alert), "HR-EMP-0006")
		self.assertEqual(report["already_set"], 1)
		self.assertEqual(report["filled"], 0)

	def test_it_is_a_no_op_the_second_time(self):
		self.a_person("HR-EMP-0007", "Ana Ruiz")
		alert = self.an_alert("CA-EMP-0003", "Employee", "HR-EMP-0007")
		backfill_alert_subject_employee.execute()
		second = backfill_alert_subject_employee.backfill_alert_subject_employee()
		self.assertEqual(self.stored(alert), "HR-EMP-0007")
		self.assertEqual(second["filled"], 0)
		self.assertEqual(second["already_set"], 1)


class BackfillPlantingRootstock(FreshSite):
	"""Carrying the catalogue's rootstock down onto the plantings that record none.

	THE SUBSTANTIVE TESTS ARE IN `test_crop_variety_overlay.py`, with the rest of
	the v0.114.0 crop work: that it only fills a blank, never rewrites a value
	typed against a block, matches on crop as well as variety, and is safe to run
	twice. What belongs HERE is the question this module exists for — whether the
	patch survives a site that has never seen this app — because an exception let
	out of a patch stops every other app's migration too, and the site that finds
	it is somebody's upgrade rather than a test.
	"""

	def test_it_survives_a_site_with_no_crop_register_at_all(self):
		backfill_planting_rootstock.execute()

	def test_it_reports_the_absence_rather_than_raising(self):
		report = backfill_planting_rootstock.backfill_planting_rootstock()
		self.assertTrue(report["skipped"])
		self.assertEqual(report["catalogue"], 0)

	def test_it_survives_a_catalogue_that_names_no_rootstock(self):
		"""The commonest real site: crops recorded, rootstocks never filled in."""
		STORE.seed(
			"Crop",
			[
				{
					"name": "Sweet Cherry",
					"crop_name": "Sweet Cherry",
					"varieties": [
						{
							"name": "cv-1",
							"parent": "Sweet Cherry",
							"parenttype": "Crop",
							"parentfield": "varieties",
							"variety_name": "Bing",
							"rootstock": "",
						}
					],
				}
			],
		)
		backfill_planting_rootstock.execute()


class BackfillFieldVarieties(FreshSite):
	"""Copying each block's single variety into its own Field Variety row.

	Only the survival question belongs here — see the module docstring. What
	belongs elsewhere: whether `create_field`/`update_field` write a correct
	`varieties` argument, and whether `_describe_field` reports it back. This is
	just "does a `bench migrate` on a real, already-populated site survive
	writing the first row into a table that never had one."
	"""

	def a_parcel(self, name):
		STORE.seed("Parcel", [{"name": name, "parcel_name": name}])

	def a_field(self, name, field_name, parcel, **extra):
		STORE.seed(
			"Field",
			[{"name": name, "field_name": field_name, "parcel": parcel, **extra}],
		)

	def a_crop(self, name, varieties=()):
		STORE.seed(
			"Crop",
			[
				{
					"name": name,
					"crop_name": name,
					"varieties": [
						{
							"name": f"cv-{name}-{index}",
							"parent": name,
							"parenttype": "Crop",
							"parentfield": "varieties",
							"variety_name": variety_name,
						}
						for index, variety_name in enumerate(varieties, start=1)
					],
				}
			],
		)

	def test_it_survives_a_site_with_no_field_variety_doctype_at_all(self):
		backfill_field_varieties.execute()

	def test_it_reports_the_absence_rather_than_raising(self):
		report = backfill_field_varieties.backfill_field_varieties()
		self.assertTrue(report["skipped"])
		self.assertEqual(report["scanned"], 0)

	def test_it_copies_a_blocks_single_variety_into_its_own_row_at_100_percent(self):
		"""No Crop record names this block's crop, so there is no catalogue to
		check the spelling against — the common case on a farm that has not built
		its crop register yet, and the one the whole `variety` column already
		worked under."""
		self.a_parcel("Test Parcel")
		self.a_field(
			"Test Parcel - Block 3", "Block 3", "Test Parcel", variety="Black Pearl", planting_year=2019
		)
		report = backfill_field_varieties.backfill_field_varieties()
		self.assertEqual(report["filled"], 1)
		rows = frappe.db.get_all(
			"Field Variety",
			filters={"parent": "Test Parcel - Block 3"},
			fields=["variety", "percentage", "planting_year"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["variety"], "Black Pearl")
		self.assertEqual(rows[0]["percentage"], 100.0)
		self.assertEqual(rows[0]["planting_year"], 2019)

	def test_it_writes_the_catalogues_own_spelling_when_the_crop_exists(self):
		self.a_crop("Cherry", ["Black Pearl", "Bing"])
		self.a_parcel("Test Parcel")
		self.a_field("Test Parcel - Block 3", "Block 3", "Test Parcel", crop="Cherry", variety="black pearl")
		report = backfill_field_varieties.backfill_field_varieties()
		self.assertEqual(report["filled"], 1)
		rows = frappe.db.get_all(
			"Field Variety", filters={"parent": "Test Parcel - Block 3"}, fields=["variety", "percentage"]
		)
		self.assertEqual(rows[0]["variety"], "Black Pearl")
		self.assertEqual(rows[0]["percentage"], 100.0)

	def test_a_variety_not_in_the_crops_catalogue_is_reported_and_not_raised(self):
		"""The catalogue exists but does not list this spelling — a real gap, and
		one this patch has no business inventing an answer for."""
		self.a_crop("Cherry", ["Bing"])
		self.a_parcel("Test Parcel")
		self.a_field("Test Parcel - Block 3", "Block 3", "Test Parcel", crop="Cherry", variety="Black Pearl")
		report = backfill_field_varieties.backfill_field_varieties()
		self.assertEqual(report["filled"], 0)
		self.assertEqual(report["not_in_catalogue"], 1)
		self.assertEqual(frappe.db.get_all("Field Variety", filters={"parent": "Test Parcel - Block 3"}), [])

	def test_it_is_a_no_op_the_second_time(self):
		self.a_parcel("Test Parcel")
		self.a_field("Test Parcel - Block 3", "Block 3", "Test Parcel", variety="Black Pearl")
		backfill_field_varieties.execute()
		second = backfill_field_varieties.backfill_field_varieties()
		self.assertEqual(second["filled"], 0)
		self.assertEqual(second["already_set"], 1)
		self.assertEqual(
			len(frappe.db.get_all("Field Variety", filters={"parent": "Test Parcel - Block 3"})), 1
		)

	def test_it_leaves_a_block_that_already_has_a_variety_row_alone(self):
		self.a_parcel("Test Parcel")
		self.a_field(
			"Test Parcel - Block 3",
			"Block 3",
			"Test Parcel",
			variety="Black Pearl",
			varieties=[
				{
					"name": "fv-1",
					"parent": "Test Parcel - Block 3",
					"parenttype": "Field",
					"parentfield": "varieties",
					"variety": "Burgundy Pearl",
				}
			],
		)
		report = backfill_field_varieties.backfill_field_varieties()
		self.assertEqual(report["filled"], 0)
		self.assertEqual(report["already_set"], 1)
		rows = frappe.db.get_all(
			"Field Variety", filters={"parent": "Test Parcel - Block 3"}, fields=["variety"]
		)
		self.assertEqual([row["variety"] for row in rows], ["Burgundy Pearl"])

	def test_a_block_with_no_variety_is_not_scanned(self):
		self.a_parcel("Test Parcel")
		self.a_field("Test Parcel - Block 4", "Block 4", "Test Parcel")
		report = backfill_field_varieties.backfill_field_varieties()
		self.assertEqual(report["scanned"], 0)
		self.assertEqual(report["filled"], 0)
