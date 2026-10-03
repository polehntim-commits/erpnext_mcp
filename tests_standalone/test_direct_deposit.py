# SPDX-License-Identifier: MIT
"""Secure direct-deposit setup. v0.225.0 — docs/design/direct_deposit_setup.md."""

import base64
import json
from unittest import mock

import frappe

from erpnext_mcp import direct_deposit, plaid_link
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_ach import ACHTestCase
from .test_api_mobile import WORKER, WORKER_EMPLOYEE, MobileAPITestCase

CASH_APP = "041215663"  # Sutton Bank
ACCOUNT = "123456789012"
HR = "hr@example.test"


def body(**over):
	return {
		"routing_number": CASH_APP,
		"account_number": ACCOUNT,
		"account_number_confirm": ACCOUNT,
		"account_type": "Checking",
		**over,
	}


class DDCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			direct_deposit_self_service=1,
			**self.on_switches(),
		)
		STORE.seed("User", [{"name": HR, "enabled": 1}])
		set_roles(HR, ["HR Manager"])
		frappe.db.set_value("Employee", WORKER_EMPLOYEE, "personal_email", "ana.home@example.test")
		plaid_link._LOCAL_TOKENS.clear()

	def on_switches(self):
		from .test_api_mobile import ON

		return ON

	def submit(self, **over):
		self.be()
		answer = mobile_api.submit_direct_deposit_change(**body(**over))
		STORE.commit()  # a served request commits; a later refusal rolls back only itself
		return answer

	def full(self, name):
		from erpnext_mcp.tools import ach

		return ach._full_account_number(name)


class Submitting(DDCase):
	def test_off_by_default(self):
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **self.on_switches())
		self.be()
		with self.assertRaises(frappe.ValidationError):
			mobile_api.submit_direct_deposit_change(**body())

	def test_pending_masked_signed_and_noticed(self):
		STORE.emails.clear()
		answer = self.submit()
		row = frappe.get_doc("Employee Bank Account", answer["account"]).as_dict()
		self.assertEqual((row["status"], row["verification_state"]), ("Pending", "Submitted"))
		self.assertEqual(row["bank_name"], "Sutton Bank (Cash App)")
		self.assertEqual(self.full(answer["account"]), ACCOUNT)
		self.assertNotIn(ACCOUNT, json.dumps(answer))
		self.assertNotIn(CASH_APP, json.dumps(answer))
		self.assertEqual(answer["routing"], "•••• 5663")
		ev = frappe.get_doc("Signing Evidence", row["signing_evidence"]).as_dict()
		self.assertEqual(ev["verification_method"], "App sign-in")
		self.assertNotIn(ACCOUNT, json.dumps(ev, default=str))
		notice = [m for m in STORE.emails if "ana.home@example.test" in m["recipients"]]
		self.assertTrue(notice)
		self.assertNotIn("5663", notice[0]["message"])
		self.assertNotIn("9012", notice[0]["message"])
		mine = mobile_api.get_my_direct_deposit()
		self.assertNotIn(ACCOUNT, json.dumps(mine))

	def test_bad_numbers_are_refused(self):
		for over in (
			{"routing_number": "041215664"},
			{"account_number_confirm": "999"},
			{"account_number": "12", "account_number_confirm": "12"},
		):
			with self.assertRaises(frappe.ValidationError):
				self.submit(**over)
		self.assertFalse(STORE.rows("Employee Bank Account"))

	def test_device_keys_on_needs_face_id(self):
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			direct_deposit_self_service=1,
			device_keys_enabled=1,
			**self.on_switches(),
		)
		with self.assertRaises(frappe.ValidationError):
			self.submit()
		with mock.patch("erpnext_mcp.device_keys._verify_approver") as verify:
			answer = direct_deposit.submit(WORKER, body(signature="sig"), device="D1", key_bound=True)
		message = verify.call_args[0][2]
		self.assertTrue(message.startswith(f"farmops-direct-deposit|{WORKER_EMPLOYEE}|{CASH_APP}|"))
		self.assertNotIn(ACCOUNT, message)
		ev = frappe.db.get_value("Employee Bank Account", answer["account"], "signing_evidence")
		self.assertEqual(
			frappe.db.get_value("Signing Evidence", ev, "verification_method"), "Face ID (device key)"
		)

	def test_a_second_request_supersedes_the_first(self):
		first = self.submit()["account"]
		self.submit(account_number="555566667777", account_number_confirm="555566667777")
		self.assertEqual(frappe.db.get_value("Employee Bank Account", first, "status"), "Rejected")


