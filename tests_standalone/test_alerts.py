# SPDX-License-Identifier: MIT
"""The Kairotic Compliance Calendar — Sprint 7 Wave 3.

THE SHAPE OF THIS FILE IS THE SHAPE OF THE CLAIM. Every rule gets the same three
tests, because the claim being made about every rule is the same one:

    FIRES WHEN RIPE        the condition is genuinely true, and it raises
    SILENT WHEN UNRIPE     the DATE has arrived and the STATE has not, and it
                           raises nothing
    AUTO-DISMISSES         the underlying thing gets done, and the alert goes
                           away by itself on the next sweep

The middle one is the whole difference between this and a calendar reminder, and
it is the one that would be easiest to leave out. A rule that only had the first
test would pass just as well if it fired on every record every night.

Concretely, the unripe cases each rule has to survive:

  * a certificate 200 days out — outside its issuing body's lead time;
  * a policy that is superseded rather than in force;
  * a block with a stale water test that NOBODY IS SPRAYING;
  * a shower block, which takes no habitability inspection;
  * a shed on the same parcel, which is not a worker facility;
  * an employee whose I-9 is Pending — inside their lawful three-day window;
  * a former employee whose I-9 expired after they left;
  * a filing that was answered;
  * an audit action with no deadline to be past.

IDEMPOTENCE IS TESTED BY RUNNING THE SWEEP TWICE. The alert key is derived from
the rule and the source record and from nothing that changes daily, which is what
stops a certificate ticking from 60 days out to 59 spawning a second alert every
morning and discarding the snooze somebody set on the first. `TheSweepIsIdempotent`
runs it three times and counts.

A HUMAN DISMISSAL IS NEVER REOPENED, and an auto-dismissal is. Both are tested,
because they are the same mechanism pointed in opposite directions and getting
either wrong is silent.
"""

import frappe

from erpnext_mcp import alerts

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import STORE, add_field

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"get_compliance_calendar",
		"list_compliance_rules",
		"get_audit_readiness",
		"refresh_compliance_alerts",
		"snooze_alert",
		"dismiss_alert",
		"dismiss_alert_bulk",
		"create_compliance_policy",
		"update_compliance_policy",
		"create_certification",
		"renew_certification",
		"update_certification",
		"create_regulatory_filing",
		"update_regulatory_filing",
		"create_audit_event",
		"update_audit_event",
		"close_audit_event",
		"create_parcel",
		"create_field",
		"update_field",
		"create_housing_unit",
		"update_housing_unit",
	)
}

#: The fake site's today. Every fixture date is placed relative to this rather
#: than to a real clock, so the suite does not start failing next month.
TODAY = "2026-07-24"


def days_from_today(days: int) -> str:
	return str(frappe.utils.add_days(frappe.utils.getdate(TODAY), days))


class AlertTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def _set_unit(self, unit, **values):
		"""v0.337.1. Inspection and detector dates move only through their records (update_housing_unit refuses
		them); these tests are about the alert, which reads the column, so the column is set directly."""
		frappe.db.set_value("Housing Unit", unit, values)
		STORE.commit()

	def sweep(self, **kwargs):
		return self.tool_data("refresh_compliance_alerts", kwargs)

	def live(self, alert_type=None) -> list:
		"""Every alert currently on the calendar, optionally of one rule."""
		data = self.tool_data(
			"get_compliance_calendar",
			{"alert_type": alert_type} if alert_type else {},
		)
		out = []
		for group in data["by_category"].values():
			out.extend(group["alerts"])
		return out

	def raised(self, alert_type: str) -> list:
		self.sweep()
		return [alert for alert in self.live() if alert["alert_type"] == alert_type]

	def assertFires(self, alert_type: str, severity=None):
		found = self.raised(alert_type)
		self.assertTrue(found, f"{alert_type} did not fire when it was ripe")
		if severity:
			self.assertEqual(found[0]["severity"], severity)
		return found[0]

	def assertSilent(self, alert_type: str):
		found = self.raised(alert_type)
		self.assertFalse(found, f"{alert_type} fired when it was NOT ripe: {found}")

	def assertAutoDismissed(self, alert_type: str):
		"""The condition resolved, so the sweep takes it off without being asked."""
		report = self.sweep()
		self.assertFalse(
			[alert for alert in self.live() if alert["alert_type"] == alert_type],
			f"{alert_type} is still on the calendar after its condition resolved",
		)
		return report

	# ── fixture helpers ─────────────────────────────────────────────────────
	def a_certificate(self, name="GlobalGAP 2026", expires_in_days=45, **overrides):
		payload = {
			"cert_name": name,
			"cert_type": "GlobalGAP",
			"company": MAIN,
			"holder": MAIN,
			"issuing_body": "Primus Auditing Ops",
			"issued_date": days_from_today(-300),
			"expiration_date": days_from_today(expires_in_days),
			"renewal_window_days": 90,
		}
		payload.update(overrides)
		return self.tool_data("create_certification", payload)

	def a_policy(self, name="Harvest Hygiene SOP", review_in_days=-30, **overrides):
		payload = {
			"policy_name": name,
			"category": "Harvest Hygiene",
			"company": MAIN,
			"version": "v3",
			"effective_date": days_from_today(-400),
			"review_due_date": days_from_today(review_in_days),
		}
		payload.update(overrides)
		return self.tool_data("create_compliance_policy", payload)

	def a_block(self, name="Yellow Camp Block 3", sprayed_days_ago=11, tested_days_ago=118, **overrides):
		self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 131.43,
				"county": "Wasco",
				"state": "OR",
				"use_type": "Orchard",
			},
		)
		payload = {
			"parcel": "Mill Creek",
			"field_name": name,
			"acreage": 12.5,
			"variety": "Bing",
			"condition": "Good",
		}
		payload.update(overrides)
		data = self.tool_data("create_field", payload)
		row = STORE.get_raw("Field", data["name"])
		row["last_spray_date"] = None if sprayed_days_ago is None else days_from_today(-sprayed_days_ago)
		row["water_test_last_date"] = None if tested_days_ago is None else days_from_today(-tested_days_ago)
		return data

	def a_cabin(self, name="MC-Cabin-01", inspected_days_ago=400, detector_days_ago=400, **overrides):
		if not STORE.get_raw("Parcel", "Mill Creek - MC"):
			self.tool_data(
				"create_parcel",
				{
					"owning_entity": MAIN,
					"parcel_name": "Mill Creek",
					"acreage": 131.43,
					"county": "Wasco",
					"state": "OR",
					"use_type": "Orchard",
				},
			)
		payload = {
			"parcel": "Mill Creek",
			"unit_name": name,
			"unit_type": "Cabin",
			"square_footage": 384,
			"capacity": 4,
			"condition": "Good",
			"fsma_worker_facility": True,
		}
		payload.update(overrides)
		data = self.tool_data("create_housing_unit", payload)
		row = STORE.get_raw("Housing Unit", data["name"])
		row["last_habitability_inspection"] = (
			None if inspected_days_ago is None else days_from_today(-inspected_days_ago)
		)
		for field in ("smoke_detector_last_test", "co_detector_last_test"):
			row[field] = None if detector_days_ago is None else days_from_today(-detector_days_ago)
		return data

	def an_employee(self, i9="Expired", **overrides):
		install_hrms()
		for fieldname, fieldtype in (
			("i9_status", "Select"),
			("w4_status", "Select"),
			("jurisdiction", "Data"),
			("flc_license_status", "Data"),
			("flc_license_expiration", "Date"),
		):
			add_field("Employee", fieldname, fieldtype=fieldtype)
		row = STORE.get_raw("Employee", "HR-EMP-00001")
		row["i9_status"] = i9
		row["company"] = MAIN
		row.update(overrides)
		return row

	def a_filing(self, response_due_in_days=10, **overrides):
		payload = {
			"filing_name": "Pesticide Application Report 2026-Q2",
			"agency": "ODA",
			"filing_type": "Pesticide-Application-Report",
			"company": MAIN,
			"submission_date": days_from_today(-40),
			"docket_number": "ODA-2026-0417",
			"response_due_date": days_from_today(response_due_in_days),
		}
		payload.update(overrides)
		return self.tool_data("create_regulatory_filing", payload)

	def an_audit(self, due_in_days=-25, **overrides):
		payload = {
			"audit_name": "PrimusGFS 2026",
			"audit_type": "PrimusGFS",
			"audit_date": days_from_today(-53),
			"auditor": "J. Reyes",
			"company": MAIN,
			"result": "Passed With Conditions",
			"corrective_actions": [
				{
					"finding": "Hand wash station in Block 4 had no soap at 0800",
					"severity": "Major",
					"due_date": None if due_in_days is None else days_from_today(due_in_days),
				}
			],
		}
		payload.update(overrides)
		return self.tool_data("create_audit_event", payload)


