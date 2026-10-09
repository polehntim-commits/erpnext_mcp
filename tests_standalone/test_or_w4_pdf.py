# SPDX-License-Identifier: MIT
"""The Oregon DOR's own Form OR-W-4, filled. v0.295.0.

The template ships unmodified (checksums asserted); every field name in `or_w4_pdf` is checked against the
widget's position on the DOR's page; the plan is pure and tested without pypdf; and a full render fills the real
form, ticks the right line 1 radio and draws the employer block.
"""

import hashlib
import io
import os
import unittest

from erpnext_mcp import or_w4_pdf

TEMPLATES = os.path.join(os.path.dirname(or_w4_pdf.TEMPLATE_PATH))
needs_pypdf = unittest.skipUnless(or_w4_pdf.HAVE_PYPDF, "pypdf is not installed on this bench")
RECORD = {"name": "W4-1", "tax_year": 2026, "or_filing_status": "Married, withhold at higher Single rate",
          "or_allowances": 3, "or_additional_withholding": 25.4, "or_redetermination": 1,
          "or_signed_at": "2026-10-09 10:00:00"}
PERSON = {"first_name": "Ana", "middle_name": "Maria", "last_name": "Reyes", "address": "12 Orchard Rd",
          "city": "Hood River", "state": "OR", "zip": "97031"}
EMPLOYER = {"name": "Constancy Farms LLC", "ein": "12-3456789", "address": "4400 Fruit Loop Dr, Hood River, OR 97031"}


def sha(name):
	with open(os.path.join(TEMPLATES, name), "rb") as handle:
		return hashlib.sha256(handle.read()).hexdigest()


class TheTemplatesAreTheDORs(unittest.TestCase):
	def test_checksums(self):
		self.assertEqual(sha("or_w4_form.pdf"), or_w4_pdf.TEMPLATE_SHA256)
		self.assertEqual(sha("or_w4_instructions.pdf"),
		                 "c80963c5358ab74b3dfbc2d4158dd081277300f3151e74994ff2bf13fc44d819")
		self.assertEqual(sha("or_w4_instructions_es.pdf"),
		                 "329e9e6f4846f879c776317054a1050954cb84d7a16dc55594303836b2f269a2")
		self.assertEqual(sha("or_w4_employer_information.pdf"),
		                 "e044bc37d5bacb13fed0540fc36d11cca43474f809947c1d6eabb9ee14826d73")

	@needs_pypdf
	def test_every_field_name_is_where_the_table_says(self):
		from pypdf import PdfReader

		where = {}
		for annot in PdfReader(or_w4_pdf.TEMPLATE_PATH).pages[0]["/Annots"]:
			annot = annot.get_object()
			name = annot.get("/T") or annot["/Parent"].get_object().get("/T")
			where.setdefault(str(name), []).append([float(v) for v in annot["/Rect"]])
		expect = {  # field: (x near, y near)
			or_w4_pdf.FIELD_FIRST_NAME: (36, 624), or_w4_pdf.FIELD_LAST_NAME: (151, 624),
			or_w4_pdf.FIELD_REDETERMINATION: (424, 636), or_w4_pdf.FIELD_ADDRESS: (36, 600),
			or_w4_pdf.FIELD_ZIP: (497, 600), or_w4_pdf.FIELD_ALLOWANCES: (476, 493),
			or_w4_pdf.FIELD_ADDITIONAL: (474, 469), or_w4_pdf.FIELD_EXEMPTION_CODE: (476, 421),
			or_w4_pdf.FIELD_EXEMPT_WORD: (469, 408), or_w4_pdf.FIELD_DATE_YEAR: (467, 348),
		}
		for field, (x, y) in expect.items():
			rect = where[field][0]
			self.assertLess(abs(rect[0] - x) + abs(rect[1] - y), 3, field)
		self.assertEqual(len(where[or_w4_pdf.FIELD_STATUS]), 3, "line 1 is one radio of three")


class ThePlan(unittest.TestCase):
	def test_elections(self):
		got = or_w4_pdf.plan(RECORD, PERSON)
		values = got["values"]
		self.assertEqual(values[or_w4_pdf.FIELD_STATUS], "/3")
		self.assertEqual(values[or_w4_pdf.FIELD_ALLOWANCES], "3")
		self.assertEqual(values[or_w4_pdf.FIELD_ADDITIONAL], "25")
		self.assertIn("whole dollars", got["notes"][0])
		self.assertEqual(values[or_w4_pdf.FIELD_INITIAL], "M")
		self.assertEqual((values[or_w4_pdf.FIELD_DATE_MONTH], values[or_w4_pdf.FIELD_DATE_DAY]), ("10", "09"))
		for ssn in or_w4_pdf.FIELDS_SSN:
			self.assertNotIn(ssn, values, "the SSN is the employee's to write")

	def test_exempt_writes_lines_4a_and_4b_and_no_allowances(self):
		values = or_w4_pdf.plan({"or_exempt": 1, "or_exemption_code": "L"}, PERSON)["values"]
		self.assertEqual((values[or_w4_pdf.FIELD_EXEMPTION_CODE], values[or_w4_pdf.FIELD_EXEMPT_WORD]), ("L", "Exempt"))
		self.assertNotIn(or_w4_pdf.FIELD_ALLOWANCES, values)

	def test_address_split(self):
		self.assertEqual(or_w4_pdf.split_address("4400 Fruit Loop Dr, Hood River, OR 97031"),
		                 {"address": "4400 Fruit Loop Dr", "city": "Hood River", "state": "OR", "zip": "97031"})
		self.assertEqual(or_w4_pdf.split_address("PO Box 7")["address"], "PO Box 7")


@needs_pypdf
class TheRender(unittest.TestCase):
	def test_the_real_form_is_filled_and_flattened(self):
		from pypdf import PdfReader

		pdf, _notes = or_w4_pdf.fill_or_w4_pdf(RECORD, PERSON, EMPLOYER)
		reader = PdfReader(io.BytesIO(pdf))
		self.assertEqual(len(reader.pages), 1)
		text = reader.pages[0].extract_text()
		self.assertIn("Oregon Withholding Statement and Exemption Certificate", text)
		self.assertIn("Reyes", text)
		if _has_reportlab():
			self.assertIn("12-3456789", text, "the employer block is drawn")
		with open(or_w4_pdf.TEMPLATE_PATH, "rb") as handle:
			self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), or_w4_pdf.TEMPLATE_SHA256, "never edited")


def _has_reportlab():
	try:
		import reportlab  # noqa: F401
	except ImportError:
		return False
	return True
