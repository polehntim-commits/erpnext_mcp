"""Fill Oregon's own Form OR-W-4 (150-101-402). v0.295.0.

The same shape and the same promise as `w4_pdf.py`: the DOR's fillable PDF ships unmodified in
`templates/or_w4_form.pdf` (provenance in `templates/README.md`), and this module writes a record's values into a
COPY, never a lookalike.

What the DOR's page has fields for is filled through those fields: the name, the address, the line 1 radio, lines
2, 3, 4a and 4b, the Redetermination box, and the date. The Employer-use-only block has NO fields on the 2026 page,
so the employer's name, FEIN and address are drawn into the copy at measured positions, the same way the signature
is stamped (`pdf_signing.stamp`). The SSN boxes are left for the employee, as on the federal W-4.

Line 3 prints `.00` after its box, so the additional amount is written in whole dollars; a cents value is rounded
and `plan()` says so.

PURE: record, employee and employer in, bytes out. `tools/w4.render_w4_pdf form=or_w4` does the reading and the
attaching.
"""

from __future__ import annotations

import hashlib
import io
import os
import re

from . import pdf_signing
from .errors import ToolError

try:  # pragma: no cover - exercised by whichever branch the bench has
	from pypdf import PdfWriter

	HAVE_PYPDF = True
except Exception:  # pragma: no cover - a bench without the dependency
	PdfWriter = None
	HAVE_PYPDF = False

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "or_w4_form.pdf")
EDITION = "2026 Form OR-W-4, 150-101-402 (Rev. 07-28-25, ver. 01)"
TEMPLATE_TAX_YEAR = 2026
TEMPLATE_SHA256 = "78108312be71f0d10eddc922fb9e478b40cda60d85bd01f16360e5557c031709"
SOURCE_URL = "https://www.oregon.gov/dor/forms/FormsPubs/form-or-W-4_101-402_2026.pdf"
PAGE_FORM = 0

#: The DOR's own field names on the 2026 page, each checked against its widget's position in `test_or_w4_pdf.py`.
FIELD_FIRST_NAME = "w4-p1-1"
FIELD_INITIAL = "w4-p1-2"
FIELD_LAST_NAME = "w4-p1-3"
FIELDS_SSN = ("w4-p1-4a", "w4-p1-4b", "w4-p1-4c")  # never written
FIELD_REDETERMINATION = "w4-p1-5"
REDETERMINATION_ON = "/Yes"
FIELD_ADDRESS = "w4-p1-6"
FIELD_CITY = "w4-p1-7"
FIELD_STATE = "w4-p1-8"
FIELD_ZIP = "w4-p1-9"
#: Line 1 is one radio group with three on-states.
FIELD_STATUS = "Check Box2"
STATUS_STATES = {"Single": "/Single", "Married": "/Married", "Married, withhold at higher Single rate": "/3"}
FIELD_ALLOWANCES = "w4-p1-11"  # line 2
FIELD_ADDITIONAL = "w4-p1-12"  # line 3, whole dollars (".00" is printed)
FIELD_EXEMPTION_CODE = "w4-p1-13"  # line 4a
FIELD_EXEMPT_WORD = "w4-p1-14"  # line 4b: the employee writes "Exempt"
FIELD_DATE_MONTH, FIELD_DATE_DAY, FIELD_DATE_YEAR = "w4-p1-15a", "w4-p1-15b", "w4-p1-15c"

#: The exemption chart in the 2026 instructions (150-101-402-1, Rev. 02-17-26, page 4). Line 4a takes one code.
EXEMPTION_CODES = {
	"A": "Air carrier employee",
	"B": "American Indian enrolled tribal member living and working in Indian country in Oregon",
	"C": "Amtrak Act worker",
	"D": "Casual laborer",
	"E": "Domestic service worker",
	"F": "Hydroelectric dam worker at the Bonneville, John Day, McNary, or The Dalles dam",
	"G": "Military pay (nonresidents stationed in Oregon and spouses, residents stationed outside Oregon, and others "
	     "treated as nonresidents)",
	"H": "Minister or member of a religious order performing their duties",
	"J": "Real estate salesperson under a written contract not to be treated as an employee",
	"K": "Waterway worker",
	"L": "No tax liability",
	"M": "Nonresident expecting a refund of all Oregon tax withheld",
}

