# SPDX-License-Identifier: MIT
"""A housing walk and a detector test from the doorway. v0.228.0 — offline_mill_creek.md §2."""

import base64
import hashlib

import frappe

from erpnext_mcp.api import files as files_api
from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, OTHER
from .harness import STORE, set_roles
from .test_api_mobile import ON as MOBILE_ON
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase

ON = {
	**MOBILE_ON,
	"allow_create_parcel": 1,
	"allow_create_housing_unit": 1,
	"allow_create_housing_inspection": 1,
	"allow_create_detector_test": 1,
}


class FieldRecordCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.parcel = self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 131.4,
				"county": "Wasco",
				"state": "OR",
			},
		)["name"]
		self.unit = self.tool_data(
			"create_housing_unit",
			{
				"owning_entity": MAIN,
				"parcel": self.parcel,
				"unit_name": "Cabin 1",
				"unit_type": "Cabin",
				"capacity": 4,
			},
		)["name"]
		set_roles(WORKER, [*guard.roles_held(WORKER), "Foreman"])
		STORE.commit()
		self.be()

	def photo(self, text="cabin"):
		payload = text.encode()
		upload = f"walk-{text}"
		files_api.stage_file_chunk(
			upload_id=upload,
			file_name=f"{text}.jpg",
			chunk_index=0,
			chunk_count=1,
			total_bytes=len(payload),
			data=base64.b64encode(payload).decode(),
		)
		return files_api.finalize_staged_file(
			upload_id=upload,
			file_name=f"{text}.jpg",
			sha256=hashlib.sha256(payload).hexdigest(),
			total_bytes=len(payload),
		)["file_token"]


class TheWalk(FieldRecordCase):
	def test_filed_with_photos_dated_by_the_tap_and_a_resend_is_the_same_walk(self):
		token = self.photo()
		tapped = str(frappe.utils.now())[:10] + " 07:15:00"
		first = mobile_api.create_housing_inspection(
			unit=self.unit,
			findings="Window latch broken",
			photos=[token],
			client_request_id="walk-req-1",
			tapped_at=tapped,
		)
		self.assertFalse(first["replayed"])
		row = frappe.get_doc("Housing Inspection", first["name"]).as_dict()
		self.assertEqual(str(row["inspection_date"])[:10], tapped[:10])
		self.assertEqual(row["inspector"], WORKER_EMPLOYEE)
		self.assertEqual(row["client_request_id"], "walk-req-1")
		self.assertEqual(str(row["device_recorded_at"])[:19], tapped)
		self.assertTrue(row["received_at"])
		self.assertEqual(first["evidence_count"], 1)

		again = mobile_api.create_housing_inspection(
			unit=self.unit, findings="Window latch broken", photos=[token], client_request_id="walk-req-1"
		)
		self.assertTrue(again["replayed"])
		self.assertEqual(again["name"], first["name"])
		self.assertEqual(len(frappe.db.get_all("Housing Inspection")), 1)

	def test_a_picker_cannot_file_one(self):
		set_roles(WORKER, ["Field Worker"])
		with self.assertRaises(Exception):
			mobile_api.create_housing_inspection(unit=self.unit, findings="x")

	def test_another_entitys_cabin_reads_as_not_found(self):
		self.be("Administrator")
		other = self.tool_data(
			"create_parcel",
			{
				"owning_entity": OTHER,
				"parcel_name": "Elsewhere",
				"acreage": 10,
				"county": "Wasco",
				"state": "OR",
			},
		)["name"]
		unit = self.tool_data(
			"create_housing_unit",
			{"owning_entity": OTHER, "parcel": other, "unit_name": "Cabin 9", "capacity": 2},
		)["name"]
		STORE.commit()
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.create_housing_inspection(unit=unit, findings="x")


class TheDetectors(FieldRecordCase):
	def test_a_test_is_filed_and_a_resend_replays(self):
		answer = mobile_api.create_detector_test(
			unit=self.unit,
			smoke_detector_result="Pass",
			co_detector_result="Pass",
			client_request_id="det-req-1",
			tapped_at=str(frappe.utils.now())[:19],
		)
		self.assertFalse(answer["replayed"])
		again = mobile_api.create_detector_test(unit=self.unit, client_request_id="det-req-1")
		self.assertEqual((again["replayed"], again["name"]), (True, answer["name"]))
		self.assertEqual(
			frappe.db.get_value("Detector Test", answer["name"], "client_request_id"), "det-req-1"
		)


class TheScanPack(FieldRecordCase):
	def test_every_tag_and_its_live_tasks_for_a_scan_with_no_signal(self):
		self.be("Administrator")
		self.configure(enabled=1, **{**ON, "allow_register_asset": 1})
		from erpnext_mcp.tools import asset_tags

		asset_tags.register_asset(
			{
				"name": "MC-Pump-01",
				"asset_type": "General",
				"company": MAIN,
				"tag_uuid": "6f1c2a9e-4b7d-4e21-9c3a-0d5e8f7a1b22",
				"request_id": "req-pack-1",
			}
		)
		task = self.a_task(location_doctype="Housing Unit", location=self.unit)
		STORE.commit()
		self.be()
		pack = mobile_api.get_offline_scan_pack()
		by_name = {e["name"]: e for e in pack["entities"]}
		self.assertIn("6f1c2a9e-4b7d-4e21-9c3a-0d5e8f7a1b22", by_name["MC-Pump-01"]["tags"])
		self.assertEqual([t["name"] for t in by_name[self.unit]["pending_tasks"]], [task])
		self.assertIn("4 beds", by_name[self.unit]["subtitle"])
		self.assertTrue(pack["generated_at"])
		# v0.231.1: the type rides along so an offline valve scan opens the valve screen.
		self.assertEqual(by_name["MC-Pump-01"]["asset_type"], "General")
		self.assertEqual(by_name[self.unit]["asset_type"], "")
