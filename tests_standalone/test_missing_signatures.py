# SPDX-License-Identifier: MIT
"""The four boxes nobody signed, and the loop that closes them. v0.55.0.

FOUR CLAIMS.

1. `TheRulesFindTheEmptyBoxes` — each of the four rules fires on a form whose
   signature column is empty and on nothing else. The negatives matter more than
   the positives here: a rule that fires on a Draft I-9, on a superseded W-4, or
   on a form that IS signed is a rule that fills a calendar with work nobody
   owes.

2. `TheAlertsBecomeWork` — a missing-signature alert becomes a Farm Task with
   the right TYPE, the right HOLDER and a link back to the form. The holder is
   the whole point of the release: Section 1 and the W-4 go to whoever the
   employee reports to, because the errand is FINDING the employee; Section 2
   and Supplement B go to an authorized signer, because the errand is an
   attestation only they may make.

3. `TheSignatureIsCollected` — `collect_form_signature` writes the capture onto
   the box, closes the task that asked for it, and the next sweep dismisses the
   alert with nobody having touched it. That last step is the test that proves
   this is a loop rather than three features.

4. `TheRefusals` — a field outside the closed list, a scan-sized body, something
   that is not an image, a second signature without `overwrite`, and a caller
   who is not on the roster for the two boxes that need one.
"""

import base64

import frappe

from erpnext_mcp import alerts, compliance_rules
from erpnext_mcp.tools import signatures

from .fixtures import APPROVER, MAIN, OTHER, V12TestCase, install_hrms
from .harness import STORE

TODAY = "2026-07-24"

#: The smallest thing that is genuinely a PNG: an 8-byte signature and nothing
#: after it. `_sniff` reads the first eight bytes and this file never asks any
#: renderer to open the result, so a real image would be bytes spent proving
#: something no test here asserts.
A_CAPTURE = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"signature").decode()

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"refresh_compliance_alerts",
		"get_compliance_calendar",
		"generate_tasks_from_compliance_alerts",
		"list_dispatch_board",
		"get_farm_task",
		"collect_form_signature",
		"get_i9_form",
		"get_w4",
	)
}


class SignatureTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		install_hrms()
		STORE.singles.setdefault(
			"I-9 Settings",
			{"doctype": "I-9 Settings", "business_legal_name": "Test Farm LLC"},
		)
		compliance_rules.seed_compliance_rules()
		# Ben reports to Ada. Every Section 1 and W-4 task in this file should
		# land on Ada, and the assertion is only meaningful because the link is
		# set here rather than assumed.
		frappe.db.set_value("Employee", "HR-EMP-00002", "reports_to", "HR-EMP-00001")

	# ── the records the rules read ──────────────────────────────────────
	def an_i9(self, name="I9-2026-0001", **overrides):
		payload = {
			"doctype": "I-9 Form",
			"name": name,
			"employee": "HR-EMP-00002",
			"employee_name": "Ben Packhouse",
			"company": MAIN,
			"status": "Section 1 Complete",
			"hire_date": "2026-07-01",
			"legal_first_name": "Ben",
			"legal_last_name": "Packhouse",
			"citizenship_status": "US Citizen",
		}
		payload.update(overrides)
		STORE.seed("I-9 Form", [payload])
		return name

	def a_w4(self, name="W4-2026-0001", **overrides):
		payload = {
			"doctype": "W-4 Form",
			"name": name,
			"employee": "HR-EMP-00002",
			"employee_name": "Ben Packhouse",
			"company": MAIN,
			"tax_year": 2026,
			"status": "Active",
			"effective_date": "2026-07-01",
			"filing_status": "Single or Married Filing Separately",
		}
		payload.update(overrides)
		STORE.seed("W-4 Form", [payload])
		return name

	def a_reverification(self, i9: str, signed: str = "", **overrides):
		"""Append one Supplement B row to a form, where a child row actually lives.

		Nested on the parent rather than seeded as a standalone table, because
		that is where Frappe keeps it and where the harness reads it back from —
		`CHILD_TABLE_SOURCES` is what makes `get_all("I-9 Reverification", ...)`
		find it, and a row seeded beside the parent would be invisible to both
		the scanner and the document.
		"""
		row = {
			"name": f"I9REV-{i9}-1",
			"doctype": "I-9 Reverification",
			"parent": i9,
			"parenttype": "I-9 Form",
			"parentfield": "reverifications",
			"idx": 1,
			"reverification_date": "2026-06-01",
			"reason": "Work Authorization Expired",
			"document_title": "Employment Authorization Document",
			"section_3_signature": signed,
		}
		row.update(overrides)
		form = STORE.get_raw("I-9 Form", i9)
		form.setdefault("reverifications", []).append(row)
		return row["name"]

	def a_roster(self, include_caller: bool = True):
		"""Ada on the I-9 signer roster, with a User account payroll knows her by.

		`include_caller` puts the ACTING account on it as well, because the two
		halves of this feature ask the roster different questions: routing asks
		"who could be sent to sign this" and the sign call asks "may THIS account
		sign". A test that only seeded Ada would prove the routing and would be
		refused at the signature for a reason that has nothing to do with what it
		is testing.
		"""
		rows = [
			{
				"doctype": "Authorized Signer",
				"name": "SIGNER-1",
				"parent": "I-9 Settings",
				"parenttype": "I-9 Settings",
				"parentfield": "authorized_signers",
				"idx": 1,
				"user": APPROVER,
				"full_name": "Ada Orchard",
				"title": "HR Manager",
				"can_sign_i9": 1,
				"can_sign_w4": 1,
				"active": 1,
			}
		]
		if include_caller and frappe.session.user != APPROVER:
			rows.append(
				{
					"doctype": "Authorized Signer",
					"name": "SIGNER-2",
					"parent": "I-9 Settings",
					"parenttype": "I-9 Settings",
					"parentfield": "authorized_signers",
					"idx": 2,
					"user": frappe.session.user,
					"full_name": "The Office",
					"title": "Administrator",
					"can_sign_i9": 1,
					"can_sign_w4": 1,
					"active": 1,
				}
			)
		settings = STORE.singles.setdefault("I-9 Settings", {"doctype": "I-9 Settings"})
		settings["authorized_signers"] = rows

	# ── running the machinery ───────────────────────────────────────────
	def sweep(self):
		return self.tool_data("refresh_compliance_alerts", {"today": TODAY})

	def alerts_of(self, alert_type: str) -> list:
		return [row for row in STORE.rows("Compliance Alert") if row.get("alert_type") == alert_type]

	def raised(self) -> set:
		return {
			row["alert_type"]
			for row in STORE.rows("Compliance Alert")
			if not frappe.utils.cint(row.get("dismissed"))
		}

	def tasks(self) -> list:
		self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		return STORE.rows("Farm Task")

	def task_for(self, alert_type: str) -> dict:
		for task in self.tasks():
			alert = frappe.db.get_value("Compliance Alert", task.get("source_alert"), "alert_type")
			if alert == alert_type:
				return task
		raise AssertionError(f"no Farm Task was raised for {alert_type}")


