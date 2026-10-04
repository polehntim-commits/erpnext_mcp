# SPDX-License-Identifier: MIT
"""v0.99.0 — the break horn that reaches the crew rather than the foreman.

`BreakAlarm` on the handset plays a tone the instant a foreman calls a break,
over an audio session that rings through the silent switch. That is one phone.
Every other worker on the shift learned about the break when somebody shouted.
This is the other twenty phones, and these are the seven claims it rests on.

1. **THE DEVICE IS THE IDENTITY AND THE TOKEN IS NOT.** `TheDeviceIsTheIdentity`.
   Apple rotates a device token whenever the app is reinstalled or the phone is
   restored, and iOS calls `register_push_token` on every launch. A register
   that appended would hold a season of dead rows per handset, each one a wasted
   request on every crew push. Same device_id and platform is the same row.

2. **NOTHING IS DELETED.** `RetiringAHandset`. Logout clears `is_active` and
   keeps the row, because "this phone stopped receiving, on this date, for this
   reason" is the fact somebody needs when a worker reports a silent handset.

3. **A PHONE ENROLS ITSELF.** `TheSurfaceEnrolsOnlyItsOwnCaller`. Neither mobile
   wrapper declares `user` or `employee`, so `routes.bind` drops both and no
   body can point a registration at a colleague and have their break horns
   delivered to a handset of its choosing. Asserted by INSPECTING the signature,
   which is the only form of that claim a later edit cannot quietly falsify.

4. **AN UNCONFIGURED SITE LOGS THE BREAK AND SENDS NOTHING.**
   `WhatTheSiteHasBeenTold` and `SendingAndFailing`. The p8 key is an operator
   artefact that does not exist yet; the break record is compliance evidence
   under OAR 437-004-1131. The first must never be able to cost the second.

5. **APPLE'S WORD RETIRES A TOKEN, AND NOTHING ELSE DOES.** `SendingAndFailing`.
   `Unregistered` and `BadDeviceToken` deactivate the row. `TopicDisallowed` and
   the 5xxs deliberately do not — they say something about this farm's
   configuration, and treating them alike would unsubscribe a whole crew over
   one wrong line in site_config.json.

6. **A CREW BREAK PUSHES; AN INDIVIDUAL BREAK DOES NOT.**
   `TheBreakHornReachesTheCrew`. A break covering one named worker is not news
   to the other nineteen, and a tone that rings through a silent switch is not a
   thing to send to somebody it is not about.

7. **THE TWO ADMIN TOOLS ANSWER "WHY IS THEIR PHONE QUIET".** `TheAdminTools`.
   The register, with the site's own APNs state beside it — because the
   commonest answer to that question is not on any row.
"""

import base64
import json
import os
import tempfile
import unittest
from unittest import mock

import frappe

from erpnext_mcp import roles
from erpnext_mcp.alerts import base as alerts_base
from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.erpnext_mcp.doctype.mobile_push_token import mobile_push_token as token_doctype
from erpnext_mcp.errors import ToolError
from erpnext_mcp.farmops_api import routes as farmops_routes
from erpnext_mcp.services import push as push_service
from erpnext_mcp.tools import push as push_tools

from .fixtures import MAIN, V12TestCase, install_hrms
from .harness import ROLES, STORE, set_roles
from .test_api_mobile import ON as MOBILE_ON
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase

PUSH_TOKEN = "Mobile Push Token"

FOREMAN = "HR-EMP-00001"  # Ada Orchard, at MAIN
PICKER = "HR-EMP-00002"  # Ben Packhouse, at MAIN

ADA = "ada@example.test"
BEN = "ben.push@example.test"

ON = {
	f"allow_{name}": 1
	for name in (
		"list_push_tokens",
		"send_test_push",
		"start_shift",
		"add_worker_to_shift",
		"remove_worker_from_shift",
		"log_shift_break",
		"end_shift_break",
		"get_shift",
		# v0.107.0 — the two paths that push something other than a break horn.
		"create_parcel",
		"create_housing_unit",
		"create_farm_task",
		"assign_farm_task",
		"refresh_compliance_alerts",
	)
}


#: A real ES256 key, generated once per run. THE SIGNING IS NOT MOCKED: a
#: provider token is the one piece of this pipeline that fails silently and
#: identically whether the key is wrong, the curve is wrong, or the signature is
#: DER where Apple wanted raw r||s — and the last of those produces a perfectly
#: well-formed JWT that Apple rejects with InvalidProviderToken. A test that
#: stubbed `provider_token` would pass with that bug in place.
def _test_key() -> str:
	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric import ec

	key = ec.generate_private_key(ec.SECP256R1())
	return key.private_bytes(
		serialization.Encoding.PEM,
		serialization.PrivateFormat.PKCS8,
		serialization.NoEncryption(),
	).decode("ascii")


try:
	TEST_KEY = _test_key()
except Exception:  # pragma: no cover - a bench without cryptography
	TEST_KEY = ""


def _unb64(segment: str) -> bytes:
	"""One base64url JWT segment, with the padding a JWT strips put back."""
	return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def apns_conf(**overrides) -> dict:
	conf = {
		"apns_key": TEST_KEY,
		"apns_key_id": "ABC1234567",
		"apns_team_id": "TEAM123456",
		"apns_topic": "farm.fafo.scanclock",
		"apns_environment": "sandbox",
	}
	conf.update(overrides)
	return conf


class Recorder:
	"""A transport that answers whatever it is told to and remembers every call.

	The seam the module's docstring argues for: the decisions worth testing —
	which tokens are addressed, what a 410 does, whether an unconfigured site
	even gets this far — are all above the network and unreachable behind a real
	HTTP client no test may call.
	"""

	def __init__(self, status=200, reason=""):
		self.status = status
		self.reason = reason
		self.calls = []

	def __call__(self, url, headers, body, timeout=10.0):
		self.calls.append({"url": url, "headers": headers, "body": body})
		if callable(self.status):
			return self.status(url, headers, body)
		return {"status": self.status, "reason": self.reason}


