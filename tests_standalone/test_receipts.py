# SPDX-License-Identifier: MIT
"""Receipt capture — scale tickets, settlements and the branch between them.

v0.67.0. THIRTEEN CLAIMS.

 1. `ScaleTicketCapture` — a ticket comes back with what it went in with, as a draft.
 2. `NetWeightIsArithmetic` — net is gross minus tare, always, and never taken as input.
 3. `ScaleTicketRefusals` — every required field, every bad weight and every bad link is refused with a sentence.
 4. `ScaleTicketNaming` — the docname carries the company abbreviation and counts per company.
 5. `ScaleTicketSubmission` — submitting locks it, twice is refused, and a weightless ticket cannot be submitted.
 6. `ScaleTicketListing` — every filter narrows, `unmatched` answers the unpaid question, and a mixed-unit total says so.
 7. `SettlementCapture` — the five computed numbers are computed, and the lines are filled the way the receipt lines are.
 8. `SettlementRefusals` — periods, deduction types, negative deductions and malformed child arrays.
 9. `SettlementSubmission` — submitting locks it and does NOT post anything.
10. `MatchingTicketsToSettlements` — matching claims tickets, all four checks run before the insert, and cancelling releases them.
11. `Reconciliation` — the packer's weight and the grower's tickets are reported side by side and NEVER agreed.
12. `Classification` — four kinds of paper, the fallback, the ceiling and the tie-break.
13. `SettingsGates` and `TheDoctypesThemselves` — nine switches, and the schema behind them.

WHY THE RECONCILIATION TESTS MATTER MOST. Everything else here is a register
doing register things. The one thing this pair of doctypes exists for is to let a
grower see that the packer paid for 48,000 lb of fruit that the grower's own
scale tickets say was 49,850 lb. `test_the_variance_is_reported_and_never_corrected`
is the assertion that a future refactor cannot quietly "fix" the disagreement,
which would delete the only audit either document has.

ONE FURTHER NOTE ON WHAT THESE TESTS DO NOT PROVE. That ERPNext accepts a Link
to Customer from a custom doctype, or that these two DocType JSONs migrate. Those
are integration facts about a real bench and belong to the FrappeTestCase suite.
What is asserted here is the logic — every refusal, every computation, and every
place a number could be silently invented.
"""

from typing import ClassVar

from erpnext_mcp import registry
from erpnext_mcp.erpnext_mcp.doctype.scale_ticket.scale_ticket import ScaleTicket
from erpnext_mcp.erpnext_mcp.doctype.settlement_statement.settlement_statement import (
	SettlementStatement,
)
from erpnext_mcp.tools import receipts

from .fixtures import (
	MAIN,
	MAIN_ABBR,
	MASTER_CUSTOMER,
	MASTER_SUPPLIER,
	OTHER,
	MastersTestCase,
	PurchasingTestCase,
	install_hrms,
	supplies,
)
from .harness import STORE, _load_app_doctype

READ_TOOLS = (
	"list_scale_tickets",
	"get_scale_ticket",
	"list_settlement_statements",
	"get_settlement_statement",
	"classify_receipt",
)
WRITE_TOOLS = (
	"create_scale_ticket",
	"submit_scale_ticket",
	"create_settlement_statement",
	"submit_settlement_statement",
)
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS

TOOLS_ON = {f"allow_{name}": 1 for name in ALL_TOOLS}

#: A second packer, so "the filter narrowed" is a real claim rather than a
#: coincidence of there being only one customer on the site.
OTHER_PACKER = "Blue Ridge Packing"

#: One load off a truck, with the fields a phone would post. Every test that
#: needs "a ticket" starts here and overrides the one field it is about.
TICKET = {
	"ticket_number": "44718",
	"date": "2026-09-14",
	"customer": MASTER_CUSTOMER,
	"company": MAIN,
	"variety": "Honeycrisp",
	"grade": "XF",
	"gross_weight": 18400,
	"tare_weight": 6200,
	"weight_uom": "Lb",
	"truck_id": "T-14",
	"driver": "R. Ibarra",
	"destination": "Cold Store 2",
	"ticket_image": "/files/ticket-44718.jpg",
}

#: A packer's settlement over one period, with two priced lines and two
#: deductions. The second line carries its OWN gross amount and the first does
#: not — the pair is the whole of the "derive where blank, keep where given"
#: claim, exactly as `PARTS_RECEIPT` is in `test_expenses.py`.
SETTLEMENT = {
	"statement_number": "SS-2026-0912",
	"date": "2026-12-01",
	"customer": MASTER_CUSTOMER,
	"company": MAIN,
	"period_start": "2026-09-01",
	"period_end": "2026-11-30",
	"gross_delivered_weight": 48000,
	"packed_weight": 31200,
	"cull_weight": 9600,
	"weight_uom": "Lb",
	"line_items": [
		{
			"variety": "Honeycrisp",
			"grade": "XF",
			"packed_weight": 20000,
			"price_per_unit": 0.62,
			"price_uom": "Lb",
		},
		{
			"variety": "Honeycrisp",
			"grade": "Fancy",
			"packed_weight": 11200,
			"price_per_unit": 0.40,
			"price_uom": "Lb",
			"gross_amount": 4200.00,
		},
	],
	"deductions": [
		{"deduction_type": "Packing", "description": "Pack charge", "amount": 6240.00},
		{"deduction_type": "Cold Storage", "description": "Sep-Nov", "amount": 1120.00},
	],
}


class ReceiptsTestCase(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **TOOLS_ON)
		STORE.seed(
			"Customer",
			[
				{
					"name": OTHER_PACKER,
					"customer_name": OTHER_PACKER,
					"customer_group": "Packers",
					"territory": "Washington",
					"customer_type": "Company",
				}
			],
		)

	def capture(self, **overrides):
		"""Create one scale ticket and return its payload."""
		return self.tool_data("create_scale_ticket", {**TICKET, **overrides})

	def submitted_ticket(self, **overrides):
		"""A ticket that has been through submit, which is what matching needs."""
		data = self.capture(**overrides)
		self.tool_data("submit_scale_ticket", {"name": data["name"]})
		return data["name"]

	def settle(self, **overrides):
		"""Create one settlement statement and return its payload."""
		return self.tool_data("create_settlement_statement", {**SETTLEMENT, **overrides})


# ── Claim 1: a ticket comes back with what it went in with ────────────────


class ScaleTicketCapture(ReceiptsTestCase):
	def test_it_returns_the_new_docname_and_everything_captured(self):
		data = self.capture()
		self.assertTrue(data["name"])
		self.assertEqual(data["ticket_number"], "44718")
		self.assertEqual(data["date"], "2026-09-14")
		self.assertEqual(data["customer"], MASTER_CUSTOMER)
		self.assertEqual(data["variety"], "Honeycrisp")
		self.assertEqual(data["grade"], "XF")
		self.assertEqual(data["truck_id"], "T-14")
		self.assertEqual(data["driver"], "R. Ibarra")
		self.assertEqual(data["destination"], "Cold Store 2")
		self.assertEqual(data["ticket_image"], "/files/ticket-44718.jpg")

	def test_a_captured_ticket_is_a_draft_and_says_so_both_ways(self):
		"""Submitting freezes a third party's weight record; capturing does not."""
		data = self.capture()
		self.assertEqual(data["status"], "Draft")
		self.assertEqual(data["docstatus"], 0)

	def test_nothing_claims_it_until_a_settlement_does(self):
		self.assertIsNone(self.capture()["settlement"])

	def test_a_customer_can_be_named_by_customer_name_rather_than_docname(self):
		"""A phone knows the packer as the name on the ticket. On a stock site
		those are the same string — until somebody enables naming by series."""
		STORE.seed(
			"Customer",
			[{"name": "CUST-0099", "customer_name": "Cascade Fruit Co", "customer_group": "Packers"}],
		)
		self.assertEqual(self.capture(customer="Cascade Fruit Co")["customer"], "CUST-0099")

	def test_get_returns_the_ticket_and_the_photograph(self):
		name = self.capture()["name"]
		data = self.tool_data("get_scale_ticket", {"name": name})
		self.assertEqual(data["ticket_number"], "44718")
		self.assertEqual(data["ticket_image"], "/files/ticket-44718.jpg")

	def test_the_docname_aliases_all_reach_the_same_ticket(self):
		name = self.capture()["name"]
		for key in ("name", "scale_ticket", "ticket"):
			with self.subTest(alias=key):
				self.assertEqual(self.tool_data("get_scale_ticket", {key: name})["name"], name)


