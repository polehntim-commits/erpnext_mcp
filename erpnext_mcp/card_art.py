# SPDX-License-Identifier: MIT
"""Card artwork: the approved designs, at the card's own size. v0.209.0.

docs/design/card_print_queue.md, Amendment 1 §A2–A3. ONE LAYOUT, TWO DRAWINGS.
A card side is a list of drawing operations in millimetres from the bottom-left
of the page (text by its baseline):

    ("rect",  x, y, w, h, fill, stroke)
    ("text",  x, y, text, font, size_pt, colour, align, max_width)
    ("image", x, y, w, h, bytes, mode)        mode: "contain" | "cover" | "circle"
    ("qr",    x, y, size, matrix)             vector modules, no quiet zone
    ("line",  x1, y1, x2, y2, colour)

`to_pdf` draws them with reportlab — a true card-sized page per side, which is
what the printer is sent. `to_svg` draws the SAME operations as an SVG string,
for the Desk preview and the print format. Neither knows what a badge is; the
two layout functions (`employee_sides`, `asset_sides`) hold the approved
geometry, measured off Tim's test files (tests_standalone/fixtures/card_art/).

Front: 85.6 × 54 mm landscape. Back: 54 × 85.6 mm portrait. A station may ask
for either side turned 90° (`ROTATIONS`), because the driver may need it.

Nothing here reads or writes the site.
"""

from __future__ import annotations

import base64
import html
import io

MM = 72.0 / 25.4
CARD_W, CARD_H = 85.6, 54.0
MAX_BYTES = 4 * 1024 * 1024
MIN_LOGO_PX = 600
GREEN = "#356B2E"
BLACK, GREY, LIGHT, MID = "#000000", "#555555", "#ECECEC", "#8A8A8A"

#: station setting → degrees clockwise.
FRONT_ORIENTATIONS = {"Landscape": 0, "Portrait, rotated CW": 90, "Portrait, rotated CCW": 270}
BACK_ORIENTATIONS = {"Portrait": 0, "Landscape, rotated CW": 90, "Landscape, rotated CCW": 270}


class ArtError(RuntimeError):
	"""The artwork could not be rendered. The message is for a person."""


def reportlab_available() -> bool:
	try:
		import reportlab  # noqa: F401
	except Exception:
		return False
	return True


# ── measuring and fitting text ──────────────────────────────────────────────
def text_width_mm(text: str, font: str, size: float) -> float:
	try:
		from reportlab.pdfbase.pdfmetrics import stringWidth

		return stringWidth(str(text), font, size) / MM
	except Exception:
		factor = 0.58 if "Bold" in font else 0.52
		return len(str(text)) * size * factor / MM


def fit(text, font: str, size: float, max_width: float, floor: float = 5.0) -> tuple:
	"""(text, size): shrunk to the floor, then cut with an ellipsis. Never overflows."""
	text = " ".join(str(text or "").split())
	if not max_width:
		return text, size
	while size > floor and text_width_mm(text, font, size) > max_width:
		size = round(size - 0.5, 2)
	while len(text) > 1 and text_width_mm(text, font, size) > max_width:
		text = text[:-2].rstrip() + "…"
	return text, size


def _text(x, y, text, font="Helvetica", size=8.0, colour=BLACK, align="left", max_width=0.0, floor=5.0):
	text, size = fit(text, font, size, max_width, floor)
	return ("text", x, y, text, font, size, colour, align, max_width)


def initials(name: str) -> str:
	words = [w for w in str(name or "").split() if w]
	if not words:
		return "?"
	return (words[0][:2] if len(words) == 1 else words[0][:1] + words[-1][:1]).upper()


def band_lines(label: str) -> list:
	"""The green band's text, upper-cased, on one line or two.

	"EMPLOYEE" fits one; "OWNER / OPERATOR" breaks after the slash, as approved.
	A label that fits at full size stays on one line."""
	text = " ".join(str(label or "").upper().split()) or "EMPLOYEE"
	if text_width_mm(text, "Helvetica-Bold", 7.5) <= 19.5:
		return [text]
	if " / " in text:
		first, rest = text.split(" / ", 1)
		return [first + " /", rest]
	words = text.split()
	if len(words) < 2:
		return [text]
	best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
	return [" ".join(words[:best]), " ".join(words[best:])]


