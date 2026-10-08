# SPDX-License-Identifier: MIT
"""Chemical receiving. v0.272.0 (docs/contracts/chemical_receiving_v0_272.yaml).

A Wilbur-Ellis delivery against a submitted PO: off until the company turns it on; lines from a portal CSV, an
emailed invoice's text, the phone or a person; checked (qty, EPA shape, a product); matched by the connector's item
numbers, the Item code, the EPA number or the PO line's name; reconciled (ok / short / over / substitution / not
ordered / unmatched / unit); a person settles what is not ok; a DRAFT Purchase Receipt into chemical storage with a
Batch per lot; the check-in task with the RUP product on it (so the Applicator License gate applies); the job
link's ticket photo opens the intake; a lot traced forward to the spray that recorded it; the phone's gates.
"""

import json

import frappe

from erpnext_mcp import (
	compliance_fields,
	config_lifecycle,
	flags,
	job_links,
	phone_config,
	receiving,
	task_templates,
)
from erpnext_mcp.api import mobile as mobile_api

from . import test_contract_v0_262_0 as v262
from .harness import STORE, register_doctype, set_roles
from .test_api_mobile import MAIN, WORKER
from .test_farmops_api import PREFIX, FarmOpsAPITestCase

SUPPLIER = "Wilbur-Ellis"
SHED = "Chemical Shed - ETC"
CAPTAN, LORSBAN, OIL, ZIRAM = "CAPTAN-80", "LORSBAN-4E", "SUPERIOR-OIL", "ZIRAM-76"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
CSV = """Invoice #,PO #,Ship Date,Product #,Product Description,Quantity,UOM,Lot Number,EPA Reg #
INV-7781,{po},10/06/2026,WE-100,Captan 80 WDG,40,lb,C2609A,66330-38
INV-7781,{po},10/06/2026,WE-200,Lorsban 4E,4,gal,L7781,62719-220
INV-7781,{po},10/06/2026,WE-300,Superior Oil 415,30,gal,,
"""


def _item(code, name, uom, *, epa="", batch=0, rup=0, storage=""):
	row = {"name": code, "item_code": code, "item_name": name, "item_group": "Chemicals", "stock_uom": uom,
	       "is_stock_item": 1, "disabled": 0, "has_batch_no": batch, "item_defaults": [], "reorder_levels": []}
	if epa:
		row["epa_registration_number"] = epa
	if rup:
		row["restricted_use"] = 1
	if storage:
		row["storage_disposal"] = storage
	return row


def receiving_site() -> str:
	"""Wilbur-Ellis, the chemical shed, four products and a submitted PO for three of them. Returns the PO."""
	compliance_fields.install_compliance_fields()
	# The fixture site has no Batch register (a real configuration — see test_stock_inventory); this one does.
	register_doctype("Batch", [{"fieldname": f, "fieldtype": "Data"} for f in
	                           ("batch_id", "item", "supplier", "expiry_date", "reference_doctype", "reference_name")])
	STORE.seed("Supplier", [{"name": SUPPLIER, "supplier_name": SUPPLIER}])
	STORE.seed("Warehouse", [{"name": SHED, "warehouse_name": "Chemical Shed", "company": MAIN}])
	STORE.seed("UOM", [{"name": "Lb", "enabled": 1}, {"name": "Gal", "enabled": 1}])
	STORE.seed("Item", [
		_item(CAPTAN, "Captan 80 WDG", "Lb", epa="66330-38", batch=1),
		_item(LORSBAN, "Lorsban 4E", "Gal", epa="62719-220", batch=1, rup=1,
		      storage="Store above 32 °F. Do not allow to freeze."),
		_item(OIL, "Superior Oil 415", "Gal"),
		_item(ZIRAM, "Ziram 76DF", "Lb", epa="70506-1"),
	])
	STORE.seed("File", [{"name": "sds-captan", "file_name": "Captan 80 SDS.pdf", "attached_to_doctype": "Item",
	                     "attached_to_name": CAPTAN, "is_private": 1}])
	task_templates.seed_farm_task_templates()
	receiving.seed()
	po = frappe.new_doc("Purchase Order")
	po.company, po.supplier, po.transaction_date, po.schedule_date = MAIN, SUPPLIER, "2026-10-01", "2026-10-06"
	for code, qty, uom, rate in ((CAPTAN, 50, "Lb", 6.2), (LORSBAN, 4, "Gal", 58.0), (OIL, 30, "Gal", 9.5)):
		po.append("items", {"item_code": code, "qty": qty, "uom": uom, "rate": rate, "amount": qty * rate,
		                    "warehouse": SHED, "schedule_date": "2026-10-06", "received_qty": 0})
	po.insert(ignore_permissions=True)
	frappe.db.set_value("Purchase Order", po.name, "docstatus", 1)
	STORE.commit()
	return po.name


