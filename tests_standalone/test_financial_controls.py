# SPDX-License-Identifier: MIT
"""IPO readiness Phase 1 — the financial controls, and the switch they share.

THE CLAIM THIS RELEASE MAKES, AND THE ONE THESE TESTS EXIST TO PIN DOWN: every
control is bypassable, and Advisory is not a weaker version of the same check —
it is the SAME evaluation, reaching the SAME finding, filing the SAME compliance
alert, without the refusal. If that is true then an operation can run a season in
Advisory and end it holding exactly the register of findings it would hold had it
been enforcing, which is the whole argument for shipping every control off the
brake. If it is false in even one control, the argument collapses and an operator
who turns a control up gets a surprise.

So the load-bearing class here is `AdvisoryAndEnforcedDifferByRefusalAlone`, and
it asserts the equivalence directly rather than by proxy: same tool, same data,
one switch moved, and the findings compared field by field.

SEVEN CLASSES.

1. `TheChainIsEvaluatedByAmount` — `controls.required_authority` on the pure
   engine: the floor, the rungs in amount order however they were typed, the
   uncapped top, and the amount past the end of a chain that has no top.
2. `TheControllerRefusesAnAmbiguousChain` — the four states that would make
   'who approves this' depend on row order.
3. `PeriodsAndTheirSteps` — the checklist, the lock, the overlap refusal, and
   the completion carried across a re-worded step.
4. `TheJournalEntryControlsFind` — duplicates, transposed digits, self-approval,
   and the two cases where the app declines to have an opinion.
5. `AdvisoryAndEnforcedDifferByRefusalAlone` — the claim above.
6. `TheGateIsWiredIntoCreateJournalEntry` — a control that is not called from
   the write path is not a control, so the wiring is asserted rather than
   assumed.
7. `TheRegisterReadsBack` — what an operator sees when they ask what is enforced.
"""

from typing import ClassVar

import frappe

from erpnext_mcp import compliance_rules, controls, enforcement

from .fixtures import MAIN, MAIN_ABBR, OTHER, V12TestCase, cash, supplies
from .harness import STORE

TODAY = "2026-08-16"

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"list_control_points",
		"check_journal_entry_controls",
		"create_approval_threshold",
		"get_approval_threshold",
		"list_approval_thresholds",
		"update_approval_threshold",
		"create_closing_checklist",
		"get_closing_checklist",
		"list_closing_checklists",
		"update_closing_checklist",
		"complete_checklist_item",
		"close_accounting_period",
		"reopen_accounting_period",
		"create_journal_entry",
		"update_compliance_rule",
		"list_compliance_rules",
	)
}


class ControlsTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(**ALL_ON)
		STORE.seed(
			"Role", [{"name": name} for name in ("Farm Manager", "Accounts Manager", "System Manager")]
		)
		compliance_rules.seed_compliance_rules()

	# ── helpers ─────────────────────────────────────────────────────────────
	def enforce(self, control_point: str) -> None:
		"""Turn one control up, the way an operator would.

		Through the rule record rather than by monkey-patching, because the switch
		being a RECORD an operator edits is half of what this release is.
		"""
		rows = frappe.db.get_all("Compliance Rule", filters={"control_point": control_point}, pluck="name")
		self.assertTrue(rows, f"no seeded rule for control point {control_point!r}")
		frappe.db.set_value("Compliance Rule", rows[0], "enforcement_mode", "Enforced")

	def disable(self, control_point: str) -> None:
		rows = frappe.db.get_all("Compliance Rule", filters={"control_point": control_point}, pluck="name")
		frappe.db.set_value("Compliance Rule", rows[0], "enabled", 0)

	def a_threshold(self, **overrides):
		payload = {
			"threshold_name": "Operating spend",
			"company": MAIN,
			"document_type": "Journal Entry",
			"auto_approve_below": 1000,
			"levels": [
				{"approver_role": "Farm Manager", "up_to_amount": 5000},
				{"approver_role": "Accounts Manager", "up_to_amount": 50000},
				{"approver_role": "System Manager"},
			],
		}
		payload.update(overrides)
		return self.tool_data("create_approval_threshold", payload)

	def a_period(self, **overrides):
		payload = {
			"company": MAIN,
			"period_start": "2026-03-01",
			"period_end": "2026-03-31",
			"period_type": "Month",
			"items": [
				{"step": "Reconcile every bank account", "sequence": 1, "required": True},
				{"step": "Post accruals", "sequence": 2, "required": True},
				{"step": "Email the lender", "sequence": 3, "required": False},
			],
		}
		payload.update(overrides)
		return self.tool_data("create_closing_checklist", payload)

	def alerts_for(self, control_point: str) -> list:
		return [row for row in STORE.rows("Compliance Alert") if row.get("alert_type") == control_point]


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheChainIsEvaluatedByAmount(ControlsTestCase):
	"""The pure engine. No site, no tools — a table in, an authority out."""

	CHAIN: ClassVar[dict] = {
		"name": "T-1",
		"auto_approve_below": 1000,
		"levels": [
			# Deliberately OUT of amount order, because the whole claim is that row
			# order does not decide who approves anything.
			{"approver_role": "System Manager"},
			{"approver_role": "Accounts Manager", "up_to_amount": 50000},
			{"approver_role": "Farm Manager", "up_to_amount": 5000},
		],
	}

	def test_below_the_floor_needs_nobody(self):
		verdict = controls.required_authority(self.CHAIN, 999)
		self.assertFalse(verdict["needed"])
		self.assertIn("auto-approve floor", verdict["reason"])

	def test_the_floor_itself_is_not_below_the_floor(self):
		"""`auto_approve_below` means strictly below. A boundary that went the
		other way would leave the first rung unable to fire at its own floor."""
		self.assertTrue(controls.required_authority(self.CHAIN, 1000)["needed"])

	def test_the_first_rung_whose_ceiling_covers_it_wins_whatever_the_row_order(self):
		verdict = controls.required_authority(self.CHAIN, 4000)
		self.assertEqual(verdict["approver_role"], "Farm Manager")
		self.assertEqual(verdict["rung"], 5000)

	def test_a_ceiling_is_inclusive(self):
		self.assertEqual(controls.required_authority(self.CHAIN, 5000)["approver_role"], "Farm Manager")
		self.assertEqual(
			controls.required_authority(self.CHAIN, 5000.01)["approver_role"], "Accounts Manager"
		)

	def test_the_uncapped_rung_is_the_top_however_it_was_typed(self):
		verdict = controls.required_authority(self.CHAIN, 10_000_000)
		self.assertEqual(verdict["approver_role"], "System Manager")
		self.assertTrue(verdict["uncapped"])

	def test_past_the_end_of_a_capped_chain_is_reported_not_silently_allowed(self):
		"""An operation's first transaction larger than anything its table
		anticipated. Nobody may release it, and that is a finding rather than an
		error or a pass."""
		capped = {"name": "T-2", "levels": [{"approver_role": "Farm Manager", "up_to_amount": 5000}]}
		verdict = controls.required_authority(capped, 9000)
		self.assertTrue(verdict["above_chain"])
		self.assertEqual(verdict["ceiling"], 5000)

	def test_a_negative_amount_is_judged_on_its_size(self):
		"""A credit note for fifty thousand is fifty thousand of authority."""
		self.assertEqual(controls.required_authority(self.CHAIN, -40000)["approver_role"], "Accounts Manager")

	def test_a_specific_document_type_beats_the_blanket_table(self):
		tables = [
			{"name": "blanket", "document_type": "Any", "enabled": 1},
			{"name": "specific", "document_type": "Journal Entry", "enabled": 1},
		]
		self.assertEqual(controls.applicable_threshold(tables, "Journal Entry")["name"], "specific")
		self.assertEqual(controls.applicable_threshold(tables, "Payment Entry")["name"], "blanket")

	def test_a_disabled_table_is_not_consulted_even_when_it_is_the_only_one(self):
		tables = [{"name": "blanket", "document_type": "Any", "enabled": 0}]
		self.assertEqual(controls.applicable_threshold(tables, "Journal Entry"), {})


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheControllerRefusesAnAmbiguousChain(ControlsTestCase):
	def test_two_uncapped_rungs_are_refused(self):
		error = self.tool_error(
			"create_approval_threshold",
			{
				"threshold_name": "Bad",
				"company": MAIN,
				"levels": [{"approver_role": "Farm Manager"}, {"approver_role": "Accounts Manager"}],
			},
		)
		self.assertIn("uncapped", error.lower())

	def test_two_rungs_with_the_same_ceiling_are_refused(self):
		error = self.tool_error(
			"create_approval_threshold",
			{
				"threshold_name": "Bad",
				"company": MAIN,
				"levels": [
					{"approver_role": "Farm Manager", "up_to_amount": 5000},
					{"approver_role": "Accounts Manager", "up_to_amount": 5000},
				],
			},
		)
		self.assertIn("row order", error)

	def test_a_floor_above_the_first_rung_is_refused_because_that_rung_could_never_fire(self):
		error = self.tool_error(
			"create_approval_threshold",
			{
				"threshold_name": "Bad",
				"company": MAIN,
				"auto_approve_below": 9000,
				"levels": [{"approver_role": "Farm Manager", "up_to_amount": 5000}],
			},
		)
		self.assertIn("never fire", error)

	def test_an_enabled_table_with_no_rungs_is_refused(self):
		error = self.tool_error(
			"create_approval_threshold", {"threshold_name": "Bad", "company": MAIN, "levels": []}
		)
		self.assertIn("at least one level", error)

	def test_a_rung_naming_a_role_that_does_not_exist_is_refused_at_the_door(self):
		error = self.tool_error(
			"create_approval_threshold",
			{
				"threshold_name": "Bad",
				"company": MAIN,
				"levels": [{"approver_role": "Chief Cherry Officer", "up_to_amount": 100}],
			},
		)
		self.assertIn("not a Role on this site", error)

	def test_updating_levels_replaces_the_chain_rather_than_merging_into_it(self):
		"""A stale rung in an authority table authorises spending."""
		created = self.a_threshold()
		self.assertEqual(created["threshold"]["level_count"], 3)
		after = self.tool_data(
			"update_approval_threshold",
			{"name": created["threshold"]["name"], "levels": [{"approver_role": "System Manager"}]},
		)
		self.assertEqual(after["threshold"]["level_count"], 1)
		self.assertEqual(after["threshold"]["levels"][0]["approver_role"], "System Manager")

	def test_the_chain_reads_back_in_evaluation_order_not_grid_order(self):
		created = self.a_threshold(
			levels=[
				{"approver_role": "System Manager"},
				{"approver_role": "Accounts Manager", "up_to_amount": 50000},
				{"approver_role": "Farm Manager", "up_to_amount": 5000},
			]
		)
		roles = [row["approver_role"] for row in created["threshold"]["levels"]]
		self.assertEqual(roles, ["Farm Manager", "Accounts Manager", "System Manager"])


