# SPDX-License-Identifier: MIT
"""The in-app feedback bubble: one write from a handset, two reads for the farm.

THE WRITE IS NOT A CATALOGUE TOOL AND WILL NOT BECOME ONE. `submit_app_feedback`
is reached only from the mobile surface, for the reason `register_push_token` is:
a phone files its OWN note under its own login, and there is no version of "file
feedback as somebody else" that is a thing a model should be able to do.

THE TWO READS ARE, AS OF v0.128.0. `list_app_feedback` and `get_app_feedback`
ship on, like every other read here. v0.105.0 left them out on the grounds that
what an owner wants is to sort by recency and filter by screen and role, and a
Desk list already does exactly that against the columns the write puts down —
see the field descriptions in `app_feedback.json`, which are still the best
account of what each column is for. That reasoning was right about the Desk and
wrong about everyone else: the farmops sidecar does not forward `/api/resource`,
so a client reaching this site over MCP has no Desk, no list view, and had no way
to read a single note. The write half had been collecting a backlog since
v0.105.0 into a table only one transport could see.

────────────────────────────────────────────────────────────────────────────
THE DEDUP IS THE FEATURE, NOT A COURTESY
────────────────────────────────────────────────────────────────────────────

SERVER_CHANGES #24 is explicit about why. The iOS half shipped first and has
been queuing notes locally against a 404 ever since — a 404 PARKS a note rather
than failing it, retried every six hours forever and never counted against the
give-up bound. So the first call that answers 200 does not receive one note: it
receives a backlog that may be weeks deep, drained in one pass, and drained
AGAIN from the start if that pass is interrupted before the phone records the
acknowledgements.

One worker's considered complaint filed three times is how a feed becomes noise
nobody reads, which would waste the whole feature on its first day. So:

  * `entry_uuid` is `unique` on the doctype — the database refuses the second
    write even if two drains race each other past the read below.
  * A repeat is answered with **success and the record already held**, never a
    refusal. The app treats any 2xx as "filed" and would re-queue on anything
    else, so answering a duplicate with a 409 is indistinguishable, from the
    handset, from never having built this at all.

────────────────────────────────────────────────────────────────────────────
THE HANDSET'S IDENTITY CLAIMS ARE STORED AS CLAIMS
────────────────────────────────────────────────────────────────────────────

A shared handset is normal on a farm, so the app sends who it thinks is holding
the phone. `employee`, `employee_name` and `user` on the record are resolved
from the AUTHENTICATED caller and never from the body; the app's own idea is
kept in `claimed_employee` / `claimed_employee_name`, and only where the two
disagree — which is what a shared handset looks like from here.

`role` and `designation` are stored as sent and are not checked against
anything, because they are half of what the owner filters by and the server
cannot reconstruct them: one login holds several roles and only the app knows
which hat was on. Nothing on this site authorises anything off those columns.

────────────────────────────────────────────────────────────────────────────
A BAD SCREENSHOT NEVER COSTS THE NOTE
────────────────────────────────────────────────────────────────────────────

Every screenshot failure — not base64, not an image, over the ceiling, or a File
insert that threw — is recorded in `screenshot_omitted` and the note is filed
anyway. The alternative is a 400, and a 400 is a note re-queued and re-sent
forever by a handset that will never encode that JPEG any smaller. The picture
is context; the sentence somebody wrote is the thing. The app already reasons
the same way at its own end — it drops a capture over its inline ceiling and
sends `screenshot_omitted: "too_large"` rather than holding the note back.
"""

import frappe

from .. import compat, datetimes
from ..args import as_bool, as_date, as_limit, as_str
from ..erpnext_mcp.doctype.app_feedback import app_feedback as feedback_doctype
from ..errors import ToolError
from ..result import ToolResult
from . import artifacts, files

APP_FEEDBACK = "App Feedback"
EMPLOYEE = "Employee"

