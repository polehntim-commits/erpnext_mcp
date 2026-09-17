# SPDX-License-Identifier: MIT
"""Bearings, distances, closure, easement crossings and the draft description.

THE NUMBERS ARE CHECKED AGAINST VALUES THIS FILE DOES NOT COMPUTE. A geodesy
module tested against its own arithmetic is a module that agrees with itself, so
the distances below are the published WGS84 figures for a degree of latitude and
a degree of longitude at the equator, and the areas are checked against
`geo.area_acres`, which has been the app's one area function since v0.12.0.

SIX CLAIMS.

1. `TheInverse` — azimuth and distance, against known ellipsoid figures.
2. `TheQuadrantBearing` — what a deed prints, including the rounding carry that
   otherwise produces `59'60"`.
3. `TheFigure` — courses close the loop, area comes from `geo`, closure is
   reported rather than fixed.
4. `TheDescription` — it reads like a deed and it always carries its disclaimer.
5. `ThePointOfBeginning` — tied to a mapped corner, and honest when there is none.
6. `TheEasementCheck` — crossing, containing, and the negative control.
"""

import math
import unittest

from erpnext_mcp import geo, surveying

#: A square of about 200 m a side near The Dalles, Oregon — where the farm this
#: was written for is. Latitude matters: a degree of longitude is shorter here
#: than at the equator, which is exactly the kind of thing a flat-earth bearing
#: calculation gets wrong.
LAT = 45.6
LON = -121.17
DEGREE_LAT = 200.0 / 111320.0
DEGREE_LON = 200.0 / (111320.0 * math.cos(math.radians(LAT)))
SQUARE = [
	[LON, LAT],
	[LON + DEGREE_LON, LAT],
	[LON + DEGREE_LON, LAT + DEGREE_LAT],
	[LON, LAT + DEGREE_LAT],
]


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheInverse(unittest.TestCase):
	def test_a_degree_of_latitude_on_the_equator(self):
		"""110,574 m is the published WGS84 figure. Not computed here."""
		answer = surveying.inverse([0.0, 0.0], [0.0, 1.0])
		self.assertAlmostEqual(answer["distance_m"], 110574.0, delta=2.0)
		self.assertEqual(answer["method"], "vincenty")

	def test_a_degree_of_longitude_on_the_equator(self):
		"""111,319.49 m, and due east."""
		answer = surveying.inverse([0.0, 0.0], [1.0, 0.0])
		self.assertAlmostEqual(answer["distance_m"], 111319.49, delta=2.0)
		self.assertAlmostEqual(answer["azimuth_deg"], 90.0, places=6)

	def test_feet_are_international_feet(self):
		answer = surveying.inverse([0.0, 0.0], [0.0, 1.0])
		self.assertAlmostEqual(answer["distance_ft"], answer["distance_m"] / 0.3048, places=6)
		self.assertEqual(surveying.METRES_PER_FOOT, 0.3048)

	def test_due_north_is_zero_and_due_south_is_one_eighty(self):
		self.assertAlmostEqual(surveying.inverse([LON, LAT], [LON, LAT + 0.01])["azimuth_deg"], 0.0, places=5)
		self.assertAlmostEqual(
			surveying.inverse([LON, LAT], [LON, LAT - 0.01])["azimuth_deg"], 180.0, places=5
		)

	def test_coincident_points_are_named_rather_than_divided_by(self):
		answer = surveying.inverse([LON, LAT], [LON, LAT])
		self.assertEqual((answer["distance_m"], answer["method"]), (0.0, "coincident"))

	def test_the_reverse_course_is_the_same_length(self):
		there = surveying.inverse([LON, LAT], [LON + DEGREE_LON, LAT + DEGREE_LAT])
		back = surveying.inverse([LON + DEGREE_LON, LAT + DEGREE_LAT], [LON, LAT])
		self.assertAlmostEqual(there["distance_m"], back["distance_m"], places=6)


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheQuadrantBearing(unittest.TestCase):
	def test_the_four_quadrants(self):
		self.assertEqual(surveying.quadrant(45.0)["text"], "N 45°00'00\" E")
		self.assertEqual(surveying.quadrant(135.0)["text"], "S 45°00'00\" E")
		self.assertEqual(surveying.quadrant(225.0)["text"], "S 45°00'00\" W")
		self.assertEqual(surveying.quadrant(315.0)["text"], "N 45°00'00\" W")

	def test_minutes_and_seconds(self):
		self.assertEqual(surveying.quadrant(45.504166666)["text"], "N 45°30'15\" E")

	def test_the_cardinals_are_words_and_not_zero_bearings(self):
		for azimuth, word in (
			(0.0, "due North"),
			(90.0, "due East"),
			(180.0, "due South"),
			(270.0, "due West"),
		):
			with self.subTest(azimuth=azimuth):
				self.assertEqual(surveying.quadrant(azimuth)["text"], word)

	def test_the_rounding_carries_instead_of_printing_sixty(self):
		"""THE BUG THIS EXISTS TO STOP. Rounding degrees, minutes and seconds
		separately turns 45°59'59.7" into 45°59'60", which is not an angle."""
		text = surveying.quadrant(45.99999167)["text"]
		self.assertNotIn('60"', text)
		self.assertEqual(text, "N 46°00'00\" E")

	def test_a_bearing_that_rounds_onto_a_cardinal_is_the_cardinal(self):
		self.assertEqual(surveying.quadrant(89.99999999)["text"], "due East")

	def test_the_parts_agree_with_the_text(self):
		answer = surveying.quadrant(45.504166666)
		self.assertEqual((answer["degrees"], answer["minutes"], answer["seconds"]), (45, 30, 15))
		self.assertEqual(answer["quadrant"], "NE")


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheFigure(unittest.TestCase):
	def test_a_square_has_four_courses_and_the_last_one_closes(self):
		rows = surveying.courses(SQUARE)
		self.assertEqual(len(rows), 4)
		self.assertTrue(rows[-1]["closing"])
		self.assertFalse(any(row["closing"] for row in rows[:-1]))

	def test_each_side_is_about_two_hundred_metres(self):
		for row in surveying.courses(SQUARE):
			with self.subTest(course=row["index"]):
				self.assertAlmostEqual(row["distance_m"], 200.0, delta=1.0)

	def test_a_line_is_not_closed(self):
		rows = surveying.courses([[LON, LAT], [LON + DEGREE_LON, LAT]])
		self.assertEqual(len(rows), 1)
		self.assertFalse(rows[0]["closing"])

	def test_the_area_is_geos_area_and_not_a_second_implementation(self):
		self.assertEqual(surveying.acres(SQUARE), geo.area_acres(surveying.polygon(SQUARE)))

	def test_the_area_is_about_four_hectares(self):
		self.assertAlmostEqual(surveying.acres(SQUARE), (200.0 * 200.0) / 4046.8564224, delta=0.05)

	def test_a_closing_vertex_is_not_counted_twice(self):
		closed = [*SQUARE, SQUARE[0]]
		self.assertEqual(len(surveying.courses(closed)), len(surveying.courses(SQUARE)))

	def test_the_polygon_is_closed_geojson(self):
		shape = surveying.polygon(SQUARE)
		self.assertEqual(shape["type"], "Polygon")
		self.assertEqual(shape["coordinates"][0][0], shape["coordinates"][0][-1])

	def test_two_points_enclose_nothing(self):
		self.assertIsNone(surveying.polygon([[LON, LAT], [LON, LAT + 0.001]]))
		self.assertEqual(surveying.acres([[LON, LAT], [LON, LAT + 0.001]]), 0.0)

	def test_closure_on_a_drawn_square_is_under_an_inch(self):
		answer = surveying.closure(SQUARE)
		self.assertLess(answer["error_ft"], 0.1)
		self.assertGreater(answer["precision"], 10000)
		self.assertIn("1 part in", answer["precision_text"])

	def test_closure_is_reported_and_not_corrected(self):
		"""A traverse drawn by clicking does not close, and the figure that says
		so is the one a surveyor checks. Nothing here moves a point."""
		sloppy = list(SQUARE)
		sloppy[-1] = [LON + 0.0004, LAT + DEGREE_LAT]
		answer = surveying.closure(sloppy)
		self.assertGreater(answer["error_ft"], 0.0)
		self.assertEqual(surveying.courses(sloppy)[0]["from"], sloppy[0])


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheDescription(unittest.TestCase):
	def text(self, **kwargs) -> str:
		return surveying.legal_description(SQUARE, **kwargs)["text"]

	def test_it_reads_like_a_deed(self):
		text = self.text(beginning="Beginning at the northwest corner of Tax Lot 200")
		self.assertIn("Beginning at the northwest corner of Tax Lot 200;", text)
		self.assertIn("thence", text)
		self.assertIn("to the point of beginning.", text)

	def test_it_states_the_acreage(self):
		self.assertIn("Containing 9.88 acres, more or less.", self.text())

	def test_every_course_is_in_the_text(self):
		answer = surveying.legal_description(SQUARE)
		for row in answer["courses"]:
			with self.subTest(course=row["index"]):
				self.assertIn(row["bearing"], answer["text"])

	def test_it_always_ends_with_the_disclaimer(self):
		"""THE DISCLAIMER IS PART OF THE VALUE, NOT A DECORATION. A draft that
		loses it on the way to a printer reads as a survey."""
		self.assertTrue(self.text().endswith(surveying.DISCLAIMER))
		self.assertTrue(self.text(note="For LLA-2026-0001.").endswith(surveying.DISCLAIMER))

	def test_the_disclaimer_says_it_is_not_a_survey(self):
		for phrase in ("DRAFT", "not a survey", "TRUE NORTH", "international feet"):
			with self.subTest(phrase=phrase):
				self.assertIn(phrase, surveying.DISCLAIMER)

	def test_a_note_is_carried_between_the_acreage_and_the_disclaimer(self):
		text = self.text(note="Drafted for LLA-2026-0001.")
		self.assertIn("Drafted for LLA-2026-0001.", text)
		self.assertLess(text.index("Drafted for"), text.index(surveying.DISCLAIMER))

	def test_two_points_get_a_reason_rather_than_a_description(self):
		answer = surveying.legal_description([[LON, LAT], [LON, LAT + 0.001]])
		self.assertEqual(answer["text"], "")
		self.assertIn("at least three courses", answer["note"])