class Plaid(DDCase):
	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			direct_deposit_self_service=1,
			direct_deposit_plaid_enabled=1,
			plaid_client_id="cid",
			**self.on_switches(),
		)
		STORE.passwords[("ERPNext MCP Settings", "ERPNext MCP Settings", "plaid_secret")] = "sec"
		self.calls = []
		self.owner = "ANA RAMOS"
		self.ach_account = ACCOUNT
		plaid_link.TRANSPORT = self.fake
		self.addCleanup(lambda: setattr(plaid_link, "TRANSPORT", None))

	def fake(self, url, payload):
		path = url.split(".com", 1)[1]
		self.calls.append(path)
		if path == "/link/token/create":
			assert payload["products"] == ["auth", "identity"]
			return {
				"link_token": "link-1",
				"hosted_link_url": "https://hosted.plaid.com/link/abc",
				"request_id": "r1",
			}
		if path == "/link/token/get":
			return {"link_sessions": [{"results": {"item_add_results": [{"public_token": "public-1"}]}}]}
		if path == "/item/public_token/exchange":
			return {"access_token": "access-1"}
		if path == "/auth/get":
			return {"numbers": {"ach": [{"routing": CASH_APP, "account": self.ach_account}]}}
		if path == "/identity/get":
			return {"accounts": [{"owners": [{"names": [self.owner]}]}]}
		if path == "/item/remove":
			return {}
		return {"error_code": "UNKNOWN"}

	def test_match_verifies_and_the_item_is_removed(self):
		account = self.submit()["account"]
		started = mobile_api.start_bank_verification(account=account)
		self.assertEqual(started["callback_scheme"], "farmops")
		done = mobile_api.finish_bank_verification(account=account)
		self.assertEqual(done["state"], "Verified")
		self.assertIn("/item/remove", self.calls)
		row = frappe.get_doc("Employee Bank Account", account).as_dict()
		self.assertNotIn("access-1", json.dumps(row, default=str))

	def test_another_owner_or_another_account_fails(self):
		self.owner = "SOMEONE ELSE"
		account = self.submit()["account"]
		mobile_api.start_bank_verification(account=account)
		self.assertEqual(mobile_api.finish_bank_verification(account=account)["state"], "Failed")
		self.assertIn("/item/remove", self.calls)

	def test_plaid_off_falls_back_to_the_form(self):
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			direct_deposit_self_service=1,
			**self.on_switches(),
		)
		answer = self.submit()
		self.assertEqual(answer["next"], {"plaid": False, "bank_form": True})
		with self.assertRaises(frappe.ValidationError):
			mobile_api.start_bank_verification(account=answer["account"])


class BankForm(DDCase):
	def pdf(self, text):
		from erpnext_mcp.render.pdf import PdfDocument

		doc = PdfDocument(title="DD", author="Cash App", subject="Direct deposit", footer="")
		doc.paragraph(text)
		return doc.render()

	def upload(self, account, content, name="form.pdf", routing=CASH_APP, number=ACCOUNT):
		return mobile_api.upload_bank_form(
			account=account,
			file_name=name,
			content_base64=base64.b64encode(content).decode(),
			extracted_routing=routing,
			extracted_account=number,
		)

	def test_a_text_pdf_with_the_numbers_verifies(self):
		account = self.submit()["account"]
		try:
			import pypdf  # noqa: F401
		except ImportError:
			self.skipTest("pypdf is an optional package")
		answer = self.upload(account, self.pdf(f"Routing number {CASH_APP} Account number {ACCOUNT}"))
		self.assertEqual(answer["state"], "Verified")
		row = frappe.get_doc("Employee Bank Account", account).as_dict()
		self.assertTrue(row["evidence_sha256"])
		attached = next(f for f in STORE.rows("File") if f.get("attached_to_name") == account)
		self.assertEqual(int(attached["is_private"]), 1)

	def test_a_photo_needs_review_and_a_mismatch_is_refused(self):
		account = self.submit()["account"]
		with self.assertRaises(frappe.ValidationError):
			self.upload(account, b"\xff\xd8\xff", name="form.jpg", number="000011112222")
		self.assertEqual(self.upload(account, b"\xff\xd8\xff", name="form.jpg")["state"], "Needs review")