#: Appended to a note this route had to shorten. The marker is there so a reader
#: is never left wondering whether a sentence stops mid-word because the worker
#: was interrupted or because the server was.
TRUNCATION_MARKER = " […truncated on arrival]"

#: The biggest screenshot this route will store inline. A JPEG of a phone screen
#: is well under this; the handset holds its own captures under 512 KB and drops
#: anything larger before it sends. The ceiling is here so a client bug cannot
#: put a multi-megabyte base64 blob through a JSON body — and it is a REASON
#: RECORDED rather than a refusal, per the module docstring.
SCREENSHOT_MAX_BYTES = 1024 * 1024

#: What a stored capture is allowed to be, decided by the first bytes rather
#: than by a filename the caller chose. `.svg` is absent for the reason
#: `api/files.py` gives: it executes script when served.
_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"))

#: The columns a caller gets back. `screenshot` is the private file_url, which
#: the handset has no use for and an operator does.
_FIELDS = (
	"name",
	"entry_uuid",
	"screen_name",
	"screen_label",
	"feedback_text",
	"language",
	"was_dictated",
	"employee",
	"employee_name",
	"role",
	"designation",
	"user",
	"company",
	"timestamp",
	"received_at",
	"app_version",
	"app_build",
	"device_model",
	"os_version",
	"device_id",
	"has_screenshot",
	"screenshot_omitted",
	"screenshot",
)


def _require() -> None:
	compat.require_doctype(
		APP_FEEDBACK,
		"which ships with erpnext_mcp — run `bench migrate` to create it",
	)


def _existing(entry_uuid: str):
	"""The note this site already holds under that UUID, or None."""
	rows = frappe.db.get_all(APP_FEEDBACK, filters={"entry_uuid": entry_uuid}, fields=["name"], limit=1)
	return rows[0]["name"] if rows else None


def _submitted_at(args: dict) -> str:
	"""When the worker wrote it, as a `Datetime` column will take it.

	**THROUGH `as_mariadb_datetime` BECAUSE THE SENDER IS AN IPHONE, AND THIS
	IS THE THIRD TIME THAT BOUNDARY HAS COST A REGISTER.** `AppFeedback` stamps
	every note with an `ISO8601DateFormatter` in UTC — `2026-08-24T22:14:40Z` —
	and MariaDB answers a DATETIME column set to that string with
	`OperationalError (1292, "Incorrect datetime value")` at the insert. Same
	shape, same 1292, same silence as the model registry's
	`training_completed_at` (v0.59.1) and the bucket capture queue
	(`api/mobile._bucket_entries`): every field validated, then died on the
	write. Here it surfaced as a 500 per note and a handset repeating "Waiting
	to reach the farm" with a backlog nothing could drain. See `datetimes.py`,
	which exists precisely so this boundary has one answer.

	**AN UNREADABLE STAMP FALLS BACK TO NOW RATHER THAN LOSING THE NOTE.** The
	house pattern elsewhere is `as_mariadb_datetime(x) or x` — hand the raw
	string on so a validator can name the field — and it is wrong here for the
	reason the truncation and the screenshot are handled the way they are: this
	route's caller is a queue that re-sends forever, so a refusal it cannot
	correct is a note nobody ever reads. Arrival time is a worse answer than the
	instant somebody typed and a far better one than no note at all, and it is
	already what the controller substitutes for a blank.
	"""
	sent = as_str(args, "submitted_at") or as_str(args, "timestamp")
	return datetimes.as_mariadb_datetime(sent) or frappe.utils.now()


def _row(name: str) -> dict:
	rows = frappe.db.get_all(APP_FEEDBACK, filters={"name": name}, fields=list(_FIELDS), limit=1)
	return rows[0] if rows else {"name": name}


def _sniff(content: bytes) -> str:
	"""`.png` or `.jpg` off the magic bytes. Empty string for anything else.

	Returns rather than raises, because every screenshot failure on this route is
	a reason recorded beside a filed note rather than a refusal — see the module
	docstring for why a refusal here would park somebody's complaint forever.
	"""
	for magic, extension in _MAGIC:
		if content[: len(magic)] == magic:
			return extension
	return ""


