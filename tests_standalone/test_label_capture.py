# SPDX-License-Identifier: MIT
"""v0.201.0 — a product's label, stored; EPA's label attached; a mouse bait judged as one.

Tim on PROWLER™: "when we added the label it is not storing", "the PDF is not
getting attached to the item", "Its not a Crop Protection Product". Contract:
`docs/design/product_label_capture.md`.
"""

import unittest
from typing import ClassVar
from unittest import mock

import frappe

from erpnext_mcp import document_intel, epa_ppls, url_fetch
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError

from .fixtures import MAIN, STORES, seed_masters, seed_stock
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase

#: EPA's answer for 12455-97, trimmed to what this app reads (fetched 2026-09-27).
EPA_12455_97 = {
	"items": [
		{
			"eparegno": "12455-97",
			"productname": "F-TRAC PLACE PACS",
			"product_status": "Active",
			"signal_word": "Caution",
			"rup_yn": "No",
			"companyinfo": [{"name": "BELL LABORATORIES, INC"}],
			"active_ingredients": [
				{"active_ing": "Bromethalin", "cas_number": "63333-35-7", "active_ing_percent": 0.01}
			],
			"pdffiles": [
				{"epa_reg_num": "12455-97", "pdffile": "012455-00097-20150807.pdf"},
				{"epa_reg_num": "12455-97", "pdffile": "012455-00097-20191213.pdf"},
			],
		}
	]
}

PROWLER_OCR = (
	"PROWLER\nKILLS RATS, MICE & MEADOW VOLES*\nNORWAY RATS, ROOF RATS AND HOUSE MICE\n"
	"FOR INDOOR AND OUTDOOR USE\nACTIVE INGREDIENT:\nBromethalin (CAS #63333-35-7): ..... 0.01%\n"
	"CAUTION\nwaterproof gloves\nSTORAGE AND DISPOSAL\nPesticide Storage: Store only in original "
	"container.\nEPA REG. NO. 12455-97-3240\nTamper-resistant bait stations must be used"
)
PROWLER_FIELDS = {
	"product_name": "PROWLER",
	"epa_registration_number": "12455-97-3240",
	"signal_word": "Caution",
	# The phone's own shape (fafo_ios `ProductLabelModels`): `cas` rides along.
	"active_ingredients": [{"name": "Bromethalin", "concentration": 0.01, "unit": "%", "cas": "63333-35-7"}],
	"ppe_requirements": "Waterproof gloves",
	"application_rate": "Norway rats: 1 or 2 blocks",
	"storage_disposal": "Store only in original container.",
}


def _pdf():
	return url_fetch.Fetched(
		content=b"%PDF-1.7 label",
		extension="pdf",
		mime_type="application/pdf",
		final_url="https://www3.epa.gov/x.pdf",
		content_type="application/pdf",
		disposition_name="",
	)


def epa_online():
	"""EPA answering: the registration, and a PDF."""
	return mock.patch.multiple(
		url_fetch,
		fetch_json=mock.Mock(return_value=EPA_12455_97),
		fetch=mock.Mock(return_value=_pdf()),
	)


# ── 1. EPA's registration ───────────────────────────────────────────────────
class EpaRegistration(unittest.TestCase):
	def test_a_distributor_number_is_looked_up_by_its_registration(self):
		self.assertEqual(epa_ppls.base_registration("12455-97-3240"), "12455-97")
		self.assertEqual(epa_ppls.base_registration("012455-00097"), "12455-97")
		self.assertEqual(epa_ppls.base_registration("12455 - 97"), "12455-97")
		self.assertEqual(epa_ppls.base_registration("12455-WI-1"), "")
		self.assertEqual(epa_ppls.base_registration(""), "")

	def test_the_record_is_summarised_newest_label_first(self):
		record = epa_ppls.summarise(EPA_12455_97["items"][0])
		self.assertEqual(record["product_name"], "F-TRAC PLACE PACS")
		self.assertEqual(record["registrant"], "BELL LABORATORIES, INC")
		self.assertEqual(record["signal_word"], "Caution")
		self.assertEqual(record["active_ingredients"][0]["cas"], "63333-35-7")
		self.assertEqual(record["pdf_files"][0]["pdffile"], "012455-00097-20191213.pdf")
		self.assertEqual(record["pdf_files"][0]["date"], "2019-12-13")

	def test_the_api_is_asked_for_the_registration_not_the_distributor_number(self):
		with mock.patch.object(url_fetch, "fetch_json", return_value={"items": []}) as asked:
			self.assertEqual(epa_ppls.lookup("12455-97-3240"), {})
		self.assertTrue(asked.call_args[0][0].endswith("/ppls/12455-97"))


