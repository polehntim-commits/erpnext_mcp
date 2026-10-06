"""The refusals a worker hits, in Spanish. v0.254.0 (`error_phrases`, `api/guard._stamp_error`)."""

import re
import unittest

import frappe

from erpnext_mcp import error_phrases
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import translations

from .harness import set_roles
from .test_api_mobile import WORKER, MobileAPITestCase

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: One real message per phrase, as the code writes it.
SAMPLES = {
	"error.task.on_hold": "FT-2026-01-00007 is on Hold: Hold: dry_ahead — Rain likely this week. A supervisor can let it start today with a reason (override_hold).",
	"error.task.draft": "FT-2026-01-00007 is still a Draft and is not in the pool. Somebody has to publish it first. Nothing was changed.",
	"error.task.held_by_other_state": "FT-2026-01-00007 is In-Progress and held by Ana Ramos. Two people stood in front of the same work both believing it is theirs is exactly what a dispatch board exists to prevent. Nothing was changed.",
	"error.task.crew_task": "FT-2026-01-00007 is a crew task: nobody takes it from the pool. A foreman puts people on its crew with add_to_crew_task, and one supervisor closes it. Nothing was changed.",
	"error.task.dispatched_only": "FT-2026-01-00007 is dispatch_mode Dispatched: somebody has to be SENT to it by name.",
	"error.task.claim_limit": "Ana Ramos is already holding 3 task(s): FT-1, FT-2, FT-3. The limit is 3 at once.",
	"error.task.not_yours": "FT-2026-01-00007 is held by Ana Ramos, not HR-EMP-00002. Nothing was changed.",
	"error.task.already_held": "FT-2026-01-00007 is already held by Ana Ramos. Nothing was changed.",
	"error.task.already_started": "FTA-0001 was already started at 2026-01-15 07:02:00. Starting it twice would move the clock-in forward.",
	"error.task.already_paused": "FTA-0001 was already paused at 2026-01-15 10:00:00. Pausing it twice would close a segment that is not open.",
	"error.task.not_in_progress": "FTA-0001 is Claimed, not In-Progress. Only work that is actually being done can be interrupted.",
	"error.task.already_in_progress": "FTA-0001 is already in progress. Nothing was changed.",
	"error.task.nothing_to_claim": "FT-2026-01-00007 is Completed. There is nothing to claim. Nothing was changed.",
	"error.task.waiting_on": "FT-2026-01-00007 is waiting on FT-2026-01-00003 (In-Progress). Nothing was changed.",
	"error.task.steps_open": "FT-2026-01-00007 has 2 step(s) still open: FT-8, FT-9.",
	"error.task.checklist_open": "FT-2026-01-00007 cannot be completed: 3 required checklist item(s) are not marked done.\n\n- gloves",
	"error.task.evidence_contract": "FT-2026-01-00007 cannot be completed: its evidence contract is not met.\n\n- photo",
	"error.task.crew_led": "FT-2026-01-00007 is a crew task led by Ana Ramos. One supervisor closes it, with one set of evidence.",
	"error.task.cannot_complete": "FTA-0001 is Rejected and cannot be completed. Nothing was changed.",
	"error.task.cannot_reject": "FTA-0001 is Completed and cannot be rejected. Nothing was changed.",
	"error.task.reason_to_reject": "A reason is required to hand a task back. 'The ladder is broken' is a fact somebody can act on.",
	"error.task.findings_missing": "clean_pass=false says something was found, and findings_text is empty.",
	"error.task.hours_reading": "hours_reading must be the number on the hour meter, e.g. 1240.5 — got 'abc'. Nothing was changed.",
	"error.task.phi": "B7 is inside a pre-harvest interval until 2026-07-02 (Captan, applied 2026-06-28).",
	"error.task.minor": "Luis Ramos is 16 (born 2010-03-01), and FT-2026-01-00007 is a Spray task. Minors may not apply pesticides. Nothing was claimed.",
	"error.task.no_employee": "picker@example.com has no Employee record on this site, and a task assignment names an Employee rather than a login.",
	"error.report.rate_limited": "Ana Ramos (HR-EMP-00002) has already filed 5 field reports in the last hour. The limit is 5. Nothing was created.",
	"error.report.critical_role": "Critical urgency on a field report is restricted to Foreman and Farm Manager roles. A field worker may choose Normal or High.",
	"error.hold.reason": "a reason is required (a few words: why it is safe to start today). Nothing was changed.",
	"error.hold.not_supervisor": "picker@example.com cannot override a Hold — a Foreman, Farm Manager or System Manager can. Nothing was changed.",
	"error.shift.on_other": "Ana Ramos is already on SHIFT-2026-0004, an OPEN shift at B7 that started at 2026-01-15 07:00:00 under Sam Doyle.",
	"error.shift.over": "SHIFT-2026-0004 ended at 2026-01-15 15:30:00. Nobody joins a shift that is over.",
	"error.shift.already_on_crew": "Ana Ramos is already on this crew, joined at 2026-01-15 07:00:00",
	"error.shift.not_on_crew": "HR-EMP-00002 is not on the crew of SHIFT-2026-0004. get_shift lists who is. Nothing was changed.",
	"error.shift.already_left": "Ana Ramos already left SHIFT-2026-0004 at 2026-01-15 12:00:00. Pass left_at explicitly to correct that time.",
	"error.shift.already_cancelled": "SHIFT-2026-0004 was already cancelled at 2026-01-15 06:45:00: heat. Nothing was changed.",
	"error.shift.already_closed": "SHIFT-2026-0004 was already closed at 2026-01-15 15:30:00 by a review signed on 2026-01-15.",
	"error.shift.cancel_reason": "cancellation_reason is required. 'Crew stood down at 06:40, heat index already 94 °F' is a record.",
	"error.shift.signature": "supervisor_signature_file_token is required to close a shift. FSMA §112.161(b) asks for a review.",
	"error.shift.ends_before_start": "this call ends the shift at 2026-01-15 06:00:00 and it started at 2026-01-15 07:00:00 — it would have finished before it began.",
	"error.shift.needs_location": "you have no shift open, so this starts one — and a shift needs a location (the block or the yard). Nothing was changed.",
	"error.shift.break_end": "ended_at (2026-01-15 09:00:00) is before the break started (2026-01-15 09:10:00). A break cannot end before it began.",
	"error.punch.locked": "that punch was reviewed and is locked for payroll; it cannot be changed until a manager reopens it. Nothing was changed.",
	"error.punch.reason": "approve needs a reason (a few words). Nothing was changed.",
	"error.contacts.role": "The contact register is restricted to a Foreman, a Farm Manager, the bookkeeper (Accounts Manager / Accounts User) or a System Manager. Nothing was read or saved.",
	"error.role.restricted": "Adding a product is restricted to Farm Manager and Foreman. This account holds a Farm Ops credential and none of those roles.",
	"error.contacts.where_met": "give met_at and/or met_on. Nothing was changed.",
	"error.asset.duplicate_tag": "Asset Register already has a record called 'MC-EQ-SHEAD'. The docname IS the printable tag ID. Nothing was created.",
	"error.item.no_uom": "no UOM called 'Noi' on this site. Known units include: Box, Nos. Nothing was created.",
	"error.receipt.nothing_changed": "expense receipt EXR-2026-0012 already reads what was asked for on every field named (cost_center).",
	"error.upload.too_big": "file_content is over the 8 MB inline limit — upload it with stage_file_chunk and finalize_staged_file.",
}

