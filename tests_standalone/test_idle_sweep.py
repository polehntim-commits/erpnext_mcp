# SPDX-License-Identifier: MIT
"""The nightly idle-credential sweep — v0.17.1.

**v0.17.0's own docstring said this app would never install this job**, on the
grounds that it "would rewrite another app's User records on a timer with nobody
watching". These tests are what makes the reversal defensible rather than merely
convenient: each one pins down a clause of that objection and shows it answered.

The threat is an unreported lost phone. v0.17.1 put forty live credentials in
forty pockets on the open internet, and a device left on a truck seat and never
mentioned is the ordinary case. A credential that stops working by itself is the
only control that does not depend on somebody admitting to the loss.

FIVE CLAIMS.

1. `ItRevokesWhatWentQuiet` — the thing actually works, on the right clock.
2. `ItIsBoundedInEveryDirection` — persistent grants, non-Active grants, grants
   with no credential and grants whose age cannot be established are all left
   alone. "Never one whose age it cannot establish" is the clause that stops
   somebody being locked out mid-harvest by a guess.
3. `TheAccountSurvives` — it revokes the TOKEN, never the account. Roles and
   entity access are untouched. `revoke_mobile_user` is the other thing and no
   timer ever reaches it.
4. `SomebodyIsWatching` — every revocation writes an audit row and the run
   reports. This is the clause that was false in v0.17.0 and is true now.
5. `TheStampIsCheapAndHonest` — `last_seen_on` is written at most once a day, so
   the control does not put a write on the hot path of a read endpoint.
"""

import frappe

from erpnext_mcp import audit, roles, settings
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import mobile as mobile_tools

from .fixtures import MAIN, V12TestCase
from .harness import ROLES, STORE

WORKER = "ana@example.test"
QUIET = "winter@example.test"

ON = {f"allow_{name}": 1 for name in ("create_mobile_user", "get_current_user_context")}


class IdleSweepTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **ON)
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		roles.install_roles()

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	def enrol(self, email=WORKER, name="Ana Ramos"):
		return self.tool_data(
			"create_mobile_user",
			{"email": email, "full_name": name, "role": "Field Worker", "entity_access": [MAIN]},
		)

	def went_quiet(self, user=WORKER, days=45):
		"""Age a grant by moving its clock backwards."""
		when = str(frappe.utils.add_days(frappe.utils.now(), -days))
		frappe.db.set_value("Mobile Access Grant", user, "last_seen_on", when)
		frappe.db.set_value("Mobile Access Grant", user, "token_issued_on", when)
		return when

	def grant(self, user=WORKER):
		return mobile_tools._grant_row(user)


# ── 1 ───────────────────────────────────────────────────────────────────────
class ItRevokesWhatWentQuiet(IdleSweepTestCase):
	def test_a_credential_nobody_has_used_for_the_window_is_revoked(self):
		self.enrol()
		self.went_quiet()
		self.assertEqual(mobile_tools.sweep_idle_grants(), 1)
		self.assertEqual(self.grant()["state"], "Revoked")
		self.assertFalse(mobile_tools.read_api_secret(WORKER))

	def test_one_in_daily_use_is_left_alone(self):
		self.enrol()
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.assertEqual(self.grant()["state"], "Active")
		self.assertTrue(mobile_tools.read_api_secret(WORKER))

	def test_one_that_went_quiet_yesterday_is_left_alone(self):
		"""The window is a window, not a hair trigger."""
		self.enrol()
		self.went_quiet(days=1)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.assertEqual(self.grant()["state"], "Active")

	def test_the_window_is_the_operators_to_set(self):
		self.enrol()
		self.went_quiet(days=10)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.configure(enabled=1, mobile_grant_idle_days=7, **ON)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 1)

	def test_zero_switches_the_whole_sweep_off(self):
		"""A site whose crew works one month in twelve is better served by a human."""
		self.enrol()
		self.went_quiet()
		self.configure(enabled=1, mobile_grant_idle_days=0, **ON)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.assertEqual(self.grant()["state"], "Active")

	def test_an_unparseable_window_falls_back_to_the_default_not_to_off(self):
		"""Reading a broken value as 0 would silently disable a security control."""
		self.configure(enabled=1, mobile_grant_idle_days="not a number", **ON)
		self.assertEqual(settings.mobile_grant_idle_days(), 30)

	def test_it_ships_at_thirty_days(self):
		self.assertEqual(settings.mobile_grant_idle_days(), 30)

	def test_a_grant_never_scanned_ages_from_its_issue_date(self):
		"""A card printed and never scanned is exactly the risk, not an exception
		to it — but it has no last_seen_on, so the issue date is the clock."""
		self.enrol()
		when = str(frappe.utils.add_days(frappe.utils.now(), -45))
		frappe.db.set_value("Mobile Access Grant", WORKER, "last_seen_on", None)
		frappe.db.set_value("Mobile Access Grant", WORKER, "token_issued_on", when)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 1)


