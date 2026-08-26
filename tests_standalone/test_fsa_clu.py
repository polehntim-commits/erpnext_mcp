# SPDX-License-Identifier: MIT
"""The FSA county office's own copy of the farm's boundaries, read without a GIS.

────────────────────────────────────────────────────────────────────────────
WHY THE FIXTURES ARE REAL BYTES AND NOT MOCKS
────────────────────────────────────────────────────────────────────────────

`fsa.py` exists to read a 1998 binary format and a 1983 database format that
nobody in this codebase can look at. A test that handed it a dict would be
testing the part that was never in question. So the helpers below WRITE A REAL
SHAPEFILE SET — a `.shp` with its 100-byte header and big-endian record lengths,
a `.dbf` with 32-byte field descriptors and space-padded fixed-width rows, and a
`.prj` of ESRI Well-Known Text — and the tests read that back. Every one of them
would fail on an off-by-one in a struct format string, which is the failure this
module is actually exposed to.

The WKT and the Albers coordinates below were produced with `pyproj` against
EPSG:5070 and pasted in as literals. `pyproj` is not a dependency of this app and
is not installed by CI; what it was used for is generating a known answer once,
the same way a tide table is not recomputed by the boat.

────────────────────────────────────────────────────────────────────────────
THE FOUR THINGS THAT WOULD SHIP BROKEN AND LOOK FINE
────────────────────────────────────────────────────────────────────────────

REPROJECTION. A file in metres read as degrees produces a polygon "not on
Earth", which at least fails loudly. A file in US SURVEY FEET whose false
easting is subtracted in metres produces a polygon that is perfectly valid,
perfectly closed, and eleven hundred kilometres from the farm. There is a test
for the feet case specifically, because it is the one that does not announce
itself.

RING NESTING. A shapefile record is a flat list of rings and the specification's
rule for which is a hole is WINDING, which half the tools that write shapefiles
get wrong. A ten-acre field with a wetland exclusion in the middle then comes
back as two polygons totalling twelve acres — a number nobody would question.
Nesting here is decided by containment, and there are tests in both windings.

IDENTIFIER NORMALISATION. `1234.0` out of a dBase numeric column, `"0012"` out of
a text one and `12` out of JSON are the same tract. If they do not normalise to
one string then next season's file matches nothing and creates a second copy of
the farm — which is not an error, it is forty new blocks that look right.

THE COLUMN NAMES. FSA has spelled the same four columns at least three ways
since 2008 and dBase truncates the long forms at ten characters. Matching one
spelling produces an import that reports every field as unmatched, which reads
like the farm's data being wrong rather than like this app not looking properly.
"""

import base64
import io
import json
import struct
import unittest
import zipfile

from erpnext_mcp import fsa, geo

from .fixtures import MAIN, V12TestCase

# ── writing a shapefile set, so the reader has real bytes to read ───────────

#: The block on Dry Hollow Road every geometry test in this app uses, in
#: [longitude, latitude] and wound counter-clockwise.
BLOCK_RING = [
	(-121.1800, 45.6000),
	(-121.1760, 45.6000),
	(-121.1760, 45.6030),
	(-121.1800, 45.6030),
	(-121.1800, 45.6000),
]

#: The same five corners in EPSG:5070 metres, from `pyproj`. The reader has to
#: get back to `BLOCK_RING` from these, which is the whole reprojection claim
#: made checkable.
BLOCK_RING_ALBERS = [
	(-1942163.5547, 2771252.5959),
	(-1941862.2820, 2771170.8555),
	(-1941775.0043, 2771492.5639),
	(-1942076.2634, 2771574.3006),
	(-1942163.5547, 2771252.5959),
]

#: A second block, east of the first and not touching it.
OTHER_RING = [
	(-121.1750, 45.6000),
	(-121.1710, 45.6000),
	(-121.1710, 45.6030),
	(-121.1750, 45.6030),
	(-121.1750, 45.6000),
]

#: A hole inside `BLOCK_RING` — the wetland exclusion, about a tenth of it.
HOLE_RING = [
	(-121.1790, 45.6010),
	(-121.1780, 45.6010),
	(-121.1780, 45.6020),
	(-121.1790, 45.6020),
	(-121.1790, 45.6010),
]

#: What ArcGIS writes into a `.prj` for EPSG:5070, verbatim.
ALBERS_WKT = (
	'PROJCS["NAD_1983_Contiguous_USA_Albers",GEOGCS["GCS_North_American_1983",'
	'DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,298.257222101]],'
	'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Albers"],'
	'PARAMETER["False_Easting",0.0],PARAMETER["False_Northing",0.0],'
	'PARAMETER["Central_Meridian",-96.0],PARAMETER["Standard_Parallel_1",29.5],'
	'PARAMETER["Standard_Parallel_2",45.5],PARAMETER["Latitude_Of_Origin",23.0],'
	'UNIT["Meter",1.0]]'
)

