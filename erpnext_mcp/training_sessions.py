# SPDX-License-Identifier: MIT
"""The group training event, and the one place that decides when a row is evidence.

WHY THIS MODULE EXISTS. `training.py` owns what a training RECORD means — the
regime vocabulary, the retention table, the lapse arithmetic — and nothing here
duplicates any of it. What it does not own is the question this release adds:
*was this person actually at that session, and can we prove it*. Four callers
need the same answer — `add_session_attendee` when it takes a scan,
`sign_session_attendance` when it takes a mark, `complete_training_session` when
it decides which rows become records, and `get_training_session` when it reports
what is still outstanding — and four copies of "has a badge AND a signature AND
was ticked present" is four chances for the completion to write a record the
read said was not ready.

────────────────────────────────────────────────────────────────────────────
THE SESSION DOES NOT REPLACE THE RECORD, AND THAT IS THE WHOLE ARCHITECTURE
────────────────────────────────────────────────────────────────────────────

An auditor asks about Ana. Ana's file has to answer, which means the unit of
evidence is and stays one Employee Training Record per person — that is what
`get_training_compliance_report` reads, what the `training_expiring` rule watches
and what `generate_audit_packet` pulls. A session that stored twelve people in one
document and called itself the evidence would be a document nobody's compliance
matrix can see into.

So the session is the ACT and the records are the EVIDENCE, and
`complete_training_session` is the one moment the first becomes the second. The
link runs both ways — the attendee row names the record it produced, the record
names nothing — because an auditor walks it in one direction only: from the
register outward to the afternoon, via the session named on the attendee row they
find when they ask how a name got onto a sheet.

────────────────────────────────────────────────────────────────────────────
BADGE PLUS SIGNATURE, AND WHY BOTH
────────────────────────────────────────────────────────────────────────────

They answer different questions and neither answers the other's. The badge scan
says WHO — it is an identity check made by a machine at the door, resolved
through the same `resolve_badge` path the crew clock uses, so a retired card or a
card belonging to somebody who has left is refused before a name reaches a sheet.
The signature says THEY AGREE THEY WERE TAUGHT, which is what §112.161(a)(4) asks
for by name and what every GAP checklist looks for first.

A sheet with signatures and no scans proves that somebody made thirty marks. A
sheet with scans and no signatures proves thirty cards were present. `READY` is
both, and `complete_training_session` will not write a record without them —
which is a refusal this app is entitled to make because the alternative is a
training record that an inspector reads as having been assembled.

WHAT IS NOT REFUSED: the row itself. Somebody who did not attend, or attended and
would not sign, keeps their row and is NAMED in the completion's answer. Deleting
them would lose the fact that they were expected, which is the fact a supervisor
needs on Monday.
"""

from __future__ import annotations

import frappe

from . import compat, training

DOCTYPE = "Training Session"
ATTENDEE_DOCTYPE = "Training Session Attendee"

#: The curriculum master both this module and `training.py` read.
TYPE_DOCTYPE = training.TYPE_DOCTYPE

# ── the status vocabulary ───────────────────────────────────────────────────
STATUS_SCHEDULED = "Scheduled"
STATUS_IN_PROGRESS = "In Progress"
STATUS_COMPLETED = "Completed"
STATUS_CANCELLED = "Cancelled"

STATUSES = (STATUS_SCHEDULED, STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_CANCELLED)

#: The two a session may still be worked on in. A completed session is finished
#: evidence and a cancelled one is a thing that did not happen; adding an
#: attendee to either is a different act with a different fix, so the tools refuse
#: it by name rather than appending a row nobody expected.
OPEN_STATUSES = (STATUS_SCHEDULED, STATUS_IN_PROGRESS)

