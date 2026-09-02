# SPDX-License-Identifier: MIT
"""The six mobile roles — v0.17.0 Feature A.

FOUR CLAIMS, AND EVERY CLASS HERE IS ONE OF THEM.

1. **THE INSTALLER IS SAFE TO RUN ON EVERY MIGRATE, FOREVER.** `TheInstaller`.
   It creates what is missing, leaves alone what exists — including a permission
   an operator has since edited — and never raises, because it runs inside
   `bench migrate` where an exception aborts the migration for the whole bench.

2. **IT MIRRORS THE STANDARD PERMISSIONS BEFORE IT WRITES A CUSTOM ONE, AND
   THAT IS THE MOST IMPORTANT TEST IN THIS FILE.** `TheCustomDocPermTrap`.
   Frappe discards every standard DocPerm on a doctype the moment ONE Custom
   DocPerm exists for it — for every role on the site, not just the one the row
   was written for. Without the mirror, granting Field Worker read on Farm Task
   would revoke System Manager from Farm Task, silently, during a migration.
   `test_the_mirror_is_what_keeps_system_manager` is that failure, asserted.

3. **IT REFUSES TO WRITE A PERMISSION ONTO A DOCTYPE THIS APP DOES NOT OWN.**
   Same class. Not because the write would fail — it would succeed, which is the
   problem. `Employee` belongs to Frappe HR, and a Custom DocPerm on it would
   take HR Manager, HR User and System Manager off the Employee register.

4. **THE ROLES SAY WHAT THIS RELEASE CLAIMS THEY SAY.** `WhatEachRoleMay`. The
   headline pair from the spec is asserted in both directions — a Field Worker
   CANNOT read a Compliance Policy and a Compliance Officer CAN — along with the
   two separations that are easy to mistake for oversights: a Compliance Officer
   cannot dispatch, and a Family Member cannot see the operator's task board.

WHY NO COMPANY NAME APPEARS ANYWHERE IN `roles.py`, tested in `TheSplit`: the
role is the job description and the User Permission is the entity. Bolting
entities into roles would have produced a new role per LLC and made this app
specific to one install.
"""

import contextlib
import io

import frappe

from erpnext_mcp import install, roles

from .fixtures import MAIN, OTHER, SeededTestCase
from .harness import STORE

CUSTOM = roles.CUSTOM_DOCPERM


def custom_perms(doctype: str) -> dict:
	"""{role: row} for every Custom DocPerm on one doctype."""
	return {str(row.get("role")): row for row in STORE.rows(CUSTOM) if str(row.get("parent")) == doctype}


class RolesTestCase(SeededTestCase):
	def install(self) -> dict:
		return roles.install_roles()


