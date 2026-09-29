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


def requirements_of(record: dict) -> list:
	"""The certifications a piece of work demands: its own `required_certification`,
	plus (v0.207.0) those of the products it handles, from their Active label
	profiles — docs/design/phone_config_and_compliance_loop.md §5.2."""
	out = []
	own = str(record.get("required_certification") or "").strip()
	if own:
		out.append(own)
	try:
		from . import label_compliance

		product = dict(record)
		if (
			record.get("name")
			and "bait_product" not in record
			and frappe.db.exists("Farm Task", record["name"])
		):
			product.update(
				frappe.db.get_value(
					"Farm Task", record["name"], ["bait_product", "materials_used"], as_dict=True
				)
				or {}
			)
		out += [c for c in label_compliance.certifications_for(product) if c not in out]
	except Exception:
		pass
	return out


def refuse_unqualified(record: dict, employee: str, verb: str, what: str = "") -> None:
	"""Refuse work whose required certifications this worker does not hold."""
	if not employee:
		return
	missing = [req for req in requirements_of(record) if not qualification(employee, req)]
	if not missing:
		return
	requirement = missing[0]
	label = what or str(record.get("task_name") or record.get("name") or "this work")
	raise ToolError(
		f"{label} requires {requirement!r}, which {employee} does not have on record: no current "
		f"Certification of that type or name held by them, and no {requirement!r} on their Employee "
		f"skills. Record it with create_certification, or send somebody who holds it. Nothing was {verb}."
	)