WGS84_WKT = (
	'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
	'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)

NAD83_WKT = (
	'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
	'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
	'UNIT["Degree",0.0174532925199433]]'
)

#: Washington North in US survey feet — a Lambert Conformal Conic whose false
#: easting is 1,640,416.667 FEET. Getting the unit wrong here is the failure that
#: does not announce itself.
WASHINGTON_FEET_WKT = (
	'PROJCS["NAD_1983_StatePlane_Washington_North_FIPS_4601_Feet",'
	'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
	'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
	'UNIT["Degree",0.0174532925199433]],PROJECTION["Lambert_Conformal_Conic"],'
	'PARAMETER["False_Easting",1640416.667],PARAMETER["False_Northing",0.0],'
	'PARAMETER["Central_Meridian",-120.833333333333],'
	'PARAMETER["Standard_Parallel_1",48.7333333333333],'
	'PARAMETER["Standard_Parallel_2",47.5],PARAMETER["Latitude_Of_Origin",47.0],'
	'UNIT["US survey foot",0.304800609601219]]'
)

#: A block near Ephrata in Washington North, US survey feet, from `pyproj`, and
#: where those five corners actually are.
WASHINGTON_FEET_RING = [
	(1970618.3801, 148769.3131),
	(1971608.8856, 148786.5018),
	(1971589.8701, 149880.6344),
	(1970599.4214, 149863.4466),
	(1970618.3801, 148769.3131),
]
WASHINGTON_DEGREES_RING = [
	(-119.5000, 47.4000),
	(-119.4960, 47.4000),
	(-119.4960, 47.4030),
	(-119.5000, 47.4030),
	(-119.5000, 47.4000),
]

#: An Oblique Mercator — a real projection, correctly written, that this module
#: does not know how to invert. The refusal has to name it.
UNKNOWN_WKT = (
	'PROJCS["Somebody_Elses_Grid",GEOGCS["GCS_North_American_1983",'
	'DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,298.257222101]],'
	'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
	'PROJECTION["Hotine_Oblique_Mercator_Azimuth_Natural_Origin"],'
	'PARAMETER["False_Easting",0.0],UNIT["Meter",1.0]]'
)


def write_shp(shapes: list) -> bytes:
	"""A `.shp` holding polygon records. `shapes` is a list of lists of rings."""
	body = b""
	west = south = 1e30
	east = north = -1e30
	for number, rings in enumerate(shapes, start=1):
		points = [point for ring in rings for point in ring]
		if not points:
			# Shape type 0, the null shape. The .dbf still has a row for it, and
			# the two files are matched by position.
			body += struct.pack(">ii", number, 2) + struct.pack("<i", 0)
			continue
		for x, y in points:
			west, south = min(west, x), min(south, y)
			east, north = max(east, x), max(north, y)
		content = struct.pack("<i", 5)
		content += struct.pack(
			"<4d",
			min(x for x, _ in points),
			min(y for _, y in points),
			max(x for x, _ in points),
			max(y for _, y in points),
		)
		content += struct.pack("<ii", len(rings), len(points))
		start = 0
		starts = []
		for ring in rings:
			starts.append(start)
			start += len(ring)
		content += struct.pack(f"<{len(starts)}i", *starts)
		for x, y in points:
			content += struct.pack("<2d", x, y)
		body += struct.pack(">ii", number, len(content) // 2) + content

	if west > east:
		west = south = east = north = 0.0
	header = struct.pack(">i", 9994) + b"\x00" * 20
	header += struct.pack(">i", (100 + len(body)) // 2)
	header += struct.pack("<ii", 1000, 5)
	header += struct.pack("<4d", west, south, east, north)
	header += struct.pack("<4d", 0.0, 0.0, 0.0, 0.0)
	return header + body


def write_dbf(columns: list, rows: list) -> bytes:
	"""A dBase III table. `columns` is `[(name, kind, width, decimals)]`."""
	record_bytes = 1 + sum(column[2] for column in columns)
	header = struct.pack("<4B", 0x03, 26, 8, 26)
	header += struct.pack("<i", len(rows))
	header += struct.pack("<HH", 32 + 32 * len(columns) + 1, record_bytes)
	header += b"\x00" * 20
	for name, kind, width, decimals in columns:
		header += name.encode("ascii")[:11].ljust(11, b"\x00")
		header += kind.encode("ascii")
		header += b"\x00" * 4
		header += struct.pack("<BB", width, decimals)
		header += b"\x00" * 14
	header += b"\x0d"

	body = b""
	for row in rows:
		body += b"*" if row.get("__deleted__") else b" "
		for name, kind, width, decimals in columns:
			value = row.get(name)
			if value is None:
				body += b" " * width
				continue
			if kind in ("N", "F"):
				text = f"{float(value):.{decimals}f}" if decimals else str(int(value))
				body += text.encode("ascii")[:width].rjust(width, b" ")
			else:
				body += str(value).encode("ascii", "replace")[:width].ljust(width, b" ")
	return header + body + b"\x1a"


def clu_zip(shapes, columns, rows, prj=ALBERS_WKT, stem="clu", extras=None) -> bytes:
	"""The zipped set the county office actually hands over."""
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w") as archive:
		archive.writestr(f"{stem}.shp", write_shp(shapes))
		archive.writestr(f"{stem}.shx", b"\x00" * 100)
		if columns is not None:
			archive.writestr(f"{stem}.dbf", write_dbf(columns, rows))
		if prj is not None:
			archive.writestr(f"{stem}.prj", prj)
		for name, payload in (extras or {}).items():
			archive.writestr(name, payload)
	return buffer.getvalue()


#: The column set of the 2008 public CLU release, which is the spelling most
#: county exports still use.
CLU_COLUMNS = [
	("CLUID", "C", 40, 0),
	("FARMNBR", "N", 10, 0),
	("TRACTNBR", "N", 10, 0),
	("CLUNBR", "N", 10, 0),
	("CALCACRES", "N", 12, 3),
	("HELTYPECD", "C", 4, 0),
]

GUID_ONE = "{2C9A1F30-1111-4A2B-9C3D-0000000000AA}"
GUID_TWO = "{2C9A1F30-2222-4A2B-9C3D-0000000000BB}"


def shifted(ring, east=0.0, north=0.0):
	"""The same shape somewhere else, for a second block on the same farm."""
	return [(x + east, y + north) for x, y in ring]


def clockwise(ring):
	"""The shapefile specification's winding for an outer ring."""
	return list(reversed(ring))


def encoded(blob: bytes) -> str:
	return base64.b64encode(blob).decode("ascii")


# ── the file itself ─────────────────────────────────────────────────────────
class ReadingAZippedShapefile(unittest.TestCase):
	def a_file(self, **kwargs):
		rows = [
			{
				"CLUID": GUID_ONE,
				"FARMNBR": 7391,
				"TRACTNBR": 1234,
				"CLUNBR": 3,
				"CALCACRES": 25.7,
				"HELTYPECD": "NHEL",
			}
		]
		return clu_zip([[clockwise(BLOCK_RING_ALBERS)]], CLU_COLUMNS, rows, **kwargs)

	def test_a_zipped_set_comes_back_as_one_feature_in_degrees(self):
		payload = fsa.read(self.a_file(), "clu_wasco.zip")
		self.assertEqual(payload["format"], "shapefile")
		self.assertEqual(len(payload["features"]), 1)
		ring = payload["features"][0]["geometry"]["coordinates"][0]
		self.assertTrue(geo.parse(payload["features"][0]["geometry"]))
		for longitude, latitude in ring:
			self.assertTrue(-180 <= longitude <= 180)
			self.assertTrue(-90 <= latitude <= 90)

	def test_the_dbf_columns_arrive_with_the_shape_they_belong_to(self):
		"""The two files are matched BY POSITION and by nothing else, which is why
		a null shape is kept rather than dropped — see `read_shp`."""
		payload = fsa.read(self.a_file())
		properties = payload["features"][0]["properties"]
		self.assertEqual(properties["TRACTNBR"], 1234)
		self.assertEqual(properties["CLUNBR"], 3)
		self.assertAlmostEqual(properties["CALCACRES"], 25.7, places=3)
		self.assertEqual(properties["HELTYPECD"], "NHEL")

	def test_the_polygon_encloses_the_acreage_fsa_says_it_does(self):
		"""The whole reprojection claim in one number. 25.7 acres out of a file of
		metres means the .prj was read, the inverse was right and the ring came
		back to the same five corners."""
		payload = fsa.read(self.a_file())
		self.assertAlmostEqual(geo.area_acres(payload["features"][0]["geometry"]), 25.67, delta=0.1)

	def test_every_corner_lands_within_a_centimetre_of_where_it_started(self):
		payload = fsa.read(self.a_file())
		ring = payload["features"][0]["geometry"]["coordinates"][0]
		for longitude, latitude in BLOCK_RING[:4]:
			closest = min(max(abs(point[0] - longitude), abs(point[1] - latitude)) for point in ring)
			# A degree is about 111 km, so 1e-7 degrees is about a centimetre.
			self.assertLess(closest, 2e-7, f"{longitude},{latitude} did not come back")

	def test_a_mac_made_zip_does_not_confuse_the_resource_forks_for_the_shapefile(self):
		"""Every zip made on a Mac carries a `__MACOSX/._name` beside each file,
		and reading one of those gives "the .shp is too short to be a shapefile"."""
		extras = {"__MACOSX/._clu.shp": b"\x00\x05junk", "__MACOSX/._clu.dbf": b"\x00"}
		payload = fsa.read(self.a_file(extras=extras))
		self.assertEqual(len(payload["features"]), 1)

	def test_two_shapefiles_in_one_zip_are_refused_by_name(self):
		"""A county export often holds the CLU layer AND the tract layer. Picking
		one would file every field under the wrong number half the time."""
		extras = {"tracts.shp": write_shp([[clockwise(BLOCK_RING_ALBERS)]])}
		with self.assertRaises(Exception) as caught:
			fsa.read(self.a_file(extras=extras))
		self.assertIn("clu.shp", str(caught.exception))
		self.assertIn("tracts.shp", str(caught.exception))

	def test_the_sidecars_are_paired_by_stem_and_not_by_being_the_only_one(self):
		"""A zip with two layers in it has two .dbfs. Pairing the CLU geometry
		with the tract attributes is the silent version of the failure above."""
		extras = {
			"tracts.dbf": write_dbf([("TRACTNBR", "N", 10, 0)], [{"TRACTNBR": 9999}]),
			"tracts.prj": NAD83_WKT,
		}
		payload = fsa.read(self.a_file(extras=extras))
		self.assertEqual(payload["features"][0]["properties"]["TRACTNBR"], 1234)

	def test_a_bare_shp_says_what_else_to_send(self):
		"""A `.shp` on its own has no attributes and no coordinate system, and the
		two things it is missing are the two the import needs most."""
		with self.assertRaises(Exception) as caught:
			fsa.read(write_shp([[clockwise(BLOCK_RING_ALBERS)]]), "clu.shp")
		self.assertIn(".dbf", str(caught.exception))
		self.assertIn(".prj", str(caught.exception))

	def test_a_deleted_dbf_row_does_not_slide_every_later_row_onto_the_wrong_shape(self):
		"""dBase marks a row deleted with a `*` and leaves it in place; the .shp
		still has its shape. Skipping the row rather than the pair would file
		every field after it under the previous field's number."""
		rows = [
			{"CLUID": GUID_ONE, "TRACTNBR": 1234, "CLUNBR": 1, "__deleted__": True},
			{"CLUID": GUID_TWO, "TRACTNBR": 1234, "CLUNBR": 2},
		]
		blob = clu_zip(
			[[clockwise(BLOCK_RING_ALBERS)], [clockwise(OTHER_RING)]],
			CLU_COLUMNS,
			rows,
			prj=NAD83_WKT,
		)
		payload = fsa.read(blob)
		self.assertEqual(len(payload["features"]), 2)
		self.assertEqual(payload["features"][0]["properties"], {})
		self.assertEqual(payload["features"][1]["properties"]["CLUNBR"], 2)


# ── the coordinate system ───────────────────────────────────────────────────
class Reprojection(unittest.TestCase):
	"""The part that usually stops an import dead, and the part with no symptom.

	A file of metres read as degrees is refused loudly by `geo.parse` — "not on
	Earth" — and somebody goes looking. A file of FEET whose false easting is
	subtracted in METRES produces a valid, closed polygon in the wrong state.
	"""

	def a_file(self, ring, prj, **kwargs):
		rows = [{"CLUID": GUID_ONE, "TRACTNBR": 1234, "CLUNBR": 3, "CALCACRES": 25.7}]
		return clu_zip([[clockwise(ring)]], CLU_COLUMNS, rows, prj=prj, **kwargs)

	def corners(self, payload):
		return payload["features"][0]["geometry"]["coordinates"][0]

	def test_state_plane_in_us_survey_feet_lands_in_washington(self):
		"""The false easting is 1,640,416.667 FEET. Subtracting it from a
		coordinate already converted to metres puts this block about 1,100 km
		west of where it is — in the Pacific, and still a perfectly valid
		polygon."""
		payload = fsa.read(self.a_file(WASHINGTON_FEET_RING, WASHINGTON_FEET_WKT))
		for longitude, latitude in WASHINGTON_DEGREES_RING[:4]:
			closest = min(
				max(abs(point[0] - longitude), abs(point[1] - latitude)) for point in self.corners(payload)
			)
			self.assertLess(closest, 5e-7, f"{longitude},{latitude} did not come back")

	def test_a_geographic_file_is_left_exactly_as_it_is(self):
		"""FSA's own distribution is already degrees. Running it through an
		inverse projection would be the way to break the common case."""
		payload = fsa.read(self.a_file(BLOCK_RING, NAD83_WKT))
		self.assertEqual(payload["crs"]["kind"], "geographic")
		self.assertIn([-121.18, 45.6], self.corners(payload))

	def test_a_projection_this_cannot_invert_is_refused_by_name(self):
		"""Naming it is the whole point: 'this is an Oblique Mercator' is a
		sentence somebody can act on, and a polygon quietly landing in the
		Atlantic is not."""
		with self.assertRaises(Exception) as caught:
			fsa.read(self.a_file(BLOCK_RING_ALBERS, UNKNOWN_WKT))
		message = str(caught.exception)
		self.assertIn("Hotine_Oblique_Mercator", message)
		self.assertIn("EPSG:4326", message)
		self.assertIn("Nothing was changed", message)

	def test_no_prj_at_all_is_read_as_degrees_when_the_numbers_could_be(self):
		"""An export that lost its .prj is common and usually WGS84. It is read
		with a warning rather than refused, because refusing a file that is
		almost certainly fine helps nobody."""
		payload = fsa.read(self.a_file(BLOCK_RING, None))
		self.assertIn("no .prj", " ".join(payload["warnings"]))
		self.assertIn([-121.18, 45.6], self.corners(payload))

	def test_no_prj_and_coordinates_that_are_not_degrees_is_refused(self):
		"""This is the case where guessing would silently invent a location."""
		with self.assertRaises(Exception) as caught:
			fsa.read(self.a_file(BLOCK_RING_ALBERS, None))
		self.assertIn("no .prj", str(caught.exception))
		self.assertIn("not longitude/latitude", str(caught.exception))

	def test_nad27_is_imported_and_said_out_loud(self):
		"""NAD27 is off by tens of metres and no shift is applied. The file is
		still usable — it is the operator's call — but nothing pretends the
		offset is not there."""
		nad27 = (
			'GEOGCS["GCS_North_American_1927",DATUM["D_North_American_1927",'
			'SPHEROID["Clarke_1866",6378206.4,294.9786982]],PRIMEM["Greenwich",0.0],'
			'UNIT["Degree",0.0174532925199433]]'
		)
		payload = fsa.read(self.a_file(BLOCK_RING, nad27))
		self.assertIn("NAD27", " ".join(payload["warnings"]))
		self.assertIn("tens of metres", " ".join(payload["warnings"]))

	def test_an_epsg_code_on_its_own_is_understood(self):
		"""A hand-made export sometimes writes `EPSG:5070` into the .prj instead
		of the WKT. It is the same coordinate system and refusing it would be
		pedantry."""
		payload = fsa.read(self.a_file(BLOCK_RING_ALBERS, "EPSG:5070"))
		self.assertAlmostEqual(geo.area_acres(payload["features"][0]["geometry"]), 25.67, delta=0.1)

	def test_geojson_carrying_a_2008_crs_member_is_reprojected(self):
		"""RFC 7946 says GeoJSON is degrees. The 2008 draft let a file say
		otherwise and exports still do — one naming EPSG:5070 is a file full of
		metres wearing a format that promises degrees."""
		collection = {
			"type": "FeatureCollection",
			"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::5070"}},
			"features": [
				{
					"type": "Feature",
					"properties": {"TRACTNBR": 1234, "CLUNBR": 3},
					"geometry": {
						"type": "Polygon",
						"coordinates": [[list(point) for point in BLOCK_RING_ALBERS]],
					},
				}
			],
		}
		payload = fsa.read(json.dumps(collection).encode("utf-8"), "clu.geojson")
		self.assertEqual(payload["format"], "geojson")
		self.assertAlmostEqual(geo.area_acres(payload["features"][0]["geometry"]), 25.67, delta=0.1)


# ── which ring is a hole ────────────────────────────────────────────────────
class RingNesting(unittest.TestCase):
	"""A shapefile record is a flat list of rings. Nothing in it says which is a
	hole except the winding, and the winding is wrong often enough to matter."""

	def geometry(self, rings, prj=NAD83_WKT):
		payload = fsa.read(clu_zip([rings], CLU_COLUMNS, [{"CLUNBR": 1}], prj=prj))
		return payload["features"][0]["geometry"]

	def test_a_wetland_exclusion_is_a_hole_and_not_a_second_field(self):
		"""Written the specification's way: outer clockwise, hole counter-clockwise."""
		shape = self.geometry([clockwise(BLOCK_RING), HOLE_RING])
		self.assertEqual(shape["type"], "Polygon")
		self.assertEqual(len(shape["coordinates"]), 2)
		self.assertLess(geo.area_acres(shape), 25.0)

	def test_the_same_file_wound_backwards_still_gives_one_field_with_a_hole(self):
		"""Half the tools that write shapefiles ignore the winding rule. A ten-acre
		field with an exclusion would come back as twelve acres in two pieces —
		a number nobody would question."""
		shape = self.geometry([BLOCK_RING, clockwise(HOLE_RING)])
		self.assertEqual(shape["type"], "Polygon")
		self.assertEqual(len(shape["coordinates"]), 2)
		self.assertLess(geo.area_acres(shape), 25.0)

	def test_two_separate_rings_are_two_polygons_and_not_a_hole(self):
		"""A CLU split by a county road is one field in two pieces. Treating the
		second ring as a hole in the first would subtract a real ten acres."""
		shape = self.geometry([clockwise(BLOCK_RING), clockwise(OTHER_RING)])
		self.assertEqual(shape["type"], "MultiPolygon")
		self.assertEqual(len(shape["coordinates"]), 2)
		self.assertGreater(geo.area_acres(shape), 45.0)

	def test_the_outer_ring_comes_out_counter_clockwise_as_rfc_7946_requires(self):
		shape = self.geometry([clockwise(BLOCK_RING), HOLE_RING])
		outer, hole = shape["coordinates"]
		self.assertGreater(self.signed(outer), 0)
		self.assertLess(self.signed(hole), 0)

	def test_a_ring_of_three_points_is_dropped_rather_than_stored_as_an_area(self):
		"""A closed ring needs four positions. Three is a line, and a line has no
		area for the acreage check to compare against."""
		shape = self.geometry([[(-121.18, 45.6), (-121.176, 45.6), (-121.18, 45.6)]])
		self.assertIsNone(shape)

	@staticmethod
	def signed(ring):
		return sum(
			ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1]
			for index in range(len(ring) - 1)
		)


