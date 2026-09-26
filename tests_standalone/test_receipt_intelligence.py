# SPDX-License-Identifier: MIT
"""Multi-vector receipt intelligence — the paper is not only the merchant line.

v0.75.0. NINE CLAIMS.

 1. `SignalExtraction` — four anchored patterns, and what each one REFUSES.
 2. `AliasLearning` — setting a supplier teaches a mapping, and never fails the update.
 3. `AliasLookupOnSubmit` — a taught alias resolves the next receipt and counts itself.
 4. `UrlResolution` — a domain resolves a vendor; a processor's domain never does.
 5. `PhoneResolution` — a number already coded on other receipts resolves this one.
 6. `CascadeOrdering` — alias, then URL, then phone, then name, and every step recorded.
 7. `TheLlmHandoff` — the question is prepared, never asked; an answer short-circuits.
 8. `CardFingerprint` — the bank naming a card settles the case that used to be contested.
 9. `TheSchema` — the seven columns, the Select vocabulary, and the installer.

THE TWO TESTS THAT MATTER MOST HERE.

`test_a_domain_match_never_sets_the_supplier_link` is the first. Everything in
this release is machinery for producing a better SUGGESTION, and the rule it
must not break is the one `submit_expense_receipt` has had since v0.31.0:
nothing is inferred, and the supplier link is a human's decision. Exactly one
step is allowed through that door — an exact alias, which is not an inference
but a replay of a link a person already made — and a refactor that let a 0.90
domain match set a link too would be invisible in every output payload and
would put fabricated vendor attributions across a season of books.

`test_the_cascade_never_calls_a_model` is the second. This app's contract with
`document_intel` is that the deterministic half runs on the bench and the
judgement arrives from a client that has a model. A step that quietly dialled an
API would put a network call, a key and a bill inside `bench migrate`, and would
make a capture at a fuel pump fail for a reason nobody standing there could
diagnose.

WHAT THESE TESTS DO NOT PROVE. That the seven Custom Fields survive a real
`bench migrate`, or that MariaDB accepts a docname built from a normalised
merchant string. Those are integration facts about a bench and belong to the
FrappeTestCase suite.
"""

import frappe

from erpnext_mcp import install, registry
from erpnext_mcp.erpnext_mcp.doctype.merchant_alias.merchant_alias import MerchantAlias
from erpnext_mcp.tools import banking_bridge, expenses, receipts

from .fixtures import (
	BANK_ACCOUNT,
	MAIN,
	MASTER_SUPPLIER,
	MastersTestCase,
	install_hrms,
)
from .harness import INSTALLED_DOCTYPES, STORE, _load_app_doctype, add_field

EMPLOYEE = "HR-EMP-00001"
MERCHANT_ALIAS = receipts.MERCHANT_ALIAS
EXPENSE_RECEIPT = expenses.EXPENSE_RECEIPT
BANK_TRANSACTION = banking_bridge.BANK_TRANSACTION

#: The till abbreviation this whole release exists for. There is no string
#: algorithm that gets from these nine letters to "Sawyer's Ace Hardware", which
#: is why a person has to say it once and why the saying has to be kept.
SIATAPING = "SIATAPING"

ACE = "Sawyer's Ace Hardware"

TOOLS_ON = {
	"allow_submit_expense_receipt": 1,
	"allow_update_expense_receipt": 1,
	"allow_get_expense_receipt": 1,
	"allow_list_expense_receipts": 1,
	"allow_normalize_merchant": 1,
	"allow_list_merchant_aliases": 1,
	"allow_auto_match_receipts": 1,
}


class ReceiptIntelligenceTestCase(MastersTestCase):
	"""The fixture site, an HR register, and a second Supplier to be wrong about."""

	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, **TOOLS_ON)
		STORE.seed(
			"Supplier",
			[
				{"name": ACE, "supplier_name": ACE},
				{"name": "Cascade Irrigation Supply", "supplier_name": "Cascade Irrigation Supply"},
			],
		)

	def capture(self, **overrides):
		payload = {
			"merchant": SIATAPING,
			"amount": 43.18,
			"receipt_date": "2026-06-14",
			"category": "Hardware",
			"company": MAIN,
			"submitted_by": EMPLOYEE,
		}
		payload.update(overrides)
		return self.tool_data("submit_expense_receipt", payload)

	def alias_row(self, key):
		return STORE.get_raw(MERCHANT_ALIAS, key)

	def receipt_row(self, name):
		return STORE.get_raw(EXPENSE_RECEIPT, name)


