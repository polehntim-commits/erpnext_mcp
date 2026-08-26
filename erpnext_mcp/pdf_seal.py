# SPDX-License-Identifier: MIT
"""The verification page that makes a signed form tamper-EVIDENT, and its hash.

v0.63.0. Step 5 of the chain `tools/signing_evidence.py` opens with, and the one
step that module said was "a later release". Steps 1 to 4 are collected in the
field and written to a register; this is where they become part of the artefact
an inspector is handed.

────────────────────────────────────────────────────────────────────────────
WHAT WAS ALREADY TAMPER-RESISTANT, AND WHY THAT IS NOT THE SAME THING
────────────────────────────────────────────────────────────────────────────

`pdf_signing.py` has stamped captures into the page CONTENT and flattened the
form since v0.51.0, so a signed I-9 has no annotation to delete and no field to
clear. That is tamper-RESISTANT: altering it takes a PDF editor rather than a
click in Preview.

It is not tamper-EVIDENT, because resistance says nothing about DETECTION. A
page that has been edited with a real editor looks exactly like one that has
not, and the person holding it has no way to tell. Evidence is the property that
an alteration can be NOTICED — which needs two things this module adds:

  1. A VERIFICATION PAGE stating, in print, the facts the signature was
     collected under: who signed, how their identity was established at the pad,
     when, on what device, at what coordinates, and the fingerprint of the
     record's own content at the moment it was presented. A page that has been
     altered contradicts its own appendix.
  2. A SHA-256 OF THE FINISHED FILE, stored on the Signing Evidence row rather
     than on the page. Re-hash the file later and either it is byte-identical to
     what was recorded or it is not — which is the only check that catches an
     edit made anywhere on any page, including this one.

THE FILE HASH CANNOT BE PRINTED ON THE FILE. It is taken over the finished
bytes, and printing it would change them. So the page carries the DOCUMENT
fingerprint — the hash of the record's content as the signer was shown it, which
`signing_evidence.document_fingerprint` took before the ink landed — and the file
hash lives beside it in the register. Two hashes answering two questions: "is
this the form they saw" and "is this the file we produced".

────────────────────────────────────────────────────────────────────────────
PURE FUNCTIONS. NO DATABASE, NO FRAPPE.
────────────────────────────────────────────────────────────────────────────

The same line `i9_pdf.py` and `w4_pdf.py` keep and for the same reason: bytes and
dicts in, bytes out, checkable against a fixture with no site behind it. Reading
an evidence row and attaching the result is `tools/signed_documents.py`'s work.

reportlab and pypdf are imported defensively, as everywhere else in this app. A
bench without them gets `available() is False`, an unsealed signed form — which
is exactly the artefact every release before this one produced — and a sentence
saying so. A page is worth less than the attestation it depicts.
"""

from __future__ import annotations

import hashlib
import io

from .errors import ToolError

#: Fallback page size where the source document will not report one. US Letter,
#: because every form this app seals is a US federal form.
DEFAULT_PAGE = (612.0, 792.0)

#: Margins, leading and sizes for the appended page, in points.
MARGIN = 54.0
TITLE_SIZE = 13.0
HEADING_SIZE = 9.5
BODY_SIZE = 8.5
MONO_SIZE = 7.0
LINE = 12.5

#: How wide the label column is. Wide enough for "Verification method" at
#: `BODY_SIZE` with room to spare, so no label wraps and no value starts ragged.
LABEL_WIDTH = 132.0

#: What the page says it is. NOT a claim that the signature is valid — this app
#: does not get to decide that — but a statement of what was recorded and how,
#: which is the thing an employer is actually asked to produce.
TITLE = "Electronic Signature Verification Record"

CITATION = (
	"Recorded under 8 CFR 274a.2(h) and the E-SIGN Act (15 U.S.C. 7001). This page is "
	"generated from the Signing Evidence register and is part of the retained document."
)

#: Printed under the last block. Says what the two hashes mean, because a page of
#: hex that nobody knows how to check is decoration.
FOOTER = (
	"The document fingerprint above covers the record's content as it stood when the signer "
	"was shown it, excluding signature columns and this app's own bookkeeping. A SHA-256 of "
	"this finished PDF is held on the Signing Evidence row named above; re-hash the file and "
	"compare to detect any alteration to any page, including this one."
)


def available() -> bool:
	"""Whether this bench can draw a verification page at all."""
	try:
		import pypdf  # noqa: F401
		import reportlab  # noqa: F401
	except ImportError:
		return False
	return True


