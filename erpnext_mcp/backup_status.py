# SPDX-License-Identifier: MIT
"""Is this site's data safe right now? v0.215.0.

docs/design/backup_status.md.

The erp-backup kit on each box writes a status file after every stage — backup,
push, the peer's nightly restore, the archive test — and copies it to where
Frappe can read it: `<sites>/erp_backup_status/<box>.json`. The scripts hold no
API token and never call this app; that was a deliberate choice. So the two
things this module does both happen INSIDE the app:

  * `status` — what `get_backup_status` answers. Read-only. It re-derives every
    alert from the timestamps rather than trusting the file's own, because the
    failure that matters most is the one where the reporter itself has stopped:
    a file that says "ok" and is three days old is not ok.
  * `ingest` — turns the file into Backup Records and their test restores, the
    same rows `create_backup_record` and `record_backup_test` write by hand,
    keyed so reading the same file twice writes nothing twice.

WHAT IT WILL READ. Files named `<box>.json` directly inside that one directory,
under 256 KB, declaring the schema. Never another path, never a symlink out of
the directory, never a shell. Nothing in an answer names a path beyond the
file's own name.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re

import frappe

from . import compat

SCHEMA = "erp-backup-status/1"
DIRECTORY = "erp_backup_status"
BACKUP = "Backup Record"
MAX_BYTES = 256 * 1024
_NAME = re.compile(r"^[a-z0-9_-]{1,40}\.json$")

STALE_HOURS = 30
REPORTER_DEAD_HOURS = 26
KEEP_DAILY = 14
RPO_HOURS = 24

FLAG_BOX = "backup_box"
FLAG_COMPANY = "backup_record_company"
FLAG_OFFSITE = "backup_peer_is_offsite"

CRITICAL = "critical"
WARNING = "warning"
_RANK = {"ok": 0, WARNING: 1, CRITICAL: 2}
CRITICAL_CODES = frozenset(
	{
		"NO_STATUS_FILE",
		"REPORTER_DEAD",
		"BACKUP_FAILED",
		"BACKUP_STALE",
		"REMOTE_RESTORE_NOT_PASS",
		"PROMOTED",
	}
)


# ── reading ─────────────────────────────────────────────────────────────────
def status_dir() -> str:
	"""`<sites>/erp_backup_status`. The one directory this module reads."""
	sites = getattr(frappe.local, "sites_path", None)
	if not sites:
		sites = os.path.dirname(os.path.abspath(frappe.get_site_path()))
	return os.path.join(str(sites), DIRECTORY)


def read_files() -> tuple:
	"""({box: status}, [skipped]) for every acceptable file. Never raises."""
	found: dict = {}
	skipped: list = []
	try:
		root = os.path.realpath(status_dir())
		names = sorted(os.listdir(root)) if os.path.isdir(root) else []
	except Exception:
		return {}, []
	for name in names:
		if not _NAME.match(name):
			continue
		try:
			path = os.path.realpath(os.path.join(root, name))
			if os.path.dirname(path) != root or not os.path.isfile(path):
				skipped.append({"file": name, "reason": "not a plain file in the status directory"})
				continue
			if os.path.getsize(path) > MAX_BYTES:
				skipped.append({"file": name, "reason": "larger than a status file should be"})
				continue
			with open(path, encoding="utf-8") as handle:
				body = json.load(handle)
			if not isinstance(body, dict) or body.get("schema") != SCHEMA:
				skipped.append({"file": name, "reason": f"not a {SCHEMA} file"})
				continue
			box = str(body.get("box") or name[:-5])
			found[box] = body
		except Exception as exc:
			skipped.append({"file": name, "reason": f"unreadable ({type(exc).__name__})"})
	return found, skipped


# ── time ────────────────────────────────────────────────────────────────────
def parse(stamp):
	"""An aware datetime from an ISO-8601 stamp with offset, or None."""
	text = str(stamp or "").strip()
	if not text:
		return None
	try:
		when = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		return None
	if when.tzinfo is None:
		when = when.replace(tzinfo=datetime.timezone.utc)
	return when


def now_utc() -> datetime.datetime:
	return datetime.datetime.now(datetime.timezone.utc)


def hours_since(stamp, now=None):
	when = parse(stamp)
	if when is None:
		return None
	return round(((now or now_utc()) - when).total_seconds() / 3600.0, 1)


def site_naive(stamp) -> str | None:
	"""The stamp as the site's own naive datetime string, for a Datetime column."""
	when = parse(stamp)
	if when is None:
		return None
	try:
		from zoneinfo import ZoneInfo

		zone = ZoneInfo(str(frappe.utils.get_system_timezone()))
		when = when.astimezone(zone)
	except Exception:
		when = when.astimezone()
	return when.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _yes(value) -> bool:
	return str(value).strip().lower() in ("yes", "true", "1", "ok", "pass")


