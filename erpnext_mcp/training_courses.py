# SPDX-License-Identifier: MIT
"""Multi-day classes. v0.212.0. docs/design/quick_wins_2026_10.md, Amendment 1.

A DAY IS A TRAINING SESSION, AS IT ALWAYS WAS — its own date, times, place,
attendee rows, signatures and sign-in sheet. A COURSE is a Training Session
that other sessions point at through `parent_session`. Nothing else is new: no
doctype, no second attendance register. Until this, a class that ran on Oct 28
and Nov 17 was two unrelated sessions (or one session with one date), and day
two fell off every screen the morning after day one.

THE COURSE HOLDS THE REGISTRATION AND GIVES THE CREDIT. Its attendee table says
who is expected on every required day; it is never signed and holds no
attendance of its own. A day holds who came. One Employee Training Record is
filed per person, by the course, when every required day is Completed — and
only for somebody who attended all of them.

A SESSION WITH NO PARENT AND NO DAYS IS A COURSE OF ONE, which is every session
that existed before this release. Nothing about it changes and nothing is
rewritten.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat, flags, training_sessions

DOCTYPE = training_sessions.DOCTYPE
ATTENDEE = training_sessions.ATTENDEE_DOCTYPE
SELF = "Self"

#: Wording and timing (Farm Feature Flags; these are the defaults, nothing is seeded).
DEFAULTS = {
	"training_card_days_before": 1,
	"training_card_title_tomorrow": "Class tomorrow",
	"training_card_title_tomorrow_es": "Clase mañana",
	"training_card_title_today": "Class today",
	"training_card_title_today_es": "Clase hoy",
	"training_card_title_soon": "Class on {date}",
	"training_card_title_soon_es": "Clase el {date}",
	"training_card_day_suffix": " — Day {day} of {days}",
	"training_card_day_suffix_es": " — Día {day} de {days}",
	"training_checkin_opens_minutes_before": 60,
	"training_checkin_closes_minutes_after": 120,
	"training_reminder_hour": 18,
	"training_reminder_title": "Class tomorrow",
	"training_reminder_title_es": "Clase mañana",
	"training_reminder_body": "{training_type}{day_suffix}, {time} at {location}.",
	"training_reminder_body_es": "{training_type}{day_suffix}, {time} en {location}.",
}
MAX_REMINDERS_PER_RUN = 200
MAX_DAYS = 31


# ── small helpers ───────────────────────────────────────────────────────────
def ready() -> bool:
	return compat.doctype_exists(DOCTYPE) and compat.has_field(DOCTYPE, "parent_session")


def setting(key: str, company: str = "", user: str = ""):
	"""One wording / timing value: the farm's flag, else the default."""
	default = DEFAULTS[key]
	try:
		value = flags.value(key, company, (), None, default=default, user=user)
	except Exception:
		return default
	if value in (None, ""):
		return default
	if isinstance(default, (int, float)) and not isinstance(default, bool):
		try:
			return type(default)(float(value))
		except (TypeError, ValueError):
			return default
	return str(value)


def text(key: str, language: str = "", company: str = "", user: str = "") -> str:
	"""A Text setting in the reader's language, falling back to English."""
	if str(language or "").lower().startswith("es"):
		spanish = str(setting(f"{key}_es", company, user) or "")
		if spanish:
			return spanish
	return str(setting(key, company, user) or "")


def _date(value) -> datetime.date | None:
	try:
		return datetime.date.fromisoformat(str(value or "")[:10])
	except ValueError:
		return None


def _now() -> datetime.datetime:
	try:
		return datetime.datetime.fromisoformat(str(frappe.utils.now())[:19])
	except ValueError:
		return datetime.datetime.now()


def _at(day, clock_text) -> datetime.datetime | None:
	"""A session date plus a clock time, or None when the day has no such time."""
	date = _date(day)
	clock = training_sessions.clock(clock_text)
	if date is None or not clock:
		return None
	try:
		return datetime.datetime.combine(date, datetime.time.fromisoformat(clock))
	except ValueError:
		return None