#: Spellings a client will genuinely send. The iOS build says `in_progress`; a
#: model composing a call from a sentence says `done`.
STATUS_ALIASES = {
	"scheduled": STATUS_SCHEDULED,
	"booked": STATUS_SCHEDULED,
	"planned": STATUS_SCHEDULED,
	"in progress": STATUS_IN_PROGRESS,
	"in_progress": STATUS_IN_PROGRESS,
	"inprogress": STATUS_IN_PROGRESS,
	"running": STATUS_IN_PROGRESS,
	"started": STATUS_IN_PROGRESS,
	"completed": STATUS_COMPLETED,
	"complete": STATUS_COMPLETED,
	"done": STATUS_COMPLETED,
	"finished": STATUS_COMPLETED,
	"cancelled": STATUS_CANCELLED,
	"canceled": STATUS_CANCELLED,
	"called off": STATUS_CANCELLED,
}

# ── how a curriculum is delivered ───────────────────────────────────────────
#: Stored in Title Case, because that is what a Desk Select shows and what a
#: printed packet reads. THE TOKENS AN API CALLER SENDS ARE LOWER-CASE WITH
#: UNDERSCORES — that is the spelling the iOS build and every other client in
#: this app use — so both are accepted and one is stored. A column holding both
#: `field_demo` and `Field Demo` would be a column no filter can group.
#:
#: `Blended` arrived in v0.178.0 with `create_training_type`: a train-the-trainer
#: course is a classroom morning and a field afternoon, and filing it under
#: either one misdescribes the other half.
DELIVERY_METHODS = ("Video", "Classroom", "Field Demo", "Online", "Self Study", "Blended")

DELIVERY_ALIASES = {
	"video": "Video",
	"classroom": "Classroom",
	"class": "Classroom",
	"field_demo": "Field Demo",
	"field demo": "Field Demo",
	"fielddemo": "Field Demo",
	"field": "Field Demo",
	"demo": "Field Demo",
	"blended": "Blended",
	"hybrid": "Blended",
	"online": "Online",
	"self_study": "Self Study",
	"self study": "Self Study",
	"selfstudy": "Self Study",
	"self-study": "Self Study",
}

#: What each delivery method means for the handset that has to render it. Carried
#: by `get_training_curriculum` so an app choosing between a video player, a
#: document viewer and a plain description is choosing from the server's answer
#: rather than from a table compiled into a build.
DELIVERY_NOTES = {
	"Video": "A film somebody watches. `video_url` is the thing to open; where it is empty the curriculum has been marked as video and given nothing to play, which is a gap worth fixing before the session.",
	"Classroom": "Somebody talks in a room. The materials list is what has to be in the room; there is nothing for a handset to play.",
	"Field Demo": "Shown on the ground with the equipment in hand — a respirator fit, a ladder, a valve. The one delivery method whose evidence an auditor will ask to see photographs of.",
	"Online": "A course somebody completes elsewhere, usually with a certificate at the end. Attach the certificate to the training record rather than to the curriculum.",
	"Self Study": "A handout somebody reads on their own. The attachments are the training; the signature is the whole of the evidence that it happened.",
	"Blended": "Part in a room, part on the ground or online — a train-the-trainer course is the usual case. The materials list should say which half needs what; photographs of the field half are what an auditor will ask for.",
}

# ── what an attendee row is worth ───────────────────────────────────────────
#: Already turned into an Employee Training Record. Terminal: a second completion
#: of the same session must not file the same afternoon twice.
ATTENDEE_RECORDED = "recorded"

#: Present, scanned and signed. The state `complete_training_session` writes from.
ATTENDEE_READY = "ready"

#: Rostered and not there. Produces no record, and is not an error.
ATTENDEE_ABSENT = "absent"

#: There, and missing at least one of the two things that make attendance
#: provable. `missing` on the described row names which.
ATTENDEE_INCOMPLETE = "incomplete"

