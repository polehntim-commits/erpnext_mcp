# SPDX-License-Identifier: MIT
"""v0.105.0 — SERVER_CHANGES #24, the in-app feedback bubble's one route.

The handset half shipped first: a bubble on every screen, a form that captures
the screen, the person, the role, the time, the build and the model for them,
dictation in English and Spanish for a crew working in gloves, and an optional
screenshot. Every note it has produced since is sitting on a phone, because
`submit_app_feedback` was a 404 — and a 404 on this client PARKS a note rather
than failing it, retried every six hours forever. These are the six claims the
server half rests on.

1. **THE FIRST 200 DRAINS A BACKLOG, NOT A NOTE, AND MAY DRAIN IT TWICE.**
   `TheBacklogDrainsOnce`. `entry_uuid` is unique on the doctype and a resend is
   answered with SUCCESS and the record already held — never a refusal, because
   the app treats any non-2xx as "not filed" and would queue it again. One
   worker's considered complaint filed three times is how a feed becomes noise
   nobody reads, and the interrupted drain is the case that happens most.

2. **THE LOGIN ON A NOTE IS THE ONE THAT WAS PROVED.** `AClaimIsStoredAsAClaim`.
   The app sends `user` and `employee` on every call because a shared handset is
   normal here; `bind` drops `user` outright and the claimed employee is kept in
   `claimed_employee` — only where it disagrees, since two identical columns on
   every row would hide the disagreement that is the point of keeping it.

3. **A BAD SCREENSHOT NEVER COSTS THE NOTE.** `TheNoteSurvivesItsScreenshot`.
   Not base64, not an image, over the ceiling, or a File insert that threw: all
   four record a reason and file the note. A 400 would be a note re-queued and
   re-sent forever by a handset that will never encode that JPEG any smaller.

4. **THE FORM DOOR AND THE JSON DOOR REACH THE SAME METHOD.**
   `MultipartArrivesAsOneBody`. A `multipart/form-data` part is base64'd in the
   transport and lands on the key its own part is named, so no handler branches
   on how the bytes arrived and `bind` still reduces the result to the
   signature.

5. **THE OWNER'S HALF IS A LIST VIEW, AND IT IS A PROPERTY OF THE DOCTYPE.**
   `TheOwnerReadsAFeed`. Sorted by when Send was pressed rather than when the
   note landed — weeks apart on a farm with no signal in the blocks — and
   filterable by screen and by role, which are the two columns SERVER_CHANGES
   asks for by name.

6. **A NOTE IS NEVER REFUSED FOR ANYTHING BUT BEING EMPTY.**
   `TheOnlyTwoRefusals`. No `entry_uuid` and no `comment` are the whole list. A
   company this login cannot reach falls back rather than 403-ing, because
   `company` here is a filter column on a feedback feed and a 403 is a note that
   never lands.

7. **THE STAMP AN IPHONE SENDS IS NOT ONE A `Datetime` COLUMN TAKES.**
   `AnInstantOffAnIPhone`. Added after the route went live and every single note
   500'd on `OperationalError (1292)` — the third time this boundary has cost a
   register, after `training_completed_at` and the bucket capture queue, and the
   reason `datetimes.as_mariadb_datetime` is a pure module of its own. The
   fixture below is the iOS spelling now; it used to be the MariaDB one, which
   is the whole reason claims 1-6 passed while nothing on the farm could land.
"""

import base64
import json
import unittest
from pathlib import Path
from unittest import mock

import frappe

from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.farmops_api import routes as farmops_routes
from erpnext_mcp.tools import app_feedback as feedback_tools

from .fixtures import MAIN
from .harness import STORE
from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER, WORKER_EMPLOYEE
from .test_farmops_api import PREFIX, FarmOpsAPITestCase

APP_FEEDBACK = "App Feedback"
FEEDBACK = f"{PREFIX}/mobile/submit_app_feedback"

#: A one-pixel JPEG's opening bytes plus filler — enough that `_sniff` reads it
#: as a JPEG, which is the only thing this route asks of a screenshot.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

