# SPDX-License-Identifier: MIT
"""Who may sign a federal employment form, and what happens when nobody has said.

SIX CLAIMS.

1. `AnEmptyRosterIsUnrestricted` — the migration story. A site that has never
   added a signer behaves exactly as it did before v0.48.0, `submit_i9_section_2`
   included, and says so rather than leaving a caller to infer it.
2. `TheFirstRowIsTheSwitch` — adding one signer turns enforcement on for the
   whole site, and the tool that does it says so in its own result.
3. `TheRoster` — add, update, deactivate, and the refusals each of them owns.
4. `NothingIsEverDeleted` — deactivation keeps the row, and the reason is a
   question an inspection asks about a form signed two seasons ago.
5. `Section2TakesTheNameOffTheRoster` — the point of the whole feature: with a
   roster configured, `verifier_name` stops being whatever the client sent.
6. `SigningOnBehalfOfSomebodyElse` — an authorized person may file for another
   authorized person, and for nobody else.
"""

import frappe

from erpnext_mcp.tools import signers

from .harness import ROLES, STORE, set_roles
from .test_i9 import I9TestCase

#: The four tools this file is about.
SIGNER_TOOLS_ON = {
	f"allow_{name}": 1
	for name in (
		"list_authorized_signers",
		"add_authorized_signer",
		"update_authorized_signer",
		"remove_authorized_signer",
	)
}

ANA = "ana@example.test"
LUIS = "luis@example.test"
PICKER = "picker@example.test"


class SignerTestCase(I9TestCase):
	"""`I9TestCase`'s site, plus three accounts and the four signer tools on.

	Built on the I-9 fixture rather than beside it because the roster lives on
	I-9 Settings and the claim that matters most — Section 2 taking the name off
	the roster — needs a real I-9 to sign.
	"""

	acting_user = ""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **dict(self._i9_switches(), **SIGNER_TOOLS_ON))
		STORE.seed(
			"User",
			[
				{"name": ANA, "enabled": 1, "full_name": "Ana Ramos"},
				{"name": LUIS, "enabled": 1, "full_name": "Luis Ortega"},
				{"name": PICKER, "enabled": 1, "full_name": "Pat Picker"},
			],
		)
		# v0.94.0. THE TWO SIGNERS HOLD A HIRING ROLE AND THE PICKER DOES NOT, and
		# that split is the fixture's whole subject rather than housekeeping.
		# `create_i9_form` and `submit_i9_section_1` now take `require_hiring_role`
		# (F2a), so raising the form these tests then sign needs one — while the
		# ROSTER is a per-person designation that no role grants and none
		# substitutes for. Keeping the two facts separate here is what lets the
		# tests below prove they are separate: Ana can raise a form because of her
		# role and may sign Section 2 because of the roster, and losing either one
		# stops her at a different step with a different sentence.
		#
		# RESTORED AFTERWARDS, because `ROLES` is module-global and outlives this
		# file. Left in place, Ana stayed a Foreman for every module after this one,
		# and `test_training_session_documents` — whose picker is also
		# ana@example.test — watched `SHIFT_ROLES` admit a Field Worker.
		roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles, roles_before)
		for account in (ANA, LUIS):
			set_roles(account, ["Foreman"])
		set_roles(PICKER, ["Field Worker"])
		self.addCleanup(self._restore_session, frappe.session.user)

	def _restore_roles(self, before):
		ROLES.clear()
		ROLES.update(before)

	def _i9_switches(self) -> dict:
		from .test_i9 import I9_TOOLS_ON

		return I9_TOOLS_ON

	def _restore_session(self, user):
		frappe.session.user = user

	def as_user(self, user: str) -> None:
		"""Make every tool call from here on come from this account.

		The roster matches on the CALLING ACCOUNT, which is the whole point of
		it — so a test that never changed accounts would be testing one row
		against itself.

		RE-APPLIED PER CALL, in `tool` below, because it has to be. `mcp.handle`
		captures the caller and then runs `frappe.set_user(effective_user())`, so
		a call leaves the session as the MCP System User and the next one would
		arrive as it. On a real site each request re-authenticates; here that is
		one line in `tool`.
		"""
		self.acting_user = user

	def tool(self, name, arguments=None, **kwargs):
		if getattr(self, "acting_user", ""):
			frappe.session.user = self.acting_user
		return super().tool(name, arguments, **kwargs)

	def add(self, user=ANA, **overrides) -> dict:
		payload = {"user": user, "full_name": "Ana Ramos", "title": "HR Manager"}
		payload.update(overrides)
		return self.tool_data("add_authorized_signer", payload)

	def _section_1(self, **overrides) -> dict:
		"""A Draft I-9 with the worker's half filled in — Section 2's precondition."""
		self._create_draft()
		return self._submit_section_1(**overrides)

	def _section_2(self, **overrides) -> dict:
		return self._submit_section_2(**overrides)