# ── the attribute table ─────────────────────────────────────────────────────
class TheColumnNames(unittest.TestCase):
	"""FSA has spelled the same four columns at least three ways since 2008."""

	def test_the_2008_public_release_spelling_is_understood(self):
		found = fsa.canonical_attributes(
			{"CLUID": GUID_ONE, "FARMNBR": 7391, "TRACTNBR": 1234, "CLUNBR": 3, "CALCACRES": 25.7}
		)
		self.assertEqual(found["tract_number"], "1234")
		self.assertEqual(found["clu_number"], "3")
		self.assertEqual(found["farm_number"], "7391")
		self.assertEqual(found["calc_acres"], 25.7)

	def test_the_long_spelling_is_the_same_four_columns(self):
		found = fsa.canonical_attributes(
			{
				"CLU_IDENTIFIER": GUID_ONE,
				"FARM_NUMBER": 7391,
				"TRACT_NUMBER": 1234,
				"CLU_NUMBER": 3,
				"CALCULATED_ACRES": 25.7,
			}
		)
		self.assertEqual(found["tract_number"], "1234")
		self.assertEqual(found["clu_number"], "3")

	def test_case_and_separators_do_not_make_a_new_column(self):
		found = fsa.canonical_attributes({"Tract Nbr": 1234, "clu_nbr": 3, "gis acres": 25.7})
		self.assertEqual(found["tract_number"], "1234")
		self.assertEqual(found["clu_number"], "3")
		self.assertEqual(found["calc_acres"], 25.7)

	def test_a_column_this_app_has_never_heard_of_is_not_dropped(self):
		"""It is somebody's own note about their own ground. `canonical_attributes`
		does not modify the feature; the raw properties travel with it."""
		payload = fsa.read(
			clu_zip(
				[[clockwise(BLOCK_RING)]],
				[("CLUNBR", "N", 4, 0), ("PIVOT", "C", 12, 0)],
				[{"CLUNBR": 3, "PIVOT": "north wheel"}],
				prj=NAD83_WKT,
			)
		)
		self.assertEqual(payload["features"][0]["properties"]["PIVOT"], "north wheel")

	def test_three_spellings_of_one_tract_number_normalise_to_one_string(self):
		"""`1234.0` out of a dBase numeric, `"0012"` out of a text column and `12`
		out of JSON. If these do not agree, next season's file creates a second
		copy of every field."""
		self.assertEqual(fsa.canonical_attributes({"TRACTNBR": 1234.0})["tract_number"], "1234")
		self.assertEqual(fsa.canonical_attributes({"TRACTNBR": "0012"})["tract_number"], "12")
		self.assertEqual(fsa.canonical_attributes({"TRACTNBR": 12})["tract_number"], "12")

	def test_a_guid_is_compared_without_its_braces(self):
		"""ArcGIS puts them on and takes them off at random."""
		with_braces = fsa.canonical_attributes({"CLUID": GUID_ONE})["clu_identifier"]
		without = fsa.canonical_attributes({"CLUID": GUID_ONE.strip("{}").lower()})["clu_identifier"]
		self.assertEqual(with_braces, without)

	def test_a_block_is_named_after_the_tract_and_field_and_not_the_guid(self):
		"""A crew leader reads this off a task. `T1234-3` is what is written on the
		farm's own maps; the GUID is not a name anybody can say."""
		found = fsa.canonical_attributes({"TRACTNBR": 1234, "CLUNBR": 3, "CLUID": GUID_ONE})
		self.assertEqual(fsa.suggested_field_name(found), "T1234-3")

	def test_field_name_is_not_mistaken_for_a_field_number(self):
		"""`FIELD_NUMBER` is FSA's; `field_name` is this app's own convention on a
		FeatureCollection, and squashing separators brings them close together."""
		found = fsa.canonical_attributes({"field_name": "Yellow Camp Block 3", "FIELD_NUMBER": 3})
		self.assertEqual(found["field_name"], "Yellow Camp Block 3")
		self.assertEqual(found["clu_number"], "3")
		self.assertEqual(fsa.suggested_field_name(found), "Yellow Camp Block 3")

	def test_a_numeric_column_with_decimals_keeps_them(self):
		payload = fsa.read(
			clu_zip(
				[[clockwise(BLOCK_RING)]],
				CLU_COLUMNS,
				[{"CALCACRES": 25.703, "CLUNBR": 3}],
				prj=NAD83_WKT,
			)
		)
		self.assertAlmostEqual(payload["features"][0]["properties"]["CALCACRES"], 25.703, places=3)


