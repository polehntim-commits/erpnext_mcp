# SPDX-License-Identifier: MIT
"""v0.138.0. The sealed page IS the attestation, and four things had to agree.

WHAT WAS WRONG. The iOS app builds the retained Form I-9 on the handset, seals
it, and files the finished page with `attach_signed_i9`. Nothing has filled
`section_1_signature` or `section_2_signature` since — those columns hold a
picture of a signature captured at a pad THIS SERVER was holding, and the server
is no longer present at the signing. `unsigned_boxes` tested those two columns
and nothing else, so every phone-built I-9 rested at `Awaiting Verification`
permanently and the compliance sweep raised `i9_section_1_unsigned` and
`i9_section_2_unsigned` — two Criticals — on forms that were signed, sealed and
retained. A board whose Criticals are known to be wrong is a board nobody reads.

THE SECOND WITNESS, and it takes all three columns: `signed_pdf` on the record
WITH `section_1_signed_at` and `section_2_signed_at` recorded beside it. That is
the employer's app asserting both attestations were made and producing the page
an inspection would be shown to back the assertion. A scan on its own could be a
blank form somebody photographed; two timestamps on their own are a claim with
nothing behind it.

FIVE CLAIMS.

1. `TheAttestationTest` — `phone_attested` over the whole truth table, and
   `unsigned_boxes` reading BOTH witnesses rather than swapping one for the other.

2. `TheFormReachesComplete` — the end-to-end run this release exists for: both
   sections filed, no signature ever sent to the server, the sealed page filed,
   and the form at `Complete`. Plus the negatives — a page with no timestamps, a
   page with one, and a form nobody verified — which are the tests that stop the
   fix from being "call everything complete".

3. `TheSweepAgrees` — the two rules go quiet on an attested form and still fire
   on every form that is genuinely unsigned.

4. `TheyCannotDisagree` — the claim the release is really making, over all
   thirty-two states of the five columns: a rule fires for a section EXACTLY
   when `unsigned_boxes` names it. Read off the SHIPPED rule specs, so a filter
   edited in `compliance_rules.py` without the status test moving fails here.

5. `TheGroupFilter` — `any`, the one disjunction in the scope-filter vocabulary,
   including the two things it refuses and the direction it fails in.
"""

import itertools
import json
from datetime import datetime, timedelta
from typing import ClassVar

import frappe

from erpnext_mcp import compliance_rules
from erpnext_mcp.patches import widen_i9_attestation_filters as widen
from erpnext_mcp.tools import i9

from .fixtures import MAIN, V12TestCase, install_hrms
from .harness import STORE
from .test_i9 import I9_TOOLS_ON, I9TestCase

#: The three columns a phone-built attestation is made of, in the order
#: `i9_attestation_group` names them. Restated here on purpose: this file is the
#: one place a second copy is wanted, because a test that imported the names it
#: is checking could not notice one of them changing.
PHONE_COLUMNS = ("signed_pdf", "section_1_signed_at", "section_2_signed_at")

SECTION_1 = "Section 1 (the employee's attestation)"
SECTION_2 = "Section 2 (the employer's attestation)"

#: A form with the phone attestation on it, as columns. Not a real timestamp
#: format anywhere it matters — nothing here parses these, they are read for
#: presence, which is the whole of what `phone_attested` asks.
ATTESTED = {
	"signed_pdf": "/private/files/signed-i9.pdf",
	"section_1_signed_at": "2026-07-02 08:14:00",
	"section_2_signed_at": "2026-07-02 08:42:00",
}


