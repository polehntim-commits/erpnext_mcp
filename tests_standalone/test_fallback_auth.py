# SPDX-License-Identifier: MIT
"""The second door — v0.17.2, the Tailscale `Authorization`-stripping hotfix.

WHAT BROKE, AND WHY A TEST COULD NOT HAVE CAUGHT IT. Every one of v0.17.1's
mobile tests passes with `frappe.session.user` set directly, because that is
faithful: Frappe authenticates a request before a whitelisted method runs, so
what reaches the app IS a session. The bug was one layer further out. The
Tailscale `serve`/`funnel` proxy removes the `Authorization` header, so Frappe
authenticated nobody, the session was Guest, and `is_whitelisted` refused the
call before the method — the Desk's `/me` page, HTTP 200, no JSON. Proven three
ways on 2026-08-01, including from a Mac inside the tailnet, which rules out the
public funnel edge and leaves the proxy step.

So these tests do the thing the v0.17.1 suite deliberately did not: they drive
the gates from HEADERS AND A BODY, with the session left at Guest, which is
exactly the shape of the request that was failing.

SIX CLAIMS.

1. **THE THREE DOORS ALL OPEN, AND IN THE RIGHT ORDER.** `TheThreeDoors`.
   `Authorization` where it survives, `X-FarmOps-Token` where it does not,
   `_auth` in the body where nothing custom survives. First one that resolves
   wins, and a good `Authorization` is never overridden by a stale fallback.

2. **THE FALLBACK IS A DOOR, NOT A BYPASS.** `TheGatesStillRun`. A credential
   that gets in this way meets the role gate, the Mobile Access Grant, the
   entity scoping, the kill switch and the rate limit — asserted one at a time,
   against a credential that is otherwise perfectly valid.

3. **A WRONG ANSWER IS GUEST.** `WrongCredentialsAreGuest`. Wrong secret,
   unknown key, disabled account, malformed value: all refused, all with the
   same message, none of them telling the caller which fact was wrong.

4. **GUESSING IS METERED AND WORKING PHONES ARE NOT.** `GuessingIsMetered`. The
   counter is keyed on the presented key and only failures touch it, because
   forty phones arrive from one funnel address and an address-keyed limit would
   make them throttle each other.

5. **THE LOG SAYS WHICH DOOR.** `TheLogSaysWhichDoor`. Every mobile call already
   writes an MCP Action Log row; a fallback call's row carries
   `fallback_auth: header` or `fallback_auth: body` and a primary call's row
   carries no such tag at all.

6. **THE HOOK IS THIS APP'S BUSINESS ONLY.** `TheHookMindsItsOwnBusiness`. It
   runs on every request to the site, so it is asserted to do nothing at all to
   a Desk page, another app's endpoint, or `mcp.handle` itself — and to survive
   a request object that makes no sense.
"""

import json

import frappe

from erpnext_mcp.api import fallback_auth, guard
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, OTHER
from .harness import ROLES, STORE
from .test_api_mobile import OUTSIDER, WORKER, MobileAPITestCase

#: What the phone actually calls. The prefix is what `authenticate` gates on.
MOBILE_PATH = "/api/method/erpnext_mcp.api.mobile.get_current_user_context"


