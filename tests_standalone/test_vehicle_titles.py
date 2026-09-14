# SPDX-License-Identifier: MIT
"""Vehicle titles, MCOs and bills of sale through the receipt capture flow — v0.165.0.

SEVEN CLAIMS.

1. `TheDoctypesCarryTheColumns` — both JSON files have the new columns, and their
   `depends_on` and Select options are derived from the tuples the code checks, so
   the form and the tool cannot disagree about which assets have a title.
2. `CapturingATitle` — the amount is optional, the VIN is normalised, and the three
   title arguments are refused on anything that is not a document.
3. `TheVinFindsTheTruck` — one exact match links both ways, by `vin` or by
   `serial_number`; an ambiguous VIN, another company's truck and a sprayer with
   the same serial link nothing; an automatic match never replaces a title a person
   filed, and a person naming the asset does.
4. `NothingReachesTheLedger` — a Purchase Invoice is refused by name, and the
   expense summary leaves documents out of its totals.
5. `LinkingAfterwards` — `link_title_to_asset` sets both sides, copies a missing
   VIN, reports a disagreeing one, and moves a document cleanly between trucks.
6. `RegisteringAVehicle` — the four title arguments are stored on a Vehicle or a
   Tractor, refused on anything else, and a duplicate VIN is refused;
   `get_asset_detail` answers them with the linked documents.
7. `FromAPhone` — every docname a body names is scoped, and `get_asset_detail`,
   which now carries a lien holder, reads another entity's asset as not found.
"""

import json
from pathlib import Path

import frappe

from erpnext_mcp import asset_types
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.tools import asset_tags, expenses, vehicle_titles

from .fixtures import MAIN, OTHER, PurchasingTestCase, V12TestCase, install_hrms
from .harness import STORE
from .test_api_mobile import MobileAPITestCase

DOCTYPES = Path(__file__).resolve().parents[1] / "erpnext_mcp" / "erpnext_mcp" / "doctype"

VIN = "1FTFW1E50KFA12345"

ON = {
	f"allow_{name}": 1
	for name in (
		"submit_expense_receipt",
		"get_expense_receipt",
		"approve_expense_receipt",
		"create_purchase_invoice_from_receipt",
		"get_expense_summary",
		"register_asset",
		"get_asset_detail",
	)
}


class TitleTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()

	def a_vehicle(self, name="MC-Truck-01", asset_type="Vehicle", company=MAIN, **kw):
		return self.tool_data(
			"register_asset", {"name": name, "asset_type": asset_type, "company": company, **kw}
		)

	def a_title(self, **overrides):
		payload = {
			"merchant": "Oregon DMV",
			"receipt_date": "2026-03-02",
			"category": "Title/MCO",
			"document_subtype": "Vehicle Title",
			"company": MAIN,
			"submitted_by": "HR-EMP-00001",
		}
		payload.update(overrides)
		return self.tool_data("submit_expense_receipt", payload)

	def asset(self, name):
		return frappe.db.get_value(
			"Asset Register", name, ["vin", "title_receipt", "serial_number"], as_dict=True
		)

	def receipt_count(self):
		return len(STORE.rows("Expense Receipt"))

	def _title_payload(self):
		return {
			"merchant": "Oregon DMV",
			"receipt_date": "2026-03-02",
			"category": "Title/MCO",
			"company": MAIN,
			"submitted_by": "HR-EMP-00001",
		}