# ── 1 ─────────────────────────────────────────────────────────────────────────
class TheAttestationTest(V12TestCase):
	"""`phone_attested` and `unsigned_boxes`, on plain dicts and nothing else."""

	def test_all_three_columns_is_an_attestation(self):
		self.assertTrue(i9.phone_attested(dict(ATTESTED)))

	def test_any_one_missing_is_not(self):
		"""The negative that matters. A signed copy with no signing moments is a
		page somebody scanned; moments with no page are an app's word for it."""
		for column in PHONE_COLUMNS:
			with self.subTest(missing=column):
				row = dict(ATTESTED, **{column: ""})
				self.assertFalse(i9.phone_attested(row))

	def test_a_blank_is_the_same_as_an_absent_column(self):
		"""A site mid-install has the column and nothing in it; a Frappe read
		hands back None. Neither is an attestation and neither may raise."""
		self.assertFalse(i9.phone_attested({}))
		self.assertFalse(i9.phone_attested(dict(ATTESTED, signed_pdf=None)))
		self.assertFalse(i9.phone_attested(dict(ATTESTED, section_1_signed_at="   ")))

	def test_the_columns_are_the_ones_the_module_names(self):
		"""So a rename moves both halves of this release or fails here."""
		self.assertEqual((i9.SIGNED_COPY_FIELD, *i9.ATTESTATION_TIMESTAMPS), PHONE_COLUMNS)

	def test_an_attested_form_is_missing_neither_box(self):
		self.assertEqual(i9.unsigned_boxes(dict(ATTESTED)), [])

	def test_the_image_columns_still_answer_on_their_own(self):
		"""The first witness did not go away. A pad capture with no sealed page
		anywhere near it is exactly as complete as it was before v0.138.0."""
		row = {
			"section_1_signature": "/private/files/one.png",
			"section_2_signature": "/private/files/two.png",
		}
		self.assertEqual(i9.unsigned_boxes(row), [])

	def test_neither_witness_leaves_both_boxes_outstanding(self):
		self.assertEqual(i9.unsigned_boxes({}), [SECTION_1, SECTION_2])

	def test_half_a_phone_attestation_does_not_half_complete_the_form(self):
		"""`phone_attested` is per FORM, not per section, and this is what that
		means: the sealed page with only Section 1's moment on it proves nothing
		about either section, so both are still outstanding. Reading it section
		by section would invent a state the evidence cannot be in."""
		row = dict(ATTESTED, section_2_signed_at="")
		self.assertEqual(i9.unsigned_boxes(row), [SECTION_1, SECTION_2])

	def test_a_form_signed_at_the_pad_and_on_the_phone_answers_once(self):
		"""Section 1 came through the pad, Section 2 through the sealed page.
		Both witnesses are read and the form is complete."""
		row = dict(ATTESTED, section_1_signature="/private/files/one.png")
		self.assertEqual(i9.unsigned_boxes(row), [])