# ── KML, KMZ and the caps ───────────────────────────────────────────────────
def kml_document(rings, tract=1234, number=3):
	placemarks = []
	for ring in rings:
		coordinates = " ".join(f"{lon},{lat},0" for lon, lat in ring)
		placemarks.append(
			f"""<Placemark><name>Tract {tract} Field {number}</name>
			<ExtendedData><SchemaData>
			<SimpleData name="TRACTNBR">{tract}</SimpleData>
			<SimpleData name="CLUNBR">{number}</SimpleData>
			<SimpleData name="CALCACRES">25.7</SimpleData>
			</SchemaData></ExtendedData>
			<Polygon><outerBoundaryIs><LinearRing><coordinates>{coordinates}</coordinates>
			</LinearRing></outerBoundaryIs></Polygon></Placemark>"""
		)
	return (
		'<?xml version="1.0" encoding="UTF-8"?>'
		'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + "".join(placemarks) + "</Document></kml>"
	).encode("utf-8")


class KmlAndKmz(unittest.TestCase):
	def test_a_kml_placemark_becomes_a_clu(self):
		payload = fsa.read(kml_document([BLOCK_RING]), "clu.kml")
		self.assertEqual(payload["format"], "kml")
		self.assertAlmostEqual(geo.area_acres(payload["features"][0]["geometry"]), 25.67, delta=0.1)

	def test_extended_data_is_where_the_clu_columns_are(self):
		payload = fsa.read(kml_document([BLOCK_RING]))
		found = fsa.canonical_attributes(payload["features"][0]["properties"])
		self.assertEqual(found["tract_number"], "1234")
		self.assertEqual(found["clu_number"], "3")

	def test_a_kmz_is_a_zip_with_a_kml_in_it(self):
		buffer = io.BytesIO()
		with zipfile.ZipFile(buffer, "w") as archive:
			archive.writestr("doc.kml", kml_document([BLOCK_RING]))
		payload = fsa.read(buffer.getvalue(), "clu.kmz")
		self.assertEqual(payload["format"], "kml")
		self.assertEqual(len(payload["features"]), 1)

	def test_a_doctype_declaration_is_refused_before_the_parser_sees_it(self):
		"""`xml.etree` expands internal entities, so forty lines of nested entity
		declarations expand to gigabytes inside the request. Nothing Google Earth
		or FSA writes contains one."""
		bomb = (
			b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaaaaaaaa">]><kml><Document></Document></kml>'
		)
		with self.assertRaises(Exception) as caught:
			fsa.read(bomb, "clu.kml")
		self.assertIn("DOCTYPE", str(caught.exception))


