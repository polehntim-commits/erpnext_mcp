"""One-time upload links. v0.244.0. docs/design/upload_links.md §6.

Driven over HTTP through the real WSGI stack for the route, and as Python for the
issuing tool — the route is the threat surface, so every refusal it can give is
asserted as a status code a client would see.
"""

import hashlib
import io
import os

import frappe
from werkzeug.test import Client
from werkzeug.wrappers import Response

from erpnext_mcp import upload_links
from erpnext_mcp.api import guard
from erpnext_mcp.errors import ToolError
from erpnext_mcp.farmops_api import PREFIX
from erpnext_mcp.farmops_api import app as farmops_app
from erpnext_mcp.tools import upload_links as upload_tools

from .harness import STORE, get_site_path
from .test_api_mobile import MobileAPITestCase

PDF = b"%PDF-1.7\n" + b"x" * 2048
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 1024
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"v" * 4096


class UploadLinkCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1,
			upload_links_enabled=1,
			allow_request_upload_url=1,
			allow_revoke_upload_link=1,
			farmops_public_url="https://farm.example.ts.net",
		)
		self.client = Client(farmops_app.application, Response)
		guard._LOCAL_COUNTS.clear() if hasattr(guard, "_LOCAL_COUNTS") else None
		frappe.set_user("Administrator")
		self.target = frappe.get_doc({"doctype": "Supplier", "supplier_name": "Operating Agreement Holder"}).insert()
		STORE.commit()

	def issue(self, **kw):
		frappe.set_user("Administrator")
		args = {"doctype": "Supplier", "name": self.target.name, **kw}
		return upload_tools.request_upload_url(args).data

	def path(self, issued):
		return issued["url"].split("farm.example.ts.net", 1)[1]

	def put(self, issued, data, name="agreement.pdf", content_type="application/pdf", **headers):
		return self.client.put(
			self.path(issued), data=data, headers={"X-File-Name": name, "Content-Type": content_type, **headers}
		)


class IssuingALink(UploadLinkCase):
	def test_the_url_carries_the_token_and_only_its_hash_is_stored(self):
		issued = self.issue()
		token = issued["url"].rsplit("/", 1)[1]
		self.assertTrue(issued["url"].startswith("https://farm.example.ts.net/farmops/api/upload/"))
		self.assertGreaterEqual(len(token), 40)
		row = STORE.get_raw("Upload Link", issued["link"])
		self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
		self.assertNotIn(token, str(row))
		self.assertEqual(row["status"], "Open")
		self.assertEqual(row["max_bytes"], upload_links.DEFAULT_MAX_BYTES)

	def test_switched_off_nothing_is_issued(self):
		self.configure(enabled=1, upload_links_enabled=0, allow_request_upload_url=1)
		with self.assertRaisesRegex(ToolError, "switched off"):
			self.issue()

	def test_a_missing_target_or_an_outsize_request_is_refused(self):
		with self.assertRaisesRegex(ToolError, "no Supplier named"):
			upload_tools.request_upload_url({"doctype": "Supplier", "name": "Nobody Ltd"})
		with self.assertRaisesRegex(ToolError, "ceiling"):
			self.issue(max_bytes=upload_links.DOCUMENT_CAP_BYTES + 1)
		with self.assertRaisesRegex(ToolError, "expires_minutes"):
			self.issue(expires_minutes=60 * 25)
		with self.assertRaisesRegex(ToolError, "photo or document"):
			self.issue(kinds=["archive"])
		with self.assertRaisesRegex(ToolError, "video uploads are not offered"):
			self.issue(kinds=["video"])

	def test_a_field_worker_cannot_issue_one(self):
		from .harness import set_roles

		set_roles("worker@farm.test", ["Field Worker"])
		with self.assertRaisesRegex(ToolError, "restricted to"):
			upload_links.require_issuer("worker@farm.test")


