# SPDX-License-Identifier: MIT
"""get_employee_file, export_employee_file_packet. v0.223.0.

docs/design/employee_file.md. The viewer is the calling person, else the MCP
System User — `employee.require_company_scope` and `employee_file.visible_sections`
apply to both.
"""

from __future__ import annotations

import frappe

from .. import employee_file, security
from ..args import as_int, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import employee as employee_tool


def _viewer() -> str:
	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def _resolve(args: dict) -> tuple:
	person = employee_tool.resolve_employee(as_str(args, "employee", required=True))
	viewer = _viewer()
	company = str(frappe.db.get_value("Employee", person, "company") or "")
	if company:
		employee_tool.require_company_scope(viewer, company)
	return person, viewer


def get_employee_file(args: dict) -> ToolResult:
	"""Read-only. Everything about one person, as this caller may see it."""
	person, viewer = _resolve(args)
	sections = [s.strip() for s in as_str(args, "sections").split(",") if s.strip()] or None
	try:
		data = employee_file.build(
			person, viewer, sections=sections, warning_days=as_int(args, "warning_days", None)
		)
	except employee_file.NotAllowed as exc:
		raise ToolError(f"{exc} Nothing was read.") from None
	except ValueError as exc:
		raise ToolError(str(exc)) from None
	s = data["summary"]
	return ToolResult(
		data=data,
		summary=f"{data.get('employee_name') or person}: {s['expired']} expired, {s['expiring']} expiring, {s['missing']} missing",
	)


def export_employee_file_packet(args: dict) -> ToolResult:
	"""One PDF of the file and its sealed documents, filed PRIVATE on the Employee."""
	person, viewer = _resolve(args)
	if not set(frappe.get_roles(viewer) or []) & set(("System Manager", "HR Manager")):
		raise ToolError(
			f"{viewer} may not export an audit packet: it takes HR Manager or System Manager. Nothing was changed."
		)
	try:
		answer = employee_file.packet(person, viewer)
	except (employee_file.NotAllowed, ValueError) as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	return ToolResult(
		data=answer, summary=f"audit packet for {person}: {answer['file_name']} ({answer['bytes']} bytes)"
	)
