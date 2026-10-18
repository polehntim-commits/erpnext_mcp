# SPDX-License-Identifier: MIT
"""Frappe 15.123.1 / ERPNext 15.123.0: two new permission checks, met after this app's own gates. v0.336.0.

FIVE CLAIMS.

1. `TheDoubleModels15123` — the harness now refuses what the new base image refuses: a File attached to a
   record the account may not write, and a buying document whose line Item the account may not read. Without
   this every claim below would be green against the unfixed code.
2. `FileAttachments` — `insert_attachment` / `attach_bytes` keep Frappe's check by default and skip it only when
   the caller says its own gate has run; the phone's brokered attach and a receiving draft get through.
3. `EveryCallerIsClassified` — each call site is listed with its decision; a new one fails until it is.
4. `ItemsReadable` — exactly the named Items, only inside the `with`, flag restored after.
5. `PartyAccountCurrency` — the supplier's account currency is asked once, as Administrator, user restored.
"""

import ast
import pathlib
import unittest

import frappe

from erpnext_mcp import compat, receiving
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import artifacts
from erpnext_mcp.tools import files as file_tools

from .harness import STORE, set_roles
from .test_api_mobile import MAIN, WORKER
from .test_receiving import CAPTAN, ReceivingCase

APP = pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp"

#: Every call of the two attach funnels, by (file, enclosing function) -> parent_authorized.
#: True means the function has run its own gate before the attach (named in the comment); False means it
#: has not, and Frappe's own write check on the parent stays the answer.
AUDITED = {
	# insert_attachment
	("api/mobile.py", "attach_asset_document"): True,  # require_scope + _asset_for_task + check_attachable
	("epa_ppls.py", "attach_label_pdf"): "passthrough",  # the phone route passes True, masters.py False
	("tools/evidence.py", "_attach_the_procedure"): True,  # the policy was written with ignore_permissions
	("tools/files.py", "_attach"): True,  # has_permission(write), or the brokered caller's stricter gate
	("tools/governance.py", "file_governance_document"): False,  # a plain insert: Frappe decides
	("tools/governance.py", "regenerate_governance_document_pdf"): True,  # has_permission(write) itself
	("tools/signatures.py", "collect_form_signature"): True,  # _require_write + _require_entity
	("tools/signed_documents.py", "_file_on_employee"): True,  # after seal_signed_document's write gate
	("tools/uploads.py", "commit_staged_file"): True,  # _commit_target -> check_attachable (write)
	("tools/wallet.py", "_apple"): True,  # require_hr_role + require_company_scope
	# attach_bytes
	("badge_photo.py", "set_photo"): True,  # the task's own check / badges.require_may
	("card_print.py", "request"): True,  # require_requester; the job is written with ignore_permissions
	("card_print.py", "request_back"): True,
	("card_print.py", "download"): True,
	("direct_deposit.py", "upload_form"): True,  # the phone route's require_scope, own account only
	("employee_file.py", "packet"): True,  # only_for(PACKET_ROLES) + build()'s NotAllowed
	("tools/ach.py", "_deliver"): False,  # MCP only, no gate of its own
	("tools/app_feedback.py", "submit_app_feedback"): True,  # _require; the feedback row is its own
	("tools/auditpacket.py", "generate_audit_packet"): True,  # its archive is written with ignore_permissions
	("tools/badges.py", "_attach_to_employee"): True,  # hiring role + company scope / the Desk's write check
	("tools/garnishments.py", "render_garnishment_response"): True,  # require_hr_role + company scope
	("tools/i9.py", "render_i9_pdf"): True,  # the phone routes' require_hr_role / hiring role
	(
		"tools/investment_report.py",
		"generate_quarterly_investment_report",
	): False,  # plain insert: Frappe decides
	("tools/mobile.py", "_archive_card"): True,  # its archive is written with ignore_permissions
	("tools/payroll.py", "render_pay_stub"): True,  # require_hr_role, or the worker's own scoped stub
	("tools/payroll.py", "_render_earnings_explanation"): True,
	("tools/signed_documents.py", "seal_signed_document"): True,  # hiring role + require_scoped_doc
	("tools/tax.py", "generate_1099_prefill"): False,  # plain insert: Frappe decides
	("tools/taxforms.py", "render_tax_form_pdf"): False,
	("tools/taxforms.py", "bulk_render_tax_form_pdfs"): False,
	("tools/training_sessions.py", "render_training_sign_in_sheet"): True,  # shift role + company scope
	("tools/w4.py", "render_w4_pdf"): True,  # the phone route's require_hr_role
	("tools/w4.py", "_render_or_w4"): True,
}