class FallbackTestCase(MobileAPITestCase):
	"""One enrolled worker, and a way to call as them WITHOUT a session."""

	def setUp(self):
		# The base class enrols the worker as its last act, and `enrol` below is
		# what catches the credential on the way past — it is readable exactly
		# once, in `create_mobile_user`'s own result, and this suite needs it.
		super().setUp()
		fallback_auth._FAILURES.clear()
		self.addCleanup(fallback_auth._FAILURES.clear)

	def enrol(self, email=WORKER, name="Ana Ramos", role="Field Worker", entities=None):
		"""Enrol, and KEEP THE PAIR. Returns the credential rather than the record."""
		data = super().enrol(email=email, name=name, role=role, entities=entities)
		credential = {"api_key": data["api_key"], "api_secret": data["api_secret"]}
		if email == WORKER:
			self.credential = credential
		return credential

	# ── the request shapes ──────────────────────────────────────────────────
	def as_guest(self, headers=None, body=None, remote_addr="100.64.0.7", path=MOBILE_PATH):
		"""A request with NO session — what the funnel actually delivers.

		The session is set to Guest AFTER the request is built, because that is
		the state Frappe leaves it in when it never saw an `Authorization`
		header: not "the test forgot", but "the framework authenticated nobody".
		"""
		self.request(body or {}, token=False, headers=headers or {}, remote_addr=remote_addr, path=path)
		frappe.local.session.user = "Guest"
		return frappe.local.request

	def by_header(self, credential=None, value=None, **kwargs):
		credential = credential or self.credential
		token = value if value is not None else f"{credential['api_key']}:{credential['api_secret']}"
		return self.as_guest(headers={fallback_auth.HEADER: token}, **kwargs)

	def by_body(self, credential=None, payload=None, **kwargs):
		credential = credential or self.credential
		auth = payload if payload is not None else dict(credential)
		return self.as_guest(body={fallback_auth.BODY_KEY: auth}, **kwargs)

	def frappe_key(self, email=WORKER):
		"""A genuine Frappe User API key — the credential Frappe's own validator
		authenticates. v0.175.0: a phone holds a DEVICE credential, which that
		validator has never heard of, so the "Frappe already authenticated
		somebody" door needs one of these to be exercised at all."""
		from erpnext_mcp.tools import mobile as mobile_tools

		return mobile_tools._issue_user_token(email)

	def by_authorization(self, credential=None, **kwargs):
		"""The primary path: the header survived, so the harness authenticates it.

		`MCPTestCase.request` reproduces Frappe's own api-key validator, so this
		arrives with a real session and nothing in `fallback_auth` ever runs.
		"""
		credential = credential or self.credential
		header = f"token {credential['api_key']}:{credential['api_secret']}"
		return self.request(
			{}, token=False, headers={"Authorization": header}, remote_addr="100.64.0.7", path=MOBILE_PATH
		)

	def last_summary(self, method="get_current_user_context"):
		rows = self.audit_rows(method)
		self.assertTrue(rows, f"no MCP Action Log row for mobile:{method}")
		return str(rows[-1]["result_summary"])


