# SPDX-License-Identifier: MIT
"""The document either side of a signature. v0.63.0.

`tools/signing_evidence.py` names five steps that turn a drawn shape into a
record which survives a challenge, and two of them had no artefact behind them
until this release. Both are about the PDF rather than the register row.

SEVEN CLAIMS, the last of them v0.64.1's.

1. `ThePreviewIsBytes` — `get_document_preview` hands the page back as base64
   under all three spellings a client might read, because the handset
   authenticates to the sidecar and cannot follow a private `file_url`. This is
   `API_CONTRACT.md` §17.5 by name: it called the presentation step a
   server-side gap and said the fix is one route.

2. `ThePreviewDrawsOnceAndDoesNotOverWrite` — a form with no page gets one
   drawn, and a form that has one is not silently redrawn. A preview that
   re-rendered on every screen open would repoint `generated_pdf` a dozen times
   a hire day, and that field is the copy somebody printed.

3. `ThePreviewSaysWhatCanBeSigned` — the boxes, which of them already carry a
   signature, and the VERBATIM attestation for each. The pad needs all three
   before it asks anybody to draw anything.

4. `TheSealIsAppended` — sealing produces a page more than the base document,
   the verification page names the signer, the badge, the moment, the device,
   the coordinates and the document fingerprint, and the hash of the finished
   file lands on the evidence row.

5. `TheSealRefusesAnUnsignedForm` — a verification page on a form nobody signed
   is an official-looking appendix that vouches for nothing, and somebody would
   file it.

6. `TheSealFollowsTheSignature` — `submit_form_signature` takes step 5
   automatically, reports it, and is not made fatal by it.

7. `TheSealedCopyReachesThePersonnelFolder` — v0.64.1. The sealed artefact is
   cross-filed on the Employee the form is about, as a second link to one file
   rather than a second copy of the bytes. A completed I-9 that could only be
   found from an I-9 Form docname was invisible to the person who opens an
   Employee and asks to see the worker's paperwork.
"""

import base64
import io
import unittest

import frappe

from erpnext_mcp import pdf_seal
from erpnext_mcp.tools import files as file_tools
from erpnext_mcp.tools import signed_documents

from .fixtures import MAIN, OTHER
from .harness import STORE
from .test_signing_evidence import A_CAPTURE, ALL_ON, EvidenceTestCase

#: The two new switches, plus the renderer the seal drives underneath.
SEAL_ON = {
	"allow_get_document_preview": 1,
	"allow_seal_signed_document": 1,
	"allow_render_i9_pdf": 1,
	"allow_render_w4_pdf": 1,
}


class SignedDocumentTestCase(EvidenceTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON, **SEAL_ON)

	def preview(self, **args):
		payload = {"document_type": "I-9 Form"}
		payload.update(args)
		return self.tool_data("get_document_preview", payload)

	def seal(self, **args):
		payload = {"document_type": "I-9 Form"}
		payload.update(args)
		return self.tool_data("seal_signed_document", payload)

	def read_pdf(self, file_url: str):
		from pypdf import PdfReader

		docname = frappe.db.get_value("File", {"file_url": file_url}, "name")
		self.assertTrue(docname, f"no File row at {file_url}")
		return PdfReader(io.BytesIO(file_tools.read_file_bytes(str(docname))))

	def signed_i9(self, **signature):
		"""An I-9 with Section 1 signed and a full evidence packet behind it."""
		name = self.an_i9()
		self.a_roster()
		self.a_badge("CF-0007")
		payload = {
			"doctype": "I-9 Form",
			"name": name,
			"field": "section_1_signature",
			"signer_badge": "CF-0007",
			"verification_method": "Badge QR",
			"device_id": "9E1C4A70-0B2F-4C1E-9A55-1D7E0F3B2C48",
			"gps_latitude": 45.5231,
			"gps_longitude": -122.6765,
		}
		payload.update(signature)
		self.sign(**payload)
		return name


