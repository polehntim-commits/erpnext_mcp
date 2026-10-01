# SPDX-License-Identifier: MIT
"""Multi-day classes (v0.212.0). docs/design/quick_wins_2026_10.md, Amendment 1.

A day is a Training Session; a course is a session other sessions point at. The
course holds the registration and gives the credit, once, for whoever attended
every required day.
"""

import datetime
from unittest import mock

import frappe

from erpnext_mcp import alerts, training, training_courses, training_sessions
from erpnext_mcp.alerts import rules as alert_rules

from .fixtures import MAIN
from .harness import STORE
from .test_training_sessions import (
	BEN_BADGE,
	CURRICULUM,
	SECOND,
	SIGNATURE,
	THIRD_BADGE,
	TOPICS,
	TRAINEE,
	TrainingSessionTestCase,
	days_out,
)

ON = {"allow_group_training_sessions": 1, "allow_add_training_session_day": 1}


class CourseCase(TrainingSessionTestCase):
	def setUp(self):
		super().setUp()
		from .test_training_sessions import ON as BASE

		self.configure(enabled=1, **BASE, **ON)

	def a_course(self, first=-1, second=0, **overrides) -> dict:
		"""A two-day course: yesterday and today by default, so both can be closed."""
		payload = {
			"training_type": CURRICULUM,
			"company": MAIN,
			"content_topics_covered": TOPICS,
			"expires_date": days_out(365),
			"start_time": "09:00",
			"end_time": "12:00",
			"location": "CGCC Building 2",
			"days": [
				{"session_date": days_out(first)},
				{
					"session_date": days_out(second),
					"location": "CGCC Building 3",
					"start_time": "13:00",
					"end_time": "16:00",
				},
			],
		}
		payload.update(overrides)
		return self.tool_data("create_training_session", payload)

	def register(self, course: str, *people):
		for person in people:
			self.tool_data("add_session_attendee", {"session": course, "employee": person})

	def attend(self, day: str, badge: str, person: str, sign=True):
		self.tool_data("add_session_attendee", {"session": day, "badge_scan": badge})
		if sign:
			self.tool_data(
				"sign_session_attendance", {"session": day, "employee": person, "signature": SIGNATURE}
			)