def require() -> None:
	"""Raise naming what is missing. Callers that can degrade should not call this."""
	if not available():
		raise ToolError(
			"sealing a signed document needs the reportlab and pypdf Python packages, which "
			"this app declares as dependencies — install them into the bench's environment "
			"with `./env/bin/pip install 'reportlab>=4.0' pypdf` and restart. The signed form "
			"still renders without them, with the signatures stamped in and no verification "
			"page appended."
		)


def sha256_of(content: bytes) -> str:
	"""`sha256:<hex>` for some bytes. The prefix is the format `document_hash` uses.

	Spelled the same way on both columns on purpose: an operator comparing a
	document fingerprint with a file hash should not have to notice that one of
	them carries an algorithm name and the other does not.
	"""
	return "sha256:" + hashlib.sha256(content or b"").hexdigest()


# ── what the page says ──────────────────────────────────────────────────────
def block_lines(evidence: dict) -> list[tuple]:
	"""One signature's facts as `(label, value)` pairs, in the order they print.

	EVERY PAIR THAT HAS NO VALUE IS DROPPED RATHER THAN PRINTED EMPTY, with one
	exception argued below. A verification page with "GPS: —" on it invites the
	reading that a fix was taken and lost; a page that does not mention GPS says
	the true thing, which is that this record does not carry one.

	THE EXCEPTION IS `Identity verified`. An absent identity check is the single
	most important thing on this page — it is the difference between the packet
	that answers "how do you know it was him" and the one that does not — so it is
	printed as "No identity check was made at the pad" rather than omitted. The
	register says the same thing in one word (`status: Unverified`); a page that
	quietly left the line out would be the artefact disagreeing with the row.
	"""
	pairs: list[tuple] = []

	def add(label: str, value) -> None:
		text = str(value if value is not None else "").strip()
		if text:
			pairs.append((label, text))

	add("Document", _document_label(evidence))
	add("Signature", _signature_label(evidence))
	add("Signed by", evidence.get("signer_name") or evidence.get("signer"))
	add("Employee record", evidence.get("signer"))
	add("Capacity", evidence.get("signature_role"))
	add("Filed by account", evidence.get("signer_user"))

	method = str(evidence.get("verification_method") or "").strip()
	if method:
		pairs.append(("Identity verified", method))
		add("Badge scanned", evidence.get("signer_badge"))
	else:
		pairs.append(
			(
				"Identity verified",
				"No identity check was made at the pad (recorded as Unverified).",
			)
		)

	add("Signed at", evidence.get("signed_at"))
	add("Device", evidence.get("device_id"))
	add("Address", evidence.get("ip_address"))
	add("Coordinates", _coordinates(evidence))
	add("Evidence record", evidence.get("name"))
	add("Register status", evidence.get("status"))
	add("Supersedes", evidence.get("supersedes"))
	add("Document fingerprint", evidence.get("document_hash"))
	return pairs


def _document_label(evidence: dict) -> str:
	doctype = str(evidence.get("document_type") or "").strip()
	docname = str(evidence.get("document_name") or "").strip()
	return " ".join(part for part in (doctype, docname) if part)


def _signature_label(evidence: dict) -> str:
	"""Which box, as a person names it, falling back to the column name.

	`label` is what `signatures.SIGNATURE_BOXES` calls the box — "I-9 Section 2
	(employer verification)" — and is passed in by the caller where it has one.
	The raw fieldname is the honest fallback rather than a humanised guess: a
	column called `section_3_signature` holds the SUPPLEMENT B attestation on the
	current form, and prettifying it would print a section number USCIS retired.
	"""
	return str(evidence.get("label") or evidence.get("signature_field") or "").strip()


