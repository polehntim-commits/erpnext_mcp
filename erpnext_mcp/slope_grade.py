# SPDX-License-Identifier: MIT
"""How steep the ground is: slope grade as map tiles, coloured by rollover danger.

v0.168.0. The slope aspect layer (v0.167.0, `slope_aspect.py`) says which way
the ground faces. This one says how steep it is, and colours that for the
question an operator asks before sending a machine up a block: will it roll?

────────────────────────────────────────────────────────────────────────────
NOTHING IS FETCHED. THE SLOPE IS ALREADY ON DISK.
────────────────────────────────────────────────────────────────────────────

`build_slope_aspect_layer` computes slope for every 10 m cell by Horn's method,
corrected for mercator scale, and keeps it in `private/slope_aspect/grid.npz`
beside the aspect (it drives the aspect layer's saturation). This module reads
that array and nothing else. There is no second build, no second USGS request,
and no second copy of the terrain: "is the grade layer built" is the same
question as "is the aspect layer built", and a rebuild of the one refreshes the
other. Grade tiles are rendered on first request and kept under
`private/slope_aspect/grade/<scheme>/`, which the aspect build's atomic swap of
the whole folder retires with everything else.

────────────────────────────────────────────────────────────────────────────
TWO SCHEMES: THE GENERAL ONE, AND ONE PER MACHINE
────────────────────────────────────────────────────────────────────────────

Standard: under 8° green, 8–15° yellow, 15–25° orange, 25° and over red.

Equipment: a machine's `max_safe_slope_degrees` on its Asset Register row moves
the breakpoints to 60%, 80% and 100% of that limit. A tractor rated for 25°
sees yellow from 15°, orange from 20° and red from 25°; a sprayer rated for 12°
sees red from 12°. Same four colours, so a phone keys its drawing on `level`
(0–3) and never needs to know which scheme it is looking at.

A CELL EXACTLY ON A BREAKPOINT TAKES THE HIGHER BAND. The conservative side of
a rollover limit is the steeper one.

────────────────────────────────────────────────────────────────────────────
AN UNSET LIMIT FALLS BACK TO THE TYPE, AND THE FALLBACK IS THE CAUTIOUS FIGURE
────────────────────────────────────────────────────────────────────────────

A Frappe Float column is NOT NULL DEFAULT 0, so an unset limit reads back as
0.0 — which taken literally would paint every slope on the farm red. Zero is
therefore "not set", and the asset's TYPE supplies the limit. The register does
not record whether a tractor has a ROPS, so a Tractor falls back to the
no-ROPS 15° rather than the ROPS 25°: an operator who has a ROPS tractor sets
25 on it, and one who has not set anything is not told a slope is safe on the
strength of a roll bar nobody said was there. An asset of a type with no
figure (a valve) is refused by name rather than coloured generically.

────────────────────────────────────────────────────────────────────────────
A BLOCK IS AS DANGEROUS AS ITS STEEPEST CELL
────────────────────────────────────────────────────────────────────────────

A 10 m DEM smooths terrain — a cut bank or a ditch edge that a wheel meets is
steeper than the cell that contains it. So a single cell over the limit is not
survey noise to average away; it is ground that is at least that steep. A
block's `level` is its steepest cell's, and `band_shares` says how much of the
block that is, so "one bank on the south edge" and "the whole block" are told
apart without either being called safe.
"""

from __future__ import annotations

import math
import os
from urllib.parse import quote

import frappe

from . import compat
from . import slope_aspect as terrain
from .errors import ToolError

np = terrain.np

KEY = "slope_grade"
TILE_URL_TEMPLATE = "/farmops/api/tiles/slope_grade/{z}/{x}/{y}.png"
ASSET_REGISTER = "Asset Register"
RATING_FIELD = "max_safe_slope_degrees"

#: Bumped when a breakpoint or a colour changes, so kept tiles of the old
#: scheme are not served beside new ones.
SCHEME_VERSION = 1

