# SPDX-License-Identifier: MIT
"""Trade documentation across three tiers, and what it refuses.

SEVEN CLAIMS.

1. **THE TIER DECIDES HOW MUCH PAPER, NOT WHICH SYSTEM.** `OneDeskThreeTiers`.
   A local delivery, an interstate load and an export are one doctype and one
   register, and the checklist each gets is built from the destination's own
   rules rather than from code.

2. **A NEW EXPORT MARKET IS ROWS.** `ConfigNotCode`. Nothing in this app's code
   names a country: `set_destination_requirements` is what makes a destination
   ask for something, and a country's rules ADD to the tier's rather than
   replacing them.

3. **THE GATE SHIPS OFF, AND SAYS THE SAME THING EITHER WAY.**
   `AdvisoryUnlessTurnedOn`. Advisory mode reports the identical gaps and lets
   the truck go; enforcement holds it; an override is recorded on the shipment.

4. **FOUR WAYS A DOCUMENT THAT LOOKS DONE IS NOT.** `LooksDoneIsNotDone`. Not
   approved, voided, EXPIRED, or waiting on a filing this app cannot perform —
   and the last two are the ones a status column hides.

5. **A SEALED DOCUMENT IS CLOSED.** `TheSealMeansSomething`. It refuses content
   edits, it recomputes on every read, and a row changed underneath its seal
   reports the seal as broken rather than looking intact.

6. **THE CHECKLIST IS A SNAPSHOT.** `DriftIsReportedNotApplied`. A requirement
   added in March does not silently appear on a February shipment that has
   already sailed; it is reported.

7. **APPROVING IS AN ATTESTATION.** `ApprovalHasAPrincipal`. It needs a trade
   role, it stamps who did it, and where the template asks for a signature it
   writes a Signing Evidence row over the document's fingerprint.
"""

import json

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import STORE, frappe, set_roles

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_shipment",
		"get_shipment",
		"list_shipments",
		"update_shipment_status",
		"create_trade_document",
		"get_trade_document",
		"update_trade_document",
		"approve_trade_document",
		"seal_trade_document",
		"list_trade_documents",
		"get_shipment_readiness",
		"generate_shipment_packet",
		"create_trade_document_template",
		"list_trade_document_templates",
		"get_destination_requirements",
		"set_destination_requirements",
	)
}

BUYER = "Nishimoto Foods"
DESK = "desk@example.test"


class TradeTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		STORE.seed("Customer", [{"name": BUYER, "customer_name": BUYER}])
		STORE.seed("User", [{"name": DESK, "enabled": 1, "full_name": "Sam Ortiz"}])
		set_roles(DESK, ["Sales Manager"])
		set_roles("Administrator", ["System Manager"])
		self.acting_as("Administrator")
		from erpnext_mcp.tools import shipments

		self.tools = shipments
		shipments.install_trade_documents()

	def acting_as(self, user: str):
		"""Become one principal, the way `require_trade_role` actually reads one.

		BOTH HALVES, and the second is why this is a method rather than a line.
		`security.caller_identity()` wins over `frappe.session.user` — it is the
		human a phone or a Desk session authenticated as, and it is stashed on
		`frappe.local`, which is process-global and does NOT get cleared between
		test classes. A test that set only `frappe.session.user` passes on its own
		and fails the moment it runs after `test_api_mobile`, which leaves a field
		worker's identity behind it. That is a real leak in the double rather than
		in the app, and the honest fix is to set the identity the gate reads.
		"""
		frappe.local.erpnext_mcp_calling_user = user
		frappe.session.user = user
		return user

	def a_shipment(self, tier="Local", country="", **overrides):
		payload = {"destination_tier": tier, "company": MAIN}
		if country:
			payload["destination_country"] = country
		payload.update(overrides)
		return self.tools.create_shipment(payload).data

	def a_document(self, shipment, template="Commercial Invoice", **overrides):
		payload = {"shipment": shipment, "trade_document_template": template}
		payload.update(overrides)
		return self.tools.create_trade_document(payload).data

	def approved(self, shipment, template="Commercial Invoice", **overrides):
		document = self.a_document(shipment, template, **overrides)["trade_document"]
		self.tools.approve_trade_document({"trade_document": document})
		return document


