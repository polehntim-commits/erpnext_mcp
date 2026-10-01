# SPDX-License-Identifier: MIT
"""The approved card artwork (v0.209.0). docs/design/card_print_queue.md, Amendment 1 §A2–A3.

`fixtures/card_art/` holds the two PDFs Tim chose (Amendment 3). These
tests render the same sample data and hold every page size and every text
item's position, font and size to them — so a later change to the layout is a
change somebody has to make on purpose.
"""

import io
import pathlib
import re
import unittest

from erpnext_mcp import card_art
from erpnext_mcp.render import qr

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "card_art"
EMPLOYEE_PDF = FIXTURES / "OML_EmployeeID_Test_OML-0001_back-rotCW.pdf"
ASSET_PDF = FIXTURES / "OML_AssetTag_v2_40-WM-SE_A_standard_back-rotCW.pdf"
NEEDS = unittest.skipUnless(
	card_art.reportlab_available() and qr.available(), "needs reportlab, pypdf and a QR encoder"
)
EMPLOYEE = {
	"employee_name": "Tim Polehn",
	"designation": "Manager",
	"band": "Owner / Operator",
	"company": "Orchard Meadow, LLC",
	"badge_id": "OML-0001",
}
ASSET = {"asset_id": "40-WM-SE", "asset_name": "40-Acre Wind Machine", "company": "Orchard Meadow, LLC"}
#: The chosen employee test carried a placeholder line under the initials; the real card does not.
PLACEHOLDER = "PHOTO 4:5 — replace"
MM = 72 / 25.4
#: The chosen card: both pages landscape, the back turned clockwise (Amendment 3 §C1).
CHOSEN = [0, card_art.BACK_ORIENTATIONS[card_art.DEFAULT_BACK_ORIENTATION]]


def boxes(pdf: bytes) -> list:
	"""Per page: the QR's bounding box, then every other filled box smaller than the
	page — (x, y, w, h) in mm, in the page's own drawing coordinates."""
	from pypdf import PdfReader

	pages = []
	for page in PdfReader(io.BytesIO(pdf)).pages:
		content = page.get_contents().get_data().decode("latin1")
		rects = [
			tuple(float(n) / MM for n in found.groups())
			for found in re.finditer(r"([\d.-]+) ([\d.-]+) ([\d.-]+) ([\d.-]+) re", content)
		]
		modules = [r for r in rects if r[3] < 3.0]
		big = [tuple(round(n, 1) for n in r) for r in rects if 3.0 <= r[3] < 50.0]
		code = ()
		if modules:
			left = min(r[0] for r in modules)
			bottom = min(r[1] for r in modules)
			code = (
				round(left, 1),
				round(bottom, 1),
				round(max(r[0] + r[2] for r in modules) - left, 1),
				round(max(r[1] + r[3] for r in modules) - bottom, 1),
			)
		pages.append([code, *sorted(big)])
	return pages


def close(case, ours: list, theirs: list, tolerance: float = 0.5) -> None:
	theirs = [item for item in theirs if item["text"] != PLACEHOLDER]
	case.assertEqual([i["text"] for i in ours], [i["text"] for i in theirs])
	for mine, approved in zip(ours, theirs, strict=True):
		case.assertEqual((mine["font"], mine["size"]), (approved["font"], approved["size"]), mine["text"])
		case.assertAlmostEqual(mine["x"], approved["x"], delta=tolerance, msg=mine["text"])
		case.assertAlmostEqual(mine["y"], approved["y"], delta=tolerance, msg=mine["text"])


