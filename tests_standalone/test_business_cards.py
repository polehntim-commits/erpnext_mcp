# SPDX-License-Identifier: MIT
"""A business card becomes a Contact. v0.231.0 — docs/design/business_card_contacts.md (AFB-2026-00031)."""

import frappe

from erpnext_mcp import business_cards
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError

from . import harness
from .fixtures import V12TestCase
from .harness import STORE, add_field, register_doctype, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase

CARD = {
	"first_name": "Jane",
	"last_name": "Doe",
	"title": "Sales Manager",
	"company": "Coastal Nozzle Co",
	"emails": ["Jane@CoastalNozzle.com"],
	"phones": [{"number": "(541) 555-0142", "kind": "mobile"}, {"number": "000-000-0000"}],
	"website": "www.coastalnozzle.com",
	"address": {"line1": "12 Orchard Rd", "city": "Hood River", "state": "OR", "postal_code": "97031"},
	"notes": "Met at Hort Expo, sells nozzles.",
	"met_at": "Hort Expo",
	"met_on": "2026-10-02",
}


class ContactSite:
	"""Contact's child tables and Address, as a site with ERPNext's contacts has them."""

	def setUp(self):
		super().setUp()
		self._saved = (dict(harness.CHILD_TABLES), dict(harness.CHILD_TABLE_SOURCES))
		harness.CHILD_TABLES.update({
			("Contact", "email_ids"): "Contact Email",
			("Contact", "phone_nos"): "Contact Phone",
			("Contact", "links"): "Dynamic Link",
			("Address", "links"): "Dynamic Link",
		})
		harness.CHILD_TABLE_SOURCES.update({
			"Contact Email": (("Contact", "email_ids"),),
			"Contact Phone": (("Contact", "phone_nos"),),
		})
		register_doctype("Contact Phone", [{"fieldname": n} for n in ("name", "parent", "phone", "is_primary_phone", "is_primary_mobile_no")])
		register_doctype("Address", [{"fieldname": n} for n in ("name", "address_title", "address_type", "address_line1",
			"address_line2", "city", "state", "pincode", "country")])
		for field in ("designation", "phone", "mobile_no"):
			add_field("Contact", field)
		for field in business_cards.FIELDS:
			add_field("Contact", field["fieldname"], field["fieldtype"], field.get("options"))

	def tearDown(self):
		harness.CHILD_TABLES.clear()
		harness.CHILD_TABLES.update(self._saved[0])
		harness.CHILD_TABLE_SOURCES.clear()
		harness.CHILD_TABLE_SOURCES.update(self._saved[1])
		super().tearDown()


class TheCardIsCleaned(V12TestCase):
	def test_placeholder_phones_drop_emails_lowercase(self):
		cleaned = business_cards.clean(CARD)
		self.assertEqual(cleaned["emails"], ["jane@coastalnozzle.com"])
		self.assertEqual([p["digits"] for p in cleaned["phones"]], ["5415550142"])
		self.assertEqual(cleaned["phones"][0]["kind"], "mobile")

	def test_a_card_with_no_name_and_no_company_is_refused(self):
		with self.assertRaises(ToolError):
			business_cards.clean({"emails": ["x@y.com"]})


class SavingACard(ContactSite, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_save_contact=1, allow_search_contacts=1)

	def test_a_new_card_files_contact_address_note_and_provenance(self):
		data = business_cards.save("Administrator", CARD, {}, "card-req-1")
		self.assertTrue(data["created"])
		row = STORE.get_raw("Contact", data["name"])
		self.assertEqual((row["first_name"], row["designation"], row["company_name"]), ("Jane", "Sales Manager", "Coastal Nozzle Co"))
		self.assertEqual(row["card_source"], "Business card")
		self.assertEqual(row["card_met_at"], "Hort Expo")
		self.assertEqual(data["emails"], ["jane@coastalnozzle.com"])
		self.assertEqual(len(data["phones"]), 1, "the placeholder number is never stored")
		self.assertTrue(data["address"])
		self.assertIn("Hort Expo", " ".join(r.get("content") or "" for r in STORE.rows("Comment")))
		self.assertIsNone(data["linked_to"], "nothing is linked on a guess")

	def test_the_same_request_is_the_same_contact(self):
		first = business_cards.save("Administrator", CARD, {}, "card-req-2")
		again = business_cards.save("Administrator", CARD, {}, "card-req-2")
		self.assertTrue(again["replayed"])
		self.assertEqual(again["name"], first["name"])
		self.assertEqual(len(STORE.rows("Contact")), len([r for r in STORE.rows("Contact")]))

	def test_a_duplicate_needs_a_decision_and_a_merge_fills_only_blanks(self):
		first = business_cards.save("Administrator", CARD, {})
		with self.assertRaises(ToolError) as caught:
			business_cards.save("Administrator", {**CARD, "title": "Owner"}, {})
		self.assertIn(first["name"], str(caught.exception))
		self.assertIn("merge_into", str(caught.exception))
		merged = business_cards.save(
			"Administrator", {**CARD, "title": "Owner", "emails": ["jane@coastalnozzle.com", "jd@gmail.com"]},
			{"merge_into": first["name"]},
		)
		self.assertTrue(merged["merged"])
		self.assertEqual(STORE.get_raw("Contact", first["name"])["designation"], "Sales Manager", "never overwritten")
		self.assertEqual(sorted(merged["emails"]), ["jane@coastalnozzle.com", "jd@gmail.com"])

	def test_preview_finds_the_duplicate_by_phone_in_another_spelling(self):
		business_cards.save("Administrator", CARD, {})
		seen = business_cards.preview({"first_name": "J", "phones": ["+1 541.555.0142"]})
		self.assertEqual(seen["duplicates"][0]["matched_on"], ["phone"])

	def test_a_link_is_only_what_the_caller_names(self):
		STORE.seed("Supplier", [{"name": "Coastal Nozzle Co", "supplier_name": "Coastal Nozzle Co"}])
		data = business_cards.save("Administrator", CARD, {"link_to": {"doctype": "Supplier", "name": "Coastal Nozzle Co"}})
		self.assertEqual(data["linked_to"], {"doctype": "Supplier", "name": "Coastal Nozzle Co"})
		with self.assertRaises(ToolError):
			business_cards.save("Administrator", {**CARD, "emails": [], "phones": []},
				{"save_as_new": True, "link_to": {"doctype": "Supplier", "name": "Nobody Ltd"}})

	def test_search_by_phone_and_by_place(self):
		business_cards.save("Administrator", CARD, {})
		self.assertEqual(len(business_cards.search("5415550142")), 1)
		self.assertEqual(len(business_cards.search(met_at="hort")), 1)
		self.assertEqual(business_cards.search("nobody-here"), [])

	def test_the_mcp_tools(self):
		preview = self.tool_data("save_contact", {"card": CARD, "dry_run": True})
		self.assertEqual(preview["duplicates"], [])
		saved = self.tool_data("save_contact", {"card": CARD, "client_request_id": "mcp-1"})
		self.assertTrue(saved["created"])
		self.assertEqual(self.tool_data("search_contacts", {"query": "jane"})["count"], 1)


class OnlyTheOfficeSeesContacts(ContactSite, MobileAPITestCase):
	def test_a_picker_is_refused_and_a_manager_files_from_the_phone(self):
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.preview_business_card(card=CARD)
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		saved = mobile_api.save_business_card(card=CARD, client_request_id="phone-1")
		self.assertTrue(saved["created"])
		self.assertTrue(mobile_api.save_business_card(card=CARD, client_request_id="phone-1")["replayed"])