class TheInstaller(RolesTestCase):
	def test_it_creates_all_six_roles(self):
		report = self.install()
		self.assertEqual(sorted(report["created_roles"]), sorted(roles.ROLE_NAMES))
		for name in roles.ROLE_NAMES:
			with self.subTest(role=name):
				self.assertTrue(frappe.db.exists("Role", name))

	def test_a_second_run_creates_nothing_and_says_so(self):
		"""It runs on EVERY migrate. A second run that added anything would be a
		second run that eventually added everything twice."""
		self.install()
		before = len(STORE.rows(CUSTOM))
		report = self.install()
		self.assertEqual(report["created_roles"], [])
		self.assertEqual(report["created_permissions"], [])
		self.assertEqual(sorted(report["existing_roles"]), sorted(roles.ROLE_NAMES))
		self.assertEqual(len(STORE.rows(CUSTOM)), before)

	def test_it_does_not_argue_with_an_operator_who_edited_a_permission(self):
		"""If somebody decided their Foremen should not create Farm Tasks, the next
		migrate leaves that decision alone. Only behaviour that is safe to run
		forever."""
		self.install()
		row = custom_perms("Farm Task")["Foreman"]
		frappe.db.set_value(CUSTOM, row["name"], "create", 0)
		self.install()
		self.assertEqual(int(custom_perms("Farm Task")["Foreman"]["create"]), 0)

	def test_the_two_phone_roles_have_no_desk_access(self):
		"""A role that cannot open /app is one fewer thing to explain to somebody
		holding a phone in an orchard."""
		self.install()
		self.assertEqual(int(frappe.db.get_value("Role", "Field Worker", "desk_access") or 0), 0)
		self.assertEqual(int(frappe.db.get_value("Role", "Foreman", "desk_access") or 0), 1)

	def test_it_never_raises_when_the_role_table_is_missing(self):
		"""It runs inside `bench migrate`, where an exception aborts the migration
		for the whole bench."""
		from .harness import INSTALLED_DOCTYPES

		# A site mid-migration, before core tables are readable.
		INSTALLED_DOCTYPES.discard("Role")
		try:
			report = roles.install_roles()
		finally:
			INSTALLED_DOCTYPES.add("Role")
		self.assertEqual(report["created_roles"], [])
		self.assertTrue(report["failed"])

	def test_a_doctype_this_site_has_not_migrated_yet_is_skipped_not_failed(self):
		"""A site upgrading from v0.15.0 has no Farm Task until the same migrate
		creates it. That is a sequencing fact, not a failure."""
		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Farm Task")
		try:
			report = roles.install_roles()
		finally:
			INSTALLED_DOCTYPES.add("Farm Task")
		self.assertIn("Farm Task", report["skipped_doctypes"])
		self.assertNotIn("Farm Task", [str(entry["name"]) for entry in report["failed"]])

	def test_after_migrate_installs_them_and_prints_nothing(self):
		"""The hook path, end to end. A clean migrate says nothing at all."""
		output = io.StringIO()
		with contextlib.redirect_stdout(output):
			install.after_migrate()
		for name in roles.ROLE_NAMES:
			with self.subTest(role=name):
				self.assertTrue(frappe.db.exists("Role", name))
		self.assertNotIn("could not build", output.getvalue())