UUID = "6F1B2C3D-4E5F-4A6B-8C9D-0E1F2A3B4C5D"


#: What the handset stamps `submitted_at` with, verbatim: `AppFeedback`
#: builds it with an `ISO8601DateFormatter` in UTC. **THE FIXTURE USED TO SAY
#: `2026-08-01 06:42:11` AND THAT IS WHY THIS SUITE PASSED WHILE NO NOTE ON THE
#: FARM COULD LAND** — a MariaDB-shaped string no iPhone has ever sent, checked
#: against a store that does not mind either shape. See `AnInstantOffAnIPhone`.
SENT_AT = "2026-08-01T06:42:11Z"

#: The same instant as a `Datetime` column will take it, which is what must be
#: on the row afterwards.
STORED_AT = "2026-08-01 06:42:11"


def a_note(**overrides) -> dict:
	"""What `AppFeedback.requestParams` posts, with the keys it always sends."""
	body = {
		"entry_uuid": UUID,
		"client_reference": UUID,
		"screen": "harvest_day",
		"screen_label": "Harvest Day",
		"comment": "The bucket count resets when I lock the phone.",
		"was_dictated": True,
		"submitted_at": SENT_AT,
		"app_version": "1.9.0",
		"app_build": "412",
		"device_model": "iPhone17,2",
		"os_version": "18.4",
		"device_id": "E7F1-AAAA",
		"language": "es",
		"role": "Picker",
	}
	body.update(overrides)
	return body


class AppFeedbackTestCase(FarmOpsAPITestCase):
	def rows(self):
		return STORE.rows(APP_FEEDBACK)

	def only(self) -> dict:
		rows = self.rows()
		self.assertEqual(len(rows), 1, rows)
		return rows[0]

	def file(self, body=None, **kwargs):
		return self.message(FEEDBACK, a_note(**(body or {})), **kwargs)


# ── 1. the backlog ──────────────────────────────────────────────────────────
class TheBacklogDrainsOnce(AppFeedbackTestCase):
	def test_a_note_is_filed_with_what_the_handset_captured(self):
		answer = self.file()
		self.assertTrue(answer["filed"])
		self.assertTrue(answer["created"])
		self.assertFalse(answer["duplicate"])

		row = self.only()
		self.assertEqual(row["entry_uuid"], UUID)
		self.assertEqual(row["screen_name"], "harvest_day")
		self.assertEqual(row["screen_label"], "Harvest Day")
		self.assertEqual(row["feedback_text"], "The bucket count resets when I lock the phone.")
		self.assertEqual(row["language"], "es")
		self.assertEqual(int(row["was_dictated"]), 1)
		self.assertEqual(row["role"], "Picker")
		self.assertEqual(row["app_version"], "1.9.0")
		self.assertEqual(row["device_model"], "iPhone17,2")
		self.assertEqual(row["os_version"], "18.4")

	def test_the_same_uuid_twice_files_one_note_and_answers_success(self):
		"""THE CLAIM THE WHOLE FEATURE RESTS ON. An interrupted drain replays
		from the start, and a refusal on the replay is indistinguishable — from
		the handset — from never having built this route at all."""
		first = self.file()
		second = self.file()

		self.assertEqual(len(self.rows()), 1)
		self.assertTrue(second["filed"])
		self.assertFalse(second["created"])
		self.assertTrue(second["duplicate"])
		self.assertEqual(second["name"], first["name"])

	def test_a_resend_does_not_overwrite_what_was_filed(self):
		"""A replay carries the same UUID and may carry a differently-truncated
		body. The note already held is the one the worker wrote."""
		self.file()
		self.file({"comment": "…the phone.", "screen": "my_tasks"})

		row = self.only()
		self.assertEqual(row["feedback_text"], "The bucket count resets when I lock the phone.")
		self.assertEqual(row["screen_name"], "harvest_day")

	def test_client_reference_alone_is_accepted_as_the_key(self):
		"""The app sends the UUID under both spellings. Either one is the key."""
		body = a_note()
		body.pop("entry_uuid")
		self.assertEqual(self.message(FEEDBACK, body)["entry_uuid"], UUID)

		body = a_note()
		body.pop("entry_uuid")
		self.assertTrue(self.message(FEEDBACK, body)["duplicate"])
		self.assertEqual(len(self.rows()), 1)

	def test_two_different_notes_are_two_records(self):
		"""The dedup must not be so keen that it swallows a second complaint."""
		self.file()
		self.file({"entry_uuid": "AAAABBBB-1111-2222-3333-444455556666", "client_reference": None})
		self.assertEqual(len(self.rows()), 2)

	def test_the_doctype_declares_entry_uuid_unique(self):
		"""The read-then-write above loses a race; the index is what does not.
		Asserted on the shipped JSON, because that is what `bench migrate`
		builds the constraint from."""
		path = (
			Path(__file__).resolve().parent.parent
			/ "erpnext_mcp"
			/ "erpnext_mcp"
			/ "doctype"
			/ "app_feedback"
			/ "app_feedback.json"
		)
		fields = {f["fieldname"]: f for f in json.loads(path.read_text())["fields"]}
		self.assertEqual(fields["entry_uuid"].get("unique"), 1)
		self.assertEqual(fields["entry_uuid"].get("reqd"), 1)