def _calls():
	"""{(file, function): {decision, ...}} for every call of the two funnels in the app."""
	found: dict = {}
	for path in sorted(APP.rglob("*.py")):
		rel = path.relative_to(APP).as_posix()
		# Only what Python can import: `.gitignore` keeps Finder's "mobile 2.py" copies out of git, not off disk.
		if rel.startswith("tests/") or not all(part.isidentifier() for part in rel[:-3].split("/")):
			continue
		tree = ast.parse(path.read_text())
		for func in ast.walk(tree):
			if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue
			for node in ast.walk(func):
				if not isinstance(node, ast.Call):
					continue
				callee = (
					node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
				)
				if callee not in ("insert_attachment", "attach_bytes"):
					continue
				keyword = next((k for k in node.keywords if k.arg == "parent_authorized"), None)
				if keyword is None:
					decision = False
				elif isinstance(keyword.value, ast.Constant):
					decision = bool(keyword.value.value)
				else:
					decision = "passthrough"
				found.setdefault((rel, func.name), set()).add(decision)
	# A nested function's calls are also seen from its parent; keep the innermost name only.
	return found


class TheDoubleModels15123(unittest.TestCase):
	"""Proof the simulation bites, so the passes below are about the fix and not the double."""

	def setUp(self):
		STORE.reset()
		STORE.seed(
			"Item", [{"name": "WIDGET", "item_code": "WIDGET", "item_name": "Widget", "stock_uom": "Nos"}]
		)
		STORE.commit()

	def test_a_file_on_a_record_the_account_may_not_write_is_refused(self):
		STORE.denied_permissions.add(("Item", "write"))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "a.pdf",
					"content": b"%PDF",
					"attached_to_doctype": "Item",
					"attached_to_name": "WIDGET",
				}
			).insert()
		attached = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "b.pdf",
				"content": b"%PDF",
				"attached_to_doctype": "Item",
				"attached_to_name": "WIDGET",
			}
		)
		attached.insert(ignore_permissions=True)
		self.assertTrue(attached.name)

	def test_a_receipt_line_whose_item_the_account_may_not_read_is_refused(self):
		STORE.denied_permissions.add(("Item", "read"))
		pr = frappe.new_doc("Purchase Receipt")
		pr.append("items", {"item_code": "WIDGET", "qty": 1, "rate": 1})
		pr.flags.ignore_permissions = True  # the PARENT's flag does not reach the Item — as in ERPNext 15.123
		with self.assertRaises(frappe.PermissionError):
			pr.insert()


class FileAttachments(unittest.TestCase):
	def setUp(self):
		STORE.reset()
		STORE.seed(
			"Item", [{"name": "WIDGET", "item_code": "WIDGET", "item_name": "Widget", "stock_uom": "Nos"}]
		)
		STORE.commit()
		STORE.denied_permissions.add(("Item", "write"))

	def test_insert_attachment_keeps_frappes_check_by_default(self):
		with self.assertRaises(frappe.PermissionError):
			file_tools.insert_attachment("a.pdf", b"%PDF", doctype="Item", name="WIDGET")

	def test_insert_attachment_skips_it_only_when_the_caller_has_gated(self):
		doc = file_tools.insert_attachment(
			"a.pdf", b"%PDF", doctype="Item", name="WIDGET", parent_authorized=True
		)
		self.assertEqual(doc.attached_to_name, "WIDGET")

	def test_attach_bytes_the_same(self):
		with self.assertRaises(frappe.PermissionError):
			artifacts.attach_bytes("Item", "WIDGET", "a.pdf", b"%PDF")
		doc = artifacts.attach_bytes("Item", "WIDGET", "b.pdf", b"%PDF", parent_authorized=True)
		self.assertEqual(doc.attached_to_name, "WIDGET")

	def test_the_flag_is_never_an_argument_a_caller_can_send(self):
		import inspect

		for function in (file_tools.insert_attachment, artifacts.attach_bytes):
			parameter = inspect.signature(function).parameters["parent_authorized"]
			self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
			self.assertIs(parameter.default, False)
		for name in ("attach_file_to_document", "commit_staged_file"):
			from erpnext_mcp import registry

			self.assertNotIn("parent_authorized", registry.TOOLS[name]["inputSchema"]["properties"])


class EveryCallerIsClassified(unittest.TestCase):
	def test_each_call_site_carries_its_audited_decision(self):
		found = _calls()
		unclassified = sorted(set(found) - set(AUDITED))
		self.assertEqual(
			unclassified, [], "a new File attach: decide whether its own gate runs first, then add it here"
		)
		for site, wanted in AUDITED.items():
			with self.subTest(site=site):
				self.assertIn(site, found, "listed but no longer calls the funnel — drop it from AUDITED")
				self.assertEqual(found[site], {wanted})


