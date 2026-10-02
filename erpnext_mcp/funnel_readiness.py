# SPDX-License-Identifier: MIT
"""Can the public /erpnext Funnel mount be removed yet? v0.216.0.

docs/design/farmops_only_funnel.md §4. The Farm Ops app has only ever called
`<base>/farmops/api/...`; what tied it to /erpnext was the base on its login
card. From app 0.27.0 a phone reports which base it is using
(`report_device_capabilities.api_base_mode`), and this reads those reports.

IT INFORMS AND NEVER REFUSES. Nothing here stops a phone working: closing the
Funnel is something an operator does on the host, and this says when doing it
will strand nobody.
"""

from __future__ import annotations

import frappe

from . import compat, settings

GRANT = "Mobile Access Grant"
DEVICE = "Mobile Device Enrollment"

#: A device silent for this long does not hold up the cutover. The idle sweep
#: revokes at the same age by default.
IDLE_DAYS = 30

#: The first app build that reports its base.
FIRST_REPORTING_APP = "0.27.0"


def status() -> dict:
	"""The `erpnext_funnel` block of `get_server_status`. Never raises."""
	farmops = settings.farmops_public_url()
	out = {
		"farmops_public_url": farmops or None,
		"public_url": settings.public_url().rstrip("/") or None,
		"allow_legacy_erpnext_paths": settings.allow_legacy_erpnext_paths(),
		"ready_to_close_erpnext_funnel": False,
		"reasons": [],
		"devices": [],
		"ignored_idle_devices": 0,
	}
	if not farmops:
		out["reasons"].append(
			"Farm Ops Public URL is empty on ERPNext MCP Settings, so new login cards and tags are "
			"still issued on public_url."
		)
	try:
		devices, idle = _devices()
	except Exception as exc:  # pragma: no cover - a status read never fails the tool
		out["reasons"].append(f"the device register could not be read: {exc}")
		return out
	out["devices"] = devices
	out["ignored_idle_devices"] = idle
	for device in devices:
		if not device["ready"]:
			out["reasons"].append(_why(device))
	out["ready_to_close_erpnext_funnel"] = not out["reasons"]
	return out


def _devices() -> tuple[list, int]:
	if not compat.doctype_exists(GRANT) or not compat.doctype_exists(DEVICE):
		return [], 0
	# `parent` is a column of every child table and a field of none, so it is
	# named here rather than asked of the meta.
	wanted = ["name", "parent"] + [
		field
		for field in compat.existing_fields(
			DEVICE, ("device_name", "app_version", "api_base_mode", "last_seen_on", "enrolled_at")
		)
		if field != "name"
	]
	rows = frappe.db.get_all(
		DEVICE, filters={"enrollment_status": "Enrolled", "parenttype": GRANT}, fields=wanted, limit=5000
	)
	grants = {
		row["name"]: row
		for row in frappe.db.get_all(
			GRANT, filters={"state": "Active"}, fields=["name", "user", "full_name"], limit=5000
		)
	}
	today = frappe.utils.today()
	out, idle = [], 0
	for row in rows:
		grant = grants.get(row.get("parent"))
		if not grant:
			continue
		seen = str(row.get("last_seen_on") or "") or str(row.get("enrolled_at") or "")
		if seen and frappe.utils.date_diff(today, seen[:10]) > IDLE_DAYS:
			idle += 1
			continue
		mode = str(row.get("api_base_mode") or "") or None
		out.append(
			{
				"user": grant.get("user"),
				"full_name": grant.get("full_name") or None,
				"device": row.get("device_name") or row.get("name"),
				"app_version": str(row.get("app_version") or "") or None,
				"api_base_mode": mode,
				"last_seen_on": seen or None,
				"ready": mode == "farmops",
			}
		)
	out.sort(key=lambda entry: (entry["ready"], str(entry["user"] or ""), str(entry["device"] or "")))
	return out, idle


def _why(device: dict) -> str:
	who = f"{device['full_name'] or device['user']}'s {device['device']}"
	version = device["app_version"] or "an unknown version"
	if device["api_base_mode"] == "legacy":
		return (
			f"{who} (app {version}) is still calling through /erpnext — /farmops/api did not answer "
			"at the host root when it last checked."
		)
	return (
		f"{who} (app {version}) has not reported using /farmops/api — install Farm Ops "
		f"{FIRST_REPORTING_APP} or later and open it once."
	)
