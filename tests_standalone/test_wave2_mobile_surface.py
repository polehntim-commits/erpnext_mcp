# SPDX-License-Identifier: MIT
"""Wave 2 — the six places the app had a screen and the server had no door.

Every one of these was found from the handset side and written up in
`fafo_ios/SERVER_CHANGES.md`. Wave 1 was seven disagreements about a NAME or a
VALUE; this wave is different in kind and the tests are shaped differently
because of it. Five of the six are TRANSPORT gaps: the tool existed, the rules
existed, the doctype existed, and nothing a phone could reach reached them. So
the tests here are written against the ROUTE and the ARGUMENT — what `bind`
would actually deliver out of the body the app posts — because a test that
called `farm.create_field` directly would have passed on every one of the days
that method was a 404 in an orchard.

SIX CLAIMS, in the order the wave was worked.

1. `TheTemplateCarriesItsProcedure` — items 2 and 3. `Farm Task Template` joins
   `ATTACHMENT_PARENTS` un-gated, the two SOP columns exist and are declared
   fields rather than a filename convention, and a task reaches them through the
   `template` link v0.96.0 added. THE NEGATIVE CONTROL IS THAT THEY ARE NOT
   SNAPSHOTTED: `task_templates.snapshot` must NOT carry them, because an SOP
   copied onto a task freezes last season's PDF onto work still on the board.

2. `TheNoteHasARouteNow` — item 12, first half. `add_task_note` is mounted under
   the name the app asks for, takes `note` and `language`, and turns into a
   voice note when an `audio_file_token` rides along. The older
   `add_task_note_via_mobile` keeps its route.

3. `AGrievanceIsNotAWarning` — item 12, second half. `create_dispute` files a
   Worker Report: no discipline type, no rung on the chain, and — the one that
   matters — not refused after a termination. `direction` is accepted as the
   handset's spelling of `report_direction` and `discipline_type` is absent from
   the signature, so `bind` cannot deliver one.

4. `TheTaskCarriesItsFacts` — item 5. The taxonomy is read off this site's own
   meta instead of transcribed into Swift, and the four structured fields land
   in columns. THE NEGATIVE CONTROL IS THE RATE LIMIT: `observed_at` must not be
   `reported_at`, because that column is what the five-per-hour anti-spam rule
   counts on, and a caller who could move it could file a hundred backdated
   reports.

5. `TheCrewCountsDownTogether` — item 14. `get_break_schedule` returns instants
   rather than durations, places them where the regulation says, and reads the
   policy off the SHIFT before the state. It also pins the bug this uncovered:
   `shifts.FIELDS` never carried `break_policy`, so three shipped readers of it
   got None on every shift ever written.

6. `ThePickerHasSomethingToShow` — item 11. Four registers in one call, five
   doors onto one write, and the gate that is narrower than dispatch. The
   negative control is the ABSENT arguments: `title_holder` and
   `appraised_value` are on the tool and not on the wrapper, so a handset cannot
   put a number on a piece of ground that reaches a financial statement.
"""

import inspect
from typing import ClassVar

import frappe

from erpnext_mcp import breaks as breaks_mod
from erpnext_mcp import locations, roles, shifts, task_templates
from erpnext_mcp.api import guard, shape
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.farmops_api import routes as farmops_routes
from erpnext_mcp.tools import discipline as discipline_tools
from erpnext_mcp.tools import shifts as shift_tools

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import ROLES, STORE, set_roles

WORKER = "ana@example.test"
WORKER_EMPLOYEE = "EMP-ANA"
MANAGER = "mira@example.test"
MANAGER_EMPLOYEE = "EMP-MIRA"

ON = {
	f"allow_{name}": 1
	for name in (
		"create_mobile_user",
		"get_current_user_context",
		"create_parcel",
		"create_field",
		"create_irrigation_zone",
		"create_housing_unit",
		"create_farm_task",
		"create_farm_task_template",
		"start_shift",
		"add_worker_to_shift",
		"end_shift",
		"log_shift_break",
		"get_break_policy",
		"get_break_schedule",
		"get_shift",
		"create_incident_record",
		"get_incident_record",
		"add_task_note",
		"list_task_notes",
	)
}