# ── 2 ─────────────────────────────────────────────────────────────────────────
class TheFormReachesComplete(I9TestCase):
	"""The run measured in v0.137.0's release notes, which ended at
	`Awaiting Verification` and two Criticals. It ends at `Complete` now."""

	def setUp(self):
		super().setUp()
		# `allow_attach_signed_i9` ships OFF like every mutating tool; turned on
		# here the same way `test_i9.TheSignedCopyCarriesItsOwnMetadata` turns it on.
		self.configure(enabled=1, **dict(I9_TOOLS_ON, allow_attach_signed_i9=1))
		# STAMPED FROM THE HARNESS CLOCK, NEVER HARDCODED. `frappe.utils.now()` is
		# a fixed base plus one second per call, so a literal date is whatever the
		# double says relative to it — and `attach_signed_i9` refuses a signing
		# moment in the future, which is a refusal these tests are not about.
		signing = datetime.fromisoformat(str(frappe.utils.now())) - timedelta(hours=2)
		self.SIGNED_1 = signing.strftime("%Y-%m-%d %H:%M:%S")
		self.SIGNED_2 = (signing + timedelta(minutes=28)).strftime("%Y-%m-%d %H:%M:%S")

	def _ready(self, employee="HR-EMP-00001") -> str:
		"""Both sections filed and no signature ever sent to this server."""
		self._create_draft(employee=employee)
		self._submit_section_1(employee=employee, section_1_signature="")
		self._submit_section_2(employee=employee, section_2_signature="")
		return str(frappe.db.get_value("I-9 Form", {"employee": employee}, "name"))

	def _a_scan(self, name="signed-i9.pdf") -> str:
		STORE.seed("File", [{"name": name, "file_name": name, "file_url": f"/private/files/{name}"}])
		return name

	def _file(self, name, **extra):
		payload = {"i9_form": name, "file_token": self._a_scan()}
		payload.update(extra)
		return self.tool_data("attach_signed_i9", payload)

	def _status(self, name) -> str:
		return str(frappe.db.get_value("I-9 Form", name, "status") or "")

	def test_the_sealed_page_with_both_moments_completes_the_form(self):
		name = self._ready()
		self.assertEqual(self._status(name), "Awaiting Verification")
		self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		self.assertEqual(self._status(name), "Complete")

	def test_the_call_reports_the_status_it_left_behind(self):
		"""Not the one it walked in on. A handset told `Awaiting Verification`
		by the very call that completed the form is reading the past."""
		name = self._ready()
		data = self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		self.assertEqual(data["status"], "Complete")

	def test_a_page_with_no_signing_moments_completes_nothing(self):
		"""The scan on its own could be a blank form somebody photographed."""
		name = self._ready()
		self._file(name)
		self.assertEqual(self._status(name), "Awaiting Verification")

	def test_a_page_with_one_moment_completes_nothing(self):
		name = self._ready()
		self._file(name, section_1_signed_at=self.SIGNED_1)
		self.assertEqual(self._status(name), "Awaiting Verification")

	def test_it_still_moves_one_edge_and_no_other(self):
		"""A status machine an upload could drive in any direction would not be
		one. Section 2 was never filed here, so there is no edge to move."""
		self._create_draft()
		self._submit_section_1(section_1_signature="")
		name = str(frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "name"))
		before = self._status(name)
		self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		self.assertEqual(self._status(name), before)

	def test_it_will_not_quietly_complete_a_form_somebody_expired(self):
		name = self._ready()
		frappe.db.set_value("I-9 Form", name, "status", "Expired")
		self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		self.assertEqual(self._status(name), "Expired")

	def test_the_completion_is_on_the_audit_log(self):
		"""A federal record that changed status without anybody calling a status
		tool has to say which call did it.

		THIS IS THE TEST THAT FOUND THE OLDER BUG. `advance_if_signed` has written
		this row since v0.64.2 and `Completed` was never a valid `action` on I-9
		Audit Log, so every insert was refused and `_log_action` swallowed the
		refusal — which it must, because an audit row can never be the reason a
		signature is lost. Nothing else asserted on the row, so nothing noticed.
		"""
		name = self._ready()
		self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		actions = [row.get("action") for row in STORE.rows("I-9 Audit Log") if row.get("i9_form") == name]
		self.assertIn("Signed Copy Filed", actions)
		self.assertIn("Completed", actions)

	def test_the_signed_copy_row_names_the_status_it_moved_to(self):
		"""So an inspection reading the log can tell WHICH upload completed the
		form, rather than finding a status change beside four uploads."""
		name = self._ready()
		self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		filed = next(
			row
			for row in STORE.rows("I-9 Audit Log")
			if row.get("i9_form") == name and row.get("action") == "Signed Copy Filed"
		)
		self.assertEqual(json.loads(filed["details"])["advanced_to"], "Complete")

	def test_every_action_this_module_writes_is_one_the_doctype_allows(self):
		"""The guard on the class of bug above. `_log_action` cannot report a
		refused insert — by design — so an action name that is not on the Select
		goes quiet for ever, and the only way to notice is to check the two lists
		against each other."""
		allowed = set(frappe.get_meta("I-9 Audit Log").get_field("action").options.split("\n"))
		for action in ("Completed", "Signature Collected", "Signed Copy Filed", "Status Changed"):
			with self.subTest(action=action):
				self.assertIn(action, allowed)

	def test_the_completed_form_leaves_the_pending_verification_list(self):
		"""The consequence nobody had to code for, and the measure of the bug's
		reach. `list_pending_i9_verifications` and `i9_verification_overdue` both
		key on STATUS, so a form parked at `Awaiting Verification` for ever was
		also an errand on somebody's list for ever and a THIRD Critical beside the
		two. Moving the status correctly clears all of it."""
		name = self._ready()
		pending = self.tool_data("list_pending_i9_verifications", {})
		self.assertIn(name, [row["name"] for row in pending["pending"]])

		self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		pending = self.tool_data("list_pending_i9_verifications", {})
		self.assertNotIn(name, [row["name"] for row in pending["pending"]])

	def test_a_section_2_filed_after_the_page_completes_in_the_one_call(self):
		"""The other order. Section 2's own status gate asks `unsigned_boxes`,
		so it sees the attestation that is already on the record."""
		self._create_draft()
		self._submit_section_1(section_1_signature="")
		name = str(frappe.db.get_value("I-9 Form", {"employee": "HR-EMP-00001"}, "name"))
		self._file(name, section_1_signed_at=self.SIGNED_1, section_2_signed_at=self.SIGNED_2)
		self.assertNotEqual(self._status(name), "Complete")

		data = self._submit_section_2(section_2_signature="")
		self.assertEqual(data["status"], "Complete")
		self.assertEqual(data["unsigned"], [])


