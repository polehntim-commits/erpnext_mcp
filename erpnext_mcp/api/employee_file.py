# SPDX-License-Identifier: MIT
"""Desk: the Employee file and its audit packet. v0.223.0.

docs/design/employee_file.md. The person logged into the Desk is the viewer;
`employee_file.visible_sections` decides what they see, as it does on MCP and
the phone. The packet is HR Manager / System Manager only.
"""

from __future__ import annotations

import frappe

from .. import audit, employee_file

PACKET_ROLES = ("System Manager", "HR Manager")


def _viewer() -> str:
	return str(frappe.session.user or "")


@frappe.whitelist(methods=["POST", "GET"])
def get(employee=None) -> dict:
	try:
		data = employee_file.build(str(employee or ""), _viewer())
	except employee_file.NotAllowed as exc:
		frappe.throw(str(exc), frappe.PermissionError)
	except ValueError as exc:
		frappe.throw(str(exc), frappe.DoesNotExistError)
	return {"file": data, "html": employee_file.packet_html(data)}


@frappe.whitelist(methods=["POST"])
def packet(employee=None) -> dict:
	frappe.only_for(PACKET_ROLES)
	try:
		answer = employee_file.packet(str(employee or ""), _viewer())
	except (employee_file.NotAllowed, ValueError) as exc:
		frappe.throw(str(exc), frappe.ValidationError)
	audit.record(
		"employee_file:packet",
		{"employee": employee, "via": "desk"},
		audit.STATUS_SUCCESS,
		f"{_viewer()} exported the audit packet for {employee} ({answer['bytes']} bytes)",
	)
	return answer
