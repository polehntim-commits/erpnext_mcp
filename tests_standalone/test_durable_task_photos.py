# SPDX-License-Identifier: MIT
"""A completion resent after a lost reply is the same completion. v0.230.0 — offline_mill_creek.md §1b."""

import frappe

from erpnext_mcp.api import mobile as mobile_api

from . import test_api_mobile as base
from .harness import STORE


class AResentCompletion(base.MobileAPITestCase):
	upload = base.TheWholeFlowWorks.upload

	def setUp(self):
		super().setUp()
		self.unit = self.a_camp()
		self.task = self.a_task(
			creates_record="Housing Inspection", location_doctype="Housing Unit", location=self.unit
		)

	def test_is_answered_with_the_first_and_files_its_photos_once(self):
		self.be()
		claimed = mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)
		photo = self.upload("photo", "FT_photo.jpg")
		signature = self.upload("signature", "FT_signature.png")
		args = {
			"task": self.task,
			"task_assignment": claimed["assignment"],
			"clean_pass": True,
			"completion_narrative": "walked it",
			"latitude": 45.6721,
			"longitude": -121.1787,
			"evidence_files": [
				{"file_token": photo["file_token"], "file_name": "FT_photo.jpg", "kind": "photo"},
				{"file_token": signature["file_token"], "file_name": "FT_signature.png", "kind": "signature"},
			],
			"client_request_id": "done-1",
		}
		first = mobile_api.complete_task_via_mobile(**args)
		STORE.commit()
		again = mobile_api.complete_task_via_mobile(**args)
		self.assertTrue(again["replayed"])
		self.assertEqual(again.get("created_record_name"), first.get("created_record_name"))
		self.assertEqual(again.get("evidence_filed"), first.get("evidence_filed"))
		self.assertEqual(frappe.db.get_value("Farm Task", self.task, "state"), "Completed")
		self.assertEqual(len(STORE.rows("Housing Inspection")), 1)