# ── 2. a non-crop label is judged as one ────────────────────────────────────
class ANonCropLabelIsJudgedAsOne(unittest.TestCase):
	def test_prowler_reads_as_non_crop(self):
		self.assertEqual(document_intel.pesticide_scope({}, PROWLER_OCR), "Non-crop")
		self.assertEqual(
			document_intel.pesticide_scope({}, "Apples: 2.56 fl oz/acre. Pre-harvest interval 14 days"),
			"Crop",
		)
		self.assertEqual(document_intel.pesticide_scope({"pesticide_use_scope": "non crop"}, ""), "Non-crop")

	def test_a_rodenticide_is_not_flagged_for_having_no_rei_or_phi(self):
		result = document_intel.validate_extraction("Pesticide Label", PROWLER_OCR, PROWLER_FIELDS)
		codes = {entry["code"] for entry in result["issues"]}
		self.assertFalse(codes & {"rei_hours_missing", "phi_days_missing", "phi_crop_missing"}, codes)
		self.assertEqual(result["status"], "Pending")
		self.assertGreater(result["confidence"], 0.5)
		self.assertEqual(result["pesticide_use_scope"], "Non-crop")

	def test_a_crop_label_still_is(self):
		result = document_intel.validate_extraction(
			"Pesticide Label", "Apples 2 fl oz/acre", {"epa_registration_number": "100-1234"}
		)
		self.assertIn("rei_hours_missing", {entry["code"] for entry in result["issues"]})

	def test_a_reading_that_disagrees_with_epa_is_warned_about(self):
		record = epa_ppls.summarise(EPA_12455_97["items"][0])
		fields = {**PROWLER_FIELDS, "signal_word": "Warning", "active_ingredients": [{"name": "Brodifacoum"}]}
		result = document_intel.validate_extraction(
			"Pesticide Label", PROWLER_OCR, fields, {"epa_record": record}
		)
		codes = {entry["code"] for entry in result["issues"]}
		self.assertIn("signal_word_differs_from_epa", codes)
		self.assertIn("active_ingredients_differ_from_epa", codes)


# ── 3. registering a label from the phone ───────────────────────────────────
class LabelTestCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		seed_stock()
		from erpnext_mcp import compliance_fields

		compliance_fields.install_compliance_fields(respect_switch=False)
		# A migrated site has both pesticide groups (v0.202.0 seeds them).
		from erpnext_mcp import agronomy_seed

		agronomy_seed._seed_item_groups({"created": [], "skipped": [], "failed": []})
		STORE.seed(
			"File",
			[
				{
					"name": "LBL-1",
					"file_name": "label-1.jpg",
					"file_url": "/private/files/label-1.jpg",
					"is_private": 1,
					"owner": WORKER,
				},
				{
					"name": "LBL-2",
					"file_name": "label-2.jpg",
					"file_url": "/private/files/label-2.jpg",
					"is_private": 1,
					"owner": WORKER,
				},
				{
					"name": "LBL-THEIRS",
					"file_name": "x.jpg",
					"file_url": "/private/files/x.jpg",
					"is_private": 1,
					"owner": "someone@else.test",
				},
				{
					"name": "LBL-FILED",
					"file_name": "y.jpg",
					"file_url": "/private/files/y.jpg",
					"is_private": 1,
					"owner": WORKER,
					"attached_to_doctype": "Expense Receipt",
					"attached_to_name": "EXR-1",
				},
			],
		)

	def foreman(self):
		set_roles(WORKER, ["Field Worker", "Foreman"])
		return self.be()

	def prowler(self, **extra):
		self.foreman()
		name = mobile_api.create_item(item_name="PROWLER™", barcode="048745228174", **extra)["name"]
		# A refused call rolls its request back, as a site does; the product was
		# created by an earlier request and must survive it.
		STORE.commit()
		return name