#: What each element of the evidence is, in the words the rule uses. Reported
#: beside a missing element rather than as a bare field name, because "no
#: badge_scan" is a schema complaint and the sentence below is a reason.
EVIDENCE_NOTES = {
	"badge_scan": (
		"no badge was scanned for this person, so the record would say they attended on the "
		"authority of whoever typed their name. The scan is the identity check — resolve_badge "
		"refuses a retired card and a card belonging to somebody who has left, which a typed "
		"name cannot."
	),
	"signature": (
		"not signed by the person trained. FSMA §112.161(a)(4) asks for the record to be signed "
		"or initialled by the person who performed the activity, and it is the element a GAP "
		"auditor looks for first on a training sheet."
	),
}


def available() -> bool:
	return compat.doctype_exists(DOCTYPE)


def canon_status(value) -> str:
	"""One session status in canonical spelling, or "" for one this app does not know."""
	text = str(value or "").strip()
	if not text:
		return ""
	for status in STATUSES:
		if text.lower() == status.lower():
			return status
	return STATUS_ALIASES.get(text.lower().replace("-", " ").replace("_", " "), "") or STATUS_ALIASES.get(
		text.lower(), ""
	)


def canon_delivery(value) -> str:
	"""One delivery method in canonical spelling, or "" for none.

	EMPTY IS ALLOWED AND MEANS NOBODY HAS SAID. A curriculum whose delivery is
	unstated is an ordinary thing on a site that has been filing training for
	years, and inventing `Classroom` for it would send a handset to a screen that
	shows nothing while claiming the operation decided.
	"""
	text = str(value or "").strip()
	if not text:
		return ""
	for method in DELIVERY_METHODS:
		if text.lower() == method.lower():
			return method
	return DELIVERY_ALIASES.get(text.lower().replace("-", " ").replace("_", " "), "") or DELIVERY_ALIASES.get(
		text.lower(), ""
	)


def delivery_note() -> str:
	"""The whole list in one sentence, for a refusal or a schema description."""
	return (
		"delivery_method is one of: "
		+ ", ".join(DELIVERY_METHODS)
		+ " (or the same words as video, classroom, field_demo, online, self_study)."
	)


def clock(value) -> str:
	"""A Time column as `HH:MM:SS`, or "" for nothing this module can read.

	`09:00`, `9:00`, `09:00:00` and a `timedelta` off a MariaDB TIME column all
	mean nine in the morning, and the controller compares start against end as
	strings — which is only sound if both have been through here first. NEVER
	RAISES: a value this cannot read becomes "" and the comparison is simply not
	made, because a session refused over an unparseable clock is a training that
	happened and was not recorded.
	"""
	if value in (None, ""):
		return ""
	text = str(value).strip()
	if not text:
		return ""
	# A timedelta renders as `9:00:00`, a Time column as `09:00:00`, and a caller
	# types `9:00`. All three are colon-separated numbers and nothing else here
	# is, so anything that does not parse comes back empty rather than wrong.
	pieces = text.split(":")
	if not 2 <= len(pieces) <= 3:
		return ""
	numbers = []
	for piece in pieces:
		piece = piece.strip().split(".")[0]
		if not piece.isdigit():
			return ""
		numbers.append(int(piece))
	while len(numbers) < 3:
		numbers.append(0)
	hours, minutes, seconds = numbers[:3]
	if hours > 23 or minutes > 59 or seconds > 59:
		return ""
	return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def minutes_between(start, end) -> int | None:
	"""Whole minutes from `start` to `end` on one day, or None.

	None rather than 0 where either end is missing or unreadable: a duration of
	zero and a duration nobody recorded are different facts, and a session
	reported as having taken no time is the one that gets written up.
	"""
	first, second = clock(start), clock(end)
	if not (first and second):
		return None
	begin = int(first[:2]) * 60 + int(first[3:5])
	finish = int(second[:2]) * 60 + int(second[3:5])
	if finish < begin:
		return None
	return finish - begin