class Wave2TestCase(V12TestCase):
	"""A site with a picker, a manager, and a way to call the API as either."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		guard._BUCKETS.clear()
		roles.install_roles()
		STORE.seed(
			"Employee",
			[
				{
					"name": WORKER_EMPLOYEE,
					"employee_name": "Ana Ramos",
					"user_id": WORKER,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": MANAGER_EMPLOYEE,
					"employee_name": "Mira Vance",
					"user_id": MANAGER,
					"company": MAIN,
					"status": "Active",
				},
			],
		)
		self.enrol(WORKER, "Ana Ramos", "Field Worker")
		self.enrol(MANAGER, "Mira Vance", "Farm Manager")

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	def enrol(self, email, name, role, entities=None):
		return self.tool_data(
			"create_mobile_user",
			{
				"email": email,
				"full_name": name,
				"role": role,
				"entity_access": entities or [MAIN],
			},
		)

	def be(self, user=WORKER, remote_addr="100.64.0.7"):
		"""Become one user, on a request that looks like a phone's."""
		self.request({}, headers={}, remote_addr=remote_addr)
		frappe.local.session.user = user
		return user

	# ── furniture ───────────────────────────────────────────────────────────
	def a_parcel(self, parcel_name="Mill Creek", acreage=131.43):
		"""One parcel, created once. Returns the DOCNAME, which is not the name.

		`Parcel` autonames as `<parcel_name> - <company abbr>`, so a test that
		passed "Mill Creek" back into a `parcel` argument would be relying on the
		tool's own name-or-docname resolution rather than on the pair the picker
		actually sends. Everything here uses the docname.
		"""
		existing = [row["name"] for row in STORE.rows("Parcel") if row.get("parcel_name") == parcel_name]
		if existing:
			return existing[0]
		return self.tool_data(
			"create_parcel",
			{"owning_entity": MAIN, "parcel_name": parcel_name, "acreage": acreage, "county": "Wasco"},
		)["name"]

	def a_block(self, field_name="Ridge Top", **overrides):
		existing = [row["name"] for row in STORE.rows("Field") if row.get("field_name") == field_name]
		if existing:
			return existing[0]
		payload = {"parcel": self.a_parcel(), "field_name": field_name, "acreage": 12.5, "crop": "Cherry"}
		payload.update(overrides)
		return self.tool_data("create_field", payload)["name"]

	def a_template(self, **overrides):
		payload = {
			"template_name": "Cabin Habitability Inspection",
			"task_type": "Inspection",
			"skill_required": "camp_maintenance",
			"evidence_required": {"photos": True, "findings_text": True},
			"company": MAIN,
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task_template", payload)["name"]

	def a_policy(self, work_state="OR", **overrides):
		row = {
			"name": "LBP-OR-2026",
			"policy_id": "LBP-OR-2026",
			"work_state": work_state,
			"enabled": 1,
			"effective_from": "2026-01-01",
			"human_approved_by": "ada@example.test",
			"regulation_citations": "OAR 839-020-0050",
			"rest_schedule": [
				{"hours_from": 6, "hours_to": 10, "periods_owed": 2, "minutes_each": 10, "paid": 1}
			],
			"meal_schedule": [
				{"hours_from": 6, "hours_to": 12, "periods_owed": 1, "minutes_each": 30, "paid": 0}
			],
			"heat_schedule": [
				{
					"heat_index_from": 90,
					"heat_index_to": 200,
					"minutes_each": 10,
					"every_hours": 2,
					"concurrent_with_rest": 0,
				}
			],
		}
		row.update(overrides)
		STORE.seed("Labor Break Policy", [row])
		return row["name"]

	def a_shift(self, **overrides):
		payload = {
			"foreman": MANAGER_EMPLOYEE,
			"location": "Block 7 North",
			"shift_type": "Harvest",
			"start_datetime": f"{frappe.utils.today()} 06:00:00",
			"crew_employees": [WORKER_EMPLOYEE],
		}
		payload.update(overrides)
		return self.tool_data("start_shift", payload)["name"]

	def accepts(self, handler):
		"""What `routes.bind` would actually deliver to this method."""
		return farmops_routes.accepted_arguments(handler)


# ── 1. items 2 and 3: the SOP a standing job carries ────────────────────────
class TheTemplateCarriesItsProcedure(Wave2TestCase):
	"""A picker at a cabin door can open the procedure for the job they are doing.

	Two failures, one screen. `list_attachments` refused `Farm Task Template` by
	name — the allow-list is closed on purpose and the template was not on it —
	so `TaskSOPSection` could not read the folder at all. And the language of
	whatever it did find was read off the FILENAME (`..._es.pdf`,
	`..._spanish.pdf`), which works while the office follows a convention and
	fails silently when they do not: a document the app cannot place is offered
	with no language badge, so a Spanish-speaking picker gets an unlabelled
	button and has to open it to find out.
	"""

	def test_the_template_is_a_parent_this_surface_reads_attachments_from(self):
		self.assertIn(mobile_api.FARM_TASK_TEMPLATE, mobile_api.ATTACHMENT_PARENTS)

	def test_reading_a_templates_folder_does_not_take_the_hr_role(self):
		"""`False` is the whole point. A template is not a fact about a person.

		Every other entry on that list is a record of something that happened to
		somebody — a shift, an inspection, a warning, a payroll run — and two of
		them carry the HR gate because they are identity documents. This one is
		what the farm does when it cleans a cabin, and the picker standing in
		front of the cabin is who it was written for.
		"""
		self.assertIs(mobile_api.ATTACHMENT_PARENTS[mobile_api.FARM_TASK_TEMPLATE], False)

	def test_both_sop_columns_exist_on_the_doctype(self):
		self.assertTrue(frappe.get_meta("Farm Task Template").get_field("sop_document_en"))
		self.assertTrue(frappe.get_meta("Farm Task Template").get_field("sop_document_es"))

	def test_the_register_reports_the_two_documents_and_which_languages_are_on_file(self):
		name = self.a_template()
		frappe.db.set_value("Farm Task Template", name, "sop_document_es", "/private/files/cabina.pdf")
		described = task_templates.describe(name)
		self.assertIsNone(described["sop_document_en"])
		self.assertEqual(described["sop_document_es"], "/private/files/cabina.pdf")
		self.assertEqual(described["sop_documents"], {"en": None, "es": "/private/files/cabina.pdf"})
		# The gap is reported as a gap. A template with only the Spanish
		# procedure says so, which is something somebody can close.
		self.assertEqual(described["sop_languages"], ["es"])

	def test_the_language_is_a_column_and_never_read_off_the_filename(self):
		"""The negative control for the convention this replaces.

		A document filed in the ENGLISH column keeps its language whatever it is
		called — including when it is called `procedimiento_es.pdf`, which is
		exactly the filename a convention-reader would have mislabelled.
		"""
		name = self.a_template()
		frappe.db.set_value(
			"Farm Task Template", name, "sop_document_en", "/private/files/procedimiento_es.pdf"
		)
		described = task_templates.describe(name)
		self.assertEqual(described["sop_document_en"], "/private/files/procedimiento_es.pdf")
		self.assertIsNone(described["sop_document_es"])
		self.assertEqual(described["sop_languages"], ["en"])

	def test_a_task_reaches_the_procedure_through_the_template_link(self):
		"""Item 3 riding on item 7. The link was the whole delivery mechanism."""
		name = self.a_template()
		frappe.db.set_value("Farm Task Template", name, "sop_document_en", "/private/files/cabin.pdf")
		out = shape.task({"name": "TASK-1", "state": "Available", "template": name}, None)
		self.assertEqual(out["template"], name)
		self.assertEqual(out["sop_document_en"], "/private/files/cabin.pdf")

	def test_a_task_with_no_template_grows_no_sop_keys_at_all(self):
		out = shape.task({"name": "TASK-2", "state": "Available"}, None)
		self.assertNotIn("template", out)
		self.assertNotIn("sop_document_en", out)
		self.assertNotIn("sop_document_es", out)

	def test_a_template_with_no_procedure_filed_grows_no_sop_keys_either(self):
		name = self.a_template()
		out = shape.task({"name": "TASK-3", "state": "Available", "template": name}, None)
		self.assertEqual(out["template"], name)
		self.assertNotIn("sop_document_en", out)

	def test_the_procedure_is_deliberately_not_snapshotted_onto_the_task(self):
		"""THE NEGATIVE CONTROL, and the reason this is not one line in `snapshot`.

		Everything a task copies off a template is FROZEN at creation — that is
		the load-bearing rule of `task_templates`, and it is right for a
		checklist and an evidence contract, because nobody's obligations should
		tighten under them mid-job. It is exactly wrong for a procedure: an SOP
		corrected at the office has to reach the work already on the board, and a
		copied one would leave last season's PDF attached to every open task.
		Reading it through the link is what makes that true, and a future edit
		that "tidies" these into the snapshot fails here.
		"""
		name = self.a_template()
		frappe.db.set_value("Farm Task Template", name, "sop_document_en", "/private/files/cabin.pdf")
		snapshot = task_templates.snapshot(name)
		self.assertNotIn("sop_document_en", snapshot)
		self.assertNotIn("sop_document_es", snapshot)
		self.assertNotIn("sop_documents", snapshot)
		for field, _ in task_templates.SOP_DOCUMENT_FIELDS:
			self.assertNotIn(field, task_templates.TEMPLATE_FIELDS)


# ── 2. item 12, first half: the note that had no route ──────────────────────
class TheNoteHasARouteNow(Wave2TestCase):
	"""Every field note a foreman typed is still on one phone, and this is why.

	`add_task_note_via_mobile` has been mounted since v0.79.0, and `Route` builds
	every path from the wrapper's OWN NAME — so the published path carried
	`_via_mobile` on it. The handset asked for `add_task_note`, which is the MCP
	tool's name and what any reader would predict, and got a 404; then
	`add_narrative`, `append_note`, `create_task_note`, `add_note`,
	`create_narrative_note` and `log_task_note`, and got six more.
	`list_task_notes` IS published plainly, so a record's narrative could be read
	and never written.
	"""

	def test_the_route_exists_under_the_name_the_app_asks_for(self):
		self.assertIn("/mobile/add_task_note", {route.path for route in farmops_routes.ROUTES})

	def test_the_older_spelling_keeps_its_route(self):
		"""A handset already in an orchard is not asked to change to get an answer."""
		self.assertIn("/mobile/add_task_note_via_mobile", {route.path for route in farmops_routes.ROUTES})

	def test_the_route_declares_the_four_arguments_the_app_sends(self):
		"""`bind` delivers only what a signature names — see item 12's own ask.

		`add_task_note(doctype, name, note, language, audio_file_token)` is the
		call `TaskNotesAPI` makes. A method declaring `narrative` at a caller
		sending `note` is not a rename that half works: it is an empty note,
		written and stored, with nothing in the answer saying the words went
		missing.
		"""
		accepted = self.accepts(mobile_api.add_task_note)
		for argument in ("doctype", "name", "note", "language", "audio_file_token"):
			self.assertIn(argument, accepted)

	def test_both_vocabularies_reach_the_same_write(self):
		self.be(WORKER)
		task = self.a_task()
		typed = mobile_api.add_task_note(doctype="Farm Task", name=task, note="Line is split at the riser.")
		self.assertEqual(typed["narrative"], "Line is split at the riser.")
		older = mobile_api.add_task_note(doctype="Farm Task", name=task, narrative="Second look, same split.")
		self.assertEqual(older["narrative"], "Second look, same split.")

	def test_a_file_token_turns_the_same_call_into_a_voice_note(self):
		"""One register and one set of rules, whether it was typed or dictated."""
		self.be(WORKER)
		task = self.a_task()
		STORE.seed("File", [{"name": "FILE-AUDIO-1", "file_name": "note.m4a", "is_private": 1}])
		data = mobile_api.add_task_note(
			doctype="Farm Task",
			name=task,
			note="Told me the riser has been leaking a week.",
			audio_file_token="FILE-AUDIO-1",
			audio_duration_seconds=31,
		)
		self.assertEqual(data["audio_file"], "FILE-AUDIO-1")
		self.assertEqual(data["narrative"], "Told me the riser has been leaking a week.")

	def test_the_note_is_tagged_with_the_language_the_app_names(self):
		"""`language` is the handset's spelling of `source_language`.

		On a bilingual crew an entry tagged with the wrong language is a
		translation nobody knows is needed, which is why this is an argument
		rather than a guess.
		"""
		self.be(WORKER)
		task = self.a_task()
		data = mobile_api.add_task_note(
			doctype="Farm Task", name=task, note="Se rompió el elevador.", language="es"
		)
		self.assertEqual(data["source_language"], "es")

	def test_an_untagged_note_stays_untagged_rather_than_defaulting_to_english(self):
		"""`_caller_language` returns "" where nothing says, and it is left empty.

		A phone set to English by whoever handed it over says nothing about who
		is holding it now — so the entry says "nobody told me" rather than
		"English", which is the one answer that would be silently wrong.
		"""
		self.be(WORKER)
		task = self.a_task()
		data = mobile_api.add_task_note(doctype="Farm Task", name=task, note="Riser is split.")
		self.assertFalse(data["source_language"])

	def test_a_register_that_carries_no_narrative_is_refused_by_name(self):
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.add_task_note(doctype="Housing Unit", name="MC-Cabin-01", note="x")
		self.assertIn("does not carry a narrative", str(caught.exception))

	def a_task(self):
		frappe.local.session.user = MANAGER
		name = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Repair riser — Ridge Top",
				"task_type": "Repair",
				"evidence_required": {"photos": True},
				"company": MAIN,
			},
		)["name"]
		frappe.local.session.user = WORKER
		return name