class TheCustomDocPermTrap(RolesTestCase):
	"""THE CLASS THAT MATTERS. See the module docstring.

	`frappe.permissions.get_all_perms` discards every standard DocPerm on a
	doctype as soon as one Custom DocPerm exists for it. Both halves of this
	app's answer — mirror first, and never touch a foreign doctype — are asserted
	here, and each one is a live site's permissions if it is wrong.
	"""

	def test_the_mirror_is_what_keeps_system_manager(self):
		"""Farm Task ships a System Manager DocPerm. After the installer writes
		Custom DocPerms for the mobile roles, Frappe ignores the standard set — so
		System Manager has to be IN the custom set, or it has just been revoked."""
		standard = {
			str(row["role"])
			for row in frappe.db.get_all("DocPerm", filters={"parent": "Farm Task"}, fields=["role"])
		}
		self.assertIn("System Manager", standard, "fixture precondition")

		self.install()
		after = custom_perms("Farm Task")
		self.assertIn(
			"System Manager",
			after,
			"the standard DocPerms were not mirrored, so Frappe now ignores them — System "
			"Manager has just lost access to Farm Task on every site that migrated",
		)
		self.assertIn("Field Worker", after)

	def test_the_mirror_preserves_what_each_standard_permission_said(self):
		self.install()
		mirrored = custom_perms("Farm Task")["System Manager"]
		for flag in ("read", "write", "create", "delete"):
			with self.subTest(flag=flag):
				self.assertEqual(int(mirrored[flag]), 1)

	def test_it_mirrors_once_and_not_once_per_role(self):
		"""The existence check is on the DOCTYPE, not on the role. Six roles must
		not produce six copies of System Manager."""
		self.install()
		copies = [
			row
			for row in STORE.rows(CUSTOM)
			if str(row.get("parent")) == "Farm Task" and str(row.get("role")) == "System Manager"
		]
		self.assertEqual(len(copies), 1)

	def test_a_standard_row_for_a_role_this_site_lacks_does_not_abort_the_mirror(self):
		"""v0.59.3. I-9 Form ships a DocPerm for `HR Manager`, which comes from
		`hrms` and is absent on a bench that never installed it — this suite's
		site is one of those, deliberately.

		`Custom DocPerm.role` is a Link, so copying that row raises. A mirror that
		raises HALFWAY is worse than no mirror at all: the rows already written are
		enough to make Frappe discard every standard DocPerm the doctype had, so
		System Manager would lose the I-9 register during a migration. The
		unresolvable row is skipped instead — it grants nobody anything, because
		nobody can hold a role the site does not have.
		"""
		self.assertFalse(frappe.db.exists("Role", "HR Manager"), "fixture precondition")
		standard = {
			str(row["role"])
			for row in frappe.db.get_all("DocPerm", filters={"parent": "I-9 Form"}, fields=["role"])
		}
		self.assertIn("HR Manager", standard, "fixture precondition")

		report = self.install()
		self.assertEqual([], [row for row in report["failed"] if "I-9 Form" in str(row.get("name"))])
		after = custom_perms("I-9 Form")
		self.assertIn("System Manager", after)
		self.assertIn("Farm Manager", after)
		self.assertNotIn("HR Manager", after)

	def test_a_doctype_an_operator_already_customised_is_not_mirrored_again(self):
		"""Then the mirror has already happened, in the Desk, by Frappe itself."""
		frappe.get_doc(
			{"doctype": CUSTOM, "parent": "Farm Task", "role": "Accounts Manager", "read": 1}
		).insert()
		report = self.install()
		self.assertNotIn("Farm Task", report["mirrored_doctypes"])
		self.assertNotIn("System Manager", custom_perms("Farm Task"))

	def test_a_permission_on_a_foreign_doctype_is_refused_and_writes_nothing(self):
		"""Employee belongs to Frappe HR. A Custom DocPerm on it would take HR
		Manager, HR User and System Manager off the Employee register — silently,
		during somebody's migration."""
		report = {
			"created_permissions": [],
			"existing_permissions": [],
			"mirrored_doctypes": [],
			"skipped_doctypes": [],
			"failed": [],
		}
		roles._ensure_permission("Field Worker", "Employee", roles.READ, report)
		self.assertEqual(report["created_permissions"], [])
		self.assertEqual(custom_perms("Employee"), {})
		reason = report["failed"][0]["reason"]
		self.assertIn("belongs to another app", reason)
		self.assertIn("EVERY standard permission", reason)
		self.assertIn("companion_roles", reason)

	def test_a_permission_on_a_child_table_is_refused(self):
		"""A child row's access follows its parent's in Frappe, so a DocPerm here
		is read by nothing — while STILL turning off every standard permission the
		doctype has. A permission that does nothing is not merely useless; it is a
		way to break something while appearing to grant something."""
		report = {
			"created_permissions": [],
			"existing_permissions": [],
			"mirrored_doctypes": [],
			"skipped_doctypes": [],
			"failed": [],
		}
		roles._ensure_permission("Foreman", "Certification Renewal", roles.READ, report)
		self.assertEqual(report["created_permissions"], [])
		self.assertEqual(custom_perms("Certification Renewal"), {})
		self.assertIn("CHILD TABLE", report["failed"][0]["reason"])
		self.assertIn("Grant the parent instead", report["failed"][0]["reason"])

	def test_no_role_targets_a_child_table(self):
		"""The other direction, over the shipped specs. `Certification Renewal`
		and `Audit Corrective Action` are the two that look like registers and are
		not."""
		for doctype in sorted(roles.permission_targets()):
			with self.subTest(doctype=doctype):
				self.assertFalse(
					int(frappe.db.get_value("DocType", doctype, "istable") or 0),
					f"{doctype} is a child table; grant its parent instead",
				)

	def test_no_role_targets_a_doctype_this_app_does_not_own(self):
		"""The other direction, over the shipped specs rather than one call. A
		target added to a role in a future release fails here before it can reach
		a site."""
		for doctype in sorted(roles.permission_targets()):
			with self.subTest(doctype=doctype):
				self.assertEqual(
					frappe.db.get_value("DocType", doctype, "module"),
					roles.OWNED_MODULE,
					f"{doctype} is not this app's, so a Custom DocPerm on it would discard "
					"every standard permission it has, for every role on the site",
				)

	def test_the_installer_touches_no_foreign_doctype_at_all(self):
		self.install()
		for row in STORE.rows(CUSTOM):
			with self.subTest(doctype=row.get("parent")):
				self.assertEqual(frappe.db.get_value("DocType", row["parent"], "module"), roles.OWNED_MODULE)