class ACourseHasDays(CourseCase):
	def test_days_make_a_course_and_each_day_is_a_session(self):
		course = self.a_course()
		self.assertTrue(course["is_course"])
		self.assertEqual(course["day_count"], 2)
		self.assertEqual((course["session_date"], course["end_date"]), (days_out(-1), days_out(0)))
		one, two = course["days"]
		self.assertEqual((one["day_number"], two["day_number"]), (1, 2))
		self.assertEqual((one["location"], two["location"]), ("CGCC Building 2", "CGCC Building 3"))
		self.assertEqual((one["start_time"], two["start_time"]), ("09:00:00", "13:00:00"))
		day = self.tool_data("get_training_session", {"session": two["session"]})
		self.assertEqual((day["parent_session"], day["day_number"], day["day_count"]), (course["name"], 2, 2))
		self.assertFalse(day["is_course"])
		self.assertEqual(day["training_type"], CURRICULUM)

	def test_one_day_is_just_a_session_and_an_old_session_is_untouched(self):
		single = self.tool_data(
			"create_training_session",
			{
				"training_type": CURRICULUM,
				"company": MAIN,
				"days": [{"session_date": days_out(3), "location": "Shed"}],
			},
		)
		self.assertEqual((single["day_count"], single["days"], single["parent_session"]), (1, [], None))
		self.assertEqual((single["session_date"], single["location"]), (days_out(3), "Shed"))
		plain = self.open_session()
		self.assertEqual((plain["day_count"], plain["days"], plain["is_course"]), (1, [], False))

	def test_bad_days_are_refused_and_nothing_is_created(self):
		for days, words in (
			([{"session_date": "soon"}, {"session_date": days_out(1)}], "needs a session_date"),
			([{"session_date": days_out(1)}, {"session_date": days_out(1)}], "share a date"),
			("not a list", "must be a list"),
		):
			message = self.tool_error(
				"create_training_session", {"training_type": CURRICULUM, "company": MAIN, "days": days}
			)
			self.assertIn(words, message)
		self.assertEqual(STORE.rows(training_sessions.DOCTYPE), [])

	def test_the_register_lists_courses_or_days(self):
		course = self.a_course()
		self.open_session(session_date=days_out(5))
		by_course = self.tool_data("list_training_sessions", {})["sessions"]
		self.assertEqual(sorted(len(row["days"]) for row in by_course), [0, 2])
		by_day = self.tool_data("list_training_sessions", {"view": "days"})["sessions"]
		self.assertEqual(len(by_day), 3)
		self.assertNotIn(course["name"], [row["name"] for row in by_day])
		self.assertEqual(len(self.tool_data("list_training_sessions", {"view": "all"})["sessions"]), 4)
		self.assertIn("view is", self.tool_error("list_training_sessions", {"view": "weeks"}))

	def test_existing_sessions_are_grouped_into_one_course(self):
		"""Oct 28 and Nov 17, entered as two sessions before a course could have days."""
		first = self.open_session(session_date=days_out(-20), content_topics_covered=TOPICS)["name"]
		second = self.open_session(session_date=days_out(0), content_topics_covered=TOPICS)["name"]
		self.attend(first, BEN_BADGE, TRAINEE)
		course = self.tool_data("group_training_sessions", {"sessions": [second, first]})
		self.assertEqual(course["grouped"], [first, second])
		self.assertEqual([day["session"] for day in course["days"]], [first, second])
		self.assertEqual([row["employee"] for row in course["attendee_rows"]], [TRAINEE])
		# Day 1 keeps its attendance; day 2 has Ben registered and not yet there.
		self.assertTrue(
			self.tool_data("get_training_session", {"session": first})["attendee_rows"][0]["attended"]
		)
		self.assertFalse(
			self.tool_data("get_training_session", {"session": second})["attendee_rows"][0]["attended"]
		)
		self.assertIn(
			"already a day", self.tool_error("group_training_sessions", {"sessions": [first, second]})
		)

	def test_grouping_refuses_what_is_not_one_course(self):
		a = self.open_session()["name"]
		self.assertIn("two or more", self.tool_error("group_training_sessions", {"sessions": [a]}))
		other = self.open_session(training_type="WPS Worker Training")["name"]
		self.assertIn(
			"share a Training Type", self.tool_error("group_training_sessions", {"sessions": [a, other]})
		)
		done = self.a_full_session()
		self.tool_data("complete_training_session", {"session": done})
		self.assertIn("already filed", self.tool_error("group_training_sessions", {"sessions": [a, done]}))

	def test_a_day_is_added_and_a_single_session_becomes_a_course(self):
		single = self.open_session(session_date=days_out(1), content_topics_covered=TOPICS)["name"]
		grown = self.tool_data("add_training_session_day", {"session": single, "session_date": days_out(8)})
		self.assertTrue(grown["became_course"])
		self.assertEqual(grown["days"][0]["session"], single)
		self.assertEqual(grown["day_count"], 2)
		more = self.tool_data(
			"add_training_session_day",
			{"session": single, "session_date": days_out(4), "required": False},
		)
		self.assertEqual(more["name"], grown["name"])
		self.assertEqual([d["session_date"] for d in more["days"]], [days_out(1), days_out(4), days_out(8)])
		self.assertEqual([d["required"] for d in more["days"]], [True, False, True])
		self.assertIn(
			"already has a day",
			self.tool_error(
				"add_training_session_day", {"session": grown["name"], "session_date": days_out(8)}
			),
		)


class AttendanceIsPerDay(CourseCase):
	def test_registering_on_the_course_puts_them_on_every_day_not_yet_attended(self):
		course = self.a_course()
		data = self.tool_data("add_session_attendee", {"session": course["name"], "employee": TRAINEE})
		self.assertTrue(data["registered"])
		self.assertEqual(len(data["days_added"]), 2)
		for day in course["days"]:
			rows = self.tool_data("get_training_session", {"session": day["session"]})["attendee_rows"]
			self.assertEqual([(r["employee"], r["attended"]) for r in rows], [(TRAINEE, False)])
		self.assertIn(
			"already registered",
			self.tool_error("add_session_attendee", {"session": course["name"], "employee": TRAINEE}),
		)

	def test_the_scan_at_the_door_turns_a_registration_into_attendance(self):
		course = self.a_course()
		self.register(course["name"], TRAINEE)
		day_one = course["days"][0]["session"]
		self.tool_data("add_session_attendee", {"session": day_one, "badge_scan": BEN_BADGE})
		rows = self.tool_data("get_training_session", {"session": day_one})["attendee_rows"]
		self.assertEqual(len(rows), 1, "not a second row for the same person")
		self.assertTrue(rows[0]["attended"] and rows[0]["badge_scanned"])
		# A second scan IS a duplicate.
		self.assertIn(
			"already on",
			self.tool_error("add_session_attendee", {"session": day_one, "badge_scan": BEN_BADGE}),
		)

	def test_a_walk_in_on_one_day_is_registered_on_the_course(self):
		course = self.a_course()
		day_two = course["days"][1]["session"]
		self.tool_data("add_session_attendee", {"session": day_two, "badge_scan": THIRD_BADGE})
		registered = self.tool_data("get_training_session", {"session": course["name"]})["attendee_rows"]
		self.assertEqual([r["employee"] for r in registered], [SECOND])
		day_one = self.tool_data("get_training_session", {"session": course["days"][0]["session"]})
		self.assertEqual(
			[(r["employee"], r["attended"]) for r in day_one["attendee_rows"]], [(SECOND, False)]
		)


