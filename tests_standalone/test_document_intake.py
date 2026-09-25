# SPDX-License-Identifier: MIT
"""Document intake — the queue of unfiled email, reading one, routing it, and the log."""

import base64

from erpnext_mcp.tools import document_intake

from .fixtures import V2TestCase
from .harness import STORE

#: A draft Journal Entry with nothing attached, from the shared fixture: the target.
TARGET = "ACC-JV-2026-00002"

PDF_BYTES = b"%PDF-1.4 licence renewal"


def _communication(name, creation, **overrides):
	row = {
		"name": name,
		"subject": f"Subject of {name}",
		"sender": "clerk@agr.wa.gov",
		"sender_full_name": "WSDA Licensing",
		"recipients": "office@orchardmeadow.net",
		"content": "<p>Please find attached.</p>",
		"communication_type": "Communication",
		"sent_or_received": "Received",
		"status": "Open",
		"reference_doctype": None,
		"reference_name": None,
		"has_attachment": 1,
		"creation": creation,
	}
	row.update(overrides)
	return row


def _file(name, communication, file_name, size, private=1, creation="2026-09-20 08:00:00"):
	folder = "/private/files/" if private else "/files/"
	return {
		"name": name,
		"file_name": file_name,
		"file_url": folder + file_name,
		"file_size": size,
		"is_private": private,
		"is_folder": 0,
		"attached_to_doctype": "Communication",
		"attached_to_name": communication,
		"creation": creation,
	}


class IntakeTestCase(V2TestCase):
	def setUp(self):
		super().setUp()
		STORE.seed(
			"Communication",
			[
				_communication("COMM-LICENCE", "2026-09-20 08:00:00", subject="Applicator licence renewal"),
				_communication(
					"COMM-CERT",
					"2026-09-21 09:00:00",
					subject="Training certificate",
					sender="trainer@wsu.edu",
					reference_doctype="",
				),
				# Not in the queue, each for its own reason.
				_communication("COMM-NO-FILES", "2026-09-22 10:00:00", has_attachment=0),
				_communication("COMM-SENT", "2026-09-22 11:00:00", sent_or_received="Sent"),
				_communication(
					"COMM-INVOICE",
					"2026-09-22 12:00:00",
					reference_doctype="Journal Entry",
					reference_name="ACC-JV-2026-00001",
					status="Linked",
				),
				_communication(
					"COMM-AUTOMATED", "2026-09-22 13:00:00", communication_type="Automated Message"
				),
			],
		)
		STORE.seed(
			"File",
			[
				_file("file-licence-pdf", "COMM-LICENCE", "licence.pdf", len(PDF_BYTES)),
				_file(
					"file-licence-jpg", "COMM-LICENCE", "licence-card.jpg", 4, creation="2026-09-20 08:00:01"
				),
				_file("file-cert-pdf", "COMM-CERT", "certificate.pdf", 3 * 1024 * 1024),
				_file("file-sent", "COMM-SENT", "quote.pdf", 10),
				_file("file-invoice", "COMM-INVOICE", "invoice.pdf", 10),
			],
		)
		STORE.file_contents["file-licence-pdf"] = PDF_BYTES
		STORE.file_contents["file-licence-jpg"] = b"\xff\xd8\xff\xe0"
		STORE.file_contents["file-cert-pdf"] = b"x" * (3 * 1024 * 1024)

	def files_on(self, doctype, name):
		return [
			row
			for row in STORE.rows("File")
			if row.get("attached_to_doctype") == doctype and row.get("attached_to_name") == name
		]


