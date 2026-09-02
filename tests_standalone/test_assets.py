# SPDX-License-Identifier: MIT
"""Assets with a usage split, and depreciation that follows it.

Four things these tests are really about.

ERPNEXT MUST NOT ALSO BE DEPRECIATING. `create_asset` writes
`calculate_depreciation = 0`, because ERPNext's own daily job posts for every
asset that has it set. If that line were ever removed, the asset would
depreciate twice a month, silently, and no other test in this repository would
notice. So there is one here that reads the flag off the stored Asset.

THE SPLIT HAS TO ADD UP. A period of 1000 across 33.33 / 33.33 / 33.34 is three
debits that must total exactly 1000, because a journal entry that does not
balance is not a rounding problem, it is a refused save. The last debit absorbs
the drift and a test asserts on the arithmetic.

RUNNING TWICE MUST NOT POST TWICE. Every period written is recorded on the
profile with the entry that carries it, and the second run skips it. This is the
property that makes a scheduled catch-up safe to re-run, so it is tested by
running the tool twice and counting journal entries.

THE TENOR CHECK IS THE POINT OF `link_asset_to_note`. An asset whose life and
whose note have parted company is invisible until the last year of the loan.
Refusing at the moment of linking is the only cheap place to catch it, so the
refusal — and the deliberate override — both have tests.
"""

from .fixtures import (
	ACCUMULATED_DEPRECIATION,
	ASSET_CATEGORY,
	DEPRECIATION_EXPENSE,
	HARVEST,
	MAIN,
	MAIN_ABBR,
	OTHER_ABBR,
	V7TestCase,
	install_bbch_dimension,
)
from .harness import STORE, add_field, frappe

FIELD_WORK = f"110 - Field Work - {MAIN_ABBR}"
MAIN_CC = f"Main - {MAIN_ABBR}"
OPERATIONS = f"100 - Operations - {MAIN_ABBR}"
RETIRED_CC = f"190 - Retired Depot - {MAIN_ABBR}"

ALL_ON = {
	"allow_create_asset": 1,
	"allow_update_asset_allocation": 1,
	"allow_link_asset_to_note": 1,
	"allow_run_depreciation_cycle": 1,
	"allow_depreciation_note_alignment_check": 1,
}