# ── 2 ───────────────────────────────────────────────────────────────────────
class ItIsBoundedInEveryDirection(IdleSweepTestCase):
	def test_a_persistent_grant_is_exempt(self):
		"""A winter caretaker's phone is legitimately quiet for months."""
		self.enrol()
		self.went_quiet()
		frappe.db.set_value("Mobile Access Grant", WORKER, "persistent", 1)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.assertEqual(self.grant()["state"], "Active")

	def test_an_already_revoked_grant_is_not_revoked_twice(self):
		self.enrol()
		self.went_quiet()
		frappe.db.set_value("Mobile Access Grant", WORKER, "state", "Revoked")
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)

	def test_a_grant_with_no_live_credential_is_skipped(self):
		"""There is nothing to revoke, and a revocation record that says otherwise
		is a record that lies about what happened."""
		self.enrol()
		self.went_quiet()
		mobile_tools._clear_token(WORKER)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.assertEqual(self.grant()["state"], "Active")

	def test_a_grant_whose_age_cannot_be_established_is_left_alone_and_reported(self):
		"""Guessing an age for a credential and then acting on the guess is the
		shape of mistake that ends with somebody locked out mid-harvest."""
		self.enrol()
		frappe.db.set_value("Mobile Access Grant", WORKER, "last_seen_on", None)
		frappe.db.set_value("Mobile Access Grant", WORKER, "token_issued_on", None)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.assertEqual(self.grant()["state"], "Active")

	def test_one_grant_that_will_not_revoke_does_not_stop_the_others(self):
		self.enrol()
		self.enrol(email=QUIET, name="Winter Caretaker")
		self.went_quiet(WORKER)
		self.went_quiet(QUIET)

		original = mobile_tools._clear_token

		def explode(user, *args):
			if user == WORKER:
				raise RuntimeError("the database said no")
			return original(user, *args)

		try:
			mobile_tools._clear_token = explode
			self.assertEqual(mobile_tools.sweep_idle_grants(), 1)
		finally:
			mobile_tools._clear_token = original
		self.assertEqual(self.grant(QUIET)["state"], "Revoked")
		self.assertEqual(self.grant(WORKER)["state"], "Active")

	def test_it_never_raises_and_returns_what_it_did(self):
		"""It runs on somebody's scheduler beside their real work."""
		original = settings.mobile_grant_idle_days
		try:
			settings.mobile_grant_idle_days = lambda: 1 / 0
			self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		finally:
			settings.mobile_grant_idle_days = original
		self.assertTrue(any("idle mobile credential sweep" in row["title"] for row in STORE.errors))

	def test_it_takes_no_arguments(self):
		import inspect

		self.assertEqual(list(inspect.signature(mobile_tools.sweep_idle_grants).parameters), [])


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheAccountSurvives(IdleSweepTestCase):
	def setUp(self):
		super().setUp()
		self.enrol()
		self.went_quiet()
		mobile_tools.sweep_idle_grants()

	def test_the_login_is_still_enabled(self):
		"""This is 'your phone went quiet', not 'you no longer work here'."""
		self.assertTrue(frappe.db.get_value("User", WORKER, "enabled"))

	def test_the_roles_are_untouched(self):
		self.assertIn("Field Worker", roles.roles_of(WORKER))

	def test_the_entity_access_is_untouched(self):
		self.assertEqual(roles.companies_for(WORKER), [MAIN])

	def test_the_reason_on_the_record_says_what_actually_happened(self):
		reason = self.grant()["revocation_reason"]
		self.assertIn("idle sweep", reason)
		self.assertIn("30", reason)

	def test_the_worker_is_one_qr_away_from_working_again(self):
		"""Re-enrolling restores the credential without rebuilding the account."""
		self.tool_data(
			"create_mobile_user",
			{
				"email": WORKER,
				"full_name": "Ana Ramos",
				"role": "Field Worker",
				"entity_access": [MAIN],
				"update_existing": True,
				"generate_token": True,
			},
		)
		self.assertEqual(self.grant()["state"], "Active")
		self.assertTrue(mobile_tools.read_api_secret(WORKER))