# ── 1. the three doors ──────────────────────────────────────────────────────
class TheThreeDoors(FallbackTestCase):
	def test_authorization_still_works_when_it_survives_the_proxy(self):
		"""Frappe's own door, unchanged — for a credential Frappe issued."""
		self.by_authorization(credential=self.frappe_key())
		self.assertEqual(frappe.session.user, WORKER)
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		self.assertEqual(fallback_auth.source(), "")

	def test_a_device_pair_is_not_a_frappe_api_key(self):
		"""v0.175.0. A phone's pair in `Authorization` alone names nobody.

		Frappe's validator looks the key up on User, where a device key never is
		— on a bench it answers 401 before any hook runs, and here the double
		leaves the request as Guest. The phone is served by X-FarmOps-Token (and
		by the /farmops transport, which reads both headers itself), which is
		what it has sent on every call since v0.17.2.
		"""
		self.by_authorization()
		self.assertIn(frappe.session.user, ("", "Guest"))
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()

	def test_the_farmops_header_works_when_authorization_is_absent(self):
		"""THE BUG. Guest in, worker out, on the header the proxy leaves alone."""
		self.by_header()
		self.assertEqual(frappe.session.user, "Guest")
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		self.assertEqual(fallback_auth.source(), fallback_auth.SOURCE_HEADER)

	def test_the_auth_body_works_when_both_headers_are_absent(self):
		"""A layer that eats one custom header may eat another. A body is a body."""
		self.by_body()
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		self.assertEqual(fallback_auth.source(), fallback_auth.SOURCE_BODY)

	def test_the_auth_body_is_read_from_form_dict_when_frappe_already_parsed_it(self):
		"""On a real site the JSON body IS `form_dict` by the time this runs."""
		self.as_guest()
		frappe.local.form_dict = {"cmd": "erpnext_mcp.api.mobile.get_current_user_context"}
		frappe.local.form_dict[fallback_auth.BODY_KEY] = dict(self.credential)
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		self.assertEqual(fallback_auth.source(), fallback_auth.SOURCE_BODY)

	def test_a_form_encoded_client_may_send_auth_as_a_json_string(self):
		self.as_guest()
		frappe.local.form_dict = {fallback_auth.BODY_KEY: json.dumps(dict(self.credential))}
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)

	def test_the_body_may_carry_the_pair_as_one_token_string(self):
		pair = f"{self.credential['api_key']}:{self.credential['api_secret']}"
		self.by_body(payload={"token": pair})
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)

	def test_the_word_token_is_tolerated_in_front_of_the_header_value(self):
		"""The likeliest mistake in a two-line client change is copying the word."""
		pair = f"token {self.credential['api_key']}:{self.credential['api_secret']}"
		self.by_header(value=pair)
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)

	def test_a_session_frappe_established_is_never_overridden_by_a_fallback(self):
		"""Priority order: (a) Frappe's own auth beats (b) header beats (c) body."""
		other = self.enrol(email=OUTSIDER, name="Ben Ortiz", role="Foreman", entities=[OTHER])
		self.by_authorization(credential=self.frappe_key())
		frappe.local.request.headers[fallback_auth.HEADER] = f"{other['api_key']}:{other['api_secret']}"
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		self.assertEqual(fallback_auth.source(), "")

	def test_the_header_beats_the_body_when_both_are_present(self):
		other = self.enrol(email=OUTSIDER, name="Ben Ortiz", role="Foreman", entities=[OTHER])
		self.as_guest(
			headers={fallback_auth.HEADER: f"{self.credential['api_key']}:{self.credential['api_secret']}"},
			body={fallback_auth.BODY_KEY: dict(other)},
		)
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		self.assertEqual(fallback_auth.source(), fallback_auth.SOURCE_HEADER)

	def test_every_mobile_method_takes_the_fallback_and_not_just_the_first_one(self):
		"""The resolution is in the decorator, so it is on all eleven or on none."""
		self.by_header()
		self.assertEqual(mobile_api.list_my_tasks()["count"], 0)
		self.by_header()
		self.assertEqual(mobile_api.list_available_tasks()["count"], 0)
		self.by_header()
		self.assertEqual(mobile_api.list_compliance_alerts()["count"], 0)

	def test_the_arguments_survive_becoming_somebody(self):
		"""`frappe.set_user` CLEARS form_dict, and form_dict is the arguments.

		At the moment an auth hook runs, nothing has bound the request's
		arguments yet — `form_dict` is the only copy. This is the assertion that
		`_establish` puts it back, and its absence is every mobile call arriving
		with no task, no company and no evidence.
		"""
		self.as_guest()
		frappe.local.form_dict = {
			"cmd": "erpnext_mcp.api.mobile.list_my_tasks",
			"company": MAIN,
			fallback_auth.BODY_KEY: dict(self.credential),
		}
		fallback_auth.authenticate()
		self.assertEqual(frappe.session.user, WORKER)
		self.assertEqual(frappe.local.form_dict.get("company"), MAIN)