# ── 1 ───────────────────────────────────────────────────────────────────────
class SignalExtraction(ReceiptIntelligenceTestCase):
	"""Four patterns over raw OCR text, and the coincidences each one refuses.

	The refusals are the half worth testing. A pattern that finds a card number
	is worth nothing if it also finds the time printed at the top of the slip —
	a fingerprint that matched a clock would manufacture confidence out of noise,
	which is worse than having no fingerprint at all.
	"""

	SLIP = """
	SAWYER'S ACE HARDWARE
	SIATAPING
	Store #4102
	(509) 555-0134
	www.acehardware.com
	06/14/2026 14:22
	2 x HOSE CLAMP 1/2         8.98
	SUBTOTAL                  39.61
	TOTAL                     43.18
	VISA ************4417
	Ace Rewards member 8827341
	Tell us: survey.acehardware.com
	"""

	def test_all_four_signals_come_off_one_slip(self):
		found = receipts.extract_receipt_signals(self.SLIP)
		self.assertEqual(found["card_last_four"], "4417")
		self.assertEqual(found["merchant_phone"], "5095550134")
		self.assertEqual(found["merchant_url"], "acehardware.com")
		self.assertEqual(found["store_number"], "4102")

	def test_empty_text_returns_every_key_and_no_value(self):
		"""A caller never has to tell "not found" from "not looked for"."""
		found = receipts.extract_receipt_signals("")
		self.assertEqual(sorted(found), sorted(receipts.SIGNAL_FIELDS))
		self.assertEqual(set(found.values()), {""})

	def test_a_bare_four_digit_run_is_never_read_as_a_card(self):
		"""THE FAILURE THIS PATTERN EXISTS TO AVOID. A receipt is full of
		four-digit numbers — a time, a total in cents, an item code — and a card
		fingerprint that matched one would be a coincidence wearing a
		confidence."""
		for text in ("TOTAL 43.18 AT 1422", "ITEM 9931 QTY 2", "06/14/2026 1422"):
			with self.subTest(text=text):
				self.assertEqual(receipts.extract_receipt_signals(text)["card_last_four"], "")

	def test_a_masked_or_worded_card_is_read(self):
		for text, expected in (
			("VISA ****4417", "4417"),
			("XXXX-4417", "4417"),
			("Card ending in 4417", "4417"),
			("DEBIT XX4417", "4417"),
		):
			with self.subTest(text=text):
				self.assertEqual(receipts.extract_receipt_signals(text)["card_last_four"], expected)

	def test_a_processor_or_survey_domain_is_not_taken_as_the_merchant(self):
		"""It identifies the till's supplier, not the shop — and it appears on
		receipts from hundreds of unrelated businesses."""
		text = "Paid with square.com\nRate us at surveymonkey.com"
		self.assertEqual(receipts.extract_receipt_signals(text)["merchant_url"], "")

	def test_a_phone_number_is_normalised_to_ten_digits_however_it_was_printed(self):
		for printed in ("(509) 555-0134", "509.555.0134", "+1 509 555 0134", "5095550134"):
			with self.subTest(printed=printed):
				self.assertEqual(receipts.normalize_phone(printed), "5095550134")
		self.assertEqual(receipts.normalize_phone("555-0134"), "")

	def test_a_domain_loses_its_scheme_its_www_and_its_path(self):
		for printed in ("https://www.acehardware.com/store/4102", "ACEHARDWARE.COM", "www.acehardware.com"):
			with self.subTest(printed=printed):
				self.assertEqual(receipts.normalize_domain(printed), "acehardware.com")
		self.assertEqual(receipts.normalize_domain("not a domain"), "")

	def test_a_subdomain_agrees_with_its_parent_and_a_lookalike_does_not(self):
		self.assertTrue(receipts._domains_agree("shop.acehardware.com", "acehardware.com"))
		self.assertFalse(receipts._domains_agree("notacehardware.com", "acehardware.com"))

	def test_a_full_card_number_is_refused_rather_than_truncated(self):
		"""Storing the fingerprint and silently dropping the rest would hide that
		a PAN was ever sent, which is the failure worth refusing over."""
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": SIATAPING,
				"amount": 43.18,
				"receipt_date": "2026-06-14",
				"company": MAIN,
				"submitted_by": EMPLOYEE,
				"card_last_four": "4111111111114417",
			},
		)
		self.assertIn("do not send it", error)
		self.assertEqual(STORE.rows(EXPENSE_RECEIPT), [])

	def test_a_signal_the_caller_sent_beats_one_read_off_the_text(self):
		"""The phone had the full-resolution image; this app has four regexes."""
		data = self.capture(ocr_raw_text=self.SLIP, card_last_four="9002")
		self.assertEqual(data["signals"]["card_last_four"], "9002")
		self.assertNotIn("card_last_four", data["signals_from_raw_text"])
		self.assertIn("merchant_phone", data["signals_from_raw_text"])