# ── 1 ───────────────────────────────────────────────────────────────────────
class OneDeskThreeTiers(TradeTestCase):
	def test_each_tier_gets_its_own_checklist_from_the_destinations_rules(self):
		"""The whole point: more paper as the load goes further, one register."""
		local = self.a_shipment("Local")
		domestic = self.a_shipment("Domestic")
		export = self.a_shipment("International", "Japan")

		self.assertLess(len(local["checklist"]), len(domestic["checklist"]))
		self.assertLess(len(domestic["checklist"]), len(export["checklist"]))

		local_types = {line["document_type"] for line in local["checklist"]}
		self.assertIn("Commercial Invoice", local_types)
		self.assertNotIn("Phytosanitary Certificate (ePhyto)", local_types)

		export_types = {line["document_type"] for line in export["checklist"]}
		self.assertIn("Phytosanitary Certificate (ePhyto)", export_types)
		self.assertIn("AES Export Declaration", export_types)

	def test_all_three_are_the_same_doctype_and_one_register(self):
		self.a_shipment("Local")
		self.a_shipment("Domestic")
		self.a_shipment("International", "Japan")
		register = self.tools.list_shipments({}).data
		self.assertEqual(register["count"], 3)
		self.assertEqual(register["by_tier"], {"Local": 1, "Domestic": 1, "International": 1})

	def test_an_export_without_a_country_is_refused_because_the_checklist_would_be_short(self):
		"""The failure this prevents is a SHORTER checklist that looks complete."""
		with self.assertRaises(Exception) as caught:
			self.tools.create_shipment({"destination_tier": "International", "company": MAIN})
		self.assertIn("country", str(caught.exception).lower())

	def test_a_domestic_shipment_naming_a_country_is_refused_rather_than_ignored(self):
		with self.assertRaises(Exception) as caught:
			self.tools.create_shipment(
				{"destination_tier": "Domestic", "destination_country": "Japan", "company": MAIN}
			)
		self.assertIn("Japan", str(caught.exception))

	def test_a_country_is_stored_in_one_spelling_so_one_lookup_finds_it(self):
		"""Three spellings of one country would be three rules and no shipment
		getting all of them."""
		export = self.a_shipment("International", "  japan ")
		self.assertEqual(export["destination_country"], "Japan")

	def test_the_walk_is_the_order_the_world_imposes(self):
		shipment = self.a_shipment("Local")["shipment"]
		with self.assertRaises(Exception) as caught:
			self.tools.update_shipment_status({"shipment": shipment, "status": "Delivered"})
		self.assertIn("cannot go from", str(caught.exception))

	def test_a_cancellation_needs_a_reason_because_the_record_is_kept_to_say_why(self):
		shipment = self.a_shipment("Local")["shipment"]
		with self.assertRaises(Exception) as caught:
			self.tools.update_shipment_status({"shipment": shipment, "status": "Cancelled"})
		self.assertIn("reason", str(caught.exception).lower())