# ── 2. the fallback is a door, not a bypass ─────────────────────────────────
class TheGatesStillRun(FallbackTestCase):
	REFUSAL = "enrolled Farm Ops credential"

	def test_a_valid_credential_with_no_mobile_access_grant_is_refused(self):
		"""The grant is what makes 'enrolled' a fact. A key alone is not one."""
		STORE.rows("Mobile Access Grant")[0]["state"] = "Revoked"
		self.by_header()
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.get_current_user_context()
		self.assertIn(self.REFUSAL, str(caught.exception))

	def test_a_valid_credential_with_no_farm_ops_role_is_refused(self):
		ROLES[WORKER] = ["Advisor"]
		self.by_header()
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.get_current_user_context()
		self.assertIn(self.REFUSAL, str(caught.exception))

	def test_the_body_door_is_gated_exactly_as_the_header_door_is(self):
		ROLES[WORKER] = ["Advisor"]
		self.by_body()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()

	def test_entity_scoping_still_applies_to_a_fallback_caller(self):
		self.by_header()
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_my_tasks(company=OTHER)
		self.assertIn("not one of this account's entities", str(caught.exception))

	def test_the_kill_switch_still_stops_a_fallback_caller(self):
		self.configure(farm_ops_mobile_enabled=0)
		self.by_header()
		with self.assertRaises(guard.MobileDisabled):
			mobile_api.get_current_user_context()

	def test_the_rate_limit_still_meters_a_fallback_caller(self):
		for _ in range(guard.READ_LIMIT):
			self.by_header()
			mobile_api.get_current_user_context()
		self.by_header()
		with self.assertRaises(guard.RateLimited):
			mobile_api.get_current_user_context()

	def test_the_rate_limit_keys_on_the_worker_and_not_on_the_funnel_address(self):
		"""FORTY PHONES ARRIVE FROM ONE ADDRESS. This is why resolution comes first.

		A limit evaluated before the fallback resolved would key every one of
		them on `ip:<the funnel>` — one bucket for the whole farm, and the
		sixty-first read of the minute would throttle a worker who had made one
		call all day.
		"""
		other = self.enrol(email=OUTSIDER, name="Ben Ortiz", role="Foreman", entities=[OTHER])
		for _ in range(guard.READ_LIMIT):
			self.by_header(remote_addr="100.64.0.1")
			mobile_api.get_current_user_context()
		self.by_header(credential=other, remote_addr="100.64.0.1")
		self.assertEqual(mobile_api.get_current_user_context()["user"], OUTSIDER)

	def test_an_admin_holding_every_role_still_cannot_get_in_through_the_new_door(self):
		"""v0.17.1's headline check, re-run against v0.17.2's entry point."""
		ROLES["Administrator"] = ["System Manager", "Farm Manager", "Field Worker"]
		STORE.seed("User", [{"name": "Administrator", "enabled": 1, "api_key": "adminkey"}])
		STORE.passwords[("User", "Administrator", "api_secret")] = "adminsecret"
		self.by_header(credential={"api_key": "adminkey", "api_secret": "adminsecret"})
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()

	def test_no_secret_reaches_the_phone_on_the_fallback_path_either(self):
		self.by_header()
		text = json.dumps(mobile_api.get_current_user_context(), default=str)
		self.assertNotIn(self.credential["api_secret"], text)
		self.assertNotIn(self.credential["api_key"], text)

	def test_the_auth_envelope_never_becomes_an_argument_or_an_audit_row(self):
		"""`_auth` is envelope. It must not bind, and it must not be logged."""
		self.by_body()
		mobile_api.get_current_user_context(**{fallback_auth.BODY_KEY: dict(self.credential)})
		row = self.audit_rows("get_current_user_context")[-1]
		self.assertNotIn(self.credential["api_secret"], str(row["arguments_json"]))
		self.assertNotIn(fallback_auth.BODY_KEY, str(row["arguments_json"]))