# ── 1. the rules ────────────────────────────────────────────────────────────
class TheRulesFindTheEmptyBoxes(SignatureTestCase):
	def test_all_four_rules_are_seeded_and_enabled(self):
		names = set(alerts.names())
		for rule in (
			"i9_section_1_unsigned",
			"i9_section_2_unsigned",
			"i9_supplement_b_unsigned",
			"w4_signature_missing",
		):
			with self.subTest(rule=rule):
				self.assertIn(rule, names)

	def test_an_unsigned_section_1_raises(self):
		self.an_i9()
		self.sweep()
		self.assertIn("i9_section_1_unsigned", self.raised())

	def test_a_signed_section_1_raises_nothing(self):
		self.an_i9(section_1_signature="/private/files/ben.png")
		self.sweep()
		self.assertNotIn("i9_section_1_unsigned", self.raised())

	def test_a_draft_raises_nothing_because_nothing_was_submitted_to_sign(self):
		self.an_i9(status="Draft")
		self.sweep()
		self.assertNotIn("i9_section_1_unsigned", self.raised())

	def test_a_destroyed_form_raises_nothing(self):
		self.an_i9(status="Destroyed")
		self.sweep()
		self.assertNotIn("i9_section_1_unsigned", self.raised())

	def test_section_2_raises_only_once_somebody_examined_the_documents(self):
		"""The gate is `verification_date`, not `status`.

		A form awaiting verification has an empty Section 2 and nothing to have
		signed — that is `i9_verification_overdue`'s question, and raising here
		as well would be two rows on one calendar for one afternoon."""
		name = self.an_i9(status="Awaiting Verification")
		self.sweep()
		self.assertNotIn("i9_section_2_unsigned", self.raised())

		frappe.db.set_value(
			"I-9 Form",
			name,
			{"verification_date": "2026-07-03", "status": "Complete", "verifier_name": "Ada Orchard"},
		)
		self.sweep()
		self.assertIn("i9_section_2_unsigned", self.raised())

	def test_an_unsigned_reverification_raises_one_alert_per_form(self):
		"""Two unsigned entries on one worker are one conversation, not two."""
		name = self.an_i9(status="Reverification Needed")
		self.a_reverification(name)
		self.a_reverification(name, name="I9REV-second", idx=2, reverification_date="2026-06-20")
		self.sweep()
		rows = [
			row
			for row in self.alerts_of("i9_supplement_b_unsigned")
			if not frappe.utils.cint(row.get("dismissed"))
		]
		self.assertEqual(len(rows), 1)
		# The message leads with the NEWEST entry and says how many there are.
		self.assertIn("2 reverification entries have", rows[0]["alert_message"])
		self.assertIn("2026-06-20", rows[0]["alert_message"])

	def test_a_signed_reverification_raises_nothing(self):
		name = self.an_i9(status="Reverification Needed")
		self.a_reverification(name, signed="/private/files/ada.png")
		self.sweep()
		self.assertNotIn("i9_supplement_b_unsigned", self.raised())

	def test_a_form_with_no_reverification_at_all_raises_nothing(self):
		"""Most I-9s never need a Supplement B. 'No rows' is not 'unsigned rows'."""
		self.an_i9(status="Complete", verification_date="2026-07-03")
		self.sweep()
		self.assertNotIn("i9_supplement_b_unsigned", self.raised())

	def test_an_unsigned_active_w4_raises_and_a_superseded_one_does_not(self):
		self.a_w4()
		self.a_w4(name="W4-2025-0009", status="Superseded", tax_year=2025)
		self.sweep()
		rows = [
			row
			for row in self.alerts_of("w4_signature_missing")
			if not frappe.utils.cint(row.get("dismissed"))
		]
		self.assertEqual([row["source_docname"] for row in rows], ["W4-2026-0001"])

	def test_a_signed_w4_raises_nothing(self):
		self.a_w4(signature="/private/files/ben-w4.png")
		self.sweep()
		self.assertNotIn("w4_signature_missing", self.raised())

	# ── v0.68.0: {{current_year}} in w4_tax_year_outdated's scope filter ─────
	def test_a_prior_tax_year_active_w4_raises_the_outdated_informational_alert(self):
		self.a_w4()  # tax_year 2026, matching TODAY's calendar year
		self.a_w4(name="W4-2025-0002", tax_year=2025)
		self.sweep()
		rows = [
			row
			for row in self.alerts_of("w4_tax_year_outdated")
			if not frappe.utils.cint(row.get("dismissed"))
		]
		self.assertEqual([row["source_docname"] for row in rows], ["W4-2025-0002"])

	def test_a_current_tax_year_w4_does_not_raise_the_outdated_alert(self):
		self.a_w4()
		self.sweep()
		self.assertNotIn("w4_tax_year_outdated", self.raised())