def _row(name: str) -> dict:
	fields = compat.existing_fields(DOCTYPE, training_sessions.FIELDS)
	return dict(frappe.db.get_value(DOCTYPE, name, fields, as_dict=True) or {})


def _sort_key(row: dict) -> tuple:
	return (
		str(row.get("session_date") or ""),
		training_sessions.clock(row.get("start_time")) or "",
		str(row.get("name") or ""),
	)


# ── the shape of a course ───────────────────────────────────────────────────
def days_of(course: str) -> list:
	"""The day sessions of a course, in order. [] for a single-day session."""
	if not course or not ready():
		return []
	fields = compat.existing_fields(DOCTYPE, training_sessions.FIELDS)
	found = frappe.db.get_all(DOCTYPE, filters={"parent_session": course}, fields=fields, limit=MAX_DAYS * 4)
	return sorted((dict(row) for row in found), key=_sort_key)


def parent_of(row: dict) -> str:
	return str(row.get("parent_session") or "")


def is_course(name: str) -> bool:
	return bool(name) and ready() and bool(frappe.db.get_value(DOCTYPE, {"parent_session": name}, "name"))


def required(row: dict) -> bool:
	value = row.get("required_day")
	return True if value in (None, "") else bool(compat.checked(value))


def _attended_days(days: list, employee: str) -> dict:
	"""{day session: bool} for one person, over the days given."""
	if not employee or not days:
		return {}
	rows = training_sessions.attendees_for_parents([day["name"] for day in days])
	out = {}
	for day in days:
		mine = [r for r in rows.get(day["name"], []) if str(r.get("employee") or "") == employee]
		if mine:
			out[day["name"]] = bool(compat.checked(mine[0].get("attended")))
	return out


def course_fields(row: dict, viewer: str = "") -> dict:
	"""What every session answer says about days. Cheap for a single-day session."""
	blank = {
		"parent_session": None,
		"day_number": None,
		"day_count": 1,
		"required_day": True,
		"end_date": None,
		"is_course": False,
		"days": [],
	}
	if not ready():
		return blank
	name = str(row.get("name") or "")
	parent = parent_of(row)
	course = parent or name
	days = days_of(course) if course else []
	if not days:
		return blank
	attended = _attended_days(days, viewer)
	listed = []
	for index, day in enumerate(days):
		entry = {
			"session": day["name"],
			"day_number": int(day.get("day_number") or 0) or index + 1,
			"session_date": str(day.get("session_date") or "") or None,
			"start_time": training_sessions.clock(day.get("start_time")) or None,
			"end_time": training_sessions.clock(day.get("end_time")) or None,
			"location": day.get("location") or None,
			"status": day.get("status") or training_sessions.STATUS_SCHEDULED,
			"required": required(day),
		}
		if day["name"] in attended:
			entry["attended"] = attended[day["name"]]
		listed.append(entry)
	mine = next((d for d in listed if d["session"] == name), None)
	return {
		"parent_session": parent or None,
		"day_number": mine["day_number"] if mine else None,
		"day_count": len(listed),
		"required_day": required(row) if parent else True,
		"end_date": listed[-1]["session_date"],
		"is_course": not parent,
		"days": listed,
	}


def renumber(course: str) -> None:
	"""Day 1, Day 2… by date, and the course's first and last day."""
	days = days_of(course)
	for index, day in enumerate(days):
		if int(day.get("day_number") or 0) != index + 1:
			frappe.db.set_value(DOCTYPE, day["name"], "day_number", index + 1)
	if days:
		frappe.db.set_value(
			DOCTYPE,
			course,
			{"session_date": days[0].get("session_date"), "end_date": days[-1].get("session_date")},
		)


