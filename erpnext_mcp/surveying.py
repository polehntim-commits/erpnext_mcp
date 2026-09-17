# SPDX-License-Identifier: MIT
"""Bearings, distances, closure and draft metes-and-bounds text.

v0.171.0. The land map page (`/app/land-map`) lets somebody trace a proposed lot
line on satellite imagery. This module turns that string of clicks into the
numbers and the language a surveyor works from: a bearing and a distance for
every course, the area enclosed, how badly the figure fails to close, and a
draft description that reads the way a deed reads.

────────────────────────────────────────────────────────────────────────────
IT IS A DRAFT FOR A SURVEYOR AND IT SAYS SO IN ITS OWN OUTPUT
────────────────────────────────────────────────────────────────────────────

Nothing here is a survey. A monument on the ground beats a click on an aerial
photograph every time, imagery is orthorectified to about a metre at best, and a
lot line adjustment is recorded off a licensed surveyor's map. So every
description this module writes CARRIES ITS OWN DISCLAIMER as the last paragraph,
and `legal_description` will not produce text without one — the disclaimer is
part of the return value, not a decoration the caller may drop. The value of the
draft is that it turns a conversation ("we'd move the line to the far side of
the shed") into a document a surveyor can price and correct, with the acreage
and the courses roughly right before anybody drives out.

────────────────────────────────────────────────────────────────────────────
THE ARITHMETIC
────────────────────────────────────────────────────────────────────────────

`inverse` is Vincenty's inverse formula on the WGS84 ellipsoid: the azimuth and
the geodesic distance between two points, accurate to under a millimetre over a
lot line. It does not converge for nearly antipodal points, which cannot arise
between two corners of one farm; if it ever does, the haversine answer is
returned and `method` says which one you are reading. Every azimuth is a GRID
azimuth from true north, NOT magnetic and NOT a state plane grid bearing — a
deed written to a state plane basis of bearings differs from true north by the
mapping angle, which is the surveyor's job and is named in the disclaimer.

`quadrant` renders an azimuth the way a deed does — `N 45°30'15" E` — and the
rounding is done ONCE, in total seconds, then carried. Rounding degrees, minutes
and seconds separately is how `59'60"` gets printed.

DISTANCES ARE INTERNATIONAL FEET (0.3048 m exactly), and the metres are returned
alongside every one of them. The US survey foot was retired at the end of 2022
and differs by two parts per million — a hundredth of a foot in a mile — which
matters for a record of survey and not for a sketch. The unit is named in the
package so a surveyor is never guessing which foot this is.

AREA COMES FROM `geo.area_acres` AND IS NOT COMPUTED HERE. That function has
been the app's one answer to "how big is this shape" since v0.12.0, and a second
implementation in a different module is the drift that ends with two acreages
and no way to tell which is wrong. The same rule `geo_map_widget.js` states for
the browser applies to this file.

CLOSURE IS REPORTED, NEVER SILENTLY FIXED. A traverse drawn by clicking will not
close; the error and the precision ratio (`1 part in 4,300`) say by how much. A
description is written from the points as drawn, with the closing course back to
the point of beginning stated explicitly, because that is what a surveyor
checks.

────────────────────────────────────────────────────────────────────────────
THE POINT OF BEGINNING
────────────────────────────────────────────────────────────────────────────

A deed begins from something somebody can find. This module ties the first drawn
point to the NEAREST CORNER OF A RECORD THIS APP ALREADY HOLDS — a tax lot
polygon's vertex, a parcel's, a field's — and describes that corner by its
compass position within its own shape ("the northwest corner of Tax Lot 200").

IT DOES NOT NAME A PLSS CORNER, and that is a limitation rather than an
oversight. A section corner is a monument in the BLM's Geographic Coordinate
Data Base; this app ships no PLSS layer and does not fetch one, so a description
that said "from the NW corner of Section 7" would be an invention with a
surveyor's word for it. `tie_in` returns what it can actually stand behind, and
the disclaimer says the point of beginning is a mapped corner rather than a
monument.
"""

from __future__ import annotations

import itertools
import math

from . import geo

#: WGS84. The ellipsoid every GeoJSON coordinate in this app is expressed on.
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_B = WGS84_A * (1.0 - WGS84_F)