# ── 1. ───────────────────────────────────────────────────────────────────────
class TheDoctypesCarryTheColumns(V12TestCase):
	def fields(self, doctype):
		data = json.loads((DOCTYPES / doctype / f"{doctype}.json").read_text())
		return data, {field["fieldname"]: field for field in data["fields"]}

	def test_the_receipt_columns_show_for_exactly_the_document_categories(self):
		data, by_name = self.fields("expense_receipt")
		wanted = (
			"eval:"
			+ json.dumps(list(expenses.DOCUMENT_CATEGORIES), separators=(",", ":"))
			+ ".includes(doc.category)"
		)
		for field in (*vehicle_titles.RECEIPT_FIELDS, "title_section"):
			with self.subTest(field=field):
				self.assertIn(field, data["field_order"])
				self.assertEqual(by_name[field]["depends_on"], wanted)
		self.assertEqual(by_name["linked_asset"]["options"], "Asset Register")
		self.assertEqual(
			by_name["document_subtype"]["options"].split("\n")[1:], list(vehicle_titles.DOCUMENT_SUBTYPES)
		)
		for category in expenses.DOCUMENT_CATEGORIES:
			self.assertIn(category, by_name["category"]["options"].split("\n"))

	def test_the_asset_columns_show_for_exactly_the_titled_types(self):
		data, by_name = self.fields("asset_register")
		wanted = (
			"eval:"
			+ json.dumps(list(vehicle_titles.TITLED_ASSET_TYPES), separators=(",", ":"))
			+ ".includes(doc.asset_type)"
		)
		for field in (*vehicle_titles.ASSET_FIELDS, "title_section"):
			with self.subTest(field=field):
				self.assertIn(field, data["field_order"])
				self.assertEqual(by_name[field]["depends_on"], wanted)
		self.assertEqual(by_name["title_receipt"]["options"], "Expense Receipt")

	def test_every_titled_type_is_a_type_the_farm_ships_with(self):
		shipped = {row[0] for row in asset_types.SEEDED}
		self.assertLessEqual(set(vehicle_titles.TITLED_ASSET_TYPES), shipped)


# ── 2. ───────────────────────────────────────────────────────────────────────
class CapturingATitle(TitleTestCase):
	def test_a_title_needs_no_amount(self):
		data = self.a_title()
		self.assertEqual(data["amount"], 0.0)
		self.assertEqual(data["category"], "Title/MCO")
		self.assertEqual(data["document_subtype"], "Vehicle Title")

	def test_a_printed_price_on_a_bill_of_sale_is_kept(self):
		data = self.a_title(category="Bill of Sale", document_subtype="Bill of Sale", amount=24500)
		self.assertEqual(data["amount"], 24500.0)

	def test_an_expense_still_needs_its_amount(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": "Co-op",
				"receipt_date": "2026-03-02",
				"category": "Fuel",
				"company": MAIN,
				"submitted_by": "HR-EMP-00001",
			},
		)
		self.assertIn("amount is required", error)

	def test_the_vin_is_stored_as_it_compares(self):
		data = self.a_title(vin=" 1ftfw1e5-0kfa 12345 ")
		self.assertEqual(data["vin"], VIN)
		self.assertEqual(frappe.db.get_value("Expense Receipt", data["name"], "vin"), VIN)

	def test_title_arguments_are_refused_on_an_expense(self):
		for key, value in (("vin", VIN), ("document_subtype", "MCO"), ("linked_asset", "MC-Truck-01")):
			with self.subTest(key=key):
				error = self.tool_error(
					"submit_expense_receipt",
					{
						"merchant": "Co-op",
						"amount": 50,
						"receipt_date": "2026-03-02",
						"category": "Fuel",
						"company": MAIN,
						"submitted_by": "HR-EMP-00001",
						key: value,
					},
				)
				self.assertIn(key, error)
				self.assertIn("Nothing was created", error)
		self.assertEqual(self.receipt_count(), 0)

	def test_an_unknown_subtype_is_refused_with_the_list(self):
		error = self.tool_error(
			"submit_expense_receipt", {**self._title_payload(), "document_subtype": "Pink Slip"}
		)
		self.assertIn("Registration", error)


