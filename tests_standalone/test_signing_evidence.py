# SPDX-License-Identifier: MIT
"""The evidence packet behind a signature. v0.60.0.

SIX CLAIMS.

1. `TheRowIsWritten` — collecting a signature produces exactly one Signing
   Evidence row, and that row says what happened: which box, which capacity,
   which company, which account, the same timestamp the form carries.

2. `TheIdentityStep` — a badge scanned at the pad is resolved on the server and
   RECORDED; a badge that resolves to somebody other than the worker whose form
   is open is REFUSED, with nothing written anywhere. This is the half that
   makes the register worth having: verification that fails open is not
   verification.

3. `TheCapacityComesOffTheBox` — the role on the evidence row is derived from the
   signature box, and a caller claiming a capacity the box contradicts is
   refused. Section 1 and Section 2 of one form are two different legal acts.

4. `TheDocumentHash` — the fingerprint is taken BEFORE the signature is written,
   it survives the signature and the PDF being written, and it breaks when the
   substance of the form is altered afterwards.

5. `TheRegisterIsAppendOnly` — the row cannot be edited, and a replaced
   signature appends a new row naming the old one rather than revising it.

6. `TheReads` — `list_signing_evidence` filters the way an auditor asks, and
   `get_signing_evidence` re-checks the hash on every read.

Plus `TheRolesAndTheSwitches`, which asserts the two things about this register
that are decisions rather than mechanisms: nobody in this app may write it, and
neither tool is a write.
"""

import base64
import json
import pathlib

import frappe

from erpnext_mcp import compliance_rules, roles
from erpnext_mcp.tools import signatures, signing_evidence

from .fixtures import APPROVER, MAIN, OTHER, V12TestCase, install_hrms
from .harness import STORE

EVIDENCE = "Signing Evidence"
BADGE_DOCTYPE = "Bucket Log Badge Map"

#: The smallest thing that is genuinely a PNG — `_sniff` reads eight bytes and
#: nothing here asks a renderer to open the result.
A_CAPTURE = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"signature").decode()

A_DEVICE = "9E1C4A70-0B2F-4C1E-9A55-1D7E0F3B2C48"

#: The doctype as it SHIPS, found relative to this file so the test does not
#: depend on which directory `unittest discover` was started from.
DOCTYPE_JSON = (
	pathlib.Path(__file__).resolve().parent.parent
	/ "erpnext_mcp"
	/ "erpnext_mcp"
	/ "doctype"
	/ "signing_evidence"
	/ "signing_evidence.json"
)


def _shipped_doctype() -> dict:
	return json.loads(DOCTYPE_JSON.read_text())


ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"collect_form_signature",
		"list_signing_evidence",
		"get_signing_evidence",
		"resolve_badge",
		"get_i9_form",
		"get_w4",
	)
}


class EvidenceTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		install_hrms()
		STORE.singles.setdefault(
			"I-9 Settings",
			{"doctype": "I-9 Settings", "business_legal_name": "Test Farm LLC"},
		)
		compliance_rules.seed_compliance_rules()

	# ── the records ─────────────────────────────────────────────────────
	#
	# INSERTED THROUGH THE DOCUMENT RATHER THAN SEEDED INTO THE STORE, which is
	# not a stylistic preference here: the I-9 Form controller computes
	# `retention_until` and `destruction_eligible_date` on validate, and a row
	# written straight into the table has neither until something saves it. The
	# hash is taken of the record AS PRESENTED, so a fixture that acquired three
	# new columns during the signature's own save would read as a tampered
	# document — and would be testing an artefact of the fixture rather than the
	# check. Every I-9 on a real site has been through validate before anybody
	# opens a signature pad at it.
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
		doc = frappe.get_doc(payload)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name

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
		doc = frappe.get_doc(payload)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name

	def a_badge(self, badge_id: str, employee: str = "HR-EMP-00002", company: str = MAIN):
		STORE.seed(
			BADGE_DOCTYPE,
			[
				{
					"doctype": BADGE_DOCTYPE,
					"name": badge_id,
					"badge_id": badge_id,
					"employee": employee,
					"company": company,
					"active": 1,
				}
			],
		)
		return badge_id

	def a_roster(self):
		"""The acting account authorised to sign an I-9 for this employer."""
		settings = STORE.singles.setdefault("I-9 Settings", {"doctype": "I-9 Settings"})
		settings["authorized_signers"] = [
			{
				"doctype": "Authorized Signer",
				"name": "SIGNER-1",
				"parent": "I-9 Settings",
				"parenttype": "I-9 Settings",
				"parentfield": "authorized_signers",
				"idx": 1,
				"user": frappe.session.user,
				"full_name": "Ada Orchard",
				"title": "HR Manager",
				"can_sign_i9": 1,
				"can_sign_w4": 1,
				"active": 1,
			}
		]

	# ── running it ──────────────────────────────────────────────────────
	def sign(self, **args):
		payload = {"signature_base64": A_CAPTURE}
		payload.update(args)
		return self.tool_data("collect_form_signature", payload)

	def sign_section_1(self, name=None, **args):
		name = name or self.an_i9()
		payload = {"doctype": "I-9 Form", "name": name, "field": "section_1_signature"}
		payload.update(args)
		return name, self.sign(**payload)

	def rows(self) -> list:
		return STORE.rows(EVIDENCE)

	def only_row(self) -> dict:
		found = self.rows()
		self.assertEqual(len(found), 1, f"expected one evidence row, got {len(found)}")
		return found[0]


# ── 1. the row ──────────────────────────────────────────────────────────────
class TheRowIsWritten(EvidenceTestCase):
	def test_collecting_a_signature_writes_exactly_one_row(self):
		self.sign_section_1()
		self.assertEqual(len(self.rows()), 1)

	def test_the_row_names_the_box_that_was_signed(self):
		"""A Form I-9 carries three signatures. WITHOUT THE FIELD two rows for one
		form are indistinguishable, which is precisely the question an inspection
		asks."""
		name, _ = self.sign_section_1()
		row = self.only_row()
		self.assertEqual(row["document_type"], "I-9 Form")
		self.assertEqual(row["document_name"], name)
		self.assertEqual(row["signature_field"], "section_1_signature")

	def test_the_row_carries_the_same_moment_the_form_does(self):
		"""Two timestamps a millisecond apart on one signature is the kind of
		discrepancy nobody can explain afterwards."""
		name, _ = self.sign_section_1()
		self.assertEqual(
			str(self.only_row()["signed_at"]),
			str(frappe.db.get_value("I-9 Form", name, "section_1_signed_at")),
		)

	def test_the_row_carries_the_company_so_frappe_can_scope_it(self):
		self.sign_section_1()
		self.assertEqual(self.only_row()["company"], MAIN)

	def test_the_row_names_the_account_that_made_the_call(self):
		self.sign_section_1()
		self.assertEqual(self.only_row()["signer_user"], frappe.session.user)

	def test_the_signature_image_is_the_same_file_the_form_points_at(self):
		"""One image, two records naming it — so neither can drift from the
		other."""
		name, data = self.sign_section_1()
		self.assertEqual(self.only_row()["signature_image"], data["signature"])
		self.assertEqual(data["signature"], frappe.db.get_value("I-9 Form", name, "section_1_signature"))

	def test_the_result_reports_the_row_it_wrote(self):
		_, data = self.sign_section_1()
		self.assertTrue(data["evidence"]["recorded"])
		self.assertEqual(data["evidence"]["evidence"], self.only_row()["name"])

	def test_a_w4_signature_produces_one_too(self):
		"""The register is not the I-9's. Every form that carries a signature
		carries an evidence row for it, which is the whole reason it is a doctype
		rather than six columns."""
		name = self.a_w4()
		self.sign(doctype="W-4 Form", name=name, verification_method="Employee ID")
		row = self.only_row()
		self.assertEqual(row["document_type"], "W-4 Form")
		self.assertEqual(row["document_name"], name)