def _number(value):
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _block(body: dict, key: str) -> dict:
	value = body.get(key)
	return value if isinstance(value, dict) else {}


# ── alerts ──────────────────────────────────────────────────────────────────
def _alert(code: str, message: str, box: str = "") -> dict:
	return {
		"level": CRITICAL if code in CRITICAL_CODES else WARNING,
		"code": code,
		"box": box or None,
		"message": message,
	}


def box_alerts(body: dict, stale_hours: float, now=None) -> list:
	"""Every alert for one box, RECOMPUTED from its timestamps."""
	now = now or now_utc()
	box = str(body.get("box") or "")
	roles = _block(body, "roles")
	out: list = []

	age = hours_since(body.get("generated_at"), now)
	if age is None or age > REPORTER_DEAD_HOURS:
		out.append(
			_alert(
				"REPORTER_DEAD",
				f"{box}'s status file is {age if age is not None else 'undated'} h old. The backup scripts "
				"run at least daily, so the timers are probably gone — often after an umbrelOS update. "
				"Everything below is the last thing it said, not what is true now.",
				box,
			)
		)

	own = _block(body, "own_backup")
	if roles.get("send", True):
		if str(own.get("result") or "").upper() == "FAIL":
			out.append(
				_alert(
					"BACKUP_FAILED",
					f"{box}'s last backup failed: {own.get('msg') or 'no reason given'}.",
					box,
				)
			)
		last_ok = own.get("last_ok_at") or (own.get("at") if str(own.get("result")).upper() == "OK" else None)
		ok_age = hours_since(last_ok, now)
		if ok_age is None or ok_age > stale_hours:
			out.append(
				_alert(
					"BACKUP_STALE",
					f"{box}'s last good backup is "
					+ (f"{ok_age} h old" if ok_age is not None else "not on record")
					+ f" (limit {stale_hours:g} h).",
					box,
				)
			)
		if own and str(own.get("result")).upper() == "OK" and not _yes(own.get("encrypted")):
			out.append(_alert("BACKUP_UNENCRYPTED", f"{box}'s last backup was not encrypted.", box))

		push = _block(body, "push")
		push_age = hours_since(push.get("at"), now)
		if (
			str(push.get("result") or "").upper() != "OK"
			or push_age is None
			or push_age > stale_hours
			or (own.get("set") and push.get("set") and push.get("set") != own.get("set"))
		):
			out.append(
				_alert(
					"PUSH_STALE_OR_FAILED",
					f"{box}'s latest backup has not reached {push.get('peer') or body.get('peer') or 'the peer'}"
					+ (f": {push.get('msg')}" if push.get("msg") else "")
					+ ".",
					box,
				)
			)

		restore = _block(body, "remote_restore_of_my_data")
		if not restore:
			out.append(
				_alert(
					"NO_REMOTE_RESTORE",
					f"No peer has reported restoring {box}'s data. A backup nobody has restored is a belief.",
					box,
				)
			)
		else:
			restore_age = hours_since(restore.get("at"), now)
			if restore_age is None or restore_age > stale_hours:
				out.append(
					_alert(
						"REMOTE_RESTORE_STALE",
						f"The last restore of {box}'s data on {restore.get('restored_by') or 'the peer'} is "
						+ (f"{restore_age} h old." if restore_age is not None else "undated."),
						box,
					)
				)
			if str(restore.get("result") or "") != "Pass":
				out.append(
					_alert(
						"REMOTE_RESTORE_NOT_PASS",
						f"The last restore of {box}'s data on {restore.get('restored_by') or 'the peer'} was "
						f"{restore.get('result') or 'not reported'}, not Pass.",
						box,
					)
				)

	if roles.get("receive"):
		copies = _block(body, "peer_copies_check")
		if copies and str(copies.get("result") or "").upper() != "OK":
			out.append(
				_alert(
					"PEER_COPIES_BAD", f"{box}'s check of the copies it holds for its peer did not pass.", box
				)
			)

	standby = _block(body, "standby_of_peer")
	if roles.get("standby") or standby:
		result = str(standby.get("result") or "")
		if result.upper() == "FAIL":
			out.append(
				_alert(
					"STANDBY_FAILED",
					f"{box}'s standby restore of its peer failed: {standby.get('msg') or ''}".strip(),
					box,
				)
			)
		elif standby:
			standby_age = hours_since(standby.get("at"), now)
			if standby_age is None or standby_age > stale_hours:
				out.append(
					_alert("STANDBY_STALE", f"{box}'s standby copy of its peer is {standby_age} h old.", box)
				)
			if result != "Pass":
				out.append(
					_alert(
						"STANDBY_CHECK_NOT_PASS",
						f"{box}'s standby restore of its peer was {result or 'not reported'}.",
						box,
					)
				)

	if body.get("promoted"):
		out.append(
			_alert(
				"PROMOTED",
				f"{box} has been PROMOTED: its standby is serving as the live site. Somebody decided that, or something is wrong.",
				box,
			)
		)
	if body.get("fenced"):
		out.append(_alert("FENCED", f"{box} is fenced: it has been told to stop taking writes.", box))

	# The kit's own alerts, where the tool did not recompute the same code.
	have = {entry["code"] for entry in out}
	for entry in body.get("alerts") or []:
		if not isinstance(entry, dict):
			continue
		code = str(entry.get("code") or "")
		if code and code not in have:
			have.add(code)
			out.append(
				{
					"level": CRITICAL
					if str(entry.get("level")) == CRITICAL or code in CRITICAL_CODES
					else WARNING,
					"code": code,
					"box": box or None,
					"message": str(entry.get("message") or code),
				}
			)
	return out


