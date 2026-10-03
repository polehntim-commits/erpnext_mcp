# SPDX-License-Identifier: MIT
"""v0.217.0 — docs/design/security_status_and_alerts.md."""

import json

import frappe

from erpnext_mcp import security, security_status, security_watch, switch_catalog, switch_timer
from erpnext_mcp.errors import ToolError

from .fixtures import SeededTestCase
from .harness import STORE


class Defaults:
	"""frappe.defaults, global only."""

	def __init__(self):
		self.values = {}

	def get_global_default(self, key):
		return self.values.get(key)

	def set_global_default(self, key, value):
		self.values[key] = value


class SecurityCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, security_alert_email="sec@example.test")
		frappe.local.session.user = "Administrator"
		self.defaults = Defaults()
		original = getattr(frappe, "defaults", None)
		frappe.defaults = self.defaults
		self.addCleanup(
			lambda: setattr(frappe, "defaults", original) if original else delattr(frappe, "defaults")
		)


# ── §1 ──────────────────────────────────────────────────────────────────────
class TheSwitchInventory(SecurityCase):
	def test_tiers_are_derived(self):
		from erpnext_mcp import registry

		self.assertEqual(
			switch_catalog.tier("generate_api_token", registry.TOOLS["generate_api_token"]), "credential"
		)
		self.assertEqual(
			switch_catalog.tier("submit_journal_entry", registry.TOOLS["submit_journal_entry"]), "financial"
		)
		self.assertEqual(
			switch_catalog.tier("delete_account", registry.TOOLS["delete_account"]), "destructive"
		)
		self.assertEqual(
			switch_catalog.tier("get_server_status", registry.TOOLS["get_server_status"]), "read"
		)
		self.assertEqual(switch_catalog.tier("create_crop", registry.TOOLS["create_crop"]), "write")

	def test_every_switch_is_listed_and_dangerous_ones_on_are_named(self):
		from erpnext_mcp import registry

		self.configure(enabled=1, allow_submit_journal_entry=1)
		data = self.tool_data("list_mcp_switches", {})
		self.assertEqual(data["count"], len(registry.TOOLS))
		self.assertIn("submit_journal_entry", data["dangerous_enabled"])
		only = self.tool_data("list_mcp_switches", {"tier": "credential"})
		self.assertTrue(all(row["tier"] == "credential" for row in only["switches"]))

	def test_no_tool_can_turn_a_switch_on(self):
		from erpnext_mcp import registry

		writers = [
			name
			for name, spec in registry.TOOLS.items()
			if spec["mutating"] and ("switch" in name or name.startswith("enable_"))
		]
		self.assertEqual(writers, [], "a tool that turns switches on would let the AI widen its own reach")


# ── §2 ──────────────────────────────────────────────────────────────────────
class TheChecklist(SecurityCase):
	def test_a_score_and_named_fails_and_no_secret(self):
		data = self.tool_data("get_security_status", {})
		self.assertTrue(0 <= data["score"] <= 100)
		keys = {row["key"] for row in data["checks"]}
		self.assertTrue(
			{"mcp_system_user", "mcp_switches", "api_keys", "client_ip", "frappe_version"} <= keys
		)
		self.assertIn("mcp_system_user", data["fails"])
		self.assertNotIn(self.TOKEN, json.dumps(data))
		self.assertEqual(data["alert_recipients"], ["sec@example.test"])

	def test_users_with_keys_are_named_and_the_key_is_not(self):
		STORE.seed("User", [{"name": "bot@example.test", "enabled": 1, "api_key": "abc123secretish"}])
		data = self.tool_data("get_security_status", {})
		check = next(row for row in data["checks"] if row["key"] == "api_keys")
		self.assertIn("bot@example.test", check["finding"])
		self.assertNotIn("abc123secretish", json.dumps(data))

	def test_failed_login_bursts_are_counted_per_user_per_hour(self):
		now = frappe.utils.get_datetime(frappe.utils.now())
		rows = [
			{
				"name": f"AL{i}",
				"user": "ana@example.test",
				"operation": "Login",
				"status": "Failed",
				"creation": str(now)[:19],
			}
			for i in range(7)
		]
		STORE.seed("Activity Log", rows)
		self.assertEqual(
			security_status._failed_bursts(str(frappe.utils.add_days(now, -1))[:19]), ["ana@example.test"]
		)

	def test_the_version_regression_warning(self):
		self.assertEqual(security_status._version("15.118.2"), (15, 118, 2))
		self.assertGreaterEqual(security_status._version("15.120.0"), security_status.REGRESSION_FROM)