class AssetTestCase(V7TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def create(self, **overrides):
		payload = {
			"company": MAIN,
			"asset_name": "Tractor A",
			"item_code": "TRACTOR-A",
			"asset_category": ASSET_CATEGORY,
			"purchase_date": "2026-01-01",
			"purchase_amount": 12000,
			"useful_life_months": 12,
			"cost_center_allocation": [
				{"cost_center": FIELD_WORK, "percentage": 40},
				{"cost_center": MAIN_CC, "percentage": 60},
			],
		}
		payload.update(overrides)
		return payload

	def an_asset(self, **overrides):
		return self.tool_data("create_asset", self.create(**overrides))

	def entries(self):
		return STORE.rows("Journal Entry")


# ── create_asset ────────────────────────────────────────────────────────────
class CreateAsset(AssetTestCase):
	def test_it_creates_the_asset_the_item_and_the_profile(self):
		data = self.an_asset()
		self.assertTrue(frappe.db.exists("Asset", data["asset"]))
		self.assertTrue(frappe.db.exists("Asset Cost Profile", data["profile"]))
		self.assertEqual(data["item_code"], "TRACTOR-A")
		self.assertTrue(data["item_created"])
		self.assertEqual(data["docstatus"], 0)

	def test_erpnexts_own_depreciation_is_switched_off_on_the_asset(self):
		"""If this ever regressed, ERPNext's daily job and run_depreciation_cycle
		would both post and the asset would depreciate twice a month."""
		data = self.an_asset()
		asset = frappe.get_doc("Asset", data["asset"])
		self.assertEqual(int(asset.calculate_depreciation or 0), 0)
		self.assertTrue(data["erpnext_depreciation_disabled"])

	def test_the_profile_carries_the_split_and_the_schedule(self):
		data = self.an_asset()
		self.assertEqual(data["period_count"], 12)
		self.assertEqual(data["per_period_amount"], 1000.0)
		self.assertEqual(data["final_period_ends"], "2026-12-31")
		self.assertEqual(
			[(row["cost_center"], row["percentage"]) for row in data["cost_center_allocation"]],
			[(FIELD_WORK, 40.0), (MAIN_CC, 60.0)],
		)

	def test_the_assets_own_cost_center_is_the_largest_share(self):
		data = self.an_asset()
		self.assertEqual(frappe.db.get_value("Asset", data["asset"], "cost_center"), MAIN_CC)

	def test_a_salvage_value_shortens_what_is_depreciated_not_the_life(self):
		data = self.an_asset(salvage_value=1200)
		self.assertEqual(data["period_count"], 12)
		self.assertEqual(data["per_period_amount"], 900.0)

	def test_an_annual_frequency_gives_annual_periods(self):
		data = self.an_asset(useful_life_months=36, depreciation_frequency_months=12)
		self.assertEqual(data["period_count"], 3)
		self.assertEqual(data["per_period_amount"], 4000.0)
		self.assertEqual(data["final_period_ends"], "2028-12-31")

	def test_a_frequency_that_does_not_divide_the_life_is_refused(self):
		message = self.tool_error(
			"create_asset", self.create(useful_life_months=10, depreciation_frequency_months=12)
		)
		self.assertIn("stub period", message)
		self.assertIn("Nothing was created", message)

	def test_an_allocation_that_does_not_total_a_hundred_is_refused(self):
		message = self.tool_error(
			"create_asset",
			self.create(
				cost_center_allocation=[
					{"cost_center": FIELD_WORK, "percentage": 40},
					{"cost_center": MAIN_CC, "percentage": 50},
				]
			),
		)
		self.assertIn("90", message)
		self.assertIn("under-depreciates", message)
		self.assertEqual(STORE.rows("Asset"), [])

	def test_a_group_cost_center_is_refused(self):
		message = self.tool_error(
			"create_asset",
			self.create(cost_center_allocation=[{"cost_center": OPERATIONS, "percentage": 100}]),
		)
		self.assertIn("group cost center", message)

	def test_a_disabled_cost_center_is_refused(self):
		message = self.tool_error(
			"create_asset",
			self.create(cost_center_allocation=[{"cost_center": RETIRED_CC, "percentage": 100}]),
		)
		self.assertIn("disabled", message)

	def test_the_same_cost_center_twice_is_refused(self):
		message = self.tool_error(
			"create_asset",
			self.create(
				cost_center_allocation=[
					{"cost_center": FIELD_WORK, "percentage": 50},
					{"cost_center": FIELD_WORK, "percentage": 50},
				]
			),
		)
		self.assertIn("twice", message)

	def test_omitting_the_allocation_falls_back_to_the_company_default(self):
		payload = self.create()
		payload.pop("cost_center_allocation")
		data = self.tool_data("create_asset", payload)
		self.assertEqual(
			data["cost_center_allocation"],
			[{"cost_center": MAIN_CC, "percentage": 100.0, "bbch_stage": None, "note": None}],
		)

	def test_an_unknown_asset_category_is_refused_with_what_the_site_has(self):
		message = self.tool_error("create_asset", self.create(asset_category="Spaceships"))
		self.assertIn(ASSET_CATEGORY, message)
		# v0.147.0. A refusal that names the site's categories and not the tool that
		# makes one sends the reader to the Desk for something the MCP can now do.
		self.assertIn("create_asset_category", message)

	def test_a_salvage_value_at_or_above_the_cost_is_refused(self):
		message = self.tool_error("create_asset", self.create(salvage_value=12000))
		self.assertIn("less than purchase_amount", message)

	def test_an_existing_item_that_is_not_a_fixed_asset_is_refused(self):
		STORE.seed("Item", [{"name": "SEEDS", "item_code": "SEEDS", "is_fixed_asset": 0}])
		message = self.tool_error("create_asset", self.create(item_code="SEEDS"))
		self.assertIn("not flagged as a fixed asset", message)
		self.assertIn("inventory decision", message)

	def test_an_existing_fixed_asset_item_is_reused(self):
		STORE.seed("Item", [{"name": "TRACTOR-A", "item_code": "TRACTOR-A", "is_fixed_asset": 1}])
		data = self.an_asset()
		self.assertFalse(data["item_created"])

	def test_item_creation_can_be_refused_instead(self):
		message = self.tool_error("create_asset", self.create(create_item_if_missing=False))
		self.assertIn("create_item_if_missing is false", message)

	def test_a_bbch_stage_the_site_cannot_carry_is_refused(self):
		message = self.tool_error(
			"create_asset",
			self.create(
				cost_center_allocation=[
					{"cost_center": FIELD_WORK, "percentage": 100, "bbch_stage": "BBCH-8"}
				]
			),
		)
		self.assertIn("create_accounting_dimension", message)

	def test_a_bbch_stage_is_kept_when_the_site_has_the_dimension(self):
		install_bbch_dimension()
		data = self.an_asset(
			cost_center_allocation=[{"cost_center": FIELD_WORK, "percentage": 100, "bbch_stage": "BBCH-8"}]
		)
		self.assertEqual(data["cost_center_allocation"][0]["bbch_stage"], "BBCH-8")

	def test_a_note_whose_tenor_disagrees_with_the_life_is_refused_before_anything_is_written(self):
		message = self.tool_error(
			"create_asset",
			self.create(linked_note="ACC-JV-2026-00001", note_tenor_months=84),
		)
		self.assertIn("84", message)
		self.assertIn("same month", message)
		self.assertEqual(STORE.rows("Asset"), [])

	def test_a_note_whose_tenor_matches_is_linked_at_creation(self):
		data = self.an_asset(linked_note="ACC-JV-2026-00001", note_tenor_months=12)
		self.assertEqual(data["linked_note"], "ACC-JV-2026-00001")
		self.assertEqual(data["note_tenor_months"], 12)
		self.assertEqual(
			frappe.db.get_value("Asset Cost Profile", data["profile"], "note_maturity_date"),
			"2027-01-01",
		)


# ── update_asset_allocation ─────────────────────────────────────────────────
class UpdateAssetAllocation(AssetTestCase):
	def setUp(self):
		super().setUp()
		self.asset = self.an_asset()["asset"]

	def test_it_replaces_the_split(self):
		data = self.tool_data(
			"update_asset_allocation",
			{
				"asset": self.asset,
				"new_cost_center_allocation": [
					{"cost_center": FIELD_WORK, "percentage": 70},
					{"cost_center": MAIN_CC, "percentage": 30},
				],
			},
		)
		self.assertEqual([row["percentage"] for row in data["cost_center_allocation"]], [70.0, 30.0])
		self.assertEqual([row["percentage"] for row in data["previous_allocation"]], [40.0, 60.0])

	def test_the_asset_can_be_named_by_its_asset_name(self):
		data = self.tool_data(
			"update_asset_allocation",
			{
				"asset": "Tractor A",
				"new_cost_center_allocation": [{"cost_center": FIELD_WORK, "percentage": 100}],
			},
		)
		self.assertEqual(data["asset"], self.asset)

	def test_a_split_that_does_not_total_a_hundred_is_refused(self):
		message = self.tool_error(
			"update_asset_allocation",
			{
				"asset": self.asset,
				"new_cost_center_allocation": [{"cost_center": FIELD_WORK, "percentage": 80}],
			},
		)
		self.assertIn("80", message)

	def test_the_same_split_again_is_refused_rather_than_reported_as_a_change(self):
		message = self.tool_error(
			"update_asset_allocation",
			{
				"asset": self.asset,
				"new_cost_center_allocation": [
					{"cost_center": MAIN_CC, "percentage": 60},
					{"cost_center": FIELD_WORK, "percentage": 40},
				],
			},
		)
		self.assertIn("already has exactly that allocation", message)

	def test_an_asset_with_no_profile_is_refused_with_the_reason(self):
		STORE.seed("Asset", [{"name": "A-EXISTING", "asset_name": "Old Sprayer", "company": MAIN}])
		message = self.tool_error(
			"update_asset_allocation",
			{
				"asset": "A-EXISTING",
				"new_cost_center_allocation": [{"cost_center": FIELD_WORK, "percentage": 100}],
			},
		)
		self.assertIn("no Asset Cost Profile", message)


# ── link_asset_to_note ──────────────────────────────────────────────────────
class LinkAssetToNote(AssetTestCase):
	def setUp(self):
		super().setUp()
		self.asset = self.an_asset()["asset"]

	def test_a_matching_tenor_links_and_says_so(self):
		data = self.tool_data(
			"link_asset_to_note",
			{"asset": self.asset, "note_doc_ref": "ACC-JV-2026-00001", "note_tenor_months": 12},
		)
		self.assertEqual(data["delta_months"], 0)
		self.assertTrue(data["tenor_enforced"])
		self.assertIn("same month", data["note"])

	def test_a_divergence_is_refused_with_the_numbers(self):
		message = self.tool_error(
			"link_asset_to_note",
			{"asset": self.asset, "note_doc_ref": "ACC-JV-2026-00001", "note_tenor_months": 60},
		)
		self.assertIn("48-month divergence", message)
		self.assertIn("matching principle", message)
		self.assertIsNone(frappe.db.get_value("Asset Cost Profile", self.asset, "linked_note"))

	def test_a_divergence_can_be_accepted_deliberately(self):
		data = self.tool_data(
			"link_asset_to_note",
			{
				"asset": self.asset,
				"note_doc_ref": "ACC-JV-2026-00001",
				"note_tenor_months": 60,
				"enforce_tenor": False,
			},
		)
		self.assertEqual(data["delta_months"], -48)
		self.assertFalse(data["tenor_enforced"])
		self.assertEqual(
			frappe.db.get_value("Asset Cost Profile", self.asset, "linked_note"), "ACC-JV-2026-00001"
		)

	def test_a_maturity_date_is_turned_into_a_tenor(self):
		data = self.tool_data(
			"link_asset_to_note",
			{
				"asset": self.asset,
				"note_doc_ref": "ACC-JV-2026-00001",
				"note_maturity_date": "2027-01-01",
			},
		)
		self.assertEqual(data["note_tenor_months"], 12)

	def test_a_note_with_no_recorded_term_is_refused_with_what_to_pass(self):
		message = self.tool_error(
			"link_asset_to_note", {"asset": self.asset, "note_doc_ref": "ACC-JV-2026-00001"}
		)
		self.assertIn("note_tenor_months", message)

	def test_an_unknown_note_is_refused(self):
		message = self.tool_error(
			"link_asset_to_note",
			{"asset": self.asset, "note_doc_ref": "NOTE-NOPE", "note_tenor_months": 12},
		)
		self.assertIn("note_doctype", message)


# ── run_depreciation_cycle ──────────────────────────────────────────────────
class RunDepreciationCycle(AssetTestCase):
	def setUp(self):
		super().setUp()
		self.asset = self.an_asset()["asset"]

	def run_cycle(self, **overrides):
		payload = {"company": MAIN, "period_end": "2026-03-31"}
		payload.update(overrides)
		return self.tool_data("run_depreciation_cycle", payload)

	def test_it_is_a_dry_run_by_default_and_writes_nothing(self):
		before = len(self.entries())
		data = self.run_cycle()
		self.assertTrue(data["dry_run"])
		self.assertEqual(data["period_count"], 3)
		self.assertEqual(data["total_depreciation"], 3000.0)
		self.assertEqual(data["journal_entries"], [])
		self.assertEqual(len(self.entries()), before)

	def test_a_real_run_writes_one_draft_entry_per_period_split_by_the_allocation(self):
		data = self.run_cycle(dry_run=False)
		self.assertEqual(len(data["journal_entries"]), 3)
		entry = frappe.get_doc("Journal Entry", data["journal_entries"][0])
		self.assertEqual(int(entry.docstatus or 0), 0)
		self.assertEqual(str(entry.posting_date), "2026-01-31")
		debits = {
			line["account"] + "|" + line["cost_center"]: line["debit"]
			for line in entry.accounts
			if line.get("debit")
		}
		self.assertEqual(
			debits,
			{
				f"{DEPRECIATION_EXPENSE}|{FIELD_WORK}": 400.0,
				f"{DEPRECIATION_EXPENSE}|{MAIN_CC}": 600.0,
			},
		)
		credits = [line for line in entry.accounts if line.get("credit")]
		self.assertEqual(len(credits), 1)
		self.assertEqual(credits[0]["account"], ACCUMULATED_DEPRECIATION)
		self.assertEqual(credits[0]["credit"], 1000.0)

	def test_the_periods_written_are_recorded_on_the_profile(self):
		data = self.run_cycle(dry_run=False)
		profile = frappe.get_doc("Asset Cost Profile", self.asset)
		self.assertEqual([int(row["period_index"]) for row in profile.depreciation_postings], [1, 2, 3])
		self.assertEqual(
			[row["journal_entry"] for row in profile.depreciation_postings], data["journal_entries"]
		)

	def test_running_twice_does_not_post_a_period_twice(self):
		self.run_cycle(dry_run=False)
		count = len(self.entries())
		again = self.run_cycle(dry_run=False)
		self.assertEqual(again["period_count"], 0)
		self.assertEqual(len(self.entries()), count)
		self.assertIn("already written", again["assets_skipped"][0]["reason"])

	def test_a_later_run_picks_up_only_the_new_periods(self):
		self.run_cycle(dry_run=False)
		later = self.run_cycle(period_end="2026-05-31", dry_run=False)
		self.assertEqual(later["period_count"], 2)
		self.assertEqual([period["period_index"] for period in later["periods"]], [4, 5])

	def test_a_three_way_split_still_balances_to_the_cent(self):
		"""33.33 / 33.33 / 33.34 of 1000 is three debits that must total exactly
		1000. The last one absorbs the drift; without that the entry does not
		balance and ERPNext refuses the save."""
		STORE.tables["Asset Cost Profile"].clear()
		STORE.tables["Asset"].clear()
		self.an_asset(
			asset_name="Tractor B",
			item_code="TRACTOR-B",
			cost_center_allocation=[
				{"cost_center": FIELD_WORK, "percentage": 33.33},
				{"cost_center": MAIN_CC, "percentage": 33.33},
				{"cost_center": HARVEST, "percentage": 33.34},
			],
		)
		data = self.run_cycle(dry_run=False)
		entry = frappe.get_doc("Journal Entry", data["journal_entries"][0])
		debits = [line["debit"] for line in entry.accounts if line.get("debit")]
		self.assertEqual(debits, [333.3, 333.3, 333.4])
		self.assertEqual(round(sum(debits), 2), 1000.0)
		self.assertEqual(float(entry.total_debit or sum(debits)), round(sum(debits), 2))

	def test_an_asset_whose_accounts_are_not_configured_is_skipped_not_fatal(self):
		frappe.db.set_value("Asset", self.asset, "asset_category", "Unconfigured")
		data = self.run_cycle()
		self.assertEqual(data["period_count"], 0)
		self.assertIn("Asset Category", data["assets_skipped"][0]["reason"])

	def test_a_manual_asset_is_left_alone(self):
		frappe.db.set_value("Asset Cost Profile", self.asset, "depreciation_method", "Manual")
		data = self.run_cycle()
		self.assertEqual(data["period_count"], 0)
		self.assertIn("Manual", data["assets_skipped"][0]["reason"])

	def test_the_run_can_be_narrowed_to_one_asset(self):
		self.an_asset(asset_name="Sprayer", item_code="SPRAYER-1")
		data = self.run_cycle(asset="Sprayer")
		self.assertEqual({period["asset"] for period in data["periods"]}, {"A-00002"})

	def test_nothing_is_depreciated_before_the_first_period_ends(self):
		data = self.run_cycle(period_end="2026-01-15")
		self.assertEqual(data["period_count"], 0)

	def test_a_declining_balance_lands_exactly_on_the_salvage_value(self):
		STORE.tables["Asset Cost Profile"].clear()
		STORE.tables["Asset"].clear()
		data = self.an_asset(
			asset_name="Tractor C",
			item_code="TRACTOR-C",
			salvage_value=1200,
			depreciation_method="Written Down Value",
		)
		profile = frappe.db.get_value(
			"Asset Cost Profile", data["profile"], ["gross_purchase_amount", "salvage_value"], as_dict=True
		)
		cycle = self.run_cycle(period_end="2027-12-31", dry_run=False)
		written = sum(period["amount"] for period in cycle["periods"])
		self.assertAlmostEqual(
			written, float(profile["gross_purchase_amount"]) - float(profile["salvage_value"]), places=2
		)

	def test_written_down_value_with_no_salvage_is_refused_rather_than_fudged(self):
		frappe.db.set_value("Asset Cost Profile", self.asset, "depreciation_method", "Written Down Value")
		data = self.run_cycle()
		self.assertIn("undefined", data["assets_skipped"][0]["reason"])


# ── depreciation_note_alignment_check ───────────────────────────────────────
class NoteAlignment(AssetTestCase):
	def setUp(self):
		super().setUp()
		self.asset = self.an_asset()["asset"]

	def test_an_asset_with_no_note_is_listed_as_such(self):
		data = self.tool_data("depreciation_note_alignment_check", {"company": MAIN, "as_of": "2026-07-01"})
		self.assertEqual(data["checked"], 0)
		self.assertEqual(data["assets_without_a_note"], [self.asset])

	def test_an_aligned_asset_reads_as_aligned(self):
		self.tool_data(
			"link_asset_to_note",
			{"asset": self.asset, "note_doc_ref": "ACC-JV-2026-00001", "note_tenor_months": 12},
		)
		data = self.tool_data("depreciation_note_alignment_check", {"company": MAIN, "as_of": "2026-07-01"})
		self.assertEqual(data["checked"], 1)
		self.assertEqual(data["diverged_count"], 0)
		self.assertTrue(data["assets"][0]["aligned"])
		self.assertEqual(data["assets"][0]["months_elapsed"], 6)
		self.assertEqual(data["assets"][0]["remaining_depreciation_months"], 6)

	def test_a_divergence_is_reported_with_which_way_it_reads(self):
		self.tool_data(
			"link_asset_to_note",
			{
				"asset": self.asset,
				"note_doc_ref": "ACC-JV-2026-00001",
				"note_tenor_months": 24,
				"note_maturity_date": "2028-01-01",
				"enforce_tenor": False,
			},
		)
		data = self.tool_data("depreciation_note_alignment_check", {"company": MAIN, "as_of": "2026-07-01"})
		self.assertEqual(data["diverged_count"], 1)
		self.assertEqual(data["assets"][0]["delta_months"], -12)
		self.assertIn("interest is still being paid", data["assets"][0]["reading"])


# ── the switches ────────────────────────────────────────────────────────────
class AssetSwitches(V7TestCase):
	def test_every_asset_write_tool_is_off_until_it_is_turned_on(self):
		self.configure(enabled=1)
		for tool in (
			"create_asset",
			"update_asset_allocation",
			"link_asset_to_note",
			"run_depreciation_cycle",
		):
			with self.subTest(tool=tool):
				message = self.tool_error(tool, {})
				self.assertIn(f"allow_{tool}", message)

	def test_the_alignment_check_is_on_out_of_the_box(self):
		self.configure(enabled=1)
		data = self.tool_data("depreciation_note_alignment_check", {"company": MAIN})
		self.assertEqual(data["checked"], 0)


# ── create_asset_category ───────────────────────────────────────────────────
FIXED_ASSETS = f"1800 - Fixed Assets - {MAIN_ABBR}"


class CreateAssetCategory(V7TestCase):
	"""The master `create_asset` refuses without, and the reason it takes accounts.

	ERPNext marks the `accounts` table on Asset Category `reqd`, so a category
	made from a name alone cannot be saved on a bench at all — Frappe's mandatory
	check refuses it before the controller runs. The standalone double does NOT
	model that check, which is exactly why the refusal lives in the tool and is
	tested here: without it this suite would be green over a tool that fails on
	every real site.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_create_asset_category=1, allow_create_asset=1)
		# The fixture chart has an Accumulated Depreciation and a Depreciation
		# account and no Fixed Asset one, because nothing before this wrote the
		# column that needs it.
		STORE.seed(
			"Account",
			[
				{
					"name": FIXED_ASSETS,
					"account_name": "Fixed Assets",
					"account_number": "1800",
					"parent_account": f"Application of Funds (Assets) - {MAIN_ABBR}",
					"is_group": 0,
					"root_type": "Asset",
					"account_type": "Fixed Asset",
					"account_currency": "USD",
					"disabled": 0,
					"company": MAIN,
				}
			],
		)

	def payload(self, **overrides):
		payload = {
			"company": MAIN,
			"asset_category_name": "Wind Machines",
			"fixed_asset_account": FIXED_ASSETS,
			"accumulated_depreciation_account": ACCUMULATED_DEPRECIATION,
			"depreciation_expense_account": DEPRECIATION_EXPENSE,
		}
		payload.update(overrides)
		return payload

	def accounts_of(self, name):
		return frappe.get_doc("Asset Category", name).get("accounts") or []

	def test_it_creates_the_category_named_after_itself(self):
		data = self.tool_data("create_asset_category", self.payload())
		self.assertTrue(data["created"])
		self.assertEqual(data["name"], "Wind Machines")
		self.assertTrue(frappe.db.exists("Asset Category", "Wind Machines"))

	def test_the_accounts_row_carries_the_three_accounts_and_the_company(self):
		self.tool_data("create_asset_category", self.payload())
		rows = self.accounts_of("Wind Machines")
		self.assertEqual(len(rows), 1)
		# `company_name`, not `company`: ERPNext names the column on Asset
		# Category Account after its label. Writing `company` would store a value
		# no bench ever reads.
		self.assertEqual(rows[0].get("company_name"), MAIN)
		self.assertEqual(rows[0].get("fixed_asset_account"), FIXED_ASSETS)
		self.assertEqual(rows[0].get("accumulated_depreciation_account"), ACCUMULATED_DEPRECIATION)
		self.assertEqual(rows[0].get("depreciation_expense_account"), DEPRECIATION_EXPENSE)

	def test_the_depreciation_accounts_fall_back_to_the_companys_own_defaults(self):
		"""ERPNext keeps both on Company, and a site that set them there should not
		have to repeat them on every category."""
		for field in ("accumulated_depreciation_account", "depreciation_expense_account"):
			add_field("Company", field, fieldtype="Link", options="Account", label=field)
		STORE.tables["Company"][MAIN]["accumulated_depreciation_account"] = ACCUMULATED_DEPRECIATION
		STORE.tables["Company"][MAIN]["depreciation_expense_account"] = DEPRECIATION_EXPENSE

		payload = self.payload()
		payload.pop("accumulated_depreciation_account")
		payload.pop("depreciation_expense_account")
		data = self.tool_data("create_asset_category", payload)

		self.assertEqual(data["accounts"][0]["accumulated_depreciation_account"], ACCUMULATED_DEPRECIATION)
		self.assertEqual(
			self.accounts_of("Wind Machines")[0].get("depreciation_expense_account"), DEPRECIATION_EXPENSE
		)

	def test_a_company_with_no_defaults_simply_omits_them(self):
		"""The negative control for the test above: the fixture Company has neither
		field, so the fallback has to be absent rather than crashing on a column
		this site does not have."""
		payload = self.payload()
		payload.pop("accumulated_depreciation_account")
		payload.pop("depreciation_expense_account")
		data = self.tool_data("create_asset_category", payload)
		self.assertIsNone(data["accounts"][0].get("accumulated_depreciation_account"))
		self.assertEqual(data["accounts"][0]["fixed_asset_account"], FIXED_ASSETS)

	def test_no_fixed_asset_account_is_refused_with_what_the_site_has(self):
		payload = self.payload()
		payload.pop("fixed_asset_account")
		message = self.tool_error("create_asset_category", payload)
		self.assertIn("fixed_asset_account is required", message)
		self.assertIn("Data missing in table", message)
		self.assertIn(FIXED_ASSETS, message)
		self.assertFalse(frappe.db.exists("Asset Category", "Wind Machines"))

	def test_an_account_of_the_wrong_type_is_refused_before_anything_is_written(self):
		"""ERPNext's own controller throws on this at save. Refusing here names the
		argument instead of a row number, and leaves no half-made category."""
		message = self.tool_error(
			"create_asset_category", self.payload(fixed_asset_account=f"1110 - Bank Checking - {MAIN_ABBR}")
		)
		self.assertIn("fixed_asset_account", message)
		self.assertIn("Fixed Asset", message)
		self.assertIn("Nothing was created", message)
		self.assertFalse(frappe.db.exists("Asset Category", "Wind Machines"))

	def test_the_accumulated_and_expense_columns_are_type_checked_too(self):
		for key in ("accumulated_depreciation_account", "depreciation_expense_account"):
			with self.subTest(key=key):
				message = self.tool_error(
					"create_asset_category", self.payload(**{key: f"1110 - Bank Checking - {MAIN_ABBR}"})
				)
				self.assertIn(key, message)
				self.assertFalse(frappe.db.exists("Asset Category", "Wind Machines"))

	def test_an_account_of_another_company_is_refused(self):
		message = self.tool_error(
			"create_asset_category", self.payload(fixed_asset_account=f"1110 - Bank Checking - {OTHER_ABBR}")
		)
		self.assertIn("Second Example Ltd", message)
		self.assertFalse(frappe.db.exists("Asset Category", "Wind Machines"))

	def test_asking_for_one_that_exists_returns_it_and_writes_nothing(self):
		"""The name IS the docname, so there can only be one. A caller that wants
		the end state has already got it."""
		before = len(STORE.rows("Asset Category"))
		data = self.tool_data("create_asset_category", self.payload(asset_category_name=ASSET_CATEGORY))
		self.assertFalse(data["created"])
		self.assertEqual(data["name"], ASSET_CATEGORY)
		self.assertEqual(len(STORE.rows("Asset Category")), before)
		self.assertEqual(data["accounts"][0]["depreciation_expense_account"], DEPRECIATION_EXPENSE)

	def test_an_existing_category_with_no_accounts_says_so_rather_than_inventing_one(self):
		data = self.tool_data("create_asset_category", self.payload(asset_category_name="Unconfigured"))
		self.assertFalse(data["created"])
		self.assertEqual(data["accounts"], [])

	def test_a_finance_book_row_is_written_when_all_three_are_given(self):
		data = self.tool_data(
			"create_asset_category",
			self.payload(
				depreciation_method="written down value",
				total_number_of_depreciations=60,
				frequency_of_depreciation=1,
			),
		)
		self.assertEqual(data["finance_books"][0]["depreciation_method"], "Written Down Value")
		books = frappe.get_doc("Asset Category", "Wind Machines").get("finance_books") or []
		self.assertEqual(len(books), 1)
		self.assertEqual(books[0].get("total_number_of_depreciations"), 60)
		self.assertEqual(books[0].get("frequency_of_depreciation"), 1)

	def test_no_finance_book_arguments_writes_no_row(self):
		data = self.tool_data("create_asset_category", self.payload())
		self.assertEqual(data["finance_books"], [])
		self.assertEqual(frappe.get_doc("Asset Category", "Wind Machines").get("finance_books") or [], [])
		self.assertIn("No finance book row was written", data["note"])

	def test_one_of_the_three_finance_book_values_is_refused_naming_the_rest(self):
		message = self.tool_error("create_asset_category", self.payload(depreciation_method="Straight Line"))
		self.assertIn("total_number_of_depreciations", message)
		self.assertIn("frequency_of_depreciation", message)
		self.assertFalse(frappe.db.exists("Asset Category", "Wind Machines"))

	def test_a_zero_is_a_value_somebody_passed_not_a_value_left_out(self):
		"""`total_number_of_depreciations=0` must be refused for being 0, not read
		as "not given" and quietly dropped."""
		message = self.tool_error(
			"create_asset_category",
			self.payload(
				depreciation_method="Straight Line",
				total_number_of_depreciations=0,
				frequency_of_depreciation=12,
			),
		)
		self.assertIn("must both be at least 1", message)
		self.assertIn("got 0 and 12", message)

	def test_an_unknown_depreciation_method_is_refused_with_the_options(self):
		message = self.tool_error(
			"create_asset_category",
			self.payload(
				depreciation_method="Sum of the Years",
				total_number_of_depreciations=60,
				frequency_of_depreciation=1,
			),
		)
		self.assertIn("Straight Line", message)
		self.assertIn("Written Down Value", message)
		self.assertFalse(frappe.db.exists("Asset Category", "Wind Machines"))

	def test_the_category_it_makes_is_one_create_asset_accepts(self):
		"""The whole point of the tool: create_asset refuses a category the site
		does not have, and this is what stops that being a dead end."""
		self.tool_data("create_asset_category", self.payload())
		data = self.tool_data(
			"create_asset",
			{
				"company": MAIN,
				"asset_name": "Wind Machine 1",
				"item_code": "WM-1",
				"asset_category": "Wind Machines",
				"purchase_date": "2026-01-01",
				"purchase_amount": 24000,
				"useful_life_months": 12,
				"capex_type": "Growth",
				"capex_justification": "A block that had no frost protection now has it.",
			},
		)
		self.assertEqual(data["asset_category"], "Wind Machines")