class TheCaps(unittest.TestCase):
	def test_an_upload_larger_than_the_cap_is_refused_before_it_is_decoded(self):
		oversized = "A" * (fsa.MAX_BYTES * 4 // 3 + 4096)
		with self.assertRaises(Exception) as caught:
			fsa.decode_upload(oversized)
		self.assertIn("MB", str(caught.exception))

	def test_a_data_uri_prefix_is_accepted_because_that_is_what_a_browser_sends(self):
		blob = fsa.decode_upload("data:application/zip;base64," + encoded(b"PK\x03\x04rest"))
		self.assertTrue(blob.startswith(b"PK\x03\x04"))

	def test_something_that_is_not_base64_says_so(self):
		with self.assertRaises(Exception) as caught:
			fsa.decode_upload("not base64 at all!!")
		self.assertIn("base64", str(caught.exception))

	def test_a_zip_that_expands_past_the_cap_is_refused(self):
		"""A zip bomb is a small file that is not a small file."""
		buffer = io.BytesIO()
		with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
			archive.writestr("clu.shp", b"\x00" * (fsa.MAX_BYTES + 1024))
		with self.assertRaises(Exception) as caught:
			fsa.read(buffer.getvalue())
		self.assertIn("expands", str(caught.exception))

	def test_a_point_layer_is_refused_with_what_it_actually_is(self):
		"""'no polygons found' sends somebody looking for a corrupt file. 'this
		holds Point shapes' sends them back to the office for the right layer."""
		header = struct.pack(">i", 9994) + b"\x00" * 20
		header += struct.pack(">i", 56)
		header += struct.pack("<ii", 1000, 1)
		header += struct.pack("<4d", 0, 0, 0, 0) + struct.pack("<4d", 0, 0, 0, 0)
		record = struct.pack("<i", 1) + struct.pack("<2d", -121.18, 45.6)
		blob = header + struct.pack(">ii", 1, len(record) // 2) + record
		buffer = io.BytesIO()
		with zipfile.ZipFile(buffer, "w") as archive:
			archive.writestr("clu.shp", blob)
			archive.writestr("clu.prj", NAD83_WKT)
		with self.assertRaises(Exception) as caught:
			fsa.read(buffer.getvalue())
		self.assertIn("Point", str(caught.exception))


# ── the two tools, against a site ───────────────────────────────────────────
SWITCHES = {
	"allow_create_parcel": 1,
	"allow_create_field": 1,
	"allow_update_field": 1,
	"allow_get_field": 1,
	"allow_list_fields": 1,
	"allow_set_field_boundary": 1,
	"allow_set_parcel_boundary": 1,
	"allow_import_field_boundary_geojson": 1,
	"allow_read_fsa_clu_file": 1,
	"allow_import_fsa_clu_boundaries": 1,
}


class FsaToolTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **SWITCHES)

	def a_parcel(self, parcel_name="Mill Creek", acreage=330.0, company=MAIN):
		return self.tool_data(
			"create_parcel",
			{"owning_entity": company, "parcel_name": parcel_name, "acreage": acreage},
		)

	def a_field(self, field_name="Yellow Camp Block 3", parcel="Mill Creek", acreage=25.7, **kw):
		payload = {"parcel": parcel, "field_name": field_name, "acreage": acreage}
		payload.update(kw)
		return self.tool_data("create_field", payload)

	def a_clu_file(self, rows=None, shapes=None, prj=NAD83_WKT):
		rows = (
			rows
			if rows is not None
			else [
				{
					"CLUID": GUID_ONE,
					"FARMNBR": 7391,
					"TRACTNBR": 1234,
					"CLUNBR": 3,
					"CALCACRES": 25.7,
					"HELTYPECD": "NHEL",
				}
			]
		)
		shapes = shapes if shapes is not None else [[clockwise(BLOCK_RING)]]
		return encoded(clu_zip(shapes, CLU_COLUMNS, rows, prj=prj))

	def stored(self, name):
		from .harness import STORE

		return next(row for row in STORE.rows("Field") if row["name"] == name)


class ReadingTheFile(FsaToolTestCase):
	def test_it_says_what_is_in_the_file_and_touches_nothing(self):
		self.a_parcel()
		self.a_field(block_number="3")
		before = self.stored("Yellow Camp Block 3 - MC").get("boundary_geojson")

		data = self.tool_data("read_fsa_clu_file", {"file_base64": self.a_clu_file()})

		self.assertEqual(data["clu_count"], 1)
		self.assertEqual(data["format"], "shapefile")
		self.assertEqual(data["farm_numbers"], ["7391"])
		self.assertEqual(data["tracts"][0]["tract_number"], "1234")
		self.assertAlmostEqual(data["total_calc_acres"], 25.7, places=2)
		self.assertAlmostEqual(data["total_computed_acres"], 25.67, delta=0.1)
		self.assertEqual(self.stored("Yellow Camp Block 3 - MC").get("boundary_geojson"), before)

	def test_the_geometry_is_left_out_unless_it_is_asked_for(self):
		"""Forty boundaries is a large answer and the attributes are what a person
		reads."""
		plain = self.tool_data("read_fsa_clu_file", {"file_base64": self.a_clu_file()})
		self.assertNotIn("geometry", plain["clus"][0])
		full = self.tool_data(
			"read_fsa_clu_file", {"file_base64": self.a_clu_file(), "include_geometry": True}
		)
		self.assertEqual(full["clus"][0]["geometry"]["type"], "Polygon")

	@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
	def test_it_offers_the_file_in_the_shape_the_generic_importer_takes(self):
		"""`import_field_boundary_geojson` matches on `field_name` and
		`parcel_hint`, and a CLU carries neither. This is the translation, handed
		over rather than kept — and this test is the proof it is really the shape
		that tool takes rather than a plausible-looking dict."""
		self.a_parcel()
		self.a_field(field_name="T1234-3")

		read = self.tool_data(
			"read_fsa_clu_file",
			{"file_base64": self.a_clu_file(), "as_feature_collection": True, "parcel": "Mill Creek"},
		)
		collection = read["feature_collection"]
		self.assertEqual(collection["features"][0]["properties"]["field_name"], "T1234-3")
		self.assertEqual(collection["features"][0]["properties"]["parcel_hint"], "Mill Creek")

		applied = self.tool_data(
			"import_field_boundary_geojson", {"feature_collection": collection, "apply": True}
		)
		self.assertEqual(applied["set"], ["T1234-3 - MC"])

	def test_a_file_with_nothing_in_it_says_which_layer_to_ask_for(self):
		message = self.tool_error(
			"read_fsa_clu_file", {"file_base64": encoded(clu_zip([], CLU_COLUMNS, [], prj=NAD83_WKT))}
		)
		self.assertIn("CLU layer", message)

	def test_it_refuses_two_ways_of_supplying_the_same_file_at_once(self):
		message = self.tool_error(
			"read_fsa_clu_file",
			{
				"file_base64": self.a_clu_file(),
				"feature_collection": {"type": "FeatureCollection", "features": []},
			},
		)
		self.assertIn("not both", message)


@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
class MatchingCLUsToBlocks(FsaToolTestCase):
	def test_a_first_import_matches_on_the_block_number(self):
		"""A farm that numbered its blocks the way FSA numbers its fields — which
		is most farms that ever filed an acreage report — matches here, before
		anything on the site carries an FSA column at all."""
		self.a_parcel()
		self.a_field(block_number="3")

		plan = self.tool_data(
			"import_fsa_clu_boundaries", {"file_base64": self.a_clu_file(), "parcel": "Mill Creek"}
		)
		self.assertTrue(plan["dry_run"])
		self.assertEqual(plan["would_set"], 1)
		self.assertEqual(plan["results"][0]["matched_by"], "block_number")
		self.assertEqual(plan["results"][0]["field"], "Yellow Camp Block 3 - MC")

	def test_a_zero_padded_block_number_is_the_same_block(self):
		"""Compared after normalising rather than in the query. A farm that
		zero-padded its block numbers once would otherwise be told to create
		forty duplicates of what it already has."""
		self.a_parcel()
		self.a_field(block_number="03")
		plan = self.tool_data(
			"import_fsa_clu_boundaries", {"file_base64": self.a_clu_file(), "parcel": "Mill Creek"}
		)
		self.assertEqual(plan["results"][0]["matched_by"], "block_number")

	def test_nothing_is_written_without_apply(self):
		self.a_parcel()
		self.a_field(block_number="3")
		self.tool_data(
			"import_fsa_clu_boundaries", {"file_base64": self.a_clu_file(), "parcel": "Mill Creek"}
		)
		row = self.stored("Yellow Camp Block 3 - MC")
		self.assertFalse(row.get("boundary_geojson"))
		self.assertFalse(row.get("fsa_clu_identifier"))

	def test_applying_writes_the_boundary_and_the_identifiers_together(self):
		"""A boundary with no CLU identifier beside it cannot be recognised next
		season, so next season's file creates a second copy of the block."""
		self.a_parcel()
		self.a_field(block_number="3")

		done = self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		self.assertEqual(done["set"], ["Yellow Camp Block 3 - MC"])

		row = self.stored("Yellow Camp Block 3 - MC")
		self.assertAlmostEqual(geo.area_acres(json.loads(row["boundary_geojson"])), 25.67, delta=0.1)
		self.assertEqual(row["fsa_clu_identifier"], GUID_ONE.strip("{}"))
		self.assertEqual(row["fsa_tract_number"], "1234")
		self.assertEqual(row["fsa_clu_number"], "3")
		self.assertEqual(row["fsa_farm_number"], "7391")
		self.assertEqual(row["fsa_hel_type"], "NHEL")
		self.assertAlmostEqual(row["fsa_calc_acres"], 25.7, places=2)
		self.assertTrue(row["fsa_import_date"])

	def test_the_derived_columns_come_from_the_polygon_and_not_from_fsa(self):
		"""FSA's CALCACRES is kept because it is what a payment is made on, and it
		is never what the app computes with."""
		self.a_parcel()
		self.a_field(block_number="3")
		self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		row = self.stored("Yellow Camp Block 3 - MC")
		self.assertAlmostEqual(row["area_computed_acres"], 25.67, delta=0.1)
		self.assertNotEqual(row["area_computed_acres"], row["fsa_calc_acres"])
		self.assertTrue(row["h3_cells"])
		self.assertAlmostEqual(row["boundary_centroid_lat"], 45.6015, places=3)

	def test_the_block_keeps_its_own_acreage(self):
		"""The recorded acreage is what the boundary is checked against.
		Overwriting it with the number that came in the same file would make that
		check compare the import against itself."""
		self.a_parcel()
		self.a_field(block_number="3", acreage=26.4)
		self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		self.assertAlmostEqual(self.stored("Yellow Camp Block 3 - MC")["acreage"], 26.4, places=2)

	def test_next_season_the_same_file_is_an_update_and_not_a_second_farm(self):
		"""The CLU identifier is the only identifier FSA guarantees, and this is
		what it is for. The block number is changed in between to prove the match
		is not still coming from there."""
		from .harness import STORE

		self.a_parcel()
		self.a_field(block_number="3")
		self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		self.tool_data("update_field", {"field": "Yellow Camp Block 3 - MC", "block_number": "renamed"})

		again = self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		self.assertEqual(again["results"][0]["matched_by"], "clu_identifier")
		self.assertEqual(again["set"], ["Yellow Camp Block 3 - MC"])
		self.assertEqual(len(STORE.rows("Field")), 1)

	def test_tract_and_field_number_match_when_the_guid_has_changed(self):
		"""FSA reissues a CLU identifier when a field is split or combined; the
		tract and field number a farm has filed under for thirty years do not
		move."""
		self.a_parcel()
		self.a_field(block_number="3")
		self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		self.tool_data("update_field", {"field": "Yellow Camp Block 3 - MC", "block_number": "renamed"})

		reissued = self.a_clu_file(
			rows=[{"CLUID": GUID_TWO, "TRACTNBR": 1234, "CLUNBR": 3, "CALCACRES": 25.7}]
		)
		again = self.tool_data("import_fsa_clu_boundaries", {"file_base64": reissued, "parcel": "Mill Creek"})
		self.assertEqual(again["results"][0]["matched_by"], "tract_and_field")

	def test_a_clu_matching_nothing_is_skipped_and_says_what_to_do(self):
		self.a_parcel()
		plan = self.tool_data(
			"import_fsa_clu_boundaries", {"file_base64": self.a_clu_file(), "parcel": "Mill Creek"}
		)
		self.assertEqual(plan["skipped"], 1)
		self.assertEqual(plan["would_set"], 0)
		self.assertIn("create_missing", plan["results"][0]["reason"])
		self.assertIn("T1234-3", plan["results"][0]["reason"])

	def test_create_missing_registers_the_block_under_its_tract_and_field_number(self):
		self.a_parcel()
		done = self.tool_data(
			"import_fsa_clu_boundaries",
			{
				"file_base64": self.a_clu_file(),
				"parcel": "Mill Creek",
				"create_missing": True,
				"apply": True,
			},
		)
		self.assertEqual(done["created"], ["T1234-3 - MC"])
		row = self.stored("T1234-3 - MC")
		self.assertEqual(row["block_number"], "3")
		self.assertAlmostEqual(row["acreage"], 25.7, places=2)
		self.assertEqual(row["fsa_tract_number"], "1234")
		self.assertTrue(row["boundary_geojson"])

	def test_a_created_block_takes_fsas_acreage_and_not_the_polygons(self):
		"""They are within a percent of each other and one of them is the number
		the payment is made on."""
		self.a_parcel()
		self.tool_data(
			"import_fsa_clu_boundaries",
			{
				"file_base64": self.a_clu_file(),
				"parcel": "Mill Creek",
				"create_missing": True,
				"apply": True,
			},
		)
		row = self.stored("T1234-3 - MC")
		self.assertEqual(row["acreage"], 25.7)
		self.assertNotEqual(row["acreage"], row["area_computed_acres"])


@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
class WhatItRefuses(FsaToolTestCase):
	def test_a_polygon_a_quarter_away_from_the_recorded_acreage_is_refused(self):
		"""The same rule `set_field_boundary` has always applied. Two figures a
		quarter apart are not a survey disagreement — one of them is about a
		different piece of ground."""
		self.a_parcel()
		self.a_field(block_number="3", acreage=5.0)

		plan = self.tool_data(
			"import_fsa_clu_boundaries", {"file_base64": self.a_clu_file(), "parcel": "Mill Creek"}
		)
		self.assertEqual(plan["errors"], 1)
		self.assertEqual(plan["would_set"], 0)
		self.assertIn("different piece of ground", plan["results"][0]["reason"])

	def test_a_refused_clu_does_not_take_the_rest_of_the_file_with_it(self):
		"""Per-CLU and not whole-batch: naming the bad one and applying the other
		thirty-nine beats refusing the lot."""
		self.a_parcel()
		self.a_field(field_name="Block 3", block_number="3", acreage=5.0)
		self.a_field(field_name="Block 4", block_number="4", acreage=25.7)

		rows = [
			{"CLUID": GUID_ONE, "TRACTNBR": 1234, "CLUNBR": 3, "CALCACRES": 25.7},
			{"CLUID": GUID_TWO, "TRACTNBR": 1234, "CLUNBR": 4, "CALCACRES": 25.7},
		]
		blob = self.a_clu_file(rows=rows, shapes=[[clockwise(BLOCK_RING)], [clockwise(OTHER_RING)]])
		done = self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": blob, "parcel": "Mill Creek", "apply": True},
		)
		self.assertEqual(done["set"], ["Block 4 - MC"])
		self.assertEqual(done["errors"], 1)
		self.assertFalse(self.stored("Block 3 - MC").get("boundary_geojson"))
		self.assertTrue(self.stored("Block 4 - MC").get("boundary_geojson"))

	def test_an_acreage_five_percent_out_is_a_warning_and_still_applies(self):
		"""A deed, a GIS trace and an FSA measure routinely disagree by a few
		percent and all three are 'right'."""
		self.a_parcel()
		self.a_field(block_number="3", acreage=23.0)
		done = self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		self.assertEqual(done["set"], ["Yellow Camp Block 3 - MC"])
		self.assertIn("both figures are kept", done["results"][0]["warning"])

	def test_two_clus_cannot_both_be_one_block(self):
		"""A file with the same field number twice, or two CLUs whose numbers both
		resolve to one block, would otherwise leave whichever came last on the
		record with no sign the other was ever there."""
		self.a_parcel()
		self.a_field(block_number="3")
		rows = [
			{"CLUID": GUID_ONE, "TRACTNBR": 1234, "CLUNBR": 3, "CALCACRES": 25.7},
			{"CLUID": GUID_TWO, "TRACTNBR": 1234, "CLUNBR": 3, "CALCACRES": 25.7},
		]
		blob = self.a_clu_file(rows=rows, shapes=[[clockwise(BLOCK_RING)], [clockwise(OTHER_RING)]])
		plan = self.tool_data("import_fsa_clu_boundaries", {"file_base64": blob, "parcel": "Mill Creek"})
		self.assertEqual(plan["would_set"], 1)
		self.assertIn("already matched", plan["results"][1]["reason"])

	def test_two_blocks_carrying_one_block_number_are_not_guessed_between(self):
		self.a_parcel()
		self.a_field(field_name="Block 3 East", block_number="3", acreage=10.0)
		self.a_field(field_name="Block 3 West", block_number="3", acreage=10.0)
		plan = self.tool_data(
			"import_fsa_clu_boundaries", {"file_base64": self.a_clu_file(), "parcel": "Mill Creek"}
		)
		self.assertEqual(plan["errors"], 1)
		self.assertIn("will not guess", plan["results"][0]["reason"])

	def test_a_null_shape_is_reported_rather_than_silently_dropped(self):
		"""A `.dbf` row with no geometry is a CLU somebody has to go and ask about,
		not a row to leave out of the count."""
		self.a_parcel()
		blob = encoded(
			clu_zip(
				[[]],
				CLU_COLUMNS,
				[{"CLUID": GUID_ONE, "TRACTNBR": 1234, "CLUNBR": 3}],
				prj=NAD83_WKT,
			)
		)
		plan = self.tool_data("import_fsa_clu_boundaries", {"file_base64": blob, "parcel": "Mill Creek"})
		self.assertEqual(plan["feature_count"], 1)
		self.assertIn("null shape", plan["results"][0]["reason"])

	def test_create_missing_with_nowhere_to_put_it_says_so(self):
		plan = self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "create_missing": True},
		)
		self.assertEqual(plan["skipped"], 1)
		self.assertIn("no parcel", plan["results"][0]["reason"])

	def test_the_write_is_recorded_in_the_action_log(self):
		self.a_parcel()
		self.a_field(block_number="3")
		self.tool_data(
			"import_fsa_clu_boundaries",
			{"file_base64": self.a_clu_file(), "parcel": "Mill Creek", "apply": True},
		)
		self.assertTrue(self.audit_rows(tool_name="import_fsa_clu_boundaries"))


