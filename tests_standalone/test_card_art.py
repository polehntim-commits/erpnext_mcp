# SPDX-License-Identifier: MIT
"""The approved card artwork (v0.209.0). docs/design/card_print_queue.md, Amendment 1 §A2–A3.

`fixtures/card_art/` holds Tim's approved test PDFs and their geometry. These
tests render the same sample data and hold every page size and every text
item's position, font and size to them — so a later change to the layout is a
change somebody has to make on purpose.
"""

import json
import pathlib
import unittest

from erpnext_mcp import card_art
from erpnext_mcp.render import qr

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "card_art"
APPROVED = json.loads((FIXTURES / "approved_geometry.json").read_text())
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
#: The approved employee test carried a placeholder line under the initials; the real card does not.
PLACEHOLDER = "PHOTO 4:5 — replace"


def close(case, ours: list, theirs: list, tolerance: float = 0.5) -> None:
	theirs = [item for item in theirs if item["text"] != PLACEHOLDER]
	case.assertEqual([i["text"] for i in ours], [i["text"] for i in theirs])
	for mine, approved in zip(ours, theirs, strict=True):
		case.assertEqual((mine["font"], mine["size"]), (approved["font"], approved["size"]), mine["text"])
		case.assertAlmostEqual(mine["x"], approved["x"], delta=tolerance, msg=mine["text"])
		case.assertAlmostEqual(mine["y"], approved["y"], delta=tolerance, msg=mine["text"])


@NEEDS
class TheApprovedDesigns(unittest.TestCase):
	def employee(self):
		return card_art.employee_sides(
			{**EMPLOYEE, "qr": qr.qr_matrix("OML-0001", "H"), "photo": None, "logo": None}
		)

	def asset(self):
		return card_art.asset_sides(
			{**ASSET, "qr": qr.qr_matrix("https://farm.example/scan/40-WM-SE", "M"), "logo": None}
		)

	def test_the_employee_id_matches_the_approved_test(self):
		pdf = card_art.to_pdf(self.employee())
		approved = APPROVED["OML_EmployeeID_Test_OML-0001_portrait-back.pdf"]
		self.assertEqual([list(size) for size in card_art.page_sizes_mm(pdf)], approved["pages_mm"])
		for ours, theirs in zip(card_art.text_items(pdf), approved["text"], strict=True):
			close(self, ours, theirs)

	def test_the_asset_tag_matches_the_approved_test(self):
		pdf = card_art.to_pdf(self.asset())
		approved = APPROVED["OML_AssetTag_Test_40-WM-SE_A_standard.pdf"]
		self.assertEqual([list(size) for size in card_art.page_sizes_mm(pdf)], approved["pages_mm"])
		for ours, theirs in zip(card_art.text_items(pdf), approved["text"], strict=True):
			close(self, ours, theirs)

	def test_a_rotated_back_is_a_landscape_page_like_the_rot_variants(self):
		for name, rotation in (("back-rotCW", 90), ("back-rotCCW", 270)):
			pdf = card_art.to_pdf(self.asset(), [0, rotation])
			approved = APPROVED[f"OML_AssetTag_v2_40-WM-SE_A_standard_{name}.pdf"]
			self.assertEqual([list(size) for size in card_art.page_sizes_mm(pdf)], approved["pages_mm"], name)

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