def _screenshot_bytes(args: dict) -> tuple[bytes, str, str]:
	"""(content, extension, reason_it_was_dropped). At most one of the first two
	and the third is ever meaningful."""
	raw = as_str(args, "screenshot")
	if not raw:
		# The handset says so itself when it drops one at its end. A note that
		# never had a screenshot carries neither the bytes nor a reason.
		return b"", "", as_str(args, "screenshot_omitted")

	# THE PREFIX IS STRIPPED RATHER THAN REFUSED, for the reason `signatures.py`
	# gives: a canvas hands back `data:image/jpeg;base64,…` and a phone hands
	# back the tail of it, and they are the same picture.
	if raw.startswith("data:"):
		_, _, raw = raw.partition(",")

	try:
		content = files.decode_base64_content(raw, tail="The note itself was filed.")
	except ToolError:
		return b"", "", "not_base64"
	if len(content) > SCREENSHOT_MAX_BYTES:
		return b"", "", "too_large"
	extension = _sniff(content)
	if not extension:
		return b"", "", "not_an_image"
	return content, extension, ""


def submit_app_feedback(args: dict) -> ToolResult:
	"""File one note from the in-app feedback bubble. Deduplicated on entry_uuid.

	`user`, `employee` and `employee_name` are NOT taken from the body: the
	mobile wrapper resolves all three from the caller's own login and passes them
	in under `caller_user` and `caller_employee`, so a body cannot file a note
	against a colleague's name. What the handset claimed arrives as
	`claimed_employee` and is stored only where it differs.

	RESENDING A NOTE THE SITE ALREADY HAS IS A SUCCESS, not a refusal — see the
	module docstring. `created` in the answer says which of the two happened.
	"""
	_require()

	entry_uuid = as_str(args, "entry_uuid") or as_str(args, "client_reference")
	if not entry_uuid:
		raise ToolError(
			"entry_uuid is required. It is the key that stops a queued note being filed twice "
			"when the handset drains a backlog, and a note without one cannot be deduplicated "
			"by anything. Nothing was filed."
		)

	held = _existing(entry_uuid)
	if held:
		return ToolResult(
			data={"app_feedback": _row(held), "created": False, "duplicate": True},
			summary=f"app feedback {entry_uuid} was already filed as {held} — nothing was written",
		)

	feedback_text = as_str(args, "comment") or as_str(args, "feedback_text")
	if not feedback_text:
		raise ToolError(
			"comment is required — it is the only thing the worker typed, and a note with "
			"everything captured for it and nothing said in it is not feedback. Nothing was filed."
		)

	# TRUNCATED RATHER THAN REFUSED, which is the same rule the screenshot
	# follows and for the same reason. The controller's cap is a real refusal
	# because a Desk edit can be corrected by whoever is making it; a handset
	# cannot, and a note refused on length is one this app would keep sending and
	# keep being refused, forever. Nobody types eight thousand characters in a
	# row of trees, so what reaches this branch is a client bug — and the right
	# answer to a client bug is the worker's first eight thousand characters,
	# marked as shortened, not silence.
	ceiling = feedback_doctype.MAX_FEEDBACK
	if len(feedback_text) > ceiling:
		feedback_text = feedback_text[: ceiling - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER

	caller_employee = as_str(args, "caller_employee")
	claimed_employee = as_str(args, "employee")
	values = {
		"doctype": APP_FEEDBACK,
		"entry_uuid": entry_uuid,
		"screen_name": as_str(args, "screen") or as_str(args, "screen_name"),
		"screen_label": as_str(args, "screen_label"),
		"feedback_text": feedback_text,
		"language": as_str(args, "language") or "en",
		"was_dictated": 1 if as_bool(args, "was_dictated", default=False) else 0,
		"user": as_str(args, "caller_user") or None,
		"employee": caller_employee or None,
		"role": as_str(args, "role"),
		"designation": as_str(args, "designation"),
		"company": as_str(args, "company") or None,
		"timestamp": _submitted_at(args),
		"received_at": frappe.utils.now(),
		"app_version": as_str(args, "app_version"),
		"app_build": as_str(args, "app_build"),
		"device_model": as_str(args, "device_model"),
		"os_version": as_str(args, "os_version"),
		"device_id": as_str(args, "device_id"),
	}
	if claimed_employee and claimed_employee != caller_employee:
		values["claimed_employee"] = claimed_employee
		values["claimed_employee_name"] = as_str(args, "employee_name")

	content, extension, omitted = _screenshot_bytes(args)
	if omitted:
		values["screenshot_omitted"] = omitted

	doc = frappe.get_doc(values)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	stored = False
	if content:
		try:
			artifacts.attach_bytes(
				APP_FEEDBACK,
				doc.name,
				f"{doc.name}-screenshot{extension}",
				content,
				field="screenshot",
			)
			# `attach_bytes` writes the column with `db.set_value`, which does
			# not run the controller — so `has_screenshot` is set the same way
			# rather than left for a save that is not going to happen.
			frappe.db.set_value(APP_FEEDBACK, doc.name, "has_screenshot", 1)
			stored = True
		except Exception:
			# THE NOTE IS ALREADY FILED AND STAYS FILED. See the module
			# docstring: a File insert that threw must not cost the sentence
			# somebody wrote, and the reason is recorded where a reader can see
			# that a picture was meant to be here.
			frappe.log_error(title="app feedback screenshot", message=frappe.get_traceback())
			frappe.db.set_value(APP_FEEDBACK, doc.name, "screenshot_omitted", "store_failed")
			omitted = "store_failed"

	person = as_str(args, "caller_user") or caller_employee or "an unidentified caller"
	screen = values["screen_name"] or "an unnamed screen"
	return ToolResult(
		data={
			"app_feedback": _row(doc.name),
			"created": True,
			"duplicate": False,
			"screenshot_stored": stored,
			"screenshot_omitted": omitted or None,
		},
		summary=(
			f"filed app feedback {doc.name} from {person} on {screen}"
			+ (" with a screenshot" if stored else "")
			+ (f" (screenshot dropped: {omitted})" if omitted else "")
		),
		docstatus_delta="none → 0 (draft)",
	)


# ════════════════════════════════════════════════════════════════════════════
# THE OWNER'S TWO READS, ADDED IN v0.128.0
# ════════════════════════════════════════════════════════════════════════════
#
# THE MODULE DOCSTRING SAID A DESK LIST VIEW WAS THE OWNER'S HALF, AND IT IS
# STILL RIGHT ABOUT WHAT AN OWNER WANTS. Sorting by recency and filtering by
# screen and role is exactly what a Desk list does, and nothing below replaces
# it. What it was wrong about is who else reads this register: the farmops
# sidecar does not forward `/api/resource`, so a client that reaches this site
# over MCP has no Desk, no list view and — until now — no way to read a single
# one of these notes. The write half has been collecting a backlog since
# v0.105.0 into a table only one transport could see.
#
# BOTH READ TOOLS SHIP ON, like every other read here.

#: The most rows either read will return in one call, before `limit` is applied.
#: Same ceiling the other registers use.
REGISTER_CAP = 500

#: What the feed shows. `screenshot` — the private file_url — is deliberately
#: NOT here: a list of forty notes does not need forty file paths, and
#: `has_screenshot` is the column somebody filters on. `get_app_feedback` has it.
_LIST_FIELDS = (
	"name",
	"entry_uuid",
	"screen_name",
	"screen_label",
	"feedback_text",
	"language",
	"was_dictated",
	"employee",
	"employee_name",
	"role",
	"designation",
	"user",
	"company",
	"timestamp",
	"received_at",
	"app_version",
	"app_build",
	"device_model",
	"os_version",
	"device_id",
	"has_screenshot",
	"screenshot_omitted",
	"creation",
)

#: One note in full. The two `claimed_*` columns are here and not in the list
#: for the reason they are only written when they disagree: a shared handset is
#: a fact about ONE note, and a column that is empty on almost every row is
#: noise in a feed and evidence on a record.
_DETAIL_FIELDS = (
	*_LIST_FIELDS,
	"claimed_employee",
	"claimed_employee_name",
	"screenshot",
	"modified",
	"owner",
)

#: Which stamp a date range means. NOT INTERCHANGEABLE, and the whole reason
#: this doctype carries two: `timestamp` is when somebody pressed Send and
#: `received_at` is when it reached the farm, which on a block with no signal
#: may be weeks later. "Every complaint written during cherry harvest" is the
#: first; "everything that landed while I was away" is the second.
_DATE_BASIS = {"submitted": "timestamp", "received": "received_at"}


#: (argument, column) for the filters that are a string match on one column.
#: `screen` is the argument and `screen_name` is the column because the app's own
#: word for the thing is "screen" — the column is named for the doctype and the
#: argument for the person calling.
_TEXT_FILTERS = (
	("screen", "screen_name"),
	("role", "role"),
	("language", "language"),
	("app_version", "app_version"),
	("device_model", "device_model"),
	("device_id", "device_id"),
	("entry_uuid", "entry_uuid"),
)

#: The two Check columns, filtered on only when the caller says either way —
#: `has_screenshot=false` is a real question and is not the same as not asking.
_FLAG_FILTERS = (("has_screenshot", "has_screenshot"), ("was_dictated", "was_dictated"))

#: EVERY ARGUMENT `list_app_feedback` READS, so a test can check the registry
#: declares all of them without a hand-copied list going stale beside this one.
#: `additionalProperties` is advertised on every schema in this app and enforced
#: by nothing, so an argument the schema omits is not refused — it is silently
#: ignored, and no caller ever learns it exists.
LIST_ARGUMENTS = (
	"submitted_by",
	*(key for key, _ in _TEXT_FILTERS),
	*(key for key, _ in _FLAG_FILTERS),
	"from_date",
	"to_date",
	"date_basis",
	"company",
	"limit",
)

#: The same for `get_app_feedback`. Neither is `required` in the schema: one of
#: the two must be given and either will do, which `required` cannot express.
GET_ARGUMENTS = ("name", "entry_uuid")


def _stamp(value) -> str | None:
	return str(value or "") or None


def _queued_days(row: dict) -> float | None:
	"""How long the note sat in a pocket, or None if it cannot be worked out.

	The distance between the two stamps is the fact the doctype keeps two of
	them for, and it is the answer to "why did nobody act on this" — a note
	written in a block with no signal is not a note anybody ignored.
	"""
	written, landed = row.get("timestamp"), row.get("received_at")
	if not written or not landed:
		return None
	try:
		return round(frappe.utils.time_diff_in_seconds(landed, written) / 86400.0, 2)
	except Exception:  # pragma: no cover - an unparseable stamp on an old row
		return None


def _describe(row: dict) -> dict:
	"""One note as a caller sees it: the columns, plus four names for four of them.

	THE ALIASES ARE NOT SPRAWL, THEY ARE THE VOCABULARY THE FEATURE IS DISCUSSED
	IN. `submitted_at` is the LABEL on `timestamp` in the doctype JSON;
	`comment` is what `submit_app_feedback` already accepts the note under, since
	that is what the handset calls it. Answering under only the column name would
	make a caller who read the form, the label or the write tool guess, and the
	cost of carrying both is four keys on a row that already has twenty.

	`device_info` is a COMPOSITION rather than an alias — there is no such
	column, and the five that make it up are only ever read together: the
	question is always "which handset and which build", never one of the five.
	"""
	feedback_text = row.get("feedback_text") or ""
	written = _stamp(row.get("timestamp"))
	return {
		"name": row.get("name"),
		"entry_uuid": row.get("entry_uuid") or None,
		"screen_name": row.get("screen_name") or None,
		"screen_label": row.get("screen_label") or None,
		"feedback_text": feedback_text,
		"comment": feedback_text,
		"language": row.get("language") or None,
		"was_dictated": compat.checked(row.get("was_dictated")),
		# THE PROVED IDENTITY AND THE RESOLVED ONE, BOTH NAMED. `user` is the
		# login the note arrived under and is the only identity on the record
		# that was proved; `employee` is who that login resolved to and is empty
		# for a caller with no Employee row — whose feedback is worth reading
		# precisely because nobody has finished setting them up.
		"user": row.get("user") or None,
		"submitted_by": row.get("user") or None,
		"employee": row.get("employee") or None,
		"employee_name": row.get("employee_name") or None,
		"role": row.get("role") or None,
		"designation": row.get("designation") or None,
		"company": row.get("company") or None,
		"timestamp": written,
		"submitted_at": written,
		"received_at": _stamp(row.get("received_at")),
		"queued_days": _queued_days(row),
		"creation": _stamp(row.get("creation")),
		"app_version": row.get("app_version") or None,
		"device_info": {
			"device_model": row.get("device_model") or None,
			"os_version": row.get("os_version") or None,
			"device_id": row.get("device_id") or None,
			"app_version": row.get("app_version") or None,
			"app_build": row.get("app_build") or None,
		},
		"has_screenshot": compat.checked(row.get("has_screenshot")),
		"screenshot_omitted": row.get("screenshot_omitted") or None,
	}


def _submitted_by_filter(args: dict) -> tuple[str, str]:
	"""(column, value) for the `submitted_by` argument, or ("", "") for absent.

	ONE ARGUMENT, TWO COLUMNS, AND THEY ARE NOT INTERCHANGEABLE. Every note has
	a `user` — the login it arrived under, written from the session — and only
	some have an `employee`, because a login with no Employee row still files
	feedback. So the value is RESOLVED against the two registers rather than
	guessed at, and a value in neither is refused with both named.

	The refusal matters more here than it usually would. An empty feed is a real
	answer on this register — plenty of people have never filed a note — so a
	typo that quietly filtered the wrong column would be indistinguishable from
	the truth, and the reader would conclude somebody had said nothing.
	"""
	value = as_str(args, "submitted_by")
	if not value:
		return "", ""
	if frappe.db.exists("User", value):
		return "user", value
	if frappe.db.exists(EMPLOYEE, value):
		return "employee", value
	raise ToolError(
		f"{value!r} is neither a User nor an Employee on this site, so there is no column "
		"to filter on. `submitted_by` takes the login a note arrived under (the `user` "
		"column, e.g. 'picker@example.com') or the Employee it resolved to (e.g. "
		"'HR-EMP-00007'). Filtering the wrong one would answer an empty feed, and an empty "
		"feed is a real answer here — it must not also be the shape a typo takes."
	)


def _date_window(args: dict, filters: dict) -> tuple[str, str | None, str | None]:
	"""Apply the date range to whichever stamp the caller meant. Returns what it did."""
	basis = (as_str(args, "date_basis") or "submitted").strip().lower()
	if basis not in _DATE_BASIS:
		raise ToolError(
			f"date_basis {basis!r} is not one this register keeps. 'submitted' ranges over "
			"when somebody pressed Send, 'received' over when the note reached the farm — "
			"weeks apart on a block with no signal, which is why there are two."
		)
	column = _DATE_BASIS[basis]
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	# THE DAY IS CLOSED AT 23:59:59 RATHER THAN AT MIDNIGHT. Both columns are
	# Datetime, and `between '2026-08-01' and '2026-08-24'` on a Datetime drops
	# everything filed after midnight on the last day — which is all of it.
	if from_date and to_date:
		filters[column] = ("between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"])
	elif from_date:
		filters[column] = (">=", f"{from_date} 00:00:00")
	elif to_date:
		filters[column] = ("<=", f"{to_date} 23:59:59")
	return basis, from_date, to_date


