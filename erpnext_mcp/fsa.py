# SPDX-License-Identifier: MIT
"""What the FSA office hands a grower, read without a GIS.

A farm's field boundaries already exist. They were drawn by the Farm Service
Agency, they are what the farm's acreage report, its crop insurance and every
CRP and ARC/PLC payment are measured against, and the grower can walk into the
county office and be handed the lot on a memory stick. Tracing those same shapes
by eye off a satellite image — which is what everybody does when importing is
hard — reproduces a known answer badly, and then the farm has two versions of
its own boundaries that disagree with each other by a few percent. One of those
two is the one FSA will pay on.

So this module reads what the office actually gives out, which is FOUR THINGS
and not one:

  * A ZIPPED SHAPEFILE SET — `.shp` with the geometry, `.dbf` with the CLU
    attributes, `.shx` with an index nothing here needs, `.prj` with the
    coordinate system. This is the common case and the reason this file is long.
  * A BARE `.shp` WITH ITS SIDECARS, unzipped, which is the same thing with the
    files handed over separately.
  * KML OR KMZ, which is what comes back from the crop-reporting side and from
    anybody who exported "for Google Earth".
  * GEOJSON, from a grower whose agronomist already converted it.

WHY THE FORMAT IS PARSED HERE RATHER THAN BY A LIBRARY. `pyshp` would read the
shapefile and `pyproj` would reproject it, and neither is a dependency of this
app. Adding two for one import path would mean a bench that lacks them loses
this feature — which is fine and is how `shapely` and `h3` are handled — except
that the shapefile format is a published, frozen, 1998 specification of about a
page and a half for the polygon case, and dBase III is older than that. Both are
read below in a few hundred lines that cannot break on an upgrade, because
neither format will ever change again.

REPROJECTION IS THE PART THAT ACTUALLY MATTERS, and it is why "just parse the
.shp" is not enough. FSA's own CLU distribution is geographic NAD83 — degrees,
which is what GeoJSON wants — but a set that has been through a vendor's system
comes back in whatever that system projects to, and in US agriculture that is
one of four things: USA Contiguous Albers Equal Area (EPSG:5070, the FSA/NRCS
house projection), a UTM zone on NAD83, a State Plane Lambert Conformal Conic,
or Web Mercator. Those coordinates are METRES — a State Plane easting is around
2,000,000 — and handing them to a GeoJSON reader produces a polygon "not on
Earth", which is a true error message and a useless one. Each of the four has a
closed-form inverse published in USGS Professional Paper 1395 (Snyder, 1987) and
each is implemented below against the ellipsoid named in the file's own `.prj`.

WHAT IS NOT DONE IS A DATUM SHIFT. NAD83 and WGS84 differ by a metre or two at
these latitudes and share an ellipsoid for all practical purposes, so a NAD83
coordinate is used as a WGS84 one and the difference is smaller than the width
of the line the boundary is drawn with. NAD27 is a different matter — it is off
by tens of metres, and a file that says NAD27 is reported as such rather than
quietly moved. The same goes for a projection this module does not know: it
refuses and names what it found, because a boundary that has been transformed by
a guess is worse than one that has not been transformed at all.

NOTHING HERE TOUCHES THE DATABASE OR IMPORTS FRAPPE. This module turns bytes
into a GeoJSON FeatureCollection with normalised CLU attributes; `tools/fsa.py`
decides what that means for a Field, and `geo.py` still validates every polygon
that reaches a record. A file that parses is not a boundary anybody has agreed
to yet.
"""

import base64
import binascii
import json
import math
import re
import struct
import xml.etree.ElementTree as ElementTree
import zipfile
from io import BytesIO

from .errors import ToolError

#: How much of an upload this will decode. A whole farm's CLU set is tens of
#: kilobytes — the geometry is a few hundred vertices per field — so this is
#: three orders of magnitude of headroom and still small enough that a mistyped
#: upload cannot make the bench swap.
MAX_BYTES = 8 * 1024 * 1024

#: Features in one file. An operation with two thousand CLUs is a land company,
#: not a farm, and it should be importing county by county so it can check what
#: it got.
MAX_FEATURES = 2000

#: Guards on one shapefile record, so a corrupt length field cannot ask this to
#: allocate a gigabyte. A CLU with more than 50,000 vertices is not a boundary.
MAX_PARTS = 5000
MAX_POINTS = 50000

#: Shapefile geometry types, from the 1998 ESRI white paper. Only the three
#: polygon flavours carry an area; the rest are named so a file of points can be
#: refused with what it actually is rather than "no polygons found".
SHAPE_TYPES = {
	0: "Null",
	1: "Point",
	3: "PolyLine",
	5: "Polygon",
	8: "MultiPoint",
	11: "PointZ",
	13: "PolyLineZ",
	15: "PolygonZ",
	18: "MultiPointZ",
	21: "PointM",
	23: "PolyLineM",
	25: "PolygonM",
	28: "MultiPointM",
	31: "MultiPatch",
}

#: PolygonZ and PolygonM carry the same rings as Polygon with elevation or
#: measure arrays bolted on the end, and the rings are in the same place. The
#: extra arrays are ignored: a field boundary is a shape on the ground.
POLYGON_TYPES = (5, 15, 25)

#: The 100-byte shapefile header: big-endian file code 9994 at 0, big-endian
#: length in 16-bit words at 24, little-endian version 1000 at 28, little-endian
#: shape type at 32, then the bounding box.
SHP_MAGIC = 9994
SHP_HEADER_BYTES = 100


# ── the CLU attribute table, which is the other half of "what FSA gives you" ──
#
# THE COLUMN NAMES ARE NOT STABLE AND NEVER WERE. The 2008 public CLU release
# spelled them `CLUID`, `TRACTNBR`, `CLUNBR`, `CALCACRES`; a county office export
# today may spell the same four `CLU_IDENTIFIER`, `TRACT_NUMBER`, `CLU_NUMBER`,
# `CALCULATED_ACRES`; a vendor round-trip renames them again and dBase truncates
# anything past ten characters while it is at it. Matching on one spelling means
# an import that silently finds no tract numbers and reports every field as
# unmatched, which reads like the farm's data being wrong.
#
# So the lookup is on the SQUASHED key — upper-cased with every separator
# removed — and `TRACT_NBR`, `tract nbr` and `TractNbr` are one entry rather
# than three. Anything unrecognised is kept verbatim under `properties`: a
# column this table has never heard of is somebody's own note about their own
# ground, and dropping it silently is how an import loses the one thing the
# grower cared about.
_ALIASES = {
	"clu_identifier": (
		"CLUID",
		"CLUIDENTIFIER",
		"CLUGUID",
		"CLUUUID",
		"CLUIDGUID",
		"GUID",
		"UUID",
	),
	"farm_number": ("FARMNBR", "FARMNUMBER", "FARMNO", "FARMNUM", "FARMID", "FSAFARMNUMBER"),
	"tract_number": (
		"TRACTNBR",
		"TRACTNUMBER",
		"TRACTNO",
		"TRACTNUM",
		"TRACTID",
		"FSATRACTNUMBER",
	),
	"clu_number": (
		"CLUNBR",
		"CLUNUMBER",
		"CLUNO",
		"CLUNUM",
		"FIELDNBR",
		"FIELDNUMBER",
		"FIELDNO",
		"FIELDNUM",
		"FLDNBR",
		"FSAFIELDNUMBER",
	),
	"calc_acres": (
		"CALCACRES",
		"CALCULATEDACRES",
		"CLUCALCULATEDACREAGE",
		"CLUACRES",
		"GISACRES",
		"FSAACRES",
		"ACRES",
		"ACREAGE",
	),
	"state_code": ("STATECD", "STATECODE", "ADMNSTATE", "ADMINSTATE", "STATEFIPS", "STATEANSI", "STATEFP"),
	"county_code": (
		"CNTYCD",
		"CNTYCODE",
		"COUNTYCODE",
		"ADMNCOUNTY",
		"ADMINCOUNTY",
		"COUNTYFIPS",
		"COUNTYANSI",
		"COUNTYFP",
	),
	"hel_type": ("HELTYPECD", "HELTYPECODE", "HELTYPE", "HELSTATUS", "HEL"),
	"classification_code": ("CLUCLSCD", "CLUCLASSIFICATIONCODE", "CLUCLASSCODE", "CLASSIFICATIONCODE"),
	"source_date": ("LASTCHNGDATE", "LASTCHANGEDATE", "CLUEFFECTIVEDATE", "EFFECTIVEDATE", "CREATEDATE"),
	#: OUR OWN TWO, not FSA's. `import_field_boundary_geojson` has taken
	#: `field_name` and `parcel_hint` since v0.31.0, and a collection that has
	#: been through that tool's shape should mean the same thing here.
	"field_name": ("FIELDNAME", "BLOCKNAME"),
	"parcel_hint": ("PARCELHINT", "PARCEL"),
}

