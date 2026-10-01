# SPDX-License-Identifier: MIT
"""Card artwork at the card's own page size. v0.208.0.

docs/design/card_print_queue.md §3. The print queue needs ONE CARD PER PAGE at
exactly the card's size; the generators this app already has produce a QR as a
PNG, or a card on a sheet of Letter. This wraps them and changes neither:

  Employee ID   the existing card design — `badge_sheet.card_html` and
                `badge_print_format.CARD_CSS` — through the same PDF renderer
                the ID Card button uses, with the page set to the card.
  Asset Tag     a new layout (the app has no asset card), drawn with reportlab
                around the QR `generate_asset_qr` already encodes.

Nothing here writes to the site. A PDF that cannot be made raises `ArtError`
with a sentence; the caller creates no job.
"""

from __future__ import annotations

import html
import io

MM = 72.0 / 25.4
MAX_BYTES = 2 * 1024 * 1024


class ArtError(RuntimeError):
	"""The artwork could not be rendered. The message is for a person."""


# ── Employee ID: the existing design, on a card-sized page ──────────────────
def employee_html(card: dict, sides: str, width_mm: float, height_mm: float) -> str:
	from .badge_print_format import CARD_CSS
	from .badge_sheet import card_html

	pages = [card_html(card)]
	if sides == "Dual":
		pages.append(_back_html(card))
	page_css = (
		f"@page {{ size: {width_mm}mm {height_mm}mm; margin: 0; }}"
		f"html, body {{ margin: 0; padding: 0; width: {width_mm}mm; background: #ffffff; }}"
		".badge-card { page-break-after: always; break-after: page; margin: 0; }"
		".badge-card:last-child { page-break-after: auto; break-after: auto; }"
	)
	return (
		'<!DOCTYPE html><html><head><meta charset="utf-8">'
		f"<style>{CARD_CSS}{page_css}</style></head><body>{''.join(pages)}</body></html>"
	)


def _back_html(card: dict) -> str:
	"""The Print Format's back: the 38.1 mm symbol, the badge id and the return note."""
	company = html.escape(str(card.get("company_name") or card.get("company") or "this employer"))
	badge = html.escape(str(card.get("badge_id") or ""))
	blob = str(card.get("png_base64") or "")
	symbol = (
		f'<img class="bc-abs bc-back-qr" src="data:image/png;base64,{blob}" alt="">'
		if blob
		else '<div class="bc-abs bc-missing">No QR could be drawn for this badge.</div>'
	)
	return (
		'<div class="badge-card">'
		f'<div class="bc-abs bc-back-name">{company}</div>{symbol}'
		f'<div class="bc-abs bc-back-id">{badge}</div>'
		f'<div class="bc-abs bc-back-note">Property of {company}. If found, please return it. '
		"This card records piece work and is not proof of identity.</div></div>"
	)


def employee_pdf(card: dict, sides: str = "Single", width_mm: float = 85.6, height_mm: float = 54.0) -> bytes:
	if not card.get("png_base64"):
		raise ArtError(
			"no QR encoder is installed on this server (segno), so the badge code cannot be drawn. "
			"Nothing was queued."
		)
	try:
		from frappe.utils.pdf import get_pdf
	except Exception as exc:
		raise ArtError(
			f"this server has no PDF renderer ({type(exc).__name__}); wkhtmltopdf is what draws the ID "
			"card. Nothing was queued."
		) from exc
	options = {
		"page-width": f"{width_mm}mm",
		"page-height": f"{height_mm}mm",
		"margin-top": "0mm",
		"margin-bottom": "0mm",
		"margin-left": "0mm",
		"margin-right": "0mm",
		"disable-smart-shrinking": "",
		"dpi": "300",
	}
	try:
		pdf = get_pdf(employee_html(card, sides, width_mm, height_mm), options=options)
	except TypeError:
		pdf = get_pdf(employee_html(card, sides, width_mm, height_mm))
	except Exception as exc:
		raise ArtError(f"the PDF renderer failed ({type(exc).__name__}: {exc}). Nothing was queued.") from exc
	return _checked(pdf)