# ── 1 ─────────────────────────────────────────────────────────────────────────
class AnEmptyRosterIsUnrestricted(SignerTestCase):
	"""The migration story, and the reason the fallback is not a loophole."""

	def test_the_roster_reports_itself_unconfigured(self):
		data = self.tool_data("list_authorized_signers", {})
		self.assertFalse(data["configured"])
		self.assertEqual(data["count"], 0)
		self.assertIn("UNRESTRICTED", data["note"])

	def test_the_helper_says_unconfigured_rather_than_refusing(self):
		answer = signers.get_authorized_signer(ANA, "I-9")
		self.assertFalse(answer["configured"])
		self.assertEqual(answer["full_name"], "")

	def test_section_2_still_takes_the_name_it_is_given(self):
		"""Every site on the day it upgrades. This is the pre-v0.48.0 behaviour."""
		self._section_1()
		data = self._section_2(verifier_name="Whoever Was There")
		self.assertEqual(data["verifier_name"], "Whoever Was There")
		self.assertFalse(data["signer_roster_enforced"])

	def test_section_2_still_requires_a_name_when_there_is_no_roster(self):
		self._section_1()
		message = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "U.S. Passport",
				"verification_date": str(_today()),
			},
		)
		self.assertIn("verifier_name is required", message)


# ── 2 ─────────────────────────────────────────────────────────────────────────
class TheFirstRowIsTheSwitch(SignerTestCase):
	"""Adding one signer turns enforcement on, and the tool says so."""

	def test_the_first_signer_reports_that_it_flipped_the_site(self):
		data = self.add()
		self.assertTrue(data["first_signer"])
		self.assertIn("FIRST signer", data["note"])

	def test_the_second_signer_does_not(self):
		self.add()
		data = self.add(LUIS, full_name="Luis Ortega")
		self.assertFalse(data["first_signer"])
		self.assertNotIn("note", data)

	def test_an_account_not_on_the_roster_can_no_longer_sign(self):
		"""THE FORM IS RAISED BY SOMEBODY WHO MAY RAISE ONE, and only the
		SIGNATURE is attempted by the account off the roster.

		v0.94.0 made that separation necessary and it makes the test sharper. The
		picker holds no hiring role, so raising the I-9 as the picker would now
		fail at `create_i9_form` — and this test would then be passing on the
		wrong refusal, proving the hiring gate while claiming to prove the roster.
		Ana raises the form because of her ROLE; the picker is refused at Section
		2 because of the ROSTER, which is a designation on a person that no role
		grants.
		"""
		self.add()
		self.as_user(ANA)
		self._section_1()
		self.as_user(PICKER)
		message = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "U.S. Passport",
				"verifier_name": "Pat Picker",
				"verification_date": str(_today()),
			},
		)
		self.assertIn("not an authorized signer", message)
		self.assertIn("Nothing was changed", message)

	def test_a_roster_whose_every_row_is_inactive_is_still_a_roster(self):
		"""Deactivating the last signer must not silently reopen the door."""
		self.add()
		self.tool_data("remove_authorized_signer", {"user": ANA})
		self.assertTrue(signers.roster_is_configured())
		with self.assertRaises(Exception):
			signers.get_authorized_signer(ANA, "I-9")


