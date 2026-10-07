# SPDX-License-Identifier: MIT
"""v0.267.1 — the four access-audit fixes Tim approved (2026-10-06), each pinned with TWO COMPANIES ON ONE SITE.

1. The routing number is masked like the account number, except for payroll / HR (the private-HR gate).
2. The employee file's login ID and device / login IPs are a System Manager's alone (and the worker's own).
3. Accident and leave lists — rows AND every total — are the selected company's only (before Constancy).
4. Contacts are scoped by farm entity on the phone: search, the where-met edit, duplicate hints and merges.
"""

import frappe

from erpnext_mcp import business_cards, employee_file, security
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import ach

from .harness import STORE, set_roles
from .test_api_mobile import MAIN, OTHER, OUTSIDER, OUTSIDER_EMPLOYEE, WORKER, WORKER_EMPLOYEE, MobileAPITestCase
from .test_business_cards import SHEPPARD, ContactSite

BOSS = "boss@example.test"
BOSS_EMPLOYEE = "EMP-BOSS"


class TwoCompanies(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Employee", [{"name": BOSS_EMPLOYEE, "employee_name": "Bo Ss", "user_id": BOSS, "company": MAIN,
		                         "status": "Active"}])


# ── 1. routing number ──────────────────────────────────────────────────────────
class RoutingIsMasked(TwoCompanies):
	def setUp(self):
		super().setUp()
		STORE.seed("Employee Bank Account", [
			{"name": "EBA-ANA", "employee": WORKER_EMPLOYEE, "company": MAIN, "bank_name": "Columbia Bank",
			 "routing_number": "123006800", "account_number_last_four": "4321", "account_type": "Checking",
			 "allocation_type": "Full", "status": "Active", "priority": 1},
		])
		STORE.commit()

	def test_a_worker_sees_the_last_four_of_their_own_routing_number(self):
		self.be(WORKER)
		row = mobile_api.list_my_bank_accounts()["accounts"][0]
		self.assertEqual(row["routing_number"], "*****6800")
		self.assertEqual(row["routing_number_last_four"], "6800")
		self.assertEqual(row["account_number_masked"], "****4321")
		self.assertNotIn("123006800", str(row))

	def test_payroll_hr_sees_it_whole_and_a_farm_manager_does_not(self):
		setattr(frappe.local, security._CALLING_USER_KEY, BOSS)
		self.addCleanup(lambda: setattr(frappe.local, security._CALLING_USER_KEY, ""))
		set_roles(BOSS, ["HR Manager"])
		self.assertTrue(ach.sees_full_routing(BOSS))
		self.assertEqual(ach._safe_row("EBA-ANA")["routing_number"], "123006800")
		set_roles(BOSS, ["Farm Manager"])
		self.assertFalse(ach.sees_full_routing(BOSS))
		self.assertEqual(ach._safe_row("EBA-ANA")["routing_number"], "*****6800")


# ── 2. employee file: login ID and IPs ─────────────────────────────────────────
class LoginAndIPsAreTheSystemManagers(TwoCompanies):
	def _access(self, viewer_roles):
		set_roles(BOSS, viewer_roles)
		allowed = employee_file.visible_sections(BOSS, WORKER_EMPLOYEE)
		emp = {"user_id": WORKER}
		return employee_file._access(emp, bool(allowed.get("_devices_only")), bool(allowed.get("_identity")))

	def test_hr_manager_hr_user_and_farm_manager_do_not_see_them(self):
		for roles in (["HR Manager"], ["HR User"], ["Farm Manager"]):
			access = self._access(roles)
			self.assertIsNone(access["user"], roles)
			self.assertTrue(access["has_login"], roles)
			self.assertTrue(all("last_ip" not in d for d in access["devices"]), roles)
			self.assertTrue(all("from" not in r for r in access.get("recent_logins") or []), roles)

	def test_a_system_manager_does_and_the_worker_sees_their_own(self):
		self.assertEqual(self._access(["System Manager"])["user"], WORKER)
		own = employee_file.visible_sections(WORKER, WORKER_EMPLOYEE)
		self.assertTrue(own.get("_identity"))