# ── 2. the tasks ────────────────────────────────────────────────────────────
class TheAlertsBecomeWork(SignatureTestCase):
	def test_section_1_goes_to_the_employees_supervisor(self):
		"""The employee signs Section 1, so the work is FINDING them — and the
		person who knows which block they are on today is who they report to."""
		self.an_i9()
		self.sweep()
		task = self.task_for("i9_section_1_unsigned")
		self.assertEqual(task["assigned_to"], "HR-EMP-00001")
		self.assertEqual(task["task_type"], "Hiring")
		# Named holder and pooled skill are exclusive — a task that was both
		# would show in the pool beside the person already holding it.
		self.assertFalse(task.get("skill_required"))
		self.assertEqual(task["state"], "Claimed")

	def test_the_task_is_titled_for_the_person_not_for_the_docname(self):
		self.an_i9()
		self.sweep()
		task = self.task_for("i9_section_1_unsigned")
		self.assertEqual(task["task_name"], "Collect I-9 Section 1 signature for Ben Packhouse")

	def test_the_task_links_back_to_the_form_that_needs_signing(self):
		name = self.an_i9()
		self.sweep()
		task = self.task_for("i9_section_1_unsigned")
		self.assertEqual(task["subject_doctype"], "I-9 Form")
		self.assertEqual(task["subject_docname"], name)
		# NOT `location`. An I-9 is not a place, and putting it there would make
		# the pool's location filter useless.
		self.assertFalse(task.get("location"))

	def test_section_2_goes_to_an_authorized_signer(self):
		self.a_roster()
		self.an_i9(status="Complete", verification_date="2026-07-03", verifier_name="Ada Orchard")
		self.sweep()
		task = self.task_for("i9_section_2_unsigned")
		self.assertEqual(task["assigned_to"], "HR-EMP-00001")
		self.assertEqual(task["task_name"], "Collect I-9 Section 2 signature for Ben Packhouse")

	def test_supplement_b_is_compliance_audit_rather_than_hiring(self):
		"""A reverification happens to somebody who has worked here for seasons.
		Filing it under Hiring would put a returning driver on a new-hire board."""
		self.a_roster()
		name = self.an_i9(status="Reverification Needed")
		self.a_reverification(name)
		self.sweep()
		task = self.task_for("i9_supplement_b_unsigned")
		self.assertEqual(task["task_type"], "Compliance-Audit")
		self.assertEqual(task["assigned_to"], "HR-EMP-00001")

	def test_an_empty_roster_leaves_the_task_on_the_pool_and_says_so(self):
		"""An empty roster means unrestricted, so there is no name to route to —
		and a Dispatched task with nobody on it is the worst of the three ways
		for dispatch to fail."""
		self.an_i9(status="Complete", verification_date="2026-07-03")
		self.sweep()
		report = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		entry = next(row for row in report["created"] if row["alert_type"] == "i9_section_2_unsigned")
		self.assertIsNone(entry["assigned_to"])
		self.assertEqual(entry["skill_required"], "hr_admin")
		self.assertNotEqual(entry["dispatch_mode"], "Dispatched")
		self.assertIn("roster", " ".join(entry["routing_notes"]))

	def test_a_supervisorless_employee_falls_to_the_pool_and_says_so(self):
		frappe.db.set_value("Employee", "HR-EMP-00002", "reports_to", "")
		self.an_i9()
		self.sweep()
		report = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		entry = next(row for row in report["created"] if row["alert_type"] == "i9_section_1_unsigned")
		self.assertIsNone(entry["assigned_to"])
		self.assertIn("reports_to", " ".join(entry["routing_notes"]))

	def test_generating_twice_raises_one_task(self):
		self.an_i9()
		self.sweep()
		self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		again = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.assertEqual(again["created_count"], 0)