class ALabelIsStored(LabelTestCase):
	def test_a_mouse_bait_goes_under_pest_control(self):
		self.foreman()
		answer = mobile_api.create_item(
			item_name="PROWLER™", epa_registration_number="12455-97-3240", pesticide_use_scope="Non-crop"
		)
		self.assertEqual(answer["item_group"], "Pest Control Products")
		self.assertFalse(answer["item_group_created"])
		self.assertEqual(STORE.get_raw("Item", answer["name"])["pesticide_use_scope"], "Non-crop")

	def test_a_crop_product_still_goes_under_crop_protection_and_a_named_group_wins(self):
		self.foreman()
		crop = mobile_api.create_item(
			item_name="Warrior II", epa_registration_number="100-1295", pesticide_use_scope="Crop"
		)
		self.assertEqual(crop["item_group"], "Crop Protection Products")
		named = mobile_api.create_item(
			item_name="Mouse Trap Bait",
			epa_registration_number="12455-89",
			item_group="Pest Control Products",
		)
		self.assertEqual(named["item_group"], "Pest Control Products")

	def test_the_group_picker_lists_leaves_and_the_two_suggestions(self):
		self.be()
		answer = mobile_api.list_item_groups()
		self.assertEqual(answer["suggested"]["Non-crop"], "Pest Control Products")
		self.assertTrue(all(not row["is_group"] for row in answer["groups"]))

	def test_the_photos_the_validation_and_the_epa_label_all_land_on_the_item(self):
		item = self.prowler()
		with epa_online():
			answer = mobile_api.register_product_label(
				item_code=item,
				file_tokens=["LBL-1", "LBL-2"],
				ocr_text=PROWLER_OCR,
				extracted_fields=PROWLER_FIELDS,
				llm_assessment={"status": "Validated", "issues": [], "confidence": 0.8, "reasoning": "ok"},
				llm_model="apple-foundation-models",
			)
		# The photographs are the Item's now.
		for token in ("LBL-1", "LBL-2"):
			row = STORE.get_raw("File", token)
			self.assertEqual((row["attached_to_doctype"], row["attached_to_name"]), ("Item", item))
		# The validation is filed against the Item, with its scan.
		validation = STORE.get_raw("Document Validation", answer["validation"]["name"])
		self.assertEqual(validation["source_doctype"], "Item")
		self.assertEqual(validation["source_name"], item)
		self.assertEqual(validation["scan_file_url"], "/private/files/label-1.jpg")
		self.assertEqual(validation["llm_model"], "apple-foundation-models")
		self.assertEqual(STORE.get_raw("Item", item)["label_scan_validation"], answer["validation"]["name"])
		self.assertEqual(answer["validation"]["pesticide_use_scope"], "Non-crop")
		# EPA's record, and its PDF on the Item.
		epa = answer["epa_label"]
		self.assertEqual(epa["status"], "attached")
		self.assertEqual(epa["registration"], "12455-97")
		self.assertEqual(epa["product_name"], "F-TRAC PLACE PACS")
		pdf = STORE.get_raw("File", epa["pdf"]["file"])
		self.assertEqual((pdf["attached_to_doctype"], pdf["attached_to_name"]), ("Item", item))
		self.assertEqual(pdf["file_name"], "EPA label 12455-97 (2019-12-13).pdf")
		# The label's facts filled the Item's blanks.
		row = STORE.get_raw("Item", item)
		self.assertEqual(row["epa_registration_number"], "12455-97-3240")
		self.assertEqual(row["pesticide_use_scope"], "Non-crop")
		self.assertIn("original container", row["storage_disposal"])
		self.assertIn("Bromethalin", row["active_ingredients"])
		self.assertNotIn("cas", row["active_ingredients"])
		self.assertEqual(answer["warnings"], [])

	def test_epa_being_down_does_not_lose_the_label(self):
		item = self.prowler()
		with mock.patch.object(
			url_fetch, "fetch_json", side_effect=ToolError("ordspub.epa.gov could not be reached")
		):
			answer = mobile_api.register_product_label(
				item_code=item, file_tokens=["LBL-1"], ocr_text=PROWLER_OCR, extracted_fields=PROWLER_FIELDS
			)
		self.assertEqual(answer["epa_label"]["status"], "failed")
		self.assertTrue(answer["validation"]["name"])
		self.assertEqual(STORE.get_raw("File", "LBL-1")["attached_to_name"], item)

	def test_registering_again_reuses_the_epa_pdf_and_the_photos(self):
		item = self.prowler()
		with epa_online():
			first = mobile_api.register_product_label(
				item_code=item, file_tokens=["LBL-1"], ocr_text=PROWLER_OCR, extracted_fields=PROWLER_FIELDS
			)
			again = mobile_api.register_product_label(
				item_code=item, file_tokens=["LBL-1"], ocr_text=PROWLER_OCR, extracted_fields=PROWLER_FIELDS
			)
		self.assertEqual(first["epa_label"]["pdf"]["file"], again["epa_label"]["pdf"]["file"])

	def test_a_photo_that_is_not_this_callers_or_is_filed_elsewhere_is_refused(self):
		item = self.prowler()
		with epa_online():
			with self.assertRaises(frappe.PermissionError):
				mobile_api.register_product_label(item_code=item, file_tokens=["LBL-THEIRS"], ocr_text="x")
			with self.assertRaises(frappe.PermissionError):
				mobile_api.register_product_label(item_code=item, file_tokens=["LBL-FILED"], ocr_text="x")
		self.assertIsNone(STORE.get_raw("File", "LBL-THEIRS").get("attached_to_doctype"))

	def test_a_picker_cannot_register_a_label(self):
		item = self.prowler()
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.register_product_label(item_code=item, ocr_text=PROWLER_OCR)