# ── the framework itself ────────────────────────────────────────────────────
class TheRuleSet(AlertTestCase):
	def test_every_rule_states_its_kairotic_gate(self):
		"""A rule whose author cannot say what makes it RIPE has written a
		calendar reminder, which belongs in somebody's phone."""
		for rule in alerts.describe():
			with self.subTest(rule=rule["alert_type"]):
				self.assertGreater(
					len(rule["kairotic_gate"]),
					80,
					f"{rule['alert_type']} does not say what state makes it fire",
				)

	def test_registering_a_rule_without_a_gate_is_refused(self):
		"""The requirement is enforced at registration, not by convention."""
		with self.assertRaises(RuntimeError) as caught:
			alerts.register(
				alerts.Rule(
					key="gateless", title="x", category="Other", scan=lambda context: [], kairotic_gate=""
				)
			)
		self.assertIn("kairotic_gate", str(caught.exception))

	def test_every_rule_names_the_framework_it_serves(self):
		for rule in alerts.describe():
			with self.subTest(rule=rule["alert_type"]):
				self.assertTrue(rule["framework"].strip())

	def test_the_tool_reports_which_rules_cannot_run_here(self):
		"""An empty category is not the same as a clean one."""
		data = self.tool_data("list_compliance_rules", {})
		self.assertEqual(data["rule_count"], len(alerts.names()))
		self.assertIn("not evidence that anybody did the work", data["note"])


class TheSweepIsIdempotent(AlertTestCase):
	def setUp(self):
		super().setUp()
		self.a_certificate(expires_in_days=45)

	def test_three_sweeps_produce_one_alert(self):
		"""The key carries the rule and the record and nothing that moves daily."""
		counts = []
		for _ in range(3):
			self.sweep()
			counts.append(len(STORE.rows("Compliance Alert")))
		self.assertEqual(counts, [1, 1, 1], f"the sweep duplicated its own alerts: {counts}")

	def test_the_second_sweep_refreshes_rather_than_creating(self):
		self.sweep()
		report = self.sweep()
		self.assertEqual(report["created"], 0)
		self.assertEqual(report["refreshed"], 1)

	def test_a_snooze_survives_the_next_sweep(self):
		"""THE FAILURE IDEMPOTENCE EXISTS TO PREVENT. A key that moved would give
		the operator a brand new alert every morning and silently discard the
		snooze they set on yesterday's."""
		self.sweep()
		name = self.live()[0]["name"]
		self.tool_data("snooze_alert", {"alert": name, "until_date": days_from_today(30)})
		self.sweep()
		self.assertEqual(STORE.get_raw("Compliance Alert", name)["snoozed_until"], days_from_today(30))

	def test_first_seen_is_never_moved_forward(self):
		"""An alert open four months is evidence of four months. Resetting it
		would turn a chronic problem into a new one every morning."""
		self.sweep()
		name = self.live()[0]["name"]
		STORE.get_raw("Compliance Alert", name)["first_seen"] = "2026-01-01"
		self.sweep()
		self.assertEqual(STORE.get_raw("Compliance Alert", name)["first_seen"], "2026-01-01")

	def test_a_dry_run_writes_nothing(self):
		report = self.sweep(dry_run=True)
		self.assertEqual(report["created"], 1)
		self.assertEqual(len(STORE.rows("Compliance Alert")), 0)