class PushTestCase(V12TestCase):
	"""A site with two workers, two logins, and the switches this suite drives."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()
		STORE.seed(
			"User",
			[
				{"name": ADA, "email": ADA, "enabled": 1, "full_name": "Ada Orchard"},
				{"name": BEN, "email": BEN, "enabled": 1, "full_name": "Ben Packhouse"},
			],
		)
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	# -- helpers -------------------------------------------------------------
	def register(self, **overrides):
		payload = {
			"user": ADA,
			"employee": FOREMAN,
			"token": "AAAA1111",
			"device_id": "DEVICE-ADA",
			"platform": "ios",
		}
		payload.update(overrides)
		return push_tools.register_push_token(payload).data

	def rows(self) -> list:
		return [dict(row) for row in STORE.rows(PUSH_TOKEN)]

	def row(self, name: str) -> dict:
		return dict(STORE.get_raw(PUSH_TOKEN, name) or {})


# ── 1. the upsert ───────────────────────────────────────────────────────────
class TheDeviceIsTheIdentity(PushTestCase):
	def test_a_first_registration_creates_one_row(self):
		answer = self.register()
		self.assertTrue(answer["created"])
		self.assertFalse(answer["token_rotated"])
		self.assertEqual(len(self.rows()), 1)

		stored = answer["push_token"]
		self.assertEqual(stored["user"], ADA)
		self.assertEqual(stored["employee"], FOREMAN)
		self.assertEqual(stored["device_key"], "ios::DEVICE-ADA")
		self.assertEqual(stored["is_active"], 1)
		self.assertTrue(stored["registered_at"])
		self.assertTrue(stored["last_used_at"])

	def test_the_same_device_launching_again_updates_the_row_it_already_has(self):
		"""The claim the whole doctype is shaped around. iOS calls this on every
		launch; a register that appended would have a season of rows per phone."""
		first = self.register()
		second = self.register()

		self.assertTrue(first["created"])
		self.assertFalse(second["created"])
		self.assertEqual(len(self.rows()), 1)
		self.assertEqual(second["push_token"]["name"], first["push_token"]["name"])

	def test_a_rotated_token_is_written_and_reported(self):
		self.register(token="AAAA1111")
		answer = self.register(token="BBBB2222")

		self.assertTrue(answer["token_rotated"])
		self.assertEqual(answer["push_token"]["token"], "BBBB2222")
		self.assertEqual(len(self.rows()), 1)

	def test_the_same_token_presented_twice_is_not_a_rotation(self):
		"""The negative control for the assertion above: `token_rotated` has to
		mean something, and a flag that was true on every launch would not."""
		self.register(token="AAAA1111")
		self.assertFalse(self.register(token="AAAA1111")["token_rotated"])

	def test_one_device_id_on_two_platforms_is_two_rows(self):
		"""The key is the PAIR. An Android and an iOS handset are entitled to
		report the same identifier, and a key of device_id alone would have one
		of them overwriting the other's token forever."""
		self.register(device_id="SHARED", platform="ios")
		self.register(device_id="SHARED", platform="android", token="ANDROID-TOK")

		self.assertEqual(len(self.rows()), 2)
		self.assertEqual(
			sorted(row["device_key"] for row in self.rows()),
			["android::SHARED", "ios::SHARED"],
		)

	def test_platform_is_normalised_so_one_phone_is_one_row(self):
		"""A client sending `iOS` and one sending `ios` are the same handset."""
		self.register(platform="ios")
		self.register(platform="iOS", token="SECOND")

		self.assertEqual(len(self.rows()), 1)
		self.assertEqual(self.rows()[0]["token"], "SECOND")

	def test_a_launch_that_carries_no_employee_does_not_erase_the_link(self):
		"""A registration arriving before the Employee record was linked must not
		blank a link a previous one established: an employee-less token is a token
		no crew push will ever reach."""
		self.register(employee=FOREMAN)
		answer = self.register(employee=None, token="LATER")

		self.assertEqual(answer["push_token"]["employee"], FOREMAN)

	def test_re_registering_reactivates_a_handset_that_had_logged_out(self):
		"""Logging back in IS the consent to be pushed to — the app asks the OS
		for permission and the server never sees the answer, so a handset calling
		this method is by definition one the worker has signed into."""
		self.register()
		push_tools.unregister_push_token({"user": ADA, "device_id": "DEVICE-ADA", "platform": "ios"})
		self.assertEqual(self.rows()[0]["is_active"], 0)

		answer = self.register()
		self.assertEqual(answer["push_token"]["is_active"], 1)
		self.assertEqual(len(self.rows()), 1)

	def test_an_unknown_platform_is_refused(self):
		with self.assertRaises(ToolError) as caught:
			self.register(platform="blackberry")
		self.assertIn("platform must be one of", str(caught.exception))
		self.assertEqual(self.rows(), [])

	def test_a_device_id_longer_than_the_cap_is_refused(self):
		with self.assertRaises(ToolError) as caught:
			self.register(device_id="x" * (token_doctype.MAX_DEVICE_ID + 1))
		self.assertIn("maximum is", str(caught.exception))
		self.assertEqual(self.rows(), [])

	def test_a_registration_with_no_token_is_refused(self):
		with self.assertRaises(ToolError):
			self.register(token="")
		self.assertEqual(self.rows(), [])


# ── 2. the soft delete ──────────────────────────────────────────────────────
class RetiringAHandset(PushTestCase):
	def test_logout_clears_the_flag_and_keeps_the_row(self):
		self.register()
		answer = push_tools.unregister_push_token(
			{"user": ADA, "device_id": "DEVICE-ADA", "platform": "ios"}
		).data

		self.assertTrue(answer["deactivated"])
		self.assertTrue(answer["found"])
		self.assertEqual(len(self.rows()), 1)
		stored = self.rows()[0]
		self.assertEqual(stored["is_active"], 0)
		self.assertEqual(stored["last_error"], "unregistered")

	def test_a_retired_handset_is_not_sent_to(self):
		self.register()
		push_tools.unregister_push_token({"user": ADA, "device_id": "DEVICE-ADA", "platform": "ios"})
		self.assertEqual(push_service.active_tokens_for_employees([FOREMAN]), [])

	def test_a_device_this_site_has_never_seen_is_answered_rather_than_refused(self):
		"""A phone logging out on a bad signal before its registration landed is a
		normal thing to happen, and an error dialog on a screen the worker is
		already leaving helps nobody."""
		answer = push_tools.unregister_push_token(
			{"user": ADA, "device_id": "NEVER-SEEN", "platform": "ios"}
		).data
		self.assertFalse(answer["found"])
		self.assertFalse(answer["deactivated"])

	def test_one_login_cannot_retire_another_login_s_handset(self):
		"""Without this, a caller who guessed a device key could take a colleague
		off every crew push for the rest of the season."""
		self.register(user=ADA, device_id="DEVICE-ADA")
		with self.assertRaises(ToolError) as caught:
			push_tools.unregister_push_token({"user": BEN, "device_id": "DEVICE-ADA", "platform": "ios"})
		self.assertIn("registered to another account", str(caught.exception))
		self.assertEqual(self.rows()[0]["is_active"], 1)

	def test_logging_out_twice_is_not_an_error(self):
		self.register()
		push_tools.unregister_push_token({"user": ADA, "device_id": "DEVICE-ADA", "platform": "ios"})
		again = push_tools.unregister_push_token(
			{"user": ADA, "device_id": "DEVICE-ADA", "platform": "ios"}
		).data
		self.assertTrue(again["was_already_inactive"])
		self.assertEqual(len(self.rows()), 1)


# ── 3. what the site has been told ──────────────────────────────────────────
class WhatTheSiteHasBeenTold(unittest.TestCase):
	"""`apns_config` is pure and takes its conf as an argument, so this class
	needs no site at all."""

	def test_a_bench_with_nothing_configured_is_not_configured(self):
		config = push_service.apns_config({})
		self.assertFalse(config["configured"])
		self.assertEqual(config["environment"], "production")

	def test_all_four_facts_are_required_and_each_one_alone_is_not_enough(self):
		"""ANDed rather than one flag an operator could tick while leaving the key
		out. A push missing any of the four is rejected by Apple with a 403 that
		says nothing useful, which is a worse failure than not sending."""
		self.assertTrue(push_service.apns_config(apns_conf())["configured"])
		for missing in ("apns_key", "apns_key_id", "apns_team_id", "apns_topic"):
			with self.subTest(missing=missing):
				self.assertFalse(push_service.apns_config(apns_conf(**{missing: ""}))["configured"])

	def test_the_key_may_be_a_path_on_the_bench(self):
		handle = tempfile.NamedTemporaryFile("w", suffix=".p8", delete=False)
		handle.write(TEST_KEY)
		handle.close()
		self.addCleanup(os.unlink, handle.name)

		config = push_service.apns_config(apns_conf(apns_key=handle.name))
		self.assertTrue(config["configured"])
		self.assertIn("PRIVATE KEY", config["key"])

	def test_a_path_that_cannot_be_read_leaves_the_site_unconfigured(self):
		"""Rather than a signing failure at push time, which would surface in the
		middle of a break instead of when somebody was looking at the config."""
		config = push_service.apns_config(apns_conf(apns_key="/nonexistent/apns.p8"))
		self.assertFalse(config["configured"])

	def test_the_environment_picks_the_host_and_a_typo_falls_back(self):
		self.assertEqual(
			push_service.apns_config(apns_conf(apns_environment="sandbox"))["host"],
			"https://api.sandbox.push.apple.com",
		)
		self.assertEqual(
			push_service.apns_config(apns_conf(apns_environment="staging"))["host"],
			"https://api.push.apple.com",
		)