# ── 2 ───────────────────────────────────────────────────────────────────────
class AliasLearning(ReceiptIntelligenceTestCase):
	"""Coding one receipt by hand teaches every receipt that follows it."""

	def link(self, name, supplier=ACE):
		return self.tool_data("update_expense_receipt", {"name": name, "supplier": supplier})

	def test_setting_a_supplier_creates_the_alias(self):
		name = self.capture()["name"]
		learned = self.link(name)["alias_learned"]
		self.assertEqual(learned["action"], "created")
		self.assertEqual(learned["alias_key"], SIATAPING)
		row = self.alias_row(SIATAPING)
		self.assertEqual(row["canonical_supplier"], ACE)
		self.assertEqual(row["source"], receipts.ALIAS_MANUAL)
		self.assertEqual(int(row["match_count"]), 1)

	def test_the_receipt_that_taught_it_is_recorded_on_the_row(self):
		"""A mapping nobody can trace back to the paper that produced it is a
		mapping nobody can argue with."""
		name = self.capture()["name"]
		self.link(name)
		self.assertEqual(self.alias_row(SIATAPING)["first_learned_from"], name)

	def test_a_merchant_that_already_normalises_to_the_supplier_is_skipped(self):
		"""Name matching finds it, so a row here would be the identity mapping —
		one fact stored twice, and a second place for it to go wrong."""
		name = self.capture(merchant="SAWYER'S ACE HARDWARE, LLC")["name"]
		learned = self.link(name)["alias_learned"]
		self.assertEqual(learned["action"], "skipped")
		self.assertIn("stored twice", learned["why"])
		self.assertEqual(STORE.rows(MERCHANT_ALIAS), [])

	def test_relinking_to_a_different_supplier_repoints_and_keeps_the_count(self):
		"""A later decision by a person beats an earlier one — and zeroing the
		count would make a long-standing alias look brand new the day somebody
		corrected it."""
		self.link(self.capture()["name"])
		correction = self.capture()["name"]
		frappe.db.set_value(MERCHANT_ALIAS, SIATAPING, {"match_count": 9})
		learned = self.link(correction, supplier=MASTER_SUPPLIER)["alias_learned"]
		self.assertEqual(learned["action"], "repointed")
		self.assertEqual(learned["previous_supplier"], ACE)
		row = self.alias_row(SIATAPING)
		self.assertEqual(row["canonical_supplier"], MASTER_SUPPLIER)
		self.assertEqual(int(row["match_count"]), 9)

	def test_relinking_to_the_same_supplier_changes_nothing(self):
		self.link(self.capture()["name"])
		learned = receipts.learn_merchant_alias(SIATAPING, ACE)
		self.assertEqual(learned["action"], "unchanged")
		self.assertEqual(len(STORE.rows(MERCHANT_ALIAS)), 1)

	def test_the_second_receipt_needs_no_correcting_at_all(self):
		"""The point of the whole register: the bookkeeper codes ONE slip, and
		the next one arrives already coded — so `update_expense_receipt` has
		nothing left to change and says so."""
		self.link(self.capture()["name"])
		second = self.capture()
		self.assertEqual(second["supplier"], ACE)
		error = self.tool_error("update_expense_receipt", {"name": second["name"], "supplier": ACE})
		self.assertIn("Nothing to change", error)

	def test_the_receipts_own_resolution_follows_the_person(self):
		"""`Manual` at 1.0 is not this app being certain — it is this app
		recording that it was not asked."""
		name = self.capture()["name"]
		self.link(name)
		row = self.receipt_row(name)
		self.assertEqual(row["resolved_merchant"], ACE)
		self.assertEqual(row["resolution_method"], "Manual")
		self.assertEqual(float(row["resolution_confidence"]), 1.0)

	def test_clearing_a_supplier_teaches_nothing(self):
		name = self.capture(supplier=ACE)["name"]
		self.assertIsNone(
			self.tool_data("update_expense_receipt", {"name": name, "supplier": ""})["alias_learned"]
		)
		self.assertEqual(STORE.rows(MERCHANT_ALIAS), [])

	def test_a_register_that_refuses_a_row_does_not_fail_the_update(self):
		"""Learning is a side effect of a write that has already succeeded."""
		name = self.capture()["name"]
		original = frappe.get_doc

		def explode(payload, *args, **kwargs):
			wanted = payload.get("doctype") if isinstance(payload, dict) else payload
			if wanted == MERCHANT_ALIAS:
				raise frappe.ValidationError("the alias register is on fire")
			return original(payload, *args, **kwargs)

		frappe.get_doc = explode
		try:
			data = self.link(name)
		finally:
			frappe.get_doc = original
		self.assertEqual(data["after"]["supplier"], ACE)
		self.assertEqual(data["alias_learned"]["action"], "skipped")
		self.assertEqual(self.receipt_row(name)["supplier"], ACE)

	def test_the_taught_register_appears_beside_the_derived_one(self):
		self.link(self.capture()["name"])
		data = self.tool_data("list_merchant_aliases", {})
		self.assertEqual(data["taught_count"], 1)
		self.assertEqual(data["taught"][0]["alias"], SIATAPING)
		self.assertEqual(data["taught"][0]["canonical_supplier"], ACE)
		self.assertTrue(data["alias_register_installed"])
		# The derived view is unchanged and still answers its own question.
		self.assertEqual(data["count"], 1)


# ── 3 ───────────────────────────────────────────────────────────────────────
class AliasLookupOnSubmit(ReceiptIntelligenceTestCase):
	"""The next receipt with the same spelling resolves itself."""

	def teach(self, alias=SIATAPING, supplier=ACE, source=receipts.ALIAS_MANUAL):
		return receipts.learn_merchant_alias(alias, supplier, source=source)

	def test_a_taught_alias_resolves_the_next_capture(self):
		self.teach()
		data = self.capture()
		self.assertEqual(data["resolution_method"], "Alias")
		self.assertEqual(data["resolved_merchant"], ACE)
		self.assertEqual(data["resolution_confidence"], 1.0)

	def test_it_is_the_one_step_allowed_to_set_the_supplier_link(self):
		"""Not an inference — a replay of a link a person already made."""
		self.teach()
		data = self.capture()
		self.assertEqual(data["supplier"], ACE)
		self.assertIn("taught from a person's own supplier link", data["supplier_resolved_by"])

	def test_an_explicit_supplier_argument_is_never_overruled_by_an_alias(self):
		self.teach()
		data = self.capture(supplier=MASTER_SUPPLIER)
		self.assertEqual(data["supplier"], MASTER_SUPPLIER)
		self.assertEqual(data["supplier_resolved_by"], "argument")

	def test_a_different_spelling_of_the_same_key_still_finds_it(self):
		"""'Valley Co-op #14' and 'VALLEY CO-OP 14' are one row, not two."""
		self.teach(alias="Valley Co-op #14", supplier=MASTER_SUPPLIER)
		data = self.capture(merchant="VALLEY CO-OP 14")
		self.assertEqual(data["resolution_method"], "Alias")
		self.assertEqual(data["supplier"], MASTER_SUPPLIER)

	def test_each_resolution_counts_itself(self):
		"""The count is the only measure of whether teaching it was worth it."""
		self.teach()
		self.capture()
		self.capture()
		self.assertEqual(int(self.alias_row(SIATAPING)["match_count"]), 3)

	def test_a_learned_alias_replays_at_the_ceiling_and_not_at_one(self):
		"""It was a guess when it was written, and repetition does not make it a
		fact. Only a person's own decision reaches 1.0."""
		self.teach(source=receipts.ALIAS_LLM)
		data = self.capture()
		self.assertEqual(data["resolution_confidence"], receipts._MATCH_CEILING)

	def test_an_alias_pointing_at_a_deleted_supplier_is_treated_as_absent(self):
		"""Replaying a decision that points at nothing is not replaying a
		decision."""
		self.teach()
		# `force=True` from v0.83.0, when the harness learned Frappe's own
		# `check_if_doc_is_linked` and started refusing a delete something links
		# to. The alias IS the link, so an unforced delete here is refused on a
		# real bench too. What this test is about is the state AFTER the supplier
		# is gone — however it went — and forcing is how that state is reached.
		frappe.delete_doc("Supplier", ACE, force=True)
		self.assertIsNone(receipts.find_alias(SIATAPING))

	def test_the_resolution_is_stored_on_the_receipt(self):
		self.teach()
		name = self.capture()["name"]
		row = self.receipt_row(name)
		self.assertEqual(row["resolved_merchant"], ACE)
		self.assertEqual(row["resolution_method"], "Alias")

	def test_the_raw_merchant_line_is_never_overwritten_by_the_conclusion(self):
		"""Two columns for ever: overwriting the reading with the conclusion
		would delete the only evidence that the conclusion was wrong."""
		self.teach()
		row = self.receipt_row(self.capture()["name"])
		self.assertEqual(row["merchant"], SIATAPING)
		self.assertEqual(row["resolved_merchant"], ACE)


