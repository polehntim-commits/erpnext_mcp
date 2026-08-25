# SPDX-License-Identifier: MIT
"""v0.128.0 — the two reads over App Feedback, and the six claims they rest on.

`test_app_feedback.py` covers the write: what the handset sends, how a backlog
drains once, and why a bad screenshot never costs the note. This covers the
other direction — the feed the farm reads — and it exists because until v0.128.0
there was not one. The write half had been filing notes since v0.105.0 into a
table only the Desk could see, and the farmops sidecar does not forward
`/api/resource`, so a client reaching this site over MCP could not read a single
one of them.

1. **RECENCY MEANS WHEN SEND WAS PRESSED.** `TheFeedIsSortedOnTheRightStamp`.
   A phone in a block with no signal holds a note for weeks, so `timestamp` and
   `received_at` are far apart and only one of them is the order a reader means.
   `date_basis` switches both the sort and the range to the other one, because
   "what landed while I was away" is a real second question.

2. **THE LAST DAY OF A RANGE IS A DAY, NOT A MIDNIGHT.**
   `TheRangeClosesTheDay`. Both columns are Datetime; a range bounded at
   `2026-08-03` drops everything filed after midnight on the 3rd, which is all
   of it. The negative control is in the test: the same window against a
   midnight bound is asserted to miss the note that the shipped bound finds.

3. **`submitted_by` IS RESOLVED, NEVER GUESSED.** `TheAskerIsResolved`. Every
   note has a `user` and only some have an `employee`, so one argument covers
   two columns — and a value in neither register is REFUSED rather than filtered
   on whichever was tried first. An empty feed is a real answer here, and it
   must not also be the shape a typo takes.

4. **COMPANY IS NEVER INFERRED.** `TheCompanyFilterIsOptIn`. The write refuses a
   note for nothing but being empty, so a handset that never resolved an entity
   still lands its complaint with the column NULL. Inferring a company on a
   single-company site would silently drop exactly those notes.

5. **THE READ ANSWERS IN THE VOCABULARY THE FEATURE IS DISCUSSED IN.**
   `TheAnswerCarriesBothNames`. `submitted_at` is the doctype's own LABEL for
   `timestamp` and `comment` is what the write tool already takes the note
   under. Both names are on the row.

6. **A SHARED HANDSET IS REPORTED, NOT RECONCILED.** `TheDisagreementSurvives`.
   `employee` was proved from the login and `claimed_employee` is what the app
   thought; the read shows both and corrects neither.
"""

import json
import unittest
from pathlib import Path
from typing import ClassVar

import frappe

from erpnext_mcp import registry
from erpnext_mcp.errors import ToolError
from erpnext_mcp.tools import app_feedback

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import STORE

APP_FEEDBACK = "App Feedback"

PICKER_USER = "picker@example.test"
FOREMAN_USER = "foreman@example.test"
PICKER = "HR-EMP-00021"
FOREMAN = "HR-EMP-00022"

ALL_ON = {"allow_list_app_feedback": 1, "allow_get_app_feedback": 1}