# ── 3 ───────────────────────────────────────────────────────────────────────
class PeriodsAndTheirSteps(ControlsTestCase):
	def test_a_period_with_no_steps_given_gets_the_eight_a_close_usually_has(self):
		data = self.tool_data(
			"create_closing_checklist",
			{"company": MAIN, "period_start": "2026-03-01", "period_end": "2026-03-31"},
		)
		self.assertEqual(
			data["checklist"]["item_count"],
			len(__import__("erpnext_mcp.tools.controls", fromlist=["DEFAULT_STEPS"]).DEFAULT_STEPS),
		)
		self.assertIn("defaults_note", data)

	def test_the_seeded_steps_carry_spanish(self):
		data = self.tool_data(
			"create_closing_checklist",
			{"company": MAIN, "period_start": "2026-03-01", "period_end": "2026-03-31"},
		)
		self.assertTrue(all(row["step_es"] for row in data["checklist"]["items"]))

	def test_an_inverted_period_is_refused(self):
		error = self.tool_error(
			"create_closing_checklist",
			{"company": MAIN, "period_start": "2026-03-31", "period_end": "2026-03-01"},
		)
		self.assertIn("before", error)

	def test_two_periods_overlapping_for_one_company_are_refused(self):
		self.a_period()
		error = self.tool_error(
			"create_closing_checklist",
			{"company": MAIN, "period_start": "2026-03-15", "period_end": "2026-04-15"},
		)
		self.assertIn("overlap", error.lower())

	def test_another_companys_period_may_cover_the_same_dates(self):
		self.a_period()
		other = self.tool_data(
			"create_closing_checklist",
			{"company": OTHER, "period_start": "2026-03-01", "period_end": "2026-03-31"},
		)
		self.assertEqual(other["checklist"]["company"], OTHER)

	def test_only_required_steps_count_as_outstanding(self):
		data = self.a_period()
		self.assertEqual(data["checklist"]["item_count"], 3)
		self.assertEqual(data["checklist"]["outstanding_count"], 2)

	def test_completing_a_step_records_who_and_when(self):
		created = self.a_period()
		data = self.tool_data(
			"complete_checklist_item",
			{
				"name": created["checklist"]["name"],
				"step": "Post accruals",
				"evidence": "ACC-2026-0031",
			},
		)
		row = next(item for item in data["checklist"]["items"] if item["step"] == "Post accruals")
		self.assertTrue(row["completed"])
		self.assertEqual(row["completed_by"], "Administrator")
		self.assertEqual(row["evidence"], "ACC-2026-0031")
		self.assertEqual(data["checklist"]["outstanding_count"], 1)

	def test_completing_a_step_that_is_not_on_the_list_names_the_steps_that_are(self):
		created = self.a_period()
		error = self.tool_error(
			"complete_checklist_item", {"name": created["checklist"]["name"], "step": "Feed the dog"}
		)
		self.assertIn("Post accruals", error)

	def test_rewording_a_step_list_does_not_lose_a_completion(self):
		"""Losing the record that somebody reconciled the bank — and when —
		because a LATER step was reworded is a data loss nobody asked for."""
		created = self.a_period()
		name = created["checklist"]["name"]
		self.tool_data("complete_checklist_item", {"name": name, "step": "Post accruals"})
		after = self.tool_data(
			"update_closing_checklist",
			{
				"name": name,
				"items": [
					{"step": "Reconcile every bank account", "sequence": 1},
					{"step": "Post accruals", "sequence": 2},
					{"step": "Run depreciation", "sequence": 3},
				],
			},
		)
		row = next(item for item in after["checklist"]["items"] if item["step"] == "Post accruals")
		self.assertTrue(row["completed"])
		self.assertEqual(row["completed_by"], "Administrator")

	def test_reopening_says_reopened_rather_than_open(self):
		created = self.a_period()
		name = created["checklist"]["name"]
		self.tool_data("close_accounting_period", {"name": name})
		data = self.tool_data("reopen_accounting_period", {"name": name, "reason": "auditor adjustment"})
		self.assertEqual(data["checklist"]["status"], "Reopened")
		self.assertFalse(data["checklist"]["locked"])

	def test_reopening_demands_a_reason(self):
		created = self.a_period()
		name = created["checklist"]["name"]
		self.tool_data("close_accounting_period", {"name": name})
		error = self.tool_error("reopen_accounting_period", {"name": name})
		self.assertIn("reason", error)

	def test_unlocking_clears_the_attribution_rather_than_leaving_a_stale_name(self):
		created = self.a_period()
		name = created["checklist"]["name"]
		self.tool_data("close_accounting_period", {"name": name})
		self.tool_data("reopen_accounting_period", {"name": name, "reason": "auditor adjustment"})
		read = self.tool_data("get_closing_checklist", {"name": name})
		self.assertIsNone(read["locked_by"])
		self.assertIsNone(read["locked_on"])

	def test_locking_an_already_locked_period_is_refused(self):
		created = self.a_period()
		name = created["checklist"]["name"]
		self.tool_data("close_accounting_period", {"name": name})
		self.assertIn("already locked", self.tool_error("close_accounting_period", {"name": name}))


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheJournalEntryControlsFind(ControlsTestCase):
	"""The pure engine again: the four judgements, on data rather than a site."""

	def test_two_entries_with_the_same_total_accounts_and_a_close_date_match(self):
		matches = controls.duplicate_findings(
			{"posting_date": "2026-03-31", "total": 4200.0, "accounts": ("A", "B")},
			[{"name": "ACC-1", "posting_date": "2026-03-29", "total": 4200.0, "accounts": ["B", "A"]}],
		)
		self.assertEqual(len(matches), 1)
		self.assertEqual(matches[0]["days_apart"], 2)

	def test_account_order_does_not_hide_a_duplicate(self):
		"""The same entry typed by two people has the same accounts in whatever
		order each of them typed them."""
		self.assertEqual(
			controls.account_signature([{"account": "B"}, {"account": "A"}]),
			controls.account_signature([{"account": "A"}, {"account": "B"}]),
		)

	def test_a_recurring_monthly_entry_does_not_match_last_month(self):
		"""The window is the whole reason this control is quiet enough to live
		with: a monthly accrual matches on total and accounts every single time."""
		matches = controls.duplicate_findings(
			{"posting_date": "2026-03-31", "total": 4200.0, "accounts": ("A", "B")},
			[{"name": "ACC-1", "posting_date": "2026-02-28", "total": 4200.0, "accounts": ["A", "B"]}],
		)
		self.assertEqual(matches, [])

	def test_a_cent_of_difference_does_not_hide_a_duplicate(self):
		matches = controls.duplicate_findings(
			{"posting_date": "2026-03-31", "total": 4200.0, "accounts": ("A",)},
			[{"name": "ACC-1", "posting_date": "2026-03-31", "total": 4200.005, "accounts": ["A"]}],
		)
		self.assertEqual(len(matches), 1)

	def test_a_different_amount_is_not_a_duplicate(self):
		matches = controls.duplicate_findings(
			{"posting_date": "2026-03-31", "total": 4200.0, "accounts": ("A",)},
			[{"name": "ACC-1", "posting_date": "2026-03-31", "total": 4900.0, "accounts": ["A"]}],
		)
		self.assertEqual(matches, [])

	def test_different_bank_references_are_not_duplicates(self):
		"""v0.184.0. Two entries booked from two different Bank Transactions are
		two bank transactions, however alike their total, accounts and date."""
		matches = controls.duplicate_findings(
			{"posting_date": "2026-09-02", "total": 10.0, "accounts": ("A", "B"), "cheque_no": "ACC-BTN-1"},
			[
				{
					"name": "ACC-1",
					"posting_date": "2026-09-02",
					"total": 10.0,
					"accounts": ["A", "B"],
					"cheque_no": "ACC-BTN-2",
				}
			],
		)
		self.assertEqual(matches, [])

	def test_the_same_bank_reference_twice_is_still_a_duplicate(self):
		"""The same BTN booked twice is the duplicate this control exists for."""
		matches = controls.duplicate_findings(
			{"posting_date": "2026-09-02", "total": 10.0, "accounts": ("A", "B"), "cheque_no": "ACC-BTN-1"},
			[
				{
					"name": "ACC-1",
					"posting_date": "2026-09-02",
					"total": 10.0,
					"accounts": ["A", "B"],
					"cheque_no": " ACC-BTN-1 ",
				}
			],
		)
		self.assertEqual(len(matches), 1)
		self.assertIn("both reference bank transaction ACC-BTN-1", matches[0]["why"])

	def test_one_missing_bank_reference_is_still_compared(self):
		"""A reference on only one side proves nothing about the other: a
		hand-keyed copy of a bank-fed entry is the other classic duplicate."""
		for mine, theirs in (("ACC-BTN-1", ""), ("", "ACC-BTN-1"), ("", None)):
			with self.subTest(mine=mine, theirs=theirs):
				matches = controls.duplicate_findings(
					{"posting_date": "2026-09-02", "total": 10.0, "accounts": ("A",), "cheque_no": mine},
					[
						{
							"name": "ACC-1",
							"posting_date": "2026-09-02",
							"total": 10.0,
							"accounts": ["A"],
							"cheque_no": theirs,
						}
					],
				)
				self.assertEqual(len(matches), 1)

	def test_a_transposed_digit_is_caught(self):
		history = [1000.0] * 12
		verdict = controls.unusual_amount(90_000.0, history)
		self.assertTrue(verdict["unusual"])
		self.assertEqual(verdict["median"], 1000.0)
		self.assertEqual(verdict["multiple"], 90.0)

	def test_a_merely_large_entry_is_not_unusual(self):
		"""A band that fires on anything big trains people to dismiss it, at
		which point it catches nothing — including the digit it exists for."""
		self.assertFalse(controls.unusual_amount(15_000.0, [1000.0] * 12)["unusual"])

	def test_too_small_a_sample_forms_no_opinion_and_says_so(self):
		verdict = controls.unusual_amount(90_000.0, [1000.0, 900.0])
		self.assertFalse(verdict["unusual"])
		self.assertIn("No opinion was formed", verdict["basis"])
		self.assertIn("should not be read as", verdict["basis"])

	def test_a_small_entry_is_never_unusual_however_large_its_multiple(self):
		"""A company whose typical entry is four dollars would otherwise have
		every hundred-dollar entry flagged — arithmetically true and useless."""
		verdict = controls.unusual_amount(500.0, [4.0] * 12)
		self.assertFalse(verdict["unusual"])
		self.assertIn("floor", verdict["basis"])

	def test_the_median_is_unmoved_by_one_lumpy_month(self):
		"""Why a median and not a standard deviation: one month of capital spend
		would widen a variance band until it caught nothing for a year."""
		self.assertEqual(controls.median([100.0] * 11 + [5_000_000.0]), 100.0)

	def test_one_hand_on_both_sides_is_a_finding(self):
		self.assertTrue(controls.same_hand("ana@example.test", "ANA@example.test"))

	def test_a_blank_approver_is_not_a_self_approval(self):
		"""An entry nobody has approved is a different finding from one approved
		by its own author, and collapsing the two would report every draft in the
		system as a segregation failure."""
		self.assertFalse(controls.same_hand("ana@example.test", ""))
		self.assertFalse(controls.same_hand("", ""))

	def test_a_locked_period_matches_its_own_last_day(self):
		periods = [{"name": "P", "locked": 1, "period_start": "2026-03-01", "period_end": "2026-03-31"}]
		self.assertEqual(controls.locked_period(periods, "2026-03-31")["name"], "P")
		self.assertEqual(controls.locked_period(periods, "2026-04-01"), {})

	def test_an_unlocked_period_never_matches(self):
		periods = [{"name": "P", "locked": 0, "period_start": "2026-03-01", "period_end": "2026-03-31"}]
		self.assertEqual(controls.locked_period(periods, "2026-03-15"), {})