# ── 1. certification_expiring ───────────────────────────────────────────────
class CertificationExpiring(AlertTestCase):
	RULE = "certification_expiring"

	def test_it_fires_inside_the_issuing_bodys_own_lead_time(self):
		self.a_certificate(expires_in_days=45)
		alert = self.assertFires(self.RULE, "Warning")
		self.assertIn("renewal window", alert["message"])

	def test_it_is_silent_two_hundred_days_out(self):
		"""Not ripe: the renewal cannot usefully be started and the agency would
		not accept it."""
		self.a_certificate(expires_in_days=200)
		self.assertSilent(self.RULE)

	def test_the_window_is_read_from_the_record_not_from_a_fixed_number(self):
		"""A certificate whose issuing body takes 200 days says so on itself, and
		the rule believes it."""
		self.a_certificate(expires_in_days=150, renewal_window_days=200)
		self.assertFires(self.RULE, "Warning")

	def test_it_escalates_to_critical_inside_thirty_days(self):
		self.a_certificate(expires_in_days=20)
		alert = self.assertFires(self.RULE, "Critical")
		self.assertIn("may not come back before the lapse", alert["message"])

	def test_an_expired_certificate_is_critical_and_says_how_long(self):
		self.a_certificate(expires_in_days=-15)
		alert = self.assertFires(self.RULE, "Critical")
		self.assertIn("EXPIRED 15 day(s) ago", alert["message"])

	def test_a_superseded_certificate_raises_nothing(self):
		"""A newer certificate covers it. Alerting would put a permanent row on
		the calendar that nothing can clear."""
		self.a_certificate(expires_in_days=10, status="Superseded")
		self.assertSilent(self.RULE)

	def test_a_revoked_certificate_raises_nothing(self):
		"""It is not coming back, so it is not a renewal task."""
		self.a_certificate(expires_in_days=10, status="Revoked")
		self.assertSilent(self.RULE)

	def test_renewing_it_auto_dismisses_the_alert(self):
		self.a_certificate(expires_in_days=45)
		self.assertFires(self.RULE)
		self.tool_data(
			"renew_certification",
			{
				"certification": "GlobalGAP 2026",
				"new_expiration": days_from_today(400),
				"what_was_done": "passed the 2026 re-audit and paid the scheme fee",
			},
		)
		self.assertAutoDismissed(self.RULE)

	def test_an_applicator_licence_is_filed_under_workforce_not_certifications(self):
		"""One rule, two categories — a licence is somebody's ability to work."""
		self.a_certificate("Applicator — R. Mendez", cert_type="Applicator License", expires_in_days=20)
		self.assertEqual(self.assertFires(self.RULE)["category"], "Workforce")


class WhoTheAlertIsAbout(AlertTestCase):
	"""v0.106.0. `subject_employee`, and why a column beats a string search.

	THE HANDSET WAS READING A NAME OUT OF PROSE. The compliance-to-task picker
	removes the person an alert is about from the list of people it may be handed
	to — nobody signs off their own gap — and the only person on an alert was
	inside the message: *"Applicator License — Timothy Polehn 2025 EXPIRED 36
	day(s) ago"*. So the app matched candidate names against that sentence, which
	is a whole-word string search standing in for a foreign key and fails in both
	directions: a worker spelled differently on the certificate is not excluded,
	and an alert that happens to quote a second person excludes them too.

	EMPTY IS A REAL ANSWER AND IS THE COMMON ONE. Most alerts are about the
	operation, and the tests below pin that as hard as they pin the resolution —
	because a caller must never be able to read a blank as licence to guess.
	"""

	def a_person(self, docname, full_name, company=MAIN, status="Active"):
		STORE.seed(
			"Employee",
			[
				{
					"name": docname,
					"employee_name": full_name,
					"company": company,
					"status": status,
					"date_of_joining": "2025-01-01",
				}
			],
		)
		return docname

	def subject_of(self, alert_type):
		alert = self.assertFires(alert_type)
		return frappe.db.get_value(alerts.base.ALERT_DOCTYPE, alert["name"], "subject_employee")

	def test_a_certificate_naming_its_holder_resolves_to_that_employee(self):
		"""THE ALERT THIS WHOLE MECHANISM EXISTS FOR. `Certification.holder` is
		free text — the register holds licences issued to the OPERATION as well
		as to people, and a Link to Employee could not hold both — so an
		applicator licence identifies its holder by name and nothing else."""
		self.a_person("HR-EMP-TIM", "Timothy Polehn")
		self.a_certificate(
			"Applicator License — Timothy Polehn 2025",
			cert_type="Applicator License",
			holder="Timothy Polehn",
			expires_in_days=-36,
		)
		self.assertEqual(self.subject_of("certification_expiring"), "HR-EMP-TIM")

	def test_a_certificate_held_by_the_operation_is_about_nobody(self):
		"""A GlobalGAP certificate is held by the farm. The default fixture's
		`holder` IS the company, which is exactly the case that must not resolve
		to whoever happens to share a name with an entity."""
		self.a_certificate(expires_in_days=45)
		self.assertFalse(self.subject_of("certification_expiring"))

	def test_two_employees_of_one_name_resolve_to_neither(self):
		"""AMBIGUOUS IS NOT A SUBJECT, IT IS TWO PEOPLE — and on this field
		guessing does not merely mislabel a row, it removes the wrong worker from
		a picker and can hide the only person qualified to do the job."""
		self.a_person("HR-EMP-J1", "Juan Garcia")
		self.a_person("HR-EMP-J2", "Juan Garcia")
		self.a_certificate(
			"CDL — J. Garcia",
			cert_type="Commercial Driver License",
			holder="Juan Garcia",
			expires_in_days=-5,
		)
		self.assertFalse(self.subject_of("certification_expiring"))

	def test_a_holder_at_another_company_is_not_this_company_s_subject(self):
		"""The name is only ever resolved within the entity whose alert it is, so
		a holder at one farm cannot become a subject at another."""
		self.a_person("HR-EMP-ELSE", "Rosa Delgado", company=OTHER)
		self.a_certificate(
			"Applicator — R. Delgado",
			cert_type="Applicator License",
			holder="Rosa Delgado",
			expires_in_days=-5,
		)
		self.assertFalse(self.subject_of("certification_expiring"))

	def test_an_alert_about_a_cabin_is_about_nobody(self):
		"""A stale detector and an uninspected cabin are about the OPERATION.
		Inventing a person for them would put somebody's name on a gap that is
		not theirs."""
		self.a_cabin()
		self.assertFalse(self.subject_of("housing_inspection_overdue"))

	def test_the_column_is_rewritten_on_a_refresh_not_only_on_a_create(self):
		"""DERIVED, so it follows the record. A certificate reassigned to another
		holder is about somebody else from that night on, and an alert still
		naming the previous holder would remove the wrong person from the
		picker."""
		self.a_person("HR-EMP-TIM", "Timothy Polehn")
		self.a_person("HR-EMP-ANA", "Ana Ruiz")
		self.a_certificate(
			"Applicator — handover",
			cert_type="Applicator License",
			holder="Timothy Polehn",
			expires_in_days=-5,
		)
		self.assertEqual(self.subject_of("certification_expiring"), "HR-EMP-TIM")

		frappe.db.set_value("Certification", "Applicator — handover", "holder", "Ana Ruiz")
		self.assertEqual(self.subject_of("certification_expiring"), "HR-EMP-ANA")