def list_app_feedback(args: dict) -> ToolResult:
	"""Read-only. The in-app feedback feed, newest first, filtered and counted."""
	_require()

	filters: dict = {}
	column, value = _submitted_by_filter(args)
	if column:
		filters[column] = value
	for key, target in _TEXT_FILTERS:
		given = as_str(args, key)
		if given:
			filters[target] = given
	for key, target in _FLAG_FILTERS:
		flag = as_bool(args, key, None)
		if flag is not None:
			filters[target] = 1 if flag else 0

	# COMPANY IS FILTERED ONLY WHEN THE CALLER NAMED ONE, and is deliberately not
	# put through `resolve_company`. On a single-company site that helper infers
	# the company, and inferring it here would filter the feed to `company =
	# 'Orchard Meadow, LLC'` — which silently drops every note filed WITHOUT a
	# company, and this is the one register where that is the ordinary case: the
	# write tool refuses a note for nothing but being empty, so a handset that
	# never resolved an entity still lands its complaint with the column NULL.
	# An inferred filter would hide exactly the notes from the least-configured
	# phones, which are the ones most worth reading.
	company = as_str(args, "company")
	if company:
		filters["company"] = company

	basis, from_date, to_date = _date_window(args, filters)
	order_column = _DATE_BASIS[basis]
	limit = min(as_limit(args), REGISTER_CAP)

	rows = (
		frappe.db.get_all(
			APP_FEEDBACK,
			filters=filters,
			fields=compat.existing_fields(APP_FEEDBACK, _LIST_FIELDS),
			order_by=f"{order_column} desc, creation desc",
			limit=limit + 1,
		)
		or []
	)
	truncated = len(rows) > limit
	notes = [_describe(dict(row)) for row in rows[:limit]]

	by_screen: dict = {}
	by_role: dict = {}
	for note in notes:
		screen = note["screen_name"] or "(unnamed screen)"
		by_screen[screen] = by_screen.get(screen, 0) + 1
		role = note["role"] or "(no role claimed)"
		by_role[role] = by_role.get(role, 0) + 1

	data = {
		"count": len(notes),
		"limit": limit,
		"truncated": truncated,
		"date_basis": basis,
		"filters": {
			"submitted_by": value or None,
			"submitted_by_column": column or None,
			"screen": filters.get("screen_name"),
			"role": filters.get("role"),
			"language": filters.get("language"),
			"app_version": filters.get("app_version"),
			"device_model": filters.get("device_model"),
			"company": company or None,
			"from_date": from_date,
			"to_date": to_date,
		},
		"by_screen": dict(sorted(by_screen.items())),
		"by_role": dict(sorted(by_role.items())),
		"dictated_count": sum(1 for note in notes if note["was_dictated"]),
		"with_screenshot": sum(1 for note in notes if note["has_screenshot"]),
		"app_feedback": notes,
		"note": (
			"Sorted on when Send was pressed, not on when the note landed — the two are weeks "
			"apart for a phone that spent the week in a block with no signal, and the recency a "
			"reader means is the first. `queued_days` on each row is the distance between them."
		),
	}
	if company:
		unscoped = filters.copy()
		unscoped["company"] = ("is", "not set")
		data["without_company"] = frappe.db.count(APP_FEEDBACK, unscoped)
		if data["without_company"]:
			data["company_note"] = (
				f"{data['without_company']} note(s) matching these filters carry NO company and "
				f"are not in the {len(notes)} above. A note is never refused for failing to name "
				"one, so a handset that had not resolved an entity still filed — call this "
				"without `company` to see them."
			)
	if truncated:
		data["truncated_note"] = (
			f"More than {limit} notes matched. Narrow by screen, role or date rather than "
			"raising the limit — this feed is read to answer a question about one screen or "
			"one week."
		)
	if not notes:
		data["empty_note"] = (
			"No notes match. AN EMPTY FEED IS A REAL ANSWER and is not the same as a broken "
			"one: the handsets drained their backlog into whatever window you did not ask for, "
			"nobody has tapped the bubble on that screen, or the filters name a screen the app "
			"calls something else. Call it with no filters to see whether the register holds "
			"anything at all."
		)

	return ToolResult(
		data=data,
		summary=(
			f"{len(notes)} app feedback note(s)"
			+ (f" from {value}" if value else "")
			+ (f", {data['with_screenshot']} with a screenshot" if notes else "")
		),
	)