# ── 4 ───────────────────────────────────────────────────────────────────────
class UrlResolution(ReceiptIntelligenceTestCase):
	"""A domain is registered to one company."""

	def test_a_supplier_website_resolves_the_merchant(self):
		add_field("Supplier", "website")
		frappe.db.set_value("Supplier", ACE, {"website": "https://www.acehardware.com"})
		data = self.capture(merchant_url="acehardware.com")
		self.assertEqual(data["resolution_method"], "URL")
		self.assertEqual(data["resolved_merchant"], ACE)
		self.assertEqual(data["resolution_confidence"], receipts._URL_CONFIDENCE)

	def test_a_domain_match_never_sets_the_supplier_link(self):
		"""THE RULE THIS WHOLE RELEASE MUST NOT BREAK. Only a replayed human
		decision may set a link; a 0.90 domain match is still a suggestion, and a
		refactor that let it through would be invisible in every payload."""
		add_field("Supplier", "website")
		frappe.db.set_value("Supplier", ACE, {"website": "acehardware.com"})
		data = self.capture(merchant_url="acehardware.com")
		self.assertIsNone(data["supplier"])
		self.assertIsNone(data["supplier_resolved_by"])
		self.assertIsNone(self.receipt_row(data["name"])["supplier"])

	def test_receipts_already_coded_to_a_supplier_answer_for_the_domain(self):
		"""The same argument list_merchant_aliases makes: the answer is already
		on the site, sitting on receipts somebody coded by hand."""
		first = self.capture(merchant="ACE HDW", merchant_url="acehardware.com")["name"]
		self.tool_data("update_expense_receipt", {"name": first, "supplier": ACE})
		data = self.capture(merchant="SIATAPING2", merchant_url="www.acehardware.com/store")
		self.assertEqual(data["resolution_method"], "URL")
		self.assertEqual(data["resolved_merchant"], ACE)
		self.assertEqual(
			data["resolution_confidence"], round(receipts._URL_CONFIDENCE - receipts._CORPUS_PENALTY, 4)
		)

	def test_a_processor_domain_resolves_nothing_and_says_why(self):
		data = self.capture(merchant_url="squareup.com")
		steps = {step["step"]: step for step in data["resolution_steps"]}
		self.assertIsNone(steps["url"]["matched"])
		self.assertIn("payment processor", steps["url"]["why"])

	def test_two_suppliers_claiming_one_domain_resolve_to_neither(self):
		"""With two answers there is no answer, and inventing one is worse."""
		add_field("Supplier", "website")
		frappe.db.set_value("Supplier", ACE, {"website": "acehardware.com"})
		frappe.db.set_value("Supplier", MASTER_SUPPLIER, {"website": "acehardware.com"})
		data = self.capture(merchant_url="acehardware.com")
		self.assertNotEqual(data["resolution_method"], "URL")

	def test_a_malformed_url_is_refused_before_anything_is_written(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": SIATAPING,
				"amount": 12,
				"receipt_date": "2026-06-14",
				"company": MAIN,
				"submitted_by": EMPLOYEE,
				"merchant_url": "not a url",
			},
		)
		self.assertIn("is not a domain", error)
		self.assertEqual(STORE.rows(EXPENSE_RECEIPT), [])


# ── 5 ───────────────────────────────────────────────────────────────────────
class PhoneResolution(ReceiptIntelligenceTestCase):
	"""A number rings in one building."""

	def test_a_number_already_coded_elsewhere_resolves_this_receipt(self):
		first = self.capture(merchant="ACE HDW", merchant_phone="(509) 555-0134")["name"]
		self.tool_data("update_expense_receipt", {"name": first, "supplier": ACE})
		data = self.capture(merchant="SIATAPING2", merchant_phone="509.555.0134")
		self.assertEqual(data["resolution_method"], "Phone")
		self.assertEqual(data["resolved_merchant"], ACE)

	def test_a_phone_match_never_sets_the_supplier_link_either(self):
		first = self.capture(merchant="ACE HDW", merchant_phone="5095550134")["name"]
		self.tool_data("update_expense_receipt", {"name": first, "supplier": ACE})
		data = self.capture(merchant="SIATAPING2", merchant_phone="5095550134")
		self.assertIsNone(data["supplier"])

	def test_receipts_disagreeing_about_a_number_resolve_to_neither(self):
		one = self.capture(merchant="A", merchant_phone="5095550134")["name"]
		two = self.capture(merchant="B", merchant_phone="5095550134")["name"]
		self.tool_data("update_expense_receipt", {"name": one, "supplier": ACE})
		self.tool_data("update_expense_receipt", {"name": two, "supplier": MASTER_SUPPLIER})
		data = self.capture(merchant="SIATAPING2", merchant_phone="5095550134")
		self.assertNotEqual(data["resolution_method"], "Phone")

	def test_a_malformed_phone_is_refused_before_anything_is_written(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": SIATAPING,
				"amount": 12,
				"receipt_date": "2026-06-14",
				"company": MAIN,
				"submitted_by": EMPLOYEE,
				"merchant_phone": "555-0134",
			},
		)
		self.assertIn("ten-digit", error)
		self.assertEqual(STORE.rows(EXPENSE_RECEIPT), [])


