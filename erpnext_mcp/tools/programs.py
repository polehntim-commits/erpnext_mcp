# SPDX-License-Identifier: MIT
"""MCP tools for programs-as-data and device capabilities. v0.205.0.

docs/design/programs_and_field_kinds.md A6, B4.
"""

from __future__ import annotations

import json

from .. import device_capabilities, programs
from ..args import as_bool, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def list_programs(args: dict) -> ToolResult:
	"""The shipped programs and how much of each this site has."""
	out = [programs.status(programs.shipped(name)) for name in sorted(programs.SHIPPED)]
	return ToolResult({"count": len(out), "programs": out}, f"{len(out)} shipped program(s)")


def export_program(args: dict) -> ToolResult:
	"""A bundle built from this site's records — carry a tuned program to another farm."""
	program = as_str(args, "program", required=True)
	parts = {part: args.get(part) or [] for part in programs.PARTS}
	if not any(parts.values()):
		if program in programs.SHIPPED:
			# What this site has of the shipped program, as tuned here.
			parts = programs.status(programs.shipped(program))["present"]
		else:
			raise ToolError(
				"name the parts to export (task_templates, compliance_rules, uoms …) or a shipped program."
			)
	title = args.get("title")
	if isinstance(title, str):
		title = {"en": title}
	bundle = programs.export(program, title, parts)
	return ToolResult(
		{"bundle": bundle, "size": len(json.dumps(bundle))},
		f"exported program {program}: "
		+ ", ".join(f"{len(bundle.get(p) or [])} {p}" for p in programs.PARTS),
	)


def import_program(args: dict) -> ToolResult:
	"""Install a bundle (or a shipped program). Create-only; dry_run by default."""
	bundle = args.get("bundle")
	if isinstance(bundle, str) and bundle.strip():
		try:
			bundle = json.loads(bundle)
		except ValueError as exc:
			raise ToolError(f"bundle is not valid JSON: {exc}") from None
	if not bundle:
		name = as_str(args, "program", required=True)
		bundle = programs.shipped(name)
	dry_run = as_bool(args, "dry_run", True)
	report = programs.import_bundle(bundle, dry_run=bool(dry_run))
	verb = "would create" if dry_run else "created"
	return ToolResult(
		report,
		f"program {report['program']}: {verb} {len(report['created'])}, present {len(report['present'])}, "
		f"refused {len(report['refused'])}",
		docstatus_delta="none" if dry_run else "none → created",
	)


def list_device_capabilities(args: dict) -> ToolResult:
	"""Every active device, its app version, and what it cannot render."""
	company = resolve_company(as_str(args, "company")) if args.get("company") else ""
	rows = device_capabilities.devices(company)
	behind = [row for row in rows if row["missing_kinds"]]
	return ToolResult(
		{"count": len(rows), "needs_update": len(behind), "devices": rows},
		f"{len(rows)} active device(s); {len(behind)} cannot render every field kind",
	)