# ── 2 ───────────────────────────────────────────────────────────────────────
class ConfigNotCode(TradeTestCase):
	def test_a_countrys_rules_add_to_the_tiers_rather_than_replacing_them(self):
		"""The bug this guards: a lookup returning only the country's rows would
		drop the AES declaration from exactly the shipments most likely to need
		it."""
		self.tools.set_destination_requirements(
			{
				"destination_tier": "International",
				"destination_country": "Vietnam",
				"requirements": ["Import Permit Reference"],
			}
		)
		answer = self.tools.get_destination_requirements(
			{"destination_tier": "International", "destination_country": "Vietnam"}
		).data
		templates = {entry["template"] for entry in answer["requirements"]}
		self.assertIn("Import Permit Reference", templates)
		self.assertIn("AES Export Declaration", templates)
		self.assertIn("Phytosanitary Certificate (ePhyto)", templates)

	def test_adding_a_market_needs_no_release_and_the_next_shipment_carries_it(self):
		self.tools.set_destination_requirements(
			{
				"destination_tier": "International",
				"destination_country": "Vietnam",
				"requirements": [{"trade_document_template": "Fumigation Certificate", "required": True}],
			}
		)
		export = self.a_shipment("International", "Vietnam")
		types = {line["document_type"] for line in export["checklist"]}
		self.assertIn("Fumigation Certificate", types)

	def test_a_new_kind_of_paper_is_a_row(self):
		self.tools.create_trade_document_template(
			{
				"template_name": "Halal Certificate",
				"document_type": "Other",
				"applicable_tiers": "International",
				"required_fields": [{"fieldname": "certifier", "label_en": "Certifier", "required": True}],
			}
		)
		register = self.tools.list_trade_document_templates({"destination_tier": "International"}).data
		self.assertIn("Halal Certificate", {row["template"] for row in register["templates"]})

	def test_it_will_not_silently_overwrite_a_template_an_operator_tuned(self):
		with self.assertRaises(Exception) as caught:
			self.tools.create_trade_document_template(
				{"template_name": "Commercial Invoice", "document_type": "Commercial Invoice"}
			)
		self.assertIn("update_existing", str(caught.exception))

	def test_setting_requirements_is_additive_so_it_does_not_delete_what_it_did_not_mention(self):
		before = self.tools.get_destination_requirements({"destination_tier": "Local"}).data["count"]
		self.tools.set_destination_requirements(
			{"destination_tier": "Local", "requirements": ["Packing List"]}
		)
		after = self.tools.get_destination_requirements({"destination_tier": "Local"}).data
		self.assertEqual(after["count"], before + 1)

	def test_replace_disables_rather_than_deletes_because_an_old_shipment_is_audited_against_it(self):
		self.tools.set_destination_requirements(
			{"destination_tier": "Local", "requirements": ["Commercial Invoice"], "replace": True}
		)
		answer = self.tools.get_destination_requirements({"destination_tier": "Local"}).data
		self.assertEqual([entry["template"] for entry in answer["requirements"]], ["Commercial Invoice"])
		# Disabled, not gone: the rows are still there to answer "why did we need that".
		self.assertTrue(
			frappe.db.get_all(
				"Destination Document Requirement",
				filters={"destination_tier": "Local", "trade_document_template": "Delivery Receipt"},
			)
		)

	def test_the_whole_list_is_checked_before_anything_is_written(self):
		"""A typo in the fourth entry must not leave three rules half-applied."""
		before = self.tools.get_destination_requirements({"destination_tier": "Local"}).data["count"]
		with self.assertRaises(Exception):
			self.tools.set_destination_requirements(
				{
					"destination_tier": "Local",
					"requirements": ["Packing List", "Bill of Lading", "Not A Real Template"],
				}
			)
		after = self.tools.get_destination_requirements({"destination_tier": "Local"}).data["count"]
		self.assertEqual(after, before)

	def test_a_destination_with_nothing_configured_says_so_loudly(self):
		"""An empty checklist means nothing is TRACKED, not that nothing is needed."""
		for row in frappe.db.get_all(
			"Destination Document Requirement", filters={"destination_tier": "Local"}
		):
			frappe.db.set_value("Destination Document Requirement", row["name"], "enabled", 0)
		answer = self.tools.get_destination_requirements({"destination_tier": "Local"}).data
		self.assertEqual(answer["count"], 0)
		self.assertIn("NOTHING IS CONFIGURED", answer["note"])

	def test_a_company_scoped_rule_does_not_reach_another_entitys_shipment(self):
		self.tools.set_destination_requirements(
			{"destination_tier": "Local", "requirements": ["Packing List"], "company": OTHER}
		)
		mine = self.a_shipment("Local")
		self.assertNotIn("Packing List", {line["template"] for line in mine["checklist"]})

	def test_the_seeder_does_not_overrule_an_operator_who_turned_a_document_off(self):
		"""Re-running a migration must not put back a rule somebody disabled."""
		row = frappe.db.get_all(
			"Destination Document Requirement",
			filters={"destination_tier": "Local", "trade_document_template": "Delivery Receipt"},
		)[0]
		frappe.db.set_value("Destination Document Requirement", row["name"], "enabled", 0)
		self.tools.install_trade_documents()
		answer = self.tools.get_destination_requirements({"destination_tier": "Local"}).data
		self.assertNotIn("Delivery Receipt", {entry["template"] for entry in answer["requirements"]})


class ReSeedingIsQuiet(TradeTestCase):
	"""v0.230.2. OML's migrate to v0.226.0 printed ~26 "could not seed trade document
	… already has a rule requiring …" lines: the rules seeded earlier carried a company
	and a NULL country, the seeder looked only for company "" / country "", missed
	them, tried to insert, and the controller's duplicate check refused each one."""

	def test_rules_stamped_with_a_company_and_a_null_country_are_skipped_not_failed(self):
		for row in STORE.rows("Destination Document Requirement"):
			row["company"] = MAIN
			if not row.get("destination_country"):
				row["destination_country"] = None
		before = len(STORE.rows("Destination Document Requirement"))
		report = self.tools.install_trade_documents()
		self.assertEqual(report["failed"], [])
		self.assertEqual(report["requirements_created"], [])
		self.assertTrue(report["requirements_existing"])
		self.assertEqual(len(STORE.rows("Destination Document Requirement")), before)

	def test_a_second_run_creates_nothing_and_fails_nothing(self):
		report = self.tools.install_trade_documents()
		self.assertEqual((report["failed"], report["requirements_created"]), ([], []))