# ── 6 ───────────────────────────────────────────────────────────────────────
class CascadeOrdering(ReceiptIntelligenceTestCase):
	"""Best evidence first, and every step recorded including the silent ones."""

	def setUp(self):
		super().setUp()
		add_field("Supplier", "website")
		frappe.db.set_value("Supplier", "Cascade Irrigation Supply", {"website": "cascadeirrigation.com"})

	def resolve(self, **kwargs):
		return receipts.resolve_merchant(SIATAPING, **kwargs)

	def test_an_alias_outranks_a_domain(self):
		receipts.learn_merchant_alias(SIATAPING, ACE)
		out = self.resolve(merchant_url="cascadeirrigation.com")
		self.assertEqual(out["method"], "Alias")
		self.assertEqual(out["supplier"], ACE)

	def test_a_domain_outranks_a_name_score(self):
		out = receipts.resolve_merchant("CASCADE IRRIGATION SUPPLY CO", merchant_url="cascadeirrigation.com")
		self.assertEqual(out["method"], "URL")
		self.assertGreater(out["confidence"], receipts._MATCH_CEILING - 0.1)

	def test_the_name_score_is_still_the_last_deterministic_word(self):
		out = receipts.resolve_merchant("CASCADE IRRIGATION SUPPLY CO")
		self.assertEqual(out["method"], "OCR")
		self.assertEqual(out["supplier"], "Cascade Irrigation Supply")

	def test_the_steps_are_ordered_and_name_what_they_did_not_find(self):
		"""'Why didn't the URL match' is the interesting question on the day
		somebody's receipts stop resolving."""
		out = self.resolve()
		self.assertEqual([step["step"] for step in out["steps"]], ["alias", "url", "phone", "ocr", "llm"])
		for step in out["steps"]:
			with self.subTest(step=step["step"]):
				self.assertTrue(step["why"])

	def test_a_short_cascade_stops_at_its_winner(self):
		receipts.learn_merchant_alias(SIATAPING, ACE)
		self.assertEqual([step["step"] for step in self.resolve()["steps"]], ["alias"])

	def test_the_step_that_won_names_the_supplier_it_matched(self):
		out = receipts.resolve_merchant("CASCADE IRRIGATION SUPPLY CO", merchant_url="cascadeirrigation.com")
		url_step = next(step for step in out["steps"] if step["step"] == "url")
		self.assertEqual(url_step["matched"], "Cascade Irrigation Supply")

	def test_a_site_with_no_alias_register_drops_step_one_rather_than_raising(self):
		"""A bench that pulled the code without migrating has the tools and not
		the table, and no evidence is not an error."""
		INSTALLED_DOCTYPES.discard(MERCHANT_ALIAS)
		self.addCleanup(INSTALLED_DOCTYPES.add, MERCHANT_ALIAS)
		out = self.resolve()
		alias_step = out["steps"][0]
		self.assertFalse(alias_step["tried"])
		self.assertIn("bench migrate", alias_step["why"])

	def test_normalize_merchant_keeps_the_shape_a_v0_68_client_reads(self):
		"""`match` and `alternatives` still mean the NAME match specifically."""
		data = self.tool_data("normalize_merchant", {"merchant": "CASCADE IRRIGATION SUPPLY CO"})
		self.assertEqual(data["match"]["supplier"], "Cascade Irrigation Supply")
		self.assertIn("alternatives", data)
		self.assertEqual(data["threshold"], receipts._MATCH_FLOOR)
		self.assertEqual(data["resolution"]["method"], "OCR")

	def test_normalize_merchant_reads_the_signals_off_raw_text_alone(self):
		data = self.tool_data(
			"normalize_merchant",
			{"merchant": SIATAPING, "ocr_raw_text": "visit www.cascadeirrigation.com today"},
		)
		self.assertEqual(data["resolution"]["method"], "URL")
		self.assertIn("merchant_url", data["resolution"]["signals_from_raw_text"])


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheLlmHandoff(ReceiptIntelligenceTestCase):
	"""The question is prepared here; it is never asked from here."""

	SLIP = "Thank you for shopping at Sawyer's!\nAce Rewards member 8827341\nITEM 9931 2.10\n"

	def test_nothing_deterministic_leaves_a_question_rather_than_a_guess(self):
		out = receipts.resolve_merchant(SIATAPING, ocr_raw_text=self.SLIP)
		self.assertIsNone(out["method"])
		self.assertIsNotNone(out["llm_context"])
		self.assertIn("Ace Rewards member 8827341", out["llm_context"]["raw_text_lines"])
		self.assertIn("candidate_suppliers", out["llm_context"])

	def test_the_cascade_never_calls_a_model(self):
		"""THE CONTRACT WITH document_intel, asserted rather than assumed: a step
		that quietly dialled an API would put a network call, a key and a bill
		inside a `bench migrate`."""
		import urllib.request

		def forbidden(*args, **kwargs):  # pragma: no cover - reached only on failure
			raise AssertionError("the resolver made a network call")

		original = urllib.request.urlopen
		urllib.request.urlopen = forbidden
		try:
			out = receipts.resolve_merchant(SIATAPING, ocr_raw_text=self.SLIP)
		finally:
			urllib.request.urlopen = original
		self.assertIn("NOTHING HERE HAS BEEN SENT ANYWHERE", out["llm_context"]["note"])

	def test_a_resolved_receipt_carries_no_question(self):
		receipts.learn_merchant_alias(SIATAPING, ACE)
		self.assertIsNone(receipts.resolve_merchant(SIATAPING, ocr_raw_text=self.SLIP)["llm_context"])

	def test_a_callers_own_answer_short_circuits_the_cascade(self):
		data = self.capture(
			ocr_raw_text=self.SLIP,
			resolved_merchant=ACE,
			resolution_method="LLM",
			resolution_confidence=0.82,
		)
		self.assertEqual(data["resolution_method"], "LLM")
		self.assertEqual(data["resolved_merchant"], ACE)
		self.assertEqual(data["resolution_confidence"], 0.82)

	def test_the_deterministic_steps_still_run_and_are_still_recorded(self):
		"""Which is the only way anybody ever finds out the model was wrong."""
		data = self.capture(resolved_merchant=ACE, resolution_method="LLM")
		steps = [step["step"] for step in data["resolution_steps"]]
		self.assertEqual(steps, ["caller", "url", "phone", "ocr"])

	def test_a_callers_answer_does_not_set_the_supplier_link(self):
		data = self.capture(resolved_merchant=ACE, resolution_method="LLM")
		self.assertIsNone(data["supplier"])

	def test_a_method_with_no_answer_beside_it_is_refused(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": SIATAPING,
				"amount": 12,
				"receipt_date": "2026-06-14",
				"company": MAIN,
				"submitted_by": EMPLOYEE,
				"resolution_method": "LLM",
			},
		)
		self.assertIn("records how nothing was decided", error)

	def test_an_unknown_method_is_refused_with_the_list(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": SIATAPING,
				"amount": 12,
				"receipt_date": "2026-06-14",
				"company": MAIN,
				"submitted_by": EMPLOYEE,
				"resolved_merchant": ACE,
				"resolution_method": "Vibes",
			},
		)
		for method in receipts.RESOLUTION_METHODS:
			self.assertIn(method, error)

	def test_a_confidence_outside_zero_to_one_is_refused_as_a_percentage(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": SIATAPING,
				"amount": 12,
				"receipt_date": "2026-06-14",
				"company": MAIN,
				"submitted_by": EMPLOYEE,
				"resolved_merchant": ACE,
				"resolution_method": "LLM",
				"resolution_confidence": 87,
			},
		)
		self.assertIn("87 means 0.87", error)


