# SPDX-License-Identifier: MIT
"""v0.191.0 — an asset's stock movement and documents go through a Farm Task.

Tim's rule: every action started from an asset's screen is a Farm Task on that
asset, and the Stock Entry or the File is its outcome. These tests hold four
claims:

1. `record_asset_stock_movement` books only into or out of the asset's OWN
   warehouse, refuses any other direction, refuses an asset with no warehouse,
   and leaves a completed task whose draft entry names it as the source.
2. `attach_asset_document` files an upload or a downloaded link against the
   asset, through a completed task, and a phone can list and open it.
3. A failure after the task is raised leaves nothing behind.
4. `url_fetch` will not fetch anything off the public internet's edge — no
   loopback, private, link-local or tailnet address, no other scheme, no body
   over the cap, nothing that is not a document.
"""

from __future__ import annotations

import base64
import ipaddress
from unittest import mock

import frappe

from erpnext_mcp import url_fetch
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.farmops_api import routes as farmops_routes
from erpnext_mcp.tools import files as file_tools
from erpnext_mcp.tools import stock_inventory as stock_tools

from .fixtures import MAIN, OTHER_STORES, SPRAY, STORES, seed_masters, seed_stock
from .harness import STORE, set_roles
from .test_api_mobile import ON, WORKER, WORKER_EMPLOYEE, MobileAPITestCase

SHED = "40-5-MPH"
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PUBLIC = "93.184.216.34"


class AssetActionsTestCase(MobileAPITestCase):
	"""A storage asset in the worker's own entity, linked to Stores."""

	def setUp(self):
		super().setUp()
		seed_masters()
		seed_stock()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **ON, allow_register_asset=1)
		self.tool_data("register_asset", {"name": SHED, "asset_type": "Storage", "company": MAIN})

	def link_the_shed(self, warehouse=STORES):
		STORE.tables["Asset Register"][SHED]["warehouse"] = warehouse

	def staged(self, file_name="manual.pdf", content=PDF):
		"""What `finalize_staged_file` leaves: a private File attached to nothing."""
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": file_name, "is_private": 1, "content": content}
		).insert()
		return doc.name

	def tasks_on_the_shed(self):
		return [row for row in STORE.rows("Farm Task") if row.get("asset") == SHED]


