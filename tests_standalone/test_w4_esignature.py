# SPDX-License-Identifier: MIT
"""E-signed W-4 / OR-W-4 to the IRS standard, and Oregon's 8% with no OR-W-4. v0.308.0.

26 CFR 31.3402(f)(5)-1(c): the signer is authenticated; the perjury statement comes immediately before the signature
(recorded on the evidence); the signature is the final entry (the elections lock); reads are logged; a hard copy is
reproducible on the official form. Oregon has no explicit rule (ORS 84.019 / 84.052), so the "awaiting DOR written
confirmation" flag stays. OAR 150-316-0234: no OR-W-4 — a 2020+ federal W-4 included — is a flat 8%.
"""

import base64

from erpnext_mcp.tools import w4

from .harness import STORE, frappe, register_doctype
from .test_or_w4_form import FEDERAL, ON, OrW4Case
from .test_payroll_integration import WORKER

CAPTURE = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"signature").decode()
OR_STATEMENT = "Under penalty of false swearing, I declare the information provided is true, correct, and complete."


class Case(OrW4Case):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, "allow_collect_form_signature": 1})

	def sign(self, name, field="or_signature", **extra):
		return self.tool_result("collect_form_signature", {"doctype": "W-4 Form", "name": name, "field": field,
		                                                   "signature_base64": CAPTURE, **extra})

	def tool_result(self, name, args):
		result = self.tool(name, args)
		STORE.commit()
		return result

	def text(self, result):
		return result["content"][0]["text"]


class TheSignerIsAuthenticated(Case):
	def test_an_unverified_signature_is_refused_on_both_forms(self):
		name = self.or_w4(or_filing_status="Single", or_allowances=1)["name"]
		got = self.sign(name)
		self.assertTrue(got.get("isError"))
		self.assertIn("31.3402(f)(5)-1(c)", self.text(got))
		fed = self.tool_data("submit_w4", {**FEDERAL})["name"]
		self.assertTrue(self.sign(fed, field="signature").get("isError"))
		self.assertFalse(frappe.db.get_value("W-4 Form", name, "or_signature"))

	def test_an_in_person_id_check_by_a_hiring_user_is_accepted_and_the_statement_recorded(self):
		name = self.or_w4(or_filing_status="Single", or_allowances=1)["name"]
		got = self.sign(name, verification_method="Employee ID")
		self.assertFalse(got.get("isError"), self.text(got))
		row = STORE.rows("Signing Evidence")[-1]
		self.assertEqual((row["verification_method"], row["attestation"]), ("Employee ID", OR_STATEMENT))

	def test_a_photograph_alone_is_not_an_authentication(self):
		name = self.or_w4(or_filing_status="Single", or_allowances=1)["name"]
		got = self.sign(name, verification_method="Photo")
		self.assertTrue(got.get("isError"))
		self.assertIn("'Photo' is not one of them", self.text(got))

	def test_an_id_check_needs_a_hiring_or_hr_user(self):
		from .harness import ROLES

		name = self.or_w4(or_filing_status="Single", or_allowances=1)["name"]
		saved = list(ROLES.get("Administrator", []))
		self.addCleanup(lambda: ROLES.__setitem__("Administrator", saved))
		ROLES["Administrator"] = ["Field Worker"]
		got = self.sign(name, verification_method="Employee ID")
		self.assertTrue(got.get("isError"))


class TheSignatureIsTheFinalEntry(Case):
	def test_the_oregon_elections_lock_once_signed(self):
		name = self.or_w4(or_filing_status="Single", or_allowances=1)["name"]
		self.sign(name, verification_method="Employee ID")
		doc = frappe.get_doc("W-4 Form", name)
		doc.or_allowances = 3
		with self.assertRaises(Exception) as caught:
			doc.save(ignore_permissions=True)
		self.assertIn("final entry", str(caught.exception))
		doc = frappe.get_doc("W-4 Form", name)
		doc.or_forwarded_on = "2026-10-10"  # the employer's own bookkeeping is not an election
		doc.save(ignore_permissions=True)

	def test_an_unsigned_form_can_still_be_corrected(self):
		name = self.or_w4(or_filing_status="Single", or_allowances=1)["name"]
		doc = frappe.get_doc("W-4 Form", name)
		doc.or_allowances = 2
		doc.save(ignore_permissions=True)


class ReadsAreLogged(Case):
	def test_reading_a_signed_form_writes_an_access_log_row(self):
		register_doctype("Access Log", [{"fieldname": f} for f in ("export_from", "reference_document", "method",
		                                                           "user", "file_type")])
		self.tool_data("submit_w4", {**FEDERAL})
		fed = w4.federal_row_name(WORKER)
		self.sign(fed, field="signature", verification_method="Employee ID")
		self.tool_data("get_w4", {"employee": WORKER})
		rows = [r for r in STORE.rows("Access Log") if r.get("reference_document") == fed]
		self.assertEqual([r["method"] for r in rows], ["get_w4"])


class TheStatusSaysWhereItStands(Case):
	def test_awaiting_dor_written_confirmation_with_the_standard(self):
		got = w4.esignature_status()
		self.assertEqual((got["confirmed"], got["status"]), (False, "awaiting DOR written confirmation"))
		self.assertEqual(got["standard"], "26 CFR 31.3402(f)(5)-1(c)")
		self.assertIn("ORS 84.052", got["state_law"])
		self.assertTrue(all(got["requirements"].values()))


class EightPercentWithoutAnOrW4(Case):
	def test_a_2020_plus_federal_w4_alone_is_eight_percent_not_single_zero(self):
		self.tool_data("submit_w4", {**FEDERAL})
		STORE.commit()
		_run, income = self.income()
		self.assertEqual((income["method"], income["rate"]), ("flat", 8.0))
		self.assertIn("OAR 150-316-0234", income["rule"])
		self.assertEqual(income["based_on"], "2020+ federal W-4, no OR-W-4")

	def test_a_pre_2020_federal_w4_stops_counting_once_a_new_federal_w4_is_filed(self):
		old = self.or_w4(or_source="Pre-2020 federal W-4", or_filing_status="Married", or_allowances=4)["name"]
		frappe.db.set_value("W-4 Form", old, "effective_date", "2019-03-01")
		STORE.commit()
		new = self.tool_data("submit_w4", {**FEDERAL})["name"]
		frappe.db.set_value("W-4 Form", new, "effective_date", "2025-02-01")
		STORE.commit()
		_run, income = self.income()
		self.assertEqual((income["method"], income["rate"]), ("flat", 8.0))
		self.assertIn("superseded", income["based_on"])