# ── 4. a receipt with no cost ───────────────────────────────────────────────
class AReceiptWithNoCost(LabelTestCase):
	def test_a_product_with_no_valuation_is_received_at_zero_and_says_so(self):
		item = self.prowler()
		from erpnext_mcp.tools import stock_inventory

		data = stock_inventory.create_stock_entry(
			{
				"entry_type": "Material Receipt",
				"company": MAIN,
				"items": [{"item_code": item, "qty": 1, "warehouse": STORES}],
			}
		).data
		self.assertEqual(data["zero_valued_items"], [item])
		self.assertIn("zero value", data["zero_valued_note"])
		priced = stock_inventory.create_stock_entry(
			{
				"entry_type": "Material Receipt",
				"company": MAIN,
				"items": [{"item_code": item, "qty": 1, "warehouse": STORES, "basic_rate": 27.99}],
			}
		).data
		self.assertEqual(priced["zero_valued_items"], [])


# ── 5. the Desk back-fill, and the patch ────────────────────────────────────
class TheDeskBackFill(LabelTestCase):
	def test_attach_epa_label_back_fills_an_existing_item(self):
		item = self.prowler()
		self.configure(enabled=1, allow_attach_epa_label=1)
		set_roles(WORKER, ["Field Worker", "Foreman", "Farm Manager"])
		with epa_online():
			data = self.tool_data(
				"attach_epa_label", {"item_code": item, "epa_registration_number": "12455-97-3240"}
			)
		self.assertEqual(data["status"], "attached")
		self.assertEqual(data["item_updates"]["signal_word"], "Caution")
		self.assertEqual(STORE.get_raw("Item", item)["epa_registration_number"], "12455-97-3240")

	def test_the_patch_moves_only_the_old_rule(self):
		from erpnext_mcp import compliance_fields
		from erpnext_mcp.patches import widen_pesticide_field_visibility as patch

		STORE.seed(
			"Custom Field",
			[
				{
					"name": "Item-old",
					"dt": "Item",
					"fieldname": "a",
					"depends_on": compliance_fields.PREVIOUS_CHEMICAL_ITEM_DEPENDS_ON,
				},
				{"name": "Item-mine", "dt": "Item", "fieldname": "b", "depends_on": "eval:doc.custom"},
			],
		)
		self.assertGreaterEqual(patch.widen_pesticide_field_visibility()["updated"], 1)
		self.assertEqual(
			STORE.get_raw("Custom Field", "Item-old")["depends_on"],
			compliance_fields.CHEMICAL_ITEM_DEPENDS_ON,
		)
		self.assertEqual(STORE.get_raw("Custom Field", "Item-mine")["depends_on"], "eval:doc.custom")
		self.assertEqual(patch.widen_pesticide_field_visibility()["updated"], 0)


