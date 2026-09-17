# SPDX-License-Identifier: MIT
"""Who sees which workspace, and which default pages are put away.

SIX CLAIMS.

1. `TheProfilesSeeWhatTheyShould` — THE RELEASE. The sidebar each profile gets is
   computed the way Frappe computes it (union over the roles it holds) and
   compared against what the operation asked for. The two tables in `sidebar.py`
   define the answer; this walks the mechanism to it, so a role added to one
   profile that quietly widens another fails here.
2. `TheDefaultsArePutAway` — the six irrelevant pages are hidden, Accounting and
   HR are not, and nothing is deleted.
3. `TheOperatorsChoiceWins` — a page somebody puts back stays back through the
   next migrate. This is the half that makes re-applying on migrate safe.
4. `NobodyIsLockedOut` — an administrator keeps every page, which is Frappe's own
   rule and the thing a visibility release most needs to not break.
5. `TheTools` — the five doors: what they answer, and who is refused.
6. `TheInstallerIsIdempotent` — this runs inside `bench migrate`.
"""

import json
import unittest
from typing import ClassVar

import frappe

from erpnext_mcp import sidebar
from erpnext_mcp.dashboard import WORKSPACE

from .fixtures import V12TestCase
from .harness import ROLES, STORE

#: The sidebar the operation asked for, written out ONCE, here, as the thing the
#: mechanism is checked against. `sidebar.PROFILE_WORKSPACES` is the source the
#: code derives from; this is the independent statement of the same intent, and a
#: release that changed one without the other fails.
EXPECTED_SIDEBARS = {
	"Owner": {
		"Farm Operations",
		"Crew & Labor",
		"Compliance",
		"Crop Protection",
		"Assets & Equipment",
		"Land & Parcels",
		"Financial",
		"Market & Sales",
		"Map & Terrain",
	},
	"Manager": {
		"Farm Operations",
		"Crew & Labor",
		"Compliance",
		"Crop Protection",
		"Assets & Equipment",
		"Land & Parcels",
		"Map & Terrain",
	},
	"Bookkeeper": {"Financial", "Compliance", "Market & Sales", "Assets & Equipment"},
	"Field Supervisor": {"Farm Operations", "Crew & Labor", "Crop Protection", "Assets & Equipment"},
	"Land Owner": {"Land & Parcels", "Financial", "Assets & Equipment"},
}

THE_NINE = EXPECTED_SIDEBARS["Owner"]

DEFAULT_PAGES = ("Manufacturing", "Quality", "Projects", "Support", "Website", "CRM", "Accounting", "HR")


