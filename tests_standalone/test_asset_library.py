"""Library documents on an asset, shown from its scan. v0.255.0 (AFB-2026-00021: "show the library
information for the well, like well logs")."""

import base64
from unittest import mock

import frappe

from erpnext_mcp import reference_library as library
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, OTHER
from .test_api_mobile import ON as MOBILE_ON
from .test_api_mobile import MobileAPITestCase

PDF = b"%PDF-1.7\n% the 1998 well log\n"
LIB = {f"allow_{t}": 1 for t in ("add_reference", "cite_reference", "get_reference", "register_asset", "get_asset_detail")}


class AssetLibraryCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **MOBILE_ON, **LIB)
		patcher = mock.patch.object(library, "extract_pages", return_value=["Well log. Static water level 42 ft."])
		patcher.start()
		self.addCleanup(patcher.stop)

	def a_well(self, name="MC-Well-01", company=MAIN):
		return self.tool_data("register_asset", {"name": name, "asset_type": "Water Source", "company": company})["name"]

	def a_log(self, data=PDF, title="Well log, Well 1 (1998)"):
		return self.tool_data("add_reference", {"file_content": base64.b64encode(data).decode(), "file_name": "well1.pdf",
		                                        "title": title, "ref_type": "Record"})["name"]


class CitingOnAnAsset(AssetLibraryCase):
	def test_a_record_needs_no_source_and_is_cited_on_the_well_with_pages(self):
		well, log = self.a_well(), self.a_log()
		data = self.tool_data("cite_reference", {"doctype": "Asset Register", "name": well, "reference": log,
		                                         "pages": "1-2", "note": "Drilled 1998, 180 ft"})
		row = data["references"][0]
		self.assertEqual((row["reference"], row["title"], row["ref_type"], row["pages"], row["has_pdf"]),
		                 (log, "Well log, Well 1 (1998)", "Record", "1-2", True))
		detail = self.tool_data("get_asset_detail", {"asset_name": well})
		self.assertEqual([r["reference"] for r in detail["references"]], [log])
		self.assertIn({"doctype": "Asset Register", "name": well, "pages": "1-2"}, library.describe(log)["cited_by"])

	def test_citing_again_updates_and_remove_takes_it_off(self):
		well, log = self.a_well(), self.a_log()
		self.tool_data("cite_reference", {"doctype": "Asset Register", "name": well, "reference": log})
		again = self.tool_data("cite_reference", {"doctype": "Asset Register", "name": well, "reference": log, "pages": "3"})
		self.assertEqual([(r["reference"], r["pages"]) for r in again["references"]], [(log, "3")])
		gone = self.tool_data("cite_reference", {"doctype": "Asset Register", "name": well, "reference": log, "remove": True})
		self.assertEqual(gone["references"], [])

	def test_what_is_refused(self):
		well, log = self.a_well(), self.a_log()
		self.assertIn("doctype is one of", self.tool_error("cite_reference", {"doctype": "Item", "name": well, "reference": log}))
		self.assertIn("no Reference Document", self.tool_error("cite_reference", {"doctype": "Asset Register", "name": well,
		                                                                           "reference": "REF-NOPE"}))
		self.assertIn("pages is like", self.tool_error("cite_reference", {"doctype": "Asset Register", "name": well,
		                                                                   "reference": log, "pages": "the middle"}))
		self.assertIn("does not cite", self.tool_error("cite_reference", {"doctype": "Asset Register", "name": well,
		                                                                   "reference": log, "remove": True}))

	def test_the_write_is_off_by_default(self):
		self.configure(allow_cite_reference=0)
		self.assertIn("allow_cite_reference", self.tool_error("cite_reference", {"doctype": "Asset Register",
		                                                                          "name": "x", "reference": "y"}))


class OpenedFromTheScan(AssetLibraryCase):
	def test_the_phone_reads_the_list_and_opens_the_pdf_through_the_asset(self):
		well, log = self.a_well(), self.a_log()
		self.tool_data("cite_reference", {"doctype": "Asset Register", "name": well, "reference": log})
		self.be()
		detail = mobile_api.get_asset_detail(asset_name=well)
		self.assertEqual(detail["references"][0]["title"], "Well log, Well 1 (1998)")
		opened = mobile_api.get_asset_reference(asset_name=well, reference=log)
		self.assertEqual(base64.b64decode(opened["content_base64"]), PDF)
		self.assertEqual((opened["reference"], opened["title"]), (log, "Well log, Well 1 (1998)"))

	def test_only_what_the_asset_cites_and_only_in_the_callers_company(self):
		well, log = self.a_well(), self.a_log()
		other_log = self.a_log(data=PDF + b"other", title="Somebody else's well")
		self.tool_data("cite_reference", {"doctype": "Asset Register", "name": well, "reference": log})
		far = self.a_well("OF-Well-09", company=OTHER)
		self.tool_data("cite_reference", {"doctype": "Asset Register", "name": far, "reference": other_log})
		self.be()
		with self.assertRaisesRegex(frappe.DoesNotExistError, "does not cite"):
			mobile_api.get_asset_reference(asset_name=well, reference=other_log)
		with self.assertRaises((frappe.DoesNotExistError, frappe.PermissionError)):
			mobile_api.get_asset_reference(asset_name=far, reference=other_log)