#: Where the field-less parts go, in PDF points on page 1, measured off the 2026 page (labels sit above their
#: rules): the signature band is between "Employee signature" (y 365) and the rule at y 348; the employer rows are
#: y 314-328 (name | FEIN at x 296) and y 289-304 (address | city x 296 | state x 461 | ZIP x 497).
SIGNATURE_BOX = (40.0, 347.0, 418.0, 364.0)
EMPLOYER_TEXT = {"name": (40.0, 317.0), "ein": (300.0, 317.0), "address": (40.0, 292.0),
                 "city": (300.0, 292.0), "state": (464.0, 292.0), "zip": (500.0, 292.0)}
EMPLOYER_FONT_SIZE = 9


def available() -> bool:
	return bool(HAVE_PYPDF) and os.path.exists(TEMPLATE_PATH)


def require() -> None:
	if not HAVE_PYPDF:
		raise ToolError("this site is missing the pypdf Python package — install it into the bench's environment "
		                "with `./env/bin/pip install 'pypdf>=4.0'` and restart. Nothing was changed.")
	if not os.path.exists(TEMPLATE_PATH):
		raise ToolError(f"the shipped Oregon template is missing at {TEMPLATE_PATH} — reinstall the app, or download "
		                f"it from {SOURCE_URL} and put it there. Nothing was changed.")


def template_sha256() -> str:
	with open(TEMPLATE_PATH, "rb") as handle:
		return hashlib.sha256(handle.read()).hexdigest()


def _text(value) -> str:
	return "" if value is None else " ".join(str(value).split())


def _flag(value) -> bool:
	return value in (1, "1", True, "True", "true")


def split_address(line: str) -> dict:
	"""`12 Orchard Rd, Hood River, OR 97031` into its four boxes; anything else stays whole in the address box."""
	text = _text(line)
	match = re.match(r"^(.*?),\s*([^,]+?),\s*([A-Za-z]{2})\.?\s+(\d{5}(?:-\d{4})?)$", text)
	if not match:
		return {"address": text, "city": "", "state": "", "zip": ""}
	street, city, state, postcode = match.groups()
	return {"address": street.strip(), "city": city.strip(), "state": state.upper(), "zip": postcode}


def _date_parts(value) -> tuple:
	head = _text(value).split(" ")[0].split("T")[0]
	parts = head.split("-")
	if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[0]) == 4:
		return f"{int(parts[1]):02d}", f"{int(parts[2]):02d}", parts[0]
	return "", "", ""


def plan(record: dict, employee: dict) -> dict:
	"""Every field value this form will carry, plus notes. No PDF is opened."""
	values, notes = {}, []
	first = _text(employee.get("first_name"))
	if first:
		values[FIELD_FIRST_NAME] = first
	initial = _text(employee.get("middle_name"))[:1].upper()
	if initial:
		values[FIELD_INITIAL] = initial
	last = _text(employee.get("last_name"))
	if last:
		values[FIELD_LAST_NAME] = last
	for field, key in ((FIELD_ADDRESS, "address"), (FIELD_CITY, "city"), (FIELD_STATE, "state"), (FIELD_ZIP, "zip")):
		value = _text(employee.get(key))
		if value:
			values[field] = value
	if _flag(record.get("or_redetermination")):
		values[FIELD_REDETERMINATION] = REDETERMINATION_ON

	exempt = _flag(record.get("or_exempt"))
	state = STATUS_STATES.get(_text(record.get("or_filing_status")))
	if state and not exempt:
		values[FIELD_STATUS] = state
	if exempt:
		code = _text(record.get("or_exemption_code")).upper()
		if code:
			values[FIELD_EXEMPTION_CODE] = code
		else:
			notes.append("exempt, but no line 4a exemption code is recorded — the form is incomplete without one")
		values[FIELD_EXEMPT_WORD] = "Exempt"
	else:
		values[FIELD_ALLOWANCES] = str(int(record.get("or_allowances") or 0))
		extra = float(record.get("or_additional_withholding") or 0)
		if extra > 0:
			values[FIELD_ADDITIONAL] = f"{round(extra):,d}"
			if abs(extra - round(extra)) > 0.004:
				notes.append(f"line 3 is whole dollars on the form: ${extra:,.2f} is printed as ${round(extra):,d}")

	month, day, year = _date_parts(record.get("or_signed_at"))
	if year:
		values.update({FIELD_DATE_MONTH: month, FIELD_DATE_DAY: day, FIELD_DATE_YEAR: year})
	return {"values": values, "notes": notes}