# ── 2. policy_review_overdue ────────────────────────────────────────────────
class PolicyReviewOverdue(AlertTestCase):
	RULE = "policy_review_overdue"

	def test_it_fires_on_a_procedure_in_force_past_its_own_review_date(self):
		self.a_policy(review_in_days=-30)
		alert = self.assertFires(self.RULE, "Warning")
		self.assertIn("30 day(s) ago", alert["message"])

	def test_it_is_silent_before_the_review_date(self):
		self.a_policy(review_in_days=30)
		self.assertSilent(self.RULE)

	def test_a_draft_raises_nothing(self):
		"""A procedure that was never adopted cannot meaningfully be reviewed."""
		self.a_policy(review_in_days=-30, status="Draft")
		self.assertSilent(self.RULE)

	def test_a_superseded_procedure_raises_nothing(self):
		self.a_policy(review_in_days=-30, status="Retired")
		self.assertSilent(self.RULE)

	def test_moving_the_review_date_forward_auto_dismisses_it(self):
		"""Which is what reviewing it looks like in the record."""
		self.a_policy(review_in_days=-30)
		self.assertFires(self.RULE)
		self.tool_data(
			"update_compliance_policy",
			{"policy": "Harvest Hygiene SOP", "review_due_date": days_from_today(365)},
		)
		self.assertAutoDismissed(self.RULE)


# ── 3. water_test_stale ─────────────────────────────────────────────────────
class WaterTestStale(AlertTestCase):
	"""THE CLEAREST KAIROTIC GATE IN THE ENGINE, and the one worth the most tests.

	The gate is the SPRAY, not the date. FSMA Subpart E is engaged by water
	contacting a crop, and on a tree fruit block that is mostly what goes through
	the sprayer — so an untested block nobody is spraying is dormant rather than
	unsafe, and the same block becomes Critical the day it re-enters rotation.
	"""

	RULE = "water_test_stale"

	def test_it_fires_on_a_block_in_rotation_with_a_stale_test(self):
		self.a_block(sprayed_days_ago=11, tested_days_ago=118)
		alert = self.assertFires(self.RULE, "Critical")
		self.assertIn("sprayed 11 day(s) ago", alert["message"])
		self.assertIn("Subpart E", alert["message"])

	def test_it_is_silent_on_a_block_nobody_is_spraying(self):
		"""THE GATE. Same stale water test, no spray activity — not unsafe, dormant."""
		self.a_block(sprayed_days_ago=400, tested_days_ago=118)
		self.assertSilent(self.RULE)

	def test_it_is_silent_on_a_block_that_has_never_been_sprayed(self):
		self.a_block(sprayed_days_ago=None, tested_days_ago=None)
		self.assertSilent(self.RULE)

	def test_it_is_silent_on_fallow_ground(self):
		self.a_block(sprayed_days_ago=11, tested_days_ago=118, condition="Fallow")
		self.assertSilent(self.RULE)

	def test_it_is_silent_when_the_test_is_current(self):
		self.a_block(sprayed_days_ago=11, tested_days_ago=30)
		self.assertSilent(self.RULE)

	def test_a_never_tested_block_in_rotation_fires_and_says_so(self):
		alert_source = self.a_block(sprayed_days_ago=11, tested_days_ago=None)
		alert = self.assertFires(self.RULE, "Critical")
		self.assertIn("NO agricultural water test on record", alert["message"])
		self.assertEqual(alert["source_docname"], alert_source["name"])

	def test_a_dormant_block_starts_firing_the_day_it_is_sprayed_again(self):
		"""The gate opening, which is the whole design in one test."""
		block = self.a_block(sprayed_days_ago=400, tested_days_ago=118)
		self.assertSilent(self.RULE)
		STORE.get_raw("Field", block["name"])["last_spray_date"] = days_from_today(-1)
		self.assertFires(self.RULE, "Critical")

	def test_performing_the_water_test_auto_dismisses_it(self):
		block = self.a_block(sprayed_days_ago=11, tested_days_ago=118)
		self.assertFires(self.RULE)
		self.tool_data("update_field", {"field": block["name"], "water_test_last_date": days_from_today(-1)})
		self.assertAutoDismissed(self.RULE)