def _coordinates(evidence: dict) -> str:
	"""`45.5231, -122.6765` or "". ALL OR NOTHING, as the client sends them.

	A latitude with no longitude is a point on a line rather than a place, and
	printing half a fix on a verification page would be worse than printing none.

	EXACTLY (0, 0) IS NOT A PLACE, IT IS AN UNSET FLOAT COLUMN. v0.136.0, and
	this is the bug that put `Coordinates 0.000000, 0.000000` on every sealed
	I-9 this app has produced. `gps_latitude` and `gps_longitude` are `Float`
	on Signing Evidence, `signing_evidence.record` passes `None` for a signature
	that reported no fix, and Frappe CASTS `None` TO `0.0` on insert — so the
	`in (None, "")` test above, which is correct about the argument, is never
	reached by the value that comes back out of the database. The row says 0.0
	and this page said the attestation was made at 0°N 0°E, which is open water
	in the Gulf of Guinea about 300 miles off Ghana.

	Printing a location a signer was not at is the one direction that matters on
	a verification page: an inspector reading coordinates has been told where
	somebody stood, and a record asserting more than it knows is the failure
	`signing_evidence.py` opens by arguing against. A blank line says "not
	recorded", which is true.

	ONLY THE EXACT PAIR IS TREATED AS UNSET, not either value on its own. A
	latitude of 0 with a real longitude is a point on the equator and a real
	place; a farm at exactly 0.000000, 0.000000 is in the ocean. The narrow rule
	is what keeps this from being a second zero-drop of the kind
	`test_zero_drop.py` exists to catch.

	`tools/housing._gps` IS THE PRECEDENT AND HAS DONE THIS SINCE IT WAS WRITTEN,
	which makes this a consistency fix rather than a new rule — it guards
	`in (None, "")` and then `if not float(latitude) and not float(longitude)`,
	the second line being exactly the check added here. `housing.check_coordinates`
	states the reasoning in the same words this docstring reaches for: a record
	carrying half a coordinate "would sit on a map at longitude zero — off the
	coast of Ghana, on the same meridian as everything else anybody forgot to
	finish."
	"""
	latitude = evidence.get("gps_latitude")
	longitude = evidence.get("gps_longitude")
	if latitude in (None, "") or longitude in (None, ""):
		return ""
	try:
		fix = (float(latitude), float(longitude))
	except (TypeError, ValueError):  # pragma: no cover - a column holding prose
		return ""
	if fix == (0.0, 0.0):
		return ""
	return f"{fix[0]:.6f}, {fix[1]:.6f}"


# ── drawing it ──────────────────────────────────────────────────────────────
def seal(pdf_bytes: bytes, evidence_rows: list, note: str = "") -> bytes:
	"""The document with a verification page appended. Returns the sealed bytes.

	Args:
		pdf_bytes: The rendered form, signatures already stamped into the page
			content and the AcroForm already flattened — which is what
			`render_i9_pdf` and `render_w4_pdf` produce. This function does NOT
			composite signatures; those modules place them at the form's own
			named widget rectangles, which is where they belong and where a
			second implementation here would put them slightly differently.
		evidence_rows: Signing Evidence rows, oldest first, each a dict of the
			columns `block_lines` reads. One block is printed per row, so a Form
			I-9 signed by the worker in July and the employer in August carries
			both — an appendix naming one of two signatures would be a page that
			looks complete and is not.
		note: An extra sentence under the footer, where the caller has one worth
			printing (a re-seal, a row that could not be read).

	Returns:
		PDF bytes. The input is not modified.

	Raises:
		ToolError: only via `require`, and only where the bench cannot draw at
			all. Every other failure belongs to the caller's degradation path.
	"""
	require()
	from pypdf import PdfReader, PdfWriter

	reader = PdfReader(io.BytesIO(pdf_bytes))
	width, height = _page_size(reader)
	writer = PdfWriter()
	for page in reader.pages:
		writer.add_page(page)

	for page_bytes in _verification_pages(evidence_rows, width, height, note):
		writer.add_page(PdfReader(io.BytesIO(page_bytes)).pages[0])

	buffer = io.BytesIO()
	writer.write(buffer)
	payload = buffer.getvalue()
	if not payload.startswith(b"%PDF"):  # pragma: no cover - defensive
		raise ToolError(
			f"appending the verification page produced {len(payload)} byte(s) that are not a "
			f"PDF. Nothing was attached."
		)
	return payload


def _page_size(reader) -> tuple:
	"""The source document's own page size, so the appendix does not look bolted on."""
	try:
		box = reader.pages[0].mediabox
		return float(box.width), float(box.height)
	except Exception:  # pragma: no cover - a reader with no readable first page
		return DEFAULT_PAGE


def _verification_pages(evidence_rows: list, width: float, height: float, note: str) -> list:
	"""One page per canvas-full of blocks. A list, because three signatures overflow.

	PAGINATION RATHER THAN TRUNCATION. A Form I-9 carries three signatures and a
	Supplement B can carry three more; a single page that silently dropped the
	last of them would be an appendix that looks complete and is not — the exact
	failure `i9_pdf._overflow_note` exists to avoid on the form itself.
	"""
	from reportlab.pdfgen import canvas

	pages = []
	rows = list(evidence_rows or [])
	index = 0
	page_number = 0
	while True:
		page_number += 1
		buffer = io.BytesIO()
		sheet = canvas.Canvas(buffer, pagesize=(width, height))
		cursor = _draw_header(sheet, width, height, page_number)

		drawn = 0
		while index < len(rows):
			block = block_lines(rows[index])
			needed = LINE * (len(block) + 2)
			if drawn and cursor - needed < MARGIN + LINE * 4:
				break
			cursor = _draw_block(sheet, width, cursor, index + 1, len(rows), block)
			index += 1
			drawn += 1

		if not rows and page_number == 1:
			cursor = _draw_wrapped(
				sheet,
				MARGIN,
				cursor,
				width - 2 * MARGIN,
				"No Signing Evidence row was found for this document, so this page records the "
				"seal and nothing about a signer. A signature collected before v0.60.0 has no "
				"evidence row and cannot grow one retrospectively.",
				BODY_SIZE,
			)

		if index >= len(rows):
			_draw_footer(sheet, width, cursor, note)
		sheet.save()
		pages.append(buffer.getvalue())
		if index >= len(rows):
			return pages