# ── Claim 1 ─────────────────────────────────────────────────────────────────
@unittest.skipUnless(
	pdf_seal.available(),
	"drawing and appending the verification page needs reportlab and pypdf. Without "
	"them the seal tools go unavailable and say so, which is what the two classes "
	"left unguarded here exist to check.",
)
class ThePreviewIsBytes(SignedDocumentTestCase):
	def test_the_page_comes_back_as_base64(self):
		"""§17.5. The whole reason this route exists: a `file_url` is a login page
		to a caller holding an `X-FarmOps-Token`, so the bytes have to travel."""
		data = self.preview(document_name=self.an_i9())
		self.assertTrue(data["available"])
		self.assertEqual(data["content_type"], "application/pdf")
		self.assertEqual(data["encoding"], "base64")
		self.assertTrue(base64.b64decode(data["content"]).startswith(b"%PDF"))
		self.assertGreater(data["bytes"], 1000)

	def test_all_three_spellings_are_the_same_string(self):
		"""`content` is the contract's, `content_base64` is the file tools', and
		`base64` is what the signature answer puts the signed page under. A client
		written against any of them reads the page."""
		data = self.preview(document_name=self.an_i9())
		self.assertEqual(data["content"], data["content_base64"])
		self.assertEqual(data["content"], data["base64"])

	def test_it_finds_the_form_by_the_person_it_belongs_to(self):
		"""A handset holding an employee and no docname is the ordinary case on
		the wizard's own screens."""
		name = self.an_i9()
		data = self.preview(employee="HR-EMP-00002")
		self.assertEqual(data["document_name"], name)

	def test_a_w4_previews_through_the_same_call(self):
		"""The vocabulary is read off `signatures.FORM_HANDLERS`, so a second form
		is a row there rather than a branch here."""
		name = self.a_w4()
		data = self.preview(document_type="W-4 Form", document_name=name)
		self.assertEqual(data["document_type"], "W-4 Form")
		self.assertTrue(base64.b64decode(data["content"]).startswith(b"%PDF"))

	def test_a_form_with_no_signature_line_is_refused_by_name(self):
		"""The useful refusal: a W-2 has no signature line, which ends the
		conversation rather than starting a search for a field name."""
		error = self.tool_error("get_document_preview", {"document_type": "W-2", "document_name": "X"})
		self.assertIn("no signature line", error)

	def test_a_destroyed_i9_is_not_reconstituted(self):
		"""`destroy_i9` certifies the record was disposed of at the end of its
		retention period. Drawing a copy afterwards contradicts the certificate."""
		name = self.an_i9(status="Destroyed")
		error = self.tool_error("get_document_preview", {"document_type": "I-9 Form", "document_name": name})
		self.assertIn("destroyed", error)


# ── Claim 2 ─────────────────────────────────────────────────────────────────
@unittest.skipUnless(
	pdf_seal.available(),
	"drawing and appending the verification page needs reportlab and pypdf. Without "
	"them the seal tools go unavailable and say so, which is what the two classes "
	"left unguarded here exist to check.",
)
class ThePreviewDrawsOnceAndDoesNotOverwrite(SignedDocumentTestCase):
	def test_a_form_with_no_page_gets_one_drawn(self):
		"""Otherwise the route answers 'no page' on the exact case the pad opens
		for — a fresh I-9, which is every hire."""
		name = self.an_i9()
		self.assertFalse(frappe.db.get_value("I-9 Form", name, "generated_pdf"))
		data = self.preview(document_name=name)
		self.assertTrue(data["rendered"])
		self.assertTrue(frappe.db.get_value("I-9 Form", name, "generated_pdf"))

	def test_a_second_preview_reads_rather_than_redraws(self):
		name = self.an_i9()
		first = self.preview(document_name=name)
		second = self.preview(document_name=name)
		self.assertTrue(first["rendered"])
		self.assertFalse(second["rendered"])
		self.assertEqual(first["file_url"], second["file_url"])

	def test_refresh_redraws_on_purpose(self):
		name = self.an_i9()
		self.preview(document_name=name)
		again = self.preview(document_name=name, refresh=True)
		self.assertTrue(again["rendered"])

	def test_a_record_changed_since_the_draw_reads_as_stale(self):
		"""The fingerprint taken at signing covers the RECORD. Showing a signer a
		page drawn before the last edit means hashing something else."""
		name = self.an_i9()
		self.preview(document_name=name)
		frappe.db.set_value("I-9 Form", name, "modified", "2099-01-01 00:00:00")
		data = self.preview(document_name=name)
		self.assertTrue(data["stale"])
		self.assertIn("refresh=true", data["note"])
		self.assertFalse(data["rendered"])