# ── 4. housing_inspection_overdue ───────────────────────────────────────────
class HousingInspectionOverdue(AlertTestCase):
	RULE = "housing_inspection_overdue"

	def test_it_fires_on_a_cabin_uninspected_for_over_a_year(self):
		self.a_cabin(inspected_days_ago=400)
		alert = self.assertFires(self.RULE, "Warning")
		self.assertIn("sleep in it tonight", alert["message"])

	def test_it_is_silent_inside_the_year(self):
		self.a_cabin(inspected_days_ago=100)
		self.assertSilent(self.RULE)

	def test_a_never_inspected_cabin_counts_as_overdue(self):
		"""The answer that gets somebody to go and look."""
		self.a_cabin(inspected_days_ago=None)
		self.assertIn("never had a habitability inspection", self.assertFires(self.RULE)["message"])

	def test_a_shower_block_raises_nothing(self):
		"""Nobody is assigned to one, so there is nobody to protect."""
		self.a_cabin("MC-BathHouse", unit_type="Toilet-Shower", inspected_days_ago=None)
		self.assertSilent(self.RULE)

	def test_an_uninhabitable_unit_raises_nothing(self):
		"""Assignments into it are already refused."""
		self.a_cabin(inspected_days_ago=None, condition="Uninhabitable")
		self.assertSilent(self.RULE)

	def test_it_starts_firing_again_once_the_unit_is_marked_habitable(self):
		unit = self.a_cabin(inspected_days_ago=None, condition="Uninhabitable")
		self.assertSilent(self.RULE)
		self.tool_data("update_housing_unit", {"unit": unit["name"], "condition": "Good"})
		self.assertFires(self.RULE)

	def test_recording_an_inspection_auto_dismisses_it(self):
		unit = self.a_cabin(inspected_days_ago=400)
		self.assertFires(self.RULE)
		# v0.337.1. The register's date moves through a Housing Inspection; the rule reads the column, set here.
		self._set_unit(unit["name"], last_habitability_inspection=days_from_today(-2))
		self.assertAutoDismissed(self.RULE)


# ── 5. housing_detector_test_stale ──────────────────────────────────────────
class DetectorTestStale(AlertTestCase):
	RULE = "housing_detector_test_stale"

	def test_it_fires_on_a_worker_facility_with_stale_detector_tests(self):
		self.a_cabin(inspected_days_ago=10, detector_days_ago=400)
		alert = self.assertFires(self.RULE, "Warning")
		self.assertIn("propane heater", alert["message"])

	def test_it_is_silent_when_both_tests_are_inside_the_year(self):
		self.a_cabin(inspected_days_ago=10, detector_days_ago=30)
		self.assertSilent(self.RULE)

	def test_a_building_that_is_not_a_worker_facility_raises_nothing(self):
		"""A shed on the same parcel is not a bunkhouse."""
		self.a_cabin(inspected_days_ago=10, detector_days_ago=None, fsma_worker_facility=False)
		self.assertSilent(self.RULE)

	def test_a_never_tested_detector_fires_and_names_which_one(self):
		self.a_cabin(inspected_days_ago=10, detector_days_ago=None)
		self.assertIn("no smoke detector test has ever been recorded", self.assertFires(self.RULE)["message"])

	def test_recording_both_tests_auto_dismisses_it(self):
		unit = self.a_cabin(inspected_days_ago=10, detector_days_ago=400)
		self.assertFires(self.RULE)
		self._set_unit(
			unit["name"],
			smoke_detector_last_test=days_from_today(-2),
			co_detector_last_test=days_from_today(-2),
		)
		self.assertAutoDismissed(self.RULE)

	def test_recording_only_one_of_the_two_keeps_it_up(self):
		"""A cabin with a tested smoke alarm and an untested CO alarm is still a
		cabin with an untested CO alarm."""
		unit = self.a_cabin(inspected_days_ago=10, detector_days_ago=400)
		self._set_unit(unit["name"], smoke_detector_last_test=days_from_today(-2))
		self.assertIn("CO detector", self.assertFires(self.RULE)["message"])


# ── 6. i9_expired ───────────────────────────────────────────────────────────
class I9Expired(AlertTestCase):
	RULE = "i9_expired"

	def test_it_fires_on_an_active_employee_whose_i9_expired(self):
		self.an_employee(i9="Expired")
		alert = self.assertFires(self.RULE, "Critical")
		self.assertIn("cannot lawfully be put on a crew tomorrow", alert["message"])

	def test_a_pending_i9_raises_nothing(self):
		"""Somebody inside their lawful three-day window is not yet a problem."""
		self.an_employee(i9="Pending")
		self.assertSilent(self.RULE)

	def test_a_verified_i9_raises_nothing(self):
		self.an_employee(i9="Verified")
		self.assertSilent(self.RULE)

	def test_a_former_employee_raises_nothing(self):
		"""An expired I-9 on somebody who left blocks nothing, and alerting on it
		would fill the calendar with people who are not here."""
		self.an_employee(i9="Expired", status="Left")
		self.assertSilent(self.RULE)

	def test_it_is_silent_where_the_compliance_field_is_not_installed(self):
		"""There is nothing to read. Reported as an empty scan rather than a
		failure — install_compliance_fields is the fix and it says so itself."""
		install_hrms()
		self.assertSilent(self.RULE)

	def test_re_verifying_auto_dismisses_it(self):
		row = self.an_employee(i9="Expired")
		self.assertFires(self.RULE)
		row["i9_status"] = "Verified"
		self.assertAutoDismissed(self.RULE)


# ── 7. flc_license_expiring ─────────────────────────────────────────────────
class FlcLicenseExpiring(AlertTestCase):
	RULE = "flc_license_expiring"

	def test_it_fires_inside_the_ninety_day_lead_time(self):
		self.an_employee(i9="Verified", flc_license_expiration=days_from_today(60))
		alert = self.assertFires(self.RULE, "Warning")
		self.assertIn("bond and background check", alert["message"])

	def test_it_is_silent_further_out(self):
		self.an_employee(i9="Verified", flc_license_expiration=days_from_today(200))
		self.assertSilent(self.RULE)

	def test_it_escalates_to_critical_inside_thirty_days(self):
		self.an_employee(i9="Verified", flc_license_expiration=days_from_today(15))
		self.assertIn("mid-harvest", self.assertFires(self.RULE, "Critical")["message"])

	def test_an_expired_licence_says_take_them_off_the_schedule(self):
		self.an_employee(i9="Verified", flc_license_expiration=days_from_today(-5))
		alert = self.assertFires(self.RULE, "Critical")
		self.assertIn("grower's violation", alert["message"])
		self.assertIn("crew schedule", alert["message"])

	def test_an_employee_with_no_licence_raises_nothing(self):
		"""Most people on a crew are not contractors."""
		self.an_employee(i9="Verified")
		self.assertSilent(self.RULE)

	def test_renewing_auto_dismisses_it(self):
		row = self.an_employee(i9="Verified", flc_license_expiration=days_from_today(15))
		self.assertFires(self.RULE)
		row["flc_license_expiration"] = days_from_today(400)
		self.assertAutoDismissed(self.RULE)