class WhatEachRoleMay(RolesTestCase):
	def setUp(self):
		super().setUp()
		self.install()

	def may(self, role: str, doctype: str, flag: str = "read") -> bool:
		row = custom_perms(doctype).get(role)
		return bool(row and int(row.get(flag) or 0))

	def test_a_field_worker_cannot_read_a_compliance_policy(self):
		"""THE HEADLINE PAIR, half one. The SOP library names procedures, versions
		and effective dates an operation's certification hangs on. A worker who
		needs one gets it in the task's notes, put there by whoever raised the job."""
		self.assertFalse(self.may("Field Worker", "Compliance Policy"))

	def test_a_compliance_officer_can(self):
		"""Half two. Asserting only half of a separation proves nothing — a role
		that can read NOTHING would pass the first test."""
		self.assertTrue(self.may("Compliance Officer", "Compliance Policy"))
		self.assertTrue(self.may("Compliance Officer", "Compliance Policy", "write"))

	def test_a_field_worker_reads_the_task_and_writes_only_the_assignment(self):
		"""A worker moves their own record through its states and files what they
		found. They do not rewrite the job — its urgency, its evidence contract, or
		which compliance record it produces."""
		self.assertTrue(self.may("Field Worker", "Farm Task", "read"))
		self.assertFalse(self.may("Field Worker", "Farm Task", "write"))
		self.assertTrue(self.may("Field Worker", "Farm Task Assignment", "write"))
		self.assertTrue(self.may("Field Worker", "Farm Task Assignment", "create"))

	def test_a_field_worker_cannot_read_the_compliance_calendar(self):
		self.assertFalse(self.may("Field Worker", "Compliance Alert"))

	def test_a_compliance_officer_cannot_dispatch(self):
		"""NOT AN OVERSIGHT. The person who decides a walk is required and the
		person who decides who walks it must not be one account — a role that did
		both could raise a task, assign it to itself and close it, which is the
		first thing an auditor looks for."""
		self.assertTrue(self.may("Compliance Officer", "Farm Task", "read"))
		self.assertFalse(self.may("Compliance Officer", "Farm Task", "write"))
		self.assertFalse(self.may("Compliance Officer", "Farm Task", "create"))

	def test_a_foreman_runs_the_board_and_reads_the_registers(self):
		self.assertTrue(self.may("Foreman", "Farm Task", "create"))
		self.assertTrue(self.may("Foreman", "Farm Task Assignment", "write"))
		self.assertTrue(self.may("Foreman", "Compliance Alert", "write"))
		self.assertTrue(self.may("Foreman", "Compliance Policy", "read"))
		self.assertFalse(self.may("Foreman", "Compliance Policy", "write"))

	def test_no_role_anywhere_touches_accounting(self):
		"""'Cannot touch accounting' is a claim in the spec for three of the six.
		It is true of all six, and it is true by construction: no accounting
		doctype is named in any permission list."""
		targets = roles.permission_targets()
		for doctype in (
			"Account",
			"Journal Entry",
			"GL Entry",
			"Bank Transaction",
			"Cap Table Entry",
		):
			with self.subTest(doctype=doctype):
				if doctype == "Cap Table Entry":
					# Family Member reads it. Nobody writes it, and no operational
					# role sees it at all.
					self.assertFalse(self.may("Farm Manager", doctype))
					self.assertFalse(self.may("Foreman", doctype))
					continue
				self.assertNotIn(doctype, targets)

	def test_a_family_member_cannot_see_the_operators_task_board(self):
		"""Constancy Farms' day-to-day is not the holding company's business."""
		self.assertFalse(self.may("Family Member", "Farm Task"))
		self.assertFalse(self.may("Family Member", "Farm Task Assignment"))
		self.assertTrue(self.may("Family Member", "Cap Table Entry"))
		self.assertTrue(self.may("Family Member", "Governance Document", "write"))
		self.assertFalse(self.may("Family Member", "Cap Table Entry", "write"))

	def test_an_advisor_writes_nothing_anywhere(self):
		"""The narrowest role in the app, asserted over every doctype it names."""
		for doctype, flags in roles.BY_NAME["Advisor"].permissions:
			with self.subTest(doctype=doctype):
				self.assertTrue(flags["read"])
				for flag in ("write", "create", "delete"):
					self.assertFalse(flags[flag])

	def test_an_advisor_sees_only_paper(self):
		self.assertTrue(self.may("Advisor", "Governance Document"))
		self.assertTrue(self.may("Advisor", "Regulatory Filing"))
		self.assertFalse(self.may("Advisor", "Farm Task"))
		self.assertFalse(self.may("Advisor", "Compliance Alert"))
		self.assertFalse(self.may("Advisor", "Cap Table Entry"))

	def test_a_farm_manager_runs_operations_and_the_ground_under_them(self):
		for doctype in ("Farm Task", "Parcel", "Field", "Housing Unit", "Lease", "Certification"):
			with self.subTest(doctype=doctype):
				self.assertTrue(self.may("Farm Manager", doctype, "write"))
		self.assertFalse(self.may("Farm Manager", "Governance Document", "write"))

	def test_a_farm_manager_may_write_the_i9_because_section_2_is_theirs(self):
		"""v0.59.3. THE EMPLOYER'S HALF OF THE FORM NEEDS AN EMPLOYER WHO CAN WRITE IT.

		8 CFR § 274a.2(b)(1)(ii) puts Section 2 on the employer or its authorised
		representative, and `signatures._require_write` gates every signature on
		Frappe's `write` — so a role that supervises hiring and held only `read`
		was refused at the pad with a sentence about a column.

		Asserted in both directions, because a grant asserted one way proves
		nothing about its edges: read and write yes, create and delete no. The
		form begins with the worker's Section 1 and ends on the retention
		schedule, and neither of those is a manager's decision.
		"""
		self.assertTrue(self.may("Farm Manager", "I-9 Form", "read"))
		self.assertTrue(self.may("Farm Manager", "I-9 Form", "write"))
		self.assertFalse(self.may("Farm Manager", "I-9 Form", "create"))
		self.assertFalse(self.may("Farm Manager", "I-9 Form", "delete"))

	def test_the_i9_grant_reaches_nobody_it_was_not_written_for(self):
		"""The other five roles are where they were. A permission added for one
		role that quietly appears on another is the shape of this file's worst
		failure — and `HR Manager` keeping its standard row is the mirror doing
		its job, since the first Custom DocPerm on a doctype discards every
		standard permission it had."""
		for role in ("Field Worker", "Foreman", "Compliance Officer", "Family Member", "Advisor"):
			with self.subTest(role=role):
				self.assertFalse(self.may(role, "I-9 Form"))
		# And System Manager, which the doctype ships with, is still there —
		# without the mirror the Farm Manager row would have taken it off.
		self.assertTrue(self.may("System Manager", "I-9 Form", "write"))
		self.assertTrue(self.may("System Manager", "I-9 Form", "delete"))