# ── 8 ───────────────────────────────────────────────────────────────────────
CARD_STATEMENT = (
	("BT-CARD-4417", "2026-06-15", "CHEVRON 0093746 PASCO WA XXXX4417", 47.83),
	("BT-CARD-9002", "2026-06-15", "CHEVRON 0093746 PASCO WA XXXX9002", 47.83),
	("BT-PLAIN", "2026-06-20", "POS PURCHASE 4417 TERMINAL 88", 31.00),
)


class CardFingerprint(ReceiptIntelligenceTestCase):
	"""The bank naming the physical card settles what used to be contested."""

	def setUp(self):
		super().setUp()
		STORE.seed(
			BANK_TRANSACTION,
			[
				{
					"name": name,
					"date": date,
					"bank_account": BANK_ACCOUNT,
					"company": MAIN,
					"description": description,
					"status": "Unreconciled",
					"deposit": 0,
					"withdrawal": withdrawal,
					"allocated_amount": 0,
					"unallocated_amount": withdrawal,
					"currency": "USD",
					"docstatus": 1,
					"payment_entries": [],
				}
				for name, date, description, withdrawal in CARD_STATEMENT
			],
		)

	def fuel(self, card, **overrides):
		payload = {
			"merchant": "Chevron",
			"amount": 47.83,
			"receipt_date": "2026-06-14",
			"category": "Fuel",
			"card_last_four": card,
		}
		payload.update(overrides)
		return self.capture(**payload)["name"]

	def test_a_masked_card_in_a_memo_line_is_a_fingerprint(self):
		self.assertTrue(banking_bridge.card_fingerprint("4417", "CHEVRON XXXX4417", 1))

	def test_a_bare_number_in_a_memo_line_is_not(self):
		"""A memo line is full of terminal ids and authorisation codes, and a
		fingerprint that matched one would be worse than none at all."""
		self.assertFalse(banking_bridge.card_fingerprint("4417", "POS PURCHASE 4417 TERMINAL 88", 0))

	def test_a_match_outside_a_day_is_not_a_fingerprint(self):
		"""The wide window is for FINDING a match; this is for confirming one,
		and a same-card same-amount charge a week later is next week's fill-up."""
		self.assertFalse(banking_bridge.card_fingerprint("4417", "CHEVRON XXXX4417", 6))

	def test_a_receipt_with_no_card_scores_exactly_what_it_scored_before(self):
		without = banking_bridge.score_match(
			{"name": "R", "amount": 47.83, "receipt_date": "2026-06-14", "merchant": "Chevron"},
			banking_bridge._transaction_row("BT-CARD-4417"),
			tolerance=0.02,
			window=7,
		)
		self.assertFalse(without["card_fingerprint"])
		self.assertEqual(without["signals"]["card_fingerprint_bonus"], 0.0)

	def test_a_fingerprint_lifts_the_score_toward_the_ceiling(self):
		transaction = banking_bridge._transaction_row("BT-CARD-4417")
		base = {"name": "R", "amount": 47.83, "receipt_date": "2026-06-14", "merchant": "Chevron"}
		without = banking_bridge.score_match(base, transaction, tolerance=0.02, window=7)
		with_card = banking_bridge.score_match(
			{**base, "card_last_four": "4417"}, transaction, tolerance=0.02, window=7
		)
		self.assertTrue(with_card["card_fingerprint"])
		self.assertGreater(with_card["confidence"], without["confidence"])

	def test_two_identical_receipts_are_told_apart_by_their_cards(self):
		"""THE CASE THIS FEATURE EXISTS FOR. Two trucks, two drivers, one amount,
		one day, one station — and two cards."""
		self.fuel("4417")
		self.fuel("9002")
		data = self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertEqual(data["card_fingerprint_count"], 2)
		self.assertEqual(data["contested"], [])
		by_transaction = {row["bank_transaction"]: row for row in data["proposals"]}
		self.assertEqual(
			{name: row["signals"]["card_last_four"] for name, row in by_transaction.items()},
			{"BT-CARD-4417": "4417", "BT-CARD-9002": "9002"},
		)

	def test_a_fingerprint_outranks_a_higher_score_without_one(self):
		"""The bank naming the physical card is better evidence about WHICH slip
		this is than any margin in a similarity number."""
		fingerprinted = {"card_fingerprint": True, "confidence": 0.71}
		higher = {"card_fingerprint": False, "confidence": 0.93}
		self.assertTrue(banking_bridge._outranks(fingerprinted, higher))
		self.assertFalse(banking_bridge._outranks(higher, fingerprinted))

	def test_two_receipts_with_no_cards_are_still_contested(self):
		"""Nothing has changed for the case the fingerprint cannot answer."""
		frappe.delete_doc(BANK_TRANSACTION, "BT-CARD-9002")
		self.fuel("")
		self.fuel("")
		data = self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertEqual(len(data["contested"]), 1)
		self.assertIn("warning", data)

	def test_auto_match_still_writes_nothing(self):
		name = self.fuel("4417")
		self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertFalse(self.receipt_row(name).get(banking_bridge.RECEIPT_LINK_FIELD))
		self.assertFalse(self.tool_data("auto_match_receipts", {"company": MAIN})["committed"])