# ── building one ────────────────────────────────────────────────────────────
COPIED = (
	"training_type",
	"company",
	"conducted_by",
	"instructor_name",
	"provider",
	"training_source",
	"delivery_method",
	"content_topics_covered",
	"expires_date",
)


def day_specs(raw) -> list:
	"""`days` as a list of dicts, or raises ValueError with a sentence."""
	if raw in (None, "", []):
		return []
	if isinstance(raw, str):
		import json

		try:
			raw = json.loads(raw)
		except ValueError:
			raise ValueError(
				"days must be a list like [{session_date, start_time, end_time, location}]."
			) from None
	if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
		raise ValueError("days must be a list like [{session_date, start_time, end_time, location}].")
	if len(raw) > MAX_DAYS:
		raise ValueError(f"a course has at most {MAX_DAYS} days.")
	out = []
	for index, item in enumerate(raw):
		date = _date(item.get("session_date") or item.get("date"))
		if date is None:
			raise ValueError(f"days[{index}] needs a session_date (YYYY-MM-DD).")
		start = training_sessions.clock(item.get("start_time"))
		end = training_sessions.clock(item.get("end_time"))
		if start and end and end < start:
			raise ValueError(f"days[{index}] ends before it starts.")
		out.append(
			{
				"session_date": date.isoformat(),
				"start_time": start,
				"end_time": end,
				"location": str(item.get("location") or "").strip(),
				"required": item.get("required", item.get("required_day", True))
				not in (0, "0", False, "false"),
			}
		)
	dates = [spec["session_date"] for spec in out]
	if len(set(dates)) != len(dates) and not all(spec["start_time"] for spec in out):
		raise ValueError("two days share a date — give each a start_time so they can be told apart.")
	return sorted(out, key=lambda spec: (spec["session_date"], spec["start_time"] or ""))


def spawn_day(course, spec: dict):
	"""Insert one day of `course` (a Training Session doc). Returns the day doc."""
	day = frappe.new_doc(DOCTYPE)
	for field in COPIED:
		if course.get(field) not in (None, ""):
			day.set(field, course.get(field))
	for row in course.get("regimes") or []:
		day.append("regimes", {"regime": row.get("regime")} if hasattr(row, "get") else row)
	day.status = training_sessions.STATUS_SCHEDULED
	day.session_date = spec["session_date"]
	day.start_time = spec.get("start_time") or course.get("start_time")
	day.end_time = spec.get("end_time") or course.get("end_time")
	day.location = spec.get("location") or course.get("location")
	day.parent_session = course.name
	day.required_day = 1 if spec.get("required", True) else 0
	for row in course.get("attendees") or []:
		day.append("attendees", {"employee": row.get("employee"), "attended": 0})
	day.flags.ignore_permissions = True
	day.insert(ignore_permissions=True)
	return day


def register(course: str, employee: str) -> list:
	"""Put somebody on the course and on each open day they are not on. Returns the
	day sessions they were added to."""
	added = []
	targets = [course] + [
		day["name"]
		for day in days_of(course)
		if (day.get("status") or training_sessions.STATUS_SCHEDULED) in training_sessions.OPEN_STATUSES
	]
	for name in targets:
		doc = frappe.get_doc(DOCTYPE, name)
		if any(str(r.get("employee") or "") == employee for r in doc.get("attendees") or []):
			continue
		doc.append("attendees", {"employee": employee, "attended": 0})
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		if name != course:
			added.append(name)
	return added


def adopt(course, sessions: list) -> None:
	"""Make existing sessions the days of `course`; register everybody on them."""
	people = []
	for name in sessions:
		for row in training_sessions.attendees_of(name):
			person = str(row.get("employee") or "")
			if person and person not in people:
				people.append(person)
		frappe.db.set_value(DOCTYPE, name, "parent_session", course.name)
	if people:
		doc = frappe.get_doc(DOCTYPE, course.name)
		have = {str(r.get("employee") or "") for r in doc.get("attendees") or []}
		for person in people:
			if person not in have:
				doc.append("attendees", {"employee": person, "attended": 0})
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		for person in people:
			register(course.name, person)
	renumber(course.name)