# ── 4. the payload the handset plays ────────────────────────────────────────
class ThePayloadTheHandsetPlays(unittest.TestCase):
	def test_a_break_starting_names_the_start_tone(self):
		payload = push_service.break_payload("Paid Rest", "start", 10, "SHIFT-0001", "EV-1")
		self.assertEqual(payload["aps"]["sound"], "break_start.caf")
		self.assertEqual(payload["aps"]["interruption-level"], "time-sensitive")
		self.assertIn("10 minutes", payload["aps"]["alert"]["body"])

	def test_a_break_ending_names_the_end_tone(self):
		payload = push_service.break_payload("Paid Rest", "end", 10, "SHIFT-0001", "EV-1")
		self.assertEqual(payload["aps"]["sound"], "break_end.caf")
		self.assertEqual(payload["phase"], "end")

	def test_the_two_tones_are_never_the_same_file(self):
		"""They are a rising double blast and a descending triple pip on purpose:
		a worker in an orchard has to know which one they just heard without
		taking the phone out of a pocket."""
		self.assertNotEqual(push_service.SOUND_BREAK_START, push_service.SOUND_BREAK_END)

	def test_the_custom_keys_sit_beside_aps_and_not_inside_it(self):
		"""Apple owns `aps` and will one day add a key this app already uses."""
		payload = push_service.break_payload("Cool-Down", "start", 15, "SHIFT-0001", "EV-9")
		self.assertEqual(payload["break_kind"], "Cool-Down")
		self.assertEqual(payload["shift"], "SHIFT-0001")
		self.assertEqual(payload["event"], "EV-9")
		for key in ("break_kind", "shift", "event", "phase"):
			self.assertNotIn(key, payload["aps"])

	def test_a_break_with_no_duration_still_produces_a_sentence(self):
		body = push_service.break_payload("Water Break", "start")["aps"]["alert"]["body"]
		self.assertIn("Water Break", body)
		self.assertNotIn("None", body)


