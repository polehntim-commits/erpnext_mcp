# SPDX-License-Identifier: MIT
"""v0.229.1. A placeholder phone never resolves a merchant, and receipts resolved on one are repaired.

EXR-2026-0015 (Coastal Farm Stores, coastalcountry.com) was resolved to Sawyer's Hardware by Phone at
0.83 because EXR-2026-0014 also carried 0000000000. Capture has dropped such numbers since v0.183.0;
the rows resolved before that keep the wrong answer until `repair_placeholder_phones` re-resolves them.
"""

import frappe

from erpnext_mcp.tools import receipts

from .harness import STORE
from .test_receipt_intelligence import ReceiptIntelligenceTestCase, TheNightlyRunCannotLinkTheWrongSupplier

R = receipts


class Coastal(ReceiptIntelligenceTestCase):
	"""The EXR-2026-0015 fixture, without re-running its parent's tests."""

	COASTAL = TheNightlyRunCannotLinkTheWrongSupplier.COASTAL
	SAWYERS = TheNightlyRunCannotLinkTheWrongSupplier.SAWYERS
	SLIP = TheNightlyRunCannotLinkTheWrongSupplier.SLIP

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Supplier",
			[
				{"name": self.COASTAL, "supplier_name": self.COASTAL},
				{"name": self.SAWYERS, "supplier_name": self.SAWYERS},
			],
		)


class APlaceholderNeverMatches(Coastal):
	def test_url_wins_over_a_placeholder_shared_with_another_receipt(self):
		first = self.capture(merchant="SAWYERS HDW", merchant_phone="0000000000")["name"]
		self.tool_data("update_expense_receipt", {"name": first, "supplier": self.SAWYERS})
		answer = R.resolve_merchant(
			"COASTAL FARM STORES", merchant_phone="0000000000", merchant_url="coastalcountry.com"
		)
		self.assertNotEqual(answer["method"], "Phone")
		self.assertEqual(answer["resolved_merchant"], self.COASTAL)
		self.assertEqual(answer["dropped_phone"], "0000000000")
		for empty in ("", "   ", "(000) 000-0000"):
			self.assertNotEqual(
				R.resolve_merchant("COASTAL FARM STORES", merchant_phone=empty)["method"], "Phone"
			)


class TheRepair(Coastal):
	def legacy(self):
		"""Two receipts as a pre-v0.183.0 server stored them."""
		fourteen = self.capture(merchant="SAWYERS HDW")["name"]
		fifteen = self.capture(merchant="COASTAL FARM STORES", ocr_raw_text=self.SLIP)["name"]
		frappe.db.set_value(
			"Expense Receipt",
			fourteen,
			{
				"merchant_phone": "0000000000",
				"resolved_merchant": self.SAWYERS,
				"resolution_method": "Manual",
			},
		)
		frappe.db.set_value(
			"Expense Receipt",
			fifteen,
			{
				"merchant_phone": "0000000000",
				"merchant_url": "coastalcountry.com",
				"resolved_merchant": self.SAWYERS,
				"resolution_method": "Phone",
				"resolution_confidence": 0.83,
				"supplier": self.SAWYERS,
			},
		)
		return fourteen, fifteen

	def test_a_dry_run_reports_and_writes_nothing(self):
		fourteen, fifteen = self.legacy()
		report = R.repair_placeholder_phones()
		self.assertFalse(report["applied"])
		self.assertEqual({r["receipt"] for r in report["receipts"]}, {fourteen, fifteen})
		row = next(r for r in report["receipts"] if r["receipt"] == fifteen)
		self.assertEqual(row["before"]["method"], "Phone")
		self.assertEqual(row["after"]["resolved_merchant"], self.COASTAL)
		self.assertNotEqual(row["after"]["method"], "Phone")
		self.assertIn("left as it is", row["supplier_note"])
		self.assertEqual(self.receipt_row(fifteen)["resolution_method"], "Phone", "nothing written")

	def test_apply_re_resolves_keeps_a_persons_answer_and_never_touches_the_link(self):
		fourteen, fifteen = self.legacy()
		report = R.repair_placeholder_phones(apply=True)
		self.assertTrue(report["applied"])
		fixed = self.receipt_row(fifteen)
		self.assertEqual(fixed["resolved_merchant"], self.COASTAL)
		self.assertNotEqual(fixed["resolution_method"], "Phone")
		self.assertFalse(fixed.get("merchant_phone"))
		self.assertEqual(frappe.db.get_value("Expense Receipt", fifteen, "supplier"), self.SAWYERS)
		kept = self.receipt_row(fourteen)
		self.assertEqual((kept["resolved_merchant"], kept["resolution_method"]), (self.SAWYERS, "Manual"))
		self.assertFalse(kept.get("merchant_phone"))
		self.assertEqual(R.repair_placeholder_phones()["count"], 0, "nothing left to repair")

	def test_the_tool_is_off_by_default_and_a_dry_run_when_on(self):
		self.legacy()
		self.assertIn("switched off", self.tool_error("repair_placeholder_phones", {}))
		self.configure(enabled=1, allow_repair_placeholder_phones=1)
		data = self.tool_data("repair_placeholder_phones", {})
		self.assertEqual((data["applied"], data["count"]), (False, 2))