#: Three notes. The first was written weeks before it landed, which is the case
#: the whole sort order turns on; the second was written last and landed first;
#: the third has no company and no Employee behind its login.
NOTES = (
	{
		"name": "AFB-2026-00001",
		"entry_uuid": "AAAA1111-0000-0000-0000-000000000001",
		"screen_name": "harvest_day",
		"screen_label": "Harvest Day",
		"feedback_text": "The bucket count resets when I lock the phone.",
		"language": "es",
		"was_dictated": 1,
		"employee": PICKER,
		"employee_name": "Ana Ramos",
		"role": "Picker",
		"designation": "Picker",
		"user": PICKER_USER,
		"company": MAIN,
		"timestamp": "2026-08-01 06:42:11",
		"received_at": "2026-08-19 18:03:00",
		"app_version": "1.9.0",
		"app_build": "412",
		"device_model": "iPhone17,2",
		"os_version": "18.4",
		"device_id": "E7F1-AAAA",
		"has_screenshot": 1,
		"screenshot": "/private/files/AFB-2026-00001-screenshot.jpg",
	},
	{
		"name": "AFB-2026-00002",
		"entry_uuid": "AAAA1111-0000-0000-0000-000000000002",
		"screen_name": "my_tasks",
		"screen_label": "My Tasks",
		"feedback_text": "The crew board shows yesterday's rows until I pull down.",
		"language": "en",
		"was_dictated": 0,
		"employee": FOREMAN,
		"employee_name": "Luis Ortega",
		"role": "Foreman",
		"user": FOREMAN_USER,
		"company": MAIN,
		"timestamp": "2026-08-03 23:30:00",
		"received_at": "2026-08-03 23:31:00",
		"app_version": "1.9.1",
		"device_model": "iPhone14,5",
		"os_version": "18.1",
		"device_id": "E7F1-BBBB",
		"has_screenshot": 0,
		"screenshot_omitted": "too_large",
		"claimed_employee": PICKER,
		"claimed_employee_name": "Ana Ramos",
	},
	{
		"name": "AFB-2026-00003",
		"entry_uuid": "AAAA1111-0000-0000-0000-000000000003",
		"screen_name": "bucket_capture",
		"feedback_text": "Nobody has given me a badge yet.",
		"language": "es",
		"was_dictated": 1,
		"employee": "",
		"user": "Administrator",
		"company": "",
		"timestamp": "2026-07-28 05:10:00",
		"received_at": "2026-07-28 05:12:00",
		"app_version": "1.8.0",
		"device_model": "iPhone14,5",
		"device_id": "E7F1-CCCC",
		"has_screenshot": 0,
	},
)


class AppFeedbackReadTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		STORE.seed(
			"User",
			[
				{"name": PICKER_USER, "enabled": 1, "full_name": "Ana Ramos"},
				{"name": FOREMAN_USER, "enabled": 1, "full_name": "Luis Ortega"},
			],
		)
		STORE.seed(
			"Employee",
			[
				{"name": PICKER, "employee_name": "Ana Ramos", "company": MAIN, "status": "Active"},
				{"name": FOREMAN, "employee_name": "Luis Ortega", "company": MAIN, "status": "Active"},
			],
		)
		STORE.seed(APP_FEEDBACK, [dict(note) for note in NOTES])

	def feed(self, **args) -> dict:
		return app_feedback.list_app_feedback(args).data

	def one(self, **args) -> dict:
		return app_feedback.get_app_feedback(args).data

	def names(self, **args) -> list:
		return [note["name"] for note in self.feed(**args)["app_feedback"]]


# ── 1. recency means when Send was pressed ──────────────────────────────────
class TheFeedIsSortedOnTheRightStamp(AppFeedbackReadTestCase):
	def test_the_default_order_is_when_send_was_pressed(self):
		"""AFB-00002 was written last. AFB-00001 landed last, and a feed ordered
		on arrival would put it first — which is the wrong answer to "what is the
		newest complaint", because a note is not made newer by finding wifi."""
		self.assertEqual(
			self.names(),
			["AFB-2026-00002", "AFB-2026-00001", "AFB-2026-00003"],
		)

	def test_date_basis_received_orders_on_arrival_instead(self):
		"""The other real question: what landed while I was away."""
		self.assertEqual(
			self.names(date_basis="received"),
			["AFB-2026-00001", "AFB-2026-00002", "AFB-2026-00003"],
		)

	def test_a_range_means_the_stamp_the_basis_names(self):
		"""AFB-00001 was WRITTEN on 1 August and RECEIVED on 19 August. One
		window finds it and the other does not, and that is not a bug in either."""
		written = self.names(from_date="2026-08-01", to_date="2026-08-05")
		landed = self.names(date_basis="received", from_date="2026-08-01", to_date="2026-08-05")
		self.assertIn("AFB-2026-00001", written)
		self.assertNotIn("AFB-2026-00001", landed)

	def test_queued_days_is_the_distance_between_the_two_stamps(self):
		"""The answer to "why did nobody act on this": the phone was in a block."""
		note = self.one(name="AFB-2026-00001")
		self.assertEqual(note["queued_days"], 18.47)
		self.assertGreaterEqual(note["queued_days"], 1)

	def test_an_unknown_date_basis_is_refused_rather_than_defaulted(self):
		with self.assertRaises(ToolError) as caught:
			self.feed(date_basis="whenever")
		self.assertIn("submitted", str(caught.exception))
		self.assertIn("received", str(caught.exception))


