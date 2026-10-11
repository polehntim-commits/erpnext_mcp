# SPDX-License-Identifier: MIT
"""v0.319.0 — MCP parity, part 1 (docs/design/mcp_native_gap_audit_2026_10.md §B4).

No new tools. The phone-only capabilities land as arguments on the tools that already own the register, and the
registers that had only a create tool get get / list / update through `record`, which runs the two generic tools
already in the catalogue (query_doctype, update_document) under their own switches and rules.
"""

import frappe

from erpnext_mcp import superseded
from erpnext_mcp.tools import record_tool

from . import test_employee as te
from . import test_vehicle_titles as tv
from . import test_wave3_compliance_binseal as tw
from .fixtures import MAIN, SPRAY, PurchasingTestCase, V12TestCase
from .harness import STORE, set_roles
from .test_update_document import EVIDENCE, rows

TASK = "FT-PARITY-1"


class RecordReachesTheCreateOnlyRegisters(V12TestCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Farm Task", [{"name": TASK, "task_name": "Prune block 4", "task_type": "Repair",
		                          "state": "Draft", "notes": "old", "company": MAIN,
		                          "evidence_required": EVIDENCE}])
		self.configure(enabled=1, allow_record=1)

	def test_types_name_the_generic_actions(self):
		types = {t["type"]: t for t in self.tool_data("record", {"action": "types"})["types"]}
		self.assertIn("update", types["farm_task"]["actions"])
		self.assertIn("list", types["farm_task"]["actions"])
		self.assertEqual(types["farm_task"]["generic"], ["list", "update"])
		self.assertNotIn("generic", types["supplier"])

	def test_every_generic_type_has_a_create_and_is_not_money_hr_or_security(self):
		records, _ = record_tool.tables()
		for type_ in record_tool.GENERIC:
			self.assertFalse(record_tool.SPECIAL.search(type_), type_)
			if type_ in records:
				self.assertIn("create", records[type_], type_)

	def test_update_runs_update_document_and_its_whitelist(self):
		message = self.tool_error("record", {"action": "update", "type": "farm_task", "name": TASK,
		                                     "body": {"notes": "new"}})
		self.assertIn("update_document", message)
		self.configure(enabled=1, allow_record=1, allow_update_document=1,
		               update_document_fields=rows(("Farm Task", "notes")))
		data = self.tool_data("record", {"action": "update", "type": "farm_task", "name": TASK,
		                                 "body": {"notes": "new"}})
		self.assertEqual(data["via"], "update_document")
		self.assertEqual(STORE.get_raw("Farm Task", TASK)["notes"], "new")
		refused = self.tool_error("record", {"action": "update", "type": "farm_task", "name": TASK,
		                                     "body": {"task_name": "x"}})
		self.assertIn("task_name", refused)

	def test_list_runs_query_doctype(self):
		data = self.tool_data("record", {"action": "list", "type": "farm_task", "filters": {"company": MAIN}})
		self.assertEqual(data["via"], "query_doctype")
		self.assertIn(TASK, [row["name"] for row in data["rows"]])

	def test_the_types_own_tool_still_wins(self):
		self.assertEqual(record_tool._generic(record_tool.tables()[0], "farm_task").get("get"), None)

	def test_the_two_generic_tools_are_never_called_superseded(self):
		superseded._CACHE.clear()
		covering = superseded.covering()
		self.assertNotIn("query_doctype", covering)
		self.assertNotIn("update_document", covering)


class TheBarcodeIsOnTheItemTools(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_update_item=1)

	def test_add_then_find(self):
		self.assertFalse(self.tool_data("get_item", {"barcode": "012345678905"})["found"])
		self.tool_data("update_item", {"item_code": SPRAY, "barcode": "012345678905"})
		found = self.tool_data("get_item", {"barcode": "012345678905"})
		self.assertTrue(found["found"])
		self.assertEqual(found["item"]["name"] if "name" in found["item"] else found["item"]["item_code"], SPRAY)

	def test_a_barcode_with_another_change_does_both(self):
		data = self.tool_data("update_item", {"item_code": SPRAY, "barcode": "012345678905",
		                                      "description": "Copper, 50WP"})
		self.assertEqual(data["barcode"]["barcode"], "012345678905")
		self.assertIn("description", data["changed"])


class RehireOnUpdateEmployee(te.EmployeeTestCase):
	def test_a_foreman_rehires_through_update_employee(self):
		set_roles("Administrator", ["Farm Manager"])
		created = self.create()["employee"]
		frappe.db.set_value("Employee", created, "status", "Left")
		set_roles("Administrator", ["Foreman"])
		self.tool_data("update_employee", {"employee": created, "reactivate": True})
		self.assertEqual(frappe.db.get_value("Employee", created, "status"), "Active")

	def test_nothing_else_rides_along(self):
		set_roles("Administrator", ["Farm Manager"])
		created = self.create()["employee"]
		message = self.tool_error("update_employee", {"employee": created, "reactivate": True,
		                                              "department": "Harvest"})
		self.assertIn("department", message)


class TheTitleIsFiledFromTheAssetTool(tv.TitleTestCase):
	def test_title_receipt_on_update_registered_asset(self):
		self.configure(enabled=1, allow_update_registered_asset=1, **tv.ON)
		self.a_vehicle()
		name = self.a_title(vin=tv.VIN)["name"]
		data = self.tool_data("update_registered_asset", {"asset_name": "MC-Truck-01", "title_receipt": name})
		self.assertEqual(data["title_receipt"], name)
		self.assertEqual(self.asset("MC-Truck-01")["title_receipt"], name)


class TheShiftsBreaksOnGetBreakPolicy(tw.Wave3TestCase):
	def test_shift_answers_with_due_times(self):
		policy = self.a_policy()
		name = self.a_shift(start_datetime=self.at(6), crew_employees=[])["name"]
		frappe.db.set_value("Farm Shift", name, "break_policy", policy)
		data = self.tool_data("get_break_policy", {"shift": name, "planned_hours": 8})
		self.assertTrue(data["breaks"])
		self.assertIn("due_at", data["breaks"][0])


class TheCertificateRouteIsOptIn(V12TestCase):
	def test_as_certificate_is_for_training_records_only(self):
		self.configure(enabled=1, allow_attach_file_to_document=1)
		message = self.tool_error("attach_file_to_document", {"doctype": "Supplier", "name": "x",
		                                                      "file_name": "a.pdf", "as_certificate": True})
		self.assertIn("as_certificate is for", message)