# ── the curriculum ──────────────────────────────────────────────────────────
#: Every column of the Training Type this app reads. One list, so a field added
#: to the doctype and not to this tuple is a field nothing surfaces.
TYPE_FIELDS = (
	"name",
	"training_type_name",
	"active",
	"retention_years",
	"description",
	"video_url",
	"materials_description",
	"duration_minutes",
	"delivery_method",
	# v0.98.0. Whether this curriculum is delivered to a crew at once. Read by
	# the compliance-alert bundler to decide whether several lapsing records
	# become ONE session or N tasks. Through `compat.existing_fields` below, so a
	# site mid-migrate loses the key rather than the read.
	"group_training",
)


def type_row(name) -> dict:
	"""One Training Type's columns, selecting only the ones this site has.

	`{}` for a name that is not on the register — the CONTROLLER calls this to
	inherit defaults and must not fail a save over a curriculum Frappe's own link
	validation is about to refuse more precisely.
	"""
	wanted = str(name or "").strip()
	if not wanted or not compat.doctype_exists(TYPE_DOCTYPE):
		return {}
	row = frappe.db.get_value(
		TYPE_DOCTYPE, wanted, compat.existing_fields(TYPE_DOCTYPE, TYPE_FIELDS), as_dict=True
	)
	return dict(row or {})


def attachments_of(doctype: str, name: str, limit: int = 40) -> list:
	"""The Files hanging off one document, as a handset would list them.

	READ THROUGH `frappe.db.get_all` LIKE EVERY OTHER READ IN THIS APP, which is
	the deliberate difference from `tools/files.py`: that module checks Frappe
	permissions because it hands back CONTENT, and this hands back a list of
	names and URLs so a curriculum screen can show what exists. Fetching a file's
	bytes still goes through `get_attachment_content`, gate and all.
	"""
	if not compat.doctype_exists("File"):
		return []
	rows = (
		frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": doctype, "attached_to_name": name},
			fields=compat.existing_fields(
				"File", ("name", "file_name", "file_url", "file_size", "is_private")
			),
			limit=limit,
		)
		or []
	)
	return [
		{
			"file": row.get("name"),
			"file_name": row.get("file_name"),
			"file_url": row.get("file_url"),
			"file_size": row.get("file_size"),
			"is_private": bool(row.get("is_private")),
		}
		for row in (dict(entry) for entry in rows)
	]


def describe_type(row: dict, with_attachments: bool = True) -> dict:
	"""One curriculum in the shape a handset renders it."""
	name = str(row.get("name") or "")
	method = canon_delivery(row.get("delivery_method"))
	described = {
		"training_type": name,
		"active": bool(compat.checked(row.get("active"))),
		"description": row.get("description") or None,
		"delivery_method": method or None,
		"delivery_note": DELIVERY_NOTES.get(method) if method else None,
		"video_url": row.get("video_url") or None,
		"materials_description": row.get("materials_description") or None,
		"duration_minutes": int(row.get("duration_minutes") or 0) or None,
		"retention_years": int(row.get("retention_years") or 0) or None,
		"regimes": training.type_regimes(name),
	}
	if with_attachments:
		described["attachments"] = attachments_of(TYPE_DOCTYPE, name)
	return described


# ── the session ─────────────────────────────────────────────────────────────
#: Every column of the session this app reads.
FIELDS = (
	"name",
	"training_type",
	"status",
	"company",
	"session_date",
	"start_time",
	"end_time",
	"location",
	"duration_minutes",
	"conducted_by",
	"conducted_by_name",
	"instructor_name",
	"provider",
	"training_source",
	"delivery_method",
	"expires_date",
	"content_topics_covered",
	"completed_at",
	"completed_by",
	"records_created",
	"generated_pdf",
	"generated_pdf_on",
	"source_alerts",
	"notes",
)