# ── 3. the loop closing ─────────────────────────────────────────────────────
class TheSignatureIsCollected(SignatureTestCase):
	def test_the_capture_lands_on_the_box_with_its_timestamp(self):
		name = self.an_i9()
		data = self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		row = frappe.db.get_value(
			"I-9 Form", name, ["section_1_signature", "section_1_signed_at"], as_dict=True
		)
		self.assertTrue(row["section_1_signature"])
		self.assertEqual(data["signature"], row["section_1_signature"])
		# The image alone is not an electronic signature — 274a.2(h) asks when.
		self.assertTrue(row["section_1_signed_at"])

	def test_the_file_is_private(self):
		name = self.an_i9()
		data = self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"signature_base64": A_CAPTURE,
				"field": "section_1_signature",
			},
		)
		self.assertTrue(frappe.db.get_value("File", data["file_docname"], "is_private"))

	def test_the_w4_field_is_inferred_because_there_is_only_one(self):
		name = self.a_w4()
		self.tool_data(
			"collect_form_signature",
			{"doctype": "W-4 Form", "name": name, "signature_base64": A_CAPTURE,
			 # v0.308.0: an e-signed W-4 needs an authenticated signer (26 CFR 31.3402(f)(5)-1(c)).
			 "verification_method": "Employee ID"},
		)
		self.assertTrue(frappe.db.get_value("W-4 Form", name, "signature"))

	def test_supplement_b_defaults_to_the_newest_unsigned_row(self):
		self.a_roster()
		name = self.an_i9(status="Reverification Needed")
		self.a_reverification(name, signed="/private/files/old.png")
		newest = self.a_reverification(name, name="I9REV-second", idx=2, reverification_date="2026-06-20")
		data = self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_3_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertEqual(data["row"], newest)
		self.assertTrue(frappe.db.get_value("I-9 Reverification", newest, "section_3_signature"))

	def test_the_task_that_asked_for_it_is_completed(self):
		name = self.an_i9()
		self.sweep()
		task = self.task_for("i9_section_1_unsigned")
		data = self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertEqual(data["task"]["task"], task["name"])
		self.assertTrue(data["task"]["completed"])
		self.assertEqual(frappe.db.get_value("Farm Task", task["name"], "state"), "Completed")

	def test_it_closes_the_task_for_ITS_box_and_not_the_other_one(self):
		"""An I-9 missing both signatures has two tasks held by two different
		people. Closing the wrong one would tell a supervisor the employee had
		signed because an authorized signer had."""
		self.a_roster()
		name = self.an_i9(status="Complete", verification_date="2026-07-03", verifier_name="Ada Orchard")
		self.sweep()
		section_1 = self.task_for("i9_section_1_unsigned")
		section_2 = self.task_for("i9_section_2_unsigned")
		self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_2_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertEqual(frappe.db.get_value("Farm Task", section_2["name"], "state"), "Completed")
		self.assertNotEqual(frappe.db.get_value("Farm Task", section_1["name"], "state"), "Completed")

	def test_the_alert_auto_dismisses_on_the_next_sweep(self):
		"""The test that makes this a loop rather than three features."""
		name = self.an_i9()
		self.sweep()
		self.assertIn("i9_section_1_unsigned", self.raised())
		self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.sweep()
		self.assertNotIn("i9_section_1_unsigned", self.raised())

	def test_a_data_prefix_is_stripped_rather_than_refused(self):
		name = self.an_i9()
		self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": f"data:image/png;base64,{A_CAPTURE}",
			},
		)
		self.assertTrue(frappe.db.get_value("I-9 Form", name, "section_1_signature"))

	def test_no_pdf_is_produced_for_a_form_that_never_had_one(self):
		"""Regeneration, and only regeneration. A form that was never rendered
		gets nothing — producing a federal form nobody asked for, as a side
		effect of collecting a signature, is not this call's decision to make."""
		name = self.an_i9()
		data = self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertFalse(data["pdf"]["regenerated"])
		self.assertIn("nothing to bring up to date", data["pdf"]["note"])


