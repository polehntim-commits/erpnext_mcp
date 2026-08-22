# SPDX-License-Identifier: MIT
"""Satellite vegetation indices: what to ask a provider for, and what to do with the answer.

WHAT IS HERE AND WHAT IS DELIBERATELY NOT. Everything in this module is the part
of a satellite pull that can be got wrong quietly: which bands an index needs,
what the evalscript computes, which acquisition is worth downloading, how a raw
index becomes the 0-100 figure a report compares, and which fields the answer
lands in. The HTTP transport — OAuth against Sentinel Hub, the POST, the retry —
is not here, and that is the point rather than an omission. It is thirty lines
of `requests` that fail loudly when they fail, it cannot be exercised without
credentials, and the farm_app's version of this file was 935 lines in which the
transport was tested by nobody and the index arithmetic was tested by nobody
BECAUSE it was tangled with the transport. Splitting them makes the half that
holds the errors testable.

WHY THE PROVIDER IS NAMED AND THE MODULE IS NOT. Sentinel-2 L2A is what the
farm_app pulled and what `Field.satellite_provider` records, and the evalscripts
below are Sentinel-2 band names. A site on Landsat or on a commercial provider
needs its own band mapping; `METRICS` is where that would go, which is why the
band list is data next to each index rather than baked into a string.

AN UNKNOWN INDEX IS AN ERROR, NOT NDVI. The farm_app's `get_evalscript` ended
`scripts.get(metric_type, scripts['ndvi'])`, so a caller asking for `"nvdi"` — or
for `"moisture"` before that key was added — got NDVI back, computed it, stored
it under the name it asked for, and charted a moisture series that was actually
greenness. Nothing anywhere errored. `evalscript()` raises instead, and that
change is the single most valuable line in this port.

CLOUD IS THE WHOLE PROBLEM WITH OPTICAL SATELLITE DATA. Sentinel-2 revisits every
five days and a cloudy pass is worthless — worse than worthless, because a cloud
over a block reads as a low NDVI and looks exactly like a crop in trouble. So
`pick_acquisition` chooses a pass, states WHY it chose it, and refuses rather
than returning the least-bad option when everything available is too cloudy. A
scheduler that stored a 90%-cloud pass because it was the newest one would raise
an anomaly alert on the weather.

THE INDEX SCALE IS 0-100 IN STORAGE AND RAW IN THE FIELD RECORD, and both are
right. `Field.last_ndvi_mean` holds the raw -1..1 value because that is what an
agronomist reads and what `geo.NDVI_MIN`/`NDVI_MAX` document; the 0-100 index is
what the time series and the anomaly detector compare, for the reason written
out in `ndvi_anomaly`. `to_index` and `from_index` are the only two places the
conversion is written.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: One entry per index this app knows how to ask for. `bands` is what the
#: evalscript declares, `raw_range` is the physical range of the index itself,
#: and `formula` is stated in the comment of the evalscript so a reviewer can
#: check the arithmetic without opening a remote-sensing textbook.
#:
#: NDRE uses the red-edge band B05 and is the one worth having on a mature
#: orchard: NDVI saturates over a full canopy and stops distinguishing a good
#: block from an excellent one, while NDRE keeps resolving nitrogen status after
#: it does.
METRICS = {
	"ndvi": {
		"label": "Normalised Difference Vegetation Index",
		"bands": ("B04", "B08"),
		"raw_range": (-1.0, 1.0),
	},
	"evi": {
		"label": "Enhanced Vegetation Index",
		"bands": ("B02", "B04", "B08"),
		"raw_range": (-1.0, 1.0),
	},
	"ndre": {
		"label": "Normalised Difference Red Edge",
		"bands": ("B05", "B08"),
		"raw_range": (-1.0, 1.0),
	},
	"ndwi": {
		"label": "Normalised Difference Water Index",
		"bands": ("B03", "B08"),
		"raw_range": (-1.0, 1.0),
	},
	"ndmi": {
		"label": "Normalised Difference Moisture Index",
		"bands": ("B08", "B11"),
		"raw_range": (-1.0, 1.0),
	},
	"savi": {
		"label": "Soil Adjusted Vegetation Index",
		"bands": ("B04", "B08"),
		"raw_range": (-1.5, 1.5),
	},
}

#: `moisture` was the farm_app's own name for NDMI and it is in stored rows, so
#: it resolves rather than raising. Aliases are listed rather than folded into
#: `METRICS` so that `list(METRICS)` stays the list of distinct indices.
METRIC_ALIASES = {"moisture": "ndmi", "nd_moisture": "ndmi", "rededge": "ndre", "red_edge": "ndre"}

#: The Sentinel-2 collection these evalscripts are written against. L2A is
#: bottom-of-atmosphere reflectance — already atmospherically corrected, which is
#: what makes two dates comparable at all.
DATA_COLLECTION = "sentinel-2-l2a"

#: Above this cloud fraction a pass is not worth downloading. Thirty percent
#: over a whole tile routinely means zero percent over one 20-acre block, which
#: is why the figure is a default a caller can raise rather than a hard refusal.
MAX_CLOUD_PCT = 30.0

#: How far back a pull will look for a usable pass before giving up. Five-day
#: revisit means a month covers roughly six chances; a block that has had none
#: of them clear has a real weather story and the answer should say so rather
#: than reaching back into a different phenological stage.
MAX_AGE_DAYS = 30

#: The raster this asks for, in pixels. Sentinel-2 is 10 m/pixel in the visible
#: and NIR bands, so 512 px covers 5.1 km — larger than any block on this farm,
#: and asking for more resolution than the sensor has is paying for interpolation.
DEFAULT_SIZE = (512, 512)

#: Fields on `Field` the answer lands in. Named here because two functions write
#: them and a third reads them back, and a typo in one of the three is a column
#: that silently never updates.
FIELD_COLUMNS = {
	"provider": "satellite_provider",
	"pulled_on": "last_ndvi_pull_date",
	"mean": "last_ndvi_mean",
	"stddev": "last_ndvi_stddev",
}


class SatelliteError(ValueError):
	"""An index nobody defined, a window that runs backwards, or a pass too cloudy to use."""


def resolve_metric(metric) -> str:
	"""The canonical index key, or an error naming what is available.

	See the module docstring: this is where the farm_app silently answered NDVI.
	"""
	key = str(metric or "").strip().lower().replace("-", "_")
	key = METRIC_ALIASES.get(key, key)
	if key not in METRICS:
		raise SatelliteError(
			f"{metric!r} is not an index this knows. Available: {', '.join(sorted(METRICS))} "
			f"(aliases: {', '.join(sorted(METRIC_ALIASES))}). Nothing was requested — an unknown "
			"index answered with NDVI is a moisture chart that is secretly a greenness chart."
		)
	return key


def bands_for(metric) -> tuple:
	"""The Sentinel-2 bands an index needs."""
	return METRICS[resolve_metric(metric)]["bands"]


def evalscript(metric) -> str:
	"""The Sentinel Hub evalscript that computes one index, as FLOAT32.

	`dataMask` is requested by every script and used by none of them here: the
	masking happens provider-side through `mosaicking_order` and the cloud filter
	on the acquisition. It is declared because a script that does not declare it
	cannot be extended to use it without another round trip to the provider, and
	every published Sentinel Hub example carries it.
	"""
	key = resolve_metric(metric)
	inputs = ", ".join(f'"{band}"' for band in (*METRICS[key]["bands"], "dataMask"))
	formulas = {
		"ndvi": "(sample.B08 - sample.B04) / (sample.B08 + sample.B04)",
		"evi": "2.5 * (sample.B08 - sample.B04) / (sample.B08 + 6 * sample.B04 - 7.5 * sample.B02 + 1)",
		"ndre": "(sample.B08 - sample.B05) / (sample.B08 + sample.B05)",
		"ndwi": "(sample.B03 - sample.B08) / (sample.B03 + sample.B08)",
		"ndmi": "(sample.B08 - sample.B11) / (sample.B08 + sample.B11)",
		# SAVI's L is the soil brightness correction. 0.5 is the standard value
		# for partial canopy, which is what an orchard floor is all season.
		"savi": "1.5 * (sample.B08 - sample.B04) / (sample.B08 + sample.B04 + 0.5)",
	}
	return (
		"//VERSION=3\n"
		f"// {METRICS[key]['label']} ({key.upper()}) over {DATA_COLLECTION}\n"
		"function setup() {\n"
		f'  return {{ input: [{inputs}], output: {{ bands: 1, sampleType: "FLOAT32" }} }};\n'
		"}\n"
		"function evaluatePixel(sample) {\n"
		f"  return [{formulas[key]}];\n"
		"}\n"
	)


def to_index(value, metric="ndvi"):
	"""The 0-100 index for a raw reading, or `None` if it is not a reading.

	Linear across the index's own physical range, so SAVI's -1.5..1.5 and NDVI's
	-1..1 both land on the same 0-100 scale and a chart can carry both.
	"""
	number = _float(value)
	if number is None:
		return None
	low, high = METRICS[resolve_metric(metric)]["raw_range"]
	clamped = min(max(number, low), high)
	return (clamped - low) / (high - low) * 100.0


def from_index(index, metric="ndvi"):
	"""The raw index value a 0-100 figure came from. The inverse of `to_index`."""
	number = _float(index)
	if number is None:
		return None
	low, high = METRICS[resolve_metric(metric)]["raw_range"]
	return low + min(max(number, 0.0), 100.0) / 100.0 * (high - low)


def pick_acquisition(acquisitions, max_cloud_pct=MAX_CLOUD_PCT, max_age_days=MAX_AGE_DAYS, now=None) -> dict:
	"""The pass worth downloading, with the reason, or the reason there is none.

	`{"chosen": {...}|None, "reason": str, "considered": int, "rejected": [...]}`.

	NEWEST CLEAR WINS, not clearest. A five-day-old pass at 20% cloud is a better
	answer than a twenty-day-old one at 2%, because the question every caller is
	asking is what the block looks like NOW — an older cleaner image answers a
	question about a different week. The rejected list carries each pass and why
	it lost, so a block that has not updated in three weeks can be explained
	without re-querying the provider.
	"""
	moment = _moment(now) or datetime.now(timezone.utc)
	horizon = moment - timedelta(days=int(max_age_days))
	limit = _float(max_cloud_pct)
	limit = MAX_CLOUD_PCT if limit is None else limit

	usable, rejected = [], []
	for entry in acquisitions or []:
		row = dict(entry or {})
		when = _moment(row.get("date") or row.get("timestamp") or row.get("acquired_on"))
		cloud = _float(row.get("cloud_cover") if row.get("cloud_cover") is not None else row.get("cloud"))
		if when is None:
			rejected.append({**row, "why": "no usable acquisition date"})
			continue
		if when < horizon:
			rejected.append({**row, "why": f"older than {max_age_days} days"})
			continue
		if when > moment + timedelta(days=1):
			rejected.append({**row, "why": "dated in the future"})
			continue
		if cloud is None:
			rejected.append({**row, "why": "no cloud cover reported — an unmeasured pass is not a clear one"})
			continue
		if cloud > limit:
			rejected.append({**row, "why": f"{cloud:.0f}% cloud, over the {limit:.0f}% limit"})
			continue
		usable.append((when, cloud, row))

	if not usable:
		return {
			"chosen": None,
			"considered": len(acquisitions or []),
			"rejected": rejected,
			"reason": (
				f"no pass in the last {max_age_days} days was under {limit:.0f}% cloud. This is a "
				"weather fact about the block, not a failure — storing the least-bad pass would "
				"chart the cloud as a crop decline."
			),
		}

	# Newest first; a same-day tie goes to the clearer pass, which happens when
	# two orbits cover one block on one day.
	usable.sort(key=lambda item: (item[0], -item[1]), reverse=True)
	when, cloud, row = usable[0]
	age = (moment - when).days
	return {
		"chosen": row,
		"considered": len(acquisitions or []),
		"rejected": rejected,
		"reason": f"newest pass under the cloud limit: {when.date().isoformat()}, {cloud:.0f}% cloud, {age} day(s) old",
	}


def request_payload(
	bounds, start, end, metric="ndvi", size=DEFAULT_SIZE, max_cloud_pct=MAX_CLOUD_PCT
) -> dict:
	"""The Sentinel Hub Process API body for one pull, as a plain dict.

	The fiddly half of the integration and the half worth testing: a `bbox` in
	the wrong coordinate order, a time range whose `to` precedes its `from`, or a
	missing `mosaickingOrder` all produce a 200 response holding the wrong
	raster. Every one of those is checked here, before anything is sent.

	`bounds` is `(min_lon, min_lat, max_lon, max_lat)` — the order
	`geo.bbox_bounds` hands back, and the order Sentinel Hub expects for
	EPSG:4326.
	"""
	key = resolve_metric(metric)
	try:
		min_lon, min_lat, max_lon, max_lat = (float(value) for value in bounds)
	except (TypeError, ValueError) as problem:
		raise SatelliteError(
			f"bounds must be (min_lon, min_lat, max_lon, max_lat); got {bounds!r}"
		) from problem
	if min_lon >= max_lon or min_lat >= max_lat:
		raise SatelliteError(
			f"bounds are inverted or empty: {bounds!r}. A bbox whose minimum is not below its "
			"maximum returns an empty raster and a 200, which is the failure that looks like a "
			"block with no vegetation."
		)
	if not (-180.0 <= min_lon <= 180.0 and -90.0 <= min_lat <= 90.0 and max_lat <= 90.0):
		raise SatelliteError(f"bounds are not longitude/latitude degrees: {bounds!r}")

	first, last = _day(start), _day(end)
	if not first or not last:
		raise SatelliteError(f"the time window needs two dates; got {start!r} and {end!r}")
	if first > last:
		raise SatelliteError(f"the time window runs backwards: {first} to {last}")

	width, height = (int(value) for value in size)
	if width < 1 or height < 1 or width > 2500 or height > 2500:
		raise SatelliteError(f"size must be 1-2500 px per side; got {size!r}")

	return {
		"input": {
			"bounds": {
				"bbox": [min_lon, min_lat, max_lon, max_lat],
				"properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
			},
			"data": [
				{
					"type": DATA_COLLECTION,
					"dataFilter": {
						"timeRange": {"from": f"{first}T00:00:00Z", "to": f"{last}T23:59:59Z"},
						"maxCloudCoverage": float(max_cloud_pct),
						# Least cloud cover rather than most recent: the window has
						# already been narrowed to usable passes by
						# `pick_acquisition`, so within it the clearest is right.
						"mosaickingOrder": "leastCC",
					},
				}
			],
		},
		"output": {
			"width": width,
			"height": height,
			"responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
		},
		"evalscript": evalscript(key),
	}


def plan_pull(field_row, metric="ndvi", days_back=14, today=None, max_cloud_pct=MAX_CLOUD_PCT) -> dict:
	"""What to ask for, for one block: `{field, metric, bounds, start, end, payload}`.

	Reads the bounding box off the block's STORED `boundary_bbox_geojson`, which
	`geo.derive` writes on every boundary save. Deriving it from the polygon here
	instead would need shapely, which is optional on this app — and a satellite
	pull that could not be planned on a site without a geospatial library would
	be an integration that quietly did nothing on half the installs.
	"""
	from . import geo

	row = dict(field_row or {})
	bounds = geo.bbox_bounds(row.get("boundary_bbox_geojson"))
	if not bounds:
		raise SatelliteError(
			f"block {row.get('name') or row.get('field_name') or 'unnamed'} has no stored boundary "
			"bounding box. Set its boundary first — a satellite pull needs a shape on the ground, "
			"and an acreage figure is not one."
		)
	end = _day(today) or date.today().isoformat()
	start = (date.fromisoformat(end) - timedelta(days=max(1, int(days_back)))).isoformat()
	return {
		"field": row.get("name") or row.get("field_name"),
		"metric": resolve_metric(metric),
		"bounds": bounds,
		"start": start,
		"end": end,
		"payload": request_payload(bounds, start, end, metric, DEFAULT_SIZE, max_cloud_pct),
	}


def summarise_pixels(values, metric="ndvi") -> dict:
	"""`{mean, stddev, index_mean, count, min, max}` over a raster's usable pixels.

	Non-numeric pixels and no-data sentinels are dropped, not zeroed. A masked
	pixel scored as 0.0 pulls a block's mean towards bare ground in proportion to
	how much of it was under cloud, which is the same failure as storing a cloudy
	pass and is harder to see because the number looks plausible.

	The standard deviation is the POPULATION one: these are all the pixels in the
	block, not a sample of them, and the sample correction would be answering a
	question about a wider population that does not exist.
	"""
	key = resolve_metric(metric)
	low, high = METRICS[key]["raw_range"]
	usable = []
	for value in values or []:
		number = _float(value)
		if number is None or number != number:
			continue
		if number < low or number > high:
			continue
		usable.append(number)

	if not usable:
		return {"mean": None, "stddev": None, "index_mean": None, "count": 0, "min": None, "max": None}
	mean = sum(usable) / len(usable)
	variance = sum((value - mean) ** 2 for value in usable) / len(usable)
	return {
		"mean": round(mean, 6),
		"stddev": round(variance**0.5, 6),
		"index_mean": round(to_index(mean, key), 4),
		"count": len(usable),
		"min": round(min(usable), 6),
		"max": round(max(usable), 6),
	}


def field_update(summary: dict, acquired_on=None, provider: str = DATA_COLLECTION, metric="ndvi") -> dict:
	"""The `Field` columns a pull result writes, as `{fieldname: value}`.

	Returns the update rather than performing it, so the decision is testable and
	the write is one line in the caller that owns the transaction.

	ONLY NDVI LANDS ON `Field`. The three stored columns say `ndvi` in their own
	names, and writing a moisture mean into `last_ndvi_mean` because it was the
	metric this run happened to fetch would corrupt the one series the anomaly
	detector reads. Any other index returns an empty update and the caller stores
	it as a time-series row — which is what `Satellite Metric` will be for when it
	ships.
	"""
	if resolve_metric(metric) != "ndvi":
		return {}
	if not summary or summary.get("mean") is None:
		return {}
	return {
		FIELD_COLUMNS["provider"]: provider,
		FIELD_COLUMNS["pulled_on"]: _day(acquired_on) or date.today().isoformat(),
		FIELD_COLUMNS["mean"]: summary["mean"],
		FIELD_COLUMNS["stddev"]: summary.get("stddev"),
	}


def decode_uint16(pixel, metric="ndvi"):
	"""A raw index value out of a UINT16-encoded raster.

	Sentinel Hub can return a compact 16-bit raster instead of FLOAT32, which is
	a quarter of the bytes over a slow link and is what the farm_app's scheduler
	asked for on its overnight run. The encoding maps the index's physical range
	onto 0-65535 linearly, so the decode is the inverse — and it has to know the
	index's range, which is the reason this cannot be a general-purpose helper.
	"""
	number = _float(pixel)
	if number is None:
		return None
	low, high = METRICS[resolve_metric(metric)]["raw_range"]
	return low + (min(max(number, 0.0), 65535.0) / 65535.0) * (high - low)


# ── the parts nobody outside calls ──────────────────────────────────────────
def _float(value):
	if value is None or isinstance(value, bool):
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _day(value) -> str:
	"""An ISO date string from a date, a datetime or text; `""` if there is none."""
	if value is None or isinstance(value, bool):
		return ""
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	text = str(value).strip()
	if not text:
		return ""
	moment = _moment(text)
	return moment.date().isoformat() if moment else ""


def _moment(value):
	"""A timezone-aware datetime from what a provider or a caller supplies."""
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, datetime):
		return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
	if isinstance(value, date):
		return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
	text = str(value).strip().replace("Z", "+00:00")
	if not text:
		return None
	for pattern in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
		try:
			parsed = datetime.fromisoformat(text) if pattern is None else datetime.strptime(text, pattern)
		except ValueError:
			continue
		return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
	return None
