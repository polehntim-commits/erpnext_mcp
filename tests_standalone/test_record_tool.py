# SPDX-License-Identifier: MIT
"""One tool for the registers, one for documents' state changes. v0.281.0 (consolidation phase 3).

`record` and `document` read their types from the registry's own tool names and run each type's existing tool under
its own switch, so the type's validation and answer hold; money, HR and security stay off them; the old tools are
untouched.
"""

from erpnext_mcp import registry
from erpnext_mcp.tools import record_tool

from .fixtures import MAIN, MASTER_SUPPLIER, SPRAY, STORES, PurchasingTestCase
from .harness import STORE

WRITES = ("create_supplier", "update_supplier", "create_purchase_order", "submit_purchase_order")


class RecordToolCase(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_record=1, allow_document=1, **{f"allow_{n}": 1 for n in WRITES})

	def r(self, **args):
		return self.tool_data("record", args)

	def d(self, **args):
		return self.tool_data("document", args)


class TheTypesComeFromTheRegistry(RecordToolCase):
	def test_registers_and_documents_are_listed_with_their_actions(self):
		types = {t["type"]: t["actions"] for t in self.r(action="types")["types"]}
		self.assertEqual(types["supplier"], ["create", "get", "list", "update"])
		self.assertIn("field", types)
		docs = {t["type"]: t["actions"] for t in self.d(action="types")["types"]}
		self.assertIn("submit", docs["purchase_order"])
		self.assertIn("approve", docs["leave_request"])

	def test_money_hr_and_security_stay_on_their_own_tools(self):
		records, documents = record_tool.tables()
		for kept in ("employee", "i9_form", "journal_entry", "payment_entry", "bank_account", "garnishment",
		             "employee_bank_account", "api_token", "state_tax_config", "accounting_period"):
			self.assertNotIn(kept, records)
			self.assertNotIn(kept, documents)
		self.assertNotIn("payroll", documents)
		for tools in [*records.values(), *documents.values()]:
			for tool in tools.values():
				self.assertIn(tool, registry.TOOLS)

	def test_describe_shows_each_actions_arguments(self):
		got = self.r(action="describe", type="supplier")
		self.assertEqual(got["calls"]["create"], "create_supplier")
		self.assertIn("supplier_name", got["arguments"]["create"]["properties"])

	def test_an_unknown_type_or_action_is_refused_with_the_way_out(self):
		self.assertIn("action=types", self.tool_error("record", {"action": "get", "type": "spaceship", "name": "x"}))
		self.assertIn("has no delete", self.tool_error("record", {"action": "delete", "type": "supplier", "name": "x"}))
		self.assertIn("action is one of", self.tool_error("document", {"action": "explode", "type": "purchase_order"}))


class TheTypesOwnToolDoesTheWork(RecordToolCase):
	def test_create_get_update_list_a_supplier(self):
		made = self.r(action="create", type="supplier", body={"supplier_name": "Wilbur-Ellis"})
		self.assertEqual(made["name"], "Wilbur-Ellis")
		self.assertIsNotNone(STORE.get_raw("Supplier", "Wilbur-Ellis"))
		self.assertEqual(self.r(action="get", type="supplier", name="Wilbur-Ellis")["name"], "Wilbur-Ellis")
		self.r(action="update", type="Supplier", name=MASTER_SUPPLIER, body={"disabled": True})
		self.assertTrue(STORE.get_raw("Supplier", MASTER_SUPPLIER)["disabled"])
		listed = self.r(action="list", type="supplier", filters={"limit": 50})
		self.assertTrue(listed)

	def test_the_types_own_validation_answers(self):
		message = self.tool_error("record", {"action": "create", "type": "supplier",
		                                     "body": {"supplier_name": "X", "supplier_type": "Partnership"}})
		self.assertIn("Partnership", message)

	def test_submit_a_purchase_order(self):
		po = self.tool_data("create_purchase_order", {
			"company": MAIN, "supplier": MASTER_SUPPLIER, "schedule_date": "2026-09-01",
			"items": [{"item_code": SPRAY, "qty": 10, "rate": 12.5, "warehouse": STORES}]})
		got = self.d(action="submit", type="purchase_order", name=po["name"])
		self.assertEqual(got["docstatus"], 1)


class EachSwitchStillHolds(RecordToolCase):
	def test_the_generic_switch_and_the_types_own_switch_are_both_needed(self):
		self.configure(enabled=1, allow_record=1, allow_document=1)
		message = self.tool_error("record", {"action": "create", "type": "supplier", "body": {"supplier_name": "Y"}})
		self.assertIn("create_supplier", message)
		self.assertIsNone(STORE.get_raw("Supplier", "Y"))
		self.configure(enabled=1, **{f"allow_{n}": 1 for n in WRITES})
		self.assertIn("record", self.tool_error("record", {"action": "types"}))  # off without allow_record

	def test_the_old_tools_are_untouched(self):
		for name in ("create_supplier", "update_supplier", "submit_purchase_order", "approve_leave_request"):
			self.assertIn(name, registry.TOOLS)
		self.assertIn("record", registry.MUTATING_TOOLS)
		self.assertIn("document", registry.MUTATING_TOOLS)