def turn_on(warehouse=True) -> None:
	flags.upsert(receiving.FLAG, "Flag", True, company=MAIN, description="test", owner_area="receiving", active=True)
	if warehouse:
		flags.upsert(receiving.WAREHOUSE_FLAG, "Text", SHED, company=MAIN, description="test",
		             owner_area="receiving", active=True)
	STORE.commit()


class ReceivingCase(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		self.po = receiving_site()

	def turn_on(self, warehouse=True):
		turn_on(warehouse)

	def publish(self, key, **changes):
		body = {**receiving.SEED_CONNECTORS[key], **changes}
		doc, _report = phone_config.save_draft(receiving.KIND, key, body, "test", "Operator")
		with config_lifecycle.desk_action():
			phone_config.publish(receiving.KIND, key, doc.version, "test", "Administrator")
		STORE.commit()

	def from_csv(self):
		self.turn_on()
		self.publish("wilbur_ellis_portal_csv",
		             item_map={"WE-100": CAPTAN, "WE-200": LORSBAN, "WE-300": OIL})
		made = receiving.ingest_csv(MAIN, "wilbur_ellis_portal_csv", CSV.format(po=self.po), actor="tim")
		STORE.commit()
		return made[0]


class OffUntilOn(ReceivingCase):
	def test_refused_for_a_company_that_has_not_turned_it_on(self):
		with self.assertRaisesRegex(receiving.ReceivingError, "chemical receiving is off"):
			receiving.open_intake(MAIN, purchase_order=self.po)
		self.assertEqual({r["status"] for r in phone_config.rows(receiving.KIND)}, {"Draft"}, "connectors seed as drafts")


class FromThePortalCSV(ReceivingCase):
	def test_one_intake_per_invoice_matched_and_reconciled(self):
		data = self.from_csv()
		self.assertEqual(data["document_no"], "INV-7781")
		self.assertEqual(data["purchase_order"], self.po, "the PO number in the export")
		by_item = {line["item_code"]: line for line in data["lines"]}
		self.assertEqual(by_item[CAPTAN]["match"], "short")
		self.assertEqual(by_item[CAPTAN]["delta"], -10)
		self.assertEqual(by_item[LORSBAN]["match"], "ok")
		self.assertTrue(by_item[LORSBAN]["restricted_use"] and by_item[LORSBAN]["freeze_sensitive"])
		self.assertTrue(by_item[CAPTAN]["sds_on_file"])
		self.assertFalse(by_item[LORSBAN]["sds_on_file"])
		self.assertEqual(data["status"], "Needs Review")
		again = receiving.ingest_csv(MAIN, "wilbur_ellis_portal_csv", CSV.format(po=self.po))
		self.assertTrue(again[0]["already"], "an invoice number already taken in is not doubled")

	def test_checks_refuse_a_bad_document(self):
		self.turn_on()
		name = receiving.open_intake(MAIN, purchase_order=self.po)
		with self.assertRaisesRegex(receiving.ReceivingError, "EPA number"):
			receiving.set_lines(name, [{"item_text": "Captan", "qty": 5, "epa_reg_number": "S24-S37"}])
		with self.assertRaisesRegex(receiving.ReceivingError, "more than 0"):
			receiving.set_lines(name, [{"item_text": "Captan", "qty": 0}])


class Matching(ReceivingCase):
	def test_epa_number_name_substitution_not_ordered_unit_and_unmatched(self):
		self.turn_on()
		name = receiving.open_intake(MAIN, purchase_order=self.po)
		data = receiving.set_lines(name, [
			{"item_text": "CAPTAN 80WDG 50# BAG", "qty": 50, "uom": "lbs", "epa_reg_number": "66330-38"},
			{"item_text": "superior oil 415", "qty": 30, "uom": "gallons"},
			{"item_text": "Ziram 76DF", "qty": 20, "uom": "lb", "epa_reg_number": "70506-1"},
			{"item_text": "Lorsban 4E", "qty": 2, "uom": "case", "lot_no": "L1"},
			{"item_text": "Mystery jug", "qty": 1},
		])
		got = [(line["item_code"], line["match"], line["matched_by"]) for line in data["lines"]]
		self.assertEqual(got, [(CAPTAN, "ok", "EPA number"), (OIL, "ok", "name on the PO"),
		                       (ZIRAM, "not_ordered", "EPA number"), (LORSBAN, "uom_check", "name on the PO"),
		                       (None, "unmatched", None)])
		self.assertEqual(data["ordered_not_delivered"], [])

	def test_a_twin_by_epa_number_is_a_substitution_and_missing_lines_are_listed(self):
		self.turn_on()
		frappe.db.set_value("Item", ZIRAM, "epa_registration_number", "66330-38")
		name = receiving.open_intake(MAIN, purchase_order=self.po)
		data = receiving.set_lines(name, [{"item_code": ZIRAM, "qty": 50}])
		self.assertEqual(data["lines"][0]["match"], "substitution")
		self.assertEqual({m["item_code"] for m in data["ordered_not_delivered"]}, {LORSBAN, OIL})


class SettleAndDraft(ReceivingCase):
	def test_short_accepted_then_draft_receipt_with_batches_and_check_in(self):
		data = self.from_csv()
		name = data["intake"]
		with self.assertRaisesRegex(receiving.ReceivingError, "settle line"):
			receiving.draft_receipt(name)
		receiving.resolve_line(name, 1, resolution="Accept", note="rest on backorder", actor="tim")
		out = receiving.draft_receipt(name, "tim")
		STORE.commit()
		pr = frappe.get_doc("Purchase Receipt", out["purchase_receipt"])
		self.assertEqual(int(pr.docstatus or 0), 0, "a draft — a person submits it")
		rows = {r.get("item_code"): r for r in pr.get("items")}
		self.assertEqual(rows[CAPTAN].get("qty"), 40)
		self.assertEqual(rows[CAPTAN].get("warehouse"), SHED)
		self.assertEqual(rows[CAPTAN].get("batch_no"), "C2609A")
		self.assertTrue(rows[CAPTAN].get("purchase_order_item"))
		self.assertFalse(rows[OIL].get("batch_no"), "no lot on the line")
		self.assertEqual(frappe.db.get_value("Batch", "L7781", "item"), LORSBAN)
		task = frappe.get_doc("Farm Task", out["check_in_task"])
		self.assertEqual(task.source_workorder, f"receiving:{name}")
		self.assertIn(LORSBAN, [m["item_code"] for m in json.loads(task.materials_used)])
		self.assertIn("RESTRICTED USE", task.notes)
		self.assertIn("NO SDS ON FILE", task.notes)
		self.assertTrue(receiving.draft_receipt(name)["already"])

	def test_needs_a_warehouse_and_a_reject_leaves_the_line_off(self):
		self.turn_on(warehouse=False)
		name = receiving.open_intake(MAIN, purchase_order=self.po)
		receiving.set_lines(name, [{"item_code": CAPTAN, "qty": 50, "lot_no": "C1"},
		                           {"item_code": ZIRAM, "qty": 5}])
		receiving.resolve_line(name, 2, resolution="Reject", note="not ordered — sent back on the truck")
		with self.assertRaisesRegex(receiving.ReceivingError, "chemical_storage_warehouse"):
			receiving.draft_receipt(name)
		self.turn_on()
		out = receiving.draft_receipt(name)
		self.assertEqual([r.get("item_code") for r in frappe.get_doc("Purchase Receipt", out["purchase_receipt"]).get("items")],
		                 [CAPTAN])


class FromTheEmailAndTheJob(ReceivingCase):
	def test_invoice_text_through_the_line_pattern(self):
		body = receiving.SEED_CONNECTORS["wilbur_ellis_email_pdf"]
		text = ("WILBUR-ELLIS  Invoice No: INV-9001\nPO #: " + self.po + "\nShip Date: 10/06/2026\n"
		        "100200 CAPTAN 80 WDG 40 LB LOT: C2609A EPA: 66330-38\n100300 SUPERIOR OIL 415 30 GAL\n")
		documents = receiving.adapter(body).to_lines(text)
		self.assertEqual(documents[0]["document_no"], "INV-9001")
		self.assertEqual(documents[0]["po_number"], self.po)
		self.assertEqual([(line["sku"], line["qty"], line["uom"], line["lot_no"]) for line in documents[0]["lines"]],
		                 [("100200", "40", "LB", "C2609A"), ("100300", "30", "GAL", "")])
		self.assertTrue(receiving.adapter(body).recognises("ar@wilburellis.com"))
		self.assertFalse(receiving.adapter(body).recognises("ar@wilburellis.com.evil.test"))

	def test_unshipped_channels_say_so(self):
		with self.assertRaisesRegex(receiving.ReceivingError, "configuration only"):
			receiving.adapter(receiving.SEED_CONNECTORS["agvance_rest_api"]).fetch("2026-10-01")
		self.assertEqual(receiving.validate(receiving.SEED_CONNECTORS["agvance_rest_api"])["errors"], [])
		self.assertTrue(receiving.validate({"channel": "portal_csv", "supplier": "X", "csv": {"columns": {"qty": "Q"}}})["errors"])

	def test_the_ticket_photo_opens_the_intake_once(self):
		self.turn_on()
		flags.upsert(job_links.FLAG, "Flag", True, company=MAIN, description="t", owner_area="job_links", active=True)
		job_links.seed()
		job = job_links.create_job("supplier_delivery", MAIN, supplier=SUPPLIER, purchase_order=self.po,
		                           title="Wilbur-Ellis fall order")
		job_links.mark_ready(job, "tim")
		token = job_links.issue_link(job, "tim")["url"].rsplit("/", 1)[1]
		STORE.commit()
		link = job_links.find(token)
		job_links.attach_photo(link, JPEG, kind="Delivery ticket")
		job_links.record_event(link, "Delivered")
		intakes = [r for r in STORE.rows(receiving.INTAKE) if r.get("job") == job]
		self.assertEqual(len(intakes), 1)
		self.assertEqual((intakes[0]["source"], intakes[0]["status"], intakes[0]["purchase_order"]),
		                 ("ticket_photo", "Awaiting Lines", self.po))
		self.assertTrue(intakes[0]["source_file"])


class TraceTheLot(ReceivingCase):
	def test_the_spray_that_recorded_the_lot(self):
		data = self.from_csv()
		receiving.resolve_line(data["intake"], 1, resolution="Accept")
		receiving.draft_receipt(data["intake"])
		STORE.seed("Spray Application", [
			{"name": "SPRAY-1", "company": MAIN, "completed_at": "2026-10-09 08:00:00",
			 "products_applied": json.dumps([{"item": CAPTAN, "lot_no": "C2609A"}]), "blocks": []},
			{"name": "SPRAY-2", "company": MAIN, "completed_at": "2026-10-10 08:00:00",
			 "products_applied": json.dumps([{"item": CAPTAN, "lot_no": "OTHER"}]), "blocks": []},
			{"name": "SPRAY-3", "company": MAIN, "completed_at": "2026-10-11 08:00:00",
			 "products_applied": json.dumps([{"item": CAPTAN}]), "blocks": []},
		])
		trace = receiving.trace_lot("C2609A")
		self.assertEqual(trace["arrived"][0]["item_code"], CAPTAN)
		self.assertEqual({(s["spray_application"], s["lot_recorded"]) for s in trace["sprays"]},
		                 {("SPRAY-1", True), ("SPRAY-3", False)})


class FromThePhone(ReceivingCase):
	def test_a_foreman_reads_and_posts_lines_and_a_manager_drafts(self):
		self.turn_on()
		name = receiving.open_intake(MAIN, purchase_order=self.po, source="ticket_photo")
		STORE.commit()
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_delivery_intakes()
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be(WORKER)
		self.assertEqual([r["name"] for r in mobile_api.list_delivery_intakes()["intakes"]], [name])
		answer = mobile_api.submit_delivery_lines(intake=name, lines=json.dumps([{"item_text": "Captan 80 WDG",
		                                                                          "qty": 50, "lot_no": "C9"}]))
		self.assertEqual(answer["status"], "Matched")
		STORE.commit()
		self.assertFalse(mobile_api.get_delivery_intake(intake=name)["may_settle"])
		with self.assertRaises(frappe.PermissionError):
			mobile_api.draft_delivery_receipt(intake=name)
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be(WORKER)
		self.assertTrue(mobile_api.draft_delivery_receipt(intake=name)["purchase_receipt"])

	def test_over_http_another_companys_delivery_is_not_found(self):
		self.turn_on()
		name = receiving.open_intake(MAIN, purchase_order=self.po)
		frappe.db.set_value(receiving.INTAKE, name, "company", "Second Example Ltd")
		STORE.commit()
		set_roles(WORKER, ["Field Worker", "Foreman"])
		answer = self.post(f"{PREFIX}/mobile/get_delivery_intake", {"intake": name})
		self.assertEqual(answer.status_code, 404)


class Contract(ReceivingCase):
	HERE = v262.HERE.parent / "v0_272_0"

	def check(self, name, value):
		rows = [r for key in ("lines", "ordered_not_delivered") if isinstance(value.get(key), list) for r in value[key]]
		for row in rows:
			if row.get("po_item"):
				row["po_item"] = "<po_item>"
		before = v262.HERE
		v262.HERE = self.HERE
		try:
			v262.check(self, name, value)
		finally:
			v262.HERE = before

	def test_the_phone_answers(self):
		self.turn_on()
		name = receiving.open_intake(MAIN, purchase_order=self.po, source="ticket_photo")
		STORE.commit()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be(WORKER)
		lines = [{"item_text": "Captan 80 WDG", "qty": 40, "uom": "lb", "lot_no": "C2609A", "epa_reg_number": "66330-38"},
		         {"item_text": "Lorsban 4E", "qty": 4, "uom": "gal", "lot_no": "L7781"}]
		self.check("submit_delivery_lines", mobile_api.submit_delivery_lines(intake=name, lines=json.dumps(lines),
		                                                                     document_no="T-1001"))
		STORE.commit()
		self.check("resolve_delivery_line", mobile_api.resolve_delivery_line(intake=name, idx=1, resolution="Accept",
		                                                                     note="backorder"))
		STORE.commit()
		listed = mobile_api.list_delivery_intakes()
		for row in listed["intakes"]:
			row["creation"] = "<creation>"
		self.check("list_delivery_intakes", listed)
		self.check("draft_delivery_receipt", mobile_api.draft_delivery_receipt(intake=name))
		STORE.commit()
		self.check("get_delivery_intake", mobile_api.get_delivery_intake(intake=name))


class FromAPDF(ReceivingCase):
	def test_the_pdf_text_is_read_where_the_server_can(self):
		try:
			import pypdf  # noqa: F401
			from reportlab.pdfgen import canvas
		except ImportError:
			self.skipTest("pypdf / reportlab not installed here")
		import io

		buffer = io.BytesIO()
		page = canvas.Canvas(buffer)
		for y, text in ((800, "Invoice No: INV-9002"), (780, f"PO #: {self.po}"),
		                (760, "100200 CAPTAN 80 WDG 40 LB LOT: C2609A EPA: 66330-38")):
			page.drawString(40, y, text)
		page.save()
		documents = receiving.adapter(receiving.SEED_CONNECTORS["wilbur_ellis_email_pdf"]).to_lines(buffer.getvalue())
		self.assertEqual(documents[0]["document_no"], "INV-9002")
		self.assertEqual([(line["sku"], line["qty"], line["lot_no"]) for line in documents[0]["lines"]], [("100200", "40", "C2609A")])
		with self.assertRaisesRegex(receiving.ReceivingError, "could not be read"):
			receiving.pdf_text(b"%PDF-1.4 not really")