#: Imperatives and pronouns of the formal register (usted). The app speaks tú.
FORMAL = re.compile(
	r"\b(usted|Usted|Avise|avise|Pida|pida|Intente|intente|Espere|espere|Termine|Ciérrelo|Pregunte|"
	r"pregunte|Tome|tome|Firme|Escriba|escriba|Toque|toque|Escanee|escanee|Abra|abra|Elija|elija|"
	r"Marque|marque|Describa|describa|Revise|Actualice|actualice|Está registrado|Ha salido|"
	r"Le corresponde|su supervisor|su operador|Su turno|Su descanso)\b"
)


class EveryPhraseIsReal(unittest.TestCase):
	def test_each_phrase_matches_a_real_message_and_fills_both_languages(self):
		lines = {key: (en, es) for key, en, es in error_phrases.catalogue()}
		self.assertEqual(set(SAMPLES), set(lines), "a sample for every phrase, and no phrase without one")
		for key, message in SAMPLES.items():
			with self.subTest(key=key):
				got, fill = error_phrases.match(message)
				self.assertEqual(got, key)
				en, es = lines[key]
				for line in (en, es):
					self.assertLessEqual(set(PLACEHOLDER.findall(line)), set(fill), line)
				self.assertNotIn("{", translations.render(es, **fill))

	def test_an_unknown_message_matches_nothing(self):
		self.assertEqual(error_phrases.match("debit and credit do not balance by 0.01"), ("", {}))

	def test_a_specific_sentence_is_not_swallowed_by_a_general_one(self):
		self.assertEqual(error_phrases.match(SAMPLES["error.contacts.role"])[0], "error.contacts.role")
		self.assertEqual(error_phrases.match(SAMPLES["error.task.already_held"])[0], "error.task.already_held")

	def test_the_whole_shipped_catalogue_speaks_tu(self):
		for key, (_category, _en, es) in translations.SHIPPED.items():
			with self.subTest(key=key):
				self.assertIsNone(FORMAL.search(es), es)


class ASpanishPhoneReadsWhy(MobileAPITestCase):
	def test_a_refusal_with_no_key_arrives_in_spanish_beside_the_english(self):
		translations.install_translations()
		frappe.db.commit()  # a refusal rolls back; on a site the catalogue is committed at migrate
		set_roles(WORKER, ["Field Worker"])
		self.request({}, headers={"Accept-Language": "es-MX"}, remote_addr="100.64.0.7")
		frappe.local.session.user = WORKER
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.update_contact_where_met(contact="Ben Sheppard", met_at="Hood River")
		self.assertIn("The contact register is restricted", str(caught.exception), "the English is unchanged")
		response = frappe.local.response
		self.assertEqual(response["error_key"], "error.contacts.role")
		self.assertEqual(response["error_language"], "es")
		self.assertEqual(response["error_message"], "Los contactos son para un mayordomo, un gerente o la oficina.")

	def test_a_key_at_the_raise_site_wins(self):
		from erpnext_mcp.api import guard
		from erpnext_mcp.errors import ToolError

		translations.install_translations()
		self.request({}, headers={"Accept-Language": "es"}, remote_addr="100.64.0.7")
		frappe.local.session.user = WORKER
		guard._stamp_language(WORKER)
		guard._stamp_error(ToolError("tag X is already on Asset Register Y. Nothing was changed.", "error.asset.tag_in_use"),
		                   "error.unspecified")
		self.assertEqual(frappe.local.response["error_message"], "Esa etiqueta ya está en otro registro.")
