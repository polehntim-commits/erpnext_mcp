# SPDX-License-Identifier: MIT
"""v0.140.0 — the horn that reaches the one person who can call the cool-down.

The weather sweep has known since v0.19.4 that a block crossed eighty degrees at
11:45. It wrote the reading, it wrote a Threshold Crossed event on the shift's
own timeline, and then it stopped — and the foreman standing in that block found
out the next time they happened to open the app, which on a picking crew is at
lunch. OAR 437-004-1131 runs its clock from the crossing, not from the moment
somebody read a screen.

SEVEN CLAIMS.

 1. **THE CROSSING RINGS THE CREW LEADER'S HANDSET AND NOBODY ELSE'S.**
    `TheCrossingRingsTheCrewLeader`. Not the office, not the crew. The
    obligation — water at the required rate, shade within reach, the
    preventative cool-down cycle, observation for signs — belongs to the person
    standing on the block, which is what the compliance rule of the same name
    already says in code: `producer_assigned_to_expression` is `row.foreman`.

 2. **ONCE PER SHIFT, AND THE FENCE IS NOT THE ONE THE TIMELINE USES.**
    `TheHornRingsOnce`. `already_crossed` asks whether the shift carries ANY
    Threshold Crossed event, which is right for the timeline and wrong for the
    horn: a Spray shift over the wind limit at 09:00 in cool air would spend the
    shift's one event and then never ring on the hottest afternoon of the
    season. `heat_announced_for` reads the stored snapshot instead.

 3. **WIND IS NOT PUSHED AND HEAT IS.** `TheHornRingsOnce`. An applicator over
    the wind threshold knows from the boom; heat is the one where the condition
    is survivable minute to minute and the obligation is not.

 4. **THE PAYLOAD IS ACTIONABLE ON THE LOCK SCREEN.** `ThePayloadTheHandsetActsOn`.
    The block, both temperatures, the shift docname, and an `action` naming a
    route this app actually publishes — `log_shift_break` with a `Cool-Down`.

 5. **IT PIERCES DO NOT DISTURB, AND IT IS THE ONLY NON-BREAK PUSH THAT MAY.**
    `ThePayloadTheHandsetActsOn`. `INTERRUPTION_ACTIVE`'s own comment argues that
    a server overriding a foreman's Focus NIGHTLY gets trained out of within a
    fortnight. Claim 2 is what makes this not nightly.

 6. **A BACKFILL NEVER RINGS ANYBODY.** `TheHornRingsOnce`. Reading last
    Tuesday's archive into a closed shift is bookkeeping, and a phone that buzzes
    about Tuesday on Thursday teaches its owner to ignore the one that buzzes
    about now.

 7. **NOTHING IT DOES CAN COST THE READING.** `TheReadingSurvivesTheHorn`. No p8
    key, no enrolled handset, a transport that 503s, a shift with no foreman —
    every one of them is a named report beside a timeline that was written
    anyway.
"""

import unittest
from unittest import mock

import frappe

from erpnext_mcp import roles, shifts
from erpnext_mcp.services import push as push_service
from erpnext_mcp.services import weather
from erpnext_mcp.tools import push as push_tools

from .fixtures import MAIN
from .harness import ROLES, STORE
from .test_push import Recorder, apns_conf
from .test_weather import FOREMAN, WORKER, WeatherTestCase, at

ADA = "ada.heat@example.test"
BOSS = "boss.heat@example.test"