# ── 5. the dispatch ─────────────────────────────────────────────────────────
class SendingAndFailing(PushTestCase):
	def tokens(self) -> list:
		self.register(user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA")
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		return push_service.active_tokens_for_employees([FOREMAN, PICKER])

	def test_one_request_per_handset_at_the_configured_host(self):
		transport = Recorder()
		report = push_service.send_push(self.tokens(), {"aps": {}}, transport=transport, conf=apns_conf())

		self.assertEqual(report["sent"], 2)
		self.assertEqual(report["failed"], 0)
		self.assertEqual(report["reason"], "sent")
		self.assertEqual(
			sorted(call["url"] for call in transport.calls),
			[
				"https://api.sandbox.push.apple.com/3/device/TOK-ADA",
				"https://api.sandbox.push.apple.com/3/device/TOK-BEN",
			],
		)

	def test_the_headers_carry_the_topic_a_provider_token_and_the_urgent_priority(self):
		transport = Recorder()
		push_service.send_push(self.tokens(), {"aps": {}}, transport=transport, conf=apns_conf())

		headers = transport.calls[0]["headers"]
		self.assertEqual(headers["apns-topic"], "farm.fafo.scanclock")
		self.assertEqual(headers["apns-push-type"], "alert")
		# 10 is "deliver now". A break horn that arrives when the phone next
		# wakes up is not a break horn.
		self.assertEqual(headers["apns-priority"], "10")
		self.assertTrue(headers["authorization"].startswith("bearer "))

		header, claims, signature = headers["authorization"][len("bearer ") :].split(".")
		self.assertEqual(json.loads(_unb64(header)), {"alg": "ES256", "kid": "ABC1234567"})
		self.assertEqual(json.loads(_unb64(claims))["iss"], "TEAM123456")
		# THE SIGNATURE IS 64 RAW BYTES — r||s — AND NOT DER. `cryptography`
		# signs to DER, and handing Apple the DER blob produces a perfectly
		# well-formed JWT that it rejects with InvalidProviderToken: every push
		# fails, with nothing on this side to show for it. This assertion is the
		# only place that difference is visible without an Apple account.
		self.assertEqual(len(_unb64(signature)), 64)

	def test_an_unconfigured_site_never_reaches_the_transport(self):
		"""THE NEGATIVE CONTROL FOR THE WHOLE FEATURE. Until Tim puts a p8 key on
		the bench this is the only path that runs, and it has to be a named skip
		rather than an exception, a hang, or a silent zero."""
		transport = Recorder()
		report = push_service.send_push(self.tokens(), {"aps": {}}, transport=transport, conf={})

		self.assertEqual(report["reason"], "not_configured")
		self.assertEqual(report["skipped"], 2)
		self.assertEqual(report["sent"], 0)
		self.assertEqual(transport.calls, [])

	def test_apple_saying_unregistered_retires_that_handset(self):
		tokens = self.tokens()
		transport = Recorder(status=410, reason="Unregistered")
		report = push_service.send_push(tokens, {"aps": {}}, transport=transport, conf=apns_conf())

		self.assertEqual(report["deactivated"], 2)
		self.assertTrue(all(row["is_active"] == 0 for row in self.rows()))
		self.assertTrue(all(row["last_error"] == "Unregistered" for row in self.rows()))

	def test_a_bad_device_token_retires_that_handset_too(self):
		tokens = self.tokens()
		push_service.send_push(
			tokens, {"aps": {}}, transport=Recorder(status=400, reason="BadDeviceToken"), conf=apns_conf()
		)
		self.assertTrue(all(row["is_active"] == 0 for row in self.rows()))

	def test_a_configuration_failure_does_not_unsubscribe_the_crew(self):
		"""THE COUNTERPART TO THE TWO ABOVE, and the one that matters most.
		`TopicDisallowed` says the bundle id in site_config.json is wrong — it is
		true of every handset on the farm, and deactivating on it would silence
		the whole crew over one line an operator can fix in a minute."""
		tokens = self.tokens()
		report = push_service.send_push(
			tokens, {"aps": {}}, transport=Recorder(status=400, reason="TopicDisallowed"), conf=apns_conf()
		)

		self.assertEqual(report["failed"], 2)
		self.assertEqual(report["deactivated"], 0)
		self.assertTrue(all(row["is_active"] == 1 for row in self.rows()))

	def test_a_transport_that_raises_is_a_failed_send_and_not_an_exception(self):
		def explode(url, headers, body, timeout=10.0):
			raise RuntimeError("connection reset")

		report = push_service.send_push(self.tokens(), {"aps": {}}, transport=explode, conf=apns_conf())
		self.assertEqual(report["failed"], 2)
		self.assertEqual(report["sent"], 0)

	def test_a_row_with_no_token_string_is_skipped_rather_than_addressed(self):
		self.register(user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA")
		frappe.db.set_value(PUSH_TOKEN, self.rows()[0]["name"], "token", "")
		transport = Recorder()
		report = push_service.send_push(
			push_service.active_tokens_for_employees([FOREMAN]),
			{"aps": {}},
			transport=transport,
			conf=apns_conf(),
		)
		self.assertEqual(report["skipped"], 1)
		self.assertEqual(transport.calls, [])

	def test_no_tokens_at_all_says_so(self):
		report = push_service.send_push([], {"aps": {}}, transport=Recorder(), conf=apns_conf())
		self.assertEqual(report["reason"], "no_tokens")
		self.assertEqual(report["attempted"], 0)


# ── 6. who hears it ─────────────────────────────────────────────────────────
class WhoHearsIt(PushTestCase):
	def a_shift(self, crew=(FOREMAN, PICKER)) -> str:
		return self.tool_data(
			"start_shift",
			{
				"foreman": FOREMAN,
				"location": "Block 7 North",
				"shift_type": "Harvest",
				"start_datetime": f"{frappe.utils.today()} 06:00:00",
				"crew_employees": list(crew),
			},
		)["name"]

	def test_the_crew_is_read_through_the_shift_document(self):
		shift = self.a_shift()
		self.assertEqual(sorted(push_service.shift_crew_employees(shift)), sorted([FOREMAN, PICKER]))

	def test_a_worker_who_clocked_out_is_not_sent_to(self):
		"""Their phone is elsewhere, and a tone that rings through the silent
		switch is not a thing to send to somebody who went home."""
		shift = self.a_shift()
		self.tool_data("remove_worker_from_shift", {"shift": shift, "employee": PICKER})
		self.assertEqual(push_service.shift_crew_employees(shift), [FOREMAN])

	def test_an_unknown_shift_is_nobody_rather_than_an_exception(self):
		self.assertEqual(push_service.shift_crew_employees("SHIFT-NOPE"), [])
		self.assertEqual(push_service.shift_crew_employees(""), [])

	def test_an_empty_crew_issues_no_query_at_all(self):
		"""THE NEGATIVE CONTROL. Frappe reads `{"in": []}` as no filter at all on
		some backends, which would turn a crew of nobody into a push to the whole
		farm — the single worst outcome this module could produce."""
		with mock.patch.object(frappe.db, "get_all") as queried:
			self.assertEqual(push_service.active_tokens_for_employees([]), [])
			self.assertEqual(push_service.active_tokens_for_employees(["", "  "]), [])
		queried.assert_not_called()

	def test_only_active_tokens_are_returned(self):
		self.register(user=ADA, employee=FOREMAN, device_id="LIVE", token="LIVE-TOK")
		self.register(user=ADA, employee=FOREMAN, device_id="DEAD", token="DEAD-TOK")
		push_tools.unregister_push_token({"user": ADA, "device_id": "DEAD", "platform": "ios"})

		found = push_service.active_tokens_for_employees([FOREMAN])
		self.assertEqual([row["device_id"] for row in found], ["LIVE"])


# ── 7. the trigger ──────────────────────────────────────────────────────────
class TheBreakHornReachesTheCrew(PushTestCase):
	def setUp(self):
		super().setUp()
		self.transport = Recorder()
		patched = mock.patch.object(push_service, "_apns_transport", self.transport)
		patched.start()
		self.addCleanup(patched.stop)
		frappe.conf.update(apns_conf())
		self.addCleanup(frappe.conf.clear)
		push_service._JWT_CACHE.clear()

		self.register(user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA")
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		self.shift = self.tool_data(
			"start_shift",
			{
				"foreman": FOREMAN,
				"location": "Block 7 North",
				"shift_type": "Harvest",
				"start_datetime": f"{frappe.utils.today()} 06:00:00",
				"crew_employees": [FOREMAN, PICKER],
			},
		)["name"]

	def sounds(self) -> list:
		return [call["body"]["aps"]["sound"] for call in self.transport.calls]

	def test_a_crew_break_rings_every_handset_on_the_shift(self):
		answer = self.tool_data(
			"log_shift_break",
			{"shift": self.shift, "break_kind": "Paid Rest", "duration_minutes": 10},
		)

		self.assertEqual(answer["push"]["crew"], 2)
		self.assertEqual(answer["push"]["tokens"], 2)
		self.assertEqual(answer["push"]["sent"], 2)
		self.assertEqual(self.sounds(), ["break_start.caf", "break_start.caf"])

	def test_the_payload_carries_the_event_the_handset_ends_the_break_with(self):
		answer = self.tool_data(
			"log_shift_break",
			{"shift": self.shift, "break_kind": "Paid Rest", "duration_minutes": 10},
		)
		event = answer["logged"]["event"]
		self.assertTrue(event)
		self.assertEqual(self.transport.calls[0]["body"]["event"], event)
		self.assertEqual(self.transport.calls[0]["body"]["shift"], self.shift)

	def test_ending_a_crew_break_rings_the_other_tone(self):
		started = self.tool_data(
			"log_shift_break",
			{"shift": self.shift, "break_kind": "Paid Rest", "duration_minutes": 10},
		)
		self.transport.calls.clear()

		ended = self.tool_data("end_shift_break", {"shift": self.shift, "event": started["logged"]["event"]})
		self.assertEqual(ended["push"]["sent"], 2)
		self.assertEqual(self.sounds(), ["break_end.caf", "break_end.caf"])

	def test_an_individual_break_rings_nobody(self):
		"""A break covering one named worker is not news to the other nineteen."""
		answer = self.tool_data(
			"log_shift_break",
			{
				"shift": self.shift,
				"break_kind": "Unpaid Meal",
				"applies_to": "Individual",
				"employee": PICKER,
				"duration_minutes": 30,
			},
		)
		self.assertEqual(answer["push"]["reason"], "individual_break")
		self.assertEqual(self.transport.calls, [])

	def test_a_heat_break_reaches_the_crew_like_any_other(self):
		"""The kinds v0.96.0 added are the ones the heat rules are written about,
		and they are exactly the breaks somebody in a hot block needs told about."""
		answer = self.tool_data(
			"log_shift_break",
			{"shift": self.shift, "break_kind": "Shade Break", "duration_minutes": 15},
		)
		self.assertEqual(answer["push"]["sent"], 2)
		self.assertEqual(self.transport.calls[0]["body"]["break_kind"], "Shade Break")

	def test_a_push_that_fails_completely_never_costs_the_break_record(self):
		"""THE INVARIANT THE WHOLE TRIGGER IS SUBORDINATE TO. The break log IS the
		evidence heat relief was provided under OAR 437-004-1131; the push is a
		convenience on top of it."""
		with mock.patch.object(push_service, "send_push_to_shift_crew", side_effect=RuntimeError("boom")):
			answer = self.tool_data(
				"log_shift_break",
				{"shift": self.shift, "break_kind": "Cool-Down", "duration_minutes": 15},
			)

		self.assertIn("error:", answer["push"]["reason"])
		events = [
			row
			for row in self.tool_data("get_shift", {"farm_shift": self.shift})["compliance_events"]
			if row.get("break_kind") == "Cool-Down"
		]
		self.assertEqual(len(events), 1)

	def test_an_unconfigured_bench_logs_the_break_and_sends_nothing(self):
		frappe.conf.clear()
		answer = self.tool_data(
			"log_shift_break",
			{"shift": self.shift, "break_kind": "Paid Rest", "duration_minutes": 10},
		)
		self.assertEqual(answer["push"]["reason"], "not_configured")
		self.assertEqual(self.transport.calls, [])
		self.assertEqual(answer["logged"]["break_kind"], "Paid Rest")

	def test_a_crew_with_no_enrolled_handsets_is_reported_as_such(self):
		"""`crew` and `tokens` are separate numbers on purpose: eight workers with
		two tokens is six people who never enrolled a phone, which is a different
		conversation from a push that failed."""
		for row in self.rows():
			frappe.db.set_value(PUSH_TOKEN, row["name"], "is_active", 0)

		answer = self.tool_data(
			"log_shift_break",
			{"shift": self.shift, "break_kind": "Paid Rest", "duration_minutes": 10},
		)
		self.assertEqual(answer["push"]["crew"], 2)
		self.assertEqual(answer["push"]["tokens"], 0)
		self.assertEqual(answer["push"]["reason"], "no_tokens")


# ── 8. the mobile surface ───────────────────────────────────────────────────
class TheSurfaceEnrolsOnlyItsOwnCaller(MobileAPITestCase):
	"""Subclassed from the mobile case rather than from `PushTestCase`, because
	what is under test here is the ROUTE — the scope gate, the subject resolution
	and what `routes.bind` will and will not pass through."""

	def setUp(self):
		super().setUp()
		# MERGED WITH THE MOBILE CASE'S OWN SET rather than replacing it:
		# `configure` writes the whole Single, so passing only this file's
		# switches would turn `create_mobile_user` off underneath the base
		# class's `enrol` helper.
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **{**MOBILE_ON, **ON})

	def rows(self) -> list:
		return [dict(row) for row in STORE.rows(PUSH_TOKEN)]

	def test_the_wrapper_declares_no_subject_at_all(self):
		"""THE WHOLE OF THE SECURITY, asserted by inspecting the signature rather
		than by driving a body past it: a body naming `employee` would be a way to
		have a colleague's break horns delivered to a handset of your choosing,
		and the reason it cannot is that the argument does not exist."""
		accepted = farmops_routes.accepted_arguments(mobile_api.register_push_token)
		self.assertEqual(accepted, {"token", "device_id", "platform", "app_version", "device_model"})
		self.assertNotIn("employee", accepted)
		self.assertNotIn("user", accepted)

	def test_a_body_naming_somebody_else_is_dropped_at_the_door(self):
		route = farmops_routes.BY_PATH["/mobile/register_push_token"]
		bound = farmops_routes.bind(
			route,
			{"token": "TOK", "device_id": "DEV", "platform": "ios", "employee": "HR-EMP-00001", "user": "x"},
		)
		self.assertEqual(bound, {"token": "TOK", "device_id": "DEV", "platform": "ios"})

	def test_the_logout_wrapper_does_not_accept_a_token(self):
		"""The device is the identity. A logout that had to present the current
		token would fail exactly when it matters — a phone whose token Apple
		rotated between login and logout would go on receiving another shift's
		break horns forever."""
		accepted = farmops_routes.accepted_arguments(mobile_api.unregister_push_token)
		self.assertEqual(accepted, {"device_id", "platform"})

	def test_registering_stores_the_caller_s_own_login_and_employee(self):
		self.be(WORKER)
		answer = mobile_api.register_push_token(token="PHONE-TOK", device_id="PHONE-1", platform="ios")

		self.assertTrue(answer["created"])
		self.assertEqual(answer["device"]["employee"], WORKER_EMPLOYEE)
		self.assertEqual(self.rows()[0]["user"], WORKER)
		self.assertEqual(self.rows()[0]["token"], "PHONE-TOK")

	def test_the_device_token_is_never_echoed_back_to_the_handset(self):
		"""`guard.strip_secrets` removes every token-shaped key on the way out of
		this surface, so a pass-through response would have holes in it whose
		names depended on a hint list two files away. The app does not need it —
		it is the thing that just sent it."""
		self.be(WORKER)
		answer = mobile_api.register_push_token(token="PHONE-TOK", device_id="PHONE-1", platform="ios")

		self.assertNotIn("token", answer)
		self.assertNotIn("push_token", answer)
		self.assertNotIn("token", answer["device"])
		self.assertNotIn("PHONE-TOK", str(answer))

	def test_the_answer_says_whether_this_bench_can_deliver_anything(self):
		"""False means the app should go on playing its own local tone rather
		than expecting one to arrive."""
		self.be(WORKER)
		answer = mobile_api.register_push_token(token="TOK", device_id="PHONE-1", platform="ios")
		self.assertFalse(answer["push_enabled"])

	def test_the_second_launch_of_the_day_does_not_add_a_row(self):
		self.be(WORKER)
		mobile_api.register_push_token(token="TOK-1", device_id="PHONE-1", platform="ios")
		mobile_api.register_push_token(token="TOK-2", device_id="PHONE-1", platform="ios")
		self.assertEqual(len(self.rows()), 1)
		self.assertEqual(self.rows()[0]["token"], "TOK-2")

	def test_platform_defaults_to_ios_when_the_handset_omits_it(self):
		self.be(WORKER)
		answer = mobile_api.register_push_token(token="TOK", device_id="PHONE-1")
		self.assertEqual(answer["device"]["platform"], "ios")

	def test_logging_out_retires_the_handset(self):
		self.be(WORKER)
		mobile_api.register_push_token(token="TOK", device_id="PHONE-1", platform="ios")
		answer = mobile_api.unregister_push_token(device_id="PHONE-1", platform="ios")

		self.assertTrue(answer["deactivated"])
		self.assertEqual(answer["device"]["device_id"], "PHONE-1")
		self.assertEqual(self.rows()[0]["is_active"], 0)

	def test_a_login_with_no_employee_record_still_gets_a_row(self):
		"""Refusing here would be worse than storing a row no crew push reaches
		yet: the handset has already asked the OS for notification permission and
		will not ask again, and the row is repaired the moment somebody links the
		Employee record."""
		unlinked = "casual@example.test"
		self.enrol(email=unlinked, name="Casual Picker")
		self.be(unlinked)
		answer = mobile_api.register_push_token(token="TOK", device_id="PHONE-1", platform="ios")

		self.assertTrue(answer["created"])
		self.assertFalse(answer["device"]["employee"])
		self.assertEqual(self.rows()[0]["user"], unlinked)

	def test_both_routes_are_on_the_table_and_both_are_mutating(self):
		for path in ("/mobile/register_push_token", "/mobile/unregister_push_token"):
			with self.subTest(path=path):
				self.assertIn(path, farmops_routes.BY_PATH)
				self.assertTrue(farmops_routes.BY_PATH[path].mutating)

	def test_the_kill_switch_stops_them_like_every_other_method(self):
		self.configure(enabled=1, farm_ops_mobile_enabled=0, **{**MOBILE_ON, **ON})
		self.be(WORKER)
		with self.assertRaises(guard.MobileDisabled):
			mobile_api.register_push_token(token="TOK", device_id="PHONE-1", platform="ios")
		with self.assertRaises(guard.MobileDisabled):
			mobile_api.unregister_push_token(device_id="PHONE-1", platform="ios")


# ── 9. the two admin tools ──────────────────────────────────────────────────
class TheAdminTools(PushTestCase):
	def setUp(self):
		super().setUp()
		self.register(user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA")
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")

	def test_the_register_lists_every_handset(self):
		answer = self.tool_data("list_push_tokens", {})
		self.assertEqual(answer["count"], 2)
		self.assertEqual(answer["active"], 2)
		self.assertEqual(answer["inactive"], 0)

	def test_it_filters_by_employee(self):
		answer = self.tool_data("list_push_tokens", {"employee": FOREMAN})
		self.assertEqual([row["device_id"] for row in answer["push_tokens"]], ["DEV-ADA"])

	def test_it_filters_by_active_state_so_the_retired_half_is_findable(self):
		push_tools.unregister_push_token({"user": BEN, "device_id": "DEV-BEN", "platform": "ios"})

		self.assertEqual(self.tool_data("list_push_tokens", {"is_active": True})["count"], 1)
		retired = self.tool_data("list_push_tokens", {"is_active": False})
		self.assertEqual([row["device_id"] for row in retired["push_tokens"]], ["DEV-BEN"])

	def test_it_says_whether_this_bench_can_send_at_all(self):
		"""The commonest answer to "why is their phone quiet" is not on any row: a
		site with no p8 key has a perfect register and sends nothing."""
		answer = self.tool_data("list_push_tokens", {})
		self.assertFalse(answer["apns"]["configured"])
		self.assertIn("apns_key", answer["apns"]["requires"])

		frappe.conf.update(apns_conf())
		self.addCleanup(frappe.conf.clear)
		configured = self.tool_data("list_push_tokens", {})
		self.assertTrue(configured["apns"]["configured"])
		self.assertEqual(configured["apns"]["requires"], "")

	def test_the_register_is_not_a_field_worker_s_read(self):
		set_roles("Administrator", ["Field Worker"])
		refusal = str(self.tool_error("list_push_tokens", {}))
		self.assertIn("may not form or close a crew shift", refusal)
		self.assertIn("Farm Manager", refusal)

	def test_a_test_push_reaches_that_worker_s_handsets_and_nobody_else_s(self):
		frappe.conf.update(apns_conf())
		self.addCleanup(frappe.conf.clear)
		push_service._JWT_CACHE.clear()
		transport = Recorder()
		with mock.patch.object(push_service, "_apns_transport", transport):
			answer = self.tool_data("send_test_push", {"employee": FOREMAN, "message": "Testing the horn."})

		self.assertEqual(answer["tokens"], 1)
		self.assertEqual(answer["result"]["sent"], 1)
		self.assertEqual(len(transport.calls), 1)
		self.assertTrue(transport.calls[0]["url"].endswith("/TOK-ADA"))
		self.assertEqual(transport.calls[0]["body"]["aps"]["alert"]["body"], "Testing the horn.")

	def test_a_test_push_on_an_unconfigured_bench_reports_rather_than_refuses(self):
		"""'This bench has no p8 key' is exactly what somebody running this tool
		is trying to find out."""
		answer = self.tool_data("send_test_push", {"employee": FOREMAN})
		self.assertEqual(answer["result"]["reason"], "not_configured")
		self.assertEqual(answer["tokens"], 1)
		self.assertFalse(answer["apns"]["configured"])

	def test_the_test_push_switch_ships_off(self):
		"""It makes a phone in somebody's pocket ring at the time-sensitive
		interruption level, which rings through Focus."""
		self.configure(allow_send_test_push=0)
		self.assertIn("send_test_push", str(self.tool_error("send_test_push", {"employee": FOREMAN})))


# ── 8. the payloads that are not a break horn ───────────────────────────────
class ThePayloadsForWorkAndForCompliance(unittest.TestCase):
	"""v0.107.0. A dispatch and a raised alert are news; neither is a break horn,
	and the payload is where that distinction is either made or lost."""

	def test_a_dispatched_task_carries_the_docname_the_handset_opens(self):
		"""Without it the notification is an instruction to go and find the thing
		it is about, which is the app the worker already was not opening."""
		payload = push_service.task_payload(task="TASK-0007", task_name="Habitability walk")

		self.assertEqual(payload["task"], "TASK-0007")
		self.assertEqual(payload["phase"], "assigned")
		self.assertEqual(payload["aps"]["alert"]["title"], "New task")
		self.assertIn("Habitability walk", payload["aps"]["alert"]["body"])

	def test_neither_new_payload_spends_a_break_tone(self):
		"""The two .caf files are LEARNED SOUNDS meaning stop work and resume.
		Spending them on paperwork is how they stop meaning anything, and the
		first thing lost is the one that matters."""
		for payload in (
			push_service.task_payload(task="TASK-1"),
			push_service.alert_payload(alert="ALERT-1", message="a cabin is overdue"),
		):
			self.assertEqual(payload["aps"]["sound"], push_service.SOUND_DEFAULT)
			self.assertNotIn(
				payload["aps"]["sound"], (push_service.SOUND_BREAK_START, push_service.SOUND_BREAK_END)
			)

	def test_neither_new_payload_pierces_do_not_disturb(self):
		"""THE CLAIM THIS RELEASE RESTS ON. `time-sensitive` is the break horn's
		and stays the break horn's: a server that overrode a foreman's Focus
		nightly would be silenced within a fortnight, and the horn with it."""
		for payload in (
			push_service.task_payload(task="TASK-1"),
			push_service.alert_payload(alert="ALERT-1", message="x"),
		):
			self.assertEqual(payload["aps"]["interruption-level"], push_service.INTERRUPTION_ACTIVE)
			self.assertNotEqual(payload["aps"]["interruption-level"], push_service.INTERRUPTION_LEVEL)

	def test_a_break_horn_still_pierces_it(self):
		"""The negative control for the test above. If this ever fails the two
		levels have been swapped, which is the one way to get this wrong that
		leaves every other assertion in this file passing."""
		payload = push_service.break_payload(break_kind="Paid Rest", phase="start")
		self.assertEqual(payload["aps"]["interruption-level"], push_service.INTERRUPTION_LEVEL)

	def test_being_given_somebody_else_s_job_reads_differently(self):
		"""Being sent to a job and having one taken off somebody and handed to
		you are the same row and different news: the second means somebody may
		already be stood in front of it."""
		plain = push_service.task_payload(task="TASK-1", task_name="Walk it")
		taken = push_service.task_payload(task="TASK-1", task_name="Walk it", reassigned=True)

		self.assertEqual(plain["aps"]["alert"]["title"], "New task")
		self.assertEqual(taken["aps"]["alert"]["title"], "Task reassigned to you")
		self.assertNotIn("reassigned", plain)
		self.assertTrue(taken["reassigned"])

	def test_urgency_reaches_the_lock_screen_and_normal_does_not(self):
		"""The lock screen is where "do I walk over there now" is decided, so a
		key the app has to be opened to read is not on the lock screen. `Normal`
		is left out because every task has it and it says nothing."""
		urgent = push_service.task_payload(task="T", task_name="Walk it", urgency="Urgent")
		normal = push_service.task_payload(task="T", task_name="Walk it", urgency="Normal")

		self.assertIn("Urgent", urgent["aps"]["alert"]["body"])
		self.assertEqual(normal["aps"]["alert"]["body"], "Walk it")

	def test_an_alert_names_the_person_it_is_about(self):
		"""Delivered to a supervisor and ABOUT a worker. "Ada Orchard" on the
		lock screen is what makes it actionable without opening anything."""
		payload = push_service.alert_payload(
			alert="ALERT-9",
			severity="Critical",
			message="Work authorization expired",
			subject_name="Ada Orchard",
			due_date="2026-08-01",
		)

		self.assertEqual(payload["aps"]["alert"]["title"], "Critical: Ada Orchard")
		self.assertIn("Work authorization expired", payload["aps"]["alert"]["body"])
		self.assertIn("due 2026-08-01", payload["aps"]["alert"]["body"])
		self.assertEqual(payload["compliance_alert"], "ALERT-9")

	def test_an_alert_about_the_operation_has_no_name_to_use(self):
		"""Most alerts are about the operation rather than a person, and a blank
		subject is a real answer rather than licence to guess one."""
		payload = push_service.alert_payload(alert="A", severity="Critical", message="water test stale")
		self.assertEqual(payload["aps"]["alert"]["title"], "Critical compliance alert")
		self.assertNotIn("subject_name", payload)

	def test_a_long_alert_message_is_shortened_rather_than_rejected(self):
		"""An APNs payload is capped at 4KB and `alert_message` is not. Trimming
		here is the difference between a shortened alert and no alert at all."""
		payload = push_service.alert_payload(alert="A", severity="Critical", message="cabin " * 300)
		body = payload["aps"]["alert"]["body"]
		self.assertLessEqual(len(body), push_service.MAX_BODY)
		self.assertTrue(body.endswith("\u2026"))

	def test_the_custom_keys_sit_beside_aps_in_both_new_payloads(self):
		"""Apple owns `aps` and will one day add a key this app already uses."""
		task = push_service.task_payload(task="T", task_name="n", location="Block 7")
		alert = push_service.alert_payload(alert="A", severity="Critical", message="m")
		for key in ("task", "task_name", "location", "phase"):
			self.assertIn(key, task)
			self.assertNotIn(key, task["aps"])
		for key in ("compliance_alert", "severity", "phase"):
			self.assertIn(key, alert)
			self.assertNotIn(key, alert["aps"])


# ── 9. who is told about an alert ───────────────────────────────────────────
class WhoIsToldAboutAnAlert(PushTestCase):
	"""`supervisor_employees` is a ROLE question, not a job-title one. This app
	has had that argument once already — `roles.capability_of` exists because a
	mobile picker filtering by `designation` offered the wrong people."""

	def setUp(self):
		super().setUp()
		# The Role documents have to exist before anybody can hold one — a
		# `Has Role` row is a Link, and the double validates it exactly as a
		# bench does.
		roles.install_roles()

	def a_login_holding(self, employee: str, login: str, role: str) -> None:
		"""Give somebody a real `Has Role` ROW, which is what the lookup reads.

		Deliberately not `set_roles`, which writes the double's ROLES dict and is
		what `frappe.get_roles` reads — a different question with a different
		answer, and faking the first while asserting the second would test the
		double rather than the app.
		"""
		user = frappe.get_doc("User", login)
		user.append("roles", {"role": role})
		user.save()
		frappe.db.set_value("Employee", employee, "user_id", login)

	def test_a_foreman_with_a_login_is_told_and_a_picker_is_not(self):
		self.a_login_holding(FOREMAN, ADA, "Foreman")
		self.a_login_holding(PICKER, BEN, "Field Worker")

		self.assertEqual(push_service.supervisor_employees(MAIN), [FOREMAN])

	def test_a_farm_manager_counts_too_and_the_set_is_the_one_the_gate_uses(self):
		"""Two frozensets in two modules are two lists that agree until somebody
		edits one, so this reads the gate's own."""
		self.a_login_holding(PICKER, BEN, "Farm Manager")
		self.assertEqual(push_service.supervisor_employees(MAIN), [PICKER])
		self.assertEqual(roles.DISPATCH_ROLES, frozenset({"Foreman", "Farm Manager"}))

	def test_a_worker_with_no_login_is_nobody_to_push_to(self):
		"""Most of a picking crew has no `user_id`, which is exactly right —
		they hold no dispatch role either."""
		frappe.db.set_value("Employee", FOREMAN, "user_id", None)
		self.assertEqual(push_service.supervisor_employees(MAIN), [])

	def test_somebody_who_has_left_is_not_told(self):
		self.a_login_holding("HR-EMP-00003", ADA, "Foreman")  # Cara Office, status Left
		self.assertEqual(push_service.supervisor_employees(MAIN), [])

	def test_the_lookup_is_scoped_to_the_alert_s_company(self):
		self.a_login_holding(FOREMAN, ADA, "Foreman")
		self.assertEqual(push_service.supervisor_employees("Somebody Else Ltd"), [])
		self.assertEqual(push_service.supervisor_employees(MAIN), [FOREMAN])

	def test_an_alert_with_no_company_reaches_every_supervisor(self):
		"""Frappe's own convention, which `roles.companies_for` states in the
		same words: an empty scope is EVERY company and not none. An alert about
		the operation has no company on it, and narrowing it to nobody would
		raise the alert, report it, and ring no phone."""
		self.a_login_holding(FOREMAN, ADA, "Foreman")
		self.assertEqual(push_service.supervisor_employees(""), [FOREMAN])


# ── 10. the dispatched worker hears about it ────────────────────────────────
class TheDispatchedWorkerIsToldAtOnce(PushTestCase):
	"""v0.107.0. A dispatched task appeared in `list_my_tasks` and nowhere else,
	so the foreman's half of the dispatch was instant and the worker's half was
	whenever they next opened the app — which on a picking crew is at lunch."""

	def setUp(self):
		super().setUp()
		self.transport = Recorder()
		patched = mock.patch.object(push_service, "_apns_transport", self.transport)
		patched.start()
		self.addCleanup(patched.stop)
		frappe.conf.update(apns_conf())
		self.addCleanup(frappe.conf.clear)
		push_service._JWT_CACHE.clear()

	def a_task(self, **overrides):
		payload = {
			"task_name": "Habitability walk — MC-Cabin-01",
			"task_type": "Inspection",
			"evidence_required": {"photos": True, "signature": True, "findings_text": True},
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)["name"]

	def assign(self, task, worker=PICKER, **extra):
		return self.tool_data("assign_farm_task", {"task": task, "assigned_to": worker, **extra})

	def test_dispatching_somebody_rings_their_handset(self):
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		answer = self.assign(self.a_task())

		self.assertEqual(answer["push"]["employees"], 1)
		self.assertEqual(answer["push"]["tokens"], 1)
		self.assertEqual(answer["push"]["sent"], 1)
		self.assertEqual(len(self.transport.calls), 1)
		self.assertTrue(self.transport.calls[0]["url"].endswith("/TOK-BEN"))

	def test_it_rings_the_assignee_and_nobody_else(self):
		"""The foreman has a handset on this farm too. A dispatch is news to one
		person, and a crew that gets buzzed about every job somebody else was
		sent to stops reading any of them."""
		self.register(user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA")
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		self.assign(self.a_task())

		self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in self.transport.calls], ["TOK-BEN"])

	def test_the_notification_carries_the_task_the_handset_should_open(self):
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		task = self.a_task()
		self.assign(task)

		body = self.transport.calls[0]["body"]
		self.assertEqual(body["task"], task)
		self.assertEqual(body["phase"], "assigned")
		self.assertEqual(body["aps"]["category"], push_service.CATEGORY_TASK)
		self.assertIn("Habitability walk", body["aps"]["alert"]["body"])

	def test_the_push_is_collapsed_on_the_task_so_one_job_is_one_notification(self):
		"""A task dispatched, taken off somebody and given back is three pushes
		about one job. Collapsing on the docname makes it one lock-screen row."""
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		task = self.a_task()
		self.assign(task)
		self.assertEqual(self.transport.calls[0]["headers"]["apns-collapse-id"], task)

	def test_taking_work_off_one_worker_tells_the_other_it_was_a_reassignment(self):
		self.register(user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA")
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		task = self.a_task()
		self.assign(task, worker=FOREMAN)
		self.transport.calls.clear()

		self.assign(task, worker=PICKER, reassign=True, reason="Ada is on the packhouse line")

		body = self.transport.calls[0]["body"]
		self.assertTrue(body["reassigned"])
		self.assertEqual(body["aps"]["alert"]["title"], "Task reassigned to you")

	def test_a_dispatch_is_urgent_enough_to_deliver_now(self):
		"""Somebody is being asked to go and do something. Priority 5 would let
		Apple hold it for a moment that conserves battery, which for a job that
		starts in ten minutes is the wrong trade."""
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		self.assign(self.a_task())
		self.assertEqual(self.transport.calls[0]["headers"]["apns-priority"], push_service.PRIORITY_IMMEDIATE)

	def test_a_worker_with_no_handset_is_still_dispatched(self):
		"""THE PROPERTY THAT MATTERS MOST. The assignment is the record; the push
		is a convenience on top of it. `no_tokens` rather than silence, because
		"they never enrolled a phone" is a fixable thing somebody has to see."""
		answer = self.assign(self.a_task())

		self.assertEqual(answer["push"]["reason"], "no_tokens")
		self.assertEqual(answer["push"]["sent"], 0)
		self.assertEqual(answer["assignment"]["assigned_to"], PICKER)
		self.assertEqual(answer["state"], "Claimed")
		self.assertEqual(self.transport.calls, [])

	def test_a_bench_with_no_p8_key_still_dispatches(self):
		frappe.conf.clear()
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		answer = self.assign(self.a_task())

		self.assertEqual(answer["push"]["reason"], "not_configured")
		self.assertEqual(answer["assignment"]["assigned_to"], PICKER)
		self.assertEqual(self.transport.calls, [])

	def test_a_transport_that_fails_completely_never_costs_the_assignment(self):
		self.register(user=BEN, employee=PICKER, device_id="DEV-BEN", token="TOK-BEN")
		with mock.patch.object(
			push_service, "_apns_transport", Recorder(status=503, reason="ServiceUnavailable")
		):
			answer = self.assign(self.a_task())

		self.assertEqual(answer["push"]["failed"], 1)
		self.assertEqual(answer["push"]["sent"], 0)
		self.assertEqual(answer["assignment"]["assigned_to"], PICKER)
		# APPLE BEING UNWELL IS NOT EVIDENCE ABOUT THIS WORKER'S PHONE. A 503 that
		# retired the row would unsubscribe a picker for the season over one bad
		# afternoon at Apple. Only `Unregistered` and `BadDeviceToken` do that.
		self.assertTrue(push_service.active_tokens_for_employees([PICKER]))


# ── 11. the alert reaches the people who can act on it ──────────────────────
class TheAlertReachesTheSupervisors(PushTestCase):
	"""v0.107.0. A compliance alert was raised at two in the morning and sat on a
	calendar until somebody opened it. The people who can raise a task for one
	are exactly `roles.DISPATCH_ROLES`, which is who this tells."""

	def setUp(self):
		super().setUp()
		self.transport = Recorder()
		patched = mock.patch.object(push_service, "_apns_transport", self.transport)
		patched.start()
		self.addCleanup(patched.stop)
		frappe.conf.update(apns_conf())
		self.addCleanup(frappe.conf.clear)
		push_service._JWT_CACHE.clear()

		roles.install_roles()
		user = frappe.get_doc("User", ADA)
		user.append("roles", {"role": "Foreman"})
		user.save()
		frappe.db.set_value("Employee", FOREMAN, "user_id", ADA)
		self.register(user=ADA, employee=FOREMAN, device_id="DEV-ADA", token="TOK-ADA")

	def a_camp(self, unit_name="MC-Cabin-01"):
		if not STORE.rows("Parcel"):
			self.tool_data(
				"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
			)
		return self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": unit_name,
				"unit_type": "Cabin",
				"square_footage": 400,
				"capacity": 4,
				"fsma_worker_facility": True,
			},
		)["name"]

	def a_dead_detector(self, unit_name="MC-Cabin-01"):
		"""A cabin somebody has walked and found a fault in. RAISES A CRITICAL.

		Deliberately not a merely un-inspected cabin: `housing_inspection_overdue`
		is a `Warning` in every branch — an annual walk being due is the calendar
		doing its job — and only an OPEN FINDING is Critical. That distinction is
		exactly the one this release pushes on, so the fixture has to respect it
		rather than assert against whatever severity happened to come out.
		"""
		unit = self.a_camp(unit_name)
		STORE.seed(
			"Housing Inspection",
			[
				{
					"name": f"HI-{unit_name}",
					"unit": unit,
					"company": MAIN,
					"inspection_date": "2026-07-01",
					"workflow_state": "Corrective Action Required",
					"findings": "CO detector dead in the back bedroom.",
				}
			],
		)
		return unit

	def sweep(self, **overrides):
		payload = {"today": "2026-07-24"}
		payload.update(overrides)
		return self.tool_data("refresh_compliance_alerts", payload)

	def bodies(self) -> list:
		return [call["body"] for call in self.transport.calls]

	def test_a_critical_alert_rings_the_foreman_s_phone(self):
		self.a_dead_detector()
		report = self.sweep()

		self.assertTrue(report["created"])
		self.assertTrue(report["pushed"])
		self.assertTrue(self.transport.calls)
		self.assertTrue(all(call["url"].endswith("/TOK-ADA") for call in self.transport.calls))
		self.assertTrue(
			all(body["aps"]["category"] == push_service.CATEGORY_COMPLIANCE for body in self.bodies())
		)

	def test_the_notification_carries_the_alert_the_handset_should_open(self):
		self.a_dead_detector()
		self.sweep()

		body = self.bodies()[0]
		self.assertEqual(body["phase"], "alert")
		self.assertEqual(body["severity"], "Critical")
		self.assertTrue(body["compliance_alert"])
		self.assertTrue(body["aps"]["alert"]["body"])

	def test_the_second_sweep_of_the_night_rings_nobody(self):
		"""RAISING AN ALERT IS THE EVENT; NOTICING IT IS STILL TRUE IS NOT. The
		sweep refreshes every open item every night, and a push per refresh would
		be a notification per alert per night forever — which is a feed every
		supervisor turns off, taking the break horn with it."""
		self.a_dead_detector()
		self.sweep()
		first = len(self.transport.calls)
		self.assertTrue(first)

		again = self.sweep()

		self.assertEqual(again["created"], 0)
		self.assertTrue(again["refreshed"])
		self.assertEqual(again["pushed"], 0)
		self.assertEqual(len(self.transport.calls), first)

	def test_only_critical_severities_ring_a_phone(self):
		"""A certificate expiring in five weeks is exactly what the calendar is
		FOR. Pushing it would train every foreman to swipe these away."""
		self.assertEqual(alerts_base.PUSH_SEVERITIES, frozenset({alerts_base.SEVERITY_CRITICAL}))

		self.a_dead_detector()
		self.sweep()
		severities = {body["severity"] for body in self.bodies()}
		self.assertEqual(severities, {"Critical"})

	def test_a_warning_is_raised_and_not_pushed(self):
		"""The other half of the claim above, driven rather than asserted about a
		constant: a Warning observation writes an alert and rings nothing."""
		doc = frappe.get_doc(
			{
				"doctype": "Compliance Alert",
				"alert_key": "test::warning",
				"alert_type": "test",
				"severity": alerts_base.SEVERITY_WARNING,
				"company": MAIN,
				"alert_message": "A certificate expires in five weeks",
			}
		)
		answer = alerts_base._push_alert(doc, "created")

		self.assertEqual(answer["reason"], "severity_below_threshold")
		self.assertEqual(self.transport.calls, [])

	def test_the_alert_is_raised_even_when_no_supervisor_has_a_handset(self):
		"""THE PROPERTY THAT MATTERS MOST, and the mirror of the dispatch one.
		The calendar is the compliance record; the push is a convenience."""
		push_tools.unregister_push_token({"user": ADA, "device_id": "DEV-ADA", "platform": "ios"})
		self.a_dead_detector()
		report = self.sweep()

		self.assertTrue(report["created"])
		self.assertEqual(report["pushed"], 0)
		self.assertIn("no_tokens", report.get("push_notes") or [])
		self.assertEqual(self.transport.calls, [])

	def test_a_farm_with_no_foreman_at_all_still_keeps_its_calendar(self):
		frappe.db.set_value("Employee", FOREMAN, "user_id", None)
		self.a_dead_detector()
		report = self.sweep()

		self.assertTrue(report["created"])
		self.assertEqual(report["pushed"], 0)
		self.assertIn("no_recipients", report.get("push_notes") or [])

	def test_a_compliance_alert_is_delivered_considerately(self):
		"""The sweep runs while the farm is asleep. Priority 5 lets Apple pick a
		moment; `active` leaves a phone in Do Not Disturb silent until morning."""
		self.a_dead_detector()
		self.sweep()

		headers = self.transport.calls[0]["headers"]
		self.assertEqual(headers["apns-priority"], push_service.PRIORITY_CONSIDERATE)
		self.assertEqual(self.bodies()[0]["aps"]["interruption-level"], push_service.INTERRUPTION_ACTIVE)

	def test_the_first_sweep_on_an_old_farm_does_not_buzz_a_phone_ninety_times(self):
		"""THE CAP, AND WHY IT IS NOT SILENT. An operation installing this app in
		August with four years of camp records has a legitimately Critical finding
		in every cabin, and a foreman whose phone buzzes ninety times at once
		turns the category off — taking the break horn with it. Every alert is
		still RAISED; `push_suppressed` is the backlog, said out loud."""
		wanted = alerts_base.MAX_PUSHES_PER_SWEEP + 3
		for index in range(1, wanted + 1):
			self.a_dead_detector(f"MC-Cabin-{index:02d}")

		report = self.sweep()

		self.assertEqual(report["created"], wanted * 3)  # two Warnings + one Critical each
		self.assertEqual(report["pushed_alerts"], alerts_base.MAX_PUSHES_PER_SWEEP)
		self.assertEqual(report["push_suppressed"], 3)
		self.assertEqual(len(self.transport.calls), alerts_base.MAX_PUSHES_PER_SWEEP)

	def test_a_warning_never_spends_one_of_the_ten(self):
		"""An alert that was never going to ring a phone must not push a Critical
		out of the budget, which is why the cap is checked outside the severity
		gate rather than inside it."""
		for index in range(1, 4):
			self.a_camp(f"MC-Warning-{index:02d}")  # Warnings only
		self.a_dead_detector("MC-Cabin-99")

		report = self.sweep()

		self.assertTrue(report["created"] > 4)
		self.assertEqual(report["pushed_alerts"], 1)
		self.assertEqual(report["push_suppressed"], 0)
		self.assertEqual(len(self.transport.calls), 1)

	def test_a_sweep_survives_a_push_that_explodes(self):
		"""It runs on somebody's scheduler beside their real work. A notification
		that could not be sent must never take the night's alerts down with it."""
		self.a_dead_detector()
		with mock.patch.object(push_service, "supervisor_employees", side_effect=RuntimeError("boom")):
			report = self.sweep()

		self.assertTrue(report["created"])
		self.assertEqual(report["pushed"], 0)


if __name__ == "__main__":
	unittest.main()


class TheMissingKeyIsSaidOnce(PushTestCase):
	"""v0.230.1. umbrel.local logged the no-APNs paragraph ten times in forty
	seconds — once per push. It is a fact about the site, said once a window."""

	tokens = SendingAndFailing.tokens

	def setUp(self):
		super().setUp()
		push_service._LOGGED_AT.clear()

	def pushed_logs(self) -> list:
		return [e for e in STORE.errors if e["title"] == "erpnext_mcp: push"]

	def test_many_pushes_one_row_and_every_report_still_says_why(self):
		tokens = self.tokens()
		for _ in range(5):
			report = push_service.send_push(tokens, {"aps": {}}, transport=Recorder(), conf={})
			self.assertEqual(report["reason"], "not_configured")
		self.assertEqual(len(self.pushed_logs()), 1)
		self.assertIn("once every 6 hours", self.pushed_logs()[0]["message"])

	def test_after_the_window_it_is_said_again(self):
		tokens = self.tokens()
		push_service.send_push(tokens, {"aps": {}}, transport=Recorder(), conf={})
		push_service._LOGGED_AT["not_configured"] -= push_service.NOT_CONFIGURED_LOG_HOURS * 3600 + 1
		push_service.send_push(tokens, {"aps": {}}, transport=Recorder(), conf={})
		self.assertEqual(len(self.pushed_logs()), 2)