# ── 2. identity ─────────────────────────────────────────────────────────────
class TheIdentityStep(EvidenceTestCase):
	def test_a_scanned_badge_is_resolved_and_recorded(self):
		self.a_badge("ETC-0001")
		self.sign_section_1(signer_badge="ETC-0001")
		row = self.only_row()
		self.assertEqual(row["signer_badge"], "ETC-0001")
		self.assertEqual(row["verification_method"], "Badge QR")
		self.assertEqual(row["signer"], "HR-EMP-00002")
		self.assertEqual(row["status"], "Recorded")

	def test_a_badge_naming_somebody_else_is_refused_and_writes_nothing(self):
		"""THE CLAIM THE WHOLE RELEASE RESTS ON. Either the wrong person is at the
		pad or the wrong form is open, and a signature filed across that gap would
		attest under one person's penalty of perjury to another person's
		document."""
		self.a_badge("ETC-0002", employee="HR-EMP-00001")
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
				"signer_badge": "ETC-0002",
			},
		)
		self.assertIn("HR-EMP-00001", error)
		self.assertIn("Nothing was changed", error)
		self.assertEqual(self.rows(), [])
		self.assertFalse(frappe.db.get_value("I-9 Form", name, "section_1_signature"))

	def test_an_unknown_badge_is_refused_by_resolve_badge_itself(self):
		"""Delegated rather than re-implemented — `resolve_badge` already has four
		sentences for four situations, and a second reading of the register here
		would be a fifth that means something slightly different."""
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
				"signer_badge": "ETC-9999",
			},
		)
		self.assertIn("no badge", error)
		self.assertEqual(self.rows(), [])

	def test_a_badge_from_another_entity_does_not_resolve_here_either(self):
		self.a_badge("HO-0001", company=OTHER)
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
				"signer_badge": "HO-0001",
			},
		)
		self.assertIn("no badge", error)

	def test_no_badge_at_all_is_not_an_error_and_is_recorded_as_unverified(self):
		"""An operator signing at a desk has no card to scan. Refusing them would
		be this app inventing a requirement no form makes — and a row that quietly
		claimed a check that never happened would be worse than an honest one."""
		self.sign_section_1()
		row = self.only_row()
		self.assertEqual(row["verification_method"], "")
		self.assertEqual(row["status"], "Unverified")

	def test_the_unverified_row_says_which_half_is_missing(self):
		_, data = self.sign_section_1()
		self.assertIn("no identity check", data["evidence"]["note"])

	def test_a_method_claiming_a_scan_with_no_badge_is_refused(self):
		"""An identity check the server cannot repeat is not one it may record:
		the column would look like proof and hold nothing."""
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
				"verification_method": "Badge QR",
			},
		)
		self.assertIn("verification_method says a badge was scanned", error)
		self.assertEqual(self.rows(), [])

	def test_a_verification_method_outside_the_three_is_refused(self):
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
				"verification_method": "he looked like himself",
			},
		)
		self.assertIn("verification_method must be one of", error)

	def test_the_employer_box_does_not_require_the_badge_to_be_the_employees(self):
		"""Section 2 is the EMPLOYER's representative, whose badge is legitimately
		not the worker's. There the roster is the gate and the scan identifies the
		verifier."""
		self.a_roster()
		self.a_badge("ETC-0003", employee="HR-EMP-00001")
		name = self.an_i9(status="Complete", verification_date="2026-07-03", verifier_name="Ada Orchard")
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature", signer_badge="ETC-0003")
		row = self.only_row()
		self.assertEqual(row["signature_role"], "Employer Representative")
		self.assertEqual(row["signer"], "HR-EMP-00001")

	def test_the_context_the_handset_reports_is_recorded(self):
		self.sign_section_1(gps_latitude=45.5231, gps_longitude=-122.6765, device_id=A_DEVICE)
		row = self.only_row()
		self.assertEqual(row["device_id"], A_DEVICE)
		self.assertAlmostEqual(float(row["gps_latitude"]), 45.5231, places=4)
		self.assertAlmostEqual(float(row["gps_longitude"]), -122.6765, places=4)

	def test_half_a_fix_is_recorded_as_none_rather_than_as_a_place(self):
		"""A latitude with no longitude is a point on a line."""
		self.sign_section_1(gps_latitude=45.5231)
		row = self.only_row()
		self.assertFalse(row.get("gps_latitude"))
		self.assertFalse(row.get("gps_longitude"))