# ── 6. a public host whose IPv6 does not route ─────────────────────────────
class AnUnroutedIPv6IsNotTheEnd(unittest.TestCase):
	"""EPA answers an IPv6 address first; a bench with no IPv6 route timed out on
	it where curl, which falls back, fetched the page in a second."""

	def test_ipv4_is_tried_first_and_the_next_checked_address_on_failure(self):
		infos = [
			(None, None, None, None, ("2620:117:506f:15::f020", 443, 0, 0)),
			(None, None, None, None, ("161.80.8.10", 443)),
		]
		with mock.patch("socket.getaddrinfo", return_value=infos):
			self.assertEqual(url_fetch.public_addresses("ordspub.epa.gov", 443)[0], "161.80.8.10")

		tried = []

		def opener(scheme, host, port, address, target, timeout):
			tried.append(address)
			if address == "161.80.8.10":
				raise TimeoutError("no route")
			return _JsonResponse(b'{"items": []}')

		with (
			mock.patch("socket.getaddrinfo", return_value=infos),
			mock.patch.object(url_fetch, "_open", side_effect=opener),
		):
			self.assertEqual(url_fetch.fetch_json("https://ordspub.epa.gov/ords/x"), {"items": []})
		self.assertEqual(tried, ["161.80.8.10", "2620:117:506f:15::f020"])

	def test_every_address_is_still_checked_before_any_is_tried(self):
		infos = [
			(None, None, None, None, ("161.80.8.10", 443)),
			(None, None, None, None, ("10.0.0.5", 443)),
		]
		with mock.patch("socket.getaddrinfo", return_value=infos):
			with self.assertRaises(ToolError):
				url_fetch.public_addresses("mixed.example", 443)


class _JsonResponse:
	status = 200

	def __init__(self, body):
		self._body = body

	def getheader(self, name, default=None):
		return {"content-type": "application/json"}.get(name.lower(), default)

	def read(self, amount=-1):
		body, self._body = self._body, b""
		return body

	def close(self):
		pass


# ── 7. a bait counts in blocks ──────────────────────────────────────────────
class ABaitCountsInBlocks(LabelTestCase):
	def test_the_bait_context_names_its_default(self):
		from erpnext_mcp import agronomy_seed

		STORE.seed("UOM", [{"name": "Block", "uom_name": "Block", "enabled": 1, "must_be_whole_number": 1}])
		agronomy_seed._seed_contexts({"created": [], "skipped": [], "failed": []})
		self.be()
		answer = mobile_api.list_uoms(context="Bait")
		self.assertEqual(answer["default_uom"], "Block")
		self.assertEqual([row["name"] for row in answer["uoms"]], ["Block"])
		self.assertIsNone(mobile_api.list_uoms()["default_uom"])