def describe_box(body: dict, stale_hours: float, now=None) -> dict:
	now = now or now_utc()
	own = _block(body, "own_backup")
	push = _block(body, "push")
	restore = _block(body, "remote_restore_of_my_data")
	archive = _block(body, "last_archive_test")
	standby = _block(body, "standby_of_peer")
	duration = _number(restore.get("duration_s"))
	out = {
		"box": body.get("box"),
		"peer": body.get("peer") or None,
		"kit_version": body.get("kit_version") or None,
		"generated_at": body.get("generated_at") or None,
		"status_file_age_hours": hours_since(body.get("generated_at"), now),
		"last_backup": (
			{
				"at": own.get("at") or None,
				"set": own.get("set") or None,
				"size_mb": _number(own.get("size_mb")),
				"encrypted": _yes(own.get("encrypted")),
				"origin": own.get("origin") or None,
				"result": own.get("result") or None,
				"last_ok_at": own.get("last_ok_at")
				or (own.get("at") if str(own.get("result")).upper() == "OK" else None),
				"message": own.get("msg") or None,
			}
			if own
			else None
		),
		"offsite_copy": (
			{
				"peer": push.get("peer") or body.get("peer"),
				"last_push_at": push.get("at") or None,
				"set": push.get("set") or None,
				"result": push.get("result") or None,
			}
			if push
			else None
		),
		"last_standby_restore": (
			{
				"by": restore.get("restored_by") or None,
				"at": restore.get("at") or None,
				"set": restore.get("set") or None,
				"result": restore.get("result") or None,
				"counts_ok": _yes(restore.get("counts_ok")),
				"files_ok": _files_ok(restore.get("files")),
				"decrypt_failures": int(_number(restore.get("decrypt_fail")) or 0),
				"duration_minutes": math.ceil(duration / 60.0) if duration else None,
			}
			if restore
			else None
		),
		"last_archive_test": (
			{
				"at": archive.get("at") or None,
				"no_key_test": archive.get("no_key_test") or None,
				"result": archive.get("check") or archive.get("result") or None,
			}
			if archive
			else None
		),
		"standby_held_here": (
			{
				"of": body.get("peer") or None,
				"at": standby.get("at") or None,
				"result": standby.get("result") or None,
			}
			if standby
			else None
		),
		"promoted": body.get("promoted") or None,
		"fenced": bool(body.get("fenced")),
		"alerts": box_alerts(body, stale_hours, now),
	}
	return out