def new_course_over(row: dict):
	"""A course document carrying a session's curriculum facts and no attendance."""
	source = frappe.get_doc(DOCTYPE, row["name"])
	course = frappe.new_doc(DOCTYPE)
	for field in (*COPIED, "location", "notes"):
		if source.get(field) not in (None, ""):
			course.set(field, source.get(field))
	for regime in source.get("regimes") or []:
		course.append("regimes", {"regime": regime.get("regime")})
	course.status = training_sessions.STATUS_SCHEDULED
	course.session_date = source.session_date
	course.flags.ignore_permissions = True
	course.insert(ignore_permissions=True)
	return course


# ── attendance across days, and credit ──────────────────────────────────────
def attendance(course: str) -> dict:
	"""Per person on the course: which required days they attended and missed.

	{employee: {employee_name, attended: [day…], missed: [day…], signature,
	signed_on, scanned}} — `missed` counts only REQUIRED days that are Completed
	or more than a day past."""
	days = days_of(course)
	registered = training_sessions.attendees_of(course)
	by_day = training_sessions.attendees_for_parents([day["name"] for day in days])
	today = _now().date()
	out: dict = {}

	def person(employee, name=""):
		return out.setdefault(
			employee,
			{
				"employee": employee,
				"employee_name": name or employee,
				"attended": [],
				"missed": [],
				"signature": "",
				"signed_on": "",
				"scanned": False,
			},
		)

	for row in registered:
		if row.get("employee"):
			person(str(row["employee"]), str(row.get("employee_name") or ""))
	for day in days:
		rows = {str(r.get("employee") or ""): r for r in by_day.get(day["name"], [])}
		for employee, row in rows.items():
			if employee:
				person(employee, str(row.get("employee_name") or ""))
		date = _date(day.get("session_date"))
		over = (day.get("status") == training_sessions.STATUS_COMPLETED) or (
			date is not None and (today - date).days > 1
		)
		for employee, entry in out.items():
			row = rows.get(employee)
			described = training_sessions.describe_attendee(row) if row else None
			if described and described["attended"]:
				entry["attended"].append(day["name"])
				entry["scanned"] = entry["scanned"] or described["badge_scanned"]
				if described["signature"] and str(day.get("session_date") or "") >= entry["signed_on"]:
					entry["signature"] = described["signature"]
					entry["signed_on"] = str(day.get("session_date") or "")
			elif required(day) and over and day.get("status") != training_sessions.STATUS_CANCELLED:
				entry["missed"].append(day["name"])
	return out


def required_days(course: str) -> list:
	return [
		day
		for day in days_of(course)
		if required(day) and day.get("status") != training_sessions.STATUS_CANCELLED
	]


def open_required_days(course: str) -> list:
	return [day for day in required_days(course) if day.get("status") != training_sessions.STATUS_COMPLETED]


def earned(course: str) -> tuple:
	"""(ready, incomplete, missed): who has earned the course's record.

	ready: attended every required day, was identified on one, and signed one.
	incomplete: attended every required day but nothing proves it (no scan or no
	signature). missed: did not attend a required day."""
	needed = [day["name"] for day in required_days(course)]
	ready_rows, incomplete, missed = [], [], []
	for entry in attendance(course).values():
		absent = [day for day in needed if day not in entry["attended"]]
		if absent:
			missed.append({**entry, "missed": absent})
		elif not entry["signature"] or not entry["scanned"]:
			lacking = [
				w for w, ok in (("badge_scan", entry["scanned"]), ("signature", entry["signature"])) if not ok
			]
			incomplete.append({**entry, "missing": lacking})
		else:
			ready_rows.append(entry)
	return ready_rows, incomplete, missed