# ── 3. ───────────────────────────────────────────────────────────────────────
class TheVinFindsTheTruck(TitleTestCase):
	def test_one_exact_vin_links_both_ways(self):
		self.a_vehicle(vin=VIN)
		data = self.a_title(vin=VIN.lower())
		self.assertEqual(data["title"]["linked_asset"], "MC-Truck-01")
		self.assertEqual(data["title"]["matched_by"], "vin")
		self.assertEqual(frappe.db.get_value("Expense Receipt", data["name"], "linked_asset"), "MC-Truck-01")
		self.assertEqual(self.asset("MC-Truck-01")["title_receipt"], data["name"])

	def test_a_vin_kept_in_serial_number_is_found_and_copied_across(self):
		"""`register_asset` documented `serial_number` as the serial or VIN for eighty releases."""
		self.a_vehicle(serial_number="1FTFW1E50KFA-12345")
		data = self.a_title(vin=VIN)
		self.assertEqual(data["title"]["matched_by"], "serial_number")
		self.assertTrue(data["title"]["vin_copied"])
		self.assertEqual(self.asset("MC-Truck-01")["vin"], VIN)

	def test_two_trucks_answering_to_one_vin_link_neither(self):
		self.a_vehicle(serial_number=VIN)
		self.a_vehicle(name="MC-Truck-02", serial_number=VIN)
		data = self.a_title(vin=VIN)
		self.assertIsNone(data["title"]["linked_asset"])
		self.assertEqual(data["title"]["candidates"], ["MC-Truck-01", "MC-Truck-02"])
		self.assertIsNone(self.asset("MC-Truck-01")["title_receipt"])
		self.assertIsNone(self.asset("MC-Truck-02")["title_receipt"])

	def test_another_companys_truck_is_not_matched(self):
		self.a_vehicle(company=OTHER, vin=VIN)
		data = self.a_title(vin=VIN)
		self.assertIsNone(data["title"]["linked_asset"])
		self.assertEqual(data["title"]["candidates"], [])

	def test_a_sprayer_with_the_same_serial_is_not_a_titled_asset(self):
		self.a_vehicle(name="MC-Sprayer-01", asset_type="Sprayer", serial_number=VIN)
		self.assertIsNone(self.a_title(vin=VIN)["title"]["linked_asset"])

	def test_a_tractor_is_matched(self):
		self.a_vehicle(name="MC-Tractor-01", asset_type="Tractor", vin="RW7230X012345")
		self.assertEqual(self.a_title(vin="RW7230X012345")["title"]["linked_asset"], "MC-Tractor-01")

	def test_an_automatic_match_keeps_a_title_already_filed(self):
		self.a_vehicle(vin=VIN)
		first = self.a_title(vin=VIN)["name"]
		second = self.a_title(vin=VIN, document_subtype="Registration")
		self.assertEqual(second["title"]["title_receipt_kept"], first)
		self.assertEqual(self.asset("MC-Truck-01")["title_receipt"], first)
		# The document itself still records which truck it is for.
		self.assertEqual(
			frappe.db.get_value("Expense Receipt", second["name"], "linked_asset"), "MC-Truck-01"
		)

	def test_a_person_naming_the_asset_replaces_it(self):
		self.a_vehicle(vin=VIN)
		first = self.a_title(vin=VIN)["name"]
		second = self.a_title(linked_asset="MC-Truck-01")
		self.assertEqual(second["title"]["matched_by"], "argument")
		self.assertEqual(second["title"]["title_receipt_replaced"], first)
		self.assertEqual(self.asset("MC-Truck-01")["title_receipt"], second["name"])

	def test_a_disagreeing_vin_is_reported_and_not_overwritten(self):
		self.a_vehicle(vin=VIN)
		data = self.a_title(linked_asset="MC-Truck-01", vin="1FTFW1E50KFA99999")
		self.assertEqual(data["title"]["vin_mismatch"], {"document": "1FTFW1E50KFA99999", "asset": VIN})
		self.assertEqual(self.asset("MC-Truck-01")["vin"], VIN)

	def test_a_named_asset_that_cannot_hold_a_title_refuses_the_whole_capture(self):
		self.a_vehicle(name="MC-Valve-01", asset_type="Irrigation Valve")
		self.a_vehicle(name="OT-Truck-01", company=OTHER)
		for asset, words in (
			("MC-Valve-01", "Irrigation Valve"),
			("OT-Truck-01", OTHER),
			("MC-Nothing", "no Asset Register"),
		):
			with self.subTest(asset=asset):
				error = self.tool_error(
					"submit_expense_receipt", {**self._title_payload(), "linked_asset": asset}
				)
				self.assertIn(words, error)
		self.assertEqual(self.receipt_count(), 0)


# ── 4. ───────────────────────────────────────────────────────────────────────
class NothingReachesTheLedger(TitleTestCase):
	def test_the_expense_summary_leaves_documents_out(self):
		self.tool_data(
			"submit_expense_receipt",
			{
				"merchant": "Co-op",
				"amount": 100,
				"receipt_date": "2026-03-02",
				"category": "Fuel",
				"company": MAIN,
				"submitted_by": "HR-EMP-00001",
			},
		)
		self.a_title(category="Bill of Sale", amount=24500)
		data = self.tool_data(
			"get_expense_summary", {"company": MAIN, "from_date": "2026-01-01", "to_date": "2026-12-31"}
		)
		self.assertEqual(data["total_amount"], 100.0)
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["documents_excluded"], 1)
		self.assertNotIn("Bill of Sale", data["by_category"])
		self.assertIn("left out", data["note"])


