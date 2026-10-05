# SPDX-License-Identifier: MIT
"""Crop stage: the MCP tools. v0.241.0. See `erpnext_mcp.growth_stage`."""

from __future__ import annotations

import frappe

from .. import growth_stage
from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from .spray_rei import _resolve_block


def _installed() -> None:
	if not growth_stage.installed():
		raise ToolError("this site has not migrated to v0.241.0 (no stage_flags on Crop Observation).")


def record_growth_stage(args: dict) -> ToolResult:
	"""File a block's crop stage (BBCH). A missing, unreadable or backwards code is kept and flagged."""
	_installed()
	block, block_doctype = _resolve_block(as_str(args, "block", required=True), as_str(args, "block_doctype"), "recorded")
	company = resolve_company(as_str(args, "company")) if as_str(args, "company") else ""
	try:
		data = growth_stage.record(
			block=block,
			block_doctype=block_doctype,
			code=as_str(args, "bbch"),
			words=as_str(args, "stage"),
			observed_at=as_str(args, "observed_at"),
			observer=as_str(args, "observer") or frappe.session.user,
			company=company,
			crop=as_str(args, "crop"),
			source_task=as_str(args, "task"),
			gps=as_str(args, "gps"),
			photo=as_str(args, "photo"),
			notes=as_str(args, "notes"),
		)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was recorded.") from None
	flagged = f" — flagged: {data['flags'][0]}" if data["flags"] else ""
	return ToolResult(
		data=data,
		summary=f"{block}: BBCH {data['bbch'] or '—'} ({data['stage'] or 'no words'}){flagged}",
		docstatus_delta="none → 0 (created)",
	)


def get_stage_timeline(args: dict) -> ToolResult:
	"""A block's stages this year (or `year`), oldest first, with flags and the current stage's age."""
	_installed()
	block, _ = _resolve_block(as_str(args, "block", required=True), as_str(args, "block_doctype"), "read")
	data = growth_stage.timeline(block, as_int(args, "year"))
	current = data["current"]
	return ToolResult(
		data=data,
		summary=(f"{block}: BBCH {current['bbch']} ({current['age_days']} day(s) ago)" if current
		         else f"{block}: no stage recorded in {data['year']}")
		+ (f", {data['flagged']} flagged" if data["flagged"] else ""),
	)
