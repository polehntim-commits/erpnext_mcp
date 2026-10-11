# SPDX-License-Identifier: MIT
"""v0.320.0 — MCP parity, part 2 (docs/design/mcp_native_gap_audit_2026_10.md §B4c).

The phone's reads land as optional arguments on the MCP tools that own each register; no new tools. The worker's
own bank, pay, sign-off and check-in calls stay on the phone (audit §A).
"""

from erpnext_mcp import business_cards, ipm_graph

from . import test_business_cards as tb
from . import test_contract_v0_262_0 as v262
from . import test_field_self_service as tf
from .fixtures import SPRAY, PurchasingTestCase, V12TestCase
from .harness import set_roles


class TheFieldCardOnGetField(tf.FieldCase):
	def test_include_card(self):
		data = self.tool_data("get_field", {"field": tf.CENTER, "include_card": True})
		self.assertEqual([h["name"] for h in data["card"]["hazards"]], ["MC-V1"])
		self.assertEqual([t["name"] for t in data["card"]["open_tasks"]], ["FT-OPEN"])

	def test_without_the_flag_there_is_no_card(self):
		self.assertNotIn("card", self.tool_data("get_field", {"field": tf.CENTER}))


class ThresholdStatusOnGetIpmOrganism(v262.ContractIPM):
	def test_a_count_answers_against_the_threshold(self):
		set_roles("Administrator", ["System Manager", "Farm Manager"])
		data = self.tool_data("get_ipm_organism", {"organism": "spider-mites", "block": v262.BLOCK, "count": 8,
		                                           "sample_unit": "Per Leaf"})
		self.assertEqual(data, ipm_graph.threshold_status("spider-mites", block=v262.BLOCK, count=8,
		                                                  sample_unit="Per Leaf"))

	def test_no_count_is_the_node_as_before(self):
		data = self.tool_data("get_ipm_organism", {"organism": "american-robin"})
		self.assertIn("edges", data)


class WhereMetOnSaveContact(tb.ContactSite, V12TestCase):
	def test_contact_and_met_at_without_a_card(self):
		self.configure(enabled=1, allow_save_contact=1)
		first = business_cards.save("Administrator", tb.SHEPPARD, {})
		data = self.tool_data("save_contact", {"contact": first["name"], "met_at": "Sheppard's, Hood River",
		                                       "met_on": "2026-10-05"})
		self.assertEqual((data["met_at"], data["met_on"]), ("Sheppard's, Hood River", "2026-10-05"))


class TheLabelOnGetItem(PurchasingTestCase):
	def test_include_label(self):
		self.configure(enabled=1)
		data = self.tool_data("get_item", {"item_code": SPRAY, "include_label": True})
		self.assertIn("label", data)
		self.assertNotIn("label", self.tool_data("get_item", {"item_code": SPRAY}))


class TheInboxOnTheCalendarForMe(V12TestCase):
	def test_inbox_is_the_acting_users(self):
		from unittest import mock

		from erpnext_mcp import compliance_loop
		from erpnext_mcp.tools import fieldwork

		me = {"user": "foreman@farm.test", "entity_access": ["A", "B"], "employee": "", "roles": []}
		with mock.patch.object(fieldwork, "_me", return_value=me), \
			mock.patch.object(compliance_loop, "inbox", return_value={"mine": ["ALERT-1"]}) as inbox:
			data = fieldwork.list_compliance_calendar_for_me({"inbox": True}).data
		inbox.assert_called_once_with("foreman@farm.test", "")
		self.assertEqual(data, {"mine": ["ALERT-1"]})


class TheTasksSOPs(V12TestCase):
	def test_current_sops_covering_the_task_type(self):
		from unittest import mock

		import frappe

		from erpnext_mcp import sop
		from erpnext_mcp.tools import fieldwork

		row = {"policy_name": "Thinning SOP", "version": 2, "status": sorted(sop.LIVE)[0], "attached_document": "/f"}
		with mock.patch.object(sop, "covering", return_value=[{"name": "POL-1"}]) as covering, \
			mock.patch.object(frappe.db, "get_value", return_value=row):
			out = fieldwork._task_sops({"name": "FT-1", "task_type": "Thinning"})
		covering.assert_called_once_with("Thinning")
		self.assertEqual(out, [{"policy": "POL-1", **row, "approved": True}])