class TheSplit(RolesTestCase):
	"""The role says what KIND of work; the User Permission says WHOSE."""

	def test_no_company_name_appears_in_any_role_DEFINITION(self):
		"""A role per LLC is what this split exists to avoid, and it is also what
		would make this app specific to one install.

		Asserted over the SPECS, not over the file: the module docstring names two
		of Tim's entities to explain why the split exists, which is exactly the
		right place for a name and exactly the wrong place to look for one.
		"""
		names = (MAIN, OTHER, "Constancy Farms", "Highland", "Orchard Meadow", "LLC")
		for spec in roles.ROLE_SPECS:
			text = " ".join([spec.name, spec.description, spec.summary, *spec.cannot, *spec.companion_roles])
			for company in names:
				with self.subTest(role=spec.name, company=company):
					self.assertNotIn(company, text)

	def test_no_role_is_granted_a_permission_on_Company_itself(self):
		"""The entity is scoped BY a User Permission, never granted AS a doctype
		permission. A role that could read Company would be a role whose scoping
		lived in two places."""
		self.assertNotIn("Company", roles.permission_targets())

	def test_companies_for_reads_the_user_permission_rows_default_first(self):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": "worker@example.test",
				"first_name": "Wanda",
				"full_name": "Wanda Worker",
				"enabled": 1,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": "worker@example.test",
				"allow": "Company",
				"for_value": OTHER,
				"apply_to_all_doctypes": 1,
				"is_default": 0,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": "worker@example.test",
				"allow": "Company",
				"for_value": MAIN,
				"apply_to_all_doctypes": 1,
				"is_default": 1,
			}
		).insert()
		self.assertEqual(roles.companies_for("worker@example.test"), [MAIN, OTHER])
		self.assertEqual(roles.default_company_for("worker@example.test"), MAIN)

	def test_an_empty_permission_list_is_reported_as_UNRESTRICTED_not_as_none(self):
		"""Frappe's rule, and the one place the safe-looking reading is the
		dangerous one. A user with no Company User Permission sees EVERY company."""
		note = roles.entity_access_note([])
		self.assertIn("UNRESTRICTED", note)
		self.assertIn("every entity", note)
		self.assertNotIn("no access", note)

	def test_a_populated_list_says_what_is_invisible(self):
		note = roles.entity_access_note([MAIN])
		self.assertIn(MAIN, note)
		self.assertIn("invisible", note)


