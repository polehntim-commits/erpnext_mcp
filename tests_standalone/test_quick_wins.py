# SPDX-License-Identifier: MIT
"""Quick wins from Tell the Farm (v0.211.0). docs/design/quick_wins_2026_10.md."""

import pathlib
import unittest

from erpnext_mcp import extraction_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "receipts"
HOME_DEPOT = (FIXTURES / "EXR-2026-0018.txt").read_text()


class ReceiptTwo(unittest.TestCase):
	"""§4. The shipped Receipt body: valid, and it reads the slip that broke Receipt@1."""

	def setUp(self):
		self.body, self.version = extraction_config.builtin("Receipt")

	def test_the_builtin_is_revision_two_and_valid(self):
		self.assertEqual(self.version, "Receipt@builtin-2")
		self.assertEqual(extraction_config.problems(self.body), [])
		self.assertEqual(extraction_config.builtin("Pesticide Label")[1], "Pesticide Label@builtin-1")

	def test_the_home_depot_slip_reads_its_total_not_the_years_spend(self):
		extracted = extraction_config.extract(HOME_DEPOT, self.body)
		self.assertEqual(extracted["amount"], "626.94")
		self.assertEqual(extracted["receipt_date"], "09/30/26")

	def test_each_amount_extractor_alone(self):
		"""The figure above its label, the label then the figure, and the card charge."""
		patterns = [row for row in self.body["extractors"] if row["field"] == "amount"]
		self.assertEqual(len(patterns), 3)
		cases = (
			("$12.50\nTOTAL\nVISA", "12.50"),
			("SUBTOTAL 10.00\nTAX 0.50\nTOTAL $10.50\nCASH", "10.50"),
			("TOTAL\n$1,010.50\nCASH", "1,010.50"),
			("GRAND TOTAL: 44.00", "44.00"),
			("thanks\nUSD$ 9.99\nAUTH", "9.99"),
		)
		for text, wanted in cases:
			self.assertEqual(extraction_config.extract(text, self.body).get("amount"), wanted, text)
		self.assertNotIn(
			"amount", extraction_config.extract("2026 PRO XTRA SPEND 09/29:\n$1,937.00", self.body)
		)
		self.assertNotIn("amount", extraction_config.extract("SUBTOTAL\n626.94\nSALES TAX", self.body))

	def test_the_receipt_block_is_checked(self):
		block = self.body["receipt"]
		self.assertEqual(block["merchant_domains"]["homedepot.com"], "The Home Depot")
		for phrase in ("SPEND", "SUMMARY", "YTD", "POINTS", "SAVINGS", "CREDIT LINE"):
			self.assertIn(phrase, block["never_amount"])
		for phrase in ("SELF CHECKOUT", "USD$"):
			self.assertIn(phrase, block["not_items"])

		def problems(**change):
			return extraction_config.problems({**self.body, "receipt": {**block, **change}})

		self.assertTrue(any("unknown keys" in p for p in problems(biggest_wins=True)))
		self.assertTrue(any("list of non-empty strings" in p for p in problems(never_amount="SPEND")))
		self.assertTrue(any("one capture group" in p for p in problems(charge_patterns=["USD"])))
		self.assertTrue(any("merchant_domains" in p for p in problems(merchant_domains={"Home Depot": "x"})))
		self.assertTrue(
			any(
				"look-behind" in p.lower() or "lookbehind" in p.lower()
				for p in problems(charge_patterns=["(?<=a)(b)"])
			)
		)
		self.assertEqual(
			extraction_config.problems({k: v for k, v in self.body.items() if k != "receipt"}), []
		)
