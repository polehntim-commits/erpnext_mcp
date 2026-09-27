# SPDX-License-Identifier: MIT
"""v0.196.0 — which AI model made a call, on the audit row both transports write.

`X-Agent-Model` and `X-Agent-Session` land on MCP Action Log's `agent_model` and
`agent_session`. Asserted on both doors — MCP JSON-RPC and the phone's REST
routes — because each writes its row through a different caller of
`audit.record`, and on the two ways a header read can go wrong, because the
attribution must never cost the audit row itself.
"""

from erpnext_mcp import audit

from .fixtures import SeededTestCase
from .harness import STORE, frappe
from .test_farmops_api import CONTEXT, FarmOpsAPITestCase

MODEL = "claude-opus-4-6"
SESSION = "conv-7f3a"


class TheMcpDoor(SeededTestCase):
	def test_the_model_and_session_are_on_the_row(self):
		self.tool_data("list_fiscal_years", headers={"X-Agent-Model": MODEL, "X-Agent-Session": SESSION})
		row = self.assertAudited("list_fiscal_years")
		self.assertEqual(row["agent_model"], MODEL)
		self.assertEqual(row["agent_session"], SESSION)

	def test_no_header_is_the_row_it_always_was(self):
		self.tool_data("list_fiscal_years")
		row = self.assertAudited("list_fiscal_years")
		self.assertFalse(row.get("agent_model"))
		self.assertFalse(row.get("agent_session"))

	def test_a_model_alone_is_fine(self):
		self.tool_data("list_fiscal_years", headers={"X-Agent-Model": "llama-3.1-8b-q4"})
		row = self.assertAudited("list_fiscal_years")
		self.assertEqual(row["agent_model"], "llama-3.1-8b-q4")
		self.assertFalse(row.get("agent_session"))

	def test_a_failed_call_is_attributed_too(self):
		self.tool_error("get_journal_entry", {"name": "ACC-JV-9999-1"}, headers={"X-Agent-Model": MODEL})
		row = self.assertAudited("get_journal_entry", status="Error")
		self.assertEqual(row["agent_model"], MODEL)

	def test_an_agent_sessions_calls_are_queryable(self):
		for tool in ("list_fiscal_years", "get_company_topology"):
			self.tool_data(tool, headers={"X-Agent-Model": MODEL, "X-Agent-Session": SESSION})
		self.tool_data("list_fiscal_years", headers={"X-Agent-Model": "llama-3.1-8b-q4"})
		data = self.tool_data(
			"query_doctype",
			{
				"doctype": "MCP Action Log",
				"filters": {"agent_session": SESSION},
				"fields": ["tool_name", "agent_model", "agent_session"],
			},
		)
		rows = data["records"]
		self.assertEqual(
			sorted(row["tool_name"] for row in rows), ["get_company_topology", "list_fiscal_years"]
		)
		self.assertEqual({row["agent_model"] for row in rows}, {MODEL})

	def test_the_header_is_cleaned_and_capped(self):
		self.tool_data("list_fiscal_years", headers={"X-Agent-Model": "  gpt\x00-x\t" + "y" * 400})
		value = self.assertAudited("list_fiscal_years")["agent_model"]
		self.assertTrue(value.startswith("gpt-x"))
		self.assertEqual(len(value), 140)
		self.assertTrue(value.isprintable())


class ThePhoneDoor(FarmOpsAPITestCase):
	def test_a_phone_route_is_attributed(self):
		self.message(CONTEXT, headers={"X-Agent-Model": MODEL, "X-Agent-Session": SESSION})
		row = self.audit_rows("get_current_user_context")[-1]
		self.assertEqual(row["agent_model"], MODEL)
		self.assertEqual(row["agent_session"], SESSION)

	def test_a_phone_without_the_header_is_unchanged(self):
		self.message(CONTEXT)
		self.assertFalse(self.audit_rows("get_current_user_context")[-1].get("agent_model"))


class TheRowIsNeverTheCost(SeededTestCase):
	def test_no_request_at_all_still_writes_the_row(self):
		"""A scheduled job has no request; `get_request_header` has nothing to read."""
		frappe.local.request = None
		name = audit.record("scheduled_sweep", {})
		self.assertTrue(name)
		self.assertFalse(STORE.get_raw("MCP Action Log", name).get("agent_model"))

	def test_a_header_read_that_raises_still_writes_the_row(self):
		original = frappe.get_request_header

		def boom(*_a, **_kw):
			raise RuntimeError("proxy ate the headers")

		frappe.get_request_header = boom
		try:
			name = audit.record("list_fiscal_years", {})
		finally:
			frappe.get_request_header = original
		self.assertTrue(name)
		self.assertFalse(STORE.get_raw("MCP Action Log", name).get("agent_model"))

	def test_the_columns_are_filterable_in_the_desk(self):
		import json
		from pathlib import Path

		path = (
			Path(audit.__file__).parent / "erpnext_mcp" / "doctype" / "mcp_action_log" / "mcp_action_log.json"
		)
		fields = {f["fieldname"]: f for f in json.loads(path.read_text())["fields"]}
		for name in ("agent_model", "agent_session"):
			self.assertTrue(fields[name].get("in_standard_filter"), name)
			self.assertTrue(fields[name].get("search_index"), name)
			self.assertTrue(fields[name].get("read_only"), name)