#: Squashed column name → canonical key, built once from the table above.
_ALIAS_INDEX = {alias: canonical for canonical, aliases in _ALIASES.items() for alias in aliases}

#: The three that are NUMBERS WRITTEN AS TEXT. A tract number is an identifier,
#: not a quantity — nobody adds two of them — but FSA stores it in a dBase
#: numeric column, so it arrives as `1234.0` and a leading-zero form arrives as
#: `"0012"`. Both are the tract the office calls 12, and both must key the same
#: record or a re-import creates a second copy of every field.
_IDENTIFIER_KEYS = ("farm_number", "tract_number", "clu_number")

#: A CLU identifier is a GUID and is compared case-insensitively with its braces
#: and hyphens intact, because the braces are what ArcGIS puts on and takes off
#: at random.
_GUID_TRIM = re.compile(r"^[{(]|[)}]$")


# ── coordinate systems ──────────────────────────────────────────────────────
#
# A `.prj` is one line of OGC Well-Known Text. What has to come out of it is
# small: is this degrees already, and if not, which of the four projections is
# it, on which ellipsoid, with which parameters, in which linear unit. Every
# name below is quoted from a real `.prj` — ESRI's WKT1 spellings and the
# EPSG-registry spellings differ, and a file from ArcGIS and a file from QGIS
# describing the SAME projection do not agree on the parameter names.

#: WGS84 / GRS80, which NAD83 and WGS84 both use to within a rounding error.
DEFAULT_SEMI_MAJOR = 6378137.0
DEFAULT_INVERSE_FLATTENING = 298.257223563

#: US survey foot and international foot, for the State Plane systems that are
#: published in feet. A State Plane easting in feet is around 6,500,000 and in
#: metres around 2,000,000, and getting this wrong moves the farm to another
#: state rather than failing.
_LINEAR_UNITS = {
	"METRE": 1.0,
	"METER": 1.0,
	"M": 1.0,
	"FOOT_US": 1200.0 / 3937.0,
	"USSURVEYFOOT": 1200.0 / 3937.0,
	"USFEET": 1200.0 / 3937.0,
	"USFOOT": 1200.0 / 3937.0,
	"FOOT": 0.3048,
	"FEET": 0.3048,
	"INTERNATIONALFOOT": 0.3048,
}

#: Projection names this module can invert, squashed the same way column names
#: are. The value is the family; the parameters are read separately because the
#: same family is spelled several ways.
_PROJECTIONS = {
	"ALBERS": "albers",
	"ALBERSEQUALAREA": "albers",
	"ALBERSCONICEQUALAREA": "albers",
	"ALBERSEQUALAREACONIC": "albers",
	"LAMBERTCONFORMALCONIC": "lcc",
	"LAMBERTCONFORMALCONIC2SP": "lcc",
	"LAMBERTCONFORMALCONIC1SP": "lcc",
	"TRANSVERSEMERCATOR": "tmerc",
	"GAUSSKRUGER": "tmerc",
	"MERCATORAUXILIARYSPHERE": "webmerc",
	"POPULARVISUALISATIONPSEUDOMERCATOR": "webmerc",
	"MERCATOR1SP": "webmerc",
	"MERCATOR": "webmerc",
}

#: Parameter names, squashed. Each row is one quantity and every spelling of it
#: that turns up in the wild — ESRI's `Standard_Parallel_1`, EPSG's "Latitude of
#: 1st standard parallel", PROJ's `standard_parallel_1`.
_PARAMETERS = {
	"standard_parallel_1": ("STANDARDPARALLEL1", "LATITUDEOF1STSTANDARDPARALLEL"),
	"standard_parallel_2": ("STANDARDPARALLEL2", "LATITUDEOF2NDSTANDARDPARALLEL"),
	"latitude_of_origin": (
		"LATITUDEOFORIGIN",
		"LATITUDEOFCENTER",
		"LATITUDEOFCENTRE",
		"LATITUDEOFFALSEORIGIN",
		"LATITUDEOFNATURALORIGIN",
		"LATITUDEOFPROJECTIONCENTRE",
	),
	"central_meridian": (
		"CENTRALMERIDIAN",
		"LONGITUDEOFCENTER",
		"LONGITUDEOFCENTRE",
		"LONGITUDEOFFALSEORIGIN",
		"LONGITUDEOFNATURALORIGIN",
		"LONGITUDEOFORIGIN",
	),
	"false_easting": ("FALSEEASTING", "EASTINGATFALSEORIGIN", "EASTINGATPROJECTIONCENTRE"),
	"false_northing": ("FALSENORTHING", "NORTHINGATFALSEORIGIN", "NORTHINGATPROJECTIONCENTRE"),
	"scale_factor": ("SCALEFACTOR", "SCALEFACTORATNATURALORIGIN"),
}

_PARAMETER_INDEX = {alias: canonical for canonical, aliases in _PARAMETERS.items() for alias in aliases}

#: A `.prj` that is just an authority code, which is what a hand-made export
#: sometimes contains instead of WKT. Only the codes that actually turn up on
#: American farm data are here; anything else is reported as unknown rather than
#: guessed at.
_EPSG_CODES = {
	"4326": {"kind": "geographic", "name": "WGS 84"},
	"4269": {"kind": "geographic", "name": "NAD83"},
	"4152": {"kind": "geographic", "name": "NAD83(HARN)"},
	"6318": {"kind": "geographic", "name": "NAD83(2011)"},
	"3857": {"kind": "projected", "name": "WGS 84 / Pseudo-Mercator", "projection": "webmerc"},
	"5070": {
		"kind": "projected",
		"name": "NAD83 / Conus Albers",
		"projection": "albers",
		"params": {
			"standard_parallel_1": 29.5,
			"standard_parallel_2": 45.5,
			"latitude_of_origin": 23.0,
			"central_meridian": -96.0,
			"false_easting": 0.0,
			"false_northing": 0.0,
		},
	},
}
_EPSG_CODES["102003"] = dict(_EPSG_CODES["5070"], name="USA Contiguous Albers Equal Area Conic")


def _squash(name) -> str:
	"""A column, projection or parameter name with every separator taken out."""
	return re.sub(r"[^A-Z0-9]", "", str(name or "").upper())