# ── Claim 2: net weight is arithmetic ─────────────────────────────────────


class NetWeightIsArithmetic(ReceiptsTestCase):
	def test_net_is_gross_minus_tare(self):
		self.assertEqual(self.capture()["net_weight"], 12200.0)

	def test_a_net_weight_passed_in_is_ignored_rather_than_stored(self):
		"""The one number a settlement dispute turns on is not something a caller
		gets to assert. `additionalProperties: false` refuses it at the schema on
		the MCP path; the tool never reads it either."""
		self.assertEqual(
			receipts.create_scale_ticket({**TICKET, "net_weight": 99999}).data["net_weight"], 12200.0
		)

	def test_the_subtraction_is_shown_and_not_only_the_answer(self):
		"""A tool that returned only the answer would be asking to be trusted
		about the arithmetic the whole document exists to settle."""
		name = self.capture()["name"]
		check = self.tool_data("get_scale_ticket", {"name": name})["weight_check"]
		self.assertEqual(check["computed_as"], "18400.0 - 6200.0 = 12200.0")
		self.assertEqual(check["net_weight"], 12200.0)
		self.assertEqual(check["weight_uom"], "Lb")

	def test_a_ticket_with_no_tare_nets_the_gross(self):
		self.assertEqual(self.capture(tare_weight=0)["net_weight"], 18400.0)

	def test_a_ticket_printed_with_only_a_net_keeps_it(self):
		"""Some packers print nothing but the net. Subtracting a tare of zero
		from a gross of zero would erase the one weight the slip actually gave."""
		doc = ScaleTicket(
			{"doctype": "Scale Ticket", "gross_weight": 0, "tare_weight": 0, "net_weight": 9000}
		)
		doc.compute_net_weight()
		self.assertEqual(doc.net_weight, 9000)

	def test_the_controller_recomputes_on_every_save_not_only_on_insert(self):
		doc = ScaleTicket({"doctype": "Scale Ticket", "gross_weight": 500, "tare_weight": 120})
		doc.compute_net_weight()
		self.assertEqual(doc.net_weight, 380.0)


# ── Claim 3: refusals ─────────────────────────────────────────────────────


class ScaleTicketRefusals(ReceiptsTestCase):
	def test_the_ticket_number_is_required(self):
		payload = {k: v for k, v in TICKET.items() if k != "ticket_number"}
		self.assertIn("ticket_number", self.tool_error("create_scale_ticket", payload))

	def test_the_date_is_required(self):
		payload = {k: v for k, v in TICKET.items() if k != "date"}
		self.assertIn("date", self.tool_error("create_scale_ticket", payload))

	def test_the_customer_is_required_because_a_delivery_is_to_somebody(self):
		payload = {k: v for k, v in TICKET.items() if k != "customer"}
		self.assertIn("customer is required", self.tool_error("create_scale_ticket", payload))

	def test_a_customer_that_does_not_exist_is_refused_with_what_to_do(self):
		message = self.tool_error("create_scale_ticket", {**TICKET, "customer": "Nobody Packing"})
		self.assertIn("no Customer called 'Nobody Packing'", message)
		self.assertIn("Create the packer first", message)

	def test_a_tare_above_the_gross_is_refused_rather_than_netting_negative(self):
		message = self.tool_error("create_scale_ticket", {**TICKET, "tare_weight": 20000})
		self.assertIn("greater than gross_weight", message)
		self.assertIn("Nothing was created", message)

	def test_a_negative_weight_is_refused(self):
		self.assertIn(
			"cannot be negative", self.tool_error("create_scale_ticket", {**TICKET, "gross_weight": -1})
		)

	def test_a_weight_unit_the_doctype_does_not_offer_is_refused_with_the_list(self):
		message = self.tool_error("create_scale_ticket", {**TICKET, "weight_uom": "Furlongs"})
		self.assertIn("weight_uom must be one of", message)
		self.assertIn("Bin", message)

	def test_a_field_nobody_created_is_refused(self):
		self.assertIn(
			"no Field called 'Block 9'",
			self.tool_error("create_scale_ticket", {**TICKET, "field": "Block 9"}),
		)

	def test_a_field_that_exists_is_accepted_and_stored(self):
		STORE.seed("Field", [{"name": "North 12", "field_name": "North 12", "company": MAIN}])
		self.assertEqual(self.capture(field="North 12")["field"], "North 12")

	def test_a_ticket_that_does_not_exist_reads_as_not_found(self):
		self.assertIn("no Scale Ticket called", self.tool_error("get_scale_ticket", {"name": "ST-XXX-9999"}))

	def test_nothing_is_written_when_a_refusal_lands(self):
		self.tool_error("create_scale_ticket", {**TICKET, "tare_weight": 20000})
		self.assertEqual(STORE.rows("Scale Ticket"), [])


# ── Claim 4: the docname ──────────────────────────────────────────────────


class ScaleTicketNaming(ReceiptsTestCase):
	def test_the_docname_carries_the_company_abbreviation(self):
		"""Not the year, which is what every other dated register here uses. The
		question asked of a scale ticket is whose fruit, not which season."""
		self.assertEqual(self.capture()["name"], f"ST-{MAIN_ABBR}-0001")

	def test_the_sequence_counts_up(self):
		self.capture()
		self.assertEqual(self.capture(ticket_number="44719")["name"], f"ST-{MAIN_ABBR}-0002")

	def test_two_companies_count_separately(self):
		"""Two entities delivering to one packer under one bench is the ordinary
		case, and a shared run would interleave them."""
		self.capture()
		other = self.capture(company=OTHER, ticket_number="44720")
		self.assertTrue(other["name"].endswith("-0001"), other["name"])
		self.assertNotIn(MAIN_ABBR, other["name"])

	def test_the_packers_own_ticket_number_is_not_made_unique(self):
		"""Two packers will both have a ticket 4471 sooner or later, and refusing
		the second would refuse a real ticket."""
		first = self.capture()
		second = self.capture(customer=OTHER_PACKER)
		self.assertEqual(first["ticket_number"], second["ticket_number"])
		self.assertNotEqual(first["name"], second["name"])


# ── Claim 5: submission ───────────────────────────────────────────────────


