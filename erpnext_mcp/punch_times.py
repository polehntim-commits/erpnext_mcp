# SPDX-License-Identifier: MIT
"""Punch times from a phone that may have had no signal. v0.227.0.

docs/design/offline_mill_creek.md §4 (Tim's decision, 2026-10-03):

* THE PHONE'S TAP IS THE OFFICIAL TIME. A crew clocked in at 06:02 in a block
  with no signal worked from 06:02, not from 14:40 when the foreman's phone
  found Wi-Fi.
* THE SERVER'S RECEIPT IS KEPT BESIDE IT. Both are stored and neither is ever
  overwritten by this module.
* A GAP BIGGER THAN THE OFFLINE WINDOW GOES TO A MANAGER. More than
  `offline_punch_window_hours` between tap and receipt, or a tap stamped after
  the receipt (a phone clock running ahead), sets `punch_review` with the reason.
* ONLY A PERSON CHANGES AN OFFICIAL TIME, through `resolve`, and the device and
  received times stay as they were.

Two child rows carry this: Farm Shift Crew Member (`joined_*`, `left_*`) and
Training Session Attendee (`device_scanned_at`, `received_at`).
"""

from __future__ import annotations

import datetime

import frappe

from . import compat, datetimes, settings, timezones
from .errors import ToolError

CREW = "Farm Shift Crew Member"
ATTENDEE = "Training Session Attendee"
SHIFT = "Farm Shift"
SESSION = "Training Session"

DEFAULT_WINDOW_HOURS = 12
#: A tap this far after the receipt is a phone clock running ahead.
AHEAD_TOLERANCE = datetime.timedelta(minutes=5)

PHONE_STANDS = "Phone time stands"
SERVER_USED = "Server time used"
CORRECTED = "Corrected"
RESOLUTIONS = (PHONE_STANDS, SERVER_USED, CORRECTED)
REVIEW_ROLES = ("Farm Manager", "HR Manager", "HR User", "System Manager")

#: Which official field each kind of punch has, and where its two witnesses live.
KINDS = {
	"in": {
		"doctype": CREW,
		"official": "joined_at",
		"device": "joined_device_at",
		"received": "joined_received_at",
	},
	"out": {
		"doctype": CREW,
		"official": "left_at",
		"device": "left_device_at",
		"received": "left_received_at",
	},
	"class": {
		"doctype": ATTENDEE,
		"official": "scanned_at",
		"device": "device_scanned_at",
		"received": "received_at",
	},
}


def window_hours() -> float:
	try:
		value = float(settings._value("offline_punch_window_hours") or DEFAULT_WINDOW_HOURS)
	except (TypeError, ValueError):
		value = DEFAULT_WINDOW_HOURS
	return value if value > 0 else DEFAULT_WINDOW_HOURS


def device_time(raw) -> str:
	"""The phone's tap in the site's zone (`YYYY-MM-DD HH:MM:SS`), or "" when absent or unreadable."""
	if raw in (None, ""):
		return ""
	return datetimes.as_site_datetime(raw, timezones.site_timezone()[0])


def _parse(value) -> datetime.datetime | None:
	try:
		return datetime.datetime.fromisoformat(str(value)[:19])
	except (TypeError, ValueError):
		return None


def judge(device: str, received: str) -> str:
	"""Why this punch needs a manager, or ""."""
	tap, got = _parse(device), _parse(received)
	if tap is None or got is None:
		return ""
	window = datetime.timedelta(hours=window_hours())
	if tap - got > AHEAD_TOLERANCE:
		return f"The phone's time ({device}) is after the server received it ({received}) — the phone clock may be wrong."
	if got - tap > window:
		hours = round((got - tap).total_seconds() / 3600, 1)
		return (
			f"Sent {hours} hours after the tap ({device} on the phone, received {received}); the offline window "
			f"is {window_hours():g} hours."
		)
	return ""


def stamp(row, kind: str, device: str, received: str = "") -> str:
	"""Record both witnesses on a child row (before save) and flag it if needed. Returns the reason or ""."""
	spec = KINDS[kind]
	doctype = spec["doctype"]
	if not compat.has_field(doctype, spec["received"]):
		return ""
	received = received or str(frappe.utils.now())[:19]
	row.set(spec["received"], received)
	if not device:
		return ""
	row.set(spec["device"], device)
	reason = judge(device, received)
	if reason and compat.has_field(doctype, "punch_review"):
		row.set("punch_review", 1)
		earlier = str(row.get("punch_review_reason") or "")
		row.set("punch_review_reason", (f"{earlier}\n{reason}" if earlier else reason)[:1000])
		row.set("punch_resolution", None)
	return reason


# ── the manager's side ──────────────────────────────────────────────────────
def require_reviewer(user: str) -> None:
	if not set(frappe.get_roles(user) or []) & set(REVIEW_ROLES):
		raise ToolError(
			"reviewing punch times is for a Farm Manager, HR or a System Manager. Nothing was changed.",
			"error.punch.forbidden",
		)


