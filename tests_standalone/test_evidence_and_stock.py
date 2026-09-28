# SPDX-License-Identifier: MIT
"""v0.200.0 — a finished task's evidence on the phone, and a stock entry the books can take.

AFB-2026-00024 ("I cannot review photo es etc after inspection", on 40-WM-SE) and
AFB-2026-00025 (a stock receipt refused with ERPNext's HTML about a Stock
Adjustment Account). Contract: `docs/design/task_evidence_and_stock_accounts.md`.
"""

import base64
import unittest

import frappe

from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.farmops_api import app as farmops_app
from erpnext_mcp.tools import dimensions

from .fixtures import MAIN, OTHER, SHOP, STORES
from .harness import STORE
from .test_api_mobile import MobileAPITestCase
from .test_stock_inventory import WriteEnabledTestCase, _receipt, _transfer

TASK = "FT-2026-09-00004"


def _file(name, file_name):
	return {
		"name": name,
		"file_name": file_name,
		"file_url": f"/private/files/{file_name}",
		"is_private": 1,
		"attached_to_doctype": None,
		"attached_to_name": None,
	}


# ── 1. a finished task's evidence ───────────────────────────────────────────
class AFinishedTaskShowsItsEvidence(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		STORE.seed(
			"File",
			[
				_file("F-BEFORE", f"{TASK}_photo_before_307F.jpg"),
				_file("F-AFTER", f"{TASK}_photo_after_28D1.jpg"),
				_file("F-SIG", f"{TASK}_signature_BBA7.png"),
				_file("F-REPORT", "scan-report-1.jpg"),
				_file("F-ELSEWHERE", "someone-elses.jpg"),
			],
		)
		for name in ("F-BEFORE", "F-AFTER", "F-SIG", "F-REPORT", "F-ELSEWHERE"):
			STORE.file_contents[name] = b"\xff\xd8" + name.encode()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": TASK,
					"task_name": "Routine walk",
					"task_type": "Inspection",
					"company": MAIN,
					"state": "Completed",
					"asset": "40-WM-SE",
					"report_photo": "F-REPORT",
					"evidence_required": '{"photos": true, "findings_text": true}',
				}
			],
		)
		STORE.seed(
			"Farm Task Assignment",
			[
				{
					"name": "FTA-2026-09-00004",
					"task": TASK,
					"company": MAIN,
					"state": "Completed",
					"assigned_to": "HR-EMP-00001",
					"assigned_to_name": "Tim Polehn",
					"completed_at": "2026-09-27 16:58:46",
					"findings_text": "Looks operable",
					"completion_narrative": "Before (1) · After (1)",
					"evidence_files": [
						# An old build's row: no phase, the name says it.
						{
							"file": "F-BEFORE",
							"evidence_type": "Photo",
							"caption": f"{TASK}_photo_before_307F.jpg",
						},
						{"file": "F-AFTER", "evidence_type": "Photo", "phase": "after"},
						{"file": "F-SIG", "evidence_type": "Signature"},
					],
				}
			],
		)

	def test_the_list_carries_photos_signature_findings_and_notes(self):
		self.be()
		answer = mobile_api.list_task_evidence(task=TASK)
		self.assertEqual(answer["asset"], "40-WM-SE")
		self.assertEqual(answer["report_photo"]["file"], "F-REPORT")
		(only,) = answer["assignments"]
		self.assertEqual(only["findings_text"], "Looks operable")
		self.assertEqual(only["completion_narrative"], "Before (1) · After (1)")
		self.assertEqual(
			[(e["file"], e["phase"]) for e in only["evidence"]],
			[("F-BEFORE", "before"), ("F-AFTER", "after")],
		)
		self.assertTrue(all(e["is_image"] for e in only["evidence"]))
		self.assertEqual(only["signature"]["file"], "F-SIG")
		self.assertEqual(answer["evidence_count"], 4)

	def test_a_photo_and_the_signature_open(self):
		self.be()
		for name in ("F-AFTER", "F-SIG", "F-REPORT"):
			answer = mobile_api.get_task_evidence(task=TASK, file=name)
			self.assertEqual(base64.b64decode(answer["content"]), b"\xff\xd8" + name.encode())

	def test_a_file_that_is_not_this_tasks_is_refused(self):
		"""A File docname is a global handle; bringing one does not open it."""
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_task_evidence(task=TASK, file="F-ELSEWHERE")

	def test_another_entitys_task_is_not_reachable(self):
		STORE.seed(
			"Farm Task",
			[{"name": "FT-OTHER", "company": OTHER, "state": "Completed", "task_type": "Inspection"}],
		)
		self.be()
		with self.assertRaises(Exception):
			mobile_api.list_task_evidence(task="FT-OTHER")