class SidebarTestCase(V12TestCase):
	SWITCHES: ClassVar[dict] = {
		f"allow_{name}": 1
		for name in (
			"list_workspace_visibility",
			"get_user_sidebar",
			"configure_workspace_visibility",
			"hide_default_workspace",
			"set_user_role_profile",
		)
	}

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **self.SWITCHES)
		ROLES["Administrator"] = ["System Manager"]
		STORE.rows(WORKSPACE).clear()
		for role in sorted(
			{role for roles in sidebar.PROFILES.values() for role in roles}
			| {"System Manager", "Compliance Officer", "Workspace Manager"}
		):
			if not frappe.db.exists("Role", role):
				STORE.seed("Role", [{"name": role, "role_name": role, "desk_access": 1}])
		self.build_workspaces()

	def build_workspaces(self):
		"""The nine pages, as `farm_workspaces` builds them, plus the defaults."""
		from erpnext_mcp import farm_workspaces

		farm_workspaces.install_farm_workspaces()
		for name in DEFAULT_PAGES:
			if not frappe.db.exists(WORKSPACE, name):
				STORE.seed(
					WORKSPACE,
					[
						{
							"name": name,
							"title": name,
							"module": name,
							"public": 1,
							"is_hidden": 0,
							"content": "[]",
						}
					],
				)

	def a_user(self, email: str, *roles) -> str:
		"""A user whose roles are on the DOCUMENT as well as in the session
		registry, which is how a real one carries them: `frappe.get_roles` reads
		the session, and `set_user_role_profile` writes the child table."""
		for role in roles:
			if not frappe.db.exists("Role", role):
				STORE.seed("Role", [{"name": role, "role_name": role, "desk_access": 1}])
		STORE.seed(
			"User",
			[
				{
					"name": email,
					"email": email,
					"enabled": 1,
					"full_name": email,
					"user_type": "System User",
					"roles": [{"role": role} for role in roles],
				}
			],
		)
		ROLES[email] = list(roles)
		return email

	def as_roles(self, *roles):
		ROLES["Administrator"] = list(roles)


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheProfilesSeeWhatTheyShould(SidebarTestCase):
	"""THE RELEASE. Every other class here is a safety property; this is the ask."""

	def test_each_profile_sees_exactly_what_was_asked_for(self):
		"""Of the NINE. This app also ships three older pages (the dispatch board,
		Irrigation, Onboard Worker) which are gated too — `TheOlderPagesAreGatedToo`
		is where those are asserted."""
		for profile, expected in EXPECTED_SIDEBARS.items():
			with self.subTest(profile=profile):
				self.assertEqual(set(sidebar.profile_sidebar(profile)) & THE_NINE, expected)

	def test_the_owner_sees_all_nine(self):
		self.assertEqual(set(sidebar.profile_sidebar("Owner")) & THE_NINE, THE_NINE)

	def test_the_land_owner_sees_three_and_not_the_task_board(self):
		"""Tim's mother's view. The point of the release is what is NOT on it."""
		seen = set(sidebar.profile_sidebar("Land Owner"))
		self.assertEqual(seen, {"Land & Parcels", "Financial", "Assets & Equipment"})
		self.assertNotIn("Farm Operations", seen)
		self.assertNotIn("Crew & Labor", seen)

	def test_no_profile_invents_a_role_this_app_does_not_ship(self):
		"""A role named here that no installer creates is a profile that grants
		nothing and looks like it worked."""
		for profile, roles in sidebar.PROFILES.items():
			for role in roles:
				with self.subTest(profile=profile, role=role):
					self.assertTrue(frappe.db.exists("Role", role))

	def test_every_workspace_role_comes_from_a_profile_or_is_named_as_an_extra(self):
		allowed = {role for roles in sidebar.PROFILES.values() for role in roles}
		allowed |= {role for roles in sidebar.EXTRA_WORKSPACE_ROLES.values() for role in roles}
		allowed.add(sidebar.ADMIN_ROLE)
		for workspace, roles in sidebar.workspace_roles().items():
			for role in roles:
				with self.subTest(workspace=workspace, role=role):
					self.assertIn(role, allowed)

	def test_every_page_this_app_ships_is_gated_and_no_page_it_does_not_is(self):
		"""A page with an empty roles table is shown to EVERYBODY — which is how a
		Land Owner was still getting the dispatch board. So the set of pages this
		app gates is asserted exactly: the nine, plus its own three older ones, and
		nothing belonging to anybody else."""
		expected = {name for names in sidebar.PROFILE_WORKSPACES.values() for name in names}
		expected |= set(sidebar.OUR_OTHER_WORKSPACES)
		self.assertEqual(set(sidebar.workspace_roles()), expected)