def parse_prj(text: str) -> dict:
	"""What the `.prj` says, as much of it as reprojection needs.

	Returns a dict with `kind` ("geographic", "projected" or "unknown"), the
	system's name, the ellipsoid, and for a projected system the family and its
	parameters already converted to the units the inverse formulae want.
	"""
	raw = str(text or "").strip()
	if not raw:
		return {"kind": "missing", "name": "", "warnings": []}

	code = re.fullmatch(r"(?:EPSG|ESRI)\s*[:=]?\s*(\d{4,6})", raw, flags=re.IGNORECASE)
	if code:
		known = _EPSG_CODES.get(code.group(1))
		if known:
			return {**known, "warnings": [], "unit_factor": 1.0, "authority": code.group(1)}
		return {
			"kind": "unknown",
			"name": f"EPSG:{code.group(1)}",
			"warnings": [],
			"authority": code.group(1),
		}

	crs = {
		"kind": "geographic" if not re.search(r"PROJCS|PROJCRS", raw, flags=re.IGNORECASE) else "projected",
		"name": _wkt_name(raw),
		"datum": _wkt_datum(raw),
		"warnings": [],
	}
	spheroid = re.search(
		r"(?:SPHEROID|ELLIPSOID)\s*\[\s*\"([^\"]*)\"\s*,\s*([0-9.eE+-]+)\s*,\s*([0-9.eE+-]+)", raw
	)
	if spheroid:
		crs["ellipsoid"] = spheroid.group(1)
		crs["semi_major"] = float(spheroid.group(2))
		inverse_flattening = float(spheroid.group(3))
		crs["inverse_flattening"] = inverse_flattening if inverse_flattening else 0.0
	else:
		crs["ellipsoid"] = ""
		crs["semi_major"] = DEFAULT_SEMI_MAJOR
		crs["inverse_flattening"] = DEFAULT_INVERSE_FLATTENING

	if crs["kind"] == "geographic":
		return crs

	projection = re.search(r"PROJECTION\s*\[\s*\"([^\"]*)\"", raw)
	if not projection:
		projection = re.search(r"(?:CONVERSION|METHOD)\s*\[\s*\"([^\"]*)\"", raw)
	crs["projection_name"] = projection.group(1) if projection else ""
	crs["projection"] = _PROJECTIONS.get(_squash(crs["projection_name"]), "")

	params = {}
	for name, value in re.findall(r"PARAMETER\s*\[\s*\"([^\"]*)\"\s*,\s*([0-9.eE+-]+)", raw):
		canonical = _PARAMETER_INDEX.get(_squash(name))
		if canonical:
			params[canonical] = float(value)
	crs["params"] = params

	unit = re.findall(r"UNIT\s*\[\s*\"([^\"]*)\"\s*,\s*([0-9.eE+-]+)", raw)
	crs["unit_factor"] = _LINEAR_UNITS.get(_squash(unit[-1][0]), 0.0) if unit else 1.0
	if not crs["unit_factor"] and unit:
		# The WKT states the conversion to metres itself, which is the only
		# reason a unit this table has never heard of is still usable.
		try:
			crs["unit_factor"] = float(unit[-1][1])
		except (TypeError, ValueError):
			crs["unit_factor"] = 0.0
	crs["unit_name"] = unit[-1][0] if unit else "metre"
	return crs


def _wkt_name(raw: str) -> str:
	first = re.search(r"^\s*[A-Z_]+\s*\[\s*\"([^\"]*)\"", raw)
	return first.group(1) if first else ""


def _wkt_datum(raw: str) -> str:
	datum = re.search(r"DATUM\s*\[\s*\"([^\"]*)\"", raw)
	return datum.group(1) if datum else ""


def _ellipsoid(crs: dict) -> tuple:
	"""`(a, e², e)` from the spheroid the file names, defaulting to WGS84/GRS80."""
	semi_major = float(crs.get("semi_major") or DEFAULT_SEMI_MAJOR)
	inverse_flattening = float(crs.get("inverse_flattening") or 0.0)
	if inverse_flattening <= 0:
		return semi_major, 0.0, 0.0
	flattening = 1.0 / inverse_flattening
	eccentricity_squared = 2 * flattening - flattening * flattening
	return semi_major, eccentricity_squared, math.sqrt(eccentricity_squared)


def _clamp(value: float, limit: float = 1.0) -> float:
	return max(-limit, min(limit, value))


def _m(latitude: float, eccentricity_squared: float) -> float:
	"""Snyder 14-15. The radius of the parallel, scaled to the semi-major axis."""
	sine = math.sin(latitude)
	return math.cos(latitude) / math.sqrt(1 - eccentricity_squared * sine * sine)


def _q(latitude: float, eccentricity_squared: float, eccentricity: float) -> float:
	"""Snyder 3-12, the authalic quantity Albers is equal-area in."""
	sine = math.sin(latitude)
	if eccentricity < 1e-12:
		return 2 * sine
	return (1 - eccentricity_squared) * (
		sine / (1 - eccentricity_squared * sine * sine)
		- (1 / (2 * eccentricity)) * math.log((1 - eccentricity * sine) / (1 + eccentricity * sine))
	)


def _t(latitude: float, eccentricity: float) -> float:
	"""Snyder 15-9, the conformal quantity Lambert Conformal Conic works in."""
	sine = math.sin(latitude)
	base = math.tan(math.pi / 4 - latitude / 2)
	if eccentricity < 1e-12:
		return base
	return base / (((1 - eccentricity * sine) / (1 + eccentricity * sine)) ** (eccentricity / 2))


def _latitude_from_q(q: float, eccentricity_squared: float, eccentricity: float) -> float:
	"""Snyder 3-16, iterated. The inverse of `_q`, which has no closed form."""
	if eccentricity < 1e-12:
		return math.asin(_clamp(q / 2))
	q_pole = (1 - eccentricity_squared) * (
		1 / (1 - eccentricity_squared)
		- (1 / (2 * eccentricity)) * math.log((1 - eccentricity) / (1 + eccentricity))
	)
	if abs(abs(q) - q_pole) < 1e-9:
		return math.copysign(math.pi / 2, q)
	latitude = math.asin(_clamp(q / 2))
	for _ in range(30):
		sine = math.sin(latitude)
		cosine = math.cos(latitude)
		if abs(cosine) < 1e-12:
			break
		denominator = 1 - eccentricity_squared * sine * sine
		delta = (denominator * denominator / (2 * cosine)) * (
			q / (1 - eccentricity_squared)
			- sine / denominator
			+ (1 / (2 * eccentricity)) * math.log((1 - eccentricity * sine) / (1 + eccentricity * sine))
		)
		latitude += delta
		if abs(delta) < 1e-12:
			break
	return latitude


def _albers_inverse(crs: dict):
	"""USA Contiguous Albers Equal Area — EPSG:5070, and what NRCS ships in."""
	semi_major, eccentricity_squared, eccentricity = _ellipsoid(crs)
	params = crs.get("params") or {}
	parallel_1 = math.radians(params.get("standard_parallel_1", 0.0))
	parallel_2 = math.radians(params.get("standard_parallel_2", params.get("standard_parallel_1", 0.0)))
	origin = math.radians(params.get("latitude_of_origin", 0.0))
	meridian = math.radians(params.get("central_meridian", 0.0))
	false_easting = params.get("false_easting", 0.0)
	false_northing = params.get("false_northing", 0.0)

	m1 = _m(parallel_1, eccentricity_squared)
	m2 = _m(parallel_2, eccentricity_squared)
	q1 = _q(parallel_1, eccentricity_squared, eccentricity)
	q2 = _q(parallel_2, eccentricity_squared, eccentricity)
	q0 = _q(origin, eccentricity_squared, eccentricity)
	if abs(parallel_1 - parallel_2) < 1e-12:
		cone = math.sin(parallel_1)
	else:
		cone = (m1 * m1 - m2 * m2) / (q2 - q1)
	if abs(cone) < 1e-12:
		raise ToolError(
			"the .prj describes an Albers projection whose standard parallels give a cone "
			"constant of zero, which is not a projection anything can be un-projected from. "
			"Nothing was changed."
		)
	constant = m1 * m1 + cone * q1
	rho0 = semi_major * math.sqrt(max(0.0, constant - cone * q0)) / cone

	def inverse(x: float, y: float) -> tuple:
		east = x - false_easting
		north = rho0 - (y - false_northing)
		rho = math.hypot(east, north)
		if cone < 0:
			rho = -rho
			theta = math.atan2(-east, -north)
		else:
			theta = math.atan2(east, north)
		q = (constant - (rho * rho * cone * cone) / (semi_major * semi_major)) / cone
		latitude = _latitude_from_q(q, eccentricity_squared, eccentricity)
		return math.degrees(meridian + theta / cone), math.degrees(latitude)

	return inverse


