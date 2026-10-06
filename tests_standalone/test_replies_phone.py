"""office@ replies reviewed on the phone. v0.258.0 — docs/design/office_reply_drafts.md §3 (Phase 3)."""

from unittest import mock

import frappe

from erpnext_mcp import device_keys, mail_drafts, office_mail, tile_queries
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, OTHER
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase
from .test_security_217 import Defaults

DRAFT = "Thanks — we have your order and will confirm the delivery date."


class ReplyCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		original = getattr(frappe, "defaults", None)
		frappe.defaults = Defaults()
		self.addCleanup(lambda: setattr(frappe, "defaults", original) if original else delattr(frappe, "defaults"))
		self.configure(office_mail_enabled=1)
		STORE.seed("Email Account", [{"name": "Office", "email_id": "office@orchardmeadow.net", "enable_incoming": 1}])
		STORE.seed("Customer", [{"name": "Valley Fruit Co", "customer_name": "Valley Fruit Co",
		                         "email_id": "buyer@valleyfruit.com"}])
		set_roles(WORKER, ["Field Worker", "Accounts User"])
		self.sent = []
		patcher = mock.patch.object(mail_drafts, "_send", side_effect=lambda row, text, files, who:
		                            self.sent.append((text, files, who)) or f"COMM-SENT-{len(self.sent)}")
		patcher.start()
		self.addCleanup(patcher.stop)
		self.n = 0

	def drafted(self, company=MAIN, text=DRAFT):
		self.n += 1
		comm = f"COMM-{self.n:04d}"
		STORE.seed("Communication", [{"name": comm, "subject": "Order 42", "sender": "buyer@valleyfruit.com",
		                              "content": "<p>Can you confirm delivery?</p>", "communication_type": "Communication",
		                              "communication_medium": "Email", "sent_or_received": "Received",
		                              "email_account": "Office", "communication_date": frappe.utils.now(),
		                              "creation": frappe.utils.now()}])
		row = office_mail.triage_one(comm)
		frappe.db.set_value("Office Mail", row["name"], "company", company)
		mail_drafts.save_draft(row["name"], text, model="claude")
		return row["name"]


class TheQueue(ReplyCase):
	def test_an_approver_sees_the_reply_and_reads_it_whole(self):
		name = self.drafted()
		self.be()
		listed = mobile_api.list_replies_to_review()
		self.assertEqual([r["name"] for r in listed["replies"]], [name])
		one = mobile_api.get_reply_to_review(name=name)
		self.assertEqual((one["draft_text"], one["mail_class"]), (DRAFT, "customer"))
		self.assertTrue(one["message"]["untrusted"])

	def test_someone_who_approves_nothing_is_refused_by_name(self):
		self.drafted()
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaisesRegex(frappe.PermissionError, "is restricted to"):
			mobile_api.list_replies_to_review()

	def test_another_companys_reply_is_not_found(self):
		name = self.drafted(company=OTHER)
		self.be()
		self.assertEqual(mobile_api.list_replies_to_review()["replies"], [])
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_reply_to_review(name=name)


class EditApproveDiscard(ReplyCase):
	def test_edited_then_approved_sends_once_from_the_phone(self):
		name = self.drafted()
		self.be()
		edited = mobile_api.update_reply_draft(name=name, text="Thanks — delivery is Thursday morning.")
		self.assertEqual(edited["state"], "Edited")
		sent = mobile_api.approve_reply(name=name)
		self.assertEqual((sent["state"], len(self.sent)), ("Sent", 1))
		self.assertEqual(self.sent[0][0], "Thanks — delivery is Thursday morning.")
		self.assertTrue(str(frappe.db.get_value("Office Mail", name, "approved_via")).startswith("phone"))

	def test_with_device_keys_on_face_id_signs_the_exact_text_and_files(self):
		name = self.drafted()
		frappe.db.commit()  # the refused call below rolls back; on a site the draft is long committed
		self.be()
		with mock.patch.object(device_keys, "enabled", return_value=True):
			with self.assertRaisesRegex(frappe.PermissionError, "Face ID"):
				mobile_api.approve_reply(name=name)
			self.assertEqual(self.sent, [])
			frappe.local.erpnext_mcp_device, frappe.local.erpnext_mcp_key_bound = "DEV-1", True
			self.addCleanup(lambda: [setattr(frappe.local, k, v) for k, v in
			                         (("erpnext_mcp_device", ""), ("erpnext_mcp_key_bound", False))])
			with mock.patch.object(device_keys, "_verify_approver") as verify:
				mobile_api.approve_reply(name=name, signature="sig")
		user, device, message, signature = verify.call_args[0]
		self.assertEqual((user, device, signature), (WORKER, "DEV-1", "sig"))
		self.assertEqual(message, mail_drafts.signed_message(name, DRAFT, []))
		self.assertIn("Face ID", frappe.db.get_value("Office Mail", name, "approved_via"))

	def test_discard_sends_nothing(self):
		name = self.drafted()
		self.be()
		self.assertEqual(mobile_api.discard_reply(name=name, reason="Answered by phone")["state"], "Discarded")
		self.assertEqual(self.sent, [])
		self.assertEqual(mobile_api.list_replies_to_review()["replies"], [])


class TheTile(ReplyCase):
	def test_the_tile_counts_what_waits_and_hides_with_office_mail_off(self):
		name = self.drafted()
		answer = tile_queries.run("replies_to_review", WORKER, MAIN)
		self.assertEqual((answer["count"], answer["rows"][0]["doctype"], answer["rows"][0]["name"]),
		                 (1, "Office Mail", name))
		self.assertTrue(tile_queries.available("replies_to_review"))
		self.configure(office_mail_enabled=0)
		self.assertFalse(tile_queries.available("replies_to_review"))
