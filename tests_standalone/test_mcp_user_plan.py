"""The MCP System User plan and dry run. v0.257.0 — read-only: it never sets anything."""

import frappe

from erpnext_mcp import mcp_user_plan, settings

from .fixtures import V12TestCase
from .harness import STORE, set_roles

PLAN = {"allow_plan_mcp_system_user": 1}
MCP = "mcp@orchardmeadow.example"


class PlanCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **PLAN)

	def a_user(self, roles=("Accounts User",), user_type="System User", enabled=1):
		STORE.seed("User", [{"name": MCP, "email": MCP, "enabled": enabled, "user_type": user_type}])
		set_roles(MCP, list(roles))
		return MCP


class ThePlan(PlanCase):
	def test_today_it_says_calls_run_as_administrator_and_what_the_switches_on_need(self):
		self.configure(allow_create_journal_entry=1, allow_list_purchase_invoices=1)
		data = self.tool_data("plan_mcp_system_user", {})
		self.assertEqual(data["current"]["runs_as"], "Administrator")
		self.assertFalse(data["current"]["dedicated"])
		areas = {a["area"]: a for a in data["areas_in_use"]}
		self.assertGreaterEqual(areas["Ledger"]["write_tools_on"], 1)
		self.assertIn("Accounts User", data["recommended_roles"])
		self.assertNotIn("System Manager", data["recommended_roles"])
		self.assertIn("nothing", data["changed"])
		self.assertTrue(any("Role Profile" in step for step in data["steps"]))

	def test_it_writes_nothing(self):
		before = settings._value("mcp_system_user")
		self.a_user()
		self.tool_data("plan_mcp_system_user", {"candidate": MCP})
		self.assertEqual(settings._value("mcp_system_user"), before)
		self.assertNotIn(MCP, [r.get("name") for r in STORE.rows("Role Profile")])


class TheDryRun(PlanCase):
	def test_a_candidate_with_the_right_roles_is_ready(self):
		self.configure(allow_create_journal_entry=1)
		self.a_user(roles=("Accounts User",))
		check = self.tool_data("plan_mcp_system_user", {"candidate": MCP})["candidate"]
		self.assertTrue(check["ready"], check)
		self.assertEqual(check["gaps"], [])

	def test_what_frappe_would_refuse_is_named_with_the_tools_it_breaks(self):
		self.configure(allow_create_journal_entry=1)
		self.a_user(roles=())
		STORE.denied_permissions.add(("Journal Entry", "create"))
		check = self.tool_data("plan_mcp_system_user", {"candidate": MCP})["candidate"]
		self.assertFalse(check["ready"])
		gap = next(g for g in check["gaps"] if g["doctype"] == "Journal Entry")
		self.assertEqual((gap["permission"], gap["role_that_covers_it"]), ("create", ["Accounts User"]))
		self.assertIn("Accounts User", check["missing_roles"])

	def test_system_manager_a_website_user_and_a_disabled_user_are_problems(self):
		self.a_user(roles=("Accounts User", "System Manager"), user_type="Website User", enabled=0)
		problems = " ".join(self.tool_data("plan_mcp_system_user", {"candidate": MCP})["candidate"]["problems"])
		for words in ("disabled", "System User", "System Manager"):
			self.assertIn(words, problems)

	def test_a_user_not_created_yet_says_so(self):
		check = self.tool_data("plan_mcp_system_user", {"candidate": "nobody@example.test"})["candidate"]
		self.assertFalse(check["exists"])

	def test_every_area_names_only_standard_roles(self):
		for _area, _modules, roles, _reads, _writes in mcp_user_plan.AREAS:
			self.assertFalse(set(roles) & set(mcp_user_plan.NEVER))