# ── 3. a wrong answer is Guest ──────────────────────────────────────────────
class WrongCredentialsAreGuest(FallbackTestCase):
	REFUSAL = "enrolled Farm Ops credential"

	def _refused(self):
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.get_current_user_context()
		self.assertIn(self.REFUSAL, str(caught.exception))
		self.assertEqual(fallback_auth.source(), "")

	def test_a_wrong_secret_is_refused(self):
		self.by_header(credential={"api_key": self.credential["api_key"], "api_secret": "not-it"})
		self._refused()

	def test_an_unknown_key_is_refused(self):
		self.by_header(credential={"api_key": "nobody", "api_secret": "not-it"})
		self._refused()

	def test_a_revoked_credential_stops_working_on_the_new_door_too(self):
		"""Revocation clears both halves — see `_clear_token`. It must close this."""
		self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "phone lost"})
		self.by_header()
		self._refused()

	def test_a_disabled_account_is_refused_even_with_the_right_secret(self):
		frappe.db.set_value("User", WORKER, "enabled", 0)
		self.by_header()
		self._refused()

	def test_a_malformed_header_is_refused_and_does_not_raise(self):
		for value in ("", "   ", "no-colon-here", ":", "key:", ":secret", "token "):
			with self.subTest(value=value):
				self.by_header(value=value)
				self._refused()

	def test_a_body_that_is_not_an_object_is_ignored(self):
		for payload in ("nonsense", [1, 2], {"api_key": "x"}, {}):
			with self.subTest(payload=payload):
				self.by_body(payload=payload)
				self._refused()

	def test_a_body_too_large_to_be_an_envelope_is_not_parsed(self):
		"""The one endpoint carrying megabytes sends 512 KB slices; this is the ceiling."""
		self.as_guest(body={fallback_auth.BODY_KEY: dict(self.credential), "pad": "x" * 3_000_000})
		self._refused()


# ── 4. guessing is metered, working phones are not ──────────────────────────
class GuessingIsMetered(FallbackTestCase):
	def test_a_guesser_working_one_key_is_stopped_inside_a_minute(self):
		wrong = {"api_key": self.credential["api_key"], "api_secret": "not-it"}
		for _ in range(fallback_auth.FAILURE_LIMIT):
			self.by_header(credential=wrong)
			with self.assertRaises(frappe.PermissionError):
				mobile_api.get_current_user_context()
		# The right answer for that key is now declined too, for the rest of the
		# window. That is the lockout doing its job, and it is bounded to sixty
		# seconds and to the one key somebody is hammering.
		self.by_header()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()

	def test_one_hammered_key_does_not_lock_out_anybody_else(self):
		"""THE REASON THE COUNTER IS NOT KEYED ON THE CALLER'S ADDRESS.

		Every phone on the farm arrives from the funnel's address. A counter
		keyed there would let one phone with a stale credential take the other
		thirty-nine off the air.
		"""
		other = self.enrol(email=OUTSIDER, name="Ben Ortiz", role="Foreman", entities=[OTHER])
		wrong = {"api_key": self.credential["api_key"], "api_secret": "not-it"}
		for _ in range(fallback_auth.FAILURE_LIMIT * 2):
			self.by_header(credential=wrong, remote_addr="100.64.0.1")
			with self.assertRaises(frappe.PermissionError):
				mobile_api.get_current_user_context()
		self.by_header(credential=other, remote_addr="100.64.0.1")
		self.assertEqual(mobile_api.get_current_user_context()["user"], OUTSIDER)

	def test_a_working_phone_never_touches_the_counter(self):
		for _ in range(20):
			self.by_header()
			mobile_api.get_current_user_context()
		self.assertEqual(fallback_auth._FAILURES, {})

	def test_the_counter_holds_no_credential_in_plain_text(self):
		self.by_header(credential={"api_key": self.credential["api_key"], "api_secret": "not-it"})
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()
		self.assertTrue(fallback_auth._FAILURES)
		for slot in fallback_auth._FAILURES:
			self.assertNotIn(self.credential["api_key"], slot)