class TheOlderPagesAreGatedToo(SidebarTestCase):
	"""The dispatch board, Irrigation and Onboard Worker shipped before the nine
	and carried no roles, so Frappe showed them to everyone."""

	def test_the_land_owner_does_not_get_the_dispatch_board(self):
		seen = set(sidebar.profile_sidebar("Land Owner"))
		for page in sidebar.OUR_OTHER_WORKSPACES:
			with self.subTest(page=page):
				self.assertNotIn(page, seen)

	def test_the_field_supervisor_keeps_the_board_and_the_valves(self):
		seen = set(sidebar.profile_sidebar("Field Supervisor"))
		self.assertIn("Farm Task Dispatch", seen)
		self.assertIn("Irrigation", seen)

	def test_only_pages_this_app_built_are_named(self):
		"""Gating somebody else's page by role would be this app deciding who may
		use another app."""
		self.assertEqual(
			sorted(sidebar.OUR_OTHER_WORKSPACES),
			["Farm Task Dispatch", "Irrigation", "Onboard Worker"],
		)


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheDefaultsArePutAway(SidebarTestCase):
	def test_the_six_irrelevant_pages_are_hidden(self):
		sidebar.hide_defaults()
		for name in ("Manufacturing", "Quality", "Projects", "Support", "Website", "CRM"):
			with self.subTest(workspace=name):
				self.assertTrue(frappe.db.get_value(WORKSPACE, name, "is_hidden"))

	def test_accounting_and_hr_are_left_alone(self):
		"""They are where somebody who has used ERPNext before goes looking."""
		sidebar.hide_defaults()
		for name in ("Accounting", "HR"):
			with self.subTest(workspace=name):
				self.assertFalse(frappe.db.get_value(WORKSPACE, name, "is_hidden"))

	def test_agriculture_is_not_hidden_on_a_guess(self):
		self.assertNotIn("Agriculture", sidebar.HIDDEN_DEFAULTS)

	def test_nothing_is_deleted(self):
		before = len(STORE.rows(WORKSPACE))
		sidebar.hide_defaults()
		self.assertEqual(len(STORE.rows(WORKSPACE)), before)

	def test_a_page_this_site_does_not_have_is_reported_not_created(self):
		frappe.delete_doc(WORKSPACE, "Manufacturing", force=True)
		report = sidebar.hide_defaults()
		self.assertIn("Manufacturing", report["absent"])
		self.assertFalse(frappe.db.exists(WORKSPACE, "Manufacturing"))

	def test_uninstall_puts_them_back(self):
		sidebar.hide_defaults()
		report = sidebar.restore_defaults()
		self.assertIn("CRM", report["restored"])
		self.assertFalse(frappe.db.get_value(WORKSPACE, "CRM", "is_hidden"))


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheOperatorsChoiceWins(SidebarTestCase):
	"""A migrate that re-applied the SHIPPED list would undo an operator every
	upgrade. It re-applies the STORED list, which is what this proves."""

	def test_a_page_put_back_stays_back_through_the_next_migrate(self):
		sidebar.hide_defaults()
		from erpnext_mcp.tools import sidebar as tools

		tools.hide_default_workspace({"workspace": "CRM", "hidden": False})
		sidebar.hide_defaults()  # the next migrate
		self.assertFalse(frappe.db.get_value(WORKSPACE, "CRM", "is_hidden"))

	def test_a_page_hidden_by_hand_is_re_hidden_after_a_resync(self):
		from erpnext_mcp.tools import sidebar as tools

		tools.hide_default_workspace({"workspace": "Agriculture", "hidden": True}) if frappe.db.exists(
			WORKSPACE, "Agriculture"
		) else None
		STORE.seed(
			WORKSPACE,
			[
				{
					"name": "Agriculture",
					"title": "Agriculture",
					"module": "Agriculture",
					"public": 1,
					"is_hidden": 0,
				}
			],
		)
		tools.hide_default_workspace({"workspace": "Agriculture", "hidden": True})
		# An app upgrade re-syncs the page and clears the flag.
		frappe.db.set_value(WORKSPACE, "Agriculture", "is_hidden", 0)
		sidebar.hide_defaults()
		self.assertTrue(frappe.db.get_value(WORKSPACE, "Agriculture", "is_hidden"))

	def test_an_untouched_site_falls_back_to_the_shipped_list(self):
		self.assertEqual(sidebar.hidden_list(), list(sidebar.HIDDEN_DEFAULTS))

	def test_an_empty_stored_list_means_hide_none_and_is_not_the_same_as_unset(self):
		sidebar.set_hidden_list([])
		self.assertEqual(sidebar.hidden_list(), [])
		report = sidebar.hide_defaults()
		self.assertEqual(report["hidden"], [])