@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
class AFarmIsSeveralParcels(FsaToolTestCase):
	"""One FSA farm number covers several tracts and several tax lots, so a file
	spanning parcels is the normal case rather than the exception."""

	def two_tracts(self):
		self.a_parcel("Mill Creek", 330.0)
		self.a_parcel("Dry Hollow", 330.0)
		self.a_field(field_name="MC Block 3", parcel="Mill Creek", block_number="3")
		self.a_field(field_name="DH Block 7", parcel="Dry Hollow", block_number="7")
		rows = [
			{"CLUID": GUID_ONE, "TRACTNBR": 1234, "CLUNBR": 3, "CALCACRES": 25.7},
			{"CLUID": GUID_TWO, "TRACTNBR": 1235, "CLUNBR": 7, "CALCACRES": 25.7},
		]
		return self.a_clu_file(rows=rows, shapes=[[clockwise(BLOCK_RING)], [clockwise(OTHER_RING)]])

	def test_each_tract_goes_to_the_parcel_it_is_on(self):
		blob = self.two_tracts()
		done = self.tool_data(
			"import_fsa_clu_boundaries",
			{
				"file_base64": blob,
				"tract_parcels": {"1234": "Mill Creek", "1235": "Dry Hollow"},
				"apply": True,
			},
		)
		self.assertEqual(sorted(done["set"]), ["DH Block 7 - DH", "MC Block 3 - MC"])

	def test_one_parcel_for_the_whole_file_finds_only_the_blocks_on_it(self):
		"""The single-parcel shorthand is not wrong, it is narrower — and the
		report says which CLUs it could not place rather than placing them."""
		blob = self.two_tracts()
		plan = self.tool_data("import_fsa_clu_boundaries", {"file_base64": blob, "parcel": "Mill Creek"})
		self.assertEqual(plan["would_set"], 1)
		self.assertEqual(plan["skipped"], 1)

	def test_a_tract_named_against_a_parcel_that_does_not_exist_fails_before_any_write(self):
		"""Resolved once, up front: a typo in the fourth tract must not be found
		after the first three have been written."""
		blob = self.two_tracts()
		message = self.tool_error(
			"import_fsa_clu_boundaries",
			{"file_base64": blob, "tract_parcels": {"1234": "Nowhere Ranch"}, "apply": True},
		)
		self.assertIn("Nowhere Ranch", message)
		self.assertFalse(self.stored("MC Block 3 - MC").get("boundary_geojson"))