# ── 8. filing_response_due ──────────────────────────────────────────────────
class FilingResponseDue(AlertTestCase):
	RULE = "filing_response_due"

	def test_it_fires_as_the_deadline_approaches(self):
		self.a_filing(response_due_in_days=10)
		alert = self.assertFires(self.RULE, "Info")
		self.assertIn("ODA-2026-0417", alert["message"])

	def test_it_is_silent_further_out(self):
		self.a_filing(response_due_in_days=90)
		self.assertSilent(self.RULE)

	def test_it_escalates_to_warning_once_the_deadline_has_passed(self):
		"""At that point it is no longer a heads-up."""
		self.a_filing(response_due_in_days=-5)
		self.assertFires(self.RULE, "Warning")

	def test_a_draft_raises_nothing(self):
		"""Nothing was sent, so nothing is being waited for."""
		self.a_filing(response_due_in_days=10, status="Draft", submission_date=None)
		self.assertSilent(self.RULE)

	def test_a_filing_that_was_already_answered_raises_nothing(self):
		self.a_filing(response_due_in_days=10, response_received_date=days_from_today(-2))
		self.assertSilent(self.RULE)

	def test_recording_the_response_auto_dismisses_it(self):
		"""The auto-dismissal is the same clause as the silence above, which is
		what makes it reliable rather than a second implementation."""
		self.a_filing(response_due_in_days=10)
		self.assertFires(self.RULE)
		self.tool_data(
			"update_regulatory_filing",
			{
				"filing": "Pesticide Application Report 2026-Q2",
				"response_received_date": days_from_today(-1),
				"response": "Accepted with no further action",
			},
		)
		self.assertAutoDismissed(self.RULE)


# ── 9. audit_action_overdue ─────────────────────────────────────────────────
class AuditActionOverdue(AlertTestCase):
	RULE = "audit_action_overdue"

	def test_it_fires_on_an_action_past_its_own_deadline(self):
		self.an_audit(due_in_days=-25)
		alert = self.assertFires(self.RULE, "Critical")
		self.assertIn("25 day(s) past", alert["message"])
		self.assertIn("this year's penalty", alert["message"])

	def test_it_is_silent_before_the_deadline(self):
		self.an_audit(due_in_days=30)
		self.assertSilent(self.RULE)

	def test_an_action_with_no_deadline_raises_nothing(self):
		"""There is no deadline to be past."""
		self.an_audit(due_in_days=None)
		self.assertSilent(self.RULE)

	def test_a_minor_finding_left_late_is_a_warning_rather_than_critical(self):
		"""Severity follows the worst open finding: at Major the certificate is at
		risk, at Minor the paperwork is."""
		self.an_audit(
			corrective_actions=[
				{"finding": "Signage faded", "severity": "Minor", "due_date": days_from_today(-25)}
			]
		)
		self.assertFires(self.RULE, "Warning")

	def test_five_open_items_on_one_audit_are_one_alert(self):
		"""One conversation with one auditor, not five problems."""
		self.an_audit(
			corrective_actions=[
				{"finding": f"Finding {index}", "severity": "Minor", "due_date": days_from_today(-10)}
				for index in range(5)
			]
		)
		found = self.raised(self.RULE)
		self.assertEqual(len(found), 1)
		self.assertIn("5 overdue corrective action(s)", found[0]["message"])

	def test_closing_the_actions_auto_dismisses_it(self):
		self.an_audit(due_in_days=-25)
		self.assertFires(self.RULE)
		self.tool_data(
			"update_audit_event",
			{
				"audit": "PrimusGFS 2026",
				"close_corrective_action": 1,
				"corrective_action": "restocked soap and added a daily check to the pre-harvest walk",
				"closed_date": days_from_today(-3),
			},
		)
		self.assertAutoDismissed(self.RULE)

	def test_a_closed_audit_raises_nothing(self):
		self.an_audit(due_in_days=-25)
		self.tool_data(
			"update_audit_event",
			{
				"audit": "PrimusGFS 2026",
				"close_corrective_action": 1,
				"corrective_action": "restocked soap and added a daily check",
				"closed_date": days_from_today(-3),
			},
		)
		self.tool_data(
			"close_audit_event",
			{"audit": "PrimusGFS 2026", "closure_note": "auditor confirmed by email"},
		)
		self.assertSilent(self.RULE)