# ── 2. the last day of a range is a day ─────────────────────────────────────
class TheRangeClosesTheDay(AppFeedbackReadTestCase):
	def test_a_note_filed_late_on_the_last_day_is_in_the_window(self):
		"""AFB-00002 was written at 23:30 on 3 August, which is the case a range
		bounded at midnight silently drops."""
		self.assertIn("AFB-2026-00002", self.names(from_date="2026-08-01", to_date="2026-08-03"))

	def test_the_midnight_bound_this_tool_does_not_use_would_have_missed_it(self):
		"""THE NEGATIVE CONTROL. The claim above is only worth making if the
		obvious wrong bound really does fail — otherwise the test passes for a
		reason that has nothing to do with the fix. Asserted against the store
		directly, with the bound the tool deliberately does not use."""
		missed = frappe.db.get_all(
			APP_FEEDBACK,
			filters={"timestamp": ("between", ["2026-08-01", "2026-08-03"])},
			fields=["name"],
		)
		self.assertNotIn("AFB-2026-00002", [row["name"] for row in missed])

	def test_a_one_ended_range_closes_the_same_way(self):
		self.assertIn("AFB-2026-00002", self.names(to_date="2026-08-03"))
		self.assertNotIn("AFB-2026-00003", self.names(from_date="2026-08-01"))


# ── 3. submitted_by is resolved, never guessed ──────────────────────────────
class TheAskerIsResolved(AppFeedbackReadTestCase):
	def test_a_login_filters_the_user_column(self):
		feed = self.feed(submitted_by=PICKER_USER)
		self.assertEqual([note["name"] for note in feed["app_feedback"]], ["AFB-2026-00001"])
		self.assertEqual(feed["filters"]["submitted_by_column"], "user")

	def test_an_employee_docname_filters_the_employee_column(self):
		feed = self.feed(submitted_by=FOREMAN)
		self.assertEqual([note["name"] for note in feed["app_feedback"]], ["AFB-2026-00002"])
		self.assertEqual(feed["filters"]["submitted_by_column"], "employee")

	def test_a_value_in_neither_register_is_refused_by_name(self):
		"""NOT answered with an empty feed. Plenty of people have never filed a
		note, so a typo filtered onto the wrong column would be indistinguishable
		from the truth and the reader would conclude somebody said nothing."""
		with self.assertRaises(ToolError) as caught:
			self.feed(submitted_by="ana.ramos")
		message = str(caught.exception)
		self.assertIn("neither a User nor an Employee", message)
		self.assertIn("user", message)
		self.assertIn("HR-EMP", message)

	def test_a_note_from_a_login_with_no_employee_row_is_still_in_the_feed(self):
		"""A login nobody has finished setting up belongs to exactly the kind of
		person whose feedback is worth reading."""
		note = self.one(name="AFB-2026-00003")
		self.assertIsNone(note["employee"])
		self.assertEqual(note["submitted_by"], "Administrator")
		self.assertIn("no Employee record", " ".join(note["reader_notes"]))