class NoPurchaseInvoiceFromADocument(PurchasingTestCase):
	"""On the purchasing fixture, so the refusal is the category's and not a missing Buying module."""

	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, **ON)

	def a_title(self, **overrides):
		return TitleTestCase.a_title(self, **overrides)

	def test_a_purchase_invoice_is_refused_by_name(self):
		for category in expenses.DOCUMENT_CATEGORIES:
			with self.subTest(category=category):
				name = self.a_title(category=category, amount=24500)["name"]
				self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00001"})
				error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
				self.assertIn("vehicle document", error)
				self.assertFalse(frappe.db.get_value("Expense Receipt", name, "linked_document"))


# ── 5. ───────────────────────────────────────────────────────────────────────
class LinkingAfterwards(TitleTestCase):
	def link(self, receipt, asset):
		return vehicle_titles.link_title_to_asset({"receipt": receipt, "asset": asset}).data

	def test_both_sides_are_set_and_a_missing_vin_is_copied(self):
		self.a_vehicle()
		name = self.a_title(vin=VIN)["name"]  # no VIN on the truck, so nothing matched
		data = self.link(name, "MC-Truck-01")
		self.assertTrue(data["vin_copied"])
		self.assertEqual(self.asset("MC-Truck-01")["vin"], VIN)
		self.assertEqual(self.asset("MC-Truck-01")["title_receipt"], name)
		self.assertEqual(frappe.db.get_value("Expense Receipt", name, "linked_asset"), "MC-Truck-01")

	def test_a_bill_of_sale_links_as_well(self):
		self.a_vehicle()
		name = self.a_title(category="Bill of Sale", document_subtype="Bill of Sale")["name"]
		self.assertEqual(self.link(name, "MC-Truck-01")["title_receipt"], name)

	def test_an_expense_is_refused(self):
		self.a_vehicle()
		name = self.tool_data(
			"submit_expense_receipt",
			{
				"merchant": "Co-op",
				"amount": 100,
				"receipt_date": "2026-03-02",
				"category": "Fuel",
				"company": MAIN,
				"submitted_by": "HR-EMP-00001",
			},
		)["name"]
		with self.assertRaises(ToolError) as caught:
			self.link(name, "MC-Truck-01")
		self.assertIn("'Fuel'", str(caught.exception))
		self.assertIsNone(self.asset("MC-Truck-01")["title_receipt"])

	def test_an_asset_that_cannot_hold_a_title_is_refused(self):
		self.a_vehicle(name="MC-Valve-01", asset_type="Irrigation Valve")
		name = self.a_title()["name"]
		with self.assertRaises(ToolError):
			self.link(name, "MC-Valve-01")

	def test_moving_a_document_clears_the_truck_it_left(self):
		self.a_vehicle()
		self.a_vehicle(name="MC-Truck-02")
		name = self.a_title(linked_asset="MC-Truck-01")["name"]
		data = self.link(name, "MC-Truck-02")
		self.assertEqual(data["unlinked_from"], "MC-Truck-01")
		self.assertIsNone(self.asset("MC-Truck-01")["title_receipt"])
		self.assertEqual(self.asset("MC-Truck-02")["title_receipt"], name)