# ── dismissal, snoozing, and coming back ────────────────────────────────────
class ComingOffTheCalendar(AlertTestCase):
	def setUp(self):
		super().setUp()
		self.a_certificate(expires_in_days=45)
		self.sweep()
		self.alert = self.live()[0]["name"]

	def test_a_snooze_hides_it_and_it_comes_back_on_its_own(self):
		self.tool_data("snooze_alert", {"alert": self.alert, "until_date": days_from_today(7)})
		self.assertEqual(self.live(), [])
		# The snooze is a DATE, not a flag somebody has to clear.
		later = self.tool_data("get_compliance_calendar", {"as_of": days_from_today(8)})
		self.assertEqual(later["alert_count"], 1)

	def test_a_snooze_in_the_past_is_refused(self):
		message = self.tool_error("snooze_alert", {"alert": self.alert, "until_date": days_from_today(-1)})
		self.assertIn("not in the future", message)

	def test_a_dismissal_needs_a_reason_that_is_a_sentence(self):
		self.assertIn(
			"real explanation", self.tool_error("dismiss_alert", {"alert": self.alert, "reason": "no"})
		)

	def test_a_dismissal_keeps_the_row_and_records_who_and_why(self):
		"""The record that somebody looked at this and decided is itself
		compliance evidence."""
		before = len(STORE.rows("Compliance Alert"))
		self.tool_data(
			"dismiss_alert",
			{"alert": self.alert, "reason": "the buyer dropped the GlobalGAP requirement for 2027"},
		)
		self.assertEqual(len(STORE.rows("Compliance Alert")), before)
		row = STORE.get_raw("Compliance Alert", self.alert)
		self.assertTrue(row["dismissed"])
		self.assertFalse(row["auto_dismissed"])
		self.assertIn("buyer dropped", row["dismissed_reason"])

	def test_a_human_dismissal_is_never_reopened_by_the_sweep(self):
		"""Somebody looked and decided. The sweep does not overrule them by
		noticing the same thing again."""
		self.tool_data(
			"dismiss_alert",
			{"alert": self.alert, "reason": "the buyer dropped the GlobalGAP requirement for 2027"},
		)
		report = self.sweep()
		self.assertEqual(report["reopened"], 0)
		self.assertEqual(self.live(), [])

	def test_an_auto_dismissal_IS_reopened_when_the_condition_returns(self):
		"""The alert is a statement about the present, not a task somebody closed."""
		self.tool_data(
			"renew_certification",
			{
				"certification": "GlobalGAP 2026",
				"new_expiration": days_from_today(400),
				"what_was_done": "passed the 2026 re-audit and paid the scheme fee",
			},
		)
		self.assertAutoDismissed("certification_expiring")
		self.assertTrue(STORE.get_raw("Compliance Alert", self.alert)["auto_dismissed"])
		# The renewed certificate approaches its own expiry.
		STORE.get_raw("Certification", "GlobalGAP 2026")["expiration_date"] = days_from_today(20)
		report = self.sweep()
		self.assertEqual(report["reopened"], 1)
		self.assertEqual(len(self.live()), 1)

	def test_an_auto_dismissal_carries_no_human_reason(self):
		"""So the two kinds of dismissal stay distinguishable six months later."""
		self.tool_data(
			"renew_certification",
			{
				"certification": "GlobalGAP 2026",
				"new_expiration": days_from_today(400),
				"what_was_done": "passed the 2026 re-audit and paid the scheme fee",
			},
		)
		self.sweep()
		row = STORE.get_raw("Compliance Alert", self.alert)
		self.assertTrue(row["auto_dismissed"])
		self.assertFalse(row.get("dismissed_reason"))

	def test_dismissing_an_already_dismissed_alert_is_refused(self):
		payload = {"alert": self.alert, "reason": "the buyer dropped the requirement for 2027"}
		self.tool_data("dismiss_alert", payload)
		self.assertIn("already dismissed", self.tool_error("dismiss_alert", payload))


class BulkDismissal(AlertTestCase):
	def setUp(self):
		super().setUp()
		self.a_certificate("Cert A", expires_in_days=45)
		self.a_certificate("Cert B", expires_in_days=50)
		self.a_policy(review_in_days=-30)
		self.sweep()

	def test_it_refuses_with_no_filter_at_all(self):
		"""'Dismiss the entire compliance calendar' is never what anybody means."""
		message = self.tool_error("dismiss_alert_bulk", {"reason": "handled in the June walk-through"})
		self.assertIn("entire compliance calendar", message)

	def test_the_first_call_writes_nothing_even_without_dry_run(self):
		"""DRY RUN DEFAULTS TRUE. The whole calendar is one filter away."""
		data = self.tool_data(
			"dismiss_alert_bulk",
			{"reason": "handled in the June walk-through", "alert_type": "certification_expiring"},
		)
		self.assertTrue(data["dry_run"])
		self.assertEqual(data["matched"], 2)
		self.assertEqual(data["dismissed"], 0)
		self.assertEqual(len(self.live()), 3)

	def test_a_second_call_with_the_same_filter_does_the_work(self):
		payload = {
			"reason": "handled in the June walk-through with the certifier",
			"alert_type": "certification_expiring",
			"dry_run": False,
		}
		data = self.tool_data("dismiss_alert_bulk", payload)
		self.assertEqual(data["dismissed"], 2)
		self.assertEqual([alert["alert_type"] for alert in self.live()], ["policy_review_overdue"])

	def test_the_reason_lands_on_every_alert_it_touched(self):
		self.tool_data(
			"dismiss_alert_bulk",
			{
				"reason": "handled in the June walk-through with the certifier",
				"alert_type": "certification_expiring",
				"dry_run": False,
			},
		)
		reasons = {
			row.get("dismissed_reason")
			for row in STORE.rows("Compliance Alert")
			if frappe.utils.cint(row.get("dismissed"))
		}
		self.assertEqual(reasons, {"handled in the June walk-through with the certifier"})

	def test_it_says_out_loud_that_nothing_underneath_changed(self):
		data = self.tool_data(
			"dismiss_alert_bulk",
			{
				"reason": "handled in the June walk-through with the certifier",
				"alert_type": "certification_expiring",
				"dry_run": False,
			},
		)
		self.assertIn("NONE of the underlying conditions changed", data["note"])

	def test_an_unknown_alert_type_is_refused_with_the_list(self):
		message = self.tool_error(
			"dismiss_alert_bulk", {"reason": "handled in the walk-through", "alert_type": "not_a_rule"}
		)
		self.assertIn("no compliance rule", message)
		self.assertIn("certification_expiring", message)

	def test_a_dry_run_calls_out_criticals_by_count(self):
		self.a_certificate("Cert C", expires_in_days=5)
		self.sweep()
		data = self.tool_data(
			"dismiss_alert_bulk",
			{"reason": "handled in the June walk-through", "severity": "Critical"},
		)
		self.assertIn("stopped being lawful", data["note"])


