# SPDX-License-Identifier: MIT
"""v0.325.0 — file links both ways: a single-use DOWNLOAD link to one File, and upload links with a preset name,
an expected size and the company guard. Desk host by default; the public sidecar route only when chosen.
"""

import hashlib

import frappe

from erpnext_mcp import file_links, upload_links
from erpnext_mcp.tools import upload_links as upload_tools

from . import test_upload_links as tu
from .harness import STORE, get_site_path


class DownloadCase(tu.UploadLinkCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, upload_links_enabled=1, allow_request_upload_url=1, allow_revoke_upload_link=1,
		               allow_get_attachment_download_link=1, farmops_public_url="https://farm.example.ts.net")
		frappe.set_user("Administrator")
		issued = self.issue_public()
		answer = self.put(issued, tu.PDF)
		self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
		body = answer.get_json()
		self.file = (body.get("message") or body)["file"]
		self.configure(enabled=1, upload_links_enabled=1, allow_get_attachment_download_link=1,
		               allow_request_upload_url=1, allow_revoke_upload_link=1,
		               farmops_public_url="https://farm.example.ts.net")  # back to the Desk default

	def issue_public(self):
		self.configure(enabled=1, upload_links_enabled=1, allow_request_upload_url=1,
		               farmops_public_url="https://farm.example.ts.net", file_link_host="Public (Funnel)")
		return self.issue()

	def link(self, **kw):
		return upload_tools.get_attachment_download_link({"file": self.file, **kw}).data

	@staticmethod
	def token(issued):
		return issued["url"].split("token=", 1)[1] if "token=" in issued["url"] else issued["url"].rsplit("/", 1)[1]


class TheDownloadLink(DownloadCase):
	def test_the_public_host_always_single_use_with_the_files_name(self):
		"""v0.337.0. Download links point at the public /farmops route, never the Desk's own address (the :8443 404)."""
		issued = self.link()
		self.assertTrue(issued["url"].startswith("https://farm.example.ts.net/farmops/api/download/"), issued["url"])
		self.assertTrue(issued["link_id"].startswith("DNL-"))
		row = STORE.get_raw("Upload Link", issued["link"])
		self.assertEqual(row["direction"], "Download")
		self.assertEqual(row["token_hash"], upload_links.hash_token(self.token(issued)))
		self.assertNotIn(self.token(issued), str(row))
		response = file_links.download(self.token(issued))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(b"".join(response.response), tu.PDF)
		self.assertIn("attachment;", response.headers["Content-Disposition"])
		self.assertIn("agreement.pdf", response.headers["Content-Disposition"])
		self.assertEqual(STORE.get_raw("Upload Link", issued["link"])["status"], "Done")
		self.assertEqual(file_links.download(self.token(issued)).status_code, 404, "once")

	def test_every_bad_token_is_the_same_404(self):
		issued = self.link()
		upload_tools.revoke_upload_link({"upload_id": issued["link_id"]})
		for token in ("", "nope", self.token(issued)):
			response = file_links.download(token)
			self.assertEqual((response.status_code, response.get_data(as_text=True)), (404, "Not found."))

	def test_an_expired_link_is_404_and_logged(self):
		issued = self.link(expires_minutes=1)
		frappe.db.set_value("Upload Link", issued["link"], "expires_at", "2020-01-01 00:00:00")
		self.assertEqual(file_links.download(self.token(issued)).status_code, 404)
		self.assertEqual(STORE.get_raw("Upload Link", issued["link"])["status"], "Expired")
		tools = [r.get("tool_name") for r in STORE.rows("MCP Action Log")]
		self.assertIn("file_link:expired", tools)
		self.assertIn("file_link:issued", tools)

	def test_an_upload_token_does_not_download_and_the_other_way_round(self):
		up = self.issue()
		self.assertEqual(file_links.download(self.token(up)).status_code, 404)

	def test_read_permission_is_checked_at_issue(self):
		from unittest import mock

		with mock.patch.object(frappe, "has_permission", return_value=False):
			with self.assertRaises(Exception) as caught:
				self.link()
		self.assertIn("not permitted to read", str(caught.exception))

	def test_the_public_route_serves_downloads_whatever_file_link_host_says(self):
		"""v0.337.0. File Links: Host now decides only where UPLOAD links point; a download is always public."""
		public = self.link()  # file_link_host is the Desk default here
		answer = self.client.get(public["url"].split("farm.example.ts.net", 1)[1])
		self.assertEqual(answer.status_code, 200)
		self.assertEqual(answer.get_data(), tu.PDF)
		self.assertEqual(answer.headers["Cache-Control"], "no-store")
		self.assertEqual(answer.headers["Content-Type"], "application/pdf")


class TheUploadLinkAdditions(tu.UploadLinkCase):
	def test_a_preset_name_names_the_file_and_a_second_is_a_duplicate(self):
		issued = self.issue(file_name="Operating Agreement.pdf", expected_size=len(tu.PDF),
		                    sha256=hashlib.sha256(tu.PDF).hexdigest())
		self.assertEqual(issued["file_name"], "Operating_Agreement.pdf" if "_" in issued["file_name"] else issued["file_name"])
		answer = self.put(issued, tu.PDF, name="whatever-the-sender-says.pdf")
		self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
		body = answer.get_json()
		data = body.get("message") or body
		self.assertEqual(STORE.get_raw("File", data["file"])["file_name"], issued["file_name"])
		with self.assertRaises(Exception) as caught:
			self.issue(file_name="Operating Agreement.pdf")
		self.assertIn(issued["file_name"].split(".")[0][:9], str(caught.exception))

	def test_the_wrong_size_is_refused(self):
		issued = self.issue(expected_size=len(tu.PDF) + 1)
		self.assertEqual(self.put(issued, tu.PDF).status_code, 422)

	def test_the_desk_host_closes_the_public_upload_door(self):
		self.configure(enabled=1, upload_links_enabled=1, allow_request_upload_url=1,
		               farmops_public_url="https://farm.example.ts.net")
		issued = self.issue()
		self.assertIn("/api/method/erpnext_mcp.file_links.upload?token=", issued["url"])
		token = issued["url"].split("token=", 1)[1]
		answer = self.client.put(f"{tu.PREFIX}/upload/{token}".replace("/farmops", ""), data=tu.PDF,
		                         headers={"X-File-Name": "a.pdf", "Content-Type": "application/pdf"})
		self.assertEqual(answer.status_code, 404)

	def test_the_ten_minute_default(self):
		self.assertEqual(upload_links.DEFAULT_EXPIRY_MINUTES, 10)
