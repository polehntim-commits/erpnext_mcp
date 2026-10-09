# Static form templates

## `i9_form.pdf`

The official **USCIS Form I-9, Employment Eligibility Verification**, downloaded
unmodified from

    https://www.uscis.gov/sites/default/files/document/forms/i-9-paper-version.pdf

| | |
|---|---|
| Agency | U.S. Citizenship and Immigration Services, Department of Homeland Security |
| OMB control number | 1615-0047 |
| Edition | Rev. 08/01/23 |
| Expires | 05/31/2027 |
| Pages | 4 — Section 1 + Section 2, Lists of Acceptable Documents, Supplement A (Preparer/Translator), Supplement B (Reverification and Rehire) |
| SHA-256 | `780f348c34df694bb0b4dbbfaf9f22b99b9757b80d16a37ba89aadf069597281` |
| Bytes | 524,095 |
| AcroForm fields | 133, no XFA |

**IT IS SHIPPED BYTE-FOR-BYTE AND IS NEVER EDITED IN PLACE.** `erpnext_mcp/i9_pdf.py`
opens it read-only, writes the field values into a copy in memory, and hands the
copy back. The file on disk is the government's page and stays the government's
page — the checksum above is asserted by `tests_standalone/test_i9_pdf.py`, so a
template that was swapped, re-saved by a PDF editor, or corrupted in transit
fails the suite rather than quietly producing a form nobody can file.

**A U.S. GOVERNMENT WORK, and therefore not under this app's MIT licence and not
under anyone's copyright** (17 U.S.C. §105). It is redistributed here for the
purpose it was published for: an employer completing it.

### When USCIS revises it

The edition date is printed in the bottom-left corner of every page and is
restated in `i9_pdf.EDITION`. A new edition means:

1. Download the new PDF over this one.
2. Re-run `python3 -m unittest tests_standalone.test_i9_pdf` — the checksum test
   fails by design, and the field-name tests say which of the 133 names moved.
3. Update `EDITION`, the checksum in `i9_pdf.TEMPLATE_SHA256`, and this file.

Nothing else in the app hardcodes the layout. The field *names* are the whole
interface, and they live in one table in `i9_pdf.py`.

## `w4_form.pdf`

The official **IRS Form W-4, Employee's Withholding Certificate**, downloaded
unmodified from

    https://www.irs.gov/pub/irs-pdf/fw4.pdf

| | |
|---|---|
| Agency | Internal Revenue Service, Department of the Treasury |
| OMB control number | 1545-0074 |
| Edition | Form W-4 (2026), created 12/8/25 — Cat. No. 10220Q |
| Pages | 5 — the form, General Instructions, the Multiple Jobs Worksheet, the Deductions Worksheet, the Multiple Jobs tables |
| SHA-256 | `92444d8856ce55d9e25dca8b6d1420634fc68b11e1ab1f760916ea29ddd312b2` |
| Bytes | 208,845 |
| AcroForm fields | 54, **and an XFA payload** |

**IT IS SHIPPED BYTE-FOR-BYTE AND IS NEVER EDITED IN PLACE**, the same promise
`i9_form.pdf` makes above. `erpnext_mcp/w4_pdf.py` opens it read-only and writes
into a copy.

**THE COPY LOSES ITS XFA AND THE ORIGINAL KEEPS IT.** Unlike the USCIS form, the
IRS file is a hybrid: an ordinary AcroForm plus an XML payload describing the
same form. A viewer that understands XFA — Acrobat does — renders the XFA and
IGNORES the AcroForm, so a filled copy that kept it would hold every right answer
and print blank. `fill_w4_pdf` deletes `/XFA` from the copy. That the template on
disk still HAS it is asserted too, so the day the IRS ships a year without one,
the deletion can go rather than be carried forever.

**A U.S. GOVERNMENT WORK**, on the same footing as the I-9 above (17 U.S.C. §105).

### When the IRS revises it — which is every year