# ── Claim 3 ─────────────────────────────────────────────────────────────────
class ThePreviewSaysWhatCanBeSigned(SignedDocumentTestCase):
	def test_it_lists_the_boxes_with_their_verbatim_attestations(self):
		data = self.preview(document_name=self.an_i9())
		boxes = {row["field"]: row for row in data["signature_boxes"]}
		self.assertIn("section_1_signature", boxes)
		self.assertIn("section_2_signature", boxes)
		self.assertEqual(boxes["section_1_signature"]["signer_role"], "employee")
		self.assertEqual(boxes["section_2_signature"]["signer_role"], "employer")
		# THE GOVERNMENT'S OWN SENTENCE, not a summary of it. §17.5 is explicit
		# that the attestation shown at the pad is the sentence being sworn to.
		self.assertIn("under penalty of perjury", boxes["section_1_signature"]["attestation"])

	def test_it_says_which_boxes_already_carry_a_signature(self):
		"""Otherwise a pad discovers Section 1 is taken by submitting a signature
		to it and being refused, with the worker standing there."""
		name = self.signed_i9()
		boxes = {row["field"]: row for row in self.preview(document_name=name)["signature_boxes"]}
		self.assertTrue(boxes["section_1_signature"]["signed"])
		self.assertFalse(boxes["section_2_signature"]["signed"])


# ── Claim 4 ─────────────────────────────────────────────────────────────────
@unittest.skipUnless(
	pdf_seal.available(),
	"drawing and appending the verification page needs reportlab and pypdf. Without "
	"them the seal tools go unavailable and say so, which is what the two classes "
	"left unguarded here exist to check.",
)
class TheSealIsAppended(SignedDocumentTestCase):
	def test_the_sealed_copy_has_one_page_more_than_the_form(self):
		name = self.signed_i9()
		sealed = self.seal(document_name=name)
		self.assertTrue(sealed["sealed"])
		base = self.read_pdf(sealed["base_pdf"])
		full = self.read_pdf(sealed["file_url"])
		self.assertEqual(len(full.pages), len(base.pages) + 1)

	def test_the_verification_page_names_the_whole_packet(self):
		"""Signer, badge, moment, device, coordinates, fingerprint — the four
		things collected in the field plus what the record hashed to."""
		name = self.signed_i9()
		sealed = self.seal(document_name=name)
		text = self.read_pdf(sealed["file_url"]).pages[-1].extract_text()
		row = self.only_row()
		for expected in (
			"Electronic Signature Verification Record",
			"Ben Packhouse",
			"CF-0007",
			"Badge QR",
			"9E1C4A70-0B2F-4C1E-9A55-1D7E0F3B2C48",
			"45.523100, -122.676500",
			str(row["document_hash"]),
			str(row["name"]),
		):
			self.assertIn(expected, text.replace("\n", " "), f"{expected!r} missing from the page")

	def test_the_hash_is_of_the_finished_file_and_lands_on_the_row(self):
		name = self.signed_i9()
		sealed = self.seal(document_name=name)
		content = file_tools.read_file_bytes(
			str(frappe.db.get_value("File", {"file_url": sealed["file_url"]}, "name"))
		)
		self.assertEqual(sealed["sealed_pdf_hash"], pdf_seal.sha256_of(content))
		row = self.only_row()
		self.assertEqual(row["sealed_pdf_hash"], sealed["sealed_pdf_hash"])
		self.assertEqual(row["sealed_pdf"], sealed["file_url"])
		self.assertTrue(row["sealed_at"])

	def test_a_current_page_is_reused_rather_than_drawn_twice(self):
		"""`signatures._redraw` has already redrawn the form with the new capture
		stamped in by the time `submit_form_signature` reaches the seal. Rendering
		it again produces a byte-for-byte equivalent page at real cost on a handset
		waiting in an orchard."""
		name = self.signed_i9()
		self.preview(document_name=name)
		before = frappe.db.get_value("I-9 Form", name, "generated_pdf")
		sealed = self.seal(document_name=name)
		self.assertEqual(sealed["base_pdf"], before)

	def test_a_stale_page_is_redrawn_before_it_is_sealed(self):
		"""Sealing a stale page would produce a verification record vouching for a
		document other than the one on the record — the exact failure the seal
		exists to make detectable."""
		name = self.signed_i9()
		self.preview(document_name=name)
		# A record edited after the page was drawn. `generated_pdf_on` is what says
		# whether the seal redrew, and it only moves when the renderer ran.
		frappe.db.set_value("I-9 Form", name, "modified", "2099-01-01 00:00:00")
		frappe.db.set_value("I-9 Form", name, "generated_pdf_on", "2020-01-01 00:00:00")
		self.assertTrue(self.preview(document_name=name)["stale"])

		sealed = self.seal(document_name=name)
		self.assertTrue(sealed["sealed"])
		self.assertGreater(
			str(frappe.db.get_value("I-9 Form", name, "generated_pdf_on")),
			"2020-01-01 00:00:00",
			"the seal reused a page it could see was stale",
		)

	def test_it_does_not_repoint_the_working_page(self):
		"""`generated_pdf` is the copy somebody prints; the seal is the retained
		artefact. Collapsing them would mean the next redraw threw the seal away."""
		name = self.signed_i9()
		sealed = self.seal(document_name=name)
		self.assertNotEqual(frappe.db.get_value("I-9 Form", name, "generated_pdf"), sealed["file_url"])

	def test_every_signature_on_the_form_gets_a_block(self):
		"""An appendix naming one of two signatures looks complete and is not."""
		name = self.signed_i9()
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature")
		sealed = self.seal(document_name=name)
		self.assertEqual(sealed["signatures_on_page"], 2)
		self.assertEqual(len(sealed["evidence_updated"]), 2)
		text = self.read_pdf(sealed["file_url"]).pages[-1].extract_text()
		self.assertIn("Signature 1 of 2", text)
		self.assertIn("Signature 2 of 2", text)

	def test_a_reseal_repoints_every_row_and_keeps_the_old_file(self):
		"""The three seal columns are the one thing on an evidence row that moves,
		and they move because a later signature produces a new sealed copy that
		this attestation also appears in. Nothing is deleted."""
		name = self.signed_i9()
		first = self.seal(document_name=name)
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature")
		second = self.seal(document_name=name)
		self.assertNotEqual(first["sealed_pdf_hash"], second["sealed_pdf_hash"])
		for row in self.rows():
			self.assertEqual(row["sealed_pdf_hash"], second["sealed_pdf_hash"])
		self.assertTrue(frappe.db.exists("File", {"file_url": first["file_url"]}))

	def test_a_signature_with_no_evidence_row_is_sealed_and_says_so(self):
		"""Every signature collected before v0.60.0 is in this state and cannot
		grow a row retrospectively — inventing one is the thing an evidence
		register must never do."""
		name = self.signed_i9()
		for row in self.rows():
			frappe.delete_doc("Signing Evidence", row["name"], force=True)
		sealed = self.seal(document_name=name)
		self.assertTrue(sealed["sealed"])
		self.assertEqual(sealed["signatures_on_page"], 0)
		self.assertIn("before the evidence register existed", sealed["note"])
		self.assertIn(
			"never captured",
			self.read_pdf(sealed["file_url"]).pages[-1].extract_text().replace("\n", " "),
		)