class UsingALink(UploadLinkCase):
	def test_a_put_files_the_document_once(self):
		issued = self.issue()
		answer = self.put(issued, PDF)
		self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
		data = answer.get_json()["message"] if "message" in answer.get_json() else answer.get_json()
		self.assertEqual(data["file_size"], len(PDF))
		self.assertEqual(data["sha256"], hashlib.sha256(PDF).hexdigest())
		self.assertTrue(data["file_name"].startswith(issued["upload_id"] + "-"))
		on_disk = get_site_path("private", "files", data["file_name"])
		with open(on_disk, "rb") as handle:
			self.assertEqual(handle.read(), PDF)
		file_row = STORE.get_raw("File", data["file"])
		self.assertEqual(file_row["attached_to_doctype"], "Supplier")
		self.assertEqual(file_row["attached_to_name"], self.target.name)
		self.assertEqual(int(file_row["is_private"]), 1)
		self.assertEqual(STORE.get_raw("Upload Link", issued["link"])["status"], "Done")
		# Used once: the same link again is the same 404 as a link that never was.
		again = self.put(issued, PDF)
		self.assertEqual(again.status_code, 404)

	def test_a_multipart_post_with_one_file_works_too(self):
		issued = self.issue(kinds=["photo"])
		answer = self.client.post(
			self.path(issued),
			data={"file": (io.BytesIO(JPEG), "IMG_0042.jpg", "image/jpeg")},
			content_type="multipart/form-data",
		)
		self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))

	def test_unknown_expired_revoked_and_switched_off_all_look_the_same(self):
		unknown = self.client.put(f"{PREFIX}/upload/not-a-real-token", data=PDF)
		expired = self.issue()
		frappe.db.set_value("Upload Link", expired["link"], "expires_at", "2000-01-01 00:00:00")
		revoked = self.issue()
		upload_tools.revoke_upload_link({"upload_id": revoked["upload_id"]})
		answers = [unknown, self.put(expired, PDF), self.put(revoked, PDF)]
		self.configure(enabled=1, upload_links_enabled=0)
		answers.append(self.put(self.issue_raw(), PDF))
		self.assertEqual({a.status_code for a in answers}, {404})
		self.assertEqual(len({a.get_data() for a in answers}), 1, "one body for every dead link")
		self.assertEqual(STORE.get_raw("Upload Link", expired["link"])["status"], "Expired")

	def issue_raw(self):
		# Issued while on, used while off.
		self.configure(enabled=1, upload_links_enabled=1, allow_request_upload_url=1,
		               farmops_public_url="https://farm.example.ts.net")
		issued = self.issue()
		self.configure(enabled=1, upload_links_enabled=0, farmops_public_url="https://farm.example.ts.net")
		return issued

	def test_a_get_is_not_a_page(self):
		issued = self.issue()
		self.assertEqual(self.client.get(self.path(issued)).status_code, 404)
		self.assertEqual(STORE.get_raw("Upload Link", issued["link"])["status"], "Open", "a GET spends nothing")

	def test_too_big_by_declared_length_or_by_counting_and_the_part_is_gone(self):
		issued = self.issue(max_bytes=1000)
		answer = self.put(issued, PDF)
		self.assertEqual(answer.status_code, 413)
		self.assertEqual(STORE.get_raw("Upload Link", issued["link"])["status"], "Failed")
		# Counting, for a body that does not announce its length.
		issued = self.issue(max_bytes=1000)
		link = upload_links.find(issued["url"].rsplit("/", 1)[1])
		upload_links.claim(link["name"])
		with self.assertRaises(upload_links.UploadRefused) as caught:
			upload_links.receive(link, io.BytesIO(PDF), filename="a.pdf")
		self.assertEqual(caught.exception.status, 413)
		leftovers = [n for n in os.listdir(get_site_path("private", "files")) if n.endswith(".part")]
		self.assertEqual(leftovers, [])

	def test_the_wrong_kind_is_refused_three_ways(self):
		cases = [
			("tool.exe", b"MZ" + b"\x00" * 100, "application/octet-stream"),  # refused extension
			("evil.jpg", b"MZ" + b"\x00" * 100, "image/jpeg"),  # executable called .jpg
			("drawing.svg", b"<svg onload=alert(1)>", "image/svg+xml"),
			("bundle.zip", b"PK\x03\x04" + b"\x00" * 50, "application/zip"),
			("clip.mp4", MP4, "video/mp4"),  # no video anywhere (decision 47)
			("agreement.pdf", PDF, "image/png"),  # declared type disagrees
		]
		for name, body, kind in cases:
			with self.subTest(name=name):
				issued = self.issue()
				self.assertEqual(self.put(issued, body, name=name, content_type=kind).status_code, 415)

	def test_a_public_file_needs_the_admin_switch(self):
		"""Decision 48: private by default; public only once an admin allows it."""
		with self.assertRaisesRegex(ToolError, "Allow Public Files"):
			self.issue(is_private=False)
		self.configure(enabled=1, upload_links_enabled=1, allow_request_upload_url=1, allow_revoke_upload_link=1,
		               farmops_public_url="https://farm.example.ts.net", upload_link_allow_public=1)
		self.assertFalse(self.issue(is_private=False)["is_private"])

	def test_hostile_file_names_never_choose_the_path(self):
		for raw in ("../../etc/passwd.pdf", "/abs/path/x.pdf", "a\x00b\x07c.pdf", "." * 3 + "hidden.pdf", "n" * 400 + ".pdf"):
			with self.subTest(raw=raw[:20]):
				safe = upload_links.safe_filename(raw)
				self.assertNotIn("/", safe)
				self.assertNotIn("..", safe)
				self.assertFalse(safe.startswith("."))
				self.assertTrue(safe.endswith(".pdf"))
				self.assertLessEqual(len(safe), 120)
		issued = self.issue()
		answer = self.put(issued, PDF, name="../../etc/passwd.pdf")
		self.assertEqual(answer.status_code, 200)
		self.assertNotIn("/", (answer.get_json().get("message") or answer.get_json())["file_name"])

	def test_a_wrong_sha256_is_refused(self):
		issued = self.issue(sha256="0" * 64)
		self.assertEqual(self.put(issued, PDF).status_code, 422)
		right = self.issue(sha256=hashlib.sha256(PDF).hexdigest())
		self.assertEqual(self.put(right, PDF).status_code, 200)

	def test_a_target_deleted_after_issue_is_refused(self):
		issued = self.issue()
		STORE.delete("Supplier", self.target.name) if hasattr(STORE, "delete") else frappe.delete_doc(
			"Supplier", self.target.name, force=True
		)
		self.assertEqual(self.put(issued, PDF).status_code, 409)

	def test_a_link_in_flight_is_not_claimed_twice(self):
		issued = self.issue()
		link = upload_links.find(issued["url"].rsplit("/", 1)[1])
		upload_links.claim(link["name"])
		with self.assertRaises(upload_links.UploadRefused) as caught:
			upload_links.claim(link["name"])
		self.assertEqual(caught.exception.status, 409)
		self.assertEqual(self.put(issued, PDF).status_code, 404, "Receiving is not Open")

	def test_the_rate_limit_trips_before_the_token_is_looked_at(self):
		# The double's cache is reset when each request's session closes (a bench's
		# redis is not), so the counter is fed directly: one over the limit is 429,
		# and a good token behind it is not spent.
		from unittest import mock

		issued = self.issue()
		over = farmops_app.UPLOAD_LIMIT + 1
		with mock.patch.object(guard, "_count", side_effect=lambda key, s: over if key.startswith("upload:") else 1):
			self.assertEqual(self.put(issued, PDF).status_code, 429)
		self.assertEqual(STORE.get_raw("Upload Link", issued["link"])["status"], "Open")