#: The international foot, exactly. See the module docstring on the US survey foot.
METRES_PER_FOOT = 0.3048

#: Vincenty's iteration bounds.
_MAX_ITERATIONS = 200
_TOLERANCE = 1e-12

#: The sentence every generated description ends with. It is returned as part of
#: the description rather than left to the caller, because a draft that loses its
#: disclaimer on the way to a printer is a draft that reads like a survey.
DISCLAIMER = (
	"This description was generated from boundaries drawn on aerial imagery and is a DRAFT "
	"for discussion with a licensed surveyor. It is not a survey and must not be used to "
	"convey land. Bearings are grid azimuths from TRUE NORTH on the WGS84 ellipsoid, not "
	"magnetic and not referred to a state plane basis of bearings; distances are "
	"international feet (0.3048 m). The point of beginning is a corner taken from mapped "
	"parcel data, not a monument set or found on the ground."
)


def _radians(point) -> tuple:
	"""A GeoJSON `[lon, lat]` pair as radians, latitude first for readability."""
	longitude, latitude = float(point[0]), float(point[1])
	return math.radians(latitude), math.radians(longitude)


def inverse(start, end) -> dict:
	"""Azimuth and geodesic distance from `start` to `end`, both `[lon, lat]`.

	Returns `{"azimuth_deg", "distance_m", "distance_ft", "method"}`. `method` is
	"vincenty" or "haversine"; see the module docstring on when the second
	happens and why it is reported rather than hidden.
	"""
	lat1, lon1 = _radians(start)
	lat2, lon2 = _radians(end)

	difference = lon2 - lon1
	u1 = math.atan((1 - WGS84_F) * math.tan(lat1))
	u2 = math.atan((1 - WGS84_F) * math.tan(lat2))
	sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
	sin_u2, cos_u2 = math.sin(u2), math.cos(u2)

	lambda_ = difference
	sin_sigma = cos_sigma = sigma = cos_sq_alpha = cos_2sigma_m = 0.0
	converged = False
	for _ in range(_MAX_ITERATIONS):
		sin_lambda, cos_lambda = math.sin(lambda_), math.cos(lambda_)
		sin_sigma = math.sqrt(
			(cos_u2 * sin_lambda) ** 2 + (cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lambda) ** 2
		)
		if sin_sigma == 0:
			# Coincident points. Zero distance, and an azimuth nobody should read.
			return {"azimuth_deg": 0.0, "distance_m": 0.0, "distance_ft": 0.0, "method": "coincident"}
		cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lambda
		sigma = math.atan2(sin_sigma, cos_sigma)
		sin_alpha = cos_u1 * cos_u2 * sin_lambda / sin_sigma
		cos_sq_alpha = 1 - sin_alpha**2
		cos_2sigma_m = cos_sigma - 2 * sin_u1 * sin_u2 / cos_sq_alpha if cos_sq_alpha else 0.0
		c = WGS84_F / 16 * cos_sq_alpha * (4 + WGS84_F * (4 - 3 * cos_sq_alpha))
		previous = lambda_
		lambda_ = difference + (1 - c) * WGS84_F * sin_alpha * (
			sigma + c * sin_sigma * (cos_2sigma_m + c * cos_sigma * (-1 + 2 * cos_2sigma_m**2))
		)
		if abs(lambda_ - previous) < _TOLERANCE:
			converged = True
			break

	if not converged:  # pragma: no cover - needs near-antipodal points
		return _haversine(lat1, lon1, lat2, lon2)

	u_sq = cos_sq_alpha * (WGS84_A**2 - WGS84_B**2) / WGS84_B**2
	a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
	b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
	delta_sigma = (
		b
		* sin_sigma
		* (
			cos_2sigma_m
			+ b
			/ 4
			* (
				cos_sigma * (-1 + 2 * cos_2sigma_m**2)
				- b / 6 * cos_2sigma_m * (-3 + 4 * sin_sigma**2) * (-3 + 4 * cos_2sigma_m**2)
			)
		)
	)
	distance = WGS84_B * a * (sigma - delta_sigma)

	sin_lambda, cos_lambda = math.sin(lambda_), math.cos(lambda_)
	azimuth = math.atan2(cos_u2 * sin_lambda, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lambda)
	return {
		"azimuth_deg": math.degrees(azimuth) % 360.0,
		"distance_m": distance,
		"distance_ft": distance / METRES_PER_FOOT,
		"method": "vincenty",
	}