class ScaleTicketSubmission(ReceiptsTestCase):
	def test_submitting_moves_the_status_and_the_docstatus_together(self):
		name = self.capture()["name"]
		data = self.tool_data("submit_scale_ticket", {"name": name})
		self.assertEqual(data["status"], "Submitted")
		self.assertEqual(data["docstatus"], 1)

	def test_the_audit_row_records_the_docstatus_delta(self):
		"""What was frozen, and when, is the thing an audit of a weight record
		asks about — so it lands on the action log rather than only in a summary
		somebody read once."""
		name = self.capture()["name"]
		self.tool_data("submit_scale_ticket", {"name": name})
		self.assertEqual(self.assertAudited("submit_scale_ticket")["docstatus_delta"], "draft → submitted")

	def test_the_stored_row_carries_the_new_status(self):
		name = self.capture()["name"]
		self.tool_data("submit_scale_ticket", {"name": name})
		self.assertEqual(STORE.get_raw("Scale Ticket", name)["status"], "Submitted")

	def test_submitting_twice_is_refused_by_name(self):
		name = self.capture()["name"]
		self.tool_data("submit_scale_ticket", {"name": name})
		message = self.tool_error("submit_scale_ticket", {"name": name})
		self.assertIn("already submitted", message)
		self.assertIn("Nothing was changed", message)

	def test_a_ticket_with_no_weight_at_all_cannot_be_submitted(self):
		"""Refused at submit rather than at capture: a foreman at a tailgate may
		have the truck before they have read the scale."""
		name = self.capture(gross_weight=0, tare_weight=0)["name"]
		message = self.tool_error("submit_scale_ticket", {"name": name})
		self.assertIn("cannot be submitted with no weight on it", message)
		self.assertEqual(STORE.get_raw("Scale Ticket", name)["docstatus"], 0)

	def test_a_weightless_draft_is_still_allowed_to_exist(self):
		self.assertEqual(self.capture(gross_weight=0, tare_weight=0)["status"], "Draft")


# ── Claim 6: listing ──────────────────────────────────────────────────────


class ScaleTicketListing(ReceiptsTestCase):
	def setUp(self):
		super().setUp()
		self.capture()
		self.capture(ticket_number="44719", customer=OTHER_PACKER, date="2026-09-20")
		self.capture(ticket_number="44720", company=OTHER, date="2026-10-01")

	def test_it_lists_every_ticket_with_its_computed_net(self):
		data = self.tool_data("list_scale_tickets")
		self.assertEqual(data["count"], 3)
		self.assertEqual(data["total_net_weight"], 36600.0)

	def test_it_filters_by_customer(self):
		data = self.tool_data("list_scale_tickets", {"customer": OTHER_PACKER})
		self.assertEqual([row["ticket_number"] for row in data["scale_tickets"]], ["44719"])

	def test_it_filters_by_company(self):
		self.assertEqual(self.tool_data("list_scale_tickets", {"company": OTHER})["count"], 1)

	def test_it_filters_by_status(self):
		self.assertEqual(self.tool_data("list_scale_tickets", {"status": "Draft"})["count"], 3)
		self.assertEqual(self.tool_data("list_scale_tickets", {"status": "Matched"})["count"], 0)

	def test_a_status_the_doctype_does_not_declare_is_refused_with_the_list(self):
		message = self.tool_error("list_scale_tickets", {"status": "Paid"})
		self.assertIn("status must be one of", message)
		self.assertIn("Matched", message)

	def test_it_filters_by_date_range(self):
		data = self.tool_data("list_scale_tickets", {"from_date": "2026-09-18", "to_date": "2026-09-30"})
		self.assertEqual([row["ticket_number"] for row in data["scale_tickets"]], ["44719"])

	def test_an_inverted_date_range_is_refused_rather_than_answered_empty(self):
		message = self.tool_error("list_scale_tickets", {"from_date": "2026-10-01", "to_date": "2026-09-01"})
		self.assertIn("is after to_date", message)

	def test_newest_first(self):
		data = self.tool_data("list_scale_tickets")
		self.assertEqual(
			[row["date"] for row in data["scale_tickets"]], ["2026-10-01", "2026-09-20", "2026-09-14"]
		)

	def test_the_per_unit_count_is_reported_beside_the_total(self):
		"""Kilos and bins do not add, and a single total spanning both is a
		fiction. The count is how a reader knows whether they are looking at one."""
		self.capture(ticket_number="44721", weight_uom="Bin", gross_weight=40, tare_weight=0)
		data = self.tool_data("list_scale_tickets")
		self.assertEqual(data["by_weight_uom"], {"Lb": 3, "Bin": 1})
		self.assertIn("kilos and bins do not add", data["note"])

	def test_unmatched_is_the_unpaid_list(self):
		self.assertEqual(self.tool_data("list_scale_tickets", {"unmatched": True})["count"], 3)


# ── Claim 7: the settlement's computed numbers ────────────────────────────


class SettlementCapture(ReceiptsTestCase):
	def test_it_returns_the_new_docname_and_the_captured_header(self):
		data = self.settle()
		self.assertEqual(data["name"], f"SS-{MAIN_ABBR}-0001")
		self.assertEqual(data["statement_number"], "SS-2026-0912")
		self.assertEqual(data["customer"], MASTER_CUSTOMER)
		self.assertEqual(data["period_start"], "2026-09-01")
		self.assertEqual(data["period_end"], "2026-11-30")
		self.assertEqual(data["status"], "Draft")

	def test_packout_is_packed_over_delivered(self):
		"""31200 / 48000 = 65%. The number one packer is judged against another
		with, and therefore the number this app computes rather than reads."""
		self.assertEqual(self.settle()["packout_pct"], 65.0)

	def test_cull_is_culled_over_delivered(self):
		self.assertEqual(self.settle()["cull_pct"], 20.0)

	def test_the_percentages_are_not_taken_from_the_caller(self):
		data = receipts.create_settlement_statement({**SETTLEMENT, "packout_pct": 99}).data
		self.assertEqual(data["packout_pct"], 65.0)

	def test_a_settlement_with_no_delivered_weight_has_a_packout_of_zero(self):
		"""Zero rather than null: the statement not stating a delivered weight is
		visible in the weight field, which is where somebody would look."""
		self.assertEqual(self.settle(gross_delivered_weight=0)["packout_pct"], 0.0)

	def test_the_line_gross_amount_is_derived_where_the_statement_left_it_blank(self):
		"""20000 x 0.62 = 12400."""
		self.assertEqual(self.settle()["line_items"][0]["gross_amount"], 12400.0)

	def test_a_line_that_carried_its_own_amount_keeps_it(self):
		"""11200 x 0.40 would be 4480; the statement said 4200, and a packer who
		applied a promotion to a line is telling the truth."""
		self.assertEqual(self.settle()["line_items"][1]["gross_amount"], 4200.0)

	def test_total_gross_revenue_is_the_sum_of_the_lines(self):
		self.assertEqual(self.settle()["total_gross_revenue"], 16600.0)

	def test_total_deductions_is_the_sum_of_the_deduction_rows(self):
		self.assertEqual(self.settle()["total_deductions"], 7360.0)

	def test_net_proceeds_is_gross_less_deductions(self):
		self.assertEqual(self.settle()["net_proceeds"], 9240.0)

	def test_the_deductions_survive_as_rows_rather_than_one_netted_number(self):
		"""'What did storage cost me' is the question a grower asks a year later,
		and a single net figure cannot answer it."""
		rows = self.settle()["deductions"]
		self.assertEqual(
			[(row["deduction_type"], row["amount"]) for row in rows],
			[("Packing", 6240.0), ("Cold Storage", 1120.0)],
		)

	def test_the_price_uom_is_kept_separate_from_the_weight_uom(self):
		"""A packer quotes per box and weighs in pounds often enough that
		assuming they agree would misprice a line by a factor of forty."""
		data = self.settle(
			weight_uom="Lb",
			line_items=[{"variety": "Gala", "packed_weight": 100, "price_per_unit": 22, "price_uom": "Box"}],
		)
		self.assertEqual(data["weight_uom"], "Lb")
		self.assertEqual(data["line_items"][0]["price_uom"], "Box")

	def test_a_settlement_with_no_lines_at_all_is_allowed(self):
		"""A statement that arrived as a summary page is still a statement."""
		data = self.settle(line_items=[], deductions=[])
		self.assertEqual(data["total_gross_revenue"], 0.0)
		self.assertEqual(data["net_proceeds"], 0.0)

	def test_get_returns_the_lines_the_deductions_and_the_money(self):
		name = self.settle()["name"]
		data = self.tool_data("get_settlement_statement", {"name": name})
		self.assertEqual(len(data["line_items"]), 2)
		self.assertEqual(len(data["deductions"]), 2)
		self.assertEqual(data["net_proceeds"], 9240.0)

	def test_the_docname_aliases_all_reach_the_same_statement(self):
		name = self.settle()["name"]
		for key in ("name", "settlement_statement", "settlement", "statement"):
			with self.subTest(alias=key):
				self.assertEqual(self.tool_data("get_settlement_statement", {key: name})["name"], name)