# ── the two approved layouts ────────────────────────────────────────────────
def employee_sides(card: dict) -> list:
	"""[front ops (85.6×54), back ops (54×85.6)] for an employee ID.

	`card`: employee_name, designation, company, badge_id, band (the green
	bar's label; default EMPLOYEE), qr (matrix), photo (bytes or None), logo
	(bytes or None)."""
	name = card.get("employee_name") or ""
	company = card.get("company") or ""
	badge = card.get("badge_id") or ""
	front = [("rect", 0, 0, CARD_W, CARD_H, "#FFFFFF", None)]
	if card.get("photo"):
		front.append(("image", 4.9, 22.1, 21.6, 27.0, card["photo"], "cover"))
		front.append(("rect", 4.9, 22.1, 21.6, 27.0, None, "#AAAAAA"))
	else:
		front.append(("rect", 4.9, 22.1, 21.6, 27.0, LIGHT, "#AAAAAA"))
		front.append(_text(16.0, 32.0, initials(name), "Helvetica-Bold", 26, MID, "center", 20))
	band = band_lines(card.get("band") or "EMPLOYEE")
	if len(band) == 2:
		front.append(("rect", 4.9, 5.5, 21.6, 9.3, GREEN, None))
		front.append(_text(16.0, 11.9, band[0], "Helvetica-Bold", 7.5, "#FFFFFF", "center", 20.5, 5))
		front.append(_text(16.0, 8.7, band[1], "Helvetica-Bold", 7.5, "#FFFFFF", "center", 20.5, 5))
	else:
		front.append(("rect", 4.9, 9.8, 21.6, 5.9, GREEN, None))
		front.append(_text(16.0, 11.1, band[0], "Helvetica-Bold", 7.5, "#FFFFFF", "center", 20.5, 5))
	front.append(_text(30.0, 43.5, name, "Helvetica-Bold", 12.5, BLACK, "left", 31.5, 8))
	front.append(_text(30.0, 38.5, card.get("designation") or "", "Helvetica", 8.5, BLACK, "left", 31.5, 6))
	front.append(_text(30.0, 34.0, company, "Helvetica", 6.5, GREY, "left", 31.5, 5))
	front.append(_text(30.0, 17.5, "BADGE ID", "Helvetica", 5.5, GREY, "left", 30))
	front.append(_text(30.0, 11.2, badge, "Helvetica-Bold", 13, BLACK, "left", 30, 8))
	if card.get("logo"):
		front.append(("image", 63.0, 33.3, 15.0, 15.0, card["logo"], logo_mode(card["logo"])))
	if card.get("qr"):
		front.append(("qr", 61.4, 7.9, 15.8, card["qr"]))

	width, height = CARD_H, CARD_W
	back = [("rect", 0, 0, width, height, "#FFFFFF", None)]
	if card.get("qr"):
		back.append(("qr", (width - 33.5) / 2, height - 10.3 - 33.5, 33.5, card["qr"]))
	centre = width / 2
	back.append(_text(centre, 32.9, badge, "Helvetica-Bold", 17, BLACK, "center", width - 9, 9))
	back.append(_text(centre, 26.9, name, "Helvetica", 10, BLACK, "center", width - 9, 6))
	back.append(_text(centre, 21.9, company, "Helvetica", 7.5, GREY, "center", width - 9, 5))
	back.append(("line", 10.0, 14.0, width - 10.0, 14.0, "#BBBBBB"))
	back.append(_text(centre, 10.0, "If found, please return to", "Helvetica", 6, GREY, "center", width - 9))
	back.append(_text(centre, 7.0, company, "Helvetica", 6, GREY, "center", width - 9, 4.5))
	return [front, back]


def asset_sides(tag: dict) -> list:
	"""[front, back] for an asset tag. `tag`: asset_id, asset_name, company, qr, logo."""
	asset_id = tag.get("asset_id") or ""
	name = tag.get("asset_name") or ""
	front = [("rect", 0, 0, CARD_W, CARD_H, "#FFFFFF", None)]
	if tag.get("logo"):
		front.append(("image", 5.5, 7.5, 39.0, 39.0, tag["logo"], logo_mode(tag["logo"])))
	if tag.get("qr"):
		front.append(("qr", 53.0, 21.2, 25.6, tag["qr"]))
	front.append(_text(65.8, 14.4, asset_id, "Helvetica-Bold", 10, BLACK, "center", 31, 6))
	front.append(_text(65.8, 11.2, name, "Helvetica", 6.5, BLACK, "center", 31, 5))

	width, height = CARD_H, CARD_W
	centre = width / 2
	back = [("rect", 0, 0, width, height, "#FFFFFF", None)]
	if tag.get("qr"):
		back.append(("qr", (width - 34.0) / 2, height - 10.0 - 34.0, 34.0, tag["qr"]))
	back.append(_text(centre, 31.1, asset_id, "Helvetica-Bold", 16, BLACK, "center", width - 9, 8))
	back.append(_text(centre, 25.6, name, "Helvetica", 9, BLACK, "center", width - 9, 6))
	back.append(_text(centre, 16.6, tag.get("company") or "", "Helvetica", 7, GREY, "center", width - 9, 5))
	back.append(_text(centre, 12.6, "Scan for asset record", "Helvetica", 7, GREY, "center", width - 9))
	return [front, back]