# ── 1. stock ────────────────────────────────────────────────────────────────
class StockMovesThroughATask(AssetActionsTestCase):
	def test_stock_in_is_a_completed_task_and_a_draft_receipt_naming_it(self):
		self.link_the_shed()
		self.be()
		answer = mobile_api.record_asset_stock_movement(
			asset=SHED, direction="in", item_code=SPRAY, qty="10", notes="Delivered by the co-op"
		)
		self.assertEqual(answer["asset"], SHED)
		self.assertEqual(answer["warehouse"], STORES)
		self.assertEqual(answer["direction"], "in")
		self.assertEqual(answer["stock_entry"]["entry_type"], "Material Receipt")
		self.assertEqual(answer["stock_entry"]["status"], "Draft")
		self.assertEqual(answer["stock_entry"]["item_code"], SPRAY)
		self.assertEqual(answer["stock_entry"]["qty"], 10.0)

		task = STORE.tables["Farm Task"][answer["task"]["name"]]
		self.assertEqual(task["task_type"], "Other")
		self.assertEqual(task["asset"], SHED)
		self.assertEqual(task["company"], MAIN)
		self.assertEqual(task["state"], "Completed")
		self.assertTrue(task["task_name"].startswith("Stock in: 10 × Surround WP into "))
		self.assertIn("Delivered by the co-op", task["notes"])
		assignment = next(
			row for row in STORE.rows("Farm Task Assignment") if row.get("task") == task["name"]
		)
		self.assertEqual(assignment["assigned_to"], WORKER_EMPLOYEE)
		self.assertIn(answer["stock_entry"]["name"], assignment["findings_text"])

	def test_the_entry_carries_the_farm_task_as_its_source(self):
		self.link_the_shed()
		self.be()
		answer = mobile_api.record_asset_stock_movement(asset=SHED, direction="out", item_code=SPRAY, qty=2)
		entry = stock_tools.get_stock_entry({"name": answer["stock_entry"]["name"]}).data
		self.assertEqual(entry["docstatus"], 0)
		self.assertEqual(entry["entry_type"], "Material Issue")
		self.assertEqual(entry["source"]["doctype"], "Farm Task")
		self.assertEqual(entry["source"]["name"], answer["task"]["name"])
		self.assertEqual(entry["items"][0]["source_warehouse"], STORES)

	def test_any_other_direction_is_refused_and_nothing_is_written(self):
		self.link_the_shed()
		self.be()
		for bad in ("transfer", "", None, "sideways", "Material Receipt"):
			with self.subTest(direction=bad), self.assertRaises(frappe.ValidationError) as caught:
				mobile_api.record_asset_stock_movement(asset=SHED, direction=bad, item_code=SPRAY, qty=1)
			self.assertIn("Nothing was recorded", str(caught.exception))
		self.assertEqual(self.tasks_on_the_shed(), [])
		self.assertEqual(STORE.rows("Stock Entry"), [])

	def test_direction_is_read_without_case_or_padding(self):
		self.link_the_shed()
		self.be()
		answer = mobile_api.record_asset_stock_movement(asset=SHED, direction=" IN ", item_code=SPRAY, qty=1)
		self.assertEqual(answer["direction"], "in")

	def test_an_asset_with_no_warehouse_is_refused(self):
		self.be()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.record_asset_stock_movement(asset=SHED, direction="in", item_code=SPRAY, qty=1)
		self.assertIn("not linked to a warehouse", str(caught.exception))
		self.assertTrue(str(caught.exception).endswith("Nothing was recorded."))
		self.assertEqual(self.tasks_on_the_shed(), [])

	def test_zero_and_nonsense_quantities_are_refused(self):
		self.link_the_shed()
		self.be()
		for bad in (0, -3, "ten", None):
			with self.subTest(qty=bad), self.assertRaises(frappe.ValidationError):
				mobile_api.record_asset_stock_movement(asset=SHED, direction="in", item_code=SPRAY, qty=bad)

	def test_an_unknown_item_is_refused(self):
		self.link_the_shed()
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.record_asset_stock_movement(asset=SHED, direction="in", item_code="NOPE-404", qty=1)
		self.assertIn("NOPE-404", str(caught.exception))
		self.assertEqual(self.tasks_on_the_shed(), [])

	def test_another_entitys_company_is_refused(self):
		self.link_the_shed()
		self.be()
		with self.assertRaises(Exception):
			mobile_api.record_asset_stock_movement(
				asset=SHED, direction="in", item_code=SPRAY, qty=1, company="Second Example Ltd"
			)

	def test_a_failed_entry_rolls_the_task_back(self):
		"""The asset's warehouse belongs to another company, so the tool refuses the
		line AFTER the task has been raised. The request rolls back whole."""
		self.link_the_shed(OTHER_STORES)
		self.be()
		with self.assertRaises(Exception):
			mobile_api.record_asset_stock_movement(asset=SHED, direction="in", item_code=SPRAY, qty=1)
		self.assertEqual(self.tasks_on_the_shed(), [])
		self.assertEqual(STORE.rows("Farm Task Assignment"), [])

	def test_a_picker_may_do_it(self):
		"""Scope only — the same gate as `create_stock_entry`."""
		self.link_the_shed()
		set_roles(WORKER, ["Field Worker"])
		self.be()
		self.assertTrue(
			mobile_api.record_asset_stock_movement(asset=SHED, direction="in", item_code=SPRAY, qty=1)["task"]["name"]
		)


# ── 2. documents ────────────────────────────────────────────────────────────
class _Response:
	"""Enough of `http.client.HTTPResponse` for `url_fetch.fetch`."""

	def __init__(self, status=200, body=b"", headers=None, chunk_size=None):
		self.status = status
		self._body = body
		self._headers = {k.lower(): v for k, v in (headers or {}).items()}
		self._at = 0
		self._chunk = chunk_size

	def getheader(self, name, default=None):
		return self._headers.get(name.lower(), default)

	def read(self, amount=-1):
		size = self._chunk or amount
		piece = self._body[self._at : self._at + size]
		self._at += len(piece)
		return piece

	def close(self):
		pass


