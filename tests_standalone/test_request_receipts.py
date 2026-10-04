# SPDX-License-Identifier: MIT
"""Same request, same result on the routes a phone queues. v0.229.0 — offline_mill_creek.md §5–6."""

import frappe

from erpnext_mcp import request_receipts
from erpnext_mcp.api import mobile as mobile_api

from .harness import STORE
from .test_api_mobile import WORKER
from .test_wave2_mobile_surface import Wave2TestCase
from .test_work_actions import CAL, WorkActionsCase

SIGNATURE = "/files/foreman-signature.png"


class AReportSentTwice(Wave2TestCase):
	def test_is_one_task_and_the_second_answer_says_replayed(self):
		STORE.seed("File", [{"name": "FILE-PHOTO-9", "file_name": "riser.jpg", "is_private": 1}])
		self.be(WORKER)
		first = mobile_api.report_field_task(
			description="Riser split.", photo_file_token="FILE-PHOTO-9", client_request_id="rep-1"
		)
		before = len(STORE.rows("Farm Task"))
		again = mobile_api.report_field_task(
			description="Riser split.", photo_file_token="FILE-PHOTO-9", client_request_id="rep-1"
		)
		self.assertTrue(again["replayed"])
		self.assertEqual(again["name"], first["name"])
		self.assertEqual(len(STORE.rows("Farm Task")), before)

	def test_without_a_key_nothing_changes(self):
		STORE.seed("File", [{"name": "FILE-PHOTO-8", "file_name": "riser.jpg", "is_private": 1}])
		self.be(WORKER)
		one = mobile_api.report_field_task(description="A.", photo_file_token="FILE-PHOTO-8")
		self.assertNotIn("replayed", one)


class AShiftClosedTwice(WorkActionsCase):
	def test_the_second_close_is_the_first_close(self):
		STORE.seed(
			"File", [{"name": "FILE-SIG", "file_url": SIGNATURE, "file_name": "sig.png", "is_private": 1}]
		)
		self.foreman()
		shift = mobile_api.clock_in_crew(employees=[CAL], location="Yard")["shift"]
		STORE.commit()
		first = mobile_api.end_shift(
			shift=shift, supervisor_signature_file_token=SIGNATURE, client_request_id="close-1"
		)
		STORE.commit()
		again = mobile_api.end_shift(
			shift=shift, supervisor_signature_file_token=SIGNATURE, client_request_id="close-1"
		)
		self.assertTrue(again["replayed"])
		self.assertEqual(again.get("name"), first.get("name"))

	def test_a_receipt_is_per_user(self):
		self.assertNotEqual(
			request_receipts._key("end_shift", "a@x", "k"), request_receipts._key("end_shift", "b@x", "k")
		)
		self.assertIsNone(request_receipts.earlier("end_shift", WORKER, "never-sent"))
		self.assertIsNone(request_receipts.earlier("end_shift", WORKER, ""))
		self.assertTrue(frappe.db)  # the harness is up


class BadgesForTheCrewLead(WorkActionsCase):
	def test_a_foreman_gets_the_badges_and_a_picker_does_not(self):
		self.foreman()
		pack = mobile_api.get_offline_scan_pack()
		self.assertIn("ETC-0100", [b["badge_id"] for b in pack["badges"]])
		from .harness import set_roles

		set_roles(WORKER, ["Field Worker"])
		self.assertEqual(mobile_api.get_offline_scan_pack()["badges"], [])