# ── 3. capacity ─────────────────────────────────────────────────────────────
class TheCapacityComesOffTheBox(EvidenceTestCase):
	def test_every_signature_box_maps_to_a_capacity_this_register_can_hold(self):
		"""THE INVARIANT A SIXTH BOX WOULD BREAK QUIETLY. `signature_role` is
		required on the evidence row, so a box whose `signer_role` is not in
		`ROLES_BY_BOX` would collect signatures perfectly and write no evidence for
		any of them — a register silently missing one form, which is the worst
		shape this failure could take. It fails here instead, at the code change
		that introduced it."""
		for box in signatures.SIGNATURE_BOXES:
			with self.subTest(box=box.key):
				mapped = signing_evidence.ROLES_BY_BOX.get(box.signer_role)
				self.assertIsNotNone(
					mapped, f"{box.key} signs as {box.signer_role!r}, which this register cannot record"
				)
				self.assertIn(mapped, signing_evidence.SIGNATURE_ROLES)

	def test_section_1_is_the_employees_own_attestation(self):
		self.sign_section_1()
		self.assertEqual(self.only_row()["signature_role"], "Employee")

	def test_section_2_is_the_employers(self):
		self.a_roster()
		name = self.an_i9(status="Complete", verification_date="2026-07-03", verifier_name="Ada Orchard")
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature")
		self.assertEqual(self.only_row()["signature_role"], "Employer Representative")

	def test_a_role_the_caller_states_correctly_is_accepted(self):
		"""The pad posts §14.1's `signer_role` back with the signature, and the
		common case must not become a refusal."""
		self.sign_section_1(signature_role="employee")
		self.assertEqual(self.only_row()["signature_role"], "Employee")

	def test_a_role_the_box_contradicts_is_refused(self):
		"""A client that could label them would be choosing which of two legal
		acts it had just performed."""
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
				"signature_role": "Employer Representative",
			},
		)
		self.assertIn("Employee", error)
		self.assertIn("Employer Representative", error)
		self.assertEqual(self.rows(), [])
		self.assertFalse(frappe.db.get_value("I-9 Form", name, "section_1_signature"))

	def test_a_role_outside_the_vocabulary_is_refused(self):
		name = self.an_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_1_signature",
				"signature_base64": A_CAPTURE,
				"signature_role": "Regional Vice President",
			},
		)
		self.assertIn("signature_role must be one of", error)