# ── list_incoming_documents ─────────────────────────────────────────────────
class ListIncomingDocuments(IntakeTestCase):
	def test_only_received_unlinked_email_with_attachments_is_queued(self):
		data = self.tool_data("list_incoming_documents", {})
		self.assertEqual([row["name"] for row in data["documents"]], ["COMM-CERT", "COMM-LICENCE"])

	def test_an_empty_reference_doctype_counts_as_unlinked(self):
		names = [row["name"] for row in self.tool_data("list_incoming_documents", {})["documents"]]
		self.assertIn("COMM-CERT", names)

	def test_each_row_names_its_attachments(self):
		data = self.tool_data("list_incoming_documents", {})
		row = next(item for item in data["documents"] if item["name"] == "COMM-LICENCE")
		self.assertEqual(row["attachment_count"], 2)
		self.assertEqual(row["attachment_filenames"], ["licence.pdf", "licence-card.jpg"])
		self.assertEqual(row["subject"], "Applicator licence renewal")
		self.assertEqual(row["sender"], "clerk@agr.wa.gov")

	def test_sender_filter_matches_part_of_the_address(self):
		data = self.tool_data("list_incoming_documents", {"sender": "wsu.edu"})
		self.assertEqual([row["name"] for row in data["documents"]], ["COMM-CERT"])

	def test_since_date_filter(self):
		data = self.tool_data("list_incoming_documents", {"since_date": "2026-09-21"})
		self.assertEqual([row["name"] for row in data["documents"]], ["COMM-CERT"])

	def test_limit_defaults_to_twenty_and_is_honoured(self):
		self.assertEqual(self.tool_data("list_incoming_documents", {})["limit"], 20)
		data = self.tool_data("list_incoming_documents", {"limit": 1})
		self.assertEqual(data["count"], 1)
		self.assertTrue(data["truncated"])

	def test_it_is_on_by_default(self):
		self.configure(enabled=1)
		self.tool_data("list_incoming_documents", {})


# ── get_incoming_document ───────────────────────────────────────────────────
class GetIncomingDocument(IntakeTestCase):
	def test_the_body_comes_back_as_plain_text(self):
		STORE.tables["Communication"]["COMM-LICENCE"]["content"] = (
			"<html><head><style>p { color: red }</style></head><body>"
			"<p>Hello&nbsp;Tim,</p><p>Your licence &amp; card are attached.<br>Thanks</p>"
			"<script>track()</script></body></html>"
		)
		data = self.tool_data("get_incoming_document", {"name": "COMM-LICENCE"})
		self.assertEqual(data["content"], "Hello Tim,\n\nYour licence & card are attached.\nThanks")
		self.assertNotIn("color", data["content"])
		self.assertNotIn("track", data["content"])

	def test_it_carries_the_header_fields(self):
		data = self.tool_data("get_incoming_document", {"name": "COMM-LICENCE"})
		self.assertEqual(data["subject"], "Applicator licence renewal")
		self.assertEqual(data["sender"], "clerk@agr.wa.gov")
		self.assertEqual(data["recipients"], "office@orchardmeadow.net")
		self.assertTrue(data["creation"])
		self.assertFalse(data["routed"])
		self.assertIsNone(data["reference_doctype"])

	def test_every_attachment_is_described(self):
		data = self.tool_data("get_incoming_document", {"name": "COMM-LICENCE"})
		by_name = {row["file_name"]: row for row in data["attachments"]}
		self.assertEqual(set(by_name), {"licence.pdf", "licence-card.jpg"})
		pdf = by_name["licence.pdf"]
		self.assertEqual(pdf["name"], "file-licence-pdf")
		self.assertEqual(pdf["file_url"], "/private/files/licence.pdf")
		self.assertEqual(pdf["mime_type"], "application/pdf")
		self.assertTrue(pdf["is_private"])
		self.assertEqual(pdf["file_size"], len(PDF_BYTES))

	def test_a_small_pdf_comes_back_inline(self):
		data = self.tool_data("get_incoming_document", {"name": "COMM-LICENCE"})
		pdf = next(row for row in data["attachments"] if row["file_name"] == "licence.pdf")
		self.assertEqual(pdf["encoding"], "base64")
		self.assertEqual(base64.b64decode(pdf["content_base64"]), PDF_BYTES)

	def test_a_file_that_is_not_a_pdf_is_not_inlined(self):
		data = self.tool_data("get_incoming_document", {"name": "COMM-LICENCE"})
		jpg = next(row for row in data["attachments"] if row["file_name"] == "licence-card.jpg")
		self.assertNotIn("content_base64", jpg)

	def test_a_pdf_over_two_megabytes_is_not_inlined(self):
		data = self.tool_data("get_incoming_document", {"name": "COMM-CERT"})
		self.assertNotIn("content_base64", data["attachments"][0])
		self.assertEqual(data["attachments"][0]["file_url"], "/private/files/certificate.pdf")

	def test_an_unreadable_pdf_is_reported_and_the_rest_still_returns(self):
		del STORE.file_contents["file-licence-pdf"]
		data = self.tool_data("get_incoming_document", {"name": "COMM-LICENCE"})
		pdf = next(row for row in data["attachments"] if row["file_name"] == "licence.pdf")
		self.assertIn("content_error", pdf)
		self.assertEqual(data["attachment_count"], 2)

	def test_a_linked_email_says_what_it_is_linked_to(self):
		data = self.tool_data("get_incoming_document", {"name": "COMM-INVOICE"})
		self.assertTrue(data["routed"])
		self.assertEqual(data["reference_doctype"], "Journal Entry")
		self.assertEqual(data["reference_name"], "ACC-JV-2026-00001")

	def test_an_unknown_email_is_refused(self):
		self.assertIn(
			"no Communication named", self.tool_error("get_incoming_document", {"name": "COMM-NOPE"})
		)

	def test_no_read_permission_means_no_email(self):
		STORE.denied_permissions.add(("Communication", "COMM-LICENCE"))
		message = self.tool_error("get_incoming_document", {"name": "COMM-LICENCE"})
		self.assertIn("not permitted to read Communication", message)