def _haversine(lat1, lon1, lat2, lon2) -> dict:  # pragma: no cover - see `inverse`
	"""The fallback. Spherical, and named as such in the answer."""
	difference = lon2 - lon1
	sin_half_lat = math.sin((lat2 - lat1) / 2) ** 2
	sin_half_lon = math.sin(difference / 2) ** 2
	a = sin_half_lat + math.cos(lat1) * math.cos(lat2) * sin_half_lon
	distance = 2 * geo.EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
	azimuth = math.atan2(
		math.sin(difference) * math.cos(lat2),
		math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(difference),
	)
	return {
		"azimuth_deg": math.degrees(azimuth) % 360.0,
		"distance_m": distance,
		"distance_ft": distance / METRES_PER_FOOT,
		"method": "haversine",
	}


def dms(azimuth: float) -> tuple:
	"""An angle in degrees as `(degrees, minutes, seconds)`, rounded ONCE.

	See the module docstring: rounding the three parts separately is what prints
	`59'60"`.
	"""
	total_seconds = round(float(azimuth) * 3600.0)
	degrees, remainder = divmod(total_seconds, 3600)
	minutes, seconds = divmod(remainder, 60)
	return int(degrees), int(minutes), int(seconds)


def quadrant(azimuth: float) -> dict:
	"""An azimuth as a surveyor's quadrant bearing.

	`{"text": "N 45°30'15\\" E", "quadrant": "NE", "degrees", "minutes",
	"seconds", "azimuth_deg"}`. A course within half a second of a cardinal
	direction is written as the word — "due North" is what a deed says, and
	`N 0°00'00" E` is what a machine says.
	"""
	azimuth = float(azimuth) % 360.0
	cardinal = {0.0: "North", 90.0: "East", 180.0: "South", 270.0: "West"}
	for angle, word in cardinal.items():
		if abs(((azimuth - angle + 180.0) % 360.0) - 180.0) * 3600.0 < 0.5:
			return {
				"text": f"due {word}",
				"quadrant": word[0],
				"degrees": 0,
				"minutes": 0,
				"seconds": 0,
				"azimuth_deg": angle,
				"cardinal": True,
			}

	if azimuth < 90.0:
		prefix, suffix, angle = "N", "E", azimuth
	elif azimuth < 180.0:
		prefix, suffix, angle = "S", "E", 180.0 - azimuth
	elif azimuth < 270.0:
		prefix, suffix, angle = "S", "W", azimuth - 180.0
	else:
		prefix, suffix, angle = "N", "W", 360.0 - azimuth

	degrees, minutes, seconds = dms(angle)
	# The carry can push a quadrant angle to exactly 90°, which is the next
	# cardinal direction rather than a bearing of ninety degrees.
	if degrees >= 90:
		# The carry landed on a cardinal direction. Name it rather than printing
		# a quadrant bearing of ninety degrees, which no deed contains.
		return quadrant(round(azimuth / 90.0) * 90.0 % 360.0)
	return {
		"text": f"{prefix} {degrees}°{minutes:02d}'{seconds:02d}\" {suffix}",
		"quadrant": f"{prefix}{suffix}",
		"degrees": degrees,
		"minutes": minutes,
		"seconds": seconds,
		"azimuth_deg": azimuth,
		"cardinal": False,
	}


def ring(points: list) -> list:
	"""The drawn points as a closed ring, without duplicating a closing vertex."""
	cleaned = [[float(point[0]), float(point[1])] for point in points or []]
	if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
		cleaned = cleaned[:-1]
	return cleaned