# ── 3b. the roster, and the entity ──────────────────────────────────────────
class TheEmployerRepresentativeIsChecked(EvidenceTestCase):
	def a_verified_i9(self, **overrides):
		return self.an_i9(
			status="Complete",
			verification_date="2026-07-03",
			verifier_name="Ada Orchard",
			**overrides,
		)

	def test_an_account_off_the_roster_cannot_sign_as_the_employer(self):
		settings = STORE.singles.setdefault("I-9 Settings", {"doctype": "I-9 Settings"})
		settings["authorized_signers"] = [
			{
				"doctype": "Authorized Signer",
				"name": "SIGNER-1",
				"parent": "I-9 Settings",
				"parenttype": "I-9 Settings",
				"parentfield": "authorized_signers",
				"idx": 1,
				"user": APPROVER,
				"full_name": "Ada Orchard",
				"can_sign_i9": 1,
				"can_sign_w4": 1,
				"active": 1,
			}
		]
		name = self.a_verified_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_2_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertIn("not an authorized signer", error)
		self.assertEqual(self.rows(), [])

	def test_the_roster_and_the_entity_are_asked_in_one_place(self):
		"""v0.60.0. "May this account sign" was only ever half a question. A
		signer on the roster and scoped to a DIFFERENT farm used to be refused with
		a sentence about write permission, which sends an operator to the
		permission manager when the roster is where they need to look — or the
		other way round."""
		self.a_roster()
		STORE.seed(
			"User Permission",
			[
				{
					"doctype": "User Permission",
					"name": "UP-EVIDENCE-1",
					"user": frappe.session.user,
					"allow": "Company",
					"for_value": OTHER,
					"apply_to_all_doctypes": 1,
				}
			],
		)
		name = self.a_verified_i9()
		error = self.tool_error(
			"collect_form_signature",
			{
				"doctype": "I-9 Form",
				"name": name,
				"field": "section_2_signature",
				"signature_base64": A_CAPTURE,
			},
		)
		self.assertIn(MAIN, error)
		self.assertEqual(self.rows(), [])

	def test_an_unscoped_account_is_unrestricted_as_it_is_everywhere_else(self):
		"""Frappe's rule, and `permissions.py` argues it at length: a stricter
		reading here would refuse every correctly-configured operator on a call
		they have always been able to make. The fail-closed reading lives on the
		mobile door."""
		self.a_roster()
		name = self.a_verified_i9()
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature")
		self.assertEqual(len(self.rows()), 1)


# ── 4. the hash ─────────────────────────────────────────────────────────────
class TheDocumentHash(EvidenceTestCase):
	def test_a_hash_is_recorded(self):
		self.sign_section_1()
		self.assertTrue(str(self.only_row()["document_hash"]).startswith("sha256:"))

	def test_it_still_matches_after_the_signature_was_written(self):
		"""TAKEN BEFORE THE WRITE, and excluding the columns this app writes as a
		consequence of somebody signing — otherwise the check would fire on its
		own side effects and nobody would read it."""
		self.sign_section_1()
		checked = signing_evidence.verify_fingerprint(dict(self.only_row()))
		self.assertIs(checked["matches"], True)

	def test_a_later_section_being_signed_does_not_trip_it(self):
		self.a_roster()
		name = self.an_i9()
		self.sign(doctype="I-9 Form", name=name, field="section_1_signature")
		first = dict(self.rows()[0])
		frappe.db.set_value(
			"I-9 Form",
			name,
			{"verification_date": "2026-07-03", "verifier_name": "Ada Orchard", "status": "Complete"},
		)
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature")
		self.assertIs(signing_evidence.verify_fingerprint(first)["matches"], True)

	def test_altering_the_substance_of_the_form_breaks_it(self):
		"""The point of the column. Change what the signer attested to and the
		signature stops vouching for the record as it stands."""
		name, _ = self.sign_section_1()
		row = dict(self.only_row())
		frappe.db.set_value("I-9 Form", name, "citizenship_status", "Permanent Resident")
		checked = signing_evidence.verify_fingerprint(row)
		self.assertIs(checked["matches"], False)
		self.assertIn("NO LONGER HASHES", checked["note"])

	def test_erasing_something_the_signer_was_shown_breaks_it(self):
		"""The key drops out of the recomputed set, so the hashes differ. A rule
		that only caught CHANGES would let a form be emptied unnoticed."""
		name, _ = self.sign_section_1()
		row = dict(self.only_row())
		frappe.db.set_value("I-9 Form", name, "citizenship_status", "")
		self.assertIs(signing_evidence.verify_fingerprint(row)["matches"], False)

	def test_the_row_records_which_columns_the_signature_covers(self):
		"""Stored because it cannot be re-derived: by the time anybody checks, some
		of the columns that were empty hold something. It also answers a question
		an auditor would otherwise have to take on trust."""
		self.sign_section_1()
		covered = str(self.only_row()["hashed_fields"]).split(",")
		self.assertIn("citizenship_status", covered)
		self.assertIn("legal_last_name", covered)
		# Empty at presentation, so outside what this signature vouches for.
		self.assertNotIn("verifier_name", covered)
		# Written by this app as a consequence of signing, so never in the hash.
		for column in signatures.hash_exclusions("I-9 Form"):
			with self.subTest(column=column):
				self.assertNotIn(column, covered)

	def test_the_detail_read_says_what_the_signature_covers(self):
		self.sign_section_1()
		data = self.tool_data("get_signing_evidence", {"name": self.only_row()["name"]})
		self.assertIn("citizenship_status", data["tamper_check"]["covers"])

	def test_a_row_with_no_hash_is_null_rather_than_a_mismatch(self):
		"""An evidence row that was never tamper-evident must not be reported as
		an altered document."""
		checked = signing_evidence.verify_fingerprint(
			{"document_type": "I-9 Form", "document_name": "I9-2026-0001", "document_hash": ""}
		)
		self.assertIsNone(checked["matches"])

	def test_a_document_that_is_gone_is_null_rather_than_a_mismatch(self):
		name, _ = self.sign_section_1()
		row = dict(self.only_row())
		STORE.tables["I-9 Form"].pop(name, None)
		self.assertIsNone(signing_evidence.verify_fingerprint(row)["matches"])

	def test_the_exclusions_cover_every_box_on_the_doctype_not_just_the_signed_one(self):
		excluded = signatures.hash_exclusions("I-9 Form")
		for box in signatures.SIGNATURE_BOXES:
			if box.doctype != "I-9 Form":
				continue
			with self.subTest(box=box.field):
				self.assertIn(box.field, excluded)
				self.assertIn(box.signed_at_field, excluded)