class TheSweep(UploadLinkCase):
	def test_open_past_its_time_expires_and_a_dead_transfer_fails(self):
		stale = self.issue()
		frappe.db.set_value("Upload Link", stale["link"], "expires_at", "2000-01-01 00:00:00")
		stuck = self.issue()
		frappe.db.set_value(
			"Upload Link", stuck["link"], {"status": "Receiving", "last_attempt_at": "2000-01-01 00:00:00"}
		)
		fresh = self.issue()
		report = upload_links.sweep()
		self.assertEqual((report["expired"], report["failed"]), (1, 1))
		self.assertEqual(STORE.get_raw("Upload Link", stale["link"])["status"], "Expired")
		self.assertEqual(STORE.get_raw("Upload Link", stuck["link"])["status"], "Failed")
		self.assertEqual(STORE.get_raw("Upload Link", fresh["link"])["status"], "Open")

	def test_status_and_list_never_show_the_token(self):
		issued = self.issue(note="OML operating agreement")
		token = issued["url"].rsplit("/", 1)[1]
		status = upload_tools.get_upload_status({"upload_id": issued["upload_id"]}).data
		listing = upload_tools.list_upload_links({}).data
		self.assertEqual(status["status"], "Open")
		self.assertNotIn(token, str(status) + str(listing))
		self.assertNotIn("token_hash", status)