#: Every column of an attendee row this app reads.
#:
#: `parent` IS NOT HERE AND MUST NOT BE. It is a framework column rather than one
#: of the doctype's own fields, so `compat.existing_fields` — which asks the meta
#: — drops it, and a batch read that relied on it would come back with every row
#: filed under an empty key. `attendees_for_parents` asks for it explicitly, the
#: same way `training.rows_for_parents` does.
ATTENDEE_FIELDS = (
	"name",
	"idx",
	"employee",
	"employee_name",
	"attended",
	"badge_scan",
	"scanned_at",
	"scan_latitude",
	"scan_longitude",
	"scan_accuracy_meters",
	"scan_h3_cell",
	"scan_source",
	"signature",
	"signed_at",
	"training_record",
	"signing_evidence",
	"notes",
)


def rows(filters: dict, limit: int = 500, order_by: str = "session_date desc") -> list:
	"""Sessions, selecting only the columns this site actually has."""
	if not available():
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			DOCTYPE,
			filters=filters or {},
			fields=compat.existing_fields(DOCTYPE, FIELDS),
			order_by=order_by,
			limit=limit,
		)
		or []
	]


#: Most sessions one idempotency read walks. Same figure `sessions.SESSION_CAP`
#: uses and for the same reason: this is a question about outstanding work, not
#: an export.
SESSION_CAP = 500


def alerts_answered_by_open_sessions(company: str = "") -> dict:
	"""alert docname → the Training Session that already answers it.

	v0.98.0. THE TWIN OF `sessions.alerts_answered_by_open_sessions`, and it is a
	separate function rather than a parameter on that one because the two read
	different doctypes with different open-states and neither should have to know
	about the other's.

	SCOPED TO SESSIONS STILL OPEN. A COMPLETED session has written its training
	records; those move the register; the alerts it answered dismiss themselves on
	the next sweep and never reach the generator, which only ever looks at alerts
	that are still open. A CANCELLED one answers nothing and its alerts must come
	back — which is exactly what leaving it out of this read does.
	"""
	if not compat.doctype_exists(DOCTYPE) or not compat.has_field(DOCTYPE, "source_alerts"):
		return {}
	filters = {"status": ("in", list(OPEN_STATUSES)), "source_alerts": ("is", "set")}
	if company:
		filters["company"] = company
	out = {}
	for row in (
		frappe.db.get_all(
			DOCTYPE,
			filters=filters,
			fields=["name", "training_type", "company", "session_date", "source_alerts"],
			limit=SESSION_CAP,
		)
		or []
	):
		for alert in alert_names(row.get("source_alerts")):
			out[alert] = {
				"session": str(row["name"]),
				"training_type": row.get("training_type"),
				"session_date": str(row.get("session_date") or "") or None,
			}
	return out


def alert_names(raw) -> list:
	"""The alert docnames on a session, parsed from the stored text.

	WHOLE TOKENS, NEVER A SUBSTRING — the identical argument `sessions.alert_names`
	makes: an alert docname is a prefix of the next one often enough that a LIKE
	would answer yes about the wrong record, and the consequence is a compliance
	alert silently treated as answered.
	"""
	if not raw:
		return []
	pieces = (
		[str(entry) for entry in raw]
		if isinstance(raw, (list, tuple))
		else str(raw).replace(",", "\n").split("\n")
	)
	out = []
	for piece in pieces:
		name = piece.strip()
		if name and name not in out:
			out.append(name)
	return out


def attendees_of(session: str, limit: int = 500) -> list:
	"""One session's attendee rows, read off the child table directly.

	Directly rather than by loading the parent, for the reason `shifts.crew_of`
	does the same: `list_training_sessions` reports the attendance summary of
	forty sessions at once, and forty parent loads to count signatures is forty
	round trips to answer one join.
	"""
	if not compat.doctype_exists(ATTENDEE_DOCTYPE):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			ATTENDEE_DOCTYPE,
			filters={"parenttype": DOCTYPE, "parentfield": "attendees", "parent": session},
			fields=compat.existing_fields(ATTENDEE_DOCTYPE, ATTENDEE_FIELDS),
			order_by="idx asc",
			limit=limit,
		)
		or []
	]