class HeatPushTestCase(WeatherTestCase):
	"""A weather site with APNs configured and a recording transport on it."""

	def setUp(self):
		super().setUp()
		self.transport = Recorder()
		patched = mock.patch.object(push_service, "_apns_transport", self.transport)
		patched.start()
		self.addCleanup(patched.stop)
		frappe.conf.update(apns_conf())
		self.addCleanup(frappe.conf.clear)
		push_service._JWT_CACHE.clear()

		STORE.seed(
			"User",
			[
				{"name": ADA, "email": ADA, "enabled": 1, "full_name": "Ada Orchard"},
				{"name": BOSS, "email": BOSS, "enabled": 1, "full_name": "Mo Office"},
			],
		)
		frappe.db.set_value("Employee", FOREMAN, "user_id", ADA)
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	# -- helpers -------------------------------------------------------------
	def enrol(self, user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA"):
		return push_tools.register_push_token(
			{
				"user": user,
				"employee": employee,
				"token": token,
				"device_id": device_id,
				"platform": "ios",
			}
		).data

	def tokens_rung(self) -> list:
		return [call["url"].rsplit("/", 1)[-1] for call in self.transport.calls]

	def hot(self, shift, hour=11, temp=92.0, humidity=40.0, minute=0, wind=3.0):
		"""One reading onto the shift, then the sweep's own evaluation of it."""
		reading = self.reading(hour, temp=temp, humidity=humidity, minute=minute, wind=wind)
		self.append(shift, reading)
		return weather.evaluate_thresholds(self.raw(shift), reading)


# ── 1. who it reaches ───────────────────────────────────────────────────────
class TheCrossingRingsTheCrewLeader(HeatPushTestCase):
	def test_a_heat_crossing_rings_the_foreman_of_that_shift(self):
		self.enrol()
		shift = self.start()["name"]
		answer = self.hot(shift)

		self.assertEqual(answer["heat_push"]["sent"], 1)
		self.assertEqual(answer["heat_push"]["recipients"], "foreman")
		self.assertEqual(answer["heat_push"]["foreman"], FOREMAN)
		self.assertEqual(self.tokens_rung(), ["TOK-ADA"])

	def test_it_does_not_ring_the_crew(self):
		"""A break horn goes to twenty phones because a break is twenty people's
		news. This is one person's DECISION, and a crew that gets buzzed about a
		decision they cannot make stops reading the horn that tells them to stop
		work."""
		self.enrol()
		self.enrol(user=BOSS, employee=WORKER, device_id="DEV-BEN", token="TOK-BEN")
		shift = self.start()["name"]
		self.hot(shift)

		self.assertEqual(self.tokens_rung(), ["TOK-ADA"])

	def test_a_shift_with_no_foreman_falls_back_to_the_supervisors_and_says_so(self):
		"""An imported shift, or one written by hand. The choice there is between
		the wrong recipients and nobody at all, and which of the two happened has
		to be readable off the report."""
		self.enrol(user=BOSS, employee=WORKER, device_id="DEV-BEN", token="TOK-BEN")
		# A REAL `Has Role` ROW, not `set_roles`. `supervisor_employees` reads the
		# child table; the double's ROLES dict is what `frappe.get_roles` answers
		# from, and faking the second while asserting the first would test the
		# double rather than the app. Same argument `WhoIsToldAboutAnAlert` makes.
		roles.install_roles()
		user = frappe.get_doc("User", BOSS)
		user.append("roles", {"role": "Farm Manager"})
		user.save()
		frappe.db.set_value("Employee", WORKER, "user_id", BOSS)

		shift = self.start()["name"]
		frappe.db.set_value(shifts.DOCTYPE, shift, "foreman", None)
		answer = self.hot(shift)

		self.assertEqual(answer["heat_push"]["recipients"], "supervisors")
		self.assertIsNone(answer["heat_push"]["foreman"])
		self.assertEqual(self.tokens_rung(), ["TOK-BEN"])

	def test_the_push_is_collapsed_on_the_shift(self):
		"""A foreman with two handsets gets one notification, and a resend — which
		claim 2 makes rare and does not make impossible — replaces its
		predecessor rather than stacking a season of them."""
		self.enrol()
		shift = self.start()["name"]
		self.hot(shift)
		self.assertEqual(self.transport.calls[0]["headers"]["apns-collapse-id"], shift)

	def test_it_is_delivered_immediately_and_not_at_apples_convenience(self):
		"""Priority 5 lets Apple hold delivery for a moment that conserves the
		handset's battery. A crew is in the sun now; that is the wrong trade, and
		it is the trade the nightly compliance sweep correctly makes."""
		self.enrol()
		shift = self.start()["name"]
		self.hot(shift)
		self.assertEqual(self.transport.calls[0]["headers"]["apns-priority"], push_service.PRIORITY_IMMEDIATE)


# ── 2. when it rings, and when it must not ──────────────────────────────────
class TheHornRingsOnce(HeatPushTestCase):
	def test_a_second_hot_reading_does_not_ring_again(self):
		self.enrol()
		shift = self.start()["name"]
		self.hot(shift, hour=11, temp=92.0)
		self.transport.calls.clear()

		answer = self.hot(shift, hour=12, temp=96.0)

		self.assertNotIn("heat_push", answer)
		self.assertEqual(self.transport.calls, [])

	def test_a_reading_below_the_threshold_rings_nobody(self):
		self.enrol()
		shift = self.start()["name"]
		answer = self.hot(shift, temp=71.0)

		self.assertNotIn("heat_push", answer)
		self.assertEqual(self.transport.calls, [])

	def test_a_wind_crossing_on_a_spray_shift_rings_nobody(self):
		"""CLAIM 3. An applicator over the wind limit knows from the boom. Heat is
		the one where the condition is survivable minute to minute and the
		obligation on it is not."""
		self.enrol()
		shift = self.start(shift_type=weather.SPRAY_SHIFT_TYPE)["name"]
		answer = self.hot(shift, temp=60.0, humidity=50.0, wind=25.0)

		self.assertTrue(answer["crossed"], "the wind crossing was recorded")
		self.assertNotIn("heat_push", answer)
		self.assertEqual(self.transport.calls, [])

	def test_a_wind_crossing_does_not_spend_the_shifts_heat_horn(self):
		"""THE CASE THE OBVIOUS FENCE GETS WRONG, and the reason
		`heat_announced_for` exists beside `already_crossed`. A Spray shift over
		the wind limit at 09:00 in cool air has its one Threshold Crossed event;
		if the horn were fenced on THAT, the phone would never ring on the hottest
		afternoon of the season and nobody would ever find out why."""
		self.enrol()
		shift = self.start(shift_type=weather.SPRAY_SHIFT_TYPE)["name"]
		self.hot(shift, hour=9, temp=60.0, humidity=50.0, wind=25.0)
		self.assertEqual(self.transport.calls, [])

		answer = self.hot(shift, hour=14, temp=94.0, humidity=40.0)

		self.assertEqual(answer["heat_push"]["sent"], 1)
		self.assertEqual(self.tokens_rung(), ["TOK-ADA"])

	def test_the_timeline_event_is_still_deduplicated_the_way_it_always_was(self):
		"""The negative control for the test above: `heat_announced_for` must not
		have loosened the fence the timeline has used since v0.19.4."""
		self.enrol()
		shift = self.start()["name"]
		self.hot(shift, hour=11, temp=92.0)
		self.hot(shift, hour=12, temp=96.0)

		self.assertEqual(len(self.events(shift, weather.THRESHOLD_EVENT)), 1)

	def test_backfilling_last_week_rings_nobody(self):
		"""CLAIM 6. A phone that buzzes about Tuesday on Thursday teaches its
		owner to ignore the one that buzzes about now."""
		self.enrol()
		shift = self.start()["name"]
		self.close(shift)
		self.api.set_archive(hours=9, temp=99.0, humidity=60.0)

		self.tool_data(
			"backfill_weather_for_shift", {"shift": shift, "from_datetime": at(6), "to_datetime": at(15)}
		)

		self.assertEqual(self.transport.calls, [])


# ── 3. what the handset is given ────────────────────────────────────────────
class ThePayloadTheHandsetActsOn(unittest.TestCase):
	"""No site and no transport: the payload is a data structure, and every claim
	here is about what is in it rather than about who received it."""

	def payload(self, **overrides):
		arguments = {
			"shift": "SHIFT-2026-0114",
			"location": "Block 7 North",
			"temp_f": 92.4,
			"heat_index_f": 101.7,
			"reading_datetime": "2026-08-26 13:45:00",
			"threshold_temp_f": 80.0,
			"threshold_heat_index_f": 80.0,
		}
		arguments.update(overrides)
		return push_service.heat_payload(**arguments)

	def test_it_carries_the_shift_the_handset_should_open(self):
		payload = self.payload()
		self.assertEqual(payload["shift"], "SHIFT-2026-0114")
		self.assertEqual(payload["phase"], "heat")
		self.assertEqual(payload["aps"]["category"], push_service.CATEGORY_HEAT)

	def test_the_action_names_a_route_this_app_actually_publishes(self):
		"""A payload naming a screen invented for the payload is a contract with
		nobody on the other end of it. `log_shift_break` is a real mobile endpoint
		and `Cool-Down` is a real `BREAK_KINDS` entry."""
		from erpnext_mcp.tools import shifts as shift_tools

		action = self.payload()["action"]
		self.assertEqual(action["shift"], "SHIFT-2026-0114")
		self.assertEqual(action["endpoint"], "log_shift_break")
		self.assertIn(action["break_kind"], shift_tools.BREAK_KINDS)

	def test_both_temperatures_are_on_the_lock_screen_and_not_only_in_the_keys(self):
		"""The lock screen is where "do I stop the crew" is actually decided, and
		a key the app has to be opened to read is not on the lock screen."""
		body = self.payload()["aps"]["alert"]["body"]
		self.assertIn("Block 7 North", body)
		self.assertIn("92°F", body)
		self.assertIn("102°F", body)

	def test_the_numbers_are_also_carried_as_numbers(self):
		payload = self.payload()
		self.assertEqual(payload["temp_f"], 92.4)
		self.assertEqual(payload["heat_index_f"], 101.7)
		self.assertEqual(payload["threshold_temp_f"], 80.0)
		self.assertEqual(payload["reading_datetime"], "2026-08-26 13:45:00")

	def test_the_ambient_reading_is_carried_under_the_name_ios_asks_for_too(self):
		"""`shifts.describe_event_row` carries both spellings from one column for
		this exact reason: a server answering with one name while the client reads
		the other is the failure v0.96.0 was seven instances of."""
		payload = self.payload()
		self.assertEqual(payload["ambient_temp_f"], payload["temp_f"])

	def test_it_pierces_do_not_disturb(self):
		"""CLAIM 5, and it is a deliberate spend of the scarcest thing this app
		has. A task and a nightly alert do NOT get this level — see
		`INTERRUPTION_ACTIVE` — because a server that overrides a foreman's Focus
		nightly is silenced by the second week, and the break horn with it."""
		self.assertEqual(self.payload()["aps"]["interruption-level"], push_service.INTERRUPTION_LEVEL)

	def test_it_does_not_spend_a_break_tone(self):
		"""The two .caf files mean "stop work" and "resume" to a whole crew. This
		reaches one phone and asks its owner to decide."""
		self.assertEqual(self.payload()["aps"]["sound"], push_service.SOUND_DEFAULT)

	def test_a_reading_with_no_heat_index_still_produces_a_sentence(self):
		payload = self.payload(heat_index_f=None)
		self.assertIn("92°F", payload["aps"]["alert"]["body"])
		self.assertNotIn("heat_index_f", payload)

	def test_a_shift_with_no_location_still_produces_a_sentence(self):
		payload = self.payload(location="")
		self.assertIn("92°F", payload["aps"]["alert"]["body"])
		self.assertNotIn("location", payload)


# ── 4. nothing it does can cost the reading ─────────────────────────────────
class TheReadingSurvivesTheHorn(HeatPushTestCase):
	def test_a_foreman_with_no_handset_still_gets_the_timeline_event(self):
		"""`no_tokens` rather than silence: "they never enrolled a phone" is a
		fixable thing somebody has to be able to see."""
		shift = self.start()["name"]
		answer = self.hot(shift)

		self.assertEqual(answer["heat_push"]["reason"], "no_tokens")
		self.assertEqual(answer["heat_push"]["sent"], 0)
		self.assertTrue(answer["event_logged"])
		self.assertEqual(len(self.events(shift, weather.THRESHOLD_EVENT)), 1)

	def test_a_bench_with_no_p8_key_still_gets_the_timeline_event(self):
		frappe.conf.clear()
		self.enrol()
		shift = self.start()["name"]
		answer = self.hot(shift)

		self.assertEqual(answer["heat_push"]["reason"], "not_configured")
		self.assertTrue(answer["event_logged"])
		self.assertEqual(self.transport.calls, [])

	def test_a_transport_that_fails_completely_still_gets_the_timeline_event(self):
		self.enrol()
		shift = self.start()["name"]
		with mock.patch.object(
			push_service, "_apns_transport", Recorder(status=503, reason="ServiceUnavailable")
		):
			answer = self.hot(shift)

		self.assertEqual(answer["heat_push"]["sent"], 0)
		self.assertEqual(answer["heat_push"]["failed"], 1)
		self.assertTrue(answer["event_logged"])
		self.assertEqual(len(self.readings(shift)), 1)

	def test_the_horn_is_reported_on_the_fetch_tool_a_person_presses(self):
		"""End to end through `fetch_weather_now`, which is what a foreman
		standing in a hot block actually calls."""
		self.enrol()
		self.api.set_current(temp=94.0, humidity=45.0)
		shift = self.start()["name"]

		data = self.tool_data("fetch_weather_now", {"shift": shift})

		self.assertEqual(data["heat_push"]["sent"], 1)
		self.assertIn("crew leader", data["note"])

	def test_a_cool_fetch_says_nothing_about_a_horn_at_all(self):
		"""The key's ABSENCE is what makes its presence readable — a hot shift
		with no `heat_push` is one that had already crossed, which is a different
		fact from a horn that reached zero handsets."""
		self.enrol()
		self.api.set_current(temp=68.0, humidity=45.0)
		shift = self.start()["name"]

		data = self.tool_data("fetch_weather_now", {"shift": shift})

		self.assertNotIn("heat_push", data)


# ── 5. the company's own threshold decides ──────────────────────────────────
class TheThresholdThatRingsIsTheCompanysOwn(HeatPushTestCase):
	def test_a_lowered_threshold_rings_on_a_reading_the_default_would_not(self):
		self.enrol()
		self.override(MAIN, heat_threshold_temp_f=75.0)
		shift = self.start()["name"]

		answer = self.hot(shift, temp=76.0, humidity=20.0)

		self.assertEqual(answer["heat_push"]["sent"], 1)
		self.assertEqual(self.transport.calls[0]["body"]["threshold_temp_f"], 75.0)

	def test_the_same_reading_on_the_default_threshold_rings_nobody(self):
		"""THE NEGATIVE CONTROL for the test above. Same reading, same minute, two
		entities, two right answers."""
		self.enrol()
		shift = self.start()["name"]

		answer = self.hot(shift, temp=76.0, humidity=20.0)

		self.assertNotIn("heat_push", answer)
		self.assertEqual(self.transport.calls, [])