# ── 2. whose note it is ─────────────────────────────────────────────────────
class AClaimIsStoredAsAClaim(AppFeedbackTestCase):
	def test_the_record_carries_the_authenticated_caller(self):
		self.file()
		row = self.only()
		self.assertEqual(row["user"], WORKER)
		self.assertEqual(row["employee"], WORKER_EMPLOYEE)
		self.assertEqual(row["employee_name"], "Ana Ramos")

	def test_a_body_cannot_file_a_note_under_another_login(self):
		"""`user` is not on the signature, so `bind` drops it before the call.
		Asserted by INSPECTING the signature as well as by calling, because the
		absence is the security property and a later edit could restore it."""
		self.assertNotIn("user", farmops_routes.accepted_arguments(mobile_api.submit_app_feedback))

		self.file({"user": "someone.else@example.test"})
		self.assertEqual(self.only()["user"], WORKER)

	def test_a_shared_handset_keeps_both_names(self):
		self.file({"employee": OUTSIDER_EMPLOYEE, "employee_name": "Ben Ortiz"})
		row = self.only()
		self.assertEqual(row["employee"], WORKER_EMPLOYEE)
		self.assertEqual(row["claimed_employee"], OUTSIDER_EMPLOYEE)
		self.assertEqual(row["claimed_employee_name"], "Ben Ortiz")

	def test_an_agreeing_claim_is_not_stored_twice(self):
		"""Two identical columns on every row would hide the disagreement that
		is the only reason the claim is kept."""
		self.file({"employee": WORKER_EMPLOYEE, "employee_name": "Ana Ramos"})
		row = self.only()
		self.assertFalse(row.get("claimed_employee"))
		self.assertFalse(row.get("claimed_employee_name"))

	def test_a_login_with_no_employee_record_still_files_a_note(self):
		"""Somebody whose Employee row has not been linked yet is exactly the
		person whose feedback about being onboarded is worth reading."""
		with mock.patch.object(mobile_api, "_employee", side_effect=frappe.ValidationError("no row")):
			self.file()
		row = self.only()
		self.assertEqual(row["user"], WORKER)
		self.assertFalse(row.get("employee"))

	def test_the_roles_array_fills_the_filter_when_no_active_role_is_sent(self):
		body = a_note(roles=["Field Worker", "Crew Leader"])
		body.pop("role")
		self.message(FEEDBACK, body)
		self.assertEqual(self.only()["role"], "Field Worker, Crew Leader")

	def test_an_active_role_wins_over_the_array(self):
		self.file({"roles": ["Field Worker", "Crew Leader"]})
		self.assertEqual(self.only()["role"], "Picker")