# ── 3. item 12, second half: a grievance is not a warning ───────────────────
class AGrievanceIsNotAWarning(Wave2TestCase):
	"""A worker's complaint was being filed as a step on their own discipline chain.

	`Farm Incident Record` was built to carry both directions and v0.94.0 added
	`report_direction` with four behaviours hanging off it. None of that was
	reachable in the app's vocabulary, so the handset filed a grievance through
	`create_discipline_record` at the lowest step with `DISPUTE RAISED BY …`
	typed into the description. `SERVER_CHANGES.md` says in as many words that it
	works and is not right — and what it produces is a WARNING, escalating from
	nothing and escalated from by whatever comes next.
	"""

	def test_a_dispute_is_stored_as_a_worker_report(self):
		self.be(MANAGER)
		data = mobile_api.create_dispute(
			employee=WORKER_EMPLOYEE,
			description="Was not given the second rest period on Tuesday.",
			company=MAIN,
		)
		row = STORE.get_raw("Farm Incident Record", data["name"])
		self.assertEqual(row["report_direction"], discipline_tools.WORKER_REPORT)

	def test_it_carries_no_discipline_type_and_no_rung_on_the_chain(self):
		"""The three things that make it not a warning, asserted on the row."""
		self.be(MANAGER)
		data = mobile_api.create_dispute(
			employee=WORKER_EMPLOYEE, description="Second rest period missed.", company=MAIN
		)
		row = STORE.get_raw("Farm Incident Record", data["name"])
		self.assertFalse(row.get("discipline_type"))
		self.assertFalse(row.get("prior_record"))
		self.assertFalse(row.get("step_number"))

	def test_discipline_type_is_not_on_the_signature_so_bind_cannot_deliver_one(self):
		"""The argument filter is the first lock; the tool's refusal is the second.

		A complaint that could be given a warning level by the body that raises
		it is the bug restated with an extra field.
		"""
		self.assertNotIn("discipline_type", self.accepts(mobile_api.create_dispute))

	def test_a_dispute_is_not_refused_after_a_termination(self):
		"""THE ONE THAT MATTERS, and the reason this cannot be a discipline record.

		The chain rule refuses a step after the end of employment, correctly: a
		warning issued to somebody who no longer works here is either about a
		rehire or a mistake. A worker DISPUTING THE TERMINATION ITSELF is
		precisely the report that must not come back "there is no step after the
		end of employment", and under the old spelling that is exactly what it
		came back as.
		"""
		self.be(MANAGER)
		self.tool_data(
			"create_incident_record",
			{
				"employee": WORKER_EMPLOYEE,
				"discipline_type": "Termination",
				"incident_description": "Final incident.",
				"expected_improvement": "n/a",
				"followup_date": str(frappe.utils.add_days(frappe.utils.today(), 14)),
				"company": MAIN,
			},
		)
		self.be(MANAGER)
		data = mobile_api.create_dispute(
			employee=WORKER_EMPLOYEE,
			description="I dispute the termination — I was not told about the meeting.",
			company=MAIN,
		)
		self.assertTrue(data["name"])

	def test_the_negative_control_a_supervisor_report_after_a_termination_is_still_refused(self):
		"""The chain rule is narrowed by direction, not deleted."""
		self.be(MANAGER)
		self.tool_data(
			"create_incident_record",
			{
				"employee": WORKER_EMPLOYEE,
				"discipline_type": "Termination",
				"incident_description": "Final incident.",
				"expected_improvement": "n/a",
				"followup_date": str(frappe.utils.add_days(frappe.utils.today(), 14)),
				"company": MAIN,
			},
		)
		message = self.tool_error(
			"create_incident_record",
			{
				"employee": WORKER_EMPLOYEE,
				"discipline_type": "Verbal Warning",
				"incident_description": "Another one.",
				"expected_improvement": "x",
				"followup_date": str(frappe.utils.add_days(frappe.utils.today(), 14)),
				"company": MAIN,
			},
		)
		self.assertIn("no step after the end of employment", message)

	def test_the_handsets_two_words_map_onto_the_column_and_nothing_else_does(self):
		self.assertEqual(mobile_api.REPORT_DIRECTIONS["grievance"], discipline_tools.WORKER_REPORT)
		self.assertEqual(mobile_api.REPORT_DIRECTIONS["disciplinary"], discipline_tools.SUPERVISOR_REPORT)
		self.assertEqual(mobile_api._report_direction("Worker Report"), discipline_tools.WORKER_REPORT)
		self.assertEqual(mobile_api._report_direction("GRIEVANCE"), discipline_tools.WORKER_REPORT)
		self.assertEqual(mobile_api._report_direction(""), "")

	def test_an_unknown_direction_is_refused_with_both_vocabularies_in_the_sentence(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api._report_direction("sideways")
		message = str(caught.exception)
		self.assertIn("sideways", message)
		self.assertIn("Worker Report", message)
		self.assertIn("grievance", message)

	def test_the_discipline_route_takes_direction_as_a_spelling_of_report_direction(self):
		self.assertIn("direction", self.accepts(mobile_api.create_discipline_record))
		self.be(MANAGER)
		data = mobile_api.create_discipline_record(
			employee=WORKER_EMPLOYEE,
			direction="grievance",
			incident_description="Raised through the older route.",
			company=MAIN,
		)
		row = STORE.get_raw("Farm Incident Record", data["name"])
		self.assertEqual(row["report_direction"], discipline_tools.WORKER_REPORT)

	def test_two_vocabularies_saying_the_same_thing_are_not_a_contradiction(self):
		"""Normalised before they are compared, which `_one_spelling` alone cannot do.

		`report_direction="Worker Report"` and `direction="grievance"` are one
		instruction in two vocabularies. Comparing the raw strings would have
		refused it as a body that says both, differently.
		"""
		self.be(MANAGER)
		data = mobile_api.create_discipline_record(
			employee=WORKER_EMPLOYEE,
			report_direction="Worker Report",
			direction="grievance",
			incident_description="Both spellings, one meaning.",
			company=MAIN,
		)
		row = STORE.get_raw("Farm Incident Record", data["name"])
		self.assertEqual(row["report_direction"], discipline_tools.WORKER_REPORT)

	def test_two_directions_that_really_do_differ_are_still_refused(self):
		"""The negative control for the normalisation above."""
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.create_discipline_record(
				employee=WORKER_EMPLOYEE,
				report_direction="Supervisor Report",
				direction="grievance",
				incident_description="Two different instructions.",
				company=MAIN,
			)
		self.assertIn("two spellings of one argument", str(caught.exception))


# ── 4. item 5: the facts a task was carrying as prose ───────────────────────
class TheTaskCarriesItsFacts(Wave2TestCase):
	"""A task type list transcribed into Swift, and four facts stored as a blob.

	`FarmTaskType.all` is a hand-copy of the eleven options the Select shipped
	with, so a site that CUSTOMISES it — a supported thing needing no release on
	this side — gets a picker missing its own options until the app ships again.
	And everything below the first line of `description` was prose the app
	composed and the server kept whole, so the affected asset, the affected
	block, when it was seen and who saw it were greppable and not queryable.
	"""

	def test_the_taxonomy_is_read_off_this_sites_meta_rather_than_a_constant(self):
		published = shape.taxonomy()
		self.assertIn("Inspection", published["task_types"])
		self.assertIn("Spray", published["task_types"])
		self.assertEqual(
			published["task_types"],
			[
				line.strip()
				for line in frappe.get_meta("Farm Task").get_field("task_type").options.split("\n")
				if line.strip()
			],
		)

	def test_a_site_that_customises_the_select_gets_its_own_options(self):
		"""The whole reason this is published rather than transcribed.

		A constant on this side would be the same copy `FarmTaskType.all`
		already is, one layer further from the client and just as stale.
		"""
		field = frappe.get_meta("Farm Task").get_field("task_type")
		before = field.options
		self.addCleanup(setattr, field, "options", before)
		field.options = before + "\nCider-Press"
		self.assertIn("Cider-Press", shape.taxonomy()["task_types"])

	def test_the_login_call_carries_it_so_no_extra_request_is_spent(self):
		self.be(WORKER)
		context = mobile_api.get_current_user_context()
		self.assertIn("Inspection", context["taxonomy"]["task_types"])
		self.assertIn("Critical", context["taxonomy"]["task_urgencies"])
		self.assertIn("Water Break", context["taxonomy"]["break_kinds"])

	def test_the_four_location_registers_are_published_too(self):
		"""`TaskLocationRegister` transcribed these out of a refusal sentence."""
		self.be(WORKER)
		published = mobile_api.get_current_user_context()["taxonomy"]["location_registers"]
		self.assertEqual(published, list(locations.REGISTERS))

	def test_the_structured_arguments_are_declared_so_bind_delivers_them(self):
		created = self.accepts(mobile_api.create_farm_task)
		for argument in ("affected_asset", "affected_block", "observed_at", "reported_by"):
			self.assertIn(argument, created)
		reported = self.accepts(mobile_api.report_field_task)
		for argument in ("affected_asset", "affected_block", "observed_at", "estimated_duration_minutes"):
			self.assertIn(argument, reported)

	def test_reported_by_is_not_on_the_field_report_signature(self):
		"""The reporter is the authenticated worker, and only there.

		`create_farm_task` accepts one because a foreman is recording somebody
		else's observation. Here the reporter IS the caller, and a body that
		could name anybody else would put a stranger's name on a report they
		never made.
		"""
		self.assertNotIn("reported_by", self.accepts(mobile_api.report_field_task))

	def test_the_affected_block_lands_in_the_location_pair(self):
		block = self.a_block()
		self.be(MANAGER)
		data = mobile_api.create_farm_task(
			task_name="Split riser",
			task_type="Repair",
			evidence_required={"photos": True},
			affected_block=block,
			company=MAIN,
		)
		self.assertEqual(data["location_doctype"], "Field")
		self.assertEqual(data["location"], block)

	def test_a_block_that_is_not_a_field_is_refused_by_name(self):
		self.be(MANAGER)
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Split riser",
				"task_type": "Repair",
				"evidence_required": {"photos": True},
				"affected_block": "Block 7 North",
				"company": MAIN,
			},
		)
		self.assertIn("no Field called", message)

	def test_a_block_and_a_location_that_disagree_are_refused_rather_than_ranked(self):
		"""Two answers to where the work is. A crew sent to the wrong ground is
		a worse outcome than a refusal somebody can read."""
		block = self.a_block()
		other = self.a_block(field_name="Lower Bench")
		self.be(MANAGER)
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Split riser",
				"task_type": "Repair",
				"evidence_required": {"photos": True},
				"affected_block": block,
				"location_doctype": "Field",
				"location": other,
				"company": MAIN,
			},
		)
		self.assertIn("two answers to where this work is", message)

	def test_observed_at_lands_in_its_own_column_and_survives_an_iso_stamp(self):
		"""An iPhone writes `2026-08-18T07:12:00Z`, which a Datetime column refuses.

		The same wall the whole bucket capture queue hit in v0.59.1, and the
		reason `datetimes.as_mariadb_datetime` is its own module.
		"""
		self.be(MANAGER)
		data = mobile_api.create_farm_task(
			task_name="Split riser",
			task_type="Repair",
			evidence_required={"photos": True},
			observed_at="2026-08-18T07:12:00Z",
			company=MAIN,
		)
		row = STORE.get_raw("Farm Task", data["name"])
		self.assertEqual(str(row["observed_at"]), "2026-08-18 07:12:00")
		self.assertEqual(data["observed_at"], "2026-08-18 07:12:00")

	def test_an_unreadable_observed_at_is_refused_before_anything_is_written(self):
		before = len(STORE.rows("Farm Task"))
		self.be(MANAGER)
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Split riser",
				"task_type": "Repair",
				"evidence_required": {"photos": True},
				"observed_at": "yesterday morning",
				"company": MAIN,
			},
		)
		self.assertIn("not a timestamp", message)
		self.assertEqual(len(STORE.rows("Farm Task")), before)

	def test_the_negative_control_observed_at_is_not_reported_at(self):
		"""THE REASON THIS NEEDED A NEW COLUMN INSTEAD OF REUSING ONE.

		`reported_at` is the filing stamp, and `dispatch._field_report_count`
		counts the five-per-hour anti-spam limit on it. A caller who could set it
		backwards could file a hundred reports dated an hour ago and the rule
		would count none of them. So `observed_at` is settable and `reported_at`
		is never — and a future edit that folds the two together fails here.
		"""
		self.assertIn(
			'"reported_at": (">=", cutoff)',
			inspect.getsource(__import__("erpnext_mcp.tools.dispatch", fromlist=["x"])._field_report_count),
		)
		source = inspect.getsource(
			__import__("erpnext_mcp.tools.dispatch", fromlist=["x"])._structured_report
		)
		self.assertIn("doc.observed_at", source)
		self.assertNotIn("doc.reported_at", source)
		self.assertNotIn("reported_at", self.accepts(mobile_api.report_field_task))

	def test_a_field_report_can_finally_carry_an_estimate(self):
		"""An ad-hoc task with no duration sorts last on a board that orders by cost."""
		STORE.seed("File", [{"name": "FILE-PHOTO-1", "file_name": "riser.jpg", "is_private": 1}])
		self.be(WORKER)
		data = mobile_api.report_field_task(
			description="Riser split at the head of the row.",
			photo_file_token="FILE-PHOTO-1",
			estimated_duration_minutes=45,
		)
		self.assertEqual(STORE.get_raw("Farm Task", data["name"])["estimated_duration_minutes"], 45)

	def test_a_field_report_still_stamps_its_reporter_from_the_session(self):
		STORE.seed("File", [{"name": "FILE-PHOTO-2", "file_name": "riser.jpg", "is_private": 1}])
		self.be(WORKER)
		data = mobile_api.report_field_task(description="Riser split.", photo_file_token="FILE-PHOTO-2")
		self.assertEqual(STORE.get_raw("Farm Task", data["name"])["reported_by"], WORKER_EMPLOYEE)


