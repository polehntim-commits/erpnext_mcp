# SPDX-License-Identifier: MIT
"""Security alerts from what Frappe already records. v0.217.0.

docs/design/security_status_and_alerts.md §3. Every 5 minutes this reads the
Activity Log and Version rows written since the last run, and for each event:
one email to Security Alert Recipients, one `security:alert` row in MCP Action
Log, and every method in the `erpnext_mcp_security_alert` hook. It writes
nothing else and never raises.

WHY POLL AND NOT HOOK. This app installs no document hooks (hooks.py) and no
login hooks; Frappe already writes an Activity Log row per login and a Version
row per change to a tracked doctype, so reading them is enough and touches
nothing.

NO SECRET IN AN ALERT. A Version row on ERPNext MCP Settings may carry the
auth token's change and one on User may carry `api_key`: alerts name the
FIELD, never the value — except `allow_*` switches, whose on/off is the point.
"""

from __future__ import annotations

import json

import frappe

from . import audit, compat, security_alerts

WATERMARK_KEY = "erpnext_mcp_security_watch_at"
FAILED_LIMIT = 5
HOOK = "erpnext_mcp_security_alert"
WATCHED_SETTINGS = ("ERPNext MCP Settings", "System Settings")

#: First run looks back this far, so a fresh install does not alert on history.
FIRST_LOOKBACK_MINUTES = 10


def _shift(**delta) -> str:
	"""Now, moved by `delta` (days/hours/minutes), as 'YYYY-MM-DD HH:MM:SS'."""
	return str(frappe.utils.add_to_date(frappe.utils.now(), **delta))[:19]


def _now() -> str:
	return str(frappe.utils.now())[:19]


def _watermark() -> str:
	try:
		value = frappe.defaults.get_global_default(WATERMARK_KEY)
	except Exception:  # pragma: no cover
		value = None
	if value:
		return str(value)
	return str(_shift(minutes=-FIRST_LOOKBACK_MINUTES))[:19]


def _set_watermark(value: str) -> None:
	try:
		frappe.defaults.set_global_default(WATERMARK_KEY, value)
	except Exception:  # pragma: no cover
		pass


def scan() -> list:
	"""The scheduled job. Returns the alerts it raised. Never raises."""
	try:
		since, until = _watermark(), _now()
		events = []
		events += _admin_logins(since, until)
		events += _failed_login_bursts(since, until)
		events += _settings_changes(since, until)
		events += _api_keys(since, until)
		for event in events:
			raise_alert(event)
		_set_watermark(until)
		frappe.db.commit()
		return events
	except Exception:  # pragma: no cover - a scheduled job never raises
		try:
			frappe.log_error(title="erpnext_mcp: security_watch.scan failed")
		except Exception:
			pass
		return []


def raise_alert(event: dict) -> None:
	subject = f"ERPNext security: {event['title']}"
	try:
		audit.record(
			"security:alert", {"kind": event["kind"]}, audit.STATUS_SUCCESS, event["text"][:500], commit=False
		)
	except Exception:  # pragma: no cover
		pass
	security_alerts.send(subject, event["text"])
	for listener in _listeners():
		try:
			listener(dict(event))
		except Exception:
			pass


def _listeners() -> list:
	try:
		return [frappe.get_attr(method) for method in frappe.get_hooks(HOOK) or []]
	except Exception:
		return []


def _rows(doctype: str, filters: dict, fields: list, since: str, until: str) -> list:
	if not compat.doctype_exists(doctype):
		return []
	try:
		return [
			dict(row)
			for row in frappe.db.get_all(
				doctype,
				filters={**filters, "creation": [">", since]},
				fields=fields,
				order_by="creation asc",
				limit=2000,
			)
			if str(row.get("creation") or "")[:19] <= until
		]
	except Exception:
		return []


def _admin_logins(since: str, until: str) -> list:
	out = []
	for row in _rows(
		"Activity Log",
		{"operation": "Login", "status": "Success", "user": "Administrator"},
		["name", "creation", "ip_address"],
		since,
		until,
	):
		out.append(
			{
				"kind": "administrator_login",
				"title": "Administrator logged in",
				"text": f"Administrator logged in at {row.get('creation')} from {row.get('ip_address') or 'an unknown address'}.",
			}
		)
	return out


def _failed_login_bursts(since: str, until: str) -> list:
	"""A user over FAILED_LIMIT failures in the hour ending now — once per user per hour."""
	hour_ago = str(_shift(hours=-1))[:19]
	rows = _rows(
		"Activity Log",
		{"operation": "Login", "status": "Failed"},
		["user", "creation", "ip_address"],
		hour_ago,
		until,
	)
	counts: dict = {}
	addresses: dict = {}
	for row in rows:
		user = str(row.get("user") or "")
		counts[user] = counts.get(user, 0) + 1
		addresses.setdefault(user, set()).add(str(row.get("ip_address") or "?"))
	out = []
	for user, count in sorted(counts.items()):
		if count <= FAILED_LIMIT or not user:
			continue
		key = f"erpnext_mcp_failed_login_alert:{user}:{until[:13]}"
		try:
			if frappe.defaults.get_global_default(key):
				continue
			frappe.defaults.set_global_default(key, until)
		except Exception:  # pragma: no cover
			pass
		out.append(
			{
				"kind": "failed_logins",
				"title": f"{count} failed logins for {user}",
				"text": f"{count} failed logins for {user} in the last hour, from {', '.join(sorted(addresses[user]))}.",
			}
		)
	return out


def _changed_fields(raw) -> list:
	try:
		data = json.loads(raw) if isinstance(raw, str) else (raw or {})
	except ValueError:
		return []
	return [row for row in (data.get("changed") or []) if isinstance(row, (list, tuple)) and len(row) >= 3]


def _settings_changes(since: str, until: str) -> list:
	out = []
	for doctype in WATCHED_SETTINGS:
		for row in _rows(
			"Version", {"ref_doctype": doctype}, ["name", "owner", "creation", "data"], since, until
		):
			changed = _changed_fields(row.get("data"))
			if not changed:
				continue
			parts = []
			for field, _old, new in (tuple(item[:3]) for item in changed):
				if str(field).startswith("allow_"):
					parts.append(f"{field}: {'on' if str(new) in ('1', 'True', 'true') else 'off'}")
				else:
					parts.append(str(field))
			out.append(
				{
					"kind": "settings_changed",
					"title": f"{doctype} changed",
					"text": f"{row.get('owner')} changed {doctype} at {row.get('creation')}: {', '.join(parts)}.",
				}
			)
	return out


def _api_keys(since: str, until: str) -> list:
	out = []
	for row in _rows(
		"Version", {"ref_doctype": "User"}, ["docname", "owner", "creation", "data"], since, until
	):
		for item in _changed_fields(row.get("data")):
			if item[0] == "api_key" and item[2]:
				out.append(
					{
						"kind": "api_key_generated",
						"title": f"API key generated for {row.get('docname')}",
						"text": f"{row.get('owner')} generated a Frappe API key for {row.get('docname')} at {row.get('creation')}.",
					}
				)
	return out