class TheCatalogue(RolesTestCase):
	def test_every_role_says_what_it_cannot_do(self):
		"""A permission list says what somebody may do. `cannot` is what a person
		staffing the role actually needs, and it is what the mobile account screen
		shows."""
		for spec in roles.ROLE_SPECS:
			with self.subTest(role=spec.name):
				self.assertTrue(spec.cannot)
				self.assertTrue(spec.summary)
				self.assertTrue(spec.description)

	def test_describe_role_reports_whether_it_is_actually_installed(self):
		spec = roles.BY_NAME["Foreman"]
		self.assertFalse(roles.describe_role(spec)["installed"])
		self.install()
		described = roles.describe_role(spec)
		self.assertTrue(described["installed"])
		self.assertIn("Farm Task", [perm["doctype"] for perm in described["permissions"]])

	def test_roles_of_reports_only_this_apps_roles(self):
		"""A Farm Manager who is also a System Manager is a fact about the site's
		own configuration; this app answers questions about this app's roles."""
		self.install()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "foreman@example.test",
				"first_name": "Fran",
				"full_name": "Fran Foreman",
				"enabled": 1,
			}
		).insert()
		user.append("roles", {"role": "Foreman"})
		user.append("roles", {"role": "System Manager"})
		user.save()
		self.assertEqual(roles.roles_of("foreman@example.test"), ["Foreman"])
		self.assertEqual(roles.all_roles_of("foreman@example.test"), ["Foreman", "System Manager"])

	def test_spec_for_is_exact_and_does_not_guess(self):
		self.assertIsNotNone(roles.spec_for("Field Worker"))
		self.assertIsNone(roles.spec_for("field worker"))
		self.assertIsNone(roles.spec_for("Worker"))


#: A company whose LEGAL NAME CONTAINS A COMMA, which is the ordinary case on a
#: farm and the one every parser in this app used to get wrong.
COMMA_CO = "Orchard Meadow, LLC"
#: Its near-neighbour. Seeded so `_names_in` has to choose between an exact match
#: on the whole line and a split that would also resolve — the ambiguity that
#: makes "check the whole line first" load-bearing rather than incidental.
PREFIX_CO = "Orchard Meadow"