def pending(companies=None, limit: int = 200) -> list:
	"""Every punch waiting for a manager, oldest tap first."""
	out = []
	for kind_doctype, parent_doctype, parent_label in ((CREW, SHIFT, "shift"), (ATTENDEE, SESSION, "class")):
		if not compat.has_field(kind_doctype, "punch_review"):
			continue
		rows = frappe.db.get_all(
			kind_doctype,
			filters={"punch_review": 1, "parenttype": parent_doctype},
			fields=[
				"parent",
				*compat.existing_fields(
					kind_doctype,
					(
						"name",
						"employee",
						"employee_name",
						"joined_at",
						"left_at",
						"joined_device_at",
						"joined_received_at",
						"left_device_at",
						"left_received_at",
						"scanned_at",
						"device_scanned_at",
						"received_at",
						"punch_review_reason",
						"punch_resolution",
					),
				),
			],
			limit=limit,
		)
		for row in rows or []:
			if row.get("punch_resolution"):
				continue
			company = frappe.db.get_value(parent_doctype, row["parent"], "company")
			if companies and company not in companies:
				continue
			out.append(
				{
					"row": row["name"],
					"kind": "class" if kind_doctype == ATTENDEE else "shift",
					parent_label: row["parent"],
					"employee": row.get("employee"),
					"employee_name": row.get("employee_name"),
					"reason": row.get("punch_review_reason"),
					"official": {
						key: str(row.get(key) or "") or None
						for key in ("joined_at", "left_at", "scanned_at")
						if key in row
					},
					"device": {
						key: str(row.get(key) or "") or None
						for key in ("joined_device_at", "left_device_at", "device_scanned_at")
						if key in row
					},
					"received": {
						key: str(row.get(key) or "") or None
						for key in ("joined_received_at", "left_received_at", "received_at")
						if key in row
					},
					"company": company,
				}
			)
	return sorted(out, key=lambda r: min([v for v in r["device"].values() if v] or ["~"]))


def resolve(
	user: str, row: str, resolution: str, kind: str = "", corrected_at=None, note: str = "", companies=None
) -> dict:
	"""A person decides. Only Server time used / Corrected change the official time."""
	require_reviewer(user)
	if resolution not in RESOLUTIONS:
		raise ToolError(f"resolution is one of {', '.join(RESOLUTIONS)}. Nothing was changed.")
	doctype = CREW if frappe.db.exists(CREW, row) else (ATTENDEE if frappe.db.exists(ATTENDEE, row) else "")
	if not doctype:
		raise ToolError(f"no punch called {row!r}. Nothing was changed.", "error.punch.not_found")
	parent_doctype = SHIFT if doctype == CREW else SESSION
	if doctype == CREW:
		# v0.251.0. A punch a supervisor reviewed is locked for payroll.
		from . import time_review

		time_review.refuse_if_locked(row, "resolved again")
	values = (
		frappe.db.get_value(doctype, row, ["parent", "punch_review", "punch_resolution"], as_dict=True) or {}
	)
	company = frappe.db.get_value(parent_doctype, values.get("parent"), "company")
	if companies and company not in companies:
		raise ToolError(f"no punch called {row!r}. Nothing was changed.", "error.punch.not_found")
	if not compat.checked(values.get("punch_review")):
		raise ToolError(f"{row} is not waiting for review. Nothing was changed.")
	if doctype == ATTENDEE:
		kinds = ["class"]
	else:
		kinds = [kind] if kind in ("in", "out") else ["in", "out"]
	changes = {
		"punch_resolution": resolution,
		"punch_reviewed_by": user,
		"punch_review_note": (note or "")[:500],
	}
	if resolution == CORRECTED:
		fixed = device_time(corrected_at)
		if not fixed:
			raise ToolError("Corrected needs corrected_at (the right time). Nothing was changed.")
		if len(kinds) != 1:
			raise ToolError("say which punch is corrected: kind 'in' or 'out'. Nothing was changed.")
		changes[KINDS[kinds[0]]["official"]] = fixed
	elif resolution == SERVER_USED:
		for name in kinds:
			received = frappe.db.get_value(doctype, row, KINDS[name]["received"])
			device = frappe.db.get_value(doctype, row, KINDS[name]["device"])
			if received and device:
				changes[KINDS[name]["official"]] = str(received)
	changes = {
		k: v
		for k, v in changes.items()
		if compat.has_field(doctype, k) or k in ("joined_at", "left_at", "scanned_at")
	}
	frappe.db.set_value(doctype, row, changes, update_modified=True)
	from . import audit

	audit.record(
		"resolve_punch_review",
		{"row": row, "resolution": resolution},
		audit.STATUS_SUCCESS,
		f"{user} resolved punch {row}: {resolution}" + (f" — {note}" if note else ""),
		commit=False,
	)
	return {
		"row": row,
		"resolution": resolution,
		"changed": {k: v for k, v in changes.items() if k in ("joined_at", "left_at", "scanned_at")},
	}