def page_size(index: int) -> tuple:
	"""(width, height) in mm of side 0 (front) or 1 (back), as designed."""
	return (CARD_W, CARD_H) if index == 0 else (CARD_H, CARD_W)


# ── images ──────────────────────────────────────────────────────────────────
def image_size(data: bytes) -> tuple:
	"""(width_px, height_px), or (0, 0) when it cannot be read."""
	if not data:
		return 0, 0
	if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
		return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
	try:
		from PIL import Image

		with Image.open(io.BytesIO(data)) as image:
			return image.size
	except Exception:
		return 0, 0


def _mime(data: bytes) -> str:
	if data[:8] == b"\x89PNG\r\n\x1a\n":
		return "image/png"
	if data[:3] == b"\xff\xd8\xff":
		return "image/jpeg"
	if data[:4] == b"<svg" or b"<svg" in data[:200]:
		return "image/svg+xml"
	return "image/png"


def logo_warning(data, where: str = "the company's logo") -> str:
	if not data:
		return f"{where} is not set, so the card prints without one (Company › Badge Logo)."
	width, _height = image_size(data)
	if width and width < MIN_LOGO_PX:
		return (
			f"{where} is {width} px wide; it will look soft on a card — upload one at least "
			f"{MIN_LOGO_PX} px wide (Company › Badge Logo)."
		)
	return ""


def logo_mode(data: bytes) -> str:
	""" "circle" for a round mark saved on a coloured square (the approved tests
	showed Orchard Meadow's as a clean circle), else "contain". Needs Pillow to
	look at the corners; without it the logo is simply contained."""
	try:
		from PIL import Image

		with Image.open(io.BytesIO(data)) as image:
			width, height = image.size
			if not width or abs(width - height) / max(width, height) > 0.05:
				return "contain"
			rgba = image.convert("RGBA")
			corners = [
				rgba.getpixel(xy) for xy in ((1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2))
			]
	except Exception:
		return "contain"
	for red, green, blue, alpha in corners:
		if alpha < 16 or min(red, green, blue) > 244:
			return "contain"  # already transparent or white: nothing to clip away
	spread = max(max(c[i] for c in corners) - min(c[i] for c in corners) for i in range(3))
	return "circle" if spread < 24 else "contain"


def _placement(box_w, box_h, image_w, image_h, mode) -> tuple:
	"""(dx, dy, w, h) of an image inside its box, contained or covering."""
	if not (image_w and image_h):
		return 0, 0, box_w, box_h
	scale_x, scale_y = box_w / image_w, box_h / image_h
	scale = max(scale_x, scale_y) if mode in ("cover", "circle") else min(scale_x, scale_y)
	w, h = image_w * scale, image_h * scale
	return (box_w - w) / 2, (box_h - h) / 2, w, h


def _qr_runs(matrix: list):
	"""(row, start, length) for each horizontal run of dark modules."""
	for row_index, row in enumerate(matrix):
		start = None
		for col, module in enumerate([*row, 0]):
			if module and start is None:
				start = col
			elif not module and start is not None:
				yield row_index, start, col - start
				start = None