# ── 5 ───────────────────────────────────────────────────────────────────────
class ThePointOfBeginning(unittest.TestCase):
	def references(self) -> list:
		return [{"label": "Tax Lot 200", "geometry": surveying.polygon(SQUARE)}]

	def test_a_corner_is_named_for_where_it_sits_in_its_own_shape(self):
		labels = sorted(corner["label"] for corner in surveying.corners_of(surveying.polygon(SQUARE)))
		self.assertEqual(labels, ["northeast", "northwest", "southeast", "southwest"])

	def test_starting_on_a_corner_begins_there(self):
		answer = surveying.tie_in(SQUARE[0], self.references())
		self.assertTrue(answer["found"])
		self.assertEqual(answer["description"], "Beginning at the southwest corner of Tax Lot 200")

	def test_starting_away_from_a_corner_commences_at_one(self):
		point = [LON + DEGREE_LON / 2, LAT]
		answer = surveying.tie_in(point, self.references())
		self.assertTrue(answer["description"].startswith("Commencing at the s"))
		self.assertIn("to the point of beginning", answer["description"])
		self.assertAlmostEqual(answer["distance_ft"], 100.0 / 0.3048, delta=2.0)

	def test_the_nearest_corner_wins_across_several_references(self):
		far = [[LON + 1, LAT + 1], [LON + 1.001, LAT + 1], [LON + 1.001, LAT + 1.001]]
		answer = surveying.tie_in(
			SQUARE[2],
			[{"label": "Far Lot", "geometry": surveying.polygon(far)}, *self.references()],
		)
		self.assertEqual(answer["reference"], "Tax Lot 200")
		self.assertEqual(answer["corner"]["label"], "northeast")

	def test_nothing_to_tie_to_is_said_rather_than_invented(self):
		"""No PLSS layer ships with this app, so a description that named a
		section corner would be an invention. See the module docstring."""
		answer = surveying.tie_in(SQUARE[0], [])
		self.assertFalse(answer["found"])
		self.assertEqual(answer["description"], "")
		self.assertIn("in your own words", answer["note"])


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheEasementCheck(unittest.TestCase):
	def ditch(self, coordinates) -> dict:
		return {"label": "Ditch", "geometry": {"type": "LineString", "coordinates": coordinates}}

	def test_a_corridor_across_the_figure_is_reported(self):
		crossing = surveying.crossings(
			SQUARE,
			[
				self.ditch(
					[[LON - 0.001, LAT + DEGREE_LAT / 2], [LON + DEGREE_LON + 0.001, LAT + DEGREE_LAT / 2]]
				)
			],
		)
		self.assertEqual(len(crossing), 1)
		self.assertEqual(crossing[0]["how"], "crosses")

	def test_a_corridor_somewhere_else_is_not_reported(self):
		"""THE NEGATIVE CONTROL. A checker that flags everything is a checker
		nobody reads."""
		self.assertEqual(
			surveying.crossings(SQUARE, [self.ditch([[LON + 1, LAT + 1], [LON + 1.001, LAT + 1.001]])]),
			[],
		)

	def test_a_figure_drawn_inside_a_corridor_is_reported_as_inside(self):
		corridor = {
			"label": "Access easement",
			"geometry": {
				"type": "Polygon",
				"coordinates": [
					[
						[LON - 0.01, LAT - 0.01],
						[LON + 0.01, LAT - 0.01],
						[LON + 0.01, LAT + 0.01],
						[LON - 0.01, LAT + 0.01],
						[LON - 0.01, LAT - 0.01],
					]
				],
			},
		}
		answer = surveying.crossings(SQUARE, [corridor])
		self.assertEqual([row["how"] for row in answer], ["inside"])

	def test_the_row_carries_what_the_page_prints(self):
		corridor = self.ditch(
			[[LON - 0.001, LAT + DEGREE_LAT / 2], [LON + DEGREE_LON + 0.001, LAT + DEGREE_LAT / 2]]
		)
		corridor.update({"easement_type": "Irrigation", "burdened": "Party 1", "benefited": "Party 2"})
		row = surveying.crossings(SQUARE, [corridor])[0]
		self.assertEqual(
			(row["label"], row["easement_type"], row["burdened"], row["benefited"]),
			("Ditch", "Irrigation", "Party 1", "Party 2"),
		)

	def test_segments_that_touch_at_a_point_count_as_crossing(self):
		"""A line ending exactly on a ditch is on the ditch."""
		self.assertTrue(surveying.segments_cross([0, 0], [1, 1], [1, 1], [2, 0]))

	def test_segments_that_miss_do_not(self):
		self.assertFalse(surveying.segments_cross([0, 0], [1, 0], [0, 1], [1, 1]))

	def test_an_easement_with_no_geometry_is_skipped_rather_than_guessed_at(self):
		self.assertEqual(surveying.crossings(SQUARE, [{"label": "Unmapped", "geometry": None}]), [])