# ── the card on Today, and checking in ──────────────────────────────────────
def language_of(employee: str) -> str:
	if not employee or not compat.has_field("Employee", "preferred_language"):
		return ""
	return str(frappe.db.get_value("Employee", employee, "preferred_language") or "")


def _my_days(employee: str, first: datetime.date, last: datetime.date) -> list:
	"""Open sessions-that-are-days (or single sessions) this person is on, by date."""
	if not employee:
		return []
	rows = frappe.db.get_all(
		DOCTYPE,
		filters={
			"session_date": ("between", [first.isoformat(), last.isoformat()]),
			"status": ("in", list(training_sessions.OPEN_STATUSES)),
		},
		fields=compat.existing_fields(DOCTYPE, training_sessions.FIELDS),
		limit=500,
	)
	rows = [dict(row) for row in rows]
	if not rows:
		return []
	courses = {str(row["name"]) for row in rows if is_course(str(row["name"]))}
	attendees = training_sessions.attendees_for_parents([row["name"] for row in rows])
	mine = []
	for row in rows:
		if row["name"] in courses:
			continue  # a course is not a day; its days are in this list
		entry = next(
			(r for r in attendees.get(row["name"], []) if str(r.get("employee") or "") == employee), None
		)
		if entry is not None:
			mine.append((row, entry))
	return sorted(mine, key=lambda pair: _sort_key(pair[0]))


def check_in_window(row: dict, company: str = "", user: str = "") -> tuple:
	"""(opens, closes) for a day; all of the day when it carries no times."""
	date = _date(row.get("session_date"))
	if date is None:
		return None, None
	start = _at(row.get("session_date"), row.get("start_time"))
	end = _at(row.get("session_date"), row.get("end_time"))
	if start is None:
		return (
			datetime.datetime.combine(date, datetime.time.min),
			datetime.datetime.combine(date, datetime.time.max),
		)
	before = int(setting("training_checkin_opens_minutes_before", company, user))
	after = int(setting("training_checkin_closes_minutes_after", company, user))
	return (
		start - datetime.timedelta(minutes=max(0, before)),
		(end or start) + datetime.timedelta(minutes=max(0, after)),
	)


def _words(row: dict, fields: dict, language: str, company: str, user: str) -> dict:
	"""The placeholders a wording template may use."""
	suffix = ""
	if fields["day_count"] > 1 and fields["day_number"]:
		suffix = text("training_card_day_suffix", language, company, user).format(
			day=fields["day_number"], days=fields["day_count"]
		)
	start = training_sessions.clock(row.get("start_time"))
	end = training_sessions.clock(row.get("end_time"))
	clock = (start or "")[:5] + (f"–{end[:5]}" if start and end else "")
	return {
		"training_type": str(row.get("training_type") or ""),
		"day": fields["day_number"] or 1,
		"days": fields["day_count"],
		"day_suffix": suffix,
		"date": str(row.get("session_date") or ""),
		"time": clock,
		"location": str(row.get("location") or ""),
	}


def _fill(template: str, words: dict) -> str:
	try:
		out = template.format(**words)
	except (KeyError, IndexError, ValueError):
		out = template
	# A template that names a time or place the session does not have.
	for stub in (",  at .", ",  en .", " at .", " en .", ", ."):
		out = out.replace(stub, ".")
	return " ".join(out.split())