# ── 4. company is never inferred ────────────────────────────────────────────
class TheCompanyFilterIsOptIn(AppFeedbackReadTestCase):
	def test_a_note_with_no_company_is_in_the_unfiltered_feed(self):
		"""THE CLAIM THAT WOULD BREAK IF SOMEBODY ADDED `resolve_company` HERE.
		On a single-company site that helper infers the company, and the filter
		it would build drops every note the handset filed without one."""
		self.assertIn("AFB-2026-00003", self.names())

	def test_a_single_company_site_does_not_infer_a_filter(self):
		"""THE NEGATIVE CONTROL FOR THE CLAIM ABOVE, and the reason it is here:
		`resolve_company` only infers when the site has exactly ONE Company, and
		the fixture site has two — so the test above passes whether this tool
		infers or not, and would have gone on passing if somebody wired
		`resolve_company` in. Drop the second company and the difference shows."""
		for name in [row for row in STORE.tables.get("Company", {}) if row != MAIN]:
			STORE.tables["Company"].pop(name)
		self.assertEqual(frappe.db.get_all("Company", pluck="name"), [MAIN])
		self.assertIn("AFB-2026-00003", self.names())

	def test_naming_a_company_excludes_the_unscoped_notes_and_counts_them(self):
		feed = self.feed(company=MAIN)
		self.assertNotIn("AFB-2026-00003", [note["name"] for note in feed["app_feedback"]])
		self.assertEqual(feed["without_company"], 1)
		self.assertIn("carry NO company", feed["company_note"])

	def test_a_company_that_filed_nothing_answers_empty_rather_than_refusing(self):
		feed = self.feed(company=OTHER)
		self.assertEqual(feed["count"], 0)
		self.assertIn("EMPTY FEED IS A REAL ANSWER", feed["empty_note"])


# ── 5. both names for the same column ───────────────────────────────────────
class TheAnswerCarriesBothNames(AppFeedbackReadTestCase):
	def setUp(self):
		super().setUp()
		self.note = self.one(name="AFB-2026-00001")

	def test_the_note_is_answered_under_both_comment_and_feedback_text(self):
		self.assertEqual(self.note["comment"], self.note["feedback_text"])
		self.assertEqual(self.note["comment"], "The bucket count resets when I lock the phone.")

	def test_submitted_at_is_the_doctypes_own_label_for_timestamp(self):
		self.assertEqual(self.note["submitted_at"], self.note["timestamp"])
		self.assertEqual(self.note["submitted_at"], "2026-08-01 06:42:11")
		path = (
			Path(__file__).resolve().parent.parent
			/ "erpnext_mcp"
			/ "erpnext_mcp"
			/ "doctype"
			/ "app_feedback"
			/ "app_feedback.json"
		)
		fields = {f["fieldname"]: f for f in json.loads(path.read_text())["fields"]}
		self.assertEqual(fields["timestamp"]["label"], "Submitted At")

	def test_submitted_by_is_the_login_that_was_proved(self):
		"""NOT the employee. `user` is the one identity on the record that was
		written from the session rather than reported by the handset."""
		self.assertEqual(self.note["submitted_by"], PICKER_USER)
		self.assertEqual(self.note["user"], PICKER_USER)

	def test_device_info_composes_the_five_columns_that_are_read_together(self):
		self.assertEqual(
			self.note["device_info"],
			{
				"device_model": "iPhone17,2",
				"os_version": "18.4",
				"device_id": "E7F1-AAAA",
				"app_version": "1.9.0",
				"app_build": "412",
			},
		)
		self.assertEqual(self.note["app_version"], "1.9.0")

	def test_creation_is_on_the_row(self):
		self.assertTrue(self.note["creation"])

	def test_the_feed_counts_by_screen_and_by_role(self):
		feed = self.feed()
		self.assertEqual(feed["by_screen"]["harvest_day"], 1)
		self.assertEqual(feed["by_role"]["Foreman"], 1)
		self.assertEqual(feed["by_role"]["(no role claimed)"], 1)
		self.assertEqual(feed["dictated_count"], 2)
		self.assertEqual(feed["with_screenshot"], 1)