# ── 4 ───────────────────────────────────────────────────────────────────────
class NobodyIsLockedOut(SidebarTestCase):
	def test_every_page_this_app_restricts_still_names_the_admin_role(self):
		for workspace, roles in sidebar.workspace_roles().items():
			with self.subTest(workspace=workspace):
				self.assertIn("System Manager", roles)

	def test_an_administrator_sees_all_nine(self):
		sidebar.apply_visibility()
		user = self.a_user("admin@example.com", "System Manager")
		answer = sidebar.user_sidebar(user)
		for name in EXPECTED_SIDEBARS["Owner"]:
			with self.subTest(workspace=name):
				self.assertIn(name, answer["sees"])

	def test_a_workspace_manager_sees_even_the_hidden_pages(self):
		"""Frappe's own rule, and the reason nothing here can lock somebody out."""
		sidebar.hide_defaults()
		sidebar.apply_visibility()
		user = self.a_user("boss@example.com", "Workspace Manager")
		answer = sidebar.user_sidebar(user)
		self.assertTrue(answer["is_workspace_manager"])
		self.assertIn("Manufacturing", answer["sees"])
		self.assertIn("Workspace Manager", answer["note"])

	def test_a_field_supervisor_is_told_why_a_page_is_missing(self):
		sidebar.apply_visibility()
		user = self.a_user("foreman@example.com", "Foreman")
		answer = sidebar.user_sidebar(user)
		self.assertEqual(
			set(answer["sees"]) & EXPECTED_SIDEBARS["Owner"], EXPECTED_SIDEBARS["Field Supervisor"]
		)
		missing = {row["workspace"]: row["reason"] for row in answer["does_not_see"]}
		self.assertIn("Financial", missing)
		self.assertIn("needs one of", missing["Financial"])


