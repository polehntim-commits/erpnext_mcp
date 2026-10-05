# SPDX-License-Identifier: MIT
"""Knowledge checks: the MCP tools. v0.247.0. See `erpnext_mcp.training_quiz`.

Authoring is through the generic config tools (`draft_config` / `preview_config` with kind `quiz`; a person
publishes). Sign-off is a person's act in the Desk or on the phone — there is no MCP sign-off.
"""

from __future__ import annotations

import frappe

from .. import training_quiz, training_videos
from ..args import as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _installed() -> None:
	if not training_videos.installed():
		raise ToolError("this site has not migrated to v0.247.0 (no Training Evidence).")


def submit_quiz_attempt(args: dict) -> ToolResult:
	"""One attempt, graded here against the version it was taken on (the phone's grade is a preview)."""
	_installed()
	answers = args.get("answers") or {}
	if not isinstance(answers, dict):
		raise ToolError("answers is {question id: answer}. Nothing was recorded.")
	try:
		data = training_quiz.submit(
			employee=as_str(args, "employee", required=True), training_type=as_str(args, "training_type", required=True),
			answers=answers, version=args.get("version"), started_at=as_str(args, "started_at"),
			finished_at=as_str(args, "finished_at"), device=as_str(args, "device"),
			client_request_id=as_str(args, "client_request_id"),
		)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was recorded.") from None
	return ToolResult(data=data, summary=f"{data['evidence']}: {data['score_pct']:g}% — {data['result']}",
	                  docstatus_delta="none → 0 (created)")


def mark_short_answers(args: dict) -> ToolResult:
	"""A trainer marks the short answers right or wrong; the result follows."""
	_installed()
	name = as_str(args, "attempt", required=True)
	if not frappe.db.exists(training_quiz.EVIDENCE, name):
		raise ToolError(f"no Training Evidence {name!r}. Nothing was changed.")
	marks = args.get("marks") or {}
	if not isinstance(marks, dict) or not marks:
		raise ToolError("marks is {question id: true | false}. Nothing was changed.")
	try:
		data = training_quiz.mark(name, marks, frappe.session.user)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	return ToolResult(data=data, summary=f"{name}: {data['score_pct']:g}% — {data['result']}",
	                  docstatus_delta="0 → 0 (updated)")


def get_quiz_results(args: dict) -> ToolResult:
	"""Attempts by person, course, or status: 'ready' (for sign-off), 'marking', Passed, Failed."""
	_installed()
	rows = training_quiz.results(as_str(args, "employee"), as_str(args, "training_type"), as_str(args, "status"),
	                             max(1, min(as_int(args, "limit", 100), 500)))
	ready = sum(1 for r in rows if r["signoff_status"] == training_quiz.READY)
	return ToolResult(
		data={"attempts": rows, "count": len(rows), "ready_for_signoff": ready, "enabled": training_quiz.enabled(),
		      "note": "Sign-off is done by a person in the Desk (Training Evidence → Sign Off) or on the phone."},
		summary=f"{len(rows)} attempt(s), {ready} ready for sign-off",
	)