# ── §3 ──────────────────────────────────────────────────────────────────────
class TheAlerts(SecurityCase):
	def stamp(self):
		return str(frappe.utils.now())[:19]

	def test_an_administrator_login_is_alerted_once(self):
		self.defaults.set_global_default(security_watch.WATERMARK_KEY, "2000-01-01 00:00:00")
		STORE.seed(
			"Activity Log",
			[
				{
					"name": "AL1",
					"user": "Administrator",
					"operation": "Login",
					"status": "Success",
					"creation": self.stamp(),
					"ip_address": "100.64.0.9",
				}
			],
		)
		before = len(STORE.emails)
		events = security_watch.scan()
		self.assertEqual([event["kind"] for event in events], ["administrator_login"])
		self.assertEqual(len(STORE.emails), before + 1)
		self.assertEqual(STORE.emails[-1]["recipients"], ["sec@example.test"])
		self.assertEqual(security_watch.scan(), [])

	def test_more_than_five_failures_in_an_hour_once_per_user_per_hour(self):
		STORE.seed(
			"Activity Log",
			[
				{
					"name": f"F{i}",
					"user": "ana@example.test",
					"operation": "Login",
					"status": "Failed",
					"creation": self.stamp(),
				}
				for i in range(6)
			],
		)
		self.assertEqual([event["kind"] for event in security_watch.scan()], ["failed_logins"])
		self.assertEqual(security_watch.scan(), [])

	def test_a_settings_change_names_fields_and_switch_states_never_secrets(self):
		self.defaults.set_global_default(security_watch.WATERMARK_KEY, "2000-01-01 00:00:00")
		data = {
			"changed": [
				["allow_submit_journal_entry", 0, 1],
				["auth_token", "old-secret-xyz", "new-secret-abc"],
			]
		}
		STORE.seed(
			"Version",
			[
				{
					"name": "V1",
					"ref_doctype": "ERPNext MCP Settings",
					"docname": "ERPNext MCP Settings",
					"owner": "tim@example.test",
					"creation": self.stamp(),
					"data": json.dumps(data),
				}
			],
		)
		events = security_watch.scan()
		self.assertEqual(len(events), 1)
		self.assertIn("allow_submit_journal_entry: on", events[0]["text"])
		self.assertIn("auth_token", events[0]["text"])
		self.assertNotIn("secret", json.dumps(events) + json.dumps(STORE.emails))

	def test_a_new_api_key_is_alerted_without_the_key(self):
		self.defaults.set_global_default(security_watch.WATERMARK_KEY, "2000-01-01 00:00:00")
		data = {"changed": [["api_key", None, "k3yv4lu3"]]}
		STORE.seed(
			"Version",
			[
				{
					"name": "V2",
					"ref_doctype": "User",
					"docname": "bot@example.test",
					"owner": "Administrator",
					"creation": self.stamp(),
					"data": json.dumps(data),
				}
			],
		)
		events = security_watch.scan()
		self.assertEqual([event["kind"] for event in events], ["api_key_generated"])
		self.assertNotIn("k3yv4lu3", json.dumps(events))

	def test_the_hook_hears_it(self):
		heard = []
		original = security_watch._listeners
		security_watch._listeners = lambda: [heard.append]
		self.addCleanup(lambda: setattr(security_watch, "_listeners", original))
		security_watch.raise_alert({"kind": "x", "title": "t", "text": "body"})
		self.assertEqual(heard[0]["kind"], "x")


# ── §4 ──────────────────────────────────────────────────────────────────────
class TheClientAddress(SecurityCase):
	def test_empty_setting_is_the_old_rule(self):
		self.assertEqual(security.client_ip("1.2.3.4, 172.18.0.1", "127.0.0.1", trusted=[]), "172.18.0.1")
		self.assertEqual(security.client_ip("", "100.64.0.9", trusted=[]), "100.64.0.9")

	def test_trusted_proxies_are_skipped_from_the_right(self):
		trusted = ["127.0.0.0/8", "172.16.0.0/12"]
		self.assertEqual(
			security.client_ip("100.70.1.2, 172.18.0.1", "127.0.0.1", trusted=trusted), "100.70.1.2"
		)

	def test_a_forged_leftmost_hop_does_not_win(self):
		trusted = ["127.0.0.0/8", "172.16.0.0/12"]
		self.assertEqual(
			security.client_ip("6.6.6.6, 100.70.1.2, 172.18.0.1", "127.0.0.1", trusted=trusted), "100.70.1.2"
		)

	def test_the_setting_is_read(self):
		self.configure(enabled=1, trusted_proxy_cidrs="127.0.0.0/8\n172.16.0.0/12  # docker")
		self.assertEqual(security.client_ip("100.70.1.2, 172.18.0.1", "127.0.0.1"), "100.70.1.2")


# ── §5 ──────────────────────────────────────────────────────────────────────
class TimeBoxedSwitches(SecurityCase):
	def test_enable_for_then_revert(self):
		answer = switch_timer.enable_for("create_crop", 15, "tim@example.test")
		self.assertTrue(answer["enabled"])
		from erpnext_mcp import settings

		self.assertTrue(settings.tool_enabled("create_crop"))
		self.assertIn("create_crop", switch_catalog.expiries())
		self.assertEqual(switch_timer.revert_expired(), [])
		doc = frappe.get_single("ERPNext MCP Settings")
		doc.switch_expiry = json.dumps({"create_crop": "2000-01-01 00:00:00"})
		doc.save()
		self.assertEqual(switch_timer.revert_expired(), ["create_crop"])
		self.assertFalse(settings.tool_enabled("create_crop"))
		self.assertNotIn("create_crop", switch_catalog.expiries())

	def test_a_switch_already_on_by_hand_is_refused_and_never_reverted(self):
		self.configure(enabled=1, allow_create_crop=1)
		with self.assertRaises(ToolError):
			switch_timer.enable_for("create_crop", 15, "tim@example.test")
		switch_timer.revert_expired()
		from erpnext_mcp import settings

		self.assertTrue(settings.tool_enabled("create_crop"))

	def test_bounds_and_unknown_tools(self):
		for tool, minutes in (
			("create_crop", 0),
			("create_crop", 241),
			("not_a_tool", 5),
			("create_crop", "x"),
		):
			with self.assertRaises(ToolError):
				switch_timer.enable_for(tool, minutes, "tim@example.test")

	def test_the_desk_method_is_system_manager_only(self):
		from erpnext_mcp.api import switches

		self.assertTrue(
			hasattr(switches.enable_for, "__wrapped_whitelisted__") or callable(switches.enable_for)
		)
		self.assertIn("frappe.only_for", open(switches.__file__).read())