def cards(user: str, employee: str) -> dict:
	"""The caller's classes that are today or coming within the configured days."""
	now = _now()
	out = {"cards": [], "evaluated_at": str(frappe.utils.now())}
	if not ready() or not employee:
		return out
	company = str(frappe.db.get_value("Employee", employee, "company") or "")
	language = language_of(employee)
	ahead = max(0, int(setting("training_card_days_before", company, user)))
	today = now.date()
	for row, entry in _my_days(employee, today, today + datetime.timedelta(days=ahead)):
		date = _date(row.get("session_date"))
		delta = (date - today).days
		phase = "today" if delta == 0 else ("tomorrow" if delta == 1 else "soon")
		fields = course_fields(row, employee)
		words = _words(row, fields, language, company, user)
		title = (
			_fill(text(f"training_card_title_{phase}", language, company, user), words) + words["day_suffix"]
		)
		opens, closes = check_in_window(row, company, user)
		checked_in = bool(compat.checked(entry.get("attended"))) and bool(str(entry.get("scanned_at") or ""))
		can = phase == "today" and not checked_in and opens is not None and opens <= now <= closes
		note = ""
		if phase == "today" and not checked_in and opens is not None:
			if now < opens:
				note = f"Check-in opens at {opens.strftime('%H:%M')}."
			elif now > closes:
				note = "Check-in has closed — see whoever is running the class."
		course = parent_of(row) or None
		papers = [{"doctype": DOCTYPE, "docname": row["name"]}]
		if course:
			papers.append({"doctype": DOCTYPE, "docname": course})
		papers.append({"doctype": "Training Type", "docname": str(row.get("training_type") or "")})
		out["cards"].append(
			{
				"session": row["name"],
				"course": course,
				"training_type": row.get("training_type"),
				"phase": phase,
				"title": title,
				"subtitle": " · ".join(part for part in (words["time"], words["location"]) if part),
				"session_date": str(row.get("session_date") or ""),
				"start_time": training_sessions.clock(row.get("start_time")) or None,
				"end_time": training_sessions.clock(row.get("end_time")) or None,
				"location": row.get("location") or None,
				"day_number": fields["day_number"] or 1,
				"day_count": fields["day_count"],
				"can_check_in": can,
				"checked_in": checked_in,
				"check_in_opens_at": str(opens) if opens else None,
				"check_in_note": note or None,
				"papers": papers,
			}
		)
	return out


class CheckInError(Exception):
	def __init__(self, message: str, kind: str = "invalid"):
		super().__init__(message)
		self.kind = kind


def check_in(session: str, user: str, employee: str, latitude=None, longitude=None, accuracy=None) -> dict:
	"""The caller marks their own row present on a day they are registered for."""
	if not ready():
		raise CheckInError("this farm's server has no multi-day classes yet.")
	if not employee:
		raise CheckInError(
			"this account is not linked to an Employee, so nobody can be checked in.", "forbidden"
		)
	if not session or not frappe.db.exists(DOCTYPE, session):
		raise CheckInError(f"no class called {session!r}.", "not_found")
	row = _row(session)
	if is_course(session):
		raise CheckInError(
			f"{session} is a multi-day course; check in to the day you are at. Nothing was changed."
		)
	doc = frappe.get_doc(DOCTYPE, session)
	index = next(
		(i for i, r in enumerate(doc.get("attendees") or []) if str(r.get("employee") or "") == employee), -1
	)
	if index < 0:
		# Not found rather than forbidden: a class somebody is not on is not theirs to learn about.
		raise CheckInError(f"no class called {session!r}.", "not_found")
	mine = doc.attendees[index]
	if compat.checked(mine.get("attended")) and str(mine.get("scanned_at") or ""):
		return {"session": session, "already": True, "checked_in_at": str(mine.get("scanned_at"))}
	status = row.get("status") or training_sessions.STATUS_SCHEDULED
	if status not in training_sessions.OPEN_STATUSES:
		raise CheckInError(f"{session} is {status}; check-in is closed. Nothing was changed.")
	company = str(row.get("company") or "")
	opens, closes = check_in_window(row, company, user)
	now = _now()
	if opens is None or not opens <= now <= closes:
		when = f"from {opens.strftime('%H:%M')} on {row.get('session_date')}" if opens else "on the day"
		raise CheckInError(f"check-in for {row.get('training_type')} is open {when}. Nothing was changed.")
	mine.attended = 1
	mine.scanned_at = frappe.utils.now()
	mine.scan_source = SELF
	if latitude not in (None, "") and longitude not in (None, ""):
		try:
			mine.scan_latitude, mine.scan_longitude = float(latitude), float(longitude)
			if accuracy not in (None, ""):
				mine.scan_accuracy_meters = float(accuracy)
			from . import geo

			mine.scan_h3_cell = geo.point_cell(mine.scan_latitude, mine.scan_longitude)
		except (TypeError, ValueError):
			pass
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	fields = course_fields(_row(session), employee)
	return {
		"session": session,
		"already": False,
		"checked_in_at": str(mine.scanned_at),
		"day_number": fields["day_number"] or 1,
		"day_count": fields["day_count"],
		"note": "You are checked in. Your signature is taken at the end of the class.",
	}