# ── 3 ───────────────────────────────────────────────────────────────────────
class AdvisoryUnlessTurnedOn(TradeTestCase):
	def test_the_site_default_is_advisory_and_the_truck_goes(self):
		"""It ships off because an operation that locks itself out turns the whole
		module off, and then gets no warnings either."""
		shipment = self.a_shipment("Local")
		self.assertFalse(shipment["enforcement"]["enforced"])
		result = self.tools.update_shipment_status(
			{"shipment": shipment["shipment"], "status": "Ready to Ship"}
		).data
		self.assertEqual(result["status"], "Ready to Ship")
		self.assertIn("outstanding", result["warning"])

	def test_advisory_reports_the_identical_gaps_it_would_have_held_for(self):
		shipment = self.a_shipment("Local")["shipment"]
		readiness = self.tools.get_shipment_readiness({"shipment": shipment}).data
		self.assertFalse(readiness["ready"])
		self.assertEqual(len(readiness["blocking"]), readiness["required_count"])
		self.assertIn("advisory_note", readiness)

	def test_enforcement_on_the_site_holds_the_shipment(self):
		self.configure(enabled=1, trade_document_enforcement=1, **ALL_ON)
		shipment = self.a_shipment("Local")["shipment"]
		with self.assertRaises(Exception) as caught:
			self.tools.update_shipment_status({"shipment": shipment, "status": "Ready to Ship"})
		self.assertIn("not ready to ship", str(caught.exception))

	def test_one_shipment_can_be_enforced_on_an_otherwise_advisory_site(self):
		"""An operation shipping mostly local and occasionally to Japan."""
		shipment = self.a_shipment("Local", enforcement="Enforced")["shipment"]
		with self.assertRaises(Exception):
			self.tools.update_shipment_status({"shipment": shipment, "status": "Ready to Ship"})

	def test_one_shipment_can_be_advisory_on_an_otherwise_enforced_site(self):
		self.configure(enabled=1, trade_document_enforcement=1, **ALL_ON)
		shipment = self.a_shipment("Local", enforcement="Advisory")["shipment"]
		result = self.tools.update_shipment_status({"shipment": shipment, "status": "Ready to Ship"}).data
		self.assertEqual(result["status"], "Ready to Ship")

	def test_an_override_is_written_to_the_shipment_because_a_bypass_nobody_recorded_cannot_be_reviewed(self):
		self.configure(enabled=1, trade_document_enforcement=1, **ALL_ON)
		shipment = self.a_shipment("Local")["shipment"]
		result = self.tools.update_shipment_status(
			{
				"shipment": shipment,
				"status": "Ready to Ship",
				"override_reason": "Buyer waived the grade certificate in writing; email on file.",
			}
		).data
		self.assertTrue(result["overridden"])
		self.assertIn("waived", result["override_reason"])
		self.assertIn("waived", frappe.db.get_value("Trade Shipment", shipment, "override_reason"))

	def test_the_register_names_every_shipment_released_with_an_override(self):
		self.configure(enabled=1, trade_document_enforcement=1, **ALL_ON)
		shipment = self.a_shipment("Local")["shipment"]
		self.tools.update_shipment_status(
			{"shipment": shipment, "status": "Ready to Ship", "override_reason": "Buyer waived it."}
		)
		register = self.tools.list_shipments({}).data
		row = next(entry for entry in register["shipments"] if entry["name"] == shipment)
		self.assertTrue(row["released_with_override"])

	def test_a_complete_checklist_needs_no_override_at_all(self):
		self.configure(enabled=1, trade_document_enforcement=1, **ALL_ON)
		shipment = self.a_shipment("Local")["shipment"]
		for line in self.tools.get_shipment_readiness({"shipment": shipment}).data["blocking"]:
			self.approved(shipment, line["template"])
		result = self.tools.update_shipment_status({"shipment": shipment, "status": "Ready to Ship"}).data
		self.assertEqual(result["status"], "Ready to Ship")
		self.assertFalse(result["overridden"])
		self.assertNotIn("warning", result)


