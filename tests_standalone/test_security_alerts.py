# SPDX-License-Identifier: MIT
"""Security Alert Recipients (v0.216.1).

The v0.216.0 sign-in alert read the drift report's field: it emailed nobody
when that was empty and sent a comma-separated list as one address. It now has
its own field, with the drift report's fallback, and the drift report is
untouched.
"""


from erpnext_mcp import drift, security_alerts
from erpnext_mcp.farmops_api import app as sidecar_app

from .harness import STORE, set_roles
from .test_farmops_api import FarmOpsAPITestCase

MANAGER = "boss@example.test"
OTHER = "ops@example.test"
CONTEXT = "/farmops/api/mobile/get_current_user_context"


class RecipientsTestCase(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		STORE.seed(
			"User",
			[
				{"name": MANAGER, "enabled": 1, "full_name": "Boss", "roles": [{"role": "System Manager"}]},
				{"name": OTHER, "enabled": 0, "full_name": "Gone", "roles": [{"role": "System Manager"}]},
			],
		)
		set_roles(MANAGER, ["System Manager"])
		sidecar_app._ALERTED.clear()
		self.addCleanup(sidecar_app._ALERTED.clear)


class WhoHears(RecipientsTestCase):
	def test_empty_means_the_enabled_system_managers(self):
		self.configure(enabled=1, security_alert_email="")
		who = security_alerts.recipients()
		self.assertIn(MANAGER, who)
		self.assertNotIn(OTHER, who)
		self.assertNotIn("Administrator", who)

	def test_one_address(self):
		self.configure(enabled=1, security_alert_email="tim@example.test")
		self.assertEqual(security_alerts.recipients(), ["tim@example.test"])

	def test_commas_and_semicolons_are_split_and_deduplicated(self):
		self.configure(
			enabled=1, security_alert_email=" tim@example.test; it@example.test ,tim@example.test,, "
		)
		self.assertEqual(security_alerts.recipients(), ["tim@example.test", "it@example.test"])

	def test_the_drift_field_is_not_read(self):
		self.configure(enabled=1, drift_report_email="books@example.test", security_alert_email="")
		self.assertNotIn("books@example.test", security_alerts.recipients())

	def test_the_drift_report_is_unchanged(self):
		self.configure(
			enabled=1, drift_report_email="books@example.test", security_alert_email="tim@example.test"
		)
		self.assertEqual(drift.recipients(), ["books@example.test"])

	def test_no_mail_account_lands_in_the_error_log_and_does_not_raise(self):
		self.configure(enabled=1, security_alert_email="tim@example.test")
		STORE.mail_fails = True
		self.assertEqual(security_alerts.send("subject", "body"), [])
		self.assertTrue(any(row["title"] == "subject" for row in STORE.errors))


class TheSignInAlertUsesIt(RecipientsTestCase):
	def fail_ten_times(self):
		for _ in range(sidecar_app.AUTH_FAILURE_ALERT):
			self.post(CONTEXT, credential=False)

	def test_the_configured_list_gets_one_email_with_every_address(self):
		self.configure(enabled=1, security_alert_email="tim@example.test; it@example.test")
		before = len(STORE.emails)
		self.fail_ten_times()
		sent = STORE.emails[before:]
		self.assertEqual(len(sent), 1)
		self.assertEqual(list(sent[0]["recipients"]), ["tim@example.test", "it@example.test"])
		self.assertIn("failed Farm Ops sign-ins", sent[0]["message"])

	def test_empty_reaches_the_system_managers(self):
		self.configure(enabled=1, security_alert_email="", drift_report_email="")
		before = len(STORE.emails)
		self.fail_ten_times()
		sent = STORE.emails[before:]
		self.assertEqual(len(sent), 1)
		self.assertIn(MANAGER, sent[0]["recipients"])

	def test_the_drift_address_is_no_longer_emailed(self):
		self.configure(
			enabled=1, security_alert_email="tim@example.test", drift_report_email="books@example.test"
		)
		before = len(STORE.emails)
		self.fail_ten_times()
		self.assertNotIn("books@example.test", str(STORE.emails[before:]))

	def test_still_once_an_hour_per_address(self):
		self.configure(enabled=1, security_alert_email="tim@example.test")
		before = len(STORE.emails)
		for _ in range(3):
			self.fail_ten_times()
		self.assertEqual(len(STORE.emails[before:]), 1)