# ── the Desk path ───────────────────────────────────────────────────────────
class TheDeskEndpoints(FsaToolTestCase):
	"""Two whitelisted methods, reached from a form by somebody already signed in.

	`mcp.handle` runs the master switch, the shared token and the CIDR allowlist
	before it looks a tool up. A `@frappe.whitelist()` method reached from a Desk
	form runs NONE of that, so the gate is the one line in each of these — and
	each is asserted by its absence first.
	"""

	def as_user(self, user):
		import frappe

		frappe.local.session.user = user
		self.addCleanup(lambda: setattr(frappe.local.session, "user", "Administrator"))

	def test_guest_cannot_parse_an_upload(self):
		import frappe

		from erpnext_mcp.api import gis

		self.as_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			gis.read_fsa_clu(content=self.a_clu_file(), filename="clu.zip")

	def test_parsing_wants_write_on_Field_and_not_merely_read(self):
		"""The only thing a CLU polygon is for here is setting a block's boundary.
		Gating on `read` would leave the site parsing uploaded archives for any
		signed-in account — a Family Member, an Advisor."""
		import frappe

		from erpnext_mcp.api import gis

		from .harness import STORE

		STORE.denied_permissions.add(("Field", "write"))
		self.addCleanup(STORE.denied_permissions.discard, ("Field", "write"))
		with self.assertRaises(frappe.PermissionError):
			gis.read_fsa_clu(content=self.a_clu_file(), filename="clu.zip")

	def test_guest_cannot_run_the_bulk_import(self):
		import frappe

		from erpnext_mcp.api import gis

		self.as_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			gis.import_fsa_clu(content=self.a_clu_file(), parcel="Mill Creek")

	def test_the_upload_is_never_stored_as_a_file_record(self):
		"""It goes into the request, is parsed, and the shapes come back. A grower
		who picked the wrong export off a memory stick has not left a copy of it
		on the site."""
		from erpnext_mcp.api import gis

		from .harness import STORE

		self.a_parcel()
		before = len(STORE.rows("File"))
		gis.read_fsa_clu(content=self.a_clu_file(), filename="clu.zip")
		self.assertEqual(len(STORE.rows("File")), before)

	def test_the_parse_answers_with_shapes_the_map_can_draw(self):
		from erpnext_mcp.api import gis

		answer = gis.read_fsa_clu(content=self.a_clu_file(), filename="clu.zip")
		self.assertEqual(answer["clu_count"], 1)
		self.assertEqual(answer["clus"][0]["tract_number"], "1234")
		self.assertEqual(answer["clus"][0]["geometry"]["type"], "Polygon")
		self.assertAlmostEqual(answer["clus"][0]["computed_acres"], 25.67, delta=0.1)

	@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
	def test_the_bulk_import_goes_through_the_tool_and_leaves_an_audit_row(self):
		from erpnext_mcp.api import gis

		self.a_parcel()
		self.a_field(block_number="3")
		answer = gis.import_fsa_clu(
			content=self.a_clu_file(), filename="clu.zip", parcel="Mill Creek", apply=1
		)
		self.assertEqual(answer["set"], ["Yellow Camp Block 3 - MC"])
		self.assertTrue(self.stored("Yellow Camp Block 3 - MC")["boundary_geojson"])
		self.assertTrue(self.audit_rows(tool_name="desk:import_fsa_clu_boundaries"))

	@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
	def test_the_bulk_import_is_a_dry_run_unless_the_browser_says_otherwise(self):
		from erpnext_mcp.api import gis

		self.a_parcel()
		self.a_field(block_number="3")
		answer = gis.import_fsa_clu(content=self.a_clu_file(), parcel="Mill Creek")
		self.assertTrue(answer["dry_run"])
		self.assertFalse(self.stored("Yellow Camp Block 3 - MC").get("boundary_geojson"))