# ── Claim 8: settlement refusals ──────────────────────────────────────────


class SettlementRefusals(ReceiptsTestCase):
	def test_the_statement_number_is_required(self):
		payload = {k: v for k, v in SETTLEMENT.items() if k != "statement_number"}
		self.assertIn("statement_number", self.tool_error("create_settlement_statement", payload))

	def test_an_inverted_period_is_refused(self):
		message = self.tool_error(
			"create_settlement_statement",
			{**SETTLEMENT, "period_start": "2026-11-30", "period_end": "2026-09-01"},
		)
		self.assertIn("is after period_end", message)
		self.assertIn("Nothing was created", message)

	def test_a_deduction_type_the_select_does_not_offer_is_refused_with_the_list(self):
		message = self.tool_error(
			"create_settlement_statement",
			{**SETTLEMENT, "deductions": [{"deduction_type": "Shrinkage", "amount": 10}]},
		)
		self.assertIn("deductions[1].deduction_type must be one of", message)
		self.assertIn("Commission", message)

	def test_a_negative_deduction_is_refused_because_it_would_add_to_the_proceeds(self):
		message = self.tool_error(
			"create_settlement_statement",
			{**SETTLEMENT, "deductions": [{"deduction_type": "Packing", "amount": -50}]},
		)
		self.assertIn("cannot be negative", message)
		self.assertIn("that is a line item", message)

	def test_a_deduction_with_no_type_defaults_to_other_rather_than_being_refused(self):
		data = self.settle(deductions=[{"description": "Sorting", "amount": 12}])
		self.assertEqual(data["deductions"][0]["deduction_type"], "Other")

	def test_line_items_that_are_not_a_list_are_refused(self):
		message = self.tool_error("create_settlement_statement", {**SETTLEMENT, "line_items": "Honeycrisp"})
		self.assertIn("line_items must be a list", message)

	def test_a_line_item_that_is_not_an_object_names_its_position(self):
		message = self.tool_error("create_settlement_statement", {**SETTLEMENT, "line_items": ["Honeycrisp"]})
		self.assertIn("line_items[1] must be an object", message)

	def test_deductions_that_are_not_a_list_are_refused(self):
		self.assertIn(
			"deductions must be a list",
			self.tool_error("create_settlement_statement", {**SETTLEMENT, "deductions": 7360}),
		)

	def test_a_negative_weight_is_refused(self):
		self.assertIn(
			"cannot be negative",
			self.tool_error("create_settlement_statement", {**SETTLEMENT, "packed_weight": -1}),
		)

	def test_nothing_is_written_when_a_refusal_lands(self):
		self.tool_error(
			"create_settlement_statement",
			{**SETTLEMENT, "deductions": [{"deduction_type": "Packing", "amount": -50}]},
		)
		self.assertEqual(STORE.rows("Settlement Statement"), [])


# ── Claim 9: settlement submission ────────────────────────────────────────


class SettlementSubmission(ReceiptsTestCase):
	def test_submitting_moves_the_status_and_the_docstatus_together(self):
		name = self.settle()["name"]
		data = self.tool_data("submit_settlement_statement", {"name": name})
		self.assertEqual(data["status"], "Submitted")
		self.assertEqual(data["docstatus"], 1)

	def test_the_money_comes_back_with_the_submission(self):
		name = self.settle()["name"]
		data = self.tool_data("submit_settlement_statement", {"name": name})
		self.assertEqual(data["total_gross_revenue"], 16600.0)
		self.assertEqual(data["net_proceeds"], 9240.0)
		self.assertEqual(data["packout_pct"], 65.0)

	def test_submitting_posts_nothing_to_the_ledger(self):
		"""`Posted` exists as a state and NOTHING in v0.67.0 reaches it. The
		column is here so the release that books proceeds does not have to
		migrate every statement captured before it."""
		before = len(STORE.rows("Journal Entry"))
		name = self.settle()["name"]
		data = self.tool_data("submit_settlement_statement", {"name": name})
		self.assertEqual(data["status"], "Submitted")
		self.assertIsNone(STORE.get_raw("Settlement Statement", name).get("posted_journal_entry"))
		self.assertEqual(len(STORE.rows("Journal Entry")), before)

	def test_submitting_twice_is_refused_by_name(self):
		name = self.settle()["name"]
		self.tool_data("submit_settlement_statement", {"name": name})
		self.assertIn("already submitted", self.tool_error("submit_settlement_statement", {"name": name}))

	def test_a_statement_that_does_not_exist_reads_as_not_found(self):
		self.assertIn(
			"no Settlement Statement called",
			self.tool_error("submit_settlement_statement", {"name": "SS-XXX-9999"}),
		)

	def test_it_filters_the_register_by_status(self):
		name = self.settle()["name"]
		self.tool_data("submit_settlement_statement", {"name": name})
		self.settle(statement_number="SS-2026-0913")
		self.assertEqual(self.tool_data("list_settlement_statements", {"status": "Draft"})["count"], 1)
		self.assertEqual(self.tool_data("list_settlement_statements", {"status": "Submitted"})["count"], 1)


# ── Claim 10: matching ────────────────────────────────────────────────────