# ── 3 ─────────────────────────────────────────────────────────────────────────
class SweepTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_refresh_compliance_alerts=1)
		install_hrms()
		STORE.singles.setdefault(
			"I-9 Settings",
			{"doctype": "I-9 Settings", "business_legal_name": "Test Farm LLC"},
		)
		compliance_rules.seed_compliance_rules()

	def an_i9(self, name="I9-2026-0001", **overrides):
		payload = {
			"doctype": "I-9 Form",
			"name": name,
			"employee": "HR-EMP-00002",
			"employee_name": "Ben Packhouse",
			"company": MAIN,
			"status": "Awaiting Verification",
			"hire_date": "2026-07-01",
			"verification_date": "2026-07-03",
			"verifier_name": "Ada Orchard",
			"legal_first_name": "Ben",
			"legal_last_name": "Packhouse",
			"citizenship_status": "US Citizen",
		}
		payload.update(overrides)
		STORE.seed("I-9 Form", [payload])
		return name

	def raised(self) -> set:
		self.tool_data("refresh_compliance_alerts", {"today": "2026-07-24"})
		return {
			row["alert_type"]
			for row in STORE.rows("Compliance Alert")
			if not frappe.utils.cint(row.get("dismissed"))
		}


class TheSweepAgrees(SweepTestCase):
	def test_an_unsigned_form_still_raises_both(self):
		"""The negative control. Without this the class below could pass because
		the rules had gone quiet on everything."""
		self.an_i9()
		raised = self.raised()
		self.assertIn("i9_section_1_unsigned", raised)
		self.assertIn("i9_section_2_unsigned", raised)

	def test_an_attested_form_raises_neither(self):
		self.an_i9(**ATTESTED)
		raised = self.raised()
		self.assertNotIn("i9_section_1_unsigned", raised)
		self.assertNotIn("i9_section_2_unsigned", raised)

	def test_a_sealed_page_on_its_own_still_raises_both(self):
		"""All three or none, at the sweep as well as at the status."""
		self.an_i9(signed_pdf=ATTESTED["signed_pdf"])
		raised = self.raised()
		self.assertIn("i9_section_1_unsigned", raised)
		self.assertIn("i9_section_2_unsigned", raised)

	def test_moments_with_no_page_still_raise_both(self):
		self.an_i9(
			section_1_signed_at=ATTESTED["section_1_signed_at"],
			section_2_signed_at=ATTESTED["section_2_signed_at"],
		)
		raised = self.raised()
		self.assertIn("i9_section_1_unsigned", raised)
		self.assertIn("i9_section_2_unsigned", raised)

	def test_the_patch_puts_the_filter_on_a_rule_seeded_before_the_release(self):
		"""A site that installed earlier already HAS these rule rows, and
		`seed_compliance_rules` leaves anything it finds alone — deliberately, so
		an operator's edit is not corrected back every upgrade. Without the patch
		the fix would reach fresh installs and nobody else."""
		self.an_i9(**ATTESTED)
		for rule_id in widen.WIDENED:
			name = compliance_rules.resolve(rule_id)
			stored = compliance_rules.parse_filters(
				frappe.db.get_value(compliance_rules.DOCTYPE, name, "scope_filters_json")
			)
			frappe.db.set_value(
				compliance_rules.DOCTYPE,
				name,
				"scope_filters_json",
				json.dumps([entry for entry in stored if entry["op"] != "any"]),
				update_modified=False,
			)
		self.assertIn("i9_section_1_unsigned", self.raised())

		report = widen.widen_i9_attestation_filters()
		self.assertEqual(sorted(report["widened"]), sorted(widen.WIDENED))
		raised = self.raised()
		self.assertNotIn("i9_section_1_unsigned", raised)
		self.assertNotIn("i9_section_2_unsigned", raised)

	def test_running_the_patch_twice_adds_one_filter(self):
		"""Two ANDed groups asking overlapping questions is a scope nobody can
		read, and a patch runs again on every `bench migrate`."""
		first = widen.widen_i9_attestation_filters()
		self.assertEqual(sorted(first["already"]), sorted(widen.WIDENED))
		self.assertEqual(first["widened"], [])
		for rule_id in widen.WIDENED:
			filters = compliance_rules.parse_filters(
				frappe.db.get_value(
					compliance_rules.DOCTYPE, compliance_rules.resolve(rule_id), "scope_filters_json"
				)
			)
			self.assertEqual(len([entry for entry in filters if entry["op"] == "any"]), 1)

	def test_the_patch_keeps_a_filter_an_operator_added(self):
		"""Dropping it would widen a rule somebody deliberately narrowed."""
		rule_id = "i9_section_1_unsigned"
		name = compliance_rules.resolve(rule_id)
		theirs = {"field": "company", "op": "eq", "value": MAIN}
		stored = compliance_rules.parse_filters(
			frappe.db.get_value(compliance_rules.DOCTYPE, name, "scope_filters_json")
		)
		frappe.db.set_value(
			compliance_rules.DOCTYPE,
			name,
			"scope_filters_json",
			json.dumps([entry for entry in stored if entry["op"] != "any"] + [theirs]),
			update_modified=False,
		)
		widen.widen_i9_attestation_filters()
		after = compliance_rules.parse_filters(
			frappe.db.get_value(compliance_rules.DOCTYPE, name, "scope_filters_json")
		)
		self.assertIn(theirs, after)
		self.assertEqual(len([entry for entry in after if entry["op"] == "any"]), 1)


