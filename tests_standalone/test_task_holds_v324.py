# SPDX-License-Identifier: MIT
"""v0.324.0 — On Hold: a Farm Task nobody should pick up yet, and why.

AFB-2026-00034: the Bing removal parent sat Claimed for a week while its notes said ON HOLD until the land transfer
closes. Now a hold is a state with a reason; held work cannot be claimed or started; release puts it back.
"""

import frappe

from erpnext_mcp import task_holds
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.api import shape
from erpnext_mcp.tools import dispatch

from . import test_task_locations_v323 as tl
from .harness import STORE, set_roles

ON = {**tl.ON, "allow_hold_farm_task": 1, "allow_release_farm_task": 1, "allow_claim_farm_task": 1,
      "allow_document": 1}
LAND = "Mill Creek land transfer closes"


class HoldCase(tl.BingCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		STORE.seed("Farm Task", [
			{"name": "FT-STEP-1", "task_name": "Flag the Bing rows", "task_type": "Maintenance", "state": "Available",
			 "company": tl.tf.MAIN, "parent_doctype": "Farm Task", "parent_task": "FT-BING",
			 "evidence_required": '{"findings_text": true}'},
			{"name": "FT-STEP-2", "task_name": "Pull stumps", "task_type": "Maintenance", "state": "Draft",
			 "company": tl.tf.MAIN, "parent_doctype": "Farm Task", "parent_task": "FT-BING",
			 "evidence_required": '{"findings_text": true}'},
		])
		STORE.commit()

	def hold(self, **extra):
		return self.tool_data("hold_farm_task", {"task": "FT-BING", "hold_reason": "Waiting on the land transfer",
		                                          "hold_until_event": LAND, **extra})


class PuttingItOnHold(HoldCase):
	def test_hold_records_why_and_until_what(self):
		data = self.hold()
		row = STORE.get_raw("Farm Task", "FT-BING")
		self.assertEqual(row["state"], "On Hold")
		self.assertEqual(row["hold_until_event"], LAND)
		self.assertEqual(row["held_from_state"], "Available")
		self.assertEqual(data["task_detail"]["hold"]["reason"], "Waiting on the land transfer")

	def test_a_reason_is_required(self):
		self.assertIn("hold_reason", self.tool_error("hold_farm_task", {"task": "FT-BING", "hold_reason": " "}))
		self.assertEqual(STORE.get_raw("Farm Task", "FT-BING")["state"], "Available")

	def test_cascade_holds_the_steps(self):
		data = self.hold(cascade=True)
		self.assertEqual(data["held_count"], 3)
		self.assertEqual(STORE.get_raw("Farm Task", "FT-STEP-2")["held_from_state"], "Draft")

	def test_holding_twice_changes_nothing(self):
		self.hold()
		self.assertEqual(self.hold()["held_count"], 0)

	def test_a_finished_task_is_not_held(self):
		frappe.db.set_value("Farm Task", "FT-BING", "state", "Completed")
		self.assertIn("finished", self.tool_error("hold_farm_task", {"task": "FT-BING", "hold_reason": "x"}))


class HeldWorkIsNotPickedUp(HoldCase):
	def test_claim_and_assign_are_refused_with_the_reason(self):
		self.hold()
		message = self.tool_error("claim_farm_task", {"task": "FT-BING", "worker_id": "EMP-001"})
		self.assertIn("on hold", message)
		self.assertIn(LAND, message)
		self.assertIn("on hold", self.tool_error("assign_farm_task", {"task": "FT-BING", "worker_id": "EMP-001"}))

	def test_a_live_claim_ends_as_held_not_rejected(self):
		STORE.seed("Farm Task Assignment", [{"name": "FTA-1", "task": "FT-BING", "assigned_to": "EMP-001",
		                                     "assigned_to_name": "Ana", "state": "Claimed",
		                                     "claimed_at": "2026-10-03 08:00:00"}])
		frappe.db.set_value("Farm Task", "FT-BING", "state", "Claimed")
		STORE.commit()
		data = self.hold()
		self.assertEqual(data["tasks"][0]["assignments_ended"][0]["assignment"], "FTA-1")
		self.assertEqual(STORE.get_raw("Farm Task Assignment", "FTA-1")["state"], "Held")
		self.assertEqual(STORE.get_raw("Farm Task", "FT-BING")["held_from_state"], "Claimed")

	def test_the_pool_and_the_board(self):
		self.hold()
		pool = self.tool_data("list_available_tasks", {})["tasks"]
		self.assertNotIn("FT-BING", [t["name"] for t in pool])
		board = self.tool_data("list_dispatch_board", {})
		self.assertEqual([t["name"] for t in board["columns"]["On Hold"]], ["FT-BING"], "its own column")
		only = self.tool_data("list_dispatch_board", {"on_hold": True})
		self.assertEqual(list(only["columns"]), ["On Hold"])
		without = self.tool_data("list_dispatch_board", {"on_hold": False})
		self.assertNotIn("On Hold", without["columns"])


class Releasing(HoldCase):
	def test_back_to_the_pool_or_draft(self):
		self.hold(cascade=True)
		data = self.tool_data("release_farm_task", {"task": "FT-BING", "cascade": True})
		self.assertEqual(data["released_count"], 3)
		self.assertEqual(STORE.get_raw("Farm Task", "FT-BING")["state"], "Available")
		self.assertEqual(STORE.get_raw("Farm Task", "FT-STEP-2")["state"], "Draft")

	def test_through_the_document_tool(self):
		self.tool_data("document", {"action": "hold", "type": "farm_task", "name": "FT-BING",
		                            "body": {"hold_reason": "Constancy go-live"}})
		self.assertEqual(STORE.get_raw("Farm Task", "FT-BING")["state"], "On Hold")
		self.tool_data("document", {"action": "release", "type": "farm_task", "name": "FT-BING"})
		self.assertEqual(STORE.get_raw("Farm Task", "FT-BING")["state"], "Available")

	def test_the_date_releases_it_and_only_when_asked(self):
		self.hold(hold_until_date="2026-01-01", auto_release=True)
		frappe.db.set_value("Farm Task", "FT-STEP-1", {"state": "On Hold", "hold_reason": "x",
		                                               "hold_until_date": "2026-01-01", "hold_auto_release": 0})
		report = task_holds.auto_release_due()
		self.assertEqual(report["released"], ["FT-BING"])
		self.assertEqual(STORE.get_raw("Farm Task", "FT-STEP-1")["state"], "On Hold")

	def test_auto_release_needs_a_date(self):
		self.assertIn("hold_until_date", self.tool_error("hold_farm_task", {"task": "FT-BING", "hold_reason": "x",
		                                                                    "auto_release": True}))


class BornOnHold(HoldCase):
	def test_create_farm_task_on_hold(self):
		made = self.tool_data("create_farm_task", {"task_name": "Burn piles", "task_type": "Maintenance",
		                                           "evidence_required": {"findings_text": True}, "company": tl.tf.MAIN,
		                                           "on_hold": {"reason": "Burn placeholder", "until_event": "burn season"}})
		self.assertEqual(frappe.db.get_value("Farm Task", made["name"], "state"), "On Hold")

	def test_a_hold_without_a_reason_creates_nothing(self):
		before = len(STORE.rows("Farm Task"))
		self.tool_error("create_farm_task", {"task_name": "X", "task_type": "Maintenance",
		                                     "evidence_required": {"findings_text": True}, "on_hold": {}})
		self.assertEqual(len(STORE.rows("Farm Task")), before)


class FromThePhone(HoldCase):
	def test_a_foreman_holds_and_releases_and_everyone_sees_the_section(self):
		set_roles(tl.tf.WORKER, ["Field Worker", "Foreman"])
		self.be()
		mobile_api.hold_task(task="FT-BING", hold_reason="Waiting on the land transfer", hold_until_event=LAND)
		held = mobile_api.list_held_tasks()
		self.assertEqual([t["name"] for t in held["tasks"]], ["FT-BING"])
		self.assertEqual(held["tasks"][0]["hold"]["until_event"], LAND)
		self.assertEqual(mobile_api.release_task(task="FT-BING")["released_count"], 1)
		self.assertTrue(mobile_api.release_task(task="FT-BING")["already"], "a replay is harmless")

	def test_a_picker_may_read_but_not_hold(self):
		set_roles(tl.tf.WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.hold_task(task="FT-BING", hold_reason="x")
		self.assertEqual(mobile_api.list_held_tasks()["count"], 0)

	def test_the_task_payload_carries_the_hold(self):
		self.hold()
		phone = shape.task(dispatch._describe_task(dispatch.task_row("FT-BING")))
		self.assertEqual(phone["state"], "On Hold")
		self.assertEqual(phone["hold"]["until_event"], LAND)