class MatchingTicketsToSettlements(ReceiptsTestCase):
	def test_matching_claims_the_tickets_and_moves_them_to_matched(self):
		ticket = self.submitted_ticket()
		data = self.settle(scale_tickets=[ticket])
		self.assertEqual(STORE.get_raw("Scale Ticket", ticket)["settlement"], data["name"])
		self.assertEqual(STORE.get_raw("Scale Ticket", ticket)["status"], "Matched")

	def test_a_matched_ticket_drops_off_the_unpaid_list(self):
		ticket = self.submitted_ticket()
		self.assertEqual(self.tool_data("list_scale_tickets", {"unmatched": True})["count"], 1)
		self.settle(scale_tickets=[ticket])
		self.assertEqual(self.tool_data("list_scale_tickets", {"unmatched": True})["count"], 0)

	def test_the_ticket_reports_the_settlement_that_claimed_it(self):
		ticket = self.submitted_ticket()
		settlement = self.settle(scale_tickets=[ticket])["name"]
		data = self.tool_data("get_scale_ticket", {"name": ticket})
		self.assertEqual(data["settlement"], settlement)
		self.assertEqual(data["settlement_detail"]["statement_number"], "SS-2026-0912")

	def test_a_draft_ticket_cannot_be_matched(self):
		"""Its weights can still change after the settlement is checked against
		them, which makes the check meaningless."""
		draft = self.capture()["name"]
		message = self.tool_error("create_settlement_statement", {**SETTLEMENT, "scale_tickets": [draft]})
		self.assertIn("is not submitted", message)
		self.assertIn("Nothing was created", message)

	def test_a_ticket_already_claimed_by_another_settlement_is_refused(self):
		"""Two statements paying for one load is the overpayment this register
		exists to surface. Re-pointing the ticket would hide it."""
		ticket = self.submitted_ticket()
		first = self.settle(scale_tickets=[ticket])["name"]
		message = self.tool_error(
			"create_settlement_statement",
			{**SETTLEMENT, "statement_number": "SS-2026-0913", "scale_tickets": [ticket]},
		)
		self.assertIn(f"already matched to settlement {first}", message)

	def test_a_ticket_from_another_company_is_refused(self):
		ticket = self.submitted_ticket(company=OTHER)
		message = self.tool_error("create_settlement_statement", {**SETTLEMENT, "scale_tickets": [ticket]})
		self.assertIn("belongs to", message)

	def test_a_ticket_delivered_to_another_packer_is_refused(self):
		ticket = self.submitted_ticket(customer=OTHER_PACKER)
		message = self.tool_error("create_settlement_statement", {**SETTLEMENT, "scale_tickets": [ticket]})
		self.assertIn("does not settle another packer's deliveries", message)

	def test_a_ticket_that_does_not_exist_is_refused(self):
		message = self.tool_error(
			"create_settlement_statement", {**SETTLEMENT, "scale_tickets": ["ST-XX-0001"]}
		)
		self.assertIn("no Scale Ticket called", message)

	def test_every_check_runs_before_the_insert_so_no_half_matched_settlement_exists(self):
		"""The refusals all say 'nothing was created', which is only true because
		none of the matching runs until every ticket has passed."""
		good = self.submitted_ticket()
		bad = self.capture(ticket_number="44719")["name"]
		self.tool_error("create_settlement_statement", {**SETTLEMENT, "scale_tickets": [good, bad]})
		self.assertEqual(STORE.rows("Settlement Statement"), [])
		self.assertIsNone(STORE.get_raw("Scale Ticket", good).get("settlement"))
		self.assertEqual(STORE.get_raw("Scale Ticket", good)["status"], "Submitted")

	def test_scale_tickets_that_are_not_a_list_are_refused(self):
		self.assertIn(
			"scale_tickets must be a list",
			self.tool_error("create_settlement_statement", {**SETTLEMENT, "scale_tickets": "ST-ETC-0001"}),
		)

	def test_cancelling_a_settlement_puts_its_tickets_back_on_the_unpaid_list(self):
		"""A cancelled settlement has not paid for anything, and a ticket left
		reading Matched would sit in the register as paid for, for ever."""
		import frappe

		ticket = self.submitted_ticket()
		settlement = self.settle(scale_tickets=[ticket])["name"]
		doc = frappe.get_doc("Settlement Statement", settlement)
		doc.submit()
		doc.cancel()
		row = STORE.get_raw("Scale Ticket", ticket)
		self.assertIsNone(row.get("settlement"))
		self.assertEqual(row["status"], "Submitted")


# ── Claim 11: the reconciliation, which is the point of all of it ─────────


class Reconciliation(ReceiptsTestCase):
	def matched_settlement(self, **overrides):
		first = self.submitted_ticket()
		second = self.submitted_ticket(ticket_number="44719", gross_weight=44000, tare_weight=6350)
		return self.settle(scale_tickets=[first, second], **overrides), first, second

	def test_it_reports_both_figures_side_by_side(self):
		data, _, _ = self.matched_settlement()
		reconciliation = data["delivery_reconciliation"]
		self.assertEqual(reconciliation["packer_gross_delivered_weight"], 48000.0)
		self.assertEqual(reconciliation["matched_ticket_net_weight"], 49850.0)
		self.assertEqual(reconciliation["matched_ticket_count"], 2)

	def test_the_variance_is_reported_and_never_corrected(self):
		"""THE ASSERTION THIS MODULE EXISTS FOR. The packer paid for 48,000 lb;
		the grower's own tickets say 49,850 lb crossed the scale. Neither figure
		is derived from the other and neither is adjusted — a future refactor
		that quietly agreed them would delete the only audit either document has.
		"""
		data, _, _ = self.matched_settlement()
		self.assertEqual(data["delivery_reconciliation"]["variance"], -1850.0)
		self.assertEqual(data["gross_delivered_weight"], 48000.0)

	def test_the_settlement_read_carries_the_same_reconciliation(self):
		data, _, _ = self.matched_settlement()
		fresh = self.tool_data("get_settlement_statement", {"name": data["name"]})
		self.assertEqual(fresh["delivery_reconciliation"]["variance"], -1850.0)
		self.assertEqual(len(fresh["matched_scale_tickets"]), 2)

	def test_tickets_in_another_unit_are_counted_and_excluded_rather_than_converted(self):
		"""There is no bins-to-pounds conversion this app knows, and a fabricated
		one would put a fabricated variance on the answer."""
		bins = self.submitted_ticket(ticket_number="44730", weight_uom="Bin", gross_weight=40, tare_weight=0)
		data = self.settle(scale_tickets=[bins])
		reconciliation = data["delivery_reconciliation"]
		self.assertEqual(reconciliation["tickets_in_other_units_excluded"], 1)
		self.assertEqual(reconciliation["matched_ticket_net_weight"], 0.0)
		self.assertEqual(reconciliation["matched_ticket_count"], 1)

	def test_no_matched_tickets_makes_the_variance_meaningless_and_says_so(self):
		data = self.settle()
		reconciliation = data["delivery_reconciliation"]
		self.assertEqual(reconciliation["matched_ticket_count"], 0)
		self.assertEqual(reconciliation["variance"], 48000.0)
		self.assertIn("means nothing at all", reconciliation["note"])

	def test_the_note_says_the_variance_is_a_finding_rather_than_an_error(self):
		data, _, _ = self.matched_settlement()
		self.assertIn("not an error in this record", data["delivery_reconciliation"]["note"])


# ── Claim 12: classification ──────────────────────────────────────────────