# ── 5. append-only ──────────────────────────────────────────────────────────
class TheRegisterIsAppendOnly(EvidenceTestCase):
	def test_a_row_cannot_be_edited_after_it_is_written(self):
		"""An audit row that could be edited is a weakened audit trail. An
		EVIDENCE row that could be edited is not evidence at all."""
		self.sign_section_1()
		doc = frappe.get_doc(EVIDENCE, self.only_row()["name"])
		doc.device_id = "somebody else's phone"
		with self.assertRaises(Exception) as caught:
			doc.save()
		self.assertIn("immutable", str(caught.exception).lower())

	def test_the_desk_cannot_create_one(self):
		"""`in_create` is the other half of append-only: the only thing that makes
		a row is the signature path that writes it, which is what makes the
		register's completeness mean something.

		READ OFF THE SHIPPED JSON, because that is where the claim lives — the
		harness models the columns this app queries and `in_create` is one Frappe
		reads for itself, so asserting it against the double would prove only that
		the double had been taught the answer."""
		self.assertEqual(int(_shipped_doctype().get("in_create") or 0), 1)

	def test_no_role_on_the_doctype_itself_can_amend_a_row_either(self):
		"""System Manager and HR Manager hold the doctype fully and still cannot
		revise a row — the controller refuses, and `delete` is there so an operator
		can prune under a retention policy rather than correct anything."""
		granted = {row["role"]: row for row in _shipped_doctype().get("permissions") or []}
		self.assertEqual(set(granted), {"System Manager", "HR Manager"})
		self.assertTrue(granted["System Manager"].get("delete"))
		self.assertFalse(granted["HR Manager"].get("delete"))

	def test_replacing_a_signature_appends_a_row_naming_the_one_it_replaced(self):
		name, _ = self.sign_section_1()
		first = self.only_row()["name"]
		self.sign(doctype="I-9 Form", name=name, field="section_1_signature", overwrite=True)
		found = self.rows()
		self.assertEqual(len(found), 2)
		newest = [row for row in found if row["name"] != first]
		self.assertEqual(len(newest), 1)
		self.assertEqual(newest[0]["supersedes"], first)

	def test_the_replaced_row_is_left_exactly_as_it_was(self):
		name, _ = self.sign_section_1()
		before = dict(self.rows()[0])
		self.sign(doctype="I-9 Form", name=name, field="section_1_signature", overwrite=True)
		after = next(row for row in self.rows() if row["name"] == before["name"])
		for column in ("signed_at", "signature_image", "document_hash", "status"):
			with self.subTest(column=column):
				self.assertEqual(after.get(column), before.get(column))

	def test_a_first_signature_supersedes_nothing(self):
		"""A row that claimed to supersede one it has nothing to do with would be
		a false claim in the one register whose value is that it makes none."""
		self.sign_section_1()
		self.assertFalse(self.only_row().get("supersedes"))

	def test_a_signature_on_another_box_does_not_supersede_this_one(self):
		self.a_roster()
		name = self.an_i9()
		self.sign(doctype="I-9 Form", name=name, field="section_1_signature")
		frappe.db.set_value(
			"I-9 Form",
			name,
			{"verification_date": "2026-07-03", "verifier_name": "Ada Orchard", "status": "Complete"},
		)
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature")
		for row in self.rows():
			with self.subTest(field=row["signature_field"]):
				self.assertFalse(row.get("supersedes"))