# ── 3. accident and leave totals: one company ─────────────────────────────────
class OneCompanysTotals(TwoCompanies):
	def setUp(self):
		super().setUp()
		STORE.seed("Accident Report", [
			{"name": "ACC-MAIN-1", "company": MAIN, "status": "Open", "severity": "First Aid", "osha_recordable": "Yes",
			 "occurred_at": "2026-09-01 08:00:00", "days_away_from_work": 2, "injured_person": WORKER},
			{"name": "ACC-OTHER-1", "company": OTHER, "status": "Open", "severity": "Lost Time",
			 "osha_recordable": "Undetermined", "occurred_at": "2026-09-02 08:00:00", "days_away_from_work": 9},
			{"name": "ACC-OTHER-2", "company": OTHER, "status": "Open", "severity": "Lost Time",
			 "osha_recordable": "Yes", "occurred_at": "2026-09-03 08:00:00", "days_away_from_work": 5},
		])
		STORE.seed("Leave Application", [
			{"name": "LV-MAIN-1", "employee": WORKER_EMPLOYEE, "company": MAIN, "status": "Open", "leave_type": "Sick Leave",
			 "from_date": "2026-09-01", "to_date": "2026-09-01", "total_leave_days": 1, "description": "flu"},
			{"name": "LV-OTHER-1", "employee": OUTSIDER_EMPLOYEE, "company": OTHER, "status": "Open",
			 "leave_type": "Sick Leave", "from_date": "2026-09-01", "to_date": "2026-09-05", "total_leave_days": 5,
			 "description": "surgery"},
		])
		STORE.commit()

	def _no_other(self, data):
		text = str(data)
		self.assertNotIn("ACC-OTHER", text)
		self.assertNotIn("LV-OTHER", text)
		self.assertNotIn("surgery", text)

	def test_a_foreman_of_one_company_counts_only_it(self):
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be(WORKER)
		acc = mobile_api.list_accident_reports()
		self.assertEqual(acc["company"], MAIN)
		self.assertEqual((acc["report_count"], acc["open_count"], acc["osha_recordable_count"], acc["undetermined_count"],
		                  acc["days_away_total"]), (1, 1, 1, 0, 2))
		self._no_other(acc)
		leave = mobile_api.list_leave_requests()
		self.assertEqual((leave["count"], leave["pending_count"], leave["total_days"]), (1, 1, 1))
		self.assertEqual(leave["by_status"], {"Open": 1})
		self._no_other(leave)

	def test_another_company_is_refused_by_name(self):
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be(WORKER)
		for call in (lambda: mobile_api.list_accident_reports(company=OTHER),
		             lambda: mobile_api.list_leave_requests(company=OTHER)):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_a_two_company_manager_sees_one_company_at_a_time(self):
		self.enrol(email=BOSS, name="Bo Ss", role="Foreman", entities=[MAIN, OTHER])
		self.be(BOSS)
		main = mobile_api.list_accident_reports(company=MAIN)
		self.assertEqual(main["report_count"], 1)
		self._no_other(main)
		other = mobile_api.list_accident_reports(company=OTHER)
		self.assertEqual((other["report_count"], other["days_away_total"], other["undetermined_count"]), (2, 14, 1))
		self.assertNotIn("ACC-MAIN", str(other))
		leave = mobile_api.list_leave_requests(company=OTHER)
		self.assertEqual((leave["count"], leave["total_days"]), (1, 5))
		self.assertNotIn("LV-MAIN", str(leave))


# ── 4. contacts by farm entity ─────────────────────────────────────────────────
OTHER_CARD = {"first_name": "Olga", "last_name": "Other", "company": "Other Co",
              "phones": [{"number": "(541) 555-0123", "kind": "mobile"}], "met_at": "Other farm"}


class ContactsByEntity(ContactSite, TwoCompanies):
	def setUp(self):
		super().setUp()
		self.enrol(email=OUTSIDER, name="Ben Ortiz", role="Foreman", entities=[OTHER])
		set_roles(WORKER, ["Field Worker", "Foreman"])
		set_roles(OUTSIDER, ["Field Worker", "Foreman"])

	def _other_contact(self):
		self.be(OUTSIDER)
		name = mobile_api.save_business_card(card=OTHER_CARD, client_request_id="other-1")["name"]
		STORE.commit()
		return name

	def test_new_cards_are_stamped_and_another_entity_cannot_see_them(self):
		theirs = self._other_contact()
		self.assertEqual(frappe.db.get_value("Contact", theirs, business_cards.ENTITY), OTHER)
		self.be(WORKER)
		mine = mobile_api.save_business_card(card=SHEPPARD, client_request_id="mine-1")["name"]
		self.assertEqual(frappe.db.get_value("Contact", mine, business_cards.ENTITY), MAIN)
		found = [c["name"] for c in mobile_api.search_contacts(query="")["contacts"]]
		self.assertIn(mine, found)
		self.assertNotIn(theirs, found)
		self.assertEqual(mobile_api.search_contacts(query="olga")["contacts"], [])

	def test_no_duplicate_hint_edit_or_merge_across_entities(self):
		theirs = self._other_contact()
		self.be(WORKER)
		preview = mobile_api.preview_business_card(card=OTHER_CARD)
		self.assertEqual(preview["duplicates"], [])
		with self.assertRaises(Exception):
			mobile_api.update_contact_where_met(contact=theirs, met_at="x")
		with self.assertRaises(Exception):
			mobile_api.save_business_card(card=OTHER_CARD, merge_into=theirs, client_request_id="merge-1")
		self.assertEqual(frappe.db.get_value("Contact", theirs, "card_met_at"), "Other farm")

	def test_older_contacts_join_an_unambiguous_entity_else_stay_with_their_capturer(self):
		business_cards.ensure_fields()
		STORE.seed("Contact", [
			{"name": "OLD-ANA", "first_name": "Old", "card_captured_by": WORKER},
			{"name": "OLD-BOSS", "first_name": "Older", "card_captured_by": BOSS},
		])
		self.enrol(email=BOSS, name="Bo Ss", role="Foreman", entities=[MAIN, OTHER])
		STORE.commit()
		report = business_cards.backfill_entities()
		self.assertEqual(frappe.db.get_value("Contact", "OLD-ANA", business_cards.ENTITY), MAIN)
		self.assertFalse(frappe.db.get_value("Contact", "OLD-BOSS", business_cards.ENTITY))
		self.assertGreaterEqual(report["unassigned"], 1)
		self.be(WORKER)
		found = [c["name"] for c in mobile_api.search_contacts(query="old")["contacts"]]
		self.assertEqual(found, ["OLD-ANA"])
		set_roles(BOSS, ["Foreman"])
		self.be(BOSS)
		self.assertIn("OLD-BOSS", [c["name"] for c in mobile_api.search_contacts(query="older")["contacts"]])