# ── 8. a phone's on-device model is heard, not obeyed ───────────────────────
class AnOnDeviceAssessmentIsAdvisory(unittest.TestCase):
	"""DVAL-2026-0007: the on-device model called 12455-97-3240 malformed and a
	100% ingredient total implausible, and flagged PROWLER® on its own."""

	ASSESSMENT: ClassVar[dict] = {
		"status": "Flagged",
		"confidence": 0.6,
		"reasoning": "several inconsistencies",
		"issues": [
			{
				"severity": "error",
				"field": "epa_registration_number",
				"code": "on_device_finding",
				"message": "lacks hyphens",
			},
			{
				"severity": "error",
				"field": "active_ingredients",
				"code": "on_device_finding",
				"message": "100% is implausible",
			},
			{
				"severity": "error",
				"field": "rei_hours",
				"code": "on_device_finding",
				"message": "No REI is stated",
			},
			{
				"severity": "error",
				"field": "storage_disposal",
				"code": "on_device_finding",
				"message": "The storage statement is cut off mid-sentence. " * 10,
			},
		],
	}

	def merged(self, model):
		deterministic = document_intel.validate_extraction("Pesticide Label", PROWLER_OCR, PROWLER_FIELDS)
		return deterministic, document_intel.merge_llm_assessment(deterministic, self.ASSESSMENT, model)

	def test_it_cannot_flag_a_label_the_rules_passed(self):
		deterministic, merged = self.merged("apple-foundation-models")
		self.assertEqual(merged["status"], deterministic["status"])
		self.assertEqual(merged["confidence"], deterministic["confidence"])
		kept = [entry for entry in merged["issues"] if entry.get("code") == "on_device_finding"]
		self.assertEqual({entry["severity"] for entry in kept}, {"warning"})
		self.assertEqual(merged["llm_model"], "apple-foundation-models")

	def test_findings_the_rules_contradict_are_dropped_and_the_rest_cut_short(self):
		_, merged = self.merged("apple-foundation-models")
		kept = [entry for entry in merged["issues"] if entry.get("code") == "on_device_finding"]
		# the well-formed EPA number, the 100% total and the REI on a mouse bait go
		self.assertEqual([entry["field"] for entry in kept], ["storage_disposal"])
		self.assertEqual(merged["advisory_dropped"], 3)
		self.assertLessEqual(len(kept[0]["message"]), document_intel.ADVISORY_MESSAGE_LIMIT)

	def test_an_mcp_clients_model_still_judges(self):
		_, merged = self.merged("claude-opus-5-5")
		self.assertEqual(merged["status"], "Flagged")
		self.assertEqual(
			len([entry for entry in merged["issues"] if entry.get("code") == "on_device_finding"]), 4
		)


# ── 9. two products under one brand stay two Items ──────────────────────────
class TwoProwlersAreTwoProducts(LabelTestCase):
	"""Tim: PROWLER™ (a refillable station, blocks) and PROWLER® (Place Pacs,
	22 × 3 oz, EPA 12455-97-3240) are TWO products — don't merge them."""

	def setUp(self):
		super().setUp()
		self.foreman()
		self.station = mobile_api.create_item(
			item_name="PROWLER Rat & Mouse Killer Bait Station",
			barcode="048745228174",
			product_form="Bait Station",
		)["name"]
		self.pacs = mobile_api.create_item(
			item_name="PROWLER Place Pacs (22 × 3 oz)",
			barcode="048745221441",
			epa_registration_number="12455-97-3240",
			pesticide_use_scope="Non-crop",
			product_form="Place Pacs",
			package_size="22 × 3 oz (85 g)",
		)["name"]
		STORE.commit()

	def test_the_place_pac_barcode_is_the_place_pacs_only(self):
		answer = mobile_api.match_product(barcode="048745221441")
		self.assertEqual(answer["verdict"], "existing")
		self.assertEqual([row["item_code"] for row in answer["matches"]], [self.pacs])

	def test_registration_form_and_package_together_are_the_same_product(self):
		answer = mobile_api.match_product(
			epa_registration_number="12455-97", product_form="place pac", package_size="22 x 3oz"
		)
		self.assertEqual(answer["verdict"], "existing")
		self.assertEqual(answer["matches"][0]["match"], "same")

	def test_a_new_form_under_the_registration_is_related_and_not_merged(self):
		answer = mobile_api.match_product(
			epa_registration_number="12455-97-3240", product_form="Pellets", package_size="4 lb"
		)
		self.assertEqual(answer["verdict"], "related")
		self.assertEqual(answer["matches"][0]["match"], "same_registration_other_form")

	def test_the_name_is_never_compared(self):
		answer = mobile_api.match_product(epa_registration_number="999-1", product_form="Bait Station")
		self.assertEqual(answer, {"verdict": "new", "matches": []})

	def test_nothing_to_match_on_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			mobile_api.match_product()