# ── the schemes ─────────────────────────────────────────────────────────────
STANDARD_BREAKS = (8.0, 15.0, 25.0)
STANDARD_BANDS = (
	("gentle", "Gentle"),
	("moderate", "Moderate"),
	("steep", "Steep"),
	("very_steep", "Very steep"),
)
#: Of the machine's maximum safe slope.
EQUIPMENT_FRACTIONS = (0.6, 0.8, 1.0)
EQUIPMENT_BANDS = (
	("within", "Well within the limit"),
	("approaching", "Approaching the limit"),
	("at_limit", "At the limit"),
	("over_limit", "Over the limit"),
)
#: Green, yellow, orange, red — one per level.
COLOURS = ((46, 158, 72), (232, 200, 36), (240, 128, 28), (208, 36, 40))
ALPHA = terrain.ALPHA

#: The range a limit may be set in. Nothing with wheels is safe beyond 45°.
MIN_RATING_DEGREES = 1.0
MAX_RATING_DEGREES = 45.0

#: Common limits, published in the descriptor so whoever sets a machine's limit
#: has the usual figure in front of them. Guidance, not a manufacturer's rating.
EQUIPMENT_DEFAULTS = (
	("Tractor with ROPS", 25.0),
	("Tractor without ROPS", 15.0),
	("ATV/UTV", 20.0),
	("Sprayer", 12.0),
	("Mower", 15.0),
)

#: What an asset with no limit of its own falls back to, by `asset_type`. The
#: cautious figure for each — see the module docstring. These are also the
#: types the Asset Register form shows the field for.
TYPE_DEFAULTS = {
	"Tractor": 15.0,
	"Vehicle": 20.0,
	"Sprayer": 12.0,
	"Implement": 15.0,
}
SLOPE_RATED_ASSET_TYPES = tuple(TYPE_DEFAULTS)


class AssetNotRated(ToolError):
	"""An asset with no limit and a type with no fallback. A 400, not a 404."""


def available() -> bool:
	return terrain.available()


def percent_grade(degrees):
	"""Rise over run, as a percentage, for the legend. None stays None."""
	if degrees is None:
		return None
	return round(math.tan(math.radians(float(degrees))) * 100.0, 1)


def standard_scheme() -> dict:
	return {
		"mode": "standard",
		"breaks": STANDARD_BREAKS,
		"bands": STANDARD_BANDS,
		"max_safe_slope_degrees": None,
	}


def equipment_scheme(max_safe_slope_degrees) -> dict:
	limit = round(float(max_safe_slope_degrees), 1)
	return {
		"mode": "equipment",
		"breaks": tuple(round(limit * fraction, 2) for fraction in EQUIPMENT_FRACTIONS),
		"bands": EQUIPMENT_BANDS,
		"max_safe_slope_degrees": limit,
	}


def scheme_key(scheme: dict) -> str:
	"""The folder a scheme's kept tiles live in."""
	if scheme["mode"] == "standard":
		return f"v{SCHEME_VERSION}-standard"
	return f"v{SCHEME_VERSION}-max-{scheme['max_safe_slope_degrees']:g}"


def legend(scheme: dict) -> list:
	edges = (0.0, *scheme["breaks"], None)
	out = []
	for level, ((band, label), rgb) in enumerate(zip(scheme["bands"], COLOURS, strict=True)):
		low, high = edges[level], edges[level + 1]
		out.append(
			{
				"level": level,
				"band": band,
				"label": label,
				"min_degrees": low,
				"max_degrees": high,
				"min_percent": percent_grade(low),
				"max_percent": percent_grade(high),
				"color": terrain.hex_colour(rgb),
			}
		)
	return out


# ── the arithmetic ──────────────────────────────────────────────────────────
def classify(slope, breaks):
	"""Level 0–3 per cell; -1 where there is no data. A cell on a break goes up."""
	terrain._require_numpy()
	slope = np.asarray(slope, dtype="float64")
	valid = np.isfinite(slope)
	filled = np.where(valid, slope, 0.0)
	level = sum((filled >= float(edge)).astype("int8") for edge in breaks)
	return np.where(valid, level, -1).astype("int8")


