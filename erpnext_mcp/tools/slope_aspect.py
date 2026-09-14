# SPDX-License-Identifier: MIT
"""Slope aspect, as tools: read the layer and rank the blocks, or build it.

v0.167.0. `erpnext_mcp/slope_aspect.py` is the engine and argues every decision
in it; this file is the two doors onto it.

  `get_slope_aspect_layer`    what a map needs to draw the tiles, and every
                              block ranked by how much it faces the sun. READ.
  `build_slope_aspect_layer`  fetch USGS elevation for the farm's boundaries,
                              compute aspect and cache the tiles. MUTATING, off
                              — it writes files under the site and calls out to
                              a public service.
"""

from __future__ import annotations

from .. import slope_aspect
from ..args import as_bool, as_float, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def _company(args: dict) -> str:
	"""An entity only when one was NAMED. `resolve_company` infers the only
	company on a single-company site, and inferring one here would be harmless;
	on a multi-company site it answers None, which is the whole farm."""
	if not as_str(args, "company"):
		return ""
	return resolve_company(as_str(args, "company")) or ""


def _number(args: dict, key: str):
	"""None when not passed. `as_float` answers 0.0 for a missing value, and a
	missing longitude read as 0 is a point on the prime meridian."""
	value = args.get(key)
	if value is None or value == "":
		return None
	return as_float(value, key)


def get_slope_aspect_layer(args: dict) -> ToolResult:
	"""The layer descriptor, the per-block ranking, and optionally one point."""
	meta = slope_aspect.read_meta()
	data = slope_aspect.describe(meta)
	company = _company(args)
	warnings: list = []

	if meta is not None and as_bool(args, "include_blocks", True):
		slope_aspect._require_numpy()
		blocks, warnings = slope_aspect.block_summaries(company, meta)
		data["blocks"] = blocks
		data["block_count"] = len(blocks)
		data["southness_index_note"] = (
			"sin(slope) × −cos(aspect), averaged over the block's 10 m cells: +1 would be a "
			"cliff facing due south, −1 due north, 0 flat or facing east or west. Higher warms "
			"earlier in spring. earliness_rank 1 is the most sun-facing block."
		)

	latitude = _number(args, "latitude")
	longitude = _number(args, "longitude")
	if latitude is not None or longitude is not None:
		if latitude is None or longitude is None:
			raise ToolError("latitude and longitude go together. Nothing was read.")
		if meta is None:
			data["point"] = None
		else:
			slope_aspect._require_numpy()
			data["point"] = slope_aspect.aspect_at(latitude, longitude, meta)
			if data["point"] is None:
				warnings.append("That point is outside the built layer or over no data.")
	data["warnings"] = warnings

	if meta is None:
		return ToolResult(data=data, summary="Slope aspect layer not built on this site")
	ranked = data.get("blocks") or []
	lead = f"; most sun-facing block {ranked[0]['field']}" if ranked else ""
	return ToolResult(
		data=data,
		summary=f"Slope aspect layer built {meta.get('built_at')}, {len(ranked)} block(s) summarised{lead}",
	)


def build_slope_aspect_layer(args: dict) -> ToolResult:
	"""Fetch, compute and cache. Replaces any previous build."""
	company = _company(args)
	buffer_metres = _number(args, "buffer_metres")
	dry_run = as_bool(args, "dry_run", False)
	result = slope_aspect.build(company=company, buffer_metres=buffer_metres, dry_run=dry_run)
	grid = result["grid"]
	if dry_run:
		return ToolResult(
			data=result,
			summary=(
				f"Dry run — would fetch {grid['width']}×{grid['height']} cells and pre-render "
				f"{result['tiles_to_prerender']} tile(s)"
			),
		)
	data = {**result, "layer": slope_aspect.describe(result)}
	return ToolResult(
		data=data,
		summary=(
			f"Built the slope aspect layer over {grid['width']}×{grid['height']} cells, "
			f"{result['tiles_prerendered']} tile(s) pre-rendered"
		),
	)