class Classification(ReceiptsTestCase):
	def classify(self, **args):
		return self.tool_data("classify_receipt", args)

	def test_a_fuel_receipt_reads_as_an_expense(self):
		data = self.classify(
			merchant="Valley Co-op Fuel",
			text="VALLEY CO-OP FUEL\nPUMP 4\nDIESEL 42.1 GALLONS\nSUBTOTAL 171.30\nSALES TAX 13.32\nTOTAL 184.62\nVISA CARD ENDING 4419",
		)
		self.assertEqual(data["receipt_type"], "expense")
		self.assertFalse(data["default_applied"])
		self.assertIn("gallons", data["matched_signals"])
		self.assertEqual(data["suggested_tool"], "Expense Receipt (submit_expense_receipt)")

	def test_a_scale_ticket_reads_as_a_scale_ticket(self):
		data = self.classify(
			merchant="Blue Ridge Packing",
			text="SCALE TICKET 44718\nCERTIFIED SCALE\nGROSS WT 18400\nTARE WT 6200\nNET WT 12200\nTRUCK T-14\nORCHARD NORTH 12",
		)
		self.assertEqual(data["receipt_type"], "scale_ticket")
		self.assertIn("scale ticket", data["matched_signals"])
		self.assertEqual(data["suggested_tool"], "Scale Ticket (create_scale_ticket)")

	def test_a_packout_report_reads_as_a_settlement(self):
		data = self.classify(
			merchant="Blue Ridge Packing",
			text="GROWER STATEMENT — POOL RETURN\nPACKOUT 65%\nCULL 20%\nPACKING CHARGE 6240.00\nCOLD STORAGE 1120.00\nCOMMISSION 830.00\nNET PROCEEDS 9240.00",
		)
		self.assertEqual(data["receipt_type"], "settlement")
		self.assertIn("packout", data["matched_signals"])
		self.assertEqual(data["suggested_tool"], "Settlement Statement (create_settlement_statement)")

	def test_an_invoice_reads_as_a_bill_and_says_it_has_nowhere_to_go(self):
		data = self.classify(
			merchant="Cascade Ag Parts",
			text="INVOICE NO 88213\nBILL TO: Orchard Meadow\nTERMS NET 30\nPURCHASE ORDER 4471\nAMOUNT DUE 1,204.60\nREMIT TO: PO Box 4",
		)
		self.assertEqual(data["receipt_type"], "bill")
		self.assertIn("NOT YET IMPLEMENTED", data["suggested_tool"])

	def test_nothing_matching_is_a_stated_fallback_rather_than_a_guess(self):
		data = self.classify(text="qqqq zzzz wwww")
		self.assertEqual(data["receipt_type"], "expense")
		self.assertTrue(data["default_applied"])
		self.assertEqual(data["confidence"], 0.0)
		self.assertEqual(data["matched_signals"], [])
		self.assertIn("FALLBACK", data["note"])

	def test_an_empty_input_is_refused_rather_than_classified(self):
		"""Classifying an empty string would return a guess wearing a confidence."""
		message = self.tool_error("classify_receipt", {"merchant": "", "text": ""})
		self.assertIn("needs something to read", message)

	def test_the_confidence_never_reaches_one(self):
		"""A keyword rule is never certain, and 1.0 is an instruction to the
		client to stop asking."""
		data = self.classify(text="scale ticket weight ticket weighmaster gross wt tare wt net wt bin count")
		self.assertEqual(data["receipt_type"], "scale_ticket")
		self.assertLessEqual(data["confidence"], receipts.CLASSIFIER_CEILING)

	def test_one_weak_signal_is_not_certainty(self):
		"""Confidence is scaled down when there was little evidence to share."""
		data = self.classify(text="lot 7")
		self.assertEqual(data["receipt_type"], "scale_ticket")
		self.assertLess(data["confidence"], 0.5)

	def test_the_reasoning_comes_back_with_the_answer(self):
		"""A classifier nobody can argue with is a classifier nobody corrects."""
		data = self.classify(text="GROSS WT 18400 TARE WT 6200")
		self.assertEqual(sorted(data["matched_signals"]), ["gross", "gross wt", "tare", "tare wt"])
		self.assertIn("show it rather than the number", data["note"])

	def test_the_losing_types_come_back_as_alternatives(self):
		data = self.classify(text="GROWER STATEMENT — PACKOUT 65% — NET PROCEEDS 9240 — GROSS WT 48000")
		self.assertEqual(data["receipt_type"], "settlement")
		self.assertIn("scale_ticket", [row["receipt_type"] for row in data["alternatives"]])
		self.assertIn(
			"gross wt",
			dict((row["receipt_type"], row["matched_signals"]) for row in data["alternatives"])[
				"scale_ticket"
			],
		)

	def test_a_tie_breaks_toward_the_more_specific_document(self):
		"""A settlement quotes weights and so always picks up scale-ticket words;
		a scale ticket almost never says 'packout'."""
		data = self.classify(text="packout tare")
		self.assertEqual(data["scores"]["settlement"], data["scores"]["scale_ticket"] + 1)
		self.assertEqual(data["receipt_type"], "settlement")

	def test_the_fallback_could_never_win_a_tie(self):
		"""`expense` is last in precedence because it is also the fallback, and a
		fallback that could win a tie would swallow the two new registers."""
		self.assertEqual(receipts.CLASSIFIER_PRECEDENCE[-1], "expense")

	def test_the_amount_is_echoed_and_never_used_to_classify(self):
		"""A $9,000 fuel bill and a $9,000 settlement are the same number, and a
		rule on it would be a rule on farm size."""
		low = self.classify(text="GROSS WT 100 TARE WT 10", amount=12.50)
		high = self.classify(text="GROSS WT 100 TARE WT 10", amount=91000.00)
		self.assertEqual(low["receipt_type"], high["receipt_type"])
		self.assertEqual(low["confidence"], high["confidence"])
		self.assertEqual(high["amount"], 91000.00)

	def test_it_touches_no_doctype_at_all(self):
		before = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		self.classify(text="SCALE TICKET 44718 GROSS WT 18400")
		after = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		after.pop("MCP Action Log", None)
		before.pop("MCP Action Log", None)
		self.assertEqual(before, after)


# ── Claim 13a: the switches ───────────────────────────────────────────────


class SettingsGates(ReceiptsTestCase):
	ARGUMENTS: ClassVar[dict] = {
		"list_scale_tickets": {},
		"get_scale_ticket": {"name": "ST-ETC-0001"},
		"list_settlement_statements": {},
		"get_settlement_statement": {"name": "SS-ETC-0001"},
		"classify_receipt": {"text": "scale ticket"},
		"create_scale_ticket": TICKET,
		"submit_scale_ticket": {"name": "ST-ETC-0001"},
		"create_settlement_statement": SETTLEMENT,
		"submit_settlement_statement": {"name": "SS-ETC-0001"},
	}

	def test_every_switch_refuses_by_the_name_an_operator_would_tick(self):
		for name in ALL_TOOLS:
			with self.subTest(tool=name):
				self.configure(enabled=1, **{**TOOLS_ON, f"allow_{name}": 0})
				message = self.tool_error(name, self.ARGUMENTS[name])
				self.assertIn(f"allow_{name}", message)
				self.assertIn("switched off", message)

	def test_the_five_reads_are_on_out_of_the_box(self):
		defaults = self.configure()
		for name in READ_TOOLS:
			with self.subTest(tool=name):
				self.assertEqual(defaults[f"allow_{name}"], "1")

	def test_the_four_writes_are_off_out_of_the_box(self):
		defaults = self.configure()
		for name in WRITE_TOOLS:
			with self.subTest(tool=name):
				self.assertEqual(defaults[f"allow_{name}"], "0")

	def test_every_write_declares_itself_mutating_to_the_protocol(self):
		for name in WRITE_TOOLS:
			with self.subTest(tool=name):
				self.assertTrue(registry.TOOLS[name]["mutating"])
				self.assertFalse(registry.TOOLS[name]["annotations"]["readOnlyHint"])

	def test_every_read_declares_itself_read_only(self):
		for name in READ_TOOLS:
			with self.subTest(tool=name):
				self.assertFalse(registry.TOOLS[name]["mutating"])
				self.assertTrue(registry.TOOLS[name]["annotations"]["readOnlyHint"])

	def test_the_eight_register_tools_need_erpnext_as_well_as_the_doctype(self):
		"""Both doctypes link to Customer, which exists nowhere but ERPNext. A
		predicate that asked only for the doctype would publish eight tools that
		cannot store the one field they all require."""
		for name in set(ALL_TOOLS) - {"classify_receipt"}:
			with self.subTest(tool=name):
				self.assertIsNot(registry.TOOLS[name]["available"], registry._always)
				self.assertIn("ERPNext app", registry.TOOLS[name]["requires"])

	def test_classify_receipt_is_available_everywhere_because_it_needs_nothing(self):
		self.assertIs(registry.TOOLS["classify_receipt"]["available"], registry._always)


# ── Claim 13b: the schema behind them ─────────────────────────────────────