def _lcc_inverse(crs: dict):
	"""Lambert Conformal Conic, which is most of the State Plane zones."""
	semi_major, eccentricity_squared, eccentricity = _ellipsoid(crs)
	params = crs.get("params") or {}
	origin = math.radians(params.get("latitude_of_origin", 0.0))
	meridian = math.radians(params.get("central_meridian", 0.0))
	false_easting = params.get("false_easting", 0.0)
	false_northing = params.get("false_northing", 0.0)
	parallel_1 = math.radians(params.get("standard_parallel_1", params.get("latitude_of_origin", 0.0)))
	has_second = "standard_parallel_2" in params
	parallel_2 = math.radians(params.get("standard_parallel_2", params.get("standard_parallel_1", 0.0)))

	t1 = _t(parallel_1, eccentricity)
	m1 = _m(parallel_1, eccentricity_squared)
	if not has_second or abs(parallel_1 - parallel_2) < 1e-12:
		cone = math.sin(parallel_1)
	else:
		t2 = _t(parallel_2, eccentricity)
		m2 = _m(parallel_2, eccentricity_squared)
		cone = math.log(m1 / m2) / math.log(t1 / t2)
	if abs(cone) < 1e-12 or t1 <= 0:
		raise ToolError(
			"the .prj describes a Lambert Conformal Conic whose standard parallel is the "
			"equator, which the projection is not defined at. Nothing was changed."
		)
	factor = m1 / (cone * (t1**cone))
	t0 = _t(origin, eccentricity)
	rho0 = semi_major * factor * (t0**cone)

	def inverse(x: float, y: float) -> tuple:
		east = x - false_easting
		north = rho0 - (y - false_northing)
		rho = math.copysign(math.hypot(east, north), cone)
		if cone < 0:
			theta = math.atan2(-east, -north)
		else:
			theta = math.atan2(east, north)
		if abs(rho) < 1e-12:
			return math.degrees(meridian + theta / cone), math.copysign(90.0, cone)
		t = (abs(rho) / (semi_major * abs(factor))) ** (1 / cone)
		latitude = math.pi / 2 - 2 * math.atan(t)
		for _ in range(30):
			sine = math.sin(latitude)
			adjusted = math.pi / 2 - 2 * math.atan(
				t * (((1 - eccentricity * sine) / (1 + eccentricity * sine)) ** (eccentricity / 2))
			)
			if abs(adjusted - latitude) < 1e-12:
				latitude = adjusted
				break
			latitude = adjusted
		return math.degrees(meridian + theta / cone), math.degrees(latitude)

	return inverse