# ── 6. a shared handset is reported, not reconciled ─────────────────────────
class TheDisagreementSurvives(AppFeedbackReadTestCase):
	def test_both_identities_are_reported_and_neither_is_corrected(self):
		note = self.one(name="AFB-2026-00002")
		self.assertEqual(note["employee"], FOREMAN)
		self.assertEqual(note["claimed_employee"], PICKER)
		self.assertIn("shared phone", " ".join(note["reader_notes"]))

	def test_a_missing_screenshot_says_why(self):
		"""'The worker did not take one' and 'it did not fit' are different
		facts, and only the second is somebody's bug."""
		note = self.one(name="AFB-2026-00002")
		self.assertEqual(note["screenshot_omitted"], "too_large")
		self.assertIn("too_large", " ".join(note["reader_notes"]))

		clean = self.one(name="AFB-2026-00003")
		self.assertIsNone(clean["screenshot_omitted"])


# ── the register, and what each read is allowed to hand back ────────────────
class TheReadsAreScopedAndFound(AppFeedbackReadTestCase):
	def test_the_list_does_not_hand_back_forty_private_file_paths(self):
		"""`has_screenshot` is the column somebody filters on; the file_url is
		on the single read, where a caller asked for one note."""
		for note in self.feed()["app_feedback"]:
			self.assertNotIn("screenshot", note)
		self.assertTrue(self.feed()["app_feedback"][1]["has_screenshot"])

	def test_the_single_read_carries_the_private_file_url(self):
		self.assertEqual(
			self.one(name="AFB-2026-00001")["screenshot"],
			"/private/files/AFB-2026-00001-screenshot.jpg",
		)

	def test_a_note_is_found_by_the_handsets_own_uuid(self):
		"""The only identifier a phone ever knows, and the one somebody has when
		they are chasing 'the app says it filed this and I cannot find it'."""
		by_uuid = self.one(name="AAAA1111-0000-0000-0000-000000000002")
		self.assertEqual(by_uuid["name"], "AFB-2026-00002")
		self.assertEqual(self.one(entry_uuid="AAAA1111-0000-0000-0000-000000000002"), by_uuid)

	def test_an_unknown_name_names_both_ways_of_finding_one(self):
		with self.assertRaises(ToolError) as caught:
			self.one(name="AFB-2026-09999")
		message = str(caught.exception)
		self.assertIn("entry_uuid", message)
		self.assertIn("list_app_feedback", message)

	def test_naming_nothing_at_all_is_refused_with_the_finder(self):
		with self.assertRaises(ToolError) as caught:
			self.one()
		self.assertIn("list_app_feedback", str(caught.exception))

	def test_the_other_filters_narrow_the_feed(self):
		self.assertEqual(self.names(screen="my_tasks"), ["AFB-2026-00002"])
		self.assertEqual(self.names(role="Picker"), ["AFB-2026-00001"])
		self.assertEqual(self.names(language="en"), ["AFB-2026-00002"])
		self.assertEqual(self.names(app_version="1.8.0"), ["AFB-2026-00003"])
		self.assertEqual(self.names(device_id="E7F1-CCCC"), ["AFB-2026-00003"])
		self.assertEqual(self.names(has_screenshot=True), ["AFB-2026-00001"])
		self.assertEqual(len(self.names(device_model="iPhone14,5")), 2)
		self.assertEqual(len(self.names(was_dictated=True)), 2)

	#: One value per filter argument that the fixture's three notes do NOT match,
	#: so an argument the handler quietly ignored would come back with all three.
	NARROWING_PROBES: ClassVar[dict] = {
		"submitted_by": PICKER_USER,
		"screen": "settings",
		"role": "Agronomist",
		"language": "fr",
		"app_version": "2.0.0",
		"device_model": "iPad13,1",
		"device_id": "E7F1-ZZZZ",
		"entry_uuid": "no-such-uuid",
		"has_screenshot": False,
		"was_dictated": False,
		"from_date": "2027-01-01",
		"to_date": "2020-01-01",
		"company": OTHER,
		"limit": 1,
	}

	def test_every_argument_the_table_names_actually_narrows_the_feed(self):
		"""LIST_ARGUMENTS is what the registry schema is asserted against, so it
		is only worth asserting if it is the table the handler really uses. Each
		argument gets a value the fixture does not match: one the handler ignored
		would answer with all three notes."""
		self.assertEqual(self.feed()["count"], 3)
		for key in app_feedback.LIST_ARGUMENTS:
			if key == "date_basis":
				continue  # not a filter — it chooses which column the others read
			with self.subTest(key=key):
				feed = self.feed(**{key: self.NARROWING_PROBES[key]})
				self.assertLess(feed["count"], 3, key)

	def test_the_probe_table_covers_every_argument(self):
		"""A probe missing from the table above would silently skip its argument
		with a KeyError nobody sees, since the loop is inside a subTest."""
		self.assertEqual(
			set(self.NARROWING_PROBES) | {"date_basis"},
			set(app_feedback.LIST_ARGUMENTS),
		)

	def test_the_limit_is_reported_with_what_it_left_out(self):
		feed = self.feed(limit=1)
		self.assertEqual(feed["count"], 1)
		self.assertTrue(feed["truncated"])
		self.assertIn("Narrow by screen", feed["truncated_note"])

		whole = self.feed()
		self.assertEqual(whole["count"], 3)
		self.assertFalse(whole["truncated"])