def courses(points: list, close: bool = True) -> list:
	"""One row per course: from, to, bearing, distance.

	`close` adds the course from the last point back to the first, which is the
	one a deed states as "returning to the point of beginning" and the one a
	reader most needs to check.
	"""
	vertices = ring(points)
	if len(vertices) < 2:
		return []
	pairs = list(itertools.pairwise(vertices))
	if close and len(vertices) > 2:
		pairs.append((vertices[-1], vertices[0]))

	rows = []
	for index, (start, end) in enumerate(pairs):
		answer = inverse(start, end)
		bearing = quadrant(answer["azimuth_deg"])
		rows.append(
			{
				"index": index + 1,
				"from": start,
				"to": end,
				"bearing": bearing["text"],
				"quadrant": bearing["quadrant"],
				"azimuth_deg": round(answer["azimuth_deg"], 6),
				"distance_ft": round(answer["distance_ft"], 2),
				"distance_m": round(answer["distance_m"], 3),
				"method": answer["method"],
				"closing": close and len(vertices) > 2 and index == len(pairs) - 1,
			}
		)
	return rows


def closure(points: list) -> dict:
	"""How far the traverse misses its own starting point, and the precision ratio.

	Computed by walking the drawn courses as plane bearings and distances — which
	is what a surveyor's closure check does — and comparing where that walk ends
	with where it began. The ratio is reported as `1 part in N`, the form a deed
	reviewer reads. Perfect closure returns `precision: None` and says so.
	"""
	vertices = ring(points)
	if len(vertices) < 3:
		return {"error_ft": 0.0, "perimeter_ft": 0.0, "precision": None, "precision_text": "", "courses": 0}

	north = east = perimeter = 0.0
	pairs = [*itertools.pairwise(vertices), (vertices[-1], vertices[0])]
	for start, end in pairs:
		answer = inverse(start, end)
		angle = math.radians(answer["azimuth_deg"])
		north += answer["distance_ft"] * math.cos(angle)
		east += answer["distance_ft"] * math.sin(angle)
		perimeter += answer["distance_ft"]

	error = math.hypot(north, east)
	ratio = int(perimeter / error) if error > 1e-9 else None
	return {
		"error_ft": round(error, 3),
		"perimeter_ft": round(perimeter, 2),
		"precision": ratio,
		"precision_text": f"1 part in {ratio:,}" if ratio else "closes exactly as drawn",
		"courses": len(pairs),
	}


def polygon(points: list) -> dict:
	"""The drawn points as a GeoJSON Polygon, closed. `None` under three points."""
	vertices = ring(points)
	if len(vertices) < 3:
		return None
	return {"type": "Polygon", "coordinates": [[*vertices, vertices[0]]]}


def acres(points: list) -> float:
	"""Enclosed area in acres, through `geo.area_acres` and nothing else."""
	shape = polygon(points)
	return geo.area_acres(shape) if shape else 0.0


# ── the point of beginning ──────────────────────────────────────────────────
#: How far round the compass each label reaches. A corner is named for where it
#: sits in its OWN shape, which is how a deed refers to one.
_CORNER_LABELS = (
	(22.5, "north"),
	(67.5, "northeast"),
	(112.5, "east"),
	(157.5, "southeast"),
	(202.5, "south"),
	(247.5, "southwest"),
	(292.5, "west"),
	(337.5, "northwest"),
	(360.0, "north"),
)


def corner_label(azimuth: float) -> str:
	"""`northwest` and so on, for a bearing from a shape's middle to its corner."""
	azimuth = float(azimuth) % 360.0
	for limit, label in _CORNER_LABELS:
		if azimuth < limit:
			return label
	return "north"


def corners_of(geometry: dict) -> list:
	"""Every vertex of a parsed geometry, with the label it carries in its shape.

	Deduplicated on the rounded coordinate, because a closed ring repeats its
	first vertex and a shared boundary repeats a corner in both directions.
	"""
	points = []
	for point in geo_points(geometry):
		if point not in points:
			points.append(point)
	if not points:
		return []
	middle = [
		sum(point[0] for point in points) / len(points),
		sum(point[1] for point in points) / len(points),
	]
	out = []
	for point in points:
		answer = inverse(middle, point)
		out.append(
			{
				"point": point,
				"label": corner_label(answer["azimuth_deg"]),
				"distance_from_middle_ft": round(answer["distance_ft"], 2),
			}
		)
	return out


def geo_points(geometry) -> list:
	"""Every `[lon, lat]` in a geometry, rounded to the centimetre. Order kept."""
	out = []
	if isinstance(geometry, dict):
		_walk(geometry.get("coordinates"), out)
		for part in geometry.get("geometries") or []:
			out.extend(geo_points(part))
	return out


