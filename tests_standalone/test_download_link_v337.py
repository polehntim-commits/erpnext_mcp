# SPDX-License-Identifier: MIT
"""The download link to Tim's spec (make_download_link): public host, seconds, single-use or not, sha256. v0.337.0.

Redeemed through the sidecar's own test client — the route the Funnel carries — with no cookies and no headers.
"""

import hashlib

import frappe

from erpnext_mcp import upload_links
from erpnext_mcp.tools import upload_links as upload_tools

from . import test_upload_links as tu
from .harness import STORE
from .test_file_links_v325 import DownloadCase

HOST = "https://farm.example.ts.net"


class Case(DownloadCase):
	def get(self, issued):
		return self.client.get(issued["url"].split(HOST, 1)[1])


class Redeeming(Case):
	def test_the_bytes_match_the_sha256_it_promised(self):
		issued = self.link()
		answer = self.get(issued)
		self.assertEqual(answer.status_code, 200)
		self.assertEqual(hashlib.sha256(answer.get_data()).hexdigest(), issued["sha256"])
		self.assertEqual((issued["size_bytes"], issued["mime"]), (len(tu.PDF), "application/pdf"))
		self.assertIn("attachment;", answer.headers["Content-Disposition"])
		row = STORE.get_raw("Upload Link", issued["link"])
		self.assertEqual(row["file_sha256"], issued["sha256"])
		self.assertTrue(row["received_at"], "used_at is recorded")
		self.assertTrue(row["last_ip"] is not None)

	def test_a_second_use_is_404(self):
		issued = self.link()
		self.assertEqual(self.get(issued).status_code, 200)
		self.assertEqual(self.get(issued).status_code, 404)

	def test_a_reusable_link_works_until_it_expires(self):
		issued = self.link(single_use=False)
		self.assertEqual([self.get(issued).status_code for _ in range(3)], [200, 200, 200])
		frappe.db.set_value("Upload Link", issued["link"], "expires_at", "2020-01-01 00:00:00")
		STORE.commit()
		self.assertEqual(self.get(issued).status_code, 404)

	def test_tampered_expired_and_revoked_are_the_same_404(self):
		good = self.link()
		tampered = {**good, "url": good["url"][:-2] + ("AA" if not good["url"].endswith("AA") else "BB")}
		expired = self.link()
		frappe.db.set_value("Upload Link", expired["link"], "expires_at", "2020-01-01 00:00:00")
		revoked = self.link()
		upload_tools.revoke_upload_link({"upload_id": revoked["link_id"]})
		STORE.commit()
		for label, issued in (("tampered", tampered), ("expired", expired), ("revoked", revoked)):
			with self.subTest(label):
				answer = self.get(issued)
				self.assertEqual((answer.status_code, answer.get_data(as_text=True)), (404, "Not found."))


class Minting(Case):
	def test_no_public_url_is_refused(self):
		self.configure(
			enabled=1,
			upload_links_enabled=1,
			allow_get_attachment_download_link=1,
			farmops_public_url="",
			public_url="",
		)
		with self.assertRaises(Exception) as caught:
			self.link()
		self.assertIn("no public URL configured — the link would not be reachable", str(caught.exception))

	def test_a_user_without_read_cannot_mint(self):
		parent = STORE.get_raw("File", self.file)
		STORE.denied_permissions.add((parent["attached_to_doctype"], "read"))
		self.addCleanup(STORE.denied_permissions.discard, (parent["attached_to_doctype"], "read"))
		with self.assertRaises(Exception) as caught:
			self.link()
		self.assertIn("not permitted to read", str(caught.exception))
		self.assertEqual([r for r in STORE.rows("Upload Link") if r.get("direction") == "Download"], [])

	def test_ttl_in_seconds_default_600_at_most_3600(self):
		self.assertEqual(self.link()["ttl_seconds"], 600)
		self.assertEqual(self.link(ttl_seconds=3600)["ttl_seconds"], 3600)
		for bad in (0, 3601):
			with self.subTest(ttl=bad), self.assertRaises(Exception):
				self.link(ttl_seconds=bad)

	def test_by_document_and_file_name(self):
		parent = STORE.get_raw("File", self.file)
		issued = upload_tools.get_attachment_download_link(
			{
				"doctype": parent["attached_to_doctype"],
				"name": parent["attached_to_name"],
				"file_name": parent["file_name"],
			}
		).data
		self.assertEqual(issued["file"], self.file)

	def test_by_governance_document(self):
		STORE.seed(
			"Governance Document",
			[
				{
					"name": "GD-1",
					"title": "Operating agreement",
					"attached_file": STORE.get_raw("File", self.file)["file_url"],
				}
			],
		)
		STORE.commit()
		self.assertEqual(
			upload_tools.get_attachment_download_link({"governance_document": "GD-1"}).data["file"], self.file
		)


class Housekeeping(Case):
	def test_rows_expired_over_a_week_are_purged(self):
		old, fresh = self.link(), self.link()
		frappe.db.set_value("Upload Link", old["link"], "expires_at", "2020-01-01 00:00:00")
		STORE.commit()
		result = upload_links.sweep()
		self.assertEqual(result["downloads_purged"], 1)
		self.assertIsNone(STORE.get_raw("Upload Link", old["link"]))
		self.assertIsNotNone(STORE.get_raw("Upload Link", fresh["link"]))

	def test_active_only(self):
		live, spent = self.link(), self.link()
		self.get(spent)
		rows = upload_tools.list_upload_links({"direction": "Download", "active_only": True}).data["links"]
		self.assertEqual([r["upload_id"] for r in rows], [live["link_id"]])