class TheToolsAreRegistered(unittest.TestCase):
	"""Registered, read-only, and on by default — like every other read here."""

	NAMES = ("list_app_feedback", "get_app_feedback")

	def test_both_are_in_the_catalogue_and_neither_mutates(self):
		for name in self.NAMES:
			with self.subTest(name=name):
				self.assertIn(name, registry.TOOLS)
				self.assertFalse(registry.TOOLS[name]["mutating"])
				self.assertIn(name, registry.READ_TOOLS)

	def test_the_write_half_is_still_not_a_catalogue_tool(self):
		"""A phone files its OWN note under its own login, and there is no
		version of "file feedback as somebody else" a model should be able to
		reach. Publishing the reads does not change that."""
		self.assertNotIn("submit_app_feedback", registry.TOOLS)

	def test_the_schema_declares_exactly_what_the_handler_reads(self):
		"""BOTH DIRECTIONS, AGAINST THE HANDLER'S OWN TABLE rather than a list
		copied beside it. `additionalProperties` is advertised on every schema in
		this app and enforced by nothing, so an argument the schema omits is not
		refused — it is silently ignored, and no caller ever learns it exists.
		The other direction is the same failure read backwards: an argument the
		schema promises and the handler never reads."""
		self.assertEqual(
			set(registry.TOOLS["list_app_feedback"]["inputSchema"]["properties"]),
			set(app_feedback.LIST_ARGUMENTS),
		)
		self.assertEqual(
			set(registry.TOOLS["get_app_feedback"]["inputSchema"]["properties"]),
			set(app_feedback.GET_ARGUMENTS),
		)

	def test_both_switches_ship_on(self):
		path = (
			Path(__file__).resolve().parent.parent
			/ "erpnext_mcp"
			/ "erpnext_mcp"
			/ "doctype"
			/ "erpnext_mcp_settings"
			/ "erpnext_mcp_settings.json"
		)
		settings = json.loads(path.read_text())
		fields = {f["fieldname"]: f for f in settings["fields"]}
		for name in self.NAMES:
			with self.subTest(name=name):
				self.assertEqual(fields[f"allow_{name}"]["default"], "1")
				self.assertIn(f"allow_{name}", settings["field_order"])


if __name__ == "__main__":
	unittest.main()