# ── 3 ─────────────────────────────────────────────────────────────────────────
class TheRoster(SignerTestCase):
	"""Add, update, deactivate — and each refusal that belongs to one of them."""

	def test_a_signer_is_added_with_both_forms_by_default(self):
		data = self.add()
		self.assertEqual(data["signer"]["full_name"], "Ana Ramos")
		self.assertEqual(data["signer"]["title"], "HR Manager")
		self.assertTrue(data["signer"]["can_sign_i9"])
		self.assertTrue(data["signer"]["can_sign_w4"])
		self.assertTrue(data["signer"]["active"])

	def test_the_printed_name_falls_back_to_the_accounts_own(self):
		data = self.tool_data("add_authorized_signer", {"user": LUIS})
		self.assertEqual(data["signer"]["full_name"], "Luis Ortega")

	def test_an_account_that_is_not_on_the_site_is_refused(self):
		message = self.tool_error("add_authorized_signer", {"user": "ghost@example.test"})
		self.assertIn("no User called", message)

	def test_a_second_row_for_one_account_is_refused(self):
		self.add()
		message = self.tool_error("add_authorized_signer", {"user": ANA})
		self.assertIn("already on the authorized signer roster", message)

	def test_one_form_can_be_granted_without_the_other(self):
		self.add(LUIS, full_name="Luis Ortega", can_sign_i9=False)
		answer = signers.get_authorized_signer(LUIS, "W-4")
		self.assertEqual(answer["full_name"], "Luis Ortega")
		with self.assertRaises(Exception):
			signers.get_authorized_signer(LUIS, "I-9")

	def test_the_refusal_for_one_form_names_the_other(self):
		self.add(LUIS, full_name="Luis Ortega", can_sign_i9=False)
		try:
			signers.get_authorized_signer(LUIS, "I-9")
		except Exception as error:
			self.assertIn("can_sign_i9 is off", str(error))
			self.assertIn("W-4", str(error))

	def test_update_changes_the_printed_name(self):
		self.add()
		data = self.tool_data("update_authorized_signer", {"user": ANA, "full_name": "Ana Ramos-Ortega"})
		self.assertEqual(data["signer"]["full_name"], "Ana Ramos-Ortega")
		self.assertEqual(data["updated"], ["full_name"])

	def test_update_with_no_fields_is_refused(self):
		self.add()
		message = self.tool_error("update_authorized_signer", {"user": ANA})
		self.assertIn("no fields to update", message)

	def test_update_of_somebody_not_on_the_roster_is_refused(self):
		message = self.tool_error("update_authorized_signer", {"user": ANA, "title": "Owner"})
		self.assertIn("not on the authorized signer roster", message)

	def test_a_deactivated_signer_is_reactivated_through_update(self):
		self.add()
		self.tool_data("remove_authorized_signer", {"user": ANA})
		self.tool_data("update_authorized_signer", {"user": ANA, "active": True})
		self.assertTrue(signers.get_authorized_signer(ANA, "I-9")["configured"])

	def test_deactivating_twice_is_refused_rather_than_silently_repeated(self):
		self.add()
		self.tool_data("remove_authorized_signer", {"user": ANA})
		message = self.tool_error("remove_authorized_signer", {"user": ANA})
		self.assertIn("already inactive", message)

	def test_the_last_deactivation_warns_that_nobody_can_sign(self):
		self.add()
		data = self.tool_data("remove_authorized_signer", {"user": ANA})
		self.assertEqual(data["remaining_active"], 0)
		self.assertIn("NO ACTIVE SIGNERS REMAIN", data["warning"])

	def test_listing_filters_by_form_and_by_active(self):
		self.add()
		self.add(LUIS, full_name="Luis Ortega", can_sign_i9=False)
		self.tool_data("remove_authorized_signer", {"user": ANA})

		everything = self.tool_data("list_authorized_signers", {})
		self.assertEqual(everything["count"], 2)
		self.assertEqual(everything["active_count"], 1)
		self.assertEqual(everything["i9_signers"], 0)
		self.assertEqual(everything["w4_signers"], 1)

		live = self.tool_data("list_authorized_signers", {"include_inactive": False})
		self.assertEqual([row["user"] for row in live["signers"]], [LUIS])

		for_i9 = self.tool_data("list_authorized_signers", {"form_type": "i9"})
		self.assertEqual([row["user"] for row in for_i9["signers"]], [ANA])

	def test_a_form_type_the_roster_does_not_cover_is_refused_by_name(self):
		message = self.tool_error("list_authorized_signers", {"form_type": "W-2"})
		self.assertIn("must be 'I-9' or 'W-4'", message)