def _files_ok(text) -> bool | None:
	"""`812/812,1204/1204` → every pair matches. None when there is nothing to judge."""
	pairs = [part for part in str(text or "").split(",") if "/" in part]
	if not pairs:
		return None
	return all(part.split("/")[0].strip() == part.split("/")[1].strip() for part in pairs)


def _records(company: str) -> dict:
	"""What the Backup Record register says, for the last block of the answer."""
	from .tools import itgc

	window = itgc.BACKUP_VERIFICATION_DAYS
	out = {
		"latest_backup_record": None,
		"last_passing_test_on": None,
		"verification_window_days": window,
		"within_window": False,
	}
	if not compat.doctype_exists(BACKUP):
		return out
	filters = {"company": company} if company else {}
	latest = frappe.db.get_all(BACKUP, filters=filters, pluck="name", order_by="started_at desc", limit=1)
	out["latest_backup_record"] = str(latest[0]) if latest else None
	passed = frappe.db.get_all(
		BACKUP,
		filters={**filters, "test_restore_result": "Pass"},
		fields=["test_restore_on"],
		order_by="test_restore_on desc",
		limit=1,
	)
	if passed and passed[0].get("test_restore_on"):
		on = str(passed[0]["test_restore_on"])[:10]
		out["last_passing_test_on"] = on
		try:
			days = (
				datetime.date.fromisoformat(str(frappe.utils.today())[:10]) - datetime.date.fromisoformat(on)
			).days
			out["within_window"] = days <= window
		except ValueError:
			pass
	return out


def status(box: str = "", stale_hours: float = STALE_HOURS, include_raw: bool = False) -> dict:
	"""The whole answer `get_backup_status` gives. Reads; never writes; never raises on a file."""
	now = now_utc()
	files, skipped = read_files()
	wanted = str(box or "").strip()
	if wanted:
		files = {name: body for name, body in files.items() if name == wanted}
	boxes = [describe_box(body, stale_hours, now) for _name, body in sorted(files.items())]
	alerts: list = []
	if not boxes:
		alerts.append(
			_alert(
				"NO_STATUS_FILE",
				(
					f"No backup status file for {wanted!r}."
					if wanted
					else "No backup status file on this site."
				)
				+ " The erp-backup kit writes one after every stage; with none, nothing here can say the data is safe.",
				wanted,
			)
		)
	seen = set()
	for entry in boxes:
		for alert in entry["alerts"]:
			key = (alert["code"], alert.get("box"))
			if key not in seen:
				seen.add(key)
				alerts.append(alert)
	company = _company()
	records = _records(company)
	if not records["within_window"]:
		alerts.append(
			_alert(
				"NO_PASSING_TEST_IN_WINDOW",
				f"No Backup Record has a passing test restore in the last {records['verification_window_days']} days"
				+ (
					f" (last: {records['last_passing_test_on']})." if records["last_passing_test_on"] else "."
				),
			)
		)
	overall = "ok"
	for alert in alerts:
		if _RANK[alert["level"]] > _RANK[overall]:
			overall = alert["level"]
	out = {
		"site": str(getattr(frappe.local, "site", "") or "") or None,
		"checked_at": now.isoformat(timespec="seconds"),
		"overall": overall,
		"stale_hours": stale_hours,
		"boxes": boxes,
		"erpnext_records": records,
		"alerts": alerts,
		"skipped": skipped,
	}
	if include_raw:
		out["raw"] = files
	return out