def is_attendee(session: str, employee: str) -> bool:
	"""Whether this person is on the session, its course, or one of its days."""
	if not session or not employee or not compat.doctype_exists(ATTENDEE):
		return False
	names = {session}
	if ready():
		parent = str(frappe.db.get_value(DOCTYPE, session, "parent_session") or "")
		if parent:
			names.add(parent)
		names.update(day["name"] for day in days_of(session))
	return bool(
		frappe.db.get_all(
			ATTENDEE,
			filters={"parent": ("in", sorted(names)), "employee": employee},
			fields=["name"],
			limit=1,
		)
	)


# ── the evening before ──────────────────────────────────────────────────────
def reminder_payload(row: dict, fields: dict, language: str, company: str) -> dict:
	from .services import push

	words = _words(row, fields, language, company, "")
	return {
		"aps": {
			"alert": {
				"title": _fill(text("training_reminder_title", language, company), words),
				"body": _fill(text("training_reminder_body", language, company), words)[: push.MAX_BODY],
			},
			"sound": push.SOUND_DEFAULT,
			"interruption-level": push.INTERRUPTION_ACTIVE,
			"category": push.CATEGORY_TRAINING,
		},
		"training_session": row["name"],
		"course": parent_of(row) or None,
		"phase": "tomorrow",
		"session_date": str(row.get("session_date") or ""),
	}


def send_reminders(now=None) -> dict:
	"""Hourly. At the configured hour, push tomorrow's attendees once. Never raises."""
	report = {"sent": 0, "skipped": 0, "sessions": 0, "reason": ""}
	try:
		if not ready() or not compat.has_field(ATTENDEE, "reminded_on"):
			report["reason"] = "not migrated"
			return report
		moment = now or _now()
		tomorrow = (moment.date() + datetime.timedelta(days=1)).isoformat()
		rows = frappe.db.get_all(
			DOCTYPE,
			filters={"session_date": tomorrow, "status": ("in", list(training_sessions.OPEN_STATUSES))},
			fields=compat.existing_fields(DOCTYPE, training_sessions.FIELDS),
			limit=200,
		)
		from .services import push

		for row in (dict(r) for r in rows):
			if is_course(row["name"]):
				continue
			company = str(row.get("company") or "")
			hour = int(setting("training_reminder_hour", company))
			if hour < 0 or moment.hour != hour:
				continue
			report["sessions"] += 1
			fields = course_fields(row)
			for entry in training_sessions.attendees_of(row["name"]):
				if report["sent"] >= MAX_REMINDERS_PER_RUN:
					report["reason"] = "cap reached"
					return report
				employee = str(entry.get("employee") or "")
				if not employee or str(entry.get("reminded_on") or ""):
					report["skipped"] += 1
					continue
				payload = reminder_payload(row, fields, language_of(employee), company)
				result = push.send_push_to_employees(
					[employee], payload, priority="5", collapse_id=f"training-{row['name']}"
				)
				# Marked whether or not a phone took it: one reminder, not one an hour.
				frappe.db.set_value(ATTENDEE, entry["name"], "reminded_on", frappe.utils.now())
				report["sent" if int(result.get("sent") or 0) else "skipped"] += 1
	except Exception as exc:  # pragma: no cover - a reminder must not fail the scheduler
		report["reason"] = f"{type(exc).__name__}: {exc}"
		try:
			frappe.log_error(title="training reminders", message=frappe.get_traceback())
		except Exception:
			pass
	return report