# ── 4 ─────────────────────────────────────────────────────────────────────────
class NothingIsEverDeleted(SignerTestCase):
	"""The row outlives the authorisation, and there is no tool that drops it."""

	def test_deactivation_keeps_the_row(self):
		self.add()
		data = self.tool_data("remove_authorized_signer", {"user": ANA})
		self.assertFalse(data["deleted"])
		rows = self.tool_data("list_authorized_signers", {})["signers"]
		self.assertEqual([row["user"] for row in rows], [ANA])
		self.assertFalse(rows[0]["active"])

	def test_there_is_no_delete_tool_in_the_catalogue(self):
		"""A form signed last season names whoever was authorised last season."""
		from erpnext_mcp import registry

		self.assertNotIn("delete_authorized_signer", registry.TOOLS)


# ── 5 ─────────────────────────────────────────────────────────────────────────
class Section2TakesTheNameOffTheRoster(SignerTestCase):
	"""The point of the feature: `verifier_name` stops being a client's string."""

	def setUp(self):
		super().setUp()
		self.add()
		self.as_user(ANA)
		self._section_1()

	def test_the_name_and_title_come_from_the_roster_row(self):
		data = self._section_2(verifier_name=None, verifier_title=None)
		self.assertEqual(data["verifier_name"], "Ana Ramos")
		self.assertEqual(data["verifier_title"], "HR Manager")
		self.assertTrue(data["signer_roster_enforced"])

	def test_a_client_that_sends_the_wrong_name_does_not_get_it_on_the_form(self):
		message = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "U.S. Passport",
				"verifier_name": "Somebody Else Entirely",
				"verification_date": str(_today()),
			},
		)
		self.assertIn("not an active authorized signer", message)

	def test_a_title_may_still_be_corrected_on_one_form(self):
		"""A title authorises nothing, so an override needs no roster edit."""
		data = self._section_2(verifier_name=None, verifier_title="Orchard Manager")
		self.assertEqual(data["verifier_name"], "Ana Ramos")
		self.assertEqual(data["verifier_title"], "Orchard Manager")

	def test_the_audit_row_records_that_a_roster_was_enforced(self):
		self._section_2(verifier_name=None)
		rows = self.tool_data("get_i9_audit_log", {"employee": "HR-EMP-00001"})["entries"]
		signed = next(row for row in rows if row["action"] == "Section 2 Signed")
		self.assertIn('"signer_roster": true', signed["details"])

	def test_nothing_is_written_when_the_signer_is_refused(self):
		"""The check runs before the save, so a refusal leaves a Section 1 form."""
		self.as_user(PICKER)
		self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "U.S. Passport",
				"verification_date": str(_today()),
			},
		)
		form = self.tool_data("get_i9_form", {"employee": "HR-EMP-00001"})
		self.assertEqual(form["status"], "Section 1 Complete")
		self.assertFalse(form.get("verifier_name"))