# ── 9 ───────────────────────────────────────────────────────────────────────
class TheSchema(ReceiptIntelligenceTestCase):
	"""The seven columns, the vocabulary they hold, and the installer."""

	def setUp(self):
		super().setUp()
		self.alias = _load_app_doctype("merchant_alias")

	def test_the_installer_adds_all_seven_columns(self):
		self.assertTrue(receipts.ensure_receipt_intelligence_fields())
		for fieldname in receipts.INTELLIGENCE_FIELDS:
			with self.subTest(field=fieldname):
				self.assertTrue(frappe.get_meta(EXPENSE_RECEIPT).has_field(fieldname))

	def test_running_it_twice_creates_nothing_the_second_time(self):
		receipts.ensure_receipt_intelligence_fields()
		before = len(STORE.rows("Custom Field"))
		self.assertTrue(receipts.ensure_receipt_intelligence_fields())
		self.assertEqual(len(STORE.rows("Custom Field")), before)

	def test_migrate_installs_them(self):
		"""The columns exist before anybody needs them, which is what makes them
		filterable in the Desk."""
		install._receipt_intelligence_fields()
		self.assertTrue(receipts._intelligence_fields_present())

	def test_capture_still_works_and_still_resolves_without_the_columns(self):
		"""A site that will not take them loses the STORING of the answer and
		nothing else — not the computing of it, and not capture."""
		receipts.learn_merchant_alias(SIATAPING, ACE)
		original = receipts.ensure_receipt_intelligence_fields
		receipts.ensure_receipt_intelligence_fields = lambda: False
		try:
			data = self.capture()
		finally:
			receipts.ensure_receipt_intelligence_fields = original
		self.assertFalse(data["intelligence_fields_installed"])
		self.assertEqual(data["resolved_merchant"], ACE)
		self.assertIn("bench migrate", data["intelligence_note"])

	def test_the_resolution_methods_agree_between_the_column_and_the_constant(self):
		receipts.ensure_receipt_intelligence_fields()
		row = frappe.db.get_value(
			"Custom Field", {"dt": EXPENSE_RECEIPT, "fieldname": "resolution_method"}, "options"
		)
		self.assertEqual(tuple(str(row).strip().split("\n")), receipts.RESOLUTION_METHODS)

	def test_the_alias_sources_agree_between_the_json_and_the_constant(self):
		by_name = {field["fieldname"]: field for field in self.alias["fields"]}
		self.assertEqual(tuple(by_name["source"]["options"].split("\n")), receipts.ALIAS_SOURCES)

	def test_the_alias_links_to_a_supplier_and_to_the_receipt_that_taught_it(self):
		by_name = {field["fieldname"]: field for field in self.alias["fields"]}
		self.assertEqual(by_name["canonical_supplier"]["options"], "Supplier")
		self.assertEqual(by_name["first_learned_from"]["options"], EXPENSE_RECEIPT)

	def test_the_alias_has_no_company_column(self):
		"""One alias key resolves to exactly one Supplier, and a company column
		would break that by making the key non-unique — see test_permissions.py
		for the whole argument."""
		self.assertNotIn("company", [field["fieldname"] for field in self.alias["fields"]])

	def test_the_docname_is_the_normalised_key(self):
		"""Which is what makes 'one alias, one Supplier' a primary key rather
		than a rule somebody has to remember to run."""
		learned = receipts.learn_merchant_alias("Valley Co-op #14", MASTER_SUPPLIER)
		self.assertEqual(learned["action"], "created")
		# "CO" is a legal-form word and is stripped from both sides, which is the
		# same normaliser `_merchant_similarity` has always used.
		self.assertEqual(learned["alias_key"], "VALLEY OP 14")
		self.assertEqual(self.alias_row("VALLEY OP 14")["alias_key"], "VALLEY OP 14")

	def test_an_alias_that_normalises_to_nothing_is_refused(self):
		doc = frappe.get_doc({"doctype": MERCHANT_ALIAS, "alias": "LLC, Inc.", "canonical_supplier": ACE})
		with self.assertRaises(Exception) as caught:
			doc.insert()
		self.assertIn("empty key", str(caught.exception))

	def test_the_controller_is_importable_and_is_a_document(self):
		"""The v0.7.1 failure: a DocType JSON with no Python module beside it and
		a `bench migrate` that dies with ModuleNotFoundError."""
		self.assertTrue(issubclass(MerchantAlias, frappe.model.document.Document))

	def test_a_bad_source_is_refused_on_save(self):
		doc = frappe.get_doc(
			{"doctype": MERCHANT_ALIAS, "alias": SIATAPING, "canonical_supplier": ACE, "source": "manual"}
		)
		with self.assertRaises(Exception) as caught:
			doc.insert()
		self.assertIn("not an alias source", str(caught.exception))

	def test_the_register_is_not_declared_precious(self):
		"""It is LEARNED data and every row is recoverable from the Expense
		Receipts that taught it — which is exactly what list_merchant_aliases'
		derived half reads. Warning about it on uninstall would dilute a list
		whose entries are each the only copy of something."""
		self.assertNotIn(MERCHANT_ALIAS, [doctype for doctype, _what in install._PRECIOUS_DOCTYPES])

	def test_the_new_arguments_are_all_declared_on_the_tool(self):
		"""An argument a tool accepts and does not declare is an argument no
		client can discover."""
		schema = registry.TOOLS["submit_expense_receipt"]["inputSchema"]["properties"]
		for name in (
			"card_last_four",
			"merchant_phone",
			"merchant_url",
			"store_number",
			"resolved_merchant",
			"resolution_method",
			"resolution_confidence",
		):
			with self.subTest(argument=name):
				self.assertIn(name, schema)

	def test_the_read_tool_refuses_a_full_card_number_too(self):
		"""A caller who gets no refusal here learns nothing and sends it again to
		the tool that stores things."""
		error = self.tool_error(
			"normalize_merchant", {"merchant": SIATAPING, "card_last_four": "4111111111114417"}
		)
		self.assertIn("do not send it", error)


