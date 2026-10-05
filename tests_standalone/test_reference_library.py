"""The Reference Library. v0.238.0 (approved queue item 4)."""

import base64
from unittest import mock

from erpnext_mcp import reference_library as library

from .fixtures import V12TestCase
from .harness import STORE

PDF = b"%PDF-1.7\n% a stand-in; text comes from the stubbed extractor\n"
ON = {f"allow_{t}": 1 for t in ("search_references", "get_reference", "list_references", "add_reference",
                                  "update_reference", "supersede_reference")}

PAGES = [
	"Bacterial canker of sweet cherry. Pruning wounds are infection courts.",
	"Prune in dry weather: no rain forecast for at least a week after pruning cuts are made.",
	"",  # a scanned page: nothing in the text layer
]


class TheLibrary(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		patcher = mock.patch.object(library, "extract_pages", return_value=list(PAGES))
		patcher.start()
		self.addCleanup(patcher.stop)

	def add(self, **kw):
		args = {"file_content": base64.b64encode(kw.pop("data", PDF)).decode(), "file_name": "canker.pdf",
		        "title": "Bacterial canker of cherry", "ref_type": "Extension Guide", "publisher": "OSU",
		        "source_url": "https://extension.oregonstate.edu/canker", "crops": ["Sweet Cherry"],
		        "topics": ["Pruning", "Bacterial canker"], **kw}
		return self.tool_data("add_reference", args)

	def test_a_pdf_is_filed_privately_with_its_pages_and_a_scan_waits_for_ocr(self):
		ref = self.add()
		self.assertEqual(ref["page_count"], 3)
		self.assertEqual(ref["text_status"], "OCR pending")
		self.assertEqual(ref["pages_pending_ocr"], [3])
		files = [r for r in STORE.rows("File") if r.get("attached_to_name") == ref["name"]]
		self.assertEqual(int(files[0]["is_private"]), 1)

	def test_search_finds_the_page_with_a_short_snippet(self):
		ref = self.add()
		hits = self.tool_data("search_references", {"query": "rain week pruning"})["hits"]
		self.assertEqual((hits[0]["reference"], hits[0]["page"]), (ref["name"], 2))
		self.assertLessEqual(len(hits[0]["snippet"]), library.SNIPPET + 2)
		self.assertEqual(self.tool_data("search_references", {"query": "rain", "crop": "Apple"})["hits"], [])

	def test_the_mac_posts_ocr_text_and_the_status_follows(self):
		ref = self.add()
		done = self.tool_data("update_reference", {"reference": ref["name"],
		                                           "page_texts": {"3": "Copper sprays before fall rains reduce canker."}})
		self.assertEqual(done["text_status"], "OCR'd")
		hit = self.tool_data("search_references", {"query": "copper fall rains"})["hits"][0]
		self.assertEqual(hit["page"], 3)

	def test_duplicates_unsourced_and_off_vocabulary_are_refused(self):
		self.add()
		self.assertIn("already in the library", self.tool_error("add_reference", {
			"file_content": base64.b64encode(PDF).decode(), "title": "Again", "ref_type": "Extension Guide",
			"source_url": "https://x"}))
		other = PDF + b"other"
		self.assertIn("source_url is required", self.tool_error("add_reference", {
			"file_content": base64.b64encode(other).decode(), "title": "No source", "ref_type": "Research Paper"}))
		self.assertIn("not in the vocabulary", self.tool_error("add_reference", {
			"file_content": base64.b64encode(other).decode(), "title": "Odd", "ref_type": "Internal",
			"crops": ["Durian"]}))
		self.assertIn("not one", self.tool_error("add_reference", {
			"file_content": base64.b64encode(b"GIF89a").decode(), "title": "Gif", "ref_type": "Internal"}))

	def test_cited_by_and_superseded(self):
		old = self.add()["name"]
		STORE.seed("Compliance Policy", [{"name": "SOP-PRUNE", "policy_name": "Pruning SOP", "references": [
			{"doctype": "Reference Citation", "reference": old, "pages": "2", "parenttype": "Compliance Policy",
			 "parentfield": "references", "parent": "SOP-PRUNE", "idx": 1}]}])
		self.assertEqual(self.tool_data("get_reference", {"reference": old})["cited_by"],
		                 [{"doctype": "Compliance Policy", "name": "SOP-PRUNE", "pages": "2"}])
		new = self.add(data=PDF + b"2027 edition", title="Bacterial canker of cherry (2027)")["name"]
		self.tool_data("supersede_reference", {"reference": old, "superseded_by": new})
		refs = {h["reference"] for h in self.tool_data("search_references", {"query": "pruning"})["hits"]}
		self.assertEqual(refs, {new})