# ── ingest ──────────────────────────────────────────────────────────────────
def _flag(key: str, default=None):
	try:
		from . import flags

		return flags.value(key, default=default)
	except Exception:
		return default


def _company() -> str:
	named = str(_flag(FLAG_COMPANY, "") or "").strip()
	if named and frappe.db.exists("Company", named):
		return named
	try:
		default = frappe.defaults.get_global_default("company") or frappe.db.get_single_value(
			"Global Defaults", "default_company"
		)
	except Exception:
		default = None
	if default and frappe.db.exists("Company", default):
		return str(default)
	names = frappe.db.get_all("Company", pluck="name", limit=2) or []
	return str(names[0]) if len(names) == 1 else ""


def own_box(files: dict) -> str:
	named = str(_flag(FLAG_BOX, "") or "").strip()
	if named:
		return named if named in files else ""
	return next(iter(files)) if len(files) == 1 else ""


def _by_key(key: str) -> str:
	return str(frappe.db.get_value(BACKUP, {"ingest_key": key}, "name") or "")


def _insert(company: str, key: str, **fields) -> str:
	doc = frappe.new_doc(BACKUP)
	doc.company = company
	doc.ingest_key = key
	for name, value in fields.items():
		if value is not None:
			doc.set(name, value)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _rto_hours(company: str):
	rows = frappe.db.get_all(
		BACKUP,
		filters={"company": company, "test_restore_result": "Pass", "restore_duration_minutes": (">", 0)},
		fields=["restore_duration_minutes"],
		order_by="test_restore_on desc",
		limit=1,
	)
	minutes = int(rows[0]["restore_duration_minutes"]) if rows else 0
	return math.ceil(minutes / 60.0) if minutes else None


def _apply_test(record: str, key: str, result: str, at, minutes, notes: str) -> bool:
	"""Write one test restore onto a record, unless this one (or a later one) is already there."""
	doc = frappe.get_doc(BACKUP, record)
	applied = [line for line in str(doc.get("test_ingest_key") or "").splitlines() if line]
	if key in applied:
		return False
	day = (site_naive(at) or str(frappe.utils.now()))[:10]
	if doc.get("test_restore_on") and str(doc.test_restore_on)[:10] > day:
		# An older test than the one on file: remembered, not written over it.
		doc.test_ingest_key = "\n".join([*applied, key][-20:])
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		return False
	doc.test_restore_result = result
	doc.test_restore_on = min(day, str(frappe.utils.today())[:10])
	doc.test_restore_by = None
	if minutes:
		doc.restore_duration_minutes = int(minutes)
	doc.test_restore_notes = notes
	# Every key ever applied is kept, so two kinds of test on one record (the
	# nightly restore, the archive test) do not take turns overwriting each other.
	doc.test_ingest_key = "\n".join([*applied, key][-20:])
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return True


def _result(value) -> str:
	text = str(value or "").strip().lower()
	if text in ("pass", "ok"):
		return "Pass"
	return "Partial" if text == "partial" else "Fail"


