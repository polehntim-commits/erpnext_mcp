# SPDX-License-Identifier: MIT
"""Which address emailed links carry. v0.223.3 — OML's reset link said http://frontend/…"""

import frappe

from erpnext_mcp import security_status, site_url

from .fixtures import SeededTestCase

DESK = "https://orchardmeadow-umbrel.tail2b0bb0.ts.net:8443"
FUNNEL = "https://orchardmeadow-umbrel.tail2b0bb0.ts.net"


class EmailedLinks(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url=DESK, farmops_public_url=FUNNEL)
		self._site = getattr(frappe.local, "site", None)
		frappe.local.site = "frontend"
		self.addCleanup(lambda: setattr(frappe.local, "site", self._site))

	def test_unset_host_name_fails_with_the_exact_command(self):
		frappe.conf.pop("host_name", None)
		found = site_url.status()
		self.assertFalse(found["ok"])
		self.assertIn("site name", found["problems"][0])
		self.assertEqual(
			found["fix"],
			"sudo docker exec -u frappe -w /home/frappe/frappe-bench fafo-erpnext_server_1 "
			f"bench --site frontend set-config host_name {DESK}",
		)
		self.assertEqual(security_status._emailed_links()["status"], "fail")

	def test_the_site_name_and_loopback_fail(self):
		for bad in ("http://frontend", "frontend", "http://127.0.0.1:8080"):
			frappe.conf["host_name"] = bad
			self.assertTrue(site_url.status()["problems"], bad)

	def test_the_funnel_and_a_bare_ip_warn(self):
		frappe.conf["host_name"] = FUNNEL
		self.assertIn("Funnel", " ".join(site_url.status()["warnings"]))
		frappe.conf["host_name"] = "http://100.69.162.122"
		self.assertIn("Umbrel dashboard", " ".join(site_url.status()["warnings"]))
		self.assertEqual(security_status._emailed_links()["status"], "warn")

	def test_the_tailnet_desk_passes_and_pdfs_are_untouched(self):
		from erpnext_mcp import pdf_base

		frappe.conf["host_name"] = DESK
		found = site_url.status()
		self.assertTrue(found["ok"], found)
		self.assertEqual(security_status._emailed_links()["status"], "pass")
		frappe.conf["pdf_base_url"] = None
		self.assertNotIn(DESK, str(pdf_base.status().get("base_url") or ""))

	def test_server_status_carries_the_block(self):
		frappe.conf["host_name"] = DESK
		self.assertTrue(self.tool_data("get_server_status")["email_links"]["ok"])
