# SPDX-License-Identifier: MIT
"""Which way the ground faces: slope aspect from USGS 3DEP, as map tiles.

v0.167.0. A south-facing hillside warms earlier in spring, breaks dormancy
earlier and ripens earlier; a north-facing one runs late. Every orchard knows
this about its own blocks by reputation, and nothing on the map said it. This
module computes it from public elevation data and serves it two ways: as
colour-coded z/x/y PNG tiles a phone draws under the blocks (`MKTileOverlay`),
and as a per-block summary a model can rank for variety placement and harvest
scheduling.

────────────────────────────────────────────────────────────────────────────
THE DATA IS USGS 1/3 ARC-SECOND, AND IT IS PINNED TO THAT PRODUCT
────────────────────────────────────────────────────────────────────────────

The National Map's 3DEP elevation service is a DYNAMIC MOSAIC: asked for a
bounding box it returns the best raster it holds there, which over much of
Washington is 1 m lidar, somewhere else 1/9 arc-second, and elsewhere again the
seamless 1/3 arc-second layer. A layer built from whichever happened to win
would change resolution across a farm boundary and between two builds of the
same farm. So the request carries a mosaic rule selecting the seamless
1/3 arc-second rasters by their catalogue pixel size (~10.3 m; 1 arc-second is
~30.9 and 1/9 is ~3.4), checked against the live service on 2026-09-13 — the
rule is honoured, an impossible rule returns an empty image, and the pinned
answer is identical to locking raster 5474 (`n48w121`, 1/3") by id.

The request is `exportImage` clipped to the farm, in Web Mercator, as an
uncompressed float32 GeoTIFF: one small file per farm rather than the 450 MB
one-degree tile the TNM download would be. The TNM Access API is still asked —
once, best-effort — for the PRODUCT the pixels came from (title, publication
date, download URL), because "which survey is this" is the first question the
first disagreement with a colour will ask. No account, no key.

────────────────────────────────────────────────────────────────────────────
ASPECT IN MERCATOR IS EXACT; SLOPE IN MERCATOR IS NOT, AND IS CORRECTED
────────────────────────────────────────────────────────────────────────────

Web Mercator is conformal: both horizontal axes are stretched by the same
sec(latitude), so a direction measured on the grid IS the compass direction on
the ground and aspect needs no correction. Steepness does — a mercator metre at
47° N is 0.68 of a ground metre, and a gradient taken per mercator metre
understates the slope by that much. `gradient` takes each row's true ground
cell size.

The gradient is Horn's (1981) 3×3 weighted difference, the one `gdaldem aspect`
uses, so a GIS user checking a block in QGIS gets the same number.

────────────────────────────────────────────────────────────────────────────
FLAT GROUND HAS NO ASPECT, AND IS DRAWN GREY RATHER THAN IN A GUESSED COLOUR
────────────────────────────────────────────────────────────────────────────

A valley floor at 0.3° "faces" whichever way the survey noise tilts it, and a
map that painted it red would be telling somebody to plant an early variety on
ground that warms like every other flat acre. Under `FLAT_DEGREES` the aspect
is None and the colour is grey; from there to `STEEP_DEGREES` the colour
saturates. The palette is a circle — north blue, east green, south red, west
yellow — interpolated through eight anchors so that neighbouring bearings are
neighbouring colours.

────────────────────────────────────────────────────────────────────────────
WHAT IS CACHED, AND WHERE
────────────────────────────────────────────────────────────────────────────

Terrain does not change between one morning and the next, which is the opposite
of every other overlay on this map (`overlays.py` caches nothing on purpose).
The build writes `private/slope_aspect/` under the site: `grid.npz` (aspect,
slope, colours), `meta.json`, and pre-rendered tiles to `PREBUILD_MAX_ZOOM`.
Deeper tiles render from the grid on first request and are kept. Nothing is in
the database: a raster is not a record, and "avoid table sprawl" is the rule a
DocType per tile would break twice over.

NUMPY IS REQUIRED; RASTERIO IS OPTIONAL. numpy is already in every bench that
has shapely. rasterio (GDAL) reads the GeoTIFF when it is installed; the shipped
image does not have it, and Pillow — which it does — reads the same uncompressed
float32 file.
"""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import struct
import zlib

import frappe

from . import compat
from .errors import ToolError

try:  # pragma: no cover - exercised by whichever branch the bench has
	import numpy as np

	HAVE_NUMPY = True
except Exception:  # pragma: no cover - a bench without the dependency
	np = None
	HAVE_NUMPY = False

REQUIRES = (
	"the numpy Python package — install it into the bench's environment with "
	"`./env/bin/pip install numpy` and restart"
)

FIELD = "Field"
PARCEL = "Parcel"

#: Bumped when the algorithm or the palette changes, so a cache built by an
#: older release is rebuilt rather than served beside tiles of a new one.
ALGORITHM_VERSION = 1

# ── the source ──────────────────────────────────────────────────────────────
EXPORT_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
TNM_PRODUCTS_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"
TNM_DATASET = "National Elevation Dataset (NED) 1/3 arc-second"
SOURCE_LABEL = "USGS 3DEP 1/3 arc-second DEM (The National Map)"