# ── Claim 7 ─────────────────────────────────────────────────────────────────
@unittest.skipUnless(
	pdf_seal.available(),
	"drawing and appending the verification page needs reportlab and pypdf. Without "
	"them the seal tools go unavailable and say so, which is what the two classes "
	"left unguarded here exist to check.",
)
class TheSealedCopyReachesThePersonnelFolder(SignedDocumentTestCase):
	"""v0.64.1. A completed I-9 that only an I-9 Form docname could find.

	The seal was attached to the FORM, which is correct and was the whole of it.
	Nobody asked to see an I-9 Form: they ask to see a worker's paperwork, open
	the Employee, and found nothing — the sealed, hashed, tamper-evident artefact
	was one join away from the place an inspection looks. Filing it in both is
	what a cross-reference is for.
	"""

	def folder(self, employee: str) -> list:
		return frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": "Employee", "attached_to_name": employee},
			fields=["name", "file_url", "file_name"],
		)

	def test_the_sealed_copy_is_filed_on_the_employee_too(self):
		name = self.signed_i9()
		employee = str(frappe.db.get_value("I-9 Form", name, "employee"))
		self.assertFalse(self.folder(employee), "the folder should be empty before the seal")

		sealed = self.seal(document_name=name)
		filed = sealed["employee_copy"]
		self.assertTrue(filed["filed"])
		self.assertEqual(filed["employee"], employee)
		self.assertFalse(filed["already_linked"])
		self.assertEqual([row["file_url"] for row in self.folder(employee)], [sealed["file_url"]])

	def test_it_is_a_second_link_and_not_a_second_copy_of_the_bytes(self):
		"""Two links to one artefact is a cross-reference. Two COPIES would be two
		documents that can drift apart, and the one thing a tamper-evident file
		must not do is exist twice under one hash."""
		name = self.signed_i9()
		employee = str(frappe.db.get_value("I-9 Form", name, "employee"))
		sealed = self.seal(document_name=name)

		rows = frappe.db.get_all("File", filters={"file_url": sealed["file_url"]}, fields=["name"])
		self.assertEqual(len(rows), 2, "one File on the form, one on the Employee, one URL")
		self.assertEqual(self.folder(employee)[0]["file_url"], sealed["file_url"])
		# And the bytes behind that URL are the sealed artefact itself, read
		# through the Employee's own link rather than the form's — same file,
		# same hash, which is what makes it evidence in both places.
		filed = self.folder(employee)[0]
		docname = str(frappe.db.get_value("File", {"file_url": filed["file_url"]}, "name"))
		self.assertEqual(pdf_seal.sha256_of(file_tools.read_file_bytes(docname)), sealed["sealed_pdf_hash"])

	def test_a_reseal_does_not_file_a_duplicate(self):
		"""`seal_signed_document` is documented as re-runnable — an operator
		re-seals a form that has gained a second signature. A personnel folder
		that grew a link per re-seal would be a changelog of one document."""
		name = self.signed_i9()
		employee = str(frappe.db.get_value("I-9 Form", name, "employee"))
		first = self.seal(document_name=name)
		again = self.seal(document_name=name)

		self.assertEqual(again["file_url"], first["file_url"])
		self.assertTrue(again["employee_copy"]["filed"])
		self.assertTrue(again["employee_copy"]["already_linked"])
		self.assertEqual(len(self.folder(employee)), 1)

	def test_a_second_signature_files_the_new_sealed_copy_beside_the_first(self):
		"""A re-seal after new ink is a DIFFERENT artefact with a different hash,
		and both are retained — the same promise the form's own attachments make.
		Nothing is deleted, here or there."""
		name = self.signed_i9()
		employee = str(frappe.db.get_value("I-9 Form", name, "employee"))
		first = self.seal(document_name=name)
		self.sign(doctype="I-9 Form", name=name, field="section_2_signature")
		second = self.seal(document_name=name)

		self.assertNotEqual(first["sealed_pdf_hash"], second["sealed_pdf_hash"])
		self.assertEqual(
			sorted(row["file_url"] for row in self.folder(employee)),
			sorted({first["file_url"], second["file_url"]}),
		)

	def test_a_form_that_names_nobody_is_told_so_rather_than_guessed_at(self):
		"""An employer return is signed by an officer and belongs in no personnel
		folder. `filed: false` with the reason is the honest answer; inventing a
		link would put a 941 in somebody's file."""
		name = self.signed_i9()
		frappe.db.set_value("I-9 Form", name, "employee", "")
		sealed = self.seal(document_name=name)

		self.assertTrue(sealed["sealed"])
		self.assertFalse(sealed["employee_copy"]["filed"])
		self.assertIn("names no employee", sealed["employee_copy"]["reason"])

	def test_the_cross_link_cannot_undo_the_seal(self):
		"""The ordering `tools/signatures.py` opens with, inherited: the sealed
		artefact is written and hashed before this runs, and a cross-reference
		that could fail the call would trade the irreplaceable thing for the
		convenient one."""
		from unittest import mock

		name = self.signed_i9()
		with mock.patch.object(
			file_tools, "insert_attachment", side_effect=RuntimeError("no room on the disk")
		):
			sealed = self.seal(document_name=name)

		self.assertTrue(sealed["sealed"])
		self.assertTrue(sealed["sealed_pdf_hash"])
		self.assertFalse(sealed["employee_copy"]["filed"])
		self.assertIn("no room on the disk", sealed["employee_copy"]["reason"])
		self.assertIn("attach_file_to_document", sealed["employee_copy"]["reason"])