def _public_dns(host, port, *args, **kwargs):
	"""Every NAME resolves to one public address; a literal address is itself."""
	try:
		ipaddress.ip_address(host)
	except ValueError:
		return [(2, 1, 6, "", (PUBLIC, port))]
	return [(2, 1, 6, "", (host, port))]


def _serving(*responses):
	queue = list(responses)
	return mock.patch.object(url_fetch, "_open", side_effect=lambda *a, **k: queue.pop(0))


class DocumentsGoThroughATask(AssetActionsTestCase):

	def test_an_uploaded_file_lands_on_the_asset_through_a_completed_task(self):
		self.be()
		token = self.staged()
		answer = mobile_api.attach_asset_document(asset=SHED, file_token=token, title="Wind machine manual")
		self.assertEqual(answer["asset"], SHED)
		self.assertEqual(answer["attachment"]["name"], token)
		self.assertEqual(answer["attachment"]["file_name"], "manual.pdf")
		self.assertEqual(answer["attachment"]["mime_type"], "application/pdf")
		row = STORE.tables["File"][token]
		self.assertEqual((row["attached_to_doctype"], row["attached_to_name"]), ("Asset Register", SHED))
		task = STORE.tables["Farm Task"][answer["task"]["name"]]
		self.assertEqual(task["task_type"], "Other")
		self.assertEqual(task["asset"], SHED)
		self.assertEqual(task["state"], "Completed")
		self.assertEqual(task["task_name"], "Document: Wind machine manual")
		self.assertIn("Document added: Wind machine manual (uploaded from phone)", task["notes"])

	def test_the_same_token_twice_is_one_document(self):
		self.be()
		token = self.staged()
		first = mobile_api.attach_asset_document(asset=SHED, file_token=token)
		again = mobile_api.attach_asset_document(asset=SHED, file_token=token)
		self.assertTrue(again["already_attached"])
		self.assertEqual(again["task"]["name"], first["task"]["name"])
		self.assertEqual(len(self.tasks_on_the_shed()), 1)

	def test_a_file_on_another_record_is_not_moved(self):
		self.be()
		token = self.staged()
		STORE.tables["File"][token].update(attached_to_doctype="Farm Task", attached_to_name="FT-X")
		with self.assertRaises(frappe.ValidationError):
			mobile_api.attach_asset_document(asset=SHED, file_token=token)

	def test_exactly_one_source_is_required(self):
		self.be()
		for kwargs in ({}, {"url": "https://example.com/a.pdf", "file_token": "x"}):
			with self.subTest(kwargs=kwargs), self.assertRaises(frappe.ValidationError):
				mobile_api.attach_asset_document(asset=SHED, **kwargs)

	def test_a_link_is_downloaded_and_stored_as_bytes(self):
		self.be()
		with mock.patch("socket.getaddrinfo", _public_dns), _serving(
			_Response(200, PDF, {"Content-Type": "application/octet-stream"})
		):
			answer = mobile_api.attach_asset_document(
				asset=SHED, url="https://example.com/docs/op-manual.pdf?v=2", title="Operator's manual"
			)
		self.assertEqual(answer["attachment"]["file_name"], "Operator s manual.pdf")
		self.assertEqual(answer["attachment"]["mime_type"], "application/pdf")
		self.assertEqual(answer["attachment"]["file_size"], len(PDF))
		file_row = STORE.tables["File"][answer["attachment"]["name"]]
		self.assertEqual(file_row["attached_to_name"], SHED)
		self.assertTrue(file_row["is_private"])
		task = STORE.tables["Farm Task"][answer["task"]["name"]]
		self.assertIn("(from example.com)", task["notes"])

	def test_a_refused_link_writes_nothing(self):
		self.be()
		# `guard.endpoint` turns the fetch's ToolError into the surface's 400.
		with self.assertRaises(frappe.ValidationError):
			mobile_api.attach_asset_document(asset=SHED, url="http://127.0.0.1/manual.pdf")
		self.assertEqual(self.tasks_on_the_shed(), [])