# ── 4 ─────────────────────────────────────────────────────────────────────────
class TheyCannotDisagree(V12TestCase):
	"""The claim the release is making, and the reason all four things moved in
	one commit: for every state of the five columns, a rule fires for a section
	EXACTLY when `unsigned_boxes` names that section.

	READ OFF THE SHIPPED SPECS. `declarative_seed_specs()` is what a site is
	seeded from, so a filter edited there without the status test moving fails
	here rather than in an orchard.
	"""

	COLUMNS = (
		"section_1_signature",
		"section_2_signature",
		"signed_pdf",
		"section_1_signed_at",
		"section_2_signed_at",
	)

	#: What each column holds when it is filled. Distinct strings so a mix-up
	#: between two of them shows up as a wrong value rather than a passing test.
	FILLED: ClassVar[dict] = {
		"section_1_signature": "/private/files/one.png",
		"section_2_signature": "/private/files/two.png",
		"signed_pdf": "/private/files/signed-i9.pdf",
		"section_1_signed_at": "2026-07-02 08:14:00",
		"section_2_signed_at": "2026-07-02 08:42:00",
	}

	def setUp(self):
		super().setUp()
		specs = {spec["rule_id"]: spec for spec in compliance_rules.declarative_seed_specs()}
		self.filters = {
			rule_id: compliance_rules.parse_filters(specs[rule_id]["scope_filters"])
			for rule_id in ("i9_section_1_unsigned", "i9_section_2_unsigned")
		}

	def _fires(self, rule_id: str, row: dict) -> bool:
		matched, _warnings = compliance_rules.row_matches(row, self.filters[rule_id], set(row))
		return matched

	def test_every_state_of_the_five_columns(self):
		#: The two columns that put a form in scope of BOTH rules at once, so the
		#: only thing varying across the matrix is the evidence.
		base = {"status": "Awaiting Verification", "verification_date": "2026-07-03"}
		seen = 0
		for filled in itertools.product((False, True), repeat=len(self.COLUMNS)):
			row = dict(base)
			for column, on in zip(self.COLUMNS, filled, strict=True):
				row[column] = self.FILLED[column] if on else ""
			outstanding = i9.unsigned_boxes(row)
			with self.subTest(**{column: ("set" if row[column] else "") for column in self.COLUMNS}):
				self.assertEqual(self._fires("i9_section_1_unsigned", row), SECTION_1 in outstanding)
				self.assertEqual(self._fires("i9_section_2_unsigned", row), SECTION_2 in outstanding)
			seen += 1
		# The matrix is the test; a loop that ran nothing would pass silently.
		self.assertEqual(seen, 2 ** len(self.COLUMNS))

	def test_the_matrix_contains_both_answers(self):
		"""Measured rather than assumed. If every state fired, or none did, the
		loop above would agree with itself and prove nothing."""
		base = {"status": "Awaiting Verification", "verification_date": "2026-07-03"}
		answers = set()
		for filled in itertools.product((False, True), repeat=len(self.COLUMNS)):
			row = dict(base)
			for column, on in zip(self.COLUMNS, filled, strict=True):
				row[column] = self.FILLED[column] if on else ""
			answers.add(self._fires("i9_section_1_unsigned", row))
			answers.add(self._fires("i9_section_2_unsigned", row))
		self.assertEqual(answers, {True, False})

	def test_the_group_names_the_columns_the_module_reads(self):
		group = compliance_rules.i9_attestation_group()
		self.assertEqual([entry["field"] for entry in group["value"]], list(PHONE_COLUMNS))
		self.assertEqual({entry["op"] for entry in group["value"]}, {"isnull"})


