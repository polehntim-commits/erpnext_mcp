# SPDX-License-Identifier: MIT
"""`gs1` — the check digit, and the refusal to invent a company prefix.

Cycle 2 of the Farm App retirement. Three claims.

1. **THE CHECK DIGIT IS RIGHT, PROVED AGAINST REAL BARCODES.** `TheCheckDigit`.
   The classic implementation bug is weighting from the left instead of the
   right, and it is INVISIBLE on symmetric test data — so the assertions below
   use two published barcodes with known-good check digits and one asymmetric
   body worked out by hand.

2. **A PREFIX IS NEVER INVENTED.** `NoPrefixNoKey`. The farm_app fell back to a
   literal `"DEV123"`, which is unscannable at best and another company's
   address space at worst, and printed it onto labels either way. Every
   generator here goes through `prefix_or_raise` and every one of them refuses.

3. **A CUSTOM KEY NEVER CLAIMS TO BE A GS1 KEY.** `CustomModeIsHonest`. The
   number is sound, the length is right, the check digit verifies — and
   `gs1_compliant` is False, because the one thing a farm's own number must
   never do is get quoted to a buyer as a GS1 key.
"""

import unittest

from erpnext_mcp import gs1

#: A real GS1 company prefix length. Not a real licence — the digits are the
#: ones GS1's own documentation uses in worked examples.
PREFIX = "0614141"


class TheCheckDigit(unittest.TestCase):
	def test_two_published_barcodes_verify(self):
		"""EAN-13 and UPC-A, both from published examples. If the weighting were
		reversed these would both fail."""
		self.assertTrue(gs1.validate_gtin("5901234123457"))
		self.assertTrue(gs1.validate_gtin("036000291452"))

	def test_reversing_the_weighting_would_give_a_different_answer(self):
		"""The negative control for the classic bug. `12345678` weighted from
		the right gives one digit and weighted from the left gives another, so
		this body — unlike a symmetric one — can actually tell the two apart.

		Worked from the right, alternating 3 and 1 starting at the last
		character: 8·3+7·1+6·3+5·1+4·3+3·1+2·3+1·1 = 76, and
		(10 − 76 mod 10) mod 10 = 4.
		"""
		self.assertEqual(gs1.check_digit("12345678"), 4)
		reversed_body = gs1.check_digit("87654321")
		self.assertNotEqual(reversed_body, gs1.check_digit("12345678"))

	def test_a_single_transposed_digit_fails(self):
		"""The whole point of the check digit: a mis-keyed pair does not verify."""
		self.assertTrue(gs1.validate_gtin("5901234123457"))
		self.assertFalse(gs1.validate_gtin("5901234132457"))

	def test_a_body_that_is_not_digits_is_refused_rather_than_scored(self):
		with self.assertRaises(gs1.GS1Error):
			gs1.check_digit("59012341234X")

	def test_the_wrong_length_is_not_well_formed_however_good_the_digit(self):
		self.assertFalse(gs1.is_well_formed("5901234123"))
		self.assertFalse(gs1.is_well_formed(""))
		self.assertFalse(gs1.is_well_formed(None))

	def test_each_key_type_checks_its_own_length(self):
		gtin = gs1.generate_gtin("42", PREFIX)
		self.assertTrue(gs1.validate_gtin(gtin))
		self.assertFalse(gs1.validate_sscc(gtin))
		self.assertTrue(gs1.validate_gln(gs1.generate_gln("7", PREFIX)))
		self.assertTrue(gs1.validate_sscc(gs1.generate_sscc("12345", PREFIX)))