# ── 3. the screenshot ───────────────────────────────────────────────────────
class TheNoteSurvivesItsScreenshot(AppFeedbackTestCase):
	def stored_files(self):
		return [row for row in STORE.rows("File") if row.get("attached_to_doctype") == APP_FEEDBACK]

	def test_a_capture_is_stored_private_and_flagged(self):
		answer = self.file({"screenshot": base64.b64encode(JPEG).decode("ascii")})
		self.assertTrue(answer["screenshot_stored"])

		row = self.only()
		self.assertEqual(int(row["has_screenshot"]), 1)
		self.assertTrue(row["screenshot"])

		files = self.stored_files()
		self.assertEqual(len(files), 1)
		self.assertEqual(int(files[0]["is_private"]), 1)
		self.assertTrue(str(files[0]["file_name"]).endswith(".jpg"))
		self.assertEqual(files[0]["attached_to_name"], row["name"])

	def test_a_data_prefix_is_stripped_rather_than_refused(self):
		payload = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
		self.assertTrue(self.file({"screenshot": payload})["screenshot_stored"])
		self.assertTrue(str(self.stored_files()[0]["file_name"]).endswith(".png"))

	def test_the_extension_comes_off_the_bytes_and_not_off_a_name(self):
		"""A caller-supplied filename has nowhere to land: it is not on the
		signature, and the stored name is composed from the docname."""
		self.assertNotIn(
			"screenshot_filename", farmops_routes.accepted_arguments(mobile_api.submit_app_feedback)
		)
		self.file({"screenshot": base64.b64encode(PNG).decode("ascii")})
		name = str(self.stored_files()[0]["file_name"])
		self.assertTrue(name.startswith(self.only()["name"]), name)
		self.assertTrue(name.endswith(".png"), name)

	def test_something_that_is_not_base64_files_the_note_anyway(self):
		answer = self.file({"screenshot": "not base64 at all !!!"})
		self.assertTrue(answer["filed"])
		self.assertEqual(answer["screenshot_omitted"], "not_base64")
		row = self.only()
		self.assertEqual(int(row["has_screenshot"]), 0)
		self.assertEqual(row["screenshot_omitted"], "not_base64")

	def test_something_that_is_not_an_image_files_the_note_anyway(self):
		answer = self.file({"screenshot": base64.b64encode(b"<html>hello</html>").decode("ascii")})
		self.assertTrue(answer["filed"])
		self.assertEqual(answer["screenshot_omitted"], "not_an_image")
		self.assertFalse(self.stored_files())

	def test_a_capture_over_the_ceiling_files_the_note_anyway(self):
		oversized = JPEG + b"\x00" * (feedback_tools.SCREENSHOT_MAX_BYTES + 1)
		answer = self.file({"screenshot": base64.b64encode(oversized).decode("ascii")})
		self.assertTrue(answer["filed"])
		self.assertEqual(answer["screenshot_omitted"], "too_large")

	def test_the_handsets_own_reason_is_kept_when_it_dropped_the_capture(self):
		"""The app drops a JPEG over its inline ceiling and says so instead of
		holding the note back. 'The worker took no screenshot' and 'the
		screenshot did not fit' are different facts and only one is a bug."""
		answer = self.file({"screenshot_omitted": "too_large"})
		self.assertTrue(answer["filed"])
		self.assertEqual(self.only()["screenshot_omitted"], "too_large")

	def test_a_note_with_no_screenshot_carries_no_reason(self):
		self.file()
		row = self.only()
		self.assertEqual(int(row["has_screenshot"]), 0)
		self.assertFalse(row.get("screenshot_omitted"))

	def test_a_file_insert_that_throws_does_not_cost_the_note(self):
		with mock.patch.object(
			feedback_tools.artifacts, "attach_bytes", side_effect=RuntimeError("disk full")
		):
			answer = self.file({"screenshot": base64.b64encode(JPEG).decode("ascii")})
		self.assertTrue(answer["filed"])
		self.assertTrue(answer["created"])
		self.assertEqual(self.only()["screenshot_omitted"], "store_failed")

	def test_the_flag_is_the_controllers_and_never_a_callers(self):
		"""A client that ticked the box without sending a picture would put a
		note in the 'has a screenshot' filter that has none."""
		self.file({"has_screenshot": 1})
		self.assertEqual(int(self.only()["has_screenshot"]), 0)