class TheAssetFolderOpens(AssetActionsTestCase):
	"""`Asset Register` is on `ATTACHMENT_PARENTS`, with no HR gate, and brokered."""

	def test_the_entry_is_there_without_an_hr_gate(self):
		self.assertIs(mobile_api.ATTACHMENT_PARENTS["Asset Register"], False)
		self.assertIn("Asset Register", mobile_api.BROKERED_PARENTS)

	def no_asset_read(self):
		"""The bench condition for a picker without Frappe's `Employee` role.

		`STORE.has_permission` is default-allow, so the denial is modelled — the
		same lever `test_employee_documents.no_employee_read` pulls."""
		STORE.denied_permissions.add(("Asset Register", "read"))
		self.addCleanup(STORE.denied_permissions.discard, ("Asset Register", "read"))

	def test_the_tool_refuses_without_the_brokering(self):
		"""The negative control: with the read denied, the TOOL still refuses."""
		self.be()
		token = self.staged()
		mobile_api.attach_asset_document(asset=SHED, file_token=token)
		self.no_asset_read()
		with self.assertRaises(ToolError) as caught:
			file_tools.list_attachments({"doctype": "Asset Register", "name": SHED})
		self.assertIn("not permitted to read Asset Register", str(caught.exception))

	def test_a_picker_lists_and_opens_what_was_filed(self):
		set_roles(WORKER, ["Field Worker"])
		self.be()
		token = self.staged()
		mobile_api.attach_asset_document(asset=SHED, file_token=token)
		self.no_asset_read()
		listed = mobile_api.list_attachments(doctype="Asset Register", docname=SHED)
		self.assertEqual([row["name"] for row in listed["attachments"]], [token])
		opened = mobile_api.get_attachment_content(file=token)
		self.assertEqual(base64.b64decode(opened["content"]), PDF)

	def test_both_routes_are_mounted_and_mutating(self):
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		for method in ("record_asset_stock_movement", "attach_asset_document"):
			self.assertTrue(by_path[f"/mobile/{method}"].mutating, method)

	def test_the_signatures_are_the_ones_the_phone_was_written_against(self):
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		self.assertEqual(
			farmops_routes.accepted_arguments(by_path["/mobile/record_asset_stock_movement"].handler),
			{"asset", "direction", "item_code", "qty", "uom", "notes", "company"},
		)
		self.assertEqual(
			farmops_routes.accepted_arguments(by_path["/mobile/attach_asset_document"].handler),
			{"asset", "url", "file_token", "title", "notes", "company"},
		)