class CreditNeedsEveryRequiredDay(CourseCase):
	def both_days(self):
		"""Ben comes to both days; Marco only to the first."""
		course = self.a_course()
		self.register(course["name"], TRAINEE, SECOND)
		one, two = (day["session"] for day in course["days"])
		self.attend(one, BEN_BADGE, TRAINEE)
		self.attend(one, THIRD_BADGE, SECOND)
		self.attend(two, BEN_BADGE, TRAINEE)
		return course["name"], one, two

	def test_a_day_files_nothing_and_the_course_waits_for_the_rest(self):
		course, one, _two = self.both_days()
		closed = self.tool_data("complete_training_session", {"session": one})
		self.assertEqual((closed["status"], closed["filed_count"]), ("Completed", 0))
		self.assertEqual(len(closed["days_outstanding"]), 1)
		self.assertEqual(STORE.rows(training.DOCTYPE), [])
		self.assertIn("still open", self.tool_error("complete_training_session", {"session": course}))
		self.assertTrue(self.tool_data("complete_training_session", {"session": one})["already"])

	def test_the_last_day_completes_the_course_for_whoever_came_to_all_of_it(self):
		course, one, two = self.both_days()
		self.tool_data("complete_training_session", {"session": one})
		last = self.tool_data("complete_training_session", {"session": two})
		self.assertTrue(last["course_completed"])
		self.assertEqual([item["employee"] for item in last["records_filed"]], [TRAINEE])
		self.assertEqual(
			[(m["employee"], [d["day_number"] for d in m["missed"]]) for m in last["missed_days"]],
			[(SECOND, [2])],
		)
		records = STORE.rows(training.DOCTYPE)
		self.assertEqual(len(records), 1)
		self.assertEqual((records[0]["employee"], records[0]["completed_date"]), (TRAINEE, days_out(0)))
		self.assertIn("required day", records[0]["notes"])
		head = self.tool_data("get_training_session", {"session": course})
		self.assertEqual((head["status"], head["records_created"]), ("Completed", 1))
		ben = next(r for r in head["attendee_rows"] if r["employee"] == TRAINEE)
		self.assertEqual(ben["state"], "recorded")
		# A second completion files nothing twice.
		again = self.tool_data("complete_training_session", {"session": course})
		self.assertEqual(again["filed_count"], 0)
		self.assertEqual(len(STORE.rows(training.DOCTYPE)), 1)

	def test_an_optional_day_does_not_cost_anybody_their_credit(self):
		course = self.a_course(
			days=[
				{"session_date": days_out(-1)},
				{"session_date": days_out(0), "required": False},
			]
		)
		self.register(course["name"], TRAINEE)
		one, two = (day["session"] for day in course["days"])
		self.attend(one, BEN_BADGE, TRAINEE)
		closed = self.tool_data("complete_training_session", {"session": one})
		self.assertTrue(closed["course_completed"], "the only REQUIRED day is closed")
		self.assertEqual([item["employee"] for item in closed["records_filed"]], [TRAINEE])
		self.assertEqual(self.raw(two)["status"], "Scheduled")

	def test_closing_a_day_nobody_attended_is_refused(self):
		course = self.a_course()
		self.register(course["name"], TRAINEE)
		self.assertIn(
			"nobody is marked present",
			self.tool_error("complete_training_session", {"session": course["days"][0]["session"]}),
		)

	def test_a_single_session_still_completes_as_it_always_did(self):
		session = self.a_full_session()
		data = self.tool_data("complete_training_session", {"session": session})
		self.assertEqual((data["status"], data["filed_count"]), ("Completed", 2))