# ── 5. the log says which door ──────────────────────────────────────────────
class TheLogSaysWhichDoor(FallbackTestCase):
	def test_a_header_fallback_is_tagged(self):
		self.by_header()
		mobile_api.get_current_user_context()
		self.assertIn("fallback_auth: header", self.last_summary())

	def test_a_body_fallback_is_tagged(self):
		self.by_body()
		mobile_api.get_current_user_context()
		self.assertIn("fallback_auth: body", self.last_summary())

	def test_the_primary_path_is_tagged_with_nothing_at_all(self):
		"""An untagged row IS the signal: that request's header survived."""
		self.by_authorization(credential=self.frappe_key())
		mobile_api.get_current_user_context()
		self.assertNotIn("fallback_auth", self.last_summary())

	def test_a_refusal_on_the_fallback_path_is_tagged_too(self):
		"""The rows worth reading after the fact are the refused ones."""
		ROLES[WORKER] = ["Advisor"]
		self.by_header()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()
		self.assertIn("fallback_auth: header", self.last_summary())

	def test_a_refusal_that_never_resolved_anybody_carries_no_tag(self):
		self.by_header(credential={"api_key": "nobody", "api_secret": "not-it"})
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()
		self.assertNotIn("fallback_auth", self.last_summary())

	def test_the_row_records_the_worker_and_not_guest(self):
		self.by_header()
		mobile_api.get_current_user_context()
		self.assertIn(WORKER, self.last_summary())


# ── 6. the hook minds its own business ──────────────────────────────────────
class TheHookMindsItsOwnBusiness(FallbackTestCase):
	def _ignored(self, **kwargs):
		self.by_header(**kwargs)
		self.assertEqual(fallback_auth.authenticate(), "")
		self.assertEqual(frappe.session.user, "Guest")

	def test_a_desk_page_load_is_not_this_hooks_business(self):
		self._ignored(path="/app/farm-task/FT-0001")

	def test_another_apps_endpoint_is_not_this_hooks_business(self):
		self._ignored(path="/api/method/frappe.client.get_list")

	def test_the_mcp_endpoint_is_not_this_hooks_business(self):
		"""`mcp.handle` has its own three gates and does not want a second auth."""
		self._ignored(path="/api/method/erpnext_mcp.mcp.handle")

	def test_a_mobile_path_is_this_hooks_business(self):
		self.by_header()
		self.assertEqual(fallback_auth.authenticate(), fallback_auth.SOURCE_HEADER)
		self.assertEqual(frappe.session.user, WORKER)

	def test_the_files_endpoints_are_this_hooks_business_too(self):
		self.by_header(path="/api/method/erpnext_mcp.api.files.stage_file_chunk")
		self.assertEqual(fallback_auth.authenticate(), fallback_auth.SOURCE_HEADER)

	def test_a_cmd_carrying_the_dotted_path_is_recognised(self):
		"""`/api/method/<path>` and a POST to `/` with `cmd=<path>` are one call."""
		self.by_header(path="/")
		frappe.local.form_dict = {"cmd": "erpnext_mcp.api.mobile.list_my_tasks"}
		self.assertEqual(fallback_auth.authenticate(), fallback_auth.SOURCE_HEADER)

	def test_the_hook_never_raises_whatever_the_request_is(self):
		"""It runs before the handler on EVERY request. An exception here is the site."""
		frappe.local.request = None
		frappe.local.form_dict = None
		self.assertEqual(fallback_auth.authenticate(), "")
		frappe.local.request = object()
		self.assertEqual(fallback_auth.authenticate(), "")

	def test_the_enrolment_tools_print_the_second_header(self):
		"""An operator wiring a client up reads THIS, not the release notes."""
		data = self.tool_data(
			"create_mobile_user",
			{
				"email": "cara@example.test",
				"full_name": "Cara Diaz",
				"role": "Field Worker",
				"entity_access": [MAIN],
			},
		)
		self.assertEqual(
			data["farmops_auth_header"],
			f"X-FarmOps-Token: {data['api_key']}:{data['api_secret']}",
		)

	def test_the_second_header_is_stripped_out_of_a_mobile_response_like_the_first(self):
		"""It carries a live credential, so it must trip the same strip."""
		cleaned = guard.strip_secrets({"farmops_auth_header": "X-FarmOps-Token: a:b", "keep": 1})
		self.assertEqual(cleaned, {"keep": 1})

	def test_the_hook_is_declared_in_the_app_manifest(self):
		"""A resolver nothing calls is a resolver that does not run. See hooks.py."""
		from erpnext_mcp import hooks

		self.assertEqual(hooks.auth_hooks, ["erpnext_mcp.api.fallback_auth.authenticate"])