#: Selects the seamless 1/3 arc-second rasters in the 3DEP mosaic. See the
#: module docstring for how this was checked.
MOSAIC_RULE = {
	"mosaicMethod": "esriMosaicAttribute",
	"where": "Category = 1 AND LowPS > 5 AND LowPS < 20",
	"sortField": "LowPS",
	"sortValue": "10",
	"ascending": True,
}

#: The nodata value the export is asked to write. Anything below `NODATA_BELOW`
#: is treated as no data too — the lowest dry land in the country is -86 m.
NODATA = -9999.0
NODATA_BELOW = -1000.0

#: The ground cell size requested. 1/3 arc-second is ~10.3 m north-south.
NATIVE_GROUND_METRES = 10.0

#: Most cells along one side. The export's own limit is 8000; 3000 cells is a
#: 30 km run of ground, which is a farm and not a county.
MAX_GRID_SIDE = 3000

HTTP_TIMEOUT_SECONDS = 60

# ── the tiles ───────────────────────────────────────────────────────────────
TILE_SIZE = 256
MIN_ZOOM = 11
MAX_ZOOM = 17
PREBUILD_MAX_ZOOM = 16
TILE_URL_TEMPLATE = "/farmops/api/tiles/slope_aspect/{z}/{x}/{y}.png"

#: How far past the outermost boundary the layer runs. Enough to see the
#: hillside a block sits on, not the neighbour's whole ranch.
DEFAULT_BUFFER_METRES = 300.0
MAX_BUFFER_METRES = 2000.0

EARTH_RADIUS = 6378137.0
MERCATOR_HALF = math.pi * EARTH_RADIUS

# ── the palette ─────────────────────────────────────────────────────────────
FLAT_DEGREES = 2.0
STEEP_DEGREES = 20.0
#: Baked into every drawn pixel, so the blocks and the imagery under the layer
#: stay readable without the client having to set a blend.
ALPHA = 160
FLAT_GREY = (150, 150, 150)

#: Eight anchors, 45° apart, clockwise from north.
ANCHORS = (
	("N", 0, (40, 95, 205)),
	("NE", 45, (30, 150, 170)),
	("E", 90, (50, 160, 70)),
	("SE", 135, (225, 95, 35)),
	("S", 180, (205, 35, 45)),
	("SW", 225, (240, 140, 30)),
	("W", 270, (230, 200, 40)),
	("NW", 315, (125, 110, 195)),
)

#: The quarter each bearing band counts toward in a block summary.
SOUTH_FACING = (135.0, 225.0)
NORTH_FACING = (315.0, 45.0)


def available() -> bool:
	return HAVE_NUMPY


def _require_numpy() -> None:
	if not HAVE_NUMPY:
		raise ToolError(f"Slope aspect needs {REQUIRES}. Nothing was read or changed.")


def hex_colour(rgb) -> str:
	return "#{:02x}{:02x}{:02x}".format(*rgb)


def legend() -> list:
	return [
		{"aspect": label, "bearing": bearing, "color": hex_colour(rgb)} for label, bearing, rgb in ANCHORS
	]


def aspect_label(bearing) -> str | None:
	"""The nearest of the eight compass points, or None for flat ground."""
	if bearing is None:
		return None
	return ANCHORS[int(((float(bearing) % 360.0) + 22.5) // 45.0) % 8][0]


# ── Web Mercator ────────────────────────────────────────────────────────────
def to_mercator(lon: float, lat: float) -> tuple:
	lat = max(min(float(lat), 85.05112878), -85.05112878)
	return (
		EARTH_RADIUS * math.radians(float(lon)),
		EARTH_RADIUS * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)),
	)


def to_lonlat(x: float, y: float) -> tuple:
	return (
		math.degrees(float(x) / EARTH_RADIUS),
		math.degrees(2 * math.atan(math.exp(float(y) / EARTH_RADIUS)) - math.pi / 2),
	)


def tile_bounds(z: int, x: int, y: int) -> tuple:
	"""`(xmin, ymin, xmax, ymax)` of an XYZ tile in mercator metres. y=0 is north."""
	span = 2 * MERCATOR_HALF / (2**z)
	xmin = -MERCATOR_HALF + x * span
	ymax = MERCATOR_HALF - y * span
	return xmin, ymax - span, xmin + span, ymax