# ── 4 ───────────────────────────────────────────────────────────────────────
class LooksDoneIsNotDone(TradeTestCase):
	def _line(self, shipment, template):
		readiness = self.tools.get_shipment_readiness({"shipment": shipment}).data
		return next(line for line in readiness["lines"] if line["template"] == template)

	def test_a_draft_document_does_not_satisfy_its_line(self):
		shipment = self.a_shipment("Local")["shipment"]
		self.a_document(shipment, "Commercial Invoice")
		line = self._line(shipment, "Commercial Invoice")
		self.assertFalse(line["satisfied"])
		self.assertIn("Draft", line["reason"])

	def test_an_approved_document_satisfies_it(self):
		shipment = self.a_shipment("Local")["shipment"]
		self.approved(shipment, "Commercial Invoice")
		self.assertTrue(self._line(shipment, "Commercial Invoice")["satisfied"])

	def test_a_sealed_document_still_satisfies_it(self):
		"""Sealing is a step BEYOND approval, not an alternative — a checklist
		that stopped counting a sealed document would report a fully prepared
		shipment as incomplete."""
		shipment = self.a_shipment("Local")["shipment"]
		document = self.approved(shipment, "Commercial Invoice")
		self.tools.seal_trade_document({"trade_document": document})
		self.assertTrue(self._line(shipment, "Commercial Invoice")["satisfied"])

	def test_a_voided_document_does_not_because_a_withdrawn_certificate_is_not_one(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice")["trade_document"]
		self.tools.update_trade_document({"trade_document": document, "status": "Void"})
		line = self._line(shipment, "Commercial Invoice")
		self.assertFalse(line["satisfied"])
		self.assertIn("voided", line["reason"])

	def test_an_expired_document_does_not_however_approved_it_is(self):
		"""An ePhyto approved in June for a September sailing is one a border
		rejects, and a status column cannot see that."""
		shipment = self.a_shipment("Local")["shipment"]
		self.approved(shipment, "Commercial Invoice", expires_on="2020-01-01")
		line = self._line(shipment, "Commercial Invoice")
		self.assertFalse(line["satisfied"])
		self.assertIn("expired", line["reason"])

	def test_a_document_awaiting_an_external_filing_does_not_because_this_app_files_nothing(self):
		shipment = self.a_shipment("International", "Japan")["shipment"]
		self.approved(shipment, "AES Export Declaration")
		line = self._line(shipment, "AES Export Declaration")
		self.assertFalse(line["satisfied"])
		self.assertIn("AES", line["reason"])

	def test_recording_the_reference_that_came_back_satisfies_it(self):
		shipment = self.a_shipment("International", "Japan")["shipment"]
		document = self.approved(shipment, "AES Export Declaration")
		self.tools.update_trade_document(
			{"trade_document": document, "external_reference": "X20260816123456"}
		)
		self.assertTrue(self._line(shipment, "AES Export Declaration")["satisfied"])

	def test_approving_an_unfiled_document_warns_rather_than_pretending(self):
		shipment = self.a_shipment("International", "Japan")["shipment"]
		document = self.a_document(shipment, "Phytosanitary Certificate (ePhyto)")["trade_document"]
		result = self.tools.approve_trade_document({"trade_document": document}).data
		self.assertIn("PCIT", result["filing_warning"])

	def test_the_register_names_the_unfiled_and_the_expired_separately(self):
		shipment = self.a_shipment("International", "Japan")["shipment"]
		self.approved(shipment, "AES Export Declaration")
		self.approved(shipment, "Commercial Invoice", expires_on="2020-01-01")
		register = self.tools.list_trade_documents({"shipment": shipment}).data
		self.assertEqual(len(register["awaiting_external_filing"]), 1)
		self.assertEqual(len(register["expired"]), 1)

	def test_a_template_needing_a_filing_with_no_system_named_is_refused(self):
		"""'Requires external filing' and no system is a document nobody can
		ever complete."""
		with self.assertRaises(Exception) as caught:
			self.tools.create_trade_document_template(
				{
					"template_name": "Mystery Filing",
					"document_type": "Other",
					"requires_external_filing": True,
				}
			)
		self.assertIn("External System", str(caught.exception))


# ── 5 ───────────────────────────────────────────────────────────────────────
class TheSealMeansSomething(TradeTestCase):
	def _sealed(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.approved(shipment, "Commercial Invoice", document_data={"invoice_number": "INV-0007"})
		self.tools.seal_trade_document({"trade_document": document})
		return shipment, document

	def test_only_an_approved_document_can_be_sealed(self):
		"""Sealing an unapproved one would certify that nobody had checked it."""
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice")["trade_document"]
		with self.assertRaises(Exception) as caught:
			self.tools.seal_trade_document({"trade_document": document})
		self.assertIn("only an Approved document", str(caught.exception))

	def test_sealing_records_the_hash_and_which_columns_it_covers(self):
		"""The field list cannot be re-derived later, so it is stored."""
		_, document = self._sealed()
		row = frappe.db.get_value(
			"Trade Document", document, ["document_hash", "hashed_fields"], as_dict=True
		)
		self.assertTrue(row["document_hash"].startswith("sha256:"))
		self.assertIn("document_data", json.loads(row["hashed_fields"]))

	def test_a_sealed_document_refuses_content_edits(self):
		"""A seal over a row anybody can still edit is a timestamp wearing a
		seal's clothes."""
		_, document = self._sealed()
		with self.assertRaises(Exception) as caught:
			self.tools.update_trade_document({"trade_document": document, "title": "Something else"})
		self.assertIn("sealed", str(caught.exception))

	def test_the_seal_verifies_on_every_read_rather_than_being_trusted(self):
		_, document = self._sealed()
		seal = self.tools.get_trade_document({"trade_document": document}).data["seal"]
		self.assertTrue(seal["sealed"])
		self.assertTrue(seal["verified"])

	def test_a_row_changed_underneath_its_seal_reports_the_seal_as_broken(self):
		"""The Desk is another door; a check that only ran in the tool layer
		would be a check with a door beside it."""
		_, document = self._sealed()
		frappe.db.set_value(
			"Trade Document", document, "document_data", json.dumps({"invoice_number": "TAMPERED"})
		)
		seal = self.tools.get_trade_document({"trade_document": document}).data["seal"]
		self.assertFalse(seal["verified"])
		self.assertIn("DOES NOT HASH", seal["note"])

	def test_voiding_is_the_escape_hatch_and_keeps_the_record(self):
		"""What actually happens when a certificate is withdrawn and reissued."""
		_, document = self._sealed()
		self.tools.update_trade_document({"trade_document": document, "status": "Void"})
		self.assertEqual(frappe.db.get_value("Trade Document", document, "status"), "Void")

	def test_a_document_cannot_be_created_already_sealed(self):
		with self.assertRaises(Exception):
			frappe.get_doc(
				{"doctype": "Trade Document", "title": "Fake", "status": "Sealed", "company": MAIN}
			).insert()

	def test_a_packet_refuses_unsealed_documents_by_default(self):
		"""A packet is something somebody carries into a room and defends."""
		shipment = self.a_shipment("Local")["shipment"]
		self.approved(shipment, "Commercial Invoice")
		with self.assertRaises(Exception) as caught:
			self.tools.generate_shipment_packet({"shipment": shipment})
		self.assertIn("not sealed", str(caught.exception))

	def test_a_packet_names_unsealed_documents_rather_than_dropping_them(self):
		"""A bundle that quietly omitted them would read as a shipment with less
		paperwork than it has."""
		shipment = self.a_shipment("Local")["shipment"]
		self.approved(shipment, "Commercial Invoice")
		packet = self.tools.generate_shipment_packet({"shipment": shipment, "allow_unsealed": True}).data
		self.assertEqual(packet["document_count"], 1)
		self.assertEqual(len(packet["unsealed"]), 1)
		self.assertIn("UNSEALED", packet["disclosure"])

	def test_a_packet_hashes_over_its_members_and_files_the_bundle(self):
		shipment, _ = self._sealed()
		packet = self.tools.generate_shipment_packet({"shipment": shipment}).data
		self.assertTrue(packet["packet_hash"].startswith("sha256:"))
		self.assertEqual(packet["sealed_count"], 1)
		self.assertTrue(packet["governance_document"])
		self.assertEqual(
			frappe.db.get_value("Trade Shipment", shipment, "packet_document"),
			packet["governance_document"],
		)

	def test_a_packet_of_nothing_is_refused_because_it_is_not_evidence(self):
		shipment = self.a_shipment("Local")["shipment"]
		with self.assertRaises(Exception) as caught:
			self.tools.generate_shipment_packet({"shipment": shipment})
		self.assertIn("no documents", str(caught.exception))

	def test_a_packet_reports_a_broken_seal_rather_than_bundling_it_quietly(self):
		shipment, document = self._sealed()
		frappe.db.set_value("Trade Document", document, "document_data", json.dumps({"x": 1}))
		packet = self.tools.generate_shipment_packet({"shipment": shipment}).data
		self.assertEqual(packet["broken_seals"], [document])
		self.assertIn("DO NOT HASH", packet["warning"])


# ── 6 ───────────────────────────────────────────────────────────────────────
class DriftIsReportedNotApplied(TradeTestCase):
	def test_a_requirement_added_later_does_not_appear_on_an_existing_shipment(self):
		"""A rule changing in March must not silently add a requirement to a
		February shipment that has already sailed."""
		shipment = self.a_shipment("Local")["shipment"]
		before = len(self.tools.get_shipment_readiness({"shipment": shipment}).data["lines"])
		self.tools.set_destination_requirements(
			{"destination_tier": "Local", "requirements": ["Packing List"]}
		)
		readiness = self.tools.get_shipment_readiness({"shipment": shipment}).data
		self.assertEqual(len(readiness["lines"]), before)

	def test_but_it_is_reported_as_drift_rather_than_hidden(self):
		shipment = self.a_shipment("Local")["shipment"]
		self.tools.set_destination_requirements(
			{"destination_tier": "Local", "requirements": ["Packing List"]}
		)
		readiness = self.tools.get_shipment_readiness({"shipment": shipment}).data
		self.assertEqual([entry["template"] for entry in readiness["requirement_drift"]], ["Packing List"])

	def test_drift_does_not_block_the_shipment(self):
		self.configure(enabled=1, trade_document_enforcement=1, **ALL_ON)
		shipment = self.a_shipment("Local")["shipment"]
		for line in self.tools.get_shipment_readiness({"shipment": shipment}).data["blocking"]:
			self.approved(shipment, line["template"])
		self.tools.set_destination_requirements(
			{"destination_tier": "Local", "requirements": ["Packing List"]}
		)
		result = self.tools.update_shipment_status({"shipment": shipment, "status": "Ready to Ship"}).data
		self.assertEqual(result["status"], "Ready to Ship")

	def test_a_deleted_template_on_a_rule_is_named_rather_than_dropped(self):
		"""A checklist quietly one line short is the failure this module is
		built against."""
		frappe.db.set_value("Trade Document Template", "Delivery Receipt", "enabled", 0)
		shipment = self.a_shipment("Local")
		self.assertIn("Delivery Receipt", {entry["template"] for entry in shipment["skipped"]})


# ── 7 ───────────────────────────────────────────────────────────────────────
class ApprovalHasAPrincipal(TradeTestCase):
	def test_approving_needs_a_trade_role(self):
		"""A commercial invoice is a customs declaration; it is not anonymous."""
		STORE.seed("User", [{"name": "picker@example.test", "enabled": 1}])
		set_roles("picker@example.test", ["Field Worker"])
		self.acting_as("picker@example.test")
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice")["trade_document"]
		with self.assertRaises(Exception) as caught:
			self.tools.approve_trade_document({"trade_document": document})
		self.assertIn("may not approve", str(caught.exception))

	def test_a_sales_manager_may(self):
		self.acting_as(DESK)
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice")["trade_document"]
		result = self.tools.approve_trade_document({"trade_document": document}).data
		self.assertEqual(result["approved_by"], DESK)

	def test_a_template_asking_for_a_signature_writes_an_evidence_row(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Delivery Receipt")["trade_document"]
		result = self.tools.approve_trade_document({"trade_document": document}).data
		self.assertTrue(result["signing_evidence"])
		row = frappe.db.get_value(
			"Signing Evidence", result["signing_evidence"], ["document_hash", "document_name"], as_dict=True
		)
		self.assertEqual(row["document_name"], document)
		self.assertTrue(row["document_hash"])

	def test_a_template_not_asking_for_one_does_not(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice")["trade_document"]
		result = self.tools.approve_trade_document({"trade_document": document}).data
		self.assertIsNone(result["signing_evidence"])

	def test_update_will_not_approve_because_that_act_has_a_principal(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice")["trade_document"]
		with self.assertRaises(Exception) as caught:
			self.tools.update_trade_document({"trade_document": document, "status": "Approved"})
		self.assertIn("approve_trade_document", str(caught.exception))

	def test_unfilled_declared_fields_are_reported_not_refused(self):
		"""The person holding the certificate is a better authority on what it
		needs than a JSON list is."""
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice")["trade_document"]
		result = self.tools.approve_trade_document({"trade_document": document}).data
		self.assertEqual(result["status"], "Approved")
		self.assertIn("invoice_number", result["unfilled_required_fields"])
		self.assertIn("still", result["warning"])

	def test_a_document_walks_one_way(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.approved(shipment, "Commercial Invoice")
		with self.assertRaises(Exception) as caught:
			self.tools.update_trade_document({"trade_document": document, "status": "Draft"})
		self.assertIn("cannot go from", str(caught.exception))


# ── 8: the smaller promises ─────────────────────────────────────────────────
class TheDocumentItself(TradeTestCase):
	def test_document_data_merges_rather_than_replacing(self):
		"""A desk completing a certificate over three calls would find each call
		erasing the last, at the worst moment."""
		shipment = self.a_shipment("International", "Japan")["shipment"]
		document = self.a_document(
			shipment, "Phytosanitary Certificate (ePhyto)", document_data={"certificate_number": "US-1"}
		)["trade_document"]
		self.tools.update_trade_document(
			{"trade_document": document, "document_data": {"botanical_name": "Prunus avium"}}
		)
		data = self.tools.get_trade_document({"trade_document": document}).data["document_data"]
		self.assertEqual(data["certificate_number"], "US-1")
		self.assertEqual(data["botanical_name"], "Prunus avium")

	def test_replace_is_available_for_the_caller_who_means_it(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.a_document(shipment, "Commercial Invoice", document_data={"invoice_number": "A"})[
			"trade_document"
		]
		self.tools.update_trade_document(
			{"trade_document": document, "document_data": {"seller": "OML"}, "replace": True}
		)
		data = self.tools.get_trade_document({"trade_document": document}).data["document_data"]
		self.assertNotIn("invoice_number", data)

	def test_a_reference_arriving_stamps_when_it_was_filed(self):
		shipment = self.a_shipment("International", "Japan")["shipment"]
		document = self.a_document(shipment, "AES Export Declaration")["trade_document"]
		self.tools.update_trade_document({"trade_document": document, "external_reference": "X1"})
		self.assertTrue(frappe.db.get_value("Trade Document", document, "external_filed_on"))

	def test_an_off_tier_template_is_refused_unless_the_caller_insists(self):
		shipment = self.a_shipment("Local")["shipment"]
		with self.assertRaises(Exception) as caught:
			self.a_document(shipment, "Phytosanitary Certificate (ePhyto)")
		self.assertIn("allow_off_tier", str(caught.exception))
		allowed = self.a_document(shipment, "Phytosanitary Certificate (ePhyto)", allow_off_tier=True)
		self.assertTrue(allowed["trade_document"])

	def test_a_document_off_the_checklist_says_the_rule_might_be_missing(self):
		shipment = self.a_shipment("Local")["shipment"]
		result = self.a_document(shipment, "Packing List", allow_off_tier=True)
		self.assertTrue(any("checklist" in note for note in result["notes"]))

	def test_the_checklist_mirror_follows_the_document_but_is_never_authoritative(self):
		shipment = self.a_shipment("Local")["shipment"]
		document = self.approved(shipment, "Commercial Invoice")
		detail = self.tools.get_shipment({"shipment": shipment}).data
		line = next(entry for entry in detail["checklist"] if entry["template"] == "Commercial Invoice")
		self.assertEqual(line["documents"][0]["trade_document"], document)
		self.assertEqual(line["documents"][0]["status"], "Approved")

	def test_a_shipment_carries_its_own_readiness_so_nobody_has_to_ask_twice(self):
		shipment = self.a_shipment("Local")["shipment"]
		detail = self.tools.get_shipment({"shipment": shipment}).data
		self.assertIn("readiness", detail)
		self.assertEqual(detail["readiness"]["required_count"], 4)

	def test_a_template_declares_its_schema_and_the_document_reports_what_is_missing(self):
		shipment = self.a_shipment("International", "Japan")["shipment"]
		result = self.a_document(shipment, "Phytosanitary Certificate (ePhyto)")
		names = {field["fieldname"] for field in result["schema"]}
		self.assertIn("additional_declaration", names)
		self.assertIn("treatment", names)
		self.assertIn("certificate_number", result["unfilled_required_fields"])

	def test_the_export_templates_name_their_standard(self):
		"""So a broker's schema and this app's can be reconciled by reading."""
		register = self.tools.list_trade_document_templates({"destination_tier": "International"}).data
		by_name = {row["template"]: row for row in register["templates"]}
		self.assertEqual(by_name["Phytosanitary Certificate (ePhyto)"]["standard_reference"], "IPPC ISPM-12")
		self.assertEqual(by_name["AES Export Declaration"]["standard_reference"], "AES/EEI 15 CFR 30")
		self.assertEqual(
			by_name["Electronic Bill of Lading (eBL)"]["standard_reference"], "DCSA eBL data model"
		)

	def test_labels_resolve_into_spanish_and_a_gap_is_reported_not_hidden(self):
		self.tools.create_trade_document_template(
			{"template_name": "Sin Traducir", "document_type": "Other", "label_en": "Untranslated"}
		)
		register = self.tools.list_trade_document_templates({"language": "es"}).data
		by_name = {row["template"]: row for row in register["templates"]}
		self.assertEqual(by_name["Commercial Invoice"]["label"], "Factura Comercial")
		self.assertEqual(by_name["Sin Traducir"]["label"], "Untranslated")
		self.assertTrue(any(gap["where"] == "Sin Traducir" for gap in register["untranslated"]))

	def test_a_checklist_cannot_carry_one_template_twice(self):
		"""Two lines for one document mean somebody satisfies whichever they
		open first."""
		shipment = self.a_shipment("Local")["shipment"]
		doc = frappe.get_doc("Trade Shipment", shipment)
		doc.append("documents", {"template": "Commercial Invoice", "required": 1})
		with self.assertRaises(Exception) as caught:
			doc.save()
		self.assertIn("twice", str(caught.exception))

	def test_a_destination_rule_cannot_be_written_twice(self):
		with self.assertRaises(Exception) as caught:
			frappe.get_doc(
				{
					"doctype": "Destination Document Requirement",
					"destination_tier": "Local",
					"trade_document_template": "Commercial Invoice",
				}
			).insert()
		self.assertIn("already has a rule", str(caught.exception))

	def test_a_local_rule_naming_a_country_is_refused(self):
		with self.assertRaises(Exception) as caught:
			frappe.get_doc(
				{
					"doctype": "Destination Document Requirement",
					"destination_tier": "Local",
					"destination_country": "Japan",
					"trade_document_template": "Packing List",
				}
			).insert()
		self.assertIn("silently apply", str(caught.exception))
