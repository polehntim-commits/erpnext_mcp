# SPDX-License-Identifier: MIT
"""Turn a tool switch on for N minutes; turn it off again when the time is up. v0.217.0.

docs/design/security_status_and_alerts.md §5. A Desk action, System Manager
only (`api/switches.enable_for`). No MCP tool can turn a switch on: that stays
a person's act. A switch turned on by hand in the form is permanent and this
never touches it — only switches it turned on itself, recorded in the hidden
`switch_expiry` field.
"""

from __future__ import annotations

import json

import frappe

from . import audit, security_alerts, settings
from .errors import ToolError

MIN_MINUTES, MAX_MINUTES = 1, 240


def _shift(**delta) -> str:
	"""Now, moved by `delta` (days/hours/minutes), as 'YYYY-MM-DD HH:MM:SS'."""
	return str(frappe.utils.add_to_date(frappe.utils.now(), **delta))[:19]


def _load(doc) -> dict:
	raw = doc.get("switch_expiry")
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except ValueError:
		value = {}
	return value if isinstance(value, dict) else {}


def enable_for(tool: str, minutes, by: str) -> dict:
	from . import registry

	tool = str(tool or "").strip()
	if tool not in registry.TOOLS:
		raise ToolError(f"{tool!r} is not a tool. Nothing was changed.")
	try:
		minutes = int(minutes)
	except (TypeError, ValueError):
		raise ToolError("minutes must be a whole number.") from None
	if not MIN_MINUTES <= minutes <= MAX_MINUTES:
		raise ToolError(f"minutes must be {MIN_MINUTES}–{MAX_MINUTES}. Nothing was changed.")
	doc = frappe.get_single(settings.SETTINGS_DOCTYPE)
	field = f"allow_{tool}"
	timed = _load(doc)
	if settings.as_bool(doc.get(field)) and tool not in timed:
		raise ToolError(f"{tool} is already on, permanently. Nothing was changed.")
	until = str(_shift(minutes=minutes))[:19]
	timed[tool] = until
	doc.set(field, 1)
	doc.switch_expiry = json.dumps(timed, sort_keys=True)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	text = f"{by} turned on {tool} for {minutes} minute(s), until {until}."
	audit.record(
		"switch:enable_for", {"tool": tool, "minutes": minutes}, audit.STATUS_SUCCESS, text, commit=False
	)
	security_alerts.send("ERPNext MCP: a tool was switched on for a limited time", text)
	return {"tool": tool, "enabled": True, "expires_at": until}


def revert_expired() -> list:
	"""The scheduled job. Turns off every time-boxed switch whose time is up. Never raises."""
	try:
		doc = frappe.get_single(settings.SETTINGS_DOCTYPE)
		timed = _load(doc)
		now = str(frappe.utils.now())[:19]
		done = [tool for tool, until in timed.items() if str(until) <= now]
		if not done:
			return []
		for tool in done:
			doc.set(f"allow_{tool}", 0)
			timed.pop(tool, None)
		doc.switch_expiry = json.dumps(timed, sort_keys=True)
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		text = f"Time is up: switched off {', '.join(sorted(done))}."
		audit.record("switch:expired", {"tools": sorted(done)}, audit.STATUS_SUCCESS, text, commit=False)
		security_alerts.send("ERPNext MCP: time-limited tool switches turned off", text)
		frappe.db.commit()
		return sorted(done)
	except Exception:  # pragma: no cover - a scheduled job never raises
		frappe.log_error(title="erpnext_mcp: switch_timer.revert_expired failed")
		return []