# ── Claim 5 ─────────────────────────────────────────────────────────────────
@unittest.skipUnless(
	pdf_seal.available(),
	"drawing and appending the verification page needs reportlab and pypdf. Without "
	"them the seal tools go unavailable and say so, which is what the two classes "
	"left unguarded here exist to check.",
)
class TheSealRefusesAnUnsignedForm(SignedDocumentTestCase):
	def test_an_unsigned_form_is_refused(self):
		error = self.tool_error(
			"seal_signed_document", {"document_type": "I-9 Form", "document_name": self.an_i9()}
		)
		self.assertIn("no signature in any of its boxes", error)
		self.assertIn("vouches for nothing", error)

	def test_nothing_is_attached_by_the_refusal(self):
		name = self.an_i9()
		before = len(frappe.db.get_all("File", filters={"attached_to_name": name}))
		self.tool_error("seal_signed_document", {"document_type": "I-9 Form", "document_name": name})
		self.assertEqual(len(frappe.db.get_all("File", filters={"attached_to_name": name})), before)


# ── Claim 6 ─────────────────────────────────────────────────────────────────
@unittest.skipUnless(
	pdf_seal.available(),
	"drawing and appending the verification page needs reportlab and pypdf. Without "
	"them the seal tools go unavailable and say so, which is what the two classes "
	"left unguarded here exist to check.",
)
class TheSealFollowsTheSignature(SignedDocumentTestCase):
	def setUp(self):
		super().setUp()
		# THE ENTITY SCOPE, WHICH IS NOT OPTIONAL ON THIS SURFACE. `guard.require_scope`
		# inverts Frappe's rule — an account with no Company User Permission reaches
		# NOTHING here rather than everything — so a wrapper test with no permission
		# row is testing the refusal rather than the method. One row, on MAIN, which
		# is what `create_mobile_user` writes on a real site.
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-SEAL-1",
					"doctype": "User Permission",
					"user": frappe.session.user,
					"allow": "Company",
					"for_value": MAIN,
					"is_default": 1,
					"apply_to_all_doctypes": 1,
				}
			],
		)

	def wrapper(self, **kwargs):
		"""The mobile method with `guard.endpoint`'s gates unwrapped.

		The decorator runs the kill switch, the role gate, the enrolment gate, the
		rate limit and the audit row, all of which `test_api_mobile` already
		asserts in both directions. What this class is about is the BODY — that
		step 5 runs after the signature, follows `include_pdf`, and cannot take the
		signature down with it — so the gates are stepped over rather than
		re-tested here.
		"""
		from erpnext_mcp.api import mobile as mobile_api

		return mobile_api.submit_form_signature.__wrapped__(user=frappe.session.user, **kwargs)

	def test_submit_form_signature_seals_what_it_just_signed(self):
		name = self.an_i9()
		self.a_roster()
		answer = self.wrapper(
			doctype="I-9 Form",
			docname=name,
			signature_field="section_1_signature",
			signature_image=A_CAPTURE,
		)
		self.assertTrue(answer["seal"]["sealed"])
		self.assertTrue(answer["seal"]["sealed_pdf_hash"].startswith("sha256:"))
		self.assertEqual(self.only_row()["sealed_pdf_hash"], answer["seal"]["sealed_pdf_hash"])

	def test_turning_the_page_off_turns_the_seal_off(self):
		"""A caller that wanted only the write did not want a page, and sealing
		produces one. Turning it back on for them would be the method deciding it
		knows better."""
		name = self.an_i9()
		self.a_roster()
		answer = self.wrapper(
			doctype="I-9 Form",
			docname=name,
			signature_field="section_1_signature",
			signature_image=A_CAPTURE,
			include_pdf=False,
		)
		self.assertFalse(answer["seal"]["sealed"])
		self.assertIn("include_pdf was off", answer["seal"]["note"])

	def test_a_seal_that_fails_does_not_lose_the_signature(self):
		"""The ordering rule the whole signature path keeps: each step may fail
		without undoing the one before it, because the capture is the
		irreplaceable artefact and the signer has gone back to work."""
		from unittest import mock

		name = self.an_i9()
		self.a_roster()
		with mock.patch.object(
			signed_documents, "seal_signed_document", side_effect=RuntimeError("no reportlab")
		):
			answer = self.wrapper(
				doctype="I-9 Form",
				docname=name,
				signature_field="section_1_signature",
				signature_image=A_CAPTURE,
			)
		self.assertFalse(answer["seal"]["sealed"])
		self.assertIn("no reportlab", answer["seal"]["note"])
		self.assertTrue(answer["file_url"], "the signature is on the record regardless")
		self.assertTrue(frappe.db.get_value("I-9 Form", name, "section_1_signature"))

	def test_the_idempotent_retry_seals_nothing(self):
		"""On the retry path a fresh sealed copy per attempt would be a marginal
		link filling somebody's personnel record with near-identical PDFs."""
		name = self.an_i9()
		self.a_roster()
		self.wrapper(
			doctype="I-9 Form",
			docname=name,
			signature_field="section_1_signature",
			signature_image=A_CAPTURE,
		)
		files_before = len(frappe.db.get_all("File", filters={"attached_to_name": name}))
		again = self.wrapper(
			doctype="I-9 Form",
			docname=name,
			signature_field="section_1_signature",
			signature_image=A_CAPTURE,
		)
		self.assertTrue(again["already_signed"])
		self.assertFalse(again["seal"]["sealed"])
		self.assertEqual(len(frappe.db.get_all("File", filters={"attached_to_name": name})), files_before)