# ── PDF ─────────────────────────────────────────────────────────────────────
def to_pdf(sides: list, rotations: list | None = None, title: str = "Card") -> bytes:
	"""One page per side, each exactly its side's size (turned 90° where asked)."""
	try:
		from reportlab.lib.colors import HexColor
		from reportlab.lib.utils import ImageReader
		from reportlab.pdfgen import canvas
	except Exception as exc:
		raise ArtError(
			f"reportlab is not installed on this server ({type(exc).__name__}), so the card cannot be "
			"drawn. Nothing was queued."
		) from exc
	rotations = rotations or [0] * len(sides)
	buffer = io.BytesIO()
	pdf = None
	for ops, sized, rotation in zip(sides, _sizes(sides), rotations, strict=False):
		width, height = sized
		turned = rotation in (90, 270)
		page = ((height if turned else width) * MM, (width if turned else height) * MM)
		if pdf is None:
			pdf = canvas.Canvas(buffer, pagesize=page, pageCompression=1)
			pdf.setTitle(title)
		else:
			pdf.setPageSize(page)
		pdf.saveState()
		if rotation == 90:  # clockwise: the drawing's top goes to the page's right
			pdf.translate(0, width * MM)
			pdf.rotate(-90)
		elif rotation == 270:
			pdf.translate(height * MM, 0)
			pdf.rotate(90)
		for op in ops:
			_draw_pdf(pdf, op, HexColor, ImageReader)
		pdf.restoreState()
		pdf.showPage()
	if pdf is None:
		raise ArtError("there was nothing to draw. Nothing was queued.")
	pdf.save()
	data = buffer.getvalue()
	if not data.startswith(b"%PDF"):
		raise ArtError("the PDF renderer produced nothing. Nothing was queued.")
	if len(data) > MAX_BYTES:
		raise ArtError("the card artwork is over 4 MB (a very large photo or logo?). Nothing was queued.")
	return data


def _sizes(sides: list) -> list:
	"""Each side's own size, read off its background rect."""
	out = []
	for ops in sides:
		first = ops[0]
		out.append((first[3], first[4]) if first[0] == "rect" else (CARD_W, CARD_H))
	return out


def _draw_pdf(pdf, op, colour, image_reader) -> None:
	kind = op[0]
	if kind == "rect":
		_k, x, y, w, h, fill, stroke = op
		if fill:
			pdf.setFillColor(colour(fill))
		if stroke:
			pdf.setStrokeColor(colour(stroke))
			pdf.setLineWidth(0.4)
		pdf.rect(x * MM, y * MM, w * MM, h * MM, stroke=1 if stroke else 0, fill=1 if fill else 0)
	elif kind == "text":
		_k, x, y, text, font, size, col, align, _max = op
		pdf.setFillColor(colour(col))
		pdf.setFont(font, size)
		(pdf.drawCentredString if align == "center" else pdf.drawString)(x * MM, y * MM, text)
	elif kind == "line":
		_k, x1, y1, x2, y2, col = op
		pdf.setStrokeColor(colour(col))
		pdf.setLineWidth(0.4)
		pdf.line(x1 * MM, y1 * MM, x2 * MM, y2 * MM)
	elif kind == "qr":
		_k, x, y, size, matrix = op
		count = len(matrix)
		if not count:
			return
		module = size / count
		pdf.setFillColor(colour(BLACK))
		for row, start, length in _qr_runs(matrix):
			# +0.02 mm of overlap so adjacent runs never show a hairline.
			pdf.rect(
				(x + start * module) * MM,
				(y + size - (row + 1) * module) * MM,
				(length * module + 0.02) * MM,
				(module + 0.02) * MM,
				stroke=0,
				fill=1,
			)
	elif kind == "image":
		_k, x, y, w, h, data, mode = op
		try:
			reader = image_reader(io.BytesIO(data))
			image_w, image_h = reader.getSize()
		except Exception:
			return  # an unreadable picture must not lose the card
		dx, dy, draw_w, draw_h = _placement(w, h, image_w, image_h, mode)
		pdf.saveState()
		clip = pdf.beginPath()
		if mode == "circle":
			clip.circle((x + w / 2) * MM, (y + h / 2) * MM, min(w, h) / 2 * MM)
		else:
			clip.rect(x * MM, y * MM, w * MM, h * MM)
		pdf.clipPath(clip, stroke=0, fill=0)
		pdf.drawImage(reader, (x + dx) * MM, (y + dy) * MM, draw_w * MM, draw_h * MM, mask="auto")
		pdf.restoreState()


# ── SVG (the Desk preview and the print format) ─────────────────────────────
_FONTS = {
	"Helvetica": ("Helvetica, Arial, sans-serif", "normal"),
	"Helvetica-Bold": ("Helvetica, Arial, sans-serif", "bold"),
}


