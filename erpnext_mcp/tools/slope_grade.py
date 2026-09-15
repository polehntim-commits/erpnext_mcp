# SPDX-License-Identifier: MIT
"""Slope grade, as a tool: how steep every block is, and whether a machine can work it.

v0.168.0. `erpnext_mcp/slope_grade.py` is the engine and argues every decision
in it. There is no build tool: the grade layer reads the slope the slope aspect
build already cached, so `build_slope_aspect_layer` builds both.

  `get_slope_grade_layer`   the layer descriptor, every block steepest first,
                            and — given an asset — each block classified
                            against that machine's rollover limit. READ.
"""

from __future__ import annotations

from .. import slope_aspect, slope_grade
from ..args import as_bool, as_str
from ..errors import ToolError
from ..result import ToolResult
from .slope_aspect import _company, _number


def get_slope_grade_layer(args: dict) -> ToolResult:
	"""The descriptor, the per-block steepness, and optionally one point."""
	equipment = slope_grade.asset_rating(as_str(args, "asset")) if as_str(args, "asset") else None
	scheme = (
		slope_grade.equipment_scheme(equipment["max_safe_slope_degrees"])
		if equipment
		else slope_grade.standard_scheme()
	)
	meta = slope_aspect.read_meta()
	data = slope_grade.describe(meta, equipment)
	company = _company(args)
	warnings: list = []

	if meta is not None and as_bool(args, "include_blocks", True):
		slope_aspect._require_numpy()
		blocks, warnings = slope_grade.block_summaries(company, meta, scheme)
		data["blocks"] = blocks
		data["block_count"] = len(blocks)
		data["level_note"] = (
			"A block's level and band are its STEEPEST 10 m cell's: 0 green, 1 yellow, 2 orange, "
			"3 red. A 10 m survey smooths banks and ditch edges, so one cell over a break is "
			"ground at least that steep, not noise. band_shares says how much of the block each "
			"band covers. steepness_rank 1 is the most dangerous block."
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
			data["point"] = slope_grade.grade_at(latitude, longitude, meta, scheme)
			if data["point"] is None:
				warnings.append("That point is outside the built layer or over no data.")
	data["warnings"] = warnings

	if meta is None:
		return ToolResult(data=data, summary="Slope grade layer not built on this site")
	ranked = data.get("blocks") or []
	against = f" for {equipment['asset']} ({equipment['max_safe_slope_degrees']:g}°)" if equipment else ""
	over = sum(1 for block in ranked if block["level"] == 3)
	return ToolResult(
		data=data,
		summary=f"Slope grade{against}: {len(ranked)} block(s), {over} with ground in the red band",
	)