class NoPrefixNoKey(unittest.TestCase):
	def test_every_generator_refuses_without_a_prefix(self):
		for build in (
			lambda: gs1.generate_gtin("42", ""),
			lambda: gs1.generate_gln("0001", ""),
			lambda: gs1.generate_sscc("12345", ""),
		):
			with self.assertRaises(gs1.GS1Error) as caught:
				build()
			self.assertIn("will not invent one", str(caught.exception))

	def test_the_farm_apps_own_fallback_is_refused_by_name(self):
		"""`DEV123` is what the farm_app substituted. It is not digits, so it
		could not be encoded as a barcode at all — and it was printed onto
		labels regardless."""
		with self.assertRaises(gs1.GS1Error) as caught:
			gs1.generate_gtin("42", "DEV123")
		self.assertIn("digits only", str(caught.exception))

	def test_a_prefix_off_a_certificate_is_read_through_its_punctuation(self):
		self.assertEqual(gs1.normalise_prefix("0-6141-41"), PREFIX)
		self.assertEqual(gs1.normalise_prefix(" 061 4141 "), PREFIX)

	def test_a_prefix_outside_the_licensed_length_is_refused(self):
		for bad in ("061", "061414100000"):
			with self.assertRaises(gs1.GS1Error) as caught:
				gs1.generate_gtin("42", bad)
			self.assertIn("4 to 11 digits", str(caught.exception))

	def test_a_reference_too_long_for_the_key_names_both_figures(self):
		with self.assertRaises(gs1.GS1Error) as caught:
			gs1.generate_gtin("1234567", PREFIX)
		message = str(caught.exception)
		self.assertIn("7 digits", message)
		self.assertIn("room for 5", message)

	def test_a_reference_is_zero_padded_so_seven_and_double_oh_seven_are_one_item(self):
		self.assertEqual(gs1.generate_gtin("7", PREFIX), gs1.generate_gtin("007", PREFIX))

	def test_a_gtin_14_with_indicator_zero_is_the_gtin_13_zero_padded(self):
		"""Not a coincidence and worth pinning: the check digit is weighted from
		the RIGHT, so a leading zero contributes nothing and shifts no position.
		GTIN-14 with indicator 0 is therefore exactly `"0" + GTIN-13`, which is
		what every receiving system assumes when it pads a 13 to a 14. A future
		change that broke this would break scanning against buyers who pad."""
		thirteen = gs1.generate_gtin("42", PREFIX, 13)
		fourteen = gs1.generate_gtin("42", PREFIX, 14)
		self.assertEqual(len(fourteen), 14)
		self.assertTrue(gs1.validate_gtin(fourteen))
		self.assertEqual(fourteen, "0" + thirteen)

	def test_a_length_gs1_does_not_define_is_refused(self):
		with self.assertRaises(gs1.GS1Error):
			gs1.generate_gtin("42", PREFIX, 11)

	def test_configured_prefix_answers_empty_on_a_site_with_no_such_field(self):
		"""No release has added the settings field yet, so this must answer `""`
		rather than raising — the refusal belongs to `prefix_or_raise`, which is
		where the explanation is."""
		self.assertEqual(gs1.configured_prefix(), "")
		self.assertEqual(gs1.configured_prefix("custom"), "")


class CustomModeIsHonest(unittest.TestCase):
	def test_a_custom_key_is_well_formed_and_not_gs1_compliant(self):
		key = gs1.generate_gtin("42", "9990001", standard="custom")
		self.assertTrue(gs1.is_well_formed(key))
		self.assertFalse(gs1.describe(key, "9990001", "custom")["gs1_compliant"])

	def test_custom_mode_still_needs_a_prefix_of_its_own(self):
		with self.assertRaises(gs1.GS1Error) as caught:
			gs1.generate_gtin("42", "", standard="custom")
		self.assertIn("custom mode still needs a prefix", str(caught.exception))

	def test_custom_mode_skips_the_gs1_length_rule_but_not_the_digits_rule(self):
		"""A farm's own prefix need not be a licensed length — but it still has
		to encode as a barcode."""
		self.assertTrue(gs1.is_well_formed(gs1.generate_gtin("4242", "999", standard="custom")))
		with self.assertRaises(gs1.GS1Error):
			gs1.generate_gtin("42", "FARM", standard="custom")

	def test_an_unknown_standard_is_refused_rather_than_defaulted(self):
		with self.assertRaises(gs1.GS1Error):
			gs1.prefix_or_raise(PREFIX, "iso")

	def test_describe_tells_our_case_from_a_suppliers(self):
		"""A well-formed key that is not ours is a supplier's box that arrived on
		the farm — a normal thing to scan, and one a receiving flow has to tell
		apart from a case the farm packed."""
		ours = gs1.generate_gtin("42", PREFIX)
		theirs = "5901234123457"
		self.assertTrue(gs1.describe(ours, PREFIX)["ours"])
		self.assertFalse(gs1.describe(theirs, PREFIX)["ours"])
		self.assertTrue(gs1.describe(theirs, PREFIX)["well_formed"])

	def test_describe_names_the_key_type_by_length(self):
		self.assertEqual(gs1.describe(gs1.generate_sscc("1", PREFIX))["kind"], "SSCC")
		self.assertEqual(gs1.describe("12345")["kind"], "unknown")

	def test_describe_survives_a_prefix_it_cannot_use(self):
		"""A bad prefix must not make `describe` raise — it is the function a
		scanning flow calls on whatever came off the reader."""
		self.assertFalse(gs1.describe("5901234123457", "NOT-DIGITS")["ours"])


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