def run_ingest() -> dict:
	"""Status files → Backup Records. Idempotent. Raises nothing a caller must catch for a file."""
	report = {"box": None, "company": None, "created": [], "tests": [], "skipped": [], "notes": []}
	if not compat.doctype_exists(BACKUP) or not compat.has_field(BACKUP, "ingest_key"):
		report["notes"].append("this site has no Backup Record ingest columns yet — run `bench migrate`.")
		return report
	files, skipped = read_files()
	report["skipped"] = skipped
	if not files:
		report["notes"].append("no status file to ingest.")
		return report
	box = own_box(files)
	if not box:
		report["notes"].append(
			f"{len(files)} status files and no `{FLAG_BOX}` flag saying which box this site is; nothing was ingested."
		)
		return report
	company = _company()
	if not company:
		report["notes"].append(f"no company to file Backup Records under; set the `{FLAG_COMPANY}` flag.")
		return report
	report.update(box=box, company=company)
	body = files[box]
	kit = str(body.get("kit_version") or "")
	own = _block(body, "own_backup")
	offsite = bool(_flag(FLAG_OFFSITE, False))

	# 1. Our own backup: a good one per set, a failed one per failure.
	set_name = str(own.get("set") or "")
	if str(own.get("result") or "").upper() == "OK" and set_name:
		key = f"{box}|{set_name}|own"
		if not _by_key(key):
			encrypted = _yes(own.get("encrypted"))
			report["created"].append(
				_insert(
					company,
					key,
					backup_type="Full",
					status="Success",
					started_at=site_naive(own.get("started")) or site_naive(own.get("at")),
					completed_at=site_naive(own.get("at")),
					location=str(own.get("location") or f"{box}:{set_name}")[:140],
					size_mb=_number(own.get("size_mb")),
					retention_days=KEEP_DAILY,
					rpo_hours=RPO_HOURS,
					rto_hours=_rto_hours(company),
					offsite=0,
					notes=(
						f"Set {set_name}; origin {own.get('origin') or box}; "
						+ (
							f"encrypted (age, recipient {own.get('recipient') or '?'})"
							if encrypted
							else "NOT ENCRYPTED"
						)
						+ f"; erp-backup kit {kit}. Ingested from the box's status file."
					),
				)
			)
	elif str(own.get("result") or "").upper() == "FAIL" and own.get("at"):
		key = f"{box}|{own.get('at')}|failed"
		if not _by_key(key):
			report["created"].append(
				_insert(
					company,
					key,
					backup_type="Full",
					status="Failed",
					started_at=site_naive(own.get("started")) or site_naive(own.get("at")),
					location=str(own.get("location") or f"{box}: backup failed")[:140],
					notes=f"FAILED: {own.get('msg') or 'no reason given'}. Last good backup: {own.get('last_ok_at') or 'none on record'}. erp-backup kit {kit}.",
				)
			)

	# 2. The copy on the peer.
	push = _block(body, "push")
	pushed = str(push.get("set") or "")
	if str(push.get("result") or "").upper() == "OK" and pushed:
		key = f"{box}|{pushed}|replica"
		if not _by_key(key):
			peer = str(push.get("peer") or body.get("peer") or "peer")
			source = own if pushed == set_name else {}
			report["created"].append(
				_insert(
					company,
					key,
					backup_type="Offsite Replica" if offsite else "Full",
					status="Success",
					started_at=site_naive(push.get("at")),
					completed_at=site_naive(push.get("at")),
					location=f"{peer}:/home/umbrel/umbrel/erp-backup-from-{box}/daily/{pushed}"[:140],
					size_mb=_number(source.get("size_mb")),
					retention_days=KEEP_DAILY,
					rpo_hours=RPO_HOURS,
					rto_hours=_rto_hours(company),
					offsite=1 if offsite else 0,
					notes=f"Copy of set {pushed} on {peer}. erp-backup kit {kit}. Ingested from the box's status file.",
				)
			)

	# 3. The peer restored our data last night.
	restore = _block(body, "remote_restore_of_my_data")
	if restore.get("at") and restore.get("set"):
		record = _by_key(f"{box}|{restore['set']}|replica")
		by = str(restore.get("restored_by") or body.get("peer") or "peer")
		if record:
			duration = _number(restore.get("duration_s"))
			wrote = _apply_test(
				record,
				f"{by}|{restore['at']}|restore",
				_result(restore.get("result")),
				restore.get("at"),
				math.ceil(duration / 60.0) if duration else None,
				f"By erp-backup@{by}\n"
				f"method {restore.get('method') or '?'}; site {restore.get('site') or '?'}; "
				f"counts_ok {restore.get('counts_ok') or '?'}; files {restore.get('files') or '?'}; "
				f"secrets {restore.get('secrets') or '?'}; decrypt_fail {restore.get('decrypt_fail') or '0'}",
			)
			if wrote:
				report["tests"].append(
					{"record": record, "kind": "restore", "result": _result(restore.get("result"))}
				)
		else:
			report["notes"].append(
				f"a restore of set {restore['set']} was reported, and there is no replica record for that set yet."
			)

	# 4. A full restore of OUR archive, run on the peer (its file, source = peer).
	for other, theirs in files.items():
		if other == box:
			continue
		archive = _block(theirs, "last_archive_test")
		if (
			str(archive.get("source") or "") != "peer"
			or str(theirs.get("peer") or "") != box
			or not archive.get("at")
		):
			continue
		target = str(archive.get("set") or "")
		record = _by_key(f"{box}|{target}|replica") if target else ""
		if not record:
			latest = frappe.db.get_all(
				BACKUP,
				filters={"company": company, "ingest_key": ("like", f"{box}|%|replica")},
				pluck="name",
				order_by="started_at desc",
				limit=1,
			)
			record = str(latest[0]) if latest else ""
		if not record:
			continue
		duration = _number(archive.get("duration_s"))
		wrote = _apply_test(
			record,
			f"{other}|{archive['at']}|archive",
			_result(archive.get("check") or archive.get("result")),
			archive.get("at"),
			math.ceil(duration / 60.0) if duration else None,
			f"By erp-backup@{other}\nfull restore from encrypted archive + no-key test={archive.get('no_key_test') or '?'}",
		)
		if wrote:
			report["tests"].append(
				{
					"record": record,
					"kind": "archive",
					"result": _result(archive.get("check") or archive.get("result")),
				}
			)
	return report