def attendees_for_parents(names, limit_per_parent: int = 200) -> dict:
	"""`{session docname: [attendee rows]}` for a batch of sessions, in one query."""
	wanted = [str(name) for name in (names or []) if name]
	if not wanted or not compat.doctype_exists(ATTENDEE_DOCTYPE):
		return {}
	found: dict = {}
	for row in (
		frappe.db.get_all(
			ATTENDEE_DOCTYPE,
			filters={"parenttype": DOCTYPE, "parentfield": "attendees", "parent": ("in", wanted)},
			fields=["parent", *compat.existing_fields(ATTENDEE_DOCTYPE, ATTENDEE_FIELDS)],
			order_by="idx asc",
			limit=len(wanted) * limit_per_parent,
		)
		or []
	):
		found.setdefault(str(dict(row).get("parent") or ""), []).append(dict(row))
	return found


def scan_position(row: dict) -> dict | None:
	"""Where the badge was scanned, in the shape a shift breadcrumb reports it.

	`shifts.describe_location_row`'s KEYS, not a second set. A fix taken at a shed
	door and one taken on a block are the same measurement, and a client that had
	to read `lat` on one and `scan_latitude` on the other would be reading two
	formats of one thing. `None` where the phone had no fix, because an object of
	nulls reads on a screen as a position that failed rather than one nobody took.
	"""
	latitude, longitude = row.get("scan_latitude"), row.get("scan_longitude")
	if latitude in (None, "") or longitude in (None, ""):
		return None
	accuracy = row.get("scan_accuracy_meters")
	return {
		"lat": round(float(latitude), 7),
		"lon": round(float(longitude), 7),
		"accuracy_meters": round(float(accuracy), 2) if accuracy not in (None, "") else None,
		"h3_cell": row.get("scan_h3_cell") or None,
		"source": row.get("scan_source") or None,
	}


def describe_attendee(row: dict) -> dict:
	"""One attendee row, with the state that decides whether it becomes a record.

	`state` AND `missing` ARE COMPUTED HERE AND NOWHERE ELSE. See the module
	docstring: four callers need the same answer, and the one that writes records
	must not be able to disagree with the one that reported what was outstanding.
	"""
	scanned = bool(str(row.get("badge_scan") or "").strip())
	signed = bool(str(row.get("signature") or "").strip())
	recorded = str(row.get("training_record") or "").strip()
	attended = compat.checked(row.get("attended"))

	missing = []
	if not scanned:
		missing.append("badge_scan")
	if not signed:
		missing.append("signature")

	if recorded:
		state = ATTENDEE_RECORDED
	elif not attended:
		state = ATTENDEE_ABSENT
	elif missing:
		state = ATTENDEE_INCOMPLETE
	else:
		state = ATTENDEE_READY

	return {
		"employee": row.get("employee"),
		"employee_name": row.get("employee_name") or row.get("employee"),
		"attended": bool(attended),
		"badge_scan": row.get("badge_scan") or None,
		"badge_scanned": scanned,
		"scanned_at": str(row.get("scanned_at") or "") or None,
		"scan_position": scan_position(row),
		"signature": row.get("signature") or None,
		"signed": signed,
		"signed_at": str(row.get("signed_at") or "") or None,
		"training_record": recorded or None,
		"signing_evidence": row.get("signing_evidence") or None,
		"state": state,
		# `missing` is reported for an ABSENT row too, and on purpose: somebody
		# who was scanned in and left before signing is a different Monday
		# conversation from somebody who never arrived, and a blank list would
		# collapse the two.
		"missing": missing,
		"missing_notes": [EVIDENCE_NOTES[element] for element in missing],
		"notes": row.get("notes") or None,
	}