def _tmerc_inverse(crs: dict):
	"""Transverse Mercator — every UTM zone, and the other State Plane half."""
	semi_major, eccentricity_squared, _eccentricity = _ellipsoid(crs)
	params = crs.get("params") or {}
	origin = math.radians(params.get("latitude_of_origin", 0.0))
	meridian = math.radians(params.get("central_meridian", 0.0))
	false_easting = params.get("false_easting", 0.0)
	false_northing = params.get("false_northing", 0.0)
	scale = params.get("scale_factor", 1.0)
	if not scale:
		scale = 1.0
	second_eccentricity = eccentricity_squared / (1 - eccentricity_squared) if eccentricity_squared else 0.0

	def meridional_arc(latitude: float) -> float:
		e2 = eccentricity_squared
		e4 = e2 * e2
		e6 = e4 * e2
		return semi_major * (
			(1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * latitude
			- (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * latitude)
			+ (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * latitude)
			- (35 * e6 / 3072) * math.sin(6 * latitude)
		)

	arc_origin = meridional_arc(origin)

	def inverse(x: float, y: float) -> tuple:
		e2 = eccentricity_squared
		arc = arc_origin + (y - false_northing) / scale
		mu = arc / (semi_major * (1 - e2 / 4 - 3 * e2 * e2 / 64 - 5 * e2 * e2 * e2 / 256))
		root = math.sqrt(1 - e2)
		e1 = (1 - root) / (1 + root)
		footprint = (
			mu
			+ (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
			+ (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
			+ (151 * e1**3 / 96) * math.sin(6 * mu)
			+ (1097 * e1**4 / 512) * math.sin(8 * mu)
		)
		cosine = math.cos(footprint)
		if abs(cosine) < 1e-12:
			return math.degrees(meridian), math.degrees(footprint)
		sine = math.sin(footprint)
		tangent = math.tan(footprint)
		c1 = second_eccentricity * cosine * cosine
		t1 = tangent * tangent
		n1 = semi_major / math.sqrt(1 - e2 * sine * sine)
		r1 = semi_major * (1 - e2) / ((1 - e2 * sine * sine) ** 1.5)
		d = (x - false_easting) / (n1 * scale)
		latitude = footprint - (n1 * tangent / r1) * (
			d * d / 2
			- (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * second_eccentricity) * d**4 / 24
			+ (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * second_eccentricity - 3 * c1 * c1) * d**6 / 720
		)
		longitude = meridian + (
			d
			- (1 + 2 * t1 + c1) * d**3 / 6
			+ (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * second_eccentricity + 24 * t1 * t1) * d**5 / 120
		) / math.cos(footprint)
		return math.degrees(longitude), math.degrees(latitude)

	return inverse


def _webmerc_inverse(crs: dict):
	"""Web Mercator, on the sphere of the semi-major axis, as EPSG:3857 defines."""
	semi_major, _, _ = _ellipsoid(crs)
	params = crs.get("params") or {}
	meridian = params.get("central_meridian", 0.0)
	false_easting = params.get("false_easting", 0.0)
	false_northing = params.get("false_northing", 0.0)

	def inverse(x: float, y: float) -> tuple:
		longitude = meridian + math.degrees((x - false_easting) / semi_major)
		latitude = math.degrees(2 * math.atan(math.exp((y - false_northing) / semi_major)) - math.pi / 2)
		return longitude, latitude

	return inverse


_INVERSES = {
	"albers": _albers_inverse,
	"lcc": _lcc_inverse,
	"tmerc": _tmerc_inverse,
	"webmerc": _webmerc_inverse,
}


def transformer(crs: dict):
	"""`(function, warnings)` turning this file's coordinates into `(lon, lat)`.

	A geographic system passes straight through. A projected one is inverted
	through the formulae above. Anything else raises, and NAMES WHAT IT FOUND —
	"this is Oregon State Plane North, which I cannot un-project" is a sentence
	somebody can act on; a polygon quietly landing in the Atlantic is not.
	"""
	warnings = []
	datum = str(crs.get("datum") or "")
	if re.search(r"1927|NAD27", datum, flags=re.IGNORECASE):
		warnings.append(
			f"the .prj says {datum} (NAD27), and no datum shift is applied — these boundaries "
			"will sit tens of metres from where NAD83/WGS84 puts the same ground. Re-export "
			"from FSA in NAD83 if the offset matters, which for a spray record it does."
		)

	kind = crs.get("kind")
	if kind in ("geographic", "missing"):
		return (lambda x, y: (x, y)), warnings

	family = crs.get("projection")
	if kind == "projected" and family in _INVERSES:
		factor = float(crs.get("unit_factor") or 0.0)
		if not factor:
			raise ToolError(
				f"the .prj is in {crs.get('unit_name') or 'a linear unit'}, which this cannot "
				"convert to metres. Re-export in metres or in degrees. Nothing was changed."
			)
		# THE FALSE ORIGIN IS IN THE FILE'S OWN UNIT, NOT IN METRES. A State Plane
		# zone published in US survey feet states a false easting of 1,640,416.667
		# FEET, and subtracting that from a coordinate already converted to metres
		# puts the farm about 1,100 km from where it is. The offsets are converted
		# with the coordinates, once, here — every formula below then works in
		# metres throughout.
		metric = dict(crs)
		metric["params"] = {
			name: (value * factor if name in ("false_easting", "false_northing") else value)
			for name, value in (crs.get("params") or {}).items()
		}
		inverse = _INVERSES[family](metric)

		def convert(x: float, y: float) -> tuple:
			return inverse(x * factor, y * factor)

		return convert, warnings

	found = crs.get("projection_name") or crs.get("name") or "an unnamed coordinate system"
	raise ToolError(
		f"the shapefile is projected as {found!r}, which this cannot un-project. The four it "
		"knows are Albers Equal Area (what FSA and NRCS use), Lambert Conformal Conic, "
		"Transverse Mercator (every UTM zone) and Web Mercator. Re-export the layer as "
		"geographic NAD83 or WGS84 — in QGIS that is Export → Save Features As with CRS "
		"EPSG:4326 — and import that. A boundary transformed by a guess is worse than one "
		"that was never transformed. Nothing was changed."
	)


def looks_like_degrees(points) -> bool:
	"""Could these coordinates be longitude/latitude? A superset test, on purpose."""
	for x, y in points:
		if not -180.0 <= x <= 180.0 or not -90.0 <= y <= 90.0:
			return False
	return True


# ── the shapefile itself ────────────────────────────────────────────────────
def read_shp(blob: bytes) -> dict:
	"""Every polygon in a `.shp`, in the file's own coordinates.

	Returns `{"shape_type": int, "shapes": [[ring, ...], ...]}` where a shape is
	its list of rings and a ring is a list of `(x, y)`. A null shape is kept as
	an EMPTY LIST rather than dropped, because the `.dbf` has a row for it and
	the two files are matched by position — dropping one here would slide every
	attribute after it onto the wrong field.
	"""
	if len(blob) < SHP_HEADER_BYTES:
		raise ToolError("the .shp is too short to be a shapefile. Nothing was changed.")
	(magic,) = struct.unpack(">i", blob[0:4])
	if magic != SHP_MAGIC:
		raise ToolError(
			"that file does not start with the shapefile signature. If it came out of a zip, "
			"the .shp inside it is the one to send. Nothing was changed."
		)
	(declared_words,) = struct.unpack(">i", blob[24:28])
	(file_type,) = struct.unpack("<i", blob[32:36])
	end = min(len(blob), max(SHP_HEADER_BYTES, declared_words * 2))

	shapes = []
	offset = SHP_HEADER_BYTES
	while offset + 8 <= end:
		_, content_words = struct.unpack(">ii", blob[offset : offset + 8])
		offset += 8
		content_bytes = content_words * 2
		if content_bytes <= 0 or offset + content_bytes > len(blob):
			break
		record = blob[offset : offset + content_bytes]
		offset += content_bytes
		shapes.append(_read_shape_record(record, file_type))
		if len(shapes) > MAX_FEATURES:
			raise ToolError(
				f"the shapefile holds more than {MAX_FEATURES} shapes. Split it by county or by "
				"farm and import the pieces, so you can check what each one did. Nothing was changed."
			)
	return {"shape_type": file_type, "shapes": shapes}


def _read_shape_record(record: bytes, file_type: int) -> list:
	"""The rings of one record, or `[]` for a null shape."""
	if len(record) < 4:
		return []
	(shape_type,) = struct.unpack("<i", record[0:4])
	if shape_type == 0:
		return []
	if shape_type not in POLYGON_TYPES:
		name = SHAPE_TYPES.get(shape_type, str(shape_type))
		declared = SHAPE_TYPES.get(file_type, str(file_type))
		raise ToolError(
			f"the shapefile holds {name} shapes (the header says {declared}), and a field "
			"boundary has to be an area. A CLU layer is a polygon layer — this looks like the "
			"points or the tract lines rather than the fields. Nothing was changed."
		)
	if len(record) < 44:
		return []
	part_count, point_count = struct.unpack("<ii", record[36:44])
	if part_count <= 0 or point_count <= 0:
		return []
	if part_count > MAX_PARTS or point_count > MAX_POINTS:
		raise ToolError(
			f"a shape in this file declares {part_count} part(s) and {point_count} point(s), "
			"which is not a field boundary. The file is probably truncated. Nothing was changed."
		)
	parts_end = 44 + 4 * part_count
	points_end = parts_end + 16 * point_count
	if points_end > len(record):
		raise ToolError("a shape in this file is truncated part-way through. Nothing was changed.")
	starts = list(struct.unpack(f"<{part_count}i", record[44:parts_end]))
	flat = struct.unpack(f"<{2 * point_count}d", record[parts_end:points_end])

	rings = []
	for index, start in enumerate(starts):
		stop = starts[index + 1] if index + 1 < len(starts) else point_count
		if start < 0 or stop > point_count or stop <= start:
			continue
		rings.append([(flat[2 * position], flat[2 * position + 1]) for position in range(start, stop)])
	return rings


def _close(ring: list) -> list:
	"""A ring that comes back to its own first point, which GeoJSON requires."""
	if len(ring) >= 2 and ring[0] != ring[-1]:
		return [*ring, ring[0]]
	return ring


def _signed_area(ring: list) -> float:
	"""Twice the shoelace area. POSITIVE is counter-clockwise."""
	total = 0.0
	for index in range(len(ring) - 1):
		x1, y1 = ring[index]
		x2, y2 = ring[index + 1]
		total += x1 * y2 - x2 * y1
	return total / 2.0


def _inside(point: tuple, ring: list) -> bool:
	"""Ray casting, used only to sanity-check which outer ring a hole belongs to."""
	x, y = point
	inside = False
	for index in range(len(ring) - 1):
		x1, y1 = ring[index]
		x2, y2 = ring[index + 1]
		if (y1 > y) != (y2 > y):
			crossing = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
			if crossing > x:
				inside = not inside
	return inside


def rings_to_geometry(rings: list, precision: int = 7) -> dict | None:
	"""A GeoJSON Polygon or MultiPolygon from one shapefile record's rings.

	A shapefile record is a FLAT LIST OF RINGS with nothing saying which is a
	field and which is a hole in one. The specification's answer is winding — an
	outer ring runs clockwise, a hole runs counter-clockwise — and that answer
	cannot be relied on, because half the tools that write shapefiles do not
	enforce it and a farm would only find out when a ten-acre block came back as
	two.

	SO NESTING IS DECIDED BY CONTAINMENT AND NOT BY WINDING. A ring inside an odd
	number of other rings is a hole in the smallest one containing it; a ring
	inside an even number is a field in its own right, which is what an island in
	the middle of a wetland exclusion actually is. Winding is then IMPOSED on the
	way out, counter-clockwise for an outer ring and clockwise for a hole, as RFC
	7946 requires — so a file that had it backwards comes out right rather than
	coming out inverted.
	"""
	closed = [_close(ring) for ring in rings]
	closed = [ring for ring in closed if len(ring) >= 4]
	if not closed:
		return None
	if len(closed) == 1:
		return _render_polygons([closed], precision)

	areas = [abs(_signed_area(ring)) for ring in closed]
	order = sorted(range(len(closed)), key=lambda index: areas[index], reverse=True)
	parents = {}
	for position, index in enumerate(order):
		# Only a LARGER ring can contain this one, and the largest containing
		# ring is not the parent — the smallest is, which is why this walks the
		# already-placed rings from smallest to largest and stops at the first hit.
		for candidate in reversed(order[:position]):
			if _inside(closed[index][0], closed[candidate]):
				parents[index] = candidate
				break

	depth = {}
	for index in order:
		parent = parents.get(index)
		depth[index] = 0 if parent is None else depth[parent] + 1

	polygons = []
	slots = {}
	for index in order:
		if depth[index] % 2 == 0:
			slots[index] = len(polygons)
			polygons.append([closed[index]])
	for index in order:
		if depth[index] % 2 == 1:
			polygons[slots[parents[index]]].append(closed[index])
	return _render_polygons(polygons, precision)


# ── the .dbf, which is where every CLU attribute lives ──────────────────────
def read_dbf(blob: bytes) -> list:
	"""Every row of a dBase III/IV table as a dict, in file order.

	dBase is a fixed-width format with a 32-byte header, a 32-byte descriptor per
	column and a one-byte deletion flag in front of each row. Memo (`M`) columns
	point into a separate `.dbt` file nobody sends; they come back as None rather
	than as the raw pointer.
	"""
	if len(blob) < 32:
		raise ToolError("the .dbf is too short to be a dBase table. Nothing was changed.")
	record_count, header_bytes, record_bytes = struct.unpack("<iHH", blob[4:12])
	if header_bytes < 33 or record_bytes <= 0:
		raise ToolError("the .dbf header does not describe a table. Nothing was changed.")

	columns = []
	offset = 32
	while offset + 32 <= header_bytes and blob[offset] not in (0x0D, 0x00):
		descriptor = blob[offset : offset + 32]
		name = descriptor[0:11].split(b"\x00")[0].decode("ascii", "replace").strip()
		kind = chr(descriptor[11])
		width = descriptor[16]
		decimals = descriptor[17]
		columns.append((name, kind, width, decimals))
		offset += 32
	if not columns:
		raise ToolError("the .dbf declares no columns. Nothing was changed.")

	encoding = "cp1252" if blob[29] in (0x03, 0x57) else "utf-8"
	rows = []
	position = header_bytes
	for _ in range(max(0, record_count)):
		if position + record_bytes > len(blob):
			break
		record = blob[position : position + record_bytes]
		position += record_bytes
		if record[0:1] == b"*":  # deleted, but still counted by the .shp
			rows.append({})
			continue
		row = {}
		cursor = 1
		for name, kind, width, decimals in columns:
			raw = record[cursor : cursor + width]
			cursor += width
			row[name] = _dbf_value(raw, kind, decimals, encoding)
		rows.append(row)
	return rows


def _dbf_value(raw: bytes, kind: str, decimals: int, encoding: str):
	"""One dBase cell, typed. Empty is None — dBase pads with spaces, not nulls."""
	try:
		text = raw.decode(encoding, "replace").strip()
	except Exception:
		text = raw.decode("latin-1", "replace").strip()
	if not text:
		return None
	if kind in ("N", "F", "B", "O"):
		try:
			return float(text) if decimals else int(text)
		except ValueError:
			try:
				return float(text)
			except ValueError:
				return text
	if kind == "D":
		digits = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
		return f"{digits.group(1)}-{digits.group(2)}-{digits.group(3)}" if digits else text
	if kind == "L":
		return {"T": True, "Y": True, "F": False, "N": False}.get(text[0].upper())
	if kind == "M":
		return None
	return text


def _render_polygons(polygons: list, precision: int) -> dict | None:
	"""Rounded, RFC 7946-wound GeoJSON from `[[outer, hole, ...], ...]`."""
	shaped = []
	for polygon in polygons:
		rendered = []
		for index, ring in enumerate(polygon):
			ring = _close(ring)
			if len(ring) < 4:
				continue
			wants_counter_clockwise = index == 0
			if (_signed_area(ring) < 0) is wants_counter_clockwise:
				ring = list(reversed(ring))
			rendered.append([[round(x, precision), round(y, precision)] for x, y in ring])
		if rendered:
			shaped.append(rendered)
	if not shaped:
		return None
	if len(shaped) == 1:
		return {"type": "Polygon", "coordinates": shaped[0]}
	return {"type": "MultiPolygon", "coordinates": shaped}


# ── KML and KMZ, which is what "export for Google Earth" produces ───────────
#
# NO DOCTYPE AND NO ENTITY DECLARATIONS ARE ACCEPTED, and the check is a string
# search before the parser is handed anything. `xml.etree` expands internal
# entities, so a forty-line file declaring nested entities expands to gigabytes
# inside the request — the billion laughs. Nothing FSA or Google Earth writes
# contains either construct, so refusing them costs no real file anything.
_XML_REFUSED = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def read_kml(blob: bytes) -> list:
	"""Every Placemark with an area, as `{"geometry":..., "properties":...}`."""
	if _XML_REFUSED.search(blob[:65536]):
		raise ToolError(
			"the KML declares a DOCTYPE or an XML entity. Neither belongs in a boundary file "
			"and both are how an XML parser is made to consume a machine. Nothing was changed."
		)
	try:
		root = ElementTree.fromstring(blob.decode("utf-8", "replace"))
	except ElementTree.ParseError as error:
		raise ToolError(f"the KML is not well-formed XML: {error}. Nothing was changed.") from None

	features = []
	for placemark in root.iter():
		if not str(placemark.tag).endswith("Placemark"):
			continue
		polygons = []
		for element in placemark.iter():
			if not str(element.tag).endswith("Polygon"):
				continue
			rings = []
			for boundary in element.iter():
				tag = str(boundary.tag)
				if not tag.endswith("coordinates"):
					continue
				ring = _kml_coordinates(boundary.text)
				if len(ring) >= 3:
					rings.append(ring)
			if rings:
				polygons.append(rings)
		geometry = _render_polygons(polygons, 7)
		if not geometry:
			continue
		features.append({"geometry": geometry, "properties": _kml_properties(placemark)})
		if len(features) > MAX_FEATURES:
			raise ToolError(
				f"the KML holds more than {MAX_FEATURES} placemarks. Split it and import the "
				"pieces, so you can check what each one did. Nothing was changed."
			)
	return features


def _kml_coordinates(text) -> list:
	"""`lon,lat[,alt] lon,lat[,alt] …` — KML is always WGS84 degrees."""
	points = []
	for chunk in str(text or "").replace("\n", " ").replace("\t", " ").split(" "):
		chunk = chunk.strip()
		if not chunk:
			continue
		parts = chunk.split(",")
		if len(parts) < 2:
			continue
		try:
			points.append((float(parts[0]), float(parts[1])))
		except ValueError:
			continue
	return points


def _kml_properties(placemark) -> dict:
	"""A Placemark's name and its ExtendedData, which is where a CLU's columns go."""
	properties = {}
	for child in placemark:
		if str(child.tag).endswith("name") and (child.text or "").strip():
			properties["name"] = child.text.strip()
	for element in placemark.iter():
		tag = str(element.tag)
		if tag.endswith("SimpleData") or tag.endswith("Data"):
			key = element.get("name")
			if not key:
				continue
			if tag.endswith("SimpleData"):
				properties[key] = (element.text or "").strip()
				continue
			for child in element:
				if str(child.tag).endswith("value"):
					properties[key] = (child.text or "").strip()
	return properties


# ── GeoJSON, from a grower whose agronomist already converted it ────────────
def read_geojson(blob: bytes) -> tuple:
	"""`(features, crs)` from a FeatureCollection, a Feature or a bare geometry."""
	try:
		payload = json.loads(blob.decode("utf-8", "replace"))
	except (json.JSONDecodeError, UnicodeDecodeError) as error:
		raise ToolError(f"that file is not valid JSON: {error}. Nothing was changed.") from None
	return features_from_geojson(payload)


def features_from_geojson(payload) -> tuple:
	"""The same, from an object a caller passed in rather than a file."""
	if not isinstance(payload, dict):
		raise ToolError("the GeoJSON must be an object. Nothing was changed.")
	crs = _geojson_crs(payload)
	kind = str(payload.get("type") or "")
	if kind == "FeatureCollection":
		raw = payload.get("features")
		if not isinstance(raw, list):
			raise ToolError("the FeatureCollection has no `features` array. Nothing was changed.")
	elif kind == "Feature":
		raw = [payload]
	elif kind in ("Polygon", "MultiPolygon"):
		raw = [{"type": "Feature", "geometry": payload, "properties": {}}]
	else:
		raise ToolError(
			f"the GeoJSON is a {kind or 'thing with no type'}; this reads a FeatureCollection, "
			"a Feature, or a Polygon. Nothing was changed."
		)
	if len(raw) > MAX_FEATURES:
		raise ToolError(
			f"the collection holds {len(raw)} features, more than the {MAX_FEATURES} this takes "
			"in one call. Nothing was changed."
		)

	features = []
	for entry in raw:
		if not isinstance(entry, dict):
			continue
		geometry = entry.get("geometry") if entry.get("type") == "Feature" else entry
		properties = entry.get("properties")
		features.append(
			{
				"geometry": geometry if isinstance(geometry, dict) else None,
				"properties": dict(properties) if isinstance(properties, dict) else {},
			}
		)
	return features, crs


def _geojson_crs(payload: dict) -> dict:
	"""RFC 7946 says GeoJSON is WGS84 degrees. The 2008 draft let a file say otherwise.

	Exports still carry the old `crs` member, and one naming EPSG:5070 is a file
	full of metres wearing a format that promises degrees. It is read rather than
	ignored, because ignoring it is how a farm lands off the coast of Africa.
	"""
	member = payload.get("crs")
	if not isinstance(member, dict):
		return {"kind": "geographic", "name": "WGS 84", "warnings": []}
	name = str(
		((member.get("properties") or {}) if isinstance(member.get("properties"), dict) else {}).get("name")
		or ""
	)
	code = re.search(r"(\d{4,6})$", name)
	if not code or name.upper().endswith("CRS84"):
		return {"kind": "geographic", "name": name or "WGS 84", "warnings": []}
	known = _EPSG_CODES.get(code.group(1))
	if known:
		return {**known, "warnings": [], "unit_factor": 1.0}
	return {"kind": "unknown", "name": name, "warnings": []}


# ── the front door: bytes in, a FeatureCollection out ───────────────────────
def decode_upload(text, label: str = "file_base64") -> bytes:
	"""Base64 from a caller or a browser, with the data-URI prefix taken off."""
	raw = str(text or "").strip()
	if not raw:
		raise ToolError(f"{label} is required (the file, base64-encoded). Nothing was changed.")
	raw = re.sub(r"^data:[^;]*;base64,", "", raw)
	raw = re.sub(r"\s+", "", raw)
	if len(raw) > MAX_BYTES * 4 // 3 + 1024:
		raise ToolError(
			f"that upload is larger than the {MAX_BYTES // (1024 * 1024)} MB this takes. A whole "
			"farm's CLU set is tens of kilobytes — this is probably an imagery layer rather than "
			"boundaries. Nothing was changed."
		)
	try:
		blob = base64.b64decode(raw, validate=True)
	except (binascii.Error, ValueError) as error:
		raise ToolError(f"{label} is not valid base64: {error}. Nothing was changed.") from None
	if not blob:
		raise ToolError(f"{label} decoded to nothing. Nothing was changed.")
	if len(blob) > MAX_BYTES:
		raise ToolError(
			f"that file is {len(blob) // (1024 * 1024)} MB, over the {MAX_BYTES // (1024 * 1024)} MB "
			"limit. Nothing was changed."
		)
	return blob


def _members(archive: zipfile.ZipFile) -> dict:
	"""The pieces of a shapefile set inside a zip, keyed by extension.

	`__MACOSX` is skipped: a zip made on a Mac carries a second copy of every
	file in there, and picking one of those up gives "the .shp is too short".
	"""
	found = {}
	total = 0
	for info in archive.infolist():
		name = info.filename
		if info.is_dir() or name.startswith("__MACOSX/") or "/._" in name or name.startswith("._"):
			continue
		total += info.file_size
		if total > MAX_BYTES:
			raise ToolError(
				f"the zip expands to more than {MAX_BYTES // (1024 * 1024)} MB. Nothing was changed."
			)
		extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
		found.setdefault(extension, []).append(name)
	return found


def read(blob: bytes, filename: str = "") -> dict:
	"""Whatever the office gave you, as features in WGS84 degrees.

	Returns the format it turned out to be, the coordinate system it was in, the
	files it read, any warnings, and the features themselves with their original
	attribute columns untouched under `properties`.
	"""
	warnings = []
	source_files = [filename] if filename else []

	if blob[:4] == b"PK\x03\x04":
		try:
			archive = zipfile.ZipFile(BytesIO(blob))
		except zipfile.BadZipFile as error:
			raise ToolError(f"that zip cannot be opened: {error}. Nothing was changed.") from None
		members = _members(archive)
		if members.get("shp"):
			if len(members["shp"]) > 1:
				raise ToolError(
					f"the zip holds {len(members['shp'])} shapefiles ({', '.join(sorted(members['shp']))}). "
					"Send the one with the CLU boundaries in it. Nothing was changed."
				)
			shp_name = members["shp"][0]
			stem = shp_name[:-4]
			return _read_shapefile_set(
				archive.read(shp_name),
				_sidecar(archive, members, stem, "dbf"),
				_sidecar(archive, members, stem, "prj"),
				source_files=sorted(
					name for names in members.values() for name in names if name.startswith(stem)
				),
			)
		for extension in ("geojson", "json", "kml"):
			if members.get(extension):
				inner = archive.read(members[extension][0])
				payload = read(inner, members[extension][0])
				payload["source_files"] = [members[extension][0]]
				return payload
		raise ToolError(
			"that zip has no .shp, .geojson or .kml in it — it holds "
			f"{', '.join(sorted(members)) or 'nothing this reads'}. FSA's export is a zip with "
			"a .shp, .shx, .dbf and .prj in it. Nothing was changed."
		)

	if len(blob) >= 4 and struct.unpack(">i", blob[0:4])[0] == SHP_MAGIC:
		raise ToolError(
			"that is a bare .shp, and a .shp on its own is only the geometry — the tract and "
			"field numbers are in the .dbf beside it and the coordinate system is in the .prj. "
			"Zip the whole set together (.shp, .shx, .dbf, .prj) and send that. Nothing was changed."
		)

	head = blob[:512].lstrip()
	if head[:1] in (b"{", b"["):
		features, crs = read_geojson(blob)
		return _finish(features, crs, "geojson", source_files, warnings, reproject=True)
	if head[:1] == b"<":
		return _finish(
			read_kml(blob),
			{"kind": "geographic", "name": "WGS 84"},
			"kml",
			source_files,
			warnings,
			reproject=False,
		)

	raise ToolError(
		"that file is none of the four this reads. FSA gives out a zipped shapefile (.shp with "
		"its .dbf and .prj), a KML or KMZ, or GeoJSON — send one of those. Nothing was changed."
	)


def _sidecar(archive, members: dict, stem: str, extension: str):
	"""The `.dbf` or `.prj` belonging to a particular `.shp` inside a zip.

	Matched on the stem, not on "the only one": a zip holding both the CLU layer
	and the tract layer has two of each, and pairing a CLU geometry with the
	tract attributes files every field under the wrong number.
	"""
	preferred = f"{stem}.{extension}"
	for name in members.get(extension, []):
		if name == preferred or name.lower() == preferred.lower():
			return archive.read(name)
	return None


def _read_shapefile_set(shp: bytes, dbf, prj, source_files: list) -> dict:
	"""The three files together: geometry, attributes and coordinate system."""
	warnings = []
	crs = parse_prj(prj.decode("utf-8", "replace") if isinstance(prj, bytes) else (prj or ""))
	geometry = read_shp(shp)
	rows = read_dbf(dbf) if dbf else []
	if not dbf:
		warnings.append(
			"there is no .dbf in the set, so the shapes arrived with no tract or field numbers "
			"on them. Every field will have to be matched by hand."
		)
	if rows and len(rows) != len(geometry["shapes"]):
		warnings.append(
			f"the .shp holds {len(geometry['shapes'])} shape(s) and the .dbf {len(rows)} row(s). "
			"They are matched by position, so the shorter of the two is what was read."
		)

	flat = [point for shape in geometry["shapes"] for ring in shape for point in ring]
	if crs["kind"] == "missing":
		if flat and looks_like_degrees(flat):
			warnings.append(
				"there is no .prj in the set. The coordinates are within longitude/latitude "
				"range, so they were read as WGS84 degrees — check one field against the map "
				"before you trust the lot."
			)
			crs = {**crs, "kind": "geographic", "name": "assumed WGS 84"}
		else:
			raise ToolError(
				"there is no .prj in the set and the coordinates are not longitude/latitude, so "
				"there is no way to tell where on Earth these shapes are. Ask the office for the "
				".prj — it is one line of text. Nothing was changed."
			)
	convert, crs_warnings = transformer(crs)
	warnings.extend(crs_warnings)

	features = []
	for index, rings in enumerate(geometry["shapes"]):
		properties = dict(rows[index]) if index < len(rows) else {}
		if not rings:
			features.append({"geometry": None, "properties": properties})
			continue
		converted = [[convert(x, y) for x, y in ring] for ring in rings]
		features.append({"geometry": rings_to_geometry(converted), "properties": properties})

	return _finish(features, crs, "shapefile", source_files, warnings, reproject=False)


def _finish(features: list, crs: dict, fmt: str, source_files: list, warnings: list, reproject: bool) -> dict:
	"""Common tail: reproject if the caller has not already, then report.

	`reproject` is False for the shapefile path because `_read_shapefile_set`
	converts as it reads — it has the rings in hand and turns them into GeoJSON
	in the same pass. It is True for a GeoJSON file carrying a 2008-style `crs`
	member that names something other than degrees, which is the one other way a
	file can arrive projected.
	"""
	if reproject and crs.get("kind") in ("projected", "unknown"):
		convert, extra = transformer(crs)
		warnings.extend(extra)
		for feature in features:
			feature["geometry"] = _reproject_geometry(feature.get("geometry"), convert)
	return {
		"format": fmt,
		"crs": {
			"kind": crs.get("kind"),
			"name": crs.get("name") or "",
			"datum": crs.get("datum") or "",
			"projection": crs.get("projection_name") or crs.get("projection") or "",
			"unit": crs.get("unit_name") or ("degree" if crs.get("kind") == "geographic" else ""),
		},
		"source_files": source_files,
		"warnings": warnings,
		"features": features,
	}


def _reproject_geometry(geometry, convert):
	"""A Polygon or MultiPolygon with every position run through `convert`."""
	if not isinstance(geometry, dict):
		return geometry
	kind = geometry.get("type")
	coordinates = geometry.get("coordinates")
	if kind == "Polygon" and isinstance(coordinates, list):
		rings = [[convert(float(x), float(y)) for x, y, *_ in ring] for ring in coordinates]
		return _render_polygons([rings], 7)
	if kind == "MultiPolygon" and isinstance(coordinates, list):
		polygons = [
			[[convert(float(x), float(y)) for x, y, *_ in ring] for ring in polygon]
			for polygon in coordinates
		]
		return _render_polygons(polygons, 7)
	return geometry


# ── what a CLU says about itself ────────────────────────────────────────────
def canonical_attributes(properties: dict) -> dict:
	"""FSA's columns under this app's names, whatever the file spelled them.

	Every key in `_ALIASES` comes back, set to None where the file had nothing,
	so a caller never has to ask whether a key is present before reading it. The
	original columns are NOT modified or removed — they stay on the feature.
	"""
	found = dict.fromkeys(_ALIASES)
	if not isinstance(properties, dict):
		return found
	for key, value in properties.items():
		canonical = _ALIAS_INDEX.get(_squash(key))
		if not canonical or value is None or value == "":
			continue
		if found.get(canonical) is None:
			found[canonical] = value

	for key in _IDENTIFIER_KEYS:
		found[key] = _identifier(found.get(key))
	found["clu_identifier"] = _guid(found.get("clu_identifier"))
	found["calc_acres"] = _acres(found.get("calc_acres"))
	for key in ("state_code", "county_code", "hel_type", "classification_code"):
		found[key] = _text(found.get(key))
	found["field_name"] = _text(found.get("field_name"))
	found["parcel_hint"] = _text(found.get("parcel_hint"))
	found["source_date"] = _text(found.get("source_date"))
	return found


def _identifier(value):
	"""A tract, farm or field number as the office says it out loud.

	`1234.0` (dBase numeric), `"0012"` (zero-padded text) and `12` (JSON number)
	are the same tract, and if they do not normalise to one string then a second
	import creates a second copy of every field on it.
	"""
	if value is None or value == "":
		return None
	if isinstance(value, bool):
		return None
	if isinstance(value, float):
		return str(int(value)) if value.is_integer() else str(value)
	if isinstance(value, int):
		return str(value)
	text = str(value).strip()
	if not text:
		return None
	if re.fullmatch(r"\d+", text):
		return str(int(text))
	if re.fullmatch(r"\d+\.0+", text):
		return str(int(float(text)))
	return text.upper()


def _guid(value):
	"""A CLU identifier, braces off and upper-cased, because ArcGIS varies both."""
	if value is None:
		return None
	text = _GUID_TRIM.sub("", str(value).strip())
	text = _GUID_TRIM.sub("", text)
	return text.upper() or None


def _acres(value):
	if value is None or value == "":
		return None
	try:
		acres = round(float(value), 2)
	except (TypeError, ValueError):
		return None
	return acres if acres >= 0 else None


def _text(value):
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def clu_key(attributes: dict) -> str:
	"""The stable name for one CLU, for reporting and for spotting duplicates.

	The GUID when there is one — it is the only identifier FSA guarantees — and
	tract/field otherwise, which is what the office and the grower both actually
	say.
	"""
	identifier = attributes.get("clu_identifier")
	if identifier:
		return identifier
	tract = attributes.get("tract_number")
	number = attributes.get("clu_number")
	if tract and number:
		return f"tract {tract} field {number}"
	if number:
		return f"field {number}"
	return attributes.get("field_name") or "unidentified CLU"


def suggested_field_name(attributes: dict) -> str:
	"""What a block created from this CLU would be called.

	`T1234-3` rather than the GUID: a crew leader has to read it off a task, and
	the tract and field number are what is written on the farm's own maps and on
	every acreage report it has ever filed.
	"""
	explicit = attributes.get("field_name")
	if explicit:
		return str(explicit)
	tract = attributes.get("tract_number")
	number = attributes.get("clu_number")
	if tract and number:
		return f"T{tract}-{number}"
	if tract:
		return f"T{tract}"
	if number:
		return f"Field {number}"
	identifier = attributes.get("clu_identifier")
	return f"CLU {identifier[:8]}" if identifier else ""


def to_feature_collection(payload: dict, parcel: str = "", tract_parcels: dict | None = None) -> dict:
	"""The parsed file as a FeatureCollection `import_field_boundary_geojson` takes.

	That tool has wanted `properties.field_name` and `properties.parcel_hint`
	since v0.31.0 and a CLU has neither, which is the whole reason a file from
	the office cannot simply be fed to it. This is the translation: every feature
	keeps its FSA columns, gains the two keys that tool matches on, and can then
	go through the generic importer unchanged by anybody who prefers it.
	"""
	tract_parcels = {_identifier(key): value for key, value in (tract_parcels or {}).items()}
	features = []
	for feature in payload.get("features") or []:
		attributes = canonical_attributes(feature.get("properties") or {})
		hint = attributes.get("parcel_hint") or tract_parcels.get(attributes.get("tract_number")) or parcel
		properties = {
			**{key: value for key, value in (feature.get("properties") or {}).items()},
			**{f"fsa_{key}": value for key, value in attributes.items() if value is not None},
			"field_name": suggested_field_name(attributes),
		}
		if hint:
			properties["parcel_hint"] = hint
		features.append({"type": "Feature", "geometry": feature.get("geometry"), "properties": properties})
	return {"type": "FeatureCollection", "features": features}