# ── 4. the fetch guard ──────────────────────────────────────────────────────
class TheFetchStaysOnThePublicInternet(AssetActionsTestCase):
	def refused(self, url, **kwargs):
		with self.assertRaises(ToolError) as caught:
			url_fetch.fetch(url, **kwargs)
		self.assertIn("Nothing was attached", str(caught.exception))
		return str(caught.exception)

	def test_private_and_local_addresses_are_refused(self):
		with mock.patch.object(url_fetch, "_open") as opened:
			for url in (
				"http://127.0.0.1/",
				"http://10.0.0.5/",
				"http://[::1]/",
				"http://192.168.1.1/manual.pdf",
				"http://169.254.169.254/latest/meta-data/",
				"http://100.101.102.103/",  # the tailnet
				"http://0.0.0.0/",
				"http://[::ffff:10.0.0.5]/",
				"http://224.0.0.1/",
			):
				with self.subTest(url=url):
					self.assertIn("not on the public internet", self.refused(url))
			opened.assert_not_called()

	def test_other_schemes_are_refused_before_any_lookup(self):
		with mock.patch("socket.getaddrinfo") as looked_up:
			for url in ("file:///etc/passwd", "ftp://example.com/manual.pdf", "gopher://x/", "javascript:alert(1)"):
				with self.subTest(url=url):
					self.assertIn("not an http or https link", self.refused(url))
			looked_up.assert_not_called()

	def test_every_resolved_address_is_checked(self):
		def mixed(host, port, *args, **kwargs):
			return [(2, 1, 6, "", (PUBLIC, port)), (2, 1, 6, "", ("10.1.2.3", port))]

		with mock.patch("socket.getaddrinfo", mixed), mock.patch.object(url_fetch, "_open") as opened:
			self.assertIn("10.1.2.3", self.refused("https://sneaky.example.com/a.pdf"))
			opened.assert_not_called()

	def test_a_redirect_is_checked_again(self):
		with mock.patch("socket.getaddrinfo", _public_dns), _serving(
			_Response(302, b"", {"Location": "http://127.0.0.1/admin"})
		):
			self.assertIn("not on the public internet", self.refused("https://example.com/a.pdf"))

	def test_redirects_are_followed_a_few_times_and_no_more(self):
		hop = _Response(301, b"", {"Location": "/next"})
		with mock.patch("socket.getaddrinfo", _public_dns), _serving(*[hop] * 5):
			self.assertIn("redirected more than", self.refused("https://example.com/a.pdf", max_redirects=3))
		with mock.patch("socket.getaddrinfo", _public_dns), _serving(
			_Response(301, b"", {"Location": "https://cdn.example.com/b.pdf"}), _Response(200, PDF)
		):
			self.assertEqual(url_fetch.fetch("http://example.com/a.pdf").final_url, "https://cdn.example.com/b.pdf")

	def test_a_body_over_the_cap_is_refused_while_it_streams(self):
		body = b"%PDF-" + b"x" * 5000
		response = _Response(200, body, {}, chunk_size=1000)
		with mock.patch("socket.getaddrinfo", _public_dns), _serving(response):
			self.assertIn("larger than", self.refused("https://example.com/a.pdf", max_bytes=2048))
		self.assertLess(response._at, len(body), "the whole body was read before the refusal")

	def test_a_declared_length_over_the_cap_is_refused_before_reading(self):
		response = _Response(200, PDF, {"Content-Length": str(10**9)})
		with mock.patch("socket.getaddrinfo", _public_dns), _serving(response):
			self.assertIn("over the", self.refused("https://example.com/a.pdf"))
		self.assertEqual(response._at, 0)

	def test_a_page_that_is_not_a_document_is_refused_whatever_it_claims(self):
		for body, content_type in (
			(b"<!doctype html><html><body>Sign in</body></html>", "application/pdf"),
			(b"<html>hi</html>", "text/plain"),
			(b"PK\x03\x04zipfile", "application/zip"),
			(b"MZ\x90\x00exe", "application/octet-stream"),
		):
			with self.subTest(content_type=content_type), mock.patch(
				"socket.getaddrinfo", _public_dns
			), _serving(_Response(200, body, {"Content-Type": content_type})):
				self.assertIn("not a PDF", self.refused("https://example.com/a.pdf"))

	def test_the_documents_it_does_take_are_sniffed(self):
		cases = {
			PDF: "pdf",
			b"\x89PNG\r\n\x1a\n" + b"\x00" * 8: "png",
			b"\xff\xd8\xff\xe0" + b"\x00" * 8: "jpg",
			b"RIFF\x00\x00\x00\x00WEBPVP8 ": "webp",
			b"\x00\x00\x00\x18ftypheic\x00\x00": "heic",
		}
		for body, extension in cases.items():
			with self.subTest(extension=extension):
				self.assertEqual(url_fetch.sniff(body)[0], extension)
		self.assertEqual(url_fetch.sniff(b"Torque: 40 ft-lb\n", "text/plain; charset=utf-8")[0], "txt")

	def test_an_error_status_is_refused(self):
		with mock.patch("socket.getaddrinfo", _public_dns), _serving(_Response(404, b"")):
			self.assertIn("HTTP 404", self.refused("https://example.com/a.pdf"))

	def test_file_names_are_sanitized_and_take_the_sniffed_extension(self):
		self.assertEqual(url_fetch.safe_file_name("../../etc/passwd", "", "", "pdf"), "passwd.pdf")
		self.assertEqual(url_fetch.safe_file_name("", "https://x.com/a/Op%20Manual.html", "", "pdf"), "Op Manual.pdf")
		self.assertEqual(
			url_fetch.safe_file_name("", "https://x.com/dl", url_fetch.disposition_file_name(
				'attachment; filename="WM-200 spec.pdf"'), "pdf"),
			"WM-200 spec.pdf",
		)
		self.assertEqual(url_fetch.safe_file_name("", "https://x.com/", "", "png"), "document.png")