# ── the entity gate, and the order it runs in ───────────────────────────────
class TheEntityGateRunsBeforeTheDraw(SignedDocumentTestCase):
	"""A caller scoped to one company reaches nothing in another, and finds out
	before anything of theirs lands on the record.

	The preview DRAWS the page where a record has none, so a gate that ran on the
	way out would refuse the bytes having already rendered and attached a File to
	a form in a company this caller may not touch. `employee=` is the argument
	that makes this more than theoretical: the tool resolves a person to a form,
	so without the check a docname the caller could not name directly is reachable
	by naming the person it belongs to.
	"""

	def scope_to_other(self):
		"""Scope this account to the OTHER entity, AFTER the fixture is built.

		Not in `setUp`, because `signatures._require_entity` refuses a signature
		on a form the caller is not scoped to — so a fixture that scoped first
		could not produce the signed I-9 the seal case needs. Scoping afterwards
		is also the truer shape: the record exists and belongs to somebody else.
		"""
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-OTHER-1",
					"doctype": "User Permission",
					"user": frappe.session.user,
					"allow": "Company",
					"for_value": OTHER,
					"is_default": 1,
					"apply_to_all_doctypes": 1,
				}
			],
		)

	def wrapper(self, method, **kwargs):
		from erpnext_mcp.api import mobile as mobile_api

		return getattr(mobile_api, method).__wrapped__(user=frappe.session.user, **kwargs)

	def test_a_form_in_another_entity_reads_as_not_found(self):
		name = self.an_i9()  # seeded against MAIN; this caller holds OTHER
		self.scope_to_other()
		with self.assertRaises(Exception) as caught:
			self.wrapper("get_document_preview", document_type="I-9 Form", document_name=name)
		self.assertIn("was not found", str(caught.exception))

	def test_nothing_was_drawn_by_the_refusal(self):
		name = self.an_i9()
		self.scope_to_other()
		with self.assertRaises(Exception):
			self.wrapper("get_document_preview", document_type="I-9 Form", document_name=name)
		self.assertFalse(frappe.db.get_value("I-9 Form", name, "generated_pdf"))
		self.assertEqual(frappe.db.get_all("File", filters={"attached_to_name": name}), [])

	def test_naming_the_person_is_not_a_way_round_it(self):
		self.an_i9()
		self.scope_to_other()
		with self.assertRaises(Exception) as caught:
			self.wrapper("get_document_preview", document_type="I-9 Form", employee="HR-EMP-00002")
		self.assertIn("was not found", str(caught.exception))

	def test_the_seal_refuses_the_same_way_and_writes_nothing(self):
		name = self.signed_i9()
		self.scope_to_other()
		with self.assertRaises(Exception) as caught:
			self.wrapper("seal_signed_document", document_type="I-9 Form", document_name=name)
		self.assertIn("was not found", str(caught.exception))
		self.assertFalse(self.only_row().get("sealed_pdf"))


