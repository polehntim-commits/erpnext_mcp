# SPDX-License-Identifier: MIT
"""Payroll and bookkeeping windows; the payroll calendar. v0.224.0 — docs/design/payroll_windows.md."""

import datetime
import json

import frappe

from erpnext_mcp import payroll_calendar, settings, switch_timer, switch_windows, tile_queries, tiles
from erpnext_mcp.errors import ToolError

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_security_217 import SecurityCase

TIM = "tim@example.test"


class Windows(SecurityCase):
	def on(self, tool):
		return settings.as_bool(frappe.get_single("ERPNext MCP Settings").get(f"allow_{tool}"))

	def test_the_payroll_window_opens_the_group_and_closes_by_itself(self):
		answer = switch_windows.open_window("payroll", None, TIM)
		self.assertIn("submit_payroll", answer["opened"])
		self.assertIn("generate_nacha_file", answer["opened"])
		self.assertTrue(self.on("post_payroll_to_gl"))
		until = datetime.datetime.fromisoformat(answer["expires_at"])
		self.assertAlmostEqual(
			(until - datetime.datetime.fromisoformat(str(frappe.utils.now())[:19])).total_seconds(),
			7200,
			delta=120,
		)
		self.assertTrue(
			[r for r in STORE.rows("MCP Action Log") if r.get("tool_name") == "switch:open_window"]
		)
		doc = frappe.get_single("ERPNext MCP Settings")
		timed = json.loads(doc.switch_expiry)
		doc.switch_expiry = json.dumps({tool: "2020-01-01 00:00:00" for tool in timed})
		doc.save(ignore_permissions=True)
		self.assertIn("submit_payroll", switch_timer.revert_expired())
		self.assertFalse(self.on("submit_payroll"))

	def test_filing_is_never_in_a_window_and_hand_switches_stay(self):
		self.configure(
			enabled=1,
			allow_render_pay_stub=1,
			switch_windows=json.dumps(
				{"payroll": ["render_pay_stub", "mark_tax_form_filed", "submit_payroll"]}
			),
		)
		answer = switch_windows.open_window("payroll", 30, TIM)
		self.assertEqual(answer["already_on"], ["render_pay_stub"])
		self.assertEqual(answer["never_in_a_window"], ["mark_tax_form_filed"])
		self.assertFalse(self.on("mark_tax_form_filed"))
		self.assertNotIn(
			"render_pay_stub", json.loads(frappe.get_single("ERPNext MCP Settings").switch_expiry)
		)

	def test_credential_tools_are_refused_and_limits_hold(self):
		self.configure(enabled=1, switch_windows=json.dumps({"keys": ["generate_api_token", "create_crop"]}))
		answer = switch_windows.open_window("keys", 10, TIM)
		self.assertEqual(answer["never_in_a_window"], ["generate_api_token"])
		with self.assertRaises(ToolError):
			switch_windows.open_window("payroll", 999, TIM)
		with self.assertRaises(ToolError):
			switch_windows.open_window("nonsense", 10, TIM)

	def test_bookkeeping_and_no_mcp_tool_opens_a_window(self):
		from erpnext_mcp import registry

		self.assertIn("submit_journal_entry", switch_windows.open_window("bookkeeping", 60, TIM)["opened"])
		self.assertFalse(
			[name for name in registry.TOOLS if "open_window" in name or "switch_window" in name]
		)


class Calendar(SecurityCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, payroll_calendar_enabled=1, security_alert_email="sec@example.test")

	def test_quarterly_and_annual_dates(self):
		rows = payroll_calendar._filings(MAIN, 2026)
		by = {r["name"]: r for r in rows}
		self.assertEqual(by[f"941:{MAIN}:2026Q3"]["due_date"], "2026-11-02")  # 31 Oct 2026 is a Saturday
		self.assertEqual(by[f"annual:{MAIN}:2026:W-2"]["due_date"], "2027-02-01")  # 31 Jan 2027 is a Sunday
		self.assertIn("943", " ".join(r["title"] for r in rows))

	def test_a_filed_form_is_done(self):
		STORE.seed(
			"Tax Form",
			[
				{
					"name": "TF-1",
					"form_type": "941",
					"company": MAIN,
					"fiscal_year": 2026,
					"quarter": "Q3",
					"status": "Filed",
				}
			],
		)
		by = {r["name"]: r for r in payroll_calendar._filings(MAIN, 2026)}
		self.assertTrue(by[f"941:{MAIN}:2026Q3"]["done"])
		self.assertFalse(by[f"oq:{MAIN}:2026Q3"]["done"])

	def test_the_tile_is_hidden_while_off_and_counts_what_is_due(self):
		body = tiles.PAYROLL_TILES["payroll_calendar"]
		person = {"user": TIM, "roles": ["HR Manager"]}
		set_roles(TIM, ["HR Manager"])
		self.assertEqual(tiles.hidden_reason(body, person, MAIN, "0.31.0", {}), "")
		self.configure(enabled=1, payroll_calendar_enabled=0)
		self.assertIn("switched off", tiles.hidden_reason(body, person, MAIN, "0.31.0", {}))
		self.assertEqual(tile_queries.run("payroll_calendar", TIM, MAIN)["count"], 0)

	def test_reminders_once_per_lead(self):
		STORE.seed("User", [{"name": TIM, "enabled": 1, "email": TIM}])
		self.configure(
			enabled=1,
			payroll_calendar_enabled=1,
			payroll_reminder_recipients=TIM,
			payroll_reminder_days="400",
		)
		STORE.emails.clear()
		first = payroll_calendar.send_reminders()
		self.assertEqual(payroll_calendar.send_reminders(), [])
		self.assertEqual(len(STORE.emails), len(first))
		for mail in STORE.emails:
			self.assertIn("Paying and filing are yours", mail["message"])
		self.configure(enabled=1, payroll_calendar_enabled=0)
		self.assertEqual(payroll_calendar.send_reminders(), [])