def _walk(coordinates, out: list) -> None:
	if not isinstance(coordinates, (list, tuple)) or not coordinates:
		return
	first = coordinates[0]
	if isinstance(first, (int, float)) and len(coordinates) >= 2:
		try:
			out.append([round(float(coordinates[0]), 7), round(float(coordinates[1]), 7)])
		except (TypeError, ValueError):
			pass
		return
	for item in coordinates:
		_walk(item, out)


def tie_in(point, references: list) -> dict:
	"""The nearest mapped corner to `point`, and the course from it.

	`references` are `{"label", "geometry"}` rows — a tax lot, a parcel, a field.
	Returns `{"found": bool, "description", "corner", "bearing", "distance_ft",
	"reference"}`. Nothing near enough to be useful is `found: False` with a
	sentence saying so; the caller then writes the description from an operator's
	own words rather than inventing a monument.
	"""
	best = None
	for reference in references or []:
		geometry = reference.get("geometry")
		if not isinstance(geometry, dict):
			continue
		for corner in corners_of(geometry):
			answer = inverse(corner["point"], point)
			if best is None or answer["distance_ft"] < best["distance_ft"]:
				best = {
					"distance_ft": answer["distance_ft"],
					"azimuth_deg": answer["azimuth_deg"],
					"corner": corner,
					"reference": reference.get("label") or reference.get("name") or "a mapped boundary",
				}

	if best is None:
		return {
			"found": False,
			"description": "",
			"note": (
				"no mapped parcel, tax lot or field corner was found to tie the point of "
				"beginning to. Describe the starting point in your own words before sending "
				"this to a surveyor."
			),
		}

	bearing = quadrant(best["azimuth_deg"])
	corner = f"the {best['corner']['label']} corner of {best['reference']}"
	if best["distance_ft"] < 1.0:
		description = f"Beginning at {corner}"
	else:
		description = (
			f"Commencing at {corner}; thence {bearing['text']}, "
			f"{best['distance_ft']:,.2f} feet to the point of beginning"
		)
	return {
		"found": True,
		"description": description,
		"corner": best["corner"],
		"corner_description": corner,
		"reference": best["reference"],
		"bearing": bearing["text"],
		"distance_ft": round(best["distance_ft"], 2),
	}


# ── the description ─────────────────────────────────────────────────────────
def legal_description(points: list, beginning: str = "", note: str = "") -> dict:
	"""Draft metes and bounds for the drawn figure.

	`beginning` is the tie-in sentence — `tie_in(...)["description"]` or an
	operator's own. `note` is anything the caller wants between the courses and
	the disclaimer (the county, the adjustment's title, the parties).

	Returns `{"text", "courses", "acres", "closure", "disclaimer"}`. The text
	ALWAYS ends with the disclaimer; see the module docstring.
	"""
	rows = courses(points)
	if len(rows) < 3:
		return {
			"text": "",
			"courses": rows,
			"acres": 0.0,
			"closure": closure(points),
			"disclaimer": DISCLAIMER,
			"note": "a description needs at least three courses; draw the whole figure first.",
		}

	opening = str(beginning or "").strip() or "Beginning at the point of beginning"
	if opening.endswith("."):
		opening = opening[:-1]

	sentences = [f"{opening};"]
	for row in rows:
		distance = f"{row['distance_ft']:,.2f} feet"
		if row["closing"]:
			sentences.append(f"thence {row['bearing']}, {distance} to the point of beginning.")
		else:
			sentences.append(f"thence {row['bearing']}, {distance} to a point;")

	area = acres(points)
	body = " ".join(sentences)
	tail = f"Containing {area:,.2f} acres, more or less."
	paragraphs = [body, tail]
	if note:
		paragraphs.append(str(note).strip())
	paragraphs.append(DISCLAIMER)
	return {
		"text": "\n\n".join(paragraphs),
		"courses": rows,
		"acres": area,
		"closure": closure(points),
		"disclaimer": DISCLAIMER,
		"note": "",
	}


# ── easements ───────────────────────────────────────────────────────────────
def _segments(points: list) -> list:
	vertices = [[float(point[0]), float(point[1])] for point in points or []]
	return list(itertools.pairwise(vertices))