def _draw_employer(writer, employer: dict) -> bool:
	"""The Employer-use-only block, drawn into the copy (the page has no fields there). False when it cannot be."""
	block = {"name": _text(employer.get("name")), "ein": _text(employer.get("ein")),
	         **split_address(employer.get("address") or "")}
	if not any(block.values()):
		return True
	try:
		from pypdf import PdfReader
		from reportlab.pdfgen import canvas
	except ImportError:
		return False
	page = writer.pages[PAGE_FORM]
	buffer = io.BytesIO()
	overlay = canvas.Canvas(buffer, pagesize=(float(page.mediabox.width), float(page.mediabox.height)))
	overlay.setFont("Helvetica", EMPLOYER_FONT_SIZE)
	limits = {"name": 250, "address": 250, "ein": 270, "city": 160, "state": 30, "zip": 75}
	for key, (x, y) in EMPLOYER_TEXT.items():
		text = block.get(key) or ""
		while text and overlay.stringWidth(text, "Helvetica", EMPLOYER_FONT_SIZE) > limits[key]:
			text = text[:-2].rstrip() + "…"
		if text:
			overlay.drawString(x, y, text)
	overlay.save()
	page.merge_page(PdfReader(io.BytesIO(buffer.getvalue())).pages[0])
	return True


def fill_or_w4_pdf(record: dict, employee: dict, employer: dict, signature: bytes | None = None) -> tuple:
	"""(PDF bytes, notes). The shipped DOR form with this record in its boxes; the template is never modified."""
	require()
	planned = plan(record, employee or {})
	notes = list(planned["notes"])
	writer = PdfWriter(clone_from=TEMPLATE_PATH)
	writer.set_need_appearances_writer(True)
	writer.update_page_form_field_values(writer.pages[PAGE_FORM], planned["values"], auto_regenerate=False)
	# ALWAYS FLATTENED, unlike the federal W-4: some viewers (macOS Preview among them) draw the DOR's radio and
	# narrow date boxes from the field values and get them wrong, and an unsigned copy is for printing and signing
	# in ink — nobody edits it.
	pdf_signing.flatten(writer)
	if not _draw_employer(writer, employer or {}):
		notes.append("the employer block could not be drawn on this bench (reportlab missing) — write it in by hand")
	if signature:
		x0, y0, x1, y1 = SIGNATURE_BOX
		if not pdf_signing.stamp(writer, PAGE_FORM, [x0, y0, x1, y1], signature, max_height=y1 - y0):
			notes.append("the captured signature could not be drawn — sign the printed form")
	buffer = io.BytesIO()
	writer.write(buffer)
	payload = buffer.getvalue()
	if not payload.startswith(b"%PDF"):  # pragma: no cover - defensive
		raise ToolError("filling the OR-W-4 template did not produce a PDF. Nothing was attached.")
	return payload, notes


def file_name_for(record: dict, employee: dict | None = None) -> str:
	parts = ["OR-W-4", _text(record.get("name")) or "form", _text(record.get("tax_year"))]
	person = employee or {}
	parts += [_text(person.get("last_name")), _text(person.get("first_name"))]
	return "-".join(re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-") for p in parts if p) + ".pdf"