def colourise(slope, breaks):
	"""RGBA uint8 per cell. Transparent where there is no data."""
	level = classify(slope, breaks)
	palette = np.array(COLOURS, dtype="uint8")
	out = np.zeros((*level.shape, 4), dtype="uint8")
	valid = level >= 0
	out[valid, :3] = palette[level[valid]]
	out[valid, 3] = ALPHA
	return out


def sample_tile(grid: dict, values, z: int, x: int, y: int):
	"""A 256×256 float array of `values` nearest-cell sampled; NaN off the grid.

	The same cell placement `slope_aspect.render_tile_pixels` uses, over a 2-D
	array instead of a colour image, so a grade tile and an aspect tile of one
	address put every cell in the same pixel.
	"""
	size = terrain.TILE_SIZE
	xmin, _ymin, xmax, ymax = terrain.tile_bounds(z, x, y)
	step = (xmax - xmin) / size
	centres = (np.arange(size, dtype="float64") + 0.5) * step
	cols = np.floor((xmin + centres - grid["xmin"]) / grid["cell"]).astype(int)
	rows = np.floor((grid["ymax"] - (ymax - centres)) / grid["cell"]).astype(int)
	col_ok = (cols >= 0) & (cols < grid["width"])
	row_ok = (rows >= 0) & (rows < grid["height"])
	out = np.full((size, size), np.nan, dtype="float32")
	if col_ok.any() and row_ok.any():
		out[np.ix_(np.nonzero(row_ok)[0], np.nonzero(col_ok)[0])] = values[np.ix_(rows[row_ok], cols[col_ok])]
	return out


# ── the equipment ───────────────────────────────────────────────────────────
def rating_of(row: dict) -> dict:
	"""The limit for one Asset Register row: its own, else its type's. Raises if neither."""
	raw = row.get(RATING_FIELD)
	own = float(raw) if raw not in (None, "") else 0.0
	asset_type = row.get("asset_type") or None
	if own > 0:
		limit, source = own, "asset"
	elif asset_type in TYPE_DEFAULTS:
		limit, source = TYPE_DEFAULTS[asset_type], "type_default"
	else:
		raise AssetNotRated(
			f"{row.get('name')} is a {asset_type or 'asset with no type'} and has no max_safe_slope_degrees, "
			f"so there is no limit to colour the ground against. Set one with update_registered_asset, "
			f"or pick a {', '.join(SLOPE_RATED_ASSET_TYPES[:-1])} or {SLOPE_RATED_ASSET_TYPES[-1]}."
		)
	return {
		"asset": row.get("name"),
		"asset_type": asset_type,
		"company": row.get("company") or None,
		"max_safe_slope_degrees": round(limit, 1),
		"max_safe_slope_source": source,
	}


def asset_rating(name: str) -> dict:
	"""`rating_of` the Asset Register row called `name`. ToolError when there is none."""
	name = str(name or "").strip()
	if not compat.doctype_exists(ASSET_REGISTER) or not name or not frappe.db.exists(ASSET_REGISTER, name):
		raise ToolError(f"No Asset Register record called {name!r}. list_assets has the register.")
	fields = compat.existing_fields(ASSET_REGISTER, ("name", "asset_type", "company", RATING_FIELD))
	row = dict(frappe.db.get_value(ASSET_REGISTER, name, fields, as_dict=True) or {})
	return rating_of(row)


def validate_rating(value, tail: str):
	"""A limit an operator typed, as a float, or None to clear. Refuses a zero by name."""
	if value in (None, ""):
		return None
	try:
		number = float(value)
	except (TypeError, ValueError):
		raise ToolError(
			f"max_safe_slope_degrees must be a number of degrees, got {value!r}. {tail}"
		) from None
	if not math.isfinite(number) or number < MIN_RATING_DEGREES or number > MAX_RATING_DEGREES:
		raise ToolError(
			f"max_safe_slope_degrees must be between {MIN_RATING_DEGREES:g} and {MAX_RATING_DEGREES:g} "
			f"degrees, got {value!r}. To clear it and fall back to the type's figure, pass null. {tail}"
		)
	return round(number, 1)