# ── 6. ───────────────────────────────────────────────────────────────────────
class RegisteringAVehicle(TitleTestCase):
	def test_the_four_title_arguments_are_stored_on_a_vehicle(self):
		data = self.a_vehicle(
			vin="1ftfw1e50kfa12345",
			license_plate="ABC 123",
			title_holder="Orchard Meadow, LLC",
			lien_holder="Farm Credit West",
		)
		self.assertEqual(data["vin"], VIN)
		stored = frappe.db.get_value(
			"Asset Register", "MC-Truck-01", list(vehicle_titles.REGISTRABLE_ASSET_FIELDS), as_dict=True
		)
		self.assertEqual(
			stored,
			{
				"vin": VIN,
				"license_plate": "ABC 123",
				"title_holder": "Orchard Meadow, LLC",
				"lien_holder": "Farm Credit West",
			},
		)

	def test_they_are_refused_on_anything_but_a_vehicle_or_tractor(self):
		error = self.tool_error(
			"register_asset",
			{"name": "MC-Valve-01", "asset_type": "Irrigation Valve", "company": MAIN, "lien_holder": "Bank"},
		)
		self.assertIn("lien_holder", error)
		self.assertFalse(frappe.db.exists("Asset Register", "MC-Valve-01"))

	def test_a_duplicate_vin_is_refused(self):
		self.a_vehicle(vin=VIN)
		error = self.tool_error(
			"register_asset", {"name": "MC-Truck-02", "asset_type": "Vehicle", "company": MAIN, "vin": VIN}
		)
		self.assertIn("MC-Truck-01", error)

	def test_the_detail_answers_the_title_and_the_documents_filed(self):
		self.a_vehicle(vin=VIN, lien_holder="Farm Credit West")
		title = self.a_title(vin=VIN)["name"]
		self.a_title(vin=VIN, document_subtype="Registration", receipt_date="2026-04-01")
		data = self.tool_data("get_asset_detail", {"asset_name": "MC-Truck-01"})
		self.assertEqual(data["vin"], VIN)
		self.assertEqual(data["lien_holder"], "Farm Credit West")
		self.assertEqual(data["title_receipt"], title)
		self.assertEqual(data["title_document"]["name"], title)
		self.assertEqual(data["title_document"]["document_subtype"], "Vehicle Title")
		self.assertEqual(data["title_document_count"], 2)
		self.assertEqual(data["title_documents"][0]["document_subtype"], "Registration")

	def test_an_asset_with_no_title_answers_empty_not_missing(self):
		self.a_vehicle(name="MC-Valve-01", asset_type="Irrigation Valve")
		data = self.tool_data("get_asset_detail", {"asset_name": "MC-Valve-01"})
		self.assertIsNone(data["title_receipt"])
		self.assertIsNone(data["title_document"])
		self.assertEqual(data["title_documents"], [])


# ── 7. ───────────────────────────────────────────────────────────────────────
class FromAPhone(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.be()

	def a_vehicle(self, name="MC-Truck-01", company=MAIN, **kw):
		return asset_tags.register_asset(
			{"name": name, "asset_type": "Vehicle", "company": company, **kw}
		).data

	def a_title(self, **overrides):
		payload = {
			"merchant": "Oregon DMV",
			"receipt_date": "2026-03-02",
			"category": "Title/MCO",
			**overrides,
		}
		return mobile_api.create_expense_receipt(**payload)

	def test_a_title_photographed_on_a_phone_finds_its_truck(self):
		self.a_vehicle(vin=VIN)
		data = self.a_title(vin=VIN, document_subtype="MCO")
		self.assertEqual(data["title"]["linked_asset"], "MC-Truck-01")

	def test_a_linked_asset_in_another_entity_reads_as_not_found(self):
		self.a_vehicle(name="OT-Truck-01", company=OTHER)
		with self.assertRaises(Exception) as caught:
			self.a_title(linked_asset="OT-Truck-01")
		self.assertIn("not found", str(caught.exception))

	def test_link_title_to_asset_files_a_title_in_scope(self):
		self.a_vehicle()
		name = self.a_title()["name"]
		self.assertEqual(
			mobile_api.link_title_to_asset(receipt=name, asset="MC-Truck-01")["title_receipt"], name
		)

	def test_link_title_to_asset_reads_another_entitys_truck_as_not_found(self):
		"""Separate from the test above: a refused mobile call rolls the whole test back."""
		self.a_vehicle(name="OT-Truck-01", company=OTHER)
		name = self.a_title()["name"]
		with self.assertRaises(Exception) as caught:
			mobile_api.link_title_to_asset(receipt=name, asset="OT-Truck-01")
		self.assertIn("not found", str(caught.exception))

	def test_another_entitys_asset_detail_reads_as_not_found(self):
		self.a_vehicle(name="OT-Truck-01", company=OTHER, lien_holder="Farm Credit West")
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_asset_detail(asset_name="OT-Truck-01")

	def test_the_callers_own_asset_detail_still_opens(self):
		"""The other half of the control above."""
		self.a_vehicle(lien_holder="Farm Credit West")
		self.assertEqual(
			mobile_api.get_asset_detail(asset_name="MC-Truck-01")["lien_holder"], "Farm Credit West"
		)

	def test_the_register_route_forwards_the_title_arguments(self):
		data = mobile_api.register_asset(
			name="MC-Truck-09", asset_type="Vehicle", company=MAIN, vin=VIN, license_plate="XYZ 789"
		)
		self.assertEqual(data["vin"], VIN)
		self.assertEqual(frappe.db.get_value("Asset Register", "MC-Truck-09", "license_plate"), "XYZ 789")