class TheCommaInACompanysName(RolesTestCase):
	"""S6. "Orchard Meadow, LLC" is ONE entity, and was read as two.

	Both ends of `Mobile Access Grant.entity_access` split on commas
	unconditionally — the doctype's `_tidy_lines` on the way in, and
	`tools/mobile._resolve_entities` on the way in from a request body. A farm
	whose entities are LLCs got a name and a suffix, neither of which is a
	Company, so `create_mobile_user` refused a spelling that was correct and
	`list_mobile_users` reported two grants of access that scope nothing.

	The fix is not "stop splitting on commas" — a line that really is two
	companies still has to split, because that is what somebody typing into a
	Small Text field means. It is "split, then check", which is what every test
	below is about.
	"""

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Company",
			[
				{
					"name": COMMA_CO,
					"abbr": "OML",
					"default_currency": "USD",
					"country": "United States",
					"is_group": 0,
				},
				{
					"name": PREFIX_CO,
					"abbr": "OMD",
					"default_currency": "USD",
					"country": "United States",
					"is_group": 0,
				},
			],
		)

	def test_a_company_name_containing_a_comma_stays_one_entity(self):
		self.assertEqual(roles.parse_entity_access(COMMA_CO), [COMMA_CO])

	def test_two_companies_on_one_line_still_split(self):
		"""The case the old `replace(",", "\\n")` existed for. It still works."""
		self.assertEqual(roles.parse_entity_access(f"{MAIN}, {OTHER}"), [MAIN, OTHER])

	def test_the_whole_line_wins_over_a_split_that_would_also_resolve(self):
		"""Both readings resolve here, and only one of them is what was typed.

		`Orchard Meadow, LLC` splits into `Orchard Meadow` — a real company on
		this fixture — and `LLC`, which is not; so the all-pieces-known test
		fails and the whole line is kept. Seeding the near-neighbour is what
		makes this a real choice rather than a vacuous one.
		"""
		self.assertEqual(roles.parse_entity_access(COMMA_CO), [COMMA_CO])
		self.assertEqual(roles.parse_entity_access(PREFIX_CO), [PREFIX_CO])

	def test_newline_is_always_a_separator(self):
		self.assertEqual(roles.parse_entity_access(f"{COMMA_CO}\n{MAIN}"), [COMMA_CO, MAIN])

	def test_a_quoted_name_splits_at_the_quotes_and_needs_no_lookup(self):
		"""Somebody who quoted a name has said where it ends. No register needed."""
		self.assertEqual(
			roles.split_entity_names('"Orchard Meadow, LLC", "Highland Orchards, Inc."'),
			["Orchard Meadow, LLC", "Highland Orchards, Inc."],
		)

	def test_with_no_predicate_nothing_is_comma_split(self):
		"""The safe direction. An unsplit line fails loudly at `resolve_company`;
		a wrongly-split one silently records two entities that scope nothing."""
		self.assertEqual(roles.split_entity_names("Anything, At All"), ["Anything, At All"])

	def test_a_list_is_taken_element_by_element_and_never_re_split(self):
		"""A caller that built a list has already said where each name ends."""
		self.assertEqual(roles.split_entity_names([COMMA_CO, MAIN]), [COMMA_CO, MAIN])

	def test_blanks_and_duplicates_go_and_order_is_kept(self):
		self.assertEqual(
			roles.parse_entity_access(f"{MAIN}\n\n{COMMA_CO}\n{MAIN}\n   "),
			[MAIN, COMMA_CO],
		)

	def test_an_unknown_name_with_a_comma_is_reported_whole(self):
		"""So the refusal names what somebody typed, not a fragment of it."""
		self.assertEqual(roles.parse_entity_access("Nowhere Farms, LLC"), ["Nowhere Farms, LLC"])

	def test_the_column_round_trips(self):
		stored = roles.tidy_entity_access(f"{COMMA_CO}, {MAIN}")
		# Two companies were named; the comma between them separates because both
		# halves resolve — but the LLC's own comma is not one of the two.
		self.assertEqual(roles.parse_entity_access(stored), [COMMA_CO, MAIN])
		self.assertEqual(stored, f"{COMMA_CO}\n{MAIN}")

	def test_a_comma_name_beside_a_plain_one_on_the_same_line(self):
		"""Three comma-separated pieces, two companies. The case that forced the
		longest-match walk: an all-or-nothing rule reads this as one bad name."""
		self.assertEqual(
			roles.parse_entity_access(f"{COMMA_CO}, {MAIN}"),
			[COMMA_CO, MAIN],
		)

	def test_the_unresolvable_tail_is_reported_whole(self):
		"""So the refusal names what was typed rather than a fragment of it."""
		self.assertEqual(
			roles.parse_entity_access(f"{MAIN}, Nowhere Farms, LLC"),
			[MAIN, "Nowhere Farms, LLC"],
		)

	def test_a_missing_space_after_the_comma_looks_up_as_written(self):
		self.assertEqual(roles.parse_entity_access(f"{MAIN},{OTHER}"), [MAIN, OTHER])