# ── route_incoming_document ─────────────────────────────────────────────────
class RouteIncomingDocument(IntakeTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_route_incoming_document=1)

	def payload(self, **overrides):
		values = {
			"communication": "COMM-LICENCE",
			"target_doctype": "Journal Entry",
			"target_name": TARGET,
			"classification": "applicator_license",
		}
		values.update(overrides)
		return values

	def test_every_attachment_lands_on_the_target(self):
		data = self.tool_data("route_incoming_document", self.payload())
		self.assertEqual(data["attachments_routed"], 2)
		self.assertEqual(sorted(data["file_names"]), ["licence-card.jpg", "licence.pdf"])
		landed = self.files_on("Journal Entry", TARGET)
		self.assertEqual(sorted(row["file_name"] for row in landed), ["licence-card.jpg", "licence.pdf"])

	def test_the_file_moves_by_url_not_by_bytes(self):
		"""The whole point: the target's File points at the email's stored file.
		Nothing was decoded, re-encoded or written as new content."""
		self.tool_data("route_incoming_document", self.payload())
		landed = {row["file_name"]: row for row in self.files_on("Journal Entry", TARGET)}
		self.assertEqual(landed["licence.pdf"]["file_url"], "/private/files/licence.pdf")
		self.assertEqual(landed["licence.pdf"]["is_private"], 1)
		self.assertNotIn(landed["licence.pdf"]["name"], STORE.file_contents)

	def test_the_emails_own_files_are_left_where_they_were(self):
		self.tool_data("route_incoming_document", self.payload())
		self.assertEqual(len(self.files_on("Communication", "COMM-LICENCE")), 2)

	def test_the_email_is_linked_the_way_relink_links_it(self):
		self.tool_data("route_incoming_document", self.payload())
		row = STORE.get_raw("Communication", "COMM-LICENCE")
		self.assertEqual(row["reference_doctype"], "Journal Entry")
		self.assertEqual(row["reference_name"], TARGET)
		self.assertEqual(row["status"], "Linked")

	def test_it_leaves_the_audit_comment_on_the_email(self):
		data = self.tool_data("route_incoming_document", self.payload(note="renewal for 2027"))
		comment = STORE.get_raw("Comment", data["audit_comment"])
		self.assertEqual(comment["reference_doctype"], "Communication")
		self.assertEqual(comment["reference_name"], "COMM-LICENCE")
		self.assertEqual(
			comment["content"],
			f"Document Intake Agent routed to Journal Entry {TARGET} as applicator_license.\n"
			"Note: renewal for 2027",
		)

	def test_the_reply_names_what_was_done(self):
		data = self.tool_data("route_incoming_document", self.payload())
		self.assertEqual(data["communication"], "COMM-LICENCE")
		self.assertEqual(data["target_doctype"], "Journal Entry")
		self.assertEqual(data["target_name"], TARGET)
		self.assertEqual(data["classification"], "applicator_license")

	def test_the_email_leaves_the_queue(self):
		self.tool_data("route_incoming_document", self.payload())
		names = [row["name"] for row in self.tool_data("list_incoming_documents", {})["documents"]]
		self.assertNotIn("COMM-LICENCE", names)

	def test_the_audit_row_carries_the_routing(self):
		self.tool_data("route_incoming_document", self.payload())
		row = self.assertAudited("route_incoming_document", status="Success")
		self.assertIn("routed Communication COMM-LICENCE", row["result_summary"])
		self.assertIn("applicator_license", row["result_summary"])

	def test_an_email_already_linked_is_refused(self):
		message = self.tool_error("route_incoming_document", self.payload(communication="COMM-INVOICE"))
		self.assertIn("already linked", message)
		self.assertEqual(self.files_on("Journal Entry", TARGET), [])

	def test_an_email_with_no_files_is_refused(self):
		message = self.tool_error("route_incoming_document", self.payload(communication="COMM-NO-FILES"))
		self.assertIn("nothing to route", message)

	def test_a_classification_that_is_not_a_tag_is_refused(self):
		message = self.tool_error("route_incoming_document", self.payload(classification="Licence renewal."))
		self.assertIn("lower-case tag", message)
		self.assertIsNone(STORE.get_raw("Communication", "COMM-LICENCE")["reference_doctype"])

	def test_a_classification_is_folded_to_lower_case(self):
		data = self.tool_data("route_incoming_document", self.payload(classification="Applicator_License"))
		self.assertEqual(data["classification"], "applicator_license")

	def test_a_missing_target_is_refused_and_nothing_is_linked(self):
		message = self.tool_error("route_incoming_document", self.payload(target_name="ACC-JV-NOPE"))
		self.assertIn("no Journal Entry named", message)
		self.assertIsNone(STORE.get_raw("Communication", "COMM-LICENCE")["reference_doctype"])

	def test_a_cancelled_target_is_refused(self):
		STORE.tables["Journal Entry"][TARGET]["docstatus"] = 2
		message = self.tool_error("route_incoming_document", self.payload())
		self.assertIn("CANCELLED", message)

	def test_a_half_routed_email_is_rolled_back_whole(self):
		"""The second file clashes with a name already on the target. The first
		file had already been attached; the refusal must take it back too."""
		STORE.seed(
			"File",
			[
				dict(
					_file("file-existing", "x", "licence-card.jpg", 4),
					attached_to_doctype="Journal Entry",
					attached_to_name=TARGET,
				)
			],
		)
		message = self.tool_error("route_incoming_document", self.payload())
		self.assertIn("already has an attachment named 'licence-card.jpg'", message)
		self.assertEqual([row["name"] for row in self.files_on("Journal Entry", TARGET)], ["file-existing"])
		self.assertIsNone(STORE.get_raw("Communication", "COMM-LICENCE")["reference_doctype"])

	def test_no_write_on_the_target_is_refused(self):
		STORE.denied_permissions.add(("Journal Entry", "write"))
		message = self.tool_error("route_incoming_document", self.payload())
		self.assertIn("not permitted to write Journal Entry", message)

	def test_no_write_on_the_email_is_refused(self):
		STORE.denied_permissions.add(("Communication", "COMM-LICENCE", "write"))
		message = self.tool_error("route_incoming_document", self.payload())
		self.assertIn("not permitted to write Communication", message)
		self.assertEqual(self.files_on("Journal Entry", TARGET), [])

	def test_it_is_off_until_an_operator_turns_it_on(self):
		self.configure(enabled=1)
		message = self.tool_error("route_incoming_document", self.payload())
		self.assertIn("allow_route_incoming_document", message)
		self.assertEqual(self.files_on("Journal Entry", TARGET), [])