# ── 4. both doors ───────────────────────────────────────────────────────────
class MultipartArrivesAsOneBody(AppFeedbackTestCase):
	def post_form(self, fields, files=None):
		data = dict(fields)
		for key, (filename, content, content_type) in (files or {}).items():
			data[key] = (__import__("io").BytesIO(content), filename, content_type)
		pair = self.credential
		frappe.local.session = frappe._dict(user="Guest", data=frappe._dict())
		from erpnext_mcp.api import fallback_auth

		return self.client.open(
			FEEDBACK,
			method="POST",
			data=data,
			content_type="multipart/form-data",
			headers={fallback_auth.HEADER: f"{pair['api_key']}:{pair['api_secret']}"},
			environ_base={"REMOTE_ADDR": "100.64.0.7"},
		)

	def test_a_form_post_files_a_note_with_its_attached_capture(self):
		response = self.post_form(
			{
				"entry_uuid": UUID,
				"screen": "bucket_capture",
				"comment": "The shutter fires before the bucket is in frame.",
				"submitted_at": "2026-08-02T07:10:00Z",
				"role": "Checker",
			},
			files={"screenshot": ("shot.jpg", JPEG, "image/jpeg")},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		answer = json.loads(response.get_data(as_text=True))["message"]
		self.assertTrue(answer["created"])
		self.assertTrue(answer["screenshot_stored"])

		row = self.only()
		self.assertEqual(row["screen_name"], "bucket_capture")
		self.assertEqual(row["role"], "Checker")
		self.assertEqual(int(row["has_screenshot"]), 1)

	def test_a_form_post_dedupes_against_a_json_one(self):
		"""The two doors reach the same method, so they share the one key."""
		self.file()
		response = self.post_form({"entry_uuid": UUID, "comment": "again", "role": "Picker"})
		self.assertEqual(response.status_code, 200)
		self.assertTrue(json.loads(response.get_data(as_text=True))["message"]["duplicate"])
		self.assertEqual(len(self.rows()), 1)

	def test_a_form_part_naming_an_undeclared_argument_is_dropped(self):
		"""`bind` still reduces the body to the signature — the transport reads
		one more encoding, it does not widen what any method accepts."""
		response = self.post_form(
			{"entry_uuid": UUID, "comment": "hello", "user": "someone.else@example.test"}
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.only()["user"], WORKER)

	def test_a_json_body_still_reads_as_json(self):
		"""The multipart branch must not have changed the door every shipped
		handset already comes through."""
		self.assertTrue(self.file()["created"])


# ── 5. the owner's feed ─────────────────────────────────────────────────────
class TheOwnerReadsAFeed(unittest.TestCase):
	"""Item #19's other half: a feed the farm owner reads, sorted by recency and
	filterable by screen and by role. A LIST VIEW rather than a report, which
	makes it a property of the doctype JSON — so that is what this reads."""

	@classmethod
	def setUpClass(cls):
		path = (
			Path(__file__).resolve().parent.parent
			/ "erpnext_mcp"
			/ "erpnext_mcp"
			/ "doctype"
			/ "app_feedback"
			/ "app_feedback.json"
		)
		cls.doctype = json.loads(path.read_text())
		cls.fields = {f["fieldname"]: f for f in cls.doctype["fields"]}

	def test_the_feed_is_sorted_by_when_send_was_pressed(self):
		"""NOT by when it landed. A phone with no signal in the blocks holds a
		note for weeks, and the newest complaint is the one most recently made."""
		self.assertEqual(self.doctype["sort_field"], "timestamp")
		self.assertEqual(self.doctype["sort_order"], "DESC")

	def test_screen_and_role_are_standard_filters(self):
		for fieldname in ("screen_name", "role"):
			with self.subTest(fieldname=fieldname):
				self.assertEqual(self.fields[fieldname].get("in_standard_filter"), 1)

	def test_the_columns_a_reader_scans_are_in_the_list_view(self):
		for fieldname in ("screen_name", "feedback_text", "employee", "role", "timestamp"):
			with self.subTest(fieldname=fieldname):
				self.assertEqual(self.fields[fieldname].get("in_list_view"), 1)

	def test_the_farm_owner_can_read_the_feed_and_cannot_rewrite_it(self):
		"""A note is what a worker said. A feed the reader can edit is not
		evidence of anything."""
		by_role = {row["role"]: row for row in self.doctype["permissions"]}
		self.assertEqual(by_role["Farm Manager"].get("read"), 1)
		self.assertEqual(by_role["Farm Manager"].get("report"), 1)
		self.assertNotIn("write", by_role["Farm Manager"])
		self.assertNotIn("create", by_role["Farm Manager"])
		self.assertNotIn("delete", by_role["Farm Manager"])

	def test_the_screenshot_is_private(self):
		"""A screenshot of the app is a screenshot of whatever roster, wage or
		task list was on the screen. `attach_bytes` stores it private and takes
		no argument that could make it otherwise — asserted end to end in
		`TheNoteSurvivesItsScreenshot`; this is the field it lands on."""
		self.assertEqual(self.fields["screenshot"]["fieldtype"], "Attach Image")
		self.assertEqual(self.fields["screenshot"].get("read_only"), 1)


# ── 6. the only two refusals ────────────────────────────────────────────────
class TheOnlyTwoRefusals(AppFeedbackTestCase):
	def test_no_uuid_is_refused_by_name(self):
		body = a_note()
		body.pop("entry_uuid")
		body.pop("client_reference")
		status, parsed = self.refusal(FEEDBACK, body)
		self.assertEqual(status, 400)
		self.assertIn("entry_uuid", parsed["error"])
		self.assertFalse(self.rows())

	def test_an_empty_note_is_refused_by_name(self):
		status, parsed = self.refusal(FEEDBACK, a_note(comment=""))
		self.assertEqual(status, 400)
		self.assertIn("comment", parsed["error"])
		self.assertFalse(self.rows())

	def test_a_company_this_login_cannot_reach_falls_back_rather_than_refusing(self):
		"""Every other write on this surface 403s here and this one must not: a
		403 is a note re-queued and re-sent by a handset that will keep naming
		the same company forever."""
		answer = self.file({"company": "Somebody Else Orchards"})
		self.assertTrue(answer["created"])
		self.assertEqual(self.only()["company"], MAIN)

	def test_the_tool_refuses_a_note_with_no_uuid_on_its_own_terms(self):
		with self.assertRaises(ToolError) as caught:
			feedback_tools.submit_app_feedback({"comment": "hello"})
		self.assertIn("entry_uuid", str(caught.exception))

	def test_a_note_longer_than_the_column_holds_is_shortened_and_not_refused(self):
		"""THE THIRD REFUSAL THIS ROUTE DELIBERATELY DOES NOT MAKE. A handset
		cannot shorten what it already queued, so a note refused on length is one
		it would keep sending and keep being refused, forever."""
		answer = self.file({"comment": "x" * 9000})
		self.assertTrue(answer["created"])

		stored = self.only()["feedback_text"]
		self.assertEqual(len(stored), feedback_tools.feedback_doctype.MAX_FEEDBACK)
		self.assertTrue(stored.endswith(feedback_tools.TRUNCATION_MARKER))

	def test_the_controller_still_refuses_an_oversized_note_from_a_desk(self):
		"""The cap is a real refusal on the path where somebody can act on it —
		which is what makes the wrapper's truncation above a decision about
		handsets rather than the column having no limit at all."""
		with self.assertRaises(Exception) as caught:
			frappe.get_doc(
				{
					"doctype": APP_FEEDBACK,
					"entry_uuid": "DESK-0001",
					"feedback_text": "x" * 9000,
					"timestamp": "2026-08-01 06:42:11",
				}
			).insert(ignore_permissions=True)
		self.assertIn("maximum", str(caught.exception))


# ── 7. the instant ──────────────────────────────────────────────────────────
class AnInstantOffAnIPhone(AppFeedbackTestCase):
	"""**THE ROUTE WENT LIVE AND EVERY NOTE STILL 500'd, FOR THE THIRD TIME AT
	THIS BOUNDARY.** `AppFeedback` stamps `submitted_at` with an
	`ISO8601DateFormatter` in UTC — `2026-08-01T06:42:11Z` — and this tool put
	that string straight into a `Datetime` column, which MariaDB answers with
	`OperationalError (1292, "Incorrect datetime value")` at the insert. The
	note validated, the screenshot stored, the write died. From the handset it
	looked like nothing at all: a 500 is retried with backoff, so Settings said
	"Waiting to reach the farm" over a backlog that could never drain.

	It is the same failure as `training_completed_at` (v0.59.1) and as the
	bucket capture queue, which is why `datetimes.as_mariadb_datetime` is its
	own pure module. Every JSON producer writing a Frappe `Datetime` goes
	through it.

	**AND THIS SUITE PASSED THROUGHOUT.** `a_note` claimed to be "what
	`AppFeedback.requestParams` posts" and posted `2026-08-01 06:42:11`, a shape
	no iPhone produces, against a store with no opinion about datetime syntax.
	The fixture is the iOS spelling now, so every test above is also a test that
	the conversion happened.
	"""

	def test_the_stamp_the_handset_sends_is_stored_as_the_column_takes_it(self):
		self.file()
		self.assertEqual(self.only()["timestamp"], STORED_AT)

	def test_an_offset_is_applied_rather_than_dropped(self):
		"""One zone in the column for every row. Keeping the wall clock and
		discarding the offset would file two notes two hours apart as the same
		moment, and the feed is sorted on this column."""
		self.file({"submitted_at": "2026-08-01T08:42:11+02:00"})
		self.assertEqual(self.only()["timestamp"], STORED_AT)

	def test_a_stamp_already_in_the_columns_shape_is_left_alone(self):
		"""The Desk and the standalone fixtures both send this one."""
		self.file({"submitted_at": STORED_AT})
		self.assertEqual(self.only()["timestamp"], STORED_AT)

	def test_the_older_spelling_is_converted_too(self):
		"""`timestamp` is the fallback key the wrapper declares beside
		`submitted_at`, and a fallback that skipped the conversion would be the
		same bug behind a second door."""
		body = a_note()
		body.pop("submitted_at")
		body["timestamp"] = SENT_AT
		self.assertTrue(self.message(FEEDBACK, body)["created"])
		self.assertEqual(self.only()["timestamp"], STORED_AT)

	def test_an_unreadable_stamp_costs_the_stamp_and_never_the_note(self):
		"""The house pattern elsewhere is `as_mariadb_datetime(x) or x` — hand
		the raw string on so a validator names the field. Wrong here, for the
		reason the truncation and the screenshot are handled as they are: the
		caller is a queue that re-sends forever, so a refusal it cannot correct
		is a note nobody ever reads."""
		answer = self.file({"submitted_at": "the day after the rain"})
		self.assertTrue(answer["created"])

		row = self.only()
		self.assertEqual(row["feedback_text"], "The bucket count resets when I lock the phone.")
		self.assertTrue(row["timestamp"])
		self.assertNotIn("T", str(row["timestamp"]))

	def test_a_note_with_no_stamp_at_all_still_lands(self):
		body = a_note()
		body.pop("submitted_at")
		self.assertTrue(self.message(FEEDBACK, body)["created"])
		self.assertTrue(self.only()["timestamp"])

	def test_the_docname_carries_the_year_the_note_was_written_in(self):
		"""`autoname` reads `timestamp[:4]`, so a conversion that answered ""
		would file a 2026 note under whatever `today()` said — the one thing
		`AppFeedback.autoname` exists to prevent."""
		self.file()
		self.assertIn("2026", self.only()["name"])
