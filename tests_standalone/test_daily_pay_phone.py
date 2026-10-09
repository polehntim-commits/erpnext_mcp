# SPDX-License-Identifier: MIT
"""Daily pay on the phone. v0.290.0 (docs/design/daily_pay.md §8).

The worker reads the terms (English or Spanish, tú), signs to start or stop, and it takes effect at the next
workweek; a Signing Evidence row says who signed what. A supervisor sees today's pay — who is due, who waits and why,
the total — and starts the run; submitting it and the bank file stay in the Desk.
"""

import datetime

import frappe

from erpnext_mcp import daily_pay, flags
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase

BOSS = "boss@example.test"


class PhoneCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		daily_pay.install_fields()
		flags.upsert(daily_pay.FLAG, "Flag", True, company=MAIN, description="test", owner_area="payroll", active=True)
		frappe.db.set_value("Employee", WORKER_EMPLOYEE, "employment_type", "Seasonal")
		self.enrol(email=BOSS, name="Rosa Boss", role="Farm Manager")
		set_roles(BOSS, ["Farm Manager"])
		STORE.commit()

	def ask(self, start):
		self.be()
		answer = mobile_api.request_daily_pay(start=start)
		STORE.commit()
		return answer


class TheWorker(PhoneCase):
	def test_reads_the_terms_in_their_language(self):
		self.be()
		mine = mobile_api.get_my_daily_pay(lang="es")
		self.assertTrue(mine["available"])
		self.assertFalse(mine["on"])
		self.assertTrue(any("Se te paga" in line for line in mine["terms"]))
		self.assertEqual(datetime.date.fromisoformat(mine["next_change_on"]).weekday(), 6, "a Sunday")

	def test_signs_to_start_at_the_next_workweek(self):
		answer = self.ask("1")
		boundary = daily_pay.next_boundary()
		self.assertEqual(answer["effective"], str(boundary))
		row = frappe.db.get_value("Employee", WORKER_EMPLOYEE, ["daily_pay", "daily_pay_since", "daily_pay_until"],
		                          as_dict=True)
		self.assertEqual((int(row["daily_pay"]), str(row["daily_pay_since"])[:10], row["daily_pay_until"]),
		                 (1, str(boundary), None))
		self.assertTrue(answer["signing_evidence"])
		self.assertEqual(frappe.db.get_value("Signing Evidence", answer["signing_evidence"], "signer_user"), WORKER)
		with self.assertRaisesRegex(frappe.ValidationError, "already on daily pay"):
			self.ask("1")

	def test_signs_to_stop_after_this_week(self):
		self.ask("1")
		answer = self.ask("0")
		self.assertEqual(str(frappe.db.get_value("Employee", WORKER_EMPLOYEE, "daily_pay_until"))[:10],
		                 str(daily_pay.next_boundary() - datetime.timedelta(days=1)))
		self.assertIn("weekly", answer["note"])
		day_after = str(daily_pay.next_boundary() + datetime.timedelta(days=8))
		self.assertIn("ended", " ".join(daily_pay.eligibility(WORKER_EMPLOYEE, day_after)["reasons"]))

	def test_not_offered_until_the_farm_turns_it_on(self):
		flags.upsert(daily_pay.FLAG, "Flag", False, company=MAIN, description="test", owner_area="payroll", active=True)
		STORE.commit()
		with self.assertRaisesRegex(frappe.ValidationError, "not offered"):
			self.ask("1")


class TheSupervisor(PhoneCase):
	def test_a_worker_cannot_see_or_run_todays_pay(self):
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_todays_pay()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.run_todays_pay()

	def test_today_names_who_waits_and_why(self):
		self.ask("1")
		self.be(BOSS)
		later = mobile_api.get_todays_pay(day=str(daily_pay.next_boundary() + datetime.timedelta(days=1)))
		self.assertEqual(later["company"], MAIN)
		self.assertFalse(later["ready"])
		self.assertIn("has not happened yet", later["message"])
		self.be(BOSS)
		past = mobile_api.get_todays_pay(day="2026-01-05")
		self.assertFalse(past["ready"], "nobody was on daily pay then")
		self.assertEqual(past["pay"], [])
