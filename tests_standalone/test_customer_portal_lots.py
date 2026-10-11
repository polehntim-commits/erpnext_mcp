# SPDX-License-Identifier: MIT
"""The customer portal's deliveries and lots — FSMA 204 for a first receiver. v0.314.0.

Scope is what went to THIS customer: scale tickets naming it (or on its settlement statements), and the lots those
tickets resolve to or that a Shipping CTE names it as receiver of. Each lot carries its critical tracking events
(KDEs); a Shipping CTE to another customer is left out; no person and no price is ever shown. The FSMA 204 sortable
spreadsheet is one row per CTE.
"""

import csv
import io
import json

import frappe

from erpnext_mcp import customer_portal

from .harness import STORE
from .test_api_mobile import MAIN
from .test_field_self_service import CENTER, WIND
from .test_packer_portal import PortalCase


class LotsAndDeliveries(PortalCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Customer", [{"name": "Monson Fruit", "customer_name": "Monson Fruit"}])
		STORE.seed("Settlement Statement", [{"name": "SS-OVF-1", "customer": "Orchard View Farms", "company": MAIN,
		                                     "date": "2027-07-30"}])
		STORE.seed("Scale Ticket", [
			{"name": "ST-1", "company": MAIN, "customer": "Orchard View Farms", "date": "2027-07-01", "field": CENTER,
			 "variety": "Bing", "grade": "#1", "net_weight": 4000, "weight_uom": "lb", "destination": "OVF Cashmere",
			 "status": "Submitted"},
			{"name": "ST-2", "company": MAIN, "settlement": "SS-OVF-1", "date": "2027-07-02", "field": CENTER,
			 "variety": "Bing", "net_weight": 3000, "weight_uom": "lb", "status": "Submitted"},
			{"name": "ST-3", "company": MAIN, "customer": "Monson Fruit", "date": "2027-07-03", "field": WIND,
			 "variety": "Rainier", "net_weight": 2500, "weight_uom": "lb", "status": "Submitted"},
		])
		STORE.seed("Traceability Lot Code", [
			{"name": "MCC-BING-20270701", "lot_code": "MCC-BING-20270701", "company": MAIN, "field": CENTER,
			 "variety": "Bing", "harvest_date": "2027-07-01", "quantity": 7000, "quantity_uom": "lb", "status": "Shipped"},
			{"name": "MCW-RAIN-20270703", "lot_code": "MCW-RAIN-20270703", "company": MAIN, "field": WIND,
			 "variety": "Rainier", "harvest_date": "2027-07-03", "quantity": 2500, "quantity_uom": "lb",
			 "status": "Shipped"},
		])
		STORE.seed("Critical Tracking Event", [
			{"name": "CTE-1", "company": MAIN, "lot_code": "MCC-BING-20270701", "event_type": "Growing",
			 "event_datetime": "2027-05-02 07:00:00", "location": CENTER, "actor": "HR-EMP-00002",
			 "actor_name": "Beto Cruz", "description": "Captan by Beto", "reference_doctype": "Spray Application"},
			{"name": "CTE-2", "company": MAIN, "lot_code": "MCC-BING-20270701", "event_type": "Shipping",
			 "event_datetime": "2027-07-01 15:00:00", "location": CENTER, "receiver": "Orchard View Farms",
			 "destination_location": "OVF Cashmere", "carrier": "Hal's Trucking", "quantity": 4000,
			 "quantity_uom": "lb", "reference_doctype": "Scale Ticket", "reference_name": "ST-1"},
			{"name": "CTE-3", "company": MAIN, "lot_code": "MCC-BING-20270701", "event_type": "Shipping",
			 "event_datetime": "2027-07-01 16:00:00", "location": CENTER, "receiver": "Monson Fruit",
			 "destination_location": "Monson Selah", "quantity": 500, "quantity_uom": "lb",
			 "reference_doctype": "Scale Ticket"},
			{"name": "CTE-4", "company": MAIN, "lot_code": "MCW-RAIN-20270703", "event_type": "Shipping",
			 "event_datetime": "2027-07-03 15:00:00", "location": WIND, "receiver": "Monson Fruit",
			 "reference_doctype": "Scale Ticket"},
		])
		STORE.commit()

	def test_only_its_own_tickets_including_by_settlement(self):
		rows = customer_portal.deliveries(self.share, "2027")
		self.assertEqual([r["ticket"] for r in rows], ["ST-1", "ST-2"])
		self.assertEqual(rows[0]["block"], "MCC")

	def test_only_its_own_lots_and_shipping_events(self):
		got = customer_portal.lots(self.share, "2027")
		self.assertEqual([lot["lot_code"] for lot in got], ["MCC-BING-20270701"])
		events = got[0]["ctes"]
		self.assertEqual([e["event_type"] for e in events], ["Growing", "Shipping"])
		self.assertEqual(events[1]["receiver"], "Orchard View Farms")
		self.assertEqual(events[0]["location"], "MCC", "a block is shown by its ticker")

	def test_never_a_person_or_a_price(self):
		blob = json.dumps(customer_portal.page(self.share, "2027"), default=str)
		for word in ("Beto", "HR-EMP", "Monson", "price", "Selah", "MCW-RAIN"):
			self.assertNotIn(word, blob, word)

	def test_the_fsma_204_sortable_spreadsheet(self):
		body, kind, name = customer_portal.pack(self.share, season="2027", fmt="fsma204")
		self.assertTrue(name.endswith("_fsma204.csv"))
		rows = list(csv.reader(io.StringIO(body.decode())))
		self.assertEqual(rows[0], list(customer_portal.FSMA_COLUMNS))
		self.assertEqual(len(rows), 3, "one row per CTE")
		self.assertEqual(rows[2][4], "Shipping")
		self.assertEqual(rows[2][12], MAIN, "the grower is the traceability lot code source")

	def test_another_customers_portal_sees_only_its_own(self):
		other = customer_portal.create_share(MAIN, "Monson Fruit", ["Wind Mecine"], season="2027", role="Buyer")
		STORE.commit()
		self.assertEqual([r["ticket"] for r in customer_portal.deliveries(other, "2027")], ["ST-3"])
		self.assertEqual([lot["lot_code"] for lot in customer_portal.lots(other, "2027")],
		                 ["MCC-BING-20270701", "MCW-RAIN-20270703"])
		bing = customer_portal.lots(other, "2027")[0]
		self.assertEqual([e.get("receiver") for e in bing["ctes"] if e["event_type"] == "Shipping"], ["Monson Fruit"])

	def test_the_sections_toggle(self):
		doc = frappe.get_doc(customer_portal.SHARE, self.share)
		doc.sections = json.dumps({**json.loads(doc.sections), "lots": 0})
		doc.save(ignore_permissions=True)
		STORE.commit()
		self.assertNotIn("lots", customer_portal.page(self.share, "2027"))
		with self.assertRaises(customer_portal.PortalError):
			customer_portal.pack(self.share, season="2027", fmt="fsma204")