def asset_slope_rating(row: dict) -> dict:
	"""For `get_asset_detail`: the stored limit and the one in effect. Empty off a rated type."""
	if row.get("asset_type") not in TYPE_DEFAULTS and not compat.has_field(ASSET_REGISTER, RATING_FIELD):
		return {}
	stored = None
	if compat.has_field(ASSET_REGISTER, RATING_FIELD):
		stored = frappe.db.get_value(ASSET_REGISTER, row["name"], RATING_FIELD)
	stored = float(stored) if stored not in (None, "") and float(stored) > 0 else None
	try:
		effective = rating_of({**row, RATING_FIELD: stored})
	except AssetNotRated:
		return {RATING_FIELD: None, "slope_rating": None}
	return {RATING_FIELD: stored, "slope_rating": effective}


# ── tiles ───────────────────────────────────────────────────────────────────
def tile_png(z: int, x: int, y: int, scheme: dict | None = None) -> bytes | None:
	"""One grade tile. None when the terrain has never been built.

	Off the layer or the zoom range: the transparent tile. Otherwise from disk,
	or rendered from the cached slope and kept.
	"""
	scheme = scheme or standard_scheme()
	z, x, y = int(z), int(x), int(y)
	meta = terrain.read_meta()
	if meta is None:
		return None
	terrain._require_numpy()
	if z < terrain.MIN_ZOOM or z > terrain.MAX_ZOOM or not terrain.valid_tile(z, x, y):
		return terrain.TRANSPARENT_TILE
	grid = meta["grid"]
	gx0, gy0, gx1, gy1 = terrain.grid_bounds(grid)
	tx0, ty0, tx1, ty1 = terrain.tile_bounds(z, x, y)
	if tx1 <= gx0 or tx0 >= gx1 or ty1 <= gy0 or ty0 >= gy1:
		return terrain.TRANSPARENT_TILE
	path = os.path.join(terrain.cache_dir(), "grade", scheme_key(scheme), str(z), str(x), f"{y}.png")
	try:
		with open(path, "rb") as handle:
			return handle.read()
	except OSError:
		pass
	slope = terrain.load_grid(meta)["slope"]
	png = terrain.png_rgba(colourise(sample_tile(grid, slope, z, x, y), scheme["breaks"]))
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		temporary = f"{path}.{os.getpid()}.tmp"
		with open(temporary, "wb") as handle:
			handle.write(png)
		os.replace(temporary, path)
	except OSError:  # pragma: no cover - a read-only site folder still serves
		pass
	return png


def tile_url_template(equipment: dict | None = None) -> str:
	if not equipment:
		return TILE_URL_TEMPLATE
	return f"{TILE_URL_TEMPLATE}?asset={quote(str(equipment['asset']), safe='')}"


# ── describing ──────────────────────────────────────────────────────────────
def describe(meta: dict | None = None, equipment: dict | None = None) -> dict:
	"""What a map needs to draw the grade layer, or why it cannot."""
	meta = meta if meta is not None else terrain.read_meta()
	scheme = equipment_scheme(equipment["max_safe_slope_degrees"]) if equipment else standard_scheme()
	if equipment:
		detail = (
			f"How steep the ground is for {equipment['asset']}, rated to "
			f"{scheme['max_safe_slope_degrees']:g}°. Green is under 60% of that, yellow 60–80%, "
			"orange 80–100%, red over it. A 10 m survey smooths banks and ditch edges, so treat "
			"orange as the working limit."
		)
	else:
		detail = (
			"How steep the ground is. Green under 8°, yellow 8–15°, orange 15–25°, red 25° and "
			"over. Pass an asset for that machine's own limits."
		)
	base = {
		"key": KEY,
		"label": "Slope grade",
		"detail": detail,
		"mode": scheme["mode"],
		"tile_url_template": tile_url_template(equipment),
		"tile_size": terrain.TILE_SIZE,
		"min_zoom": terrain.MIN_ZOOM,
		"max_zoom": terrain.MAX_ZOOM,
		"legend": legend(scheme),
		"breakpoints_degrees": list(scheme["breaks"]),
		"equipment": equipment or None,
		"equipment_defaults": [
			{"equipment": label, "max_safe_slope_degrees": degrees} for label, degrees in EQUIPMENT_DEFAULTS
		],
		"opacity": round(ALPHA / 255, 2),
		"source": terrain.SOURCE_LABEL,
	}
	if meta is None:
		return {
			**base,
			"available": False,
			"reason": (
				"Not built on this site yet. The grade layer reads the terrain the slope aspect "
				"layer caches: an operator runs build_slope_aspect_layer once and both layers exist."
			),
		}
	return {
		**base,
		"available": True,
		"bounds": meta.get("bounds"),
		"built_at": meta.get("built_at"),
		"company": meta.get("company"),
		"products": meta.get("products") or [],
		"slope_degrees_max": meta.get("slope_degrees_max"),
	}


