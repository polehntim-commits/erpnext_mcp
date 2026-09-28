# SPDX-License-Identifier: MIT
"""Who may do a piece of work: a template's `required_certification`. v0.205.0.

docs/design/programs_and_field_kinds.md A1. Any Farm Task Template or
Inspection Template may name a certification — Applicator License for bait
placement, Forklift for a forklift task — and it is enforced the same way for
every one of them, on claim, assign, start and resume, and on an inspection a
worker is named on. A requirement nobody set enforces nothing.
"""

from __future__ import annotations

import re

import frappe

from . import compat
from .errors import ToolError


def qualification(employee: str, requirement: str) -> str:
	"""What satisfies `requirement` for this worker, as a sentence; '' when nothing does.

	A current Certification whose cert_type OR cert_name is the requirement
	(case-insensitive), held by the worker's Employee id or name; or the
	requirement listed on the Employee's skill field, where the site has one.
	"""
	requirement = str(requirement or "").strip()
	if not requirement or not employee:
		return ""
	wanted = requirement.casefold()
	if compat.doctype_exists("Employee"):
		from .tools import fieldwork

		field = compat.first_field("Employee", *fieldwork._SKILL_FIELDS)
		if field:
			listed = str(frappe.db.get_value("Employee", employee, field) or "")
			if wanted in {token.strip().casefold() for token in re.split(r"[,\n;]", listed) if token.strip()}:
				return f"Employee.{field} lists {requirement}"
	if not compat.doctype_exists("Certification"):
		return ""
	names = {employee.casefold()}
	if compat.doctype_exists("Employee"):
		full = str(frappe.db.get_value("Employee", employee, "employee_name") or "").strip()
		if full:
			names.add(full.casefold())
	today = frappe.utils.today()
	for row in frappe.db.get_all(
		"Certification",
		fields=["name", "cert_type", "cert_name", "holder", "status", "expiration_date"],
		limit=5000,
	):
		kinds = {str(row.get("cert_type") or "").casefold(), str(row.get("cert_name") or "").casefold()}
		if wanted not in kinds:
			continue
		if str(row.get("holder") or "").strip().casefold() not in names:
			continue
		if str(row.get("status") or "Active") != "Active":
			continue
		expires = str(row.get("expiration_date") or "")[:10]
		if expires and expires < today:
			continue
		return f"Certification {row['name']} ({requirement})"
	return ""


def certificate_of(employee: str, requirement: str) -> str:
	"""The Certification docname that satisfies it, or ''."""
	answer = qualification(employee, requirement)
	return answer.split(" ")[1] if answer.startswith("Certification ") else ""


def refuse_unqualified(record: dict, employee: str, verb: str, what: str = "") -> None:
	"""Refuse work whose `required_certification` this worker does not hold."""
	requirement = str(record.get("required_certification") or "").strip()
	if not requirement or not employee:
		return
	if qualification(employee, requirement):
		return
	label = what or str(record.get("task_name") or record.get("name") or "this work")
	raise ToolError(
		f"{label} requires {requirement!r}, which {employee} does not have on record: no current "
		f"Certification of that type or name held by them, and no {requirement!r} on their Employee "
		f"skills. Record it with create_certification, or send somebody who holds it. Nothing was {verb}."
	)