class TheDoctypesThemselves(ReceiptsTestCase):
	def setUp(self):
		super().setUp()
		self.ticket = _load_app_doctype("scale_ticket")
		self.statement = _load_app_doctype("settlement_statement")
		self.line = _load_app_doctype("settlement_line_item")
		self.deduction = _load_app_doctype("settlement_deduction")

	def test_both_registers_are_submittable(self):
		self.assertEqual(int(self.ticket.get("is_submittable") or 0), 1)
		self.assertEqual(int(self.statement.get("is_submittable") or 0), 1)

	def test_both_child_tables_are_child_tables(self):
		self.assertEqual(int(self.line.get("istable") or 0), 1)
		self.assertEqual(int(self.deduction.get("istable") or 0), 1)

	def test_the_computed_fields_are_read_only_in_the_desk_too(self):
		"""A number the tool computes and the Desk lets somebody retype is a
		number that will eventually disagree with its own inputs."""
		computed = {
			"scale_ticket": ("net_weight", "status", "settlement"),
			"settlement_statement": (
				"packout_pct",
				"cull_pct",
				"total_gross_revenue",
				"total_deductions",
				"net_proceeds",
				"status",
				"posted_journal_entry",
			),
		}
		for folder, fieldnames in computed.items():
			payload = _load_app_doctype(folder)
			by_name = {field["fieldname"]: field for field in payload["fields"]}
			for fieldname in fieldnames:
				with self.subTest(doctype=folder, field=fieldname):
					self.assertEqual(by_name[fieldname].get("read_only"), 1)

	def test_the_status_options_agree_with_the_constants(self):
		by_name = {field["fieldname"]: field for field in self.ticket["fields"]}
		self.assertEqual(tuple(by_name["status"]["options"].split("\n")), receipts.TICKET_STATUSES)
		by_name = {field["fieldname"]: field for field in self.statement["fields"]}
		self.assertEqual(tuple(by_name["status"]["options"].split("\n")), receipts.SETTLEMENT_STATUSES)

	def test_the_weight_units_agree_across_both_doctypes_and_the_tool(self):
		"""They have to: the two registers are compared against each other."""
		for payload in (self.ticket, self.statement):
			by_name = {field["fieldname"]: field for field in payload["fields"]}
			self.assertEqual(tuple(by_name["weight_uom"]["options"].split("\n")), receipts.WEIGHT_UOMS)

	def test_the_deduction_types_agree_with_the_constants(self):
		by_name = {field["fieldname"]: field for field in self.deduction["fields"]}
		self.assertEqual(tuple(by_name["deduction_type"]["options"].split("\n")), receipts.DEDUCTION_TYPES)

	def test_the_statement_points_at_its_two_child_tables(self):
		by_name = {field["fieldname"]: field for field in self.statement["fields"]}
		self.assertEqual(by_name["line_items"]["options"], "Settlement Line Item")
		self.assertEqual(by_name["deductions"]["options"], "Settlement Deduction")

	def test_the_ticket_points_at_the_settlement_and_not_the_other_way(self):
		"""One edge, on the many side. A child table of tickets on the settlement
		would be a second place for the same fact to live, and the two would
		disagree the first time a ticket was re-matched."""
		by_name = {field["fieldname"]: field for field in self.ticket["fields"]}
		self.assertEqual(by_name["settlement"]["options"], "Settlement Statement")
		self.assertNotIn("Scale Ticket", [field.get("options") for field in self.statement["fields"]])

	def test_both_controllers_are_importable_and_are_documents(self):
		"""The v0.7.1 failure: a DocType JSON with no Python module beside it and
		a `bench migrate` that dies with ModuleNotFoundError."""
		import frappe

		for cls, doctype in ((ScaleTicket, "Scale Ticket"), (SettlementStatement, "Settlement Statement")):
			with self.subTest(doctype=doctype):
				self.assertTrue(issubclass(cls, frappe.model.document.Document))

	def test_the_status_helpers_cover_every_docstatus(self):
		from erpnext_mcp.erpnext_mcp.doctype.scale_ticket import scale_ticket as ticket_module
		from erpnext_mcp.erpnext_mcp.doctype.settlement_statement import (
			settlement_statement as statement_module,
		)

		self.assertEqual(ticket_module.status_for(0, None), "Draft")
		self.assertEqual(ticket_module.status_for(1, None), "Submitted")
		self.assertEqual(ticket_module.status_for(1, "SS-ETC-0001"), "Matched")
		self.assertEqual(ticket_module.status_for(2, "SS-ETC-0001"), "Cancelled")
		self.assertEqual(statement_module.status_for(0, None), "Draft")
		self.assertEqual(statement_module.status_for(1, None), "Submitted")
		self.assertEqual(statement_module.status_for(1, "JE-0001"), "Posted")
		self.assertEqual(statement_module.status_for(2, None), "Cancelled")

	def test_both_registers_are_declared_precious_so_uninstall_warns_about_them(self):
		"""A scale ticket is the GROWER's only copy — the packer keeps the
		original and the thermal paper fades inside a season. Losing the register
		makes every settlement that follows unauditable, because there is nothing
		left to check one against."""
		from erpnext_mcp import install

		precious = dict(install._PRECIOUS_DOCTYPES)
		self.assertIn("only copy", precious["Scale Ticket"].lower())
		self.assertIn("unauditable", precious["Scale Ticket"])
		self.assertIn("deducted", precious["Settlement Statement"])


# ── v0.68.0: merchant matching and the bill a receipt becomes ───────────────


class NormalizeMerchant(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_normalize_merchant=1)
		STORE.seed(
			"Supplier",
			[{"name": "Wilbur-Ellis Company LLC", "supplier_name": "Wilbur-Ellis Company LLC"}],
		)

	def test_matches_a_similarly_punctuated_name(self):
		data = self.tool_data("normalize_merchant", {"merchant": "WILBUR ELLIS CO"})
		self.assertIsNotNone(data["match"])
		self.assertEqual(data["match"]["supplier"], "Wilbur-Ellis Company LLC")
		self.assertGreater(data["match"]["confidence"], 0.8)

	def test_an_unrelated_name_matches_nothing(self):
		data = self.tool_data("normalize_merchant", {"merchant": "Zzyzx Quantum Widgets"})
		self.assertIsNone(data["match"])

	def test_merchant_is_required(self):
		error = self.tool_error("normalize_merchant", {})
		self.assertIn("merchant", error)

	def test_switched_off_by_default(self):
		self.configure(enabled=1, allow_normalize_merchant=0)
		error = self.tool_error("normalize_merchant", {"merchant": "WILBUR ELLIS CO"})
		self.assertIn("switched off", error)


class ListMerchantAliases(MastersTestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, allow_submit_expense_receipt=1, allow_list_merchant_aliases=1)

	def _receipt(self, merchant, supplier=None, **overrides):
		payload = {
			"merchant": merchant,
			"amount": 50,
			"receipt_date": "2026-06-01",
			"company": MAIN,
			"submitted_by": "HR-EMP-00001",
		}
		if supplier:
			payload["supplier"] = supplier
		payload.update(overrides)
		return self.tool_data("submit_expense_receipt", payload)

	def test_groups_every_spelling_under_its_supplier(self):
		self._receipt("VALLEY CO-OP #14", supplier=MASTER_SUPPLIER)
		self._receipt("Valley Co-op Fuel", supplier=MASTER_SUPPLIER)
		data = self.tool_data("list_merchant_aliases", {})
		self.assertEqual(data["count"], 1)
		entry = data["aliases"][0]
		self.assertEqual(entry["supplier"], MASTER_SUPPLIER)
		self.assertEqual(entry["receipt_count"], 2)
		merchants = {row["merchant"] for row in entry["merchant_strings"]}
		self.assertEqual(merchants, {"VALLEY CO-OP #14", "Valley Co-op Fuel"})

	def test_a_receipt_with_no_supplier_link_does_not_appear(self):
		self._receipt("Some Unlinked Vendor")
		data = self.tool_data("list_merchant_aliases", {})
		self.assertEqual(data["count"], 0)

	def test_supplier_filter_narrows_it(self):
		self._receipt("Cascade Ag Parts", supplier=MASTER_SUPPLIER)
		data = self.tool_data("list_merchant_aliases", {"supplier": MASTER_SUPPLIER})
		self.assertEqual(data["count"], 1)

	def test_an_unknown_supplier_filter_is_refused(self):
		error = self.tool_error("list_merchant_aliases", {"supplier": "Nobody Inc"})
		self.assertIn("no Supplier called", error)