@NEEDS
class TheChosenDesigns(unittest.TestCase):
	def employee(self):
		return card_art.employee_sides(
			{**EMPLOYEE, "qr": qr.qr_matrix("OML-0001", "H"), "photo": None, "logo": None}
		)

	def asset(self):
		return card_art.asset_sides(
			{**ASSET, "qr": qr.qr_matrix("https://farm.example/scan/40-WM-SE", "M"), "logo": None}
		)

	def held_to(self, pdf: bytes, chosen: bytes, qr_size_only=False):
		self.assertEqual(card_art.page_sizes_mm(pdf), [(85.6, 54.0), (85.6, 54.0)])
		self.assertEqual(card_art.page_sizes_mm(pdf), card_art.page_sizes_mm(chosen))
		for ours, theirs in zip(card_art.text_items(pdf), card_art.text_items(chosen), strict=True):
			close(self, ours, theirs)
		for ours, theirs in zip(boxes(pdf), boxes(chosen), strict=True):
			self.assertEqual(len(ours), len(theirs))
			for mine, approved in zip(ours, theirs, strict=True):
				for a, b in zip(mine, approved, strict=True):
					self.assertAlmostEqual(a, b, delta=0.6, msg=f"{mine} vs {approved}")

	def test_the_employee_id_matches_the_chosen_file(self):
		"""Title Manager; the bar OWNER / over OPERATOR at 7.5 pt in a 9.5 mm bar."""
		pdf = card_art.to_pdf(self.employee(), CHOSEN)
		self.held_to(pdf, EMPLOYEE_PDF.read_bytes())
		front = boxes(pdf)[0]
		self.assertIn((5.0, 6.5, 22.0, 9.5), front)

	def test_the_asset_tag_matches_the_chosen_file(self):
		self.held_to(card_art.to_pdf(self.asset(), CHOSEN), ASSET_PDF.read_bytes())

	def test_the_back_is_turned_clockwise_onto_its_page(self):
		pdf = card_art.to_pdf(self.asset(), CHOSEN)
		from pypdf import PdfReader

		def turn(data):
			content = PdfReader(io.BytesIO(data)).pages[1].get_contents().get_data().decode("latin1")
			return re.search(r"0 -1 1 0 0 153\.07", content)

		self.assertTrue(turn(pdf))
		self.assertTrue(turn(ASSET_PDF.read_bytes()))

	def test_the_other_orientations_are_still_there(self):
		self.assertEqual(card_art.page_sizes_mm(card_art.to_pdf(self.asset())), [(85.6, 54.0), (54.0, 85.6)])
		self.assertEqual(
			card_art.page_sizes_mm(card_art.to_pdf(self.asset(), [0, 270])), [(85.6, 54.0), (85.6, 54.0)]
		)

	def test_the_front_can_be_rotated_too(self):
		pdf = card_art.to_pdf(self.employee(), [90, 0])
		self.assertEqual(card_art.page_sizes_mm(pdf), [(54.0, 85.6), (54.0, 85.6)])

	def test_the_qr_is_vector_and_no_raster_is_embedded_without_images(self):
		pdf = card_art.to_pdf(self.asset())
		self.assertNotIn(b"/Subtype /Image", pdf)
		self.assertLess(len(pdf), 60_000)

	def test_margins_are_kept(self):
		for sides in (self.employee(), self.asset()):
			for index, ops in enumerate(sides):
				width, height = card_art.page_size(index)
				for op in ops[1:]:
					if op[0] in ("qr", "image"):
						x, y, w = op[1], op[2], op[3]
						h = op[4] if op[0] == "image" else w
						self.assertGreaterEqual(min(x, y, width - x - w, height - y - h), 4.0, op[0])


class FittingAndFallbacks(unittest.TestCase):
	def test_long_text_shrinks_then_is_cut_and_never_overflows(self):
		text, size = card_art.fit(
			"Bartholomew Maximilian Featherstonehaugh-Cholmondeley III", "Helvetica-Bold", 12.5, 31.5, 8
		)
		self.assertEqual(size, 8)
		self.assertTrue(text.endswith("…"))
		self.assertLessEqual(card_art.text_width_mm(text, "Helvetica-Bold", size), 31.5)
		self.assertEqual(card_art.fit("Ana", "Helvetica", 10, 30), ("Ana", 10))

	def test_the_band_is_one_line_or_two(self):
		self.assertEqual(card_art.band_lines(""), [""])
		self.assertEqual(card_art.band_lines("Employee"), ["EMPLOYEE"])
		self.assertEqual(card_art.band_lines("Owner / Operator"), ["OWNER /", "OPERATOR"])
		self.assertEqual(card_art.band_lines("Seasonal"), ["SEASONAL"])
		self.assertEqual(card_art.band_lines("Field crew leader"), ["FIELD CREW", "LEADER"])

	def test_initials(self):
		self.assertEqual(card_art.initials("Tim Polehn"), "TP")
		self.assertEqual(card_art.initials("Ana María Ramos"), "AR")
		self.assertEqual(card_art.initials(""), "?")

	def test_a_small_logo_is_flagged(self):
		png = (
			b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (406).to_bytes(4, "big") + (404).to_bytes(4, "big")
		)
		self.assertIn("406 px wide", card_art.logo_warning(png))
		self.assertIn("not set", card_art.logo_warning(None))
		big = (
			b"\x89PNG\r\n\x1a\n"
			+ b"\x00\x00\x00\rIHDR"
			+ (1200).to_bytes(4, "big")
			+ (1200).to_bytes(4, "big")
		)
		self.assertEqual(card_art.logo_warning(big), "")

	def test_the_svg_is_the_same_drawing(self):
		sides = card_art.employee_sides({**EMPLOYEE, "qr": [[1, 0], [0, 1]], "photo": None, "logo": None})
		svg = card_art.to_svg(sides[0])
		for words in (
			'viewBox="0 0 85.6 54.0"',
			"Tim Polehn",
			"OWNER /",
			"OPERATOR",
			"OML-0001",
			card_art.GREEN,
		):
			self.assertIn(words, svg)
		self.assertIn('viewBox="0 0 85.6 54.0"', card_art.to_svg(sides[1], rotation=90))