def to_svg(ops: list, rotation: int = 0, css_width_mm: float | None = None) -> str:
	"""One side as an SVG string, in millimetres. `rotation` as `to_pdf`."""
	width, height = _sizes([ops])[0]
	turned = rotation in (90, 270)
	out_w, out_h = (height, width) if turned else (width, height)
	shown = css_width_mm or out_w
	parts = [
		f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {out_w} {out_h}" '
		f'width="{shown}mm" height="{round(shown * out_h / out_w, 3)}mm">'
	]
	if rotation == 90:
		parts.append(f'<g transform="translate({out_w} 0) rotate(90)">')
	elif rotation == 270:
		parts.append(f'<g transform="translate(0 {out_h}) rotate(-90)">')
	else:
		parts.append("<g>")
	for index, op in enumerate(ops):
		parts.append(_draw_svg(op, height, index))
	parts.append("</g></svg>")
	return "".join(parts)


def _draw_svg(op, page_h: float, index: int) -> str:
	kind = op[0]
	if kind == "rect":
		_k, x, y, w, h, fill, stroke = op
		return (
			f'<rect x="{x}" y="{round(page_h - y - h, 3)}" width="{w}" height="{h}" '
			f'fill="{fill or "none"}" stroke="{stroke or "none"}" stroke-width="0.15"/>'
		)
	if kind == "text":
		_k, x, y, text, font, size, col, align, _max = op
		family, weight = _FONTS.get(font, _FONTS["Helvetica"])
		anchor = "middle" if align == "center" else "start"
		return (
			f'<text x="{x}" y="{round(page_h - y, 3)}" font-family="{family}" font-weight="{weight}" '
			f'font-size="{round(size / MM, 3)}" fill="{col}" text-anchor="{anchor}">{html.escape(str(text))}</text>'
		)
	if kind == "line":
		_k, x1, y1, x2, y2, col = op
		return (
			f'<line x1="{x1}" y1="{round(page_h - y1, 3)}" x2="{x2}" y2="{round(page_h - y2, 3)}" '
			f'stroke="{col}" stroke-width="0.15"/>'
		)
	if kind == "qr":
		_k, x, y, size, matrix = op
		count = len(matrix)
		if not count:
			return ""
		module = size / count
		top = page_h - y - size
		path = "".join(
			f"M{round(x + start * module, 3)} {round(top + row * module, 3)}h{round(length * module, 3)}"
			f"v{round(module, 3)}h-{round(length * module, 3)}z"
			for row, start, length in _qr_runs(matrix)
		)
		return f'<path d="{path}" fill="{BLACK}"/>'
	if kind == "image":
		_k, x, y, w, h, data, mode = op
		image_w, image_h = image_size(data)
		dx, dy, draw_w, draw_h = _placement(w, h, image_w, image_h, mode)
		top = page_h - y - h
		clip = f"c{index}"
		uri = f"data:{_mime(data)};base64,{base64.b64encode(data).decode()}"
		shape = (
			f'<circle cx="{round(x + w / 2, 3)}" cy="{round(top + h / 2, 3)}" r="{round(min(w, h) / 2, 3)}"/>'
			if mode == "circle"
			else f'<rect x="{x}" y="{round(top, 3)}" width="{w}" height="{h}"/>'
		)
		return (
			f'<clipPath id="{clip}">{shape}</clipPath>'
			f'<image x="{round(x + dx, 3)}" y="{round(top + dy, 3)}" width="{round(draw_w, 3)}" '
			f'height="{round(draw_h, 3)}" preserveAspectRatio="none" clip-path="url(#{clip})" href="{uri}"/>'
		)
	return ""


# ── reading a PDF back (tests, and the golden comparison) ───────────────────
def page_sizes_mm(pdf: bytes) -> list:
	from pypdf import PdfReader

	out = []
	for page in PdfReader(io.BytesIO(pdf)).pages:
		box = page.mediabox
		out.append((round(float(box.width) / MM, 1), round(float(box.height) / MM, 1)))
	return out


def text_items(pdf: bytes) -> list:
	"""[[{text, x, y, size, font}] per page], positions in mm. Needs pypdf."""
	from pypdf import PdfReader

	pages = []
	for page in PdfReader(io.BytesIO(pdf)).pages:
		items: list = []

		def visit(text, cm, tm, font, size, _items=items):
			if str(text).strip():
				_items.append(
					{
						"text": str(text).strip(),
						"x": round(tm[4] / MM, 1),
						"y": round(tm[5] / MM, 1),
						"size": round(float(size), 1),
						"font": str((font or {}).get("/BaseFont", "")).lstrip("/"),
					}
				)

		page.extract_text(visitor_text=visit)
		pages.append(items)
	return pages