# ── the calendar read ───────────────────────────────────────────────────────
class TheCalendar(AlertTestCase):
	def setUp(self):
		super().setUp()
		self.a_certificate("Critical Cert", expires_in_days=5)
		self.a_policy(review_in_days=-30)
		self.a_filing(response_due_in_days=10)
		self.sweep()

	def test_it_sorts_critical_first(self):
		data = self.tool_data("get_compliance_calendar", {})
		first_category = next(iter(data["by_category"].values()))
		self.assertEqual(first_category["worst"], "Critical")

	def test_severity_min_filters_to_that_severity_and_worse(self):
		data = self.tool_data("get_compliance_calendar", {"severity_min": "Warning"})
		self.assertEqual(
			sorted(
				{alert["severity"] for group in data["by_category"].values() for alert in group["alerts"]}
			),
			["Critical", "Warning"],
		)

	def test_days_ahead_never_hides_something_already_overdue(self):
		"""It was due in the PAST. A forward-looking window has nothing to say
		about it."""
		data = self.tool_data("get_compliance_calendar", {"days_ahead": 1})
		names = [alert["alert_type"] for group in data["by_category"].values() for alert in group["alerts"]]
		self.assertIn("policy_review_overdue", names)

	def test_snoozed_alerts_are_hidden_and_counted(self):
		name = self.live()[0]["name"]
		self.tool_data("snooze_alert", {"alert": name, "until_date": days_from_today(30)})
		data = self.tool_data("get_compliance_calendar", {})
		self.assertEqual(data["hidden_snoozed"], 1)
		self.assertIn("snoozed alert(s) are hidden", data["note"])

	def test_it_reports_which_rules_cannot_run_on_this_site(self):
		"""An empty category is not the same as a clean one."""
		data = self.tool_data("get_compliance_calendar", {})
		self.assertIsInstance(data["rules_unavailable_here"], list)

	def test_it_is_scoped_by_company(self):
		self.a_certificate("Other Cert", company=OTHER, expires_in_days=5)
		self.sweep()
		data = self.tool_data("get_compliance_calendar", {"company": OTHER})
		names = [
			alert["source_docname"] for group in data["by_category"].values() for alert in group["alerts"]
		]
		self.assertEqual(names, ["Other Cert"])

	def test_an_alerts_age_is_reported_from_when_it_was_first_seen(self):
		name = self.live()[0]["name"]
		STORE.get_raw("Compliance Alert", name)["first_seen"] = days_from_today(-120)
		alert = next(entry for entry in self.live() if entry["name"] == name)
		self.assertEqual(alert["open_for_days"], 120)


class TheReadinessScore(AlertTestCase):
	def setUp(self):
		super().setUp()
		self.a_certificate("Cert A", expires_in_days=45)
		self.a_certificate("Cert B", expires_in_days=50)
		self.sweep()

	def test_an_untouched_site_scores_zero_with_two_alerts_open(self):
		data = self.tool_data("get_audit_readiness", {})
		self.assertEqual(data["raised"], 2)
		self.assertEqual(data["open"], 2)
		self.assertEqual(data["audit_readiness_score"], 0.0)

	def test_resolving_one_moves_the_score(self):
		self.tool_data(
			"renew_certification",
			{
				"certification": "Cert A",
				"new_expiration": days_from_today(400),
				"what_was_done": "passed the 2026 re-audit and paid the scheme fee",
			},
		)
		self.sweep()
		self.assertEqual(self.tool_data("get_audit_readiness", {})["audit_readiness_score"], 50.0)

	def test_it_reports_how_the_score_was_earned(self):
		"""An operation at 100% through dismissals is a different operation from
		one at 100% because the work got done."""
		for name in ("Cert A", "Cert B"):
			alert = next(
				row["name"] for row in STORE.rows("Compliance Alert") if row["source_docname"] == name
			)
			self.tool_data(
				"dismiss_alert",
				{"alert": alert, "reason": "the buyer dropped the GlobalGAP requirement for 2027"},
			)
		data = self.tool_data("get_audit_readiness", {})
		self.assertEqual(data["audit_readiness_score"], 100.0)
		self.assertEqual(data["resolved_by_hand_percent"], 100.0)
		self.assertTrue(any("by hand" in warning for warning in data["warnings"]))

	def test_an_open_critical_is_called_out_whatever_the_percentage(self):
		self.a_certificate("Cert C", expires_in_days=5)
		self.sweep()
		data = self.tool_data("get_audit_readiness", {})
		self.assertTrue(any("stopped being lawful" in warning for warning in data["warnings"]))

	def test_a_site_with_no_alerts_scores_a_hundred_and_says_why(self):
		STORE.tables["Compliance Alert"] = {}
		data = self.tool_data("get_audit_readiness", {})
		self.assertEqual(data["audit_readiness_score"], 100.0)
		self.assertEqual(data["raised"], 0)
		self.assertIn("not the same as being audit-ready", data["note"])

	def test_a_snoozed_alert_still_counts_as_open(self):
		"""The score does not flatter a snooze."""
		name = self.live()[0]["name"]
		self.tool_data("snooze_alert", {"alert": name, "until_date": days_from_today(30)})
		data = self.tool_data("get_audit_readiness", {})
		self.assertEqual(data["open"], 2)
		self.assertEqual(data["snoozed"], 1)


class RulesThatCannotRunHere(AlertTestCase):
	def test_an_absent_doctype_dismisses_nothing(self):
		"""THE ONE SAFE READING. Uninstalling farm_precision_ag this afternoon is
		not evidence that anybody performed a water test."""
		from .harness import INSTALLED_DOCTYPES

		self.a_block(sprayed_days_ago=11, tested_days_ago=118)
		self.assertFires("water_test_stale")
		INSTALLED_DOCTYPES.discard("Field")
		try:
			report = self.sweep()
			self.assertEqual(report["auto_dismissed"], 0)
			skipped = {entry["alert_type"] for entry in report["rules_skipped"]}
			self.assertIn("water_test_stale", skipped)
		finally:
			INSTALLED_DOCTYPES.add("Field")

	def test_a_rule_that_throws_does_not_stop_the_others(self):
		"""The sweep runs on somebody's scheduler beside their real work."""
		self.a_certificate(expires_in_days=45)
		rule = alerts.RULES["policy_review_overdue"]
		broken = alerts.Rule(
			key=rule.key,
			title=rule.title,
			category=rule.category,
			scan=lambda context: (_ for _ in ()).throw(RuntimeError("boom")),
			kairotic_gate=rule.kairotic_gate,
			requires=rule.requires,
		)
		alerts.RULES[rule.key] = broken
		try:
			report = self.sweep()
			self.assertEqual([entry["alert_type"] for entry in report["rules_failed"]], [rule.key])
			self.assertEqual(report["created"], 1)
		finally:
			alerts.RULES[rule.key] = rule