@frappe.whitelist(methods=["POST"])
def ingest() -> dict:
	"""Read the status files into Backup Records now. System Manager only."""
	user = str(getattr(frappe.session, "user", "") or "")
	if "System Manager" not in set(frappe.get_roles(user) or []):
		raise frappe.PermissionError(
			"Ingesting backup status is a System Manager's act. Nothing was changed."
		)
	return run_ingest()


def ingest_scheduled() -> None:
	"""The hourly job. NEVER RAISES: a status file must not break the scheduler."""
	try:
		run_ingest()
	except Exception:  # pragma: no cover - reported in the Error Log, never raised
		try:
			frappe.log_error(title="erpnext_mcp backup status ingest")
		except Exception:
			pass


# ── the tool ────────────────────────────────────────────────────────────────
def get_backup_status(args: dict):
	"""Is this site's data safe right now? Read-only."""
	from .args import as_bool, as_str
	from .errors import ToolError
	from .result import ToolResult

	raw = args.get("stale_hours")
	stale = STALE_HOURS
	if raw not in (None, ""):
		try:
			stale = float(raw)
		except (TypeError, ValueError) as error:
			raise ToolError("stale_hours is a number of hours.") from error
		if stale <= 0:
			raise ToolError("stale_hours must be more than zero.")
	data = status(as_str(args, "box"), stale, as_bool(args, "include_raw", False))
	worst = [a for a in data["alerts"] if a["level"] == data["overall"]]
	return ToolResult(
		data=data,
		summary=(
			f"backups: {data['overall']}"
			+ (
				f" — {worst[0]['code']}"
				+ (f" and {len(data['alerts']) - 1} more" if len(data["alerts"]) > 1 else "")
				if worst
				else ""
			)
		),
	)