class TheCardAndCheckIn(CourseCase):
	def test_tomorrow_then_today_with_the_day_of_the_course(self):
		course = self.a_course(
			first=0,
			second=1,
			start_time=None,
			end_time=None,
			days=[
				{"session_date": days_out(0)},
				{
					"session_date": days_out(1),
					"start_time": "08:00",
					"end_time": "12:00",
					"location": "Building 3",
				},
			],
		)
		self.register(course["name"], TRAINEE)
		cards = training_courses.cards("ben@example.test", TRAINEE)["cards"]
		self.assertEqual([c["phase"] for c in cards], ["today", "tomorrow"])
		self.assertEqual(cards[0]["title"], "Class today — Day 1 of 2")
		self.assertEqual(cards[1]["title"], "Class tomorrow — Day 2 of 2")
		self.assertEqual(cards[1]["subtitle"], "08:00–12:00 · Building 3")
		self.assertEqual(
			(cards[0]["day_number"], cards[0]["day_count"], cards[0]["course"]), (1, 2, course["name"])
		)
		self.assertTrue(cards[0]["can_check_in"], "no times on the day: open all day")
		self.assertFalse(cards[1]["can_check_in"])
		self.assertEqual(
			[p["doctype"] for p in cards[0]["papers"]],
			["Training Session", "Training Session", "Training Type"],
		)
		# Somebody not on the class has no card.
		self.assertEqual(training_courses.cards("x", SECOND)["cards"], [])

	def test_a_single_day_class_has_no_day_suffix_and_the_wording_is_the_farms(self):
		session = self.open_session(session_date=days_out(1))["name"]
		self.tool_data("add_session_attendee", {"session": session, "employee": TRAINEE, "attended": False})
		with mock.patch.dict(
			training_courses.DEFAULTS, {"training_card_title_tomorrow": "Tomorrow: {training_type}"}
		):
			card = training_courses.cards("u", TRAINEE)["cards"][0]
		self.assertEqual(card["title"], f"Tomorrow: {CURRICULUM}")
		with mock.patch.dict(training_courses.DEFAULTS, {"training_card_days_before": 0}):
			self.assertEqual(training_courses.cards("u", TRAINEE)["cards"], [])

	def test_checking_in_is_the_callers_own_row_once(self):
		course = self.a_course(
			first=0,
			second=1,
			start_time=None,
			end_time=None,
			days=[
				{"session_date": days_out(0)},
				{"session_date": days_out(1)},
			],
		)
		self.register(course["name"], TRAINEE)
		today, tomorrow = (day["session"] for day in course["days"])
		done = training_courses.check_in(today, "u", TRAINEE, 45.6, -121.18, 9)
		self.assertEqual((done["already"], done["day_number"], done["day_count"]), (False, 1, 2))
		row = self.tool_data("get_training_session", {"session": today})["attendee_rows"][0]
		self.assertTrue(row["attended"] and row["badge_scanned"], "a self check-in identifies the person")
		self.assertEqual(row["scan_position"]["source"], "Self")
		self.assertEqual(row["missing"], ["signature"])
		self.assertTrue(training_courses.check_in(today, "u", TRAINEE)["already"])
		self.assertTrue(training_courses.cards("u", TRAINEE)["cards"][0]["checked_in"])
		for call, kind in (
			(lambda: training_courses.check_in(tomorrow, "u", TRAINEE), "invalid"),  # not open yet
			(lambda: training_courses.check_in(today, "u", SECOND), "not_found"),  # not on the class
			(
				lambda: training_courses.check_in(course["name"], "u", TRAINEE),
				"invalid",
			),  # a course is not a day
			(lambda: training_courses.check_in("TRNS-NOPE", "u", TRAINEE), "not_found"),
			(lambda: training_courses.check_in(today, "u", ""), "forbidden"),
		):
			with self.assertRaises(training_courses.CheckInError) as caught:
				call()
			self.assertEqual(caught.exception.kind, kind)

	def test_the_window_follows_the_days_times(self):
		row = {"session_date": "2026-11-17", "start_time": "09:00:00", "end_time": "12:00:00"}
		opens, closes = training_courses.check_in_window(row)
		self.assertEqual((str(opens), str(closes)), ("2026-11-17 08:00:00", "2026-11-17 14:00:00"))
		opens, closes = training_courses.check_in_window({"session_date": "2026-11-17"})
		self.assertEqual((opens.hour, closes.hour), (0, 23))


