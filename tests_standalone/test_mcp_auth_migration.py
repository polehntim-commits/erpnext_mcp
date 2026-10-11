# SPDX-License-Identifier: MIT
"""OAuth sign-in and the limited MCP user — the safe migration. v0.317.0.

Every MCP call records how it signed in (Auth Method: Static token / OAuth). `list_access_inventory` groups who still
uses the static token, so each client can be moved to OAuth and verified before Tim turns the static token off in the
Desk. The Desk's "Create MCP System User (from plan)" makes the least-privilege user and names it, but never ticks
Require User Context — that stays Tim's tick.
"""

import frappe

from erpnext_mcp import mcp_user_plan

from .harness import STORE
from .test_mcp_oauth import OAuthCase


class HowEachCallSignedIn(OAuthCase):
	def rows(self, tool):
		return [r for r in STORE.rows("MCP Action Log") if r.get("tool_name") == tool]

	def test_a_static_token_call_is_marked(self):
		self.configure(enabled=1, allow_get_server_status=1)
		self.tool_data("get_server_status")
		self.assertEqual(self.rows("get_server_status")[-1].get("auth_method"), "Static token")

	def test_an_oauth_call_is_marked(self):
		self.oauth_on()
		_client, _opened, tokens = self.signed_in()
		self.mcp("tools/call", {"name": "get_server_status", "arguments": {}}, bearer=tokens["access_token"])
		self.assertEqual(self.rows("get_server_status")[-1].get("auth_method"), "OAuth")

	def test_the_inventory_lists_who_still_uses_the_static_token(self):
		self.configure(enabled=1, allow_get_server_status=1, allow_list_access_inventory=1)
		self.tool_data("get_server_status", headers={"User-Agent": "oml-morning-brief/1.0"})
		got = self.tool_data("list_access_inventory")
		clients = {c["client"] for c in got["static_token_callers"]}
		self.assertIn("oml-morning-brief/1.0", clients)
		self.assertIn("ready_to_disable_static_mcp_token", got["static_token"])


class TheSystemUser(OAuthCase):
	def test_it_is_created_with_the_plans_roles_and_require_user_context_is_untouched(self):
		self.configure(enabled=1, require_user_context=0)
		STORE.seed("Role", [{"name": r, "role_name": r} for r in mcp_user_plan.plan()["recommended_roles"]])
		STORE.commit()
		out = mcp_user_plan.apply("mcp@farm.test", "Administrator")
		STORE.commit()
		self.assertTrue(frappe.db.exists("User", "mcp@farm.test"))
		self.assertNotIn("System Manager", out["roles"])
		settings = frappe.get_single("ERPNext MCP Settings")
		self.assertEqual(settings.mcp_system_user, "mcp@farm.test")
		self.assertFalse(int(settings.get("require_user_context") or 0), "Tim ticks it, not this")

	def test_a_bad_address_is_refused(self):
		with self.assertRaises(ValueError):
			mcp_user_plan.apply("not-an-email", "Administrator")

	def test_no_mcp_tool_reaches_it(self):
		from erpnext_mcp import registry

		self.assertFalse([n for n in registry.TOOLS if "create_mcp_system_user" in n])
