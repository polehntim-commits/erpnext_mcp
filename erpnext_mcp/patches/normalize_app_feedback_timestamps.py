# SPDX-License-Identifier: MIT
"""Put every App Feedback `submitted_at` in the site's zone, beside its `received_at`.

WHAT WAS WRONG. Until v0.198.0 `app_feedback._submitted_at` converted the
phone's `2026-09-27T19:53:43Z` with `datetimes.as_mariadb_datetime`, which lands
an instant in UTC, while `received_at` two fields along is `frappe.utils.now()`
— site-local. On a Pacific site every note therefore claimed it was written
seven hours AFTER it reached the farm, and `queued_days` was negative on every
row (AFB-2026-00022, the note that reported the photo bug, reads -0.29).

HOW A BAD ROW IS KNOWN, AND WHY THAT TEST IS SAFE. Nothing arrives before it is
sent, so a `timestamp` later than its own `received_at` is impossible. A margin
of ten minutes covers a handset whose clock runs fast; past that, the row was
written in UTC by the old code and is converted into the site's zone. A
converted row now reads earlier than its arrival, so a second run finds nothing
to do — the patch is idempotent by the same inequality that selects its rows.

WHAT IT CANNOT FIX, SAID PLAINLY. On a site EAST of UTC the old bug stored
stamps that were too early rather than too late, and "too early" is
indistinguishable from a note that queued for a while. Those rows are left
alone; this app's sites are Pacific, where every affected row is detectable.

Written with `update_modified=False`: `modified` on a note is when somebody
answered it, and a migration answering nothing should not claim to have.
"""

from __future__ import annotations

import datetime

import frappe

from erpnext_mcp import compat, datetimes, timezones

APP_FEEDBACK = "App Feedback"

#: How far a handset's clock may run ahead before its stamp is read as UTC.
SKEW_MARGIN = datetime.timedelta(minutes=10)


def execute() -> None:
	report = normalize_app_feedback_timestamps()
	print(
		f"erpnext_mcp: App Feedback stamps — {report['converted']} moved into {report['zone']}, "
		f"{report['scanned']} scanned" + (f" ({report['skipped']})" if report["skipped"] else "")
	)


def _as_datetime(value) -> datetime.datetime | None:
	if isinstance(value, datetime.datetime):
		return value.replace(tzinfo=None, microsecond=0)
	text = datetimes.as_mariadb_datetime(value)
	if not text:
		return None
	return datetime.datetime.strptime(text, datetimes.MARIADB_DATETIME_FORMAT)


def normalize_app_feedback_timestamps() -> dict:
	"""Convert every impossible (UTC-stored) `timestamp` into the site's zone. Idempotent."""
	zone = timezones.site_timezone()[0]
	report = {"scanned": 0, "converted": 0, "zone": zone, "skipped": ""}
	if not compat.doctype_exists(APP_FEEDBACK):
		report["skipped"] = f"this site has no {APP_FEEDBACK} DocType"
		return report
	if not (compat.has_field(APP_FEEDBACK, "timestamp") and compat.has_field(APP_FEEDBACK, "received_at")):
		report["skipped"] = f"{APP_FEEDBACK} has no timestamp/received_at pair yet — run migrate again"
		return report

	for row in frappe.db.get_all(
		APP_FEEDBACK, fields=["name", "timestamp", "received_at"], order_by="creation asc", limit=0
	):
		report["scanned"] += 1
		sent = _as_datetime(row.get("timestamp"))
		arrived = _as_datetime(row.get("received_at"))
		if not (sent and arrived) or sent <= arrived + SKEW_MARGIN:
			continue
		fixed = datetimes.as_site_datetime(sent.replace(tzinfo=datetime.timezone.utc), zone)
		if not fixed:
			continue
		frappe.db.set_value(APP_FEEDBACK, row["name"], "timestamp", fixed, update_modified=False)
		report["converted"] += 1
	return report