# ── 5 ───────────────────────────────────────────────────────────────────────
class AdvisoryAndEnforcedDifferByRefusalAlone(ControlsTestCase):
	"""THE CLAIM OF THE RELEASE, ASSERTED DIRECTLY.

	Same tool, same data, one switch moved. If Advisory and Enforced ever reach
	different findings then "run a season in Advisory and you will know exactly
	what enforcement costs you" is false, and every operator who trusted it gets a
	surprise on the day they turn a control up.
	"""

	def a_locked_march(self):
		created = self.a_period(items=[])
		self.tool_data("close_accounting_period", {"name": created["checklist"]["name"]})
		return created["checklist"]["name"]

	def an_entry_into_march(self):
		return {
			"company": MAIN,
			"posting_date": "2026-03-15",
			"user_remark": "back-dated repair",
			"accounts": [
				{"account": supplies(MAIN_ABBR), "debit": 250},
				{"account": cash(MAIN_ABBR), "credit": 250},
			],
		}

	def test_everything_ships_advisory(self):
		"""The default is the load-bearing part. An operation that cannot book a
		fuel invoice on day one turns the module off entirely."""
		for control_point in enforcement.CONTROL_POINTS:
			self.assertEqual(
				enforcement.mode(control_point),
				enforcement.ADVISORY,
				f"{control_point} does not ship Advisory",
			)

	def test_advisory_lets_the_entry_through_and_says_what_it_found(self):
		self.a_locked_march()
		data = self.tool_data("create_journal_entry", self.an_entry_into_march())
		self.assertTrue(data["name"], "the entry was not written")
		block = data["controls"]["controls"]["period_close_lockdown"]
		self.assertEqual(block["mode"], enforcement.ADVISORY)
		self.assertEqual(block["action"], "reported")
		self.assertEqual(block["finding_count"], 1)
		self.assertIn("written anyway", data["controls_note"].lower())
		self.assertIn("would have been refused", data["controls_note"].lower())

	def test_enforced_refuses_the_same_entry_and_explains(self):
		self.a_locked_march()
		self.enforce("period_close_lockdown")
		error = self.tool_error("create_journal_entry", self.an_entry_into_march())
		self.assertIn("REFUSED", error)
		self.assertIn("locked", error)
		self.assertIn("reopen_accounting_period", error)

	def test_enforced_writes_nothing(self):
		"""'Nothing was written' is a claim the refusal makes in words. It is
		asserted against the store, because a refusal that left a draft behind
		would be worse than no control."""
		self.a_locked_march()
		self.enforce("period_close_lockdown")
		before = len(STORE.rows("Journal Entry"))
		self.tool_error("create_journal_entry", self.an_entry_into_march())
		self.assertEqual(len(STORE.rows("Journal Entry")), before)

	def test_both_modes_reach_an_identical_finding(self):
		"""THE EQUIVALENCE, FIELD BY FIELD."""
		self.a_locked_march()
		advisory = self.tool_data(
			"check_journal_entry_controls",
			{
				"company": MAIN,
				"posting_date": "2026-03-15",
				"total": 250,
				"accounts": [supplies(MAIN_ABBR), cash(MAIN_ABBR)],
			},
		)["controls"]["period_close_lockdown"]["findings"]

		self.enforce("period_close_lockdown")
		enforced = self.tool_data(
			"check_journal_entry_controls",
			{
				"company": MAIN,
				"posting_date": "2026-03-15",
				"total": 250,
				"accounts": [supplies(MAIN_ABBR), cash(MAIN_ABBR)],
			},
		)["controls"]["period_close_lockdown"]["findings"]

		self.assertEqual(advisory, enforced)
		self.assertEqual(len(advisory), 1)

	def test_both_modes_file_the_same_alert_against_the_same_record(self):
		"""The data trail is the reason Advisory is worth running, so it is not
		allowed to be thinner than the enforced one."""
		self.a_locked_march()
		self.tool_data("create_journal_entry", self.an_entry_into_march())
		advisory_alerts = self.alerts_for("period_close_lockdown")
		self.assertEqual(len(advisory_alerts), 1)
		self.assertEqual(advisory_alerts[0]["source_doctype"], "Journal Entry")
		self.assertIn("advisory", advisory_alerts[0]["alert_message"])

		self.enforce("period_close_lockdown")
		self.tool_error("create_journal_entry", self.an_entry_into_march())
		blocked_alerts = self.alerts_for("period_close_lockdown")
		self.assertEqual(len(blocked_alerts), 1, "the refusal wrote a second alert instead of refreshing")
		self.assertIn("blocked", blocked_alerts[0]["alert_message"])
		self.assertEqual(blocked_alerts[0]["severity"], enforcement.SEVERITY_BLOCKED)

	def test_an_enforced_refusal_survives_the_dispatchers_rollback(self):
		"""THE BUG THIS TEST WAS WRITTEN FOR, and it is worth stating in full.

		`registry.dispatch` catches ToolError and calls `frappe.db.rollback()`
		before it logs, so that a half-built document cannot be committed by the
		framework at the end of a failed request. An alert written moments before
		`evaluate` raises is inside that transaction and goes with it — which
		would give an app that KEEPS the evidence when a control merely advised
		and DISCARDS IT WHEN THE CONTROL ACTUALLY FIRED. That inverts this
		module's whole claim, precisely for the refusal somebody will later be
		asked to explain.

		Asserted on a control firing for the FIRST time under enforcement, with no
		advisory run before it to leave a committed row behind, because that is the
		case where the alert would vanish completely rather than merely go stale.
		"""
		self.a_locked_march()
		self.enforce("period_close_lockdown")
		self.assertEqual(self.alerts_for("period_close_lockdown"), [], "fixture already had an alert")

		self.tool_error("create_journal_entry", self.an_entry_into_march())

		alerts = self.alerts_for("period_close_lockdown")
		self.assertEqual(len(alerts), 1, "the refusal's alert did not survive the rollback")
		self.assertEqual(alerts[0]["severity"], enforcement.SEVERITY_BLOCKED)
		self.assertIn("blocked", alerts[0]["alert_message"])

	def test_the_refused_entry_itself_is_still_rolled_back(self):
		"""The other half, and the reason the commit is narrow. Filing the alert
		must not also persist the document the refusal exists to prevent."""
		self.a_locked_march()
		self.enforce("period_close_lockdown")
		before = len(STORE.rows("Journal Entry"))
		self.tool_error("create_journal_entry", self.an_entry_into_march())
		self.assertEqual(len(STORE.rows("Journal Entry")), before)

	def test_advisory_does_not_force_a_commit_of_its_own(self):
		"""Nothing raises on the advisory path, so the framework commits the
		request as it always would. Forcing one would break the caller's own
		transaction boundary for no gain."""
		self.a_locked_march()
		before = STORE.committed
		self.tool_data("create_journal_entry", self.an_entry_into_march())
		# One commit, and it is the harness's own end-of-request commit rather
		# than a second one from inside the control.
		self.assertLessEqual(STORE.committed - before, 1)

	def test_a_disabled_control_evaluates_nothing_and_is_not_merely_advisory(self):
		"""Off and Advisory are different answers. Advisory still fills the
		calendar; an operator who wants silence has to say so explicitly."""
		self.a_locked_march()
		self.disable("period_close_lockdown")
		data = self.tool_data("create_journal_entry", self.an_entry_into_march())
		block = data["controls"]["controls"]["period_close_lockdown"]
		self.assertEqual(block["mode"], enforcement.OFF)
		self.assertEqual(block["action"], "none")
		self.assertEqual(self.alerts_for("period_close_lockdown"), [])

	def test_each_control_has_its_own_switch(self):
		"""An operation enforcing period locks while leaving duplicate detection
		advisory is the normal shape of a company growing into this."""
		self.a_locked_march()
		self.enforce("period_close_lockdown")
		self.assertEqual(enforcement.mode("period_close_lockdown"), enforcement.ENFORCED)
		self.assertEqual(enforcement.mode("journal_entry_duplicate"), enforcement.ADVISORY)

	def test_turning_a_control_up_through_the_tool_versions_the_rule(self):
		"""The date enforcement began, and who decided it, is the question an
		auditor asks about a control environment — and version-by-copy is already
		how this register answers it."""
		rule = frappe.db.get_all(
			"Compliance Rule", filters={"control_point": "period_close_lockdown"}, pluck="name"
		)[0]
		self.tool_data(
			"update_compliance_rule",
			{"name": rule, "enforcement_mode": "Enforced", "reason": "board resolution 2026-08"},
		)
		self.assertEqual(enforcement.mode("period_close_lockdown"), enforcement.ENFORCED)
		rows = frappe.db.get_all(
			"Compliance Rule",
			filters={"control_point": "period_close_lockdown"},
			fields=["name", "version", "enforcement_mode"],
		)
		self.assertEqual(len(rows), 2, "flipping the switch did not write a new version")


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheGateIsWiredIntoCreateJournalEntry(ControlsTestCase):
	"""A control that is not called from the write path is not a control.

	This class exists because the failure it guards against is silent: every
	control could be correct, every unit test green, and `create_journal_entry`
	simply never call any of them.
	"""

	def a_clean_entry(self, **overrides):
		payload = {
			"company": MAIN,
			"posting_date": TODAY,
			"user_remark": "supplies",
			"accounts": [
				{"account": supplies(MAIN_ABBR), "debit": 250},
				{"account": cash(MAIN_ABBR), "credit": 250},
			],
		}
		payload.update(overrides)
		return payload

	def test_a_clean_entry_still_reports_that_the_controls_ran(self):
		"""A caller has to be able to tell 'the controls looked and found
		nothing' from 'the controls did not run'."""
		data = self.tool_data("create_journal_entry", self.a_clean_entry())
		self.assertTrue(data["controls"]["evaluated"])
		self.assertTrue(data["controls"]["clear"])
		self.assertEqual(data["controls"]["finding_count"], 0)
		self.assertNotIn("controls_note", data)

	def test_every_control_point_appears_on_the_response(self):
		data = self.tool_data("create_journal_entry", self.a_clean_entry())
		for control_point in (
			"period_close_lockdown",
			"approval_threshold",
			"segregation_of_duties",
			"journal_entry_duplicate",
			"journal_entry_unusual_amount",
		):
			self.assertIn(control_point, data["controls"]["controls"])

	def test_a_self_approved_entry_is_found(self):
		data = self.tool_data(
			"create_journal_entry",
			self.a_clean_entry(prepared_by="ana@example.test", approved_by="ana@example.test"),
		)
		block = data["controls"]["controls"]["segregation_of_duties"]
		self.assertEqual(block["finding_count"], 1)
		self.assertIn("prepared and approved", block["findings"][0]["message"])

	def test_a_second_pair_of_hands_clears_it(self):
		data = self.tool_data(
			"create_journal_entry",
			self.a_clean_entry(prepared_by="ana@example.test", approved_by="ben@example.test"),
		)
		self.assertEqual(data["controls"]["controls"]["segregation_of_duties"]["finding_count"], 0)

	def test_a_spend_above_the_chain_is_found_and_names_the_role(self):
		self.a_threshold()
		data = self.tool_data(
			"create_journal_entry",
			self.a_clean_entry(
				accounts=[
					{"account": supplies(MAIN_ABBR), "debit": 20000},
					{"account": cash(MAIN_ABBR), "credit": 20000},
				]
			),
		)
		block = data["controls"]["controls"]["approval_threshold"]
		self.assertEqual(block["finding_count"], 1)
		self.assertIn("Accounts Manager", block["findings"][0]["message"])

	def test_a_spend_under_the_auto_approve_floor_says_nothing(self):
		"""The field that makes this control survivable."""
		self.a_threshold()
		data = self.tool_data("create_journal_entry", self.a_clean_entry())
		self.assertEqual(data["controls"]["controls"]["approval_threshold"]["finding_count"], 0)

	def test_a_second_identical_entry_is_flagged_as_a_duplicate(self):
		self.tool_data("create_journal_entry", self.a_clean_entry())
		data = self.tool_data("create_journal_entry", self.a_clean_entry())
		block = data["controls"]["controls"]["journal_entry_duplicate"]
		self.assertEqual(block["finding_count"], 1)
		self.assertIn("already on the books", block["findings"][0]["message"])

	def test_entries_from_different_bank_transactions_are_not_flagged(self):
		"""v0.184.0. Two identical charges on one day, each booked from its own
		Bank Transaction, file no duplicate alert."""
		self.tool_data("create_journal_entry", self.a_clean_entry(cheque_no="ACC-BTN-2026-00001"))
		before_alerts = len(STORE.rows("Compliance Alert"))
		data = self.tool_data("create_journal_entry", self.a_clean_entry(cheque_no="ACC-BTN-2026-00002"))
		block = data["controls"]["controls"]["journal_entry_duplicate"]
		self.assertEqual(block["finding_count"], 0)
		self.assertEqual(len(STORE.rows("Compliance Alert")), before_alerts)

	def test_the_same_bank_transaction_booked_twice_is_flagged(self):
		self.tool_data("create_journal_entry", self.a_clean_entry(cheque_no="ACC-BTN-2026-00001"))
		data = self.tool_data("create_journal_entry", self.a_clean_entry(cheque_no="ACC-BTN-2026-00001"))
		block = data["controls"]["controls"]["journal_entry_duplicate"]
		self.assertEqual(block["finding_count"], 1)
		self.assertIn("ACC-BTN-2026-00001", block["findings"][0]["message"])

	def test_a_bank_fed_entry_and_an_unreferenced_copy_are_flagged(self):
		self.tool_data("create_journal_entry", self.a_clean_entry(cheque_no="ACC-BTN-2026-00001"))
		data = self.tool_data("create_journal_entry", self.a_clean_entry())
		self.assertEqual(data["controls"]["controls"]["journal_entry_duplicate"]["finding_count"], 1)

	def test_the_preview_reads_the_existing_entrys_bank_reference(self):
		first = self.tool_data("create_journal_entry", self.a_clean_entry(cheque_no="ACC-BTN-2026-00001"))
		second = self.tool_data("create_journal_entry", self.a_clean_entry(cheque_no="ACC-BTN-2026-00002"))
		data = self.tool_data(
			"check_journal_entry_controls",
			{"journal_entry": second["name"], "company": MAIN, "posting_date": TODAY},
		)
		self.assertEqual(data["controls"]["journal_entry_duplicate"]["finding_count"], 0)
		# The negative control: the same entry, asked about as if it carried the
		# first one's reference, IS a duplicate — so the zero above is the BTN
		# rule, not a preview that cannot see the other entry.
		data = self.tool_data(
			"check_journal_entry_controls",
			{
				"journal_entry": second["name"],
				"company": MAIN,
				"posting_date": TODAY,
				"cheque_no": "ACC-BTN-2026-00001",
			},
		)
		self.assertEqual(data["controls"]["journal_entry_duplicate"]["finding_count"], 1)
		self.assertEqual(
			data["controls"]["journal_entry_duplicate"]["findings"][0]["detail"]["name"], first["name"]
		)

	def test_the_preview_writes_nothing_and_files_no_alert(self):
		"""`check_journal_entry_controls` is how somebody decides whether to
		enforce, so asking has to be free."""
		self.a_period(items=[])
		created = frappe.db.get_all("Closing Checklist", pluck="name")[0]
		self.tool_data("close_accounting_period", {"name": created})
		before_entries = len(STORE.rows("Journal Entry"))
		before_alerts = len(STORE.rows("Compliance Alert"))
		data = self.tool_data(
			"check_journal_entry_controls",
			{"company": MAIN, "posting_date": "2026-03-15", "total": 250, "accounts": [cash(MAIN_ABBR)]},
		)
		self.assertEqual(data["finding_count"], 1)
		self.assertEqual(len(STORE.rows("Journal Entry")), before_entries)
		self.assertEqual(len(STORE.rows("Compliance Alert")), before_alerts)

	def test_the_preview_reports_would_block_without_blocking(self):
		self.a_period(items=[])
		created = frappe.db.get_all("Closing Checklist", pluck="name")[0]
		self.tool_data("close_accounting_period", {"name": created})
		self.enforce("period_close_lockdown")
		data = self.tool_data(
			"check_journal_entry_controls",
			{"company": MAIN, "posting_date": "2026-03-15", "total": 250, "accounts": [cash(MAIN_ABBR)]},
		)
		self.assertEqual(data["would_block"], ["period_close_lockdown"])

	def test_closing_a_period_with_a_step_outstanding_is_advisory_by_default(self):
		created = self.a_period()
		data = self.tool_data("close_accounting_period", {"name": created["checklist"]["name"]})
		self.assertTrue(data["locked"], "the period did not lock in advisory mode")
		self.assertEqual(data["control"]["finding_count"], 2)

	def test_enforced_refuses_the_close_and_the_period_stays_open(self):
		created = self.a_period()
		self.enforce("closing_checklist")
		name = created["checklist"]["name"]
		error = self.tool_error("close_accounting_period", {"name": name})
		self.assertIn("REFUSED", error)
		self.assertIn("Reconcile every bank account", error)
		read = self.tool_data("get_closing_checklist", {"name": name})
		self.assertFalse(read["locked"])

	def test_a_clean_checklist_closes_under_enforcement(self):
		created = self.a_period()
		name = created["checklist"]["name"]
		self.enforce("closing_checklist")
		for step in ("Reconcile every bank account", "Post accruals"):
			self.tool_data("complete_checklist_item", {"name": name, "step": step})
		data = self.tool_data("close_accounting_period", {"name": name})
		self.assertTrue(data["locked"])
		self.assertEqual(data["control"]["finding_count"], 0)

	def test_a_step_that_is_not_required_does_not_hold_the_close(self):
		created = self.a_period()
		name = created["checklist"]["name"]
		self.enforce("closing_checklist")
		for step in ("Reconcile every bank account", "Post accruals"):
			self.tool_data("complete_checklist_item", {"name": name, "step": step})
		# "Email the lender" is deliberately left undone.
		self.assertTrue(self.tool_data("close_accounting_period", {"name": name})["locked"])


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheRegisterReadsBack(ControlsTestCase):
	def test_every_control_point_is_seeded_as_a_rule(self):
		for control_point in enforcement.CONTROL_POINTS:
			self.assertTrue(
				frappe.db.get_all("Compliance Rule", filters={"control_point": control_point}, pluck="name"),
				f"{control_point} has no seeded rule, so nothing could ever turn it on",
			)

	def test_a_rule_naming_a_control_this_app_does_not_implement_is_refused(self):
		"""The worst failure the register can produce: an operator reads that the
		site enforces something, believes it, and tells an auditor so."""
		doc = frappe.new_doc("Compliance Rule")
		doc.rule_id = "made_up"
		doc.title = "A control nothing consults"
		doc.kairotic_gate_description = "never"
		doc.control_point = "there_is_no_such_control"
		with self.assertRaises(Exception) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("control point", str(caught.exception).lower())

	def test_a_gate_may_not_also_carry_a_scanner(self):
		doc = frappe.new_doc("Compliance Rule")
		doc.rule_id = "hybrid"
		doc.title = "Both at once"
		doc.kairotic_gate_description = "never"
		doc.control_point = "approval_threshold"
		doc.builtin_scanner = "certification_expiring"
		with self.assertRaises(Exception) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("is a gate", str(caught.exception).lower())
		self.assertIn("never scanned", str(caught.exception).lower())

	def test_a_swept_rule_has_its_enforcement_mode_blanked(self):
		"""A register sorted by enforcement must not show every cadence rule as
		'Advisory' — that would read as a claim they are gates somebody softened."""
		rows = frappe.db.get_all(
			"Compliance Rule",
			filters={"rule_id": "certification_expiring"},
			fields=["control_point", "enforcement_mode"],
		)
		self.assertFalse(rows[0].get("control_point"))
		self.assertFalse(rows[0].get("enforcement_mode"))

	def test_the_register_separates_enforced_from_advisory_from_off(self):
		self.enforce("period_close_lockdown")
		self.disable("journal_entry_duplicate")
		data = self.tool_data("list_control_points", {})
		self.assertIn("period_close_lockdown", data["enforced"])
		self.assertIn("journal_entry_duplicate", data["off"])
		self.assertIn("approval_threshold", data["advisory"])
		self.assertEqual(data["control_count"], len(enforcement.CONTROL_POINTS))

	def test_the_register_says_what_advisory_means(self):
		"""The note is the interface. An operator who reads 'Advisory' as 'the
		control is weaker' will never turn one up."""
		data = self.tool_data("list_control_points", {})
		self.assertIn("SAME evaluation", data["note"])

	def test_every_control_point_carries_a_citation(self):
		for key, spec in enforcement.CONTROL_POINTS.items():
			self.assertTrue(spec.citation, f"{key} has no citation")
			self.assertTrue(spec.purpose, f"{key} has no purpose")
			self.assertTrue(spec.blocks, f"{key} does not say what it refuses")

	def test_every_gate_seed_fills_every_mandatory_column(self):
		"""v0.87.1. Read from the DocType meta, not from a list written here.

		THE FAILURE THIS CATCHES ALREADY HAPPENED, and it ran from v0.80.0 to
		v0.87.0. `target_doctype` has been `reqd` on Compliance Rule since well
		before gates existed; the thirteen gate seeds never filled it, because a
		gate is not scanned and nothing in `enforcement` needed one. On a real bench
		every one of them raised MandatoryError on migrate.

		IT FAILED SILENTLY, WHICH IS WHY IT LASTED SEVEN RELEASES.
		`seed_compliance_rules` never raises, by design, because it runs inside
		`bench migrate` where an exception aborts the migration for the whole bench
		— so the thirteen went into the report's `failed` list, which nobody reads,
		and an operator's register simply had no IPO readiness controls in it. A
		control an operator believes they have and does not is the failure
		`enforcement`'s own docstring calls the worst one available.

		THIS SUITE COULD NOT HAVE CAUGHT IT BEFORE. The standalone harness does not
		enforce `reqd` — it is a test double, not an emulator — so the seeds
		inserted here perfectly well while failing on every real site. Hence a test
		that reads the requirement off the meta and checks the spec dicts directly,
		rather than one that inserts and trusts the double to refuse.

		Deriving the mandatory set from the meta rather than naming `target_doctype`
		means the NEXT column somebody marks required is caught here, at the point
		of marking it, rather than on a site.
		"""
		mandatory = [
			field.fieldname
			for field in frappe.get_meta(compliance_rules.DOCTYPE).fields
			if getattr(field, "reqd", 0)
		]
		self.assertIn("target_doctype", mandatory, "did target_doctype stop being required?")
		for spec in enforcement.seed_specs():
			for fieldname in mandatory:
				with self.subTest(rule_id=spec["rule_id"], field=fieldname):
					self.assertTrue(
						str(spec.get(fieldname) or "").strip(),
						f"{spec['rule_id']} leaves the mandatory {fieldname} empty — it will not "
						"insert on a real bench, and the seeder will swallow the failure.",
					)

	def test_every_gate_targets_a_doctype_that_exists(self):
		"""A target naming nothing would make `available` false for ever, which
		reads on the register as a control the site cannot run — indistinguishable
		from one that is simply quiet."""
		for key, spec in enforcement.CONTROL_POINTS.items():
			with self.subTest(control_point=key):
				self.assertTrue(spec.target_doctype, f"{key} names no target doctype")
				self.assertTrue(
					frappe.db.exists("DocType", spec.target_doctype),
					f"{key} targets {spec.target_doctype!r}, which is not a DocType this site has.",
				)

	def test_gates_are_never_swept(self):
		"""A gate handed to the declarative scanner would put a 'could not be
		assembled' note in front of an operator every night."""
		from erpnext_mcp.alerts import engine

		assembled, _notes = engine.rule_set()
		for control_point in enforcement.CONTROL_POINTS:
			self.assertNotIn(f"control_{control_point}", assembled)