# ── 2. the books are asked before ERPNext is ────────────────────────────────
class TheBooksAreAskedFirst(WriteEnabledTestCase):
	def setUp(self):
		super().setUp()
		STORE.get_raw("Company", MAIN).update(
			{
				"enable_perpetual_inventory": 1,
				"stock_adjustment_account": None,
				"default_inventory_account": None,
			}
		)

	def test_a_receipt_on_orchard_meadows_books_is_refused_in_a_sentence(self):
		before = len(STORE.rows("Stock Entry"))
		message = self.tool_error("create_stock_entry", _receipt())
		self.assertIn("no Stock Adjustment Account", message)
		self.assertIn("set_company_defaults", message)
		self.assertIn("no inventory account", message)
		self.assertIn("The warehouse chosen is not the problem", message)
		self.assertNotIn("<", message)
		self.assertEqual(len(STORE.rows("Stock Entry")), before, "nothing was filed")

	def test_the_two_company_defaults_let_it_through(self):
		STORE.get_raw("Company", MAIN).update(
			{
				"stock_adjustment_account": "Stock Adjustment - M",
				"default_inventory_account": "Stock In Hand - M",
			}
		)
		self.assertEqual(self.tool_data("create_stock_entry", _receipt())["status"], "Draft")

	def test_a_warehouse_with_its_own_account_needs_no_company_default(self):
		STORE.get_raw("Company", MAIN)["stock_adjustment_account"] = "Stock Adjustment - M"
		STORE.get_raw("Warehouse", STORES)["account"] = "Stock In Hand - M"
		self.assertEqual(self.tool_data("create_stock_entry", _receipt())["status"], "Draft")

	def test_a_transfer_posts_no_difference(self):
		STORE.get_raw("Warehouse", STORES)["account"] = "Stock In Hand - M"
		STORE.get_raw("Warehouse", SHOP)["account"] = "Stock In Hand - M"
		self.assertEqual(self.tool_data("create_stock_entry", _transfer())["status"], "Draft")

	def test_without_perpetual_inventory_nothing_is_asked(self):
		STORE.get_raw("Company", MAIN)["enable_perpetual_inventory"] = 0
		self.assertEqual(self.tool_data("create_stock_entry", _receipt())["status"], "Draft")

	def test_the_inventory_default_can_now_be_set(self):
		self.assertIn("default_inventory_account", dimensions.SUPPORTED_COMPANY_DEFAULTS)


# ── 3. a refusal reaches the phone as text ─────────────────────────────────
class ARefusalIsText(unittest.TestCase):
	def test_html_is_stripped_from_what_the_phone_reads(self):
		message = farmops_app._message_for(
			frappe.ValidationError(
				"Please enter <b>Difference Account</b> or set default <b>Stock Adjustment Account</b> "
				"for company <strong>Orchard Meadow, LLC</strong>&nbsp;&amp; more"
			),
			400,
		)
		self.assertEqual(
			message,
			"Please enter Difference Account or set default Stock Adjustment Account for company "
			"Orchard Meadow, LLC & more",
		)