def summarise(slope, scheme: dict) -> dict | None:
	"""What one block's cells say about how steep it is."""
	slope = np.asarray(slope, dtype="float64")
	slope = slope[np.isfinite(slope)]
	cells = int(slope.size)
	if not cells:
		return None
	levels = classify(slope, scheme["breaks"])
	steepest = int(levels.max())
	band, label = scheme["bands"][steepest]
	shares = {
		name: round(float((levels == level).sum()) / cells, 3)
		for level, (name, _label) in enumerate(scheme["bands"])
	}
	out = {
		"cells": cells,
		"mean_slope_degrees": round(float(slope.mean()), 1),
		"max_slope_degrees": round(float(slope.max()), 1),
		"p90_slope_degrees": round(float(np.percentile(slope, 90)), 1),
		"share_at_or_above_degrees": {
			f"{edge:g}": round(float((slope >= edge).sum()) / cells, 3) for edge in STANDARD_BREAKS
		},
		"band_shares": shares,
		"level": steepest,
		"band": band,
		"band_label": label,
	}
	if scheme["mode"] == "equipment":
		limit = scheme["breaks"][-1]
		over = int((slope >= limit).sum())
		out["cells_over_limit"] = over
		out["share_over_limit"] = round(over / cells, 3)
	return out


def block_summaries(
	company: str = "", meta: dict | None = None, scheme: dict | None = None
) -> tuple[list, list]:
	"""Every Field on the layer, steepest first. `(rows, warnings)`."""
	scheme = scheme or standard_scheme()
	cells, warnings = terrain.block_cells(company, meta)
	out = []
	for row, _aspect, slope in cells:
		stats = summarise(slope, scheme) if slope.size else None
		if stats is None:
			warnings.append(
				f"{row['name']} lies outside the built layer or over no data; rebuild to include it."
			)
			continue
		out.append(
			{
				"field": row["name"],
				"field_name": row.get("field_name") or None,
				"company": row.get("owning_entity") or None,
				**stats,
			}
		)
	out.sort(
		key=lambda entry: (
			-entry["level"],
			-entry["band_shares"][scheme["bands"][entry["level"]][0]],
			-entry["max_slope_degrees"],
			entry["field"],
		)
	)
	for rank, entry in enumerate(out, start=1):
		entry["steepness_rank"] = rank
	return out, warnings


def grade_at(latitude, longitude, meta: dict | None = None, scheme: dict | None = None) -> dict | None:
	"""The slope and band of the one cell under a point, or None outside the layer.

	Classified on the stored slope, not the rounded one `slope_aspect.aspect_at`
	reports, so a point and the tile pixel over it never disagree at a break.
	"""
	scheme = scheme or standard_scheme()
	meta = meta if meta is not None else terrain.read_meta()
	point = terrain.aspect_at(latitude, longitude, meta)
	if point is None:
		return None
	grid = meta["grid"]
	x, y = terrain.to_mercator(float(longitude), float(latitude))
	col = int((x - grid["xmin"]) // grid["cell"])
	row = int((grid["ymax"] - y) // grid["cell"])
	raw = float(terrain.load_grid(meta)["slope"][row, col])
	level = int(classify([raw], scheme["breaks"])[0])
	band, label = scheme["bands"][level]
	return {
		"latitude": point["latitude"],
		"longitude": point["longitude"],
		"slope_degrees": point["slope_degrees"],
		"slope_percent": percent_grade(raw),
		"level": level,
		"band": band,
		"band_label": label,
		"color": terrain.hex_colour(COLOURS[level]),
	}
