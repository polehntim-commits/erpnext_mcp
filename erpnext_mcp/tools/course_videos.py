# SPDX-License-Identifier: MIT
"""Course videos and watched amount: the MCP tools. v0.246.0. See `erpnext_mcp.training_videos`."""

from __future__ import annotations

import frappe

from .. import training_videos
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _type(args: dict) -> str:
	if not training_videos.installed():
		raise ToolError("this site has not migrated to v0.246.0 (no Course Video table / Training Evidence).")
	name = as_str(args, "training_type", required=True)
	if not frappe.db.exists(training_videos.TYPE, name):
		raise ToolError(f"no Training Type {name!r}.")
	return name


def _values(args: dict) -> dict:
	out = {}
	for key in ("title", "title_es", "url", "url_es", "section"):
		if key in args:
			out[key] = as_str(args, key)
	for key in ("sort_order", "length_seconds", "min_pct"):
		if key in args:
			out[key] = as_int(args, key)
	if "required" in args:
		out["required"] = as_bool(args, "required", True)
	return out


def add_course_video(args: dict) -> ToolResult:
	name = _type(args)
	try:
		video = training_videos.add(name, _values(args))
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was added.") from None
	return ToolResult(data={"training_type": name, "video": video}, summary=f"{name}: added {video['title']}",
	                  docstatus_delta="0 → 0 (updated)")


def update_course_video(args: dict) -> ToolResult:
	name = _type(args)
	try:
		video = training_videos.update(name, as_str(args, "video", required=True), _values(args))
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	return ToolResult(data={"training_type": name, "video": video}, summary=f"{name}: updated {video['title']}",
	                  docstatus_delta="0 → 0 (updated)")


def remove_course_video(args: dict) -> ToolResult:
	name = _type(args)
	try:
		data = training_videos.remove(name, as_str(args, "video", required=True))
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was removed.") from None
	return ToolResult(data={"training_type": name, **data}, summary=f"{name}: removed {data['title']} (views kept)",
	                  docstatus_delta="0 → 0 (updated)")


def record_video_view(args: dict) -> ToolResult:
	"""One view of one course video, as the phone's player counted it."""
	name = _type(args)
	stretches = args.get("stretches") or []
	if not isinstance(stretches, list):
		raise ToolError("stretches is a list of [start, end] seconds. Nothing was recorded.")
	try:
		data = training_videos.record_view(
			employee=as_str(args, "employee", required=True), training_type=name, video=as_str(args, "video", required=True),
			stretches=stretches, length_seconds=args.get("length_seconds"), play_seconds=args.get("play_seconds"),
			seeks=args.get("seeks"), furthest=args.get("furthest_seconds"), started_at=as_str(args, "started_at"),
			ended_at=as_str(args, "finished_at"), device=as_str(args, "device"),
			measured=as_bool(args, "measured", True), client_request_id=as_str(args, "client_request_id"),
		)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was recorded.") from None
	return ToolResult(
		data={**data, "note": training_videos.FOOTER["en"]},
		summary=f"{data['evidence']}: {data['watched_pct'] if data['watched_pct'] is not None else 'not measured'}"
		+ ("%" if data["watched_pct"] is not None else ""),
		docstatus_delta="none → 0 (created)",
	)


def get_video_progress(args: dict) -> ToolResult:
	name = _type(args)
	employee = as_str(args, "employee", required=True)
	data = training_videos.progress(employee, name)
	return ToolResult(
		data=data,
		summary=f"{employee} on {name}: " + "; ".join(f"{v['title']} — {v['line']}" for v in data["videos"]),
	)