# ── 5. item 14: the countdown every phone reads the same ────────────────────
class TheCrewCountsDownTogether(Wave2TestCase):
	"""Seven phones in an orchard, each computing its own break times.

	`BreakSchedule` counts down from the shift's start against the farm's policy
	when `get_break_policy` answers and against the state statutory minimum when
	it does not — which is honest, prints which of the two it used, and is not
	SYNCHRONISED. Each handset works out its own instants from its own idea of
	when the shift began, and they disagree by whatever the clocks and the last
	sync disagree by. `SERVER_CHANGES.md` §14 states the requirement as every
	phone counting down to the same second.
	"""

	POLICY: ClassVar[dict] = {
		"policy": "LBP-OR-2026",
		"rest_schedule": [
			{"hours_from": 6, "hours_to": 10, "periods_owed": 2, "minutes_each": 10, "paid": 1}
		],
		"meal_schedule": [
			{"hours_from": 6, "hours_to": 12, "periods_owed": 1, "minutes_each": 30, "paid": 0}
		],
	}

	def test_the_breaks_fall_in_the_middle_of_each_work_period(self):
		"""OAR 839-020-0050(1): "in the middle of each work period of four hours".

		Two rest periods over an eight-hour day starting at six therefore fall at
		eight and at twelve — the middle of the first four hours and the middle
		of the second — with the meal at ten. That is also where an orchard has
		always taken them, which is the check that the arithmetic is describing
		the real day rather than dividing a number.
		"""
		rows = breaks_mod.schedule("2026-08-18 06:00:00", self.POLICY, hours=8.0)
		self.assertEqual(
			[(row["break_type"], row["due_at"]) for row in rows],
			[
				("Paid Rest", "2026-08-18 08:00:00"),
				("Unpaid Meal", "2026-08-18 10:00:00"),
				("Paid Rest", "2026-08-18 12:00:00"),
			],
		)

	def test_it_answers_with_instants_so_a_late_reader_sees_the_same_clock(self):
		"""The whole point. A duration would have to be re-based on a device clock."""
		early = breaks_mod.schedule("2026-08-18 06:00:00", self.POLICY, hours=8.0, now="2026-08-18 06:01:00")
		late = breaks_mod.schedule("2026-08-18 06:00:00", self.POLICY, hours=8.0, now="2026-08-18 09:30:00")
		self.assertEqual([row["due_at"] for row in early], [row["due_at"] for row in late])
		# Only the advisory half moves.
		self.assertEqual(early[0]["status"], "upcoming")
		self.assertEqual(late[0]["status"], "overdue")

	def test_each_row_carries_its_duration_its_pay_status_and_where_it_came_from(self):
		rows = breaks_mod.schedule("2026-08-18 06:00:00", self.POLICY, hours=8.0)
		rest = rows[0]
		self.assertEqual(rest["duration_minutes"], 10)
		self.assertIs(rest["is_paid"], True)
		self.assertEqual(rest["policy_source"], "LBP-OR-2026")
		meal = rows[1]
		self.assertEqual(meal["duration_minutes"], 30)
		self.assertIs(meal["is_paid"], False)

	def test_the_break_type_is_the_string_log_shift_break_takes_back(self):
		"""A countdown ends with a foreman tapping the button, and this is what
		that button posts. A fifth vocabulary would have made the handset carry
		a translation table between the two."""
		rows = breaks_mod.schedule("2026-08-18 06:00:00", self.POLICY, hours=8.0)
		for row in rows:
			self.assertIn(row["break_type"], shift_tools.BREAK_KINDS)

	def test_a_break_already_taken_does_not_go_red_behind_the_crew(self):
		rows = breaks_mod.schedule(
			"2026-08-18 06:00:00",
			self.POLICY,
			hours=8.0,
			events=[{"break_kind": "Paid Rest", "event_datetime": "2026-08-18 08:02:00"}],
			now="2026-08-18 13:00:00",
		)
		rest = [row for row in rows if row["break_type"] == "Paid Rest"]
		self.assertEqual(rest[0]["status"], "taken")
		self.assertEqual(rest[1]["status"], "overdue")

	def test_the_three_heat_kinds_are_one_counter_here_as_everywhere_else(self):
		"""A crew just out of the shade has had its relief for that hour whichever
		of the three words the foreman tapped. See `HEAT_RELIEF_KINDS`."""
		policy = dict(self.POLICY)
		policy["heat_schedule"] = [
			{"heat_index_from": 90, "heat_index_to": 200, "minutes_each": 10, "every_hours": 4}
		]
		rows = breaks_mod.schedule(
			"2026-08-18 06:00:00",
			policy,
			hours=8.0,
			heat_index=97.0,
			events=[{"break_kind": "Shade Break", "event_datetime": "2026-08-18 10:00:00"}],
		)
		heat = [row for row in rows if row["break_type"] == "Cool-Down"]
		self.assertEqual(len(heat), 2)
		self.assertTrue(heat[0]["taken"])
		self.assertFalse(heat[1]["taken"])

	def test_no_heat_index_means_no_cool_downs_invented(self):
		"""The negative control. A schedule that invented cool-downs for a 60°F
		morning would train a crew to ignore the ones that matter."""
		policy = dict(self.POLICY)
		policy["heat_schedule"] = [
			{"heat_index_from": 90, "heat_index_to": 200, "minutes_each": 10, "every_hours": 4}
		]
		rows = breaks_mod.schedule("2026-08-18 06:00:00", policy, hours=8.0)
		self.assertEqual([row for row in rows if row["break_type"] == "Cool-Down"], [])

	def test_a_shift_too_short_to_owe_anything_owes_nothing(self):
		self.assertEqual(breaks_mod.schedule("2026-08-18 06:00:00", self.POLICY, hours=3.0), [])

	def test_the_endpoint_reads_the_policy_off_the_shift_first(self):
		"""A policy amended in October must not change what August's crew was owed."""
		self.a_policy()
		self.be(MANAGER)
		shift = self.a_shift()
		frappe.db.set_value("Farm Shift", shift, "break_policy", "LBP-OR-2026")
		self.be(MANAGER)
		data = mobile_api.get_break_schedule(shift=shift)
		self.assertEqual(data["policy"], "LBP-OR-2026")
		self.assertEqual(data["policy_source"], "shift")
		self.assertIn("stamped on this shift", data["note"])
		self.assertEqual(len(data["breaks"]), 3)

	def test_a_shift_with_no_stamp_falls_back_to_the_state_and_says_so(self):
		"""Shifts started before v0.58.0 carry no stamp, and an app that cannot
		tell the two apart cannot print the sentence `BreakSchedule` prints."""
		self.a_policy()
		self.be(MANAGER)
		shift = self.a_shift()
		frappe.db.set_value("Farm Shift", shift, "break_policy", None)
		frappe.db.set_value("Farm Shift", shift, "work_state", "OR")
		self.be(MANAGER)
		data = mobile_api.get_break_schedule(shift=shift)
		self.assertEqual(data["policy"], "LBP-OR-2026")
		self.assertEqual(data["policy_source"], "work_state")
		self.assertIn("names no break policy", data["note"])

	def test_no_policy_anywhere_says_so_rather_than_scheduling_nothing_quietly(self):
		self.be(MANAGER)
		shift = self.a_shift()
		self.be(MANAGER)
		data = mobile_api.get_break_schedule(shift=shift)
		self.assertEqual(data["breaks"], [])
		self.assertIn("falls back to the statutory minimum", data["note"])

	def test_an_open_shift_is_planned_and_a_closed_one_is_measured(self):
		self.a_policy()
		self.be(MANAGER)
		shift = self.a_shift()
		frappe.db.set_value("Farm Shift", shift, "break_policy", "LBP-OR-2026")
		self.be(MANAGER)
		open_answer = mobile_api.get_break_schedule(shift=shift)
		self.assertTrue(open_answer["hours_are_planned"])
		self.assertEqual(open_answer["hours"], shift_tools.PLANNED_SHIFT_HOURS)

		frappe.db.set_value("Farm Shift", shift, "end_datetime", f"{frappe.utils.today()} 13:00:00")
		closed = mobile_api.get_break_schedule(shift=shift)
		self.assertFalse(closed["hours_are_planned"])
		self.assertEqual(closed["hours"], 7.0)

	def test_the_bands_are_keyed_on_the_shift_and_not_on_hours_elapsed(self):
		"""THE REASON `PLANNED_SHIFT_HOURS` EXISTS.

		Six hours owes a meal period and four does not, so computing against
		elapsed time would make the meal APPEAR on the schedule four hours in —
		three hours after the crew needed to know it was coming, and already
		overdue the moment it arrived.
		"""
		self.a_policy()
		self.be(MANAGER)
		shift = self.a_shift()
		frappe.db.set_value("Farm Shift", shift, "break_policy", "LBP-OR-2026")
		self.be(MANAGER)
		data = mobile_api.get_break_schedule(shift=shift)
		self.assertIn("Unpaid Meal", [row["break_type"] for row in data["breaks"]])

	def test_the_route_takes_farm_shift_as_well_as_shift(self):
		"""v0.96.0's item 1, applied before it could bite a second route."""
		accepted = self.accepts(mobile_api.get_break_schedule)
		self.assertIn("shift", accepted)
		self.assertIn("farm_shift", accepted)
		self.a_policy()
		self.be(MANAGER)
		shift = self.a_shift()
		frappe.db.set_value("Farm Shift", shift, "break_policy", "LBP-OR-2026")
		self.be(MANAGER)
		self.assertEqual(mobile_api.get_break_schedule(farm_shift=shift)["shift"], shift)

	def test_a_shift_in_an_entity_this_account_cannot_reach_reads_as_not_found(self):
		self.a_policy()
		self.be(MANAGER)
		shift = self.a_shift()
		frappe.db.set_value("Farm Shift", shift, "company", OTHER)
		self.be(MANAGER)
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_break_schedule(shift=shift)

	def test_the_shift_row_finally_carries_the_policy_it_names(self):
		"""THE BUG THIS ITEM UNCOVERED, AND IT WAS ALREADY SHIPPED.

		`shifts.FIELDS` did not list `break_policy`, and three readers built
		their row from that tuple and then did `row.get("break_policy")`:
		`_break_summary`, `_compute_shift_production` and
		`get_shift_crew_timeline`. So the lookup returned None on every shift
		ever written, the break reconciliation was skipped in silence, and
		`get_shift_crew_timeline` reported `"break_policy": null` for shifts that
		named one. Nothing failed — a block of entitlement figures was simply
		absent, which is the quietest way for a compliance number to go missing.
		"""
		self.assertIn("break_policy", shifts.FIELDS)
		self.assertIn("work_state", shifts.FIELDS)

	def test_the_negative_control_the_summary_really_does_read_that_key(self):
		"""Without this the line above is a tuple entry nobody proves is load-bearing."""
		source = inspect.getsource(shifts._break_summary)
		self.assertIn('shift_row.get("break_policy")', source)

	def test_the_break_summary_now_appears_on_a_shift_that_names_a_policy(self):
		self.a_policy()
		self.be(MANAGER)
		shift = self.a_shift()
		frappe.db.set_value("Farm Shift", shift, "break_policy", "LBP-OR-2026")
		self.be(MANAGER)
		self.assertIsNotNone(self.tool_data("get_shift", {"name": shift})["break_summary"])


