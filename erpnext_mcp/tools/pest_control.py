# SPDX-License-Identifier: MIT
"""Approval steps on a task, and the pest control application register. v0.204.0.

docs/design/form_schema_and_labels.md §3.3, §5, §6.
"""

from __future__ import annotations

import frappe

from .. import compat, pest_control, security, task_forms
from ..args import as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def approve_task_step(args: dict) -> ToolResult:
	"""Sign one approval step on a Farm Task. The caller must hold the step's role."""
	task = as_str(args, "task", required=True)
	key = as_str(args, "key", required=True)
	user = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not user or user == "Guest":
		raise ToolError("this call has no identity to approve as. Nothing was approved.")
	data = task_forms.approve_step(task, key, user, as_str(args, "signature"))
	return ToolResult(
		data,
		f"{task}: step {key} {'was already approved' if data.get('already') else 'approved'} by {data.get('approved_by')}",
	)


def list_pest_control_applications(args: dict) -> ToolResult:
	"""The pest control application records, newest first."""
	if not compat.doctype_exists(pest_control.DOCTYPE):
		raise ToolError(f"this site has no {pest_control.DOCTYPE} DocType yet — run `bench migrate`.")
	company = resolve_company(as_str(args, "company")) if args.get("company") else ""
	rows = pest_control.list_applications(company or "", as_str(args, "location"), as_limit(args))
	return ToolResult(
		{"count": len(rows), "applications": rows},
		f"{len(rows)} pest control application(s)",
	)
