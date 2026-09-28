# SPDX-License-Identifier: MIT
"""Which phones can render which field kinds. v0.205.0.

docs/design/programs_and_field_kinds.md B4. Each app build reports its schema
version and the kinds it renders natively (`report_device_capabilities`),
stored on the caller's Mobile Device Enrollment row. A device that never
reported is schema 1 — exactly the v0.204.0 kinds. Template tools validate
against the devices actually enrolled for the template's company and say which
devices and app versions need updating; a SAFETY-CRITICAL field on a device that
cannot render it is marked blocking.
"""

from __future__ import annotations

import json

import frappe

from . import compat, form_schema

GRANT = "Mobile Access Grant"
DEVICE = "Mobile Device Enrollment"


def report(user: str, device_identifier: str, app_version: str, schema_version, field_kinds) -> dict:
	"""Store what one of the caller's devices renders. Returns the stored row."""
	if not compat.doctype_exists(GRANT):
		return {"stored": False, "reason": "no Mobile Access Grant doctype"}
	grant = frappe.db.get_value(GRANT, {"user": user}, "name")
	if not grant:
		return {"stored": False, "reason": "no grant for this user"}
	kinds = [form_schema.canonical(kind) for kind in (field_kinds or []) if str(kind or "").strip()]
	doc = frappe.get_doc(GRANT, grant)
	target = None
	wanted = str(device_identifier or "").strip()
	for row in doc.get("devices") or []:
		if wanted and str(row.get("device_identifier") or "") == wanted:
			target = row
			break
	if target is None:
		live = [
			row for row in doc.get("devices") or [] if str(row.get("enrollment_status") or "") != "Revoked"
		]
		target = live[-1] if len(live) == 1 else None
	if target is None:
		return {"stored": False, "reason": f"no enrolled device {wanted!r} on this account"}
	values = {
		"app_version": str(app_version or "")[:60],
		"form_schema_version": int(schema_version or 1),
		"field_kinds": json.dumps(sorted(set(kinds))),
		"capabilities_reported_at": frappe.utils.now(),
	}
	for key, value in values.items():
		if compat.has_field(DEVICE, key):
			target.set(key, value)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"stored": True, "device": target.name, **values, "field_kinds": sorted(set(kinds))}


def devices(company: str = "") -> list:
	"""Every active device (optionally for one company's accounts) with what it cannot render."""
	if not compat.doctype_exists(GRANT):
		return []
	out = []
	for grant in frappe.db.get_all(
		GRANT, filters={"state": "Active"}, fields=["name", "user", "full_name"], limit=2000
	):
		doc = frappe.get_doc(GRANT, grant["name"])
		if company:
			raw = str(doc.get("entity_access") or "")
			entities = {line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()}
			if entities and company not in entities and str(doc.get("preferred_company") or "") != company:
				continue
		for row in doc.get("devices") or []:
			if str(row.get("enrollment_status") or "") != "Enrolled":
				continue
			capabilities = {
				"schema_version": row.get("form_schema_version") or 1,
				"field_kinds": _list(row.get("field_kinds")),
			}
			kinds = form_schema.kinds_of(capabilities)
			out.append(
				{
					"user": grant.get("user"),
					"full_name": grant.get("full_name"),
					"device": row.get("device_name") or row.get("device_identifier") or row.name,
					"app_version": row.get("app_version") or None,
					"schema_version": int(row.get("form_schema_version") or 1),
					"reported_at": str(row.get("capabilities_reported_at") or "") or None,
					"kinds": sorted(kinds),
					"missing_kinds": sorted(set(form_schema.TYPES) - kinds),
				}
			)
	return out


def _list(raw) -> list:
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
	except ValueError:
		return []
	return value if isinstance(value, list) else []


def problems(fields: list, company: str = "") -> list:
	"""Sentences: which enrolled devices cannot render which kinds of this form."""
	used = form_schema.kinds_used(fields)
	critical = form_schema.kinds_used(fields, safety_critical_only=True)
	out = []
	for device in devices(company):
		missing = sorted(used - set(device["kinds"]))
		if not missing:
			continue
		blocking = sorted(critical & set(missing))
		who = f"{device['full_name'] or device['user']}'s {device['device']} (app {device['app_version'] or 'unknown'}, schema {device['schema_version']})"
		if blocking:
			out.append(
				f"BLOCKING on {who}: safety-critical {', '.join(blocking)} cannot be rendered — update the app before "
				"this work is sent to that device."
			)
		else:
			out.append(f"{who} shows {', '.join(missing)} as a fallback — update the app for the full form.")
	return out


def refuse_incapable(fields: list, answers: dict, context: dict, capabilities) -> None:
	"""Refuse a completion when a visible safety-critical field's kind is beyond the client."""
	from .errors import ToolError

	kinds = form_schema.kinds_of(capabilities)
	blocked = []
	for field in fields or []:
		if not field.get("safety_critical"):
			continue
		if field.get("show_if") and not form_schema.holds(field["show_if"], answers or {}, context or {}):
			continue
		if form_schema.canonical(field.get("type")) not in kinds:
			blocked.append(form_schema.text_of(field.get("label")) or field.get("key"))
	if blocked:
		raise ToolError(
			f"this app cannot show the safety-critical field(s) {', '.join(blocked)} — update the app, then "
			"complete it. Nothing was changed."
		)