# ── the pure module, with no site behind it ─────────────────────────────────
@unittest.skipUnless(
	pdf_seal.available(),
	"drawing and appending the verification page needs reportlab and pypdf. Without "
	"them the seal tools go unavailable and say so, which is what the two classes "
	"left unguarded here exist to check.",
)
class ThePureSealModule(EvidenceTestCase):
	def test_the_hash_is_prefixed_the_way_document_hash_is(self):
		"""An operator comparing a document fingerprint with a file hash should
		not have to notice that one carries an algorithm name and the other does
		not."""
		self.assertTrue(pdf_seal.sha256_of(b"x").startswith("sha256:"))
		self.assertEqual(len(pdf_seal.sha256_of(b"x")), 71)

	def test_a_missing_identity_check_is_printed_rather_than_omitted(self):
		"""The single most important thing on the page. Every other empty value is
		dropped, because "GPS: —" invites the reading that a fix was taken and
		lost."""
		lines = dict(pdf_seal.block_lines({"document_type": "I-9 Form", "document_name": "I9-1"}))
		self.assertIn("No identity check was made", lines["Identity verified"])
		self.assertNotIn("Coordinates", lines)
		self.assertNotIn("Device", lines)

	def test_half_a_fix_is_no_fix(self):
		"""A latitude with no longitude is a point on a line rather than a place."""
		self.assertEqual(pdf_seal._coordinates({"gps_latitude": 45.5}), "")
		self.assertEqual(pdf_seal._coordinates({"gps_longitude": -122.6}), "")
		self.assertEqual(
			pdf_seal._coordinates({"gps_latitude": 45.5, "gps_longitude": -122.6}), "45.500000, -122.600000"
		)

	def test_an_unset_float_column_is_not_a_place_in_the_gulf_of_guinea(self):
		"""v0.136.0, AND THIS ONE SHIPPED. Every sealed I-9 this app produced
		carried `Coordinates 0.000000, 0.000000` on its verification page.

		The guard above tests `in (None, "")`, which is correct about the
		ARGUMENT `signing_evidence.record` passes and never sees the value that
		comes back out of the database: `gps_latitude` and `gps_longitude` are
		`Float`, MariaDB stores those `NOT NULL DEFAULT 0`, and a signature that
		reported no fix reads back as two zeroes. 0°N 0°E is open water about
		300 miles off Ghana, so the page told an inspector where somebody stood
		and was wrong — which is the one direction that matters on a record whose
		whole purpose is to state what was and was not observed.

		THE LITERAL ZEROES BELOW ARE LOAD-BEARING AND MUST NOT BE "SIMPLIFIED"
		INTO A SEEDED ROW. `STORE.seed` stores the dict it is handed verbatim and
		does not model `NOT NULL DEFAULT 0`, so a fixture that seeds
		`gps_latitude=None` and reads it back gets `None` in this suite and `0.0`
		on a bench. The standalone double CANNOT reproduce this bug class through
		normal seeding — handing `_coordinates` the value the database would
		return is the only shape of test that exercises it, and a version rewritten
		to look more realistic would go vacuously green.

		`tools/housing._gps` has guarded the same pair the same way since it was
		written; this is the module that did not.
		"""
		self.assertEqual(pdf_seal._coordinates({"gps_latitude": 0.0, "gps_longitude": 0.0}), "")
		self.assertEqual(pdf_seal._coordinates({"gps_latitude": 0, "gps_longitude": 0}), "")

	def test_the_seal_page_omits_the_line_entirely_for_a_signature_with_no_fix(self):
		"""Not "Coordinates: —", which invites the reading that a fix was taken
		and lost. The row is dropped, exactly as `Device` is."""
		lines = dict(
			pdf_seal.block_lines(
				{
					"document_type": "I-9 Form",
					"document_name": "I9-2026-0011",
					"gps_latitude": 0.0,
					"gps_longitude": 0.0,
				}
			)
		)
		self.assertNotIn("Coordinates", lines)

	def test_a_zero_on_one_axis_alone_is_kept_because_it_is_a_real_place(self):
		"""The equator and the prime meridian are real lines. Treating any zero
		as unset would be the zero-drop `test_zero_drop.py` exists to catch,
		committed while fixing its mirror image."""
		self.assertEqual(
			pdf_seal._coordinates({"gps_latitude": 0.0, "gps_longitude": -122.6}), "0.000000, -122.600000"
		)
		self.assertEqual(
			pdf_seal._coordinates({"gps_latitude": 45.5, "gps_longitude": 0.0}), "45.500000, 0.000000"
		)

	def test_a_long_token_wraps_by_measurement_rather_than_running_off_the_page(self):
		"""A 71-character hash contains no spaces, so a word-only wrapper hands
		back one line and reportlab draws it off the right edge — silently, which
		is the failure that makes a verification record useless unnoticed."""
		from reportlab.pdfbase.pdfmetrics import stringWidth

		digest = pdf_seal.sha256_of(b"anything")
		lines = pdf_seal._wrap(digest, 120.0, "Courier", 7.0, stringWidth)
		self.assertGreater(len(lines), 1)
		self.assertEqual("".join(lines), digest)
		for line in lines:
			self.assertLessEqual(stringWidth(line, "Courier", 7.0), 120.0)