# ── 5 ─────────────────────────────────────────────────────────────────────────
class TheGroupFilter(V12TestCase):
	"""`any`, on its own, away from the I-9 it was added for."""

	GROUP: ClassVar[dict] = {
		"op": "any",
		"value": [{"field": "left", "op": "isnull"}, {"field": "right", "op": "isnull"}],
	}

	def _matches(self, row: dict, present=None) -> bool:
		filters = compliance_rules.parse_filters([self.GROUP])
		matched, _warnings = compliance_rules.row_matches(row, filters, present)
		return matched

	def test_one_member_passing_passes_the_group(self):
		self.assertTrue(self._matches({"left": "", "right": "here"}))
		self.assertTrue(self._matches({"left": "here", "right": ""}))

	def test_no_member_passing_fails_the_group(self):
		self.assertFalse(self._matches({"left": "here", "right": "here"}))

	def test_a_group_is_still_ANDed_with_its_neighbours(self):
		"""`any` is a disjunction INSIDE one entry. The list it sits in is not."""
		filters = compliance_rules.parse_filters(
			[{"field": "status", "op": "eq", "value": "Open"}, self.GROUP]
		)
		matched, _warnings = compliance_rules.row_matches(
			{"status": "Shut", "left": "", "right": ""}, filters, None
		)
		self.assertFalse(matched)

	def test_a_column_this_site_has_not_got_widens_rather_than_narrows(self):
		"""The direction every absent column takes here. A group is a NARROWING,
		so passing it is the fail-safe answer — a rule on a site that has not run
		`install_compliance_fields` scans MORE rows and says so, rather than
		going quiet on records nobody has looked at."""
		self.assertTrue(self._matches({"other": "x"}, present={"other"}))

	def test_an_absent_column_is_skipped_and_the_others_still_answer(self):
		"""Half the group present is not half an answer: the members this site
		can answer decide it."""
		self.assertFalse(self._matches({"left": "here"}, present={"left"}))
		self.assertTrue(self._matches({"left": ""}, present={"left"}))

	def test_the_skip_is_reported(self):
		"""Asked of a row whose answerable member FAILS, because the group short-
		circuits on the first member that passes and a passing group never gets as
		far as the column this site has not got."""
		filters = compliance_rules.parse_filters([self.GROUP])
		matched, warnings = compliance_rules.row_matches({"left": "here"}, filters, {"left"})
		self.assertFalse(matched)
		self.assertTrue(any("'right'" in warning for warning in warnings))

	def test_an_empty_group_is_refused_at_authoring_time(self):
		"""It passes every row, so the rule would look scoped and be unscoped —
		which is the failure this parser exists to catch."""
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_filters([{"op": "any", "value": []}])
		self.assertIn("passes every row", str(caught.exception))

	def test_a_group_inside_a_group_is_refused(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_filters([{"op": "any", "value": [dict(self.GROUP)]}])
		self.assertIn("custom_python", str(caught.exception))

	def test_a_bad_member_is_refused_by_its_own_position(self):
		with self.assertRaises(ValueError) as caught:
			compliance_rules.parse_filters([{"op": "any", "value": [{"field": "x", "op": "nope"}]}])
		self.assertIn("scope_filters[0].value[0]", str(caught.exception))

	def test_a_group_names_no_field_of_its_own(self):
		parsed = compliance_rules.parse_filters([self.GROUP])
		self.assertEqual(parsed[0]["field"], "")

	def test_filter_fields_looks_inside_the_group(self):
		"""The callers that build a SELECT from a filter list read this. A SELECT
		missing the group's columns is not a crash — `row_matches` skips a column
		it has not got — so it would be a rule that quietly stopped scoping."""
		parsed = compliance_rules.parse_filters(
			[{"field": "status", "op": "eq", "value": "Open"}, self.GROUP]
		)
		self.assertEqual(compliance_rules.filter_fields(parsed), ["status", "left", "right"])

	def test_any_is_in_the_advertised_vocabulary(self):
		"""A rule author is told which operators exist by `FILTER_OPS`, and one
		that worked but was not listed would be one nobody could find."""
		self.assertIn("any", compliance_rules.FILTER_OPS)