# ── list_document_intake_log ────────────────────────────────────────────────
class ListDocumentIntakeLog(IntakeTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_route_incoming_document=1)
		self.tool_data(
			"route_incoming_document",
			{
				"communication": "COMM-LICENCE",
				"target_doctype": "Journal Entry",
				"target_name": TARGET,
				"classification": "training_certificate_renewal",
				"note": "prefix trap",
			},
		)
		self.tool_data(
			"route_incoming_document",
			{
				"communication": "COMM-CERT",
				"target_doctype": "Journal Entry",
				"target_name": "ACC-JV-2026-00001",
				"classification": "training_certificate",
			},
		)

	def test_it_lists_what_the_agent_routed_newest_first(self):
		data = self.tool_data("list_document_intake_log", {})
		self.assertEqual([row["communication"] for row in data["entries"]], ["COMM-CERT", "COMM-LICENCE"])
		first = data["entries"][0]
		self.assertEqual(first["classification"], "training_certificate")
		self.assertEqual(first["reference_doctype"], "Journal Entry")
		self.assertEqual(first["reference_name"], "ACC-JV-2026-00001")
		self.assertEqual(first["subject"], "Training certificate")
		self.assertEqual(first["sender"], "trainer@wsu.edu")
		self.assertTrue(first["routed_at"])

	def test_an_email_erpnext_linked_by_itself_is_not_in_the_log(self):
		names = [row["communication"] for row in self.tool_data("list_document_intake_log", {})["entries"]]
		self.assertNotIn("COMM-INVOICE", names)

	def test_the_note_is_read_back(self):
		data = self.tool_data("list_document_intake_log", {})
		entry = next(row for row in data["entries"] if row["communication"] == "COMM-LICENCE")
		self.assertEqual(entry["note"], "prefix trap")

	def test_a_classification_filter_does_not_match_a_longer_tag(self):
		data = self.tool_data("list_document_intake_log", {"classification": "training_certificate"})
		self.assertEqual([row["communication"] for row in data["entries"]], ["COMM-CERT"])

	def test_target_doctype_filter(self):
		self.assertEqual(
			self.tool_data("list_document_intake_log", {"target_doctype": "Employee"})["count"], 0
		)
		self.assertEqual(
			self.tool_data("list_document_intake_log", {"target_doctype": "Journal Entry"})["count"], 2
		)

	def test_limit_defaults_to_fifty(self):
		self.assertEqual(self.tool_data("list_document_intake_log", {})["limit"], 50)

	def test_since_date_in_the_future_finds_nothing(self):
		self.assertEqual(self.tool_data("list_document_intake_log", {"since_date": "2099-01-01"})["count"], 0)


class HtmlToText(V2TestCase):
	def test_plain_text_passes_through(self):
		self.assertEqual(document_intake.html_to_text("Just words &amp; more"), "Just words & more")

	def test_empty_is_empty(self):
		self.assertEqual(document_intake.html_to_text(None), "")

	def test_blank_runs_collapse(self):
		self.assertEqual(
			document_intake.html_to_text("<div>a</div><div></div><div></div><div>b</div>"), "a\n\nb"
		)