class TheWidgetAndTheFsaMethodsAgree(unittest.TestCase):
	"""An argument the browser sends that the method does not name is a TypeError
	in a console nobody has open; one the method grew and the browser never sends
	is a feature that shipped and does nothing. Neither is visible from either
	file alone — which is exactly what `TheWidgetAndTheMethodAgree` says about the
	county pair, and the reason it is said again here rather than assumed."""

	import pathlib

	WIDGET = (
		pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp" / "public" / "js" / "geo_map_widget.js"
	)
	FIELD_MAP = WIDGET.parent / "field_map.js"
	PARCEL_MAP = WIDGET.parent / "parcel_map.js"

	def source(self, path=None):
		path = path or self.WIDGET
		self.assertTrue(path.exists(), f"{path} is gone")
		return path.read_text(encoding="utf-8")

	def keys_sent_to(self, method_constant: str) -> set:
		"""Every key of the `args` object literal passed alongside that method."""
		import re

		source = self.source()
		sent = set()
		for match in re.finditer(rf"method: {method_constant},\s*args: \{{", source):
			start = source.index("{", match.end() - 1)
			depth, index = 0, start
			while index < len(source):
				depth += {"{": 1, "}": -1}.get(source[index], 0)
				index += 1
				if not depth:
					break
			sent.update(re.findall(r"[,{]\s*(\w+):", source[start:index]))
		return sent

	def test_the_browser_sends_nothing_the_parse_method_does_not_name(self):
		import inspect

		from erpnext_mcp.api import gis

		sent = self.keys_sent_to("FSA_READ_METHOD")
		self.assertTrue(sent, "no read_fsa_clu call site was found in the widget")
		self.assertLessEqual(sent, set(inspect.signature(gis.read_fsa_clu).parameters))

	def test_the_browser_sends_nothing_the_import_method_does_not_name(self):
		import inspect

		from erpnext_mcp.api import gis

		sent = self.keys_sent_to("FSA_IMPORT_METHOD")
		self.assertTrue(sent, "no import_fsa_clu call site was found in the widget")
		self.assertLessEqual(sent, set(inspect.signature(gis.import_fsa_clu).parameters))

	def test_every_argument_the_import_method_takes_has_a_caller(self):
		"""The other direction: an argument the server grew that the dialog never
		offers is a feature nobody can reach."""
		import inspect

		from erpnext_mcp.api import gis

		accepted = set(inspect.signature(gis.import_fsa_clu).parameters)
		self.assertEqual(accepted, self.keys_sent_to("FSA_IMPORT_METHOD"))

	def test_the_button_is_on_the_block_form_and_on_no_other(self):
		"""A county publishes tax lots and FSA publishes fields. Each import is on
		the form whose unit it is about, and a button that appeared on both would
		be an invitation to set a block's boundary to a whole tax lot."""
		self.assertIn("fsa: true", self.source(self.FIELD_MAP))
		self.assertNotIn("fsa: true", self.source(self.PARCEL_MAP))
		self.assertNotIn("county: true", self.source(self.FIELD_MAP))

	def test_the_button_is_wired_only_where_the_form_asks_for_it(self):
		"""`conf.fsa` is what puts it there. A widget that added it unconditionally
		would put it on the Irrigation Zone and Housing Unit maps too."""
		source = self.source()
		self.assertIn("if (conf.fsa) {", source)
		body = source[source.index("if (conf.fsa) {") :]
		body = body[: body.index("\n\t\t}")]
		self.assertIn("open_fsa_import", body)

	def test_the_identifiers_written_from_a_clu_are_named_here(self):
		"""So that a seventh arriving is a deliberate edit rather than a diff
		nobody reads — the same reason the county's three are enumerated."""
		import re

		carried = re.findall(r'carry\("(\w+)"', self.source())
		self.assertEqual(
			carried,
			[
				"fsa_clu_identifier",
				"fsa_farm_number",
				"fsa_tract_number",
				"fsa_clu_number",
				"fsa_calc_acres",
				"fsa_hel_type",
			],
		)
		self.assertNotIn('frm.set_value("acreage"', self.source())