# ── Asset Tag: QR, tag id, type, description, company ───────────────────────
def reportlab_available() -> bool:
	try:
		import reportlab  # noqa: F401
	except Exception:
		return False
	return True


def asset_pdf(asset: dict, qr_png: bytes, width_mm: float = 85.6, height_mm: float = 54.0) -> bytes:
	"""One card: a 30 mm QR with its quiet zone on the left, the facts on the right."""
	if not qr_png:
		raise ArtError("no QR encoder is installed on this server (segno). Nothing was queued.")
	try:
		from reportlab.lib.utils import ImageReader
		from reportlab.pdfbase.pdfmetrics import stringWidth
		from reportlab.pdfgen import canvas
	except Exception as exc:
		raise ArtError(
			f"reportlab is not installed on this server ({type(exc).__name__}), so the asset tag cannot "
			"be drawn. Nothing was queued."
		) from exc
	width, height = width_mm * MM, height_mm * MM
	buffer = io.BytesIO()
	pdf = canvas.Canvas(buffer, pagesize=(width, height))
	pdf.setTitle(f"Asset tag {asset.get('name')}")
	margin = 4 * MM
	qr_side = min(30 * MM, height - 2 * margin - 6 * MM)
	qr_y = height - margin - qr_side
	pdf.drawImage(ImageReader(io.BytesIO(qr_png)), margin, qr_y, qr_side, qr_side, mask=None)
	text_x = margin + qr_side + 4 * MM
	text_w = width - text_x - margin

	def fitted(text: str, font: str, size: float, floor: float) -> float:
		while size > floor and stringWidth(text, font, size) > text_w:
			size -= 0.5
		return size

	def clipped(text: str, font: str, size: float) -> str:
		text = str(text or "")
		while text and stringWidth(text, font, size) > text_w:
			text = text[:-2].rstrip() + "…" if len(text) > 2 else ""
		return text

	tag = str(asset.get("name") or "")
	size = fitted(tag, "Helvetica-Bold", 20, 9)
	y = height - margin - size
	pdf.setFont("Helvetica-Bold", size)
	pdf.drawString(text_x, y, clipped(tag, "Helvetica-Bold", size))
	y -= 6 * MM
	pdf.setFont("Helvetica", 10)
	pdf.drawString(text_x, y, clipped(asset.get("asset_type"), "Helvetica", 10))
	for line in _wrap(str(asset.get("description") or ""), "Helvetica", 8, text_w, stringWidth)[:3]:
		y -= 4 * MM
		pdf.setFont("Helvetica", 8)
		pdf.drawString(text_x, y, line)
	pdf.setFont("Helvetica-Bold", 8)
	pdf.drawString(margin, margin + 3.2 * MM, clipped(asset.get("company"), "Helvetica-Bold", 8))
	pdf.setFont("Helvetica", 6)
	pdf.drawString(margin, margin, "Scan with Farm Ops. If found, please return it.")
	pdf.showPage()
	pdf.save()
	return _checked(buffer.getvalue())


def _wrap(text: str, font: str, size: float, width: float, measure) -> list:
	lines, current = [], ""
	for word in text.split():
		trial = f"{current} {word}".strip()
		if measure(trial, font, size) <= width:
			current = trial
		else:
			if current:
				lines.append(current)
			current = word
	if current:
		lines.append(current)
	return lines


def _checked(pdf) -> bytes:
	if not pdf or not bytes(pdf).startswith(b"%PDF"):
		raise ArtError("the PDF renderer produced nothing. Nothing was queued.")
	if len(pdf) > MAX_BYTES:
		raise ArtError("the card artwork is over 2 MB (a very large photo?). Nothing was queued.")
	return bytes(pdf)


def page_sizes_mm(pdf: bytes) -> list:
	"""[(width_mm, height_mm)] per page, for tests and the audit. Needs pypdf."""
	from pypdf import PdfReader

	out = []
	for page in PdfReader(io.BytesIO(pdf)).pages:
		box = page.mediabox
		out.append((round(float(box.width) / MM, 1), round(float(box.height) / MM, 1)))
	return out
