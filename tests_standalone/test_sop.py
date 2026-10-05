"""SOP review and approval. v0.245.0 (approved queue item 6; decisions 38, 40)."""

import json
from unittest import mock

import frappe

from erpnext_mcp import ccf_providers, go_hold, sop
from erpnext_mcp.tools import evidence

from .harness import STORE
from .test_go_hold import ON, GoHoldTestCase

TOOLS = {f"allow_{t}": 1 for t in ("list_configs", "get_config", "preview_config", "draft_config", "stage_config",
                                   "publish_config", "create_compliance_policy", "update_compliance_policy",
                                   "get_compliance_policy", "list_compliance_policies")}
RULES = "task_type: Spray = spray.boss@example.com, tim@example.com\n* = tim@example.com"


class SopTestCase(GoHoldTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, **TOOLS, "sop_approver_rules": RULES})
		STORE.seed("User", [{"name": u, "enabled": 1} for u in ("tim@example.com", "spray.boss@example.com", "crew@example.com")])
		frappe.set_user("Administrator")

	def an_sop(self, name="Copper spray SOP", covers=("Spray",), **extra):
		data = self.tool_data("draft_config", {"kind": "sop", "key": name, "fields": {
			"category": "Spray SOP", "version": "1", "covers_task_types": list(covers), **extra}})
		return data["draft"].get("name") or name

	def submitted(self, **kw):
		name = self.an_sop(**kw)
		self.tool_data("stage_config", {"kind": "sop", "key": name})
		return name

	def as_user(self, user, fn, *args):
		frappe.set_user(user)
		try:
			return fn(*args)
		finally:
			frappe.set_user("Administrator")


class Approvers(SopTestCase):
	def test_rules_parse_and_say_what_is_wrong(self):
		rules, problems = sop.parse_rules("task_type: Spray = a@x\nposition = b@y\nnonsense\n* = c@z")
		self.assertEqual([r["scope"] for r in rules], ["task_type", "*"])
		self.assertEqual(len(problems), 2)

	def test_submit_writes_the_approvers_for_what_it_covers(self):
		name = self.submitted()
		review = sop.describe(name)
		self.assertEqual(review["status"], "In Review")
		self.assertEqual([a["approver"] for a in review["approvers"]], ["spray.boss@example.com", "tim@example.com"])
		other = self.submitted(name="Ladder SOP", covers=("Maintenance",))
		self.assertEqual([a["approver"] for a in sop.describe(other)["approvers"]], ["tim@example.com"], "falls back to *")


class ApprovalIsAPersons(SopTestCase):
	def test_mcp_cannot_approve_or_set_the_status(self):
		name = self.submitted()
		self.assertIn("never over MCP", self.tool_error("publish_config", {"kind": "sop", "key": name}))
		self.assertIn("not set directly", self.tool_error("update_compliance_policy", {"policy": name, "status": "Approved"}))

	def test_every_required_approver_then_approved_with_a_signature_each(self):
		name = self.submitted()
		with self.assertRaisesRegex(PermissionError, "not an approver"):
			sop.approve(name, "crew@example.com")
		first = sop.approve(name, "tim@example.com")
		self.assertEqual((first["status"], first["waiting_on"]), ("In Review", ["spray.boss@example.com"]))
		done = sop.approve(name, "spray.boss@example.com")
		self.assertEqual(done["status"], "Approved")
		self.assertTrue(done["approved_now"])
		roles = [r["signature_role"] for r in STORE.rows("Signing Evidence") if r.get("document_name") == name]
		self.assertEqual(roles, ["Approver", "Approver"])
		self.assertTrue(evidence._describe_policy(dict(frappe.get_doc("Compliance Policy", name).as_dict()))["in_force"])

	def test_request_changes_sends_it_back_and_clears_approvals(self):
		name = self.submitted()
		sop.approve(name, "tim@example.com")
		back = sop.request_changes(name, "spray.boss@example.com", "Add the PPE list")
		self.assertEqual(back["status"], "Draft")
		self.assertIn("Add the PPE list", back["review_note"])
		self.assertFalse(any(a["approved_at"] for a in back["approvers"]))

	def test_an_approved_new_version_supersedes_the_old(self):
		old = self.submitted()
		sop.approve(old, "tim@example.com"); sop.approve(old, "spray.boss@example.com")
		new = self.submitted(name="Copper spray SOP v2", supersedes=old)
		sop.approve(new, "tim@example.com"); done = sop.approve(new, "spray.boss@example.com")
		self.assertEqual(done["superseded"], old)
		self.assertEqual(STORE.get_raw("Compliance Policy", old)["status"], "Superseded")


class TheGate(SopTestCase):
	def test_spray_work_holds_while_its_sop_is_in_review_and_goes_once_approved(self):
		self.a_rule(rule_id="go_hold_sop_approved")
		task = self.claimed(task_name="Copper, Block 3", task_type="Spray")
		name = self.submitted()
		verdict = go_hold.check(task)
		self.assertEqual(verdict["status"], "Hold")
		self.assertIn("SOP for this work is not approved", verdict["reasons"][0])
		self.assertEqual(verdict["rules"][0]["read"]["sop.unapproved_count"], 1)
		sop.approve(name, "tim@example.com"); sop.approve(name, "spray.boss@example.com")
		self.assertEqual(go_hold.check(task)["status"], "Go")
		# A new version in review does not un-approve the one in force.
		self.submitted(name="Copper spray SOP v2", supersedes=name)
		self.assertEqual(go_hold.check(task)["status"], "Go")

	def test_preview_names_the_approvers_and_the_work_it_covers(self):
		task = self.claimed(task_name="Copper, Block 3", task_type="Spray")
		name = self.an_sop()
		data = self.tool_data("preview_config", {"kind": "sop", "key": name})["preview"]
		self.assertEqual(data["would_be_approved_by"], ["spray.boss@example.com", "tim@example.com"])
		self.assertIn(task, [t["name"] for t in data["open_work_it_covers"]])