# ── 5 ───────────────────────────────────────────────────────────────────────
class TheTools(SidebarTestCase):
	def test_list_workspace_visibility_reports_ours_hidden_and_restricted(self):
		sidebar.apply_visibility()
		sidebar.hide_defaults()
		data = self.tool_data("list_workspace_visibility", {})
		self.assertEqual(len(data["ours"]), 9)
		self.assertIn("CRM", data["hidden"])
		self.assertIn("Financial", data["restricted"])
		self.assertEqual(set(data["profiles"]["Land Owner"]), EXPECTED_SIDEBARS["Land Owner"])

	def test_get_user_sidebar_answers_for_one_person(self):
		sidebar.apply_visibility()
		self.a_user("mum@example.com", "Family Member")
		data = self.tool_data("get_user_sidebar", {"user": "mum@example.com"})
		self.assertEqual(set(data["sees"]) & EXPECTED_SIDEBARS["Owner"], EXPECTED_SIDEBARS["Land Owner"])

	def test_get_user_sidebar_refuses_a_user_that_does_not_exist(self):
		self.assertIn("no User called", self.tool_error("get_user_sidebar", {"user": "ghost@example.com"}))

	def test_configure_workspace_visibility_writes_the_roles_table(self):
		data = self.tool_data(
			"configure_workspace_visibility", {"workspace": "Financial", "roles": ["Accounts Manager"]}
		)
		self.assertEqual(data["roles_after"], ["Accounts Manager"])
		doc = frappe.get_doc(WORKSPACE, "Financial")
		self.assertEqual([row.get("role") for row in doc.get("roles")], ["Accounts Manager"])

	def test_an_empty_role_list_means_everyone(self):
		"""Frappe's own rule: `is_permitted` returns True on an empty table."""
		data = self.tool_data("configure_workspace_visibility", {"workspace": "Financial", "roles": []})
		self.assertTrue(data["visible_to_everyone"])

	def test_a_role_this_site_does_not_have_is_dropped_and_named(self):
		data = self.tool_data(
			"configure_workspace_visibility",
			{"workspace": "Financial", "roles": ["Accounts Manager", "Chief Vibes Officer"]},
		)
		self.assertEqual(data["roles_not_on_this_site"], ["Chief Vibes Officer"])
		self.assertFalse(frappe.db.exists("Role", "Chief Vibes Officer"))

	def test_hide_default_workspace_records_the_choice(self):
		data = self.tool_data("hide_default_workspace", {"workspace": "Quality"})
		self.assertTrue(data["is_hidden"])
		self.assertIn("Quality", data["hidden_default_workspaces"])

	def test_set_user_role_profile_adds_roles_and_never_removes_one(self):
		user = self.a_user("mum@example.com", "Employee")
		data = self.tool_data("set_user_role_profile", {"user": user, "profile": "Land Owner"})
		self.assertIn("Family Member", data["roles_now"])
		self.assertIn("Employee", data["roles_now"])
		self.assertEqual(set(data["workspaces"]), EXPECTED_SIDEBARS["Land Owner"])

	def test_a_profile_takes_the_modules_it_does_not_need_off_the_sidebar(self):
		"""ROLE VISIBILITY ALONE IS NOT A SHORT SIDEBAR. On a real bench, with the
		nine pages gated and six defaults hidden, a Land Owner still saw
		twenty-seven entries: every remaining ERPNext and HRMS page carries an
		empty roles table, and Frappe shows those to everyone. Blocking the
		modules that person does not need is the mechanism that matches what was
		actually asked for."""
		STORE.seed(
			"Module Def",
			[
				{"name": name, "module_name": name}
				for name in ("ERPNext MCP", "Accounts", "Manufacturing", "Support")
			],
		)
		user = self.a_user("mum@example.com", "Employee")
		data = self.tool_data("set_user_role_profile", {"user": user, "profile": "Land Owner"})
		self.assertIn("Manufacturing", data["modules_blocked"])
		self.assertIn("Support", data["modules_blocked"])
		self.assertNotIn("ERPNext MCP", data["modules_blocked"])
		self.assertNotIn("Accounts", data["modules_blocked"])
		self.assertEqual(frappe.db.get_value("User", user, "module_profile"), "Farm Land Owner")

	def test_an_administrator_is_never_trimmed(self):
		"""Blocking modules for somebody who administers the site would take away
		the Desk they work in."""
		STORE.seed("Module Def", [{"name": "Manufacturing", "module_name": "Manufacturing"}])
		user = self.a_user("boss@example.com", "System Manager")
		data = self.tool_data("set_user_role_profile", {"user": user, "profile": "Land Owner"})
		self.assertEqual(data["modules_blocked"], [])
		self.assertIn("administers this site", data["modules_note"])

	def test_the_owner_profile_blocks_nothing(self):
		STORE.seed("Module Def", [{"name": "Manufacturing", "module_name": "Manufacturing"}])
		user = self.a_user("owner@example.com", "Employee")
		data = self.tool_data("set_user_role_profile", {"user": user, "profile": "Owner"})
		self.assertEqual(data["modules_blocked"], [])
		self.assertIsNone(data["module_profile"])

	def test_trimming_can_be_declined(self):
		STORE.seed("Module Def", [{"name": "Manufacturing", "module_name": "Manufacturing"}])
		user = self.a_user("mum2@example.com", "Employee")
		data = self.tool_data(
			"set_user_role_profile", {"user": user, "profile": "Land Owner", "trim_sidebar": False}
		)
		self.assertEqual(data["modules_blocked"], [])
		self.assertIn("asked not to trim", data["modules_note"])
		self.assertIn("Family Member", data["roles_now"])

	def test_an_unknown_profile_is_refused_with_the_list(self):
		user = self.a_user("mum@example.com")
		error = self.tool_error("set_user_role_profile", {"user": user, "profile": "Chief"})
		self.assertIn("Land Owner", error)

	def test_the_three_writers_are_system_manager_only(self):
		self.as_roles("Farm Manager")
		for name, args in (
			("configure_workspace_visibility", {"workspace": "Financial", "roles": []}),
			("hide_default_workspace", {"workspace": "Quality"}),
			("set_user_role_profile", {"user": "x@example.com", "profile": "Owner"}),
		):
			with self.subTest(tool=name):
				self.assertIn("System Manager", self.tool_error(name, args))

	def test_the_two_readers_are_not_mutating(self):
		from erpnext_mcp import registry

		for name in ("list_workspace_visibility", "get_user_sidebar"):
			with self.subTest(tool=name):
				self.assertFalse(registry.TOOLS[name]["mutating"])
		for name in ("configure_workspace_visibility", "hide_default_workspace", "set_user_role_profile"):
			with self.subTest(tool=name):
				self.assertTrue(registry.TOOLS[name]["mutating"])


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheInstallerIsIdempotent(SidebarTestCase):
	"""This runs inside `bench migrate`. Anything raised here takes a real site's
	migration down for every app on the bench."""

	def test_applying_twice_changes_nothing_the_second_time(self):
		first = sidebar.apply_visibility()
		second = sidebar.apply_visibility()
		self.assertTrue(first["configured"])
		self.assertEqual(second["configured"], [])
		self.assertEqual(second["failed"], [])

	def test_it_does_not_double_the_roles_table(self):
		sidebar.apply_visibility()
		sidebar.apply_visibility()
		doc = frappe.get_doc(WORKSPACE, "Financial")
		roles = [row.get("role") for row in doc.get("roles")]
		self.assertEqual(len(roles), len(set(roles)))

	def test_a_page_moved_to_another_module_keeps_its_own_visibility(self):
		frappe.db.set_value(WORKSPACE, "Financial", "module", "Accounts")
		report = sidebar.apply_visibility()
		reasons = {row["workspace"]: row["reason"] for row in report["skipped"]}
		self.assertIn("Financial", reasons)
		self.assertIn("not this app's to set", reasons["Financial"])

	def test_a_site_with_no_workspace_doctype_gets_a_note(self):
		original = sidebar.compat.doctype_exists
		sidebar.compat.doctype_exists = lambda doctype: doctype != WORKSPACE
		try:
			self.assertIn("Workspace", sidebar.apply_visibility()["note"])
			self.assertIn("Workspace", sidebar.hide_defaults()["note"])
		finally:
			sidebar.compat.doctype_exists = original

	def test_the_profiles_are_built_and_rebuilt_without_doubling(self):
		first = sidebar.ensure_profiles()
		self.assertEqual(first["failed"], [])
		sidebar.ensure_profiles()
		doc = frappe.get_doc("Role Profile", "Farm Land Owner")
		roles = [row.get("role") for row in doc.get("roles")]
		self.assertEqual(roles, ["Family Member"])

	def test_a_profile_that_is_already_right_is_not_saved_again(self):
		"""A no-op save is NOT free on a bench: Frappe's Role Profile controller
		enqueues `update_all_users` and LOCKS the document until that job runs, so
		a migrate that re-saved five profiles would churn every user holding one
		and leave the next call throwing DocumentLockedError. Found on a real
		site, and this is the guard."""
		self.assertTrue(sidebar.ensure_profile("Land Owner")["created"])
		second = sidebar.ensure_profile("Land Owner")
		self.assertFalse(second["created"])
		self.assertFalse(second["changed"])

	def test_a_profile_whose_roles_changed_is_saved(self):
		"""The other half: the no-op guard must not stop a real repair."""
		sidebar.ensure_profile("Land Owner")
		doc = frappe.get_doc("Role Profile", "Farm Land Owner")
		doc.set("roles", [])
		doc.save(ignore_permissions=True)
		self.assertTrue(sidebar.ensure_profile("Land Owner")["changed"])

	def test_the_stored_list_is_json_a_later_release_can_read(self):
		sidebar.set_hidden_list(["Quality"])
		raw = frappe.db.get_single_value(sidebar.SETTINGS, sidebar.HIDDEN_SETTING)
		self.assertEqual(json.loads(raw), ["Quality"])


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