def _orientation(a, b, c) -> float:
	return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, point) -> bool:
	return (
		min(a[0], b[0]) - 1e-12 <= point[0] <= max(a[0], b[0]) + 1e-12
		and min(a[1], b[1]) - 1e-12 <= point[1] <= max(a[1], b[1]) + 1e-12
	)


def segments_cross(a, b, c, d) -> bool:
	"""Whether segment `ab` meets segment `cd`, touching included.

	Plane geometry in degrees, which is right for this question and would be
	wrong for a distance: two lot lines a few hundred feet long do not care that
	a degree of longitude is shorter than a degree of latitude, because an
	intersection is an intersection in any affine frame.
	"""
	d1, d2 = _orientation(c, d, a), _orientation(c, d, b)
	d3, d4 = _orientation(a, b, c), _orientation(a, b, d)
	if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
		return True
	for orientation, (start, end, point) in (
		(d1, (c, d, a)),
		(d2, (c, d, b)),
		(d3, (a, b, c)),
		(d4, (a, b, d)),
	):
		if abs(orientation) < 1e-15 and _on_segment(start, end, point):
			return True
	return False


def point_in_ring(point, ring_points) -> bool:
	"""Ray casting. A point exactly on the edge is caught by `segments_cross`."""
	inside = False
	count = len(ring_points)
	for index in range(count):
		x1, y1 = ring_points[index][0], ring_points[index][1]
		x2, y2 = ring_points[(index + 1) % count][0], ring_points[(index + 1) % count][1]
		if (y1 > point[1]) != (y2 > point[1]):
			x_at = x1 + (point[1] - y1) * (x2 - x1) / ((y2 - y1) or 1e-15)
			if x_at > point[0]:
				inside = not inside
	return inside


def crossings(points: list, easements: list) -> list:
	"""Which easement corridors the drawn line touches, and how.

	`easements` are `{"label", "geometry", ...}` rows. Each answer is
	`{"label", "crosses", "how", "note", ...}` — `how` is "crosses" where a
	course cuts the corridor, "inside" where the whole line lies within it, and
	the row is omitted where neither is true. An easement is a right somebody
	else holds over the ground, so a line that runs down one is a title problem
	found on a map rather than at closing.
	"""
	drawn = _segments(ring(points) + ([ring(points)[0]] if len(ring(points)) > 2 else []))
	if not drawn:
		return []

	out = []
	for easement in easements or []:
		geometry = easement.get("geometry")
		if not isinstance(geometry, dict):
			continue
		crosses = False
		for other in _corridor_lines(geometry):
			for a, b in drawn:
				if any(segments_cross(a, b, c, d) for c, d in _segments(other)):
					crosses = True
					break
			if crosses:
				break

		inside = False
		if not crosses:
			for ring_points in _corridor_rings(geometry):
				if all(point_in_ring(point, ring_points) for point, _ in drawn):
					inside = True
					break

		if crosses or inside:
			out.append(
				{
					"label": easement.get("label") or "an easement",
					"easement_type": easement.get("easement_type") or "",
					"burdened": easement.get("burdened") or "",
					"benefited": easement.get("benefited") or "",
					"crosses": True,
					"how": "crosses" if crosses else "inside",
					"note": (
						"the proposed line crosses this easement corridor"
						if crosses
						else "the proposed line lies inside this easement corridor"
					),
				}
			)
	return out


def _corridor_lines(geometry: dict) -> list:
	"""Every line a corridor is made of: a LineString's own, a Polygon's rings."""
	kind = str(geometry.get("type") or "")
	coordinates = geometry.get("coordinates") or []
	if kind == "LineString":
		return [coordinates]
	if kind == "MultiLineString":
		return list(coordinates)
	if kind == "Polygon":
		return [[*ring_points, ring_points[0]] for ring_points in coordinates if ring_points]
	if kind == "MultiPolygon":
		out = []
		for polygon_rings in coordinates:
			out.extend([*ring_points, ring_points[0]] for ring_points in polygon_rings if ring_points)
		return out
	return []


def _corridor_rings(geometry: dict) -> list:
	kind = str(geometry.get("type") or "")
	coordinates = geometry.get("coordinates") or []
	if kind == "Polygon":
		return [ring_points for ring_points in coordinates[:1] if ring_points]
	if kind == "MultiPolygon":
		return [rings[0] for rings in coordinates if rings]
	return []