def tiles_covering(bounds: tuple, z: int) -> list:
	"""Every `(x, y)` at zoom `z` touching mercator `bounds`."""
	xmin, ymin, xmax, ymax = bounds
	n = 2**z
	span = 2 * MERCATOR_HALF / n
	x0 = max(0, int((xmin + MERCATOR_HALF) // span))
	x1 = min(n - 1, int((xmax + MERCATOR_HALF) // span))
	y0 = max(0, int((MERCATOR_HALF - ymax) // span))
	y1 = min(n - 1, int((MERCATOR_HALF - ymin) // span))
	return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


# ── the extent ──────────────────────────────────────────────────────────────
def _bbox_of(text):
	try:
		ring = json.loads(str(text or ""))["coordinates"][0]
		lons = [float(p[0]) for p in ring]
		lats = [float(p[1]) for p in ring]
	except Exception:
		return None
	box = (min(lons), min(lats), max(lons), max(lats))
	if not (-180 <= box[0] <= 180 and -90 <= box[1] <= 90 and -180 <= box[2] <= 180 and -90 <= box[3] <= 90):
		return None
	if not any(box):
		# Null Island: an unset pair, not a place.
		return None
	return box


def boundary_rows(doctype: str, company: str = "") -> list:
	"""Rows of `doctype` carrying a boundary, narrowed to one entity if asked."""
	if not compat.doctype_exists(doctype) or not compat.has_field(doctype, "boundary_bbox_geojson"):
		return []
	fields = compat.existing_fields(
		doctype, ("name", "owning_entity", "field_name", "boundary_geojson", "boundary_bbox_geojson")
	)
	filters = {"owning_entity": company} if company and compat.has_field(doctype, "owning_entity") else {}
	rows = frappe.db.get_all(doctype, filters=filters, fields=fields, limit=10000) or []
	return [dict(row) for row in rows if _bbox_of(dict(row).get("boundary_bbox_geojson"))]


def farm_extent(company: str = "") -> dict | None:
	"""The lon/lat box around every Parcel and Field boundary. None if there are none."""
	boxes = []
	counts = {}
	for doctype in (PARCEL, FIELD):
		rows = boundary_rows(doctype, company)
		counts[doctype] = len(rows)
		boxes.extend(_bbox_of(row.get("boundary_bbox_geojson")) for row in rows)
	if not boxes:
		return None
	return {
		"west": min(b[0] for b in boxes),
		"south": min(b[1] for b in boxes),
		"east": max(b[2] for b in boxes),
		"north": max(b[3] for b in boxes),
		"parcels": counts.get(PARCEL, 0),
		"fields": counts.get(FIELD, 0),
	}


def plan_grid(extent: dict, buffer_metres: float = DEFAULT_BUFFER_METRES) -> dict:
	"""The mercator grid a build fetches: origin, cell size, width and height.

	The cell is `NATIVE_GROUND_METRES` of GROUND at the middle latitude, which
	is sec(latitude) mercator metres — so a farm at 47° N asks for ~14.8 m
	mercator cells and gets ~10 m on the ground. The box is snapped outwards to
	whole cells so the export never resamples to fit.
	"""
	mid_lat = (float(extent["south"]) + float(extent["north"])) / 2
	scale = 1.0 / math.cos(math.radians(mid_lat))
	cell = NATIVE_GROUND_METRES * scale
	pad = float(buffer_metres) * scale
	x0, y0 = to_mercator(extent["west"], extent["south"])
	x1, y1 = to_mercator(extent["east"], extent["north"])
	x0 = math.floor((x0 - pad) / cell) * cell
	y0 = math.floor((y0 - pad) / cell) * cell
	x1 = math.ceil((x1 + pad) / cell) * cell
	y1 = math.ceil((y1 + pad) / cell) * cell
	width = round((x1 - x0) / cell)
	height = round((y1 - y0) / cell)
	return {"xmin": x0, "ymax": y1, "cell": cell, "width": width, "height": height}


def grid_bounds(grid: dict) -> tuple:
	return (
		grid["xmin"],
		grid["ymax"] - grid["cell"] * grid["height"],
		grid["xmin"] + grid["cell"] * grid["width"],
		grid["ymax"],
	)


def grid_lonlat_bounds(grid: dict) -> dict:
	xmin, ymin, xmax, ymax = grid_bounds(grid)
	west, south = to_lonlat(xmin, ymin)
	east, north = to_lonlat(xmax, ymax)
	return {
		"west": round(west, 7),
		"south": round(south, 7),
		"east": round(east, 7),
		"north": round(north, 7),
	}


def count_tiles(grid: dict, max_zoom: int = PREBUILD_MAX_ZOOM) -> int:
	bounds = grid_bounds(grid)
	return sum(len(tiles_covering(bounds, z)) for z in range(MIN_ZOOM, max_zoom + 1))


# ── fetching ────────────────────────────────────────────────────────────────
def _http_get(url: str, params: dict | None = None, timeout: int = HTTP_TIMEOUT_SECONDS):
	"""`(status, content_bytes)`. The one network call; the suite replaces it."""
	import requests

	response = requests.get(url, params=params, timeout=timeout)
	return int(response.status_code), response.content


def _get_json(url: str, params: dict | None = None) -> dict:
	try:
		status, body = _http_get(url, params)
	except Exception as exc:
		raise ToolError(f"USGS did not answer ({type(exc).__name__}: {exc}). Nothing was changed.") from None
	if status != 200:
		raise ToolError(f"USGS answered HTTP {status}. Nothing was changed — try again later.")
	try:
		payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
	except Exception:
		raise ToolError("USGS answered with something that is not JSON. Nothing was changed.") from None
	if not isinstance(payload, dict):
		raise ToolError("USGS answered with an unexpected shape. Nothing was changed.")
	return payload


def fetch_elevation(grid: dict):
	"""The DEM over `grid`, as a float32 array with NaN for no data."""
	_require_numpy()
	xmin, ymin, xmax, ymax = grid_bounds(grid)
	params = {
		"bbox": f"{xmin},{ymin},{xmax},{ymax}",
		"bboxSR": 3857,
		"imageSR": 3857,
		"size": f"{grid['width']},{grid['height']}",
		"format": "tiff",
		"pixelType": "F32",
		"noData": str(NODATA),
		"interpolation": "RSP_BilinearInterpolation",
		"mosaicRule": json.dumps(MOSAIC_RULE),
		"f": "json",
	}
	answer = _get_json(EXPORT_URL, params)
	href = str(answer.get("href") or "")
	if not href.startswith("https://"):
		detail = (answer.get("error") or {}).get("message") if isinstance(answer.get("error"), dict) else ""
		raise ToolError(
			f"The USGS elevation service returned no image{': ' + detail if detail else ''}. Nothing was changed."
		)
	try:
		status, raw = _http_get(href)
	except Exception as exc:
		raise ToolError(
			f"The USGS image did not download ({type(exc).__name__}). Nothing was changed."
		) from None
	if status != 200 or not raw:
		raise ToolError(f"The USGS image download answered HTTP {status}. Nothing was changed.")
	dem = read_geotiff(raw)
	if dem.shape != (grid["height"], grid["width"]):
		raise ToolError(
			f"USGS returned a {dem.shape[1]}×{dem.shape[0]} image for a "
			f"{grid['width']}×{grid['height']} request, so its cells cannot be placed. Nothing was changed."
		)
	return dem


def read_geotiff(raw: bytes):
	"""A single-band float GeoTIFF as float32, NaN where there is no data.

	rasterio when the bench has it; Pillow otherwise. Georeferencing is not read
	back from the file: the request fixed it, and the shape check in
	`fetch_elevation` is what proves the answer matches the request.
	"""
	_require_numpy()
	array = None
	try:
		from rasterio.io import MemoryFile  # pragma: no cover - not in the shipped image

		with MemoryFile(raw) as memory, memory.open() as dataset:  # pragma: no cover
			array = dataset.read(1).astype("float32")
	except ImportError:
		array = None
	except Exception:  # pragma: no cover - a file rasterio refuses; let Pillow try
		array = None
	if array is None:
		try:
			from PIL import Image

			with Image.open(io.BytesIO(raw)) as image:
				image.load()
				if image.mode not in ("F", "I", "I;16", "I;16S"):
					image = image.convert("F")
				array = np.asarray(image, dtype="float32")
		except Exception as exc:
			raise ToolError(
				f"The USGS elevation file could not be read ({type(exc).__name__}). Nothing was changed."
			) from None
	array = np.array(array, dtype="float32")
	array[~np.isfinite(array) | (array < NODATA_BELOW)] = np.nan
	return array


def usgs_products(extent: dict) -> list:
	"""The 1/3 arc-second products under the farm, from the TNM Access API.

	Provenance only, and best-effort: a TNM outage must not stop a build whose
	pixels already arrived.
	"""
	params = {
		"datasets": TNM_DATASET,
		"bbox": f"{extent['west']},{extent['south']},{extent['east']},{extent['north']}",
		"outputFormat": "JSON",
		"max": 10,
	}
	try:
		payload = _get_json(TNM_PRODUCTS_URL, params)
	except ToolError:
		return []
	out = []
	for item in payload.get("items") or []:
		if not isinstance(item, dict):
			continue
		out.append(
			{
				"title": item.get("title") or None,
				"publication_date": item.get("publicationDate") or None,
				"download_url": item.get("downloadURL") or None,
				"source_id": item.get("sourceId") or None,
			}
		)
	return out


# ── the terrain arithmetic ──────────────────────────────────────────────────
def row_ground_cells(grid: dict):
	"""The true ground size of one cell, per row, from each row's latitude."""
	cell = grid["cell"]
	centres = grid["ymax"] - (np.arange(grid["height"], dtype="float64") + 0.5) * cell
	lats = 2 * np.arctan(np.exp(centres / EARTH_RADIUS)) - math.pi / 2
	return cell * np.cos(lats)


def gradient(dem, ground_cells):
	"""`(aspect_degrees, slope_degrees)` by Horn's method.

	`ground_cells` is the ground size of a cell per row (square cells, as a
	mercator grid's are). Aspect is the compass bearing the slope FACES — the
	downhill direction — clockwise from north; NaN on flat ground and wherever
	any of the nine cells has no data. Edge rows and columns reuse their
	neighbour, as `gdaldem -compute_edges` does.
	"""
	_require_numpy()
	z = np.pad(np.asarray(dem, dtype="float64"), 1, mode="edge")
	a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
	d, f = z[1:-1, :-2], z[1:-1, 2:]
	g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
	size = np.asarray(ground_cells, dtype="float64").reshape(-1, 1)
	dz_east = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * size)
	# Rows run SOUTHWARD, so this is the rise toward the south.
	dz_south = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * size)
	slope = np.degrees(np.arctan(np.hypot(dz_east, dz_south)))
	# Downhill is (-dz_east, -dz_north) = (-dz_east, +dz_south) in (east, north).
	aspect = np.degrees(np.arctan2(-dz_east, dz_south)) % 360.0
	aspect[slope < FLAT_DEGREES] = np.nan
	aspect[~np.isfinite(slope)] = np.nan
	return aspect.astype("float32"), slope.astype("float32")


def colourise(aspect, slope):
	"""RGBA uint8 for every cell. Transparent where there is no data."""
	_require_numpy()
	aspect = np.asarray(aspect, dtype="float64")
	slope = np.asarray(slope, dtype="float64")
	anchors = np.array([rgb for _label, _bearing, rgb in ANCHORS] + [ANCHORS[0][2]], dtype="float64")
	position = np.nan_to_num(aspect, nan=0.0) / 45.0
	index = np.floor(position).astype(int) % 8
	frac = (position - np.floor(position))[..., None]
	hue = anchors[index] * (1 - frac) + anchors[index + 1] * frac

	valid = np.isfinite(slope)
	strength = np.clip((np.nan_to_num(slope) - FLAT_DEGREES) / (STEEP_DEGREES - FLAT_DEGREES), 0.0, 1.0)
	strength[~np.isfinite(aspect)] = 0.0
	grey = np.array(FLAT_GREY, dtype="float64")
	rgb = grey + (hue - grey) * strength[..., None]

	out = np.zeros((*aspect.shape, 4), dtype="uint8")
	out[..., :3] = np.clip(np.rint(rgb), 0, 255).astype("uint8")
	out[..., 3] = np.where(valid, ALPHA, 0).astype("uint8")
	out[~valid, :3] = 0
	return out


# ── PNG ─────────────────────────────────────────────────────────────────────
def _chunk(kind: bytes, payload: bytes) -> bytes:
	return (
		struct.pack(">I", len(payload))
		+ kind
		+ payload
		+ struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
	)


def png_rgba(pixels) -> bytes:
	"""An 8-bit RGBA PNG from an HxWx4 uint8 array. zlib and struct only."""
	height, width = int(pixels.shape[0]), int(pixels.shape[1])
	rows = np.zeros((height, width * 4 + 1), dtype="uint8")
	rows[:, 1:] = np.ascontiguousarray(pixels, dtype="uint8").reshape(height, width * 4)
	return b"".join(
		[
			b"\x89PNG\r\n\x1a\n",
			_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
			_chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)),
			_chunk(b"IEND", b""),
		]
	)


def _transparent_tile() -> bytes:
	raw = b"\x00" * (TILE_SIZE * 4 + 1) * TILE_SIZE
	return b"".join(
		[
			b"\x89PNG\r\n\x1a\n",
			_chunk(b"IHDR", struct.pack(">IIBBBBB", TILE_SIZE, TILE_SIZE, 8, 6, 0, 0, 0)),
			_chunk(b"IDAT", zlib.compress(raw, 9)),
			_chunk(b"IEND", b""),
		]
	)


#: What a tile with nothing on it answers. 200 and a real PNG rather than a 404
#: or a 204, so `MKTileOverlay` draws nothing without logging a failure per tile.
TRANSPARENT_TILE = _transparent_tile()


# ── the cache ───────────────────────────────────────────────────────────────
def cache_dir() -> str:
	return frappe.get_site_path("private", "slope_aspect")


def _meta_path(root: str | None = None) -> str:
	return os.path.join(root or cache_dir(), "meta.json")


def read_meta() -> dict | None:
	try:
		with open(_meta_path(), encoding="utf-8") as handle:
			meta = json.load(handle)
	except Exception:
		return None
	if not isinstance(meta, dict) or int(meta.get("algorithm_version") or 0) != ALGORITHM_VERSION:
		return None
	return meta


#: One loaded grid per worker, keyed on the build stamp.
_GRID_CACHE: dict = {}


def load_grid(meta: dict) -> dict:
	key = (cache_dir(), meta.get("built_at"))
	cached = _GRID_CACHE.get("grid")
	if cached and cached[0] == key:
		return cached[1]
	with np.load(os.path.join(cache_dir(), "grid.npz")) as data:
		loaded = {"aspect": data["aspect"], "slope": data["slope"], "rgba": data["rgba"]}
	_GRID_CACHE["grid"] = (key, loaded)
	return loaded


def render_tile_pixels(grid: dict, rgba, z: int, x: int, y: int):
	"""The 256×256 RGBA for one tile, nearest-cell sampled from the grid."""
	xmin, _ymin, xmax, ymax = tile_bounds(z, x, y)
	step = (xmax - xmin) / TILE_SIZE
	centres = (np.arange(TILE_SIZE, dtype="float64") + 0.5) * step
	cols = np.floor((xmin + centres - grid["xmin"]) / grid["cell"]).astype(int)
	rows = np.floor((grid["ymax"] - (ymax - centres)) / grid["cell"]).astype(int)
	col_ok = (cols >= 0) & (cols < grid["width"])
	row_ok = (rows >= 0) & (rows < grid["height"])
	out = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype="uint8")
	if not col_ok.any() or not row_ok.any():
		return out
	picked = rgba[np.ix_(rows[row_ok], cols[col_ok])]
	out[np.ix_(np.nonzero(row_ok)[0], np.nonzero(col_ok)[0])] = picked
	return out


def valid_tile(z, x, y) -> bool:
	try:
		z, x, y = int(z), int(x), int(y)
	except (TypeError, ValueError):
		return False
	return 0 <= z <= 30 and 0 <= x < 2**z and 0 <= y < 2**z


def tile_png(z: int, x: int, y: int) -> bytes | None:
	"""One tile's PNG bytes. None when the layer has never been built.

	A tile outside the zoom range or the coverage is the transparent tile. A
	tile inside it is served from disk, or rendered from the grid and kept.
	"""
	z, x, y = int(z), int(x), int(y)
	meta = read_meta()
	if meta is None:
		return None
	if not HAVE_NUMPY:
		_require_numpy()
	if z < MIN_ZOOM or z > MAX_ZOOM or not valid_tile(z, x, y):
		return TRANSPARENT_TILE
	grid = meta["grid"]
	gx0, gy0, gx1, gy1 = grid_bounds(grid)
	tx0, ty0, tx1, ty1 = tile_bounds(z, x, y)
	if tx1 <= gx0 or tx0 >= gx1 or ty1 <= gy0 or ty0 >= gy1:
		return TRANSPARENT_TILE
	path = os.path.join(cache_dir(), "tiles", str(z), str(x), f"{y}.png")
	try:
		with open(path, "rb") as handle:
			return handle.read()
	except OSError:
		pass
	loaded = load_grid(meta)
	png = png_rgba(render_tile_pixels(grid, loaded["rgba"], z, x, y))
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		temporary = f"{path}.{os.getpid()}.tmp"
		with open(temporary, "wb") as handle:
			handle.write(png)
		os.replace(temporary, path)
	except OSError:  # pragma: no cover - a read-only site folder still serves
		pass
	return png


# ── building ────────────────────────────────────────────────────────────────
def build(company: str = "", buffer_metres=None, dry_run: bool = False) -> dict:
	"""Fetch, compute, colour and cache the layer. Replaces any previous build."""
	_require_numpy()
	buffer_metres = DEFAULT_BUFFER_METRES if buffer_metres in (None, "") else float(buffer_metres)
	if buffer_metres < 0 or buffer_metres > MAX_BUFFER_METRES:
		raise ToolError(
			f"buffer_metres must be between 0 and {int(MAX_BUFFER_METRES)}, got {buffer_metres:g}. "
			"Nothing was changed."
		)
	extent = farm_extent(company)
	if extent is None:
		raise ToolError(
			f"No Parcel or Field on {company or 'this site'} has a boundary, so there is no ground to "
			"compute aspect over. Draw or import boundaries first (set_parcel_boundary, "
			"set_field_boundary, import_field_boundary_geojson). Nothing was changed."
		)
	grid = plan_grid(extent, buffer_metres)
	if grid["width"] > MAX_GRID_SIDE or grid["height"] > MAX_GRID_SIDE:
		raise ToolError(
			f"The boundaries on {company or 'this site'} span {grid['width']}×{grid['height']} "
			f"10 m cells, over the {MAX_GRID_SIDE}-cell limit — the farm's parcels are too far apart "
			"for one layer. Pass `company` to build one entity's ground. Nothing was changed."
		)
	plan = {
		"company": company or None,
		"extent": {key: extent[key] for key in ("west", "south", "east", "north")},
		"boundaries": {"parcels": extent["parcels"], "fields": extent["fields"]},
		"buffer_metres": buffer_metres,
		"bounds": grid_lonlat_bounds(grid),
		"grid": {
			"width": grid["width"],
			"height": grid["height"],
			"ground_cell_metres": NATIVE_GROUND_METRES,
		},
		"tiles_to_prerender": count_tiles(grid),
	}
	if dry_run:
		return {**plan, "dry_run": True}

	dem = fetch_elevation(grid)
	valid_cells = int(np.isfinite(dem).sum())
	if not valid_cells:
		raise ToolError(
			"USGS holds no 1/3 arc-second elevation under these boundaries (3DEP covers the United "
			"States and its territories). Nothing was changed."
		)
	aspect, slope = gradient(dem, row_ground_cells(grid))
	rgba = colourise(aspect, slope)
	products = usgs_products(extent)

	built_at = str(frappe.utils.now())
	root = cache_dir()
	staging = f"{root}.building.{os.getpid()}"
	shutil.rmtree(staging, ignore_errors=True)
	os.makedirs(staging, exist_ok=True)
	np.savez_compressed(os.path.join(staging, "grid.npz"), aspect=aspect, slope=slope, rgba=rgba)

	bounds = grid_bounds(grid)
	rendered = 0
	for z in range(MIN_ZOOM, PREBUILD_MAX_ZOOM + 1):
		for x, y in tiles_covering(bounds, z):
			pixels = render_tile_pixels(grid, rgba, z, x, y)
			if not pixels[..., 3].any():
				continue
			folder = os.path.join(staging, "tiles", str(z), str(x))
			os.makedirs(folder, exist_ok=True)
			with open(os.path.join(folder, f"{y}.png"), "wb") as handle:
				handle.write(png_rgba(pixels))
			rendered += 1

	finite_slope = slope[np.isfinite(slope)]
	meta = {
		"algorithm_version": ALGORITHM_VERSION,
		"built_at": built_at,
		"built_by": str(getattr(frappe.session, "user", "") or "") or None,
		**plan,
		"grid": grid,
		"source": SOURCE_LABEL,
		"source_url": EXPORT_URL,
		"mosaic_rule": MOSAIC_RULE,
		"products": products,
		"cells_with_data": valid_cells,
		"cells_total": int(grid["width"] * grid["height"]),
		"slope_degrees_max": round(float(finite_slope.max()), 1) if finite_slope.size else None,
		"elevation_metres": {
			"min": round(float(np.nanmin(dem)), 1),
			"max": round(float(np.nanmax(dem)), 1),
		},
		"tiles_prerendered": rendered,
	}
	with open(_meta_path(staging), "w", encoding="utf-8") as handle:
		json.dump(meta, handle, indent=1, default=str)

	retired = f"{root}.retired.{os.getpid()}"
	if os.path.isdir(root):
		os.replace(root, retired)
	os.replace(staging, root)
	shutil.rmtree(retired, ignore_errors=True)
	_GRID_CACHE.clear()
	return meta


# ── describing ──────────────────────────────────────────────────────────────
def describe(meta: dict | None = None) -> dict:
	"""What a map needs to draw the layer, or why it cannot."""
	meta = meta if meta is not None else read_meta()
	base = {
		"key": "slope_aspect",
		"label": "Slope aspect",
		"detail": (
			"Which way the ground faces. South-facing slopes (red) warm first in spring and "
			"ripen earliest; north-facing (blue) run late; east green, west yellow. Grey is "
			f"flat — under {FLAT_DEGREES:g}° it faces nowhere. Colour strengthens to "
			f"{STEEP_DEGREES:g}°."
		),
		"tile_url_template": TILE_URL_TEMPLATE,
		"tile_size": TILE_SIZE,
		"min_zoom": MIN_ZOOM,
		"max_zoom": MAX_ZOOM,
		"legend": legend(),
		"flat_slope_degrees": FLAT_DEGREES,
		"steep_slope_degrees": STEEP_DEGREES,
		"opacity": round(ALPHA / 255, 2),
		"source": SOURCE_LABEL,
	}
	if meta is None:
		return {
			**base,
			"available": False,
			"reason": (
				"Not built on this site yet. An operator runs build_slope_aspect_layer once; it "
				"fetches USGS elevation for the farm's boundaries and caches the tiles."
			),
		}
	return {
		**base,
		"available": True,
		"bounds": meta.get("bounds"),
		"built_at": meta.get("built_at"),
		"company": meta.get("company"),
		"products": meta.get("products") or [],
		"elevation_metres": meta.get("elevation_metres"),
		"slope_degrees_max": meta.get("slope_degrees_max"),
	}


def _circular_mean(bearings, weights):
	east = float(np.sum(np.sin(np.radians(bearings)) * weights))
	north = float(np.sum(np.cos(np.radians(bearings)) * weights))
	if abs(east) < 1e-12 and abs(north) < 1e-12:
		return None
	return round(math.degrees(math.atan2(east, north)) % 360.0, 1)


def summarise(aspect, slope) -> dict | None:
	"""What one block's cells say about which way it faces."""
	aspect = np.asarray(aspect, dtype="float64")
	slope = np.asarray(slope, dtype="float64")
	have = np.isfinite(slope)
	cells = int(have.sum())
	if not cells:
		return None
	aspect, slope = aspect[have], slope[have]
	tilted = np.isfinite(aspect)
	faced = aspect[tilted]
	south = int(((faced >= SOUTH_FACING[0]) & (faced < SOUTH_FACING[1])).sum())
	north = int(((faced >= NORTH_FACING[0]) | (faced < NORTH_FACING[1])).sum())
	counts = {label: 0 for label, _b, _c in ANCHORS}
	for bearing in faced:
		counts[aspect_label(bearing)] += 1
	mean_bearing = _circular_mean(faced, np.sin(np.radians(slope[tilted]))) if faced.size else None
	# Toward the sun: sin(slope) × −cos(aspect). +1 is a cliff facing due south,
	# −1 one facing due north, 0 flat or east/west. Flat cells count as 0.
	southness = np.zeros(cells)
	southness[tilted] = np.sin(np.radians(slope[tilted])) * -np.cos(np.radians(faced))
	dominant = max(counts, key=lambda key: counts[key]) if faced.size else None
	return {
		"cells": cells,
		"mean_slope_degrees": round(float(slope.mean()), 1),
		"max_slope_degrees": round(float(slope.max()), 1),
		"mean_aspect_degrees": mean_bearing,
		"mean_aspect": aspect_label(mean_bearing),
		"dominant_aspect": dominant,
		"south_facing_share": round(south / cells, 3),
		"north_facing_share": round(north / cells, 3),
		"flat_share": round(float((~tilted).sum()) / cells, 3),
		"aspect_shares": {key: round(value / cells, 3) for key, value in counts.items()},
		"southness_index": round(float(southness.mean()), 4),
	}


def block_cells(company: str = "", meta: dict | None = None) -> tuple[list, list]:
	"""Every Field's cells on the built layer. `([(row, aspect, slope)], warnings)`.

	v0.168.0, split out of `block_summaries` so the slope GRADE layer reads the
	same cells for a block that the aspect ranking does — two answers about one
	block's ground must not disagree about which ground it is. `aspect` and
	`slope` are 1-D and EMPTY for a block outside the layer; a boundary that
	cannot be read is a warning and no entry.
	"""
	meta = meta if meta is not None else read_meta()
	if meta is None:
		return [], []
	try:
		from shapely import contains_xy
		from shapely.geometry import shape
	except Exception:
		return [], ["Per-block summaries need the shapely package; the tiles do not."]
	grid = meta["grid"]
	loaded = load_grid(meta)
	empty = np.zeros(0, dtype="float32")
	out, warnings = [], []
	for row in boundary_rows(FIELD, company):
		try:
			polygon = shape(json.loads(row.get("boundary_geojson") or ""))
		except Exception:
			warnings.append(f"{row['name']}'s boundary could not be read, so it has no summary.")
			continue
		west, south, east, north = polygon.bounds
		x0, y0 = to_mercator(west, south)
		x1, y1 = to_mercator(east, north)
		c0 = max(0, int((x0 - grid["xmin"]) // grid["cell"]))
		c1 = min(grid["width"] - 1, int((x1 - grid["xmin"]) // grid["cell"]))
		r0 = max(0, int((grid["ymax"] - y1) // grid["cell"]))
		r1 = min(grid["height"] - 1, int((grid["ymax"] - y0) // grid["cell"]))
		aspect, slope = empty, empty
		if c0 <= c1 and r0 <= r1:
			cols = np.arange(c0, c1 + 1)
			rows = np.arange(r0, r1 + 1)
			xs = grid["xmin"] + (cols + 0.5) * grid["cell"]
			ys = grid["ymax"] - (rows + 0.5) * grid["cell"]
			lons = np.degrees(xs / EARTH_RADIUS)
			lats = np.degrees(2 * np.arctan(np.exp(ys / EARTH_RADIUS)) - math.pi / 2)
			lon_grid, lat_grid = np.meshgrid(lons, lats)
			inside = contains_xy(polygon, lon_grid, lat_grid)
			if not inside.any():
				# A block narrower than one 10 m cell: take the cell under its middle.
				point = polygon.representative_point()
				px, py = to_mercator(point.x, point.y)
				col = int((px - grid["xmin"]) // grid["cell"])
				rw = int((grid["ymax"] - py) // grid["cell"])
				if 0 <= col < grid["width"] and 0 <= rw < grid["height"]:
					aspect = loaded["aspect"][rw : rw + 1, col : col + 1].reshape(-1)
					slope = loaded["slope"][rw : rw + 1, col : col + 1].reshape(-1)
			else:
				aspect = loaded["aspect"][r0 : r1 + 1, c0 : c1 + 1][inside]
				slope = loaded["slope"][r0 : r1 + 1, c0 : c1 + 1][inside]
		out.append((row, aspect, slope))
	return out, warnings


def block_summaries(company: str = "", meta: dict | None = None) -> tuple[list, list]:
	"""Every Field inside the coverage, with its aspect summary. `(rows, warnings)`."""
	cells, warnings = block_cells(company, meta)
	out = []
	for row, aspect, slope in cells:
		stats = summarise(aspect, slope) if slope.size else None
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
	out.sort(key=lambda entry: (-entry["southness_index"], entry["field"]))
	for rank, entry in enumerate(out, start=1):
		entry["earliness_rank"] = rank
	return out, warnings


def aspect_at(latitude, longitude, meta: dict | None = None) -> dict | None:
	"""The aspect and slope of the one cell under a point, or None outside the layer."""
	meta = meta if meta is not None else read_meta()
	if meta is None:
		return None
	grid = meta["grid"]
	x, y = to_mercator(float(longitude), float(latitude))
	col = int((x - grid["xmin"]) // grid["cell"])
	row = int((grid["ymax"] - y) // grid["cell"])
	if not (0 <= col < grid["width"] and 0 <= row < grid["height"]):
		return None
	loaded = load_grid(meta)
	slope = float(loaded["slope"][row, col])
	if not math.isfinite(slope):
		return None
	bearing = float(loaded["aspect"][row, col])
	bearing = round(bearing, 1) if math.isfinite(bearing) else None
	return {
		"latitude": float(latitude),
		"longitude": float(longitude),
		"aspect_degrees": bearing,
		"aspect": aspect_label(bearing),
		"slope_degrees": round(slope, 1),
		"color": hex_colour(tuple(int(v) for v in loaded["rgba"][row, col, :3])),
	}
