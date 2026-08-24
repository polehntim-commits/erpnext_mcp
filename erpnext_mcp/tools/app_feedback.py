# SPDX-License-Identifier: MIT
"""Filing one note a worker wrote from inside the handset app. v0.105.0.

ONE FUNCTION, AND IT IS NOT A CATALOGUE TOOL. `submit_app_feedback` is reached
only from the mobile surface, for the reason `register_push_token` is: a phone
files its OWN note under its own login, and there is no version of "file
feedback as somebody else" that is a thing a model should be able to do. The
owner's half of the feature is a LIST VIEW on the doctype — see the field
descriptions in `app_feedback.json` — rather than a read tool, because what an
owner wants is to sort by recency and filter by screen and role, and a Desk list
already does exactly that against the columns this writes.

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
from ..args import as_bool, as_str
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