class TheEveningBefore(CourseCase):
	def test_tomorrows_attendees_are_pushed_once_at_the_configured_hour(self):
		course = self.a_course(first=1, second=20)
		self.register(course["name"], TRAINEE, SECOND)
		sent = []

		def fake(employees, payload, **_):
			sent.append((list(employees), payload))
			return {"sent": 1}

		noon = datetime.datetime.combine(datetime.date.fromisoformat(days_out(0)), datetime.time(12, 0))
		evening = noon.replace(hour=18)
		with mock.patch("erpnext_mcp.services.push.send_push_to_employees", side_effect=fake):
			self.assertEqual(training_courses.send_reminders(noon)["sent"], 0)
			first = training_courses.send_reminders(evening)
			second = training_courses.send_reminders(evening)
		self.assertEqual((first["sent"], second["sent"]), (2, 0))
		self.assertEqual(sorted(people[0] for people, _ in sent), sorted([TRAINEE, SECOND]))
		payload = sent[0][1]
		self.assertEqual(payload["aps"]["category"], "FARM_TRAINING")
		self.assertEqual(payload["aps"]["alert"]["title"], "Class tomorrow")
		self.assertEqual(
			payload["aps"]["alert"]["body"], f"{CURRICULUM} — Day 1 of 2, 09:00–12:00 at CGCC Building 2."
		)
		self.assertEqual(
			(payload["training_session"], payload["course"], payload["phase"]),
			(course["days"][0]["session"], course["name"], "tomorrow"),
		)

	def test_minus_one_switches_it_off(self):
		course = self.a_course(first=1, second=20)
		self.register(course["name"], TRAINEE)
		evening = datetime.datetime.combine(datetime.date.fromisoformat(days_out(0)), datetime.time(18, 0))
		with (
			mock.patch.dict(training_courses.DEFAULTS, {"training_reminder_hour": -1}),
			mock.patch("erpnext_mcp.services.push.send_push_to_employees") as push,
		):
			self.assertEqual(training_courses.send_reminders(evening)["sent"], 0)
			push.assert_not_called()


class AMissedRequiredDay(CourseCase):
	def a_miss(self):
		course = self.a_course(first=-3, second=-2)
		self.register(course["name"], TRAINEE, SECOND)
		one, two = (day["session"] for day in course["days"])
		self.attend(one, BEN_BADGE, TRAINEE)
		self.attend(one, THIRD_BADGE, SECOND)
		self.attend(two, BEN_BADGE, TRAINEE)
		self.tool_data("complete_training_session", {"session": one})
		self.tool_data("complete_training_session", {"session": two})
		return course["name"], two

	def observations(self):
		return alert_rules._scan_missed_training_days(
			{"today": frappe.utils.today(), "company": MAIN, "rule": {}}
		)

	def test_the_person_who_missed_a_day_is_found_and_the_one_who_did_not_is_not(self):
		course, two = self.a_miss()
		found = training_courses.missed_days(MAIN)
		self.assertEqual([(f["course"], f["employee"]) for f in found], [(course, SECOND)])
		self.assertEqual([(d["session"], d["day_number"]) for d in found[0]["missed"]], [(two, 2)])
		seen = self.observations()
		self.assertEqual(len(seen), 1)
		self.assertEqual(seen[0].source_doctype, "Training Session Attendee")
		self.assertIn("missed Day 2", seen[0].message)
		self.assertIn("Book a make-up day by", seen[0].message)
		from erpnext_mcp.alerts import base as alert_base

		self.assertEqual(
			alert_base.subject_employee(seen[0].source_doctype, seen[0].source_docname, MAIN), SECOND
		)

	def test_the_warning_is_tied_to_the_certificate_the_course_renews(self):
		self.a_miss()
		frappe.db.set_value("Training Type", CURRICULUM, "renews_certification", "Applicator License")
		STORE.seed(
			"Certification",
			[
				{
					"name": "Marco — applicator",
					"cert_name": "Marco — applicator",
					"cert_type": "Applicator License",
					"holder": SECOND,
					"company": MAIN,
					"status": "Active",
					"expiration_date": days_out(20),
				}
			],
		)
		seen = self.observations()[0]
		self.assertEqual(seen.due_date, days_out(20))
		self.assertEqual(seen.severity, "Critical")
		self.assertIn(f"expires on {days_out(20)} (20 day(s))", seen.message)
		self.assertIn("make-up day is needed before then, or it lapses", seen.message)

	def test_a_make_up_clears_it(self):
		self.a_miss()
		make_up = self.open_session(session_date=days_out(0), content_topics_covered=TOPICS)["name"]
		self.attend(make_up, THIRD_BADGE, SECOND)
		self.assertEqual(training_courses.missed_days(MAIN), [])

	def test_the_rule_is_seeded_as_a_swept_rule(self):
		self.assertIn("training_day_missed", alerts.names())
		self.assertIn("training_day_missed", alert_rules.SCANNERS)
		self.assertEqual(
			alert_rules.SHAPES["training_day_missed"]["extra_parameters"]["notify_roles"],
			["Farm Manager", "Foreman"],
		)