# ── 6. item 11: the picker that showed only "No location" ───────────────────
class ThePickerHasSomethingToShow(Wave2TestCase):
	"""The largest gap on the document, and it degraded every other feature.

	Ten spellings were probed on 2026-08-18 and all ten 404'd. The two published
	reads the app scavenged — `list_available_housing` and
	`list_org_reference_data` — cover cabins and parcels only, which on a farm
	whose work happens in BLOCKS is close to nothing. So a shift's location ended
	up as the typed note "Test 1" that no task could ever be routed against.
	"""

	# ── the read ────────────────────────────────────────────────────────────
	def test_all_four_registers_come_back_in_one_call(self):
		self.a_block("Ridge Top")
		self.a_zone()
		self.a_cabin()
		self.be(WORKER)
		data = mobile_api.list_farm_locations()
		self.assertEqual(
			sorted({row["doctype"] for row in data["locations"]}),
			["Field", "Housing Unit", "Irrigation Zone", "Parcel"],
		)

	def test_every_row_carries_the_pair_that_makes_it_resolvable(self):
		"""`location` without `location_doctype` is the refusal all three
		task-raising tools open with, and the reason `TaskLocationOption` has a
		failable initialiser."""
		self.a_block("Ridge Top")
		self.be(WORKER)
		row = next(r for r in mobile_api.list_farm_locations()["locations"] if r["doctype"] == "Field")
		self.assertTrue(row["name"])
		self.assertEqual(row["location_type"], "Field")
		self.assertEqual(row["label"], "Ridge Top")
		self.assertEqual(row["parent_parcel"], self.a_parcel())
		self.assertEqual(row["acreage"], 12.5)

	def test_the_read_is_open_to_a_picker_and_not_only_to_a_foreman(self):
		"""`report_field_task` is open to every enrolled worker and TAKES a
		location. A picker gated on the dispatch role would leave the one call a
		field worker makes about a place unable to name one."""
		self.a_block("Ridge Top")
		self.be(WORKER)
		self.assertTrue(mobile_api.list_farm_locations()["locations"])

	def test_the_answer_says_whether_this_caller_may_add_one(self):
		"""The handset gates its button on its own role list and calls that a
		courtesy. This is the real answer, off the same function the write uses."""
		self.a_block("Ridge Top")
		self.be(WORKER)
		self.assertFalse(mobile_api.list_farm_locations()["can_create"])
		self.be(MANAGER)
		self.assertTrue(mobile_api.list_farm_locations()["can_create"])

	def test_an_empty_register_is_still_a_section_the_picker_draws(self):
		"""A farm with no zones registered should learn that zones exist, rather
		than see three sections and conclude the fourth does not."""
		self.a_block("Ridge Top")
		self.be(WORKER)
		data = mobile_api.list_farm_locations()
		self.assertEqual(data["registers"], list(locations.REGISTERS))
		self.assertEqual(data["by_register"]["Irrigation Zone"], 0)

	def test_one_register_can_be_asked_for_on_its_own(self):
		self.a_block("Ridge Top")
		self.a_cabin()
		self.be(WORKER)
		data = mobile_api.list_farm_locations(doctype="Housing Unit")
		self.assertEqual({row["doctype"] for row in data["locations"]}, {"Housing Unit"})

	def test_a_register_this_surface_does_not_serve_is_refused_by_name(self):
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_farm_locations(doctype="Journal Entry")
		self.assertIn("Journal Entry", str(caught.exception))
		self.assertIn("Field", str(caught.exception))

	def test_the_rows_are_ordered_by_register_then_by_label(self):
		"""Not alphabetical across the whole list, which would interleave a cabin
		between two blocks and make the four sections impossible to find."""
		self.a_block("Ridge Top")
		self.a_block("Lower Bench")
		self.be(WORKER)
		rows = mobile_api.list_farm_locations()["locations"]
		blocks = [row["label"] for row in rows if row["doctype"] == "Field"]
		self.assertEqual(blocks, ["Lower Bench", "Ridge Top"])
		self.assertEqual(rows[0]["doctype"], "Field")

	def test_another_entitys_ground_is_not_in_the_answer(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		for row in mobile_api.list_farm_locations()["locations"]:
			self.assertIn(row["company"], (None, MAIN))

	# ── the write ───────────────────────────────────────────────────────────
	def test_a_picker_may_not_add_a_place_and_is_told_what_to_do_instead(self):
		"""A refusal that does not name the alternative reads as the feature
		being broken. The answer is "the office adds it and you pick it from the
		list five minutes later"."""
		self.a_parcel()
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.create_field(name="Ridge Top", parcel=self.a_parcel())
		message = str(caught.exception)
		self.assertIn("Farm Manager", message)
		self.assertIn("list_farm_locations is open to everybody enrolled", message)
		self.assertIn("Nothing was created", message)

	def test_the_gate_is_narrower_than_dispatch_and_that_is_deliberate(self):
		"""A dispatched task is undone by rejecting it. A register entry is
		routed through by every task, spray record and acre of cost allocation
		this farm will ever file."""
		self.assertTrue(guard.LOCATION_ROLES < guard.DISPATCH_ROLES)
		self.assertEqual(guard.LOCATION_ROLES, {"Farm Manager"})

	def test_the_roles_the_app_guessed_are_not_roles_this_app_has(self):
		"""§11 asked for the real name and this is the answer. `Farm Owner` and
		`Property Owner` were transcribed as a guess; neither is among the seven
		`roles.ROLE_SPECS` creates."""
		defined = {spec.name for spec in roles.ROLE_SPECS}
		self.assertNotIn("Farm Owner", defined)
		self.assertNotIn("Property Owner", defined)
		self.assertIn("Farm Manager", defined)

	def test_a_foreman_holds_dispatch_and_still_may_not_add_a_place(self):
		"""The negative control for the subset above."""
		set_roles(WORKER, ["Foreman"])
		self.a_parcel()
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError):
			mobile_api.create_field(name="Ridge Top", parcel=self.a_parcel())

	def test_a_manager_adds_a_block_and_gets_back_the_pair_to_select_it_with(self):
		"""The screen that posted this is a picker, and the next thing it does is
		select what it just made."""
		self.a_parcel()
		self.be(MANAGER)
		data = mobile_api.create_field(name="Ridge Top", parcel=self.a_parcel(), acres=12.5)
		self.assertEqual(data["doctype"], "Field")
		self.assertEqual(data["location_type"], "Field")
		self.assertEqual(data["location"], data["name"])
		self.assertEqual(data["option"]["label"], "Ridge Top")
		self.assertEqual(data["acreage"], 12.5)

	def test_the_polymorphic_door_and_the_named_one_write_the_same_record(self):
		"""Five doors, one write — so a rename cannot open a fifth way in."""
		parcel = self.a_parcel()
		self.be(MANAGER)
		named = mobile_api.create_field(name="Ridge Top", parcel=parcel, acres=4.0)
		poly = mobile_api.create_farm_location(name="Lower Bench", doctype="Field", parcel=parcel, acres=4.0)
		self.assertEqual(named["doctype"], poly["doctype"])
		self.assertEqual(
			STORE.get_raw("Field", named["name"])["parcel"],
			STORE.get_raw("Field", poly["name"])["parcel"],
		)

	def test_the_register_is_matched_case_insensitively_and_stored_exactly(self):
		self.a_parcel()
		self.be(MANAGER)
		data = mobile_api.create_farm_location(name="Ridge Top", doctype="field", parcel=self.a_parcel())
		self.assertEqual(data["doctype"], "Field")

	def test_a_parcel_needs_no_parent_because_it_is_the_top_of_the_tree(self):
		self.be(MANAGER)
		data = mobile_api.create_parcel(name="Cherry Bench", company=MAIN, acres=40.0)
		self.assertEqual(data["doctype"], "Parcel")
		self.assertIsNone(data["option"]["parent_parcel"])

	def test_a_zone_hangs_off_a_block_and_a_parcel_gets_the_sentence_explaining_it(self):
		"""The one place the four registers diverge in a way the handset's uniform
		sheet cannot see. Named rather than guessed at: a parcel usually holds
		several blocks, and picking one would put a zone on ground it does not
		water."""
		self.a_block("Ridge Top")
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.create_irrigation_zone(name="Zone 2", parcel=self.a_parcel())
		message = str(caught.exception)
		self.assertIn("hangs off a Field", message)
		self.assertIn("Nothing was created", message)

	def test_a_zones_acres_become_square_feet_because_the_tool_refuses_acres(self):
		"""`area_acres` is COMPUTED from `area_sq_ft` by the controller, and two
		independently settable figures are two figures that will disagree. So
		this converts rather than setting a second one."""
		block = self.a_block("Ridge Top")
		self.be(MANAGER)
		data = mobile_api.create_irrigation_zone(name="Zone 2", field=block, acres=2.0)
		self.assertEqual(
			STORE.get_raw("Irrigation Zone", data["name"])["area_sq_ft"],
			2.0 * mobile_api.SQ_FT_PER_ACRE,
		)

	def test_a_cabin_takes_no_acreage_and_an_acres_sent_anyway_is_ignored(self):
		"""A cabin is measured in beds, not acres. The handset's uniform sheet
		sends one anyway, and it must not be written somewhere it would be wrong."""
		self.a_parcel()
		self.be(MANAGER)
		data = mobile_api.create_farm_location(
			name="MC-Cabin-01", doctype="Housing Unit", parcel=self.a_parcel(), acres=3.0, capacity=4
		)
		row = STORE.get_raw("Housing Unit", data["name"])
		self.assertEqual(row["capacity"], 4)
		self.assertNotIn("acreage", row)

	def test_a_place_with_no_name_is_refused_and_names_both_spellings(self):
		self.a_parcel()
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.create_field(parcel=self.a_parcel())
		self.assertIn("field_name", str(caught.exception))

	def test_a_missing_parent_is_refused_with_the_register_that_would_answer_it(self):
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.create_field(name="Ridge Top")
		message = str(caught.exception)
		self.assertIn("parcel is required", message)
		self.assertIn("list_farm_locations", message)

	def test_a_parent_in_another_entity_reads_as_not_found_rather_than_refused(self):
		"""So this cannot become a way to discover another farm's parcels by
		watching which error comes back."""
		parcel = self.a_parcel()
		frappe.db.set_value("Parcel", parcel, "owning_entity", OTHER)
		self.be(MANAGER)
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.create_field(name="Ridge Top", parcel=parcel)

	def test_the_negative_control_the_money_arguments_cannot_be_delivered(self):
		"""THE ABSENT ARGUMENTS ARE THE POINT. `bind` keeps only what a signature
		names, so what a piece of ground is worth and who holds the deed are
		unreachable from a handset rather than merely discouraged. Both reach a
		financial statement and are settled at a desk with the paperwork open.
		"""
		accepted = self.accepts(mobile_api.create_parcel)
		for argument in (
			"title_holder",
			"appraised_value",
			"appraiser",
			"appraisal_document",
			"related_asset",
		):
			self.assertNotIn(argument, accepted)
		unit = self.accepts(mobile_api.create_housing_unit)
		for argument in (
			"or_housing_law_compliant",
			"smoke_detector_last_test",
			"last_habitability_inspection",
		):
			self.assertNotIn(argument, unit)

	def test_all_five_writes_run_the_one_gate(self):
		"""One implementation, five spellings — asserted rather than assumed,
		because five copies of a permission check is five places to forget one."""
		for handler in (
			mobile_api.create_farm_location,
			mobile_api.create_field,
			mobile_api.create_irrigation_zone,
			mobile_api.create_parcel,
			mobile_api.create_housing_unit,
		):
			self.assertIn("_create_one_location", inspect.getsource(handler))
		self.assertIn("guard.require_location_role", inspect.getsource(mobile_api._create_one_location))

	def test_every_write_is_declared_mutating_so_the_route_table_agrees(self):
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		for path in (
			"/mobile/create_farm_location",
			"/mobile/create_field",
			"/mobile/create_irrigation_zone",
			"/mobile/create_parcel",
			"/mobile/create_housing_unit",
		):
			self.assertTrue(by_path[path].mutating, path)
		self.assertFalse(by_path["/mobile/list_farm_locations"].mutating)

	# ── the row shape itself ────────────────────────────────────────────────
	def test_a_register_with_no_acreage_column_reports_none_and_never_nought(self):
		"""A cabin is measured in beds. Reporting 0.0 would file every building on
		the farm as zero acres of ground for a picker sorting by size."""
		self.assertIsNone(
			locations.option("Housing Unit", {"name": "U-1", "unit_name": "MC-Cabin-01"})["acreage"]
		)
		self.assertIsNone(locations.option("Field", {"name": "F-1", "field_name": "Ridge Top"})["acreage"])

	def test_a_figure_that_was_given_comes_back_as_it_was_given(self):
		"""Including a nought. The unmeasured/measured-at-zero distinction is lost
		upstream — `farm._describe_field` collapses a missing acreage to 0.0 — and
		re-deriving it here would be a second opinion about a value this function
		did not measure. `list_fields.without_acreage` is where that gap lives.
		"""
		self.assertEqual(
			locations.option("Field", {"name": "F-1", "field_name": "R", "acreage": 0})["acreage"], 0.0
		)
		self.assertEqual(
			locations.option("Field", {"name": "F-1", "field_name": "R", "acreage": 12.5})["acreage"], 12.5
		)

	def test_a_row_with_no_name_column_falls_back_to_the_docname(self):
		"""Rows imported from the other farm system before it had one."""
		self.assertEqual(locations.option("Field", {"name": "F-1"})["label"], "F-1")

	def test_a_zone_is_grouped_by_parcel_and_names_its_block_in_the_detail(self):
		"""A picker that grouped three registers by parcel and one by block would
		put the zones somewhere nobody looked for them."""
		row = locations.option(
			"Irrigation Zone",
			{"name": "Z-1", "zone_name": "Zone 2", "parcel": "Mill Creek", "field": "Ridge Top"},
		)
		self.assertEqual(row["parent_parcel"], "Mill Creek")
		self.assertIn("Ridge Top", row["detail"])

	# ── furniture ───────────────────────────────────────────────────────────
	def a_zone(self, zone_name="Zone 2"):
		block = self.a_block("Ridge Top")
		return self.tool_data(
			"create_irrigation_zone", {"field": block, "zone_name": zone_name, "area_sq_ft": 87120}
		)["name"]

	def a_cabin(self, unit_name="MC-Cabin-01"):
		return self.tool_data(
			"create_housing_unit",
			{"parcel": self.a_parcel(), "unit_name": unit_name, "unit_type": "Cabin", "capacity": 4},
		)["name"]
