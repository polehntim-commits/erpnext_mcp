# SPDX-License-Identifier: MIT
"""Put the two other phone stamps stored in UTC into the site's zone.

WHAT WAS WRONG. v0.198.0 fixed App Feedback (`normalize_app_feedback_timestamps`)
and left two more registers converting an iPhone's `…Z` with
`datetimes.as_mariadb_datetime`, which lands the instant in UTC beside columns
Frappe writes site-local:

- `Farm Task.observed_at`, two fields from `reported_at` (`frappe.utils.now()`).
  On a Pacific site a report read as seen seven hours after it was filed.
- `Bucket Log Entry.timestamp`, which every payroll read slices by day
  (`00:00:00`–`23:59:59`). A bucket picked after five in the afternoon counted
  toward tomorrow. Its session's `started_at`/`ended_at` are the earliest and
  latest entry, so each session a moved entry belongs to is re-derived too.

HOW A BAD ROW IS KNOWN, AND WHY THAT TEST IS SAFE. The same inequality as the
App Feedback patch: nothing is seen before it happens, and nothing is filed
before it is seen. An `observed_at` later than its own `reported_at`, or a
capture later than its own row's `creation`, is impossible past a ten-minute
margin for a handset clock running fast — so it was written in UTC by the old
code. A converted row reads earlier than its filing, and a second run finds
nothing to do.

WHAT IT CANNOT FIX, SAID PLAINLY. A capture that sat in the phone's queue for
longer than the site's offset (seven or eight hours on a Pacific site) before it
synced was stored too late by the old code and STILL reads earlier than its
`creation`, so it is indistinguishable from a correct row and is left alone.
Sites east of UTC are the App Feedback patch's caveat again: nothing there is
detectable.

Written with `update_modified=False`: a migration changes no one's record.
"""

from __future__ import annotations

import datetime

import frappe

from erpnext_mcp import bucket_bridge, compat, datetimes, timezones

FARM_TASK = "Farm Task"
BUCKET_ENTRY = "Bucket Log Entry"
BUCKET_SESSION = "Bucket Log Session"

#: How far a handset's clock may run ahead before its stamp is read as UTC.
SKEW_MARGIN = datetime.timedelta(minutes=10)


def execute() -> None:
	report = normalize_phone_timestamps()
	print(
		f"erpnext_mcp: phone stamps into {report['zone']} — "
		f"{report['observed_at']} Farm Task observed_at, "
		f"{report['bucket_entries']} Bucket Log Entry timestamps, "
		f"{report['sessions']} Bucket Log Sessions re-derived"
		+ (f" ({'; '.join(report['skipped'])})" if report["skipped"] else "")
	)


def _as_datetime(value) -> datetime.datetime | None:
	if isinstance(value, datetime.datetime):
		return value.replace(tzinfo=None, microsecond=0)
	text = datetimes.as_mariadb_datetime(value)
	if not text:
		return None
	return datetime.datetime.strptime(text, datetimes.MARIADB_DATETIME_FORMAT)


def _moved(stamp, filed, zone: str) -> str:
	"""`stamp` in the site's zone when it is later than `filed` could allow, else `""`."""
	sent = _as_datetime(stamp)
	arrived = _as_datetime(filed)
	if not (sent and arrived) or sent <= arrived + SKEW_MARGIN:
		return ""
	return datetimes.as_site_datetime(sent.replace(tzinfo=datetime.timezone.utc), zone)


def _observed_at(zone: str, report: dict) -> None:
	if not compat.doctype_exists(FARM_TASK) or not compat.has_field(FARM_TASK, "observed_at"):
		report["skipped"].append(f"{FARM_TASK} has no observed_at")
		return
	fields = ["name", "observed_at", "creation"]
	if compat.has_field(FARM_TASK, "reported_at"):
		fields.append("reported_at")
	for row in frappe.db.get_all(FARM_TASK, fields=fields, order_by="creation asc", limit=0):
		fixed = _moved(row.get("observed_at"), row.get("reported_at") or row.get("creation"), zone)
		if fixed:
			frappe.db.set_value(FARM_TASK, row["name"], "observed_at", fixed, update_modified=False)
			report["observed_at"] += 1


def _bucket_entries(zone: str, report: dict) -> None:
	if not compat.doctype_exists(BUCKET_ENTRY):
		report["skipped"].append(f"this site has no {BUCKET_ENTRY} DocType")
		return
	sessions = set()
	for row in frappe.db.get_all(
		BUCKET_ENTRY,
		fields=["name", "timestamp", "creation", "session_uuid"],
		order_by="creation asc",
		limit=0,
	):
		fixed = _moved(row.get("timestamp"), row.get("creation"), zone)
		if fixed:
			frappe.db.set_value(BUCKET_ENTRY, row["name"], "timestamp", fixed, update_modified=False)
			report["bucket_entries"] += 1
			if row.get("session_uuid"):
				sessions.add(row["session_uuid"])

	if not compat.doctype_exists(BUCKET_SESSION):
		return
	for session_uuid in sorted(sessions):
		name = frappe.db.get_value(BUCKET_SESSION, {"session_uuid": session_uuid}, "name")
		if not name:
			continue
		totals = bucket_bridge.aggregate_session(
			frappe.db.get_all(
				BUCKET_ENTRY,
				filters={"session_uuid": session_uuid},
				fields=["verdict", "timestamp"],
				limit=0,
			)
		)
		frappe.db.set_value(
			BUCKET_SESSION,
			name,
			{"started_at": totals["started_at"], "ended_at": totals["ended_at"]},
			update_modified=False,
		)
		report["sessions"] += 1


def normalize_phone_timestamps() -> dict:
	"""Convert every impossible (UTC-stored) phone stamp into the site's zone. Idempotent."""
	zone = timezones.site_timezone()[0]
	report = {"zone": zone, "observed_at": 0, "bucket_entries": 0, "sessions": 0, "skipped": []}
	_observed_at(zone, report)
	_bucket_entries(zone, report)
	return report