def _draw_header(sheet, width: float, height: float, page_number: int) -> float:
	cursor = height - MARGIN
	sheet.setFont("Helvetica-Bold", TITLE_SIZE)
	sheet.drawString(MARGIN, cursor, TITLE + (" (continued)" if page_number > 1 else ""))
	cursor -= LINE * 1.4
	sheet.setLineWidth(0.75)
	sheet.line(MARGIN, cursor, width - MARGIN, cursor)
	cursor -= LINE * 1.2
	return _draw_wrapped(sheet, MARGIN, cursor, width - 2 * MARGIN, CITATION, BODY_SIZE) - LINE * 0.6


def _draw_block(sheet, width: float, cursor: float, position: int, total: int, block: list) -> float:
	sheet.setFont("Helvetica-Bold", HEADING_SIZE)
	sheet.drawString(MARGIN, cursor, f"Signature {position} of {total}")
	cursor -= LINE
	for label, value in block:
		sheet.setFont("Helvetica-Bold", BODY_SIZE)
		sheet.drawString(MARGIN, cursor, f"{label}")
		# The hex strings are the only values worth a monospaced face: a
		# fingerprint somebody is comparing by eye against another one is exactly
		# the case proportional digits make harder.
		mono = label.endswith("fingerprint")
		sheet.setFont("Courier" if mono else "Helvetica", MONO_SIZE if mono else BODY_SIZE)
		cursor = _draw_wrapped(
			sheet,
			MARGIN + LABEL_WIDTH,
			cursor,
			width - MARGIN - LABEL_WIDTH - MARGIN,
			value,
			MONO_SIZE if mono else BODY_SIZE,
			font="Courier" if mono else "Helvetica",
		)
	return cursor - LINE * 0.8


def _draw_footer(sheet, width: float, cursor: float, note: str) -> None:
	cursor -= LINE * 0.4
	sheet.setLineWidth(0.5)
	sheet.line(MARGIN, cursor, width - MARGIN, cursor)
	cursor -= LINE
	sheet.setFont("Helvetica", BODY_SIZE)
	cursor = _draw_wrapped(sheet, MARGIN, cursor, width - 2 * MARGIN, FOOTER, BODY_SIZE)
	if str(note or "").strip():
		cursor -= LINE * 0.4
		_draw_wrapped(sheet, MARGIN, cursor, width - 2 * MARGIN, str(note).strip(), BODY_SIZE)


def _draw_wrapped(
	sheet, x: float, y: float, wide: float, text: str, size: float, font: str = "Helvetica"
) -> float:
	"""Draw `text` wrapped to `wide` points. Returns the cursor below the last line.

	MEASURED WITH `stringWidth` RATHER THAN COUNTED IN CHARACTERS, because a
	fingerprint is 71 characters of Courier and a citation is 180 of Helvetica,
	and one character budget for both would either wrap the citation twice too
	early or run the hash off the page.
	"""
	from reportlab.pdfbase.pdfmetrics import stringWidth

	sheet.setFont(font, size)
	for line in _wrap(str(text or ""), wide, font, size, stringWidth):
		sheet.drawString(x, y, line)
		y -= LINE
	return y


def _wrap(text: str, wide: float, font: str, size: float, measure) -> list:
	"""Greedy word wrap, with a character-level fallback for one long token.

	The fallback is what a 71-character hash needs: it contains no spaces, so a
	word-only wrapper would hand back one line and reportlab would draw it off
	the right edge of the page — silently, which is the failure mode that makes
	a verification record useless without anybody noticing.
	"""
	lines: list = []
	current = ""
	for word in text.split():
		candidate = f"{current} {word}".strip()
		if current and measure(candidate, font, size) > wide:
			lines.append(current)
			current = word
		else:
			current = candidate
		while measure(current, font, size) > wide and len(current) > 1:
			cut = len(current)
			while cut > 1 and measure(current[:cut], font, size) > wide:
				cut -= 1
			lines.append(current[:cut])
			current = current[cut:]
	if current:
		lines.append(current)
	return lines or [""]