# ── 4. the refusals ─────────────────────────────────────────────────────────
class TheRefusals(SignatureTestCase):
	def test_a_field_outside_the_closed_list_is_refused_with_the_list(self):
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{"doctype": "I-9 Form", "name": name, "field": "generated_pdf", "signature_base64": A_CAPTURE},
		)
		self.assertIn("not a signature box", error)
		self.assertIn("section_2_signature", error)
		self.assertFalse(frappe.db.get_value("I-9 Form", name, "generated_pdf"))

	def test_something_that_is_not_an_image_is_refused_on_its_bytes(self):
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": base64.b64encode(b"<svg onload=alert(1)>").decode(),
			},
		)
		self.assertIn("neither a PNG nor a JPEG", error)

	def test_a_scan_sized_body_is_sent_to_the_other_door(self):
		name = self.an_i9()
		oversized = base64.b64encode(
			b"\x89PNG\r\n\x1a\n" + b"x" * (signatures.SIGNATURE_MAX_BYTES + 1)
		).decode()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": oversized,
			},
		)
		self.assertIn("attach_signed_i9", error)

	def test_a_second_signature_is_refused_without_overwrite(self):
		name = self.an_i9()
		payload = {
			"doctype": "I-9 Form",
			"name": name,
			"field": "section_1_signature",
			"signature_base64": A_CAPTURE,
		}
		first = self.tool_data("collect_form_signature", payload)
		error = self.tool_error("collect_form_signature", payload)
		self.assertIn("already carries a signature", error)
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "section_1_signature"), first["signature"])
		replaced = self.tool_data("collect_form_signature", {**payload, "overwrite": True})
		self.assertEqual(replaced["replaced"], first["signature"])

	def test_a_destroyed_i9_is_refused(self):
		name = self.an_i9(status="Destroyed")
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertIn("destroyed", error.lower())

	def test_the_employer_boxes_want_the_roster_and_the_employee_boxes_do_not(self):
		"""Section 1 is signed by the WORKER, who is on nobody's roster and must
		not need to be — requiring one would mean the only accounts that could
		collect a worker's signature are the ones authorised to sign FOR the
		employer, which is the conflation §274a keeps apart."""
		self.a_roster(include_caller=False)
		name = self.an_i9(status="Complete", verification_date="2026-07-03")

		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_2_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertIn("authorized signer", error)

		self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertTrue(frappe.db.get_value("I-9 Form", name, "section_1_signature"))

	def scope_caller_to(self, company: str) -> None:
		"""One User Permission row on Company for the acting account.

		The same row `create_mobile_user` writes, and the same one an operator
		writes in the Desk — `apply_to_all_doctypes` is what makes it reach a
		doctype nobody thought about when the row was created, which on this site
		includes the federal hiring forms.
		"""
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": frappe.session.user,
				"allow": "Company",
				"for_value": company,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)

	def test_a_form_belonging_to_another_entity_is_refused(self):
		"""v0.59.3. WHICH I-9, NOT JUST WHICH ROLE.

		A Farm Manager now holds `write` on I-9 Form, which is what lets them
		complete Section 2 at all. What decides WHOSE Section 2 is the User
		Permission on Company — and it has to hold on the signature path, because
		the form the alert points at is named by the caller. A manager scoped to
		one entity signing another entity's I-9 would be attesting for an employer
		they do not work for.
		"""
		mine = self.an_i9(name="I9-2026-MINE", company=MAIN)
		theirs = self.an_i9(name="I9-2026-THEIRS", company=OTHER)
		self.scope_caller_to(MAIN)

		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": theirs,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertIn(OTHER, error)
		self.assertIn("not scoped to", error)
		self.assertFalse(frappe.db.get_value("I-9 Form", theirs, "section_1_signature"))

		# And the entity they DO manage is untouched by the refusal.
		self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": mine,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertTrue(frappe.db.get_value("I-9 Form", mine, "section_1_signature"))

	def test_an_account_with_no_user_permission_is_unrestricted(self):
		"""FRAPPE'S RULE, KEPT ON PURPOSE. An operator's own login and the MCP
		System User hold no Company User Permission, and every Desk surface on the
		site treats that as "every company". The strict reading lives on the
		mobile door, where `guard.require_scope` refuses a phone with no entities
		outright — that one faces the open internet and can afford to fail
		closed."""
		self.assertEqual([], [row for row in STORE.rows("User Permission") if row.get("allow") == "Company"])
		theirs = self.an_i9(name="I9-2026-THEIRS", company=OTHER)
		self.tool_data(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": theirs,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertTrue(frappe.db.get_value("I-9 Form", theirs, "section_1_signature"))

	def test_the_tool_ships_off(self):
		self.configure(enabled=1, allow_collect_form_signature=0)
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertTrue(error)