# ── 6 ─────────────────────────────────────────────────────────────────────────
class SigningOnBehalfOfSomebodyElse(SignerTestCase):
	"""The foreman examined the documents; the office files the form."""

	def setUp(self):
		super().setUp()
		self.add()
		self.add(LUIS, full_name="Luis Ortega", title="Foreman")
		self.as_user(ANA)
		self._section_1()

	def test_one_authorized_signer_may_file_for_another(self):
		data = self._section_2(verifier_name="Luis Ortega")
		self.assertEqual(data["verifier_name"], "Luis Ortega")
		self.assertEqual(data["verifier_title"], "Foreman")

	def test_the_audit_row_names_who_it_was_filed_for(self):
		self._section_2(verifier_name="Luis Ortega")
		rows = self.tool_data("get_i9_audit_log", {"employee": "HR-EMP-00001"})["entries"]
		signed = next(row for row in rows if row["action"] == "Section 2 Signed")
		self.assertIn(LUIS, signed["details"])

	def test_the_match_is_not_case_sensitive(self):
		data = self._section_2(verifier_name="luis ortega")
		self.assertEqual(data["verifier_name"], "Luis Ortega")

	def test_a_deactivated_signers_name_cannot_be_borrowed(self):
		self.tool_data("remove_authorized_signer", {"user": LUIS})
		message = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "U.S. Passport",
				"verifier_name": "Luis Ortega",
				"verification_date": str(_today()),
			},
		)
		self.assertIn("not an active authorized signer", message)


def _today():
	from datetime import date

	return date.today()


# ── 9 ─────────────────────────────────────────────────────────────────────────
class TheFailClosedSwitchAndWhyItShipsOff(SignerTestCase):
	"""v0.94.0, F2b. The empty roster authorises everybody; this closes that.

	AND IT SHIPS OFF, WHICH IS THE MOST IMPORTANT ASSERTION IN THIS CLASS. The
	live Orchard Meadow bench reports `configured: false, count: 0` — the roster
	is empty — so a release that turned this on by default would refuse every I-9
	Section 2 on the farm the moment it deployed. The sequence is: populate the
	roster, confirm `list_authorized_signers` says configured, THEN switch this
	on. `test_it_is_off_by_default` is what stops a later edit reversing that.
	"""

	def _fail_closed(self, on=True):
		STORE.singles.setdefault("I-9 Settings", {"doctype": "I-9 Settings"})[
			"fail_closed_without_roster"
		] = 1 if on else 0

	def test_it_is_off_by_default_and_an_empty_roster_still_signs(self):
		"""THE DEPLOY-SAFETY ASSERTION. Nothing about this release changes what an
		unconfigured site does — which is what makes it safe to ship before the
		operator has done the data entry."""
		self.as_user(ANA)
		self._section_1()
		data = self._section_2(verifier_name="Ana Ramos")
		self.assertTrue(data)
		self.assertFalse(
			frappe.db.get_single_value("I-9 Settings", "fail_closed_without_roster"),
			"fail_closed_without_roster must ship OFF: the roster on the live bench is "
			"empty, and defaulting this on would refuse every Section 2 on the farm.",
		)

	def test_switched_on_with_no_roster_it_refuses_and_names_the_fix(self):
		"""The refusal names `add_authorized_signer` rather than a role, because
		no role is the answer: who may attest for the employer is a designation on
		a PERSON under §1324a, and 'find somebody from HR' is not available on a
		farm with no HR."""
		self.as_user(ANA)
		self._section_1()
		self._fail_closed()
		message = self.tool_error(
			"submit_i9_section_2",
			{
				"employee": "HR-EMP-00001",
				"document_path": "List A",
				"list_a_doc_title": "U.S. Passport",
				"verifier_name": "Ana Ramos",
				"verification_date": str(_today()),
			},
		)
		self.assertIn("add_authorized_signer", message)
		self.assertIn("Nothing was signed", message)

	def test_and_once_the_roster_has_rows_the_switch_changes_nothing(self):
		"""THE POINT OF THE SEQUENCING, asserted. With signers configured this
		flag is inert — the roster was already doing the work. It only governs the
		unconfigured branch, which is the only branch that was open."""
		self.add()
		self._fail_closed()
		self.as_user(ANA)
		self._section_1()
		self.assertTrue(self._section_2(verifier_name="Ana Ramos"))

	def test_it_does_not_touch_the_workers_own_boxes(self):
		"""§274a KEEPS THESE APART AND SO DOES THIS. Section 1 is the worker's own
		attestation and is on nobody's roster — a switch about EMPLOYER signatures
		that closed the employee's would be the exact conflation
		`signatures._require_signer` refuses at length."""
		self._fail_closed()
		self.as_user(ANA)
		self.assertTrue(self._section_1())