class ApprovalPrenoteActivation(DDCase):
	def verified(self):
		account = self.submit()["account"]
		frappe.db.set_value("Employee Bank Account", account, "verification_state", "Verified")
		return account

	def test_controls(self):
		account = self.submit()["account"]
		with self.assertRaises(direct_deposit.Refused):
			direct_deposit.approve(account, HR)  # not verified
		frappe.db.set_value("Employee Bank Account", account, "verification_state", "Verified")
		with self.assertRaises(direct_deposit.Refused):
			direct_deposit.approve(account, WORKER)  # not HR, and their own
		set_roles(WORKER, ["HR Manager"])
		with self.assertRaises(direct_deposit.Refused):
			direct_deposit.approve(account, WORKER)  # never your own
		self.assertEqual(direct_deposit.approve(account, HR)["approved_by"], HR)

	def test_prenote_then_hold_then_paid(self):
		ACHTestCase._seed_originator(self)
		STORE.seed("Employee Bank Account", [])
		old = frappe.get_doc(
			{
				"doctype": "Employee Bank Account",
				"employee": WORKER_EMPLOYEE,
				"company": MAIN,
				"status": "Active",
				"bank_name": "Old Bank",
				"routing_number": "021000021",
				"account_number": "11112222",
				"account_number_last_four": "2222",
				"account_type": "Checking",
				"allocation_type": "Full",
				"prenote_sent": 1,
			}
		).insert(ignore_permissions=True)
		account = self.verified()
		self.assertEqual(frappe.db.get_value("Employee Bank Account", account, "replaces"), old.name)
		direct_deposit.approve(account, HR)
		from erpnext_mcp.tools import ach

		ach.generate_prenote_file({"company": MAIN})
		activates = frappe.db.get_value("Employee Bank Account", account, "activates_on")
		self.assertTrue(activates)
		self.assertEqual(direct_deposit.activate_due(), [])  # hold not passed
		frappe.db.set_value("Employee Bank Account", account, "activates_on", "2020-01-01")
		self.assertEqual(direct_deposit.activate_due(), [account])
		self.assertEqual(frappe.db.get_value("Employee Bank Account", account, "status"), "Active")
		self.assertEqual(frappe.db.get_value("Employee Bank Account", old.name, "status"), "Inactive")

	def test_a_returned_prenote_never_activates(self):
		account = self.verified()
		direct_deposit.approve(account, HR)
		frappe.db.set_value(
			"Employee Bank Account", account, {"prenote_sent": 1, "activates_on": "2020-01-01"}
		)
		direct_deposit.prenote_returned(account, HR)
		self.assertEqual(direct_deposit.activate_due(), [])
		self.assertEqual(frappe.db.get_value("Employee Bank Account", account, "status"), "Rejected")

	def test_nacha_never_pays_a_pending_account(self):
		from erpnext_mcp.tools import ach

		account = self.verified()
		self.assertFalse(
			[a for a in ach._employee_accounts(WORKER_EMPLOYEE, active_only=True) if a["name"] == account]
		)


class Masked(DDCase):
	def test_the_hr_list_and_no_tool_approves(self):
		from erpnext_mcp import registry

		self.submit()
		text = json.dumps(direct_deposit.changes())
		self.assertNotIn(ACCOUNT, text)
		self.assertNotIn(CASH_APP, text)
		self.assertFalse(
			[n for n in registry.TOOLS if "direct_deposit" in n and n != "list_direct_deposit_changes"]
		)

	def test_aba(self):
		for good in (CASH_APP, "073923033", "021000021"):
			self.assertTrue(direct_deposit.aba_valid(good), good)
		self.assertFalse(direct_deposit.aba_valid("123456789"))
		self.assertTrue(direct_deposit.name_matches("Ana María Ramos", ["ANA M RAMOS"]))
		self.assertFalse(direct_deposit.name_matches("Ana Ramos", ["RAMOS HOLDINGS LLC"]))


class TheMessageThePhoneSigns(DDCase):
	def test_byte_for_byte_with_the_app(self):
		"""FarmOpsKit DirectDepositTests pins the same string; the two must not drift."""
		self.assertEqual(
			direct_deposit.signed_message("EMP-ANA", "041215663", "123456789012", "Checking"),
			"farmops-direct-deposit|EMP-ANA|041215663|"
			"2a33349e7e606a8ad2e30e3c84521f9377450cf09083e162e0a9b1480ce0f972|Checking",
		)