# ── a missed required day ───────────────────────────────────────────────────
def certification_of(employee: str, training_type: str) -> dict:
	"""The Certification this course renews, as this person holds it, or {}."""
	if not employee or not compat.has_field("Training Type", "renews_certification"):
		return {}
	wanted = str(frappe.db.get_value("Training Type", training_type, "renews_certification") or "").strip()
	if not wanted or not compat.doctype_exists("Certification"):
		return {}
	name = str(frappe.db.get_value("Employee", employee, "employee_name") or "")
	holders = [h for h in (employee, name) if h]
	found = []
	for key in ("cert_type", "cert_name"):
		found += frappe.db.get_all(
			"Certification",
			filters={key: wanted, "holder": ("in", holders)},
			fields=["name", "cert_type", "cert_name", "expiration_date", "status", "holder"],
			limit=20,
		)
	live = [dict(c) for c in found if str(c.get("status") or "Active") not in ("Superseded", "Revoked")]
	if not live:
		return {}
	live.sort(key=lambda c: str(c.get("expiration_date") or ""), reverse=True)
	return live[0]


def missed_days(company: str = "") -> list:
	"""One finding per (course, attendee) with a missed required day and no credit.

	Cleared by its own absence: a person who later attends a day of the same
	Training Type after the missed one, or who holds a record from the course, is
	no longer returned."""
	if not ready():
		return []
	filters: dict = {"company": company} if company else {}
	parents = sorted(
		{
			str(row.get("parent_session"))
			for row in frappe.db.get_all(DOCTYPE, filters=filters, fields=["parent_session"], limit=2000)
			if row.get("parent_session")
		}
	)
	findings = []
	for course in parents:
		if not frappe.db.exists(DOCTYPE, course):
			continue
		head = _row(course)
		if head.get("status") == training_sessions.STATUS_CANCELLED:
			continue
		days = {day["name"]: day for day in days_of(course)}
		recorded = {
			str(r.get("employee") or "")
			for r in training_sessions.attendees_of(course)
			if str(r.get("training_record") or "")
		}
		for entry in attendance(course).values():
			if not entry["missed"] or entry["employee"] in recorded:
				continue
			last_missed = max(str(days[d].get("session_date") or "") for d in entry["missed"])
			if _made_up(entry["employee"], str(head.get("training_type") or ""), last_missed, set(days)):
				continue
			findings.append(
				{
					"course": course,
					"company": head.get("company"),
					"training_type": head.get("training_type"),
					"employee": entry["employee"],
					"employee_name": entry["employee_name"],
					"missed": [
						{
							"session": d,
							"day_number": int(days[d].get("day_number") or 0),
							"session_date": str(days[d].get("session_date") or ""),
						}
						for d in entry["missed"]
					],
					"last_missed": last_missed,
					"course_expires": str(head.get("expires_date") or "") or None,
					"certification": certification_of(
						entry["employee"], str(head.get("training_type") or "")
					),
				}
			)
	return findings


def _made_up(employee: str, training_type: str, after: str, own_days: set) -> bool:
	"""Whether they attended another session of this Training Type after the miss."""
	later = frappe.db.get_all(
		DOCTYPE,
		filters={"training_type": training_type, "session_date": (">", after)},
		fields=["name"],
		limit=200,
	)
	names = [str(r["name"]) for r in later if str(r["name"]) not in own_days]
	if not names:
		return False
	rows = training_sessions.attendees_for_parents(names)
	return any(
		str(r.get("employee") or "") == employee and compat.checked(r.get("attended"))
		for group in rows.values()
		for r in group
	)