# ── 6. the reads ────────────────────────────────────────────────────────────
class TheReads(EvidenceTestCase):
	def two_signatures(self):
		self.a_badge("ETC-0001")
		self.sign_section_1(signer_badge="ETC-0001")
		w4 = self.a_w4()
		self.sign(doctype="W-4 Form", name=w4, verification_method="Employee ID")
		return w4

	def test_it_lists_every_signature_event(self):
		self.two_signatures()
		data = self.tool_data("list_signing_evidence", {})
		self.assertEqual(data["count"], 2)

	def test_it_filters_by_document_type_and_takes_the_usual_aliases(self):
		self.two_signatures()
		data = self.tool_data("list_signing_evidence", {"document_type": "i9"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["evidence"][0]["document_type"], "I-9 Form")

	def test_it_filters_by_one_document(self):
		w4 = self.two_signatures()
		data = self.tool_data("list_signing_evidence", {"document_name": w4})
		self.assertEqual(data["count"], 1)

	def test_it_filters_by_signer(self):
		"""Both forms are Ben's and both are signed by him in his own name, so
		both rows name him — which is the answer "show me everything this worker
		signed" is asking for."""
		self.two_signatures()
		self.assertEqual(self.tool_data("list_signing_evidence", {"signer": "HR-EMP-00002"})["count"], 2)
		self.assertEqual(self.tool_data("list_signing_evidence", {"signer": "HR-EMP-00001"})["count"], 0)

	def test_it_filters_by_badge(self):
		self.two_signatures()
		data = self.tool_data("list_signing_evidence", {"signer_badge": "ETC-0001"})
		self.assertEqual(data["count"], 1)

	def test_it_filters_by_capacity(self):
		self.two_signatures()
		data = self.tool_data("list_signing_evidence", {"signature_role": "Employee"})
		self.assertEqual(data["count"], 2)

	def test_a_date_range_reaches_the_end_of_its_last_day(self):
		"""`signed_at` is a Datetime, so a bare `<= '2026-07-31'` would drop every
		signature collected after midnight — which on a farm is all of them."""
		self.two_signatures()
		today = str(frappe.utils.now())[:10]
		data = self.tool_data("list_signing_evidence", {"from_date": today, "to_date": today})
		self.assertEqual(data["count"], 2)

	def test_the_unverified_count_is_reported_separately(self):
		"""A register that only gave its total would hide the rows that cannot
		answer the question it exists for."""
		self.two_signatures()
		# v0.308.0: an unverified W-4 can no longer be signed at all, so the unverified row is an older one.
		STORE.seed(EVIDENCE, [{**self.rows()[0], "name": "SE-OLD-UNVERIFIED", "status": "Unverified",
		                       "verification_method": "", "signer_badge": ""}])
		STORE.commit()
		data = self.tool_data("list_signing_evidence", {})
		self.assertEqual(data["unverified_count"], 1)
		self.assertIn("Unverified", data["note"])

	def test_it_gets_one_row_in_full_with_the_hash_rechecked(self):
		name, _ = self.sign_section_1()
		row = self.only_row()
		data = self.tool_data("get_signing_evidence", {"name": row["name"]})
		self.assertEqual(data["document_name"], name)
		self.assertIs(data["tamper_check"]["matches"], True)

	def test_the_detail_read_says_when_the_document_has_changed(self):
		name, _ = self.sign_section_1()
		row = self.only_row()
		frappe.db.set_value("I-9 Form", name, "legal_last_name", "Somebody Else")
		data = self.tool_data("get_signing_evidence", {"name": row["name"]})
		self.assertIs(data["tamper_check"]["matches"], False)

	def test_the_detail_read_names_the_row_that_replaced_it(self):
		name, _ = self.sign_section_1()
		first = self.only_row()["name"]
		self.sign(doctype="I-9 Form", name=name, field="section_1_signature", overwrite=True)
		data = self.tool_data("get_signing_evidence", {"name": first})
		self.assertTrue(data["superseded_by"])
		self.assertNotEqual(data["superseded_by"], first)

	def test_an_unknown_row_is_refused_by_name(self):
		error = self.tool_error("get_signing_evidence", {"name": "SE-9999"})
		self.assertIn("SE-9999", error)


# ── 7. the decisions ────────────────────────────────────────────────────────
class TheRolesAndTheSwitches(V12TestCase):
	def test_farm_manager_and_compliance_officer_read_it(self):
		for role in ("Farm Manager", "Compliance Officer"):
			with self.subTest(role=role):
				spec = roles.spec_for(role)
				granted = dict(spec.permissions).get(EVIDENCE)
				self.assertIsNotNone(granted, f"{role} cannot read the signature evidence register")
				self.assertTrue(granted["read"])

	def test_no_role_in_this_app_may_write_it(self):
		"""NOT CAUTION ABOUT A NEW DOCTYPE. The register's whole value is that
		nobody edits it — a grant that could not be used would still say somebody
		is expected to."""
		for spec in roles.ROLE_SPECS:
			for doctype, flags in spec.permissions:
				if doctype != EVIDENCE:
					continue
				with self.subTest(role=spec.name):
					self.assertFalse(flags["write"])
					self.assertFalse(flags["create"])
					self.assertFalse(flags["delete"])

	def test_the_phone_roles_and_the_holding_company_roles_do_not_see_it(self):
		"""Badge IDs, device identifiers and coordinates for every signature on
		the operation is a movement record for the whole crew."""
		for role in ("Field Worker", "Foreman", "Family Member", "Advisor"):
			with self.subTest(role=role):
				self.assertNotIn(EVIDENCE, dict(roles.spec_for(role).permissions))

	def test_both_tools_are_reads(self):
		"""There is NO tool that creates a row, and that is the design: one that
		could would be one that could manufacture an identity check that never
		happened."""
		from erpnext_mcp import registry

		for tool in ("list_signing_evidence", "get_signing_evidence"):
			with self.subTest(tool=tool):
				self.assertIn(tool, registry.READ_TOOLS)
				self.assertFalse(registry.TOOLS[tool]["mutating"])
		self.assertFalse(
			[name for name in registry.MUTATING_TOOLS if "signing_evidence" in name],
			"nothing may write the signature evidence register but the signature path itself",
		)