def attendance_summary(described: list) -> dict:
	"""The counts every read of a session leads with."""
	counted = {
		ATTENDEE_READY: 0,
		ATTENDEE_RECORDED: 0,
		ATTENDEE_ABSENT: 0,
		ATTENDEE_INCOMPLETE: 0,
	}
	for row in described:
		counted[row["state"]] = counted.get(row["state"], 0) + 1
	return {
		"attendees": len(described),
		"ready": counted[ATTENDEE_READY],
		"recorded": counted[ATTENDEE_RECORDED],
		"absent": counted[ATTENDEE_ABSENT],
		"incomplete": counted[ATTENDEE_INCOMPLETE],
		"without_badge_scan": [
			row["employee"] for row in described if "badge_scan" in row["missing"] and row["attended"]
		],
		"without_signature": [
			row["employee"] for row in described if "signature" in row["missing"] and row["attended"]
		],
	}


def describe(row: dict, attendees: list | None = None) -> dict:
	"""One session in the shape every tool reports it.

	`duration_minutes` is REPORTED AS STORED and `duration_from_clock` beside it
	is computed from the two times. They disagree whenever somebody recorded a
	planned length and an actual one, and reconciling them here would throw away
	the disagreement — which is the number an auditor asking "was this really
	ninety minutes" is entitled to see.
	"""
	described_attendees = [describe_attendee(entry) for entry in (attendees or [])]
	name = str(row.get("name") or "")
	return {
		"name": name,
		"training_type": row.get("training_type"),
		"status": row.get("status") or STATUS_SCHEDULED,
		"company": row.get("company"),
		"session_date": str(row.get("session_date") or "") or None,
		"start_time": clock(row.get("start_time")) or None,
		"end_time": clock(row.get("end_time")) or None,
		"location": row.get("location") or None,
		"duration_minutes": int(row.get("duration_minutes") or 0) or None,
		"duration_from_clock": minutes_between(row.get("start_time"), row.get("end_time")),
		"conducted_by": row.get("conducted_by") or None,
		"conducted_by_name": row.get("conducted_by_name") or None,
		"instructor_name": row.get("instructor_name") or None,
		"instructor": row.get("conducted_by_name") or row.get("instructor_name") or None,
		"provider": row.get("provider") or None,
		"training_source": row.get("training_source") or None,
		"delivery_method": canon_delivery(row.get("delivery_method")) or None,
		"expires_date": str(row.get("expires_date") or "") or None,
		"one_time": not str(row.get("expires_date") or ""),
		"regimes": training.rows_for_parents(DOCTYPE, [name], "regimes").get(name, []),
		"content_topics_covered": training.topics(row.get("content_topics_covered")),
		"completed_at": str(row.get("completed_at") or "") or None,
		"completed_by": row.get("completed_by") or None,
		"records_created": int(row.get("records_created") or 0),
		"notes": row.get("notes") or None,
		"attendee_rows": described_attendees,
		"attendance": attendance_summary(described_attendees),
	}


def completion_blockers(described: dict) -> list:
	"""What stands between this session and a completion, in the caller's terms.

	SESSION-LEVEL ONLY. An attendee who has not signed does not block the
	completion — their row simply produces no record and is named — because a
	session where eleven of twelve signed should file eleven records rather than
	nothing. What blocks it is the session lacking something EVERY record it
	writes would need, which is the regimes, the topics and at least one ready
	row.
	"""
	blockers = []
	if not described.get("regimes"):
		blockers.append(
			"the session carries no regimes, so every record it wrote would appear in no audit "
			"packet. Which audits a training counts towards is the whole point of the record — "
			"set them on the session, or correct them once on the Training Type and they are "
			"inherited by every future session of it."
		)
	if not described.get("content_topics_covered"):
		blockers.append(
			"content_topics_covered is empty. It is what makes a regime tag defensible rather "
			"than optimistic: Oregon's heat rule names six topics that must be covered annually, "
			"and a record claiming OR-OSHA without them is a record an inspector will disallow."
		)
	if not described["attendance"]["ready"]:
		blockers.append(
			"no attendee is ready. A record is written for somebody who was marked present, had "
			"a badge scanned and gave a signature; nobody on this list has all three, so there is "
			"nothing to file."
		)
	return blockers