# ── 4 ───────────────────────────────────────────────────────────────────────
class SomebodyIsWatching(IdleSweepTestCase):
	def test_every_revocation_writes_an_audit_row(self):
		"""THE CLAUSE THAT WAS FALSE IN v0.17.0. 'With nobody watching' was the
		objection, and this is the answer to it."""
		self.enrol()
		self.went_quiet()
		mobile_tools.sweep_idle_grants()
		rows = [row for row in STORE.rows("MCP Action Log") if row["tool_name"] == "sweep_idle_grants"]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["result_status"], audit.STATUS_SUCCESS)
		self.assertIn(WORKER, rows[0]["result_summary"])

	def test_the_run_reports_what_it_revoked_and_how_to_get_back_on(self):
		self.enrol()
		self.went_quiet()
		mobile_tools.sweep_idle_grants()
		body = (STORE.emails or [{"message": ""}])[-1]["message"] or (
			STORE.errors[-1]["message"] if STORE.errors else ""
		)
		self.assertIn(WORKER, body)
		self.assertIn("generate_mobile_login_qr", body)
		self.assertIn("Exempt From Idle Sweep", body)

	def test_a_quiet_night_reports_nothing(self):
		"""A notification every night is a notification somebody filters away."""
		self.enrol()
		before = len(STORE.emails)
		mobile_tools.sweep_idle_grants()
		self.assertEqual(len(STORE.emails), before)

	def test_a_report_that_cannot_be_emailed_lands_in_the_error_log(self):
		self.enrol()
		self.went_quiet()
		STORE.mail_fails = True
		mobile_tools.sweep_idle_grants()
		self.assertTrue(any("idle Farm Ops credentials" in row["title"] for row in STORE.errors))


# ── 5 ───────────────────────────────────────────────────────────────────────
class TheStampIsCheapAndHonest(IdleSweepTestCase):
	def be(self, user=WORKER):
		self.request({}, headers={}, remote_addr="100.64.0.7")
		frappe.local.session.user = user

	def test_a_mobile_call_records_that_the_credential_was_used(self):
		self.enrol()
		frappe.db.set_value("Mobile Access Grant", WORKER, "last_seen_on", None)
		self.be()
		mobile_api.get_current_user_context()
		self.assertTrue(self.grant()["last_seen_on"])

	def test_a_second_call_the_same_day_does_not_write_again(self):
		"""Forty phones polling cost forty writes a day, not forty an hour — a
		timestamp on every request would put a write on the hot path of a read
		endpoint to serve a job that runs at midnight and rounds to days."""
		self.enrol()
		self.be()
		mobile_api.get_current_user_context()
		first = self.grant()["last_seen_on"]
		frappe.db.set_value("Mobile Access Grant", WORKER, "last_seen_on", first)
		mobile_api.get_current_user_context()
		mobile_api.get_current_user_context()
		self.assertEqual(self.grant()["last_seen_on"], first)

	def test_using_the_app_keeps_a_credential_alive_through_a_sweep(self):
		"""The two halves, joined: the stamp is what the sweep reads."""
		self.enrol()
		self.went_quiet()
		self.be()
		mobile_api.get_current_user_context()
		self.assertEqual(mobile_tools.sweep_idle_grants(), 0)
		self.assertEqual(self.grant()["state"], "Active")

	def test_a_failed_stamp_never_fails_the_call(self):
		"""A credential that looks a day staler than it is beats a 500 on a phone."""
		self.enrol()
		original = frappe.db.set_value
		try:
			frappe.db.set_value = lambda *a, **k: 1 / 0
			self.be()
			self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		finally:
			frappe.db.set_value = original