# ── v0.183.0 ────────────────────────────────────────────────────────────────
class TheNightlyRunCannotLinkTheWrongSupplier(ReceiptIntelligenceTestCase):
	"""EXR-2026-0015: COASTAL FARM STORES resolved to Sawyer's Hardware by Phone.

	THREE DEFECTS, ONE RECEIPT. The phone pattern took the card terminal's
	`TVR : 0000000000` as a phone number; nothing asked whether ten zeros could
	ring a shop; and the phone verdict beat a printed name that plainly said
	Coastal. Each is fixed separately, and each is tested separately, because a
	nightly job with nobody watching needs all three to hold.
	"""

	COASTAL = "Coastal Farm and Ranch"
	SAWYERS = "Sawyer's Hardware Llc"

	#: The slip's own lines, as the scanner read them.
	SLIP = """THANK YOU FOR SHOPPING AT
COASTAL - THE DALLES
COASTAL FARM STORES
2600 W 6TH ST
THE DALLES, OR 97058
(541), 296-9610
09/24/26 4:37PM 260502
PROWLER RATKILLER REFILL 15PK
TOTAL: $
137.95
xXXXXXXXXXXX0845
TVR : 0000000000
IAD : 06061203A00000
Shop online at CoastalCountry.com"""

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Supplier",
			[
				{"name": self.COASTAL, "supplier_name": self.COASTAL},
				{"name": self.SAWYERS, "supplier_name": self.SAWYERS},
			],
		)

	# ── the reading ─────────────────────────────────────────────────────────
	def test_the_terminals_zeros_are_not_the_shops_phone(self):
		found = receipts.extract_receipt_signals(self.SLIP)
		self.assertEqual(found["merchant_phone"], "5412969610", "the comma'd number is the real one")

	def test_a_bare_ten_digit_run_is_not_a_phone(self):
		found = receipts.extract_receipt_signals("SKU 0123456789\nUPC 4006381333")
		self.assertEqual(found["merchant_phone"], "")

	def test_plausibility(self):
		self.assertTrue(receipts.plausible_phone("5412969610"))
		self.assertTrue(receipts.plausible_phone("5095550134"))
		for junk in ("0000000000", "9999999999", "1234567890", "5410296961", "541296961"):
			self.assertFalse(receipts.plausible_phone(junk), junk)

	# ── the cascade ─────────────────────────────────────────────────────────
	def test_a_placeholder_phone_is_dropped_not_matched_and_not_stored(self):
		# The earlier receipt that taught the zeros a supplier.
		first = self.capture(merchant="SAWYERS HDW", merchant_phone="0000000000")["name"]
		self.tool_data("update_expense_receipt", {"name": first, "supplier": self.SAWYERS})

		data = self.capture(merchant="COASTAL FARM STORES", merchant_phone="0000000000",
		                    ocr_raw_text=self.SLIP)
		self.assertNotEqual(data["resolution_method"], "Phone")
		self.assertEqual(data["resolved_merchant"], self.COASTAL)
		self.assertIsNone(self.receipt_row(data["name"]).get("merchant_phone") or None)

	def test_a_phone_that_contradicts_the_printed_name_is_capped_and_flagged(self):
		first = self.capture(merchant="SAWYERS HDW", merchant_phone="(541) 296-9610")["name"]
		self.tool_data("update_expense_receipt", {"name": first, "supplier": self.SAWYERS})

		answer = receipts.resolve_merchant("COASTAL FARM STORES", merchant_phone="5412969610")
		self.assertEqual(answer["method"], "Phone")
		self.assertLessEqual(answer["confidence"], receipts._CONFLICT_CAP)
		self.assertEqual(answer["conflict"]["name_supplier"], self.COASTAL)
		self.assertFalse(answer["auto_link_safe"])

	def test_normalize_merchant_says_whether_an_unattended_link_is_safe(self):
		exact = self.tool_data("normalize_merchant", {"merchant": "COASTAL FARM AND RANCH"})
		self.assertTrue(exact["auto_link_safe"])
		loose = self.tool_data("normalize_merchant", {"merchant": "COASTAL FARM STORES"})
		self.assertFalse(loose["auto_link_safe"], "a 0.6-ish name match is a person's to confirm")

	# ── the write ───────────────────────────────────────────────────────────
	def test_an_automations_link_teaches_no_alias(self):
		name = self.capture(merchant="COASTAL FARM STORES")["name"]
		data = self.tool_data(
			"update_expense_receipt", {"name": name, "supplier": self.COASTAL, "linked_by": "automation"}
		)
		self.assertEqual(data["alias_learned"]["action"], "skipped")
		self.assertIsNone(self.alias_row(receipts.alias_key("COASTAL FARM STORES")))

	def test_a_persons_link_still_teaches_one(self):
		name = self.capture(merchant="COASTAL FARM STORES")["name"]
		data = self.tool_data("update_expense_receipt", {"name": name, "supplier": self.COASTAL})
		self.assertNotEqual(data["alias_learned"]["action"], "skipped")