# ── the register the operator could not open ────────────────────────────────
class TheAssetRegisterIsReadableFromTheDesk(RolesTestCase):
	"""v0.153.0. The doctype that granted its own roles and forgot the farm's.

	`Asset Register` shipped with two standard DocPerms — System Manager and
	Accounts Manager — and nothing else, so the person who runs the orchard could
	not open the list of the orchard's equipment in the Desk. Not a scoping
	refusal and not a missing workspace link: no permission at all, on the
	register `register_asset` writes into from every handset on the farm.

	IT IS GRANTED IN THE DOCTYPE JSON AND NOT IN `roles.py`, WHICH IS THE PATTERN
	THIS APP ALREADY HAS FOR ITS OWN DOCTYPES AND IS NOT THE ONE `Farm Task`
	USES. Farm Task, Housing Unit and Field carry the same two shipped rows this
	one did; their Farm Manager access is a Custom DocPerm `install_roles` writes
	because they are in `DISPATCH`, `CAMP` and `GROUND`. The other pattern —
	`Container Fill Threshold`, `Bucket Log Session`, `Budget`, `ML Model` and
	twelve more — ships the grant as a standard DocPerm and deliberately does NOT
	list the doctype in `ROLE_SPECS`, because `describe_role` reads that tuple and
	a grant written there would be a silent no-op the catalogue then advertises as
	truth. The v0.68.1 comment on `FILL_STANDARDS` sets that rule out; this
	follows it.
	"""

	def perms(self) -> dict:
		return {
			str(row["role"]): row
			for row in frappe.db.get_all(
				"DocPerm", filters={"parent": "Asset Register"}, fields="*"
			)
			or []
		}

	def test_the_farm_manager_may_read_write_and_create(self):
		perms = self.perms()
		self.assertIn("Farm Manager", perms, "the role that runs the operation")
		row = perms["Farm Manager"]
		self.assertEqual(int(row.get("read") or 0), 1)
		self.assertEqual(int(row.get("write") or 0), 1)
		self.assertEqual(int(row.get("create") or 0), 1)

	def test_the_farm_manager_may_not_delete(self):
		"""A tag on a machine is what a scan resolves and what an insurance
		schedule is built from. Retiring an asset is `retire_asset`, which leaves
		the row and its history; deleting it takes both away silently."""
		self.assertEqual(int(self.perms()["Farm Manager"].get("delete") or 0), 0)

	def test_an_employee_may_read_it_and_nothing_more(self):
		"""Every account on the site holds `Employee`, so this row is the widest
		grant on the doctype and is read-only by construction."""
		row = self.perms()["Employee"]
		self.assertEqual(int(row.get("read") or 0), 1)
		for denied in ("write", "create", "delete", "submit", "cancel", "amend"):
			with self.subTest(flag=denied):
				self.assertEqual(int(row.get(denied) or 0), 0)

	def test_the_employee_row_does_not_hand_out_the_whole_register(self):
		"""`export`, `share` and `email` are how a list leaves the site. Read in
		the Desk is what was missing; a spreadsheet of every machine the farm owns
		in every worker's hands is not the same request."""
		row = self.perms()["Employee"]
		for denied in ("export", "share", "email"):
			with self.subTest(flag=denied):
				self.assertEqual(int(row.get(denied) or 0), 0)

	def test_the_two_shipped_roles_are_untouched(self):
		"""Adding rows to a doctype's permission list is how the existing ones get
		reordered out of existence by a careless rewrite."""
		perms = self.perms()
		self.assertEqual(int(perms["System Manager"].get("delete") or 0), 1)
		self.assertEqual(int(perms["Accounts Manager"].get("write") or 0), 1)
		self.assertEqual(int(perms["Accounts Manager"].get("delete") or 0), 0)

	def test_no_custom_docperm_is_written_for_it_so_the_standard_rows_survive(self):
		"""THE TRAP THAT WOULD MAKE ALL OF THE ABOVE A NO-OP ON TIM'S SITE. One
		Custom DocPerm on a doctype makes Frappe discard EVERY standard DocPerm it
		has, for every role — see `TheCustomDocPermTrap`. These four rows are
		standard, so they are live only while nothing writes a custom one, and
		`install_roles` writes custom rows for exactly `permission_targets()`.

		If a later release adds Asset Register to a `roles.py` group, the mirror in
		`_mirror_standard_perms` copies these across first and they survive — but
		the grant written there would then be the live one and this file would be
		describing history. Fail here instead, so that is a decision somebody makes
		rather than one they discover.
		"""
		self.install()
		self.assertNotIn("Asset Register", roles.permission_targets())
		self.assertEqual(custom_perms("Asset Register"), {})

	def test_the_roles_named_here_exist_on_the_site(self):
		"""A standard DocPerm naming a role the site does not have is a link this
		app cannot resolve at migrate, and `_mirror_standard_perms` documents the
		same failure from the other direction. `Employee` comes from ERPNext,
		which this app requires; `Farm Manager` is `install_roles`' own.
		"""
		self.install()
		for role in ("Farm Manager", "Employee"):
			with self.subTest(role=role):
				self.assertTrue(frappe.db.exists("Role", role))