class ItemsReadable(unittest.TestCase):
	def setUp(self):
		STORE.reset()
		STORE.seed(
			"Item", [{"name": c, "item_code": c, "item_name": c, "stock_uom": "Nos"} for c in ("A", "B", "C")]
		)
		STORE.commit()
		STORE.denied_permissions.add(("Item", "read"))

	def test_only_the_named_items_inside_the_block(self):
		with compat.items_readable(["A", "B", "A", "", None, "NOPE"]):
			self.assertTrue(frappe.get_cached_doc("Item", "A").has_permission("read"))
			self.assertTrue(frappe.get_cached_doc("Item", "B").has_permission("read"))
			self.assertFalse(frappe.get_cached_doc("Item", "C").has_permission("read"))
		self.assertFalse(frappe.get_cached_doc("Item", "A").has_permission("read"))

	def test_restored_after_an_error_and_never_unflags_what_it_did_not_flag(self):
		pre = frappe.get_cached_doc("Item", "C")
		pre.flags.ignore_permissions = True
		with self.assertRaises(RuntimeError), compat.items_readable(["A", "C"]):
			raise RuntimeError
		self.assertFalse(frappe.get_cached_doc("Item", "A").has_permission("read"))
		self.assertTrue(frappe.get_cached_doc("Item", "C").flags.get("ignore_permissions"))


class ThePhoneUnder15123(ReceivingCase):
	"""The two reported failures, end to end through the phone routes, with the 15.123 refusals switched on."""

	def test_a_farm_manager_without_item_read_drafts_the_delivery_receipt(self):
		self.turn_on()
		name = receiving.open_intake(MAIN, purchase_order=self.po, source="ticket_photo")
		STORE.commit()
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be(WORKER)
		mobile_api.submit_delivery_lines(intake=name, lines='[{"item_text": "Captan 80 WDG", "qty": 50}]')
		STORE.commit()
		STORE.denied_permissions.add(("Item", "read"))
		self.addCleanup(STORE.denied_permissions.discard, ("Item", "read"))
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be(WORKER)
		receipt = mobile_api.draft_delivery_receipt(intake=name)["purchase_receipt"]
		STORE.commit()
		self.assertEqual(frappe.get_doc("Purchase Receipt", receipt).get("items")[0].get("item_code"), CAPTAN)
		self.assertFalse(frappe.get_cached_doc("Item", CAPTAN).has_permission("read"), "flag cleared after")


class PartyAccountCurrency(unittest.TestCase):
	"""ERPNext 15.123's Account check is answered by priming ERPNext's own per-request memo, as Administrator,
	for one call. The double has no ERPNext, so a stand-in `erpnext.accounts.party` records who asked."""

	def setUp(self):
		import sys
		import types

		self.seen = []
		party = types.ModuleType("erpnext.accounts.party")

		def get_party_account_currency(party_type, party_name, company):
			self.seen.append((frappe.session.user, party_type, party_name, company))
			if party_name == "BOOM":
				raise RuntimeError("ERPNext refused")
			return "USD"

		party.get_party_account_currency = get_party_account_currency
		modules = {
			"erpnext": types.ModuleType("erpnext"),
			"erpnext.accounts": types.ModuleType("erpnext.accounts"),
			"erpnext.accounts.party": party,
		}
		saved = {name: sys.modules.get(name) for name in modules}
		sys.modules.update(modules)
		self.addCleanup(
			lambda: [
				sys.modules.pop(n) if saved[n] is None else sys.modules.__setitem__(n, saved[n])
				for n in modules
			]
		)
		frappe.local.session.user = "limited@example.com"
		self.addCleanup(setattr, frappe.local.session, "user", "Administrator")

	def test_asked_once_as_administrator_and_the_user_is_restored(self):
		compat.prime_party_account_currency("Supplier", "Chem Co", "Compat Farm")
		self.assertEqual(self.seen, [("Administrator", "Supplier", "Chem Co", "Compat Farm")])
		self.assertEqual(frappe.session.user, "limited@example.com")

	def test_restored_and_silent_when_erpnext_refuses(self):
		compat.prime_party_account_currency("Supplier", "BOOM", "Compat Farm")
		self.assertEqual(frappe.session.user, "limited@example.com")

	def test_nothing_asked_without_a_party(self):
		compat.prime_party_account_currency("Supplier", "", "Compat Farm")
		self.assertEqual(self.seen, [])


if __name__ == "__main__":
	unittest.main()
