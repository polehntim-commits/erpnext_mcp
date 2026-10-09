# SPDX-License-Identifier: MIT
"""One tool for every outside link. v0.282.0 (consolidation phase 4).

`share_link` runs each kind's existing tool under its own switch: the job link and the packer credential are shown
once and hashed as before, the view logs are the kinds' own, and the per-kind tools are untouched.
"""

import json

import frappe

from erpnext_mcp import job_links, registry

from .harness import STORE
from .test_job_links import JobCase
from .test_packer_portal import PortalCase

JOB_WRITES = ("create_job_link", "extend_job_link", "revoke_job_link")
PACKER_WRITES = ("issue_packer_credential", "revoke_packer_access")


class Discovery(PortalCase):
	def test_kinds_aliases_and_what_name_means(self):
		self.configure(enabled=1, allow_share_link=1)
		got = self.tool_data("share_link", {"action": "kinds"})
		self.assertEqual({k["kind"] for k in got["kinds"]}, {"job", "packer", "upload", "enrollment"})
		job = self.tool_data("share_link", {"action": "describe", "kind": "contractor"})
		self.assertEqual(job["kind"], "job")
		self.assertEqual(job["calls"]["create"], "create_job_link")
		self.assertIn("days", job["arguments"]["create"])
		for tools in job["calls"].values():
			self.assertIn(tools, registry.TOOLS)

	def test_an_action_a_kind_lacks_is_refused_with_what_it_has(self):
		self.configure(enabled=1, allow_share_link=1)
		message = self.tool_error("share_link", {"action": "extend", "kind": "enrollment", "name": "a@b.test"})
		self.assertIn("have create", message)
		self.assertIn("kind is one of", self.tool_error("share_link", {"action": "list", "kind": "rocket"}))


class Packer(PortalCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_share_link=1, **{f"allow_{n}": 1 for n in PACKER_WRITES})

	def test_credential_shown_once_get_log_revoke(self):
		self.on()
		made = self.tool_data("share_link", {"action": "create", "kind": "packer", "name": self.share,
		                                     "contact_name": "Dana at OVF", "contact_email": "dana@ovf.example"})
		self.assertIn("token", json.dumps(made).lower())
		share = self.tool_data("share_link", {"action": "get", "kind": "packer", "name": self.share})
		self.assertEqual(share["credentials"][0]["contact"], "Dana at OVF")
		self.assertNotIn("token_hash", json.dumps(share))
		self.tool_data("share_link", {"action": "log", "kind": "packer", "name": self.share})
		self.tool_data("share_link", {"action": "revoke", "kind": "packer", "name": self.share,
		                              "contact_name": "Dana at OVF", "reason": "left OVF"})
		again = self.tool_data("share_link", {"action": "get", "kind": "packer", "name": self.share})
		self.assertTrue(again["credentials"][0].get("revoked"))

	def test_both_switches_are_needed(self):
		self.on()
		self.configure(enabled=1, allow_share_link=1)
		message = self.tool_error("share_link", {"action": "create", "kind": "packer", "name": self.share,
		                                         "contact_name": "Dana"})
		self.assertIn("issue_packer_credential", message)
		self.configure(enabled=1, **{f"allow_{n}": 1 for n in PACKER_WRITES})
		self.assertIn("share_link", self.tool_error("share_link", {"action": "kinds"}))


class Job(JobCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_share_link=1, **{f"allow_{n}": 1 for n in JOB_WRITES})

	def test_create_list_extend_revoke_a_contractor_link(self):
		self.share_on()
		job_links.mark_ready(self.job, "tim@example.test", override_reason="crew finished early")
		STORE.commit()
		made = self.tool_data("share_link", {"action": "create", "kind": "contractor", "name": self.job})
		self.assertIn("/farmops/api/job/", made["url"])
		link = made["link"]
		listed = self.tool_data("share_link", {"action": "list", "kind": "job", "name": self.job})
		self.assertIn(link, json.dumps(listed))
		self.tool_data("share_link", {"action": "extend", "kind": "job", "name": link, "days": 3})
		self.tool_data("share_link", {"action": "revoke", "kind": "job", "name": link, "reason": "done"})
		self.assertTrue(frappe.db.get_value("Contractor Job Link", link, "revoked")
		                or frappe.db.get_value("Contractor Job Link", link, "status") == "Revoked")

	def test_the_old_tools_are_untouched(self):
		for name in (*JOB_WRITES, "list_job_links", *PACKER_WRITES, "request_upload_url", "issue_enrollment_link"):
			self.assertIn(name, registry.TOOLS)
		self.assertIn("share_link", registry.MUTATING_TOOLS)
