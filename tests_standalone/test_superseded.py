# SPDX-License-Identifier: MIT
"""Hiding what a generic tool covers, logging old-name calls, and who a call was for. v0.283.0 (consolidation
phase 5).

Off by default the tool list is exactly what it was. With `hide_superseded_tools` on, an old tool leaves the list
only while its generic tool is listed for the client, and stays callable by name. Every old-name call carries
Superseded By on its audit row; a call through a generic tool is one row under the generic name. The client program
and the person acted for land on every row.
"""

from erpnext_mcp import registry, superseded

from .fixtures import MAIN, MASTER_SUPPLIER, PurchasingTestCase

WRITES = ("create_supplier", "update_supplier")


def listed():
	return {tool["name"] for tool in registry.tools_list()["tools"]}


class TheMap(PurchasingTestCase):
	def test_it_is_what_the_generic_tools_run(self):
		m = superseded.covering()
		self.assertEqual(m["create_supplier"], "record")
		self.assertEqual(m["submit_purchase_order"], "document")
		self.assertEqual(m["create_farm_task_template"], "template")
		self.assertEqual(m["publish_config"], "config")
		self.assertEqual(m["create_job_link"], "share_link")
		for generic in ("record", "document", "template", "config", "share_link"):
			self.assertNotIn(generic, m)
		for kept in ("create_employee", "submit_journal_entry", "run_payroll_for_period", "get_company_topology"):
			self.assertNotIn(kept, m)


class Hiding(PurchasingTestCase):
	def test_off_by_default_the_list_is_unchanged(self):
		self.configure(enabled=1, allow_record=1, **{f"allow_{n}": 1 for n in WRITES})
		self.assertIn("create_supplier", listed())
		self.assertIn("get_supplier", listed())

	def test_on_hides_only_what_a_listed_generic_tool_covers(self):
		self.configure(enabled=1, hide_superseded_tools=1, **{f"allow_{n}": 1 for n in WRITES})
		self.assertIn("create_supplier", listed(), "record is off, so nothing it covers is hidden")
		self.configure(enabled=1, hide_superseded_tools=1, allow_record=1, **{f"allow_{n}": 1 for n in WRITES})
		names = listed()
		self.assertNotIn("create_supplier", names)
		self.assertNotIn("get_supplier", names)
		self.assertIn("record", names)
		self.assertIn("get_company_topology", names)

	def test_a_hidden_tool_is_still_callable_by_name(self):
		self.configure(enabled=1, hide_superseded_tools=1, allow_record=1, **{f"allow_{n}": 1 for n in WRITES})
		self.assertEqual(self.tool_data("get_supplier", {"name": MASTER_SUPPLIER})["name"], MASTER_SUPPLIER)
		made = self.tool_data("create_supplier", {"supplier_name": "Wilbur-Ellis", "company": MAIN})
		self.assertEqual(made["name"], "Wilbur-Ellis")


class TheLog(PurchasingTestCase):
	def test_an_old_name_call_says_what_covers_it(self):
		self.tool_data("get_supplier", {"name": MASTER_SUPPLIER})
		self.assertEqual(self.assertAudited("get_supplier")["superseded_by"], "record")
		self.tool_data("get_company_topology")
		self.assertFalse(self.assertAudited("get_company_topology").get("superseded_by"))

	def test_a_call_through_the_generic_tool_is_one_row_under_its_name(self):
		self.configure(enabled=1, allow_record=1)
		self.tool_data("record", {"action": "get", "type": "supplier", "name": MASTER_SUPPLIER})
		row = self.assertAudited("record")
		self.assertFalse(row.get("superseded_by"))
		self.assertEqual(self.audit_rows(tool_name="get_supplier"), [])

	def test_the_client_and_the_person_acted_for(self):
		self.tool_data("get_company_topology", headers={"User-Agent": "claude-code/2.1", "X-Acting-For": "tim@example.test"})
		row = self.assertAudited("get_company_topology")
		self.assertEqual(row["agent_client"], "claude-code/2.1")
		self.assertEqual(row["acting_for"], "tim@example.test")

	def test_no_headers_no_columns(self):
		self.tool_data("get_company_topology")
		row = self.assertAudited("get_company_topology")
		self.assertFalse(row.get("agent_client"))
		self.assertFalse(row.get("acting_for"))