def get_app_feedback(args: dict) -> ToolResult:
	"""Read-only. One note in full, by docname or by the handset's own UUID."""
	_require()
	name = as_str(args, "name") or as_str(args, "entry_uuid")
	if not name:
		raise ToolError(
			"name is required — the App Feedback docname (e.g. 'AFB-2026-00042') or the "
			"handset's own entry_uuid, which is unique and is what a phone's logs quote. "
			"list_app_feedback has the register."
		)
	name = name.strip()

	selected = compat.existing_fields(APP_FEEDBACK, _DETAIL_FIELDS)
	row = None
	if frappe.db.exists(APP_FEEDBACK, name):
		row = dict(frappe.db.get_value(APP_FEEDBACK, name, selected, as_dict=True) or {})
	else:
		# THE UUID IS A LEGITIMATE SECOND KEY AND NOT A GUESS. It is `unique` on
		# the doctype, so it resolves to at most one note — and it is the only
		# identifier a handset ever knows, which makes it the one somebody
		# chasing "the app says it filed this and I cannot find it" will have.
		matches = frappe.db.get_all(APP_FEEDBACK, filters={"entry_uuid": name}, fields=selected, limit=2)
		if len(matches) == 1:
			row = dict(matches[0])
	if not row:
		raise ToolError(
			f"no App Feedback called {name!r} on this site, and no note carries it as an "
			"entry_uuid. list_app_feedback has the register."
		)

	data = _describe(row)
	data["claimed_employee"] = row.get("claimed_employee") or None
	data["claimed_employee_name"] = row.get("claimed_employee_name") or None
	data["modified"] = _stamp(row.get("modified"))
	data["owner"] = row.get("owner") or None
	# THE PRIVATE file_url, and private is not a choice a caller made: a
	# screenshot of the app is a screenshot of whatever roster, wage or task list
	# was on the screen. It still needs an authenticated fetch to read.
	data["screenshot"] = row.get("screenshot") or None

	notes = []
	if data["claimed_employee"] and data["claimed_employee"] != data["employee"]:
		notes.append(
			f"THE HANDSET SAID {data['claimed_employee']} AND THE LOGIN RESOLVED TO "
			f"{data['employee'] or '(no Employee record)'}. That is what a shared phone looks "
			"like from here. `employee` is the one that was proved; the claim is kept beside it "
			"rather than instead of it, and neither is corrected into the other."
		)
	if data["screenshot_omitted"]:
		notes.append(
			f"A screenshot was meant to be on this note and is not: {data['screenshot_omitted']}. "
			"The note was filed anyway — the sentence somebody wrote matters more than the "
			"picture, and refusing the submission would have parked the complaint forever."
		)
	if not data["employee"]:
		notes.append(
			"This note has no Employee record behind its login. It is filed and readable, "
			"because a login nobody has finished setting up belongs to exactly the kind of "
			"person whose feedback is worth reading."
		)
	if data["queued_days"] and data["queued_days"] >= 1:
		notes.append(
			f"This note sat on the handset for {data['queued_days']} day(s) before it reached "
			"the farm. That is the phone finding signal, not somebody sitting on a complaint."
		)
	if notes:
		data["reader_notes"] = notes

	who = data["employee_name"] or data["employee"] or data["user"] or "an unidentified caller"
	return ToolResult(
		data=data,
		summary=(
			f"{data['name']}: {who} on {data['screen_name'] or 'an unnamed screen'}"
			f", {data['submitted_at'] or 'undated'}"
			+ (" (with a screenshot)" if data["has_screenshot"] else "")
		),
	)