Form W-4 is reissued annually and the year is printed in its masthead and in the
bottom-right corner of page 1, restated in `w4_pdf.EDITION` and
`w4_pdf.TEMPLATE_TAX_YEAR`. A new year means:

1. Download the new PDF over this one.
2. Re-run `python3 -m unittest tests_standalone.test_w4_pdf` — the checksum test
   fails by design, and `TheFieldTableIsCheckedAgainstGeometry` says which names
   moved. That class is the one that matters here: the IRS names its fields
   `f1_12[0]` rather than after the boxes, so a renumbered form would otherwise
   fill perfectly valid fields with the wrong values.
3. Update `EDITION`, `TEMPLATE_TAX_YEAR`, the checksum in
   `w4_pdf.TEMPLATE_SHA256`, and this file.

**A W-4 for a year the template does not print is still rendered**, on the page
that is here, and `render_w4_pdf` reports `template_tax_year_matches: false`. A
prior year's election on this year's page is a readable record; no form at all is
not.

## Oregon: `or_w4_form.pdf` and its references (v0.295.0)

Downloaded unmodified from the Oregon Department of Revenue on 2026-10-09, with Tim's approval (source:
oregon.gov/dor only).

| File | Official title | Source URL | Revision | Bytes | SHA-256 |
|---|---|---|---|---|---|
| `or_w4_form.pdf` | 2026 Form OR-W-4, Oregon Withholding Statement and Exemption Certificate, 150-101-402 | https://www.oregon.gov/dor/forms/FormsPubs/form-or-W-4_101-402_2026.pdf | Rev. 07-28-25, ver. 01 | 65,323 | `78108312be71f0d10eddc922fb9e478b40cda60d85bd01f16360e5557c031709` |
| `or_w4_instructions.pdf` | 2026 Form OR-W-4 Instructions, 150-101-402-1 | https://www.oregon.gov/dor/forms/FormsPubs/form-or-W-4-instr_101-402-1_2026.pdf | Rev. 02-17-26 | 274,057 | `c80963c5358ab74b3dfbc2d4158dd081277300f3151e74994ff2bf13fc44d819` |
| `or_w4_instructions_es.pdf` | 2026 Formulario OR-W-4 Instrucciones, 150-101-402-1-5 | https://www.oregon.gov/dor/forms/FormsPubs/form-or-W-4-instr_sp_101-402-1-5_2026.pdf | Rev. 02-17-26 | 282,862 | `329e9e6f4846f879c776317054a1050954cb84d7a16dc55594303836b2f269a2` |
| `or_w4_employer_information.pdf` | W-4 Information for Employers, 150-211-602 | https://www.oregon.gov/dor/forms/FormsPubs/w-4-information_150-211-602.pdf | Rev. 02-18-22 | 70,695 | `e044bc37d5bacb13fed0540fc36d11cca43474f809947c1d6eabb9ee14826d73` |

**The form is filled; the other three are reference copies.** `erpnext_mcp/or_w4_pdf.py` opens `or_w4_form.pdf`
read-only, writes the employee's elections into its own AcroForm fields (`w4-p1-1` …, the line 1 radio
`Check Box2` with `/Single` `/Married` `/3`, the Redetermination box `w4-p1-5`), and draws the employer block,
which has no fields, and the signature into the copy. The SSN boxes are left for the employee, as on the federal
W-4. The instructions, in English and Spanish, are what the phone shows a worker. The employer publication is the
source of the 8% rules: an Oregon-only exemption, or an expired one.

These are Oregon state publications, not U.S. Government works. They are published by the DOR for employers and
employees to complete and keep, and they are redistributed here unmodified for that purpose. The checksums are
asserted by `tests_standalone/test_or_w4_pdf.py`.

### When the DOR revises them

The OR-W-4 is reissued each year (the year is in the masthead). Download the new files from oregon.gov/dor over
these, re-run `test_or_w4_pdf` (the checksum and field-name tests fail by design), then update
`or_w4_pdf.EDITION`, `TEMPLATE_TAX_YEAR`, `TEMPLATE_SHA256` and this table.