PI_FROM_RECEIPT_TOOLS_ON = {
	"allow_submit_expense_receipt": 1,
	"allow_approve_expense_receipt": 1,
	"allow_create_purchase_invoice_from_receipt": 1,
}


class PurchaseInvoiceFromReceipt(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, **PI_FROM_RECEIPT_TOOLS_ON)

	def approved_receipt(self, **overrides):
		payload = {
			"merchant": "Cascade Ag Parts",
			"amount": 96.40,
			"receipt_date": "2026-06-14",
			"category": "Supplies",
			"company": MAIN,
			"submitted_by": "HR-EMP-00001",
		}
		payload.update(overrides)
		name = self.tool_data("submit_expense_receipt", payload)["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00001"})
		return name

	def test_creates_a_draft_purchase_invoice_and_links_it_back(self):
		name = self.approved_receipt()
		data = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertTrue(data["purchase_invoice"])
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(data["amount"], 96.40)
		self.assertEqual(data["expense_account"], supplies())

		receipt = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(receipt["linked_doctype"], "Purchase Invoice")
		self.assertEqual(receipt["linked_document"], data["purchase_invoice"])

		invoice = self.tool_data("get_purchase_invoice", {"name": data["purchase_invoice"]})
		self.assertEqual(invoice["grand_total"], 96.40)
		self.assertEqual(invoice["items"][0]["item_code"], "EXP-SUPPLIES")

	def test_a_supplier_is_created_from_the_merchant_when_nothing_matches(self):
		name = self.approved_receipt(merchant="Totally New Vendor Ninety Two")
		data = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertEqual(data["supplier"], "Totally New Vendor Ninety Two")
		self.assertIn("created from merchant name", data["supplier_resolved_by"])
		self.assertTrue(STORE.get_raw("Supplier", "Totally New Vendor Ninety Two"))

	def test_a_confident_merchant_match_is_linked_automatically(self):
		STORE.seed(
			"Supplier",
			[{"name": "Wilbur-Ellis Company LLC", "supplier_name": "Wilbur-Ellis Company LLC"}],
		)
		name = self.approved_receipt(merchant="WILBUR ELLIS CO")
		data = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertEqual(data["supplier"], "Wilbur-Ellis Company LLC")
		self.assertIn("matched to merchant name", data["supplier_resolved_by"])

	def test_the_receipts_own_supplier_link_is_used_first(self):
		name = self.approved_receipt(supplier=MASTER_SUPPLIER, merchant="Whatever The Slip Said")
		data = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertEqual(data["supplier"], MASTER_SUPPLIER)
		self.assertEqual(data["supplier_resolved_by"], "receipt link")

	def test_explicit_supplier_argument_is_honoured(self):
		name = self.approved_receipt()
		data = self.tool_data(
			"create_purchase_invoice_from_receipt", {"receipt": name, "supplier": MASTER_SUPPLIER}
		)
		self.assertEqual(data["supplier"], MASTER_SUPPLIER)
		self.assertEqual(data["supplier_resolved_by"], "argument")

	def test_explicit_expense_account_is_honoured(self):
		name = self.approved_receipt()
		data = self.tool_data(
			"create_purchase_invoice_from_receipt", {"receipt": name, "expense_account": supplies()}
		)
		self.assertEqual(data["expense_account_resolved_by"], "argument")

	def test_two_receipts_of_the_same_category_share_one_item(self):
		first = self.approved_receipt()
		second = self.approved_receipt(merchant="Different Vendor Co")
		data1 = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": first})
		data2 = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": second})
		self.assertEqual(data1["item"], data2["item"])

	def test_a_submitted_not_approved_receipt_is_refused(self):
		name = self.tool_data(
			"submit_expense_receipt",
			{
				"merchant": "Cascade Ag Parts",
				"amount": 50,
				"receipt_date": "2026-06-14",
				"category": "Supplies",
				"company": MAIN,
				"submitted_by": "HR-EMP-00001",
			},
		)["name"]
		error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertIn("not Approved", error)

	def test_an_owner_draw_receipt_is_refused_by_name(self):
		name = self.approved_receipt(category="Owner Draw")
		error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertIn("Owner Draw", error)
		self.assertIn("create_owner_draw", error)

	def test_an_already_linked_receipt_is_refused(self):
		name = self.approved_receipt()
		self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertIn("already linked", error)

	def test_a_missing_receipt_is_refused_by_name(self):
		error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": "EXR-NOPE"})
		self.assertIn("no Expense Receipt called", error)

	def test_switched_off_by_default(self):
		name = self.approved_receipt()
		self.configure(
			enabled=1, **{**PI_FROM_RECEIPT_TOOLS_ON, "allow_create_purchase_invoice_from_receipt": 0}
		)
		error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertIn("switched off", error)


class DeleteDraftPurchaseInvoiceReleasesTheReceipt(PurchasingTestCase):
	"""The delete-and-recreate path an OCR misread needs. `delete_draft_purchase_invoice`
	is tested for its own refusals in test_purchasing; this is the receipt side."""

	approved_receipt = PurchaseInvoiceFromReceipt.approved_receipt

	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(
			enabled=1,
			**PI_FROM_RECEIPT_TOOLS_ON,
			allow_delete_draft_purchase_invoice=1,
			allow_update_expense_receipt=1,
		)

	def billed_receipt(self, **overrides):
		name = self.approved_receipt(**overrides)
		invoice = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		return name, invoice["purchase_invoice"]

	def delete(self, invoice):
		return self.tool_data(
			"delete_draft_purchase_invoice",
			{"name": invoice, "reason": "OCR matched the wrong supplier"},
		)

	def test_the_link_is_cleared_on_the_receipt(self):
		name, invoice = self.billed_receipt()
		data = self.delete(invoice)
		receipt = STORE.get_raw("Expense Receipt", name)
		self.assertFalse(receipt.get("linked_document"))
		self.assertFalse(receipt.get("linked_doctype"))
		self.assertEqual([row["name"] for row in data["receipts_released"]], [name])
		self.assertIsNone(STORE.get_raw("Purchase Invoice", invoice))

	def test_the_receipt_can_be_billed_again(self):
		name, invoice = self.billed_receipt()
		self.delete(invoice)
		again = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertNotEqual(again["purchase_invoice"], invoice)
		self.assertEqual(STORE.get_raw("Expense Receipt", name)["linked_document"], again["purchase_invoice"])

	def test_a_corrected_supplier_is_what_the_new_invoice_carries(self):
		name, invoice = self.billed_receipt(merchant="Totally New Vendor Ninety Two")
		data = self.delete(invoice)
		released = data["receipts_released"][0]
		self.assertEqual(released["supplier"], "Totally New Vendor Ninety Two")
		self.assertIn("update_expense_receipt", data["next_step"])

		self.tool_data("update_expense_receipt", {"name": name, "supplier": MASTER_SUPPLIER})
		again = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertEqual(again["supplier"], MASTER_SUPPLIER)

	def test_a_receipt_linked_to_another_invoice_is_left_alone(self):
		_, first_invoice = self.billed_receipt()
		second, _ = self.billed_receipt(merchant="Another Vendor")
		self.delete(first_invoice)
		self.assertTrue(STORE.get_raw("Expense Receipt", second)["linked_document"])

	def test_a_journal_entry_link_with_the_same_docname_is_left_alone(self):
		_, invoice = self.billed_receipt()
		other = self.approved_receipt(merchant="Owner")
		STORE.tables["Expense Receipt"][other].update(linked_doctype="Journal Entry", linked_document=invoice)
		self.delete(invoice)
		self.assertEqual(STORE.get_raw("Expense Receipt", other)["linked_document"], invoice)
